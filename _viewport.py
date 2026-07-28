"""_viewport.py — opera-houdini-mcp 视口控制与导航工具。

模块职责（add-viewport-control-tools）：
- 8 个公共 API：get_viewport_info / set_viewport_camera /
  set_viewport_display / set_viewport_renderer / frame_selection /
  frame_all / set_viewport_direction / set_current_network
- framing 使用 hou.GeometryViewport.frameSelected / frameAll；
- 方向映射到 hou.geometryViewportType 七个真实枚举；
- display 使用 ``viewport.settings().displaySet(...).setShadedMode(...)``
  两段白名单 → 真实 HOM enum；
- renderer 仅在 LOP SceneViewer 内可用，调用
  ``sceneViewer.hydraRenderers() / setHydraRenderer() / currentHydraRenderer()``；
- network navigation 验证节点后调 NetworkEditor 的 ``cd(path)``。

设计原则：
- hou 通过参数注入 / 不顶层 import hou，便于纯 Python 单测。
- 所有枚举固定白名单 → 真实 HOM enum；不接受反射式任意方法调用。
- 8 项全部归 NO_UNDO（UI/view 状态，不是可 undo 的 scene mutation）。
- 无 GUI / 无 pane / 非 LOP 时统一返回
  ``{"status": "warning", "_warning": {...}}``，不抛异常。
- 不实现 capture_screenshot 或任何新截图管线；不修改 _pane_capture。
- 4 空格缩进 / snake_case / 中文 docstring / 无 f-string / 无类型注解。
"""
from . import _common as cmn


_GEOMETRY_VIEWPORT_TYPE_MAP = {
    "front": "Front",
    "back": "Back",
    "left": "Left",
    "right": "Right",
    "top": "Top",
    "bottom": "Bottom",
    "perspective": "Perspective",
}


_DISPLAY_SET_MAP = {
    "main": "Main",
    "object": "Object",
    "scene": "Scene",
    "stereo": "Stereo",
    "material": "Material",
    "compositing": "Compositing",
}


_SHADED_MODE_MAP = {
    "wireframe": "Wireframe",
    "wire_shade": "WireShade",
    "shaded": "Shaded",
    "ghost": "Ghost",
    "hidden_line": "HiddenLine",
}


def _resolve_scene_viewer(hou):
    """返回当前 desktop 下的 SceneViewer；失败返回 ``None``。"""
    try:
        ui = getattr(hou, "ui", None)
        if ui is None:
            return None
        desktops = ui.desktops()
        if not desktops:
            return None
        current = ui.paneTabOfType(hou.paneTabType.SceneViewer)
        if current is not None:
            return current
        for desktop in desktops:
            try:
                pane = desktop.paneTabOfType(hou.paneTabType.SceneViewer)
            except Exception:
                pane = None
            if pane is not None:
                return pane
    except Exception:
        return None
    return None


