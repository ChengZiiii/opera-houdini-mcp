"""回归测试 Bug B：_pane_capture.py SceneViewer 必须走 hou.SceneViewer.flipbook()。

现象（用户 2026-07-21 实机验证）：
    - SideFX 文档称 flipbook 需 Vulkan 兼容显卡
    - 实测 hou.SceneViewer.flipbook() 在不升级 GPU 驱动前提下
      FlipbookSettings.beautyPassOnly(True) + 单帧 frameRange((1,1)) +
      output("C:/temp/xxx.png") + outputToMPlay(False) 产出 png 不崩
    - 原 widget.grab() 路径在 H21 缺 OGL 3.3 时直接 GUI Fatal Error
    - 任何 Qt grab 回退都不允许（GPU 崩就让它报错）

修复目标（user spec）：
    - SceneViewer 分支调 scene_viewer.flipbook(settings) 而非 widget.grab()
    - settings 参数：beautyPassOnly=True, frameRange((f,f)) 单帧,
      output(path_template) 用 $F4 占位, outputToMPlay(False)
    - 视口相机从 scene_viewer.curViewport().camera() 取，不新建 cam 节点
    - 失败不回退 Qt grab，raise + 返回 _renderer 标记 + 错误信息
    - 返回结构加 _renderer: "flipbook_via_Houdini_internal"
    - NetworkEditor 等 30 种其他 pane 保留原 Qt grab 路径
"""
import importlib.util as _ilu
import os
import sys
import struct
import tempfile
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 复用 test_pane_capture.py 的 sys.modules 注入范式
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
# Fakes
# ---------------------------------------------------------------------------
class _FakeFlipbookSettings(object):
    """Mock hou.FlipbookSettings — SideFX 官方用方法调用而非属性赋值。

    settings.frameRange((f,f)) / settings.beautyPassOnly(True) /
    settings.output(filename) / settings.outputToMPlay(False) — 都
    是方法调用。Mock 拦截 __getattr__ 时返回当前保存的值（如果已
    设置过）或一个 setter 函数（第一次访问时）。这样既支持测试
    断言 getattr(settings, "frameRange") 获取最后保存值，也支持
    production code 调用 settings.frameRange((f,f)) 触发 setter。
    """
    def __init__(self):
        self._attrs = {}
        self.calls = []

    def __getattr__(self, name):
        # Python 内部 dunder / private 属性
        if name.startswith("_") or name == "calls":
            raise AttributeError(name)
        # 已设置过的值：返回该值（getter 语义）
        if name in self._attrs:
            return self._attrs[name]
        # 未设置：返回一个 setter 函数
        def _setter(*args, **kwargs):
            val = args[0] if len(args) == 1 else args
            self._attrs[name] = val
            self.calls.append((name, args))
            return None
        return _setter

    def __setattr__(self, name, value):
        if name in ("_attrs", "calls"):
            super().__setattr__(name, value)
        else:
            # 允许属性赋值（兼容旧 mock 行为）
            self._attrs[name] = value

    def stash(self):
        """H21 hou.FlipbookSettings.stash() — 返回当前 settings 副本。

        测试场景：生产代码用 `pane.flipbookSettings().stash()`，
        mock 直接返回 self（同一实例）便于断言。
        """
        return self


class _FakeViewport(object):
    def __init__(self, camera):
        self._camera = camera

    def home(self):
        pass

    def camera(self):
        return self._camera


class _FakeCamera(object):
    def __init__(self, name="cam1"):
        self.name = name


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


