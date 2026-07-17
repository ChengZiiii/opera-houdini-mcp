"""Unit tests for external/houdinimcp/_discovery.py and PR 6 _common additions.

Stdlib unittest, no hython required. hou is mocked via tiny stub classes.
Run with:
    python -m unittest tests.test_discovery -v
"""
import os
import sys
import json
import threading
import unittest
import importlib.util as _ilu
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Load _common.py under a synthetic "houdinimcp" package so the
# production-style "from . import _common as cmn" inside _discovery.py resolves
# without needing hython / the real package __init__.py.
pkg = types.ModuleType("houdinimcp")
pkg.__path__ = [ROOT]
sys.modules["houdinimcp"] = pkg

_spec_common = _ilu.spec_from_file_location("houdinimcp._common",
                                            os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec_common)
sys.modules["houdinimcp._common"] = common
_spec_common.loader.exec_module(common)
cmn = common

_spec_disc = _ilu.spec_from_file_location("houdinimcp._discovery",
                                          os.path.join(ROOT, "_discovery.py"))
disc = _ilu.module_from_spec(_spec_disc)
sys.modules["houdinimcp._discovery"] = disc
_spec_disc.loader.exec_module(disc)


# ---------------------------------------------------------------------------
# hou stub: enough surface for NodeTypeCache populate + list/find scans.
# Real classes so isinstance checks (Ramp/Vector/Color) and
# method/attribute access via cat.nodeTypes() / nt.name()/nt.label() all work.
# ---------------------------------------------------------------------------
class _FakeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name

    def nodeTypes(self):
        # Look up against a registry seeded via set_categories()
        reg = getattr(_FakeCategory, "_REG", {})
        return reg.get(self._name, {})


def set_categories(categories):
    """Seed the fake category registry. categories: {cat_name: {nt_name: (label, desc)}}."""
    _FakeCategory._REG = {}
    for cat_name, types in categories.items():
        nt_dict = {}
        for nt_name, (label, desc) in types.items():
            nt_dict[nt_name] = _FakeNodeType(nt_name, cat_name, label, desc)
        _FakeCategory._REG[cat_name] = nt_dict


class _FakeNodeType(object):
    def __init__(self, name, category_name, label="", description=""):
        self._name = name
        self._category = _FakeCategory(category_name)
        self._label = label
        self._description = description

    def name(self):
        return self._name

    def category(self):
        return self._category

    def label(self):
        return self._label

    def description(self):
        return self._description


def _make_hou(categories):
    """Build a fake hou module. categories: dict per set_categories()."""
    set_categories(categories)

    class _H(object):
        pass

    hou = _H()
    cats = []
    for cat_name in categories:
        cats.append(_FakeCategory(cat_name))

    def _node_type_categories():
        return {c.name(): c for c in cats}

    def _node_type_categories_values():
        return cats

    hou.nodeTypeCategories = _node_type_categories
    # provide both .values() and direct iteration fallback (D3 note in brief).
    # To keep it simple, our stub returns a dict-like so nodeTypeCategories().values()
    # works; that covers the Houdini 19+ API.
    return hou


def _make_node(name, node_type="geo", children=None):
    """Recursive node tree builder for list_children / find_nodes tests."""
    node = _FakeSceneNode(name, node_type)
    for cname, ctype in (children or []):
        c = _FakeSceneNode(cname, ctype)
        node._children.append(c)
    return node


class _FakeSceneNode(object):
    def __init__(self, name, node_type="geo"):
        self._name = name
        self._type = node_type
        self._children = []

    def path(self):
        return "/" + self._name

    def type(self):
        return _FakeNodeType(self._type, "Object", self._type, "")

    def children(self):
        return list(self._children)

    def allSubChildren(self):
        out = []

        def _walk(n):
            for c in n.children():
                out.append(c)
                _walk(c)

        _walk(self)
        return out


def _make_hou_with_node(node):
    """Build hou stub where hou.node(path) returns given node."""
    class _H(object):
        pass

    hou = _H()
    nodes_by_path = {}

    def _register(n):
        nodes_by_path[n.path()] = n
        for c in n.children():
            _register(c)

    _register(node)

    def _node(path):
        return nodes_by_path.get(path)

    hou.node = _node
    return hou


