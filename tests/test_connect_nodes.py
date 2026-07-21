"""Unit tests for HoudiniMCPServer.connect_nodes — B2 H21 compat audit.

Implements the Task 5.4 spec cases from
openspec/changes/opera-houdinimcp-h21-compat-audit (B2 bug):

    SOP descendant → OBJ container must route to
        src.setDisplayFlag(True) + src.setRenderFlag(True)
    (NOT dst.setInput — which live-verified hangs 30s+ on H21 because
    hou.ObjNode.setInput(0, sop_descendant) is unsupported).

Direct-load test: AST-extracts the production connect_nodes body from
server.py (mirrors the test_tier2_bugfixes.py F-C helper) and binds it
to a tiny stub server. No conftest hou stub needed at runtime since we
don't import the full server.py (avoids pulling PySide6 / requests).

Spec scenarios covered:
    - test_connect_sop_to_obj_uses_display_flag
        SOP→OBJ: display flags set on src, dst.setInput NOT called.
    - test_connect_sop_to_obj_returns_via_marker
        Response envelope carries via="sop_display_flag".
    - test_connect_sop_to_sop_still_uses_setInput
        Same-parent SOP→SOP still uses the 3-arg setInput form
        (regression guard).

Run with:
    python -m pytest tests/test_connect_nodes.py -v
    python -m unittest tests.test_connect_nodes -v
"""
import ast
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Tiny stubs mirroring hou.Node pieces touched by connect_nodes.
# ---------------------------------------------------------------------------
class _FakeNode(object):
    """Minimal hou.Node stub. ``kind="sop"`` exposes setDisplayFlag /
    setRenderFlag (matching hou.SopNode); ``kind="obj"`` (default) does
    not — exactly like the real hou class hierarchy where only
    hou.SopNode has the display-flag setters."""

    def __init__(self, name, parent=None, kind="obj"):
        self._name = name
        self._parent = parent
        self._path = None
        self.kind = kind
        self.set_input_calls = []
        self.set_display_flag_calls = []
        self.set_render_flag_calls = []
        if kind == "sop":
            # Bind as instance attributes — hasattr(src, "setDisplayFlag")
            # in production code then returns True exactly for SOP nodes.
            self.setDisplayFlag = lambda v: self.set_display_flag_calls.append(v)
            self.setRenderFlag = lambda v: self.set_render_flag_calls.append(v)

    def name(self):
        return self._name

    def path(self):
        if self._path is not None:
            return self._path
        if self._parent is None:
            return "/" + self._name
        return self._parent.path().rstrip("/") + "/" + self._name

    def parent(self):
        return self._parent

    def setInput(self, *args):
        self.set_input_calls.append(tuple(args))


class _FakeHou(object):
    """Registry of named fake nodes — stands in for the hou module's
    node(path) lookup."""

    def __init__(self):
        self._nodes = {}

    def add_node(self, path, node):
        self._nodes[path] = node
        node._path = path

    def node(self, path):
        return self._nodes.get(path)


class _StubServer(object):
    """Minimal stand-in for HoudiniMCPServer exposing only the two
    methods connect_nodes touches: _resolve_node + connect_nodes
    (the latter bound from AST extraction)."""

    def __init__(self, hou_reg):
        self._hou = hou_reg

    def _resolve_node(self, path):
        node = self._hou.node(path)
        if not node:
            raise ValueError("Node not found: {0}".format(path))
        return node


