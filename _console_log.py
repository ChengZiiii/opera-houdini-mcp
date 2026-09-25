# -*- coding: utf-8 -*-
"""Console 输出环形缓冲（feat-mcp-console-log-audit §1）。

数据流：
- ``install_tee()`` 把 ``sys.stdout`` / ``sys.stderr`` 包成 ``ConsoleTee``
  （透传原流 + 按 ``\\n`` 分帧写 ``ConsoleRing``）。
- execute_code 的响应捕获路径（``_common._run_code_sync`` 的
  ``redirect_stdout``）会整体替换 ``sys.stdout``、绕过 tee——由其调用侧
  在执行结束后经 ``append_capture()`` 把捕获文本行级同步入 ring
  （spec「execute_code 输出同步入缓冲」场景）。
- 读取走 ``query()``（分页 / tail 尾读 / last_seconds 时间窗过滤）。

约束：
- 幂等：包装对象带 ``_mcp_console_tee`` 标记，二次 install 跳过已包装
  流（热重启 SOP 下 purge sys.modules 后 fresh import 时 sys.stdout
  未被替换，标记天然防重）。
- 总开关：``HOUDINI_MCP_CONSOLE_LOG`` falsy（0/false/no/off）时
  install_tee 完全不包装，get_console_log 恒返回空集。
- 覆盖边界（诚实声明）：tee 位于 Python 层，仅捕获 Python 侧输出；
  C++ 层直写 console 的输出不在保证范围。

env：
- ``HOUDINI_MCP_CONSOLE_LOG_LINES``：环形缓冲行数，默认 4000（非正数或
  解析失败回退默认）。
"""

import os
import sys
import time
import threading
from collections import deque
from datetime import datetime


def _env_lines():
    """环形缓冲容量（行）。"""
    raw = os.environ.get("HOUDINI_MCP_CONSOLE_LOG_LINES", "")
    try:
        value = int(str(raw).strip())
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return 4000


def _tee_enabled():
    """总开关（默认开）。"""
    raw = os.environ.get("HOUDINI_MCP_CONSOLE_LOG", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


class ConsoleRing(object):
    """有界环形缓冲：完整行 (ts, stream, text)，deque(maxlen) 自动淘汰。"""

    def __init__(self, maxlen):
        self._lock = threading.Lock()
        self._entries = deque(maxlen=maxlen)
        self._partial = {}

    def append_text(self, stream_name, text):
        """流式写入：按 ``\\n`` 分帧，末尾未满行留 partial 待拼接。"""
        if not text:
            return
        with self._lock:
            buf = self._partial.get(stream_name, "") + text
            lines = buf.split("\n")
            keep = lines.pop()
            now = time.time()
            for line in lines:
                self._entries.append((now, stream_name, line))
            self._partial[stream_name] = keep

    def append_lines(self, stream_name, text):
        """整段写入（append_capture 用）：已完成文本，行级直接入 ring。"""
        if not text:
            return
        with self._lock:
            now = time.time()
            for line in text.splitlines():
                self._entries.append((now, stream_name, line))

    def snapshot(self):
        with self._lock:
            return list(self._entries)

    def clear(self):
        """清空缓冲，返回清空前条数。"""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._partial.clear()
            return count

    def __len__(self):
        with self._lock:
            return len(self._entries)


class ConsoleTee(object):
    """file-like 包装：透传原流 + 分帧写 ring。未知属性透传原流。"""

    def __init__(self, stream, ring, stream_name):
        # 只设常用属性；_mcp_console_tee 是幂等标记（getattr 探测用），
        # 不下划线存储避免 __getattr__ 递归。
        self.__dict__["_tee_stream"] = stream
        self.__dict__["_tee_ring"] = ring
        self.__dict__["_tee_name"] = stream_name
        self.__dict__["_mcp_console_tee"] = True

    def write(self, text):
        try:
            self.__dict__["_tee_ring"].append_text(
                self.__dict__["_tee_name"], text)
        except Exception:
            pass  # 缓冲故障不得影响透传
        return self.__dict__["_tee_stream"].write(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)
        return None

    def flush(self):
        return self.__dict__["_tee_stream"].flush()

    def isatty(self):
        try:
            return self.__dict__["_tee_stream"].isatty()
        except Exception:
            return False

    def close(self):
        # console 流不可真关；透传给原流会破坏解释器输出，no-op。
        return None

    def __getattr__(self, item):
        return getattr(self.__dict__["_tee_stream"], item)


_ring = ConsoleRing(_env_lines())


def install_tee():
    """幂等安装 stdout/stderr tee。返回 (wrapped_stdout, wrapped_stderr)。

    总开关关闭时返回 (False, False) 且不包装任何流。
    """
    wrapped = [False, False]
    if not _tee_enabled():
        return (False, False)
    for index, attr in enumerate(("stdout", "stderr")):
        current = getattr(sys, attr, None)
        if current is None:
            continue
        if getattr(current, "_mcp_console_tee", False):
            continue
        setattr(sys, attr, ConsoleTee(current, _ring, attr))
        wrapped[index] = True
    return (wrapped[0], wrapped[1])


def append_capture(stream_name, text):
    """公开接口：execute_code 响应捕获路径同步文本入 ring。"""
    _ring.append_lines(stream_name, text)


def tee_installed():
    """当前 stdout 是否已被本模块包装（诊断用）。"""
    return bool(getattr(sys.stdout, "_mcp_console_tee", False))


def query(offset=0, limit=200, tail=None, last_seconds=None):
    """读取缓冲并过滤。返回 (rows, total, filter_mode)。

    - ``last_seconds`` 优先于 ``tail``（spec 锁定）；两者都未给时分页。
    - rows 每项 ``{"ts": epoch, "ts_iso": ISO8601 毫秒, "stream", "text"}``。
    - 参数合法性（负数 / 非数值）由 server handler 层校验，本函数只做
      防御性 int() 转换。
    """
    entries = _ring.snapshot()
    if last_seconds is not None:
        cutoff = time.time() - float(last_seconds)
        entries = [e for e in entries if e[0] >= cutoff]
        mode = "last_seconds"
        total = len(entries)
    elif tail:
        # total 反映过滤后全集（切片前），tail 只影响返回窗口
        mode = "tail"
        total = len(entries)
        entries = entries[-int(tail):]
    else:
        mode = "paged"
        total = len(entries)
    start = max(int(offset), 0)
    count = max(int(limit), 0)
    rows = [
        {
            "ts": entry[0],
            "ts_iso": datetime.fromtimestamp(entry[0]).isoformat(
                timespec="milliseconds"),
            "stream": entry[1],
            "text": entry[2],
        }
        for entry in entries[start:start + count]
    ]
    return rows, total, mode


def clear():
    """清空缓冲，返回清空前条数。"""
    return _ring.clear()