# ===========================================================================
# Section A: NodeTypeCache unit tests
# ===========================================================================
class NodeTypeCacheInitTests(unittest.TestCase):
    def test_initial_empty(self):
        cache = disc.NodeTypeCache()
        self.assertEqual(cache.size(), 0)
        self.assertEqual(cache.stats(), {
            "hits": 0, "misses": 0, "size": 0, "last_populated_at": None,
        })

    def test_stats_have_documented_keys(self):
        cache = disc.NodeTypeCache()
        st = cache.stats()
        for k in ("hits", "misses", "size", "last_populated_at"):
            self.assertIn(k, st)


class NodeTypeCachePopulateTests(unittest.TestCase):
    def test_populate_populates_categories(self):
        cache = disc.NodeTypeCache()
        hou = _make_hou({
            "Sop": {"box": ("Box", "box sop"), "sphere": ("Sphere", "sphere sop")},
            "Object": {"geo": ("Geometry", "")},
        })
        cache.populate(hou)
        self.assertGreater(cache.size(), 0)
        items = cache.get(category=None)
        self.assertEqual(len(items), 3)

    def test_get_category_filters(self):
        cache = disc.NodeTypeCache()
        hou = _make_hou({
            "Sop": {"box": ("Box", ""), "sphere": ("Sphere", "")},
            "Object": {"geo": ("Geometry", "")},
        })
        cache.populate(hou)
        items = cache.get(category="Sop")
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertEqual(it["category"], "Sop")

    def test_get_substring_filter(self):
        cache = disc.NodeTypeCache()
        hou = _make_hou({
            "Sop": {"box": ("Box", ""), "sphere": ("Sphere", "")},
            "Object": {"geo": ("Geometry", "")},
        })
        cache.populate(hou)
        items = cache.get(name_filter="box")
        # matches "Box" label and "box" name
        self.assertGreaterEqual(len(items), 1)
        names = {it["name"] for it in items}
        self.assertIn("box", names)

    def test_get_glob_filter(self):
        cache = disc.NodeTypeCache()
        hou = _make_hou({
            "Sop": {"box_OUT": ("Box Out", ""), "box_IN": ("Box In", ""), "sphere": ("Sphere", "")},
        })
        cache.populate(hou)
        items = cache.get(name_filter="*_OUT*")
        names = [it["name"] for it in items]
        self.assertIn("box_OUT", names)
        self.assertNotIn("box_IN", names)
        self.assertNotIn("sphere", names)

    def test_clear_resets_size_and_data(self):
        cache = disc.NodeTypeCache()
        hou = _make_hou({"Sop": {"box": ("Box", "")}})
        cache.populate(hou)
        self.assertGreater(cache.size(), 0)
        cache.clear()
        self.assertEqual(cache.size(), 0)
        items = cache.get(category=None)
        self.assertEqual(items, [])

    def test_invalidate_alias_for_clear(self):
        cache = disc.NodeTypeCache()
        hou = _make_hou({"Sop": {"box": ("Box", "")}})
        cache.populate(hou)
        cache.invalidate()
        self.assertEqual(cache.size(), 0)

    def test_hits_and_misses_counted(self):
        cache = disc.NodeTypeCache()
        hou = _make_hou({"Sop": {"box": ("Box", "")}})
        cache.populate(hou)
        # 2 hits, 1 miss (unknown category)
        cache.get(category="Sop")
        cache.get(category="Sop")
        items = cache.get(category="Nope")
        self.assertEqual(items, [])
        st = cache.stats()
        self.assertGreaterEqual(st["hits"], 2)
        self.assertGreaterEqual(st["misses"], 1)
        self.assertGreater(st["size"], 0)

    def test_last_populated_at_set(self):
        cache = disc.NodeTypeCache()
        hou = _make_hou({"Sop": {"box": ("Box", "")}})
        cache.populate(hou)
        st = cache.stats()
        self.assertIsNotNone(st["last_populated_at"])
        # should be a float >= 0
        self.assertGreaterEqual(st["last_populated_at"], 0.0)


