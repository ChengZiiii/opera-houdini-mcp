"""Unit tests for external/houdinimcp/_geo_summary.py + PR 12 bridge tool.

Stdlib unittest, no hython required. hou is mocked via a small stub class.
Tests cover:
    - get_geo_summary: 默认参数 / counts / bbox / attributes / groups /
      sample_points / sample_size=0 / 大几何降级 / 精确 1M 边界 /
      OBJ 自动 resolve 到 displayNode / 节点不存在 -> ValueError /
      非几何节点 -> ValueError / bbox 全 None（空几何）
    - 桥接端 @mcp.tool() style AST 探针 (PR 12 自写，不修改 test_bridge_style.py)
    - send_command 参数 keys / _houdini_call 错误返回透传

Bridge style 由本文件内置 AST probe 验证 (不 import houdini_mcp_server.py,
因其有 mcp / requests / dotenv / langchain 等重依赖). test_bridge_style.py
仍是 PR 7 专用; 本测试不修改它. PR 12 工具放在 "# PR 12 Geometry Summary"
section header 之后，使 PR 7 探针继续返回 3 个 tool.

Run with:
    python -m unittest tests.test_geo_summary -v
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

_spec_gs = _ilu.spec_from_file_location(
    "houdinimcp._geo_summary", os.path.join(ROOT, "_geo_summary.py"))
geo_summary = _ilu.module_from_spec(_spec_gs)
sys.modules["houdinimcp._geo_summary"] = geo_summary
_spec_gs.loader.exec_module(geo_summary)
gs = geo_summary


# ---------------------------------------------------------------------------
# hou stub: enough surface area for the get_geo_summary implementation.
# ---------------------------------------------------------------------------
class _FakeAttribType(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeAttrib(object):
    def __init__(self, name, data_type="Float", size=3):
        self._name = name
        self._data_type = data_type
        self._size = size

    def name(self):
        return self._name

    def dataType(self):
        return _FakeAttribType(self._data_type)

    def size(self):
        return self._size


class _FakeGroupType(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeGroup(object):
    def __init__(self, name, group_type="Point", size=0):
        self._name = name
        # Houdini real group type names: 'Point' / 'Primitive' / 'Vertex'
        self._type = group_type
        self._size = size

    def name(self):
        return self._name

    def type(self):
        return _FakeGroupType(self._type)

    def __len__(self):
        return self._size


class _FakePoint(object):
    def __init__(self, number, attribs):
        # attribs: dict of name -> value (already scalar/list)
        self._number = number
        self._attribs = attribs

    def number(self):
        return self._number

    def position(self):
        return self._attribs.get("P", (0.0, 0.0, 0.0))

    def attribValue(self, attrib):
        # attrib is a _FakeAttrib or a string name
        if isinstance(attrib, _FakeAttrib):
            name = attrib.name()
        else:
            name = attrib
        return self._attribs.get(name)


class _FakeVector3(object):
    def __init__(self, x, y, z):
        self._vals = (float(x), float(y), float(z))

    def __iter__(self):
        return iter(self._vals)

    def __getitem__(self, i):
        return self._vals[i]

    def __len__(self):
        return 3


class _FakeBoundingBox(object):
    def __init__(self, minv, maxv):
        self._minv = _FakeVector3(*minv)
        self._maxv = _FakeVector3(*maxv)
        self._sizev = _FakeVector3(
            maxv[0] - minv[0], maxv[1] - minv[1], maxv[2] - minv[2])
        self._center = _FakeVector3(
            (minv[0] + maxv[0]) / 2.0,
            (minv[1] + maxv[1]) / 2.0,
            (minv[2] + maxv[2]) / 2.0)

    def minvec(self):
        return self._minv

    def maxvec(self):
        return self._maxv

    def sizevec(self):
        return self._sizev

    def center(self):
        return self._center


class _FakeGeometry(object):
    def __init__(self, point_count=0, primitive_count=0, vertex_count=0,
                 bbox=None, point_attribs=None, prim_attribs=None,
                 vertex_attribs=None, global_attribs=None,
                 point_groups=None, prim_groups=None,
                 points_data=None):
        self._point_count = point_count
        self._primitive_count = primitive_count
        self._vertex_count = vertex_count
        self._bbox = bbox
        self._point_attribs = list(point_attribs or [])
        self._prim_attribs = list(prim_attribs or [])
        self._vertex_attribs = list(vertex_attribs or [])
        self._global_attribs = list(global_attribs or [])
        self._point_groups = list(point_groups or [])
        self._prim_groups = list(prim_groups or [])
        self._points_data = list(points_data or [])

    def intrinsicValue(self, key):
        if key == "pointcount":
            return self._point_count
        if key == "primitivecount":
            return self._primitive_count
        if key == "vertexcount":
            return self._vertex_count
        raise KeyError("unknown intrinsic: {0}".format(key))

    def boundingBox(self):
        if self._bbox is None:
            return _FakeBoundingBox((0, 0, 0), (0, 0, 0))
        return _FakeBoundingBox(self._bbox[0], self._bbox[1])

    def pointAttribs(self):
        return list(self._point_attribs)

    def primAttribs(self):
        return list(self._prim_attribs)

    def vertexAttribs(self):
        return list(self._vertex_attribs)

    def globalAttribs(self):
        return list(self._global_attribs)

    def pointGroups(self):
        return list(self._point_groups)

    def primGroups(self):
        return list(self._prim_groups)

    def iterPoints(self):
        return iter(self._points_data)

    def point(self, i):
        return self._points_data[i]

    def findPointAttrib(self, name):
        for a in self._point_attribs:
            if a.name() == name:
                return a
        return None

    def findPrimAttrib(self, name):
        for a in self._prim_attribs:
            if a.name() == name:
                return a
        return None


class _FakeSopNode(object):
    """A SOP node that owns geometry; behaves like hou.SopNode.

    The test hou stub will register this class as `hou.SopNode`, so
    `isinstance(node, hou.SopNode)` in production code returns True for
    every _FakeSopNode instance.
    """

    def __init__(self, path, geometry):
        self._path = path
        self._geometry = geometry

    def path(self):
        return self._path

    def type(self):
        return _FakeAttribType("sop")

    def geometry(self):
        return self._geometry


class _FakeDisplayNode(object):
    """A OBJ displayNode stub: behaves like hou.Node with displayNode() call."""

    def __init__(self, sop):
        self._sop = sop

    def path(self):
        return self._sop.path()

    def type(self):
        return _FakeAttribType("sop")

    def geometry(self):
        return self._sop.geometry()


class _FakeObjNode(object):
    """OBJ geometry container node: not isinstance(SopNode), has displayNode."""

    def __init__(self, path, display_sop):
        self._path = path
        self._display_sop = display_sop

    def path(self):
        return self._path

    def type(self):
        return _FakeAttribType("geo")

    def displayNode(self):
        return self._display_sop


class _FakeObjNoDisplay(object):
    """OBJ node with no display SOP — should raise."""

    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path

    def type(self):
        return _FakeAttribType("geo")

    def displayNode(self):
        return None


class _FakeHou(object):
    def __init__(self, nodes):
        # nodes: dict path -> node
        self._nodes = dict(nodes)
        # Register _FakeSopNode as hou.SopNode so isinstance() works.
        self.SopNode = _FakeSopNode

    def node(self, path):
        return self._nodes.get(path)


def _make_simple_geometry(point_count=10, primitive_count=8,
                          vertex_count=24,
                          point_attribs=None, prim_attribs=None,
                          point_groups=None, prim_groups=None,
                          points_data=None, bbox=None):
    pa = point_attribs if point_attribs is not None else [
        _FakeAttrib("P", "Float", 3),
        _FakeAttrib("N", "Float", 3),
        _FakeAttrib("id", "Int", 1),
    ]
    pra = prim_attribs if prim_attribs is not None else []
    pgs = point_groups if point_groups is not None else [
        _FakeGroup("selected", "Point", 5),
    ]
    prgs = prim_groups if prim_groups is not None else [
        _FakeGroup("visible", "Primitive", 8),
    ]
    pd = points_data if points_data is not None else [
        _FakePoint(i, {"P": (float(i), 0.0, 0.0),
                       "N": (0.0, 1.0, 0.0),
                       "id": i})
        for i in range(point_count)
    ]
    return _FakeGeometry(
        point_count=point_count,
        primitive_count=primitive_count,
        vertex_count=vertex_count,
        point_attribs=pa, prim_attribs=pra,
        point_groups=pgs, prim_groups=prgs,
        points_data=pd, bbox=bbox,
    )


# ===========================================================================
# Section A: default behavior (small geometry, full payload)
# ===========================================================================
class DefaultBehaviorTests(unittest.TestCase):

    def _make_hou(self, **kwargs):
        geo = _make_simple_geometry(**kwargs)
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        return hou, sop, geo

    def test_returns_expected_top_level_keys(self):
        hou, sop, geo = self._make_hou()
        result = gs.get_geo_summary(hou, "/obj/box")
        for key in ("path", "type", "point_count", "primitive_count",
                    "vertex_count", "bbox", "attributes", "groups",
                    "sample_points", "_degraded", "_degrade_reason"):
            self.assertIn(key, result, "missing top-level key: {0}".format(key))

    def test_path_is_sop_path(self):
        hou, sop, geo = self._make_hou()
        result = gs.get_geo_summary(hou, "/obj/box")
        self.assertEqual(result["path"], "/obj/box")

    def test_type_is_sop_type(self):
        hou, sop, geo = self._make_hou()
        result = gs.get_geo_summary(hou, "/obj/box")
        self.assertEqual(result["type"], "sop")


# ===========================================================================
# Section B: counts (point_count / primitive_count / vertex_count)
# ===========================================================================
class CountsTests(unittest.TestCase):

    def test_counts_match_intrinsics(self):
        geo = _make_simple_geometry(point_count=42, primitive_count=17,
                                    vertex_count=88)
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        self.assertEqual(result["point_count"], 42)
        self.assertEqual(result["primitive_count"], 17)
        self.assertEqual(result["vertex_count"], 88)


# ===========================================================================
# Section C: bbox 6-tuple [xmin,ymin,zmin,xmax,ymax,zmax]
# ===========================================================================
class BboxTests(unittest.TestCase):

    def test_bbox_is_six_tuple(self):
        geo = _make_simple_geometry(bbox=((-1.0, -2.0, -3.0), (4.0, 5.0, 6.0)))
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        bbox = result["bbox"]
        self.assertIsInstance(bbox, list)
        self.assertEqual(len(bbox), 6)
        self.assertEqual(bbox, [-1.0, -2.0, -3.0, 4.0, 5.0, 6.0])

    def test_bbox_negative_coords(self):
        geo = _make_simple_geometry(bbox=((-10.0, -20.0, -30.0),
                                          (-1.0, -2.0, -3.0)))
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        self.assertEqual(result["bbox"][:3], [-10.0, -20.0, -30.0])
        self.assertEqual(result["bbox"][3:], [-1.0, -2.0, -3.0])

    def test_bbox_zero_for_empty_geometry(self):
        """空几何 bbox 应能安全返 6 元（minvec/maxvec 返回 None 的 fallback）."""
        geo = _make_simple_geometry(point_count=0, primitive_count=0,
                                    vertex_count=0,
                                    point_attribs=[], prim_attribs=[],
                                    point_groups=[], prim_groups=[],
                                    points_data=[])
        sop = _FakeSopNode("/obj/empty", geo)
        hou = _FakeHou({"/obj/empty": sop})
        # bbox returns default (0,0,0)-(0,0,0) on empty geo in stub
        result = gs.get_geo_summary(hou, "/obj/empty")
        self.assertIsInstance(result["bbox"], list)
        self.assertEqual(len(result["bbox"]), 6)


# ===========================================================================
# Section D: attributes list (name / type / size)
# ===========================================================================
class AttributesTests(unittest.TestCase):

    def test_attributes_have_name_type_size(self):
        geo = _make_simple_geometry()
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        attribs = result["attributes"]
        self.assertIsInstance(attribs, list)
        self.assertGreater(len(attribs), 0)
        for a in attribs:
            self.assertIn("name", a)
            self.assertIn("type", a)
            self.assertIn("size", a)
            self.assertIsInstance(a["name"], str)
            self.assertIsInstance(a["type"], str)
            self.assertIsInstance(a["size"], int)

    def test_attributes_includes_P_N_id(self):
        geo = _make_simple_geometry()
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        names = [a["name"] for a in result["attributes"]]
        self.assertIn("P", names)
        self.assertIn("N", names)
        self.assertIn("id", names)

    def test_attributes_size_matches_attrib(self):
        geo = _make_simple_geometry()
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        for a in result["attributes"]:
            if a["name"] == "P":
                self.assertEqual(a["size"], 3)
            elif a["name"] == "id":
                self.assertEqual(a["size"], 1)


# ===========================================================================
# Section E: groups list (name / type / size)
# ===========================================================================
class GroupsTests(unittest.TestCase):

    def test_groups_have_name_type_size(self):
        geo = _make_simple_geometry()
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        groups = result["groups"]
        self.assertIsInstance(groups, list)
        self.assertGreater(len(groups), 0)
        for g in groups:
            self.assertIn("name", g)
            self.assertIn("type", g)
            self.assertIn("size", g)
            self.assertIn(g["type"], ("point", "primitive", "vertex"))

    def test_groups_include_selected_and_visible(self):
        geo = _make_simple_geometry()
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        names = [g["name"] for g in result["groups"]]
        self.assertIn("selected", names)
        self.assertIn("visible", names)


# ===========================================================================
# Section F: sample_points (default sample_size=10)
# ===========================================================================
class SamplePointsTests(unittest.TestCase):

    def test_sample_points_default_size_10(self):
        geo = _make_simple_geometry(point_count=50)
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        self.assertIsInstance(result["sample_points"], list)
        self.assertEqual(len(result["sample_points"]), 10)

    def test_sample_points_contains_position(self):
        geo = _make_simple_geometry(point_count=5)
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box")
        for sp in result["sample_points"]:
            self.assertIn("P", sp)
            self.assertIsInstance(sp["P"], list)
            self.assertEqual(len(sp["P"]), 3)

    def test_sample_size_explicit(self):
        geo = _make_simple_geometry(point_count=50)
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box", sample_size=3)
        self.assertEqual(len(result["sample_points"]), 3)

    def test_sample_size_0_returns_no_samples(self):
        geo = _make_simple_geometry(point_count=50)
        sop = _FakeSopNode("/obj/box", geo)
        hou = _FakeHou({"/obj/box": sop})
        result = gs.get_geo_summary(hou, "/obj/box", sample_size=0)
        self.assertEqual(result["sample_points"], [])


# ===========================================================================
# Section G: degradation — large geometry (>1M points)
# ===========================================================================
class DegradationTests(unittest.TestCase):

    def _make_large_hou(self, point_count):
        # Don't materialize 1M points; sample only needs first 10.
        # get_geo_summary should NOT iterate points when degraded.
        geo = _make_simple_geometry(
            point_count=point_count,
            primitive_count=point_count // 2,
            vertex_count=point_count * 2,
            point_attribs=[_FakeAttrib("P", "Float", 3),
                           _FakeAttrib("N", "Float", 3)],
            point_groups=[_FakeGroup("g1", "Point", 100)],
            prim_groups=[_FakeGroup("g2", "Primitive", 50)],
            points_data=[],
        )
        sop = _FakeSopNode("/obj/huge", geo)
        hou = _FakeHou({"/obj/huge": sop})
        return hou, sop, geo

    def test_over_1m_is_degraded(self):
        hou, sop, geo = self._make_large_hou(1_000_001)
        result = gs.get_geo_summary(hou, "/obj/huge")
        self.assertTrue(result["_degraded"])
        self.assertIsInstance(result["_degrade_reason"], str)
        self.assertGreater(len(result["_degrade_reason"]), 0)

    def test_over_1m_omits_sample_points(self):
        hou, sop, geo = self._make_large_hou(1_500_000)
        result = gs.get_geo_summary(hou, "/obj/huge")
        self.assertEqual(result["sample_points"], [])

    def test_over_1m_omits_groups(self):
        hou, sop, geo = self._make_large_hou(1_500_000)
        result = gs.get_geo_summary(hou, "/obj/huge")
        self.assertEqual(result["groups"], [])

    def test_over_1m_does_not_return_detailed_attributes(self):
        """降级模式不返每个 attribute 的 size 详情。"""
        hou, sop, geo = self._make_large_hou(1_500_000)
        result = gs.get_geo_summary(hou, "/obj/huge")
        # 不返详细 size：要么 attributes 为空列表，要么每个 entry 缺 size
        for a in result["attributes"]:
            self.assertNotIn(
                "size", a,
                "degraded attributes must not include per-entry 'size'")
        # 同时应保留 attributes 总数（便于上游了解几何规模）
        self.assertIn("attribute_count", result)
        self.assertEqual(result["attribute_count"], 2)

    def test_exact_1m_is_full_payload(self):
        """精确 1M 点应仍走全字段路径（边界：> not >=）。"""
        hou, sop, geo = self._make_large_hou(1_000_000)
        result = gs.get_geo_summary(hou, "/obj/huge")
        self.assertFalse(result["_degraded"])
        self.assertEqual(result["_degrade_reason"], "")
        # attributes 必有 size 字段（不降级）
        for a in result["attributes"]:
            self.assertIn("size", a, "full-mode attributes must include size")
        # groups 必有 size 字段
        for g in result["groups"]:
            self.assertIn("size", g, "full-mode groups must include size")

    def test_exact_1m_attributes_have_size(self):
        """精确 1M 点 -> 全字段: 每个 attribute entry 含 size."""
        geo = _make_simple_geometry(
            point_count=1_000_000,
            primitive_count=500_000,
            vertex_count=1_500_000,
            point_attribs=[_FakeAttrib("P", "Float", 3),
                           _FakeAttrib("id", "Int", 1)],
            point_groups=[_FakeGroup("all", "Point", 100)],
            prim_groups=[],
            points_data=[],
        )
        sop = _FakeSopNode("/obj/huge", geo)
        hou = _FakeHou({"/obj/huge": sop})
        result = gs.get_geo_summary(hou, "/obj/huge")
        self.assertFalse(result["_degraded"])
        for a in result["attributes"]:
            self.assertIn("size", a, "full-mode attributes must include size")

    def test_1m_plus_one_is_degraded(self):
        hou, sop, geo = self._make_large_hou(1_000_001)
        result = gs.get_geo_summary(hou, "/obj/huge")
        self.assertTrue(result["_degraded"])

    def test_custom_max_points_threshold(self):
        """max_points_for_full=100 时, 101 点 -> 降级."""
        geo = _make_simple_geometry(point_count=101, primitive_count=50,
                                    vertex_count=150,
                                    point_attribs=[_FakeAttrib("P", "Float", 3)],
                                    point_groups=[],
                                    points_data=[])
        sop = _FakeSopNode("/obj/m", geo)
        hou = _FakeHou({"/obj/m": sop})
        result = gs.get_geo_summary(hou, "/obj/m",
                                    max_points_for_full=100)
        self.assertTrue(result["_degraded"])

    def test_custom_max_points_under_threshold_full(self):
        """max_points_for_full=100 时, 100 点 -> 全字段."""
        geo = _make_simple_geometry(point_count=100, primitive_count=50,
                                    vertex_count=150,
                                    point_attribs=[_FakeAttrib("P", "Float", 3)],
                                    point_groups=[],
                                    points_data=[])
        sop = _FakeSopNode("/obj/m", geo)
        hou = _FakeHou({"/obj/m": sop})
        result = gs.get_geo_summary(hou, "/obj/m",
                                    max_points_for_full=100)
        self.assertFalse(result["_degraded"])

    def test_degrade_reason_mentions_point_count(self):
        hou, sop, geo = self._make_large_hou(2_000_000)
        result = gs.get_geo_summary(hou, "/obj/huge")
        self.assertIn("2000000", result["_degrade_reason"])
        # 也应提到 max_points_for_full
        self.assertIn("1000000", result["_degrade_reason"])


# ===========================================================================
# Section H: OBJ node auto-resolve to displayNode
# ===========================================================================
class ObjResolveTests(unittest.TestCase):

    def test_obj_node_resolves_to_display_sop(self):
        geo = _make_simple_geometry(point_count=5)
        display_sop = _FakeSopNode("/obj/geo1", geo)
        obj = _FakeObjNode("/obj", display_sop)
        hou = _FakeHou({"/obj": obj, "/obj/geo1": display_sop})
        result = gs.get_geo_summary(hou, "/obj")
        # path 应是 display SOP 的路径
        self.assertEqual(result["path"], "/obj/geo1")
        self.assertEqual(result["point_count"], 5)


# ===========================================================================
# Section I: error paths
# ===========================================================================
class ErrorPathTests(unittest.TestCase):

    def test_missing_node_raises_value_error(self):
        hou = _FakeHou({})
        with self.assertRaises(ValueError):
            gs.get_geo_summary(hou, "/obj/missing")

    def test_non_geometry_node_no_display_raises_value_error(self):
        """非几何节点 + 无 displayNode -> ValueError."""
        obj = _FakeObjNoDisplay("/obj/empty")
        hou = _FakeHou({"/obj/empty": obj})
        with self.assertRaises(ValueError):
            gs.get_geo_summary(hou, "/obj/empty")


# ===========================================================================
# Section J: hou 不顶层 import
# ===========================================================================
class HouImportIsolationTests(unittest.TestCase):

    def test_geo_summary_does_not_top_level_import_hou(self):
        """_geo_summary.py 必须不顶层 import hou（参数注入约定）."""
        src_path = os.path.join(ROOT, "_geo_summary.py")
        with open(src_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "hou" or alias.name.startswith("hou."):
                        bad_imports.append("import {0}".format(alias.name))
            elif isinstance(node, ast.ImportFrom):
                if (node.module == "hou"
                        or (node.module
                            and node.module.startswith("hou."))):
                    bad_imports.append(
                        "from {0} import ...".format(node.module))
        self.assertEqual(
            bad_imports, [],
            "_geo_summary.py must not top-level import hou: {0}"
            .format(bad_imports))


# ===========================================================================
# Section K: bridge @mcp.tool() style + behavior (PR 12 自写探针)
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "houdini_mcp_server.py")
PR12_SECTION_HEADER = "# PR 12 Geometry Summary"

PR12_BRIDGE_TOOLS = ["get_geo_summary"]


def _parse_server_source():
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        return f.read()


def _find_pr12_tool_nodes():
    """Locate PR 12 bridge @mcp.tool() function nodes via AST.

    Stops scanning at the next top-level "# PR" section header so we never
    accidentally pick up PR 7/8/9/10/11/future sections.
    """
    src = _parse_server_source()
    tree = ast.parse(src)
    lines = src.splitlines()

    header_line = None
    for i, line in enumerate(lines, start=1):
        if PR12_SECTION_HEADER in line:
            header_line = i
            break
    if header_line is None:
        raise AssertionError(
            "PR 12 section marker not found in houdini_mcp_server.py. "
            "Add a comment line containing {0!r} before the new tool."
            .format(PR12_SECTION_HEADER))

    next_header_line = None
    for i, line in enumerate(lines, start=1):
        if i <= header_line:
            continue
        stripped = line.lstrip()
        if (stripped.startswith("# PR ")
                and PR12_SECTION_HEADER not in line):
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
        if node.name not in PR12_BRIDGE_TOOLS:
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
    missing = [n for n in PR12_BRIDGE_TOOLS if n not in found]
    if missing:
        raise AssertionError(
            "PR 12 bridge tools not found in section: {0}. Found: {1}"
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
        if not (isinstance(first, ast.Constant)
                and first.value == cmd_type):
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


class PR12BridgeStyleTests(unittest.TestCase):
    """PR 12 brief: bridge @mcp.tool() 必须遵守 PR 7 fix 范式 — 无类型注解 +
    中文 docstring。本探针不修改 test_bridge_style.py."""

    def setUp(self):
        self.tools = _find_pr12_tool_nodes()
        self.assertEqual(
            len(self.tools), 1,
            "Expected 1 PR 12 bridge tool, found {0}: {1}".format(
                len(self.tools), list(self.tools.keys())))

    def test_get_geo_summary_no_type_annotations(self):
        fn = self.tools["get_geo_summary"]
        kinds = _signature_annotation_kinds(fn)
        self.assertFalse(
            kinds["arg_annotations"],
            "get_geo_summary must not have parameter type annotations")
        self.assertFalse(
            kinds["return_annotation"],
            "get_geo_summary must not have return type annotation")

    def test_get_geo_summary_chinese_docstring(self):
        fn = self.tools["get_geo_summary"]
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(
            _has_cjk(doc),
            "get_geo_summary docstring must contain Chinese (CJK). "
            "Got: {0!r}".format(doc))

    def test_get_geo_summary_signature_has_ctx(self):
        fn = self.tools["get_geo_summary"]
        params = _extract_param_names(fn)
        self.assertIn(
            "ctx", params,
            "get_geo_summary must accept ctx as first parameter")

    def test_get_geo_summary_signature_params(self):
        fn = self.tools["get_geo_summary"]
        params = _extract_param_names(fn)
        for name in ("node_path", "max_points_for_full", "sample_size"):
            self.assertIn(
                name, params,
                "get_geo_summary bridge must accept {0} kwarg".format(name))

    def test_section_marker_present(self):
        src = _parse_server_source()
        self.assertIn(
            PR12_SECTION_HEADER, src,
            "PR 12 section marker missing in houdini_mcp_server.py")


class PR12BridgeBehaviorTests(unittest.TestCase):
    """PR 12 brief: bridge 必须 _houdini_call('get_geo_summary', {node_path,...})
    转发所有 3 个 kwarg (node_path, max_points_for_full, sample_size)."""

    def setUp(self):
        self.tools = _find_pr12_tool_nodes()
        self.src = _parse_server_source()

    def test_get_geo_summary_calls_houdini_with_correct_cmd(self):
        fn = self.tools["get_geo_summary"]
        d = _find_houdini_call_kwargs(fn, "get_geo_summary")
        self.assertIsNotNone(
            d,
            "get_geo_summary must call _houdini_call('get_geo_summary', ...)")

    def test_get_geo_summary_payload_contains_all_params(self):
        fn = self.tools["get_geo_summary"]
        d = _find_houdini_call_kwargs(fn, "get_geo_summary")
        self.assertIsNotNone(d, "_houdini_call kwargs not found")
        keys = _dict_keys(d)
        for k in ("node_path", "max_points_for_full", "sample_size"):
            self.assertIn(
                k, keys,
                "get_geo_summary params must include {0!r}. Got: {1}"
                .format(k, keys))

    def test_get_geo_summary_default_max_points(self):
        """默认 max_points_for_full=1_000_000 必须出现在 body source 中."""
        fn = self.tools["get_geo_summary"]
        lines = self.src.splitlines()
        body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
        self.assertIn(
            "1000000", body,
            "get_geo_summary default max_points_for_full should be 1000000")

    def test_get_geo_summary_runtime_call_succeeds(self):
        """AST-execute the bridge against a stub server and verify param dict
        reaches the server with all 3 keys + correct defaults."""
        fn = self.tools["get_geo_summary"]
        houdini_requests = []
        server_calls = []

        class _ServerStub(object):
            def get_geo_summary(self, node_path, max_points_for_full,
                                sample_size):
                server_calls.append({
                    "node_path": node_path,
                    "max_points_for_full": max_points_for_full,
                    "sample_size": sample_size,
                })
                return {
                    "path": node_path,
                    "point_count": 0,
                    "_degraded": False,
                }

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
        module = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, SERVER_PY, "exec"), namespace)

        # Default-args call: only ctx + node_path
        result = namespace["get_geo_summary"](object(), "/obj/box")
        self.assertEqual(len(houdini_requests), 1)
        cmd, params = houdini_requests[0]
        self.assertEqual(cmd, "get_geo_summary")
        self.assertEqual(params["node_path"], "/obj/box")
        self.assertEqual(params["max_points_for_full"], 1000000)
        self.assertEqual(params["sample_size"], 10)
        self.assertEqual(len(server_calls), 1)
        self.assertEqual(server_calls[0]["node_path"], "/obj/box")

    def test_get_geo_summary_explicit_args_propagate(self):
        """显式 kwarg 必须能透传到 server."""
        fn = self.tools["get_geo_summary"]
        server_calls = []

        class _ServerStub(object):
            def get_geo_summary(self, node_path, max_points_for_full,
                                sample_size):
                server_calls.append({
                    "node_path": node_path,
                    "max_points_for_full": max_points_for_full,
                    "sample_size": sample_size,
                })
                return {"path": node_path}

        def _houdini_call(command, params):
            return _ServerStub().get_geo_summary(**params)

        namespace = {
            "mcp": type(
                "_McpStub", (), {
                    "tool": lambda self: (lambda decorated: decorated),
                })(),
            "_houdini_call": _houdini_call,
        }
        module = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, SERVER_PY, "exec"), namespace)

        namespace["get_geo_summary"](
            object(), "/obj/custom", max_points_for_full=500,
            sample_size=5)
        self.assertEqual(len(server_calls), 1)
        self.assertEqual(server_calls[0]["node_path"], "/obj/custom")
        self.assertEqual(server_calls[0]["max_points_for_full"], 500)
        self.assertEqual(server_calls[0]["sample_size"], 5)


class PR12BridgeErrorPassthroughTests(unittest.TestCase):
    """PR 12 brief: bridge 必须把 _houdini_call 的 error envelope 透传."""

    def setUp(self):
        self.fn = _find_pr12_tool_nodes()["get_geo_summary"]

    def test_bridge_propagates_error_envelope(self):
        def _houdini_call(command, params):
            return {
                "status": "error",
                "message": "节点不存在: " + params.get("node_path", ""),
                "origin": "houdini",
            }

        _FakeMcp = type(
            "_FakeMcp", (), {"tool": staticmethod(lambda: lambda f: f)})
        namespace = {"_houdini_call": _houdini_call, "mcp": _FakeMcp()}
        module = ast.Module(body=[self.fn], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, SERVER_PY, "exec"), namespace)

        result = namespace["get_geo_summary"](object(), "/obj/missing")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["origin"], "houdini")
        self.assertIn("/obj/missing", result["message"])

    def test_bridge_propagates_success_envelope(self):
        success_payload = {
            "status": "success",
            "result": {"path": "/obj/box", "point_count": 10},
        }

        def _houdini_call(command, params):
            return success_payload

        _FakeMcp = type(
            "_FakeMcp", (), {"tool": staticmethod(lambda: lambda f: f)})
        namespace = {"_houdini_call": _houdini_call, "mcp": _FakeMcp()}
        module = ast.Module(body=[self.fn], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, SERVER_PY, "exec"), namespace)

        result = namespace["get_geo_summary"](object(), "/obj/box")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"]["path"], "/obj/box")


if __name__ == "__main__":
    unittest.main()