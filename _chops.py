"""_chops.py — Houdini 21+ CHOP network 有界查询与编辑工具。

核心约束：
- 4 个公共函数都显式接收注入的 ``hou``；模块顶层不导入 hou，零新依赖。
- 仅支持 H21+ ``hou.ChopNode``；数据入口严格走官方 ``ChopNode.clip()`` →
  ``hou.Clip``，再以 ``Clip.tracks()``/``Clip.track(name)``（或
  ``ChopNode.tracks()``/``track()`` 退化）获取 ``hou.Track``。
- 范围与速率来自 ``Clip.sampleRange()``/``sampleRate()``；track 样本数来自
  ``Track.numSamples()``。
- 有界 sample 读取：窗口走 ``Track.evalAtSampleRange(start, end)``（start/end
  为闭区间 sample index，夹取到 clip sample range）；单点 frame/time/sample
  分别走 ``evalAtFrame``/``evalAtTime``/``evalAtSample``；完整 track 仅在预先
  确认 ``numSamples <= max_samples`` 时使用 ``allSamples()``。
- 严格禁止 ``findTrack`` 与 ``.evaluator`` 等旧/虚构 API。
- 多 channel 请求实行 ``max_channels``、``max_samples_per_channel`` 与总响应
  cap 三层限制；截断时返回原始范围、实际范围与 ``truncated: true``。
- ``export_chop_to_parm`` 的语义固定为在目标参数上创建 HScript ``chop()``
  channel reference：预校验 source track + scalar numeric target parm，默认
  拒绝覆盖已有 expression/keyframe（仅 ``replace_existing=True`` 替换并披露）。
  它不启用 CHOP export flag，不创建 Export CHOP 映射，也不烘焙 keyframe。
- 所有公共返回（success/warning/error）均经过 ``apply_response_cap``。
"""
import math

from . import _common as cmn


# 三层 cap 默认值。
_MAX_CHANNELS = 32
_MAX_SAMPLES_PER_CHANNEL = 4096
_MAX_ITEMS = 1000
_MAX_FIELD_ITEMS = 64
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


def _finite_int(value, default=None, minimum=0, maximum=_MAX_ITEMS):
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(minimum, min(maximum, value))


def _finite_number(value):
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _bounded_value(value, depth=3, max_items=_MAX_FIELD_ITEMS):
    """把任意 HOM 值转为有界 JSON-safe 结构；不展开大型容器。"""
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


def _node_category_name(node):
    try:
        return str(node.type().category().name())
    except Exception:
        return ""


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


def _validate_output_index(output_index):
    """output_index 必须是 >= 0 的整数；负值或非整数返 None（调用方报错）。"""
    if isinstance(output_index, bool) or not isinstance(output_index, int):
        return None
    if output_index < 0:
        return None
    return min(output_index, _MAX_ITEMS)


def _resolve_chop_node(hou, node_path):
    """解析路径并校验是 hou.ChopNode。返回 (node, error)。"""
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
            "node_not_found", "CHOP node not found: " + node_path,
            node_path=node_path)
    chop_class = getattr(hou, "ChopNode", None)
    if chop_class is not None and not isinstance(node, chop_class):
        return node, _error(
            "not_a_chop_node",
            "Node is not a hou.ChopNode: " + node_path,
            node_path=node_path)
    if chop_class is None:
        # 防御：环境无 ChopNode 类定义，按 category 名做 best-effort 判定。
        category = _node_category_name(node).lower()
        if category != "chop":
            return node, _error(
                "not_a_chop_node",
                "Node is not a CHOP node: " + node_path,
                node_path=node_path, category=category)
    return node, None


def _get_clip(hou, node, output_index):
    """数据入口：node.clip(output_index) -> hou.Clip。返回 (clip, error)。

    优先官方 ``clip(output_index)`` 签名；缺失时退回无参 ``clip()`` 以兼容
    旧假设，响应披露实际入口。
    """
    getter = getattr(node, "clip", None)
    if not callable(getter):
        return None, _error(
            "clip_unavailable",
            "ChopNode does not expose clip() on this Houdini",
            houdini_version=_version_key(hou))
    try:
        return getter(output_index), None
    except TypeError:
        try:
            return getter(), None
        except Exception as error:
            return None, _error("clip_query_failed", error)
    except Exception as error:
        return None, _error("clip_query_failed", error)


