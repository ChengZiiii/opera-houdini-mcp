"""_pane_capture.py — opera-houdini-mcp pane 截图工具集（PR 13）。

模块职责：
- 把 Houdini UI pane（NetworkEditor / SceneViewer / Compositor /
  ChannelEditor 等）通过 Qt widget.grab() 抓成 PNG；fit contents 后再截图
  可让 NetworkEditor/Compositor 的可视范围对齐，SceneViewer 通过
  curViewport().home() 缩放到当前显示节点。
- 跨 desktop 列出所有可见 pane（list_visible_panes）。
- 批量截图（capture_multiple_panes）。
- 定位到指定节点所在 NetworkEditor、cd 到节点、再截图
  （render_node_network）。
- PySide6/PySide2 同进程容错：Houdini 21.0+ 用 PySide6，更老版本回退
  PySide2；测试环境无 PySide 时 `_QT_BACKEND = None`，所有截图函数
  graceful 返 `_warning` dict，不抛异常。
- 响应全部走 `cmn.apply_response_cap` 由 server.py 包装；本模块保持纯
  函数（hou 第一参数注入），便于单测。

约束：
- hou 通过参数注入，不在顶层 import hou。
- 不新增 pip 依赖；PySide 由运行环境（houdini-mcp-env）提供。
- 4 空格缩进 / snake_case / 中文 docstring / 无 f-string / 无类型注解。
"""
import glob
import os
import struct
from . import _common as cmn
from . import _capture_paths as cp


# ---------------------------------------------------------------------------
# PySide6 / PySide2 容错 import。沿用 server.py / HoudiniMCPRender.py
# 已有 try/except 范式：先 PySide6（Houdini 21.0+），回退 PySide2，再回退 None。
# 测试环境通常无 PySide，此时 _QT_BACKEND = None，截图函数返 warning dict。
# ---------------------------------------------------------------------------
QtWidgets = None
QtCore = None
QtGui = None
try:
    from PySide6 import QtWidgets as _QtWidgets6, QtCore as _QtCore6, QtGui as _QtGui6
    QtWidgets = _QtWidgets6
    QtCore = _QtCore6
    QtGui = _QtGui6
    _QT_BACKEND = "PySide6"
except ImportError:
    try:
        from PySide2 import QtWidgets as _QtWidgets2, QtCore as _QtCore2, QtGui as _QtGui2
        QtWidgets = _QtWidgets2
        QtCore = _QtCore2
        QtGui = _QtGui2
        _QT_BACKEND = "PySide2"
    except ImportError:
        _QT_BACKEND = None


# ---------------------------------------------------------------------------
# 30 个常用 Houdini pane 类型常量。lower() 后匹配 hou.paneTabType 属性名
# （实际 hou 用 PascalCase enum 值，故 "NetworkEditor".lower() 在 brief 中
# 仅用于分发决策，hou.paneTabType.X 始终用原 PascalCase 取）。
# ---------------------------------------------------------------------------
VALID_PANE_TYPES = [
    "NetworkEditor", "SceneViewer", "Compositor",
    "ChannelEditor", "ParameterEditor", "PythonPanel",
    "GeometrySpreadsheet", "MaterialPalette", "ChannelList",
    "HelpBrowser", "MessageWindow", "Textport",
    "BundleList", "TakeList", "AssetManager",
    "PerformanceMonitor", "AnimationEditor", "SceneGraphTree",
    "DetailsView", "TreeView", "LightLinker",
    "RenderScheduler", "NetWork", "OutputViewer",
    "ROP Network", "SHOP Network", "VOP Network",
    "LOP Network", "COP Network", "CHOP Network",
]


# ---------------------------------------------------------------------------
# H21+ hou.paneTabType 重命名兼容 map（Bug 2 / opera-houdinimcp-h21-compat）：
# 用户传历史名（如 ParameterEditor / ChannelEditorPane）时，先查
# hou.paneTabType 真实名失败后再查 alias list 找第一个可见实例。
# 真实名字（如 NetworkEditor / ParmSpreadsheet）始终优先匹配 hou.paneTabType。
# 依据 SideFX hou.paneTabType H22 文档：ParameterEditor 已移除，真实名是
# ParmSpreadsheet / Parm / DetailsView；ChannelEditorPane 改名为
# ChannelEditor（hou.ChannelEditorPane.html 显式标记 Deprecated）；其余
# 旧 *Pane 尾缀名一并并入新名（不带 Pane 后缀）。alias 仅在 H21+ hou.paneTabType
# 未保留这些旧名时作为兜底；装了老 plugin 或 H20 时 hou.paneTabType 上仍可能
# 存在旧名，D5 决策：alias **不覆盖**真实名字。
# ---------------------------------------------------------------------------
_PANETYPE_ALIASES = {
    "ParameterEditor":   ["ParmSpreadsheet", "Parm", "DetailsView"],
    "ParameterPane":     ["ParmSpreadsheet", "Parm"],
    "ChannelEditorPane": ["ChannelEditor"],
    "ChannelViewerPane": ["ChannelViewer"],
    "ChannelListPane":   ["ChannelList"],
}


