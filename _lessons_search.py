"""_lessons_search.py — per-root BM25 检索与融合层（stdlib only，无 hou）。

本模块在 ``_lessons.py`` 的存储 / registry 之上叠加「检索与融合」：
跨全部可用 root 检索 published lessons 与各 root ``recipes/BEST_PRACTICES.md``，
返回紧凑摘要列表（tasks 3.1-3.4 / 6.5-6.6）。

设计要点：
- 仅依赖 Python 标准库（R4 零新增 pip），不在顶层 import ``hou``。
- tokenizer：复用 ``_rag.tokenize``（hou API / 节点路径 token），并补充中文
  CJK 连续串的 2-gram shingles（如 ``渲染失败`` → ``渲染失败/渲染/染失/失败``），
  单字 CJK 保留自身。
- per-root 独立 BM25（Okapi k1=1.5、b=0.75、+1 平滑 IDF），索引 JSON 缓存落在
  ``_lessons.cache_index_dir(root_name)/index.v1.json``：schema-versioned、
  source_sig（lessons/*.md + recipes/BEST_PRACTICES.md 的 mtime_ns+size）校验，
  corrupt / 语义损坏 / 不兼容 → 静默重建（重建不污染进程内已校验缓存）。
  同一 root 内重复 doc id 保留先出现者并记 ``_warning``。
- 融合评分：
  ``score = BM25 × 指纹精确命中(×2.0，仅 lesson) × (1+0.1×strength，仅 lesson)
  × 新鲜度衰减(仅 lesson，90 天起线性至 0.7 下限 180 天) × root.priority``；
  空查询 → 常数 1.0 基线。跨 ok root 合并，score 降序，tie-break priority
  降序再 id 升序。每条结果带 ``source_root``。
- draft **绝不进索引**（但 ``find_lesson_by_id`` 可读）；recipes 解析失败 →
  该 root recipes 跳过并附 ``_warning``，lessons 不受影响；header-only 的
  recipes 文件（无 ``### BP-NNN`` 块）视为无 recipes，**不**告警。
- 过滤：category / severity 精确、node_type（doc 文本子串）、houdini_version
  （affected_versions 子串），组合 AND。
- hint：lesson 结果其 fingerprint 在 inbox 累计 count>=3 → ``hint`` 字段
  （"已踩 N 次，请补充 fix"）；顶层 ``draft_suggestions`` 列出 count>=3 的
  draft 骨架。inbox 读取复用 ``_lessons._read_inbox`` / ``_record_count``。
- envelope：``{status, query, top_k, matched, returned_count, truncated,
  results, _warning?, draft_suggestions}``；``returned_count`` 恒等于
  ``len(results)``，整体过 ``apply_response_cap``（defense-in-depth）后重新
  对齐计数；unavailable root 附 ``_warning``，unconfigured 静默跳过。
- **绝不读取** fork 官方 submodule 的 ``BEST_PRACTICES.md``：只处理各 root 自
  身 ``recipes/BEST_PRACTICES.md``。
- 测试钩子：``_lessons._base_dir`` 可被 monkeypatch，缓存路径随之派生。
"""

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from datetime import datetime

try:
    from . import _common as cmn  # noqa: F401  (风格要求：响应 cap 备用)
except ImportError:
    try:
        import _common as cmn  # noqa: F401
    except ImportError:
        cmn = None

try:
    from . import _lessons
except ImportError:
    try:
        import _lessons
    except ImportError:
        _lessons = None

try:
    from . import _rag
except ImportError:
    try:
        import _rag
    except ImportError:
        _rag = None

try:
    from . import _best_practices
except ImportError:
    try:
        import _best_practices
    except ImportError:
        _best_practices = None


# ---------------------------------------------------------------------------
# 常量与 schema
# ---------------------------------------------------------------------------
SCHEMA_NAME = "houdinimcp.lessons-search"
SCHEMA_VERSION = 1

INDEX_FILENAME = "index.v1.json"
RECIPES_RELPATH = os.path.join("recipes", "BEST_PRACTICES.md")

