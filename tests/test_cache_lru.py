"""perf-mcp-round3 §3 单测：知识库 / RAG 进程内缓存 LRU 淘汰。

覆盖 design.md §3 验收：
- 容量上限淘汰顺序（popitem(last=False)，最旧先走）
- 命中 move_to_end（访问刷新 recency，不被下一轮淘汰）
- mtime/sig 失效优先于 LRU：key 含 mtime/sig，源变化 → 新 key 不命中
  旧条目（无论旧条目多"新"），走重建；旧条目随 LRU 自然淘汰
- 容量边界（capacity=1 只留最新）
- HOUDINI_MCP_CACHE_CAPACITY env 解析矩阵（默认 8 / 合法 / 非整数 / 越界）
- 并发下不炸（多线程 hammer put/get，容量恒不超限）

既有行为回归（mtime reload / stale 降级 / sig 重建）由 test_rag.py
CacheReloadTests 与 test_lessons_search.py 缓存测试继续覆盖（本次改
LRU 后原样通过即回归证明）。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _lessons_search as lssearch  # noqa: E402


def _load_rag():
    """以独立 module name 加载 _rag（模式同 tests/test_rag.py）。"""
    name = "test_cache_lru_isolated._rag"
    if name in sys.modules:
        return sys.modules[name]
    import _common  # noqa: F401  （_rag 顶层 fallback 需要）
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "_rag.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rag = _load_rag()


class _CapacityEnvFixture(unittest.TestCase):
    """HOUDINI_MCP_CACHE_CAPACITY save/restore + clear_cache 公共夹具。"""

    def setUp(self):
        self._saved = os.environ.pop("HOUDINI_MCP_CACHE_CAPACITY", None)
        self.addCleanup(self._restore_env)
        lssearch.clear_cache()
        rag.clear_cache()
        self.addCleanup(lssearch.clear_cache)
        self.addCleanup(rag.clear_cache)

    def _restore_env(self):
        if self._saved is None:
            os.environ.pop("HOUDINI_MCP_CACHE_CAPACITY", None)
        else:
            os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = self._saved


class LessonsSearchLruTests(_CapacityEnvFixture):

    def test_eviction_order_oldest_first(self):
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "3"
        for i in range(4):
            lssearch._cache_put(("p", "sig%d" % i), {"n": i})
        # 容量 3：最旧的 sig0 被淘汰
        self.assertIsNone(lssearch._cache_get(("p", "sig0")))
        for i in (1, 2, 3):
            self.assertEqual(lssearch._cache_get(("p", "sig%d" % i))["n"], i)

    def test_hit_refreshes_recency(self):
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "2"
        lssearch._cache_put(("p", "a"), {"n": "a"})
        lssearch._cache_put(("p", "b"), {"n": "b"})
        # 命中 a → a 移尾（最近使用）
        self.assertEqual(lssearch._cache_get(("p", "a"))["n"], "a")
        # 插入 c 超容量 → 淘汰最久未用的 b，a 保留
        lssearch._cache_put(("p", "c"), {"n": "c"})
        self.assertIsNone(lssearch._cache_get(("p", "b")))
        self.assertEqual(lssearch._cache_get(("p", "a"))["n"], "a")
        self.assertEqual(lssearch._cache_get(("p", "c"))["n"], "c")

    def test_capacity_boundary_one_keeps_newest(self):
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "1"
        lssearch._cache_put(("p", "a"), {"n": "a"})
        lssearch._cache_put(("p", "b"), {"n": "b"})
        self.assertIsNone(lssearch._cache_get(("p", "a")))
        self.assertEqual(lssearch._cache_get(("p", "b"))["n"], "b")
        self.assertLessEqual(len(lssearch._CACHE), 1)

    def test_sig_change_misses_old_entry_regardless_of_recency(self):
        # mtime/sig 失效优先于 LRU：key 含 sig，sig 变化 → 不命中旧条目
        # （即使容量无限大、旧条目刚刚写入）
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "128"
        lssearch._cache_put(("p", "sig-v1"), {"n": 1})
        self.assertIsNone(lssearch._cache_get(("p", "sig-v2")))
        # 旧 sig 仍在缓存中（未被误删），但永远不会被新 sig 查询命中
        self.assertEqual(lssearch._cache_get(("p", "sig-v1"))["n"], 1)

    def test_source_sig_sensitive_to_file_mtime(self):
        # 行为级：_root_source_sig 对文件 mtime/内容变化敏感（key 派生料）
        with tempfile.TemporaryDirectory() as tmp:
            # lessons_dir(root) = root/lessons（不是 root/knowledge/lessons）
            lessons_dir = os.path.join(tmp, "lessons")
            os.makedirs(lessons_dir)
            lesson_path = os.path.join(lessons_dir, "L-1.md")
            with open(lesson_path, "w", encoding="utf-8") as handle:
                handle.write("# one\n")
            sig1 = lssearch._root_source_sig(tmp)
            with open(lesson_path, "w", encoding="utf-8") as handle:
                handle.write("# two\n")
            os.utime(lesson_path, ns=(10 ** 18, 10 ** 18))
            sig2 = lssearch._root_source_sig(tmp)
            self.assertNotEqual(sig1, sig2)

    def test_capacity_env_matrix(self):
        # 未设 → 默认 8
        self.assertEqual(lssearch._cache_capacity(), 8)
        # 合法值
        for raw, expected in (("1", 1), ("8", 8), ("128", 128), ("16", 16)):
            os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = raw
            self.assertEqual(lssearch._cache_capacity(), expected, raw)
        # 非整数 / 越界 → 回退 8 + 日志
        for raw in ("abc", "1.5", "", "0", "129", "-3"):
            os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = raw
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                value = lssearch._cache_capacity()
            self.assertEqual(value, 8, raw)
            self.assertTrue(
                "非整数" in captured.getvalue() or "越界" in captured.getvalue(),
                raw)

    def test_concurrent_put_get_no_crash_and_bounded(self):
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "4"
        errors = []

        def _worker(seed):
            try:
                for i in range(300):
                    key = ("p", "sig%d" % ((seed + i) % 12))
                    lssearch._cache_put(key, {"n": i})
                    lssearch._cache_get(key)
            except Exception as exc:  # pragma: no cover - 失败即报
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(s,))
                   for s in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(lssearch._CACHE), 4)


class RagLruTests(_CapacityEnvFixture):

    def test_cache_eviction_order_and_recency(self):
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "3"
        for i in range(4):
            rag._cache_put(("p", i, 1), "index-%d" % i)
        self.assertIsNone(rag._cache_get(("p", 0, 1)))
        for i in (1, 2, 3):
            self.assertEqual(rag._cache_get(("p", i, 1)), "index-%d" % i)
        # 命中 1 后插入新 key → 淘汰 2（最久未用）
        rag._cache_get(("p", 1, 1))
        rag._cache_put(("p", 9, 1), "index-9")
        self.assertIsNone(rag._cache_get(("p", 2, 1)))
        self.assertEqual(rag._cache_get(("p", 1, 1)), "index-1")

    def test_last_good_bounded_by_same_capacity(self):
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "1"
        rag._last_good_put("/a/index.json", "idx-a")
        rag._last_good_put("/b/index.json", "idx-b")
        # 容量 1：/a 的 last-good 被淘汰
        self.assertIsNone(rag._last_good_get("/a/index.json"))
        self.assertEqual(rag._last_good_get("/b/index.json"), "idx-b")
        self.assertLessEqual(len(rag._LAST_GOOD), 1)

    def test_mtime_change_beats_lru_recency(self):
        # 行为级：同一文件 mtime 变化 → 新 key 不命中旧缓存（重新加载），
        # 无论旧条目刚刚命中（recency 最新）。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.v1.json")
            index_json = json.dumps({
                "schema": rag.SCHEMA_NAME,
                "version": rag.SCHEMA_VERSION,
                "built_at": "2026-01-01T00:00:00+00:00",
                "source": "test",
                "document_count": 1,
                "avgdl": 2.0,
                "documents": [
                    {"id": 0, "path": "a.html", "title": "A",
                     "length": 2, "content": "box geometry"}],
                "postings": {"box": [[0, 1]], "geometry": [[0, 1]]},
            }, ensure_ascii=False)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(index_json)
            first = rag.load_index(path)
            self.assertEqual(first["state"], "ok")
            # 刚命中（recency 最新），仍要因 mtime 变化重新加载
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(index_json)
            os.utime(path, ns=(10 ** 18, 10 ** 18))
            second = rag.load_index(path)
            self.assertEqual(second["state"], "ok")
            # 两个 mtime key 并存于 LRU（旧 key 不再被命中）
            self.assertGreaterEqual(len(rag._CACHE), 2)
            self.assertLessEqual(len(rag._CACHE), 8)

    def test_capacity_env_shared_parsing(self):
        self.assertEqual(rag._cache_capacity(), 8)
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "64"
        self.assertEqual(rag._cache_capacity(), 64)
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "999"
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            value = rag._cache_capacity()
        self.assertEqual(value, 8)
        self.assertIn("越界", captured.getvalue())

    def test_concurrent_put_get_no_crash_and_bounded(self):
        os.environ["HOUDINI_MCP_CACHE_CAPACITY"] = "4"
        errors = []

        def _worker(seed):
            try:
                for i in range(300):
                    key = ("/p%d" % ((seed + i) % 12), i, 1)
                    rag._cache_put(key, "v%d" % i)
                    rag._cache_get(key)
                    rag._last_good_put("/p%d" % ((seed + i) % 12), "v")
                    rag._last_good_get("/p%d" % ((seed + i) % 12))
            except Exception as exc:  # pragma: no cover - 失败即报
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(s,))
                   for s in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(rag._CACHE), 4)
        self.assertLessEqual(len(rag._LAST_GOOD), 4)


if __name__ == "__main__":
    unittest.main()
