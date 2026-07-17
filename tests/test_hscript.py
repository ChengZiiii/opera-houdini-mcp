"""Unit tests for external/houdinimcp/_hscript.py + bridge execute_hscript (PR 8).

Stdlib unittest, no hython required. hou is mocked via a small stub class.
Tests cover:
    - execute_hscript: 正常 / (None, None) / 单边返回 / 空字符串 -> ValueError /
      纯空白 -> ValueError / Unicode 通过 / multi-command 通过
    - bridge @mcp.tool() execute_hscript: 无类型注解 / 中文 docstring /
      send_command 参数正确 / stdout/stderr 格式化输出 / 错误状态返回 Error

Bridge style 由独立的 AST probe 验证（不 import houdini_mcp_server.py，因其
有 mcp / requests / dotenv / langchain 等重依赖）。test_bridge_style.py 仍是
PR 7 专用；本测试不修改它，PR 8 工具必须放在 PR 7 section 之前，使既有
PR 7 探针继续返回 3 个 tool。

Run with:
    python -m unittest tests.test_hscript -v
"""
import ast
import os
import re
import sys
import types
import unittest
import importlib.util as _ilu


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Build a synthetic "houdinimcp" package so the production-style
# `from . import _common as cmn` inside _hscript.py resolves.
pkg = types.ModuleType("houdinimcp")
pkg.__path__ = [ROOT]
sys.modules["houdinimcp"] = pkg

_spec_common = _ilu.spec_from_file_location("houdinimcp._common",
                                            os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec_common)
sys.modules["houdinimcp._common"] = common
_spec_common.loader.exec_module(common)
cmn = common

_spec_hscript = _ilu.spec_from_file_location(
    "houdinimcp._hscript", os.path.join(ROOT, "_hscript.py"))
hscript = _ilu.module_from_spec(_spec_hscript)
sys.modules["houdinimcp._hscript"] = hscript
_spec_hscript.loader.exec_module(hscript)
hsc = hscript


# ---------------------------------------------------------------------------
# hou stub for execute_hscript tests
# ---------------------------------------------------------------------------
class _FakeHou(object):
    """Records the HScript code passed to hou.hscript(code)."""

    def __init__(self, return_value=("", "")):
        self._return_value = return_value
        self.calls = []

    def hscript(self, code):
        self.calls.append(code)
        return self._return_value


def _make_hou(return_value=("", "")):
    return _FakeHou(return_value=return_value)


