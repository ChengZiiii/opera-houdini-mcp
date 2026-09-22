"""_rag_lifecycle.py — bridge 侧 RAG 索引生命周期（versioned-rag-index §3）。

旁挂模块：**不 import hou、不依赖 HFS 环境变量**；经 bridge 传入的 TCP
回调（``_houdini_call`` 形态）与 Houdini server 通信。双进程架构（design
Context）：bridge（本进程，无 hou）与 server（Houdini GUI 进程）env 不共
享，版本与 HFS 必须来自 server 权威应答，不从路径猜测。

职责链（RAG 工具调用前的轻量 gate，``pre_tool_gate``）：

  版本路由（ensure_routed，versioned 结果进程内粘滞）
      经 TCP 调 get_scene_info 取 houdini_version + hfs_path
      → 设 os.environ["HOUDINI_MCP_RAG_INDEX_DIR"] = rag/<ver>/
      （env 是 _rag 解析序最高优先级 → _rag 本体零改动路由）
  → 封存旧 flat 索引（archive_legacy_flat：改名保留不迁移，幂等）
  → 索引缺失时自动构建（trigger_build：独占 lock + 二次探测 +
      detached 无窗口子进程 + 状态文件 pid/龄期双判据防并发）
  → 预热（ensure_preheat：daemon 线程 load_index 填 LRU；
      构建完成 watcher 自动 re-preheat）
  → warming / building 轻量 envelope（区别于正常检索语义）

事件循环纪律（design D4）：FastMCP 同步工具直接跑在 bridge asyncio 事件
循环上，gate 内只允许 O(1) 判定 + 文件 stat + （首查一次的）TCP 查询——
与任何中继工具同量级；**绝不同步 load_index**（27.7MB 全量索引加载实测
2.88s，会把整个 bridge 冻结）。

失败语义：任何环节异常都吞掉并回退 flat 解析序（不设 env → _rag 原生
行为），不阻塞、不抛出到工具层。

手工 env 优先：若 bridge 启动时 ``HOUDINI_MCP_RAG_INDEX_DIR`` 已被用户
显式设置，路由整体跳过（env_pinned 模式，尊重手工意图），仅保留预热。
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

try:
    from . import _rag
except ImportError:
    import _rag  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（与 scripts/build_rag_index.py 协议对齐）
# ---------------------------------------------------------------------------
RAG_HOME_DIRNAME = os.path.join(".opera-houdini-mcp", "rag")
INDEX_FILENAME = _rag.INDEX_FILENAME        # "index.v1.json"
STATUS_FILENAME = "build.status.json"
LOCK_FILENAME = "build.lock"

# 存量 flat 索引封存名后缀（design D2：2026-09-09 的 5-zip 局部快照，
# 11% 覆盖——迁移等于把缺陷合法化，故封存保留 + 直接全量重建）
LEGACY_ARCHIVE_SUFFIX = ".legacy-5zip-20260909"

# building 状态最大龄期（秒）：超过即判失效可重试（design D3：Windows
# pid 复用风险下的第二判据；全量构建实测 ~7.5s，300s 裕量充足）
BUILD_MAX_AGE_SECONDS = 300.0
# 子进程退出与终态状态落盘之间的竞争宽限（秒）
BUILD_EXIT_GRACE_SECONDS = 5.0
# 同一版本目录自动构建重试节流（秒）：防 failed 状态下每次工具调用都
# 拉起子进程（失败重试交给下一次会话/下一轮节流窗口）
RETRY_THROTTLE_SECONDS = 60.0
# flat 回退后的路由重试间隔（秒）：server 短暂不在线不该把 flat 结果
# 永久粘滞（spec：版本路由在下一次成功连接后自动生效）
FLAT_RETRY_SECONDS = 30.0

# building envelope 的预计提示（实测 H21 46 zips 全量构建 ~7.5s）
ETA_HINT = "full index build typically finishes in ~10s"

# hou_version 合法形态（hou.applicationVersionString() 形如 "21.0.596"；
# 首字符必须字母数字，拒绝路径分隔/纯点段，防目录逃逸）
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


# ---------------------------------------------------------------------------
# 模块级状态（全部经 _STATE_LOCK 保护；测试用 reset_state_for_tests 清零）
# ---------------------------------------------------------------------------
_STATE_LOCK = threading.Lock()
# 路由缓存：{"route": {...} | None, "last_attempt": ts}
# route 形态：{"mode": "versioned"|"flat"|"env_pinned", "hou_version": str,
#             "verdir": str, "hfs_path": str}
# versioned 粘滞；flat 按 FLAT_RETRY_SECONDS 重试；env_pinned 永久粘滞
_route_state = {"route": None, "last_attempt": 0.0}
# 本进程拉起的构建子进程：verdir -> {"proc": Popen, "started_at": ts}
_build_procs = {}
# 自动构建触发节流：verdir -> 上次触发 ts
_last_trigger = {}
# 预热线程登记：index_path -> Thread（防同路径重复起线程）
_preheat_threads = {}
# 预热结果：index_path -> {"load_state": str, "mtime_ns": int, "size": int}
# load_state 终态 = missing/unavailable/stale 时不再盲目重试；文件变化
# （mtime/size 不同）则重试
_preheat_results = {}


def reset_state_for_tests():
    """清零全部进程内状态（测试隔离用；不动 os.environ 与磁盘）。"""
    global _route_state
    with _STATE_LOCK:
        _route_state = {"route": None, "last_attempt": 0.0}
        _build_procs.clear()
        _last_trigger.clear()
        _preheat_threads.clear()
        _preheat_results.clear()


# ---------------------------------------------------------------------------
# 目录与版本路由（task 3.1）
# ---------------------------------------------------------------------------
def rag_root():
    """rag 根目录：``~/.opera-houdini-mcp/rag``（测试可 patch expanduser）。"""
    return os.path.join(os.path.expanduser("~"), RAG_HOME_DIRNAME)


def _scene_info_via_tcp(tcp_call):
    """经 TCP 调 ``get_scene_info``；成功返回 result dict，任何失败返回
    ``None``（不抛）。tcp_call 为 bridge 的 ``_houdini_call`` 形态：
    ``fn(cmd_type, params) -> {"status": "success", "result": {...}}``。

    版本字段双键回退：get_scene_info 实际字段名是 ``houdini_version``
    （design 笔误写作 hou_version——那是 check_connection 的字段名）。
    """
    if not callable(tcp_call):
        return None
    try:
        resp = tcp_call("get_scene_info", {})
    except Exception:
        return None
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return None
    result = resp.get("result")
    return result if isinstance(result, dict) else None


def ensure_routed(tcp_call=None, now=None):
    """版本路由（进程内缓存；task 3.1，design D1）。

    - ``versioned``：成功取到合法版本串 → 设 env 指向 ``rag/<ver>/``，
      结果粘滞（bridge 会话天然单版本）。
    - ``env_pinned``：启动时 env 已被手工设置 → 整体跳过路由。
    - ``flat``：查询失败/版本异常 → 不设 env（_rag 原生 flat 解析序），
      按 FLAT_RETRY_SECONDS 重试（spec：下次成功连接后自动生效）。
    绝不抛异常。
    """
    now = time.time() if now is None else now
    with _STATE_LOCK:
        route = _route_state["route"]
        last_attempt = _route_state["last_attempt"]

    if route is not None:
        if route["mode"] in ("versioned", "env_pinned"):
            return route
        if (now - last_attempt) < FLAT_RETRY_SECONDS:
            return route

    env_dir = os.environ.get("HOUDINI_MCP_RAG_INDEX_DIR", "")
    if env_dir.strip():
        new_route = {"mode": "env_pinned", "hou_version": "",
                     "verdir": "", "hfs_path": ""}
    else:
        info = _scene_info_via_tcp(tcp_call)
        version = ""
        if info is not None:
            raw = info.get("houdini_version") or info.get("hou_version")
            if isinstance(raw, str):
                version = raw.strip()
        if version and _SAFE_VERSION_RE.match(version):
            verdir = os.path.join(rag_root(), version)
            try:
                os.environ["HOUDINI_MCP_RAG_INDEX_DIR"] = verdir
            except OSError:
                logger.warning("rag lifecycle: set RAG env failed")
                new_route = {"mode": "flat", "hou_version": "",
                             "verdir": "", "hfs_path": ""}
            else:
                hfs = info.get("hfs_path")
                new_route = {
                    "mode": "versioned", "hou_version": version,
                    "verdir": verdir,
                    "hfs_path": hfs if isinstance(hfs, str) else "",
                }
        else:
            new_route = {"mode": "flat", "hou_version": "",
                         "verdir": "", "hfs_path": ""}

    with _STATE_LOCK:
        # 竞争下先到先得（两个工具并发首查）：不覆盖已粘滞的路由
        if _route_state["route"] is None or (
                _route_state["route"]["mode"] == "flat"
                and new_route["mode"] == "versioned"):
            _route_state["route"] = new_route
        _route_state["last_attempt"] = now
        return _route_state["route"]


# ---------------------------------------------------------------------------
# 存量 flat 索引封存（task 3.2，design D2）
# ---------------------------------------------------------------------------
def archive_legacy_flat():
    """封存 rag 根 flat 索引：改名保留、不迁移、幂等、失败仅警告。

    返回 ``"absent"``（无可封存）/ ``"archived"``（本次完成改名）/
    ``"conflict"``（封存名已存在而 flat 又出现，人工介入，不覆盖任何
    文件）/ ``"failed"``（改名失败，警告不阻塞——flat 继续躺在原地作
    意外兜底，版本化构建不受影响）。
    """
    flat = os.path.join(rag_root(), INDEX_FILENAME)
    if not os.path.isfile(flat):
        return "absent"
    archived = flat + LEGACY_ARCHIVE_SUFFIX
    if os.path.exists(archived):
        logger.warning(
            "rag lifecycle: legacy archive name exists but flat index "
            "reappeared (%s); manual intervention needed", archived)
        return "conflict"
    try:
        os.replace(flat, archived)
    except OSError as exc:
        logger.warning(
            "rag lifecycle: archive legacy flat index failed (non-fatal): "
            "%s", exc)
        return "failed"
    logger.info("rag lifecycle: archived legacy flat index -> %s", archived)
    return "archived"


# ---------------------------------------------------------------------------
# 构建状态判定与自动构建触发（task 3.3，design D3）
# ---------------------------------------------------------------------------
def read_build_status(verdir):
    """读 ``<verdir>/build.status.json``；缺失/损坏/非 dict 返回 None。"""
    try:
        with open(os.path.join(verdir, STATUS_FILENAME), "r",
                  encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_iso_epoch(value):
    """ISO8601（build 脚本写入的 UTC isoformat）→ epoch 秒；失败 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        import datetime
        text = value.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _pid_alive(pid):
    """pid 探活。True/False=确证，None=无法判定（探活机制不可用）。

    Windows：ctypes OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) +
    GetExitCodeProcess==STILL_ACTIVE（无子进程开销，gate 热路径友好；
    build 脚本退出码 0-4，不会与 STILL_ACTIVE(259) 混淆）。
    POSIX：os.kill(pid, 0)。
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        query_limited = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(query_limited, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code)):
                return None
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def build_in_progress(verdir, now=None):
    """判定版本目录是否有**有效**进行中构建（design D3 双判据）。

    有效 building = 状态文件 state==building 且不超龄期，且（本进程
    子进程活着 / 外部 pid 探活活着 / 处于启动-落盘竞争宽限内）。
    超龄（>BUILD_MAX_AGE_SECONDS）或 pid 已死超宽限 → False（可重试）。
    """
    now = time.time() if now is None else now
    status = read_build_status(verdir)
    if not status or status.get("state") != "building":
        return False
    started = _parse_iso_epoch(status.get("started_at"))
    if started is not None and (now - started) > BUILD_MAX_AGE_SECONDS:
        return False  # 超龄：判失效（spec scenario：伪造超龄被重置）

    with _STATE_LOCK:
        track = _build_procs.get(verdir)
    if track is not None:
        proc = track.get("proc")
        if proc is not None and proc.poll() is None:
            return True
        # 本进程子进程已退但终态未落盘：宽限期内仍算 building
        if started is not None and (now - started) > BUILD_EXIT_GRACE_SECONDS:
            return False
        return True

    alive = _pid_alive(status.get("pid"))
    if alive is True:
        return True
    if alive is False:
        # 外部 pid 已死：宽限期后判失效（真失败，可重试）
        if started is not None and (
                now - started) > BUILD_EXIT_GRACE_SECONDS:
            return False
        return True
    # 探活不可用：退化为纯龄期判据（BUILD_MAX_AGE 上限已过）
    return started is not None


def _remove_file(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _spawn_build_process(command):
    """detached 拉起构建子进程：无窗口（CREATE_NO_WINDOW）、不阻塞、
    不监听任何端口；stdout/stderr 静默（可观测性走状态文件）。"""
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, **kwargs)


def trigger_build(verdir, hfs_path="", spawn_fn=None, now=None):
    """触发自动构建（design D3）。返回结果码：

    - ``"spawned"``：本次拉起了子进程（含 watcher 线程）
    - ``"in_progress"``：已有有效进行中构建（状态/lock 判定）
    - ``"lock_stale"``：lock 被占但无有效 building（他者刚退出/残留），
      本轮不抢锁，下轮再评估
    - ``"throttled"``：节流窗口内（RETRY_THROTTLE_SECONDS）
    - ``"failed"``：无法拉起（无 help 源 / 无解释器 / spawn 异常）；
      lock 已清理，不影响后续重试
    """
    now = time.time() if now is None else now
    if build_in_progress(verdir, now=now):
        return "in_progress"
    with _STATE_LOCK:
        last = _last_trigger.get(verdir)
        if last is not None and (now - last) < RETRY_THROTTLE_SECONDS:
            return "throttled"
        _last_trigger[verdir] = now

    # help 源：hfs_path 是 HFS 根（server 权威应答），拼 houdini/help
    if not isinstance(hfs_path, str) or not hfs_path.strip():
        logger.warning(
            "rag lifecycle: cannot auto-build without server hfs_path")
        return "failed"
    help_dir = os.path.join(hfs_path, "houdini", "help")
    if not os.path.isdir(help_dir):
        logger.warning("rag lifecycle: help source not found: %s", help_dir)
        return "failed"

    try:
        os.makedirs(verdir, exist_ok=True)
    except OSError:
        return "failed"

    # 独占 lock + 二次探测（headless 先例；构建子进程退出契约保证清锁）
    lock_path = os.path.join(verdir, LOCK_FILENAME)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return "in_progress" if build_in_progress(verdir, now=now) \
            else "lock_stale"
    except OSError:
        return "failed"
    if build_in_progress(verdir, now=now):
        _remove_file(lock_path)
        return "in_progress"

    # 解释器（design D3）：优先 bridge 自身 sys.executable（内嵌 3.12，
    # build 脚本纯 stdlib）；回退 $HFS/bin/hython.exe。**明确禁用**
    # Houdini GUI 进程的 sys.executable（那是 houdini.exe——拉起完整
    # GUI 实例占 license 开窗口）。bridge 进程内 sys.executable 就是
    # 内嵌 Python，天然满足。
    build_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "scripts", "build_rag_index.py")
    exe = sys.executable or ""
    if not exe or not os.path.isfile(exe):
        alt = os.path.join(hfs_path, "bin", "hython.exe")
        exe = alt if os.path.isfile(alt) else ""
    if not exe or not os.path.isfile(build_script):
        logger.warning(
            "rag lifecycle: no usable interpreter (%r) or build script "
            "(%s)", exe, build_script)
        _remove_file(lock_path)
        return "failed"

    command = [exe, build_script, "--source", help_dir,
               "--output", verdir]
    spawner = spawn_fn if callable(spawn_fn) else _spawn_build_process
    try:
        proc = spawner(command)
    except Exception as exc:
        logger.warning("rag lifecycle: spawn build failed: %s", exc)
        _remove_file(lock_path)
        return "failed"

    with _STATE_LOCK:
        _build_procs[verdir] = {"proc": proc, "started_at": now}
    _start_build_watcher(verdir)
    logger.info("rag lifecycle: build spawned for %s (pid %s)", verdir,
                getattr(proc, "pid", "?"))
    return "spawned"


def _start_build_watcher(verdir):
    """构建完成轮询线程（design D3/D4）：1s 间隔查状态文件，done 后自动
    re-preheat 新索引（修「构建后首查仍 2.88s 冻结」缺口）。"""
    def _watch():
        deadline = time.time() + BUILD_MAX_AGE_SECONDS + 60.0
        while time.time() < deadline:
            status = read_build_status(verdir)
            if status is not None:
                state = status.get("state")
                if state == "done":
                    logger.info(
                        "rag lifecycle: build done for %s (%s docs); "
                        "re-preheating", verdir, status.get("doc_count"))
                    _clear_preheat_terminal(os.path.join(
                        verdir, INDEX_FILENAME))
                    ensure_preheat()
                    return
                if state == "failed":
                    logger.warning(
                        "rag lifecycle: build failed for %s: %s", verdir,
                        status.get("error"))
                    return
            time.sleep(1.0)

    thread = threading.Thread(
        target=_watch, daemon=True, name="rag-build-watcher")
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# 预热（task 3.4，design D4）
# ---------------------------------------------------------------------------
def _clear_preheat_terminal(index_path):
    """文件变化后清除终态记录（允许重新预热）。"""
    with _STATE_LOCK:
        _preheat_results.pop(index_path, None)


def _file_stamp(path):
    try:
        stat_result = os.stat(path)
        return (stat_result.st_mtime_ns, stat_result.st_size)
    except OSError:
        return None


def ensure_preheat():
    """确保**当前解析**的索引的预热线程在跑（幂等、不阻塞）。

    返回 ``_rag.cache_status()`` 快照（调用方据此判 loaded/loading）。
    终态短路：上次预热已确认 missing/unavailable/stale 且文件未变化时
    不再重复拉线程（防 warming 死循环；让工具路径走原生错误 envelope）。
    """
    cs = _rag.cache_status()
    path = cs.get("path", "")
    if not path or not cs.get("file_exists"):
        return cs
    if cs.get("state") == "loaded":
        return cs

    stamp = _file_stamp(path)
    with _STATE_LOCK:
        result = _preheat_results.get(path)
        if result is not None:
            if stamp is not None and (result.get("mtime_ns"),
                                      result.get("size")) == stamp:
                # 同一文件的终态结果（missing/unavailable/stale）：
                # 重试无意义，短路（loaded 不会出现在终态记录里）
                return cs
            _preheat_results.pop(path, None)
        thread = _preheat_threads.get(path)
        if thread is not None and thread.is_alive():
            return cs
        thread = threading.Thread(
            target=_preheat_worker, args=(path,), daemon=True,
            name="rag-preheat")
        _preheat_threads[path] = thread
    thread.start()
    return cs


def _preheat_worker(path):
    """预热线程体：load_index 填充 LRU（结果记入 _preheat_results）。"""
    status = _rag.load_index(path)
    stamp = _file_stamp(path) or (None, None)
    with _STATE_LOCK:
        _preheat_results[path] = {
            "load_state": status.get("state", ""),
            "mtime_ns": stamp[0],
            "size": stamp[1],
        }


def _preheat_terminal_state(index_path):
    """该路径的预热终态（missing/unavailable/stale）；无终态返回 None。"""
    with _STATE_LOCK:
        result = _preheat_results.get(index_path)
    if not result:
        return None
    state = result.get("load_state", "")
    return state if state in ("missing", "unavailable", "stale") else None


# ---------------------------------------------------------------------------
# 轻量 envelope 与工具 gate（task 3.5）
# ---------------------------------------------------------------------------
def building_envelope(path_arg=None, index_path=""):
    """索引缺失 + 后台构建中：rag_index_missing envelope + building 字段。

    提示 agent 本轮回退 ``get_houdini_help``（在线通道不受影响）。"""
    env = {
        "status": "error",
        "error": {
            "code": "rag_index_missing",
            "message": "RAG index missing for this Houdini version; "
                       "background build in progress",
            "details": {
                "index_path": index_path,
                "hint": "fall back to get_houdini_help for this turn",
            },
        },
        "matched": 0,
        "returned": 0,
        "results": [],
        "building": True,
        "eta_hint": ETA_HINT,
    }
    if path_arg is not None:
        env["path"] = path_arg
        env["content"] = ""
    return env


def warming_envelope(path_arg=None, index_path=""):
    """索引存在但预热未完：warming_up 字段（区别于缺失语义，spec D7）。"""
    env = {
        "status": "error",
        "error": {
            "code": "rag_index_warming_up",
            "message": "index exists; background preheat in progress",
            "details": {"index_path": index_path},
        },
        "matched": 0,
        "returned": 0,
        "results": [],
        "warming_up": True,
    }
    if path_arg is not None:
        env["path"] = path_arg
        env["content"] = ""
    return env


def pre_tool_gate(tcp_call=None, path=None, response_cap_fn=None):
    """RAG 工具调用前的轻量 gate（search_docs / get_doc wrapper 用）。

    返回 ``None`` = 放行走正常检索（``_rag.search_docs`` / ``get_doc``）；
    返回 dict = 轻量 envelope（building / warming_up，已过 response cap）。
    绝不阻塞超过一次 TCP 查询的量级、绝不抛异常（兜底回退放行）。
    ``path`` 非 None 时表示 get_doc 调用（envelope 附 path/content 字段，
    与 _rag 原生 error envelope 形态一致）。
    """
    try:
        gate = _gate(tcp_call, path)
    except Exception:
        logger.exception("rag lifecycle gate failed; falling through")
        return None
    if gate is None:
        return None
    if callable(response_cap_fn):
        try:
            capped = response_cap_fn(gate)
            if isinstance(capped, dict):
                return capped
        except Exception:
            pass
    return gate


def _gate(tcp_call, path_arg):
    route = ensure_routed(tcp_call)
    if route["mode"] == "versioned":
        verdir = route["verdir"]
        index_file = os.path.join(verdir, INDEX_FILENAME)
        if not os.path.isfile(index_file):
            # 缺索引：封存旧 flat（幂等；仅当 rag 根 flat 尚存时生效）
            # → 触发/确认自动构建 → building envelope
            archive_legacy_flat()
            trigger_build(verdir, route.get("hfs_path", ""))
            return building_envelope(path_arg, index_file)
    # 索引存在（versioned）或 flat/env_pinned 模式：预热 fast-path
    cs = ensure_preheat()
    if cs.get("state") == "loaded":
        return None
    index_path = cs.get("path", "")
    if not cs.get("file_exists"):
        # flat 回退且无索引：放行走 _rag 原生 missing envelope（行为不变）
        return None
    if _preheat_terminal_state(index_path) is not None:
        # 预热已终态失败（missing/unavailable/stale）：放行走 _rag 原生
        # envelope（missing/unavailable/stale success + _index_warning）
        return None
    # loading / not_loaded（文件存在，预热已确保在跑）：warming_up
    return warming_envelope(path_arg, index_path)
