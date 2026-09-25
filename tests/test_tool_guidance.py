# -*- coding: utf-8 -*-
"""feat-mcp-tool-guidance §1/§2 单测：annotations / instructions / 执行引导。

覆盖 spec 场景：
- tools/list 全量带标注（readOnlyHint 恒存在）+ destructive 三件套；
- 映射与三分类一致（AST 提取 bridge 工具→命令，import server 三分类
  双向对账——spec「映射与三分类一致」场景）；
- initialize 响应含契约（四要点关键词 + ≤1200 字符）；
- _ai_hint：失败（hou.* traceback）追加 / 无模式不追加 / 提取去重；
- _hint：第 4 次不附 / 第 5 次起附 / env=0 永不附。
"""

import ast
import asyncio
import importlib.util
import os
import sys
import types
import unittest

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")
TANN_PATH = os.path.join(ROOT, "_tool_annotations.py")
EHINT_PATH = os.path.join(ROOT, "_exec_hint.py")


def _load(path, mod_name):
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


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


def _bridge_tool_to_command():
    """AST 提取 bridge 工具名 → 转发命令名（_houdini_call / send_command）。

    bridge-local 工具（无命令转发）映射为 None。工具主体抽到私有 impl
    函数时（如 execute_houdini_code → _execute_houdini_code_impl）向下
    递归一层扫描。
    """
    with open(BRIDGE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    top_level = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _scan_for_command(func_node, depth=0):
        cmd = None
        callees = []
        for sub in ast.walk(func_node):
            if (isinstance(sub, ast.Call) and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)):
                func = sub.func
                arg0 = None
                if isinstance(func, ast.Name) and func.id == "_houdini_call":
                    arg0 = sub.args[0].value
                elif (isinstance(func, ast.Attribute)
                        and func.attr == "send_command"):
                    arg0 = sub.args[0].value
                if arg0:
                    cmd = arg0
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id.startswith("_")
                    and sub.func.id in top_level):
                callees.append(sub.func.id)
        if cmd is None and depth < 2:
            for callee in callees:
                found = _scan_for_command(top_level[callee], depth + 1)
                if found:
                    return found
        return cmd

    mapping = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = False
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "mcp" and target.attr == "tool"):
                decorated = True
        if not decorated:
            continue
        mapping[node.name] = _scan_for_command(node)
    return mapping


def _load_server_module():
    """以合成 houdinimcp 包加载 server.py（conftest 已 stub hou/numpy）。"""
    if ("houdinimcp" in sys.modules
            and getattr(sys.modules["houdinimcp"], "__path__", None)):
        pkg = sys.modules["houdinimcp"]
    else:
        pkg = types.ModuleType("houdinimcp")
        pkg.__path__ = [ROOT]
        sys.modules["houdinimcp"] = pkg
    full = "houdinimcp.server"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# §1.1 annotations 注册表后处理（spike 实证固化为单测）
# ---------------------------------------------------------------------------

class TestApplyToolAnnotations:
    def test_applies_hints_to_registered_tools(self):
        tann = _load(TANN_PATH, "tann_test_pkg")
        FastMCP, _ = _ensure_real_mcp()
        app = FastMCP("AnnTest")

        @app.tool()
        def find_nodes(pattern: str = "") -> dict:
            """查询节点（read-only 语义示例）。"""
            return {"status": "success"}

        @app.tool()
        def delete_node(path: str = "") -> dict:
            """删除节点（破坏性示例）。"""
            return {"status": "success"}

        @app.tool()
        def set_parameters(path: str = "", parameters: dict = None) -> dict:
            """常规变更示例。"""
            return {"status": "success"}

        applied = tann.apply_tool_annotations(app)
        assert applied == 3
        tools = {t.name: t for t in app._tool_manager.list_tools()}
        ro = tools["find_nodes"].annotations
        assert ro.readOnlyHint is True
        assert ro.destructiveHint is False
        assert ro.idempotentHint is True
        de = tools["delete_node"].annotations
        assert de.destructiveHint is True
        assert de.readOnlyHint is False
        mu = tools["set_parameters"].annotations
        # destructiveHint 规范默认 true——常规变更必须显式 false
        assert mu.destructiveHint is False
        assert mu.readOnlyHint is False

    def test_idempotent_reread_same(self):
        tann = _load(TANN_PATH, "tann_test_pkg")
        FastMCP, _ = _ensure_real_mcp()
        app = FastMCP("AnnTest2")

        @app.tool()
        def ping_houdini() -> dict:
            """只读探测。"""
            return {"status": "success"}

        assert tann.apply_tool_annotations(app) == 1
        assert tann.apply_tool_annotations(app) == 1  # 幂等重设
        tool = app._tool_manager.list_tools()[0]
        assert tool.annotations.readOnlyHint is True

    def test_stale_registry_names_warn(self, caplog):
        """集合登记但未注册的工具名 → warning（防改名漂移）。"""
        tann = _load(TANN_PATH, "tann_test_pkg")
        FastMCP, _ = _ensure_real_mcp()
        app = FastMCP("AnnTest3")

        @app.tool()
        def ping_houdini() -> dict:
            """只读。"""
            return {"status": "success"}

        with caplog.at_level("WARNING", logger="tann_test_pkg"):
            tann.apply_tool_annotations(app)
        assert any("未注册工具名" in message for message in caplog.messages)


