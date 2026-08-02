"""_lessons 存储引擎测试（section 1：paths / schema / parser / atomic write /
fingerprint / inbox）。

覆盖（对应 OpenSpec delta）：
- 1.1：base dir 推导（expanduser + USERPROFILE 兜底 + HOUDINI_MCP_HOME 覆盖钩子）、
  cache/index/<root-name> 路径 helper。
- 1.2：lesson schema + strict parser（缺失字段 / 未知 key / 非法 severity /
  非法 advisory / 非法 status / 非法 strength / 非法时间戳 / 非法 fingerprint /
  重复 id / published 空 body），loader 逐文件报错不 nuke root。
- 1.3：save_lesson 原子写（同目录 temp + os.replace）、id 防碰撞、失败保留旧文件。
- 1.4：symptom fingerprint 规范化、同 fingerprint 只累加 strength 不改内容、
  inbox ≥3 自动生成 draft 骨架。
- 1.5：inbox append-only + 按 fingerprint 去重累加、事件过大/写失败永不抛异常。

全部测试使用 TemporaryDirectory + monkeypatch `_lessons._base_dir`，绝不写真实
~/.opera-houdini-mcp。
"""

import datetime
import os
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _lessons  # noqa: E402
from _lessons import LessonsError  # noqa: E402


# ---------------------------------------------------------------------------
# 样本 lesson 文本
# ---------------------------------------------------------------------------
FP_A = "a" * 64
FP_B = "b" * 64

VALID_PUBLISHED = textwrap.dedent("""\
    ---
    id: L-20260802-001
    status: published
    strength: 3
    root: personal
    created_at: "2026-08-02T09:30:00+08:00"
    updated_at: "2026-08-02T10:00:00+08:00"
    category: rendering
    severity: high
    affected_versions: H21, H22
    verified_versions: H21.0 fork live smoke
    source: agent-session-20260802
    advisory: true
    fingerprint: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    ---

    # 渲染输出全黑排查

    ## Problem
    渲染结果全黑，但视口显示正常。

    ## Symptom
    输出 exr 全黑，无报错。

    ## Fix
    检查 camera 的 near/far 裁剪设置。
    """)

# draft 骨架：symptom 已知，problem/fix 为空（允许，仅 draft）
VALID_DRAFT = textwrap.dedent("""\
    ---
    id: L-20260802-002
    status: draft
    strength: 1
    root: personal
    created_at: "2026-08-02T09:30:00+08:00"
    updated_at: "2026-08-02T09:30:00+08:00"
    category: unclassified
    severity: medium
    affected_versions: unknown
    verified_versions: unknown
    source: inbox-auto
    advisory: false
    fingerprint: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    ---

    # 自动生成的问题骨架

    ## Problem

    ## Symptom
    某工具报错 foo。

    ## Fix
    """)


def _write(tmpdir, relpath, text):
    path = os.path.join(tmpdir, relpath)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


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


class _FixedClock(object):
    """固定时刻的测试时钟（替换 ``_lessons.datetime.now``，消除跨日 flake）。"""

    def __init__(self, fixed_dt):
        self._fixed = fixed_dt

    def now(self, tz=None):
        return self._fixed


def _assert_parse_error(testcase, text):
    with testcase.assertRaises(LessonsError) as ctx:
        _lessons.parse_lesson(text)
    testcase.assertEqual(ctx.exception.code, "ls_parse_error")
    return ctx.exception


# ---------------------------------------------------------------------------
# 1.1 路径推导
# ---------------------------------------------------------------------------
class BaseDirTests(unittest.TestCase):
    """路径推导测试：确保 _base_dir 是真实实现（其他用例的临时覆盖不能泄漏）。"""

    def setUp(self):
        self._original_base_dir = _lessons._base_dir

    def tearDown(self):
        _lessons._base_dir = self._original_base_dir

    def test_default_derivation_uses_expanduser(self):
        # 未设 HOUDINI_MCP_HOME 时走 expanduser("~") + ".opera-houdini-mcp"
        env = _lessons.os.environ
        old_home = env.get("HOUDINI_MCP_HOME")
        if old_home is not None:
            del env["HOUDINI_MCP_HOME"]
        orig = _lessons.os.path.expanduser
        _lessons.os.path.expanduser = lambda p: "/home/tester"
        try:
            self.assertEqual(
                _lessons._base_dir(),
                os.path.join("/home/tester", ".opera-houdini-mcp"))
        finally:
            _lessons.os.path.expanduser = orig
            if old_home is not None:
                env["HOUDINI_MCP_HOME"] = old_home

    def test_windows_fallback_to_userprofile_when_expanduser_returns_tilde(self):
        # expanduser 返回 "~"（如缺 HOME）→ 兜底 USERPROFILE
        env = _lessons.os.environ
        old_home = env.get("HOUDINI_MCP_HOME")
        old_up = env.get("USERPROFILE")
        orig = _lessons.os.path.expanduser
        _lessons.os.path.expanduser = lambda p: "~"
        env["USERPROFILE"] = r"C:\Users\someone"
        if old_home is not None:
            del env["HOUDINI_MCP_HOME"]
        try:
            self.assertEqual(
                _lessons._base_dir(),
                os.path.join(r"C:\Users\someone", ".opera-houdini-mcp"))
        finally:
            _lessons.os.path.expanduser = orig
            if old_up is None:
                env.pop("USERPROFILE", None)
            else:
                env["USERPROFILE"] = old_up
            if old_home is not None:
                env["HOUDINI_MCP_HOME"] = old_home

    def test_env_override_hook_honored(self):
        env = _lessons.os.environ
        old = env.get("HOUDINI_MCP_HOME")
        env["HOUDINI_MCP_HOME"] = r"C:\mcp_home_test"
        try:
            self.assertEqual(_lessons._base_dir(), r"C:\mcp_home_test")
        finally:
            if old is None:
                env.pop("HOUDINI_MCP_HOME", None)
            else:
                env["HOUDINI_MCP_HOME"] = old

    def test_knowledge_dir_and_cache_index_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _lessons._base_dir = lambda: tmp
            self.assertEqual(
                _lessons.knowledge_dir(),
                os.path.join(tmp, "knowledge"))
            self.assertEqual(
                _lessons.lessons_dir(_lessons.knowledge_dir()),
                os.path.join(tmp, "knowledge", "lessons"))
            self.assertEqual(
                _lessons.inbox_path(_lessons.knowledge_dir()),
                os.path.join(tmp, "knowledge", "inbox", "events.jsonl"))
            self.assertEqual(
                _lessons.cache_index_dir("personal"),
                os.path.join(tmp, "cache", "index", "personal"))
            self.assertEqual(
                _lessons.cache_index_dir("teamx"),
                os.path.join(tmp, "cache", "index", "teamx"))

    def test_cache_index_dir_rejects_path_traversal_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            _lessons._base_dir = lambda: tmp
            for bad in ("../evil", "a/b", "C:\\x", ".."):
                with self.assertRaises(LessonsError) as ctx:
                    _lessons.cache_index_dir(bad)
                self.assertEqual(ctx.exception.code, "ls_unknown_root")


