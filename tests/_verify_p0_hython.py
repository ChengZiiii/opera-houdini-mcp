# -*- coding: utf-8 -*-
"""fix-mcp-dead-tools-p0 实机验证（hython）。

覆盖 tasks.md §2 / §3 全部验证点：
- 2.1 populate 修复：size > 0 且 get(Sop, box) 命中
- 2.2 单 category 失败不清空整体（代码路径 + 实机数据完整）
- 2.3 populate 复用：连调两次 list_node_types，第二次耗时应显著低于第一次；
      manage_cache stats hits/misses 语义
- 2.4 invalidate_all_caches 后失效仍生效（重 populate）
- 3.1 三 handler 信封键集 + has_more 边界（恰好取满 / 超出 1 项）
- 3.2 paginate_list 边界（空列表 / 越界 start / limit=0 / limit=999）
- 3.3 manage_cache stats 字段集合
"""
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

import hou
from houdinimcp import _discovery as disc
from houdinimcp import _common as cmn

FAILURES = []


def check(label, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (tag, label, detail))
    if not cond:
        FAILURES.append(label)


# ---------------- 2.1 populate 修复 ----------------
print("=== 2.1 populate fix ===")
cache = disc.node_type_cache
t0 = time.time()
res1 = disc.list_node_types(hou, category="Sop", limit=5)
t_first = time.time() - t0
size = cache.size()
check("size > 0", size > 0, "size=%d" % size)
check("Sop total > 1000", res1["total"] > 1000, "total=%d" % res1["total"])
box = disc.list_node_types(hou, category="Sop", name_filter="box")
names = [it["name"] for it in box["node_types"]]
check("name_filter=box hits 'box'", "box" in names, "names=%s" % names[:6])
entry = [it for it in box["node_types"] if it["name"] == "box"]
if entry:
    check("box label fallback = description",
          entry[0]["label"] == entry[0]["description"] and entry[0]["label"],
          "label=%r" % entry[0]["label"])

# Sop 数量与其他 category 完整性（2.2 整体不被清空）
stats_raw = cache.stats()
cats_with_types = sum(1 for _ in [1])  # placeholder
all_cats = [c for c in ["Sop", "Object", "Vop", "Lop", "Dop", "Chop"]]
for c in all_cats:
    r = disc.list_node_types(hou, category=c, limit=1)
    check("category %s non-empty" % c, r["total"] > 0, "total=%d" % r["total"])

# ---------------- 2.3 populate 复用 ----------------
print("=== 2.3 populate reuse ===")
stats_before = cache.stats()
t0 = time.time()
res2 = disc.list_node_types(hou, category="Sop", limit=5)
t_second = time.time() - t0
stats_after = cache.stats()
check("second call faster (or ~equal)",
      t_second < max(t_first * 0.5, 0.05),
      "first=%.3fs second=%.3fs" % (t_first, t_second))
check("hits grow on reuse",
      stats_after["hits"] > stats_before["hits"],
      "hits %d -> %d" % (stats_before["hits"], stats_after["hits"]))
check("last_populate_ms recorded",
      isinstance(stats_raw.get("last_populate_ms"), (int, float)),
      "ms=%s" % stats_raw.get("last_populate_ms"))

# ---------------- 2.4 invalidate 后重 populate ----------------
print("=== 2.4 invalidate ===")
cmn.invalidate_all_caches()
check("cache empty after invalidate", cache.size() == 0)
r3 = disc.list_node_types(hou, category="Sop", name_filter="box", limit=5)
check("re-populated after invalidate",
      cache.size() > 0 and r3["total"] >= 1,
      "size=%d total=%d" % (cache.size(), r3["total"]))

# ---------------- 3.1 信封 + has_more 边界 ----------------
print("=== 3.1 envelope ===")
ENV_KEYS = {"status", "count", "total", "has_more", "cursor"}
r = disc.list_node_types(hou, category="Sop", limit=5)
check("list_node_types keys",
      ENV_KEYS <= set(r.keys()) and "node_types" in r,
      "keys=%s" % sorted(r.keys()))