# recipes 块 heading：文件含任意块才算有 recipes；纯 header 文件视为可空。
_RECIPE_HEADING_RE = re.compile(r"^###\s+BP-\d{3}\s*$", re.MULTILINE)

BM25_K1 = 1.5
BM25_B = 0.75

FINGERPRINT_BOOST = 2.0
STRENGTH_FACTOR = 0.1
FRESH_DAYS = 90.0            # <= 90 天 → 1.0
DECAY_END_DAYS = 180.0       # >= 180 天 → 0.7 下限
DECAY_FLOOR = 0.7

TOP_K_DEFAULT = 5
TOP_K_MIN = 1
TOP_K_MAX = 5
SUMMARY_MAX = 200            # symptom / fix 摘要截断字符数
DEFAULT_MAX_BYTES = 16384

DRAFT_SUGGESTIONS_MAX = 5       # draft_suggestions 最多返回条数

HINT_THRESHOLD = 3           # inbox 同 fingerprint 累计 >= 3 → hint / draft 建议
HINT_TEXT = "已踩 {0} 次，请补充 fix"

_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

# cache key = (cache 文件绝对路径, source_sig) -> index dict
_CACHE = {}


def clear_cache():
    """清空进程内索引缓存（测试 / 显式失效用）。"""
    _CACHE.clear()


def _now_iso():
    """当前本地时间 ISO 8601（含 tz offset）。"""
    return datetime.now().astimezone().isoformat()


# ---------------------------------------------------------------------------
# tokenizer：_rag 复用 + 中文 2-gram shingles
# ---------------------------------------------------------------------------
def _cjk_shingles(run):
    """单条 CJK 连续串的 token：整串 + 全部 2-gram；单字保留自身。"""
    if len(run) == 1:
        return [run]
    shingles = [run]
    shingles.extend(run[i:i + 2] for i in range(len(run) - 1))
    return shingles


def tokenize(text):
    """把文本切分为稳定小写 token 列表（_rag.tokenize + CJK shingles）。

    中文连续串（如 ``渲染失败``）同时产出整串与 2-gram（渲染/染失/失败），
    便于与文档侧同一 tokenizer 产出的 postings 对齐。
    """
    if not isinstance(text, str):
        return []
    tokens = []
    for run in _CJK_RUN_RE.findall(text):
        tokens.extend(_cjk_shingles(run))
    remainder = _CJK_RUN_RE.sub(" ", text)
    if _rag is not None:
        tokens.extend(_rag.tokenize(remainder))
    return tokens


# ---------------------------------------------------------------------------
# 新鲜度衰减
# ---------------------------------------------------------------------------
def freshness_decay(updated_at, now=None):
    """lesson 新鲜度衰减：90 天起线性衰减至 0.7 下限（180 天）。

    ``updated_at`` 接受 ISO 8601 字符串（带 tz）或 aware datetime；解析失败 /
    无时区 → 返回 1.0（不惩罚）。``now`` 默认当前本地时间。
    """
    if now is None:
        now = datetime.now().astimezone()
    if isinstance(updated_at, str):
        try:
            parsed = datetime.fromisoformat(updated_at)
        except ValueError:
            return 1.0
    else:
        parsed = updated_at
    if parsed is None or parsed.tzinfo is None:
        return 1.0
    try:
        age_days = (now - parsed).total_seconds() / 86400.0
    except (TypeError, ValueError):
        return 1.0
    if age_days <= FRESH_DAYS:
        return 1.0
    if age_days >= DECAY_END_DAYS:
        return DECAY_FLOOR
    return (1.0 - (1.0 - DECAY_FLOOR) * (age_days - FRESH_DAYS)
            / (DECAY_END_DAYS - FRESH_DAYS))


# ---------------------------------------------------------------------------
# 索引构建（per-root）
# ---------------------------------------------------------------------------
def _search_text_parts(values):
    """拼装可检索文本（join 去空），过滤层与 BM25 共用同一文本。"""
    return " ".join(str(v) for v in values if v is not None and str(v).strip())


