"""H20 best-effort fallback smoke test (opera-houdinimcp-h21-compat Bug 1, task 5.7).

SideFX H20 hou.PaneTab 仍有 qtWidget() 方法（SideFX H22 文档仅删 qtWidget
未删 qtScreenGeometry，但 H20 实际仅 qtWidget 可用）。User spec：
- H20 best-effort：hasattr(pane, "qtWidget") and callable(...) 才走老路径
- widget.grab() 成功 → _renderer="qtWidget_fallback_H20"
- widget.grab() 返 None / 抛异常 → 降级到 QScreen.grabWindow
  → _renderer="qtScreenGeometry_fallback_H20_widget_None"

本文件只测 smoke：H20 mock 能正常 import + capture_pane_screenshot 不崩。
**不覆盖完整 H20 功能**（spec 明确 best-effort, untested）。

执行入口：
    cd external/houdinimcp
    python -m pytest tests/test_pane_capture_h20_smoke.py -v
"""
import importlib.util as _ilu
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_PKG_KEY = "houdinimcp"
_PCP_KEY = "houdinimcp._pane_capture"


def _ensure_pkg():
    if _PKG_KEY not in sys.modules:
        pkg = types.ModuleType(_PKG_KEY)
        pkg.__path__ = [ROOT]
        sys.modules[_PKG_KEY] = pkg
    if "houdinimcp._common" not in sys.modules:
        spec = _ilu.spec_from_file_location(
            "houdinimcp._common", os.path.join(ROOT, "_common.py"))
        mod = _ilu.module_from_spec(spec)
        sys.modules["houdinimcp._common"] = mod
        spec.loader.exec_module(mod)


