"""add-chops-tools 单元测试。

覆盖 4 个 CHOP 查询/编辑 API、sample 边界（闭区间）、allSamples guard、
三层 cap、export target 原子预校验、禁用旧/虚构 API（findTrack/evaluator），
以及 server/bridge 注册三分类唯一穷尽互斥。真实 H21 CHOP network 由
h21_live_chops_smoke.py 覆盖。
"""
import ast
import importlib.util
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHOPS_PATH = os.path.join(ROOT, "_chops.py")
SERVER_PATH = os.path.join(ROOT, "server.py")
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


# ---------------------------------------------------------------------------
# Mock objects — 模拟 H21+ ChopNode/Clip/Track 真实 surface。
# ---------------------------------------------------------------------------
class _TrackBase(object):
    """hou.Track 基类；mock track 继承它（isinstance(hou.Track) 检查）。"""
    pass


class _Track(_TrackBase):
    """hou.Track mock；提供 numSamples / evalAt* / allSamples。"""

    def __init__(self, name, values, sample_rate=30.0):
        self._name = name
        self._values = list(values)
        self._rate = sample_rate
        self.all_called = False
        self.range_calls = []
        self.sample_calls = []
        self.frame_calls = []
        self.time_calls = []

    def name(self):
        return self._name

    def numSamples(self):
        return len(self._values)

    def allSamples(self):
        self.all_called = True
        return list(self._values)

    def evalAtSampleRange(self, start, end):
        self.range_calls.append((start, end))
        start = max(0, int(start))
        end = min(len(self._values) - 1, int(end))
        if end < start:
            return []
        return list(self._values[start:end + 1])

    def evalAtSample(self, index):
        self.sample_calls.append(index)
        idx = int(index)
        if 0 <= idx < len(self._values):
            return self._values[idx]
        return 0.0

    def evalAtFrame(self, frame):
        self.frame_calls.append(frame)
        return float(self._values[0])

    def evalAtTime(self, time):
        self.time_calls.append(time)
        return float(self._values[0])


class _Clip(object):
    """hou.Clip mock；tracks / track / sampleRange / sampleRate。"""

    def __init__(self, tracks, sample_range=(0, 9), sample_rate=30.0):
        self._tracks = tracks
        self._range = sample_range
        self._rate = sample_rate

    def tracks(self):
        return list(self._tracks)

    def track(self, name):
        for track in self._tracks:
            if track.name() == name:
                return track
        raise ValueError("no track: " + name)

    def sampleRange(self):
        return self._range

    def sampleRate(self):
        return self._rate


class _TypeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _TypeObj(object):
    def __init__(self, name, category_name="Chop"):
        self._name = name
        self._category = _TypeCategory(category_name)

    def name(self):
        return self._name

    def category(self):
        return self._category


