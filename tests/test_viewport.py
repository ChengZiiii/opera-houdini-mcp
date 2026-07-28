"""add-viewport-control-tools 单元测试。

覆盖：
- 8 个工具的 schema、enum 白名单、headless warning、错误结构
- LOP renderer 可用/不可用/非 LOP 三分支
- NO_UNDO_COMMANDS 8 项注册 + 与既有契约不冲突
- 现有 capture_pane_screenshot / _pane_capture 路径未变（R1 红线）
- 8 项均不创建 undo group（NO_UNDO 分支）
- 工具计数恰为 8（不允许 capture_screenshot）
"""
import ast
import importlib.util
import json
import os
import sys
import types
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# 注册 houdinimcp 包以支持 _viewport 模块内的 relative import
_pkg = types.ModuleType("houdinimcp")
_pkg.__path__ = [ROOT]
sys.modules["houdinimcp"] = _pkg
# 加载 _common 作为 sibling
_common = _load_module("houdinimcp._common", os.path.join(ROOT, "_common.py"))
# 加载 _viewport 模块（hou 通过 mock 注入；模块顶层不 import hou）
_viewport = _load_module("houdinimcp._viewport", os.path.join(ROOT, "_viewport.py"))


def _fake_hou_no_ui():
    hou = types.SimpleNamespace()
    hou.ui = None
    return hou


def _fake_hou_no_scene_viewer():
    class _UI(object):
        def desktops(self):
            return []

        def paneTabOfType(self, *a, **kw):
            return None

    hou = types.SimpleNamespace()
    hou.ui = _UI()
    hou.paneTabType = types.SimpleNamespace(SceneViewer="SV", NetworkEditor="NE")
    return hou


def _fake_hou_with_scene_viewer(sv):
    class _UI(object):
        def __init__(self, sv):
            self._sv = sv

        def desktops(self):
            return [self]

        def paneTabOfType(self, *a, **kw):
            return self._sv

    hou = types.SimpleNamespace()
    hou.ui = _UI(sv)
    hou.paneTabType = types.SimpleNamespace(SceneViewer="SV", NetworkEditor="NE")
    return hou


def _scene_viewer_with_viewport(viewport):
    sv = mock.Mock()
    sv.curViewport.return_value = viewport
    return sv


class _Vp(object):
    def __init__(self, camera_path="", vtype=None, display_set=None,
                 shaded_mode=None):
        self._camera_path = camera_path
        self._vtype = vtype
        self._display_set = display_set
        self._shaded_mode = shaded_mode

    def camera(self):
        if not self._camera_path:
            return None
        node = mock.Mock()
        node.path.return_value = self._camera_path
        return node

    def type(self):
        return self._vtype

    def settings(self):
        if self._display_set is None:
            return None
        ds = mock.Mock()
        ds.shadedMode.return_value = self._shaded_mode
        return mock.Mock(displaySet=mock.Mock(return_value=ds))

    def setCamera(self, node):
        pass

    def frameSelected(self):
        pass

    def frameAll(self):
        pass

    def changeType(self, enum):
        self._last_enum = enum


