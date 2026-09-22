#!/usr/bin/env python
"""build_rag_index.py — 扫描 Houdini help 源（zip wiki 文本 + HTML）构建 RAG JSON 索引。

独立脚本（可由系统 Python 或 hython 直接运行），**不复用、不修改、不
替换** ``_help.py`` 的 ``SideFXDocParser`` 与 local-help-first 查询路径
（task 2.2 / R8）。本脚本自带仅用于批量索引的解析器：stdlib
``HTMLParser`` 子类（散装 HTML 目录）+ wiki 文本解析器（zip 帮助包）。

## 源实况（2026-09-10 实机侦察，feat-mcp-round2-hardening §4a）

H21 的 ``$HFS/houdini/help`` 下没有散装 HTML——文档以 **zip 打包的 wiki
文本**发布（46 个内容 zip：nodes.zip 4768 条 / vex.zip 1163 / hom.zip
939 / expressions.zip 475 / commands.zip 439 / solaris / pyro / render /
vellum / tops 等，条目为 ``sop/adaptiveprune.txt`` 风格：
``#type/#context`` 元数据 + ``= 标题 =`` + ``\"\"\"摘要\"\"\"`` + 正文 +
``@parameters`` 段）。**默认扫全部内容 zip 减排除清单**（初始排除
``images`` 纯图片包；versioned-rag-index task 1.1），``--zips`` 显式
传参可覆盖；散装 HTML 目录扫描模式**保留**（超集，可混扫）。

## 构建状态与锁（versioned-rag-index tasks 1.1 / 3.3）

- 输出目录内维护 ``build.status.json``（原子写）：字段 ``state``
  （building/done/failed）/ ``started_at`` / ``finished_at`` / ``pid`` /
  ``exit_code`` / ``error`` / ``zips``（本次 zip 全名集）/ ``doc_count``
  / ``source_path``；额外可选 ``zip_entry_failures``（异常 zip 统计，
  task 1.2）。bridge 侧 ``_rag_lifecycle.py`` 依赖该文件做防并发与
  完成检测。
- **锁清理契约**：bridge 触发自动构建前会在输出目录独占创建
  ``build.lock``；本脚本退出时（无论成败）best-effort 移除该锁文件。
- ``--version-dir <ver>``（task 1.3）：输出落到 ``<output>/<ver>/``
  子目录（版本化布局）；不传则保持平铺（手工用法兼容）。

wiki 文本解析规则（``parse_wiki_text``）：
- ``#key: value`` 元数据行：剥出正文；``#context`` / ``#internal`` /
  ``#tags`` 的值回灌为可检索 token（如 ``sop attribwrangle``——节点
  internal 名是精确检索键，标题 "Attribute Wrangle" 分词后不含它）
- ``= 标题 =``：提取 title；``== 段名 ==``：剥等号保留段名文本
- ``\"\"\"摘要\"\"\"``：保留为正文（去引号标记）
- ``@section`` 标记行：一律剥除；``@parameters`` 起的缩进参数块整段
  跳过（至下一个 ``@`` 标记或 EOF）——参数块占 nodes.zip 大头且非检索
  目标，跳过后索引体积可控
- ``:include ...:`` 指令行：剥除（内容不在本文件内）
- ``[text|target]`` 链接 markup → ``text``
- 其余正文保留；最终 ``_collapse_ws`` 折叠空白（与 HTML 模式一致）

doc path（索引内稳定 POSIX 标识）：zip 条目用 ``<zip 名>/<entry 相对
路径>``（如 ``nodes.zip/sop/attribwrangle.txt``）；HTML 用源目录相对
路径。全量按 path 排序后分配 doc id（确定性）。

## 设计要点（tasks 2.1-2.6 + round2 §4a）
- 仅 stdlib（R4）。可由系统 Python / hython 独立运行。
- 源优先级：``--source`` > ``HOUDINI_MCP_RAG_SOURCE`` >
  ``$HFS/houdini/help``。
- 输出优先级：``--output`` > ``HOUDINI_MCP_RAG_INDEX_DIR`` >
  ``~/.opera-houdini-mcp/rag/``（**不得**默认写入 git submodule 目录）；
  ``--version-dir`` 在其下再落 ``<ver>/`` 子目录。
- 构建 documents/postings/avgdl/document_count，全文内嵌 JSON；
  ``json.dump`` 流式写句柄（不一次性物化整串）；postings 逐 term
  ``popitem`` 转换释放中间 dict（峰值 ≈ 单份结构）。索引顶层附加
  ``zips``（实际入库 zip 全名集，供 eval 可比性指纹使用）。
- 异常 zip 统计（task 1.2）：单条目读取/解码失败只计数不中断（防单点
  坏条目拖垮全量构建）；整包打不开的 zip 单列。统计进 stderr 报告与
  状态文件 ``zip_entry_failures`` 字段。
- 原子发布（task 2.5）：同目录写唯一临时文件，flush + ``os.fsync()``
  后 ``os.replace()``；写入/替换失败保留旧索引并 best-effort 清临时
  文件。0 doc 时拒绝发布（保护既有索引）。
- 源目录缺失时 graceful 退出并给出配置提示（task 2.6）；此时未启动
  构建，不写状态文件。

运行示例：
    python external/houdinimcp/scripts/build_rag_index.py
    python build_rag_index.py --source "C:/Program Files/Side Effects \\
        Software/Houdini 21.0.596/houdini/help" --version-dir 21.0.596
    python build_rag_index.py --zips nodes,vex --output D:/rag
    hython external/houdinimcp/scripts/build_rag_index.py
"""
import argparse
import datetime
import io
import json
import os
import re
import sys
import tempfile
import zipfile
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

