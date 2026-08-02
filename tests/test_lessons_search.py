"""_lessons_search 检索与融合测试（tasks 3.x + 6.5 / 6.6）。

覆盖：
- tokenizer：_rag 复用 + 中文 2-gram shingles（渲染失败 → 渲染失败/渲染/染失/失败）。
- 新鲜度衰减数学：90 天起线性衰减至 0.7 下限（180 天）。
- BM25 排序（tf 加权）、中文查询、指纹精确命中 ×2.0、strength ×(1+0.1N)、
  新鲜度（200 天 vs 新鲜）、root priority 融合 + source_root、tie-break id asc。
- 紧凑摘要：无全文、symptom/fix 截断 ~200ch、top_k clamp [1,5]。
- 过滤：category / severity 精确、node_type / houdini_version 子串，AND 组合。
- recipes：kind="recipe" + source_root；非法 recipes → _warning 不崩。
- hint + draft_suggestions（inbox count >= 3）。
- 缓存：命中不重写、source_sig 变化重建、corrupt → rebuild。
- cap 语义：returned_count == len(results)，truncated 对齐。
- unavailable root → _warning，personal 不受影响；unconfigured 静默跳过。
- find_lesson_by_id / compute_stats helpers。
- 无嵌入模型源码扫描（_lessons_search.py + import 闭包）。

全部使用 TemporaryDirectory + monkeypatch `_lessons._base_dir`。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _common as cmn  # noqa: E402
import _lessons  # noqa: E402
from _lessons import LessonsError  # noqa: E402
import _lessons_search as lssearch  # noqa: E402


# ---------------------------------------------------------------------------
# 样本构造 helpers
# ---------------------------------------------------------------------------
def _iso_ago(days):
    """返回距 now 恰好 days 天的 ISO 8601 字符串（带 tz offset）。"""
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat()


def _make_lesson(**overrides):
    """lesson dict 骨架；overrides 覆盖任意字段。"""
    lesson = {
        "id": "L-20260802-001",
        "status": "published",
        "strength": 1,
        "root": "personal",
        "created_at": _iso_ago(10),
        "updated_at": _iso_ago(2),
        "category": "rendering",
        "severity": "high",
        "affected_versions": "H21, H22",
        "verified_versions": "H21.0",
        "source": "unit-test",
        "advisory": True,
        "fingerprint": "a" * 64,
        "title": "Test lesson alpha",
        "problem": "Problem body alpha",
        "symptom": "camera near plane large",
        "fix": "Fix body alpha",
    }
    lesson.update(overrides)
    return lesson


def _render_lesson(lesson):
    """把 lesson dict 渲染为可 parse_lesson 的 markdown 文本。"""
    lines = ["---"]
    for key in ("id", "status", "strength", "root", "created_at", "updated_at",
                "category", "severity", "affected_versions",
                "verified_versions", "source", "advisory", "fingerprint"):
        value = lesson.get(key)
        if value is None:
            continue
        if key in ("created_at", "updated_at"):
            lines.append('{0}: "{1}"'.format(key, value))
        elif key == "advisory":
            lines.append("advisory: " + ("true" if value else "false"))
        elif key == "strength":
            lines.append("strength: {0}".format(int(value)))
        else:
            lines.append("{0}: {1}".format(key, value))
    lines.append("---")
    lines.append("")
    lines.append("# " + lesson["title"])
    for section in ("problem", "symptom", "fix"):
        lines.append("")
        lines.append("## " + section.capitalize())
        lines.append(lesson[section])
    return "\n".join(lines) + "\n"


def _recipes_text(recipes):
    """把 recipe dict 列表渲染为可 parse_best_practices 的 markdown 文本。"""
    lines = ["# 团队最佳实践", ""]
    for recipe in recipes:
        lines.append("### " + recipe["id"])
        for key in ("category", "severity", "affected_versions",
                    "verified_versions", "source", "advisory",
                    "problem", "symptom", "fix"):
            value = recipe[key]
            if key == "advisory":
                value = "true" if value else "false"
            lines.append("- {0}: {1}".format(key, value))
        lines.append("")
    return "\n".join(lines) + "\n"


def _write(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class _BaseDirFixture(unittest.TestCase):
    """把 _lessons._base_dir 指到临时目录 + 清空检索进程内缓存的公共夹具。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self._original_base_dir = _lessons._base_dir
        _lessons._base_dir = lambda: self.base
        lssearch.clear_cache()

    def tearDown(self):
        _lessons._base_dir = self._original_base_dir

    def _personal(self):
        return os.path.join(self.base, "knowledge")

    def _write_config(self, entries):
        _write(os.path.join(self.base, "config.json"),
               json.dumps(entries, ensure_ascii=False))

    def _write_lesson(self, root_path, lesson):
        """写入一条 lesson 到 root_path/lessons/<id>.md。"""
        return _write(os.path.join(root_path, "lessons", lesson["id"] + ".md"),
                      _render_lesson(lesson))

    def _write_recipes(self, root_path, text):
        return _write(os.path.join(root_path, "recipes", "BEST_PRACTICES.md"),
                      text)

    def _write_inbox(self, root_path, events):
        """events: [{fingerprint, count, message, ...}] → inbox/events.jsonl。"""
        payload = ""
        for event in events:
            payload += json.dumps(event, ensure_ascii=False) + "\n"
        return _write(os.path.join(root_path, "inbox", "events.jsonl"),
                      payload)

    def _remember_env(self, key):
        old = os.environ.get(key)
        if old is not None:
            self.addCleanup(os.environ.__setitem__, key, old)
        else:
            self.addCleanup(os.environ.pop, key, None)


