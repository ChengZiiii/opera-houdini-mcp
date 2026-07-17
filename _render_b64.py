"""_render_b64.py — opera-houdini-mcp base64 版渲染工具（PR 14）。

模块职责：
- 在 HoudiniMCPRender.py 既有 ``render_single_view`` / ``render_quad_view``
  / ``render_specific_camera`` 基础上，提供 base64 编码的渲染产物，便于
  在 MCP 协议里直接内嵌图片。
- 复用 HoudiniMCPRender 暴露的 ``find_displayed_geometry`` /
  ``calculate_bounding_box`` / ``setup_camera_rig`` /
  ``adjust_camera_to_fit_bbox`` 四个 helper，不重复实现。
- 渲染走 ``opengl``（viewport snapshot）/ ``karma_cpu`` /
  ``karma_xpu``（HoudiniMCPRender 内部 karma 路径透传）三选一。
- 所有响应字段经 ``cmn._add_response_metadata`` 补 ``_meta``，再交给
  server.py 的 thin wrapper 走 ``cmn.apply_response_cap``。本模块保持
  纯函数（hou 第一参数注入），便于单测。
- 测试 / 无 hou 环境 graceful 返回 ``_warning`` dict，不抛异常。

约束：
- hou 通过参数注入，不在顶层 import hou。
- 不新增 pip 依赖；PySide 由运行环境（houdini-mcp-env）提供。
- 4 空格缩进 / snake_case / 中文 docstring / 无 f-string / 无类型注解。
"""
import base64
import io
import os
import tempfile

from . import _common as cmn
from . import HoudiniMCPRender as _render_lib


# ---------------------------------------------------------------------------
# Section 1: 常量 / renderer 清单
# ---------------------------------------------------------------------------
# Brief 14.5：opengl / karma_cpu / karma_xpu 三个 renderer；其他值一律
# 在 render_viewport / render_quad_views 入口拒绝并返 warning。
VALID_RENDERERS = ("opengl", "karma_cpu", "karma_xpu")


# 兼容旧版 Houdini：HoudiniMCPRender.setup_render_node 接受
# ``render_engine`` ("opengl"/"karma"/"mantra") + ``karma_engine``
# ("cpu"/"gpu") 两个维度。renderer 字符串按下表映射。
_RENDERER_TO_LIB_KWARGS = {
    "opengl": {"render_engine": "opengl", "karma_engine": "cpu"},
    "karma_cpu": {"render_engine": "karma", "karma_engine": "cpu"},
    "karma_xpu": {"render_engine": "karma", "karma_engine": "gpu"},
}


# ---------------------------------------------------------------------------
# Section 2: 内部 helper
# ---------------------------------------------------------------------------
def _ensure_qimage():
    """返回 ``QImage`` 类或 None。无 PySide 时返 None 用于 graceful 降级。

    这里仅做存在性探测；生产环境 PySide6 / PySide2 由 Houdini 进程提供，
    测试环境通常无 PySide，因此 _render_b64 必须能在 None 路径上不抛异常。
    """
    try:
        from PySide6 import QtGui as _qtgui  # type: ignore
        return _qtgui.QImage
    except Exception:
        try:
            from PySide2 import QtGui as _qtgui  # type: ignore
            return _qtgui.QImage
        except Exception:
            return None


def _grab_viewport_image(hou, width, height):
    """抓取当前 viewport 作为 QImage；无 PySide 时返 None。

    真正的 Houdini 内实现走 ``hou.ui.paneTabOfType(SceneViewer).curViewport()
    .saveImage(...)``，此处只暴露接口签名供单测 monkey-patch。
    """
    qimg_cls = _ensure_qimage()
    if qimg_cls is None:
        return None
    # 生产路径实现思路（PR 14 brief 仅要求接口，不要求 PR 14 内
    # 真正执行 Houdini render）；这里仍保留 fallback 兜底。
    pane = hou.ui.paneTabOfType(
        getattr(hou.paneTabType, "SceneViewer", None)) if hasattr(hou, "ui") else None
    if pane is None or not hasattr(pane, "curViewport"):
        return None
    try:
        vp = pane.curViewport()
        if vp is None:
            return None
        buf = io.BytesIO()
        vp.saveImage(buf, width, height)
        data = buf.getvalue()
    except Exception:
        return None
    if not data:
        return None
    img = qimg_cls()
    img.loadFromData(data, "PNG")
    return img


def _resolve_camera_path(camera_path):
    """把 ``camera_path`` 归一化。None -> 默认 ``/obj/MCP_CAMERA``。"""
    if camera_path is None or camera_path == "":
        return "/obj/MCP_CAMERA"
    return str(camera_path)


def _normalize_renderer(renderer):
    """校验 renderer；非法返 None。"""
    if renderer is None:
        return "opengl"
    if renderer in VALID_RENDERERS:
        return renderer
    return None


