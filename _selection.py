"""_selection.py — opera-houdini-mcp node-only 选择工具（add-scene-context-selection-materials）。

模块职责：
- ``get_selection()``：固定使用 ``hou.selectedNodes()`` 读取 Houdini 当前
  选中的节点；不接受 ``selectedItems()``，因此不混入 network box、note、
  dot。返回 ``{selected:[{path,type,category}], count}``。
- ``set_selection(node_paths, clear_others=True)``：先验证全部路径，失败
  时 selection 不得部分改变；clear_others 走 ``setSelected(False)`` 而
  **不**调用 ``clearAllSelected()``，避免误清 network box/note/dot。

约束：
- hou 通过第一参数注入；顶层不 ``import hou``。
- 不引入 f-string / 类型注解。
- 不新增 pip 依赖。

设计依据：
- D2（node-only selection）：固定 ``selectedNodes``；``setSelected``
  仅作用于节点对象；不暴露 box/note/dot 通道。
- D3（不部分改变）：所有目标路径在 hou 调用之前校验；任一无效
  返回 ``invalid_node_path`` 且 0 写入。
"""
from . import _common as cmn


# ---------------------------------------------------------------------------
# Section 1: 错误 envelope helper（与 _geo_measure / _hda 保持一致形状）
# ---------------------------------------------------------------------------
def _error(code, message, details=None):
    """统一错误 envelope；``details`` 可为 None。"""
    payload = {"status": "error", "error": {"code": code,
                                            "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _success(data):
    """成功 envelope；保留调用方传入字段。"""
    payload = {"status": "success"}
    for key, value in data.items():
        payload[key] = value
    return payload


# ---------------------------------------------------------------------------
# Section 2: get_selection
# ---------------------------------------------------------------------------
def get_selection(hou):
    """读取当前选中节点列表，**仅**用 ``hou.selectedNodes()``。

    Returns:
        dict: ``{"status": "success", "selected": [...], "count": N}``，
        每项 ``{"path", "type", "category"}``。HOM 异常降级为 error。
    """
    try:
        nodes = hou.selectedNodes()
    except Exception as error:
        return _error("selection_read_failed",
                       "hou.selectedNodes() failed: %s" % error,
                       {"exception": error.__class__.__name__})
    selected = []
    for node in nodes:
        try:
            path = node.path()
        except Exception:
            path = ""
        try:
            type_name = node.type().name()
        except Exception:
            type_name = ""
        try:
            category = node.type().category().name()
        except Exception:
            category = ""
        selected.append({
            "path": path,
            "type": type_name,
            "category": category,
        })
    return _success({
        "selected": selected,
        "count": len(selected),
    })


# ---------------------------------------------------------------------------
# Section 3: set_selection
# ---------------------------------------------------------------------------
def set_selection(hou, node_paths, clear_others=True):
    """覆盖当前节点选择为目标 ``node_paths`` 列表。

    - 全部预校验：任一路径无法解析为 ``hou.node()`` → 立即
      ``invalid_node_path`` 错误，**零部分改变**。
    - ``clear_others=True``：仅对当前 ``selectedNodes()`` 调
      ``setSelected(False)``，**不**调用 ``clearAllSelected()``，避免
      影响 network box / note / dot。
    - ``clear_others=False``：保留其它当前选择，仅追加 / 覆盖目标节点。
    - 目标节点逐个 ``setSelected(True)``；HOM 异常降级为 error。

    Args:
        hou: hou mock / real hou
        node_paths: list[str]；允许空 list（仅清空）
        clear_others: bool

    Returns:
        dict: ``{"status": "success", "selected":[...], "cleared": N,
        "set": M}``。
    """
    if not isinstance(node_paths, list):
        return _error("invalid_node_paths",
                       "node_paths must be a list of strings",
                       {"field": "node_paths",
                        "value_type": type(node_paths).__name__})
    for index, raw in enumerate(node_paths):
        if not isinstance(raw, str) or not raw.strip():
            return _error("invalid_node_path",
                           "node_paths[%d] must be a non-empty string"
                           % index,
                           {"field": "node_paths", "index": index,
                            "value": raw})

    # 预解析：所有路径必须存在；任一失败 → 0 写入
    resolved = []
    for raw in node_paths:
        try:
            node = hou.node(raw)
        except Exception as error:
            return _error("invalid_node_path",
                           "hou.node(%r) failed: %s" % (raw, error),
                           {"field": "node_paths", "value": raw,
                            "exception": error.__class__.__name__})
        if node is None:
            return _error("invalid_node_path",
                           "no node at path %r" % raw,
                           {"field": "node_paths", "value": raw})
        resolved.append(node)

    # 第一步：若 clear_others，对 selectedNodes() 逐个 setSelected(False)
    cleared = 0
    if clear_others:
        try:
            current = hou.selectedNodes()
        except Exception as error:
            return _error("selection_read_failed",
                           "hou.selectedNodes() failed: %s" % error,
                           {"exception": error.__class__.__name__})
        for node in current:
            try:
                node.setSelected(False)
                cleared += 1
            except Exception as error:
                return _error("selection_clear_failed",
                               "setSelected(False) failed: %s" % error,
                               {"exception": error.__class__.__name__})

    # 第二步：把目标节点 setSelected(True)
    set_count = 0
    for node in resolved:
        try:
            node.setSelected(True)
            set_count += 1
        except Exception as error:
            return _error("selection_set_failed",
                           "setSelected(True) failed: %s" % error,
                           {"exception": error.__class__.__name__})

    # 二次读出实际 selectedNodes 列表作为响应
    try:
        result_nodes = hou.selectedNodes()
    except Exception:
        result_nodes = resolved
    selected_out = []
    for node in result_nodes:
        try:
            path = node.path()
        except Exception:
            path = ""
        try:
            type_name = node.type().name()
        except Exception:
            type_name = ""
        try:
            category = node.type().category().name()
        except Exception:
            category = ""
        selected_out.append({
            "path": path,
            "type": type_name,
            "category": category,
        })

    return _success({
        "selected": selected_out,
        "count": len(selected_out),
        "cleared": cleared,
        "set": set_count,
    })
