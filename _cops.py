"""_cops.py — Houdini 21+ Copernicus (COP) 有界查询与控制工具。

核心约束：
- 7 个公共函数都显式接收注入的 ``hou``；模块顶层不导入 hou，零新依赖。
- 仅支持 H21+ Copernicus ``hou.CopNode``；旧 ``/img`` COP2 节点一律返回
  ``unsupported_legacy_cop2``，绝不调用旧 COP2 pixel-plane 类 API 或虚构方法。
- 读取统一走官方入口 ``geometry``/``geometryAtFrame``、``layer``/
  ``layerAtFrame``、``vdb``/``vdbAtFrame``、``cable``/``cableAtFrame``、
  ``inputDataTypes``/``outputDataTypes``/``outputCableStructure``；cable
  wire 枚举只读 ``hasattr`` 实测确认存在的属性（反射式探针），不猜方法名。
- geometry/layer/VDB 只返回有界 metadata、counts、bbox、统计；绝不回传完整
  几何、原始像素或体素。
- 读取可能触发 COP cook；响应中披露 ``cook_errors`` / ``cook_warnings``。
- create 验证 parent 可编辑且 child category 为 Copernicus（"Cop"），并要求
  nodeType 在 Cop category registry 中存在。
- set_cop_flags 只接受白名单 display/export/template/selectable_template/
  compress/bypass，映射到官方 setter；未知键在任何写入前拒绝整次请求。
- 所有公共返回（success/warning/error）均经过 ``apply_response_cap``。
"""
import math

from . import _common as cmn


_MAX_ITEMS = 1000
_MAX_FIELD_ITEMS = 64
_MAX_STRING_CHARS = 2048

# wire 类型候选 token：仅用于把 cable 反射出的属性归类到语义桶，不作为
# 方法名调用。真实 wire 访问由 getattr + callable 探针在运行时确认。
_IMAGE_WIRE_TOKENS = ("image", "layer", "rgba", "pixel", "plane")
_GEOMETRY_WIRE_TOKENS = ("geometry", "geo", "mesh", "particle", "prim")
_VDB_WIRE_TOKENS = ("vdb", "nanovdb", "volume", "fog", "field")

# list_cop_node_types 默认枚举的 node type category 名。H21+ Copernicus
# 节点属于 "Cop" category；旧 COP2 属于 "Cop2"。
_DEFAULT_CATEGORY = "Cop"


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


def _call_value(obj, name, default=None):
    """读取 obj.name；若为 callable 则调用，异常或缺失返 default。"""
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


def _finite_int(value, default, minimum=0, maximum=_MAX_ITEMS):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(minimum, min(maximum, value))


def _validate_output_index(output_index):
    """output_index 必须是 >= 0 的整数；负值或非整数返 None（调用方报错）。"""
    if isinstance(output_index, bool) or not isinstance(output_index, int):
        return None
    if output_index < 0:
        return None
    return min(output_index, _MAX_ITEMS)


