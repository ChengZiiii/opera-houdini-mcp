#!/usr/bin/env python
"""build_rag_index.py — 递归扫描 Houdini help HTML 构建 RAG JSON 索引。

独立脚本（可由系统 Python 或 hython 直接运行），**不复用、不修改、不
替换** ``_help.py`` 的 ``SideFXDocParser`` 与 local-help-first 查询路径
（task 2.2 / R8）。本脚本自带仅用于批量索引的 stdlib ``HTMLParser``
子类，提取 ``<title>`` 与可见正文，跳过 ``script/style/noscript``。

设计要点（tasks 2.1-2.6）：
- 仅 stdlib（R4）。可由系统 Python / hython 独立运行。
- 扫描源优先级：``HOUDINI_MCP_RAG_SOURCE`` > ``$HFS/houdini/help``。
- 递归 ``**/*.html``，保留稳定 POSIX 相对路径作为文档 identity。
- 构建 documents/postings/avgdl/document_count，全文内嵌 JSON。
- 原子发布（task 2.5）：同目录写唯一临时文件，flush + ``os.fsync()`` 后
  ``os.replace()``；写入/替换失败保留旧索引并 best-effort 清临时文件。
- 源目录缺失时 graceful 退出并给出配置提示（task 2.6）。

运行示例：
    python external/houdinimcp/scripts/build_rag_index.py
    set HOUDINI_MCP_RAG_SOURCE=C:/HFS/houdini/help && python build_rag_index.py
    hython external/houdinimcp/scripts/build_rag_index.py
"""
import datetime
import io
import json
import os
import sys
import tempfile
from html.parser import HTMLParser

# 让脚本既能从 scripts/ 单独运行，也能 -m 加载：把 fork 根目录
# （scripts 的父目录，即 houdinimcp/）加进 sys.path，使其内的 _rag 可
# 被 flat import 找到。
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

try:
    import _rag
except ImportError:
    sys.path.insert(0, _HERE)
    import _rag


SKIP_TAGS = frozenset(("script", "style", "noscript"))
TITLE_TAG = "title"
INDEX_FILENAME = _rag.INDEX_FILENAME


# ---------------------------------------------------------------------------
# 独立 HTML 正文解析器（task 2.2）
# ---------------------------------------------------------------------------
class HTMLBodyParser(HTMLParser):
    """提取 ``<title>`` 与可见正文；忽略 ``script/style/noscript``。

    与 ``_help.py::SideFXDocParser`` 完全独立：本解析器只关心批量索引
    所需的「title + 任意可见正文」，不做 SideFX 文档结构化字段提取。
    """

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title_parts = []
        self._body_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == TITLE_TAG and self._skip_depth == 0:
            self._in_title = True

    def handle_startendtag(self, tag, attrs):
        # 自闭合标签（如 <br/>）：若属于 skip 集合则不进入 data 收集；
        # title 自闭合无意义，忽略。
        pass

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag == TITLE_TAG:
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._body_parts.append(data)

    def get_title(self):
        return _collapse_ws("".join(self._title_parts))

    def get_body(self):
        return _collapse_ws("".join(self._body_parts))


def _collapse_ws(text):
    """折叠连续空白（含换行）为单个空格。"""
    if not text:
        return ""
    return " ".join(text.split())


def parse_html(text):
    """解析 HTML 文本，返回 ``(title, body)``。

    HTMLParser 对畸形 HTML 较宽容；任何解析异常都视为空正文，不抛。
    """
    parser = HTMLBodyParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        pass
    return parser.get_title(), parser.get_body()


# ---------------------------------------------------------------------------
# 递归扫描 + 索引构建（tasks 2.3 / 2.4）
# ---------------------------------------------------------------------------
def find_html_files(source_root):
    """递归扫描 ``source_root`` 下 ``**/*.html``。

    返回 ``[(posix_rel_path, abs_path), ...]``，按 POSIX 相对路径稳定排序。
    """
    results = []
    source_root = os.path.abspath(source_root)
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.lower().endswith(".html"):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, source_root)
            posix_rel = rel.replace(os.sep, "/")
            results.append((posix_rel, abs_path))
    results.sort(key=lambda pair: pair[0])
    return results


