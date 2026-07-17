"""Unit tests for external/houdinimcp/_graph_edit.py + PR 9 bridge tools.

Stdlib unittest, no hython required. hou is mocked via a small stub class.
Tests cover:
    - reorder_inputs: full disconnect + reconnect per new_order / identity /
      empty / partial reorder / missing node -> ValueError
    - layout_children: default spacing / custom spacing / horizontal &
      vertical direction / empty children / missing parent -> ValueError
    - set_node_position: normal / missing node -> ValueError
    - set_node_color: normal / clamp negative / clamp >1 / mixed /
      missing node -> ValueError
    - create_network_box: no name / custom name / with nodes /
      missing nodes skipped / missing parent -> ValueError
    - bridge 5 tools: style AST probe (no type annotations + Chinese
      docstring) + _houdini_call param keys + legacy alias back-compat

Bridge style is verified via AST probe inside this file (we do NOT import
houdini_mcp_server.py because it has heavy runtime deps). test_bridge_style.py
is PR 7 specific; this test does not modify it. PR 9 tools are placed before
the PR 7 section header so the existing PR 7 probe keeps returning 3 tools.

Run with:
    python -m unittest tests.test_graph_edit -v
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
# `from . import _common as cmn` inside _graph_edit.py resolves.
pkg = types.ModuleType("houdinimcp")
pkg.__path__ = [ROOT]
sys.modules["houdinimcp"] = pkg

_spec_common = _ilu.spec_from_file_location("houdinimcp._common",
                                            os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec_common)
sys.modules["houdinimcp._common"] = common
_spec_common.loader.exec_module(common)
cmn = common

_spec_graph = _ilu.spec_from_file_location(
    "houdinimcp._graph_edit", os.path.join(ROOT, "_graph_edit.py"))
graph_edit = _ilu.module_from_spec(_spec_graph)
sys.modules["houdinimcp._graph_edit"] = graph_edit
_spec_graph.loader.exec_module(graph_edit)
ge = graph_edit


# ---------------------------------------------------------------------------
# hou stub: enough surface area for graph_edit handlers.
# ---------------------------------------------------------------------------
class _FakeColor(object):
    """Records the RGB triple passed to hou.Color((r,g,b))."""

    def __init__(self, rgb):
        self.rgb = tuple(float(c) for c in rgb)

    def __eq__(self, other):
        return isinstance(other, _FakeColor) and self.rgb == other.rgb

    def __repr__(self):
        return "_FakeColor({0!r})".format(self.rgb)


class _FakeConnector(object):
    """Mimics hou.Node.inputConnectors() output tuples."""

    def __init__(self, input_index, output_node, output_index=0):
        self.input_index = input_index
        self.output_node = output_node
        self.output_index = output_index


class _FakeNode(object):
    """Minimal graph node stub.

    Tracks setInput / setPosition / setColor calls; supports inputConnectors
    derived from the current setInput state. Each node owns its child list
    so parent.children() works.
    """

    def __init__(self, name, parent=None):
        self._name = name
        self._parent = parent
        self._path = None
        self._inputs = {}
        self._children = []
        self.set_position_calls = []
        self.set_color_calls = []
        self.network_box_creator_returns = []

    def name(self):
        return self._name

    def path(self):
        if self._path is not None:
            return self._path
        if self._parent is None:
            return "/" + self._name
        return self._parent.path().rstrip("/") + "/" + self._name

    def setInput(self, input_index, output_node, output_index=0):
        self.set_input_calls = getattr(self, "set_input_calls", [])
        self.set_input_calls.append((input_index, output_node, output_index))
        if output_node is None:
            self._inputs.pop(input_index, None)
        else:
            self._inputs[input_index] = (output_node, output_index)

    def inputConnectors(self):
        return tuple(
            _FakeConnector(idx, src, oidx)
            for idx, (src, oidx) in sorted(self._inputs.items())
        )

    def setPosition(self, pos):
        self.set_position_calls.append(tuple(pos))

    def setColor(self, color):
        self.set_color_calls.append(color)

    def createNetworkBox(self, name=None):
        box = _FakeNetworkBox(name=name or "auto_box_{0}".format(
            len(self.network_box_creator_returns) + 1), parent=self)
        self.network_box_creator_returns.append(box)
        return box

    def children(self):
        return list(self._children)


class _FakeNetworkBox(object):
    def __init__(self, name, parent):
        self._name = name
        self._parent = parent
        self.added_nodes = []

    def name(self):
        return self._name

    def addNode(self, node):
        self.added_nodes.append(node)


class _FakeHou(object):
    """Registry of named nodes for graph-edit handlers."""

    def __init__(self):
        self._nodes = {}

    def add_node(self, path, node):
        self._nodes[path] = node
        node._path = path
        if node._parent is not None:
            node._parent._children.append(node)

    def node(self, path):
        return self._nodes.get(path)

    @staticmethod
    def Color(rgb):
        return _FakeColor(rgb)


def _make_node(name, parent=None):
    return _FakeNode(name, parent=parent)


def _make_hou():
    return _FakeHou()


def _build_simple_network(hou, container_path="/obj/container"):
    """Build a 3-input target network under container."""
    container = _make_node("container")
    hou.add_node(container_path, container)
    a = _make_node("a", parent=container)
    b = _make_node("b", parent=container)
    c = _make_node("c", parent=container)
    hou.add_node(container_path + "/a", a)
    hou.add_node(container_path + "/b", b)
    hou.add_node(container_path + "/c", c)
    target = _make_node("target", parent=container)
    hou.add_node(container_path + "/target", target)
    target.setInput(0, a)
    target.setInput(1, b)
    target.setInput(2, c)
    return container, a, b, c, target


# ===========================================================================
# Section A: reorder_inputs
# ===========================================================================
class ReorderInputsTests(unittest.TestCase):

    def test_normal_reorder_swap_0_and_2(self):
        hou = _make_hou()
        container, a, b, c, target = _build_simple_network(hou)

        result = ge.reorder_inputs(hou, target.path(), [2, 1, 0])

        self.assertEqual(target._inputs[0][0], c)
        self.assertEqual(target._inputs[1][0], b)
        self.assertEqual(target._inputs[2][0], a)
        self.assertEqual(result["path"], target.path())
        self.assertEqual(result["old_order"], [0, 1, 2])
        self.assertEqual(result["new_order"], [2, 1, 0])
        self.assertTrue(result["success"])

    def test_reorder_to_identity(self):
        hou = _make_hou()
        container, a, b, c, target = _build_simple_network(hou)

        result = ge.reorder_inputs(hou, target.path(), [0, 1, 2])

        self.assertEqual(target._inputs[0][0], a)
        self.assertEqual(target._inputs[1][0], b)
        self.assertEqual(target._inputs[2][0], c)
        self.assertEqual(result["old_order"], [0, 1, 2])
        self.assertEqual(result["new_order"], [0, 1, 2])

    def test_reorder_to_empty_disconnects_all(self):
        hou = _make_hou()
        container, a, b, c, target = _build_simple_network(hou)
        self.assertEqual(len(target._inputs), 3)

        result = ge.reorder_inputs(hou, target.path(), [])

        self.assertEqual(target._inputs, {})
        self.assertEqual(result["new_order"], [])

    def test_partial_reorder_disconnects_unmentioned(self):
        """new_order with fewer entries than inputs: only mentioned old
        inputs are rewired at new positions 0..N-1; any input index not
        present in new_order ends up disconnected.
        """
        hou = _make_hou()
        container, a, b, c, target = _build_simple_network(hou)

        # Only mention old input 2 -> wire new position 0 from old 2.
        ge.reorder_inputs(hou, target.path(), [2])

        # Input 0 should now point to c (was at input 2)
        self.assertEqual(target._inputs[0][0], c)
        # Inputs 1 and 2 are not in new_order -> disconnected
        self.assertNotIn(1, target._inputs)
        self.assertNotIn(2, target._inputs)

    def test_node_missing_raises_value_error(self):
        hou = _make_hou()
        with self.assertRaises(ValueError):
            ge.reorder_inputs(hou, "/obj/nonexistent", [0, 1])


# ===========================================================================
# Section B: layout_children
# ===========================================================================
class LayoutChildrenTests(unittest.TestCase):

    def test_default_spacing_horizontal(self):
        hou = _make_hou()
        container = _make_node("container")
        hou.add_node("/obj/container", container)
        for nm in ("a", "b", "c"):
            child = _make_node(nm, parent=container)
            hou.add_node("/obj/container/" + nm, child)

        result = ge.layout_children(hou, "/obj/container")

        self.assertEqual(result["children_count"], 3)
        self.assertEqual(result["direction"], "horizontal")
        self.assertEqual(result["spacing"], [2.0, 1.5])
        positions = [ch.set_position_calls[-1] for ch in container.children()]
        self.assertEqual(positions[0][0], 0.0)
        self.assertEqual(positions[1][0], 2.0)
        self.assertEqual(positions[2][0], 4.0)
        for p in positions:
            self.assertEqual(p[1], 0.0)

    def test_custom_spacing(self):
        hou = _make_hou()
        container = _make_node("container")
        hou.add_node("/obj/container", container)
        for nm in ("a", "b"):
            child = _make_node(nm, parent=container)
            hou.add_node("/obj/container/" + nm, child)

        result = ge.layout_children(
            hou, "/obj/container",
            horizontal_spacing=4.0, vertical_spacing=2.0,
        )
        self.assertEqual(result["spacing"], [4.0, 2.0])
        positions = [ch.set_position_calls[-1] for ch in container.children()]
        self.assertEqual(positions[0][0], 0.0)
        self.assertEqual(positions[1][0], 4.0)
        for p in positions:
            self.assertEqual(p[1], 0.0)

    def test_vertical_direction(self):
        hou = _make_hou()
        container = _make_node("container")
        hou.add_node("/obj/container", container)
        for nm in ("a", "b", "c"):
            child = _make_node(nm, parent=container)
            hou.add_node("/obj/container/" + nm, child)

        result = ge.layout_children(
            hou, "/obj/container", direction="vertical",
        )
        self.assertEqual(result["direction"], "vertical")
        positions = [ch.set_position_calls[-1] for ch in container.children()]
        # Vertical: y grows negatively (Houdini convention: top-down), x = 0
        self.assertEqual(positions[0][1], 0.0)
        self.assertEqual(positions[1][1], -1.5)
        self.assertEqual(positions[2][1], -3.0)
        for p in positions:
            self.assertEqual(p[0], 0.0)

    def test_empty_children(self):
        hou = _make_hou()
        container = _make_node("container")
        hou.add_node("/obj/container", container)

        result = ge.layout_children(hou, "/obj/container")
        self.assertEqual(result["children_count"], 0)

    def test_parent_missing_raises_value_error(self):
        hou = _make_hou()
        with self.assertRaises(ValueError):
            ge.layout_children(hou, "/obj/nonexistent")


# ===========================================================================
# Section C: set_node_position
# ===========================================================================
class SetNodePositionTests(unittest.TestCase):

    def test_normal_position(self):
        hou = _make_hou()
        n = _make_node("n")
        hou.add_node("/obj/n", n)

        result = ge.set_node_position(hou, "/obj/n", 3.5, -2.0)

        self.assertEqual(n.set_position_calls[-1], (3.5, -2.0))
        self.assertEqual(result["path"], "/obj/n")
        self.assertEqual(result["position"], [3.5, -2.0])
        self.assertTrue(result["success"])

    def test_zero_position(self):
        hou = _make_hou()
        n = _make_node("n")
        hou.add_node("/obj/n", n)

        result = ge.set_node_position(hou, "/obj/n", 0, 0)
        self.assertEqual(n.set_position_calls[-1], (0, 0))

    def test_node_missing_raises_value_error(self):
        hou = _make_hou()
        with self.assertRaises(ValueError):
            ge.set_node_position(hou, "/obj/nonexistent", 0, 0)


# ===========================================================================
# Section D: set_node_color (with [0,1] clamp)
# ===========================================================================
class SetNodeColorTests(unittest.TestCase):

    def test_normal_color(self):
        hou = _make_hou()
        n = _make_node("n")
        hou.add_node("/obj/n", n)

        result = ge.set_node_color(hou, "/obj/n", 0.5, 0.25, 0.75)

        self.assertEqual(n.set_color_calls[-1], _FakeColor((0.5, 0.25, 0.75)))
        self.assertEqual(result["color"], [0.5, 0.25, 0.75])
        self.assertTrue(result["success"])

    def test_clamp_negative_to_zero(self):
        hou = _make_hou()
        n = _make_node("n")
        hou.add_node("/obj/n", n)

        result = ge.set_node_color(hou, "/obj/n", -0.5, -2.0, -0.1)

        self.assertEqual(n.set_color_calls[-1], _FakeColor((0.0, 0.0, 0.0)))
        self.assertEqual(result["color"], [0.0, 0.0, 0.0])

    def test_clamp_above_one_to_one(self):
        hou = _make_hou()
        n = _make_node("n")
        hou.add_node("/obj/n", n)

        result = ge.set_node_color(hou, "/obj/n", 1.5, 2.0, 100.0)

        self.assertEqual(n.set_color_calls[-1], _FakeColor((1.0, 1.0, 1.0)))
        self.assertEqual(result["color"], [1.0, 1.0, 1.0])

    def test_mixed_clamp(self):
        hou = _make_hou()
        n = _make_node("n")
        hou.add_node("/obj/n", n)

        result = ge.set_node_color(hou, "/obj/n", -0.1, 0.5, 1.5)

        self.assertEqual(n.set_color_calls[-1], _FakeColor((0.0, 0.5, 1.0)))
        self.assertEqual(result["color"], [0.0, 0.5, 1.0])

    def test_node_missing_raises_value_error(self):
        hou = _make_hou()
        with self.assertRaises(ValueError):
            ge.set_node_color(hou, "/obj/nonexistent", 0, 0, 0)


# ===========================================================================
# Section E: create_network_box
# ===========================================================================
class CreateNetworkBoxTests(unittest.TestCase):

    def test_no_name_uses_hou_default(self):
        hou = _make_hou()
        parent = _make_node("parent")
        hou.add_node("/obj/parent", parent)

        result = ge.create_network_box(hou, "/obj/parent")

        self.assertEqual(result["path"], "/obj/parent")
        self.assertTrue(result["name"])
        self.assertEqual(result["nodes_in_box"], [])
        self.assertEqual(len(parent.network_box_creator_returns), 1)

    def test_custom_name(self):
        hou = _make_hou()
        parent = _make_node("parent")
        hou.add_node("/obj/parent", parent)

        result = ge.create_network_box(hou, "/obj/parent", name="myBox")

        self.assertEqual(result["name"], "myBox")

    def test_with_nodes(self):
        hou = _make_hou()
        parent = _make_node("parent")
        hou.add_node("/obj/parent", parent)
        a = _make_node("a", parent=parent)
        b = _make_node("b", parent=parent)
        hou.add_node("/obj/parent/a", a)
        hou.add_node("/obj/parent/b", b)

        result = ge.create_network_box(
            hou, "/obj/parent", name="grp",
            node_paths=["/obj/parent/a", "/obj/parent/b"],
        )

        self.assertEqual(result["nodes_in_box"],
                         ["/obj/parent/a", "/obj/parent/b"])
        box = parent.network_box_creator_returns[-1]
        self.assertEqual(box.added_nodes, [a, b])

    def test_missing_nodes_are_skipped(self):
        hou = _make_hou()
        parent = _make_node("parent")
        hou.add_node("/obj/parent", parent)
        a = _make_node("a", parent=parent)
        hou.add_node("/obj/parent/a", a)

        result = ge.create_network_box(
            hou, "/obj/parent", name="mixed",
            node_paths=["/obj/parent/a", "/obj/parent/ghost"],
        )

        self.assertEqual(result["nodes_in_box"],
                         ["/obj/parent/a", "/obj/parent/ghost"])
        box = parent.network_box_creator_returns[-1]
        self.assertEqual(box.added_nodes, [a])

    def test_parent_missing_raises_value_error(self):
        hou = _make_hou()
        with self.assertRaises(ValueError):
            ge.create_network_box(hou, "/obj/nonexistent")


# ===========================================================================
# Section F: bridge @mcp.tool() style & behavior — AST probe
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "houdini_mcp_server.py")
PR9_SECTION_HEADER = "# PR 9 Graph Edit Tools"

PR9_BRIDGE_TOOLS = [
    "reorder_inputs",
    "layout_children",
    "set_node_position",
    "set_node_color",
    "create_network_box",
]


def _parse_server_source():
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        return f.read()


def _find_pr9_tool_nodes():
    """Locate the 5 PR 9 bridge @mcp.tool() function nodes via AST.

    Stops scanning when the next top-level "# PR" section header is hit
    (so we never accidentally pick up PR 7 / future sections).
    """
    src = _parse_server_source()
    tree = ast.parse(src)
    lines = src.splitlines()

    header_line = None
    for i, line in enumerate(lines, start=1):
        if PR9_SECTION_HEADER in line:
            header_line = i
            break
    if header_line is None:
        raise AssertionError(
            "PR 9 section marker not found in houdini_mcp_server.py. "
            "Add a comment line containing {0!r} before the new tools."
            .format(PR9_SECTION_HEADER))

    found = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.lineno <= header_line:
            continue
        if node.name not in PR9_BRIDGE_TOOLS:
            continue
        # Stop at next "# PR" section header to avoid sweeping later sections
        src_line = lines[node.lineno - 1]
        # (No-op; we already filtered by name.)
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
    missing = [n for n in PR9_BRIDGE_TOOLS if n not in found]
    if missing:
        raise AssertionError(
            "PR 9 bridge tools not found after section header: {0}. "
            "Found: {1}".format(missing, list(found.keys())))
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


class PR9BridgeStyleTests(unittest.TestCase):
    """PR 9 brief: bridge @mcp.tool() functions must have no type annotations
    and Chinese docstrings (PR 7 fix paradigm)."""

    def setUp(self):
        self.tools = _find_pr9_tool_nodes()
        self.assertEqual(
            len(self.tools), 5,
            "Expected 5 PR 9 bridge tools, found {0}: {1}".format(
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
        self.assertIn(PR9_SECTION_HEADER, src,
                      "PR 9 section marker missing in houdini_mcp_server.py")


class PR9BridgeBehaviorTests(unittest.TestCase):
    """PR 9 brief: bridge tools must call _houdini_call with the correct
    command type and parameter keys, and must surface errors."""

    def setUp(self):
        self.tools = _find_pr9_tool_nodes()
        self.src = _parse_server_source()

    # -- reorder_inputs ------------------------------------------------------
    def test_reorder_inputs_calls_houdini_with_node_path_and_new_order(self):
        fn = self.tools["reorder_inputs"]
        d = _find_houdini_call_kwargs(fn, "reorder_inputs")
        self.assertIsNotNone(
            d, "reorder_inputs must call _houdini_call('reorder_inputs', ...)")
        keys = _dict_keys(d)
        self.assertIn("node_path", keys)
        self.assertIn("new_order", keys)

    def test_reorder_inputs_accepts_legacy_order_alias(self):
        """Backward-compat: bridge must read 'order' as alias for new_order."""
        fn = self.tools["reorder_inputs"]
        lines = self.src.splitlines()
        body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
        self.assertIn(
            "order", body,
            "reorder_inputs bridge must reference 'order' (legacy alias)")

    def test_reorder_inputs_accepts_legacy_order_kwarg(self):
        """Legacy order kwarg must reach the server as new_order only."""
        fn = self.tools["reorder_inputs"]
        namespace = {
            "mcp": type(
                "_McpStub", (), {
                    "tool": lambda self: (lambda decorated: decorated),
                })(),
        }
        houdini_requests = []
        server_calls = []

        class _ServerStub(object):
            def reorder_inputs(self, node_path, new_order):
                server_calls.append((node_path, new_order))
                return {"new_order": new_order}

        server = _ServerStub()

        def _houdini_call(command, params):
            houdini_requests.append((command, params))
            try:
                return getattr(server, command)(**params)
            except TypeError as exc:
                return {"error": str(exc)}

        namespace["_houdini_call"] = _houdini_call
        module = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, SERVER_PY, "exec"), namespace)

        result = namespace["reorder_inputs"](
            object(), "/obj/x", order=[0, 1])

        self.assertEqual(
            houdini_requests,
            [("reorder_inputs", {
                "node_path": "/obj/x",
                "new_order": [0, 1],
            })],
        )
        self.assertEqual(server_calls, [("/obj/x", [0, 1])])
        self.assertEqual(result, {"new_order": [0, 1]})

    # -- layout_children -----------------------------------------------------
    def test_layout_children_calls_houdini_with_parent_path(self):
        fn = self.tools["layout_children"]
        d = _find_houdini_call_kwargs(fn, "layout_children")
        self.assertIsNotNone(
            d, "layout_children must call _houdini_call('layout_children', ...)")
        keys = _dict_keys(d)
        self.assertIn("parent_path", keys)

    def test_layout_children_accepts_spacing_kwargs(self):
        fn = self.tools["layout_children"]
        params = _extract_param_names(fn)
        for name in ("horizontal_spacing", "vertical_spacing", "direction"):
            self.assertIn(
                name, params,
                "layout_children bridge must accept {0} kwarg".format(name))

    def test_layout_children_supports_legacy_parent_kwarg(self):
        """Backward-compat: legacy bridge call layout_children(ctx, parent)
        must still work — bridge must read 'parent' from params."""
        fn = self.tools["layout_children"]
        lines = self.src.splitlines()
        body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
        self.assertIn(
            "parent", body,
            "layout_children bridge body must reference 'parent' (legacy)")

    # -- set_node_position --------------------------------------------------
    def test_set_node_position_calls_houdini_with_coords(self):
        fn = self.tools["set_node_position"]
        d = _find_houdini_call_kwargs(fn, "set_node_position")
        self.assertIsNotNone(d)
        keys = _dict_keys(d)
        self.assertIn("node_path", keys)
        self.assertIn("x", keys)
        self.assertIn("y", keys)

    # -- set_node_color ------------------------------------------------------
    def test_set_node_color_calls_houdini_with_rgb(self):
        fn = self.tools["set_node_color"]
        d = _find_houdini_call_kwargs(fn, "set_node_color")
        self.assertIsNotNone(d)
        keys = _dict_keys(d)
        self.assertIn("node_path", keys)
        for ch in ("r", "g", "b"):
            self.assertIn(ch, keys, "set_node_color params must include {0}"
                          .format(ch))

    # -- create_network_box --------------------------------------------------
    def test_create_network_box_calls_houdini_with_parent_and_name(self):
        fn = self.tools["create_network_box"]
        d = _find_houdini_call_kwargs(fn, "create_network_box")
        self.assertIsNotNone(d)
        keys = _dict_keys(d)
        self.assertIn("parent_path", keys)
        self.assertIn("name", keys)
        self.assertIn("node_paths", keys)


if __name__ == "__main__":
    unittest.main()