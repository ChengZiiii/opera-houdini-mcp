#!/usr/bin/env python
"""
houdini_mcp_server.py

This is the "bridge" or "driver" script that Claude will run via `uv run`.
It uses the MCP library (fastmcp) to communicate with Claude over stdio,
and relays each command to the local Houdini plugin on port 9876.
"""
import sys
import os
import time
import argparse
import shutil
import signal
import subprocess
import threading
import uuid

# 内嵌 Python 受 _pth 控制，启动独立脚本时不会自动把脚本目录加进
# sys.path。这里显式把脚本所在目录 prepend 进去，确保 sibling 模块
# （如 _render_policy）在 standalone 启动方式下也能被平铺 import 找到。
# 不影响走 ``-m houdinimcp.houdini_mcp_server`` 或 test_tools.py 主动
# sys.path.insert 的场景。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Get the directory where the script is located (needed for dotenv path)
script_dir = _HERE

import json
import socket
import struct
import logging
from dataclasses import dataclass
from typing import Dict, Any, List
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP, Context
import asyncio

# --- OPUS RapidAPI moved to optional _opus module ---
# refactor-opus-optional-and-debt-cleanup：原 OPUS imports / setup
# （requests / dotenv / langchain）已全部迁出 bridge 顶层到独立可选模块
# ``_opus.py``。bridge 顶层不再 import 这些，使无 RapidAPI key（亦无
# langchain）的默认安装仍能启动并注册所有 MCP tool。只有四个真正发
# RapidAPI 请求的 wrapper 委托 ``_opus``；``opus_get_model_names`` 无 key
# 可用；``opus_import_model_url`` 原地保留为 Houdini relay，不依赖 ``_opus``。
HOUDINI_CONNECTION_TIMEOUT = 300  # 5 minutes for Houdini operations (rendering can be slow)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HoudiniMCP_StdioServer")

# --- Render policy enforcement (fork-render-policy-redirect-and-consent) ---
# 入口校验 helper：6 个 render tool 共享，opengl 走 redirect，karma_* 走
# interrupt + consent token。其他 renderer（mantra / 未知值）原样放行。
# 设计契约：返回 ``{"_redirect": ...}`` / ``{"_interrupt": ...}`` 结构化
# dict，bridge 层透传到任何 AI 客户端 SDK，由 agent 框架识别处理。
#
# 注意：相对 import 会被 ``test_tools.py`` 的 flat ``import houdini_mcp_server``
# 模式破坏（无 parent package）。这里先尝试相对 import；fallback 到 flat
# import（hython / test_tools.py 把 fork 根目录加进 sys.path 的场景）。
try:
    from . import _render_policy as _rp
except ImportError:
    import _render_policy as _rp  # type: ignore

try:
    from . import _common as cmn
except ImportError:
    import _common as cmn  # type: ignore

try:
    from . import _best_practices as _bp
except ImportError:
    import _best_practices as _bp  # type: ignore

try:
    from . import _rag as _rag
except ImportError:
    import _rag as _rag  # type: ignore

try:
    from . import _hip_parser as _hip
except ImportError:
    import _hip_parser as _hip  # type: ignore

# OPUS RapidAPI 可选模块（refactor-opus-optional-and-debt-cleanup）。
# 容错加载：package 与 flat 两种布局均尝试；加载失败时仅五个委托给
# ``_opus`` 的 wrapper 返回 module unavailable，``opus_import_model_url``
# relay 与 bridge 其余工具不受影响。
try:
    from . import _opus as _opus
except ImportError:
    try:
        import _opus as _opus  # type: ignore
    except ImportError:
        _opus = None  # type: ignore

RENDER_POLICY_COMMANDS = _rp.RENDER_POLICY_COMMANDS
register_render_policy_command = _rp.register_render_policy_command


# ---------------------------------------------------------------------------
# monitor_render — bridge-only stdlib OS process 查询（design.md §"monitor_render 降级"）
# ---------------------------------------------------------------------------
_MONITOR_RENDERER_BASENAMES = frozenset({
    "husk", "husk.exe",
    "mantra", "mantra.exe",
    "mantra-bin", "mantra-bin.exe",
})


def _apply_render_policy_to_engine(render_engine, karma_engine=None,
                                   consent_token=None, command=None):
    """应用 fork-render-policy-redirect-and-consent 入口校验。

    Args:
        render_engine: ``render_engine`` 参数（``opengl`` / ``karma`` /
            ``mantra``）。
        karma_engine: ``karma_engine`` 参数（``cpu`` / ``gpu``），仅
            ``render_engine == "karma"`` 时有意义。
        consent_token: agent 重调时携带的 token（karma 路径需要）。

    Returns:
        dict_or_None: 命中 redirect / interrupt 时返回对应结构化 dict；
        ``None`` 时表示放行，调用方继续原逻辑。
    """
    params = {
        "render_engine": render_engine,
        "karma_engine": karma_engine,
        "consent_token": consent_token,
    }
    return _rp.evaluate_render_policy_command(
        command or "render_single_view", params)


def _apply_render_policy_to_renderer(renderer, consent_token=None,
                                     command=None):
    """renderer 直接版本（PR 14 render_*_base64 工具用）。"""
    params = {"renderer": renderer, "consent_token": consent_token}
    return _rp.evaluate_render_policy_command(
        command or "render_viewport_base64", params)

# OPUS helper functions（fix_rgb / get_struct_params / format_params /
# create_opus_* / variate_opus_result / get_opus_job_result 等）已全部迁出
# 到独立可选模块 `_opus.py`。bridge 仅保留薄 wrapper 委托。



