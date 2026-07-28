"""_usd.py — opera-houdini-mcp USD/Solaris 工具（add-usd-solaris-tools）。

提供 15 个工具（3 MUTATING + 12 NO_UNDO；本 change 的 READ_ONLY 为空）：

读工具（NO_UNDO_COMMANDS，获取 composed stage 可能触发 LOP cook）：
- ``lop_stage_info``：composed stage 级元数据（upAxis / metersPerUnit /
  framesPerSecond / 默认 prim / prim 计数摘要）。
- ``lop_prim_get``：单个 prim 的 type / active / loaded / kind + 有界属性。
- ``lop_prim_search``：按 name / type_name 搜索 prim（路径/名称/类型）。
- ``lop_layer_info``：layer stack 摘要（root layer identifier / sublayer 数）。
- ``list_usd_prims``：受 ``max_depth`` / ``max_prims`` 限制的 prim 遍历。
- ``get_usd_attribute``：单个属性值 + 类型名。
- ``get_usd_prim_stats``：prim active / loaded / defined / abstract / instance。
- ``get_last_modified_prims``：无可证明来源时一律返回 ``unsupported``，
  不伪造「最近修改」。
- ``get_usd_composition``：composition arc 摘要（受 ``max_arcs`` 限制）。
- ``get_usd_variants``：variant set 名称与选择。
- ``inspect_usd_layer``：layer 自定义元数据 / sublayer 路径。
- ``list_lights``：优先 ``UsdLux.LightAPI``，再以实际存在的具体 light
  schema ``prim.IsA(schema)`` 补充；不依赖 ``UsdLux.Light`` 基类。

写工具（MUTATING_COMMANDS，创建 / 配置 / 连接 LOP authoring 节点）：
- ``lop_import``：创建 Reference 或 Sublayer LOP，按该版本探针固化的参数
  schema 设置 file / prim path；**不**直接修改 stage layer stack。
- ``set_usd_attribute``：创建白名单属性 authoring LOP（Edit Properties），
  按其真实参数 schema author；adapter 或 value 无法无损映射时返回
  ``unsupported``，**禁止** fallback 到 composed stage mutation。
- ``create_lop_node``：在可编辑 LOP parent 下创建指定 node type 节点。

模块职责与约束（R1-R14）：
- R4：零新 pip 依赖；仅 Python 3.12 标准库 + Houdini 内置 ``pxr``。
- R6：所有返回过 ``apply_response_cap``。
- R9：node type / 参数 schema / value type 映射由 live probe 固化（``hou.
  lopNodeTypeCategory().nodeTypes()`` 探针），不用模糊参数名猜测。
- R10：LOP mutation 走 MUTATING_COMMANDS（可 undo 的 hip 网络编辑）；
  composed stage 查询走 NO_UNDO_COMMANDS；pxr stage/prim/layer mutation
  API（Set/DefinePrim/编辑 layer）一律**不**直接调用，只经 authoring adapter。
- hou 通过第一参数注入，顶层不 ``import hou``。
- ``pxr`` 惰性导入（``_get_pxr``）；非 Houdini 环境返回结构化 warning。
- ``hou.LopNode.stage()`` 是 composed、只读观察面；不存在 ``hou.LOPStage``。
- 遍历受 ``max_depth`` / ``max_prims`` / ``max_attributes`` 限制；值先转
  JSON-safe 再过 ``apply_response_cap``。
- 不引入 f-string / 类型注解，匹配既有 server.py 风格。
- ``get_last_modified_prims`` 无可证明来源时返回 ``unsupported``，不伪造。
"""
import math
import os

from . import _common as cmn


# ---------------------------------------------------------------------------
# Section 1: 常量 / authoring adapter 候选（node type + 参数 schema 白名单）
# ---------------------------------------------------------------------------
# D2/R9：adapter 候选的 node_type 在运行时由 ``hou.lopNodeTypeCategory
# ().nodeTypes()`` 探针校验存在性；首个存在项的参数 schema 固定使用，
# 不尝试多个 parm 名猜测。H21.0.596 实测：reference / sublayer /
# editproperties 三个 node type 均存在，参数名如下。
_REFERENCE_ADAPTERS = (
    {
        "node_type": "reference",
        "primpath_parm": "primpath",
        "createprims_parm": "createprims",
        "file_count_parm": "num_files",
        "file_enable_parm": "enable1",
        "filepath_parm": "filepath1",
        "refprimpath_parm": "filerefprimpath1",
    },
)
_SUBLAYER_ADAPTERS = (
    {
        "node_type": "sublayer",
        "file_count_parm": "num_files",
        "file_enable_parm": "enable1",
        "filepath_parm": "filepath1",
    },
)
# 属性 authoring adapter：Edit Properties LOP。H21.0.596 实测其静态参数
# 仅覆盖 prim 创建 / 定型（primpath / createprims / primtype / primkind）；
# 任意 attribute *value* 的编辑经其交互 editinterface，无干净静态 value
# 参数 → value 映射不可用时返回 ``unsupported``（R10：不 fallback 到
# pxr mutation）。
_ATTR_ADAPTERS = (
    {
        "node_type": "editproperties",
        "primpattern_parm": "primpattern",
        "primpath_parm": "primpath",
        "createprims_parm": "createprims",
        "primtype_parm": "primtype",
        "primkind_parm": "primkind",
        "value_parm": None,
    },
)

# ``set_usd_attribute`` 接受的 value type 白名单（能无损映射到 JSON）。
_VALID_ATTR_TYPES = frozenset({"float", "int", "string", "vector"})

# ``import_type`` 白名单。
_VALID_IMPORT_TYPES = frozenset({"reference", "sublayer"})

# 遍历 cap 默认值（读工具）。
_DEFAULT_MAX_DEPTH = 5
_DEFAULT_MAX_PRIMS = 500
_DEFAULT_MAX_ATTRIBUTES = 100
_DEFAULT_MAX_ARCS = 50
_DEFAULT_MAX_LAYERS = 20
_DEFAULT_MAX_LIGHTS = 200


