"""_discovery.py — opera-houdini-mcp 节点类型缓存与发现（PR 6）。

模块职责：
- NodeTypeCache：线程安全的 Houdini 节点类型缓存（RLock + stats）
- node_type_cache：模块级单例；import 时自动注册到 cmn._cache_registry
- list_node_types：按 category / name 过滤 + 分页查询节点类型
- list_children：列出节点的子节点（recursive / max_depth / max_nodes / compact）
- find_nodes：在 root_path 下 glob / substring + node_type 过滤查找
- manage_cache：stats / invalidate / warmup 三种动作的桥接入口

设计要点：
- hou 隔离：本模块不顶层 import hou；hou 通过参数注入（测试用 mock）
- 线程安全：NodeTypeCache 用 threading.RLock（允许同一线程重入）
- populate 幂等：多次调用 populate 覆盖 cache 内容，不报错；已填充且未
  失效时跳过全量枚举（fix-mcp-dead-tools-p0）
- TTL=0 语义：populate 立即生效；hits 即便数据未变也自增
- pagination 一致：list/find 函数返主 spec 信封
  {<field>, count, total, has_more, cursor}（多取 1 判 has_more）
- _populate_fast 在 Houdini 端 exec 批量拉（一次性收集所有 category）
- _populate_standard 是 RPC 慢路径占位回退

依赖：
- 仅 Python 3.12 标准库（threading / fnmatch / json / io / contextlib）
- 复用 cmn.paginate_list / cmn.serialize_scene_state
"""
from __future__ import annotations

import fnmatch
import io
import json
import threading
import time
from contextlib import redirect_stdout, redirect_stderr

from . import _common as cmn


__all__ = [
    "NodeTypeCache",
    "node_type_cache",
    "list_node_types",
    "list_children",
    "find_nodes",
    "manage_cache",
    "_populate_fast",
    "_populate_standard",
]


