#!/usr/bin/env python
"""Houdini headless MCP host。

该进程只在 bridge 发现目标端口无人监听时由 hython 启动。它把
``houdinimcp`` 的 package root 设为 ``external/``，在 QCoreApplication
事件循环中复用现有 ``HoudiniMCPServer``，并由自己根据 client activity
管理 daemon 生命周期。runtime metadata 和 startup lock 都按 owner token
清理，避免旧 bridge 误删或停止新 daemon。
"""
import argparse
import atexit
import json
import os
import signal
import sys
import time


_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

_HEADLESS_DIR_NAME = ".headless"
_RUNTIME_SUFFIX = ".runtime.json"
_LOCK_SUFFIX = ".lock"
_DEFAULT_IDLE_SECONDS = 300.0
_MIN_IDLE_SECONDS = 30.0
_MAX_IDLE_SECONDS = 86400.0

_HOST = None
_PORT = None
_OWNER_TOKEN = None
_IDLE_SECONDS = _DEFAULT_IDLE_SECONDS
_STARTED_MONOTONIC = time.monotonic()
_SERVER_API = None
_SERVER_STARTED_HERE = False
_APP = None
_IDLE_TIMER = None
_CLEANED = False


def _env_dir():
    """返回与 embedded environment 并列的 houdinimcp-env 根目录。"""
    return os.path.join(_PACKAGE_ROOT, "houdinimcp-env")


def _headless_dir():
    """返回 headless metadata 根目录并确保目录存在。"""
    directory = os.path.join(_env_dir(), _HEADLESS_DIR_NAME)
    os.makedirs(directory, exist_ok=True)
    return directory


def _safe_host(host):
    """把 host 转成不会穿越目录的 metadata key。"""
    value = str(host or "127.0.0.1")
    return "".join(ch if ch.isalnum() or ch in "._-" else "_"
                   for ch in value)


def _headless_key(host, port):
    return "{0}-{1}".format(_safe_host(host), int(port))


def _metadata_path(host, port, suffix):
    return os.path.join(_headless_dir(), _headless_key(host, port) + suffix)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _atomic_write_json(path, value):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    temporary = path + ".{0}.{1}.tmp".format(os.getpid(), os.getpid())
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def _metadata_matches(value, host, port, token, pid=None):
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


def _remove_owned_file(path, host, port, token, pid=None):
    value = _read_json(path)
    if not _metadata_matches(value, host, port, token, pid=pid):
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _release_startup_lock(host, port, token):
    """只释放匹配 token/host/port 的 startup lock。"""
    return _remove_owned_file(
        _metadata_path(host, port, _LOCK_SUFFIX), host, port, token)


def _write_runtime_metadata(host, port, token):
    metadata = {
        "pid": os.getpid(),
        "owner_token": str(token),
        "host": str(host),
        "port": int(port),
        "created_at": time.time(),
    }
    _atomic_write_json(
        _metadata_path(host, port, _RUNTIME_SUFFIX), metadata)
    return metadata


def _runtime_metadata(host, port):
    return _read_json(_metadata_path(host, port, _RUNTIME_SUFFIX))


def _runtime_owned(host, port, token):
    return _metadata_matches(
        _runtime_metadata(host, port), host, port, token, pid=os.getpid())


def _load_qt_core():
    try:
        from PySide6 import QtCore
        return QtCore, "PySide6"
    except ImportError:
        try:
            from PySide2 import QtCore
            return QtCore, "PySide2"
        except ImportError as error:
            raise RuntimeError(
                "headless Houdini 需要 PySide6 或 PySide2") from error


def _load_server_api():
    if _SERVER_API is not None:
        return _SERVER_API
    from houdinimcp import is_server_running, start_server, stop_server
    return {
        "is_server_running": is_server_running,
        "start_server": start_server,
        "stop_server": stop_server,
    }


def _get_houdini_server():
    try:
        import hou
        session = getattr(hou, "session", None)
        return getattr(session, "houdinimcp_server", None)
    except Exception:
        return None


def _call_observer(server, names, fallback=None):
    for name in names:
        value = getattr(server, name, None)
        if callable(value):
            try:
                return value()
            except Exception:
                continue
        if value is not None:
            return value
    return fallback


def _server_has_client(server):
    if server is None:
        return False
    value = _call_observer(
        server, ("client_presence", "has_client", "is_client_connected"),
        fallback=None)
    if value is None:
        return getattr(server, "client", None) is not None
    return bool(value)


def _server_last_activity(server):
    if server is None:
        return _STARTED_MONOTONIC
    value = _call_observer(
        server, ("last_activity", "get_last_activity"), fallback=None)
    if value is None:
        value = getattr(server, "_last_activity", None)
    try:
        return float(value)
    except (TypeError, ValueError):
        return _STARTED_MONOTONIC


