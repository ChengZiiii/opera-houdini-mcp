"""_cache_nodes.py — opera-houdini-mcp Houdini 21+ 场景缓存节点工具。

核心约束：
- 4 个公共函数（list_caches / get_cache_status / clear_cache / write_cache）
  都显式接收注入的 ``hou``；模块顶层不导入 hou，零新依赖。
- cache adapter 严格按规范 operator type（含 namespace / version）白名单
  注册：首批仅允许 H21.0.596 / H22 实机 live smoke 通过的 File Cache 系列
  (``filecache`` / ``filecache::2.0`` / ``labs::filecache::1.0`` /
  ``labs::filecache::2.0``)。
- 普通 ``Sop/file`` 仅加载外部文件，不是通用可写缓存节点；**绝不**
  进入白名单。
- 每个 adapter 固定 ``status / clear / write`` 真实 surface：
    * status  ：loadfromdisk parm + cook 错误 + file parm 文件存在 + 节点 errors/warnings。
    * clear   ：把 loadfromdisk 切到 0 并 cook，必要时删磁盘缓存文件。
    * write   ：cook 后调 ``node.geometry().saveToFile(file_path)`` 落盘
      真实文件（与 H21 live 探测一致）。
- clear / write 改运行态 / 磁盘文件，不可由 Houdini undo 恢复；上层分类
  进 ``NO_UNDO_COMMANDS``，**不**进 ``MUTATING_COMMANDS``。
- 所有公共返回（success / warning / error）均经过 ``apply_response_cap``。
"""
import os

from . import _common as cmn


# adapter 白名单：key = node type.name()（含 namespace + version）。
# 任何不在这里的 type 一律 unsupported；普通 Sop/file 永远不进入此表。
_FILECACHE_TYPES = frozenset({
    "filecache",
    "filecache::2.0",
    "labs::filecache::1.0",
    "labs::filecache::2.0",
})


_DEFAULT_LIMIT = 256
_MAX_NODES_BUDGET = 1024
_MAX_STRING_CHARS = 2048


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


def _node_messages(node, method_name):
    getter = getattr(node, method_name, None)
    if not callable(getter):
        return []
    try:
        values = getter() or ()
    except Exception:
        return []
    return [str(value) for value in list(values)[:_MAX_STRING_CHARS]]


def _cook_report(node):
    return {
        "cook_errors": _node_messages(node, "errors"),
        "cook_warnings": _node_messages(node, "warnings"),
    }


def _call_parm(node, name):
    """返回 node.parm(name)；缺失返 None。"""
    getter = getattr(node, "parm", None)
    if not callable(getter):
        return None
    try:
        return getter(name)
    except Exception:
        return None


def _version_key(hou):
    """返回 (major, minor)；仅识别 H21.0 / H22.x。"""
    getter = getattr(hou, "applicationVersion", None)
    if not callable(getter):
        return None
    try:
        version = tuple(getter())
    except Exception:
        return None
    if len(version) < 2:
        return None
    try:
        return int(version[0]), int(version[1])
    except (TypeError, ValueError):
        return None


def _node_type_name(node):
    getter = getattr(node, "type", None)
    if not callable(getter):
        return ""
    try:
        type_obj = getter()
    except Exception:
        return ""
    name_fn = getattr(type_obj, "name", None)
    if not callable(name_fn):
        return ""
    try:
        return str(name_fn())
    except Exception:
        return ""


def _node_path(node):
    getter = getattr(node, "path", None)
    if not callable(getter):
        return ""
    try:
        return str(getter())
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Adapter：File Cache 系列（filecache::2.0 主白名单 + 旧/同名变体）
# 任何不属于 _FILECACHE_TYPES 的 type 都不会被 adapter 接受。
# ---------------------------------------------------------------------------


def _match_filecache(node):
    type_name = _node_type_name(node)
    if type_name in _FILECACHE_TYPES:
        return {"type": type_name}
    return None


def _filecache_status(hou, node):
    file_parm = _call_parm(node, "file")
    output_path = file_parm.eval() if file_parm is not None else ""
    file_exists = bool(output_path) and os.path.isfile(str(output_path))
    load_parm = _call_parm(node, "loadfromdisk")
    loadfromdisk = load_parm.eval() if load_parm is not None else None
    return {
        "adapter": "filecache",
        "type": _node_type_name(node),
        "output_path": str(output_path) if output_path else "",
        "file_exists": file_exists,
        "loadfromdisk": bool(loadfromdisk) if loadfromdisk is not None else None,
    }


