#!/usr/bin/env python
"""真实 hython headless daemon E2E。

运行方式：
    python tests/hython_headless_e2e.py --hython C:/Houdini/bin/hython.exe

脚本只在找到真实 hython 时执行；缺少 Houdini 或 license 会输出带原因的
SKIP，不使用 fake hou、fake Qt 或 mock server 冒充 live smoke。
"""
import argparse
import json
import os
import shutil
import sys
import subprocess
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _env_dir():
    """Derive env dir from package dirname: <package_parent>/<package_basename>-env."""
    override = os.environ.get("HOUDINI_MCP_ENV_DIR", "").strip()
    if override and os.path.isabs(override):
        return override
    return os.path.join(ROOT, "..", f"{os.path.basename(ROOT)}-env")


def _find_hython(explicit=None):
    candidates = []
    if explicit:
        candidates.append(explicit)
    configured = os.environ.get("HOUDINI_MCP_HYTHON")
    if configured:
        candidates.append(configured)
    hfs = os.environ.get("HFS")
    if hfs:
        candidates.extend([
            os.path.join(hfs, "bin", "hython.exe"),
            os.path.join(hfs, "bin", "hython"),
        ])
    candidates.extend(["hython.exe", "hython"])
    for candidate in candidates:
        resolved = candidate
        if not os.path.isabs(candidate):
            resolved = shutil.which(candidate) or candidate
        if os.path.isfile(resolved):
            return os.path.abspath(resolved)
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _find_bridge_python():
    candidates = []
    configured = os.environ.get("HOUDINI_MCP_BRIDGE_PYTHON")
    if configured:
        candidates.append(configured)
    candidates.append(os.path.join(_env_dir(), "python", "python.exe"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _run_bridge(bridge_python, hython, host, port, idle_seconds,
                startup_timeout):
    bridge_code = "\n".join([
        "import json, os, socket, sys",
        "sys.path.insert(0, {0!r})".format(ROOT),
        "os.environ['HOUDINI_MCP_HYTHON'] = {0!r}".format(hython),
        "os.environ['HOUDINI_MCP_HEADLESS_IDLE_SECONDS'] = {0!r}".format(
            str(idle_seconds)),
        "os.environ['HOUDINI_MCP_HEADLESS_START_TIMEOUT'] = {0!r}".format(
            str(startup_timeout)),
        "import houdini_mcp_server as bridge",
        "bridge._houdini_port = {0!r}".format(int(port)),
        "bridge._houdini_connection = None",
        "before = set(bridge._HEADLESS_PROCESSES)",
        "ping = bridge._houdini_call('ping', {})",
        "scene = bridge._houdini_call('get_scene_info', {})",
        "no_pane = bridge._houdini_call('capture_pane_screenshot', "
        "{'pane_type_name': 'SceneViewer'})",
        "connection = bridge._houdini_connection",
        "new_processes = [p for token, p in bridge._HEADLESS_PROCESSES.items() "
        "if token not in before]",
        "runtime_path = bridge._headless_path("
        "{0!r}, {1!r}, bridge._HEADLESS_RUNTIME_SUFFIX)".format(host, int(port)),
        "payload = {'ping': ping, 'scene': scene, 'no_pane': no_pane, "
        "'process_pids': [p.pid for p in new_processes], "
        "'port': getattr(connection, 'port', None), "
        "'runtime_path': runtime_path}",
        "if connection is not None:",
        "    if connection.sock is not None:",
        "        try: connection.sock.shutdown(socket.SHUT_RDWR)",
        "        except OSError: pass",
        "    connection.disconnect()",
        "    bridge._houdini_connection = None",
        "print(json.dumps(payload, ensure_ascii=False))",
    ])
    completed = subprocess.run(
        [bridge_python, "-c", bridge_code],
        cwd=ROOT,
        capture_output=True,
        timeout=max(120.0, float(startup_timeout) + 30.0),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode(
            "utf-8", errors="replace").strip()
        raise RuntimeError("bridge subprocess failed: " + detail[-2000:])
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("bridge subprocess returned no JSON payload")
    return json.loads(lines[-1])


def _build_parser():
    parser = argparse.ArgumentParser(description="真实 hython headless E2E")
    parser.add_argument("--hython", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10987,
                        help="必须保持非默认端口以验证端到端透传")
    parser.add_argument("--idle-seconds", type=float, default=30)
    parser.add_argument("--startup-timeout", type=float, default=120)
    return parser


def run(argv=None):
    args = _build_parser().parse_args(argv)
    if int(args.port) == 9876:
        print("FAIL: E2E port must be non-default (not 9876)")
        return 1
    hython = _find_hython(args.hython)
    if not hython:
        print("SKIP: no real hython found; set --hython or HFS")
        return 0
    bridge_python = _find_bridge_python()
    if not bridge_python:
        print("SKIP: no embedded bridge Python found")
        return 0

    try:
        payload = _run_bridge(
            bridge_python, hython, args.host, args.port,
            args.idle_seconds, args.startup_timeout)
        process_pids = payload.get("process_pids") or []
        if len(process_pids) != 1:
            print("FAIL: bridge lazy launcher did not create exactly one daemon")
            return 1
        if payload.get("port") != int(args.port):
            print("FAIL: bridge did not retain the requested non-default port")
            return 1
        runtime_path = payload.get("runtime_path")
        if not runtime_path or not os.path.isfile(runtime_path):
            print("FAIL: bridge did not publish runtime metadata")
            return 1

        ping = payload.get("ping") or {}
        scene = payload.get("scene") or {}
        no_pane = payload.get("no_pane") or {}
        scene_ok = scene.get("status") == "success"
        warning = no_pane.get("result") or {}
        warning_ok = (warning.get("status") == "warning"
                      and (warning.get("_warning") or {}).get("code")
                      == "ui_unavailable")
        print("ping: {0}".format(
            "PASS" if (ping.get("result") or {}).get("pong") else "FAIL"))
        print("get_scene_info: {0}".format("PASS" if scene_ok else "FAIL"))
        print("no-pane warning: {0}".format(
            "PASS" if warning_ok else "FAIL"))
        if not scene_ok or not warning_ok:
            return 1

        idle_deadline = time.monotonic() + max(30.0, args.idle_seconds) + 20
        while time.monotonic() < idle_deadline:
            if not os.path.exists(runtime_path):
                break
            time.sleep(0.5)
        idle_ok = not os.path.exists(runtime_path)
        print("idle lifecycle: {0}".format("PASS" if idle_ok else "FAIL"))
        return 0 if idle_ok else 1
    except Exception as error:
        print("FAIL: " + str(error))
        return 1
if __name__ == "__main__":
    sys.exit(run())