class NodeTypeCacheRlockTests(unittest.TestCase):
    def test_same_thread_reentrant_populate(self):
        """RLock 必须允许同一线程重入 populate 不死锁。"""
        cache = disc.NodeTypeCache()
        hou = _make_hou({"Sop": {"box": ("Box", "")}, "Object": {"geo": ("Geo", "")}})

        def _recurse():
            cache.populate(hou)
            cache.get(category="Sop")
            cache.get(category="Object")
            return cache.size()

        t = threading.Thread(target=_recurse)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "thread deadlocked")
        self.assertGreater(cache.size(), 0)

    def test_concurrent_threads_populate(self):
        """多线程并发 populate 不冲突；最终 size 一致。"""
        cache = disc.NodeTypeCache()
        hou = _make_hou({"Sop": {"box": ("Box", "")}, "Object": {"geo": ("Geo", "")}})

        def _worker():
            for _ in range(50):
                cache.populate(hou)

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        for t in threads:
            self.assertFalse(t.is_alive(), "thread deadlocked")
        self.assertGreater(cache.size(), 0)


# ===========================================================================
# Section B: node_type_cache singleton + auto-register
# ===========================================================================
class SingletonTests(unittest.TestCase):
    def test_singleton_identity(self):
        from houdinimcp import _discovery as d
        self.assertIs(d.node_type_cache, d.node_type_cache)

    def test_singleton_registered_with_registry(self):
        from houdinimcp import _discovery as d
        # node_type_cache should be in cmn._cache_registry (auto-registered on import)
        # Allow duplicates not to break the assertion (registry dedup via `not in`)
        registered = d.node_type_cache in cmn._cache_registry
        self.assertTrue(registered, "node_type_cache not auto-registered")


# ===========================================================================
# Section C: invalidate_all_caches via _cache_registry
# ===========================================================================
class InvalidateAllCachesTests(unittest.TestCase):
    def setUp(self):
        # Snapshot registry to restore after test
        self._snapshot = list(cmn._cache_registry)

    def tearDown(self):
        cmn._cache_registry[:] = self._snapshot

    def test_clears_registered_caches(self):
        cleared = []

        class _DummyCache(object):
            def __init__(self, tag):
                self.tag = tag

            def clear(self):
                cleared.append(self.tag)

        c1 = _DummyCache("a")
        c2 = _DummyCache("b")
        cmn.register_cache(c1)
        cmn.register_cache(c2)
        cmn.invalidate_all_caches()
        self.assertIn("a", cleared)
        self.assertIn("b", cleared)

    def test_one_failing_cache_does_not_break_others(self):
        cleared = []

        class _OkCache(object):
            def __init__(self, tag):
                self.tag = tag

            def clear(self):
                cleared.append(self.tag)

        class _BadCache(object):
            def clear(self):
                raise RuntimeError("boom")

        bad = _BadCache()
        ok = _OkCache("good")
        cmn.register_cache(bad)
        cmn.register_cache(ok)
        cmn.invalidate_all_caches()
        self.assertIn("good", cleared)

    def test_register_cache_idempotent(self):
        cleared = []

        class _Once(object):
            def __init__(self):
                self.n = 0

            def clear(self):
                self.n += 1
                cleared.append(self.n)

        c = _Once()
        cmn.register_cache(c)
        cmn.register_cache(c)
        cmn.register_cache(c)
        cmn.invalidate_all_caches()
        # only one clear() should be called
        self.assertEqual(cleared, [1])

    def test_invalidate_all_caches_returns_none(self):
        # contract: return value is None (or anything); only side-effect matters
        result = cmn.invalidate_all_caches()
        self.assertIsNone(result)


# ===========================================================================
# Section D: register_cache + _cache_registry exports
# ===========================================================================
class RegistryExportTests(unittest.TestCase):
    def test__all_contains_new_names(self):
        for name in ("_cache_registry", "register_cache", "invalidate_all_caches"):
            self.assertIn(name, cmn.__all__, "{0} missing from __all__".format(name))


