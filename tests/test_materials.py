"""Unit tests for external/houdinimcp/_materials.py (PR 7).

Stdlib unittest, no hython required. hou is mocked via a small stub class.
Tests cover:
    - create_material: default name / custom name / custom parent / params /
      missing parm skip / returned dict keys
    - assign_material: normal / group optional / missing geo -> ValueError /
      missing material -> ValueError
    - get_material_info: path/type/name / known parm present / texture
      detection (.png yes, .txt no) / multiple texture refs
    - texture extension coverage: .png / .jpg / .jpeg / .exr / .hdr /
      .tif / .tiff / .rat / .tex
Run with:
    python -m unittest tests.test_materials tests.test_discovery \
        tests.test_scene tests.test_execute_code_safety tests.test_common -v
"""
import os
import sys
import unittest
import importlib.util as _ilu
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Build a synthetic "houdinimcp" package so the
# production-style `from . import _common as cmn` inside _materials.py resolves.
pkg = types.ModuleType("houdinimcp")
pkg.__path__ = [ROOT]
sys.modules["houdinimcp"] = pkg

_spec_common = _ilu.spec_from_file_location("houdinimcp._common",
                                            os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec_common)
sys.modules["houdinimcp._common"] = common
_spec_common.loader.exec_module(common)
cmn = common

_spec_materials = _ilu.spec_from_file_location(
    "houdinimcp._materials", os.path.join(ROOT, "_materials.py"))
materials = _ilu.module_from_spec(_spec_materials)
sys.modules["houdinimcp._materials"] = materials
_spec_materials.loader.exec_module(materials)
mat_mod = materials


# ---------------------------------------------------------------------------
# hou stubs
# ---------------------------------------------------------------------------
class _FakeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeNodeType(object):
    def __init__(self, name, category_name="Object"):
        self._name = name
        self._category = _FakeCategory(category_name)

    def name(self):
        return self._name

    def category(self):
        return self._category


class _FakeParm(object):
    """Mutable parameter carrying an eval() value and supporting set()."""

    def __init__(self, name, value):
        self._name = name
        self._value = value
        self._set_calls = []  # track set() calls

    def name(self):
        return self._name

    def eval(self):
        return self._value

    def set(self, value):
        self._value = value
        self._set_calls.append(value)


class _FakeMaterialNode(object):
    """Just enough surface for _materials: path/name/type/parm/parms/createNode."""

    def __init__(self, name, node_type_name="principledshader",
                 category_name="Sop", parent=None, parms=None,
                 hou_ref=None):
        self._name = name
        self._type = _FakeNodeType(node_type_name, category_name)
        self._parent = parent
        self._hou = hou_ref  # back-ref to _FakeHou for node registry
        self._parm_dict = {}
        self._children = []
        self._created = []  # children created via createNode
        if parms:
            for p in parms:
                self._parm_dict[p.name()] = p

    # identity
    def name(self):
        return self._name

    def path(self):
        if self._parent is None:
            return "/" + self._name
        return self._parent.path().rstrip("/") + "/" + self._name

    def type(self):
        return self._type

    # parms
    def parm(self, name):
        return self._parm_dict.get(name)

    def parms(self):
        return list(self._parm_dict.values())

    # create child material node (returns a new _FakeMaterialNode attached)
    def createNode(self, node_type, node_name=None):
        actual_name = node_name if node_name else node_type + "_auto"
        child = _FakeMaterialNode(actual_name, node_type_name=node_type,
                                   category_name="Sop", parent=self,
                                   hou_ref=self._hou)
        # Default: principledshader exposes a small set of whitelist parms
        # so tests can evaluate presence / texture detection.
        if node_type == "principledshader":
            child._parm_dict = {
                "basecolor": _FakeParm("basecolor", (0.5, 0.5, 0.5)),
                "rough": _FakeParm("rough", 0.3),
                "metallic": _FakeParm("metallic", 0.0),
                "ior": _FakeParm("ior", 1.5),
                "basecolor_texture": _FakeParm("basecolor_texture", ""),
                "rough_texture": _FakeParm("rough_texture", ""),
                "metallic_texture": _FakeParm("metallic_texture", ""),
            }
        self._children.append(child)
        self._created.append(child)
        # Register in the hou node registry so hou.node(path) can find it.
        if self._hou is not None:
            self._hou._nodes[child.path()] = child
        return child


