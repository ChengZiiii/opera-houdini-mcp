# -*- coding: utf-8 -*-
"""feat-mcp-round2-hardening §2c（1a94a62 补丁）live e2e：
policy redirect 响应必须携带 renderer 语义字段。

背景：2026-09-10 GUI 冒烟发现 render_single_view 在缺 OGL 3.3 主机走
policy redirect 早返回时，响应只有 ``renderer`` 没有
``requested_renderer`` / ``actual_backend``（_attach_render_semantics
只挂在成功/失败路径）。1a94a62 修复后本脚本在 hython headless daemon
（独立端口，绝不触达生产 9876）实证：

1. render_single_view(opengl) 的 redirect 响应含 requested_renderer
   与 actual_backend；
2. actual_backend == ``_redirect`` 目标（flipbook / qscreen_fallback），
   不虚构 opengl_rop；
3. 兼容字段 renderer 仍为请求值。

运行（tests/ 目录）：
    python h21_live_render_redirect_semantics.py \
        --hython "C:/Program Files/Side Effects Software/Houdini 21.0.596/bin/hython.exe"

退出码：0 全 PASS / SKIP（环境缺 hython）；1 有 FAIL。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _e2e_helpers import (  # noqa: E402  (tests/ 目录运行)
    HoudiniConn,
    HoudiniCallError,
    StepResult,
    assert_step,
    emit_summary,
)
from hython_headless_e2e import _find_hython  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 19877  # 与 §1 e2e（19876）错开，可并行


def _drill(resp: Any) -> Dict[str, Any]:
    if not isinstance(resp, dict):
        return {}
    inner = resp.get("result")
    return inner if isinstance(inner, dict) else {}


def _env_headless_dir() -> str:
    override = os.environ.get("HOUDINI_MCP_ENV_DIR", "").strip()
    env_dir = override if (override and os.path.isabs(override)) else \
        os.path.join(ROOT, "..", "{0}-env".format(os.path.basename(ROOT)))
    return os.path.join(env_dir, ".headless")


def _cleanup_runtime_metadata(host: str, port: int) -> None:
    key = "{0}-{1}".format(host, int(port))
    for suffix in (".runtime.json", ".lock"):
        path = os.path.join(_env_headless_dir(), key + suffix)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _wait_ready(proc: subprocess.Popen, port: int, log_paths,
                timeout: float = 120.0) -> Optional[str]:
    import time
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return "daemon 退出（code={0}）".format(proc.returncode)
        try:
            with HoudiniConn(host=HOST, port=port, timeout=5.0) as conn:
                conn.call("ping")
            return None
        except (ConnectionError, OSError, HoudiniCallError) as e:
            last_err = e
            time.sleep(0.5)
    return "daemon {0}s 未就绪（last_err={1}）".format(timeout, last_err)


def _log_tail(log_paths, max_chars: int = 1500) -> str:
    chunks = []
    for path in log_paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                chunks.append(fh.read()[-max_chars:])
        except OSError:
            continue
    return "\n".join(chunks)


def _make_displayed_geo(conn: HoudiniConn, results: List[StepResult]) -> None:
    """redirect 判定在 find_displayed_geometry 之前，但为稳妥仍备好显示几何。"""
    name = "0. 准备显示几何（隔离场景内）"
    try:
        code = ("import hou\n"
                "geo = hou.node('/obj').createNode('geo', 'MCP_E2E_RDR_GEO')\n"
                "box = geo.createNode('box')\n"
                "box.setDisplayFlag(True)\n"
                "print('ready')")
        resp = conn.call("execute_code", code=code, policy="normal")
        inner = _drill(resp)
        ok = "ready" in (inner.get("stdout") or "")
        assert_step(results, name, ok=ok)
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail=str(e)[:200])


def _assert_redirect_semantics(conn: HoudiniConn,
                               results: List[StepResult]) -> None:
    name = "1. redirect 响应携带 requested_renderer / actual_backend"
    try:
        resp = conn.call("render_single_view",
                         render_engine="opengl", render_path=None)
        inner = _drill(resp)
        has_fields = ("requested_renderer" in inner
                      and "actual_backend" in inner)
        detail = "keys={0}".format(sorted(
            k for k in inner.keys()
            if k in ("requested_renderer", "actual_backend", "renderer",
                     "_redirect", "status")))
        assert_step(results, name, ok=has_fields, detail=detail)
        return inner
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail=str(e)[:200])
        return {}


def _assert_backend_matches_redirect(inner: Dict[str, Any],
                                     results: List[StepResult]) -> None:
    name = "2. actual_backend == _redirect 目标（不虚构 opengl_rop）"
    redirect = inner.get("_redirect")
    backend = inner.get("actual_backend")
    if redirect:
        ok = (backend == str(redirect)) and (backend != "opengl_rop")
        assert_step(results, name, ok=ok,
                    detail="_redirect={0!r} actual_backend={1!r}".format(
                        redirect, backend))
    else:
        # 非 redirect 主机（有 OGL 或走了执行/错误路径）：字段仍必须在，
        # backend 为实际执行值即可
        ok = isinstance(backend, str) and backend != ""
        assert_step(results, name, ok=ok,
                    detail="（无 _redirect）actual_backend={0!r}".format(backend))


def _assert_compat_renderer_field(inner: Dict[str, Any],
                                  results: List[StepResult]) -> None:
    name = "3. 兼容字段 renderer == 请求值 opengl"
    assert_step(results, name, ok=(inner.get("renderer") == "opengl"),
                detail="renderer={0!r}".format(inner.get("renderer")))


def _cleanup_nodes(conn: HoudiniConn) -> None:
    try:
        conn.call("delete_node", path="/obj/MCP_E2E_RDR_GEO")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="redirect 语义字段 live e2e（独立端口）")
    parser.add_argument("--hython", default=None)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    port = int(args.port)
    if port == 9876:
        print("FAIL: 禁止使用生产端口 9876")
        return 1

    hython = _find_hython(args.hython)
    if not hython:
        print("SKIP: 未找到 hython（--hython 或 HFS 环境变量）")
        return 0

    token = uuid.uuid4().hex
    log_dir = os.path.join(_env_headless_dir(), "e2e_logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = os.path.join(os.path.expanduser("~"), "houdini_mcp_e2e_logs")
        os.makedirs(log_dir, exist_ok=True)
    out_log = os.path.join(log_dir, "render_redirect_{0}.out.log".format(port))
    err_log = os.path.join(log_dir, "render_redirect_{0}.err.log".format(port))
    log_paths = (out_log, err_log)

    cmd = [hython, os.path.join(ROOT, "headless_host.py"),
           "--host", HOST, "--port", str(port),
           "--owner-token", token, "--idle-seconds", "60"]
    results: List[StepResult] = []
    proc: Optional[subprocess.Popen] = None
    try:
        with open(out_log, "w", encoding="utf-8") as out_fh, \
                open(err_log, "w", encoding="utf-8") as err_fh:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out_fh, stderr=err_fh)
            not_ready = _wait_ready(proc, port, log_paths)
            if not_ready:
                print("FAIL: {0}\n{1}".format(not_ready, _log_tail(log_paths)))
                return 1
            with HoudiniConn(host=HOST, port=port, timeout=60.0) as conn:
                try:
                    _make_displayed_geo(conn, results)              # 0
                    inner = _assert_redirect_semantics(conn, results)  # 1
                    _assert_backend_matches_redirect(inner, results)   # 2
                    _assert_compat_renderer_field(inner, results)      # 3
                finally:
                    _cleanup_nodes(conn)
        return 0
    except (ConnectionError, OSError, HoudiniCallError) as e:
        print("FAIL: daemon 通信失败: {0}".format(e))
        return 1
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
            _cleanup_runtime_metadata(HOST, port)
        md = emit_summary(results)
        print(md)
        n_fail = sum(1 for r in results if r.status == "FAIL")
        n_pass = sum(1 for r in results if r.status == "PASS")
        print("[summary] {0} steps: {1} PASS / {2} FAIL".format(
            len(results), n_pass, n_fail))
        if n_fail:
            print(_log_tail(log_paths))
        return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
