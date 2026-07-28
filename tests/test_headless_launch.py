"""headless launch 的 stdlib 单测。

这些测试只验证 bridge/host 的进程边界与协议约束，不用 mock 冒充
Houdini live smoke；真实 hython 验证由 ``hython_headless_e2e.py`` 负责。
"""
import ast
import io
import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_host():
    return _load("headless_host_test_module", os.path.join(
        ROOT, "headless_host.py"))


def _load_bridge():
    package = types.ModuleType("houdinimcp")
    package.__path__ = [ROOT]
    sys.modules["houdinimcp"] = package

    mcp = types.ModuleType("mcp")
    mcp_server = types.ModuleType("mcp.server")
    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class _FastMCP(object):
        def __init__(self, *args, **kwargs):
            self.lifespan = None

        def tool(self, *args, **kwargs):
            return lambda function: function

        def run(self):
            return None

    fastmcp.FastMCP = _FastMCP
    fastmcp.Context = object
    mcp_server.fastmcp = fastmcp
    mcp.server = mcp_server
    sys.modules["mcp"] = mcp
    sys.modules["mcp.server"] = mcp_server
    sys.modules["mcp.server.fastmcp"] = fastmcp

    requests = types.ModuleType("requests")
    requests.exceptions = types.SimpleNamespace(
        RequestException=Exception, HTTPError=Exception)
    sys.modules["requests"] = requests
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda **kwargs: None
    sys.modules["dotenv"] = dotenv

    _load("houdinimcp._render_policy", os.path.join(ROOT, "_render_policy.py"))
    return _load("houdini_mcp_server_test_module", os.path.join(
        ROOT, "houdini_mcp_server.py"))


def _load_capture_multiple_handler(pcp_result):
    path = os.path.join(ROOT, "server.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    server_class = next(node for node in tree.body
                        if isinstance(node, ast.ClassDef)
                        and node.name == "HoudiniMCPServer")
    method = next(node for node in server_class.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "capture_multiple_panes")

    cap_calls = []

    def apply_response_cap(value):
        cap_calls.append(value)
        return value

    pcp = types.SimpleNamespace(
        capture_multiple_panes=lambda *args, **kwargs: pcp_result)
    namespace = {
        "hou": object(),
        "pcp": pcp,
        "cmn": types.SimpleNamespace(apply_response_cap=apply_response_cap),
    }
    exec(compile(ast.get_source_segment(source, method),
                 "<capture_multiple_panes_handler>", "exec"), namespace)
    server_type = type("CaptureMultipleServer", (object,), {
        "capture_multiple_panes": namespace["capture_multiple_panes"]})
    return server_type(), cap_calls


class _TempEnvMixin(object):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()


