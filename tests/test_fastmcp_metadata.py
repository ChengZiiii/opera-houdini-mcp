"""test_fastmcp_metadata.py — FastMCP client-visible instructions 验证
（refactor-opus-optional-and-debt-cleanup task 5.9）。

验证：
- bridge 的 ``FastMCP(...)`` 构造使用 ``instructions=`` 而非 ``description=``
  （AST 源码断言，拒绝 description 回归）。
- 在 mcp 1.12.2 下经真实 MCP initialize 协议断言原 metadata 文本作为
  client-visible instructions 返回（不仅断言 Python 对象构造成功）。
- 反向验证：``FastMCP(description=...)`` 在 1.12.2 下被接收但忽略，
  initialize 响应的 instructions 为 None（证明 description 不对 client 可见）。
"""
import ast
import asyncio
import os
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


def _ensure_real_mcp():
    """清除其他测试注入的 stub mcp 模块（test_headless_launch 等会在
    sys.modules 放无 __file__/__path__ 的 fake mcp），确保 import 到真实
    mcp 1.12.2。返回 (FastMCP, create_connected_server_and_client_session)。

    其他测试会在自身 _load_bridge 中重新安装所需 stub，互不影响。
    """
    for key in list(sys.modules):
        if key == "mcp" or key.startswith("mcp."):
            mod = sys.modules[key]
            if not hasattr(mod, "__file__") and not hasattr(mod, "__path__"):
                del sys.modules[key]
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session
    return FastMCP, create_connected_server_and_client_session


def _extract_fastmcp_call(source):
    """从 bridge 源码解析 ``mcp = FastMCP(...)`` 赋值的 Call 节点。

    返回 (call_node, kwargs_dict)，kwargs_dict 仅含字符串字面量 kwarg。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "mcp":
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        func_name = ""
                        if isinstance(func, ast.Name):
                            func_name = func.id
                        elif isinstance(func, ast.Attribute):
                            func_name = func.attr
                        if func_name == "FastMCP":
                            return node.value
    return None


def _literal_kwargs(call_node):
    """提取 call 节点中值为字符串字面量的关键字参数。"""
    out = {}
    for kw in call_node.keywords:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            out[kw.arg] = kw.value.value
    return out


class BridgeFastMcpConstructionTests(unittest.TestCase):
    """5.9：bridge 源码必须用 instructions=，不得用 description=。"""

    def setUp(self):
        with open(BRIDGE_PATH, "r", encoding="utf-8") as f:
            self.source = f.read()
        self.call = _extract_fastmcp_call(self.source)

    def test_fastmcp_call_found(self):
        self.assertIsNotNone(self.call, "未找到 mcp = FastMCP(...) 赋值")

    def test_no_description_kwarg(self):
        """禁止 description= 回归。"""
        kwarg_names = [kw.arg for kw in self.call.keywords]
        self.assertNotIn(
            "description", kwarg_names,
            "FastMCP 构造不得再传 description=: " + repr(kwarg_names))

    def test_uses_instructions_kwarg(self):
        kwarg_names = [kw.arg for kw in self.call.keywords]
        self.assertIn("instructions", kwarg_names)

    def test_instructions_text_preserved(self):
        """原 metadata 文本应完整保留在 instructions= 中。"""
        lit = _literal_kwargs(self.call)
        self.assertIn("instructions", lit)
        text = lit["instructions"]
        self.assertIn("Houdini", text)
        self.assertIn("OPUS", text)


class McpInitializeProtocolTests(unittest.TestCase):
    """5.9：经 MCP initialize 协议断言 instructions 对 client 可见。"""

    def _run_coro(self, coro):
        return asyncio.run(coro)

    def test_instructions_visible_via_initialize(self):
        """``instructions=`` 在 1.12.2 下经 initialize 成为 client-visible。"""
        FastMCP, make_session = _ensure_real_mcp()

        text = ("A bridging server that connects Claude to Houdini via "
                "MCP stdio + TCP, with OPUS API integration.")

        async def check():
            app = FastMCP("HoudiniMCP", instructions=text)
            async with make_session(app._mcp_server) as client:
                result = await client.initialize()
                return result

        result = self._run_coro(check())
        self.assertEqual(result.serverInfo.name, "HoudiniMCP")
        self.assertEqual(
            result.instructions, text,
            "initialize 响应应暴露精确 instructions 文本")

    def test_description_ignored_not_client_visible(self):
        """``description=`` 在 1.12.2 被接收但忽略，instructions 为 None。

        这是本 change 把 description 改为 instructions 的根因：description 不会
        成为 client-visible server instructions。
        """
        FastMCP, make_session = _ensure_real_mcp()

        text = "should-not-be-visible-as-instructions"

        async def check():
            app = FastMCP("X", description=text)
            async with make_session(app._mcp_server) as client:
                result = await client.initialize()
                return result

        result = self._run_coro(check())
        self.assertIsNone(
            result.instructions,
            "description= 不应成为 client-visible instructions")


class RuntimeMcpVersionTests(unittest.TestCase):
    """5.9 前提：测试环境 mcp distribution 为 1.12.2（FastMCP wire contract
    以 1.12.2 为前提）。"""

    def test_mcp_is_1_12_2(self):
        from importlib.metadata import version
        self.assertEqual(version("mcp"), "1.12.2")


if __name__ == "__main__":
    unittest.main()
