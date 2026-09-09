"""Unit tests for PR 4 execute_code safety layer.

Covers:
- _common.validate_policy / _bypass_config_enabled / check_execute_code_policy / _build_audit
- _common.serialize_scene_state placeholder (mock hou)
- _common._run_code_sync 主线程同步执行：正常 / 异常 / 完整执行（无超时中断）
- server.HoudiniMCPServer.execute_code handler 主线程契约（feat-mcp-round2-hardening
  §1）：audit 字段（timed_out 恒 false / execution_mode / timeout_ignored /
  undo_group）、undo 包组与 performUndo 回滚（mock undo 栈）、timeout 参数
  保留不生效、read-only 拦截、dangerous fail-closed、capture_diff
- bridge get_last_scene_diff round-trip (mock get_houdini_connection)

Stdlib unittest, no hython required. hou is mocked via a tiny stub class.
Run with:
    python -m unittest tests.test_execute_code_safety -v
"""
import io
import json
import os
import sys
import time
import types
import unittest
import importlib.util as _ilu

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Load _common.py directly as a top-level module (matches tests/test_common.py)
_spec = _ilu.spec_from_file_location("_common", os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec)
sys.modules["_common"] = common
_spec.loader.exec_module(common)
cmn = common  # short alias


# ---------------------------------------------------------------------------
# hou stub: minimal attribute bag for serialize_scene_state placeholder.
# Uses real classes so isinstance(value, hou.EnumValue) etc. would work.
# ---------------------------------------------------------------------------
class _FakeVector(list):
    pass


class _FakeColor(list):
    pass


class _FakeEnumValue(str):
    pass


class _FakeRamp(object):
    def __init__(self, points=None):
        self.points = points or []


class _FakeNode(object):
    """A minimal hou.Node stand-in for serialize_scene_state tests."""

    def __init__(self, name, type_name="geo", children=None):
        self._name = name
        self._type = type_name
        self._children = children or []

    def name(self):
        return self._name

    def type(self):
        t = types.SimpleNamespace()
        t.name = lambda: self._type
        return t

    def children(self):
        return list(self._children)

    def path(self):
        return "/obj/" + self._name


class _FakeHou(object):
    Vector = _FakeVector
    Color = _FakeColor
    EnumValue = _FakeEnumValue
    Ramp = _FakeRamp

    def __init__(self):
        root_child = _FakeNode("geo1", children=[
            _FakeNode("grid1"),
            _FakeNode("xform1", type_name="xform"),
        ])
        self._obj = _FakeNode("obj", children=[root_child])

    def node(self, path):
        if path == "/":
            return self._obj
        if path == "/obj":
            return self._obj
        if path == "/obj/geo1":
            return self._obj.children()[0]
        return None


# ===========================================================================
# Section A: validate_policy
# ===========================================================================
class ValidatePolicyTests(unittest.TestCase):
    def test_read_only_accepted(self):
        self.assertEqual(cmn.validate_policy("read-only"), "read-only")

    def test_normal_accepted(self):
        self.assertEqual(cmn.validate_policy("normal"), "normal")

    def test_privileged_accepted(self):
        self.assertEqual(cmn.validate_policy("privileged"), "privileged")

    def test_case_insensitive(self):
        self.assertEqual(cmn.validate_policy("Normal"), "normal")
        self.assertEqual(cmn.validate_policy("PRIVILEGED"), "privileged")
        self.assertEqual(cmn.validate_policy("Read-Only"), "read-only")

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            cmn.validate_policy("super")
        with self.assertRaises(ValueError):
            cmn.validate_policy("")
        with self.assertRaises(ValueError):
            cmn.validate_policy("read-only-extreme")