# 默认 zip 集策略（versioned-rag-index task 1.1）：help 目录全部 *.zip 减
# 排除清单。排除按 stem 小写比较；--zips 显式传参可整体覆盖。
EXCLUDED_ZIP_STEMS = frozenset(("images",))  # 纯图片包，无文本

# 构建状态文件 / 锁文件名（tasks 1.1 / 3.3；与 bridge _rag_lifecycle 协议）
STATUS_FILENAME = "build.status.json"
LOCK_FILENAME = "build.lock"

# 默认输出目录（round2 §4a：不再默认写 fork 模块目录）
DEFAULT_OUTPUT_DIRNAME = os.path.join(".opera-houdini-mcp", "rag")


def discover_zip_names(source_root):
    """默认 zip 集：``source_root`` 下全部 ``*.zip`` 减排除清单。

    返回按名排序的 **含 .zip 后缀** 的 basename 列表（与 ``--zips``
    显式传参统一形态，便于状态文件与 eval 指纹使用）；目录不可读返回
    ``[]``。
    """
    try:
        names = os.listdir(source_root)
    except OSError:
        return []
    found = []
    for name in names:
        if not name.lower().endswith(".zip"):
            continue
        if name[:-4].lower() in EXCLUDED_ZIP_STEMS:
            continue
        found.append(name)
    return sorted(found)


# ---------------------------------------------------------------------------
# 独立 HTML 正文解析器（task 2.2；散装 HTML 目录模式，保留为超集）
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
# wiki 文本解析器（round2 §4a：zip 帮助包条目格式）
# ---------------------------------------------------------------------------
# ``#key: value`` 元数据行（#type/#context/#internal/#icon/#tags/#since）
_WIKI_META_LINE_RE = re.compile(r"^#([A-Za-z_][\w-]*):\s*(.*)$")
# ``= 标题 =``：首字符 = 后必须跟空白（排除 ``== 段名 ==``）
_WIKI_TITLE_RE = re.compile(r"^=\s+(.*?)\s*=+\s*$")
# ``== 段名 ==``（及更深层级）：剥等号保留段名文本
_WIKI_SECTION_RE = re.compile(r"^==+\s*(.*?)\s*=+\s*$")
# ``@section`` 标记行（@parameters / @related / @examples / @inputs ...）
_WIKI_MARKER_RE = re.compile(r"^@([A-Za-z_]\w*)\s*$")
# ``:include xxx:`` 指令行（内容不在本文件内，纯噪声 token）
_WIKI_INCLUDE_RE = re.compile(r"^:include\b")
# ``[text|target]`` wiki 链接 → text
_WIKI_LINK_RE = re.compile(r"\[([^\]|]+)\|[^\]]*\]")
# 回灌为可检索 token 的元数据键（internal 名是精确检索键）
_WIKI_META_KEYWORD_KEYS = frozenset(("context", "internal", "tags"))
# 触发"跳过整段"的标记（参数块：体积大头且非检索目标）
_WIKI_SKIP_SECTION_MARKERS = frozenset(("parameters",))


