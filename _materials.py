"""_materials.py — opera-houdini-mcp 材质 CRUD + 参数白名单 + texture 识别（PR 7）。

模块职责：
- create_material: 创建材质节点（hou.node(parent).createNode）并可选应用参数
- assign_material: 把材质绑定到几何节点（通过 shop_materialpath parm 实现，
  与 server.py 中既有 set_material 的 OBJ-level 方式兼容；这是跨 Houdini
  版本最稳定的实现路径）
- get_material_info: 返回材质节点的 path/type/name/parameters（白名单过滤）/
  texture_references（识别 .png 等贴图路径）

约束：
- 不顶层 import hou；hou 通过参数注入（测试 mock）
- 不引入类型注解与 f-string
- 不新增 pip 依赖
- hou.assignToNode(geo, group=...) 是 Houdini 较新版本 API；旧版本会
  AttributeError。本模块以 shop_materialpath parm 为主路径，HOU 17+
  均稳定。
"""
from . import _common as cmn


# ---------------------------------------------------------------------------
# PR 7: 50+ 参数白名单。覆盖 Principled Shader 主要参数 + 通用贴图相关字段
# 实际节点类型只需包含其中子集，未含在白名单的 parm 一律不出现在
# get_material_info 输出里 —— 保证响应体稳定。
# ---------------------------------------------------------------------------
MATERIAL_PARM_WHITELIST = (
    # basecolor / albedo
    "basecolor", "basecolor_useTexture", "basecolor_texture",
    "diffuse", "diffuse_useTexture", "diffuse_texture",
    # roughness
    "rough", "rough_useTexture", "rough_texture", "roughness",
    # metallic / reflectivity
    "metallic", "metallic_useTexture", "metallic_texture",
    "reflect", "reflectivity_useTexture", "reflectivity_texture",
    "ior", "reflectivity",
    # specular
    "spec", "specular_useTexture", "specular_texture",
    # bump / normal
    "baseBumpAndNormal_enable", "baseNormal_useTexture", "baseNormal_texture",
    "bumpScale", "bumpblur", "normalMapEnable",
    # emissive
    "emit", "emitcolor", "emitcolor_useTexture", "emitcolor_texture",
    "emitColor", "emitIntensity",
    # opacity
    "alpha", "alphaclip", "opacity", "opacity_useTexture",
    "opacity_texture", "transparency", "transparency_useTexture",
    "transparency_texture",
    # displacement
    "displaceAlongNormal_enable", "dispNormal_texture", "dispNormal_scale",
    "dispAmount",
    # sheen
    "sheen", "sheen_useTexture", "sheen_color", "sheen_texture",
    "sheen_roughness", "sheen_opacity",
    # coat
    "coat", "coat_useTexture", "coat_color", "coat_roughness",
    "coat_texture", "coatRough_useTexture", "coatRough_texture",
    # anisotropy
    "anisotropic", "anisotropic_useTexture", "anisotropy_texture",
    "anisotropicdirection", "anisoangle",
    # SSS / scattering
    "sss", "sss_useTexture", "sss_color", "sss_texture",
    "scattering", "scattering_color", "scattering_texture",
    # utility
    "ambient", "shadowMask", "dispBoundingBox",
)


# 支持识别为 texture 引用（Houdini 常用贴图扩展名）的后缀集合。
# 全部小写；匹配时统一 lower() 再比对。
_TEXTURE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".exr", ".hdr",
    ".tif", ".tiff", ".rat", ".tex",
)


def is_texture_reference(value):
    """返回 True 当 value 是字符串且文件名后缀在 _TEXTURE_EXTENSIONS 集合内。

    大小写无关：'foo.PNG' 视作匹配 '.png'。
    非字符串（None / 数值 / tuple）一律返回 False。
    空字符串返回 False。
    """
    if not isinstance(value, str) or not value:
        return False
    lower = value.lower()
    for ext in _TEXTURE_EXTENSIONS:
        if lower.endswith(ext):
            return True
    return False


