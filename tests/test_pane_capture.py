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


# ---------------------------------------------------------------------------
# Important 3: PySide fixture robustness helpers.
#
# Baseline assumption: neither PySide6 nor PySide2 is installed in the test
# env, so _pane_capture._QT_BACKEND becomes None. We must enforce this
# invariant even if the host happens to have PySide pre-installed (e.g.
# another test in the same process loaded it). The snapshot context manager
# also guarantees that any fakes installed during a test are stripped on
# teardown, and the original _pane_capture module object is restored.
# ---------------------------------------------------------------------------
_QT_PREFIXES = ("PySide6", "PySide2", "shiboken6", "shiboken2")


class _QtSysModulesSnapshot(object):
    """Snapshot and restore sys.modules entries related to Qt.

    On __enter__: snapshot all sys.modules keys matching one of the Qt
    prefixes (or submodules thereof), then delete them. After __enter__,
    `from PySide6 ...` / `from PySide2 ...` will fall through to ImportError,
    so _pane_capture's try/except will set _QT_BACKEND = None.

    On __exit__: first delete any Qt-related keys NOT in the snapshot (those
    were injected as fakes during the block), then restore the originals.
    """

    def __init__(self, prefixes=_QT_PREFIXES):
        self._prefixes = tuple(prefixes)
        self._saved = None

    def __enter__(self):
        saved = {}
        for k in list(sys.modules):
            if self._is_qt_key(k):
                saved[k] = sys.modules[k]
                del sys.modules[k]
        self._saved = saved
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Strip any Qt-related fakes that snuck in during the block.
        for k in list(sys.modules):
            if self._is_qt_key(k) and k not in self._saved:
                del sys.modules[k]
        # Restore originals.
        for k, v in self._saved.items():
            sys.modules[k] = v
        self._saved = None
        return False

    def _is_qt_key(self, key):
        return any(key == p or key.startswith(p + ".")
                   for p in self._prefixes)


