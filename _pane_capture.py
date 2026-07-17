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

    Args:
        hou: hou 模块或 stub（测试 mock）。
        pane_type_name: pane 类型名（须是 hou.paneTabType 的合法属性，如
                       "SceneViewer" / "NetworkEditor"）。
        save_path: 截图保存路径；None 表示不落盘，size_bytes 改用
                   QBuffer.size() 估算 PNG 字节数。
        fit_contents: True 则先调用 fit 方法把可视范围对齐。

    Returns:
        dict with keys: pane_type, save_path, width, height, size_bytes,
        _qt_backend。无 PySide 时额外含 _warning 字段。

    Raises:
        ValueError: hou.paneTabType 上找不到 pane_type_name / 当前 UI 无
                    该类型 pane。
        RuntimeError: pane 存在但 qtWidget() 返回 None。
    """
    if _QT_BACKEND is None:
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
    if pane_type_enum is None:
        raise ValueError("未找到 pane 类型: " + str(pane_type_name))

    pane = hou.ui.paneTabOfType(pane_type_enum)
    if pane is None:
        raise ValueError("未找到 " + str(pane_type_name) + " pane")

    if fit_contents:
        _fit_pane_contents(pane, pane_type_name)

    widget = pane.qtWidget() if hasattr(pane, "qtWidget") else None
    if widget is None:
        raise RuntimeError("无法获取 " + str(pane_type_name) + " Qt widget")

    pixmap = widget.grab()
    img = pixmap.toImage()
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
                results.append({
                    "pane_type": pt,
                    "save_path": save_path,
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