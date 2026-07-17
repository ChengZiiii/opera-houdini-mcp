"""_geo_summary.py — opera-houdini-mcp 轻量级几何概要 + 大几何降级（PR 12）。

模块职责：
- get_geo_summary: 返回 SOP 节点的概要信息 (counts / bbox / attributes /
  groups / sample_points)。当 point_count 超过 max_points_for_full 时自动
  降级 — 跳过 sample_points 与详细 attributes/groups，避免大几何撑爆 MCP。

约束：
- hou 隔离：通过 hou 参数注入（与 _error_nodes / _hscript / _materials /
  _node_info 风格一致），便于在单测中以 stub 替换。
- 不引入类型注解与 f-string
- 不新增 pip 依赖
- 复用 server.py 中 _resolve_geometry_node 的语义：在模块内自包含实现
  （SOP 直接通过 isinstance 检查，OBJ 节点自动取 displayNode，其他抛
  ValueError），不依赖外部方法。
- 不在 MUTATING_COMMANDS 中（只读）
"""


# hou 注入：SOP 类标记 / OBJ displayNode fallback / 错误语义与
# server.py:HoudiniMCPServer._resolve_geometry_node 一致


def _resolve_geometry_node(hou, path):
    """解析 'path' 到 SOP 节点：SOP 路径直接返，OBJ 容器取 displayNode。

    Args:
        hou: hou 模块或 stub。
        path: SOP 或 OBJ 节点路径。

    Returns:
        hou.SopNode (或 stub 等价物)。

    Raises:
        ValueError: 节点不存在 / 节点既非 SOP 也无 displayNode。
    """
    node = hou.node(path)
    if node is None:
        raise ValueError(u"Node not found: {0}".format(path))
    if hasattr(hou, "SopNode") and isinstance(node, hou.SopNode):
        return node
    display = getattr(node, "displayNode", lambda: None)()
    if display is not None:
        return display
    raise ValueError(
        u"{0} has no geometry. Pass a SOP path or a geometry container "
        u"(got {1} node '{2}').".format(
            path,
            node.type().category().name() if hasattr(
                node.type(), "category") else "unknown",
            node.type().name() if hasattr(node.type(), "name") else "unknown"))


def _bbox_six_tuple(bbox):
    """将 hou.BoundingBox 转为 6 元 list [xmin, ymin, zmin, xmax, ymax, zmax]。

    处理 minvec()/maxvec() 返回 Vector3-like 对象（含 None 时安全 fallback 到
    全 0 元组）的边界。
    """
    minv = bbox.minvec()
    maxv = bbox.maxvec()
    if minv is None or maxv is None:
        return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    # Vector3-like supports iteration; convert each coord to float.
    try:
        mn = [float(minv[0]), float(minv[1]), float(minv[2])]
        mx = [float(maxv[0]), float(maxv[1]), float(maxv[2])]
    except (TypeError, IndexError):
        return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    return mn + mx


def _attrib_entry(attrib):
    """生成单个 attribute 的 dict: {name, type, size}."""
    data_type = attrib.dataType()
    type_name = data_type.name() if hasattr(data_type, "name") else str(data_type)
    return {
        "name": attrib.name(),
        "type": type_name,
        "size": attrib.size(),
    }


def _attrib_entry_no_size(attrib):
    """降级模式：生成仅 {name, type} 的 dict（不返 size 详情）."""
    data_type = attrib.dataType()
    type_name = data_type.name() if hasattr(data_type, "name") else str(data_type)
    return {"name": attrib.name(), "type": type_name}


def _group_entry(group):
    """生成单个 group 的 dict: {name, type, size}.

    group.type().name() 在 Houdini 中为 'Point' / 'Primitive' / 'Vertex'；
    我们 normalize 成小写 'point' / 'primitive' / 'vertex' 以便统一。
    """
    gtype = group.type()
    type_name = gtype.name() if hasattr(gtype, "name") else str(gtype)
    type_lower = type_name.lower()
    return {
        "name": group.name(),
        "type": type_lower,
        "size": len(group) if hasattr(group, "__len__") else 0,
    }


def _collect_sample_points(geo, sample_size, raw_attribs):
    """从几何采前 sample_size 个点，每个点包含所有 raw_attribs 的值.

    raw_attribs 是 hou.Attrib 对象列表（不是 dict），用于点上的 attribValue
    查询。跳过空 sample_size 或 0 点几何。
    """
    if sample_size <= 0:
        return []
    total = geo.intrinsicValue("pointcount")
    if total <= 0 or len(raw_attribs) == 0:
        return []
    samples = []
    take = min(int(sample_size), int(total))
    for i, pt in enumerate(geo.iterPoints()):
        if i >= take:
            break
        entry = {}
        for a in raw_attribs:
            try:
                val = pt.attribValue(a)
            except Exception:
                continue
            entry[a.name()] = _jsonable(val)
        samples.append(entry)
    return samples