def _fit_pane_contents(pane, pane_type_name):
    """按 pane 类型调用对应的 fit 方法。

    NetworkEditor / Compositor / ChannelEditor -> pane.homeAll()
    SceneViewer -> pane.curViewport().home()
    其他类型 no-op。

    测试环境无 Qt 时（_QT_BACKEND is None）一律 no-op，避免 AttributeError
    on missing Qt 模块。
    """
    if _QT_BACKEND is None:
        return
    name = pane_type_name.lower()
    if name == "networkeditor":
        if hasattr(pane, "homeAll"):
            pane.homeAll()
    elif name == "sceneviewer":
        if hasattr(pane, "curViewport"):
            vp = pane.curViewport()
            if vp is not None and hasattr(vp, "home"):
                vp.home()
    elif name == "compositor":
        if hasattr(pane, "homeAll"):
            pane.homeAll()
    elif name == "channeleditor":
        if hasattr(pane, "homeAll"):
            pane.homeAll()
    # 其他类型 no-op（ParameterEditor / PythonPanel / 等）


def capture_pane_screenshot(hou, pane_type_name, save_path=None,
                            fit_contents=True):
    """截图指定类型 pane。

    SceneViewer 分支走 hou.SceneViewer.flipbook() 内部管线（不走 Qt
    widget.grab），原因：用户 H21 缺 OGL 3.3，widget.grab() 会触发 GUI
    Fatal Error；flipbook 走 Houdini 内部管线不受 OGL 3.3 强制要求
    （2026-07-21 用户实机验证推翻 SideFX 文档「Vulkan 必需」结论）。

    其他 30 种 pane 保留 Qt grab 路径不变。

    save_path 为 None 时自动走 BASE 目录规范（Bug C / PR 21）：
        $TEMP/houdini_mcp/<YYYY-MM-DD>/<HHMMSS>_<scene>_<frame>_<engine>.png
    调用方显式传入的 save_path 仍生效（向后兼容，不变量）。

    Args:
        hou: hou 模块或 stub（测试 mock）。
        pane_type_name: pane 类型名（须是 hou.paneTabType 的合法属性，如
                       "SceneViewer" / "NetworkEditor"）。
        save_path: 截图保存路径；None 走 BASE 目录自动生成。
        fit_contents: True 则先调用 fit 方法把可视范围对齐。

    Returns:
        dict with keys: pane_type, save_path, width, height, size_bytes,
        _qt_backend；SceneViewer 分支额外含 _renderer=
        "flipbook_via_Houdini_internal"。其他 30 种 pane 额外含 _renderer
        三态标记：H21+H22 "qtScreenGeometry_QScreen_grabWindow" /
        H20 老路径 "qtWidget_fallback_H20" /
        H20 widget 降级 "qtScreenGeometry_fallback_H20_widget_None"。
        无 PySide 时（非 SceneViewer 类型）额外含 _warning 字段。

    Raises:
        ValueError: hou.paneTabType 上找不到 pane_type_name / 当前 UI 无
                    该类型 pane / SceneViewer 必须传 save_path。
        RuntimeError: pane 存在但 qtWidget() 返回 None / Qt grab 失败 /
                      flipbook 失败（不回退 Qt grab，user spec 硬约束）。
    """
    if _QT_BACKEND is None and pane_type_name != "SceneViewer":
        return {
            "pane_type": pane_type_name,
            "save_path": save_path,
            "width": 0,
            "height": 0,
            "size_bytes": 0,
            "_qt_backend": None,
            "_warning": "PySide6/PySide2 不可用，截图失败",
        }

    pane_type_enum = getattr(hou.paneTabType, pane_type_name, None)
    # Bug 2：alias 兜底。attempted 列表用于失败时给出排查信息，含用户原名
    # 加上尝试过的 alias（如 ["ParameterEditor", "ParmSpreadsheet", ...]）。
    attempted = [pane_type_name]

    if pane_type_enum is not None:
        pane = hou.ui.paneTabOfType(pane_type_enum)
        if pane is None:
            # 真实名在 hou.paneTabType 上，但当前 UI 无该类型 pane
            raise ValueError("未找到 " + str(pane_type_name) + " pane")
    else:
        # 用户传了 hou.paneTabType 上没有的属性名 → 试 _PANETYPE_ALIASES
        # 中预定义的历史别名（D4 决策）。第一个可见实例胜出。
        pane = None
        aliases = _PANETYPE_ALIASES.get(pane_type_name, [])
        for alias_name in aliases:
            attempted.append(alias_name)
            alias_enum = getattr(hou.paneTabType, alias_name, None)
            if alias_enum is None:
                # 用户的 hou 版本连 alias 名都没有（H20 之前的极老版本）→ 跳过
                continue
            pane = hou.ui.paneTabOfType(alias_enum)
            if pane is not None:
                break  # 第一个可见实例即胜出
        if pane is None:
            raise ValueError(
                "未找到 pane 类型: " + str(pane_type_name)
                + "（已尝试别名: " + str(attempted) + "）")

    # save_path 为 None 时按 Bug C 规范走 BASE 目录自动生成
    # （SceneViewer flipbook 需要路径，否则 ValueError；Qt grab 路径
    # None 时走 QBuffer 不落盘，与原行为兼容）
    if save_path is None and pane_type_name != "SceneViewer":
        # Qt grab 路径：保留原行为（None = QBuffer 不落盘）
        # 因为 Qt grab 是 Qt 内部缓存，不需要自动落盘路径
        pass
    elif save_path is None and pane_type_name == "SceneViewer":
        # SceneViewer flipbook 必须有路径，自动用 BASE 目录
        scene_basename = _get_scene_basename(hou)
        frame = _get_current_frame(hou)
        save_path = cp.default_capture_path(
            hou=hou, pane_type="SceneViewer", engine="flipbook",
            scene_basename=scene_basename, frame=frame)

    # SceneViewer 走 hou.SceneViewer.flipbook()，不走 Qt grab。
    # 原因：用户机 H21 缺 OGL 3.3，widget.grab() 触发 GUI Fatal Error。
    if pane_type_name == "SceneViewer":
        return _capture_sceneviewer_via_flipbook(
            hou, pane, save_path=save_path, fit_contents=fit_contents)

    if fit_contents:
        _fit_pane_contents(pane, pane_type_name)

    # Bug 1 H21+H22 截屏路径（opera-houdinimcp-h21-compat）：
    # H21+ hou.PaneTab.qtWidget() API 已移除（SideFX H22 文档确认）。
    # 改走 hou.PaneTab.qtScreenGeometry() 返 QRect(x, y, w, h)，
    # 再用 QScreen.grabWindow(0, x, y, w, h) 直接从屏幕像素缓冲读 pane 区域。
    # 不依赖 Houdini 内部 OGL，规避用户 H21 缺 OGL 3.3 时 widget.grab() 触发
    # GUI Fatal Error 的 fatal combination（项目记忆 env-houdini-opengl-driver-missing）。
    #
    # H20 best-effort fallback：hasattr(pane, "qtWidget") and callable(...) 时
    # 仍走老 widget.grab() 路径（保持现有 H20 行为），widget 返 None 或抛异常
    # 时 fail-soft 到下方 QScreen 兜底。
    #
    # _renderer 三态标记区分走的哪条路径：
    #   "qtScreenGeometry_QScreen_grabWindow"    — H21+H22 主路径（pane 无 qtWidget）
    #   "qtWidget_fallback_H20"                   — H20 老 widget.grab() 成功
    #   "qtScreenGeometry_fallback_H20_widget_None" — H20 widget 失败/None，降级到 QScreen
    pixmap = None
    _renderer_marker = None
    _h20_attempted = False  # 是否尝试过 H20 widget 路径（决定 _renderer 三态）

    # H20 best-effort：仅当 pane 有可调用的 qtWidget 方法时尝试
    if hasattr(pane, "qtWidget") and callable(getattr(pane, "qtWidget", None)):
        _h20_attempted = True
        try:
            _widget = pane.qtWidget()
            if _widget is not None:
                try:
                    _p = _widget.grab()
                    if _p is not None and not _p.isNull():
                        pixmap = _p
                        _renderer_marker = "qtWidget_fallback_H20"
                except Exception:
                    # H20 widget.grab() 抛异常（如用户机缺 OGL 3.3）
                    pixmap = None
        except Exception:
            # H20 SWIG bug 或 qtWidget() 自身抛异常
            pixmap = None

    # H21+H22 主路径（qtScreenGeometry + QScreen.grabWindow）：
    # H21 时直接走此处（无 qtWidget 方法）；H20 widget=None/失败时降级到此处。
    if pixmap is None:
        try:
            # SideFX hou.PaneTab H22 文档：qtScreenGeometry() 返 PySide6.QtCore.QRect
            # x/y/width/height 为 pane 屏幕坐标（top-left corner + size）
            geom = pane.qtScreenGeometry()
            # QApplication.instance() 已存在（Houdini 内）时不能 QApplication([])
            # 否则抛 RuntimeError（Qt 文档：QApplication 必须单例）
            _app = QtWidgets.QApplication.instance()
            if _app is None:
                _app = QtWidgets.QApplication([])
            _screen = _app.primaryScreen()
            # QScreen.grabWindow(WId window=0, int x=0, int y=0,
            #                    int width=-1, int height=-1) -> QPixmap
            # window=0 表示截整个屏幕 + 指定矩形区域（Qt 文档）
            pixmap = _screen.grabWindow(
                0, geom.x(), geom.y(), geom.width(), geom.height())
            if _h20_attempted:
                # H20 widget 路径已尝试但失败（None 或抛异常），降级到 QScreen
                _renderer_marker = "qtScreenGeometry_fallback_H20_widget_None"
            else:
                # H21+H22 主路径（pane 无 qtWidget 方法，H20 路径未尝试）
                _renderer_marker = "qtScreenGeometry_QScreen_grabWindow"
        except Exception as _e:
            if pixmap is None:
                raise RuntimeError(
                    str(pane_type_name) + " 截屏失败: " + str(_e)) from _e

    if pixmap is None or pixmap.isNull():
        raise RuntimeError(
            str(pane_type_name) + " 截屏返回无效 pixmap")
    try:
        img = pixmap.toImage()
    except Exception as e:
        raise RuntimeError(str(pane_type_name) + " pixmap.toImage() 失败: " + str(e)) from e
    if img is None or img.isNull():
        raise RuntimeError(str(pane_type_name) + " pixmap 转 image 失败")
    width = img.width()
    height = img.height()

    if save_path:
        img.save(save_path)
        size_bytes = os.path.getsize(save_path)
    else:
        buf = QtCore.QBuffer()
        buf.open(QtCore.QIODevice.WriteOnly)
        img.save(buf, "PNG")
        size_bytes = buf.size()
        buf.close()

    return {
        "pane_type": pane_type_name,
        "save_path": save_path,
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "_qt_backend": _QT_BACKEND,
        "_renderer": _renderer_marker,
    }