@dataclass
class HoudiniConnection:
    host: str
    port: int
    sock: socket.socket = None
    protocol_verified: bool = False

    def connect(self) -> bool:
        """Connect to the Houdini plugin (which is listening on self.host:self.port)."""
        if self.sock is not None:
            try:
                self.sock.getpeername()
                return True
            except (OSError, socket.error):
                logger.info("Stale socket detected, reconnecting...")
                self.disconnect()

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Houdini at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Houdini: {str(e)}")
            self.sock = None
            return False

    def disconnect(self):
        """Close socket if open."""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Houdini: {str(e)}")
            self.sock = None
        self.protocol_verified = False

    def send_command(self, cmd_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Send a JSON command to Houdini's server and wait for the JSON response.
        
        Protocol: each message is a 4-byte big-endian length prefix
        followed by that many bytes of UTF-8 JSON.
        
        Returns the parsed Python dict (e.g. {"status": "success", "result": {...}})
        """
        if not self.connect():
            error_msg = f"Could not connect to Houdini on {self.host}:{self.port}."
            logger.error(error_msg)
            return {"status": "error", "message": error_msg, "origin": "mcp_server_connection"}

        if not self.protocol_verified:
            try:
                ping_cmd = {"type": "ping", "params": {}}
                ping_data = json.dumps(ping_cmd).encode("utf-8")
                ping_frame = struct.pack('>I', len(ping_data)) + ping_data
                self.sock.sendall(ping_frame)

                self.sock.settimeout(HOUDINI_CONNECTION_TIMEOUT)
                hdr = b""
                while len(hdr) < 4:
                    chunk = self.sock.recv(4 - len(hdr))
                    if not chunk:
                        raise ConnectionAbortedError("Connection closed during ping handshake.")
                    hdr += chunk
                resp_len = struct.unpack('>I', hdr)[0]
                MAX_MSG_LEN = 50 * 1024 * 1024
                if resp_len > MAX_MSG_LEN:
                    raise ValueError(f"Ping response too large ({resp_len} bytes)")
                resp_payload = b""
                while len(resp_payload) < resp_len:
                    chunk = self.sock.recv(min(resp_len - len(resp_payload), 65536))
                    if not chunk:
                        raise ConnectionAbortedError("Connection closed during ping response transfer.")
                    resp_payload += chunk
                resp = json.loads(resp_payload.decode("utf-8"))
                result = resp.get("result", {})
                if result.get("pong"):
                    self.protocol_verified = True
                    logger.info(f"Protocol handshake verified (v{result.get('protocol', '?')})")
                else:
                    self.protocol_verified = True
                    logger.warning("Ping not recognized by plugin (old version?), but framing protocol works — proceeding")
            except Exception as e:
                logger.error(f"Protocol handshake failed: {str(e)}")
                self.disconnect()
                return {
                    "status": "error",
                    "message": "Houdini plugin protocol mismatch. Please restart the HoudiniMCP server in Houdini using the shelf tool, then retry.",
                    "origin": "mcp_server_protocol_handshake",
                }

        command = {"type": cmd_type, "params": params or {}}
        data_out = json.dumps(command).encode("utf-8")
        frame_out = struct.pack('>I', len(data_out)) + data_out

        try:
            self.sock.sendall(frame_out)
            logger.info(f"Sent command to Houdini: {command}")

            self.sock.settimeout(HOUDINI_CONNECTION_TIMEOUT)
            header = b""
            while len(header) < 4:
                chunk = self.sock.recv(4 - len(header))
                if not chunk:
                    raise ConnectionAbortedError("Connection closed by Houdini before sending response header.")
                header += chunk

            msg_len = struct.unpack('>I', header)[0]
            MAX_MSG_LEN = 50 * 1024 * 1024
            if msg_len > MAX_MSG_LEN:
                raise ValueError(f"Response too large ({msg_len} bytes)")

            payload = b""
            while len(payload) < msg_len:
                chunk = self.sock.recv(min(msg_len - len(payload), 65536))
                if not chunk:
                    raise ConnectionAbortedError("Connection closed by Houdini during response transfer.")
                payload += chunk

            decoded = payload.decode("utf-8")
            parsed = json.loads(decoded)
            logger.info(f"Received response from Houdini: {parsed}")
            return parsed

        except socket.timeout:
            error_msg = "Timeout receiving data from Houdini."
            logger.error(error_msg)
            self.disconnect()
            return {"status": "error", "message": error_msg, "origin": "mcp_server_send_command_timeout"}
        except Exception as e:
            error_msg = f"Error during Houdini communication for command '{cmd_type}': {str(e)}"
            logger.error(error_msg)
            self.disconnect()
            return {"status": "error", "message": error_msg, "origin": "mcp_server_send_command"}


_HEADLESS_LOCK_SUFFIX = ".lock"
_HEADLESS_RUNTIME_SUFFIX = ".runtime.json"
_HEADLESS_DIR_NAME = ".headless"
_HEADLESS_LOG_MAX_BYTES = 1024 * 1024
_HEADLESS_LOG_BACKUPS = 2
_HEADLESS_MAX_MESSAGE = 50 * 1024 * 1024
_HEADLESS_PROCESSES = {}
_HEADLESS_DRAIN_THREADS = {}


def _env_truthy(name):
    return str(os.environ.get(name, "")).strip().lower() in (
        "1", "true", "yes", "on")


def _env_dir():
    """返回 embedded env 根目录；不创建依赖目录。"""
    return os.path.join(os.path.dirname(_HERE), "houdinimcp-env")


def _headless_dir():
    directory = os.path.join(_env_dir(), _HEADLESS_DIR_NAME)
    os.makedirs(directory, exist_ok=True)
    return directory


def _headless_safe_host(host):
    value = str(host or "127.0.0.1")
    return "".join(ch if ch.isalnum() or ch in "._-" else "_"
                   for ch in value)


def _headless_key(host, port):
    return "{0}-{1}".format(_headless_safe_host(host), int(port))


def _headless_path(host, port, suffix):
    return os.path.join(
        _headless_dir(), _headless_key(host, port) + suffix)


def _headless_log_path(host, port):
    log_dir = os.path.join(_headless_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, _headless_key(host, port) + ".log")


def _headless_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _headless_write_lock(host, port, token):
    path = _headless_path(host, port, _HEADLESS_LOCK_SUFFIX)
    metadata = {
        "pid": os.getpid(),
        "owner_token": str(token),
        "host": str(host),
        "port": int(port),
        "created_at": time.time(),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return True


def _headless_metadata_matches(value, host, port, token, pid=None):
    if not isinstance(value, dict):
        return False
    if str(value.get("owner_token", "")) != str(token):
        return False
    if str(value.get("host", "")) != str(host):
        return False
    try:
        if int(value.get("port")) != int(port):
            return False
    except (TypeError, ValueError):
        return False
    if pid is not None:
        try:
            if int(value.get("pid")) != int(pid):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _headless_release_lock(host, port, token):
    path = _headless_path(host, port, _HEADLESS_LOCK_SUFFIX)
    value = _headless_read_json(path)
    if not _headless_metadata_matches(value, host, port, token):
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _headless_pid_alive(pid):
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if value == os.getpid():
        return True
    try:
        os.kill(value, 0)
        return True
    except (ProcessLookupError, FileNotFoundError):
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _headless_stale_lock_seconds():
    try:
        value = float(os.environ.get(
            "HOUDINI_MCP_HEADLESS_STALE_LOCK_SECONDS", "30"))
    except (TypeError, ValueError):
        value = 30.0
    return max(5.0, min(600.0, value))


def _headless_lock_is_stale(host, port, now=None):
    path = _headless_path(host, port, _HEADLESS_LOCK_SUFFIX)
    value = _headless_read_json(path)
    if not isinstance(value, dict):
        return False
    if str(value.get("host", "")) != str(host):
        return False
    try:
        if int(value.get("port")) != int(port):
            return False
        created_at = float(value.get("created_at"))
    except (TypeError, ValueError):
        return False
    token = str(value.get("owner_token", ""))
    if not token:
        return False
    current = time.time() if now is None else float(now)
    if current - created_at < _headless_stale_lock_seconds():
        return False
    return not _headless_pid_alive(value.get("pid"))


def _headless_recover_stale_lock(host, port):
    path = _headless_path(host, port, _HEADLESS_LOCK_SUFFIX)
    if not _headless_lock_is_stale(host, port):
        return False
    first = _headless_read_json(path)
    if not isinstance(first, dict):
        return False
    second = _headless_read_json(path)
    if not isinstance(second, dict):
        return False
    if first.get("owner_token") != second.get("owner_token"):
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _headless_runtime_owned(host, port, token):
    value = _headless_read_json(
        _headless_path(host, port, _HEADLESS_RUNTIME_SUFFIX))
    return _headless_metadata_matches(value, host, port, token)


def _headless_clear_token_state(host, port, token):
    removed = False
    for suffix in (_HEADLESS_RUNTIME_SUFFIX, _HEADLESS_LOCK_SUFFIX):
        path = _headless_path(host, port, suffix)
        value = _headless_read_json(path)
        if not _headless_metadata_matches(value, host, port, token):
            continue
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            removed = True
        except OSError:
            pass
    return removed


def _find_hython():
    """按显式配置、HFS 和 PATH 顺序找到真实 hython 可执行文件。"""
    candidates = []
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
        if os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return os.path.abspath(resolved)
        if shutil.which(candidate):
            return shutil.which(candidate)
    raise FileNotFoundError(
        "未找到 hython；设置 HOUDINI_MCP_HYTHON 或 HFS 后重试")


def _headless_recv_exact(sock, size):
    chunks = []
    remaining = int(size)
    while remaining:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            raise ConnectionAbortedError("headless server 在 framed response 前断开")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _headless_protocol_ping(host, port, timeout=0.5):
    """对已监听端口执行真实 4-byte framed ping。"""
    sock = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.settimeout(timeout)
        payload = json.dumps({"type": "ping", "params": {}}).encode("utf-8")
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        header = _headless_recv_exact(sock, 4)
        size = struct.unpack(">I", header)[0]
        if size > _HEADLESS_MAX_MESSAGE:
            return False
        response = json.loads(_headless_recv_exact(sock, size).decode("utf-8"))
        return bool((response.get("result") or {}).get("pong"))
    except (OSError, ValueError, TypeError, ConnectionError,
            json.JSONDecodeError):
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _headless_port_listening(host, port, timeout=0.25):
    sock = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _headless_readiness_timeout():
    try:
        value = float(os.environ.get(
            "HOUDINI_MCP_HEADLESS_START_TIMEOUT", "30"))
    except (TypeError, ValueError):
        value = 30.0
    return max(5.0, min(120.0, value))


def _wait_for_headless_ready(host, port, timeout=None, process=None):
    """先观察 listen，再以 framed ping 作为唯一 readiness。"""
    wait_seconds = (_headless_readiness_timeout()
                    if timeout is None else max(5.0, min(120.0, float(timeout))))
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        if _headless_port_listening(host, port):
            if _headless_protocol_ping(host, port, timeout=0.5):
                return True
        time.sleep(0.1)
    return False


class _HeadlessRotatingLog(object):
    """无依赖、有界的 1MB/3 份 headless 输出日志。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def _rotate(self):
        oldest = self.path + ".{0}".format(_HEADLESS_LOG_BACKUPS)
        try:
            if os.path.exists(oldest):
                os.remove(oldest)
        except OSError:
            pass
        for index in range(_HEADLESS_LOG_BACKUPS - 1, 0, -1):
            source = self.path + ".{0}".format(index)
            target = self.path + ".{0}".format(index + 1)
            try:
                if os.path.exists(source):
                    os.replace(source, target)
            except OSError:
                pass
        try:
            if os.path.exists(self.path):
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def write(self, data):
        if not data:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        if len(data) > _HEADLESS_LOG_MAX_BYTES:
            data = data[-_HEADLESS_LOG_MAX_BYTES:]
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            try:
                current_size = os.path.getsize(self.path)
            except OSError:
                current_size = 0
            if current_size and current_size + len(data) > _HEADLESS_LOG_MAX_BYTES:
                self._rotate()
            with open(self.path, "ab") as handle:
                handle.write(data)
                handle.flush()


def _drain_headless_output(process, writer):
    stream = getattr(process, "stdout", None)
    if stream is None:
        return
    read_chunk = getattr(stream, "read1", None)
    try:
        while True:
            chunk = (read_chunk(4096) if callable(read_chunk)
                     else stream.read(4096))
            if not chunk:
                break
            writer.write(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _start_headless_process(host, port, token):
    hython = _find_hython()
    command = [
        hython,
        os.path.join(_HERE, "headless_host.py"),
        "--host", str(host),
        "--port", str(int(port)),
        "--owner-token", str(token),
        "--idle-seconds", str(_headless_idle_seconds()),
    ]
    process = subprocess.Popen(
        command,
        cwd=_HERE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    writer = _HeadlessRotatingLog(_headless_log_path(host, port))
    thread = threading.Thread(
        target=_drain_headless_output, args=(process, writer),
        name="houdini-mcp-headless-log-drain", daemon=True)
    thread.start()
    _HEADLESS_PROCESSES[token] = process
    _HEADLESS_DRAIN_THREADS[token] = thread
    return process


def _headless_log_tail(host, port, max_chars=2000):
    path = _headless_log_path(host, port)
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(-min(handle.tell(), 8192), os.SEEK_END)
            data = handle.read()
        text = data.decode("utf-8", errors="replace")
    except OSError:
        return ""
    return text[-int(max_chars):]


def _headless_idle_seconds():
    try:
        value = float(os.environ.get(
            "HOUDINI_MCP_HEADLESS_IDLE_SECONDS", "300"))
    except (TypeError, ValueError):
        value = 300.0
    return max(30.0, min(86400.0, value))


def _headless_start_failure(host, port, token, reason):
    drain = _HEADLESS_DRAIN_THREADS.get(token)
    if drain is not None and drain is not threading.current_thread():
        drain.join(timeout=1.0)
    tail = _headless_log_tail(host, port, max_chars=2000)
    _headless_clear_token_state(host, port, token)
    message = "无法启动 headless Houdini daemon {0}:{1}: {2}".format(
        host, port, reason)
    if tail:
        message += "\nheadless log tail:\n" + tail
    return ConnectionError(message)


def _ensure_headless_daemon(host="127.0.0.1", port=9876):
    """串行化并等待共享 headless daemon，不拥有其退出生命周期。"""
    if _env_truthy("HOUDINIMCP_NO_HEADLESS"):
        raise ConnectionError(
            "headless Houdini 已由 HOUDINIMCP_NO_HEADLESS 禁用")
    timeout = _headless_readiness_timeout()
    deadline = time.monotonic() + timeout
    token = uuid.uuid4().hex
    process = None
    while time.monotonic() < deadline:
        if _headless_protocol_ping(host, port, timeout=0.5):
            return True
        lock_acquired = _headless_write_lock(host, port, token)
        if lock_acquired:
            try:
                # 获锁后必须二次探测，避免竞争窗口重复 Popen。
                if _headless_protocol_ping(host, port, timeout=0.5):
                    return True
                if _headless_port_listening(host, port):
                    remaining = max(5.0, deadline - time.monotonic())
                    if _wait_for_headless_ready(
                            host, port, timeout=remaining):
                        return True
                    raise RuntimeError("已有进程占用端口但 framed ping 未就绪")
                process = _start_headless_process(host, port, token)
                remaining = max(5.0, deadline - time.monotonic())
                if _wait_for_headless_ready(
                        host, port, timeout=remaining, process=process):
                    return True
                if process.poll() is not None:
                    raise RuntimeError(
                        "hython 提前退出，returncode={0}".format(
                            process.returncode))
                raise RuntimeError("等待 framed ping readiness 超时")
            except Exception as error:
                raise _headless_start_failure(
                    host, port, token, str(error))
            finally:
                # host 成功时已经释放；失败时仅释放自己的 token lock。
                _headless_release_lock(host, port, token)
        else:
            if _headless_protocol_ping(host, port, timeout=0.5):
                return True
            if not _headless_port_listening(host, port):
                _headless_recover_stale_lock(host, port)
        time.sleep(0.1)
    raise _headless_start_failure(
        host, port, token, "启动/等待超时")


def _shutdown_headless_daemon(host, port, owner_token, pid=None):
    """按 runtime metadata 的 PID+token 显式停止 daemon。"""
    if pid is None:
        return False
    value = _headless_read_json(
        _headless_path(host, port, _HEADLESS_RUNTIME_SUFFIX))
    if not _headless_metadata_matches(value, host, port, owner_token,
                                      pid=pid):
        return False
    target_pid = value.get("pid")
    if not _headless_pid_alive(target_pid):
        return False
    try:
        os.kill(int(target_pid), signal.SIGTERM)
        return True
    except (OSError, TypeError, ValueError):
        return False


def shutdown_headless_daemon(host, port, owner_token, pid=None):
    """显式运维入口；拒绝旧 token 或旧 PID。"""
    return _shutdown_headless_daemon(host, port, owner_token, pid=pid)


# A global Houdini connection object
_houdini_connection: HoudiniConnection = None
_houdini_port: int = 9876  # Default port; override with --port

def get_houdini_connection() -> HoudiniConnection:
    """Get or create a persistent HoudiniConnection object."""
    global _houdini_connection
    if _houdini_connection is None:
        logger.info(f"Creating new HoudiniConnection on port {_houdini_port}.")
        _houdini_connection = HoudiniConnection(host="127.0.0.1", port=_houdini_port)

    # Always try to connect, returns True if already connected or successful now
    if not _houdini_connection.connect():
         host, port = _houdini_connection.host, _houdini_connection.port
         _houdini_connection = None
         if not _env_truthy("HOUDINIMCP_NO_HEADLESS"):
             _ensure_headless_daemon(host, port)
             connection = HoudiniConnection(host=host, port=port)
             if connection.connect():
                 _houdini_connection = connection
                 return _houdini_connection
         raise ConnectionError(
             f"Could not connect to Houdini on {host}:{port}. "
              "Is the plugin running or is headless startup disabled?")

    return _houdini_connection


# Now define the MCP server that Claude will talk to over stdio
mcp = FastMCP(
    "HoudiniMCP",
    # refactor-opus-optional-and-debt-cleanup：mcp 1.12.2 接收但忽略
    # ``description=``（不会成为 client-visible instructions）；mcp >=1.12.3
    # 显式拒绝 ``description``。改用正式参数 ``instructions=``，使原 metadata
    # 文本经 MCP initialize 协议对 client 可见，并消除升级时的构造错误。
    instructions="A bridging server that connects Claude to Houdini via MCP stdio + TCP, with OPUS API integration."
)

@asynccontextmanager
async def server_lifespan(app: FastMCP):
    """Startup/shutdown logic. Called automatically by fastmcp."""
    logger.info("Houdini MCP server starting up (stdio).")
    # Attempt to connect right away? Or lazily on first call? Lazy seems safer.
    # try:
    #     get_houdini_connection()
    #     logger.info("Successfully connected to Houdini on startup.")
    # except Exception as e:
    #     logger.warning(f"Could not connect to Houdini on startup: {e}")
    #     logger.warning("Make sure Houdini is running with the plugin on port 9876.")
    yield {} # Context is empty for now
    logger.info("Houdini MCP server shutting down.")
    global _houdini_connection
    if _houdini_connection is not None:
        _houdini_connection.disconnect()
        _houdini_connection = None
    logger.info("Connection to Houdini closed.")

mcp.lifespan = server_lifespan


# -------------------------------------------------------------------
# Houdini Event Tools（bridge-only names；server registry 使用 pending 命令）
# -------------------------------------------------------------------
@mcp.tool()
def get_houdini_events(ctx, limit=100, cursor=None):
    """分页拉取 Houdini 进程级事件；cursor 由上一页响应返回。"""
    return _houdini_call("get_pending_events", {
        "limit": limit,
        "cursor": cursor,
    })


@mcp.tool()
def subscribe_houdini_events(ctx, types=None):
    """订阅事件类型；省略 types 时订阅当前支持的全部事件。"""
    return _houdini_call("subscribe_events", {"types": types})


@mcp.tool()
def unsubscribe_houdini_events(ctx, types=None):
    """取消指定事件订阅；省略 types 时清空全部订阅。"""
    return _houdini_call("unsubscribe_events", {"types": types})


# -------------------------------------------------------------------
# Best Practices Knowledge Base Tool（bridge-local；不建立 Houdini 连接）
# 放在 event tools 与 Original Houdini Tools 之间的无 # PR 探针区，
# 避免被任何 "# PR N" bounded AST 探针扫到。
# -------------------------------------------------------------------
@mcp.tool()
def get_best_practices(ctx, query=None, category=None, id=None):
    """查询 fork 人工审查的 BEST_PRACTICES advisory recipes（bridge-local）。

    本工具 **不建立 Houdini TCP 连接**，直接在 bridge 进程内加载并查询
    BEST_PRACTICES.md。recipe 是 advisory，不替代 verify_hou_api /
    get_houdini_help，也不替代目标 Houdini 版本的 live verification。

    参数说明：
    - query: 可选，对 problem/symptom/fix/category/source 做 casefold
      子串匹配。
    - category: 可选，精确匹配 category 字段。
    - id: 可选，精确匹配 recipe id（如 "BP-001"）。
    多个参数组合为 AND。

    返回统一 envelope：status（success/error）、practices（实际返回列表）、
    total_indexed（过滤前索引数）、matched_count（过滤命中数）、
    returned_count（cap 后实际返回数，恒等于 len(practices)）、truncated
    （matched > returned 时为 true）。error 时 error={code,message,details}，
    且 practices 为空、三个 count 为 0。响应整体过 apply_response_cap。
    """
    return _bp.get_best_practices(
        query=query, category=category, bp_id=id,
        response_cap_fn=getattr(cmn, "apply_response_cap", None))


# -------------------------------------------------------------------
# BM25 Doc RAG Tools（bridge-local；不建立 Houdini 连接）
# 与 get_best_practices 同置于 event tools 与 Original Houdini Tools
# 之间的无 # PR 探针区，避免被任何 "# PR N" bounded AST 探针扫到。
# 本工具与 get_houdini_help / verify_hou_api 互补：那两个面向单条
# API / 节点结构化查询（local-help-first + 在线回退，本 change 未改）；
# search_docs / get_doc 面向跨文档主题检索，只读已校验本地 JSON 索引。
# -------------------------------------------------------------------
@mcp.tool()
def search_docs(ctx, query, limit=10):
    """跨 Houdini 文档做 BM25 检索（bridge-local，无 Houdini 连接）。

    本工具 **不建立 Houdini TCP 连接**，直接在 bridge 进程内加载并查询
    本地 RAG 索引（``index.v1.json``）。与 ``get_houdini_help`` /
    ``verify_hou_api`` 互补：那两个面向单条 API / 节点的结构化查询
    （local-help-first + 在线回退），本工具面向「跨文档主题检索」，
    如「怎么搭 pyro 网络」「karma 采样设置」。

    参数说明：
    - query: 检索文本；tokenizer 会保留 hou.xxx() API 与 /obj/geo1 节点
      路径整体语义，下划线复合词同时按完整与拆分匹配。
    - limit: 可选，返回条目上限，clamp 到 [1, 50]，默认 10。

    返回统一 envelope：status（success/error）、query、limit、matched
    （response cap 前所有 BM25 正分文档总数）、returned（cap 后实际
    results 长度，恒等于 len(results)）、results（每条含 path / title /
    score / 围绕首个命中位置的 snippet）。索引缺失返回
    rag_index_missing；损坏 / 不兼容返回 rag_index_unavailable；命中
    stale 缓存时附 _index_warning。响应整体过 apply_response_cap。
    """
    return _rag.search_docs(
        query=query, limit=limit,
        response_cap_fn=getattr(cmn, "apply_response_cap", None))


@mcp.tool()
def get_doc(ctx, path):
    """按相对路径取回已索引文档的全文（bridge-local，无 Houdini 连接）。

    本工具 **不建立 Houdini TCP 连接**，``path`` 只与已校验索引中的规范
    化 POSIX 相对 path 做精确匹配，全文从 JSON 内嵌 content 返回，
    **绝不拼接源文件系统路径或回读索引外文件**（含 ``..`` 遍历串）。
    与 ``get_houdini_help`` / ``verify_hou_api`` 互补：那两个面向在线
    结构化字段查询，本工具面向已索引离线文档的整篇读取。

    参数说明：
    - path: 索引中文档的 POSIX 相对路径（如 ``nodes/sop/box.html``），
      可先用 search_docs 取得。

    返回统一 envelope：status（success/error）、path、title、length、
    content（全文，过 apply_response_cap）、returned（1 命中 / 0 未命中）。
    path 不存在或非法返回 rag_doc_not_found；索引缺失 / 损坏分别返回
    rag_index_missing / rag_index_unavailable。
    """
    return _rag.get_doc(
        path=path,
        response_cap_fn=getattr(cmn, "apply_response_cap", None))


@mcp.tool()
def parse_hip_offline(ctx, file_path, include_params=False, max_depth=10):
    """离线 best-effort 解析 .hip/.hiplc/.hipnc（bridge-local，无 Houdini 连接）。

    本工具 **不建立 Houdini TCP 连接、不 import hou**，直接在 bridge 进程
    内按真实 legacy cpio/odc entry 流式读取 archive（magic 070707、76 字节
    header），best-effort 提取节点 type、def comment/position/connections、
    可选 parm 序列化原始值、postit 文本与 netbox label，并对不可信输入施加
    file/entry/section/total/node 五类硬限额（``max_depth`` 仅裁输出树）。

    与 ``serialize_scene`` / ``get_node_info`` 互补：那两个面向**在线** Houdini
    连接的实时节点树查询；本工具面向**离线**文件审计（如查看一个 .hip 里
    有哪些节点/连线，而不必启动 Houdini）。

    参数说明：
    - file_path: ``.hip``/``.hiplc``/``.hipnc`` 文件路径；其他扩展名返回
      ``unsupported_extension``。
    - include_params: 可选，True 时每节点附 ``parameters``（archive 中序列化
      的原始 parm 文本，**不求值、不比较默认、不保证动画/表达式完整**）。
    - max_depth: 可选，输出 ``structure`` 树深度，clamp 到 ``[1,64]``；
      flat ``nodes``/``connections`` 不受影响。

    返回统一 envelope：``status``（success/error）、``file_path``、
    ``save_version``（从 ``.variables`` 的 ``_HIP_SAVEVERSION`` 明确提取，
    不可得为 null）、``nodes``（flat）、``connections``、``postits``、
    ``netboxes``、``structure``（depth-clipped 树）、``metadata``（含
    complete_entries/bytes_consumed/trailer_seen/duplicate_entries/
    skipped_sections/limits）。error 形如
    ``error:{code,message,details}`` 并尽可能附 partial（trailer_seen=false，
    不含截断 body）。error code：unsupported_extension / invalid_archive /
    corrupt_archive / truncated_archive / resource_limit_exceeded /
    hip_not_found / hip_io_error。success 与 error-partial 均**过**
    ``apply_response_cap``。
    """
    return _hip.parse_hip_offline(
        file_path, include_params=include_params, max_depth=max_depth,
        response_cap_fn=getattr(cmn, "apply_response_cap", None))


# -------------------------------------------------------------------
# Original Houdini Tools (Get/Create Node, Execute Code)
# -------------------------------------------------------------------
@mcp.tool()
def get_scene_info(ctx: Context) -> str:
    """
    Ask Houdini for scene info. Returns JSON as a string.
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("get_scene_info")
        # response should look like {"status": "success", "result": {...}} or {"status": "error", ...}
        if response.get("status") == "error":
            # Include origin if available
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}): {response.get('message', 'Unknown error')}"
        return json.dumps(response.get("result", {}), indent=2) # Return empty dict if no result
    except ConnectionError as e:
         return f"Connection Error getting scene info: {str(e)}"
    except Exception as e:
        # Catch-all for unexpected errors in this function
        logger.error(f"Unexpected error in get_scene_info tool: {str(e)}", exc_info=True)
        return f"Server Error retrieving scene info: {str(e)}"

@mcp.tool()
def create_node(ctx: Context, node_type: str, parent_path: str = "/obj", name: str = None) -> str:
    """
    Create a new node in Houdini.
    """
    try:
        conn = get_houdini_connection()
        params = { "node_type": node_type, "parent_path": parent_path }
        if name: params["name"] = name
        response = conn.send_command("create_node", params)

        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}): {response.get('message', 'Unknown error')}"
        # Assuming result contains node info like {'name': ..., 'path': ..., 'type': ...}
        return f"Node created: {json.dumps(response.get('result', {}), indent=2)}"
    except ConnectionError as e:
         return f"Connection Error creating node: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in create_node tool: {str(e)}", exc_info=True)
        return f"Server Error creating node: {str(e)}"

@mcp.tool()
def execute_houdini_code(ctx: Context, code: str,
                          policy: str = "normal",
                          allow_dangerous: bool = False,
                          allow_heavy_geometry: bool = False,
                          capture_diff: bool = False) -> str:
    """
    Execute arbitrary Python code in Houdini's environment. LAST RESORT:
    prefer the dedicated tools (connect_nodes, set_parameters, create_wrangle,
    get_geometry_info, ...) — they validate input, report structured errors
    and are undoable as a single step. Use this only for operations no
    dedicated tool covers.

    Args:
        code: Python source to exec inside Houdini.
        policy: "read-only" / "normal" / "privileged" (PR 4 safety policy).
        allow_dangerous: explicit per-call dangerous-code override (privileged only).
        allow_heavy_geometry: explicit per-call heavy-geometry override.
        capture_diff: when True, server snapshots scene state before & after.

    Returns status, any stdout/stderr, and an optional audit block.
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("execute_code", {
            "code": code,
            "policy": policy,
            "allow_dangerous": allow_dangerous,
            "allow_heavy_geometry": allow_heavy_geometry,
            "capture_diff": capture_diff,
        })

        # Handle Houdini-side errors first (could be connection error or execution error)
        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}): {response.get('message', 'Unknown error')}"

        # Handle success case (response should have status=success and a result dict)
        result = response.get("result", {}) # Default to empty dict
        if result.get("executed"): # Check if executed flag is True
            stdout = result.get("stdout", "").strip()
            stderr = result.get("stderr", "").strip()
            audit = result.get("_audit")

            output_message = "Code executed successfully."
            if stdout:
                output_message += f"\n--- Stdout ---\n{stdout}"
            if stderr:
                output_message += f"\n--- Stderr ---\n{stderr}"
            if audit:
                output_message += "\n--- Audit ---\n" + json.dumps(audit, indent=2)
            return output_message
        elif result.get("blocked"):
            # PR 4 policy rejection: server returned blocked dict
            reason = result.get("reason", "blocked by policy")
            output_message = "Execution blocked: " + reason
            hits = result.get("hits") or {}
            if hits:
                output_message += "\n--- Hits ---\n" + json.dumps(hits, indent=2)
            audit = result.get("_audit")
            if audit:
                output_message += "\n--- Audit ---\n" + json.dumps(audit, indent=2)
            return output_message
        else:
            # Unexpected success response format or executed flag missing/false
            logger.warning(f"execute_houdini_code received success status but unexpected result format: {response}")
            return f"Execution status unclear from Houdini response: {json.dumps(response)}"

    except ConnectionError as e:
         return f"Connection Error executing code: {str(e)}"
    except Exception as e:
        # Errors during communication or parsing in this script
        logger.error(f"Unexpected error in execute_houdini_code tool: {str(e)}", exc_info=True)
        return f"Server Error executing code: {str(e)}"


@mcp.tool()
def get_last_scene_diff(ctx: Context) -> str:
    """Return the last execute_code (capture_diff=True) scene before/after diff.

    The Houdini-side server caches the most recent serialize_scene_state pair;
    this tool fetches and pretty-prints the diff so the agent can verify what
    a privileged execution actually changed in the scene.
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("get_last_scene_diff", {})

        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}): {response.get('message', 'Unknown error')}"

        result = response.get("result", {}) or {}
        if not result.get("available", False):
            return ("No scene diff available yet. Run execute_houdini_code "
                    "with capture_diff=True first.")
        # Server (server.py get_last_scene_diff) returns
        # {available, changed, before, after}; align bridge field reads.
        payload = {
            "available": result.get("available"),
            "changed": result.get("changed"),
            "before": result.get("before"),
            "after": result.get("after"),
        }
        return json.dumps(payload, indent=2)
    except ConnectionError as e:
         return f"Connection Error getting scene diff: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in get_last_scene_diff tool: {str(e)}", exc_info=True)
        return f"Server Error getting scene diff: {str(e)}"


@mcp.tool()
def save_scene(ctx: Context, file_path: str) -> str:
    """Save the current Houdini scene to file_path.

    Returns JSON like {"saved": true, "file_path": "..."} or an error string.
    """
    return _houdini_call("save_scene", {"file_path": file_path})


@mcp.tool()
def load_scene(ctx: Context, file_path: str) -> str:
    """Load a .hip file as the current Houdini scene.

    Server-side also calls cmn.invalidate_all_caches() so downstream caches
    (NodeTypeCache coming in PR 6) reset on scene switch.
    """
    return _houdini_call("load_scene", {"file_path": file_path})


@mcp.tool()
def new_scene(ctx: Context) -> str:
    """Reset Houdini to an empty scene (suppress_save_prompt=True).

    Server-side also calls cmn.invalidate_all_caches().
    """
    return _houdini_call("new_scene", {})


@mcp.tool()
def serialize_scene(ctx: Context, root_path: str = "/obj",
                    include_params: bool = False,
                    max_depth: int = 10) -> str:
    """递归序列化 root_path 下的节点树为 dict。

    只读操作，AI 用于场景结构对比 / 文档生成。
    include_params=False 时每节点只含 path/type/name/children；
    True 时增加 parameters dict。
    """
    return _houdini_call("serialize_scene", {
        "root_path": root_path,
        "include_params": include_params,
        "max_depth": max_depth,
    })


# -------------------------------------------------------------------
# PR 6: Node Discovery & Cache Management Tools
# -------------------------------------------------------------------
@mcp.tool()
def list_node_types(ctx: Context, category: str = None,
                    name_filter: str = None, limit: int = 50,
                    cursor: int = None) -> dict:
    """List Houdini node types with optional category / name filter, paginated.

    PR 6: relays to server-side disc.list_node_types, which populates the
    NodeTypeCache on first call and reuses it across invocations.
    """
    return _houdini_call("list_node_types", {
        "category": category,
        "name_filter": name_filter,
        "limit": limit,
        "cursor": cursor,
    })


@mcp.tool()
def list_children(ctx: Context, node_path: str = "/",
                  recursive: bool = False, max_depth: int = 5,
                  max_nodes: int = 1000, compact: bool = False,
                  limit: int = 50, cursor: int = None) -> dict:
    """List the children of node_path. With recursive=True walk the subtree up
    to max_depth. compact=True returns only {path, type, children_count}.

    PR 6: relays to server-side disc.list_children.
    """
    return _houdini_call("list_children", {
        "node_path": node_path,
        "recursive": recursive,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "compact": compact,
        "limit": limit,
        "cursor": cursor,
    })


@mcp.tool()
def find_nodes(ctx: Context, root_path: str = "/", pattern: str = None,
               node_type: str = None, limit: int = 50,
               cursor: int = None) -> dict:
    """Find nodes under root_path matching a glob / substring pattern or
    node_type. Default root_path is "/".

    PR 6: relays to server-side disc.find_nodes.
    """
    return _houdini_call("find_nodes", {
        "root_path": root_path,
        "pattern": pattern,
        "node_type": node_type,
        "limit": limit,
        "cursor": cursor,
    })


@mcp.tool()
def manage_cache(ctx: Context, action: str = "stats") -> dict:
    """Manage the Houdini-side NodeTypeCache.

    action="stats"     -> return cache hits/misses/size/last_populated_at
    action="invalidate"-> clear all registered caches (calls
                          cmn.invalidate_all_caches under the hood)
    action="warmup"    -> pre-populate the NodeTypeCache

    PR 6: relays to server-side disc.manage_cache. ValueError on unknown
    action surfaces as an error dict with origin="houdini".
    """
    return _houdini_call("manage_cache", {"action": action})

# -------------------------------------------------------------------
# Graph Editing & Introspection Tools
# -------------------------------------------------------------------

def _houdini_call(cmd_type: str, params: Dict[str, Any] = None) -> dict:
    """Relay a command to Houdini and normalize the response envelope."""
    try:
        conn = get_houdini_connection()
        response = conn.send_command(cmd_type, params or {})
    except ConnectionError as e:
        return {"status": "error", "message": str(e), "origin": "connection"}
    except Exception as e:
        logger.error(f"Bridge error relaying '{cmd_type}': {e}", exc_info=True)
        return {"status": "error", "message": str(e), "origin": "mcp_bridge"}

    if response.get("status") == "error":
        return {
            "status": "error",
            "message": response.get("message", "Unknown error"),
            "origin": response.get("origin", "houdini"),
        }
    return {"status": "success", "result": response.get("result", {})}


_BATCH_DEFAULT_MAX_OPERATIONS = 50
_BATCH_MIN_OPERATIONS = 1
_BATCH_MAX_OPERATIONS = 200


def _batch_operation_limit():
    """读取 batch 上限并限制在协议允许范围内。"""
    raw_value = os.environ.get(
        "HOUDINI_MCP_BATCH_MAX_OPERATIONS",
        str(_BATCH_DEFAULT_MAX_OPERATIONS))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = _BATCH_DEFAULT_MAX_OPERATIONS
    return max(_BATCH_MIN_OPERATIONS, min(_BATCH_MAX_OPERATIONS, value))


def _batch_validation_error(message, requested=0):
    """构造不执行任何 operation 的 batch 错误并应用 response cap。"""
    return cmn.apply_response_cap({
        "status": "error",
        "requested": requested,
        "executed": 0,
        "succeeded": 0,
        "failed": 0,
        "results": [],
        "error": message,
    })


def _validate_batch_operations(operations):
    """只校验 batch 结构；不复制 dispatcher handler registry。"""
    if not isinstance(operations, list):
        return _batch_validation_error("operations must be a list")
    requested = len(operations)
    limit = _batch_operation_limit()
    if requested > limit:
        return _batch_validation_error(
            "operations exceeds batch limit {0}".format(limit), requested)
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            return _batch_validation_error(
                "operation {0} must be an object".format(index), requested)
        command_type = operation.get("type")
        if not isinstance(command_type, str) or not command_type.strip():
            return _batch_validation_error(
                "operation {0} type must be a non-empty string".format(index),
                requested)
        if command_type == "batch":
            return _batch_validation_error(
                "nested batch operations are not allowed", requested)
        if "params" in operation and not isinstance(operation["params"], dict):
            return _batch_validation_error(
                "operation {0} params must be an object".format(index),
                requested)
    return None


def _batch_policy_control(policy_result, index, command_type, requested):
    """保留 policy payload 并补齐 capped batch control envelope。"""
    control = dict(policy_result)
    control.setdefault("status", "error")
    control["requested"] = requested
    control["executed"] = 0
    control["succeeded"] = 0
    control["failed"] = 0
    control["results"] = []
    control["operation_index"] = index
    control["operation_type"] = command_type
    return cmn.apply_response_cap(control)


def _preflight_batch_render_policy(operations):
    """在任何 TCP relay 前完整扫描 registry 命中的 render operations。"""
    for index, operation in enumerate(operations):
        command_type = operation["type"]
        if command_type not in RENDER_POLICY_COMMANDS:
            continue
        policy_result = _rp.evaluate_render_policy_command(
            command_type, operation.get("params", {}))
        if policy_result is not None:
            return _batch_policy_control(
                policy_result, index, command_type, len(operations))
    return None


@mcp.tool()
def batch(ctx: Context, operations: List[Dict[str, Any]],
          continue_on_error: bool = True) -> dict:
    """按顺序执行一批既有 Houdini command。

    batch 只做一次 TCP relay；bridge 先完整预检 render policy，任何
    redirect / interrupt / blocked response 都不会触发连接或前序 mutation。
    Houdini 端按 mutating segment 合并 undo；batch 不提供事务回滚，结果逐项
    报告。默认最多 50 项，上限可由环境变量调整并 clamp 到 1..200。
    """
    validation_error = _validate_batch_operations(operations)
    if validation_error is not None:
        return validation_error
    if not isinstance(continue_on_error, bool):
        return _batch_validation_error(
            "continue_on_error must be a boolean", len(operations))

    policy_control = _preflight_batch_render_policy(operations)
    if policy_control is not None:
        return policy_control

    try:
        connection = get_houdini_connection()
        response = connection.send_command("batch", {
            "operations": operations,
            "continue_on_error": continue_on_error,
        })
    except ConnectionError as error:
        return _batch_validation_error(str(error), len(operations))
    except Exception as error:
        logger.error("Unexpected error relaying batch: %s", error,
                     exc_info=True)
        return _batch_validation_error(str(error), len(operations))

    if not isinstance(response, dict):
        return cmn.apply_response_cap({
            "status": "error",
            "requested": len(operations),
            "executed": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "error": "Houdini returned a non-object batch response",
        })
    if response.get("status") == "error":
        return cmn.apply_response_cap(response)
    result = response.get("result", response)
    return cmn.apply_response_cap(result)


@mcp.tool()
def connect_nodes(ctx: Context, from_path: str, to_path: str,
                  input_index: int = 0, output_index: int = 0) -> dict:
    """
    Wire one node's output into another node's input. Both nodes must live in
    the same network. input_index selects which input of to_path to connect
    (0-based); output_index selects which output of from_path to use.
    """
    return _houdini_call("connect_nodes", {
        "from_path": from_path,
        "to_path": to_path,
        "input_index": input_index,
        "output_index": output_index,
    })


@mcp.tool()
def disconnect_node_input(ctx: Context, path: str, input_index: int = 0) -> dict:
    """Disconnect one input of a node (reports what it was connected to)."""
    return _houdini_call("disconnect_input", {"path": path, "input_index": input_index})


@mcp.tool()
def delete_node(ctx: Context, path: str) -> dict:
    """Delete a node from the scene by path."""
    return _houdini_call("delete_node", {"path": path})


@mcp.tool()
def set_parameters(ctx: Context, path: str, parameters: Dict[str, Any]) -> dict:
    """
    Set one or more parameters on a node in a single undoable call.
    Values: scalar for single parms (e.g. {"scale": 2.0}), a list for parm
    tuples (e.g. {"t": [0, 1, 0]}), and menu token or label strings for menu
    parms. Unknown names fail per-parameter with did-you-mean suggestions —
    check the "failed" list in the result. Use get_parameter_schema first if
    unsure of names, types or valid menu values.
    """
    return _houdini_call("set_parameters", {"path": path, "parameters": parameters})


@mcp.tool()
def get_parameter_schema(ctx: Context, path: str, pattern: str = None,
                         offset: int = 0, limit: int = 50) -> dict:
    """
    Describe a node's parameters: names, labels, types, tuple sizes, current
    values, defaults, numeric ranges and menu options. Use this to discover
    valid parameter names/values before calling set_parameters. Filter with a
    glob pattern (e.g. "*scale*", matched against name and label); paginate
    with offset/limit when a node has many parameters.
    """
    params = {"path": path, "offset": offset, "limit": limit}
    if pattern:
        params["pattern"] = pattern
    return _houdini_call("get_parameter_schema", params)


@mcp.tool()
def set_node_flags(ctx: Context, path: str, display: bool = None,
                   render: bool = None, bypass: bool = None,
                   template: bool = None) -> dict:
    """
    Set node flags (display/render/bypass/template). Only flags you pass are
    changed. Flags a node type doesn't support are reported as 'unsupported'.
    """
    return _houdini_call("set_node_flags", {
        "path": path, "display": display, "render": render,
        "bypass": bypass, "template": template,
    })


@mcp.tool()
def layout_network(ctx: Context, path: str) -> dict:
    """Auto-layout all children of a network node for a tidy graph."""
    return _houdini_call("layout_children", {"path": path})


# PR 11 Error Nodes
# ---------------------------------------------------------------------------


@mcp.tool()
def find_error_nodes(ctx, root_path="/", include_warnings=True,
                     max_warnings=50, max_errors=None):
    """扫描场景中的错误与警告节点。

    从 root_path 出发，单次调用 node.allSubChildren() 收集所有后代节点，
    返回 errors 与 warnings 双列表。include_warnings 默认 True（PR 11 行为）；
    max_warnings 限制警告条目数（超过返 _warnings_truncated 标记）；
    max_errors 限制错误条目数（None 表示不限）。适合场景构建完成后做
    一次性体检，比逐节点 cook_node 更快。
    """
    return _houdini_call("find_error_nodes", {
        "root_path": root_path,
        "include_warnings": include_warnings,
        "max_warnings": max_warnings,
        "max_errors": max_errors,
    })


# ---------------------------------------------------------------------------
# PR 12 Geometry Summary (thin relay to server-side _geo_summary)
# ---------------------------------------------------------------------------


@mcp.tool()
def get_geo_summary(ctx, node_path, max_points_for_full=1000000,
                    sample_size=10):
    """获取几何节点的轻量级概要信息。

    返回 SOP 节点的 point / primitive / vertex 计数、bbox 6 元、attributes /
    groups 列表（带 name/type/size），以及前 sample_size 个点的属性采样。
    point_count 超过 max_points_for_full 时自动降级 — 跳过 sample_points 与
    详细 attributes/groups，避免大几何撑爆 MCP。比 get_geometry_info 更轻，
    比 get_geometry_data 更结构化。适用于“先看看节点生成了什么规模的几何”。
    """
    return _houdini_call("get_geo_summary", {
        "node_path": node_path,
        "max_points_for_full": max_points_for_full,
        "sample_size": sample_size,
    })


@mcp.tool()
def cook_node(ctx: Context, path: str) -> dict:
    """
    Force-cook a node and report whether it cooked cleanly, with errors,
    warnings and cook time. The definitive way to verify a node works.
    """
    return _houdini_call("cook_node", {"path": path})


@mcp.tool()
def create_wrangle(ctx: Context, parent_path: str, vex_code: str,
                   name: str = None, run_over: str = "points",
                   input_node: str = None) -> dict:
    """
    Create an Attribute Wrangle SOP with the given VEX snippet, optionally
    wiring input_node into its first input. run_over: points, primitives,
    vertices, detail or numbers. The node is cooked immediately and the
    result includes a 'validation' report — check it for VEX compile errors
    before building on top. On invalid input the node is removed, never left
    half-configured.
    """
    params = {"parent_path": parent_path, "vex_code": vex_code, "run_over": run_over}
    if name:
        params["name"] = name
    if input_node:
        params["input_node"] = input_node
    return _houdini_call("create_wrangle", params)


@mcp.tool()
def set_wrangle_code(ctx: Context, path: str, vex_code: str,
                     validate: bool = True) -> dict:
    """
    Replace the VEX snippet on an existing wrangle node. With validate=True
    (default) the node is re-cooked and the result includes a 'validation'
    report with any VEX compile errors.
    """
    return _houdini_call("set_wrangle_code", {
        "path": path, "vex_code": vex_code, "validate": validate,
    })


@mcp.tool()
def get_geometry_info(ctx: Context, path: str) -> dict:
    """
    Summarize a node's geometry: point/primitive/vertex counts, bounding box,
    attribute listings per class, and group names. Accepts a SOP path or a
    geometry container (its display SOP is used). Use this to verify what a
    network actually produced instead of judging from a render.
    """
    return _houdini_call("get_geometry_info", {"path": path})


@mcp.tool()
def get_geometry_data(ctx: Context, path: str, element: str = "points",
                      attributes: List[str] = None, start: int = 0,
                      limit: int = 100) -> dict:
    """
    Read actual attribute values from geometry, paginated (limit capped at
    500 — use start to page through large geometry). element: 'points' or
    'primitives'. attributes: names to read (default: P for points); call
    get_geometry_info first to see what exists.
    """
    params = {"path": path, "element": element, "start": start, "limit": limit}
    if attributes:
        params["attributes"] = attributes
    return _houdini_call("get_geometry_data", params)


# -------------------------------------------------------------------
# NEW rendering Tools
# -------------------------------------------------------------------
@mcp.tool()
def render_single_view(ctx: Context,
                       orthographic: bool = False,
                       rotation: List[float] = [0, 90, 0],
                       render_path: str = "C:/temp/",
                       render_engine: str = "opengl",
                       karma_engine: str = "cpu",
                       consent_token: str = None) -> dict:
    """
    IMPORTANT (fork-render-policy-redirect-and-consent):
        在用户机 H21 缺 OGL 3.3 环境下，本工具的 opengl renderer 已被 fork
        强制 redirect 到 ``capture_pane_screenshot(SceneViewer)``（不再
        触发 opengl output node 链路，避免 Houdini 主线程死锁）；karma_cpu /
        karma_xpu renderer 需带 ``consent_token`` 重调，token 在首次调用返
        回的 ``_interrupt`` 字段中获得。详见 ``_render_policy.py``。

    Render a single view inside Houdini and return a structured result dict.

    Returns a dict (carrying renderer / image_path / size_bytes / etc.)
    instead of a string. Pydantic-typed MCP output models reject dicts
    when the return annotation is `str`; this tool is the one that broke
    live with `1 validation error for render_single_viewOutput / result
    Input should be a valid string [type=string_type, input_type=dict]`.
    Server-side always returns a dict; we forward it verbatim and only
    fall back to an error envelope on exception.
    """
    policy_resp = _apply_render_policy_to_engine(
        render_engine, karma_engine, consent_token=consent_token,
        command="render_single_view")
    if policy_resp is not None:
        return policy_resp
    try:
        conn = get_houdini_connection()
        response = conn.send_command("render_single_view", {
            "orthographic": orthographic,
            "rotation": rotation,
            "render_path": render_path,
            "render_engine": render_engine,
            "karma_engine": karma_engine,
            "consent_token": consent_token,
        })

        if response.get("status") == "error":
            origin = response.get("origin", "houdini")
            return {"status": "error", "origin": origin,
                    "message": response.get("message", "Unknown error")}

        result = response.get("result")
        if isinstance(result, dict):
            return result
        return {"status": "unknown", "raw": str(result)}
    except Exception as e:
        logger.error(f"render_single_view failed: {e}", exc_info=True)
        return {"status": "error", "origin": "bridge",
                "message": f"Render failed: {str(e)}"}

@mcp.tool()
def render_quad_views(ctx: Context,
                      render_path: str = "C:/temp/",
                      render_engine: str = "opengl",
                      karma_engine: str = "cpu",
                      consent_token: str = None) -> dict:
    """
    IMPORTANT (fork-render-policy-redirect-and-consent):
        在用户机 H21 缺 OGL 3.3 环境下，本工具的 opengl renderer 已被 fork
        强制 redirect 到 ``capture_pane_screenshot(SceneViewer)``；karma_cpu
        / karma_xpu 需带 ``consent_token`` 重调。详见 ``_render_policy.py``。

    Render 4 canonical views from Houdini and return a structured result dict.

    Returns a dict (4 views × {image_path, size_bytes, ...}) instead of a
    string. See render_single_view docstring for the dict-vs-str Pydantic
    background. The legacy bridge command name is `render_quad_view`
    (singular) — kept for backward compatibility with the server-side
    handler dictionary in opera-houdini-mcp/server.py.
    """
    policy_resp = _apply_render_policy_to_engine(
        render_engine, karma_engine, consent_token=consent_token,
        command="render_quad_view")
    if policy_resp is not None:
        return policy_resp
    try:
        conn = get_houdini_connection()
        response = conn.send_command("render_quad_view", {
            "render_path": render_path,
            "render_engine": render_engine,
            "karma_engine": karma_engine,
            "consent_token": consent_token,
        })

        if response.get("status") == "error":
            origin = response.get("origin", "houdini")
            return {"status": "error", "origin": origin,
                    "message": response.get("message", "Unknown error")}

        result = response.get("result")
        if isinstance(result, dict):
            return result
        return {"status": "unknown", "raw": str(result)}
    except Exception as e:
        logger.error(f"render_quad_views failed: {e}", exc_info=True)
        return {"status": "error", "origin": "bridge",
                "message": f"Render failed: {str(e)}"}

@mcp.tool()
def render_specific_camera(ctx: Context,
                           camera_path: str,
                           render_path: str = "C:/temp/",
                           render_engine: str = "opengl",
                           karma_engine: str = "cpu",
                           consent_token: str = None) -> dict:
    """
    IMPORTANT (fork-render-policy-redirect-and-consent):
        在用户机 H21 缺 OGL 3.3 环境下，本工具的 opengl renderer 已被 fork
        强制 redirect 到 ``capture_pane_screenshot(SceneViewer)``；karma_cpu
        / karma_xpu 需带 ``consent_token`` 重调。详见 ``_render_policy.py``。

    Render from a specific camera path in the Houdini scene.

    Returns a structured dict (renderer / image_path / size_bytes) instead
    of a string. See render_single_view docstring for the dict-vs-str
    Pydantic background.
    """
    policy_resp = _apply_render_policy_to_engine(
        render_engine, karma_engine, consent_token=consent_token,
        command="render_specific_camera")
    if policy_resp is not None:
        return policy_resp
    try:
        conn = get_houdini_connection()
        response = conn.send_command("render_specific_camera", {
            "camera_path": camera_path,
            "render_path": render_path,
            "render_engine": render_engine,
            "karma_engine": karma_engine,
            "consent_token": consent_token,
        })

        if response.get("status") == "error":
            origin = response.get("origin", "houdini")
            return {"status": "error", "origin": origin,
                    "message": response.get("message", "Unknown error")}

        result = response.get("result")
        if isinstance(result, dict):
            return result
        return {"status": "unknown", "raw": str(result)}
    except Exception as e:
        logger.error(f"render_specific_camera failed: {e}", exc_info=True)
        return {"status": "error", "origin": "bridge",
                "message": f"Render failed: {str(e)}"}

# -------------------------------------------------------------------
# NEW OPUS API Tools
# -------------------------------------------------------------------

@mcp.tool()
def opus_get_model_names(ctx: Context) -> List[str]:
    """
    Returns a list of available OPUS component/structure names.
    """
    # 委托 _opus 模块（无 RapidAPI key 也可用，纯硬编码 catalog，不检查配置）。
    # _opus 容错加载失败时返回空 list（module unavailable）。
    if _opus is None:
        return []
    return _opus.get_all_component_names()

@mcp.tool()
def opus_get_model_params_schema(ctx: Context, structure: str) -> dict:
    """
    Retrieves the parameter schema or format instructions for a given OPUS model structure.
    Returns a dictionary, which might contain 'result' (JSON schema) or 'result_format_instructions' (string).
    Check 'statusCode' for success (200) or failure (e.g., 500).
    """
    if not structure:
        return {"statusCode": 400, "error": "Structure name cannot be empty."}
    if _opus is None:
        return {"statusCode": 503, "error": "OPUS module unavailable."}
    # 委托 _opus；配置不全时返回稳定 disabled error（不 import requests/langchain）。
    return _opus.get_formatted_opus_params(structure)

@mcp.tool()
def opus_create_model(ctx: Context, structure: str, parameters: Dict[str, Any], count: int = 1) -> dict:
    """
    Starts a batch job to create one or more 3D models using the OPUS API.
    Requires the model structure name and a dictionary of parameters.
    Returns a dictionary containing the 'batch_id' on success (statusCode 200) or an error message.
    """
    if not structure:
        return {"statusCode": 400, "error": "Structure name cannot be empty."}
    if not isinstance(parameters, dict):
         return {"statusCode": 400, "error": "Parameters must be a valid JSON object (dict)."}
    if not isinstance(count, int) or count < 1:
         return {"statusCode": 400, "error": "Count must be a positive integer."}
         
    if _opus is None:
        return {"statusCode": 503, "error": "OPUS module unavailable."}
    # 委托 _opus；配置不全时返回稳定 disabled error。
    return _opus.create_opus_component(structure, parameters, count)

@mcp.tool()
def opus_variate_model(ctx: Context, result_id: str, count: int = 12) -> dict:
    """
    Starts a batch job to create variations of an existing OPUS model result.
    Requires the result_id of the base model.
    Returns a dictionary containing the 'batch_id' on success (statusCode 200) or an error message.
    """
    if not result_id:
        return {"statusCode": 400, "error": "Result ID cannot be empty."}
    if not isinstance(count, int) or count < 1:
         return {"statusCode": 400, "error": "Count must be a positive integer."}

    if _opus is None:
        return {"statusCode": 503, "error": "OPUS module unavailable."}
    # 委托 _opus；配置不全时返回稳定 disabled error。
    return _opus.variate_opus_result(result_id, count)

# -------------------------------------------------------------------
# NEW Tools Forwarding to Houdini for OPUS Job Handling
# -------------------------------------------------------------------

@mcp.tool()
def opus_check_job_status(ctx: Context, batch_id: str) -> dict:
    """
    Checks the status of an OPUS batch job directly via the API.
    Requires the batch_id returned by opus_create_model or opus_variate_model.
    Returns the JSON response from the OPUS API, including status and potential download URLs, or an error dictionary.
    """
    if not batch_id:
        return {"error": "Batch ID cannot be empty."}
    if _opus is None:
        return {"error": "OPUS module unavailable."}
    # 委托 _opus；配置不全时返回稳定 disabled error。
    return _opus.get_opus_job_result(batch_job_id=batch_id)

@mcp.tool()
def opus_import_model_url(ctx: Context, download_url: str, node_name: str = None) -> str:
    """
    Asks Houdini to download a model (zip containing USD) from a URL and import it into the scene.
    Requires the download URL (likely obtained from opus_check_job_status).
    Optionally specify a base name for the new container node.
    (Houdini needs a corresponding 'import_opus_url' command handler)
    """
    if not download_url:
        return "Error: Download URL cannot be empty."
    try:
        conn = get_houdini_connection()
        params = {"url": download_url}
        # Use provided name or generate one from URL
        if node_name:
             params["node_name"] = node_name
        else:
             try:
                 from urllib.parse import urlparse as _urlparse
                 parsed_name = os.path.splitext(os.path.basename(_urlparse(download_url).path))[0]
                 params["node_name"] = parsed_name if parsed_name else "opus_import"
             except Exception:
                 params["node_name"] = "opus_import"
             
        logger.info(f"Requesting Houdini import: URL={download_url}, NodeName={params['node_name']}")
        # Send command to Houdini's server.py
        response = conn.send_command("import_opus_url", params)

        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}) importing model: {response.get('message', 'Unknown error')}"

        # Assuming success returns a dict in 'result' with import info (e.g., new node path)
        result_data = response.get('result', {})
        return f"Import Result: {json.dumps(result_data)}"

    except ConnectionError as e:
         return f"Connection Error importing model: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in opus_import_model_url tool: {str(e)}", exc_info=True)
        return f"Server Error importing model: {str(e)}"




# ... (rest of existing code, main function etc) ...


# -------------------------------------------------------------------
# PR 8 HScript Tools (thin relay to server-side _hscript)
# -------------------------------------------------------------------
@mcp.tool()
def execute_hscript(ctx, code):
    """在 Houdini 中执行 HScript 命令字符串。

    HScript 是 Houdini 的传统脚本语言（与 Python/HScript 两套接口并存），
    适合执行 `ls`、`cd`、`opset` 等内建命令。调用结果以 stdout / stderr
    形式返回。

    参数说明：
    - code: HScript 命令字符串（如 "cd /obj; ls"）。空字符串 / 纯空白
      会被服务端拒绝并返回错误。

    返回字符串包含 stdout / stderr 两段；连接或服务端出错时返回
    "Error (...): ..." 形式的提示。
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("execute_hscript", {"code": code})

        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return "Error ({0}): {1}".format(
                origin, response.get('message', 'Unknown error'))

        result = response.get("result", {}) or {}
        stdout = (result.get("stdout") or "").rstrip()
        stderr = (result.get("stderr") or "").rstrip()
        output_message = "HScript executed."
        if stdout:
            output_message += "\n--- Stdout ---\n{0}".format(stdout)
        if stderr:
            output_message += "\n--- Stderr ---\n{0}".format(stderr)
        return output_message
    except ConnectionError as e:
        return "Connection Error executing HScript: {0}".format(e)
    except Exception as e:
        logger.error("Unexpected error in execute_hscript tool: {0}".format(e),
                     exc_info=True)
        return "Server Error executing HScript: {0}".format(e)


# -------------------------------------------------------------------
# PR 9 Graph Edit Tools (thin relay to server-side _graph_edit)
# -------------------------------------------------------------------
@mcp.tool()
def reorder_inputs(ctx, node_path, new_order=None, order=None):
    """重新排列节点的输入顺序。

    参数说明：
    - node_path: 目标节点路径。
    - new_order: list of input_index，按新顺序排列（如 [2, 0, 1] 表示把
      原 input 2 移到 input 0，依此类推）。空 list 表示全部断开。
    - order: 旧版别名；若同时传 new_order 与 order，以 new_order 为准。

    返回 dict 包含 path / old_order / new_order / success 四项；
    节点不存在时函数会抛 ValueError，bridge 不会再以 success:True 形式
    静默吞错。
    """
    effective_order = new_order if new_order is not None else order
    return _houdini_call("reorder_inputs", {
        "node_path": node_path, "new_order": effective_order,
    })


@mcp.tool()
def layout_children(ctx, parent_path=None, parent=None,
                    horizontal_spacing=None, vertical_spacing=None,
                    direction=None):
    """布局父节点下的子节点（按间距参数手动 setPosition，跨 Houdini
    版本可移植）。

    参数说明：
    - parent_path: 父节点路径（PR 9 推荐命名）。
    - parent: 旧版别名；若同时传 parent_path 与 parent，以 parent_path 为准。
    - horizontal_spacing: 水平间距（Houdini units），缺省 2.0。
    - vertical_spacing: 垂直间距，缺省 1.5。
    - direction: "horizontal"（默认）或 "vertical"。

    返回 dict 包含 parent_path / children_count / direction / spacing
    四项。后向兼容：现有调用 layout_children(ctx, parent) 仍 work。
    """
    effective_parent = parent_path if parent_path is not None else parent
    if horizontal_spacing is not None or vertical_spacing is not None \
            or direction is not None:
        return _houdini_call("layout_children", {
            "parent_path": effective_parent,
            "horizontal_spacing": horizontal_spacing,
            "vertical_spacing": vertical_spacing,
            "direction": direction,
        })
    return _houdini_call("layout_children", {"parent_path": effective_parent})


@mcp.tool()
def set_node_position(ctx, node_path, x, y):
    """设置节点在 network editor 中的位置。

    参数说明：
    - node_path: 节点路径。
    - x: x 坐标（Houdini units）。
    - y: y 坐标。

    返回 dict 包含 path / position / success 三项；
    节点不存在时函数会抛 ValueError。
    """
    return _houdini_call("set_node_position", {
        "node_path": node_path, "x": x, "y": y,
    })


@mcp.tool()
def set_node_color(ctx, node_path, r, g, b):
    """设置节点颜色（颜色分量自动 clamp 到 [0, 1]）。

    参数说明：
    - node_path: 节点路径。
    - r, g, b: 颜色分量；负值 clamp 为 0.0，>1 值 clamp 为 1.0。

    返回 dict 包含 path / color / success 三项；
    节点不存在时函数会抛 ValueError。
    """
    return _houdini_call("set_node_color", {
        "node_path": node_path, "r": r, "g": g, "b": b,
    })


@mcp.tool()
def create_network_box(ctx, parent_path, name=None, node_paths=None):
    """在父节点下创建 network box（network editor 中的分组框）。

    参数说明：
    - parent_path: 父节点路径。
    - name: 可选，box 名；缺省时由 Houdini 自动命名。
    - node_paths: 可选，要包含到此 box 的节点路径列表；
      不存在的节点静默跳过，不抛错。

    返回 dict 包含 path / name / nodes_in_box 三项；
    父节点不存在时函数会抛 ValueError。
    """
    return _houdini_call("create_network_box", {
        "parent_path": parent_path, "name": name, "node_paths": node_paths,
    })


# -------------------------------------------------------------------
# PR 10 Node Info Tool (thin relay to server-side _node_info)
# -------------------------------------------------------------------
@mcp.tool()
def get_node_info(ctx, node_path, include_errors=True, force_cook=False,
                  include_input_details=False, compact=False):
    """获取节点的详细信息。

    参数说明：
    - node_path: 目标节点路径。
    - include_errors: 可选，是否包含 errors / warnings 字段，默认 True。
    - force_cook: 可选，读取前是否调 node.cook(force=True)，默认 False。
    - include_input_details: 可选，是否包含每个 input 的详细连接
      （用 node.inputConnectors() 一次性取），默认 False。
    - compact: 可选，是否仅返精简字段 path/type/counts（不含 parameters /
      errors / warnings），默认 False。

    返回 dict：compact=True 时仅含 path / type / children_count / input_count
    / output_count 五项；否则包含完整字段（详见 _node_info.get_node_info）。
    节点不存在时函数会抛 ValueError，bridge 透传 error envelope 不静默吞错。
    """
    return _houdini_call("get_node_info", {
        "node_path": node_path,
        "include_errors": include_errors,
        "force_cook": force_cook,
        "include_input_details": include_input_details,
        "compact": compact,
    })


# -------------------------------------------------------------------
# PR 13 Pane Capture Tools
# -------------------------------------------------------------------
@mcp.tool()
def capture_pane_screenshot(ctx, pane_type_name, save_path=None,
                            fit_contents=True):
    """截图指定类型 pane（NetworkEditor / SceneViewer / Compositor /
    ChannelEditor 等 30 种）。

    pane_type_name 必须是 hou.paneTabType 的合法属性名。save_path 为 None
    时不落盘，size_bytes 改用 QBuffer 估算。fit_contents=True 时先按
    pane 类型调用 homeAll() / curViewport().home() 把可视范围对齐。
    响应走 apply_response_cap。无 PySide 环境返回 _warning dict。
    """
    return _houdini_call("capture_pane_screenshot", {
        "pane_type_name": pane_type_name,
        "save_path": save_path,
        "fit_contents": fit_contents,
    })


@mcp.tool()
def list_visible_panes(ctx):
    """列出当前所有 desktop 中可见的 pane tab。

    返回 {desktop, pane_type, name, is_current} 四元组列表；is_current
    标记该 desktop 当前激活的 pane。只读操作，响应过 apply_response_cap。
    """
    return _houdini_call("list_visible_panes", {})


@mcp.tool()
def capture_multiple_panes(ctx, pane_types, save_dir):
    """批量截图多种 pane 到 save_dir（不存在会自动创建）。

    pane_types 是 pane 类型名列表；返回与 pane_types 等长的 result 列表，
    每条 {pane_type, save_path, success, error} 独立报告。任意一种 pane
    抛异常不影响其他 pane。响应过 apply_response_cap。
    """
    return _houdini_call("capture_multiple_panes", {
        "pane_types": pane_types,
        "save_dir": save_dir,
    })


@mcp.tool()
def render_node_network(ctx, node_path, fit_contents=True,
                        save_path=None):
    """定位到节点所在 NetworkEditor pane，cd 到节点，再截图。

    node_path 必须存在；fit_contents=True 时截图前调用 homeAll() 把可视
    范围对齐到节点子树。save_path=None 时不落盘（size_bytes 改用 QBuffer
    估算）。响应过 apply_response_cap。
    """
    return _houdini_call("render_node_network", {
        "node_path": node_path,
        "fit_contents": fit_contents,
        "save_path": save_path,
    })


# -------------------------------------------------------------------
# PR 22 SceneViewer Flipbook Views Tools
# -------------------------------------------------------------------
@mcp.tool()
def capture_sceneviewer_flipbook_views(ctx, views=None, save_dir=None,
                                       desktop_name=None, pane_name=None,
                                       fit_contents=True):
    """采集 SceneViewer 的 Top / Front / Right flipbook，可显式请求 Perspective。

    views=None 时严格按 top、front、right 顺序采集；传入 views 时保留调用方
    顺序且不允许重复或未知视图。每张图由 Houdini 内部 flipbook 生成并校验
    PNG IHDR，返回结构化的逐视图结果与 state_restored 状态。
    """
    return _houdini_call("capture_sceneviewer_flipbook_views", {
        "views": views,
        "save_dir": save_dir,
        "desktop_name": desktop_name,
        "pane_name": pane_name,
        "fit_contents": fit_contents,
    })


# -------------------------------------------------------------------
# PR 14 Render Base64 Tools (placed before PR 7 so existing test_bridge_style
# PR 7 section probe does not pick them up — the probe scans all
# @mcp.tool() after the PR 7 header without an explicit upper bound)
# -------------------------------------------------------------------
@mcp.tool(name="render_viewport_base64")
def render_viewport_base64(ctx, camera_path=None, geometry_path=None,
                           renderer="opengl", resolution=(640, 480),
                           format="PNG", consent_token=None):
    """渲染单个 viewport 视角并以 base64 形式返回图像（PR 14）。

    IMPORTANT (fork-render-policy-redirect-and-consent):
        在用户机 H21 缺 OGL 3.3 环境下，本工具的 ``renderer="opengl"`` 已被
        fork 强制 redirect 到 ``capture_pane_screenshot(SceneViewer)``（返
        回 ``_redirect`` dict，不进实际 render 引擎调用链路）；``karma_cpu``
        / ``karma_xpu`` 需带 ``consent_token`` 重调，token 在首次调用返回
        的 ``_interrupt`` 字段中获得。详见 ``_render_policy.py``。

    renderer 支持 opengl / karma_cpu / karma_xpu 三选一；resolution 为
    (width, height) 元组；format 支持 PNG / JPEG。响应含 image_base64 字段
    与 size_bytes，响应整体过 apply_response_cap 截断大 payload。无 hou /
    PySide 环境返回 _warning dict。
    """
    policy_resp = _apply_render_policy_to_renderer(
        renderer, consent_token=consent_token,
        command="render_viewport_base64")
    if policy_resp is not None:
        return policy_resp
    return _houdini_call("render_viewport_base64", {
        "camera_path": camera_path,
        "geometry_path": geometry_path,
        "renderer": renderer,
        "resolution": list(resolution) if isinstance(resolution, tuple)
        else resolution,
        "format": format,
        "consent_token": consent_token,
    })


@mcp.tool(name="render_quad_views_base64")
def render_quad_views_base64(ctx, geometry_path=None, renderer="opengl",
                              resolution=(480, 360), format="PNG",
                              consent_token=None):
    """渲染四视图（top / front / side / perspective）并以 base64 形式返回
    4 张图（PR 14）。

    IMPORTANT (fork-render-policy-redirect-and-consent):
        opengl 已 redirect 到 ``capture_pane_screenshot(SceneViewer)``；
        karma_cpu / karma_xpu 需带 ``consent_token`` 重调。详见
        ``_render_policy.py``。

    共享 bbox + camera rig，每个视图旋转 null 节点切换视角。响应以
    top/front/side/perspective 四键分别承载 base64 字符串，整体过
    apply_response_cap。无 hou 环境返回 _warning dict。
    """
    policy_resp = _apply_render_policy_to_renderer(
        renderer, consent_token=consent_token,
        command="render_quad_views_base64")
    if policy_resp is not None:
        return policy_resp
    return _houdini_call("render_quad_views_base64", {
        "geometry_path": geometry_path,
        "renderer": renderer,
        "resolution": list(resolution) if isinstance(resolution, tuple)
        else resolution,
        "format": format,
        "consent_token": consent_token,
    })


@mcp.tool(name="render_specific_camera_base64")
def render_specific_camera_base64(ctx, camera_path, resolution=(640, 480),
                                   format="PNG", renderer="opengl",
                                   consent_token=None):
    """渲染指定相机视角并以 base64 形式返回图像（PR 14）。

    IMPORTANT (fork-render-policy-redirect-and-consent):
        opengl 已 redirect 到 ``capture_pane_screenshot(SceneViewer)``；
        karma_cpu / karma_xpu 需带 ``consent_token`` 重调。详见
        ``_render_policy.py``。

    camera_path 必须指向 /obj 下已存在的相机节点；renderer 支持
    opengl / karma_cpu / karma_xpu 三选一。响应整体过 apply_response_cap
    截断大 payload。
    """
    policy_resp = _apply_render_policy_to_renderer(
        renderer, consent_token=consent_token,
        command="render_specific_camera_base64")
    if policy_resp is not None:
        return policy_resp
    return _houdini_call("render_specific_camera_base64", {
        "camera_path": camera_path,
        "resolution": list(resolution) if isinstance(resolution, tuple)
        else resolution,
        "format": format,
        "renderer": renderer,
        "consent_token": consent_token,
    })


# -------------------------------------------------------------------
# PR 19 Animation & Frame Control Tools (placed before PR-16 / PR-15 /
# PR-18 / PR-7 sections so all four pre-existing AST probes
# (test_bridge_style PR-7 / test_connection PR-16 /
# test_verify_hou_api PR-18 / test_help PR-15) which scan strictly
# inside their own header boundaries do not pick these tools up; PR
# 19 ships its own source-level check in test_animation.py). 10 个
# 工具的语义、number / float 校验与 sub-frame 透传契约来自
# _animation 模块；服务器端的分类（2 read-only / 6 mutating / 2
# no-undo）保证 undo group 边界符合设计 D3。
# -------------------------------------------------------------------
@mcp.tool()
def get_frame(ctx):
    """读取当前帧 / 时间 / fps / 三组 range / increment，全部 float（PR 19）。

    返回 dict 字段：frame / time / fps / frame_range /
    playback_range / frame_increment；任一 hou 调用抛异常时降级为
    status=error 而非向调用方抛异常。仅读取时间线状态，不修改场
    景或参数（READ_ONLY_COMMANDS）。响应整体过 server 端
    ``apply_response_cap`` 截断大 payload（虽然规模小，仍保持
    defense-in-depth）。
    """
    return _houdini_call("get_frame", {})


@mcp.tool()
def set_frame(ctx, frame):
    """设置当前帧（PR 19，运行态时间线写，no-undo）。

    ``frame`` 接受 int / float；拒绝 bool / NaN / ±inf / 非数值；
    hou 接受 float 值并保留 sub-frame。任何 hou 异常降级为 error
    dict。该命令在 NO_UNDO_COMMANDS 中，batch dispatcher 会在调
    用前自动关闭当前 undo segment，确保不进入 ``hou.undos.group``。
    """
    return _houdini_call("set_frame", {"frame": frame})


@mcp.tool()
def set_frame_range(ctx, start, end):
    """设置全局 frame range（PR 19，场景写，可 undo）。

    ``start`` / ``end`` 必须为有限浮点且 ``start <= end``；end
    可 sub-frame。错误（如 start > end）返回 status=error 不写；
    成功时由 hou.playbar.setFrameRange 持久化。
    """
    return _houdini_call("set_frame_range",
                         {"start": start, "end": end})


@mcp.tool()
def set_playback_range(ctx, start, end):
    """设置 playback range（PR 19，场景写，可 undo）。

    校验同 ``set_frame_range``；调 ``hou.playbar.setPlaybackRange``。
    """
    return _houdini_call("set_playback_range",
                         {"start": start, "end": end})


@mcp.tool()
def set_keyframe(ctx, path, parameter, frame, value):
    """单关键帧写入（PR 19，场景写，可 undo）。

    ``path`` / ``parameter`` 必为非空字符串；``frame`` / ``value``
    必须为有限浮点。value 创建 ``hou.Keyframe(float(value))`` 并
    ``keyframe.setFrame(float(frame))`` 后 ``parm.setKeyframe``。
    字符串参数 / NaN / inf 等返回 status=error 不写。
    """
    return _houdini_call("set_keyframe", {
        "path": path,
        "parameter": parameter,
        "frame": frame,
        "value": value,
    })


@mcp.tool()
def set_keyframes(ctx, keyframes):
    """批量关键帧写入（PR 19，场景写，可 undo）。

    ``keyframes`` 为 list，每项 dict 至少含 ``path`` /
    ``parameter`` / ``frame`` / ``value``；任一项无效则**整调
    用**失败、零写入（在 server 上层预校验拒绝）。全部有效时
    在单个 ``hou.undos.group`` 内逐项写入并返回 ``set_count`` /
    ``requested``。错误列表同样受 server 端 ``apply_response_cap``
    截断保护。
    """
    return _houdini_call("set_keyframes",
                         {"keyframes": keyframes})


@mcp.tool()
def delete_keyframe(ctx, path, parameter, frame):
    """删除指定帧的关键帧（PR 19，场景写，可 undo）。

    ``frame`` 必须为有限浮点（删除 sub-frame 精确点）。目标帧
    不存在返回 status=error（"no keyframe found at frame ..."），
    不写。实际删除后再次读取 keyframes 列表验证已消失。
    """
    return _houdini_call("delete_keyframe", {
        "path": path,
        "parameter": parameter,
        "frame": frame,
    })


@mcp.tool()
def get_keyframes(ctx, path, parameter):
    """读取 parm 的全部关键帧（PR 19，只读）。

    返回 list 中每项 ``{"frame": float, "value": float}``，不
    做 ``int()`` 截断；空关键帧列表返回 ``keyframes=[]``。本
    工具仅查询状态（READ_ONLY_COMMANDS），不会修改场景或参数。
    """
    return _houdini_call("get_keyframes",
                         {"path": path, "parameter": parameter})


@mcp.tool()
def playbar_control(ctx, action):
    """playbar 播放 / 步进 / 跳转（PR 19，运行态时间线写，no-undo）。

    ``action`` 取值：
    - ``play`` / ``reverse`` / ``stop``：直接调 SideFX HOM 同名方法。
    - ``step_forward`` / ``step_backward``：仅通过
      ``hou.setFrame(current ± hou.playbar.frameIncrement())``
      路径并 clamp 到当前 playback range 闭区间，**不引入其
      他 step helper**（increment 非有限正数 / range 不可用
      → error 且 **不**调 hou.setFrame）。
    - ``goto_start`` / ``goto_end``：直接设 playback range 端点。

    整个 action 集在 NO_UNDO_COMMANDS 中，batch dispatcher 在
    该命令前关闭 undo segment，保证不进入 ``hou.undos.group``。
    """
    return _houdini_call("playbar_control",
                         {"action": action})


@mcp.tool()
def set_expression(ctx, path, parameter, expression,
                   language="hscript"):
    """写入 parm 表达式（PR 19，参数通道持久写，**可 undo**）。

    ``language`` 接受 ``hscript`` / ``python``，映射到对应
    ``hou.exprLanguage``；其他值（包括大小写变体）一律
    status=error。该命令属于参数通道数据写
    （MUTATING_COMMANDS），**不**归为只读或 no-undo；与其他
    关键帧 / 范围写共用 undo group 策略。
    """
    return _houdini_call("set_expression", {
        "path": path,
        "parameter": parameter,
        "expression": expression,
        "language": language,
    })


# -------------------------------------------------------------------
# C9 add-render-workflow-tools（placed between PR 19 and PR 16 so all
# four pre-existing AST probes — test_bridge_style PR 7 / test_help
# PR 15 / test_verify_hou_api PR 18 / test_connection PR 16 — which
# scan strictly inside their own header boundaries do not pick these
# tools up; same convention as PR 19）
# -------------------------------------------------------------------
def _query_renderer_processes_windows():
    """Windows 路径：用 PowerShell + CIM 拿 husk / mantra 进程字段。

    解析失败 / 命令缺失 / 权限不足返回空 list 与详细 warning；不抛异常。
    """
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process -Filter \"Name like 'husk%.exe' or "
        "Name like 'mantra%.exe'\" | "
        "Select-Object ProcessId,Name,CommandLine,"
        "@{n='CPU';e={try {[double]$_.KernelModeTime + [double]$_.UserModeTime} "
        "catch { '' }}},"
        "@{n='WorkingSetSizeMB';e={try {[math]::Round($_.WorkingSetSize/1MB,2)} "
        "catch { '' }}} | "
        "ForEach-Object { [PSCustomObject]@{"
        "ProcessId=$_.ProcessId; Name=$_.Name; "
        "CommandLine=$_.CommandLine; CPU=$_.CPU; "
        "WorkingSetSizeMB=$_.WorkingSetSizeMB} } | "
        "ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=15)
    except (OSError, ValueError) as error:
        return [], ["powershell_exec_failed: {0}".format(error)]
    if completed.returncode != 0:
        return [], ["powershell_exit_code: {0}".format(
            completed.returncode)]
    stdout = (completed.stdout or "").strip()
    if not stdout:
        return [], []
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return [], ["powershell_output_parse_failed"]
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return [], ["powershell_unexpected_shape"]
    results = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        name = entry.get("Name") or ""
        results.append({
            "pid": entry.get("ProcessId"),
            "name": name,
            "command": entry.get("CommandLine") or "",
            "cpu": _as_float_or_null(entry.get("CPU")),
            "memory_mb": _as_float_or_null(entry.get("WorkingSetSizeMB")),
        })
    return results, []


def _query_renderer_processes_posix():
    """POSIX 路径：用 ``ps`` 拿 husk / mantra 进程字段。"""
    ps_command = ["ps", "-ax", "-o", "pid=,comm=,args="]
    try:
        completed = subprocess.run(
            ps_command, capture_output=True, text=True, timeout=15)
    except (OSError, ValueError) as error:
        return [], ["ps_exec_failed: {0}".format(error)]
    if completed.returncode != 0:
        return [], ["ps_exit_code: {0}".format(completed.returncode)]
    results = []
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        basename = os.path.basename(parts[1])
        if basename not in _MONITOR_RENDERER_BASENAMES:
            continue
        results.append({
            "pid": pid,
            "name": basename,
            "command": parts[2],
            "cpu": None,
            "memory_mb": None,
        })
    return results, []


def _as_float_or_null(value):
    """CIM 输出可能是数字、空串或其它；统一为 float 或 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        as_float = float(value)
        if as_float != as_float or as_float in (float("inf"),
                                                  float("-inf")):
            return None
        return as_float
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            as_float = float(stripped)
        except ValueError:
            return None
        if as_float != as_float or as_float in (float("inf"),
                                                  float("-inf")):
            return None
        return as_float
    return None


def _monitor_render_renderer_processes():
    """按平台分发；返回 ``(entries, warnings)``。entries 每项含
    pid / name（必填）/ command；cpu / memory_mb 可得为 float，否则
    None。warnings 列出 cpu_unavailable / memory_unavailable /
    powershell_exec_failed 等降级原因。"""
    if sys.platform.startswith("win"):
        return _query_renderer_processes_windows()
    return _query_renderer_processes_posix()


@mcp.tool()
def monitor_render(ctx: Context) -> dict:
    """在 bridge 进程 best-effort 观察 husk / mantra OS 进程。

    不读 hou、不发 TCP；通过 stdlib ``subprocess`` 调 PowerShell CIM
    （Windows）或 ``ps``（POSIX），按 executable basename
    （``husk`` / ``husk.exe`` / ``mantra`` / ``mantra.exe`` /
    ``mantra-bin`` / ``mantra-bin.exe``）过滤；不把命令行任意
    substring 当 renderer。PID / name 必填；CPU / memory 可得为
    float，不可得为 ``null`` 并在 ``_warning`` 列明
    ``cpu_unavailable`` / ``memory_unavailable``。命令缺失、权限
    不足或解析失败返 ``status=success``、空 / 部分结果与 warning，
    不抛异常。响应整体过 ``apply_response_cap``。

    本工具 **bridge-only**，不进入 server registry 或三个 server
    分类集合。
    """
    entries, warnings = _monitor_render_renderer_processes()
    cpu_unavailable = any(entry.get("cpu") is None for entry in entries)
    memory_unavailable = any(entry.get("memory_mb") is None
                              for entry in entries)
    if cpu_unavailable:
        warnings.append("cpu_unavailable")
    if memory_unavailable and entries:
        warnings.append("memory_unavailable")
    payload = {
        "status": "success",
        "count": len(entries),
        "processes": entries,
    }
    if warnings:
        payload["_warning"] = warnings
    return cmn.apply_response_cap(payload)


@mcp.tool()
def start_render(ctx: Context, node_path: str, policy_renderer: str,
                  frame_range: List[float] = None,
                  consent_token: str = None) -> dict:
    """同步启动一次 ROP 渲染；四层防御见 ``_render_jobs.start_render``。

    Args:
        node_path: 真实 ROP 节点路径（如 ``/out/mantra1``）。
        policy_renderer: 必填提示，bridge Layer 1 用其初筛（``mantra`` /
            ``opengl`` / ``karma_cpu`` / ``karma_xpu``）；不替换真实
            node 推断。
        frame_range: 可选 2 或 3 元 ``[start, end[, inc]]``，缺省走
            ROP 自身设置。
        consent_token: 可选，karma 路径重调时携带。

    Returns:
        dict: 直接 relay server 响应；blocked 时为 redirect / interrupt /
        error 字典；正常完成时为 ``status=success`` 含
        ``state / elapsed / frame_range``。
    """
    preflight = _rp.evaluate_render_policy_command(
        "start_render", {
            "policy_renderer": policy_renderer,
            "consent_token": consent_token,
        })
    if preflight is not None:
        return cmn.apply_response_cap(preflight)
    params = {"node_path": node_path}
    if frame_range is not None:
        params["frame_range"] = list(frame_range)
    if consent_token is not None:
        params["consent_token"] = consent_token
    return _houdini_call("start_render", params)


@mcp.tool()
def list_render_nodes(ctx: Context, parent_path: str = "/out") -> dict:
    """枚举 ``parent_path`` 下可分类 ROP 节点（ifd / opengl / karmarender）。

    响应字段：``parent_path / count / nodes``，每节点含
    ``name / path / type / renderer``。未知 ROP type 仍列出但
    ``renderer=""``。整体过 ``apply_response_cap``。
    """
    return _houdini_call("list_render_nodes", {"parent_path": parent_path})


@mcp.tool()
def get_render_settings(ctx: Context, node_path: str) -> dict:
    """读取 ``node_path`` 的白名单 parm 值（design.md §"设置白名单"）。

    仅返回 ``ifd`` / ``opengl`` / ``karmarender`` 实际存在且数据安全的
    parm；script / callback / command / executable 类型拒绝。整体
    过 ``apply_response_cap``。
    """
    return _houdini_call("get_render_settings", {"node_path": node_path})


@mcp.tool()
def set_render_settings(ctx: Context, node_path: str,
                         parameters: Dict[str, Any]) -> dict:
    """受限可撤销写入（design.md §"set_render_settings"）。

    完整预校验所有 key/value/parm 可写性/prospective engine 后快
    照旧值；应用失败显式恢复快照旧值，**不**依赖 undo 自动 rollback。
    全部成功 -> ``status=success``；恢复成功 ->
    ``status=error, error_code=render_settings_apply_failed,
    restored=true``；任一恢复失败 ->
    ``status=error, error_code=render_settings_restore_failed,
    restored=false`` + ``restore_errors``。响应过
    ``apply_response_cap``。
    """
    return _houdini_call("set_render_settings", {
        "node_path": node_path, "parameters": parameters})


@mcp.tool()
def create_render_node(ctx: Context, node_type: str,
                        parent_path: str = "/out",
                        name: str = None,
                        parameters: Dict[str, Any] = None) -> dict:
    """受限创建可分类 ROP 节点（design.md §"create_render_node"）。

    仅允许 ``ifd`` / ``opengl`` / ``karmarender``；创建后通过同一
    白名单设置参数并校验 renderer 可识别。未知 node type 整体
    error。响应过 ``apply_response_cap``。
    """
    params = {"node_type": node_type, "parent_path": parent_path}
    if name is not None:
        params["name"] = name
    if parameters is not None:
        params["parameters"] = parameters
    return _houdini_call("create_render_node", params)


# -------------------------------------------------------------------
# add-hda-management-tools：10 个 HDA/OTL bridge tool。
# 全部透传 server 端同名命令；不接受 ``override`` /
# ``allow_protected`` / ``authorization`` / 任何隐藏绕过参数。
# 三分类见 server.py：
# - READ_ONLY：hda_list / hda_get / get_hda_sections /
#   get_hda_section_content
# - MUTATING：hda_create / update_hda / set_hda_section_content
# - NO_UNDO：hda_install / uninstall_hda / reload_hda
# -------------------------------------------------------------------
@mcp.tool()
def hda_list(ctx: Context, category: str = None) -> dict:
    """枚举已加载 HDA（add-hda-management-tools，READ_ONLY）。

    使用 ``hou.hda.loadedFiles()`` + ``hou.hda.definitionsInFile()``
    按 ``(libraryFilePath, nameWithCategory())`` 去重。响应过
    server 端 ``apply_response_cap``。``category`` 可选透传过滤。
    """
    params = {}
    if category is not None:
        params["category"] = category
    return _houdini_call("hda_list", params)


@mcp.tool()
def hda_get(ctx: Context, node_type: str) -> dict:
    """读取 definition metadata（add-hda-management-tools，READ_ONLY）。

    ``node_type`` 仅接受 ``hou.NodeType.nameWithCategory()`` 完整
    类别名（如 ``Sop/box``）；短名称 / 未知 / 歧义均返回稳定
    error。响应过 ``apply_response_cap``。
    """
    return _houdini_call("hda_get", {"node_type": node_type})


@mcp.tool()
def hda_install(ctx: Context, file_path: str) -> dict:
    """安装 HDA 库（add-hda-management-tools，NO_UNDO）。

    落盘 + 全局 HDA registry 副作用，**不**可由 Houdini undo 恢复。
    响应过 ``apply_response_cap``。
    """
    return _houdini_call("hda_install", {"file_path": file_path})


@mcp.tool()
def hda_create(ctx: Context, node_path: str, name: str,
                save_path: str, label: str = None) -> dict:
    """从节点创建 HDA（add-hda-management-tools，MUTATING）。

    先 ``canCreateDigitalAsset()``，再
    ``createDigitalAsset(name=, hda_file_name=, description=)``。
    ``label`` 可选，作为 description。响应过 ``apply_response_cap``。
    """
    params = {"node_path": node_path, "name": name,
              "save_path": save_path}
    if label is not None:
        params["label"] = label
    return _houdini_call("hda_create", params)


@mcp.tool()
def uninstall_hda(ctx: Context, file_path: str) -> dict:
    """卸载 HDA 库（add-hda-management-tools，NO_UNDO）。

    落盘 + registry 副作用，**不**可由 Houdini undo 恢复。
    """
    return _houdini_call("uninstall_hda", {"file_path": file_path})


@mcp.tool()
def reload_hda(ctx: Context, file_path: str) -> dict:
    """重载 HDA 库（add-hda-management-tools，NO_UNDO）。

    落盘 + registry 副作用，**不**可由 Houdini undo 恢复。
    """
    return _houdini_call("reload_hda", {"file_path": file_path})


@mcp.tool()
def update_hda(ctx: Context, node_path: str) -> dict:
    """从实例更新定义（add-hda-management-tools，MUTATING）。

    验证节点存在、拥有 definition、实例类型匹配后调
    ``definition.updateFromNode(node)``；**不**使用
    ``definition.save()``。响应过 ``apply_response_cap``。
    """
    return _houdini_call("update_hda", {"node_path": node_path})


@mcp.tool()
def get_hda_sections(ctx: Context, node_type: str) -> dict:
    """枚举 sections metadata（add-hda-management-tools，READ_ONLY）。

    每项含 ``name / size / protected / binary / utf8``；``utf8``
    严格探测；``binary`` 固定 true。响应过 ``apply_response_cap``。
    """
    return _houdini_call("get_hda_sections", {"node_type": node_type})


@mcp.tool()
def get_hda_section_content(ctx: Context, node_type: str,
                             section: str, encoding: str,
                             offset: int = 0,
                             limit: int = 8192) -> dict:
    """分页读取 section 正文（add-hda-management-tools，READ_ONLY）。

    ``encoding`` 显式必填 ``utf8`` / ``base64``；两种模式均以
    ``binaryContents()`` 一次拿到的 raw bytes 为唯一分页真相。
    响应过 ``apply_response_cap``。
    """
    return _houdini_call("get_hda_section_content", {
        "node_type": node_type, "section": section,
        "encoding": encoding, "offset": offset, "limit": limit})


@mcp.tool()
def set_hda_section_content(ctx: Context, node_type: str,
                             section: str, content: str) -> dict:
    """allowlist 写入 section（add-hda-management-tools，MUTATING）。

    ``section`` 仅 ``Help`` / ``IconSVG`` 大小写敏感精确匹配允许；
    其他全部 ``section_write_denied`` 且零写入。``content`` UTF-8
    字节上限 65536。响应过 ``apply_response_cap``。
    """
    return _houdini_call("set_hda_section_content", {
        "node_type": node_type, "section": section, "content": content})


# -------------------------------------------------------------------
# add-geometry-export-and-measure：8 个几何测量/导出 bridge tool。
# 全部透传 server 端同名命令；不接受隐藏绕过参数。三分类见 server.py：
# - MUTATING：set_detail_attrib（创建 Attribute Create SOP，单 undo group）
# - NO_UNDO：geo_export（外部文件系统 mutation，不可 undo）
#            + 6 个 cooked Geometry 查询（get_bounding_box /
#            get_groups / get_group_members / get_attrib_values /
#            get_prim_intrinsics / find_nearest_point；访问 cooked
#            Geometry 可能触发 SOP cook，不可由 HIP undo 恢复）
# -------------------------------------------------------------------
@mcp.tool()
def get_bounding_box(ctx, node_path):
    """解包几何 6 元 bounds 为 ``{min,max,size,center}``
    （add-geometry-export-and-measure，NO_UNDO）。

    使用 ``geo.intrinsicValue("bounds")`` 的
    ``(xmin, xmax, ymin, ymax, zmin, zmax)`` 标准布局。响应过 server
    端 ``apply_response_cap``。
    """
    return _houdini_call("get_bounding_box", {"node_path": node_path})


@mcp.tool()
def get_groups(ctx, node_path):
    """返回四类 groups（point / prim / vertex / edge）name 列表
    （add-geometry-export-and-measure，NO_UNDO）。

    edge groups 在 H21+ 通过 ``geo.edgeGroups()`` 公开。响应过
    server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_groups", {"node_path": node_path})


@mcp.tool()
def get_group_members(ctx, node_path, group_type, group_name,
                      offset=0, limit=1000):
    """分页读取 group 成员
    （add-geometry-export-and-measure，NO_UNDO）。

    - ``offset / limit`` 必填；``limit`` 默认 1000。
    - vertex 成员 ``{prim_index, vertex_index, point_index}``；
      edge 成员 ``[min_point, max_point]`` 排序端点对。
    - 返回 ``{values, offset, limit, total, next_offset}``。响应过
      server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_group_members", {
        "node_path": node_path, "group_type": group_type,
        "group_name": group_name,
        "offset": offset, "limit": limit})


@mcp.tool()
def get_attrib_values(ctx, node_path, attribute, attrib_class="point",
                      offset=0, limit=1000):
    """按 owner/storage/tuple-size 分派读取属性，原生分页
    （add-geometry-export-and-measure，NO_UNDO）。

    - ``attrib_class`` 接受 ``point / prim / vertex / detail``。
    - 返回 ``{values, offset, limit, total, next_offset, storage,
      tuple_size}``。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_attrib_values", {
        "node_path": node_path, "attribute": attribute,
        "attrib_class": attrib_class,
        "offset": offset, "limit": limit})


