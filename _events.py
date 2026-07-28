"""Houdini 进程级事件缓冲与正式 HOM callback 适配。

本模块故意不在顶层 import ``hou``。Houdini 由 server.py 注入，事件状态
保持为进程级 singleton；callback 只做轻量字段读取和 append，不执行 cook、
render、flipbook 或几何序列化。
"""

import base64
import json
import time
from collections import deque

try:
    from . import _common as _common
except ImportError:
    try:
        import _common
    except ImportError:
        _common = None


EVENT_TYPES = (
    "scene_saved",
    "node_created",
    "node_deleted",
    "frame_changed",
)
EVENT_TYPE_SET = frozenset(EVENT_TYPES)

DEFAULT_MAX_EVENTS = 1000
DEFAULT_DEBOUNCE_SECONDS = 0.25
DEFAULT_LIMIT = 100
MIN_LIMIT = 1
MAX_LIMIT = 500
CURSOR_VERSION = 1
MAX_PUBLIC_EVENT_BYTES = 4096


def _json_safe(value):
    """把 callback payload 转为有限、可 JSON 序列化的结构。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    try:
        return str(value)
    except Exception:
        return "<unserializable>"


def _serialized_size(value):
    try:
        return len(json.dumps(
            value, ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return MAX_PUBLIC_EVENT_BYTES + 1


def _compact_public_event(event):
    """给单条超大事件保留有界摘要，避免 cap 删除整条 events 列表。"""
    result = dict(event)
    original_payload = result.get("payload")
    original_size = _serialized_size(result)
    if original_size <= MAX_PUBLIC_EVENT_BYTES:
        return result

    try:
        preview = json.dumps(
            original_payload, ensure_ascii=False, default=str,
            separators=(",", ":"))[:1024]
    except (TypeError, ValueError, UnicodeError):
        preview = str(original_payload)[:1024]
    result["payload"] = {
        "_truncated": True,
        "original_size": original_size,
        "preview": preview,
    }
    result["_truncated"] = True
    result["_original_size"] = original_size

    for field, max_chars in (("path", 512), ("type", 128)):
        value = result.get(field)
        if isinstance(value, str) and len(value) > max_chars:
            result[field] = value[:max_chars]
    if _serialized_size(result) <= MAX_PUBLIC_EVENT_BYTES:
        return result

    result["payload"].pop("preview", None)
    if _serialized_size(result) <= MAX_PUBLIC_EVENT_BYTES:
        return result
    result["payload"] = {
        "_truncated": True,
        "original_size": original_size,
    }
    return result


def _ordered_types(values):
    selected = set(values)
    return [event_type for event_type in EVENT_TYPES if event_type in selected]


def normalize_event_types(types, none_means_all=False):
    """校验并按稳定顺序标准化事件类型。"""
    if types is None:
        values = list(EVENT_TYPES) if none_means_all else []
    elif isinstance(types, str):
        values = [types]
    elif isinstance(types, (list, tuple, set, frozenset)):
        values = list(types)
    else:
        return None, {
            "status": "error",
            "error": "invalid_types",
            "message": "types must be null, a string, or a list of strings",
        }

    invalid = [value for value in values
               if not isinstance(value, str) or value not in EVENT_TYPE_SET]
    if invalid:
        return None, {
            "status": "error",
            "error": "invalid_types",
            "invalid_types": [_json_safe(value) for value in invalid],
            "supported_types": list(EVENT_TYPES),
        }
    return _ordered_types(values), None


def _clamp_limit(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, value))


def _encode_cursor(generation, after_seq, snapshot_seq):
    payload = {
        "v": CURSOR_VERSION,
        "generation": int(generation),
        "after_seq": int(after_seq),
        "snapshot_seq": int(snapshot_seq),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(cursor):
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("cursor must be a non-empty string")
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(
            (cursor + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ValueError("cursor is not valid base64 JSON")
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise ValueError("unsupported cursor version")
    try:
        generation = int(payload["generation"])
        after_seq = int(payload["after_seq"])
        snapshot_seq = int(payload["snapshot_seq"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("cursor fields are invalid")
    if generation < 0 or after_seq < 0 or snapshot_seq < 0:
        raise ValueError("cursor sequence fields must be non-negative")
    if after_seq > snapshot_seq:
        raise ValueError("cursor after_seq exceeds snapshot_seq")
    return generation, after_seq, snapshot_seq


class EventCollector(object):
    """bounded deque with append-time debounce and stable identity index."""

    def __init__(self, maxlen=DEFAULT_MAX_EVENTS,
                 dedupe_window=DEFAULT_DEBOUNCE_SECONDS,
                 monotonic_clock=None, wall_clock=None):
        self.maxlen = max(1, int(maxlen))
        self.dedupe_window = max(0.0, float(dedupe_window))
        self._monotonic = monotonic_clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self.buffer = deque(maxlen=self.maxlen)
        # events is kept as an explicit alias because callers/tests use both
        # names; both always reference the same bounded deque.
        self.events = self.buffer
        self.key_index = {}
        self.next_sequence = 0
        self.dropped_total = 0

    @property
    def max_sequence(self):
        return self.next_sequence

    def append(self, event_type, path=None, payload=None,
               timestamp=None, now=None):
        event_type = str(event_type)
        key = (event_type, path)
        current = self._monotonic() if now is None else float(now)
        indexed = self.key_index.get(key)
        if indexed is not None:
            indexed_seq, indexed_event = indexed
            last = indexed_event.get("_dedupe_at")
            if (last is not None
                    and current - float(last) <= self.dedupe_window):
                indexed_event["payload"] = _json_safe(payload)
                indexed_event["timestamp"] = (
                    self._wall_clock() if timestamp is None else timestamp)
                indexed_event["count"] = int(indexed_event.get("count", 1)) + 1
                indexed_event["_dedupe_at"] = current
                # Preserve identity and sequence; the index must continue to
                # point to this exact mutable object.
                self.key_index[key] = (indexed_seq, indexed_event)
                return indexed_event

        evicted = self.buffer[0] if len(self.buffer) >= self.maxlen else None
        self.next_sequence += 1
        event = {
            "type": event_type,
            "path": path,
            "payload": _json_safe(payload),
            "timestamp": self._wall_clock() if timestamp is None else timestamp,
            "count": 1,
            "_seq": self.next_sequence,
            "_dedupe_at": current,
        }
        self.buffer.append(event)
        if evicted is not None:
            self.dropped_total += 1
            evicted_key = (evicted.get("type"), evicted.get("path"))
            indexed = self.key_index.get(evicted_key)
            if (indexed is not None and indexed[0] == evicted.get("_seq")
                    and indexed[1] is evicted):
                del self.key_index[evicted_key]
        self.key_index[key] = (self.next_sequence, event)
        return event

    def matching(self, types, after_seq=0, snapshot_seq=None):
        selected = set(types or ())
        if snapshot_seq is None:
            snapshot_seq = self.max_sequence
        return [event for event in self.buffer
                if event.get("type") in selected
                and after_seq < int(event.get("_seq", 0)) <= snapshot_seq]

    def remove_objects(self, events):
        """只从 buffer 移除传入的真实 event object。"""
        if not events:
            return
        identities = set(id(event) for event in events)
        kept = [event for event in self.buffer if id(event) not in identities]
        self.buffer.clear()
        self.buffer.extend(kept)
        for event in events:
            key = (event.get("type"), event.get("path"))
            indexed = self.key_index.get(key)
            if (indexed is not None and indexed[0] == event.get("_seq")
                    and indexed[1] is event):
                del self.key_index[key]

    def clear(self):
        self.buffer.clear()
        self.key_index.clear()

    @staticmethod
    def public_event(event):
        result = dict(event)
        result.pop("_dedupe_at", None)
        return _compact_public_event(_json_safe(result))


class _CallbackGuard(object):
    def __init__(self, listener_token, subscription_token):
        self.listener_token = listener_token
        self.subscription_token = subscription_token
        self.active = True


class _CallbackHandle(object):
    def __init__(self, kind, owner, event_types, callback, guard):
        self.kind = kind
        self.owner = owner
        self.event_types = event_types
        self.callback = callback
        self.guard = guard


class EventState(object):
    """进程级 collector、subscription、generation 与 callback registrations。"""

    def __init__(self, collector=None):
        self.collector = collector or EventCollector()
        self.subscription = set()
        self.generation = 0
        self.callback_handles = []
        self.callback_registrations = self.callback_handles
        self.callbacks = []
        self.warnings = []
        self.attached = False
        self._listener_epoch = 0
        self._active_listener_token = None
        self._active_subscription_token = object()
        self._callback_guards = []

    def _warning(self, event_type, code, reason):
        warning = {
            "event_type": str(event_type),
            "code": str(code),
            "reason": str(reason),
        }
        if warning not in self.warnings:
            self.warnings.append(warning)
        return warning

    def append(self, event_type, path=None, payload=None,
               timestamp=None, now=None):
        return self.collector.append(
            event_type, path=path, payload=payload,
            timestamp=timestamp, now=now)

    def subscribe(self, types=None):
        normalized, error = normalize_event_types(types, none_means_all=True)
        if error is not None:
            return error
        new_subscription = set(normalized)
        if new_subscription != self.subscription:
            self.subscription = new_subscription
            self.generation += 1
        self._rotate_subscription_token()
        return {
            "status": "success",
            "subscription": _ordered_types(self.subscription),
            "generation": self.generation,
            "warnings": list(self.warnings),
        }

    def unsubscribe(self, types=None):
        normalized, error = normalize_event_types(types, none_means_all=False)
        if error is not None:
            return error
        if types is None:
            new_subscription = set()
        else:
            new_subscription = self.subscription - set(normalized)
        if new_subscription != self.subscription:
            self.subscription = new_subscription
            self.generation += 1
        self._rotate_subscription_token()
        return {
            "status": "success",
            "subscription": _ordered_types(self.subscription),
            "generation": self.generation,
            "warnings": list(self.warnings),
        }

    def _rotate_subscription_token(self):
        self._active_subscription_token = object()
        for guard in self._callback_guards:
            guard.subscription_token = self._active_subscription_token

    def _new_callback_guard(self):
        guard = _CallbackGuard(
            self._active_listener_token,
            self._active_subscription_token,
        )
        self._callback_guards.append(guard)
        return guard

    def _callback_is_active(self, guard):
        return bool(
            guard is not None
            and guard.active
            and self.attached
            and guard.listener_token is self._active_listener_token
            and guard.subscription_token is self._active_subscription_token
        )

    def _invalidate_listener(self):
        """在调用 Houdini remove 前先切断所有旧闭包的写入资格。"""
        self.attached = False
        self._listener_epoch += 1
        self._active_listener_token = None
        for guard in self._callback_guards:
            guard.active = False
        self._callback_guards[:] = []

    def _metadata(self, events=None, remaining=None, next_cursor=None):
        payload = {
            "events": list(events or []),
            "count": len(events or []),
            "remaining": int(remaining if remaining is not None else 0),
            "next_cursor": next_cursor,
            "generation": self.generation,
            "dropped_total": self.collector.dropped_total,
            "warnings": list(self.warnings),
        }
        return payload

    def drain(self, types=None, limit=DEFAULT_LIMIT, cursor=None,
              response_cap=None):
        normalized, error = normalize_event_types(
            self.subscription if types is None else types,
            none_means_all=False)
        if error is not None:
            return self._metadata()

        if response_cap is None and _common is not None:
            response_cap = getattr(_common, "apply_response_cap", None)

        limit = _clamp_limit(limit)
        if cursor is None:
            after_seq = 0
            snapshot_seq = self.collector.max_sequence
        else:
            try:
                cursor_generation, after_seq, snapshot_seq = _decode_cursor(cursor)
            except ValueError as exc:
                result = self._metadata(remaining=len(self.collector.matching(
                    normalized, after_seq=0,
                    snapshot_seq=self.collector.max_sequence)))
                result.update({
                    "status": "error",
                    "error": "invalid_cursor",
                    "message": str(exc),
                })
                return result
            if cursor_generation != self.generation:
                result = self._metadata(remaining=len(self.collector.matching(
                    normalized, after_seq=0,
                    snapshot_seq=self.collector.max_sequence)))
                result.update({
                    "status": "error",
                    "error": "stale_cursor",
                    "message": "cursor subscription generation is stale",
                })
                return result

        eligible = self.collector.matching(
            normalized, after_seq=after_seq, snapshot_seq=snapshot_seq)
        selected = eligible[:limit]
        result = self._metadata(
            events=[self.collector.public_event(event) for event in selected])
        if callable(response_cap):
            capped = response_cap(result)
            if isinstance(capped, dict):
                result = capped

        returned_events = result.get("events")
        if not isinstance(returned_events, list):
            returned_events = []
            result["events"] = returned_events
        returned_count = min(len(returned_events), len(selected))
        if returned_count < len(returned_events):
            result["events"] = returned_events[:returned_count]
            returned_events = result["events"]
        self.collector.remove_objects(selected[:returned_count])

        next_after = after_seq
        if returned_count:
            next_after = int(selected[returned_count - 1].get("_seq", after_seq))
        remaining_snapshot = self.collector.matching(
            normalized, after_seq=next_after, snapshot_seq=snapshot_seq)
        next_cursor = None
        if remaining_snapshot:
            next_cursor = _encode_cursor(
                self.generation, next_after, snapshot_seq)

        remaining_all = len(self.collector.matching(
            normalized, after_seq=0, snapshot_seq=self.collector.max_sequence))
        result["count"] = len(result.get("events") or [])
        result["remaining"] = remaining_all
        result["next_cursor"] = next_cursor
        result["generation"] = self.generation
        result["dropped_total"] = self.collector.dropped_total
        result["warnings"] = list(self.warnings)
        return result

    def _register_handle(self, kind, owner, event_types, callback, guard):
        handle = _CallbackHandle(kind, owner, event_types, callback, guard)
        self.callback_handles.append(handle)
        self.callbacks.append(callback)

    def _register_hip_callback(self, hou_module):
        hip_file = getattr(hou_module, "hipFile", None)
        add = getattr(hip_file, "addEventCallback", None)
        event_enum = getattr(getattr(hou_module, "hipFileEventType", None),
                             "AfterSave", None)
        if not callable(add):
            self._warning("scene_saved", "unsupported_api",
                          "hou.hipFile.addEventCallback is unavailable")
            return
        if event_enum is None:
            self._warning("scene_saved", "unsupported_enum",
                          "hou.hipFileEventType.AfterSave is unavailable")
            return

        guard = self._new_callback_guard()

        def callback(event_type):
            if not self._callback_is_active(guard):
                return
            try:
                if event_type != event_enum:
                    return
                path = ""
                try:
                    path = hou_module.hipFile.path() or ""
                except Exception:
                    pass
                self.append("scene_saved", path=path, payload={"path": path})
            except Exception as exc:
                self._warning("scene_saved", "callback_error", exc)

        try:
            add(callback)
        except Exception as exc:
            self._warning("scene_saved", "registration_error", exc)
            return
        self._register_handle("hip", hip_file, None, callback, guard)

    @staticmethod
    def _safe_node_value(node, method_name):
        try:
            method = getattr(node, method_name, None)
            return method() if callable(method) else None
        except Exception:
            return None

    def _register_node_callback(self, hou_module):
        obj = None
        try:
            node_function = getattr(hou_module, "node", None)
            obj = node_function("/obj") if callable(node_function) else None
        except Exception as exc:
            self._warning("node_created", "unsupported_api", exc)
            return

        op_node_type = getattr(hou_module, "OpNode", None)
        if obj is None or op_node_type is None or not isinstance(obj, op_node_type):
            self._warning("node_created", "unsupported_api",
                          "hou.node('/obj') is not a hou.OpNode")
            self._warning("node_deleted", "unsupported_api",
                          "hou.node('/obj') is not a hou.OpNode")
            return

        add = getattr(obj, "addEventCallback", None)
        if not callable(add):
            self._warning("node_created", "unsupported_api",
                          "hou.OpNode.addEventCallback is unavailable")
            self._warning("node_deleted", "unsupported_api",
                          "hou.OpNode.addEventCallback is unavailable")
            return

        node_event_enum = getattr(hou_module, "nodeEventType", None)
        created_enum = getattr(node_event_enum, "ChildCreated", None)
        deleted_enum = getattr(node_event_enum, "ChildDeleted", None)
        event_pairs = []
        if created_enum is None:
            self._warning("node_created", "unsupported_enum",
                          "hou.nodeEventType.ChildCreated is unavailable")
        else:
            event_pairs.append(("node_created", created_enum))
        if deleted_enum is None:
            self._warning("node_deleted", "unsupported_enum",
                          "hou.nodeEventType.ChildDeleted is unavailable")
        else:
            event_pairs.append(("node_deleted", deleted_enum))
        if not event_pairs:
            return

        event_types = tuple(pair[1] for pair in event_pairs)
        enum_to_name = dict((pair[1], pair[0]) for pair in event_pairs)

        guard = self._new_callback_guard()

        def callback(node, event_type, **kwargs):
            if not self._callback_is_active(guard):
                return
            event_name = enum_to_name.get(event_type)
            if event_name is None:
                return
            try:
                child = kwargs.get("child_node")
                child_path = kwargs.get("child_path") or kwargs.get("path")
                child_type = kwargs.get("child_type")
                if not child_path and child is not None:
                    child_path = self._safe_node_value(child, "path")
                if not child_type and child is not None:
                    child_type_obj = self._safe_node_value(child, "type")
                    child_type = self._safe_node_value(child_type_obj, "name")
                parent_path = self._safe_node_value(node, "path")
                payload = {"parent_path": parent_path}
                if child_path:
                    payload["child_path"] = child_path
                if child_type:
                    payload["child_type"] = child_type
                self.append(
                    event_name,
                    path=child_path or parent_path,
                    payload=payload,
                )
            except Exception as exc:
                self._warning(event_name, "callback_error", exc)

        try:
            add(event_types, callback)
        except Exception as exc:
            self._warning("node_created", "registration_error", exc)
            return
        self._register_handle("node", obj, event_types, callback, guard)

    def _register_playbar_callback(self, hou_module):
        playbar = getattr(hou_module, "playbar", None)
        add = getattr(playbar, "addEventCallback", None)
        event_enum = getattr(getattr(hou_module, "playbarEvent", None),
                             "FrameChanged", None)
        if not callable(add):
            self._warning("frame_changed", "unsupported_api",
                          "hou.playbar.addEventCallback is unavailable")
            return
        if event_enum is None:
            self._warning("frame_changed", "unsupported_enum",
                          "hou.playbarEvent.FrameChanged is unavailable")
            return

        guard = self._new_callback_guard()

        def callback(event_type, frame):
            if not self._callback_is_active(guard):
                return
            try:
                if event_type != event_enum:
                    return
                self.append(
                    "frame_changed",
                    path="playbar",
                    payload={"frame": frame},
                )
            except Exception as exc:
                self._warning("frame_changed", "callback_error", exc)

        try:
            add(callback)
        except Exception as exc:
            self._warning("frame_changed", "registration_error", exc)
            return
        self._register_handle("playbar", playbar, None, callback, guard)

    def attach_callbacks(self, hou_module):
        """按正式 HOM API 注册 callback；重复 attach 不重复注册。"""
        if self.attached:
            return self
        for guard in self._callback_guards:
            guard.active = False
        self._callback_guards[:] = []
        self._listener_epoch += 1
        self._active_listener_token = object()
        self.attached = True
        self._register_hip_callback(hou_module)
        self._register_node_callback(hou_module)
        self._register_playbar_callback(hou_module)
        return self

    def detach_callbacks(self):
        """使用原 owner、event_types、callback 对称 remove，失败继续清理。"""
        self._invalidate_listener()
        for handle in list(self.callback_handles):
            try:
                remove = getattr(handle.owner, "removeEventCallback", None)
                if not callable(remove):
                    raise AttributeError("removeEventCallback is unavailable")
                if handle.kind == "node":
                    remove(handle.event_types, handle.callback)
                else:
                    remove(handle.callback)
            except Exception as exc:
                self._warning("callback_cleanup", "remove_error", exc)
        self.callback_handles[:] = []
        self.callbacks[:] = []
        self.collector.clear()
        self.attached = False
        return self


PROCESS_EVENT_STATE = EventState()


def attach_callbacks(hou_module):
    return PROCESS_EVENT_STATE.attach_callbacks(hou_module)


def detach_callbacks():
    return PROCESS_EVENT_STATE.detach_callbacks()


def get_pending_events(limit=DEFAULT_LIMIT, cursor=None):
    return PROCESS_EVENT_STATE.drain(limit=limit, cursor=cursor)


def subscribe_events(types=None):
    return PROCESS_EVENT_STATE.subscribe(types)


def unsubscribe_events(types=None):
    return PROCESS_EVENT_STATE.unsubscribe(types)
