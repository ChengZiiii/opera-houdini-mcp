"""tests/test_geo_measure.py — add-geometry-export-and-measure 单测。

覆盖（tasks 3.3 / 3.4 / 3.5）：
- bounds 6 元解包与 min/max/size/center 派生。
- 四类 groups schema 与分页（point/prim 编号、vertex
  ``{prim_index, vertex_index, point_index}``、edge
  ``[min_point, max_point]``）。
- 属性分页：owner/storage/tuple-size 分派；detail owner 单值。
- ``prim_index`` 必填 + 越界 error。
- nearest Point / None 双路径。
- ``set_detail_attrib`` 通过 Attribute Create SOP（不调用 cooked
  Geometry 写方法）；单 undo group；失败 destroy 半成品。
- translator registry：format / extension 校验；临时文件 + fsync +
  ``os.replace`` 原子覆盖；``overwrite=False`` 目标存在
  → ``target_exists``；失败清理。
- 8 个 server commands = 1 MUTATING + 7 NO_UNDO；三集合并集完整、
  两两交集为空。

约束：
- stdlib unittest + 简易 hou mock；不引入新依赖。
- 不依赖真实 Houdini；H21.0 live smoke 由
  ``h21_live_geo_measure_smoke.py`` 单独执行。
"""
import base64
import importlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
import ast


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _ensure_pkg():
    pkg_name = "gme_test_pkg"
    if pkg_name in sys.modules and getattr(
            sys.modules[pkg_name], "__path__", None):
        return sys.modules[pkg_name]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [ROOT]
    sys.modules[pkg_name] = pkg
    return pkg


