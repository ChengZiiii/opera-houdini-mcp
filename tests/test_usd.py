"""tests/test_usd.py — add-usd-solaris-tools 单测。

覆盖（tasks 4.1 / 4.2）：
- capability / version 探针：Houdini 版本、USD 版本、has_stage、
  LightAPI、具体 light schema。
- 读 cap：max_depth / max_prims / max_attributes / max_arcs / max_lights。
- JSON-safe 转换：pxr 值递归转 list/str。
- 写路径不调用 pxr mutation：mutation tracker 断言
  ``Set`` / ``DefinePrim`` / layer 编辑零调用；只经 LOP authoring。
- ``set_usd_attribute`` 在 adapter value_parm=None 时返回 unsupported。
- ``get_last_modified_prims`` 一律返回 unsupported（不伪造）。
- 15 个 server commands = 3 MUTATING + 12 NO_UNDO；本 change READ_ONLY
  为空；三集合并集完整、两两无交集。
- 15 handler 注册；15 bridge tool 风格（无注解 + 中文 docstring + ctx）。
- 模块不变量：顶层不 import hou、无 exec/eval、无类型注解。

约束：
- stdlib unittest + 简易 hou/pxr mock；不引入新依赖。
- 不依赖真实 Houdini；H21.0 live smoke 由
  ``h21_live_usd_smoke.py`` 单独执行。
"""
import ast
import importlib
import importlib.util
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _ensure_pkg():
    pkg_name = "usd_test_pkg"
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


_common = _ensure_module("_common")
_usd = _ensure_module("_usd")

usd = _usd
cmn = _common


# ===========================================================================
# fake pxr infrastructure（带 mutation tracker）
# ===========================================================================
class _MutationTracker(object):
    """记录 pxr mutation 调用（Set / DefinePrim / layer 编辑）。

    写工具 MUST NOT 触发任何这些调用（R10）。
    """

    def __init__(self):
        self.calls = []

    def note(self, what, *args):
        self.calls.append((what,) + args)


class _FakeSdfPath(object):
    def __init__(self, path):
        self._path = path

    def __str__(self):
        return self._path


class _FakeAttribute(object):
    def __init__(self, name, type_name, value):
        self._name = name
        self._type_name = type_name
        self._value = value

    def GetName(self):
        return self._name

    def GetTypeName(self):
        return self._type_name

    def IsValid(self):
        return True

    def Get(self, time):
        return self._value

    def Set(self, value, time=None):
        # pxr mutation — 写工具 MUST NOT 调用
        _ACTIVE_TRACKER.note("attr.Set", self._name, value)


class _FakePrim(object):
    def __init__(self, path, name, type_name, attrs=None, children=None,
                 lights_as=None, variants=None, active=True, loaded=True):
        self._path = path
        self._name = name
        self._type_name = type_name
        self._attrs = list(attrs or [])
        self._children = list(children or [])
        self._lights_as = lights_as or []
        self._variants = variants or []
        self._active = active
        self._loaded = loaded
        self._has_api = False

    def GetName(self):
        return self._name

    def GetPath(self):
        return _FakeSdfPath(self._path)

    def GetTypeName(self):
        return self._type_name

    def GetKind(self):
        return "component"

    def IsActive(self):
        return self._active

    def IsLoaded(self):
        return self._loaded

    def IsDefined(self):
        return True

    def IsAbstract(self):
        return False

    def IsInstance(self):
        return False

    def IsValid(self):
        return True

    def GetChildren(self):
        return list(self._children)

    def GetAttributes(self):
        return list(self._attrs)

    def GetAttribute(self, name):
        for a in self._attrs:
            if a.GetName() == name:
                return a
        return _MissingAttribute()

    def HasAPI(self, api_cls):
        return self._has_api and api_cls is _FAKE_LIGHT_API

    def IsA(self, schema_cls):
        return schema_cls in self._lights_as

    def GetVariantSets(self):
        return _FakeVariantSets(self._variants)

    # 让 set_usd_attribute 的 authoring 探测不命中 mutation
    def DefinePrim(self, *a, **kw):
        _ACTIVE_TRACKER.note("DefinePrim", self._path)
        return None


class _MissingAttribute(object):
    def IsValid(self):
        return False


class _FakeVariantSets(object):
    def __init__(self, variants):
        self._variants = variants

    def GetNames(self):
        return [v["name"] for v in self._variants]

    def GetVariantSet(self, name):
        for v in self._variants:
            if v["name"] == name:
                return _FakeVariantSet(v)
        return _FakeVariantSet({"name": name, "choices": [], "selected": None})