# ---------------------------------------------------------------------------
# tokenizer：中文 2-gram shingles
# ---------------------------------------------------------------------------
class TokenizerTests(unittest.TestCase):

    def test_chinese_run_produces_shingles(self):
        toks = lssearch.tokenize("渲染失败")
        for expected in ("渲染失败", "渲染", "染失", "失败"):
            self.assertIn(expected, toks)

    def test_mixed_chinese_and_hou_api_tokens(self):
        toks = lssearch.tokenize("hou.node camera 渲染失败")
        self.assertIn("hou.node", toks)
        self.assertIn("camera", toks)
        self.assertIn("渲染失败", toks)

    def test_single_cjk_char_kept(self):
        toks = lssearch.tokenize("黑")
        self.assertIn("黑", toks)

    def test_non_string_returns_empty(self):
        self.assertEqual(lssearch.tokenize(None), [])
        self.assertEqual(lssearch.tokenize(123), [])


# ---------------------------------------------------------------------------
# 新鲜度衰减数学
# ---------------------------------------------------------------------------
class FreshnessDecayTests(unittest.TestCase):

    def test_within_90_days_is_fresh(self):
        self.assertEqual(lssearch.freshness_decay(_iso_ago(89)), 1.0)
        self.assertEqual(lssearch.freshness_decay(_iso_ago(90)), 1.0)

    def test_linear_midpoint_at_135_days(self):
        self.assertAlmostEqual(lssearch.freshness_decay(_iso_ago(135)),
                               0.85, delta=1e-9)

    def test_floor_at_180_days_and_beyond(self):
        self.assertEqual(lssearch.freshness_decay(_iso_ago(180)), 0.7)
        self.assertEqual(lssearch.freshness_decay(_iso_ago(365)), 0.7)

    def test_invalid_or_naive_timestamp_no_decay(self):
        self.assertEqual(lssearch.freshness_decay("not-a-date"), 1.0)
        self.assertEqual(lssearch.freshness_decay("2026-08-02T09:30:00"), 1.0)


# ---------------------------------------------------------------------------
# 基础检索：BM25 / 中文 / 空查询 / draft 排除 / top_k / 摘要
# ---------------------------------------------------------------------------
class SearchBasicTests(_BaseDirFixture):

    def test_bm25_ranks_higher_tf_first(self):
        a = _make_lesson(id="L-20260802-001", symptom="camera near plane large",
                         fingerprint="a" * 64)
        b = _make_lesson(
            id="L-20260802-002",
            symptom=("camera near plane large camera near plane large "
                     "camera near plane large camera near plane large"),
            fingerprint="b" * 64)
        self._write_lesson(self._personal(), a)
        self._write_lesson(self._personal(), b)
        env = lssearch.search_lessons("camera near plane large")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched"], 2)
        self.assertEqual([r["id"] for r in env["results"]],
                         ["L-20260802-002", "L-20260802-001"])

    def test_chinese_query_ranks_chinese_lesson(self):
        a = _make_lesson(id="L-20260802-001",
                         title="渲染输出全黑排查",
                         symptom="渲染失败时输出全黑画面",
                         fingerprint="a" * 64)
        b = _make_lesson(id="L-20260802-002",
                         title="灯光参数调整",
                         symptom="灯光参数调整导致泛白",
                         fingerprint="b" * 64)
        self._write_lesson(self._personal(), a)
        self._write_lesson(self._personal(), b)
        env = lssearch.search_lessons("渲染失败")
        self.assertEqual(env["matched"], 1)
        self.assertEqual(env["results"][0]["id"], "L-20260802-001")

    def test_empty_query_baseline_returns_all_published(self):
        for num in (1, 2, 3):
            lesson = _make_lesson(id="L-20260802-{0:03d}".format(num),
                                  symptom="baseline symptom {0}".format(num),
                                  fingerprint=chr(96 + num) * 64)
            self._write_lesson(self._personal(), lesson)
        for query in (None, ""):
            env = lssearch.search_lessons(query)
            self.assertEqual(env["status"], "success")
            self.assertEqual(env["matched"], 3)
            self.assertEqual(len(env["results"]), 3)
            # 全部 baseline 同分 → tie-break id asc
            self.assertEqual([r["id"] for r in env["results"]],
                             ["L-20260802-001", "L-20260802-002",
                              "L-20260802-003"])

    def test_drafts_never_indexed_but_findable(self):
        published = _make_lesson(
            id="L-20260802-001", symptom="karma crash on exit",
            fingerprint="a" * 64)
        draft = _make_lesson(
            id="L-20260802-002", status="draft", symptom="karma crash on exit",
            fingerprint="b" * 64)
        self._write_lesson(self._personal(), published)
        self._write_lesson(self._personal(), draft)
        env = lssearch.search_lessons("karma crash on exit")
        self.assertEqual([r["id"] for r in env["results"]],
                         ["L-20260802-001"])
        found = lssearch.find_lesson_by_id(draft["id"])
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "draft")
        self.assertEqual(found["root"], "personal")

    def test_top_k_clamped_to_1_5(self):
        for num in (1, 2, 3, 4, 5):
            lesson = _make_lesson(id="L-20260802-{0:03d}".format(num),
                                  symptom="clamp symptom {0}".format(num),
                                  fingerprint=chr(96 + num) * 64)
            self._write_lesson(self._personal(), lesson)
        env = lssearch.search_lessons(None, top_k=2)
        self.assertEqual(env["top_k"], 2)
        self.assertEqual(len(env["results"]), 2)
        self.assertEqual(env["matched"], 5)
        self.assertEqual(env["truncated"], True)
        env = lssearch.search_lessons(None, top_k=99)
        self.assertEqual(env["top_k"], 5)
        self.assertEqual(len(env["results"]), 5)
        env = lssearch.search_lessons(None, top_k=0)
        self.assertEqual(env["top_k"], 1)
        self.assertEqual(len(env["results"]), 1)
        env = lssearch.search_lessons(None, top_k="abc")
        self.assertEqual(env["top_k"], 5)
        self.assertEqual(len(env["results"]), 5)

    def test_compact_summary_has_no_full_text_and_truncates(self):
        lesson = _make_lesson(
            id="L-20260802-001",
            symptom="s" * 250,
            fix="f" * 250,
            problem="SUPER_SECRET_PROBLEM_MARKER_XYZ",
            fingerprint="a" * 64)
        self._write_lesson(self._personal(), lesson)
        env = lssearch.search_lessons(None)
        result = env["results"][0]
        self.assertEqual(len(result["symptom"]), 200)
        self.assertEqual(len(result["fix"]), 200)
        self.assertNotIn("problem", result)
        self.assertNotIn("SUPER_SECRET_PROBLEM_MARKER_XYZ",
                         json.dumps(env["results"], ensure_ascii=False))
        self.assertEqual(
            set(result.keys()),
            {"id", "kind", "title", "category", "severity", "symptom",
             "fix", "verified_versions", "source_root", "strength"})

    def test_filters_combined_as_and(self):
        a = _make_lesson(id="L-20260802-001", category="rendering",
                         severity="high", affected_versions="H21, H22",
                         symptom="box transform reset on cook",
                         fingerprint="a" * 64)
        b = _make_lesson(id="L-20260802-002", category="lighting",
                         severity="medium", affected_versions="H22",
                         symptom="vdb visualisation slow",
                         fingerprint="b" * 64)
        c = _make_lesson(id="L-20260802-003", category="rendering",
                         severity="low", affected_versions="H21",
                         symptom="box shading broken",
                         fingerprint="c" * 64)
        for lesson in (a, b, c):
            self._write_lesson(self._personal(), lesson)

        def ids(**kwargs):
            env = lssearch.search_lessons(None, **kwargs)
            return [r["id"] for r in env["results"]], env["matched"]

        got, _ = ids(category="rendering")
        self.assertEqual(got, ["L-20260802-001", "L-20260802-003"])
        got, _ = ids(severity="high")
        self.assertEqual(got, ["L-20260802-001"])
        got, _ = ids(category="rendering", severity="high")
        self.assertEqual(got, ["L-20260802-001"])
        got, _ = ids(node_type="box")
        self.assertEqual(got, ["L-20260802-001", "L-20260802-003"])
        got, _ = ids(node_type="vdb")
        self.assertEqual(got, ["L-20260802-002"])
        got, _ = ids(houdini_version="H21")
        self.assertEqual(got, ["L-20260802-001", "L-20260802-003"])
        got, _ = ids(houdini_version="H22")
        self.assertEqual(got, ["L-20260802-001", "L-20260802-002"])
        got, _ = ids(category="rendering", severity="high",
                     node_type="box", houdini_version="H21")
        self.assertEqual(got, ["L-20260802-001"])
        got, matched = ids(category="lighting", severity="high")
        self.assertEqual(got, [])
        self.assertEqual(matched, 0)


