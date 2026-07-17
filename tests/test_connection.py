"""Unit tests for external/houdinimcp PR 16 — 连接诊断.

Covers three layers without launching Houdini or the MCP bridge:

1. Server-side methods `check_connection()` / `ping_houdini()` in server.py
   are exercised by extracting their source via AST and exec'ing in a
   namespace with a mocked `hou` module (server.py top-level imports hou
   so we cannot import the module directly outside Houdini).

2. Bridge `@mcp.tool()` functions `check_connection` / `ping_houdini` in
   houdini_mcp_server.py are exercised by exec'ing their source with a
   recording `_houdini_call` relay.

3. Style probes (own AST probe for PR 16 — the existing test_bridge_style
   only scans the PR 7 section, so PR 16 must ship its own probe):
   - No parameter or return type annotations
   - Chinese (CJK) docstrings
   - Section header marker "# PR 16 Connection Diagnostic Tools"
   - Exactly 2 PR 16 @mcp.tool() functions

Also asserts that the existing `_handle_ping` (bridge protocol ping) is
left untouched — PR 16 adds Houdini-side ping, not bridge ping.

Tests use stdlib unittest + unittest.mock only — zero new pip deps.
Run with:
    python -m unittest tests.test_connection -v
"""
import ast
import importlib.util as _ilu
import os
import sys
import time
import types
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_PY = os.path.join(ROOT, "server.py")
BRIDGE_PY = os.path.join(ROOT, "houdini_mcp_server.py")


# ---------------------------------------------------------------------------
# Package bootstrap so server.py `from . import _common as cmn` resolves when
# we exec extracted method source. (We never import server.py directly.)
# ---------------------------------------------------------------------------
_PKG_KEY = "houdinimcp"
_CMN_KEY = "houdinimcp._common"
if _PKG_KEY not in sys.modules:
    pkg = types.ModuleType(_PKG_KEY)
    pkg.__path__ = [ROOT]
    sys.modules[_PKG_KEY] = pkg

if _CMN_KEY not in sys.modules:
    _SPEC_CMN = _ilu.spec_from_file_location(
        _CMN_KEY, os.path.join(ROOT, "_common.py"))
    _common = _ilu.module_from_spec(_SPEC_CMN)
    sys.modules[_CMN_KEY] = _common
    _SPEC_CMN.loader.exec_module(_common)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _has_cjk(s):
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


# ---------------------------------------------------------------------------
# Mock hou helpers
# ---------------------------------------------------------------------------
def _make_mock_hou(
    *,
    version="20.5.123",
    application_version_string="Houdini 20.5.123",
    build="123",
    hip_path="/tmp/scene.hip",
    is_untitled=False,
    sub_children=None,
    desktops=None,
):
    """Build a Mock that mimics the subset of hou API PR 16 uses.

    Returns a Mock that responds to .version(), .applicationVersionString(),
    .build(), .hipFile.path(), .hipFile.isUntitled(), .node("/").allSubChildren()
    and .ui.desktops() with sensible defaults.
    """
    h = mock.Mock()
    h.version.return_value = version
    h.applicationVersionString.return_value = application_version_string
    h.build.return_value = build

    h.hipFile.path.return_value = hip_path
    h.hipFile.isUntitled.return_value = is_untitled

    all_children = sub_children if sub_children is not None else []
    h.node.return_value.allSubChildren.return_value = all_children

    desks = desktops if desktops is not None else []
    h.ui.desktops.return_value = desks
    return h


def _find_server_methods(source, *names):
    """Find HoudiniMCPServer methods by name and return their source."""
    tree = ast.parse(source)
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "HoudiniMCPServer")
    methods = {}
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            methods[node.name] = ast.get_source_segment(source, node)
    return methods


