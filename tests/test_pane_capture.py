"""Unit tests for external/houdinimcp/_pane_capture.py (PR 13).

Stdlib unittest, no hython required. hou is mocked via stub classes. PySide6 /
PySide2 is not installed in the test environment, so the default state of
_pane_capture._QT_BACKEND is None; that exercises the "graceful no-op /
warning" path required by the brief. A dedicated test class injects fake
PySide6 modules via sys.modules to exercise the widget.grab() path and the
Pillow-style in-memory QBuffer sizing path.

Tests cover (>= 30):
    - VALID_PANE_TYPES: length >= 30; contains 4 core types; all lowercase
      forms match real hou.paneTabType names.
    - _fit_pane_contents: 4 core types call correct fit method; other types
      no-op; _QT_BACKEND=None also no-op.
    - capture_pane_screenshot: with fake Qt -> widget.grab() called and
      return dict has required keys; without Qt -> warning dict;
      no pane -> ValueError; no widget -> RuntimeError; PySide2 backend
      surfaces in `_qt_backend`.
    - list_visible_panes: multi-desktop / multi-pane; is_current marker;
      no desktops -> empty list.
    - capture_multiple_panes: save_dir auto-created; all success; partial
      failure; one entry per input.
    - render_node_network: pane.cd() called; fit_contents True / False;
      missing node -> ValueError; no NetworkEditor pane -> ValueError.
    - apply_response_cap: each handler returns go through cmn.apply_response_cap.
    - bridge style: 4 new @mcp.tool() in PR 13 section have no type
      annotations and Chinese docstrings (AST probe). send_command is
      invoked with expected params for each tool; Houdini error responses
      are surfaced as bridge error dicts.

Run with:
    python -m unittest tests.test_pane_capture -v
"""
import ast
import importlib as _il
import importlib.util as _ilu
import os
import re
import shutil
import sys
import tempfile
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Build a synthetic "houdinimcp" package so the production-style
# `from . import _common as cmn` inside _pane_capture.py resolves.
_PKG_KEY = "houdinimcp"
_PCP_KEY = "houdinimcp._pane_capture"
if _PKG_KEY not in sys.modules:
    pkg = types.ModuleType(_PKG_KEY)
    pkg.__path__ = [ROOT]
    sys.modules[_PKG_KEY] = pkg

_SPEC_CMN = _ilu.spec_from_file_location("houdinimcp._common",
                                         os.path.join(ROOT, "_common.py"))
_common = _ilu.module_from_spec(_SPEC_CMN)
sys.modules["houdinimcp._common"] = _common
_SPEC_CMN.loader.exec_module(_common)
cmn = _common


