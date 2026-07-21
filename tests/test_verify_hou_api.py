"""Unit tests for fork PR 18 verify_hou_api bridge tool + _ai_hint synthesis.

Stdlib unittest, no hython required. urllib is mocked via unittest.mock so
no real network access happens in tests. The tests target TWO things:

1. The pure `_synthesize_ai_hint(item_name, help_result)` string synthesis
   logic (pure function, no hou / no network). The function is AST-extracted
   from `server.py` at test time and exec-loaded into an isolated namespace
   so tests run without importing `server` (which requires `hou`). The
   expected behavior is contractually identical to
   `openspec/changes/opera-houdinimcp-unknown-api-guard/specs/mcp-tools/spec.md`.

2. The PR 18 bridge tool wiring (decorator signature, param forwarding,
   default help_type="python_hou"). AST-executes the real `@mcp.tool()` from
   `houdini_mcp_server.py` with a recording relay stub.

Tests cover (>= 8):

    - _synthesize_ai_hint:
        - test_verify_setdisplaynode_returns_not_found_hint
            (success + empty methods + ObjNode prefix -> F-C pattern)
        - test_verify_node_setinput_extracts_3arg_signature
            (success + non-empty methods -> "已找到方法: <sig>" hint)
        - test_verify_returns_error_hint_on_network_failure
            (status=error -> fallback hint with hou.node(item_path).help())
        - regression: existing get_houdini_help kwarg behavior unchanged
          (default param forwarding intact)
        - PR15BridgeExec-style bridge tool checks via AST

    - PR18BridgeStyleTests:
        - real assertions (no skip): PR 18 section header probe finds the
          verify_hou_api @mcp.tool() in houdini_mcp_server.py.

Run with:
    python -m unittest tests.test_verify_hou_api -v
"""
import ast
import importlib.util as _ilu
import os
import socket
import sys
import types
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# Package bootstrap (matches tests/test_help.py pattern).
# ---------------------------------------------------------------------------
_PKG_KEY = "houdinimcp"
_HELP_KEY = "houdinimcp._help"
if _PKG_KEY not in sys.modules:
    pkg = types.ModuleType(_PKG_KEY)
    pkg.__path__ = [ROOT]
    sys.modules[_PKG_KEY] = pkg

try:
    _SPEC_CMN = _ilu.spec_from_file_location(
        "houdinimcp._common", os.path.join(ROOT, "_common.py"))
    _common = _ilu.module_from_spec(_SPEC_CMN)
    sys.modules["houdinimcp._common"] = _common
    _SPEC_CMN.loader.exec_module(_common)
    cmn = _common
except (ImportError, FileNotFoundError):
    cmn = types.SimpleNamespace()


def _load_help_fresh():
    """Reload _help.py fresh; returns the module instance."""
    if _HELP_KEY in sys.modules:
        del sys.modules[_HELP_KEY]
    spec = _ilu.spec_from_file_location(
        _HELP_KEY, os.path.join(ROOT, "_help.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[_HELP_KEY] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_mock_urlopen(status, html_text, encode="utf-8"):
    mock_resp = mock.Mock()
    mock_resp.status = status
    if encode is None:
        mock_resp.read.return_value = html_text
    else:
        mock_resp.read.return_value = html_text.encode(encode)
    mock_resp.__enter__ = lambda self: self
    mock_resp.__exit__ = lambda self, *args: None
    return mock_resp


def _patched_urlopen(mock_urlopen, status, html_text, encode="utf-8"):
    mock_urlopen.return_value = _make_mock_urlopen(status, html_text, encode)


# ===========================================================================
# Real _synthesize_ai_hint from server.py (AST-exec'd, no `hou` dependency).
#
# server.py cannot be imported without `hou`, so we AST-extract the
# _synthesize_ai_hint FunctionDef source and exec it into an isolated
# namespace. This mirrors the same pattern used by
# test_help.py::_exec_pr15_bridge_tool for the bridge tool.
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "server.py")
BRIDGE_PY = os.path.join(ROOT, "houdini_mcp_server.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_synthesize_ai_hint():
    """AST-extract _synthesize_ai_hint from server.py and exec-load it.

    Returns the real function. Raises AssertionError if not found (e.g.
    Wave B hasn't shipped yet) or RuntimeError on syntax/import errors.
    """
    src = _read(SERVER_PY)
    tree = ast.parse(src)
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef)
           and n.name == "_synthesize_ai_hint"]
    assert fns, (
        "_synthesize_ai_hint not found in server.py — Wave B must "
        "implement task 3.3 first.")
    assert len(fns) == 1, (
        "Expected exactly 1 _synthesize_ai_hint in server.py, found %d"
        % len(fns))
    function_source = ast.get_source_segment(src, fns[0])
    assert function_source, (
        "Could not extract _synthesize_ai_hint source segment from server.py")
    namespace = {}
    exec(compile(function_source, "<server._synthesize_ai_hint>", "exec"),
         namespace)
    return namespace["_synthesize_ai_hint"]


