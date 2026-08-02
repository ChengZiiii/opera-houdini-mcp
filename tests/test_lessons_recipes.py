"""save_recipe / _agent_source 存储引擎测试（add-workflow-knowledge-capture
tasks 4.1 / 4.2）。

覆盖：
- 4.1：BP-NNN 自增（BP-001→BP-002；已有 BP-001/BP-003 → 新块 BP-004）；
  撞号重试（手工制造重复 heading 场景，id 生成不崩溃）；9 字段校验
  （缺 category / 非法 severity（含 "critical" 被拒且 message 列合法取值）/
  advisory=False 被拒 / verified_versions 默认 "unknown"）；原子写失败保留
  旧文件；空文件与缺失目录自动创建（写后 ``_lessons_search._load_root_recipes``
  可解析）；个人库 source == "agent"（不含 @用户名）；标题 ``>`` 行渲染正确。
- 4.2：团队库归属——写 registry config.json 声明 writable=true 的团队 root，
  monkeypatch ``_lessons.getpass.getuser`` 返回固定值 → 文件里 source ==
  "agent@<用户名>"；save_lesson 团队库同样带 @用户名；writable=false →
  root_not_writable 且零写入；unavailable（state!=ok）拒绝；个人库写入不受
  registry 影响。
- 常量一致性：``RECIPE_SEVERITIES`` 与 ``_best_practices.SEVERITIES`` 同构、
  ``RECIPE_FIELDS`` 与 ``_best_practices.REQUIRED_FIELDS`` 同构。

全部测试使用 TemporaryDirectory + monkeypatch ``_lessons._base_dir``，绝不写
真实 ~/.opera-houdini-mcp。
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _best_practices  # noqa: E402
import _lessons  # noqa: E402
import _lessons_search as lssearch  # noqa: E402
from _lessons import LessonsError  # noqa: E402


def _write(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _block(bid, **overrides):
    """渲染一个可被 parse_best_practices 解析的 BP-NNN 块文本（测试预置用）。"""
    fields = {
        "category": "c", "severity": "medium",
        "affected_versions": "H21", "verified_versions": "unknown",
        "source": "manual", "advisory": "true",
        "problem": "p", "symptom": "s", "fix": "f",
    }
    fields.update(overrides)
    lines = ["### " + bid]
    for key in _best_practices.REQUIRED_FIELDS:
        lines.append("- {0}: {1}".format(key, fields[key]))
    return "\n".join(lines) + "\n"


def _valid_fields(**overrides):
    """save_recipe 合法 fields 骨架；overrides 覆盖任意键。

    verified_versions / title / source 默认省略（分别走默认值 / 可选 / 系统
    标注路径）。
    """
    fields = {
        "category": "rendering",
        "severity": "high",
        "affected_versions": "H21",
        "problem": "渲染输出全黑。",
        "symptom": "输出 exr 全黑，无报错。",
        "fix": "检查 camera 的 near/far 裁剪设置。",
    }
    fields.update(overrides)
    return fields


class _BaseDirFixture(unittest.TestCase):
    """把 _lessons._base_dir 指到临时目录的公共夹具。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self._original_base_dir = _lessons._base_dir
        _lessons._base_dir = lambda: self.base

    def tearDown(self):
        _lessons._base_dir = self._original_base_dir

    def _personal(self):
        return _lessons.knowledge_dir()

    def _write_config(self, entries):
        _write(os.path.join(self.base, "config.json"),
               json.dumps(entries, ensure_ascii=False))

    def _patch_getuser(self, callable_):
        """monkeypatch getpass.getuser 为给定 callable（测试结束自动恢复）。"""
        original = _lessons.getpass.getuser
        _lessons.getpass.getuser = callable_
        self.addCleanup(setattr, _lessons.getpass, "getuser", original)

    def _remember_env(self, key):
        """记录 env key 现值并在测试结束时恢复（防污染其他用例）。"""
        old = os.environ.get(key)
        if old is not None:
            self.addCleanup(os.environ.__setitem__, key, old)
        else:
            self.addCleanup(os.environ.pop, key, None)