# ---------------------------------------------------------------------------
# 融合评分
# ---------------------------------------------------------------------------
class FusionTests(_BaseDirFixture):

    def _setup_pair(self, **overrides):
        """写两条内容一致（除 id）的 lesson，返回 (a, b)。"""
        a = _make_lesson(id="L-20260802-001", fingerprint="a" * 64)
        b = _make_lesson(id="L-20260802-002", fingerprint="b" * 64)
        a.update(overrides.pop("a_override", {}))
        b.update(overrides.pop("b_override", {}))
        self._write_lesson(self._personal(), a)
        self._write_lesson(self._personal(), b)
        return a, b

    def test_fingerprint_exact_match_boost_doubles(self):
        # B 的 BM25 更高（query token tf ×4）；A 靠指纹精确命中 ×2.0 反超
        query = "camera near plane large"
        a = _make_lesson(
            id="L-20260802-001", symptom=query,
            fingerprint=_lessons.make_fingerprint(query))
        b = _make_lesson(
            id="L-20260802-002",
            symptom=(query + " " + query + " " + query + " " + query),
            fingerprint="b" * 64)
        self._write_lesson(self._personal(), a)
        self._write_lesson(self._personal(), b)
        env = lssearch.search_lessons(query)
        self.assertEqual([r["id"] for r in env["results"]],
                         ["L-20260802-001", "L-20260802-002"])

    def test_strength_weighting_5_beats_1(self):
        symptom = "flipbook export fails with timeout"
        a = _make_lesson(id="L-20260802-001", strength=5, symptom=symptom,
                         fingerprint="a" * 64)
        b = _make_lesson(id="L-20260802-002", strength=1, symptom=symptom,
                         fingerprint="b" * 64)
        self._write_lesson(self._personal(), a)
        self._write_lesson(self._personal(), b)
        env = lssearch.search_lessons("flipbook export fails")
        self.assertEqual(env["results"][0]["id"], "L-20260802-001")
        self.assertEqual(env["results"][0]["strength"], 5)

    def test_freshness_200_days_old_ranked_below_fresh(self):
        symptom = "karma render crashes on final frame"
        a = _make_lesson(id="L-20260802-001", symptom=symptom,
                         updated_at=_iso_ago(2), fingerprint="a" * 64)
        b = _make_lesson(id="L-20260802-002", symptom=symptom,
                         updated_at=_iso_ago(200), fingerprint="b" * 64)
        self._write_lesson(self._personal(), a)
        self._write_lesson(self._personal(), b)
        env = lssearch.search_lessons("karma render crashes")
        self.assertEqual([r["id"] for r in env["results"]],
                         ["L-20260802-001", "L-20260802-002"])

    def test_root_priority_merge_and_source_root(self):
        symptom = "xpu memory leak spike"
        personal = _make_lesson(id="L-20260802-001", symptom=symptom,
                                fingerprint="a" * 64)
        teamx = _make_lesson(id="L-20260802-001", root="teamx",
                             symptom=symptom, fingerprint="b" * 64)
        teamhi = _make_lesson(id="L-20260802-001", root="teamhi",
                              symptom=symptom, fingerprint="c" * 64)
        for root_name, lesson in (("personal", personal),
                                  ("teamx", teamx), ("teamhi", teamhi)):
            root_path = (self._personal() if root_name == "personal"
                         else os.path.join(self.base, root_name))
            os.makedirs(root_path, exist_ok=True)
            self._write_lesson(root_path, lesson)
        self._write_config([
            {"name": "teamx", "path": "teamx"},
            {"name": "teamhi", "path": "teamhi", "priority": 0.9},
        ])
        env = lssearch.search_lessons(symptom)
        # 同 BM25 同强度同新鲜度 → 仅 priority 排序：1.0 / 0.9 / 0.5
        self.assertEqual([r["source_root"] for r in env["results"]],
                         ["personal", "teamhi", "teamx"])
        for result in env["results"]:
            self.assertIn("source_root", result)
        self.assertEqual(env["results"][0]["source_root"], "personal")

    def test_tie_break_id_ascending(self):
        symptom = "identical symptom text"
        for num in (1, 2):
            lesson = _make_lesson(id="L-20260802-{0:03d}".format(num),
                                  symptom=symptom,
                                  fingerprint="a" * 64)
            self._write_lesson(self._personal(), lesson)
        env = lssearch.search_lessons("identical symptom")
        self.assertEqual([r["id"] for r in env["results"]],
                         ["L-20260802-001", "L-20260802-002"])


