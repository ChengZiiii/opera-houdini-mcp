"""知识库写路径并发防护回归（feat-mcp-round2-hardening §3）。

覆盖 delta spec「知识库写路径并发防护」三个 Scenario：
- per-root 双层锁：多线程并发 save_lesson / 同指纹累积 / save_recipe 追加
  → N 条结果全部存在、无同 id 互覆、strength == 期望递增值、块数无丢失。
- id 撞号零覆盖：目标文件名被不可解析的既有文件占用 → 独占创建撞号
  重算 id，既有文件字节不变。
- 锁降级：``.locks`` 目录不可用（同名普通文件占位）/ 跨进程锁获取注入
  失败 → 写入仍成功且有降级日志；获取失败重试次数有界（== 尝试上限）。

测试基建：``HOUDINI_MCP_HOME`` 指向 TemporaryDirectory（env 成对恢复，
绝不写真实 ~/.opera-houdini-mcp）；纯文件操作 + 线程，零 hou / 零网络。
probe_mode 显式优先（include_hda_internals 不再无条件覆盖）的 server 侧
回归见 test_workflow_capture.py（fake hou handler 级测试）。
"""

import contextlib
import datetime
import io
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _lessons  # noqa: E402
import _best_practices  # noqa: E402


def _lesson_fields(symptom, title="t"):
    return {
        "title": title,
        "category": "concurrency",
        "severity": "medium",
        "affected_versions": "H21",
        "source": "pytest",
        "advisory": True,
        "problem": "p",
        "symptom": symptom,
        "fix": "f",
    }


def _recipe_fields(idx):
    return {
        "title": "recipe-{0}".format(idx),
        "category": "concurrency",
        "severity": "low",
        "affected_versions": "H21",
        "problem": "p {0}".format(idx),
        "symptom": "s {0}".format(idx),
        "fix": "f {0}".format(idx),
    }