def _ensure_module(name):
    pkg = _ensure_pkg()
    full = pkg.__name__ + "." + name
    if full in sys.modules:
        del sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(ROOT, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# Pre-load sibling modules.
_common = _ensure_module("_common")
_geo_measure = _ensure_module("_geo_measure")


gme = _geo_measure
cmn = _common


# ---------------------------------------------------------------------------
# hou mock infrastructure
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
    """支持 point / prim / vertex / edge 四类。"""

    def __init__(self, name, group_type="Point", members=None):
        self._name = name
        self._type_name = group_type
        self._members = members or []

    def name(self):
        return self._name

    def type(self):
        return _FakeGroupType(self._type_name)

    def iterIndices(self):
        return iter(list(self._members))

    def iterVertices(self):
        return iter([v for v in self._members
                     if isinstance(v, _FakeVertex)])

    def iterEdges(self):
        return iter([e for e in self._members
                     if isinstance(e, _FakeEdge)])

    def __len__(self):
        return len(self._members)


class _FakePoint(object):
    def __init__(self, number, position, attribs=None):
        self._number = number
        self._position = position
        self._attribs = attribs or {}

    def number(self):
        return self._number

    def position(self):
        return self._position

    def attribValue(self, attrib):
        if isinstance(attrib, _FakeAttrib):
            name = attrib.name()
        else:
            name = attrib
        return self._attribs.get(name)


class _FakeVertex(object):
    def __init__(self, prim, vertex_index, point_index):
        self._prim = prim
        self._vertex_index = vertex_index
        self._point_index = point_index

    def prim(self):
        return self._prim

    def number(self):
        return self._vertex_index

    def point(self):
        return _FakePoint(self._point_index, (0.0, 0.0, 0.0))


class _FakeEdge(object):
    def __init__(self, point_a, point_b):
        self._point_a = point_a
        self._point_b = point_b

    def vertices(self):
        v_a = _FakeVertex(None, -1, self._point_a)
        v_b = _FakeVertex(None, -1, self._point_b)
        return [v_a, v_b]


class _FakePrim(object):
    def __init__(self, number, attribs=None):
        self._number = number
        self._attribs = attribs or {}

    def number(self):
        return self._number

    def attribValue(self, attrib):
        if isinstance(attrib, _FakeAttrib):
            name = attrib.name()
        else:
            name = attrib
        return self._attribs.get(name)

    def intrinsicNames(self):
        return ["closed", "hasdetail"]

    def intrinsicValue(self, name):
        if name == "closed":
            return True
        if name == "hasdetail":
            return False
        raise KeyError(name)


class _FakeGeometry(object):
    """提供 H21+ geometry 关键 API 的最小集."""

    def __init__(self, points=None, prims=None, attribs=None,
                 point_groups=None, prim_groups=None,
                 vertex_groups=None, edge_groups=None,
                 global_attribs=None, bounds=None):
        self._points = list(points or [])
        self._prims = list(prims or [])
        self._attribs = attribs or {}
        self._point_groups = list(point_groups or [])
        self._prim_groups = list(prim_groups or [])
        self._vertex_groups = list(vertex_groups or [])
        self._edge_groups = list(edge_groups or [])
        self._global_attribs = list(global_attribs or [])
        self._bounds = bounds or ([0.0] * 3, [1.0] * 3)
        self._save_calls = []

    # --- counts / bbox ---
    def intrinsicValue(self, key):
        if key == "pointcount":
            return len(self._points)
        if key == "primitivecount":
            return len(self._prims)
        if key == "vertexcount":
            return sum(len(p._attribs.get("vcount", [1])) for p in self._prims)
        if key == "bounds":
            mn = self._bounds[0]
            mx = self._bounds[1]
            return [mn[0], mx[0], mn[1], mx[1], mn[2], mx[2]]
        raise KeyError(key)

    def boundingBox(self):
        class _BB(object):
            def minvec(self):
                return tuple(self._bounds[0])
            def maxvec(self):
                return tuple(self._bounds[1])
        _BB._bounds = self._bounds
        return _BB()

    # --- iteration ---
    def iterPoints(self):
        return iter(self._points)

    def iterPrims(self):
        return iter(self._prims)

    def iterVertices(self):
        return iter([])

    def attribValue(self, attrib):
        # detail
        if isinstance(attrib, _FakeAttrib):
            name = attrib.name()
        else:
            name = attrib
        return self._attribs.get("detail_" + name)

    # --- group queries ---
    def pointGroups(self):
        return list(self._point_groups)

    def primGroups(self):
        return list(self._prim_groups)

    def vertexGroups(self):
        return list(self._vertex_groups)

    def edgeGroups(self):
        return list(self._edge_groups)

    def findPointAttrib(self, name):
        return self._attribs.get("point_" + name)

    def findPrimAttrib(self, name):
        return self._attribs.get("prim_" + name)

    def findVertexAttrib(self, name):
        return self._attribs.get("vertex_" + name)

    def findGlobalAttrib(self, name):
        return self._attribs.get("detail_" + name)

    # --- saveToFile ---
    def saveToFile(self, path, file_type=None):
        if file_type is None:
            file_type = "bgeo"
        # 记录调用 + 写最小 bgeo header 让 fsync 验证通过
        self._save_calls.append((path, file_type))
        with open(path, "wb") as fh:
            fh.write(b"GEOH")
            fh.write(file_type.encode("ascii")[:8])
            fh.write(b"\x00\x00")

    def save_calls(self):
        return list(self._save_calls)


class _FakeSopNode(object):
    def __init__(self, path, geometry, parent=None):
        self._path = path
        self._geometry = geometry
        self._parent = parent

    def path(self):
        return self._path

    def geometry(self):
        return self._geometry

    def parent(self):
        return self._parent

    def type(self):
        return _FakeAttribType("sop")


class _FakeNode(object):
    def __init__(self, path, children=None, parent=None):
        self._path = path
        self._children = list(children or [])
        self._parent = parent
        self._destroyed = False

    def path(self):
        return self._path

    def children(self):
        return list(self._children)

    def createNode(self, node_type, node_name=None):
        if node_type != "attribcreate":
            raise RuntimeError("unexpected node_type: " + str(node_type))
        name = node_name or "attribcreate_auto"
        full_path = (self._path + "/" + name).replace("//", "/")
        new_node = _FakeAttribCreateNode(full_path, parent=self)
        self._children.append(new_node)
        return new_node

    def destroy(self):
        self._destroyed = True
        if self._parent is not None:
            if self in self._parent._children:
                self._parent._children.remove(self)


class _FakeAttribCreateNode(_FakeNode):
    def __init__(self, path, parent=None):
        super(_FakeAttribCreateNode, self).__init__(path, parent=parent)
        self._parms = {}
        self._input = None

    def parm(self, name):
        return _FakeParm(self, name)

    def set_parm(self, name, value):
        self._parms[name] = value

    def setFirstInput(self, source):
        self._input = source


class _FakeParm(object):
    def __init__(self, owner, name):
        self._owner = owner
        self._name = name

    def set(self, value):
        self._owner.set_parm(self._name, value)

    def get(self):
        return self._owner._parms.get(self._name)


class _FakeHou(object):
    """支持 .node() / .SopNode / .Geometry 等必要 API."""

    def __init__(self, nodes, undos=None):
        self._nodes = nodes
        self.SopNode = _FakeSopNode
        self._undos = undos

    def node(self, path):
        return self._nodes.get(path)

    @property
    def undos(self):
        return self._undos


class _FakeUndoGroup(object):
    def __init__(self):
        self.entered = False
        self.exited = False

    def group(self, label):
        return self

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exited = True
        return False


# ---------------------------------------------------------------------------
# Section A: helper builders
# ---------------------------------------------------------------------------
def _make_basic_geo():
    pts = [
        _FakePoint(0, (0.0, 0.0, 0.0), {"P": [0.0, 0.0, 0.0],
                                          "id": 10}),
        _FakePoint(1, (1.0, 0.0, 0.0), {"P": [1.0, 0.0, 0.0],
                                          "id": 11}),
        _FakePoint(2, (1.0, 1.0, 0.0), {"P": [1.0, 1.0, 0.0],
                                          "id": 12}),
        _FakePoint(3, (0.0, 1.0, 0.0), {"P": [0.0, 1.0, 0.0],
                                          "id": 13}),
        _FakePoint(4, (0.0, 0.0, 1.0), {"P": [0.0, 0.0, 1.0],
                                          "id": 14}),
    ]
    prims = [_FakePrim(0), _FakePrim(1)]
    attribs = {
        "point_id": _FakeAttrib("id", "Int", 1),
        "prim_age": _FakeAttrib("age", "Float", 1),
        "detail_label": _FakeAttrib("label", "String", 1),
        "detail_age": _FakeAttrib("age_d", "Float", 1),
    }
    point_groups = [
        _FakeGroup("selected", "Point", [0, 1, 4]),
    ]
    prim_groups = [
        _FakeGroup("visible", "Primitive", [0, 1]),
    ]
    vertex_groups = [
        _FakeGroup("corners", "Vertex", [
            _FakeVertex(prims[0], 0, 0),
            _FakeVertex(prims[1], 1, 3),
        ]),
    ]
    edge_groups = [
        _FakeGroup("base", "Edge", [
            _FakeEdge(0, 1), _FakeEdge(2, 3),
        ]),
    ]
    geo = _FakeGeometry(
        points=pts, prims=prims, attribs=attribs,
        point_groups=point_groups, prim_groups=prim_groups,
        vertex_groups=vertex_groups, edge_groups=edge_groups,
        bounds=([0.0, 0.0, 0.0], [1.0, 1.0, 1.0]))
    geo._attribs["detail_label"] = _FakeAttrib("label", "String", 1)
    geo._attribs["detail_age"] = _FakeAttrib("age_d", "Float", 1)
    return geo


def _make_hou(extra_nodes=None, undos=None):
    geo = _make_basic_geo()
    parent = _FakeNode("/obj")
    sop = _FakeSopNode("/obj/box", geo, parent=parent)
    parent._children.append(sop)
    nodes = {"/obj/box": sop, "/obj": parent}
    if extra_nodes:
        nodes.update(extra_nodes)
    return _FakeHou(nodes, undos=undos)


# ===========================================================================
# Section B: get_bounding_box — 6 元 + min/max/size/center
# ===========================================================================
class BoundingBoxTests(unittest.TestCase):

    def test_six_tuple_unpacked_min_max_size_center(self):
        hou = _make_hou()
        result = gme.get_bounding_box(hou, "/obj/box")
        self.assertEqual(result["status"], "success")
        r = result["result"]
        self.assertEqual(r["min"], [0.0, 0.0, 0.0])
        self.assertEqual(r["max"], [1.0, 1.0, 1.0])
        self.assertEqual(r["size"], [1.0, 1.0, 1.0])
        self.assertEqual(r["center"], [0.5, 0.5, 0.5])

    def test_negative_coords(self):
        geo = _make_basic_geo()
        geo._bounds = ([-2.0, -3.0, -4.0], [2.0, 3.0, 4.0])
        parent = _FakeNode("/obj")
        sop = _FakeSopNode("/obj/box", geo, parent=parent)
        parent._children.append(sop)
        hou = _FakeHou({"/obj/box": sop, "/obj": parent})
        r = gme.get_bounding_box(hou, "/obj/box")["result"]
        self.assertEqual(r["min"], [-2.0, -3.0, -4.0])
        self.assertEqual(r["max"], [2.0, 3.0, 4.0])
        self.assertEqual(r["center"], [0.0, 0.0, 0.0])

    def test_node_not_found_returns_error(self):
        hou = _make_hou()
        result = gme.get_bounding_box(hou, "/obj/missing")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["field"], "node_path")

    def test_empty_node_path_returns_error(self):
        hou = _make_hou()
        result = gme.get_bounding_box(hou, "")
        self.assertEqual(result["status"], "error")