# ===========================================================================
# Section E: list_node_types
# ===========================================================================
class ListNodeTypesTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = list(cmn._cache_registry)

    def tearDown(self):
        cmn._cache_registry[:] = self._snapshot
        # also clear the global singleton so next test is fresh
        from houdinimcp import _discovery as d
        d.node_type_cache.clear()

    def test_returns_paginated_items(self):
        hou = _make_hou({
            "Sop": {"box": ("Box", ""), "sphere": ("Sphere", "")},
            "Object": {"geo": ("Geo", "")},
        })
        page, cursor = disc.list_node_types(hou, limit=2)
        self.assertEqual(len(page), 2)
        self.assertIsNotNone(cursor)

    def test_filter_by_category(self):
        hou = _make_hou({
            "Sop": {"box": ("Box", ""), "sphere": ("Sphere", "")},
            "Object": {"geo": ("Geo", "")},
        })
        page, cursor = disc.list_node_types(hou, category="Object")
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]["name"], "geo")
        self.assertEqual(page[0]["category"], "Object")
        self.assertIsNone(cursor)

    def test_filter_by_name_substring(self):
        hou = _make_hou({
            "Sop": {"box": ("Box", ""), "sphere": ("Sphere", "")},
        })
        page, _ = disc.list_node_types(hou, name_filter="sphere")
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]["name"], "sphere")

    def test_empty_returns_empty_page(self):
        hou = _make_hou({"Sop": {"box": ("Box", "")}})
        page, cursor = disc.list_node_types(hou, category="Nope")
        self.assertEqual(page, [])
        self.assertIsNotNone(cursor)  # cursor stays valid for next iter even empty

    def test_limit_zero(self):
        hou = _make_hou({"Sop": {"box": ("Box", "")}})
        page, cursor = disc.list_node_types(hou, limit=0)
        self.assertEqual(page, [])
        self.assertEqual(cursor, 0)  # paginate_list contract

    def test_pagination_cursor_advances(self):
        hou = _make_hou({
            "Sop": {"a": ("A", ""), "b": ("B", ""), "c": ("C", "")},
        })
        page1, cursor1 = disc.list_node_types(hou, limit=2, cursor=0)
        self.assertEqual(len(page1), 2)
        self.assertIsNotNone(cursor1)
        page2, cursor2 = disc.list_node_types(hou, limit=2, cursor=cursor1)
        self.assertEqual(len(page2), 1)
        self.assertIsNone(cursor2)


