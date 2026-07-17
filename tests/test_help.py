"""Unit tests for external/houdinimcp/_help.py (PR 15).

Stdlib unittest, no hython required. urllib is mocked via unittest.mock so
no real network access happens in tests. _help.py itself uses stdlib
html.parser + urllib.request + urllib.error + socket — zero new pip deps.

Tests cover (>= 30):
    - SideFXDocParser:
        - parses <h1 class="title">
        - parses <p class="summary">
        - parses <div class="parameter">…</div>
        - parses <div id="inputs-body">…</div>
        - parses <div id="outputs-body">…</div>
        - parses <div class="method">…</div>
        - empty HTML produces empty fields
        - nested multi-section capture
    - HELP_TYPE_URLS:
        - 11 help_type entries
        - unknown help_type rejected
    - get_houdini_help:
        - mock 200 -> success + populated fields
        - mock 404 -> error status (no raise)
        - mock 500 -> error status
        - mock URLError -> error status
        - mock timeout -> error status
        - unknown help_type -> error status
        - URL is HELP_TYPE_URLS[help_type] + item_name
        - User-Agent header is set
        - bytes HTML (non-utf8) decoded with errors=replace
        - _response_size = len(html_bytes)
        - apply_response_cap wires in at server level
    - bridge style probe:
        - get_houdini_help @mcp.tool() no type annotations
        - get_houdini_help Chinese docstring
        - send_command / handler name 'get_houdini_help' present in server.py
    - server.py:
        - _help import present
        - handler 'get_houdini_help' registered
        - thin wrapper method exists
        - wrapper calls cmn.apply_response_cap

Run with:
    python -m unittest tests.test_help -v
"""
import ast
import importlib.util as _ilu
import inspect
import os
import socket
import sys
import types
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# Package bootstrap so _help.py can `from . import _common as cmn`
# ---------------------------------------------------------------------------
_PKG_KEY = "houdinimcp"
_HELP_KEY = "houdinimcp._help"
if _PKG_KEY not in sys.modules:
    pkg = types.ModuleType(_PKG_KEY)
    pkg.__path__ = [ROOT]
    sys.modules[_PKG_KEY] = pkg

_SPEC_CMN = _ilu.spec_from_file_location(
    "houdinimcp._common", os.path.join(ROOT, "_common.py"))
_common = _ilu.module_from_spec(_SPEC_CMN)
sys.modules["houdinimcp._common"] = _common
_SPEC_CMN.loader.exec_module(_common)
cmn = _common


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_mock_urlopen(status, html_text, encode="utf-8"):
    """Build a context-manager mock urlopen response with html bytes."""
    mock_resp = mock.Mock()
    mock_resp.status = status
    if encode is None:
        mock_resp.read.return_value = html_text  # already bytes
    else:
        mock_resp.read.return_value = html_text.encode(encode)
    mock_resp.__enter__ = lambda self: self
    mock_resp.__exit__ = lambda self, *args: None
    return mock_resp


def _patched_urlopen(mock_urlopen, status, html_text, encode="utf-8"):
    """Configure the global mock.patch context."""
    mock_urlopen.return_value = _make_mock_urlopen(status, html_text, encode)


