"""Unit tests for H21+H22 pane capture path (opera-houdinimcp-h21-compat Bug 1).

SideFX 文档依据（2026-07-21 验证）：
- hou.PaneTab.qtScreenGeometry() → PySide6.QtCore.QRect（H21+ API，H22 文档确认）
- H21+ 中 hou.PaneTab.qtWidget() 已移除（hasattr 返 False）
- QScreen.grabWindow(WId window=0, int x=0, int y=0, int width=-1, int height=-1)
  → QPixmap（Qt 官方文档）

测试目标：
- H21 hou mock（无 qtWidget 方法，有 qtScreenGeometry）→ 走 QScreen.grabWindow
  主路径，返回 _renderer="qtScreenGeometry_QScreen_grabWindow"
- 不抛 AttributeError，不依赖 OGL 3.3
- H20 best-effort fallback（仅 smoke，详见 test_pane_capture_h20_smoke.py）

执行入口：
    cd external/houdinimcp
    python -m pytest tests/test_pane_capture_h21.py -v
"""
import importlib.util as _ilu
import os
import sys
import tempfile
import shutil
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_PKG_KEY = "houdinimcp"
_PCP_KEY = "houdinimcp._pane_capture"


# ---------------------------------------------------------------------------
# pkg + _common 注入（沿用 test_pane_capture.py 的范式）
# ---------------------------------------------------------------------------
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
# Qt 注入辅助（与 test_pane_capture.py 的 _FakeQtFixture 类似，但支持 QApplication
# 注入 fake QScreen.grabWindow 路径）。完全独立避免与 test_pane_capture.py
# 的全局 _BASELINE_NO_QT 冲突。
# ---------------------------------------------------------------------------
class _FakeQRect(object):
    """PySide6.QtCore.QRect stub — x/y/width/height 方法。"""

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
    """PySide6.QtGui.QScreen stub — grabWindow 记录调用并返 fake QPixmap。"""

    def __init__(self, w=1024, h=768):
        self._w = w
        self._h = h
        self.grabWindow_calls = []

    def grabWindow(self, wid, x, y, w, h):
        self.grabWindow_calls.append((wid, x, y, w, h))
        # 返回与请求 w/h 匹配的 fake pixmap
        from tests.test_pane_capture import _FakeQPixmap
        return _FakeQPixmap(w if w > 0 else self._w,
                            h if h > 0 else self._h)


class _FakeQApplication(object):
    """PySide6.QtWidgets.QApplication stub — instance() + primaryScreen()。"""

    _instance = None

    @classmethod
    def instance(cls):
        return cls._instance

    def __init__(self, *args):
        _FakeQApplication._instance = self
        self._screen = _FakeScreen()

    def primaryScreen(self):
        return self._screen


class _FakePaneTabH21(object):
    """H21 hou.PaneTab stub — 无 qtWidget 方法（H21 已移除）。"""

    def __init__(self, name="PaneTab1", x=0, y=0, w=1024, h=768):
        self._name = name
        self._home_calls = []
        # Bug 1 H21：qtScreenGeometry 返 QRect（SideFX H22 文档）
        self._geom = _FakeQRect(x, y, w, h)
        # 不实现 qtWidget — hasattr 返 False 模拟 H21 真实行为

    def name(self):
        return self._name

    def homeAll(self):
        self._home_calls.append(True)

    def qtScreenGeometry(self):
        return self._geom


class _FakePaneTabH21WithText(object):
    """H21 hou.PaneTab + hou.text stub — future-proof 测试。

    hou.text 属性存在但当前实现不使用（仅记录以确保未来扩展性）。
    截屏路径不应触碰 hou.text（Bug 1 不涉及 hou.text.expandString）。
    """

    def __init__(self, x=100, y=50, w=800, h=600):
        self._geom = _FakeQRect(x, y, w, h)

    def homeAll(self):
        pass

    def qtScreenGeometry(self):
        return self._geom

    def text(self):
        """H22 hou.text 命名空间 — 当前实现不应触碰。"""
        return types.SimpleNamespace(expandString=lambda s: s)


class _FakeUI(object):
    def __init__(self, pane_tabs_by_type=None):
        self._by_type = pane_tabs_by_type or {}

    def paneTabOfType(self, pane_type):
        return self._by_type.get(pane_type)


