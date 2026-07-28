"""_dops.py — 有界 DOP 查询、时间线步进与模拟重置工具。

核心约束：
- 8 个公共函数都显式接收注入的 ``hou``；模块顶层不导入 hou。
- 查询只通过 DopSimulation 的 objects/findObject/relationships/time/
  timestep/memoryUsage 入口；DOP data/record 只做有界摘要，volume/VDB
  不回传体素。
- step/reset 通过全局时间线 + ``node.cook(force=True)`` 驱动，属于
  no-undo 运行态操作；会生成、替换、清空或重建 DOP cache。
- 可选 ``DopSimulation.setTime(..., force_reset_sim=True)`` 同时受真实
  签名探针与逐版本 live 结果门禁；mock 上出现同名方法不能建立门禁。
- 精确区分注入的 ``hou.PermissionError`` 与 Python PermissionError。
- 所有公共返回（success/warning/error）均经过 ``apply_response_cap``。
"""
import inspect
import math

from . import _common as cmn


_MAX_QUERY_ITEMS = 1000
_MAX_FIELD_ITEMS = 64
_MAX_STRING_CHARS = 2048

# 只有真实 live smoke 明确允许的版本才可改为 True。H21.0.596 实机的
# owned simulation 路径会抛 hou.PermissionError，因此保持 False；H22 尚未
# 安装、未实测，同样 fail closed。mock capability 不读取、不修改此表。
_FORCE_RESET_SIM_LIVE_RESULTS = {
    (21, 0): False,
    (22, 0): False,
}


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


def _finite_number(value):
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _bounded_int(value, default, minimum=0, maximum=_MAX_QUERY_ITEMS):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(minimum, min(maximum, value))


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