def _clip_tracks(clip, node):
    """枚举 track：优先 clip.tracks()，缺失时退化 node.tracks()。

    返回 track 列表（可能为空）。绝不调用 findTrack。
    """
    for source in (clip, node):
        getter = getattr(source, "tracks", None)
        if callable(getter):
            try:
                values = getter()
            except Exception:
                continue
            try:
                return list(values or [])
            except Exception:
                return []
    return []


def _clip_track(clip, node, name):
    """取单 track：优先 clip.track(name)，缺失时退化 node.track(name)。"""
    for source in (clip, node):
        getter = getattr(source, "track", None)
        if callable(getter):
            try:
                return getter(name), True
            except Exception:
                continue
    return None, False


def _clip_sample_range(clip):
    """返回 (start, end) sample index 闭区间；不可得返 (None, None)。"""
    value = _call_value(clip, "sampleRange", None)
    if value is None:
        return None, None
    try:
        seq = list(value)
    except Exception:
        return None, None
    if len(seq) >= 2:
        try:
            return int(seq[0]), int(seq[1])
        except (TypeError, ValueError):
            return None, None
    return None, None


def list_chop_channels(hou, node_path, output_index=0,
                       max_channels=_MAX_CHANNELS):
    """枚举 CHOP clip 的 channel（track）名、sample range/rate/count。

    数据入口 ``ChopNode.clip()`` → ``Clip.tracks()``；单 track 走
    ``Clip.track(name)``。读取可能触发 CHOP cook。响应经过
    ``apply_response_cap``。
    """
    safe_index = _validate_output_index(output_index)
    if safe_index is None:
        return _cap(_error(
            "invalid_output_index",
            "output_index must be a non-negative integer"))
    safe_max_channels = _finite_int(
        max_channels, default=_MAX_CHANNELS, minimum=1,
        maximum=_MAX_CHANNELS)
    if safe_max_channels is None:
        safe_max_channels = _MAX_CHANNELS
    node, error = _resolve_chop_node(hou, node_path)
    if error is not None:
        return _cap(error)
    try:
        clip, clip_error = _get_clip(hou, node, safe_index)
        if clip_error is not None:
            result = clip_error
            result.setdefault("node_path", node_path)
            result.setdefault("output_index", safe_index)
            return _cap(result)
        tracks = _clip_tracks(clip, node)
        rate = _call_value(clip, "sampleRate", None)
        sr_start, sr_end = _clip_sample_range(clip)
        channels = []
        truncated = len(tracks) > safe_max_channels
        for track in tracks[:safe_max_channels]:
            channels.append({
                "name": _name_of(track),
                "sample_count": _call_value(track, "numSamples", None),
            })
        result = {
            "status": "success",
            "node_path": node_path,
            "output_index": safe_index,
            "node_type": _call_value(node.type(), "name", "") or "",
            "sample_rate": _bounded_value(rate),
            "sample_range": [sr_start, sr_end],
            "channels": channels,
            "channel_count": len(channels),
            "total_channels": len(tracks),
            "truncated": truncated,
            "houdini_version": _version_key(hou),
        }
        result.update(_cook_report(node))
    except Exception as exc:
        result = _error(
            "chop_channels_query_failed", exc, node_path=node_path,
            output_index=safe_index)
    return _cap(result)