class _FakeVariantSet(object):
    def __init__(self, spec):
        self._spec = spec

    def GetVariantNames(self):
        return list(self._spec.get("choices", []))

    def GetVariantSelection(self):
        return self._spec.get("selected")


class _FakeLayer(object):
    def __init__(self, identifier="anon:root.usda", real_path="",
                 anonymous=True, default_prim="", sublayers=None):
        self.identifier = identifier
        self.realPath = real_path
        self.anonymous = anonymous
        self.defaultPrim = default_prim
        self.subLayerPaths = list(sublayers or [])


class _FakeCompositionArc(object):
    def __init__(self, arc_type, node_id, root_path):
        self._arc_type = arc_type
        self._node_id = node_id
        self._root_path = root_path

    def GetArcType(self):
        return self._arc_type

    def GetNodeIdentifier(self):
        return self._node_id

    def GetRootPath(self):
        return _FakeSdfPath(self._root_path)


class _FakeCompositionQuery(object):
    def __init__(self, arcs):
        self._arcs = arcs

    def GetCompositionArcs(self):
        return list(self._arcs)


class _FakeStage(object):
    def __init__(self, prims=None, root_layer=None, session_layer=None,
                 layers=None):
        # prims: dict path -> _FakePrim
        self._prims = dict(prims or {})
        self._root = root_layer or _FakeLayer(default_prim="/Asset")
        self._session = session_layer
        self._layers = layers or [self._root]
        self._traverse_list = list(self._prims.values())

    def GetPrimAtPath(self, path):
        return self._prims.get(path)

    def Traverse(self):
        return iter(list(self._traverse_list))

    def GetPseudoRoot(self):
        root = _FakePrim("/", "", "", children=[
            p for p in self._traverse_list
            if str(p.GetPath()).count("/") == 1])
        return root

    def GetRootLayer(self):
        return self._root

    def GetSessionLayer(self):
        return self._session

    def GetLayerStack(self):
        return list(self._layers)

    def GetUpAxis(self):
        return "Y"

    def GetMetersPerUnit(self):
        return 0.01

    def GetFramesPerSecond(self):
        return 24.0

    def GetTimeCodesPerSecond(self):
        return 24.0

    def GetStartTimeCode(self):
        return 100.0

    def GetEndTimeCode(self):
        return 200.0

    # pxr mutation — 写工具 MUST NOT 调用
    def DefinePrim(self, *a, **kw):
        _ACTIVE_TRACKER.note("stage.DefinePrim", a)
        return None

    def GetRootLayer_edit(self, *a, **kw):
        _ACTIVE_TRACKER.note("layer_edit")
        return None


# fake UsdLux schema / API 标记类
_FAKE_LIGHT_API = types.SimpleNamespace(_marker="LightAPI")
_FAKE_DISTANT = types.SimpleNamespace(_marker="DistantLight")
_FAKE_SPHERE = types.SimpleNamespace(_marker="SphereLight")
_FAKE_RECT = types.SimpleNamespace(_marker="RectLight")


_ACTIVE_TRACKER = _MutationTracker()


def _install_fake_pxr(light_api=True, schemas=None, has_composition=True):
    """安装 fake pxr 到 sys.modules，返回 tracker。"""
    global _ACTIVE_TRACKER
    _ACTIVE_TRACKER = _MutationTracker()
    schemas = schemas if schemas is not None else ["DistantLight",
                                                    "SphereLight", "RectLight"]
    fake_usd = types.ModuleType("pxr.Usd")
    fake_usd.GetVersion = lambda: (0, 23, 8)

    class _PCQ(object):
        @staticmethod
        def GetForPrim(prim):
            if has_composition:
                return _FakeCompositionQuery([
                    _FakeCompositionArc("reference", "anon:x.usda",
                                        "/Asset")])
            raise RuntimeError("not available")

    if has_composition:
        fake_usd.PrimCompositionQuery = _PCQ

    fake_usdlux = types.ModuleType("pxr.UsdLux")
    if light_api:
        fake_usdlux.LightAPI = _FAKE_LIGHT_API
    schema_map = {
        "DistantLight": _FAKE_DISTANT,
        "SphereLight": _FAKE_SPHERE,
        "RectLight": _FAKE_RECT,
        "DiskLight": types.SimpleNamespace(_marker="DiskLight"),
        "CylinderLight": types.SimpleNamespace(_marker="CylinderLight"),
        "DomeLight": types.SimpleNamespace(_marker="DomeLight"),
    }
    for name in schemas:
        if name in schema_map:
            setattr(fake_usdlux, name, schema_map[name])

    fake_sdf = types.ModuleType("pxr.Sdf")

    fake_pxr = types.ModuleType("pxr")
    fake_pxr.Usd = fake_usd
    fake_pxr.Sdf = fake_sdf
    fake_pxr.UsdLux = fake_usdlux
    fake_pxr.__path__ = []  # mark as package
    sys.modules["pxr"] = fake_pxr
    sys.modules["pxr.Usd"] = fake_usd
    sys.modules["pxr.Sdf"] = fake_sdf
    sys.modules["pxr.UsdLux"] = fake_usdlux
    return _ACTIVE_TRACKER