# ---------------------------------------------------------------------------
# recipes
# ---------------------------------------------------------------------------
class RecipeTests(_BaseDirFixture):

    def setUp(self):
        super(RecipeTests, self).setUp()
        self.team = os.path.join(self.base, "teamx")
        os.makedirs(self.team, exist_ok=True)
        self._write_config([{"name": "teamx", "path": "teamx"}])

    def _recipe(self, **overrides):
        recipe = {
            "id": "BP-001",
            "category": "caching",
            "severity": "high",
            "affected_versions": "H21",
            "verified_versions": "H21.0",
            "source": "team-docs",
            "advisory": True,
            "problem": "file cache returns stale data",
            "symptom": "cached hip references old file",
            "fix": "clear cache and recook",
        }
        recipe.update(overrides)
        return recipe

    def test_recipes_kind_recipe_with_source_root(self):
        self._write_recipes(self.team, _recipes_text([self._recipe()]))
        personal = _make_lesson(
            id="L-20260802-001", category="api",
            symptom="unrelated unrelated unrelated",
            fingerprint="a" * 64)
        self._write_lesson(self._personal(), personal)
        env = lssearch.search_lessons("stale file cache")
        recipe_results = [r for r in env["results"] if r["kind"] == "recipe"]
        self.assertEqual(len(recipe_results), 1)
        result = recipe_results[0]
        self.assertEqual(result["id"], "BP-001")
        self.assertEqual(result["kind"], "recipe")
        self.assertEqual(result["source_root"], "teamx")
        self.assertEqual(result["category"], "caching")
        self.assertEqual(result["severity"], "high")
        self.assertEqual(result["verified_versions"], "H21.0")
        self.assertNotIn("strength", result)
        self.assertNotIn("hint", result)
        self.assertEqual(
            set(result.keys()),
            {"id", "kind", "title", "category", "severity", "symptom",
             "fix", "verified_versions", "source_root"})

    def test_invalid_recipes_warn_and_skip_no_crash(self):
        self._write_recipes(self.team, "### BP-001\n- category: caching\n")
        personal = _make_lesson(
            id="L-20260802-001",
            symptom="xpu memory leak spike",
            fingerprint="a" * 64)
        self._write_lesson(self._personal(), personal)
        env = lssearch.search_lessons("xpu memory leak spike")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["results"][0]["id"], "L-20260802-001")
        self.assertIn("_warning", env)
        self.assertTrue(any("recipes" in w for w in env["_warning"]))

    def test_header_only_recipes_file_is_silent_no_warning(self):
        # 可空设计：文件存在但无任何 ### BP-NNN 块 → 视为无 recipes，不告警
        self._write_recipes(self.team, "# 团队最佳实践\n\n> 说明\n")
        personal = _make_lesson(
            id="L-20260802-001",
            symptom="header only recipes probe",
            fingerprint="a" * 64)
        self._write_lesson(self._personal(), personal)
        env = lssearch.search_lessons("header only recipes probe")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched"], 1)
        self.assertEqual(env["results"][0]["id"], "L-20260802-001")
        self.assertNotIn("_warning", env)


# ---------------------------------------------------------------------------
# hint + draft_suggestions
# ---------------------------------------------------------------------------
class HintAndSuggestionTests(_BaseDirFixture):

    def test_hint_on_frequent_fingerprint_and_draft_suggestions(self):
        a = _make_lesson(
            id="L-20260802-001",
            symptom="camera black output black",
            fingerprint=_lessons.make_fingerprint("camera black output black"))
        d = _make_lesson(
            id="L-20260802-002", status="draft",
            symptom="draft skeleton text",
            fingerprint=_lessons.make_fingerprint("draft skeleton text"))
        e = _make_lesson(
            id="L-20260802-003",
            symptom="sparse hint issue",
            fingerprint=_lessons.make_fingerprint("sparse hint issue"))
        for lesson in (a, d, e):
            self._write_lesson(self._personal(), lesson)
        self._write_inbox(self._personal(), [
            {"fingerprint": a["fingerprint"], "count": 3,
             "message": "camera black output black"},
            {"fingerprint": d["fingerprint"], "count": 5,
             "message": "draft skeleton text"},
            {"fingerprint": e["fingerprint"], "count": 2,
             "message": "sparse hint issue"},
        ])

        env = lssearch.search_lessons("camera black output")
        result = env["results"][0]
        self.assertEqual(result["id"], "L-20260802-001")
        self.assertEqual(result["hint"], "已踩 3 次，请补充 fix")
        # draft_suggestions：只含 count>=3 的 draft 骨架，published 不出现
        suggestions = env["draft_suggestions"]
        self.assertEqual(len(suggestions), 1)
        suggestion = suggestions[0]
        self.assertEqual(suggestion["id"], "L-20260802-002")
        self.assertEqual(suggestion["root"], "personal")
        self.assertEqual(suggestion["count"], 5)
        self.assertEqual(suggestion["symptom"], "draft skeleton text")

        env = lssearch.search_lessons("sparse hint issue")
        self.assertNotIn("hint", env["results"][0])