_synthesize_ai_hint = _load_synthesize_ai_hint()


# ===========================================================================
# _synthesize_ai_hint tests (4.2 / 4.3 / 4.4)
# ===========================================================================
class SynthesizeAiHintTests(unittest.TestCase):
    """Pure string-synthesis tests for the real server._synthesize_ai_hint."""

    def test_verify_setdisplaynode_returns_not_found_hint(self):
        """4.2: ObjNode.setDisplayNode + empty methods + python_hou ->
        F-C pattern hint mentioning SOP display flag."""
        help_result = {
            "status": "success",
            "help_type": "python_hou",
            "item_name": "ObjNode.setDisplayNode",
            "title": "",
            "summary": "",
            "methods": [],
            "parameters": [],
            "inputs": [],
            "outputs": [],
        }
        hint = _synthesize_ai_hint("ObjNode.setDisplayNode", help_result)
        self.assertTrue(hint, "expected non-empty _ai_hint")
        # Spec-required keywords
        self.assertTrue(
            "OBJ 容器显示" in hint or "不存在" in hint,
            "expected OBJ 容器显示 or 不存在 keyword in hint, got: %r" % hint,
        )
        # F-C pattern hint MUST mention SOP flag fallback
        self.assertIn("setDisplayFlag", hint)
        self.assertIn("setRenderFlag", hint)

    def test_verify_setdisplaynode_non_python_hou_no_fc_hint(self):
        """If help_type is NOT python_hou, the F-C pattern hint MUST NOT
        fire even for ObjNode prefix. AI-friendly contract: only python_hou
        queries are about Python method APIs."""
        help_result = {
            "status": "success",
            "help_type": "sop",
            "item_name": "ObjNode.setDisplayNode",
            "methods": [],
        }
        hint = _synthesize_ai_hint("ObjNode.setDisplayNode", help_result)
        self.assertTrue(hint)
        self.assertNotIn("setDisplayFlag(True)", hint)
        # Still says "API 不存在"
        self.assertIn("API 不存在", hint)

    def test_verify_node_setinput_extracts_3arg_signature(self):
        """4.3: Node.setInput + methods=[{"text": "hou.Node.setInput(input_index, item, output_index=0)"}]
        -> hint contains 'setInput' AND 3-arg keywords (input_index / output_index)."""
        sig = "hou.Node.setInput(input_index, item, output_index=0)"
        help_result = {
            "status": "success",
            "help_type": "python_hou",
            "item_name": "Node.setInput",
            "methods": [{"text": sig}],
        }
        hint = _synthesize_ai_hint("Node.setInput", help_result)
        self.assertTrue(hint)
        self.assertIn("setInput", hint)
        self.assertIn("input_index", hint)
        self.assertIn("output_index", hint)
        # Per spec: '已找到方法: <signature>' format
        self.assertIn("已找到方法:", hint)
        self.assertIn(sig, hint)

    def test_verify_returns_error_hint_on_network_failure(self):
        """4.4: urlopen raises URLError -> status="error" + hint mentions
        fallback OR hou.node(...).help()."""
        err_mod = __import__("urllib.error", fromlist=["URLError"])
        with mock.patch("urllib.request.urlopen") as mu:
            mu.side_effect = err_mod.URLError("dns failure")
            result = _load_help_fresh().get_houdini_help("python_hou", "ObjNode.setDisplayNode")
        # First, verify the lower-level get_houdini_help produced error envelope.
        self.assertEqual(result["status"], "error")
        # Then synthesize the hint.
        hint = _synthesize_ai_hint("ObjNode.setDisplayNode", result)
        self.assertTrue(hint)
        # Required keyword per spec
        self.assertTrue(
            "fallback" in hint or "hou.node(...).help()" in hint or "hou.node(item_path).help()" in hint,
            "expected fallback or hou.node(...).help() keyword in error hint, got: %r" % hint,
        )
        # The error reason SHOULD be reflected
        self.assertIn("dns failure", hint)

    def test_error_hint_for_empty_dict_returns_empty(self):
        """Defensive: empty help_result -> empty hint."""
        self.assertEqual(_synthesize_ai_hint("ObjNode.X", {}), "")
        self.assertEqual(_synthesize_ai_hint("ObjNode.X", None), "")


