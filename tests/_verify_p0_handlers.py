# -*- coding: utf-8 -*-
"""task 3.1 附加验证：直调 server.py 三个 thin handler（不启动 socket）。

handler 只引用模块级 hou / disc，不使用 self —— 用 None 作 self 直调
unbound method 即可验证信封组装路径。
"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

import hou
from houdinimcp import server as srv

H = srv.HoudiniMCPServer

failures = []

r = H.list_node_types(None, category="Sop", name_filter="box", limit=3)
ok = (isinstance(r, dict) and "node_types" in r and r.get("status") == "success"
      and {"count", "total", "has_more", "cursor"} <= set(r))
print("list_node_types handler:", "PASS" if ok else "FAIL", sorted(r.keys()))
if not ok:
    failures.append("list_node_types")

r = H.list_children(None, node_path="/obj", limit=2)
ok = (isinstance(r, dict) and "children" in r and r.get("status") == "success"
      and {"count", "total", "has_more", "cursor", "node_path"} <= set(r))
print("list_children handler:", "PASS" if ok else "FAIL", sorted(r.keys()))
if not ok:
    failures.append("list_children")

r = H.find_nodes(None, root_path="/obj", pattern="geo", limit=2)
ok = (isinstance(r, dict) and "matches" in r and r.get("status") == "success"
      and {"count", "total", "has_more", "cursor", "root_path"} <= set(r))
print("find_nodes handler:", "PASS" if ok else "FAIL", sorted(r.keys()))
if not ok:
    failures.append("find_nodes")

r = H.manage_cache(None, action="stats")
ok = (isinstance(r, dict)
      and {"node_types", "parameter_schemas"} <= set(r)
      and "valid" in r.get("node_types", {}))
print("manage_cache handler:", "PASS" if ok else "FAIL", sorted(r.keys()))
if not ok:
    failures.append("manage_cache")

if failures:
    sys.exit(1)
print("ALL HANDLER CHECKS PASSED")
