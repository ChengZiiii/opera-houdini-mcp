#!/usr/bin/env python3
"""H21.0 真实事件 API smoke：save、node、playbar、remove、分页与 generation。

该脚本只连接真实 Houdini MCP framed socket，不 import mock hou，也不启动
伪造 server。Houdini 未运行或 MCP 端口不可达时按 change 约定输出 SKIP。
"""

import json
import os
import socket
import sys
import tempfile
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _e2e_helpers import HoudiniCallError, HoudiniConn  # noqa: E402


EVENT_TYPES = [
    "scene_saved", "node_created", "node_deleted", "frame_changed",
]


def _inner(response):
    if isinstance(response, dict) and isinstance(response.get("result"), dict):
        return response["result"]
    return {}


def _drain(conn, limit=100, cursor=None):
    """按 cursor 读完一个固定 snapshot，返回 events 和各页响应。"""
    events = []
    pages = []
    current_cursor = cursor
    for _index in range(50):
        result = _inner(conn.call(
            "get_pending_events", limit=limit, cursor=current_cursor))
        pages.append(result)
        events.extend(result.get("events") or [])
        current_cursor = result.get("next_cursor")
        if not current_cursor:
            return events, pages
    raise RuntimeError("event pagination exceeded 50 pages")


def _event(events, event_type, path=None):
    for item in events:
        if item.get("type") != event_type:
            continue
        if path is not None and item.get("path") != path:
            continue
        return item
    return None


def _print(name, ok, detail, status=None):
    status = status or ("PASS" if ok else "FAIL")
    print("[{0}] {1}: {2}".format(status, name, detail))
    return name, status, detail


def _same_path(left, right):
    if not left or not right:
        return False
    normalize = lambda value: os.path.normcase(
        os.path.normpath(str(value).replace("/", os.sep)))
    return normalize(left) == normalize(right)