class _FakeSceneViewerPane(object):
    """Mock SceneViewer pane，flipbook / curViewport / qtWidget 全部可探测。"""
    def __init__(self, camera=None, raise_on_flipbook=False,
                 output_mode="valid", output_width=320, output_height=180):
        self.flipbook_calls = []
        self.raise_on_flipbook = raise_on_flipbook
        self.output_mode = output_mode
        self.output_width = output_width
        self.output_height = output_height
        self._camera = camera or _FakeCamera("viewport_cam")
        self._viewport = _FakeViewport(self._camera)
        # 关键：qtWidget 必须存在（说明 Houdini 端有 GUI），但调用 grab
        # 时抛 OGL 异常，模拟用户机器无 OGL 3.3 的情况。
        self._widget = _FakeWidget(raise_on_grab=True)
        # H21 flipbookSettings() 工厂方法返回 settings 实例（H21 抽象类
        # 必须经此方法获取，stash() 返回副本）
        self._flipbook_settings = _FakeFlipbookSettings()
        # Bug 1 H21+H22：qtScreenGeometry stub（SideFX H22 文档）。
        # SceneViewer 走 flipbook 路径不会触达此处，但加方法避免未来重构
        # 或 H21 实际调用时的 AttributeError（spec task 5.2）。
        self._geom = _FakeQRect(0, 0, 1024, 768)

    def curViewport(self):
        return self._viewport

    def qtWidget(self):
        return self._widget

    def qtScreenGeometry(self):
        # Bug 1 H21+H22：SceneViewer 路径短路到 flipbook，不触达此方法。
        # 仍 stub 返回避免任何 AttributeError。
        return self._geom

    def flipbookSettings(self):
        # H21 hou.SceneViewer.flipbookSettings() 返回 viewer 当前 settings
        # 测试返回同一实例（stash() 在 production code 内调用）
        return self._flipbook_settings

    def flipbook(self, *args, **kwargs):
        # H21 实测兼容多种签名：(settings) / (viewport, settings) /
        # vp.flipbook(settings) — 测试 mock 接受任意参数。
        settings_arg = None
        for a in args:
            # FlipbookSettings 方法调用 / 属性赋值都会被 mock 接受
            # 通过 hasattr 检查可能命中方法名（如 'outputToMPlay'）
            # 跳过这些，只匹配真实 settings 实例
            if hasattr(a, "_attrs") or (hasattr(a, "calls") and hasattr(a, "_attrs")):
                settings_arg = a
                break
        if settings_arg is not None:
            self.flipbook_calls.append(settings_arg)
        if self.raise_on_flipbook:
            raise RuntimeError("simulated flipbook failure")
        if settings_arg is None:
            return None
        output = getattr(settings_arg, "output", None)
        frame_range = getattr(settings_arg, "frameRange", (1, 1))
        if not output or "$F4" not in output:
            return None
        frame = frame_range[0] if isinstance(frame_range, (tuple, list)) else 1
        actual_path = output.replace("$F4", str(int(frame)).zfill(4))
        if self.output_mode == "missing":
            return None
        if self.output_mode == "zero_bytes":
            with open(actual_path, "wb"):
                pass
            return None
        if self.output_mode == "zero_ihdr":
            width, height = 0, 0
        elif self.output_mode == "invalid_png":
            with open(actual_path, "wb") as handle:
                handle.write(b"not-a-png")
            return None
        else:
            width, height = self.output_width, self.output_height
        payload = struct.pack(">II", width, height)
        payload += b"\x08\x02\x00\x00\x00"
        png = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13)
               + b"IHDR" + payload + b"\x00\x00\x00\x00")
        with open(actual_path, "wb") as handle:
            handle.write(png)


class _FakeWidget(object):
    def __init__(self, raise_on_grab=False):
        self._raise = raise_on_grab
        self.grab_calls = 0

    def grab(self):
        self.grab_calls += 1
        if self._raise:
            raise RuntimeError(
                "OpenGL 3.3 not available (simulated user env)")
        return _FakePixmap()


class _FakePixmap(object):
    def isNull(self):
        return False

    def toImage(self):
        return _FakeImage()

    def width(self):
        return 100

    def height(self):
        return 100


class _FakeImage(object):
    def isNull(self):
        return False

    def save(self, *args, **kwargs):
        return True

    def width(self):
        return 100

    def height(self):
        return 100


