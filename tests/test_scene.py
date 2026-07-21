"""Unit tests for external/houdinimcp/_scene.py and PR 5 _common additions.

Stdlib unittest, no hython required. hou is mocked via a tiny stub class.
Run with:
    python -m unittest tests.test_scene tests.test_execute_code_safety tests.test_common -v
"""
import os
import sys
import unittest
import importlib.util as _ilu

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Load _common.py and _scene.py under a synthetic "houdinimcp" package so the
# production-style "from . import _common as cmn" inside _scene.py resolves
# without needing hython / the real package __init__.py.
import types
pkg = types.ModuleType("houdinimcp")
pkg.__path__ = [ROOT]
sys.modules["houdinimcp"] = pkg

_spec_common = _ilu.spec_from_file_location("houdinimcp._common",
                                            os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec_common)
sys.modules["houdinimcp._common"] = common
_spec_common.loader.exec_module(common)
cmn = common

_spec_scene = _ilu.spec_from_file_location("houdinimcp._scene",
                                           os.path.join(ROOT, "_scene.py"))
scene = _ilu.module_from_spec(_spec_scene)
sys.modules["houdinimcp._scene"] = scene
_spec_scene.loader.exec_module(scene)
scn = scene


# ---------------------------------------------------------------------------
# hou stub: enough surface area for get_scene_info / save / load / new /
# serialize_scene. Real classes so isinstance checks work.
# ---------------------------------------------------------------------------
class _FakeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeNodeType(object):
    def __init__(self, name, category_name="Object"):
        self._name = name
        self._category = _FakeCategory(category_name)

    def name(self):
        return self._name

    def category(self):
        return self._category


class _FakeParm(object):
    def __init__(self, name, value):
        self._name = name
        self._value = value

    def name(self):
        return self._name

    def eval(self):
        return self._value


class _FakeNode(object):
    def __init__(self, name, type_name="geo", category="Object", parent=None,
                 children=None, parms=None):
        self._name = name
        self._type = _FakeNodeType(type_name, category)
        self._parent = parent
        self._children = list(children) if children else []
        self._parms = list(parms) if parms else []
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

    def children(self):
        return list(self._children)

    def parms(self):
        return list(self._parms)

    def parent(self):
        return self._parent

    def allSubChildren(self):
        """递归收集所有后代节点（不含自身）。"""
        out = []
        for c in self._children:
            out.append(c)
            out.extend(c.allSubChildren())
        return out


class _FakeHipFile(object):
    def __init__(self):
        self.saved = []
        self.loaded = []
        self.cleared = 0

    def save(self, *args, **kwargs):
        # hou.hipFile.save(file_path=..., save_as_revert=False)
        # 也支持位置参数。统一记录。
        path = kwargs.get("file_path")
        if path is None and args:
            path = args[0]
        self.saved.append((path, kwargs))
        return True

    def load(self, path, **kwargs):
        self.loaded.append((path, kwargs))
        return True

    def clear(self, suppress_save_prompt=False):
        self.cleared += 1
        return True

    def name(self):
        return "/tmp/scene.hip"


class _FakeHou(object):
    def __init__(self, root_node):
        self._root = root_node
        self.hipFile = _FakeHipFile()
        self._version = "21.0.0"

    def node(self, path):
        if path is None:
            return None
        if path == "/" or path == "":
            return self._root
        # Resolve by walking children
        if not path.startswith("/"):
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

    # H21+ 真实存在的新 API
    # Task 8（conftest 揭露性增强 / opera-houdinimcp-h21-compat-audit）：
    # 已移除旧 hou.houdiniVersion() 方法 —— H21 不存在该 API。
    # 若 fork 代码误调 hou.houdiniVersion()，会抛 AttributeError 让单测 FAIL。
    def applicationVersionString(self):
        return self._version

    def applicationVersion(self):
        # H21 真实返回 tuple (major, minor, build)；本 fake 简化为 3-tuple
        return (21, 0, 0)

    def fps(self):
        return 24.0

    def playbar(self):
        class _PB(object):
            def frameRange(self):
                return (1, 240)
        return _PB()