# ---------------------------------------------------------------------------
# check_connection tests
# ---------------------------------------------------------------------------
class CheckConnectionServerTests(unittest.TestCase):
    """Server-side check_connection() method behavior (PR 16.1)."""

    def setUp(self):
        self.src = _read(SERVER_PY)
        self.methods = _find_server_methods(self.src, "check_connection")
        self.assertIn(
            "check_connection", self.methods,
            "HoudiniMCPServer.check_connection method not found in server.py")
        self.code = self.methods["check_connection"]

    def _exec_method(self, hou_mock):
        """Exec the method source with a controlled namespace."""
        namespace = {"hou": hou_mock, "os": os}
        exec(compile(self.code, "<server_check_connection>", "exec"),
             namespace)
        return namespace["check_connection"]

    def test_method_returns_full_field_set(self):
        h = _make_mock_hou(
            version="21.0.456",
            application_version_string="Houdini 21.0.456",
            build="456",
            hip_path="/tmp/scene.hip",
            is_untitled=False,
            sub_children=[mock.Mock(), mock.Mock(), mock.Mock()],
            desktops=[mock.Mock(), mock.Mock()],
        )
        fn = self._exec_method(h)
        result = fn(self)  # self arg unused inside
        self.assertEqual(result["hou_version"], "21.0.456")
        self.assertEqual(result["hou_build"], "Houdini 21.0.456")
        self.assertEqual(result["hip_file"], "/tmp/scene.hip")
        self.assertEqual(result["hip_file_basename"], "scene.hip")
        self.assertFalse(result["is_untitled"])
        # node_count = root (1) + 3 sub children = 4
        self.assertEqual(result["node_count"], 4)
        self.assertEqual(result["desktop_count"], 2)
        self.assertEqual(result["_status"], "ok")

    def test_untitled_hip_returns_none_paths(self):
        h = _make_mock_hou(is_untitled=True)
        fn = self._exec_method(h)
        result = fn(self)
        self.assertIsNone(result["hip_file"])
        self.assertIsNone(result["hip_file_basename"])
        self.assertTrue(result["is_untitled"])

    def test_saved_hip_has_basename(self):
        h = _make_mock_hou(
            hip_path="C:/projects/shots/shot_010.hip", is_untitled=False)
        fn = self._exec_method(h)
        result = fn(self)
        self.assertEqual(result["hip_file"], "C:/projects/shots/shot_010.hip")
        self.assertEqual(result["hip_file_basename"], "shot_010.hip")
        self.assertFalse(result["is_untitled"])

    def test_node_count_includes_root(self):
        # node_count must be len(allSubChildren) + 1 (root itself).
        h = _make_mock_hou(sub_children=[mock.Mock() for _ in range(7)])
        fn = self._exec_method(h)
        result = fn(self)
        self.assertEqual(result["node_count"], 8)

    def test_node_count_zero_when_empty_scene(self):
        h = _make_mock_hou(sub_children=[])
        fn = self._exec_method(h)
        result = fn(self)
        self.assertEqual(result["node_count"], 1)  # root alone

    def test_desktop_count_echoes_len(self):
        h = _make_mock_hou(desktops=[mock.Mock() for _ in range(5)])
        fn = self._exec_method(h)
        result = fn(self)
        self.assertEqual(result["desktop_count"], 5)

    def test_status_is_ok(self):
        h = _make_mock_hou()
        fn = self._exec_method(h)
        result = fn(self)
        self.assertEqual(result["_status"], "ok")

    def test_falls_back_to_hou_build_when_no_application_version_string(self):
        # When hou has no applicationVersionString attribute, fall back to
        # str(hou.build()).
        h = mock.Mock()
        h.version.return_value = "20.5.999"
        # build() returns something printable
        h.build.return_value = "999"
        # applicationVersionString does not exist on this hou
        del h.applicationVersionString
        h.hipFile.path.return_value = "/tmp/x.hip"
        h.hipFile.isUntitled.return_value = False
        h.node.return_value.allSubChildren.return_value = []
        h.ui.desktops.return_value = []

        fn = self._exec_method(h)
        result = fn(self)
        self.assertEqual(result["hou_build"], "999")


# ---------------------------------------------------------------------------
# ping_houdini tests
# ---------------------------------------------------------------------------
class PingHoudiniServerTests(unittest.TestCase):
    """Server-side ping_houdini(timeout) method behavior (PR 16.2)."""

    def setUp(self):
        self.src = _read(SERVER_PY)
        self.methods = _find_server_methods(self.src, "ping_houdini")
        self.assertIn(
            "ping_houdini", self.methods,
            "HoudiniMCPServer.ping_houdini method not found in server.py")
        self.code = self.methods["ping_houdini"]

    def _exec_method(self, hou_mock, time_mock=None):
        namespace = {"hou": hou_mock, "time": time_mock or time}
        exec(compile(self.code, "<server_ping_houdini>", "exec"), namespace)
        return namespace["ping_houdini"]

    def test_success_returns_pong_true(self):
        h = mock.Mock()
        h.version.return_value = "20.5.123"
        fn = self._exec_method(h)
        result = fn(self)
        self.assertTrue(result["pong"])
        self.assertEqual(result["hou_version"], "20.5.123")
        self.assertIn("elapsed_ms", result)
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        self.assertTrue(result["within_timeout"])

    def test_elapsed_ms_is_integer(self):
        h = mock.Mock()
        h.version.return_value = "20.5.123"
        fn = self._exec_method(h)
        result = fn(self)
        self.assertIsInstance(result["elapsed_ms"], int)

    def test_exception_returns_pong_false_with_error(self):
        h = mock.Mock()
        h.version.side_effect = RuntimeError("hou not available")
        fn = self._exec_method(h)
        result = fn(self)
        self.assertFalse(result["pong"])
        self.assertIn("error", result)
        self.assertIn("hou not available", result["error"])
        self.assertFalse(result["within_timeout"])
        self.assertIn("elapsed_ms", result)

    def test_elapsed_exceeding_timeout_returns_within_timeout_false(self):
        # Simulate slow hou: time advances by 10 seconds between time.time()
        # calls, so elapsed_ms >> timeout*1000.
        h = mock.Mock()
        h.version.return_value = "20.5.123"
        # First call (start): t0=0; second call (end): t1=10 -> 10000ms
        t_values = iter([0.0, 10.0])

        def fake_time():
            return next(t_values)

        fake_time_mod = mock.Mock()
        fake_time_mod.time = fake_time
        fn = self._exec_method(h, time_mock=fake_time_mod)
        result = fn(self, timeout=1)
        self.assertTrue(result["pong"])
        self.assertFalse(result["within_timeout"])
        self.assertEqual(result["elapsed_ms"], 10000)

    def test_default_timeout_does_not_break_call(self):
        h = mock.Mock()
        h.version.return_value = "20.5.123"
        fn = self._exec_method(h)
        # No timeout kwarg -> default must work
        result = fn(self)
        self.assertTrue(result["pong"])


