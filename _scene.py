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