# ---------------------------------------------------------------------------
# draft_suggestions 上限 / symptom 截断
# ---------------------------------------------------------------------------
class DraftSuggestionCapTests(_BaseDirFixture):

    def test_draft_suggestions_capped_and_symptom_truncated(self):
        long_symptom = "d" * 250
        for num in range(1, 8):
            lesson = _make_lesson(
                id="L-20260802-{0:03d}".format(100 + num),
                status="draft",
                symptom=long_symptom,
                fingerprint=chr(96 + num) * 64)
            self._write_lesson(self._personal(), lesson)
        self._write_inbox(self._personal(), [
            {"fingerprint": chr(96 + num) * 64, "count": 3 + num,
             "message": "ev{0}".format(num)}
            for num in range(1, 8)
        ])
        env = lssearch.search_lessons(None)
        self.assertEqual(env["status"], "success")
        suggestions = env["draft_suggestions"]
        # 超过 5 条 count>=3 的 draft → 最多返回 5 条
        self.assertLessEqual(len(suggestions), 5)
        for suggestion in suggestions:
            self.assertLessEqual(len(suggestion["symptom"]), 200)
        # count 降序
        counts = [s["count"] for s in suggestions]
        self.assertEqual(counts, sorted(counts, reverse=True))


# ---------------------------------------------------------------------------
# 缓存：语义损坏（schema 合法但数值/引用损坏）→ 静默重建
# ---------------------------------------------------------------------------
class CorruptSemanticCacheTests(_BaseDirFixture):
    """缓存 JSON 顶层结构合法但内容语义损坏时，检索必须静默重建而非崩溃。"""

    def _cache_path(self, root_name="personal"):
        return os.path.join(_lessons.cache_index_dir(root_name),
                            lssearch.INDEX_FILENAME)

    def _build_and_corrupt(self, mutate):
        """先建索引落盘缓存，再按 mutate 篡改缓存 JSON（sig 保持有效）。"""
        lesson = _make_lesson(id="L-20260802-001",
                              symptom="semantic cache probe",
                              fingerprint="a" * 64)
        self._write_lesson(self._personal(), lesson)
        baseline = lssearch.search_lessons("semantic cache probe")
        cache_path = self._cache_path()
        with open(cache_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        mutate(payload)
        _write(cache_path, json.dumps(payload, ensure_ascii=False))
        lssearch.clear_cache()  # 模拟新进程：内存缓存清空
        return baseline, cache_path

    def test_corrupt_posting_tf_string_rebuilds(self):
        baseline, cache_path = self._build_and_corrupt(
            lambda payload: payload["postings"]["probe"]
            .update({"L-20260802-001": "abc"}))
        env = lssearch.search_lessons("semantic cache probe")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["results"], baseline["results"])
        with open(cache_path, "r", encoding="utf-8") as handle:
            repaired = json.load(handle)
        self.assertEqual(repaired["postings"]["probe"]["L-20260802-001"], 1)

    def test_non_numeric_avgdl_rebuilds(self):
        baseline, cache_path = self._build_and_corrupt(
            lambda payload: payload.__setitem__("avgdl", "not-a-number"))
        env = lssearch.search_lessons("semantic cache probe")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["results"], baseline["results"])
        with open(cache_path, "r", encoding="utf-8") as handle:
            repaired = json.load(handle)
        self.assertIsInstance(repaired["avgdl"], (int, float))

    def test_posting_referencing_missing_doc_rebuilds(self):
        baseline, cache_path = self._build_and_corrupt(
            lambda payload: payload["postings"]
            .__setitem__("phantom", {"L-20260802-999": 1}))
        env = lssearch.search_lessons("semantic cache probe")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["results"], baseline["results"])
        # 重建后损坏的 dangling posting 被清除
        with open(cache_path, "r", encoding="utf-8") as handle:
            repaired = json.load(handle)
        self.assertNotIn("phantom", repaired["postings"])


# ---------------------------------------------------------------------------
# 同一 root 内重复 lesson id（手工编辑文件）→ 跳过 + 告警，检索不崩
# ---------------------------------------------------------------------------
class DuplicateIdTests(_BaseDirFixture):

    def test_duplicate_lesson_id_within_root_warns_and_keeps_one(self):
        a = _make_lesson(id="L-20260802-001", symptom="dup id probe alpha",
                         fingerprint="a" * 64)
        b = _make_lesson(id="L-20260802-001", symptom="dup id probe beta",
                         fingerprint="b" * 64)
        lessons_dir = os.path.join(self._personal(), "lessons")
        os.makedirs(lessons_dir, exist_ok=True)
        _write(os.path.join(lessons_dir, "L-20260802-001.md"),
               _render_lesson(a))
        _write(os.path.join(lessons_dir, "L-20260802-001-dup.md"),
               _render_lesson(b))
        env = lssearch.search_lessons("dup id probe")
        self.assertEqual(env["status"], "success")
        # 去重后只索引一份，检索正常
        self.assertEqual(env["matched"], 1)
        self.assertEqual(len(env["results"]), 1)
        # 重复被记录为 warning，而不是静默崩溃 / 静默折叠
        self.assertIn("_warning", env)
        self.assertTrue(any("重复" in w for w in env["_warning"]))


