"""_render_settings.py — opera-houdini-mcp ROP 设置与受限创建（C9）。

集中承载 ROP 枚举 / 引擎推断 / 静态 parm 白名单 / 设置读写与受限创建，
把 hou.RopNode.render() 紧前的所有静态约束固化到模块层；运行态
sync render 与四层 policy gate 全部委托 ``_render_jobs.py``。

模块职责：
- hou 通过第一参数注入；顶层不 ``import hou``。
- ROP type 命名空间剥离：``Sop/karmarender::2.0`` 等版本/命名空间前缀
  在比较前剥离；**不**做字符串 ``in`` 包含判断。
- 引擎推断只接受 ``ifd / opengl / karmarender`` 三种 node type；
  ``karmarender`` 的 ``engine`` parm 只接受 ``cpu / xpu / gpu``（其中
  ``gpu`` 归一为 ``xpu``）。
- 静态 parm 白名单按 node type 分组；script / callback / command /
  executable 类型参数明确排除。tuple 长度按 ``parmTuple`` 实际长度
  校验。
- ``set_render_settings`` 必须分四阶段：
    1) 预校验所有请求 key/value/parm 可写性/prospective engine；
       任一失败 -> 零写入。
    2) 快照所有待写 parm 旧值；任一失败 -> 零写入。
    3) 应用所有 set；任一失败 / 验证失败 -> 显式恢复全部快照旧值
       （**不**依赖 ``hou.undos.group`` 自动 rollback）。
    4) 全部成功 -> 返 ``status=success``；恢复全部成功 ->
       ``status=error, error_code=render_settings_apply_failed,
       restored=true``；任一恢复失败 -> ``status=error,
       error_code=render_settings_restore_failed, restored=false``
       且附 ``restore_errors``。
- ``create_render_node`` 只允许 ``ifd / opengl / karmarender``，创建后
  立即通过同一白名单设置参数；未知 / 未列出 node type 整体 error。

设计依据：
- D1（命名空间剥离）：比较前必须 ``_normalize_node_type``；不接受
  ``node_type == "Sop/ifd"`` 这类 fallback。
- D2（白名单结构）：四类分组（公共 / ifd / opengl / karmarender），
  按 ``_PARM_WHITELIST`` 表覆盖；其余 key -> ``skipped``。
- D3（parm 类型守卫）：只接受 ``ParmTemplateType.Int / Float / String /
  Menu / Toggle / MultiParm`` 与 parm 实际存在的交集；button /
  callback / node reference list / ramp 显式拒绝。
- D4（preflight + snapshot + apply + restore）：与 design.md 完全一
  致；undo group 仅用于用户手动撤销，不承担错误恢复。
- D5（create allowlist）：与 ``_ENGINE_BY_TYPE`` 同根，保证创建后立
  即可分类与 policy。

约束：
- hou 通过参数注入；不新增 pip 依赖。
- 4 空格缩进 / snake_case / 中文 docstring / 无 f-string / 无类型注解。
- 错误含 ``status=error`` + ``message`` + ``field``，供 server 透传。
"""
from . import _common as cmn


# ---------------------------------------------------------------------------
# Section 1: 节点类型规范化
# ---------------------------------------------------------------------------
def _normalize_node_type(raw):
    """剥离命名空间与版本后缀，返回 ``ifd / opengl / karmarender / 其他``。

    ``Sop/karmarender::2.0`` -> ``karmarender``；
    ``ifd::3.0`` -> ``ifd``；``opengl`` 透传。

    非字符串 / 空字符串返回 ``""``。
    """
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    if "/" in value:
        value = value.split("/", 1)[1]
    if "::" in value:
        value = value.split("::", 1)[0]
    return value.strip()


# 引擎映射表（design.md §"ROP 与 policy renderer 映射"）。
_ENGINE_BY_TYPE = {
    "ifd": "mantra",
    "opengl": "opengl",
}
# Karma 在不同 Houdini 版本下 node type 名不同：
# - H21.0.x：``karma``（设计文档误写为 karmarender）。
# - H22.0+ ：``karmarender``（命名空间剥离后）。
# 两版本归一为 ``karmarender`` 内部表示。
_KARMA_NODE_TYPES = frozenset({"karma", "karmarender"})
_KARMA_NORMALIZED = "karmarender"
_KARMA_ENGINES = ("cpu", "xpu", "gpu")
_KARMA_ENGINE_ALIAS = {"gpu": "xpu"}
_POLICY_BY_ENGINE = {
    "mantra": "mantra",
    "opengl": "opengl",
    "karma_cpu": "karma_cpu",
    "karma_xpu": "karma_xpu",
}

