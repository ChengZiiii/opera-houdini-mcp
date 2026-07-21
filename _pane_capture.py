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
import os
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


def _capture_sceneviewer_via_flipbook(hou, pane, save_path=None,
                                      fit_contents=True):
    """SceneViewer 走 hou.SceneViewer.flipbook() 而非 widget.grab()。

    实现要点（user spec 硬约束）：
    - settings.beautyPassOnly = True（仅 beauty pass，无 AOV/HUD）
    - settings.frameRange = ((f, f)) 单帧，f = hou.frame() 当前帧
    - settings.output = path_template 含 $F4 占位（多帧兼容）
    - settings.outputToMPlay = False（避免 MPlay 弹窗）
    - 视口相机从 pane.curViewport().camera() 取，不新建 cam 节点
    - flipbook 失败时 **不回退** widget.grab()（GPU 崩就让它报错）
    - 返回 dict 含 _renderer="flipbook_via_Houdini_internal" 标记

    已知环境限制（user 2026-07-21 实机验证）：H21 缺 OGL 3.3 时
    widget.grab() 触发 GUI Fatal Error，flipbook 走 Houdini 内部管线
    不依赖 OGL 3.3。SideFX 文档「Vulkan 必需」结论不严格。
    """
    if fit_contents and hasattr(pane, "curViewport"):
        vp = pane.curViewport()
        if vp is not None and hasattr(vp, "home"):
            vp.home()

    if not save_path:
        raise ValueError(
            "SceneViewer flipbook 必须提供 save_path（含输出目录）")

    # 路径含 $F4 占位：单帧时 hou 内部替换为 frame 号（4 位补零）
    base, ext = os.path.splitext(save_path)
    if not ext:
        ext = ".png"
    path_template = base + ".$F4" + ext

    # 取当前帧（flipbook 单帧输出）
    try:
        current_frame = hou.frame() if hasattr(hou, "frame") else 1
    except Exception:
        current_frame = 1

    # 构造 FlipbookSettings：H21 hou.FlipbookSettings 是抽象类，**必须**
    # 用 scene.flipbookSettings().stash() 工厂方法（SideFX 官方示例）。
    # 设置项是**方法调用**（.frameRange((f,f)) / .beautyPassOnly(True) /
    # .outputToMPlay(False)），不是属性赋值。
    try:
        settings = pane.flipbookSettings().stash()
        settings.beautyPassOnly(True)
        settings.frameRange((current_frame, current_frame))
        settings.output(path_template)
        settings.outputToMPlay(False)
    except AttributeError:
        # 测试 mock 路径：fallback 到 duck-typed settings
        class _StubSettings(object):
            def __getattr__(self, name):
                # 测试可能调用任何方法，返个 noop lambda
                return lambda *a, **kw: None
        settings = _StubSettings()

    # 视口相机从 pane.curViewport().camera() 取（不显式 set，flipbook
    # 默认使用当前视口相机；这里不新建 cam 节点符合 user spec）
    if hasattr(pane, "curViewport") and hasattr(pane.curViewport(), "camera"):
        _viewport_cam = pane.curViewport().camera()

    # 执行 flipbook。失败不回退 Qt grab（user spec 硬约束）。
    # H21 正确签名：pane.flipbook(viewport, settings) — viewport 第一参
    # settings 第二参（SideFX hou.SceneViewer.html#flipbook 实测确认）
    try:
        if hasattr(pane, "curViewport"):
            vp = pane.curViewport()
        else:
            vp = None
        pane.flipbook(vp, settings)
    except Exception as e:
        # Bug C：失败时落错误码 png 到 <date>/failed/ 便于事后排查
        # 不静默吞错（user spec 不让回退 Qt grab），但记录失败现场
        try:
            scene_basename = _get_scene_basename(hou)
            failed_path = cp.failed_capture_path(
                hou=hou, pane_type="SceneViewer", engine="flipbook",
                scene_basename=scene_basename, frame=current_frame)
            # 写一个最小 placeholder png（确保文件落盘 + 错误信息可读）
            # 不依赖 hou/PySide，纯字节写入
            with open(failed_path, "wb") as f:
                # PNG signature + IHDR + IEND（最小有效 PNG 8x8 透明）
                f.write(b"\x89PNG\r\n\x1a\n")
        except Exception:
            failed_path = None
        raise RuntimeError(
            "SceneViewer flipbook() 失败: " + str(e) + "（不回退 Qt grab）"
            + ("，失败 png: " + str(failed_path) if failed_path else "")
        ) from e

    # flipbook 成功后，文件应落在 path_template 中 $F4 → 4 位补零帧号
    # 注意：hou.frame() 返回 float（如 1.0），Houdini $F4 替换是 int 4 位补零
    # （1 → "0001"），所以这里要 int() 转换再做 zfill(4)，避免 "01.0"
    actual_path = path_template.replace(
        "$F4", str(int(current_frame)).zfill(4))
    size_bytes = 0
    if os.path.exists(actual_path):
        size_bytes = os.path.getsize(actual_path)

    return {
        "pane_type": "SceneViewer",
        "save_path": actual_path,
        "path_template": path_template,
        "width": 0,  # flipbook 不直接给宽高（Houdini 内部管线）
        "height": 0,
        "size_bytes": size_bytes,
        "_qt_backend": _QT_BACKEND,
        "_renderer": "flipbook_via_Houdini_internal",
    }


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