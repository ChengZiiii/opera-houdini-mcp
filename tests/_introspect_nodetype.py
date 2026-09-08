# -*- coding: utf-8 -*-
"""introspect H21 hou.NodeType 可用属性。"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

import hou

nt = hou.nodeTypeCategories()["Sop"].nodeTypes()["box"]
methods = [a for a in dir(nt) if not a.startswith("__")]
print("NodeType attrs:", methods)
for name in ("name", "nameWithCategory", "description", "label",
             "category", "helpUrl", "icon"):
    fn = getattr(nt, name, None)
    if fn is None:
        print("%-18s: MISSING" % name)
    elif callable(fn):
        try:
            v = fn()
            print("%-18s: %r" % (name, str(v)[:100]))
        except Exception as e:
            print("%-18s: RAISED %r" % (name, e))
    else:
        print("%-18s: (attr) %r" % (name, str(fn)[:100]))