# ---------------------------------------------------------------------------
# 1.2 parser
# ---------------------------------------------------------------------------
class ParserTests(unittest.TestCase):

    def test_valid_published_parses(self):
        lesson = _lessons.parse_lesson(VALID_PUBLISHED)
        self.assertEqual(lesson["id"], "L-20260802-001")
        self.assertEqual(lesson["status"], "published")
        self.assertEqual(lesson["strength"], 3)
        self.assertEqual(lesson["root"], "personal")
        self.assertEqual(lesson["category"], "rendering")
        self.assertEqual(lesson["severity"], "high")
        self.assertEqual(lesson["advisory"], True)
        self.assertEqual(lesson["affected_versions"], "H21, H22")
        self.assertEqual(lesson["created_at"], "2026-08-02T09:30:00+08:00")
        self.assertEqual(lesson["updated_at"], "2026-08-02T10:00:00+08:00")
        self.assertIn("渲染结果全黑", lesson["problem"])
        self.assertIn("输出 exr 全黑", lesson["symptom"])
        self.assertIn("near/far", lesson["fix"])
        self.assertEqual(len(lesson["fingerprint"]), 64)
        # title 从 # 行解析
        self.assertEqual(lesson["title"], "渲染输出全黑排查")
        # 稳定字段集：9 content + metadata（fingerprint 恒在，可为 None）
        expected = {"id", "title", "status", "strength", "root",
                    "created_at", "updated_at", "category", "severity",
                    "affected_versions", "verified_versions", "source",
                    "advisory", "problem", "symptom", "fix", "fingerprint"}
        self.assertEqual(set(lesson.keys()), expected)

    def test_valid_draft_with_empty_problem_fix_parses(self):
        lesson = _lessons.parse_lesson(VALID_DRAFT)
        self.assertEqual(lesson["status"], "draft")
        self.assertEqual(lesson["problem"], "")
        self.assertEqual(lesson["fix"], "")
        self.assertNotEqual(lesson["symptom"], "")
        self.assertEqual(lesson["advisory"], False)

    def test_draft_all_bodies_empty_parses(self):
        text = VALID_DRAFT.replace(
            "## Symptom\n某工具报错 foo。\n", "## Symptom\n")
        lesson = _lessons.parse_lesson(text)
        self.assertEqual(lesson["status"], "draft")
        self.assertEqual(lesson["problem"], "")
        self.assertEqual(lesson["symptom"], "")
        self.assertEqual(lesson["fix"], "")

    def test_missing_required_field_rejected(self):
        for field in ("id", "status", "strength", "root", "created_at",
                      "updated_at", "category", "severity",
                      "affected_versions", "verified_versions", "source",
                      "advisory"):
            text = VALID_PUBLISHED.replace(
                _line_for(field), "")
            _assert_parse_error(self, text)

    def test_missing_fingerprint_is_optional(self):
        text = VALID_PUBLISHED.replace(
            "fingerprint: " + FP_A + "\n", "")
        lesson = _lessons.parse_lesson(text)
        self.assertIsNone(lesson["fingerprint"])

    def test_unknown_frontmatter_key_rejected(self):
        text = VALID_PUBLISHED.replace(
            "severity: high\n",
            "severity: high\nbogus_key: x\n")
        _assert_parse_error(self, text)

    def test_invalid_severity_rejected(self):
        for bad in ("urgent", "desperate", ""):
            text = VALID_PUBLISHED.replace(
                "severity: high\n", "severity: {0}\n".format(bad))
            _assert_parse_error(self, text)

    def test_invalid_advisory_rejected(self):
        # 注：与 _best_practices 一致，bool 字面量大小写不敏感（True 合法）
        for bad in ("maybe", "1", "yes"):
            text = VALID_PUBLISHED.replace(
                "advisory: true\n", "advisory: {0}\n".format(bad))
            _assert_parse_error(self, text)

    def test_invalid_status_rejected(self):
        for bad in ("archived", "trash", ""):
            text = VALID_PUBLISHED.replace(
                "status: published\n", "status: {0}\n".format(bad))
            _assert_parse_error(self, text)

    def test_invalid_strength_rejected(self):
        for bad in ("0", "-1", "abc", "1.5"):
            text = VALID_PUBLISHED.replace(
                "strength: 3\n", "strength: {0}\n".format(bad))
            _assert_parse_error(self, text)

    def test_invalid_timestamp_rejected(self):
        # 缺少 tz offset / 完全非法
        for bad in ("2026-08-02T09:30:00", "2026/08/02 09:30", "yesterday",
                    "2026-08-02T09:30:00Z-05:00"):
            text = VALID_PUBLISHED.replace(
                'created_at: "2026-08-02T09:30:00+08:00"\n',
                'created_at: "{0}"\n'.format(bad))
            _assert_parse_error(self, text)

    def test_invalid_fingerprint_rejected(self):
        for bad in ("zz" * 32, "abc", "A" * 64):
            text = VALID_PUBLISHED.replace(
                "fingerprint: " + FP_A + "\n",
                "fingerprint: {0}\n".format(bad))
            _assert_parse_error(self, text)

    def test_duplicate_id_within_file_rejected(self):
        text = VALID_PUBLISHED.replace(
            "id: L-20260802-001\n",
            "id: L-20260802-001\nid: L-20260802-002\n")
        _assert_parse_error(self, text)

    def test_duplicate_frontmatter_key_rejected(self):
        text = VALID_PUBLISHED.replace(
            "category: rendering\n",
            "category: rendering\ncategory: lighting\n")
        _assert_parse_error(self, text)

    def test_published_with_empty_problem_rejected(self):
        text = VALID_PUBLISHED.replace(
            "## Problem\n渲染结果全黑，但视口显示正常。\n",
            "## Problem\n")
        _assert_parse_error(self, text)

    def test_published_with_empty_fix_rejected(self):
        text = VALID_PUBLISHED.replace(
            "## Fix\n检查 camera 的 near/far 裁剪设置。\n",
            "## Fix\n")
        _assert_parse_error(self, text)

    def test_published_with_empty_symptom_rejected(self):
        text = VALID_PUBLISHED.replace(
            "## Symptom\n输出 exr 全黑，无报错。\n",
            "## Symptom\n")
        _assert_parse_error(self, text)

    def test_missing_title_rejected(self):
        text = VALID_PUBLISHED.replace("# 渲染输出全黑排查\n", "")
        _assert_parse_error(self, text)

    def test_empty_title_rejected(self):
        text = VALID_PUBLISHED.replace("# 渲染输出全黑排查\n", "#\n")
        _assert_parse_error(self, text)

    def test_missing_section_heading_rejected(self):
        text = VALID_PUBLISHED.replace("## Problem\n", "## ProblemX\n")
        _assert_parse_error(self, text)

    def test_unknown_section_heading_rejected(self):
        text = VALID_PUBLISHED.replace("## Problem\n", "## Workaround\n")
        _assert_parse_error(self, text)

    def test_duplicate_section_heading_rejected(self):
        text = VALID_PUBLISHED.replace(
            "## Problem\n渲染结果全黑，但视口显示正常。\n",
            "## Problem\n渲染结果全黑，但视口显示正常。\n## Problem\nx\n")
        _assert_parse_error(self, text)

    def test_body_outside_sections_rejected(self):
        text = VALID_PUBLISHED.replace(
            "---\n\n# 渲染输出全黑排查",
            "---\n\n# 渲染输出全黑排查\n\n游离正文")
        _assert_parse_error(self, text)

    def test_missing_frontmatter_delimiter_rejected(self):
        text = VALID_PUBLISHED.replace("---\n", "", 1)
        _assert_parse_error(self, text)

    def test_non_string_input_rejected(self):
        _assert_parse_error(self, None)

    def test_no_partial_lesson_never_returned(self):
        # 单个文件任一失败即整体失败（此处由 parse_lesson 抛异常体现）
        with self.assertRaises(LessonsError):
            _lessons.parse_lesson(VALID_PUBLISHED.replace(
                "## Fix\n", ""))