def _query_track_values(track, sample, frame, time, start, end,
                        sr_start, sr_end, max_samples):
    """根据查询模式从 track 取值。返回 (values, query_mode, actual_range,
    requested_range, truncated, error)。

    - sample/frame/time 给定其一 → 单点 evalAt*（values 长度 1）
    - start/end 给定 → evalAtSampleRange（闭区间，已夹取到 clip range）
    - 都不给 → 完整 track：numSamples<=max_samples 用 allSamples，
      否则 evalAtSampleRange(全 range)
    """
    truncated = False
    requested_range = None
    # 单点模式优先级：sample > frame > time
    if sample is not None:
        if not _finite_number(sample):
            return None, None, None, None, False, _error(
                "invalid_sample", "sample must be a finite number")
        getter = getattr(track, "evalAtSample", None)
        if not callable(getter):
            return None, None, None, None, False, _error(
                "eval_unavailable",
                "Track does not expose evalAtSample()")
        try:
            value = getter(sample)
        except Exception as exc:
            return None, None, None, None, False, _error(
                "eval_sample_failed", exc)
        return ([_bounded_value(value)], "sample",
                [sample, sample], [sample, sample], False, None)
    if frame is not None:
        if not _finite_number(frame):
            return None, None, None, None, False, _error(
                "invalid_frame", "frame must be a finite number")
        getter = getattr(track, "evalAtFrame", None)
        if not callable(getter):
            return None, None, None, None, False, _error(
                "eval_unavailable",
                "Track does not expose evalAtFrame()")
        try:
            value = getter(frame)
        except Exception as exc:
            return None, None, None, None, False, _error(
                "eval_frame_failed", exc)
        return ([_bounded_value(value)], "frame",
                [frame, frame], [frame, frame], False, None)
    if time is not None:
        if not _finite_number(time):
            return None, None, None, None, False, _error(
                "invalid_time", "time must be a finite number")
        getter = getattr(track, "evalAtTime", None)
        if not callable(getter):
            return None, None, None, None, False, _error(
                "eval_unavailable",
                "Track does not expose evalAtTime()")
        try:
            value = getter(time)
        except Exception as exc:
            return None, None, None, None, False, _error(
                "eval_time_failed", exc)
        return ([_bounded_value(value)], "time",
                [time, time], [time, time], False, None)

    # 范围 / 完整模式
    range_getter = getattr(track, "evalAtSampleRange", None)
    has_range = callable(range_getter)
    # 解析请求窗口
    if start is None and end is None:
        # 完整 track：优先 allSamples（仅当 numSamples 在上限内）
        num_samples = _call_value(track, "numSamples", None)
        all_getter = getattr(track, "allSamples", None)
        if (callable(all_getter)
                and isinstance(num_samples, int)
                and num_samples <= max_samples):
            try:
                values = list(all_getter() or [])
            except Exception as exc:
                return None, None, None, None, False, _error(
                    "all_samples_failed", exc)
            bounded = [_bounded_value(v) for v in values[:max_samples]]
            truncated = len(values) > max_samples
            full_range = [sr_start, sr_end] if sr_start is not None \
                else [0, num_samples - 1]
            return (bounded, "all_samples", full_range, full_range,
                    truncated, None)
        # allSamples 不可用或超限 → 走 evalAtSampleRange(全 clip range)
        if not has_range:
            return None, None, None, None, False, _error(
                "eval_unavailable",
                "Track exposes neither allSamples() nor evalAtSampleRange()")
        q_start = sr_start if sr_start is not None else 0
        q_end = sr_end if sr_end is not None else 0
        requested_range = [q_start, q_end]
    else:
        if not has_range:
            return None, None, None, None, False, _error(
                "eval_unavailable",
                "Track does not expose evalAtSampleRange()")
        if start is not None and not _finite_number(start):
            return None, None, None, None, False, _error(
                "invalid_start", "start must be a finite sample index")
        if end is not None and not _finite_number(end):
            return None, None, None, None, False, _error(
                "invalid_end", "end must be a finite sample index")
        # 夹取到 clip sample range（闭区间）
        clip_lo = sr_start if sr_start is not None else 0
        clip_hi = sr_end if sr_end is not None else clip_lo
        q_start = clip_lo if start is None else max(clip_lo, int(start))
        q_end = clip_hi if end is None else min(clip_hi, int(end))
        if q_end < q_start:
            q_end = q_start
        requested_range = [start, end]

    # 限制窗口长度到 max_samples_per_channel
    window = q_end - q_start + 1
    actual_end = q_end
    if window > max_samples:
        actual_end = q_start + max_samples - 1
        truncated = True
    try:
        values = range_getter(q_start, actual_end)
    except Exception as exc:
        return None, None, None, None, False, _error(
            "eval_sample_range_failed", exc)
    try:
        bounded = [_bounded_value(v) for v in list(values)[:max_samples]]
    except Exception:
        bounded = [_bounded_value(values)]
    if len(bounded) > max_samples:
        bounded = bounded[:max_samples]
        truncated = True
    actual_range = [q_start, actual_end]
    return (bounded, "sample_range", actual_range, requested_range,
            truncated, None)