def _jsonable(value):
    """Convert HOM values (vectors, tuples, ...) to JSON-friendly types."""
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    return str(value)


def _collect_raw_attributes(geo):
    """收集所有 point/prim attribs 的原始 hou.Attrib 对象.

    用于 _collect_sample_points 与 _attrib_entry 转换。
    """
    out = []
    for a in geo.pointAttribs():
        out.append(a)
    for a in geo.primAttribs():
        out.append(a)
    return out


def _collect_attributes_full(geo, raw_attribs):
    """全字段：把 raw_attribs 转成 [{name, type, size}] dict 列表."""
    return [_attrib_entry(a) for a in raw_attribs]


def _collect_attributes_degraded(geo, raw_attribs):
    """降级模式：仅 {name, type}，不返 size."""
    return [_attrib_entry_no_size(a) for a in raw_attribs]


def _collect_groups_full(geo):
    """收集 point + prim groups."""
    out = []
    for g in geo.pointGroups():
        out.append(_group_entry(g))
    for g in geo.primGroups():
        out.append(_group_entry(g))
    return out


def get_geo_summary(hou, node_path, max_points_for_full=1000000,
                    sample_size=10):
    """获取几何节点的概要信息（轻量级，避免返回大几何撑爆 MCP）。

    Args:
        hou: hou 模块（参数注入，便于单测）。
        node_path: SOP 或 OBJ 节点路径（OBJ 自动 resolve 到 displayNode）。
        max_points_for_full: 全字段返回的点数上限。point_count > 此值时
            触发降级 — 跳过 sample_points、跳过详细 attributes/groups。
        sample_size: 采样点数（全字段模式用；sample_size=0 时不返采样）。

    Returns:
        {
            "path": str,                # SOP path (可能经 OBJ resolve)
            "type": str,                # SOP type name
            "point_count": int,
            "primitive_count": int,
            "vertex_count": int,
            "bbox": [xmin, ymin, zmin, xmax, ymax, zmax],
            "attributes": [             # 全字段: [{name, type, size}]
                {"name": str, "type": str, "size": int},
                ...
            ],                          # 降级: [{name, type}] (无 size)
            "attribute_count": int,     # 全部 attribute 总数（不论模式）
            "groups": [                 # 全字段: [{name, type, size}]
                {"name": str, "type": "point"|"primitive"|"vertex",
                 "size": int},
                ...
            ],                          # 降级: [] (不返详情)
            "group_count": int,         # 全部 group 总数
            "sample_points": [          # 仅全字段 + sample_size > 0 时有内容
                {"P": [x, y, z], "N": [nx, ny, nz], ...},
                ...
            ],
            "_degraded": bool,          # True 若 > max_points_for_full
            "_degrade_reason": str      # 仅 _degraded=True 时非空
        }

    Raises:
        ValueError: 节点不存在 / 节点非几何且无 displayNode。
    """
    sop = _resolve_geometry_node(hou, node_path)
    geo = sop.geometry()

    point_count = int(geo.intrinsicValue("pointcount"))
    primitive_count = int(geo.intrinsicValue("primitivecount"))
    vertex_count = int(geo.intrinsicValue("vertexcount"))

    bbox = _bbox_six_tuple(geo.boundingBox())

    # 先收集原始 attrib 对象（全 / 降级共用），便于 sample_points 内部调用
    raw_attribs = _collect_raw_attributes(geo)

    if point_count > max_points_for_full:
        # 降级：跳过 sample_points + 详细 attributes/groups
        degraded_attrs = _collect_attributes_degraded(geo, raw_attribs)
        attribute_count = len(degraded_attrs)
        groups = []
        group_count = sum(1 for _ in geo.pointGroups()) + sum(
            1 for _ in geo.primGroups())
        sample_points = []
        degraded = True
        degrade_reason = (
            u"point_count={0} > {1}，跳过 sample_points / 详细 attributes / "
            u"groups".format(point_count, max_points_for_full))
        attributes = degraded_attrs
    else:
        # 全字段
        full_attrs = _collect_attributes_full(geo, raw_attribs)
        full_groups = _collect_groups_full(geo)
        attributes = full_attrs
        attribute_count = len(full_attrs)
        groups = full_groups
        group_count = len(full_groups)
        sample_points = _collect_sample_points(geo, sample_size, raw_attribs)
        degraded = False
        degrade_reason = u""

    return {
        "path": sop.path(),
        "type": sop.type().name() if hasattr(sop.type(), "name") else str(
            sop.type()),
        "point_count": point_count,
        "primitive_count": primitive_count,
        "vertex_count": vertex_count,
        "bbox": bbox,
        "attributes": attributes,
        "attribute_count": attribute_count,
        "groups": groups,
        "group_count": group_count,
        "sample_points": sample_points,
        "_degraded": degraded,
        "_degrade_reason": degrade_reason,
    }