"""_parameters.py — add-node-parameter-vex-tools 参数/spare/链接 helper。

提供 8 个独立、hou 注入的参数操作函数：
- get_parameter / set_parameter / get_expression / revert_parameter
- link_parameters / lock_parameter
- create_spare_parameter / create_spare_parameters

设计原则：
- hou 注入：第一参数为 hou，单测用 stub 替换。
- 错误处理：节点 / parm / 链接目标不存在抛 ValueError；spare 校验全部
  项后单次 setParmTemplateGroup 提交，失败零部分提交。
- 链接：必须使用 ``Parm.set(Parm)`` 或 ``Parm.setExpression()``，不用
  channel alias 冒充。
- 不使用不存在的 addSpareParm / spareParameters / hou.FolderSet。
- 不引入新 pip 依赖。

ParmTemplate 构造：根据 data_type 路由到具体子类
``FloatParmTemplate / IntParmTemplate / StringParmTemplate /
ToggleParmTemplate / MenuParmTemplate``。folder 接受目标 folder 名；
非空且不存在时由 helper 自己创建同名 folder（Houdini 默认接受
appendToFolder 不存在的 folder 名并自动创建）。
"""
import re


# 合法 flag 白名单（与 modify_node flags 扩展一致）：D1 复用既有 helper。
FLAG_ALLOWED = ("display", "render", "bypass", "selectable", "template")
FLAG_METHODS = {
    "display": "setDisplayFlag",
    "render": "setRenderFlag",
    "bypass": "bypass",
    "selectable": "setSelected",  # selectable 取反；setSelectableFlag 在 H21 并不稳定
    "template": "setTemplateFlag",
}


# data_type 白名单
_PARM_DATA_TYPES = ("float", "int", "string", "toggle", "menu")


def _validate_flag_keys(flags):
    """flags 若非 None，必须为 dict 且 key 属于白名单。"""
    if flags is None:
        return
    if not isinstance(flags, dict):
        raise ValueError(
            u"flags 必须为 dict 或 None, got {0}".format(type(flags).__name__)
        )
    for key in flags:
        if key not in FLAG_ALLOWED:
            raise ValueError(
                u"flag {0!r} 不在白名单 {1} 内".format(key, list(FLAG_ALLOWED))
            )


def _resolve_node(hou, path):
    node = hou.node(path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(path))
    return node


def _resolve_parm(hou, node, parm_name):
    parm = node.parm(parm_name)
    if parm is None:
        raise ValueError(
            u"参数 {0} 不在节点 {1} 上".format(parm_name, node.path())
        )
    return parm


def _resolve_parm_by_path(hou, path):
    """支持 ``node_path.parm_name`` 简写，返回 (node, parm_name)。"""
    if "." not in path:
        raise ValueError(
            u"必须显式指定 parm 名: {0}".format(path)
        )
    node_path, parm_name = path.rsplit(".", 1)
    node = _resolve_node(hou, node_path)
    return node, parm_name


def _resolve_target_parm(hou, target):
    """target 形式 ``node_path.parm_name``。"""
    node, parm_name = _resolve_parm_by_path(hou, target)
    return _resolve_parm(hou, node, parm_name)


def _flag_helper(hou, node, flags):
    """复用 set_node_flags 风格：仅修改非 None 字段。"""
    applied = {}
    unsupported = []
    for flag, value in (flags or {}).items():
        if value is None:
            continue
        method_name = FLAG_METHODS.get(flag)
        if method_name is None:
            unsupported.append(flag)
            continue
        method = getattr(node, method_name, None)
        if method is None:
            unsupported.append(flag)
            continue
        if flag == "selectable":
            method(bool(value))
        else:
            method(bool(value))
        applied[flag] = bool(value)
    return applied, unsupported


