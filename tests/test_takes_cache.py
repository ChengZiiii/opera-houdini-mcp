"""add-takes-and-cache-tools 单元测试。

覆盖 8 个新增 API（4 Takes + 4 cache nodes），断言：
- Takes 走 hou.takes.takes / findTake（**不**传字符串给 setCurrentTake）
- create_take 在任何写入前完成 parent / parm 原子预校验
- include addParmTuple 临时切换 new_take 为 current、finally 恢复
- cache adapter 精确匹配 File Cache 白名单（普通 Sop/file 永远不在）
- clear / write 走 adapter 真实 surface，**不**进 undo group
- 全部 8 个工具经 apply_response_cap
- server/bridge 三分类 8 命令唯一穷尽互斥
- bridge 8 @mcp.tool() 中文 docstring + 无类型注解
"""
import ast
import importlib.util
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCENE_PATH = os.path.join(ROOT, "_scene.py")
CACHE_PATH = os.path.join(ROOT, "_cache_nodes.py")
SERVER_PATH = os.path.join(ROOT, "server.py")
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


def _is_stdlib_top_level(name):
    """顶层模块名是否标准库（3.10+ 用 sys.stdlib_module_names）。"""
    stdlib_names = getattr(sys, "stdlib_module_names", None)
    if stdlib_names is not None:
        return name in stdlib_names
    # 旧解释器回退：site-packages 之外的模块视为标准库
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        return False
    if spec is None:
        return False
    origin = spec.origin or ""
    return "site-packages" not in origin and "dist-packages" not in origin


# ---------------------------------------------------------------------------
# Mock objects — Takes / FileCache / 普通 Sop/file。
# ---------------------------------------------------------------------------
class _TakeBase(object):
    """hou.Take 基类；mock take 继承它（isinstance 判定）。"""
    pass


class _Take(_TakeBase):
    """hou.Take mock。"""

    def __init__(self, name, path, parent=None, current=False):
        self._name = name
        self._path = path
        self._parent = parent
        self._current = current
        self._children = []
        self._parm_tuples = []
        self.add_pt_calls = []
        if parent is not None:
            parent._children.append(self)

    def name(self):
        return self._name

    def path(self):
        return self._path

    def parent(self):
        return self._parent

    def isCurrent(self):
        return self._current

    def children(self):
        return list(self._children)

    def parmTuples(self):
        return list(self._parm_tuples)

    def addChildTake(self, name):
        child_path = (self._path + "/" + name) if self._path else name
        child = _Take(name, child_path, parent=self)
        return child

    def addParmTuple(self, pt):
        self._parm_tuples.append(pt)
        self.add_pt_calls.append(pt)

    def destroy(self):
        if self._parent is not None and self in self._parent._children:
            self._parent._children.remove(self)


class _Takes(object):
    """hou.takes mock。"""

    def __init__(self, root):
        self._root = root
        self._set_current_calls = []

    def _collect(self):
        out = [self._root]
        def walk(t):
            for c in t.children():
                out.append(c)
                walk(c)
        walk(self._root)
        return out

    def takes(self):
        return self._collect()

    def currentTake(self):
        for t in self._collect():
            if t.isCurrent():
                return t
        return None

    def setCurrentTake(self, take):
        if not isinstance(take, _TakeBase):
            raise TypeError("must pass hou.Take object, got " + repr(take))
        for t in self._collect():
            t._current = False
        take._current = True
        self._set_current_calls.append(take)

    def findTake(self, name):
        if not isinstance(name, str):
            return None
        if name == "":
            return None
        # match by name (last segment) or full path
        for t in self._collect():
            if t.name() == name or t.path() == name:
                return t
        return None


class _Parm(object):
    """hou.Parm mock（component level）。"""
    def __init__(self, node, tuple_name, index, comp_name=None):
        self._node = node
        self._tuple_name = tuple_name
        self._index = index
        # In real Houdini, component parm name is ``<tuple_name><index>``-
        # like "sizex"/"sizey"/"sizez" or "tx"/"ty"/"tz" depending on
        # the template. Mock builds comp_name via comp_name hint or by
        # using ``tuple_name + <suffix>`` when caller supplies it.
        if comp_name is None:
            self._comp_name = tuple_name
        else:
            self._comp_name = comp_name

    def name(self):
        return self._comp_name

    def tuple(self):
        return self._node.parmTuples()[self._node._tuple_index(self._tuple_name)]