def build_index(source_root):
    """扫描 HTML 并构造符合 ``houdinimcp.rag-index`` v1 schema 的 dict。"""
    files = find_html_files(source_root)
    documents = []
    postings = {}  # term -> {doc_id: tf}
    total_length = 0

    for doc_id, (posix_rel, abs_path) in enumerate(files):
        try:
            with io.open(abs_path, "r", encoding="utf-8",
                         errors="replace") as handle:
                html_text = handle.read()
        except OSError:
            continue
        title, body = parse_html(html_text)
        combined = (title + "\n" + body) if title else body
        tokens = _rag.tokenize(combined)
        length = len(tokens)

        doc_tf = {}
        for tok in tokens:
            doc_tf[tok] = doc_tf.get(tok, 0) + 1
        for term, tf in doc_tf.items():
            postings.setdefault(term, {})[doc_id] = tf

        documents.append({
            "id": doc_id,
            "path": posix_rel,
            "title": title,
            "length": length,
            "content": body,
        })
        total_length += length

    document_count = len(documents)
    avgdl = (total_length / document_count) if document_count > 0 else 0.0

    postings_out = {}
    for term, doc_map in postings.items():
        plist = [[doc_id, tf] for doc_id, tf in doc_map.items()]
        plist.sort(key=lambda pair: pair[0])
        postings_out[term] = plist

    built_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    index = {
        "schema": _rag.SCHEMA_NAME,
        "version": _rag.SCHEMA_VERSION,
        "built_at": built_at,
        "source": "build_rag_index.py from {0}".format(
            os.path.abspath(source_root)),
        "document_count": document_count,
        "avgdl": avgdl,
        "documents": documents,
        "postings": postings_out,
    }
    return index


# ---------------------------------------------------------------------------
# 源/输出目录解析（tasks 2.3 / 2.6）
# ---------------------------------------------------------------------------
def resolve_source():
    """扫描源优先级：``HOUDINI_MCP_RAG_SOURCE`` > ``$HFS/houdini/help``。

    返回存在的目录绝对路径，或 ``None``（缺失）。
    """
    env_src = os.environ.get("HOUDINI_MCP_RAG_SOURCE")
    if env_src and os.path.isdir(env_src):
        return os.path.abspath(env_src)
    hfs = os.environ.get("HFS")
    if hfs:
        candidate = os.path.join(hfs, "houdini", "help")
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return None


def resolve_index_dir():
    """输出目录：``HOUDINI_MCP_RAG_INDEX_DIR`` > ``_rag`` 模块目录。"""
    env_dir = os.environ.get("HOUDINI_MCP_RAG_INDEX_DIR")
    if env_dir:
        return env_dir
    return _PARENT


# ---------------------------------------------------------------------------
# 原子发布（task 2.5）
# ---------------------------------------------------------------------------
def publish_index(index, index_dir):
    """原子发布索引到 ``index_dir/index.v1.json``。

    在目标同目录写唯一临时文件，flush + ``os.fsync()`` 后
    ``os.replace(temp, final)`` 原子替换。写入或 replace 失败时保留旧
    索引，并 best-effort 删除临时文件。
    """
    if not os.path.isdir(index_dir):
        os.makedirs(index_dir, exist_ok=True)
    final_path = os.path.join(index_dir, INDEX_FILENAME)

    fd, tmp_path = tempfile.mkstemp(
        prefix=".index.v1.", suffix=".tmp", dir=index_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, final_path)
    except OSError:
        # 失败：best-effort 清理临时文件，保留旧索引
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    return final_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    source = resolve_source()
    if source is None:
        sys.stderr.write(
            "build_rag_index: no HTML source found.\n"
            "Set HOUDINI_MCP_RAG_SOURCE=<dir>, or run inside hython "
            "(HFS set),\nor point HOUDINI_MCP_RAG_INDEX_DIR at the "
            "output location.\n")
        return 2
    index_dir = resolve_index_dir()
    sys.stderr.write("build_rag_index: scanning {0}\n".format(source))
    index = build_index(source)
    try:
        final_path = publish_index(index, index_dir)
    except OSError as exc:
        sys.stderr.write("build_rag_index: publish failed: {0}\n".format(exc))
        return 3
    sys.stderr.write(
        "build_rag_index: wrote {0} ({1} docs, avgdl={2:.1f})\n".format(
            final_path, index["document_count"], index["avgdl"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