# ===========================================================================
# Section A: execute_hscript — 正常路径
# ===========================================================================
class ExecuteHscriptTests(unittest.TestCase):

    def test_normal_code_returns_stdout_stderr_zero(self):
        """正常 HScript 命令 -> 返回 {stdout, stderr, return_code:0}."""
        hou = _make_hou(return_value=("obj geo1\n", ""))
        result = hsc.execute_hscript(hou, "ls /obj")
        self.assertEqual(result["stdout"], "obj geo1\n")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["return_code"], 0)
        # hou.hscript 实际被调用了一次，且参数是原样传入的 code
        self.assertEqual(hou.calls, ["ls /obj"])

    def test_multi_command_with_semicolon(self):
        """HScript 常用 ';' 分隔多命令；必须整串透传 hou.hscript."""
        hou = _make_hou(return_value=("/obj\n", ""))
        code = "cd /obj; ls"
        result = hsc.execute_hscript(hou, code)
        self.assertEqual(hou.calls, [code])
        self.assertEqual(result["stdout"], "/obj\n")
        self.assertEqual(result["return_code"], 0)

    def test_returns_dict_with_three_keys(self):
        """返回值必须包含 stdout / stderr / return_code 三个键。"""
        hou = _make_hou(return_value=("x", "y"))
        result = hsc.execute_hscript(hou, "echo x")
        self.assertEqual(set(result.keys()),
                         {"stdout", "stderr", "return_code"})

    def test_return_code_is_always_zero(self):
        """HScript 不返回 return code，按 brief 固定返 0（无论 hou.hscript 返回什么）."""
        hou = _make_hou(return_value=("out", "err"))
        result = hsc.execute_hscript(hou, "anything")
        self.assertEqual(result["return_code"], 0)

    # ---- 容错：hou.hscript 返 (None, None) 或 None 的边界 ----
    def test_none_none_returns_empty_strings(self):
        """hou.hscript 返 (None, None) -> stdout/stderr 都规范化为 ''."""
        hou = _make_hou(return_value=(None, None))
        result = hsc.execute_hscript(hou, "ls")
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["return_code"], 0)

    def test_none_only_stdout_returns_empty(self):
        """hou.hscript 返 (None, "err") -> stdout=''. stderr=err."""
        hou = _make_hou(return_value=(None, "warning text"))
        result = hsc.execute_hscript(hou, "ls")
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["stderr"], "warning text")

    def test_none_only_stderr_returns_empty(self):
        """hou.hscript 返 ("out", None) -> stdout=out. stderr=''."""
        hou = _make_hou(return_value=("ok", None))
        result = hsc.execute_hscript(hou, "ls")
        self.assertEqual(result["stdout"], "ok")
        self.assertEqual(result["stderr"], "")

    def test_falsy_strings_treated_as_empty(self):
        """hou.hscript 返 ('', '') -> stdout/stderr 都为 ''（不是 None）."""
        hou = _make_hou(return_value=("", ""))
        result = hsc.execute_hscript(hou, "ls")
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["stderr"], "")

    # ---- 空 / 空白 code -> ValueError ----
    def test_empty_string_raises_value_error(self):
        """空字符串 code 必须抛 ValueError，不能静默返空 stdout."""
        hou = _make_hou()
        with self.assertRaises(ValueError):
            hsc.execute_hscript(hou, "")
        # 抛错前不应调用 hou.hscript
        self.assertEqual(hou.calls, [])

    def test_none_code_raises_value_error(self):
        """None code 也算空，必须抛 ValueError。"""
        hou = _make_hou()
        with self.assertRaises(ValueError):
            hsc.execute_hscript(hou, None)
        self.assertEqual(hou.calls, [])

    def test_whitespace_only_raises_value_error(self):
        """纯空白 code（空格 / tab / 换行）必须抛 ValueError。"""
        hou = _make_hou()
        for blank in ("   ", "\t", "\n", " \t \n ", "\r\n"):
            with self.assertRaises(ValueError,
                                   msg="blank code {0!r} must raise".format(blank)):
                hsc.execute_hscript(hou, blank)
        self.assertEqual(hou.calls, [])

    def test_error_message_mentions_empty(self):
        """ValueError 信息应提示 code 为空，便于上层 UI 给出明确反馈。"""
        hou = _make_hou()
        with self.assertRaises(ValueError) as ctx:
            hsc.execute_hscript(hou, "   ")
        msg = str(ctx.exception)
        # 中文 / 英文均可，但 "空" / "empty" 至少出现其一
        self.assertTrue("空" in msg or "empty" in msg.lower(),
                        "Error message should mention empty: {0!r}".format(msg))

    # ---- Unicode 容错 ----
    def test_unicode_code_passes_through(self):
        """中文 / emoji code 必须能透传到 hou.hscript，不抛 UnicodeError."""
        hou = _make_hou(return_value=("OK\n", ""))
        result = hsc.execute_hscript(hou, "opset -n 中文节点")
        self.assertEqual(hou.calls, ["opset -n 中文节点"])
        self.assertEqual(result["stdout"], "OK\n")
        self.assertEqual(result["return_code"], 0)

    def test_emoji_code_passes_through(self):
        """emoji code 也不应触发 UnicodeError."""
        hou = _make_hou(return_value=("", ""))
        result = hsc.execute_hscript(hou, "echo \U0001f600")
        self.assertEqual(hou.calls, ["echo \U0001f600"])
        self.assertEqual(result["return_code"], 0)

    # ---- hou 不在顶层 import ----
    def test_does_not_top_level_import_hou(self):
        """_hscript.py 必须不顶层 import hou（参数注入约定）. AST 扫描源码确认。"""
        src_path = os.path.join(ROOT, "_hscript.py")
        with open(src_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "hou" or alias.name.startswith("hou."):
                        bad_imports.append("import {0}".format(alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.module == "hou" or (node.module
                                            and node.module.startswith("hou.")):
                    bad_imports.append("from {0} import ...".format(node.module))
        self.assertEqual(bad_imports, [],
                         "_hscript.py must not top-level import hou: {0}"
                         .format(bad_imports))


# ===========================================================================
# Section B: bridge execute_hscript tool — AST probe
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "houdini_mcp_server.py")
PR8_SECTION_HEADER = "# PR 8 HScript Tools"


def _parse_server_source():
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        return f.read()


def _find_pr8_execute_hscript_node():
    """Locate the bridge execute_hscript @mcp.tool() function node via AST.

    Locate by PR 8 section header comment. Require a strictly-after-the-header
    FunctionDef with a preceding `@mcp.tool()` decorator. The function must be
    named `execute_hscript`.
    """
    src = _parse_server_source()
    tree = ast.parse(src)
    lines = src.splitlines()

    header_line = None
    for i, line in enumerate(lines, start=1):
        if PR8_SECTION_HEADER in line:
            header_line = i
            break
    if header_line is None:
        raise AssertionError(
            "PR 8 section marker not found in houdini_mcp_server.py. "
            "Add a comment line containing {0!r} before the new tool."
            .format(PR8_SECTION_HEADER))

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.lineno <= header_line:
            continue
        if node.name != "execute_hscript":
            continue
        # require @mcp.tool() decorator
        has_tool_decorator = False
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "tool"):
                has_tool_decorator = True
                break
        if not has_tool_decorator:
            continue
        return node
    raise AssertionError(
        "No @mcp.tool() execute_hscript found after PR 8 section header.")


def _signature_annotation_kinds(fn):
    args = fn.args
    has_arg = False
    for arg in (args.posonlyargs + args.args + args.kwonlyargs):
        if arg.annotation is not None:
            has_arg = True
            break
    if args.vararg and args.vararg.annotation is not None:
        has_arg = True
    if args.kwarg and args.kwarg.annotation is not None:
        has_arg = True
    return {
        "arg_annotations": has_arg,
        "return_annotation": fn.returns is not None,
    }


def _has_cjk(s):
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _find_send_command_kwargs(fn):
    """Walk the function body looking for conn.send_command("execute_hscript", {...}).

    Return the parsed kwargs dict node, or None if not found.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # conn.send_command(...) or self.sock.sendall(...) — match .send_command
        if not (isinstance(func, ast.Attribute) and func.attr == "send_command"):
            continue
        if not node.args:
            continue
        # first positional must be a Constant string "execute_hscript"
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "execute_hscript"):
            continue
        if len(node.args) < 2:
            return None
        second = node.args[1]
        if not isinstance(second, ast.Dict):
            return None
        return second
    return None


class BridgeHscriptStyleTests(unittest.TestCase):
    """PR 8 brief: bridge @mcp.tool() execute_hscript 必须遵守 PR 7 fix 范式."""

    def setUp(self):
        self.fn = _find_pr8_execute_hscript_node()
        self.kinds = _signature_annotation_kinds(self.fn)
        self.doc = ast.get_docstring(self.fn) or ""

    def test_no_type_annotations(self):
        self.assertFalse(
            self.kinds["arg_annotations"],
            "execute_hscript (bridge) must not have parameter annotations. "
            "Found annotations: {0}".format(self.kinds))
        self.assertFalse(
            self.kinds["return_annotation"],
            "execute_hscript (bridge) must not have return annotation.")

    def test_chinese_docstring(self):
        self.assertTrue(
            _has_cjk(self.doc),
            "execute_hscript (bridge) docstring must contain Chinese (CJK). "
            "Current docstring: {0!r}".format(self.doc))

    def test_function_name(self):
        self.assertEqual(self.fn.name, "execute_hscript")

    def test_has_context_parameter(self):
        """bridge tool 必须接收 ctx（与 PR 7 既有 tool 风格一致）。"""
        param_names = [a.arg for a in self.fn.args.args]
        self.assertIn("ctx", param_names)

    def test_has_code_parameter(self):
        """bridge tool 必须接收 code 参数。"""
        param_names = [a.arg for a in self.fn.args.args]
        self.assertIn("code", param_names)


class BridgeHscriptBehaviorTests(unittest.TestCase):
    """PR 8 brief: bridge execute_hscript 必须正确转发到 send_command 并格式化输出。"""

    def setUp(self):
        self.fn = _find_pr8_execute_hscript_node()
        # Extract the function body as source text for substring probes.
        src = _parse_server_source()
        lines = src.splitlines()
        self.body_text = "\n".join(lines[self.fn.lineno - 1:self.fn.end_lineno])

    def test_calls_send_command_with_execute_hscript_type(self):
        """bridge tool 必须用 'execute_hscript' 作为 cmd_type 调用 conn.send_command."""
        kw = _find_send_command_kwargs(self.fn)
        self.assertIsNotNone(
            kw,
            "execute_hscript must call conn.send_command(\"execute_hscript\", "
            "{...}) somewhere in its body.")

    def test_send_command_payload_contains_code(self):
        """send_command 的 params dict 必须包含 'code' key."""
        kw = _find_send_command_kwargs(self.fn)
        self.assertIsNotNone(kw, "send_command kwargs not found")
        # The Dict node's keys may be Constant (str) nodes.
        keys = []
        for k in kw.keys:
            if isinstance(k, ast.Constant):
                keys.append(k.value)
            elif isinstance(k, ast.Str):  # py<3.8 fallback
                keys.append(k.s)
        self.assertIn("code", keys,
                      "send_command params must include 'code' key. "
                      "Got keys: {0}".format(keys))

    def test_status_error_branch_present(self):
        """bridge tool 必须检查 response.status == 'error' 并返回 Error 字符串。"""
        # Look for a string literal "error" near response access patterns.
        # Use AST to look for Compare nodes with response.get("status") == "error".
        found = False
        for node in ast.walk(self.fn):
            if not isinstance(node, ast.Compare):
                continue
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, ast.Eq):
                    continue
                # left should look like response.get("status") or response["status"]
                left = node.left
                ok_left = False
                if (isinstance(left, ast.Call)
                        and isinstance(left.func, ast.Attribute)
                        and left.func.attr == "get"
                        and left.args
                        and isinstance(left.args[0], ast.Constant)
                        and left.args[0].value == "status"):
                    ok_left = True
                if (isinstance(left, ast.Subscript)
                        and isinstance(left.slice, ast.Constant)
                        and left.slice.value == "status"):
                    ok_left = True
                if not ok_left:
                    continue
                if (isinstance(comparator, ast.Constant)
                        and comparator.value == "error"):
                    found = True
                    break
            if found:
                break
        self.assertTrue(found,
                        "execute_hscript must check response.get('status') == "
                        "'error' (or response['status'] == 'error') and "
                        "return an Error string.")

    def test_includes_stdout_and_stderr_in_output(self):
        """bridge tool 输出文本必须提到 stdout 和 stderr（格式化输出证据）。"""
        # 简单 substring 检查：body_text 中应出现 'stdout' 和 'stderr'。
        self.assertIn("stdout", self.body_text,
                      "execute_hscript bridge body must format stdout in its "
                      "output text.")
        self.assertIn("stderr", self.body_text,
                      "execute_hscript bridge body must format stderr in its "
                      "output text.")


if __name__ == "__main__":
    unittest.main()