# ---------------------------------------------------------------------------
# Section 1: NodeTypeCache class
# ---------------------------------------------------------------------------
class NodeTypeCache(object):
    """线程安全的 Houdini 节点类型缓存。

    Attributes:
        _lock: threading.RLock（允许同一线程重入）
        _ttl: float = 0（TTL=0 表示 populate 后立即可用；新 populate 覆盖整个 cache）
        _stats: dict{hits, misses, size, last_populated_at}
        _data: dict[category -> list of {name, label, description}]
        _name_index: dict[name -> [categories]]（加速按 name 查找）

    Methods:
        get(category, name_filter=None) -> list[dict]
        populate(hou) -> None
        clear() -> None
        stats() -> dict
        invalidate() -> None
        size() -> int
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._ttl = 0.0
        self._stats = {
            "hits": 0,
            "misses": 0,
            "size": 0,
            "last_populated_at": None,
            "invalidations": 0,
            "last_populate_ms": None,
        }
        self._data = {}
        self._name_index = {}

    def populate(self, hou):
        """调用 _populate_fast；失败则回退到 _populate_standard。

        本方法本身持有 RLock；_populate_fast / _populate_standard 在锁内调用。
        使用 RLock 而非 Lock 保证同一线程重入调用（来自 populate 内的 get 链）
        不会死锁。

        复用（fix-mcp-dead-tools-p0）：已填充（``last_populated_at`` 非 None
        且 ``_data`` 非空）时跳过全量枚举直接返回；失效由
        ``cmn.invalidate_all_caches()``（load_scene / new_scene 已调用）触发，
        与 spec "TTL=0" 语义一致。
        """
        with self._lock:
            if (self._stats.get("last_populated_at") is not None
                    and self._data):
                return
            started = time.time()
            try:
                _populate_fast(self, hou)
            except Exception:
                # fast 路径失败时回退到 standard（fallback path）。
                try:
                    _populate_standard(self, hou)
                except Exception:
                    # 最终失败：清空 cache 不抛异常（populate 幂等）
                    self._data = {}
                    self._name_index = {}
                    self._stats["size"] = 0
            self._stats["last_populate_ms"] = round(
                (time.time() - started) * 1000.0, 3)

    def _rebuild_index_locked(self):
        """重建 name->categories 索引（在 RLock 内已持有锁的情况下调用）。"""
        self._name_index = {}
        for cat, types in self._data.items():
            for entry in types:
                self._name_index.setdefault(entry["name"], []).append(cat)

    def get(self, category=None, name_filter=None):
        """按 category 与 name_filter 过滤；返回 list[{category, name, label, description}]。

        hits 在 lock 内自增（即便过滤结果为空，只要 cache size > 0 且 category
        命中就算 hit；category miss 则算 miss）。
        """
        with self._lock:
            if not self._data:
                # cache 为空 — 是否 miss？避免反复递增，仅记一次
                self._stats["misses"] += 1
                return []

            items = []
            cats_to_iter = (
                [category] if category is not None
                else list(self._data.keys())
            )
            category_hit = False
            for cat in cats_to_iter:
                if cat not in self._data:
                    continue
                category_hit = True
                for entry in self._data[cat]:
                    name = entry.get("name", "")
                    label = entry.get("label", "")
                    if name_filter is not None:
                        if not _matches_filter(name, label, name_filter):
                            continue
                    items.append({
                        "category": cat,
                        "name": name,
                        "label": label,
                        "description": entry.get("description", ""),
                    })

            if category is not None and not category_hit:
                self._stats["misses"] += 1
            else:
                self._stats["hits"] += 1
            return items

    def clear(self):
        """清空 cache + 重置 stats 中的 size/last_populated_at（invalidations 计数保留累加）。"""
        with self._lock:
            self._data = {}
            self._name_index = {}
            self._stats["size"] = 0
            self._stats["last_populated_at"] = None
            self._stats["invalidations"] += 1

    def invalidate(self):
        """clear() 的别名；命名上更贴近典型 cache 术语。"""
        self.clear()

    def stats(self):
        """返回 stats 的浅拷贝（dict）。"""
        with self._lock:
            return dict(self._stats)

    def size(self):
        """返回 cache 中所有 category 的类型总数。"""
        with self._lock:
            return self._stats["size"]


# ---------------------------------------------------------------------------
# Section 2: populate helpers
# ---------------------------------------------------------------------------
def _populate_fast(cache, hou):
    """Houdini 端 Python exec 一次性收集所有 category 的节点类型。

    通过 redirect_stdout / redirect_stderr 在 hou 模块的命名空间中执行
    一段 print(json.dumps(...)) 代码；捕获 stdout 后 json.loads 写回 cache。
    hou 模块被 exec 时的 __name__ 即 'hou'，与 Houdini 内部环境一致。

    fix-mcp-dead-tools-p0：H21 的 ``hou.NodeType`` **没有** ``label()``
    （实机 21.0.596 AttributeError 实锤，正是 _populate_fast 产出为零的
    根因）——label 取值改为 ``label()`` 失败时回退 ``description()``；
    单 category / 单条 entry 解析失败只跳过该条，不清空整体结果。
    """
    code = (
        "import hou\n"
        "import json\n"
        "_result = {}\n"
        "_cats = hou.nodeTypeCategories()\n"
        "_values = (_cats.values() if hasattr(_cats, 'values') else list(_cats))\n"
        "for _cat in _values:\n"
        "    try:\n"
        "        _cn = _cat.name()\n"
        "    except Exception:\n"
        "        continue\n"
        "    _types = []\n"
        "    try:\n"
        "        _nt_dict = _cat.nodeTypes()\n"
        "    except Exception:\n"
        "        _nt_dict = {}\n"
        "    _nt_values = (_nt_dict.values() if hasattr(_nt_dict, 'values')\n"
        "                   else list(_nt_dict))\n"
        "    for _nt in _nt_values:\n"
        "        try:\n"
        "            try:\n"
        "                _label = _nt.label()\n"
        "            except Exception:\n"
        "                _label = ''\n"
        "                try:\n"
        "                    _label = _nt.description()\n"
        "                except Exception:\n"
        "                    _label = ''\n"
        "            try:\n"
        "                _desc = _nt.description()\n"
        "            except Exception:\n"
        "                _desc = ''\n"
        "            _types.append({\n"
        "                'name': _nt.name(),\n"
        "                'label': _label,\n"
        "                'description': _desc,\n"
        "            })\n"
        "        except Exception:\n"
        "            continue\n"
        "    _result[_cn] = _types\n"
        "print(json.dumps(_result))\n"
    )
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    # hou 模块作为 exec 命名空间的 __name__ 必须等于 'hou' 才能让
    # `if __name__ == "__main__"` 等条件与 Houdini 内部兼容；但即使失败，
    # 我们捕获异常并在外层 try/except 中走 _populate_standard 回退。
    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(code, hou.__dict__)
    except Exception:
        # 将异常向上抛，让 cache.populate 的 try/except 触发 fallback。
        raise

    raw = stdout_capture.getvalue().strip()
    if not raw:
        # 没拿到任何 stdout — 视为 fast 失败
        raise RuntimeError("_populate_fast produced no output")

    try:
        parsed = json.loads(raw)
    except Exception:
        raise RuntimeError("_populate_fast output not valid JSON")

    _apply_parsed_to_cache(cache, parsed)


def _populate_standard(cache, hou):
    """RPC 慢路径回退：逐个 category 调 hou.nodeTypeCategories()。

    当 _populate_fast 失败（exec 路径不可用）时使用。
    与 fast 路径语义一致；最终把数据写入同一 cache。
    fix-mcp-dead-tools-p0：H21 ``hou.NodeType`` 无 ``label()`` ——label
    失败时回退 ``description()``；单条 entry 失败只跳过该条。
    """
    cats = hou.nodeTypeCategories()
    values = cats.values() if hasattr(cats, "values") else list(cats)
    parsed = {}
    for cat in values:
        try:
            cat_name = cat.name()
        except Exception:
            continue
        types = []
        try:
            nt_dict = cat.nodeTypes()
        except Exception:
            nt_dict = {}
        for _, nt in nt_dict.items():
            try:
                try:
                    label = nt.label()
                except Exception:
                    label = ""
                    try:
                        label = nt.description()
                    except Exception:
                        label = ""
                try:
                    description = nt.description()
                except Exception:
                    description = ""
                types.append({
                    "name": nt.name(),
                    "label": label,
                    "description": description,
                })
            except Exception:
                continue
        parsed[cat_name] = types
    _apply_parsed_to_cache(cache, parsed)


def _apply_parsed_to_cache(cache, parsed):
    """把 parsed dict 写入 cache（必须在持有 cache._lock 时调用）。"""
    with cache._lock:
        cache._data = {}
        for cat_name, types in parsed.items():
            cache._data[cat_name] = []
            for t in types:
                cache._data[cat_name].append({
                    "name": str(t.get("name", "")),
                    "label": str(t.get("label", "")),
                    "description": str(t.get("description", "")),
                })
        cache._rebuild_index_locked()
        cache._stats["size"] = sum(len(v) for v in cache._data.values())
        cache._stats["last_populated_at"] = time.time()


# ---------------------------------------------------------------------------
# Section 3: filter helper
# ---------------------------------------------------------------------------
def _matches_filter(name, label, pattern):
    """支持两种模式：

    - glob 模式：含 * 或 ? 或 [...] 时按 fnmatch 处理（不区分大小写）
    - 子串匹配：纯字符串时按 case-insensitive contains 处理

    匹配范围：name 或 label 任一命中即视为命中。
    """
    if not isinstance(pattern, str) or not pattern:
        return True
    has_glob = any(ch in pattern for ch in ("*", "?", "["))
    if has_glob:
        if fnmatch.fnmatch(name, pattern):
            return True
        if fnmatch.fnmatch(label, pattern):
            return True
        return False
    p = pattern.lower()
    if p in (name or "").lower():
        return True
    if p in (label or "").lower():
        return True
    return False


# ---------------------------------------------------------------------------
# Section 4: public list/find functions
# ---------------------------------------------------------------------------
_PAGE_MAX_LIMIT = 500


def _paginate_envelope(items, field, limit, cursor, **extra):
    """按主 spec 信封分页（fix-mcp-dead-tools-p0）。

    - 多取 1 项判断 ``has_more``；lookahead 项本身不进入返回
    - ``limit<=0`` clamp 到 1；上限 500；``cursor`` 负数 clamp 到 0
    - ``start`` 越界 → 空 items + ``has_more=False`` + ``cursor=None``
    - ``extra`` 中的键（如 list_children 的 node_path）原样并入信封
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 1
    if limit <= 0:
        limit = 1
    if limit > _PAGE_MAX_LIMIT:
        limit = _PAGE_MAX_LIMIT
    try:
        start = int(cursor)
    except (TypeError, ValueError):
        start = 0
    if start < 0:
        start = 0

    total = len(items)
    env = {
        "status": "success",
        field: [],
        "count": 0,
        "total": total,
        "has_more": False,
        "cursor": None,
    }
    env.update(extra)
    if start >= total:
        return env
    page_plus = items[start:start + limit + 1]
    has_more = len(page_plus) > limit
    page = page_plus[:limit]
    env[field] = page
    env["count"] = len(page)
    env["has_more"] = has_more
    env["cursor"] = (start + limit) if has_more else None
    return env