def _make_simple_scene():
    """Build a 3-level deep scene:
        / (root, Object)
            /obj (Object)
                /obj/geo1 (Sop, category 'Sop')
                    /obj/geo1/box1 (box, 'Sop')
    """
    root = _FakeNode("root", type_name="root", category="Object")
    obj = _FakeNode("obj", type_name="obj", category="Object", parent=root)
    geo1 = _FakeNode("geo1", type_name="geo", category="Sop",
                     parent=obj,
                     parms=[_FakeParm("size", 1.0)])
    box1 = _FakeNode("box1", type_name="box", category="Sop", parent=geo1)
    root._children = [obj]
    obj._children = [geo1]
    geo1._children = [box1]
    return root


def _make_hou():
    return _FakeHou(_make_simple_scene())


# ===========================================================================
# Section A: _scene.get_scene_info
# ===========================================================================
class GetSceneInfoTests(unittest.TestCase):
    def test_returns_required_fields(self):
        hou = _make_hou()
        info = scn.get_scene_info(hou)
        for key in ("houdini_version", "node_count", "file_path"):
            self.assertIn(key, info, "missing key: {0}".format(key))

    def test_houdini_version_from_hou(self):
        hou = _make_hou()
        info = scn.get_scene_info(hou)
        self.assertEqual(info["houdini_version"], "21.0.0")

    def test_node_count_is_int(self):
        hou = _make_hou()
        info = scn.get_scene_info(hou)
        self.assertIsInstance(info["node_count"], int)
        # fixture: root has descendants obj, geo1, box1 = 3 descendants
        self.assertEqual(info["node_count"], 3)

    def test_file_path_present(self):
        hou = _make_hou()
        info = scn.get_scene_info(hou)
        self.assertEqual(info["file_path"], "/tmp/scene.hip")


# ===========================================================================
# Section A2: H21 compat — get_scene_info 必须用 applicationVersionString
# ===========================================================================
class H21CompatGetSceneInfoTests(unittest.TestCase):
    """get_scene_info 不得调用 H21 已移除的 hou.houdiniVersion().

    H21 移除了 hou.houdiniVersion；fork 必须改用 hou.applicationVersionString
    （参见 SideFX H22 HOM 索引：
      https://www.sidefx.com/docs/houdini22.0/hom/hou/applicationVersionString ）。
    """

    def test_uses_application_version_string_not_houdiniVersion(self):
        hou = _make_hou()
        # H21 已移除 hou.houdiniVersion；_FakeHou 不再提供（Task 8 揭露性增强）。
        # 用 spy 包装 applicationVersionString 计数
        calls = {"avs": 0}
        original_avs = hou.applicationVersionString

        def _counting_avs():
            calls["avs"] += 1
            return original_avs()
        hou.applicationVersionString = _counting_avs

        info = scn.get_scene_info(hou)
        self.assertEqual(info["houdini_version"], "21.0.0")
        self.assertGreaterEqual(
            calls["avs"], 1,
            "get_scene_info must call hou.applicationVersionString() on H21")
        # 回归保护：_FakeHou 不再提供 hou.houdiniVersion（H21 已移除），
        # 若 fork 代码误调 hou.houdiniVersion()，会抛 AttributeError 让本测试 FAIL。
        self.assertFalse(
            hasattr(hou, "houdiniVersion"),
            "_FakeHou must NOT provide houdiniVersion (removed on H21); "
            "mock contract enforced by Task 8 conftest 揭露性增强")