@mcp.tool()
def get_prim_intrinsics(ctx, node_path, prim_index, names=None):
    """仅查询指定 ``prim_index`` 的 intrinsics
    （add-geometry-export-and-measure，NO_UNDO）。

    ``names`` 可选子集过滤；越界返回结构化 error。响应过 server
    端 ``apply_response_cap``。
    """
    params = {"node_path": node_path, "prim_index": prim_index}
    if names is not None:
        params["names"] = names
    return _houdini_call("get_prim_intrinsics", params)


@mcp.tool()
def find_nearest_point(ctx, node_path, position, max_distance=1.0):
    """最近点查询：``Point | None`` 双路径
    （add-geometry-export-and-measure，NO_UNDO）。

    - ``position`` 必须是 ``[x, y, z]``。
    - ``max_distance`` 默认 1.0，作为 ``geo.nearestPoint`` 的
      ``max_radius``。
    - Point 返回 ``{point_index, point_position, distance}``；None
      返回三字段均 null。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("find_nearest_point", {
        "node_path": node_path, "position": position,
        "max_distance": max_distance})


@mcp.tool()
def set_detail_attrib(ctx, node_path, name, value, attrib_type="float",
                      node_name=None):
    """创建 Attribute Create SOP，class=detail
    （add-geometry-export-and-measure，MUTATING）。

    - 创建 + 连接 + 配置是单 undo group 的连续步骤；失败 destroy
      半成品。
    - **不**调用 cooked ``node.geometry()`` 的写方法。
    - ``attrib_type`` 接受 ``float / int / string / vector``。
    - 响应过 server 端 ``apply_response_cap``。
    """
    params = {"node_path": node_path, "name": name, "value": value,
              "attrib_type": attrib_type}
    if node_name is not None:
        params["node_name"] = node_name
    return _houdini_call("set_detail_attrib", params)


@mcp.tool()
def geo_export(ctx, node_path, format, output_path, overwrite=False):
    """translator 驱动的原子几何导出
    （add-geometry-export-and-measure，NO_UNDO）。

    - ``format`` 接受 ``bgeo / bgeo.gz / bgeo.lzma / bgeo.bz2 / geo``
      （H21+ 实机 ``geo.saveToFile`` 验证）。
    - ``output_path`` 扩展名必须与 format 匹配；不匹配返回
      ``extension_mismatch``。
    - 临时文件 ``fsync`` + ``os.replace`` 原子覆盖；``overwrite=False``
      且目标存在返回 ``target_exists``；失败清理临时文件。
    - 落盘副作用不进 Houdini undo group；响应过 server 端
      ``apply_response_cap``。
    """
    return _houdini_call("geo_export", {
        "node_path": node_path, "format": format,
        "output_path": output_path, "overwrite": overwrite})


# -------------------------------------------------------------------
# add-node-parameter-vex-tools：14 个新增 bridge tool（净新增）。
# 旧 modify_node 已在 server.py 原地扩展 flags，不重写新桥。
# 三分类见 server.py：
# - READ_ONLY：get_parameter / get_expression / get_wrangle_code
# - NO_UNDO：validate_vex（vcc 临时 .vfl + subprocess 副作用）
# - MUTATING：rename_node / copy_node / move_node / set_parameter /
#   revert_parameter / link_parameters / lock_parameter /
#   create_spare_parameter / create_spare_parameters /
#   create_vex_expression
# 风格：与 PR 9 / HDA / geo 保持一致（无类型注解 + 中文 docstring）。
# -------------------------------------------------------------------
@mcp.tool()
def rename_node(ctx, path, new_name):
    """重命名节点（add-node-parameter-vex-tools，MUTATING）。

    预检同名冲突；返回新 path / old_name / new_name。响应过
    ``apply_response_cap``。
    """
    return _houdini_call("rename_node", {
        "path": path, "new_name": new_name})


@mcp.tool()
def copy_node(ctx, src_path, dest_parent, name=None):
    """复制节点到 dest_parent 下（add-node-parameter-vex-tools，MUTATING）。

    使用 ``hou.copyNodesTo``；预检目标 category 与同名冲突。
    ``name`` 可选，None 时由 hou 决定。响应过 ``apply_response_cap``。
    """
    params = {"src_path": src_path, "dest_parent": dest_parent}
    if name is not None:
        params["name"] = name
    return _houdini_call("copy_node", params)


@mcp.tool()
def move_node(ctx, src_path, dest_parent):
    """移动节点到 dest_parent 下（add-node-parameter-vex-tools，MUTATING）。

    使用 ``hou.moveNodesTo``；预检目标 category。响应过
    ``apply_response_cap``。
    """
    return _houdini_call("move_node", {
        "src_path": src_path, "dest_parent": dest_parent})


@mcp.tool()
def get_parameter(ctx, path, parameter):
    """读取 parm 当前值/类型/表达式/时间依赖
    （add-node-parameter-vex-tools，READ_ONLY）。

    返回 ``{value, type, expression, is_time_dependent}``；无 expression
    时 ``expression: None``。响应过 ``apply_response_cap``。
    """
    return _houdini_call("get_parameter", {
        "path": path, "parameter": parameter})


@mcp.tool()
def set_parameter(ctx, path, parameter, value):
    """写 parm 值（add-node-parameter-vex-tools，MUTATING）。

    单 undo group；失败抛 error。响应过 ``apply_response_cap``。
    """
    return _houdini_call("set_parameter", {
        "path": path, "parameter": parameter, "value": value})


@mcp.tool()
def get_expression(ctx, path, parameter):
    """读取 parm 表达式（add-node-parameter-vex-tools，READ_ONLY）。

    返回 ``{expression}``，空表达式时 ``expression: None``。响应过
    ``apply_response_cap``。
    """
    return _houdini_call("get_expression", {
        "path": path, "parameter": parameter})


@mcp.tool()
def revert_parameter(ctx, path, parameter):
    """恢复 parm 至默认值（add-node-parameter-vex-tools，MUTATING）。

    走 ``parm.revertToDefaults()``，单 undo group。响应过
    ``apply_response_cap``。
    """
    return _houdini_call("revert_parameter", {
        "path": path, "parameter": parameter})


@mcp.tool()
def link_parameters(ctx, source, target):
    """建立 parm 之间的真实引用（add-node-parameter-vex-tools，MUTATING）。

    使用 ``Parm.set(Parm)`` / ``setExpression()`` 建跨 parm 引用；
    不用 channel alias 冒充。``source`` / ``target`` 形式
    ``node_path.parm_name``。响应过 ``apply_response_cap``。
    """
    return _houdini_call("link_parameters", {
        "source": source, "target": target})


@mcp.tool()
def lock_parameter(ctx, path, parameter, locked):
    """切换 parm 锁定状态（add-node-parameter-vex-tools，MUTATING）。

    ``locked`` 接受 bool；单 undo group。响应过 ``apply_response_cap``。
    """
    return _houdini_call("lock_parameter", {
        "path": path, "parameter": parameter, "locked": locked})


@mcp.tool()
def create_spare_parameter(ctx, path, name, data_type, label=None,
                            default=None, min_value=None, max_value=None,
                            menu_items=None, menu_labels=None, folder=None,
                            num_components=1):
    """单项 spare 参数创建（add-node-parameter-vex-tools，MUTATING）。

    通过 ``parmTemplateGroup()`` 复制 + 一次性
    ``setParmTemplateGroup()`` 提交。``data_type`` 接受 ``float / int /
    string / toggle / menu``。``folder`` 可选。响应过
    ``apply_response_cap``。
    """
    params = {"path": path, "name": name, "data_type": data_type,
              "num_components": num_components}
    if label is not None:
        params["label"] = label
    if default is not None:
        params["default"] = default
    if min_value is not None:
        params["min_value"] = min_value
    if max_value is not None:
        params["max_value"] = max_value
    if menu_items is not None:
        params["menu_items"] = menu_items
    if menu_labels is not None:
        params["menu_labels"] = menu_labels
    if folder is not None:
        params["folder"] = folder
    return _houdini_call("create_spare_parameter", params)


@mcp.tool()
def create_spare_parameters(ctx, path, parameters, folder=None):
    """批量 spare 参数创建（add-node-parameter-vex-tools，MUTATING）。

    ``parameters`` 是 list of spec dict；先全量校验、失败零部分提交。
    单次 ``setParmTemplateGroup()`` 完成。响应过 ``apply_response_cap``。
    """
    params = {"path": path, "parameters": parameters}
    if folder is not None:
        params["folder"] = folder
    return _houdini_call("create_spare_parameters", params)


@mcp.tool()
def get_wrangle_code(ctx, path):
    """读取 Attribute Wrangle SOP 的 snippet
    （add-node-parameter-vex-tools，READ_ONLY）。

    返回 ``{path, name, type, code}``。响应过 ``apply_response_cap``。
    """
    return _houdini_call("get_wrangle_code", {"path": path})


@mcp.tool()
def validate_vex(ctx, code, context="cvex"):
    """用真实 HFS/bin/vcc 编译 VEX（add-node-parameter-vex-tools，NO_UNDO）。

    临时 ``.vfl`` + ``subprocess.run([vcc, ...], shell=False, timeout=10)``；
    不调用 Python exec / eval / compile / execute_code / hou.hscript /
    hou.vexLint / hou.text.vexSyntaxCheck；不执行编译产物。10 秒超时、
    输出 64KB 上限、finally 清理源与产物。返回
    ``{valid, context, diagnostics: [{severity, line, column, message}]}``。
    响应过 ``apply_response_cap``。
    """
    return _houdini_call("validate_vex", {
        "code": code, "context": context})


@mcp.tool()
def create_vex_expression(ctx, parent_path, code, attrib_class="point",
                           name=None):
    """在 SOP parent 下创建 Attribute Wrangle
    （add-node-parameter-vex-tools，MUTATING）。

    设置 ``snippet`` 与 ``runover``（point / primitive / vertex / detail /
    number）；单 undo group。父节点 category 非 Sop 时返回 error。响应过
    ``apply_response_cap``。
    """
    params = {"parent_path": parent_path, "code": code,
              "attrib_class": attrib_class}
    if name is not None:
        params["name"] = name
    return _houdini_call("create_vex_expression", params)


# -------------------------------------------------------------------
# add-scene-context-selection-materials：9 个净新增 bridge tool。
# 全部透传 server 端同名命令；不引入隐藏 override / 授权参数。
# 三分类见 server.py：
# - READ_ONLY：get_network_overview / get_cook_chain / explain_node /
#   get_scene_summary / get_selection / list_materials /
#   list_material_types
# - MUTATING：create_material_network
# - NO_UNDO：set_selection
# 风格：与 PR 9 / HDA / geo 保持一致（无类型注解 + 中文 docstring）。
# 放置在 PR 7 section header 之前以避免被 test_bridge_style PR 7 probe
# 误识别（与 add-hda-management-tools / add-geometry-export-and-measure
# / add-node-parameter-vex-tools 的放置策略保持一致）。
# -------------------------------------------------------------------
@mcp.tool()
def get_network_overview(ctx, parent_path, max_depth=2, max_nodes=500):
    """有界 BFS 遍历 parent 节点的网络拓扑（add-scene-context-selection-materials，READ_ONLY）。

    ``max_depth`` 控制 BFS 深度（0 = 仅 parent_path），
    ``max_nodes`` 限制 HOM 访问节点预算；返回 ``nodes / edges /
    visited_count / truncated / truncation_reason``。节点去重
    使用 path-based visited，环 / 共享祖先仅记一次。响应过 server
    端 ``apply_response_cap``。
    """
    return _houdini_call("get_network_overview", {
        "parent_path": parent_path,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
    })


@mcp.tool()
def get_cook_chain(ctx, node_path, max_depth=20, max_nodes=500):
    """有界 DFS 上游 cook chain（add-scene-context-selection-materials，READ_ONLY）。

    从 ``node_path`` 沿 ``inputs()`` 关系向上递归；path-based
    visited 在入栈前判定，菱形 / 环自动去重。``max_nodes`` 是
    HOM 访问预算硬约束；触发时 ``truncated=True``。响应过
    server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_cook_chain", {
        "node_path": node_path,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
    })