class _FakeHou(object):
    def __init__(self, scene_viewer=None, qt_backend="PySide6"):
        self.frame_value = 1
        self._sv = scene_viewer or _FakeSceneViewerPane()
        self._qt_backend = qt_backend
        # paneTabType.SceneViewer 标记
        self.paneTabType = types.SimpleNamespace(
            SceneViewer=object(),
            NetworkEditor=object(),
        )
        # ui.paneTabOfType
        self.ui = types.SimpleNamespace(
            paneTabOfType=lambda t: self._sv,
        )

    def frame(self):
        return self.frame_value


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class SceneViewerFlipbookTest(unittest.TestCase):
    """Bug B 核心：SceneViewer 必须走 flipbook 而不是 widget.grab()。"""

    @classmethod
    def setUpClass(cls):
        _ensure_pkg()
        cls.pcp = _load_pane_capture_fresh()

    def setUp(self):
        # 每个测试前确保 Qt 不可用（或注入 fake）— 我们要测的是 flipbook
        # 路径，所以强制 _QT_BACKEND = None，让 SceneViewer 分支走 flipbook
        # 而非 Qt grab。
        # 临时 patch _QT_BACKEND
        self._orig_qt = self.pcp._QT_BACKEND
        self.pcp._QT_BACKEND = None

    def tearDown(self):
        self.pcp._QT_BACKEND = self._orig_qt

    def test_sceneviewer_calls_flipbook_with_correct_args(self):
        """SceneViewer 必须调 flipbook（不是 widget.grab）且参数正确。"""
        sv = _FakeSceneViewerPane()
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
        hou_fake.frame_value = 7

        out_path = os.path.join(tempfile.gettempdir(), "test_flipbook.png")
        result = self.pcp.capture_pane_screenshot(
            hou_fake, "SceneViewer", save_path=out_path)

        # flipbook 至少被调一次
        self.assertEqual(len(sv.flipbook_calls), 1,
            "SceneViewer 必须调 scene_viewer.flipbook() 而非 widget.grab()")
        # widget.grab() 不能被调用（不能回退 Qt）
        self.assertEqual(sv._widget.grab_calls, 0,
            "SceneViewer flipbook 路径不能回退到 widget.grab()")

    def test_sceneviewer_flipbook_uses_beauty_pass_only(self):
        """settings.beautyPassOnly 必须 = True。"""
        sv = _FakeSceneViewerPane()
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
        hou_fake.frame_value = 5

        self.pcp.capture_pane_screenshot(
            hou_fake, "SceneViewer",
            save_path=os.path.join(tempfile.gettempdir(), "x.png"))

        settings = sv.flipbook_calls[0]
        self.assertTrue(getattr(settings, "beautyPassOnly", None),
            "flipbook settings.beautyPassOnly 必须为 True")

    def test_sceneviewer_flipbook_uses_single_frame_range(self):
        """settings.frameRange 必须 = ((f, f)) 单帧。"""
        sv = _FakeSceneViewerPane()
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
        hou_fake.frame_value = 12

        self.pcp.capture_pane_screenshot(
            hou_fake, "SceneViewer",
            save_path=os.path.join(tempfile.gettempdir(), "x.png"))

        settings = sv.flipbook_calls[0]
        fr = getattr(settings, "frameRange", None)
        self.assertIsNotNone(fr, "settings.frameRange 必须被设置")
        # Houdini FlipbookSettings.frameRange 是 (start, end) 二元 tuple
        # 单帧时 start=end；不要包成 (start, end), 那样 hou 解析会失败
        self.assertEqual(tuple(fr), (12, 12),
            "settings.frameRange 必须是单帧 (f, f)")

    def test_sceneviewer_flipbook_output_to_mplay_false(self):
        """settings.outputToMPlay 必须 = False（避免 MPlay 弹窗）。"""
        sv = _FakeSceneViewerPane()
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
        hou_fake.frame_value = 1

        self.pcp.capture_pane_screenshot(
            hou_fake, "SceneViewer",
            save_path=os.path.join(tempfile.gettempdir(), "x.png"))

        settings = sv.flipbook_calls[0]
        self.assertFalse(getattr(settings, "outputToMPlay", None),
            "settings.outputToMPlay 必须为 False")

    def test_sceneviewer_flipbook_uses_zero_frame_increment(self):
        """settings.frameIncrement 必须为 0。"""
        sv = _FakeSceneViewerPane()
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)

        self.pcp.capture_pane_screenshot(
            hou_fake, "SceneViewer",
            save_path=os.path.join(tempfile.gettempdir(), "x.png"))

        settings = sv.flipbook_calls[0]
        self.assertEqual(getattr(settings, "frameIncrement", None), 0)

    def test_sceneviewer_flipbook_uses_template_with_placeholder(self):
        """settings.output 必须用 $F4 占位（支持多帧）。"""
        sv = _FakeSceneViewerPane()
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
        hou_fake.frame_value = 1

        out_path = os.path.join(tempfile.gettempdir(), "x.png")
        self.pcp.capture_pane_screenshot(
            hou_fake, "SceneViewer", save_path=out_path)

        settings = sv.flipbook_calls[0]
        output = getattr(settings, "output", None)
        self.assertIsNotNone(output, "settings.output 必须被设置")
        # output 应当是带 $F4 占位的 path_template（user spec 明确要求）
        self.assertIn("$F4", output,
            "settings.output 必须含 $F4 占位符以支持多帧")

    def test_sceneviewer_return_dict_has_flipbook_marker(self):
        """返回 dict 必须含 _renderer='flipbook_via_Houdini_internal'。"""
        sv = _FakeSceneViewerPane()
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
        hou_fake.frame_value = 1

        out_path = os.path.join(tempfile.gettempdir(), "test_marker.png")
        result = self.pcp.capture_pane_screenshot(
            hou_fake, "SceneViewer", save_path=out_path)

        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("_renderer"),
            "flipbook_via_Houdini_internal",
            "返回 dict 必须含 _renderer='flipbook_via_Houdini_internal'")
        self.assertEqual(result.get("pane_type"), "SceneViewer")

    def test_sceneviewer_returns_actual_png_metadata(self):
        """成功响应必须来自实际 PNG 的 IHDR 与文件大小。"""
        sv = _FakeSceneViewerPane(output_width=321, output_height=123)
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
        tmpdir = tempfile.mkdtemp()
        try:
            result = self.pcp.capture_pane_screenshot(
                hou_fake, "SceneViewer",
                save_path=os.path.join(tmpdir, "actual.png"))
            self.assertEqual(result["width"], 321)
            self.assertEqual(result["height"], 123)
            self.assertGreater(result["size_bytes"], 0)
            self.assertEqual(result["size_bytes"],
                             os.path.getsize(result["save_path"]))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sceneviewer_rejects_missing_zero_and_zero_ihdr_outputs(self):
        """缺失、零字节、零 IHDR 均不得伪成功。"""
        for mode in ("missing", "zero_bytes", "zero_ihdr", "invalid_png"):
            sv = _FakeSceneViewerPane(output_mode=mode)
            hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
            tmpdir = tempfile.mkdtemp()
            try:
                with self.assertRaises(RuntimeError):
                    self.pcp.capture_pane_screenshot(
                        hou_fake, "SceneViewer",
                        save_path=os.path.join(tmpdir, mode + ".png"))
                self.assertEqual(sv._widget.grab_calls, 0)
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sceneviewer_uses_viewport_camera_no_new_cam_node(self):
        """相机必须从 curViewport().camera() 取，不新建 cam 节点。"""
        cam = _FakeCamera("my_viewport_cam")
        sv = _FakeSceneViewerPane(camera=cam)
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
        hou_fake.frame_value = 1

        self.pcp.capture_pane_screenshot(
            hou_fake, "SceneViewer",
            save_path=os.path.join(tempfile.gettempdir(), "x.png"))

        # flipbook 仍被调一次（视口相机被内部使用，外部无法直接观测）
        self.assertEqual(len(sv.flipbook_calls), 1)

    def test_sceneviewer_flipbook_failure_raises_no_qt_fallback(self):
        """flipbook 抛异常时不能再回退 widget.grab()。"""
        sv = _FakeSceneViewerPane(raise_on_flipbook=True)
        hou_fake = _FakeHou(scene_viewer=sv, qt_backend=None)
        hou_fake.frame_value = 1

        with self.assertRaises(Exception) as ctx:
            self.pcp.capture_pane_screenshot(
                hou_fake, "SceneViewer",
                save_path=os.path.join(tempfile.gettempdir(), "x.png"))

        # 关键不变量：即使 flipbook 失败，widget.grab() 也不被回退调用
        self.assertEqual(sv._widget.grab_calls, 0,
            "flipbook 失败时绝不能回退到 widget.grab()")

    def test_networkeditor_does_not_use_flipbook(self):
        """NetworkEditor 仍走 Qt grab 路径（不能因 Bug B 修复被误改）。"""
        # 注入 fake PySide6 让 Qt 后端可用
        fake_qtcore = types.ModuleType("PySide6.QtCore")
        fake_pkg = types.ModuleType("PySide6")
        fake_pkg.QtCore = fake_qtcore
        sys.modules["PySide6"] = fake_pkg
        sys.modules["PySide6.QtCore"] = fake_qtcore

        try:
            pcp_with_qt = _load_pane_capture_fresh()
            pcp_with_qt._QT_BACKEND = "PySide6"

            class _NormalWidget(object):
                """不抛异常的 widget（测试用）；grab 返 _FakeQImage 模拟。"""
                def __init__(self):
                    self.grab_calls = 0
                def grab(self):
                    self.grab_calls += 1
                    return _LocalPixmap()

            class _LocalImage(object):
                def isNull(self):
                    return False
                def width(self):
                    return 100
                def height(self):
                    return 100
                def save(self, path):
                    with open(path, "wb") as handle:
                        handle.write(b"non-sceneviewer-test")

            class _LocalPixmap(object):
                def isNull(self):
                    return False
                def toImage(self):
                    return _LocalImage()

            class _NetworkEditorPane(object):
                def __init__(self):
                    self.flipbook_calls = []
                    self._home_calls = []
                    self._widget = _NormalWidget()
                def homeAll(self):
                    self._home_calls.append(True)
                def qtWidget(self):
                    return self._widget
                def flipbook(self, settings):
                    self.flipbook_calls.append(settings)
                def curViewport(self):
                    return None

            class _NetworkEditorHou(object):
                def __init__(self):
                    self._pane = _NetworkEditorPane()
                    self.paneTabType = types.SimpleNamespace(
                        NetworkEditor=object(),
                    )
                    self.ui = types.SimpleNamespace(
                        paneTabOfType=lambda t: self._pane,
                    )
                    self.frame_value = 1
                    self.FlipbookSettings = _FakeFlipbookSettings
                def frame(self):
                    return 1

            hou_fake = _NetworkEditorHou()
            out_path = os.path.join(tempfile.gettempdir(), "ne.png")
            pcp_with_qt.capture_pane_screenshot(
                hou_fake, "NetworkEditor", save_path=out_path)
            # NetworkEditor 走 Qt grab，flipbook 绝不能被调
            self.assertEqual(hou_fake._pane.flipbook_calls, [],
                "NetworkEditor 不能调 flipbook（仅 SceneViewer 走该路径）")
            self.assertGreater(hou_fake._pane._widget.grab_calls, 0,
                "NetworkEditor 应走 Qt grab（widget.grab() 至少被调一次）")
        finally:
            sys.modules.pop("PySide6", None)
            sys.modules.pop("PySide6.QtCore", None)


if __name__ == "__main__":
    unittest.main()
