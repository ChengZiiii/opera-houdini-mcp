"""_rag.py — 纯 stdlib BM25 跨文档检索（RAG）引擎与安全加载。

本模块与 ``_help.py`` 的 ``get_houdini_help`` / ``verify_hou_api`` **互补**
而非替换：那两个工具面向单条 API / 节点结构化查询，并保持
local-help-first + 在线回退；本模块面向「跨文档主题检索」，从已校验的
本地 JSON 索引读取，**不建立 Houdini 连接，不回读索引外源文件**。

设计要点（tasks 1.1-1.7）：
- 仅依赖 Python 标准库（R4 零新增 pip）。不在顶层 import ``hou``，绝不
  使用 pickle 或任何可执行反序列化格式。
- ``HoudiniTokenizer``：按优先级保留 ``hou.xxx()`` API 与 ``/obj/geo1``
  节点路径的整体语义；``snake_case`` 复合词同时保留完整 token 与拆分
  token；普通词统一小写、过滤短词与停用词（task 1.2）。
- ``BM25Index``：Okapi BM25（``k1=1.5``、``b=0.75``、+1 平滑 IDF），
  ``score/search`` 按 score 降序、同分按 doc_id 升序稳定排序（task 1.3）。
- 版本化 JSON schema ``houdinimcp.rag-index`` version 1（task 1.4）：
  逐字段校验类型、非负计数、唯一文档 id/path、posting 引用必须存在。
- ``_index_path()``：默认 ``index.v1.json``，支持
  ``HOUDINI_MCP_RAG_INDEX_DIR`` 覆盖（task 1.5）。
- path + mtime_ns + size cache（task 1.6）：缺失/损坏/不兼容返回结构化
  status；**坏的新文件绝不得替换进程内最后一次已校验缓存**；同 path
  有已校验缓存时降级为 stale success（响应附 ``_index_warning``）。
- 命中位置中心 snippet（task 1.7）：在大小写不敏感的正文中寻找最早 query
  token 命中，以命中位置为中心取最多 300 字符；无可定位命中时回退正文开头。
"""

import json
import math
import os
import re

try:
    from . import _common as cmn
except ImportError:
    try:
        import _common as cmn
    except ImportError:
        cmn = None


# ---------------------------------------------------------------------------
# 常量与 schema（tasks 1.4 / 1.5）
# ---------------------------------------------------------------------------
SCHEMA_NAME = "houdinimcp.rag-index"
SCHEMA_VERSION = 1

INDEX_FILENAME = "index.v1.json"

DEFAULT_MAX_BYTES = 16384
SNIPPET_MAX = 300
SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 50
SEARCH_LIMIT_DEFAULT = 10

BM25_K1 = 1.5
BM25_B = 0.75


# ---------------------------------------------------------------------------
# 统一 error 类型（仅用于内部解析/校验失败；bridge 层不向调用方抛）
# ---------------------------------------------------------------------------
class RagIndexError(Exception):
    """索引解析或 schema 校验失败的稳定 code/message/details。"""

    def __init__(self, code, message, details=None):
        super(RagIndexError, self).__init__(message)
        self.code = code
        self.message = message
        self.details = details if isinstance(details, dict) else {}


# ---------------------------------------------------------------------------
# HoudiniTokenizer（task 1.2）
# ---------------------------------------------------------------------------
# 优先级 1：hou.xxx.yyy API 调用（含 hou.session / hou.node 等）
_HOU_API_RE = re.compile(r"hou(?:\.[a-z_][a-z0-9_]*)+")
# 优先级 2：节点路径 /obj/geo1/box1（大小写不敏感，至少两段）
_NODE_PATH_RE = re.compile(r"(?:^|[^a-z0-9_])((?:/[a-z0-9_]+){2,})", re.IGNORECASE)
# 优先级 3：单词 / snake_case 复合（剩余文本）
_WORD_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")