class _NodeType(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _TypeRegistry(object):
    def __init__(self, names):
        self._types = {name: _NodeType(name) for name in names}

    def nodeTypes(self):
        return dict(self._types)


class _ChopNodeBase(object):
    """hou.ChopNode 基类；mock 节点继承它（isinstance 判定）。"""
    pass


class _ChopNode(_ChopNodeBase):
    """hou.ChopNode mock；clip(output_index) -> Clip。"""

    def __init__(self, path="/ch/ch1/wave1", category_name="Chop",
                 clip=None):
        self._path = path
        self._type = _TypeObj("wave", category_name)
        self._clip = clip or _Clip([_Track("tx", [0.0, 1.0, 2.0, 3.0])])

    def path(self):
        return self._path

    def type(self):
        return self._type

    def errors(self):
        return ()

    def warnings(self):
        return ()

    def clip(self, output_index):
        return self._clip

    def tracks(self):
        return self._clip.tracks()

    def track(self, name):
        return self._clip.track(name)


class _ChopNetParent(object):
    """CHOP network parent（childTypeCategory == "Chop"）。"""

    def __init__(self, path="/ch/ch1", editable=True, category_name="Chop"):
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
        child = _ChopNode(self._path.rstrip("/") + "/" + (node_name or "child"))
        self.created_nodes.append((node_type, node_name, child))
        return child


class _Parm(object):
    """hou.Parm mock；scalar numeric + expression/keyframe 状态。"""

    def __init__(self, name="tx", num_components=1, expression=None,
                 keyframes=()):
        self._name = name
        self._num_components = num_components
        self._expression = expression
        self._keyframes = list(keyframes)
        self.set_calls = []

    def name(self):
        return self._name

    def numComponents(self):
        return self._num_components

    def expression(self):
        return self._expression

    def keyframes(self):
        return list(self._keyframes)

    def setExpression(self, expr, language=None):
        self.set_calls.append((expr, language))
        self._expression = expr


class _TargetNode(object):
    """目标节点 mock；parm(name) -> _Parm。"""

    def __init__(self, path="/obj/geo1", parms=None):
        self._path = path
        self._parms = parms or {}

    def path(self):
        return self._path

    def parm(self, name):
        return self._parms.get(name)


class _ExprLanguage(object):
    Hscript = "Hscript"


class _Hou(object):
    ChopNode = _ChopNodeBase
    Track = _TrackBase
    exprLanguage = _ExprLanguage()

    def __init__(self):
        self._nodes = {}
        self._registries = {"Chop": _TypeRegistry(
            ["wave", "constant", "noise"])}

    def node(self, path):
        return self._nodes.get(path)

    def applicationVersion(self):
        return (21, 0, 596)

    def chopNodeTypeCategory(self):
        return self._registries.get("Chop")

    def nodeTypeCategories(self):
        return dict(self._registries)

    def register(self, path, node):
        self._nodes[path] = node


# ---------------------------------------------------------------------------
# module loader（隔离 _common + _chops）
# ---------------------------------------------------------------------------
_pkg = types.ModuleType("chops_test_pkg")
_pkg.__path__ = [ROOT]
sys.modules["chops_test_pkg"] = _pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_chops():
    common_name = "chops_test_pkg._common"
    chops_name = "chops_test_pkg._chops"
    for name in (chops_name, common_name):
        sys.modules.pop(name, None)
    _load(common_name, os.path.join(ROOT, "_common.py"))
    return _load(chops_name, CHOPS_PATH)


def _make_hou():
    hou = _Hou()
    track = _Track("tx", [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0])
    clip = _Clip([track], sample_range=(0, 6), sample_rate=30.0)
    chop_node = _ChopNode("/ch/ch1/wave1", clip=clip)
    hou.register("/ch/ch1/wave1", chop_node)
    parent = _ChopNetParent("/ch/ch1")
    hou.register("/ch/ch1", parent)
    hou.register("/ch/ch1/locked", _ChopNetParent(
        "/ch/ch1/locked", editable=False))
    hou.register("/obj/geo1", _ChopNetParent(
        "/obj/geo1", category_name="Object"))
    # 目标节点 + scalar parm
    target = _TargetNode("/obj/geo1", {"tx": _Parm("tx")})
    hou.register("/obj/geo1/target", target)
    return hou, chop_node, track, parent, target


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
CHOPS_4 = {
    "get_chop_data", "list_chop_channels",
    "create_chop_node", "export_chop_to_parm",
}
CHOPS_MUT = {"create_chop_node", "export_chop_to_parm"}
CHOPS_NO_UNDO = {"get_chop_data", "list_chop_channels"}
CHOPS_RO = set()


class ChopsQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chops = _load_chops()

    def test_module_does_not_import_hou_or_new_dependencies(self):
        source = open(CHOPS_PATH, "r", encoding="utf-8").read()
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
        source = open(CHOPS_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        # 检查 AST 中是否出现禁止 API 的属性访问/调用（不匹配 docstring 文字）
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                called.add(node.attr)
        for forbidden in ("findTrack", "evaluator", "find_track"):
            self.assertNotIn(forbidden, called,
                             "forbidden legacy/fictional API called: "
                             + forbidden)

    def test_invalid_path_is_structured(self):
        hou, _, _, _, _ = _make_hou()
        missing = self.chops.list_chop_channels(hou, "/missing")
        self.assertEqual(missing["error"]["code"], "node_not_found")
        empty = self.chops.list_chop_channels(hou, "")
        self.assertEqual(empty["error"]["code"], "invalid_node_path")

    def test_not_a_chop_node_is_unsupported(self):
        hou, _, _, _, _ = _make_hou()
        result = self.chops.list_chop_channels(hou, "/obj/geo1")
        self.assertEqual(result["error"]["code"], "not_a_chop_node")

    def test_invalid_output_index_rejected(self):
        hou, _, _, _, _ = _make_hou()
        result = self.chops.list_chop_channels(hou, "/ch/ch1/wave1", -1)
        self.assertEqual(result["error"]["code"], "invalid_output_index")

    def test_list_chop_channels_reads_tracks(self):
        hou, _, _, _, _ = _make_hou()
        result = self.chops.list_chop_channels(hou, "/ch/ch1/wave1", 0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["node_type"], "wave")
        self.assertEqual(result["sample_rate"], 30.0)
        self.assertEqual(result["sample_range"], [0, 6])
        self.assertEqual(len(result["channels"]), 1)
        self.assertEqual(result["channels"][0]["name"], "tx")
        self.assertEqual(result["channels"][0]["sample_count"], 7)

    def test_get_chop_data_full_track_uses_all_samples(self):
        hou, _, track, _, _ = _make_hou()
        result = self.chops.get_chop_data(hou, "/ch/ch1/wave1")
        self.assertEqual(result["status"], "success")
        self.assertTrue(track.all_called)
        self.assertEqual(result["channels"][0]["query_mode"], "all_samples")
        self.assertEqual(len(result["channels"][0]["samples"]), 7)
        self.assertEqual(result["channels"][0]["samples"][0], 10.0)

    def test_get_chop_data_all_samples_guard_when_over_limit(self):
        # numSamples(7) > max_samples_per_channel(3) -> 不得调 allSamples，
        # 改走 evalAtSampleRange(clip range) 并截断。
        big_track = _Track("big", list(range(7)))
        clip = _Clip([big_track], sample_range=(0, 6))
        node = _ChopNode("/ch/big1", clip=clip)
        hou = _Hou()
        hou.register("/ch/big1", node)
        result = self.chops.get_chop_data(
            hou, "/ch/big1", max_samples_per_channel=3)
        self.assertEqual(result["status"], "success")
        self.assertFalse(big_track.all_called)
        self.assertTrue(len(big_track.range_calls) >= 1)
        self.assertEqual(result["channels"][0]["query_mode"], "sample_range")
        # 截断到 max_samples_per_channel
        self.assertEqual(len(result["channels"][0]["samples"]), 3)
        self.assertTrue(result["channels"][0]["truncated"])
        self.assertTrue(result["truncated"])

    def test_get_chop_data_sample_range_uses_eval_at_sample_range(self):
        hou, _, track, _, _ = _make_hou()
        result = self.chops.get_chop_data(
            hou, "/ch/ch1/wave1", start=1, end=3)
        self.assertEqual(result["status"], "success")
        self.assertFalse(track.all_called)
        self.assertEqual(track.range_calls, [(1, 3)])
        self.assertEqual(result["channels"][0]["query_mode"], "sample_range")
        self.assertEqual(result["channels"][0]["samples"], [11.0, 12.0, 13.0])
        self.assertEqual(result["channels"][0]["actual_range"], [1, 3])

    def test_get_chop_data_sample_range_clamps_to_clip_range(self):
        hou, _, track, _, _ = _make_hou()
        # start 超过 clip hi(6) 夹取；end 负值夹取到 lo(0)
        result = self.chops.get_chop_data(
            hou, "/ch/ch1/wave1", start=-2, end=99)
        self.assertEqual(result["status"], "success")
        self.assertEqual(track.range_calls[0], (0, 6))

    def test_get_chop_data_single_sample_uses_eval_at_sample(self):
        hou, _, track, _, _ = _make_hou()
        result = self.chops.get_chop_data(
            hou, "/ch/ch1/wave1", sample=2)
        self.assertEqual(result["status"], "success")
        self.assertEqual(track.sample_calls, [2])
        self.assertEqual(result["channels"][0]["query_mode"], "sample")
        self.assertEqual(result["channels"][0]["samples"], [12.0])

    def test_get_chop_data_frame_uses_eval_at_frame(self):
        hou, _, track, _, _ = _make_hou()
        result = self.chops.get_chop_data(
            hou, "/ch/ch1/wave1", frame=5.0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(track.frame_calls, [5.0])
        self.assertEqual(result["channels"][0]["query_mode"], "frame")

    def test_get_chop_data_time_uses_eval_at_time(self):
        hou, _, track, _, _ = _make_hou()
        result = self.chops.get_chop_data(
            hou, "/ch/ch1/wave1", time=0.1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(track.time_calls, [0.1])
        self.assertEqual(result["channels"][0]["query_mode"], "time")

    def test_get_chop_data_specific_channels_by_name(self):
        second = _Track("ty", [20.0, 21.0])
        track_tx = _Track("tx", [10.0, 11.0])
        clip = _Clip([track_tx, second], sample_range=(0, 1))
        node = _ChopNode("/ch/multi1", clip=clip)
        hou = _Hou()
        hou.register("/ch/multi1", node)
        result = self.chops.get_chop_data(
            hou, "/ch/multi1", channels=["ty"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["channels"]), 1)
        self.assertEqual(result["channels"][0]["name"], "ty")
        self.assertEqual(result["channels"][0]["samples"], [20.0, 21.0])

    def test_get_chop_data_channel_not_found(self):
        hou, _, _, _, _ = _make_hou()
        result = self.chops.get_chop_data(
            hou, "/ch/ch1/wave1", channels=["bogus"])
        self.assertEqual(result["error"]["code"], "channel_not_found")
        self.assertIn("bogus", result["missing_channels"])

    def test_get_chop_data_max_channels_truncation(self):
        tracks = [_Track("c{0}".format(i), [float(i)])
                  for i in range(10)]
        clip = _Clip(tracks, sample_range=(0, 0))
        node = _ChopNode("/ch/many1", clip=clip)
        hou = _Hou()
        hou.register("/ch/many1", node)
        result = self.chops.get_chop_data(
            hou, "/ch/many1", max_channels=3)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["channel_count"], 3)
        self.assertEqual(result["total_channels"], 10)
        self.assertTrue(result["truncated"])

    def test_get_chop_data_invalid_single_point_rejected(self):
        hou, _, _, _, _ = _make_hou()
        result = self.chops.get_chop_data(
            hou, "/ch/ch1/wave1", frame="x")
        self.assertEqual(result["error"]["code"], "invalid_frame")


class ChopsCreateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chops = _load_chops()

    def test_create_validates_parent_editable_and_category(self):
        hou, _, _, parent, _ = _make_hou()
        result = self.chops.create_chop_node(
            hou, "/ch/ch1", "wave", node_name="created1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["created_path"], "/ch/ch1/created1")
        self.assertEqual(parent.created_nodes[0][0], "wave")

        locked = self.chops.create_chop_node(
            hou, "/ch/ch1/locked", "wave")
        self.assertEqual(locked["error"]["code"], "parent_locked")

        bad_cat = self.chops.create_chop_node(
            hou, "/obj/geo1", "wave")
        self.assertEqual(bad_cat["error"]["code"],
                         "unsupported_parent_category")

    def test_create_rejects_unknown_node_type(self):
        hou, _, _, _, _ = _make_hou()
        result = self.chops.create_chop_node(
            hou, "/ch/ch1", "bogus::missing")
        self.assertEqual(result["error"]["code"], "node_type_unavailable")

    def test_create_rejects_invalid_args(self):
        hou, _, _, _, _ = _make_hou()
        self.assertEqual(
            self.chops.create_chop_node(hou, "/ch/ch1", "")["error"]["code"],
            "invalid_node_type")
        self.assertEqual(
            self.chops.create_chop_node(
                hou, "/ch/ch1", "wave", node_name="")["error"]["code"],
            "invalid_node_name")


class ChopsExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chops = _load_chops()

    def test_export_creates_chop_reference(self):
        hou, _, _, _, target = _make_hou()
        result = self.chops.export_chop_to_parm(
            hou, "/ch/ch1/wave1", "tx", "/obj/geo1/target", "tx")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["channel_path"], "/ch/ch1/wave1/tx")
        parm = target.parm("tx")
        self.assertEqual(len(parm.set_calls), 1)
        expr, lang = parm.set_calls[0]
        self.assertEqual(expr, 'chop("/ch/ch1/wave1/tx")')
        self.assertEqual(lang, "Hscript")
        self.assertFalse(result["replaced_existing"])

    def test_export_rejects_occupied_target_by_default(self):
        hou = _Hou()
        chop = _ChopNode("/ch/src1", clip=_Clip(
            [_Track("tx", [1.0])], sample_range=(0, 0)))
        hou.register("/ch/src1", chop)
        occupied = _Parm("tx", expression="foo + 1")
        target = _TargetNode("/obj/occ", {"tx": occupied})
        hou.register("/obj/occ", target)
        result = self.chops.export_chop_to_parm(
            hou, "/ch/src1", "tx", "/obj/occ", "tx")
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["_warning"]["code"], "target_occupied")
        self.assertEqual(len(occupied.set_calls), 0)

    def test_export_replace_existing_discloses_old_and_new(self):
        hou = _Hou()
        chop = _ChopNode("/ch/src2", clip=_Clip(
            [_Track("tx", [1.0])], sample_range=(0, 0)))
        hou.register("/ch/src2", chop)
        occupied = _Parm("tx", expression="old_expr", keyframes=[object()])
        target = _TargetNode("/obj/rep", {"tx": occupied})
        hou.register("/obj/rep", target)
        result = self.chops.export_chop_to_parm(
            hou, "/ch/src2", "tx", "/obj/rep", "tx", replace_existing=True)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["replaced_existing"])
        self.assertEqual(result["previous_expression"], "old_expr")
        self.assertEqual(result["previous_keyframe_count"], 1)
        self.assertEqual(len(occupied.set_calls), 1)

    def test_export_rejects_non_scalar_parm(self):
        hou = _Hou()
        chop = _ChopNode("/ch/src3", clip=_Clip(
            [_Track("tx", [1.0])], sample_range=(0, 0)))
        hou.register("/ch/src3", chop)
        vector_parm = _Parm("p", num_components=3)
        target = _TargetNode("/obj/vec", {"p": vector_parm})
        hou.register("/obj/vec", target)
        result = self.chops.export_chop_to_parm(
            hou, "/ch/src3", "tx", "/obj/vec", "p")
        self.assertEqual(result["error"]["code"], "parm_not_scalar")
        self.assertEqual(len(vector_parm.set_calls), 0)

    def test_export_rejects_missing_target_parm(self):
        hou, _, _, _, _ = _make_hou()
        result = self.chops.export_chop_to_parm(
            hou, "/ch/ch1/wave1", "tx", "/obj/geo1/target", "missing")
        self.assertEqual(result["error"]["code"], "parm_not_found")

    def test_export_rejects_missing_source_channel(self):
        hou, _, _, _, target = _make_hou()
        result = self.chops.export_chop_to_parm(
            hou, "/ch/ch1/wave1", "bogus", "/obj/geo1/target", "tx")
        self.assertEqual(result["error"]["code"], "channel_not_found")
        self.assertEqual(len(target.parm("tx").set_calls), 0)

    def test_export_target_not_found_is_atomic(self):
        hou, _, _, _, _ = _make_hou()
        result = self.chops.export_chop_to_parm(
            hou, "/ch/ch1/wave1", "tx", "/missing/target", "tx")
        self.assertEqual(result["error"]["code"], "target_not_found")


class ResponseCapTests(unittest.TestCase):
    def test_all_four_public_functions_cap_success_and_error_paths(self):
        chops = _load_chops()
        original = chops.cmn.apply_response_cap
        calls = []

        def cap(value, max_bytes=16384):
            calls.append(value.get("status") if isinstance(value, dict)
                         else None)
            result = dict(value)
            result["_cap_test"] = True
            return result

        chops.cmn.apply_response_cap = cap
        try:
            hou, _, _, _, _ = _make_hou()
            results = [
                chops.list_chop_channels(hou, "/ch/ch1/wave1"),
                chops.get_chop_data(hou, "/ch/ch1/wave1"),
                chops.create_chop_node(hou, "/ch/ch1", "wave"),
                chops.export_chop_to_parm(
                    hou, "/ch/ch1/wave1", "tx", "/obj/geo1/target", "tx"),
                # error / warning paths
                chops.list_chop_channels(hou, "/missing"),
                chops.get_chop_data(hou, "/missing"),
                chops.create_chop_node(hou, "/ch/ch1", "bogus"),
                chops.export_chop_to_parm(
                    hou, "/ch/ch1/wave1", "bogus",
                    "/obj/geo1/target", "tx"),
            ]
        finally:
            chops.cmn.apply_response_cap = original
        self.assertEqual(len(calls), 8)
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
    def test_four_commands_are_pairwise_disjoint_and_exhaustive(self):
        values, handlers, _ = _class_sets_and_handlers()
        mut = values["MUTATING_COMMANDS"] & CHOPS_4
        ro = values["READ_ONLY_COMMANDS"] & CHOPS_4
        no_undo = values["NO_UNDO_COMMANDS"] & CHOPS_4
        self.assertEqual(mut, CHOPS_MUT)
        self.assertEqual(ro, CHOPS_RO)
        self.assertEqual(no_undo, CHOPS_NO_UNDO)
        self.assertEqual(mut | ro | no_undo, CHOPS_4)
        self.assertFalse(mut & ro)
        self.assertFalse(mut & no_undo)
        self.assertFalse(ro & no_undo)
        self.assertTrue(CHOPS_4 <= handlers)

    def test_server_handlers_apply_cap(self):
        source = open(SERVER_PATH, "r", encoding="utf-8").read()
        _, _, class_node = _class_sets_and_handlers()
        functions = {node.name: node for node in class_node.body
                     if isinstance(node, ast.FunctionDef)}
        for command in CHOPS_4:
            fn = functions["handle_" + command]
            segment = ast.get_source_segment(source, fn) or ""
            self.assertIn("cmn.apply_response_cap", segment, command)

    def test_read_only_and_no_undo_handlers_never_create_undo_group(self):
        source = open(SERVER_PATH, "r", encoding="utf-8").read()
        _, _, class_node = _class_sets_and_handlers()
        functions = {node.name: node for node in class_node.body
                     if isinstance(node, ast.FunctionDef)}
        for command in CHOPS_NO_UNDO | CHOPS_RO:
            fn = functions["handle_" + command]
            segment = ast.get_source_segment(source, fn) or ""
            self.assertNotIn("undos.group", segment, command)

    def test_bridge_has_exact_four_unannotated_chinese_tools(self):
        source = open(BRIDGE_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        found = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in CHOPS_4:
                continue
            has_tool = any(
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
                for dec in node.decorator_list)
            if has_tool:
                found[node.name] = node
        self.assertEqual(set(found), CHOPS_4)
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