def _remove_fake_pxr():
    for key in ("pxr", "pxr.Usd", "pxr.Sdf", "pxr.UsdLux"):
        sys.modules.pop(key, None)


# ===========================================================================
# fake hou infrastructure
# ===========================================================================
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


class _FakeUndoStack(object):
    def __init__(self):
        self._entries = []

    def group(self, label):
        g = _FakeUndoGroup()
        self._entries.append((label, g))
        return g

    def entries(self):
        return len(self._entries)


class _FakeParm(object):
    def __init__(self, owner, name):
        self._owner = owner
        self._name = name

    def set(self, value):
        self._owner._parms[self._name] = value

    def get(self):
        return self._owner._parms.get(self._name)


class _FakeLopNode(object):
    def __init__(self, stage=None):
        self._stage = stage

    def stage(self):
        return self._stage


class _FakeNodeType(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeNodeCategory(object):
    def __init__(self, type_names):
        self._types = {t: object() for t in type_names}

    def nodeTypes(self):
        return dict(self._types)


class _FakeLopContainer(object):
    """可创建子节点的 LOP parent（如 lopnet）。"""

    def __init__(self, path, type_names):
        self._path = path
        self._children = []
        self._type_names = set(type_names)
        self._parms = {}

    def path(self):
        return self._path

    def children(self):
        return list(self._children)

    def createNode(self, node_type, node_name=None):
        if node_type not in self._type_names:
            raise RuntimeError("unknown node_type: %s" % node_type)
        name = node_name or (node_type + "_auto")
        full = (self._path + "/" + name).replace("//", "/")
        node = _FakeCreatedNode(full, node_type, parent=self)
        self._children.append(node)
        return node


class _FakeCreatedNode(object):
    def __init__(self, path, node_type, parent=None):
        self._path = path
        self._node_type = node_type
        self._parent = parent
        self._parms = {}
        self._destroyed = False

    def path(self):
        return self._path

    def type(self):
        return _FakeNodeType(self._node_type)

    def parm(self, name):
        return _FakeParm(self, name)

    def destroy(self):
        self._destroyed = True
        if self._parent is not None and self in self._parent._children:
            self._parent._children.remove(self)


class _FakeLopNodeWithStage(_FakeLopNode):
    def __init__(self, path, stage):
        super(_FakeLopNodeWithStage, self).__init__(stage=stage)
        self._path = path

    def path(self):
        return self._path


class _FakeHou(object):
    def __init__(self, nodes, lop_types=None, undos=None):
        self._nodes = nodes
        self._lop_types = set(lop_types or [])
        self._undos = undos
        self.LopNode = _FakeLopNode

    def node(self, path):
        return self._nodes.get(path)

    def lopNodeTypeCategory(self):
        return _FakeNodeCategory(self._lop_types)

    @property
    def undos(self):
        return self._undos

    def applicationVersion(self):
        return (21, 0, 596)

    def applicationVersionString(self):
        return "21.0.596"


# ===========================================================================
# builder helpers
# ===========================================================================
def _make_stage():
    attr_p = _FakeAttribute("xformOpOrder", "token[]", ["xformOp:translate"])
    light_prim = _FakePrim(
        "/Asset/Lights/Key", "Key", "DistantLight",
        attrs=[attr_p], lights_as=[_FAKE_DISTANT])
    light_prim._has_api = True
    asset = _FakePrim(
        "/Asset", "Asset", "Xform",
        attrs=[attr_p], children=[light_prim])
    root_layer = _FakeLayer(identifier="anon:root.usda",
                            default_prim="/Asset",
                            sublayers=["./sub.usda"])
    stage = _FakeStage(
        prims={"/Asset": asset, "/Asset/Lights/Key": light_prim},
        root_layer=root_layer,
        layers=[root_layer])
    return stage


_LOP_TYPES_FULL = {
    "reference", "sublayer", "editproperties", "configureproperty",
    "configureprimitive", "distantlight", "domelight", "cube", "sphere",
}


def _make_hou(stage=None, lop_types=None, undos=None):
    stage = stage if stage is not None else _make_stage()
    lop_types = lop_types if lop_types is not None else set(_LOP_TYPES_FULL)
    lop = _FakeLopNodeWithStage("/stage/lops/sopinput", stage)
    container = _FakeLopContainer("/stage/lops", lop_types)
    nodes = {
        "/stage/lops/sopinput": lop,
        "/stage/lops": container,
    }
    return _FakeHou(nodes, lop_types=lop_types, undos=undos)


# ===========================================================================
# Section A: capability probe
# ===========================================================================
class CapabilityProbeTests(unittest.TestCase):

    def setUp(self):
        _install_fake_pxr()

    def tearDown(self):
        _remove_fake_pxr()

    def test_probe_reports_versions_and_flags(self):
        hou = _make_hou()
        caps = usd._probe_capabilities(hou)
        self.assertEqual(caps["houdini_version"], "21.0.596")
        self.assertEqual(caps["usd_version"], "0.23.8")
        self.assertTrue(caps["has_stage"])
        self.assertTrue(caps["has_pxr"])
        self.assertTrue(caps["has_light_api"])
        self.assertIn("DistantLight", caps["light_schemas"])

    def test_probe_without_pxr_returns_warning(self):
        _remove_fake_pxr()
        hou = _make_hou()
        caps = usd._probe_capabilities(hou)
        self.assertFalse(caps["has_pxr"])
        self.assertTrue(caps["has_stage"])  # LopNode.stage surface
        self.assertEqual(caps["light_schemas"], [])
        self.assertTrue(any("pxr" in w for w in caps["warnings"]))

    def test_probe_without_light_api(self):
        _remove_fake_pxr()
        _install_fake_pxr(light_api=False, schemas=["DistantLight"])
        hou = _make_hou()
        caps = usd._probe_capabilities(hou)
        self.assertFalse(caps["has_light_api"])
        self.assertIn("DistantLight", caps["light_schemas"])

    def test_probe_version_from_string_fallback(self):
        hou = _make_hou()
        # applicationVersion raises → fallback to string
        def _raise():
            raise RuntimeError("no version tuple")
        hou.applicationVersion = _raise
        hou.applicationVersionString = lambda: "22.0.0"
        caps = usd._probe_capabilities(hou)
        self.assertEqual(caps["houdini_version"], "22.0.0")


# ===========================================================================
# Section B: JSON-safe conversion
# ===========================================================================
class JsonSafeTests(unittest.TestCase):

    def test_scalars_passthrough(self):
        self.assertEqual(usd._jsonable(None), None)
        self.assertEqual(usd._jsonable(True), True)
        self.assertEqual(usd._jsonable(3), 3)
        self.assertEqual(usd._jsonable(1.5), 1.5)
        self.assertEqual(usd._jsonable("x"), "x")

    def test_list_tuple_recursive(self):
        self.assertEqual(usd._jsonable([1, [2, 3]]), [1, [2, 3]])
        self.assertEqual(usd._jsonable((1, 2)), [1, 2])

    def test_iterable_to_floats(self):
        class _Vec(object):
            def __iter__(self):
                return iter([1.0, 2.0, 3.0])
        self.assertEqual(usd._jsonable(_Vec()), [1.0, 2.0, 3.0])

    def test_unserializable_to_str(self):
        class _Bad(object):
            def __iter__(self):
                raise TypeError("nope")
            def __str__(self):
                return "<bad>"
        self.assertEqual(usd._jsonable(_Bad()), "<bad>")


# ===========================================================================
# Section C: read tools — composed read + caps
# ===========================================================================
class ReadToolsTests(unittest.TestCase):

    def setUp(self):
        _install_fake_pxr()

    def tearDown(self):
        _remove_fake_pxr()

    def test_lop_stage_info(self):
        hou = _make_hou()
        r = usd.lop_stage_info(hou, "/stage/lops/sopinput")
        self.assertEqual(r["status"], "success")
        res = r["result"]
        self.assertEqual(res["up_axis"], "Y")
        self.assertEqual(res["meters_per_unit"], 0.01)
        self.assertIn("capability", res)
        self.assertTrue(res["capability"]["has_stage"])

    def test_lop_prim_get(self):
        hou = _make_hou()
        r = usd.lop_prim_get(hou, "/stage/lops/sopinput", "/Asset")
        self.assertEqual(r["status"], "success")
        res = r["result"]
        self.assertEqual(res["name"], "Asset")
        self.assertEqual(res["type"], "Xform")
        self.assertTrue(res["active"])
        self.assertGreaterEqual(len(res["attributes"]), 1)

    def test_lop_prim_get_missing(self):
        hou = _make_hou()
        r = usd.lop_prim_get(hou, "/stage/lops/sopinput", "/Missing")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "prim_path")

    def test_lop_prim_get_max_attributes_cap(self):
        attrs = [_FakeAttribute("attr%d" % i, "float", float(i))
                 for i in range(10)]
        prim = _FakePrim("/Many", "Many", "Xform", attrs=attrs)
        stage = _FakeStage(prims={"/Many": prim})
        hou = _make_hou(stage=stage)
        r = usd.lop_prim_get(hou, "/stage/lops/sopinput", "/Many",
                             max_attributes=3)
        self.assertEqual(r["status"], "success")
        self.assertEqual(len(r["result"]["attributes"]), 3)
        self.assertTrue(r["result"]["attributes_capped"])
        self.assertEqual(r["result"]["attributes_total"], 10)

    def test_lop_prim_search_by_type(self):
        hou = _make_hou()
        r = usd.lop_prim_search(hou, "/stage/lops/sopinput",
                                type_name="DistantLight")
        self.assertEqual(r["status"], "success")
        self.assertEqual(len(r["result"]["matches"]), 1)
        self.assertEqual(r["result"]["matches"][0]["type"], "DistantLight")

    def test_lop_prim_search_by_name(self):
        hou = _make_hou()
        r = usd.lop_prim_search(hou, "/stage/lops/sopinput", name="Key")
        self.assertEqual(r["status"], "success")
        self.assertEqual(len(r["result"]["matches"]), 1)

    def test_list_usd_prims_cap(self):
        children = [_FakePrim("/R/c%d" % i, "c%d" % i, "Xform")
                    for i in range(20)]
        root_child = _FakePrim("/R", "R", "Xform", children=children)
        stage = _FakeStage(prims={"/R": root_child,
                                  **{"/R/c%d" % i: c for i, c in enumerate(children)}})
        stage._traverse_list = [root_child] + children
        hou = _make_hou(stage=stage)
        r = usd.list_usd_prims(hou, "/stage/lops/sopinput",
                               max_depth=1, max_prims=5)
        self.assertEqual(r["status"], "success")
        self.assertLessEqual(len(r["result"]["prims"]), 5)

    def test_get_usd_attribute(self):
        hou = _make_hou()
        r = usd.get_usd_attribute(hou, "/stage/lops/sopinput",
                                  "/Asset", "xformOpOrder", time=0)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["result"]["type"], "token[]")

    def test_get_usd_attribute_missing(self):
        hou = _make_hou()
        r = usd.get_usd_attribute(hou, "/stage/lops/sopinput",
                                  "/Asset", "nope", time=0)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "attribute")

    def test_get_usd_prim_stats(self):
        hou = _make_hou()
        r = usd.get_usd_prim_stats(hou, "/stage/lops/sopinput", "/Asset")
        self.assertEqual(r["status"], "success")
        self.assertTrue(r["result"]["active"])
        self.assertTrue(r["result"]["defined"])

    def test_get_last_modified_prims_always_unsupported(self):
        hou = _make_hou()
        r = usd.get_last_modified_prims(hou, "/stage/lops/sopinput")
        self.assertEqual(r["status"], "unsupported")
        self.assertEqual(r["error"]["code"], "last_modified_unprovable")

    def test_get_usd_composition(self):
        hou = _make_hou()
        r = usd.get_usd_composition(hou, "/stage/lops/sopinput", "/Asset")
        self.assertEqual(r["status"], "success")
        self.assertGreaterEqual(len(r["result"]["arcs"]), 1)

    def test_get_usd_composition_unsupported_when_no_api(self):
        _remove_fake_pxr()
        _install_fake_pxr(has_composition=False)
        hou = _make_hou()
        r = usd.get_usd_composition(hou, "/stage/lops/sopinput", "/Asset")
        self.assertEqual(r["status"], "unsupported")

    def test_get_usd_variants(self):
        prim = _FakePrim("/Var", "Var", "Xform",
                         variants=[{"name": "shading", "choices": ["a", "b"],
                                    "selected": "a"}])
        stage = _FakeStage(prims={"/Var": prim})
        hou = _make_hou(stage=stage)
        r = usd.get_usd_variants(hou, "/stage/lops/sopinput", "/Var")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["result"]["variant_sets"][0]["name"], "shading")
        self.assertEqual(r["result"]["variant_sets"][0]["selected"], "a")

    def test_lop_layer_info(self):
        hou = _make_hou()
        r = usd.lop_layer_info(hou, "/stage/lops/sopinput")
        self.assertEqual(r["status"], "success")
        self.assertGreaterEqual(len(r["result"]["layers"]), 1)

    def test_inspect_usd_layer(self):
        hou = _make_hou()
        r = usd.inspect_usd_layer(hou, "/stage/lops/sopinput")
        self.assertEqual(r["status"], "success")
        root_summary = [l for l in r["result"]["layers"] if l.get("is_root")]
        self.assertEqual(root_summary[0]["sublayer_paths"], ["./sub.usda"])

    def test_list_lights_lightapi_priority(self):
        hou = _make_hou()
        r = usd.list_lights(hou, "/stage/lops/sopinput")
        self.assertEqual(r["status"], "success")
        self.assertGreaterEqual(len(r["result"]["lights"]), 1)
        self.assertEqual(r["result"]["lights"][0]["detected_by"], "LightAPI")

    def test_list_lights_schema_isa_fallback(self):
        # LightAPI disabled, schema IsA still detects
        _remove_fake_pxr()
        _install_fake_pxr(light_api=False, schemas=["DistantLight"])
        prim = _FakePrim("/L", "L", "DistantLight",
                         lights_as=[_FAKE_DISTANT])
        stage = _FakeStage(prims={"/L": prim})
        hou = _make_hou(stage=stage)
        r = usd.list_lights(hou, "/stage/lops/sopinput")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["result"]["lights"][0]["detected_by"], "DistantLight")

    def test_list_lights_no_api_unsupported(self):
        _remove_fake_pxr()
        _install_fake_pxr(light_api=False, schemas=[])
        hou = _make_hou()
        r = usd.list_lights(hou, "/stage/lops/sopinput")
        self.assertEqual(r["status"], "unsupported")

    def test_read_without_pxr_unsupported(self):
        _remove_fake_pxr()
        hou = _make_hou()
        r = usd.lop_stage_info(hou, "/stage/lops/sopinput")
        self.assertEqual(r["status"], "unsupported")
        self.assertEqual(r["error"]["code"], "pxr_unavailable")

    def test_non_lopnode_error(self):
        hou = _make_hou()
        # put a non-LopNode at a path
        container = hou._nodes["/stage/lops"]
        hou._nodes["/obj/geo1"] = container  # not a LopNode instance
        r = usd.lop_stage_info(hou, "/obj/geo1")
        self.assertEqual(r["status"], "error")