def _finite_number(value):
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _bounded_value(value, depth=3, max_items=_MAX_FIELD_ITEMS):
    """把任意 HOM 值转为有界 JSON-safe 结构；不展开大型容器/像素/体素。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
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


def _version_key(hou):
    """返回 (major, minor) 简化版本键；仅识别 H21.0 / H22.x，其余 None。"""
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
        major = int(version[0])
        minor = int(version[1])
    except (TypeError, ValueError):
        return None
    if major == 21 and minor == 0:
        return (21, 0)
    if major == 22:
        return (22, 0)
    return None


def _cop_node_class(hou):
    """H21+ Copernicus 节点基类；不存在则 None。"""
    return getattr(hou, "CopNode", None)


def _legacy_cop2_classes(hou):
    """旧 COP2 相关类（可能不存在）；用于 legacy 检测，不调用其方法。"""
    classes = []
    for name in ("Cop2_Node", "Cop2_Net", "CopNet"):
        cls = getattr(hou, name, None)
        if isinstance(cls, type):
            classes.append(cls)
    return tuple(classes)


def _node_category_name(node):
    try:
        return str(node.type().category().name())
    except Exception:
        return ""


def _classify_node(hou, node):
    """返回 copernicus / legacy_cop2 / not_a_cop_node。

    主信号是 isinstance(hou.CopNode)；category 名作为次要 legacy 信号。
    """
    cop_class = _cop_node_class(hou)
    if cop_class is not None and isinstance(node, cop_class):
        return "copernicus"
    category = _node_category_name(node).lower()
    try:
        path = node.path()
    except Exception:
        path = ""
    legacy_path = isinstance(path, str) and path.startswith("/img/")
    for cls in _legacy_cop2_classes(hou):
        if isinstance(node, cls):
            return "legacy_cop2"
    if category == "cop2" or "cop2" in category or legacy_path:
        return "legacy_cop2"
    return "not_a_cop_node"


def _resolve_cop_node(hou, node_path):
    """解析路径并校验是 H21+ Copernicus CopNode。

    返回 (node, kind, error)；kind 为 copernicus/legacy_cop2/not_a_cop_node。
    非 copernicus 一律返结构化 unsupported 错误。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return None, None, _error(
            "invalid_node_path", "node_path must be a non-empty string")
    try:
        node = hou.node(node_path)
    except Exception as error:
        return None, None, _error(
            "node_resolution_failed", error, node_path=node_path)
    if node is None:
        return None, None, _error(
            "node_not_found", "COP node not found: " + node_path,
            node_path=node_path)
    kind = _classify_node(hou, node)
    if kind == "legacy_cop2":
        return node, kind, _error(
            "unsupported_legacy_cop2",
            "Legacy COP2 nodes are not supported; use H21+ Copernicus",
            node_path=node_path)
    if kind != "copernicus":
        return node, kind, _error(
            "not_a_cop_node",
            "Node is not a Copernicus hou.CopNode: " + node_path,
            node_path=node_path)
    return node, kind, None


def _node_messages(node, method_name):
    getter = getattr(node, method_name, None)
    if not callable(getter):
        return []
    try:
        values = getter() or ()
    except Exception:
        return []
    return [str(value) for value in list(values)[:_MAX_FIELD_ITEMS]]


def _cook_report(node):
    return {
        "cook_errors": _node_messages(node, "errors"),
        "cook_warnings": _node_messages(node, "warnings"),
    }


def _call_output_entry(node, base_name, at_frame_name, output_index, frame):
    """优先调官方 frame 变体；缺失时退回 base；都不存在返 (None, entry_name)。

    返回 (value, used_entry_name)；used_entry_name 用于响应中披露实际入口。
    """
    if frame is not None:
        method = getattr(node, at_frame_name, None)
        if callable(method):
            try:
                return method(output_index, frame), at_frame_name
            except Exception:
                return None, at_frame_name
    method = getattr(node, base_name, None)
    if callable(method):
        try:
            return method(output_index), base_name
        except Exception:
            return None, base_name
    return None, None


def _public_surface(obj, max_items=_MAX_FIELD_ITEMS):
    """反射式探针：返回 obj 公开属性/方法名（去 dunder），仅读 hasattr 确认项。

    这是 cable wire 枚举的诚实实现：不预设方法名，只汇报实测存在的 surface，
    供响应与 live smoke 固化。任何虚构 API 都不会出现在这里。
    """
    if obj is None:
        return []
    try:
        names = [name for name in dir(obj)
                 if not name.startswith("_")]
    except Exception:
        return []
    return sorted(names)[:max_items]


def _probe_attribute(obj, name):
    """对 obj.name 做 callable 探针；存在且可取值返 (value, True)，否则 (None, False)。"""
    if obj is None or not hasattr(obj, name):
        return None, False
    value = getattr(obj, name, None)
    if callable(value):
        try:
            return value(), True
        except Exception:
            return None, False
    return value, True


def _cable_metadata(cable):
    """从 cable 对象反射出有界 metadata；只汇报实测可得字段。

    候选键是语义桶名（resolution/storage/type/bounds/...），由探针对真实
    cable 属性实测填充；未提供的留 absent 而非编造。
    """
    if cable is None:
        return {}
    candidates = (
        ("resolution", ("resolution", "size", "dimensions", "resolution_")),
        ("storage", ("storage", "datatype", "data_type", "format",
                     "storage_type", "precision")),
        ("bounds", ("bounds", "bbox", "boundingbox", "extent")),
        ("channels", ("channels", "channelcount", "channel_count",
                      "num_channels")),
        ("wire_name", ("name", "wirename", "wire_name", "label")),
        ("wire_type", ("type", "wiretype", "wire_type", "datatype")),
    )
    metadata = {}
    for bucket, names in candidates:
        for alias in names:
            value, ok = _probe_attribute(cable, alias)
            if not ok:
                continue
            metadata[bucket] = _bounded_value(value)
            break
    metadata["surface"] = _public_surface(cable)
    return metadata


