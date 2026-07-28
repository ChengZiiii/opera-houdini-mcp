"""BEST_PRACTICES 知识库的 strict parser、cache 与 query（stdlib only）。

本模块把 fork 的兼容性 / 生产事故经验整理为可查询、可追溯的 advisory
recipes。recipes 是「先查线索」，**不替代** ``verify_hou_api`` /
``get_houdini_help``，也不替代目标 Houdini 版本的 live verification。

设计要点：
- 仅依赖 Python 标准库（R4 零新增 pip）。不在顶层 import ``hou``。
- ``BEST_PRACTICES.md`` 每块为 ``### BP-NNN``，heading 提供 id，块内恰好
  解析 9 个必填字段：category / severity / affected_versions /
  verified_versions / source / advisory / problem / symptom / fix。
- parser 严格校验：duplicate id、重复 / 未知 / 缺失 field、非法
  severity enum、非法 advisory bool、recipe 外正文，任一失败都使整个
  文件 parse 失败，**绝不返回 partial entries**（task 2.3）。
- 所有 not-found / read / stat / parse / query 错误统一归一为
  ``BestPracticesError(code, message, details)``，bridge 层据此返回完全
  相同 shape 的 error envelope（task 2.2）。
- cache key = absolute path + ``st_mtime_ns`` + size；stat/read 竞态重试
  一次，仍不一致则 ``bp_read_error``（task 2.4）。
- query 对 problem/symptom/fix/category/source 做 casefold 子串匹配，
  category/id 精确过滤，组合为 AND（task 2.5）。
- 成功结果分别报告过滤前 ``total_indexed``、过滤命中 ``matched_count``、
  response-cap 后实际返回 ``returned_count``；``returned_count`` 恒等于
  ``len(practices)``，cap 截断时 ``truncated=true``（tasks 3.2 / 3.3）。
- 最终 dict 仍过 ``apply_response_cap`` 作 defense-in-depth，但 cap 之后
  会重新对齐 ``returned_count``，绝不与 ``practices`` 脱节（task 3.4）。
"""

import json
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
# 常量与 schema
# ---------------------------------------------------------------------------
DEFAULT_BP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "BEST_PRACTICES.md")

# 9 个必填字段（heading 另提供 id，故每条共 id + 9 字段）。
REQUIRED_FIELDS = (
    "category",
    "severity",
    "affected_versions",
    "verified_versions",
    "source",
    "advisory",
    "problem",
    "symptom",
    "fix",
)
KNOWN_FIELDS = frozenset(REQUIRED_FIELDS)

SEVERITIES = frozenset(("high", "medium", "low"))

# advisory bool 字面量（小写）。非这些值视为非法 bool。
_BOOL_TRUE = frozenset(("true", "yes", "1"))
_BOOL_FALSE = frozenset(("false", "no", "0"))
_BOOL_LITERALS = _BOOL_TRUE | _BOOL_FALSE

DEFAULT_MAX_BYTES = 16384

_HEADING_RE = re.compile(r"^###\s+(BP-\d{3})\s*$")
_FIELD_RE = re.compile(r"^-\s+([a-z_]+)\s*:\s*(.*)$")


# ---------------------------------------------------------------------------
# 统一 error 类型（task 2.2）
# ---------------------------------------------------------------------------
class BestPracticesError(Exception):
    """把 not-found / read / stat / parse / query 归一为稳定 code/message/details。

    code 稳定区分：bp_not_found / bp_read_error / bp_parse_error /
    bp_query_error。bridge 层据此构造完全相同 shape 的 error envelope。
    """

    def __init__(self, code, message, details=None):
        super(BestPracticesError, self).__init__(message)
        self.code = code
        self.message = message
        self.details = details if isinstance(details, dict) else {}


# ---------------------------------------------------------------------------
# parser（tasks 2.1 / 2.3）
# ---------------------------------------------------------------------------
def _parse_bool(raw):
    norm = raw.strip().lower()
    if norm not in _BOOL_LITERALS:
        raise ValueError("invalid bool literal: {0!r}".format(raw))
    return norm in _BOOL_TRUE