def _line_for(field):
    """返回 VALID_PUBLISHED 中对应 field 的行，用于构造缺字段样本。"""
    for line in VALID_PUBLISHED.splitlines():
        s = line.strip()
        if s.startswith(field + ":"):
            return line + "\n"
    raise AssertionError("no such field: " + field)


# ---------------------------------------------------------------------------
# 1.2 loader
# ---------------------------------------------------------------------------
class LoaderTests(_BaseDirFixture):

    def test_mixed_draft_and_published_load(self):
        root = os.path.join(self.base, "knowledge")
        _write(root, os.path.join("lessons", "a.md"), VALID_PUBLISHED)
        _write(root, os.path.join("lessons", "b.md"), VALID_DRAFT)
        lessons, errors = _lessons.load_root_lessons(root)
        self.assertEqual(errors, {})
        self.assertEqual(len(lessons), 2)
        by_id = {l["id"]: l for l in lessons}
        self.assertEqual(by_id["L-20260802-001"]["status"], "published")
        self.assertEqual(by_id["L-20260802-002"]["status"], "draft")
        self.assertEqual(by_id["L-20260802-002"]["symptom"], "某工具报错 foo。")

    def test_per_file_error_does_not_nuke_root(self):
        root = os.path.join(self.base, "knowledge")
        _write(root, os.path.join("lessons", "good.md"), VALID_PUBLISHED)
        _write(root, os.path.join("lessons", "bad.md"),
               "---\nid: L-20260802-099\nstatus: draft\n---\n# x\n")
        lessons, errors = _lessons.load_root_lessons(root)
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["id"], "L-20260802-001")
        self.assertIn("bad.md", errors)
        self.assertIsInstance(errors["bad.md"], str)

    def test_empty_or_missing_lessons_dir(self):
        root = os.path.join(self.base, "knowledge")
        lessons, errors = _lessons.load_root_lessons(root)
        self.assertEqual((lessons, errors), ([], {}))
        os.makedirs(os.path.join(root, "lessons"))
        self.assertEqual(_lessons.load_root_lessons(root), ([], {}))

    def test_non_md_files_ignored(self):
        root = os.path.join(self.base, "knowledge")
        _write(root, os.path.join("lessons", "notes.txt"), "hello")
        _write(root, os.path.join("lessons", "a.md"), VALID_PUBLISHED)
        lessons, _ = _lessons.load_root_lessons(root)
        self.assertEqual(len(lessons), 1)

    def test_loader_records_file_name(self):
        root = os.path.join(self.base, "knowledge")
        _write(root, os.path.join("lessons", "a.md"), VALID_PUBLISHED)
        lessons, _ = _lessons.load_root_lessons(root)
        self.assertEqual(lessons[0]["file"], "a.md")