class _FakePaneTabType(object):
    NetworkEditor = "NetworkEditor"
    SceneViewer = "SceneViewer"


class _FakeHouH21(object):
    """H21 hou stub — paneTabType 完整，ui.paneTabOfType 按类型分发。"""

    def __init__(self, pane_tabs_by_type=None, with_text=False):
        self.paneTabType = _FakePaneTabType()
        self.ui = _FakeUI(pane_tabs_by_type=pane_tabs_by_type)
        self._with_text = with_text
        if with_text:
            # H21+ 通常有 hou.text（H22 主路径，H21 多数版本已包含）
            self.text = types.SimpleNamespace(
                expandString=lambda s: s)

    def frame(self):
        return 1


class _FakeQtEnvH21(object):
    """注入 fake PySide6 模块：QtWidgets.QApplication + QtGui.QPixmap
    + QtCore.QBuffer（让 _pane_capture._QT_BACKEND = "PySide6"）。

    用法：
        env = _FakeQtEnvH21()
        env.__enter__()
        try:
            pcp = _load_pane_capture_fresh()
            # ... 测试
        finally:
            env.__exit__(None, None, None)
    """

    _QT_KEYS = ("PySide6", "PySide2", "PySide6.QtCore", "PySide6.QtGui",
                "PySide6.QtWidgets", "PySide2.QtCore", "PySide2.QtGui",
                "PySide2.QtWidgets")

    def __init__(self):
        # 保存已有 Qt 模块（如果有）以便恢复
        self._saved = {}
        for k in self._QT_KEYS:
            if k in sys.modules:
                self._saved[k] = sys.modules[k]

    def __enter__(self):
        # 复用 test_pane_capture.py 的 fake pixmap/image/buffer 类
        from tests.test_pane_capture import (
            _FakeQBuffer, _FakeQIODevice, _FakeQImage, _FakeQPixmap)
        # 注：_FakeQPixmap 必须先 import 才能被 _FakeScreen 用
        _FakeScreen.__module__ = __name__  # 强制使用本地 _FakeScreen

        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_qtcore.QBuffer = _FakeQBuffer
        fake_qtcore.QIODevice = _FakeQIODevice

        fake_qtgui = types.ModuleType("PySide6.QtGui")
        fake_qtgui.QImage = _FakeQImage
        fake_qtgui.QPixmap = _FakeQPixmap

        fake_qtwidgets = types.ModuleType("PySide6.QtWidgets")
        fake_qtwidgets.QApplication = _FakeQApplication
        from tests.test_pane_capture import _FakeQWidget
        fake_qtwidgets.QWidget = _FakeQWidget

        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        fake_pkg.QtGui = fake_qtgui
        fake_pkg.QtWidgets = fake_qtwidgets

        # 先 pop 已有（避免 import cache 命中 host 真实 PySide6）
        for k in self._QT_KEYS:
            sys.modules.pop(k, None)

        sys.modules["PySide6"] = fake_pkg
        sys.modules["PySide6.QtCore"] = fake_qtcore
        sys.modules["PySide6.QtGui"] = fake_qtgui
        sys.modules["PySide6.QtWidgets"] = fake_qtwidgets
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 清理 fake
        for k in self._QT_KEYS:
            sys.modules.pop(k, None)
        # 恢复原模块
        for k, v in self._saved.items():
            sys.modules[k] = v
        # 重置 _FakeQApplication._instance
        _FakeQApplication._instance = None
        return False