# ===========================================================================
# Section B: _bypass_config_enabled
# ===========================================================================
class BypassConfigEnabledTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("HOUDINI_MCP_ALLOW_BYPASS")
        os.environ.pop("HOUDINI_MCP_ALLOW_BYPASS", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("HOUDINI_MCP_ALLOW_BYPASS", None)
        else:
            os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = self._saved

    def test_unset_returns_false(self):
        self.assertFalse(cmn._bypass_config_enabled())

    def test_zero_returns_false(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "0"
        self.assertFalse(cmn._bypass_config_enabled())

    def test_false_returns_false(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "false"
        self.assertFalse(cmn._bypass_config_enabled())

    def test_one_returns_true(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "1"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_true_returns_true(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "true"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_yes_returns_true(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "yes"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_on_returns_true(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "on"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_case_insensitive(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "TRUE"
        self.assertTrue(cmn._bypass_config_enabled())
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "Yes"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_random_string_returns_false(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "maybe"
        self.assertFalse(cmn._bypass_config_enabled())


# ===========================================================================
# Section C: check_execute_code_policy — 7 combo cases
# ===========================================================================
class CheckExecuteCodePolicyTests(unittest.TestCase):
    def _safe_code(self):
        return "x = 1 + 1\nprint(x)"

    def _mutation_code(self):
        return "hou.node('/obj/geo1').destroy()"

    def _dangerous_code(self):
        return "subprocess.run(['ls'])"

    def _heavy_code(self):
        return "geo = hou.node('/obj/geo1').geometry()"

    def _import_hou_code(self):
        return "import hou\nhou.node('/obj')"

    # ---- read-only: any mutation rejected ----
    def test_read_only_rejects_mutation(self):
        r = cmn.check_execute_code_policy(self._mutation_code(), "read-only",
                                           False, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("mutation", r["reason"])
        self.assertIn("destroy", r["hits"]["mutation"][0])

    # ---- normal + dangerous code + no allow ----
    def test_normal_rejects_dangerous_without_allow(self):
        r = cmn.check_execute_code_policy(self._dangerous_code(), "normal",
                                           False, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("dangerous", r["reason"])
        self.assertTrue(len(r["hits"]["dangerous"]) >= 1)

    # ---- normal + heavy code + no allow ----
    def test_normal_rejects_heavy_without_allow(self):
        r = cmn.check_execute_code_policy(self._heavy_code(), "normal",
                                           False, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("heavy", r["reason"])
        self.assertTrue(len(r["hits"]["heavy"]) >= 1)

    # ---- normal + safe code ----
    def test_normal_accepts_safe_code(self):
        r = cmn.check_execute_code_policy(self._safe_code(), "normal",
                                           False, False, False)
        self.assertTrue(r["allowed"], r)

    # ---- privileged + dangerous + allow + bypass OFF ----
    def test_privileged_dangerous_requires_bypass(self):
        r = cmn.check_execute_code_policy(self._dangerous_code(), "privileged",
                                           True, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("bypass", r["reason"].lower())

    # ---- privileged + dangerous + allow + bypass ON ----
    def test_privileged_dangerous_with_bypass_allowed(self):
        r = cmn.check_execute_code_policy(self._dangerous_code(), "privileged",
                                           True, False, True)
        self.assertTrue(r["allowed"], r)

    # ---- import hou detection ----
    def test_import_hou_flagged(self):
        r = cmn.check_execute_code_policy(self._import_hou_code(), "normal",
                                           False, False, False)
        # The code is safe in terms of dangerous/mutation, but import hou is
        # detected. In normal policy this should be allowed but flagged in hits.
        self.assertTrue(r["hits"]["import_hou"])
        # read-only + import hou is now ALLOWED (import itself is not a scene
        # mutation; read-only traversal code can import hou). Flag still set.
        r2 = cmn.check_execute_code_policy(self._import_hou_code(), "read-only",
                                            False, False, False)
        self.assertTrue(r2["allowed"], r2)
        self.assertTrue(r2["hits"]["import_hou"])
        # read-only still rejects actual mutations (双层防御不变)
        r3 = cmn.check_execute_code_policy(self._mutation_code(), "read-only",
                                            False, False, False)
        self.assertFalse(r3["allowed"])
        self.assertIn("mutation", r3["reason"])

    # ---- read-only 求值白名单：hou.Parm 接收者的 eval 放行 ----
    def test_read_only_parm_eval_allowed(self):
        # node.parm('scale').eval() → 接收者链含 .parm( 调用 → 白名单放行
        code = "import hou\nv = hou.node('/obj/geo1').parm('scale').eval()"
        r = cmn.check_execute_code_policy(code, "read-only",
                                          False, False, False)
        self.assertTrue(r["allowed"], r)
        self.assertNotIn("eval 动态执行", r["hits"]["dangerous"])

    def test_parm_variable_eval_allowed(self):
        # p = node.parm('s'); p.eval() → 变量绑定推导 → 白名单放行
        code = ("import hou\n"
                "p = hou.node('/obj/geo1').parm('snippet')\n"
                "print(p.evalAsString())\n"
                "print(p.rawValue())")
        r = cmn.check_execute_code_policy(code, "read-only",
                                          False, False, False)
        self.assertTrue(r["allowed"], r)
        self.assertNotIn("eval 动态执行", r["hits"]["dangerous"])

    def test_parms_loop_eval_allowed(self):
        # for p in node.parms(): p.eval() → 循环变量推导 → 白名单放行
        code = ("import hou\n"
                "for p in hou.node('/obj/geo1').parms():\n"
                "    print(p.eval())")
        r = cmn.check_execute_code_policy(code, "read-only",
                                          False, False, False)
        self.assertTrue(r["allowed"], r)
        self.assertNotIn("eval 动态执行", r["hits"]["dangerous"])

    def test_bare_eval_still_intercepted(self):
        # 裸 eval("1+1") 维持拦截；混合场景（parm.eval + 裸 eval）也拦截
        code = 'import hou\nx = eval("1 + 1")\ny = hou.node("/obj").parm("t").eval()'
        r = cmn.check_execute_code_policy(code, "read-only",
                                          False, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("eval 动态执行", r["hits"]["dangerous"])
        r2 = cmn.check_execute_code_policy('eval("1 + 1")', "normal",
                                           False, False, False)
        self.assertFalse(r2["allowed"])
        self.assertIn("eval 动态执行", r2["hits"]["dangerous"])

    def test_unprovable_receiver_conservatively_intercepted(self):
        # 接收者无法静态证明为 hou.Parm（普通变量名）→ 保守拦截
        code = "v = mystery.eval()"
        r = cmn.check_execute_code_policy(code, "normal",
                                          False, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("eval 动态执行", r["hits"]["dangerous"])


# ===========================================================================
# Section D: _build_audit
# ===========================================================================
class BuildAuditTests(unittest.TestCase):
    def test_minimal_dict_has_required_fields(self):
        audit = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=12,
            undo_group=None,
        )
        self.assertEqual(audit["policy"], "normal")
        self.assertFalse(audit["bypass_used"])
        self.assertEqual(audit["elapsed_ms"], 12)
        # feat-mcp-round2-hardening §1：timed_out 恒存在（False 时不再省略，
        # 保留字段以兼容既有消费方）
        self.assertIn("timed_out", audit)
        self.assertFalse(audit["timed_out"])
        # empty hits fields should be omitted, not None
        self.assertNotIn("dangerous_hits", audit)
        self.assertNotIn("heavy_hits", audit)
        self.assertNotIn("mutation_hits", audit)

    def test_with_hits_includes_fields(self):
        audit = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=["subprocess 启动子进程"],
            heavy_hits=[],
            mutation_hits=[".destroy() 删除节点"],
            elapsed_ms=42,
            undo_group="MCP: execute_code",
        )
        self.assertEqual(audit["dangerous_hits"], ["subprocess 启动子进程"])
        self.assertNotIn("heavy_hits", audit)
        self.assertEqual(audit["mutation_hits"], [".destroy() 删除节点"])
        self.assertEqual(audit["undo_group"], "MCP: execute_code")

    def test_exception_recorded(self):
        audit = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=5,
            undo_group=None,
            exception_type="ValueError",
            exception_message="bad code",
        )
        self.assertEqual(audit["exception_type"], "ValueError")
        self.assertEqual(audit["exception_message"], "bad code")

    def test_timed_out_recorded(self):
        audit = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=30000,
            undo_group=None,
            timed_out=True,
        )
        self.assertTrue(audit["timed_out"])

    def test_undo_group_none_omitted(self):
        audit = cmn._build_audit(
            policy="read-only",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=1,
            undo_group=None,
        )
        self.assertNotIn("undo_group", audit)

    def test_execution_mode_and_timeout_ignored_recorded(self):
        # feat-mcp-round2-hardening §1：主线程路径恒记录 execution_mode /
        # timeout_ignored；未传时省略（保持 _build_audit 通用性）
        audit = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=7,
            undo_group=None,
            execution_mode="main_thread",
            timeout_ignored=True,
        )
        self.assertEqual(audit["execution_mode"], "main_thread")
        self.assertTrue(audit["timeout_ignored"])
        minimal = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=1,
            undo_group=None,
        )
        self.assertNotIn("execution_mode", minimal)
        self.assertNotIn("timeout_ignored", minimal)


# ===========================================================================
# Section E: serialize_scene_state placeholder
# ===========================================================================
class SerializeSceneStateTests(unittest.TestCase):
    def test_returns_dict(self):
        hou = _FakeHou()
        result = cmn.serialize_scene_state(hou, root_path="/")
        self.assertIsInstance(result, dict)

    def test_returns_non_empty(self):
        hou = _FakeHou()
        result = cmn.serialize_scene_state(hou, root_path="/")
        self.assertTrue(len(result) > 0)

    def test_default_root_path(self):
        hou = _FakeHou()
        # no root_path arg → must still work
        result = cmn.serialize_scene_state(hou)
        self.assertIsInstance(result, dict)

    def test_nonexistent_path_returns_empty(self):
        hou = _FakeHou()
        result = cmn.serialize_scene_state(hou, root_path="/nope")
        # placeholder may return {} for missing path or error marker
        self.assertIsInstance(result, dict)


# ===========================================================================
# Section F: _run_code_sync — 主线程同步执行（feat-mcp-round2-hardening §1）
# ===========================================================================
class RunCodeSyncTests(unittest.TestCase):
    """_run_code_sync 取代 _run_code_thread：无 timeout / 无 daemon 线程 /
    无 timed_out 概念，代码在调用线程同步执行至自然结束。"""

    def test_normal_code_captures_stdout(self):
        ns = {"hou": _FakeHou(), "x": 0}
        result = cmn._run_code_sync("x = 1 + 2\nprint('hello', x)", ns)
        self.assertIn("stdout", result)
        self.assertIn("hello 3", result["stdout"])
        self.assertIsNone(result.get("exception_type"))
        self.assertGreaterEqual(result.get("elapsed_ms", 0), 0)
        # 同步模型：无 timed_out 字段（handler 层恒填 False）
        self.assertNotIn("timed_out", result)

    def test_exception_recorded(self):
        ns = {"hou": _FakeHou()}
        result = cmn._run_code_sync("raise ValueError('boom')", ns)
        self.assertIsNotNone(result.get("exception_type"))
        self.assertEqual(result["exception_type"], "ValueError")
        self.assertIn("boom", result.get("exception_message", ""))
        # traceback 已同步捕获进 stderr
        self.assertIn("ValueError", result.get("stderr", ""))

    def test_sleep_runs_to_completion(self):
        # 主线程模型：无超时中断——sleep 代码必然完整执行，stdout 完整，
        # elapsed 反映真实耗时（旧 worker 模型 join(timeout) 会提前返回）
        ns = {"hou": _FakeHou()}
        start = time.time()
        result = cmn._run_code_sync(
            "import time; time.sleep(0.6); print('done')", ns)
        wall = time.time() - start
        self.assertIn("done", result.get("stdout", ""))
        self.assertGreaterEqual(result.get("elapsed_ms", 0), 550)
        self.assertGreaterEqual(wall, 0.55)

    def test_runs_in_calling_thread(self):
        # 核心语义：代码在调用线程（= main thread）执行，不再经 worker
        ns = {"hou": _FakeHou()}
        result = cmn._run_code_sync(
            "import threading\n"
            "print(threading.current_thread() is threading.main_thread())",
            ns)
        self.assertIn("True", result.get("stdout", ""))

    def test_exception_in_redirected_stdout_still_recorded(self):
        ns = {"hou": _FakeHou()}
        result = cmn._run_code_sync(
            "print('before'); raise RuntimeError('nope')", ns)
        self.assertEqual(result["exception_type"], "RuntimeError")
        # stdout before raise should still be captured
        self.assertIn("before", result.get("stdout", ""))


# ===========================================================================
# Section H: server.execute_code handler — 主线程契约（feat-mcp-round2 §1）
# ===========================================================================
def _load_server_module():
    """加载真实 server.py（模式同 tests/test_batch_undo.py）。

    只有 ``_common`` / ``_render_policy`` 从真实文件加载，其余兄弟模块打
    stub（execute_code 路径只依赖 _common 与顶层 import 成功）。
    """
    package_name = "execute_code_test_houdinimcp"
    module_name = package_name + ".server"
    if module_name in sys.modules:
        return sys.modules[module_name]

    package = types.ModuleType(package_name)
    package.__path__ = [ROOT]
    sys.modules[package_name] = package

    for name in (
            "_scene", "_error_nodes", "_discovery", "_materials",
            "_hscript", "_graph_edit", "_node_info", "_geo_summary",
            "_pane_capture", "_capture_paths", "_render_b64", "_help",
            "HoudiniMCPRender"):
        sys.modules[package_name + "." + name] = types.ModuleType(
            package_name + "." + name)

    for name in ("_common", "_render_policy"):
        full_name = package_name + "." + name
        path = os.path.join(ROOT, name + ".py")
        spec = _ilu.spec_from_file_location(full_name, path)
        module = _ilu.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)

    full_name = package_name + ".server"
    spec = _ilu.spec_from_file_location(full_name, os.path.join(ROOT, "server.py"))
    module = _ilu.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeUndoGroup(object):
    def __init__(self, owner, label):
        self.owner = owner
        self.label = label

    def __enter__(self):
        self.owner.events.append(("enter", self.label))
        self.owner._open_creations = []
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.owner.events.append(("exit", self.label))
        self.owner.closed_creations.append(self.owner._open_creations)
        self.owner._open_creations = None
        return False


class _FakeUndos(object):
    """mock hou.undos：模拟 HOM 语义——只有包在 group 内的场景变更可被
    performUndo 回滚；group 外的创建不进 undo 栈。"""

    def __init__(self):
        self.events = []
        self.closed_creations = []
        self._open_creations = None

    def group(self, label):
        self.events.append(("create", label))
        return _FakeUndoGroup(self, label)

    def record_creation(self, node):
        # 不在 undo group 内的创建不进栈（worker 线程时代的失真即源于此）
        if self._open_creations is not None:
            self._open_creations.append(node)

    def performUndo(self):
        if not self.closed_creations:
            raise RuntimeError("no undo to perform")
        for node in self.closed_creations.pop():
            node.destroy()


class _FakeHouNode(object):
    def __init__(self, path, undos=None):
        self._path = path
        self._children = []
        self._destroyed = False
        self._undos = undos

    def path(self):
        return self._path

    def isDestroyed(self):
        return self._destroyed

    def destroy(self):
        self._destroyed = True

    def children(self):
        return [c for c in self._children if not c._destroyed]

    def type(self):
        t = types.SimpleNamespace()
        t.name = lambda: "geo"
        t.category = lambda: types.SimpleNamespace(name=lambda: "Object")
        return t

    def createNode(self, node_type, node_name=None):
        name = node_name or "{0}{1}".format(node_type, len(self._children) + 1)
        child = _FakeHouNode(self._path + "/" + name, undos=self._undos)
        self._children.append(child)
        if self._undos is not None:
            self._undos.record_creation(child)
        return child


class _FakeHandlerHou(object):
    """execute_code handler 用 mock hou：node() + undos，可做 undo 回滚断言。"""

    def __init__(self):
        self.undos = _FakeUndos()
        self._obj = _FakeHouNode("/obj", undos=self.undos)
        self._root = _FakeHouNode("/", undos=self.undos)
        self._root._children.append(self._obj)

    def node(self, path):
        if path == "/":
            return self._root
        if path == "/obj":
            return self._obj
        return None


class ExecuteCodeHandlerMainThreadTests(unittest.TestCase):
    """server.HoudiniMCPServer.execute_code 主线程契约单测（mock hou）。

    核心验收（spec "normal 策略可撤销" scenario 的 mock 版）：normal 策略
    建节点 → performUndo() → 节点被回滚。真实 hou 版由
    tests/h21_live_execute_code_main_thread.py 在 hython 独立端口上验证。
    """

    @classmethod
    def setUpClass(cls):
        cls.server_mod = _load_server_module()

    def setUp(self):
        self.hou = _FakeHandlerHou()
        self.server_mod.hou.node = self.hou.node
        self.server_mod.hou.undos = self.hou.undos

    def tearDown(self):
        # conftest 的 hou stub 是全局共享的：恢复原样避免跨测试泄漏
        self.server_mod.hou.node = lambda p: None
        try:
            delattr(self.server_mod.hou, "undos")
        except AttributeError:
            pass

    def _run(self, code, **kwargs):
        inst = self.server_mod.HoudiniMCPServer.__new__(
            self.server_mod.HoudiniMCPServer)
        return self.server_mod.HoudiniMCPServer.execute_code(inst, code, **kwargs)

    def _find_node(self, name):
        for child in self.hou._obj.children():
            if child.path() == "/obj/" + name:
                return child
        return None

    def test_normal_creates_node_with_undo_group_recorded(self):
        result = self._run("hou.node('/obj').createNode('geo', 'UNDO_E2E')")
        self.assertTrue(result.get("executed"), result)
        self.assertFalse(result.get("blocked", False))
        audit = result["_audit"]
        self.assertEqual(audit["undo_group"], "MCP: execute_code (normal)")
        self.assertEqual(audit["execution_mode"], "main_thread")
        self.assertTrue(audit["timeout_ignored"])
        self.assertFalse(audit["timed_out"])
        # undo group 确实开合（包住主线程执行）
        self.assertIn(("create", "MCP: execute_code (normal)"),
                      self.hou.undos.events)
        self.assertIn(("enter", "MCP: execute_code (normal)"),
                      self.hou.undos.events)
        self.assertIn(("exit", "MCP: execute_code (normal)"),
                      self.hou.undos.events)
        self.assertIsNotNone(self._find_node("UNDO_E2E"))

    def test_normal_create_then_perform_undo_rolls_back(self):
        # 核心验收：normal 建节点 → performUndo → 节点消失
        result = self._run("hou.node('/obj').createNode('geo', 'UNDO_E2E')")
        self.assertTrue(result.get("executed"), result)
        self.assertIsNotNone(self._find_node("UNDO_E2E"))
        self.hou.undos.performUndo()
        self.assertIsNone(self._find_node("UNDO_E2E"))

    def test_execution_happens_in_main_thread(self):
        result = self._run(
            "import threading\n"
            "print(threading.current_thread() is threading.main_thread())")
        self.assertTrue(result.get("executed"), result)
        self.assertIn("True", result.get("stdout", ""))

    def test_timeout_param_accepted_but_ignored(self):
        # spec "timeout 参数忽略" scenario 的单测版：sleep(1.5) > timeout=1
        # → 旧 worker 模型会在 ~1s 提前返回 timed_out=true 且 stdout 残缺；
        #   新主线程模型必须完整执行至自然结束（区分度高）
        start = time.time()
        result = self._run(
            "import time; time.sleep(1.5); print('done')",
            policy="normal", timeout=1)
        wall = time.time() - start
        self.assertTrue(result.get("executed"), result)
        self.assertIn("done", result.get("stdout", ""))
        audit = result["_audit"]
        self.assertFalse(audit["timed_out"])
        self.assertTrue(audit["timeout_ignored"])
        self.assertEqual(audit["execution_mode"], "main_thread")
        self.assertGreaterEqual(audit["elapsed_ms"], 1500)
        self.assertGreaterEqual(wall, 1.5)

    def test_read_only_blocks_mutation_and_never_wraps_undo_group(self):
        result = self._run("hou.node('/obj').createNode('geo', 'NOPE')",
                           policy="read-only")
        self.assertFalse(result.get("executed", False))
        self.assertTrue(result.get("blocked"))
        self.assertIn("mutation", result["reason"])
        audit = result["_audit"]
        self.assertNotIn("undo_group", audit)
        self.assertEqual(audit["execution_mode"], "main_thread")
        self.assertTrue(audit["timeout_ignored"])
        # 未执行 → 无 undo group 开合
        self.assertEqual(self.hou.undos.events, [])
        self.assertIsNone(self._find_node("NOPE"))

    def test_read_only_safe_code_executes_without_undo_group(self):
        result = self._run("print('read-only ok')", policy="read-only")
        self.assertTrue(result.get("executed"), result)
        audit = result["_audit"]
        self.assertNotIn("undo_group", audit)
        self.assertFalse(audit["timed_out"])
        self.assertEqual(self.hou.undos.events, [])

    def test_normal_dangerous_fail_closed(self):
        result = self._run("import subprocess\nsubprocess.run(['ls'])")
        self.assertFalse(result.get("executed", False))
        self.assertTrue(result.get("blocked"))
        self.assertIn("dangerous", result["reason"])
        self.assertEqual(self.hou.undos.events, [])

    def test_capture_diff_still_reports_scene_changes(self):
        result = self._run(
            "hou.node('/obj').createNode('geo', 'DIFF_NODE')",
            capture_diff=True)
        self.assertTrue(result.get("executed"), result)
        inst = self.server_mod.HoudiniMCPServer.__new__(
            self.server_mod.HoudiniMCPServer)
        diff = self.server_mod.HoudiniMCPServer.get_last_scene_diff(inst)
        self.assertTrue(diff.get("available"))
        self.assertTrue(diff.get("changed"))
        after_paths = [n["path"] for n in diff.get("after", {}).get("nodes", [])]
        self.assertIn("/obj/DIFF_NODE", after_paths)

    def test_invalid_policy_audit_carries_main_thread_fields(self):
        result = self._run("print('x')", policy="super")
        self.assertTrue(result.get("blocked"))
        audit = result["_audit"]
        self.assertFalse(audit["timed_out"])
        self.assertEqual(audit["execution_mode"], "main_thread")
        self.assertTrue(audit["timeout_ignored"])


# ===========================================================================
# Section G: bridge get_last_scene_diff round-trip
# ===========================================================================
class BridgeGetLastSceneDiffTests(unittest.TestCase):
    """Mock get_houdini_connection to verify send_command is called with the
    right cmd_type. We stub the mcp module before importing the bridge."""

    @classmethod
    def setUpClass(cls):
        # Stub mcp.server.fastmcp so houdini_mcp_server can be imported
        # without the real mcp package installed.
        mcp_pkg = types.ModuleType("mcp")
        server_pkg = types.ModuleType("mcp.server")
        fastmcp_mod = types.ModuleType("mcp.server.fastmcp")

        class _FakeFastMCP(object):
            # fix-mcp-test-suite-repair：注册面与真 FastMCP 对齐
            # （tool/resource/prompt），6ff79b6 引入 @mcp.resource 后旧 stub
            # 缺 resource 导致 BridgeGetLastSceneDiffTests setUpClass 即炸。
            def __init__(self, *args, **kwargs):
                self.lifespan = None
                self._tools = []
                self._resources = []
                self._prompts = []
                self._tool_manager = types.SimpleNamespace(
                    list_tools=lambda: [
                        types.SimpleNamespace(name=fn.__name__)
                        for (_, _, fn) in self._tools],
                    # 桥 import 末尾 _install_capture_hook() 会包装
                    # call_tool（幂等），stub 必须提供
                    call_tool=lambda name, arguments=None: None)

            def tool(self, *args, **kwargs):
                def deco(fn):
                    self._tools.append((args, kwargs, fn))
                    return fn
                return deco

            def resource(self, *args, **kwargs):
                def deco(fn):
                    self._resources.append((args, kwargs, fn))
                    return fn
                return deco

            def prompt(self, *args, **kwargs):
                def deco(fn):
                    self._prompts.append((args, kwargs, fn))
                    return fn
                return deco

        fastmcp_mod.FastMCP = _FakeFastMCP

        class _FakeContext(object):
            pass

        fastmcp_mod.Context = _FakeContext
        sys.modules["mcp"] = mcp_pkg
        sys.modules["mcp.server"] = server_pkg
        sys.modules["mcp.server.fastmcp"] = fastmcp_mod

        # Stub langchain imports the bridge does defensively
        for name in ("langchain_classic", "langchain_classic.output_parsers",
                     "langchain", "langchain.output_parsers"):
            mod = types.ModuleType(name)
            sys.modules[name] = mod

        # Bridge module imports HoudiniMCPRender if it's a sibling in the
        # houdinimcp package — we want to avoid that. Import as a flat file
        # and set up the module's globals minimally.
        bridge_path = os.path.join(ROOT, "houdini_mcp_server.py")
        spec = _ilu.spec_from_file_location(
            "houdini_mcp_server_under_test", bridge_path
        )
        cls.bridge = _ilu.module_from_spec(spec)
        spec.loader.exec_module(cls.bridge)

    def _make_mock_conn(self):
        """Build a mock HoudiniConnection whose send_command records calls.

        Mock returns the real server.py:604 get_last_scene_diff shape
        ({available, changed, before, after}) so the test catches
        field-name mismatches between bridge and server.
        """
        sent = []

        class MockConn(object):
            def send_command(self, cmd_type, params=None):
                sent.append((cmd_type, params))
                return {
                    "status": "success",
                    "result": {
                        "available": True,
                        "changed": True,
                        "before": {"nodes": ["obj1"]},
                        "after": {"nodes": ["obj1", "obj2"]},
                    },
                }

            def disconnect(self):
                pass

        return MockConn(), sent

    def test_get_last_scene_diff_calls_send_command(self):
        conn, sent = self._make_mock_conn()
        self.bridge.get_houdini_connection = lambda: conn
        result = self.bridge.get_last_scene_diff(None)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "get_last_scene_diff")
        self.assertEqual(sent[0][1], {})

    def test_get_last_scene_diff_returns_string(self):
        conn, _ = self._make_mock_conn()
        self.bridge.get_houdini_connection = lambda: conn
        result = self.bridge.get_last_scene_diff(None)
        self.assertIsInstance(result, str)

    def test_get_last_scene_diff_output_contains_before_and_after(self):
        """Regression: bridge must serialize real {available, changed, before, after}.

        Prior bug (C1 in PR 4 review): bridge read result.get('diff', {}) which
        server never emits, so agent always saw '{}'. With mock returning the
        real server shape, bridge output must be non-empty and contain the
        before / after keys.
        """
        conn, _ = self._make_mock_conn()
        self.bridge.get_houdini_connection = lambda: conn
        result = self.bridge.get_last_scene_diff(None)
        # Must not be the placeholder '{}' from the old implementation
        self.assertNotEqual(result.strip(), "{}")
        # Must contain the real server keys
        self.assertIn("before", result)
        self.assertIn("after", result)
        # And the stubbed scene payload
        self.assertIn("obj1", result)
        self.assertIn("obj2", result)

    def test_get_last_scene_diff_handles_status_error(self):
        class MockConn(object):
            def send_command(self, cmd_type, params=None):
                return {"status": "error", "message": "no scene yet",
                        "origin": "houdini"}

            def disconnect(self):
                pass

        self.bridge.get_houdini_connection = lambda: MockConn()
        result = self.bridge.get_last_scene_diff(None)
        self.assertIn("no scene yet", result)


if __name__ == "__main__":
    unittest.main()