# ---------------------------------------------------------------------------
# 1.3 atomic write / save_lesson
# ---------------------------------------------------------------------------
class SaveLessonTests(_BaseDirFixture):

    def _root(self):
        return os.path.join(self.base, "knowledge")

    def _save(self, **overrides):
        fields = {
            "title": "测试问题",
            "category": "api",
            "severity": "medium",
            "affected_versions": "H21",
            "verified_versions": "H21",
            "source": "unit-test",
            "advisory": False,
            "problem": "",
            "symptom": "某工具报错 foo。",
            "fix": "",
        }
        fields.update(overrides)
        return _lessons.save_lesson(self._root(), fields)

    def _freeze_clock(self, fixed_dt):
        """固定 ``_lessons.datetime.now`` 到给定时刻（测试结束自动恢复）。"""
        original = _lessons.datetime
        _lessons.datetime = _FixedClock(fixed_dt)
        self.addCleanup(setattr, _lessons, "datetime", original)

    def test_save_creates_dirs_and_file(self):
        lesson = self._save()
        # fingerprint 由 symptom 计算，恒为 64 位 sha256 hex
        self.assertEqual(len(lesson["fingerprint"]), 64)
        # id 格式 L-YYYYMMDD-NNN
        self.assertRegex(lesson["id"], r"^L-\d{8}-\d{3,}$")
        self.assertEqual(lesson["status"], "draft")
        self.assertEqual(lesson["strength"], 1)
        self.assertEqual(lesson["root"], "personal")
        self.assertRegex(lesson["created_at"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*[+\-]\d{2}:\d{2}$")
        self.assertRegex(lesson["updated_at"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*[+\-]\d{2}:\d{2}$")
        self.assertTrue(os.path.isdir(self._root()))
        self.assertTrue(os.path.isdir(_lessons.lessons_dir(self._root())))
        # 文件存在且可 round-trip
        path = os.path.join(_lessons.lessons_dir(self._root()),
                            lesson["id"] + ".md")
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as handle:
            reparsed = _lessons.parse_lesson(handle.read())
        self.assertEqual(reparsed["id"], lesson["id"])

    def test_save_generates_incrementing_ids(self):
        # 固定时钟：避免跨日时两次 datetime.now() 取值不一致导致 flake
        self._freeze_clock(datetime.datetime(2026, 8, 2, 12, 0, 0))
        first = self._save()
        second = self._save(symptom="另一个完全不同的报错。")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["id"], "L-20260802-001")
        self.assertEqual(second["id"], "L-20260802-002")

    def test_save_collision_avoidance_with_existing_lesson(self):
        # 手工预置一个已占用当天 001 的 lesson → 新 lesson 必须避开；
        # 固定时钟避免跨日 flake
        self._freeze_clock(datetime.datetime(2026, 8, 2, 12, 0, 0))
        root = self._root()
        text = VALID_DRAFT.replace(
            "id: L-20260802-002\n",
            "id: L-20260802-001\n")
        _write(root, os.path.join("lessons", "existing.md"), text)
        lesson = self._save()
        self.assertEqual(lesson["id"], "L-20260802-002")

    def test_save_rejects_unknown_field(self):
        with self.assertRaises(LessonsError) as ctx:
            self._save(bogus="x")
        self.assertEqual(ctx.exception.code, "ls_write_error")

    def test_save_rejects_invalid_severity(self):
        with self.assertRaises(LessonsError) as ctx:
            self._save(severity="urgent")
        self.assertEqual(ctx.exception.code, "ls_write_error")

    def test_save_rejects_empty_symptom(self):
        with self.assertRaises(LessonsError) as ctx:
            self._save(symptom="   ")
        self.assertEqual(ctx.exception.code, "ls_write_error")

    def test_save_without_verified_versions_defaults_unknown(self):
        # 回归：verified_versions 缺省时应存 "unknown"（draft 骨架语义），
        # 而不是报错拒绝
        fields = {
            "title": "测试问题",
            "category": "api",
            "severity": "medium",
            "affected_versions": "H21",
            "source": "unit-test",
            "advisory": False,
            "problem": "",
            "symptom": "未验证版本的症状。",
            "fix": "",
        }
        lesson = _lessons.save_lesson(self._root(), fields)
        self.assertEqual(lesson["verified_versions"], "unknown")
        # 落盘文件 round-trip 后仍是 unknown
        path = os.path.join(_lessons.lessons_dir(self._root()),
                            lesson["id"] + ".md")
        with open(path, "r", encoding="utf-8") as handle:
            reparsed = _lessons.parse_lesson(handle.read())
        self.assertEqual(reparsed["verified_versions"], "unknown")

    def test_save_failure_preserves_old_file(self):
        root = self._root()
        first = self._save()
        path = os.path.join(_lessons.lessons_dir(root), first["id"] + ".md")
        with open(path, "r", encoding="utf-8") as handle:
            before = handle.read()

        real_replace = _lessons.os.replace

        def boom(src, dst):
            raise OSError("simulated replace failure")

        _lessons.os.replace = boom
        try:
            with self.assertRaises(LessonsError) as ctx:
                self._save(symptom="又一个完全不同的报错乙。")
            self.assertEqual(ctx.exception.code, "ls_write_error")
        finally:
            _lessons.os.replace = real_replace

        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)
        # 无残留临时文件
        leftovers = [n for n in os.listdir(_lessons.lessons_dir(root))
                     if n != first["id"] + ".md"]
        self.assertEqual(leftovers, [])

    def test_save_failure_reports_error_not_crash(self):
        real_replace = _lessons.os.replace

        def boom(src, dst):
            raise OSError("simulated replace failure")

        _lessons.os.replace = boom
        try:
            with self.assertRaises(LessonsError) as ctx:
                self._save()
            self.assertEqual(ctx.exception.code, "ls_write_error")
            details = ctx.exception.details
            self.assertIsInstance(details, dict)
            self.assertIn("path", details)
        finally:
            _lessons.os.replace = real_replace


# ---------------------------------------------------------------------------
# 1.4 fingerprint / strength
# ---------------------------------------------------------------------------
class FingerprintTests(unittest.TestCase):

    def test_normalization_case_punctuation_whitespace(self):
        a = _lessons.make_fingerprint("  Foo, bar!!  BAZ? ")
        b = _lessons.make_fingerprint("foo bar baz")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{64}$")

    def test_semantic_difference_changes_fingerprint(self):
        a = _lessons.make_fingerprint("camera near plane too large")
        b = _lessons.make_fingerprint("camera far plane too large")
        self.assertNotEqual(a, b)

    def test_unicode_content_preserved(self):
        a = _lessons.make_fingerprint("渲染全黑。")
        b = _lessons.make_fingerprint("渲染全黑")
        self.assertEqual(a, b)
        c = _lessons.make_fingerprint("渲染白屏。")
        self.assertNotEqual(a, c)

    def test_deterministic_and_sha256_hex(self):
        a = _lessons.make_fingerprint("some error message")
        self.assertEqual(a, _lessons.make_fingerprint("some error message"))
        self.assertEqual(len(a), 64)
        import hashlib
        self.assertEqual(a, hashlib.sha256(b"some error message").hexdigest())

    def test_empty_string_has_stable_fingerprint(self):
        a = _lessons.make_fingerprint("")
        b = _lessons.make_fingerprint("   ")
        self.assertEqual(a, b)

    def test_non_string_rejected(self):
        with self.assertRaises(LessonsError):
            _lessons.make_fingerprint(123)


class StrengthAccumulationTests(_BaseDirFixture):

    def _root(self):
        return os.path.join(self.base, "knowledge")

    def test_same_fingerprint_increments_strength_keeps_content(self):
        fields = {
            "title": "重复问题",
            "category": "api",
            "severity": "medium",
            "affected_versions": "H21",
            "verified_versions": "H21",
            "source": "unit-test",
            "advisory": False,
            "problem": "原始 problem 内容。",
            "symptom": "同样的报错文本。",
            "fix": "",
        }
        first = _lessons.save_lesson(self._root(), dict(fields))
        lesson_dir = _lessons.lessons_dir(self._root())
        self.assertEqual(len(os.listdir(lesson_dir)), 1)

        second = _lessons.save_lesson(self._root(), dict(fields))
        # 不新增文件
        self.assertEqual(len(os.listdir(lesson_dir)), 1)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["strength"], 2)
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])
        # 内容保留（problem 未被覆盖）
        path = os.path.join(lesson_dir, first["id"] + ".md")
        with open(path, "r", encoding="utf-8") as handle:
            reparsed = _lessons.parse_lesson(handle.read())
        self.assertEqual(reparsed["problem"], "原始 problem 内容。")
        self.assertEqual(reparsed["strength"], 2)
        self.assertEqual(reparsed["status"], "draft")
        self.assertEqual(reparsed["id"], first["id"])

    def test_different_fingerprint_creates_new_lesson(self):
        fields = {
            "title": "T",
            "category": "api",
            "severity": "low",
            "affected_versions": "H21",
            "verified_versions": "H21",
            "source": "unit-test",
            "advisory": False,
            "problem": "",
            "symptom": "错误 A。",
            "fix": "",
        }
        _lessons.save_lesson(self._root(), dict(fields))
        fields["symptom"] = "错误 B。"
        _lessons.save_lesson(self._root(), dict(fields))
        self.assertEqual(len(os.listdir(_lessons.lessons_dir(self._root()))), 2)

    def test_bump_strength_preserves_body_lines_matching_metadata_keys(self):
        # 回归：正文中恰好以 strength:/updated_at: 开头的行必须逐字节保留；
        # _bump_strength 只允许改 front matter 内的两行元数据，不得碰正文
        fields = {
            "title": "正文含元数据字样",
            "category": "api",
            "severity": "medium",
            "affected_versions": "H21",
            "verified_versions": "H21",
            "source": "unit-test",
            "advisory": False,
            "problem": "strength: 100 表示采样强度。",
            "symptom": "症状正文。",
            "fix": "updated_at: 2020-01-01 表示上次刷新。",
        }
        first = _lessons.save_lesson(self._root(), dict(fields))
        path = os.path.join(_lessons.lessons_dir(self._root()),
                            first["id"] + ".md")
        with open(path, "r", encoding="utf-8") as handle:
            before = handle.read()

        second = _lessons.save_lesson(self._root(), dict(fields))
        self.assertEqual(second["strength"], 2)
        with open(path, "r", encoding="utf-8") as handle:
            after = handle.read()

        def _body_lines(text):
            lines = text.splitlines()
            close = next(i for i, line in enumerate(lines[1:], 1)
                         if line.strip() == "---")
            return lines[close + 1:]

        # 正文（front matter 闭合 --- 之后）逐字节一致
        self.assertEqual(_body_lines(before), _body_lines(after))
        self.assertIn("strength: 100 表示采样强度。", after)
        self.assertIn("updated_at: 2020-01-01 表示上次刷新。", after)