# ===========================================================================
# End-to-end mock: get_houdini_help + _synthesize_ai_hint
# ===========================================================================
class SynthesizeEndToEndTests(unittest.TestCase):
    """Compose _load_help_fresh + urlopen mock + _synthesize_ai_hint,
    mirroring how the eventual verify_hou_api server.py handler will run."""

    def setUp(self):
        self.mod = _load_help_fresh()

    def test_wrapped_via_get_houdini_help_empty_python_hou_methods(self):
        """End-to-end: verify_hou_api pipeline -> empty methods + python_hou."""
        # Empty HTML -> parser yields empty methods.
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, "<html><body></body></html>")
            result = self.mod.get_houdini_help("python_hou", "ObjNode.setDisplayNode")
        # Now apply the hint
        result["_ai_hint"] = _synthesize_ai_hint("ObjNode.setDisplayNode", result)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["methods"], [])
        self.assertTrue(
            "OBJ 容器显示" in result["_ai_hint"] or "不存在" in result["_ai_hint"]
        )

    def test_wrapped_via_get_houdini_help_populated_methods(self):
        """End-to-end: verify_hou_api pipeline -> populated methods -> signature hint."""
        fake_html = (
            "<html><body>"
            "<h1 class='title'>Node</h1>"
            "<div class='method'>hou.Node.setInput(input_index, item, output_index=0)</div>"
            "</body></html>"
        )
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, fake_html)
            result = self.mod.get_houdini_help("python_hou", "Node.setInput")
        result["_ai_hint"] = _synthesize_ai_hint("Node.setInput", result)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["methods"]), 1)
        self.assertIn("setInput", result["_ai_hint"])
        self.assertIn("input_index", result["_ai_hint"])
        self.assertIn("output_index", result["_ai_hint"])


# ===========================================================================
# Bridge tool tests (4.5 / 4.6 / 4.7)
#
# AST-extract the REAL @mcp.tool() from houdini_mcp_server.py — no stubs.
# ===========================================================================
class _RecordingHoudiniCallLocal(object):
    """Record bridge relay arguments; return a configurable envelope."""

    def __init__(self, response=None):
        self.calls = []
        self.response = response if response is not None else {
            "status": "success", "result": {"ok": True}, "_ai_hint": ""}

    def __call__(self, command, params=None):
        self.calls.append((command, params or {}))
        return self.response


class _FakeMCPLocal(object):
    def tool(self):
        return lambda decorated: decorated