def _load_connect_nodes_from_server():
    """AST-extract just the connect_nodes method body from server.py,
    avoiding a full import (which would pull PySide6 / requests etc.)."""
    server_path = os.path.join(ROOT, "server.py")
    with open(server_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "connect_nodes":
                stub_cls = ast.ClassDef(
                    name="_ASTStubServer",
                    bases=[],
                    keywords=[],
                    body=[item],
                    decorator_list=[],
                )
                mod = ast.Module(body=[stub_cls], type_ignores=[])
                ast.fix_missing_locations(mod)
                ns = {}
                exec(compile(mod, server_path, "exec"), ns)
                return ns["_ASTStubServer"].connect_nodes
    raise AssertionError("connect_nodes method not found in server.py")


_CONNECT_NODES = _load_connect_nodes_from_server()


# ---------------------------------------------------------------------------
# Task 5.4 spec scenarios.
# ---------------------------------------------------------------------------
class ConnectNodesSopToObjDisplayFlag(unittest.TestCase):
    """B2 (H21 compat): SOP→OBJ container must NOT call dst.setInput
    (hangs 30s+ on H21); must set src display/render flags instead."""

    def test_connect_sop_to_obj_uses_display_flag(self):
        """Spec 5.2: SOP descendant → OBJ container sets
        src.setDisplayFlag(True) + src.setRenderFlag(True) and does
        NOT call dst.setInput."""
        hou = _FakeHou()
        obj_root = _FakeNode("obj")
        hou.add_node("/obj", obj_root)
        # OBJ container (default kind="obj": no SOP display API)
        obj_container = _FakeNode("table", parent=obj_root)
        hou.add_node("/obj/table", obj_container)
        # SOP child (kind="sop": has setDisplayFlag/setRenderFlag)
        sop_child = _FakeNode("wood_grain", parent=obj_container, kind="sop")
        hou.add_node("/obj/table/wood_grain", sop_child)

        server = _StubServer(hou)
        result = _CONNECT_NODES(
            server,
            from_path="/obj/table/wood_grain",
            to_path="/obj/table",
            input_index=0,
            output_index=0,
        )

        # Display + render flags set on src SOP
        self.assertEqual(
            sop_child.set_display_flag_calls, [True],
            "src.setDisplayFlag(True) must be called for SOP→OBJ")
        self.assertEqual(
            sop_child.set_render_flag_calls, [True],
            "src.setRenderFlag(True) must be called for SOP→OBJ")
        # CRITICAL: dst.setInput must NOT be called (H21 hang root cause)
        self.assertEqual(
            obj_container.set_input_calls, [],
            "dst.setInput must NOT be called for SOP→OBJ "
            "(hangs 30s+ on H21)")

    def test_connect_sop_to_obj_returns_via_marker(self):
        """Spec 5.3: response envelope carries via="sop_display_flag"
        to mark the special H21-safe path was taken."""
        hou = _FakeHou()
        obj_root = _FakeNode("obj")
        hou.add_node("/obj", obj_root)
        obj_container = _FakeNode("table", parent=obj_root)
        hou.add_node("/obj/table", obj_container)
        sop_child = _FakeNode("wood_grain", parent=obj_container, kind="sop")
        hou.add_node("/obj/table/wood_grain", sop_child)

        server = _StubServer(hou)
        result = _CONNECT_NODES(
            server,
            from_path="/obj/table/wood_grain",
            to_path="/obj/table",
            input_index=0,
            output_index=0,
        )

        self.assertEqual(result.get("via"), "sop_display_flag")
        # Standard envelope fields also present
        self.assertEqual(result["from"], "/obj/table/wood_grain")
        self.assertEqual(result["to"], "/obj/table")
        self.assertEqual(result["input_index"], 0)
        self.assertEqual(result["output_index"], 0)

    def test_connect_sop_to_sop_still_uses_setInput(self):
        """Spec guard: same-parent SOP→SOP must still use the legacy
        3-arg setInput form (regression guard — B2 fix only affects the
        cross-parent SOP→OBJ case)."""
        hou = _FakeHou()
        obj_root = _FakeNode("obj")
        hou.add_node("/obj", obj_root)
        # Two sibling SOPs under the same OBJ container — same parent.
        container = _FakeNode("geo1", parent=obj_root)
        hou.add_node("/obj/geo1", container)
        sop_a = _FakeNode("a", parent=container, kind="sop")
        sop_b = _FakeNode("b", parent=container, kind="sop")
        hou.add_node("/obj/geo1/a", sop_a)
        hou.add_node("/obj/geo1/b", sop_b)

        server = _StubServer(hou)
        result = _CONNECT_NODES(
            server,
            from_path="/obj/geo1/a",
            to_path="/obj/geo1/b",
            input_index=0,
            output_index=0,
        )

        # Same-parent path: 3-arg setInput preserved, no via marker
        self.assertNotIn("via", result)
        self.assertEqual(sop_b.set_input_calls, [(0, sop_a, 0)])
        # Display flags NOT touched on src (same-parent setInput path)
        self.assertEqual(sop_a.set_display_flag_calls, [])
        self.assertEqual(sop_a.set_render_flag_calls, [])


if __name__ == "__main__":
    unittest.main()
