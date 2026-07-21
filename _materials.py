"""_materials.py — opera-houdini-mcp 材质 CRUD + 参数白名单 + texture 识别（PR 7）。

模块职责：
- create_material: 创建材质节点（hou.node(parent).createNode）并可选应用参数
- assign_material: 把材质绑定到几何节点。H21 compat audit (A4) 后实现：
  - group=None：通过 shop_materialpath parm 实现（跨 Houdini 版本最稳定）
  - group!=None：优先 mat.assignToNode（老 Houdini），失败走 Material SOP
    子节点（H21+ 标准 per-group 绑定，assignToNode 在 H21 已移除）
- get_material_info: 返回材质节点的 path/type/name/parameters（白名单过滤）/
  texture_references（识别 .png 等贴图路径）

约束：
- 不顶层 import hou；hou 通过参数注入（测试 mock）
- 不引入类型注解与 f-string
- 不新增 pip 依赖
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
    # H21+ principledshader 2.0 basecolor multi-parm sub-keys
    "basecolorr", "basecolorg", "basecolorb",
    "diffuse", "diffuse_useTexture", "diffuse_texture",
    # H21+ diffuse multi-parm sub-keys (mtlx alias)
    "diffuser", "diffuseg", "diffuseb",
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
    # H21+ emitcolor multi-parm sub-keys
    "emitcolorr", "emitcolorg", "emitcolorb",
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
    # H21+ sheen_color multi-parm sub-keys
    "sheenr", "sheeng", "sheenb",
    # coat
    "coat", "coat_useTexture", "coat_color", "coat_roughness",
    "coat_texture", "coatRough_useTexture", "coatRough_texture",
    # H21+ coat_color multi-parm sub-keys
    "coat_colorr", "coat_colorg", "coat_colorb",
    # anisotropy
    "anisotropic", "anisotropic_useTexture", "anisotropy_texture",
    "anisotropicdirection", "anisoangle",
    # SSS / scattering
    "sss", "sss_useTexture", "sss_color", "sss_texture",
    "scattering", "scattering_color", "scattering_texture",
    # H21+ sss_color multi-parm sub-keys
    "sssr", "sssg", "sssb",
    # H21+ scattering_color multi-parm sub-keys
    "scattering_colorr", "scattering_colorg", "scattering_colorb",
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

    实现策略（H21 compat audit, A4）：
    - group 非 None 时（按优先级降级）：
        1. 优先尝试 mat.assignToNode(geo, group=group) —— 老版本 Houdini
           可能有此 API；成功即返回（via="assignToNode"）。
        2. assignToNode 不存在或调用失败 -> H21+ 路径：在 geo 容器下创建
           Material SOP 子节点，设其 group 过滤 parm + shop_materialpath
           parm（via="material_sop_child"）。这是 H21+ per-group 绑定的
           标准做法（assignToNode 在 H21 已移除）。
        3. Material SOP 创建失败 -> 终极兜底：在 geo 上设 shop_materialpath
           parm（与 group=None 路径相同），返回 via="fallback_shop_materialpath"
           + warning 字段说明 group 信息丢失。不硬失败。
        4. shop_materialpath parm 也不存在 -> 抛 ValueError。
    - group 为 None 时：保持既有 shop_materialpath 优先策略不变
      （跨 Houdini 版本最稳定）。

    Args:
        hou: hou mock / real hou 模块（参数注入）
        geometry_path: SOP / OBJ 几何节点路径
        material_path: 材质节点路径
        group: 可选，primitive group 名；None 时整节点绑定

    Returns:
        dict 至少含 {"geometry_path", "material_path", "group", "success"}
        + "via" 标记走哪条路径（assignToNode / material_sop_child /
        fallback_shop_materialpath）。material_sop_child 路径额外返回
        "material_sop_path" / "group_parm" / "material_parm" 字段。

    Raises:
        ValueError: 节点不存在；或所有绑定路径（包括最终兜底）都失败
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

    if group is not None:
        # ---- group 绑定：assignToNode 优先（老 Houdini），失败走 Material SOP ----
        assign = getattr(mat, "assignToNode", None)
        if callable(assign):
            try:
                assign(geo, group=group)
                return {
                    "geometry_path": geometry_path,
                    "material_path": material_path,
                    "group": group,
                    "success": True,
                    "via": "assignToNode",
                }
            except Exception:
                # assignToNode 存在但调用失败（如 TypeError 拒绝 group kwarg
                # 或 RuntimeError）—— 降级到 Material SOP 路径，不再硬抛
                pass

        # H21+ per-group 绑定：Material SOP 子节点
        try:
            sop_node, group_parm, mat_parm = _bind_material_to_group_via_sop(
                hou, geo, mat, group)
            return {
                "geometry_path": geometry_path,
                "material_path": material_path,
                "group": group,
                "success": True,
                "via": "material_sop_child",
                "material_sop_path": sop_node.path(),
                "group_parm": group_parm,
                "material_parm": mat_parm,
            }
        except Exception as sop_err:
            # 终极兜底：在 geo 上设 shop_materialpath（group 信息会丢失，
            # 但能保证材质生效到整节点；调用方据 warning 决定是否补刀）
            parm = geo.parm("shop_materialpath")
            if parm is not None:
                parm.set(mat.path())
                return {
                    "geometry_path": geometry_path,
                    "material_path": material_path,
                    "group": group,
                    "success": True,
                    "via": "fallback_shop_materialpath",
                    "warning": (
                        "Material SOP creation failed ({0}); group={1} "
                        "NOT applied - assigned to whole geometry"
                    ).format(str(sop_err), group),
                }
            raise ValueError(
                "Material SOP 创建失败且无 shop_materialpath 兜底: {0}"
                .format(sop_err))
    else:
        # ---- 整节点绑定：保留既有 shop_materialpath 优先策略（不变） ----
        parm = geo.parm("shop_materialpath")
        if parm is not None:
            parm.set(mat.path())
        else:
            assign = getattr(mat, "assignToNode", None)
            if not callable(assign):
                raise ValueError(
                    "无可用绑定入口（shop_materialpath parm 不存在且"
                    "材质节点无 assignToNode）")
            try:
                assign(geo)
            except Exception as e:
                raise ValueError("assignToNode 调用失败: {0}".format(e))

    return {
        "geometry_path": geometry_path,
        "material_path": material_path,
        "group": group,
        "success": True,
    }


# ---------------------------------------------------------------------------
# H21 compat audit (A4): per-group 绑定的 Material SOP 子节点路径
# 设计依据 SideFX H22 Material SOP 文档：
#   https://www.sidefx.com/docs/houdini22.0/nodes/sop/material.html
# Material SOP 用 "Number of materials" multiparm，每个 slot 含 Group + Material
# 两个 parm。num_materials=1 时 slot 1 的 parm 名为 group1 / shop_materialpath1。
# 老版本 Material SOP 可能有顶层 group / shop_materialpath（无后缀），代码同时
# 兼容。
# ---------------------------------------------------------------------------

def _resolve_material_sop_container(geo):
    """决定 Material SOP 应该创建在哪个容器下。

    - geo 是 OBJ 容器（Object category）：直接返回 geo（在其内创建子 SOP）
    - geo 是 SOP / 其他：返回 geo.parent()（在父 OBJ 容器下创建兄弟节点）
    - parent() 不可用或返回 None：回退到 geo 自身（best-effort）
    """
    try:
        cat_name = geo.type().category().name()
    except Exception:
        cat_name = None
    if cat_name == "Object":
        return geo
    # SOP / 其他类型 —— 找父容器
    parent_attr = getattr(geo, "parent", None)
    if callable(parent_attr):
        try:
            parent_node = parent_attr()
            if parent_node is not None:
                return parent_node
        except Exception:
            pass
    return geo  # 最后兜底


def _unique_material_sop_name(container, base="material"):
    """在 container 下找一个未占用的 Material SOP 名（material / material1 / ...）。

    通过 children() 收集现有子节点名，避开冲突。
    """
    existing_names = set()
    children_attr = getattr(container, "children", None)
    if callable(children_attr):
        try:
            for child in (children_attr() or []):
                try:
                    existing_names.add(child.name())
                except Exception:
                    pass
        except Exception:
            pass

    if base not in existing_names:
        return base
    i = 1
    while True:
        cand = "{0}{1}".format(base, i)
        if cand not in existing_names:
            return cand
        i += 1


def _set_first_existing_parm(node, candidate_names, value):
    """在 node 上按 candidate_names 顺序找第一个存在的 parm 并 set(value)。

    Returns:
        命中的 parm 名（str）；若全部不存在返回 None。
    """
    for name in candidate_names:
        try:
            pt = node.parm(name)
        except Exception:
            pt = None
        if pt is not None:
            pt.set(value)
            return name
    return None


def _wire_material_sop_after_display(geo, material_sop, container):
    """Best-effort：把 Material SOP 的 input 0 接到当前 display SOP 之后。

    - geo 是 OBJ 容器：用 geo.displayNode() 找当前 display SOP 作为源
    - geo 自身是 SOP：直接用 geo 作为源（Material SOP 是 geo 的兄弟）
    - 任何步骤失败都静默跳过 —— wiring 是 best-effort，不影响材质生效
    """
    src = None
    # OBJ 容器：用 displayNode() 找源
    display_method = getattr(geo, "displayNode", None)
    if callable(display_method):
        try:
            src = display_method()
        except Exception:
            src = None
    # geo 自身是 SOP（container 是其 parent）—— 直接用 geo 作为源
    if src is None:
        try:
            cat_name = geo.type().category().name()
        except Exception:
            cat_name = None
        if cat_name and cat_name != "Object":
            src = geo
    if src is None:
        return  # 无源可接

    set_input = getattr(material_sop, "setInput", None)
    if callable(set_input):
        try:
            set_input(0, src)
        except Exception:
            pass

    # 把 Material SOP 设为新 display flag（让 viewport 立即看到效果）
    set_display = getattr(material_sop, "setDisplayFlag", None)
    if callable(set_display):
        try:
            set_display(True)
        except Exception:
            pass


def _bind_material_to_group_via_sop(hou, geo, mat, group):
    """H21+ per-group 材质绑定：创建 Material SOP 子节点。

    流程：
        1. 解析容器（OBJ 容器自身或 SOP 的父）
        2. 在容器下找唯一名（material / material1 / ...）createNode("material")
        3. 设 group 过滤 parm（group1 / group）
        4. 设 material 路径 parm（shop_materialpath1 / shop_materialpath）
        5. Best-effort：wire Material SOP 到当前 display SOP 之后

    Args:
        hou: hou 模块（参数注入）
        geo: 几何节点（OBJ 容器或 SOP）
        mat: 材质节点
        group: primitive group 名

    Returns:
        (material_sop_node, group_parm_name, material_parm_name)

    Raises:
        RuntimeError: Material SOP 创建失败，或 group/material parm 都不存在
    """
    container = _resolve_material_sop_container(geo)
    sop_name = _unique_material_sop_name(container, base="material")

    create_attr = getattr(container, "createNode", None)
    if not callable(create_attr):
        raise RuntimeError(
            "container has no createNode method: {0}".format(container))
    try:
        material_sop = create_attr("material", node_name=sop_name)
    except Exception as e:
        raise RuntimeError(
            "createNode('material') failed: {0}".format(e))

    # group 过滤 parm：H21+ multiparm slot 1 是 group1；老版本是 group（无后缀）
    group_parm_name = _set_first_existing_parm(
        material_sop, ["group1", "group"], group)
    if group_parm_name is None:
        raise RuntimeError(
            "Material SOP exposes neither group1 nor group parm")

    # material 路径 parm：同理 slot 1 是 shop_materialpath1
    mat_parm_name = _set_first_existing_parm(
        material_sop,
        ["shop_materialpath1", "shop_materialpath"],
        mat.path())
    if mat_parm_name is None:
        raise RuntimeError(
            "Material SOP exposes neither shop_materialpath1 nor "
            "shop_materialpath parm")

    # Best-effort wiring（失败不影响返回）
    try:
        _wire_material_sop_after_display(geo, material_sop, container)
    except Exception:
        pass

    return material_sop, group_parm_name, mat_parm_name


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
