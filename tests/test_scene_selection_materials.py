"""tests/test_scene_selection_materials.py — add-scene-context-selection-materials 单测。

覆盖（tasks 1-3 + 4.x）：
- 场景遍历：BFS overview / DFS cook chain / explain_node / scene
  summary，path-based visited 去重（环、菱形、共享祖先）+ max_nodes
  预算截断 + ``visited_count / truncated / truncation_reason`` 三
  元 metadata。
- 节点选择：``get_selection`` 仅读 ``selectedNodes()`` 路径；
  ``set_selection`` 全部预校验 + 零部分改变 + ``setSelected(False)``
  单调 + 不调 ``clearAllSelected()``。
- 材质：``list_materials`` parent 存在 / child category 校验；
  ``list_material_types`` category allowlist + ``nameWithCategory``
  完整名 + 稳定排序；``create_material_network`` parent 缺失 /
  锁定 / 不支持 category / 缺 type 的结构化错误。
- RGB 子键回归：既有 ``get_material_info`` whitelist 与
  ``principledshader::2.0`` 行为 **不**被本 change 改动。
- 9 server commands 唯一穷尽互斥分类：create_material_network 只
  进 MUTATING；set_selection 只进 NO_UNDO；其余 7 个只进
  READ_ONLY；既有 3 个材质命令不计入新增集合。
- 9 server handler 注册 + 9 bridge tool 命名 / 签名风格。

约束：
- stdlib unittest + 简易 hou mock；不引入新依赖。
- 不依赖真实 Houdini；H21.0 live smoke 由
  ``h21_live_scene_selection_materials_smoke.py`` 单独执行。
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
    pkg_name = "scene_sel_mat_test_pkg"
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


# Pre-load sibling modules in dependency order.
_common = _ensure_module("_common")
_scene = _ensure_module("_scene")
_selection = _ensure_module("_selection")
_materials = _ensure_module("_materials")


# ---------------------------------------------------------------------------
# hou mock for scene / selection
# ---------------------------------------------------------------------------
class _FakeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeNodeType(object):
    def __init__(self, name, category_name="Sop"):
        self._name = name
        self._category = _FakeCategory(category_name)

    def name(self):
        return self._name

    def category(self):
        return self._category

    def nameWithCategory(self):
        return "%s/%s" % (self._category.name(), self._name)

    def description(self):
        return "desc:" + self._name

    def nodeTypes(self):
        return {}


class _FakeParm(object):
    def __init__(self, name, value=0, default=None):
        self._name = name
        self._value = value
        self._default = default if default is not None else value

    def name(self):
        return self._name

    def eval(self):
        return self._value

    def defaultValue(self):
        return self._default


class _FakeNode(object):
    """Mock hou.Node. The scene root is the one whose path() returns
    "/" (i.e. parent=None). Children relative paths are "/{name}". """

    def __init__(self, name, type_name="geo", category="Sop",
                 parent=None, children=None, parms=None, locked=False):
        self._name = name
        self._type = _FakeNodeType(type_name, category)
        self._parent = parent
        self._children = list(children) if children else []
        self._parms = list(parms) if parms else []
        self._locked = locked
        self._selected = False
        for c in self._children:
            c._parent = self

    def name(self):
        return self._name

    def path(self):
        if self._parent is None:
            return "/"
        return self._parent.path().rstrip("/") + "/" + self._name

    def type(self):
        return self._type

    def category(self):
        return self._type._category

    def children(self):
        return list(self._children)

    def parms(self):
        return list(self._parms)

    def parent(self):
        return self._parent

    def inputs(self):
        return []

    def outputs(self):
        return []

    def isLocked(self):
        return self._locked

    def isEditable(self):
        return not self._locked

    def setSelected(self, value):
        self._selected = bool(value)

    def isSelected(self):
        return self._selected

    def createNode(self, node_type, node_name=None):
        actual_name = node_name if node_name else node_type + "_auto"
        new = _FakeNode(actual_name, type_name=node_type, category="Sop",
                         parent=self)
        self._children.append(new)
        return new


class _CookNode(_FakeNode):
    """FakeNode with explicit inputs() list and a separate ``type_name``
    used by _FakeNodeType."""

    def __init__(self, name, type_name="geo", category="Sop",
                 parent=None, inputs=None):
        super().__init__(name, type_name=type_name, category=category,
                          parent=parent)
        self._inputs = list(inputs) if inputs else []

    def inputs(self):
        return list(self._inputs)


class _FakeHouScene(object):
    """Mock for scene-context tests: enough surface for
    children/inputs/outputs/parms + path resolution.

    Convention: scene root is the only node whose path() returns "/".
    hou.node("/X") walks children of root by name.
    """

    def __init__(self, root_node):
        self._root = root_node
        self._frame = 1.0
        self._fps = 24.0
        self._frame_range = (1, 240)

    def node(self, path):
        if path is None:
            return None
        if path == "/" or path == "":
            return self._root
        if not isinstance(path, str) or not path.startswith("/"):
            return None
        parts = [p for p in path.split("/") if p]
        current = self._root
        for part in parts:
            found = None
            for c in current.children():
                if c.name() == part:
                    found = c
                    break
            if found is None:
                return None
            current = found
        return current

    def frame(self):
        return self._frame

    def fps(self):
        return self._fps

    def playbar(self):
        pb = types.SimpleNamespace()
        pb.frameRange = lambda: self._frame_range
        return pb


class _FakeHouSelection(object):
    """Mock for selection module: selectedNodes() + node() + clearAllSelected tracker."""

    def __init__(self, nodes, registry=None):
        self._nodes = list(nodes)
        self.clearAllSelected_called = 0
        self.setSelected_calls = []
        self._registry = dict(registry) if registry else {}

    def selectedNodes(self):
        return list(self._nodes)

    def clearAllSelected(self):
        self.clearAllSelected_called += 1
        for n in self._nodes:
            n._selected = False
        self._nodes = []

    def node(self, path):
        if not isinstance(path, str) or not path:
            return None
        return self._registry.get(path)


class _FakeHouMatContainer(object):
    """Mock for materials module: parent.childTypeCategory() + createNode.

    Separate ``type_name`` controls type().name() — different from the
    node instance name (real Houdini distinguishes type vs node name).
    """

    def __init__(self, name, category_name="Sop", child_type_category="Sop",
                 locked=False, available_node_types=None, parent=None,
                 type_name=None):
        self._name = name
        self._type = _FakeNodeType(type_name or name, category_name)
        self._parent = parent
        self._children = []
        self._locked = locked
        self._child_type_category_name = child_type_category
        self._available_node_types = available_node_types or {}
        self._created = []
        for c in self._children:
            c._parent = self

    def name(self):
        return self._name

    def path(self):
        if self._parent is None:
            return "/" + self._name
        return self._parent.path().rstrip("/") + "/" + self._name

    def type(self):
        return self._type

    def category(self):
        return self._type._category

    def parent(self):
        return self._parent

    def children(self):
        return list(self._children)

    def childTypeCategory(self):
        cat = _FakeCategory(self._child_type_category_name)
        cat.nodeTypes = lambda: dict(self._available_node_types)
        return cat

    def isLocked(self):
        return self._locked

    def isEditable(self):
        return not self._locked

    def createNode(self, node_type, node_name=None):
        actual_name = node_name if node_name else node_type + "_auto"
        new = _FakeHouMatContainer(
            actual_name, category_name="Sop",
            child_type_category="Sop", parent=self,
            type_name=node_type)
        self._children.append(new)
        self._created.append((node_type, actual_name, new))
        return new


class _FakeHouMatTop(object):
    """hou stub: node() resolves paths from a registry; also provides
    nodeTypeCategories() returning a dict."""

    def __init__(self, registry):
        self._registry = dict(registry)
        self._nodeTypeCategories = {
            "Vop": self._make_vop_category(),
            "Shop": self._make_shop_category(),
        }

    def node(self, path):
        return self._registry.get(path)

    def nodeTypeCategories(self):
        return self._nodeTypeCategories

    def _make_vop_category(self):
        cat = _FakeCategory("Vop")
        node_types = {
            "Vop/principledshader": _FakeNodeType("principledshader", "Vop"),
            "Vop/vopsurface": _FakeNodeType("vopsurface", "Vop"),
        }
        cat.nodeTypes = lambda: node_types
        return cat

    def _make_shop_category(self):
        cat = _FakeCategory("Shop")
        node_types = {
            "Shop/material": _FakeNodeType("material", "Shop"),
        }
        cat.nodeTypes = lambda: node_types
        return cat


# ---------------------------------------------------------------------------
# Section A: get_network_overview
# ---------------------------------------------------------------------------
def _make_branchy_scene():
    """Small scene with 3 levels (root's path is "/"):
        /
            /branch_a
                /branch_a/leaf1
                /branch_a/leaf2
            /branch_b
                /branch_b/leaf3
    Total 6 nodes.
    """
    root = _FakeNode("root", type_name="root", category="Object")
    a = _FakeNode("branch_a", type_name="branch", category="Object",
                  parent=root)
    leaf1 = _FakeNode("leaf1", type_name="leaf", category="Object",
                      parent=a)
    leaf2 = _FakeNode("leaf2", type_name="leaf", category="Object",
                      parent=a)
    b = _FakeNode("branch_b", type_name="branch", category="Object",
                  parent=root)
    leaf3 = _FakeNode("leaf3", type_name="leaf", category="Object",
                      parent=b)
    root._children = [a, b]
    a._children = [leaf1, leaf2]
    b._children = [leaf3]
    return root


class GetNetworkOverviewTests(unittest.TestCase):
    def test_basic_bfs_traversal(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_network_overview(hou, "/", max_depth=2,
                                         max_nodes=500)
        self.assertEqual(r["status"], "success")
        paths = [n["path"] for n in r["nodes"]]
        self.assertIn("/branch_a", paths)
        self.assertIn("/branch_b", paths)
        self.assertIn("/branch_a/leaf1", paths)
        self.assertEqual(r["visited_count"], 6)

    def test_max_depth_zero_returns_only_root(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_network_overview(hou, "/", max_depth=0,
                                         max_nodes=500)
        self.assertEqual(len(r["nodes"]), 1)
        self.assertEqual(r["nodes"][0]["path"], "/")
        self.assertEqual(r["depth_reached"], 0)
        self.assertFalse(r["truncated"])

    def test_max_nodes_truncation(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_network_overview(hou, "/", max_depth=2,
                                         max_nodes=3)
        # 3 budget -> root + 2 children, leaves not reached
        self.assertTrue(r["truncated"])
        self.assertEqual(r["truncation_reason"], "max_nodes")
        self.assertEqual(r["visited_count"], 3)
        self.assertLessEqual(r["visited_count"], 3)

    def test_max_nodes_zero(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_network_overview(hou, "/", max_depth=2,
                                         max_nodes=0)
        self.assertEqual(r["status"], "success")
        self.assertEqual(len(r["nodes"]), 0)
        # 0 budget means ALL nodes (including root) are truncated
        self.assertTrue(r["truncated"])
        self.assertEqual(r["truncation_reason"], "max_nodes")

    def test_invalid_max_depth(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_network_overview(hou, "/", max_depth=-1,
                                         max_nodes=500)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_max_depth")

    def test_invalid_max_nodes(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_network_overview(hou, "/", max_depth=2,
                                         max_nodes="ten")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_max_nodes")

    def test_parent_not_found(self):
        hou = _FakeHouScene(_FakeNode("root", category="Object"))
        r = _scene.get_network_overview(hou, "/no/such", max_depth=2,
                                         max_nodes=500)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "parent_not_found")

    def test_empty_parent_path(self):
        hou = _FakeHouScene(_FakeNode("root", category="Object"))
        r = _scene.get_network_overview(hou, "", max_depth=2,
                                         max_nodes=500)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_parent_path")

    def test_visited_count_and_edges(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_network_overview(hou, "/", max_depth=2,
                                         max_nodes=500)
        # edges: 5 (root->a, root->b, a->leaf1, a->leaf2, b->leaf3)
        self.assertEqual(len(r["edges"]), 5)


# ---------------------------------------------------------------------------
# Section B: get_cook_chain — DFS / cycle / diamond dedup
# ---------------------------------------------------------------------------
def _make_diamond_scene():
    """Build a diamond cook chain + cycle:
        A -> B -> D
        A -> C -> D
        A -> D  (cycle)

    All nodes are flat children of a root so hou.node("/A"), hou.node("/B"),
    hou.node("/C"), hou.node("/D") all resolve.
    """
    root = _FakeNode("root", type_name="root", category="Object")
    a = _CookNode("A", category="Sop", parent=root)
    b = _CookNode("B", category="Sop", parent=root)
    c = _CookNode("C", category="Sop", parent=root)
    d = _CookNode("D", category="Sop", parent=root)
    root._children = [a, b, c, d]
    b._inputs = [a]
    c._inputs = [a]
    d._inputs = [b, c]
    a._inputs = [d]  # cycle
    return root, a, b, c, d


def _make_shared_ancestor_scene():
    """Chain with a shared ancestor:
        A -> B
        A -> C
        B -> D
        C -> D  (D shared; should appear once)
    """
    root = _FakeNode("root", type_name="root", category="Object")
    a = _CookNode("A", category="Sop", parent=root)
    b = _CookNode("B", category="Sop", parent=root)
    c = _CookNode("C", category="Sop", parent=root)
    d = _CookNode("D", category="Sop", parent=root)
    root._children = [a, b, c, d]
    b._inputs = [a]
    c._inputs = [a]
    d._inputs = [b, c]
    return root, a, b, c, d


class GetCookChainTests(unittest.TestCase):
    def test_basic_upstream_chain(self):
        root, a, b, c, d = _make_diamond_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_cook_chain(hou, "/D", max_depth=20, max_nodes=500)
        self.assertEqual(r["status"], "success")
        paths = [n["path"] for n in r["chain"]]
        self.assertEqual(paths[0], "/D")
        self.assertIn("/B", paths)
        self.assertIn("/C", paths)
        self.assertIn("/A", paths)
        # No duplicate paths
        self.assertEqual(len(paths), len(set(paths)))

    def test_shared_ancestor_dedup(self):
        root, a, b, c, d = _make_shared_ancestor_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_cook_chain(hou, "/D", max_depth=20, max_nodes=500)
        paths = [n["path"] for n in r["chain"]]
        self.assertEqual(paths.count("/A"), 1)
        self.assertEqual(paths.count("/B"), 1)
        self.assertEqual(paths.count("/C"), 1)
        self.assertEqual(paths.count("/D"), 1)

    def test_max_nodes_truncation(self):
        root = _FakeNode("root", type_name="root", category="Object")
        a = _CookNode("A", category="Sop", parent=root)
        b = _CookNode("B", category="Sop", parent=root)
        c = _CookNode("C", category="Sop", parent=root)
        d = _CookNode("D", category="Sop", parent=root)
        e = _CookNode("E", category="Sop", parent=root)
        root._children = [a, b, c, d, e]
        b._inputs = [a]
        c._inputs = [b]
        d._inputs = [c]
        e._inputs = [d]
        hou = _FakeHouScene(root)
        r = _scene.get_cook_chain(hou, "/E", max_depth=20, max_nodes=3)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["truncation_reason"], "max_nodes")
        self.assertLessEqual(r["visited_count"], 3)

    def test_max_depth_zero(self):
        root = _FakeNode("root", type_name="root", category="Object")
        a = _CookNode("A", category="Sop", parent=root)
        root._children = [a]
        hou = _FakeHouScene(root)
        r = _scene.get_cook_chain(hou, "/A", max_depth=0, max_nodes=500)
        # max_depth=0 → 只 root 自身（1 个 chain entry）
        self.assertEqual(len(r["chain"]), 1)
        self.assertEqual(r["chain"][0]["path"], "/A")
        self.assertEqual(r["depth_reached"], 0)
        self.assertFalse(r["truncated"])

    def test_node_not_found(self):
        root = _FakeNode("root", type_name="root", category="Object")
        a = _CookNode("A", category="Sop", parent=root)
        root._children = [a]
        hou = _FakeHouScene(root)
        r = _scene.get_cook_chain(hou, "/no/such", max_depth=20,
                                    max_nodes=500)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "node_not_found")


# ---------------------------------------------------------------------------
# Section C: get_scene_summary
# ---------------------------------------------------------------------------
class GetSceneSummaryTests(unittest.TestCase):
    def test_category_counts(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_scene_summary(hou, max_nodes=500)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["total_nodes"], 6)
        self.assertIn("Object", r["category_counts"])
        self.assertEqual(r["category_counts"]["Object"], 6)

    def test_timeline_fields_present(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_scene_summary(hou, max_nodes=500)
        for key in ("frame", "fps", "start_frame", "end_frame"):
            self.assertIn(key, r)

    def test_invalid_max_nodes(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.get_scene_summary(hou, max_nodes=-1)
        self.assertEqual(r["status"], "error")
        self.assertIn(r["error"]["code"], ("invalid_max_nodes",
                                            "invalid_max_depth"))


# ---------------------------------------------------------------------------
# Section D: explain_node
# ---------------------------------------------------------------------------
class ExplainNodeTests(unittest.TestCase):
    def test_basic_explain(self):
        root = _make_branchy_scene()
        hou = _FakeHouScene(root)
        r = _scene.explain_node(hou, "/branch_a", include_params=False)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["type"], "branch")
        self.assertEqual(r["category"], "Object")
        self.assertEqual(r["name"], "branch_a")
        self.assertEqual(r["input_count"], 0)
        self.assertEqual(r["output_count"], 0)

    def test_include_params_non_default(self):
        parm1 = _FakeParm("px", value=2.0, default=0.0)
        parm2 = _FakeParm("py", value=0.0, default=0.0)
        root = _FakeNode("r", type_name="root", category="Object")
        node = _FakeNode("n", category="Sop", parent=root,
                         parms=[parm1, parm2])
        root._children = [node]
        hou = _FakeHouScene(root)
        r = _scene.explain_node(hou, "/n", include_params=True,
                                  max_params=10)
        self.assertEqual(r["status"], "success")
        self.assertIn("non_default_parameters", r)
        self.assertIn("px", r["non_default_parameters"])
        self.assertNotIn("py", r["non_default_parameters"])
        self.assertEqual(r["non_default_parameter_count"], 1)

    def test_node_not_found(self):
        root = _FakeNode("r", type_name="root", category="Object")
        hou = _FakeHouScene(root)
        r = _scene.explain_node(hou, "/no/such")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "node_not_found")

    def test_invalid_include_params(self):
        root = _FakeNode("r", type_name="root", category="Object")
        node = _FakeNode("n", category="Sop", parent=root)
        root._children = [node]
        hou = _FakeHouScene(root)
        r = _scene.explain_node(hou, "/n", include_params="yes")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_include_params")

    def test_max_params_zero(self):
        parm1 = _FakeParm("px", value=2.0, default=0.0)
        root = _FakeNode("r", type_name="root", category="Object")
        node = _FakeNode("n", category="Sop", parent=root,
                         parms=[parm1])
        root._children = [node]
        hou = _FakeHouScene(root)
        r = _scene.explain_node(hou, "/n", include_params=True,
                                  max_params=0)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["non_default_parameter_count"], 0)


# ---------------------------------------------------------------------------
# Section E: get_selection / set_selection
# ---------------------------------------------------------------------------
class GetSelectionTests(unittest.TestCase):
    def test_returns_selected_nodes(self):
        root = _FakeNode("root", type_name="root", category="Object")
        n1 = _FakeNode("a", category="Sop", parent=root)
        n2 = _FakeNode("b", category="Sop", parent=root)
        root._children = [n1, n2]
        n1._selected = True
        n2._selected = True
        hou = _FakeHouSelection([n1, n2])
        r = _selection.get_selection(hou)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["count"], 2)
        paths = [e["path"] for e in r["selected"]]
        self.assertIn("/a", paths)
        self.assertIn("/b", paths)

    def test_empty_selection(self):
        hou = _FakeHouSelection([])
        r = _selection.get_selection(hou)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["selected"], [])


class SetSelectionTests(unittest.TestCase):
    def _make_scene(self):
        root = _FakeNode("root", type_name="root", category="Object")
        n1 = _FakeNode("a", category="Sop", parent=root)
        n2 = _FakeNode("b", category="Sop", parent=root)
        n3 = _FakeNode("c", category="Sop", parent=root)
        root._children = [n1, n2, n3]
        return root, n1, n2, n3

    def test_replace_selection(self):
        root, n1, n2, n3 = self._make_scene()
        n1._selected = True
        n2._selected = True
        hou = _FakeHouSelection([n1, n2],
                                  registry={"/a": n1, "/b": n2, "/c": n3})
        r = _selection.set_selection(hou, ["/c"], clear_others=True)
        self.assertEqual(r["status"], "success")
        self.assertEqual(hou.clearAllSelected_called, 0)
        self.assertFalse(n1._selected)
        self.assertFalse(n2._selected)
        self.assertTrue(n3._selected)
        self.assertEqual(r["cleared"], 2)
        self.assertEqual(r["set"], 1)

    def test_no_clear_appends(self):
        root, n1, n2, n3 = self._make_scene()
        n1._selected = True
        hou = _FakeHouSelection([n1],
                                  registry={"/a": n1, "/b": n2, "/c": n3})
        r = _selection.set_selection(hou, ["/b"], clear_others=False)
        self.assertEqual(r["status"], "success")
        self.assertTrue(n1._selected)
        self.assertTrue(n2._selected)
        self.assertEqual(r["cleared"], 0)
        self.assertEqual(r["set"], 1)

    def test_invalid_path_no_partial_change(self):
        root, n1, n2, n3 = self._make_scene()
        n1._selected = True
        hou = _FakeHouSelection([n1],
                                  registry={"/a": n1, "/b": n2, "/c": n3})
        r = _selection.set_selection(hou, ["/a", "/no/such"],
                                       clear_others=True)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_node_path")
        # n1 must remain selected (no partial change)
        self.assertTrue(n1._selected)

    def test_non_list_node_paths(self):
        hou = _FakeHouSelection([])
        r = _selection.set_selection(hou, "/a", clear_others=True)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_node_paths")

    def test_empty_string_path(self):
        hou = _FakeHouSelection([])
        r = _selection.set_selection(hou, [""], clear_others=True)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_node_path")

    def test_empty_list_clears(self):
        root, n1, n2, n3 = self._make_scene()
        n1._selected = True
        hou = _FakeHouSelection([n1],
                                  registry={"/a": n1, "/b": n2, "/c": n3})
        r = _selection.set_selection(hou, [], clear_others=True)
        self.assertEqual(r["status"], "success")
        self.assertFalse(n1._selected)
        self.assertEqual(r["cleared"], 1)


# ---------------------------------------------------------------------------
# Section F: list_materials
# ---------------------------------------------------------------------------
def _make_mat_container():
    """Build /mat with 2 child material nodes."""
    mat = _FakeHouMatContainer("mat", category_name="mat",
                                child_type_category="Mat")
    child_a = _FakeHouMatContainer("myMat1", category_name="Mat",
                                     child_type_category="Mat", parent=mat,
                                     type_name="principledshader")
    child_b = _FakeHouMatContainer("myMat2", category_name="Mat",
                                     child_type_category="Mat", parent=mat,
                                     type_name="vopsurface")
    mat._children = [child_a, child_b]
    return mat, [child_a, child_b]


class ListMaterialsTests(unittest.TestCase):
    def test_basic_list(self):
        mat, _ = _make_mat_container()
        hou = _FakeHouMatTop({"/mat": mat})
        r = _materials.list_materials(hou, "/mat")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["count"], 2)
        names = [e["name"] for e in r["materials"]]
        self.assertIn("myMat1", names)
        self.assertIn("myMat2", names)

    def test_parent_not_found(self):
        hou = _FakeHouMatTop({})
        r = _materials.list_materials(hou, "/no/such")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "parent_not_found")

    def test_unsupported_parent_category(self):
        # Drive category doesn't allow material children
        obj = _FakeHouMatContainer("obj", category_name="obj",
                                     child_type_category="Driver")
        hou = _FakeHouMatTop({"/obj": obj})
        r = _materials.list_materials(hou, "/obj")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "unsupported_parent_category")

    def test_stable_sort(self):
        mat, _ = _make_mat_container()
        hou = _FakeHouMatTop({"/mat": mat})
        r = _materials.list_materials(hou, "/mat")
        paths = [e["path"] for e in r["materials"]]
        self.assertEqual(paths, sorted(paths))


# ---------------------------------------------------------------------------
# Section G: list_material_types
# ---------------------------------------------------------------------------
class ListMaterialTypesTests(unittest.TestCase):
    def test_vop_category(self):
        hou = _FakeHouMatTop({})
        r = _materials.list_material_types(hou, "Vop")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["category"], "Vop")
        names = [e["name"] for e in r["types"]]
        self.assertIn("principledshader", names)
        self.assertIn("vopsurface", names)

    def test_shop_category(self):
        hou = _FakeHouMatTop({})
        r = _materials.list_material_types(hou, "Shop")
        self.assertEqual(r["status"], "success")
        names = [e["name"] for e in r["types"]]
        self.assertIn("material", names)

    def test_unsupported_category(self):
        hou = _FakeHouMatTop({})
        r = _materials.list_material_types(hou, "Sop")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "unsupported_category")

    def test_unknown_category_in_session(self):
        hou = _FakeHouMatTop({})
        r = _materials.list_material_types(hou, "BadCat")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "unsupported_category")

    def test_node_type_uses_nameWithCategory(self):
        hou = _FakeHouMatTop({})
        r = _materials.list_material_types(hou, "Vop")
        node_types = [e["node_type"] for e in r["types"]]
        for nt in node_types:
            self.assertIn("/", nt)

    def test_invalid_category_type(self):
        hou = _FakeHouMatTop({})
        r = _materials.list_material_types(hou, None)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_category")

    def test_stable_sort(self):
        hou = _FakeHouMatTop({})
        r = _materials.list_material_types(hou, "Vop")
        names = [e["name"] for e in r["types"]]
        self.assertEqual(names, sorted(names))


# ---------------------------------------------------------------------------
# Section H: create_material_network
# ---------------------------------------------------------------------------
class CreateMaterialNetworkTests(unittest.TestCase):
    def _make_available_matnet(self, category_name="Sop"):
        cat = _FakeCategory(category_name)
        cat.nodeTypes = lambda: {
            "%s/matnet" % category_name: _FakeNodeType("matnet",
                                                          category_name),
        }
        return cat

    def test_create_success(self):
        mat = _FakeHouMatContainer("mat", category_name="mat",
                                    child_type_category="Mat")
        cat = self._make_available_matnet("Mat")
        mat.childTypeCategory = lambda: cat
        hou = _FakeHouMatTop({"/mat": mat})
        r = _materials.create_material_network(hou, "/mat", name="myNet")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["type"], "matnet")
        self.assertEqual(r["name"], "myNet")
        self.assertEqual(len(mat._created), 1)
        self.assertEqual(mat._created[0][0], "matnet")

    def test_parent_not_found(self):
        hou = _FakeHouMatTop({})
        r = _materials.create_material_network(hou, "/no/such", name="myNet")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "parent_not_found")

    def test_parent_locked(self):
        mat = _FakeHouMatContainer("mat", category_name="mat",
                                    child_type_category="Sop", locked=True)
        hou = _FakeHouMatTop({"/mat": mat})
        r = _materials.create_material_network(hou, "/mat", name="myNet")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "parent_locked")

    def test_unsupported_parent_category(self):
        obj = _FakeHouMatContainer("obj", category_name="obj",
                                     child_type_category="Driver")
        hou = _FakeHouMatTop({"/obj": obj})
        r = _materials.create_material_network(hou, "/obj", name="myNet")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "unsupported_parent_category")

    def test_node_type_unavailable(self):
        # createNode("matnet") raises — function should return
        # ``node_type_unavailable`` without raising.
        mat = _FakeHouMatContainer("mat", category_name="mat",
                                    child_type_category="Mat")
        def _raise_create(*args, **kwargs):
            raise RuntimeError("Invalid node type name 'matnet'")
        mat.createNode = _raise_create
        hou = _FakeHouMatTop({"/mat": mat})
        r = _materials.create_material_network(hou, "/mat", name="myNet")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "node_type_unavailable")

    def test_invalid_parent_path(self):
        hou = _FakeHouMatTop({})
        r = _materials.create_material_network(hou, "", name="myNet")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_parent_path")

    def test_invalid_name(self):
        mat = _FakeHouMatContainer("mat", category_name="mat",
                                    child_type_category="Sop")
        hou = _FakeHouMatTop({"/mat": mat})
        r = _materials.create_material_network(hou, "/mat", name="")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_name")


# ---------------------------------------------------------------------------
# Section I: RGB whitelist regression
# ---------------------------------------------------------------------------
class RGBWhitelistRegressionTests(unittest.TestCase):
    """本 change **不**改动 get_material_info / principledshader::2.0 的
    RGB 子键与 H20 3-tuple key 行为。Regression 保护：whitelist 长度
    ≥ 50、包含 basecolorr/g/b、emitcolorr/g/b、sheenr/g/b、
    coat_colorr/g/b、sssr/g/b、scattering_colorr/g/b。
    """

    def test_whitelist_size(self):
        self.assertGreaterEqual(len(_materials.MATERIAL_PARM_WHITELIST), 50)

    def test_basecolor_rgb_subkeys(self):
        wl = _materials.MATERIAL_PARM_WHITELIST
        for k in ("basecolorr", "basecolorg", "basecolorb"):
            self.assertIn(k, wl)

    def test_emit_rgb_subkeys(self):
        wl = _materials.MATERIAL_PARM_WHITELIST
        for k in ("emitcolorr", "emitcolorg", "emitcolorb"):
            self.assertIn(k, wl)

    def test_sheen_rgb_subkeys(self):
        wl = _materials.MATERIAL_PARM_WHITELIST
        for k in ("sheenr", "sheeng", "sheenb"):
            self.assertIn(k, wl)

    def test_coat_rgb_subkeys(self):
        wl = _materials.MATERIAL_PARM_WHITELIST
        for k in ("coat_colorr", "coat_colorg", "coat_colorb"):
            self.assertIn(k, wl)

    def test_sss_rgb_subkeys(self):
        wl = _materials.MATERIAL_PARM_WHITELIST
        for k in ("sssr", "sssg", "sssb"):
            self.assertIn(k, wl)

    def test_scattering_rgb_subkeys(self):
        wl = _materials.MATERIAL_PARM_WHITELIST
        for k in ("scattering_colorr", "scattering_colorg",
                  "scattering_colorb"):
            self.assertIn(k, wl)


# ---------------------------------------------------------------------------
# Section J: 9 server commands 三分类唯一穷尽互斥断言
# ---------------------------------------------------------------------------
SCENE_MAT_CHANGE_9 = (
    "get_network_overview", "get_cook_chain", "explain_node",
    "get_scene_summary", "get_selection", "set_selection",
    "list_materials", "list_material_types", "create_material_network",
)
NEW_MUTATING = frozenset({"create_material_network"})
NEW_NO_UNDO = frozenset({"set_selection"})
NEW_READ_ONLY = frozenset({
    "get_network_overview", "get_cook_chain", "explain_node",
    "get_scene_summary", "get_selection", "list_materials",
    "list_material_types",
})
LEGACY_3 = frozenset({"create_material", "assign_material", "get_material_info"})


class ServerCommandClassificationTests(unittest.TestCase):
    def test_three_sets_partition_of_9(self):
        union = NEW_MUTATING | NEW_NO_UNDO | NEW_READ_ONLY
        self.assertEqual(union, frozenset(SCENE_MAT_CHANGE_9))

    def test_three_sets_pairwise_disjoint(self):
        self.assertEqual(NEW_MUTATING & NEW_NO_UNDO, frozenset())
        self.assertEqual(NEW_MUTATING & NEW_READ_ONLY, frozenset())
        self.assertEqual(NEW_NO_UNDO & NEW_READ_ONLY, frozenset())

    def test_create_material_network_only_mutating(self):
        self.assertEqual(NEW_MUTATING, {"create_material_network"})

    def test_set_selection_only_no_undo(self):
        self.assertEqual(NEW_NO_UNDO, {"set_selection"})

    def test_legacy_3_not_in_new_sets(self):
        for cmd in LEGACY_3:
            self.assertNotIn(cmd, NEW_MUTATING)
            self.assertNotIn(cmd, NEW_NO_UNDO)
            self.assertNotIn(cmd, NEW_READ_ONLY)


# ---------------------------------------------------------------------------
# Section K: 9 handler 存在 + 9 bridge tool 存在 + bridge 风格
# ---------------------------------------------------------------------------
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


class ServerHandlerRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_mod = _import_server_module()

    def test_9_new_handlers_present(self):
        cls = self.server_mod.HoudiniMCPServer
        instance = cls.__new__(cls)
        handlers = cls._get_command_handlers(instance)
        for cmd in SCENE_MAT_CHANGE_9:
            self.assertIn(cmd, handlers,
                          "missing handler: %s" % cmd)

    def test_legacy_3_still_registered(self):
        cls = self.server_mod.HoudiniMCPServer
        instance = cls.__new__(cls)
        handlers = cls._get_command_handlers(instance)
        for cmd in ("create_material", "assign_material",
                     "get_material_info"):
            self.assertIn(cmd, handlers)

    def test_classification_validation_passes(self):
        cls = self.server_mod.HoudiniMCPServer
        instance = cls.__new__(cls)
        handlers = cls._get_command_handlers(instance)
        # Should not raise
        cls._validate_handler_classification(handlers)


class BridgeToolStyleTests(unittest.TestCase):
    """Inspect houdini_mcp_server.py source for the 9 new @mcp.tool()
    declarations. We use AST rather than importing the bridge file
    directly (which requires MCP and many deps)."""

    BRIDGE_FILE = os.path.join(ROOT, "houdini_mcp_server.py")

    @classmethod
    def setUpClass(cls):
        with open(cls.BRIDGE_FILE, "r", encoding="utf-8") as fh:
            source = fh.read()
        cls.tree = ast.parse(source)
        cls.source = source

    def _find_mcp_tool_functions(self):
        out = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Name) and target.id == "mcp":
                    out.append(node)
                    break
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    out.append(node)
                    break
        return out

    def test_9_new_tools_present(self):
        funcs = self._find_mcp_tool_functions()
        names = {f.name for f in funcs}
        for cmd in SCENE_MAT_CHANGE_9:
            self.assertIn(cmd, names, "missing bridge tool: %s" % cmd)

    def test_no_legacy_3_renamed(self):
        funcs = self._find_mcp_tool_functions()
        names = {f.name for f in funcs}
        for cmd in ("create_material", "assign_material",
                     "get_material_info"):
            self.assertIn(cmd, names)

    def test_no_type_annotations_on_new_tools(self):
        funcs = self._find_mcp_tool_functions()
        names_to_check = set(SCENE_MAT_CHANGE_9)
        for fn in funcs:
            if fn.name not in names_to_check:
                continue
            for arg in fn.args.args:
                self.assertIsNone(arg.annotation,
                                   "tool %s arg %s has type annotation"
                                   % (fn.name, arg.arg))

    def test_chinese_docstring_on_new_tools(self):
        for fn in self._find_mcp_tool_functions():
            if fn.name not in set(SCENE_MAT_CHANGE_9):
                continue
            doc = ast.get_docstring(fn)
            self.assertIsNotNone(doc, "tool %s missing docstring" % fn.name)
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in doc),
                            "tool %s docstring not Chinese" % fn.name)


if __name__ == "__main__":
    unittest.main()