def list_node_types(hou, category=None, name_filter=None, limit=50, cursor=None):
    """列出 Houdini 节点类型（信封分页，fix-mcp-dead-tools-p0）。

    Args:
        hou: hou 模块（Houdini 端）
        category: 可选，按 category 过滤（如 "Sop", "Object"）
        name_filter: 可选，glob 或子串模糊匹配 name/label
        limit: 分页大小
        cursor: 分页游标

    Returns:
        dict 信封 ``{status, node_types, count, total, has_more, cursor}``
        （多取 1 项判 has_more，不返回 lookahead 项）。
    """
    cache = node_type_cache
    cache.populate(hou)
    items = cache.get(category=category, name_filter=name_filter)
    return _paginate_envelope(items, "node_types", limit, cursor or 0)


def list_children(hou, node_path, recursive=False, max_depth=5,
                  max_nodes=1000, compact=False, limit=50, cursor=None):
    """列出节点的子节点（信封分页，fix-mcp-dead-tools-p0）。

    Args:
        hou: hou 模块
        node_path: 起始节点路径
        recursive: 是否递归子树
        max_depth: 递归最大深度（recursive=True 时生效）
        max_nodes: 最多返回节点数（保护防 OOM）
        compact: True 时仅返 path/type/children_count 三字段
        limit: 分页大小
        cursor: 分页游标

    Returns:
        dict 信封 ``{status, node_path, children, count, total,
        has_more, cursor}``。
    """
    items = _collect_children(hou, node_path, recursive, max_depth, max_nodes, compact)
    return _paginate_envelope(items, "children", limit, cursor or 0,
                              node_path=node_path)