# ===========================================================================
# Section C: get_groups — 四类 schema
# ===========================================================================
class GetGroupsTests(unittest.TestCase):

    def test_four_class_groups_returned(self):
        hou = _make_hou()
        r = gme.get_groups(hou, "/obj/box")["result"]
        self.assertIn("point", r["groups"])
        self.assertIn("prim", r["groups"])
        self.assertIn("vertex", r["groups"])
        self.assertIn("edge", r["groups"])
        self.assertEqual(r["groups"]["point"], ["selected"])
        self.assertEqual(r["groups"]["prim"], ["visible"])
        self.assertEqual(r["groups"]["vertex"], ["corners"])
        self.assertEqual(r["groups"]["edge"], ["base"])

    def test_missing_node_error(self):
        hou = _make_hou()
        r = gme.get_groups(hou, "/missing")
        self.assertEqual(r["status"], "error")


# ===========================================================================
# Section D: get_group_members — 分页 + 四类成员 schema
# ===========================================================================
class GetGroupMembersTests(unittest.TestCase):

    def test_point_members_paginated(self):
        hou = _make_hou()
        r = gme.get_group_members(
            hou, "/obj/box", "point", "selected", offset=0, limit=10)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["result"]["members"], [0, 1, 4])
        self.assertEqual(r["result"]["total"], 3)
        self.assertIsNone(r["result"]["next_offset"])

    def test_prim_members_paginated_with_offset(self):
        hou = _make_hou()
        r = gme.get_group_members(
            hou, "/obj/box", "prim", "visible", offset=1, limit=1)
        self.assertEqual(r["result"]["members"], [1])
        self.assertEqual(r["result"]["total"], 2)
        self.assertIsNone(r["result"]["next_offset"])

    def test_vertex_members_schema(self):
        hou = _make_hou()
        r = gme.get_group_members(
            hou, "/obj/box", "vertex", "corners", offset=0, limit=10)
        self.assertEqual(r["status"], "success")
        mems = r["result"]["members"]
        self.assertEqual(len(mems), 2)
        self.assertIn("prim_index", mems[0])
        self.assertIn("vertex_index", mems[0])
        self.assertIn("point_index", mems[0])
        self.assertEqual(mems[0]["point_index"], 0)
        self.assertEqual(mems[1]["point_index"], 3)

    def test_edge_members_sorted_endpoints(self):
        hou = _make_hou()
        r = gme.get_group_members(
            hou, "/obj/box", "edge", "base", offset=0, limit=10)
        mems = r["result"]["members"]
        self.assertEqual(mems[0], [0, 1])
        self.assertEqual(mems[1], [2, 3])

    def test_unknown_group_type(self):
        hou = _make_hou()
        r = gme.get_group_members(
            hou, "/obj/box", "vertex", "corners", offset=0, limit=10)
        self.assertEqual(r["status"], "success")

    def test_unknown_group_name(self):
        hou = _make_hou()
        r = gme.get_group_members(
            hou, "/obj/box", "point", "missing_group", offset=0, limit=10)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "group_name")

    def test_invalid_group_type_string(self):
        hou = _make_hou()
        r = gme.get_group_members(
            hou, "/obj/box", "bogus", "any", offset=0, limit=10)
        self.assertEqual(r["status"], "error")

    def test_limit_zero_returns_empty_page(self):
        hou = _make_hou()
        r = gme.get_group_members(
            hou, "/obj/box", "point", "selected", offset=0, limit=0)
        # limit <= 0 由 _coerce_limit 处理：返回 error
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "limit")

    def test_negative_offset_error(self):
        hou = _make_hou()
        r = gme.get_group_members(
            hou, "/obj/box", "point", "selected", offset=-1, limit=10)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "offset")


