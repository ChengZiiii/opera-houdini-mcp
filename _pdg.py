"""_pdg.py — PDG/TOPs cook、状态、work item、dirty、cancel 工具。

核心约束（见 openspec/changes/add-pdg-tops-tools）：
- 5 个公共函数（pdg_cook/pdg_status/pdg_workitems/pdg_dirty/pdg_cancel）都
  显式接收注入的 ``hou``；模块顶层不导入 ``hou``，不新增依赖。
- 控制面一律走 ``hou.TopNode``：cookWorkItems(block=False)/
  getCookState(force=True)/workItemStates()/dirtyWorkItems(remove_outputs=
  False)/cancelCook()。getPDGNode()/getPDGGraphContext() 仅用于只读 work
  item 详情，**不得**承担 cook/dirty/cancel 控制。
- ``executeGraph(block=False)`` 仅作经实机探针证明必要的 deprecated
  fallback（节点不提供 cookWorkItems 时），并在响应中披露 fallback。
- 进程内有界 cook handle registry：``_COOK_REGISTRY``（cook_id -> entry）
  与 ``_NODE_TO_COOK``（node_path -> active cook_id）。registry 在 server
  重启后失效，所有响应标明 ``scope: process``。
- 同一节点 active cook 重复调用返回同一 handle（``already_running``），
  **不**启动第二个 cook；terminal 后的新调用生成新 ID。未知/过期/属他节
  点的 cook_id 返回结构化错误。
- blocking 只轮询 handle；超时返回 ``timed_out``，handle 保持 active 且
  **不**自动 cancel；后续 status/cancel 仍使用同一 handle。
- cancel 幂等：对已 terminal/cancelled handle 返回稳定 cancelled 状态。
- cook/dirty/cancel 属调度/运行态 no-undo 操作，**不**进入
  ``hou.undos.group``；status/workitems 只读。
- 所有公共返回（success/warning/error）均经过 ``apply_response_cap``；
  workitems 另受 max_items 限制。
"""
import time
import uuid

from . import _common as cmn


_MAX_WORK_ITEMS = 1000
_MAX_HANDLES = 256
_DEFAULT_TIMEOUT = 300
_POLL_INTERVAL = 0.1

# cook state 名归一化后判定 terminal 的 token 集合。cook 进入 cooked
# （hou.topCookState.Cooked，即成功完成）/failed/canceled 即视为完成；
# 其余（cooking/uncooked/空/未知）继续轮询，由 timeout 兜底。仅显式列举
# 已知终态，避免把未知态误判为完成。
_TERMINAL_STATE_TOKENS = frozenset((
    "cooked", "success", "succeeded", "failed", "failure",
    "canceled", "cancelled", "complete", "completed",
))

# pdg.workItemState 序号 -> 状态名（Houdini 公开枚举，H18+ 稳定）。
# hou.TopNode.workItemStates() 返回按此序号索引的计数 tuple（非 dict）。
_WORK_ITEM_STATE_ORDINALS = (
    "undefined",     # 0
    "uncooked",      # 1
    "waiting",       # 2
    "scheduled",     # 3
    "cooking",       # 4
    "cookedsuccess", # 5
    "cookedcache",   # 6
    "cookedfail",    # 7
    "cookedcancel",  # 8
    "dirty",         # 9
    "unknown",       # 10
)

# 进程内有界 cook handle registry。cook_id -> entry(dict)。
# node_path -> 当前 active cook_id。terminal 后从 _NODE_TO_COOK 移除，
# 但 _COOK_REGISTRY 保留终态条目供幂等 cancel/status 查询，直至被淘汰。
# server 重启（模块重载）后整体失效，响应标 scope: process。
_COOK_REGISTRY = {}
_NODE_TO_COOK = {}


def _cap(value):
    return cmn.apply_response_cap(value)


def _error(code, message, **extra):
    payload = {
        "status": "error",
        "error": {
            "code": code,
            "message": str(message),
        },
    }
    payload.update(extra)
    return payload


