"""SceneViewer 多视图 flipbook 工具的定向回归测试。"""
import ast
import importlib.util as _ilu
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG_KEY = "houdinimcp"
PCP_KEY = "houdinimcp._pane_capture"


def _load_pane_capture_fresh():
    if PKG_KEY not in sys.modules:
        pkg = types.ModuleType(PKG_KEY)
        pkg.__path__ = [ROOT]
        sys.modules[PKG_KEY] = pkg
    if "houdinimcp._common" not in sys.modules:
        spec = _ilu.spec_from_file_location(
            "houdinimcp._common", os.path.join(ROOT, "_common.py"))
        common = _ilu.module_from_spec(spec)
        sys.modules["houdinimcp._common"] = common
        spec.loader.exec_module(common)
    sys.modules.pop(PCP_KEY, None)
    package = sys.modules.get(PKG_KEY)
    if package is not None:
        package.__dict__.pop("_pane_capture", None)
    spec = _ilu.spec_from_file_location(
        PCP_KEY, os.path.join(ROOT, "_pane_capture.py"))
    module = _ilu.module_from_spec(spec)
    sys.modules[PCP_KEY] = module
    spec.loader.exec_module(module)
    return module


def _png_bytes(width=320, height=180):
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13)
            + b"IHDR" + struct.pack(">II", width, height)
            + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00")


class _Settings(object):
    def __init__(self):
        self.values = {}
        self.calls = []

    def stash(self):
        return self

    def _set(self, name, value):
        self.values[name] = value
        self.calls.append((name, value))

    def beautyPassOnly(self, value):
        self._set("beautyPassOnly", value)

    def frameRange(self, value):
        self._set("frameRange", value)

    def frameIncrement(self, value):
        self._set("frameIncrement", value)

    def outputToMPlay(self, value):
        self._set("outputToMPlay", value)

    def output(self, value):
        self._set("output", value)

    def __getattr__(self, name):
        if name in self.values:
            return self.values[name]
        raise AttributeError(name)


class _CameraSettings(object):
    def stash(self):
        return "stashed-camera"


class _Viewport(object):
    def __init__(self, initial_type="Perspective", camera_path="/obj/cam0",
                 locked=True, fail_restore=False):
        self._type = initial_type
        self._camera_path = camera_path
        self._locked = locked
        self.fail_restore = fail_restore
        self.change_calls = []
        self.home_calls = []
        self.set_camera_calls = []
        self.default_camera_calls = []

    def type(self):
        return self._type

    def changeType(self, value):
        self.change_calls.append(value)
        if self.fail_restore and value == "Perspective":
            raise RuntimeError("restore viewport type failed")
        self._type = value
        self._camera_path = ""

    def homeAll(self):
        self.home_calls.append("homeAll")

    def home(self):
        self.home_calls.append("home")

    def cameraPath(self):
        return self._camera_path

    def defaultCamera(self):
        return _CameraSettings()

    def setDefaultCamera(self, value):
        self.default_camera_calls.append(value)
        if self.fail_restore:
            raise RuntimeError("restore default camera failed")
        self._camera_path = ""

    def useDefaultCamera(self):
        self._camera_path = ""

    def setCamera(self, node):
        self.set_camera_calls.append(node)
        if self.fail_restore:
            raise RuntimeError("restore camera failed")
        self._camera_path = node.path()

    def camera(self):
        return object()

    def isCameraLockedToView(self):
        return self._locked

    def lockCameraToView(self, value):
        self._locked = bool(value)


class _Pane(object):
    def __init__(self, name, viewport=None, fail_views=None,
                 output_dimensions=(320, 180)):
        self._name = name
        self._viewport = viewport or _Viewport()
        self._settings = _Settings()
        self.fail_views = set(fail_views or [])
        self.output_dimensions = output_dimensions
        self.flipbook_calls = []
        self.qt_grab_calls = 0

    def name(self):
        return self._name

    def type(self):
        return "SceneViewer"

    def curViewport(self):
        return self._viewport

    def flipbookSettings(self):
        return self._settings

    def qtWidget(self):
        self.qt_grab_calls += 1
        raise AssertionError("SceneViewer 不得调用 Qt widget")

    def flipbook(self, viewport, settings):
        self.flipbook_calls.append({
            "view_type": viewport.type(),
            "settings": settings,
        })
        names = {
            "Top": "top",
            "Front": "front",
            "Right": "right",
            "Perspective": "perspective",
        }
        view_name = names.get(viewport.type(), viewport.type())
        if view_name in self.fail_views:
            raise RuntimeError("simulated " + view_name + " flipbook failure")
        output = settings.values["output"]
        frame = settings.values["frameRange"][0]
        actual = output.replace("$F4", str(int(frame)).zfill(4))
        with open(actual, "wb") as handle:
            handle.write(_png_bytes(*self.output_dimensions))