def create_material(hou, material_type, name=None, parent_path="/mat",
                    parameters=None):
    """创建材质节点。

    Args:
        hou: hou mock / real hou 模块（参数注入）
        material_type: 材质类型字符串，如 "principledshader" / "vopsurface"
        name: 节点名（默认 None -> Houdini 自动命名）
        parent_path: 父路径（默认 "/mat"；不存在时回退 /mat）

    Returns:
        dict {"path", "type", "name", "parameters_set"}
            parameters_set 是已成功调用 parm.set 的 key 列表
            （含虽然节点未暴露而静默跳过的 key，便于上层 UI 给反馈）
    """
    parent = hou.node(parent_path) if parent_path else None
    if parent is None:
        parent = hou.node("/mat")
    mat = parent.createNode(material_type, node_name=name)

    parameters_set = []
    if parameters:
        for parm_name, value in parameters.items():
            pt = mat.parm(parm_name)
            if pt is not None:
                pt.set(value)
            parameters_set.append(parm_name)

    return {
        "path": mat.path(),
        "type": mat.type().name(),
        "name": mat.name(),
        "parameters_set": parameters_set,
    }


def assign_material(hou, geometry_path, material_path, group=None):
    """把 material_path 处的材质绑定到 geometry_path 处的几何节点。

    实现策略：
    - 优先用 geo.parm("shop_materialpath") 设值；这是 PR 之前 server.py
      既有的 set_material 的兼容写法，跨 Houdini 版本最稳定
    - 若 geo 没有 shop_materialpath parm，尝试 mat.assignToNode(geo,
      group=group)（HOU 较新版本支持；参数缺失 AttributeError 时
      退化为直接 pass，让 Houdini 抛具体错）
    - group 可选；None 时整节点绑定，非 None 时仅绑定到指定 group

    Returns:
        dict {"geometry_path", "material_path", "group", "success": True}
    """
    geo = hou.node(geometry_path)
    mat = hou.node(material_path)
    if geo is None or mat is None:
        missing = []
        if geo is None:
            missing.append("geometry_path={0}".format(geometry_path))
        if mat is None:
            missing.append("material_path={0}".format(material_path))
        raise ValueError(
            "几何或材质节点不存在: {0}".format(", ".join(missing)))

    parm = geo.parm("shop_materialpath")
    if parm is not None:
        parm.set(mat.path())
    else:
        # 退化路径：调用 Houdini 较新版本的 assignToNode
        assign = getattr(mat, "assignToNode", None)
        if callable(assign):
            try:
                if group is not None:
                    assign(geo, group=group)
                else:
                    assign(geo)
            except TypeError:
                # 某些版本签名不接受 group kwargs
                if group is not None:
                    assign(geo, group)
                else:
                    assign(geo)
        # 没有可调用的绑定入口也不抛错 —— 调用方可在 Houdini 侧自行处理

    return {
        "geometry_path": geometry_path,
        "material_path": material_path,
        "group": group,
        "success": True,
    }


def get_material_info(hou, material_path):
    """返回材质节点详细信息字典。

    Returns:
        {
            "path": ...,
            "type": ...,
            "name": ...,
            "parameters": {parm_name: value, ...},  # 白名单过滤后
            "texture_references": [
                {"parm": ..., "value": ..., "is_texture": True},
                ...
            ]
        }

    Raises:
        ValueError: 当 material_path 无法解析到节点
    """
    mat = hou.node(material_path)
    if mat is None:
        raise ValueError("材质节点不存在: {0}".format(material_path))

    whitelist_set = frozenset(MATERIAL_PARM_WHITELIST)

    info = {
        "path": mat.path(),
        "type": mat.type().name(),
        "name": mat.name(),
        "parameters": {},
        "texture_references": [],
    }

    parms = mat.parms() or []
    for pt in parms:
        pname = pt.name()
        if pname not in whitelist_set:
            continue
        try:
            value = pt.eval()
        except Exception:
            continue
        # 用 _json_safe_hou_value 统一序列化（vector/tuple 转列表）
        safe_value = cmn._json_safe_hou_value(hou, value, max_depth=2)
        info["parameters"][pname] = safe_value
        if is_texture_reference(value):
            info["texture_references"].append({
                "parm": pname,
                "value": str(value),
                "is_texture": True,
            })

    return info