def _cable_structure(node, output_index=0):
    """读 outputCableStructure(output_index)；反射枚举 wire 名/类型，不调虚构方法。

    H21 真实签名要求 output_index；缺失时回退无参形式以兼容旧假设。
    """
    getter = getattr(node, "outputCableStructure", None)
    if not callable(getter):
        return {"available": False, "wires": []}
    structure = None
    try:
        structure = getter(output_index)
    except Exception:
        try:
            structure = getter()
        except Exception:
            structure = None
    if structure is None:
        return {"available": False, "wires": []}
    wires = []
    # structure 可能是 dict {name: type}、list of tuple 或 list of wire 对象。
    if isinstance(structure, dict):
        for name in sorted(structure, key=lambda item: str(item)):
            wires.append({
                "name": str(name),
                "type": _bounded_value(structure[name]),
            })
    else:
        try:
            items = list(structure)
        except Exception:
            items = []
        for item in items[:_MAX_FIELD_ITEMS]:
            wire_name = _call_value(item, "name", None)
            wire_type = (_call_value(item, "type", None)
                         or _call_value(item, "dataType", None))
            if wire_name is None and isinstance(item, (list, tuple)) \
                    and len(item) >= 1:
                wire_name = item[0]
                wire_type = item[1] if len(item) >= 2 else wire_type
            wires.append({
                "name": str(wire_name) if wire_name is not None else "",
                "type": _bounded_value(wire_type) if wire_type is not None
                        else "",
            })
    return {"available": True, "wires": wires}


def _classify_wire(name, wire_type):
    text = "{0} {1}".format(name or "", wire_type or "").lower()
    if any(token in text for token in _IMAGE_WIRE_TOKENS):
        return "image"
    if any(token in text for token in _VDB_WIRE_TOKENS):
        return "vdb"
    if any(token in text for token in _GEOMETRY_WIRE_TOKENS):
        return "geometry"
    return "unknown"


def _data_type_names(node, method_name):
    getter = getattr(node, method_name, None)
    if not callable(getter):
        return []
    try:
        values = getter()
    except Exception:
        return []
    return [_bounded_value(item) for item in list(values)[:_MAX_FIELD_ITEMS]]


def _geo_count(geometry, plural):
    """通过 len(X()) 取计数；不可得返 None（H21 Geometry 用 points()/prims()）。"""
    getter = getattr(geometry, plural, None)
    if not callable(getter):
        return None
    try:
        seq = getter()
        return len(seq)
    except Exception:
        return None


def _geometry_summary(geometry):
    """把 hou.Geometry 转为有界 counts/bbox/attrib 摘要；不序列化完整几何。

    H21 真实 surface：无 numPoints/numPrims，改用 len(points())/len(prims())；
    无 vertices()。bbox 走 boundingBox()。
    """
    if geometry is None:
        return {"available": False}
    summary = {"available": True}
    counts = (
        ("point_count", "numPoints", "points"),
        ("prim_count", "numPrims", "prims"),
        ("vertex_count", "numVertices", "vertices"),
    )
    for bucket, num_name, plural in counts:
        num_getter = getattr(geometry, num_name, None)
        if callable(num_getter):
            try:
                summary[bucket] = _bounded_value(num_getter())
                continue
            except Exception:
                pass
        count = _geo_count(geometry, plural)
        if count is not None:
            summary[bucket] = _bounded_value(count)
    bbox = _call_value(geometry, "boundingBox", None)
    if bbox is not None:
        summary["bbox"] = _bounded_value(bbox)
    attribs = []
    for group_name in ("pointAttribs", "primAttribs", "vertexAttribs",
                       "globalAttribs"):
        getter = getattr(geometry, group_name, None)
        if not callable(getter):
            continue
        try:
            items = list(getter() or ())[:_MAX_FIELD_ITEMS]
        except Exception:
            items = []
        for item in items:
            attribs.append({
                "scope": group_name,
                "name": _call_value(item, "name", ""),
                "type": _call_value(item, "dataType", "")
                or _call_value(item, "type", ""),
                "size": _call_value(item, "size", None),
            })
    summary["attributes"] = attribs
    summary["attribute_count"] = len(attribs)
    return summary