# 受限创建 / 安全分类的 ROP type 集合（design.md §"ROP 与 policy renderer
# 映射"）。``karma`` / ``karmarender`` 两个名字都接受，覆盖 H21 / H22。
_CREATE_ALLOWLIST = frozenset({"ifd", "opengl", "karma", "karmarender"})

# 静态 parm 白名单（design.md §"设置白名单"）。键 = node type，值为
# 该 type 允许读写的 parm name 集合；公共 parm 单独列出避免重复。
# karma / karmarender 共用同一 parm 表。
_COMMON_PARMS = ("trange", "f1", "f2", "f3", "camera", "picture")
_TYPE_PARMS = {
    "ifd": ("vm_renderengine", "vm_samples",
             "override_camerares", "res1", "res2"),
    "opengl": ("scenepath",
                "override_camerares", "res1", "res2"),
    "karmarender": ("engine", "samples", "variance", "denoise",
                     "override_camerares", "res1", "res2"),
}


def _whitelist_for(node_type):
    """返回 ``node_type`` 的白名单 parm name 集合；未知 ROP 返空 frozenset。"""
    if node_type in _KARMA_NODE_TYPES:
        node_type = _KARMA_NORMALIZED
    if node_type not in _TYPE_PARMS:
        return frozenset()
    return frozenset(_COMMON_PARMS) | frozenset(_TYPE_PARMS[node_type])


def _policy_renderer_for(node_type, engine=None):
    """从 node type + karma engine 归一 policy renderer 字符串。

    Returns:
        str: ``mantra`` / ``opengl`` / ``karma_cpu`` / ``karma_xpu``。
        未知 type 或 karmarender 缺 / 非法 engine -> ``""``（fail-closed）。
    """
    if node_type in _ENGINE_BY_TYPE:
        return _ENGINE_BY_TYPE[node_type]
    if node_type in _KARMA_NODE_TYPES:
        if engine not in _KARMA_ENGINES:
            return ""
        normalized = _KARMA_ENGINE_ALIAS.get(engine, engine)
        return "karma_" + normalized
    return ""


# ---------------------------------------------------------------------------
# Section 2: hou parm 类型守卫
# ---------------------------------------------------------------------------
# 拒绝执行的 parmTemplate.type（按钮 / 回调 / 节点引用列表 /
# ramp 等）：HOM enum 在 H21 / H22 名称稳定但跨版本 ``hou.parmTemplateType``
# 的具体子集可能变化；按字符串 name 比较而非直接 enum。
_EXECUTABLE_PARM_TYPES = frozenset({
    "Button", "Callback", "Command", "Ramp", "MultiLineString",
    "NodeReferenceList", "ObjectList", "SpareParms",
})
# 允许的 parmTemplate.type：其余类型（None / unknown）一律拒绝读写。
_ACCEPTABLE_PARM_TYPES = frozenset({
    "Int", "Float", "String", "Menu", "Toggle", "MultiParm",
    "OrderedMenu", "Float2", "Float3", "Float4",
    "Int2", "Int3", "Int4",
    "Color", "Dir", "Vec", "Vec2", "Vec3", "Vec4",
    "UV", "UVW", "Matrix3", "Matrix4",
})


def _is_executable_parm(parm_tuple):
    """parm tuple 是否属于执行型 / 不可安全读写类型。"""
    template = parm_tuple.parmTemplate()
    type_name = template.type().name()
    return type_name in _EXECUTABLE_PARM_TYPES


def _is_acceptable_parm(parm_tuple):
    """parm tuple 是否属于允许读写的可安全类型。"""
    template = parm_tuple.parmTemplate()
    type_name = template.type().name()
    if type_name in _EXECUTABLE_PARM_TYPES:
        return False
    return type_name in _ACCEPTABLE_PARM_TYPES


