"""add-cops-tools 单元测试。

覆盖 7 个 Copernicus 查询/控制 API、H21+ CopNode 类型边界、旧 COP2 拒绝、
flag 原子预校验、response cap、禁用旧/虚构 API，以及 server/bridge 注册
三分类唯一穷尽互斥。真实 H21 Copernicus 网络由 h21_live_cops_smoke.py 覆盖。
"""
import ast
import builtins
import importlib.util
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COPS_PATH = os.path.join(ROOT, "_cops.py")
SERVER_PATH = os.path.join(ROOT, "server.py")
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


# ---------------------------------------------------------------------------
# Mock objects — 模拟 H21+ Copernicus 真实 surface。mock 自身不建立门禁。
# ---------------------------------------------------------------------------
class _CopNodeBase(object):
    """hou.CopNode 基类；mock Copernicus 节点继承它。"""
    pass


class _LegacyCop2Node(object):
    """模拟旧 COP2 节点（非 CopNode）。"""
    pass


class _TypeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _NodeType(object):
    def __init__(self, name, label=""):
        self._name = name
        self._label = label

    def name(self):
        return self._name

    def label(self):
        return self._label


class _TypeObj(object):
    def __init__(self, name, category_name="Cop"):
        self._name = name
        self._category = _TypeCategory(category_name)

    def name(self):
        return self._name

    def category(self):
        return self._category


class _TypeRegistry(object):
    def __init__(self, names):
        self._types = {name: _NodeType(name) for name in names}

    def nodeTypes(self):
        return dict(self._types)


class _Geometry(object):
    """模拟 hou.Geometry；提供有界 counts/bbox/attribs。"""

    def __init__(self):
        self._big = list(range(10000))  # 不应被序列化

    def numPoints(self):
        return 12

    def numPrims(self):
        return 3

    def numVertices(self):
        return 24

    def boundingBox(self):
        return (-1.0, -2.0, -3.0, 1.0, 2.0, 3.0)

    def pointAttribs(self):
        return (_NodeType("P", "Position"), _NodeType("v", "Velocity"))

    def primAttribs(self):
        return ()

    def vertexAttribs(self):
        return ()

    def globalAttribs(self):
        return (_NodeType("id", "Detail id"),)


class _Layer(object):
    def resolution(self):
        return (1920, 1080)

    def storage(self):
        return "Float32"

    def bounds(self):
        return (0.0, 0.0, 1.0, 1.0)


class _Vdb(object):
    def name(self):
        return "density"

    def storage(self):
        return "NanoVDB float"

    def bounds(self):
        return (-5.0, -5.0, -5.0, 5.0, 5.0, 5.0)


class _Cable(object):
    """模拟 Copernicus cable；公开 image/vdb 属性供反射 wire 选择。"""

    def imageLayer(self):
        return _Layer()

    def nanoVdb(self):
        return _Vdb()

    def resolution(self):
        return (512, 512)


class _CopNode(_CopNodeBase):
    """Copernicus 节点 mock；按 output_index 返不同 payload。"""

    def __init__(self, path="/img/copnet1/blur", category_name="Cop"):
        self._path = path
        self._type = _TypeObj("copnet::blur", category_name)
        self._geometry = _Geometry()
        self._layer = _Layer()
        self._vdb = _Vdb()
        self._cable = _Cable()
        self._errors = ()
        self._warnings = ()
        self.flag_calls = []
        self.created = None
        self._flags = {
            "display": False, "export": False, "template": False,
            "selectable_template": False, "compress": False, "bypass": False}

    def path(self):
        return self._path

    def type(self):
        return self._type

    def errors(self):
        return self._errors

    def warnings(self):
        return self._warnings

    def inputDataTypes(self):
        return ("Image",)

    def outputDataTypes(self):
        return ("Image", "Geometry")

    def outputCableStructure(self):
        return {"image": "Image", "geo": "Geometry"}

    def cable(self, output_index):
        return self._cable

    def geometry(self, output_index):
        return self._geometry

    def geometryAtFrame(self, output_index, frame):
        return self._geometry

    def layer(self, output_index):
        return self._layer

    def vdb(self, output_index):
        return self._vdb

    # flag setters
    def setDisplayFlag(self, value):
        self.flag_calls.append(("display", value))

    def setExportFlag(self, value):
        self.flag_calls.append(("export", value))

    def setTemplateFlag(self, value):
        self.flag_calls.append(("template", value))

    def setSelectableTemplateFlag(self, value):
        self.flag_calls.append(("selectable_template", value))

    def setCompressFlag(self, value):
        self.flag_calls.append(("compress", value))

    def bypass(self, value):
        self.flag_calls.append(("bypass", value))