@mcp.tool()
def explain_node(ctx, node_path, include_params=False, max_params=64):
    """单节点结构化摘要（add-scene-context-selection-materials，READ_ONLY）。

    字段：``path / name / type / category / input_count /
    output_count / inputs / outputs``；``include_params=True`` 时
    附 ``non_default_parameters``（最多 ``max_params`` 条）。仅读
    不修改场景。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("explain_node", {
        "node_path": node_path,
        "include_params": include_params,
        "max_params": max_params,
    })


@mcp.tool()
def get_scene_summary(ctx, max_nodes=2000):
    """全场景 category counts + 时间线（add-scene-context-selection-materials，READ_ONLY）。

    ``max_nodes`` 控制 HOM 遍历预算；返回 ``total_nodes /
    category_counts / frame / fps / start_frame / end_frame /
    truncated / truncation_reason``。不返回完整节点列表，只聚合
    category 分布。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_scene_summary", {
        "max_nodes": max_nodes,
    })


@mcp.tool()
def get_selection(ctx):
    """读取当前节点选择（add-scene-context-selection-materials，READ_ONLY）。

    固定走 ``hou.selectedNodes()``，不接受 ``selectedItems()``；
    因此不混入 network box / note / dot。返回 ``selected / count``，
    每项 ``path / type / category``。响应过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("get_selection", {})


@mcp.tool()
def set_selection(ctx, node_paths, clear_others=True):
    """覆盖节点选择（add-scene-context-selection-materials，NO_UNDO）。

    全量预校验 ``node_paths``，任一无效 → 0 部分改变；clear 仅
    走当前 ``selectedNodes()`` 的 ``setSelected(False)``，**不**
    调 ``clearAllSelected()`` 避免影响 box / note / dot。UI /
    viewport 运行态写，**不**能由 HIP undo 恢复；该命令归
    ``NO_UNDO_COMMANDS``，batch dispatcher 会在执行前关闭 undo
    segment。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("set_selection", {
        "node_paths": node_paths,
        "clear_others": clear_others,
    })