# ---------------------------------------------------------------------------
# 4.5: AST-exec the REAL PR 18 verify_hou_api @mcp.tool() (mirrors
# test_help.py::_exec_pr15_bridge_tool pattern).
# ---------------------------------------------------------------------------
def _find_pr18_function_nodes():
    """Scan houdini_mcp_server.py for the PR 18 verify_hou_api @mcp.tool().
    Returns list of FunctionDef nodes between '# PR 18 Help Wrapper Tools'
    and the next section header."""
    src = _read(BRIDGE_PY)
    tree = ast.parse(src)
    PR18_HEADER = "# PR 18 Help Wrapper Tools"
    NEXT_HEADER = "# PR 7 Materials Tools"  # PR 18 sits before PR 7 too
    lines = src.splitlines()
    header_line = None
    next_header_line = None
    for i, line in enumerate(lines, start=1):
        if PR18_HEADER in line and header_line is None:
            header_line = i
        if NEXT_HEADER in line and next_header_line is None:
            next_header_line = i
    if header_line is None:
        return []
    upper = next_header_line if next_header_line else len(lines) + 1
    fns = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.lineno <= header_line:
            continue
        if node.lineno >= upper:
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                func = dec.func
                if (isinstance(func, ast.Attribute)
                        and func.attr == "tool"):
                    fns.append(node)
                    break
    return fns


def _exec_pr18_bridge_tool(response=None):
    """AST-execute the PR 18 bridge tool with a recording relay stub.

    Mirrors test_help.py::_exec_pr15_bridge_tool.
    Returns (tool, relay) tuple.
    """
    functions = _find_pr18_function_nodes()
    if len(functions) != 1:
        raise AssertionError(
            "Expected exactly 1 PR 18 @mcp.tool(), found %d" % len(functions))
    function_source = ast.get_source_segment(_read(BRIDGE_PY), functions[0])
    relay = _RecordingHoudiniCallLocal(response=response)
    namespace = {"mcp": _FakeMCPLocal(), "_houdini_call": relay}
    exec(compile(function_source, "<pr18_verify_hou_api>", "exec"),
         namespace)
    return namespace["verify_hou_api"], relay


class PR18BridgeExecTests(unittest.TestCase):
    """4.5: verify_hou_api bridge tool forwards (item_name, help_type, timeout)
    with default help_type='python_hou' and default timeout=10."""

    def test_default_help_type_is_python_hou(self):
        tool, relay = _exec_pr18_bridge_tool()
        result = tool(object(), "ObjNode.X")  # no help_type kwarg
        self.assertEqual(relay.calls, [(
            "verify_hou_api",
            {"item_name": "ObjNode.X", "help_type": "python_hou", "timeout": 10},
        )])
        self.assertEqual(result, relay.response)

    def test_explicit_help_type_overrides_default(self):
        tool, relay = _exec_pr18_bridge_tool()
        tool(object(), "grid", help_type="sop", timeout=7)
        self.assertEqual(relay.calls, [(
            "verify_hou_api",
            {"item_name": "grid", "help_type": "sop", "timeout": 7},
        )])

    def test_passes_through_error_envelope(self):
        error = {
            "status": "error",
            "message": "SideFX unavailable",
            "origin": "houdini",
        }
        tool, relay = _exec_pr18_bridge_tool(response=error)
        result = tool(object(), "ObjNode.X")
        self.assertEqual(result, error)


