"""tests/test_workflow_capture.py — add-workflow-knowledge-capture Section B 单测。

覆盖 server.py 的 ``capture_workflow_snapshot`` handler（readOnly）：
- 定位：空选择 → ``no_selection`` 结构化错误（不静默回退）；node_path
  无效 → ``invalid_node_path``。
- 闭包遍历：seeds BFS（inputs + outputs），max_nodes 硬上限 + truncated
  标记 + 按 path 稳定排序；默认 max_nodes=50。
- 节点表：path / name / type / type_full / is_hda / comment / 非默认参数
  （parmTemplates，仅收录非默认、菜单与命令类跳过）/ vex（attribwrangle
  snippet）/ hda（资产级引用）/ errors / warnings；comment / VEX 读取异常
  降级 + ``_warning``，绝不 crash。
- 资产级标识（improve-knowledge-capture）：type_full =
  nameWithCategory（API 缺失降级 type）；is_hda = definition() 非 None；
  hda 字段 = {type_name, version(空串省略), definition_source
  (embedded/external)}，**绝不输出 library_path 或本机路径**；顶层
  hip_file = basename（隐私安全，异常降级空串）。
- HDA 内部研究：include_hda_internals=True → 展开判定（用户语义收敛）=
  children() 非空 且（用户资产（非 $HFS otls）／官方 HDA 带 Editable
  Nodes 声明（definition().hasSection("EditableNodes")，如
  rbdbulletsolver 的 dopnet/forces）／非 HDA 普通容器 subnet/geo）；
  官方无声明的封装 HDA（rbdconstraintproperties/rbdconfigure 实机
  64/102 children）默认不拆解；官方空壳节点（attribwrangle children
  恒空）不展开；不能用 isEditable() 判定（锁定态 isEditable False 但
  children 可读，且大 HDA 上定义比较可能极慢）；嵌套递归、同一
  max_nodes 预算与 truncated 语义。
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
import tempfile
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
    def __init__(self, library_path, version="", editable_nodes=False,
                 editable_nodes_content=None):
        self._library_path = library_path
        self._version = version
        self._editable_nodes = editable_nodes
        self._editable_nodes_content = editable_nodes_content

    def libraryFilePath(self):
        return self._library_path

    def version(self):
        return self._version

    def hasSection(self, section):
        # 官方 HDA 的 Editable Nodes 声明（Type Properties 字段）
        return section == "EditableNodes" and self._editable_nodes

    def binaryContents(self, section):
        # EditableNodes 段内容（UTF-8 多行相对路径，如 "dopnet/forces"）
        if section != "EditableNodes" or not self._editable_nodes:
            raise KeyError(section)
        content = self._editable_nodes_content
        if content is None:
            content = "dopnet/forces\n"
        if isinstance(content, str):
            return content.encode("utf-8")
        return content


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
    def __init__(self, name, value, default=None):
        self._name = name
        self._value = value
        self._default = default

    def name(self):
        return self._name

    def eval(self):
        return self._value

    def rawValue(self):
        return self._value

    def isAtDefault(self):
        # HOM 语义：无默认模板返回 None；有默认时按值对比
        if self._default is None:
            return None
        return self._value == self._default


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
    iterStickyNotes / children。"""

    def __init__(self, name, type_name="geo", parent=None, comment="",
                 templates=None, parm_values=None, inputs=None,
                 outputs=None, errors=None, warnings=None,
                 definition=None, sticky_notes=None, children=None,
                 is_editable=True, category="Sop"):
        self._name = name
        self._type = _FakeNodeType(type_name, category, definition)
        self._parent = parent
        self._comment = comment
        self._templates = list(templates) if templates else []
        self._parm_values = dict(parm_values) if parm_values else {}
        self._inputs = list(inputs) if inputs else []
        self._outputs = list(outputs) if outputs else []
        self._errors = list(errors) if errors else []
        self._warnings = list(warnings) if warnings else []
        self._sticky_notes = list(sticky_notes) if sticky_notes else []
        self._children = list(children) if children else []
        self._is_editable = is_editable

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
            default = None
            for template in self._templates:
                if template.name() == name:
                    default = template.defaultValue()
                    break
            return _FakeParm(name, self._parm_values[name], default)
        return None

    def evalParm(self, name):
        return self._parm_values.get(name)

    def node(self, rel_path):
        # 模拟 hou.Node.node() 相对路径解析（EditableNodes 段路径定位）
        current = self
        for segment in rel_path.split("/"):
            if not segment:
                continue
            found = None
            for child in current._children:
                if child.name() == segment:
                    found = child
                    break
            if found is None:
                return None
            current = found
        return current

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

    def children(self):
        return list(self._children)

    def isEditable(self):
        return self._is_editable


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
        # conftest stub 自带 hipFile（basename 返回 "untitled"）；统一覆写为
        # 确定性值，个别用例再单独改（如路径降级测试）
        hou.hipFile = types.SimpleNamespace(basename=lambda: "scene.hip")
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
        result = self._handler().handle_capture_workflow_snapshot(
            include_connected=True)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["root"], "selection")
        self.assertEqual(result["node_count"], 3)
        self.assertFalse(result["truncated"])
        # 无降级 → 不出现 _warning；顶层键集合精确（含 hip_file）
        self.assertEqual(set(result.keys()), {
            "status", "root", "node_count", "truncated",
            "hip_file", "nodes", "sticky_notes", "connections"})
        self.assertEqual(result["hip_file"], "scene.hip")

        by_path = {n["path"]: n for n in result["nodes"]}
        self.assertEqual(set(by_path.keys()),
                         {"/net/A", "/net/B", "/net/C"})
        node_a = by_path["/net/A"]
        self.assertEqual(set(node_a.keys()), {
            "path", "name", "type", "type_full", "is_hda", "comment",
            "params", "vex", "hda", "errors", "warnings"})
        self.assertEqual(node_a["name"], "A")
        self.assertEqual(node_a["type"], "geo")
        self.assertEqual(node_a["type_full"], "Sop/geo")
        self.assertIs(node_a["is_hda"], False)
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
            max_nodes=2, include_connected=True)
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
        result = self._handler().handle_capture_workflow_snapshot(
            include_connected=True)
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

    # ---- 资产级标识降级 ----

    def test_type_full_falls_back_to_type_when_api_missing(self):
        net = _FakeNode("net")

        class _NoCategoryType(object):
            def name(self):
                return "weird"

            def nameWithCategory(self):
                raise AttributeError("no nameWithCategory API")

            def definition(self):
                return None

        node = _FakeNode("A", parent=net)
        node._type = _NoCategoryType()
        self._select([node])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        entry = result["nodes"][0]
        self.assertEqual(entry["type"], "weird")
        self.assertEqual(entry["type_full"], "weird")
        self.assertIs(entry["is_hda"], False)

    # ---- HDA 资产级引用（improve-knowledge-capture）----

    def test_hda_asset_reference_with_version_and_source(self):
        net = _FakeNode("net")
        definition = _FakeDefinition("C:/otls/mytool.hda", version="1.2.3")
        hda_node = _FakeNode("HDA1", type_name="mysop", parent=net,
                             definition=definition)
        self._select([hda_node])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        entry = result["nodes"][0]
        self.assertEqual(entry["type_full"], "Sop/mysop")
        self.assertIs(entry["is_hda"], True)
        # hda 字段：资产全名 + 版本 + external 判定（.hda → 非 hip 内嵌）
        self.assertEqual(entry["hda"], {
            "type_name": "Sop/mysop",
            "version": "1.2.3",
            "definition_source": "external",
        })
        # 响应绝不输出 library_path 或任何本机路径
        blob = json.dumps(result, default=str)
        self.assertNotIn("library_path", blob)
        self.assertNotIn("C:/otls", blob)
        self.assertNotIn("mytool.hda", blob)

    def test_hda_embedded_source_and_version_omitted_when_empty(self):
        net = _FakeNode("net")
        # libraryFilePath 指向 .hipnc → embedded；version() 空串 → 省略
        definition = _FakeDefinition("C:/work/scene.hipnc", version="")
        hda_node = _FakeNode("HDA1", type_name="mysop", parent=net,
                             definition=definition)
        self._select([hda_node])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["nodes"][0]["hda"], {
            "type_name": "Sop/mysop",
            "definition_source": "embedded",
        })
        self.assertNotIn("version", result["nodes"][0]["hda"])

    def test_hda_embedded_hip_and_missing_path_degrades(self):
        net = _FakeNode("net")
        # .hip → embedded；路径不可得 → 保守不标用户资产（is_hda False，
        # hda None，不 crash）
        hip_def = _FakeDefinition("D:/shot/asset.hip")
        hip_hda = _FakeNode("HDA1", type_name="mysop", parent=net,
                            definition=hip_def)
        self._select([hip_hda])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["nodes"][0]["hda"]["definition_source"],
                         "embedded")

        net2 = _FakeNode("net2")

        class _NoPathDefinition(object):
            def libraryFilePath(self):
                raise AttributeError("no path API")

            def version(self):
                return ""

        nodepath_hda = _FakeNode("HDA2", type_name="mysop", parent=net2,
                                 definition=_NoPathDefinition())
        self._select([nodepath_hda])
        result2 = self._handler().handle_capture_workflow_snapshot()
        entry2 = result2["nodes"][0]
        self.assertIs(entry2["is_hda"], False)
        self.assertIsNone(entry2["hda"])

    def test_builtin_hda_backed_type_not_flagged_as_user_asset(self):
        # H21 实测 attribwrangle 等 HDA 化内建类型的 definition 挂在
        # $HFS/houdini/otls/OPlibSop.hda —— 不得误标为用户 HDA（实施修正）
        net = _FakeNode("net")
        builtin_def = _FakeDefinition(
            "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.596/houdini/otls/OPlibSop.hda")
        wrangle = _FakeNode("wrang", type_name="attribwrangle", parent=net,
                            definition=builtin_def)
        self._select([wrangle])
        saved_hfs = os.environ.get("HFS")
        try:
            os.environ["HFS"] = "C:/Program Files/Side Effects Software/" \
                                "Houdini 21.0.596"
            result = self._handler().handle_capture_workflow_snapshot()
        finally:
            if saved_hfs is None:
                os.environ.pop("HFS", None)
            else:
                os.environ["HFS"] = saved_hfs
        entry = result["nodes"][0]
        self.assertIs(entry["is_hda"], False)
        self.assertIsNone(entry["hda"])
        # 资产级标识仍可用：type_full 正常
        self.assertEqual(entry["type_full"], "Sop/attribwrangle")

    def test_user_asset_detected_without_hfs_env(self):
        # HFS env 不可得时外部 .hda 仍判为用户资产（判定不依赖 HFS）
        net = _FakeNode("net")
        definition = _FakeDefinition("C:/otls/mytool.hda", version="2.0")
        hda_node = _FakeNode("HDA1", type_name="mysop", parent=net,
                             definition=definition)
        self._select([hda_node])
        saved_hfs = os.environ.get("HFS")
        try:
            os.environ.pop("HFS", None)
            result = self._handler().handle_capture_workflow_snapshot()
        finally:
            if saved_hfs is not None:
                os.environ["HFS"] = saved_hfs
        entry = result["nodes"][0]
        self.assertIs(entry["is_hda"], True)
        self.assertEqual(entry["hda"]["definition_source"], "external")

    def test_hip_file_never_contains_full_path(self):
        net = _FakeNode("net")
        a = _FakeNode("A", parent=net)
        self._select([a])
        # 即使 hipFile.path() 能给出完整路径，快照也只用 basename（隐私安全）
        self.server_mod.hou.hipFile = types.SimpleNamespace(
            basename=lambda: "shoot.hip",
            path=lambda: "C:/Users/someone/scenes/shoot.hip")
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["hip_file"], "shoot.hip")
        blob = json.dumps(result, default=str)
        self.assertNotIn("scenes", blob)
        self.assertNotIn("C:/Users", blob)

    def test_hip_file_degrades_to_empty_on_api_failure(self):
        net = _FakeNode("net")
        a = _FakeNode("A", parent=net)
        self._select([a])

        def boom():
            raise RuntimeError("hipFile API 不可用")

        self.server_mod.hou.hipFile.basename = boom
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["hip_file"], "")

    # ---- HDA 内部研究（include_hda_internals）----

    def test_hda_internals_expanded_by_default_for_unlocked_user_asset(self):
        # 新语义（分层探测 auto）：解锁实例（isEditable=True，含用户自制
        # HDA 与解锁官方）默认整棵渗透；probe_mode="none" /
        # include_hda_internals=False 显式关闭内部展开。
        net = _FakeNode("net")
        definition = _FakeDefinition("C:/otls/mytool.hda")
        inner = _FakeNode("inner_wrangle", type_name="attribwrangle",
                          parent=net,
                          parm_values={"snippet": "@P.y += 1;"})
        hda_node = _FakeNode("HDA1", type_name="mysop", parent=net,
                             definition=definition, children=[inner])
        inner._parent = hda_node
        self._select([hda_node])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["node_count"], 2)
        self.assertEqual({n["path"] for n in result["nodes"]},
                         {"/net/HDA1", "/net/HDA1/inner_wrangle"})
        # 显式关闭内部展开 → 仅根节点
        result_none = self._handler().handle_capture_workflow_snapshot(
            probe_mode="none")
        self.assertEqual(result_none["node_count"], 1)
        self.assertEqual({n["path"] for n in result_none["nodes"]},
                         {"/net/HDA1"})
        # 兼容旧参数 include_hda_internals=False → 同样不展开
        result_legacy = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=False)
        self.assertEqual(result_legacy["node_count"], 1)

    def test_official_locked_hda_without_children_not_expanded(self):
        # H21 实测：官方 OPlib 节点（如 attribwrangle）内容锁定且
        # children() 恒空 → 无可拆解内容，不展开（用户需求：官方节点
        # 默认不拆解分析）
        net = _FakeNode("net")
        official_def = _FakeDefinition(
            "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.596/houdini/otls/OPlibSop.hda")
        official = _FakeNode("official_asset", type_name="attribwrangle",
                             parent=net, definition=official_def,
                             is_editable=False)
        self._select([official])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["node_count"], 1)
        self.assertEqual({n["path"] for n in result["nodes"]},
                         {"/net/official_asset"})

    def test_locked_user_hda_not_expanded_without_explicit_override(self):
        # 新语义（分层探测 auto）：锁定用户数字资产（isEditable False，
        # 如 csr_voronoi_advanced1）默认**只记节点名**（不展开内部）；
        # 调用方可传 probe_mode="expand_all" 显式要求展开。
        net = _FakeNode("net")
        definition = _FakeDefinition("C:/otls/mytool.hda")
        inner = _FakeNode("inner_wrangle", type_name="attribwrangle",
                          parent=net,
                          parm_values={"snippet": "@P.y += 1;"})
        locked_hda = _FakeNode("HDA1", type_name="mysop", parent=net,
                               definition=definition, children=[inner],
                               is_editable=False)
        inner._parent = locked_hda
        self._select([locked_hda])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["node_count"], 1)
        self.assertEqual({n["path"] for n in result["nodes"]},
                         {"/net/HDA1"})
        # 显式 expand_all → 整棵渗透
        result_all = self._handler().handle_capture_workflow_snapshot(
            probe_mode="expand_all")
        by_path = {n["path"]: n for n in result_all["nodes"]}
        self.assertEqual(set(by_path.keys()),
                         {"/net/HDA1", "/net/HDA1/inner_wrangle"})

    def test_node_without_children_not_expanded(self):
        # 无内部内容（children 空）的节点即使可编辑也不展开
        net = _FakeNode("net")
        empty = _FakeNode("empty", type_name="attribwrangle", parent=net,
                          is_editable=True)
        self._select([empty])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["node_count"], 1)
        self.assertEqual({n["path"] for n in result["nodes"]},
                         {"/net/empty"})

    def test_editable_contents_node_expanded_even_with_builtin_path(self):
        # 官方节点带 Editable Nodes 声明（hasSection("EditableNodes")，
        # 如 rbdbulletsolver 的 dopnet/forces）→ 参与分析
        net = _FakeNode("net")
        official_def = _FakeDefinition(
            "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.596/houdini/otls/OPlibSop.hda",
            editable_nodes=True)
        inner = _FakeNode("editable_inner", parent=net)
        official = _FakeNode("unlocked_official", type_name="attribwrangle",
                             parent=net, definition=official_def,
                             children=[inner], is_editable=True)
        inner._parent = official
        self._select([official])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        by_path = {n["path"]: n for n in result["nodes"]}
        self.assertEqual(set(by_path.keys()),
                         {"/net/unlocked_official",
                          "/net/unlocked_official/editable_inner"})

    def test_official_hda_without_editable_nodes_not_expanded(self):
        # 官方无声明的封装 HDA（rbdconstraintproperties 实机 64 children
        # / rbdconfigure 102 children，均无 EditableNodes section）→
        # 即使内部有封装内容也不拆解（用户预期：官方节点默认不拆解）
        net = _FakeNode("net")
        official_def = _FakeDefinition(
            "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.596/houdini/otls/OPlibSop.hda")
        inner = _FakeNode("wrapped_inner", parent=net)
        official = _FakeNode("rbdconstraintproperties1",
                             type_name="rbdconstraintproperties",
                             parent=net, definition=official_def,
                             children=[inner], is_editable=False)
        inner._parent = official
        self._select([official])
        saved_hfs = os.environ.get("HFS")
        try:
            os.environ["HFS"] = "C:/Program Files/Side Effects Software/" \
                                "Houdini 21.0.596"
            result = self._handler().handle_capture_workflow_snapshot(
                include_hda_internals=True)
        finally:
            if saved_hfs is None:
                os.environ.pop("HFS", None)
            else:
                os.environ["HFS"] = saved_hfs
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["node_count"], 1)
        self.assertEqual({n["path"] for n in result["nodes"]},
                         {"/net/rbdconstraintproperties1"})

    def test_editable_only_probes_forces_subtree_only(self):
        # 锁定官方 + EditableNodes 声明（内容 "dopnet/forces"）→ 只探
        # editable 子树：Dop 容器（dopnet）进入、forces 子树完整捕获，
        # 其余内部网络（solver_internals）不展开（H21 全量 307 节点
        # 的根因修复）。
        net = _FakeNode("net")
        official_def = _FakeDefinition(
            "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.596/houdini/otls/OPlibDop.hda",
            editable_nodes=True,
            editable_nodes_content="dopnet/forces\n")
        wrangle = _FakeNode("inner_wrangle", type_name="attribwrangle",
                            parent=net,
                            parm_values={"snippet": "f@age = 1.0;"})
        forces = _FakeNode("forces", type_name="forces", parent=net,
                           category="Dop", children=[wrangle])
        dopnet = _FakeNode("dopnet", type_name="dopnet", parent=net,
                           category="Dop", children=[forces])
        hidden_inner = _FakeNode("hidden_w", type_name="attribwrangle",
                                 parent=net,
                                 parm_values={"snippet": "f@x = 2.0;"})
        solver_internals = _FakeNode("solver_internals", type_name="subnet",
                                     parent=net, children=[hidden_inner])
        rbdbulletsolver = _FakeNode(
            "rbdbulletsolver1", type_name="rbdbulletsolver", parent=net,
            definition=official_def, is_editable=False,
            children=[dopnet, solver_internals])
        wrangle._parent = forces
        forces._parent = dopnet
        dopnet._parent = rbdbulletsolver
        hidden_inner._parent = solver_internals
        solver_internals._parent = rbdbulletsolver
        self._select([rbdbulletsolver])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["truncated"])
        by_path = {n["path"]: n for n in result["nodes"]}
        self.assertEqual(set(by_path.keys()), {
            "/net/rbdbulletsolver1",
            "/net/rbdbulletsolver1/dopnet",
            "/net/rbdbulletsolver1/dopnet/forces",
            "/net/rbdbulletsolver1/dopnet/forces/inner_wrangle"})
        # 无关内部网络不进入（只探 editable 子树）
        self.assertNotIn("/net/rbdbulletsolver1/solver_internals",
                         by_path)
        self.assertNotIn("/net/rbdbulletsolver1/solver_internals/"
                         "hidden_w", by_path)
        # forces 子树内 wrangle 与外部节点同构产出（VEX）
        self.assertEqual(
            by_path["/net/rbdbulletsolver1/dopnet/forces/inner_wrangle"]
            ["vex"], "f@age = 1.0;")

    def test_unlocked_official_hda_fully_expanded(self):
        # 解锁官方 HDA（isEditable=True，如 transformpieces1）→ 整棵渗透，
        # 不因"官方无 EditableNodes 声明"跳过
        net = _FakeNode("net")
        official_def = _FakeDefinition(
            "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.596/houdini/otls/OPlibSop.hda")
        inner_a = _FakeNode("wrangle_a", type_name="attribwrangle",
                            parent=net,
                            parm_values={"snippet": "@P.x += 1.0;"})
        inner_b = _FakeNode("sub_b", type_name="subnet", parent=net,
                            children=[])
        xformpieces = _FakeNode("transformpieces1",
                                type_name="xformpieces", parent=net,
                                definition=official_def, is_editable=True,
                                children=[inner_a, inner_b])
        inner_a._parent = xformpieces
        inner_b._parent = xformpieces
        self._select([xformpieces])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["truncated"])
        by_path = {n["path"]: n for n in result["nodes"]}
        self.assertEqual(set(by_path.keys()), {
            "/net/transformpieces1",
            "/net/transformpieces1/wrangle_a",
            "/net/transformpieces1/sub_b"})
        self.assertEqual(
            by_path["/net/transformpieces1/wrangle_a"]["vex"],
            "@P.x += 1.0;")

    def test_default_closure_does_not_follow_connections(self):
        # 默认闭包只沿 children 展开：连线邻居（inputs/outputs）不入队，
        # include_connected=True 时才扩展（两阶段预算）
        net = _FakeNode("net")
        sub = _FakeNode("mysubnet", type_name="subnet", parent=net)
        sub_inner = _FakeNode("sub_w", type_name="attribwrangle", parent=sub,
                              parm_values={"snippet": "f@x = 1.0;"})
        sub._children = [sub_inner]
        up = _FakeNode("upstream", parent=net)
        down = _FakeNode("downstream", parent=net)
        sub._inputs = [up]
        sub._outputs = [down]
        self._select([sub])
        result = self._handler().handle_capture_workflow_snapshot()
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["truncated"])
        self.assertEqual({n["path"] for n in result["nodes"]},
                         {"/net/mysubnet", "/net/mysubnet/sub_w"})
        # include_connected=True → 连线邻居进入（用剩余预算）
        result_conn = self._handler().handle_capture_workflow_snapshot(
            include_connected=True)
        self.assertEqual(
            {n["path"] for n in result_conn["nodes"]},
            {"/net/mysubnet", "/net/mysubnet/sub_w",
             "/net/upstream", "/net/downstream"})

    def test_budget_prioritizes_forced_subtree(self):
        # 预算两阶段：强制子树（EditableNodes 路径）优先完整进入，连接节点
        # 用剩余预算，不挤占目标子树
        net = _FakeNode("net")
        official_def = _FakeDefinition(
            "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.596/houdini/otls/OPlibDop.hda",
            editable_nodes=True,
            editable_nodes_content="dopnet/forces\n")
        inner = []
        for i in range(4):
            inner.append(_FakeNode("inner%02d" % i, type_name="wrangle",
                                   parent=net))
        forces = _FakeNode("forces", type_name="forces", parent=net,
                           category="Dop", children=inner)
        dopnet = _FakeNode("dopnet", type_name="dopnet", parent=net,
                           category="Dop", children=[forces])
        solver = _FakeNode("rbdbulletsolver1", type_name="rbdbulletsolver",
                           parent=net, definition=official_def,
                           is_editable=False, children=[dopnet])
        for node in inner:
            node._parent = forces
        forces._parent = dopnet
        dopnet._parent = solver
        connected = [_FakeNode("conn%02d" % i, parent=net)
                     for i in range(6)]
        for i in range(5):
            connected[i]._inputs = [connected[i + 1]]
        solver._inputs = [connected[0]]
        self._select([solver])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True, include_connected=True,
            max_nodes=10)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["truncated"])
        by_path = {n["path"]: n for n in result["nodes"]}
        # 强制子树完整（根 + dopnet + forces + 4 inner = 7）
        for expected in ("/net/rbdbulletsolver1",
                         "/net/rbdbulletsolver1/dopnet",
                         "/net/rbdbulletsolver1/dopnet/forces",
                         "/net/rbdbulletsolver1/dopnet/forces/inner00",
                         "/net/rbdbulletsolver1/dopnet/forces/inner03"):
            self.assertIn(expected, by_path)
        # 连接节点用剩余预算（10 - 7 = 3），其余被截断
        conn_paths = [p for p in by_path if "/net/conn" in p]
        self.assertEqual(len(conn_paths), 3)
        self.assertIn("/net/conn00", conn_paths)
        self.assertIn("/net/conn02", conn_paths)

    def test_summary_and_pagination(self):
        # 输出摘要/分页：完整 JSON 超阈值 → 精简摘要 + 全量落盘
        # （summary_file basename，无敏感路径）+ offset/limit 续读全量
        # 详情节点（page.total / next_offset）
        net = _FakeNode("net")
        nodes = []
        for i in range(5):
            nodes.append(_FakeNode("N%02d" % i, parent=net,
                                   comment="x" * 500))
        for i in range(4):
            nodes[i]._inputs = [nodes[i + 1]]
        self._select([nodes[0]])
        original = self.server_mod._SNAPSHOT_SUMMARY_THRESHOLD
        self.server_mod._SNAPSHOT_SUMMARY_THRESHOLD = 2048
        self.addCleanup(
            setattr, self.server_mod, "_SNAPSHOT_SUMMARY_THRESHOLD",
            original)
        result = self._handler().handle_capture_workflow_snapshot(
            include_connected=True)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result.get("summary"))
        # summary_file 只是 basename（不含路径分隔符）
        summary_file = result.get("summary_file")
        self.assertTrue(summary_file)
        self.assertNotIn("/", summary_file)
        self.assertNotIn("\\", summary_file)
        # 摘要行：精简字段 + 计数，无详情参数
        entry = result["nodes"][0]
        self.assertEqual(set(entry.keys()), {
            "path", "name", "type", "type_full", "is_hda",
            "has_vex", "param_count"})
        self.assertNotIn("params", entry)
        self.assertEqual(result["node_count"], 5)
        # 清理落盘文件
        full_path = os.path.join(
            tempfile.gettempdir(), summary_file)
        if os.path.isfile(full_path):
            os.remove(full_path)
        # 分页续读：offset/limit 返回全量详情条目
        page = self._handler().handle_capture_workflow_snapshot(
            include_connected=True, offset=2, limit=2)
        self.assertEqual(page["status"], "success")
        self.assertNotIn("summary", page)
        self.assertEqual(len(page["nodes"]), 2)
        self.assertEqual(page["nodes"][0]["path"], "/net/N02")
        self.assertIn("comment", page["nodes"][0])
        self.assertEqual(page["page"]["total"], 5)
        self.assertEqual(page["page"]["next_offset"], 4)
        last = self._handler().handle_capture_workflow_snapshot(
            include_connected=True, offset=4, limit=2)
        self.assertEqual(len(last["nodes"]), 1)
        self.assertIsNone(last["page"]["next_offset"])

    def test_official_hda_non_default_params_not_empty(self):
        # 锁定官方 HDA（无 EditableNodes）仅参数：非默认参数必须可靠产出
        # （与 get_node_info 统一 isAtDefault 语义，修复快照 params 恒空）
        net = _FakeNode("net")
        official_def = _FakeDefinition(
            "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.596/houdini/otls/OPlibSop.hda")
        official = _FakeNode(
            "official_asset", type_name="attribwrangle", parent=net,
            definition=official_def, is_editable=False,
            templates=[
                _FakeParmTemplate("scale", 101, 1.0),     # Float 非默认
                _FakeParmTemplate("size", 101, 1.0),       # Float 默认
                _FakeParmTemplate("toggle", 104, True),    # Toggle 非默认
            ],
            parm_values={"scale": 2.5, "size": 1.0,
                         "toggle": False})
        self._select([official])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["node_count"], 1)
        entry = result["nodes"][0]
        # 官方 HDA 非默认参数非空（size 保持默认被排除）
        self.assertEqual(entry["params"], {"scale": 2.5, "toggle": False})

    def test_invalid_probe_mode_returns_structured_error(self):
        net = _FakeNode("net")
        a = _FakeNode("A", parent=net)
        self._select([a])
        result = self._handler().handle_capture_workflow_snapshot(
            probe_mode="bogus")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_probe_mode")
        self.assertIn("auto", result["error"]["message"])

    def test_plain_network_container_expanded(self):
        # 非 HDA 普通容器（subnet，definition None）+ children 非空 →
        # 展开（其 children 是用户工作流内容）
        net = _FakeNode("net")
        sub = _FakeNode("mysubnet", type_name="subnet", parent=net)
        sub_inner = _FakeNode("sub_w", type_name="attribwrangle", parent=sub,
                              parm_values={"snippet": "f@x = 1.0;"})
        sub._children = [sub_inner]
        self._select([sub])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        by_path = {n["path"]: n for n in result["nodes"]}
        self.assertEqual(set(by_path.keys()),
                         {"/net/mysubnet", "/net/mysubnet/sub_w"})

    def test_hda_internals_expanded_with_vex(self):
        net = _FakeNode("net")
        definition = _FakeDefinition("C:/otls/mytool.hda")
        inner = _FakeNode("inner_wrangle", type_name="attribwrangle",
                          comment="激活算法",
                          parm_values={"snippet": "f@activation = @P.y > 0.1;"})
        hda_node = _FakeNode("HDA1", type_name="mysop", parent=net,
                             definition=definition, children=[inner])
        # 真实 Houdini 中 HDA 内部节点 path 含资产名层级；fake 需手动挂 parent
        inner._parent = hda_node
        self._select([hda_node])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["truncated"])
        by_path = {n["path"]: n for n in result["nodes"]}
        self.assertEqual(set(by_path.keys()),
                         {"/net/HDA1", "/net/HDA1/inner_wrangle"})
        # 内部节点与外部节点同构产出（type_full + vex + comment）
        entry = by_path["/net/HDA1/inner_wrangle"]
        self.assertEqual(entry["type_full"], "Sop/attribwrangle")
        self.assertIs(entry["is_hda"], False)
        self.assertEqual(entry["vex"], "f@activation = @P.y > 0.1;")
        self.assertEqual(entry["comment"], "激活算法")
        # HDA 节点本身仍产出 hda 引用
        self.assertEqual(by_path["/net/HDA1"]["hda"]["type_name"],
                         "Sop/mysop")

    def test_hda_internals_nested_hda_recursion(self):
        net = _FakeNode("net")
        outer_def = _FakeDefinition("C:/otls/outer.hda")
        inner_def = _FakeDefinition("C:/otls/inner.hda")
        wrangle = _FakeNode("deep_wrangle", type_name="attribwrangle",
                            parm_values={"snippet": "@P.z = 0;"})
        inner_hda = _FakeNode("inner_asset", type_name="inner_asset",
                              definition=inner_def, children=[wrangle])
        outer_hda = _FakeNode("outer_asset", type_name="outer_asset",
                              parent=net, definition=outer_def,
                              children=[inner_hda])
        # fake 层级：wrangle 挂 inner_hda，inner_hda 挂 outer_hda
        wrangle._parent = inner_hda
        inner_hda._parent = outer_hda
        self._select([outer_hda])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True)
        self.assertEqual(result["status"], "success")
        by_path = {n["path"]: n for n in result["nodes"]}
        self.assertEqual(set(by_path.keys()), {
            "/net/outer_asset", "/net/outer_asset/inner_asset",
            "/net/outer_asset/inner_asset/deep_wrangle"})
        self.assertEqual(
            by_path["/net/outer_asset/inner_asset/deep_wrangle"]["vex"],
            "@P.z = 0;")

    def test_hda_internals_respect_max_nodes_budget(self):
        net = _FakeNode("net")
        definition = _FakeDefinition("C:/otls/big.hda")
        internals = [_FakeNode("inner%02d" % i, parent=net)
                     for i in range(20)]
        hda_node = _FakeNode("big_asset", type_name="big_asset", parent=net,
                             definition=definition, children=internals)
        self._select([hda_node])
        result = self._handler().handle_capture_workflow_snapshot(
            include_hda_internals=True, max_nodes=5)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["node_count"], 5)

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
            max_nodes=60, include_connected=True)
        self.assertEqual(result["status"], "success")
        self.assertIsInstance(result, dict)
        self.assertIn("_truncated", result)
        self.assertTrue(result["_truncated"])


if __name__ == "__main__":
    unittest.main()
