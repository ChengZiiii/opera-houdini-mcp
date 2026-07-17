"""Unit tests for external/houdinimcp/_error_nodes.py + PR 11 bridge tool.

Stdlib unittest, no hython required. hou is mocked via a small stub class.
Tests cover:
    - find_error_nodes default args (include_warnings=True, max_warnings=50)
    - include_warnings=False omits warnings list entirely
    - nodes without errors/warnings do not appear in results
    - error_nodes list contains multiple error messages
    - warning_nodes list contains multiple warning messages
    - max_warnings truncation flag set + warning_nodes capped at max_warnings
    - max_errors=None means unlimited
    - max_errors=N triggers _errors_truncated
    - root_path missing -> ValueError
    - allSubChildren() called exactly once (single sweep, no recursion)
    - bridge @mcp.tool() style AST probe (no type annotations + Chinese
      docstring) + _houdini_call param keys + legacy alias back-compat

Bridge style is verified via AST probe inside this file (we do NOT import
houdini_mcp_server.py because it has heavy runtime deps). test_bridge_style.py
is PR 7 specific; this test does not modify it. PR 11 tool is placed inside
a "# PR 11 Error Nodes" section so the existing PR 7 probe keeps its scope.

Run with:
    python -m unittest tests.test_error_nodes -v
"""
import ast
import os
import sys
import types
import unittest
import importlib.util as _ilu


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Build a synthetic "houdinimcp" package so the production-style
# `from . import _common as cmn` resolves when present.
pkg = types.ModuleType("houdinimcp")
pkg.__path__ = [ROOT]
sys.modules["houdinimcp"] = pkg

_spec_common = _ilu.spec_from_file_location("houdinimcp._common",
                                            os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec_common)
sys.modules["houdinimcp._common"] = common
_spec_common.loader.exec_module(common)
cmn = common

_spec_en = _ilu.spec_from_file_location(
    "houdinimcp._error_nodes", os.path.join(ROOT, "_error_nodes.py"))
error_nodes = _ilu.module_from_spec(_spec_en)
sys.modules["houdinimcp._error_nodes"] = error_nodes
_spec_en.loader.exec_module(error_nodes)
en = error_nodes