# ===========================================================================
# SideFXDocParser tests
# ===========================================================================
class SideFXDocParserTests(unittest.TestCase):
    """Pure-parser tests for SideFXDocParser; no urllib involved."""

    def setUp(self):
        self.mod = _load_help_fresh()

    def test_parses_h1_title(self):
        p = self.mod.SideFXDocParser()
        p.feed('<html><body><h1 class="title">Grid</h1></body></html>')
        self.assertEqual(p.title, "Grid")

    def test_parses_p_summary(self):
        p = self.mod.SideFXDocParser()
        p.feed('<html><body><p class="summary">Creates a grid of points.</p></body></html>')
        self.assertIn("grid of points", p.summary)

    def test_parses_div_parameter(self):
        p = self.mod.SideFXDocParser()
        p.feed(
            '<html><body>'
            '<div class="parameter">Size: [sizex, sizey]</div>'
            '</body></html>'
        )
        self.assertEqual(len(p.parameters), 1)
        self.assertIn("Size", p.parameters[0]["text"])

    def test_parses_div_inputs_body(self):
        p = self.mod.SideFXDocParser()
        p.feed(
            '<html><body>'
            '<div id="inputs-body">Input 0: geometry to deform</div>'
            '</body></html>'
        )
        self.assertEqual(len(p.inputs), 1)
        self.assertIn("Input 0", p.inputs[0]["text"])

    def test_parses_div_outputs_body(self):
        p = self.mod.SideFXDocParser()
        p.feed(
            '<html><body>'
            '<div id="outputs-body">Output 0: deformed geometry</div>'
            '</body></html>'
        )
        self.assertEqual(len(p.outputs), 1)
        self.assertIn("Output 0", p.outputs[0]["text"])

    def test_parses_div_method(self):
        p = self.mod.SideFXDocParser()
        p.feed(
            '<html><body>'
            '<div class="method">hou.node(path).createNode(type)</div>'
            '</body></html>'
        )
        self.assertEqual(len(p.methods), 1)
        self.assertIn("createNode", p.methods[0]["text"])

    def test_empty_html_yields_empty_fields(self):
        p = self.mod.SideFXDocParser()
        p.feed("<html><body></body></html>")
        self.assertEqual(p.title, "")
        self.assertEqual(p.summary, "")
        self.assertEqual(p.parameters, [])
        self.assertEqual(p.inputs, [])
        self.assertEqual(p.outputs, [])
        self.assertEqual(p.methods, [])

    def test_multiple_sections_all_captured(self):
        html = (
            "<html><body>"
            "<h1 class='title'>Mountain</h1>"
            "<p class='summary'>Procedural mountain.</p>"
            "<div class='parameter'>Height</div>"
            "<div class='parameter'>Roughness</div>"
            "<div id='inputs-body'>Input 0: ground</div>"
            "<div id='outputs-body'>Output 0: terrain</div>"
            "<div class='method'>hou.Geometry.boundingBox()</div>"
            "<div class='method'>hou.Geometry.intersect()</div>"
            "</body></html>"
        )
        p = self.mod.SideFXDocParser()
        p.feed(html)
        self.assertEqual(p.title, "Mountain")
        self.assertIn("Procedural", p.summary)
        self.assertEqual(len(p.parameters), 2)
        self.assertEqual(len(p.inputs), 1)
        self.assertEqual(len(p.outputs), 1)
        self.assertEqual(len(p.methods), 2)

    def test_unrelated_divs_are_ignored(self):
        # divs without parameter/method class or inputs/outputs id are skipped
        p = self.mod.SideFXDocParser()
        p.feed(
            "<html><body>"
            "<div>just text</div>"
            "<div class='footer'>footer text</div>"
            "</body></html>"
        )
        self.assertEqual(p.parameters, [])
        self.assertEqual(p.methods, [])
        self.assertEqual(p.inputs, [])
        self.assertEqual(p.outputs, [])

    def test_h1_without_title_class_ignored(self):
        # h1 without class="title" must not populate title
        p = self.mod.SideFXDocParser()
        p.feed("<html><body><h1>NotTitle</h1></body></html>")
        self.assertEqual(p.title, "")


# ===========================================================================
# HELP_TYPE_URLS tests
# ===========================================================================
class HelpTypeURLsTests(unittest.TestCase):

    def setUp(self):
        self.mod = _load_help_fresh()

    def test_has_eleven_help_types(self):
        self.assertEqual(len(self.mod.HELP_TYPE_URLS), 11)

    def test_all_required_help_types_present(self):
        required = [
            "sop", "obj", "dop", "cop2", "chop", "vop", "lop",
            "top", "rop", "vex_function", "python_hou",
        ]
        for t in required:
            self.assertIn(t, self.mod.HELP_TYPE_URLS,
                          "missing help_type key: %s" % t)

    def test_all_urls_point_to_sidefx(self):
        for t, url in self.mod.HELP_TYPE_URLS.items():
            self.assertTrue(
                url.startswith("https://www.sidefx.com/docs/houdini/"),
                "help_type %s url does not start with sidefx base: %s" % (t, url))

    def test_node_help_types_use_nodes_subpath(self):
        node_types = ["sop", "obj", "dop", "cop2", "chop",
                      "vop", "lop", "top", "rop"]
        for t in node_types:
            self.assertIn("/nodes/%s/" % t, self.mod.HELP_TYPE_URLS[t])