def parse_wiki_text(text):
    """解析 Houdini help zip 的 wiki 文本条目，返回 ``(title, body)``。

    规则见模块 docstring；title 缺失时返回 ``""``（由调用方回退 entry
    stem）。任何异常输入都宽容处理（非字符串返回空），不抛。
    """
    if not isinstance(text, str):
        return "", ""
    title = ""
    meta_keywords = []
    body_parts = []
    in_skip_section = False
    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        meta = _WIKI_META_LINE_RE.match(stripped)
        if meta is not None:
            if meta.group(1).lower() in _WIKI_META_KEYWORD_KEYS:
                value = meta.group(2).strip()
                if value:
                    meta_keywords.append(value)
            continue
        marker = _WIKI_MARKER_RE.match(stripped)
        if marker is not None:
            # 标记行本身一律剥除；parameters 起的段整段跳过
            in_skip_section = (marker.group(1).lower()
                               in _WIKI_SKIP_SECTION_MARKERS)
            continue
        if in_skip_section:
            continue
        if stripped.startswith('"""'):
            body_parts.append(stripped.replace('"""', " "))
            continue
        if _WIKI_INCLUDE_RE.match(stripped):
            continue
        if not title:
            title_match = _WIKI_TITLE_RE.match(stripped)
            if title_match is not None:
                title = title_match.group(1).strip()
                continue
        section = _WIKI_SECTION_RE.match(stripped)
        if section is not None:
            body_parts.append(section.group(1).strip())
            continue
        body_parts.append(stripped)
    if meta_keywords:
        body_parts.append(" ".join(meta_keywords))
    body = _collapse_ws(_WIKI_LINK_RE.sub(r"\1", " ".join(body_parts)))
    return title, body


# ---------------------------------------------------------------------------
# 递归扫描 + zip 感知扫描 + 索引构建（tasks 2.3 / 2.4 + round2 §4a）
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


def find_zip_txt_entries(source_root, zip_names=None):
    """扫描 ``source_root`` 下指定 zip 帮助包的 ``.txt`` 条目。

    返回 ``[(doc_path, zip_abs_path, entry_name), ...]``，按 doc_path
    稳定排序。``doc_path`` 为 ``<zip 文件名>/<entry 相对路径>`` 的稳定
    POSIX 标识（如 ``nodes.zip/sop/attribwrangle.txt``）。``zip_names``
    为 ``None`` 时走默认发现（全部 zip 减排除清单，task 1.1）；zip
    缺失 / 损坏时跳过该 zip（不抛）。
    """
    if zip_names is None:
        zip_names = discover_zip_names(source_root)
    results = []
    source_root = os.path.abspath(source_root)
    for zname in zip_names:
        base = str(zname).strip()
        if not base:
            continue
        if not base.lower().endswith(".zip"):
            base = base + ".zip"
        zip_path = os.path.join(source_root, base)
        if not os.path.isfile(zip_path):
            continue
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = [n for n in zf.namelist()
                         if n.lower().endswith(".txt") and not n.endswith("/")]
        except (OSError, zipfile.BadZipFile, RuntimeError):
            continue
        prefix = os.path.basename(base)
        for entry in sorted(names):
            entry_norm = entry.replace("\\", "/")
            results.append(("%s/%s" % (prefix, entry_norm),
                            zip_path, entry_norm))
    results.sort(key=lambda triple: triple[0])
    return results


def _index_one_text(doc_id, doc_path, title, body, documents, postings):
    """把单个已解析文档写入 documents/postings，返回其 token 长度。"""
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
        "path": doc_path,
        "title": title,
        "length": length,
        "content": body,
    })
    return length