class _FakeHou(object):
    """Holds a registry of named top-level material containers."""

    def __init__(self):
        self._nodes = {}
        # Default /mat container pre-populated. hou_ref is set after both
        # nodes are created so the back-reference forms an unambiguous graph.
        self._mat = _FakeMaterialNode("mat", node_type_name="mat",
                                      category_name="Sop")
        self._obj = _FakeMaterialNode("obj", node_type_name="obj",
                                      category_name="Object")
        self._mat._hou = self
        self._obj._hou = self
        self._nodes["/mat"] = self._mat
        self._nodes["/obj"] = self._obj

    def node(self, path):
        if path is None:
            return None
        if path == "/":
            return None  # not used by _materials
        return self._nodes.get(path)

    def add_node(self, path, node):
        """Helper for tests to register extra material paths."""
        node._hou = self
        self._nodes[path] = node

    def register_mat(self, node):
        """Register a node into /mat pre-population (for fixture building)."""
        node._hou = self
        self._nodes[node.path()] = node


def _make_hou():
    return _FakeHou()


# ===========================================================================
# Section A: create_material
# ===========================================================================
class CreateMaterialTests(unittest.TestCase):
    def test_default_name_returns_auto_named(self):
        hou = _make_hou()
        result = mat_mod.create_material(hou, "principledshader")
        self.assertIn("path", result)
        self.assertIn("type", result)
        self.assertIn("name", result)
        # default name should be non-empty
        self.assertTrue(result["name"])
        self.assertEqual(result["type"], "principledshader")

    def test_custom_name(self):
        hou = _make_hou()
        result = mat_mod.create_material(hou, "principledshader", name="myMat")
        self.assertEqual(result["name"], "myMat")
        self.assertEqual(result["path"], "/mat/myMat")
        self.assertEqual(result["type"], "principledshader")

    def test_custom_parent_path(self):
        hou = _make_hou()
        # Build a custom /shop container
        shop = _FakeMaterialNode("shop", node_type_name="shop",
                                 category_name="Sop")
        hou.add_node("/shop", shop)
        result = mat_mod.create_material(hou, "principledshader",
                                         name="gold",
                                         parent_path="/shop")
        self.assertEqual(result["path"], "/shop/gold")

    def test_default_parent_falls_back_to_mat(self):
        """If parent_path doesn't exist, fallback to /mat."""
        hou = _make_hou()
        result = mat_mod.create_material(hou, "principledshader",
                                         name="fallbackMat",
                                         parent_path="/nonexistent")
        # Falls back to /mat
        self.assertEqual(result["path"], "/mat/fallbackMat")

    def test_parameters_applied(self):
        hou = _make_hou()
        result = mat_mod.create_material(
            hou, "principledshader", name="parmMat",
            parameters={"rough": 0.42, "metallic": 0.9})
        self.assertIn("parameters_set", result)
        self.assertIn("rough", result["parameters_set"])
        self.assertIn("metallic", result["parameters_set"])

    def test_parameters_missing_parm_skipped_no_error(self):
        """A non-existent parm name must be skipped silently, not raise."""
        hou = _make_hou()
        result = mat_mod.create_material(
            hou, "principledshader", name="skipMat",
            parameters={"does_not_exist": 1.0, "rough": 0.5})
        # Existing one applied; missing one appears in parameters_set but is
        # silently skipped — verify no exception and known parm took effect.
        self.assertIn("rough", result["parameters_set"])
        self.assertIn("does_not_exist", result["parameters_set"])

    def test_empty_parameters_no_error(self):
        hou = _make_hou()
        result = mat_mod.create_material(hou, "principledshader",
                                         name="emptyP", parameters={})
        self.assertEqual(result["parameters_set"], [])

    def test_returns_dict_with_required_keys(self):
        hou = _make_hou()
        result = mat_mod.create_material(hou, "principledshader",
                                         name="keyMat")
        for k in ("path", "type", "name"):
            self.assertIn(k, result)


