"""自进化知识库的存储引擎与多 root registry（stdlib only，无 hou）。

本模块是 opera-houdini-mcp bridge 的「lessons 知识库」底座：负责 lesson
文件的严格解析、原子写入、symptom fingerprint 累加去重、inbox 事件收集与
draft 骨架自动生成，以及多 root（personal + 团队共享）的 registry 解析。
BM25 检索与 bridge 工具注册由后续 agent 基于本模块的公开 API 叠加。

设计要点：
- 仅依赖 Python 标准库（零新增 pip），不在顶层 import ``hou``。
- lesson 文件格式：YAML 风格 front matter（``key: value``，两端 ``---``）
  + ``# <title>`` + ``## Problem`` / ``## Symptom`` / ``## Fix`` 三段正文。
  front matter 含 9 个内容字段（category / severity / affected_versions /
  verified_versions / source / advisory / problem / symptom / fix 中前 6 个）
  与元数据（id / status / strength / root / created_at / updated_at /
  可选 fingerprint）。problem/symptom/fix 正文放正文段，不放 front matter。
- parser 严格校验：缺失必填字段、未知 front matter key、非法 severity /
  advisory / status / strength / 时间戳 / fingerprint、重复 id 或重复 key、
  published 空正文，任一失败都使**整个文件** parse 失败（code
  ``ls_parse_error``），绝不返回 partial lesson；``load_root_lessons``
  逐文件报错（``{rel_filename: error_message}``），坏文件不拖垮整个 root。
- 原子写：同目录 temp file + fsync + ``os.replace``；失败保留旧文件并返回
  结构化错误（code ``ls_write_error``），残留 temp 一律清理。
- fingerprint：``make_fingerprint`` 精确规范化（见其 docstring），
  sha256 hexdigest。同 fingerprint 已存在于 root → 只 ``strength++`` +
  更新 updated_at，**绝不覆盖已有内容**（ADD-only 累积引擎）。
- inbox（``inbox/events.jsonl``）：按 fingerprint 去重累加 count，一行一条；
  append-only（只增不删，坏行原样保留）。事件过大或写失败 → 跳过并记录，
  **永不抛异常**。count >= 3 自动生成 draft 骨架（category unclassified /
  severity medium / source inbox-auto，symptom 已知、problem/fix 为空）。
- registry（``config.json``）：只声明**额外**团队 root；personal root
  自动发现、永不进 registry、永远可写。path 只接受 ``${VAR}`` 占位符或
  相对路径（相对 base dir），裸绝对路径（盘符 / 前导 ``/`` / UNC）被拒绝。
  占位符未定义 → state ``unconfigured``（静默跳过，单机模式）；已定义但
  目录不可读 → state ``unavailable``（由 bridge 层据此加 ``_warning``）；
  两者均不影响 personal。非法 root 逐项报错并跳过。
- writability 门禁：registry 声明 ``writable=false`` 的 root 上
  ``save_lesson`` 返回结构化错误 ``root_not_writable``，零写入。
- recipes（``recipes/BEST_PRACTICES.md``）：``save_recipe`` 以 ``### BP-NNN``
  块写用法/流程知识——默认追加（id 自增、9 字段校验、advisory 固定 true、
  source 系统标注），传 ``recipe_id`` 则**原地替换**既有块 9 字段（不新增块，
  未知 id → ``ls_recipe_not_found`` 附既有 id 列表），写入即被检索（无 draft
  门槛）；团队 root 写入附 ``@<用户名>`` 归属。
  title 可选，仅用于校验、首块 ``> title`` 注释行渲染（title 仅供调用方
  自行使用/汇报）；追加写入因
  strict parser 限制（块间仅允许 ``- field`` 行与空行）不落盘；9 字段 schema
  无 title 字段。
- 路径推导：base dir = ``~/.opera-houdini-mcp``（expanduser("~")；Windows
  上 expanduser 返回 "~" 时兜底 USERPROFILE）；可用 ``HOUDINI_MCP_HOME``
  环境变量覆盖（测试钩子）。绝不使用 ``_repo_root()`` 或硬编码用户名。
- 测试钩子：``_base_dir()`` 可被 monkeypatch，所有路径 helper 都从它派生。
"""

import getpass
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime

try:
    from . import _common as cmn  # noqa: F401  (风格要求：响应 cap 备用)
except ImportError:
    try:
        import _common as cmn  # noqa: F401
    except ImportError:
        cmn = None

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
BASE_DIRNAME = ".opera-houdini-mcp"
KNOWLEDGE_DIRNAME = "knowledge"
LESSONS_DIRNAME = "lessons"
INBOX_DIRNAME = "inbox"
INBOX_FILENAME = "events.jsonl"
CONFIG_FILENAME = "config.json"
CACHE_DIRNAME = "cache"
INDEX_DIRNAME = "index"

PERSONAL_ROOT_NAME = "personal"
DEFAULT_PRIORITY = 0.5
PROMOTE_THRESHOLD = 3          # inbox 同一 fingerprint ≥3 次 → 自动生成 draft
EVENT_MAX_MESSAGE = 4096       # 单条事件消息上限（字符），超过跳过

SEVERITIES = ("low", "medium", "high", "critical")
STATUSES = ("draft", "published")
BODY_FIELDS = ("problem", "symptom", "fix")

# recipes（BEST_PRACTICES）常量：schema 与 _best_practices.py 同构（注意
# severity 枚举与 lesson 不同，recipes 只有 3 值）
RECIPES_DIRNAME = "recipes"
RECIPES_FILENAME = "BEST_PRACTICES.md"
RECIPE_FIELDS = (
    "category", "severity", "affected_versions", "verified_versions",
    "source", "advisory", "problem", "symptom", "fix",
)
RECIPE_SEVERITIES = ("low", "medium", "high")
RECIPE_FIELD_KEYS = frozenset(("title",) + RECIPE_FIELDS)  # title 可选
# 新文件 header（recipe 外只允许 blank / # / > 行，parser 安全）
RECIPES_HEADER = (
    "# BEST PRACTICES\n"
    "\n"
    "> 本文件由 save_recipe 自动追加维护：每块以 ### BP-NNN 为 heading"
    "（id 由系统自增，\n"
    "> 不接受用户自定义），块内为 9 个必填字段（category / severity /"
    " affected_versions /\n"
    "> verified_versions / source / advisory / problem / symptom / fix），"
    "advisory 恒为 true，\n"
    "> source 由系统按 root 归属标注。人工编辑请保持该结构，否则本 root 的"
    "recipes 将无法解析。\n"
)

# 正文行禁止以这些前缀起始（否则会被误读为 field / heading / blockquote）
_RECIPE_BAD_LINE_PREFIXES = ("- ", "#", ">", "###")
_RECIPE_HEADING_RE = re.compile(r"^###\s+BP-(\d{3})\s*$", re.MULTILINE)
# save_recipe 的 recipe_id（引用既有块，非自定义新 id）格式
_RECIPE_ID_RE = re.compile(r"^BP-\d{3}$")

# front matter 必填（9 个内容字段中的 6 个 + 6 个元数据）
REQUIRED_FRONTMATTER = (
    "id", "status", "strength", "root", "created_at", "updated_at",
    "category", "severity", "affected_versions", "verified_versions",
    "source", "advisory",
)
OPTIONAL_FRONTMATTER = ("fingerprint",)
FRONTMATTER_KEYS = frozenset(REQUIRED_FRONTMATTER + OPTIONAL_FRONTMATTER)

# 渲染顺序（与规格示例一致）
FRONTMATTER_ORDER = REQUIRED_FRONTMATTER + OPTIONAL_FRONTMATTER

