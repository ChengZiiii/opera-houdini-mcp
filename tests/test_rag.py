"""BM25 RAG 引擎的 tokenizer、BM25、JSON schema、安全降级、原子发布、
snippet、cap 与 get_doc 边界测试。

覆盖 tasks 4.1-4.8：
- 4.1 tokenizer/BM25 排序和 limit clamp
- 4.2 HTML parser 递归 fixture：嵌套标签、实体、script/style/noscript、无 title
- 4.3 JSON round-trip、未知 version、错 schema、坏 JSON、重复 path/id、悬空 posting
- 4.4 原子发布：成功 replace；写入/replace 失败时旧索引完整且临时文件清理
- 4.5 mtime reload 与坏新文件保留已校验 stale cache
- 4.6 snippet 命中在正文头/中/尾和无可定位命中
- 4.7 真实大 payload：matched 为 cap 前总数、returned == len(results) 且为 cap 后数量
- 4.8 get_doc 不存在 path/遍历字符串均不能触发源文件回读
"""

import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_rag():
    """以独立 module name 加载 _rag，避免与 package import 冲突。"""
    name = "test_rag_isolated._rag"
    if name in sys.modules:
        return sys.modules[name]
    # _rag 顶层 fallback `import _common`；确保 flat import 可用。
    import _common  # noqa: F401
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "_rag.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rag = _load_rag()
import _common as cmn  # noqa: E402

# build_rag_index 位于 scripts/ 子目录；独立加载以便测试 HTML 解析与发布。
_BUILD_NAME = "test_rag_isolated.build_rag_index"
if _BUILD_NAME in sys.modules:
    build_mod = sys.modules[_BUILD_NAME]
else:
    _scripts_dir = os.path.join(ROOT, "scripts")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    _build_spec = importlib.util.spec_from_file_location(
        _BUILD_NAME, os.path.join(_scripts_dir, "build_rag_index.py"))
    build_mod = importlib.util.module_from_spec(_build_spec)
    sys.modules[_BUILD_NAME] = build_mod
    _build_spec.loader.exec_module(build_mod)


# ---------------------------------------------------------------------------
# 构造合法索引 JSON 的 helper
# ---------------------------------------------------------------------------
def _make_index_json(documents, postings=None, avgdl=None,
                     document_count=None, schema=None, version=None,
                     built_at="2026-01-01T00:00:00+00:00",
                     source="test"):
    if schema is None:
        schema = rag.SCHEMA_NAME
    if version is None:
        version = rag.SCHEMA_VERSION
    if document_count is None:
        document_count = len(documents)
    if avgdl is None:
        if documents:
            avgdl = sum(d["length"] for d in documents) / float(len(documents))
        else:
            avgdl = 0.0
    if postings is None:
        postings = {}
    return json.dumps({
        "schema": schema,
        "version": version,
        "built_at": built_at,
        "source": source,
        "document_count": document_count,
        "avgdl": avgdl,
        "documents": documents,
        "postings": postings,
    }, ensure_ascii=False)


def _doc(doc_id, path, title, content, length=None, postings_tokens=None):
    """构造单个 document dict；length 默认为 content 的 token 数。"""
    if length is None:
        length = len(rag.tokenize(content))
    return {"id": doc_id, "path": path, "title": title,
            "length": length, "content": content}


def _write(tmpdir, name, text):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _write_index(tmpdir, name, index_dict_or_json):
    if isinstance(index_dict_or_json, str):
        text = index_dict_or_json
    else:
        text = json.dumps(index_dict_or_json, ensure_ascii=False)
    return _write(tmpdir, name, text)