def _coerce_value_for_parm(parm_tuple, value):
    """校验 ``value`` 是否匹配 parm tuple 的类型 / 长度约束；返 dict。

    返回 ``{"value": ...}`` 表示可用值（已转 tuple），或
    ``{"status": "error", ...}`` 表示拒绝（类型 / 长度不匹配）。
    """
    template = parm_tuple.parmTemplate()
    type_name = template.type().name()
    expected_len = len(parm_tuple)

    if expected_len == 1:
        # 单值 parm：兼容直接传标量或 list-of-1
        scalar_input = value
        if isinstance(value, (list, tuple)):
            if len(value) == 1:
                scalar_input = value[0]
            else:
                return {"status": "error", "message": (
                    "%r expects a single value; got a list of %d")
                    % (parm_tuple.name(), len(value)),
                    "field": parm_tuple.name()}
        scalar = _coerce_scalar(type_name, scalar_input, parm_tuple.name())
        if scalar.get("status") == "error":
            return scalar
        return {"value": scalar["value"]}
    # multi-component：要求 list / tuple 且长度匹配
    if not isinstance(value, (list, tuple)):
        return {"status": "error", "message": (
            "%r expects a list of %d values; got %r")
            % (parm_tuple.name(), expected_len, value),
            "field": parm_tuple.name()}
    if len(value) != expected_len:
        return {"status": "error", "message": (
            "%r expects %d values; got %d")
            % (parm_tuple.name(), expected_len, len(value)),
            "field": parm_tuple.name()}
    coerced = []
    for index, item in enumerate(value):
        scalar = _coerce_scalar(type_name, item, parm_tuple.name())
        if scalar.get("status") == "error":
            scalar["field"] = "{0}[{1}]".format(parm_tuple.name(), index)
            return scalar
        coerced.append(scalar["value"])
    return {"value": tuple(coerced)}


def _coerce_scalar(type_name, value, parm_name):
    """单值校验；返回 dict ``{"value": ...}`` 或 error。"""
    if isinstance(value, bool):
        # 显式拒绝 bool，避免 ``isinstance(True, int)`` 陷阱
        return {"status": "error", "message": (
            "%r must not be a bool; got %r") % (parm_name, value),
            "field": parm_name}
    if type_name in ("Int", "Toggle"):
        if isinstance(value, int):
            return {"value": int(value)}
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return {"status": "error", "message": (
                    "%r must be a finite integer; got %r")
                    % (parm_name, value), "field": parm_name}
            return {"value": int(value)}
        return {"status": "error", "message": (
            "%r must be an integer; got %r")
            % (parm_name, value), "field": parm_name}
    if type_name in ("Float", "Color", "Dir", "Vec", "Vec2", "Vec3", "Vec4",
                      "UV", "UVW", "Float2", "Float3", "Float4",
                      "Matrix3", "Matrix4"):
        if isinstance(value, (int, float)):
            as_float = float(value)
            if as_float != as_float or as_float in (float("inf"),
                                                     float("-inf")):
                return {"status": "error", "message": (
                    "%r must be a finite number; got %r")
                    % (parm_name, value), "field": parm_name}
            return {"value": as_float}
        return {"status": "error", "message": (
            "%r must be a number; got %r")
            % (parm_name, value), "field": parm_name}
    if type_name in ("String", "Menu", "OrderedMenu"):
        if isinstance(value, str):
            return {"value": value}
        return {"status": "error", "message": (
            "%r must be a string; got %r")
            % (parm_name, value), "field": parm_name}
    if type_name == "MultiParm":
        if isinstance(value, int):
            return {"value": int(value)}
        return {"status": "error", "message": (
            "%r must be an integer; got %r")
            % (parm_name, value), "field": parm_name}
    return {"status": "error", "message": (
        "%r has unsupported parm type %r") % (parm_name, type_name),
        "field": parm_name}


# ---------------------------------------------------------------------------
# Section 3: 解析 hou node / engine
# ---------------------------------------------------------------------------
def _resolve_rop_node(hou, node_path):
    """把 ``node_path`` 解析为 hou.RopNode；失败返 error dict。"""
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"),
            "field": "node_path"}
    node = hou.node(node_path)
    if node is None:
        return {"status": "error", "message": (
            "node not found at path %r") % node_path,
            "field": "node_path"}
    type_name = _normalize_node_type(node.type().name())
    if type_name in _KARMA_NODE_TYPES:
        type_name = _KARMA_NORMALIZED
    if type_name not in _TYPE_PARMS:
        return {"status": "error", "message": (
            "node %r has unsupported ROP type %r") % (
                node_path, node.type().name()),
            "field": "node_path"}
    return {"node": node, "type": type_name}