class _CopNetParent(object):
    """Copernicus network parent（childTypeCategory == "Cop"）。"""

    def __init__(self, path="/img/copnet1", editable=True,
                 category_name="Cop"):
        self._path = path
        self._editable = editable
        self._category_name = category_name
        self.created_nodes = []

    def path(self):
        return self._path

    def isEditable(self):
        return self._editable

    def childTypeCategory(self):
        return _TypeCategory(self._category_name)

    def createNode(self, node_type, node_name):
        child = _CopNode(self._path.rstrip("/") + "/" + (node_name or "child"))
        self.created_nodes.append((node_type, node_name, child))
        return child


class _Hou(object):
    CopNode = _CopNodeBase
    # legacy classes（用于 _legacy_cop2_classes 探针）
    Cop2_Node = _LegacyCop2Node

    def __init__(self):
        self._nodes = {}
        self._registries = {"Cop": _TypeRegistry(
            ["copnet::blur", "copnet::null", "copnet::tonemap"])}

    def node(self, path):
        return self._nodes.get(path)

    def applicationVersion(self):
        return (21, 0, 596)

    # H21 真实 surface：Cop category 走专用 hou.copNodeTypeCategory()，
    # 不是 hou.nodeTypeCategory()（后者在 H21 不存在）。
    def copNodeTypeCategory(self):
        return self._registries.get("Cop")

    def nodeTypeCategories(self):
        return dict(self._registries)

    def register(self, path, node):
        self._nodes[path] = node


# ---------------------------------------------------------------------------
# module loader（与 test_dops 风格一致：隔离 _common + _cops）
# ---------------------------------------------------------------------------
_pkg = types.ModuleType("cops_test_pkg")
_pkg.__path__ = [ROOT]
sys.modules["cops_test_pkg"] = _pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_cops():
    common_name = "cops_test_pkg._common"
    cops_name = "cops_test_pkg._cops"
    for name in (cops_name, common_name):
        sys.modules.pop(name, None)
    _load(common_name, os.path.join(ROOT, "_common.py"))
    return _load(cops_name, COPS_PATH)


def _make_hou():
    hou = _Hou()
    cop_node = _CopNode()
    hou.register("/img/copnet1/blur", cop_node)
    parent = _CopNetParent()
    hou.register("/img/copnet1", parent)
    hou.register("/img/copnet1/locked_parent", _CopNetParent(
        "/img/copnet1/locked_parent", editable=False))
    hou.register("/obj/geo1", _CopNetParent(
        "/obj/geo1", category_name="Object"))
    # legacy COP2 节点
    legacy = _LegacyCop2Node()
    legacy.path = lambda: "/img/legacy_cop2"
    legacy.type = lambda: _TypeObj("rop_comp", "Cop2")
    hou.register("/img/legacy_cop2", legacy)
    return hou, cop_node, parent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
COPS_7 = {
    "get_cop_info", "get_cop_geometry", "get_cop_layer", "get_cop_vdb",
    "create_cop_node", "set_cop_flags", "list_cop_node_types",
}
COPS_MUT = {"create_cop_node", "set_cop_flags"}
COPS_NO_UNDO = {"get_cop_info", "get_cop_geometry", "get_cop_layer",
                "get_cop_vdb"}