def _public_entry(bid, fields):
    """对单个 recipe 做字段校验并返回 id + 9 字段的 public dict。"""
    # 缺字段
    present = set(k for k in KNOWN_FIELDS if k in fields)
    missing = KNOWN_FIELDS - present
    if missing:
        raise BestPracticesError(
            "bp_parse_error",
            "{0} 缺失字段: {1}".format(bid, sorted(missing)),
            {"id": bid, "missing": sorted(missing)})

    # severity enum
    severity = fields["severity"].strip().lower()
    if severity not in SEVERITIES:
        raise BestPracticesError(
            "bp_parse_error",
            "{0} 非法 severity {1!r}（合法: {2}）".format(
                bid, fields["severity"], sorted(SEVERITIES)),
            {"id": bid, "field": "severity", "value": fields["severity"]})

    # advisory bool（必须为 true）
    try:
        advisory = _parse_bool(fields["advisory"])
    except ValueError:
        raise BestPracticesError(
            "bp_parse_error",
            "{0} 非法 advisory bool {1!r}".format(bid, fields["advisory"]),
            {"id": bid, "field": "advisory", "value": fields["advisory"]})
    if not advisory:
        raise BestPracticesError(
            "bp_parse_error",
            "{0} advisory 必须为 true".format(bid),
            {"id": bid, "field": "advisory"})

    # source / verified_versions 非空
    for must in ("source", "verified_versions"):
        if not fields[must].strip():
            raise BestPracticesError(
                "bp_parse_error",
                "{0} {1} 不得为空".format(bid, must),
                {"id": bid, "field": must})

    public = {"id": bid}
    for key in REQUIRED_FIELDS:
        value = fields[key]
        public[key] = advisory if key == "advisory" else value.strip()
    public["severity"] = severity
    return public


def parse_best_practices(text):
    """严格解析 BEST_PRACTICES 文本，返回 public entries 列表。

    任一 schema 失败抛 ``BestPracticesError(code='bp_parse_error')``，
    不返回 partial entries。recipe 外只允许 blank / ``#`` / ``>`` 行。
    """
    if not isinstance(text, str):
        raise BestPracticesError(
            "bp_parse_error", "text must be a string",
            {"type": type(text).__name__})

    lines = text.splitlines()
    entries = []
    seen_ids = set()
    current_id = None
    current_fields = None

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()

        heading = _HEADING_RE.match(stripped)
        if heading:
            # 提交上一个 recipe
            if current_id is not None:
                _commit_recipe(current_id, current_fields, entries, seen_ids)
            current_id = heading.group(1)
            current_fields = {}
            continue

        if current_id is None:
            # recipe 外：仅允许 blank / # / > 行
            if stripped == "" or stripped.startswith("#") or stripped.startswith(">"):
                continue
            raise BestPracticesError(
                "bp_parse_error",
                "recipe 外存在正文（line {0}）: {1!r}".format(lineno, stripped),
                {"lineno": lineno, "line": stripped})

        # recipe 内
        if stripped == "":
            continue
        field = _FIELD_RE.match(stripped)
        if field:
            key = field.group(1)
            value = field.group(2)
            if key not in KNOWN_FIELDS:
                raise BestPracticesError(
                    "bp_parse_error",
                    "{0} 未知字段 {1!r}（line {2}）".format(current_id, key, lineno),
                    {"id": current_id, "field": key, "lineno": lineno})
            if key in current_fields:
                raise BestPracticesError(
                    "bp_parse_error",
                    "{0} 重复字段 {1!r}（line {2}）".format(current_id, key, lineno),
                    {"id": current_id, "field": key, "lineno": lineno})
            current_fields[key] = value
            continue

        # recipe 内非 blank / 非 field 行 → 非法正文
        raise BestPracticesError(
            "bp_parse_error",
            "{0} 内出现非法正文（line {1}）: {2!r}".format(current_id, lineno, stripped),
            {"id": current_id, "lineno": lineno, "line": stripped})

    if current_id is not None:
        _commit_recipe(current_id, current_fields, entries, seen_ids)

    if not entries:
        raise BestPracticesError(
            "bp_parse_error", "未找到任何 recipe 块（### BP-NNN）",
            {"heading_format": "### BP-NNN"})
    return entries