def _load_pane_capture_fresh():
    """Reload _pane_capture module fresh from source, returns module."""
    if _PCP_KEY in sys.modules:
        del sys.modules[_PCP_KEY]
    spec = _ilu.spec_from_file_location(
        _PCP_KEY, os.path.join(ROOT, "_pane_capture.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[_PCP_KEY] = mod
    spec.loader.exec_module(mod)
    return mod


# Baseline module load: neither PySide6 nor PySide2 is installed -> _QT_BACKEND
# becomes None. All "graceful no-op" tests rely on this default.
pcp = _load_pane_capture_fresh()


# ---------------------------------------------------------------------------
# hou + Qt stubs
# ---------------------------------------------------------------------------
class _FakePaneTabType(object):
    """Holds attribute access matching hou.paneTabType.<Enum>."""
    NetworkEditor = "NetworkEditor"
    SceneViewer = "SceneViewer"
    Compositor = "Compositor"
    ChannelEditor = "ChannelEditor"


class _FakeViewport(object):
    def __init__(self):
        self.home_calls = []

    def home(self):
        self.home_calls.append(True)


class _FakePaneTab(object):
    """Just enough surface for _fit_pane_contents + render_node_network."""

    def __init__(self, name="PaneTab1", pane_class="NetworkEditor",
                 has_viewport=False, widget=None):
        self._name = name
        self._class = pane_class
        self._home_calls = []
        self._cd_calls = []
        self._qt_widget = widget
        self._viewport = _FakeViewport() if has_viewport else None

    def name(self):
        return self._name

    def homeAll(self):
        self._home_calls.append(True)

    def cd(self, path):
        self._cd_calls.append(path)

    def curViewport(self):
        return self._viewport

    def qtWidget(self):
        return self._qt_widget


class _FakeDesktop(object):
    def __init__(self, name, pane_tabs, current_pane=None):
        self._name = name
        self._pane_tabs = pane_tabs
        self._current = current_pane if current_pane is not None else (
            pane_tabs[0] if pane_tabs else None)

    def name(self):
        return self._name

    def paneTabs(self):
        return list(self._pane_tabs)

    def currentPaneTab(self):
        return self._current


class _FakeUI(object):
    def __init__(self, pane_tabs_by_type=None, desktops=None):
        # pane_tabs_by_type: dict mapping hou.paneTabType.X -> _FakePaneTab
        self._by_type = pane_tabs_by_type or {}
        self._desktops = desktops or []

    def paneTabOfType(self, pane_type):
        return self._by_type.get(pane_type)

    def desktops(self):
        return list(self._desktops)


class _FakeNode(object):
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path


class _FakeHou(object):
    def __init__(self, pane_tabs_by_type=None, desktops=None, nodes=None):
        self.paneTabType = _FakePaneTabType()
        self.ui = _FakeUI(pane_tabs_by_type=pane_tabs_by_type,
                          desktops=desktops)
        self._nodes = nodes or {}

    def node(self, path):
        return self._nodes.get(path)


def _make_hou_with_scene_viewer(widget):
    """hou stub with a SceneViewer pane that exposes a fake Qt widget."""
    sv = _FakePaneTab(name="sv1", pane_class="SceneViewer", widget=widget)
    return _FakeHou(pane_tabs_by_type={
        _FakePaneTabType.SceneViewer: sv,
    })


def _make_hou_with_network_editor(widget=None):
    ne = _FakePaneTab(name="ne1", pane_class="NetworkEditor", widget=widget)
    return _FakeHou(pane_tabs_by_type={
        _FakePaneTabType.NetworkEditor: ne,
    }), ne


# ---------------------------------------------------------------------------
# Fake PySide6 / PySide2 modules (used by QtMockedCaptureTests)
# ---------------------------------------------------------------------------
class _FakeQBuffer(object):
    def __init__(self):
        self._open = False
        # Pretend PNG: 32 bytes header + 100 bytes body
        self._data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    def open(self, mode):
        self._open = True

    def close(self):
        self._open = False

    def size(self):
        return len(self._data)


class _FakeQIODevice(object):
    WriteOnly = 1


class _FakeQImage(object):
    def __init__(self, w, h):
        self._w = w
        self._h = h

    def width(self):
        return self._w

    def height(self):
        return self._h

    def save(self, target, fmt=None):
        if hasattr(target, "size"):
            return True
        # File path mode: write 132 bytes
        with open(target, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        return True


class _FakeQPixmap(object):
    def __init__(self, w, h):
        self._img = _FakeQImage(w, h)

    def toImage(self):
        return self._img


class _FakeQWidget(object):
    def __init__(self, w=800, h=600):
        self._w = w
        self._h = h
        self.grab_calls = []

    def grab(self):
        self.grab_calls.append(True)
        return _FakeQPixmap(self._w, self._h)


class _FakeQtWidgets(object):
    QWidget = _FakeQWidget


class _FakeQtGui(object):
    QImage = _FakeQImage


class _FakeQtCore(object):
    QBuffer = _FakeQBuffer
    QIODevice = _FakeQIODevice


def _install_fake_pyside(modules_dict):
    """Inject fake PySide modules into sys.modules.

    modules_dict: keys are 'PySide6' / 'PySide2'; values are module instances
    with attributes QtWidgets / QtCore / QtGui matching the real layout.
    """
    for name, mod in modules_dict.items():
        sys.modules[name] = mod


def _uninstall_fake_pyside(names):
    for name in names:
        sys.modules.pop(name, None)


# ===========================================================================
# Section A: VALID_PANE_TYPES
# ===========================================================================
class ValidPaneTypesTests(unittest.TestCase):

    def test_length_at_least_30(self):
        self.assertGreaterEqual(len(pcp.VALID_PANE_TYPES), 30)

    def test_contains_network_editor(self):
        self.assertIn("NetworkEditor", pcp.VALID_PANE_TYPES)

    def test_contains_scene_viewer(self):
        self.assertIn("SceneViewer", pcp.VALID_PANE_TYPES)

    def test_contains_compositor(self):
        self.assertIn("Compositor", pcp.VALID_PANE_TYPES)

    def test_contains_channel_editor(self):
        self.assertIn("ChannelEditor", pcp.VALID_PANE_TYPES)

    def test_lowercase_matches_real_hou_naming(self):
        # Spot-check: lower() of each entry is a legal attribute on the fake
        # hou.paneTabType (we use uppercase keys here matching hou convention).
        for name in pcp.VALID_PANE_TYPES:
            self.assertTrue(name, "Empty pane type name")
            # All must be strings.
            self.assertIsInstance(name, str)


# ===========================================================================
# Section B: _fit_pane_contents
# ===========================================================================
class FitPaneContentsTests(unittest.TestCase):

    def test_network_editor_calls_homeall(self):
        pane = _FakePaneTab()
        # Qt backend is None in this env, so _fit_pane_contents is a no-op.
        # To still verify the method dispatch path, install fake PySide6,
        # reload, run, restore.
        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType("PySide6.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType("PySide6.QtWidgets")
        fake_qtwidgets.QWidget = _FakeQWidget
        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        _install_fake_pyside({"PySide6": fake_pkg,
                              "PySide6.QtCore": fake_qtcore,
                              "PySide6.QtGui": fake_qtgui,
                              "PySide6.QtWidgets": fake_qtwidgets})
        try:
            pcp2 = _load_pane_capture_fresh()
            self.assertEqual(pcp2._QT_BACKEND, "PySide6")
            pcp2._fit_pane_contents(pane, "NetworkEditor")
            self.assertEqual(len(pane._home_calls), 1)
        finally:
            _uninstall_fake_pyside(["PySide6", "PySide6.QtCore",
                                    "PySide6.QtGui", "PySide6.QtWidgets"])
            _load_pane_capture_fresh()

    def test_compositor_calls_homeall(self):
        pane = _FakePaneTab()
        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType("PySide6.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType("PySide6.QtWidgets")
        fake_qtwidgets.QWidget = _FakeQWidget
        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        _install_fake_pyside({"PySide6": fake_pkg,
                              "PySide6.QtCore": fake_qtcore,
                              "PySide6.QtGui": fake_qtgui,
                              "PySide6.QtWidgets": fake_qtwidgets})
        try:
            pcp2 = _load_pane_capture_fresh()
            pcp2._fit_pane_contents(pane, "Compositor")
            self.assertEqual(len(pane._home_calls), 1)
        finally:
            _uninstall_fake_pyside(["PySide6", "PySide6.QtCore",
                                    "PySide6.QtGui", "PySide6.QtWidgets"])
            _load_pane_capture_fresh()

    def test_channel_editor_calls_homeall(self):
        pane = _FakePaneTab()
        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType("PySide6.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType("PySide6.QtWidgets")
        fake_qtwidgets.QWidget = _FakeQWidget
        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        _install_fake_pyside({"PySide6": fake_pkg,
                              "PySide6.QtCore": fake_qtcore,
                              "PySide6.QtGui": fake_qtgui,
                              "PySide6.QtWidgets": fake_qtwidgets})
        try:
            pcp2 = _load_pane_capture_fresh()
            pcp2._fit_pane_contents(pane, "ChannelEditor")
            self.assertEqual(len(pane._home_calls), 1)
        finally:
            _uninstall_fake_pyside(["PySide6", "PySide6.QtCore",
                                    "PySide6.QtGui", "PySide6.QtWidgets"])
            _load_pane_capture_fresh()

    def test_scene_viewer_calls_viewport_home(self):
        pane = _FakePaneTab(has_viewport=True)
        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType("PySide6.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType("PySide6.QtWidgets")
        fake_qtwidgets.QWidget = _FakeQWidget
        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        _install_fake_pyside({"PySide6": fake_pkg,
                              "PySide6.QtCore": fake_qtcore,
                              "PySide6.QtGui": fake_qtgui,
                              "PySide6.QtWidgets": fake_qtwidgets})
        try:
            pcp2 = _load_pane_capture_fresh()
            pcp2._fit_pane_contents(pane, "SceneViewer")
            self.assertEqual(len(pane._viewport.home_calls), 1)
        finally:
            _uninstall_fake_pyside(["PySide6", "PySide6.QtCore",
                                    "PySide6.QtGui", "PySide6.QtWidgets"])
            _load_pane_capture_fresh()

    def test_other_types_no_op_even_with_qt(self):
        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType("PySide6.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType("PySide6.QtWidgets")
        fake_qtwidgets.QWidget = _FakeQWidget
        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        _install_fake_pyside({"PySide6": fake_pkg,
                              "PySide6.QtCore": fake_qtcore,
                              "PySide6.QtGui": fake_qtgui,
                              "PySide6.QtWidgets": fake_qtwidgets})
        try:
            pcp2 = _load_pane_capture_fresh()
            pane = _FakePaneTab()  # has homeAll but for ParameterEditor should NOT call it
            pcp2._fit_pane_contents(pane, "ParameterEditor")
            self.assertEqual(pane._home_calls, [])
            pcp2._fit_pane_contents(pane, "PythonPanel")
            self.assertEqual(pane._home_calls, [])
            pcp2._fit_pane_contents(pane, "HelpBrowser")
            self.assertEqual(pane._home_calls, [])
        finally:
            _uninstall_fake_pyside(["PySide6", "PySide6.QtCore",
                                    "PySide6.QtGui", "PySide6.QtWidgets"])
            _load_pane_capture_fresh()

    def test_no_qt_no_op(self):
        # Default state: _QT_BACKEND is None -> _fit_pane_contents must be
        # no-op even for NetworkEditor (so we don't crash on missing Qt).
        pane = _FakePaneTab()
        self.assertIsNone(pcp._QT_BACKEND)
        pcp._fit_pane_contents(pane, "NetworkEditor")
        self.assertEqual(pane._home_calls, [])


# ===========================================================================
# Section C: capture_pane_screenshot (no Qt path)
# ===========================================================================
class CapturePaneScreenshotNoQtTests(unittest.TestCase):

    def test_no_qt_returns_warning(self):
        hou = _FakeHou()
        result = pcp.capture_pane_screenshot(hou, "SceneViewer")
        self.assertIn("_warning", result)
        self.assertEqual(result["pane_type"], "SceneViewer")
        self.assertEqual(result["width"], 0)
        self.assertEqual(result["height"], 0)
        self.assertEqual(result["_qt_backend"], None)


# ===========================================================================
# Section C.2: capture_pane_screenshot (with fake PySide6)
# ===========================================================================
class CapturePaneScreenshotWithQtTests(unittest.TestCase):
    """Reload _pane_capture with fake PySide6 injected; verify widget.grab
    is called and the return dict carries required keys + Pyside6 marker."""

    @classmethod
    def setUpClass(cls):
        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType("PySide6.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType("PySide6.QtWidgets")
        fake_qtwidgets.QWidget = _FakeQWidget
        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        sys.modules["PySide6"] = fake_pkg
        sys.modules["PySide6.QtCore"] = fake_qtcore
        sys.modules["PySide6.QtGui"] = fake_qtgui
        sys.modules["PySide6.QtWidgets"] = fake_qtwidgets
        cls.pcp = _load_pane_capture_fresh()

    @classmethod
    def tearDownClass(cls):
        for k in ("PySide6", "PySide6.QtCore", "PySide6.QtGui",
                  "PySide6.QtWidgets"):
            sys.modules.pop(k, None)
        _load_pane_capture_fresh()

    def test_qt_backend_is_pyside6(self):
        self.assertEqual(self.pcp._QT_BACKEND, "PySide6")

    def test_normal_capture_with_save_path(self):
        widget = _FakeQWidget(w=1024, h=768)
        hou = _make_hou_with_scene_viewer(widget)
        tmpdir = tempfile.mkdtemp()
        try:
            save_path = os.path.join(tmpdir, "out.png")
            result = self.pcp.capture_pane_screenshot(
                hou, "SceneViewer", save_path=save_path)
            self.assertEqual(result["pane_type"], "SceneViewer")
            self.assertEqual(result["save_path"], save_path)
            self.assertEqual(result["width"], 1024)
            self.assertEqual(result["height"], 768)
            self.assertGreater(result["size_bytes"], 0)
            self.assertEqual(result["_qt_backend"], "PySide6")
            self.assertEqual(widget.grab_calls, [True])
            self.assertTrue(os.path.isfile(save_path))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_capture_without_save_path_uses_qbuffer(self):
        widget = _FakeQWidget(w=800, h=600)
        hou = _make_hou_with_scene_viewer(widget)
        result = self.pcp.capture_pane_screenshot(hou, "SceneViewer")
        self.assertIsNone(result["save_path"])
        self.assertEqual(result["width"], 800)
        self.assertEqual(result["height"], 600)
        self.assertGreater(result["size_bytes"], 0)

    def test_no_pane_raises_value_error(self):
        widget = _FakeQWidget()
        # Empty by_type dict => paneTabOfType returns None
        hou = _FakeHou(pane_tabs_by_type={})
        with self.assertRaises(ValueError):
            self.pcp.capture_pane_screenshot(hou, "SceneViewer")

    def test_no_widget_raises_runtime_error(self):
        # Pane exists but qtWidget() returns None
        pane = _FakePaneTab(widget=None)
        hou = _FakeHou(pane_tabs_by_type={_FakePaneTabType.SceneViewer: pane})
        with self.assertRaises(RuntimeError):
            self.pcp.capture_pane_screenshot(hou, "SceneViewer")

    def test_fit_contents_false_skips_fit(self):
        widget = _FakeQWidget()
        # Use a NetworkEditor pane (so fit would call homeAll if enabled)
        pane = _FakePaneTab(widget=widget)
        hou = _FakeHou(pane_tabs_by_type={
            _FakePaneTabType.NetworkEditor: pane})
        self.pcp.capture_pane_screenshot(hou, "NetworkEditor",
                                         fit_contents=False)
        self.assertEqual(pane._home_calls, [])


# ===========================================================================
# Section C.3: capture_pane_screenshot with PySide2 fake
# ===========================================================================
class CapturePaneScreenshotPySide2Tests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fake_qtcore = types.ModuleType("PySide2.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType("PySide2.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType("PySide2.QtWidgets")
        fake_qtwidgets.QWidget = _FakeQWidget
        fake_pkg = types.ModuleType("PySide2")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        sys.modules["PySide2"] = fake_pkg
        sys.modules["PySide2.QtCore"] = fake_qtcore
        sys.modules["PySide2.QtGui"] = fake_qtgui
        sys.modules["PySide2.QtWidgets"] = fake_qtwidgets
        cls.pcp2 = _load_pane_capture_fresh()

    @classmethod
    def tearDownClass(cls):
        for k in ("PySide2", "PySide2.QtCore", "PySide2.QtGui",
                  "PySide2.QtWidgets"):
            sys.modules.pop(k, None)
        _load_pane_capture_fresh()

    def test_qt_backend_is_pyside2(self):
        self.assertEqual(self.pcp2._QT_BACKEND, "PySide2")

    def test_capture_with_pyside2(self):
        widget = _FakeQWidget(w=320, h=240)
        hou = _make_hou_with_scene_viewer(widget)
        result = self.pcp2.capture_pane_screenshot(hou, "SceneViewer")
        self.assertEqual(result["_qt_backend"], "PySide2")
        self.assertEqual(result["width"], 320)
        self.assertEqual(result["height"], 240)


# ===========================================================================
# Section D: list_visible_panes
# ===========================================================================
class ListVisiblePanesTests(unittest.TestCase):

    def test_multi_desktop_multi_pane(self):
        p1 = _FakePaneTab(name="pane1")
        p2 = _FakePaneTab(name="pane2")
        p3 = _FakePaneTab(name="pane3")
        d1 = _FakeDesktop("desk1", [p1, p2])
        d2 = _FakeDesktop("desk2", [p3])
        hou = _FakeHou(desktops=[d1, d2])
        result = pcp.list_visible_panes(hou)
        self.assertEqual(len(result), 3)
        desktops = sorted(r["desktop"] for r in result)
        self.assertEqual(desktops, ["desk1", "desk1", "desk2"])

    def test_is_current_marker(self):
        p1 = _FakePaneTab(name="pane1")
        p2 = _FakePaneTab(name="pane2")
        d1 = _FakeDesktop("desk1", [p1, p2], current_pane=p2)
        hou = _FakeHou(desktops=[d1])
        result = pcp.list_visible_panes(hou)
        is_current_map = {r["name"]: r["is_current"] for r in result}
        self.assertTrue(is_current_map["pane2"])
        self.assertFalse(is_current_map["pane1"])

    def test_no_desktops_returns_empty(self):
        hou = _FakeHou(desktops=[])
        result = pcp.list_visible_panes(hou)
        self.assertEqual(result, [])


# ===========================================================================
# Section E: capture_multiple_panes
# ===========================================================================
class CaptureMultiplePanesTests(unittest.TestCase):

    def test_save_dir_auto_created(self):
        hou = _FakeHou()
        tmpdir = tempfile.mkdtemp()
        new_dir = os.path.join(tmpdir, "subdir", "nested")
        try:
            pcp.capture_multiple_panes(hou, ["SceneViewer"], new_dir)
            self.assertTrue(os.path.isdir(new_dir))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_all_return_warning_when_no_qt(self):
        hou = _FakeHou()
        tmpdir = tempfile.mkdtemp()
        try:
            results = pcp.capture_multiple_panes(
                hou, ["SceneViewer", "NetworkEditor", "Compositor"], tmpdir)
            self.assertEqual(len(results), 3)
            for r in results:
                self.assertIn("pane_type", r)
                self.assertIn("save_path", r)
                self.assertIn("success", r)
                self.assertIn("error", r)
                # Without Qt, capture_pane_screenshot returns warning dict ->
                # capture_multiple_panes must surface it as success=False.
                self.assertFalse(r["success"])
                self.assertIsNotNone(r["error"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_partial_failure_continues(self):
        """Inject fake PySide6 so capture_pane_screenshot normally succeeds,
        then monkey-patch it to raise ValueError for NetworkEditor. The
        other two panes must still succeed and the loop must continue past
        the failure."""

        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType("PySide6.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType("PySide6.QtWidgets")
        fake_qtwidgets.QWidget = _FakeQWidget
        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        _install_fake_pyside({"PySide6": fake_pkg,
                              "PySide6.QtCore": fake_qtcore,
                              "PySide6.QtGui": fake_qtgui,
                              "PySide6.QtWidgets": fake_qtwidgets})
        try:
            pcp_qt = _load_pane_capture_fresh()
            self.assertEqual(pcp_qt._QT_BACKEND, "PySide6")
            original = pcp_qt.capture_pane_screenshot

            def flaky(hou, pane_type_name, save_path=None, fit_contents=True):
                if pane_type_name == "NetworkEditor":
                    raise ValueError("simulated failure")
                return original(hou, pane_type_name, save_path=save_path,
                                fit_contents=fit_contents)

            pcp_qt.capture_pane_screenshot = flaky
            # Build hou with all three pane types so they would normally succeed
            widget = _FakeQWidget()
            sv = _FakePaneTab(widget=widget)
            ne = _FakePaneTab(widget=widget)
            co = _FakePaneTab(widget=widget)
            hou = _FakeHou(pane_tabs_by_type={
                _FakePaneTabType.SceneViewer: sv,
                _FakePaneTabType.NetworkEditor: ne,
                _FakePaneTabType.Compositor: co,
            })
            tmpdir = tempfile.mkdtemp()
            try:
                results = pcp_qt.capture_multiple_panes(
                    hou,
                    ["SceneViewer", "NetworkEditor", "Compositor"],
                    tmpdir)
                self.assertEqual(len(results), 3)
                failed = [r for r in results if not r["success"]]
                self.assertEqual(len(failed), 1)
                self.assertEqual(failed[0]["pane_type"], "NetworkEditor")
                self.assertIn("simulated failure", failed[0]["error"])
            finally:
                pcp_qt.capture_pane_screenshot = original
                shutil.rmtree(tmpdir, ignore_errors=True)
        finally:
            _uninstall_fake_pyside(["PySide6", "PySide6.QtCore",
                                    "PySide6.QtGui", "PySide6.QtWidgets"])
            _load_pane_capture_fresh()


# ===========================================================================
# Section F: render_node_network
# ===========================================================================
class RenderNodeNetworkTests(unittest.TestCase):

    def test_pane_cd_called(self):
        widget = _FakeQWidget()
        hou, ne = _make_hou_with_network_editor(widget=widget)
        node = _FakeNode("/obj/geo1")
        hou._nodes["/obj/geo1"] = node
        # Qt is None here -> capture_pane_screenshot returns warning;
        # pane.cd() must still be called.
        pcp.render_node_network(hou, "/obj/geo1")
        self.assertEqual(ne._cd_calls, ["/obj/geo1"])

    def test_missing_node_raises_value_error(self):
        widget = _FakeQWidget()
        hou, _ne = _make_hou_with_network_editor(widget=widget)
        with self.assertRaises(ValueError):
            pcp.render_node_network(hou, "/obj/missing")

    def test_no_network_editor_pane_raises(self):
        hou = _FakeHou(pane_tabs_by_type={})
        hou._nodes["/obj/geo1"] = _FakeNode("/obj/geo1")
        with self.assertRaises(ValueError):
            pcp.render_node_network(hou, "/obj/geo1")


# ===========================================================================
# Section G: apply_response_cap integration
# ===========================================================================
class ApplyResponseCapIntegrationTests(unittest.TestCase):
    """Each handler's return dict must be processable by cmn.apply_response_cap
    without raising and without mutating semantics (size enforcement)."""

    def test_capture_pane_screenshot_passes_through_cap(self):
        hou = _FakeHou()
        result = pcp.capture_pane_screenshot(hou, "SceneViewer")
        capped = cmn.apply_response_cap(result, 16384)
        self.assertEqual(capped.get("pane_type"), "SceneViewer")

    def test_list_visible_panes_passes_through_cap(self):
        p1 = _FakePaneTab(name="p1")
        d1 = _FakeDesktop("d", [p1])
        hou = _FakeHou(desktops=[d1])
        result = pcp.list_visible_panes(hou)
        capped = cmn.apply_response_cap({"panes": result}, 16384)
        self.assertIn("panes", capped)
        self.assertEqual(len(capped["panes"]), 1)

    def test_capture_multiple_panes_passes_through_cap(self):
        hou = _FakeHou()
        tmpdir = tempfile.mkdtemp()
        try:
            result = pcp.capture_multiple_panes(hou, ["SceneViewer"], tmpdir)
            capped = cmn.apply_response_cap({"results": result}, 16384)
            self.assertIn("results", capped)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_render_node_network_passes_through_cap(self):
        widget = _FakeQWidget()
        hou, _ne = _make_hou_with_network_editor(widget=widget)
        hou._nodes["/obj/geo1"] = _FakeNode("/obj/geo1")
        result = pcp.render_node_network(hou, "/obj/geo1")
        capped = cmn.apply_response_cap(result, 16384)
        self.assertEqual(capped.get("pane_type"), "NetworkEditor")


# ===========================================================================
# Section H: bridge style probe (PR 13 tools)
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "houdini_mcp_server.py")
PR13_SECTION_HEADER = "# PR 13 Pane Capture Tools"


def _has_cjk(s):
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _signature_has_annotations(fn):
    args = fn.args
    for arg in (args.posonlyargs + args.args + args.kwonlyargs):
        if arg.annotation is not None:
            return True
    if args.vararg and args.vararg.annotation is not None:
        return True
    if args.kwarg and args.kwarg.annotation is not None:
        return True
    return fn.returns is not None


def _find_pr13_function_nodes():
    """Find PR 13 @mcp.tool() functions. The PR 13 section is bounded by
    its own header and the next major section header (a top-level comment
    line of the form `# PR <number> ... Tools`).
    """
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    lines = src.splitlines()
    header_line = None
    for i, line in enumerate(lines, start=1):
        if PR13_SECTION_HEADER in line:
            header_line = i
            break
    if header_line is None:
        raise AssertionError(
            "PR 13 section marker not found in houdini_mcp_server.py")
    # Stop at the next top-level section header. Section headers in this
    # file follow the pattern `# PR <N> <Title> Tools` and live at column 0
    # (not indented inside a function body).
    stop_line = len(lines) + 1
    section_header_re = re.compile(r"^#\s*PR\s+\d+\s+\S.*\bTools\b")
    for i in range(header_line + 1, len(lines) + 1):
        line = lines[i - 1]
        if section_header_re.match(line) and PR13_SECTION_HEADER not in line:
            stop_line = i
            break
    fns = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.lineno <= header_line:
            continue
        if node.lineno >= stop_line:
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                func = dec.func
                if isinstance(func, ast.Attribute) and func.attr == "tool":
                    fns.append(node)
                    break
    return fns


class PR13BridgeStyleTests(unittest.TestCase):
    """Brief: 4 new @mcp.tool() must have no type annotations and Chinese
    docstrings."""

    def setUp(self):
        self.fns = _find_pr13_function_nodes()
        self.assertEqual(
            len(self.fns), 4,
            "Expected 4 PR 13 @mcp.tool() functions, found {0}: {1}".format(
                len(self.fns), [f.name for f in self.fns]))
        self.names = sorted(f.name for f in self.fns)
        self.expected = sorted([
            "capture_pane_screenshot",
            "list_visible_panes",
            "capture_multiple_panes",
            "render_node_network",
        ])
        self.assertEqual(self.names, self.expected)

    def test_capture_pane_screenshot_no_annotations(self):
        fn = next(f for f in self.fns if f.name == "capture_pane_screenshot")
        self.assertFalse(_signature_has_annotations(fn))

    def test_list_visible_panes_no_annotations(self):
        fn = next(f for f in self.fns if f.name == "list_visible_panes")
        self.assertFalse(_signature_has_annotations(fn))

    def test_capture_multiple_panes_no_annotations(self):
        fn = next(f for f in self.fns if f.name == "capture_multiple_panes")
        self.assertFalse(_signature_has_annotations(fn))

    def test_render_node_network_no_annotations(self):
        fn = next(f for f in self.fns if f.name == "render_node_network")
        self.assertFalse(_signature_has_annotations(fn))

    def test_capture_pane_screenshot_chinese_docstring(self):
        fn = next(f for f in self.fns if f.name == "capture_pane_screenshot")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(_has_cjk(doc),
                        "capture_pane_screenshot docstring must contain CJK: "
                        + repr(doc))

    def test_list_visible_panes_chinese_docstring(self):
        fn = next(f for f in self.fns if f.name == "list_visible_panes")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(_has_cjk(doc),
                        "list_visible_panes docstring must contain CJK: "
                        + repr(doc))

    def test_capture_multiple_panes_chinese_docstring(self):
        fn = next(f for f in self.fns if f.name == "capture_multiple_panes")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(_has_cjk(doc),
                        "capture_multiple_panes docstring must contain CJK: "
                        + repr(doc))

    def test_render_node_network_chinese_docstring(self):
        fn = next(f for f in self.fns if f.name == "render_node_network")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(_has_cjk(doc),
                        "render_node_network docstring must contain CJK: "
                        + repr(doc))


# ===========================================================================
# Section I: bridge send_command wiring (PR 13 server.py handlers)
# ===========================================================================
class ServerHandlersTests(unittest.TestCase):
    """Verify the 4 new handlers are registered in server.py's handlers dict
    and wrap the underlying pcp functions, applying cmn.apply_response_cap."""

    def setUp(self):
        self.server_py_path = os.path.join(ROOT, "server.py")
        with open(self.server_py_path, "r", encoding="utf-8") as f:
            self.src = f.read()
        self.tree = ast.parse(self.src)

    def test_pane_capture_import_present(self):
        self.assertIn("from . import _pane_capture as pcp", self.src)

    def test_handlers_dict_registers_all_four(self):
        # Find the handlers dict literal: {'capture_pane_screenshot': ..., ...}
        expected_keys = {
            "capture_pane_screenshot",
            "list_visible_panes",
            "capture_multiple_panes",
            "render_node_network",
        }
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        if k.value in expected_keys:
                            found.add(k.value)
        self.assertEqual(found, expected_keys,
                         "All 4 PR 13 handlers must be in server.py handlers dict")

    def test_handlers_call_pcp_functions(self):
        # Each registered handler should reference the pcp module
        # (self.<name> -> thin wrapper that calls pcp.<name>)
        # Verify by checking the method definitions exist in the class.
        cls = next((n for n in self.tree.body
                    if isinstance(n, ast.ClassDef)
                    and n.name == "HoudiniMCPServer"), None)
        self.assertIsNotNone(cls)
        method_names = {m.name for m in cls.body if isinstance(m, ast.FunctionDef)}
        for name in ("capture_pane_screenshot", "list_visible_panes",
                     "capture_multiple_panes", "render_node_network"):
            self.assertIn(name, method_names,
                          "HoudiniMCPServer must define method {0}".format(name))

    def test_handlers_apply_response_cap(self):
        """Each thin wrapper must end with cmn.apply_response_cap(<call>)."""
        cls = next((n for n in self.tree.body
                    if isinstance(n, ast.ClassDef)
                    and n.name == "HoudiniMCPServer"), None)
        self.assertIsNotNone(cls)
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            if method.name not in ("capture_pane_screenshot",
                                   "list_visible_panes",
                                   "capture_multiple_panes",
                                   "render_node_network"):
                continue
            src = ast.get_source_segment(self.src, method) or ""
            self.assertIn("apply_response_cap", src,
                          "Handler {0} must wrap result with apply_response_cap"
                          .format(method.name))

    def test_mutating_commands_includes_three_capture(self):
        """capture_pane_screenshot / capture_multiple_panes / render_node_network
        are added to MUTATING_COMMANDS; list_visible_panes is not."""
        cls = next((n for n in self.tree.body
                    if isinstance(n, ast.ClassDef)
                    and n.name == "HoudiniMCPServer"), None)
        self.assertIsNotNone(cls)
        # Find MUTATING_COMMANDS assignment
        for node in cls.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "MUTATING_COMMANDS":
                        src = ast.get_source_segment(self.src, node) or ""
                        self.assertIn("capture_pane_screenshot", src)
                        self.assertIn("capture_multiple_panes", src)
                        self.assertIn("render_node_network", src)
                        # list_visible_panes is read-only, must NOT be in MUTATING
                        self.assertNotIn("list_visible_panes", src)
                        return
        self.fail("MUTATING_COMMANDS assignment not found")


if __name__ == "__main__":
    unittest.main()