def _get_scene_basename(hou):
    """返回 hou.hipFile.basename()（无后缀）；失败回退 "untitled"。"""
    try:
        if hasattr(hou, "hipFile") and hasattr(hou.hipFile, "basename"):
            return hou.hipFile.basename()
    except Exception:
        pass
    return "untitled"


def _get_current_frame(hou):
    """返回 hou.frame() 当前帧；失败回退 1。"""
    try:
        if hasattr(hou, "frame"):
            return hou.frame()
    except Exception:
        pass
    return 1


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SCENEVIEWER_VIEW_NAMES = ("top", "front", "right", "perspective")
_SCENEVIEWER_VIEW_TYPES = {
    "top": "Top",
    "front": "Front",
    "right": "Right",
    "perspective": "Perspective",
}


def _path_value(value, label):
    """Return a normalized filesystem path or raise a clear ValueError."""
    try:
        path = os.fspath(value)
    except TypeError:
        raise ValueError(label + " 必须是文件系统路径")
    if not isinstance(path, str) or not path:
        raise ValueError(label + " 不能为空")
    return os.path.abspath(path)


def _ensure_output_parent(path):
    """Create and verify the actual output parent directory."""
    parent = os.path.dirname(os.path.abspath(path))
    if not parent:
        parent = os.getcwd()
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        raise RuntimeError("无法创建 SceneViewer 输出目录: " + str(e)) from e
    if not os.path.isdir(parent):
        raise RuntimeError("SceneViewer 输出目录不是目录: " + parent)
    return parent