class LessonsConcurrencyFixture(unittest.TestCase):
    """HOUDINI_MCP_HOME → tmp（env 成对恢复）+ personal root helper。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.root = os.path.join(self.base, _lessons.KNOWLEDGE_DIRNAME)
        self._old_home = os.environ.get("HOUDINI_MCP_HOME")
        os.environ["HOUDINI_MCP_HOME"] = self.base

        def _restore_home():
            if self._old_home is None:
                os.environ.pop("HOUDINI_MCP_HOME", None)
            else:
                os.environ["HOUDINI_MCP_HOME"] = self._old_home
        self.addCleanup(_restore_home)

    def _lessons_files(self):
        dir_path = os.path.join(self.root, _lessons.LESSONS_DIRNAME)
        if not os.path.isdir(dir_path):
            return []
        return sorted(f for f in os.listdir(dir_path) if f.endswith(".md"))


# ---------------------------------------------------------------------------
# Scenario: per-root 双层锁 —— 并发 save / 累积 / recipe 追加
# ---------------------------------------------------------------------------
class ConcurrentSaveLessonTests(LessonsConcurrencyFixture):

    def test_concurrent_distinct_saves_all_present_unique_ids(self):
        threads_n, per_thread = 8, 3
        total = threads_n * per_thread
        barrier = threading.Barrier(threads_n)
        results = [[] for _ in range(threads_n)]
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=10)
                for j in range(per_thread):
                    results[i].append(_lessons.save_lesson(
                        self.root, _lesson_fields("symptom {0}-{1}".format(i, j))))
            except Exception as exc:  # noqa: BLE001 —— 收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        ids = [lesson["id"] for chunk in results for lesson in chunk]
        self.assertEqual(len(ids), total)
        self.assertEqual(len(set(ids)), total, "并发 save 出现同 id 互覆")

        lessons, parse_errors = _lessons.load_root_lessons(self.root)
        self.assertEqual(parse_errors, {})
        self.assertEqual(len(lessons), total)
        self.assertEqual(sorted(l["id"] for l in lessons), sorted(ids))
        self.assertEqual(len(self._lessons_files()), total)

    def test_concurrent_same_fingerprint_single_file_strength_accumulates(self):
        n = 8
        barrier = threading.Barrier(n)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=10)
                _lessons.save_lesson(self.root, _lesson_fields("same boom"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        # 首个线程创建（strength=1），其余 n-1 个并发触发 _bump_strength
        lessons, parse_errors = _lessons.load_root_lessons(self.root)
        self.assertEqual(parse_errors, {})
        self.assertEqual(len(lessons), 1, "同指纹并发只允许一个文件")
        self.assertEqual(lessons[0]["strength"], n)
        self.assertEqual(len(self._lessons_files()), 1)

    def test_concurrent_record_error_event_count_accumulates(self):
        n = 6
        barrier = threading.Barrier(n)
        outcomes = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=10)
                outcomes.append(_lessons.record_error_event(
                    self.root, "tool_x", "err_x", "boom shared message"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, [True] * n)

        records, bad = _lessons._read_inbox(self.root)
        self.assertEqual(bad, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1]["count"], n)
        # count 达阈值 3 → 恰好晋升一个 draft 骨架（幂等）
        lessons, _errs = _lessons.load_root_lessons(self.root)
        promoted = [l for l in lessons if l["source"] == "inbox-auto"]
        self.assertEqual(len(promoted), 1)


class ConcurrentSaveRecipeTests(LessonsConcurrencyFixture):

    def test_concurrent_appends_no_block_loss(self):
        n = 8
        barrier = threading.Barrier(n)
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=10)
                _lessons.save_recipe(self.root, _recipe_fields(i))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        text = _lessons._read_text(_lessons.recipes_path(self.root))
        entries = _best_practices.parse_best_practices(text)
        self.assertEqual(len(entries), n, "并发追加出现块丢失")
        ids = [entry["id"] for entry in entries]
        self.assertEqual(len(set(ids)), n)
        symptoms = {entry["symptom"] for entry in entries}
        self.assertEqual(symptoms,
                         {"s {0}".format(i) for i in range(n)})

    def test_concurrent_append_and_update_same_root(self):
        # 追加与原地更新混跑：不丢失块、更新落在正确块上
        seed = _lessons.save_recipe(self.root, _recipe_fields(99))
        n = 6
        barrier = threading.Barrier(n + 1)  # n 个追加 + 1 个原地更新
        errors = []

        def appender(i):
            try:
                barrier.wait(timeout=10)
                _lessons.save_recipe(self.root, _recipe_fields(i))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def updater():
            try:
                barrier.wait(timeout=10)
                _lessons.save_recipe(
                    self.root, _recipe_fields(99), recipe_id=seed["id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=appender, args=(i,))
                   for i in range(n)]
        threads.append(threading.Thread(target=updater))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        text = _lessons._read_text(_lessons.recipes_path(self.root))
        entries = _best_practices.parse_best_practices(text)
        self.assertEqual(len(entries), n + 1)
        ids = [entry["id"] for entry in entries]
        self.assertEqual(len(set(ids)), n + 1)
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(by_id[seed["id"]]["symptom"], "s 99")


# ---------------------------------------------------------------------------
# Scenario: id 撞号零覆盖（O_CREAT|O_EXCL + 有界重算）
# ---------------------------------------------------------------------------
class IdCollisionZeroOverwriteTests(LessonsConcurrencyFixture):

    def test_next_id_occupied_by_unparseable_file_recomputes(self):
        _lessons.save_lesson(self.root, _lesson_fields("seed symptom"))
        day = datetime.datetime.now().strftime("%Y%m%d")
        lessons_dir = os.path.join(self.root, _lessons.LESSONS_DIRNAME)
        # existing 只含已解析成功的 lesson（-001）→ 下一个分配 -002；
        # 预放一个不可解析的同名文件占用 -002（模拟锁外进程 / 坏文件）
        occupied = "L-{0}-002".format(day)
        occupied_path = os.path.join(lessons_dir, occupied + ".md")
        with open(occupied_path, "w", encoding="utf-8") as handle:
            handle.write("junk — not a lesson")

        lesson = _lessons.save_lesson(self.root, _lesson_fields("real two"))
        self.assertEqual(lesson["id"], "L-{0}-003".format(day))
        # 既有占位文件字节不变（MUST NOT 覆盖）
        with open(occupied_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "junk — not a lesson")
        # 全部可解析（占位坏文件逐文件报错，不拖垮 root）
        lessons, parse_errors = _lessons.load_root_lessons(self.root)
        self.assertEqual(len(lessons), 2)
        self.assertIn(occupied + ".md", parse_errors)

    def test_exhausted_retries_raise_structured_error(self):
        day = datetime.datetime.now().strftime("%Y%m%d")
        lessons_dir = os.path.join(self.root, _lessons.LESSONS_DIRNAME)
        os.makedirs(lessons_dir)
        # 占死 -001..-005 + 一个合法 id 分配基线（-001 已占 → next 从 002
        # 起逐个撞号）；6 = 1 次首发 + 5 次重算全数占满
        for num in range(1, 7):
            with open(os.path.join(
                    lessons_dir, "L-{0}-{1:03d}.md".format(day, num)),
                    "w", encoding="utf-8") as handle:
                handle.write("junk")

        with self.assertRaises(_lessons.LessonsError) as ctx:
            _lessons.save_lesson(self.root, _lesson_fields("anything"))
        self.assertEqual(ctx.exception.code, "ls_write_error")
        self.assertIn("撞号", ctx.exception.message)
        # 无任何文件被覆盖
        for num in range(1, 7):
            with open(os.path.join(
                    lessons_dir, "L-{0}-{1:03d}.md".format(day, num)),
                    "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "junk")


# ---------------------------------------------------------------------------
# Scenario: 锁降级（best-effort，写入不中断）
# ---------------------------------------------------------------------------
class LockDegradationTests(LessonsConcurrencyFixture):

    def test_locks_dir_unavailable_degrades_and_write_succeeds(self):
        # .locks 被同名普通文件占位 → makedirs 失败 → 降级仅进程内锁 + 日志
        os.makedirs(self.root)
        with open(os.path.join(self.root, _lessons.LOCKS_DIRNAME),
                  "w", encoding="utf-8") as handle:
            handle.write("")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lesson = _lessons.save_lesson(self.root, _lesson_fields("degraded"))
        self.assertEqual(lesson["strength"], 1)
        self.assertEqual(len(self._lessons_files()), 1)
        self.assertIn("降级", buf.getvalue())

        # inbox 路径同样降级成功（record_error_event 永不抛）
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            ok = _lessons.record_error_event(
                self.root, "tool_x", "err_x", "boom in degraded mode")
        self.assertTrue(ok)
        self.assertIn("降级", buf2.getvalue())

    def test_cross_process_acquire_failure_bounded_retries_then_degrades(self):
        calls = []

        def _boom(fd):
            calls.append(fd)
            raise OSError("injected cross-process lock failure")

        original = _lessons._acquire_cross_process_lock
        _lessons._acquire_cross_process_lock = _boom
        self.addCleanup(setattr, _lessons, "_acquire_cross_process_lock",
                        original)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lesson = _lessons.save_lesson(self.root, _lesson_fields("injected"))
        # 重试有界：恰好在尝试上限次后放弃（1 次首发 + attempts-1 次重试）
        self.assertEqual(len(calls), _lessons._CROSS_LOCK_ATTEMPTS)
        self.assertIn("降级", buf.getvalue())
        self.assertEqual(lesson["strength"], 1)
        self.assertEqual(len(self._lessons_files()), 1)
        # 降级后同指纹累积仍正确（进程内锁继续保护读-改-写）
        with contextlib.redirect_stdout(io.StringIO()):
            bumped = _lessons.save_lesson(self.root, _lesson_fields("injected"))
        self.assertEqual(bumped["strength"], 2)


# ---------------------------------------------------------------------------
# 锁基建单元：per-root 粒度（不同 root 不共用锁）
# ---------------------------------------------------------------------------
class LockInfrastructureTests(unittest.TestCase):

    def test_thread_lock_per_root_isolation(self):
        lock_a1 = _lessons._thread_lock_for(r"C:\kb\rootA")
        lock_a2 = _lessons._thread_lock_for(r"C:\kb\rootA")
        lock_b = _lessons._thread_lock_for(r"C:\kb\rootB")
        self.assertIs(lock_a1, lock_a2, "同 root 必须复用同一把进程内锁")
        self.assertIsNot(lock_a1, lock_b, "不同 root 不得串行化到同一把锁")

    def test_cross_lock_filename_unique_per_path(self):
        # 同 basename 不同目录 → 不同锁文件；名字只含安全字符
        name1 = _lessons._cross_lock_filename(r"C:\nas\kb\knowledge")
        name2 = _lessons._cross_lock_filename(r"D:\other\knowledge")
        self.assertNotEqual(name1, name2)
        for name in (name1, name2):
            self.assertTrue(name.endswith(".lock"))
            self.assertRegex(
                name, r"^[A-Za-z0-9_-]+-[0-9a-f]{10}\.lock$")

    def test_root_lock_context_manager_yields_and_releases(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "knowledge")
        with _lessons._root_lock(root):
            lock_file = os.path.join(
                root, _lessons.LOCKS_DIRNAME,
                _lessons._cross_lock_filename(root))
            self.assertTrue(os.path.isfile(lock_file), "锁文件应已创建")
            # 同 root 进程内锁在上下文内被持有（非阻塞 try-acquire 失败）
            thread_lock = _lessons._thread_lock_for(root)
            self.assertFalse(thread_lock.acquire(blocking=False))
        # 退出后进程内锁已释放（可再次非阻塞获取）
        self.assertTrue(_lessons._thread_lock_for(root).acquire(blocking=False))
        _lessons._thread_lock_for(root).release()


if __name__ == "__main__":
    unittest.main()