STOPWORDS = frozenset((
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "for",
    "of", "to", "in", "on", "at", "by", "with", "from", "as", "is", "are",
    "was", "were", "be", "been", "being", "this", "that", "these", "those",
    "it", "its", "do", "does", "did", "will", "would", "can", "could",
    "should", "shall", "may", "might", "you", "your", "they", "them",
    "their", "we", "our", "us", "i", "me", "my", "not", "no", "so", "than",
    "too", "very", "just", "about", "above", "after", "again", "all", "any",
    "each", "few", "more", "most", "other", "out", "over", "own", "same",
    "some", "such", "up", "down", "into", "through", "during", "before",
    "below", "between", "under", "further", "once", "here", "there",
    "when", "where", "why", "how", "both", "few", "off", "because", "while",
    "against", "within", "without",
))

# 短词过滤阈值：长度 < 2 的 token 一律丢弃（单字符噪声）
_MIN_TOKEN_LEN = 2


def tokenize(text):
    """把文本切分为稳定小写 token 列表。

    优先级：
    1. ``hou.xxx.yyy`` API 整体保留。
    2. ``/obj/geo1/box1`` 节点路径整体保留。
    3. ``snake_case_compound`` 同时保留完整 token 与下划线拆分子词。
    4. 其余普通单词。

    所有 token 小写；长度 < 2 或命中停用词的 token 丢弃。hou.xxx 与节点
    路径整体 token 不会被停用词过滤（它们与任何单字停用词都不相等）。
    """
    if not isinstance(text, str):
        return []
    work = text.lower()
    tokens = []

    # 1. hou API
    for match in _HOU_API_RE.finditer(work):
        tokens.append(match.group())
    work = _HOU_API_RE.sub(" ", work)

    # 2. 节点路径（正则带前导字符捕获，需取 group(1)）
    for match in _NODE_PATH_RE.finditer(work):
        tokens.append(match.group(1).lower())
    work = _NODE_PATH_RE.sub(lambda m: m.group(0)[0] if m.group(0)[0] != "/" else " ", work)
    # 兜底：清理可能残留的孤立斜杠段
    work = re.sub(r"/[a-z0-9_]+", " ", work)

    # 3/4. 单词与 snake_case
    for match in _WORD_RE.finditer(work):
        tok = match.group()
        if "_" in tok:
            tokens.append(tok)
            for part in tok.split("_"):
                if part:
                    tokens.append(part)
        else:
            tokens.append(tok)

    # 过滤短词与停用词
    result = []
    for tok in tokens:
        if len(tok) < _MIN_TOKEN_LEN:
            continue
        if tok in STOPWORDS:
            continue
        result.append(tok)
    return result


