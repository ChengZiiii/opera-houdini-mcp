"""versioned-rag-index task 3.6：bridge 语境全链路集成测试。

review 新增测试组（design D8）：**hython 语境只能验证 2.x，不得作为本
组通过依据**——hython 有 hou 有 HFS，恰是生产消费方（无 hou 的 bridge）
的对立面。本文件在 fork ``.venv``（嵌入式 Python 3.12，无 hou）语境下
走完整链路：

    连接（真 TCP mock server，4 字节长度前缀帧）
    → 版本路由（get_scene_info 应答 houdini_version + hfs_path）
    → 封存旧 flat 索引（真改名）
    → 自动构建（**真子进程**：venv python + build_rag_index.py，
       CREATE_NO_WINDOW，无 stub spawn）
    → 预热 / re-preheat（daemon 线程 + watcher 轮询）
    → search_docs / get_doc 正常检索

环境纪律：
- 清除 ``HFS`` / ``HOUDINI_USER_PREF_DIR`` 继承 env（bridge 进程没有它们）
- hython 语境 skip（见上）；socket 全走 127.0.0.1 临时端口，零 MCP 实机
- images.zip 排除、双版本共存（两版本目录互不干扰）在本组一并覆盖
"""

import importlib.util
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if "hou" in sys.modules and hasattr(sys.modules["hou"], "__file__"):
    raise unittest.SkipTest(
        "bridge-context suite must run without real hou (hython context "
        "is not valid evidence for this group; run under fork .venv)")

import _rag as rag  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lc = _load("test_raglc_e2e._rag_lifecycle",
           os.path.join(ROOT, "_rag_lifecycle.py"))

_WIKI_TEMPLATE = (
    "#type: node\n#context: {context}\n= {title} =\n"
    "\"\"\"{summary}.\"\"\"\n{body}\n"
)


def _make_help_source(root):
    """造 help 源：nodes.zip + solaris.zip + images.zip（应被排除）。"""
    help_dir = os.path.join(root, "hfs", "houdini", "help")
    os.makedirs(help_dir)
    entries = [
        ("nodes.zip", "sop", "attribwrangle", "Attribute Wrangle",
         "run vex snippets per point", "point count loop vex code"),
        ("nodes.zip", "sop", "pyrosource", "Pyro Source",
         "emit smoke source", "pyro sourcing volume"),
        ("solaris.zip", "lop", "karma", "Karma Renderer",
         "husk render settings", "karma sampling pixelfilter"),
    ]
    for zip_name, folder, stem, title, summary, body in entries:
        with zipfile.ZipFile(os.path.join(help_dir, zip_name), "a") as zf:
            zf.writestr(
                "%s/%s.txt" % (folder, stem),
                _WIKI_TEMPLATE.format(context=folder, title=title,
                                      summary=summary, body=body))
    with zipfile.ZipFile(os.path.join(help_dir, "images.zip"), "w") as zf:
        zf.writestr("icons/box.txt", "binary payload placeholder")
    return help_dir


