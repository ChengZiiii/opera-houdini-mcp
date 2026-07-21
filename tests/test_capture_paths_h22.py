"""回归测试 Bug 3：resolve_base_dir H22/H21/H20 三态 fallback chain +
caller fallback_base 不重复拼 houdini_mcp。

依据（SideFX 文档 2026-07-21）：
  - hou.text.expandString（H22+ 推荐，新模块 hou.text）：
    https://www.sidefx.com/docs/houdini/hom/hou/text.html#expandString
  - hou.expandString（已 deprecated in H22，原模块 top-level）：
    https://www.sidefx.com/docs/houdini/hom/hou/expandString.html
    "This method is deprecated in favor of hou.text.expandString."
  - H20 缺 hou.text（hou.text 模块 H22 才引入），回退 hou.expandString

测试覆盖：
  - H22 hou.text.expandString 优先（不调 hou.expandString）
  - H21 hou.expandString fallback（无 hou.text 属性）
  - hou.text.expandString 抛异常时降级到 hou.expandString
  - hou=None 兜底 os.environ['TEMP'] or 'TMP' or '/tmp'
  - H20 hou 只有 expandString（无 text）走 hou.expandString
  - default_capture_path / failed_capture_path 在 caller 传 fallback_base
    时不再重复拼 houdini_mcp（caller 已拼好）
  - caller 不传 fallback_base 时行为不变（向后兼容，仍走
    resolve_base_dir 拼一次 houdini_mcp）

本测试纯 stdlib，无 hython 依赖。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_PKG_KEY = "houdinimcp"
_CP_KEY = "houdinimcp._capture_paths"


def _ensure_pkg():
    if _PKG_KEY not in sys.modules:
        import types
        pkg = types.ModuleType(_PKG_KEY)
        pkg.__path__ = [ROOT]
        sys.modules[_PKG_KEY] = pkg


def _load_fresh():
    import importlib.util as _ilu
    if _CP_KEY in sys.modules:
        del sys.modules[_CP_KEY]
    spec = _ilu.spec_from_file_location(_CP_KEY,
                                        os.path.join(ROOT, "_capture_paths.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[_CP_KEY] = mod
    spec.loader.exec_module(mod)
    return mod


class _HouH22:
    """H22 mock：hou.text.expandString 优先；不应调 hou.expandString。"""

    def __init__(self, text_return, expand_calls_ref):
        self._text_return = text_return
        self._expand_calls_ref = expand_calls_ref

        class _Text:
            def __init__(self, return_value, ref):
                self._return = return_value
                self._ref = ref

            def expandString(self, s):
                self._ref["text_called"] = self._ref.get("text_called", 0) + 1
                self._ref["text_arg"] = s
                return self._return

        self.text = _Text(text_return, expand_calls_ref)

    def expandString(self, s):
        self._expand_calls_ref["expand_called"] = (
            self._expand_calls_ref.get("expand_called", 0) + 1)
        self._expand_calls_ref["expand_arg"] = s
        return "/should/not/be/used"


class _HouH22ThrowingText:
    """H22 mock：hou.text.expandString 抛异常，hou.expandString 兜底。"""

    def __init__(self, expand_return, ref):
        self._expand_return = expand_return
        self._ref = ref

        class _Text:
            def __init__(self, ref):
                self._ref = ref

            def expandString(self, s):
                self._ref["text_called"] = self._ref.get("text_called", 0) + 1
                raise RuntimeError("hou.text.expandString broken")

        self.text = _Text(ref)

    def expandString(self, s):
        self._ref["expand_called"] = self._ref.get("expand_called", 0) + 1
        self._ref["expand_arg"] = s
        return self._expand_return


class _HouH21Only:
    """H21 mock：仅有 hou.expandString，无 hou.text 属性。"""

    def __init__(self, expand_return, ref):
        self._expand_return = expand_return
        self._ref = ref

    def expandString(self, s):
        self._ref["expand_called"] = self._ref.get("expand_called", 0) + 1
        self._ref["expand_arg"] = s
        return self._expand_return


class _HouH20Only:
    """H20 mock：与 H21 一致（仅有 hou.expandString，无 hou.text）。"""

    def __init__(self, expand_return, ref):
        self._expand_return = expand_return
        self._ref = ref

    def expandString(self, s):
        self._ref["expand_called"] = self._ref.get("expand_called", 0) + 1
        self._ref["expand_arg"] = s
        return self._expand_return


class ResolveBaseDirH22ChainTest(unittest.TestCase):
    """resolve_base_dir H22/H21/H20 三态 fallback chain。"""

    @classmethod
    def setUpClass(cls):
        _ensure_pkg()
        cls.cp = _load_fresh()

    def test_h22_hou_text_expand_string_priority(self):
        """H22：hou.text.expandString 优先；不调 hou.expandString。"""
        ref = {}
        hou = _HouH22("/custom/h22/temp", ref)
        base = self.cp.resolve_base_dir(hou=hou)
        self.assertEqual(base, os.path.join("/custom/h22/temp", "houdini_mcp"))
        # hou.text.expandString 必须被调
        self.assertEqual(ref.get("text_called"), 1,
            "H22 必须调 hou.text.expandString 1 次")
        self.assertEqual(ref.get("text_arg"), "$TEMP")
        # hou.expandString 不应被调（H22 优先 hou.text）
        self.assertIsNone(ref.get("expand_called"),
            "H22 优先 hou.text.expandString；不应回退到 hou.expandString")

    def test_h21_hou_without_text_falls_back_to_expand_string(self):
        """H21：hou 无 text 属性；走 hou.expandString。"""
        ref = {}
        hou = _HouH21Only("/h21/temp", ref)
        base = self.cp.resolve_base_dir(hou=hou)
        self.assertEqual(base, os.path.join("/h21/temp", "houdini_mcp"))
        self.assertEqual(ref.get("expand_called"), 1)
        self.assertEqual(ref.get("expand_arg"), "$TEMP")

    def test_h22_hou_text_throws_falls_back_to_expand_string(self):
        """H22 hou.text 抛异常 → 降级 hou.expandString（仍在 hou 上）。"""
        ref = {}
        hou = _HouH22ThrowingText("/h21/temp", ref)
        base = self.cp.resolve_base_dir(hou=hou)
        self.assertEqual(base, os.path.join("/h21/temp", "houdini_mcp"))
        self.assertEqual(ref.get("text_called"), 1)
        self.assertEqual(ref.get("expand_called"), 1)
        self.assertEqual(ref.get("expand_arg"), "$TEMP")

    def test_no_hou_uses_environment_variable(self):
        """hou=None → 兜底 os.environ['TEMP'] or 'TMP' or '/tmp' + houdini_mcp。"""
        base = self.cp.resolve_base_dir(hou=None)
        expected = os.path.join(
            os.environ.get("TEMP")
            or os.environ.get("TMP")
            or "/tmp",
            "houdini_mcp",
        )
        self.assertEqual(base, expected)

    def test_h20_fallback_via_expand_string(self):
        """H20：hou 只有 expandString（无 text 属性）；走 hou.expandString。"""
        ref = {}
        hou = _HouH20Only("/h20/temp", ref)
        base = self.cp.resolve_base_dir(hou=hou)
        self.assertEqual(base, os.path.join("/h20/temp", "houdini_mcp"))
        self.assertEqual(ref.get("expand_called"), 1)
        self.assertEqual(ref.get("expand_arg"), "$TEMP")

    def test_h22_hou_text_returns_empty_string_falls_back(self):
        """H22 hou.text.expandString 返空字符串 → 视为失败，降级 hou.expandString。

        hou.text.expandString 在某些 hou 状态可能返 ''（如 $TEMP 未设置），
        当下应降级到 hou.expandString（仍在 hou 上）。
        """
        class _Hou:
            class text:
                @staticmethod
                def expandString(s):
                    return ""

            @staticmethod
            def expandString(s):
                return "/h21/from/expand"

        base = self.cp.resolve_base_dir(hou=_Hou())
        self.assertEqual(base, os.path.join("/h21/from/expand", "houdini_mcp"))


class CallerFallbackBaseNoDoubleHoudiniMcpTest(unittest.TestCase):
    """caller 传 fallback_base 时不重复拼 houdini_mcp。

    修复前 bug：
      resolve_base_dir(hou=hou, fallback=fallback_base) 总是拼 houdini_mcp，
      导致 caller 已拼 /some/path/houdini_mcp 时实际生成
      /some/path/houdini_mcp/houdini_mcp/<date>/...。

    修复后：
      caller 传 fallback_base → 信任 caller，base 直接用 fallback_base。
      caller 不传 fallback_base → 走 resolve_base_dir 拼 houdini_mcp（向后兼容）。
    """

    @classmethod
    def setUpClass(cls):
        _ensure_pkg()
        cls.cp = _load_fresh()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_capture_path_with_fallback_base_no_double_houdini_mcp(self):
        """caller 传 fallback_base='/some/path/houdini_mcp'（已拼好）→ 不重复拼。"""
        now = time.mktime(time.strptime("2026-07-21 14:30:45",
                                        "%Y-%m-%d %H:%M:%S"))
        # caller 已拼好 houdini_mcp 子目录
        caller_base = os.path.join(self.tmp, "houdini_mcp")
        path = self.cp.default_capture_path(
            hou=None, pane_type="SceneViewer", engine="flipbook",
            scene_basename="test.hip", frame=5, now=now,
            fallback_base=caller_base,
        )
        # 路径不能含 "houdini_mcp/houdini_mcp" 重复
        norm = os.path.normpath(path)
        self.assertNotIn(os.sep + "houdini_mcp" + os.sep + "houdini_mcp" + os.sep,
                         norm,
                         "路径不应重复拼 houdini_mcp：实际 {0}".format(norm))
        # 但 caller_base 路径前缀必须出现 1 次
        self.assertTrue(norm.startswith(os.path.normpath(caller_base)),
                        "路径必须以 caller_base 开头：实际 {0}".format(norm))
        # 日期子目录必须出现
        self.assertIn(os.sep + "2026-07-21" + os.sep, norm)

    def test_default_capture_path_without_fallback_base_keeps_old_behavior(self):
        """caller 不传 fallback_base 时仍走 resolve_base_dir 拼一次 houdini_mcp。

        向后兼容：hou 有 expandString 时，行为与 Bug 1 重构后一致。
        """
        now = time.mktime(time.strptime("2026-07-21 14:30:45",
                                        "%Y-%m-%d %H:%M:%S"))
        # caller 不传 fallback_base；hou 有 expandString
        # 注意：闭包捕获 self.tmp，不能在 mock 内部用 self.tmp
        tmp = self.tmp

        class _Hou:
            def expandString(self, s):
                return tmp if s == "$TEMP" else s
        hou = _Hou()
        path = self.cp.default_capture_path(
            hou=hou, pane_type="SceneViewer", engine="flipbook",
            scene_basename="test.hip", frame=5, now=now,
        )
        norm = os.path.normpath(path)
        # 路径应含 "<tmp>/houdini_mcp/<date>/..."（resolve_base_dir 拼 1 次）
        self.assertTrue(norm.startswith(os.path.normpath(
            os.path.join(self.tmp, "houdini_mcp"))),
            "向后兼容：路径应含 <tmp>/houdini_mcp 前缀，实际 {0}".format(norm))
        # 不应出现重复的 houdini_mcp/houdini_mcp
        self.assertNotIn(os.sep + "houdini_mcp" + os.sep + "houdini_mcp" + os.sep,
                         norm)

    def test_default_capture_path_hou_none_no_fallback_base_keeps_old_behavior(self):
        """hou=None 且 caller 不传 fallback_base 时仍走 resolve_base_dir 兜底。

        hou=None 时 resolve_base_dir 用 os.environ；行为不变。
        """
        now = time.mktime(time.strptime("2026-07-21 14:30:45",
                                        "%Y-%m-%d %H:%M:%S"))
        path = self.cp.default_capture_path(
            hou=None, pane_type="SceneViewer", engine="flipbook",
            scene_basename="s", frame=1, now=now,
        )
        norm = os.path.normpath(path)
        # 路径应含 houdini_mcp/<date>/... 一次
        env_base = (os.environ.get("TEMP")
                    or os.environ.get("TMP")
                    or "/tmp")
        expected_prefix = os.path.normpath(os.path.join(env_base, "houdini_mcp"))
        self.assertTrue(norm.startswith(expected_prefix),
            "hou=None 兜底：路径应含 <TEMP>/houdini_mcp 前缀，实际 {0}".format(norm))

    def test_failed_capture_path_with_fallback_base_no_double_houdini_mcp(self):
        """failed_capture_path 同修复：caller 传 fallback_base 时不重复拼。"""
        now = time.mktime(time.strptime("2026-07-21 14:30:45",
                                        "%Y-%m-%d %H:%M:%S"))
        caller_base = os.path.join(self.tmp, "houdini_mcp")
        path = self.cp.failed_capture_path(
            hou=None, pane_type="SceneViewer", engine="flipbook",
            scene_basename="test.hip", frame=5, now=now,
            fallback_base=caller_base,
        )
        norm = os.path.normpath(path)
        self.assertNotIn(os.sep + "houdini_mcp" + os.sep + "houdini_mcp" + os.sep,
                         norm,
                         "failed 路径不应重复拼 houdini_mcp：实际 {0}".format(norm))
        self.assertTrue(norm.startswith(os.path.normpath(caller_base)),
                        "failed 路径必须以 caller_base 开头：实际 {0}".format(norm))
        # 必须含 failed/ 子目录
        self.assertIn(os.sep + "failed" + os.sep, norm)
        # 必须以 _error.png 结尾
        self.assertTrue(norm.endswith("_error.png"))

    def test_failed_capture_path_without_fallback_base_keeps_old_behavior(self):
        """failed_capture_path 不传 fallback_base 时仍走 resolve_base_dir 拼一次。"""
        now = time.mktime(time.strptime("2026-07-21 14:30:45",
                                        "%Y-%m-%d %H:%M:%S"))
        # 闭包捕获 self.tmp，避免在 mock 内部用 self.tmp（指向 mock 实例）
        tmp = self.tmp

        class _Hou:
            def expandString(self, s):
                return tmp if s == "$TEMP" else s
        hou = _Hou()
        path = self.cp.failed_capture_path(
            hou=hou, pane_type="SceneViewer", engine="flipbook",
            scene_basename="s", frame=1, now=now,
        )
        norm = os.path.normpath(path)
        self.assertTrue(norm.startswith(os.path.normpath(
            os.path.join(self.tmp, "houdini_mcp"))),
            "向后兼容：failed 路径应含 <tmp>/houdini_mcp 前缀，实际 {0}".format(norm))
        self.assertNotIn(os.sep + "houdini_mcp" + os.sep + "houdini_mcp" + os.sep,
                         norm)


if __name__ == "__main__":
    unittest.main()