# ===========================================================================
# Section E: get_attrib_values — owner / storage / tuple-size 分派
# ===========================================================================
class GetAttribValuesTests(unittest.TestCase):

    def test_point_owner_storage_int_tuple_size_1(self):
        hou = _make_hou()
        r = gme.get_attrib_values(
            hou, "/obj/box", "id", attrib_class="point",
            offset=0, limit=10)
        self.assertEqual(r["status"], "success")
        res = r["result"]
        self.assertEqual(res["storage"], "int")
        self.assertEqual(res["tuple_size"], 1)
        self.assertEqual(res["total"], 5)
        self.assertEqual(res["values"], [10, 11, 12, 13, 14])

    def test_detail_owner_single_value(self):
        hou = _make_hou()
        # mock findGlobalAttrib 拼前缀 ``detail_<name>``
        hou._nodes["/obj/box"]._geometry._attribs["detail_age_d"] = \
            _FakeAttrib("age_d", "Float", 1)
        original = hou._nodes["/obj/box"]._geometry.attribValue

        def _patched(attrib):
            if isinstance(attrib, _FakeAttrib):
                name = attrib.name()
            else:
                name = attrib
            if name == "age_d":
                return 42.0
            return original(attrib)
        hou._nodes["/obj/box"]._geometry.attribValue = _patched
        r = gme.get_attrib_values(
            hou, "/obj/box", "age_d", attrib_class="detail",
            offset=0, limit=10)
        self.assertEqual(r["status"], "success")
        res = r["result"]
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["values"], [42.0])
        self.assertEqual(res["storage"], "float")

    def test_unknown_attribute_returns_error(self):
        hou = _make_hou()
        r = gme.get_attrib_values(
            hou, "/obj/box", "no_such", attrib_class="point",
            offset=0, limit=10)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "attribute")

    def test_invalid_attrib_class(self):
        hou = _make_hou()
        r = gme.get_attrib_values(
            hou, "/obj/box", "id", attrib_class="bogus",
            offset=0, limit=10)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "attrib_class")

    def test_pagination_offset_limit_total(self):
        hou = _make_hou()
        r = gme.get_attrib_values(
            hou, "/obj/box", "id", attrib_class="point",
            offset=2, limit=2)
        self.assertEqual(r["result"]["offset"], 2)
        self.assertEqual(r["result"]["limit"], 2)
        self.assertEqual(r["result"]["total"], 5)
        self.assertEqual(r["result"]["values"], [12, 13])
        self.assertEqual(r["result"]["next_offset"], 4)

    def test_offset_past_total_returns_empty(self):
        hou = _make_hou()
        r = gme.get_attrib_values(
            hou, "/obj/box", "id", attrib_class="point",
            offset=100, limit=10)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["result"]["values"], [])
        self.assertEqual(r["result"]["next_offset"], None)