# ===========================================================================
# Section B: assign_material
# ===========================================================================
class AssignMaterialTests(unittest.TestCase):
    def test_normal_assignment(self):
        hou = _make_hou()
        # Create a geometry node with shop_materialpath
        geo = _FakeMaterialNode("myGeo", node_type_name="geo",
                                category_name="Sop")
        geo._parm_dict["shop_materialpath"] = _FakeParm(
            "shop_materialpath", "")
        geo._parent = hou.node("/obj")
        hou.node("/obj")._children.append(geo)
        hou.add_node("/obj/myGeo", geo)

        # Create a material to assign
        mat = hou.node("/mat").createNode("principledshader", "skinMat")
        result = mat_mod.assign_material(hou, "/obj/myGeo", mat.path())
        self.assertTrue(result["success"])
        self.assertEqual(result["geometry_path"], "/obj/myGeo")
        self.assertEqual(result["material_path"], mat.path())
        # parm should have been set to material path
        self.assertEqual(geo._parm_dict["shop_materialpath"]._value,
                         mat.path())

    def test_group_optional(self):
        hou = _make_hou()
        geo = _FakeMaterialNode("grpGeo", node_type_name="geo",
                                category_name="Sop")
        geo._parm_dict["shop_materialpath"] = _FakeParm(
            "shop_materialpath", "")
        geo._parent = hou.node("/obj")
        hou.node("/obj")._children.append(geo)
        hou.add_node("/obj/grpGeo", geo)

        mat = hou.node("/mat").createNode("principledshader", "layerMat")
        result = mat_mod.assign_material(hou, "/obj/grpGeo",
                                          mat.path(), group="piece1")
        self.assertTrue(result["success"])
        self.assertEqual(result["group"], "piece1")

    def test_group_none_default(self):
        hou = _make_hou()
        geo = _FakeMaterialNode("noneGeo", node_type_name="geo",
                                category_name="Sop")
        geo._parm_dict["shop_materialpath"] = _FakeParm(
            "shop_materialpath", "")
        geo._parent = hou.node("/obj")
        hou.node("/obj")._children.append(geo)
        hou.add_node("/obj/noneGeo", geo)

        mat = hou.node("/mat").createNode("principledshader", "skinMat")
        result = mat_mod.assign_material(hou, "/obj/noneGeo", mat.path())
        # Default group must be None (or empty string but stable; spec: None)
        self.assertIsNone(result["group"])

    def test_geometry_missing_raises_value_error(self):
        hou = _make_hou()
        # Create a material only
        mat = hou.node("/mat").createNode("principledshader", "x")
        with self.assertRaises(ValueError) as ctx:
            mat_mod.assign_material(hou, "/obj/nonexistent", mat.path())
        # The message should mention geometry (or geo)
        msg = str(ctx.exception)
        self.assertTrue("nonexistent" in msg or "\u51e0" in msg
                        or "geometry" in msg.lower()
                        or "geo" in msg.lower())

    def test_material_missing_raises_value_error(self):
        hou = _make_hou()
        geo = _FakeMaterialNode("validGeo", node_type_name="geo",
                                category_name="Sop")
        geo._parm_dict["shop_materialpath"] = _FakeParm(
            "shop_materialpath", "")
        geo._parent = hou.node("/obj")
        hou.node("/obj")._children.append(geo)
        hou.add_node("/obj/validGeo", geo)

        with self.assertRaises(ValueError):
            mat_mod.assign_material(hou, "/obj/validGeo", "/mat/noSuchMat")