# save_lesson 可接受的 fields 键（title + 9 内容字段 + 可选 fingerprint）
SAVE_FIELD_KEYS = frozenset(
    ("title", "category", "severity", "affected_versions",
     "verified_versions", "source", "advisory",
     "problem", "symptom", "fix", "fingerprint"))

_FRONTMATTER_LINE_RE = re.compile(r"^([a-z_]+):\s*(.*)$")
_HEADING1_RE = re.compile(r"^#\s+(.*)$")
_HEADING2_RE = re.compile(r"^##\s+([A-Za-z]+)\s*$")
_ID_RE = re.compile(r"^L-\d{8}-\d{3,}$")
_ISO_TZ_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_ROOT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# 统一 error 类型
# ---------------------------------------------------------------------------
class LessonsError(Exception):
    """把 parse / write / root 解析错误归一为稳定 code/message/details。

    code 稳定区分：ls_parse_error / ls_write_error / ls_unknown_root /
    root_not_writable / ls_recipe_not_found（save_recipe 按 id 原地更新时
    引用不存在的块）。bridge 层据此构造完全相同 shape 的 error envelope。
    """

    def __init__(self, code, message, details=None):
        super(LessonsError, self).__init__(message)
        self.code = code
        self.message = message
        self.details = details if isinstance(details, dict) else {}


# ---------------------------------------------------------------------------
# 1.1 路径
# ---------------------------------------------------------------------------
def _base_dir():
    """knowledge base 目录（测试钩子）。

    优先级：``HOUDINI_MCP_HOME`` 环境变量（测试覆盖用）→
    ``os.path.expanduser("~")`` + ``.opera-houdini-mcp``；expanduser 返回
    "~"（如缺 HOME）时 Windows 兜底 ``USERPROFILE``；两者都不可得时
    兜底当前工作目录（仅防崩溃，正常环境不会走到）。
    """
    override = os.environ.get("HOUDINI_MCP_HOME")
    if override:
        return os.path.abspath(override)
    home = os.path.expanduser("~")
    if not home or home == "~":
        home = os.environ.get("USERPROFILE", "")
        if not home:
            home = os.getcwd()
    return os.path.join(home, BASE_DIRNAME)


def knowledge_dir():
    """personal knowledge root（base/knowledge/）。"""
    return os.path.join(_base_dir(), KNOWLEDGE_DIRNAME)


def _normalize_root_path(root_path):
    """校验并规范化 root_path（knowledge 根目录）。"""
    if not isinstance(root_path, str) or not root_path.strip():
        raise LessonsError(
            "ls_write_error", "root_path 必须是非空字符串",
            {"root_path": root_path, "type": type(root_path).__name__})
    return os.path.normpath(os.path.abspath(root_path.strip()))


def lessons_dir(root_path):
    """root_path 下的 lessons 目录（lessons/*.md）。"""
    return os.path.join(_normalize_root_path(root_path), LESSONS_DIRNAME)


def inbox_path(root_path):
    """root_path 下的 inbox 事件文件（inbox/events.jsonl）。"""
    return os.path.join(_normalize_root_path(root_path),
                        INBOX_DIRNAME, INBOX_FILENAME)


def recipes_path(root_path):
    """root_path 下的 recipes 文件（recipes/BEST_PRACTICES.md）。"""
    return os.path.join(_normalize_root_path(root_path),
                        RECIPES_DIRNAME, RECIPES_FILENAME)


def cache_index_dir(root_name):
    """BM25 检索 cache 目录（base/cache/index/<root-name>/，section 3 用）。

    root_name 只接受 ``[A-Za-z0-9_-]``，防目录穿越；非法抛
    ``ls_unknown_root``。
    """
    if not isinstance(root_name, str) or not _ROOT_NAME_RE.match(root_name):
        raise LessonsError(
            "ls_unknown_root", "非法 root 名: {0!r}".format(root_name),
            {"root_name": root_name})
    return os.path.join(_base_dir(), CACHE_DIRNAME, INDEX_DIRNAME, root_name)


# ---------------------------------------------------------------------------
# 1.4 fingerprint
# ---------------------------------------------------------------------------
_PUNCT_SPACE_RE = re.compile(r"[\W_]+")