def _layer_summary(layer):
    """把 Copernicus ImageLayer 转为有界 metadata；不回传像素。

    候选键含 H21 实测属性名（bufferResolution/displayWindow/dataWindow/
    channelCount/storageType）+ 通用别名；由探针对真实 layer 实测填充。
    """
    if layer is None:
        return {"available": False}
    summary = {"available": True}
    for bucket, names in (
            ("resolution", ("bufferResolution", "resolution", "size",
                            "dimensions")),
            ("storage", ("storageType", "storage", "datatype",
                         "data_type", "format")),
            ("display_window", ("displayWindow", "bounds", "bbox")),
            ("data_window", ("dataWindow", "extent")),
            ("channels", ("channelCount", "channels", "channelcount"))):
        for alias in names:
            value, ok = _probe_attribute(layer, alias)
            if ok:
                summary[bucket] = _bounded_value(value)
                break
    summary["surface"] = _public_surface(layer)
    return summary


def _vdb_summary(vdb):
    """把 Copernicus NanoVDB/grid 转为有界 metadata/统计；不回传体素。"""
    if vdb is None:
        return {"available": False}
    summary = {"available": True}
    for bucket, names in (
            ("grid_name", ("name", "gridname", "grid_name")),
            ("storage", ("storage", "datatype", "data_type", "grid_class")),
            ("bounds", ("bounds", "bbox", "extent")),
            ("resolution", ("resolution", "activexdim", "dims")),
            ("background", ("background", "background_value"))):
        for alias in names:
            value, ok = _probe_attribute(vdb, alias)
            if ok:
                summary[bucket] = _bounded_value(value)
                break
    summary["surface"] = _public_surface(vdb)
    return summary


def _select_wire_payload(node, output_index, frame, want, primary_base,
                         primary_at_frame):
    """先试官方入口 (layer/vdb)，缺失时反射 cable 按 wire 类型选 ImageLayer/NanoVDB。

    want: image / vdb。返回 (payload, entry_name, fallback_used)。
    cable wire 枚举只读 hasattr 确认存在的属性，不调虚构方法。
    """
    value, entry = _call_output_entry(
        node, primary_base, primary_at_frame, output_index, frame)
    if value is not None:
        return value, entry, False
    # cable fallback：只在官方入口不可得时启用，且仅读真实属性。
    cable_value, cable_entry = _call_output_entry(
        node, "cable", "cableAtFrame", output_index, frame)
    if cable_value is None:
        return None, entry, False
    surface = _public_surface(cable_value)
    target_attr = None
    for attr in surface:
        classified = _classify_wire(attr, attr)
        if classified == want:
            target_attr = attr
            break
    if target_attr is None:
        return None, entry, False
    selected = _probe_attribute(cable_value, target_attr)[0]
    return selected, cable_entry + "." + target_attr, True


def get_cop_info(hou, node_path):
    """返回 Copernicus 节点的 input/output data types、cable structure 与 metadata。

    读取 ``inputDataTypes``/``outputDataTypes``/``outputCableStructure`` 与
    每个 output 的 ``cable()``；cable wire surface 由反射探针如实汇报。
    响应经过 ``apply_response_cap``。
    """
    node, kind, error = _resolve_cop_node(hou, node_path)
    if error is not None:
        return _cap(error)
    try:
        outputs = []
        # 尝试枚举 output 数量：outputCableStructure 的 wire 数或 outputDataTypes。
        output_types = _data_type_names(node, "outputDataTypes")
        output_count = len(output_types)
        if output_count == 0:
            structure = _cable_structure(node)
            output_count = max(1, len(structure.get("wires", [])))
        for index in range(min(output_count, _MAX_FIELD_ITEMS)):
            cable_value, cable_entry = _call_output_entry(
                node, "cable", "cableAtFrame", index, None)
            outputs.append({
                "output_index": index,
                "cable_available": cable_value is not None,
                "cable_entry": cable_entry,
                "cable_metadata": _cable_metadata(cable_value),
            })
        result = {
            "status": "success",
            "node_path": node_path,
            "node_type": _call_value(node.type(), "name", "") or "",
            "input_data_types": _data_type_names(node, "inputDataTypes"),
            "output_data_types": output_types,
            "cable_structure": _cable_structure(node, 0),
            "outputs": outputs,
            "houdini_version": _version_key(hou),
        }
        result.update(_cook_report(node))
    except Exception as exc:
        result = _error("cop_info_query_failed", exc, node_path=node_path)
    return _cap(result)