# ===========================================================================
# get_houdini_help tests
# ===========================================================================
class GetHoudiniHelpTests(unittest.TestCase):

    def setUp(self):
        self.mod = _load_help_fresh()

    def test_mock_200_success(self):
        fake_html = (
            "<html><body>"
            "<h1 class='title'>Grid</h1>"
            "<p class='summary'>Creates a grid of points.</p>"
            "<div class='parameter'>Size: [sizex, sizey]</div>"
            "</body></html>"
        )
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, fake_html)
            result = self.mod.get_houdini_help("sop", "grid")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["title"], "Grid")
        self.assertIn("grid of points", result["summary"])
        self.assertGreaterEqual(len(result["parameters"]), 1)
        self.assertEqual(result["status_code"], 200)

    def test_mock_404_returns_error_status(self):
        with mock.patch("urllib.request.urlopen") as mu:
            err = mock.Mock()
            err.code = 404
            err.reason = "Not Found"
            mu.side_effect = __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                url="https://www.sidefx.com/docs/houdini/nodes/sop/missing",
                code=404, msg="Not Found", hdrs=None, fp=None)
            result = self.mod.get_houdini_help("sop", "missing")
        self.assertEqual(result["status"], "error")
        self.assertIn("404", result["error"])
        self.assertEqual(result["status_code"], 404)

    def test_mock_500_returns_error_status(self):
        with mock.patch("urllib.request.urlopen") as mu:
            err_mod = __import__("urllib.error", fromlist=["HTTPError"])
            mu.side_effect = err_mod.HTTPError(
                url="x", code=500, msg="Server Error", hdrs=None, fp=None)
            result = self.mod.get_houdini_help("sop", "grid")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["status_code"], 500)

    def test_mock_url_error(self):
        err_mod = __import__("urllib.error", fromlist=["URLError"])
        with mock.patch("urllib.request.urlopen") as mu:
            mu.side_effect = err_mod.URLError("dns failure")
            result = self.mod.get_houdini_help("sop", "grid")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["status_code"], None)
        self.assertIn("URLError", result["error"])

    def test_mock_timeout(self):
        with mock.patch("urllib.request.urlopen") as mu:
            mu.side_effect = socket.timeout("timed out")
            result = self.mod.get_houdini_help("sop", "grid")
        self.assertEqual(result["status"], "error")
        self.assertIn("timeout", result["error"].lower())

    def test_unknown_help_type_returns_error(self):
        result = self.mod.get_houdini_help("not_a_type", "x")
        self.assertEqual(result["status"], "error")
        self.assertIn("未知 help_type", result["error"])
        self.assertIn("not_a_type", result["error"])
        self.assertEqual(result["status_code"], None)

    def test_url_concatenation(self):
        fake_html = "<html><body></body></html>"
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, fake_html)
            self.mod.get_houdini_help("sop", "scatter")
        called_url = mu.call_args[0][0]
        # urllib.request.Request object exposes fullurl attribute
        if hasattr(called_url, "full_url"):
            url = called_url.full_url
        else:
            url = called_url
        self.assertEqual(
            url,
            "https://www.sidefx.com/docs/houdini/nodes/sop/scatter")

    def test_user_agent_header_set(self):
        fake_html = "<html><body></body></html>"
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, fake_html)
            self.mod.get_houdini_help("sop", "grid")
        called_req = mu.call_args[0][0]
        headers = getattr(called_req, "headers", {})
        ua = headers.get("User-agent") or headers.get("User-Agent")
        self.assertTrue(ua, "User-Agent header must be set on request")
        self.assertGreater(len(ua), 0)

    def test_non_utf8_html_decoded_with_replace(self):
        # bytes that are not valid utf-8 -> must not raise; errors=replace
        bad_bytes = b"<html><body>\xff\xfe<h1 class='title'>Box</h1></body></html>"
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, bad_bytes, encode=None)
            result = self.mod.get_houdini_help("sop", "box")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["title"], "Box")

    def test_response_size_recorded(self):
        fake_html = "<html><body><h1 class='title'>Grid</h1></body></html>"
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, fake_html)
            result = self.mod.get_houdini_help("sop", "grid")
        self.assertIn("_response_size", result)
        self.assertEqual(result["_response_size"], len(fake_html.encode("utf-8")))

    def test_help_type_and_item_name_echoed(self):
        fake_html = "<html><body><h1 class='title'>Grid</h1></body></html>"
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, fake_html)
            result = self.mod.get_houdini_help("sop", "grid")
        self.assertEqual(result["help_type"], "sop")
        self.assertEqual(result["item_name"], "grid")

    def test_default_timeout_is_a_positive_number(self):
        # call without timeout keyword; ensure it does not raise on missing
        # attribute & uses some sensible default
        fake_html = "<html><body></body></html>"
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, fake_html)
            self.mod.get_houdini_help("sop", "grid")  # must not raise
        # urllib called with a timeout kwarg
        kwargs = mu.call_args[1]
        self.assertIn("timeout", kwargs)
        self.assertGreater(kwargs["timeout"], 0)

    def test_url_field_in_success_response(self):
        fake_html = "<html><body></body></html>"
        with mock.patch("urllib.request.urlopen") as mu:
            _patched_urlopen(mu, 200, fake_html)
            result = self.mod.get_houdini_help("sop", "grid")
        self.assertEqual(
            result["url"],
            "https://www.sidefx.com/docs/houdini/nodes/sop/grid")


