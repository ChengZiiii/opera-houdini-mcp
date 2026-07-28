"""BEST_PRACTICES 知识库的 strict parser、cache、query、count 与 envelope 测试。

覆盖：
- task 4.1：全部 schema 错误 + 四类 error code，断言 envelope 形状完全一致。
- task 4.2：mtime_ns / size cache hit、变更、stat/read race。
- task 4.3：query AND、Unicode casefold、零匹配、cap 截断、matched/returned count。
- task 4.4：对 BP-001..010 lint（source 非空、verified_versions 非推测、advisory=true、id 唯一）。
- bridge tool 集成（tasks 3.1-3.4）：统一 envelope、returned_count == len、cap defense-in-depth。
"""

import importlib.util
import os
import sys
import tempfile
import textwrap
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_bp():
    """以独立 module name 加载 _best_practices，避免与 package import 冲突。"""
    name = "test_bp_isolated._best_practices"
    if name in sys.modules:
        return sys.modules[name]
    # _best_practices 顶层 fallback `import _common`；确保 flat import 可用。
    import _common  # noqa: F401  (ensures _common on sys.path as top-level)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "_best_practices.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bp = _load_bp()
import _common as cmn  # noqa: E402


VALID_RECIPE = textwrap.dedent("""\
    # title

    > intro

    ### BP-001

    - category: api
    - severity: medium
    - affected_versions: H21.0
    - verified_versions: H21.0 fork live smoke
    - source: https://example.com/docs; tests/x.py
    - advisory: true
    - problem: the problem
    - symptom: the symptom
    - fix: the fix
    """)


def _write(tmpdir, name, text):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _make_stat(mtime_ns, size):
    stat = types.SimpleNamespace()
    stat.st_mtime_ns = mtime_ns
    stat.st_size = size
    return stat


# ---------------------------------------------------------------------------
# task 4.1：schema 错误
# ---------------------------------------------------------------------------
class ParserSchemaTests(unittest.TestCase):

    def test_valid_minimal_recipe_parses(self):
        entries = bp.parse_best_practices(VALID_RECIPE)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], "BP-001")
        self.assertEqual(entry["advisory"], True)
        self.assertEqual(entry["severity"], "medium")
        # id + 9 字段
        expected_keys = {"id", "category", "severity", "affected_versions",
                         "verified_versions", "source", "advisory",
                         "problem", "symptom", "fix"}
        self.assertEqual(set(entry.keys()), expected_keys)

    def _assert_parse_error(self, text):
        with self.assertRaises(bp.BestPracticesError) as ctx:
            bp.parse_best_practices(text)
        self.assertEqual(ctx.exception.code, "bp_parse_error")
        return ctx.exception

    def test_duplicate_id_rejected(self):
        text = VALID_RECIPE + VALID_RECIPE.replace("BP-001", "BP-001")
        self._assert_parse_error(text)

    def test_duplicate_field_rejected(self):
        text = VALID_RECIPE + "- category: dup\n"
        self._assert_parse_error(text)

    def test_unknown_field_rejected(self):
        text = VALID_RECIPE.replace(
            "- fix: the fix\n",
            "- fix: the fix\n- bogus: value\n")
        self._assert_parse_error(text)

    def test_missing_field_rejected(self):
        text = VALID_RECIPE.replace("- fix: the fix\n", "")
        self._assert_parse_error(text)

    def test_invalid_severity_rejected(self):
        text = VALID_RECIPE.replace("- severity: medium\n",
                                    "- severity: critical\n")
        self._assert_parse_error(text)

    def test_advisory_not_bool_rejected(self):
        text = VALID_RECIPE.replace("- advisory: true\n",
                                    "- advisory: maybe\n")
        self._assert_parse_error(text)

    def test_advisory_false_rejected(self):
        text = VALID_RECIPE.replace("- advisory: true\n",
                                    "- advisory: false\n")
        self._assert_parse_error(text)

    def test_empty_source_rejected(self):
        text = VALID_RECIPE.replace(
            "- source: https://example.com/docs; tests/x.py\n",
            "- source:   \n")
        self._assert_parse_error(text)

    def test_empty_verified_versions_rejected(self):
        text = VALID_RECIPE.replace(
            "- verified_versions: H21.0 fork live smoke\n",
            "- verified_versions: \n")
        self._assert_parse_error(text)

    def test_body_outside_recipe_rejected(self):
        # recipe 外的裸正文（非 blank/#/>）
        text = "# title\n\nrandom prose line\n\n" + VALID_RECIPE.split("\n\n", 1)[1]
        self._assert_parse_error(text)

    def test_body_inside_recipe_rejected(self):
        text = VALID_RECIPE.replace(
            "- fix: the fix\n", "- fix: the fix\nstray line in recipe\n")
        self._assert_parse_error(text)

    def test_no_recipes_rejected(self):
        self._assert_parse_error("# title\n\n> only intro\n")

    def test_hash_and_blockquote_outside_ok(self):
        text = "# h1\n\n## h2\n\n> quote\n\n" + \
               VALID_RECIPE.split("# title\n\n> intro\n\n", 1)[1]
        entries = bp.parse_best_practices(text)
        self.assertEqual(len(entries), 1)

    def test_partial_entries_not_returned(self):
        # 第一个 recipe 合法、第二个缺字段 → 整个文件失败，不返回第一个
        good = VALID_RECIPE.split("# title\n\n> intro\n\n", 1)[1]
        bad = good.replace("BP-001", "BP-002").replace(
            "- fix: the fix\n", "")
        with self.assertRaises(bp.BestPracticesError):
            bp.parse_best_practices(good + bad)