def _commit_recipe(bid, fields, entries, seen_ids):
    if bid in seen_ids:
        raise BestPracticesError(
            "bp_parse_error", "重复 id {0}".format(bid), {"id": bid})
    seen_ids.add(bid)
    entries.append(_public_entry(bid, fields))


# ---------------------------------------------------------------------------
# cache（task 2.4）
# ---------------------------------------------------------------------------
_CACHE = {}


def clear_cache():
    """清空进程内 cache（测试 / 显式失效用）。"""
    _CACHE.clear()


def _normalize_path(path):
    if path is None:
        path = DEFAULT_BP_PATH
    if not isinstance(path, str):
        raise BestPracticesError(
            "bp_query_error", "path 必须是字符串或 None",
            {"path": path, "type": type(path).__name__})
    return os.path.abspath(path)


def _stat_file(path):
    try:
        return os.stat(path)
    except FileNotFoundError:
        raise BestPracticesError(
            "bp_not_found", "知识库文件不存在: {0}".format(path), {"path": path})
    except OSError as exc:
        raise BestPracticesError(
            "bp_read_error", "无法 stat 知识库文件: {0}: {1}".format(path, exc),
            {"path": path, "exception": str(exc)})


def _read_with_race_check(path, stat_before):
    """读文件并覆盖 stat/read 竞态：read 后再 stat，不一致重试一次。

    返回 ``(text, final_stat)``；两次都仍变化则 ``bp_read_error``。
    """
    stat_current = stat_before
    for _attempt in range(2):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            raise BestPracticesError(
                "bp_not_found", "知识库文件读取时消失: {0}".format(path),
                {"path": path})
        except OSError as exc:
            raise BestPracticesError(
                "bp_read_error", "无法读取知识库文件: {0}: {1}".format(path, exc),
                {"path": path, "exception": str(exc)})

        try:
            stat_after = os.stat(path)
        except FileNotFoundError:
            raise BestPracticesError(
                "bp_not_found", "知识库文件读取后消失: {0}".format(path),
                {"path": path})
        except OSError as exc:
            raise BestPracticesError(
                "bp_read_error", "无法重新 stat 知识库文件: {0}: {1}".format(path, exc),
                {"path": path, "exception": str(exc)})

        if (stat_after.st_mtime_ns, stat_after.st_size) == \
                (stat_current.st_mtime_ns, stat_current.st_size):
            return text, stat_after
        # 竞态：读期间文件变化，用新 stat 重试一次
        stat_current = stat_after

    raise BestPracticesError(
        "bp_read_error", "知识库文件读取期间持续变化: {0}".format(path),
        {"path": path})


