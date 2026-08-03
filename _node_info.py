"""外部/houdinimcp/_node_info.py — PR 10 节点信息查询的纯函数实现。

提供两个独立、hou 可注入的接口：
- get_node_info(hou, node_path, include_errors, force_cook,
                include_input_details, compact)
  获取节点的详细信息，支持 H20.5+ 的 cookState()，旧版回退到 needsToCook()。
  compact 模式下仅返 path/type/counts 五个字段。
  include_input_details 模式下使用 node.inputConnectors() 一次性取所有输入
  连接（避免 per-input RPC）。
- _cook_state(hou, node)
  辅助函数：H20.5+ 调 node.cookState() 并返 enum 名称尾巴；H<20.5 根据
  node.needsToCook() 回退为 "Dirty" 或 "Cooked"。

设计原则：
- hou 隔离：函数第一参数是 hou（与 _materials / _hscript / _scene /
  _graph_edit 风格一致），便于在单测中以 stub 替换。
- 错误处理：节点不存在抛 ValueError。
- 返回值：所有函数返回 dict，便于 bridge 直接透传。
"""
from . import _common as cmn


# 单条参数列表最大保留条数（与既有行为一致，避免返回过长的 payload）
_PARMS_LIMIT = 20


def _cook_state(hou, node):
    """获取节点的 cook state，并为 H<20.5 回退 needsToCook()。

    Args:
        hou: hou 模块或 stub（保留参数以备后续扩展；当前实现只依赖 node
             对象本身）。
        node: hou.Node 实例。

    Returns:
        str: cook state 名称。H<20.5 时，needsToCook() 为 True 返回
             "Dirty"，否则返回 "Cooked"。
    """
    if hasattr(node, "cookState"):
        state = node.cookState()
        if state is None:
            return "Unknown"
        # hou.cookStateType.Cooked -> "Cooked"
        return str(state).split(".")[-1]
    if hasattr(node, "needsToCook") and node.needsToCook():
        return "Dirty"
    return "Cooked"


def _collect_input_connectors(node):
    """遍历 inputConnectors() 的 HOM 嵌套 tuple 并序列化。

    Returns:
        list of {"input_index": int, "connections": list}。外层按输入索引
        保留空项；每条连接包含 output_node 路径和 output_index。
    """
    out = []
    for input_index, connections in enumerate(node.inputConnectors()):
        entry = {"input_index": input_index, "connections": []}
        for connection in connections:
            try:
                source = connection.inputNode()
            except Exception:
                source = None
            if source is None:
                continue
            entry["connections"].append({
                "output_node": source.path(),
                "output_index": getattr(
                    connection, "outputIndex", lambda: 0)(),
            })
        out.append(entry)
    return out


def parm_is_non_default(parm, template=None):
    """非默认参数判定（get_node_info 与 capture_workflow_snapshot 共用语义）。

    - ``parm.isAtDefault()`` 优先（HOM 规范 API，官方 HDA 实例同样可靠）；
      True/False 直接采用，None（无默认模板）视为默认（排除）。
    - API 缺失/异常回退：``rawValue()``（不触发表达式求值）与模板默认值
      对比；rawValue 缺失再回退 ``eval()``。
    - 模板默认值不可得且 isAtDefault 不可用 → 无法证明为默认，保守按
      非默认收录（保证官方 HDA 实例参数可靠产出）。
    - **绝不触发 cook**：只读 parm 模板与取值。
    """
    try:
        at_default = parm.isAtDefault()
    except Exception:
        at_default = None
    if at_default is not None:
        return not bool(at_default)
    raw = None
    try:
        raw = parm.rawValue()
    except Exception:
        raw = None
    if raw is None:
        try:
            raw = parm.eval()
        except Exception:
            raw = None
    default = None
    if template is not None:
        try:
            default = template.defaultValue()
        except Exception:
            default = None
    if raw is None:
        return True
    if default is None:
        return True
    try:
        return raw != default
    except Exception:
        return True


def _parm_type_name(parm, template):
    """parm 类型名；模板/API 缺失降级 "unknown"（不 crash）。"""
    if template is not None:
        try:
            return template.type().name()
        except Exception:
            pass
    try:
        return parm.parmTemplate().type().name()
    except Exception:
        return "unknown"


def _collect_parameters(node):
    """收集**非默认** parm 信息（前 _PARMS_LIMIT 条）。

    与 capture_workflow_snapshot 共用 ``parm_is_non_default`` 语义：只收
    与参数模板默认值不同的 parm（isAtDefault 优先），官方 HDA 实例同样
    可靠产出；不触发 cook。
    """
    parm_list = []
    for i, parm in enumerate(node.parms()):
        if i >= _PARMS_LIMIT:
            break
        template = None
        try:
            template = parm.parmTemplate()
        except Exception:
            template = None
        if not parm_is_non_default(parm, template):
            continue
        parm_list.append({
            "name": parm.name(),
            "value": str(parm.eval()),
            "type": _parm_type_name(parm, template),
        })
    return parm_list


def get_node_info(hou, node_path, include_errors=True, force_cook=False,
                  include_input_details=False, compact=False):
    """获取节点的详细信息。

    Args:
        hou: hou 模块或 stub。
        node_path: 节点路径。
        include_errors: 是否包含 errors / warnings 字段（默认 True）。
        force_cook: 是否在读取前调 node.cook(force=True)（默认 False）。
        include_input_details: 是否包含 input_connectors 详细连接信息
                              （默认 False，使用 node.inputConnectors()
                               一次性取所有连接）。
        compact: 是否仅返精简字段 path/type/counts（默认 False）。

    Returns:
        dict; compact=True 时仅 5 字段，否则包含完整信息（详见 brief 10.1）。
        节点不存在抛 ValueError。
    """
    node = hou.node(node_path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(node_path))

    # 强制 cook（先于读取，确保 errors / cook_state 是最新状态）
    if force_cook:
        node.cook(force=True)

    inputs = node.inputs()
    output_conns = node.outputConnections()

    if compact:
        return {
            "path": node.path(),
            "type": node.type().name(),
            "children_count": len(node.children()),
            "input_count": len(inputs),
            "output_count": len(output_conns),
        }

    info = {
        "path": node.path(),
        "type": node.type().name(),
        "category": node.type().category().name(),
        "name": node.name(),
        "position": [node.position()[0], node.position()[1]],
        "parent_path": node.parent().path() if node.parent() else "",
        "children_count": len(node.children()),
        "input_count": len(inputs),
        "output_count": len(output_conns),
        "parameters": _collect_parameters(node),
        "cook_state": _cook_state(hou, node),
        "needs_to_cook": node.needsToCook(),
        "is_cooking": (
            node.isCooking() if hasattr(node, "isCooking") else None
        ),
    }

    if include_input_details:
        info["input_connectors"] = _collect_input_connectors(node)

    if include_errors:
        info["errors"] = list(node.errors())
        info["warnings"] = list(node.warnings())

    return info