# ---------------------------------------------------------------------------
# task 4.1：四类 error code + 统一 envelope shape
# ---------------------------------------------------------------------------
class ErrorContractTests(unittest.TestCase):

    def _assert_envelope_shape(self, env, status):
        self.assertEqual(env["status"], status)
        self.assertIsInstance(env["practices"], list)
        for key in ("total_indexed", "matched_count", "returned_count"):
            self.assertIn(key, env)
            self.assertIsInstance(env[key], int)
        if status == "error":
            err = env["error"]
            self.assertIn(err["code"], (
                "bp_not_found", "bp_read_error",
                "bp_parse_error", "bp_query_error"))
            self.assertIsInstance(err["message"], str)
            self.assertIsInstance(err["details"], dict)
            self.assertEqual(env["practices"], [])
            self.assertEqual(env["total_indexed"], 0)
            self.assertEqual(env["matched_count"], 0)
            self.assertEqual(env["returned_count"], 0)

    def test_not_found_envelope(self):
        env = bp.get_best_practices(path=os.path.join(ROOT, "does_not_exist.md"))
        self.assertEqual(env["error"]["code"], "bp_not_found")
        self._assert_envelope_shape(env, "error")

    def test_read_error_envelope(self):
        # 目录：stat 成功但 open 失败 → bp_read_error
        env = bp.get_best_practices(path=ROOT)
        self.assertEqual(env["error"]["code"], "bp_read_error")
        self._assert_envelope_shape(env, "error")

    def test_query_error_envelope(self):
        # 非法参数类型 → bp_query_error（不读文件）
        env = bp.get_best_practices(query=123)
        self.assertEqual(env["error"]["code"], "bp_query_error")
        self._assert_envelope_shape(env, "error")

    def test_parse_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.md", "### BP-001\n- category: x\n")
            env = bp.get_best_practices(path=path)
        self.assertEqual(env["error"]["code"], "bp_parse_error")
        self._assert_envelope_shape(env, "error")

    def test_success_envelope_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "ok.md", VALID_RECIPE)
            env = bp.get_best_practices(path=path)
        self._assert_envelope_shape(env, "success")