# ---------------------------------------------------------------------------
# Section 2: 错误 / success envelope helper
# ---------------------------------------------------------------------------
def _error(code, message, details=None):
    payload = {"status": "error", "error": {"code": code,
                                            "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _success(data):
    return {"status": "success", "result": data}


def _unsupported(code, message, details=None):
    """spec-compliant ``unsupported`` envelope（不伪造、不静默 fallback）。"""
    payload = {"status": "unsupported", "error": {"code": code,
                                                  "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


# ---------------------------------------------------------------------------
# Section 3: pxr 惰性导入 + capability / version 探针
# ---------------------------------------------------------------------------
def _get_pxr():
    """惰性导入 ``pxr.Usd`` / ``Sdf`` / ``UsdLux``；不可用返回 None。

    仅在 Houdini server 进程内可用；测试环境注入 fake pxr 到 sys.modules。
    顶层不导入，避免非 Houdini 进程 import 失败。
    """
    try:
        from pxr import Usd, Sdf, UsdLux
    except Exception:
        return None
    return {"Usd": Usd, "Sdf": Sdf, "UsdLux": UsdLux}


# ``UsdLux`` 具体 light schema 候选；probe 仅保留实际存在者。
_LIGHT_SCHEMA_CANDIDATES = (
    "DistantLight", "SphereLight", "RectLight", "DiskLight",
    "CylinderLight", "DomeLight",
)


def _probe_capabilities(hou):
    """探测 Houdini / USD 版本与关键 feature flags。

    返回 dict（每个响应携带简化 capability 信息）：
    - ``houdini_version``：str 或 None
    - ``usd_version``：str 或 None（``Usd.GetVersion`` 若存在）
    - ``has_stage``：bool（``hou.LopNode.stage`` 是否可用）
    - ``has_light_api``：bool（``UsdLux.LightAPI`` 是否存在）
    - ``light_schemas``：list[str]（实际存在的具体 light schema 名）
    - ``has_pxr``：bool（pxr 是否可导入）
    - ``warnings``：list[str]

    版本判断由 import / hasattr / 调用探针决定，MUST NOT 仅比较版本字符串。
    """
    caps = {
        "houdini_version": None,
        "usd_version": None,
        "has_stage": False,
        "has_light_api": False,
        "light_schemas": [],
        "has_pxr": False,
        "warnings": [],
    }
    # Houdini version
    try:
        ver = hou.applicationVersion()
        caps["houdini_version"] = "%d.%d.%d" % (ver[0], ver[1], ver[2])
    except Exception:
        try:
            caps["houdini_version"] = hou.applicationVersionString()
        except Exception:
            caps["warnings"].append("houdini version unavailable")
    # LopNode.stage surface
    lop = getattr(hou, "LopNode", None)
    caps["has_stage"] = lop is not None and hasattr(lop, "stage")
    # pxr
    pxr = _get_pxr()
    if pxr is None:
        caps["warnings"].append("pxr unavailable; USD tools limited")
        return caps
    caps["has_pxr"] = True
    Usd = pxr["Usd"]
    UsdLux = pxr["UsdLux"]
    # USD version（Usd.GetVersion 若存在）
    get_version = getattr(Usd, "GetVersion", None)
    if callable(get_version):
        try:
            raw = get_version()
            caps["usd_version"] = _format_version(raw)
        except Exception:
            caps["warnings"].append("Usd.GetVersion() failed")
    else:
        caps["warnings"].append("Usd.GetVersion not available")
    # LightAPI
    caps["has_light_api"] = hasattr(UsdLux, "LightAPI")
    # 具体 schema（仅保留实际存在的）
    schemas = []
    for name in _LIGHT_SCHEMA_CANDIDATES:
        if hasattr(UsdLux, name):
            schemas.append(name)
    caps["light_schemas"] = schemas
    if not caps["has_light_api"] and not schemas:
        caps["warnings"].append("no UsdLux light detection API available")
    return caps


def _format_version(raw):
    """``Usd.GetVersion`` 返回 tuple / str → str。"""
    try:
        if isinstance(raw, (tuple, list)):
            return ".".join(str(x) for x in raw)
        return str(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Section 4: LopNode 解析 + composed stage 访问
# ---------------------------------------------------------------------------
def _resolve_lop_node(hou, path):
    """解析 LOP 节点；非 LopNode 抛 ValueError（与 server 既有契约一致）。

    **不**使用不存在的 ``hou.LOPStage``；composed stage 仅通过
    ``LopNode.stage()`` 获取。
    """
    node = hou.node(path)
    if node is None:
        raise ValueError(u"Node not found: {0}".format(path))
    lop = getattr(hou, "LopNode", None)
    if lop is not None and isinstance(node, lop):
        return node
    raise ValueError(
        u"{0} is not a LopNode; pass a LOP path".format(path))


def _get_stage(node):
    """``LopNode.stage()`` composed、只读观察面。"""
    return node.stage()


# ---------------------------------------------------------------------------
# Section 5: JSON-safe 转换（pxr 值 → JSON-friendly）
# ---------------------------------------------------------------------------
def _jsonable(value):
    """递归把 pxr / HOM 值转 JSON-safe。

    - 标量（bool/int/float/str/None）直通。
    - tuple/list 递归。
    - 其它（pxr 值 / GfVec / Sdf 类型）尝试迭代取 float，失败转 str。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    try:
        return [float(x) for x in value]
    except (TypeError, ValueError):
        try:
            return str(value)
        except Exception:
            return "<unserializable>"


def _prim_path_str(prim_path):
    """pxr.Sdf.Path / str → str。"""
    try:
        return str(prim_path)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Section 6: 内部遍历 helper（统一 cap）
# ---------------------------------------------------------------------------
def _walk_prims(stage, prim, depth, max_depth, max_prims, out):
    """深度受限地收集 prim 摘要（path/name/type/depth）。

    超出 ``max_prims`` 立即停止；``max_depth`` 截断递归。
    """
    if len(out) >= max_prims or depth > max_depth:
        return
    try:
        children = prim.GetChildren()
    except Exception:
        children = []
    for child in children:
        if len(out) >= max_prims:
            return
        try:
            name = child.GetName()
        except Exception:
            name = None
        try:
            type_name = str(child.GetTypeName())
        except Exception:
            type_name = None
        try:
            path = _prim_path_str(child.GetPath())
        except Exception:
            path = None
        out.append({"path": path, "name": name,
                    "type": type_name, "depth": depth})
        if depth < max_depth:
            _walk_prims(stage, child, depth + 1, max_depth, max_prims, out)


# ---------------------------------------------------------------------------
# Section 7: 读工具（11 个，归 NO_UNDO_COMMANDS）
# ---------------------------------------------------------------------------
def lop_stage_info(hou, node_path, max_prims=_DEFAULT_MAX_PRIMS):
    """composed stage 级元数据。

    从 ``LopNode.stage()`` 取 upAxis / metersPerUnit /
    framesPerSecond / timeCodesPerSecond / start / end timeCode /
    defaultPrim 与有界 prim 计数。所有值 JSON-safe + capability 探针。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    cap_check = _coerce_int("max_prims", max_prims)
    if cap_check.get("status") == "error":
        return cap_check
    prim_cap = cap_check["value"]
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable; cannot read stage",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None (cook may have failed)",
            details={"capability": caps}))
    data = _gather_stage_info(stage, prim_cap)
    data["capability"] = caps
    return cmn.apply_response_cap(_success(data))


def _gather_stage_info(stage, prim_cap):
    """stage 元数据 + 有界 prim 计数（纯 pxr 读）。"""
    out = {}
    # stage-level 元数据
    for key, getter in (
            ("up_axis", "GetUpAxis"),
            ("meters_per_unit", "GetMetersPerUnit"),
            ("frames_per_second", "GetFramesPerSecond"),
            ("time_codes_per_second", "GetTimeCodesPerSecond"),
            ("start_time_code", "GetStartTimeCode"),
            ("end_time_code", "GetEndTimeCode")):
        method = getattr(stage, getter, None)
        if callable(method):
            try:
                out[key] = _jsonable(method())
            except Exception:
                out[key] = None
    # defaultPrim
    try:
        root = stage.GetRootLayer()
        out["default_prim"] = getattr(root, "defaultPrim", None)
    except Exception:
        out["default_prim"] = None
    # 有界 prim 计数（遍历伪 root 的直接 + 受限递归）
    prim_count = 0
    try:
        pseudo = stage.GetPseudoRoot()
        _walk_prims(stage, pseudo, 0, _DEFAULT_MAX_DEPTH, prim_cap, [])
        # 用 Traverse 计数到 cap
        count = 0
        for _prim in stage.Traverse():
            count += 1
            if count >= prim_cap:
                break
        prim_count = count
        out["prim_count_capped"] = count >= prim_cap
    except Exception:
        out["prim_count_capped"] = None
    out["prim_count"] = prim_count
    return out


def lop_prim_get(hou, node_path, prim_path,
                 max_attributes=_DEFAULT_MAX_ATTRIBUTES):
    """单个 prim 的 type / active / loaded / kind + 有界属性列表。

    ``prim_path`` 必填（USD path，如 ``/Asset``）。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    if not isinstance(prim_path, str) or not prim_path.strip():
        return {"status": "error", "message": (
            "prim_path must be a non-empty string"), "field": "prim_path"}
    cap_check = _coerce_int("max_attributes", max_attributes)
    if cap_check.get("status") == "error":
        return cap_check
    attr_cap = cap_check["value"]
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    prim = stage.GetPrimAtPath(prim_path)
    if prim is None or not _prim_valid(prim):
        return cmn.apply_response_cap({"status": "error", "message": (
            "prim %r not found on composed stage") % prim_path,
            "field": "prim_path"})
    data = _gather_prim_info(prim, attr_cap)
    data["capability"] = caps
    return cmn.apply_response_cap(_success(data))


def _prim_valid(prim):
    """``prim.IsValid()`` 若可用，否则 best-effort。"""
    is_valid = getattr(prim, "IsValid", None)
    if callable(is_valid):
        try:
            return bool(is_valid())
        except Exception:
            return True
    return True


def _gather_prim_info(prim, attr_cap):
    out = {}
    for key, getter in (
            ("name", "GetName"),
            ("path", "GetPath"),
            ("type", "GetTypeName"),
            ("kind", "GetKind")):
        method = getattr(prim, getter, None)
        if callable(method):
            try:
                val = method()
                out[key] = _prim_path_str(val) if key in ("path",) else (
                    _jsonable(val))
            except Exception:
                out[key] = None
        else:
            out[key] = None
    for key, getter in (
            ("active", "IsActive"),
            ("loaded", "IsLoaded"),
            ("defined", "IsDefined"),
            ("abstract", "IsAbstract"),
            ("instance", "IsInstance")):
        method = getattr(prim, getter, None)
        if callable(method):
            try:
                out[key] = bool(method())
            except Exception:
                out[key] = None
        else:
            out[key] = None
    # 有界属性
    attrs = []
    try:
        attr_list = list(prim.GetAttributes())
    except Exception:
        attr_list = []
    for attr in attr_list[:attr_cap]:
        try:
            attrs.append({
                "name": attr.GetName(),
                "type": str(attr.GetTypeName()),
            })
        except Exception:
            continue
    out["attributes"] = attrs
    out["attributes_capped"] = (len(attr_list) > attr_cap)
    out["attributes_total"] = len(attr_list)
    return out


def lop_prim_search(hou, node_path, name=None, type_name=None,
                    max_prims=_DEFAULT_MAX_PRIMS,
                    max_depth=_DEFAULT_MAX_DEPTH):
    """按 ``name`` 子串 / ``type_name`` 精确匹配搜索 prim。

    两者都省略时等价于 ``list_usd_prims``（受 cap 限制）。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    if name is not None and not isinstance(name, str):
        return {"status": "error", "message": (
            "name must be a string or None"), "field": "name"}
    if type_name is not None and not isinstance(type_name, str):
        return {"status": "error", "message": (
            "type_name must be a string or None"), "field": "type_name"}
    prim_cap_check = _coerce_int("max_prims", max_prims)
    if prim_cap_check.get("status") == "error":
        return prim_cap_check
    depth_check = _coerce_int("max_depth", max_depth)
    if depth_check.get("status") == "error":
        return depth_check
    prim_cap = prim_cap_check["value"]
    depth_cap = depth_check["value"]
    name_l = name.lower() if isinstance(name, str) else None
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    matches = []
    try:
        for prim in stage.Traverse():
            if len(matches) >= prim_cap:
                break
            try:
                pname = prim.GetName()
            except Exception:
                pname = None
            try:
                ptype = str(prim.GetTypeName())
            except Exception:
                ptype = ""
            if name_l is not None:
                if pname is None or name_l not in pname.lower():
                    continue
            if type_name is not None:
                if ptype != type_name:
                    continue
            matches.append({
                "path": _prim_path_str(prim.GetPath()),
                "name": pname,
                "type": ptype,
            })
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "prim search failed: %s") % err,
            "exception": err.__class__.__name__})
    data = {
        "name_filter": name,
        "type_filter": type_name,
        "matches": matches,
        "match_count": len(matches),
        "capped": len(matches) >= prim_cap,
        "capability": caps,
    }
    return cmn.apply_response_cap(_success(data))