def get_chop_data(hou, node_path, channels=None, output_index=0,
                  sample=None, frame=None, time=None,
                  start=None, end=None,
                  max_channels=_MAX_CHANNELS,
                  max_samples_per_channel=_MAX_SAMPLES_PER_CHANNEL):
    """有界读取 CHOP track 的 sample 数据。

    查询模式（优先级）：``sample``/``frame``/``time`` 单点（对应
    ``evalAtSample``/``evalAtFrame``/``evalAtTime``）；``start``/``end``
    sample index 闭区间（``evalAtSampleRange``，夹取到 clip sample range）；
    都不给则完整 track（``numSamples<=max_samples`` 时 ``allSamples``，否则
    ``evalAtSampleRange`` 全 range）。多 track 同时受 ``max_channels``、
    ``max_samples_per_channel`` 与响应 cap 限制；截断时返回原始范围、实际
    范围与 ``truncated: true``。读取可能触发 CHOP cook。响应经过
    ``apply_response_cap``。
    """
    safe_index = _validate_output_index(output_index)
    if safe_index is None:
        return _cap(_error(
            "invalid_output_index",
            "output_index must be a non-negative integer"))
    safe_max_channels = _finite_int(
        max_channels, default=_MAX_CHANNELS, minimum=1,
        maximum=_MAX_CHANNELS)
    if safe_max_channels is None:
        safe_max_channels = _MAX_CHANNELS
    safe_max_samples = _finite_int(
        max_samples_per_channel, default=_MAX_SAMPLES_PER_CHANNEL,
        minimum=1, maximum=_MAX_SAMPLES_PER_CHANNEL)
    if safe_max_samples is None:
        safe_max_samples = _MAX_SAMPLES_PER_CHANNEL
    # channels 参数规范化
    if channels is None:
        requested_names = None
    elif isinstance(channels, str):
        requested_names = [channels] if channels.strip() else None
    else:
        try:
            requested_names = [str(name) for name in list(channels)
                               if str(name).strip()]
        except Exception:
            return _cap(_error(
                "invalid_channels",
                "channels must be None, a name string, or a list of names"))
        if not requested_names:
            requested_names = None
    node, error = _resolve_chop_node(hou, node_path)
    if error is not None:
        return _cap(error)
    try:
        clip, clip_error = _get_clip(hou, node, safe_index)
        if clip_error is not None:
            result = clip_error
            result.setdefault("node_path", node_path)
            result.setdefault("output_index", safe_index)
            return _cap(result)
        sr_start, sr_end = _clip_sample_range(clip)
        rate = _call_value(clip, "sampleRate", None)

        # 选定要读的 track
        if requested_names is None:
            tracks = _clip_tracks(clip, node)
            track_names = [_name_of(track) for track in tracks]
            track_pairs = list(zip(tracks, track_names))
        else:
            track_pairs = []
            missing = []
            for name in requested_names[:safe_max_channels]:
                track, found = _clip_track(clip, node, name)
                if found and track is not None:
                    track_pairs.append((track, name))
                else:
                    missing.append(name)
            if missing:
                return _cap(_error(
                    "channel_not_found",
                    "Requested channel(s) not found in clip: "
                    + ", ".join(missing),
                    node_path=node_path, output_index=safe_index,
                    missing_channels=missing))

        any_truncated = False
        channel_results = []
        for track, name in track_pairs[:safe_max_channels]:
            values, mode, actual_range, requested_range, truncated, qerr = \
                _query_track_values(
                    track, sample, frame, time, start, end,
                    sr_start, sr_end, safe_max_samples)
            if qerr is not None:
                qerr.setdefault("node_path", node_path)
                qerr.setdefault("output_index", safe_index)
                qerr.setdefault("channel", name)
                return _cap(qerr)
            if truncated:
                any_truncated = True
            channel_results.append({
                "name": name,
                "sample_count": len(values),
                "samples": values,
                "query_mode": mode,
                "actual_range": actual_range,
                "requested_range": requested_range,
                "truncated": truncated,
            })
        channel_truncated = len(track_pairs) > safe_max_channels
        if channel_truncated:
            any_truncated = True
        query = {}
        if sample is not None:
            query["sample"] = sample
        if frame is not None:
            query["frame"] = frame
        if time is not None:
            query["time"] = time
        if start is not None:
            query["start"] = start
        if end is not None:
            query["end"] = end
        result = {
            "status": "success",
            "node_path": node_path,
            "output_index": safe_index,
            "sample_rate": _bounded_value(rate),
            "sample_range": [sr_start, sr_end],
            "query": query,
            "channels": channel_results,
            "channel_count": len(channel_results),
            "total_channels": (len(track_names)
                               if requested_names is None
                               else len(requested_names)),
            "truncated": any_truncated,
            "houdini_version": _version_key(hou),
        }
        result.update(_cook_report(node))
    except Exception as exc:
        result = _error(
            "chop_data_query_failed", exc, node_path=node_path,
            output_index=safe_index)
    return _cap(result)