# ---------------------------------------------------------------------------
# 4.6: regression test — get_houdini_help kwarg default MUST NOT be
# accidentally replaced by PR 18's default help_type. We use the real PR 15
# bridge tool style (AST-execute source from houdini_mcp_server.py).
# ---------------------------------------------------------------------------
def _find_pr15_function_nodes():
    """Mirror test_help.py's _find_pr15_function_nodes for the real bridge
    tool. Returns the get_houdini_help @mcp.tool() node in
    houdini_mcp_server.py.

    Stops at either the PR 18 section header or the PR 7 header (whichever
    comes first), so this scanner picks up exactly one PR 15 tool even
    after PR 18 lands between them.
    """
    src = _read(BRIDGE_PY)
    tree = ast.parse(src)
    PR15_HEADER = "# PR 15 Help Tools"
    NEXT_HEADERS = ["# PR 18 Help Wrapper Tools", "# PR 7 Materials Tools"]
    lines = src.splitlines()
    header_line = None
    next_header_line = None
    for i, line in enumerate(lines, start=1):
        if PR15_HEADER in line and header_line is None:
            header_line = i
        for hdr in NEXT_HEADERS:
            if hdr in line and next_header_line is None:
                next_header_line = i
    if header_line is None:
        return []
    upper = next_header_line if next_header_line else len(lines) + 1
    fns = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.lineno <= header_line:
            continue
        if node.lineno >= upper:
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                func = dec.func
                if (isinstance(func, ast.Attribute)
                        and func.attr == "tool"):
                    fns.append(node)
                    break
    return fns


class PR15RegressionTests(unittest.TestCase):
    """4.6: PR 15 get_houdini_help forward MUST preserve help_type kwarg."""

    def test_get_houdini_help_unchanged(self):
        """Call existing PR 15 bridge tool with help_type='sop' -> relay
        captured help_type='sop' (NOT defaulted to 'python_hou')."""
        functions = _find_pr15_function_nodes()
        if not functions:
            self.skipTest("PR 15 bridge tool not found in houdini_mcp_server.py")
        if len(functions) != 1:
            # PR 15 not exactly one entry point — skip rather than fail
            self.skipTest(
                "expected 1 PR 15 bridge tool, found %d" % len(functions))
        function_source = ast.get_source_segment(
            _read(BRIDGE_PY), functions[0])
        relay = _RecordingHoudiniCallLocal()
        namespace = {
            "mcp": _FakeMCPLocal(),
            "_houdini_call": relay,
        }
        exec(compile(function_source, "<pr15_get_houdini_help>", "exec"),
             namespace)
        tool = namespace["get_houdini_help"]
        tool(object(), "sop", "grid")
        # The PR 15 tool MUST have forwarded help_type="sop" (not default).
        self.assertEqual(relay.calls[0][1]["help_type"], "sop")


# ---------------------------------------------------------------------------
# 4.7: PR18BridgeStyleTests — real assertions, no skip.
# ---------------------------------------------------------------------------
def _has_cjk(s):
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


class PR18BridgeStyleTests(unittest.TestCase):
    """4.7: PR 18 @mcp.tool() verify_hou_api style contract:
      - exactly 1 PR 18 @mcp.tool() in the section between PR 18 header
        and PR 7 Materials Tools header
      - function name MUST be `verify_hou_api`
      - no type annotations on params / return
      - Chinese (CJK) docstring
    """

    def setUp(self):
        self.fns = _find_pr18_function_nodes()
        names = [f.name for f in self.fns]
        self.assertEqual(
            len(self.fns), 1,
            "Expected exactly 1 PR 18 @mcp.tool(), found %d: %s"
            % (len(self.fns), names))
        self.fn = self.fns[0]

    def test_function_named_verify_hou_api(self):
        self.assertEqual(self.fn.name, "verify_hou_api")

    def test_no_type_annotations(self):
        args = self.fn.args
        has_arg_annot = any(
            a.annotation is not None for a in
            (args.posonlyargs + args.args + args.kwonlyargs))
        if args.vararg and args.vararg.annotation is not None:
            has_arg_annot = True
        if args.kwarg and args.kwarg.annotation is not None:
            has_arg_annot = True
        self.assertFalse(
            has_arg_annot,
            "verify_hou_api must not have parameter type annotations")
        self.assertIsNone(
            self.fn.returns,
            "verify_hou_api must not have a return type annotation")

    def test_chinese_docstring(self):
        doc = ast.get_docstring(self.fn) or ""
        self.assertTrue(
            _has_cjk(doc),
            "verify_hou_api docstring must contain Chinese (CJK) text. "
            "Current docstring: %r" % doc)


if __name__ == "__main__":
    unittest.main()