# ===========================================================================
# Section B: _scene.save_scene
# ===========================================================================
class SaveSceneTests(unittest.TestCase):
    def test_calls_hipFile_save(self):
        hou = _make_hou()
        result = scn.save_scene(hou, "/tmp/out.hip")
        self.assertEqual(len(hou.hipFile.saved), 1)
        self.assertEqual(hou.hipFile.saved[0][0], "/tmp/out.hip")

    def test_returns_success_dict(self):
        hou = _make_hou()
        result = scn.save_scene(hou, "/tmp/out.hip")
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("saved"))
        self.assertEqual(result.get("file_path"), "/tmp/out.hip")

    def test_propagates_hipFile_errors(self):
        hou = _make_hou()

        def _boom(path=None, **kwargs):
            raise RuntimeError("disk full")
        hou.hipFile.save = _boom
        with self.assertRaises(RuntimeError):
            scn.save_scene(hou, "/tmp/out.hip")


# ===========================================================================
# Section C: _scene.load_scene
# ===========================================================================
class LoadSceneTests(unittest.TestCase):
    def test_calls_hipFile_load(self):
        hou = _make_hou()
        result = scn.load_scene(hou, "/tmp/in.hip")
        self.assertEqual(len(hou.hipFile.loaded), 1)
        self.assertEqual(hou.hipFile.loaded[0][0], "/tmp/in.hip")

    def test_returns_success_dict(self):
        hou = _make_hou()
        result = scn.load_scene(hou, "/tmp/in.hip")
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("loaded"))
        self.assertEqual(result.get("file_path"), "/tmp/in.hip")

    def test_invalidate_all_caches_called(self):
        hou = _make_hou()
        called = {"n": 0}
        original = cmn.invalidate_all_caches
        cmn.invalidate_all_caches = lambda: called.__setitem__("n", called["n"] + 1)
        try:
            scn.load_scene(hou, "/tmp/in.hip")
            self.assertEqual(called["n"], 1,
                             "invalidate_all_caches must be called once on load")
        finally:
            cmn.invalidate_all_caches = original


# ===========================================================================
# Section D: _scene.new_scene
# ===========================================================================
class NewSceneTests(unittest.TestCase):
    def test_calls_hipFile_clear(self):
        hou = _make_hou()
        scn.new_scene(hou)
        self.assertEqual(hou.hipFile.cleared, 1)

    def test_clear_called_with_suppress_save_prompt(self):
        hou = _make_hou()
        captured = {}
        original_clear = hou.hipFile.clear

        def _capturing_clear(suppress_save_prompt=False):
            captured["suppress"] = suppress_save_prompt
            return original_clear(suppress_save_prompt)
        hou.hipFile.clear = _capturing_clear
        scn.new_scene(hou)
        self.assertTrue(captured.get("suppress"),
                        "new_scene must call hipFile.clear(suppress_save_prompt=True)")

    def test_invalidate_all_caches_called(self):
        hou = _make_hou()
        called = {"n": 0}
        original = cmn.invalidate_all_caches
        cmn.invalidate_all_caches = lambda: called.__setitem__("n", called["n"] + 1)
        try:
            scn.new_scene(hou)
            self.assertEqual(called["n"], 1,
                             "invalidate_all_caches must be called once on new_scene")
        finally:
            cmn.invalidate_all_caches = original

    def test_returns_success_dict(self):
        hou = _make_hou()
        result = scn.new_scene(hou)
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("cleared"))