def _filecache_clear(hou, node):
    """让缓存节点回到不读磁盘状态。

    H21 实测：把 ``loadfromdisk`` 切到 0 + cook 即可清除运行态缓存。
    若 ``file`` parm 指向磁盘文件，**仅**在调用方显式要求时删除（避免
    误删），因此本函数不删磁盘；调用方可拿到 ``output_path`` 自行决定。
    本操作属 no-undo：使用 ``hou.undos.disabler()`` 阻止 parm 写入生成
    undo 条目，保证 tool 真正"不可 undo"语义。
    """
    load_parm = _call_parm(node, "loadfromdisk")
    if load_parm is None:
        return {"cleared": False,
                "reason": "loadfromdisk parm unavailable"}
    output_path = ""
    file_parm = _call_parm(node, "file")
    if file_parm is not None:
        try:
            output_path = str(file_parm.eval() or "")
        except Exception:
            output_path = ""
    try:
        disabler = getattr(getattr(hou, "undos", None), "disabler", None)
        if callable(disabler):
            with disabler():
                try:
                    load_parm.set(0)
                except Exception as exc:
                    return {"cleared": False, "reason": str(exc)}
        else:
            load_parm.set(0)
    except Exception as exc:
        return {"cleared": False, "reason": str(exc)}
    cook_fn = getattr(node, "cook", None)
    if callable(cook_fn):
        try:
            cook_fn(force=True)
        except Exception:
            pass
    return {
        "cleared": True,
        "output_path": output_path,
    }