# ===========================================================================
# Section C: get_material_info
# ===========================================================================
class GetMaterialInfoTests(unittest.TestCase):
    def _make_material(self):
        hou = _make_hou()
        return hou, hou.node("/mat").createNode("principledshader", "infoMat")

    def test_returns_path_type_name(self):
        hou, mat = self._make_material()
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertEqual(info["path"], mat.path())
        self.assertEqual(info["name"], "infoMat")
        self.assertEqual(info["type"], "principledshader")

    def test_includes_known_parm_basecolor(self):
        hou, mat = self._make_material()
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertIn("parameters", info)
        # basecolor / rough / metallic are in the whitelist AND in fake node
        self.assertIn("basecolor", info["parameters"])
        self.assertIn("rough", info["parameters"])
        self.assertIn("metallic", info["parameters"])

    def test_texture_recognition_png(self):
        hou, mat = self._make_material()
        # Set basecolor_texture to a .png path
        mat.parm("basecolor_texture").set("/textures/skin.png")
        info = mat_mod.get_material_info(hou, mat.path())
        tex = info.get("texture_references", [])
        png_refs = [t for t in tex if t.get("parm") == "basecolor_texture"]
        self.assertTrue(png_refs,
                        "basecolor_texture .png must be classified as a "
                        "texture reference")
        self.assertTrue(png_refs[0].get("is_texture"))
        self.assertEqual(png_refs[0]["value"], "/textures/skin.png")

    def test_texture_recognition_txt_not_texture(self):
        hou, mat = self._make_material()
        # /textures/notes.txt must NOT be recognized as a texture
        mat.parm("basecolor_texture").set("/textures/notes.txt")
        info = mat_mod.get_material_info(hou, mat.path())
        tex = info.get("texture_references", [])
        png_refs = [t for t in tex if t.get("parm") == "basecolor_texture"]
        self.assertEqual(png_refs, [],
                         ".txt value must not be classified as texture")

    def test_multiple_texture_references(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/base.png")
        mat.parm("rough_texture").set("/tex/r.exr")
        info = mat_mod.get_material_info(hou, mat.path())
        tex = info.get("texture_references", [])
        parms = sorted(t["parm"] for t in tex)
        self.assertIn("basecolor_texture", parms)
        self.assertIn("rough_texture", parms)
        self.assertEqual(len(tex), 2)

    def test_texture_extensions_png(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/a.png")
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertTrue(any(t.get("is_texture")
                            for t in info["texture_references"]))

    def test_texture_extensions_jpg(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/a.jpg")
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertTrue(any(t.get("is_texture")
                            for t in info["texture_references"]))

    def test_texture_extensions_jpeg(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/a.jpeg")
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertTrue(any(t.get("is_texture")
                            for t in info["texture_references"]))

    def test_texture_extensions_exr(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/a.exr")
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertTrue(any(t.get("is_texture")
                            for t in info["texture_references"]))

    def test_texture_extensions_hdr(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/a.hdr")
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertTrue(any(t.get("is_texture")
                            for t in info["texture_references"]))

    def test_texture_extensions_tif(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/a.tif")
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertTrue(any(t.get("is_texture")
                            for t in info["texture_references"]))

    def test_texture_extensions_tiff(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/a.tiff")
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertTrue(any(t.get("is_texture")
                            for t in info["texture_references"]))

    def test_texture_extensions_rat(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/a.rat")
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertTrue(any(t.get("is_texture")
                            for t in info["texture_references"]))

    def test_texture_extensions_tex(self):
        hou, mat = self._make_material()
        mat.parm("basecolor_texture").set("/tex/a.tex")
        info = mat_mod.get_material_info(hou, mat.path())
        self.assertTrue(any(t.get("is_texture")
                            for t in info["texture_references"]))

    def test_missing_material_raises_value_error(self):
        hou = _make_hou()
        with self.assertRaises(ValueError):
            mat_mod.get_material_info(hou, "/mat/doesnotexist")


# ===========================================================================
# Section D: texture classification helper
# ===========================================================================
class IsTextureReferenceTests(unittest.TestCase):
    """PR 7 brief: texture detection via filename extension.
    Extension list: .png .jpg .jpeg .exr .hdr .tif .tiff .rat .tex
    """

    def test_png(self):
        self.assertTrue(mat_mod.is_texture_reference("a.png"))

    def test_jpg(self):
        self.assertTrue(mat_mod.is_texture_reference("a.jpg"))

    def test_jpeg(self):
        self.assertTrue(mat_mod.is_texture_reference("a.jpeg"))

    def test_exr(self):
        self.assertTrue(mat_mod.is_texture_reference("a.exr"))

    def test_hdr(self):
        self.assertTrue(mat_mod.is_texture_reference("a.hdr"))

    def test_tif(self):
        self.assertTrue(mat_mod.is_texture_reference("a.tif"))

    def test_tiff(self):
        self.assertTrue(mat_mod.is_texture_reference("a.tiff"))

    def test_rat(self):
        self.assertTrue(mat_mod.is_texture_reference("a.rat"))

    def test_tex(self):
        self.assertTrue(mat_mod.is_texture_reference("a.tex"))

    def test_uppercase_extension_still_detected(self):
        """Some pipelines write .PNG or .EXR. Case-insensitive match."""
        self.assertTrue(mat_mod.is_texture_reference("a.PNG"))
        self.assertTrue(mat_mod.is_texture_reference("a.EXR"))

    def test_full_path_with_texture_extension(self):
        self.assertTrue(mat_mod.is_texture_reference("/foo/bar/a.png"))

    def test_not_texture_txt(self):
        self.assertFalse(mat_mod.is_texture_reference("readme.txt"))

    def test_not_texture_no_extension(self):
        self.assertFalse(mat_mod.is_texture_reference("myfile"))

    def test_not_texture_none(self):
        self.assertFalse(mat_mod.is_texture_reference(None))

    def test_not_texture_empty_string(self):
        self.assertFalse(mat_mod.is_texture_reference(""))


# ===========================================================================
# Section E: whitelist size (50+)
# ===========================================================================
class WhitelistTests(unittest.TestCase):
    def test_whitelist_has_at_least_50_entries(self):
        wl = mat_mod.MATERIAL_PARM_WHITELIST
        self.assertIsInstance(wl, (list, tuple, set, frozenset))
        # PR 7 brief: "50+ 参数白名单"
        self.assertGreaterEqual(len(wl), 50,
                                "Whitelist must have 50+ entries (got {0})"
                                .format(len(wl)))


if __name__ == "__main__":
    unittest.main()
