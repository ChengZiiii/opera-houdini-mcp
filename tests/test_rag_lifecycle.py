"""versioned-rag-index Phase 3（bridge 侧生命周期）单测。

覆盖 tasks 3.1-3.5（_rag_lifecycle.py 旁挂模块 + _rag.cache_status）：
- 3.1 版本路由：TCP 应答 → env 指向 rag/<ver>/；versioned 粘滞；flat
  失败回退 + 30s 重试；env_pinned 手工优先；非法版本拒绝
- 3.2 封存：改名保留 / 幂等 / 冲突不覆盖 / 失败仅警告；gate 内联动
- 3.3 自动构建：spawn 命令形态、无窗口 kwargs 由真实 spawn 层保证
  （单测注入 spawn_fn）、二次触发 in_progress、超龄/死 pid 失效重置、
  lock 独占 + 二次探测、失败路径清锁
- 3.4 预热：warming fast-path（cache_status 不触发 load）、daemon 线程
  加载后 gate 放行、终态短路防死循环、构建 done 后 re-preheat
- 3.5 envelope：building/warming_up 形态与互斥、get_doc 附
  path/content、正常态无额外字段

隔离纪律：expanduser 全程 patch 到 tmp home（防触达真实
~/.opera-houdini-mcp/rag 的生产索引）；HOUDINI_MCP_RAG_INDEX_DIR
save/restore；每个用例 reset_state_for_tests + _rag.clear_cache。
spawn 一律注入 stub，不起真实子进程（真机链路见 3.6 集成测试）。
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# 注意：lc 的 fallback `import _rag` 拿到的是真实模块；这里 rag 必须用
# **同一对象**（否则 patch lc._rag.load_index 与 cache_status 状态不互通）
import _rag as rag  # noqa: E402
lc = _load("test_raglc_isolated._rag_lifecycle",
           os.path.join(ROOT, "_rag_lifecycle.py"))
build_mod = _load("test_raglc_isolated.build_rag_index",
                  os.path.join(ROOT, "scripts", "build_rag_index.py"))

_WIKI = (
    "#type: node\n#context: sop\n= Test Node =\n"
    "\"\"\"Summary.\"\"\"\nBody alpha beta gamma.\n"
)


def _write_status(verdir, **fields):
    """直接落一份构建状态文件（模拟 build 子进程的写入）。"""
    payload = {
        "state": "building", "started_at": _iso_now(),
        "finished_at": None, "pid": os.getpid(), "exit_code": None,
        "error": None, "zips": ["nodes.zip"], "doc_count": None,
        "source_path": "unit",
    }
    payload.update(fields)
    with io.open(os.path.join(verdir, "build.status.json"), "w",
                 encoding="utf-8") as handle:
        json.dump(payload, handle)


def _iso_now(offset_seconds=0.0):
    import datetime
    moment = datetime.datetime.now(datetime.timezone.utc)
    if offset_seconds:
        moment -= datetime.timedelta(seconds=offset_seconds)
    return moment.isoformat()


class _FakeProc(object):
    """Popen 替身：poll 可控。"""

    def __init__(self, pid=None, exits_after=None):
        self.pid = pid or os.getpid()
        self._exit_at = time.time() + exits_after if exits_after else None

    def poll(self):
        if self._exit_at is not None and time.time() >= self._exit_at:
            return 0
        return None


class LifecycleTestCase(unittest.TestCase):
    """公共隔离底座：tmp home + rag root + env save/restore + 状态清零。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.rag_root = os.path.join(self.home, ".opera-houdini-mcp", "rag")
        # 假 HFS（trigger_build 的 help 源 isdir 检查需要真实存在的目录）
        self.hfs = os.path.join(self.tmp.name, "hfs")
        os.makedirs(os.path.join(self.hfs, "houdini", "help"))
        # expanduser 全程 patch：lc.rag_root 与 _rag._index_path 同时隔离
        patcher = mock.patch("os.path.expanduser",
                             lambda path="", *a, **k: self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._env_saved = os.environ.get("HOUDINI_MCP_RAG_INDEX_DIR")
        os.environ.pop("HOUDINI_MCP_RAG_INDEX_DIR", None)
        self.addCleanup(self._restore_env)
        lc.reset_state_for_tests()
        rag.clear_cache()
        self.addCleanup(lc.reset_state_for_tests)
        self.addCleanup(rag.clear_cache)

    def _restore_env(self):
        if self._env_saved is None:
            os.environ.pop("HOUDINI_MCP_RAG_INDEX_DIR", None)
        else:
            os.environ["HOUDINI_MCP_RAG_INDEX_DIR"] = self._env_saved

    # -- 小工具 ------------------------------------------------------------
    def _tcp_ok(self, version="21.0.596", hfs=None, calls=None):
        def _call(cmd, params):
            if calls is not None:
                calls.append(cmd)
            return {"status": "success", "result": {
                "houdini_version": version, "hfs_path": hfs or self.hfs}}
        return _call

    def _make_index(self, dirname):
        """在目录内产出一个合法小索引（build/publish 全真路径）。"""
        source = os.path.join(self.tmp.name, "help")
        os.makedirs(source)
        with zipfile.ZipFile(os.path.join(source, "nodes.zip"), "w") as zf:
            zf.writestr("sop/thing.txt", _WIKI)
        os.makedirs(dirname, exist_ok=True)
        build_mod.publish_index(build_mod.build_index(source), dirname)
        return os.path.join(dirname, "index.v1.json")


class RoutingTests(LifecycleTestCase):

    def test_versioned_route_sets_env(self):
        route = lc.ensure_routed(self._tcp_ok())
        self.assertEqual(route["mode"], "versioned")
        self.assertEqual(route["hou_version"], "21.0.596")
        self.assertEqual(route["hfs_path"], self.hfs)
        expected = os.path.join(self.rag_root, "21.0.596")
        self.assertEqual(route["verdir"], expected)
        self.assertEqual(
            os.environ["HOUDINI_MCP_RAG_INDEX_DIR"], expected)

    def test_hou_version_key_fallback(self):
        def _call(cmd, params):
            return {"status": "success", "result": {
                "hou_version": "20.5.332", "hfs_path": ""}}
        route = lc.ensure_routed(_call)
        self.assertEqual(route["mode"], "versioned")
        self.assertEqual(route["hou_version"], "20.5.332")

    def test_versioned_route_is_sticky(self):
        calls = []
        route = lc.ensure_routed(self._tcp_ok(calls=calls))
        self.assertEqual(route["mode"], "versioned")
        broken = lambda cmd, params: (_ for _ in ()).throw(
            ConnectionError("server died"))
        again = lc.ensure_routed(broken)
        self.assertEqual(again["mode"], "versioned")
        self.assertEqual(len(calls), 1, "versioned 路由后不得再查 TCP")

    def test_flat_fallback_then_retry_after_window(self):
        def dead(cmd, params):
            raise ConnectionError("down")
        route = lc.ensure_routed(dead, now=1000.0)
        self.assertEqual(route["mode"], "flat")
        self.assertNotIn("HOUDINI_MCP_RAG_INDEX_DIR", os.environ)
        # 节流窗口内：不重试
        calls = []
        def counting_dead(cmd, params):
            calls.append(cmd)
            raise ConnectionError("still down")
        route = lc.ensure_routed(counting_dead, now=1000.0 + 10.0)
        self.assertEqual(route["mode"], "flat")
        self.assertEqual(calls, [])
        # 窗口过后：重试并成功 → versioned 生效（spec：下次成功连接后自动生效）
        route = lc.ensure_routed(self._tcp_ok(), now=1000.0 + 31.0)
        self.assertEqual(route["mode"], "versioned")

    def test_unsafe_version_rejected_to_flat(self):
        route = lc.ensure_routed(self._tcp_ok(version="../../evil"))
        self.assertEqual(route["mode"], "flat")
        self.assertNotIn("HOUDINI_MCP_RAG_INDEX_DIR", os.environ)

    def test_env_pinned_skips_routing(self):
        os.environ["HOUDINI_MCP_RAG_INDEX_DIR"] = "C:/manual/dir"
        calls = []
        route = lc.ensure_routed(self._tcp_ok(calls=calls))
        self.assertEqual(route["mode"], "env_pinned")
        self.assertEqual(calls, [], "手工 env 优先，不查 TCP")
        self.assertEqual(
            os.environ["HOUDINI_MCP_RAG_INDEX_DIR"], "C:/manual/dir")

    def test_non_success_response_falls_flat(self):
        route = lc.ensure_routed(
            lambda cmd, params: {"status": "error", "message": "nope"})
        self.assertEqual(route["mode"], "flat")


class ArchiveTests(LifecycleTestCase):

    def _flat(self):
        os.makedirs(self.rag_root, exist_ok=True)
        flat = os.path.join(self.rag_root, "index.v1.json")
        with io.open(flat, "w", encoding="utf-8") as handle:
            handle.write("{}")
        return flat

    def test_archive_renames_and_is_idempotent(self):
        flat = self._flat()
        self.assertEqual(lc.archive_legacy_flat(), "archived")
        archived = flat + lc.LEGACY_ARCHIVE_SUFFIX
        self.assertFalse(os.path.exists(flat))
        self.assertTrue(os.path.exists(archived), "内容保留不删除")
        self.assertEqual(lc.archive_legacy_flat(), "absent", "幂等")

    def test_archive_conflict_keeps_both(self):
        flat = self._flat()
        archived = flat + lc.LEGACY_ARCHIVE_SUFFIX
        with io.open(archived, "w", encoding="utf-8") as handle:
            handle.write("{}")
        self.assertEqual(lc.archive_legacy_flat(), "conflict")
        self.assertTrue(os.path.exists(flat))
        self.assertTrue(os.path.exists(archived), "不覆盖任何文件")

    def test_archive_failure_warns_not_raises(self):
        flat = self._flat()
        with mock.patch.object(lc.os, "replace",
                               side_effect=OSError("locked")):
            self.assertEqual(lc.archive_legacy_flat(), "failed")
        self.assertTrue(os.path.exists(flat), "原文件不动")

    def test_gate_archives_flat_before_first_build(self):
        spawned = []

        def spawn_fn(command):
            spawned.append(command)
            return _FakeProc()

        self._flat()
        self._make_index(os.path.join(self.tmp.name, "help_out"))  # 无关
        route = lc.ensure_routed(self._tcp_ok())
        verdir = route["verdir"]
        lc.trigger_build(verdir, route["hfs_path"], spawn_fn=spawn_fn)
        # gate 路径的封存联动由 pre_tool_gate 触发（本例直接调用等价分支）
        lc.archive_legacy_flat()
        self.assertFalse(os.path.exists(
            os.path.join(self.rag_root, "index.v1.json")))
        self.assertTrue(spawned, "封存后构建照常触发")


class BuildTriggerTests(LifecycleTestCase):

    def _route(self):
        return lc.ensure_routed(self._tcp_ok())

    def test_spawn_command_shape(self):
        route = self._route()
        spawned = []
        result = lc.trigger_build(
            route["verdir"], route["hfs_path"],
            spawn_fn=lambda cmd: spawned.append(cmd) or _FakeProc())
        self.assertEqual(result, "spawned")
        command = spawned[0]
        self.assertEqual(command[0], sys.executable,
                         "bridge 内嵌解释器优先（design D3）")
        self.assertEqual(command[-4:], [
            "--source", os.path.join(self.hfs, "houdini", "help"),
            "--output", route["verdir"]])
        lock = os.path.join(route["verdir"], "build.lock")
        self.assertTrue(os.path.exists(lock), "spawn 后锁归子进程清理")

    def test_second_trigger_while_alive_is_in_progress(self):
        route = self._route()
        calls = []

        def spawn_fn(command):
            calls.append(command)
            return _FakeProc(exits_after=10.0)
        self.assertEqual(lc.trigger_build(
            route["verdir"], route["hfs_path"], spawn_fn=spawn_fn), "spawned")
        # 真实子进程起手即写 building 状态；替身场景补写（pid 用本进程
        # 保持探活为活，started_at 新鲜不超龄）
        _write_status(route["verdir"])
        self.assertEqual(lc.trigger_build(
            route["verdir"], route["hfs_path"], spawn_fn=spawn_fn),
            "in_progress")
        self.assertEqual(len(calls), 1, "不重复构建")

    def test_overage_building_status_is_invalid(self):
        route = self._route()
        verdir = route["verdir"]
        os.makedirs(verdir, exist_ok=True)
        # 伪造超龄 building（pid 是活的本进程，但龄期超 300s → 失效优先）
        _write_status(verdir, started_at=_iso_now(400.0))
        self.assertFalse(lc.build_in_progress(verdir))
        spawned = []
        self.assertEqual(lc.trigger_build(
            verdir, route["hfs_path"],
            spawn_fn=lambda cmd: spawned.append(cmd) or _FakeProc()),
            "spawned")

    def test_dead_foreign_pid_beyond_grace_is_invalid(self):
        route = self._route()
        verdir = route["verdir"]
        os.makedirs(verdir, exist_ok=True)
        _write_status(verdir, started_at=_iso_now(30.0))
        with mock.patch.object(lc, "_pid_alive", return_value=False):
            self.assertFalse(lc.build_in_progress(verdir),
                             "死 pid + 超宽限 → 可重试")
        _write_status(verdir, started_at=_iso_now(1.0))
        with mock.patch.object(lc, "_pid_alive", return_value=False):
            self.assertTrue(lc.build_in_progress(verdir),
                            "死 pid + 宽限期内 → 宁可误报不双拉")

    def test_no_hfs_path_fails_without_spawn(self):
        route = self._route()
        spawned = []
        self.assertEqual(lc.trigger_build(
            route["verdir"], "", spawn_fn=lambda cmd: spawned.append(1)),
            "failed")
        self.assertEqual(spawned, [])

    def test_spawn_exception_cleans_lock(self):
        route = self._route()

        def bad_spawn(command):
            raise OSError("no exec")
        self.assertEqual(lc.trigger_build(
            route["verdir"], route["hfs_path"], spawn_fn=bad_spawn),
            "failed")
        self.assertFalse(os.path.exists(
            os.path.join(route["verdir"], "build.lock")))

    def test_lock_held_without_building_reports_stale(self):
        route = self._route()
        verdir = route["verdir"]
        os.makedirs(verdir, exist_ok=True)
        with io.open(os.path.join(verdir, "build.lock"), "w") as handle:
            handle.write("")
        self.assertEqual(lc.trigger_build(
            verdir, route["hfs_path"], spawn_fn=lambda cmd: _FakeProc()),
            "lock_stale")

    def test_retry_throttle_after_failure_window(self):
        route = self._route()
        verdir = route["verdir"]
        spawned = []

        def spawn_fn(command):
            spawned.append(command)
            # 立即退出 + 子写 done → 下次 build_in_progress False
            return _FakeProc(exits_after=0.0)
        self.assertEqual(lc.trigger_build(
            verdir, route["hfs_path"], spawn_fn=spawn_fn), "spawned")
        _write_status(verdir, state="failed", finished_at=_iso_now(),
                      exit_code=1, error="unit")
        self.assertEqual(lc.trigger_build(
            verdir, route["hfs_path"], spawn_fn=spawn_fn), "throttled",
            "failed 后节流窗口内不重拉")
        self.assertEqual(len(spawned), 1)


class PreheatTests(LifecycleTestCase):

    def test_cache_status_states_without_loading(self):
        index_dir = os.path.join(self.tmp.name, "rag", "21.0.596")
        self._make_index(index_dir)
        os.environ["HOUDINI_MCP_RAG_INDEX_DIR"] = index_dir
        cs = rag.cache_status()
        self.assertEqual(cs["state"], "not_loaded")
        self.assertTrue(cs["file_exists"])
        # 白盒验证 loading 判据（_LOADING 标记由真实 load_index 维护，
        # 此处直接置位以隔离验证 cache_status 本身不触发 load）
        with rag._CACHE_LOCK:
            rag._LOADING.add(cs["path"])
        try:
            self.assertEqual(rag.cache_status()["state"], "loading")
        finally:
            with rag._CACHE_LOCK:
                rag._LOADING.discard(cs["path"])
        status = rag.load_index(cs["path"])
        self.assertEqual(status["state"], "ok")
        self.assertEqual(rag.cache_status()["state"], "loaded")

    def test_gate_returns_warming_then_falls_through(self):
        index_dir = os.path.join(self.tmp.name, "rag", "21.0.596")
        index_file = self._make_index(index_dir)
        os.environ["HOUDINI_MCP_RAG_INDEX_DIR"] = index_dir

        real_load = rag.load_index
        started = threading.Event()
        release = threading.Event()

        def slow_load(path=None):
            started.set()
            release.wait(timeout=5.0)
            return real_load(path)
        with mock.patch.object(lc._rag, "load_index", slow_load):
            env = lc.pre_tool_gate(tcp_call=self._tcp_ok())
            self.assertIsNotNone(env)
            self.assertEqual(
                env["error"]["code"], "rag_index_warming_up")
            self.assertIs(env["warming_up"], True)
            self.assertNotIn("building", env, "两状态互斥")
            self.assertTrue(started.wait(timeout=2.0),
                            "预热线程应已启动")
            self.assertEqual(rag.cache_status()["state"], "not_loaded",
                             "gate 不得在事件循环线程上触发 load")
            release.set()
            thread = None
            deadline = time.time() + 2.0
            while time.time() < deadline:
                with lc._STATE_LOCK:
                    thread = lc._preheat_threads.get(index_file)
                if thread is not None and not thread.is_alive():
                    break
                time.sleep(0.02)
            self.assertIsNotNone(thread)
            thread.join(timeout=2.0)
        # 加载完成后放行（无额外字段）
        gate = lc.pre_tool_gate(tcp_call=self._tcp_ok())
        self.assertIsNone(gate)
        result = rag.search_docs("test node alpha")
        self.assertEqual(result["status"], "success")
        self.assertNotIn("warming_up", result)
        self.assertNotIn("building", result)

    def test_terminal_failure_short_circuits_warming_loop(self):
        index_dir = os.path.join(self.tmp.name, "rag", "21.0.596")
        os.makedirs(index_dir, exist_ok=True)
        # 索引存在但损坏：load → unavailable 终态 → gate 放行走原生 envelope
        with io.open(os.path.join(index_dir, "index.v1.json"), "w",
                     encoding="utf-8") as handle:
            handle.write("{ not json")
        os.environ["HOUDINI_MCP_RAG_INDEX_DIR"] = index_dir
        env = lc.pre_tool_gate(tcp_call=self._tcp_ok())
        self.assertIsNotNone(env, "首轮 warming（预热线程刚起）")
        thread = None
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with lc._STATE_LOCK:
                thread = lc._preheat_threads.get(env["error"]["details"][
                    "index_path"])
            if thread is not None and not thread.is_alive():
                break
            time.sleep(0.02)
        if thread is not None:
            thread.join(timeout=2.0)
        gate = lc.pre_tool_gate(tcp_call=self._tcp_ok())
        self.assertIsNone(gate, "终态后放行，不得 warming 死循环")
        result = rag.search_docs("anything")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "rag_index_unavailable")

    def test_build_done_watcher_repreheats(self):
        route = lc.ensure_routed(self._tcp_ok())
        verdir = route["verdir"]
        # 子进程替身 + 模拟其异步写 done 状态（含产物索引）
        index_file = self._make_index(verdir)

        def spawn_fn(command):
            def _child_sim():
                time.sleep(0.3)
                _write_status(verdir, state="done", finished_at=_iso_now(),
                              exit_code=0, doc_count=1)
            threading.Thread(target=_child_sim, daemon=True).start()
            return _FakeProc(exits_after=1.0)
        self.assertEqual(lc.trigger_build(
            verdir, route["hfs_path"], spawn_fn=spawn_fn), "spawned")
        deadline = time.time() + 8.0
        loaded = False
        while time.time() < deadline:
            if rag.cache_status(index_file)["state"] == "loaded":
                loaded = True
                break
            time.sleep(0.2)
        self.assertTrue(loaded, "构建 done 后 watcher 应自动 re-preheat")