# ---------------------------------------------------------------------------
# 4.1 个人库：常量 / 路径 / id 自增 / 校验 / 原子写 / 渲染
# ---------------------------------------------------------------------------
class RecipeConstantsAndPathsTests(_BaseDirFixture):

    def test_recipes_constants_match_best_practices_schema(self):
        self.assertEqual(_lessons.RECIPES_DIRNAME, "recipes")
        self.assertEqual(_lessons.RECIPES_FILENAME, "BEST_PRACTICES.md")
        # 与 _best_practices 同构（spec：9 字段语义同构；severity 枚举只有 3 值）
        self.assertEqual(set(_lessons.RECIPE_FIELDS),
                         set(_best_practices.REQUIRED_FIELDS))
        self.assertEqual(set(_lessons.RECIPE_SEVERITIES),
                         set(_best_practices.SEVERITIES))
        self.assertEqual(set(_lessons.RECIPE_SEVERITIES),
                         {"low", "medium", "high"})
        self.assertNotIn("critical", _lessons.RECIPE_SEVERITIES)

    def test_recipes_path_helper(self):
        self.assertEqual(
            _lessons.recipes_path(self._personal()),
            os.path.join(self.base, "knowledge", "recipes",
                         "BEST_PRACTICES.md"))


class SaveRecipePersonalTests(_BaseDirFixture):

    def test_save_creates_missing_dirs_header_and_bp001(self):
        # root 目录不存在 → 自动创建目录 + header + 首个块
        recipe = _lessons.save_recipe(self._personal(), _valid_fields())
        self.assertEqual(recipe["id"], "BP-001")
        self.assertEqual(recipe["root"], "personal")
        self.assertEqual(recipe["source"], "agent")
        self.assertIs(recipe["advisory"], True)
        self.assertEqual(recipe["verified_versions"], "unknown")
        path = _lessons.recipes_path(self._personal())
        self.assertTrue(os.path.isfile(path))
        text = _read(path)
        self.assertTrue(text.startswith("# BEST PRACTICES"))
        self.assertIn("### BP-001", text)
        # 全文可被 strict parser round-trip
        entries = _best_practices.parse_best_practices(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "BP-001")
        self.assertEqual(entries[0]["source"], "agent")
        self.assertEqual(entries[0]["advisory"], True)

    def test_save_increments_ids(self):
        first = _lessons.save_recipe(self._personal(), _valid_fields())
        second = _lessons.save_recipe(
            self._personal(), _valid_fields(symptom="另一个症状。"))
        self.assertEqual(first["id"], "BP-001")
        self.assertEqual(second["id"], "BP-002")

    def test_save_max_plus_one_skips_gap(self):
        # 文件已有 BP-001 / BP-003（手工编辑留空 BP-002）→ 新块必须 BP-004
        pre = _block("BP-001") + "\n" + _block("BP-003")
        _write(_lessons.recipes_path(self._personal()), pre)
        recipe = _lessons.save_recipe(self._personal(), _valid_fields())
        self.assertEqual(recipe["id"], "BP-004")
        text = _read(_lessons.recipes_path(self._personal()))
        self.assertIn("### BP-004", text)
        self.assertEqual(len(_best_practices.parse_best_practices(text)), 3)

    def test_next_recipe_id_handles_duplicate_headings(self):
        # 撞号重试：手工制造重复 heading 场景，id 生成取 max+1、不崩溃
        self.assertEqual(
            _lessons._next_recipe_id("### BP-001\n### BP-001\n### BP-002\n"),
            "BP-003")
        self.assertEqual(_lessons._next_recipe_id(""), "BP-001")
        self.assertEqual(_lessons._next_recipe_id("### BP-001\n### BP-003\n"),
                         "BP-004")

    def test_save_rejects_corrupt_existing_file_zero_write(self):
        # 既有文件含重复 heading（无法 round-trip）→ 拒绝写入并保留旧文件
        corrupt = _block("BP-001") + _block("BP-001")
        path = _lessons.recipes_path(self._personal())
        _write(path, corrupt)
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_recipe(self._personal(), _valid_fields())
        self.assertEqual(ctx.exception.code, "ls_write_error")
        self.assertIn("自校验", ctx.exception.message)
        self.assertEqual(_read(path), corrupt)

    def test_id_overflow_rejected(self):
        # 候选超 999 → ls_write_error（与检索端 \d{3} 正则保持兼容）
        _write(_lessons.recipes_path(self._personal()), _block("BP-999"))
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_recipe(self._personal(), _valid_fields())
        self.assertEqual(ctx.exception.code, "ls_write_error")
        self.assertIn("999", ctx.exception.message)
        text = _read(_lessons.recipes_path(self._personal()))
        self.assertIn("### BP-999", text)
        self.assertNotIn("BP-1000", text)

    def test_missing_category_rejected(self):
        fields = _valid_fields()
        del fields["category"]
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_recipe(self._personal(), fields)
        self.assertEqual(ctx.exception.code, "ls_write_error")
        self.assertIn("category", ctx.exception.message)

    def test_invalid_severity_rejected_lists_valid_values(self):
        for bad in ("critical", "urgent", None):
            with self.assertRaises(LessonsError) as ctx:
                _lessons.save_recipe(self._personal(),
                                     _valid_fields(severity=bad))
            self.assertEqual(ctx.exception.code, "ls_write_error")
            self.assertIn("low", ctx.exception.message)
            self.assertIn("medium", ctx.exception.message)
            self.assertIn("high", ctx.exception.message)

    def test_advisory_false_rejected(self):
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_recipe(self._personal(), _valid_fields(advisory=False))
        self.assertEqual(ctx.exception.code, "ls_write_error")
        self.assertIn("advisory", ctx.exception.message)

    def test_verified_versions_defaults_unknown(self):
        recipe = _lessons.save_recipe(self._personal(), _valid_fields())
        self.assertEqual(recipe["verified_versions"], "unknown")
        entry = _best_practices.parse_best_practices(
            _read(_lessons.recipes_path(self._personal())))[0]
        self.assertEqual(entry["verified_versions"], "unknown")

    def test_verified_versions_explicit_value_kept(self):
        recipe = _lessons.save_recipe(
            self._personal(), _valid_fields(verified_versions="H21.0 live"))
        self.assertEqual(recipe["verified_versions"], "H21.0 live")

    def test_write_failure_preserves_old_file(self):
        _lessons.save_recipe(self._personal(), _valid_fields())
        path = _lessons.recipes_path(self._personal())
        before = _read(path)

        real_replace = _lessons.os.replace

        def boom(src, dst):
            raise OSError("simulated replace failure")

        _lessons.os.replace = boom
        try:
            with self.assertRaises(LessonsError) as ctx:
                _lessons.save_recipe(self._personal(),
                                     _valid_fields(symptom="另一个症状。"))
            self.assertEqual(ctx.exception.code, "ls_write_error")
        finally:
            _lessons.os.replace = real_replace

        self.assertEqual(_read(path), before)
        # 无残留临时文件
        leftovers = [n for n in os.listdir(os.path.join(self.base, "knowledge",
                                                        "recipes"))
                     if n != "BEST_PRACTICES.md"]
        self.assertEqual(leftovers, [])

    def test_empty_existing_file_gets_header(self):
        path = _lessons.recipes_path(self._personal())
        _write(path, "")
        recipe = _lessons.save_recipe(self._personal(), _valid_fields())
        self.assertEqual(recipe["id"], "BP-001")
        text = _read(path)
        self.assertTrue(text.startswith("# BEST PRACTICES"))
        self.assertIn("### BP-001", text)
        _best_practices.parse_best_practices(text)  # 不抛即通过

    def test_header_only_file_appends_block(self):
        path = _lessons.recipes_path(self._personal())
        _write(path, "# BEST PRACTICES\n\n> 说明：本文件由人工维护。\n")
        recipe = _lessons.save_recipe(self._personal(), _valid_fields())
        self.assertEqual(recipe["id"], "BP-001")
        entries = _best_practices.parse_best_practices(_read(path))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "BP-001")

    def test_title_renders_as_blockquote_above_block(self):
        recipe = _lessons.save_recipe(
            self._personal(), _valid_fields(title="渲染排查示例"))
        self.assertEqual(recipe["id"], "BP-001")
        text = _read(_lessons.recipes_path(self._personal()))
        self.assertIn("> 渲染排查示例", text)
        # > 行必须位于块 heading 上方（recipe 外，parser 允许）
        self.assertLess(text.index("> 渲染排查示例"), text.index("### BP-001"))
        entries = _best_practices.parse_best_practices(text)
        self.assertEqual(len(entries), 1)
        # title 不参与检索，返回 dict 也不含 title
        self.assertNotIn("title", recipe)

    def test_title_not_rendered_when_appending_to_existing_blocks(self):
        # 追加到已有块的文件：title 仍校验、仍返回，但不落盘（strict parser
        # 只允许首个 heading 之前的 `>` 行，块间 `> title` 会被判为前一块的
        # 非法正文 → 自校验失败 → ls_write_error）
        first = _lessons.save_recipe(
            self._personal(), _valid_fields(title="首块标题"))
        second = _lessons.save_recipe(
            self._personal(),
            _valid_fields(title="第二条标题", symptom="另一个症状。"))
        self.assertEqual(first["id"], "BP-001")
        self.assertEqual(second["id"], "BP-002")
        text = _read(_lessons.recipes_path(self._personal()))
        # 首块 title 仍在（位于 BP-001 上方），但追加块的 title 不落盘
        self.assertIn("> 首块标题", text)
        self.assertNotIn("> 第二条标题", text)
        # 全文 round-trip：两块都在、无非法正文
        entries = _best_practices.parse_best_practices(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["id"], "BP-001")
        self.assertEqual(entries[1]["id"], "BP-002")

    def test_title_rendered_when_appending_to_header_only_file(self):
        # header-only 文件（无任何 BP 块）+ 带 title 追加 → 视为首块场景，
        # title 渲染在首块上方（parser 只允许首个 heading 前的 `>` 行）
        path = _lessons.recipes_path(self._personal())
        _write(path, "# BEST PRACTICES\n\n> 说明：本文件由人工维护。\n")
        recipe = _lessons.save_recipe(
            self._personal(), _valid_fields(title="渲染排查示例"))
        self.assertEqual(recipe["id"], "BP-001")
        text = _read(path)
        self.assertIn("> 渲染排查示例", text)
        self.assertLess(text.index("> 渲染排查示例"), text.index("### BP-001"))
        entries = _best_practices.parse_best_practices(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "BP-001")

    def test_personal_source_never_stamped_with_username(self):
        # 个人库写入不受 getpass 影响：source 恒为 agent，无 @ 后缀
        self._patch_getuser(lambda: "tester")
        recipe = _lessons.save_recipe(self._personal(), _valid_fields())
        self.assertEqual(recipe["source"], "agent")
        self.assertNotIn("@", recipe["source"])

    def test_user_supplied_source_ignored(self):
        # source 不接受用户传入，由系统标注
        recipe = _lessons.save_recipe(self._personal(),
                                      _valid_fields(source="hacker"))
        self.assertEqual(recipe["source"], "agent")
        text = _read(_lessons.recipes_path(self._personal()))
        self.assertIn("- source: agent", text)
        self.assertNotIn("hacker", text)

    def test_body_line_prefix_safety(self):
        for bad in ("- 列表项", "# 标题", "> 引用", "### 块"):
            with self.assertRaises(LessonsError) as ctx:
                _lessons.save_recipe(self._personal(),
                                     _valid_fields(problem=bad))
            self.assertEqual(ctx.exception.code, "ls_write_error")
            self.assertIn("problem", ctx.exception.message)

    def test_multiline_body_rejected(self):
        # recipes 严格 parser 的字段值是单行的：多行正文无法 round-trip → 拒绝
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_recipe(self._personal(),
                                 _valid_fields(fix="第一行\n第二行"))
        self.assertEqual(ctx.exception.code, "ls_write_error")

    def test_unknown_field_rejected(self):
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_recipe(self._personal(), _valid_fields(bogus="x"))
        self.assertEqual(ctx.exception.code, "ls_write_error")
        self.assertIn("bogus", ctx.exception.message)

    def test_non_dict_fields_rejected(self):
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_recipe(self._personal(), ["not", "a", "dict"])
        self.assertEqual(ctx.exception.code, "ls_write_error")

    def test_saved_recipe_immediately_parseable_by_search(self):
        # 写入即被 search_lessons 检索：_load_root_recipes 立即可解析
        _lessons.save_recipe(self._personal(), _valid_fields())
        recipes, error = lssearch._load_root_recipes(self._personal())
        self.assertIsNone(error)
        self.assertEqual(len(recipes), 1)
        self.assertEqual(recipes[0]["id"], "BP-001")
        self.assertEqual(recipes[0]["source"], "agent")
        self.assertEqual(recipes[0]["category"], "rendering")
        self.assertEqual(recipes[0]["severity"], "high")
        self.assertIs(recipes[0]["advisory"], True)