# ===========================================================================
# Section D: write tools — 不调用 pxr mutation（R10 核心断言）
# ===========================================================================
class WriteToolsNoPxrMutationTests(unittest.TestCase):

    def setUp(self):
        _install_fake_pxr()

    def tearDown(self):
        _remove_fake_pxr()

    def test_lop_import_creates_node_no_pxr_mutation(self):
        tracker = _ACTIVE_TRACKER
        undo = _FakeUndoStack()
        hou = _make_hou(undos=undo)
        r = usd.lop_import(hou, "/stage/lops", "/data/asset.usd",
                           import_type="reference", prim_path="/Asset",
                           node_name="ref1")
        self.assertEqual(r["status"], "success")
        res = r["result"]
        self.assertEqual(res["adapter"], "reference")
        self.assertEqual(res["node_path"], "/stage/lops/ref1")
        # 单 undo group
        self.assertEqual(undo.entries(), 1)
        # **不**调用任何 pxr mutation
        self.assertEqual(tracker.calls, [],
                         "lop_import MUST NOT call pxr mutation: %s"
                         % tracker.calls)
        # node 创建 + 参数写入
        container = hou._nodes["/stage/lops"]
        created = container._children[-1]
        self.assertEqual(created._parms.get("filepath1"), "/data/asset.usd")
        self.assertEqual(created._parms.get("primpath"), "/Asset")
        self.assertEqual(created._parms.get("num_files"), 1)

    def test_lop_import_sublayer(self):
        tracker = _ACTIVE_TRACKER
        hou = _make_hou(undos=_FakeUndoStack())
        r = usd.lop_import(hou, "/stage/lops", "/data/sub.usd",
                           import_type="sublayer")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["result"]["adapter"], "sublayer")
        self.assertEqual(tracker.calls, [])

    def test_lop_import_no_adapter_unsupported(self):
        # lop_types 不含 reference/sublayer
        hou = _make_hou(lop_types={"cube"}, undos=_FakeUndoStack())
        r = usd.lop_import(hou, "/stage/lops", "/data/asset.usd")
        self.assertEqual(r["status"], "unsupported")
        self.assertEqual(r["error"]["code"], "no_import_adapter")

    def test_lop_import_invalid_import_type(self):
        hou = _make_hou(undos=_FakeUndoStack())
        r = usd.lop_import(hou, "/stage/lops", "/data/asset.usd",
                           import_type="bogus")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "import_type")

    def test_set_usd_attribute_unsupported_when_no_value_parm(self):
        """H21 Edit Properties 无干净 value 参数 → unsupported（不 fallback pxr）。"""
        tracker = _ACTIVE_TRACKER
        hou = _make_hou(undos=_FakeUndoStack())
        r = usd.set_usd_attribute(hou, "/stage/lops", "/Asset",
                                  "displayColor", [1.0, 0.0, 0.0],
                                  attribute_type="vector")
        self.assertEqual(r["status"], "unsupported")
        self.assertEqual(r["error"]["code"], "attr_value_mapping_unsupported")
        # 不创建节点、不调用 pxr
        container = hou._nodes["/stage/lops"]
        self.assertEqual(len(container._children), 0)
        self.assertEqual(tracker.calls, [])

    def test_set_usd_attribute_no_adapter_unsupported(self):
        hou = _make_hou(lop_types={"cube"}, undos=_FakeUndoStack())
        r = usd.set_usd_attribute(hou, "/stage/lops", "/Asset", "x", 1.0)
        self.assertEqual(r["status"], "unsupported")
        self.assertEqual(r["error"]["code"], "no_attr_adapter")

    def test_set_usd_attribute_invalid_type(self):
        hou = _make_hou(undos=_FakeUndoStack())
        r = usd.set_usd_attribute(hou, "/stage/lops", "/Asset", "x", 1,
                                  attribute_type="bogus")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["field"], "attribute_type")

    def test_create_lop_node_creates_no_pxr(self):
        tracker = _ACTIVE_TRACKER
        hou = _make_hou(undos=_FakeUndoStack())
        r = usd.create_lop_node(hou, "/stage/lops", "distantlight",
                                node_name="key1")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["result"]["resolved_type"], "distantlight")
        self.assertEqual(tracker.calls, [])

    def test_create_lop_node_unknown_type_unsupported(self):
        hou = _make_hou(undos=_FakeUndoStack())
        r = usd.create_lop_node(hou, "/stage/lops", "bogus_type")
        self.assertEqual(r["status"], "unsupported")
        self.assertEqual(r["error"]["code"], "unknown_lop_node_type")

    def test_create_lop_node_failure_destroys_partial(self):
        # container 拒绝创建（type 在 registry 但 createNode 抛异常）
        class _BadContainer(_FakeLopContainer):
            def createNode(self, node_type, node_name=None):
                raise RuntimeError("simulated create failure")
        bad = _BadContainer("/stage/lops", _LOP_TYPES_FULL)
        hou = _FakeHou({"/stage/lops": bad},
                       lop_types=_LOP_TYPES_FULL, undos=_FakeUndoStack())
        r = usd.create_lop_node(hou, "/stage/lops", "distantlight")
        self.assertEqual(r["status"], "error")