class _ParmTuple(object):
    def __init__(self, node, name, comp_names=None, n=1):
        self._node = node
        self._name = name
        self._n = n
        if comp_names is None:
            self._comps = [_Parm(node, name, i) for i in range(n)]
        else:
            self._comps = [_Parm(node, name, i, comp_name=cn)
                           for i, cn in enumerate(comp_names)]

    def name(self):
        return self._name

    def node(self):
        return self._node

    def __len__(self):
        return self._n


class _Node(object):
    """hou.Node mock with parmTuples registry。"""

    def __init__(self, path):
        self._path = path
        self._tuples = {}

    def path(self):
        return self._path

    def parmTuples(self):
        return list(self._tuples.values())

    def _tuple_index(self, name):
        for i, t in enumerate(self._tuples.values()):
            if t.name() == name:
                return i
        raise KeyError(name)

    def parmTuple(self, name):
        return self._tuples.get(name)

    def parm(self, name):
        # exact tuple name -> first component
        if name in self._tuples:
            return self._tuples[name]._comps[0]
        # exact component name
        for t in self._tuples.values():
            for c in t._comps:
                if c._comp_name == name:
                    return c
        return None

    def add_parm_tuple(self, name, n=1, comp_names=None):
        pt = _ParmTuple(self, name, comp_names=comp_names, n=n)
        self._tuples[name] = pt
        return pt


class _Hou(object):
    """hou mock：takes + parmTuple/parm + node 解析。"""

    def __init__(self, root_take=None):
        self._nodes = {}
        if root_take is None:
            self._root = _Take("Main", "Main", parent=None, current=True)
        else:
            self._root = root_take
        self.takes = _Takes(self._root)

    def node(self, path):
        return self._nodes.get(path)

    def parmTuple(self, path):
        # extract node path
        if "/" not in path:
            return None
        parts = path.rsplit("/", 1)
        if len(parts) != 2:
            return None
        node_path, tuple_name = parts
        node = self._nodes.get(node_path)
        if node is None:
            return None
        return node.parmTuple(tuple_name)

    def parm(self, path):
        # /a/b/c[0]? or /a/b/c? — try tuple path first
        node = None
        rest = path
        # find longest matching node path
        candidates = sorted(self._nodes.keys(), key=len, reverse=True)
        for np in candidates:
            if path == np or path.startswith(np + "/"):
                node = self._nodes[np]
                rest = path[len(np):].lstrip("/")
                break
        if node is None:
            return None
        # if rest is tuple name, return first component
        if rest in node._tuples:
            return node._tuples[rest]._comps[0]
        # else try to find a component by component name (e.g. "sizex")
        for tt in node._tuples.values():
            for c in tt._comps:
                if c._comp_name == rest:
                    return c
        return None

    def applicationVersion(self):
        return (21, 0, 596)

    def applicationVersionString(self):
        return "21.0.596"

    def register(self, path, node):
        self._nodes[path] = node


# ---------------------------------------------------------------------------
# Cache adapter mock surface（filecache::2.0 + 普通 file SOP）
# ---------------------------------------------------------------------------
class _FileCacheNode(object):
    """Mock SopNode 'filecache::2.0' 真实 parm/cook 流程。"""

    def __init__(self, path, file_path):
        self._path = path
        self._type_name = "filecache::2.0"
        self._file_path = file_path
        self._loadfromdisk = 0
        self._errors = ()
        self._warnings = ()
        self.cook_calls = 0
        self.geometry_calls = 0
        self.save_calls = []
        self._load_parm = _Par(self, "loadfromdisk", 0)
        self._file_parm = _Par(self, "file", file_path)

    def path(self):
        return self._path

    def type(self):
        return _TypeObj(self._type_name)

    def parm(self, name):
        if name == "loadfromdisk":
            return self._load_parm
        if name == "file":
            return self._file_parm
        return None

    def cook(self, force=True):
        self.cook_calls += 1

    def errors(self):
        return list(self._errors)

    def warnings(self):
        return list(self._warnings)

    def geometry(self):
        self.geometry_calls += 1
        return _Geometry(self._file_path, save_to_file_fn=self._save_to_file)

    def _save_to_file(self, path):
        self.save_calls.append(path)
        with open(path, "wb") as handle:
            handle.write(b"MCPCACHE")
        return True