def _lesson_doc(lesson):
    """published lesson → 索引 doc（含摘要渲染所需元数据）。"""
    search_text = _search_text_parts((
        lesson.get("title", ""), lesson.get("problem", ""),
        lesson.get("symptom", ""), lesson.get("fix", ""),
        lesson.get("category", ""), lesson.get("source", ""),
        lesson.get("affected_versions", ""),
        lesson.get("verified_versions", "")))
    return {
        "id": lesson["id"],
        "kind": "lesson",
        "title": lesson["title"],
        "category": lesson["category"],
        "severity": lesson["severity"],
        "symptom": lesson["symptom"],
        "fix": lesson["fix"],
        "verified_versions": lesson["verified_versions"],
        "affected_versions": lesson["affected_versions"],
        "fingerprint": lesson.get("fingerprint"),
        "strength": int(lesson.get("strength") or 1),
        "updated_at": lesson.get("updated_at"),
        "search_text": search_text,
        "length": 0,
    }


def _recipe_doc(recipe):
    """root recipes 条目 → 索引 doc（recipe 无 title，以 id 充作摘要 title）。"""
    search_text = _search_text_parts((
        recipe.get("problem", ""), recipe.get("symptom", ""),
        recipe.get("fix", ""), recipe.get("category", ""),
        recipe.get("source", ""), recipe.get("affected_versions", ""),
        recipe.get("verified_versions", "")))
    return {
        "id": recipe["id"],
        "kind": "recipe",
        "title": recipe["id"],
        "category": recipe["category"],
        "severity": recipe["severity"],
        "symptom": recipe["symptom"],
        "fix": recipe["fix"],
        "verified_versions": recipe["verified_versions"],
        "affected_versions": recipe["affected_versions"],
        "fingerprint": None,
        "strength": None,
        "updated_at": None,
        "search_text": search_text,
        "length": 0,
    }