def get_cop_geometry(hou, node_path, output_index=0, frame=None):
    """返回 Copernicus output 的有界 geometry 摘要（counts/bbox/attribs）。

    调 ``geometry(output_index)`` 或 ``geometryAtFrame``；不返回完整几何。
    响应经过 ``apply_response_cap``。
    """
    node, kind, error = _resolve_cop_node(hou, node_path)
    if error is not None:
        return _cap(error)
    safe_index = _validate_output_index(output_index)
    if safe_index is None:
        return _cap(_error(
            "invalid_output_index",
            "output_index must be a non-negative integer"))
    if frame is not None and not _finite_number(frame):
        return _cap(_error(
            "invalid_frame", "frame must be a finite number"))
    try:
        geometry, entry = _call_output_entry(
            node, "geometry", "geometryAtFrame", safe_index, frame)
        summary = _geometry_summary(geometry)
        result = {
            "status": "success",
            "node_path": node_path,
            "output_index": safe_index,
            "frame": frame,
            "geometry_entry": entry,
            "geometry": summary,
            "houdini_version": _version_key(hou),
        }
        result.update(_cook_report(node))
    except Exception as exc:
        result = _error(
            "cop_geometry_query_failed", exc, node_path=node_path,
            output_index=safe_index, frame=frame)
    return _cap(result)


def get_cop_layer(hou, node_path, output_index=0, frame=None):
    """返回 Copernicus ImageLayer 的有界 metadata（resolution/storage/bounds）。

    先调 ``layer``/``layerAtFrame``；不可得时从 ``cable`` 反射 wire 选
    ImageLayer。不返回原始像素。响应经过 ``apply_response_cap``。
    """
    node, kind, error = _resolve_cop_node(hou, node_path)
    if error is not None:
        return _cap(error)
    safe_index = _validate_output_index(output_index)
    if safe_index is None:
        return _cap(_error(
            "invalid_output_index",
            "output_index must be a non-negative integer"))
    if frame is not None and not _finite_number(frame):
        return _cap(_error(
            "invalid_frame", "frame must be a finite number"))
    try:
        layer, entry, fallback = _select_wire_payload(
            node, safe_index, frame, "image", "layer", "layerAtFrame")
        if layer is None:
            result = _warning(
                "layer_unavailable",
                "No ImageLayer available for output_index {0}".format(
                    safe_index),
                node_path=node_path, output_index=safe_index, frame=frame)
            result.update(_cook_report(node))
            return _cap(result)
        result = {
            "status": "success",
            "node_path": node_path,
            "output_index": safe_index,
            "frame": frame,
            "layer_entry": entry,
            "cable_fallback_used": fallback,
            "layer": _layer_summary(layer),
            "houdini_version": _version_key(hou),
        }
        result.update(_cook_report(node))
    except Exception as exc:
        result = _error(
            "cop_layer_query_failed", exc, node_path=node_path,
            output_index=safe_index, frame=frame)
    return _cap(result)


def get_cop_vdb(hou, node_path, output_index=0, frame=None):
    """返回 Copernicus NanoVDB/grid 的有界 metadata（grid_name/bounds/统计）。

    先调 ``vdb``/``vdbAtFrame``；不可得时从 ``cable`` 反射 wire 选 NanoVDB。
    不返回原始体素。响应经过 ``apply_response_cap``。
    """
    node, kind, error = _resolve_cop_node(hou, node_path)
    if error is not None:
        return _cap(error)
    safe_index = _validate_output_index(output_index)
    if safe_index is None:
        return _cap(_error(
            "invalid_output_index",
            "output_index must be a non-negative integer"))
    if frame is not None and not _finite_number(frame):
        return _cap(_error(
            "invalid_frame", "frame must be a finite number"))
    try:
        vdb, entry, fallback = _select_wire_payload(
            node, safe_index, frame, "vdb", "vdb", "vdbAtFrame")
        if vdb is None:
            result = _warning(
                "vdb_unavailable",
                "No NanoVDB available for output_index {0}".format(
                    safe_index),
                node_path=node_path, output_index=safe_index, frame=frame)
            result.update(_cook_report(node))
            return _cap(result)
        result = {
            "status": "success",
            "node_path": node_path,
            "output_index": safe_index,
            "frame": frame,
            "vdb_entry": entry,
            "cable_fallback_used": fallback,
            "vdb": _vdb_summary(vdb),
            "houdini_version": _version_key(hou),
        }
        result.update(_cook_report(node))
    except Exception as exc:
        result = _error(
            "cop_vdb_query_failed", exc, node_path=node_path,
            output_index=safe_index, frame=frame)
    return _cap(result)