def _bounded_value(value, depth=3, max_items=_MAX_FIELD_ITEMS):
    """把 DOP record field 转为有界 JSON-safe 值，不展开大型容器。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return value[:_MAX_STRING_CHARS]
    if depth <= 0:
        return "unavailable"
    if isinstance(value, dict):
        result = {}
        keys = sorted(value, key=lambda item: str(item))[:max_items]
        for key in keys:
            result[str(key)] = _bounded_value(
                value[key], depth=depth - 1, max_items=max_items)
        if len(value) > max_items:
            result["_truncated_count"] = len(value) - max_items
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth=depth - 1, max_items=max_items)
                for item in value[:max_items]]
    try:
        sequence = list(value)
    except Exception:
        text = str(value)
        return text[:_MAX_STRING_CHARS]
    return [_bounded_value(item, depth=depth - 1, max_items=max_items)
            for item in sequence[:max_items]]


def _resolve_dop(hou, dop_path):
    if not isinstance(dop_path, str) or not dop_path.strip():
        return None, None, _error(
            "invalid_dop_path", "dop_path must be a non-empty string")
    try:
        node = hou.node(dop_path)
    except Exception as error:
        return None, None, _error(
            "dop_node_resolution_failed", error, dop_path=dop_path)
    if node is None:
        return None, None, _error(
            "dop_node_not_found", "DOP network not found: " + dop_path,
            dop_path=dop_path)

    child_category = getattr(node, "childTypeCategory", None)
    if callable(child_category):
        try:
            category = child_category()
            category_name = _name_of(category).lower()
        except Exception:
            category_name = ""
        if category_name and category_name != "dop":
            return None, None, _error(
                "not_dop_network",
                "node child category is not Dop: " + category_name,
                dop_path=dop_path)

    simulation_getter = getattr(node, "simulation", None)
    if not callable(simulation_getter):
        return None, None, _error(
            "not_dop_network", "node does not expose simulation()",
            dop_path=dop_path)
    try:
        simulation = simulation_getter()
    except Exception as error:
        return None, None, _error(
            "simulation_unavailable", error, dop_path=dop_path)
    if simulation is None:
        return None, None, _error(
            "simulation_unavailable", "DOP simulation is unavailable",
            dop_path=dop_path)
    return node, simulation, None


def _simulation_time(simulation):
    try:
        return float(simulation.time())
    except Exception:
        return None


def _simulation_memory(simulation):
    try:
        return simulation.memoryUsage()
    except Exception:
        return None


def _node_messages(node, method_name):
    getter = getattr(node, method_name, None)
    if not callable(getter):
        return []
    try:
        values = getter() or ()
    except Exception:
        return []
    return [str(value) for value in list(values)[:_MAX_FIELD_ITEMS]]


def _data_items(dop_object):
    # H21 hou.DopObject / hou.DopData 公开 ``subData()``；早期 mock 与
    # 兼容对象可能只提供 ``data()``。优先真实 H21 surface。
    getter = getattr(dop_object, "subData", None)
    if not callable(getter):
        getter = getattr(dop_object, "data", None)
    if not callable(getter):
        return []
    try:
        data = getter()
    except Exception:
        return []
    if isinstance(data, dict):
        return sorted([(str(name), item) for name, item in data.items()],
                      key=lambda pair: pair[0])
    try:
        values = list(data)
    except Exception:
        return []
    return sorted([(_name_of(item), item) for item in values],
                  key=lambda pair: pair[0])


def _record_types(data):
    getter = getattr(data, "recordTypes", None)
    if not callable(getter):
        return []
    try:
        return [str(item) for item in list(getter() or ())[:_MAX_FIELD_ITEMS]]
    except Exception:
        return []


def _data_summary(name, data):
    data_type = _call_value(data, "dataType", "")
    return {
        "name": str(name),
        "data_type": str(data_type or ""),
        "record_types": _record_types(data),
    }


def _object_summary(dop_object, max_data=_MAX_FIELD_ITEMS):
    objid = _call_value(dop_object, "objid", None)
    all_data_items = _data_items(dop_object)
    data_items = all_data_items[:max_data]
    return {
        "name": _name_of(dop_object),
        "object_id": objid,
        "record_types": _record_types(dop_object),
        "data": [_data_summary(name, data) for name, data in data_items],
        "data_count": len(all_data_items),
    }


def _find_data(dop_object, data_name):
    # ``.`` 显式表示 DopObject 自身的 record layer；H21 DopObject 是
    # hou.DopData 子类，Options/Basic records 直接挂在对象上。
    if data_name in (".", "/", _name_of(dop_object)):
        return dop_object
    finder = getattr(dop_object, "findSubData", None)
    if callable(finder):
        try:
            found = finder(data_name)
        except Exception:
            found = None
        if found is not None:
            return found
    for name, data in _data_items(dop_object):
        if name == data_name:
            return data
    return None


def _records(data, record_type):
    getter = getattr(data, "records", None)
    if not callable(getter):
        return []
    try:
        return list(getter(record_type) or ())[:_MAX_FIELD_ITEMS]
    except Exception:
        return []


def _field_names(record):
    getter = getattr(record, "fieldNames", None)
    if not callable(getter):
        return []
    try:
        return [str(name) for name in list(getter() or ())[:_MAX_FIELD_ITEMS]]
    except Exception:
        return []


def _record_field(record, name, default="unavailable"):
    getter = getattr(record, "field", None)
    if not callable(getter):
        return default
    try:
        return getter(name)
    except Exception:
        return default


def _first_field(record, names, default="unavailable"):
    available = set(_field_names(record))
    for name in names:
        if name not in available:
            continue
        value = _record_field(record, name, default)
        if value != default:
            return _bounded_value(value)
    return default


def _field_statistics(record):
    return {
        "resolution": _first_field(
            record, ("resolution", "res", "voxelresolution")),
        "bbox": _first_field(
            record, ("bbox", "bounds", "boundingbox")),
        "minimum": _first_field(
            record, ("min", "minimum", "minvalue")),
        "maximum": _first_field(
            record, ("max", "maximum", "maxvalue")),
        "average": _first_field(
            record, ("average", "avg", "mean")),
    }


def _is_volume_data(data):
    data_type = str(_call_value(data, "dataType", "") or "").lower()
    return any(token in data_type for token in (
        "field", "volume", "voxel", "vdb"))


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


def _version_key(hou):
    getter = getattr(hou, "applicationVersion", None)
    if not callable(getter):
        return None
    try:
        version = tuple(getter())
    except Exception:
        return None
    if len(version) < 2:
        return None
    major = int(version[0])
    minor = int(version[1])
    if major == 21 and minor == 0:
        return (21, 0)
    if major == 22:
        return (22, 0)
    return None


def _probe_force_reset_signature(hou, simulation):
    """确认真实方法签名中存在 force_reset_sim；属性存在本身不够。"""
    if _version_key(hou) not in ((21, 0), (22, 0)):
        return False
    method = getattr(simulation, "setTime", None)
    if not callable(method):
        return False
    try:
        signature = inspect.signature(method)
        if "force_reset_sim" in signature.parameters:
            return True
    except (TypeError, ValueError):
        pass
    doc = getattr(method, "__doc__", "") or ""
    return "force_reset_sim" in doc


def _force_reset_live_allowed(hou):
    key = _version_key(hou)
    return bool(_FORCE_RESET_SIM_LIVE_RESULTS.get(key, False))


def _reset_payload(dop_path, old_frame, new_frame, old_time, new_time,
                   old_memory, new_memory, force_reset_attempted=False,
                   force_reset_applied=False):
    return {
        "dop_path": dop_path,
        "old_frame": old_frame,
        "new_frame": new_frame,
        "old_simulation_time": old_time,
        "new_simulation_time": new_time,
        "old_memory_usage": old_memory,
        "new_memory_usage": new_memory,
        "force_reset_attempted": bool(force_reset_attempted),
        "force_reset_applied": bool(force_reset_applied),
        "cook_errors": [],
        "undoable": False,
        "side_effects": {
            "timeline_changed": True,
            "dependency_graph_cooked": True,
            "dop_cache_cleared_or_rebuilt": True,
            "undo_restores_cache": False,
        },
    }


def get_simulation_info(hou, dop_path):
    """返回 frame/time/timestep/object_count 的有界模拟元数据。"""
    node, simulation, error = _resolve_dop(hou, dop_path)
    if error is not None:
        return _cap(error)
    try:
        objects = list(simulation.objects() or ())
        result = {
            "status": "success",
            "dop_path": dop_path,
            "frame": float(hou.frame()),
            "time": float(simulation.time()),
            "timestep": float(simulation.timestep()),
            "object_count": len(objects),
        }
    except Exception as exc:
        result = _exception_error(
            hou, exc, "simulation_query_failed", dop_path=dop_path)
    return _cap(result)


def list_dop_objects(hou, dop_path, offset=0, limit=100):
    """分页列出 simulation.objects()，每项仅含对象和 data 摘要。"""
    node, simulation, error = _resolve_dop(hou, dop_path)
    if error is not None:
        return _cap(error)
    safe_offset = _bounded_int(offset, 0)
    safe_limit = _bounded_int(limit, 100, minimum=1)
    if safe_offset is None or safe_limit is None:
        return _cap(_error(
            "invalid_pagination", "offset/limit must be integers"))
    try:
        objects = list(simulation.objects() or ())
        page = objects[safe_offset:safe_offset + safe_limit]
        result = {
            "status": "success",
            "dop_path": dop_path,
            "objects": [_object_summary(item) for item in page],
            "offset": safe_offset,
            "limit": safe_limit,
            "count": len(page),
            "total": len(objects),
            "next_offset": (safe_offset + len(page)
                            if safe_offset + len(page) < len(objects)
                            else None),
        }
    except Exception as exc:
        result = _exception_error(
            hou, exc, "objects_query_failed", dop_path=dop_path)
    return _cap(result)


def get_dop_object(hou, dop_path, object_name, max_data=64):
    """用 simulation.findObject() 返回单个 DOP object 的有界摘要。"""
    node, simulation, error = _resolve_dop(hou, dop_path)
    if error is not None:
        return _cap(error)
    if not isinstance(object_name, str) or not object_name:
        return _cap(_error(
            "invalid_object_name", "object_name must be a non-empty string"))
    safe_max_data = _bounded_int(max_data, 64, maximum=_MAX_FIELD_ITEMS)
    if safe_max_data is None:
        return _cap(_error(
            "invalid_max_data", "max_data must be an integer"))
    try:
        dop_object = simulation.findObject(object_name)
        if dop_object is None:
            result = _error(
                "dop_object_not_found",
                "DOP object not found: " + object_name,
                dop_path=dop_path, object_name=object_name)
        else:
            result = {
                "status": "success",
                "dop_path": dop_path,
                "object": _object_summary(dop_object, safe_max_data),
            }
    except Exception as exc:
        result = _exception_error(
            hou, exc, "object_query_failed", dop_path=dop_path,
            object_name=object_name)
    return _cap(result)


def get_dop_field(hou, dop_path, object_name, data_name, field_name,
                  record_type="Options", record_index=0):
    """从 DOP data/record 读取一个字段；volume/VDB 只返统计。"""
    node, simulation, error = _resolve_dop(hou, dop_path)
    if error is not None:
        return _cap(error)
    for value, code, label in (
            (object_name, "invalid_object_name", "object_name"),
            (data_name, "invalid_data_name", "data_name"),
            (field_name, "invalid_field_name", "field_name"),
            (record_type, "invalid_record_type", "record_type")):
        if not isinstance(value, str) or not value:
            return _cap(_error(code, label + " must be a non-empty string"))
    safe_index = _bounded_int(record_index, 0, maximum=_MAX_FIELD_ITEMS)
    if safe_index is None:
        return _cap(_error(
            "invalid_record_index", "record_index must be an integer"))
    try:
        dop_object = simulation.findObject(object_name)
        if dop_object is None:
            result = _error(
                "dop_object_not_found", "DOP object not found: " + object_name)
        else:
            data = _find_data(dop_object, data_name)
            if data is None:
                result = _error(
                    "dop_data_not_found", "DOP data not found: " + data_name)
            else:
                records = _records(data, record_type)
                if safe_index >= len(records):
                    result = _error(
                        "dop_record_not_found",
                        "DOP record not found: {0}[{1}]".format(
                            record_type, safe_index))
                else:
                    record = records[safe_index]
                    names = _field_names(record)
                    if field_name not in names:
                        result = _error(
                            "dop_field_not_found",
                            "DOP field not found: " + field_name,
                            available_fields=names)
                    else:
                        raw = _record_field(record, field_name)
                        volume = _is_volume_data(data)
                        result = {
                            "status": "success",
                            "dop_path": dop_path,
                            "object_name": object_name,
                            "data_name": data_name,
                            "data_type": str(
                                _call_value(data, "dataType", "") or ""),
                            "record_type": record_type,
                            "record_index": safe_index,
                            "field_name": field_name,
                            "statistics": _field_statistics(record),
                        }
                        if volume:
                            result["value"] = "unavailable"
                            result["raw_voxels_returned"] = False
                        elif isinstance(raw, (list, tuple)) \
                                and len(raw) > _MAX_FIELD_ITEMS:
                            result["value"] = {
                                "kind": "sequence",
                                "count": len(raw),
                                "truncated": True,
                            }
                            result["sample"] = _bounded_value(raw)
                        else:
                            result["value"] = _bounded_value(raw)
    except Exception as exc:
        result = _exception_error(
            hou, exc, "field_query_failed", dop_path=dop_path,
            object_name=object_name, data_name=data_name,
            field_name=field_name)
    return _cap(result)


def _relationship_record_names(relationship, record_type, max_objects):
    """读取 H21 DopRelationship ObjInGroup/ObjInAffectors records。"""
    names = []
    for record in _records(relationship, record_type):
        value = _record_field(record, "objname", "")
        if value in (None, "", "unavailable"):
            value = _record_field(record, "name", "")
        if value not in (None, "", "unavailable"):
            names.append(str(value))
        if len(names) >= max_objects:
            break
    return names


def get_dop_relationships(hou, dop_path, offset=0, limit=100,
                          max_objects=100):
    """分页返回 simulation.relationships() 与每个关系的有界对象名。"""
    node, simulation, error = _resolve_dop(hou, dop_path)
    if error is not None:
        return _cap(error)
    safe_offset = _bounded_int(offset, 0)
    safe_limit = _bounded_int(limit, 100, minimum=1)
    safe_max_objects = _bounded_int(
        max_objects, 100, maximum=_MAX_QUERY_ITEMS)
    if None in (safe_offset, safe_limit, safe_max_objects):
        return _cap(_error(
            "invalid_relationship_limit",
            "offset/limit/max_objects must be integers"))
    try:
        relationships = list(simulation.relationships() or ())
        page = relationships[safe_offset:safe_offset + safe_limit]
        entries = []
        for relationship in page:
            object_getter = getattr(relationship, "objects", None)
            try:
                objects = list(object_getter() or ()) \
                    if callable(object_getter) else []
            except Exception:
                objects = []
            direct_names = [_name_of(item)
                            for item in objects[:safe_max_objects]]
            group_names = _relationship_record_names(
                relationship, "ObjInGroup", safe_max_objects)
            affector_names = _relationship_record_names(
                relationship, "ObjInAffectors", safe_max_objects)
            combined = []
            for name in direct_names + group_names + affector_names:
                if name and name not in combined:
                    combined.append(name)
                if len(combined) >= safe_max_objects:
                    break
            member_count = max(
                len(objects), len(group_names) + len(affector_names))
            entries.append({
                "name": _name_of(relationship),
                "objects": combined,
                "group_objects": group_names,
                "affector_objects": affector_names,
                "object_count": member_count,
                "objects_truncated": member_count > safe_max_objects,
            })
        result = {
            "status": "success",
            "dop_path": dop_path,
            "relationships": entries,
            "offset": safe_offset,
            "limit": safe_limit,
            "count": len(entries),
            "total": len(relationships),
            "next_offset": (safe_offset + len(entries)
                            if safe_offset + len(entries) < len(relationships)
                            else None),
        }
    except Exception as exc:
        result = _exception_error(
            hou, exc, "relationships_query_failed", dop_path=dop_path)
    return _cap(result)


def step_simulation(hou, dop_path, frames=1):
    """推进全局时间线后强制 cook；不恢复旧帧且不进入 undo。"""
    if not _finite_number(frames) or float(frames) <= 0:
        return _cap(_error(
            "invalid_frames", "frames must be a finite number greater than 0"))
    node, simulation, error = _resolve_dop(hou, dop_path)
    if error is not None:
        return _cap(error)
    try:
        old_frame = float(hou.frame())
        old_time = float(simulation.time())
        target_frame = old_frame + float(frames)
        target_time = hou.frameToTime(target_frame)
        hou.setTime(target_time)
    except Exception as exc:
        return _cap(_exception_error(
            hou, exc, "timeline_set_failed", dop_path=dop_path))
    try:
        node.cook(force=True)
    except Exception as exc:
        return _cap(_exception_error(
            hou, exc, "cook_failed", dop_path=dop_path,
            old_frame=old_frame, target_frame=target_frame,
            timeline_changed=True, undoable=False))
    cook_errors = _node_messages(node, "errors")
    cook_warnings = _node_messages(node, "warnings")
    refreshed = _resolve_dop(hou, dop_path)[1]
    result = {
        "status": "success" if not cook_errors else "error",
        "dop_path": dop_path,
        "old_frame": old_frame,
        "new_frame": float(hou.frame()),
        "old_simulation_time": old_time,
        "new_simulation_time": _simulation_time(refreshed),
        "cook_errors": cook_errors,
        "cook_warnings": cook_warnings,
        "undoable": False,
        "side_effects": {
            "timeline_changed": True,
            "dependency_graph_cooked": True,
            "dop_cache_generated_or_replaced": True,
            "undo_restores_cache": False,
        },
    }
    if cook_errors:
        result["error"] = {
            "code": "cook_failed",
            "message": "DOP node reported cook errors",
        }
    return _cap(result)


def reset_simulation(hou, dop_path, reset_frame=None):
    """时间线优先重置；可选 force reset 受签名与 live 双门禁。"""
    node, simulation, error = _resolve_dop(hou, dop_path)
    if error is not None:
        return _cap(error)
    if reset_frame is None:
        try:
            reset_frame = hou.playbar.playbackRange()[0]
        except Exception as exc:
            return _cap(_exception_error(
                hou, exc, "reset_frame_unavailable", dop_path=dop_path))
    if not _finite_number(reset_frame):
        return _cap(_error(
            "invalid_reset_frame", "reset_frame must be a finite number"))
    old_frame = None
    old_time = _simulation_time(simulation)
    old_memory = _simulation_memory(simulation)
    try:
        old_frame = float(hou.frame())
        target_time = hou.frameToTime(float(reset_frame))
        hou.setTime(target_time)
    except Exception as exc:
        return _cap(_exception_error(
            hou, exc, "timeline_set_failed", dop_path=dop_path,
            old_frame=old_frame, old_simulation_time=old_time,
            old_memory_usage=old_memory, undoable=False))
    try:
        node.cook(force=True)
    except Exception as exc:
        return _cap(_exception_error(
            hou, exc, "cook_failed", dop_path=dop_path,
            old_frame=old_frame, target_frame=float(reset_frame),
            timeline_changed=True, undoable=False))
    cook_errors = _node_messages(node, "errors")
    refreshed = _resolve_dop(hou, dop_path)[1]
    base = _reset_payload(
        dop_path, old_frame, float(hou.frame()), old_time,
        _simulation_time(refreshed), old_memory,
        _simulation_memory(refreshed))
    base["cook_errors"] = cook_errors
    if cook_errors:
        base.update(_error(
            "cook_failed", "DOP node reported cook errors"))
        return _cap(base)

    if not _probe_force_reset_signature(hou, refreshed):
        base.update(_warning(
            "force_reset_signature_unavailable",
            "force_reset_sim signature was not confirmed; timeline reset completed"))
        return _cap(base)
    if not _force_reset_live_allowed(hou):
        base.update(_warning(
            "force_reset_live_gate_blocked",
            "force_reset_sim is blocked until this Houdini version passes live smoke"))
        return _cap(base)

    base["force_reset_attempted"] = True
    try:
        refreshed.setTime(target_time, force_reset_sim=True)
    except Exception as exc:
        if _hom_permission_error(hou, exc):
            base.update(_warning(
                "owned_simulation_permission_denied", exc))
            base["new_simulation_time"] = _simulation_time(refreshed)
            base["new_memory_usage"] = _simulation_memory(refreshed)
            return _cap(base)
        base.update(_exception_error(
            hou, exc, "force_reset_failed"))
        base["new_simulation_time"] = _simulation_time(refreshed)
        base["new_memory_usage"] = _simulation_memory(refreshed)
        return _cap(base)
    try:
        node.cook(force=True)
    except Exception as exc:
        base.update(_exception_error(
            hou, exc, "cook_failed_after_force_reset"))
        return _cap(base)
    final_simulation = _resolve_dop(hou, dop_path)[1]
    base["status"] = "success"
    base["force_reset_applied"] = True
    base["new_frame"] = float(hou.frame())
    base["new_simulation_time"] = _simulation_time(final_simulation)
    base["new_memory_usage"] = _simulation_memory(final_simulation)
    base["cook_errors"] = _node_messages(node, "errors")
    if base["cook_errors"]:
        base.update(_error(
            "cook_failed_after_force_reset",
            "DOP node reported cook errors after force reset"))
    return _cap(base)


def get_sim_memory_usage(hou, dop_path):
    """通过 DopSimulation.memoryUsage() 返回内存值并标明 bytes。"""
    node, simulation, error = _resolve_dop(hou, dop_path)
    if error is not None:
        return _cap(error)
    try:
        result = {
            "status": "success",
            "dop_path": dop_path,
            "memory_usage": simulation.memoryUsage(),
            "unit": "bytes",
        }
    except Exception as exc:
        result = _exception_error(
            hou, exc, "memory_usage_query_failed", dop_path=dop_path)
    return _cap(result)