# ===========================================================================
# Section E: module invariants
# ===========================================================================
class ModuleInvariantTests(unittest.TestCase):

    def test_usd_does_not_top_level_import_hou(self):
        src_path = os.path.join(ROOT, "_usd.py")
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
                          "_usd.py must not top-level import hou: {0}"
                          .format(bad))

    def test_no_exec_or_eval_in_usd(self):
        src_path = os.path.join(ROOT, "_usd.py")
        with open(src_path, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("exec(", text)
        self.assertNotIn("eval(", text)

    def test_no_type_annotations_in_module(self):
        src_path = os.path.join(ROOT, "_usd.py")
        with open(src_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns is not None:
                    offenders.append("{0}.returns".format(node.name))
                args = (node.args.posonlyargs
                        + node.args.args
                        + node.args.kwonlyargs)
                for arg in args:
                    if arg.annotation is not None:
                        offenders.append(
                            "{0}.{1}.annotation".format(node.name, arg.arg))
        self.assertEqual(offenders, [],
                          "no type annotations allowed: {0}".format(offenders))

    def test_no_hou_LOPStage_api_usage(self):
        """实现 MUST NOT 使用不存在的 hou.LOPStage 作为 API。

        docstring / 注释中说明「不使用 hou.LOPStage」是允许的；这里只
        断言代码不存在 ``.LOPStage`` 属性访问或 ``getattr(..., "LOPStage")``。
        """
        src_path = os.path.join(ROOT, "_usd.py")
        with open(src_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad = []
        for node in ast.walk(tree):
            # x.LOPStage 属性访问（排除字符串/注释）
            if isinstance(node, ast.Attribute) and node.attr == "LOPStage":
                bad.append("attribute access .LOPStage at line %d"
                           % node.lineno)
            # getattr(..., "LOPStage")
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Name) and func.id == "getattr"
                        and node.args):
                    arg = node.args[1]
                    if (isinstance(arg, ast.Constant)
                            and arg.value == "LOPStage"):
                        bad.append('getattr(..., "LOPStage") at line %d'
                                   % node.lineno)
        self.assertEqual(bad, [],
                          "_usd.py must not use nonexistent hou.LOPStage: {0}"
                          .format(bad))


# ===========================================================================
# Section F: server classification invariant (15 = 3 MU + 12 NO_UNDO)
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "server.py")