# ---------------------------------------------------------------------------
# 读 / 写 / 表达式 / 撤销 / 锁 / 链接
# ---------------------------------------------------------------------------
def get_parameter(hou, path, parameter):
    """读取 parm 当前值、类型、表达式与时间依赖标志。

    无 expression 时 expression 字段为 None。
    """
    node = _resolve_node(hou, path)
    parm = _resolve_parm(hou, node, parameter)
    try:
        raw_expr = parm.expression()
    except (hou.OperationFailed, RuntimeError, ValueError):
        # H21+ 会抛 "Parameter is not animated"；按无表达式处理
        raw_expr = ""
    return {
        "path": path,
        "parameter": parameter,
        "value": parm.eval(),
        "type": _parm_type_name(hou, parm),
        "expression": raw_expr if raw_expr else None,
        "is_time_dependent": bool(getattr(parm, "isTimeDependent", lambda: False)()),
    }


def set_parameter(hou, path, parameter, value):
    """写 parm 值；返回 {path, parameter, old, new}。"""
    node = _resolve_node(hou, path)
    parm = _resolve_parm(hou, node, parameter)
    old = parm.eval()
    parm.set(value)
    return {
        "path": path,
        "parameter": parameter,
        "old": old,
        "new": parm.eval(),
    }


def get_expression(hou, path, parameter):
    """读取 parm 表达式字符串。空表达式返回 None。"""
    node = _resolve_node(hou, path)
    parm = _resolve_parm(hou, node, parameter)
    try:
        expr = parm.expression()
    except (hou.OperationFailed, RuntimeError, ValueError):
        # H21+ 会抛 "Parameter is not animated"；按无表达式处理
        expr = ""
    return {
        "path": path,
        "parameter": parameter,
        "expression": expr if expr else None,
    }


def revert_parameter(hou, path, parameter):
    """恢复 parm 至默认值（revertToDefaults）。"""
    node = _resolve_node(hou, path)
    parm = _resolve_parm(hou, node, parameter)
    parm.revertToDefaults()
    return {
        "path": path,
        "parameter": parameter,
        "value": parm.eval(),
    }


def link_parameters(hou, source, target):
    """用 ``Parm.set(Parm)`` 链接 source parm 至 target parm。

    不用 channel alias；走真实跨 parm 引用。
    """
    src_node, src_name = _resolve_parm_by_path(hou, source)
    src_parm = _resolve_parm(hou, src_node, src_name)
    target_node, target_name = _resolve_parm_by_path(hou, target)
    target_parm = _resolve_parm(hou, target_node, target_name)
    src_parm.set(target_parm)
    return {
        "source": source,
        "target": target,
        "source_value": src_parm.eval(),
    }


def lock_parameter(hou, path, parameter, locked):
    """切换 parm 锁定状态（hou.Parm.lock(on)）。"""
    node = _resolve_node(hou, path)
    parm = _resolve_parm(hou, node, parameter)
    parm.lock(bool(locked))
    return {
        "path": path,
        "parameter": parameter,
        "locked": bool(locked),
    }


# ---------------------------------------------------------------------------
# spare 参数：单/批量，PTG 一次提交。
# ---------------------------------------------------------------------------
def _ensure_default_tuple(default):
    """把 default 统一为 tuple；list/单值都接受。"""
    if isinstance(default, (list, tuple)):
        return tuple(default)
    return (default,)


