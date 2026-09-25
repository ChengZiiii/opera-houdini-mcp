# -*- coding: utf-8 -*-
"""feat-mcp-console-log-audit §1 单测：环形缓冲 / tee / append_capture / query。

覆盖 spec 场景：tee 幂等、print 后可读回、总开关关闭、execute_code 捕获
同步（append_capture）、时间窗过滤、clear 后为空。
"""

import ast
import importlib
import importlib.util
import io
import unittest
import os
import sys
import time

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONSOLE_LOG_PATH = os.path.join(ROOT, "_console_log.py")


def _load():
    sys.modules.pop("console_log_test_pkg", None)
    spec = importlib.util.spec_from_file_location(
        "console_log_test_pkg", CONSOLE_LOG_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["console_log_test_pkg"] = module
    spec.loader.exec_module(module)
    return module


clog = _load()


@pytest.fixture(autouse=True)
def _fresh_module(monkeypatch):
    """每测试重载模块：清空模块级 ring 与包装状态，env 回默认。"""
    monkeypatch.delenv("HOUDINI_MCP_CONSOLE_LOG", raising=False)
    monkeypatch.delenv("HOUDINI_MCP_CONSOLE_LOG_LINES", raising=False)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    global clog
    clog = _load()
    yield clog
    # 恢复被包装的流，避免污染其他测试 / pytest 捕获
    sys.stdout = original_stdout
    sys.stderr = original_stderr


def _write_and_flush(text):
    sys.stdout.write(text)
    sys.stdout.flush()
    sys.stdout.write("\n")
    sys.stdout.flush()


class TestTeeInstall:
    def test_install_wraps_and_records(self, _fresh_module):
        wrapped_out, wrapped_err = clog.install_tee()
        assert wrapped_out and wrapped_err
        assert getattr(sys.stdout, "_mcp_console_tee", False)
        _write_and_flush("marker-x")
        rows, total, mode = clog.query()
        assert any("marker-x" in r["text"] for r in rows)
        assert total >= 1
        assert mode == "paged"

    def test_install_idempotent(self, _fresh_module):
        clog.install_tee()
        first = sys.stdout
        wrapped_out, wrapped_err = clog.install_tee()
        # 二次安装不新包装（返回 False）且对象不变
        assert not wrapped_out and not wrapped_err
        assert sys.stdout is first
        # 同一条 print 只出现一次
        _write_and_flush("only-once")
        rows, _, _ = clog.query()
        assert sum(1 for r in rows if "only-once" in r["text"]) == 1

    def test_env_disable_skips_wrap(self, monkeypatch, _fresh_module):
        monkeypatch.setenv("HOUDINI_MCP_CONSOLE_LOG", "0")
        wrapped_out, wrapped_err = clog.install_tee()
        assert not wrapped_out and not wrapped_err
        assert not getattr(sys.stdout, "_mcp_console_tee", False)
        _write_and_flush("should-not-record")
        rows, total, _ = clog.query()
        assert rows == [] and total == 0

    def test_partial_line_assembles_across_writes(self, _fresh_module):
        clog.install_tee()
        sys.stdout.write("partial-")
        sys.stdout.write("line\n")
        sys.stdout.flush()
        rows, _, _ = clog.query()
        assert any(r["text"] == "partial-line" for r in rows)

    def test_stderr_stream_tagged(self, _fresh_module):
        clog.install_tee()
        sys.stderr.write("err-marker\n")
        sys.stderr.flush()
        rows, _, _ = clog.query()
        match = [r for r in rows if "err-marker" in r["text"]]
        assert match and match[0]["stream"] == "stderr"


class TestAppendCapture:
    def test_capture_lines_enter_ring(self, _fresh_module):
        clog.append_capture("stdout", "captured-a\ncaptured-b\n")
        rows, total, _ = clog.query()
        texts = [r["text"] for r in rows]
        assert "captured-a" in texts and "captured-b" in texts
        assert all(r["stream"] == "stdout" for r in rows)

    def test_capture_empty_is_noop(self, _fresh_module):
        clog.append_capture("stdout", "")
        rows, total, _ = clog.query()
        assert rows == [] and total == 0


class TestQuery:
    def test_last_seconds_filter(self, _fresh_module):
        # 直接注入两条不同时间戳的行
        clog._ring._entries.append((time.time() - 10.0, "stdout", "old-line"))
        clog._ring._entries.append((time.time() - 0.5, "stdout", "new-line"))
        rows, total, mode = clog.query(last_seconds=6)
        assert mode == "last_seconds"
        assert total == 1
        assert rows[0]["text"] == "new-line"

    def test_last_seconds_takes_priority_over_tail(self, _fresh_module):
        clog._ring._entries.append((time.time() - 10.0, "stdout", "old-a"))
        clog._ring._entries.append((time.time() - 1.0, "stdout", "new-b"))
        _, _, mode = clog.query(tail=1, last_seconds=5)
        assert mode == "last_seconds"

    def test_tail_reads_recent(self, _fresh_module):
        for i in range(10):
            clog._ring._entries.append((time.time(), "stdout", f"l{i}"))
        rows, total, mode = clog.query(tail=3)
        assert mode == "tail"
        assert total == 10
        assert [r["text"] for r in rows] == ["l7", "l8", "l9"]

    def test_paging(self, _fresh_module):
        for i in range(10):
            clog._ring._entries.append((time.time(), "stdout", f"p{i}"))
        rows, total, mode = clog.query(offset=2, limit=3)
        assert mode == "paged"
        assert total == 10
        assert [r["text"] for r in rows] == ["p2", "p3", "p4"]

    def test_ring_evicts_oldest(self, monkeypatch):
        monkeypatch.delenv("HOUDINI_MCP_CONSOLE_LOG_LINES", raising=False)
        monkeypatch.setenv("HOUDINI_MCP_CONSOLE_LOG_LINES", "3")
        module = _load()
        for i in range(5):
            module.append_capture("stdout", f"e{i}")
        rows, total, _ = module.query()
        assert total == 3
        assert [r["text"] for r in rows] == ["e2", "e3", "e4"]


class TestClear:
    def test_clear_returns_prior_count_and_empties(self, _fresh_module):
        clog.append_capture("stdout", "a\nb\n")
        removed = clog.clear()
        assert removed == 2
        rows, total, _ = clog.query()
        assert rows == [] and total == 0


# ---------------------------------------------------------------------------
# Section 2: server handler 行为（伪包 houdinimcp，仿 test_animation 模式）
# ---------------------------------------------------------------------------
import types


def _ensure_pkg():
    if ("houdinimcp" in sys.modules
            and getattr(sys.modules["houdinimcp"], "__path__", None)):
        return sys.modules["houdinimcp"]
    pkg = types.ModuleType("houdinimcp")
    pkg.__path__ = [ROOT]
    sys.modules["houdinimcp"] = pkg
    return pkg


def _ensure_module(name):
    _ensure_pkg()
    full = "houdinimcp." + name
    if full in sys.modules:
        del sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(ROOT, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


class ServerHandlerTests(unittest.TestCase):
    """get_console_log / clear_console_log 的 handler 行为 + 分类归属。"""

    @classmethod
    def setUpClass(cls):
        cls.server_mod = _ensure_module("server")

    def setUp(self):
        # handler 用的 ring 是 server 模块内 clog 的模块级实例
        self.clog = self.server_mod.clog
        self.clog.clear()

    def test_classification_single_bucket(self):
        s = self.server_mod.HoudiniMCPServer
        buckets = (s.READ_ONLY_COMMANDS, s.MUTATING_COMMANDS,
                   s.NO_UNDO_COMMANDS)
        self.assertIn("get_console_log", s.READ_ONLY_COMMANDS)
        self.assertIn("clear_console_log", s.NO_UNDO_COMMANDS)
        for cmd in ("get_console_log", "clear_console_log"):
            count = sum(cmd in b for b in buckets)
            self.assertEqual(count, 1, cmd)

    def test_handlers_registry_contains_both(self):
        server = self.server_mod.HoudiniMCPServer
        handlers = server._get_command_handlers(server)
        self.assertIn("get_console_log", handlers)
        self.assertIn("clear_console_log", handlers)

    def test_get_console_log_time_window(self):
        self.clog._ring._entries.append(
            (time.time() - 10.0, "stdout", "old-a"))
        self.clog._ring._entries.append(
            (time.time() - 0.5, "stdout", "new-b"))
        server = self.server_mod.HoudiniMCPServer
        result = server.get_console_log(server, last_seconds=6)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["filter_mode"], "last_seconds")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["lines"][0]["text"], "new-b")
        self.assertFalse(result["truncated"])

    def test_get_console_log_tail_and_paging(self):
        for i in range(8):
            self.clog._ring._entries.append((time.time(), "stdout", "t%d" % i))
        server = self.server_mod.HoudiniMCPServer
        result = server.get_console_log(server, tail=2)
        self.assertEqual(result["filter_mode"], "tail")
        self.assertEqual([r["text"] for r in result["lines"]],
                         ["t6", "t7"])
        result = server.get_console_log(server, offset=1, limit=2)
        self.assertEqual(result["filter_mode"], "paged")
        self.assertEqual([r["text"] for r in result["lines"]],
                         ["t1", "t2"])

    def test_get_console_log_invalid_args(self):
        server = self.server_mod.HoudiniMCPServer
        result = server.get_console_log(server, offset=-1)
        self.assertEqual(result["status"], "error")
        result = server.get_console_log(server, tail="abc")
        self.assertEqual(result["status"], "error")

    def test_get_console_log_cap_truncates(self):
        # 3 行 × 12000 字符 = 36KB > 16KB cap → 走 list 截断路径
        # （保留部分行 + _truncated 元数据），贴近真实单行几百字节的场景
        payload = (("Z" * 12000) + chr(10)) * 3
        self.clog.append_capture("stdout", payload)
        server = self.server_mod.HoudiniMCPServer
        result = server.get_console_log(server)
        self.assertTrue(result.get("_truncated"))
        self.assertTrue(result["truncated"])
        self.assertIn("lines", result)
        self.assertLess(len(str(result["lines"])), 40000)

    def test_clear_console_log_returns_count(self):
        self.clog.append_capture("stdout", "a" + chr(10) + "b" + chr(10))
        server = self.server_mod.HoudiniMCPServer
        result = server.clear_console_log(server)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["cleared"], 2)
        after = server.get_console_log(server)
        self.assertEqual(after["lines"], [])
        self.assertEqual(after["total"], 0)

    def test_run_code_sync_feeds_ring(self):
        """spec「execute_code 输出同步入缓冲」：_run_code_sync 的
        redirect 捕获路径经 append_capture 同步入 ring。"""
        common = self.server_mod.cmn
        run_result = common._run_code_sync(
            "print('ec-captured')", {"__name__": "__main__"})
        self.assertIn("ec-captured", run_result["stdout"])
        rows, total, _ = self.clog.query()
        self.assertTrue(
            any("ec-captured" in r["text"] for r in rows),
            "execute_code 输出应经 append_capture 进入环形缓冲")


# ---------------------------------------------------------------------------
# Section 3: bridge 注册静态断言（AST，不加载 bridge 进程）
# ---------------------------------------------------------------------------
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


class BridgeRegistrationTests(unittest.TestCase):
    """tools/list 应含两工具且 _houdini_call 转发命令名正确。"""

    def test_bridge_registers_console_tools(self):
        source = io.open(BRIDGE_PATH, encoding="utf-8").read()
        tree = ast.parse(source)
        tools = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call)
                            and getattr(dec.func, "attr", None) == "tool"):
                        tools[node.name] = node
        self.assertIn("get_console_log", tools)
        self.assertIn("clear_console_log", tools)
        seg = ast.get_source_segment(source, tools["get_console_log"])
        self.assertIn(chr(34) + "get_console_log" + chr(34), seg)
        seg = ast.get_source_segment(source, tools["clear_console_log"])
        self.assertIn(chr(34) + "clear_console_log" + chr(34), seg)
