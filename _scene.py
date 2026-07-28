"""_scene.py — opera-houdini-mcp 场景 CRUD、序列化与场景上下文理解。

模块职责：
- get_scene_info: 场景元信息（含 houdini_version / node_count / file_path）
- save_scene: hou.hipFile.save 包装
- load_scene: hou.hipFile.load 包装 + 缓存失效
- new_scene: hou.hipFile.clear 包装 + 缓存失效
- serialize_scene: 全场景递归序列化（thin wrapper around cmn.serialize_scene_state）
- get_network_overview: 有界 BFS 节点 / 边遍历，预设 ``max_depth`` /
  ``max_nodes`` 预算，返回 ``visited_count / truncated /
  truncation_reason`` 三元 metadata（add-scene-context-selection-materials）
- get_cook_chain: 有界 DFS 上游 cook chain 遍历，path-based visited
  去重 + 预算截断（add-scene-context-selection-materials）
- get_scene_summary: 全场景 category counts + 时间线（add-scene-context-
  selection-materials）
- explain_node: 单节点 type / category / 非默认 parm / 输入输出摘要
  （add-scene-context-selection-materials）

注意：
- get_last_scene_diff 已在 server.py 实现（PR 4），不在本模块重复。
- hou 隔离：本模块不顶层 import hou；hou 通过参数注入（测试用 mock）。
- 缓存失效：load_scene / new_scene 调用 cmn.invalidate_all_caches()。
  PR 5 占位实现为 no-op，PR 6 替换为 NodeTypeCache-aware 版本。
- 4 个新增场景工具的遍历硬约束：path-based ``visited`` 在入队前判定，
  ``max_nodes`` 截断 HOM 遍历；``apply_response_cap`` 仅作为最终 R6
  防线（add-scene-context-selection-materials D1）。
"""
from . import _common as cmn


def get_scene_info(hou):
    """返回场景元信息 dict。

    字段：
    - houdini_version: hou.applicationVersionString() 字符串（H21+；
      旧 hou.houdiniVersion() 已移除）
    - node_count: 全场景节点数（hou.node('/').allSubChildren() 长度，失败回退 0）
    - file_path: hou.hipFile.name() 字符串
    - fps / start_frame / end_frame: 时间线相关
    """
    info = {}
    try:
        info["houdini_version"] = hou.applicationVersionString()
    except Exception:
        info["houdini_version"] = ""
    try:
        root = hou.node("/")
        if root is not None:
            try:
                children = root.allSubChildren()
                info["node_count"] = len(children)
            except Exception:
                # 部分 hou 版本无 allSubChildren；退化用 children()
                info["node_count"] = len(root.children())
        else:
            info["node_count"] = 0
    except Exception:
        info["node_count"] = 0
    try:
        info["file_path"] = hou.hipFile.name() or ""
    except Exception:
        info["file_path"] = ""
    # 补充轻量时间线信息，便于上层 UI 展示
    try:
        info["fps"] = hou.fps()
    except Exception:
        info["fps"] = 0
    try:
        fr = hou.playbar.frameRange()
        info["start_frame"] = fr[0]
        info["end_frame"] = fr[1]
    except Exception:
        info["start_frame"] = 0
        info["end_frame"] = 0
    return info


def save_scene(hou, file_path):
    """保存当前 .hip 文件到 file_path，返回成功 dict。异常向上传播。"""
    hou.hipFile.save(file_path=file_path)
    return {
        "saved": True,
        "file_path": file_path,
    }


def load_scene(hou, file_path):
    """加载 file_path 为当前 .hip 文件，返回成功 dict。

    加载完成后调用 cmn.invalidate_all_caches() 让上层缓存模块感知场景切换
    （PR 5 占位 no-op，PR 6 替换为真实清空）。
    """
    hou.hipFile.load(file_path)
    cmn.invalidate_all_caches()
    return {
        "loaded": True,
        "file_path": file_path,
    }


def new_scene(hou):
    """新建空白场景（hou.hipFile.clear），返回成功 dict。

    suppress_save_prompt=True 避免在 MCP 流程中触发交互式保存提示。
    完成后调用 cmn.invalidate_all_caches()。
    """
    hou.hipFile.clear(suppress_save_prompt=True)
    cmn.invalidate_all_caches()
    return {
        "cleared": True,
    }