# ---------------------------------------------------------------------------
# 缓存：命中 / invalidate / corrupt-rebuild
# ---------------------------------------------------------------------------
class CacheTests(_BaseDirFixture):

    def _cache_path(self, root_name="personal"):
        return os.path.join(_lessons.cache_index_dir(root_name),
                            lssearch.INDEX_FILENAME)

    def test_cache_file_created_and_hit_does_not_rewrite(self):
        lesson = _make_lesson(id="L-20260802-001",
                              symptom="cache me now please",
                              fingerprint="a" * 64)
        self._write_lesson(self._personal(), lesson)
        env1 = lssearch.search_lessons("cache me now please")
        cache_path = self._cache_path()
        self.assertTrue(os.path.isfile(cache_path))
        with open(cache_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["schema"], lssearch.SCHEMA_NAME)
        self.assertEqual(data["version"], lssearch.SCHEMA_VERSION)
        mtime1 = os.stat(cache_path).st_mtime_ns
        env2 = lssearch.search_lessons("cache me now please")
        self.assertEqual(env1["results"], env2["results"])
        # 缓存命中：不重写磁盘文件
        self.assertEqual(os.stat(cache_path).st_mtime_ns, mtime1)

    def test_corrupt_cache_rebuilds(self):
        lesson = _make_lesson(id="L-20260802-001",
                              symptom="corrupt cache test",
                              fingerprint="a" * 64)
        self._write_lesson(self._personal(), lesson)
        first = lssearch.search_lessons("corrupt cache test")
        _write(self._cache_path(), "{{{not-json")
        lssearch.clear_cache()  # 模拟新进程：内存缓存清空
        env = lssearch.search_lessons("corrupt cache test")
        self.assertEqual(env["results"], first["results"])
        with open(self._cache_path(), "r", encoding="utf-8") as handle:
            repaired = json.load(handle)
        self.assertEqual(repaired["schema"], lssearch.SCHEMA_NAME)

    def test_cache_invalidated_on_lesson_change(self):
        a = _make_lesson(id="L-20260802-001",
                         symptom="original query tokens zeta",
                         fingerprint="a" * 64)
        self._write_lesson(self._personal(), a)
        lssearch.search_lessons("original query tokens zeta")
        with open(self._cache_path(), "r", encoding="utf-8") as handle:
            sig1 = json.load(handle)["source_sig"]
        b = _make_lesson(id="L-20260802-002",
                         symptom="brand new tokens omega",
                         fingerprint="b" * 64)
        self._write_lesson(self._personal(), b)
        env = lssearch.search_lessons("brand new tokens omega")
        self.assertEqual(env["results"][0]["id"], "L-20260802-002")
        with open(self._cache_path(), "r", encoding="utf-8") as handle:
            sig2 = json.load(handle)["source_sig"]
        self.assertNotEqual(sig1, sig2)


# ---------------------------------------------------------------------------
# cap 语义
# ---------------------------------------------------------------------------
class CapTests(_BaseDirFixture):

    def _write_five(self):
        for num in (1, 2, 3, 4, 5):
            lesson = _make_lesson(id="L-20260802-{0:03d}".format(num),
                                  symptom="cap symptom {0}".format(num),
                                  fingerprint=chr(96 + num) * 64)
            self._write_lesson(self._personal(), lesson)

    def test_cap_truncation_aligns_counts(self):
        self._write_five()
        env = lssearch.search_lessons(
            None, response_cap_fn=cmn.apply_response_cap, max_bytes=300)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["returned_count"], len(env["results"]))
        self.assertLessEqual(env["returned_count"], env["matched"])
        if env["returned_count"] < env["matched"]:
            self.assertEqual(env["truncated"], True)

    def test_no_cap_truncation_when_small(self):
        self._write_five()
        env = lssearch.search_lessons(None)
        self.assertEqual(env["truncated"], False)
        self.assertEqual(env["returned_count"], len(env["results"]))
        self.assertEqual(env["returned_count"], 5)
        for key in ("status", "query", "top_k", "matched", "returned_count",
                    "truncated", "results", "draft_suggestions"):
            self.assertIn(key, env)


# ---------------------------------------------------------------------------
# unavailable / unconfigured root 降级
# ---------------------------------------------------------------------------
class DegradationTests(_BaseDirFixture):

    def test_unavailable_root_warns_and_personal_unaffected(self):
        self._write_config([{"name": "gone", "path": "missing-dir"}])
        lesson = _make_lesson(id="L-20260802-001",
                              symptom="team offline but personal works",
                              fingerprint="a" * 64)
        self._write_lesson(self._personal(), lesson)
        env = lssearch.search_lessons("team offline but personal works")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["results"][0]["id"], "L-20260802-001")
        self.assertIn("_warning", env)
        self.assertTrue(any("gone" in w for w in env["_warning"]))

    def test_unconfigured_root_silent_no_warning(self):
        self._remember_env("LS_SEARCH_UNSET_VAR")
        os.environ.pop("LS_SEARCH_UNSET_VAR", None)
        self._write_config([{"name": "undef",
                             "path": "${LS_SEARCH_UNSET_VAR}"}])
        lesson = _make_lesson(id="L-20260802-001",
                              symptom="single machine mode",
                              fingerprint="a" * 64)
        self._write_lesson(self._personal(), lesson)
        env = lssearch.search_lessons("single machine mode")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["results"][0]["id"], "L-20260802-001")
        self.assertNotIn("_warning", env)

    def test_unknown_scope_returns_error_envelope(self):
        env = lssearch.search_lessons("anything", scope="nope")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_unknown_root")
        self.assertEqual(env["results"], [])
        self.assertEqual(env["returned_count"], 0)

    def test_single_scope_searches_only_that_root(self):
        team = os.path.join(self.base, "teamx")
        os.makedirs(team, exist_ok=True)
        self._write_config([{"name": "teamx", "path": "teamx"}])
        team_lesson = _make_lesson(id="L-20260802-001", root="teamx",
                                   symptom="team only secret",
                                   fingerprint="b" * 64)
        self._write_lesson(team, team_lesson)
        personal = _make_lesson(id="L-20260802-001",
                                symptom="team only secret",
                                fingerprint="a" * 64)
        self._write_lesson(self._personal(), personal)
        env = lssearch.search_lessons("team only secret", scope="teamx")
        self.assertEqual(len(env["results"]), 1)
        self.assertEqual(env["results"][0]["source_root"], "teamx")