# ===========================================================================
# Section E: _scene.serialize_scene + _common.serialize_scene_state (extended)
# ===========================================================================
class SerializeSceneTests(unittest.TestCase):
    def test_default_no_params_field(self):
        hou = _make_hou()
        result = scn.serialize_scene(hou, root_path="/")
        self.assertIn("nodes", result)
        for n in result["nodes"]:
            self.assertNotIn("parameters", n,
                             "default include_params=False must omit parameters")

    def test_include_params_true_adds_parameters(self):
        hou = _make_hou()
        result = scn.serialize_scene(hou, root_path="/", include_params=True)
        # geo1 has a 'size' parm -> at least one node should expose parameters
        nodes_with_params = [n for n in result["nodes"] if "parameters" in n]
        self.assertTrue(nodes_with_params,
                        "include_params=True must produce parameters on at least one node")
        geo1_entry = next((n for n in result["nodes"] if n["path"] == "/obj/geo1"), None)
        self.assertIsNotNone(geo1_entry)
        self.assertIn("parameters", geo1_entry)
        # parameters should be json-safe dict keyed by parm name
        self.assertIn("size", geo1_entry["parameters"])

    def test_max_depth_zero_returns_only_root_summary(self):
        hou = _make_hou()
        result = scn.serialize_scene(hou, root_path="/", max_depth=0)
        # only the root node should be present (no children recursed)
        self.assertEqual(len(result["nodes"]), 1)
        self.assertEqual(result["nodes"][0]["path"], "/")

    def test_max_depth_three_reaches_third_level(self):
        hou = _make_hou()
        result = scn.serialize_scene(hou, root_path="/", max_depth=3)
        paths = [n["path"] for n in result["nodes"]]
        # /obj/geo1/box1 is depth 3 -> should appear
        self.assertIn("/obj/geo1/box1", paths)

    def test_empty_scene_returns_root_only(self):
        # root with no children: nodes list contains only the root entry
        root = _FakeNode("root", type_name="root")
        hou = _FakeHou(root)
        result = scn.serialize_scene(hou, root_path="/")
        self.assertEqual(len(result["nodes"]), 1)
        self.assertEqual(result["node_count"], 1)
        self.assertEqual(result["nodes"][0]["path"], "/")

    def test_node_fields_path_type_category_children_count(self):
        hou = _make_hou()
        result = scn.serialize_scene(hou, root_path="/", max_depth=3)
        geo1 = next((n for n in result["nodes"] if n["path"] == "/obj/geo1"), None)
        self.assertIsNotNone(geo1)
        self.assertEqual(geo1["type"], "geo")
        self.assertEqual(geo1["category"], "Sop")
        self.assertEqual(geo1["children_count"], 1)

    def test_common_serialize_scene_state_signature_compat(self):
        # The extended _common.serialize_scene_state must still accept the
        # legacy positional signature (hou, root_path=None, max_depth=2).
        hou = _make_hou()
        # legacy positional call
        result = cmn.serialize_scene_state(hou, "/")
        self.assertIn("nodes", result)
        # legacy default max_depth=2 still works
        result2 = cmn.serialize_scene_state(hou)
        self.assertIn("nodes", result2)
        # new include_params kw works
        result3 = cmn.serialize_scene_state(hou, "/", include_params=True, max_depth=3)
        self.assertIn("nodes", result3)

    def test_returns_root_path_and_node_count(self):
        hou = _make_hou()
        result = scn.serialize_scene(hou, root_path="/", max_depth=3)
        self.assertEqual(result["root_path"], "/")
        # full walk to depth 3 covers all 4 fixture nodes
        self.assertEqual(result["node_count"], 4)


# ===========================================================================
# Section F: _common.invalidate_all_caches placeholder
# ===========================================================================
class InvalidateAllCachesTests(unittest.TestCase):
    def test_callable(self):
        self.assertTrue(callable(cmn.invalidate_all_caches))

    def test_does_not_raise(self):
        cmn.invalidate_all_caches()
        # second call still no-op
        cmn.invalidate_all_caches()

    def test_returns_none(self):
        self.assertIsNone(cmn.invalidate_all_caches())


# ===========================================================================
# Section G: __all__ export contract (PR 5 additions)
# ===========================================================================
class ExportContractTests(unittest.TestCase):
    def test_common_has_invalidate_all_caches(self):
        self.assertIn("invalidate_all_caches", cmn.__all__)
        self.assertTrue(hasattr(cmn, "invalidate_all_caches"))


if __name__ == "__main__":
    unittest.main()