def _stop_owned_server():
    """停止当前 token 创建的 server，不触碰其他 runtime owner。"""
    if not _HOST or _PORT is None or not _OWNER_TOKEN:
        return
    runtime = _runtime_metadata(_HOST, _PORT)
    if runtime is not None and not _metadata_matches(
            runtime, _HOST, _PORT, _OWNER_TOKEN, pid=os.getpid()):
        return
    if runtime is None and not _SERVER_STARTED_HERE:
        return
    api = _load_server_api()
    try:
        api["stop_server"]()
    except Exception:
        pass


def _cleanup():
    global _CLEANED
    if _CLEANED:
        return
    _CLEANED = True
    try:
        _stop_owned_server()
    finally:
        if _HOST and _PORT is not None and _OWNER_TOKEN:
            _remove_owned_file(
                _metadata_path(_HOST, _PORT, _RUNTIME_SUFFIX),
                _HOST, _PORT, _OWNER_TOKEN, pid=os.getpid())
            _release_startup_lock(_HOST, _PORT, _OWNER_TOKEN)


def _handle_signal(signum, frame):
    _cleanup()
    if _APP is not None:
        try:
            _APP.quit()
        except Exception:
            pass


def _install_signal_handlers():
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _handle_signal)
        except (ValueError, OSError):
            pass


def _check_idle():
    if _CLEANED:
        return
    server = _get_houdini_server()
    if server is None or _server_has_client(server):
        return
    if time.monotonic() - _server_last_activity(server) < _IDLE_SECONDS:
        return
    _cleanup()
    if _APP is not None:
        try:
            _APP.quit()
        except Exception:
            pass


def _clamp_idle_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = _DEFAULT_IDLE_SECONDS
    return max(_MIN_IDLE_SECONDS, min(_MAX_IDLE_SECONDS, seconds))


def _default_idle_seconds():
    return _clamp_idle_seconds(
        os.environ.get("HOUDINI_MCP_HEADLESS_IDLE_SECONDS",
                       _DEFAULT_IDLE_SECONDS))


def _build_parser():
    parser = argparse.ArgumentParser(description="Houdini MCP headless host")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--owner-token", required=True)
    parser.add_argument("--idle-seconds", type=float,
                        default=_default_idle_seconds())
    return parser


def run(argv=None, server_api=None, qt_core=None):
    """启动 headless server 并阻塞在 Qt event loop。"""
    global _HOST, _PORT, _OWNER_TOKEN, _IDLE_SECONDS
    global _SERVER_API, _SERVER_STARTED_HERE, _APP, _IDLE_TIMER
    global _CLEANED, _STARTED_MONOTONIC
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not 1 <= int(args.port) <= 65535:
        raise ValueError("port 必须在 1..65535")
    _HOST = str(args.host)
    _PORT = int(args.port)
    _OWNER_TOKEN = str(args.owner_token)
    if not _OWNER_TOKEN:
        raise ValueError("owner-token 不能为空")
    _IDLE_SECONDS = _clamp_idle_seconds(args.idle_seconds)
    _CLEANED = False
    _SERVER_STARTED_HERE = False
    _STARTED_MONOTONIC = time.monotonic()
    if server_api is not None:
        _SERVER_API = server_api
    if qt_core is None:
        qt_core, _qt_backend = _load_qt_core()
    else:
        _qt_backend = "injected"

    atexit.register(_cleanup)
    _install_signal_handlers()
    app = qt_core.QCoreApplication.instance()
    if app is None:
        app = qt_core.QCoreApplication([sys.argv[0]])
    _APP = app

    api = _load_server_api()
    api["start_server"](host=_HOST, port=_PORT)
    if not api["is_server_running"]():
        raise RuntimeError(
            "HoudiniMCP server 未能启动在 {0}:{1}".format(_HOST, _PORT))
    _SERVER_STARTED_HERE = True
    _write_runtime_metadata(_HOST, _PORT, _OWNER_TOKEN)
    _release_startup_lock(_HOST, _PORT, _OWNER_TOKEN)

    timer = qt_core.QTimer()
    timer.timeout.connect(_check_idle)
    timer.start(1000)
    _IDLE_TIMER = timer
    try:
        execute = getattr(app, "exec", None)
        if not callable(execute):
            execute = getattr(app, "exec_", None)
        if not callable(execute):
            raise RuntimeError("Qt application 缺少 exec()/exec_()")
        return execute()
    finally:
        try:
            timer.stop()
        except Exception:
            pass
        _cleanup()


def main():
    try:
        return int(run() or 0)
    except Exception as error:
        print("headless Houdini MCP 启动失败: " + str(error),
              file=sys.stderr)
        _cleanup()
        return 1


if __name__ == "__main__":
    sys.exit(main())