def _read_karma_engine(node):
    """读取 karmarender 的 engine parm；缺 / eval 失败返 ``""``。"""
    parm = node.parm("engine")
    if parm is None:
        return ""
    try:
        value = parm.eval()
    except Exception:
        return ""
    # hou.RopNode.parm("engine").eval() 在单值 parm 下返回标量字符串；
    # parmTuple.eval() 在某些场景返 list，统一规范化。
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        value = value[0]
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def _resolve_policy_renderer(node, type_name):
    """从真实 node + type 解析 policy renderer；未识别返 ``""``。"""
    if type_name == _KARMA_NORMALIZED:
        engine = _read_karma_engine(node)
        return _policy_renderer_for(type_name, engine)
    return _policy_renderer_for(type_name)


# ---------------------------------------------------------------------------
# Section 4: 公共 API — list_render_nodes
# ---------------------------------------------------------------------------
def list_render_nodes(hou, parent_path="/out"):
    """枚举 ``parent_path`` 下所有可分类 ROP 节点（含子节点）。

    Returns:
        dict: ``{"status": "success", "parent_path", "nodes": [...]}``，
        ``nodes`` 每项含 ``name / path / type / renderer``；未知 ROP
        type 仍列出但 ``renderer=""``。响应过 ``apply_response_cap``。
    """
    if not isinstance(parent_path, str) or not parent_path.strip():
        return {"status": "error", "message": (
            "parent_path must be a non-empty string"),
            "field": "parent_path"}
    parent = hou.node(parent_path)
    if parent is None:
        return {"status": "error", "message": (
            "parent not found at path %r") % parent_path,
            "field": "parent_path"}

    entries = []

    def _walk(node):
        for child in node.children():
            type_name = _normalize_node_type(child.type().name())
            if type_name in _KARMA_NODE_TYPES:
                type_name = _KARMA_NORMALIZED
            if type_name in _TYPE_PARMS:
                renderer = _resolve_policy_renderer(child, type_name)
                entries.append({
                    "name": child.name(),
                    "path": child.path(),
                    "type": type_name,
                    "renderer": renderer,
                })
            _walk(child)

    _walk(parent)

    return cmn.apply_response_cap({
        "status": "success",
        "parent_path": parent_path,
        "count": len(entries),
        "nodes": entries,
    })


# ---------------------------------------------------------------------------
# Section 5: 公共 API — get_render_settings
# ---------------------------------------------------------------------------
def get_render_settings(hou, node_path):
    """读取 ``node_path`` 的白名单 parm 值。

    Returns:
        dict: ``{"status": "success", "node_path", "node_type",
        "renderer", "parameters": {name: value}}``。未知 ROP type 整体
        error。响应过 ``apply_response_cap``。
    """
    resolved = _resolve_rop_node(hou, node_path)
    if resolved.get("status") == "error":
        return resolved
    node = resolved["node"]
    type_name = resolved["type"]
    renderer = _resolve_policy_renderer(node, type_name)

    whitelist = _whitelist_for(type_name)
    parameters = {}
    for name in whitelist:
        parm_tuple = node.parmTuple(name)
        if parm_tuple is None:
            continue
        if not _is_acceptable_parm(parm_tuple):
            continue
        try:
            value = parm_tuple.eval()
        except Exception:
            continue
        if isinstance(value, (tuple, list)):
            value = [v for v in value]
        parameters[name] = value

    return cmn.apply_response_cap({
        "status": "success",
        "node_path": node.path(),
        "node_type": type_name,
        "renderer": renderer,
        "parameters": parameters,
    })