# ---------------------------------------------------------------------------
# task 4.1：tokenizer 与 BM25 排序 / limit clamp
# ---------------------------------------------------------------------------
class TokenizerTests(unittest.TestCase):

    def test_hou_api_kept_whole(self):
        tokens = rag.tokenize("Call hou.node.setDisplayNode now")
        self.assertIn("hou.node.setdisplaynode", tokens)

    def test_node_path_kept_whole(self):
        tokens = rag.tokenize("path /obj/geo1/box1 here")
        self.assertIn("/obj/geo1/box1", tokens)

    def test_snake_case_split_and_whole(self):
        tokens = rag.tokenize("point_count value")
        self.assertIn("point_count", tokens)
        self.assertIn("point", tokens)
        self.assertIn("count", tokens)

    def test_lowercase_and_stopwords(self):
        tokens = rag.tokenize("The Box IS a Geometry")
        # 停用词 the/is/a 被过滤；box/geometry 保留并小写
        self.assertIn("box", tokens)
        self.assertIn("geometry", tokens)
        self.assertNotIn("the", tokens)
        self.assertNotIn("is", tokens)
        self.assertNotIn("a", tokens)

    def test_short_tokens_filtered(self):
        tokens = rag.tokenize("a 1 x box")
        self.assertNotIn("a", tokens)
        self.assertNotIn("x", tokens)
        self.assertIn("box", tokens)

    def test_non_string_returns_empty(self):
        self.assertEqual(rag.tokenize(None), [])
        self.assertEqual(rag.tokenize(123), [])


class BM25SearchTests(unittest.TestCase):
    def setUp(self):
        # 3 个文档：box 出现在 2 个，sphere 仅 1 个
        docs = [
            _doc(0, "nodes/sop/box.html", "Box",
                 "The box node creates box geometry primitives."),
            _doc(1, "nodes/sop/sphere.html", "Sphere",
                 "The sphere node creates sphere geometry."),
            _doc(2, "nodes/sop/box2.html", "Box Again",
                 "Another box example with more box detail."),
        ]
        # 手工构造 postings：box -> {0:1, 2:2}; sphere -> {1:2}
        postings = {
            "box": [[0, 1], [2, 2]],
            "sphere": [[1, 2]],
            "node": [[0, 1], [1, 1]],
            "geometry": [[0, 1], [1, 1]],
        }
        index_json = _make_index_json(docs, postings=postings)
        status = rag.load_index.__wrapped__ if hasattr(
            rag.load_index, "__wrapped__") else None
        # 直接走 _validate_index 构造，不走磁盘
        self.index = rag._validate_index(index_json)

    def test_search_returns_positive_only_sorted_desc(self):
        matched = self.index.search("box")
        # 两个正分文档：doc 2 (tf=2) 应排在 doc 0 (tf=1) 前
        self.assertEqual(len(matched), 2)
        self.assertEqual(matched[0][0], 2)
        self.assertEqual(matched[1][0], 0)
        self.assertGreater(matched[0][1], matched[1][1])

    def test_search_no_match_returns_empty(self):
        self.assertEqual(self.index.search("nonexistentterm"), [])

    def test_search_limit_clamp(self):
        matched_all = self.index.search("box")
        matched_lim = self.index.search("box", limit=1)
        self.assertEqual(len(matched_all), 2)
        self.assertEqual(len(matched_lim), 1)
        self.assertEqual(matched_lim[0][0], 2)

    def test_search_limit_zero_or_negative(self):
        self.assertEqual(self.index.search("box", limit=0), [])
        self.assertEqual(self.index.search("box", limit=-5), [])

    def test_search_accepts_token_list(self):
        matched = self.index.search(["box"])
        self.assertEqual(len(matched), 2)

    def test_search_score_is_positive(self):
        for _doc_id, score in self.index.search("box"):
            self.assertGreater(score, 0.0)


# ---------------------------------------------------------------------------
# task 4.2：HTML parser 递归 fixture
# ---------------------------------------------------------------------------
class HTMLParserTests(unittest.TestCase):

    def test_nested_tags_extract_visible_body(self):
        html = "<div><p>hello <b>world</b></p></div>"
        title, body = build_mod.parse_html(html)
        self.assertEqual(title, "")
        self.assertIn("hello", body)
        self.assertIn("world", body)

    def test_title_extracted(self):
        html = "<html><head><title>Box Node</title></head><body><p>body</p></body></html>"
        title, body = build_mod.parse_html(html)
        self.assertEqual(title, "Box Node")
        self.assertIn("body", body)

    def test_entities_decoded(self):
        html = "<p>foo &amp; bar &#39;baz&#39;</p>"
        title, body = build_mod.parse_html(html)
        self.assertIn("foo & bar", body)
        self.assertIn("'baz'", body)

    def test_script_style_noscript_ignored(self):
        html = ("<p>before</p>"
                "<script>var x = 'ignore';</script>"
                "<style>.ignore { color: red; }</style>"
                "<noscript>nojs ignore</noscript>"
                "<p>after</p>")
        title, body = build_mod.parse_html(html)
        self.assertNotIn("ignore", body.lower())
        self.assertNotIn("nojs", body.lower())
        self.assertIn("before", body)
        self.assertIn("after", body)

    def test_no_title_returns_empty_title(self):
        html = "<p>only body</p>"
        title, body = build_mod.parse_html(html)
        self.assertEqual(title, "")
        self.assertIn("only body", body)

    def test_whitespace_collapsed(self):
        html = "<p>line1\n\n\n   line2\t\tline3</p>"
        _title, body = build_mod.parse_html(html)
        self.assertNotIn("\n", body)
        self.assertNotIn("\t", body)
        self.assertIn("line1 line2 line3", body)

    def test_malformed_html_does_not_throw(self):
        # 未闭合标签 + 坏嵌套
        html = "<p>unclosed <b>bold</p> trailing"
        _title, body = build_mod.parse_html(html)
        self.assertIn("unclosed", body)
        self.assertIn("bold", body)