def _load_pane_capture_fresh():
    if _PCP_KEY in sys.modules:
        del sys.modules[_PCP_KEY]
    spec = _ilu.spec_from_file_location(
        _PCP_KEY, os.path.join(ROOT, "_pane_capture.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[_PCP_KEY] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# H20 mocks
# ---------------------------------------------------------------------------
class _FakeQRect(object):
    def __init__(self, x, y, w, h):
        self._x, self._y, self._w, self._h = x, y, w, h

    def x(self):
        return self._x

    def y(self):
        return self._y

    def width(self):
        return self._w

    def height(self):
        return self._h


class _FakeScreen(object):
    def __init__(self):
        self.grabWindow_calls = []

    def grabWindow(self, wid, x, y, w, h):
        self.grabWindow_calls.append((wid, x, y, w, h))
        from tests.test_pane_capture import _FakeQPixmap
        return _FakeQPixmap(w, h)


class _FakeQApplication(object):
    _instance = None

    @classmethod
    def instance(cls):
        return cls._instance

    def __init__(self, *args):
        _FakeQApplication._instance = self
        self._screen = _FakeScreen()

    def primaryScreen(self):
        return self._screen


class _FakeH20Widget(object):
    """H20 风格的 Qt widget — 有 grab() 方法。"""

    def __init__(self, w=1024, h=768):
        self._w = w
        self._h = h
        self.grab_calls = 0

    def grab(self):
        self.grab_calls += 1
        from tests.test_pane_capture import _FakeQPixmap
        return _FakeQPixmap(self._w, self._h)


class _FakeH20PaneTab(object):
    """H20 hou.PaneTab stub — 同时有 qtWidget() 和 qtScreenGeometry()。

    H20 实际同时存在两者（H22 文档均保留），但用户 H20 走 widget.grab()。
    """

    def __init__(self, widget=None, w=1024, h=768):
        self._widget = widget if widget is not None else _FakeH20Widget(w, h)
        self._geom = _FakeQRect(0, 0, w, h)
        self._home_calls = []

    def homeAll(self):
        self._home_calls.append(True)

    def qtWidget(self):
        return self._widget

    def qtScreenGeometry(self):
        return self._geom


class _FakeUI(object):
    def __init__(self, pane_tabs_by_type=None):
        self._by_type = pane_tabs_by_type or {}

    def paneTabOfType(self, pane_type):
        return self._by_type.get(pane_type)


class _FakePaneTabType(object):
    NetworkEditor = "NetworkEditor"


class _FakeHouH20(object):
    def __init__(self, pane_tabs_by_type=None):
        self.paneTabType = _FakePaneTabType()
        self.ui = _FakeUI(pane_tabs_by_type=pane_tabs_by_type)

    def frame(self):
        return 1


class _FakeQtEnvH20(object):
    """H20 兼容 PySide6 注入。"""

    _QT_KEYS = ("PySide6", "PySide2", "PySide6.QtCore", "PySide6.QtGui",
                "PySide6.QtWidgets", "PySide2.QtCore", "PySide2.QtGui",
                "PySide2.QtWidgets")

    def __init__(self):
        self._saved = {}
        for k in self._QT_KEYS:
            if k in sys.modules:
                self._saved[k] = sys.modules[k]

    def __enter__(self):
        from tests.test_pane_capture import (
            _FakeQBuffer, _FakeQIODevice, _FakeQImage)
        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice
        fake_qtgui = types.ModuleType("PySide6.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtwidgets = types.ModuleType("PySide6.QtWidgets")
        fake_qtwidgets.QApplication = _FakeQApplication
        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets
        for k in self._QT_KEYS:
            sys.modules.pop(k, None)
        sys.modules["PySide6"] = fake_pkg
        sys.modules["PySide6.QtCore"] = fake_qtcore
        sys.modules["PySide6.QtGui"] = fake_qtgui
        sys.modules["PySide6.QtWidgets"] = fake_qtwidgets
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for k in self._QT_KEYS:
            sys.modules.pop(k, None)
        for k, v in self._saved.items():
            sys.modules[k] = v
        _FakeQApplication._instance = None
        return False


# ===========================================================================
# Tests
# ===========================================================================
class H20PaneCaptureSmokeTests(unittest.TestCase):
    """H20 best-effort fallback — 仅 smoke（不覆盖完整功能）。"""

    def setUp(self):
        _ensure_pkg()
        self._orig_pcp = sys.modules.get(_PCP_KEY)
        self._env = _FakeQtEnvH20()
        self._env.__enter__()
        self.pcp = _load_pane_capture_fresh()

    def tearDown(self):
        if self._orig_pcp is None:
            sys.modules.pop(_PCP_KEY, None)
        else:
            sys.modules[_PCP_KEY] = self._orig_pcp
        self._env.__exit__(None, None, None)

    def test_h20_smoke_qtwidget_fallback(self):
        """H20 mock（有 qtWidget）→ capture_pane_screenshot 不崩，
        返回 dict 含 _renderer 字段（qtWidget_fallback_H20 或
        qtScreenGeometry_fallback_H20_widget_None）。"""
        widget = _FakeH20Widget(w=1024, h=768)
        pane = _FakeH20PaneTab(widget=widget)
        hou = _FakeHouH20(pane_tabs_by_type={
            _FakePaneTabType.NetworkEditor: pane})
        # 不应抛异常（best-effort 不阻断 import / 调用）
        result = self.pcp.capture_pane_screenshot(
            hou, "NetworkEditor", save_path=None)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["pane_type"], "NetworkEditor")
        self.assertEqual(result["width"], 1024)
        self.assertEqual(result["height"], 768)
        # _renderer 必须存在（H21+ 三态标记）
        self.assertIn("_renderer", result)
        self.assertIn(result["_renderer"], (
            "qtWidget_fallback_H20",
            "qtScreenGeometry_QScreen_grabWindow",
            "qtScreenGeometry_fallback_H20_widget_None",
        ), "H20 smoke：_renderer 必须是已知三态之一")


if __name__ == "__main__":
    unittest.main()