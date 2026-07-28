"""add-dops-tools 单元测试。

覆盖 8 个 DOP 查询/控制 API、timeline/cook/cache no-undo 边界、
force-reset 双门禁、HOM/Python PermissionError 分流、response cap 与
server/bridge 注册三分类。真实 H21 DOP 网络由 h21_live_dops_smoke.py 覆盖。
"""
import ast
import builtins
import importlib.util
import os
import sys
import types
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOPS_PATH = os.path.join(ROOT, "_dops.py")
SERVER_PATH = os.path.join(ROOT, "server.py")
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


class HomPermissionError(Exception):
    pass


class _Category(object):
    def name(self):
        return "Dop"


class _Record(object):
    def __init__(self, fields):
        self._fields = dict(fields)

    def fieldNames(self):
        return tuple(self._fields)

    def field(self, name):
        return self._fields[name]


class _Data(object):
    def __init__(self, name="Geometry", data_type="SIM_Geometry", fields=None):
        self._name = name
        self._data_type = data_type
        self._record = _Record(fields or {"mass": 1.0})

    def name(self):
        return self._name

    def dataType(self):
        return self._data_type

    def recordTypes(self):
        return ("Options",)

    def records(self, record_type):
        if record_type != "Options":
            return ()
        return (self._record,)


class _DopObject(object):
    def __init__(self, name="obj0", objid=0, data=None):
        self._name = name
        self._objid = objid
        self._data = data or _Data()

    def name(self):
        return self._name

    def objid(self):
        return self._objid

    def data(self):
        return {self._data.name(): self._data}

    def findSubData(self, name):
        return self._data if name == self._data.name() else None


class _Relationship(object):
    def __init__(self, name, objects):
        self._name = name
        self._objects = tuple(objects)

    def name(self):
        return self._name

    def objects(self):
        return self._objects


class _RelationshipRecordOnly(object):
    """H21 DopRelationship 没有 objects()，成员存放在 records 中。"""
    def name(self):
        return "merge"

    def recordTypes(self):
        return ("Options", "ObjInGroup", "ObjInAffectors")

    def records(self, record_type):
        if record_type == "ObjInGroup":
            return (_Record({"objname": "obj0", "objid": 0}),)
        if record_type == "ObjInAffectors":
            return (_Record({"objname": "gravity", "objid": 1}),)
        return ()


class _Simulation(object):
    def __init__(self, events, objects=None, relationships=None):
        self.events = events
        self._time = 0.25
        self._memory = 4096
        self._objects = list(objects or [_DopObject()])
        self._relationships = list(
            relationships or [_Relationship("constraint", self._objects)])
        self.force_error = None
        self.force_calls = []

    def time(self):
        return self._time

    def timestep(self):
        return 1.0 / 24.0

    def objects(self):
        return tuple(self._objects)

    def findObject(self, name):
        for item in self._objects:
            if item.name() == name:
                return item
        return None

    def relationships(self):
        return tuple(self._relationships)

    def memoryUsage(self):
        return self._memory

    def setTime(self, value, force_reset_sim=False):
        self.events.append(("sim.setTime", value, force_reset_sim))
        self.force_calls.append((value, force_reset_sim))
        if self.force_error is not None:
            raise self.force_error
        self._time = value


class _Node(object):
    def __init__(self, simulation, events):
        self._simulation = simulation
        self.events = events
        self.cook_error = None
        self._errors = ()
        self._warnings = ()

    def simulation(self):
        return self._simulation

    def childTypeCategory(self):
        return _Category()

    def path(self):
        return "/obj/dopnet1"

    def cook(self, force=False):
        self.events.append(("cook", force))
        if self.cook_error is not None:
            raise self.cook_error

    def errors(self):
        return self._errors

    def warnings(self):
        return self._warnings


class _Playbar(object):
    def playbackRange(self):
        return (1.0, 120.0)


class _Hou(object):
    PermissionError = HomPermissionError

    def __init__(self, data=None):
        self.events = []
        objects = None
        if data is not None:
            objects = [_DopObject(data=data)]
        self.sim = _Simulation(self.events, objects=objects)
        self.dop_node = _Node(self.sim, self.events)
        self._frame = 7.0
        self.playbar = _Playbar()

    def node(self, path):
        if path == "/obj/dopnet1":
            return self.dop_node
        return None

    def frame(self):
        return self._frame

    def frameToTime(self, frame):
        return float(frame) / 24.0

    def setTime(self, value):
        self.events.append(("hou.setTime", value))
        self._frame = float(value) * 24.0
        self.sim._time = float(value)

    def applicationVersion(self):
        return (21, 0, 596)