class _MockHoudiniServer(object):
    """127.0.0.1 临时端口的单连接 mock server（长度前缀 JSON 帧协议，
    与生产 9876 server 同帧格式）。应答 get_scene_info。"""

    def __init__(self, version, hfs_path):
        self.version = version
        self.hfs_path = hfs_path
        self.commands_seen = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name="mock-houdini-server")
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(conn)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn):
        request = self._read_frame(conn)
        if request is None:
            return
        self.commands_seen.append(request.get("type"))
        result = {
            "houdini_version": self.version,
            "hfs_path": self.hfs_path,
            "name": "unit.hipnc",
        }
        self._write_frame(conn, {"status": "success", "result": result})

    @staticmethod
    def _read_frame(conn):
        header = b""
        while len(header) < 4:
            chunk = conn.recv(4 - len(header))
            if not chunk:
                return None
            header += chunk
        (length,) = struct.unpack(">I", header)
        payload = b""
        while len(payload) < length:
            chunk = conn.recv(min(length - len(payload), 65536))
            if not chunk:
                return None
            payload += chunk
        return json.loads(payload.decode("utf-8"))

    @staticmethod
    def _write_frame(conn, obj):
        data = json.dumps(obj).encode("utf-8")
        conn.sendall(struct.pack(">I", len(data)) + data)

    def tcp_call(self, cmd_type, params):
        """bridge ``_houdini_call`` 形态的回调：一次连接一问一答。"""
        with socket.create_connection(("127.0.0.1", self.port),
                                       timeout=5.0) as conn:
            self._write_frame(conn, {"type": cmd_type,
                                     "params": params or {}})
            response = self._read_frame(conn)
        if response is None:
            return {"status": "error", "message": "mock closed"}
        return response

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class BridgeContextE2ETests(unittest.TestCase):
    """全链路：路由 → 封存 → 真子进程构建 → 预热 → 检索。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.rag_root = os.path.join(self.home, ".opera-houdini-mcp", "rag")
        self.help_source = _make_help_source(self.tmp.name)
        self.hfs_root = os.path.dirname(os.path.dirname(self.help_source))
        # bridge 语境：无 HFS / 无 HOUDINI_USER_PREF_DIR（save/restore）
        self._saved_env = {}
        for key in ("HFS", "HOUDINI_USER_PREF_DIR",
                    "HOUDINI_MCP_RAG_INDEX_DIR"):
            self._saved_env[key] = os.environ.pop(key, None)
        self.addCleanup(self._restore_env)
        patcher = mock.patch("os.path.expanduser",
                             lambda path="", *a, **k: self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        lc.reset_state_for_tests()
        rag.clear_cache()
        self.addCleanup(lc.reset_state_for_tests)
        self.addCleanup(rag.clear_cache)
        self.server = _MockHoudiniServer("21.0.596", self.hfs_root)
        self.addCleanup(self.server.close)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_legacy_flat(self):
        os.makedirs(self.rag_root, exist_ok=True)
        flat = os.path.join(self.rag_root, "index.v1.json")
        with io.open(flat, "w", encoding="utf-8") as handle:
            handle.write('{"schema": "houdinimcp.rag-index", "legacy": true}')
        return flat

    def _wait_search_success(self, query, timeout=60.0):
        """轮询直到 search_docs 成功（构建 + re-preheat 完成的判据）。"""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = rag.search_docs(query)
            if last.get("status") == "success" and last.get("returned", 0):
                return last
            time.sleep(0.25)
        self.fail("search_docs never succeeded: %r" % (last,))

    def test_full_chain_archive_build_preheat_search(self):
        flat = self._write_legacy_flat()
        self.assertEqual("HFS" in os.environ, False,
                         "bridge 语境不得依赖 HFS")

        # 1) 首次 gate：路由 → 封存 → 拉起真构建 → building envelope
        env = lc.pre_tool_gate(tcp_call=self.server.tcp_call)
        self.assertIsNotNone(env)
        self.assertIs(env["building"], True)
        self.assertIn("eta_hint", env)
        # 路由结果
        verdir = os.path.join(self.rag_root, "21.0.596")
        self.assertEqual(
            os.environ.get("HOUDINI_MCP_RAG_INDEX_DIR"), verdir)
        self.assertIn("get_scene_info", self.server.commands_seen)
        # 封存完成（内容保留）
        self.assertFalse(os.path.exists(flat))
        self.assertTrue(os.path.isfile(flat + lc.LEGACY_ARCHIVE_SUFFIX))

        # 2) 构建期间 gate 持续快速返回 building（不阻塞、不重复构建）
        env2 = lc.pre_tool_gate(tcp_call=self.server.tcp_call)
        self.assertIsNotNone(env2)
        self.assertIs(env2.get("building"), True)

        # 3) 等待真子进程完成 + watcher re-preheat + 检索成功
        result = self._wait_search_success("vex snippet point")
        self.assertGreaterEqual(result["returned"], 1)
        top = result["results"][0]
        self.assertEqual(top["path"], "nodes.zip/sop/attribwrangle.txt")

        # 4) 构建产物与状态文件
        self.assertTrue(os.path.isfile(os.path.join(verdir, "index.v1.json")))
        with io.open(os.path.join(verdir, "build.status.json"), "r",
                     encoding="utf-8") as handle:
            status = json.load(handle)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["doc_count"], 3)
        self.assertEqual(sorted(status["zips"]),
                         ["nodes.zip", "solaris.zip"],
                         "images.zip 必须被排除清单挡下")
        self.assertFalse(os.path.exists(
            os.path.join(verdir, "build.lock")), "锁已由子进程清理")

        # 5) get_doc 全文路径（content=正文；title 独立字段）
        doc = rag.get_doc("nodes.zip/sop/attribwrangle.txt")
        self.assertEqual(doc["status"], "success")
        self.assertEqual(doc["title"], "Attribute Wrangle")
        self.assertIn("vex snippets", doc["content"])

        # 6) 正常态：无 building / warming_up 附加字段
        plain = rag.search_docs("karma sampling")
        self.assertEqual(plain["status"], "success")
        self.assertNotIn("building", plain)
        self.assertNotIn("warming_up", plain)
        self.assertEqual(plain["results"][0]["path"],
                         "solaris.zip/lop/karma.txt")

    def test_dual_version_coexistence(self):
        # 版本 A：走全链路构建
        first = lc.pre_tool_gate(tcp_call=self.server.tcp_call)
        self.assertIsNotNone(first)
        self._wait_search_success("vex snippet point")
        verdir_a = os.path.join(self.rag_root, "21.0.596")
        index_a = os.path.join(verdir_a, "index.v1.json")
        stamp_a = os.stat(index_a).st_mtime_ns

        # 版本 B：换 server 版本应答（H22），重置路由状态 + 清 env 模拟
        # **新 bridge 会话**（新进程不继承上一会话自设的 env）
        lc.reset_state_for_tests()
        rag.clear_cache()
        os.environ.pop("HOUDINI_MCP_RAG_INDEX_DIR", None)
        self.server.version = "22.0.100"
        second = lc.pre_tool_gate(tcp_call=self.server.tcp_call)
        self.assertIsNotNone(second)
        self.assertIs(second.get("building"), True)
        verdir_b = os.path.join(self.rag_root, "22.0.100")
        self._wait_search_success("karma sampling")
        index_b = os.path.join(verdir_b, "index.v1.json")

        # 双版本共存互不干扰：B 的构建未动 A 的索引
        self.assertTrue(os.path.isfile(index_a))
        self.assertTrue(os.path.isfile(index_b))
        self.assertEqual(os.stat(index_a).st_mtime_ns, stamp_a,
                         "版本 B 重建不得影响版本 A 目录")


if __name__ == "__main__":
    unittest.main()