def _resolve_chop_parent(hou, parent_path):
    """校验 parent 可编辑且 child category 为 "Chop"。"""
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
    if category_name.lower() != "chop":
        return None, _error(
            "unsupported_parent_category",
            "Parent child category is not CHOP: " + category_name,
            parent_path=parent_path, child_category=category_name)
    return parent, None


def _chop_type_category(hou, category):
    """返回指定 category 的 NodeTypeCategory；缺失返 None。"""
    if category.lower() == "chop":
        getter = getattr(hou, "chopNodeTypeCategory", None)
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


def create_chop_node(hou, parent_path, node_type, node_name=None):
    """在可编辑 CHOP parent 下创建节点（MUTATING，单 undo group）。

    校验 parent 可编辑且 child category 为 "Chop"，并要求 nodeType 在 Chop
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
    parent, error = _resolve_chop_parent(hou, parent_path)
    if error is not None:
        return _cap(error)
    registry = _chop_type_category(hou, "Chop")
    if registry is not None:
        try:
            existing = registry.nodeTypes()
        except Exception:
            existing = {}
        if node_type not in existing:
            return _cap(_error(
                "node_type_unavailable",
                "node_type '{0}' not found in Chop category registry".format(
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


def _resolve_target_parm(hou, target_path, target_parm):
    """校验 target node 存在、parm 存在且为 scalar numeric。

    返回 (target_node, parm, error)。
    """
    if not isinstance(target_path, str) or not target_path.strip():
        return None, None, _error(
            "invalid_target_path",
            "target_path must be a non-empty string")
    if not isinstance(target_parm, str) or not target_parm.strip():
        return None, None, _error(
            "invalid_target_parm",
            "target_parm must be a non-empty string")
    try:
        target_node = hou.node(target_path)
    except Exception as error:
        return None, None, _error(
            "target_resolution_failed", error, target_path=target_path)
    if target_node is None:
        return None, None, _error(
            "target_not_found", "Target node not found: " + target_path,
            target_path=target_path)
    parm_getter = getattr(target_node, "parm", None)
    if not callable(parm_getter):
        return target_node, None, _error(
            "parm_unavailable",
            "Target node does not expose parm()",
            target_path=target_path)
    try:
        parm = parm_getter(target_parm)
    except Exception as error:
        return target_node, None, _error(
            "parm_resolution_failed", error,
            target_path=target_path, target_parm=target_parm)
    if parm is None:
        return target_node, None, _error(
            "parm_not_found",
            "Parameter not found: " + target_parm,
            target_path=target_path, target_parm=target_parm)
    # scalar numeric 校验：numComponents==1（拒绝 vector/multi）
    num_components = _call_value(parm, "numComponents", None)
    if num_components is not None and num_components != 1:
        return target_node, parm, _error(
            "parm_not_scalar",
            "Target parm is not scalar (numComponents={0})".format(
                num_components),
            target_path=target_path, target_parm=target_parm,
            num_components=num_components)
    # 可编辑性（parm 上的 setStatus / isEditable 不稳定，用 node 级 isEditable
    # 已在 caller 之外；这里只拒绝明显的 non-numeric 类型）。
    return target_node, parm, None


def _parm_has_existing(parm):
    """返回 (has_expression, expression, has_keyframes, keyframe_count)。"""
    existing_expr = _call_value(parm, "expression", None)
    has_expr = bool(existing_expr)
    keyframe_count = 0
    kf_getter = getattr(parm, "keyframes", None)
    if callable(kf_getter):
        try:
            keyframes = list(kf_getter() or [])
            keyframe_count = len(keyframes)
        except Exception:
            keyframe_count = 0
    return has_expr, existing_expr, keyframe_count > 0, keyframe_count


def export_chop_to_parm(hou, chop_path, channel, target_path, target_parm,
                        output_index=0, replace_existing=False):
    """在目标参数上创建 HScript chop() channel reference（MUTATING）。

    预校验 source track 与 scalar numeric target parm 均存在且可编辑，
    在任何写入前构造并验证绝对 CHOP channel path，然后以
    ``target.setExpression('chop("<channel_path>")', hou.exprLanguage.Hscript)``
    建立实时 channel reference。它不调用 ``ChopNode.setExportFlag``，不创建
    Export CHOP 映射，也不采样烘焙 keyframe。目标已有表达式/关键帧时默认拒绝；
    仅 ``replace_existing=True`` 时替换并在响应披露旧/新表达式。响应经过
    ``apply_response_cap``。
    """
    if not isinstance(channel, str) or not channel.strip():
        return _cap(_error(
            "invalid_channel",
            "channel must be a non-empty string"))
    safe_index = _validate_output_index(output_index)
    if safe_index is None:
        return _cap(_error(
            "invalid_output_index",
            "output_index must be a non-negative integer"))
    # 预校验 target（在任何 source 写入假设前完成双侧原子校验）
    target_node, parm, target_error = _resolve_target_parm(
        hou, target_path, target_parm)
    if target_error is not None:
        return _cap(target_error)
    # 预校验 source CHOP node + clip + track
    node, node_error = _resolve_chop_node(hou, chop_path)
    if node_error is not None:
        return _cap(node_error)
    clip, clip_error = _get_clip(hou, node, safe_index)
    if clip_error is not None:
        result = clip_error
        result.setdefault("chop_path", chop_path)
        result.setdefault("output_index", safe_index)
        return _cap(result)
    track, found = _clip_track(clip, node, channel)
    if not found or track is None:
        return _cap(_error(
            "channel_not_found",
            "Source channel not found in clip: " + channel,
            chop_path=chop_path, output_index=safe_index, channel=channel))
    # 构造并验证绝对 CHOP channel path
    try:
        chop_node_path = node.path()
    except Exception:
        chop_node_path = chop_path
    channel_path = chop_node_path.rstrip("/") + "/" + channel.strip()

    # 检查目标已有 expression/keyframe（默认拒绝覆盖）
    has_expr, old_expr, has_kf, kf_count = _parm_has_existing(parm)
    if (has_expr or has_kf) and not replace_existing:
        return _cap(_warning(
            "target_occupied",
            "Target parm already has an expression or keyframes; "
            "pass replace_existing=True to overwrite",
            target_path=target_path, target_parm=target_parm,
            existing_expression=old_expr if has_expr else None,
            has_keyframes=has_kf, keyframe_count=kf_count,
            channel_path=channel_path))

    expr = 'chop("{0}")'.format(channel_path)
    expr_lang_obj = getattr(hou, "exprLanguage", None)
    hscript_lang = (getattr(expr_lang_obj, "Hscript", None)
                    if expr_lang_obj is not None else None)
    set_expr = getattr(parm, "setExpression", None)
    if not callable(set_expr):
        return _cap(_error(
            "set_expression_unavailable",
            "Target parm does not expose setExpression()",
            target_path=target_path, target_parm=target_parm))
    try:
        if hscript_lang is not None:
            set_expr(expr, hscript_lang)
        else:
            set_expr(expr)
    except Exception as exc:
        return _cap(_error(
            "set_expression_failed", exc,
            target_path=target_path, target_parm=target_parm,
            channel_path=channel_path, expression=expr))
    # 读回新表达式（验证写入并供响应披露）
    new_expr = _call_value(parm, "expression", None)
    result = {
        "status": "success",
        "chop_path": chop_path,
        "output_index": safe_index,
        "channel": channel,
        "channel_path": channel_path,
        "target_path": target_path,
        "target_parm": target_parm,
        "expression": expr,
        "expression_language": "Hscript" if hscript_lang is not None
                               else "default",
        "verified_expression": new_expr,
        "replaced_existing": bool(replace_existing and (has_expr or has_kf)),
    }
    if replace_existing and (has_expr or has_kf):
        result["previous_expression"] = old_expr if has_expr else None
        result["previous_keyframe_count"] = kf_count
    result.update(_cook_report(node))
    return _cap(result)
