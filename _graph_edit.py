"""外部/houdinimcp/_graph_edit.py — PR 9 图编辑增强的纯函数实现。

提供 5 个独立、hou 可注入的图编辑操作。所有函数接受 hou 作为第一参数，
方便在单测中以 stub 替换。

设计原则：
- hou 隔离：函数第一参数是 hou（与 _materials / _hscript / _scene 风格一致）。
- 错误处理：节点不存在抛 ValueError；颜色分量超出 [0,1] 自动 clamp；
  network_box 中缺失的节点静默跳过。
- 返回值：所有函数返回 dict，便于 bridge 直接透传。
"""
from . import _common as cmn


def reorder_inputs(hou, node_path, new_order):
    """重新排列节点的输入顺序。

    Args:
        hou: hou 模块或 stub。
        node_path: 目标节点路径。
        new_order: list of input_index，按新顺序排列（如 [2, 0, 1] 表示把
                   原 input 2 移到 input 0，原 input 0 移到 input 1，原
                   input 1 移到 input 2）。空 list 表示全部断开。

    Returns:
        {"path": ..., "old_order": [...], "new_order": [...], "success": True}
    """
    node = hou.node(node_path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(node_path))

    # 收集当前所有已连接输入 (input_index, output_node, output_index)
    current = []
    for conn in node.inputConnectors():
        current.append((conn.input_index, conn.output_node,
                        getattr(conn, "output_index", 0)))

    old_order = sorted(idx for idx, _, _ in current)

    # 全部断开
    for idx, _, _ in current:
        node.setInput(idx, None)

    # 按 new_order 重连：new_order[i] 是 old input index，要放到新位置 i
    for new_idx, old_idx in enumerate(new_order):
        src_node = None
        src_out = 0
        found = False
        for idx, candidate, out_idx in current:
            if idx == old_idx:
                src_node = candidate
                src_out = out_idx
                found = True
                break
        if not found:
            # old_idx 没在 current 中（可能原本就没连接），跳过
            continue
        node.setInput(new_idx, src_node, src_out)

    return {
        "path": node.path(),
        "old_order": old_order,
        "new_order": list(new_order),
        "success": True,
    }


def layout_children(hou, parent_path, horizontal_spacing=2.0,
                    vertical_spacing=1.5, direction="horizontal"):
    """布局父节点下的子节点。

    通过手动 setPosition 实现，跨 Houdini 版本可移植。
    direction=horizontal 时子节点沿 x 轴排列；vertical 时沿 y 轴排列。

    Args:
        hou: hou 模块或 stub。
        parent_path: 父节点路径。
        horizontal_spacing: 水平间距（Houdini units）。
        vertical_spacing: 垂直间距。
        direction: "horizontal"（默认）或 "vertical"。

    Returns:
        {"parent_path": ..., "children_count": N, "direction": ...,
         "spacing": [h, v]}
    """
    parent = hou.node(parent_path)
    if parent is None:
        raise ValueError(u"父节点不存在: {0}".format(parent_path))

    children = list(parent.children())
    for i, child in enumerate(children):
        if direction == "vertical":
            pos = (0.0, -i * vertical_spacing)
        else:
            pos = (i * horizontal_spacing, 0.0)
        # H21+ SWIG 要求 hou.Vector2 实例，raw tuple 会抛
        # 'argument 2 of type std::vector<double>...' type-check 错。
        # 参考 HoudiniMCPRender.py:124,133 的正确用法。
        child.setPosition(hou.Vector2(pos[0], pos[1]))

    return {
        "parent_path": parent.path(),
        "children_count": len(children),
        "direction": direction,
        "spacing": [horizontal_spacing, vertical_spacing],
    }


def set_node_position(hou, node_path, x, y):
    """设置节点在 network editor 中的位置。

    Args:
        hou: hou 模块或 stub。
        node_path: 节点路径。
        x: x 坐标。
        y: y 坐标。

    Returns:
        {"path": ..., "position": [x, y], "success": True}
    """
    node = hou.node(node_path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(node_path))
    # H21+ SWIG 要求 hou.Vector2 实例，raw tuple 会抛 type-check 错。
    node.setPosition(hou.Vector2(x, y))
    return {
        "path": node.path(),
        "position": [x, y],
        "success": True,
    }


def set_node_color(hou, node_path, r, g, b):
    """设置节点颜色（自动 clamp 到 [0, 1]）。

    Args:
        hou: hou 模块或 stub。
        node_path: 节点路径。
        r, g, b: 颜色分量。

    Returns:
        {"path": ..., "color": [r, g, b], "success": True}
    """
    r = max(0.0, min(1.0, float(r)))
    g = max(0.0, min(1.0, float(g)))
    b = max(0.0, min(1.0, float(b)))
    node = hou.node(node_path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(node_path))
    node.setColor(hou.Color((r, g, b)))
    return {
        "path": node.path(),
        "color": [r, g, b],
        "success": True,
    }


def create_network_box(hou, parent_path, name=None, node_paths=None):
    """在父节点下创建 network box，可选包含若干节点。

    Args:
        hou: hou 模块或 stub。
        parent_path: 父节点路径。
        name: 可选，box 名；None 时由 Houdini 自动命名。
        node_paths: 可选，要包含到此 box 的节点路径列表；缺失节点静默跳过。

    Returns:
        {"path": ..., "name": ..., "nodes_in_box": [...]}
    """
    parent = hou.node(parent_path)
    if parent is None:
        raise ValueError(u"父节点不存在: {0}".format(parent_path))
    box = parent.createNetworkBox(name=name) if name else parent.createNetworkBox()
    if node_paths:
        for np in node_paths:
            n = hou.node(np)
            if n is not None:
                box.addNode(n)
    return {
        "path": parent.path(),
        "name": box.name(),
        "nodes_in_box": list(node_paths) if node_paths else [],
    }