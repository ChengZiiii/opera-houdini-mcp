"""共享端到端测试辅助：Houdini TCP socket 客户端 + 步骤断言 + Markdown 汇总。

本模块是 fork `opera-houdini-mcp` 在 tests/ 目录下的官方 TCP 协议客户端与
断言工具，被 `phase4_e2e.py`、`phase5_full_regression.py`、`e2e_demo_table.py`
共同引用。

设计要点：
- 仅 stdlib（socket / struct / json / os / sys / time / dataclasses / typing），
  不引入 pytest / hou 等外部依赖；本模块可在纯 Python 环境（无 hou）下被
  `ast.parse` 静态校验。
- 与 server.py 的 `execute_command` 完全对齐：
  * 帧 = 4 字节大端长度前缀 + UTF-8 JSON
  * 请求 = {"type": cmd_type, "params": params}
  * 响应 = {"status": "success" | "error", ...}
- 默认 timeout=300s，与 phase4_e2e.py 历史行为一致。
- `HoudiniConn` 是 context manager，确保 socket 关闭。
- `HoudiniCallError` 携带完整响应字典，方便调用方做精细诊断。
- `assert_step` + `emit_summary` 提供统一 Markdown 报告，避免每个测试脚本
  各自实现一遍。

历史：
- 2026-07-21 从 phase4_e2e.py 提取 HoudiniConn / step / assert_ok；
  新增 StepResult / assert_step / emit_summary；统一 phase4_e2e.py
  与 e2e_demo_table.py 共享此模块。
- 修正 phase4_e2e.py 中硬编码的 host-specific 绝对路径 bug：
  原先指向某个固定 host 工作区下的 _capture_paths.py，违反 fork 跨主机
  可移植约束。现改为 `os.path.join(os.path.dirname(...), "_capture_paths.py")`
  的相对路径模式。
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 协议常量（与 server.py 保持一致；任何偏离都需先更新 server.py）
# ---------------------------------------------------------------------------
HOST_DEFAULT = "127.0.0.1"
PORT_DEFAULT = 9876
TIMEOUT_DEFAULT = 300  # 秒；与 phase4_e2e.py 历史默认一致
RECV_CHUNK = 8192
MAX_MSG_LEN = 50 * 1024 * 1024  # 50 MB；与 server.py _process_server 一致


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------
class HoudiniCallError(RuntimeError):
    """Houdini 端返回 status=error 或传输失败时抛出。

    携带完整响应字典（若可解析），方便调用方检查 error_type / error / message
    等字段。
    """

    def __init__(self, message: str, response: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.response = response or {}


# ---------------------------------------------------------------------------
# TCP 客户端
# ---------------------------------------------------------------------------
class HoudiniConn:
    """Houdini TCP 协议客户端（127.0.0.1:9876）。

    帧格式：4 字节大端长度前缀 + UTF-8 JSON 负载。
    请求格式：{"type": cmd_type, "params": params}。
    响应格式：{"status": "success" | "error", "result": ..., "message": ...}。

    用法：
        with HoudiniConn() as conn:
            r = conn.call("get_scene_info")
            print(r["result"]["node_count"])
    """

    def __init__(self, host: str = HOST_DEFAULT, port: int = PORT_DEFAULT,
                 timeout: float = TIMEOUT_DEFAULT) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buf = b""

    # ----- context manager -----
    def __enter__(self) -> "HoudiniConn":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        # connect 抛 ConnectionRefusedError / socket.timeout / OSError 时透传；
        # demo 入口在 with 外捕获并视为 SKIP。
        sock.connect((self.host, self.port))
        sock.settimeout(self.timeout)
        self.sock = sock
        self._buf = b""
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ----- 生命周期 -----
    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    # ----- 帧层 -----
    def _recv_exact(self, n: int) -> bytes:
        """从 socket 精确读取 n 字节（缓冲 + 多 recv 循环）。

        socket 关闭（recv 返回 b""）→ 抛 ConnectionError。
        超时（继承 socket.timeout，OSError 子类）→ 透传，由调用方决定如何处理。
        """
        sock = self.sock
        if sock is None:
            raise ConnectionError("socket closed")
        buf = self._buf
        while len(buf) < n:
            chunk = sock.recv(max(RECV_CHUNK, n - len(buf)))
            if not chunk:
                # 已关闭：把当前缓冲保留以便诊断；抛出前清空以防 stale
                self._buf = b""
                raise ConnectionError("socket closed by peer")
            buf += chunk
        out, buf = buf[:n], buf[n:]
        self._buf = buf
        return out

    def _recv_frame(self) -> bytes:
        head = self._recv_exact(4)
        msg_len = struct.unpack(">I", head)[0]
        if msg_len > MAX_MSG_LEN:
            raise HoudiniCallError(
                "response too large: {0} bytes".format(msg_len),
                response=None,
            )
        return self._recv_exact(msg_len)

    # ----- 高层 API -----
    def send_json(self, payload: Dict[str, Any]) -> None:
        """发送一个完整 JSON 请求（带 4 字节长度前缀）。"""
        sock = self.sock
        if sock is None:
            raise ConnectionError("socket closed")
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise HoudiniCallError("failed to encode payload: {0}".format(e),
                                   response=None) from e
        if len(body) > MAX_MSG_LEN:
            raise HoudiniCallError(
                "request too large: {0} bytes".format(len(body)),
                response=None,
            )
        framed = struct.pack(">I", len(body)) + body
        sock.sendall(framed)

    def recv_json(self) -> Dict[str, Any]:
        """读取一帧完整 JSON 响应。"""
        body = self._recv_frame()
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HoudiniCallError(
                "failed to decode response: {0}".format(e),
                response=None,
            ) from e

    def call(self, command: str, **params: Any) -> Dict[str, Any]:
        """发送 {type: command, params: params} 并返回解析后的响应 dict。

        Houdini 返回 status=error 时抛 HoudiniCallError；status=success 时
        返回响应 dict 本身（dict["result"] 由调用方按需提取）。
        """
        # params 中允许 None 值；空 kwargs 等价于 params={}
        payload_params = dict(params) if params else {}
        self.send_json({"type": command, "params": payload_params})
        response = self.recv_json()
        status = response.get("status")
        if status != "success":
            msg = response.get("message") or response.get("error") or \
                "unknown error from Houdini"
            raise HoudiniCallError(
                "Houdini returned status={0!r}: {1}".format(status, msg),
                response=response,
            )
        return response


# ---------------------------------------------------------------------------
# 步骤断言与汇总
# ---------------------------------------------------------------------------
@dataclass
class StepResult:
    """单步断言记录。"""

    name: str
    status: str  # "PASS" | "FAIL" | "SKIP" | "WARN"
    artifact: str = "-"
    detail: str = ""


def assert_step(results: List[StepResult], name: str, ok: bool,
                artifact: str = "-", detail: str = "",
                on_skip: bool = False) -> bool:
    """记录一个步骤结果。

    - ok=True → 记 PASS 并返回 True
    - ok=False, on_skip=False → 记 FAIL 并返回 False
    - ok=False, on_skip=True → 记 SKIP 并返回 False（视为非阻塞）

    返回值约定：仅 PASS 返回 True；FAIL / SKIP / WARN 均返回 False，方便
    调用方按需短路后续步骤（demo 默认不短路，所有步骤都跑完以便全面汇报）。
    """
    if ok:
        results.append(StepResult(name=name, status="PASS",
                                  artifact=artifact, detail=detail))
        return True
    status = "SKIP" if on_skip else "FAIL"
    results.append(StepResult(name=name, status=status,
                              artifact=artifact, detail=detail))
    return False


def emit_summary(results: List[StepResult],
                 out_path: Optional[str] = None) -> str:
    """把步骤列表渲染成 Markdown 表并可选落盘。

    表头：| Step | Status | Artifact | Detail |
    末尾追加一行 verdict："all_pass" / "fails: <list>" / "pass_with_warn"。
    返回渲染好的 Markdown 字符串。
    """
    lines: List[str] = []
    lines.append("| # | Step | Status | Artifact | Detail |")
    lines.append("|---|------|--------|----------|--------|")
    for idx, r in enumerate(results, start=1):
        # 表格里 detail 内的换行符替换为空格，避免破坏行结构
        safe_detail = (r.detail or "").replace("\n", " ").replace("|", "\\|")
        safe_artifact = (r.artifact or "-").replace("|", "\\|")
        lines.append("| {0} | {1} | {2} | {3} | {4} |".format(
            idx, r.name, r.status, safe_artifact, safe_detail))
    # 末尾判定
    fails = [r.name for r in results if r.status == "FAIL"]
    warns = [r.name for r in results if r.status == "WARN"]
    skips = [r.name for r in results if r.status == "SKIP"]
    if fails:
        verdict = "fails: {0}".format(", ".join(fails))
    elif warns:
        verdict = "pass_with_warn: {0}".format(", ".join(warns))
    elif skips:
        verdict = "pass_with_skip: {0}".format(", ".join(skips))
    else:
        verdict = "all_pass"
    lines.append("")
    lines.append("**Verdict:** {0}".format(verdict))
    md = "\n".join(lines)
    if out_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(out_path)),
                        exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(md)
        except OSError as e:
            # 落盘失败不致命——仍把 Markdown 返回给 stdout
            sys.stderr.write(
                "[warn] failed to write summary to {0!r}: {1}\n".format(
                    out_path, e))
    return md


__all__ = [
    "HoudiniConn",
    "HoudiniCallError",
    "StepResult",
    "assert_step",
    "emit_summary",
    "HOST_DEFAULT",
    "PORT_DEFAULT",
    "TIMEOUT_DEFAULT",
]