_pkg = types.ModuleType("dops_test_pkg")
_pkg.__path__ = [ROOT]
sys.modules["dops_test_pkg"] = _pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_dops():
    common_name = "dops_test_pkg._common"
    dops_name = "dops_test_pkg._dops"
    for name in (dops_name, common_name):
        sys.modules.pop(name, None)
    _load(common_name, os.path.join(ROOT, "_common.py"))
    return _load(dops_name, DOPS_PATH)


class DopsQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dops = _load_dops()

    def test_module_does_not_import_hou_or_new_dependencies(self):
        source = open(DOPS_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        self.assertNotIn("hou", imports)
        self.assertTrue(imports <= {"inspect", "math", ""})

    def test_get_simulation_info(self):
        result = self.dops.get_simulation_info(_Hou(), "/obj/dopnet1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["object_count"], 1)
        self.assertAlmostEqual(result["time"], 0.25)
        self.assertAlmostEqual(result["timestep"], 1.0 / 24.0)
        self.assertEqual(result["frame"], 7.0)

    def test_node_validation_and_simulation_failure_are_structured(self):
        hou = _Hou()
        missing = self.dops.get_simulation_info(hou, "/missing")
        self.assertEqual(missing["error"]["code"], "dop_node_not_found")
        hou.dop_node._simulation = None
        unavailable = self.dops.get_simulation_info(hou, "/obj/dopnet1")
        self.assertEqual(unavailable["error"]["code"], "simulation_unavailable")

    def test_list_and_find_objects_are_bounded(self):
        hou = _Hou()
        hou.sim._objects = [_DopObject("obj{0}".format(i), i)
                            for i in range(6)]
        page = self.dops.list_dop_objects(
            hou, "/obj/dopnet1", offset=2, limit=2)
        self.assertEqual(page["total"], 6)
        self.assertEqual(page["count"], 2)
        self.assertEqual([item["name"] for item in page["objects"]],
                         ["obj2", "obj3"])
        found = self.dops.get_dop_object(
            hou, "/obj/dopnet1", "obj3", max_data=4)
        self.assertEqual(found["object"]["name"], "obj3")
        self.assertLessEqual(len(found["object"]["data"]), 4)
        missing = self.dops.get_dop_object(
            hou, "/obj/dopnet1", "none")
        self.assertEqual(missing["error"]["code"], "dop_object_not_found")

    def test_h21_dop_object_subdata_surface_is_supported(self):
        hou = _Hou()
        obj = hou.sim._objects[0]
        obj.subData = lambda: {"Position": _Data("Position")}
        obj.data = None
        obj.recordTypes = lambda: ("Basic", "Options")
        result = self.dops.get_dop_object(
            hou, "/obj/dopnet1", "obj0", max_data=4)
        self.assertEqual(result["object"]["data"][0]["name"], "Position")
        self.assertEqual(result["object"]["record_types"],
                         ["Basic", "Options"])

    def test_get_dop_field_never_returns_raw_voxels(self):
        fields = {
            "voxels": list(range(1000)),
            "resolution": (32, 16, 8),
            "bbox": (-1, -2, -3, 1, 2, 3),
            "min": -2.0,
            "max": 7.0,
            "average": 0.5,
        }
        hou = _Hou(_Data("density", "SIM_ScalarField", fields))
        result = self.dops.get_dop_field(
            hou, "/obj/dopnet1", "obj0", "density", "voxels")
        self.assertIn(result["status"], ("success", "warning"))
        self.assertNotIn("voxels", result)
        self.assertNotEqual(result.get("value"), fields["voxels"])
        self.assertLessEqual(len(result.get("sample", [])), 64)
        self.assertEqual(result["statistics"]["resolution"], [32, 16, 8])
        self.assertEqual(result["statistics"]["minimum"], -2.0)

    def test_relationships_and_memory_usage(self):
        hou = _Hou()
        rels = self.dops.get_dop_relationships(
            hou, "/obj/dopnet1", offset=0, limit=10,
            max_objects=10)
        self.assertEqual(rels["status"], "success")
        self.assertEqual(rels["relationships"][0]["name"], "constraint")
        self.assertEqual(rels["relationships"][0]["objects"], ["obj0"])
        memory = self.dops.get_sim_memory_usage(hou, "/obj/dopnet1")
        self.assertEqual(memory["memory_usage"], 4096)
        self.assertEqual(memory["unit"], "bytes")

    def test_h21_relationship_record_members_are_bounded(self):
        hou = _Hou()
        hou.sim._relationships = [_RelationshipRecordOnly()]
        result = self.dops.get_dop_relationships(
            hou, "/obj/dopnet1", max_objects=10)
        relationship = result["relationships"][0]
        self.assertEqual(relationship["group_objects"], ["obj0"])
        self.assertEqual(relationship["affector_objects"], ["gravity"])
        self.assertEqual(relationship["objects"], ["obj0", "gravity"])


class DopsTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dops = _load_dops()

    def test_step_rejects_non_positive_frames(self):
        for value in (0, -1, False, "1"):
            result = self.dops.step_simulation(
                _Hou(), "/obj/dopnet1", frames=value)
            self.assertEqual(result["error"]["code"], "invalid_frames")

    def test_step_sets_timeline_before_force_cook_and_does_not_restore(self):
        hou = _Hou()
        result = self.dops.step_simulation(
            hou, "/obj/dopnet1", frames=2)
        self.assertEqual(result["status"], "success")
        self.assertEqual(hou.events[0][0], "hou.setTime")
        self.assertEqual(hou.events[1], ("cook", True))
        self.assertEqual(result["old_frame"], 7.0)
        self.assertEqual(result["new_frame"], 9.0)
        self.assertEqual(hou.frame(), 9.0)
        self.assertFalse(result["undoable"])
        self.assertTrue(result["side_effects"]["dop_cache_generated_or_replaced"])

    def test_step_cook_failure_is_structured(self):
        hou = _Hou()
        hou.dop_node.cook_error = RuntimeError("cook exploded")
        result = self.dops.step_simulation(hou, "/obj/dopnet1", 1)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "cook_failed")
        self.assertEqual(hou.events[0][0], "hou.setTime")

    def test_reset_timeline_and_cook_precede_force_gate(self):
        hou = _Hou()
        result = self.dops.reset_simulation(hou, "/obj/dopnet1")
        self.assertEqual(hou.events[0][0], "hou.setTime")
        self.assertEqual(hou.events[1], ("cook", True))
        self.assertEqual(hou.sim.force_calls, [])
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["_warning"]["code"],
                         "force_reset_live_gate_blocked")
        self.assertEqual(result["new_frame"], 1.0)
        self.assertFalse(result["undoable"])
        self.assertTrue(result["side_effects"]["dop_cache_cleared_or_rebuilt"])

    def test_hou_permission_error_has_owned_simulation_code(self):
        hou = _Hou()
        hou.sim.force_error = HomPermissionError("owned by dopnet")
        old = dict(self.dops._FORCE_RESET_SIM_LIVE_RESULTS)
        self.dops._FORCE_RESET_SIM_LIVE_RESULTS[(21, 0)] = True
        try:
            result = self.dops.reset_simulation(hou, "/obj/dopnet1", 1)
        finally:
            self.dops._FORCE_RESET_SIM_LIVE_RESULTS.clear()
            self.dops._FORCE_RESET_SIM_LIVE_RESULTS.update(old)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["_warning"]["code"],
                         "owned_simulation_permission_denied")
        self.assertEqual(hou.events[:2],
                         [("hou.setTime", 1.0 / 24.0), ("cook", True)])
        self.assertEqual(hou.events[2][0], "sim.setTime")

    def test_builtin_permission_error_is_not_hom_owned_branch(self):
        hou = _Hou()
        hou.sim.force_error = builtins.PermissionError("filesystem denied")
        old = dict(self.dops._FORCE_RESET_SIM_LIVE_RESULTS)
        self.dops._FORCE_RESET_SIM_LIVE_RESULTS[(21, 0)] = True
        try:
            result = self.dops.reset_simulation(hou, "/obj/dopnet1", 1)
        finally:
            self.dops._FORCE_RESET_SIM_LIVE_RESULTS.clear()
            self.dops._FORCE_RESET_SIM_LIVE_RESULTS.update(old)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "python_permission_error")
        self.assertNotEqual(result["error"]["code"],
                            "owned_simulation_permission_denied")

    def test_mock_capability_alone_cannot_enable_force_reset(self):
        hou = _Hou()
        self.assertTrue(self.dops._probe_force_reset_signature(hou, hou.sim))
        self.assertFalse(self.dops._force_reset_live_allowed(hou))
        result = self.dops.reset_simulation(hou, "/obj/dopnet1", 1)
        self.assertEqual(result["_warning"]["code"],
                         "force_reset_live_gate_blocked")
        self.assertEqual(hou.sim.force_calls, [])

    def test_forbidden_fictional_apis_absent(self):
        source = open(DOPS_PATH, "r", encoding="utf-8").read()
        self.assertNotIn(".advance(", source)
        self.assertNotIn("resetSimulation", source)