# ---------------------------------------------------------------------------
# server.py wiring tests
# ---------------------------------------------------------------------------
class ServerHandlerRegistrationTests(unittest.TestCase):
    """Verify both methods are registered in handlers dict (PR 16.3 server side)."""

    def setUp(self):
        self.src = _read(SERVER_PY)
        self.tree = ast.parse(self.src)

    def test_check_connection_registered(self):
        self.assertIn(
            '"check_connection": self.check_connection', self.src,
            "check_connection handler not registered in handlers dict")

    def test_ping_houdini_registered(self):
        self.assertIn(
            '"ping_houdini": self.ping_houdini', self.src,
            "ping_houdini handler not registered in handlers dict")

    def test_check_connection_is_not_mutating(self):
        # check_connection must NOT be in MUTATING_COMMANDS
        cls = next(
            n for n in self.tree.body
            if isinstance(n, ast.ClassDef) and n.name == "HoudiniMCPServer")
        mutating = None
        for node in cls.body:
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "MUTATING_COMMANDS"
                            for t in node.targets)):
                mutating = node.value
                break
        self.assertIsNotNone(mutating, "MUTATING_COMMANDS not found")
        # MUTATING_COMMANDS = frozenset({...}); unwrap the Call to get the
        # inner set literal.
        inner = mutating
        if isinstance(inner, ast.Call) and inner.args:
            inner = inner.args[0]
        names = set()
        for elt in inner.elts:
            if isinstance(elt, ast.Constant):
                names.add(elt.value)
        self.assertNotIn("check_connection", names)
        self.assertNotIn("ping_houdini", names)

    def test_existing_handle_ping_unchanged(self):
        # Brief: 既有 _handle_ping (bridge 协议 ping) 不删
        cls = next(
            n for n in self.tree.body
            if isinstance(n, ast.ClassDef) and n.name == "HoudiniMCPServer")
        names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertIn(
            "_handle_ping", names,
            "Existing _handle_ping (bridge protocol ping) must remain present")
        # ping handler still registered too
        self.assertIn('"ping": self._handle_ping', self.src)


# ---------------------------------------------------------------------------
# Bridge style probes (PR 16 self-owned)
# ---------------------------------------------------------------------------
PR16_SECTION_HEADER = "# PR 16 Connection Diagnostic Tools"
PR15_SECTION_HEADER = "# PR 15 Help Tools"


def _find_pr16_function_nodes():
    """Find PR 16 @mcp.tool() functions in houdini_mcp_server.py.

    PR 16 section is placed BEFORE the PR 15 Help Tools section so the
    existing PR 15 probe (which scans between "# PR 15 Help Tools" and
    "# PR 7 Materials Tools") does not pick it up. We stop scanning at
    the PR 15 header line so the same discipline applies symmetrically.
    """
    src = _read(BRIDGE_PY)
    tree = ast.parse(src)
    lines = src.splitlines()
    header_line = None
    next_header_line = None
    for i, line in enumerate(lines, start=1):
        if PR16_SECTION_HEADER in line and header_line is None:
            header_line = i
        if PR15_SECTION_HEADER in line and next_header_line is None:
            next_header_line = i
    if header_line is None:
        raise AssertionError(
            "PR 16 section marker not found in houdini_mcp_server.py")
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