# ---------------------------------------------------------------------------
# BM25Index（task 1.3）
# ---------------------------------------------------------------------------
class BM25Index(object):
    """运行时 BM25 索引：从已校验 JSON 构造，提供 score/search 与 path 精确查找。

    构造后只读；不持有源文件句柄。``search`` 接受 query 字符串或预切分
    token 列表，返回 ``(doc_id, score)`` 列表（score 降序、doc_id 升序），
    只包含 BM25 正分文档。``limit`` 为 ``None`` 返回全部正分文档。
    """

    def __init__(self, docs_by_id, postings, avgdl, document_count,
                 k1=BM25_K1, b=BM25_B):
        self.docs_by_id = docs_by_id
        self.postings = postings
        self.avgdl = float(avgdl) if avgdl else 0.0
        self.document_count = document_count
        self.k1 = k1
        self.b = b
        # path -> doc_id 精确查找索引（get_doc 使用）
        self._paths = {}
        for doc_id, doc in docs_by_id.items():
            self._paths[doc["path"]] = doc_id

    def search(self, query, limit=None):
        """返回 [(doc_id, score), ...]，仅正分，score 降序、doc_id 升序。

        ``query`` 可为字符串（内部 tokenize）或 token 列表。
        """
        if isinstance(query, str):
            q_tokens = tokenize(query)
        elif isinstance(query, (list, tuple)):
            q_tokens = list(query)
        else:
            q_tokens = []

        scores = {}
        seen_terms = set()
        for term in q_tokens:
            if term in seen_terms:
                continue
            seen_terms.add(term)
            plist = self.postings.get(term)
            if not plist:
                continue
            df = len(plist)
            # +1 平滑 IDF：log((N - df + 0.5)/(df + 0.5) + 1) 恒正
            idf = math.log(
                (self.document_count - df + 0.5) / (df + 0.5) + 1)
            for doc_id, tf in plist.items():
                doc = self.docs_by_id.get(doc_id)
                if doc is None:
                    continue
                dl = doc["length"]
                if self.avgdl > 0:
                    denom = tf + self.k1 * (
                        1 - self.b + self.b * (dl / self.avgdl))
                else:
                    denom = tf + self.k1
                if denom <= 0:
                    continue
                contrib = idf * (tf * (self.k1 + 1)) / denom
                scores[doc_id] = scores.get(doc_id, 0.0) + contrib

        matched = [(doc_id, score)
                   for doc_id, score in scores.items() if score > 0]
        # 稳定排序：score 降序，同分按 doc_id 升序
        matched.sort(key=lambda pair: (-pair[1], pair[0]))
        if limit is not None:
            if limit < 0:
                limit = 0
            matched = matched[:limit]
        return matched

    def get_doc_by_path(self, path):
        """对已校验索引中的规范化 POSIX 相对 path 做精确匹配。

        不拼接源文件系统路径，不回读索引外文件。返回 doc dict 或 None。
        """
        if not isinstance(path, str):
            return None
        doc_id = self._paths.get(path)
        if doc_id is None:
            return None
        return self.docs_by_id[doc_id]


