"""_hip_parser 的格式、损坏、重复、限额、include_params、response cap 与
真实 golden fixture 比对测试。

覆盖 tasks 5.1-5.6 + golden fixture 验收（4.5 / 6.3 受 H20/H22 缺失阻塞，
见 ``GoldenFixtureTests`` 文档与 change design D7）：

- 5.1 合成 entry：header 字段、TRAILER、section 任意顺序、完整路径关联
- 5.2 重复 entry last-complete-wins、duplicate_entries、截断重复项不覆盖
- 5.3 首个 header 无效（无 partial）/ 后续 header 损坏 / name 截断 / body
      截断 / 缺 trailer
- 5.4 file / entry / single-section / total-section / node / max_depth 限额
- 5.5 未知 section、include_params True/False、可选 save_version
- 5.6 大 partial payload 经 response cap 后结构仍合法

合成 cpio 用 odc 格式（magic 070707、76 字节 header、namesize 含 NUL、
entry 间无 padding、TRAILER!!! 结束），与真实 Houdini .hip 一致。
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES_DIR = os.path.join(HERE, "fixtures", "hip")


def _load_hip():
    name = "test_hip_parser_isolated._hip_parser"
    if name in sys.modules:
        return sys.modules[name]
    import _common  # noqa: F401  确保 flat import 可用（_hip_parser fallback）
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "_hip_parser.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hip = _load_hip()
import _common as cmn  # noqa: E402


# ---------------------------------------------------------------------------
# 合成 cpio 工具
# ---------------------------------------------------------------------------
def make_header(namesize, filesize):
    """构造 76 字节 odc header（magic 070707 + 7×6 dev..rdev + mtime11 +
    namesize6 + filesize11，全 ASCII 八进制）。"""
    return (b"070707"
            + b"000000" * 7
            + b"%011o" % 0          # mtime
            + b"%06o" % namesize     # [59:65]
            + b"%011o" % filesize)   # [65:76]


def make_entry(name, body=b""):
    if isinstance(body, str):
        body = body.encode("latin-1")
    nameb = name.encode("latin-1") + b"\x00"   # namesize 含尾部 NUL
    return make_header(len(nameb), len(body)) + nameb + body


def make_archive(entries, trailer=True):
    out = b""
    for name, body in entries:
        out += make_entry(name, body)
    if trailer:
        out += make_entry("TRAILER!!!", b"")
    return out


class _TmpHip(object):
    """把原始字节写成一个 .hip（或指定扩展名）临时文件并返回路径。"""

    def __init__(self, raw, ext=".hip"):
        fd, self.path = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)

    def remove(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def parse_raw(raw, ext=".hip", **kw):
    tmp = _TmpHip(raw, ext)
    try:
        return hip.parse_hip_offline(tmp.path, **kw)
    finally:
        tmp.remove()


def parse_entries(entries, ext=".hip", trailer=True, **kw):
    return parse_raw(make_archive(entries, trailer=trailer), ext=ext, **kw)


# 真实风格的合成 section 片段
def init_body(t):
    return "type = %s\nmatchesdef = 0\n" % t


DEF_NO_INPUTS = (
    'comment ""\n'
    "position 0 0\n"
    "connectornextid 0\n"
    "inputsNamed3\n{\n}\n"
    "inputs\n{\n}\n")


def def_with_inputs(parent, sibling, idx=0, out=0):
    return (
        'comment ""\n'
        "position 1 2\n"
        "inputsNamed3\n"
        "{\n"
        "%d \t%s %d 1 \"input1\"\n"
        "}\n" % (idx, sibling, out))


def parm_body():
    return '{\nversion 0.8\nsize\t[ 0\tlocks=0 ]\t( 1 1 1 )\n}\n'


class _EnvOverride(object):
    """临时设置 os.environ 限额覆盖（只能收紧）。"""

    def __init__(self, **overrides):
        self.overrides = overrides
        self._old = {}

    def __enter__(self):
        for k, v in self.overrides.items():
            self._old[k] = os.environ.get(k)
            os.environ[k] = str(v)
        return self

    def __exit__(self, *exc):
        for k, old in self._old.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


# ---------------------------------------------------------------------------
# task 5.1：header 字段、TRAILER、section 任意顺序、完整路径关联
# ---------------------------------------------------------------------------
class EntryHeaderAndAssociationTests(unittest.TestCase):

    def test_header_fields_and_trailer(self):
        env = parse_entries([("obj/geo1.init", init_body("geo"))])
        self.assertEqual(env["status"], "success")
        self.assertTrue(env["metadata"]["trailer_seen"])
        self.assertGreater(env["metadata"]["complete_entries"], 0)
        self.assertEqual(env["metadata"]["bytes_consumed"],
                         len(make_archive([("obj/geo1.init", init_body("geo"))])))

    def test_section_order_independent_path_association(self):
        # 故意把 .def/.parm 放在 .init 之前，且 .def 引用 sibling
        entries = [
            ("obj/geo1/xform1.def",
             def_with_inputs("obj/geo1", "box1")),
            ("obj/geo1/xform1.parm", parm_body()),
            ("obj/geo1/box1.def", DEF_NO_INPUTS),
            ("obj/geo1/box1.init", init_body("box")),
            ("obj/geo1/xform1.init", init_body("xform")),
            ("obj/geo1.init", init_body("geo")),
        ]
        env = parse_entries(entries)
        self.assertEqual(env["status"], "success")
        paths = {n["path"] for n in env["nodes"]}
        self.assertEqual(paths, {"obj/geo1", "obj/geo1/box1",
                                 "obj/geo1/xform1"})
        types = {n["path"]: n["type"] for n in env["nodes"]}
        self.assertEqual(types["obj/geo1/box1"], "box")
        self.assertEqual(types["obj/geo1/xform1"], "xform")
        # sibling source 解析为完整路径
        conns = {(c["from"], c["to"], c["input_index"])
                 for c in env["connections"]}
        self.assertIn(("obj/geo1/box1", "obj/geo1/xform1", 0), conns)

    def test_trailer_terminates_even_if_more_bytes_absent(self):
        # 只有 TRAILER，无任何 section → 空成功
        env = parse_entries([])
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["nodes"], [])
        self.assertTrue(env["metadata"]["trailer_seen"])


# ---------------------------------------------------------------------------
# task 5.2：重复 entry last-complete-wins / duplicate_entries / 截断不覆盖
# ---------------------------------------------------------------------------
class DuplicateEntryTests(unittest.TestCase):

    def test_last_complete_wins_and_duplicate_count(self):
        entries = [
            ("obj/a.init", init_body("box")),
            ("obj/a.init", init_body("sphere")),   # 覆盖
        ]
        env = parse_entries(entries)
        self.assertEqual(env["status"], "success")
        self.assertEqual({n["path"] for n in env["nodes"]}, {"obj/a"})
        self.assertEqual(env["nodes"][0]["type"], "sphere")  # 最后完整值
        self.assertEqual(env["metadata"]["duplicate_entries"], 1)

    def test_truncated_duplicate_does_not_overwrite(self):
        # 第一个完整 box.init；随后一个被截断的 box.init（body 短读，且
        # 处于 EOF，没有后续字节可凑足声明的 filesize）。
        good = make_entry("obj/a.init", init_body("box"))
        nameb = b"obj/a.init\x00"
        bad_header = make_header(len(nameb), 50)   # 声明 50 字节 body
        bad = bad_header + nameb + b"only10"        # 实际只给 6 字节，无 trailer
        raw = good + bad
        env = parse_raw(raw)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "truncated_archive")
        # partial 用的是第一个完整 entry 的值（box，未被截断的覆盖）
        self.assertEqual({n["path"]: n["type"] for n in env["nodes"]},
                         {"obj/a": "box"})
        self.assertFalse(env["metadata"]["trailer_seen"])

    def test_duplicate_init_does_not_create_duplicate_node(self):
        entries = [
            ("obj/a.init", init_body("box")),
            ("obj/a.init", init_body("box")),
            ("obj/a.init", init_body("box")),
        ]
        env = parse_entries(entries)
        self.assertEqual([n["path"] for n in env["nodes"]], ["obj/a"])
        self.assertEqual(env["metadata"]["duplicate_entries"], 2)


# ---------------------------------------------------------------------------
# task 5.3：损坏与截断契约
# ---------------------------------------------------------------------------
class CorruptionTruncationTests(unittest.TestCase):

    def test_first_header_bad_magic_no_partial(self):
        raw = b"BADMSG0" + b"0" * 69 + make_entry("obj/a.init", init_body("x"))
        env = parse_raw(raw)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "invalid_archive")
        self.assertEqual(env["nodes"], [])           # 无 partial
        self.assertEqual(env["metadata"]["complete_entries"], 0)

    def test_first_header_short_no_partial(self):
        env = parse_raw(b"070707" + b"0" * 10)       # 不足 76
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "invalid_archive")
        self.assertEqual(env["nodes"], [])

    def test_first_header_bad_octal_no_partial(self):
        nameb = b"x.init\x00"
        # namesize 字段放非八进制字符
        header = (b"070707" + b"000000" * 7 + b"%011o" % 0
                  + b"00000X" + b"%011o" % 0)   # [59:65] 非法八进制
        raw = header + nameb + make_entry("TRAILER!!!", b"")
        env = parse_raw(raw)
        self.assertEqual(env["error"]["code"], "invalid_archive")
        self.assertEqual(env["nodes"], [])

    def test_empty_file_invalid_no_partial(self):
        env = parse_raw(b"")
        self.assertEqual(env["error"]["code"], "invalid_archive")
        self.assertEqual(env["nodes"], [])

    def test_subsequent_bad_magic_partial(self):
        good = make_entry("obj/a.init", init_body("box"))
        bad = b"XXXXXX" + b"0" * 70
        raw = good + bad + make_entry("TRAILER!!!", b"")
        env = parse_raw(raw)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "corrupt_archive")
        # 有 partial（来自第一个完整 entry）
        self.assertEqual({n["path"] for n in env["nodes"]}, {"obj/a"})
        self.assertFalse(env["metadata"]["trailer_seen"])

    def test_name_truncation_after_entry_partial(self):
        good = make_entry("obj/a.init", init_body("box"))
        # 下一个 entry header 声明 namesize=50 但只给 3 字节 name
        header = make_header(50, 0)
        raw = good + header + b"ab"
        env = parse_raw(raw)
        self.assertEqual(env["error"]["code"], "truncated_archive")
        self.assertEqual({n["path"] for n in env["nodes"]}, {"obj/a"})

    def test_body_truncation_partial_excludes_incomplete_body(self):
        nameb = b"obj/a.init\x00"
        header = make_header(len(nameb), 80)     # 声明 80 字节 body
        raw = header + nameb + b"only20byteshere!!"   # 实际 18 字节
        env = parse_raw(raw)
        self.assertEqual(env["error"]["code"], "truncated_archive")
        # 无任何完整 entry → 空 partial
        self.assertEqual(env["nodes"], [])

    def test_body_truncation_after_complete_entry(self):
        good = make_entry("obj/a.init", init_body("box"))
        nameb = b"obj/b.init\x00"
        header = make_header(len(nameb), 80)
        raw = good + header + nameb + b"short"
        env = parse_raw(raw)
        self.assertEqual(env["error"]["code"], "truncated_archive")
        self.assertEqual({n["path"] for n in env["nodes"]}, {"obj/a"})

    def test_missing_trailer_partial(self):
        # 多个完整 entry 但无 TRAILER，EOF
        raw = (make_entry("obj/a.init", init_body("box"))
               + make_entry("obj/b.init", init_body("sphere")))
        env = parse_raw(raw)
        self.assertEqual(env["error"]["code"], "truncated_archive")
        self.assertEqual({n["path"] for n in env["nodes"]},
                         {"obj/a", "obj/b"})
        self.assertFalse(env["metadata"]["trailer_seen"])


# ---------------------------------------------------------------------------
# task 5.4：限额（env 只能收紧）
# ---------------------------------------------------------------------------
class ResourceLimitTests(unittest.TestCase):

    def _fixture_path(self):
        return os.path.join(FIXTURES_DIR, "h21", "minimal.hip")

    def test_file_bytes_limit(self):
        with _EnvOverride(HOUDINI_MCP_HIP_MAX_FILE_BYTES=64):
            env = hip.parse_hip_offline(self._fixture_path())
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "resource_limit_exceeded")
        self.assertEqual(env["error"]["details"]["limit"], "max_file_bytes")
        self.assertEqual(env["nodes"], [])          # 无 partial（stat 阶段）

    def test_entry_count_limit(self):
        entries = [
            ("obj/a.init", init_body("box")),
            ("obj/a.def", DEF_NO_INPUTS),
            ("obj/b.init", init_body("sphere")),
        ]
        with _EnvOverride(HOUDINI_MCP_HIP_MAX_ENTRIES=2):
            env = parse_entries(entries)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "resource_limit_exceeded")
        self.assertEqual(env["error"]["details"]["limit"], "max_entries")
        # partial 不含第 3 个 entry
        self.assertEqual({n["path"] for n in env["nodes"]}, {"obj/a"})

    def test_single_section_bytes_limit(self):
        big = b"x" * 200
        with _EnvOverride(HOUDINI_MCP_HIP_MAX_SECTION_BYTES=10):
            env = parse_entries([("obj/a.parm", big)])
        self.assertEqual(env["error"]["code"], "resource_limit_exceeded")
        self.assertEqual(env["error"]["details"]["limit"], "max_section_bytes")
        self.assertEqual(env["nodes"], [])          # body 未读，无节点数据

    def test_total_section_bytes_limit(self):
        # body1=11 字节（放得下 max_total=11），body2=15 字节（累计超限）
        entries = [
            ("obj/a.init", b"type = box\n"),          # 11 字节，可解析为 box
            ("obj/b.init", b"x" * 15),
        ]
        with _EnvOverride(HOUDINI_MCP_HIP_MAX_TOTAL_SECTION_BYTES=11):
            env = parse_entries(entries)
        self.assertEqual(env["error"]["code"], "resource_limit_exceeded")
        self.assertEqual(env["error"]["details"]["limit"],
                         "max_total_section_bytes")
        # partial 含第一个完整 entry（obj/a 已是合法 .init）
        self.assertIn("obj/a", {n["path"] for n in env["nodes"]})

    def test_node_count_limit(self):
        entries = [
            ("obj/a.init", init_body("box")),
            ("obj/b.init", init_body("sphere")),
        ]
        with _EnvOverride(HOUDINI_MCP_HIP_MAX_NODES=1):
            env = parse_entries(entries)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "resource_limit_exceeded")
        self.assertEqual(env["error"]["details"]["limit"], "max_nodes")
        # partial 仅 1 个节点
        self.assertEqual(len(env["nodes"]), 1)

    def test_env_can_only_tighten_not_widen(self):
        # env 给一个远大于默认的值；生效值仍为默认（不能放宽）
        with _EnvOverride(HOUDINI_MCP_HIP_MAX_NODES=999999999):
            limits = hip.effective_limits()
        self.assertEqual(limits["max_nodes"], hip._DEFAULT_MAX_NODES)

    def test_max_depth_clamps_and_only_clips_tree(self):
        # 构造 3 层嵌套节点
        entries = [
            ("obj/a.init", init_body("geo")),
            ("obj/a/b.init", init_body("box")),
            ("obj/a/b/c.init", init_body("xform")),
        ]
        env = parse_entries(entries, max_depth=100)  # 先 clamp 测试
        self.assertEqual(env["metadata"]["max_depth"], 64)
        # flat nodes 不受 depth 影响
        self.assertEqual(len(env["nodes"]), 3)
        # depth=1：结构树根的孩子被裁
        env1 = parse_entries(entries, max_depth=1)
        self.assertEqual(env1["metadata"]["max_depth"], 1)
        self.assertEqual(len(env1["nodes"]), 3)       # flat 不变
        root = env1["structure"][0]
        self.assertEqual(root["path"], "obj/a")
        self.assertEqual(root["children"], [])
        self.assertTrue(root.get("truncated"))
        # depth=2：第二层可见，第三层被裁
        env2 = parse_entries(entries, max_depth=2)
        child = env2["structure"][0]["children"][0]
        self.assertEqual(child["path"], "obj/a/b")
        self.assertTrue(child.get("truncated"))

    def test_max_depth_invalid_falls_back_to_default(self):
        env = parse_entries([("obj/a.init", init_body("box"))],
                            max_depth="notint")
        self.assertEqual(env["metadata"]["max_depth"], hip._DEFAULT_MAX_DEPTH)


# ---------------------------------------------------------------------------
# task 5.5：未知 section / include_params / save_version
# ---------------------------------------------------------------------------
class ContentExtractionTests(unittest.TestCase):

    def test_unknown_section_skipped_and_counted(self):
        entries = [
            ("obj/a.init", init_body("box")),
            ("obj/a.unknownsuffix", b"hello"),
            ("obj/a.userdata", b"\x00\x01\x02"),
            ("weird.thing", b"x"),
        ]
        env = parse_entries(entries)
        self.assertEqual(env["status"], "success")
        self.assertGreaterEqual(env["metadata"]["skipped_sections"], 3)

    def test_unknown_section_not_treated_as_corrupt(self):
        env = parse_entries([("foo.bar", b"baz")])
        self.assertEqual(env["status"], "success")

    def test_include_params_true_includes_raw_parm(self):
        entries = [
            ("obj/a.init", init_body("box")),
            ("obj/a.parm", parm_body()),
        ]
        env = parse_entries(entries, include_params=True)
        node = env["nodes"][0]
        self.assertIn("parameters", node)
        self.assertIn("version 0.8", node["parameters"])

    def test_include_params_false_omits_parm(self):
        entries = [
            ("obj/a.init", init_body("box")),
            ("obj/a.parm", parm_body()),
        ]
        env = parse_entries(entries, include_params=False)
        node = env["nodes"][0]
        self.assertNotIn("parameters", node)

    def test_include_params_missing_parm_omits_field(self):
        env = parse_entries([("obj/a.init", init_body("box"))],
                            include_params=True)
        self.assertNotIn("parameters", env["nodes"][0])

    def test_save_version_extracted_from_variables(self):
        variables = ("set -g HIP = '/tmp'\n"
                     "set -g _HIP_SAVEVERSION = '20.5.621'\n"
                     "set -g PI = '3.14'\n")
        env = parse_entries([
            ("obj/a.init", init_body("geo")),
            (".variables", variables),
        ])
        self.assertEqual(env["save_version"], "20.5.621")

    def test_save_version_null_when_unparseable(self):
        env = parse_entries([
            ("obj/a.init", init_body("geo")),
            (".variables", "set -g PI = '3.14'\n"),   # 无 _HIP_SAVEVERSION
        ])
        self.assertIsNone(env["save_version"])

    def test_save_version_null_when_no_variables(self):
        env = parse_entries([("obj/a.init", init_body("geo"))])
        self.assertIsNone(env["save_version"])

    def test_postit_and_netbox_extraction(self):
        entries = [
            ("obj/note1.postitinit", "type = postitnote\nmatchesdef = 0\n"),
            ("obj/note1.postitdef", 'text "remember me"\nposition 1 1\n'),
            ("obj/nb1.netboxinit",
             '{\n\tcomment := mybox;\n\twidth := 2.5;\n}\n'),
        ]
        env = parse_entries(entries)
        self.assertEqual(len(env["postits"]), 1)
        self.assertEqual(env["postits"][0],
                         {"context": "obj", "name": "note1",
                          "text": "remember me"})
        self.assertEqual(len(env["netboxes"]), 1)
        self.assertEqual(env["netboxes"][0],
                         {"context": "obj", "name": "nb1", "label": "mybox"})


# ---------------------------------------------------------------------------
# task 5.6：大 partial payload 经 response cap 后结构仍合法
# ---------------------------------------------------------------------------
class ResponseCapTests(unittest.TestCase):
    """apply_response_cap 只裁剪 envelope 内最大的单个 list。为让「截断发生」
    可稳定断言，这里用 ``include_params=True`` + 大 parm 让 ``nodes`` 成为
    唯一占主导的 list（``structure`` 仅含少量根），cap 即可生效。"""

    def _big_entries(self, n_nodes=8, parm_repeat=60):
        # parm_body()≈45 字节，×60 后每节点 parameters 远大于结构树开销
        entries = []
        for i in range(n_nodes):
            p = "obj/n%02d" % i
            entries.append((p + ".init", init_body("box")))
            entries.append((p + ".parm", parm_body() * parm_repeat))
        return entries

    def test_success_payload_capped_structure_valid(self):
        # identity cap_fn 拿到真正未 cap 的 baseline（response_cap_fn=None 会
        # 回退到 cmn.apply_response_cap，故必须显式 identity）。
        identity = lambda d, m=None: d
        full_env = parse_entries(self._big_entries(), include_params=True,
                                 response_cap_fn=identity)
        full_size = len(json.dumps(full_env, default=str).encode("utf-8"))
        # max_bytes 取在「不可压缩下限」（structure+metadata ≈ 1.1KB）与
        # 「完整体积」之间，确保 cap 实际裁剪 nodes 而非整体放弃。
        env = parse_entries(self._big_entries(), include_params=True,
                            response_cap_fn=cmn.apply_response_cap,
                            max_bytes=6000)
        self.assertEqual(env["status"], "success")
        self.assertIsInstance(env, dict)
        self.assertIsInstance(env["nodes"], list)
        self.assertIn("metadata", env)
        capped_size = len(json.dumps(env, default=str).encode("utf-8"))
        # cap 确实生效：capped 体积严格小于未 cap baseline
        self.assertLess(capped_size, full_size)

    def test_truncated_partial_capped_structure_valid(self):
        identity = lambda d, m=None: d
        raw = make_archive(self._big_entries(), trailer=False)
        tmp_full = _TmpHip(raw)
        tmp_capped = _TmpHip(raw)
        try:
            full_env = hip.parse_hip_offline(tmp_full.path,
                                             include_params=True,
                                             response_cap_fn=identity)
            env = hip.parse_hip_offline(
                tmp_capped.path, include_params=True,
                response_cap_fn=cmn.apply_response_cap, max_bytes=6000)
        finally:
            tmp_full.remove()
            tmp_capped.remove()
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "truncated_archive")
        self.assertIsInstance(env, dict)
        self.assertIsInstance(env["nodes"], list)
        full_size = len(json.dumps(full_env, default=str).encode("utf-8"))
        capped_size = len(json.dumps(env, default=str).encode("utf-8"))
        self.assertLess(capped_size, full_size)

    def test_no_cap_fn_returns_full(self):
        env = parse_entries(
            [("obj/n%02d.init" % i, init_body("box")) for i in range(5)],
            response_cap_fn=None)
        self.assertEqual(len(env["nodes"]), 5)


# ---------------------------------------------------------------------------
# 错误 envelope 形状与扩展名校验
# ---------------------------------------------------------------------------
class EnvelopeShapeTests(unittest.TestCase):

    def test_unsupported_extension(self):
        tmp = _TmpHip(make_archive([("obj/a.init", init_body("x"))]),
                      ext=".txt")
        try:
            env = hip.parse_hip_offline(tmp.path)
        finally:
            tmp.remove()
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "unsupported_extension")
        self.assertEqual(env["nodes"], [])

    def test_missing_file(self):
        env = hip.parse_hip_offline(
            os.path.join(FIXTURES_DIR, "does_not_exist.hip"))
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "hip_not_found")

    def test_non_string_path(self):
        env = hip.parse_hip_offline(None)
        self.assertEqual(env["error"]["code"], "unsupported_extension")

    def test_editions_all_supported(self):
        for ext in (".hip", ".hiplc", ".hipnc"):
            env = parse_entries([("obj/a.init", init_body("x"))], ext=ext)
            self.assertEqual(env["status"], "success",
                             "edition %s 应被支持" % ext)

    def test_error_envelope_has_consistent_metadata(self):
        env = parse_raw(b"")
        md = env["metadata"]
        for key in ("complete_entries", "bytes_consumed", "trailer_seen",
                    "duplicate_entries", "skipped_sections", "section_count",
                    "node_count", "limits", "node_limit_hit"):
            self.assertIn(key, md)


# ---------------------------------------------------------------------------
# golden fixture 验收（task 4.5 / 6.3）
# ---------------------------------------------------------------------------
class GoldenFixtureTests(unittest.TestCase):
    """对真实 Houdini 保存的 golden fixture 做解析比对。

    矩阵完整性（H20/H21/H22 × 3 edition = 9）受 H20/H22 未安装阻塞：当前
    仅 H21.0.596 可生成 3 个 fixture。本类对 **manifest 中存在** 的 fixture
    逐个解析并与 hou ground-truth manifest 比对（真实 cpio，禁止 mock）。
    缺失的 H20/H22 fixture 不以 skip/xfail 掩盖，而在 change tasks.md /
    报告中显式记为阻塞，待对应版本可用时用 ``generate_fixtures.py`` 补齐。
    """

    def setUp(self):
        self.manifest_path = os.path.join(FIXTURES_DIR, "manifest.json")
        with open(self.manifest_path, encoding="utf-8") as fh:
            self.manifest = json.load(fh)

    def test_manifest_schema(self):
        self.assertEqual(self.manifest.get("schema"), "hip-golden-fixtures/v1")
        self.assertIsInstance(self.manifest.get("fixtures"), list)
        self.assertGreater(len(self.manifest["fixtures"]), 0)

    def test_present_fixtures_match_manifest(self):
        for fx in self.manifest["fixtures"]:
            with self.subTest(version=fx["version"], edition=fx["edition"]):
                path = os.path.join(FIXTURES_DIR, fx["path"])
                self.assertTrue(os.path.exists(path),
                                "fixture 缺失: %s" % fx["path"])
                env = hip.parse_hip_offline(path)
                self.assertEqual(env["status"], "success",
                                 "fixture %s 解析失败" % fx["path"])
                exp = fx["expected"]
                # node paths（集合）
                got_paths = {n["path"] for n in env["nodes"]}
                self.assertEqual(got_paths, set(exp["node_paths"]))
                # node types
                for n in env["nodes"]:
                    self.assertEqual(n["type"], exp["node_types"][n["path"]],
                                     "type 不符: %s" % n["path"])
                # connections（集合）
                got_c = {(c["from"], c["to"], c["input_index"])
                         for c in env["connections"]}
                exp_c = {(c["from"], c["to"], c["input_index"])
                         for c in exp["connections"]}
                self.assertEqual(got_c, exp_c)
                # postits
                got_p = {(p["context"], p["name"], p["text"])
                         for p in env["postits"]}
                exp_p = {(p["context"], p["name"], p["text"])
                         for p in exp["postits"]}
                self.assertEqual(got_p, exp_p)
                # netboxes
                got_n = {(nb["context"], nb["name"], nb["label"])
                         for nb in env["netboxes"]}
                exp_n = {(nb["context"], nb["name"], nb["label"])
                         for nb in exp["netboxes"]}
                self.assertEqual(got_n, exp_n)
                # save_version 与 fixture 记录的 Houdini 版本一致
                self.assertEqual(env["save_version"], fx["houdini_version"])
                # trailer 正常结束
                self.assertTrue(env["metadata"]["trailer_seen"])

    def test_present_fixtures_sha256_matches(self):
        import hashlib
        for fx in self.manifest["fixtures"]:
            path = os.path.join(FIXTURES_DIR, fx["path"])
            if not os.path.exists(path):
                continue
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(digest, fx["sha256"],
                             "sha256 不符: %s" % fx["path"])

    def test_matrix_coverage_documented(self):
        """矩阵覆盖现状（信息性断言，非门禁）。

        完整 9-fixture 矩阵（H20/H21/H22 × 3 edition）的「全部通过且无
        skip/xfail」是 change 的硬门禁（tasks 4.5/6.3），但 H20/H22 当前
        未安装、fixture 无法生成。本测试仅记录当前覆盖，不在缺 H20/H22
        时失败（避免用失败信号变相 skip），矩阵门禁留在 tasks.md 显式未勾选。
        当 9 个 fixture 全部到齐时本测试自然转为完整覆盖断言。
        """
        versions = ["h20", "h21", "h22"]
        editions = ["hip", "hiplc", "hipnc"]
        present = {(f["version"], f["edition"])
                   for f in self.manifest["fixtures"]}
        full = {(v, e) for v in versions for e in editions}
        missing = sorted(full - present)
        # 当前仅 H21.0.596 可生成；断言至少 H21×3 完整，记录缺失。
        for e in editions:
            self.assertIn(("h21", e), present,
                          "H21×%s 应完整存在" % e)
        # 仅记录，不依缺失判定 pass/fail
        self.assertIsInstance(missing, list)


if __name__ == "__main__":
    unittest.main()
