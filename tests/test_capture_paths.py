"""回归测试 Bug C：_capture_paths.py 临时目录规范 + 7 天清理。

现象（2026-07-21）：
    - 截图 / 渲染产物散落在 C:/temp/MCP_CPU_KARMA_*.jpg / C:/temp/...
      等 unique 路径，无统一目录，磁盘膨胀无清理机制。

修复目标（user spec）：
    - 基础目录：BASE = os.path.join(hou.expandString('$TEMP'), 'houdini_mcp')
    - 每次调用：BASE/<YYYY-MM-DD>/<HHMMSS>_<scene>_<frame>_<engine>.png
    - 失败路径：BASE/<YYYY-MM-DD>/failed/...
    - 启动时扫描 BASE/，删除 > 7 天的子目录
    - helper: _cleanup_old_captures(base_dir, max_age_days=7, now=None)
    - 单元测试覆盖（mock 时间）
    - 不变量：调用方传入的 save_path 仍生效（向后兼容）

本测试纯 stdlib，无 hython 依赖；通过 mock 时间戳与临时目录验证
清理逻辑与路径生成。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_PKG_KEY = "houdinimcp"
_CP_KEY = "houdinimcp._capture_paths"


def _ensure_pkg():
    if _PKG_KEY not in sys.modules:
        import types
        pkg = types.ModuleType(_PKG_KEY)
        pkg.__path__ = [ROOT]
        sys.modules[_PKG_KEY] = pkg


def _load_fresh():
    import importlib.util as _ilu
    if _CP_KEY in sys.modules:
        del sys.modules[_CP_KEY]
    spec = _ilu.spec_from_file_location(_CP_KEY,
                                        os.path.join(ROOT, "_capture_paths.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[_CP_KEY] = mod
    spec.loader.exec_module(mod)
    return mod


class ResolveBaseDirTest(unittest.TestCase):
    """BASE 解析：hou.expandString('$TEMP') 优先，fallback 回退。"""

    @classmethod
    def setUpClass(cls):
        _ensure_pkg()
        cls.cp = _load_fresh()

    def test_hou_temp_used_when_available(self):
        """hou 可用时用 hou.expandString('$TEMP')。"""
        class _Hou:
            def expandString(self, s):
                return "/custom/temp" if s == "$TEMP" else s
        base = self.cp.resolve_base_dir(hou=_Hou())
        self.assertEqual(base, os.path.join("/custom/temp", "houdini_mcp"))

    def test_fallback_when_no_hou(self):
        """无 hou 时回退 os.environ['TEMP']。"""
        base = self.cp.resolve_base_dir(hou=None)
        expected = os.path.join(
            os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
            "houdini_mcp")
        self.assertEqual(base, expected)

    def test_fallback_explicit(self):
        """显式 fallback 参数覆盖环境变量。"""
        base = self.cp.resolve_base_dir(hou=None, fallback="/explicit/temp")
        self.assertEqual(base, os.path.join("/explicit/temp", "houdini_mcp"))

    def test_hou_exception_falls_back(self):
        """hou.expandString 抛异常时静默回退。"""
        class _BrokenHou:
            def expandString(self, s):
                raise RuntimeError("broken")
        base = self.cp.resolve_base_dir(hou=_BrokenHou())
        expected = os.path.join(
            os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
            "houdini_mcp")
        self.assertEqual(base, expected)


class DefaultCapturePathTest(unittest.TestCase):
    """默认路径生成：BASE/<YYYY-MM-DD>/<HHMMSS>_<scene>_<frame>_<engine>.png"""

    @classmethod
    def setUpClass(cls):
        _ensure_pkg()
        cls.cp = _load_fresh()

    def test_path_contains_required_components(self):
        """路径必须含 BASE / 日期目录 / 时间戳 / scene / frame / engine。"""
        tmp = tempfile.mkdtemp()
        try:
            now = time.mktime(time.strptime("2026-07-21 14:30:45",
                                             "%Y-%m-%d %H:%M:%S"))
            # Bug 3 修复后契约变更（2026-07-21）：caller 传 fallback_base 时
            # 必须传已拼好 houdini_mcp 的完整 BASE；不再由 resolve_base_dir
            # 二次拼接 houdini_mcp（避免 /<x>/houdini_mcp/houdini_mcp 双层）。
            # 见 test_capture_paths_h22.py::test_default_capture_path_with_fallback_base_no_double_houdini_mcp
            base = os.path.join(tmp, "houdini_mcp")
            path = self.cp.default_capture_path(
                hou=None, pane_type="SceneViewer", engine="flipbook",
                scene_basename="test.hip", frame=5, now=now,
                fallback_base=base)
            # 路径组件检查
            self.assertTrue(path.startswith(base),
                "路径必须以 <BASE>/houdini_mcp 开头，实际: " + path)
            self.assertIn(os.path.join(base, "2026-07-21"),
                          os.path.dirname(path))
            self.assertTrue(path.endswith(".png"))
            fname = os.path.basename(path)
            # 命名规范：HHMMSS_scene_frame_engine.png
            parts = fname[:-4].split("_")
            self.assertEqual(len(parts), 4,
                "文件名应为 HHMMSS_scene_frame_engine.png，实际: " + fname)
            # 用 time.strftime 计算期望 HHMMSS（避免硬编码 + 时区漂移）
            expected_ts = time.strftime("%H%M%S", time.localtime(now))
            self.assertEqual(parts[0], expected_ts,
                "时间戳应为 {0}，实际: {1}".format(expected_ts, parts[0]))
            self.assertEqual(parts[1], "test")
            self.assertEqual(parts[2], "5")
            self.assertEqual(parts[3], "flipbook")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_hip_suffix_stripped(self):
        """.hip / .hipnc 后缀必须剥离。"""
        tmp = tempfile.mkdtemp()
        try:
            path = self.cp.default_capture_path(
                hou=None, engine="flipbook",
                scene_basename="myscene.hipnc", frame=1,
                fallback_base=tmp)
            self.assertIn("myscene", os.path.basename(path))
            self.assertNotIn(".hip", os.path.basename(path))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_scene_basename(self):
        """scene_basename=None 默认 untitled。"""
        tmp = tempfile.mkdtemp()
        try:
            path = self.cp.default_capture_path(
                hou=None, engine="qt_grab", frame=1,
                fallback_base=tmp)
            self.assertIn("untitled", os.path.basename(path))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unsafe_chars_in_scene_replaced(self):
        """scene basename 中的不安全字符替换为下划线。"""
        tmp = tempfile.mkdtemp()
        try:
            path = self.cp.default_capture_path(
                hou=None, engine="flipbook",
                scene_basename="my scene!*", frame=1,
                fallback_base=tmp)
            fname = os.path.basename(path)
            self.assertIn("my_scene__", fname)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_directory_auto_created(self):
        """<BASE>/<YYYY-MM-DD>/ 目录必须自动创建。"""
        tmp = tempfile.mkdtemp()
        try:
            path = self.cp.default_capture_path(
                hou=None, engine="flipbook", scene_basename="s",
                frame=1, fallback_base=tmp)
            self.assertTrue(os.path.isdir(os.path.dirname(path)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _safe_rmtree(self, path):
        """Helper: shutil.rmtree may not be imported in test module."""
        try:
            shutil.rmtree(path, ignore_errors=True)
        except NameError:
            import shutil as _s
            _s.rmtree(path, ignore_errors=True)

    def test_failed_path_under_failed_subdir(self):
        """失败路径必须在 <BASE>/<YYYY-MM-DD>/failed/ 下。"""
        tmp = tempfile.mkdtemp()
        try:
            path = self.cp.failed_capture_path(
                hou=None, engine="flipbook", scene_basename="s",
                frame=1, fallback_base=tmp)
            self.assertIn(os.sep + "failed" + os.sep, path)
            self.assertTrue(path.endswith("_error.png"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CleanupOldCapturesTest(unittest.TestCase):
    """_cleanup_old_captures 删除 > max_age_days 天的子目录。"""

    @classmethod
    def setUpClass(cls):
        _ensure_pkg()
        cls.cp = _load_fresh()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_date_dir(self, name, mtime):
        """在 tmp 下建一个子目录并强制设置 mtime。"""
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        # 先建文件（Windows 上建文件会重置父目录 mtime）
        with open(os.path.join(d, "test.png"), "w") as f:
            f.write("fake")
        # 再设 mtime（必须最后一步，否则 Windows 会被覆盖）
        os.utime(d, (mtime, mtime))
        return d

    def test_old_dirs_deleted_new_dirs_kept(self):
        """老目录删除、新目录保留。"""
        # 现在时间 = 2026-07-21 00:00:00
        now = time.mktime(time.strptime("2026-07-21 00:00:00",
                                        "%Y-%m-%d %H:%M:%S"))
        # 10 天前（应被删）
        old_ts = now - 10 * 86400
        # 3 天前（应保留）
        new_ts = now - 3 * 86400
        # 1 天前（应保留）
        newer_ts = now - 1 * 86400

        old_dir = self._make_date_dir("2026-07-11", old_ts)
        new_dir = self._make_date_dir("2026-07-18", new_ts)
        newer_dir = self._make_date_dir("2026-07-20", newer_ts)

        result = self.cp.cleanup_old_captures(self.tmp, max_age_days=7, now=now)

        # 旧目录已被删（含其下文件）
        self.assertFalse(os.path.exists(old_dir),
            "10 天前的目录应被删除")
        # 新目录保留
        self.assertTrue(os.path.exists(new_dir),
            "3 天前的目录应保留")
        self.assertTrue(os.path.exists(newer_dir),
            "1 天前的目录应保留")
        # 统计
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["kept"], 2)
        self.assertEqual(result["errors"], [])

    def test_max_age_days_zero_deletes_everything_old(self):
        """max_age_days=0 → 任何早于 now 的目录都删除。"""
        now = 1000000.0
        old_ts = now - 1  # 1 秒前
        self._make_date_dir("old", old_ts)
        result = self.cp.cleanup_old_captures(self.tmp, max_age_days=0, now=now)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["kept"], 0)

    def test_nonexistent_base_dir_returns_zeros(self):
        """不存在的 base_dir 不抛异常，返回全 0。"""
        result = self.cp.cleanup_old_captures(
            os.path.join(self.tmp, "nonexistent"), max_age_days=7)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["kept"], 0)
        self.assertEqual(result["scanned"], 0)

    def test_files_in_base_not_deleted(self):
        """base_dir 下的文件（非子目录）不删。"""
        now = time.time()
        # 在 tmp 根目录建一个 old 文件
        old_file = os.path.join(self.tmp, "old_log.txt")
        with open(old_file, "w") as f:
            f.write("x")
        os.utime(old_file, (now - 30 * 86400, now - 30 * 86400))
        # 再建一个老目录
        old_dir = self._make_date_dir("2026-06-01", now - 30 * 86400)

        result = self.cp.cleanup_old_captures(self.tmp, max_age_days=7, now=now)
        self.assertTrue(os.path.exists(old_file),
            "文件（非日期子目录）不应被删")
        self.assertFalse(os.path.exists(old_dir),
            "30 天前的日期子目录应被删")
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["kept"], 1)

    def test_failed_subdir_cleaned_with_parent(self):
        """failed/ 子目录随父日期目录一并删除。"""
        now = 1000000.0
        old_ts = now - 30 * 86400
        old_dir = self._make_date_dir("2026-06-15", old_ts)
        failed_subdir = os.path.join(old_dir, "failed")
        os.makedirs(failed_subdir, exist_ok=True)
        with open(os.path.join(failed_subdir, "err.png"), "w") as f:
            f.write("x")
        # Windows 建子目录 + 文件会重置父目录 mtime（与 _make_date_dir
        # 同坑），必须最后一步再 utime 父目录才能保留 old_ts
        os.utime(old_dir, (old_ts, old_ts))

        result = self.cp.cleanup_old_captures(self.tmp, max_age_days=7, now=now)
        self.assertFalse(os.path.exists(failed_subdir),
            "failed/ 子目录应随父日期目录一并删除")
        self.assertEqual(result["deleted"], 1)


if __name__ == "__main__":
    unittest.main()