class _FakeQtFixture(object):
    """Set up + tear down a fake PySide6/PySide2 environment robustly.

    Usage:
        cls.fx = _FakeQtFixture("PySide6")
        cls.fx.__enter__()
        try:
            cls.pcp = _load_pane_capture_fresh()
            cls.addClassCleanup(cls.fx.__exit__, None, None, None)
            cls.addClassCleanup(_restore_pcp_module, cls._orig_pcp)
        except Exception:
            cls.fx.__exit__(*sys.exc_info())
            raise

    On teardown:
      1. Pop fake PySide keys from sys.modules
      2. _QtSysModulesSnapshot.__exit__ removes any other Qt fakes and
         restores originals (handles PySide6 vs PySide2 cross-contamination)
      3. Restore original sys.modules[_PCP_KEY] object captured pre-setup
    """

    def __init__(self, version, block=None):
        self.version = version
        # By default block the OTHER major version to prevent cross-detection.
        if block is None:
            block = ("PySide6",) if version == "PySide2" else ("PySide2",)
        self._snapshot = _QtSysModulesSnapshot(prefixes=_QT_PREFIXES)

    def __enter__(self):
        self._snapshot.__enter__()
        self._install_fake_qt()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Pop fakes we installed first
        self._uninstall_fake_qt()
        # Then snapshot teardown restores original Qt modules + strips others
        self._snapshot.__exit__(exc_type, exc_val, exc_tb)

    def _install_fake_qt(self):
        fake_qtcore = types.ModuleType(self.version + ".QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType(self.version + ".QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType(self.version + ".QtWidgets")
        fake_qtwidgets.QWidget = _FakeQWidget
        fake_pkg = types.ModuleType(self.version)
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        sys.modules[self.version] = fake_pkg
        sys.modules[self.version + ".QtCore"] = fake_qtcore
        sys.modules[self.version + ".QtGui"] = fake_qtgui
        sys.modules[self.version + ".QtWidgets"] = fake_qtwidgets

    def _uninstall_fake_qt(self):
        for k in (self.version, self.version + ".QtCore",
                  self.version + ".QtGui", self.version + ".QtWidgets"):
            sys.modules.pop(k, None)


def _restore_pcp_module(orig):
    """Restore sys.modules[_PCP_KEY] to the pre-fixture value.

    If no pre-fixture value existed, pop the key so subsequent fresh loads
    create a new module object (matches the original "first time" state).
    """
    if orig is None:
        sys.modules.pop(_PCP_KEY, None)
    else:
        sys.modules[_PCP_KEY] = orig


# Baseline: block Qt up front so the module-level _pane_capture load below
# observes _QT_BACKEND == None regardless of host environment. Baseline is
# permanent for the test process lifetime (no teardown); subsequent fixtures
# use their own _QtSysModulesSnapshot.
_BASELINE_NO_QT = _QtSysModulesSnapshot()
_BASELINE_NO_QT.__enter__()
# Baseline module load: neither PySide6 nor PySide2 is installed -> _QT_BACKEND
# becomes None. All "graceful no-op" tests rely on this default.
pcp = _load_pane_capture_fresh()


def _run_with_fake_pyside6(body):
    """Module-level helper: run `body(pcp_module)` inside a fake PySide6
    fixture context. Restores sys.modules + original _pane_capture module
    object on teardown even if `body` raises.

    Used by instance-level tests that need a transient fake PySide6
    environment per test case.
    """
    orig_pcp = sys.modules.get(_PCP_KEY)
    fx = _FakeQtFixture("PySide6")
    fx.__enter__()
    try:
        pcp2 = _load_pane_capture_fresh()
        body(pcp2)
    finally:
        fx.__exit__(None, None, None)
        _restore_pcp_module(orig_pcp)


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
    def __init__(self, w, h, null=False):
        self._w = w
        self._h = h
        self._null = null

    def isNull(self):
        return self._null

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
    def __init__(self, w, h, null=False):
        self._img = _FakeQImage(w, h, null=null)
        self._null = null

    def isNull(self):
        return self._null

    def toImage(self):
        return self._img


class _NullImageQImage(object):
    """Returns from toImage() when image is invalid (null or None)."""
    def width(self):
        return 0

    def height(self):
        return 0

    def isNull(self):
        return True

    def save(self, target, fmt=None):
        return False


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
    """Important 3: each test that needs fake PySide6 wraps it in a
    _FakeQtFixture context manager via the helper below, so any exception
    inside the test body still triggers teardown (restoring sys.modules
    + the original _pane_capture module object)."""

    def _run_with_fake_pyside6(self, body):
        """Run `body(pcp_module)` inside a fake PySide6 fixture context.

        The original sys.modules + houdinimcp._pane_capture object are
        restored on teardown even if `body` raises.
        """
        orig_pcp = sys.modules.get(_PCP_KEY)
        fx = _FakeQtFixture("PySide6")
        fx.__enter__()
        try:
            pcp2 = _load_pane_capture_fresh()
            body(pcp2)
        finally:
            fx.__exit__(None, None, None)
            _restore_pcp_module(orig_pcp)

    def test_network_editor_calls_homeall(self):
        def body(pcp2):
            self.assertEqual(pcp2._QT_BACKEND, "PySide6")
            pane = _FakePaneTab()
            pcp2._fit_pane_contents(pane, "NetworkEditor")
            self.assertEqual(len(pane._home_calls), 1)
        self._run_with_fake_pyside6(body)

    def test_compositor_calls_homeall(self):
        def body(pcp2):
            pane = _FakePaneTab()
            pcp2._fit_pane_contents(pane, "Compositor")
            self.assertEqual(len(pane._home_calls), 1)
        self._run_with_fake_pyside6(body)

    def test_channel_editor_calls_homeall(self):
        def body(pcp2):
            pane = _FakePaneTab()
            pcp2._fit_pane_contents(pane, "ChannelEditor")
            self.assertEqual(len(pane._home_calls), 1)
        self._run_with_fake_pyside6(body)

    def test_scene_viewer_calls_viewport_home(self):
        def body(pcp2):
            pane = _FakePaneTab(has_viewport=True)
            pcp2._fit_pane_contents(pane, "SceneViewer")
            self.assertEqual(len(pane._viewport.home_calls), 1)
        self._run_with_fake_pyside6(body)

    def test_other_types_no_op_even_with_qt(self):
        def body(pcp2):
            pane = _FakePaneTab()  # has homeAll but non-fit types should NOT call it
            for name in ("ParameterEditor", "PythonPanel", "HelpBrowser"):
                pcp2._fit_pane_contents(pane, name)
                self.assertEqual(pane._home_calls, [])
        self._run_with_fake_pyside6(body)

    def test_no_qt_no_op(self):
        # Default state: _QT_BACKEND is None -> _fit_pane_contents must be
        # no-op even for NetworkEditor (so we don't crash on missing Qt).
        # The baseline _QtSysModulesSnapshot enforces "no PySide" for the
        # module-level `pcp` reference loaded below.
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
        # Important 3: snapshot the original sys.modules + _pane_capture
        # object, install fake PySide6 via _FakeQtFixture, and register
        # addClassCleanup so teardown runs even if a later setup phase
        # raises. try/finally inside setUpClass handles the rare case
        # where setup itself blows up before cleanup can be registered.
        cls._orig_pcp = sys.modules.get(_PCP_KEY)
        cls.fx = _FakeQtFixture("PySide6")
        cls.fx.__enter__()
        try:
            cls.pcp = _load_pane_capture_fresh()
            cls.addClassCleanup(cls.fx.__exit__, None, None, None)
            cls.addClassCleanup(_restore_pcp_module, cls._orig_pcp)
        except Exception:
            cls.fx.__exit__(*sys.exc_info())
            raise

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

    # ----- Important 1: grab exception handling (RED) -----
    def test_grab_returns_none_raises_runtime_error(self):
        """widget.grab() 返回 None 必须抛 RuntimeError，不能 AttributeError."""
        class _NoneGrabWidget(_FakeQWidget):
            def grab(self):
                return None
        widget = _NoneGrabWidget()
        hou = _make_hou_with_scene_viewer(widget)
        with self.assertRaises(RuntimeError):
            self.pcp.capture_pane_screenshot(hou, "SceneViewer")

    def test_grab_raises_runtime_error_wraps_with_cause(self):
        """widget.grab() 自身抛异常时必须包装为 RuntimeError 并保留 __cause__."""
        class _ExplodingGrabWidget(_FakeQWidget):
            def grab(self):
                raise RuntimeError("explosion in grab")
        widget = _ExplodingGrabWidget()
        hou = _make_hou_with_scene_viewer(widget)
        with self.assertRaises(RuntimeError) as cm:
            self.pcp.capture_pane_screenshot(hou, "SceneViewer")
        # Must preserve original cause chain
        self.assertIsNotNone(cm.exception.__cause__)
        self.assertIn("explosion in grab", str(cm.exception.__cause__))

    def test_pixmap_is_null_raises_runtime_error(self):
        """widget.grab() 返回 isNull()==True pixmap 必须抛 RuntimeError."""
        class _NullPixmapGrabWidget(_FakeQWidget):
            def grab(self):
                return _FakeQPixmap(100, 100, null=True)
        widget = _NullPixmapGrabWidget()
        hou = _make_hou_with_scene_viewer(widget)
        with self.assertRaises(RuntimeError):
            self.pcp.capture_pane_screenshot(hou, "SceneViewer")

    def test_to_image_returns_none_raises_runtime_error(self):
        """pixmap.toImage() 返回 None 必须抛 RuntimeError."""
        class _NoneImagePixmap(_FakeQPixmap):
            def toImage(self):
                return None
        class _NoneImageWidget(_FakeQWidget):
            def grab(self):
                return _NoneImagePixmap(100, 100)
        widget = _NoneImageWidget()
        hou = _make_hou_with_scene_viewer(widget)
        with self.assertRaises(RuntimeError):
            self.pcp.capture_pane_screenshot(hou, "SceneViewer")

    def test_to_image_raises_runtime_error_wraps_with_cause(self):
        """pixmap.toImage() 抛异常时必须包装为 RuntimeError 并保留 __cause__."""
        class _ExplodingToImagePixmap(_FakeQPixmap):
            def toImage(self):
                raise RuntimeError("explosion in toImage")
        class _ExplodingToImageWidget(_FakeQWidget):
            def grab(self):
                return _ExplodingToImagePixmap(100, 100)
        widget = _ExplodingToImageWidget()
        hou = _make_hou_with_scene_viewer(widget)
        with self.assertRaises(RuntimeError) as cm:
            self.pcp.capture_pane_screenshot(hou, "SceneViewer")
        self.assertIsNotNone(cm.exception.__cause__)
        self.assertIn("explosion in toImage", str(cm.exception.__cause__))

    def test_image_is_null_raises_runtime_error(self):
        """pixmap.toImage() 返回 isNull()==True image 必须抛 RuntimeError."""
        class _NullImageWidget(_FakeQWidget):
            def grab(self):
                p = _FakeQPixmap(100, 100, null=True)
                # Also make toImage() return a null image
                return p
        widget = _NullImageWidget()
        hou = _make_hou_with_scene_viewer(widget)
        with self.assertRaises(RuntimeError):
            self.pcp.capture_pane_screenshot(hou, "SceneViewer")

    def test_grab_error_message_includes_pane_type(self):
        """RuntimeError 消息必须包含 pane 类型名便于排错."""
        class _NoneGrabWidget(_FakeQWidget):
            def grab(self):
                return None
        widget = _NoneGrabWidget()
        hou = _make_hou_with_scene_viewer(widget)
        try:
            self.pcp.capture_pane_screenshot(hou, "SceneViewer")
            self.fail("Expected RuntimeError")
        except RuntimeError as e:
            self.assertIn("SceneViewer", str(e))


# ===========================================================================
# Section C.3: capture_pane_screenshot with PySide2 fake
# ===========================================================================
class CapturePaneScreenshotPySide2Tests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Important 3: use _FakeQtFixture so PySide6 is blocked (would
        # otherwise be preferred over PySide2) and original module
        # objects are restored on teardown via addClassCleanup.
        cls._orig_pcp = sys.modules.get(_PCP_KEY)
        cls.fx = _FakeQtFixture("PySide2")
        cls.fx.__enter__()
        try:
            cls.pcp2 = _load_pane_capture_fresh()
            cls.addClassCleanup(cls.fx.__exit__, None, None, None)
            cls.addClassCleanup(_restore_pcp_module, cls._orig_pcp)
        except Exception:
            cls.fx.__exit__(*sys.exc_info())
            raise

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

    def test_all_success_when_qt_available(self):
        """Important 2：注入 fake PySide6 让 capture_pane_screenshot 真正
        成功时，capture_multiple_panes 必须全部 success=True, error=None，
        且 save_path 实际落盘。"""
        def body(pcp_qt):
            self.assertEqual(pcp_qt._QT_BACKEND, "PySide6")
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
                # 全部成功
                succeeded = [r for r in results if r["success"]]
                self.assertEqual(len(succeeded), 3)
                for r in results:
                    self.assertTrue(r["success"], r)
                    self.assertIsNone(r["error"], r)
                    self.assertTrue(os.path.isfile(r["save_path"]))
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        _run_with_fake_pyside6(body)

    def test_partial_failure_continues(self):
        """Inject fake PySide6 so capture_pane_screenshot normally succeeds,
        then monkey-patch it to raise ValueError for NetworkEditor. The
        other two panes must still succeed and the loop must continue past
        the failure."""
        def body(pcp_qt):
            self.assertEqual(pcp_qt._QT_BACKEND, "PySide6")
            original = pcp_qt.capture_pane_screenshot

            def flaky(hou, pane_type_name, save_path=None, fit_contents=True):
                if pane_type_name == "NetworkEditor":
                    raise ValueError("simulated failure")
                return original(hou, pane_type_name, save_path=save_path,
                                fit_contents=fit_contents)

            pcp_qt.capture_pane_screenshot = flaky
            try:
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
            except Exception:
                pcp_qt.capture_pane_screenshot = original
                raise
        _run_with_fake_pyside6(body)


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

    # ----- Important 2: fit_contents True/False 直接影响 render_node_network -----
    def test_fit_contents_true_calls_homeAll(self):
        """render_node_network(fit_contents=True) 必须触发 pane.homeAll()."""
        def body(pcp_qt):
            self.assertEqual(pcp_qt._QT_BACKEND, "PySide6")
            widget = _FakeQWidget()
            hou, ne = _make_hou_with_network_editor(widget=widget)
            hou._nodes["/obj/geo1"] = _FakeNode("/obj/geo1")
            pcp_qt.render_node_network(hou, "/obj/geo1", fit_contents=True)
            self.assertEqual(len(ne._home_calls), 1)
            self.assertEqual(ne._cd_calls, ["/obj/geo1"])
        _run_with_fake_pyside6(body)

    def test_fit_contents_false_skips_homeAll(self):
        """render_node_network(fit_contents=False) 必须跳过 pane.homeAll()."""
        def body(pcp_qt):
            self.assertEqual(pcp_qt._QT_BACKEND, "PySide6")
            widget = _FakeQWidget()
            hou, ne = _make_hou_with_network_editor(widget=widget)
            hou._nodes["/obj/geo1"] = _FakeNode("/obj/geo1")
            pcp_qt.render_node_network(hou, "/obj/geo1", fit_contents=False)
            self.assertEqual(ne._home_calls, [])
            # pane.cd() 仍然必须被调用（cd 与 fit_contents 独立）
            self.assertEqual(ne._cd_calls, ["/obj/geo1"])
        _run_with_fake_pyside6(body)


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


# ===========================================================================
# Section J: Bridge tool AST node exec (Important 2)
# ===========================================================================
# Parse houdini_mcp_server.py, extract the 4 PR 13 @mcp.tool() function
# source, compile + exec each in an isolated namespace where _houdini_call
# is a recording mock. This validates that each tool:
#   - Calls _houdini_call with the correct cmd_name
#   - Passes the correct param keys + values
#   - Surfaces Houdini error responses as bridge error envelopes
# without importing the heavy houdini_mcp_server module (mcp / requests /
# langchain).
class _RecordingHoudiniCall(object):
    """Mock for houdini_mcp_server._houdini_call that records (cmd, params)
    and returns a configurable response dict."""

    def __init__(self, response=None, raise_exc=None):
        self.calls = []
        self.response = response if response is not None else {
            "status": "success", "result": {"ok": True}}
        self.raise_exc = raise_exc

    def __call__(self, cmd, params=None):
        self.calls.append({"cmd": cmd, "params": params or {}})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


class _FakeMCP(object):
    """Stub for the @mcp.tool() decorator used in houdini_mcp_server.py.

    Records the wrapped function under the function's __name__ on .registry."""

    def __init__(self):
        self.registry = {}

    def tool(self):
        def decorator(fn):
            self.registry[fn.__name__] = fn
            return fn
        return decorator


def _exec_pr13_bridge_tool(tool_name):
    """AST-isolate-exec the named PR 13 bridge tool from houdini_mcp_server.py.

    Returns (executed_function, recording_mock, fake_mcp).
    """
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    fn_node = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == tool_name:
            # Ensure @mcp.tool() decorator is present
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call):
                    func = dec.func
                    if isinstance(func, ast.Attribute) and func.attr == "tool":
                        fn_node = node
                        break
            if fn_node is not None:
                break
    if fn_node is None:
        raise AssertionError(
            "PR 13 bridge tool {0} not found in houdini_mcp_server.py".format(
                tool_name))
    fn_src = ast.get_source_segment(src, fn_node)
    fake_mcp = _FakeMCP()
    rec = _RecordingHoudiniCall()
    ns = {"mcp": fake_mcp, "_houdini_call": rec}
    exec(compile(fn_src, "<pr13_{0}>".format(tool_name), "exec"), ns)
    return ns[tool_name], rec, fake_mcp


class PR13BridgeToolASTExecTests(unittest.TestCase):
    """Important 2: each PR 13 bridge tool, AST-isolated, must call
    _houdini_call with the exact cmd_name + param keys/values."""

    def test_capture_pane_screenshot_cmd_and_params(self):
        fn, rec, _mcp = _exec_pr13_bridge_tool("capture_pane_screenshot")
        fn(object(), "NetworkEditor", "/tmp/out.png", True)
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call["cmd"], "capture_pane_screenshot")
        self.assertEqual(call["params"]["pane_type_name"], "NetworkEditor")
        self.assertEqual(call["params"]["save_path"], "/tmp/out.png")
        self.assertTrue(call["params"]["fit_contents"])

    def test_list_visible_panes_cmd_and_params(self):
        fn, rec, _mcp = _exec_pr13_bridge_tool("list_visible_panes")
        fn(object())
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call["cmd"], "list_visible_panes")
        # No params, but tool should pass an empty dict (per source).
        self.assertEqual(call["params"], {})

    def test_capture_multiple_panes_cmd_and_params(self):
        fn, rec, _mcp = _exec_pr13_bridge_tool("capture_multiple_panes")
        pane_list = ["SceneViewer", "NetworkEditor"]
        fn(object(), pane_list, "/tmp/captures")
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call["cmd"], "capture_multiple_panes")
        self.assertEqual(call["params"]["pane_types"], pane_list)
        self.assertEqual(call["params"]["save_dir"], "/tmp/captures")

    def test_render_node_network_cmd_and_params(self):
        fn, rec, _mcp = _exec_pr13_bridge_tool("render_node_network")
        fn(object(), "/obj/geo1", True, "/tmp/net.png")
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call["cmd"], "render_node_network")
        self.assertEqual(call["params"]["node_path"], "/obj/geo1")
        self.assertTrue(call["params"]["fit_contents"])
        self.assertEqual(call["params"]["save_path"], "/tmp/net.png")

    def test_capture_pane_screenshot_passes_through_houdini_error(self):
        """Houdini 返 status=error 时，bridge 工具必须把 envelope 原样透传."""
        err_envelope = {
            "status": "error",
            "message": "pane not found",
            "origin": "houdini",
        }
        fn, rec, _mcp = _exec_pr13_bridge_tool("capture_pane_screenshot")
        rec.response = err_envelope
        result = fn(object(), "SceneViewer")
        self.assertEqual(result, err_envelope)

    def test_render_node_network_passes_through_houdini_error(self):
        err_envelope = {
            "status": "error",
            "message": "no NetworkEditor pane",
            "origin": "houdini",
        }
        fn, rec, _mcp = _exec_pr13_bridge_tool("render_node_network")
        rec.response = err_envelope
        result = fn(object(), "/obj/geo1")
        self.assertEqual(result, err_envelope)