def make_fingerprint(text):
    """对症状文本做规范化后取 sha256 hexdigest（64 位小写 hex）。

    精确规范化步骤：
    1. ``str.casefold()``（大小写不敏感）；
    2. ``re.sub(r"[\\W_]+", " ", ...)``：把标点与空白（含 Unicode 空白）
       的连续串折叠为单个 ASCII 空格；Unicode 字母数字（如中文）保留，
       因此不同语言内容的 fingerprint 仍可区分；
    3. ``.strip()`` 去首尾空白；
    4. ``hashlib.sha256(norm.encode("utf-8")).hexdigest()``。

    不剥离末尾数字（保持错误码语义；规格中该项标注为可选，本实现选择保留）。
    非字符串输入抛 ``LessonsError('ls_write_error')``。
    """
    if not isinstance(text, str):
        raise LessonsError(
            "ls_write_error", "make_fingerprint 需要 str 输入",
            {"type": type(text).__name__})
    norm = _PUNCT_SPACE_RE.sub(" ", text.casefold()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1.2 parser
# ---------------------------------------------------------------------------
def _unquote(value):
    """剥离首尾成对匹配的引号（front matter 值允许带引号）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _parse_bool(raw):
    """advisory 布尔字面量：仅接受 true/false（大小写不敏感）。"""
    norm = raw.strip().lower()
    if norm == "true":
        return True
    if norm == "false":
        return False
    raise ValueError("invalid bool literal: {0!r}".format(raw))


def parse_lesson(text):
    """严格解析单个 lesson 文件文本，返回 public lesson dict。

    任一 schema 失败抛 ``LessonsError(code='ls_parse_error')``，绝不返回
    partial lesson。返回字段：id / title / status / strength / root /
    created_at / updated_at / category / severity / affected_versions /
    verified_versions / source / advisory(bool) / problem / symptom / fix /
    fingerprint（缺省为 None）。
    """
    if not isinstance(text, str):
        raise LessonsError(
            "ls_parse_error", "text 必须是字符串",
            {"type": type(text).__name__})

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise LessonsError(
            "ls_parse_error", "缺少 front matter 起始分隔符 '---'",
            {"lineno": 1})

    # ---- front matter ----
    front = {}
    index = 1
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped == "---":
            break
        match = _FRONTMATTER_LINE_RE.match(stripped)
        if not match:
            raise LessonsError(
                "ls_parse_error",
                "front matter 行非法（line {0}）: {1!r}".format(index + 1,
                                                                stripped),
                {"lineno": index + 1, "line": stripped})
        key, raw = match.group(1), match.group(2).strip()
        if key not in FRONTMATTER_KEYS:
            raise LessonsError(
                "ls_parse_error",
                "未知 front matter key {0!r}（line {1}）".format(key,
                                                                index + 1),
                {"field": key, "lineno": index + 1})
        if key in front:
            raise LessonsError(
                "ls_parse_error",
                "重复 front matter key {0!r}（line {1}）".format(key,
                                                                index + 1),
                {"field": key, "lineno": index + 1})
        front[key] = _unquote(raw)
        index += 1
    else:
        raise LessonsError(
            "ls_parse_error", "front matter 未以 '---' 闭合", {})

    # ---- title ----
    index += 1
    while index < len(lines) and lines[index].strip() == "":
        index += 1
    if index >= len(lines):
        raise LessonsError(
            "ls_parse_error", "缺少 # title 行", {})
    title_match = _HEADING1_RE.match(lines[index].strip())
    if not title_match or not title_match.group(1).strip():
        raise LessonsError(
            "ls_parse_error", "缺少或为空 # title 行（line {0}）".format(index + 1),
            {"lineno": index + 1})
    title = title_match.group(1).strip()
    index += 1

    # ---- 正文段 ----
    bodies = {}
    current = None
    while index < len(lines):
        stripped = lines[index].strip()
        heading = _HEADING2_RE.match(stripped)
        if heading:
            name = heading.group(1).lower()
            if name not in BODY_FIELDS:
                raise LessonsError(
                    "ls_parse_error",
                    "未知正文段 ## {0}（line {1}）".format(heading.group(1),
                                                          index + 1),
                    {"section": heading.group(1), "lineno": index + 1})
            if name in bodies:
                raise LessonsError(
                    "ls_parse_error",
                    "重复正文段 ## {0}（line {1}）".format(heading.group(1),
                                                          index + 1),
                    {"section": heading.group(1), "lineno": index + 1})
            bodies[name] = []
            current = name
        else:
            if current is None and stripped != "":
                raise LessonsError(
                    "ls_parse_error",
                    "正文段外出现正文（line {0}）: {1!r}".format(index + 1,
                                                                stripped),
                    {"lineno": index + 1, "line": stripped})
            if current is not None:
                bodies[current].append(lines[index])
        index += 1

    missing_sections = [f for f in BODY_FIELDS if f not in bodies]
    if missing_sections:
        raise LessonsError(
            "ls_parse_error",
            "缺少正文段: {0}".format(sorted(missing_sections)),
            {"missing": sorted(missing_sections)})

    # ---- 字段校验 ----
    missing = [k for k in REQUIRED_FRONTMATTER if k not in front]
    if missing:
        raise LessonsError(
            "ls_parse_error",
            "缺失必填字段: {0}".format(sorted(missing)),
            {"missing": sorted(missing)})

    status = front["status"].strip().lower()
    if status not in STATUSES:
        raise LessonsError(
            "ls_parse_error",
            "非法 status {0!r}（合法: {1}）".format(front["status"],
                                                 sorted(STATUSES)),
            {"field": "status", "value": front["status"]})

    severity = front["severity"].strip().lower()
    if severity not in SEVERITIES:
        raise LessonsError(
            "ls_parse_error",
            "非法 severity {0!r}（合法: {1}）".format(front["severity"],
                                                  sorted(SEVERITIES)),
            {"field": "severity", "value": front["severity"]})

    try:
        advisory = _parse_bool(front["advisory"])
    except ValueError:
        raise LessonsError(
            "ls_parse_error",
            "非法 advisory bool {0!r}（只接受 true/false）".format(
                front["advisory"]),
            {"field": "advisory", "value": front["advisory"]})

    try:
        strength = int(front["strength"])
        if strength < 1:
            raise ValueError
    except (ValueError, TypeError):
        raise LessonsError(
            "ls_parse_error",
            "非法 strength {0!r}（必须是 >= 1 的整数）".format(
                front["strength"]),
            {"field": "strength", "value": front["strength"]})

    lesson_id = front["id"].strip()
    if not _ID_RE.match(lesson_id):
        raise LessonsError(
            "ls_parse_error",
            "非法 id {0!r}（格式 L-YYYYMMDD-NNN）".format(lesson_id),
            {"field": "id", "value": lesson_id})

    root = front["root"].strip()
    if not root:
        raise LessonsError(
            "ls_parse_error", "root 不得为空", {"field": "root"})

    for timestamp_field in ("created_at", "updated_at"):
        value = front[timestamp_field].strip()
        if not _ISO_TZ_RE.match(value):
            raise LessonsError(
                "ls_parse_error",
                "非法 {0} {1!r}（需 ISO 8601 且带时区偏移）".format(
                    timestamp_field, value),
                {"field": timestamp_field, "value": value})

    fingerprint = None
    if "fingerprint" in front:
        fp_value = front["fingerprint"].strip()
        if not _FINGERPRINT_RE.match(fp_value):
            raise LessonsError(
                "ls_parse_error",
                "非法 fingerprint（需 64 位小写 hex）: {0!r}".format(fp_value),
                {"field": "fingerprint", "value": fp_value})
        fingerprint = fp_value

    for must in ("category", "affected_versions", "verified_versions",
                 "source"):
        if not front[must].strip():
            raise LessonsError(
                "ls_parse_error", "{0} 不得为空".format(must),
                {"field": must})

    bodies_out = {}
    for body_field in BODY_FIELDS:
        bodies_out[body_field] = "\n".join(bodies[body_field]).strip()
    if status == "published":
        empty = [f for f in BODY_FIELDS if not bodies_out[f]]
        if empty:
            raise LessonsError(
                "ls_parse_error",
                "published lesson 的正文段不得为空: {0}".format(
                    sorted(empty)),
                {"empty": sorted(empty)})

    return {
        "id": lesson_id,
        "title": title,
        "status": status,
        "strength": strength,
        "root": root,
        "created_at": front["created_at"].strip(),
        "updated_at": front["updated_at"].strip(),
        "category": front["category"].strip(),
        "severity": severity,
        "affected_versions": front["affected_versions"].strip(),
        "verified_versions": front["verified_versions"].strip(),
        "source": front["source"].strip(),
        "advisory": advisory,
        "problem": bodies_out["problem"],
        "symptom": bodies_out["symptom"],
        "fix": bodies_out["fix"],
        "fingerprint": fingerprint,
    }


def load_root_lessons(root_path):
    """加载 root_path 下全部 ``lessons/*.md``。

    返回 ``(lessons, errors)``：errors 为 ``{rel_filename: error_message}``，
    仅记录 strict parse 失败的单个文件；合法文件照常加载（坏文件不拖垮
    root，但任一失败 lesson 绝不 partial）。成功 lesson 额外带 ``file``
    字段（相对 lessons/ 的文件名，供引擎定位更新）。
    """
    root_path = _normalize_root_path(root_path)
    dir_path = os.path.join(root_path, LESSONS_DIRNAME)
    if not os.path.isdir(dir_path):
        return [], {}
    lessons = []
    errors = {}
    for filename in sorted(os.listdir(dir_path)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(dir_path, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            lesson = parse_lesson(text)
            lesson["file"] = filename
            lessons.append(lesson)
        except LessonsError as exc:
            errors[filename] = exc.message
        except OSError as exc:
            errors[filename] = "无法读取: {0}".format(exc)
    return lessons, errors


# ---------------------------------------------------------------------------
# 1.3 原子写
# ---------------------------------------------------------------------------
def _atomic_write_text(path, text):
    """同目录 temp file + fsync + os.replace 原子写；失败保留旧文件。

    自动创建父目录；任何失败抛 ``LessonsError('ls_write_error')`` 并清理
    残留 temp 文件（旧文件完好）。
    """
    parent = os.path.dirname(path)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise LessonsError(
            "ls_write_error",
            "无法创建目录 {0}: {1}".format(parent, exc),
            {"path": path, "dir": parent, "exception": str(exc)})
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".lessons-", suffix=".tmp", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        return path
    except OSError as exc:
        raise LessonsError(
            "ls_write_error",
            "写入 {0} 失败: {1}".format(path, exc),
            {"path": path, "exception": str(exc)})
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _read_text(path):
    """读文本文件；OSError 归一为 ls_write_error。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise LessonsError(
            "ls_write_error", "无法读取 {0}: {1}".format(path, exc),
            {"path": path, "exception": str(exc)})


def _now_iso():
    """当前本地时间 ISO 8601（含 tz offset，如 2026-08-02T15:41:13+08:00）。"""
    return datetime.now().astimezone().isoformat()


def _bump_strength(text, new_strength, new_updated_at):
    """对既有 lesson 文本做定点替换：仅改 front matter 内的两行元数据。

    替换范围严格限定在 front matter 块（首个 ``---`` 与闭合 ``---`` 之间）；
    正文中任何以 ``strength:`` / ``updated_at:`` 开头的行都不受影响，其余
    字节（id / 标题 / 正文等）原样保留，满足「绝不覆盖已有内容」。
    """
    lines = text.splitlines()
    out = []
    found_strength = False
    found_updated = False
    in_front_matter = True   # line 0 恒为起始 ---
    for index, line in enumerate(lines):
        if index > 0 and line.strip() == "---":
            in_front_matter = False
        stripped = line.strip()
        if in_front_matter and stripped.startswith("strength:"):
            out.append("strength: {0}".format(new_strength))
            found_strength = True
        elif in_front_matter and stripped.startswith("updated_at:"):
            out.append('updated_at: "{0}"'.format(new_updated_at))
            found_updated = True
        else:
            out.append(line)
    if not (found_strength and found_updated):
        raise LessonsError(
            "ls_write_error", "无法定位 strength/updated_at 行，拒绝改写",
            {"path": text[:80]})
    return "\n".join(out) + "\n"


def _new_lesson_id(existing_ids, now):
    """生成 id ``L-YYYYMMDD-NNN``：扫描当天已有 id 取 max+1，碰撞安全循环。

    NNN 从 001 起，超过 999 自然扩展为更多位数（``%03d`` 最小宽度）。
    循环兜底覆盖跨日 / 手工命名的冲突。
    """
    day = now.strftime("%Y%m%d")
    prefix = "L-{0}-".format(day)
    nums = []
    for existing in existing_ids:
        if existing.startswith(prefix):
            match = re.match(r"^L-\d{8}-(\d+)$", existing)
            if match:
                nums.append(int(match.group(1)))
    next_num = (max(nums) + 1) if nums else 1
    taken = set(existing_ids)
    while True:
        candidate = "L-{0}-{1:03d}".format(day, next_num)
        if candidate not in taken:
            return candidate
        next_num += 1


def _render_lesson_markdown(lesson):
    """把 lesson dict 渲染为规范 markdown 文本（可被 parse_lesson round-trip）。"""
    lines = ["---"]
    for key in FRONTMATTER_ORDER:
        if key not in lesson or lesson[key] is None:
            continue
        value = lesson[key]
        if key in ("created_at", "updated_at"):
            lines.append('{0}: "{1}"'.format(key, value))
        elif key == "advisory":
            lines.append("advisory: true" if value else "advisory: false")
        elif key == "strength":
            lines.append("strength: {0}".format(int(value)))
        else:
            lines.append("{0}: {1}".format(key, value))
    lines.append("---")
    lines.append("")
    lines.append("# " + lesson["title"])
    lines.append("")
    for section in BODY_FIELDS:
        lines.append("## " + section.capitalize())
        body = lesson.get(section) or ""
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 1.3 / 1.4 save_lesson
# ---------------------------------------------------------------------------
def _validate_save_fields(fields, root_name):
    """校验 save_lesson 的 fields，返回带 root 名的 lesson 骨架。

    非法字段 / 非法 severity / 非法 advisory / 空 symptom 等均抛
    ``LessonsError('ls_write_error')``。
    """
    if not isinstance(fields, dict):
        raise LessonsError(
            "ls_write_error", "fields 必须是 dict",
            {"type": type(fields).__name__})
    unknown = set(fields) - SAVE_FIELD_KEYS
    if unknown:
        raise LessonsError(
            "ls_write_error",
            "fields 含未知键: {0}".format(sorted(unknown)),
            {"unknown": sorted(unknown)})

    lesson = {"root": root_name}
    for key in ("title", "category", "affected_versions", "source"):
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LessonsError(
                "ls_write_error", "{0} 不得为空".format(key),
                {"field": key})
        lesson[key] = value.strip()
    # verified_versions 缺省为 "unknown"（与 draft 骨架语义一致）
    verified = fields.get("verified_versions")
    if verified is None:
        lesson["verified_versions"] = "unknown"
    else:
        if not isinstance(verified, str) or not verified.strip():
            raise LessonsError(
                "ls_write_error", "verified_versions 不得为空",
                {"field": "verified_versions"})
        lesson["verified_versions"] = verified.strip()
    if "\n" in lesson["title"]:
        raise LessonsError(
            "ls_write_error", "title 必须单行", {"field": "title"})

    severity = fields.get("severity")
    if severity not in SEVERITIES:
        raise LessonsError(
            "ls_write_error",
            "非法 severity {0!r}（合法: {1}）".format(severity,
                                                  sorted(SEVERITIES)),
            {"field": "severity", "value": severity})
    lesson["severity"] = severity

    advisory = fields.get("advisory")
    if not isinstance(advisory, bool):
        raise LessonsError(
            "ls_write_error", "advisory 必须是布尔值",
            {"field": "advisory", "value": advisory})
    lesson["advisory"] = advisory

    symptom = fields.get("symptom")
    if not isinstance(symptom, str) or not symptom.strip():
        raise LessonsError(
            "ls_write_error", "symptom 不得为空（fingerprint 来源）",
            {"field": "symptom"})
    lesson["symptom"] = symptom.strip()

    for key in ("problem", "fix"):
        value = fields.get(key)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise LessonsError(
                "ls_write_error", "{0} 必须是字符串".format(key),
                {"field": key})
        lesson[key] = value.strip()

    # 正文不得包含会被误判为正文段标题的行（否则写出的文件无法 round-trip）
    for key in BODY_FIELDS:
        for line in lesson[key].splitlines():
            if line.strip().startswith("##"):
                raise LessonsError(
                    "ls_write_error",
                    "{0} 正文含 '##' 起始行，格式不安全".format(key),
                    {"field": key})

    fingerprint = fields.get("fingerprint")
    if fingerprint is not None:
        if not isinstance(fingerprint, str) or \
                not _FINGERPRINT_RE.match(fingerprint.strip()):
            raise LessonsError(
                "ls_write_error", "fingerprint 必须是 64 位小写 hex",
                {"field": "fingerprint"})
        lesson["fingerprint"] = fingerprint.strip()
    return lesson


def _root_name_for_path(root_path):
    """由 root_path 推导 root 名：registry 命中优先，personal 其次，否则 basename。"""
    target = os.path.abspath(os.path.normpath(root_path))
    for desc in resolve_roots():
        desc_path = desc.get("path")
        if desc_path and \
                os.path.abspath(os.path.normpath(desc_path)) == target:
            return desc["name"]
    return os.path.basename(root_path) or PERSONAL_ROOT_NAME


def _check_root_writable(root_path):
    """registry 可写门禁：声明 writable=false 或 state!=ok 的 root 拒绝写。

    不在 registry 的路径（personal 或未知目录）不受门禁约束。失败抛
    ``LessonsError('root_not_writable')``，零写入。
    """
    target = os.path.abspath(os.path.normpath(root_path))
    for desc in resolve_roots():
        desc_path = desc.get("path")
        if not desc_path:
            continue
        if os.path.abspath(os.path.normpath(desc_path)) != target:
            continue
        if desc["state"] != "ok":
            raise LessonsError(
                "root_not_writable",
                "root {0} 不可写（state={1}）".format(desc["name"],
                                                    desc["state"]),
                {"name": desc["name"], "path": target,
                 "state": desc["state"]})
        if not desc["writable"]:
            raise LessonsError(
                "root_not_writable",
                "root {0} 声明为只读（writable=false）".format(desc["name"]),
                {"name": desc["name"], "path": target})
        return


def save_lesson(root_path, fields):
    """把一条 lesson 保存 / 累积到 root_path（knowledge 根目录）。

    创建路径：生成新 id ``L-YYYYMMDD-NNN``，stamp status=draft / strength=1 /
    created_at / updated_at / root 名 / fingerprint（symptom 的 sha256）。
    累积路径：同 fingerprint 已存在 → 仅 strength++ + updated_at 刷新，
    **绝不覆盖已有内容**，也不新增文件。

    registry 声明 writable=false 的 root 抛 ``root_not_writable``（零写入）；
    其余失败（字段非法 / 原子写失败 / 自校验失败）抛
    ``LessonsError('ls_write_error')`` 且旧文件完好。成功返回完整 lesson dict。
    """
    root_path = _normalize_root_path(root_path)
    root_name = _root_name_for_path(root_path)
    _check_root_writable(root_path)

    lesson = _validate_save_fields(fields, root_name)
    agent = _agent_source(root_path)
    if agent is not None:
        lesson["source"] = lesson["source"] + "@" + agent
    fingerprint = lesson.get("fingerprint")
    if fingerprint is None:
        fingerprint = make_fingerprint(lesson["symptom"])
        lesson["fingerprint"] = fingerprint

    existing, _errors = load_root_lessons(root_path)

    # 同 fingerprint 已存在 → 只累加 strength，不新增、不覆盖
    for existing_lesson in existing:
        existing_fp = existing_lesson.get("fingerprint")
        if existing_fp and existing_fp == fingerprint:
            new_strength = int(existing_lesson["strength"]) + 1
            new_updated_at = _now_iso()
            file_path = os.path.join(lessons_dir(root_path),
                                     existing_lesson["file"])
            _atomic_write_text(
                file_path,
                _bump_strength(_read_text(file_path),
                               new_strength, new_updated_at))
            updated = dict(existing_lesson)
            updated["strength"] = new_strength
            updated["updated_at"] = new_updated_at
            return updated

    lesson["id"] = _new_lesson_id(
        [e["id"] for e in existing], datetime.now())
    lesson["status"] = "draft"
    lesson["strength"] = 1
    lesson["created_at"] = _now_iso()
    lesson["updated_at"] = lesson["created_at"]

    rendered = _render_lesson_markdown(lesson)
    try:
        parse_lesson(rendered)  # 自校验：绝不写无法 round-trip 的文件
    except LessonsError as exc:
        raise LessonsError(
            "ls_write_error",
            "生成的 lesson 无法自校验: {0}".format(exc.message), exc.details)

    file_path = os.path.join(lessons_dir(root_path),
                             lesson["id"] + ".md")
    _atomic_write_text(file_path, rendered)
    return lesson


# ---------------------------------------------------------------------------
# 1.6 recipes（save_recipe / _agent_source）
# ---------------------------------------------------------------------------
def _agent_source(root_path):
    """非 personal root 的动态归属用户名（团队库审计用）。

    root_path 等于个人库（``knowledge_dir()``）→ 返回 None，调用方保持
    source 原样；团队 root → 返回用户名（不含 '@' 前缀）：
    ``getpass.getuser()`` 动态获取，失败回退 ``USERNAME`` env，再失败
    ``"unknown-user"``。任何路径绝不抛异常。
    """
    target = os.path.abspath(os.path.normpath(root_path))
    if target == os.path.abspath(os.path.normpath(knowledge_dir())):
        return None
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME")
    if not user:
        user = "unknown-user"
    return user


def _next_recipe_id(text):
    """扫描既有 recipes 文本的全部 ``### BP-NNN`` 块，返回最大序号 + 1 的 id。

    撞号 while 重试防手工编辑造成的重复 heading；候选序号超 999 抛
    ``LessonsError('ls_write_error')``（与检索端 ``\\d{3}`` 正则保持兼容，
    不许突破 3 位）。MUST NOT 接受用户自定义 id。
    """
    taken = set()
    for match in _RECIPE_HEADING_RE.finditer(text):
        taken.add(int(match.group(1)))
    candidate = (max(taken) + 1) if taken else 1
    while candidate in taken:
        candidate += 1
    if candidate > 999:
        raise LessonsError(
            "ls_write_error",
            "recipes 序号已达上限 BP-999，无法自动生成新 id",
            {"max": 999})
    return "BP-{0:03d}".format(candidate)


def _validate_recipe_fields(fields, root_name):
    """校验 save_recipe 的 fields，返回带 root 名的 recipe 骨架。

    category / affected_versions / problem / symptom / fix 必填非空字符串；
    verified_versions 缺省 "unknown"；severity 必须是 ``RECIPE_SEVERITIES``
    （注意 recipes 与 lesson 的 severity 枚举不同，只有 3 值）；advisory 必须
    为 True（固定 advisory 语义）；source 不接受用户传入（由 ``_agent_source``
    系统标注）；正文（problem/symptom/fix）必须单行且不以 ``- `` / ``#`` /
    ``>`` / ``###`` 起始（否则写出的文件无法被 ``parse_best_practices``
    round-trip）。title 可选（非空 + 单行）。非法抛 ``LessonsError
    ('ls_write_error')``。
    """
    if not isinstance(fields, dict):
        raise LessonsError(
            "ls_write_error", "fields 必须是 dict",
            {"type": type(fields).__name__})
    unknown = set(fields) - RECIPE_FIELD_KEYS
    if unknown:
        raise LessonsError(
            "ls_write_error",
            "fields 含未知键: {0}".format(sorted(unknown)),
            {"unknown": sorted(unknown)})

    recipe = {"root": root_name}

    title = fields.get("title")
    if title is not None:
        if not isinstance(title, str) or not title.strip():
            raise LessonsError(
                "ls_write_error", "title 不得为空", {"field": "title"})
        if "\n" in title:
            raise LessonsError(
                "ls_write_error", "title 必须单行", {"field": "title"})
        recipe["title"] = title.strip()

    for key in ("category", "affected_versions", "problem", "symptom", "fix"):
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LessonsError(
                "ls_write_error", "{0} 不得为空".format(key),
                {"field": key})
        recipe[key] = value.strip()

    verified = fields.get("verified_versions")
    if verified is None:
        recipe["verified_versions"] = "unknown"
    else:
        if not isinstance(verified, str) or not verified.strip():
            raise LessonsError(
                "ls_write_error", "verified_versions 不得为空",
                {"field": "verified_versions"})
        recipe["verified_versions"] = verified.strip()

    severity = fields.get("severity")
    if severity not in RECIPE_SEVERITIES:
        raise LessonsError(
            "ls_write_error",
            "非法 severity {0!r}（合法: {1}）".format(severity,
                                                  sorted(RECIPE_SEVERITIES)),
            {"field": "severity", "value": severity})
    recipe["severity"] = severity

    # advisory 固定 true（工具不暴露该参数）：省略视为 true，显式非 True 拒绝
    advisory = fields.get("advisory", True)
    if advisory is not True:
        raise LessonsError(
            "ls_write_error",
            "advisory 必须为 true（recipes 固定 advisory 语义）",
            {"field": "advisory", "value": advisory})
    recipe["advisory"] = True

    # source 不接受用户传入：由 save_recipe 按 root 归属用 _agent_source 标注。

    # 所有落盘字段值必须单行（strict parser 的 field 值是单行的）
    for key in recipe:
        value = recipe[key]
        if isinstance(value, str) and "\n" in value:
            raise LessonsError(
                "ls_write_error",
                "{0} 必须单行（recipes 字段值为单行）".format(key),
                {"field": key})

    # 正文行前缀安全（否则写出的文件无法 round-trip）
    for key in BODY_FIELDS:
        if recipe[key].strip().startswith(_RECIPE_BAD_LINE_PREFIXES):
            raise LessonsError(
                "ls_write_error",
                "{0} 正文以非法前缀起始，格式不安全".format(key),
                {"field": key})
    return recipe


def _render_recipe_block(recipe, render_title=True):
    """渲染单个 recipe 块。

    ``render_title=True`` 时在块上方渲染 ``> <title>`` 注释行（仅首块场景；
    strict parser 只允许首个 heading 之前的 ``>`` 行，块间 ``>`` 行会被判为
    前一块的非法正文，因此追加写入必须传 False）。块体为 9 个
    ``- key: value`` 字段行。
    """
    lines = []
    if render_title and recipe.get("title"):
        lines.append("> " + recipe["title"])
        lines.append("")
    lines.append("### " + recipe["id"])
    for key in RECIPE_FIELDS:
        value = recipe[key]
        if key == "advisory":
            value = "true"
        lines.append("- {0}: {1}".format(key, value))
    return "\n".join(lines) + "\n"


def _existing_recipe_ids(text):
    """扫描全文 heading 行，按出现顺序去重返回既有 ``BP-NNN`` id 列表。

    供 ``ls_recipe_not_found`` 错误消息附既有 id（可行动）；空文本返回 []。
    """
    ids = []
    seen = set()
    for match in _RECIPE_HEADING_RE.finditer(text):
        bid = "BP-" + match.group(1)
        if bid not in seen:
            seen.add(bid)
            ids.append(bid)
    return ids


def _sync_first_block_title(lines, heading_idx, title):
    """首块原地更新：把 heading 上方最近的 ``> `` 行替换为新 title；无则插入。

    仅当 title 非空时由调用方调用；title 缺失时既有 ``> title`` 行保持
    原样（title 仅供调用方使用/汇报，与追加语义一致）。插入位置为 heading
    正上方：``> title`` + 空行（strict parser 允许首个 heading 前的
    ``>`` 行与空行）。返回新 lines。
    """
    if not title:
        return lines
    # 向上跳过空行找最近的 `> ` 行（首块渲染格式为 `> title` / 空行 / heading）
    j = heading_idx - 1
    while j >= 0 and lines[j].strip() == "":
        j -= 1
    if j >= 0 and lines[j].startswith("> "):
        lines[j] = "> " + title
        return lines
    lines.insert(heading_idx, "> " + title)
    lines.insert(heading_idx + 1, "")
    return lines


def _update_recipe_block(existing, recipe_id, recipe):
    """按 recipe_id 原地替换既有块的 9 字段（D3）。

    定位 ``### <recipe_id>`` heading 与其后连续 ``- key: value`` 字段行
    区间，用新 9 字段行原位替换——区间外（header / 其他块 / 块间空行 /
    ``> title`` 行）逐字节不变。被更新块为文件首块时，上方 ``> title`` 行
    同步替换/插入（title 缺失则不动既有行）；非首块 title 不持久化（与
    追加语义一致）。返回 ``(新全文, recipe_id)``。未知 id →
    ``LessonsError('ls_recipe_not_found')``，message 附该 root 既有 id
    列表（可行动）。
    """
    lines = existing.split("\n")
    heading_idx = None
    for idx, line in enumerate(lines):
        # 与 _existing_recipe_ids 同一正则（容忍手工编辑的多余空白）
        match = _RECIPE_HEADING_RE.match(line)
        if match is not None and match.group(1) == recipe_id[3:]:
            heading_idx = idx
            break
    if heading_idx is None:
        ids = _existing_recipe_ids(existing)
        if ids:
            message = ("未找到 recipe {0}，无法原地更新；该 root 既有 id: "
                       "{1}".format(recipe_id, ", ".join(ids)))
        else:
            message = ("未找到 recipe {0}，无法原地更新；该 root 尚无任何 "
                       "recipe 块".format(recipe_id))
        raise LessonsError(
            "ls_recipe_not_found", message,
            {"recipe_id": recipe_id, "existing_ids": ids})

    # 字段区间：heading 之后连续的 `- key: value` 行（save_recipe 自身
    # 产出的规范布局；手工编辑造成的非常规间距由全文自校验兜底拒绝）
    field_end = heading_idx + 1
    while field_end < len(lines) and lines[field_end].startswith("- "):
        field_end += 1

    new_block = ["### " + recipe_id]
    for key in RECIPE_FIELDS:
        value = recipe[key]
        if key == "advisory":
            value = "true"
        new_block.append("- {0}: {1}".format(key, value))

    lines = lines[:heading_idx] + new_block + lines[field_end:]

    # 首块判定：heading 之前无任何 `### BP-NNN` 行
    is_first = True
    for line in lines[:heading_idx]:
        if _RECIPE_HEADING_RE.match(line):
            is_first = False
            break
    if is_first:
        lines = _sync_first_block_title(lines, heading_idx, recipe.get("title"))

    return "\n".join(lines), recipe_id


def save_recipe(root_path, fields, recipe_id=None):
    """把一条用法/流程知识写入 root 的 recipes 文件（追加或按 id 原地更新）。

    ``recipe_id=None``（默认）：以 ``### BP-NNN`` 块**追加**写入——id 自动
    生成（扫描既有块最大序号 + 1，撞号重试），MUST NOT 接受用户自定义 id，
    返回 ``action='created'``（零改动既有语义）。

    ``recipe_id`` 给定：引用**既有** ``### BP-NNN`` 块（格式 ``^BP-\\d{3}$``
    校验）原地替换该块的 9 个字段，**不新增块**，返回 ``action='updated'``；
    引用不存在的 id → ``LessonsError('ls_recipe_not_found')``，message 附
    该 root 既有 id 列表（可行动）。被更新块为文件首块时其上方 ``> title``
    行同步替换/插入；非首块 title 不持久化（与追加语义一致）。root 闸门与
    ``_agent_source`` 归属标注对更新路径同样生效。

    两条路径共用：source 由系统标注（个人库 ``"agent"``，团队 root
    ``"agent@<用户名>"``）；advisory 固定 true；verified_versions 缺省
    ``"unknown"``。title 可选：仅用于校验、首块 ``> title`` 注释行渲染
    （title 仅供调用方自行使用/汇报）；追加到已有块的文件时 title 不落盘
    （strict parser 只允许首个 heading 前的 ``>`` 行，块间仅允许
    ``- field`` 行与空行；9 字段 schema 无 title 字段）。文件缺失/为空 →
    写 header 再追加；写前全文过 ``_best_practices.parse_best_practices``
    自校验保证 round-trip；``_atomic_write_text`` 原子写，失败保留旧文件。
    registry 声明 writable=false / state!=ok → ``root_not_writable``（零写入）。
    成功返回完整 recipe dict（{id, root, category, severity,
    affected_versions, verified_versions, source, advisory, problem, symptom,
    fix, action}）。

    方法论沉淀协议（advisory，非强制）：
    - 沉淀内容是工作流的**原理 / 设计意图 / 方法论**（为什么这么搭），
      不是节点名与参数的复制粘贴；参数仅在用户要求或直接影响复现时收录。
    - 正文索引用资产级标识（capture_workflow_snapshot 的 type_full / hda
      资产全名 + 版本），实例名仅辅助。
    - **禁止本机路径入正文**：不写 HDA 库路径 / hip 完整路径（团队知识库
      跨机器误导源）；资产只用全名 + 版本索引。
    - 改造 / 加深既有知识时传 ``recipe_id`` **原地更新**，不得新增一条
      重复知识；先 ``search_lessons`` 定位既有 id 再更新。
    """
    root_path = _normalize_root_path(root_path)
    root_name = _root_name_for_path(root_path)
    _check_root_writable(root_path)

    recipe = _validate_recipe_fields(fields, root_name)
    agent = _agent_source(root_path)
    recipe["source"] = "agent" if agent is None else "agent@" + agent

    path = recipes_path(root_path)
    existing = ""
    if os.path.isfile(path):
        existing = _read_text(path)

    if recipe_id is None:
        recipe["id"] = _next_recipe_id(existing)
        # title 只在「文件尚无任何 BP 块」的首块场景落盘：strict parser 只
        # 允许首个 heading 之前的 `>` 行，块间 `> title` 会被判为前一块的
        # 非法正文，因此追加写入时 title 不落盘（仍做校验、仍随响应汇报）。
        render_title = _RECIPE_HEADING_RE.search(existing) is None
        block_text = _render_recipe_block(recipe, render_title)
        if existing.strip():
            full_text = existing.rstrip("\n") + "\n\n" + block_text
        else:
            full_text = RECIPES_HEADER + "\n" + block_text
        action = "created"
    else:
        if not isinstance(recipe_id, str) or not _RECIPE_ID_RE.match(recipe_id):
            raise LessonsError(
                "ls_write_error",
                "recipe_id 格式必须是 BP-NNN（如 BP-002），得到 {0!r}".format(
                    recipe_id),
                {"field": "recipe_id", "value": recipe_id,
                 "format": "^BP-\\d{3}$"})
        full_text, recipe_id = _update_recipe_block(existing, recipe_id, recipe)
        recipe["id"] = recipe_id
        action = "updated"

    if _best_practices is not None:
        try:
            _best_practices.parse_best_practices(full_text)
        except _best_practices.BestPracticesError as exc:
            raise LessonsError(
                "ls_write_error",
                "生成的 recipe 无法自校验: {0}".format(exc.message), exc.details)

    _atomic_write_text(path, full_text)
    return {
        "id": recipe["id"],
        "root": recipe["root"],
        "category": recipe["category"],
        "severity": recipe["severity"],
        "affected_versions": recipe["affected_versions"],
        "verified_versions": recipe["verified_versions"],
        "source": recipe["source"],
        "advisory": True,
        "problem": recipe["problem"],
        "symptom": recipe["symptom"],
        "fix": recipe["fix"],
        "action": action,
    }


# ---------------------------------------------------------------------------
# 1.5 inbox
# ---------------------------------------------------------------------------
def _read_inbox(root_path):
    """读 inbox 事件列表。返回 ``(records, bad_raw_lines)``。

    records 为 ``(raw_line, record_dict)`` 二元组列表：raw_line 保留该行
    原文，供坏记录逐字节回写；bad_raw_lines 为非 JSON / 非对象的坏行
    原文。两者共同保证 append-only「永不删除」。OSError 降级为空并打印
    （调用方永不崩）。
    """
    path = inbox_path(root_path)
    if not os.path.isfile(path):
        return [], []
    records = []
    bad = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except ValueError:
                    bad.append(raw)
                    continue
                if isinstance(record, dict):
                    records.append((raw, record))
                else:
                    bad.append(raw)
    except OSError as exc:
        print("lessons._read_inbox: 读取 inbox 失败已降级: {0}".format(exc))
        return [], []
    return records, bad


def _record_count(record):
    """读取记录的 count；不可转 int 的值（如 ``"abc"``）返回 None。

    返回 None 的记录由调用方当坏行处理（原文保留、不参与聚合），单条坏
    记录绝不能中断整个文件或毒化后续事件。
    """
    try:
        return int(record.get("count", 1))
    except (TypeError, ValueError):
        return None


def record_error_event(root_path, tool, error_code, message, source=None):
    """把一条错误事件记录进 inbox（append-only + fingerprint 去重累加）。

    同 fingerprint → 该行 count+1 / updated_at 刷新（原子重写，仍一行一条）；
    新 fingerprint → 追加一行。count 达到 ``PROMOTE_THRESHOLD``(3) 时自动
    触发 ``promote_inbox_to_drafts`` 生成 draft 骨架。

    事件过大（> EVENT_MAX_MESSAGE）或任何写失败 → 跳过并 print，返回
    False；成功返回 True。**永不抛异常**，绝不打断任何命令响应。
    """
    try:
        if not isinstance(message, str) or len(message) > EVENT_MAX_MESSAGE:
            print("record_error_event: 消息过大或非法，事件已跳过 "
                  "(tool={0}, error_code={1})".format(tool, error_code))
            return False
        fingerprint = make_fingerprint(message)
        now = _now_iso()
        raw_records, bad_lines = _read_inbox(root_path)
        # 逐记录防御：count 非法（如 "abc"）的记录整体视为坏行——原文保留
        # （verbatim）、不参与累加；单条坏记录绝不影响其他记录与后续事件。
        records = []
        for raw, record in raw_records:
            if _record_count(record) is None:
                bad_lines.append(raw)
                continue
            records.append(record)
        found = False
        for record in records:
            if record.get("fingerprint") == fingerprint:
                record["count"] = _record_count(record) + 1
                record["updated_at"] = now
                found = True
        if not found:
            records.append({
                "fingerprint": fingerprint,
                "tool": tool,
                "error_code": error_code,
                "message": message,
                "created_at": now,
                "source": source if source else "bridge",
                "count": 1,
                "updated_at": now,
            })
        payload = "".join(bad_lines)
        for record in records:
            payload += json.dumps(record, ensure_ascii=False) + "\n"
        _atomic_write_text(inbox_path(root_path), payload)

        # ≥3 次自动生成 draft 骨架（失败不阻断事件记录）
        for record in records:
            if record.get("fingerprint") == fingerprint and \
                    _record_count(record) >= PROMOTE_THRESHOLD:
                try:
                    promote_inbox_to_drafts(root_path)
                except LessonsError as exc:
                    print("record_error_event: draft 骨架生成失败（已跳过）: "
                          "{0}".format(exc.message))
                break
        return True
    except Exception as exc:  # noqa: BLE001 —— inbox 路径必须永不抛
        print("record_error_event: 写入失败已跳过: {0}".format(exc))
        return False


def _draft_title(message):
    """draft 骨架标题：取 message 首行（截断 60 字符）+ 自动生成后缀。"""
    first = ""
    for line in message.splitlines():
        if line.strip():
            first = line.strip()
            break
    if not first:
        return "未分类问题（自动生成）"
    return first[:60] + "（自动生成）"


def promote_inbox_to_drafts(root_path):
    """把 inbox 中 count 累计 >= 3 且尚无对应 lesson 的 fingerprint 升格。

    每个指纹聚合所有行（去重后单行 count=N 或原始多行各 count=1 均可）。
    已存在同 fingerprint lesson 的指纹跳过（幂等）。生成 draft 骨架：
    symptom=消息原文，problem/fix 为空，category=unclassified，
    severity=medium，source=inbox-auto。返回本次创建的 lesson id 列表。
    """
    raw_records, _bad = _read_inbox(root_path)
    existing, _errors = load_root_lessons(root_path)
    have = set(l.get("fingerprint") for l in existing if l.get("fingerprint"))
    created = []
    # 逐记录防御：count 非法（如 "abc"）的记录跳过——不参与聚合、不抛错，
    # 单条坏记录不能拖垮整个 promote（原文仍保留在 inbox 文件中）。
    records = [record for _raw, record in raw_records
               if _record_count(record) is not None]
    for fingerprint in sorted(set(r.get("fingerprint") for r in records)):
        if not fingerprint or fingerprint in have:
            continue
        total = sum(
            _record_count(r)
            for r in records if r.get("fingerprint") == fingerprint)
        if total < PROMOTE_THRESHOLD:
            continue
        message = ""
        for r in records:
            if r.get("fingerprint") == fingerprint:
                message = r.get("message") or ""
                break
        lesson = save_lesson(root_path, {
            "title": _draft_title(message),
            "category": "unclassified",
            "severity": "medium",
            "affected_versions": "unknown",
            "verified_versions": "unknown",
            "source": "inbox-auto",
            "advisory": False,
            "problem": "",
            "symptom": message,
            "fix": "",
            "fingerprint": fingerprint,
        })
        created.append(lesson["id"])
        have.add(fingerprint)
    return created


# ---------------------------------------------------------------------------
# section 2：multi-root registry
# ---------------------------------------------------------------------------
def _looks_absolute(path):
    """裸绝对路径判定：前导 / 或 \\（含 UNC）、盘符 ``C:\\`` / ``C:/``。"""
    if path.startswith("/") or path.startswith("\\"):
        return True
    if _DRIVE_RE.match(path):
        return True
    return False


def _validate_registry_path(raw_path):
    """校验 registry path：只接受 ${VAR} 占位符或相对路径。

    返回 None（合法）或错误消息（含提示用户改用 ${VAR} / 相对路径）。
    含 ``${`` 但非纯占位符（如 ``${TEAM}/sub``）同样拒绝。
    """
    if _PLACEHOLDER_RE.match(raw_path):
        return None
    if "${" in raw_path or _looks_absolute(raw_path):
        return ("root path 必须是 ${{VAR}} 环境占位符或相对路径（相对 "
                "{0}），裸绝对路径被拒绝: {1!r}").format(_base_dir(), raw_path)
    return None


def load_registry():
    """解析 ``config.json`` 中声明的额外 root（团队共享）。

    返回 ``(roots, errors)``：roots 为校验通过的原始声明
    ``{name, path(原始), priority, writable}``；errors 为
    ``{name 或 index 或 "_config": message}``。任一非法项整体跳过（不入
    roots）。personal 是保留名，禁止在 registry 声明。config 缺失 / 损坏 /
    非数组 → 空 roots + ``_config`` 错误（调用方降级为 personal only）。
    """
    base = _base_dir()
    errors = {}
    config_path = os.path.join(base, CONFIG_FILENAME)
    if not os.path.isfile(config_path):
        return [], {}
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError) as exc:
        return [], {"_config": "config.json 读取/解析失败: {0}".format(exc)}
    if not isinstance(data, list):
        return [], {"_config": "config.json 必须是 root 声明的 JSON 数组"}

    roots = []
    seen_names = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            errors[index] = "第 {0} 项必须是对象".format(index)
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            errors[index] = "第 {0} 项缺少非空 name".format(index)
            continue
        name = name.strip()
        if name == PERSONAL_ROOT_NAME:
            errors[name] = ("personal 是保留 root 名（自动发现），"
                            "不得在 config.json 中声明")
            continue
        if not _ROOT_NAME_RE.match(name):
            errors[name] = ("root 名只接受 [A-Za-z0-9_-]（与 cache 目录名"
                            "同约束，防目录穿越）: {0!r}".format(name))
            continue
        if name in seen_names:
            errors[name] = "重复的 root 名: {0}".format(name)
            continue

        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            errors[name] = "root {0} 缺少非空 path".format(name)
            continue
        raw_path = raw_path.strip()
        path_error = _validate_registry_path(raw_path)
        if path_error is not None:
            errors[name] = path_error
            continue

        priority = DEFAULT_PRIORITY
        if "priority" in item:
            value = item["priority"]
            if isinstance(value, bool) or \
                    not isinstance(value, (int, float)):
                errors[name] = "root {0} 的 priority 必须是 0..1 的数字".format(name)
                continue
            priority = float(value)
            if not (0.0 <= priority <= 1.0):
                errors[name] = "root {0} 的 priority 必须落在 0..1".format(name)
                continue

        writable = False
        if "writable" in item:
            if not isinstance(item["writable"], bool):
                errors[name] = "root {0} 的 writable 必须是布尔值".format(name)
                continue
            writable = item["writable"]

        seen_names.add(name)
        roots.append({"name": name, "path": raw_path,
                      "priority": priority, "writable": writable})
    return roots, errors


def resolve_roots():
    """返回全部 root 描述符列表（personal 恒在首位）。

    每项 ``{name, path, priority, writable, state}``，state ∈
    ``ok | unconfigured | unavailable``：
    - personal：自动发现，path=base/knowledge，priority=1.0，writable=True，
      state 恒为 ok；
    - 注册 root：相对路径对 base dir 解析；${VAR} 未定义 → state=
      ``unconfigured``、path=None（单机模式静默跳过）；已定义但目录不可读
      → state=``unavailable``（bridge 层据此加 ``_warning``）。
    非法声明（path 格式 / priority / writable 等）已被 ``load_registry``
    排除，不出现在本列表。
    """
    base = _base_dir()
    roots, _errors = load_registry()
    resolved = [{
        "name": PERSONAL_ROOT_NAME,
        "path": os.path.join(base, KNOWLEDGE_DIRNAME),
        "priority": 1.0,
        "writable": True,
        "state": "ok",
    }]
    for root in roots:
        raw_path = root["path"]
        placeholder = _PLACEHOLDER_RE.match(raw_path)
        if placeholder:
            env_value = os.environ.get(placeholder.group(1))
            if env_value is None:
                resolved.append({
                    "name": root["name"], "path": None,
                    "priority": root["priority"],
                    "writable": root["writable"],
                    "state": "unconfigured",
                })
                continue
            resolved_path = env_value
        else:
            resolved_path = os.path.join(base, raw_path)
        resolved_path = os.path.abspath(os.path.normpath(resolved_path))
        resolved.append({
            "name": root["name"], "path": resolved_path,
            "priority": root["priority"], "writable": root["writable"],
            "state": "ok" if os.path.isdir(resolved_path) else "unavailable",
        })
    return resolved


def normalize_root_name(name_or_scope=None):
    """把工具参数中的 root 名规范化为 root 描述符（root 白名单）。

    只接受 ``personal``（None / "" 默认）或 registry 中 state=ok 的注册
    root 名；其余（任意路径、unconfigured / unavailable / 未知名）抛
    ``LessonsError('ls_unknown_root')``。工具参数绝不接受任意路径。
    """
    if name_or_scope is None or name_or_scope == "":
        name_or_scope = PERSONAL_ROOT_NAME
    if not isinstance(name_or_scope, str):
        raise LessonsError(
            "ls_unknown_root", "root 名必须是字符串",
            {"value": name_or_scope})
    for desc in resolve_roots():
        if desc["name"] == name_or_scope:
            if desc["state"] != "ok":
                raise LessonsError(
                    "ls_unknown_root",
                    "root {0} 当前不可用（state={1}）".format(name_or_scope,
                                                           desc["state"]),
                    {"name": name_or_scope, "state": desc["state"]})
            return desc
    raise LessonsError(
        "ls_unknown_root",
        "未知 root 名: {0}（仅接受 personal 或 registry 中 state=ok 的 "
        "root 名）".format(name_or_scope),
        {"name": name_or_scope})


def resolve_root_for_read(scope=None):
    """按 scope 解析只读 root 描述符（默认 personal）。

    仅接受 ``personal`` 或注册且 state=ok 的 root 名；其余抛
    ``ls_unknown_root``。
    """
    return normalize_root_name(scope)


def resolve_root_for_write(name=None):
    """按 name 解析可写 root 描述符（默认 personal）。

    语义同 ``resolve_root_for_read``；writable 门禁由 ``save_lesson`` 在
    写入前强制（``root_not_writable``）。
    """
    return normalize_root_name(name)