# ---------------------------------------------------------------------------
# hou stub: enough surface area for the find_error_nodes implementation.
# ---------------------------------------------------------------------------
class _FakeNodeType(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeNode(object):
    """Minimal graph node stub for find_error_nodes.

    Tracks errors() / warnings() output, allSubChildren() call count,
    and exposes path()/type() like real hou.Node. allSubChildren() returns
    a pre-built flat descendant list (matching HOM semantics) and only
    increments its own counter — descendants are NOT recursed into, so the
    single-sweep production code path triggers exactly one counter
    increment on the root.
    """

    def __init__(self, name, errors=None, warnings=None,
                 sub_children=None, parent=None):
        self._name = name
        self._errors = list(errors) if errors else []
        self._warnings = list(warnings) if warnings else []
        self._parent = parent
        self._path = None
        self._type_name = name
        self.all_sub_children_calls = 0
        # Pre-flatten descendants so allSubChildren() is non-recursive.
        self._flat_descendants = []
        if sub_children:
            for c in sub_children:
                self._flat_descendants.append(c)
                # Inline flatten: c.allSubChildren() would normally recurse,
                # but our production code only calls .allSubChildren() on the
                # root once — we replicate HOM's flat return shape here
                # without invoking c.allSubChildren() (which would increment
                # the child's counter).
                stack = list(c._flat_descendants) if hasattr(
                    c, "_flat_descendants") else []
                self._flat_descendants.extend(stack)

    def name(self):
        return self._name

    def path(self):
        if self._path is not None:
            return self._path
        if self._parent is None:
            return "/" + self._name
        return self._parent.path().rstrip("/") + "/" + self._name

    def type(self):
        return _FakeNodeType(self._type_name)

    def errors(self):
        # Return a fresh list each call to mimic hou.Node.
        return list(self._errors)

    def warnings(self):
        return list(self._warnings)

    def allSubChildren(self):
        self.all_sub_children_calls += 1
        # HOM semantics: flat list of all descendants (not recursive).
        return list(self._flat_descendants)


class _FakeHou(object):
    def __init__(self, root_node=None):
        self._root = root_node

    def node(self, path):
        if self._root is None:
            return None
        if path is None:
            return None
        # Resolve exact-match by stored path or root.
        if path in ("/", ""):
            return self._root
        # Support either "/root" -> self._root or registered paths.
        if path == self._root.path():
            return self._root
        return None


def _make_simple_hou():
    """Build a 4-node tree under /obj with mixed errors/warnings:
        /obj/clean  -> no errors / no warnings
        /obj/broken -> 2 errors, 1 warning
        /obj/noisy  -> 1 error, 3 warnings
        /obj/warny  -> 0 errors, 2 warnings
    """
    clean = _FakeNode("clean")
    broken = _FakeNode("broken",
                       errors=["syntax err", "missing input"],
                       warnings=["deprecated parm"])
    noisy = _FakeNode("noisy",
                      errors=["cook fail"],
                      warnings=["w1", "w2", "w3"])
    warny = _FakeNode("warny", warnings=["a", "b"])
    obj = _FakeNode("obj", sub_children=[clean, broken, noisy, warny])
    obj._path = "/obj"
    clean._path = "/obj/clean"
    broken._path = "/obj/broken"
    noisy._path = "/obj/noisy"
    warny._path = "/obj/warny"
    return _FakeHou(obj), obj, clean, broken, noisy, warny


# ===========================================================================
# Section A: default behavior
# ===========================================================================
class DefaultBehaviorTests(unittest.TestCase):

    def test_returns_expected_top_level_keys(self):
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        result = en.find_error_nodes(hou, "/obj")
        for key in ("error_nodes", "warning_nodes",
                    "_warnings_truncated", "_errors_truncated", "scan_root"):
            self.assertIn(key, result,
                          "missing top-level key: {0}".format(key))
        self.assertEqual(result["scan_root"], "/obj")
        self.assertIsInstance(result["error_nodes"], list)
        self.assertIsInstance(result["warning_nodes"], list)

    def test_default_include_warnings_is_true(self):
        """include_warnings defaults to True -> warning_nodes list populated."""
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        result = en.find_error_nodes(hou, "/obj")
        # warny has 2 warnings, broken has 1, noisy has 3 => 6 total
        self.assertEqual(len(result["warning_nodes"]), 3)
        paths = [w["path"] for w in result["warning_nodes"]]
        self.assertIn("/obj/broken", paths)
        self.assertIn("/obj/noisy", paths)
        self.assertIn("/obj/warny", paths)

    def test_clean_node_does_not_appear(self):
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        result = en.find_error_nodes(hou, "/obj")
        all_paths = [n["path"] for n in result["error_nodes"]]
        all_paths += [n["path"] for n in result["warning_nodes"]]
        self.assertNotIn("/obj/clean", all_paths,
                         "clean node has no errors/warnings; must not appear")

    def test_multiple_errors_collected(self):
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        result = en.find_error_nodes(hou, "/obj")
        broken_entry = next(
            (n for n in result["error_nodes"] if n["path"] == "/obj/broken"),
            None)
        self.assertIsNotNone(broken_entry)
        self.assertEqual(broken_entry["errors"],
                         ["syntax err", "missing input"])
        self.assertEqual(broken_entry["type"], "broken")

    def test_multiple_warnings_collected(self):
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        result = en.find_error_nodes(hou, "/obj")
        noisy_entry = next(
            (n for n in result["warning_nodes"] if n["path"] == "/obj/noisy"),
            None)
        self.assertIsNotNone(noisy_entry)
        self.assertEqual(noisy_entry["warnings"], ["w1", "w2", "w3"])

    def test_node_with_only_warnings_not_in_error_nodes(self):
        """warny has only warnings -> only in warning_nodes, not error_nodes."""
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        result = en.find_error_nodes(hou, "/obj")
        error_paths = [n["path"] for n in result["error_nodes"]]
        warning_paths = [n["path"] for n in result["warning_nodes"]]
        self.assertNotIn("/obj/warny", error_paths)
        self.assertIn("/obj/warny", warning_paths)


# ===========================================================================
# Section B: include_warnings=False
# ===========================================================================
class IncludeWarningsToggleTests(unittest.TestCase):

    def test_include_warnings_false_omits_warnings(self):
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        result = en.find_error_nodes(hou, "/obj", include_warnings=False)
        self.assertEqual(result["warning_nodes"], [])
        self.assertFalse(result["_warnings_truncated"])
        # errors still present
        error_paths = [n["path"] for n in result["error_nodes"]]
        self.assertIn("/obj/broken", error_paths)
        self.assertIn("/obj/noisy", error_paths)


# ===========================================================================
# Section C: max_warnings truncation
# ===========================================================================
class MaxWarningsTruncationTests(unittest.TestCase):

    def _make_many_warners(self, count):
        """Build a tree with `count` child nodes, each with 1 warning."""
        children = []
        for i in range(count):
            n = _FakeNode("w{0}".format(i), warnings=["warn"])
            n._path = "/obj/w{0}".format(i)
            children.append(n)
        obj = _FakeNode("obj", sub_children=children)
        obj._path = "/obj"
        return _FakeHou(obj), obj

    def test_max_warnings_caps_result(self):
        hou, obj = self._make_many_warners(60)
        result = en.find_error_nodes(hou, "/obj",
                                     include_warnings=True,
                                     max_warnings=10)
        self.assertEqual(len(result["warning_nodes"]), 10)
        self.assertTrue(result["_warnings_truncated"])

    def test_max_warnings_under_cap_not_truncated(self):
        hou, obj = self._make_many_warners(3)
        result = en.find_error_nodes(hou, "/obj",
                                     include_warnings=True,
                                     max_warnings=10)
        self.assertEqual(len(result["warning_nodes"]), 3)
        self.assertFalse(result["_warnings_truncated"])

    def test_max_warnings_default_50(self):
        """Default max_warnings=50; 60 nodes -> truncated."""
        hou, obj = self._make_many_warners(60)
        result = en.find_error_nodes(hou, "/obj")
        self.assertEqual(len(result["warning_nodes"]), 50)
        self.assertTrue(result["_warnings_truncated"])


# ===========================================================================
# Section D: max_errors truncation
# ===========================================================================
class MaxErrorsTruncationTests(unittest.TestCase):

    def _make_many_errers(self, count):
        children = []
        for i in range(count):
            n = _FakeNode("e{0}".format(i), errors=["err"])
            n._path = "/obj/e{0}".format(i)
            children.append(n)
        obj = _FakeNode("obj", sub_children=children)
        obj._path = "/obj"
        return _FakeHou(obj), obj

    def test_max_errors_none_is_unlimited(self):
        hou, obj = self._make_many_errers(100)
        result = en.find_error_nodes(hou, "/obj", max_errors=None)
        self.assertEqual(len(result["error_nodes"]), 100)
        self.assertFalse(result["_errors_truncated"])

    def test_max_errors_truncates(self):
        hou, obj = self._make_many_errers(20)
        result = en.find_error_nodes(hou, "/obj", max_errors=5)
        self.assertEqual(len(result["error_nodes"]), 5)
        self.assertTrue(result["_errors_truncated"])

    def test_max_errors_under_cap_not_truncated(self):
        hou, obj = self._make_many_errers(3)
        result = en.find_error_nodes(hou, "/obj", max_errors=10)
        self.assertEqual(len(result["error_nodes"]), 3)
        self.assertFalse(result["_errors_truncated"])


# ===========================================================================
# Section E: missing root
# ===========================================================================
class MissingRootTests(unittest.TestCase):

    def test_missing_root_path_raises_value_error(self):
        hou = _FakeHou(root_node=None)
        with self.assertRaises(ValueError):
            en.find_error_nodes(hou, "/obj/nonexistent")

    def test_existing_root_does_not_raise(self):
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        # Should not raise.
        result = en.find_error_nodes(hou, "/obj")
        self.assertIsInstance(result, dict)


# ===========================================================================
# Section F: allSubChildren() single sweep
# ===========================================================================
class AllSubChildrenSingleSweepTests(unittest.TestCase):

    def test_all_sub_children_called_exactly_once_on_root(self):
        """PR 11 brief: 单次扫描 — root.allSubChildren() must be called once,
        not via recursive .children() traversal."""
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        en.find_error_nodes(hou, "/obj")
        self.assertEqual(
            obj.all_sub_children_calls, 1,
            "root.allSubChildren() must be called exactly once")

    def test_all_sub_children_not_called_on_descendants(self):
        """Implementation should not recurse via .children() — only root
        triggers the single sweep."""
        hou, obj, clean, broken, noisy, warny = _make_simple_hou()
        en.find_error_nodes(hou, "/obj")
        for child in (clean, broken, noisy, warny):
            self.assertEqual(
                child.all_sub_children_calls, 0,
                "{0}.allSubChildren() must not be called (no recursion)"
                .format(child.path()))


# ===========================================================================
# Section G: bridge @mcp.tool() style & behavior — AST probe (PR 11)
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "houdini_mcp_server.py")
PR11_SECTION_HEADER = "# PR 11 Error Nodes"