class ResponseCapTests(unittest.TestCase):
    def test_all_eight_public_functions_cap_success_and_error_paths(self):
        dops = _load_dops()
        original = dops.cmn.apply_response_cap
        calls = []

        def cap(value, max_bytes=16384):
            calls.append(value.get("status"))
            result = dict(value)
            result["_cap_test"] = True
            return result

        dops.cmn.apply_response_cap = cap
        try:
            hou = _Hou()
            results = [
                dops.get_simulation_info(hou, "/obj/dopnet1"),
                dops.list_dop_objects(hou, "/obj/dopnet1"),
                dops.get_dop_object(hou, "/obj/dopnet1", "obj0"),
                dops.get_dop_field(
                    hou, "/obj/dopnet1", "obj0", "Geometry", "mass"),
                dops.get_dop_relationships(hou, "/obj/dopnet1"),
                dops.step_simulation(hou, "/obj/dopnet1", 1),
                dops.reset_simulation(hou, "/obj/dopnet1", 1),
                dops.get_sim_memory_usage(hou, "/obj/dopnet1"),
                dops.step_simulation(hou, "/obj/dopnet1", 0),
            ]
        finally:
            dops.cmn.apply_response_cap = original
        self.assertEqual(len(calls), 9)
        self.assertTrue(all(result["_cap_test"] for result in results))