def build_index(source_root, zip_names=None, stats=None):
    """扫描源（zip wiki 文本 + 散装 HTML 超集）并构造符合
    ``houdinimcp.rag-index`` v1 schema 的 dict。

    doc id 按合并后 path 排序分配（确定性）；zip 逐包打开一次流式处理
    entry（不重复解压索引结构）；postings 逐 term popitem 转换释放中间
    dict，``json.dump`` 由 publish 阶段流式写盘。

    ``stats``（可选 dict，调用方原地收集，task 1.2 异常 zip 统计）：
    - ``entry_failures``：``{zip 名: 读取/解码失败条目数}``——单条目
      失败只计数不中断整体构建。
    - ``bad_zips``：整包打不开（缺失/损坏）的 zip 名列表。
    - ``indexed_zips``：实际产出 >=1 个文档的 zip 全名集（进索引顶层
      ``zips`` 字段，供 eval 可比性指纹）。
    """
    if stats is None:
        stats = {}
    stats.setdefault("entry_failures", {})
    stats.setdefault("bad_zips", [])
    stats.setdefault("indexed_zips", set())

    zip_entries = find_zip_txt_entries(source_root, zip_names)
    html_files = find_html_files(source_root)

    documents = []
    postings = {}  # term -> {doc_id: tf}
    total_length = 0
    doc_id = 0
    zip_doc_count = 0
    html_doc_count = 0

    # zip 条目（已按 doc_path 排序 → 同 zip 连续，仅在切换时重开 ZipFile）
    current_zip = None
    zf = None
    try:
        for doc_path, zip_abs, entry in zip_entries:
            if zip_abs != current_zip:
                if zf is not None:
                    zf.close()
                try:
                    zf = zipfile.ZipFile(zip_abs)
                except (OSError, zipfile.BadZipFile, RuntimeError):
                    zf = None
                    # find_zip_txt_entries 已能列出条目说明目录项完好，
                    # 此处整包打不开属异常 zip：记名、跳过、不中断
                    zip_base = os.path.basename(zip_abs)
                    if zip_base not in stats["bad_zips"]:
                        stats["bad_zips"].append(zip_base)
                current_zip = zip_abs
            if zf is None:
                continue
            try:
                text = zf.read(entry).decode("utf-8", "replace")
            except Exception:
                # 单条目失败：计数不中断（task 1.2）。宽捕获是有意的——
                # 损坏条目可能抛 BadZipFile（坏 CRC）/ zlib.error /
                # KeyError / OSError 等多种类型，逐一列举必漏。
                zip_base = os.path.basename(zip_abs)
                stats["entry_failures"][zip_base] = (
                    stats["entry_failures"].get(zip_base, 0) + 1)
                continue
            title, body = parse_wiki_text(text)
            if not title:
                # title 回退：entry stem（如 sop/attribwrangle.txt →
                # attribwrangle）——internal 名是检索键
                stem = entry.rsplit("/", 1)[-1]
                title = stem[:-4] if stem.lower().endswith(".txt") else stem
            total_length += _index_one_text(
                doc_id, doc_path, title, body, documents, postings)
            stats["indexed_zips"].add(doc_path.split("/", 1)[0])
            doc_id += 1
            zip_doc_count += 1
    finally:
        if zf is not None:
            zf.close()

    # 散装 HTML（超集保留）
    for posix_rel, abs_path in html_files:
        try:
            with io.open(abs_path, "r", encoding="utf-8",
                         errors="replace") as handle:
                html_text = handle.read()
        except OSError:
            continue
        title, body = parse_html(html_text)
        total_length += _index_one_text(
            doc_id, posix_rel, title, body, documents, postings)
        doc_id += 1
        html_doc_count += 1

    document_count = len(documents)
    avgdl = (total_length / document_count) if document_count > 0 else 0.0

    # postings 转换：popitem 逐 term 释放，避免双份中间结构
    postings_out = {}
    while postings:
        term, doc_map = postings.popitem()
        plist = [[did, tf] for did, tf in doc_map.items()]
        plist.sort(key=lambda pair: pair[0])
        postings_out[term] = plist

    built_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    index = {
        "schema": _rag.SCHEMA_NAME,
        "version": _rag.SCHEMA_VERSION,
        "built_at": built_at,
        "source": "build_rag_index.py from {0} (zips={1}, zip_docs={2}, "
                  "html_docs={3})".format(
                      os.path.abspath(source_root),
                      ",".join(sorted(stats["indexed_zips"])),
                      zip_doc_count, html_doc_count),
        # 实际入库 zip 全名集（versioned-rag-index：eval 可比性指纹用；
        # loader 对未知顶层字段宽容忽略）
        "zips": sorted(stats["indexed_zips"]),
        "document_count": document_count,
        "avgdl": avgdl,
        "documents": documents,
        "postings": postings_out,
    }
    return index