PR11_BRIDGE_TOOLS = ["find_error_nodes"]


def _parse_server_source():
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        return f.read()


def _find_pr11_tool_nodes():
    """Locate the PR 11 bridge @mcp.tool() function nodes via AST.

    Stops scanning when the next top-level "# PR" section header is hit
    (so we never accidentally pick up PR 7 / PR 8 / PR 9 / PR 10 / future
    sections).
    """
    src = _parse_server_source()
    tree = ast.parse(src)
    lines = src.splitlines()

    header_line = None
    for i, line in enumerate(lines, start=1):
        if PR11_SECTION_HEADER in line:
            header_line = i
            break
    if header_line is None:
        raise AssertionError(
            "PR 11 section marker not found in houdini_mcp_server.py. "
            "Add a comment line containing {0!r} before the new tool."
            .format(PR11_SECTION_HEADER))

    # Identify the next top-level "# PR" section header after PR 11.
    next_header_line = None
    for i, line in enumerate(lines, start=1):
        if i <= header_line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("# PR ") and PR11_SECTION_HEADER not in line:
            next_header_line = i
            break

    found = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.lineno <= header_line:
            continue
        if next_header_line is not None and node.lineno >= next_header_line:
            continue
        if node.name not in PR11_BRIDGE_TOOLS:
            continue
        has_tool_decorator = False
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "tool"):
                has_tool_decorator = True
                break
        if not has_tool_decorator:
            continue
        found[node.name] = node
    missing = [n for n in PR11_BRIDGE_TOOLS if n not in found]
    if missing:
        raise AssertionError(
            "PR 11 bridge tools not found in section: {0}. Found: {1}"
            .format(missing, list(found.keys())))
    return found


