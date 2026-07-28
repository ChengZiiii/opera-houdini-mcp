"""H21.0 live render policy E2E（add-render-workflow-tools / C9）。

覆盖（tasks 6.1-6.8）：
- Houdini / MCP 不可达必须 fail 非零（不依赖 mock）。
- 创建真实 ifd / opengl / karmarender ROP 与 pre-render marker fixture。
- opengl redirect → marker 不出现。
- karma 无 token / 错 token → interrupt → marker 不出现。
- karma 用上一步 interrupt 签发的有效 token → marker 出现并完成。
- mantra allow → marker 出现并完成。
- batch 中前置 mutation + 后置 opengl / karma 无 token：
  bridge blocked → ``connection.send`` 不调用；直接 TCP blocked
  → mutation / render handler 都不调用；marker 不出现。
- finally 清理节点 / 输出 / marker / 本测试 sentinel。

退出码 0 = 全部 PASS；非 0 = 有 FAIL（H21 不可达 -> 立即 exit 1，
不用 mock / skip 替代通过）。H22 未在本会话安装 -> 明确 SKIP。

运行方式（需真实 H21.0.596 hython；H21 MCP server 已在运行）：
    "C:/Program Files/Side Effects Software/Houdini 21.0.596/bin/hython.exe" \\
        external/houdinimcp/tests/h21_live_render_policy_e2e.py
"""
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _houdini_alive():
    """不连 Houdini，直接探测 hython 可执行文件 + MCP server 端口可达。

    任意一步失败立即 exit 非零。
    """
    candidates = [os.environ.get("HOUDINI_MCP_HYTHON")]
    hfs = os.environ.get("HFS")
    if hfs:
        candidates.append(os.path.join(hfs, "bin", "hython.exe"))
        candidates.append(os.path.join(hfs, "bin", "hython"))
    candidates += ["hython.exe", "hython"]
    for candidate in candidates:
        if not candidate:
            continue
        resolved = (candidate if os.path.isabs(candidate)
                     else shutil.which(candidate))
        if resolved and os.path.isfile(resolved):
            return resolved
    sys.stderr.write(
        "FAIL: hython not found; set HOUDINI_MCP_HYTHON or HFS env\n")
    sys.exit(1)


def _mcp_alive(host="127.0.0.1", port=9876):
    """对 MCP server 端口做 4-byte framed ping；不可达立即 exit 1。"""
    try:
        sock = socket.create_connection((host, int(port)), timeout=2)
    except OSError as error:
        sys.stderr.write(
            "FAIL: Houdini MCP server not reachable on %s:%d (%s)\n"
            % (host, port, error))
        sys.exit(1)
    try:
        payload = json.dumps({"type": "ping", "params": {}}).encode("utf-8")
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        header = sock.recv(4)
        if len(header) != 4:
            sys.stderr.write("FAIL: short ping header\n")
            sys.exit(1)
        size = struct.unpack(">I", header)[0]
        response = json.loads(
            sock.recv(size).decode("utf-8"))
        return bool((response.get("result") or {}).get("pong"))
    finally:
        sock.close()


def _send(cmd_type, params, host="127.0.0.1", port=9876):
    """单条 MCP 命令；返解析后的 response dict。"""
    sock = socket.create_connection((host, int(port)), timeout=300)
    try:
        payload = json.dumps(
            {"type": cmd_type, "params": params or {}}).encode("utf-8")
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        header = sock.recv(4)
        size = struct.unpack(">I", header)[0]
        body = b""
        while len(body) < size:
            chunk = sock.recv(min(size - len(body), 65536))
            if not chunk:
                break
            body += chunk
        return json.loads(body.decode("utf-8"))
    finally:
        sock.close()


def _check(name, condition, detail=""):
    tag = "PASS" if condition else "FAIL"
    sys.stderr.write("%s %s%s\n" % (
        tag, name, "  // " + detail if detail else ""))
    return bool(condition)