def _filecache_write(hou, node):
    """真实落盘：先 cook，再用 node.geometry().saveToFile(file) 写磁盘。

    H21 实测：pressButton('execute') 在 hython 中**不**写文件，必须
    用 ``node.geometry().saveToFile(file)`` 落盘（与 live 探测一致）。
    返回磁盘 side effects + 真实 file 存在校验。
    """
    file_parm = _call_parm(node, "file")
    if file_parm is None:
        return {"written": False, "reason": "file parm unavailable"}
    output_path = str(file_parm.eval() or "")
    if not output_path:
        return {"written": False, "reason": "file parm is empty"}
    cook_fn = getattr(node, "cook", None)
    if callable(cook_fn):
        try:
            cook_fn(force=True)
        except Exception as exc:
            return {"written": False, "reason": "cook failed: " + str(exc)}
    cook_errors = _node_messages(node, "errors")
    if cook_errors:
        return {"written": False, "reason": "node has cook errors",
                "cook_errors": cook_errors, "output_path": output_path}
    geo_fn = getattr(node, "geometry", None)
    if not callable(geo_fn):
        return {"written": False, "reason": "geometry() unavailable"}
    try:
        geometry = geo_fn()
    except Exception as exc:
        return {"written": False, "reason": "geometry() failed: " + str(exc)}
    if geometry is None:
        return {"written": False, "reason": "node has no geometry",
                "output_path": output_path}
    # ensure parent dir
    try:
        parent_dir = os.path.dirname(output_path)
        if parent_dir and not os.path.isdir(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
    except OSError as exc:
        return {"written": False, "reason": "mkdir failed: " + str(exc),
                "output_path": output_path}
    existed_before = os.path.isfile(output_path)
    try:
        geometry.saveToFile(output_path)
    except Exception as exc:
        return {"written": False,
                "reason": "saveToFile failed: " + str(exc),
                "output_path": output_path}
    exists_after = os.path.isfile(output_path)
    try:
        size_bytes = os.path.getsize(output_path) if exists_after else 0
    except OSError:
        size_bytes = 0
    return {
        "written": exists_after,
        "output_path": output_path,
        "file_exists": exists_after,
        "size_bytes": size_bytes,
        "existed_before": existed_before,
    }


ADAPTERS = (
    {
        "name": "filecache",
        "match": _match_filecache,
        "status": _filecache_status,
        "clear": _filecache_clear,
        "write": _filecache_write,
    },
)


def _adapter_for(hou, node):
    for adapter in ADAPTERS:
        info = adapter["match"](node)
        if info is not None:
            return adapter, info
    return None, None


def _resolve_node(hou, node_path):
    if not isinstance(node_path, str) or not node_path.strip():
        return None, _error("invalid_node_path",
                             "node_path must be a non-empty string")
    try:
        node = hou.node(node_path)
    except Exception as exc:
        return None, _error("node_resolution_failed", exc,
                             node_path=node_path)
    if node is None:
        return None, _error("node_not_found",
                             "Node not found: " + node_path,
                             node_path=node_path)
    return node, None


def _resolve_parent(hou, parent_path):
    if not isinstance(parent_path, str) or not parent_path.strip():
        return None, _error("invalid_parent_path",
                             "parent_path must be a non-empty string")
    try:
        node = hou.node(parent_path)
    except Exception as exc:
        return None, _error("parent_resolution_failed", exc,
                             parent_path=parent_path)
    if node is None:
        return None, _error("parent_not_found",
                             "Parent node not found: " + parent_path,
                             parent_path=parent_path)
    return node, None


def list_caches(hou, parent_path="/", max_nodes=_DEFAULT_LIMIT):
    """枚举 parent_path 子树内白名单 cache adapter 节点。

    按有界 BFS 走 children，节点数受 ``max_nodes`` 限制；不在
    ``ADAPTERS`` 白名单的节点（普通 ``Sop/file`` 等）不出现在结果里。
    """
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 0:
        return _cap(_error("invalid_max_nodes",
                            "max_nodes must be a non-negative integer",
                            field="max_nodes", value=type(max_nodes).__name__))
    safe_max = min(max_nodes, _MAX_NODES_BUDGET)
    parent, error = _resolve_parent(hou, parent_path)
    if error is not None:
        return _cap(error)
    matches = []
    visited = set()
    budget_left = safe_max
    try:
        root_path = parent.path()
    except Exception:
        root_path = parent_path
    visited.add(root_path)
    queue = [(parent, 0)]
    while queue and budget_left > 0:
        current, _depth = queue.pop(0)
        try:
            children = current.children() or []
        except Exception:
            children = []
        for child in children:
            try:
                child_path = child.path()
            except Exception:
                child_path = ""
            if child_path in visited:
                continue
            visited.add(child_path)
            adapter, info = _adapter_for(hou, child)
            if adapter is not None:
                status = {}
                try:
                    status = adapter["status"](hou, child)
                except Exception as exc:
                    status = {"error": str(exc)}
                matches.append({
                    "path": child_path,
                    "type": _node_type_name(child),
                    "adapter": adapter["name"],
                    "status": status,
                })
                budget_left -= 1
                if budget_left <= 0:
                    break
            try:
                queue.append((child, 0))
            except Exception:
                pass
    return _cap({
        "status": "success",
        "parent_path": parent_path,
        "max_nodes": safe_max,
        "caches": matches,
        "count": len(matches),
        "truncated": budget_left <= 0,
        "houdini_version": _version_key(hou),
    })


def get_cache_status(hou, node_path):
    """读取 node_path 白名单 cache adapter 的 status 字段。"""
    node, error = _resolve_node(hou, node_path)
    if error is not None:
        return _cap(error)
    adapter, info = _adapter_for(hou, node)
    if adapter is None:
        return _cap(_error(
            "unsupported_cache_type",
            "Node type is not in the cache adapter whitelist: "
            + _node_type_name(node),
            node_path=node_path, node_type=_node_type_name(node)))
    try:
        status = adapter["status"](hou, node)
    except Exception as exc:
        return _cap(_error("cache_status_failed", exc,
                            node_path=node_path))
    result = {
        "status": "success",
        "node_path": _node_path(node),
        "node_type": _node_type_name(node),
        "adapter": adapter["name"],
        "info": info,
        "cache_status": status,
        "houdini_version": _version_key(hou),
    }
    result.update(_cook_report(node))
    return _cap(result)


def clear_cache(hou, node_path, remove_disk_file=False):
    """清运行态 cache（NO_UNDO）；可选同步删磁盘文件。

    删除磁盘文件属于外部 FS mutation，不能由 HIP undo 恢复，本 change 文档
    与响应中明确披露；默认 ``remove_disk_file=False`` 仅清运行态。
    """
    node, error = _resolve_node(hou, node_path)
    if error is not None:
        return _cap(error)
    adapter, info = _adapter_for(hou, node)
    if adapter is None:
        return _cap(_error(
            "unsupported_cache_type",
            "Node type is not in the cache adapter whitelist: "
            + _node_type_name(node),
            node_path=node_path, node_type=_node_type_name(node)))
    try:
        clear_result = adapter["clear"](hou, node)
    except Exception as exc:
        return _cap(_error("cache_clear_failed", exc, node_path=node_path))
    disk_removed = False
    disk_error = None
    if remove_disk_file:
        try:
            status = adapter["status"](hou, node)
            output_path = status.get("output_path", "")
        except Exception:
            output_path = clear_result.get("output_path", "")
        if output_path and os.path.isfile(output_path):
            try:
                os.remove(output_path)
                disk_removed = True
            except OSError as exc:
                disk_error = str(exc)
    result = {
        "status": "success",
        "node_path": _node_path(node),
        "node_type": _node_type_name(node),
        "adapter": adapter["name"],
        "info": info,
        "cleared": clear_result,
        "disk_removed": disk_removed,
        "remove_disk_file": bool(remove_disk_file),
    }
    if disk_error is not None:
        result["disk_error"] = disk_error
    result.update(_cook_report(node))
    return _cap(result)


def write_cache(hou, node_path):
    """真实落盘 cache（NO_UNDO）；调用 adapter.write。

    adapter.write 调 ``node.geometry().saveToFile(file)`` 写磁盘；
    返回 adapter、目标路径、文件操作、cook errors 与最终状态。删除 /
    覆盖磁盘不可由 Houdini undo 恢复，工具 MUST NOT 包在 undo group 中
    或声称可撤销。
    """
    node, error = _resolve_node(hou, node_path)
    if error is not None:
        return _cap(error)
    adapter, info = _adapter_for(hou, node)
    if adapter is None:
        return _cap(_error(
            "unsupported_cache_type",
            "Node type is not in the cache adapter whitelist: "
            + _node_type_name(node),
            node_path=node_path, node_type=_node_type_name(node)))
    try:
        write_result = adapter["write"](hou, node)
    except Exception as exc:
        return _cap(_error("cache_write_failed", exc, node_path=node_path))
    result = {
        "status": "success",
        "node_path": _node_path(node),
        "node_type": _node_type_name(node),
        "adapter": adapter["name"],
        "info": info,
        "written": write_result,
        "disk_side_effect": True,
    }
    result.update(_cook_report(node))
    return _cap(result)