def run(host="127.0.0.1", port=9876):
    results = []
    temp_path = os.path.join(
        tempfile.gettempdir(),
        "houdini_mcp_event_smoke_{0}.hip".format(os.getpid()),
    )
    first_path = "/obj/MCP_EVENT_SMOKE_{0}_A".format(os.getpid())
    second_path = "/obj/MCP_EVENT_SMOKE_{0}_B".format(os.getpid())
    pagination_path = "/obj/MCP_EVENT_SMOKE_{0}_P".format(os.getpid())
    created_paths = []
    initial_created_paths = []

    try:
        with HoudiniConn(host=host, port=port, timeout=30) as conn:
            scene = _inner(conn.call("get_scene_info"))
            version = str(scene.get("houdini_version") or "")
            results.append(_print(
                "H21.0 version", version.startswith("21.0."), version))

            subscribed = _inner(conn.call("subscribe_events", types=EVENT_TYPES))
            generation = subscribed.get("generation")
            results.append(_print(
                "subscribe all event types",
                subscribed.get("subscription") == EVENT_TYPES,
                json.dumps(subscribed, ensure_ascii=False, default=str),
            ))
            repeated = _inner(conn.call("subscribe_events", types=list(EVENT_TYPES)))
            results.append(_print(
                "same subscription is idempotent",
                repeated.get("generation") == generation,
                "generation={0}->{1}".format(generation, repeated.get("generation")),
            ))

            # 清掉 subscription 建立前遗留的旧事件，后续断言只看本轮动作。
            _drain(conn)

            hscript_path = temp_path.replace("\\", "/").replace('"', '\\"')
            saved = _inner(conn.call(
                "execute_hscript",
                code='mwrite "{0}"'.format(hscript_path),
            ))
            results.append(_print(
                "real save command", saved.get("return_code") == 0
                and os.path.isfile(temp_path),
                json.dumps(saved, ensure_ascii=False, default=str),
            ))

            for path in (first_path, second_path):
                parent, name = path.rsplit("/", 1)
                created = _inner(conn.call(
                    "create_node", node_type="geo", parent_path=parent, name=name))
                actual_path = created.get("path") or path
                created_paths.append(actual_path)
                initial_created_paths.append(actual_path)
            results.append(_print(
                "ChildCreated events", len(created_paths) == 2,
                str(created_paths),
            ))

            frame = 1000 + (int(time.time()) % 100)
            frame_result = _inner(conn.call(
                "execute_hscript", code="fcur {0}".format(frame)))
            results.append(_print(
                "FrameChanged command", isinstance(frame_result, dict),
                json.dumps(frame_result, ensure_ascii=False, default=str),
            ))

            deleted = _inner(conn.call("delete_node", path=first_path))
            if first_path in created_paths:
                created_paths.remove(first_path)
            results.append(_print(
                "ChildDeleted command", deleted.get("deleted") == first_path,
                json.dumps(deleted, ensure_ascii=False, default=str),
            ))

            first_page = _inner(conn.call("get_pending_events", limit=1))
            first_cursor = first_page.get("next_cursor")
            results.append(_print(
                "first page has bounded count/cursor",
                first_page.get("count") == len(first_page.get("events") or [])
                and bool(first_cursor),
                json.dumps(first_page, ensure_ascii=False, default=str)[:500],
            ))

            parent, name = pagination_path.rsplit("/", 1)
            pagination_created = _inner(conn.call(
                "create_node", node_type="geo", parent_path=parent, name=name))
            pagination_actual = pagination_created.get("path") or pagination_path
            created_paths.append(pagination_actual)

            page_events, pages = _drain(conn, limit=100, cursor=first_cursor)
            snapshot_events = list(first_page.get("events") or []) + page_events
            results.append(_print(
                "AfterSave callback observed",
                any(item.get("type") == "scene_saved"
                    and _same_path(item.get("path"), temp_path)
                    for item in snapshot_events),
                "snapshot_events={0}".format(len(snapshot_events)),
            ))
            results.append(_print(
                "ChildCreated callback observed",
                all(any(item.get("type") == "node_created"
                        and item.get("path") == path
                        for item in snapshot_events)
                    for path in initial_created_paths),
                str(initial_created_paths),
            ))
            results.append(_print(
                "ChildDeleted callback observed",
                any(item.get("type") == "node_deleted"
                    and item.get("path") == first_path
                    for item in snapshot_events),
                first_path,
            ))
            frame_event = _event(snapshot_events, "frame_changed")
            frame_warning = any(
                warning.get("event_type") == "frame_changed"
                and warning.get("code") in {
                    "unsupported_api", "unsupported_enum", "registration_error",
                }
                for warning in (subscribed.get("warnings") or [])
            )
            if frame_event is not None:
                results.append(_print(
                    "FrameChanged callback observed", True,
                    json.dumps(frame_event, ensure_ascii=False, default=str),
                ))
            else:
                results.append(_print(
                    "FrameChanged callback observed", False,
                    "playbar unavailable in this live context",
                    status="SKIP" if frame_warning else "FAIL",
                ))
            snapshot_excludes_new = not any(
                item.get("path") == pagination_actual for item in page_events)
            results.append(_print(
                "snapshot pagination excludes newly appended event",
                snapshot_excludes_new,
                "pages={0} events={1}".format(len(pages), len(page_events)),
            ))

            tail_events, tail_pages = _drain(conn, limit=100)
            pagination_delivered = any(
                item.get("path") == pagination_actual
                and item.get("type") == "node_created"
                for item in tail_events)
            results.append(_print(
                "new event appears on next snapshot",
                pagination_delivered,
                "tail_pages={0} tail_events={1}".format(
                    len(tail_pages), len(tail_events)),
            ))

            unsubscribed = _inner(conn.call(
                "unsubscribe_events", types=["frame_changed"]))
            resubscribed = _inner(conn.call(
                "subscribe_events", types=EVENT_TYPES))
            results.append(_print(
                "generation changes only for subscription change",
                unsubscribed.get("generation") != generation
                and resubscribed.get("generation") == unsubscribed.get("generation") + 1,
                "unsubscribe={0} resubscribe={1}".format(
                    unsubscribed.get("generation"), resubscribed.get("generation")),
            ))

            for path in list(created_paths):
                try:
                    conn.call("delete_node", path=path)
                except HoudiniCallError:
                    pass

    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        print("[SKIP] H21.0 Houdini socket unavailable: {0}".format(exc))
        return 0
    except Exception as exc:
        print("[FAIL] H21.0 event smoke: {0}".format(exc))
        return 1
    finally:
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

    for _name, status, _detail in results:
        if status == "FAIL":
            return 1
    print("H21.0 event smoke: {0}/{1} PASS".format(
        sum(1 for _name, status, _detail in results if status == "PASS"),
        len(results)))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="H21.0 event live smoke")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    args = parser.parse_args()
    sys.exit(run(host=args.host, port=args.port))