def _collect_children(hou, node_path, recursive, max_depth, max_nodes, compact):
    """递归遍历 hou.node(node_path) 子树，组装 item 列表。

    - max_nodes 截断：超过即停止收集
    - max_depth：max_depth=0 表示仅起始节点本身；>0 则继续到该深度
    """
    items = []
    try:
        root = hou.node(node_path)
    except Exception:
        return items
    if root is None:
        return items

    def _shape(node):
        try:
            path = node.path()
        except Exception:
            path = node_path
        try:
            type_name = node.type().name()
        except Exception:
            type_name = "unknown"
        try:
            type_category = node.type().category().name()
        except Exception:
            type_category = "unknown"
        try:
            children_count = len(node.children())
        except Exception:
            children_count = 0
        if compact:
            return {
                "path": path,
                "type": type_name,
                "children_count": children_count,
            }
        return {
            "path": path,
            "type": type_name,
            "category": type_category,
            "children_count": children_count,
        }

    def _walk_children(parent):
        """列父节点的直接 children。"""
        try:
            children = parent.children()
        except Exception:
            children = []
        for child in children:
            if len(items) >= max_nodes:
                return
            items.append(_shape(child))

    def _walk_deep(parent, remaining):
        """递归：remaining 表示还能往下走几层。remaining<=0 视为不再深入。"""
        if remaining <= 0:
            return
        try:
            children = parent.children()
        except Exception:
            children = []
        for child in children:
            if len(items) >= max_nodes:
                return
            items.append(_shape(child))
            _walk_deep(child, remaining - 1)

    if recursive:
        _walk_deep(root, max_depth)
    else:
        _walk_children(root)
    return items


def find_nodes(hou, root_path, pattern=None, node_type=None,
               limit=50, cursor=None):
    """在 root_path 下用 pattern + node_type 过滤查找节点。

    Args:
        hou: hou 模块
        root_path: 起始路径（默认 "/"）
        pattern: glob 或子串模糊匹配 name
        node_type: 节点类型过滤
        limit / cursor: 分页

    Returns:
        dict 信封 ``{status, root_path, matches, count, total,
        has_more, cursor}``；items 为 list[{name, path, type}]。
    """
    items = _scan_for_nodes(hou, root_path, pattern, node_type)
    return _paginate_envelope(items, "matches", limit, cursor or 0,
                              root_path=root_path)