class _OtherPane(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name

    def type(self):
        return "NetworkEditor"


class _Desktop(object):
    def __init__(self, name, panes, current=None, ui=None):
        self._name = name
        self._panes = list(panes)
        self._current = current if current is not None else (
            self._panes[0] if self._panes else None)
        self._ui = ui
        self.set_current_calls = 0

    def name(self):
        return self._name

    def paneTabs(self):
        return list(self._panes)

    def currentPaneTab(self):
        return self._current

    def findPaneTab(self, name):
        for pane in self._panes:
            if pane.name() == name:
                return pane
        return None

    def setAsCurrent(self):
        self.set_current_calls += 1
        if self._ui is not None:
            self._ui._current = self


class _UI(object):
    def __init__(self, current, desktops):
        self._current = current
        self._desktops = list(desktops)

    def curDesktop(self):
        return self._current

    def desktops(self):
        return list(self._desktops)

    def paneTabOfType(self, _pane_type):
        current = self._current.currentPaneTab()
        return current


class _Hou(object):
    def __init__(self, current_desktop, desktops, frame=1):
        self.paneTabType = types.SimpleNamespace(SceneViewer="SceneViewer")
        self.geometryViewportType = types.SimpleNamespace(
            Top="Top", Front="Front", Right="Right", Perspective="Perspective")
        self.ui = _UI(current_desktop, desktops)
        self._frame = frame
        self.hipFile = types.SimpleNamespace(basename=lambda: "box.hip")

    def frame(self):
        return self._frame

    def node(self, path):
        return types.SimpleNamespace(path=lambda: path)


def _make_desktops(pane, current_is_scene=False):
    other = _OtherPane("network")
    current = _Desktop("Build", [pane, other] if current_is_scene else [other, pane],
                       current=pane if current_is_scene else other)
    second_pane = _Pane("target")
    other_desktop = _Desktop("Other", [second_pane], current=second_pane)
    ui = _UI(current, [current, other_desktop])
    current._ui = ui
    other_desktop._ui = ui
    return current, other_desktop, ui


class SceneViewerFlipbookViewsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pcp = _load_pane_capture_fresh()
        cls._orig_qt = cls.pcp._QT_BACKEND
        cls.pcp._QT_BACKEND = None

    @classmethod
    def tearDownClass(cls):
        cls.pcp._QT_BACKEND = cls._orig_qt

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _default_hou(self, pane=None, current_is_scene=False):
        pane = pane or _Pane("scene")
        current, other, ui = _make_desktops(pane, current_is_scene)
        return _Hou(current, [current, other]), pane, current, other

    def test_default_views_are_top_front_right_and_valid(self):
        hou, pane, _current, _other = self._default_hou(
            pane=_Pane("scene"), current_is_scene=False)
        result = self.pcp.capture_sceneviewer_flipbook_views(
            hou, save_dir=self.tmpdir)
        self.assertEqual(result["requested_views"], ["top", "front", "right"])
        self.assertEqual([item["view"] for item in result["views"]],
                         ["top", "front", "right"])
        self.assertTrue(result["complete"], result)
        self.assertTrue(result["state_restored"], result)
        for item in result["views"]:
            self.assertTrue(item["success"], item)
            self.assertGreater(item["width"], 0)
            self.assertGreater(item["height"], 0)
            self.assertGreater(item["size_bytes"], 0)
            self.assertTrue(os.path.isfile(item["save_path"]))
        self.assertEqual(pane.qt_grab_calls, 0)

    def test_explicit_perspective_preserves_order(self):
        hou, pane, _current, _other = self._default_hou()
        result = self.pcp.capture_sceneviewer_flipbook_views(
            hou, views=["perspective", "top"], save_dir=self.tmpdir)
        self.assertEqual(result["requested_views"], ["perspective", "top"])
        self.assertEqual([item["view"] for item in result["views"]],
                         ["perspective", "top"])
        self.assertEqual([call["view_type"] for call in pane.flipbook_calls],
                         ["Perspective", "Top"])

    def test_invalid_views_fail_before_flipbook(self):
        hou, pane, _current, _other = self._default_hou()
        for views in ([], ["top", "top"], ["bottom"], "top"):
            with self.assertRaises(ValueError):
                self.pcp.capture_sceneviewer_flipbook_views(
                    hou, views=views, save_dir=self.tmpdir)
            self.assertEqual(pane.flipbook_calls, [])

    def test_exact_desktop_and_pane_selection_restores_original_desktop(self):
        target = _Pane("target")
        current, other, ui = _make_desktops(target, current_is_scene=False)
        exact_target = other.paneTabs()[0]
        # Move the target pane exclusively to Other; Build has no SceneViewer.
        current._panes = [_OtherPane("network")]
        current._current = current._panes[0]
        hou = _Hou(current, [current, other])
        result = self.pcp.capture_sceneviewer_flipbook_views(
            hou, desktop_name="Other", pane_name="target",
            save_dir=self.tmpdir)
        self.assertTrue(result["complete"], result)
        self.assertIs(hou.ui.curDesktop(), current)
        self.assertEqual(other.set_current_calls, 1)
        self.assertEqual(current.set_current_calls, 1)
        self.assertEqual(len(exact_target.flipbook_calls), 3)

    def test_default_selection_does_not_scan_other_desktops(self):
        target = _Pane("target")
        current, other, _ui = _make_desktops(target, current_is_scene=False)
        current._panes = [_OtherPane("network")]
        current._current = current._panes[0]
        hou = _Hou(current, [current, other])
        with self.assertRaises(ValueError):
            self.pcp.capture_sceneviewer_flipbook_views(
                hou, save_dir=self.tmpdir)
        self.assertEqual(target.flipbook_calls, [])

    def test_partial_view_failure_keeps_other_results(self):
        pane = _Pane("scene", fail_views={"front"})
        hou, pane, _current, _other = self._default_hou(pane=pane)
        result = self.pcp.capture_sceneviewer_flipbook_views(
            hou, save_dir=self.tmpdir)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["views"]), 3)
        top, front, right = result["views"]
        self.assertTrue(top["success"], top)
        self.assertFalse(front["success"], front)
        self.assertIn("front", front["error"])
        self.assertTrue(right["success"], right)
        self.assertTrue(result["state_restored"])

    def test_viewport_camera_lock_and_type_are_restored(self):
        viewport = _Viewport(initial_type="Perspective",
                             camera_path="/obj/cam0", locked=True)
        pane = _Pane("scene", viewport=viewport)
        hou, pane, _current, _other = self._default_hou(pane=pane)
        result = self.pcp.capture_sceneviewer_flipbook_views(
            hou, views=["top"], save_dir=self.tmpdir)
        self.assertTrue(result["complete"], result)
        self.assertEqual(viewport.type(), "Perspective")
        self.assertEqual(viewport.cameraPath(), "/obj/cam0")
        self.assertTrue(viewport.isCameraLockedToView())
        self.assertEqual(viewport.change_calls, ["Top", "Perspective"])
        self.assertEqual(viewport.set_camera_calls[-1].path(), "/obj/cam0")

    def test_state_restore_failure_marks_partial(self):
        viewport = _Viewport(fail_restore=True)
        pane = _Pane("scene", viewport=viewport)
        hou, _pane, _current, _other = self._default_hou(pane=pane)
        result = self.pcp.capture_sceneviewer_flipbook_views(
            hou, views=["top"], save_dir=self.tmpdir)
        self.assertFalse(result["complete"])
        self.assertFalse(result["state_restored"])
        self.assertFalse(result["views"][0]["success"])
        self.assertIn("恢复", result["views"][0]["error"])