class _Par(object):
    def __init__(self, node, name, value):
        self._node = node
        self._name = name
        self._value = value
        self.set_calls = []

    def name(self):
        return self._name

    def eval(self):
        return self._value

    def set(self, v):
        self._value = v
        self.set_calls.append(v)


class _Geometry(object):
    def __init__(self, file_path, save_to_file_fn):
        self._file_path = file_path
        self._save_to_file_fn = save_to_file_fn
        self._ok = True

    def saveToFile(self, path):
        return self._save_to_file_fn(path)


class _SopFileNode(object):
    """普通 Sop/file（**不**在 cache 白名单）。"""

    def __init__(self, path):
        self._path = path
        self._type_name = "file"
        self._file_path = ""
        self._parms = {"file": _Par(self, "file", "")}
        self.cook_calls = 0

    def path(self):
        return self._path

    def type(self):
        return _TypeObj(self._type_name)

    def parm(self, name):
        return self._parms.get(name)

    def cook(self, force=True):
        self.cook_calls += 1

    def errors(self):
        return ()

    def warnings(self):
        return ()

    def geometry(self):
        return _Geometry(self._file_path, save_to_file_fn=lambda p: None)


class _TypeObj(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _UnknownNode(object):
    def __init__(self, path):
        self._path = path
        self._type_name = "box"

    def path(self):
        return self._path

    def type(self):
        return _TypeObj(self._type_name)

    def parm(self, name):
        return None

    def errors(self):
        return ()

    def warnings(self):
        return ()


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------
_pkg = types.ModuleType("takes_cache_test_pkg")
_pkg.__path__ = [ROOT]
sys.modules["takes_cache_test_pkg"] = _pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_scene():
    common_name = "takes_cache_test_pkg._common"
    scene_name = "takes_cache_test_pkg._scene"
    for name in (scene_name, common_name):
        sys.modules.pop(name, None)
    _load(common_name, os.path.join(ROOT, "_common.py"))
    return _load(scene_name, SCENE_PATH)


def _load_cache():
    common_name = "takes_cache_test_pkg._common"
    cache_name = "takes_cache_test_pkg._cache_nodes"
    for name in (cache_name, common_name):
        sys.modules.pop(name, None)
    _load(common_name, os.path.join(ROOT, "_common.py"))
    return _load(cache_name, CACHE_PATH)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
TC_8 = {
    "list_takes", "get_current_take", "set_current_take", "create_take",
    "list_caches", "get_cache_status", "clear_cache", "write_cache",
}
TC_MUT = {"set_current_take", "create_take"}
TC_NO_UNDO = {"clear_cache", "write_cache"}
TC_RO = {"list_takes", "get_current_take", "list_caches", "get_cache_status"}


class TakesQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene = _load_scene()

    def test_module_does_not_import_hou_or_new_dependencies(self):
        """_scene.py 不得 import hou 或任何第三方新依赖。

        fix-save-scene-untitled-dialog-hang 起允许顶层标准库模块
        （new_scene env 闸需要 os.environ）；第三方便用
        sys.stdlib_module_names（3.10+）排除，旧解释器按 origin
        是否位于 site-packages 判定。
        """
        source = open(SCENE_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        self.assertNotIn("hou", imports)
        non_stdlib = set()
        for name in imports:
            if name in ("", "_common"):
                continue  # 内部相对导入
            if _is_stdlib_top_level(name):
                continue  # 标准库（如 os）
            non_stdlib.add(name)
        self.assertFalse(non_stdlib,
                         "unexpected non-stdlib import(s): %r" % sorted(non_stdlib))

    def test_forbidden_fictional_apis_absent(self):
        source = open(SCENE_PATH, "r", encoding="utf-8").read()
        for forbidden in ("hou.takes.addTake", "setCurrentTake(name)",
                          "addTake(name)"):
            self.assertNotIn(forbidden, source,
                             "forbidden legacy/fictional API: " + forbidden)

    def test_list_takes_returns_name_path_parent_current(self):
        hou = _Hou()
        # add a child take
        a = hou.takes._root.addChildTake("alpha")
        b = a.addChildTake("beta")
        result = self.scene.list_takes(hou)
        self.assertEqual(result["status"], "success")
        entries = {item["name"]: item for item in result["takes"]}
        self.assertIn("Main", entries)
        self.assertEqual(entries["Main"]["path"], "Main")
        self.assertIsNone(entries["Main"]["parent"])
        self.assertTrue(entries["Main"]["current"])
        self.assertEqual(entries["alpha"]["parent"], "Main")
        self.assertEqual(entries["beta"]["parent"], "Main/alpha")
        self.assertEqual(result["total"], 3)

    def test_get_current_take_reports_current(self):
        hou = _Hou()
        a = hou.takes._root.addChildTake("alpha")
        hou.takes.setCurrentTake(a)
        result = self.scene.get_current_take(hou)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["name"], "alpha")
        self.assertEqual(result["path"], "Main/alpha")
        self.assertEqual(result["parent"], "Main")
        self.assertTrue(result["current"])

    def test_set_current_take_uses_take_object_not_string(self):
        hou = _Hou()
        a = hou.takes._root.addChildTake("alpha")
        result = self.scene.set_current_take(hou, "alpha")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["name"], "alpha")
        # setCurrentTake must be called with hou.Take object
        self.assertEqual(len(hou.takes._set_current_calls), 1)
        self.assertIs(hou.takes._set_current_calls[0], a)
        self.assertTrue(a.isCurrent())

    def test_set_current_take_rejects_unknown_name(self):
        hou = _Hou()
        result = self.scene.set_current_take(hou, "missing")
        self.assertEqual(result["error"]["code"], "take_not_found")


class CreateTakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene = _load_scene()

    def test_create_take_uses_addChildTake_not_addTake(self):
        # exercise: create_take must call parent.addChildTake
        hou = _Hou()
        node = _Node("/obj/geo1")
        node.add_parm_tuple("size", n=3)
        hou.register("/obj/geo1", node)
        before = len(hou.takes._root.children())
        result = self.scene.create_take(hou, "new_a")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["name"], "new_a")
        self.assertEqual(result["parent"], "Main")
        self.assertEqual(len(hou.takes._root.children()), before + 1)
        # default parent is current take = root
        new = hou.takes._root.children()[-1]
        self.assertIsNone(new.parent().parent())

    def test_create_take_rejects_duplicate_name(self):
        hou = _Hou()
        hou.takes._root.addChildTake("dup")
        result = self.scene.create_take(hou, "dup")
        self.assertEqual(result["error"]["code"], "take_name_conflict")

    def test_create_take_rejects_slash_in_name(self):
        hou = _Hou()
        result = self.scene.create_take(hou, "a/b")
        self.assertEqual(result["error"]["code"], "invalid_take_name")

    def test_create_take_atomic_include_parm_validation(self):
        # one valid + one invalid -> whole call rejected, no new take
        hou = _Hou()
        node = _Node("/obj/geo1")
        node.add_parm_tuple("size", n=3)
        hou.register("/obj/geo1", node)
        before = len(hou.takes._root.children())
        result = self.scene.create_take(
            hou, "atomic_test",
            include_parms=["/obj/geo1/size", "/obj/geo1/missing"])
        self.assertEqual(result["error"]["code"], "parm_not_found")
        # no take created
        self.assertEqual(len(hou.takes._root.children()), before)

    def test_create_take_include_component_parm_path(self):
        # component path -> parmTuple resolution
        hou = _Hou()
        node = _Node("/obj/geo2")
        node.add_parm_tuple("size", n=3,
                            comp_names=("sizex", "sizey", "sizez"))
        hou.register("/obj/geo2", node)
        result = self.scene.create_take(
            hou, "with_component", include_parms=["/obj/geo2/sizex"])
        self.assertEqual(result["status"], "success")
        new = hou.takes._root.children()[-1]
        # 1 parm tuple added
        self.assertEqual(len(new.add_pt_calls), 1)
        self.assertEqual(new.add_pt_calls[0].name(), "size")
        self.assertEqual(len(result["include_parms"]), 1)
        self.assertEqual(result["include_parms"][0]["parm_tuple"], "size")

    def test_create_take_restores_current_after_include(self):
        hou = _Hou()
        a = hou.takes._root.addChildTake("orig_current")
        hou.takes.setCurrentTake(a)
        node = _Node("/obj/geo3")
        node.add_parm_tuple("size", n=3)
        hou.register("/obj/geo3", node)
        result = self.scene.create_take(
            hou, "with_restore", include_parms=["/obj/geo3/size"])
        self.assertEqual(result["status"], "success")
        # after create, current must be restored to original
        self.assertTrue(a.isCurrent())

    def test_create_take_duplicate_parm_tuple_path_rejected(self):
        hou = _Hou()
        node = _Node("/obj/geo4")
        node.add_parm_tuple("size", n=3)
        hou.register("/obj/geo4", node)
        result = self.scene.create_take(
            hou, "dup_path", include_parms=["/obj/geo4/size",
                                            "/obj/geo4/size"])
        self.assertEqual(result["error"]["code"], "duplicate_parm_tuple")


class CacheAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = _load_cache()

    def test_module_does_not_import_hou_or_new_dependencies(self):
        source = open(CACHE_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        self.assertNotIn("hou", imports)
        # _common is package-internal; os is stdlib; "" is the
        # ``from . import _common`` relative marker.
        self.assertTrue(imports <= {"os", "_common", ""})

    def test_filecache_in_whitelist_sop_file_not(self):
        # whitelist contains filecache variants
        self.assertIn("filecache", self.cache._FILECACHE_TYPES)
        self.assertIn("filecache::2.0", self.cache._FILECACHE_TYPES)
        # regular Sop/file is not in whitelist
        self.assertNotIn("file", self.cache._FILECACHE_TYPES)

    def test_match_returns_none_for_sop_file(self):
        hou = _Hou()
        node = _SopFileNode("/obj/geo1/file1")
        # adapter should reject
        adapter, info = self.cache._adapter_for(hou, node)
        self.assertIsNone(adapter)
        self.assertIsNone(info)

    def test_match_returns_filecache_adapter(self):
        hou = _Hou()
        node = _FileCacheNode("/obj/geo1/fc", "/tmp/x.bgeo")
        adapter, info = self.cache._adapter_for(hou, node)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter["name"], "filecache")
        self.assertEqual(info["type"], "filecache::2.0")

    def test_list_caches_only_returns_whitelisted(self):
        hou = _Hou()
        fc = _FileCacheNode("/obj/geo1/fc", "/tmp/x.bgeo")
        sop_file = _SopFileNode("/obj/geo1/file1")
        box = _UnknownNode("/obj/geo1/box1")
        # register as nodes by direct attr
        class _Parent(object):
            def __init__(self, children):
                self._c = children
            def children(self):
                return list(self._c)
            def path(self):
                return "/obj/geo1"
        parent = _Parent([fc, sop_file, box])
        hou._root = _Take("Main", "Main", parent=None, current=True)
        hou.takes = _Takes(hou._root)
        # patch hou.node to return parent
        hou.node = lambda p: parent if p == "/obj/geo1" else None
        result = self.cache.list_caches(hou, "/obj/geo1", max_nodes=10)
        self.assertEqual(result["status"], "success")
        paths = [c["path"] for c in result["caches"]]
        self.assertIn("/obj/geo1/fc", paths)
        self.assertNotIn("/obj/geo1/file1", paths)
        self.assertNotIn("/obj/geo1/box1", paths)

    def test_get_cache_status_rejects_unknown_type(self):
        hou = _Hou()
        box = _UnknownNode("/obj/geo1/box1")
        hou.node = lambda p: box if p == "/obj/geo1/box1" else None
        result = self.cache.get_cache_status(hou, "/obj/geo1/box1")
        self.assertEqual(result["error"]["code"], "unsupported_cache_type")

    def test_get_cache_status_reports_filecache_fields(self):
        import tempfile, os
        tmp = tempfile.mkdtemp(prefix="tc_test_")
        try:
            fpath = os.path.join(tmp, "x.bgeo")
            with open(fpath, "wb") as h:
                h.write(b"X")
            fc = _FileCacheNode("/obj/geo1/fc", fpath)
            hou = _Hou()
            hou.node = lambda p: fc if p == "/obj/geo1/fc" else None
            result = self.cache.get_cache_status(hou, "/obj/geo1/fc")
            self.assertEqual(result["status"], "success")
            cs = result["cache_status"]
            self.assertEqual(cs["adapter"], "filecache")
            self.assertEqual(cs["type"], "filecache::2.0")
            self.assertTrue(cs["file_exists"])
            self.assertEqual(cs["loadfromdisk"], 0)
        finally:
            import shutil
            shutil.rmtree(tmp)

    def test_clear_cache_uses_loadfromdisk_0(self):
        import tempfile, os
        tmp = tempfile.mkdtemp(prefix="tc_clear_")
        try:
            fpath = os.path.join(tmp, "x.bgeo")
            with open(fpath, "wb") as h:
                h.write(b"X")
            fc = _FileCacheNode("/obj/geo1/fc", fpath)
            hou = _Hou()
            hou.node = lambda p: fc if p == "/obj/geo1/fc" else None
            result = self.cache.clear_cache(hou, "/obj/geo1/fc")
            self.assertEqual(result["status"], "success")
            # loadfromdisk should be 0 and cook called
            self.assertEqual(fc._load_parm._value, 0)
            self.assertGreaterEqual(fc.cook_calls, 1)
            # default remove_disk_file=False — file still on disk
            self.assertTrue(os.path.isfile(fpath))
            self.assertFalse(result["disk_removed"])
        finally:
            import shutil
            shutil.rmtree(tmp)

    def test_clear_cache_remove_disk_file_actually_deletes(self):
        import tempfile, os
        tmp = tempfile.mkdtemp(prefix="tc_clear2_")
        try:
            fpath = os.path.join(tmp, "x.bgeo")
            with open(fpath, "wb") as h:
                h.write(b"X")
            fc = _FileCacheNode("/obj/geo1/fc", fpath)
            hou = _Hou()
            hou.node = lambda p: fc if p == "/obj/geo1/fc" else None
            result = self.cache.clear_cache(
                hou, "/obj/geo1/fc", remove_disk_file=True)
            self.assertTrue(result["disk_removed"])
            self.assertFalse(os.path.isfile(fpath))
        finally:
            import shutil
            shutil.rmtree(tmp)

    def test_write_cache_writes_real_file(self):
        import tempfile, os
        tmp = tempfile.mkdtemp(prefix="tc_write_")
        try:
            fpath = os.path.join(tmp, "x.bgeo")
            fc = _FileCacheNode("/obj/geo1/fc", fpath)
            hou = _Hou()
            hou.node = lambda p: fc if p == "/obj/geo1/fc" else None
            result = self.cache.write_cache(hou, "/obj/geo1/fc")
            self.assertEqual(result["status"], "success")
            self.assertTrue(result["written"]["written"])
            self.assertTrue(os.path.isfile(fpath))
            self.assertGreater(os.path.getsize(fpath), 0)
            # disk_side_effect must be True
            self.assertTrue(result["disk_side_effect"])
        finally:
            import shutil
            shutil.rmtree(tmp)

    def test_write_cache_rejects_unknown_type(self):
        hou = _Hou()
        box = _UnknownNode("/obj/geo1/box1")
        hou.node = lambda p: box if p == "/obj/geo1/box1" else None
        result = self.cache.write_cache(hou, "/obj/geo1/box1")
        self.assertEqual(result["error"]["code"], "unsupported_cache_type")