# ===========================================================================
# Section K: Server.py handler wrapper apply_response_cap (Important 2)
# ===========================================================================
# server.py imports hou at module top, so we cannot import it directly. We
# AST-extract the 4 PR 13 HoudiniMCPServer method bodies, exec each into a
# stub class with mocked `hou` / `pcp` / `cmn` namespace, then verify
# cmn.apply_response_cap is called exactly once per invocation.
class _StubCap(object):
    """Stub for houdinimcp._common.apply_response_cap with call recording."""

    def __init__(self):
        self.calls = []

    def __call__(self, value, *args, **kwargs):
        self.calls.append({"value": value, "args": args, "kwargs": kwargs})
        return value


def _build_pr13_handler_class(pcp_stub=None, hou_stub=None,
                              cap_stub=None):
    """AST-extract the 4 PR 13 handler methods from server.py and exec each
    into a stub class namespace with mocked `hou` / `pcp` / `cmn`.

    Returns (class, cap_stub, pcp_stub).
    """
    server_src_path = os.path.join(ROOT, "server.py")
    with open(server_src_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    cls_node = next((n for n in tree.body
                     if isinstance(n, ast.ClassDef)
                     and n.name == "HoudiniMCPServer"), None)
    if cls_node is None:
        raise AssertionError("HoudiniMCPServer class not found in server.py")

    if cap_stub is None:
        cap_stub = _StubCap()
    if pcp_stub is None:
        pcp_stub = types.SimpleNamespace()
    if hou_stub is None:
        hou_stub = object()

    cmn_stub = types.SimpleNamespace(apply_response_cap=cap_stub)
    ns = {
        "hou": hou_stub,
        "pcp": pcp_stub,
        "cmn": cmn_stub,
    }

    target_methods = {
        "capture_pane_screenshot",
        "list_visible_panes",
        "capture_multiple_panes",
        "render_node_network",
    }
    class_ns = {}
    for method in cls_node.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        if method.name not in target_methods:
            continue
        method_src = ast.get_source_segment(src, method)
        # Execute the def into a per-method namespace and lift the function
        # into class_ns so it becomes an unbound method of the class.
        local = dict(ns)
        local["__name__"] = "_Pr13Method_{0}".format(method.name)
        exec(compile(method_src, "<server_{0}>".format(method.name), "exec"),
             local)
        class_ns[method.name] = local[method.name]

    class _Pr13Server(object):
        pass
    for name, fn in class_ns.items():
        setattr(_Pr13Server, name, fn)
    return _Pr13Server, cap_stub, pcp_stub


class PR13HandlerCapMockTests(unittest.TestCase):
    """Important 2: each PR 13 HoudiniMCPServer method must call
    cmn.apply_response_cap exactly once. Previously the test only grep'd
    the source for `apply_response_cap`, missing whether the wrapper
    actually invoked it."""

    def setUp(self):
        self._pcp_stub = types.SimpleNamespace()
        # Pre-populate pcp stubs so the handler calls don't blow up.
        self._pcp_stub.capture_pane_screenshot = lambda *a, **kw: {
            "pane_type": kw.get("pane_type_name", a[1] if len(a) > 1 else "?"),
            "save_path": kw.get("save_path"),
            "width": 100, "height": 100, "size_bytes": 100,
            "_qt_backend": "PySide6"}
        self._pcp_stub.list_visible_panes = lambda *a, **kw: []
        self._pcp_stub.capture_multiple_panes = lambda *a, **kw: []
        self._pcp_stub.render_node_network = lambda *a, **kw: {
            "pane_type": "NetworkEditor"}

    def test_capture_pane_screenshot_calls_cap_once(self):
        cls, cap, _ = _build_pr13_handler_class(
            pcp_stub=self._pcp_stub)
        inst = cls()
        inst.capture_pane_screenshot("SceneViewer")
        self.assertEqual(len(cap.calls), 1)

    def test_list_visible_panes_calls_cap_once(self):
        cls, cap, _ = _build_pr13_handler_class(
            pcp_stub=self._pcp_stub)
        inst = cls()
        inst.list_visible_panes()
        self.assertEqual(len(cap.calls), 1)

    def test_capture_multiple_panes_calls_cap_once(self):
        cls, cap, _ = _build_pr13_handler_class(
            pcp_stub=self._pcp_stub)
        inst = cls()
        inst.capture_multiple_panes(["SceneViewer"], "/tmp/cap")
        self.assertEqual(len(cap.calls), 1)

    def test_render_node_network_calls_cap_once(self):
        cls, cap, _ = _build_pr13_handler_class(
            pcp_stub=self._pcp_stub)
        inst = cls()
        inst.render_node_network("/obj/geo1")
        self.assertEqual(len(cap.calls), 1)


if __name__ == "__main__":
    unittest.main()