# ---------------------------------------------------------------------------
# §1.2 映射与三分类一致（对账完整性测试）
# ---------------------------------------------------------------------------

class TestAnnotationReconciliation:
    def test_read_only_mirror_matches_server_classification(self):
        tann = _load(TANN_PATH, "tann_test_pkg")
        server_mod = _load_server_module()
        ro_cmds = set(server_mod.HoudiniMCPServer.READ_ONLY_COMMANDS)
        tool_cmd = _bridge_tool_to_command()

        derived = set()
        for tool, cmd in tool_cmd.items():
            if cmd is None:
                continue
            resolved = tann.COMMAND_BY_TOOL.get(tool, tool)
            assert resolved == cmd or cmd == "batch", \
                "改名对登记不一致: {0} -> {1}".format(tool, cmd)
            if cmd in ro_cmds:
                derived.add(tool)
        # server 镜像部分 == server RO 全集经映射后的 bridge 名
        assert derived == (tann.READ_ONLY_TOOLS - tann.BRIDGE_LOCAL_TOOLS), \
            "READ_ONLY 镜像与 server 分类漂移"

    def test_rename_map_entries_are_real(self):
        tann = _load(TANN_PATH, "tann_test_pkg")
        tool_cmd = _bridge_tool_to_command()
        for tool, cmd in tann.COMMAND_BY_TOOL.items():
            assert tool_cmd.get(tool) == cmd, \
                "改名对 {0}->{1} 与源码不符".format(tool, cmd)

    def test_destructive_subset_rules(self):
        tann = _load(TANN_PATH, "tann_test_pkg")
        tool_cmd = _bridge_tool_to_command()
        # spec 场景：delete_node / load_scene / new_scene 必为 destructive
        for name in ("delete_node", "load_scene", "new_scene"):
            assert name in tann.DESTRUCTIVE_TOOLS
        # destructive 与 read-only 不相交；均在已注册工具集内
        assert not (tann.DESTRUCTIVE_TOOLS & tann.READ_ONLY_TOOLS)
        for name in tann.DESTRUCTIVE_TOOLS:
            assert name in tool_cmd or name in tann.BRIDGE_LOCAL_TOOLS

    def test_every_forwarded_tool_classified(self):
        """每个转发命令的工具都落进 server 某一分类（batch 除外）。"""
        tann = _load(TANN_PATH, "tann_test_pkg")
        server_mod = _load_server_module()
        server_cls = server_mod.HoudiniMCPServer
        known = (set(server_cls.READ_ONLY_COMMANDS)
                 | set(server_cls.MUTATING_COMMANDS)
                 | set(server_cls.NO_UNDO_COMMANDS))
        tool_cmd = _bridge_tool_to_command()
        # batch 为混合元命令：server 三分类验证显式排除，按常规变更处理
        unresolved = [cmd for cmd in tool_cmd.values()
                      if cmd is not None and cmd != "batch"
                      and cmd not in known]
        assert unresolved == [], "未分类命令: {0}".format(unresolved)


# ---------------------------------------------------------------------------
# §1.3 instructions 契约
# ---------------------------------------------------------------------------