# ---------------------------------------------------------------------------
# snippet（task 1.7）
# ---------------------------------------------------------------------------
def make_snippet(content, query_tokens, max_chars=SNIPPET_MAX):
    """围绕首个 query token 命中位置截取最多 ``max_chars`` 字符的 snippet。

    - 在大小写不敏感的正文中寻找最早 query token 命中位置。
    - 命中时以命中位置为中心取窗口，前后从正文中部截取时加 ``...`` 省略
      标记，并用稳定标记 ``«»`` 突出命中 token。
    - 无可定位命中时回退正文开头（前 ``max_chars`` 字符，超长加尾部省略）。
    """
    if not isinstance(content, str) or not content:
        return ""
    if not isinstance(query_tokens, (list, tuple)):
        query_tokens = []

    lower = content.lower()
    earliest = None
    hit_len = 0
    for tok in query_tokens:
        if not isinstance(tok, str) or not tok:
            continue
        needle = tok.lower()
        idx = lower.find(needle)
        if idx >= 0 and (earliest is None or idx < earliest):
            earliest = idx
            hit_len = len(needle)

    if earliest is None:
        # 回退正文开头
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + " ..."

    # 以命中位置为中心取窗口
    center = earliest + hit_len // 2
    half = max_chars // 2
    start = max(0, center - half)
    end = min(len(content), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    window = content[start:end]
    prefix = "... " if start > 0 else ""
    suffix = " ..." if end < len(content) else ""

    # 突出命中 token（在窗口内的相对位置）
    rel = earliest - start
    if 0 <= rel and rel + hit_len <= len(window):
        marked = (window[:rel] + "\u00ab" + window[rel:rel + hit_len]
                  + "\u00bb" + window[rel + hit_len:])
        return prefix + marked + suffix
    return prefix + window + suffix


# ---------------------------------------------------------------------------
# 版本化 JSON schema 校验（task 1.4）
# ---------------------------------------------------------------------------
def _check_is_int(value, allow_bool=False):
    if isinstance(value, bool):
        return allow_bool
    return isinstance(value, int)


def _validate_index(raw_text):
    """解析 + 校验 JSON 文本，返回 ``BM25Index``；失败抛 ``RagIndexError``。

    校验项：schema/version 精确值、顶层与文档字段类型、非负计数、唯一
    id/path、posting 只引用已存在文档。未知顶层字段可忽略；任何解析或
    校验失败都不得构造 BM25Index。
    """
    try:
        data = json.loads(raw_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RagIndexError(
            "rag_index_bad_json", "JSON decode failed: {0}".format(exc),
            {"exception": str(exc)})
    if not isinstance(data, dict):
        raise RagIndexError(
            "rag_index_bad_schema", "top-level must be object",
            {"type": type(data).__name__})

    schema = data.get("schema")
    if schema != SCHEMA_NAME:
        raise RagIndexError(
            "rag_index_bad_schema", "schema mismatch",
            {"expected": SCHEMA_NAME, "got": schema})

    version = data.get("version")
    if not _check_is_int(version):
        raise RagIndexError(
            "rag_index_bad_version", "version must be int",
            {"got": version, "type": type(version).__name__})
    if version != SCHEMA_VERSION:
        raise RagIndexError(
            "rag_index_bad_version", "unsupported version",
            {"supported": SCHEMA_VERSION, "got": version})

    built_at = data.get("built_at")
    if not isinstance(built_at, str):
        raise RagIndexError(
            "rag_index_bad_schema", "built_at must be string",
            {"type": type(built_at).__name__ if built_at is not None else "missing"})

    source = data.get("source")
    if not isinstance(source, str):
        raise RagIndexError(
            "rag_index_bad_schema", "source must be string",
            {"type": type(source).__name__ if source is not None else "missing"})

    document_count = data.get("document_count")
    if not _check_is_int(document_count) or document_count < 0:
        raise RagIndexError(
            "rag_index_bad_schema",
            "document_count must be non-negative int",
            {"got": document_count})

    avgdl = data.get("avgdl")
    if isinstance(avgdl, bool) or not isinstance(avgdl, (int, float)) or avgdl < 0:
        raise RagIndexError(
            "rag_index_bad_schema", "avgdl must be non-negative number",
            {"got": avgdl})

    documents = data.get("documents")
    if not isinstance(documents, list):
        raise RagIndexError(
            "rag_index_bad_schema", "documents must be list",
            {"type": type(documents).__name__})
    if len(documents) != document_count:
        raise RagIndexError(
            "rag_index_bad_schema", "document_count mismatch",
            {"expected": document_count, "actual": len(documents)})

    docs_by_id = {}
    paths = set()
    for i, doc in enumerate(documents):
        if not isinstance(doc, dict):
            raise RagIndexError(
                "rag_index_bad_schema",
                "document[{0}] must be object".format(i), {"index": i})
        doc_id = doc.get("id")
        if not _check_is_int(doc_id):
            raise RagIndexError(
                "rag_index_bad_schema",
                "document[{0}] id must be int".format(i),
                {"index": i, "id": doc_id})
        if doc_id in docs_by_id:
            raise RagIndexError(
                "rag_index_bad_schema", "duplicate document id",
                {"id": doc_id})
        path = doc.get("path")
        if not isinstance(path, str) or not path:
            raise RagIndexError(
                "rag_index_bad_schema",
                "document[{0}] path must be non-empty string".format(i),
                {"index": i, "path": path})
        if path in paths:
            raise RagIndexError(
                "rag_index_bad_schema", "duplicate document path",
                {"path": path})
        paths.add(path)
        title = doc.get("title")
        if not isinstance(title, str):
            raise RagIndexError(
                "rag_index_bad_schema",
                "document[{0}] title must be string".format(i),
                {"index": i})
        length = doc.get("length")
        if not _check_is_int(length) or length < 0:
            raise RagIndexError(
                "rag_index_bad_schema",
                "document[{0}] length must be non-negative int".format(i),
                {"index": i, "length": length})
        content = doc.get("content")
        if not isinstance(content, str):
            raise RagIndexError(
                "rag_index_bad_schema",
                "document[{0}] content must be string".format(i),
                {"index": i})
        docs_by_id[doc_id] = {
            "id": doc_id, "path": path, "title": title,
            "length": length, "content": content,
        }

    postings_raw = data.get("postings")
    if not isinstance(postings_raw, dict):
        raise RagIndexError(
            "rag_index_bad_schema", "postings must be object",
            {"type": type(postings_raw).__name__})
    postings = {}
    for term, plist in postings_raw.items():
        if not isinstance(term, str) or not term:
            raise RagIndexError(
                "rag_index_bad_schema",
                "posting term must be non-empty string", {"term": term})
        if not isinstance(plist, list):
            raise RagIndexError(
                "rag_index_bad_schema", "posting list must be list",
                {"term": term})
        entries = {}
        for entry in plist:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise RagIndexError(
                    "rag_index_bad_schema",
                    "posting entry must be [doc_id, tf]",
                    {"term": term, "entry": entry})
            doc_id, tf = entry[0], entry[1]
            if not _check_is_int(doc_id):
                raise RagIndexError(
                    "rag_index_bad_schema",
                    "posting doc_id must be int",
                    {"term": term, "doc_id": doc_id})
            if not _check_is_int(tf) or tf < 0:
                raise RagIndexError(
                    "rag_index_bad_schema",
                    "posting tf must be non-negative int",
                    {"term": term, "tf": tf})
            if doc_id not in docs_by_id:
                raise RagIndexError(
                    "rag_index_bad_schema", "dangling posting doc_id",
                    {"term": term, "doc_id": doc_id})
            if doc_id in entries:
                raise RagIndexError(
                    "rag_index_bad_schema",
                    "duplicate posting doc_id within term",
                    {"term": term, "doc_id": doc_id})
            entries[doc_id] = tf
        postings[term] = entries

    return BM25Index(docs_by_id, postings, avgdl, document_count)


# ---------------------------------------------------------------------------
# 索引路径与 cache（tasks 1.5 / 1.6）
# ---------------------------------------------------------------------------
# cache key = (absolute_path, st_mtime_ns, st_size) -> BM25Index
_CACHE = {}
# path -> 最后一次已校验成功的 BM25Index（stale 降级用）
_LAST_GOOD = {}


def clear_cache():
    """清空进程内 cache（测试 / 显式失效用）。"""
    _CACHE.clear()
    _LAST_GOOD.clear()


def _index_path(path=None):
    """解析索引文件绝对路径。

    - 显式 ``path`` 直接 abspath。
    - ``HOUDINI_MCP_RAG_INDEX_DIR`` 环境变量覆盖目录，文件名固定
      ``index.v1.json``。
    - 默认：``_rag.py`` 模块同目录。
    """
    if path is not None:
        if not isinstance(path, str):
            raise RagIndexError(
                "rag_index_bad_path", "path must be string", {"path": path})
        return os.path.abspath(path)
    env_dir = os.environ.get("HOUDINI_MCP_RAG_INDEX_DIR")
    if env_dir:
        return os.path.abspath(os.path.join(env_dir, INDEX_FILENAME))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), INDEX_FILENAME)