COPS_RO = {"list_cop_node_types"}


class CopsQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cops = _load_cops()

    def test_module_does_not_import_hou_or_new_dependencies(self):
        source = open(COPS_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        self.assertNotIn("hou", imports)
        # math 是标准库；_common 是包内模块
        self.assertTrue(imports <= {"math", ""})

    def test_forbidden_fictional_apis_absent(self):
        source = open(COPS_PATH, "r", encoding="utf-8").read()
        for forbidden in ("copInfo", "imagePlaneInfo", "passThrough"):
            self.assertNotIn(forbidden, source,
                             "forbidden legacy/fictional API: " + forbidden)

    def test_invalid_path_is_structured(self):
        hou, _, _ = _make_hou()
        missing = self.cops.get_cop_info(hou, "/missing")
        self.assertEqual(missing["error"]["code"], "node_not_found")
        empty = self.cops.get_cop_info(hou, "")
        self.assertEqual(empty["error"]["code"], "invalid_node_path")

    def test_legacy_cop2_is_unsupported(self):
        hou, _, _ = _make_hou()
        for fn_name, args in (
                ("get_cop_info", ("/img/legacy_cop2",)),
                ("get_cop_geometry", ("/img/legacy_cop2", 0)),
                ("get_cop_layer", ("/img/legacy_cop2", 0)),
                ("get_cop_vdb", ("/img/legacy_cop2", 0)),
                ("set_cop_flags",
                 ("/img/legacy_cop2", {"display": True}))):
            result = getattr(self.cops, fn_name)(hou, *args)
            self.assertEqual(result["error"]["code"],
                             "unsupported_legacy_cop2", fn_name)

    def test_not_a_cop_node_is_unsupported(self):
        hou, _, _ = _make_hou()
        result = self.cops.get_cop_info(hou, "/obj/geo1")
        self.assertEqual(result["error"]["code"], "not_a_cop_node")

    def test_get_cop_info_reads_types_and_cable(self):
        hou, _, _ = _make_hou()
        result = self.cops.get_cop_info(hou, "/img/copnet1/blur")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["input_data_types"], ["Image"])
        self.assertEqual(result["output_data_types"], ["Image", "Geometry"])
        self.assertTrue(result["cable_structure"]["available"])
        outputs = result["outputs"]
        self.assertTrue(outputs)
        self.assertTrue(outputs[0]["cable_available"])
        # cable metadata surface 由反射探针如实汇报
        self.assertIn("surface", outputs[0]["cable_metadata"])

    def test_get_cop_geometry_returns_bounded_summary(self):
        hou, _, _ = _make_hou()
        result = self.cops.get_cop_geometry(hou, "/img/copnet1/blur", 0)
        self.assertEqual(result["status"], "success")
        geo = result["geometry"]
        self.assertTrue(geo["available"])
        self.assertEqual(geo["point_count"], 12)
        self.assertEqual(geo["prim_count"], 3)
        self.assertEqual(geo["bbox"], [-1.0, -2.0, -3.0, 1.0, 2.0, 3.0])
        # 不回传完整几何：mock 内部 10000 元素大数组不应泄漏到响应
        self.assertNotIn("9999", str(result))

    def test_get_cop_geometry_at_frame_uses_atframe_entry(self):
        hou, _, _ = _make_hou()
        result = self.cops.get_cop_geometry(
            hou, "/img/copnet1/blur", 0, frame=5.0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["geometry_entry"], "geometryAtFrame")
        self.assertEqual(result["frame"], 5.0)

    def test_get_cop_layer_uses_official_entry(self):
        hou, _, _ = _make_hou()
        result = self.cops.get_cop_layer(hou, "/img/copnet1/blur", 0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["layer_entry"], "layer")
        self.assertFalse(result["cable_fallback_used"])
        self.assertEqual(result["layer"]["resolution"], [1920, 1080])

    def test_get_cop_vdb_uses_official_entry(self):
        hou, _, _ = _make_hou()
        result = self.cops.get_cop_vdb(hou, "/img/copnet1/blur", 0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["vdb_entry"], "vdb")
        self.assertFalse(result["cable_fallback_used"])
        self.assertEqual(result["vdb"]["grid_name"], "density")

    def test_invalid_output_index_and_frame_rejected(self):
        hou, _, _ = _make_hou()
        bad_index = self.cops.get_cop_geometry(
            hou, "/img/copnet1/blur", output_index=-1)
        self.assertEqual(bad_index["error"]["code"], "invalid_output_index")
        bad_frame = self.cops.get_cop_layer(
            hou, "/img/copnet1/blur", 0, frame="x")
        self.assertEqual(bad_frame["error"]["code"], "invalid_frame")


class CopsCableFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cops = _load_cops()

    def test_layer_falls_back_to_cable_wire_when_official_entry_missing(self):
        hou, _, _ = _make_hou()

        class _NoLayerNode(_CopNode):
            def layer(self, output_index):
                raise AttributeError("no layer entry")

        node = _NoLayerNode("/img/copnet1/nolayer")
        hou.register("/img/copnet1/nolayer", node)
        result = self.cops.get_cop_layer(hou, "/img/copnet1/nolayer", 0)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["cable_fallback_used"])
        # fallback 选择 cable 上 imageLayer wire，返回其 ImageLayer metadata
        self.assertEqual(result["layer"]["resolution"], [1920, 1080])

    def test_vdb_falls_back_to_cable_wire_when_official_entry_missing(self):
        hou, _, _ = _make_hou()

        class _NoVdbNode(_CopNode):
            def vdb(self, output_index):
                raise AttributeError("no vdb entry")

        node = _NoVdbNode("/img/copnet1/novdb")
        hou.register("/img/copnet1/novdb", node)
        result = self.cops.get_cop_vdb(hou, "/img/copnet1/novdb", 0)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["cable_fallback_used"])

    def test_layer_unavailable_when_neither_entry_nor_wire(self):
        hou, _, _ = _make_hou()

        class _BareNode(_CopNode):
            def layer(self, output_index):
                raise AttributeError()

            class _BareCable(object):
                pass

            def cable(self, output_index):
                return self._BareCable()

        node = _BareNode("/img/copnet1/bare")
        hou.register("/img/copnet1/bare", node)
        result = self.cops.get_cop_layer(hou, "/img/copnet1/bare", 0)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["_warning"]["code"], "layer_unavailable")


class CopsCreateFlagsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cops = _load_cops()

    def test_create_validates_parent_editable_and_category(self):
        hou, _, parent = _make_hou()
        result = self.cops.create_cop_node(
            hou, "/img/copnet1", "copnet::blur", node_name="created1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["created_path"], "/img/copnet1/created1")
        self.assertEqual(parent.created_nodes[0][0], "copnet::blur")

        locked = self.cops.create_cop_node(
            hou, "/img/copnet1/locked_parent", "copnet::blur")
        self.assertEqual(locked["error"]["code"], "parent_locked")

        bad_cat = self.cops.create_cop_node(
            hou, "/obj/geo1", "copnet::blur")
        self.assertEqual(bad_cat["error"]["code"],
                         "unsupported_parent_category")

    def test_create_rejects_unknown_node_type(self):
        hou, _, _ = _make_hou()
        result = self.cops.create_cop_node(
            hou, "/img/copnet1", "bogus::missing")
        self.assertEqual(result["error"]["code"], "node_type_unavailable")

    def test_flags_whitelist_and_atomic_pre_validation(self):
        hou, cop_node, _ = _make_hou()
        result = self.cops.set_cop_flags(
            hou, "/img/copnet1/blur",
            {"display": True, "export": False})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["applied_flags"], ["display", "export"])
        self.assertEqual(cop_node.flag_calls,
                         [("display", True), ("export", False)])

    def test_flags_unknown_key_rejected_before_any_write(self):
        hou, cop_node, _ = _make_hou()
        result = self.cops.set_cop_flags(
            hou, "/img/copnet1/blur",
            {"display": True, "bogus": True})
        self.assertEqual(result["error"]["code"], "unsupported_flag")
        # 原子性：display 不应被写入
        self.assertEqual(cop_node.flag_calls, [])

    def test_flags_non_bool_value_rejected(self):
        hou, cop_node, _ = _make_hou()
        result = self.cops.set_cop_flags(
            hou, "/img/copnet1/blur", {"display": "yes"})
        self.assertEqual(result["error"]["code"], "invalid_flag_value")
        self.assertEqual(cop_node.flag_calls, [])

    def test_flags_empty_or_non_dict_rejected(self):
        hou, _, _ = _make_hou()
        for flags in ({}, None, "display"):
            result = self.cops.set_cop_flags(
                hou, "/img/copnet1/blur", flags)
            self.assertEqual(result["error"]["code"], "invalid_flags")

    def test_flags_full_whitelist_mapped_to_setters(self):
        hou, cop_node, _ = _make_hou()
        result = self.cops.set_cop_flags(
            hou, "/img/copnet1/blur",
            {"display": True, "export": True, "template": True,
             "selectable_template": True, "compress": True, "bypass": True})
        self.assertEqual(result["status"], "success")
        applied = dict(cop_node.flag_calls)
        self.assertEqual(applied, {
            "display": True, "export": True, "template": True,
            "selectable_template": True, "compress": True, "bypass": True})


class CopsListTypesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cops = _load_cops()

    def test_list_node_types_enumerates_cop_registry(self):
        hou, _, _ = _make_hou()
        result = self.cops.list_cop_node_types(hou, "Cop")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["category"], "Cop")
        names = [item["name"] for item in result["node_types"]]
        self.assertIn("copnet::blur", names)
        self.assertEqual(result["total"], 3)

    def test_list_node_types_rejects_cop2_legacy(self):
        hou, _, _ = _make_hou()
        result = self.cops.list_cop_node_types(hou, "Cop2")
        self.assertEqual(result["error"]["code"], "unsupported_legacy_cop2")

    def test_list_node_types_rejects_unknown_category(self):
        hou, _, _ = _make_hou()
        result = self.cops.list_cop_node_types(hou, "Object")
        self.assertEqual(result["error"]["code"], "unsupported_category")

    def test_list_node_types_warns_when_registry_missing(self):
        hou, _, _ = _make_hou()

        class _NoRegistryHou(_Hou):
            def copNodeTypeCategory(self):
                return None

            def nodeTypeCategories(self):
                return {}

        no_reg_hou = _NoRegistryHou()
        result = self.cops.list_cop_node_types(no_reg_hou, "Cop")
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["_warning"]["code"],
                         "cop_category_unavailable")


class ResponseCapTests(unittest.TestCase):
    def test_all_seven_public_functions_cap_success_and_error_paths(self):
        cops = _load_cops()
        original = cops.cmn.apply_response_cap
        calls = []

        def cap(value, max_bytes=16384):
            calls.append(value.get("status") if isinstance(value, dict)
                         else None)
            result = dict(value)
            result["_cap_test"] = True
            return result

        cops.cmn.apply_response_cap = cap
        try:
            hou, _, _ = _make_hou()
            results = [
                cops.get_cop_info(hou, "/img/copnet1/blur"),
                cops.get_cop_geometry(hou, "/img/copnet1/blur", 0),
                cops.get_cop_layer(hou, "/img/copnet1/blur", 0),
                cops.get_cop_vdb(hou, "/img/copnet1/blur", 0),
                cops.create_cop_node(hou, "/img/copnet1", "copnet::blur"),
                cops.set_cop_flags(hou, "/img/copnet1/blur",
                                   {"display": True}),
                cops.list_cop_node_types(hou, "Cop"),
                # error paths
                cops.get_cop_info(hou, "/missing"),
                cops.set_cop_flags(hou, "/img/copnet1/blur",
                                   {"bogus": True}),
            ]
        finally:
            cops.cmn.apply_response_cap = original
        self.assertEqual(len(calls), 9)
        self.assertTrue(all(result["_cap_test"] for result in results))