def _load_root_recipes(root_path):
    """读取并解析 root 自身 recipes/BEST_PRACTICES.md。

    返回 ``(recipes, error_or_None)``：文件缺失 → 空列表；文件存在但**不含
    任何 ``### BP-NNN`` 块**（设计允许的 header-only 文件）→ 视为无
    recipes，返回空列表且**不**产生 warning；其余解析 / 读取失败 → 空列表
    + 错误消息（由调用方转为 ``_warning``，该 root 的 lessons 不受影响）。
    **绝不**读取 fork 官方 submodule 的 BEST_PRACTICES.md。
    """
    path = os.path.join(root_path, RECIPES_RELPATH)
    if not os.path.isfile(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return [], "无法读取: {0}".format(exc)
    if not _RECIPE_HEADING_RE.search(text):
        return [], None  # header-only（可空设计）：无 BP-NNN 块 = 无 recipes
    try:
        return _best_practices.parse_best_practices(text), None
    except _best_practices.BestPracticesError as exc:
        return [], exc.message


def _build_root_index(root_path, root_name):
    """从磁盘重建单 root 索引：published lessons ∪ recipes。

    返回 ``(index_dict, warnings)``：warnings 为字符串列表（构建期告警）。
    index_dict 含 ``docs``（列表，含 summary 元数据与 search_text）/
    ``postings`` / ``avgdl`` / ``document_count``。
    同一 root 内重复 doc id（手工编辑文件）保留先出现者（lessons 按文件名
    排序的先后），后续重复项跳过并记入 warnings —— 索引内 id 保证唯一。
    """
    lessons, _lesson_errors = _lessons.load_root_lessons(root_path)
    docs = []
    seen_ids = set()
    warnings = []
    for lesson in lessons:
        if lesson["status"] != "published":
            continue  # draft 绝不进索引
        doc_id = lesson["id"]
        if doc_id in seen_ids:
            warnings.append("root {0} 存在重复 id {1}，已跳过重复项".format(
                root_name, doc_id))
            continue
        seen_ids.add(doc_id)
        docs.append(_lesson_doc(lesson))
    recipes, recipes_error = _load_root_recipes(root_path)
    if recipes_error:
        warnings.append("root {0} 的 recipes 解析失败已跳过: {1}".format(
            root_name, recipes_error))
    for recipe in recipes:
        doc_id = recipe["id"]
        if doc_id in seen_ids:
            warnings.append("root {0} 存在重复 id {1}，已跳过重复项".format(
                root_name, doc_id))
            continue
        seen_ids.add(doc_id)
        docs.append(_recipe_doc(recipe))

    postings = {}
    total_len = 0
    for doc in docs:
        toks = tokenize(doc["search_text"])
        doc["length"] = len(toks)
        total_len += len(toks)
        for tok, tf in Counter(toks).items():
            postings.setdefault(tok, {})[doc["id"]] = tf
    avgdl = (float(total_len) / len(docs)) if docs else 0.0
    index = {
        "docs": docs,
        "postings": postings,
        "avgdl": avgdl,
        "document_count": len(docs),
    }
    return index, warnings


# ---------------------------------------------------------------------------
# per-root JSON 缓存（mtime_ns + size 校验，corrupt → rebuild）
# ---------------------------------------------------------------------------
def _root_source_sig(root_path):
    """root 源文件签名：lessons/*.md + recipes/BEST_PRACTICES.md 的
    (relpath, mtime_ns, size) 列表的 sha256。任一文件变化 → 签名变化。"""
    entries = []
    lessons_dir = _lessons.lessons_dir(root_path)
    if os.path.isdir(lessons_dir):
        for name in sorted(os.listdir(lessons_dir)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(lessons_dir, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entries.append(("lessons/{0}".format(name),
                            stat.st_mtime_ns, stat.st_size))
    recipes_path = os.path.join(root_path, RECIPES_RELPATH)
    if os.path.isfile(recipes_path):
        try:
            stat = os.stat(recipes_path)
        except OSError:
            pass
        else:
            entries.append(("recipes/BEST_PRACTICES.md",
                            stat.st_mtime_ns, stat.st_size))
    sig_text = json.dumps(entries, sort_keys=True)
    return hashlib.sha256(sig_text.encode("utf-8")).hexdigest()


def _cache_file(root_name):
    """单 root 的 JSON 缓存文件路径（base/cache/index/<root-name>/index.v1.json）。"""
    return os.path.join(_lessons.cache_index_dir(root_name), INDEX_FILENAME)


def _validate_cached(raw_text, sig):
    """校验缓存 JSON：schema / version / source_sig / 全量结构。失败返回 None。

    除顶层 shape 外，还镜像 ``_rag._validate_index`` 做语义校验：
    - 每篇 doc 的必需字段：id/kind/title/category/severity/symptom/fix/
      verified_versions/search_text 为 str，length 为非负 int，strength 为
      int（recipe 可为 None），doc id 在根内唯一；
    - postings：term 为非空 str；每个 entry 是 {doc_id: tf}，doc_id 必须
      存在于 docs，tf 为非负 int；
    - avgdl 为有限非负数；document_count 为非负 int 且等于 len(docs)。
    任一违反 → 返回 None（调用方据此静默重建，绝不把损坏缓存当可用数据）。
    """
    try:
        data = json.loads(raw_text)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != SCHEMA_NAME or data.get("version") != SCHEMA_VERSION:
        return None
    if data.get("source_sig") != sig:
        return None
    docs = data.get("docs")
    postings = data.get("postings")
    doc_count = data.get("document_count")
    if not isinstance(docs, list) or not isinstance(postings, dict):
        return None
    if isinstance(doc_count, bool) or not isinstance(doc_count, int) \
            or doc_count < 0:
        return None
    if len(docs) != doc_count:
        return None
    avgdl = data.get("avgdl", 0.0)
    if isinstance(avgdl, bool) or not isinstance(avgdl, (int, float)) \
            or avgdl < 0 or math.isnan(avgdl) or math.isinf(avgdl):
        return None
    doc_ids = set()
    for doc in docs:
        if not isinstance(doc, dict):
            return None
        for key in ("id", "kind", "title", "category", "severity", "symptom",
                    "fix", "verified_versions", "search_text"):
            if not isinstance(doc.get(key), str):
                return None
        length = doc.get("length")
        if isinstance(length, bool) or not isinstance(length, int) \
                or length < 0:
            return None
        strength = doc.get("strength")
        if strength is not None and (isinstance(strength, bool)
                                     or not isinstance(strength, int)):
            return None
        doc_id = doc["id"]
        if doc_id in doc_ids:
            return None
        doc_ids.add(doc_id)
    for term, plist in postings.items():
        if not isinstance(term, str) or not term:
            return None
        if not isinstance(plist, dict):
            return None
        for doc_id, tf in plist.items():
            if doc_id not in doc_ids:
                return None
            if isinstance(tf, bool) or not isinstance(tf, int) or tf < 0:
                return None
    return {"docs": docs, "postings": postings,
            "avgdl": avgdl, "document_count": doc_count}


def _write_cache(cache_path, sig, root_name, index):
    """原子写缓存 JSON（temp + os.replace）；失败仅 print，不打断检索。"""
    payload = {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "root_name": root_name,
        "source_sig": sig,
        "built_at": _now_iso(),
        "docs": index["docs"],
        "postings": index["postings"],
        "avgdl": index["avgdl"],
        "document_count": index["document_count"],
    }
    text = json.dumps(payload, ensure_ascii=False)
    tmp_path = None
    try:
        parent = os.path.dirname(cache_path)
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".lsidx-", suffix=".tmp",
                                        dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, cache_path)
    except OSError as exc:
        print("_lessons_search: cache 写入失败已跳过: {0}".format(exc))
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _load_or_build_root_index(desc, sig):
    """加载或重建单 root 索引。返回 ``(index_dict, warnings_list)``。

    优先级：进程内缓存（key=cache 路径+sig）→ 磁盘缓存（校验通过）→ 重建。
    坏缓存绝不替换进程内已校验数据；重建结果原子落盘。warnings 仅在重建
    路径产生（缓存命中时为空列表）。
    """
    cache_path = _cache_file(desc["name"])
    key = (cache_path, sig)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached, []

    raw = None
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            raw = None
    data = _validate_cached(raw, sig) if raw is not None else None

    if data is None:
        data, warnings = _build_root_index(desc["path"], desc["name"])
        _write_cache(cache_path, sig, desc["name"], data)
        _CACHE[key] = data
        return data, warnings
    _CACHE[key] = data
    return data, []


# ---------------------------------------------------------------------------
# BM25 评分
# ---------------------------------------------------------------------------
def _bm25_scores(postings, docs_by_id, avgdl, doc_count, q_tokens):
    """Okapi BM25（k1=1.5、b=0.75、+1 平滑 IDF）：返回 {doc_id: score}。"""
    scores = {}
    seen_terms = set()
    for term in q_tokens:
        if term in seen_terms:
            continue
        seen_terms.add(term)
        plist = postings.get(term)
        if not plist:
            continue
        df = len(plist)
        idf = math.log((doc_count - df + 0.5) / (df + 0.5) + 1)
        for doc_id, tf in plist.items():
            doc = docs_by_id.get(doc_id)
            if doc is None:
                continue
            dl = doc["length"]
            if avgdl > 0:
                denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * (dl / avgdl))
            else:
                denom = tf + BM25_K1
            if denom <= 0:
                continue
            contrib = idf * (tf * (BM25_K1 + 1)) / denom
            scores[doc_id] = scores.get(doc_id, 0.0) + contrib
    return scores


def _passes_filters(doc, category, severity, node_type, houdini_version):
    """过滤：category / severity 精确，node_type / houdini_version 子串，AND。"""
    if category is not None and doc.get("category") != category:
        return False
    if severity is not None and doc.get("severity") != severity:
        return False
    if node_type is not None:
        needle = str(node_type).casefold()
        if needle not in (doc.get("search_text") or "").casefold():
            return False
    if houdini_version is not None:
        needle = str(houdini_version).casefold()
        if needle not in (doc.get("affected_versions") or "").casefold():
            return False
    return True


def _score_root(index, q_tokens, query_str, desc, category, severity,
                node_type, houdini_version):
    """单 root 融合评分：BM25 × 指纹 × strength × 新鲜度 × priority。

    ``has_query`` 以 query_str 是否非空为准（而非 q_tokens）：非空查询即使
    全部 token 被停用词过滤（q_tokens 为空）也**不**走空查询基线 —— 无
    任何 BM25 命中 → 该 root 匹配 0 条；只有真正的空查询才回退到 1.0 基线。
    """
    docs_by_id = {doc["id"]: doc for doc in index["docs"]}
    postings = index["postings"]
    avgdl = index["avgdl"]
    doc_count = index["document_count"]
    has_query = bool(query_str)
    if has_query:
        bm25 = _bm25_scores(postings, docs_by_id, avgdl, doc_count, q_tokens)
    else:
        bm25 = {}
    now = datetime.now().astimezone()
    out = []
    for doc_id in sorted(docs_by_id.keys()):
        doc = docs_by_id[doc_id]
        if not _passes_filters(doc, category, severity, node_type,
                               houdini_version):
            continue
        score = bm25.get(doc_id, 0.0) if has_query else 1.0
        if has_query and score <= 0:
            continue
        if doc["kind"] == "lesson":
            if query_str and doc.get("fingerprint") and \
                    _lessons.make_fingerprint(query_str) == doc["fingerprint"]:
                score *= FINGERPRINT_BOOST
            score *= 1.0 + STRENGTH_FACTOR * float(doc.get("strength") or 0)
            score *= freshness_decay(doc.get("updated_at"), now)
        score *= float(desc["priority"])
        out.append((score, doc))
    return out


# ---------------------------------------------------------------------------
# 摘要 / hint / draft suggestions
# ---------------------------------------------------------------------------
def _truncate(text, max_chars):
    """截断到 max_chars 字符（不追加省略号，保证 len <= max）。"""
    if not isinstance(text, str):
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _summary(doc, source_root, hint=None):
    """紧凑摘要：绝不含全文（problem 不进摘要，symptom/fix 截断 ~200ch）。"""
    summary = {
        "id": doc["id"],
        "kind": doc["kind"],
        "title": doc["title"],
        "category": doc["category"],
        "severity": doc["severity"],
        "symptom": _truncate(doc["symptom"], SUMMARY_MAX),
        "fix": _truncate(doc["fix"], SUMMARY_MAX),
        "verified_versions": doc["verified_versions"],
        "source_root": source_root,
    }
    if doc["kind"] == "lesson":
        summary["strength"] = doc["strength"]
    if hint is not None:
        summary["hint"] = hint
    return summary


def _inbox_fingerprint_counts(root_path):
    """root inbox 中 fingerprint → 累计 count（复用 _lessons helpers）。"""
    records, _bad = _lessons._read_inbox(root_path)
    counts = {}
    for _raw, record in records:
        count = _lessons._record_count(record)
        if count is None:
            continue  # 坏 count 记录不参与聚合
        fp = record.get("fingerprint")
        if not fp:
            continue
        counts[fp] = counts.get(fp, 0) + count
    return counts


def _draft_suggestions(root_path, root_name):
    """count>=3 的 draft 骨架列表 [{id, root, count, symptom}]。

    count 降序（并列按 id 升序），最多 ``DRAFT_SUGGESTIONS_MAX`` 条；
    symptom 与摘要一致截断到 ``SUMMARY_MAX`` 字符，避免全量正文泄漏。
    """
    lessons, _errors = _lessons.load_root_lessons(root_path)
    counts = _inbox_fingerprint_counts(root_path)
    suggestions = []
    for lesson in lessons:
        if lesson["status"] != "draft":
            continue
        fp = lesson.get("fingerprint")
        count = counts.get(fp, 0)
        if count >= HINT_THRESHOLD:
            suggestions.append({
                "id": lesson["id"],
                "root": root_name,
                "count": count,
                "symptom": _truncate(lesson.get("symptom", ""), SUMMARY_MAX),
            })
    suggestions.sort(key=lambda item: (-item["count"], item["id"]))
    return suggestions[:DRAFT_SUGGESTIONS_MAX]


# ---------------------------------------------------------------------------
# root 解析
# ---------------------------------------------------------------------------
def _resolve_roots(scope):
    """scope → root 描述符列表（None/""/"all" → 全部；名字 → 单 root 白名单）。

    名字比较前先 ``strip()``（``" teamx "`` 等价 ``"teamx"``，纯空白等价
    空字符串）；未知 / 不可用名字经 ``_lessons.normalize_root_name`` 抛
    ``LessonsError('ls_unknown_root')``。
    """
    if isinstance(scope, str):
        scope = scope.strip()
    if scope is None or scope in ("", "all"):
        return _lessons.resolve_roots()
    return [_lessons.normalize_root_name(scope)]


def _stats_roots(scope):
    """stats 用 root 列表：scope=None/""/"all" → 全部（含降级 root）；
    名字 → 直接按 resolve_roots 查找（unavailable 也可报告状态）。
    名字比较前先 ``strip()``，与 ``_resolve_roots`` 一致。"""
    if isinstance(scope, str):
        scope = scope.strip()
    if scope is None or scope in ("", "all"):
        return _lessons.resolve_roots()
    for desc in _lessons.resolve_roots():
        if desc["name"] == scope:
            return [desc]
    raise _lessons.LessonsError(
        "ls_unknown_root", "未知 root 名: {0}".format(scope),
        {"name": scope})


# ---------------------------------------------------------------------------
# bridge 入口：search_lessons
# ---------------------------------------------------------------------------
def _clamp_top_k(top_k):
    try:
        value = int(top_k)
    except (TypeError, ValueError):
        value = TOP_K_DEFAULT
    if value < TOP_K_MIN:
        value = TOP_K_MIN
    if value > TOP_K_MAX:
        value = TOP_K_MAX
    return value


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


def _error_envelope(exc, query, top_k):
    """统一 error envelope（与 success 同形，便于消费方一致处理）。"""
    return {
        "status": "error",
        "error": {"code": exc.code, "message": exc.message,
                  "details": exc.details},
        "query": query,
        "top_k": top_k,
        "matched": 0,
        "returned_count": 0,
        "truncated": False,
        "results": [],
        "draft_suggestions": [],
    }


def search_lessons(query=None, category=None, severity=None, node_type=None,
                   houdini_version=None, scope=None, top_k=TOP_K_DEFAULT,
                   response_cap_fn=None, max_bytes=DEFAULT_MAX_BYTES):
    """跨全部可用 root 检索 published lessons 与各 root recipes，融合排序。

    本函数**不建立 Houdini 连接**（tasks 3.1）；draft 不进索引。返回 envelope：
    - success: ``status/query/top_k/matched/returned_count/truncated/results/
      draft_suggestions``；unavailable root 或 recipes 解析失败附 ``_warning``。
    - error: ``status="error"`` + ``error={code,message,details}``（scope 未知
      root → ``ls_unknown_root``）。
    ``top_k`` clamp 到 [1,5]；``returned_count`` 恒等于 ``len(results)``；
    整体过 ``apply_response_cap``（defense-in-depth）后重新对齐计数。
    """
    top_k_int = _clamp_top_k(top_k)
    query_str = query if isinstance(query, str) else ""
    try:
        roots = _resolve_roots(scope)
    except _lessons.LessonsError as exc:
        return _error_envelope(exc, query_str, top_k_int)

    q_tokens = tokenize(query_str) if query_str else []
    warnings = []
    scored = []
    draft_suggestions = []
    for desc in roots:
        name = desc["name"]
        if desc["state"] != "ok":
            if desc["state"] == "unavailable":
                warnings.append(
                    "root {0} 不可达（state=unavailable），检索已跳过".format(name))
            continue
        root_path = desc["path"]
        index, root_warnings = _load_or_build_root_index(
            desc, _root_source_sig(root_path))
        warnings.extend(root_warnings)
        fp_counts = _inbox_fingerprint_counts(root_path)
        draft_suggestions.extend(_draft_suggestions(root_path, name))
        for score, doc in _score_root(index, q_tokens, query_str, desc,
                                      category, severity, node_type,
                                      houdini_version):
            scored.append((score, doc, desc["priority"], name, fp_counts))

    # 跨 root 合并排序：score 降序 → priority 降序 → id 升序
    scored.sort(key=lambda item: (-item[0], -item[2], item[1]["id"]))
    matched = len(scored)

    results = []
    for score, doc, priority, source_root, fp_counts in scored[:top_k_int]:
        hint = None
        if doc["kind"] == "lesson" and doc.get("fingerprint"):
            count = fp_counts.get(doc["fingerprint"], 0)
            if count >= HINT_THRESHOLD:
                hint = HINT_TEXT.format(count)
        results.append(_summary(doc, source_root, hint))

    result = {
        "status": "success",
        "query": query_str,
        "top_k": top_k_int,
        "matched": matched,
        "returned_count": len(results),
        "truncated": len(results) < matched,
        "results": results,
        "draft_suggestions": draft_suggestions,
    }
    if warnings:
        result["_warning"] = warnings

    # defense-in-depth：整个响应过 apply_response_cap，之后重新对齐计数
    result = _apply_cap(result, response_cap_fn, max_bytes)
    final_results = result.get("results")
    if not isinstance(final_results, list):
        final_results = []
        result["results"] = final_results
    result["returned_count"] = len(final_results)
    result["truncated"] = len(final_results) < matched
    return result


# ---------------------------------------------------------------------------
# bridge 入口：find_lesson_by_id / compute_stats
# ---------------------------------------------------------------------------
def find_lesson_by_id(lesson_id, scope=None):
    """按 id 查找完整 lesson dict（**含 draft**），跨全部 ok root。

    返回完整字段 + ``body_problem/body_symptom/body_fix`` 别名 + ``file_path``
    （绝对路径）+ ``root``（root 名）；未找到返回 None。scope 未知 root 抛
    ``LessonsError('ls_unknown_root')``。
    """
    roots = _resolve_roots(scope)
    for desc in roots:
        if desc["state"] != "ok":
            continue
        lessons, _errors = _lessons.load_root_lessons(desc["path"])
        for lesson in lessons:
            if lesson["id"] == lesson_id:
                result = dict(lesson)
                result["body_problem"] = lesson.get("problem", "")
                result["body_symptom"] = lesson.get("symptom", "")
                result["body_fix"] = lesson.get("fix", "")
                result["file_path"] = os.path.join(
                    _lessons.lessons_dir(desc["path"]), lesson["file"])
                result["root"] = desc["name"]
                return result
    return None


def compute_stats(scope=None):
    """各 root 状态与计数摘要（含 unconfigured / unavailable，供状态报告）。

    每项 ``{name, state, path, priority, writable, lesson_count,
    draft_count, published_count, inbox_count, recipes_count}``；不可读 root
    计数为 0。scope 未知 root 抛 ``LessonsError('ls_unknown_root')``。
    """
    roots = _stats_roots(scope)
    out = []
    for desc in roots:
        entry = {
            "name": desc["name"],
            "state": desc["state"],
            "path": desc.get("path"),
            "priority": desc.get("priority"),
            "writable": desc.get("writable"),
            "lesson_count": 0,
            "draft_count": 0,
            "published_count": 0,
            "inbox_count": 0,
            "recipes_count": 0,
        }
        if desc["state"] == "ok" and desc.get("path"):
            lessons, _errors = _lessons.load_root_lessons(desc["path"])
            entry["lesson_count"] = len(lessons)
            entry["draft_count"] = sum(
                1 for lesson in lessons if lesson["status"] == "draft")
            entry["published_count"] = sum(
                1 for lesson in lessons if lesson["status"] == "published")
            entry["inbox_count"] = len(_lessons._read_inbox(desc["path"])[0])
            recipes, _recipes_error = _load_root_recipes(desc["path"])
            entry["recipes_count"] = len(recipes)
        out.append(entry)
    return {"roots": out}