# ---------------------------------------------------------------------------
# 1.5 inbox
# ---------------------------------------------------------------------------
class InboxTests(_BaseDirFixture):

    def _root(self):
        return os.path.join(self.base, "knowledge")

    def _read_lines(self):
        path = _lessons.inbox_path(self._root())
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()

    def _events(self):
        import json
        events = []
        for line in self._read_lines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # 坏行由实现保留，helper 跳过即可
        return events

    def test_first_event_creates_inbox_file(self):
        self.assertTrue(_lessons.record_error_event(
            self._root(), tool="cook", error_code="cook_error", message="坏了"))
        lines = self._read_lines()
        self.assertEqual(len(lines), 1)
        ev = self._events()[0]
        for key in ("fingerprint", "tool", "error_code", "message",
                    "created_at", "source", "count", "updated_at"):
            self.assertIn(key, ev)
        self.assertEqual(ev["tool"], "cook")
        self.assertEqual(ev["error_code"], "cook_error")
        self.assertEqual(ev["count"], 1)
        self.assertEqual(
            ev["fingerprint"],
            _lessons.make_fingerprint("坏了"))

    def test_dedupe_keeps_one_line_per_fingerprint(self):
        self.assertTrue(_lessons.record_error_event(
            self._root(), "cook", "e1", "同样的错误。"))
        self.assertTrue(_lessons.record_error_event(
            self._root(), "cook", "e1", "同样的错误。"))
        self.assertTrue(_lessons.record_error_event(
            self._root(), "render", "e2", "别的错误。"))
        events = self._events()
        self.assertEqual(len(events), 2)
        by_fp = {e["fingerprint"]: e for e in events}
        first = by_fp[_lessons.make_fingerprint("同样的错误。")]
        self.assertEqual(first["count"], 2)
        self.assertIn("updated_at", first)
        self.assertEqual(first["message"], "同样的错误。")
        # 不同指纹各自成行
        self.assertEqual(by_fp[_lessons.make_fingerprint("别的错误。")]["count"], 1)

    def test_record_returns_true_on_success(self):
        self.assertTrue(_lessons.record_error_event(
            self._root(), "t", "c", "m"))

    def test_oversized_message_skipped_returns_false(self):
        huge = "x" * (_lessons.EVENT_MAX_MESSAGE + 1)
        self.assertFalse(_lessons.record_error_event(
            self._root(), "t", "c", huge))
        self.assertFalse(os.path.exists(_lessons.inbox_path(self._root())))

    def test_write_failure_returns_false_never_raises(self):
        # 把 inbox 目录替换为文件 → 无法创建目录 → 返回 False，不抛异常
        inbox_dir = os.path.join(self._root(), "inbox")
        os.makedirs(inbox_dir, exist_ok=True)
        blocker = os.path.join(inbox_dir, "blocker")
        with open(blocker, "w") as h:
            h.write("x")
        os.remove(blocker)
        os.rmdir(inbox_dir)
        with open(inbox_dir, "w") as h:
            h.write("not a dir")
        result = _lessons.record_error_event(
            self._root(), "t", "c", "message")
        self.assertFalse(result)

    def test_corrupted_line_preserved_never_raises(self):
        _write(self._root(), os.path.join("inbox", "events.jsonl"),
               "not-json\n")
        self.assertTrue(_lessons.record_error_event(
            self._root(), "t", "c", "正常事件。"))
        raw = self._read_lines()
        self.assertIn("not-json", raw)
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "正常事件。")

    def test_corrupt_count_record_does_not_poison_other_events(self):
        # 回归：count 非数值（如 "abc"）的记录是坏记录——原样保留
        # （verbatim），且绝不影响其他记录的事件记录与累加，单条坏记录
        # 不得让整次写入失败或毒化未来事件
        import json
        fp = _lessons.make_fingerprint("正常事件。")
        corrupt_line = json.dumps({
            "fingerprint": fp, "tool": "t", "error_code": "e",
            "message": "正常事件。",
            "created_at": "2026-08-02T09:00:00+08:00",
            "source": "s", "count": "abc",
            "updated_at": "2026-08-02T09:00:00+08:00",
        }, ensure_ascii=False) + "\n"
        valid_line = json.dumps({
            "fingerprint": "d" * 64, "tool": "t", "error_code": "e",
            "message": "别的正常事件。",
            "created_at": "2026-08-02T09:00:00+08:00",
            "source": "s", "count": 1,
            "updated_at": "2026-08-02T09:00:00+08:00",
        }, ensure_ascii=False) + "\n"
        _write(self._root(), os.path.join("inbox", "events.jsonl"),
               corrupt_line + valid_line)

        # 同 fingerprint 的坏 count 记录不得让事件记录整体失败
        self.assertTrue(_lessons.record_error_event(
            self._root(), "cook", "e1", "正常事件。"))
        with open(_lessons.inbox_path(self._root()), "r",
                  encoding="utf-8") as handle:
            full = handle.read()
        self.assertIn(corrupt_line, full)   # 坏行逐字节保留
        self.assertIn(valid_line, full)     # 其他记录不受影响
        by_fp = {e["fingerprint"]: e for e in self._events()}
        self.assertEqual(by_fp[fp]["count"], 1)          # 新事件照常记录
        self.assertEqual(by_fp["d" * 64]["count"], 1)

        # 后续同类事件照常累加（坏记录不毒化未来事件）
        self.assertTrue(_lessons.record_error_event(
            self._root(), "cook", "e1", "正常事件。"))
        by_fp = {e["fingerprint"]: e for e in self._events()}
        self.assertEqual(by_fp[fp]["count"], 2)
        with open(_lessons.inbox_path(self._root()), "r",
                  encoding="utf-8") as handle:
            self.assertIn(corrupt_line, handle.read())

    def test_promote_inbox_to_drafts_works_despite_corrupt_count_record(self):
        # 回归：promote 遇到 count 非法的记录时跳过该记录继续处理其他
        # 记录，不得抛异常或中断整个 promote
        import json
        fp = _lessons.make_fingerprint("可升格的症状。")
        corrupt_line = json.dumps({
            "fingerprint": "c" * 64, "tool": "t", "error_code": "e",
            "message": "坏 count 的事件。",
            "created_at": "2026-08-02T09:00:00+08:00",
            "source": "s", "count": "abc",
            "updated_at": "2026-08-02T09:00:00+08:00",
        }, ensure_ascii=False) + "\n"
        good_line = json.dumps({
            "fingerprint": fp, "tool": "t", "error_code": "e",
            "message": "可升格的症状。",
            "created_at": "2026-08-02T09:00:00+08:00",
            "source": "s", "count": 3,
            "updated_at": "2026-08-02T09:00:00+08:00",
        }, ensure_ascii=False) + "\n"
        _write(self._root(), os.path.join("inbox", "events.jsonl"),
               corrupt_line + good_line)

        created = _lessons.promote_inbox_to_drafts(self._root())
        self.assertEqual(len(created), 1)
        lessons, errors = _lessons.load_root_lessons(self._root())
        self.assertEqual(errors, {})
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["fingerprint"], fp)
        # 坏行仍原样保留在 inbox
        with open(_lessons.inbox_path(self._root()), "r",
                  encoding="utf-8") as handle:
            self.assertIn(corrupt_line, handle.read())

    def test_promote_inbox_to_drafts_skips_corrupt_count_record(self):
        # 只有坏记录（count 非法）→ 不创建 lesson、不抛异常
        import json
        corrupt_line = json.dumps({
            "fingerprint": "c" * 64, "tool": "t", "error_code": "e",
            "message": "坏 count 的事件。",
            "created_at": "2026-08-02T09:00:00+08:00",
            "source": "s", "count": "abc",
            "updated_at": "2026-08-02T09:00:00+08:00",
        }, ensure_ascii=False) + "\n"
        _write(self._root(), os.path.join("inbox", "events.jsonl"),
               corrupt_line)
        created = _lessons.promote_inbox_to_drafts(self._root())
        self.assertEqual(created, [])
        self.assertFalse(os.path.isdir(_lessons.lessons_dir(self._root())))

    def test_promote_inbox_to_drafts_below_threshold(self):
        for _ in range(2):
            self.assertTrue(_lessons.record_error_event(
                self._root(), "cook", "e", "同一症状。"))
        created = _lessons.promote_inbox_to_drafts(self._root())
        self.assertEqual(created, [])
        self.assertFalse(os.path.isdir(_lessons.lessons_dir(self._root())))

    def test_third_event_auto_generates_draft_skeleton(self):
        for _ in range(3):
            self.assertTrue(_lessons.record_error_event(
                self._root(), "cook", "e", "同一症状。"))
        lessons, errors = _lessons.load_root_lessons(self._root())
        self.assertEqual(errors, {})
        self.assertEqual(len(lessons), 1)
        lesson = lessons[0]
        self.assertEqual(lesson["status"], "draft")
        self.assertEqual(lesson["category"], "unclassified")
        self.assertEqual(lesson["severity"], "medium")
        self.assertEqual(lesson["source"], "inbox-auto")
        self.assertEqual(lesson["problem"], "")
        self.assertEqual(lesson["fix"], "")
        self.assertNotEqual(lesson["symptom"], "")
        self.assertEqual(
            lesson["fingerprint"],
            _lessons.make_fingerprint("同一症状。"))

    def test_promote_is_idempotent(self):
        for _ in range(3):
            _lessons.record_error_event(self._root(), "cook", "e", "同一症状。")
        # 第 3 次 record 已自动触发 promote → 已有 1 个骨架
        lessons, _ = _lessons.load_root_lessons(self._root())
        self.assertEqual(len(lessons), 1)
        # 显式再 promote 不产生新 lesson
        second = _lessons.promote_inbox_to_drafts(self._root())
        self.assertEqual(second, [])
        self.assertEqual(len(_lessons.load_root_lessons(self._root())[0]), 1)

    def test_promote_threshold_directly_testable(self):
        # 直接构造 inbox 数据验证 ≥3 规则（不依赖 record 次数）
        import json
        path = _lessons.inbox_path(self._root())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fp = _lessons.make_fingerprint("三次错误。")
        with open(path, "w", encoding="utf-8") as handle:
            for i in range(3):
                handle.write(json.dumps({
                    "fingerprint": fp, "tool": "t", "error_code": "c",
                    "message": "三次错误。", "created_at": "2026-08-02T09:00:00+08:00",
                    "source": "s", "count": 1,
                    "updated_at": "2026-08-02T09:00:00+08:00",
                }) + "\n")
        created = _lessons.promote_inbox_to_drafts(self._root())
        self.assertEqual(len(created), 1)
        lesson = _lessons.load_root_lessons(self._root())[0][0]
        self.assertEqual(lesson["fingerprint"], fp)
        # 3 条同指纹行（非去重状态）也只生成 1 个骨架
        self.assertEqual(len(_lessons.load_root_lessons(self._root())[0]), 1)

    def test_promote_does_not_duplicate_when_lesson_exists(self):
        import json
        fp = _lessons.make_fingerprint("已有 lesson 的症状。")
        # 先直接存一个同指纹 lesson
        _lessons.save_lesson(self._root(), {
            "title": "已有", "category": "unclassified", "severity": "medium",
            "affected_versions": "unknown", "verified_versions": "unknown",
            "source": "inbox-auto", "advisory": False, "problem": "",
            "symptom": "已有 lesson 的症状。", "fix": "",
        })
        path = _lessons.inbox_path(self._root())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "fingerprint": fp, "tool": "t", "error_code": "c",
                "message": "已有 lesson 的症状。",
                "created_at": "2026-08-02T09:00:00+08:00",
                "source": "s", "count": 5,
                "updated_at": "2026-08-02T09:00:00+08:00",
            }) + "\n")
        created = _lessons.promote_inbox_to_drafts(self._root())
        self.assertEqual(created, [])
        lessons, _ = _lessons.load_root_lessons(self._root())
        self.assertEqual(len(lessons), 1)