# ---------------------------------------------------------------------------
# task 4.3：JSON schema 校验
# ---------------------------------------------------------------------------
class JSONSchemaTests(unittest.TestCase):

    def setUp(self):
        self.docs = [_doc(0, "a.html", "A", "box geometry")]
        self.postings = {"box": [[0, 1]], "geometry": [[0, 1]]}

    def _expect_error(self, index_dict_or_json, code=None):
        if isinstance(index_dict_or_json, dict):
            text = json.dumps(index_dict_or_json, ensure_ascii=False)
        else:
            text = index_dict_or_json
        with self.assertRaises(rag.RagIndexError) as ctx:
            rag._validate_index(text)
        if code is not None:
            self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_valid_round_trip(self):
        index_json = _make_index_json(self.docs, postings=self.postings)
        index = rag._validate_index(index_json)
        self.assertEqual(index.document_count, 1)
        self.assertIsInstance(index.avgdl, float)

    def test_unknown_version_rejected(self):
        index_json = _make_index_json(self.docs, postings=self.postings, version=99)
        self._expect_error(index_json, "rag_index_bad_version")

    def test_version_float_rejected(self):
        data = json.loads(_make_index_json(self.docs, postings=self.postings))
        data["version"] = 1.0
        self._expect_error(data, "rag_index_bad_version")

    def test_wrong_schema_rejected(self):
        index_json = _make_index_json(self.docs, postings=self.postings,
                                       schema="something.else")
        self._expect_error(index_json, "rag_index_bad_schema")

    def test_bad_json_rejected(self):
        self._expect_error("{not valid json", "rag_index_bad_json")

    def test_duplicate_path_rejected(self):
        docs = [
            _doc(0, "dup.html", "A", "box"),
            _doc(1, "dup.html", "B", "sphere"),
        ]
        index_json = _make_index_json(docs, postings={"box": [[0, 1]]})
        self._expect_error(index_json, "rag_index_bad_schema")

    def test_duplicate_id_rejected(self):
        docs = [
            _doc(0, "a.html", "A", "box"),
            _doc(0, "b.html", "B", "sphere"),
        ]
        index_json = _make_index_json(docs, postings={"box": [[0, 1]]})
        self._expect_error(index_json, "rag_index_bad_schema")

    def test_dangling_posting_rejected(self):
        # posting 引用不存在的 doc_id 99
        index_json = _make_index_json(
            self.docs, postings={"box": [[0, 1], [99, 2]]})
        self._expect_error(index_json, "rag_index_bad_schema")

    def test_negative_tf_rejected(self):
        index_json = _make_index_json(
            self.docs, postings={"box": [[0, -1]]})
        self._expect_error(index_json, "rag_index_bad_schema")

    def test_document_count_mismatch_rejected(self):
        data = json.loads(_make_index_json(self.docs, postings=self.postings))
        data["document_count"] = 99
        self._expect_error(data, "rag_index_bad_schema")

    def test_negative_avgdl_rejected(self):
        data = json.loads(_make_index_json(self.docs, postings=self.postings))
        data["avgdl"] = -1.0
        self._expect_error(data, "rag_index_bad_schema")

    def test_duplicate_posting_doc_id_within_term_rejected(self):
        index_json = _make_index_json(
            self.docs, postings={"box": [[0, 1], [0, 2]]})
        self._expect_error(index_json, "rag_index_bad_schema")

    def test_non_string_content_rejected(self):
        data = json.loads(_make_index_json(self.docs, postings=self.postings))
        data["documents"][0]["content"] = 123
        self._expect_error(data, "rag_index_bad_schema")

    def test_empty_path_rejected(self):
        data = json.loads(_make_index_json(self.docs, postings=self.postings))
        data["documents"][0]["path"] = ""
        self._expect_error(data, "rag_index_bad_schema")


