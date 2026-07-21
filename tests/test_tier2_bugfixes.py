"""Unit tests for the 3 fork tier-2 bugfixes (F-A / F-B / F-C).

Stdlib unittest, no hython required. hou is mocked via small stub classes
(reused from the conftest stub + per-test fake nodes).

Covers:
    F-A: _node_info.get_node_info OBJ-level isCooking hasattr guard
        - SOP-style fake node (has isCooking) returns its bool value
        - OBJ-style fake node (no isCooking attribute) returns None,
          does not raise
        - Full get_node_info call path on OBJ-style node succeeds

    F-B: _materials.MATERIAL_PARM_WHITELIST H21+ triplet extension
        - principledshader 1.0 fake exposes top-level 'basecolor' tuple
          → get_material_info returns 'basecolor' in parameters
        - principledshader 2.0 fake exposes basecolorr/g/b sub-keys
          → get_material_info returns all three with matching values
        - The whitelist membership check passes for the new sub-keys

    F-C: server.HoudiniMCPServer.connect_nodes cross-parent OBJ-display
        smart branch
        - SOP child → OBJ parent (cross-parent) routes to src.setDisplayFlag
          + src.setRenderFlag (B2 H21 compat: dst.setInput hangs 30s+ on
          H21), returns via="sop_display_flag", does NOT call dst.setInput
        - Same-parent connect still uses the legacy 3-arg setInput form
        - Cross-parent non-ancestor (sibling SOPs) still raises ValueError

Run with:
    python -m unittest tests.test_tier2_bugfixes -v
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
# `from . import _common as cmn` inside _node_info.py / _materials.py
# resolves, the same pattern as test_node_info.py / test_materials.py.
pkg = types.ModuleType("houdinimcp")
pkg.__path__ = [ROOT]
sys.modules["houdinimcp"] = pkg


def _load_module(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cmn = _load_module("houdinimcp._common", os.path.join(ROOT, "_common.py"))
ni = _load_module("houdinimcp._node_info",
                  os.path.join(ROOT, "_node_info.py"))
mat_mod = _load_module("houdinimcp._materials",
                       os.path.join(ROOT, "_materials.py"))


# ---------------------------------------------------------------------------
# F-A fake nodes: minimal _node_info surface, with optional isCooking.
# ---------------------------------------------------------------------------
class _FA_FakeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FA_FakeNodeType(object):
    def __init__(self, name, category_name="Sop"):
        self._name = name
        self._category = _FA_FakeCategory(category_name)

    def name(self):
        return self._name

    def category(self):
        return self._category


class _FA_FakeNode(object):
    """Stub supporting get_node_info. isCooking attribute is OPTIONAL:
    only attach it when the test wants to simulate a SOP-style node that
    has cook-state semantics; OBJ-level nodes omit it."""

    def __init__(self, name, type_name="geo", category="Sop", parent=None,
                 children=None, parms=None, inputs=None, outputs=None,
                 errors=None, warnings=None,
                 has_iscooking=False, is_cooking=False,
                 cook_state=None, needs_to_cook=False):
        self._name = name
        self._type = _FA_FakeNodeType(type_name, category)
        self._parent = parent
        self._children = list(children) if children else []
        self._parms = list(parms) if parms else []
        self._inputs = list(inputs) if inputs else []
        self._outputs = list(outputs) if outputs else []
        self._errors = list(errors) if errors else []
        self._warnings = list(warnings) if warnings else []
        if cook_state is not None:
            self.cookState = lambda: cook_state
        self.needsToCook = lambda: bool(needs_to_cook)
        # F-A key behavior: only attach isCooking when has_iscooking=True.
        # OBJ-level nodes (has_iscooking=False) intentionally LACK the
        # attribute so get_node_info's hasattr guard returns None.
        if has_iscooking:
            self.isCooking = lambda: bool(is_cooking)

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

    def position(self):
        return (0.0, 0.0)


class _FA_FakeHou(object):
    def __init__(self, nodes=None):
        self._nodes = dict(nodes) if nodes else {}

    def add_node(self, path, node):
        self._nodes[path] = node
        node._path = path

    def node(self, path):
        return self._nodes.get(path)


# ===========================================================================
# Section A — F-A: OBJ-level isCooking guard
# ===========================================================================
class F_A_OBJ_level_guard(unittest.TestCase):
    """F-A: get_node_info must not call node.isCooking() on nodes that
    lack the attribute (e.g. hou.ObjNode in H21+). hasattr guard returns
    None instead of raising AttributeError."""

    def test_sop_node_returns_is_cooking_value(self):
        """SOP-style fake node WITH isCooking attribute: result preserves
        its boolean value (no regression on the existing happy path)."""
        node = _FA_FakeNode(
            "box", type_name="geo", category="Sop",
            has_iscooking=True, is_cooking=True,
            cook_state="Cooked", needs_to_cook=False,
        )
        hou = _FA_FakeHou()
        hou.add_node("/obj/box", node)

        result = ni.get_node_info(hou, "/obj/box")

        self.assertEqual(result["is_cooking"], True)

    def test_sop_node_returns_is_cooking_false(self):
        """SOP-style fake node with isCooking returning False is preserved."""
        node = _FA_FakeNode(
            "box", type_name="geo", category="Sop",
            has_iscooking=True, is_cooking=False,
            cook_state="Cooked", needs_to_cook=False,
        )
        hou = _FA_FakeHou()
        hou.add_node("/obj/box", node)

        result = ni.get_node_info(hou, "/obj/box")

        self.assertEqual(result["is_cooking"], False)

    def test_obj_node_returns_none_for_is_cooking(self):
        """OBJ-level fake node WITHOUT isCooking attribute: result returns
        None for is_cooking, no exception raised."""
        node = _FA_FakeNode(
            "table_demo", type_name="geo", category="Object",
            has_iscooking=False,
            cook_state="Cooked", needs_to_cook=False,
        )
        hou = _FA_FakeHou()
        hou.add_node("/obj/table_demo", node)

        result = ni.get_node_info(hou, "/obj/table_demo")

        # F-A contract: missing isCooking -> None (not AttributeError).
        self.assertIsNone(result["is_cooking"])
        # Other fields still populated normally
        self.assertEqual(result["path"], "/obj/table_demo")
        self.assertEqual(result["type"], "geo")
        self.assertEqual(result["category"], "Object")

    def test_obj_node_get_node_info_does_not_raise(self):
        """Full call path through get_node_info with an OBJ-style fake
        node (no isCooking) returns a dict, never raises."""
        node = _FA_FakeNode(
            "table_demo", type_name="geo", category="Object",
            has_iscooking=False,
            cook_state="Cooked", needs_to_cook=False,
        )
        hou = _FA_FakeHou()
        hou.add_node("/obj/table_demo", node)

        try:
            result = ni.get_node_info(hou, "/obj/table_demo")
        except AttributeError as exc:
            self.fail(
                "get_node_info raised AttributeError on OBJ-level node "
                "(F-A fix missing): {0!r}".format(exc))
        except Exception as exc:
            self.fail(
                "get_node_info raised unexpected exception: {0!r}"
                .format(exc))

        self.assertIsInstance(result, dict)
        self.assertIn("is_cooking", result)
        self.assertIsNone(result["is_cooking"])


# ===========================================================================
# Section B — F-B: MATERIAL_PARM_WHITELIST H21+ triplet extension
# ===========================================================================
class _FB_FakeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FB_FakeNodeType(object):
    def __init__(self, name, category_name="Object"):
        self._name = name
        self._category = _FB_FakeCategory(category_name)

    def name(self):
        return self._name

    def category(self):
        return self._category


class _FB_FakeParm(object):
    def __init__(self, name, value):
        self._name = name
        self._value = value

    def name(self):
        return self._name

    def eval(self):
        return self._value


class _FB_FakeMaterialNode(object):
    """F-B fake node: supports both principledshader 1.0 (basecolor
    3-tuple) and 2.0 (basecolorr/g/b triplet) schemas via constructor
    flag. Keeps get_material_info's path() / type() / name() / parms()
    surface intact."""

    def __init__(self, name, parent=None, schema="v1"):
        self._name = name
        self._type = _FB_FakeNodeType("principledshader", "Object")
        self._parent = parent
        self._parm_dict = {}
        if schema == "v1":
            # H20 principledshader 1.0: basecolor 3-tuple
            self._parm_dict["basecolor"] = _FB_FakeParm(
                "basecolor", (0.8, 0.4, 0.2))
        elif schema == "v2":
            # H21+ principledshader 2.0: r/g/b sibling sub-keys
            self._parm_dict["basecolorr"] = _FB_FakeParm("basecolorr", 0.8)
            self._parm_dict["basecolorg"] = _FB_FakeParm("basecolorg", 0.4)
            self._parm_dict["basecolorb"] = _FB_FakeParm("basecolorb", 0.2)
        else:
            raise ValueError("Unknown schema: {0}".format(schema))

    def name(self):
        return self._name

    def path(self):
        if self._parent is None:
            return "/" + self._name
        return self._parent.path().rstrip("/") + "/" + self._name

    def type(self):
        return self._type

    def parm(self, name):
        return self._parm_dict.get(name)

    def parms(self):
        return list(self._parm_dict.values())


class _FB_FakeHou(object):
    """Minimal hou registry; root /mat contains a single material."""

    def __init__(self):
        self._mat = _FB_FakeMaterialNode("mat")
        self._nodes = {"/mat": self._mat}

    def node(self, path):
        return self._nodes.get(path)

    def add_material(self, name, schema="v1"):
        mat = _FB_FakeMaterialNode(name, parent=self._mat, schema=schema)
        self._nodes[mat.path()] = mat
        return mat


class F_B_principledshader_v2_readback(unittest.TestCase):
    """F-B: get_material_info must expose H21+ basecolorr/g/b triplet
    sub-keys in addition to the H20 basecolor singleton. Whitelist is
    key-additive."""

    def test_v1_basecolor_tuple_visible(self):
        """principledshader 1.0 with basecolor 3-tuple → parameters dict
        contains basecolor key (regression: no change for H20 schema)."""
        hou = _FB_FakeHou()
        mat = hou.add_material("v1Mat", schema="v1")

        info = mat_mod.get_material_info(hou, mat.path())

        self.assertIn("basecolor", info["parameters"])
        # The 3-tuple is preserved via _json_safe_hou_value
        self.assertEqual(info["parameters"]["basecolor"], [0.8, 0.4, 0.2])

    def test_v2_basecolor_subkeys_visible(self):
        """principledshader 2.0 with basecolorr/g/b sub-keys (no top-level
        basecolor) → parameters dict contains basecolorr/g/b with values
        matching the input fixture."""
        hou = _FB_FakeHou()
        mat = hou.add_material("v2Mat", schema="v2")

        info = mat_mod.get_material_info(hou, mat.path())

        # All three sub-keys present
        self.assertIn("basecolorr", info["parameters"])
        self.assertIn("basecolorg", info["parameters"])
        self.assertIn("basecolorb", info["parameters"])
        # Values match the input fixture exactly
        self.assertEqual(info["parameters"]["basecolorr"], 0.8)
        self.assertEqual(info["parameters"]["basecolorg"], 0.4)
        self.assertEqual(info["parameters"]["basecolorb"], 0.2)
        # The v1 basecolor singleton is NOT synthesized for v2 fixture
        # (F-B contract: key-additive, no parent key synthesis)
        self.assertNotIn("basecolor", info["parameters"])

    def test_v2_basecolor_subkeys_added_to_whitelist(self):
        """Direct membership check: the new H21+ sub-keys are part of
        the whitelist tuple."""
        whitelist_set = set(mat_mod.MATERIAL_PARM_WHITELIST)
        for key in ("basecolorr", "basecolorg", "basecolorb"):
            self.assertIn(
                key, whitelist_set,
                "Whitelist missing F-B key {0!r}".format(key))

    def test_v2_other_h21_triplet_subkeys_in_whitelist(self):
        """All 21 H21+ sub-keys per design.md §2 are present in whitelist."""
        expected_subkeys = {
            # basecolor
            "basecolorr", "basecolorg", "basecolorb",
            # emitcolor
            "emitcolorr", "emitcolorg", "emitcolorb",
            # sheen_color → sheenr/g/b
            "sheenr", "sheeng", "sheenb",
            # coat_color
            "coat_colorr", "coat_colorg", "coat_colorb",
            # sss_color
            "sssr", "sssg", "sssb",
            # scattering_color
            "scattering_colorr", "scattering_colorg", "scattering_colorb",
            # diffuse
            "diffuser", "diffuseg", "diffuseb",
        }
        whitelist_set = set(mat_mod.MATERIAL_PARM_WHITELIST)
        missing = expected_subkeys - whitelist_set
        self.assertEqual(
            missing, set(),
            "Whitelist missing H21+ sub-keys per design.md §2: {0}"
            .format(sorted(missing)))

    def test_v1_singletons_preserved_for_backward_compat(self):
        """F-B is key-additive: every H20 singleton must still be in the
        whitelist (no regression on existing keys)."""
        whitelist_set = set(mat_mod.MATERIAL_PARM_WHITELIST)
        for singleton in ("basecolor", "emitcolor", "emitColor",
                           "sheen_color", "coat_color",
                           "sss_color", "scattering_color", "diffuse"):
            self.assertIn(
                singleton, whitelist_set,
                "H20 singleton {0!r} missing from whitelist "
                "(F-B regression)".format(singleton))


# ===========================================================================
# Section C — F-C/B2: connect_nodes cross-parent OBJ-display smart branch
# ===========================================================================
class _FC_FakeConnector(object):
    """Mimics hou.NodeConnection for inputConnectors()."""

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


class _FC_FakeNode(object):
    """Minimal graph node stub for F-C. Captures setInput calls so tests
    can assert which signature was used (2-arg cross-parent vs 3-arg
    same-parent).

    ``kind`` selects which Houdini-role-specific APIs are exposed:
        - "sop"  → has setDisplayFlag / setRenderFlag (hou.SopNode API)
        - "obj"  → no SOP-specific APIs (hou.ObjNode container)

    SOP nodes capture setDisplayFlag / setRenderFlag calls in
    ``set_display_flag_calls`` / ``set_render_flag_calls`` so the B2
    regression tests can assert the H21 display-flag path was taken
    instead of dst.setInput.
    """

    def __init__(self, name, parent=None, kind="obj"):
        self._name = name
        self._parent = parent
        self._path = None
        self._children = []
        self._inputs = {}
        self.set_input_calls = []
        self.set_display_flag_calls = []
        self.set_render_flag_calls = []
        # SOP-kind nodes expose the hou.SopNode display/render flag API.
        # OBJ-kind (default) nodes do not — mirroring the real hou class
        # hierarchy where only SopNode has setDisplayFlag.
        if kind == "sop":
            self.setDisplayFlag = self._setDisplayFlag
            self.setRenderFlag = self._setRenderFlag

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

    def children(self):
        return list(self._children)

    def _setDisplayFlag(self, value):
        self.set_display_flag_calls.append(value)

    def _setRenderFlag(self, value):
        self.set_render_flag_calls.append(value)

    def setInput(self, *args):
        """Capture all call signatures (2-arg cross-parent and
        3-arg same-parent)."""
        self.set_input_calls.append(tuple(args))
        # 2-arg form: (input_index, src) — input_index stored as-is
        # 3-arg form: (input_index, src, output_index)
        if len(args) == 2:
            input_index, src = args
            if src is None:
                self._inputs.pop(input_index, None)
            else:
                self._inputs[input_index] = (src, 0)
        elif len(args) == 3:
            input_index, src, output_index = args
            if src is None:
                self._inputs.pop(input_index, None)
            else:
                self._inputs[input_index] = (src, output_index)

    def inputConnectors(self):
        return tuple(
            _FC_FakeConnector(idx, src, oidx)
            for idx, (src, oidx) in sorted(self._inputs.items())
        )


class _FC_FakeHou(object):
    """Registry of named nodes for F-C tests."""

    def __init__(self):
        self._nodes = {}

    def add_node(self, path, node):
        self._nodes[path] = node
        node._path = path
        if node._parent is not None:
            node._parent._children.append(node)

    def node(self, path):
        return self._nodes.get(path)


def _fc_resolve_node(self, path):
    """Replacement for HoudiniMCPServer._resolve_node that returns a fake
    from the registry."""
    node = self._fc_hou.node(path)
    if not node:
        raise ValueError("Node not found: {0}".format(path))
    return node


class _FC_ServerLike(object):
    """A minimal stand-in for HoudiniMCPServer that holds a registry and
    exposes _resolve_node + connect_nodes (the latter defined by AST
    extraction from server.py)."""

    # Bind as a method so the AST-extracted connect_nodes can call
    # self._resolve_node(...) the same way as the production server.
    _resolve_node = _fc_resolve_node

    def __init__(self, hou):
        self._fc_hou = hou


def _fc_load_connect_nodes():
    """Extract the connect_nodes method body from server.py via AST and
    return a Python callable bound to _FC_ServerLike. Avoids loading
    server.py at import time (which would pull PySide6 / requests / etc.)
    by AST-extracting just the function."""
    server_path = os.path.join(ROOT, "server.py")
    with open(server_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            if item.name != "connect_nodes":
                continue
            # Build a module containing the class def with only the
            # connect_nodes method.
            new_class = ast.ClassDef(
                name="_FC_StubServer",
                bases=[],
                keywords=[],
                body=[item],
                decorator_list=[],
            )
            module = ast.Module(body=[new_class], type_ignores=[])
            ast.fix_missing_locations(module)
            namespace = {"_fc_resolve_node": _fc_resolve_node}
            exec(compile(module, server_path, "exec"), namespace)
            cls = namespace["_FC_StubServer"]
            return cls.connect_nodes
    raise AssertionError(
        "connect_nodes method not found in server.py")


_FC_CONNECT_NODES = _fc_load_connect_nodes()


def _fc_make_server(hou):
    server = _FC_ServerLike(hou)
    # Bind connect_nodes with self=server
    return server


class F_C_connect_nodes_cross_parent(unittest.TestCase):
    """F-C/B2: connect_nodes must accept cross-parent SOP child → OBJ parent
    wiring via the H21 display-flag path (src.setDisplayFlag +
    src.setRenderFlag; dst.setInput hangs 30s+ on H21), preserve
    same-parent behavior, and still reject cross-network non-ancestor
    pairings."""

    def test_cross_parent_sop_child_to_obj_parent_succeeds(self):
        """Cross-parent SOP child → OBJ parent routes to the B2 H21
        display-flag path (src.setDisplayFlag(True) + src.setRenderFlag(True)),
        returns via="sop_display_flag", and does NOT call dst.setInput
        (which would hang 30s on H21)."""
        hou = _FC_FakeHou()
        # /obj container
        obj_root = _FC_FakeNode("obj")
        hou.add_node("/obj", obj_root)
        # /obj/table_demo (OBJ container, default kind="obj")
        obj_container = _FC_FakeNode("table_demo", parent=obj_root)
        hou.add_node("/obj/table_demo", obj_container)
        # /obj/table_demo/wood_grain (SOP child)
        sop_child = _FC_FakeNode("wood_grain", parent=obj_container, kind="sop")
        hou.add_node("/obj/table_demo/wood_grain", sop_child)

        server = _fc_make_server(hou)
        result = _FC_CONNECT_NODES(
            server, "/obj/table_demo/wood_grain",
            "/obj/table_demo", input_index=0, output_index=0)

        # Result reflects B2 display-flag path
        self.assertEqual(result.get("via"), "sop_display_flag")
        self.assertEqual(result["from"], "/obj/table_demo/wood_grain")
        self.assertEqual(result["to"], "/obj/table_demo")
        self.assertEqual(result["input_index"], 0)
        self.assertEqual(result["output_index"], 0)
        # SOP display + render flag set on src — NOT dst.setInput
        self.assertEqual(sop_child.set_display_flag_calls, [True])
        self.assertEqual(sop_child.set_render_flag_calls, [True])
        self.assertEqual(obj_container.set_input_calls, [])

    def test_cross_parent_deeper_descendant_succeeds(self):
        """Cross-parent with a deeper SOP descendant (through a subnet)
        also routes to the B2 display-flag path."""
        hou = _FC_FakeHou()
        obj_root = _FC_FakeNode("obj")
        hou.add_node("/obj", obj_root)
        obj_container = _FC_FakeNode("table_demo", parent=obj_root)
        hou.add_node("/obj/table_demo", obj_container)
        subnet = _FC_FakeNode("subnet", parent=obj_container)
        hou.add_node("/obj/table_demo/subnet", subnet)
        leaf_sop = _FC_FakeNode("leaf_sop", parent=subnet, kind="sop")
        hou.add_node("/obj/table_demo/subnet/leaf_sop", leaf_sop)

        server = _fc_make_server(hou)
        result = _FC_CONNECT_NODES(
            server, "/obj/table_demo/subnet/leaf_sop",
            "/obj/table_demo", input_index=2, output_index=0)

        self.assertEqual(result.get("via"), "sop_display_flag")
        # SOP display + render flag set on src — NOT dst.setInput
        self.assertEqual(leaf_sop.set_display_flag_calls, [True])
        self.assertEqual(leaf_sop.set_render_flag_calls, [True])
        self.assertEqual(obj_container.set_input_calls, [])

    def test_same_parent_legacy_path_preserved(self):
        """Same-parent connect still uses the legacy 3-arg setInput
        form (no _cross_parent flag)."""
        hou = _FC_FakeHou()
        obj_root = _FC_FakeNode("obj")
        hou.add_node("/obj", obj_root)
        container = _FC_FakeNode("geo1", parent=obj_root)
        hou.add_node("/obj/geo1", container)
        a = _FC_FakeNode("a", parent=container)
        b = _FC_FakeNode("b", parent=container)
        hou.add_node("/obj/geo1/a", a)
        hou.add_node("/obj/geo1/b", b)

        server = _fc_make_server(hou)
        result = _FC_CONNECT_NODES(
            server, "/obj/geo1/a",
            "/obj/geo1/b", input_index=0, output_index=0)

        # Same-parent: no _cross_parent flag, 3-arg form preserved
        self.assertNotIn("_cross_parent", result)
        self.assertEqual(result["from"], "/obj/geo1/a")
        self.assertEqual(result["to"], "/obj/geo1/b")
        self.assertEqual(result["input_index"], 0)
        self.assertEqual(result["output_index"], 0)
        # 3-arg signature captured
        self.assertEqual(b.set_input_calls, [(0, a, 0)])

    def test_same_parent_input_index_two_preserved(self):
        """Same-parent with non-zero input_index / output_index must pass
        those args through verbatim on the 3-arg setInput form."""
        hou = _FC_FakeHou()
        obj_root = _FC_FakeNode("obj")
        hou.add_node("/obj", obj_root)
        container = _FC_FakeNode("geo1", parent=obj_root)
        hou.add_node("/obj/geo1", container)
        a = _FC_FakeNode("a", parent=container)
        b = _FC_FakeNode("b", parent=container)
        hou.add_node("/obj/geo1/a", a)
        hou.add_node("/obj/geo1/b", b)

        server = _fc_make_server(hou)
        _FC_CONNECT_NODES(
            server, "/obj/geo1/a",
            "/obj/geo1/b", input_index=2, output_index=1)

        self.assertEqual(b.set_input_calls, [(2, a, 1)])

    def test_cross_parent_sop_sop_still_raises(self):
        """Two nodes under different sibling networks (e.g.,
        /obj/geo1/a and /obj/geo2/a) — neither ancestor of the other,
        neither descendant of the other — still raise ValueError. The
        F-C branch must NOT fire for cross-network non-OBJ pairings."""
        hou = _FC_FakeHou()
        obj_root = _FC_FakeNode("obj")
        hou.add_node("/obj", obj_root)
        geo1 = _FC_FakeNode("geo1", parent=obj_root)
        geo2 = _FC_FakeNode("geo2", parent=obj_root)
        hou.add_node("/obj/geo1", geo1)
        hou.add_node("/obj/geo2", geo2)
        a = _FC_FakeNode("a", parent=geo1)
        b = _FC_FakeNode("a", parent=geo2)
        hou.add_node("/obj/geo1/a", a)
        hou.add_node("/obj/geo2/a", b)

        server = _fc_make_server(hou)
        with self.assertRaises(ValueError) as ctx:
            _FC_CONNECT_NODES(
                server, "/obj/geo1/a",
                "/obj/geo2/a", input_index=0, output_index=0)

        # Error message mentions "share a parent" / "parent network"
        msg = str(ctx.exception).lower()
        self.assertTrue(
            "share" in msg or "parent" in msg,
            "ValueError must mention parent network restriction: {0!r}"
            .format(msg))

    def test_cross_parent_unrelated_networks_still_raises(self):
        """Two unrelated networks (neither ancestor of the other) raise."""
        hou = _FC_FakeHou()
        obj_root = _FC_FakeNode("obj")
        hou.add_node("/obj", obj_root)
        geo1 = _FC_FakeNode("geo1", parent=obj_root)
        geo2 = _FC_FakeNode("geo2", parent=obj_root)
        hou.add_node("/obj/geo1", geo1)
        hou.add_node("/obj/geo2", geo2)
        a = _FC_FakeNode("a", parent=geo1)
        b = _FC_FakeNode("b", parent=geo2)
        hou.add_node("/obj/geo1/a", a)
        hou.add_node("/obj/geo2/b", b)

        server = _fc_make_server(hou)
        with self.assertRaises(ValueError):
            _FC_CONNECT_NODES(
                server, "/obj/geo1/a",
                "/obj/geo2/b", input_index=0, output_index=0)


if __name__ == "__main__":
    unittest.main()