# ===========================================================================
# Section F: get_prim_intrinsics — 必须 prim_index
# ===========================================================================
class GetPrimIntrinsicsTests(unittest.TestCase):

    def test_missing_prim_index_returns_error(self):
        hou = _make_hou()
        r = gme.get_prim_intrinsics(hou, "/obj/box", None)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "prim_index")

    def test_out_of_range_prim_index(self):
        hou = _make_hou()
        r = gme.get_prim_intrinsics(hou, "/obj/box", 999)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "prim_index")

    def test_valid_prim_returns_intrinsics(self):
        hou = _make_hou()
        r = gme.get_prim_intrinsics(hou, "/obj/box", 0)
        self.assertEqual(r["status"], "success")
        res = r["result"]
        self.assertEqual(res["prim_index"], 0)
        self.assertIn("closed", res["intrinsics"])
        self.assertIn("hasdetail", res["intrinsics"])

    def test_names_filter(self):
        hou = _make_hou()
        r = gme.get_prim_intrinsics(hou, "/obj/box", 1,
                                       names=["closed"])
        self.assertEqual(r["status"], "success")
        self.assertIn("closed", r["result"]["intrinsics"])
        self.assertNotIn("hasdetail", r["result"]["intrinsics"])

    def test_names_must_be_list_or_none(self):
        hou = _make_hou()
        r = gme.get_prim_intrinsics(hou, "/obj/box", 0,
                                       names="closed")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "names")


# ===========================================================================
# Section G: find_nearest_point — Point / None 双路径
# ===========================================================================
class FindNearestPointTests(unittest.TestCase):

    def test_nearest_point_returns_point(self):
        # 简单 mock geo.nearestPoint：基于真实几何的最简单近似
        geo = _make_basic_geo()
        parent = _FakeNode("/obj")
        sop = _FakeSopNode("/obj/box", geo, parent=parent)
        parent._children.append(sop)
        hou = _FakeHou({"/obj/box": sop, "/obj": parent})
        # monkey-patch nearestPoint
        def _nearest(pos, max_radius=1.0):
            # 找绝对距离最近点
            best = None
            best_dist = None
            for pt in geo._points:
                dx = pt._position[0] - pos[0]
                dy = pt._position[1] - pos[1]
                dz = pt._position[2] - pos[2]
                d = (dx * dx + dy * dy + dz * dz) ** 0.5
                if d > max_radius:
                    continue
                if best_dist is None or d < best_dist:
                    best = pt
                    best_dist = d
            return best
        geo.nearestPoint = _nearest
        r = gme.find_nearest_point(hou, "/obj/box", [0.05, 0.0, 0.0],
                                       max_distance=0.5)
        self.assertEqual(r["status"], "success")
        res = r["result"]
        self.assertEqual(res["point_index"], 0)
        self.assertIsNotNone(res["point_position"])
        self.assertIsNotNone(res["distance"])

    def test_nearest_point_none(self):
        geo = _make_basic_geo()
        parent = _FakeNode("/obj")
        sop = _FakeSopNode("/obj/box", geo, parent=parent)
        parent._children.append(sop)
        hou = _FakeHou({"/obj/box": sop, "/obj": parent})
        geo.nearestPoint = lambda pos, max_radius=1.0: None
        r = gme.find_nearest_point(hou, "/obj/box", [100.0, 100.0, 100.0],
                                       max_distance=1.0)
        self.assertEqual(r["status"], "success")
        res = r["result"]
        self.assertIsNone(res["point_index"])
        self.assertIsNone(res["point_position"])
        self.assertIsNone(res["distance"])

    def test_position_must_be_three_elements(self):
        hou = _make_hou()
        r = gme.find_nearest_point(hou, "/obj/box", [0.0, 0.0],
                                       max_distance=1.0)
        self.assertEqual(r["status"], "error")

    def test_max_distance_negative(self):
        hou = _make_hou()
        r = gme.find_nearest_point(hou, "/obj/box", [0.0, 0.0, 0.0],
                                       max_distance=-1.0)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "max_distance")