def _extract_instructions_text():
    """从 bridge 源码 AST 提取 FastMCP(...) instructions 字符串（拼接常量）。"""
    with open(BRIDGE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "mcp":
                call = node.value
                if isinstance(call, ast.Call):
                    func = call.func
                    name = func.id if isinstance(func, ast.Name) else (
                        func.attr if isinstance(func, ast.Attribute) else "")
                    if name == "FastMCP":
                        for kw in call.keywords:
                            if kw.arg == "instructions":
                                return ast.literal_eval(kw.value)
    return None


class TestInstructionsContract:
    def test_instructions_text_contract(self):
        text = _extract_instructions_text()
        assert text, "FastMCP 构造缺少 instructions="
        assert len(text) <= 1200, "instructions 超长: {0}".format(len(text))
        for keyword in ("专用工具", "execute_code", "verify_hou_api",
                        "search_lessons"):
            assert keyword in text, "缺少要点关键词: {0}".format(keyword)

    def test_instructions_visible_via_initialize(self):
        FastMCP, make_session = _ensure_real_mcp()
        text = _extract_instructions_text()

        async def check():
            app = FastMCP("HoudiniMCP", instructions=text)
            async with make_session(app._mcp_server) as client:
                return await client.initialize()

        result = asyncio.run(check())
        assert result.instructions == text


# ---------------------------------------------------------------------------
# §2.1 / §2.2 execute_code 失败路径引导
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_exec_hint(monkeypatch):
    monkeypatch.delenv("HOUDINI_MCP_EXEC_HINT_THRESHOLD", raising=False)
    mod = _load(EHINT_PATH, "ehint_test_pkg")
    mod.reset_counter()
    yield mod
    mod.reset_counter()


class TestAiHint:
    def test_failure_with_hou_traceback_appends(self, _reset_exec_hint):
        mod = _reset_exec_hint
        text = (
            "Code executed successfully.\n"
            "--- Stderr ---\n"
            "Traceback (most recent call last):\n"
            '  File "<string>", line 2, in <module>\n'
            'AttributeError: module \'hou\' has no attribute \'nope\'\n'
            "During: hou.nope('/x')"
        )
        out = mod.append_ai_hint(text)
        assert "\n_ai_hint:" in out
        assert "hou.nope" in out
        assert "verify_hou_api" in out

    def test_no_hou_pattern_no_append(self, _reset_exec_hint):
        mod = _reset_exec_hint
        text = ("Code executed successfully.\n--- Stderr ---\n"
                "NameError: name 'foo' is not defined")
        assert "\n_ai_hint:" not in mod.append_ai_hint(text)

    def test_success_stdout_hou_string_no_append(self, _reset_exec_hint):
        """成功 stdout 里打印 hou.* 字符串不触发（零噪音）。"""
        mod = _reset_exec_hint
        text = ("Code executed successfully.\n--- Stdout ---\n"
                "created hou.node /obj/geo1 ok")
        assert "\n_ai_hint:" not in mod.append_ai_hint(text)

    def test_extraction_dedup_preserve_order(self, _reset_exec_hint):
        mod = _reset_exec_hint
        apis = mod.extract_hou_apis(
            "hou.node('/a') hou.parm('/b') hou.node('/c') hou.xyz()")
        assert apis == ["hou.node", "hou.parm", "hou.xyz"]

    def test_error_origin_line_triggers(self, _reset_exec_hint):
        mod = _reset_exec_hint
        out = mod.append_ai_hint(
            "Error (houdini): hou.loadSession failed")
        assert "\n_ai_hint:" in out
        assert "hou.loadSession" in out


class TestCountHint:
    def test_fourth_not_appended_fifth_onwards(self, _reset_exec_hint):
        mod = _reset_exec_hint
        for _ in range(4):
            assert "\n_hint:" not in mod.apply_execute_hints("ok")
        fifth = mod.apply_execute_hints("ok")
        assert "\n_hint:" in fifth
        assert "set_parameters" in fifth
        sixth = mod.apply_execute_hints("ok")
        assert "\n_hint:" in sixth

    def test_threshold_env_zero_disables(self, _reset_exec_hint, monkeypatch):
        mod = _reset_exec_hint
        monkeypatch.setenv("HOUDINI_MCP_EXEC_HINT_THRESHOLD", "0")
        for _ in range(10):
            assert "\n_hint:" not in mod.apply_execute_hints("ok")

    def test_threshold_env_custom(self, _reset_exec_hint, monkeypatch):
        mod = _reset_exec_hint
        monkeypatch.setenv("HOUDINI_MCP_EXEC_HINT_THRESHOLD", "2")
        assert "\n_hint:" not in mod.apply_execute_hints("ok")
        assert "\n_hint:" in mod.apply_execute_hints("ok")

    def test_both_hints_on_failing_fifth_call(self, _reset_exec_hint):
        mod = _reset_exec_hint
        for _ in range(4):
            mod.apply_execute_hints("ok")
        out = mod.apply_execute_hints(
            "Error (houdini): hou.bad() raised")
        assert "\n_ai_hint:" in out
        assert "\n_hint:" in out


class TestBridgeExecHintWiring:
    def test_execute_houdini_code_routes_through_hint(self):
        """注册工具的返回必须经 apply_execute_hints（统一出口）。"""
        with open(BRIDGE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        impl = None
        for node in tree.body:
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "execute_houdini_code"):
                impl = node
                break
        assert impl is not None
        found = any(
            (isinstance(sub, ast.Call)
             and isinstance(sub.func, ast.Attribute)
             and sub.func.attr == "apply_execute_hints")
            for sub in ast.walk(impl))
        assert found, "execute_houdini_code 未走 hint 统一出口"


if __name__ == "__main__":
    unittest.main()