class NoEmbeddingDependencyScanTests(unittest.TestCase):
    """无嵌入模型不变量（spec「依赖扫描断言」）。

    对 ``_lessons.py`` 及其 import 闭包做源码扫描：禁止 embedding / vector
    store / reranker 相关库名（sentence_transformers / fastembed / chromadb /
    qdrant）出现在源码中，也禁止 import 语句含 embedding / vector / rerank
    关键字。stdlib-only 是契约（与 spec.md 的 token 列表保持一致）。
    """

    FORBIDDEN_LIBS = ("sentence_transformers", "fastembed", "chromadb",
                      "qdrant")
    FORBIDDEN_IMPORT_KEYWORDS = ("embedding", "vector", "rerank")

    def _local_import_names(self, text):
        """解析模块级 import 语句中的顶层模块名（含相对导入符号）。"""
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
                    # 相对导入：mod 段与直接 import 的符号都可能是本地模块
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
        """``_lessons.py`` 及本地 import 闭包的全部源码文本。"""
        sources = []
        seen = set()
        stack = [os.path.join(ROOT, "_lessons.py")]
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

    def test_closure_scan_covers_lessons_and_local_dependencies(self):
        # 冒烟：闭包至少含 _lessons.py 本身与其本地依赖 _common.py
        sources = self._closure_sources()
        self.assertGreaterEqual(len(sources), 2)
        self.assertIn("def parse_lesson", "\n".join(sources))

    def test_closure_includes_best_practices_via_lessons(self):
        """新增代码路径（save_recipe 引擎 + 其自校验依赖的 strict parser）
        必须落在无嵌入扫描闭包内（tasks 4.5）。

        _lessons.py 新增 ``save_recipe`` 与本地导入 ``_best_practices``（容错
        两段式）。本用例显式断言：真实文件存在（闭包解析依据）+ ``def
        save_recipe`` 与 ``def parse_best_practices`` 都出现在闭包合并源码里
        ——保证这两条新代码路径一旦引入 embedding / vector / rerank 依赖，
        既有的 test_no_embedding_vector_library_dependencies 等扫描用例会
        覆盖到它们。
        """
        bp_path = os.path.join(ROOT, "_best_practices.py")
        self.assertTrue(os.path.isfile(bp_path),
                        "本地依赖 _best_practices.py 必须存在")
        sources = self._closure_sources()
        combined = "\n".join(sources)
        self.assertIn("def save_recipe", combined,
                      "save_recipe 引擎源码必须在扫描闭包内")
        self.assertIn("def parse_best_practices", combined,
                      "strict parser 源码必须在扫描闭包内")
        # 两个定义各自来自真实落盘文件（防止闭包因导入写法变化悄悄收缩后，
        # 合并源码仍偶然含字样而误判覆盖）
        with open(os.path.join(ROOT, "_lessons.py"), "r",
                  encoding="utf-8") as handle:
            self.assertIn("def save_recipe", handle.read())
        with open(bp_path, "r", encoding="utf-8") as handle:
            self.assertIn("def parse_best_practices", handle.read())

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