# ---------------------------------------------------------------------------
# 源/输出目录解析（tasks 2.3 / 2.6 + round2 §4a）
# ---------------------------------------------------------------------------
def resolve_source(explicit=None):
    """扫描源优先级：``--source`` > ``HOUDINI_MCP_RAG_SOURCE`` >
    ``$HFS/houdini/help``。

    返回存在的目录绝对路径，或 ``None``（缺失）。
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    env_src = os.environ.get("HOUDINI_MCP_RAG_SOURCE")
    if env_src:
        candidates.append(env_src)
    hfs = os.environ.get("HFS")
    if hfs:
        candidates.append(os.path.join(hfs, "houdini", "help"))
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return None


def resolve_index_dir(explicit=None):
    """输出目录：``--output`` > ``HOUDINI_MCP_RAG_INDEX_DIR`` >
    ``~/.opera-houdini-mcp/rag/``。

    round2 §4a：默认不再写 fork 模块目录（git submodule 内）。
    """
    if explicit:
        return explicit
    env_dir = os.environ.get("HOUDINI_MCP_RAG_INDEX_DIR")
    if env_dir:
        return env_dir
    return os.path.join(os.path.expanduser("~"), DEFAULT_OUTPUT_DIRNAME)


# ---------------------------------------------------------------------------
# 原子发布（task 2.5）
# ---------------------------------------------------------------------------
def publish_index(index, index_dir):
    """原子发布索引到 ``index_dir/index.v1.json``。

    在目标同目录写唯一临时文件，flush + ``os.fsync()`` 后
    ``os.replace(temp, final)`` 原子替换。写入或 replace 失败时保留旧
    索引，并 best-effort 删除临时文件。``json.dump`` 流式写句柄，不在
    内存一次性物化整串。
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
# 构建状态文件与锁清理（versioned-rag-index tasks 1.1 / 3.3）
# ---------------------------------------------------------------------------
def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_build_status(status_dir, payload):
    """原子写 ``<status_dir>/build.status.json``（temp + ``os.replace``）。

    状态文件是 bridge 防并发/完成检测的依据：内容必须始终完整可解析，
    绝不出现半写状态（原子替换保障）。
    """
    if not os.path.isdir(status_dir):
        os.makedirs(status_dir, exist_ok=True)
    final_path = os.path.join(status_dir, STATUS_FILENAME)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".build.status.", suffix=".tmp", dir=status_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, final_path)
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    return final_path


def _write_status_best_effort(status_dir, payload):
    """状态写失败不杀构建本身：警告后继续（索引可用性优先于可观测性）。"""
    try:
        write_build_status(status_dir, payload)
    except OSError as exc:
        sys.stderr.write(
            "build_rag_index: write status failed ({0}): {1}\n".format(
                status_dir, exc))


def _remove_build_lock(index_dir):
    """构建退出契约：best-effort 移除 bridge 预建的独占锁文件。

    无论成败，本脚本退出时都应释放锁，让 bridge 的二次探测能推进。
    """
    lock_path = os.path.join(index_dir, LOCK_FILENAME)
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass


def _sanitize_version_dir(value):
    """校验 ``--version-dir``：非空、单段、无分隔符（防路径逃逸）。"""
    if value is None:
        return None
    ver = str(value).strip()
    if not ver:
        return None
    if "/" in ver or "\\" in ver or ver in (".", ".."):
        raise ValueError(
            "--version-dir must be a single path segment, got: {0!r}".format(
                value))
    return ver


# ---------------------------------------------------------------------------
# CLI（round2 §4a：--source / --output / --zips；
#      versioned-rag-index：默认全量 zip + --version-dir + 状态文件）
# ---------------------------------------------------------------------------
def _parse_zips(value):
    """``--zips`` 解析：``None``/空 → 返回 ``None``（= 默认发现全部 zip）。"""
    if value is None:
        return None
    names = [part.strip() for part in str(value).split(",") if part.strip()]
    return names or None


