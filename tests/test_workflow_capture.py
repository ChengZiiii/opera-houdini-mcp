"""tests/test_workflow_capture.py — add-workflow-knowledge-capture Section B 单测。

覆盖 server.py 的 ``capture_workflow_snapshot`` handler（readOnly）：
- 定位：空选择 → ``no_selection`` 结构化错误（不静默回退）；node_path
  无效 → ``invalid_node_path``。
- 闭包遍历：seeds BFS（inputs + outputs），max_nodes 硬上限 + truncated
  标记 + 按 path 稳定排序；默认 max_nodes=50。
- 节点表：path / name / type / comment / 非默认参数（parmTemplates，
  仅收录非默认、菜单与命令类跳过）/ vex（attribwrangle snippet）/
  hda（definition 引用）/ errors / warnings；comment / VEX 读取异常
  降级 + ``_warning``，绝不 crash。
- sticky note：父网络去重 ``iterStickyNotes`` 提取 text/position；API
  缺失（AttributeError）降级跳过 + ``_warning``。
- 连线：每节点每个已连接 input → ``{from, to, input_index}``。
- MUST NOT 包含几何数据 / 完整参数表。
- 注册契约：只进 READ_ONLY_COMMANDS，不在 MUTATING / NO_UNDO，
  ``_validate_handler_classification`` 不抛。
- 响应过 ``apply_response_cap``（大快照被 cap 截断为 dict）。

约束：stdlib unittest + conftest hou stub（monkeypatch 属性）；不新增
依赖、不依赖真实 Houdini。
"""
import importlib.util
import json
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _ensure_pkg():
    pkg_name = "workflow_capture_test_pkg"
    if pkg_name in sys.modules and getattr(
            sys.modules[pkg_name], "__path__", None):
        return sys.modules[pkg_name]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [ROOT]
    sys.modules[pkg_name] = pkg
    return pkg


def _import_server_module():
    pkg = _ensure_pkg()
    full = pkg.__name__ + ".server"
    if full in sys.modules:
        del sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 说明：conftest 已 stub hou（sys.modules["hou"]），但其他测试文件会在
# 模块收集期整体替换 sys.modules["hou"]，共享进程下 module 级 patch 会被
# 覆盖。因此本文件统一在 setUp 里直接 patch ``self.server_mod.hou``（即
# server.py 顶层 ``import hou`` 实际绑定的对象），每个测试独立生效。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# fake hou.Node 基建
# ---------------------------------------------------------------------------
class _FakeDefinition(object):
    def __init__(self, library_path):
        self._library_path = library_path

    def libraryFilePath(self):
        return self._library_path


class _FakeNodeType(object):
    def __init__(self, name, category="Sop", definition=None):
        self._name = name
        self._category = category
        self._definition = definition

    def name(self):
        return self._name

    def nameWithCategory(self):
        return "%s/%s" % (self._category, self._name)

    def definition(self):
        return self._definition


class _FakeParmTemplate(object):
    def __init__(self, name, ptype, default):
        self._name = name
        self._type = ptype
        self._default = default

    def name(self):
        return self._name

    def type(self):
        return self._type

    def defaultValue(self):
        return self._default


class _FakeParm(object):
    def __init__(self, name, value):
        self._name = name
        self._value = value

    def name(self):
        return self._name

    def eval(self):
        return self._value


class _FakeStickyNote(object):
    def __init__(self, text, position):
        self._text = text
        self._position = position

    def text(self):
        return self._text

    def position(self):
        return self._position


class _FakeNode(object):
    """fake hou.Node：支持 path / type / comment / parmTemplates / parm /
    evalParm / inputs / outputs / parent / errors / warnings /
    iterStickyNotes。"""

    def __init__(self, name, type_name="geo", parent=None, comment="",
                 templates=None, parm_values=None, inputs=None,
                 outputs=None, errors=None, warnings=None,
                 definition=None, sticky_notes=None):
        self._name = name
        self._type = _FakeNodeType(type_name, "Sop", definition)
        self._parent = parent
        self._comment = comment
        self._templates = list(templates) if templates else []
        self._parm_values = dict(parm_values) if parm_values else {}
        self._inputs = list(inputs) if inputs else []
        self._outputs = list(outputs) if outputs else []
        self._errors = list(errors) if errors else []
        self._warnings = list(warnings) if warnings else []
        self._sticky_notes = list(sticky_notes) if sticky_notes else []

    def name(self):
        return self._name

    def path(self):
        if self._parent is None:
            return "/" + self._name
        return self._parent.path() + "/" + self._name

    def type(self):
        return self._type

    def comment(self):
        return self._comment

    def parmTemplates(self):
        return list(self._templates)

    def parm(self, name):
        if name in self._parm_values:
            return _FakeParm(name, self._parm_values[name])
        return None

    def evalParm(self, name):
        return self._parm_values.get(name)

    def inputs(self):
        return list(self._inputs)

    def outputs(self):
        return list(self._outputs)

    def parent(self):
        return self._parent

    def errors(self):
        return list(self._errors)

    def warnings(self):
        return list(self._warnings)

    def iterStickyNotes(self):
        return list(self._sticky_notes)