@mcp.tool()
def list_materials(ctx, parent_path="/mat"):
    """枚举 parent 下材质节点（add-scene-context-selection-materials，READ_ONLY）。

    验证 parent 存在且 childTypeCategory 为 Sop（Houdini 21
    真实材质节点归属）；每项 ``path / name / node_type / category``，
    稳定按 path 排序。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("list_materials", {
        "parent_path": parent_path,
    })


@mcp.tool()
def list_material_types(ctx, category="Vop"):
    """枚举材质 category 下的 node types（add-scene-context-selection-materials，READ_ONLY）。

    ``category`` 仅接受 ``Vop`` / ``Shop``；使用对应 category 的
    ``nodeTypes()``，稳定排序返回 ``name / node_type / category /
    description``，``node_type`` 走 ``nameWithCategory()`` 完整
    类别名。未知 / 不支持 category 返 ``unsupported_category``。
    响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("list_material_types", {
        "category": category,
    })


@mcp.tool()
def create_material_network(ctx, parent_path, name="mat"):
    """在 parent 下创建 matnet（add-scene-context-selection-materials，MUTATING）。

    验证 parent 存在、未锁定、childTypeCategory 为 Sop 后调
    ``createNode("matnet", name)``；错误结构化区分
    ``parent_not_found / parent_locked / unsupported_parent_
    category / node_type_unavailable``。此 tool 归
    ``MUTATING_COMMANDS``，server handler 通过
    ``hou.undos.group`` 入 undo group。响应过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("create_material_network", {
        "parent_path": parent_path,
        "name": name,
    })


# -------------------------------------------------------------------
# add-viewport-control-tools: 8 个 viewport 控制 bridge tool
# -------------------------------------------------------------------
# 全部透传到 server 端同名命令；**不**引入隐藏 override / 授权参数。
# 全部归 server NO_UNDO_COMMANDS（UI/view 状态，不可由 HIP undo
# 恢复；batch dispatcher 会在执行前关闭 undo segment）。
# **不**新增截图管线 / 不修改 _pane_capture.py；SceneViewer 截图
# 仍走既有 capture_pane_screenshot + flipbook。
# 放在 PR 16 / PR 15 / PR 18 / PR 7 section header 之前以避免被
# test_bridge_style (PR 7) / test_help (PR 15) / test_verify_hou_api
# (PR 18) / test_connection (PR 16) 四套 AST 探针误识别
# （与 add-hda-management-tools / add-geometry-export-and-measure
# / add-node-parameter-vex-tools / add-scene-context-selection-materials
# 的放置策略保持一致）。
# -------------------------------------------------------------------
@mcp.tool()
def get_viewport_info(ctx):
    """返回当前 SceneViewer viewport schema（add-viewport-control-tools，NO_UNDO）。

    字段：camera / viewport_type / display_set / shaded_mode /
    hydra_renderer。无 GUI / 无 pane 返 ``viewport_unavailable``
    warning。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_viewport_info", {})