def lop_layer_info(hou, node_path, max_layers=_DEFAULT_MAX_LAYERS):
    """layer stack 摘要（root layer identifier / real path / sublayer 数）。"""
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    cap_check = _coerce_int("max_layers", max_layers)
    if cap_check.get("status") == "error":
        return cap_check
    layer_cap = cap_check["value"]
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    layers = _gather_layers(stage, layer_cap)
    data = {
        "layers": layers,
        "layer_count": len(layers),
        "capability": caps,
    }
    return cmn.apply_response_cap(_success(data))


def _gather_layers(stage, layer_cap):
    """读取 layer stack（不修改 layer）；root + session + sublayers 有界。"""
    layers = []
    # root layer
    try:
        root = stage.GetRootLayer()
        layers.append(_layer_summary(root, is_root=True))
    except Exception:
        pass
    # session layer（若公开）
    try:
        session = stage.GetSessionLayer()
        summary = _layer_summary(session, is_session=True)
        if summary and summary not in layers:
            layers.append(summary)
    except Exception:
        pass
    # layer stack
    try:
        stack = stage.GetLayerStack()
        for layer in stack:
            if len(layers) >= layer_cap:
                break
            summary = _layer_summary(layer)
            if summary and summary not in layers:
                layers.append(summary)
    except Exception:
        pass
    return layers[:layer_cap]