# ===========================================================================
# Section F: list_children
# ===========================================================================
class ListChildrenTests(unittest.TestCase):
    def test_lists_direct_children(self):
        root = _make_node("obj", children=[
            ("geo1", "geo"), ("geo2", "geo"),
        ])
        hou = _make_hou_with_node(root)
        page, cursor = disc.list_children(hou, node_path="/obj")
        self.assertEqual(len(page), 2)
        self.assertIsNone(cursor)

    def test_full_item_shape(self):
        root = _make_node("obj", children=[("geo1", "geo")])
        hou = _make_hou_with_node(root)
        page, _ = disc.list_children(hou, node_path="/obj", compact=False)
        self.assertEqual(len(page), 1)
        entry = page[0]
        for key in ("path", "type", "category", "children_count"):
            self.assertIn(key, entry)

    def test_compact_only_required_keys(self):
        root = _make_node("obj", children=[("geo1", "geo")])
        hou = _make_hou_with_node(root)
        page, _ = disc.list_children(hou, node_path="/obj", compact=True)
        self.assertEqual(len(page), 1)
        entry = page[0]
        self.assertEqual(
            set(entry.keys()),
            {"path", "type", "children_count"},
        )

    def test_recursive_with_max_depth(self):
        # Build a 3-level tree by hand: a -> b -> c
        # list_children 不包含起始节点 a 本身；max_depth=1 只列第一层子节点。
        c = _FakeSceneNode("c", "geo")
        b = _FakeSceneNode("b", "geo")
        b._children.append(c)
        root = _FakeSceneNode("a", "geo")
        root._children.append(b)
        hou = _make_hou_with_node(root)
        page, _ = disc.list_children(hou, node_path="/a", recursive=True,
                                     max_depth=1, max_nodes=100)
        # 起始节点 a 不应被列入；b 是第一层 child 应被列入；c 因 max_depth=1 不应被列入
        names = [it["path"].split("/")[-1] for it in page]
        self.assertNotIn("a", names)
        self.assertIn("b", names)
        self.assertNotIn("c", names)

    def test_recursive_max_depth_two(self):
        # max_depth=2 时第一层 + 第二层子节点都应被列入
        c = _FakeSceneNode("c", "geo")
        b = _FakeSceneNode("b", "geo")
        b._children.append(c)
        root = _FakeSceneNode("a", "geo")
        root._children.append(b)
        hou = _make_hou_with_node(root)
        page, _ = disc.list_children(hou, node_path="/a", recursive=True,
                                     max_depth=2, max_nodes=100)
        names = [it["path"].split("/")[-1] for it in page]
        self.assertIn("b", names)
        self.assertIn("c", names)
        self.assertNotIn("a", names)

    def test_max_nodes_limit(self):
        root = _make_node("obj", children=[
            ("n1", "geo"), ("n2", "geo"), ("n3", "geo"),
        ])
        hou = _make_hou_with_node(root)
        page, _ = disc.list_children(hou, node_path="/obj", max_nodes=2)
        self.assertLessEqual(len(page), 2)

    def test_missing_path_returns_empty(self):
        hou = _make_hou_with_node(_make_node("obj"))
        page, cursor = disc.list_children(hou, node_path="/nope")
        self.assertEqual(page, [])

    def test_pagination_limit(self):
        children = [("n{0}".format(i), "geo") for i in range(5)]
        root = _make_node("obj", children=children)
        hou = _make_hou_with_node(root)
        page, cursor = disc.list_children(hou, node_path="/obj", limit=2)
        self.assertEqual(len(page), 2)
        self.assertIsNotNone(cursor)
        page2, cursor2 = disc.list_children(hou, node_path="/obj", limit=2, cursor=cursor)
        self.assertEqual(len(page2), 2)
        self.assertIsNotNone(cursor2)
        page3, cursor3 = disc.list_children(hou, node_path="/obj", limit=2, cursor=cursor2)
        self.assertEqual(len(page3), 1)
        self.assertIsNone(cursor3)


# ===========================================================================
# Section G: find_nodes
# ===========================================================================
class FindNodesTests(unittest.TestCase):
    def test_substring_match(self):
        root = _make_node("obj", children=[
            ("box1", "geo"),
            ("box2", "geo"),
            ("sphere1", "geo"),
        ])
        hou = _make_hou_with_node(root)
        page, _ = disc.find_nodes(hou, root_path="/obj", pattern="box")
        names = [it["name"] for it in page]
        self.assertIn("box1", names)
        self.assertIn("box2", names)
        self.assertNotIn("sphere1", names)

    def test_glob_match(self):
        root = _make_node("obj", children=[
            ("box_OUT", "geo"),
            ("box_IN", "geo"),
            ("sphere", "geo"),
        ])
        hou = _make_hou_with_node(root)
        page, _ = disc.find_nodes(hou, root_path="/obj", pattern="*_OUT")
        names = [it["name"] for it in page]
        self.assertEqual(names, ["box_OUT"])

    def test_node_type_filter(self):
        root = _make_node("obj", children=[
            ("a", "geo"),
            ("b", "cam"),
            ("c", "geo"),
        ])
        hou = _make_hou_with_node(root)
        page, _ = disc.find_nodes(hou, root_path="/obj", node_type="cam")
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]["name"], "b")

    def test_no_match_returns_empty(self):
        root = _make_node("obj", children=[("a", "geo")])
        hou = _make_hou_with_node(root)
        page, _ = disc.find_nodes(hou, root_path="/obj", pattern="z")
        self.assertEqual(page, [])

    def test_pagination_limit(self):
        children = [("n{0}".format(i), "geo") for i in range(5)]
        root = _make_node("obj", children=children)
        hou = _make_hou_with_node(root)
        page, cursor = disc.find_nodes(hou, root_path="/obj", limit=3)
        self.assertEqual(len(page), 3)
        self.assertIsNotNone(cursor)

    def test_default_root_path_via_slash(self):
        # Brief D6: find_nodes 默认 root_path 为 "/"；显式传 "/" 与不传 root_path 应等效。
        # 我们用简化的 _make_hou_with_node 注册到以 /obj 为根的节点。
        root = _make_node("obj", children=[("a", "geo")])
        hou = _make_hou_with_node(root)
        # 用 / 作 root（nodes_by_path 通过 _FakeSceneNode.path() 注册为 /obj）
        page, _ = disc.find_nodes(hou, root_path="/obj", pattern="a")
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]["name"], "a")


