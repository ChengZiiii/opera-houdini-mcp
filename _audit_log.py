# -*- coding: utf-8 -*-
"""bridge 侧 MCP 命令 JSONL 审计（feat-mcp-console-log-audit §2）。

设计（design D3/D4）：
- 织入点：包装 ``mcp._tool_manager.call_tool``——与 bridge 既有的
  ``_install_capture_hook``（lessons 自动捕获）同层。该层拦截**所有**
  协议调用（含全部 174 个工具与不经 TCP 的 bridge-local 工具），且
  arguments 是协议侧干净 dict（ctx 已被 FastMCP 消化），摘要无对象噪声。
- 旁路语义：审计落盘失败只 ``logger.warning``，MUST NOT 使工具调用失败
  或变慢到可感知；审计内容不注入任何工具响应。
- 分段：空闲超 ``HOUDINI_MCP_AUDIT_SEGMENT_MIN``（默认 15 分钟）后的
  下一次写入开新段 ``audit-<yyyymmdd>-<seq>.jsonl``；目录内文件数超过
  ``HOUDINI_MCP_AUDIT_KEEP``（默认 30）时删最旧（文件名日期+序号天然
  有序）。单段文件无大小轮转——单行体积由 args ≤256 截断约束，总量由
  KEEP 硬性上界兜底（不依赖 _capture_paths 的 7 天 mtime 清理）。
- 字段增量可扩展：未来对齐上游 oculairmedia PR #17 事务字段
  （transaction id / affected paths / before-after revisions / checkpoint）
  时只加键不改行格式，零迁移成本。
- 只记录不重放：不提供任何重放/回放工具（audit-log spec v1 边界）。

env：
- ``HOUDINI_MCP_AUDIT_DIR``：审计目录（默认 ``$TEMP/houdini_mcp/audit``）。
- ``HOUDINI_MCP_AUDIT_KEEP``：保留段数（默认 30）。
- ``HOUDINI_MCP_AUDIT_SEGMENT_MIN``：分段间隔分钟（默认 15）。
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

# 进程级会话标识（bridge 生命周期 = 一个会话）
SESSION_ID = str(uuid.uuid4())

_ARGS_SUMMARY_MAX = 256
_TOOL_NAME_MAX = 120


def audit_dir():
    override = os.environ.get("HOUDINI_MCP_AUDIT_DIR", "").strip()
    if override:
        return override
    base = os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
    return os.path.join(base, "houdini_mcp", "audit")


def _keep_count():
    try:
        return max(int(os.environ.get("HOUDINI_MCP_AUDIT_KEEP", "30")), 1)
    except (TypeError, ValueError):
        return 30


def _segment_seconds():
    try:
        minutes = float(os.environ.get("HOUDINI_MCP_AUDIT_SEGMENT_MIN", "15"))
        return max(minutes * 60.0, 1.0)
    except (TypeError, ValueError):
        return 900.0


def summarize_args(arguments):
    """协议参数 dict 的摘要（≤256 字符；可能含用户本机路径，本机单用户
    信任边界内不脱敏——README 声明）。"""
    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    except Exception:
        text = repr(arguments)
    return text[:_ARGS_SUMMARY_MAX]


def extract_payload(result):
    """从工具调用结果提取响应 dict（兼容两种形态）。

    ``ToolManager.call_tool`` 在直接调用路径返回裸 dict，在协议路径返回
    ``list[TextContent]``（text 为 JSON 序列化）——与 bridge 既有的
    ``_capture_error_from_result`` 同款双形态处理。提取失败返回 None。
    """
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)):
        for item in result:
            text = getattr(item, "text", None)
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def classify_result(result):
    """返回 (ok, error_code, error_message)。

    - 提取不到 dict payload（str 响应、纯文本 TextContent）→ 按成功记
      （design D3：审计只记调用事实，错误语义不做文本嗅探）；
    - payload status 存在且 != success → 失败，附 error.code /
      error.message（兼容旧 shape error_code / origin / message）。
    """
    payload = extract_payload(result)
    if payload is None:
        return True, None, None
    status = payload.get("status")
    if status is None or status == "success":
        return True, None, None
    error = payload.get("error")
    if isinstance(error, dict):
        error_code = (error.get("code") or payload.get("error_code")
                      or payload.get("origin") or "unknown")
        error_message = error.get("message") or payload.get("message") or ""
    else:
        error_code = (payload.get("error_code") or payload.get("origin")
                      or "unknown")
        error_message = payload.get("message") or ""
    return False, error_code, error_message


class _SegmentWriter(object):
    """当前审计段文件的惰性持有者。

    单事件循环（asyncio）串行写，无跨线程竞争；磁盘异常在 ``write``
    内全吞（旁路语义）。
    """

    def __init__(self):
        self._fh = None
        self._path = None
        self._last_write = None

    def _open_new_segment(self):
        self._close()
        directory = audit_dir()
        os.makedirs(directory, exist_ok=True)
        today = datetime.now().strftime("%Y%m%d")
        # 续接当日已有最大序号，避免重启后 seq 撞名覆盖
        seq = 0
        try:
            for name in os.listdir(directory):
                if name.startswith("audit-{0}-".format(today)) and \
                        name.endswith(".jsonl"):
                    tail = name[len("audit-{0}-".format(today)):-len(".jsonl")]
                    try:
                        seq = max(seq, int(tail))
                    except ValueError:
                        pass
        except OSError:
            pass
        seq += 1
        self._path = os.path.join(
            directory, "audit-{0}-{1:03d}.jsonl".format(today, seq))
        self._fh = open(self._path, "a", encoding="utf-8")
        self._prune(directory)

    def _prune(self, directory):
        # 保留上限淘汰：失败仅 warning（旁路）
        try:
            files = sorted(
                name for name in os.listdir(directory)
                if name.startswith("audit-") and name.endswith(".jsonl"))
            excess = len(files) - _keep_count()
            for name in files[:max(excess, 0)]:
                os.remove(os.path.join(directory, name))
        except OSError as prune_err:
            logger.warning("audit prune failed: %s", prune_err)

    def _close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def write(self, entry):
        """append 一行 JSON；任何异常吞掉（旁路语义）。"""
        try:
            now = time.monotonic()
            if (self._fh is None
                    or self._last_write is None
                    or (now - self._last_write) > _segment_seconds()):
                self._open_new_segment()
            self._last_write = now
            self._fh.write(
                json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
        except Exception as write_err:
            # 磁盘满 / 权限等：仅 warning，不影响调用方
            logger.warning("audit write failed: %s", write_err)
            self._close()
            self._last_write = None


_writer = _SegmentWriter()


def record(tool, ok, duration_ms, args_summary,
           error_code=None, error_message=None):
    """构造并落盘一行审计（旁路，绝不抛出）。

    字段：ts（ISO8601 毫秒）/ session_id / tool / ok / duration_ms /
    args（≤256 截断）；失败时附 error_code / error_message（截断）。
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "session_id": SESSION_ID,
        "tool": str(tool)[:_TOOL_NAME_MAX],
        "ok": bool(ok),
        "duration_ms": int(max(duration_ms, 0)),
        "args": str(args_summary or "")[:_ARGS_SUMMARY_MAX],
    }
    if error_code is not None:
        entry["error_code"] = str(error_code)[:_TOOL_NAME_MAX]
        entry["error_message"] = str(error_message or "")[:_ARGS_SUMMARY_MAX]
    _writer.write(entry)