# ---------------------------------------------------------------------------
# scope 名字比较：首尾空白需 strip 后匹配
# ---------------------------------------------------------------------------
class ScopeWhitespaceTests(_BaseDirFixture):

    def setUp(self):
        super(ScopeWhitespaceTests, self).setUp()
        self.team = os.path.join(self.base, "teamx")
        os.makedirs(self.team, exist_ok=True)
        self._write_config([{"name": "teamx", "path": "teamx"}])
        self.team_lesson = _make_lesson(
            id="L-20260802-001", root="teamx", symptom="whitespace scope probe",
            fingerprint="b" * 64)
        self.personal_lesson = _make_lesson(
            id="L-20260802-001", symptom="whitespace scope probe",
            fingerprint="a" * 64)
        self._write_lesson(self.team, self.team_lesson)
        self._write_lesson(self._personal(), self.personal_lesson)

    def test_scope_with_surrounding_whitespace_searches_that_root(self):
        env = lssearch.search_lessons("whitespace scope probe",
                                      scope="  teamx  ")
        self.assertEqual(env["status"], "success")
        self.assertEqual(len(env["results"]), 1)
        self.assertEqual(env["results"][0]["source_root"], "teamx")

    def test_scope_all_whitespace_means_all_roots(self):
        env = lssearch.search_lessons("whitespace scope probe", scope="   ")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched"], 2)

    def test_compute_stats_scope_with_surrounding_whitespace(self):
        stats = lssearch.compute_stats("  teamx  ")
        self.assertEqual(len(stats["roots"]), 1)
        self.assertEqual(stats["roots"][0]["name"], "teamx")

    def test_find_lesson_by_id_scope_with_whitespace(self):
        found = lssearch.find_lesson_by_id(self.team_lesson["id"],
                                           scope=" teamx ")
        self.assertIsNotNone(found)
        self.assertEqual(found["root"], "teamx")


# ---------------------------------------------------------------------------
# 边界场景：空语料 / 全停用词查询 / 超大 recipes 文件
# ---------------------------------------------------------------------------
class EdgeCaseTests(_BaseDirFixture):

    def test_empty_corpus_success_matched_zero(self):
        env = lssearch.search_lessons(None)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched"], 0)
        self.assertEqual(env["results"], [])
        self.assertEqual(env["returned_count"], 0)
        self.assertNotIn("_warning", env)

    def test_all_stopword_query_matched_zero(self):
        self._write_lesson(self._personal(), _make_lesson(
            id="L-20260802-001", symptom="camera near plane large",
            fingerprint="a" * 64))
        env = lssearch.search_lessons("the and of is")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched"], 0)
        self.assertEqual(env["results"], [])

    def test_oversized_recipes_file_results_capped(self):
        team = os.path.join(self.base, "teamx")
        os.makedirs(team, exist_ok=True)
        self._write_config([{"name": "teamx", "path": "teamx"}])
        recipes = []
        for num in range(1, 11):
            recipes.append({
                "id": "BP-{0:03d}".format(num),
                "category": "caching",
                "severity": "high",
                "affected_versions": "H21",
                "verified_versions": "H21.0",
                "source": "team-docs",
                "advisory": True,
                "problem": "problem {0}".format(num),
                "symptom": "symptom token {0}".format(num),
                "fix": "fix {0}".format(num),
            })
        self._write_recipes(team, _recipes_text(recipes))
        env = lssearch.search_lessons(None, top_k=5)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched"], 10)
        self.assertEqual(len(env["results"]), 5)
        self.assertEqual(env["truncated"], True)
        self.assertTrue(all(r["kind"] == "recipe" for r in env["results"]))