# ---------------------------------------------------------------------------
# Section 6: 公共 API — set_render_settings
# ---------------------------------------------------------------------------
def set_render_settings(hou, node_path, parameters):
    """受限可撤销写入（design.md §"设置白名单 + 阶段"）。

    完整流程：
    1) 预校验所有请求 key 在白名单内；parm 实际存在；类型 / 长度
       匹配；prospective engine 仍可读且映射到已知 policy renderer。
       任一失败 -> ``status=error``，**零写入**。
    2) 快照所有待写 parm 旧值；任一失败 -> ``status=error``，**零写入**。
    3) 应用全部待写值；随后重新读取 engine / renderer 校验；任何
       set / eval / 映射失败 -> 显式恢复全部快照旧值，**不**依赖
       ``hou.undos.group`` 自动 rollback。
    4) 恢复成功 -> ``status=error, error_code=render_settings_apply_failed,
       restored=true``；恢复失败 -> ``status=error,
       error_code=render_settings_restore_failed, restored=false`` 含
       原应用错误与逐 parm ``restore_errors``。

    Returns:
        dict: ``{"status": "success"|"error", ...}``；response 过
        ``apply_response_cap``。
    """
    if not isinstance(parameters, dict):
        return {"status": "error", "message": (
            "parameters must be a dict"), "field": "parameters"}
    if not parameters:
        return {"status": "error", "message": (
            "parameters must be a non-empty dict"), "field": "parameters"}

    resolved = _resolve_rop_node(hou, node_path)
    if resolved.get("status") == "error":
        return resolved
    node = resolved["node"]
    type_name = resolved["type"]
    whitelist = _whitelist_for(type_name)

    # Phase 1: 预校验所有请求 key + value + parm 可写性 + prospective engine
    skipped = []
    planned = []  # list of dicts: {name, parm_tuple, value, original_eval}
    for raw_name, raw_value in parameters.items():
        if not isinstance(raw_name, str):
            return {"status": "error", "message": (
                "parameter name must be a string; got %r")
                % (raw_name,), "field": "parameters"}
        if raw_name not in whitelist:
            skipped.append({"name": raw_name,
                             "reason": "not in whitelist"})
            continue
        parm_tuple = node.parmTuple(raw_name)
        if parm_tuple is None:
            return {"status": "error", "message": (
                "parameter %r does not exist on node %r")
                % (raw_name, node.path()),
                "field": raw_name}
        if _is_executable_parm(parm_tuple):
            return {"status": "error", "message": (
                "parameter %r is executable and cannot be set")
                % raw_name, "field": raw_name}
        if not _is_acceptable_parm(parm_tuple):
            return {"status": "error", "message": (
                "parameter %r has unsupported type and cannot be set")
                % raw_name, "field": raw_name}
        coerced = _coerce_value_for_parm(parm_tuple, raw_value)
        if coerced.get("status") == "error":
            return coerced
        # 记录原值；snapshot 阶段再正式读
        planned.append({"name": raw_name, "parm_tuple": parm_tuple,
                         "value": coerced["value"]})

    # 预校验 prospective engine：使用请求中 ``engine``（如有）或当前
    # 值；若 karmarender 缺 / 错 engine 立即 error 且零写入。
    prospective_engine = None
    if type_name == _KARMA_NORMALIZED:
        for entry in planned:
            if entry["name"] == "engine":
                prospective_engine = entry["value"]
                break
        if prospective_engine is None:
            prospective_engine = _read_karma_engine(node)
        if prospective_engine not in _KARMA_ENGINES:
            return {"status": "error", "message": (
                "karmarender engine must be one of %r; got %r")
                % (list(_KARMA_ENGINES), prospective_engine),
                "field": "engine"}
        renderer_preview = _policy_renderer_for(
            type_name, prospective_engine)
        if not renderer_preview:
            return {"status": "error", "message": (
                "unsupported karma engine %r") % prospective_engine,
                "field": "engine"}

    # Phase 2: 快照全部待写 parm 旧值
    snapshots = []
    for entry in planned:
        parm_tuple = entry["parm_tuple"]
        try:
            original = parm_tuple.eval()
        except Exception as error:
            return {"status": "error", "message": (
                "failed to snapshot parameter %r: %s")
                % (entry["name"], error),
                "field": entry["name"],
                "exception": error.__class__.__name__}
        snapshots.append({"name": entry["name"],
                          "parm_tuple": parm_tuple,
                          "original": original})

    # Phase 3: 应用全部 set；若任一失败 -> 进入恢复路径
    applied = []
    apply_errors = []
    for entry, snapshot in zip(planned, snapshots):
        parm_tuple = entry["parm_tuple"]
        try:
            if len(parm_tuple) == 1:
                parm_tuple[0].set(entry["value"])
            else:
                parm_tuple.set(entry["value"])
        except Exception as error:
            apply_errors.append({"name": entry["name"],
                                  "error": str(error),
                                  "exception": error.__class__.__name__})
            break
        applied.append(entry["name"])

    # 应用后重新读取 renderer / engine 校验
    post_type = _normalize_node_type(node.type().name())
    post_renderer = _resolve_policy_renderer(node, post_type)
    if not post_renderer:
        apply_errors.append({"name": "(post-apply)",
                              "error": (
                                  "engine/renderer became unrecognizable "
                                  "after apply")})

    if apply_errors:
        # 恢复路径：按 snapshot 顺序恢复全部 parm 旧值
        restore_errors = []
        restored = []
        for snapshot in snapshots:
            parm_tuple = snapshot["parm_tuple"]
            try:
                if len(parm_tuple) == 1:
                    parm_tuple[0].set(snapshot["original"])
                else:
                    parm_tuple.set(snapshot["original"])
                restored.append(snapshot["name"])
            except Exception as error:
                restore_errors.append({
                    "name": snapshot["name"],
                    "error": str(error),
                    "exception": error.__class__.__name__,
                })
        if restore_errors:
            return cmn.apply_response_cap({
                "status": "error",
                "error_code": "render_settings_restore_failed",
                "restored": False,
                "node_path": node.path(),
                "node_type": post_type,
                "renderer": post_renderer,
                "applied": applied,
                "skipped": skipped,
                "apply_errors": apply_errors,
                "restored_parameters": restored,
                "restore_errors": restore_errors,
                "message": (
                    "set_render_settings applied and partial "
                    "restore failed; node may be partially modified"),
            })
        return cmn.apply_response_cap({
            "status": "error",
            "error_code": "render_settings_apply_failed",
            "restored": True,
            "node_path": node.path(),
            "node_type": post_type,
            "renderer": post_renderer,
            "applied": applied,
            "skipped": skipped,
            "apply_errors": apply_errors,
            "message": (
                "set_render_settings apply failed but all "
                "parameters restored to original values"),
        })

    return cmn.apply_response_cap({
        "status": "success",
        "node_path": node.path(),
        "node_type": post_type,
        "renderer": post_renderer,
        "applied": applied,
        "skipped": skipped,
    })