@mcp.tool()
def set_viewport_camera(ctx, camera_path):
    """设置 SceneViewer viewport camera（add-viewport-control-tools，NO_UNDO）。

    ``camera_path`` 必须是已存在节点；无效返 ``camera_not_found``。
    仅 UI/view 写，**不**进 undo group。响应过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("set_viewport_camera", {"camera_path": camera_path})


@mcp.tool()
def set_viewport_display(ctx, display_set, shaded_mode):
    """设置 viewport display set + shaded mode（add-viewport-control-tools，NO_UNDO）。

    两个值均为 design.md D2 白名单 token；不接受反射式 setter
    或不存在枚举。仅 UI/view 写，**不**进 undo group。响应过
    server 端 ``apply_response_cap``。
    """
    return _houdini_call("set_viewport_display", {
        "display_set": display_set,
        "shaded_mode": shaded_mode,
    })


@mcp.tool()
def set_viewport_renderer(ctx, renderer):
    """LOP SceneViewer Hydra renderer 切换（add-viewport-control-tools，NO_UNDO）。

    仅 LOP context 可用；非 LOP 返 ``viewport_unavailable`` warning。
    ``renderer`` 必须是 ``sceneViewer.hydraRenderers()`` 中存在的
    identifier；不可用返 ``renderer_unavailable``。响应过 server
    端 ``apply_response_cap``。
    """
    return _houdini_call("set_viewport_renderer", {"renderer": renderer})


@mcp.tool()
def frame_selection(ctx):
    """viewport.frameSelected()（add-viewport-control-tools，NO_UNDO）。

    仅调整视图，**不**截图。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("frame_selection", {})