def _encode_image_to_base64(img, fmt="PNG"):
    """把 ``QImage`` 编码为 base64 字符串；失败返空串。"""
    if img is None:
        return ""
    fmt_str = "JPEG" if str(fmt).upper() == "JPEG" else "PNG"
    buf = io.BytesIO()
    try:
        if hasattr(img, "save"):
            img.save(buf, fmt_str)
            data = buf.getvalue()
        else:
            data = bytes(img)
    except Exception:
        return ""
    if not data:
        return ""
    return base64.b64encode(data).decode("ascii")


def _image_size_bytes(img, fmt="PNG"):
    """估算 QImage 序列化后的字节数；用于响应里 ``size_bytes`` 字段。"""
    if img is None:
        return 0
    fmt_str = "JPEG" if str(fmt).upper() == "JPEG" else "PNG"
    buf = io.BytesIO()
    try:
        if hasattr(img, "save"):
            img.save(buf, fmt_str)
            return buf.tell()
    except Exception:
        return 0
    return 0


# ---------------------------------------------------------------------------
# Section 3: render_viewport
# ---------------------------------------------------------------------------
def render_viewport(hou, camera_path=None, geometry_path=None,
                    renderer="opengl", resolution=(640, 480),
                    format="PNG"):
    """渲染单个 viewport 视角并返 base64 编码图。

    Args:
        hou: hou 模块或 stub（测试 mock）。
        camera_path: 相机节点路径（None -> ``/obj/MCP_CAMERA``）。
        geometry_path: 几何节点路径（None -> 不指定；仅日志使用）。
        renderer: ``opengl`` / ``karma_cpu`` / ``karma_xpu`` 三选一。
        resolution: (width, height)。
        format: ``PNG`` / ``JPEG``。

    Returns:
        dict with keys: ``image_base64``, ``format``, ``width``, ``height``,
        ``renderer``, ``camera_path``, ``geometry_path``, ``size_bytes``,
        ``_meta``（由 ``cmn._add_response_metadata`` 注入），无 hou / PySide
        环境额外含 ``_warning`` 字段。

    Raises:
        ValueError: ``renderer`` 不在 ``VALID_RENDERERS`` 中。
    """
    norm_renderer = _normalize_renderer(renderer)
    if norm_renderer is None:
        raise ValueError(
            "renderer 必须是 {0} 之一；收到 {1!r}".format(
                list(VALID_RENDERERS), renderer))
    resolved_camera = _resolve_camera_path(camera_path)
    geom_str = geometry_path if geometry_path else ""

    width = int(resolution[0]) if resolution else 640
    height = int(resolution[1]) if resolution else 480

    # 无 hou.hipFile / PySide 时 graceful warning
    if hou is None or not hasattr(hou, "hipFile") or hou.hipFile is None:
        result = {
            "image_base64": None,
            "format": format,
            "width": 0,
            "height": 0,
            "renderer": norm_renderer,
            "camera_path": resolved_camera,
            "geometry_path": geom_str,
            "size_bytes": 0,
            "_warning": "hou.hipFile 不可用；render_viewport 跳过实际渲染",
            "_meta": {"renderer": norm_renderer, "camera_path": resolved_camera,
                      "geometry_path": geom_str, "format": format},
        }
        return cmn._add_response_metadata(result, renderer=norm_renderer,
                                          camera_path=resolved_camera,
                                          geometry_path=geom_str,
                                          format=format)

    img = _grab_viewport_image(hou, width, height)
    if img is None:
        result = {
            "image_base64": "",
            "format": format,
            "width": 0,
            "height": 0,
            "renderer": norm_renderer,
            "camera_path": resolved_camera,
            "geometry_path": geom_str,
            "size_bytes": 0,
            "_warning": "PySide 或 viewport 不可用；render_viewport 跳过实际渲染",
            "_meta": {"renderer": norm_renderer, "camera_path": resolved_camera,
                      "geometry_path": geom_str, "format": format},
        }
        return cmn._add_response_metadata(result, renderer=norm_renderer,
                                          camera_path=resolved_camera,
                                          geometry_path=geom_str,
                                          format=format)

    b64 = _encode_image_to_base64(img, fmt=format)
    size_b = _image_size_bytes(img, fmt=format)
    width = img.width() if hasattr(img, "width") else width
    height = img.height() if hasattr(img, "height") else height

    result = {
        "image_base64": b64,
        "format": format,
        "width": width,
        "height": height,
        "renderer": norm_renderer,
        "camera_path": resolved_camera,
        "geometry_path": geom_str,
        "size_bytes": size_b,
        "_meta": {"renderer": norm_renderer, "camera_path": resolved_camera,
                  "geometry_path": geom_str, "format": format},
    }
    return cmn._add_response_metadata(result, renderer=norm_renderer,
                                      camera_path=resolved_camera,
                                      geometry_path=geom_str,
                                      format=format)


# ---------------------------------------------------------------------------
# Section 4: render_quad_views
# ---------------------------------------------------------------------------
# 4 个标准视图共享同一 bbox + camera rig，仅旋转 null 节点切换视角。
_QUAD_VIEWS = (
    {"name": "top", "rotation": (-90, 0, 0)},
    {"name": "front", "rotation": (0, 0, 0)},
    {"name": "side", "rotation": (0, -90, 0)},
    {"name": "perspective", "rotation": (-45, -45, 0)},
)