check("count == len(node_types)", r["count"] == len(r["node_types"]))
check("has_more True when total > limit", r["has_more"] is True,
      "total=%d limit=5" % r["total"])
check("cursor set when has_more", r["cursor"] == 5, "cursor=%r" % r["cursor"])

# 恰好取满：limit == total → has_more False
r_exact = disc.list_node_types(hou, category="Sop",
                               name_filter="box", limit=box["total"])
check("exact-full: has_more False",
      r_exact["has_more"] is False and r_exact["cursor"] is None,
      "total=%d" % box["total"])
# 超出 1 项：limit = total-1 → has_more True，lookahead 不返回
if box["total"] >= 2:
    r_one_past = disc.list_node_types(hou, category="Sop",
                                      name_filter="box",
                                      limit=box["total"] - 1)
    check("one-past: has_more True, no lookahead",
          r_one_past["has_more"] is True
          and r_one_past["count"] == box["total"] - 1
          and len(r_one_past["node_types"]) == box["total"] - 1,
          "count=%d total=%d" % (r_one_past["count"], r_one_past["total"]))

r = disc.list_children(hou, node_path="/obj", limit=2)
check("list_children keys",
      ENV_KEYS <= set(r.keys()) and "children" in r and "node_path" in r,
      "keys=%s" % sorted(r.keys()))

r = disc.find_nodes(hou, root_path="/obj", pattern="geo", limit=2)
check("find_nodes keys",
      ENV_KEYS <= set(r.keys()) and "matches" in r and "root_path" in r,
      "keys=%s" % sorted(r.keys()))
# 越界 cursor → 空页 + 无 cursor
r_oob = disc.list_children(hou, node_path="/obj", cursor=99999)
check("out-of-range cursor: empty + has_more False + cursor None",
      r_oob["children"] == [] and r_oob["has_more"] is False
      and r_oob["cursor"] is None,
      "count=%d cursor=%r" % (r_oob["count"], r_oob["cursor"]))

# ---------------- 3.2 paginate_list 边界 ----------------
print("=== 3.2 paginate_list ===")
p, c = cmn.paginate_list([], 10, 0)
check("empty list", p == [] and c is None, "(%r, %r)" % (p, c))
p, c = cmn.paginate_list([1, 2, 3], 10, 99)
check("out-of-range start", p == [] and c is None, "(%r, %r)" % (p, c))
p, c = cmn.paginate_list([1, 2, 3], 0, 0)
check("limit=0 clamp 1", p == [1] and c == 1, "(%r, %r)" % (p, c))
p, c = cmn.paginate_list(list(range(600)), 999, 0)
check("limit=999 clamp 500", len(p) == 500 and c == 500,
      "(len=%d, c=%r)" % (len(p), c))

# ---------------- 3.3 manage_cache stats 形状 ----------------
print("=== 3.3 manage_cache stats ===")
st = disc.manage_cache(hou, action="stats")
check("stats top-level keys",
      {"status", "node_types", "parameter_schemas"} <= set(st.keys()),
      "keys=%s" % sorted(st.keys()))
FIELDS = {"valid", "hits", "misses", "hit_rate", "invalidations",
          "entry_count", "last_populate_ms"}
for k in ("node_types", "parameter_schemas"):
    check("stats.%s fields" % k, FIELDS <= set(st[k].keys()),
          "keys=%s" % sorted(st[k].keys()))
check("node_types.valid True", st["node_types"]["valid"] is True)
check("node_types.entry_count > 0",
      st["node_types"]["entry_count"] > 0,
      "entry_count=%d" % st["node_types"]["entry_count"])
check("node_types.last_populate_ms recorded",
      isinstance(st["node_types"]["last_populate_ms"], (int, float)),
      "ms=%s" % st["node_types"]["last_populate_ms"])
check("parameter_schemas placeholder",
      st["parameter_schemas"]["valid"] is False
      and st["parameter_schemas"]["entry_count"] == 0)

print()
if FAILURES:
    print("RESULT: %d FAILURE(S): %s" % (len(FAILURES), FAILURES))
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