@mcp.tool()
def frame_all(ctx):
    """viewport.frameAll()（add-viewport-control-tools，NO_UNDO）。

    仅调整视图，**不**截图。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("frame_all", {})


@mcp.tool()
def set_viewport_direction(ctx, direction):
    """将白名单方向映射到 ``geometryViewportType`` 并调 changeType
    （add-viewport-control-tools，NO_UNDO）。

    七方向 token：front/back/left/right/top/bottom/perspective。
    不接受反射式 setter。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("set_viewport_direction", {"direction": direction})


@mcp.tool()
def set_current_network(ctx, path):
    """NetworkEditor.cd(path)（add-viewport-control-tools，NO_UNDO）。

    节点不存在返 ``node_not_found``；无 NetworkEditor pane 返
    ``viewport_unavailable`` warning。响应过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("set_current_network", {"path": path})


# -------------------------------------------------------------------
# add-dops-tools: 8 个 DOP 查询/控制 bridge tool
# -------------------------------------------------------------------
# 6 查询只归 server READ_ONLY；step/reset 只归 NO_UNDO。后二者改变
# 全局时间线、强制 cook 并生成/清空 DOP cache，HIP undo 不能恢复；
# server batch dispatcher 会在调用前关闭 mutating undo segment。
# force-reset 可选路径由 server 的真实签名探针 + 逐版本 live gate 决定，
# bridge 不提供 override/bypass 参数，mock capability 不能放行。
# -------------------------------------------------------------------
@mcp.tool()
def get_simulation_info(ctx, dop_path):
    """读取 DOP simulation 元数据（add-dops-tools，READ_ONLY）。

    返回 frame/time/timestep/object_count；只使用有界 DopSimulation
    查询。响应经过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_simulation_info", {"dop_path": dop_path})


@mcp.tool()
def list_dop_objects(ctx, dop_path, offset=0, limit=100):
    """分页列出 DOP objects（add-dops-tools，READ_ONLY）。

    每项仅返回 name/object_id 与有界 data/record type 摘要；不展开
    模拟数据。响应经过 ``apply_response_cap``。
    """
    return _houdini_call("list_dop_objects", {
        "dop_path": dop_path, "offset": offset, "limit": limit})


@mcp.tool()
def get_dop_object(ctx, dop_path, object_name, max_data=64):
    """通过 findObject 查询单个 DOP object（add-dops-tools，READ_ONLY）。

    ``max_data`` 限制 data 摘要数量；不返回无界 record 内容。
    """
    return _houdini_call("get_dop_object", {
        "dop_path": dop_path, "object_name": object_name,
        "max_data": max_data})


@mcp.tool()
def get_dop_field(ctx, dop_path, object_name, data_name, field_name,
                  record_type="Options", record_index=0):
    """读取 DOP data/record 字段（add-dops-tools，READ_ONLY）。

    volume/VDB 字段不返回原始体素，只返回可廉价取得的 resolution /
    bbox/min/max/average；不可得值标记 ``unavailable``。
    """
    return _houdini_call("get_dop_field", {
        "dop_path": dop_path, "object_name": object_name,
        "data_name": data_name, "field_name": field_name,
        "record_type": record_type, "record_index": record_index})


@mcp.tool()
def get_dop_relationships(ctx, dop_path, offset=0, limit=100,
                          max_objects=100):
    """分页读取 DOP relationships（add-dops-tools，READ_ONLY）。

    每个关系的对象名受 ``max_objects`` 硬上限约束；响应经过
    ``apply_response_cap``。
    """
    return _houdini_call("get_dop_relationships", {
        "dop_path": dop_path, "offset": offset, "limit": limit,
        "max_objects": max_objects})


@mcp.tool()
def step_simulation(ctx, dop_path, frames=1):
    """推进 DOP 模拟（add-dops-tools，NO_UNDO）。

    通过 ``hou.setTime(frameToTime(current + frames))`` 后
    ``dop_node.cook(force=True)``；拒绝 ``frames <= 0``，不恢复旧帧。
    会触发依赖图 cook 与 DOP cache 生成/替换，**不**进入 undo group。
    """
    return _houdini_call("step_simulation", {
        "dop_path": dop_path, "frames": frames})


@mcp.tool()
def reset_simulation(ctx, dop_path, reset_frame=None):
    """时间线优先重置 DOP 模拟（add-dops-tools，NO_UNDO）。

    先移动到 reset frame 并 force cook；可选 force-reset 仅在真实签名
    探针和对应 Houdini 版本 live gate 同时允许时执行。cache 清空/重建
    不可由 HIP undo 恢复，owned simulation 权限失败返回结构化 warning。
    """
    params = {"dop_path": dop_path}
    if reset_frame is not None:
        params["reset_frame"] = reset_frame
    return _houdini_call("reset_simulation", params)


@mcp.tool()
def get_sim_memory_usage(ctx, dop_path):
    """读取 DOP simulation 内存（add-dops-tools，READ_ONLY）。

    使用 ``DopSimulation.memoryUsage()``，返回值明确标记 bytes；响应
    经过 ``apply_response_cap``。
    """
    return _houdini_call("get_sim_memory_usage", {"dop_path": dop_path})


# -------------------------------------------------------------------
# add-pdg-tops-tools: 5 个 PDG/TOPs 工具。
# 控制面一律走 hou.TopNode（cookWorkItems/getCookState/workItemStates/
# dirtyWorkItems/cancelCook）；cook/dirty/cancel 改变 scheduler 与运行
# 态结果，HIP undo 不能恢复，server dispatcher 在调用前关闭 mutating
# undo segment。cook handle registry 为进程内、有界、重启失效。
# pdg_status / pdg_workitems 只读；pdg_cook / pdg_dirty / pdg_cancel 不
# 进 undo group。响应经过 server 端 ``apply_response_cap``。
# -------------------------------------------------------------------
@mcp.tool()
def pdg_cook(ctx, node_path, blocking=False, timeout_seconds=300):
    """启动 PDG/TOPs cook 并返回进程内 handle（add-pdg-tops-tools，NO_UNDO）。

    通过 ``hou.TopNode.cookWorkItems(block=False)`` 启动 cook（仅当实机探针
    证明该方法不可用时才 fallback 到 deprecated ``executeGraph``，并在响应
    中披露）。同一节点已有 active cook 时返回同一 ``cook_id`` 与
    ``already_running``，不启动第二个 cook；terminal 后的新调用生成新 ID。
    ``blocking=True`` 轮询 ``getCookState(force=True)`` 至终态或
    ``timeout_seconds``；超时返回 ``timed_out`` 且 handle 保持可用，不自动
    cancel。handle 进程内有效（``scope: process``），server 重启后失效。
    """
    return _houdini_call("pdg_cook", {
        "node_path": node_path, "blocking": blocking,
        "timeout_seconds": timeout_seconds})


@mcp.tool()
def pdg_status(ctx, node_path, cook_id=None):
    """查询 TOP cook 状态、work item 计数与 handle（add-pdg-tops-tools，READ_ONLY）。

    使用 ``getCookState(force=True)`` 与 ``workItemStates()``，结合进程内
    cook handle registry 返回 cook_state、各状态计数、是否终态与 handle 摘
    要。``cook_id`` 给出时校验其属于该节点；未知/过期/属他节点返回结构化
    错误。响应经过 ``apply_response_cap``。
    """
    params = {"node_path": node_path}
    if cook_id is not None:
        params["cook_id"] = cook_id
    return _houdini_call("pdg_status", params)


@mcp.tool()
def pdg_workitems(ctx, node_path, status_filter=None, max_items=1000):
    """读取已生成 work item 摘要（add-pdg-tops-tools，READ_ONLY）。

    从 ``getPDGNode()`` 的已生成 work items 读取有界摘要（index/name/state）。
    PDG graph 未生成时返回空列表与明确状态。受 ``status_filter`` 与
    ``max_items`` 限制；响应经过 ``apply_response_cap``。
    """
    params = {"node_path": node_path, "max_items": max_items}
    if status_filter is not None:
        params["status_filter"] = status_filter
    return _houdini_call("pdg_workitems", params)


@mcp.tool()
def pdg_dirty(ctx, node_path):
    """dirty work items（add-pdg-tops-tools，NO_UNDO）。

    调用 ``dirtyWorkItems(remove_outputs=False)``，默认**不**删除磁盘输出。
    dirty 改变 scheduler 运行态，不可由 HIP undo 恢复，不进入 undo group。
    """
    return _houdini_call("pdg_dirty", {"node_path": node_path})


@mcp.tool()
def pdg_cancel(ctx, node_path, cook_id=None):
    """cancel cook（add-pdg-tops-tools，NO_UNDO）。

    调用 ``cancelCook()`` 并验证 handle 属于该节点；对已 terminal/cancelled
    的 handle 返回稳定 cancelled 状态（幂等）。cancel 不可由 HIP undo 恢复，
    不进入 undo group。
    """
    params = {"node_path": node_path}
    if cook_id is not None:
        params["cook_id"] = cook_id
    return _houdini_call("pdg_cancel", params)


# -------------------------------------------------------------------
# add-usd-solaris-tools: 15 个 USD/Solaris bridge tool（薄封装到 server 端
# 同名命令）。三分类见 server.py：
# - MUTATING：lop_import / set_usd_attribute / create_lop_node
#   （创建 / 连接 / 配置 LOP authoring 节点，单 undo group；
#   pxr mutation 不直接调用，adapter 不可用时返回 unsupported）
# - NO_UNDO：12 个 composed stage 查询（lop_stage_info / lop_prim_get /
#   lop_prim_search / lop_layer_info / list_usd_prims / get_usd_attribute /
#   get_usd_prim_stats / get_last_modified_prims / get_usd_composition /
#   get_usd_variants / inspect_usd_layer / list_lights；获取
#   ``LopNode.stage()`` 可能触发 LOP cook，不可由 HIP undo 恢复）
# 本 change READ_ONLY 为空。composed stage 仅经 ``LopNode.stage()`` 只读读取。
# -------------------------------------------------------------------
@mcp.tool()
def lop_stage_info(ctx, node_path, max_prims=500):
    """composed stage 级元数据（add-usd-solaris-tools，NO_UNDO）。

    从 ``LopNode.stage()`` 读取 upAxis / metersPerUnit /
    framesPerSecond / defaultPrim 与有界 prim 计数。响应携带 capability
    探针（Houdini 版本 / USD 版本 / feature flags）并过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("lop_stage_info", {
        "node_path": node_path, "max_prims": max_prims})


@mcp.tool()
def lop_prim_get(ctx, node_path, prim_path, max_attributes=100):
    """单个 prim 的 type / active / loaded / kind + 有界属性
    （add-usd-solaris-tools，NO_UNDO）。

    ``prim_path`` 必填（USD path，如 ``/Asset``）。响应过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("lop_prim_get", {
        "node_path": node_path, "prim_path": prim_path,
        "max_attributes": max_attributes})


@mcp.tool()
def lop_prim_search(ctx, node_path, name=None, type_name=None,
                    max_prims=500, max_depth=5):
    """按 name 子串 / type_name 精确匹配搜索 prim
    （add-usd-solaris-tools，NO_UNDO）。

    两者都省略时等价于 ``list_usd_prims``（受 cap 限制）。响应过 server
    端 ``apply_response_cap``。
    """
    params = {"node_path": node_path, "max_prims": max_prims,
              "max_depth": max_depth}
    if name is not None:
        params["name"] = name
    if type_name is not None:
        params["type_name"] = type_name
    return _houdini_call("lop_prim_search", params)


@mcp.tool()
def lop_layer_info(ctx, node_path, max_layers=20):
    """layer stack 摘要（add-usd-solaris-tools，NO_UNDO）。

    读取 root / session / sublayer 的 identifier / real path / sublayer
    数，受 ``max_layers`` 限制。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("lop_layer_info", {
        "node_path": node_path, "max_layers": max_layers})


@mcp.tool()
def list_usd_prims(ctx, node_path, max_depth=5, max_prims=500):
    """受 max_depth / max_prims 限制的 prim 遍历
    （add-usd-solaris-tools，NO_UNDO）。

    返回 prim 路径 / 名称 / 类型 / 深度列表。响应过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("list_usd_prims", {
        "node_path": node_path, "max_depth": max_depth,
        "max_prims": max_prims})


@mcp.tool()
def get_usd_attribute(ctx, node_path, prim_path, attribute, time=0):
    """单个属性值 + 类型名（add-usd-solaris-tools，NO_UNDO）。

    从 composed stage 读取 attribute 在 ``time`` 的值。响应过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("get_usd_attribute", {
        "node_path": node_path, "prim_path": prim_path,
        "attribute": attribute, "time": time})


@mcp.tool()
def get_usd_prim_stats(ctx, node_path, prim_path):
    """prim active / loaded / defined / abstract / instance + 属性计数
    （add-usd-solaris-tools，NO_UNDO）。

    响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_usd_prim_stats", {
        "node_path": node_path, "prim_path": prim_path})


@mcp.tool()
def get_last_modified_prims(ctx, node_path):
    """最近修改信息不可证明时返回 unsupported
    （add-usd-solaris-tools，NO_UNDO）。

    USD composed stage 无法提供可证明的 last-modified 排序；不伪造。响应
    过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_last_modified_prims", {"node_path": node_path})


@mcp.tool()
def get_usd_composition(ctx, node_path, prim_path, max_arcs=50):
    """composition arc 摘要（add-usd-solaris-tools，NO_UNDO）。

    使用 ``Usd.PrimCompositionQuery`` 若可用；否则返回 unsupported。响应
    过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_usd_composition", {
        "node_path": node_path, "prim_path": prim_path,
        "max_arcs": max_arcs})


@mcp.tool()
def get_usd_variants(ctx, node_path, prim_path):
    """variant set 名称与当前选择（add-usd-solaris-tools，NO_UNDO）。

    响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_usd_variants", {
        "node_path": node_path, "prim_path": prim_path})


@mcp.tool()
def inspect_usd_layer(ctx, node_path, max_layers=20):
    """layer 自定义元数据 / sublayer 路径（add-usd-solaris-tools，NO_UNDO）。

    响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("inspect_usd_layer", {
        "node_path": node_path, "max_layers": max_layers})