# ---------------------------------------------------------------------------
# task 4.2：cache
# ---------------------------------------------------------------------------
class CacheTests(unittest.TestCase):

    def setUp(self):
        bp.clear_cache()

    def test_cache_hit_avoids_reparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bp.md", VALID_RECIPE)
            original_parse = bp.parse_best_practices
            calls = []

            def counting_parse(text):
                calls.append(text)
                return original_parse(text)

            bp.parse_best_practices = counting_parse
            try:
                first = bp.load_best_practices(path)
                second = bp.load_best_practices(path)
            finally:
                bp.parse_best_practices = original_parse
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "cache hit should skip reparse")

    def test_cache_invalidated_on_size_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bp.md", VALID_RECIPE)
            first = bp.load_best_practices(path)
            # 追加第二条 recipe 块（size 变化）；不含 # title 序言，
            # 否则 # title 会落进 BP-001 recipe 正文内被严格 parser 拒绝
            block = VALID_RECIPE.split("# title\n\n> intro\n\n", 1)[1]
            extra = block.replace("BP-001", "BP-002")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n" + extra)
            second = bp.load_best_practices(path)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 2)

    def test_cache_invalidated_on_mtime_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bp.md", VALID_RECIPE)
            first = bp.load_best_practices(path)
            # 同 size 不同内容 + 显式推进 mtime
            swapped = VALID_RECIPE.replace("the problem", "a different problem")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(swapped)
            os.utime(path, ns=(10 ** 18, 10 ** 18))
            second = bp.load_best_practices(path)
        self.assertEqual(first[0]["problem"], "the problem")
        self.assertEqual(second[0]["problem"], "a different problem")

    def test_stat_read_race_resolves_after_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bp.md", VALID_RECIPE)
            real_stat = os.stat(path)
            # stat 序列：第一次读前 T1；读后 T2（变化→重试）；第二次读后 T2（稳定）
            stats = iter([
                _make_stat(1000, real_stat.st_size),
                _make_stat(2000, real_stat.st_size),
                _make_stat(2000, real_stat.st_size),
                _make_stat(2000, real_stat.st_size),
            ])
            original_stat = bp.os.stat

            def fake_stat(p):
                if os.path.abspath(p) == os.path.abspath(path):
                    return next(stats)
                return original_stat(p)

            bp.os.stat = fake_stat
            try:
                entries = bp.load_best_practices(path)
            finally:
                bp.os.stat = original_stat
        self.assertEqual(len(entries), 1)

    def test_stat_read_race_persistent_change_is_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bp.md", VALID_RECIPE)
            real_stat = os.stat(path)
            counter = {"n": 0}

            def fake_stat(p):
                if os.path.abspath(p) == os.path.abspath(path):
                    counter["n"] += 1
                    # 每次都返回不同 mtime → 永不稳定
                    return _make_stat(1000 + counter["n"], real_stat.st_size)
                return os.stat(p)

            original_stat = bp.os.stat
            bp.os.stat = fake_stat
            try:
                with self.assertRaises(bp.BestPracticesError) as ctx:
                    bp.load_best_practices(path)
            finally:
                bp.os.stat = original_stat
        self.assertEqual(ctx.exception.code, "bp_read_error")