def _current_geometry_viewport(scene_viewer):
    """从 SceneViewer 取当前 GeometryViewport；失败返 ``None``。"""
    if scene_viewer is None:
        return None
    getter = getattr(scene_viewer, "curViewport", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    getter = getattr(scene_viewer, "currentViewport", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


def _headless_warning():
    return {
        "status": "warning",
        "_warning": {
            "code": "viewport_unavailable",
            "message": "no SceneViewer / GUI available in current Houdini context",
        },
    }


def get_viewport_info(hou):
    """返回当前 SceneViewer viewport 的稳定 schema。

    返回字段：
    - camera: 当前相机节点路径（无则空字符串）
    - viewport_type: 当前 geometry viewport 类型字符串（"front" 等）
    - display_set: 显示集 token
    - shaded_mode: 着色模式 token
    - hydra_renderer: LOP 上下文时的当前 Hydra renderer；非 LOP 为空字符串

    无 GUI / 无 SceneViewer 时返回 ``_warning``。
    """
    scene_viewer = _resolve_scene_viewer(hou)
    if scene_viewer is None:
        return _headless_warning()
    viewport = _current_geometry_viewport(scene_viewer)
    info = {
        "camera": "",
        "viewport_type": "",
        "display_set": "",
        "shaded_mode": "",
        "hydra_renderer": "",
    }
    if viewport is not None:
        try:
            camera_node = viewport.camera()
            if camera_node is not None:
                info["camera"] = camera_node.path()
        except Exception:
            pass
        try:
            view_type = viewport.type()
            if view_type is not None:
                info["viewport_type"] = str(view_type).split(".")[-1].lower()
        except Exception:
            pass
        try:
            settings = viewport.settings()
            if settings is not None:
                display_set = settings.displaySet()
                if display_set is not None:
                    info["display_set"] = str(display_set).split(".")[-1].lower()
                try:
                    info["shaded_mode"] = str(
                        display_set.shadedMode()).split(".")[-1].lower()
                except Exception:
                    pass
        except Exception:
            pass
    if _is_lop_context(hou):
        try:
            current = scene_viewer.currentHydraRenderer()
            if current:
                info["hydra_renderer"] = str(current)
        except Exception:
            pass
    return info


def set_viewport_camera(hou, camera_path):
    """设置 SceneViewer 当前 viewport 的 camera；节点不存在报结构化 error。"""
    scene_viewer = _resolve_scene_viewer(hou)
    if scene_viewer is None:
        return _headless_warning()
    viewport = _current_geometry_viewport(scene_viewer)
    if viewport is None:
        return _headless_warning()
    node = hou.node(camera_path) if camera_path else None
    if node is None:
        return {"status": "error", "error": "camera_not_found",
                "message": "camera path not found: " + str(camera_path)}
    try:
        viewport.setCamera(node)
    except Exception as error:
        return {"status": "error", "error": "set_camera_failed",
                "message": str(error)}
    return {"status": "success", "camera": node.path()}


def set_viewport_display(hou, display_set, shaded_mode):
    """设置 viewport display set 与 shaded mode（白名单 → 真实 HOM enum）。"""
    scene_viewer = _resolve_scene_viewer(hou)
    if scene_viewer is None:
        return _headless_warning()
    viewport = _current_geometry_viewport(scene_viewer)
    if viewport is None:
        return _headless_warning()
    set_token = str(display_set or "").strip().lower()
    mode_token = str(shaded_mode or "").strip().lower()
    set_attr_name = _DISPLAY_SET_MAP.get(set_token)
    if set_attr_name is None:
        return {"status": "error", "error": "unsupported_display_set",
                "message": "display_set must be one of: " +
                ", ".join(sorted(_DISPLAY_SET_MAP))}
    mode_attr_name = _SHADED_MODE_MAP.get(mode_token)
    if mode_attr_name is None:
        return {"status": "error", "error": "unsupported_shaded_mode",
                "message": "shaded_mode must be one of: " +
                ", ".join(sorted(_SHADED_MODE_MAP))}
    display_set_enum = getattr(hou.displaySetType, set_attr_name, None)
    shaded_mode_enum = getattr(hou.shadingMode, mode_attr_name, None)
    if display_set_enum is None or shaded_mode_enum is None:
        return {"status": "error", "error": "enum_unavailable",
                "message": "Houdini version missing displaySetType/shadingMode enum"}
    try:
        viewport.settings().displaySet(display_set_enum).setShadedMode(
            shaded_mode_enum)
    except Exception as error:
        return {"status": "error", "error": "set_display_failed",
                "message": str(error)}
    return {"status": "success", "display_set": set_token,
            "shaded_mode": mode_token}


def _is_lop_context(hou):
    """判断当前 pwd 是否在 LOP network 内。"""
    try:
        pwd = hou.pwd()
        if pwd is None:
            return False
        cat = pwd.childTypeCategory()
        return cat is not None and cat.name().lower() == "lop"
    except Exception:
        return False


def set_viewport_renderer(hou, renderer):
    """LOP SceneViewer Hydra renderer 切换；非 LOP 返 ``_warning``。

    renderer 必须是 ``sceneViewer.hydraRenderers()`` 中存在的 identifier；
    不可用时报 ``renderer_unavailable``，不做 fallback。
    """
    scene_viewer = _resolve_scene_viewer(hou)
    if scene_viewer is None:
        return _headless_warning()
    if not _is_lop_context(hou):
        return _headless_warning()
    identifier = str(renderer or "").strip()
    if not identifier:
        return {"status": "error", "error": "renderer_required",
                "message": "renderer identifier must be non-empty"}
    try:
        available = list(scene_viewer.hydraRenderers() or [])
    except Exception as error:
        return {"status": "error", "error": "renderer_query_failed",
                "message": str(error)}
    if identifier not in available:
        return {"status": "error", "error": "renderer_unavailable",
                "message": "renderer not in hydraRenderers(): " + identifier,
                "available": available}
    try:
        scene_viewer.setHydraRenderer(identifier)
    except Exception as error:
        return {"status": "error", "error": "set_renderer_failed",
                "message": str(error)}
    current = ""
    try:
        current = str(scene_viewer.currentHydraRenderer() or "")
    except Exception:
        pass
    return {"status": "success", "renderer": identifier,
            "current": current}


def frame_selection(hou):
    """调 ``viewport.frameSelected()``；无 GUI 返 ``_warning``。"""
    scene_viewer = _resolve_scene_viewer(hou)
    if scene_viewer is None:
        return _headless_warning()
    viewport = _current_geometry_viewport(scene_viewer)
    if viewport is None:
        return _headless_warning()
    try:
        viewport.frameSelected()
    except Exception as error:
        return {"status": "error", "error": "frame_selection_failed",
                "message": str(error)}
    return {"status": "success", "action": "frame_selection"}


def frame_all(hou):
    """调 ``viewport.frameAll()``；无 GUI 返 ``_warning``。"""
    scene_viewer = _resolve_scene_viewer(hou)
    if scene_viewer is None:
        return _headless_warning()
    viewport = _current_geometry_viewport(scene_viewer)
    if viewport is None:
        return _headless_warning()
    try:
        viewport.frameAll()
    except Exception as error:
        return {"status": "error", "error": "frame_all_failed",
                "message": str(error)}
    return {"status": "success", "action": "frame_all"}


def set_viewport_direction(hou, direction):
    """将白名单方向映射到 ``hou.geometryViewportType`` 并调 ``changeType``。"""
    scene_viewer = _resolve_scene_viewer(hou)
    if scene_viewer is None:
        return _headless_warning()
    viewport = _current_geometry_viewport(scene_viewer)
    if viewport is None:
        return _headless_warning()
    token = str(direction or "").strip().lower()
    attr_name = _GEOMETRY_VIEWPORT_TYPE_MAP.get(token)
    if attr_name is None:
        return {"status": "error", "error": "unsupported_direction",
                "message": "direction must be one of: " +
                ", ".join(sorted(_GEOMETRY_VIEWPORT_TYPE_MAP))}
    enum_value = getattr(hou.geometryViewportType, attr_name, None)
    if enum_value is None:
        return {"status": "error", "error": "enum_unavailable",
                "message": "Houdini version missing geometryViewportType enum"}
    try:
        viewport.changeType(enum_value)
    except Exception as error:
        return {"status": "error", "error": "change_type_failed",
                "message": str(error)}
    return {"status": "success", "direction": token}


def set_current_network(hou, path):
    """验证节点存在后调 NetworkEditor.pane.cd(path)；无 NetworkEditor 返 warning。"""
    node = hou.node(path) if path else None
    if node is None:
        return {"status": "error", "error": "node_not_found",
                "message": "node path not found: " + str(path)}
    pane = None
    try:
        ui = getattr(hou, "ui", None)
        if ui is not None:
            pane = ui.paneTabOfType(hou.paneTabType.NetworkEditor)
    except Exception:
        pane = None
    if pane is None:
        try:
            for desktop in hou.ui.desktops():
                candidate = desktop.paneTabOfType(
                    hou.paneTabType.NetworkEditor)
                if candidate is not None:
                    pane = candidate
                    break
        except Exception:
            pane = None
    if pane is None:
        return _headless_warning()
    try:
        pane.cd(node.path())
    except Exception as error:
        return {"status": "error", "error": "cd_failed",
                "message": str(error)}
    return {"status": "success", "path": node.path()}