def _scan_for_nodes(hou, root_path, pattern, node_type):
    """全子树扫描 + glob/子串匹配 + node_type 过滤。"""
    items = []
    try:
        root = hou.node(root_path)
    except Exception:
        return items
    if root is None:
        return items

    try:
        all_nodes = list(root.allSubChildren())
    except Exception:
        # hou 旧版可能没有 allSubChildren — 退化用递归 walk
        all_nodes = []

        def _walk(node):
            try:
                kids = node.children()
            except Exception:
                kids = []
            for k in kids:
                all_nodes.append(k)
                _walk(k)

        _walk(root)

    for node in all_nodes:
        try:
            name = node.path().split("/")[-1]
            path = node.path()
            tname = node.type().name()
        except Exception:
            continue
        if pattern is not None and not _matches_filter(name, "", pattern):
            continue
        if node_type is not None and tname != node_type:
            continue
        items.append({"name": name, "path": path, "type": tname})
    return items


# ---------------------------------------------------------------------------
# Section 5: manage_cache bridge
# ---------------------------------------------------------------------------
def _spec_cache_stats(raw, valid):
    """把 cache 原始 stats 映射为主 spec per-cache 字段集。

    主 spec（缓存管理）要求每个缓存含
    ``{valid, hits, misses, hit_rate, invalidations, entry_count,
    last_populate_ms}``。
    """
    hits = raw.get("hits", 0)
    misses = raw.get("misses", 0)
    lookups = hits + misses
    return {
        "valid": bool(valid),
        "hits": hits,
        "misses": misses,
        "hit_rate": round(float(hits) / lookups, 4) if lookups else 0.0,
        "invalidations": raw.get("invalidations", 0),
        "entry_count": raw.get("size", 0),
        "last_populate_ms": raw.get("last_populate_ms"),
    }


def manage_cache(hou, action):
    """manage_cache 桥接入口：stats / invalidate / warmup 三种动作。

    Args:
        hou: hou 模块
        action: "stats" / "invalidate" / "warmup"

    Returns:
        dict — stats 时返主 spec 形状
        ``{status, node_types: {valid, hits, misses, hit_rate,
        invalidations, entry_count, last_populate_ms},
        parameter_schemas: {同构}}``（parameter_schemas 缓存尚未落地，
        按主 spec 形状输出零值占位 valid=False，不伪造缓存行为）；
        warmup 时返 {"populated": True, "size": ...}；
        invalidate 时返 {"invalidated": True, "size_before": ...}。
    Raises:
        ValueError: action 不在合法集合内。
    """
    if not isinstance(action, str):
        raise ValueError("action must be a string")
    norm = action.strip().lower()
    if norm not in ("stats", "invalidate", "warmup"):
        raise ValueError(
            "action must be one of 'stats', 'invalidate', 'warmup'; got {0!r}".format(action)
        )

    if norm == "stats":
        raw = node_type_cache.stats()
        node_types_stats = _spec_cache_stats(
            raw, valid=raw.get("last_populated_at") is not None)
        # parameter_schemas 缓存尚未实现（get_parameter_schema 当前直查），
        # stats 按主 spec 形状输出零值占位；落地后替换为真实 cache stats。
        parameter_schemas_stats = _spec_cache_stats({}, valid=False)
        return {
            "status": "success",
            "node_types": node_types_stats,
            "parameter_schemas": parameter_schemas_stats,
        }
    if norm == "warmup":
        node_type_cache.populate(hou)
        return {"populated": True, "size": node_type_cache.size()}
    # invalidate
    size_before = node_type_cache.size()
    cmn.invalidate_all_caches()
    return {"invalidated": True, "size_before": size_before,
            "size_after": node_type_cache.size()}


# ---------------------------------------------------------------------------
# Module-level singleton + auto-register with cmn._cache_registry
# ---------------------------------------------------------------------------
node_type_cache = NodeTypeCache()
cmn.register_cache(node_type_cache)