def _warning(code, message, **extra):
    payload = {
        "status": "warning",
        "_warning": {
            "code": code,
            "message": str(message),
        },
    }
    payload.update(extra)
    return payload


def _bounded_int(value, default, minimum=0, maximum=_MAX_WORK_ITEMS):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(minimum, min(maximum, value))


def _bounded_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DEFAULT_TIMEOUT
    if value <= 0:
        return _DEFAULT_TIMEOUT
    # 上限 3600s，防止误传巨大值导致 blocking 长时间挂起。
    return min(3600.0, float(value))


def _call_value(obj, name, default=None):
    value = getattr(obj, name, default)
    if callable(value):
        try:
            return value()
        except Exception:
            return default
    return value


def _name_of(value):
    if value is None:
        return ""
    name = _call_value(value, "name", None)
    if name is not None:
        return str(name)
    try:
        return str(value)
    except Exception:
        return ""


def _hom_permission_error(hou, error):
    hom_type = getattr(hou, "PermissionError", None)
    return (isinstance(hom_type, type)
            and hom_type is not PermissionError
            and isinstance(error, hom_type))


def _exception_error(hou, error, default_code, **extra):
    if _hom_permission_error(hou, error):
        return _error("hom_permission_error", error, **extra)
    if isinstance(error, PermissionError):
        return _error("python_permission_error", error, **extra)
    return _error(default_code, error, **extra)


# ---------------------------------------------------------------------------
# cook state 探测与归一化
# ---------------------------------------------------------------------------
def _state_name(state):
    """把 pdg cookState / workItemState 归一化为小写字符串。

    兼容三类真实 surface：hou.EnumValue（``.name()`` 方法）、
    pdg.workItemState（``.name`` 字符串属性）、以及 mock 对象。
    """
    if state is None:
        return ""
    name = getattr(state, "name", None)
    if callable(name):
        try:
            value = name()
            if value is not None:
                return str(value).strip().lower()
        except Exception:
            pass
    elif isinstance(name, str) and name:
        return name.strip().lower()
    try:
        return str(state).strip().lower()
    except Exception:
        return ""


def _is_terminal(state_name):
    return state_name in _TERMINAL_STATE_TOKENS


def _probe_state(hou, node):
    """通过 getCookState(force=True) 读取 cook 状态；force 不被接受时回退。"""
    getter = getattr(node, "getCookState", None)
    if not callable(getter):
        return None
    try:
        return getter(force=True)
    except TypeError:
        # 旧/兼容签名可能不接受 force 关键字。
        try:
            return getter()
        except Exception:
            return None
    except Exception:
        return None


def _safe_count(value):
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def _work_item_states(node):
    """读取 workItemStates() 返回的 work item 计数，归一为 JSON-safe dict。

    返回 (counts_dict, total)。读取失败返回 ({}, 0)。

    真实 Houdini surface：``hou.TopNode.workItemStates()`` 返回按
    ``pdg.workItemState`` 序号索引的 ``tuple`` of ``int``（非 dict）；
    用 ``_WORK_ITEM_STATE_ORDINALS`` 把序号映射为小写状态名。兼容 dict
    与 (state, count) 对序列（mock / 未来版本）。
    """
    getter = getattr(node, "workItemStates", None)
    if not callable(getter):
        return {}, 0
    try:
        raw = getter()
    except Exception:
        return {}, 0

    counts = {}
    total = 0

    if isinstance(raw, dict):
        for key, value in raw.items():
            count = _safe_count(value)
            label = _state_name(key) or str(key)
            if count > 0 and label:
                counts[label] = counts.get(label, 0) + count
                total += count
        return counts, total

    try:
        sequence = list(raw)
    except Exception:
        return {}, 0
    if not sequence:
        return {}, 0

    # tuple/list of int：按 pdg.workItemState 序号索引。
    if all(isinstance(x, int) and not isinstance(x, bool) for x in sequence):
        for index, count in enumerate(sequence):
            value = _safe_count(count)
            if value <= 0:
                continue
            if index < len(_WORK_ITEM_STATE_ORDINALS):
                label = _WORK_ITEM_STATE_ORDINALS[index]
            else:
                label = "state_{0}".format(index)
            counts[label] = counts.get(label, 0) + value
            total += value
        return counts, total

    # 兼容 (state, count) 对序列 / dict 元素。
    for entry in sequence:
        if isinstance(entry, dict):
            for key, value in entry.items():
                count = _safe_count(value)
                label = _state_name(key) or str(key)
                if count > 0 and label:
                    counts[label] = counts.get(label, 0) + count
                    total += count
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            key, value = entry
            count = _safe_count(value)
            label = _state_name(key) or str(key)
            if count > 0 and label:
                counts[label] = counts.get(label, 0) + count
                total += count
    return counts, total