def _format_flipbook_frame(frame):
    """Format the active frame as Houdini's $F4 integer expansion."""
    try:
        return str(int(frame)).zfill(4)
    except (TypeError, ValueError, OverflowError):
        return "0001"


def _flipbook_paths(save_path, frame):
    """Return (template, predicted_actual, glob_pattern) for a PNG output."""
    normalized = _path_value(save_path, "save_path")
    base, ext = os.path.splitext(normalized)
    if not ext:
        ext = ".png"
    elif ext.lower() != ".png":
        ext = ".png"
        base = os.path.splitext(normalized)[0]
    if "$F4" in base:
        template = base + ext
    else:
        template = base + ".$F4" + ext
    frame_text = _format_flipbook_frame(frame)
    actual = template.replace("$F4", frame_text)
    pattern = template.replace("$F4", "*")
    _ensure_output_parent(actual)
    return template, actual, pattern


def _remove_stale_flipbook_targets(actual_path, pattern):
    """Remove old files matching this deterministic single-frame target."""
    candidates = set([actual_path])
    try:
        candidates.update(glob.glob(pattern))
    except (TypeError, ValueError):
        pass
    for path in candidates:
        if not os.path.exists(path):
            continue
        if os.path.isdir(path):
            raise RuntimeError("SceneViewer 输出目标是目录: " + path)
        try:
            os.remove(path)
        except OSError as e:
            raise RuntimeError("无法删除旧的 SceneViewer 输出: " + str(e)) from e


def _read_png_metadata(path):
    """Validate PNG signature/IHDR and return actual width/height/size bytes."""
    if not os.path.isfile(path):
        raise RuntimeError("SceneViewer flipbook 输出文件不存在: " + path)
    try:
        size_bytes = os.path.getsize(path)
    except OSError as e:
        raise RuntimeError("无法读取 SceneViewer 输出大小: " + str(e)) from e
    if size_bytes <= 0:
        raise RuntimeError("SceneViewer flipbook 输出为零字节: " + path)
    try:
        with open(path, "rb") as handle:
            header = handle.read(29)
    except OSError as e:
        raise RuntimeError("无法读取 SceneViewer PNG: " + str(e)) from e
    if len(header) < 29 or header[:8] != _PNG_SIGNATURE:
        raise RuntimeError("SceneViewer 输出不是有效 PNG: " + path)
    chunk_length = struct.unpack(">I", header[8:12])[0]
    if header[12:16] != b"IHDR" or chunk_length < 8:
        raise RuntimeError("SceneViewer PNG 缺少 IHDR: " + path)
    if len(header) < 16 + chunk_length:
        raise RuntimeError("SceneViewer PNG IHDR 不完整: " + path)
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise RuntimeError(
            "SceneViewer PNG IHDR 尺寸无效: {0}x{1}: {2}".format(
                width, height, path))
    return {
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
    }


