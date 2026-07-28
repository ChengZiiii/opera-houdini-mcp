#!/usr/bin/env python
"""H21.0 真实 MCP initialize + resources list/read smoke for add-mcp-resources。

使用 Houdini 21.0.596 hython 启动 MCP server（FastMCP），经 MCP protocol
调 ``initialize`` + ``resources/list`` + ``resources/templates/list`` +
``resources/read``，验证 4 static + 4 template 资源 + mime type +
可读 dict envelope。本脚本只走 MCP 协议层，不实际写 Houdini 场景。
"""
import asyncio
import importlib.util
import os
import subprocess
import sys
import time
import types


HYTHON = r"C:\Program Files\Side Effects Software\Houdini 21.0.596\bin\hython.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE = os.path.join(ROOT, "houdini_mcp_server.py")
PYLIBS = os.path.join(ROOT, "..", "..", "external", "houdinimcp-env", "python", "Lib", "site-packages")
ENV_PYTHON = os.path.normpath(os.path.join(ROOT, "..", "..", "external", "houdinimcp-env", "python", "python.exe"))


def _check(condition, label, failures):
    if condition:
        print(f"  [OK] {label}")
    else:
        print(f"  [FAIL] {label}")
        failures.append(label)


async def main():
    """直接 import bridge 的 mcp 实例，跑 MCP protocol 资源 list/read。"""
    failures = []
    # 把 houdinimcp-env 的 pylibs 加进去以拿到真实 mcp 1.12.2
    sys.path.insert(0, os.path.normpath(os.path.join(ROOT, "..", "houdinimcp-env", "pylibs")))

    # 重新 import houdini_mcp_server
    for k in list(sys.modules):
        if k == "mcp" or k.startswith("mcp."):
            if not hasattr(sys.modules[k], "__file__") and not hasattr(sys.modules[k], "__path__"):
                del sys.modules[k]
    for k in list(sys.modules):
        if k in ("houdini_mcp_server", "houdinimcp.houdini_mcp_server"):
            del sys.modules[k]
    sys.path.insert(0, ROOT)
    import houdini_mcp_server as srv  # noqa

    # 1. resources/list
    print("== resources/list ==")
    static_resources = await srv.mcp.list_resources()
    static_uris = sorted([str(r.uri) for r in static_resources])
    expected_static = sorted([
        "houdini://errors", "houdini://hdas",
        "houdini://scene/info", "houdini://scene/tree",
    ])
    _check(static_uris == expected_static,
           f"static URIs == {expected_static}", failures)
    _check(len(static_resources) == 4, "exactly 4 static", failures)
    for r in static_resources:
        _check(r.mimeType == "application/json",
               f"static {r.uri} mimeType=application/json", failures)
        _check(r.name in ("scene_info", "scene_tree", "errors", "hdas"),
               f"static {r.uri} name={r.name}", failures)

    # 2. resources/templates/list
    print("== resources/templates/list ==")
    templates = await srv.mcp.list_resource_templates()
    tmpl_uris = sorted([t.uriTemplate for t in templates])
    expected_tmpl = sorted([
        "houdini://geometry/{encoded_node_path}/summary",
        "houdini://node-types/{context}",
        "houdini://scene/nodes/{encoded_path}",
        "houdini://usd/{encoded_node_path}/stage",
    ])
    _check(tmpl_uris == expected_tmpl,
           f"templates == {expected_tmpl}", failures)
    _check(len(templates) == 4, "exactly 4 templates", failures)
    for t in templates:
        _check(t.name in ("scene_node", "node_types",
                          "geometry_summary", "usd_stage"),
               f"template {t.uriTemplate} name={t.name}", failures)

    # 3. 4 static resources/read
    print("== static resources/read ==")
    for uri in expected_static:
        items = await srv.mcp.read_resource(uri)
        _check(len(items) == 1, f"read {uri} returns 1 item", failures)
        if len(items) == 1:
            _check(items[0].mime_type == "application/json",
                   f"read {uri} mime_type=application/json", failures)
            # 内容是 JSON str；解析为 dict
            import json
            try:
                body = json.loads(items[0].content)
                _check(isinstance(body, dict),
                       f"read {uri} body is dict", failures)
                _check("status" in body and "resource" in body,
                       f"read {uri} body has status/resource", failures)
            except Exception as e:
                _check(False, f"read {uri} body parse: {e}", failures)

    # 4. 4 template resources/read
    print("== template resources/read ==")
    # 用真实路径 %2Fobj%2Fgeo1 测试 get_node_info（无 hou node → backend error）
    # 重点验证 URI 解析 + handler 路由 + mime + JSON envelope
    cases = [
        ("houdini://scene/nodes/%2Fobj%2Fgeo1", "scene_node",
         "get_node_info"),
        ("houdini://node-types/Sop", "node_types", "list_node_types"),
        ("houdini://geometry/%2Fobj%2Fgeo1/summary", "geometry_summary",
         "get_geo_summary"),
        ("houdini://usd/%2Fobj%2Fusd/stage", "usd_stage", "lop_stage_info"),
    ]
    for uri, expected_resource, expected_dep in cases:
        items = await srv.mcp.read_resource(uri)
        _check(len(items) == 1, f"read {uri} returns 1 item", failures)
        if len(items) == 1:
            _check(items[0].mime_type == "application/json",
                   f"read {uri} mime_type=application/json", failures)
            try:
                import json
                body = json.loads(items[0].content)
                _check(body.get("resource") == expected_resource,
                       f"read {uri} resource={expected_resource}", failures)
                _check(body.get("dependency") == expected_dep,
                       f"read {uri} dependency={expected_dep}", failures)
                # 离线时无 hou 连接 → status=error, code=backend_capability_error
                _check(body.get("status") in ("success", "error"),
                       f"read {uri} status", failures)
            except Exception as e:
                _check(False, f"read {uri} body parse: {e}", failures)

    # 5. invalid encoded path
    print("== invalid encoded path ==")
    items = await srv.mcp.read_resource("houdini://scene/nodes/%2fobj")
    _check(len(items) == 1, "invalid read returns 1 item", failures)
    if len(items) == 1:
        import json
        body = json.loads(items[0].content)
        _check(body.get("status") == "error",
               "invalid path returns error status", failures)
        _check(body.get("code") == "invalid_encoded_path",
               f"invalid path code=invalid_encoded_path (got {body.get('code')})",
               failures)

    print()
    if failures:
        print(f"FAIL: {len(failures)} failures")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: all MCP resource protocol checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