class BridgeAndHandlerRelayTests(unittest.TestCase):
    def _read(self, name):
        with open(os.path.join(ROOT, name), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_server_registers_handler_and_response_cap(self):
        source = self._read("server.py")
        tree = ast.parse(source)
        self.assertIn("capture_sceneviewer_flipbook_views", source)
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "HoudiniMCPServer")
        method = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "capture_sceneviewer_flipbook_views")
        method_source = ast.get_source_segment(source, method) or ""
        self.assertIn("apply_response_cap", method_source)
        names = [arg.arg for arg in method.args.args]
        self.assertEqual(names[-5:], ["views", "save_dir", "desktop_name",
                                      "pane_name", "fit_contents"])

    def test_server_handler_relays_all_arguments_and_caps_once(self):
        source = self._read("server.py")
        tree = ast.parse(source)
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "HoudiniMCPServer")
        method = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "capture_sceneviewer_flipbook_views")
        method_source = ast.get_source_segment(source, method)
        cap_calls = []

        def cap(value):
            cap_calls.append(value)
            return value

        pcp = types.SimpleNamespace(
            capture_sceneviewer_flipbook_views=lambda *args, **kwargs: {
                "kwargs": kwargs})
        namespace = {"hou": object(), "pcp": pcp,
                     "cmn": types.SimpleNamespace(apply_response_cap=cap)}
        exec(compile(method_source, "<server-handler>", "exec"), namespace)
        result = namespace["capture_sceneviewer_flipbook_views"](
            object(), ["top"], self.tmpdir, "Desk", "Pane", False)
        self.assertEqual(len(cap_calls), 1)
        self.assertEqual(result["kwargs"], {
            "views": ["top"], "save_dir": self.tmpdir,
            "desktop_name": "Desk", "pane_name": "Pane",
            "fit_contents": False,
        })

    def test_bridge_tool_relays_command_and_all_arguments(self):
        source = self._read("houdini_mcp_server.py")
        tree = ast.parse(source)
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "capture_sceneviewer_flipbook_views")
        fn_source = ast.get_source_segment(source, function)

        class _MCP(object):
            def tool(self):
                return lambda fn: fn

        calls = []

        def houdini_call(command, params):
            calls.append((command, params))
            return {"status": "success", "result": {"ok": True}}

        namespace = {"mcp": _MCP(), "_houdini_call": houdini_call}
        exec(compile(fn_source, "<bridge-tool>", "exec"), namespace)
        result = namespace["capture_sceneviewer_flipbook_views"](
            object(), ["top", "perspective"], self.tmpdir,
            "Desk", "Pane", False)
        self.assertEqual(result["status"], "success")
        self.assertEqual(calls, [("capture_sceneviewer_flipbook_views", {
            "views": ["top", "perspective"], "save_dir": self.tmpdir,
            "desktop_name": "Desk", "pane_name": "Pane",
            "fit_contents": False,
        })])

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