class PR16BridgeStyleTests(unittest.TestCase):
    """PR 16 brief: 2 new @mcp.tool() must have no type annotations and
    Chinese docstrings. Mirrors the PR 7 test_bridge_style discipline but
    self-owned so the existing PR 7 probe isn't disturbed.
    """

    def setUp(self):
        self.fns = _find_pr16_function_nodes()
        self.names = sorted(f.name for f in self.fns)
        # Expect exactly 2 PR 16 tools
        self.assertEqual(
            self.names, ["check_connection", "ping_houdini"],
            "Expected exactly 2 PR 16 @mcp.tool() functions (check_connection, "
            "ping_houdini), got %s" % self.names)

    def _get(self, name):
        return next(f for f in self.fns if f.name == name)

    def _has_arg_annotations(self, fn):
        args = fn.args
        for a in (args.posonlyargs + args.args + args.kwonlyargs):
            if a.annotation is not None:
                return True
        if args.vararg and args.vararg.annotation is not None:
            return True
        if args.kwarg and args.kwarg.annotation is not None:
            return True
        return False

    def test_check_connection_no_type_annotations(self):
        fn = self._get("check_connection")
        self.assertFalse(
            self._has_arg_annotations(fn),
            "check_connection must not have parameter type annotations")
        self.assertIsNone(
            fn.returns,
            "check_connection must not have a return type annotation")

    def test_ping_houdini_no_type_annotations(self):
        fn = self._get("ping_houdini")
        self.assertFalse(
            self._has_arg_annotations(fn),
            "ping_houdini must not have parameter type annotations")
        self.assertIsNone(
            fn.returns,
            "ping_houdini must not have a return type annotation")

    def test_check_connection_chinese_docstring(self):
        doc = ast.get_docstring(self._get("check_connection")) or ""
        self.assertTrue(
            _has_cjk(doc),
            "check_connection docstring must contain Chinese (CJK) text. "
            "Current: %r" % doc)

    def test_ping_houdini_chinese_docstring(self):
        doc = ast.get_docstring(self._get("ping_houdini")) or ""
        self.assertTrue(
            _has_cjk(doc),
            "ping_houdini docstring must contain Chinese (CJK) text. "
            "Current: %r" % doc)


# ---------------------------------------------------------------------------
# Bridge exec tests
# ---------------------------------------------------------------------------
class _RecordingRelay(object):
    """Record bridge relay arguments and return a configurable envelope."""

    def __init__(self, response=None):
        self.calls = []
        self.response = response if response is not None else {
            "status": "success", "result": {"hou_version": "20.5.123"}}

    def __call__(self, command, params=None):
        self.calls.append((command, params or {}))
        return self.response


class _FakeMCP(object):
    def tool(self):
        return lambda decorated: decorated


def _exec_pr16_bridge_tool(name, response=None):
    """AST-execute a PR 16 bridge tool with a recording relay stub."""
    source = _read(BRIDGE_PY)
    functions = _find_pr16_function_nodes()
    target = next((f for f in functions if f.name == name), None)
    if target is None:
        raise AssertionError("PR 16 bridge tool %s not found" % name)
    function_source = ast.get_source_segment(source, target)
    relay = _RecordingRelay(response=response)
    namespace = {"mcp": _FakeMCP(), "_houdini_call": relay}
    exec(compile(function_source, "<pr16_%s>" % name, "exec"), namespace)
    return namespace[name], relay


class PR16BridgeExecTests(unittest.TestCase):

    def test_check_connection_calls_houdini_with_empty_params(self):
        tool, relay = _exec_pr16_bridge_tool("check_connection")

        result = tool(object())

        self.assertEqual(relay.calls, [("check_connection", {})])
        self.assertEqual(result, relay.response)

    def test_ping_houdini_calls_houdini_with_timeout(self):
        tool, relay = _exec_pr16_bridge_tool("ping_houdini")

        result = tool(object(), timeout=3)

        self.assertEqual(relay.calls, [(
            "ping_houdini",
            {"timeout": 3},
        )])
        self.assertEqual(result, relay.response)

    def test_ping_houdini_default_timeout_is_passed(self):
        tool, relay = _exec_pr16_bridge_tool("ping_houdini")

        tool(object())

        self.assertEqual(len(relay.calls), 1)
        cmd, params = relay.calls[0]
        self.assertEqual(cmd, "ping_houdini")
        # default timeout = 5 (per brief)
        self.assertEqual(params.get("timeout"), 5)

    def test_check_connection_passes_through_error_envelope(self):
        error = {
            "status": "error",
            "message": "Houdini unavailable",
            "origin": "houdini",
        }
        tool, _relay = _exec_pr16_bridge_tool(
            "check_connection", response=error)

        result = tool(object())

        self.assertEqual(result, error)

    def test_ping_houdini_passes_through_error_envelope(self):
        error = {
            "status": "error",
            "message": "timeout",
            "origin": "houdini",
        }
        tool, _relay = _exec_pr16_bridge_tool(
            "ping_houdini", response=error)

        result = tool(object(), timeout=2)

        self.assertEqual(result, error)


if __name__ == "__main__":
    unittest.main()