# ---------------------------------------------------------------------------
# Registration / classification tests
# ---------------------------------------------------------------------------
def _class_sets_and_handlers():
    source = open(SERVER_PATH, "r", encoding="utf-8").read()
    tree = ast.parse(source)
    class_node = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef)
                      and node.name == "HoudiniMCPServer")
    values = {}
    handlers = set()
    for node in class_node.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in (
                    "MUTATING_COMMANDS", "READ_ONLY_COMMANDS",
                    "NO_UNDO_COMMANDS")):
            literal = node.value.args[0]
            values[node.targets[0].id] = {
                elt.value for elt in literal.elts
                if isinstance(elt, ast.Constant)
            }
        if isinstance(node, ast.FunctionDef) and node.name == "_get_command_handlers":
            for child in ast.walk(node):
                if isinstance(child, ast.Dict):
                    for key in child.keys:
                        if isinstance(key, ast.Constant):
                            handlers.add(key.value)
    return values, handlers, class_node


class RegistrationPolicyTests(unittest.TestCase):
    def test_seven_commands_are_pairwise_disjoint_and_exhaustive(self):
        values, handlers, _ = _class_sets_and_handlers()
        mut = values["MUTATING_COMMANDS"] & COPS_7
        ro = values["READ_ONLY_COMMANDS"] & COPS_7
        no_undo = values["NO_UNDO_COMMANDS"] & COPS_7
        self.assertEqual(mut, COPS_MUT)
        self.assertEqual(ro, COPS_RO)
        self.assertEqual(no_undo, COPS_NO_UNDO)
        self.assertEqual(mut | ro | no_undo, COPS_7)
        self.assertFalse(mut & ro)
        self.assertFalse(mut & no_undo)
        self.assertFalse(ro & no_undo)
        self.assertTrue(COPS_7 <= handlers)

    def test_server_handlers_apply_cap(self):
        source = open(SERVER_PATH, "r", encoding="utf-8").read()
        _, _, class_node = _class_sets_and_handlers()
        functions = {node.name: node for node in class_node.body
                     if isinstance(node, ast.FunctionDef)}
        for command in COPS_7:
            fn = functions["handle_" + command]
            segment = ast.get_source_segment(source, fn) or ""
            self.assertIn("cmn.apply_response_cap", segment, command)

    def test_read_only_and_no_undo_handlers_never_create_undo_group(self):
        source = open(SERVER_PATH, "r", encoding="utf-8").read()
        _, _, class_node = _class_sets_and_handlers()
        functions = {node.name: node for node in class_node.body
                     if isinstance(node, ast.FunctionDef)}
        for command in COPS_NO_UNDO | COPS_RO:
            fn = functions["handle_" + command]
            segment = ast.get_source_segment(source, fn) or ""
            self.assertNotIn("undos.group", segment, command)

    def test_bridge_has_exact_seven_unannotated_chinese_tools(self):
        source = open(BRIDGE_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        found = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in COPS_7:
                continue
            has_tool = any(
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
                for dec in node.decorator_list)
            if has_tool:
                found[node.name] = node
        self.assertEqual(set(found), COPS_7)
        for name, node in found.items():
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            self.assertEqual(args[0].arg, "ctx", name)
            self.assertTrue(all(arg.annotation is None for arg in args), name)
            self.assertIsNone(node.returns, name)
            doc = ast.get_docstring(node) or ""
            self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in doc),
                            name)


if __name__ == "__main__":
    unittest.main()