# ---------------------------------------------------------------------------
# task 4.3：query / count / cap
# ---------------------------------------------------------------------------
class QueryAndCountTests(unittest.TestCase):

    def setUp(self):
        bp.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _write(self.tmp.name, "bp.md", VALID_RECIPE)
        self.entries = bp.load_best_practices(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_query_and_combination(self):
        # category=api AND query 命中
        matched = bp.query_best_practices(
            self.entries, query="problem", category="api")
        self.assertEqual(len(matched), 1)
        # category 不匹配 → 0
        self.assertEqual(
            bp.query_best_practices(self.entries, category="rendering"), [])

    def test_unicode_casefold(self):
        matched = bp.query_best_practices(self.entries, query="THE PROBLEM")
        self.assertEqual(len(matched), 1)

    def test_zero_match(self):
        self.assertEqual(
            bp.query_best_practices(self.entries, query="zzz-not-present"), [])

    def test_id_exact(self):
        self.assertEqual(
            len(bp.query_best_practices(self.entries, bp_id="BP-001")), 1)
        self.assertEqual(
            bp.query_best_practices(self.entries, bp_id="BP-999"), [])

    def test_cap_truncation_and_counts(self):
        env = bp.get_best_practices(
            path=self.path, max_bytes=200, response_cap_fn=None)
        # 200 字节装不下完整 recipe → 截断
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched_count"], 1)
        self.assertEqual(env["returned_count"], len(env["practices"]))
        self.assertLessEqual(env["returned_count"], env["matched_count"])
        if env["returned_count"] < env["matched_count"]:
            self.assertEqual(env["truncated"], True)

    def test_cap_no_truncation_when_fits(self):
        env = bp.get_best_practices(path=self.path, max_bytes=100000)
        self.assertEqual(env["returned_count"], 1)
        self.assertEqual(env["truncated"], False)
        self.assertEqual(env["returned_count"], len(env["practices"]))


# ---------------------------------------------------------------------------
# task 3.4：cap defense-in-depth（count 不因 cap 失真）
# ---------------------------------------------------------------------------
class CapDefenseInDepthTests(unittest.TestCase):

    def setUp(self):
        bp.clear_cache()
        # 构造多条 recipe，使 apply_response_cap 真正截断 practices
        block = VALID_RECIPE.split("# title\n\n> intro\n\n", 1)[1]
        text = "# kb\n\n" + "".join(
            block.replace("BP-001", "BP-{0:03d}".format(i))
            .replace("the problem", "problem {0}".format(i))
            for i in range(1, 9))
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _write(self.tmp.name, "bp.md", text)
        self.entries = bp.load_best_practices(self.path)
        self.assertEqual(len(self.entries), 8)

    def tearDown(self):
        self.tmp.cleanup()

    def test_returned_count_aligned_after_apply_response_cap(self):
        env = bp.get_best_practices(
            path=self.path, max_bytes=600,
            response_cap_fn=cmn.apply_response_cap)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched_count"], 8)
        self.assertEqual(env["returned_count"], len(env["practices"]))
        # cap 应该真的截断了（600 字节装不下 8 条）
        self.assertLess(env["returned_count"], env["matched_count"])
        self.assertEqual(env["truncated"], True)

    def test_large_budget_returns_all(self):
        env = bp.get_best_practices(
            path=self.path, max_bytes=100000,
            response_cap_fn=cmn.apply_response_cap)
        self.assertEqual(env["returned_count"], 8)
        self.assertEqual(env["truncated"], False)
        self.assertEqual(env["returned_count"], len(env["practices"]))


# ---------------------------------------------------------------------------
# task 4.4：对真实 BEST_PRACTICES.md 的 BP-001..010 lint
# ---------------------------------------------------------------------------
class RealFileLintTests(unittest.TestCase):

    def setUp(self):
        bp.clear_cache()
        self.entries = bp.load_best_practices()  # 默认 BEST_PRACTICES.md

    def test_parses_exactly_ten_recipes(self):
        self.assertEqual(len(self.entries), 10)

    def test_ids_are_unique_and_in_range(self):
        ids = [e["id"] for e in self.entries]
        self.assertEqual(len(set(ids)), 10)
        for i in range(1, 11):
            self.assertIn("BP-{0:03d}".format(i), ids)

    def test_each_source_nonempty(self):
        for entry in self.entries:
            self.assertTrue(entry["source"].strip(),
                            "{0} source 空".format(entry["id"]))

    def test_advisory_true_for_all(self):
        for entry in self.entries:
            self.assertIs(entry["advisory"], True,
                          "{0} advisory 非 true".format(entry["id"]))

    def test_verified_versions_non_speculative(self):
        for entry in self.entries:
            vv = entry["verified_versions"]
            lower = vv.lower()
            # 不得用 >=21 / all 这类外推；H22 未 live smoke 不得写为已验证
            self.assertNotIn(">=21", lower.replace(" ", ""),
                             "{0} verified_versions 含 >=21 外推".format(entry["id"]))
            self.assertNotIn("all", lower.split())
            self.assertNotIn("h22", lower.replace(".", ""),
                             "{0} 不得在 verified_versions 写 H22（未 live smoke）".format(
                                 entry["id"]))

    def test_bp002_uses_isNewFile(self):
        entry = next(e for e in self.entries if e["id"] == "BP-002")
        self.assertIn("isNewFile", entry["fix"])

    def test_bp004_saveImage_on_GeometryViewport(self):
        entry = next(e for e in self.entries if e["id"] == "BP-004")
        self.assertIn("GeometryViewport", entry["fix"])
        self.assertIn("saveImage", entry["fix"])


# ---------------------------------------------------------------------------
# task 3.1：bridge-local（不建立 Houdini 连接）—— get_best_practices
# 不依赖任何 Houdini 连接对象即可返回结果
# ---------------------------------------------------------------------------
class BridgeLocalTests(unittest.TestCase):

    def setUp(self):
        bp.clear_cache()

    def test_real_file_query_returns_success_without_connection(self):
        env = bp.get_best_practices(category="rendering")
        self.assertEqual(env["status"], "success")
        self.assertGreater(env["total_indexed"], 0)
        self.assertEqual(env["returned_count"], len(env["practices"]))
        for entry in env["practices"]:
            self.assertEqual(entry["category"], "rendering")

    def test_query_filter_includes_source_field(self):
        # source 也参与 query 子串匹配
        env = bp.get_best_practices(query="_render_policy.py")
        self.assertEqual(env["status"], "success")
        self.assertGreater(env["matched_count"], 0)
        for entry in env["practices"]:
            self.assertIn("_render_policy.py", entry["source"])


if __name__ == "__main__":
    unittest.main()