# ---------------------------------------------------------------------------
# task 4.4：原子发布
# ---------------------------------------------------------------------------
class AtomicPublishTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _index(self, n_docs=2):
        docs = []
        for i in range(n_docs):
            docs.append(_doc(i, "doc{0}.html".format(i), "T{0}".format(i),
                             "box geometry " * (i + 1)))
        return self._manual_index(docs)

    def _manual_index(self, docs):
        postings = {}
        for doc in docs:
            tokens = rag.tokenize(doc["content"])
            doc_tf = {}
            for tok in tokens:
                doc_tf[tok] = doc_tf.get(tok, 0) + 1
            for term, tf in doc_tf.items():
                postings.setdefault(term, {})[doc["id"]] = tf
        postings_out = {}
        for term, doc_map in postings.items():
            postings_out[term] = sorted(
                [[did, tf] for did, tf in doc_map.items()])
        total = sum(d["length"] for d in docs)
        return {
            "schema": rag.SCHEMA_NAME,
            "version": rag.SCHEMA_VERSION,
            "built_at": "2026-01-01T00:00:00+00:00",
            "source": "test",
            "document_count": len(docs),
            "avgdl": total / float(len(docs)) if docs else 0.0,
            "documents": docs,
            "postings": postings_out,
        }

    def test_successful_publish_replaces(self):
        index = self._manual_index([_doc(0, "a.html", "A", "box")])
        final_path = build_mod.publish_index(index, self.index_dir)
        self.assertTrue(os.path.exists(final_path))
        # 临时文件应已清理（os.replace 移走）
        leftovers = [f for f in os.listdir(self.index_dir)
                     if f.startswith(".index.v1.")]
        self.assertEqual(leftovers, [])
        # 写出的内容可被校验
        with open(final_path, "r", encoding="utf-8") as handle:
            rag._validate_index(handle.read())

    def test_write_failure_preserves_old_and_cleans_temp(self):
        # 先发布一个合法旧索引
        old_index = self._manual_index([_doc(0, "a.html", "A", "box")])
        build_mod.publish_index(old_index, self.index_dir)
        final_path = os.path.join(self.index_dir, rag.INDEX_FILENAME)
        old_bytes = open(final_path, "rb").read()

        # 模拟写入失败：patch tempfile.mkstemp 抛 OSError
        original_mkstemp = build_mod.tempfile.mkstemp

        def failing_mkstemp(*args, **kwargs):
            raise OSError("simulated write failure")

        build_mod.tempfile.mkstemp = failing_mkstemp
        try:
            new_index = self._manual_index([_doc(0, "a.html", "A", "sphere")])
            with self.assertRaises(OSError):
                build_mod.publish_index(new_index, self.index_dir)
        finally:
            build_mod.tempfile.mkstemp = original_mkstemp

        # 旧索引完整保留
        self.assertEqual(open(final_path, "rb").read(), old_bytes)
        # 临时文件被清理（本次根本没创建成功）
        leftovers = [f for f in os.listdir(self.index_dir)
                     if f.startswith(".index.v1.")]
        self.assertEqual(leftovers, [])

    def test_replace_failure_preserves_old_and_cleans_temp(self):
        old_index = self._manual_index([_doc(0, "a.html", "A", "box")])
        build_mod.publish_index(old_index, self.index_dir)
        final_path = os.path.join(self.index_dir, rag.INDEX_FILENAME)
        old_bytes = open(final_path, "rb").read()

        # 模拟 replace 失败：patch os.replace 抛 OSError
        original_replace = build_mod.os.replace

        def failing_replace(src, dst):
            raise OSError("simulated replace failure")

        build_mod.os.replace = failing_replace
        try:
            new_index = self._manual_index([_doc(0, "a.html", "A", "sphere")])
            with self.assertRaises(OSError):
                build_mod.publish_index(new_index, self.index_dir)
        finally:
            build_mod.os.replace = original_replace

        # 旧索引完整保留
        self.assertEqual(open(final_path, "rb").read(), old_bytes)
        # 临时文件被 best-effort 清理
        leftovers = [f for f in os.listdir(self.index_dir)
                     if f.startswith(".index.v1.")]
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# task 4.5：mtime reload 与坏新文件保留 stale cache
# ---------------------------------------------------------------------------
class CacheReloadTests(unittest.TestCase):

    def setUp(self):
        rag.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _write_index(
            self.tmp.name, rag.INDEX_FILENAME,
            _make_index_json([_doc(0, "a.html", "A", "box geometry")],
                              postings={"box": [[0, 1]], "geometry": [[0, 1]]}))

    def tearDown(self):
        rag.clear_cache()
        self.tmp.cleanup()

    def test_first_load_ok(self):
        status = rag.load_index(self.path)
        self.assertEqual(status["state"], "ok")
        self.assertIsNotNone(status["index"])

    def test_mtime_change_triggers_reload(self):
        first = rag.load_index(self.path)
        self.assertEqual(first["state"], "ok")
        # 重写为新内容 + 推进 mtime
        new_json = _make_index_json(
            [_doc(0, "a.html", "A", "sphere geometry"),
             _doc(1, "b.html", "B", "box extra")],
            postings={"sphere": [[0, 1]], "geometry": [[0, 1]],
                      "box": [[1, 1]], "extra": [[1, 1]]})
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(new_json)
        os.utime(self.path, ns=(10 ** 18, 10 ** 18))
        second = rag.load_index(self.path)
        self.assertEqual(second["state"], "ok")
        # 新索引应能搜到 sphere
        matched = second["index"].search("sphere")
        self.assertEqual(len(matched), 1)

    def test_bad_new_file_preserves_stale_cache(self):
        # 先成功加载一次（建立 last-good cache）
        first = rag.load_index(self.path)
        self.assertEqual(first["state"], "ok")
        original_doc_count = first["index"].document_count

        # 覆盖为坏 JSON + 推进 mtime
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ broken json")
        os.utime(self.path, ns=(10 ** 18, 10 ** 18))

        second = rag.load_index(self.path)
        # 应降级为 stale，仍返回 last-good 缓存
        self.assertEqual(second["state"], "stale")
        self.assertIsNotNone(second["index"])
        self.assertEqual(second["index"].document_count, original_doc_count)
        self.assertTrue(second["warning"])

    def test_bad_new_file_without_cache_returns_unavailable(self):
        # 全新路径，从未加载过，直接写坏文件
        bad_path = _write_index(self.tmp.name, "bad.json", "{ broken")
        os.utime(bad_path, ns=(10 ** 18, 10 ** 18))
        status = rag.load_index(bad_path)
        self.assertEqual(status["state"], "unavailable")
        self.assertIsNone(status["index"])

    def test_missing_file_without_cache_returns_missing(self):
        status = rag.load_index(
            os.path.join(self.tmp.name, "never_existed.json"))
        self.assertEqual(status["state"], "missing")
        self.assertIsNone(status["index"])