USD_EXPECTED_MUTATING = {"lop_import", "set_usd_attribute", "create_lop_node"}
USD_EXPECTED_NO_UNDO = {
    "lop_stage_info", "lop_prim_get", "lop_prim_search",
    "lop_layer_info", "list_usd_prims", "get_usd_attribute",
    "get_usd_prim_stats", "get_last_modified_prims",
    "get_usd_composition", "get_usd_variants", "inspect_usd_layer",
    "list_lights",
}
USD_ALL_15 = USD_EXPECTED_MUTATING | USD_EXPECTED_NO_UNDO


def _extract_frozenset_elts(value_node):
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


def _parse_classifications():
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
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
    return mut, ro, nu


class ServerClassificationTests(unittest.TestCase):

    def test_15_commands_in_union(self):
        mut, ro, nu = _parse_classifications()
        union = mut | ro | nu
        self.assertEqual(USD_ALL_15 - union, set(),
                         "15 commands must all be classified: missing={0}"
                         .format(USD_ALL_15 - union))

    def test_3_mutating_exactly(self):
        mut, ro, nu = _parse_classifications()
        self.assertEqual(mut & USD_ALL_15, USD_EXPECTED_MUTATING)

    def test_12_no_undo_exactly(self):
        mut, ro, nu = _parse_classifications()
        self.assertEqual(nu & USD_ALL_15, USD_EXPECTED_NO_UNDO)

    def test_zero_read_only_for_usd(self):
        mut, ro, nu = _parse_classifications()
        self.assertEqual(ro & USD_ALL_15, set())

    def test_three_sets_pairwise_disjoint_for_usd(self):
        mut, ro, nu = _parse_classifications()
        new_mut = mut & USD_ALL_15
        new_ro = ro & USD_ALL_15
        new_nu = nu & USD_ALL_15
        self.assertFalse(new_mut & new_nu)
        self.assertFalse(new_mut & new_ro)
        self.assertFalse(new_nu & new_ro)

    def test_15_handlers_registered(self):
        with open(SERVER_PY, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
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
        missing = USD_ALL_15 - registered
        self.assertEqual(missing, set(),
                         "missing handlers: {0}".format(missing))


# ===========================================================================
# Section G: bridge tool style — 15 @mcp.tool() functions
# ===========================================================================
BRIDGE_PY = os.path.join(ROOT, "houdini_mcp_server.py")
BRIDGE_SECTION_HEADER = "add-usd-solaris-tools"
BRIDGE_TOOLS = sorted(USD_ALL_15)


def _parse_bridge_source():
    with open(BRIDGE_PY, "r", encoding="utf-8") as f:
        return f.read()


def _has_cjk(s):
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _find_usd_tool_nodes():
    src = _parse_bridge_source()
    tree = ast.parse(src)
    lines = src.splitlines()
    header_line = None
    for i, line in enumerate(lines, start=1):
        if BRIDGE_SECTION_HEADER in line and "bridge tool" in line:
            header_line = i
            break
    if header_line is None:
        # fallback: any line with header
        for i, line in enumerate(lines, start=1):
            if BRIDGE_SECTION_HEADER in line:
                header_line = i
                break
    if header_line is None:
        raise AssertionError(
            "section marker not found in bridge: %s" % BRIDGE_SECTION_HEADER)
    # next section header: a "# PR" or "# ---" block not our header
    next_header_line = None
    for i, line in enumerate(lines, start=1):
        if i <= header_line:
            continue
        stripped = line.lstrip()
        if (stripped.startswith("# ")
                and BRIDGE_SECTION_HEADER not in line
                and ("PR " in stripped or "Diagnostic Tools" in stripped)):
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
        self.tools = _find_usd_tool_nodes()
        self.assertEqual(len(self.tools), 15,
                         "Expected 15 bridge tools, found {0}: {1}".format(
                             len(self.tools), list(self.tools.keys())))

    def test_no_type_annotations(self):
        for name, fn in self.tools.items():
            for arg in (fn.args.posonlyargs + fn.args.args
                        + fn.args.kwonlyargs):
                self.assertIsNone(arg.annotation,
                                   "{0} has annotation on {1}".format(
                                       name, arg.arg))
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
                               "{0} first arg must be ctx, got {1}".format(
                                   name, params))


if __name__ == "__main__":
    unittest.main()
