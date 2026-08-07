"""_lessons 多 root registry 测试（section 2：config.json / 路径校验 / 降级 /
root 白名单 / 可写门禁）。

覆盖（对应 OpenSpec delta）：
- 2.1：config.json 只声明额外 root；personal 自动发现且永不出现在 registry；
  默认 priority 0.5 / writable false；personal 名保留。
- 2.2：path 接受 ${VAR} 占位符 / 相对路径 / 绝对路径三种形式；含 ${ 但非纯
  占位符的混合形式（${VAR}/sub）被拒绝并提示；registry 逐 root 报错、非法
  root 跳过。
- 2.3：占位符未定义 → unconfigured；已定义但目录不可读 → unavailable；
  均不影响 personal；resolve_roots / resolve_root_for_read / resolve_root_for_write。
- 2.4：normalize_root_name 拒绝任意路径，只认注册 root 名或 personal。
- 2.5：writable=false → save_lesson 返回 root_not_writable，零写入。

全部测试使用 TemporaryDirectory + monkeypatch `_lessons._base_dir`。
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

import _lessons  # noqa: E402
from _lessons import LessonsError  # noqa: E402


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

    def _write_config(self, entries):
        with open(os.path.join(self.base, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(entries, handle)

    def _names(self, descriptors):
        return [d["name"] for d in descriptors]

    def _remember_env(self, key):
        """记录 env key 现值并在测试结束时恢复（防污染其他用例）。"""
        old = os.environ.get(key)
        if old is not None:
            self.addCleanup(os.environ.__setitem__, key, old)
        else:
            self.addCleanup(os.environ.pop, key, None)


# ---------------------------------------------------------------------------
# 2.1 / 2.2 基本 registry 解析
# ---------------------------------------------------------------------------
class RegistryConfigTests(_BaseDirFixture):

    def test_no_config_means_personal_only(self):
        roots = _lessons.resolve_roots()
        self.assertEqual(self._names(roots), ["personal"])
        self.assertEqual(roots[0]["state"], "ok")
        self.assertEqual(roots[0]["path"],
                         os.path.join(self.base, "knowledge"))
        self.assertEqual(roots[0]["writable"], True)
        self.assertEqual(roots[0]["priority"], 1.0)

    def test_relative_path_root_resolves_against_base(self):
        team_dir = os.path.join(self.base, "team-shared")
        os.makedirs(team_dir)
        self._write_config([{"name": "teamx", "path": "team-shared"}])
        roots = _lessons.resolve_roots()
        teamx = [d for d in roots if d["name"] == "teamx"][0]
        self.assertEqual(teamx["path"], os.path.abspath(team_dir))
        self.assertEqual(teamx["state"], "ok")
        # 默认值
        self.assertEqual(teamx["priority"], 0.5)
        self.assertEqual(teamx["writable"], False)

    def test_explicit_priority_and_writable(self):
        os.makedirs(os.path.join(self.base, "rw"))
        self._write_config([{"name": "rw", "path": "rw",
                             "priority": 0.9, "writable": True}])
        rw = [d for d in _lessons.resolve_roots() if d["name"] == "rw"][0]
        self.assertEqual(rw["priority"], 0.9)
        self.assertEqual(rw["writable"], True)

    def test_placeholder_undefined_is_unconfigured(self):
        self._remember_env("LESSONS_TEST_UNSET_VAR")
        os.environ.pop("LESSONS_TEST_UNSET_VAR", None)
        self._write_config([{"name": "nope",
                             "path": "${LESSONS_TEST_UNSET_VAR}"}])
        roots = _lessons.resolve_roots()
        nope = [d for d in roots if d["name"] == "nope"][0]
        self.assertEqual(nope["state"], "unconfigured")
        self.assertIsNone(nope["path"])
        # personal 不受影响
        self.assertEqual(roots[0]["name"], "personal")
        self.assertEqual(roots[0]["state"], "ok")

    def test_placeholder_defined_but_missing_dir_is_unavailable(self):
        os.environ["LESSONS_TEST_TEAM"] = os.path.join(self.base, "missing-team")
        self.addCleanup(os.environ.pop, "LESSONS_TEST_TEAM", None)
        self._write_config([{"name": "teamx",
                             "path": "${LESSONS_TEST_TEAM}"}])
        teamx = [d for d in _lessons.resolve_roots()
                 if d["name"] == "teamx"][0]
        self.assertEqual(teamx["state"], "unavailable")
        self.assertEqual(teamx["path"],
                         os.path.abspath(os.path.join(self.base, "missing-team")))
        # personal 不受影响
        self.assertEqual(
            [d for d in _lessons.resolve_roots()
             if d["name"] == "personal"][0]["state"], "ok")

    def test_placeholder_defined_to_existing_dir_is_ok(self):
        env_dir = os.path.join(self.base, "env-team")
        os.makedirs(env_dir)
        os.environ["LESSONS_TEST_TEAM"] = env_dir
        self.addCleanup(os.environ.pop, "LESSONS_TEST_TEAM", None)
        self._write_config([{"name": "teamx",
                             "path": "${LESSONS_TEST_TEAM}"}])
        teamx = [d for d in _lessons.resolve_roots()
                 if d["name"] == "teamx"][0]
        self.assertEqual(teamx["state"], "ok")
        self.assertEqual(teamx["path"], os.path.abspath(env_dir))


class RegistryPathValidationTests(_BaseDirFixture):

    def _assert_skipped_with_error(self, path, message_fragment=None):
        self._write_config([{"name": "badroot", "path": path}])
        roots, errors = _lessons.load_registry()
        self.assertNotIn("badroot", self._names(_lessons.resolve_roots()))
        self.assertIn("badroot", errors)
        if message_fragment:
            self.assertIn(message_fragment, errors["badroot"])

    def _assert_path_accepted(self, path):
        """绝对/相对路径被接受：root 进入 resolve_roots，state ∈ {ok, unavailable}。

        不再被校验器跳过；state 取决于目录是否真实存在（本机路径 mkdir 成功
        → ok，系统级路径通常不可创建 → unavailable，均符合契约）。
        """
        self._write_config([{"name": "badroot", "path": path}])
        _loaded, errors = _lessons.load_registry()
        self.assertNotIn("badroot", errors)
        found = [d for d in _lessons.resolve_roots() if d["name"] == "badroot"]
        self.assertEqual(len(found), 1)
        self.assertIn(found[0]["state"], ("ok", "unavailable"))
        return found[0]

    def test_windows_drive_absolute_accepted(self):
        # 盘符绝对路径（C:\...）现在被接受
        self._assert_path_accepted(r"C:\team\share")

    def test_posix_leading_slash_accepted(self):
        # POSIX 前导 / 现在被接受
        self._assert_path_accepted("/team/share")

    def test_unc_absolute_accepted(self):
        # UNC（\\server\share）现在被接受
        self._assert_path_accepted(r"\\server\share")

    def test_single_backslash_accepted(self):
        # 前导 \ 现在被接受
        self._assert_path_accepted(r"\team")

    def test_mixed_placeholder_with_suffix_rejected(self):
        # 含 ${ 但非纯占位符的混合形式仍被拒绝
        self._assert_skipped_with_error("${TEAM}/sub", "${VAR}")

    def test_absolute_path_resolves_directly(self):
        # 绝对路径直接使用，不与 base 拼接：用一个独立于 base 的绝对路径证明
        with tempfile.TemporaryDirectory() as outside:
            abs_dir = os.path.join(outside, "team_nas")
            os.makedirs(abs_dir)
            self._write_config([{"name": "absroot", "path": abs_dir}])
            desc = [d for d in _lessons.resolve_roots()
                    if d["name"] == "absroot"][0]
            # 目录存在 → ok
            self.assertEqual(desc["state"], "ok")
            # resolved 必须等于原绝对路径的规范化，而非 base 下的子目录
            self.assertEqual(
                desc["path"], os.path.abspath(os.path.normpath(abs_dir)))

    def test_absolute_path_missing_dir_is_unavailable(self):
        # 绝对路径指向不存在的目录 → unavailable，但不影响 personal
        missing = os.path.join(self.base, "no_such_drive_dir")
        self.assertFalse(os.path.exists(missing))
        self._write_config([{"name": "absroot", "path": missing}])
        roots = _lessons.resolve_roots()
        desc = [d for d in roots if d["name"] == "absroot"][0]
        self.assertEqual(desc["state"], "unavailable")
        self.assertEqual(
            desc["path"], os.path.abspath(os.path.normpath(missing)))
        # personal 不受影响
        self.assertEqual(
            [d for d in roots if d["name"] == "personal"][0]["state"], "ok")

    def test_invalid_priority_rejected(self):
        for bad in ("high", 1.5, -0.1):
            self._write_config([{"name": "p", "path": "rel", "priority": bad}])
            roots, errors = _lessons.load_registry()
            self.assertIn("p", errors)
            self.assertNotIn("p", self._names(_lessons.resolve_roots()))

    def test_invalid_writable_rejected(self):
        self._write_config([{"name": "w", "path": "rel", "writable": "yes"}])
        roots, errors = _lessons.load_registry()
        self.assertIn("w", errors)
        self.assertNotIn("w", self._names(_lessons.resolve_roots()))

    def test_missing_name_rejected(self):
        self._write_config([{"path": "rel"}])
        _, errors = _lessons.load_registry()
        self.assertTrue(errors)

    def test_empty_path_rejected(self):
        self._write_config([{"name": "n", "path": "  "}])
        _, errors = _lessons.load_registry()
        self.assertIn("n", errors)

    def test_invalid_name_charset_rejected(self):
        # 回归：name 只接受 [A-Za-z0-9_-]+（与 cache_index_dir 同约束，
        # 防目录穿越），非法 charset 必须逐项报错并跳过
        for bad in ("team x", "team/x", "a.b", "团队", "a..b"):
            self._write_config([{"name": bad, "path": "rel"}])
            _, errors = _lessons.load_registry()
            self.assertIn(bad, errors)
            self.assertNotIn(bad, self._names(_lessons.resolve_roots()))
        # 合法 charset 名正常通过
        self._write_config([{"name": "team_x-2", "path": "rel"}])
        roots, errors = _lessons.load_registry()
        self.assertEqual(errors, {})
        self.assertIn("team_x-2", [r["name"] for r in roots])

    def test_personal_name_is_reserved(self):
        self._write_config([{"name": "personal", "path": "rel"}])
        _, errors = _lessons.load_registry()
        self.assertIn("personal", errors)
        # resolve 结果仍只有自动发现的 personal
        roots = _lessons.resolve_roots()
        self.assertEqual(len([d for d in roots if d["name"] == "personal"]), 1)

    def test_duplicate_name_rejected(self):
        self._write_config([{"name": "dup", "path": "a"},
                            {"name": "dup", "path": "b"}])
        _, errors = _lessons.load_registry()
        self.assertIn("dup", errors)

    def test_non_list_config_rejected(self):
        with open(os.path.join(self.base, "config.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"name": "x"}')
        roots, errors = _lessons.load_registry()
        self.assertEqual(roots, [])
        self.assertIn("_config", errors)
        self.assertEqual(self._names(_lessons.resolve_roots()), ["personal"])

    def test_corrupt_json_config_degrades_to_personal(self):
        with open(os.path.join(self.base, "config.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{not json")
        roots, errors = _lessons.load_registry()
        self.assertEqual(roots, [])
        self.assertIn("_config", errors)
        self.assertEqual(self._names(_lessons.resolve_roots()), ["personal"])


# ---------------------------------------------------------------------------
# 2.3 / 2.4 resolve helpers
# ---------------------------------------------------------------------------
class ResolveHelperTests(_BaseDirFixture):

    def setUp(self):
        super(ResolveHelperTests, self).setUp()
        os.makedirs(os.path.join(self.base, "okteam"))
        self._write_config([
            {"name": "okteam", "path": "okteam", "writable": True},
            {"name": "ro", "path": "ro"},
            {"name": "gone", "path": "missing-dir"},
            {"name": "undef", "path": "${LESSONS_TEST_UNSET_VAR}"},
        ])
        self._remember_env("LESSONS_TEST_UNSET_VAR")
        os.environ.pop("LESSONS_TEST_UNSET_VAR", None)

    def test_resolve_root_for_read_personal_default(self):
        for scope in (None, "", "personal"):
            desc = _lessons.resolve_root_for_read(scope)
            self.assertEqual(desc["name"], "personal")
            self.assertEqual(desc["state"], "ok")

    def test_resolve_root_for_read_ok_team(self):
        desc = _lessons.resolve_root_for_read("okteam")
        self.assertEqual(desc["name"], "okteam")
        self.assertEqual(desc["state"], "ok")
        self.assertEqual(desc["path"],
                         os.path.abspath(os.path.join(self.base, "okteam")))

    def test_resolve_root_for_read_unknown_raises(self):
        for name in ("nope", "ro", "gone", "undef"):
            with self.assertRaises(LessonsError) as ctx:
                _lessons.resolve_root_for_read(name)
            self.assertEqual(ctx.exception.code, "ls_unknown_root")

    def test_resolve_root_for_write_unknown_raises(self):
        with self.assertRaises(LessonsError) as ctx:
            _lessons.resolve_root_for_write("teamx")
        self.assertEqual(ctx.exception.code, "ls_unknown_root")
        # 未 ok 的注册 root 同样 ls_unknown_root
        for name in ("ro", "gone", "undef"):
            with self.assertRaises(LessonsError):
                _lessons.resolve_root_for_write(name)

    def test_resolve_root_for_write_ok_team(self):
        desc = _lessons.resolve_root_for_write("okteam")
        self.assertEqual(desc["name"], "okteam")
        self.assertEqual(desc["writable"], True)

    def test_normalize_root_name_rejects_arbitrary_paths(self):
        for bad in (r"C:\team\share", "/abs/path", r"\\server\share",
                    "team/../escape", "personal/.."):
            with self.assertRaises(LessonsError) as ctx:
                _lessons.normalize_root_name(bad)
            self.assertEqual(ctx.exception.code, "ls_unknown_root")

    def test_normalize_root_name_accepts_personal_and_ok(self):
        self.assertEqual(
            _lessons.normalize_root_name(None)["name"], "personal")
        self.assertEqual(
            _lessons.normalize_root_name("personal")["name"], "personal")
        self.assertEqual(
            _lessons.normalize_root_name("okteam")["name"], "okteam")

    def test_unconfigured_and_unavailable_do_not_break_personal(self):
        # 2.3：占位符未定义 / 目录不可读都必须静默、不影响 personal
        for desc in _lessons.resolve_roots():
            if desc["name"] == "undef":
                self.assertEqual(desc["state"], "unconfigured")
            if desc["name"] == "gone":
                self.assertEqual(desc["state"], "unavailable")
        personal = _lessons.resolve_root_for_read("personal")
        self.assertEqual(personal["path"],
                         os.path.join(self.base, "knowledge"))


# ---------------------------------------------------------------------------
# 2.5 writability gate
# ---------------------------------------------------------------------------
class WritabilityGateTests(_BaseDirFixture):

    def setUp(self):
        super(WritabilityGateTests, self).setUp()
        os.makedirs(os.path.join(self.base, "ro"))
        os.makedirs(os.path.join(self.base, "rw"))
        self._write_config([
            {"name": "ro", "path": "ro", "writable": False},
            {"name": "rw", "path": "rw", "writable": True},
        ])

    def _fields(self, symptom="可写性测试症状。"):
        return {
            "title": "门禁测试",
            "category": "api",
            "severity": "low",
            "affected_versions": "H21",
            "verified_versions": "H21",
            "source": "unit-test",
            "advisory": False,
            "problem": "",
            "symptom": symptom,
            "fix": "",
        }

    def test_writable_false_raises_root_not_writable_zero_writes(self):
        ro_path = os.path.join(self.base, "ro")
        with self.assertRaises(LessonsError) as ctx:
            _lessons.save_lesson(ro_path, self._fields())
        self.assertEqual(ctx.exception.code, "root_not_writable")
        # 零写入：lessons 目录不存在
        self.assertFalse(os.path.exists(
            os.path.join(ro_path, "lessons")))
        self.assertEqual(os.listdir(ro_path), [])

    def test_writable_true_root_saves_with_root_name_stamped(self):
        rw_path = os.path.join(self.base, "rw")
        lesson = _lessons.save_lesson(rw_path, self._fields())
        self.assertEqual(lesson["root"], "rw")
        self.assertEqual(
            os.listdir(os.path.join(rw_path, "lessons")),
            [lesson["id"] + ".md"])

    def test_personal_root_always_writable(self):
        lesson = _lessons.save_lesson(
            os.path.join(self.base, "knowledge"), self._fields())
        self.assertEqual(lesson["root"], "personal")

    def test_unknown_path_not_in_registry_still_writable(self):
        # 不在 registry 的路径（如测试直写）不受门禁约束
        target = os.path.join(self.base, "custom")
        lesson = _lessons.save_lesson(target, self._fields())
        self.assertTrue(os.path.isfile(os.path.join(
            target, "lessons", lesson["id"] + ".md")))


if __name__ == "__main__":
    unittest.main()