def _build_parm_template(hou, name, label, data_type, default=None,
                         min_value=None, max_value=None, menu_items=None,
                         menu_labels=None, num_components=1):
    """按 data_type 构造具体 ParmTemplate 子类。"""
    if not isinstance(name, str) or not name:
        raise ValueError(u"name 必须为非空字符串")
    if data_type not in _PARM_DATA_TYPES:
        raise ValueError(
            u"data_type 必须是 {0}, got {1!r}".format(list(_PARM_DATA_TYPES), data_type)
        )
    if num_components is not None and (
        not isinstance(num_components, int) or num_components < 1
    ):
        raise ValueError(u"num_components 必须为正整数")

    default_t = _ensure_default_tuple(default if default is not None else (0.0,))

    if data_type == "float":
        mn = min_value if min_value is not None else 0.0
        mx = max_value if max_value is not None else 1.0
        return hou.FloatParmTemplate(
            name, label or name, num_components or 1,
            default_value=default_t, min=mn, max=mx,
        )
    if data_type == "int":
        mn = int(min_value) if min_value is not None else 0
        mx = int(max_value) if max_value is not None else 10
        return hou.IntParmTemplate(
            name, label or name, num_components or 1,
            default_value=tuple(int(v) for v in default_t),
            min=mn, max=mx,
        )
    if data_type == "string":
        return hou.StringParmTemplate(
            name, label or name, num_components or 1,
            default_value=tuple(str(v) for v in default_t),
        )
    if data_type == "toggle":
        return hou.ToggleParmTemplate(
            name, label or name, default_value=bool(default_t[0]),
        )
    if data_type == "menu":
        items = list(menu_items) if menu_items else [str(v) for v in default_t]
        labels = list(menu_labels) if menu_labels else list(items)
        if len(items) != len(labels):
            raise ValueError(u"menu_items / menu_labels 长度不一致")
        return hou.MenuParmTemplate(
            name, label or name, items, labels,
            default_value=int(default_t[0]) if default_t else 0,
        )
    raise ValueError(u"无法构造 data_type={0!r} 的 ParmTemplate".format(data_type))


def _commit_spare_templates(hou, node, templates, folder_name):
    """把一组 template 追加到 PTG，然后单次 setParmTemplateGroup 提交。

    任何前置校验失败：抛 ValueError 且不修改 PTG（D2 / D4 约束）。
    """
    group = node.parmTemplateGroup()
    for tpl in templates:
        if folder_name:
            group.appendToFolder(folder_name, tpl)
        else:
            group.append(tpl)
    node.setParmTemplateGroup(group)


def create_spare_parameter(hou, path, name, data_type, label=None,
                           default=None, min_value=None, max_value=None,
                           menu_items=None, menu_labels=None,
                           folder=None, num_components=1):
    """单项 spare 参数追加。"""
    node = _resolve_node(hou, path)
    tpl = _build_parm_template(
        hou, name, label, data_type, default=default,
        min_value=min_value, max_value=max_value,
        menu_items=menu_items, menu_labels=menu_labels,
        num_components=num_components,
    )
    _commit_spare_templates(hou, node, [tpl], folder)
    return {
        "path": path,
        "name": name,
        "data_type": data_type,
        "folder": folder,
    }


def create_spare_parameters(hou, path, parameters, folder=None):
    """批量 spare 参数：先全量校验，再单次提交；失败零部分提交。"""
    node = _resolve_node(hou, path)
    if not isinstance(parameters, list) or not parameters:
        raise ValueError(u"parameters 必须为非空 list")
    # 1) 全量校验（先构造模板，构造失败立刻抛，不修改 PTG）
    templates = []
    seen = set()
    for spec in parameters:
        if not isinstance(spec, dict):
            raise ValueError(u"spec 必须为 dict")
        name = spec.get("name")
        data_type = spec.get("data_type")
        if name in seen:
            raise ValueError(u"duplicate parm name: {0}".format(name))
        seen.add(name)
        tpl = _build_parm_template(
            hou, name, spec.get("label"), data_type,
            default=spec.get("default"),
            min_value=spec.get("min_value"),
            max_value=spec.get("max_value"),
            menu_items=spec.get("menu_items"),
            menu_labels=spec.get("menu_labels"),
            num_components=spec.get("num_components", 1),
        )
        templates.append(tpl)
    # 2) 单次提交
    _commit_spare_templates(hou, node, templates, folder)
    return {
        "path": path,
        "count": len(templates),
        "folder": folder,
        "names": [t.name() for t in templates],
    }


def _parm_type_name(hou, parm):
    """提取 parm 实际类型名。"""
    try:
        tpl = parm.parmTemplate()
    except Exception:
        return "unknown"
    t = getattr(tpl, "type", None)
    if t is None:
        return "unknown"
    if hasattr(t, "name"):
        try:
            return t.name()
        except Exception:
            pass
    return str(t)