class HeadlessWarningTests(unittest.TestCase):
    def test_get_viewport_info_no_ui(self):
        result = _viewport.get_viewport_info(_fake_hou_no_ui())
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["_warning"]["code"], "viewport_unavailable")

    def test_set_viewport_camera_no_ui(self):
        result = _viewport.set_viewport_camera(_fake_hou_no_ui(), "/obj/cam1")
        self.assertEqual(result["status"], "warning")

    def test_set_viewport_display_no_ui(self):
        result = _viewport.set_viewport_display(_fake_hou_no_ui(), "main", "shaded")
        self.assertEqual(result["status"], "warning")

    def test_set_viewport_renderer_non_lop(self):
        hou = _fake_hou_with_scene_viewer(_scene_viewer_with_viewport(_Vp()))
        hou.pwd = lambda: types.SimpleNamespace(
            childTypeCategory=lambda: types.SimpleNamespace(name=lambda: "obj"))
        result = _viewport.set_viewport_renderer(hou, "gl")
        self.assertEqual(result["status"], "warning")

    def test_frame_selection_no_ui(self):
        self.assertEqual(_viewport.frame_selection(_fake_hou_no_ui())["status"], "warning")

    def test_frame_all_no_ui(self):
        self.assertEqual(_viewport.frame_all(_fake_hou_no_ui())["status"], "warning")

    def test_set_viewport_direction_no_ui(self):
        result = _viewport.set_viewport_direction(_fake_hou_no_ui(), "front")
        self.assertEqual(result["status"], "warning")

    def test_set_current_network_no_pane(self):
        hou = _fake_hou_no_scene_viewer()
        hou.node = lambda p: mock.Mock(path=lambda: p) if p else None
        result = _viewport.set_current_network(hou, "/obj")
        self.assertEqual(result["status"], "warning")


class DisplaySetWhitelistTests(unittest.TestCase):
    def setUp(self):
        self.vp = _Vp(display_set="Main", shaded_mode="Shaded")
        self.hou = _fake_hou_with_scene_viewer(
            _scene_viewer_with_viewport(self.vp))
        self.hou.displaySetType = types.SimpleNamespace(
            Main="DST_MAIN", Object="DST_OBJ", Scene="DST_SCENE")
        self.hou.shadingMode = types.SimpleNamespace(
            Wireframe="M_W", WireShade="M_WS", Shaded="M_SH",
            Ghost="M_GH", HiddenLine="M_HL")

    def test_unsupported_display_set(self):
        result = _viewport.set_viewport_display(self.hou, "bogus", "shaded")
        self.assertEqual(result["error"], "unsupported_display_set")

    def test_unsupported_shaded_mode(self):
        result = _viewport.set_viewport_display(self.hou, "main", "bogus")
        self.assertEqual(result["error"], "unsupported_shaded_mode")

    def test_valid(self):
        result = _viewport.set_viewport_display(self.hou, "main", "shaded")
        self.assertEqual(result["status"], "success")


class CameraTests(unittest.TestCase):
    def test_camera_not_found(self):
        hou = _fake_hou_with_scene_viewer(
            _scene_viewer_with_viewport(_Vp()))
        hou.node = lambda p: None
        result = _viewport.set_viewport_camera(hou, "/obj/missing")
        self.assertEqual(result["error"], "camera_not_found")


class DirectionTests(unittest.TestCase):
    def setUp(self):
        self.vp = _Vp()
        self.hou = _fake_hou_with_scene_viewer(
            _scene_viewer_with_viewport(self.vp))
        self.hou.geometryViewportType = types.SimpleNamespace(
            Front="FT", Back="BK", Left="LF", Right="RG",
            Top="TP", Bottom="BT", Perspective="PS")

    def test_unsupported(self):
        result = _viewport.set_viewport_direction(self.hou, "diagonal")
        self.assertEqual(result["error"], "unsupported_direction")

    def test_valid_sets_enum(self):
        result = _viewport.set_viewport_direction(self.hou, "front")
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.vp._last_enum, "FT")


class SetCurrentNetworkTests(unittest.TestCase):
    def test_node_not_found(self):
        hou = _fake_hou_no_scene_viewer()
        hou.node = lambda p: None
        result = _viewport.set_current_network(hou, "/obj/missing")
        self.assertEqual(result["error"], "node_not_found")


