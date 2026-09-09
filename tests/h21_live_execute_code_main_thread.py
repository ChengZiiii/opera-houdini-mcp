#!/usr/bin/env python3
"""H21 live e2e — execute_code 主线程同步执行（feat-mcp-round2-hardening §1）。

在**独立端口**（默认 19876，绝不触达生产 9876）的 hython headless daemon
上端到端验证 execute_code 新契约：

1. 主线程断言：`threading.current_thread() is threading.main_thread()` 为 True
2. **核心验收**：normal 策略 execute_code 建节点 → hou.undos.performUndo()
   → 节点消失（undo 真实回滚，worker 线程时代不可达）
3. timeout 参数忽略：timeout=1 + sleep(3) → 完整执行、stdout 完整、
   audit timed_out=false、timeout_ignored=true、execution_mode="main_thread"
4. audit 字段：undo_group 记录 / read-only 无 undo_group / read-only 拦截
   mutation / dangerous fail-closed
5. capture_diff + get_last_scene_diff 仍可用

harness 先例（change 3）：复用 headless_host.py 在 QCoreApplication 事件
循环中跑 HoudiniMCPServer；客户端用 tests/_e2e_helpers.HoudiniConn 直连
TCP（4 字节大端长度前缀 + UTF-8 JSON 帧）。

运行方式：
    .venv/Scripts/python.exe tests/h21_live_execute_code_main_thread.py \
        --hython "C:/Program Files/Side Effects Software/Houdini 21.0.596/bin/hython.exe"

前置：无（daemon 由本脚本自行拉起并销毁；不依赖也不触碰 9876）。

退出码：
- 0 — 全 PASS，或环境缺 hython（SKIP）。
- 1 — 至少一个断言 FAIL。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
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

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
DEFAULT_PORT = 19876
NODE_UNDO = "/obj/MCP_E2E_UNDO"
NODE_DIFF = "/obj/MCP_E2E_DIFF"


def _drill(resp: Any) -> Dict[str, Any]:
    """conn.call 返 envelope {"status": "success", "result": <handler dict>}。"""
    if not isinstance(resp, dict):
        return {}
    inner = resp.get("result")
    if isinstance(inner, dict):
        return inner
    return {}


def _env_headless_dir():
    """与 headless_host._env_dir 对齐的 metadata 目录（用于事后清理）。"""
    override = os.environ.get("HOUDINI_MCP_ENV_DIR", "").strip()
    env_dir = override if (override and os.path.isabs(override)) else \
        os.path.join(ROOT, "..", "{0}-env".format(os.path.basename(ROOT)))
    return os.path.join(env_dir, ".headless")


def _cleanup_runtime_metadata(host: str, port: int) -> None:
    """daemon 被硬杀时 atexit 不会跑：best-effort 清掉本端口的 metadata。"""
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
    """轮询 framed ping 直到 daemon 就绪；失败时返回原因字符串。"""
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return "daemon 退出（code={0}）\n{1}".format(
                proc.returncode, _log_tail(log_paths))
        try:
            with HoudiniConn(host=HOST, port=port, timeout=5.0) as conn:
                conn.call("ping")
            return None
        except (ConnectionError, OSError, HoudiniCallError) as e:
            last_err = e
            time.sleep(0.5)
    return "daemon {0}s 未就绪（last_err={1}）\n{2}".format(
        timeout, last_err, _log_tail(log_paths))


def _log_tail(log_paths, max_chars: int = 2000) -> str:
    chunks = []
    for path in log_paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                chunks.append("---- {0} ----\n{1}".format(
                    os.path.basename(path), fh.read()[-max_chars:]))
        except OSError:
            continue
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# 断言步骤
# ---------------------------------------------------------------------------
def _assert_main_thread(conn: HoudiniConn, results: List[StepResult]) -> None:
    name = "1. execute_code 在主线程执行（current_thread is main_thread）"
    try:
        code = ("import threading\n"
                "print(threading.current_thread() is threading.main_thread())")
        resp = conn.call("execute_code", code=code, policy="normal")
        inner = _drill(resp)
        ok = "True" in (inner.get("stdout") or "")
        assert_step(results, name, ok=ok,
                    detail="stdout={0!r}".format(
                        (inner.get("stdout") or "").strip()[:40]))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))


def _assert_undo_rollback(conn: HoudiniConn, results: List[StepResult]) -> None:
    """核心验收：normal 建节点 → performUndo → 节点消失。"""
    # 1) normal 策略建节点（audit 应记录 undo_group）
    name = "2a. normal 建节点 + undo_group 记录"
    try:
        resp = conn.call(
            "execute_code",
            code="hou.node('/obj').createNode('geo', 'MCP_E2E_UNDO')",
            policy="normal")
        inner = _drill(resp)
        audit = inner.get("_audit") or {}
        ok = (inner.get("executed") is True
              and audit.get("undo_group") == "MCP: execute_code (normal)")
        assert_step(results, name, ok=ok,
                    detail="executed={0} undo_group={1!r}".format(
                        inner.get("executed"), audit.get("undo_group")))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))
        return

    # 2) 节点确实存在（read-only 查询，不包 undo 组）
    name = "2b. 建节点后节点存在"
    try:
        resp = conn.call(
            "execute_code",
            code="print(hou.node('{0}') is not None)".format(NODE_UNDO),
            policy="read-only")
        inner = _drill(resp)
        ok = "True" in (inner.get("stdout") or "")
        assert_step(results, name, ok=ok,
                    detail="stdout={0!r}".format(
                        (inner.get("stdout") or "").strip()[:20]))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))

    # 3) performUndo（read-only 执行：performUndo 不命中 mutation 模式；
    #    read-only 不包组 → 回滚的是上一个已关闭的建节点组）
    name = "2c. hou.undos.performUndo() 执行成功"
    try:
        resp = conn.call("execute_code",
                         code="hou.undos.performUndo()",
                         policy="read-only")
        inner = _drill(resp)
        ok = (inner.get("executed") is True
              and not inner.get("execution_error"))
        assert_step(results, name, ok=ok,
                    detail="execution_error={0!r}".format(
                        (inner.get("execution_error") or "")[:120]))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))

    # 4) 节点已被回滚（核心断言）
    name = "2d. performUndo 后节点消失（undo 真实回滚）"
    try:
        resp = conn.call(
            "execute_code",
            code="print(hou.node('{0}') is None)".format(NODE_UNDO),
            policy="read-only")
        inner = _drill(resp)
        ok = "True" in (inner.get("stdout") or "")
        assert_step(results, name, ok=ok,
                    detail="stdout={0!r}（False=节点残留，undo 未生效）".format(
                        (inner.get("stdout") or "").strip()[:20]))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))


def _assert_timeout_ignored(conn: HoudiniConn,
                            results: List[StepResult]) -> None:
    name = "3. timeout=1 + sleep(3) 完整执行且 audit 标注忽略"
    try:
        resp = conn.call(
            "execute_code",
            code="import time\ntime.sleep(3)\nprint('done')",
            policy="normal", timeout=1)
        inner = _drill(resp)
        audit = inner.get("_audit") or {}
        ok = (inner.get("executed") is True
              and "done" in (inner.get("stdout") or "")
              and audit.get("timed_out") is False
              and audit.get("timeout_ignored") is True
              and audit.get("execution_mode") == "main_thread"
              and int(audit.get("elapsed_ms") or 0) >= 3000)
        assert_step(results, name, ok=ok,
                    detail="stdout={0!r} timed_out={1} timeout_ignored={2} "
                           "execution_mode={3!r} elapsed_ms={4}".format(
                               (inner.get("stdout") or "").strip()[:20],
                               audit.get("timed_out"),
                               audit.get("timeout_ignored"),
                               audit.get("execution_mode"),
                               audit.get("elapsed_ms")))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))


def _assert_read_only_intercepts(conn: HoudiniConn,
                                 results: List[StepResult]) -> None:
    name = "4. read-only 拦截 mutation（createNode blocked，无 undo_group）"
    try:
        resp = conn.call(
            "execute_code",
            code="hou.node('/obj').createNode('geo', 'MCP_E2E_NOPE')",
            policy="read-only")
        inner = _drill(resp)
        audit = inner.get("_audit") or {}
        ok = (inner.get("executed") is False
              and inner.get("blocked") is True
              and "mutation" in (inner.get("reason") or "")
              and "undo_group" not in audit)
        assert_step(results, name, ok=ok,
                    detail="blocked={0} reason={1!r}".format(
                        inner.get("blocked"),
                        (inner.get("reason") or "")[:80]))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))


def _assert_dangerous_fail_closed(conn: HoudiniConn,
                                  results: List[StepResult]) -> None:
    name = "5. normal + dangerous 代码 fail-closed"
    try:
        resp = conn.call(
            "execute_code",
            code="import subprocess\nsubprocess.run(['ls'])",
            policy="normal")
        inner = _drill(resp)
        ok = (inner.get("executed") is False
              and inner.get("blocked") is True
              and "dangerous" in (inner.get("reason") or ""))
        assert_step(results, name, ok=ok,
                    detail="blocked={0} reason={1!r}".format(
                        inner.get("blocked"),
                        (inner.get("reason") or "")[:80]))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))


def _assert_read_only_no_undo_group(conn: HoudiniConn,
                                    results: List[StepResult]) -> None:
    name = "6. read-only 安全代码执行且 audit 无 undo_group"
    try:
        resp = conn.call("execute_code", code="print('ro ok')",
                         policy="read-only")
        inner = _drill(resp)
        audit = inner.get("_audit") or {}
        ok = (inner.get("executed") is True
              and "undo_group" not in audit
              and audit.get("execution_mode") == "main_thread")
        assert_step(results, name, ok=ok,
                    detail="executed={0} audit_keys={1}".format(
                        inner.get("executed"), sorted(audit.keys())))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))


def _assert_capture_diff(conn: HoudiniConn, results: List[StepResult]) -> None:
    name = "7. capture_diff=True + get_last_scene_diff 仍可用"
    try:
        resp = conn.call(
            "execute_code",
            code="hou.node('/obj').createNode('geo', 'MCP_E2E_DIFF')",
            policy="normal", capture_diff=True)
        inner = _drill(resp)
        executed = inner.get("executed") is True
        if not executed:
            assert_step(results, name, ok=False,
                        detail="execute_code 未执行: {0!r}".format(
                            (inner.get("execution_error") or "")[:120]))
            return
        diff_resp = conn.call("get_last_scene_diff")
        diff = _drill(diff_resp)
        after_paths = [n.get("path")
                       for n in (diff.get("after") or {}).get("nodes", [])]
        ok = (diff.get("available") is True
              and diff.get("changed") is True
              and NODE_DIFF in after_paths)
        assert_step(results, name, ok=ok,
                    detail="available={0} changed={1} diff_node={2}".format(
                        diff.get("available"), diff.get("changed"),
                        NODE_DIFF in after_paths))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, detail="err: {0}".format(str(e)[:200]))


def _cleanup_nodes(conn: HoudiniConn) -> None:
    """best-effort 删测试节点；永不抛异常。"""
    for path in (NODE_UNDO, NODE_DIFF, "/obj/MCP_E2E_NOPE"):
        try:
            conn.call("delete_node", path=path)
        except Exception:
            pass


def _print_at_a_glance(results: List[StepResult]) -> None:
    n_pass = sum(1 for r in results if r.status == "PASS")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_skip = sum(1 for r in results if r.status == "SKIP")
    print("[summary] {0} steps: {1} PASS / {2} FAIL / {3} SKIP".format(
        len(results), n_pass, n_fail, n_skip))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="execute_code 主线程同步执行 live e2e（独立端口）")
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
    out_log = os.path.join(log_dir, "execute_code_main_thread_{0}.out.log".format(port))
    err_log = os.path.join(log_dir, "execute_code_main_thread_{0}.err.log".format(port))
    log_paths = (out_log, err_log)

    cmd = [
        hython,
        os.path.join(ROOT, "headless_host.py"),
        "--host", HOST,
        "--port", str(port),
        "--owner-token", token,
        "--idle-seconds", "60",
    ]
    results: List[StepResult] = []
    proc: Optional[subprocess.Popen] = None
    try:
        with open(out_log, "w", encoding="utf-8") as out_fh, \
                open(err_log, "w", encoding="utf-8") as err_fh:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out_fh, stderr=err_fh)
            not_ready = _wait_ready(proc, port, log_paths)
            if not_ready:
                print("FAIL: {0}".format(not_ready))
                return 1

            with HoudiniConn(host=HOST, port=port, timeout=60.0) as conn:
                try:
                    _assert_main_thread(conn, results)             # 1
                    _assert_undo_rollback(conn, results)           # 2a-2d 核心
                    _assert_timeout_ignored(conn, results)         # 3
                    _assert_read_only_intercepts(conn, results)    # 4
                    _assert_dangerous_fail_closed(conn, results)   # 5
                    _assert_read_only_no_undo_group(conn, results)  # 6
                    _assert_capture_diff(conn, results)            # 7
                finally:
                    _cleanup_nodes(conn)
        return 0
    except (ConnectionError, OSError, HoudiniCallError) as e:
        print("FAIL: daemon 通信失败: {0}\n{1}".format(e, _log_tail(log_paths)))
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
        _print_at_a_glance(results)
        has_fail = any(r.status == "FAIL" for r in results)
        if has_fail:
            print(_log_tail(log_paths, max_chars=1500))
        return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