class HostMetadataTests(_TempEnvMixin, unittest.TestCase):
    def setUp(self):
        super(HostMetadataTests, self).setUp()
        self.host = _load_host()
        self.original_env_dir = self.host._env_dir
        self.host._env_dir = lambda: self.tmpdir.name

    def tearDown(self):
        self.host._env_dir = self.original_env_dir
        super(HostMetadataTests, self).tearDown()

    def test_package_root_is_external_parent(self):
        self.assertEqual(self.host._PACKAGE_ROOT, os.path.dirname(ROOT))

    def test_idle_seconds_are_clamped(self):
        self.assertEqual(self.host._clamp_idle_seconds(1), 30.0)
        self.assertEqual(self.host._clamp_idle_seconds(999999), 86400.0)

    def test_runtime_cleanup_requires_pid_and_token(self):
        path = self.host._metadata_path("127.0.0.1", 10987,
                                        self.host._RUNTIME_SUFFIX)
        self.host._atomic_write_json(path, {
            "pid": os.getpid(), "owner_token": "new-token",
            "host": "127.0.0.1", "port": 10987,
        })
        self.assertFalse(self.host._remove_owned_file(
            path, "127.0.0.1", 10987, "old-token", pid=os.getpid()))
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(self.host._remove_owned_file(
            path, "127.0.0.1", 10987, "new-token", pid=os.getpid()))

    def test_headless_lock_release_accepts_matching_token_only(self):
        self.assertTrue(self.host._write_runtime_metadata(
            "127.0.0.1", 10987, "owner"))

    def test_run_starts_server_checks_running_and_cleans_owned_metadata(self):
        calls = []
        state = {"running": False}

        def start_server(host, port):
            calls.append(("start", host, port))
            state["running"] = True

        def stop_server():
            calls.append(("stop",))
            state["running"] = False

        api = {
            "start_server": start_server,
            "stop_server": stop_server,
            "is_server_running": lambda: state["running"],
        }

        class _App(object):
            current = None

            @classmethod
            def instance(cls):
                return cls.current

            def __init__(self, argv):
                _App.current = self

            def exec(self):
                return 0

            def quit(self):
                return None

        class _Timer(object):
            def __init__(self):
                self.timeout = types.SimpleNamespace(connect=lambda fn: None)

            def start(self, interval):
                self.interval = interval

            def stop(self):
                return None

        qt = types.SimpleNamespace(QCoreApplication=_App, QTimer=_Timer)
        result = self.host.run([
            "--host", "127.0.0.1", "--port", "10987",
            "--owner-token", "run-owner", "--idle-seconds", "30",
        ], server_api=api, qt_core=qt)
        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ("start", "127.0.0.1", 10987))
        self.assertEqual(calls[-1], ("stop",))
        self.assertFalse(os.path.exists(self.host._metadata_path(
            "127.0.0.1", 10987, self.host._RUNTIME_SUFFIX)))

    def test_idle_check_stops_owned_server_without_client(self):
        old_hou = sys.modules.get("hou")
        server = types.SimpleNamespace(
            client_presence=lambda: False,
            last_activity=lambda: time.monotonic() - 120,
        )
        hou = types.ModuleType("hou")
        hou.session = types.SimpleNamespace(houdinimcp_server=server)
        sys.modules["hou"] = hou
        stopped = []
        app = types.SimpleNamespace(quit=lambda: stopped.append("quit"))
        self.host._SERVER_API = {
            "stop_server": lambda: stopped.append("stop"),
            "start_server": lambda **kwargs: None,
            "is_server_running": lambda: True,
        }
        self.host._HOST = "127.0.0.1"
        self.host._PORT = 10987
        self.host._OWNER_TOKEN = "idle-owner"
        self.host._IDLE_SECONDS = 30.0
        self.host._APP = app
        self.host._SERVER_STARTED_HERE = True
        self.host._CLEANED = False
        self.host._write_runtime_metadata(
            "127.0.0.1", 10987, "idle-owner")
        try:
            self.host._check_idle()
            self.assertEqual(stopped, ["stop", "quit"])
        finally:
            if old_hou is None:
                sys.modules.pop("hou", None)
            else:
                sys.modules["hou"] = old_hou