# ---------------------------------------------------------------------------
# Section 7: 公共 API — create_render_node
# ---------------------------------------------------------------------------
def create_render_node(hou, node_type, parent_path="/out", name=None,
                        parameters=None):
    """受限创建 ROP 节点（design.md §"create_render_node"）。

    仅允许 ``ifd / opengl / karmarender``；创建后通过同一白名单
    设置参数并再次校验 renderer 可识别。未知 node type 整体 error。

    Returns:
        dict: ``{"status": "success"|"error", "path", "type",
        "renderer", "applied", "skipped"}``；response 过
        ``apply_response_cap``。
    """
    type_name = _normalize_node_type(node_type)
    if type_name not in _CREATE_ALLOWLIST:
        return {"status": "error", "message": (
            "create_render_node only allows %r; got %r")
            % (sorted(_CREATE_ALLOWLIST), node_type),
            "field": "node_type"}
    if not isinstance(parent_path, str) or not parent_path.strip():
        return {"status": "error", "message": (
            "parent_path must be a non-empty string"),
            "field": "parent_path"}
    parent = hou.node(parent_path)
    if parent is None:
        return {"status": "error", "message": (
            "parent not found at path %r") % parent_path,
            "field": "parent_path"}

    node = parent.createNode(type_name, node_name=name)
    try:
        if parameters:
            set_result = set_render_settings(
                hou, node.path(), parameters)
            if set_result.get("status") != "success":
                # set 失败（含 set 全量预校验 / 快照 / 恢复）必须清理
                # 残留节点；set 自身已经把已写 parm 恢复到旧值（默认
                # 值），此处只需销毁未保存的节点。
                node.destroy()
                return set_result
            applied = set_result.get("applied", [])
            skipped = set_result.get("skipped", [])
        else:
            applied = []
            skipped = []
    except Exception:
        try:
            node.destroy()
        except Exception:
            pass
        raise

    type_name_post = _normalize_node_type(node.type().name())
    renderer = _resolve_policy_renderer(node, type_name_post)
    if not renderer:
        try:
            node.destroy()
        except Exception:
            pass
        return {"status": "error", "message": (
            "created node has unrecognizable renderer"),
            "field": "node_type"}

    return cmn.apply_response_cap({
        "status": "success",
        "path": node.path(),
        "name": node.name(),
        "type": type_name_post,
        "renderer": renderer,
        "applied": applied,
        "skipped": skipped,
    })