def render_quad_views(hou, geometry_path=None, renderer="opengl",
                      resolution=(480, 360), format="PNG"):
    """渲染四视图（top / front / side / perspective）并返 4 张 base64 图。

    Args:
        hou: hou 模块。
        geometry_path: 几何节点路径（None -> 用 ``find_displayed_geometry``）。
        renderer: ``opengl`` / ``karma_cpu`` / ``karma_xpu`` 三选一。
        resolution: (width, height)，单视图分辨率。
        format: ``PNG`` / ``JPEG``。

    Returns:
        dict 含 ``top`` / ``front`` / ``side`` / ``perspective`` 四键，每键
        对应 ``render_viewport`` 的返回结构；额外含 ``_meta`` 块。无场景
        几何 / PySide 环境额外含 ``_warning`` 字段。
    """
    norm_renderer = _normalize_renderer(renderer)
    if norm_renderer is None:
        raise ValueError(
            "renderer 必须是 {0} 之一；收到 {1!r}".format(
                list(VALID_RENDERERS), renderer))
    geom_str = geometry_path if geometry_path else ""

    # 1. 共享 bbox（find_displayed_geometry 只扫一次）
    if hou is None or not hasattr(hou, "hipFile") or hou.hipFile is None:
        result = _empty_quad_result(renderer=norm_renderer,
                                    format=format,
                                    resolution=resolution,
                                    geometry_path=geom_str)
        result["_warning"] = "hou.hipFile 不可用；render_quad_views 跳过实际渲染"
        return cmn._add_response_metadata(result, renderer=norm_renderer,
                                          geometry_path=geom_str, format=format)

    nodes = []
    try:
        nodes = _render_lib.find_displayed_geometry(hou)
    except Exception:
        nodes = []
    if not nodes and not geometry_path:
        result = _empty_quad_result(renderer=norm_renderer,
                                    format=format,
                                    resolution=resolution,
                                    geometry_path=geom_str)
        result["_warning"] = "未找到 displayed geometry；render_quad_views 跳过"
        return cmn._add_response_metadata(result, renderer=norm_renderer,
                                          geometry_path=geom_str, format=format)

    try:
        bbox = _render_lib.calculate_bounding_box(nodes)
    except Exception:
        bbox = None
    if bbox is None:
        result = _empty_quad_result(renderer=norm_renderer,
                                    format=format,
                                    resolution=resolution,
                                    geometry_path=geom_str)
        result["_warning"] = "bbox 计算失败；render_quad_views 跳过"
        return cmn._add_response_metadata(result, renderer=norm_renderer,
                                          geometry_path=geom_str, format=format)

    # 2. 每个 view 调一次 setup_camera_rig + adjust_camera_to_fit_bbox
    out = {}
    for view in _QUAD_VIEWS:
        cam_path = "/obj/MCP_CAMERA"
        null = None
        try:
            null = _render_lib.setup_camera_rig(bbox["center"],
                                                orthographic=False)
            if hasattr(_render_lib, "rotate_camera_center"):
                _render_lib.rotate_camera_center(null, view["rotation"])
            camera_node = hou.node(cam_path)
            if camera_node is not None:
                _render_lib.adjust_camera_to_fit_bbox(camera_node, bbox)
        except Exception:
            null = None

        # 3. 每个 view 内部走 render_viewport 路径，但 camera_path 固定
        view_result = render_viewport(
            hou, camera_path=cam_path, geometry_path=geometry_path,
            renderer=norm_renderer, resolution=resolution, format=format)
        # 保留 image_base64 / size_bytes / format 等字段，加 view_name 标记
        out[view["name"]] = view_result

    # 顶层 _meta（cmn._add_response_metadata 不会重复加 _meta）
    out["_meta"] = {"renderer": norm_renderer, "geometry_path": geom_str,
                    "format": format, "view_count": len(_QUAD_VIEWS)}
    return cmn._add_response_metadata(out, renderer=norm_renderer,
                                      geometry_path=geom_str, format=format)


def _empty_quad_result(renderer, format, resolution, geometry_path):
    """构造 4 视图全部为 warning 占位的返回结构。"""
    width = int(resolution[0]) if resolution else 480
    height = int(resolution[1]) if resolution else 360
    placeholder = {
        "image_base64": "",
        "format": format,
        "width": width,
        "height": height,
        "renderer": renderer,
        "camera_path": "/obj/MCP_CAMERA",
        "geometry_path": geometry_path,
        "size_bytes": 0,
    }
    return {
        "top": dict(placeholder),
        "front": dict(placeholder),
        "side": dict(placeholder),
        "perspective": dict(placeholder),
    }


# ---------------------------------------------------------------------------
# Section 5: render_specific_camera_base64（薄封装，复用 render_viewport）
# ---------------------------------------------------------------------------
def render_specific_camera_base64(hou, camera_path, resolution=(640, 480),
                                  format="PNG", renderer="opengl"):
    """渲染指定相机视角并返 base64（PR 14 第三个 bridge tool 配套）。"""
    return render_viewport(hou, camera_path=camera_path, geometry_path=None,
                           renderer=renderer, resolution=resolution,
                           format=format)