class ResponseCapTests(unittest.TestCase):
    def test_all_eight_public_functions_cap_success_and_error_paths(self):
        scene = _load_scene()
        cache = _load_cache()
        original_scene = scene.cmn.apply_response_cap
        original_cache = cache.cmn.apply_response_cap
        scene_cap_calls = []
        cache_cap_calls = []

        def cap_scene(value, max_bytes=16384):
            scene_cap_calls.append(value.get("status") if isinstance(value, dict)
                                   else None)
            result = dict(value)
            result["_cap_test"] = True
            return result

        def cap_cache(value, max_bytes=16384):
            cache_cap_calls.append(value.get("status") if isinstance(value, dict)
                                   else None)
            result = dict(value)
            result["_cap_test"] = True
            return result

        scene.cmn.apply_response_cap = cap_scene
        cache.cmn.apply_response_cap = cap_cache
        try:
            # Takes
            hou = _Hou()
            hou.takes._root.addChildTake("alpha")
            t_res = [scene.list_takes(hou), scene.get_current_take(hou)]
            # create_take
            node = _Node("/obj/geo1")
            node.add_parm_tuple("size", n=3)
            hou.register("/obj/geo1", node)
            t_res.append(scene.create_take(hou, "cap_a"))
            # set_current_take
            t_res.append(scene.set_current_take(hou, "Main"))
            # error path
            t_res.append(scene.set_current_take(hou, "missing"))

            # Caches
            import tempfile
            tmp = tempfile.mkdtemp(prefix="cap_")
            try:
                fpath = tmp + "/x.bgeo"
                with open(fpath, "wb") as h:
                    h.write(b"X")
                fc = _FileCacheNode("/obj/geo1/fc", fpath)
                sop_file = _SopFileNode("/obj/geo1/file1")
                class _Parent(object):
                    def __init__(self, children):
                        self._c = children
                    def children(self):
                        return list(self._c)
                    def path(self):
                        return "/obj/geo1"
                parent = _Parent([fc, sop_file])
                hou2 = _Hou()
                hou2._root = _Take("Main", "Main", parent=None, current=True)
                hou2.takes = _Takes(hou2._root)
                hou2.node = lambda p: (parent if p == "/obj/geo1"
                                       else (fc if p == "/obj/geo1/fc"
                                             else None))
                c_res = [cache.list_caches(hou2, "/obj/geo1", max_nodes=10),
                         cache.get_cache_status(hou2, "/obj/geo1/fc"),
                         cache.clear_cache(hou2, "/obj/geo1/fc"),
                         cache.write_cache(hou2, "/obj/geo1/fc"),
                         cache.get_cache_status(hou2, "/missing")]
            finally:
                import shutil
                shutil.rmtree(tmp)
        finally:
            scene.cmn.apply_response_cap = original_scene
            cache.cmn.apply_response_cap = original_cache
        self.assertEqual(len(scene_cap_calls), 5)
        self.assertEqual(len(cache_cap_calls), 5)
        self.assertTrue(all(r["_cap_test"] for r in t_res + c_res))


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
    def test_eight_commands_are_pairwise_disjoint_and_exhaustive(self):
        values, handlers, _ = _class_sets_and_handlers()
        mut = values["MUTATING_COMMANDS"] & TC_8
        ro = values["READ_ONLY_COMMANDS"] & TC_8
        no_undo = values["NO_UNDO_COMMANDS"] & TC_8
        self.assertEqual(mut, TC_MUT)
        self.assertEqual(ro, TC_RO)
        self.assertEqual(no_undo, TC_NO_UNDO)
        self.assertEqual(mut | ro | no_undo, TC_8)
        self.assertFalse(mut & ro)
        self.assertFalse(mut & no_undo)
        self.assertFalse(ro & no_undo)
        self.assertTrue(TC_8 <= handlers)

    def test_server_handlers_apply_cap(self):
        source = open(SERVER_PATH, "r", encoding="utf-8").read()
        _, _, class_node = _class_sets_and_handlers()
        functions = {node.name: node for node in class_node.body
                     if isinstance(node, ast.FunctionDef)}
        for command in TC_8:
            fn = functions["handle_" + command]
            segment = ast.get_source_segment(source, fn) or ""
            self.assertIn("cmn.apply_response_cap", segment, command)

    def test_read_only_and_no_undo_handlers_never_create_undo_group(self):
        source = open(SERVER_PATH, "r", encoding="utf-8").read()
        _, _, class_node = _class_sets_and_handlers()
        functions = {node.name: node for node in class_node.body
                     if isinstance(node, ast.FunctionDef)}
        for command in TC_NO_UNDO | TC_RO:
            fn = functions["handle_" + command]
            segment = ast.get_source_segment(source, fn) or ""
            # Exclude docstring (which may explain why no undo)
            body_segment = (segment.split('"""', 2)[-1] if '"""' in segment
                            else segment)
            self.assertNotIn("undos.group", body_segment, command)

    def test_bridge_has_exact_eight_unannotated_chinese_tools(self):
        source = open(BRIDGE_PATH, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        found = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in TC_8:
                continue
            has_tool = any(
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
                for dec in node.decorator_list)
            if has_tool:
                found[node.name] = node
        self.assertEqual(set(found), TC_8)
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