class BridgeLockAndReadinessTests(_TempEnvMixin, unittest.TestCase):
    def setUp(self):
        super(BridgeLockAndReadinessTests, self).setUp()
        self.bridge = _load_bridge()
        self.original_env_dir = self.bridge._env_dir
        self.bridge._env_dir = lambda: self.tmpdir.name

    def tearDown(self):
        self.bridge._env_dir = self.original_env_dir
        super(BridgeLockAndReadinessTests, self).tearDown()

    def test_lock_competition_has_single_winner(self):
        winners = []

        def acquire(token):
            winners.append(self.bridge._headless_write_lock(
                "127.0.0.1", 10987, token))

        threads = [threading.Thread(target=acquire, args=("t{0}".format(i),))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(winners), [False, True])

    def test_stale_lock_checks_pid_token_port_and_age(self):
        path = self.bridge._headless_path(
            "127.0.0.1", 10987, self.bridge._HEADLESS_LOCK_SUFFIX)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "pid": 999999, "owner_token": "stale-token",
                "host": "127.0.0.1", "port": 10987,
                "created_at": time.time() - 120,
            }, handle)
        self.assertTrue(self.bridge._headless_lock_is_stale(
            "127.0.0.1", 10987))
        self.assertTrue(self.bridge._headless_recover_stale_lock(
            "127.0.0.1", 10987))
        self.assertFalse(os.path.exists(path))

    def test_frame_ping_requires_pong_response(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve_once():
            client, _address = listener.accept()
            try:
                header = client.recv(4)
                size = struct.unpack(">I", header)[0]
                client.recv(size)
                payload = json.dumps({
                    "status": "success", "result": {"pong": True}
                }).encode("utf-8")
                client.sendall(struct.pack(">I", len(payload)) + payload)
            finally:
                client.close()
                listener.close()

        thread = threading.Thread(target=serve_once)
        thread.start()
        try:
            self.assertTrue(self.bridge._headless_protocol_ping(
                "127.0.0.1", port, timeout=1))
        finally:
            thread.join(timeout=2)

    def test_log_drain_rotates_and_keeps_bounded_files(self):
        path = os.path.join(self.tmpdir.name, "logs", "daemon.log")
        writer = self.bridge._HeadlessRotatingLog(path)
        writer.write(b"a" * (1024 * 1024))
        writer.write(b"b" * (1024 * 1024))
        writer.write(b"c" * (1024 * 1024))
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.isfile(path + ".1"))
        self.assertTrue(os.path.isfile(path + ".2"))
        self.assertLessEqual(os.path.getsize(path), 1024 * 1024)

    def test_high_output_pipe_is_drained_without_blocking(self):
        path = os.path.join(self.tmpdir.name, "logs", "drain.log")
        writer = self.bridge._HeadlessRotatingLog(path)
        process = types.SimpleNamespace(
            stdout=io.BytesIO(b"x" * (3 * 1024 * 1024)))
        thread = threading.Thread(
            target=self.bridge._drain_headless_output,
            args=(process, writer))
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(os.path.isfile(path))

    def test_start_failure_returns_only_two_thousand_char_log_tail(self):
        token = "failure-token"
        path = self.bridge._headless_log_path("127.0.0.1", 10987)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("z" * 5000)
        self.bridge._headless_write_lock("127.0.0.1", 10987, token)
        error = self.bridge._headless_start_failure(
            "127.0.0.1", 10987, token, "boom")
        self.assertIsInstance(error, ConnectionError)
        self.assertLessEqual(str(error).count("z"), 2000)
        self.assertFalse(os.path.exists(self.bridge._headless_path(
            "127.0.0.1", 10987, self.bridge._HEADLESS_LOCK_SUFFIX)))

    def test_no_headless_gate_never_starts_process(self):
        original = os.environ.get("HOUDINIMCP_NO_HEADLESS")
        os.environ["HOUDINIMCP_NO_HEADLESS"] = "true"
        try:
            with self.assertRaises(ConnectionError):
                self.bridge._ensure_headless_daemon("127.0.0.1", 10987)
        finally:
            if original is None:
                os.environ.pop("HOUDINIMCP_NO_HEADLESS", None)
            else:
                os.environ["HOUDINIMCP_NO_HEADLESS"] = original

    def test_concurrent_ensure_starts_one_process(self):
        state = {"ready": False, "starts": 0}
        original_ping = self.bridge._headless_protocol_ping
        original_listening = self.bridge._headless_port_listening
        original_start = self.bridge._start_headless_process
        original_wait = self.bridge._wait_for_headless_ready

        class _Process(object):
            def poll(self):
                return None

        def fake_ping(host, port, timeout=0.5):
            return state["ready"]

        def fake_listening(host, port, timeout=0.25):
            return False

        def fake_start(host, port, token):
            state["starts"] += 1
            return _Process()

        def fake_wait(host, port, timeout=None, process=None):
            state["ready"] = True
            return True

        self.bridge._headless_protocol_ping = fake_ping
        self.bridge._headless_port_listening = fake_listening
        self.bridge._start_headless_process = fake_start
        self.bridge._wait_for_headless_ready = fake_wait
        try:
            results = []

            def ensure():
                results.append(self.bridge._ensure_headless_daemon(
                    "127.0.0.1", 10987))

            threads = [threading.Thread(target=ensure) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertEqual(results, [True, True])
            self.assertEqual(state["starts"], 1)
        finally:
            self.bridge._headless_protocol_ping = original_ping
            self.bridge._headless_port_listening = original_listening
            self.bridge._start_headless_process = original_start
            self.bridge._wait_for_headless_ready = original_wait

    def test_old_token_cannot_shutdown_new_daemon(self):
        path = self.bridge._headless_path(
            "127.0.0.1", 10987, self.bridge._HEADLESS_RUNTIME_SUFFIX)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "pid": os.getpid(), "owner_token": "new-token",
                "host": "127.0.0.1", "port": 10987,
                "created_at": time.time(),
            }, handle)
        with mock.patch.object(self.bridge.os, "kill") as kill:
            self.assertFalse(self.bridge._shutdown_headless_daemon(
                "127.0.0.1", 10987, "old-token", pid=os.getpid()))
            kill.assert_not_called()
            self.assertTrue(self.bridge._shutdown_headless_daemon(
                "127.0.0.1", 10987, "new-token", pid=os.getpid()))
            kill.assert_called_once()

    def test_non_default_port_is_forwarded_to_hython_command(self):
        original_find = self.bridge._find_hython
        self.bridge._find_hython = lambda: "C:/Houdini/bin/hython.exe"
        fake_process = types.SimpleNamespace(stdout=io.BytesIO(b""))
        try:
            with mock.patch.object(
                    self.bridge.subprocess, "Popen",
                    return_value=fake_process) as popen:
                self.bridge._start_headless_process(
                    "127.0.0.1", 10987, "port-token")
                command = popen.call_args[0][0]
                self.assertIn("--port", command)
                self.assertEqual(command[command.index("--port") + 1], "10987")
                self.assertIn("headless_host.py", command[1])
        finally:
            self.bridge._find_hython = original_find


class HeadlessPaneWarningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        package = sys.modules.get("houdinimcp")
        if package is None:
            package = types.ModuleType("houdinimcp")
            package.__path__ = [ROOT]
            sys.modules["houdinimcp"] = package
        _load("houdinimcp._common", os.path.join(ROOT, "_common.py"))
        _load("houdinimcp._capture_paths", os.path.join(
            ROOT, "_capture_paths.py"))
        cls.pcp = _load("houdinimcp._pane_capture", os.path.join(
            ROOT, "_pane_capture.py"))

    def test_all_capture_entries_return_structured_warning(self):
        class _HeadlessHou(object):
            def isUIAvailable(self):
                return False

        hou = _HeadlessHou()
        calls = [
            self.pcp.capture_pane_screenshot(hou, "SceneViewer"),
            self.pcp.capture_multiple_panes(hou, ["SceneViewer"],
                                            tempfile.gettempdir()),
            self.pcp.render_node_network(hou, "/obj/geo1"),
            self.pcp.capture_sceneviewer_flipbook_views(hou),
        ]
        for result in calls:
            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["_warning"]["code"], "ui_unavailable")
            self.assertTrue(result["_warning"]["headless"])

    def test_gui_missing_node_keeps_value_error(self):
        class _GuiHou(object):
            isUIAvailable = lambda self: True
            node = lambda self, path: None
            ui = types.SimpleNamespace(curDesktop=lambda: object())

        with self.assertRaises(ValueError):
            self.pcp.render_node_network(_GuiHou(), "/obj/missing")


class CaptureMultipleHandlerBoundaryTests(unittest.TestCase):
    def test_headless_warning_is_not_wrapped_as_results(self):
        warning = {
            "status": "warning",
            "_warning": {
                "code": "ui_unavailable",
                "message": "no desktop or pane available in headless Houdini",
                "headless": True,
            },
        }
        server, cap_calls = _load_capture_multiple_handler(warning)
        result = server.capture_multiple_panes(["SceneViewer"], "unused")
        self.assertEqual(result, warning)
        self.assertEqual(result["status"], "warning")
        self.assertNotIn("results", result)
        self.assertEqual(len(cap_calls), 1)


class HeadlessE2EEntrypointTests(unittest.TestCase):
    def test_live_e2e_uses_bridge_lazy_launcher(self):
        path = os.path.join(HERE, "hython_headless_e2e.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        self.assertTrue(any(
            isinstance(node, ast.FunctionDef) and node.name == "_run_bridge"
            for node in tree.body))
        self.assertIn("bridge._houdini_port", source)
        self.assertIn("bridge._houdini_call", source)
        self.assertIn("bridge._HEADLESS_PROCESSES", source)
        self.assertIn("subprocess.run", source)
        self.assertNotIn("subprocess.Popen", source)
        self.assertNotIn('os.path.join(ROOT, "headless_host.py")', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
