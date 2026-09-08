# -*- coding: utf-8 -*-
"""fix-mcp-dead-tools-p0 task 2.1 复现：_populate_fast 产出为零的根因定位。

hython 下直调 node_type_cache.populate(hou)，分别观察 fast / standard 路径
的实际产出与异常。用法：
    hython.exe _repro_populate.py
"""
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

import hou
from houdinimcp import _discovery as disc

print("== hou version:", hou.applicationVersionString())

# 1) 直接跑 _populate_fast 的 exec 代码，看原始产出
code = (
    "import hou\n"
    "import json\n"
    "_result = {}\n"
    "_cats = hou.nodeTypeCategories()\n"
    "_values = (_cats.values() if hasattr(_cats, 'values') else list(_cats))\n"
    "for _cat in _values:\n"
    "    _cn = _cat.name()\n"
    "    _types = []\n"
    "    for _nt in _cat.nodeTypes().values():\n"
    "        _types.append({\n"
    "            'name': _nt.name(),\n"
    "            'label': _nt.label(),\n"
    "            'description': _nt.description(),\n"
    "        })\n"
    "    _result[_cn] = _types\n"
    "print(json.dumps(_result))\n"
)
out = io.StringIO()
err = io.StringIO()
raised = None
try:
    with redirect_stdout(out), redirect_stderr(err):
        exec(code, hou.__dict__)
except Exception:
    raised = traceback.format_exc()
print("== fast exec raised:", bool(raised))
if raised:
    print(raised)
raw = out.getvalue()
print("== fast stdout length:", len(raw))
print("== fast stderr:", err.getvalue()[:500])
if raw:
    try:
        parsed = json.loads(raw)
        print("== fast parsed categories:", len(parsed))
        for k, v in list(parsed.items())[:5]:
            print("   ", k, len(v))
    except Exception as e:
        print("== fast JSON parse failed:", e)

# 2) nodeTypeCategories 的真实形态
cats = hou.nodeTypeCategories()
print("== type(cats):", type(cats))
print("== hasattr values:", hasattr(cats, "values"))
try:
    vals = list(cats.values())
    print("== cats.values() count:", len(vals))
    names = []
    for c in vals[:30]:
        try:
            names.append(c.name())
        except Exception as e:
            names.append("<name failed: %r>" % e)
    print("== category names (first 30):", names)
except Exception:
    traceback.print_exc()
try:
    keys = list(cats.keys())
    print("== cats.keys() (first 30):", keys[:30])
except Exception:
    traceback.print_exc()

# 3) 完整 populate + get
cache = disc.NodeTypeCache()
cache.populate(hou)
print("== after populate: size =", cache.size(), "stats =", cache.stats())
got = cache.get(category="Sop", name_filter="box")
print("== get(Sop, box):", len(got), [g["name"] for g in got[:5]])

# 4) 单独看 Sop category 的 nodeTypes 行为
try:
    sop = cats["Sop"]
    nts = sop.nodeTypes()
    print("== Sop nodeTypes count:", len(nts))
    box = nts.get("box")
    print("== Sop['box'] present:", box is not None)
    if box is not None:
        print("== box name/label:", box.name(), "|", box.label())
        try:
            print("== box description:", repr(box.description()[:80]))
        except Exception as e:
            print("== box description RAISED:", repr(e))
except Exception:
    traceback.print_exc()