DOPS_8 = {
    "get_simulation_info", "list_dop_objects", "get_dop_object",
    "get_dop_field", "get_dop_relationships", "step_simulation",
    "reset_simulation", "get_sim_memory_usage",
}
DOPS_RO = DOPS_8 - {"step_simulation", "reset_simulation"}
DOPS_NO_UNDO = {"step_simulation", "reset_simulation"}


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
    def test_eight_commands_are_pairwise_disjoint_and_exhaustive(self):
        values, handlers, _ = _class_sets_and_handlers()
        mut = values["MUTATING_COMMANDS"] & DOPS_8
        ro = values["READ_ONLY_COMMANDS"] & DOPS_8
        no_undo = values["NO_UNDO_COMMANDS"] & DOPS_8
        self.assertEqual(mut, set())
        self.assertEqual(ro, DOPS_RO)
        self.assertEqual(no_undo, DOPS_NO_UNDO)
        self.assertEqual(mut | ro | no_undo, DOPS_8)
        self.assertFalse(mut & ro)
        self.assertFalse(mut & no_undo)
        self.assertFalse(ro & no_undo)
        self.assertTrue(DOPS_8 <= handlers)

    def test_server_handlers_apply_cap_and_never_create_inner_undo_group(self):
        source = open(SERVER_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        _, _, class_node = _class_sets_and_handlers()
        functions = {node.name: node for node in class_node.body
                     if isinstance(node, ast.FunctionDef)}
        for command in DOPS_8:
            fn = functions["handle_" + command]
            segment = ast.get_source_segment(source, fn) or ""
            self.assertIn("cmn.apply_response_cap", segment)
            self.assertNotIn("undos.group", segment)

    def test_bridge_has_exact_eight_unannotated_chinese_tools(self):
        source = open(BRIDGE_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        found = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in DOPS_8:
                continue
            has_tool = any(
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
                for dec in node.decorator_list)
            if has_tool:
                found[node.name] = node
        self.assertEqual(set(found), DOPS_8)
        for name, node in found.items():
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            self.assertEqual(args[0].arg, "ctx")
            self.assertTrue(all(arg.annotation is None for arg in args))
            self.assertIsNone(node.returns)
            doc = ast.get_docstring(node) or ""
            self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in doc),
                            name)


if __name__ == "__main__":
    unittest.main()