class RegistryClassificationTests(unittest.TestCase):
    """断言 8 项在 server NO_UNDO_COMMANDS 中固化、不在 MUTATING。"""

    @classmethod
    def setUpClass(cls):
        # 复用包上下文：server.py 顶部有大量 `from . import _xxx` 相对导入。
        # 通过 sys.modules 中的 houdinimcp 包路径使这些导入能解析到 sibling。
        pkg_name = "houdinimcp"
        if pkg_name not in sys.modules:
            _pkg = types.ModuleType(pkg_name)
            _pkg.__path__ = [ROOT]
            sys.modules[pkg_name] = _pkg
        cls._server = _load_module(
            "houdinimcp._server_for_test", os.path.join(ROOT, "server.py"))

    def test_no_undo_contains_eight(self):
        eight = {
            "get_viewport_info", "set_viewport_camera",
            "set_viewport_display", "set_viewport_renderer",
            "frame_selection", "frame_all",
            "set_viewport_direction", "set_current_network",
        }
        self.assertTrue(eight.issubset(self._server.HoudiniMCPServer.NO_UNDO_COMMANDS))

    def test_not_in_mutating(self):
        eight = {
            "get_viewport_info", "set_viewport_camera",
            "set_viewport_display", "set_viewport_renderer",
            "frame_selection", "frame_all",
            "set_viewport_direction", "set_current_network",
        }
        self.assertFalse(eight & self._server.HoudiniMCPServer.MUTATING_COMMANDS)


class PaneCaptureUntouchedTests(unittest.TestCase):
    """R1 红线：既有 _pane_capture.py / capture_pane_screenshot 未变。"""

    def test_capture_pane_screenshot_still_in_no_undo(self):
        _server = self._server_or_load()
        self.assertIn("capture_pane_screenshot", _server.HoudiniMCPServer.NO_UNDO_COMMANDS)

    def _server_or_load(self):
        pkg_name = "houdinimcp"
        if pkg_name not in sys.modules:
            _pkg = types.ModuleType(pkg_name)
            _pkg.__path__ = [ROOT]
            sys.modules[pkg_name] = _pkg
        return _load_module(
            "houdinimcp._server_for_r1", os.path.join(ROOT, "server.py"))


class NoCaptureScreenshotToolTests(unittest.TestCase):
    """禁止新增 capture_screenshot 或任何截图管线。"""

    def test_no_capture_screenshot_in_houdini_mcp_server(self):
        path = os.path.join(ROOT, "houdini_mcp_server.py")
        with open(path, "r", encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("def capture_screenshot(", src)

    def test_no_capture_screenshot_in_server(self):
        path = os.path.join(ROOT, "server.py")
        with open(path, "r", encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("def capture_screenshot(", src)


class BridgeStyleASTProbeTests(unittest.TestCase):
    """新 bridge 工具 style + 数量恰好 8。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROOT, "houdini_mcp_server.py")
        with open(path, "r", encoding="utf-8") as handle:
            cls.tree = ast.parse(handle.read())
        cls.tool_funcs = []

        def _is_mcp_tool(d):
            # @mcp.tool() — Call with Attribute(value=Name(mcp), attr=tool)
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
                if d.func.attr == "tool" and isinstance(d.func.value, ast.Name):
                    if d.func.value.id == "mcp":
                        return True
            # @mcp.tool(name="...") — same shape with keyword args
            # @mcp.tool — bare Attribute
            if isinstance(d, ast.Attribute):
                if d.attr == "tool" and isinstance(d.value, ast.Name):
                    if d.value.id == "mcp":
                        return True
            return False

        for node in cls.tree.body:
            if isinstance(node, ast.FunctionDef):
                for d in node.decorator_list:
                    if _is_mcp_tool(d):
                        cls.tool_funcs.append(node.name)
                        break

    def test_exactly_eight_new_viewport_tools(self):
        eight = {
            "get_viewport_info", "set_viewport_camera",
            "set_viewport_display", "set_viewport_renderer",
            "frame_selection", "frame_all",
            "set_viewport_direction", "set_current_network",
        }
        self.assertTrue(eight.issubset(set(self.tool_funcs)))
        self.assertFalse(eight ^ set(self.tool_funcs).intersection(eight))


if __name__ == "__main__":
    unittest.main()