def serialize_scene(hou, root_path=None, include_params=False, max_depth=3):
    """全场景递归序列化 thin wrapper，转发到 cmn.serialize_scene_state。

    保留独立入口便于上层 (_scene.*) 调用语义统一；具体序列化逻辑集中在
    _common.serialize_scene_state。
    """
    return cmn.serialize_scene_state(
        hou,
        root_path=root_path,
        include_params=include_params,
        max_depth=max_depth,
    )


# ---------------------------------------------------------------------------
# add-scene-context-selection-materials: 4 个有界场景理解工具
# ---------------------------------------------------------------------------
_DEFAULT_MAX_NODES = 500
_DEFAULT_OVERVIEW_DEPTH = 2
_DEFAULT_COOK_CHAIN_DEPTH = 20
_DEFAULT_EXPLAIN_DEPTH = 1

# 截断 reason 常量；测试断言使用。
_TRUNCATION_REASON_MAX_NODES = "max_nodes"
_TRUNCATION_REASON_MAX_DEPTH = "max_depth"


def _error(code, message, details=None):
    """统一错误 envelope。"""
    payload = {"status": "error", "error": {"code": code,
                                            "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _success(data):
    payload = {"status": "success"}
    for key, value in data.items():
        payload[key] = value
    return payload


def _coerce_non_negative_int(name, value, default):
    """``max_depth`` / ``max_nodes`` 接受 int、拒 bool / 负数。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _resolve_node_path(hou, path):
    """解析 ``path`` → ``hou.Node``；失败抛 ValueError。"""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    node = hou.node(path)
    if node is None:
        raise ValueError("no node at path %r" % path)
    return node


def _node_summary(node):
    """轻量节点摘要：``{path, type, category, name}``。"""
    try:
        path = node.path()
    except Exception:
        path = ""
    try:
        type_name = node.type().name()
    except Exception:
        type_name = ""
    try:
        category = node.type().category().name()
    except Exception:
        category = ""
    try:
        name = node.name()
    except Exception:
        name = ""
    return {"path": path, "type": type_name, "category": category,
            "name": name}


def get_network_overview(hou, parent_path, max_depth=2, max_nodes=500):
    """有界 BFS 节点 / 边遍历。

    从 ``parent_path`` 开始 BFS 走 ``children()`` 关系；节点入队前
    维护 ``visited`` 与预算 ``max_nodes``，达到上限时立刻停止并报告
    ``truncated=True`` / ``truncation_reason='max_nodes'``。边列表
    收集父子关系 ``{from, to, depth}``（**不**走 wires——本 change
    仅做结构化网络拓扑，非 cook chain）。

    Args:
        hou: hou 参数注入
        parent_path: 起始节点路径
        max_depth: BFS 最大深度（0 = 仅 parent_path）
        max_nodes: 节点访问预算（>0 整数）

    Returns:
        dict: ``{"status": "success", "parent_path", "max_depth",
        "max_nodes", "depth_reached", "nodes":[...], "edges":[...],
        "visited_count", "truncated", "truncation_reason"}``
    """
    if not isinstance(parent_path, str) or not parent_path.strip():
        return _error("invalid_parent_path",
                       "parent_path must be a non-empty string",
                       {"field": "parent_path"})
    depth_check = _coerce_non_negative_int("max_depth", max_depth,
                                            _DEFAULT_OVERVIEW_DEPTH)
    if depth_check is None:
        return _error("invalid_max_depth",
                       "max_depth must be a non-negative int",
                       {"field": "max_depth", "value": max_depth})
    nodes_check = _coerce_non_negative_int("max_nodes", max_nodes,
                                            _DEFAULT_MAX_NODES)
    if nodes_check is None:
        return _error("invalid_max_nodes",
                       "max_nodes must be a non-negative int",
                       {"field": "max_nodes", "value": max_nodes})
    if depth_check == 0 or nodes_check == 0:
        # max_depth=0 → 只 parent 自身；max_nodes=0 → 0 预算（含 parent）
        # 但仍然需要先解析 parent_path 以拿到 root；若 parent 不存在
        # 则返回 parent_not_found 与其它路径一致。
        try:
            root = _resolve_node_path(hou, parent_path)
        except ValueError as err:
            return _error("parent_not_found", str(err),
                           {"field": "parent_path", "value": parent_path})
        except Exception as err:
            return _error("parent_resolve_failed", str(err),
                           {"field": "parent_path",
                            "exception": err.__class__.__name__})
        nodes_out = []
        edges_out = []
        visited_count = 0
        if nodes_check > 0:
            try:
                nodes_out.append(_node_summary(root))
                visited_count = 1
            except Exception:
                pass
        return _success({
            "parent_path": parent_path,
            "max_depth": depth_check,
            "max_nodes": nodes_check,
            "depth_reached": 0,
            "nodes": nodes_out,
            "edges": edges_out,
            "visited_count": visited_count,
            "truncated": nodes_check == 0,
            "truncation_reason": (
                _TRUNCATION_REASON_MAX_NODES if nodes_check == 0 else ""),
        })

    try:
        root = _resolve_node_path(hou, parent_path)
    except ValueError as err:
        return _error("parent_not_found", str(err),
                       {"field": "parent_path", "value": parent_path})
    except Exception as err:
        return _error("parent_resolve_failed", str(err),
                       {"field": "parent_path",
                        "exception": err.__class__.__name__})

    nodes_out = []
    edges_out = []
    visited = set()
    depth_reached = 0
    truncated = False
    truncation_reason = ""
    # BFS queue: list of (node, depth)
    queue = [(root, 0)]
    try:
        root_path = root.path()
    except Exception:
        root_path = parent_path
    visited.add(root_path)
    nodes_out.append(_node_summary(root))
    budget_left = nodes_check - 1  # root 已计入

    while queue:
        current, depth = queue.pop(0)
        if budget_left <= 0:
            truncated = True
            truncation_reason = _TRUNCATION_REASON_MAX_NODES
            break
        if depth >= depth_check:
            continue
        try:
            children = current.children() or []
        except Exception:
            children = []
        for child in children:
            try:
                child_path = child.path()
            except Exception:
                child_path = ""
            edges_out.append({
                "from": current.path() if hasattr(current, "path") else "",
                "to": child_path,
                "depth": depth + 1,
            })
            if child_path in visited:
                continue
            if budget_left <= 0:
                truncated = True
                truncation_reason = _TRUNCATION_REASON_MAX_NODES
                break
            visited.add(child_path)
            nodes_out.append(_node_summary(child))
            queue.append((child, depth + 1))
            budget_left -= 1
            if depth + 1 > depth_reached:
                depth_reached = depth + 1
        if truncated:
            break

    if depth_reached > depth_check:
        depth_reached = depth_check

    return _success({
        "parent_path": parent_path,
        "max_depth": depth_check,
        "max_nodes": nodes_check,
        "depth_reached": depth_reached,
        "nodes": nodes_out,
        "edges": edges_out,
        "visited_count": len(visited),
        "truncated": truncated,
        "truncation_reason": truncation_reason,
    })


def get_cook_chain(hou, node_path, max_depth=20, max_nodes=500):
    """有界 DFS 上游 cook chain 遍历。

    沿 ``inputs()`` 关系向上递归；path-based ``visited`` 在入栈前判定，
    因此菱形 / 环结构自动去重。``max_nodes`` 限制 HOM 访问节点数；返
    回 ``truncated=True`` 表示 ``max_nodes`` 触发。

    Args:
        hou: hou 参数注入
        node_path: 起始节点路径（自身总是第一个被记录）
        max_depth: 最大向上深度（0 = 仅 node_path）
        max_nodes: 节点访问预算

    Returns:
        dict: ``{"status": "success", "root_path", "max_depth",
        "max_nodes", "depth_reached", "chain":[...], "edges":[...],
        "visited_count", "truncated", "truncation_reason"}``
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return _error("invalid_node_path",
                       "node_path must be a non-empty string",
                       {"field": "node_path"})
    depth_check = _coerce_non_negative_int("max_depth", max_depth,
                                            _DEFAULT_COOK_CHAIN_DEPTH)
    if depth_check is None:
        return _error("invalid_max_depth",
                       "max_depth must be a non-negative int",
                       {"field": "max_depth", "value": max_depth})
    nodes_check = _coerce_non_negative_int("max_nodes", max_nodes,
                                            _DEFAULT_MAX_NODES)
    if nodes_check is None:
        return _error("invalid_max_nodes",
                       "max_nodes must be a non-negative int",
                       {"field": "max_nodes", "value": max_nodes})
    if depth_check == 0 or nodes_check == 0:
        # max_depth=0 → 只 root 自身；max_nodes=0 → 0 预算
        try:
            root = _resolve_node_path(hou, node_path)
        except ValueError as err:
            return _error("node_not_found", str(err),
                           {"field": "node_path", "value": node_path})
        except Exception as err:
            return _error("node_resolve_failed", str(err),
                           {"field": "node_path",
                            "exception": err.__class__.__name__})
        chain_out = []
        edges_out = []
        visited_count = 0
        if nodes_check > 0:
            try:
                chain_out.append(_node_summary(root))
                visited_count = 1
            except Exception:
                pass
        return _success({
            "root_path": node_path,
            "max_depth": depth_check,
            "max_nodes": nodes_check,
            "depth_reached": 0,
            "chain": chain_out,
            "edges": edges_out,
            "visited_count": visited_count,
            "truncated": nodes_check == 0,
            "truncation_reason": (
                _TRUNCATION_REASON_MAX_NODES if nodes_check == 0 else ""),
        })

    try:
        root = _resolve_node_path(hou, node_path)
    except ValueError as err:
        return _error("node_not_found", str(err),
                       {"field": "node_path", "value": node_path})
    except Exception as err:
        return _error("node_resolve_failed", str(err),
                       {"field": "node_path",
                        "exception": err.__class__.__name__})

    chain_out = []
    edges_out = []
    visited = set()
    depth_reached = 0
    truncated = False
    truncation_reason = ""

    try:
        root_path = root.path()
    except Exception:
        root_path = node_path
    visited.add(root_path)
    chain_out.append(_node_summary(root))
    budget_left = nodes_check - 1

    # DFS 栈：每项 (node, depth)
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if budget_left <= 0:
            truncated = True
            truncation_reason = _TRUNCATION_REASON_MAX_NODES
            break
        if depth >= depth_check:
            continue
        try:
            inputs = current.inputs() or []
        except Exception:
            inputs = []
        # 反向遍历以保持"先访问的 input index 较小"顺序
        for index, src in enumerate(inputs):
            if src is None:
                continue
            try:
                src_path = src.path()
            except Exception:
                src_path = ""
            edges_out.append({
                "from": src_path,
                "to": current.path() if hasattr(current, "path") else "",
                "input_index": index,
            })
            if src_path in visited:
                continue
            if budget_left <= 0:
                truncated = True
                truncation_reason = _TRUNCATION_REASON_MAX_NODES
                break
            visited.add(src_path)
            chain_out.append(_node_summary(src))
            stack.append((src, depth + 1))
            budget_left -= 1
            if depth + 1 > depth_reached:
                depth_reached = depth + 1
        if truncated:
            break

    if depth_reached > depth_check:
        depth_reached = depth_check

    return _success({
        "root_path": node_path,
        "max_depth": depth_check,
        "max_nodes": nodes_check,
        "depth_reached": depth_reached,
        "chain": chain_out,
        "edges": edges_out,
        "visited_count": len(visited),
        "truncated": truncated,
        "truncation_reason": truncation_reason,
    })


def get_scene_summary(hou, max_nodes=2000):
    """全场景 category counts + 时间线元信息 + 截断 metadata。

    复用 ``get_network_overview`` 走 ``/`` 根，但只关心 category
    分布；不返回完整节点列表（受 ``max_nodes`` 预算控制）。
    """
    if not isinstance(max_nodes, bool) and not isinstance(max_nodes, int):
        return _error("invalid_max_nodes",
                       "max_nodes must be an integer",
                       {"field": "max_nodes", "value": max_nodes})
    if isinstance(max_nodes, bool) or max_nodes < 0:
        return _error("invalid_max_nodes",
                       "max_nodes must be a non-negative integer",
                       {"field": "max_nodes", "value": max_nodes})

    overview = get_network_overview(hou, "/",
                                     max_depth=2147483647,
                                     max_nodes=max_nodes)
    if overview.get("status") == "error":
        return overview

    category_counts = {}
    for entry in overview["nodes"]:
        cat = entry.get("category") or "unknown"
        category_counts[cat] = category_counts.get(cat, 0) + 1

    frame = 0
    fps = 0
    start_frame = 0
    end_frame = 0
    try:
        frame = float(hou.frame())
    except Exception:
        try:
            frame = float(hou.time())
        except Exception:
            frame = 0
    try:
        fps = float(hou.fps())
    except Exception:
        fps = 0
    try:
        fr = hou.playbar.frameRange()
        start_frame = float(fr[0])
        end_frame = float(fr[1])
    except Exception:
        start_frame = 0
        end_frame = 0

    return _success({
        "total_nodes": overview["visited_count"],
        "category_counts": category_counts,
        "frame": frame,
        "fps": fps,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "max_nodes": max_nodes,
        "truncated": overview["truncated"],
        "truncation_reason": overview["truncation_reason"],
    })


def explain_node(hou, node_path, include_params=False,
                  max_params=64, parm_depth=_DEFAULT_EXPLAIN_DEPTH):
    """单节点 type / category / 非默认 parm / 输入输出摘要。

    - 节点不可解析 → ``node_not_found``。
    - ``include_params=True`` 时附 ``non_default_parameters``（与默认
      值不同的 parm），最多 ``max_params`` 条；负数 / bool 拒绝。
    - inputs / outputs 走 ``inputs()`` / ``outputs()``，返回路径 + index。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return _error("invalid_node_path",
                       "node_path must be a non-empty string",
                       {"field": "node_path"})
    if not isinstance(include_params, bool):
        return _error("invalid_include_params",
                       "include_params must be a boolean",
                       {"field": "include_params",
                        "value_type": type(include_params).__name__})
    if isinstance(max_params, bool) or not isinstance(max_params, int) or max_params < 0:
        return _error("invalid_max_params",
                       "max_params must be a non-negative integer",
                       {"field": "max_params", "value": max_params})

    try:
        node = _resolve_node_path(hou, node_path)
    except ValueError as err:
        return _error("node_not_found", str(err),
                       {"field": "node_path", "value": node_path})
    except Exception as err:
        return _error("node_resolve_failed", str(err),
                       {"field": "node_path",
                        "exception": err.__class__.__name__})

    summary = _node_summary(node)
    # inputs
    inputs = []
    try:
        for index, src in enumerate(node.inputs() or []):
            if src is None:
                continue
            try:
                inputs.append({"index": index, "path": src.path()})
            except Exception:
                inputs.append({"index": index, "path": ""})
    except Exception:
        pass
    # outputs
    outputs = []
    try:
        for index, dst in enumerate(node.outputs() or []):
            if dst is None:
                continue
            try:
                outputs.append({"index": index, "path": dst.path()})
            except Exception:
                outputs.append({"index": index, "path": ""})
    except Exception:
        pass

    result = {
        "path": summary["path"],
        "name": summary["name"],
        "type": summary["type"],
        "category": summary["category"],
        "input_count": len(inputs),
        "output_count": len(outputs),
        "inputs": inputs,
        "outputs": outputs,
    }

    if include_params:
        params = {}
        try:
            parms = node.parms() or []
        except Exception:
            parms = []
        for pt in parms:
            try:
                pname = pt.name()
                pval = pt.eval()
            except Exception:
                continue
            try:
                default = pt.defaultValue()
            except Exception:
                default = None
            # 标量比较；list/tuple/dict 用 _json_safe 序列化
            try:
                if isinstance(pval, (list, tuple)):
                    pval_norm = list(pval)
                else:
                    pval_norm = pval
            except Exception:
                pval_norm = pval
            if pval_norm == default:
                continue
            if len(params) >= max_params:
                break
            try:
                safe_value = cmn._json_safe_hou_value(hou, pval,
                                                       max_depth=2)
            except Exception:
                safe_value = str(pval)
            params[pname] = safe_value
        result["non_default_parameters"] = params
        result["non_default_parameter_count"] = len(params)
        result["max_params"] = max_params

    return _success(result)


# ---------------------------------------------------------------------------
# add-takes-and-cache-tools: 4 个 Takes 工具
# ---------------------------------------------------------------------------
#
# 设计契约（design.md §Takes + spec Requirement: Takes 工具）：
# - 全部走 hou.takes.takes / hou.takes.findTake / hou.takes.setCurrentTake /
#   hou.Take.addChildTake / hou.Take.addParmTuple 真实 surface。
# - list_takes / get_current_take 走只读 hou API。
# - set_current_take 必须把 hou.Take 对象传给 setCurrentTake（**绝不传字符串**）。
# - create_take 在任何写入前完成 parent / parm 预校验；预校验失败不留下部分 take。
# - include 阶段：临时把新 hou.Take 设为 current、调用 addParmTuple、finally 恢复。
# - hou 通过参数注入；本模块顶层不 import hou，零新依赖。
# - 所有成功 / 错误返回统一过 cmn.apply_response_cap。
# ---------------------------------------------------------------------------

_DEFAULT_TAKES_LIMIT = 256


def _take_attr_str(take, name):
    """安全取 take.name()/path() 字符串，缺失/异常返空串。"""
    getter = getattr(take, name, None)
    if not callable(getter):
        return ""
    try:
        return str(getter())
    except Exception:
        return ""


def _take_current(take):
    """take.isCurrent() 安全布尔，缺失返 False。"""
    getter = getattr(take, "isCurrent", None)
    if not callable(getter):
        return False
    try:
        return bool(getter())
    except Exception:
        return False


def _take_parent_path(take):
    """take.parent() 安全的 path；parent is None（root take）返 None。"""
    getter = getattr(take, "parent", None)
    if not callable(getter):
        return None
    try:
        parent = getter()
    except Exception:
        return None
    if parent is None:
        return None
    parent_path = _take_attr_str(parent, "path")
    return parent_path or None


def list_takes(hou):
    """枚举全部 hou.takes.takes()，返回 name / path / parent / current。

    受 ``_DEFAULT_TAKES_LIMIT`` 上限约束；超限返 truncated=True。
    """
    takes_fn = getattr(getattr(hou, "takes", None), "takes", None)
    if not callable(takes_fn):
        return cmn.apply_response_cap(_error(
            "takes_unavailable",
            "hou.takes.takes() is not available on this Houdini",
            details={"houdini_version": cmn._json_safe_hou_value(
                hou, getattr(hou, "applicationVersionString", lambda: "")(), max_depth=1)}))
    try:
        takes = list(takes_fn() or [])
    except Exception as exc:
        return cmn.apply_response_cap(_error("takes_query_failed", exc))
    entries = []
    total = len(takes)
    for take in takes[:_DEFAULT_TAKES_LIMIT]:
        try:
            entries.append({
                "name": _take_attr_str(take, "name"),
                "path": _take_attr_str(take, "path"),
                "parent": _take_parent_path(take),
                "current": _take_current(take),
            })
        except Exception:
            continue
    return cmn.apply_response_cap(_success({
        "takes": entries,
        "count": len(entries),
        "total": total,
        "truncated": total > _DEFAULT_TAKES_LIMIT,
    }))


def get_current_take(hou):
    """返回 hou.takes.currentTake() 的 name / path / parent。"""
    takes_mod = getattr(hou, "takes", None)
    current_fn = getattr(takes_mod, "currentTake", None)
    if not callable(current_fn):
        return cmn.apply_response_cap(_error(
            "takes_unavailable",
            "hou.takes.currentTake() is not available on this Houdini"))
    try:
        current = current_fn()
    except Exception as exc:
        return cmn.apply_response_cap(_error("current_take_query_failed", exc))
    if current is None:
        return cmn.apply_response_cap(_success(
            {"name": "", "path": "", "parent": None, "current": False}))
    return cmn.apply_response_cap(_success({
        "name": _take_attr_str(current, "name"),
        "path": _take_attr_str(current, "path"),
        "parent": _take_parent_path(current),
        "current": True,
    }))


def _resolve_take(hou, identifier):
    """用 hou.takes.findTake 解析 identifier → hou.Take。

    - identifier 必须是非空字符串。
    - 找不到 / hou.takes.findTake 不可用时返 (None, error_dict)。
    - findTake 只接受 name 或 path（不接受 hou.Take 对象），本 helper
      对两端都尝试：先用原值，再用 path。
    """
    if not isinstance(identifier, str) or not identifier.strip():
        return None, _error("invalid_take_identifier",
                             "take identifier must be a non-empty string")
    find_take = getattr(getattr(hou, "takes", None), "findTake", None)
    if not callable(find_take):
        return None, _error("find_take_unavailable",
                             "hou.takes.findTake() is not available on this Houdini")
    trimmed = identifier.strip()
    for candidate in (trimmed, trimmed.lstrip("/")):
        try:
            take = find_take(candidate)
        except Exception:
            take = None
        if take is not None:
            return take, None
    return None, _error("take_not_found",
                        "No take found for identifier: " + trimmed,
                        details={"identifier": trimmed})


def set_current_take(hou, name_or_path):
    """用 hou.takes.findTake 解析真实 hou.Take 后传给 setCurrentTake。

    - 永不传字符串给 setCurrentTake。
    - identifier 解析失败 / 歧义时拒绝。
    """
    take, error = _resolve_take(hou, name_or_path)
    if error is not None:
        return cmn.apply_response_cap(error)
    set_current = getattr(getattr(hou, "takes", None), "setCurrentTake", None)
    if not callable(set_current):
        return cmn.apply_response_cap(_error(
            "set_current_take_unavailable",
            "hou.takes.setCurrentTake() is not available on this Houdini"))
    try:
        set_current(take)
    except Exception as exc:
        return cmn.apply_response_cap(_error(
            "set_current_take_failed", exc,
            details={"identifier": str(name_or_path),
                     "take_path": _take_attr_str(take, "path")}))
    return cmn.apply_response_cap(_success({
        "name": _take_attr_str(take, "name"),
        "path": _take_attr_str(take, "path"),
        "parent": _take_parent_path(take),
        "current": True,
    }))


def _resolve_parm_tuple(hou, parm_path):
    """把 parm path 解析为 hou.ParmTuple。

    - 先用 hou.parmTuple(path) 解析为 tuple path。
    - 若失败，视为 component parm path：hou.parm(path).tuple()。
    - 仍找不到 / 不可调用 / 不可编辑时返 (None, error_dict)。
    """
    if not isinstance(parm_path, str) or not parm_path.strip():
        return None, _error("invalid_parm_path",
                             "parm path must be a non-empty string")
    path = parm_path.strip()
    parm_tuple_fn = getattr(hou, "parmTuple", None)
    if callable(parm_tuple_fn):
        try:
            pt = parm_tuple_fn(path)
        except Exception:
            pt = None
        if pt is not None:
            return pt, None
    parm_fn = getattr(hou, "parm", None)
    if callable(parm_fn):
        try:
            parm = parm_fn(path)
        except Exception:
            parm = None
        if parm is not None:
            tuple_fn = getattr(parm, "tuple", None)
            if callable(tuple_fn):
                try:
                    pt = tuple_fn()
                except Exception:
                    pt = None
                if pt is not None:
                    return pt, None
    return None, _error("parm_not_found",
                        "No parm tuple found for path: " + path,
                        details={"parm_path": path})


def create_take(hou, name, include_parms=None, parent_take=None):
    """创建 child take，预校验 parent / parm 后再调 addChildTake / addParmTuple。

    - name 必填；重复 name 拒绝。
    - parent_take 缺省时使用当前 take；非字符串 / 字符串走 findTake 解析。
    - include_parms（list of parm path）全部解析成功后才创建 take；任一
      失败 → 整次拒绝（**不**留部分 take）。
    - 创建后若 include_parms 非空：保存原 current，临时切到新 hou.Take，
      逐个调 addParmTuple，finally 恢复原 take。
    """
    if not isinstance(name, str) or not name.strip():
        return cmn.apply_response_cap(_error(
            "invalid_take_name",
            "take name must be a non-empty string"))
    if "/" in name:
        return cmn.apply_response_cap(_error(
            "invalid_take_name",
            "take name must not contain '/' (use a single segment)",
            details={"field": "name", "value": name}))
    trimmed_name = name.strip()

    # parent resolve
    if parent_take is None or parent_take == "":
        # use currentTake
        current_fn = getattr(getattr(hou, "takes", None), "currentTake", None)
        if not callable(current_fn):
            return cmn.apply_response_cap(_error(
                "current_take_unavailable",
                "hou.takes.currentTake() is not available for parent fallback"))
        try:
            parent = current_fn()
        except Exception as exc:
            return cmn.apply_response_cap(_error(
                "parent_take_query_failed", exc))
    else:
        if isinstance(parent_take, str):
            parent, parent_error = _resolve_take(hou, parent_take)
            if parent_error is not None:
                return cmn.apply_response_cap(parent_error)
        else:
            # hou.Take 对象（兼容）
            parent = parent_take
    if parent is None:
        return cmn.apply_response_cap(_error(
            "parent_take_unavailable",
            "Resolved parent take is None"))

    # duplicate-name check
    find_take = getattr(getattr(hou, "takes", None), "findTake", None)
    if callable(find_take):
        try:
            existing = find_take(trimmed_name)
        except Exception:
            existing = None
        if existing is not None:
            return cmn.apply_response_cap(_error(
                "take_name_conflict",
                "A take with this name already exists: " + trimmed_name,
                details={"name": trimmed_name,
                         "existing_path": _take_attr_str(existing, "path")}))

    # pre-validate include_parms (atomic pre-check, no writes)
    resolved_tuples = []
    if include_parms is not None:
        if isinstance(include_parms, str):
            include_list = [include_parms]
        else:
            try:
                include_list = [str(item) for item in list(include_parms)]
            except Exception:
                return cmn.apply_response_cap(_error(
                    "invalid_include_parms",
                    "include_parms must be a path string or list of paths"))
        for parm_path in include_list:
            if not parm_path.strip():
                return cmn.apply_response_cap(_error(
                    "invalid_include_parms",
                    "include_parms entries must be non-empty strings",
                    details={"parm_path": parm_path}))
            pt, pt_error = _resolve_parm_tuple(hou, parm_path)
            if pt_error is not None:
                return cmn.apply_response_cap(pt_error)
            # 重复 tuple path 检测
            try:
                pt_name = pt.name()
                pt_node = pt.node()
                node_path = pt_node.path() if pt_node is not None else ""
            except Exception:
                pt_name = ""
                node_path = ""
            key = (node_path, pt_name)
            if key in [t["_key"] for t in resolved_tuples]:
                return cmn.apply_response_cap(_error(
                    "duplicate_parm_tuple",
                    "include_parms contains duplicate parm tuple: " + parm_path,
                    details={"parm_path": parm_path}))
            resolved_tuples.append({
                "_key": key,
                "node_path": node_path,
                "parm_tuple": pt_name,
                "_tuple": pt,  # keep reference for later addParmTuple
            })

    # create the take (single addChildTake call)
    add_child = getattr(parent, "addChildTake", None)
    if not callable(add_child):
        return cmn.apply_response_cap(_error(
            "add_child_take_unavailable",
            "parent take does not expose addChildTake()"))
    try:
        new_take = add_child(trimmed_name)
    except Exception as exc:
        return cmn.apply_response_cap(_error(
            "add_child_take_failed", exc,
            details={"parent": _take_attr_str(parent, "path"),
                     "name": trimmed_name}))
    if new_take is None:
        return cmn.apply_response_cap(_error(
            "add_child_take_failed",
            "parent.addChildTake returned None",
            details={"parent": _take_attr_str(parent, "path"),
                     "name": trimmed_name}))

    # if include: save current, switch, addParmTuple, restore
    applied = []
    if resolved_tuples:
        takes_mod = getattr(hou, "takes", None)
        current_fn = getattr(takes_mod, "currentTake", None)
        set_current = getattr(takes_mod, "setCurrentTake", None)
        if not callable(set_current):
            try:
                new_take.destroy()
            except Exception:
                pass
            return cmn.apply_response_cap(_error(
                "set_current_take_unavailable",
                "hou.takes.setCurrentTake() is required for include_parms"))
        previous = current_fn() if callable(current_fn) else None
        try:
            set_current(new_take)
            for entry in resolved_tuples:
                node_path = entry["node_path"]
                parm_tuple_name = entry["parm_tuple"]
                tup = entry.get("_tuple")
                if tup is None:
                    return cmn.apply_response_cap(_error(
                        "include_parm_resolve_failed",
                        "Pre-resolved parm tuple no longer available",
                        details={"node_path": node_path,
                                 "parm_tuple": parm_tuple_name}))
                add_pt = getattr(new_take, "addParmTuple", None)
                if not callable(add_pt):
                    return cmn.apply_response_cap(_error(
                        "add_parm_tuple_unavailable",
                        "new take does not expose addParmTuple()"))
                try:
                    add_pt(tup)
                except Exception as exc:
                    return cmn.apply_response_cap(_error(
                        "add_parm_tuple_failed", exc,
                        details={"node_path": node_path,
                                 "parm_tuple": parm_tuple_name}))
                applied.append({
                    "node_path": node_path,
                    "parm_tuple": parm_tuple_name,
                })
        finally:
            try:
                if previous is not None and callable(set_current):
                    set_current(previous)
            except Exception:
                pass

    return cmn.apply_response_cap(_success({
        "name": _take_attr_str(new_take, "name"),
        "path": _take_attr_str(new_take, "path"),
        "parent": _take_parent_path(new_take),
        "include_parms": applied,
    }))
