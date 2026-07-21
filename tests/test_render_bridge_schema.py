"""回归测试 Bug A：render_* bridge 工具返回 schema 应为 dict 而非 str。

现象（2026-07-21 现场复现）：
    调用 render_quad_views / render_single_view / render_specific_camera
    时，Houdini 端 server.py 返 {"status": "success", "result": {...}}
    其中 result 本身是 dict（含 process_rendered_image / output_image 等键）。
    但 bridge 端三个 @mcp.tool() 函数的返回类型注解是 -> str，Pydantic
    校验抛：1 validation error for render_quad_viewsOutput / result /
    Input should be a valid string [type=string_type, input_type=dict]。

修复目标（按 user spec）：
    - render_single_view / render_quad_views / render_specific_camera
      三个 bridge tool 的返回注解从 -> str 改为 -> dict
    - 保留顶层 dict 键不变（image_path / renderer / size_bytes 等结构化字段）
    - 不改 wrapper 名字

本测试仅做静态 AST 探针（避免 import mcp 副作用），验证三函数的
returns 注解是 ast.Name(id="dict")。可执行验证不需要 Houdini 环境。
"""
import ast
import os
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE_PY = os.path.join(ROOT, "houdini_mcp_server.py")

# Bug A 涉及三个 bridge tool
TARGET_FUNCS = ("render_single_view", "render_quad_views", "render_specific_camera")


def _load_bridge_tree():
    """Parse houdini_mcp_server.py and return the AST tree."""
    with open(BRIDGE_PY, "r", encoding="utf-8") as f:
        return ast.parse(f.read(), filename=BRIDGE_PY)


def _find_bridge_render_funcs(tree):
    """Return {name: ast.FunctionDef} for the 3 bridge-side render_* tools.

    Bridge 版本必须有 @mcp.tool() 装饰器 + 默认 render_path 参数 +
    -> str/dict 注解；server.py 端同名函数不算（它们返回 status dict
    是 server.py 内部约定，bridge 端才是 MCP 暴露面）。
    """
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in TARGET_FUNCS:
            continue
        # 必须是 mcp.tool() 装饰器
        has_tool = False
        for d in node.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
                if d.func.attr == "tool":
                    has_tool = True
                    break
        if not has_tool:
            continue
        # 必须是 ctx: Context 形参（MCP bridge tool 标志）
        if not node.args.args or node.args.args[0].arg != "ctx":
            continue
        out[node.name] = node
    return out


class RenderBridgeSchemaTest(unittest.TestCase):
    """Bug A 回归：3 个 render_* bridge tool 的返回注解必须是 dict。"""

    def setUp(self):
        self.tree = _load_bridge_tree()
        self.funcs = _find_bridge_render_funcs(self.tree)

    def test_all_three_render_funcs_found(self):
        """AST 探针必须能找到三个 bridge tool。"""
        for name in TARGET_FUNCS:
            self.assertIn(name, self.funcs,
                f"未在 {BRIDGE_PY} 找到 @mcp.tool() + ctx 形参的 {name}")

    def _assert_return_is_dict(self, name):
        node = self.funcs[name]
        self.assertIsNotNone(node.returns,
            f"{name} 缺少返回类型注解")
        ann_src = ast.unparse(node.returns)
        # 接受 "dict" 或 "Dict[str, Any]" 两种写法
        is_dict = (
            (isinstance(node.returns, ast.Name) and node.returns.id == "dict")
            or (isinstance(node.returns, ast.Subscript)
                and isinstance(node.returns.value, ast.Name)
                and node.returns.value.id in ("dict", "Dict"))
        )
        self.assertTrue(is_dict,
            f"{name} 返回注解必须是 dict，实际为 '{ann_src}'")

    def test_render_single_view_returns_dict(self):
        self._assert_return_is_dict("render_single_view")

    def test_render_quad_views_returns_dict(self):
        self._assert_return_is_dict("render_quad_views")

    def test_render_specific_camera_returns_dict(self):
        self._assert_return_is_dict("render_specific_camera")

    def test_render_funcs_no_longer_annotate_str(self):
        """反向断言：没有任何一个 bridge render tool 还用 -> str。"""
        for name, node in self.funcs.items():
            if (isinstance(node.returns, ast.Name)
                    and node.returns.id == "str"):
                self.fail(f"{name} 仍为 -> str 注解，违反 Bug A 修复目标")


class RenderBridgeBackwardCompatTest(unittest.TestCase):
    """保证函数名 + 形参签名不变（user spec 不变量：不要改 wrapper 名）。"""

    def setUp(self):
        self.tree = _load_bridge_tree()
        self.funcs = _find_bridge_render_funcs(self.tree)

    def test_func_names_unchanged(self):
        for name in TARGET_FUNCS:
            self.assertIn(name, self.funcs,
                f"bridge tool 名 {name} 必须保留")

    def test_render_quad_views_param_signature(self):
        """render_quad_views 必须保留 render_path / render_engine / karma_engine。"""
        node = self.funcs["render_quad_views"]
        arg_names = {a.arg for a in node.args.args}
        for required in ("ctx", "render_path", "render_engine", "karma_engine"):
            self.assertIn(required, arg_names,
                f"render_quad_views 缺少参数 {required}")


if __name__ == "__main__":
    unittest.main()