def _has_cjk(s):
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


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


def _extract_param_names(fn):
    names = []
    for arg in (fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs):
        names.append(arg.arg)
    return names


def _find_houdini_call_kwargs(fn, cmd_type):
    """Find _houdini_call(cmd_type, {dict}) in fn body; return the Dict node."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "_houdini_call"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == cmd_type):
            continue
        if len(node.args) < 2:
            return None
        second = node.args[1]
        if not isinstance(second, ast.Dict):
            return None
        return second
    return None


def _dict_keys(d):
    keys = []
    for k in d.keys:
        if isinstance(k, ast.Constant):
            keys.append(k.value)
    return keys


class PR11BridgeStyleTests(unittest.TestCase):
    """PR 11 brief: bridge @mcp.tool() functions must have no type annotations
    and Chinese docstrings (PR 7 fix paradigm)."""

    def setUp(self):
        self.tools = _find_pr11_tool_nodes()
        self.assertEqual(
            len(self.tools), 1,
            "Expected 1 PR 11 bridge tool, found {0}: {1}".format(
                len(self.tools), list(self.tools.keys())))

    def test_all_tools_no_type_annotations(self):
        for name, fn in self.tools.items():
            kinds = _signature_annotation_kinds(fn)
            self.assertFalse(
                kinds["arg_annotations"],
                "{0} must not have parameter type annotations".format(name))
            self.assertFalse(
                kinds["return_annotation"],
                "{0} must not have return type annotation".format(name))

    def test_all_tools_chinese_docstring(self):
        for name, fn in self.tools.items():
            doc = ast.get_docstring(fn) or ""
            self.assertTrue(
                _has_cjk(doc),
                "{0} docstring must contain Chinese (CJK). Got: {1!r}"
                .format(name, doc))

    def test_all_tools_have_context_param(self):
        for name, fn in self.tools.items():
            params = _extract_param_names(fn)
            self.assertIn(
                "ctx", params,
                "{0} must accept ctx as first parameter".format(name))

    def test_section_marker_present(self):
        src = _parse_server_source()
        self.assertIn(PR11_SECTION_HEADER, src,
                      "PR 11 section marker missing in houdini_mcp_server.py")


class PR11BridgeBehaviorTests(unittest.TestCase):
    """PR 11 brief: bridge must call _houdini_call with the new param keys,
    preserve legacy 'include_warnings'/'root_path' params, and surface errors."""

    def setUp(self):
        self.tools = _find_pr11_tool_nodes()
        self.src = _parse_server_source()

    def test_find_error_nodes_calls_houdini_with_new_param_keys(self):
        fn = self.tools["find_error_nodes"]
        d = _find_houdini_call_kwargs(fn, "find_error_nodes")
        self.assertIsNotNone(
            d, "find_error_nodes must call _houdini_call('find_error_nodes', ...)")
        keys = _dict_keys(d)
        self.assertIn("root_path", keys,
                      "find_error_nodes params must include 'root_path'")
        self.assertIn("include_warnings", keys,
                      "find_error_nodes params must include 'include_warnings'")
        self.assertIn("max_warnings", keys,
                      "find_error_nodes params must include 'max_warnings'")
        self.assertIn("max_errors", keys,
                      "find_error_nodes params must include 'max_errors'")

    def test_find_error_nodes_signature_has_new_params(self):
        fn = self.tools["find_error_nodes"]
        params = _extract_param_names(fn)
        for name in ("root_path", "include_warnings", "max_warnings",
                     "max_errors"):
            self.assertIn(
                name, params,
                "find_error_nodes bridge must accept {0} kwarg"
                .format(name))

    def test_find_error_nodes_include_warnings_defaults_true(self):
        """Backward-compat: existing callers (legacy `find_error_nodes(ctx,
        root_path)` with include_warnings omitted) must still receive
        warnings because the default is True."""
        fn = self.tools["find_error_nodes"]
        lines = self.src.splitlines()
        body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
        self.assertIn(
            "True", body,
            "include_warnings default must be True")

    def test_find_error_nodes_runtime_call_succeeds(self):
        """Execute the bridge AST node in isolation against a stub server.
        Verifies the param dict reaches the server with all keys, including
        include_warnings=True (back-compat default)."""
        fn = self.tools["find_error_nodes"]
        houdini_requests = []
        server_calls = []

        class _ServerStub(object):
            def find_error_nodes(self, root_path, include_warnings,
                                 max_warnings, max_errors):
                server_calls.append({
                    "root_path": root_path,
                    "include_warnings": include_warnings,
                    "max_warnings": max_warnings,
                    "max_errors": max_errors,
                })
                return {"error_nodes": [], "warning_nodes": []}

        def _houdini_call(command, params):
            houdini_requests.append((command, params))
            server = _ServerStub()
            try:
                return getattr(server, command)(**params)
            except TypeError as exc:
                return {"error": str(exc)}

        namespace = {
            "mcp": type(
                "_McpStub", (), {
                    "tool": lambda self: (lambda decorated: decorated),
                })(),
            "_houdini_call": _houdini_call,
        }
        lines = self.src.splitlines()
        body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
        module = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, SERVER_PY, "exec"), namespace)

        # Legacy call: only ctx + root_path (no include_warnings)
        result = namespace["find_error_nodes"](object(), "/obj")

        self.assertEqual(len(houdini_requests), 1)
        cmd, params = houdini_requests[0]
        self.assertEqual(cmd, "find_error_nodes")
        self.assertEqual(params["root_path"], "/obj")
        # Default include_warnings must reach the server as True
        self.assertTrue(params["include_warnings"])
        # New params present with sensible defaults
        self.assertEqual(params["max_warnings"], 50)
        self.assertIsNone(params["max_errors"])
        # Server saw the call
        self.assertEqual(len(server_calls), 1)
        self.assertEqual(server_calls[0]["root_path"], "/obj")
        self.assertTrue(server_calls[0]["include_warnings"])


if __name__ == "__main__":
    unittest.main()