# ===========================================================================
# Tests
# ===========================================================================
class H21PaneCaptureTests(unittest.TestCase):
    """Bug 1 H21 主路径：qtScreenGeometry + QScreen.grabWindow."""

    def setUp(self):
        _ensure_pkg()
        # 先存原模块
        self._orig_pcp = sys.modules.get(_PCP_KEY)
        # 注入 fake Qt
        self._env = _FakeQtEnvH21()
        self._env.__enter__()
        # reload _pane_capture 注入 fake PySide6
        self.pcp = _load_pane_capture_fresh()

    def tearDown(self):
        # 还原 _pane_capture 模块对象
        if self._orig_pcp is None:
            sys.modules.pop(_PCP_KEY, None)
        else:
            sys.modules[_PCP_KEY] = self._orig_pcp
        # 还原 fake Qt
        self._env.__exit__(None, None, None)

    # ---- Test 1: qtScreenGeometry returns correct QRect → grabWindow args ----
    def test_h21_qtscreen_geometry_returns_correct_rect(self):
        """H21 qtScreenGeometry 返 QRect(100, 50, 800, 600) 时，截屏必须
        调 primaryScreen().grabWindow(0, 100, 50, 800, 600)。"""
        pane = _FakePaneTabH21(x=100, y=50, w=800, h=600)
        hou = _FakeHouH21(pane_tabs_by_type={
            _FakePaneTabType.NetworkEditor: pane})
        tmpdir = tempfile.mkdtemp()
        try:
            save_path = os.path.join(tmpdir, "out.png")
            result = self.pcp.capture_pane_screenshot(
                hou, "NetworkEditor", save_path=save_path)
            self.assertEqual(result["pane_type"], "NetworkEditor")
            self.assertEqual(result["width"], 800)
            self.assertEqual(result["height"], 600)
            self.assertTrue(os.path.isfile(save_path))
            # 验证 QApplication 已建 + grabWindow 被调用
            self.assertIsNotNone(_FakeQApplication._instance,
                "QApplication.instance() 必须非 None（自动 new）")
            screen = _FakeQApplication._instance.primaryScreen()
            self.assertEqual(len(screen.grabWindow_calls), 1,
                "primaryScreen().grabWindow 必须被调一次")
            wid, x, y, w, h = screen.grabWindow_calls[0]
            self.assertEqual(wid, 0)
            self.assertEqual(x, 100)
            self.assertEqual(y, 50)
            self.assertEqual(w, 800)
            self.assertEqual(h, 600)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- Test 2: _renderer marker == "qtScreenGeometry_QScreen_grabWindow" ----
    def test_h21_renderer_marker_qtscreen(self):
        """H21 主路径必须返 _renderer='qtScreenGeometry_QScreen_grabWindow'。"""
        pane = _FakePaneTabH21()
        hou = _FakeHouH21(pane_tabs_by_type={
            _FakePaneTabType.NetworkEditor: pane})
        result = self.pcp.capture_pane_screenshot(
            hou, "NetworkEditor", save_path=None)
        self.assertIn("_renderer", result,
            "返回 dict 必须含 _renderer 字段（H21+ 三态标记）")
        self.assertEqual(result["_renderer"],
            "qtScreenGeometry_QScreen_grabWindow",
            "H21 无 qtWidget 时必须走 QScreen 主路径")

    # ---- Test 3: hou 有 text 属性但不触碰（future-proof）----
    def test_h21_hou_with_text_attribute_unused(self):
        """hou.text 存在但 Bug 1 不使用 — 截屏仍走 QScreen 路径，不抛异常。"""
        pane = _FakePaneTabH21WithText()
        hou = _FakeHouH21(pane_tabs_by_type={
            _FakePaneTabType.NetworkEditor: pane},
            with_text=True)
        result = self.pcp.capture_pane_screenshot(
            hou, "NetworkEditor", save_path=None)
        self.assertEqual(result["pane_type"], "NetworkEditor")
        self.assertEqual(result["width"], 800)
        self.assertEqual(result["height"], 600)
        self.assertEqual(result["_renderer"],
            "qtScreenGeometry_QScreen_grabWindow")

    # ---- Test 4: H21 pane 无 qtWidget 方法 — 不抛 AttributeError ----
    def test_h21_no_qtwidget_method(self):
        """H21 pane 完全无 qtWidget（hasattr 返 False）— 截屏不抛 AttributeError。"""
        pane = _FakePaneTabH21()
        # 强制确认无 qtWidget 属性
        self.assertFalse(hasattr(pane, "qtWidget"),
            "H21 mock 必须无 qtWidget 属性（SideFX H21+ 已移除）")
        hou = _FakeHouH21(pane_tabs_by_type={
            _FakePaneTabType.NetworkEditor: pane})
        # 不应抛 AttributeError
        result = self.pcp.capture_pane_screenshot(
            hou, "NetworkEditor", save_path=None)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["_renderer"],
            "qtScreenGeometry_QScreen_grabWindow")


if __name__ == "__main__":
    unittest.main()