def _normalize_zip_names(names):
    """统一为含 ``.zip`` 后缀的 basename（状态文件/指纹形态）。"""
    out = []
    for name in names:
        base = str(name).strip()
        if not base:
            continue
        if not base.lower().endswith(".zip"):
            base = base + ".zip"
        out.append(base)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the houdinimcp RAG index (zip wiki text + HTML).")
    parser.add_argument(
        "--source", default=None,
        help="help source dir (default: HOUDINI_MCP_RAG_SOURCE > "
             "$HFS/houdini/help)")
    parser.add_argument(
        "--output", default=None,
        help="output dir (default: HOUDINI_MCP_RAG_INDEX_DIR > "
             "~/.opera-houdini-mcp/rag/)")
    parser.add_argument(
        "--zips", default=None,
        help="comma-separated zip basenames under the source dir "
             "(default: ALL *.zip under source minus exclusion list "
             "{images})")
    parser.add_argument(
        "--version-dir", default=None,
        help="optional version subdirectory: output lands in "
             "<output>/<version-dir>/ (e.g. 21.0.596); flat layout when "
             "omitted (manual usage compatible)")
    args = parser.parse_args(argv)

    # 版本段校验（单段路径，防拼接逃逸）
    try:
        version_dir = _sanitize_version_dir(args.version_dir)
    except ValueError as exc:
        sys.stderr.write("build_rag_index: {0}\n".format(exc))
        return 2

    source = resolve_source(args.source)
    if source is None:
        # 未启动构建：不写状态文件（bridge 侧不应看到 building）
        sys.stderr.write(
            "build_rag_index: no help source found.\n"
            "Pass --source <dir>, or set HOUDINI_MCP_RAG_SOURCE, or run "
            "inside hython (HFS set).\n")
        return 2

    zip_names = _parse_zips(args.zips)
    if zip_names is None:
        zip_names = discover_zip_names(source)
        if not zip_names:
            sys.stderr.write(
                "build_rag_index: no *.zip found under {0} (exclusion "
                "list: {1}); nothing to index.\n".format(
                    source, ",".join(sorted(EXCLUDED_ZIP_STEMS))))
            return 2
    zips_field = _normalize_zip_names(zip_names)

    index_dir = resolve_index_dir(args.output)
    if version_dir:
        index_dir = os.path.join(index_dir, version_dir)

    # 状态文件：building 起手（字段清单三层统一：state/started_at/
    # finished_at/pid/exit_code/error/zips/doc_count/source_path）
    status = {
        "state": "building",
        "started_at": _utc_now_iso(),
        "finished_at": None,
        "pid": os.getpid(),
        "exit_code": None,
        "error": None,
        "zips": zips_field,
        "doc_count": None,
        "source_path": os.path.abspath(source),
    }
    _write_status_best_effort(index_dir, status)

    exit_code = 0
    stats = {}
    try:
        sys.stderr.write("build_rag_index: scanning {0} ({1} zips)\n".format(
            source, len(zips_field)))
        index = build_index(source, zip_names, stats=stats)
        if index["document_count"] <= 0:
            # 0 doc 多为源配置错误：拒绝发布，保护既有索引
            sys.stderr.write(
                "build_rag_index: 0 documents indexed (no matching zips or "
                "html under source); refusing to publish.\n")
            status.update(state="failed", exit_code=4,
                          error="0 documents indexed; refusing to publish",
                          finished_at=_utc_now_iso())
            exit_code = 4
        else:
            final_path = publish_index(index, index_dir)
            sys.stderr.write(
                "build_rag_index: wrote {0} ({1} docs, avgdl={2:.1f})\n"
                .format(final_path, index["document_count"],
                        index["avgdl"]))
            status.update(state="done", exit_code=0,
                          doc_count=index["document_count"],
                          finished_at=_utc_now_iso())
    except OSError as exc:
        sys.stderr.write("build_rag_index: publish failed: {0}\n".format(exc))
        status.update(state="failed", exit_code=3, error=str(exc),
                      finished_at=_utc_now_iso())
        exit_code = 3
    except Exception as exc:  # 意外异常也要落 failed（bridge 依赖状态推进）
        sys.stderr.write("build_rag_index: unexpected failure: {0}\n".format(
            exc))
        status.update(
            state="failed", exit_code=1,
            error="{0}: {1}".format(type(exc).__name__, exc),
            finished_at=_utc_now_iso())
        exit_code = 1
    finally:
        # 异常 zip 统计（task 1.2）：进状态文件 + stderr 报告
        if stats.get("entry_failures") or stats.get("bad_zips"):
            status["zip_entry_failures"] = {
                "entry_failures": stats.get("entry_failures", {}),
                "bad_zips": stats.get("bad_zips", []),
            }
            sys.stderr.write(
                "build_rag_index: anomalous zips: entry_failures={0} "
                "bad_zips={1}\n".format(
                    stats.get("entry_failures", {}),
                    stats.get("bad_zips", [])))
        _write_status_best_effort(index_dir, status)
        _remove_build_lock(index_dir)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
