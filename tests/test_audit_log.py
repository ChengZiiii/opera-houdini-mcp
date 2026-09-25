# -*- coding: utf-8 -*-
"""feat-mcp-console-log-audit §2 单测：bridge 侧 JSONL 审计。

覆盖 audit-log spec 场景：
- 成功 / 失败调用均产生审计行（ok 判定 = status=="success"；非 dict 响应
  按成功记；工具抛异常记 tool_exception 后原样重抛）；
- args ≤256 截断、env 覆盖目录、session_id 进程内稳定；
- 空闲超分段间隔开新段 audit-<yyyymmdd>-<seq>.jsonl（seq 续接当日最大）；
- 保留上限删最旧；prune 失败仅 warning 不抛；
- 旁路语义：落盘 OSError 被吞，不影响工具返回；审计内容不注入响应；
- bridge-local 工具与 get_console_log 自身同样被记录（design D3 /
  Open Question 决策：所有工具含只读查询均记录）；
- 工具面无重放入口（AST 断言 bridge 注册的工具名不含 replay/audit）。
"""

import ast
import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
import time
import unittest
import uuid
from unittest.mock import patch

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AUDIT_LOG_PATH = os.path.join(ROOT, "_audit_log.py")
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")

_TS_MS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}")


def _load():
    sys.modules.pop("audit_log_test_pkg", None)
    spec = importlib.util.spec_from_file_location(
        "audit_log_test_pkg", AUDIT_LOG_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_log_test_pkg"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _fresh_module(tmp_path, monkeypatch):
    """每测试重载模块（新 session_id / 新 writer），审计目录指向临时目录。"""
    monkeypatch.setenv("HOUDINI_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.delenv("HOUDINI_MCP_AUDIT_KEEP", raising=False)
    monkeypatch.delenv("HOUDINI_MCP_AUDIT_SEGMENT_MIN", raising=False)
    return _load()


def _files(directory):
    return sorted(
        name for name in os.listdir(directory)
        if name.startswith("audit-") and name.endswith(".jsonl"))


def _lines(mod, directory=None):
    directory = directory or mod.audit_dir()
    rows = []
    for name in _files(directory):
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# §2.1 行构造 / 字段 / env
# ---------------------------------------------------------------------------

class TestRecordFields:
    def test_success_fields_complete(self, _fresh_module):
        mod = _fresh_module
        mod.record("some_tool", ok=True, duration_ms=7,
                   args_summary='{"a": 1}')
        rows = _lines(mod)
        assert len(rows) == 1
        row = rows[0]
        assert row["tool"] == "some_tool"
        assert row["ok"] is True
        assert isinstance(row["duration_ms"], int)
        assert row["duration_ms"] >= 0
        assert row["args"] == '{"a": 1}'
        # ts：ISO8601 含毫秒
        assert _TS_MS_RE.match(row["ts"]), row["ts"]
        # 失败字段不出现
        assert "error_code" not in row
        assert "error_message" not in row

    def test_failure_fields(self, _fresh_module):
        mod = _fresh_module
        mod.record("bad_tool", ok=False, duration_ms=3,
                   args_summary="{}", error_code="node_not_found",
                   error_message="no such node")
        row = _lines(mod)[-1]
        assert row["ok"] is False
        assert row["error_code"] == "node_not_found"
        assert row["error_message"] == "no such node"

    def test_session_id_valid_and_stable(self, _fresh_module):
        mod = _fresh_module
        mod.record("t1", ok=True, duration_ms=0, args_summary="")
        mod.record("t2", ok=True, duration_ms=0, args_summary="")
        rows = _lines(mod)
        # UUID4 可解析且同进程内稳定
        assert uuid.UUID(rows[0]["session_id"]).version == 4
        assert rows[0]["session_id"] == rows[1]["session_id"]
        # 重载模块（新 bridge 进程的模拟）→ 新 session_id
        mod2 = _load()
        mod2.record("t3", ok=True, duration_ms=0, args_summary="")
        assert _lines(mod2)[-1]["session_id"] != rows[0]["session_id"]

    def test_args_truncation(self, _fresh_module):
        mod = _fresh_module
        summary = mod.summarize_args({"blob": "x" * 5000})
        assert len(summary) <= 256
        mod.record("t", ok=True, duration_ms=0, args_summary=summary)
        assert len(_lines(mod)[-1]["args"]) <= 256

    def test_env_dir_override(self, _fresh_module, tmp_path, monkeypatch):
        mod = _fresh_module
        # fixture 已设 HOUDINI_MCP_AUDIT_DIR → audit_dir() 返回该值
        assert mod.audit_dir() == str(tmp_path / "audit")
        monkeypatch.setenv("HOUDINI_MCP_AUDIT_DIR", str(tmp_path / "other"))
        assert mod.audit_dir() == str(tmp_path / "other")

    def test_default_dir_under_temp(self, _fresh_module, monkeypatch):
        mod = _fresh_module
        monkeypatch.delenv("HOUDINI_MCP_AUDIT_DIR", raising=False)
        expected = os.path.join(
            os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
            "houdini_mcp", "audit")
        assert mod.audit_dir() == expected


# ---------------------------------------------------------------------------
# §2.2 分段与保留
# ---------------------------------------------------------------------------

class TestSegmentAndKeep:
    def test_segment_filename_shape(self, _fresh_module):
        mod = _fresh_module
        mod.record("t", ok=True, duration_ms=0, args_summary="")
        names = _files(mod.audit_dir())
        assert len(names) == 1
        assert re.match(
            r"^audit-\d{8}-\d{3}\.jsonl$", names[0]), names[0]

    def test_idle_rotation_opens_new_seq(self, _fresh_module):
        mod = _fresh_module
        mod.record("first", ok=True, duration_ms=0, args_summary="")
        # 模拟空闲超过分段间隔（默认 15 分钟）
        mod._writer._last_write = time.monotonic() - 99999
        mod.record("second", ok=True, duration_ms=0, args_summary="")
        names = _files(mod.audit_dir())
        assert len(names) == 2
        # seq 续接（001 → 002），两行分别落在两段
        assert names[0].endswith("-001.jsonl")
        assert names[1].endswith("-002.jsonl")
        rows = _lines(mod)
        assert [r["tool"] for r in rows] == ["first", "second"]

    def test_keep_eviction_oldest_removed(self, _fresh_module, monkeypatch):
        mod = _fresh_module
        directory = mod.audit_dir()
        os.makedirs(directory)
        # 预置 3 段（当日更早日份命名即可参与淘汰排序）
        today = time.strftime("%Y%m%d")
        for seq in (1, 2, 3):
            with open(os.path.join(
                    directory,
                    "audit-{0}-{1:03d}.jsonl".format(today, seq)),
                    "w", encoding="utf-8") as handle:
                handle.write("{}\n")
        monkeypatch.setenv("HOUDINI_MCP_AUDIT_KEEP", "3")
        # 空闲超阈值 → 开新段 -004（seq 续接）→ prune 删最旧
        mod._writer._last_write = time.monotonic() - 99999
        mod.record("t", ok=True, duration_ms=0, args_summary="")
        names = _files(directory)
        assert len(names) == 3
        assert "audit-{0}-001.jsonl".format(today) not in names
        assert names[-1] == "audit-{0}-004.jsonl".format(today)
        assert _lines(mod)[-1]["tool"] == "t"

    def test_prune_failure_only_warning(self, _fresh_module, monkeypatch,
                                        caplog):
        mod = _fresh_module
        directory = mod.audit_dir()
        os.makedirs(directory)
        today = time.strftime("%Y%m%d")
        for seq in (1, 2, 3):
            with open(os.path.join(
                    directory,
                    "audit-{0}-{1:03d}.jsonl".format(today, seq)),
                    "w", encoding="utf-8") as handle:
                handle.write("{}\n")
        monkeypatch.setenv("HOUDINI_MCP_AUDIT_KEEP", "3")
        mod._writer._last_write = time.monotonic() - 99999
        # os.remove 抛 OSError：_prune 仅 warning，不抛到调用路径
        with patch("os.remove", side_effect=OSError("denied")):
            with caplog.at_level(logging.WARNING,
                                 logger="audit_log_test_pkg"):
                mod.record("t", ok=True, duration_ms=0, args_summary="")
        assert any("prune failed" in message for message in caplog.messages)

    def test_write_failure_swallowed(self, _fresh_module, caplog):
        mod = _fresh_module
        with patch("builtins.open", side_effect=OSError("disk full")):
            with caplog.at_level(logging.WARNING,
                                 logger="audit_log_test_pkg"):
                mod.record("t", ok=True, duration_ms=0, args_summary="")
        assert any("write failed" in message for message in caplog.messages)
        # 写失败后 writer 自恢复（关闭句柄），下次写入重开新段
        mod.record("t2", ok=True, duration_ms=0, args_summary="")
        assert _lines(mod)[-1]["tool"] == "t2"


# ---------------------------------------------------------------------------
# §2.3/§2.4 注册层织入（真实 FastMCP 1.12.2 in-memory 会话）
# ---------------------------------------------------------------------------

def _ensure_real_mcp():
    """清除其他测试注入的 stub mcp 模块，确保 import 到真实 mcp 1.12.2。"""
    for key in list(sys.modules):
        if key == "mcp" or key.startswith("mcp."):
            mod = sys.modules[key]
            if not hasattr(mod, "__file__") and not hasattr(mod, "__path__"):
                del sys.modules[key]
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session
    return FastMCP, create_connected_server_and_client_session


def _result_payload(result):
    """从 mcp 1.12.2 CallToolResult 解析工具返回 dict（TextContent JSON）。"""
    for item in result.content or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _make_app(FastMCP):
    app = FastMCP("AuditTest")

    @app.tool()
    def ok_tool(x: int = 1) -> dict:
        """成功路径（dict status=success）。"""
        return {"status": "success", "value": x}

    @app.tool()
    def err_tool() -> dict:
        """失败路径（dict status=error + error.code/message）。"""
        return {"status": "error",
                "error": {"code": "boom", "message": "bad input"}}

    @app.tool()
    def str_tool() -> str:
        """execute_code 形态：str 响应按成功记。"""
        return "raw text result"

    @app.tool()
    def raise_tool() -> dict:
        """异常路径：记 tool_exception 后原样重抛。"""
        raise RuntimeError("kaboom")

    @app.tool()
    def get_best_practices() -> dict:
        """bridge-local 形态：不经任何 TCP 出口。"""
        return {"status": "success", "practices": []}

    @app.tool()
    def get_console_log() -> dict:
        """只读查询自身也必须被记录（design Open Question 决策）。"""
        return {"status": "success", "lines": []}

    return app


class TestAuditHook:
    def test_install_idempotent(self, _fresh_module):
        mod = _fresh_module
        FastMCP, _ = _ensure_real_mcp()
        app = _make_app(FastMCP)
        manager = app._tool_manager
        assert mod.install_audit_hook(app) is True
        assert getattr(manager, "_audit_hook_installed", False)
        assert mod.install_audit_hook(app) is False

    def test_success_and_error_recorded(self, _fresh_module):
        mod = _fresh_module
        FastMCP, make_session = _ensure_real_mcp()
        app = _make_app(FastMCP)
        assert mod.install_audit_hook(app)

        async def run():
            async with make_session(app._mcp_server) as client:
                await client.call_tool("ok_tool", {"x": 2})
                await client.call_tool("err_tool", {})

        asyncio.run(run())
        rows = _lines(mod)
        by_tool = {r["tool"]: r for r in rows}
        ok_row = by_tool["ok_tool"]
        assert ok_row["ok"] is True
        assert "error_code" not in ok_row
        assert ok_row["duration_ms"] >= 0
        err_row = by_tool["err_tool"]
        assert err_row["ok"] is False
        assert err_row["error_code"] == "boom"
        assert err_row["error_message"] == "bad input"
        assert ok_row["session_id"] == err_row["session_id"]

    def test_str_result_counts_success(self, _fresh_module):
        mod = _fresh_module
        FastMCP, make_session = _ensure_real_mcp()
        app = _make_app(FastMCP)
        assert mod.install_audit_hook(app)

        async def run():
            async with make_session(app._mcp_server) as client:
                await client.call_tool("str_tool", {})

        asyncio.run(run())
        assert _lines(mod)[-1]["ok"] is True

    def test_exception_recorded_and_reraised(self, _fresh_module):
        mod = _fresh_module
        FastMCP, make_session = _ensure_real_mcp()
        app = _make_app(FastMCP)
        assert mod.install_audit_hook(app)

        async def run():
            async with make_session(app._mcp_server) as client:
                # mcp 1.12.2 的 call_tool 不抛工具错误：isError 结果承载
                return await client.call_tool("raise_tool", {})

        result = asyncio.run(run())
        assert result.isError
        row = _lines(mod)[-1]
        assert row["tool"] == "raise_tool"
        assert row["ok"] is False
        assert row["error_code"] == "tool_exception"
        assert "kaboom" in row["error_message"]

    def test_bridge_local_and_console_log_self_recorded(self, _fresh_module):
        mod = _fresh_module
        FastMCP, make_session = _ensure_real_mcp()
        app = _make_app(FastMCP)
        assert mod.install_audit_hook(app)

        async def run():
            async with make_session(app._mcp_server) as client:
                await client.call_tool("get_best_practices", {})
                await client.call_tool("get_console_log", {})

        asyncio.run(run())
        tools = [r["tool"] for r in _lines(mod)]
        assert "get_best_practices" in tools
        assert "get_console_log" in tools

    def test_write_failure_does_not_affect_tool(self, _fresh_module,
                                                caplog):
        mod = _fresh_module
        FastMCP, make_session = _ensure_real_mcp()
        app = _make_app(FastMCP)
        assert mod.install_audit_hook(app)

        async def run():
            async with make_session(app._mcp_server) as client:
                result = await client.call_tool("ok_tool", {"x": 5})
                return result

        with patch("builtins.open", side_effect=OSError("disk full")):
            with caplog.at_level(logging.WARNING,
                                 logger="audit_log_test_pkg"):
                result = asyncio.run(run())
        # 工具语义不受旁路影响：响应仍正确返回
        assert _result_payload(result) == {"status": "success", "value": 5}

    def test_no_injection_into_response(self, _fresh_module):
        mod = _fresh_module
        FastMCP, make_session = _ensure_real_mcp()
        app = _make_app(FastMCP)
        assert mod.install_audit_hook(app)

        async def run():
            async with make_session(app._mcp_server) as client:
                return await client.call_tool("ok_tool", {"x": 1})

        result = asyncio.run(run())
        # 审计内容不进入响应体（无 audit 相关键；payload 原样）
        assert _result_payload(result) == {"status": "success", "value": 1}
        blob = json.dumps(result.model_dump(mode="json", exclude_none=True))
        for leak_key in ("session_id", "duration_ms", "error_code",
                         "args_summary"):
            assert leak_key not in blob

    def test_chains_after_existing_wrapper(self, _fresh_module):
        """与 lessons capture hook 同层链式：先装的在内层，审计在外层。"""
        mod = _fresh_module
        FastMCP, make_session = _ensure_real_mcp()
        app = _make_app(FastMCP)
        manager = app._tool_manager
        inner_calls = []
        original = manager.call_tool

        async def _inner(name, arguments, context=None, convert_result=False):
            inner_calls.append(name)
            return await original(name, arguments, context=context,
                                  convert_result=convert_result)

        manager.call_tool = _inner  # 模拟 _install_capture_hook 先装
        assert mod.install_audit_hook(app)

        async def run():
            async with make_session(app._mcp_server) as client:
                await client.call_tool("ok_tool", {"x": 3})

        asyncio.run(run())
        assert inner_calls == ["ok_tool"]
        assert _lines(mod)[-1]["tool"] == "ok_tool"


# ---------------------------------------------------------------------------
# §2.3 源码级断言：bridge 接线顺序 + 工具面无重放入口
# ---------------------------------------------------------------------------

class TestBridgeWiring:
    def _tool_names(self):
        with open(BRIDGE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(
                    decorator, ast.Call) else decorator
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "mcp"
                        and target.attr == "tool"):
                    names.append(node.name)
        return names

    def test_bridge_tool_names_have_no_replay_or_audit(self):
        names = self._tool_names()
        assert names, "bridge 源码应解析出 @mcp.tool() 工具"
        bad = [name for name in names
               if re.search(r"(?i)(replay|audit)", name)]
        assert not bad, "不得存在重放/审计读回类工具: {0}".format(bad)

    def test_audit_hook_wired_after_capture_hook(self):
        with open(BRIDGE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        capture_pos = source.find("_install_capture_hook()")
        audit_pos = source.find("_alog.install_audit_hook(mcp)")
        assert capture_pos != -1, "缺少 lessons capture hook 安装"
        assert audit_pos != -1, "缺少审计 hook 安装接线"
        assert capture_pos < audit_pos, \
            "审计 hook 必须在 capture hook 之后安装（外层，时长覆盖全链）"


if __name__ == "__main__":
    unittest.main()