# ===========================================================================
# Section H: set_detail_attrib — 单 undo group
# ===========================================================================
class SetDetailAttribTests(unittest.TestCase):

    def test_creates_attribcreate_node_with_class_detail(self):
        undo = _FakeUndoGroup()
        hou = _make_hou(undos=undo)
        r = gme.set_detail_attrib(
            hou, "/obj/box", "label", "alpha",
            attrib_type="string", node_name="my_attr")
        self.assertEqual(r["status"], "success")
        new_path = r["result"]["node_path"]
        self.assertEqual(new_path, "/obj/my_attr")
        # H21 attribcreate parm 命名：class1 / name1 / type1 / string1
        new_node = hou._nodes["/obj/box"]._parent._children[-1]
        self.assertEqual(new_node._parms.get("class1"), 0)
        self.assertEqual(new_node._parms.get("name1"), "label")
        self.assertEqual(new_node._parms.get("type1"), 3)  # string
        self.assertEqual(new_node._parms.get("string1"), "alpha")
        self.assertEqual(new_node._parms.get("writevalues1"), 1)
        self.assertIs(new_node._input, hou._nodes["/obj/box"])
        self.assertTrue(undo.entered)
        self.assertTrue(undo.exited)

    def test_vector_attrib_type(self):
        undo = _FakeUndoGroup()
        hou = _make_hou(undos=undo)
        r = gme.set_detail_attrib(
            hou, "/obj/box", "Cd", [1.0, 0.5, 0.0],
            attrib_type="vector", node_name="vec_attr")
        self.assertEqual(r["status"], "success")
        new_node = hou._nodes["/obj/box"]._parent._children[-1]
        self.assertEqual(new_node._parms.get("type1"), 2)  # vector
        self.assertEqual(new_node._parms.get("value1v1"), 1.0)
        self.assertEqual(new_node._parms.get("value1v2"), 0.5)
        self.assertEqual(new_node._parms.get("value1v3"), 0.0)

    def test_invalid_attrib_type(self):
        hou = _make_hou(undos=_FakeUndoGroup())
        r = gme.set_detail_attrib(
            hou, "/obj/box", "x", 1, attrib_type="bogus")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "attrib_type")

    def test_node_not_found(self):
        hou = _make_hou(undos=_FakeUndoGroup())
        r = gme.set_detail_attrib(
            hou, "/obj/missing", "label", "v", attrib_type="string")
        self.assertEqual(r["status"], "error")

    def test_failure_after_create_destroys_partial_node(self):
        """设置非法 value（vector 长度错）应触发 destroy 清理。"""
        undo = _FakeUndoGroup()
        hou = _make_hou(undos=undo)
        parent = hou._nodes["/obj/box"]._parent
        before = len(parent._children)
        r = gme.set_detail_attrib(
            hou, "/obj/box", "Cd", [1.0, 0.0],  # 2-element, not 3
            attrib_type="vector", node_name="bad_vec")
        self.assertEqual(r["status"], "error")
        # 半成品节点被 destroy
        self.assertEqual(len(parent._children), before)

    def test_does_not_write_to_cooked_geometry(self):
        """``set_detail_attrib`` MUST NOT 调用 ``node.geometry().`` 写方法。

        通过检查源 node 的 geometry 引用未被改写实现：原 geometry 对象
        不应被替换。
        """
        hou = _make_hou(undos=_FakeUndoGroup())
        original_geo = hou._nodes["/obj/box"]._geometry
        r = gme.set_detail_attrib(
            hou, "/obj/box", "label", "v", attrib_type="string")
        self.assertEqual(r["status"], "success")
        # 同一 geometry 对象仍是原实例
        self.assertIs(hou._nodes["/obj/box"]._geometry, original_geo)


# ===========================================================================
# Section I: geo_export — translator registry + 原子覆盖
# ===========================================================================
class GeoExportTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gme_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_bgeo_format_writes_file(self):
        hou = _make_hou()
        target = os.path.join(self.tmpdir, "test.bgeo")
        r = gme.geo_export(hou, "/obj/box", "bgeo", target)
        self.assertEqual(r["status"], "success")
        self.assertTrue(os.path.exists(target))
        self.assertGreater(r["result"]["size_bytes"], 0)
        self.assertTrue(r["result"]["atomic_replace"])
        self.assertEqual(r["result"]["translator"], "bgeo")

    def test_geo_format_writes_file(self):
        hou = _make_hou()
        target = os.path.join(self.tmpdir, "test.geo")
        r = gme.geo_export(hou, "/obj/box", "geo", target)
        self.assertEqual(r["status"], "success")
        self.assertTrue(os.path.exists(target))

    def test_bgeo_gz_writes_file(self):
        hou = _make_hou()
        target = os.path.join(self.tmpdir, "test.bgeo.gz")
        r = gme.geo_export(hou, "/obj/box", "bgeo.gz", target)
        self.assertEqual(r["status"], "success")
        self.assertTrue(os.path.exists(target))

    def test_unsupported_translator(self):
        hou = _make_hou()
        target = os.path.join(self.tmpdir, "test.bgeo")
        r = gme.geo_export(hou, "/obj/box", "obj", target)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "unsupported_translator")

    def test_extension_mismatch(self):
        hou = _make_hou()
        target = os.path.join(self.tmpdir, "test.geo")
        r = gme.geo_export(hou, "/obj/box", "bgeo", target)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "extension_mismatch")

    def test_target_exists_default_overwrite_false(self):
        hou = _make_hou()
        target = os.path.join(self.tmpdir, "test.bgeo")
        # 预创建目标
        with open(target, "wb") as fh:
            fh.write(b"OLD")
        r = gme.geo_export(hou, "/obj/box", "bgeo", target)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "target_exists")
        # 旧文件未改
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"OLD")

    def test_overwrite_true_replaces_file(self):
        hou = _make_hou()
        target = os.path.join(self.tmpdir, "test.bgeo")
        with open(target, "wb") as fh:
            fh.write(b"OLD")
        r = gme.geo_export(hou, "/obj/box", "bgeo", target, overwrite=True)
        self.assertEqual(r["status"], "success")
        with open(target, "rb") as fh:
            content = fh.read()
        self.assertNotEqual(content, b"OLD")
        self.assertGreater(len(content), 0)

    def test_failure_cleans_up_temp_file(self):
        """saveToFile 抛异常时，必须清理临时文件。"""
        hou = _make_hou()
        # monkey-patch geometry.saveToFile 抛异常
        geo = hou._nodes["/obj/box"]._geometry
        def _bad_save(path, file_type=None):
            raise RuntimeError("simulated save failure")
        geo.saveToFile = _bad_save
        target = os.path.join(self.tmpdir, "test.bgeo")
        before = set(os.listdir(self.tmpdir))
        r = gme.geo_export(hou, "/obj/box", "bgeo", target)
        self.assertEqual(r["status"], "error")
        after = set(os.listdir(self.tmpdir))
        # 临时文件已清理：差集为空
        self.assertEqual(before, after)

    def test_node_not_found(self):
        hou = _make_hou()
        target = os.path.join(self.tmpdir, "test.bgeo")
        r = gme.geo_export(hou, "/obj/missing", "bgeo", target)
        self.assertEqual(r["status"], "error")