def _resolve_actual_flipbook_path(predicted_path, pattern):
    """Resolve the file Houdini actually wrote, including fractional frames."""
    if os.path.isfile(predicted_path):
        return predicted_path
    matches = [p for p in sorted(glob.glob(pattern)) if os.path.isfile(p)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(
            "SceneViewer flipbook 未生成实际 PNG 输出: " + predicted_path)
    raise RuntimeError(
        "SceneViewer flipbook 生成多个候选 PNG，无法确定实际输出: "
        + ", ".join(matches))


def _set_flipbook_setting(settings, name, value):
    """Set a HOM flipbook setting using its H21 method API."""
    setter = getattr(settings, name, None)
    if callable(setter):
        setter(value)
        return
    # A few older test/HOM shims expose settings as writable attributes.
    try:
        setattr(settings, name, value)
    except Exception as e:
        raise RuntimeError("FlipbookSettings 缺少 " + name + " API") from e


def _stash_flipbook_settings(pane):
    """Obtain a per-call settings stash without constructing HOM directly."""
    factory = getattr(pane, "flipbookSettings", None)
    if not callable(factory):
        raise RuntimeError("SceneViewer 不支持 flipbookSettings()")
    source = factory()
    stash = getattr(source, "stash", None)
    if callable(stash):
        return stash()
    return source


def _sceneviewer_viewport(pane):
    """Return the active SceneViewer viewport."""
    getter = getattr(pane, "curViewport", None)
    if not callable(getter):
        raise RuntimeError("SceneViewer 不支持 curViewport()")
    viewport = getter()
    if viewport is None:
        raise RuntimeError("SceneViewer 没有可用的当前 viewport")
    return viewport


def _frame_sceneviewer(pane):
    """Frame visible geometry using the strongest available HOM method."""
    viewport = _sceneviewer_viewport(pane)
    for name in ("homeAll", "home"):
        method = getattr(viewport, name, None)
        if callable(method):
            method()
            return
    raise RuntimeError("SceneViewer viewport 缺少 homeAll()/home()")


def _prepare_flipbook_output(hou, save_path, current_frame):
    """Create output parent, clear stale files, and build actual path data."""
    template, predicted, pattern = _flipbook_paths(save_path, current_frame)
    _remove_stale_flipbook_targets(predicted, pattern)
    return template, predicted, pattern


def _run_sceneviewer_flipbook(hou, pane, save_path, fit_contents=True):
    """Run one verified Houdini-internal SceneViewer flipbook."""
    if fit_contents:
        _frame_sceneviewer(pane)
    current_frame = _get_current_frame(hou)
    template, predicted, pattern = _prepare_flipbook_output(
        hou, save_path, current_frame)
    settings = _stash_flipbook_settings(pane)
    _set_flipbook_setting(settings, "beautyPassOnly", True)
    _set_flipbook_setting(settings, "frameRange",
                          (current_frame, current_frame))
    _set_flipbook_setting(settings, "frameIncrement", 0)
    _set_flipbook_setting(settings, "outputToMPlay", False)
    _set_flipbook_setting(settings, "output", template)
    viewport = _sceneviewer_viewport(pane)
    try:
        pane.flipbook(viewport, settings)
    except Exception as e:
        raise RuntimeError(
            "SceneViewer flipbook() 失败（不回退 Qt grab）: " + str(e)) from e
    actual_path = _resolve_actual_flipbook_path(predicted, pattern)
    metadata = _read_png_metadata(actual_path)
    metadata.update({
        "save_path": actual_path,
        "path_template": template,
        "_renderer": "flipbook_via_Houdini_internal",
    })
    return metadata


def _capture_sceneviewer_via_flipbook(hou, pane, save_path=None,
                                      fit_contents=True):
    """SceneViewer 只使用 hou.SceneViewer.flipbook()，并验证实际 PNG。"""
    if not save_path:
        raise ValueError(
            "SceneViewer flipbook 必须提供 save_path（含输出目录）")
    metadata = _run_sceneviewer_flipbook(
        hou, pane, save_path=save_path, fit_contents=fit_contents)
    return {
        "pane_type": "SceneViewer",
        "save_path": metadata["save_path"],
        "path_template": metadata["path_template"],
        "width": metadata["width"],
        "height": metadata["height"],
        "size_bytes": metadata["size_bytes"],
        "_qt_backend": _QT_BACKEND,
        "_renderer": metadata["_renderer"],
    }


def _desktop_name(desktop):
    """Return a desktop name for matching and diagnostics."""
    getter = getattr(desktop, "name", None)
    if callable(getter):
        return str(getter())
    return str(getter or "")


def _current_desktop(hou):
    """Get the current desktop using guarded H21/H22 capability names."""
    ui = getattr(hou, "ui", None)
    if ui is None:
        return None
    for name in ("curDesktop", "currentDesktop"):
        getter = getattr(ui, name, None)
        if callable(getter):
            try:
                desktop = getter()
            except Exception:
                desktop = None
            if desktop is not None:
                return desktop
    for name in ("curDesktop", "currentDesktop"):
        desktop = getattr(ui, name, None)
        if desktop is not None and not callable(desktop):
            return desktop
    return None


def _is_sceneviewer_pane(hou, pane):
    """Check SceneViewer without assuming one HOM class shape."""
    if pane is None:
        return False
    pane_type = getattr(pane, "type", None)
    scene_type = getattr(getattr(hou, "paneTabType", None),
                         "SceneViewer", None)
    if callable(pane_type):
        try:
            value = pane_type()
            if scene_type is not None and value == scene_type:
                return True
            if str(value).lower() == "sceneviewer":
                return True
        except Exception:
            pass
    if scene_type is not None and pane_type == scene_type:
        return True
    class_name = type(pane).__name__.lower()
    if class_name == "sceneviewer":
        return True
    return (callable(getattr(pane, "flipbook", None))
            and callable(getattr(pane, "curViewport", None))
            and callable(getattr(pane, "flipbookSettings", None)))


def _desktop_panes(desktop, hou):
    """List desktop panes in deterministic order with HOM capability guards."""
    getter = getattr(desktop, "paneTabs", None)
    if callable(getter):
        try:
            panes = list(getter() or [])
        except Exception:
            panes = []
    else:
        panes = []
    if panes:
        return panes
    pane_type = getattr(getattr(hou, "paneTabType", None),
                        "SceneViewer", None)
    getter = getattr(desktop, "paneTabOfType", None)
    if pane_type is not None and callable(getter):
        for index in range(32):
            try:
                pane = getter(pane_type, index)
            except Exception:
                break
            if pane is None:
                break
            panes.append(pane)
    return panes


def _find_pane_by_name(desktop, pane_name, hou):
    """Find an exact named pane in one desktop only."""
    finder = getattr(desktop, "findPaneTab", None)
    if callable(finder):
        try:
            pane = finder(pane_name)
        except Exception:
            pane = None
        if pane is not None:
            return pane
    for pane in _desktop_panes(desktop, hou):
        getter = getattr(pane, "name", None)
        try:
            name = getter() if callable(getter) else getter
        except Exception:
            name = None
        if name == pane_name:
            return pane
    return None


def _resolve_sceneviewer(hou, desktop_name=None, pane_name=None):
    """Resolve a SceneViewer without scanning unrelated desktops."""
    if (desktop_name is None) != (pane_name is None):
        raise ValueError(
            "desktop_name 与 pane_name 必须同时提供，或同时省略")
    current = _current_desktop(hou)
    ui = getattr(hou, "ui", None)
    if desktop_name is not None:
        desktops = []
        getter = getattr(ui, "desktops", None) if ui is not None else None
        if callable(getter):
            try:
                desktops = list(getter() or [])
            except Exception:
                desktops = []
        if current is not None and _desktop_name(current) == str(desktop_name):
            if current not in desktops:
                desktops.insert(0, current)
        desktop = next((d for d in desktops
                        if _desktop_name(d) == str(desktop_name)), None)
        if desktop is None:
            raise ValueError("未找到指定 desktop: " + str(desktop_name))
        pane = _find_pane_by_name(desktop, pane_name, hou)
        if not _is_sceneviewer_pane(hou, pane):
            raise ValueError(
                "指定 desktop/pane 不是可选择的 SceneViewer: "
                + str(desktop_name) + "/" + str(pane_name))
        return desktop, pane, current

    if current is None:
        raise ValueError("无法确定当前 desktop，拒绝扫描其他 desktop")
    current_getter = getattr(current, "currentPaneTab", None)
    current_pane = None
    if callable(current_getter):
        try:
            current_pane = current_getter()
        except Exception:
            current_pane = None
    if _is_sceneviewer_pane(hou, current_pane):
        return current, current_pane, current
    for pane in _desktop_panes(current, hou):
        if _is_sceneviewer_pane(hou, pane):
            return current, pane, current
    raise ValueError("当前 desktop 没有可选择的 SceneViewer")


def _activate_desktop(desktop, current):
    """Activate a selected desktop and report whether a switch occurred."""
    if desktop is current or (current is not None
                              and _desktop_name(desktop) == _desktop_name(current)):
        return False
    setter = getattr(desktop, "setAsCurrent", None)
    if not callable(setter):
        raise ValueError("目标 desktop 不支持 setAsCurrent()")
    setter()
    return True


def _snapshot_viewport_state(hou, pane):
    """Snapshot only HOM viewport state that was successfully observed."""
    viewport = _sceneviewer_viewport(pane)
    state = {"viewport": viewport}
    getter = getattr(viewport, "type", None)
    if callable(getter):
        try:
            state["type"] = getter()
        except Exception:
            pass
    getter = getattr(viewport, "cameraPath", None)
    if callable(getter):
        try:
            state["camera_path"] = getter()
        except Exception:
            pass
    getter = getattr(viewport, "isCameraLockedToView", None)
    if callable(getter):
        try:
            state["camera_locked"] = bool(getter())
        except Exception:
            pass
    getter = getattr(viewport, "defaultCamera", None)
    if callable(getter):
        try:
            camera = getter()
            stash = getattr(camera, "stash", None)
            if callable(stash):
                state["default_camera"] = stash()
        except Exception:
            pass
    getter = getattr(viewport, "camera", None)
    if callable(getter):
        try:
            camera = getter()
            if camera is not None:
                state["camera"] = camera
        except Exception:
            pass
    return state


def _restore_viewport_state(hou, state):
    """Restore observed viewport/camera/lock state and return error strings."""
    viewport = state.get("viewport")
    errors = []
    if viewport is None:
        return ["缺少原始 viewport"]
    if "type" in state:
        setter = getattr(viewport, "changeType", None)
        if callable(setter):
            try:
                setter(state["type"])
            except Exception as e:
                errors.append("恢复 viewport type 失败: " + str(e))
    camera_path = state.get("camera_path")
    if camera_path:
        setter = getattr(viewport, "setCamera", None)
        node = None
        if callable(getattr(hou, "node", None)):
            try:
                node = hou.node(camera_path)
            except Exception:
                node = None
        if callable(setter) and node is not None:
            try:
                setter(node)
            except Exception as e:
                errors.append("恢复 look-through camera 失败: " + str(e))
        else:
            errors.append("无法恢复 look-through camera: " + str(camera_path))
    elif "default_camera" in state:
        setter = getattr(viewport, "setDefaultCamera", None)
        if callable(setter):
            try:
                setter(state["default_camera"])
            except Exception as e:
                errors.append("恢复默认 viewport camera 失败: " + str(e))
    else:
        setter = getattr(viewport, "useDefaultCamera", None)
        if callable(setter):
            try:
                setter()
            except Exception as e:
                errors.append("恢复默认 viewport camera 失败: " + str(e))
    if "camera_locked" in state:
        setter = getattr(viewport, "lockCameraToView", None)
        if callable(setter):
            try:
                setter(state["camera_locked"])
            except Exception as e:
                errors.append("恢复 camera lock 失败: " + str(e))
    return errors


def _normalize_sceneviewer_views(views):
    """Validate and preserve the caller's requested view order."""
    if views is None:
        return list(_SCENEVIEWER_VIEW_NAMES[:3])
    if isinstance(views, str):
        raise ValueError(
            "views 必须是非空、无重复的视图序列: "
            + ", ".join(_SCENEVIEWER_VIEW_NAMES))
    try:
        requested = list(views)
    except TypeError as e:
        raise ValueError("views 必须是视图序列") from e
    if not requested or len(set(requested)) != len(requested):
        raise ValueError("views 必须是非空且无重复的视图序列")
    invalid = [view for view in requested
               if view not in _SCENEVIEWER_VIEW_NAMES]
    if invalid:
        raise ValueError("非法 SceneViewer 视图: " + ", ".join(map(str, invalid)))
    return requested


def _set_sceneviewer_view(hou, viewport, view_name, fit_contents):
    """Switch to a guarded H21 geometryViewportType and frame the scene."""
    type_name = _SCENEVIEWER_VIEW_TYPES[view_name]
    enum = getattr(getattr(hou, "geometryViewportType", None), type_name,
                   None)
    setter = getattr(viewport, "changeType", None)
    if enum is None or not callable(setter):
        raise RuntimeError("当前 Houdini 不支持 viewport type 切换: " + view_name)
    setter(enum)
    if fit_contents:
        for name in ("homeAll", "home"):
            method = getattr(viewport, name, None)
            if callable(method):
                method()
                return
        raise RuntimeError("SceneViewer viewport 缺少 framing API")


def capture_sceneviewer_flipbook_views(hou, views=None, save_dir=None,
                                       desktop_name=None, pane_name=None,
                                       fit_contents=True):
    """采集确定 SceneViewer 的 Top/Front/Right（可显式加 Perspective）。"""
    requested = _normalize_sceneviewer_views(views)
    desktop, pane, original_desktop = _resolve_sceneviewer(
        hou, desktop_name=desktop_name, pane_name=pane_name)
    switched_desktop = _activate_desktop(desktop, original_desktop)
    if save_dir is None:
        default_path = cp.default_capture_path(
            hou=hou, pane_type="SceneViewer", engine="flipbook",
            scene_basename=_get_scene_basename(hou),
            frame=_get_current_frame(hou))
        output_dir = os.path.dirname(default_path)
    else:
        output_dir = _path_value(save_dir, "save_dir")
        if os.path.exists(output_dir) and not os.path.isdir(output_dir):
            raise ValueError("save_dir 不是目录: " + output_dir)
        _ensure_output_parent(os.path.join(output_dir, "placeholder.png"))

    result = {
        "status": "partial",
        "complete": False,
        "requested_views": list(requested),
        "views": [],
        "_renderer": "flipbook_via_Houdini_internal",
        "state_restored": True,
    }
    try:
        for view_name in requested:
            output_base = os.path.join(output_dir, view_name + ".png")
            item = {
                "view": view_name,
                "success": False,
                "save_path": None,
                "width": 0,
                "height": 0,
                "size_bytes": 0,
                "_renderer": "flipbook_via_Houdini_internal",
                "error": None,
                "state_restored": False,
            }
            try:
                current_frame = _get_current_frame(hou)
                _template, predicted, _pattern = _flipbook_paths(
                    output_base, current_frame)
                item["save_path"] = predicted
            except Exception as e:
                item["error"] = str(e)
                result["views"].append(item)
                continue
            state = None
            restore_errors = []
            try:
                state = _snapshot_viewport_state(hou, pane)
                _set_sceneviewer_view(
                    hou, state["viewport"], view_name, fit_contents)
                capture = _run_sceneviewer_flipbook(
                    hou, pane, save_path=output_base, fit_contents=False)
                item.update({
                    "success": True,
                    "save_path": capture["save_path"],
                    "width": capture["width"],
                    "height": capture["height"],
                    "size_bytes": capture["size_bytes"],
                })
            except Exception as e:
                item["error"] = str(e)
            finally:
                if state is not None:
                    restore_errors = _restore_viewport_state(hou, state)
                if restore_errors:
                    item["success"] = False
                    restore_text = "; ".join(restore_errors)
                    item["error"] = ((item["error"] + "; ")
                                     if item["error"] else "") + restore_text
                    result["state_restored"] = False
                elif state is None:
                    result["state_restored"] = False
                else:
                    item["state_restored"] = True
                if state is None:
                    item["state_restored"] = False
                result["views"].append(item)
        result["complete"] = (result["state_restored"]
                               and all(item["success"]
                                       for item in result["views"]))
        result["status"] = "success" if result["complete"] else "partial"
    finally:
        if switched_desktop:
            try:
                if original_desktop is None:
                    raise RuntimeError("缺少原始 desktop")
                setter = getattr(original_desktop, "setAsCurrent", None)
                if not callable(setter):
                    raise RuntimeError("原始 desktop 不支持 setAsCurrent()")
                setter()
            except Exception as e:
                result["state_restored"] = False
                result["complete"] = False
                result["status"] = "partial"
                result["desktop_restore_error"] = str(e)
    return result


def list_visible_panes(hou):
    """列出当前所有 desktop 中可见的 pane。

    Returns:
        list of dict: {desktop, pane_type, name, is_current}。每条对应一个
        pane tab 节点；is_current=True 表示这是该 desktop 当前激活的 pane。
    """
    result = []
    desktops = hou.ui.desktops() if hasattr(hou.ui, "desktops") else []
    for desktop in desktops:
        pane_tabs = desktop.paneTabs() if hasattr(desktop, "paneTabs") else []
        try:
            current = desktop.currentPaneTab() if hasattr(desktop, "currentPaneTab") else None
        except Exception:
            current = None
        for pane_tab in pane_tabs:
            try:
                pane_type_name = str(type(pane_tab).__name__)
            except Exception:
                pane_type_name = "Unknown"
            try:
                name = pane_tab.name() if hasattr(pane_tab, "name") else ""
            except Exception:
                name = ""
            is_current = (current is pane_tab)
            result.append({
                "desktop": desktop.name() if hasattr(desktop, "name") else "",
                "pane_type": pane_type_name,
                "name": name,
                "is_current": is_current,
            })
    return result


def capture_multiple_panes(hou, pane_types, save_dir):
    """批量截图多种 pane。

    Args:
        hou: hou 模块。
        pane_types: list of pane 类型名（str）。
        save_dir: 截图保存目录（不存在会自动创建）。

    Returns:
        list of dict: {pane_type, save_path, success, error}，长度等于
        pane_types。任意一种 pane 抛异常都不影响其他 pane。
        save_path 字段：成功时用 capture_pane_screenshot 返回的实际
        save_path（SceneViewer flipbook 路径下会含 $F4 替换后的帧号）；
        失败时仍用请求的 save_path（便于事后排查）。
    """
    os.makedirs(save_dir, exist_ok=True)
    results = []
    for pt in pane_types:
        save_path = os.path.join(save_dir, pt + ".png")
        try:
            cap = capture_pane_screenshot(hou, pt, save_path=save_path,
                                          fit_contents=True)
            warning = cap.get("_warning")
            if warning:
                results.append({
                    "pane_type": pt,
                    "save_path": save_path,
                    "success": False,
                    "error": warning,
                })
            else:
                # 优先用 cap 返回的实际 save_path（SceneViewer flipbook
                # 路径会带 $F4 替换帧号后缀；其他路径与请求一致）
                actual_path = cap.get("save_path") or save_path
                results.append({
                    "pane_type": pt,
                    "save_path": actual_path,
                    "success": True,
                    "error": None,
                })
        except Exception as e:
            results.append({
                "pane_type": pt,
                "save_path": save_path,
                "success": False,
                "error": str(e),
            })
    return results


def render_node_network(hou, node_path, fit_contents=True, save_path=None):
    """定位到节点所在 NetworkEditor pane，cd 到节点，再截图。

    Args:
        hou: hou 模块。
        node_path: 节点路径（必须存在）。
        fit_contents: True 则截图前调用 pane.homeAll()。
        save_path: 截图保存路径；None = 不落盘。

    Returns:
        capture_pane_screenshot 返回的 dict。

    Raises:
        ValueError: 节点不存在 / 找不到 NetworkEditor pane。
    """
    node = hou.node(node_path)
    if not node:
        raise ValueError("节点不存在: " + str(node_path))

    pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
    if pane is None:
        raise ValueError("未找到 NetworkEditor pane")

    pane.cd(node.path())
    return capture_pane_screenshot(hou, "NetworkEditor", save_path=save_path,
                                   fit_contents=fit_contents)