def _layer_summary(layer, is_root=False, is_session=False):
    if layer is None:
        return None
    out = {}
    for key, attr in (
            ("identifier", "identifier"),
            ("real_path", "realPath"),
            ("anonymous", "anonymous"),
            ("default_prim", "defaultPrim")):
        try:
            out[key] = getattr(layer, attr, None)
            if isinstance(out[key], (bytes,)):
                out[key] = out[key].decode("utf-8", "replace")
        except Exception:
            out[key] = None
    out["is_root"] = is_root
    out["is_session"] = is_session
    # sublayer path 数（不展开内容）
    try:
        sub = layer.subLayerPaths
        out["sublayer_count"] = len(sub) if sub else 0
    except Exception:
        out["sublayer_count"] = None
    return out


def list_usd_prims(hou, node_path, max_depth=_DEFAULT_MAX_DEPTH,
                   max_prims=_DEFAULT_MAX_PRIMS):
    """受 ``max_depth`` / ``max_prims`` 限制的 prim 遍历。"""
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    depth_check = _coerce_int("max_depth", max_depth)
    if depth_check.get("status") == "error":
        return depth_check
    prim_cap_check = _coerce_int("max_prims", max_prims)
    if prim_cap_check.get("status") == "error":
        return prim_cap_check
    depth_cap = depth_check["value"]
    prim_cap = prim_cap_check["value"]
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    prims = []
    try:
        pseudo = stage.GetPseudoRoot()
        _walk_prims(stage, pseudo, 0, depth_cap, prim_cap, prims)
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "prim traverse failed: %s") % err,
            "exception": err.__class__.__name__})
    data = {
        "prims": prims,
        "prim_count": len(prims),
        "capped": len(prims) >= prim_cap,
        "capability": caps,
    }
    return cmn.apply_response_cap(_success(data))


def get_usd_attribute(hou, node_path, prim_path, attribute, time=0):
    """单个属性值 + 类型名（composed 读）。"""
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    if not isinstance(prim_path, str) or not prim_path.strip():
        return {"status": "error", "message": (
            "prim_path must be a non-empty string"), "field": "prim_path"}
    if not isinstance(attribute, str) or not attribute.strip():
        return {"status": "error", "message": (
            "attribute must be a non-empty string"), "field": "attribute"}
    if isinstance(time, bool) or not isinstance(time, (int, float)):
        return {"status": "error", "message": (
            "time must be a JSON number"), "field": "time"}
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    prim = stage.GetPrimAtPath(prim_path)
    if prim is None or not _prim_valid(prim):
        return cmn.apply_response_cap({"status": "error", "message": (
            "prim %r not found") % prim_path, "field": "prim_path"})
    attr = _get_prim_attribute(prim, attribute)
    if attr is None:
        return cmn.apply_response_cap({"status": "error", "message": (
            "attribute %r not found on prim %r") % (attribute, prim_path),
            "field": "attribute"})
    try:
        type_name = str(attr.GetTypeName())
    except Exception:
        type_name = None
    try:
        value = attr.Get(time)
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "attribute read failed: %s") % err,
            "exception": err.__class__.__name__})
    data = {
        "prim_path": prim_path,
        "attribute": attribute,
        "type": type_name,
        "value": _jsonable(value),
        "time": time,
        "capability": caps,
    }
    return cmn.apply_response_cap(_success(data))