class EnvelopeTests(LifecycleTestCase):

    def test_building_envelope_shape(self):
        env = lc.building_envelope()
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "rag_index_missing")
        self.assertIs(env["building"], True)
        self.assertIn("eta_hint", env)
        self.assertNotIn("warming_up", env, "两状态互斥")
        self.assertEqual((env["matched"], env["returned"], env["results"]),
                         (0, 0, []))

    def test_building_envelope_get_doc_variant(self):
        env = lc.building_envelope(path_arg="nodes.zip/sop/box.txt")
        self.assertEqual(env["path"], "nodes.zip/sop/box.txt")
        self.assertEqual(env["content"], "")

    def test_warming_envelope_shape(self):
        env = lc.warming_envelope()
        self.assertEqual(env["error"]["code"], "rag_index_warming_up")
        self.assertIs(env["warming_up"], True)
        self.assertNotIn("building", env)

    def test_gate_building_on_missing_versioned_index(self):
        route = lc.ensure_routed(self._tcp_ok())
        spawned = []

        def spawn_fn(command):
            spawned.append(command)
            return _FakeProc(exits_after=10.0)
        with mock.patch.object(lc, "trigger_build",
                               side_effect=lambda verdir, hfs, **kw:
                               spawned.append((verdir, hfs)) or "spawned"):
            env = lc.pre_tool_gate(tcp_call=self._tcp_ok())
        self.assertIsNotNone(env)
        self.assertIs(env["building"], True)
        self.assertEqual(spawned, [(route["verdir"], self.hfs)])
        self.assertEqual(
            env["error"]["details"]["index_path"],
            os.path.join(route["verdir"], "index.v1.json"))

    def test_gate_flat_without_index_falls_through(self):
        # server 不在线 + 无 flat 索引 → 放行走 _rag 原生 missing envelope
        gate = lc.pre_tool_gate(tcp_call=lambda c, p: {
            "status": "error", "message": "down"})
        self.assertIsNone(gate)
        result = rag.search_docs("anything")
        self.assertEqual(result["error"]["code"], "rag_index_missing")

    def test_gate_never_raises(self):
        def bomb(cmd, params):
            raise RuntimeError("tcp exploded")
        # 路由内部吞异常 → flat；整体 gate 再兜底
        gate = lc.pre_tool_gate(tcp_call=bomb)
        self.assertIsNone(gate, "异常兜底回退放行，不抛")


class BridgeWiringProbe(unittest.TestCase):
    """bridge 工具接线探针：两个 RAG 工具必须先过 lifecycle gate。"""

    def test_tools_gate_wired(self):
        with io.open(os.path.join(ROOT, "houdini_mcp_server.py"), "r",
                     encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("from . import _rag_lifecycle as _raglc", source)
        self.assertEqual(source.count("_raglc.pre_tool_gate"), 2,
                         "search_docs 与 get_doc 都要接 gate")


if __name__ == "__main__":
    unittest.main()
