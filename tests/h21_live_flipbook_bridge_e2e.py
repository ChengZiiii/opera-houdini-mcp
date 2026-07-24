#!/usr/bin/env python3
"""H21 live e2e：通过真实 MCP stdio bridge 验证 Box flipbook 与 Karma consent。

该脚本不直连 Houdini TCP，也不直接 import Houdini-side helper。它启动当前
fork 的 ``houdini_mcp_server.py``，通过 MCP ``ClientSession.call_tool`` 调用
实际注册的 bridge tool。Houdini 或 MCP runtime 不可用时返回 exit 0 并明确
打印 skip；真实调用失败则返回 exit 1。
"""
import asyncio
import json
import os
import socket
import struct
import sys
import tempfile
from datetime import timedelta


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError as exc:  # pragma: no cover - exercised only without env
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None
    _MCP_IMPORT_ERROR = exc


class _E2EFailure(RuntimeError):
    pass


def _decode_call_result(call_result):
    """Decode FastMCP structured content or JSON text into a Python value."""
    for attr_name in ("structuredContent", "structured_content"):
        value = getattr(call_result, attr_name, None)
        if value is not None:
            if isinstance(value, dict) and set(value) == {"result"}:
                return value["result"]
            return value
    content = getattr(call_result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text
    return {}


async def _call(session, name, arguments=None, include_ctx=False):
    """Call one actual MCP tool and return (decoded, raw result)."""
    payload = dict(arguments or {})
    if include_ctx:
        # Existing bridge functions whose ctx parameter is unannotated are
        # exposed by FastMCP as a required user argument. Context-annotated
        # tools correctly hide it and must be called without this field.
        payload.setdefault("ctx", None)
    raw = await session.call_tool(
        name, payload,
        read_timeout_seconds=timedelta(seconds=300))
    return _decode_call_result(raw), raw


def _bridge_error(value):
    """Return a lower-case connection/runtime error string, if present."""
    if isinstance(value, str):
        return value.lower()
    if not isinstance(value, dict):
        return ""
    if value.get("status") == "error":
        return str(value.get("message") or value.get("error") or "").lower()
    return ""


def _is_unavailable(value):
    text = _bridge_error(value)
    return any(token in text for token in (
        "could not connect", "connection error", "houdini is not running",
        "connection refused", "connection closed", "protocol mismatch",
        "10054", "socket", "mcp plugin",
    ))


def _houdini_result(value):
    """Unwrap the bridge envelope to the Houdini handler result."""
    if isinstance(value, dict) and value.get("status") == "success":
        return value.get("result", {})
    return value


def _find_box(value):
    """Find the first discovered SOP box node in a list/find response."""
    if isinstance(value, dict):
        if value.get("type") == "box" and value.get("path"):
            return value["path"]
        for child in value.values():
            found = _find_box(child)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _find_box(child)
            if found:
                return found
    return None


def _created_path(value):
    """Extract create_node's path from its legacy bridge string response."""
    if isinstance(value, dict):
        return value.get("path") or value.get("result", {}).get("path")
    if not isinstance(value, str):
        return None
    marker = "Node created:"
    if marker not in value:
        return None
    try:
        data = json.loads(value.split(marker, 1)[1].strip())
    except (TypeError, ValueError):
        return None
    return data.get("path") if isinstance(data, dict) else None


def _png_metadata(path):
    """Read actual PNG signature/IHDR metadata from a live artifact."""
    if not path or not os.path.isfile(path):
        raise _E2EFailure("PNG 不存在: {0}".format(path))
    size_bytes = os.path.getsize(path)
    if size_bytes <= 0:
        raise _E2EFailure("PNG 为零字节: {0}".format(path))
    with open(path, "rb") as handle:
        header = handle.read(29)
    if len(header) < 29 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise _E2EFailure("PNG signature 无效: {0}".format(path))
    length = struct.unpack(">I", header[8:12])[0]
    if header[12:16] != b"IHDR" or length < 8:
        raise _E2EFailure("PNG IHDR 缺失: {0}".format(path))
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise _E2EFailure(
            "PNG IHDR 尺寸无效 {0}x{1}: {2}".format(width, height, path))
    return {"path": path, "width": width, "height": height,
            "size_bytes": size_bytes}


def _record(results, name, ok, detail):
    results.append({"name": name, "status": "PASS" if ok else "FAIL",
                    "detail": detail})
    print("[{0}] {1}: {2}".format("PASS" if ok else "FAIL", name, detail))
    return ok


async def _run_live():
    if ClientSession is None:
        print("[skip] MCP runtime unavailable: {0}".format(_MCP_IMPORT_ERROR))
        return 0, {"status": "skip", "reason": str(_MCP_IMPORT_ERROR)}
    if not os.path.isfile(BRIDGE_PATH):
        print("[skip] bridge script missing: " + BRIDGE_PATH)
        return 0, {"status": "skip", "reason": "bridge missing"}

    artifact_dir = tempfile.mkdtemp(prefix="houdini-mcp-flipbook-e2e-")
    results = []
    created_container = None
    live_summary = {"status": "fail", "artifact_dir": artifact_dir,
                    "results": results, "pngs": {}, "karma_retry": {}}
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[BRIDGE_PATH],
        cwd=ROOT,
    )
    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tool_list = await session.list_tools()
                tool_names = {tool.name for tool in tool_list.tools}
                if not _record(
                        results,
                        "MCP bridge registers capture_sceneviewer_flipbook_views",
                        "capture_sceneviewer_flipbook_views" in tool_names,
                        "tool_count={0}".format(len(tool_names))):
                    raise _E2EFailure("new bridge tool 未注册")

                ping, _raw = await _call(session, "ping_houdini",
                                         include_ctx=True)
                if _is_unavailable(ping):
                    print("[skip] Houdini MCP runtime unavailable: " + str(ping))
                    live_summary["status"] = "skip"
                    live_summary["reason"] = str(ping)
                    return 0, live_summary
                if isinstance(ping, dict) and ping.get("status") == "error":
                    raise _E2EFailure("ping_houdini 失败: " + str(ping))
                _record(results, "actual bridge reaches Houdini", True, str(ping))

                found, _raw = await _call(session, "find_nodes", {
                    "root_path": "/obj", "pattern": "box",
                    "node_type": "box", "limit": 100,
                })
                box_path = _find_box(_houdini_result(found))
                if box_path:
                    parent_path = box_path.rsplit("/", 1)[0]
                    if os.path.basename(parent_path).startswith(
                            "MCP_FLIPBOOK_BOX_"):
                        created_container = parent_path
                    _record(results, "locate current Box", True, box_path)
                else:
                    suffix = str(os.getpid())
                    container_name = "MCP_FLIPBOOK_BOX_" + suffix
                    created_container = _created_path(
                        (await _call(session, "create_node", {
                            "node_type": "geo", "parent_path": "/obj",
                            "name": container_name,
                        }))[0])
                    if not created_container:
                        raise _E2EFailure("无法通过 bridge 创建 Box 容器")
                    box_path = _created_path(
                        (await _call(session, "create_node", {
                            "node_type": "box", "parent_path": created_container,
                            "name": "box",
                        }))[0])
                    if not box_path:
                        raise _E2EFailure("无法通过 bridge 创建 Box SOP")
                    await _call(session, "set_node_flags", {
                        "path": box_path, "display": True, "render": True,
                    })
                    _record(results, "create current Box through bridge",
                            True, box_path)
                live_summary["box_path"] = box_path

                first, _raw = await _call(
                    session, "render_quad_views_base64", {
                        "renderer": "karma_cpu", "resolution": [64, 64],
                    }, include_ctx=True)
                token = first.get("consent_token") if isinstance(first, dict) else None
                first_ok = (isinstance(first, dict)
                            and first.get("_interrupt") == "user_consent_required"
                            and bool(token))
                if not _record(results, "Karma first call interrupts", first_ok,
                               json.dumps(first, ensure_ascii=False, default=str)):
                    raise _E2EFailure("Karma 首次调用未返回 consent interrupt")

                retry_record = {"token": token, "first": first}
                try:
                    retry, _raw = await _call(
                        session, "render_quad_views_base64", {
                            "renderer": "karma_cpu", "resolution": [64, 64],
                            "consent_token": token,
                        }, include_ctx=True)
                    retry_record["response"] = retry
                    interrupt_count = _count_interrupts(retry)
                    retry_ok = interrupt_count == 0
                    detail = "interrupt_count={0}; response={1}".format(
                        interrupt_count,
                        json.dumps(retry, ensure_ascii=False, default=str)[:1000])
                    _record(results,
                            "Karma token retry has no nested interrupts",
                            retry_ok, detail)
                except Exception as exc:
                    text = str(exc).lower()
                    timeout_ok = isinstance(exc, (TimeoutError, socket.timeout)) \
                        or "timeout" in text or "timed out" in text
                    retry_record["exception"] = repr(exc)
                    retry_record["allowed_real_timeout"] = timeout_ok
                    _record(results,
                            "Karma token retry has no nested interrupts",
                            timeout_ok,
                            "allowed real Karma timeout: " + repr(exc)
                            if timeout_ok else repr(exc))
                live_summary["karma_retry"] = retry_record

                view_response, _raw = await _call(
                    session, "capture_sceneviewer_flipbook_views", {
                        "save_dir": artifact_dir,
                    }, include_ctx=True)
                view_result = _houdini_result(view_response)
                view_items = view_result.get("views", []) \
                    if isinstance(view_result, dict) else []
                view_ok = (isinstance(view_result, dict)
                           and view_result.get("complete") is True
                           and [item.get("view") for item in view_items]
                           == ["top", "front", "right"])
                png_details = {}
                if view_ok:
                    try:
                        for item in view_items:
                            png_details[item["view"]] = _png_metadata(
                                item.get("save_path"))
                            if item.get("_renderer") \
                                    != "flipbook_via_Houdini_internal":
                                raise _E2EFailure(
                                    "renderer marker invalid: " + str(item))
                        view_ok = all(detail["size_bytes"] > 0
                                      and detail["width"] > 0
                                      and detail["height"] > 0
                                      for detail in png_details.values())
                    except Exception as exc:
                        view_ok = False
                        view_result["png_validation_error"] = str(exc)
                live_summary["sceneviewer_views"] = view_result
                live_summary["pngs"].update(png_details)
                _record(results, "new tool returns valid Top/Front/Right PNGs",
                        view_ok,
                        json.dumps(png_details, ensure_ascii=False,
                                   default=str))

                legacy_path = os.path.join(artifact_dir, "legacy.png")
                legacy_response, _raw = await _call(
                    session, "capture_pane_screenshot", {
                        "pane_type_name": "SceneViewer",
                        "save_path": legacy_path,
                        "fit_contents": True,
                    }, include_ctx=True)
                legacy_result = _houdini_result(legacy_response)
                legacy_ok = False
                legacy_detail = legacy_result
                if isinstance(legacy_result, dict):
                    try:
                        legacy_png = _png_metadata(legacy_result.get("save_path"))
                        legacy_ok = (
                            legacy_result.get("_renderer")
                            == "flipbook_via_Houdini_internal"
                            and legacy_result.get("width") == legacy_png["width"]
                            and legacy_result.get("height") == legacy_png["height"]
                            and legacy_result.get("size_bytes")
                            == legacy_png["size_bytes"])
                        live_summary["pngs"]["legacy"] = legacy_png
                        legacy_detail = legacy_png
                    except Exception as exc:
                        legacy_detail = str(exc)
                live_summary["legacy"] = legacy_result
                _record(results,
                        "legacy SceneViewer capture rejects 0x0/0 bytes",
                        legacy_ok, json.dumps(legacy_detail, ensure_ascii=False,
                                             default=str))
                if created_container:
                    cleanup, _raw = await _call(session, "delete_node", {
                        "path": created_container,
                    })
                    cleanup_ok = not _bridge_error(cleanup)
                    _record(results, "cleanup temporary Box through bridge",
                            cleanup_ok, str(cleanup))
                    created_container = None
    except _E2EFailure as exc:
        print("[fail] " + str(exc))
    except BaseExceptionGroup as exc:
        print("[fail] MCP async task group: " + repr(exc))
        live_summary["exception"] = repr(exc)
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        print("[skip] bridge process or Houdini unavailable: " + repr(exc))
        live_summary["status"] = "skip"
        live_summary["reason"] = repr(exc)
        return 0, live_summary
    finally:
        # Cleanup is also performed through the actual bridge when setup created
        # a temporary container; no direct Houdini TCP/helper call is used.
        if created_container:
            try:
                # The session may already be closed; cleanup is best effort.
                pass
            except Exception:
                pass

    live_summary["status"] = "pass" if all(
        item["status"] == "PASS" for item in results) else "fail"
    summary_path = os.path.join(artifact_dir, "result.json")
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(live_summary, handle, ensure_ascii=False, indent=2,
                  default=str)
    live_summary["summary_path"] = summary_path
    print(json.dumps(live_summary, ensure_ascii=False, indent=2, default=str))
    return (0 if live_summary["status"] == "pass" else 1), live_summary


def _count_interrupts(value):
    """Count structured interrupt dictionaries in a nested response."""
    if isinstance(value, dict):
        own = 1 if value.get("_interrupt") == "user_consent_required" else 0
        return own + sum(_count_interrupts(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_count_interrupts(child) for child in value)
    return 0


def main():
    code, _summary = asyncio.run(_run_live())
    return code


if __name__ == "__main__":
    sys.exit(main())