def _get_prim_attribute(prim, name):
    """``prim.GetAttribute(name)`` 若可用且 valid。"""
    get_attr = getattr(prim, "GetAttribute", None)
    if not callable(get_attr):
        return None
    try:
        attr = get_attr(name)
    except Exception:
        return None
    is_valid = getattr(attr, "IsValid", None)
    if callable(is_valid):
        try:
            if not is_valid():
                return None
        except Exception:
            pass
    return attr


def get_usd_prim_stats(hou, node_path, prim_path):
    """prim active / loaded / defined / abstract / instance + 属性计数。"""
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    if not isinstance(prim_path, str) or not prim_path.strip():
        return {"status": "error", "message": (
            "prim_path must be a non-empty string"), "field": "prim_path"}
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    prim = stage.GetPrimAtPath(prim_path)
    if prim is None or not _prim_valid(prim):
        return cmn.apply_response_cap({"status": "error", "message": (
            "prim %r not found") % prim_path, "field": "prim_path"})
    data = _gather_prim_info(prim, _DEFAULT_MAX_ATTRIBUTES)
    data["capability"] = caps
    return cmn.apply_response_cap(_success(data))


def get_last_modified_prims(hou, node_path):
    """最近修改信息不可证明时返回 ``unsupported``。

    HOM/USD 无法提供可证明的 layer contribution / change 时间戳；MUST NOT
    用 prim 遍历顺序、当前时间或进程内猜测伪造「最近修改」。本实现一律
    返回 ``unsupported``，并在 details 说明原因。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    caps = _probe_capabilities(hou)
    return cmn.apply_response_cap(_unsupported(
        "last_modified_unprovable",
        "USD composed stage does not expose a provable last-modified "
        "ordering; refusing to fabricate from traversal order or wall clock",
        details={"capability": caps,
                 "reason": ("HOM/USD provides no provable layer contribution "
                            "or change timestamp for a composed stage")}))


def get_usd_composition(hou, node_path, prim_path,
                        max_arcs=_DEFAULT_MAX_ARCS):
    """composition arc 摘要（受 ``max_arcs`` 限制）。

    使用 ``Usd.PrimCompositionQuery`` 若可用；否则返回 ``unsupported``。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    if not isinstance(prim_path, str) or not prim_path.strip():
        return {"status": "error", "message": (
            "prim_path must be a non-empty string"), "field": "prim_path"}
    cap_check = _coerce_int("max_arcs", max_arcs)
    if cap_check.get("status") == "error":
        return cap_check
    arc_cap = cap_check["value"]
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    prim = stage.GetPrimAtPath(prim_path)
    if prim is None or not _prim_valid(prim):
        return cmn.apply_response_cap({"status": "error", "message": (
            "prim %r not found") % prim_path, "field": "prim_path"})
    arcs = _gather_composition_arcs(pxr, prim, arc_cap)
    if arcs is None:
        return cmn.apply_response_cap(_unsupported(
            "composition_query_unavailable",
            "Usd.PrimCompositionQuery not available in this USD build",
            details={"capability": caps}))
    data = {
        "prim_path": prim_path,
        "arcs": arcs,
        "arc_count": len(arcs),
        "capped": len(arcs) >= arc_cap,
        "capability": caps,
    }
    return cmn.apply_response_cap(_success(data))


def _gather_composition_arcs(pxr, prim, arc_cap):
    """``Usd.PrimCompositionQuery`` 若可用；返回 list 或 None。"""
    Usd = pxr["Usd"]
    make_query = getattr(Usd, "PrimCompositionQuery", None)
    if make_query is None:
        return None
    try:
        query = make_query.GetForPrim(prim)
        arcs = []
        for arc in query.GetCompositionArcs():
            if len(arcs) >= arc_cap:
                break
            entry = {}
            for key, getter in (
                    ("arc_type", "GetArcType"),
                    ("node_layer_stack_identifier", "GetNodeIdentifier"),
                    ("node_path", "GetRootPath")):
                method = getattr(arc, getter, None)
                if callable(method):
                    try:
                        val = method()
                        entry[key] = _prim_path_str(val) if key in (
                            "node_path",) else _jsonable(val)
                    except Exception:
                        entry[key] = None
            arcs.append(entry)
        return arcs
    except Exception:
        return None


def get_usd_variants(hou, node_path, prim_path):
    """variant set 名称与当前选择。"""
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    if not isinstance(prim_path, str) or not prim_path.strip():
        return {"status": "error", "message": (
            "prim_path must be a non-empty string"), "field": "prim_path"}
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    prim = stage.GetPrimAtPath(prim_path)
    if prim is None or not _prim_valid(prim):
        return cmn.apply_response_cap({"status": "error", "message": (
            "prim %r not found") % prim_path, "field": "prim_path"})
    variant_sets = []
    try:
        sets = prim.GetVariantSets()
        names_method = getattr(sets, "GetNames", None)
        names = list(names_method()) if callable(names_method) else []
        for name in names:
            entry = {"name": name, "choices": [], "selected": None}
            vset = sets.GetVariantSet(name)
            choices_method = getattr(vset, "GetVariantNames", None)
            if callable(choices_method):
                try:
                    entry["choices"] = list(choices_method())
                except Exception:
                    entry["choices"] = []
            selected_method = getattr(vset, "GetVariantSelection", None)
            if callable(selected_method):
                try:
                    entry["selected"] = selected_method()
                except Exception:
                    entry["selected"] = None
            variant_sets.append(entry)
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "variant read failed: %s") % err,
            "exception": err.__class__.__name__})
    data = {
        "prim_path": prim_path,
        "variant_sets": variant_sets,
        "variant_set_count": len(variant_sets),
        "capability": caps,
    }
    return cmn.apply_response_cap(_success(data))


