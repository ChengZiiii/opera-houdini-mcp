"""Unit tests for external/houdinimcp/_node_info.py + PR 10 bridge tool.

Stdlib unittest, no hython required. hou is mocked via a small stub class.
Tests cover:
    - _node_info.get_node_info: default params / compact / include_errors /
      force_cook / include_input_details / cook_state H20.5+ & fallback /
      inputConnectors single-call / missing node -> ValueError
    - _node_info._cook_state: H20.5+ returns enum tail / H<20.5 maps
      needsToCook() to Dirty or Cooked
    - bridge @mcp.tool() style probe (no type annotations + Chinese docstring)
    - bridge behavior: _houdini_call param keys, error passthrough

Run with:
    python -m unittest tests.test_node_info -v
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

# Build a synthetic "houdinimcp" package so production-style
# `from . import _common as cmn` inside _node_info.py resolves.
pkg = types.ModuleType("houdinimcp")
pkg.__path__ = [ROOT]
sys.modules["houdinimcp"] = pkg

_spec_common = _ilu.spec_from_file_location("houdinimcp._common",
                                            os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec_common)
sys.modules["houdinimcp._common"] = common
_spec_common.loader.exec_module(common)
cmn = common

_spec_nodeinfo = _ilu.spec_from_file_location(
    "houdinimcp._node_info", os.path.join(ROOT, "_node_info.py"))
node_info_mod = _ilu.module_from_spec(_spec_nodeinfo)
sys.modules["houdinimcp._node_info"] = node_info_mod
_spec_nodeinfo.loader.exec_module(node_info_mod)
ni = node_info_mod


# ---------------------------------------------------------------------------
# hou stub: minimal surface for get_node_info / _cook_state.
# ---------------------------------------------------------------------------
class _FakeCookState(object):
    """Mimics hou.cookStateType enum values."""

    def __init__(self, name):
        self._name = name

    def __str__(self):
        return "hou.cookStateType.{0}".format(self._name)


class _FakeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeNodeType(object):
    def __init__(self, name, category_name="Sop"):
        self._name = name
        self._category = _FakeCategory(category_name)

    def name(self):
        return self._name

    def category(self):
        return self._category


class _FakeParm(object):
    def __init__(self, name, value):
        self._name = name
        self._value = value

    def name(self):
        return self._name

    def eval(self):
        return self._value


class _FakeConnector(object):
    """Mimics hou.NodeConnection methods used by inputConnectors()."""

    def __init__(self, input_index, input_node, output_index=0):
        self._input_index = input_index
        self._input_node = input_node
        self._output_index = output_index

    def inputIndex(self):
        return self._input_index

    def inputNode(self):
        return self._input_node

    def outputIndex(self):
        return self._output_index


class _FakeNode(object):
    """Stub supporting get_node_info: parms / inputs / outputs / errors /
    cookState (optional) / needsToCook (optional) / inputConnectors / cook."""

    def __init__(self, name, type_name="geo", category="Sop", parent=None,
                 children=None, parms=None,
                 inputs=None, outputs=None,
                 errors=None, warnings=None,
                 cook_state=None, needs_to_cook=False, is_cooking=False,
                 version="20.5"):
        self._name = name
        self._type = _FakeNodeType(type_name, category)
        self._parent = parent
        self._children = list(children) if children else []
        self._parms = list(parms) if parms else []
        self._inputs = list(inputs) if inputs else []
        self._outputs = list(outputs) if outputs else []
        self._errors = list(errors) if errors else []
        self._warnings = list(warnings) if warnings else []
        self._version = version
        # H20.5+: cookState; H<20.5: only needsToCook
        if cook_state is not None:
            self.cookState = lambda: _FakeCookState(cook_state)
        # needsToCook may be on either; always provide attribute if requested
        self.needsToCook = lambda: bool(needs_to_cook)
        self.isCooking = lambda: bool(is_cooking)
        # Cook / position / path bookkeeping
        self.cook_calls = []
        for c in self._children:
            c._parent = self

    def name(self):
        return self._name

    def path(self):
        if hasattr(self, "_path") and self._path is not None:
            return self._path
        if self._parent is None:
            return "/" + self._name
        return self._parent.path().rstrip("/") + "/" + self._name

    def type(self):
        return self._type

    def parent(self):
        return self._parent

    def children(self):
        return list(self._children)

    def parms(self):
        return list(self._parms)

    def inputs(self):
        return list(self._inputs)

    def outputConnections(self):
        return list(self._outputs)

    def errors(self):
        return list(self._errors)

    def warnings(self):
        return list(self._warnings)

    def inputConnectors(self):
        out = []
        for idx, src in enumerate(self._inputs):
            if src is None:
                out.append(())
            else:
                out.append((_FakeConnector(idx, src, 0),))
        return tuple(out)

    def cook(self, force=False):
        self.cook_calls.append(bool(force))
        return True

    def position(self):
        return (self._position if hasattr(self, "_position") else (0.0, 0.0))

    def setPosition(self, pos):
        self._position = (float(pos[0]), float(pos[1]))


class _FakeHou(object):
    """Holds a node registry."""

    def __init__(self, nodes=None):
        self._nodes = dict(nodes) if nodes else {}

    def add_node(self, path, node):
        self._nodes[path] = node
        node._path = path

    def node(self, path):
        return self._nodes.get(path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_hou_with_target(name="box", inputs=None, errors=None, warnings=None,
                          cook_state=None, needs_to_cook=False,
                          is_cooking=False, version="20.5", parms=None):
    """Build a /obj/box node with optional inputs/errors and return (hou, node)."""
    target = _FakeNode(
        name, type_name="geo", category="Sop",
        inputs=inputs or [],
        errors=errors or [],
        warnings=warnings or [],
        cook_state=cook_state,
        needs_to_cook=needs_to_cook,
        is_cooking=is_cooking,
        version=version,
        parms=parms,
    )
    hou = _FakeHou()
    hou.add_node("/obj/" + name, target)
    return hou, target


# ===========================================================================
# Section A: _cook_state helper
# ===========================================================================
class CookStateTests(unittest.TestCase):

    def test_cook_state_h20_5_returns_enum_tail(self):
        """H20.5+ has node.cookState(); we must return just the enum name."""
        hou, target = _make_hou_with_target(cook_state="Cooked")
        result = ni._cook_state(hou, target)
        self.assertEqual(result, "Cooked")

    def test_cook_state_h20_5_dirty(self):
        hou, target = _make_hou_with_target(cook_state="Dirty")
        self.assertEqual(ni._cook_state(hou, target), "Dirty")

    def test_cook_state_h20_5_uncooked(self):
        hou, target = _make_hou_with_target(cook_state="Uncooked")
        self.assertEqual(ni._cook_state(hou, target), "Uncooked")

    def test_cook_state_pre_20_5_dirty_when_needs_to_cook(self):
        """H<20.5: needsToCook() True maps to Dirty."""
        target = _FakeNode(
            "a", version="19.5", needs_to_cook=True)
        self.assertEqual(ni._cook_state(None, target), "Dirty")

    def test_cook_state_pre_20_5_cooked_when_clean(self):
        """H<20.5: needsToCook() False maps to Cooked."""
        target = _FakeNode(
            "a", version="19.5", needs_to_cook=False)
        self.assertEqual(ni._cook_state(None, target), "Cooked")


# ===========================================================================
# Section B: get_node_info default behavior
# ===========================================================================
class GetNodeInfoDefaultsTests(unittest.TestCase):

    def test_missing_node_raises_value_error(self):
        hou = _FakeHou()
        with self.assertRaises(ValueError):
            ni.get_node_info(hou, "/obj/nonexistent")

    def test_default_returns_full_dict(self):
        """Default: include_errors=True, force_cook=False,
        include_input_details=False, compact=False."""
        hou, target = _make_hou_with_target(
            name="box",
            errors=["err1"], warnings=["warn1"], cook_state="Cooked",
            needs_to_cook=False,
        )
        result = ni.get_node_info(hou, target.path())
        self.assertEqual(result["path"], "/obj/box")
        self.assertEqual(result["type"], "geo")
        self.assertEqual(result["category"], "Sop")
        self.assertEqual(result["name"], "box")
        self.assertIn("position", result)
        self.assertEqual(result["children_count"], 0)
        self.assertEqual(result["input_count"], 0)
        self.assertEqual(result["output_count"], 0)
        self.assertIn("parameters", result)
        self.assertIn("errors", result)
        self.assertIn("warnings", result)
        self.assertEqual(result["cook_state"], "Cooked")
        self.assertEqual(result["needs_to_cook"], False)
        self.assertIn("is_cooking", result)
        # default include_input_details=False -> not present
        self.assertNotIn("input_connectors", result)

    def test_default_includes_errors_and_warnings(self):
        hou, target = _make_hou_with_target(
            errors=["e1", "e2"], warnings=["w1"])
        result = ni.get_node_info(hou, target.path())
        self.assertEqual(result["errors"], ["e1", "e2"])
        self.assertEqual(result["warnings"], ["w1"])

    def test_position_is_xy_list(self):
        hou, target = _make_hou_with_target()
        result = ni.get_node_info(hou, target.path())
        self.assertEqual(result["position"], [0.0, 0.0])
        self.assertIsInstance(result["position"], list)
        self.assertEqual(len(result["position"]), 2)


# ===========================================================================
# Section C: include_errors toggle
# ===========================================================================
class IncludeErrorsTests(unittest.TestCase):

    def test_include_errors_false_omits_errors_and_warnings(self):
        hou, target = _make_hou_with_target(
            errors=["e1"], warnings=["w1"])
        result = ni.get_node_info(
            hou, target.path(), include_errors=False)
        self.assertNotIn("errors", result)
        self.assertNotIn("warnings", result)

    def test_include_errors_true_explicit(self):
        hou, target = _make_hou_with_target(
            errors=["e1"], warnings=["w1"])
        result = ni.get_node_info(
            hou, target.path(), include_errors=True)
        self.assertEqual(result["errors"], ["e1"])
        self.assertEqual(result["warnings"], ["w1"])


# ===========================================================================
# Section D: force_cook
# ===========================================================================
class ForceCookTests(unittest.TestCase):

    def test_force_cook_false_does_not_cook(self):
        hou, target = _make_hou_with_target()
        ni.get_node_info(hou, target.path(), force_cook=False)
        self.assertEqual(target.cook_calls, [])

    def test_force_cook_true_calls_cook_with_force(self):
        hou, target = _make_hou_with_target()
        ni.get_node_info(hou, target.path(), force_cook=True)
        self.assertEqual(target.cook_calls, [True])


# ===========================================================================
# Section E: include_input_details + single inputConnectors call
# ===========================================================================
class IncludeInputDetailsTests(unittest.TestCase):

    def test_include_input_details_false_omits_input_connectors(self):
        a = _FakeNode("a")
        b = _FakeNode("b", inputs=[a])
        hou = _FakeHou()
        hou.add_node("/obj/a", a)
        hou.add_node("/obj/b", b)
        result = ni.get_node_info(hou, "/obj/b",
                                  include_input_details=False)
        self.assertNotIn("input_connectors", result)

    def test_include_input_details_true_handles_nested_hom_connectors(self):
        a = _FakeNode("a")
        c = _FakeNode("c")
        d = _FakeNode("d")
        b = _FakeNode("b", inputs=[None, a, c])
        b.inputConnectors = lambda: (
            (),
            (_FakeConnector(1, a, 2),),
            (_FakeConnector(2, c, 3), _FakeConnector(2, d, 4)),
        )
        hou = _FakeHou()
        hou.add_node("/obj/a", a)
        hou.add_node("/obj/b", b)
        hou.add_node("/obj/c", c)
        hou.add_node("/obj/d", d)

        result = ni.get_node_info(hou, "/obj/b",
                                  include_input_details=True)

        self.assertEqual(result["input_connectors"], [
            {"input_index": 0, "connections": []},
            {
                "input_index": 1,
                "connections": [
                    {"output_node": "/obj/a", "output_index": 2},
                ],
            },
            {
                "input_index": 2,
                "connections": [
                    {"output_node": "/obj/c", "output_index": 3},
                    {"output_node": "/obj/d", "output_index": 4},
                ],
            },
        ])

    def test_input_connectors_called_once_not_per_input(self):
        """Per brief 10.3: inputConnectors() must be called ONCE on the node,
        not once per input (i.e., not a per-input RPC)."""
        a = _FakeNode("a")
        b = _FakeNode("b", inputs=[a, a])
        call_counter = {"n": 0}
        original = b.inputConnectors

        def counted():
            call_counter["n"] += 1
            return original()

        b.inputConnectors = counted
        hou = _FakeHou()
        hou.add_node("/obj/a", a)
        hou.add_node("/obj/b", b)

        ni.get_node_info(hou, "/obj/b", include_input_details=True)
        self.assertEqual(
            call_counter["n"], 1,
            "inputConnectors() must be called exactly once, "
            "got {0}".format(call_counter["n"]))


# ===========================================================================
# Section F: compact mode
# ===========================================================================
class CompactTests(unittest.TestCase):

    def test_compact_returns_only_five_fields(self):
        hou, target = _make_hou_with_target(
            errors=["e"], warnings=["w"], cook_state="Cooked",
            parms=[_FakeParm("p", "v")])
        result = ni.get_node_info(hou, target.path(), compact=True)
        expected_keys = {"path", "type", "children_count",
                         "input_count", "output_count"}
        self.assertEqual(set(result.keys()), expected_keys)
        self.assertEqual(result["path"], "/obj/box")
        self.assertEqual(result["type"], "geo")
        self.assertEqual(result["children_count"], 0)
        self.assertEqual(result["input_count"], 0)
        self.assertEqual(result["output_count"], 0)

    def test_compact_does_not_include_parameters(self):
        hou, target = _make_hou_with_target(
            parms=[_FakeParm("p", "v")])
        result = ni.get_node_info(hou, target.path(), compact=True)
        self.assertNotIn("parameters", result)

    def test_compact_does_not_include_errors(self):
        hou, target = _make_hou_with_target(errors=["e"])
        result = ni.get_node_info(hou, target.path(), compact=True)
        self.assertNotIn("errors", result)


# ===========================================================================
# Section G: counts
# ===========================================================================
class CountsTests(unittest.TestCase):

    def test_input_count_and_output_count(self):
        a = _FakeNode("a")
        b = _FakeNode("b", inputs=[a])
        out = _FakeConnector(0, b, 0)
        c = _FakeNode("c", inputs=[b], outputs=[out])
        hou = _FakeHou()
        hou.add_node("/obj/a", a)
        hou.add_node("/obj/b", b)
        hou.add_node("/obj/c", c)
        result = ni.get_node_info(hou, "/obj/c")
        self.assertEqual(result["input_count"], 1)
        self.assertEqual(result["output_count"], 1)


# ===========================================================================
# Section H: backward compat
# ===========================================================================
class BackwardCompatTests(unittest.TestCase):

    def test_legacy_single_arg_signature(self):
        """get_node_info(hou, path) — only positional path — must still work
        (pre-PR-10 callers)."""
        hou, target = _make_hou_with_target()
        result = ni.get_node_info(hou, target.path())
        self.assertEqual(result["path"], "/obj/box")


# ===========================================================================
# Section I: bridge @mcp.tool() style + behavior — AST probe
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "houdini_mcp_server.py")
PR10_SECTION_HEADER = "# PR 10 Node Info Tool"
PR10_BRIDGE_TOOL = "get_node_info"


def _parse_server_source():
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        return f.read()


def _find_pr10_tool_node():
    src = _parse_server_source()
    tree = ast.parse(src)
    lines = src.splitlines()

    header_line = None
    for i, line in enumerate(lines, start=1):
        if PR10_SECTION_HEADER in line:
            header_line = i
            break
    if header_line is None:
        raise AssertionError(
            "PR 10 section marker not found in houdini_mcp_server.py. "
            "Add a comment line containing {0!r} before the new tool."
            .format(PR10_SECTION_HEADER))

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.lineno <= header_line:
            continue
        if node.name != PR10_BRIDGE_TOOL:
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
        return node
    raise AssertionError(
        "PR 10 bridge tool {0!r} not found after section header."
        .format(PR10_BRIDGE_TOOL))


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


class PR10BridgeStyleTests(unittest.TestCase):
    """PR 10 brief: bridge @mcp.tool() must have no type annotations and
    Chinese docstring (PR 7 fix paradigm)."""

    def setUp(self):
        self.fn = _find_pr10_tool_node()

    def test_no_type_annotations(self):
        kinds = _signature_annotation_kinds(self.fn)
        self.assertFalse(
            kinds["arg_annotations"],
            "get_node_info bridge must not have parameter type annotations")
        self.assertFalse(
            kinds["return_annotation"],
            "get_node_info bridge must not have return type annotation")

    def test_chinese_docstring(self):
        doc = ast.get_docstring(self.fn) or ""
        self.assertTrue(
            _has_cjk(doc),
            "get_node_info bridge docstring must contain Chinese (CJK). "
            "Got: {0!r}".format(doc))

    def test_accepts_all_kwargs(self):
        params = _extract_param_names(self.fn)
        for kw in ("ctx", "node_path", "include_errors", "force_cook",
                   "include_input_details", "compact"):
            self.assertIn(
                kw, params,
                "get_node_info bridge must accept {0!r} kwarg; got {1}"
                .format(kw, params))

    def test_section_marker_present(self):
        src = _parse_server_source()
        self.assertIn(
            PR10_SECTION_HEADER, src,
            "PR 10 section marker missing in houdini_mcp_server.py")


class PR10BridgeBehaviorTests(unittest.TestCase):
    """PR 10 brief: bridge tool must call _houdini_call with correct cmd_type
    + parameter keys (node_path / include_errors / force_cook /
    include_input_details / compact), and surface errors."""

    def setUp(self):
        self.fn = _find_pr10_tool_node()

    def test_calls_houdini_get_node_info(self):
        d = _find_houdini_call_kwargs(self.fn, "get_node_info")
        self.assertIsNotNone(
            d,
            "get_node_info bridge must call _houdini_call('get_node_info', ...)")
        keys = _dict_keys(d)
        for expected in ("node_path", "include_errors", "force_cook",
                         "include_input_details", "compact"):
            self.assertIn(
                expected, keys,
                "get_node_info bridge params must include {0!r}; got {1}"
                .format(expected, keys))

    def test_exec_compiles_and_runs(self):
        """Execute the bridge fn in isolation to confirm it parses + runs
        with a stubbed _houdini_call, and passes all kwargs through."""
        lines = _parse_server_source().splitlines()
        body = "\n".join(lines[self.fn.lineno - 1:self.fn.end_lineno])

        houdini_requests = []

        def _houdini_call(command, params):
            houdini_requests.append((command, params))
            if command == "get_node_info":
                # Pretend the server returns the expected full dict shape.
                return {
                    "status": "success",
                    "result": {
                        "path": params["node_path"],
                        "type": "geo",
                        "category": "Sop",
                        "name": "box",
                        "position": [0.0, 0.0],
                        "children_count": 0,
                        "input_count": 0,
                        "output_count": 0,
                        "parameters": [],
                        "cook_state": "Cooked",
                        "needs_to_cook": False,
                        "is_cooking": False,
                    },
                }
            return {"status": "error", "message": "unexpected"}

        _FakeMcp = type(
            "_FakeMcp", (), {"tool": staticmethod(lambda: lambda f: f)})
        namespace = {"_houdini_call": _houdini_call, "mcp": _FakeMcp()}
        module = ast.Module(body=[self.fn], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, SERVER_PY, "exec"), namespace)

        # Default call: only node_path -> defaults applied for new params.
        result = namespace["get_node_info"](object(), "/obj/box")
        self.assertEqual(len(houdini_requests), 1)
        cmd, params = houdini_requests[0]
        self.assertEqual(cmd, "get_node_info")
        self.assertEqual(params["node_path"], "/obj/box")
        self.assertEqual(params["include_errors"], True)
        self.assertEqual(params["force_cook"], False)
        self.assertEqual(params["include_input_details"], False)
        self.assertEqual(params["compact"], False)
        # Successful response: bridge returns the result envelope as-is.
        self.assertEqual(result["status"], "success")

    def test_exec_error_passthrough(self):
        """When _houdini_call returns an error envelope, the bridge must
        propagate it (so the agent sees the error rather than a fake success)."""
        lines = _parse_server_source().splitlines()
        body = "\n".join(lines[self.fn.lineno - 1:self.fn.end_lineno])

        def _houdini_call(command, params):
            return {
                "status": "error",
                "message": "Node not found: " + params.get("node_path", ""),
                "origin": "houdini",
            }

        _FakeMcp = type(
            "_FakeMcp", (), {"tool": staticmethod(lambda: lambda f: f)})
        namespace = {"_houdini_call": _houdini_call, "mcp": _FakeMcp()}
        module = ast.Module(body=[self.fn], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, SERVER_PY, "exec"), namespace)

        result = namespace["get_node_info"](object(), "/obj/missing")
        self.assertEqual(result["status"], "error")
        self.assertIn("Node not found", result["message"])


if __name__ == "__main__":
    unittest.main()