def _lookup_last_good(path):
    return _LAST_GOOD.get(path)


def _stale_status(path, index, reason):
    return {
        "state": "stale",
        "index": index,
        "reason": reason,
        "path": path,
        "warning": "index file unreadable/invalid; serving last validated cache",
    }


def load_index(path=None):
    """加载并校验 RAG 索引；返回结构化 status dict（不向 bridge 抛异常）。

    state 取值：
    - ``"ok"``：从磁盘成功加载并校验。
    - ``"missing"``：文件不存在且无 last-good 缓存。
    - ``"unavailable"``：文件存在但不可读 / schema 不兼容，且无 last-good 缓存。
    - ``"stale"``：文件不可读 / 不兼容，但同 path 有已校验缓存可降级服务。

    **坏的新文件绝不得替换 ``_CACHE`` / ``_LAST_GOOD``**：只有在成功校验
    后才写入这两个表。
    """
    try:
        resolved = _index_path(path)
    except RagIndexError as exc:
        return {"state": "unavailable", "index": None, "reason": exc.message,
                "path": "", "warning": ""}

    try:
        stat_result = os.stat(resolved)
    except FileNotFoundError:
        last_good = _lookup_last_good(resolved)
        if last_good is not None:
            return _stale_status(resolved, last_good,
                                 "file missing; using last validated cache")
        return {"state": "missing", "index": None,
                "reason": "index file not found", "path": resolved,
                "warning": ""}
    except OSError as exc:
        last_good = _lookup_last_good(resolved)
        if last_good is not None:
            return _stale_status(resolved, last_good,
                                 "stat failed: {0}".format(exc))
        return {"state": "unavailable", "index": None,
                "reason": "stat failed: {0}".format(exc), "path": resolved,
                "warning": ""}

    key = (resolved, stat_result.st_mtime_ns, stat_result.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return {"state": "ok", "index": cached, "reason": "",
                "path": resolved, "warning": ""}

    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
    except UnicodeDecodeError as exc:
        last_good = _lookup_last_good(resolved)
        if last_good is not None:
            return _stale_status(resolved, last_good,
                                 "decode failed: {0}".format(exc))
        return {"state": "unavailable", "index": None,
                "reason": "decode failed: {0}".format(exc), "path": resolved,
                "warning": ""}
    except OSError as exc:
        last_good = _lookup_last_good(resolved)
        if last_good is not None:
            return _stale_status(resolved, last_good,
                                 "read failed: {0}".format(exc))
        return {"state": "unavailable", "index": None,
                "reason": "read failed: {0}".format(exc), "path": resolved,
                "warning": ""}

    try:
        index = _validate_index(raw_text)
    except RagIndexError as exc:
        last_good = _lookup_last_good(resolved)
        if last_good is not None:
            return _stale_status(resolved, last_good,
                                 "validation failed: {0}: {1}".format(
                                     exc.code, exc.message))
        return {"state": "unavailable", "index": None,
                "reason": "{0}: {1}".format(exc.code, exc.message),
                "path": resolved, "warning": ""}

    # 成功：才允许写 cache。坏文件永远不会污染缓存。
    _CACHE[key] = index
    _LAST_GOOD[resolved] = index
    return {"state": "ok", "index": index, "reason": "",
            "path": resolved, "warning": ""}


# ---------------------------------------------------------------------------
# bridge 入口（tasks 3.x；不建立 Houdini 连接）
# ---------------------------------------------------------------------------
def _clamp_limit(limit):
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        limit_int = SEARCH_LIMIT_DEFAULT
    if limit_int < SEARCH_LIMIT_MIN:
        limit_int = SEARCH_LIMIT_MIN
    if limit_int > SEARCH_LIMIT_MAX:
        limit_int = SEARCH_LIMIT_MAX
    return limit_int


def _apply_cap(result, response_cap_fn, max_bytes):
    """对 result 过 apply_response_cap；返回最终 dict（不抛异常）。"""
    cap_fn = response_cap_fn
    if cap_fn is None and cmn is not None:
        cap_fn = getattr(cmn, "apply_response_cap", None)
    if not callable(cap_fn):
        return result
    try:
        capped = cap_fn(result, max_bytes)
    except Exception:
        return result
    if isinstance(capped, dict):
        return capped
    return result


def _missing_envelope(status, path=None):
    env = {
        "status": "error",
        "error": {
            "code": "rag_index_missing",
            "message": status.get("reason", "index file not found"),
            "details": {"index_path": status.get("path", "")},
        },
        "matched": 0,
        "returned": 0,
        "results": [],
    }
    if path is not None:
        env["path"] = path
        env["content"] = ""
    return env


def _unavailable_envelope(status, path=None):
    env = {
        "status": "error",
        "error": {
            "code": "rag_index_unavailable",
            "message": status.get("reason", "index unavailable"),
            "details": {"index_path": status.get("path", "")},
        },
        "matched": 0,
        "returned": 0,
        "results": [],
    }
    if path is not None:
        env["path"] = path
        env["content"] = ""
    return env


def search_docs(query, limit=SEARCH_LIMIT_DEFAULT, index_path=None,
                response_cap_fn=None, max_bytes=DEFAULT_MAX_BYTES):
    """bridge-local 入口：load -> search -> snippet -> cap -> 统一 envelope。

    本函数 **不建立 Houdini TCP 连接**（task 3.1）。所有文档内容只从已
    校验索引的内嵌 content 读取。

    返回 envelope：
    - success: ``status``/``query``/``limit``/``matched``（cap 前正分总数）/
      ``returned``（cap 后实际 results 长度，恒等于 ``len(results)``）/
      ``results``（每条含 path/title/score/snippet）。stale 时附
      ``_index_warning``。
    - error: ``status="error"`` + ``error={code,message,details}``；缺失
      -> ``rag_index_missing``；损坏/不兼容 -> ``rag_index_unavailable``。
    """
    limit_int = _clamp_limit(limit)

    status = load_index(index_path)
    state = status["state"]
    if state == "missing":
        return _missing_envelope(status)
    if state == "unavailable":
        return _unavailable_envelope(status)

    index = status["index"]
    query_str = query if isinstance(query, str) else ""
    q_tokens = tokenize(query_str)

    # matched = cap 前所有 BM25 正分文档数
    matched_pairs = index.search(q_tokens)
    matched_total = len(matched_pairs)

    # limit 形成候选列表
    candidate_pairs = matched_pairs[:limit_int]

    results = []
    for doc_id, score in candidate_pairs:
        doc = index.docs_by_id[doc_id]
        results.append({
            "path": doc["path"],
            "title": doc["title"],
            "score": score,
            "snippet": make_snippet(doc["content"], q_tokens),
        })

    result = {
        "status": "success",
        "query": query_str,
        "limit": limit_int,
        "matched": matched_total,
        "returned": len(results),
        "results": results,
    }
    if state == "stale":
        result["_index_warning"] = status.get(
            "warning", "index stale; using last validated cache")

    # 整个响应过 apply_response_cap（task 3.3 / R6）
    result = _apply_cap(result, response_cap_fn, max_bytes)
    # cap 后必须按实际 results 重算 returned（task 3.3）
    final_results = result.get("results")
    if not isinstance(final_results, list):
        final_results = []
        result["results"] = final_results
    result["returned"] = len(final_results)
    return result


def get_doc(path, index_path=None, response_cap_fn=None,
            max_bytes=DEFAULT_MAX_BYTES):
    """bridge-local 入口：load -> 精确 path 查找 -> cap -> 统一 envelope。

    本函数 **不建立 Houdini TCP 连接**（task 3.5）。``path`` 只与已校验
    索引中的规范化 POSIX 相对 path 做精确匹配，全文从 JSON 内嵌 content
    返回；**绝不拼接源文件系统路径或回读索引外文件**（含 ``..`` 遍历串）。
    """
    if not isinstance(path, str) or not path:
        return {
            "status": "error",
            "error": {
                "code": "rag_doc_not_found",
                "message": "path must be non-empty string",
                "details": {"path": path},
            },
            "path": path if isinstance(path, str) else "",
            "content": "",
            "returned": 0,
        }

    status = load_index(index_path)
    state = status["state"]
    if state == "missing":
        return _missing_envelope(status, path=path)
    if state == "unavailable":
        return _unavailable_envelope(status, path=path)

    index = status["index"]
    doc = index.get_doc_by_path(path)
    if doc is None:
        return {
            "status": "error",
            "error": {
                "code": "rag_doc_not_found",
                "message": "path not present in index",
                "details": {"path": path},
            },
            "path": path,
            "content": "",
            "returned": 0,
        }

    result = {
        "status": "success",
        "path": doc["path"],
        "title": doc["title"],
        "length": doc["length"],
        "content": doc["content"],
        "returned": 1,
    }
    if state == "stale":
        result["_index_warning"] = status.get("warning", "")

    # 全文过 apply_response_cap（task 3.5 / R6）
    result = _apply_cap(result, response_cap_fn, max_bytes)
    # returned 反映 content 是否存活
    content = result.get("content")
    if isinstance(content, str) and content:
        result["returned"] = 1
    else:
        result["returned"] = 0
    return result