class _FakeNodeNoNotes(_FakeNode):
    """父网络缺失 iterStickyNotes API 的退化场景。"""

    def iterStickyNotes(self):
        raise AttributeError("sticky note API 不可用")


class _FakeNodeNoComment(_FakeNode):
    """节点缺失 comment API 的退化场景。"""

    def comment(self):
        raise AttributeError("comment API 不可用")


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
class CaptureWorkflowSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_mod = _import_server_module()
        cls.Server = cls.server_mod.HoudiniMCPServer

    def setUp(self):
        # patch server 模块实际绑定的 hou 对象（共享进程下其他测试文件
        # 可能替换过 sys.modules["hou"]，module 级 patch 不可靠）
        hou = self.server_mod.hou
        if not hasattr(hou, "parmTemplateType"):
            hou.parmTemplateType = types.SimpleNamespace(
                Float=101, Int=102, String=103, Toggle=104,
                Menu=105, Button=106, Data=107, Folder=108,
                Label=109, Separator=110)
        hou.selectedNodes = lambda: []
        hou.node = lambda path: None

    def _handler(self):
        return self.Server.__new__(self.Server)

    def _select(self, nodes):
        self.server_mod.hou.selectedNodes = lambda: list(nodes)

    # ---- 定位与错误 ----

    def test_empty_selection_returns_no_selection_error(self):
        self._select([])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "no_selection")
        self.assertIn("选择", result["error"]["message"])

    def test_invalid_node_path_returns_structured_error(self):
        self.server_mod.hou.node = lambda path: None
        result = self._handler().handle_capture_workflow_snapshot(
            node_path="/obj/geo1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_node_path")

    # ---- 闭包 / 节点表 / 连线 ----

    def test_closure_nodes_comments_params_and_connections(self):
        net = _FakeNode("net")
        b = _FakeNode("B", parent=net, comment="B 节点")
        c = _FakeNode("C", parent=net, comment="C 节点")
        a = _FakeNode(
            "A", parent=net, comment="A 节点注释",
            templates=[
                _FakeParmTemplate("scale", 101, 1.0),    # Float 非默认
                _FakeParmTemplate("seed", 102, 0),        # Int 默认值
                _FakeParmTemplate("label", 103, ""),      # String 非默认
                _FakeParmTemplate("choice", 105, "a"),    # Menu 跳过
            ],
            parm_values={"scale": 2.5, "seed": 0,
                         "label": "A 标签", "choice": "b"},
            inputs=[b], outputs=[c],
            errors=["cook error"], warnings=["slow"],
        )
        self._select([a])
        result = self._handler().handle_capture_workflow_snapshot()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["root"], "selection")
        self.assertEqual(result["node_count"], 3)
        self.assertFalse(result["truncated"])
        # 无降级 → 不出现 _warning；顶层键集合精确
        self.assertEqual(set(result.keys()), {
            "status", "root", "node_count", "truncated",
            "nodes", "sticky_notes", "connections"})

        by_path = {n["path"]: n for n in result["nodes"]}
        self.assertEqual(set(by_path.keys()),
                         {"/net/A", "/net/B", "/net/C"})
        node_a = by_path["/net/A"]
        self.assertEqual(set(node_a.keys()), {
            "path", "name", "type", "comment", "params", "vex",
            "hda", "errors", "warnings"})
        self.assertEqual(node_a["name"], "A")
        self.assertEqual(node_a["type"], "geo")
        self.assertEqual(node_a["comment"], "A 节点注释")
        # 仅非默认参数；Menu 与默认值参数被排除
        self.assertEqual(node_a["params"], {"scale": 2.5, "label": "A 标签"})
        self.assertNotIn("params_truncated", node_a)
        self.assertIsNone(node_a["vex"])
        self.assertIsNone(node_a["hda"])
        self.assertEqual(node_a["errors"], ["cook error"])
        self.assertEqual(node_a["warnings"], ["slow"])
        # 连线：A 的 input 0 = B
        self.assertEqual(result["connections"], [
            {"from": "/net/B", "to": "/net/A", "input_index": 0}])

    # ---- sticky note ----

    def test_sticky_notes_extracted(self):
        net = _FakeNode("net", sticky_notes=[
            _FakeStickyNote("先解算再缓存", (10.0, 20.0)),
            _FakeStickyNote("第二条", (1.0, 2.0)),
        ])
        a = _FakeNode("A", parent=net)
        b = _FakeNode("B", parent=net)
        c = _FakeNode("C", parent=net)
        a._inputs = [b]
        b._inputs = [c]
        self._select([a])
        result = self._handler().handle_capture_workflow_snapshot()
        sticky = result["sticky_notes"]
        self.assertEqual(len(sticky), 2)
        self.assertEqual({s["parent"] for s in sticky}, {"/net"})
        self.assertEqual({s["text"] for s in sticky},
                         {"先解算再缓存", "第二条"})
        positions_by_text = {s["text"]: s["position"] for s in sticky}
        self.assertEqual(positions_by_text["第二条"], [1.0, 2.0])
        self.assertEqual(positions_by_text["先解算再缓存"], [10.0, 20.0])

    def test_sticky_notes_api_missing_degrades_with_warning(self):
        net = _FakeNodeNoNotes("net")
        a = _FakeNode("A", parent=net)
        self._select([a])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["sticky_notes"], [])
        self.assertIn("_warning", result)
        self.assertTrue(any("iterStickyNotes" in w
                            for w in result["_warning"]))

    # ---- max_nodes 截断 ----

    def test_max_nodes_truncation(self):
        net = _FakeNode("net")
        c = _FakeNode("C", parent=net)
        b = _FakeNode("B", parent=net, inputs=[c])
        a = _FakeNode("A", parent=net, inputs=[b])
        self._select([a])
        result = self._handler().handle_capture_workflow_snapshot(
            max_nodes=2)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["node_count"], 2)
        self.assertEqual({n["path"] for n in result["nodes"]},
                         {"/net/A", "/net/B"})

    def test_max_nodes_default_50_applied(self):
        net = _FakeNode("net")
        chain = []
        for i in range(60):
            chain.append(_FakeNode("N%02d" % i, parent=net))
        for i in range(59):
            chain[i]._inputs = [chain[i + 1]]
        self._select([chain[0]])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["node_count"], 50)

    # ---- wrangle VEX ----

    def test_wrangle_vex_extracted_when_include_vex(self):
        net = _FakeNode("net")
        wrangle = _FakeNode("wrang", type_name="attribwrangle",
                            parent=net,
                            parm_values={"snippet": "@P.y += 1;"})
        self.server_mod.hou.node = lambda path: (
            wrangle if path == "/net/wrang" else None)
        result = self._handler().handle_capture_workflow_snapshot(
            node_path="/net/wrang", include_vex=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["root"], "/net/wrang")
        self.assertEqual(result["nodes"][0]["vex"], "@P.y += 1;")

    def test_wrangle_vex_skipped_when_include_vex_false(self):
        net = _FakeNode("net")
        wrangle = _FakeNode("wrang", type_name="attribwrangle",
                            parent=net,
                            parm_values={"snippet": "@P.y += 1;"})
        self.server_mod.hou.node = lambda path: (
            wrangle if path == "/net/wrang" else None)
        result = self._handler().handle_capture_workflow_snapshot(
            node_path="/net/wrang", include_vex=False)
        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["nodes"][0]["vex"])

    def test_comment_api_missing_degrades_with_warning(self):
        net = _FakeNode("net")
        a = _FakeNodeNoComment("A", parent=net)
        self._select([a])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["nodes"][0]["comment"])
        self.assertTrue(any("comment" in w for w in result["_warning"]))

    # ---- HDA 引用 ----

    def test_hda_reference_extracted(self):
        net = _FakeNode("net")
        definition = _FakeDefinition("C:/otls/mytool.hda")
        hda_node = _FakeNode("HDA1", type_name="mysop", parent=net,
                             definition=definition)
        self._select([hda_node])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["nodes"][0]["hda"], {
            "type_name": "Sop/mysop",
            "library_path": "C:/otls/mytool.hda",
        })

    # ---- 无几何数据 ----

    def test_snapshot_contains_no_geometry_data(self):
        net = _FakeNode("net")
        a = _FakeNode("A", parent=net, comment="A 节点注释")
        b = _FakeNode("B", parent=net, comment="B 节点")
        a._outputs = [b]
        self._select([a])
        result = self._handler().handle_capture_workflow_snapshot()
        blob = json.dumps(result, default=str)
        for forbidden in ("geometry", "point_count", "points",
                          "primitives", "vertex"):
            self.assertNotIn(forbidden, blob)

    # ---- 注册契约 ----

    def test_registration_contract(self):
        cls = self.Server
        instance = cls.__new__(cls)
        handlers = cls._get_command_handlers(instance)
        self.assertIn("capture_workflow_snapshot",
                      cls.READ_ONLY_COMMANDS)
        self.assertIn("capture_workflow_snapshot", handlers)
        self.assertNotIn("capture_workflow_snapshot",
                         cls.MUTATING_COMMANDS)
        self.assertNotIn("capture_workflow_snapshot",
                         cls.NO_UNDO_COMMANDS)
        # 唯一穷尽互斥断言不抛
        cls._validate_handler_classification(handlers)

    # ---- apply_response_cap ----

    def test_large_snapshot_capped(self):
        net = _FakeNode("net")
        chain = []
        for i in range(60):
            chain.append(_FakeNode("N%02d" % i, parent=net,
                                   comment="x" * 400))
        for i in range(59):
            chain[i]._inputs = [chain[i + 1]]
        self._select([chain[0]])
        result = self._handler().handle_capture_workflow_snapshot(
            max_nodes=60)
        self.assertEqual(result["status"], "success")
        self.assertIsInstance(result, dict)
        self.assertIn("_truncated", result)
        self.assertTrue(result["_truncated"])


if __name__ == "__main__":
    unittest.main()