# ---------------------------------------------------------------------------
# task 4.6：snippet 命中位置
# ---------------------------------------------------------------------------
class SnippetTests(unittest.TestCase):

    def test_hit_at_body_head(self):
        content = "box " + ("filler " * 200)
        snip = rag.make_snippet(content, ["box"])
        self.assertIn("\u00abbox\u00bb", snip)
        # 头部命中：不应有前导省略
        self.assertFalse(snip.startswith("... "))

    def test_hit_at_body_middle(self):
        content = ("leading " * 100) + " box " + ("trailing " * 100)
        snip = rag.make_snippet(content, ["box"])
        self.assertIn("\u00abbox\u00bb", snip)
        # 中部命中：应有前后省略
        self.assertTrue(snip.startswith("... "))
        self.assertTrue(snip.endswith(" ..."))

    def test_hit_at_body_tail(self):
        content = ("filler " * 200) + " box"
        snip = rag.make_snippet(content, ["box"])
        self.assertIn("\u00abbox\u00bb", snip)

    def test_no_locatable_hit_falls_back_to_start(self):
        content = "alpha beta gamma " * 50
        snip = rag.make_snippet(content, ["zzznotpresent"])
        # 回退正文开头：无命中标记
        self.assertNotIn("\u00ab", snip)
        self.assertTrue(snip.startswith("alpha"))

    def test_snippet_respects_max_chars(self):
        content = "word " * 1000
        snip = rag.make_snippet(content, ["word"], max_chars=50)
        # snippet 总长应受 max_chars 约束（含省略标记余量）
        self.assertLessEqual(len(snip), 50 + 20)

    def test_empty_content_returns_empty(self):
        self.assertEqual(rag.make_snippet("", ["box"]), "")
        self.assertEqual(rag.make_snippet(None, ["box"]), "")