# ---------------------------------------------------------------------------
# 4.2 团队库归属：@用户名 / root 闸门
# ---------------------------------------------------------------------------
class SaveRecipeTeamRootTests(_BaseDirFixture):

    def _make_team_root(self, name="teamx", writable=True):
        self._write_config([{"name": name, "path": name,
                             "writable": writable}])
        root = os.path.join(self.base, name)
        os.makedirs(root, exist_ok=True)
        return root

    def test_team_root_source_agent_at_username(self):
        team_root = self._make_team_root()
        self._patch_getuser(lambda: "tester")
        recipe = _lessons.save_recipe(team_root, _valid_fields())
        self.assertEqual(recipe["source"], "agent@tester")
        self.assertEqual(recipe["root"], "teamx")
        text = _read(_lessons.recipes_path(team_root))
        self.assertIn("- source: agent@tester", text)
        entry = _best_practices.parse_best_practices(text)[0]
        self.assertEqual(entry["source"], "agent@tester")

    def test_team_root_appends_after_existing_recipes(self):
        team_root = self._make_team_root()
        _write(_lessons.recipes_path(team_root), _block("BP-007"))
        self._patch_getuser(lambda: "tester")
        recipe = _lessons.save_recipe(team_root, _valid_fields())
        self.assertEqual(recipe["id"], "BP-008")
        self.assertEqual(recipe["source"], "agent@tester")

    def test_save_lesson_team_root_stamps_username(self):
        team_root = self._make_team_root()
        self._patch_getuser(lambda: "tester")
        lesson = _lessons.save_lesson(team_root, {
            "title": "团队问题",
            "category": "api",
            "severity": "high",
            "affected_versions": "H21",
            "source": "unit-test",
            "advisory": False,
            "problem": "问题描述。",
            "symptom": "团队库报错现象。",
            "fix": "解决办法。",
        })
        self.assertEqual(lesson["source"], "unit-test@tester")
        path = os.path.join(_lessons.lessons_dir(team_root),
                            lesson["id"] + ".md")
        reparsed = _lessons.parse_lesson(_read(path))
        self.assertEqual(reparsed["source"], "unit-test@tester")

    def test_save_lesson_personal_source_unchanged(self):
        self._patch_getuser(lambda: "tester")
        lesson = _lessons.save_lesson(self._personal(), {
            "title": "个人问题",
            "category": "api",
            "severity": "medium",
            "affected_versions": "H21",
            "source": "unit-test",
            "advisory": False,
            "problem": "问题描述。",
            "symptom": "个人库报错现象。",
            "fix": "解决办法。",
        })
        self.assertEqual(lesson["source"], "unit-test")
        self.assertNotIn("@", lesson["source"])

    def test_writable_false_root_not_writable_zero_write(self):
        ro_root = self._make_team_root(name="ro", writable=False)
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_recipe(ro_root, _valid_fields())
        self.assertEqual(ctx.exception.code, "root_not_writable")
        self.assertFalse(os.path.exists(_lessons.recipes_path(ro_root)))
        # save_lesson 同样被闸门拒绝，零写入
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_lesson(ro_root, {
                "title": "x", "category": "api", "severity": "high",
                "affected_versions": "H21", "source": "s", "advisory": False,
                "problem": "p", "symptom": "s", "fix": "f",
            })
        self.assertEqual(ctx.exception.code, "root_not_writable")
        self.assertEqual(os.listdir(ro_root), [])

    def test_unavailable_root_rejected(self):
        # 目录不存在 → state=unavailable → root_not_writable
        self._write_config([{"name": "miss", "path": "miss",
                             "writable": True}])
        miss_root = os.path.join(self.base, "miss")
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_recipe(miss_root, _valid_fields())
        self.assertEqual(ctx.exception.code, "root_not_writable")
        self.assertFalse(os.path.exists(miss_root))

    def test_personal_write_unaffected_by_registry(self):
        # registry 声明只读团队 root 不影响个人库写入
        self._make_team_root(name="ro", writable=False)
        recipe = _lessons.save_recipe(self._personal(), _valid_fields())
        self.assertEqual(recipe["id"], "BP-001")
        self.assertEqual(recipe["source"], "agent")


