"""versioned-rag-index Phase 1（build 脚本改造）单测。

覆盖 tasks 1.1 / 1.2 / 1.3：
- 1.1 默认 zip 集发现（全部 *.zip 减排除清单 images）+ build.status.json
  字段统一（state/started_at/finished_at/pid/exit_code/error/zips/
  doc_count/source_path）+ 原子推进 building→done
- 1.2 异常 zip 统计：损坏条目计数不中断；整包打不开单列；统计进状态
  文件 zip_entry_failures
- 1.3 --version-dir 版本化输出子目录 + 单段校验（防路径逃逸）

对 H21 help 目录的实跑验证（10,093 docs / 46 zips / images 排除）属
手动实测（apply 记录），不在此文件重复。
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_build():
    """独立 module name 加载 build_rag_index（scripts/ 子目录，flat 布局）。"""
    name = "test_rag_versioned.build_rag_index"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "scripts", "build_rag_index.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_mod = _load_build()

_STATUS_KEYS = {
    "state", "started_at", "finished_at", "pid", "exit_code",
    "error", "zips", "doc_count", "source_path",
}


def _make_zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in entries:
            zf.writestr(name, text)


_WIKI_ONE = (
    "#type: node\n#context: sop\n= Some Node =\n"
    "\"\"\"Summary line.\"\"\"\nBody text here.\n"
)


class _SourceDirMixin(object):
    """临时 help 源目录：nodes.zip + solaris.zip（+ 可选 images.zip）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "help")
        os.makedirs(self.source)
        _make_zip(os.path.join(self.source, "nodes.zip"), [
            ("sop/box.txt", _WIKI_ONE),
            ("sop/sphere.txt", _WIKI_ONE),
        ])
        _make_zip(os.path.join(self.source, "solaris.zip"), [
            ("sop/karmarender.txt", _WIKI_ONE),
        ])

    def tearDown(self):
        self.tmp.cleanup()


class DiscoveryTests(_SourceDirMixin, unittest.TestCase):

    def test_discover_excludes_images(self):
        _make_zip(os.path.join(self.source, "images.zip"),
                  [("img/a.txt", "x")])
        found = build_mod.discover_zip_names(self.source)
        self.assertEqual(found, ["nodes.zip", "solaris.zip"])

    def test_discover_missing_dir_returns_empty(self):
        self.assertEqual(
            build_mod.discover_zip_names(os.path.join(self.tmp.name, "nope")),
            [])

    def test_find_zip_txt_entries_none_uses_discovery(self):
        _make_zip(os.path.join(self.source, "images.zip"),
                  [("img/a.txt", "x")])
        entries = build_mod.find_zip_txt_entries(self.source, None)
        prefixes = set(p.split("/", 1)[0] for p, _, _ in entries)
        self.assertEqual(prefixes, {"nodes.zip", "solaris.zip"})

    def test_parse_zips_none_and_empty_mean_discovery(self):
        self.assertIsNone(build_mod._parse_zips(None))
        self.assertIsNone(build_mod._parse_zips("  "))

    def test_parse_zips_explicit_list(self):
        self.assertEqual(
            build_mod._parse_zips("nodes, vex"), ["nodes", "vex"])