# ---------------------------------------------------------------------------
# task 4.7：真实大 payload 的 matched / returned 计数
# ---------------------------------------------------------------------------
class LargePayloadCapTests(unittest.TestCase):

    def setUp(self):
        rag.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        # 构造 30 个文档都包含 query token，使 matched > limit
        docs = []
        postings = {}
        for i in range(30):
            docs.append(_doc(i, "doc{0:02d}.html".format(i), "T{0}".format(i),
                             "pyro simulation fire " + "noise " * (i + 1)))
        # 全部文档都含 pyro/fire
        postings["pyro"] = [[i, 1] for i in range(30)]
        postings["fire"] = [[i, 1] for i in range(30)]
        # noise 的 tf 随 i 递增，制造分数差异
        for i in range(30):
            postings.setdefault("noise", []).append([i, i + 1])
        self.index_json = _make_index_json(docs, postings=postings)
        self.path = _write_index(
            self.tmp.name, rag.INDEX_FILENAME, self.index_json)

    def tearDown(self):
        rag.clear_cache()
        self.tmp.cleanup()

    def test_matched_is_pre_cap_total_with_large_budget(self):
        env = rag.search_docs(
            "pyro", limit=5, index_path=self.path,
            response_cap_fn=cmn.apply_response_cap, max_bytes=10 ** 8)
        self.assertEqual(env["status"], "success")
        # 30 个文档都含 pyro -> matched = 30（cap 前）
        self.assertEqual(env["matched"], 30)
        # limit=5 -> 返回最多 5；大预算不被 cap 截断
        self.assertLessEqual(env["returned"], 5)
        self.assertEqual(env["returned"], len(env["results"]))

    def test_returned_aligned_after_tight_cap(self):
        env = rag.search_docs(
            "pyro", limit=10, index_path=self.path,
            response_cap_fn=cmn.apply_response_cap, max_bytes=300)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched"], 30)
        # 紧预算：cap 会截断 results；returned 必须等于实际 results 长度
        self.assertEqual(env["returned"], len(env["results"]))
        self.assertLessEqual(env["returned"], 10)

    def test_limit_clamp_in_search(self):
        env = rag.search_docs(
            "pyro", limit=1000, index_path=self.path,
            response_cap_fn=None, max_bytes=10 ** 8)
        # limit clamp 到 50
        self.assertEqual(env["limit"], 50)
        self.assertEqual(env["matched"], 30)
        self.assertEqual(env["returned"], 30)

    def test_results_contain_required_fields(self):
        env = rag.search_docs(
            "pyro", limit=3, index_path=self.path,
            response_cap_fn=None, max_bytes=10 ** 8)
        for entry in env["results"]:
            self.assertIn("path", entry)
            self.assertIn("title", entry)
            self.assertIn("score", entry)
            self.assertIn("snippet", entry)
            self.assertIsInstance(entry["score"], float)