def inspect_usd_layer(hou, node_path, max_layers=_DEFAULT_MAX_LAYERS):
    """layer 自定义元数据 / sublayer 路径（只读检视）。"""
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    cap_check = _coerce_int("max_layers", max_layers)
    if cap_check.get("status") == "error":
        return cap_check
    layer_cap = cap_check["value"]
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    layers = _gather_layers(stage, layer_cap)
    # 追加每个 layer 的 sublayer 路径（不展开内容）
    for layer_summary in layers:
        try:
            root = stage.GetRootLayer()
            if layer_summary.get("is_root"):
                try:
                    sub = root.subLayerPaths
                    layer_summary["sublayer_paths"] = list(sub) if sub else []
                except Exception:
                    layer_summary["sublayer_paths"] = []
        except Exception:
            pass
    data = {
        "layers": layers,
        "layer_count": len(layers),
        "capability": caps,
    }
    return cmn.apply_response_cap(_success(data))


def list_lights(hou, node_path, max_lights=_DEFAULT_MAX_LIGHTS):
    """灯光识别：优先 ``UsdLux.LightAPI``，再以具体 schema ``IsA`` 补充。

    不依赖 ``UsdLux.Light`` 基类；缺少 API 时返回 capability warning。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    cap_check = _coerce_int("max_lights", max_lights)
    if cap_check.get("status") == "error":
        return cap_check
    light_cap = cap_check["value"]
    caps = _probe_capabilities(hou)
    if not caps["has_stage"]:
        return cmn.apply_response_cap(_unsupported(
            "lop_stage_unavailable",
            "hou.LopNode.stage not available in this build",
            details={"capability": caps}))
    pxr = _get_pxr()
    if pxr is None:
        return cmn.apply_response_cap(_unsupported(
            "pxr_unavailable", "pxr unavailable",
            details={"capability": caps}))
    if not caps["has_light_api"] and not caps["light_schemas"]:
        return cmn.apply_response_cap(_unsupported(
            "no_light_detection_api",
            "neither UsdLux.LightAPI nor concrete light schemas available",
            details={"capability": caps}))
    UsdLux = pxr["UsdLux"]
    light_api = getattr(UsdLux, "LightAPI", None)
    schemas = {}
    for name in caps["light_schemas"]:
        schemas[name] = getattr(UsdLux, name)
    try:
        node = _resolve_lop_node(hou, node_path)
        stage = _get_stage(node)
    except ValueError as err:
        return cmn.apply_response_cap({"status": "error",
                                       "message": str(err),
                                       "field": "node_path"})
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "stage read failed: %s") % err,
            "exception": err.__class__.__name__})
    if stage is None:
        return cmn.apply_response_cap(_unsupported(
            "stage_none", "LopNode.stage() returned None",
            details={"capability": caps}))
    lights = []
    try:
        for prim in stage.Traverse():
            if len(lights) >= light_cap:
                break
            detected = _detect_light(prim, light_api, schemas)
            if detected is not None:
                try:
                    ppath = _prim_path_str(prim.GetPath())
                except Exception:
                    ppath = None
                try:
                    ptype = str(prim.GetTypeName())
                except Exception:
                    ptype = None
                lights.append({
                    "path": ppath,
                    "type": ptype,
                    "detected_by": detected,
                })
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "light traverse failed: %s") % err,
            "exception": err.__class__.__name__})
    data = {
        "lights": lights,
        "light_count": len(lights),
        "capped": len(lights) >= light_cap,
        "capability": caps,
    }
    return cmn.apply_response_cap(_success(data))


def _detect_light(prim, light_api, schemas):
    """先 ``HasAPI(LightAPI)``，再具体 schema ``IsA``；返回检测来源或 None。"""
    # 1. LightAPI（若存在）
    if light_api is not None:
        has_api = getattr(prim, "HasAPI", None)
        if callable(has_api):
            try:
                if has_api(light_api):
                    return "LightAPI"
            except Exception:
                pass
    # 2. 具体 schema IsA
    for name, schema_cls in schemas.items():
        is_a = getattr(prim, "IsA", None)
        if callable(is_a):
            try:
                if is_a(schema_cls):
                    return name
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Section 8: authoring adapter probe（R9：node type + 参数 schema 固化）
# ---------------------------------------------------------------------------
def _lop_node_types(hou):
    """``hou.lopNodeTypeCategory().nodeTypes()`` 探针；返回 set。"""
    cat = None
    try:
        cat = hou.lopNodeTypeCategory()
    except Exception:
        return set()
    if cat is None:
        return set()
    try:
        types = cat.nodeTypes()
    except Exception:
        return set()
    try:
        return set(types.keys())
    except Exception:
        return set()


def _resolve_adapter(hou, candidates):
    """返回首个 ``node_type`` 在探针结果中的 adapter（dict）；否则 None。

    选定 adapter 的参数 schema 固定使用，不尝试多个 parm 名猜测。
    """
    types = _lop_node_types(hou)
    if not types:
        return None
    for adapter in candidates:
        if adapter["node_type"] in types:
            return adapter
    return None


# ---------------------------------------------------------------------------
# Section 9: 写工具（3 个，归 MUTATING_COMMANDS）
# ---------------------------------------------------------------------------
def _capture_undo_group(hou, label):
    undos = getattr(hou, "undos", None)
    if undos is None:
        return None
    return undos.group(label)


def _enter_undo_group(group):
    if group is None:
        return True
    try:
        group.__enter__()
    except Exception:
        return False
    return True


def _exit_undo_group(group):
    if group is None:
        return
    try:
        group.__exit__(None, None, None)
    except Exception:
        return


def lop_import(hou, parent_path, file_path, import_type="reference",
               prim_path="/", node_name=None):
    """创建 Reference 或 Sublayer LOP，按探针固化的参数 schema 设置 file。

    创建 / 连接 / 配置是单 undo group 的连续步骤；失败 destroy 半成品。
    **不**直接修改 stage layer stack（R10：经 authoring adapter）。
    adapter 不可用时返回 ``unsupported``。
    """
    if not isinstance(parent_path, str) or not parent_path.strip():
        return {"status": "error", "message": (
            "parent_path must be a non-empty string"), "field": "parent_path"}
    if not isinstance(file_path, str) or not file_path.strip():
        return {"status": "error", "message": (
            "file_path must be a non-empty string"), "field": "file_path"}
    if not isinstance(import_type, str) or import_type not in _VALID_IMPORT_TYPES:
        return {"status": "error", "message": (
            "import_type must be one of %s") % sorted(_VALID_IMPORT_TYPES),
            "field": "import_type"}
    if not isinstance(prim_path, str):
        return {"status": "error", "message": (
            "prim_path must be a string"), "field": "prim_path"}
    if node_name is not None and not isinstance(node_name, str):
        return {"status": "error", "message": (
            "node_name must be a string or None"), "field": "node_name"}

    candidates = (_REFERENCE_ADAPTERS if import_type == "reference"
                  else _SUBLAYER_ADAPTERS)
    adapter = _resolve_adapter(hou, candidates)
    if adapter is None:
        return cmn.apply_response_cap(_unsupported(
            "no_import_adapter",
            "no whitelisted %s LOP authoring adapter available on this build"
            % import_type,
            details={"import_type": import_type,
                      "available_types_sample": sorted(
                          _lop_node_types(hou))[:40]}))

    try:
        parent = hou.node(parent_path)
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "parent resolve failed: %s") % err,
            "exception": err.__class__.__name__})
    if parent is None:
        return cmn.apply_response_cap({"status": "error", "message": (
            "parent not found: %s") % parent_path, "field": "parent_path"})
    create_node = getattr(parent, "createNode", None)
    if not callable(create_node):
        return cmn.apply_response_cap({"status": "error", "message": (
            "parent is not an editable LOP network container"),
            "field": "parent_path"})

    group = _capture_undo_group(hou, "MCP: lop_import")
    if not _enter_undo_group(group):
        return cmn.apply_response_cap({"status": "error", "message": (
            "failed to open undo group")})
    new_node = None
    try:
        new_node = create_node(adapter["node_type"], node_name=node_name)
        # 配置 file 参数（探针固化的 schema）
        _set_parm(new_node, adapter["file_count_parm"], 1)
        _set_parm(new_node, adapter["file_enable_parm"], 1)
        _set_parm(new_node, adapter["filepath_parm"], file_path)
        if import_type == "reference":
            _set_parm(new_node, adapter["primpath_parm"], prim_path)
            _set_parm(new_node, adapter["createprims_parm"], 1)
            # 源 prim path（默认 /）
            ref_parm = adapter.get("refprimpath_parm")
            if ref_parm is not None:
                _set_parm(new_node, ref_parm, prim_path)
    except Exception as err:
        if new_node is not None:
            try:
                new_node.destroy()
            except Exception:
                pass
        _exit_undo_group(group)
        return cmn.apply_response_cap({"status": "error", "message": (
            "lop_import configure failed: %s") % err,
            "exception": err.__class__.__name__})
    _exit_undo_group(group)
    try:
        new_path = new_node.path()
    except Exception:
        new_path = ""
    return cmn.apply_response_cap(_success({
        "node_path": new_path,
        "parent_path": parent_path,
        "import_type": import_type,
        "file_path": file_path,
        "prim_path": prim_path,
        "adapter": adapter["node_type"],
    }))


def set_usd_attribute(hou, parent_path, prim_path, attribute, value,
                      attribute_type="float", node_name=None):
    """创建白名单属性 authoring LOP（Edit Properties），按其真实参数 schema
    author prim path / attribute / type / value。

    若 adapter 不可用或 value 无法无损映射到 adapter 的 value 参数，
    返回 ``unsupported``，**禁止** fallback 到 composed stage mutation（R10）。
    """
    if not isinstance(parent_path, str) or not parent_path.strip():
        return {"status": "error", "message": (
            "parent_path must be a non-empty string"), "field": "parent_path"}
    if not isinstance(prim_path, str) or not prim_path.strip():
        return {"status": "error", "message": (
            "prim_path must be a non-empty string"), "field": "prim_path"}
    if not isinstance(attribute, str) or not attribute.strip():
        return {"status": "error", "message": (
            "attribute must be a non-empty string"), "field": "attribute"}
    if not isinstance(attribute_type, str) or attribute_type not in _VALID_ATTR_TYPES:
        return {"status": "error", "message": (
            "attribute_type must be one of %s") % sorted(_VALID_ATTR_TYPES),
            "field": "attribute_type"}
    if node_name is not None and not isinstance(node_name, str):
        return {"status": "error", "message": (
            "node_name must be a string or None"), "field": "node_name"}

    adapter = _resolve_adapter(hou, _ATTR_ADAPTERS)
    if adapter is None:
        return cmn.apply_response_cap(_unsupported(
            "no_attr_adapter",
            "no whitelisted attribute authoring LOP adapter available",
            details={"available_types_sample": sorted(
                _lop_node_types(hou))[:40]}))
    # R10：adapter 的 value_parm 为 None 时（H21 Edit Properties 无干净
    # 静态 value 参数），value authoring 不可无损映射 → 返回 unsupported，
    # **不** fallback 到 pxr mutation。
    if adapter.get("value_parm") is None:
        return cmn.apply_response_cap(_unsupported(
            "attr_value_mapping_unsupported",
            "the whitelisted authoring adapter does not expose a clean "
            "static value parameter for attribute value authoring; refusing "
            "to mutate the composed stage via pxr",
            details={"adapter": adapter["node_type"],
                      "attribute": attribute,
                      "attribute_type": attribute_type}))

    # value 校验（无损映射检查）
    value_check = _validate_attr_value(attribute_type, value)
    if value_check.get("status") == "error":
        return cmn.apply_response_cap(value_check)
    clean_value = value_check["value"]

    try:
        parent = hou.node(parent_path)
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "parent resolve failed: %s") % err,
            "exception": err.__class__.__name__})
    if parent is None:
        return cmn.apply_response_cap({"status": "error", "message": (
            "parent not found: %s") % parent_path, "field": "parent_path"})
    create_node = getattr(parent, "createNode", None)
    if not callable(create_node):
        return cmn.apply_response_cap({"status": "error", "message": (
            "parent is not an editable LOP network container"),
            "field": "parent_path"})

    group = _capture_undo_group(hou, "MCP: set_usd_attribute")
    if not _enter_undo_group(group):
        return cmn.apply_response_cap({"status": "error", "message": (
            "failed to open undo group")})
    new_node = None
    try:
        new_node = create_node(adapter["node_type"], node_name=node_name)
        _set_parm(new_node, adapter["primpattern_parm"], prim_path)
        _set_parm(new_node, adapter["primpath_parm"], prim_path)
        _set_parm(new_node, adapter["createprims_parm"], 1)
        # value 经探针固化的 value_parm 写入（此处仅当 adapter 暴露时）
        _set_parm(new_node, adapter["value_parm"], clean_value)
    except Exception as err:
        if new_node is not None:
            try:
                new_node.destroy()
            except Exception:
                pass
        _exit_undo_group(group)
        return cmn.apply_response_cap({"status": "error", "message": (
            "set_usd_attribute configure failed: %s") % err,
            "exception": err.__class__.__name__})
    _exit_undo_group(group)
    try:
        new_path = new_node.path()
    except Exception:
        new_path = ""
    return cmn.apply_response_cap(_success({
        "node_path": new_path,
        "parent_path": parent_path,
        "prim_path": prim_path,
        "attribute": attribute,
        "attribute_type": attribute_type,
        "value": clean_value,
        "adapter": adapter["node_type"],
    }))


def _validate_attr_value(attribute_type, value):
    """无损映射检查：返回 {"value": ...} 或 error dict。"""
    if attribute_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return {"status": "error", "message": (
                "float value must be a JSON number; bool not accepted"),
                "field": "value"}
        return {"value": float(value)}
    if attribute_type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return {"status": "error", "message": (
                "int value must be a JSON integer; bool not accepted"),
                "field": "value"}
        return {"value": int(value)}
    if attribute_type == "string":
        if not isinstance(value, str):
            return {"status": "error", "message": (
                "string value must be a JSON string"),
                "field": "value"}
        return {"value": value}
    if attribute_type == "vector":
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return {"status": "error", "message": (
                "vector value must be a 3-element list/tuple"),
                "field": "value"}
        out = []
        for i, coord in enumerate(value):
            if isinstance(coord, bool) or not isinstance(coord, (int, float)):
                return {"status": "error", "message": (
                    "vector[%d] must be a JSON number") % i,
                    "field": "value"}
            cf = float(coord)
            if not math.isfinite(cf):
                return {"status": "error", "message": (
                    "vector[%d] must be finite") % i, "field": "value"}
            out.append(cf)
        return {"value": out}
    return {"status": "error", "message": (
        "unsupported attribute_type"), "field": "attribute_type"}


def create_lop_node(hou, parent_path, node_type, node_name=None):
    """在可编辑 LOP parent 下创建指定 node type 节点。

    ``node_type`` 必须在 ``hou.lopNodeTypeCategory().nodeTypes()`` 探针中
    存在；单 undo group；失败 destroy 半成品。不创建 stage mutation。
    """
    if not isinstance(parent_path, str) or not parent_path.strip():
        return {"status": "error", "message": (
            "parent_path must be a non-empty string"), "field": "parent_path"}
    if not isinstance(node_type, str) or not node_type.strip():
        return {"status": "error", "message": (
            "node_type must be a non-empty string"), "field": "node_type"}
    if node_name is not None and not isinstance(node_name, str):
        return {"status": "error", "message": (
            "node_name must be a string or None"), "field": "node_name"}

    types = _lop_node_types(hou)
    if node_type not in types:
        return cmn.apply_response_cap(_unsupported(
            "unknown_lop_node_type",
            "node_type %r not in LOP node type registry" % node_type,
            details={"node_type": node_type,
                      "available_types_sample": sorted(types)[:40]}))

    try:
        parent = hou.node(parent_path)
    except Exception as err:
        return cmn.apply_response_cap({"status": "error", "message": (
            "parent resolve failed: %s") % err,
            "exception": err.__class__.__name__})
    if parent is None:
        return cmn.apply_response_cap({"status": "error", "message": (
            "parent not found: %s") % parent_path, "field": "parent_path"})
    create_node = getattr(parent, "createNode", None)
    if not callable(create_node):
        return cmn.apply_response_cap({"status": "error", "message": (
            "parent is not an editable LOP network container"),
            "field": "parent_path"})

    group = _capture_undo_group(hou, "MCP: create_lop_node")
    if not _enter_undo_group(group):
        return cmn.apply_response_cap({"status": "error", "message": (
            "failed to open undo group")})
    new_node = None
    try:
        new_node = create_node(node_type, node_name=node_name)
    except Exception as err:
        if new_node is not None:
            try:
                new_node.destroy()
            except Exception:
                pass
        _exit_undo_group(group)
        return cmn.apply_response_cap({"status": "error", "message": (
            "create_lop_node failed: %s") % err,
            "exception": err.__class__.__name__})
    _exit_undo_group(group)
    try:
        new_path = new_node.path()
    except Exception:
        new_path = ""
    try:
        resolved_type = new_node.type().name()
    except Exception:
        resolved_type = node_type
    return cmn.apply_response_cap(_success({
        "node_path": new_path,
        "parent_path": parent_path,
        "node_type": node_type,
        "resolved_type": resolved_type,
    }))


# ---------------------------------------------------------------------------
# Section 10: 内部 coerce helper（与 geo_measure 风格一致）
# ---------------------------------------------------------------------------
def _set_parm(node, name, value):
    """设置参数；parm 不存在抛 RuntimeError（让调用方决定 destroy）。"""
    parm = getattr(node, "parm", None)
    if not callable(parm):
        raise RuntimeError("node has no parm() method")
    p = parm(name)
    if p is None:
        raise RuntimeError("parm %r not found" % name)
    p.set(value)


def _coerce_int(name, value):
    """接受 int、拒 bool / 负数；返回 {"value": int} 或 error dict。"""
    if isinstance(value, bool):
        return {"status": "error", "message": (
            "%s must be a JSON number; bool is not accepted") % name,
                "field": name}
    if not isinstance(value, int):
        return {"status": "error", "message": (
            "%s must be an integer") % name, "field": name}
    if value < 0:
        return {"status": "error", "message": (
            "%s must be >= 0") % name, "field": name}
    return {"value": value}