# ---------------------------------------------------------------------------
# 节点解析与 TopNode 校验
# ---------------------------------------------------------------------------
def _resolve_top(hou, node_path):
    """解析 node_path 并验证它提供 hou.TopNode 控制面。"""
    if not isinstance(node_path, str) or not node_path.strip():
        return None, _error(
            "invalid_node_path", "node_path must be a non-empty string")
    try:
        node = hou.node(node_path)
    except Exception as error:
        return None, _error(
            "node_resolution_failed", error, node_path=node_path)
    if node is None:
        return None, _error(
            "node_not_found", "TOP node not found: " + node_path,
            node_path=node_path)
    if not _is_top_node(hou, node):
        return None, _error(
            "not_top_node",
            "node does not provide the hou.TopNode control surface",
            node_path=node_path)
    return node, None


def _is_top_node(hou, node):
    """优先 isinstance(hou.TopNode)；不可得时退化为控制面方法存在性探测。"""
    top_type = getattr(hou, "TopNode", None)
    if isinstance(top_type, type):
        try:
            return isinstance(node, top_type)
        except Exception:
            pass
    # mock / 兼容对象：要求至少具备 getCookState 控制面。
    return callable(getattr(node, "getCookState", None))


def _get_pdg_node(node):
    getter = getattr(node, "getPDGNode", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 进程内 handle registry（有界）
# ---------------------------------------------------------------------------
def _new_cook_id():
    return "pdg-" + uuid.uuid4().hex


def _evict_if_needed():
    """registry 满时淘汰最旧的终态条目；无终态则淘汰最旧条目。"""
    if len(_COOK_REGISTRY) < _MAX_HANDLES:
        return
    terminal = [(cid, entry) for cid, entry in _COOK_REGISTRY.items()
                if entry.get("terminal")]
    pool = terminal if terminal else list(_COOK_REGISTRY.items())
    oldest = min(pool, key=lambda pair: pair[1].get("started_monotonic", 0.0))
    del _COOK_REGISTRY[oldest[0]]


def _store_handle(node_path, cook_id, entry):
    _evict_if_needed()
    _COOK_REGISTRY[cook_id] = entry
    _NODE_TO_COOK[node_path] = cook_id


def _close_active(node_path):
    """terminal 后关闭 active 映射；registry 条目保留终态供幂等查询。"""
    _NODE_TO_COOK.pop(node_path, None)


def _handle_summary(entry):
    if entry is None:
        return None
    return {
        "cook_id": entry.get("cook_id"),
        "node_path": entry.get("node_path"),
        "status": entry.get("status"),
        "state": entry.get("state", ""),
        "terminal": bool(entry.get("terminal")),
        "started_at": entry.get("started_at"),
        "timeout_seconds": entry.get("timeout_seconds"),
        "fallback_used": bool(entry.get("fallback_used")),
        "started_count": int(entry.get("started_count", 0)),
    }


def _cook_base_payload(entry):
    payload = {
        "status": entry.get("status", "started"),
        "cook_id": entry.get("cook_id"),
        "node_path": entry.get("node_path"),
        "started_at": entry.get("started_at"),
        "state": entry.get("state", ""),
        "scope": "process",
        "fallback_used": bool(entry.get("fallback_used")),
        "started_count": int(entry.get("started_count", 0)),
    }
    if entry.get("terminal"):
        payload["terminal"] = True
    if entry.get("status") == "timed_out":
        payload["timed_out"] = True
        payload["message"] = (
            "cook did not reach a terminal state before timeout_seconds; "
            "the handle remains active and may be polled via pdg_status or "
            "cancelled via pdg_cancel")
    return payload


def _start_cook(hou, node):
    """启动 cook；优先 cookWorkItems(block=False)，否则 deprecated fallback。

    返回 (fallback_used, error_payload_or_None)。
    """
    cook = getattr(node, "cookWorkItems", None)
    if callable(cook):
        try:
            cook(block=False)
            return False, None
        except Exception as exc:
            return False, _exception_error(
                hou, exc, "cook_start_failed", node_path=node.path())
    execute = getattr(node, "executeGraph", None)
    if callable(execute):
        # 仅当节点确实不提供 cookWorkItems 时才走 deprecated fallback。
        try:
            execute(block=False)
            return True, None
        except Exception as exc:
            return True, _exception_error(
                hou, exc, "cook_start_failed_fallback", node_path=node.path())
    return False, _error(
        "no_cook_entry_point",
        "node exposes neither cookWorkItems nor executeGraph",
        node_path=node.path())


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------
def pdg_cook(hou, node_path, blocking=False, timeout_seconds=300):
    """启动 PDG/TOPs cook 并返回进程内 handle。

    blocking=False 立即返回；blocking=True 轮询 getCookState(force=True)
    至 terminal 或 timeout_seconds。同节点已有 active cook 时返回同一
    handle（already_running），不启动第二个 cook。terminal 后的新调用
    生成新 handle。
    """
    node, error = _resolve_top(hou, node_path)
    if error is not None:
        return _cap(error)
    timeout = _bounded_timeout(timeout_seconds)
    is_blocking = bool(blocking)

    # 幂等：同节点已有 active handle 时先探针其状态。
    active_id = _NODE_TO_COOK.get(node_path)
    if active_id is not None and active_id in _COOK_REGISTRY:
        entry = _COOK_REGISTRY[active_id]
        state_name = _state_name(_probe_state(hou, node))
        entry["state"] = state_name
        if _is_terminal(state_name):
            # 上一次 cook 已 terminal：关闭 active 映射，下面启动新 cook。
            entry["terminal"] = True
            entry["status"] = state_name or "terminal"
            _close_active(node_path)
        else:
            entry["status"] = "already_running"
            if is_blocking:
                entry = _poll_until_terminal(hou, node, entry)
            return _cap(_cook_base_payload(entry))

    cook_id = _new_cook_id()
    started_at = time.time()
    started_monotonic = time.monotonic()
    fallback_used, cook_error = _start_cook(hou, node)
    entry = {
        "cook_id": cook_id,
        "node_path": node_path,
        "started_at": started_at,
        "started_monotonic": started_monotonic,
        "timeout_seconds": timeout,
        "state": _state_name(_probe_state(hou, node)),
        "status": "started",
        "started_count": 0 if cook_error else 1,
        "fallback_used": fallback_used,
        "terminal": False,
    }
    if cook_error is not None:
        entry["status"] = "error"
        _store_handle(node_path, cook_id, entry)
        payload = _cook_base_payload(entry)
        payload.update(cook_error)
        return _cap(payload)
    _store_handle(node_path, cook_id, entry)
    if is_blocking:
        entry = _poll_until_terminal(hou, node, entry)
    return _cap(_cook_base_payload(entry))


def _poll_until_terminal(hou, node, entry):
    """轮询至 terminal 或超时；超时不自动 cancel，handle 保持 active。"""
    deadline = entry["started_monotonic"] + entry["timeout_seconds"]
    node_path = entry["node_path"]
    while True:
        state_name = _state_name(_probe_state(hou, node))
        entry["state"] = state_name
        if _is_terminal(state_name):
            entry["status"] = state_name or "terminal"
            entry["terminal"] = True
            _close_active(node_path)
            return entry
        now = time.monotonic()
        if now >= deadline:
            entry["status"] = "timed_out"
            # handle 保持 active：不 close、不 cancel。
            return entry
        remaining = deadline - now
        time.sleep(min(_POLL_INTERVAL, remaining) if remaining > 0 else 0)


def pdg_status(hou, node_path, cook_id=None):
    """返回 cook_state、work item 计数、进度与 handle 状态。

    cook_id 给出时校验其属于该节点；未知/过期/属他节点返回结构化错误。
    """
    node, error = _resolve_top(hou, node_path)
    if error is not None:
        return _cap(error)
    state_name = _state_name(_probe_state(hou, node))
    counts, total = _work_item_states(node)
    is_terminal = _is_terminal(state_name)

    handle_summary = None
    if cook_id is not None:
        entry = _COOK_REGISTRY.get(cook_id)
        if entry is None or entry.get("node_path") != node_path:
            return _cap(_error(
                "unknown_cook_id",
                "cook_id is unknown, expired after server restart, or "
                "belongs to another node",
                cook_id=cook_id, node_path=node_path, scope="process"))
        entry["state"] = state_name
        if is_terminal:
            entry["terminal"] = True
            entry.setdefault("status", state_name or "terminal")
            _close_active(node_path)
        handle_summary = _handle_summary(entry)
    else:
        active_id = _NODE_TO_COOK.get(node_path)
        if active_id is not None and active_id in _COOK_REGISTRY:
            entry = _COOK_REGISTRY[active_id]
            entry["state"] = state_name
            if is_terminal:
                entry["terminal"] = True
                entry.setdefault("status", state_name or "terminal")
                _close_active(node_path)
            handle_summary = _handle_summary(entry)

    result = {
        "status": "success",
        "node_path": node_path,
        "cook_state": state_name,
        "is_terminal": is_terminal,
        "work_item_counts": counts,
        "total_work_items": total,
        "handle": handle_summary,
        "scope": "process",
    }
    return _cap(result)


def _work_item_state_name(item):
    state = getattr(item, "state", None)
    if callable(state):
        try:
            state = state()
        except Exception:
            state = None
    return _state_name(state)


def _read_work_items(pdg_node):
    """从 pdg.Node 读取 work item 摘要列表；graph 未生成返回 None。"""
    if pdg_node is None:
        return None
    items_attr = getattr(pdg_node, "workItems", None)
    try:
        raw = items_attr() if callable(items_attr) else items_attr
        sequence = list(raw or ())
    except Exception:
        return None
    if not sequence:
        return None
    result = []
    for item in sequence[:_MAX_WORK_ITEMS]:
        result.append({
            "index": _call_value(item, "index", None),
            "name": _name_of(item),
            "state": _work_item_state_name(item),
        })
    return result


def pdg_workitems(hou, node_path, status_filter=None, max_items=1000):
    """从 getPDGNode() 的已生成 work items 读取有界摘要。

    graph 未生成时返回空列表与明确状态。受 status_filter 与 max_items 限制。
    """
    node, error = _resolve_top(hou, node_path)
    if error is not None:
        return _cap(error)
    safe_max = _bounded_int(max_items, 1000, minimum=1, maximum=_MAX_WORK_ITEMS)
    if safe_max is None:
        return _cap(_error(
            "invalid_max_items", "max_items must be an integer"))
    if status_filter is not None and not isinstance(status_filter, str):
        return _cap(_error(
            "invalid_status_filter",
            "status_filter must be a string or None"))
    pdg_node = _get_pdg_node(node)
    items = _read_work_items(pdg_node)
    graph_generated = items is not None
    if items is None:
        items = []
    if status_filter:
        wanted = status_filter.strip().lower()
        items = [item for item in items
                 if item.get("state", "").lower() == wanted]
    total = len(items)
    page = items[:safe_max]
    result = {
        "status": "success",
        "node_path": node_path,
        "graph_generated": graph_generated,
        "work_items": page,
        "count": len(page),
        "total": total,
        "max_items": safe_max,
        "truncated": total > safe_max,
        "status_filter": status_filter,
        "scope": "process",
    }
    if not graph_generated:
        result["message"] = (
            "PDG graph has not been generated yet; cook the node first")
    return _cap(result)


def pdg_dirty(hou, node_path):
    """dirty work items；默认不删除磁盘输出（remove_outputs=False）。"""
    node, error = _resolve_top(hou, node_path)
    if error is not None:
        return _cap(error)
    dirty = getattr(node, "dirtyWorkItems", None)
    if not callable(dirty):
        return _cap(_error(
            "dirty_unavailable",
            "node does not expose dirtyWorkItems",
            node_path=node_path))
    remove_outputs_arg = None
    try:
        dirty(remove_outputs=False)
        remove_outputs_arg = False
    except TypeError:
        # 兼容签名不接受 remove_outputs 关键字；绝不默认删除输出。
        try:
            dirty()
            remove_outputs_arg = False
        except Exception as exc:
            return _cap(_exception_error(
                hou, exc, "dirty_failed", node_path=node_path,
                remove_outputs=False))
    except Exception as exc:
        return _cap(_exception_error(
            hou, exc, "dirty_failed", node_path=node_path,
            remove_outputs=remove_outputs_arg))
    # dirty 使 active handle 失效（scheduler 运行态被重置）。
    _close_active(node_path)
    return _cap({
        "status": "success",
        "node_path": node_path,
        "remove_outputs": remove_outputs_arg,
        "undoable": False,
        "scope": "process",
    })


def pdg_cancel(hou, node_path, cook_id=None):
    """cancel cook；验证 handle 属于该节点。对已 terminal 的 handle 幂等。"""
    node, error = _resolve_top(hou, node_path)
    if error is not None:
        return _cap(error)
    entry = None
    if cook_id is not None:
        entry = _COOK_REGISTRY.get(cook_id)
        if entry is None or entry.get("node_path") != node_path:
            return _cap(_error(
                "unknown_cook_id",
                "cook_id is unknown, expired after server restart, or "
                "belongs to another node",
                cook_id=cook_id, node_path=node_path, scope="process"))
    else:
        active_id = _NODE_TO_COOK.get(node_path)
        if active_id is not None and active_id in _COOK_REGISTRY:
            entry = _COOK_REGISTRY[active_id]

    already_terminal = entry is not None and entry.get("terminal")
    cancel_error = None
    if not already_terminal:
        cancel = getattr(node, "cancelCook", None)
        if callable(cancel):
            try:
                cancel()
            except Exception as exc:
                # 重复 cancel 可能抛错；保持稳定 cancelled 返回，记录诊断。
                cancel_error = _exception_error(
                    hou, exc, "cancel_failed", node_path=node_path)

    if entry is not None:
        entry["state"] = "canceled"
        entry["status"] = "canceled"
        entry["terminal"] = True
        _close_active(node_path)

    result = {
        "status": "success",
        "node_path": node_path,
        "cancelled": True,
        "undoable": False,
        "scope": "process",
    }
    if entry is not None:
        result["cook_id"] = entry.get("cook_id")
    if cancel_error is not None and not already_terminal:
        # cancel 真正失败（非重复取消）才上报为 error。
        result["status"] = "error"
        result["cancelled"] = False
        result.update(cancel_error)
    return _cap(result)