@mcp.tool()
def list_lights(ctx, node_path, max_lights=200):
    """灯光识别：优先 UsdLux.LightAPI，再具体 schema IsA
    （add-usd-solaris-tools，NO_UNDO）。

    不依赖 ``UsdLux.Light`` 基类；缺少 API 时返回 capability warning。响应
    过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("list_lights", {
        "node_path": node_path, "max_lights": max_lights})


@mcp.tool()
def lop_import(ctx, parent_path, file_path, import_type="reference",
               prim_path="/", node_name=None):
    """创建 Reference 或 Sublayer LOP（add-usd-solaris-tools，MUTATING）。

    创建 / 连接 / 配置是单 undo group 的连续步骤；失败 destroy 半成品。
    **不**直接修改 stage layer stack；adapter 不可用时返回 unsupported。
    ``import_type`` 接受 ``reference`` / ``sublayer``。响应过 server 端
    ``apply_response_cap``。
    """
    params = {"parent_path": parent_path, "file_path": file_path,
              "import_type": import_type, "prim_path": prim_path}
    if node_name is not None:
        params["node_name"] = node_name
    return _houdini_call("lop_import", params)


@mcp.tool()
def set_usd_attribute(ctx, parent_path, prim_path, attribute, value,
                      attribute_type="float", node_name=None):
    """创建白名单属性 authoring LOP（add-usd-solaris-tools，MUTATING）。

    按其真实参数 schema author；adapter 或 value 无法无损映射时返回
    ``unsupported``，**禁止** fallback 到 composed stage mutation。
    ``attribute_type`` 接受 ``float / int / string / vector``。响应过
    server 端 ``apply_response_cap``。
    """
    params = {"parent_path": parent_path, "prim_path": prim_path,
              "attribute": attribute, "value": value,
              "attribute_type": attribute_type}
    if node_name is not None:
        params["node_name"] = node_name
    return _houdini_call("set_usd_attribute", params)


@mcp.tool()
def create_lop_node(ctx, parent_path, node_type, node_name=None):
    """在可编辑 LOP parent 下创建节点（add-usd-solaris-tools，MUTATING）。

    ``node_type`` 必须在 ``hou.lopNodeTypeCategory().nodeTypes()`` 探针中
    存在；单 undo group；失败 destroy 半成品。响应过 server 端
    ``apply_response_cap``。
    """
    params = {"parent_path": parent_path, "node_type": node_type}
    if node_name is not None:
        params["node_name"] = node_name
    return _houdini_call("create_lop_node", params)


# -------------------------------------------------------------------
# add-cops-tools: 7 个 Copernicus (COP) 工具（H21+）。
# 仅面向 H21+ ``hou.CopNode``；旧 COP2 返回 unsupported_legacy_cop2，不调
# copInfo/imagePlaneInfo/passThrough 等旧/虚构 API。output 读取
# （geometry/cable/layer/vdb）可能触发 COP cook，server dispatcher 在调用
# 前关闭 mutating undo segment，因此归 NO_UNDO_COMMANDS；list_cop_node_types
# 只枚举 registry，归 READ_ONLY；create/set_cop_flags 归 MUTATING，单 undo
# group。响应经过 server 端 ``apply_response_cap``。
# 放在 PR 16 / PR 15 / PR 18 / PR 7 section header 之前以避免被
# test_bridge_style (PR 7) / test_help (PR 15) / test_verify_hou_api
# (PR 18) / test_connection (PR 16) 四套 AST 探针误识别。
# -------------------------------------------------------------------
@mcp.tool()
def get_cop_info(ctx, node_path):
    """读取 Copernicus 节点信息（add-cops-tools，NO_UNDO）。

    返回 input/output data types、outputCableStructure 与每个 output 的
    cable metadata（反射探针如实汇报 wire surface）。可能触发 COP cook；
    响应经过 ``apply_response_cap``。
    """
    return _houdini_call("get_cop_info", {"node_path": node_path})


@mcp.tool()
def get_cop_geometry(ctx, node_path, output_index=0, frame=None):
    """读取 Copernicus output geometry 摘要（add-cops-tools，NO_UNDO）。

    调 ``geometry``/``geometryAtFrame``，只返回 point/prim/vertex counts、
    bbox、有界 attrib 摘要；不序列化完整几何。``frame`` 可选，指定时走
    AtFrame 变体。
    """
    params = {"node_path": node_path, "output_index": output_index}
    if frame is not None:
        params["frame"] = frame
    return _houdini_call("get_cop_geometry", params)


@mcp.tool()
def get_cop_layer(ctx, node_path, output_index=0, frame=None):
    """读取 Copernicus ImageLayer metadata（add-cops-tools，NO_UNDO）。

    先调 ``layer``/``layerAtFrame``；不可得时从 ``cable`` 反射 wire 选择
    ImageLayer（响应披露 ``cable_fallback_used``）。只返回 resolution/
    storage/bounds，不返回原始像素。
    """
    params = {"node_path": node_path, "output_index": output_index}
    if frame is not None:
        params["frame"] = frame
    return _houdini_call("get_cop_layer", params)


@mcp.tool()
def get_cop_vdb(ctx, node_path, output_index=0, frame=None):
    """读取 Copernicus NanoVDB/grid metadata（add-cops-tools，NO_UNDO）。

    先调 ``vdb``/``vdbAtFrame``；不可得时从 ``cable`` 反射 wire 选择
    NanoVDB。只返回 grid_name/storage/bounds 等有界 metadata，不返回体素。
    """
    params = {"node_path": node_path, "output_index": output_index}
    if frame is not None:
        params["frame"] = frame
    return _houdini_call("get_cop_vdb", params)


@mcp.tool()
def create_cop_node(ctx, parent_path, node_type, node_name=None):
    """在 Copernicus parent 下创建节点（add-cops-tools，MUTATING）。

    校验 parent 可编辑且 child category 为 "Cop"，node_type 必须在 Cop
    registry 中存在；单 undo group。``node_name`` 可选。
    """
    params = {"parent_path": parent_path, "node_type": node_type}
    if node_name is not None:
        params["node_name"] = node_name
    return _houdini_call("create_cop_node", params)


@mcp.tool()
def set_cop_flags(ctx, node_path, flags):
    """原子设置 Copernicus 白名单 flags（add-cops-tools，MUTATING）。

    ``flags`` 键只能是 display/export/template/selectable_template/
    compress/bypass，值为 bool；未知键在任何写入前拒绝整次请求。单 undo group。
    """
    return _houdini_call("set_cop_flags", {
        "node_path": node_path, "flags": flags})


@mcp.tool()
def list_cop_node_types(ctx, category="Cop"):
    """枚举 Copernicus node type registry（add-cops-tools，READ_ONLY）。

    默认枚举 "Cop" category（H21+ Copernicus）；``"Cop2"`` 显式拒绝为
    legacy。只查询 registry，不触发 COP cook 或写入。
    """
    return _houdini_call("list_cop_node_types", {"category": category})


@mcp.tool()
def list_chop_channels(ctx, node_path, output_index=0):
    """枚举 CHOP channel（track）名与 sample 范围（add-chops-tools，NO_UNDO）。

    数据入口 ``ChopNode.clip()`` → ``Clip.tracks()``；返回每个 track 的
    name、sample range/rate/count。读取可能触发 CHOP cook；响应经过
    ``apply_response_cap``。
    """
    return _houdini_call("list_chop_channels", {
        "node_path": node_path, "output_index": output_index})


@mcp.tool()
def get_chop_data(ctx, node_path, channels=None, output_index=0,
                  sample=None, frame=None, time=None, start=None, end=None):
    """有界读取 CHOP track 的 sample 数据（add-chops-tools，NO_UNDO）。

    查询模式（优先级）：``sample``/``frame``/``time`` 单点（对应
    ``evalAtSample``/``evalAtFrame``/``evalAtTime``）；``start``/``end``
    sample index 闭区间（``evalAtSampleRange``，夹取到 clip range）；都不
    给则完整 track（``numSamples<=上限`` 时 ``allSamples``）。多 track 受
    max_channels / max_samples_per_channel / 响应 cap 三层限制。
    """
    params = {"node_path": node_path, "output_index": output_index}
    if channels is not None:
        params["channels"] = channels
    if sample is not None:
        params["sample"] = sample
    if frame is not None:
        params["frame"] = frame
    if time is not None:
        params["time"] = time
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return _houdini_call("get_chop_data", params)


@mcp.tool()
def create_chop_node(ctx, parent_path, node_type, node_name=None):
    """在 CHOP parent 下创建节点（add-chops-tools，MUTATING）。

    校验 parent 可编辑且 child category 为 "Chop"，node_type 必须在 Chop
    registry 中存在；单 undo group。``node_name`` 可选。
    """
    params = {"parent_path": parent_path, "node_type": node_type}
    if node_name is not None:
        params["node_name"] = node_name
    return _houdini_call("create_chop_node", params)


@mcp.tool()
def export_chop_to_parm(ctx, chop_path, channel, target_path, target_parm,
                        output_index=0, replace_existing=False):
    """在目标参数建立 HScript chop() channel reference（add-chops-tools，MUTATING）。

    预校验 source track + scalar numeric target parm；以
    ``chop("<channel_path>")`` 表达式建立实时引用。不设 CHOP export flag、
    不创 Export CHOP、不烘焙 keyframe。目标已有表达式/关键帧时默认拒绝，
    ``replace_existing=True`` 时替换并披露。单 undo group。
    """
    params = {
        "chop_path": chop_path, "channel": channel,
        "target_path": target_path, "target_parm": target_parm,
        "output_index": output_index,
        "replace_existing": replace_existing,
    }
    return _houdini_call("export_chop_to_parm", params)


# -------------------------------------------------------------------
# add-takes-and-cache-tools: 8 个 bridge tool。
# 全部透传 server 端同名命令；不引入隐藏 override / 授权参数。
# 三分类见 server.py：
# - READ_ONLY：list_takes / get_current_take / list_caches /
#   get_cache_status
# - MUTATING：set_current_take / create_take（单 undo group）
# - NO_UNDO：clear_cache / write_cache（运行态 / 磁盘副作用，不
#   进 undo group）
# 风格：与 add-chops-tools / add-cops-tools 保持一致（无类型注解
# + 中文 docstring）。
# -------------------------------------------------------------------
@mcp.tool()
def list_takes(ctx):
    """枚举全部 takes（add-takes-and-cache-tools，READ_ONLY）。

    走 ``hou.takes.takes()`` 返回 name / path / parent / current 列表。
    只读取，不修改场景。响应经过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("list_takes", {})


@mcp.tool()
def get_current_take(ctx):
    """读取当前 take（add-takes-and-cache-tools，READ_ONLY）。

    走 ``hou.takes.currentTake()`` 返回 name / path / parent。
    响应经过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_current_take", {})


@mcp.tool()
def set_current_take(ctx, name_or_path):
    """切换当前 take（add-takes-and-cache-tools，MUTATING）。

    走 ``hou.takes.findTake`` 解析真实 ``hou.Take`` 对象后传给
    ``hou.takes.setCurrentTake``，**不**传字符串。找不到 / 歧义时拒
    绝。属于 take 编辑，单 undo group。响应过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("set_current_take", {
        "name_or_path": name_or_path,
    })


@mcp.tool()
def create_take(ctx, name, include_parms=None, parent_take=None):
    """创建 child take（add-takes-and-cache-tools，MUTATING）。

    走 ``parent.addChildTake(name)``；先解析 parent 与每个 parm
    路径为真实 ``hou.ParmTuple``，预校验全部成功后才写入。包含
    parm 时临时切到新 take 调 ``addParmTuple``、finally 恢复原
    current。预校验失败零部分残留。响应过 server 端
    ``apply_response_cap``。
    """
    params = {"name": name}
    if include_parms is not None:
        params["include_parms"] = list(include_parms)
    if parent_take is not None:
        params["parent_take"] = parent_take
    return _houdini_call("create_take", params)


@mcp.tool()
def list_caches(ctx, parent_path="/", max_nodes=256):
    """枚举白名单 cache 节点（add-takes-and-cache-tools，READ_ONLY）。

    走 ``_cache_nodes.list_caches`` 按 BFS 走 children，节点数受
    ``max_nodes`` 限制；只匹配在 H21.0 / H22 实测通过的 File Cache
    adapter 白名单，普通 ``Sop/file`` 等绝不出现。响应过 server 端
    ``apply_response_cap``。
    """
    return _houdini_call("list_caches", {
        "parent_path": parent_path, "max_nodes": max_nodes,
    })


@mcp.tool()
def get_cache_status(ctx, node_path):
    """读取 cache 节点 status 摘要（add-takes-and-cache-tools，READ_ONLY）。

    走 ``_cache_nodes.get_cache_status``；未知 type 返回
    ``unsupported_cache_type``。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("get_cache_status", {"node_path": node_path})


@mcp.tool()
def clear_cache(ctx, node_path, remove_disk_file=False):
    """清运行态 cache（add-takes-and-cache-tools，NO_UNDO）。

    走 ``_cache_nodes.clear_cache``；改 ``loadfromdisk`` 并 cook；
    ``remove_disk_file=True`` 才同步删磁盘文件。运行态 / 磁盘副作
    用不可由 HIP undo 恢复，server dispatcher 在调用前关闭 mutating
    undo segment。响应过 server 端 ``apply_response_cap``。
    """
    return _houdini_call("clear_cache", {
        "node_path": node_path,
        "remove_disk_file": remove_disk_file,
    })


@mcp.tool()
def write_cache(ctx, node_path):
    """真实落盘 cache（add-takes-and-cache-tools，NO_UNDO）。

    走 ``_cache_nodes.write_cache``；adapter.write 调
    ``node.geometry().saveToFile(file)`` 写磁盘（H21 实测一致路
    径）。返回 adapter、目标路径、文件操作、cook errors 与最终
    状态。HIP undo 不能恢复磁盘结果，不进 undo group。响应过
    server 端 ``apply_response_cap``。
    """
    return _houdini_call("write_cache", {"node_path": node_path})


# -------------------------------------------------------------------
# PR 16 Connection Diagnostic Tools (placed before PR 15 / PR 7 sections
# so existing test_bridge_style (PR 7) and test_help PR 15 probes — which
# scan @mcp.tool() strictly after their own header lines — do not pick it
# up; PR 16 ships its own AST probe in tests.test_connection)
# -------------------------------------------------------------------
@mcp.tool()
def check_connection(ctx):
    """检查 Houdini 端连接信息（PR 16 连接诊断）。

    返回 dict 包含 hou_version / hou_build / hip_file / hip_file_basename /
    is_untitled / node_count / desktop_count / _status 八个字段。返回结构
    与 server.py 中 HoudiniMCPServer.check_connection 保持一致。仅做只读
    查询，不会修改 .hip 文件、节点或网络；适合 AI agent 在长会话开头调用
    一次以获取当前 Houdini 版本与场景规模。
    """
    return _houdini_call("check_connection", {})


@mcp.tool()
def ping_houdini(ctx, timeout=5):
    """轻量级 Houdini 端 ping，验证响应时间（PR 16 连接诊断）。

    参数说明：
    - timeout: 最长等待秒数（默认 5），超过则 within_timeout=False

    返回 dict 包含 pong / elapsed_ms / within_timeout / hou_version 四项；
    hou 抛异常时返 pong=False 并带 error 字段。该 ping 不持久化新连接，
    只在既有 hou 上下文里调用一次 hou.version()；适合作为健康检查或
    网络抖动场景下的快速探测。注意：与 bridge 协议的 "ping" 命令不同，
    后者只验证 socket / 帧协议，本工具测量 Houdini 端的实际响应时间。
    """
    return _houdini_call("ping_houdini", {"timeout": timeout})


# -------------------------------------------------------------------
# PR 15 Help Tools (placed before PR 7 section so test_bridge_style PR 7
# probe — which scans all @mcp.tool() strictly after the "# PR 7 Materials
# Tools" header line — does not pick it up; the trailing "Tools" also makes
# the PR 14 probe's "next section header" regex stop here)
# -------------------------------------------------------------------
@mcp.tool()
def get_houdini_help(ctx, help_type, item_name, timeout=10):
    """从 SideFX 在线文档查询 Houdini 节点、VEX 函数或 hou 方法的帮助（PR 15）。

    help_type 支持 11 种："sop" / "obj" / "dop" / "cop2" / "chop" /
    "vop" / "lop" / "top" / "rop" / "vex_function" / "python_hou"。
    item_name 是节点名 / VEX 函数名 / hou 方法名。timeout 是 HTTP 请求
    超时秒数（默认 10）。返回 dict 包含 title / summary / parameters /
    inputs / outputs / methods / status 等字段，HTML 解析使用 stdlib
    html.parser（零新增 pip 依赖）。HTTP 4xx / 5xx / 网络错误 / 超时
    全部降级为 status=error，不抛异常。响应整体过 apply_response_cap。
    """
    return _houdini_call("get_houdini_help", {
        "help_type": help_type,
        "item_name": item_name,
        "timeout": timeout,
    })


# -------------------------------------------------------------------
# PR 18 Help Wrapper Tools (placed before PR 7 section so test_bridge_style
# PR 7 probe — which scans all @mcp.tool() strictly after the "# PR 7
# Materials Tools" header line — does not pick it up; the trailing "Tools"
# also makes the PR 14 probe's "next section header" regex stop here)
# -------------------------------------------------------------------
@mcp.tool()
def verify_hou_api(ctx, item_name, help_type="python_hou", timeout=10):
    """AI-friendly wrapper over get_houdini_help（PR 18）。

    参数说明：
    - item_name: 要查询的 hou API / 节点 / VEX 函数名，如
      "ObjNode.setDisplayNode" 或 "Node.setInput"。
    - help_type: 可选，帮助类型，默认 "python_hou"；其他支持值见
      get_houdini_help（sop / obj / dop / cop2 / chop / vop / lop /
      top / rop / vex_function）。
    - timeout: 可选，HTTP 请求超时秒数，默认 10。

    返回 dict 包含 title / summary / parameters / inputs / outputs /
    methods / status 等字段，并在响应末尾附 `_ai_hint` 字段，给 AI
    一个可直接使用的简短提示（命中方法签名 / F-C pattern /
    SideFX 不可达 fallback）。响应整体过 apply_response_cap。
    """
    return _houdini_call("verify_hou_api", {
        "item_name": item_name,
        "help_type": help_type,
        "timeout": timeout,
    })


# -------------------------------------------------------------------
# PR 7 Materials Tools (thin relay to server-side _materials)
# -------------------------------------------------------------------
@mcp.tool()
def create_material(ctx, material_type,
                    name=None, parent_path="/mat",
                    parameters=None):
    """在 Houdini 中创建一个材质节点并返回节点信息。

    参数说明：
    - material_type: 材质节点类型，如 "principledshader"、"vopsurface"
    - name: 可选，节点名；缺省时由 Houdini 自动命名
    - parent_path: 可选，父节点路径，默认 "/mat"；不存在时回退到 /mat
    - parameters: 可选，dict 按 parm 名设置参数值；不存在的 parm 名会
      静默跳过（不影响调用）

    返回 dict 包含 path / type / name / parameters_set 四项，
    parameters_set 列出已尝试设置的 parm 名（含静默跳过的）。
    """
    return _houdini_call("create_material", {
        "material_type": material_type,
        "name": name,
        "parent_path": parent_path,
        "parameters": parameters or {},
    })


@mcp.tool()
def assign_material(ctx, geometry_path,
                    material_path, group=None):
    """把 material_path 处的材质绑定到 geometry_path 处的几何节点。

    参数说明：
    - geometry_path: SOP / OBJ 几何节点路径
    - material_path: 材质节点路径
    - group: 可选，指定要绑定到的 group 名（如 primitive / point group）；
      传 None 时整节点绑定，传具体名字时仅绑定到该 group

    返回 dict 包含 geometry_path / material_path / group / success；
    绑定失败时函数会抛 ValueError，bridge 不会再以 success:True 形式
    静默吞错。
    """
    return _houdini_call("assign_material", {
        "geometry_path": geometry_path,
        "material_path": material_path,
        "group": group,
    })


@mcp.tool()
def get_material_info(ctx, material_path):
    """获取材质节点的详细信息。

    返回 dict 包含 path / type / name / parameters / texture_references
    五项。parameters 仅保留 _materials.MATERIAL_PARM_WHITELIST 中列出的
    50+ parm，过滤后键集合稳定跨材质类型一致；texture_references 列出
    eval 值匹配已知贴图后缀（.png / .jpg / .jpeg / .exr / .hdr / .tif /
    .tiff / .rat / .tex）的 parm。
    """
    return _houdini_call("get_material_info", {"material_path": material_path})


def main():
    """Run the MCP server on stdio."""
    global _houdini_port

    parser = argparse.ArgumentParser(description='Houdini MCP Server Bridge')
    parser.add_argument('--port', type=int, default=9876,
                        help='Port to connect to Houdini (default: 9876)')
    args = parser.parse_args()
    _houdini_port = args.port
    logger.info(f"Configured to connect to Houdini on port {_houdini_port}")

    # OPUS RapidAPI 配置检查（refactor-opus-optional-and-debt-cleanup）：
    # 配置读取延迟到 ``_opus``，bridge 不再在顶层持有 RAPIDAPI_* 全局。
    # 仅在启动时 best-effort 日志，不阻止 server 启动（无 key 也应能跑）。
    if _opus is not None and _opus.is_configured():
        logger.info("OPUS RapidAPI configured; schema/create/variate/check tools enabled.")
    else:
        logger.warning("RAPIDAPI_HOST_URL, RAPIDAPI_HOST, or RAPIDAPI_KEY not configured. OPUS API features will be disabled.")
        logger.warning("To enable OPUS features, configure your RapidAPI key in urls.env")
    mcp.run()

if __name__ == "__main__":
    main()