def load_best_practices(path=None):
    """加载并解析知识库，命中 cache 时跳过 read/parse。

    cache key = (absolute path, st_mtime_ns, st_size)。stat/read 竞态重试
    一次。返回 entries 的浅拷贝列表（调用方修改不影响 cache）。
    """
    path = _normalize_path(path)
    stat_before = _stat_file(path)
    key = (path, stat_before.st_mtime_ns, stat_before.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return [dict(entry) for entry in cached]

    text, stat_final = _read_with_race_check(path, stat_before)
    final_key = (path, stat_final.st_mtime_ns, stat_final.st_size)
    if final_key != key:
        # 竞态后 stat 变化：用 final key 再查一次 cache
        cached = _CACHE.get(final_key)
        if cached is not None:
            return [dict(entry) for entry in cached]

    entries = parse_best_practices(text)
    _CACHE[final_key] = entries
    return [dict(entry) for entry in entries]


# ---------------------------------------------------------------------------
# query（task 2.5）
# ---------------------------------------------------------------------------
def _validate_query_params(query, category, bp_id):
    for name, value in (("query", query), ("category", category), ("id", bp_id)):
        if value is not None and not isinstance(value, str):
            raise BestPracticesError(
                "bp_query_error",
                "{0} 必须是字符串或 None，得到 {1}".format(name, type(value).__name__),
                {"param": name, "type": type(value).__name__})


def query_best_practices(entries, query=None, category=None, bp_id=None):
    """对 entries 做过滤：query casefold 子串，category/id 精确，组合 AND。

    返回匹配的 entries 列表（不截断、不 cap）。参数非法抛
    ``BestPracticesError(code='bp_query_error')``。
    """
    _validate_query_params(query, category, bp_id)
    folded = query.casefold() if query is not None else None
    matched = []
    for entry in entries:
        if bp_id is not None and entry.get("id") != bp_id:
            continue
        if category is not None and entry.get("category") != category:
            continue
        if folded is not None:
            haystack = " \n".join((
                entry.get("problem", ""),
                entry.get("symptom", ""),
                entry.get("fix", ""),
                entry.get("category", ""),
                entry.get("source", ""),
            )).casefold()
            if folded not in haystack:
                continue
        matched.append(entry)
    return matched


# ---------------------------------------------------------------------------
# count / cap-aware 组装（tasks 3.2 / 3.3 / 3.4）
# ---------------------------------------------------------------------------
def _encoded_size(obj):
    if cmn is not None:
        fn = getattr(cmn, "_serialized_size", None)
        if callable(fn):
            return fn(obj)
    try:
        return len(json.dumps(
            obj, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return DEFAULT_MAX_BYTES + 1


def _cap_aware_build(matched, total_indexed, max_bytes):
    """按原顺序逐条加入 recipe，直到编码后大小即将超限。

    返回能完整容纳的 practices 列表；``returned_count == len(practices)``。
    """
    practices = []
    matched_count = len(matched)
    for entry in matched:
        candidate = practices + [entry]
        probe = {
            "status": "success",
            "practices": candidate,
            "total_indexed": total_indexed,
            "matched_count": matched_count,
            "returned_count": len(candidate),
            "truncated": len(candidate) < matched_count,
        }
        if _encoded_size(probe) > max_bytes and practices:
            break
        practices = candidate
    return practices


def _error_envelope(exc):
    """统一 error envelope shape（与 success 同形，便于消费方一致处理）。"""
    return {
        "status": "error",
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
        "practices": [],
        "total_indexed": 0,
        "matched_count": 0,
        "returned_count": 0,
    }


def get_best_practices(query=None, category=None, bp_id=None, path=None,
                       response_cap_fn=None, max_bytes=DEFAULT_MAX_BYTES):
    """bridge-local 入口：load → filter → cap-aware 组装 → 统一 envelope。

    本函数 **不建立 Houdini TCP 连接**（task 3.1）。任何 BestPracticesError
    都被归一为同形 error envelope，绝不向调用方抛异常。
    """
    try:
        _validate_query_params(query, category, bp_id)
        entries = load_best_practices(path)
    except BestPracticesError as exc:
        return _error_envelope(exc)

    total_indexed = len(entries)
    matched = query_best_practices(
        entries, query=query, category=category, bp_id=bp_id)
    matched_count = len(matched)

    practices = _cap_aware_build(matched, total_indexed, max_bytes)
    returned_count = len(practices)
    result = {
        "status": "success",
        "practices": practices,
        "total_indexed": total_indexed,
        "matched_count": matched_count,
        "returned_count": returned_count,
        "truncated": returned_count < matched_count,
    }

    # defense-in-depth：最终 dict 仍过 apply_response_cap（task 3.4）。
    cap_fn = response_cap_fn
    if cap_fn is None and cmn is not None:
        cap_fn = getattr(cmn, "apply_response_cap", None)
    if callable(cap_fn):
        try:
            capped = cap_fn(result, max_bytes)
        except Exception:
            capped = None
        if isinstance(capped, dict):
            result = capped
        # cap 之后重新对齐 count，保证 returned_count 不与 practices 脱节。
        final_practices = result.get("practices")
        if not isinstance(final_practices, list):
            final_practices = []
            result["practices"] = final_practices
        result["returned_count"] = len(final_practices)
        if len(final_practices) < matched_count:
            result["truncated"] = True

    return result