# ---------------------------------------------------------------------------
# task 4.8：get_doc 边界（不触发源文件回读 / 遍历）
# ---------------------------------------------------------------------------
class GetDocBoundaryTests(unittest.TestCase):

    def setUp(self):
        rag.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        # 索引里只内嵌一个文档
        self.index_json = _make_index_json(
            [_doc(0, "nodes/sop/box.html", "Box", "box geometry content")],
            postings={"box": [[0, 1]], "geometry": [[0, 1]],
                      "content": [[0, 1]]})
        self.path = _write_index(
            self.tmp.name, rag.INDEX_FILENAME, self.index_json)
        # 在索引同目录放一个「诱饵」文件，若 get_doc 回读源文件系统就会读到它
        self.decoy = _write(
            self.tmp.name, "decoy.html", "DECOY SECRET CONTENT")

    def tearDown(self):
        rag.clear_cache()
        self.tmp.cleanup()

    def test_existing_path_returns_embedded_content(self):
        env = rag.get_doc(
            "nodes/sop/box.html", index_path=self.path,
            response_cap_fn=None, max_bytes=10 ** 8)
        self.assertEqual(env["status"], "success")
        self.assertIn("box geometry content", env["content"])
        self.assertEqual(env["returned"], 1)

    def test_nonexistent_path_returns_not_found(self):
        env = rag.get_doc(
            "does/not/exist.html", index_path=self.path,
            response_cap_fn=None, max_bytes=10 ** 8)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "rag_doc_not_found")
        self.assertEqual(env["returned"], 0)
        self.assertEqual(env["content"], "")

    def test_traversal_string_does_not_read_source_file(self):
        # 遍历串指向诱饵文件的实际路径；get_doc 不得回读它
        # 相对路径穿越到 decoy.html
        traversal = "../" * 0 + "decoy.html"
        env = rag.get_doc(
            traversal, index_path=self.path,
            response_cap_fn=None, max_bytes=10 ** 8)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "rag_doc_not_found")
        self.assertNotIn("DECOY SECRET CONTENT", env.get("content", ""))

    def test_absolute_path_does_not_read_source_file(self):
        abs_decoy = os.path.abspath(self.decoy)
        env = rag.get_doc(
            abs_decoy, index_path=self.path,
            response_cap_fn=None, max_bytes=10 ** 8)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "rag_doc_not_found")
        self.assertNotIn("DECOY SECRET CONTENT", env.get("content", ""))

    def test_empty_path_returns_error(self):
        env = rag.get_doc("", index_path=self.path)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "rag_doc_not_found")

    def test_non_string_path_returns_error(self):
        env = rag.get_doc(None, index_path=self.path)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "rag_doc_not_found")

    def test_missing_index_returns_rag_index_missing(self):
        env = rag.get_doc(
            "nodes/sop/box.html",
index_path=os.path.join(self.tmp.name, "absent.json"))
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "rag_index_missing")


# ---------------------------------------------------------------------------
# bridge envelope shape 与 stale _index_warning
# ---------------------------------------------------------------------------
class BridgeEnvelopeTests(unittest.TestCase):

    def setUp(self):
        rag.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.index_json = _make_index_json(
            [_doc(0, "a.html", "A", "box geometry"),
             _doc(1, "b.html", "B", "sphere geometry")],
            postings={"box": [[0, 1]], "sphere": [[1, 1]],
                      "geometry": [[0, 1], [1, 1]]})
        self.path = _write_index(
            self.tmp.name, rag.INDEX_FILENAME, self.index_json)

    def tearDown(self):
        rag.clear_cache()
        self.tmp.cleanup()

    def test_search_success_envelope(self):
        env = rag.search_docs(
            "box", index_path=self.path, response_cap_fn=None, max_bytes=10 ** 8)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["query"], "box")
        self.assertEqual(env["matched"], 1)
        self.assertEqual(env["returned"], len(env["results"]))

    def test_search_missing_index_envelope(self):
        env = rag.search_docs(
            "box", index_path=os.path.join(self.tmp.name, "absent.json"))
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "rag_index_missing")
        self.assertEqual(env["matched"], 0)
        self.assertEqual(env["returned"], 0)
        self.assertEqual(env["results"], [])

    def test_search_unavailable_index_envelope(self):
        bad_path = _write_index(self.tmp.name, "bad.json", "{ broken")
        env = rag.search_docs("box", index_path=bad_path)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "rag_index_unavailable")

    def test_stale_response_carries_index_warning(self):
        # 先成功加载
        rag.load_index(self.path)
        # 写坏 + 推进 mtime
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ broken")
        os.utime(self.path, ns=(10 ** 18, 10 ** 18))
        env = rag.search_docs(
            "box", index_path=self.path, response_cap_fn=None, max_bytes=10 ** 8)
        self.assertEqual(env["status"], "success")
        self.assertIn("_index_warning", env)
        self.assertTrue(env["_index_warning"])


if __name__ == "__main__":
    unittest.main()