class MainStatusAndVersionDirTests(_SourceDirMixin, unittest.TestCase):

    def setUp(self):
        super(MainStatusAndVersionDirTests, self).setUp()
        self.out = os.path.join(self.tmp.name, "rag")
        self.verdir = os.path.join(self.out, "21.0.596")

    def _status(self):
        with io.open(os.path.join(self.verdir, "build.status.json"),
                     "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_version_dir_output_and_status_done(self):
        code = build_mod.main([
            "--source", self.source, "--output", self.out,
            "--version-dir", "21.0.596"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(
            os.path.join(self.verdir, "index.v1.json")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.out, "index.v1.json")),
            "version-dir must not leak a flat index into the rag root")
        status = self._status()
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["doc_count"], 3)
        self.assertEqual(set(status.keys()) >= _STATUS_KEYS, True)
        self.assertTrue(status["started_at"] and status["finished_at"])
        self.assertEqual(sorted(status["zips"]),
                         ["nodes.zip", "solaris.zip"])
        self.assertEqual(status["error"], None)
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(
            os.path.abspath(status["source_path"]),
            os.path.abspath(self.source))

    def test_flat_output_without_version_dir(self):
        out_flat = os.path.join(self.tmp.name, "flat")
        code = build_mod.main(["--source", self.source, "--output", out_flat])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(
            os.path.join(out_flat, "index.v1.json")))
        self.assertTrue(os.path.isfile(
            os.path.join(out_flat, "build.status.json")))

    def test_index_records_zips_field(self):
        code = build_mod.main([
            "--source", self.source, "--output", self.out,
            "--version-dir", "21.0.596"])
        self.assertEqual(code, 0)
        with io.open(os.path.join(self.verdir, "index.v1.json"),
                     "r", encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(index["zips"], ["nodes.zip", "solaris.zip"])

    def test_bad_version_dir_rejected(self):
        for bad in ("a/b", "a\\b", "..", "."):
            code = build_mod.main([
                "--source", self.source, "--output", self.out,
                "--version-dir", bad])
            self.assertEqual(code, 2, "version dir %r must be rejected" % bad)

    def test_no_source_no_status_file(self):
        code = build_mod.main([
            "--source", os.path.join(self.tmp.name, "absent"),
            "--output", self.out, "--version-dir", "21.0.596"])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.verdir),
                         "no build started → no status dir")

    def test_zero_docs_marks_failed(self):
        # zip 内无 .txt 条目：zip 被发现但 0 doc → failed/4（不发布）
        empty_zip = os.path.join(self.source, "empty.zip")
        _make_zip(empty_zip, [("readme.md", "no txt entries")])
        for name in ("nodes.zip", "solaris.zip"):
            os.remove(os.path.join(self.source, name))
        code = build_mod.main([
            "--source", self.source, "--output", self.out,
            "--version-dir", "21.0.596"])
        self.assertEqual(code, 4)
        status = self._status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["exit_code"], 4)
        self.assertTrue(status["error"])
        self.assertFalse(os.path.exists(
            os.path.join(self.verdir, "index.v1.json")))

    def test_build_lock_removed_on_exit(self):
        # bridge 预建独占锁 → 构建退出契约：锁必须被清理
        os.makedirs(self.verdir, exist_ok=True)
        lock_path = os.path.join(self.verdir, "build.lock")
        with io.open(lock_path, "w", encoding="utf-8") as handle:
            handle.write("")
        code = build_mod.main([
            "--source", self.source, "--output", self.out,
            "--version-dir", "21.0.596"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(lock_path))


class CorruptEntryStatsTests(_SourceDirMixin, unittest.TestCase):

    def _corrupt_zip(self, path):
        """在第二个条目的压缩数据段翻一个字节（坏 CRC/流）。"""
        with io.open(path, "rb") as handle:
            data = handle.read()
        idx = data.find(b"PK\x03\x04", 30)
        self.assertGreater(idx, 0)
        offset = idx + 40
        flipped = data[:offset] + bytes([data[offset] ^ 0xFF]) + data[offset + 1:]
        with io.open(path, "wb") as handle:
            handle.write(flipped)

    def test_corrupt_entry_counted_not_fatal(self):
        self._corrupt_zip(os.path.join(self.source, "nodes.zip"))
        stats = {}
        index = build_mod.build_index(self.source, None, stats=stats)
        # nodes.zip 两个条目一个坏一个好 + solaris.zip 一个 → 2 docs
        self.assertEqual(index["document_count"], 2)
        self.assertEqual(stats["entry_failures"].get("nodes.zip"), 1)
        self.assertEqual(stats["bad_zips"], [])
        self.assertEqual(sorted(stats["indexed_zips"]),
                         ["nodes.zip", "solaris.zip"])

    def test_corrupt_stats_reach_status_file(self):
        self._corrupt_zip(os.path.join(self.source, "nodes.zip"))
        out = os.path.join(self.tmp.name, "rag")
        code = build_mod.main(["--source", self.source, "--output", out])
        self.assertEqual(code, 0)
        with io.open(os.path.join(out, "build.status.json"),
                     "r", encoding="utf-8") as handle:
            status = json.load(handle)
        failures = status.get("zip_entry_failures")
        self.assertEqual(failures["entry_failures"].get("nodes.zip"), 1)

    def test_bad_zip_recorded_when_reopen_fails(self):
        # namelist 成功但重开失败（TOCTOU 形态）：mock 对 nodes.zip 的
        # 第二次 ZipFile 打开（= build_index 的重开）抛 BadZipFile，
        # 验证整包单列、其余 zip 不受影响
        real_zipfile = build_mod.zipfile.ZipFile
        nodes_path = os.path.abspath(os.path.join(self.source, "nodes.zip"))
        open_counts = {}

        class _FlakyZipFile(real_zipfile):

            def __init__(self, *args, **kwargs):
                key = os.path.abspath(str(args[0])) if args else "?"
                open_counts[key] = open_counts.get(key, 0) + 1
                if key == nodes_path and open_counts[key] >= 2:
                    raise zipfile.BadZipFile("flaky reopen")
                real_zipfile.__init__(self, *args, **kwargs)

        original = build_mod.zipfile.ZipFile
        build_mod.zipfile.ZipFile = _FlakyZipFile
        try:
            stats = {}
            index = build_mod.build_index(self.source, None, stats=stats)
        finally:
            build_mod.zipfile.ZipFile = original
        # find 阶段 nodes 打开 1 次成功（条目可列出）；build 阶段重开失败
        # → 整包跳过；solaris 不受影响
        self.assertEqual(stats["bad_zips"], ["nodes.zip"])
        self.assertEqual(stats["entry_failures"], {})
        self.assertEqual(index["document_count"], 1)
        self.assertEqual(sorted(stats["indexed_zips"]), ["solaris.zip"])


if __name__ == "__main__":
    unittest.main()