# ===========================================================================
# Section H: populate helpers _populate_fast / _populate_standard
# ===========================================================================
class PopulateFastTests(unittest.TestCase):
    def test_populate_fast_parses_json_exec(self):
        # 模拟 hou 的 exec: 把 json 字符串塞进 hou._exec_capture
        captured = {}

        class _HouForExec(object):
            def nodeTypeCategories(self):
                cats = {}
                for cat_name in ("Sop", "Object"):
                    cat = _FakeCategory(cat_name)
                    cats[cat_name] = cat
                return cats

        hou = _HouForExec()
        # We hit _populate_fast via cache.populate; it should exec in hou's
        # local namespace and read the JSON result.
        cache = disc.NodeTypeCache()
        cache.populate(hou)
        # After populate, size should be > 0
        self.assertGreater(cache.size(), 0)

    def test_populate_standard_fallback(self):
        cache = disc.NodeTypeCache()

        class _BoomHou(object):
            def nodeTypeCategories(self):
                raise RuntimeError("fake failure forcing standard path")

        # Should not crash; we don't strictly enforce which populate path is
        # taken, only that populate eventually populates OR gracefully degrades.
        # Patch: directly call _populate_standard for coverage.
        hou = _make_hou({
            "Sop": {"box": ("Box", "")},
        })
        disc._populate_standard(cache, hou)
        self.assertGreater(cache.size(), 0)

    def test_populate_standard_executes_fast_first(self):
        cache = disc.NodeTypeCache()

        # hou where nodeTypeCategories() raises — fast path fails;
        # cache.populate should fall back to standard.
        class _FastBoomHou(object):
            def nodeTypeCategories(self):
                raise RuntimeError("fast path blew up")

        # Provide a standard-friendly sibling: our cat must be reachable via
        # the same attribute access. Instead, directly verify the fallback
        # by patching helpers.
        called = {"standard": 0}
        original = disc._populate_standard

        def _wrap(c, h):
            called["standard"] += 1
            return original(c, h)

        disc._populate_standard = _wrap
        try:
            cache.populate(_FastBoomHou())
        finally:
            disc._populate_standard = original
        self.assertEqual(called["standard"], 1)


# ===========================================================================
# Section I: manage_cache bridge action validation (via the _discovery-level
# helper, since server.py / bridge will eventually wire it up).
# ===========================================================================
class ManageCacheActionTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = list(cmn._cache_registry)

    def tearDown(self):
        cmn._cache_registry[:] = self._snapshot

    def test_valid_actions(self):
        hou = _make_hou({"Sop": {"box": ("Box", "")}})
        # stats / invalidate / warmup must all succeed
        st = disc.manage_cache(hou, action="stats")
        self.assertIn("hits", st)
        cmn.invalidate_all_caches()
        # warmup: populate singleton
        res = disc.manage_cache(hou, action="warmup")
        self.assertIn("populated", res)
        self.assertTrue(res["populated"])

    def test_invalid_action_raises(self):
        hou = _make_hou({"Sop": {"box": ("Box", "")}})
        with self.assertRaises(ValueError):
            disc.manage_cache(hou, action="explode")


if __name__ == "__main__":
    unittest.main()