# ---------------------------------------------------------------------------
# helpers：find_lesson_by_id / compute_stats
# ---------------------------------------------------------------------------
class HelperTests(_BaseDirFixture):

    def setUp(self):
        super(HelperTests, self).setUp()
        self.team = os.path.join(self.base, "teamx")
        os.makedirs(self.team, exist_ok=True)
        self._write_config([{"name": "teamx", "path": "teamx"}])
        self.published = _make_lesson(
            id="L-20260802-001", symptom="published helper text",
            fingerprint="a" * 64)
        self.draft = _make_lesson(
            id="L-20260802-002", status="draft", symptom="draft helper text",
            fingerprint="b" * 64)
        self.team_lesson = _make_lesson(
            id="L-20260802-010", root="teamx", symptom="team helper text",
            fingerprint="c" * 64)
        self._write_lesson(self._personal(), self.published)
        self._write_lesson(self._personal(), self.draft)
        self._write_lesson(self.team, self.team_lesson)

    def test_find_lesson_by_id_returns_full_dict(self):
        found = lssearch.find_lesson_by_id(self.published["id"])
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], self.published["id"])
        self.assertEqual(found["title"], self.published["title"])
        self.assertEqual(found["body_symptom"], self.published["symptom"])
        self.assertEqual(found["body_problem"], self.published["problem"])
        self.assertEqual(found["body_fix"], self.published["fix"])
        self.assertEqual(found["root"], "personal")
        self.assertTrue(os.path.isfile(found["file_path"]))

    def test_find_lesson_by_id_includes_drafts(self):
        found = lssearch.find_lesson_by_id(self.draft["id"])
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "draft")
        self.assertEqual(found["body_symptom"], self.draft["symptom"])

    def test_find_lesson_by_id_unknown_returns_none(self):
        self.assertIsNone(lssearch.find_lesson_by_id("L-20260802-999"))

    def test_find_lesson_by_id_respects_scope(self):
        found = lssearch.find_lesson_by_id(self.team_lesson["id"],
                                           scope="teamx")
        self.assertIsNotNone(found)
        self.assertEqual(found["root"], "teamx")
        self.assertIsNone(lssearch.find_lesson_by_id(self.team_lesson["id"],
                                                     scope="personal"))

    def test_find_lesson_by_id_unknown_scope_raises(self):
        with self.assertRaises(LessonsError) as ctx:
            lssearch.find_lesson_by_id(self.published["id"], scope="nope")
        self.assertEqual(ctx.exception.code, "ls_unknown_root")

    def test_compute_stats_counts(self):
        self._write_inbox(self._personal(), [
            {"fingerprint": "d" * 64, "count": 1, "message": "ev1"},
            {"fingerprint": "e" * 64, "count": 2, "message": "ev2"},
        ])
        self._write_recipes(self.team, _recipes_text([{
            "id": "BP-001", "category": "caching", "severity": "high",
            "affected_versions": "H21", "verified_versions": "H21.0",
            "source": "team-docs", "advisory": True,
            "problem": "p", "symptom": "s", "fix": "f",
        }]))
        stats = lssearch.compute_stats()
        roots = {r["name"]: r for r in stats["roots"]}
        self.assertEqual(list(roots.keys()), ["personal", "teamx"])
        personal = roots["personal"]
        self.assertEqual(personal["state"], "ok")
        self.assertEqual(personal["priority"], 1.0)
        self.assertEqual(personal["writable"], True)
        self.assertEqual(personal["path"], self._personal())
        self.assertEqual(personal["lesson_count"], 2)
        self.assertEqual(personal["published_count"], 1)
        self.assertEqual(personal["draft_count"], 1)
        self.assertEqual(personal["inbox_count"], 2)
        self.assertEqual(personal["recipes_count"], 0)
        teamx = roots["teamx"]
        self.assertEqual(teamx["lesson_count"], 1)
        self.assertEqual(teamx["published_count"], 1)
        self.assertEqual(teamx["draft_count"], 0)
        self.assertEqual(teamx["inbox_count"], 0)
        self.assertEqual(teamx["recipes_count"], 1)

    def test_compute_stats_scope_single(self):
        stats = lssearch.compute_stats("teamx")
        self.assertEqual(len(stats["roots"]), 1)
        self.assertEqual(stats["roots"][0]["name"], "teamx")

    def test_compute_stats_unknown_scope_raises(self):
        with self.assertRaises(LessonsError) as ctx:
            lssearch.compute_stats("nope")
        self.assertEqual(ctx.exception.code, "ls_unknown_root")


# ---------------------------------------------------------------------------
# 无嵌入模型不变量（源码扫描）
# ---------------------------------------------------------------------------
class NoEmbeddingDependencyScanTests(unittest.TestCase):
    """对 _lessons_search.py 及其 import 闭包做源码扫描：禁止 embedding /
    vector store / reranker 相关库名与 import 关键字。"""

    FORBIDDEN_LIBS = ("sentence_transformers", "fastembed", "chromadb",
                      "qdrant")
    FORBIDDEN_IMPORT_KEYWORDS = ("embedding", "vector", "rerank")

    def _local_import_names(self, text):
        names = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("import "):
                for clause in stripped[len("import "):].split(","):
                    top = clause.strip().split(" as ")[0].strip()
                    if top:
                        names.add(top.split(".")[0])
            elif stripped.startswith("from "):
                mod, _, symbols = stripped[len("from "):].partition(" import ")
                mod = mod.strip()
                if mod.startswith("."):
                    rel = [part for part in mod.split(".") if part]
                    if rel:
                        names.add(rel[-1])
                    symbol = symbols.split(" as ")[0].strip().split(",")[0]
                    if symbol:
                        names.add(symbol)
                elif mod:
                    names.add(mod.split(".")[0])
        return names

    def _closure_sources(self):
        sources = []
        seen = set()
        stack = [os.path.join(ROOT, "_lessons_search.py")]
        while stack:
            path = stack.pop()
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            with open(real, "r", encoding="utf-8") as handle:
                text = handle.read()
            sources.append(text)
            for name in self._local_import_names(text):
                candidate = os.path.join(ROOT, name + ".py")
                if os.path.isfile(candidate):
                    stack.append(candidate)
        return sources

    def test_closure_scan_covers_search_module_and_dependencies(self):
        sources = self._closure_sources()
        joined = "\n".join(sources)
        self.assertIn("def search_lessons", joined)
        self.assertGreaterEqual(len(sources), 2)

    def test_no_embedding_vector_library_dependencies(self):
        for text in self._closure_sources():
            for lib in self.FORBIDDEN_LIBS:
                self.assertNotIn(
                    lib, text,
                    "禁止依赖嵌入/向量库 {0!r}".format(lib))

    def test_no_embedding_vector_rerank_keywords_in_imports(self):
        for text in self._closure_sources():
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith(("import ", "from ")):
                    continue
                lowered = stripped.lower()
                for keyword in self.FORBIDDEN_IMPORT_KEYWORDS:
                    self.assertNotIn(
                        keyword, lowered,
                        "import 语句禁止含 {0!r}: {1!r}".format(
                            keyword, stripped))


if __name__ == "__main__":
    unittest.main()