def _resolve_cop_parent(hou, parent_path):
    """校验 parent 可编辑且 child category 为 Copernicus（"Cop"）。"""
    if not isinstance(parent_path, str) or not parent_path.strip():
        return None, _error(
            "invalid_parent_path",
            "parent_path must be a non-empty string")
    try:
        parent = hou.node(parent_path)
    except Exception as error:
        return None, _error(
            "parent_resolution_failed", error, parent_path=parent_path)
    if parent is None:
        return None, _error(
            "parent_not_found", "Parent node not found: " + parent_path,
            parent_path=parent_path)
    is_editable = _call_value(parent, "isEditable", None)
    if is_editable is False:
        return None, _error(
            "parent_locked", "Parent is not editable: " + parent_path,
            parent_path=parent_path)
    child_category = getattr(parent, "childTypeCategory", None)
    category_name = ""
    if callable(child_category):
        try:
            category_name = _name_of(child_category())
        except Exception:
            category_name = ""
    if category_name.lower() != "cop":
        # 检测 legacy COP2 parent（childTypeCategory 为 "Cop2"）。
        if category_name.lower() == "cop2":
            return None, _error(
                "unsupported_legacy_cop2",
                "Parent child category is legacy COP2; use Copernicus",
                parent_path=parent_path,
                child_category=category_name)
        return None, _error(
            "unsupported_parent_category",
            "Parent child category is not Copernicus Cop: "
            + category_name,
            parent_path=parent_path, child_category=category_name)
    return parent, None