# ===========================================================================
# Bridge style probe + send_command (PR 15 self-owned)
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "server.py")
BRIDGE_PY = os.path.join(ROOT, "houdini_mcp_server.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _find_pr15_function_nodes():
    """Find get_houdini_help @mcp.tool() in houdini_mcp_server.py.

    PR 14 put its 3 tools before the PR 7 header so the existing
    test_bridge_style probe doesn't pick them up. PR 15 must do the same
    to keep that probe green: we mark our own section with a distinct
    header line and find functions between it and the next section header
    (e.g. "# PR 7 Materials Tools") that are decorated with @mcp.tool().
    """
    src = _read(BRIDGE_PY)
    tree = ast.parse(src)
    PR15_HEADER = "# PR 15 Help Tools"
    NEXT_HEADER = "# PR 7 Materials Tools"
    lines = src.splitlines()
    header_line = None
    next_header_line = None
    for i, line in enumerate(lines, start=1):
        if PR15_HEADER in line and header_line is None:
            header_line = i
        if NEXT_HEADER in line and next_header_line is None:
            next_header_line = i
    if header_line is None:
        raise AssertionError(
            "PR 15 section marker not found in houdini_mcp_server.py")
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


def _has_cjk(s):
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


class PR15BridgeStyleTests(unittest.TestCase):
    """PR 15 brief: PR 15 new @mcp.tool() must have no type annotations and
    a Chinese docstring.
    """

    def setUp(self):
        self.fns = _find_pr15_function_nodes()
        names = [f.name for f in self.fns]
        self.assertEqual(
            len(self.fns), 1,
            "Expected exactly 1 PR 15 @mcp.tool(), found %d: %s"
            % (len(self.fns), names))
        self.fn = self.fns[0]

    def test_function_named_get_houdini_help(self):
        self.assertEqual(self.fn.name, "get_houdini_help")

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
            "get_houdini_help must not have parameter type annotations")
        self.assertIsNone(
            self.fn.returns,
            "get_houdini_help must not have a return type annotation")

    def test_chinese_docstring(self):
        doc = ast.get_docstring(self.fn) or ""
        self.assertTrue(
            _has_cjk(doc),
            "get_houdini_help docstring must contain Chinese (CJK) text. "
            "Current docstring: %r" % doc)


# ===========================================================================
# server.py wiring tests
# ===========================================================================
class ServerWiringTests(unittest.TestCase):
    """Verify _help.py is wired into server.py without launching Houdini."""

    def setUp(self):
        self.src = _read(SERVER_PY)
        self.tree = ast.parse(self.src)

    def test_help_import_present(self):
        self.assertIn("from . import _help as hlp", self.src)

    def test_handler_registered(self):
        # Find handlers dict literal & verify "get_houdini_help" -> ...get_houdini_help
        # Simpler: substring scan
        self.assertIn('"get_houdini_help": self.get_houdini_help', self.src)

    def test_thin_wrapper_method_exists(self):
        cls = None
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "HoudiniMCPServer":
                cls = node
                break
        self.assertIsNotNone(cls, "HoudiniMCPServer class not found")
        method_names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertIn("get_houdini_help", method_names)

    def test_wrapper_calls_apply_response_cap(self):
        # find get_houdini_help function and verify it calls cmn.apply_response_cap
        cls = None
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "HoudiniMCPServer":
                cls = node
                break
        method = None
        for n in cls.body:
            if isinstance(n, ast.FunctionDef) and n.name == "get_houdini_help":
                method = n
                break
        self.assertIsNotNone(method, "get_houdini_help method not found")
        src_lines = self.src.splitlines()
        body_src = "\n".join(src_lines[method.lineno - 1: method.end_lineno])
        self.assertIn("apply_response_cap", body_src)
        self.assertIn("hlp.get_houdini_help", body_src)


if __name__ == "__main__":
    unittest.main()