def install_audit_hook(fastmcp_obj):
    """包装 ``fastmcp_obj._tool_manager.call_tool`` 安装审计（幂等）。

    与既有 lessons capture hook 链式共存（先装者在内层）。返回是否
    新安装。
    """
    manager = getattr(fastmcp_obj, "_tool_manager", None)
    if manager is None or getattr(manager, "_audit_hook_installed", False):
        return False
    original = manager.call_tool

    async def _audited_call_tool(name, arguments, context=None,
                                 convert_result=False):
        start = time.monotonic()
        args_summary = summarize_args(arguments)
        try:
            result = await original(name, arguments, context=context,
                                    convert_result=convert_result)
        except Exception as exc:
            record(name, ok=False,
                   duration_ms=int((time.monotonic() - start) * 1000),
                   args_summary=args_summary,
                   error_code="tool_exception",
                   error_message="{0}: {1}".format(
                       exc.__class__.__name__, exc))
            raise
        ok, error_code, error_message = classify_result(result)
        record(name, ok=ok,
               duration_ms=int((time.monotonic() - start) * 1000),
               args_summary=args_summary,
               error_code=error_code, error_message=error_message)
        return result

    _audited_call_tool.__name__ = getattr(original, "__name__", "call_tool")
    _audited_call_tool.__doc__ = getattr(original, "__doc__", None)
    manager.call_tool = _audited_call_tool
    manager._audit_hook_installed = True
    return True