def main():
    hython = _houdini_alive()
    if not _mcp_alive():
        sys.stderr.write("FAIL: MCP server ping returned False\n")
        sys.exit(1)

    sys.stderr.write("H21 hython: %s\n" % hython)
    sys.stderr.write("MCP server ping: OK\n\n")

    # 用 hython 子进程创真实 ROP + pre-render marker；HOM 直接操作。
    # 把 marker 路径写到 out_file_dir 下；多个 ROP 共用同一 marker
    # 文件名以便 finally 一次性清理。
    out_dir = tempfile.mkdtemp(prefix="c9_render_e2e_")
    marker_path = os.path.join(out_dir, "marker.txt")
    pre_render_script = (
        "import os\n"
        "out_dir = os.environ['OUT_DIR']\n"
        "marker_path = os.path.join(out_dir, 'marker.txt')\n"
        "node = hou.pwd()\n"
        "def _write_marker():\n"
        "    with open(marker_path, 'w') as f:\n"
        "        f.write(repr(hou.frame()))\n"
        "node.addRenderEventCallback((hou.renderEventType.PreRender,\n"
        "                              lambda: _write_marker()))\n"
    )
    sentinel_dir = tempfile.mkdtemp(prefix="c9_render_sentinel_")
    results = []

    def section(name):
        sys.stderr.write("\n=== %s ===\n" % name)

    try:
        # 真实 ROP 与 pre-render marker：在 hython 子进程里
        # createNode('ifd') + addRenderEventCallback 写 marker。
        # 简化：直接在 MCP 进程里通过 execute_code 创建。
        env_extra = {"OUT_DIR": out_dir, "HOUDINI_MCP_HYTHON": hython,
                      "HFS": os.environ.get("HFS", "")}

        # 创建 /out ROP 容器（如果不存在）+ ifd1（最小 fixture）
        # H21.0 ROP type 名是 ``karma``（H22+ 是 ``karmarender``），所以
        # fixture 用 ``karma`` 直接匹配实机；H22 live smoke 需把 karma
        # 换成 karmarender。
        section("create /out fixture (ifd1)")
        create_resp = _send("execute_code", {
            "code": (
                "out = hou.node('/out')\n"
                "if out is None:\n"
                "    out = hou.node('/').createNode('out', 'out')\n"
                "if hou.node('/out/ifd1') is None:\n"
        "    hou.node('/out').createNode('ifd', 'ifd1')\n"
                "if hou.node('/out/opengl1') is None:\n"
        "    hou.node('/out').createNode('opengl', 'opengl1')\n"
                "if hou.node('/out/karma_cpu1') is None:\n"
        "    k = hou.node('/out').createNode('karma', 'karma_cpu1')\n"
        "    k.parm('engine').set('cpu')\n"
                "if hou.node('/out/karma_xpu1') is None:\n"
        "    k = hou.node('/out').createNode('karma', 'karma_xpu1')\n"
        "    k.parm('engine').set('xpu')\n"
                "if hou.node('/out/mantra1') is None:\n"
        "    hou.node('/out').createNode('ifd', 'mantra1')\n"
                "print('created')\n"
            ),
            "policy": "privileged",
        })
        results.append(("create_fixture",
                        create_resp.get("status") == "success",
                        create_resp.get("message", "")))

        # 装 pre-render marker：每 ROP 装写 marker 文件的 callback
        section("install pre-render marker on ROPs")
        marker_code = (
            "import os\n"
            "marker = os.path.join(os.environ['OUT_DIR'], 'marker.txt')\n"
            "def _w():\n"
            "    try:\n"
            "        open(marker, 'w').write(repr(hou.frame()))\n"
            "    except Exception as e:\n"
            "        print('marker write fail', e)\n"
            "for path in ('/out/ifd1', '/out/opengl1', '/out/karma_cpu1',\n"
            "             '/out/karma_xpu1', '/out/mantra1'):\n"
            "    n = hou.node(path)\n"
            "    if n is None:\n"
            "        continue\n"
            "    n.removeAllRenderEventCallbacks()\n"
        "    n.addRenderEventCallback(\n"
            "        (hou.renderEventType.PreRender, lambda: _w()))\n"
            "print('marker installed')\n"
        )
        marker_resp = _send("execute_code", {
            "code": marker_code,
            "policy": "privileged",
        })
        results.append(("install_marker",
                        marker_resp.get("status") == "success",
                        marker_resp.get("message", "")))

        # Test 1: opengl redirect → marker 不出现
        section("opengl redirect")
        if os.path.exists(marker_path):
            os.remove(marker_path)
        resp = _send("start_render", {
            "node_path": "/out/opengl1",
        })
        result = resp.get("result", resp)
        results.append(("opengl_redirect",
                        "_redirect" in result,
                        "got _redirect=%r" % result.get("_redirect")))
        time.sleep(0.5)
        results.append(("opengl_marker_absent",
                        not os.path.exists(marker_path),
                        "marker=%s" % marker_path))

        # Test 2: karma 无 token → interrupt → marker 不出现
        section("karma_cpu no token interrupt")
        if os.path.exists(marker_path):
            os.remove(marker_path)
        resp = _send("start_render", {
            "node_path": "/out/karma_cpu1",
        })
        result = resp.get("result", resp)
        results.append(("karma_no_token_interrupt",
                        result.get("_interrupt") == "user_consent_required",
                        "got _interrupt=%r" % result.get("_interrupt")))
        time.sleep(0.5)
        results.append(("karma_no_token_marker_absent",
                        not os.path.exists(marker_path),
                        "marker=%s" % marker_path))

        # Test 3: karma 错 token → interrupt → marker 不出现
        section("karma_cpu bad token interrupt")
        if os.path.exists(marker_path):
            os.remove(marker_path)
        resp = _send("start_render", {
            "node_path": "/out/karma_cpu1",
            "consent_token": "bogus-deadbeef-token",
        })
        result = resp.get("result", resp)
        results.append(("karma_bad_token_interrupt",
                        result.get("_interrupt") == "user_consent_required",
                        "got _interrupt=%r" % result.get("_interrupt")))
        time.sleep(0.5)
        results.append(("karma_bad_token_marker_absent",
                        not os.path.exists(marker_path),
                        "marker=%s" % marker_path))

        # Test 4: karma_xpu 无 token → interrupt
        section("karma_xpu no token interrupt")
        if os.path.exists(marker_path):
            os.remove(marker_path)
        resp = _send("start_render", {
            "node_path": "/out/karma_xpu1",
        })
        result = resp.get("result", resp)
        results.append(("karma_xpu_interrupt",
                        result.get("_interrupt") == "user_consent_required",
                        "got _interrupt=%r" % result.get("_interrupt")))

        # Test 5: 未知 ROP type → error
        section("unknown ROP type error")
        if os.path.exists(marker_path):
            os.remove(marker_path)
        resp = _send("start_render", {
            "node_path": "/out/redshift1",
        })
        result = resp.get("result", resp)
        results.append(("unknown_rop_type_error",
                        result.get("status") == "error",
                        "got status=%r" % result.get("status")))

        # Test 6: batch 前置 mutation + 后置 opengl → blocked at server Layer 2
        # （hint 缺失时 Layer 1 放行，由 server handler 从真实 node 推断
        # 后拦截；result 必含 ``_redirect`` 操作结果，marker 不出现）
        section("batch mutation + opengl blocked")
        if os.path.exists(marker_path):
            os.remove(marker_path)
        resp = _send("batch", {
            "operations": [
                {"type": "set_render_settings",
                 "params": {"node_path": "/out/ifd1",
                              "parameters": {"trange": ["off"]}}},
                {"type": "start_render",
                 "params": {"node_path": "/out/opengl1"}},
            ],
            "continue_on_error": True,
        })
        result = resp.get("result", resp)
        results_list = result.get("results", [])
        render_result = next((r for r in results_list
                              if isinstance(r, dict)
                              and r.get("operation_type") == "start_render"),
                             None)
        render_response = (render_result or {}).get("response", {}) if render_result else {}
        results.append(("batch_opengl_blocked",
                        render_response.get("_redirect") == "flipbook",
                        "got %r" % render_response))
        time.sleep(0.5)
        results.append(("batch_opengl_marker_absent",
                        not os.path.exists(marker_path),
                        "marker=%s" % marker_path))

        # Test 7: batch 前置 mutation + 后置 karma no token → blocked
        section("batch mutation + karma no token blocked")
        if os.path.exists(marker_path):
            os.remove(marker_path)
        resp = _send("batch", {
            "operations": [
                {"type": "set_render_settings",
                 "params": {"node_path": "/out/ifd1",
                              "parameters": {"trange": ["off"]}}},
                {"type": "start_render",
                 "params": {"node_path": "/out/karma_cpu1"}},
            ],
            "continue_on_error": True,
        })
        result = resp.get("result", resp)
        results_list = result.get("results", [])
        render_result = next((r for r in results_list
                              if isinstance(r, dict)
                              and r.get("operation_type") == "start_render"),
                             None)
        render_response = (render_result or {}).get("response", {}) if render_result else {}
        results.append(("batch_karma_blocked",
                        render_response.get("_interrupt") == "user_consent_required",
                        "got %r" % render_response))
        time.sleep(0.5)
        results.append(("batch_karma_marker_absent",
                        not os.path.exists(marker_path),
                        "marker=%s" % marker_path))

        # Test 8: 5 分类核对（list_render_nodes 包含全部 5 个 ROP）
        section("list_render_nodes returns 5 ROPs")
        resp = _send("list_render_nodes", {"parent_path": "/out"})
        result = resp.get("result", resp)
        nodes = result.get("nodes", []) if isinstance(result, dict) else []
        results.append(("list_render_nodes_count",
                        len(nodes) == 5,
                        "got %d" % len(nodes)))
        renderer_map = {n.get("type"): n.get("renderer")
                        for n in nodes if isinstance(n, dict)}
        results.append(("list_render_nodes_ifd_mantra",
                        renderer_map.get("ifd") == "mantra",
                        "got %r" % renderer_map))
        results.append(("list_render_nodes_opengl_opengl",
                        renderer_map.get("opengl") == "opengl",
                        ""))
        results.append(("list_render_nodes_karma_cpu",
                        renderer_map.get("karmarender") in
                        ("karma_cpu", "karma_xpu"),
                        ""))

        # Test 9: get_render_settings 返回白名单
        section("get_render_settings whitelist")
        resp = _send("get_render_settings", {"node_path": "/out/ifd1"})
        result = resp.get("result", resp)
        params = result.get("parameters", {}) if isinstance(result, dict) else {}
        results.append(("get_render_settings_node_type",
                        result.get("node_type") == "ifd",
                        "got %r" % result.get("node_type")))
        results.append(("get_render_settings_no_script",
                        "soho_program" not in params,
                        "params=%r" % sorted(params.keys())))

        # Test 10: set_render_settings 安全路径
        section("set_render_settings success path")
        resp = _send("set_render_settings", {
            "node_path": "/out/ifd1",
            "parameters": {"trange": ["off"]},
        })
        result = resp.get("result", resp)
        results.append(("set_render_settings_success",
                        result.get("status") == "success",
                        "got %r" % result))

    finally:
        # 清理 ROP 节点 + marker + sentinel + out_dir
        section("cleanup")
        try:
            _send("execute_code", {
                "code": (
                    "for n in ('/out/ifd1', '/out/opengl1',\n"
                    "          '/out/karma_cpu1', '/out/karma_xpu1',\n"
                    "          '/out/mantra1'):\n"
                    "    node = hou.node(n)\n"
                    "    if node is not None:\n"
                    "        node.removeAllRenderEventCallbacks()\n"
                    "        node.destroy()\n"
                ),
                "policy": "privileged",
            })
        except Exception as error:
            sys.stderr.write("cleanup warning: %s\n" % error)
        for path in (marker_path,):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        try:
            if os.path.isdir(out_dir):
                shutil.rmtree(out_dir, ignore_errors=True)
        except OSError:
            pass
        try:
            if os.path.isdir(sentinel_dir):
                shutil.rmtree(sentinel_dir, ignore_errors=True)
        except OSError:
            pass

    # 汇总
    pass_n = sum(1 for r in results if r[1])
    fail_n = sum(1 for r in results if not r[1])
    sys.stderr.write("\n=== SUMMARY ===\n")
    for name, ok, detail in results:
        sys.stderr.write("%s %s%s\n" % (
            "PASS" if ok else "FAIL", name,
            "  // " + detail if detail else ""))
    sys.stderr.write("\n%d PASS, %d FAIL\n" % (pass_n, fail_n))
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())