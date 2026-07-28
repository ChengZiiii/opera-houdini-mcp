"""add-pdg-tops-tools 单元测试。

覆盖 5 个 PDG/TOPs API 的状态机：异步 handle、blocking terminal、
blocking 超时（不自动 cancel）、重复调用幂等、terminal 后重启 cook、
registry 重启/未知 ID 语义、dirty(remove_outputs=False)、cancel 幂等、
API 失败与 response cap。并断言 5 个新增命令在三分类中互斥且穷尽，
server handler 走 apply_response_cap 且不创建 undo group，bridge 恰好
5 个无注解中文工具。真实 H21 TOP network 由 h21_live_pdg_smoke.py 覆盖。
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
PDG_PATH = os.path.join(ROOT, "_pdg.py")
SERVER_PATH = os.path.join(ROOT, "server.py")
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


class HomPermissionError(Exception):
    pass


class _CookState(object):
    """模拟 pdg.cookState / workItemState：通过 .name() 暴露状态名。"""
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _WorkItem(object):
    def __init__(self, index, name, state):
        self.index = index
        self.name = name
        self.state = _CookState(state)


class _PdgNode(object):
    def __init__(self, items):
        self.workItems = list(items)


class _TopNode(object):
    def __init__(self, path="/tasks/topnet1"):
        self._path = path
        self.cook_calls = []
        self.execute_calls = []
        self.dirty_calls = []
        self.cancel_calls = []
        # 默认处于 cooking；测试可覆盖状态序列。
        self._states = [_CookState("cooking")]
        self._work_item_states = {}
        self._pdg_node = None
        self.cook_error = None

    def path(self):
        return self._path

    def set_states(self, states):
        self._states = list(states)

    def cookWorkItems(self, block=False):
        self.cook_calls.append(block)
        if self.cook_error is not None:
            raise self.cook_error

    def executeGraph(self, block=False):
        self.execute_calls.append(block)

    def getCookState(self, force=True):
        if not self._states:
            return _CookState("cooking")
        if len(self._states) == 1:
            return self._states[0]
        return self._states.pop(0)

    def workItemStates(self):
        return dict(self._work_item_states)

    def dirtyWorkItems(self, remove_outputs=False):
        self.dirty_calls.append(remove_outputs)

    def cancelCook(self):
        self.cancel_calls.append(True)

    def getPDGNode(self):
        return self._pdg_node


class _NotATopNode(object):
    """缺乏 TopNode 控制面的普通节点。"""
    def path(self):
        return "/obj/geo1"


class _Hou(object):
    PermissionError = HomPermissionError
    TopNode = _TopNode

    def __init__(self, node=None):
        self._node = node

    def node(self, path):
        if self._node is not None and path == self._node._path:
            return self._node
        return None


_pkg = types.ModuleType("pdg_test_pkg")
_pkg.__path__ = [ROOT]
sys.modules["pdg_test_pkg"] = _pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_pdg():
    common_name = "pdg_test_pkg._common"
    pdg_name = "pdg_test_pkg._pdg"
    for name in (pdg_name, common_name):
        sys.modules.pop(name, None)
    _load(common_name, os.path.join(ROOT, "_common.py"))
    return _load(pdg_name, PDG_PATH)


def _make_hou(states=None, work_item_states=None, pdg_items=None,
              path="/tasks/topnet1"):
    node = _TopNode(path)
    if states is not None:
        node.set_states(states)
    if work_item_states is not None:
        node._work_item_states = dict(work_item_states)
    if pdg_items is not None:
        node._pdg_node = _PdgNode(pdg_items)
    return _Hou(node), node


class ModuleHygieneTests(unittest.TestCase):
    def test_module_does_not_import_hou_or_new_dependencies(self):
        source = open(PDG_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        self.assertNotIn("hou", imports)
        self.assertNotIn("pdg", imports)
        # 仅允许标准库 + 包内相对 _common。
        self.assertTrue(imports <= {"time", "uuid", ""})

    def test_forbidden_apis_absent(self):
        source = open(PDG_PATH, "r", encoding="utf-8").read()
        # 不得默认 dirty 全部 work item 或删除输出。
        self.assertNotIn("dirtyAllWorkItems", source)
        self.assertNotIn("remove_outputs=True", source)
        # cook 控制不得走 pdg.Graph 顶层方法或 GraphContext 直接调度。
        self.assertNotIn("executeGraph(block=True)", source)


class CookStateMachineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdg = _load_pdg()

    def setUp(self):
        self.pdg._COOK_REGISTRY.clear()
        self.pdg._NODE_TO_COOK.clear()

    def test_async_cook_returns_process_scoped_handle(self):
        hou, node = _make_hou(states=[_CookState("cooking")])
        result = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        self.assertEqual(result["status"], "started")
        self.assertTrue(result["cook_id"].startswith("pdg-"))
        self.assertEqual(result["scope"], "process")
        self.assertEqual(result["started_count"], 1)
        self.assertFalse(result["fallback_used"])
        self.assertEqual(node.cook_calls, [False])
        # handle 登记为 active。
        self.assertEqual(self.pdg._NODE_TO_COOK.get("/tasks/topnet1"),
                         result["cook_id"])

    def test_duplicate_cook_returns_same_handle_without_second_cook(self):
        hou, node = _make_hou(states=[_CookState("cooking")])
        first = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        second = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        self.assertEqual(second["status"], "already_running")
        self.assertEqual(second["cook_id"], first["cook_id"])
        # cookWorkItems 只被调用一次。
        self.assertEqual(node.cook_calls, [False])

    def test_blocking_cook_polls_until_terminal(self):
        states = [_CookState("cooking"), _CookState("cooking"),
                  _CookState("success")]
        hou, node = _make_hou(states=states)
        result = self.pdg.pdg_cook(hou, "/tasks/topnet1", blocking=True)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["state"], "success")
        self.assertEqual(node.cook_calls, [False])
        # terminal 后 active 映射关闭。
        self.assertNotIn("/tasks/topnet1", self.pdg._NODE_TO_COOK)

    def test_blocking_timeout_does_not_cancel_and_keeps_handle_active(self):
        hou, node = _make_hou(states=[_CookState("cooking")])
        result = self.pdg.pdg_cook(
            hou, "/tasks/topnet1", blocking=True, timeout_seconds=0.01)
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["timed_out"])
        self.assertFalse(result.get("terminal", False))
        # handle 保持 active，未自动 cancel。
        self.assertIn("/tasks/topnet1", self.pdg._NODE_TO_COOK)
        self.assertEqual(node.cancel_calls, [])
        cook_id = result["cook_id"]
        # 后续 status 仍可使用同一 handle。
        status = self.pdg.pdg_status(hou, "/tasks/topnet1", cook_id=cook_id)
        self.assertEqual(status["handle"]["cook_id"], cook_id)
        # 后续 cancel 仍可使用同一 handle。
        cancel = self.pdg.pdg_cancel(hou, "/tasks/topnet1", cook_id=cook_id)
        self.assertTrue(cancel["cancelled"])
        self.assertEqual(node.cancel_calls, [True])

    def test_terminal_then_new_cook_generates_new_id(self):
        states = [_CookState("cooking"), _CookState("success")]
        hou, node = _make_hou(states=states)
        first = self.pdg.pdg_cook(hou, "/tasks/topnet1", blocking=True)
        self.assertEqual(first["status"], "success")
        # 再 cook：无 active handle，生成新 ID。
        node.set_states([_CookState("cooking")])
        second = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        self.assertNotEqual(second["cook_id"], first["cook_id"])
        self.assertEqual(second["status"], "started")

    def test_unknown_or_expired_cook_id_is_structured_error(self):
        hou, _ = _make_hou()
        result = self.pdg.pdg_status(
            hou, "/tasks/topnet1", cook_id="pdg-bogus")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "unknown_cook_id")
        # registry 重启（清空）后旧 handle 失效。
        first = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        stale_id = first["cook_id"]
        self.pdg._COOK_REGISTRY.clear()
        self.pdg._NODE_TO_COOK.clear()
        expired = self.pdg.pdg_status(
            hou, "/tasks/topnet1", cook_id=stale_id)
        self.assertEqual(expired["error"]["code"], "unknown_cook_id")

    def test_cook_id_must_belong_to_the_node(self):
        hou, _ = _make_hou()
        first = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        # 同一 cook_id 查询另一个不存在的节点路径直接 node_not_found。
        other = self.pdg.pdg_status(
            hou, "/tasks/other", cook_id=first["cook_id"])
        self.assertEqual(other["error"]["code"], "node_not_found")


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdg = _load_pdg()

    def setUp(self):
        self.pdg._COOK_REGISTRY.clear()
        self.pdg._NODE_TO_COOK.clear()

    def test_status_returns_state_counts_and_handle(self):
        hou, _ = _make_hou(
            states=[_CookState("cooking")],
            work_item_states={"cooking": 3, "cooked": 5})
        self.pdg.pdg_cook(hou, "/tasks/topnet1")
        result = self.pdg.pdg_status(hou, "/tasks/topnet1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["cook_state"], "cooking")
        self.assertFalse(result["is_terminal"])
        self.assertEqual(result["work_item_counts"], {"cooking": 3,
                                                      "cooked": 5})
        self.assertEqual(result["total_work_items"], 8)
        self.assertIsNotNone(result["handle"])

    def test_status_marks_terminal_and_closes_active(self):
        hou, _ = _make_hou(states=[_CookState("success")])
        first = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        self.assertIn("/tasks/topnet1", self.pdg._NODE_TO_COOK)
        status = self.pdg.pdg_status(
            hou, "/tasks/topnet1", cook_id=first["cook_id"])
        self.assertTrue(status["is_terminal"])
        self.assertNotIn("/tasks/topnet1", self.pdg._NODE_TO_COOK)

    def test_workitems_reads_bounded_summary(self):
        items = [_WorkItem(i, "item{0}".format(i), "cooked")
                 for i in range(5)]
        hou, _ = _make_hou(pdg_items=items)
        result = self.pdg.pdg_workitems(hou, "/tasks/topnet1", max_items=3)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["graph_generated"])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["total"], 5)
        self.assertTrue(result["truncated"])
        self.assertEqual([item["index"] for item in result["work_items"]],
                         [0, 1, 2])
        self.assertEqual(result["work_items"][0]["state"], "cooked")

    def test_workitems_graph_not_generated_returns_empty(self):
        hou, _ = _make_hou(pdg_items=None)
        result = self.pdg.pdg_workitems(hou, "/tasks/topnet1")
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["graph_generated"])
        self.assertEqual(result["work_items"], [])
        self.assertEqual(result["total"], 0)

    def test_workitems_status_filter_and_invalid_args(self):
        items = [_WorkItem(0, "a", "cooked"), _WorkItem(1, "b", "failed")]
        hou, _ = _make_hou(pdg_items=items)
        filtered = self.pdg.pdg_workitems(
            hou, "/tasks/topnet1", status_filter="failed")
        self.assertEqual([item["index"] for item in filtered["work_items"]],
                         [1])
        bad_max = self.pdg.pdg_workitems(hou, "/tasks/topnet1", max_items="x")
        self.assertEqual(bad_max["error"]["code"], "invalid_max_items")
        bad_filter = self.pdg.pdg_workitems(
            hou, "/tasks/topnet1", status_filter=123)
        self.assertEqual(bad_filter["error"]["code"], "invalid_status_filter")


class DirtyAndCancelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdg = _load_pdg()

    def setUp(self):
        self.pdg._COOK_REGISTRY.clear()
        self.pdg._NODE_TO_COOK.clear()

    def test_dirty_never_removes_outputs(self):
        hou, node = _make_hou()
        result = self.pdg.pdg_dirty(hou, "/tasks/topnet1")
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["remove_outputs"])
        self.assertEqual(node.dirty_calls, [False])

    def test_dirty_without_entry_point_is_structured(self):
        hou, _ = _make_hou()
        # 实例级遮蔽类方法，模拟节点不暴露 dirtyWorkItems。
        hou._node.dirtyWorkItems = None
        result = self.pdg.pdg_dirty(hou, "/tasks/topnet1")
        self.assertEqual(result["error"]["code"], "dirty_unavailable")

    def test_cancel_is_idempotent(self):
        hou, node = _make_hou(states=[_CookState("cooking")])
        first = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        cancel1 = self.pdg.pdg_cancel(
            hou, "/tasks/topnet1", cook_id=first["cook_id"])
        self.assertTrue(cancel1["cancelled"])
        self.assertEqual(node.cancel_calls, [True])
        # 重复 cancel：handle 已 terminal，不再调 cancelCook，返回稳定状态。
        cancel2 = self.pdg.pdg_cancel(
            hou, "/tasks/topnet1", cook_id=first["cook_id"])
        self.assertTrue(cancel2["cancelled"])
        self.assertEqual(node.cancel_calls, [True])

    def test_cancel_rejects_cook_id_from_another_node(self):
        hou, _ = _make_hou()
        first = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        other = _Hou(_TopNode("/tasks/other"))
        result = self.pdg.pdg_cancel(
            other, "/tasks/other", cook_id=first["cook_id"])
        self.assertEqual(result["error"]["code"], "unknown_cook_id")


class FailureAndCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdg = _load_pdg()

    def setUp(self):
        self.pdg._COOK_REGISTRY.clear()
        self.pdg._NODE_TO_COOK.clear()

    def test_cook_start_failure_is_structured(self):
        hou, node = _make_hou()
        node.cook_error = RuntimeError("cook boom")
        result = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "cook_start_failed")
        self.assertEqual(result["started_count"], 0)

    def test_hom_permission_error_has_hom_code(self):
        hou, node = _make_hou()
        node.cook_error = HomPermissionError("scheduler locked")
        result = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        self.assertEqual(result["error"]["code"], "hom_permission_error")

    def test_builtin_permission_error_is_not_hom_branch(self):
        hou, node = _make_hou()
        node.dirty_error = builtins.PermissionError("fs denied")
        # 注入 dirty 抛 builtins.PermissionError。
        original_dirty = node.dirtyWorkItems

        def raising(remove_outputs=False):
            raise node.dirty_error
        node.dirtyWorkItems = raising
        try:
            result = self.pdg.pdg_dirty(hou, "/tasks/topnet1")
        finally:
            node.dirtyWorkItems = original_dirty
            del node.dirty_error
        self.assertEqual(result["error"]["code"], "python_permission_error")

    def test_fallback_to_execute_graph_when_cook_work_items_absent(self):
        hou, node = _make_hou()
        # 实例级遮蔽类方法，模拟节点不暴露 cookWorkItems。
        node.cookWorkItems = None
        result = self.pdg.pdg_cook(hou, "/tasks/topnet1")
        self.assertEqual(result["status"], "started")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(node.execute_calls, [False])

    def test_all_public_functions_cap_success_and_error_paths(self):
        original = self.pdg.cmn.apply_response_cap
        calls = []

        def cap(value, max_bytes=16384):
            calls.append(value.get("status"))
            result = dict(value)
            result["_cap_test"] = True
            return result

        self.pdg.cmn.apply_response_cap = cap
        try:
            hou, node = _make_hou(states=[_CookState("cooking")],
                                  pdg_items=[_WorkItem(0, "a", "cooked")])
            results = [
                self.pdg.pdg_cook(hou, "/tasks/topnet1"),
                self.pdg.pdg_cook(hou, "/tasks/topnet1"),
                self.pdg.pdg_status(hou, "/tasks/topnet1"),
                self.pdg.pdg_workitems(hou, "/tasks/topnet1"),
                self.pdg.pdg_dirty(hou, "/tasks/topnet1"),
                self.pdg.pdg_cancel(hou, "/tasks/topnet1"),
                self.pdg.pdg_status(hou, "/missing"),
                self.pdg.pdg_cook(hou, ""),
            ]
        finally:
            self.pdg.cmn.apply_response_cap = original
        self.assertEqual(len(calls), 8)
        self.assertTrue(all(result["_cap_test"] for result in results))


PDG_5 = {"pdg_cook", "pdg_status", "pdg_workitems", "pdg_dirty", "pdg_cancel"}
PDG_RO = {"pdg_status", "pdg_workitems"}
PDG_NO_UNDO = {"pdg_cook", "pdg_dirty", "pdg_cancel"}


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
    def test_five_commands_are_pairwise_disjoint_and_exhaustive(self):
        values, handlers, _ = _class_sets_and_handlers()
        mut = values["MUTATING_COMMANDS"] & PDG_5
        ro = values["READ_ONLY_COMMANDS"] & PDG_5
        no_undo = values["NO_UNDO_COMMANDS"] & PDG_5
        self.assertEqual(mut, set())
        self.assertEqual(ro, PDG_RO)
        self.assertEqual(no_undo, PDG_NO_UNDO)
        self.assertEqual(mut | ro | no_undo, PDG_5)
        self.assertFalse(mut & ro)
        self.assertFalse(mut & no_undo)
        self.assertFalse(ro & no_undo)
        self.assertTrue(PDG_5 <= handlers)

    def test_server_handlers_apply_cap_and_never_create_inner_undo_group(self):
        source = open(SERVER_PATH, "r", encoding="utf-8").read()
        _, _, class_node = _class_sets_and_handlers()
        functions = {node.name: node for node in class_node.body
                     if isinstance(node, ast.FunctionDef)}
        for command in PDG_5:
            fn = functions["handle_" + command]
            segment = ast.get_source_segment(source, fn) or ""
            self.assertIn("cmn.apply_response_cap", segment, command)
            self.assertNotIn("undos.group", segment, command)

    def test_bridge_has_exact_five_unannotated_chinese_tools(self):
        source = open(BRIDGE_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        found = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in PDG_5:
                continue
            has_tool = any(
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
                for dec in node.decorator_list)
            if has_tool:
                found[node.name] = node
        self.assertEqual(set(found), PDG_5)
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