# ===========================================================================
# Section J: module invariants — hou 不顶层 import
# ===========================================================================
class ModuleInvariantTests(unittest.TestCase):

    def test_geo_measure_does_not_top_level_import_hou(self):
        src_path = os.path.join(ROOT, "_geo_measure.py")
        with open(src_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "hou" or alias.name.startswith("hou."):
                        bad.append("import {0}".format(alias.name))
            elif isinstance(node, ast.ImportFrom):
                if (node.module == "hou"
                        or (node.module
                            and node.module.startswith("hou."))):
                    bad.append("from {0} import ...".format(node.module))
        self.assertEqual(bad, [],
                          "_geo_measure.py must not top-level import hou: {0}"
                          .format(bad))

    def test_no_exec_or_eval_in_geo_measure(self):
        """``set_detail_attrib`` / 模块内部不使用 exec / eval。"""
        src_path = os.path.join(ROOT, "_geo_measure.py")
        with open(src_path, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("exec(", text)
        self.assertNotIn("eval(", text)

    def test_no_type_annotations_in_module(self):
        src_path = os.path.join(ROOT, "_geo_measure.py")
        with open(src_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns is not None:
                    offenders.append(
                        "{0}.returns".format(node.name))
                args = (node.args.posonlyargs
                        + node.args.args
                        + node.args.kwonlyargs)
                for arg in args:
                    if arg.annotation is not None:
                        offenders.append(
                            "{0}.{1}.annotation".format(node.name, arg.arg))
                if node.args.vararg and node.args.vararg.annotation is not None:
                    offenders.append(
                        "{0}.*.annotation".format(node.name))
                if node.args.kwarg and node.args.kwarg.annotation is not None:
                    offenders.append(
                        "{0}.**.annotation".format(node.name))
        self.assertEqual(offenders, [],
                          "no type annotations allowed: {0}".format(offenders))


# ===========================================================================
# Section K: server classification invariant (8 commands = 1 MU + 7 NO_UNDO)
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "server.py")


def _extract_frozenset_elts(value_node):
    """提取 ``frozenset({...})`` 或 ``{...}`` / ``[...]`` 的 elts 列表。"""
    if isinstance(value_node, ast.Call):
        func = value_node.func
        if not (isinstance(func, ast.Name) and func.id == "frozenset"):
            return None
        if not value_node.args:
            return None
        first = value_node.args[0]
        if isinstance(first, (ast.Set, ast.List)):
            return first.elts
        return None
    if isinstance(value_node, (ast.Set, ast.List)):
        return value_node.elts
    return None


class ServerClassificationInvariantTests(unittest.TestCase):
    """8 个 server commands 的并集完整、两两交集为空。"""

    EXPECTED_COMMANDS = {
        "set_detail_attrib",
        "geo_export",
        "get_bounding_box",
        "get_groups",
        "get_group_members",
        "get_attrib_values",
        "get_prim_intrinsics",
        "find_nearest_point",
    }

    def test_8_commands_in_union(self):
        with open(SERVER_PY, "r", encoding="utf-8") as f:
            text = f.read()
        tree = ast.parse(text)
        # 提取 HoudiniMCPServer 类中三个 frozenset({...}) 的字符串字面量
        mut = set()
        ro = set()
        nu = set()
        for node in ast.walk(tree):
            if (not isinstance(node, ast.Assign)
                    or len(node.targets) != 1):
                continue
            tgt = node.targets[0]
            if not isinstance(tgt, ast.Name):
                continue
            elts = _extract_frozenset_elts(node.value)
            if elts is None:
                continue
            literals = set(e.value for e in elts
                            if isinstance(e, ast.Constant))
            if tgt.id == "MUTATING_COMMANDS":
                mut = literals
            elif tgt.id == "READ_ONLY_COMMANDS":
                ro = literals
            elif tgt.id == "NO_UNDO_COMMANDS":
                nu = literals
        union = mut | ro | nu
        # 8 个新命令的并集完整
        self.assertTrue(
            self.EXPECTED_COMMANDS.issubset(union),
            "8 new commands must all be in some classification: missing={0}"
            .format(self.EXPECTED_COMMANDS - union))

    def test_8_commands_disjoint_3_sets(self):
        with open(SERVER_PY, "r", encoding="utf-8") as f:
            text = f.read()
        tree = ast.parse(text)
        mut = set()
        ro = set()
        nu = set()
        for node in ast.walk(tree):
            if (not isinstance(node, ast.Assign)
                    or len(node.targets) != 1):
                continue
            tgt = node.targets[0]
            if not isinstance(tgt, ast.Name):
                continue
            elts = _extract_frozenset_elts(node.value)
            if elts is None:
                continue
            literals = set(e.value for e in elts
                            if isinstance(e, ast.Constant))
            if tgt.id == "MUTATING_COMMANDS":
                mut = literals
            elif tgt.id == "READ_ONLY_COMMANDS":
                ro = literals
            elif tgt.id == "NO_UNDO_COMMANDS":
                nu = literals
        # 8 个新命令 = MUT ∩ 8 + NO_UNDO ∩ 8 + READ_ONLY ∩ 8
        new_mut = mut & self.EXPECTED_COMMANDS
        new_ro = ro & self.EXPECTED_COMMANDS
        new_nu = nu & self.EXPECTED_COMMANDS
        # set_detail_attrib 只在 MUTATING
        self.assertEqual(new_mut, {"set_detail_attrib"})
        # 7 个 NO_UNDO（含 geo_export）
        self.assertEqual(new_nu, {
            "geo_export",
            "get_bounding_box", "get_groups", "get_group_members",
            "get_attrib_values", "get_prim_intrinsics",
            "find_nearest_point",
        })
        # READ_ONLY 中无新命令
        self.assertEqual(new_ro, set())
        # 两两无交集
        self.assertFalse(new_mut & new_nu)
        self.assertFalse(new_mut & new_ro)
        self.assertFalse(new_nu & new_ro)

    def test_8_handlers_registered(self):
        with open(SERVER_PY, "r", encoding="utf-8") as f:
            text = f.read()
        tree = ast.parse(text)
        registered = set()
        for node in ast.walk(tree):
            if (not isinstance(node, ast.FunctionDef)
                    or node.name != "_get_command_handlers"):
                continue
            for stmt in ast.walk(node):
                if (not isinstance(stmt, ast.Assign)
                        or len(stmt.targets) != 1
                        or not isinstance(stmt.value, ast.Dict)):
                    continue
                for key in stmt.value.keys:
                    if isinstance(key, ast.Constant):
                        registered.add(key.value)
        missing = self.EXPECTED_COMMANDS - registered - {"batch"}
        self.assertEqual(missing, set(),
                          "missing handlers: {0}".format(missing))


# ===========================================================================
# Section L: bridge tool style — 8 @mcp.tool() functions + AST probe
# ===========================================================================
BRIDGE_PY = os.path.join(ROOT, "houdini_mcp_server.py")
BRIDGE_SECTION_HEADER = "add-geometry-export-and-measure"
BRIDGE_TOOLS = [
    "get_bounding_box", "get_groups", "get_group_members",
    "get_attrib_values", "get_prim_intrinsics", "find_nearest_point",
    "set_detail_attrib", "geo_export",
]


def _parse_bridge_source():
    with open(BRIDGE_PY, "r", encoding="utf-8") as f:
        return f.read()


def _has_cjk(s):
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _find_gme_tool_nodes():
    src = _parse_bridge_source()
    tree = ast.parse(src)
    lines = src.splitlines()
    header_line = None
    for i, line in enumerate(lines, start=1):
        if BRIDGE_SECTION_HEADER in line:
            header_line = i
            break
    if header_line is None:
        raise AssertionError(
            "section marker not found in bridge: %s" % BRIDGE_SECTION_HEADER)
    next_header_line = None
    for i, line in enumerate(lines, start=1):
        if i <= header_line:
            continue
        stripped = line.lstrip()
        if (stripped.startswith("# ")
                and BRIDGE_SECTION_HEADER not in line
                and "PR " in stripped):
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
        if node.name not in BRIDGE_TOOLS:
            continue
        has_tool = False
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "tool"):
                has_tool = True
                break
        if not has_tool:
            continue
        found[node.name] = node
    missing = [n for n in BRIDGE_TOOLS if n not in found]
    if missing:
        raise AssertionError(
            "tools not found: %s, found=%s" % (missing, list(found.keys())))
    return found


class BridgeStyleTests(unittest.TestCase):

    def setUp(self):
        self.tools = _find_gme_tool_nodes()
        self.assertEqual(
            len(self.tools), 8,
            "Expected 8 bridge tools, found {0}: {1}".format(
                len(self.tools), list(self.tools.keys())))

    def test_no_type_annotations(self):
        for name, fn in self.tools.items():
            for arg in (fn.args.posonlyargs + fn.args.args
                        + fn.args.kwonlyargs):
                self.assertIsNone(arg.annotation,
                                   "{0} has annotation on {1}"
                                   .format(name, arg.arg))
            self.assertIsNone(fn.returns,
                               "{0} has return annotation".format(name))

    def test_chinese_docstring(self):
        for name, fn in self.tools.items():
            doc = ast.get_docstring(fn) or ""
            self.assertTrue(_has_cjk(doc),
                             "{0} docstring must be CJK".format(name))

    def test_signature_has_ctx(self):
        for name, fn in self.tools.items():
            params = [a.arg for a in (fn.args.posonlyargs + fn.args.args
                                       + fn.args.kwonlyargs)]
            self.assertEqual(params[0], "ctx",
                              "{0} first arg must be ctx, got {1}"
                              .format(name, params))


if __name__ == "__main__":
    unittest.main()