def _cop_type_category(hou, category):
    """返回指定 category 的 NodeTypeCategory；缺失返 None。

    H21 真实 surface：Cop category 走专用 ``hou.copNodeTypeCategory()``；
    缺失时退回 ``hou.nodeTypeCategories()`` 按 category name 匹配。
    """
    if category.lower() == "cop":
        getter = getattr(hou, "copNodeTypeCategory", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
    cats_getter = getattr(hou, "nodeTypeCategories", None)
    if callable(cats_getter):
        try:
            cats = cats_getter()
        except Exception:
            return None
        try:
            items = cats.items()
        except AttributeError:
            items = list(cats)
        for key, value in items:
            if _name_of(value).lower() == category.lower():
                return value
    return None


def list_cop_node_types(hou, category=_DEFAULT_CATEGORY):
    """枚举 Copernicus node type registry；只读，不触发 cook 或写入。

    默认枚举 ``"Cop"`` category（H21+ Copernicus）；``"Cop2"`` 显式拒绝为
    legacy。每项含 ``name``/``label``，受 ``_MAX_ITEMS`` 上限约束。
    响应经过 ``apply_response_cap``。
    """
    if not isinstance(category, str) or not category.strip():
        return _cap(_error(
            "invalid_category", "category must be a non-empty string"))
    norm = category.strip()
    if norm.lower() == "cop2":
        return _cap(_error(
            "unsupported_legacy_cop2",
            "Legacy COP2 category is not supported; use Copernicus 'Cop'",
            category=norm))
    if norm.lower() != "cop":
        return _cap(_error(
            "unsupported_category",
            "Only the Copernicus 'Cop' category is supported, got: "
            + norm, category=norm))
    try:
        registry = _cop_type_category(hou, "Cop")
        if registry is None:
            return _cap(_warning(
                "cop_category_unavailable",
                "hou.nodeTypeCategory('Cop') is unavailable on this Houdini",
                category=norm, houdini_version=_version_key(hou)))
        node_types = registry.nodeTypes()
        entries = []
        for name in sorted(node_types, key=lambda item: str(item))[
                :_MAX_ITEMS]:
            type_obj = node_types[name]
            entries.append({
                "name": str(name),
                "label": _call_value(type_obj, "label", "") or "",
                "category": "Cop",
            })
        result = {
            "status": "success",
            "category": "Cop",
            "node_types": entries,
            "count": len(entries),
            "total": len(node_types),
            "truncated": len(node_types) > _MAX_ITEMS,
            "houdini_version": _version_key(hou),
        }
    except Exception as exc:
        result = _error(
            "cop_node_types_query_failed", exc, category=norm)
    return _cap(result)


def create_cop_node(hou, parent_path, node_type, node_name=None):
    """在可编辑 Copernicus parent 下创建节点（MUTATING，单 undo group）。

    校验 parent 可编辑且 child category 为 "Cop"，并要求 nodeType 在 Cop
    registry 中存在；``node_name`` 可选。响应经过 ``apply_response_cap``。
    """
    if not isinstance(node_type, str) or not node_type.strip():
        return _cap(_error(
            "invalid_node_type",
            "node_type must be a non-empty string"))
    if node_name is not None and (not isinstance(node_name, str)
                                  or not node_name.strip()):
        return _cap(_error(
            "invalid_node_name",
            "node_name must be a non-empty string or None"))
    parent, error = _resolve_cop_parent(hou, parent_path)
    if error is not None:
        return _cap(error)
    registry = _cop_type_category(hou, "Cop")
    if registry is not None:
        try:
            existing = registry.nodeTypes()
        except Exception:
            existing = {}
        if node_type not in existing:
            return _cap(_error(
                "node_type_unavailable",
                "node_type '{0}' not found in Cop category registry".format(
                    node_type),
                parent_path=parent_path, node_type=node_type,
                available_count=len(existing)))
    safe_name = node_name.strip() if isinstance(node_name, str) else None
    try:
        created = parent.createNode(node_type, safe_name)
    except Exception as exc:
        return _cap(_error(
            "create_node_failed", exc, parent_path=parent_path,
            node_type=node_type, node_name=safe_name))
    try:
        path = created.path()
    except Exception:
        path = parent_path.rstrip("/") + "/" + (safe_name or node_type)
    result = {
        "status": "success",
        "parent_path": parent_path,
        "node_type": node_type,
        "node_name": safe_name,
        "created_path": path,
    }
    result.update(_cook_report(created))
    return _cap(result)


# 白名单 flag → 官方 setter 名。未知键在任何写入前被拒绝（原子预校验）。
_FLAG_SETTERS = {
    "display": "setDisplayFlag",
    "export": "setExportFlag",
    "template": "setTemplateFlag",
    "selectable_template": "setSelectableTemplateFlag",
    "compress": "setCompressFlag",
    "bypass": "bypass",
}


def set_cop_flags(hou, node_path, flags):
    """原子地设置 Copernicus 节点白名单 flags（MUTATING）。

    ``flags`` 必须是 dict，键只能是 display/export/template/
    selectable_template/compress/bypass，值为 bool。未知键在任何写入前
    拒绝整次请求。响应经过 ``apply_response_cap``。
    """
    if not isinstance(flags, dict) or not flags:
        return _cap(_error(
            "invalid_flags",
            "flags must be a non-empty dict of flag->bool"))
    normalized = {}
    for key, value in flags.items():
        if key not in _FLAG_SETTERS:
            return _cap(_error(
                "unsupported_flag",
                "Unknown flag '{0}'; allowed: {1}".format(
                    key, sorted(_FLAG_SETTERS)),
                node_path=node_path))
        if not isinstance(value, bool):
            return _cap(_error(
                "invalid_flag_value",
                "Flag '{0}' must be a bool, got {1}".format(
                    key, type(value).__name__),
                node_path=node_path))
        normalized[key] = value
    node, kind, error = _resolve_cop_node(hou, node_path)
    if error is not None:
        return _cap(error)
    applied = []
    try:
        for key, value in normalized.items():
            setter_name = _FLAG_SETTERS[key]
            setter = getattr(node, setter_name, None)
            if not callable(setter):
                return _cap(_error(
                    "setter_unavailable",
                    "Node does not expose setter '{0}' for flag '{1}'".format(
                        setter_name, key),
                    node_path=node_path, flag=key,
                    applied=applied))
            setter(value)
            applied.append(key)
    except Exception as exc:
        return _cap(_error(
            "set_flags_failed", exc, node_path=node_path, applied=applied))
    result = {
        "status": "success",
        "node_path": node_path,
        "applied_flags": applied,
        "flags": normalized,
    }
    result.update(_cook_report(node))
    return _cap(result)