# ---------------------------------------------------------------------------
# _agent_source：personal 判定与用户名回退链
# ---------------------------------------------------------------------------
class AgentSourceTests(_BaseDirFixture):

    def test_personal_root_returns_none(self):
        self.assertIsNone(_lessons._agent_source(self._personal()))

    def test_team_root_uses_getpass(self):
        self._patch_getuser(lambda: "tester")
        self.assertEqual(
            _lessons._agent_source(os.path.join(self.base, "teamx")),
            "tester")

    def test_getpass_failure_falls_back_to_username_env(self):
        def boom():
            raise OSError("no controlling tty")

        self._patch_getuser(boom)
        self._remember_env("USERNAME")
        os.environ["USERNAME"] = "envuser"
        self.assertEqual(
            _lessons._agent_source(os.path.join(self.base, "teamx")),
            "envuser")

    def test_getpass_failure_without_env_falls_back_unknown_user(self):
        def boom():
            raise OSError("no controlling tty")

        self._patch_getuser(boom)
        self._remember_env("USERNAME")
        os.environ.pop("USERNAME", None)
        self.assertEqual(
            _lessons._agent_source(os.path.join(self.base, "teamx")),
            "unknown-user")

    def test_never_raises_on_weird_exceptions(self):
        def boom():
            raise RuntimeError("getpass totally broken")

        self._patch_getuser(boom)
        self._remember_env("USERNAME")
        os.environ.pop("USERNAME", None)
        # 任何异常路径都不抛，只回退
        self.assertEqual(
            _lessons._agent_source(os.path.join(self.base, "teamx")),
            "unknown-user")


if __name__ == "__main__":
    unittest.main()
