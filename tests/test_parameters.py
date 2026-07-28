"""tests/test_parameters.py — add-node-parameter-vex-tools 参数/spare/链接 单测。

覆盖（tasks 2.1 - 2.4）：
- 8 个参数 helper：get_parameter / set_parameter / get_expression /
  revert_parameter / link_parameters / lock_parameter /
  create_spare_parameter / create_spare_parameters
- 链接：Parm.set(Parm) 真实引用。
- spare：单/批量 PTG 一次提交；批量先全量校验、失败零部分提交。
- 错误 schema：节点 / parm / 链接目标 / 批量 spec 失败。
- 14 个 bridge @mcp.tool() 工具 + 三分类精确穷尽互斥断言。

约束：
- stdlib unittest + 简易 hou mock；不引入新依赖。
- 不依赖真实 Houdini；H21.0 live smoke 由
  ``h21_live_node_param_vex_smoke.py`` 单独执行。
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
    """Build / reuse a synthetic package scoped to this test file."""
    pkg_name = "param_test_pkg"
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
_parameters = _ensure_module("_parameters")


# ---------------------------------------------------------------------------
# hou mock infrastructure
# ---------------------------------------------------------------------------
class _FakeParm(object):
    """Minimal hou.Parm stub。"""

    def __init__(self, name, node, default=0.0):
        self._name = name
        self._node = node
        self._value = default
        self._expression = ""
        self._locked = False
        self._set_log = []
        self._set_expr_log = []
        self._template = _FakeParmTemplate(name, default_value=default)

    def name(self):
        return self._name

    def path(self):
        return self._node.path() + "." + self._name

    def eval(self):
        return self._value

    def set(self, value):
        self._set_log.append(value)
        self._value = value

    def expression(self):
        return self._expression

    def setExpression(self, expr, language=None):
        self._set_expr_log.append((expr, language))
        self._expression = expr

    def revertToDefaults(self):
        self._value = self._template.default_value

    def lock(self, locked):
        self._locked = bool(locked)

    def isTimeDependent(self):
        return False

    def parmTemplate(self):
        return self._template


class _FakeParmTemplate(object):
    def __init__(self, name, default_value=0.0):
        self._name = name
        self.default_value = default_value
        self._type = types.SimpleNamespace(name=lambda: "float")

    def name(self):
        return self._name

    def type(self):
        return self._type


class _FakeFloatParmTemplate(object):
    def __init__(self, name, label, num_components, default_value=(),
                 min=0.0, max=1.0):
        self._kind = "float"
        self._name = name
        self._label = label
        self._num_components = num_components
        self._default = tuple(default_value)
        self._min = min
        self._max = max

    def name(self):
        return self._name

    def label(self):
        return self._label

    def numComponents(self):
        return self._num_components

    def defaultValue(self):
        return self._default


class _FakeIntParmTemplate(_FakeFloatParmTemplate):
    def __init__(self, name, label, num_components, default_value=(),
                 min=0, max=10):
        super().__init__(name, label, num_components,
                         default_value=tuple(int(v) for v in default_value),
                         min=min, max=max)
        self._kind = "int"


class _FakeStringParmTemplate(_FakeFloatParmTemplate):
    def __init__(self, name, label, num_components, default_value=()):
        super().__init__(name, label, num_components,
                         default_value=tuple(str(v) for v in default_value))
        self._kind = "string"


class _FakeToggleParmTemplate(_FakeFloatParmTemplate):
    def __init__(self, name, label, default_value=False):
        super().__init__(name, label, 1, default_value=(bool(default_value),))
        self._kind = "toggle"


class _FakeMenuParmTemplate(_FakeFloatParmTemplate):
    def __init__(self, name, label, menu_items, menu_labels,
                 default_value=0):
        super().__init__(name, label, 1, default_value=(int(default_value),))
        self._kind = "menu"
        self._menu_items = list(menu_items)
        self._menu_labels = list(menu_labels)


class _FakeParmTemplateGroup(object):
    """group.append(name, tpl) / group.appendToFolder(folder, tpl) 记录。"""

    def __init__(self):
        self.appended = []
        self.folder_appended = []

    def append(self, tpl):
        self.appended.append(tpl)

    def appendToFolder(self, folder, tpl):
        self.folder_appended.append((folder, tpl))


class _FakeNode(object):
    """Minimal graph node stub。"""

    def __init__(self, name, parent=None):
        self._name = name
        self._parent = parent
        self._path = None
        self._children = []
        self._parms = {}
        for n in ("tx", "ty", "tz", "scale", "enable"):
            self._parms[n] = _FakeParm(n, self, default=1.0)
        self._parms["enable"]._template = _FakeParmTemplate("enable", 0.0)
        self._parms["scale"]._value = 2.0
        self._ptg = _FakeParmTemplateGroup()
        self._set_position_calls = []
        self._rename_calls = []
        self._display = False
        self._render = False
        self._bypass = False
        self._template_flag = False
        self._selected = False

    def name(self):
        return self._name

    def path(self):
        if self._path is not None:
            return self._path
        if self._parent is None:
            return "/" + self._name
        return self._parent.path().rstrip("/") + "/" + self._name

    def parm(self, name):
        return self._parms.get(name)

    def parmTemplateGroup(self):
        return self._ptg

    def setParmTemplateGroup(self, group):
        self._ptg = group

    def setPosition(self, pos):
        self._set_position_calls.append(pos)

    def setName(self, name):
        self._rename_calls.append(name)
        self._name = name

    def setDisplayFlag(self, v):
        self._display = bool(v)

    def setRenderFlag(self, v):
        self._render = bool(v)

    def bypass(self, v):
        self._bypass = bool(v)

    def setTemplateFlag(self, v):
        self._template_flag = bool(v)

    def setSelected(self, v):
        self._selected = bool(v)


class _FakeHou(object):
    """Registry of named nodes + ParmTemplate factory."""

    def __init__(self):
        self._nodes = {}
        self.FloatParmTemplate = _FakeFloatParmTemplate
        self.IntParmTemplate = _FakeIntParmTemplate
        self.StringParmTemplate = _FakeStringParmTemplate
        self.ToggleParmTemplate = _FakeToggleParmTemplate
        self.MenuParmTemplate = _FakeMenuParmTemplate

    def add_node(self, path, node):
        self._nodes[path] = node
        node._path = path

    def node(self, path):
        return self._nodes.get(path)

    def getenv(self, key):
        return None


def _make_node(name):
    return _FakeNode(name)


# ===========================================================================
# Section A: get_parameter / set_parameter / get_expression / revert_parameter
# ===========================================================================
class GetParameterTests(unittest.TestCase):

    def setUp(self):
        self.hou = _FakeHou()
        self.node = _make_node("n")
        self.hou.add_node("/obj/n", self.node)

    def test_get_parameter_returns_value_type_and_expression(self):
        result = _parameters.get_parameter(self.hou, "/obj/n", "scale")
        self.assertEqual(result["value"], 2.0)
        self.assertEqual(result["parameter"], "scale")
        self.assertEqual(result["expression"], None)
        self.assertFalse(result["is_time_dependent"])

    def test_get_parameter_missing_node_raises(self):
        with self.assertRaises(ValueError):
            _parameters.get_parameter(self.hou, "/obj/missing", "tx")

    def test_get_parameter_missing_parm_raises(self):
        with self.assertRaises(ValueError):
            _parameters.get_parameter(self.hou, "/obj/n", "no_such_parm")

    def test_get_parameter_returns_expression_when_set(self):
        self.node._parms["tx"].setExpression("ch('../ty') * 2", language="hou")
        result = _parameters.get_parameter(self.hou, "/obj/n", "tx")
        self.assertEqual(result["expression"], "ch('../ty') * 2")


class SetParameterTests(unittest.TestCase):

    def setUp(self):
        self.hou = _FakeHou()
        self.node = _make_node("n")
        self.hou.add_node("/obj/n", self.node)

    def test_set_parameter_updates_value(self):
        result = _parameters.set_parameter(self.hou, "/obj/n", "scale", 9.5)
        self.assertEqual(result["old"], 2.0)
        self.assertEqual(result["new"], 9.5)
        self.assertEqual(self.node._parms["scale"]._value, 9.5)

    def test_set_parameter_missing_node_raises(self):
        with self.assertRaises(ValueError):
            _parameters.set_parameter(self.hou, "/obj/missing", "tx", 1.0)

    def test_set_parameter_missing_parm_raises(self):
        with self.assertRaises(ValueError):
            _parameters.set_parameter(self.hou, "/obj/n", "no_such", 1.0)


class GetExpressionTests(unittest.TestCase):

    def setUp(self):
        self.hou = _FakeHou()
        self.node = _make_node("n")
        self.hou.add_node("/obj/n", self.node)

    def test_get_expression_none_when_empty(self):
        result = _parameters.get_expression(self.hou, "/obj/n", "tx")
        self.assertEqual(result["expression"], None)

    def test_get_expression_returns_string(self):
        self.node._parms["tx"].setExpression("sin($F)", language="hou")
        result = _parameters.get_expression(self.hou, "/obj/n", "tx")
        self.assertEqual(result["expression"], "sin($F)")


class RevertParameterTests(unittest.TestCase):

    def setUp(self):
        self.hou = _FakeHou()
        self.node = _make_node("n")
        self.hou.add_node("/obj/n", self.node)

    def test_revert_resets_to_default(self):
        self.node._parms["scale"].set(99.0)
        result = _parameters.revert_parameter(self.hou, "/obj/n", "scale")
        # _FakeParm.revertToDefaults 取 _FakeParmTemplate.default_value
        self.assertNotEqual(result["value"], 99.0)
        self.assertEqual(result["value"], self.node._parms["scale"]._template.default_value)


# ===========================================================================
# Section B: link_parameters / lock_parameter
# ===========================================================================
class LinkParametersTests(unittest.TestCase):

    def setUp(self):
        self.hou = _FakeHou()
        a = _make_node("a")
        b = _make_node("b")
        self.hou.add_node("/obj/a", a)
        self.hou.add_node("/obj/b", b)
        self.a = a
        self.b = b

    def test_link_uses_parm_set_parm(self):
        result = _parameters.link_parameters(
            self.hou, "/obj/a.scale", "/obj/b.ty")
        self.assertEqual(self.a._parms["scale"]._set_log[-1],
                         self.b._parms["ty"])
        self.assertEqual(result["source"], "/obj/a.scale")
        self.assertEqual(result["target"], "/obj/b.ty")

    def test_link_missing_source_raises(self):
        with self.assertRaises(ValueError):
            _parameters.link_parameters(
                self.hou, "/obj/missing.scale", "/obj/b.ty")

    def test_link_missing_target_parm_raises(self):
        with self.assertRaises(ValueError):
            _parameters.link_parameters(
                self.hou, "/obj/a.scale", "/obj/b.no_such")


class LockParameterTests(unittest.TestCase):

    def setUp(self):
        self.hou = _FakeHou()
        self.node = _make_node("n")
        self.hou.add_node("/obj/n", self.node)

    def test_lock_true(self):
        result = _parameters.lock_parameter(self.hou, "/obj/n", "tx", True)
        self.assertTrue(self.node._parms["tx"]._locked)
        self.assertTrue(result["locked"])

    def test_lock_false(self):
        self.node._parms["tx"]._locked = True
        result = _parameters.lock_parameter(self.hou, "/obj/n", "tx", False)
        self.assertFalse(self.node._parms["tx"]._locked)
        self.assertFalse(result["locked"])


# ===========================================================================
# Section C: create_spare_parameter / create_spare_parameters
# ===========================================================================
class CreateSpareParameterTests(unittest.TestCase):

    def setUp(self):
        self.hou = _FakeHou()
        self.node = _make_node("n")
        self.hou.add_node("/obj/n", self.node)

    def test_create_float_spare(self):
        result = _parameters.create_spare_parameter(
            self.hou, "/obj/n", "myFloat", "float",
            label="My Float", default=(0.5,),
        )
        self.assertEqual(result["name"], "myFloat")
        self.assertEqual(result["data_type"], "float")
        self.assertEqual(len(self.node._ptg.appended), 1)
        tpl = self.node._ptg.appended[0]
        self.assertEqual(tpl._kind, "float")
        self.assertEqual(tpl._name, "myFloat")
        self.assertEqual(tpl._label, "My Float")

    def test_create_int_spare(self):
        _parameters.create_spare_parameter(
            self.hou, "/obj/n", "myInt", "int", default=(7,))
        tpl = self.node._ptg.appended[0]
        self.assertEqual(tpl._kind, "int")

    def test_create_string_spare(self):
        _parameters.create_spare_parameter(
            self.hou, "/obj/n", "myStr", "string", default=("hello",))
        tpl = self.node._ptg.appended[0]
        self.assertEqual(tpl._kind, "string")

    def test_create_toggle_spare(self):
        _parameters.create_spare_parameter(
            self.hou, "/obj/n", "myToggle", "toggle", default=True)
        tpl = self.node._ptg.appended[0]
        self.assertEqual(tpl._kind, "toggle")

    def test_create_menu_spare(self):
        _parameters.create_spare_parameter(
            self.hou, "/obj/n", "myMenu", "menu",
            menu_items=["a", "b", "c"],
            menu_labels=["AAA", "BBB", "CCC"],
            default=(0,),
        )
        tpl = self.node._ptg.appended[0]
        self.assertEqual(tpl._kind, "menu")
        self.assertEqual(tpl._menu_items, ["a", "b", "c"])
        self.assertEqual(tpl._menu_labels, ["AAA", "BBB", "CCC"])

    def test_create_spare_with_folder_uses_appendToFolder(self):
        _parameters.create_spare_parameter(
            self.hou, "/obj/n", "myFloat", "float",
            folder="MyFolder",
        )
        self.assertEqual(len(self.node._ptg.folder_appended), 1)
        folder, _tpl = self.node._ptg.folder_appended[0]
        self.assertEqual(folder, "MyFolder")

    def test_create_spare_invalid_data_type_raises(self):
        with self.assertRaises(ValueError):
            _parameters.create_spare_parameter(
                self.hou, "/obj/n", "x", "invalid")
        self.assertEqual(len(self.node._ptg.appended), 0)


class CreateSpareParametersTests(unittest.TestCase):

    def setUp(self):
        self.hou = _FakeHou()
        self.node = _make_node("n")
        self.hou.add_node("/obj/n", self.node)

    def test_batch_create_all_success(self):
        result = _parameters.create_spare_parameters(
            self.hou, "/obj/n",
            [
                {"name": "f1", "data_type": "float", "default": (0.0,)},
                {"name": "i1", "data_type": "int", "default": (1,)},
                {"name": "s1", "data_type": "string", "default": ("x",)},
            ],
        )
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(self.node._ptg.appended), 3)

    def test_batch_validation_fails_no_partial_commit(self):
        with self.assertRaises(ValueError):
            _parameters.create_spare_parameters(
                self.hou, "/obj/n",
                [
                    {"name": "ok1", "data_type": "float"},
                    {"name": "bad", "data_type": "invalid"},
                ],
            )
        self.assertEqual(len(self.node._ptg.appended), 0)

    def test_batch_duplicate_name_rejected(self):
        with self.assertRaises(ValueError):
            _parameters.create_spare_parameters(
                self.hou, "/obj/n",
                [
                    {"name": "dup", "data_type": "float"},
                    {"name": "dup", "data_type": "int"},
                ],
            )
        self.assertEqual(len(self.node._ptg.appended), 0)

    def test_batch_empty_list_rejected(self):
        with self.assertRaises(ValueError):
            _parameters.create_spare_parameters(self.hou, "/obj/n", [])

    def test_batch_with_folder_all_use_appendToFolder(self):
        _parameters.create_spare_parameters(
            self.hou, "/obj/n",
            [
                {"name": "f1", "data_type": "float"},
                {"name": "i1", "data_type": "int"},
            ],
            folder="Settings",
        )
        self.assertEqual(len(self.node._ptg.folder_appended), 2)
        for folder, _ in self.node._ptg.folder_appended:
            self.assertEqual(folder, "Settings")


# ===========================================================================
# Section D: validate_vex — safety & context 校验 in static source
# ===========================================================================
class ValidateVexSourceSafetyTests(unittest.TestCase):
    """R3: validate_vex 不得绕 Python exec / eval / compile / hos.execute_code /
    hou.hscript / hou.vexLint / hou.text.vexSyntaxCheck；不接受 caller
    compiler flags。"""

    def setUp(self):
        self.src = open(os.path.join(ROOT, "_graph_edit.py"),
                        encoding="utf-8").read()

    def _validate_vex_code_only(self):
        """提取 validate_vex 函数体（剥离 docstring），便于静态检查。"""
        tree = ast.parse(self.src)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "validate_vex":
                # ast.unparse 函数体不可直接 unparse，重新 dump
                # 简化：取源码的 def 段（docstring 之前 + 之后）
                # 这里只关心 call 树：使用 ast.walk over 整个函数节点
                # 但 docstring 是 Expr(Constant)，不应被 walk 到 Call。
                # 因此 ast.walk(node) 已经只走 code 体（docstring 当作 Expr
                # 节点不会被识别为 Call）。
                return node
        raise AssertionError("validate_vex not found")

    def test_validate_vex_no_python_exec_eval_compile(self):
        # 走 AST 节点，docstring 不命中
        target = self._validate_vex_code_only()
        for sub in ast.walk(target):
            if isinstance(sub, ast.Call):
                fname = None
                if isinstance(sub.func, ast.Name):
                    fname = sub.func.id
                elif isinstance(sub.func, ast.Attribute):
                    fname = sub.func.attr
                self.assertNotIn(
                    fname, ("exec", "eval", "compile"),
                    "validate_vex must not call {0}".format(fname))

    def test_validate_vex_no_execute_code_or_hscript(self):
        # 通过 AST Name 节点检查
        target = self._validate_vex_code_only()
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                self.assertNotIn(sub.id, ("execute_code", "hscript"),
                                 "validate_vex must not reference {0}".format(
                                     sub.id))

    def test_validate_vex_no_vexLint_or_text_vexSyntaxCheck(self):
        # 检查 AST Attribute 链：hou.vexLint / hou.text.vexSyntaxCheck
        target = self._validate_vex_code_only()
        for sub in ast.walk(target):
            if isinstance(sub, ast.Attribute):
                chain = []
                cur = sub
                while isinstance(cur, ast.Attribute):
                    chain.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    chain.append(cur.id)
                chain_str = ".".join(reversed(chain))
                self.assertNotIn("vexLint", chain_str)
                self.assertNotIn("vexSyntaxCheck", chain_str)

    def test_validate_vex_uses_subprocess_run_with_shell_false(self):
        target = self._validate_vex_code_only()
        # 必须有 subprocess.run(shell=False, timeout=10)
        run_calls = []
        for sub in ast.walk(target):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Attribute) and f.attr == "run":
                    if isinstance(f.value, ast.Name) and f.value.id == "subprocess":
                        run_calls.append(sub)
        self.assertTrue(run_calls,
                        "validate_vex must call subprocess.run")
        call = run_calls[0]
        kw = {kw.arg: kw.value for kw in call.keywords}
        self.assertIn("shell", kw)
        self.assertIsInstance(kw["shell"], ast.Constant)
        self.assertEqual(kw["shell"].value, False)
        self.assertIn("timeout", kw)
        self.assertIsInstance(kw["timeout"], ast.Constant)
        self.assertEqual(kw["timeout"].value, 10)

    def test_validate_vex_does_not_accept_compiler_flags(self):
        target = self._validate_vex_code_only()
        # 必须用 argv 列表调用 subprocess.run
        run_calls = []
        for sub in ast.walk(target):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Attribute) and f.attr == "run":
                    if isinstance(f.value, ast.Name) and f.value.id == "subprocess":
                        run_calls.append(sub)
        self.assertTrue(run_calls)
        call = run_calls[0]
        # 第一个位置参数必须是 List
        self.assertTrue(call.args)
        self.assertIsInstance(call.args[0], (ast.List, ast.Tuple))


# ===========================================================================
# Section E: server-level registry classification assertion
# ===========================================================================
class ServerClassificationTests(unittest.TestCase):
    """断言 14 个新增 server command 恰好等于三分类并集，两两无交集。"""

    def test_14_new_commands_classification(self):
        server_path = os.path.join(ROOT, "server.py")
        with open(server_path, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        mut = set()
        ro = set()
        nou = set()
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name != "HoudiniMCPServer":
                continue
            for child in node.body:
                if not isinstance(child, ast.Assign):
                    continue
                if len(child.targets) != 1:
                    continue
                tgt = child.targets[0]
                if not isinstance(tgt, ast.Name):
                    continue
                if tgt.id == "MUTATING_COMMANDS":
                    mut = _extract_frozenset_strings(child.value)
                elif tgt.id == "READ_ONLY_COMMANDS":
                    ro = _extract_frozenset_strings(child.value)
                elif tgt.id == "NO_UNDO_COMMANDS":
                    nou = _extract_frozenset_strings(child.value)

        new_cmds = {
            "rename_node", "copy_node", "move_node",
            "get_parameter", "set_parameter", "get_expression",
            "revert_parameter", "link_parameters", "lock_parameter",
            "create_spare_parameter", "create_spare_parameters",
            "get_wrangle_code", "validate_vex", "create_vex_expression",
        }

        expected_ro = {"get_parameter", "get_expression", "get_wrangle_code"}
        expected_nou = {"validate_vex"}
        expected_mut = {
            "rename_node", "copy_node", "move_node",
            "set_parameter", "revert_parameter", "link_parameters",
            "lock_parameter", "create_spare_parameter",
            "create_spare_parameters", "create_vex_expression",
        }

        new_ro = ro & new_cmds
        new_nou = nou & new_cmds
        new_mut = mut & new_cmds

        self.assertEqual(new_ro, expected_ro,
                         "READ_ONLY mismatch: {0} vs {1}".format(
                             new_ro, expected_ro))
        self.assertEqual(new_nou, expected_nou,
                         "NO_UNDO mismatch: {0} vs {1}".format(
                             new_nou, expected_nou))
        self.assertEqual(new_mut, expected_mut,
                         "MUTATING mismatch: {0} vs {1}".format(
                             new_mut, expected_mut))

        self.assertFalse(new_ro & new_nou)
        self.assertFalse(new_ro & new_mut)
        self.assertFalse(new_nou & new_mut)
        self.assertEqual(new_ro | new_nou | new_mut, new_cmds)


def _extract_frozenset_strings(node):
    """Extract string literals from a frozenset({...}) call."""
    out = set()
    if not isinstance(node, ast.Call):
        return out
    if not (isinstance(node.func, ast.Name) and node.func.id == "frozenset"):
        return out
    if not node.args:
        return out
    arg = node.args[0]
    if not isinstance(arg, ast.Set):
        return out
    for elt in arg.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            out.add(elt.value)
    return out


# ===========================================================================
# Section F: bridge tool registration & AST style (14 tools)
# ===========================================================================
class BridgeToolTests(unittest.TestCase):
    """Bridge 14 个 @mcp.tool() 存在 → 无类型注解 → 中文 docstring。"""

    EXPECTED = [
        "rename_node", "copy_node", "move_node",
        "get_parameter", "set_parameter", "get_expression",
        "revert_parameter", "link_parameters", "lock_parameter",
        "create_spare_parameter", "create_spare_parameters",
        "get_wrangle_code", "validate_vex", "create_vex_expression",
    ]

    def setUp(self):
        self.src = open(os.path.join(ROOT, "houdini_mcp_server.py"),
                        encoding="utf-8").read()
        self.tree = ast.parse(self.src)

    def _find_tool_funcs(self):
        out = {}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in self.EXPECTED:
                continue
            has_tool = False
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "tool"):
                    has_tool = True
                    break
            if has_tool:
                out[node.name] = node
        return out

    def test_all_14_tools_registered(self):
        tools = self._find_tool_funcs()
        self.assertEqual(set(tools.keys()), set(self.EXPECTED),
                         "missing: {0}; extra: {1}".format(
                             set(self.EXPECTED) - set(tools.keys()),
                             set(tools.keys()) - set(self.EXPECTED)))

    def test_no_type_annotations(self):
        tools = self._find_tool_funcs()
        for name, fn in tools.items():
            self.assertIsNone(
                fn.returns,
                "{0} must not have return annotation".format(name))
            for arg in (fn.args.posonlyargs + fn.args.args
                        + fn.args.kwonlyargs):
                self.assertIsNone(
                    arg.annotation,
                    "{0} arg {1} must not have annotation".format(
                        name, arg.arg))

    def test_chinese_docstring(self):
        tools = self._find_tool_funcs()
        for name, fn in tools.items():
            doc = ast.get_docstring(fn) or ""
            self.assertTrue(
                any("\u4e00" <= ch <= "\u9fff" for ch in doc),
                "{0} docstring must have CJK".format(name))


# ===========================================================================
# Section G: modify_node flags 扩展（D1 兼容）
# ===========================================================================
class ModifyNodeFlagsTests(unittest.TestCase):
    """modify_node 原地扩展 flags=None；旧 param/position/name 行为不变。"""

    def setUp(self):
        self.src = open(os.path.join(ROOT, "server.py"),
                        encoding="utf-8").read()

    def test_modify_node_signatures_has_flags(self):
        tree = ast.parse(self.src)
        names = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == "modify_node":
                        names.append(child)
        self.assertEqual(len(names), 1,
                         "modify_node must be defined exactly once in server.py")
        fn = names[0]
        arg_names = [a.arg for a in fn.args.args]
        self.assertIn("flags", arg_names)
        # flags 必须在所有现有参数之后（向后兼容）
        self.assertLess(arg_names.index("path"), arg_names.index("flags"))
        self.assertLess(arg_names.index("parameters"), arg_names.index("flags"))
        self.assertLess(arg_names.index("position"), arg_names.index("flags"))
        self.assertLess(arg_names.index("name"), arg_names.index("flags"))

    def test_flags_whitelist_in_parameters(self):
        pm_src = open(os.path.join(ROOT, "_parameters.py"),
                      encoding="utf-8").read()
        for flag in ("display", "render", "bypass", "selectable", "template"):
            self.assertIn(
                flag, pm_src,
                "flag {0} must be in _parameters.py whitelist".format(flag))


if __name__ == "__main__":
    unittest.main()
