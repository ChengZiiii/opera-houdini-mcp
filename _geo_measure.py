"""_geo_measure.py — opera-houdini-mcp 几何测量与导出工具（PR 22）。

提供 8 个工具：
- ``get_bounding_box``：解包 ``geo.intrinsicValue("bounds")`` 的
  ``(xmin, xmax, ymin, ymax, zmin, zmax)`` 6 元组，返回
  ``{min, max, size, center}``。
- ``get_groups``：返回四类 groups（point/prim/vertex/edge）的列表。
- ``get_group_members``：按 ``(offset, limit)`` 分页；vertex 成员含
  ``{prim_index, vertex_index, point_index}``，edge 成员为排序后的
  ``[point_a, point_b]``。
- ``get_attrib_values``：按 owner（point/prim/vertex/detail）+ storage +
  tuple-size 分派，原生分页 ``(offset, limit, total, next_offset)``。
- ``get_prim_intrinsics``：必须传入 ``prim_index``，仅读指定 primitive 的
  intrinsicNames/intrinsicValue。
- ``find_nearest_point``：用 ``geo.nearestPoint(position, max_radius=...)``
  拿到 ``hou.Point | None``；None 时 ``point_index / point_position /
  distance`` 全部 null。
- ``set_detail_attrib``：在输入 SOP 父网络创建 Attribute Create SOP，
  class=detail，**不**调用 ``node.geometry()`` 写方法；创建/连接/配置是
  单 undo group 的连续步骤，失败时清理半成品。
- ``geo_export``：基于 ``hou.Geometry.saveToFile`` 的 translator registry
  （bgeo / geo / bgeo.gz / bgeo.lzma / bgeo.bz2），同目录临时文件
  fsync + ``os.replace`` 原子覆盖；``overwrite=False`` 时目标存在返回
  ``target_exists``，失败清理临时文件。

模块职责与约束：
- hou 通过第一参数注入，顶层不 ``import hou``。
- 不引入 f-string / 类型注解，匹配既有 server.py 风格。
- 不引入新的 pip 依赖；仅使用 Python 3.12 标准库。
- 错误一律 ``{"status": "error", "error": {"code", "message", "details"}}``
  envelope，code 为稳定字符串。
- 所有路径在 hou 调用之前校验；hou 异常降级为 error dict 而非抛异常。
- 不修改 cooked Geometry 的写方法（``setGlobalAttribValue`` 等），仅
  通过 SOP 节点配置产生可由 Houdini undo 恢复的副作用。

设计依据：
- D1（translator 与原子覆盖）：仅暴露 H21+ 实机 ``saveToFile`` 真正
  支持的字符串 token；临时文件 ``os.replace`` 保证最终状态为「要么
  旧文件，要么完整新文件」，**不**出现 0 字节 / 半截目标。
- D2（cooked Geometry 只读）：``set_detail_attrib`` 必须创建独立
  Attribute Create SOP，避免在 MUTATING_COMMANDS 之外出现
  ``.geometry().setGlobalAttribValue()`` 这类不被 undo 覆盖的写入。
- D3（查询契约）：bounds / groups / attribs / intrinsics / nearest
  严格按 design.md §"D3 准确的查询契约"。
- D4（四类 groups）：point/prim/vertex/edge 四类全列；edge 在 H21
  ``geo.edgeGroups()`` 公开。
- D5（三分类互斥）：8 个 server commands = 1 MUTATING +
  7 NO_UNDO；本 change 不新增 READ_ONLY_COMMANDS 成员（cooked
  Geometry 访问可能触发 SOP cook，归 NO_UNDO）。
"""
import math
import os
import tempfile

from . import _common as cmn


# ---------------------------------------------------------------------------
# Section 1: 常量与 translator registry
# ---------------------------------------------------------------------------
# D1: 仅暴露 ``hou.Geometry.saveToFile`` 经 H21+ 实机验证可接受的字符串
# token；key 既是 user 传入的 ``format``，也直接作为 saveToFile 的
# ``file_type`` 参数；value 的 ``extension`` 仅用于校验请求路径的扩展名。
TRANSLATOR_REGISTRY = {
    "bgeo": {"extension": ".bgeo", "save_type": "bgeo"},
    "bgeo.gz": {"extension": ".bgeo.gz", "save_type": "bgeo.gz"},
    "bgeo.lzma": {"extension": ".bgeo.lzma", "save_type": "bgeo.lzma"},
    "bgeo.bz2": {"extension": ".bgeo.bz2", "save_type": "bgeo.bz2"},
    "geo": {"extension": ".geo", "save_type": "geo"},
}

# ``geo_export`` 原子替换的临时文件后缀（必须与 translator extension
# 区分，避免 rename 跨格式的歧义）。
_TEMP_SUFFIX = ".tmp_export"

# ``get_group_members`` 默认分页。
_DEFAULT_PAGE_LIMIT = 1000

# ``get_attrib_values`` 默认分页。
_DEFAULT_ATTRIB_PAGE_LIMIT = 1000

# ``get_attrib_values`` 最大 limit（避免单页过大撑爆 response）。
_MAX_ATTRIB_PAGE_LIMIT = 100000

# ``find_nearest_point`` 默认 max_radius。
_DEFAULT_MAX_RADIUS = 1.0

# ``set_detail_attrib`` 支持的 attrib_type（Houdini Attribute Create SOP
# 的 type 参数 menu，0=Float, 1=Int, 2=String, 3=Vector 等）。
# 我们仅暴露数值 / 字符串 / 标量 vector 四种，避免半配置节点。
_VALID_DETAIL_TYPES = frozenset({"float", "int", "string", "vector"})


# ---------------------------------------------------------------------------
# Section 2: 错误 envelope helper
# ---------------------------------------------------------------------------
def _error(code, message, details=None):
    """构造统一错误 envelope；``details`` 可为 None 或 dict."""
    payload = {"status": "error", "error": {"code": code,
                                            "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _success(data):
    return {"status": "success", "result": data}


# ---------------------------------------------------------------------------
# Section 3: 内部 helper（共用 hou 解析与 JSON-safe 转换）
# ---------------------------------------------------------------------------
def _resolve_geometry_node(hou, path):
    """复用 ``_geo_summary._resolve_geometry_node`` 风格的解析。

    解析 SOP 路径直接返；OBJ 容器取 displayNode；其他抛
    ValueError（与 server.py 中既有契约保持一致）。
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
        u"{0} has no geometry. Pass a SOP path or a geometry container.".format(
            path))


def _jsonable(value):
    """递归把 HOM 值（vectors, tuples, ...）转 JSON-friendly 类型。"""
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    try:
        # hou.Vector / hou.Color：取 iterable 的前 3 个 float
        return [float(x) for x in value]
    except (TypeError, ValueError):
        return str(value)


def _coerce_int(name, value):
    """接受 int、拒 bool / 非数值 / 负数；返回 {"value": int} 或 error dict."""
    if isinstance(value, bool):
        return {"status": "error", "message": (
            "must be a JSON number; bool is not accepted"), "field": name}
    if not isinstance(value, int):
        return {"status": "error", "message": (
            "must be an integer"), "field": name}
    if value < 0:
        return {"status": "error", "message": (
            "must be >= 0"), "field": name}
    return {"value": value}


def _coerce_position(value):
    """``find_nearest_point`` 接受 ``[x, y, z]`` 或 tuple（3 元素）。"""
    if not isinstance(value, (list, tuple)):
        return {"status": "error", "message": (
            "position must be a list/tuple of 3 numbers")}
    if len(value) != 3:
        return {"status": "error", "message": (
            "position must contain exactly 3 elements")}
    out = []
    for index, coord in enumerate(value):
        if isinstance(coord, bool) or not isinstance(coord, (int, float)):
            return {"status": "error", "message": (
                "position[%d] must be a JSON number") % index}
        cf = float(coord)
        if not math.isfinite(cf):
            return {"status": "error", "message": (
                "position[%d] must be finite") % index}
        out.append(cf)
    return {"value": out}


def _coerce_limit(name, value, default=_DEFAULT_PAGE_LIMIT):
    """接受 int、拒 bool / 零 / 负数（None 走 default）。"""
    if value is None:
        return {"value": default}
    if isinstance(value, bool):
        return {"status": "error", "message": (
            "%s must be a JSON number; bool is not accepted") % name,
                "field": name}
    if not isinstance(value, int):
        return {"status": "error", "message": (
            "%s must be an integer") % name, "field": name}
    if value <= 0:
        return {"status": "error", "message": (
            "%s must be > 0") % name, "field": name}
    return {"value": value}


# ---------------------------------------------------------------------------
# Section 4: get_bounding_box — 正确解包 6 元 + 派生 size / center
# ---------------------------------------------------------------------------
def get_bounding_box(hou, node_path):
    """解包 ``geo.intrinsicValue("bounds")`` 的 ``(xmin,xmax,ymin,ymax,
    zmin,zmax)`` 6 元组，返回 ``{min, max, size, center}``。

    HOM 契约（实测 H21.0.596）：``intrinsicValue("bounds")`` 返回
    ``(xmin, xmax, ymin, ymax, zmin, zmax)``（这是 Houdini 几何 intrinsic
    标准布局，与 box SOP 的 ``size`` 内部顺序一致）。空几何 / cook error
    返回结构化 error dict。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    try:
        sop = _resolve_geometry_node(hou, node_path)
        raw = sop.geometry().intrinsicValue("bounds")
    except ValueError as err:
        return {"status": "error", "message": str(err),
                "field": "node_path"}
    except Exception as err:
        return {"status": "error", "message": (
            "boundingBox read failed: %s") % err,
                "exception": err.__class__.__name__}

    # Houdini bounds 是 6 元 sequence（list / tuple / hou.Vector3 数组）
    try:
        xmin = float(raw[0])
        xmax = float(raw[1])
        ymin = float(raw[2])
        ymax = float(raw[3])
        zmin = float(raw[4])
        zmax = float(raw[5])
    except (TypeError, IndexError, ValueError) as err:
        return {"status": "error", "message": (
            "bounds intrinsic has unexpected shape: %s") % err,
                "exception": err.__class__.__name__}

    sx = xmax - xmin
    sy = ymax - ymin
    sz = zmax - zmin
    return _success({
        "min": [xmin, ymin, zmin],
        "max": [xmax, ymax, zmax],
        "size": [sx, sy, sz],
        "center": [
            (xmin + xmax) / 2.0,
            (ymin + ymax) / 2.0,
            (zmin + zmax) / 2.0,
        ],
    })


# ---------------------------------------------------------------------------
# Section 5: get_groups — 四类 groups schema
# ---------------------------------------------------------------------------
def get_groups(hou, node_path):
    """返回 ``{point, prim, vertex, edge}`` 四类 group 名列表。

    每类 name-only；edge 在 H21+ 公开（``geo.edgeGroups()``）。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    try:
        sop = _resolve_geometry_node(hou, node_path)
        geo = sop.geometry()
        point_names = [g.name() for g in geo.pointGroups()]
        prim_names = [g.name() for g in geo.primGroups()]
        vertex_names = [g.name() for g in geo.vertexGroups()]
        edge_callable = getattr(geo, "edgeGroups", None)
        if callable(edge_callable):
            edge_names = [g.name() for g in edge_callable()]
        else:
            edge_names = []
    except ValueError as err:
        return {"status": "error", "message": str(err),
                "field": "node_path"}
    except Exception as err:
        return {"status": "error", "message": (
            "groups read failed: %s") % err,
                "exception": err.__class__.__name__}
    return _success({
        "groups": {
            "point": point_names,
            "prim": prim_names,
            "vertex": vertex_names,
            "edge": edge_names,
        },
    })


# ---------------------------------------------------------------------------
# Section 6: get_group_members — 分页 + vertex / edge 规范 schema
# ---------------------------------------------------------------------------
def _group_lookup(geo, group_type, group_name):
    """按 group_type 返回 hou.Group 对象；未知 type 返回结构化 error."""
    type_lower = group_type.lower()
    if type_lower == "point":
        groups = geo.pointGroups()
    elif type_lower == "prim" or type_lower == "primitive":
        groups = geo.primGroups()
    elif type_lower == "vertex":
        groups = geo.vertexGroups()
    elif type_lower == "edge":
        edge_callable = getattr(geo, "edgeGroups", None)
        if not callable(edge_callable):
            return {"status": "error", "message": (
                "edge groups not supported by this Houdini version")}
        groups = edge_callable()
    else:
        return {"status": "error", "message": (
            "unknown group_type %r (use point/prim/vertex/edge)") % group_type,
                "field": "group_type"}
    for g in groups:
        if g.name() == group_name:
            return {"group": g}
    return {"status": "error", "message": (
        "group %r not found in %s groups") % (group_name, group_type),
            "field": "group_name"}


def get_group_members(hou, node_path, group_type, group_name,
                      offset=0, limit=_DEFAULT_PAGE_LIMIT):
    """返回分页成员 + ``{total, next_offset}``。

    - point / prim：成员为编号 int（直接 ``g.iterIndices()`` 或迭代）。
    - vertex：``{prim_index, vertex_index, point_index}``。
    - edge：``[min_point, max_point]``（端点对排序，保证 ``a <= b``）。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    if not isinstance(group_name, str) or not group_name.strip():
        return {"status": "error", "message": (
            "group_name must be a non-empty string"), "field": "group_name"}
    if not isinstance(group_type, str):
        return {"status": "error", "message": (
            "group_type must be a string"), "field": "group_type"}
    offset_check = _coerce_int("offset", offset)
    if offset_check.get("status") == "error":
        return offset_check
    limit_check = _coerce_limit("limit", limit)
    if limit_check.get("status") == "error":
        return limit_check
    start = offset_check["value"]
    size = limit_check["value"]

    try:
        sop = _resolve_geometry_node(hou, node_path)
        geo = sop.geometry()
        lookup = _group_lookup(geo, group_type, group_name)
        if lookup.get("status") == "error":
            return lookup
        g = lookup["group"]
    except ValueError as err:
        return {"status": "error", "message": str(err),
                "field": "node_path"}
    except Exception as err:
        return {"status": "error", "message": (
            "group_members read failed: %s") % err,
                "exception": err.__class__.__name__}

    type_lower = group_type.lower()
    if type_lower == "point":
        # PointGroup 在 H21 公开 ``iterPoints()`` 与 ``points``，
        # 不暴露 ``iterIndices``。
        all_members = _point_indices(g)
        total = len(all_members)
        page = all_members[start:start + size]
        next_offset = start + len(page) if (start + len(page)) < total else None
        return _success({
            "group_type": "point",
            "group_name": group_name,
            "members": [int(x) for x in page],
            "offset": start,
            "limit": size,
            "total": total,
            "next_offset": next_offset,
        })
    if type_lower == "prim" or type_lower == "primitive":
        all_members = _prim_indices(g)
        total = len(all_members)
        page = all_members[start:start + size]
        next_offset = start + len(page) if (start + len(page)) < total else None
        return _success({
            "group_type": "prim",
            "group_name": group_name,
            "members": [int(x) for x in page],
            "offset": start,
            "limit": size,
            "total": total,
            "next_offset": next_offset,
        })
    if type_lower == "vertex":
        iter_vertices = getattr(g, "iterVertices", None)
        if callable(iter_vertices):
            vertices = list(iter_vertices())
        else:
            vertices = []
        total = len(vertices)
        page = vertices[start:start + size]
        members = []
        for vertex in page:
            try:
                prim = vertex.prim()
                prim_index = int(prim.number())
                vertex_index = int(vertex.number())
            except Exception:
                prim_index = -1
                vertex_index = -1
            try:
                point_index = int(vertex.point().number())
            except Exception:
                point_index = -1
            members.append({
                "prim_index": prim_index,
                "vertex_index": vertex_index,
                "point_index": point_index,
            })
        next_offset = start + len(members) if (start + len(members)) < total else None
        return _success({
            "group_type": "vertex",
            "group_name": group_name,
            "members": members,
            "offset": start,
            "limit": size,
            "total": total,
            "next_offset": next_offset,
        })
    if type_lower == "edge":
        # Edge group 公开 ``iterEdges``（H21），返回 ``hou.Edge``；
        # ``edge.vertices()`` 给两端 vertex，``vertex.point()`` 拿
        # 端点编号并排序得到 ``[min_point, max_point]``。
        iter_edges = getattr(g, "iterEdges", None)
        if callable(iter_edges):
            edges = list(iter_edges())
        else:
            edges = []
        total = len(edges)
        page = edges[start:start + size]
        members = []
        for edge in page:
            endpoints = []
            try:
                verts = list(edge.vertices())
            except Exception:
                verts = []
            for vertex in verts:
                try:
                    endpoints.append(int(vertex.point().number()))
                except Exception:
                    endpoints.append(-1)
            endpoints_sorted = sorted([e for e in endpoints if e >= 0])
            members.append(endpoints_sorted)
        next_offset = start + len(members) if (start + len(members)) < total else None
        return _success({
            "group_type": "edge",
            "group_name": group_name,
            "members": members,
            "offset": start,
            "limit": size,
            "total": total,
            "next_offset": next_offset,
        })
    return {"status": "error", "message": (
        "unknown group_type %r (use point/prim/vertex/edge)") % group_type,
            "field": "group_type"}


def _point_indices(point_group):
    """PointGroup -> list[int] of point numbers.

    H21 公开 ``iterPoints()`` / ``points``（无 ``iterIndices``）。
    """
    iter_points = getattr(point_group, "iterPoints", None)
    if callable(iter_points):
        try:
            return [int(pt.number()) for pt in iter_points()]
        except Exception:
            pass
    points_attr = getattr(point_group, "points", None)
    if callable(points_attr):
        try:
            return [int(pt.number()) for pt in points_attr()]
        except Exception:
            pass
    iter_indices = getattr(point_group, "iterIndices", None)
    if callable(iter_indices):
        try:
            return [int(x) for x in iter_indices()]
        except Exception:
            pass
    # 最后 fallback：基于 geo 全量点 + contains 判定
    geo = getattr(point_group, "geometry", None)
    count_attr = getattr(point_group, "pointCount", None)
    if geo is not None and callable(count_attr):
        try:
            out = []
            total = int(count_attr())
            for pt in geo.iterPoints():
                if point_group.contains(pt):
                    out.append(int(pt.number()))
            return out
        except Exception:
            pass
    return []


def _prim_indices(prim_group):
    """PrimGroup -> list[int] of prim numbers (H21 公开 ``iterPrims``)."""
    iter_prims = getattr(prim_group, "iterPrims", None)
    if callable(iter_prims):
        try:
            return [int(p.number()) for p in iter_prims()]
        except Exception:
            pass
    iter_indices = getattr(prim_group, "iterIndices", None)
    if callable(iter_indices):
        try:
            return [int(x) for x in iter_indices()]
        except Exception:
            pass
    return []


# ---------------------------------------------------------------------------
# Section 7: get_attrib_values — owner / storage / tuple-size 分派
# ---------------------------------------------------------------------------
_VALID_ATTRIB_CLASSES = frozenset({"point", "prim", "vertex", "detail"})


def _lookup_attrib(geo, attrib_class, name):
    """按 owner 解析 hou.Attrib；找不到返回结构化 error."""
    if not isinstance(name, str) or not name.strip():
        return {"status": "error", "message": (
            "attribute must be a non-empty string"), "field": "attribute"}
    if attrib_class not in _VALID_ATTRIB_CLASSES:
        return {"status": "error", "message": (
            "attrib_class %r invalid (use point/prim/vertex/detail)")
            % attrib_class, "field": "attrib_class"}
    if attrib_class == "point":
        attrib = geo.findPointAttrib(name)
    elif attrib_class == "prim":
        attrib = geo.findPrimAttrib(name)
    elif attrib_class == "vertex":
        attrib = geo.findVertexAttrib(name)
    else:
        # detail -> findGlobalAttrib
        attrib = geo.findGlobalAttrib(name)
    if attrib is None:
        return {"status": "error", "message": (
            "attribute %r not found on %s") % (name, attrib_class),
                "field": "attribute"}
    return {"attrib": attrib}


def get_attrib_values(hou, node_path, attribute, attrib_class="point",
                      offset=0, limit=_DEFAULT_ATTRIB_PAGE_LIMIT):
    """按 owner / storage / tuple-size 分派读取；原生分页。

    Returns ``{values, offset, limit, total, next_offset, storage,
    tuple_size}``。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    offset_check = _coerce_int("offset", offset)
    if offset_check.get("status") == "error":
        return offset_check
    limit_check = _coerce_limit("limit", limit)
    if limit_check.get("status") == "error":
        return limit_check
    if limit_check["value"] > _MAX_ATTRIB_PAGE_LIMIT:
        return {"status": "error", "message": (
            "limit exceeds %d") % _MAX_ATTRIB_PAGE_LIMIT,
                "field": "limit"}

    start = offset_check["value"]
    size = limit_check["value"]

    try:
        sop = _resolve_geometry_node(hou, node_path)
        geo = sop.geometry()
        lookup = _lookup_attrib(geo, attrib_class, attribute)
        if lookup.get("status") == "error":
            return lookup
        attrib = lookup["attrib"]
    except ValueError as err:
        return {"status": "error", "message": str(err),
                "field": "node_path"}
    except Exception as err:
        return {"status": "error", "message": (
            "attrib read failed: %s") % err,
                "exception": err.__class__.__name__}

    storage = _storage_name(attrib)
    tuple_size = int(attrib.size())

    if attrib_class == "detail":
        # 仅 1 个 detail entry
        try:
            value = geo.attribValue(attrib)
        except Exception as err:
            return {"status": "error", "message": (
                "detail attrib read failed: %s") % err,
                    "exception": err.__class__.__name__}
        values = [_jsonable(value)]
        return _success({
            "attribute": attribute,
            "attrib_class": "detail",
            "storage": storage,
            "tuple_size": tuple_size,
            "values": values,
            "offset": 0,
            "limit": 1,
            "total": 1,
            "next_offset": None,
        })

    # point / prim / vertex：按 owner 拿 iterator，按 offset/limit 切片
    try:
        if attrib_class == "point":
            entries = list(geo.iterPoints())
        elif attrib_class == "prim":
            entries = list(geo.iterPrims())
        else:
            entries = list(geo.iterVertices())
    except Exception as err:
        return {"status": "error", "message": (
            "iterator failed: %s") % err,
                "exception": err.__class__.__name__}
    total = len(entries)
    page = entries[start:start + size]
    values = []
    for entry in page:
        try:
            v = entry.attribValue(attrib)
        except Exception as err:
            return {"status": "error", "message": (
                "entry attribValue failed: %s") % err,
                    "exception": err.__class__.__name__}
        values.append(_jsonable(v))
    next_offset = start + len(values) if (start + len(values)) < total else None
    return _success({
        "attribute": attribute,
        "attrib_class": attrib_class,
        "storage": storage,
        "tuple_size": tuple_size,
        "values": values,
        "offset": start,
        "limit": size,
        "total": total,
        "next_offset": next_offset,
    })


def _storage_name(attrib):
    """attrib.dataType() 推断 storage：Float / Int / String（其它归类）。"""
    data_type = attrib.dataType()
    type_name = data_type.name() if hasattr(data_type, "name") else str(
        data_type)
    name_lower = type_name.lower()
    if "float" in name_lower:
        return "float"
    if "int" in name_lower:
        return "int"
    if "string" in name_lower:
        return "string"
    return name_lower


# ---------------------------------------------------------------------------
# Section 8: get_prim_intrinsics — 指定 primitive 查询
# ---------------------------------------------------------------------------
def get_prim_intrinsics(hou, node_path, prim_index, names=None):
    """必须传 ``prim_index``；仅读指定 primitive 的
    intrinsicNames/intrinsicValue。``names`` 可选子集过滤。

    不支持的 storage / 越界 / cook error → 结构化 error。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    prim_check = _coerce_int("prim_index", prim_index)
    if prim_check.get("status") == "error":
        return prim_check
    if names is not None and not isinstance(names, list):
        return {"status": "error", "message": (
            "names must be a list of strings or None"), "field": "names"}
    pindex = prim_check["value"]
    try:
        sop = _resolve_geometry_node(hou, node_path)
        geo = sop.geometry()
        prim_count = int(geo.intrinsicValue("primitivecount"))
        if pindex < 0 or pindex >= prim_count:
            return {"status": "error", "message": (
                "prim_index %d out of range [0, %d)")
                % (pindex, prim_count), "field": "prim_index"}
        # 通过 iterPrims 取指定 index
        target = None
        for index, prim in enumerate(geo.iterPrims()):
            if index == pindex:
                target = prim
                break
        if target is None:
            return {"status": "error", "message": (
                "prim %d not found") % pindex, "field": "prim_index"}
        intrinsic_names = list(target.intrinsicNames())
    except ValueError as err:
        return {"status": "error", "message": str(err),
                "field": "node_path"}
    except Exception as err:
        return {"status": "error", "message": (
            "prim intrinsics read failed: %s") % err,
                "exception": err.__class__.__name__}

    selected = intrinsic_names
    if names is not None:
        name_set = set(names)
        selected = [n for n in intrinsic_names if n in name_set]

    intrinsics = {}
    for name in selected:
        try:
            value = target.intrinsicValue(name)
        except Exception as err:
            intrinsics[name] = {"error": str(err)}
            continue
        intrinsics[name] = _jsonable(value)
    return _success({
        "prim_index": pindex,
        "intrinsics": intrinsics,
    })


# ---------------------------------------------------------------------------
# Section 9: find_nearest_point — Point / None 双路径
# ---------------------------------------------------------------------------
def find_nearest_point(hou, node_path, position,
                       max_distance=_DEFAULT_MAX_RADIUS):
    """``geo.nearestPoint(position, max_radius=max_distance)``；Point / None。

    ``position`` 必须是 ``[x, y, z]``；返回 Point 时给出
    ``{point_index, point_position, distance}``，None 时三字段均为 null。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    pos_check = _coerce_position(position)
    if pos_check.get("status") == "error":
        return pos_check
    if isinstance(max_distance, bool) or not isinstance(max_distance,
                                                          (int, float)):
        return {"status": "error", "message": (
            "max_distance must be a JSON number"), "field": "max_distance"}
    max_radius = float(max_distance)
    if not math.isfinite(max_radius):
        return {"status": "error", "message": (
            "max_distance must be finite"), "field": "max_distance"}
    if max_radius < 0:
        return {"status": "error", "message": (
            "max_distance must be >= 0"), "field": "max_distance"}

    pos = pos_check["value"]
    try:
        sop = _resolve_geometry_node(hou, node_path)
        geo = sop.geometry()
        point = geo.nearestPoint(pos, max_radius=max_radius)
    except ValueError as err:
        return {"status": "error", "message": str(err),
                "field": "node_path"}
    except Exception as err:
        return {"status": "error", "message": (
            "nearestPoint failed: %s") % err,
                "exception": err.__class__.__name__}

    if point is None:
        return _success({
            "point_index": None,
            "point_position": None,
            "distance": None,
        })

    try:
        point_index = int(point.number())
        pt_pos = list(point.position())
        # 转 [x, y, z]
        pt_xyz = [float(pt_pos[0]), float(pt_pos[1]), float(pt_pos[2])]
        dx = pt_xyz[0] - pos[0]
        dy = pt_xyz[1] - pos[1]
        dz = pt_xyz[2] - pos[2]
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    except Exception as err:
        return {"status": "error", "message": (
            "nearestPoint result decode failed: %s") % err,
                "exception": err.__class__.__name__}
    return _success({
        "point_index": point_index,
        "point_position": pt_xyz,
        "distance": distance,
    })


# ---------------------------------------------------------------------------
# Section 10: set_detail_attrib — Attribute Create SOP 单 undo group
# ---------------------------------------------------------------------------
def _capture_undo_group(hou, label):
    undos = getattr(hou, "undos", None)
    if undos is None:
        return None
    return undos.group(label)


def _enter_undo_group(group):
    if group is None:
        return True
    try:
        group.__enter__()
    except Exception:
        return False
    return True


def _exit_undo_group(group):
    if group is None:
        return
    try:
        group.__exit__(None, None, None)
    except Exception:
        return


def set_detail_attrib(hou, node_path, name, value,
                      attrib_type="float", node_name=None):
    """创建并配置 Attribute Create SOP，class=detail。

    全部预检 → 单 undo group 创建/连接/配置；任一 hou 异常 → destroy
    半成品并返回 error。**不**调用 ``node.geometry()`` 写方法。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    if not isinstance(name, str) or not name.strip():
        return {"status": "error", "message": (
            "name must be a non-empty string"), "field": "name"}
    if not isinstance(attrib_type, str) or attrib_type not in _VALID_DETAIL_TYPES:
        return {"status": "error", "message": (
            "attrib_type must be one of %s") % sorted(_VALID_DETAIL_TYPES),
                "field": "attrib_type"}
    if node_name is not None and not isinstance(node_name, str):
        return {"status": "error", "message": (
            "node_name must be a string or None"), "field": "node_name"}

    try:
        source = _resolve_geometry_node(hou, node_path)
        parent = source.parent()
        if parent is None:
            return {"status": "error", "message": (
                "source node has no parent network"), "field": "node_path"}
        # 创建节点前的预校验全部通过后，进入 undo group。
        group = _capture_undo_group(hou, "MCP: set_detail_attrib")
        if not _enter_undo_group(group):
            return {"status": "error", "message": (
                "failed to open undo group")}
        new_node = None
        try:
            new_node = parent.createNode("attribcreate",
                                          node_name=node_name)
            # H21 attribcreate SOP parm 命名：``class1``、``name1``、
            # ``type1``、``value1v1..v4``、``string1``、``size1``、
            # ``writevalues1``（与 H20 / H22 兼容）。
            class_parm = new_node.parm("class1")
            name_parm = new_node.parm("name1")
            type_parm = new_node.parm("type1")
            if (class_parm is None or name_parm is None
                    or type_parm is None):
                raise RuntimeError(
                    "attribcreate parms missing (class1/name1/type1)")
            # class1: 0=detail, 1=prim, 2=point, 3=vertex
            class_parm.set(0)
            name_parm.set(name)
            # type1 menu 实际数据映射（H21.0.596 实测）：
            # 0=float, 1=int, 2=vector（size>1）, 3=string,
            # 4-6=*array, 7+=dict/dictarray（与设计 _VALID_DETAIL_TYPES
            # 一一对应）。
            type_map = {"float": 0, "int": 1, "vector": 2, "string": 3}
            type_parm.set(type_map[attrib_type])
            # writevalues1 必须打开，否则 value 不写入。
            writevalues_parm = new_node.parm("writevalues1")
            if writevalues_parm is not None:
                writevalues_parm.set(1)
            if attrib_type == "vector":
                if not isinstance(value, (list, tuple)) or len(value) != 3:
                    raise ValueError(
                        "vector value must be a 3-element list/tuple")
                vec_value = [float(value[0]), float(value[1]),
                             float(value[2])]
                size_parm = new_node.parm("size1")
                if size_parm is not None:
                    size_parm.set(3)
                for i in range(4):
                    p = new_node.parm("value1v{0}".format(i + 1))
                    if p is None:
                        continue
                    p.set(vec_value[i] if i < 3 else 0.0)
            elif attrib_type == "string":
                if isinstance(value, bool) or not isinstance(value, str):
                    raise ValueError(
                        "string value must be a JSON string; bool/int/float not accepted")
                vec_value = value
                string_parm = new_node.parm("string1")
                if string_parm is None:
                    raise RuntimeError("string1 parm not found on attribcreate")
                string_parm.set(vec_value)
            else:
                if isinstance(value, bool):
                    raise ValueError(
                        "value must be a JSON number; bool not accepted")
                if not isinstance(value, (int, float)):
                    raise ValueError(
                        "value must be a JSON number (int or float)")
                vec_value = float(value)
                value_parm = new_node.parm("value1v1")
                if value_parm is None:
                    raise RuntimeError("value1v1 parm not found on attribcreate")
                value_parm.set(vec_value)
            # 连接输入：第一个输入槽接 source
            new_node.setFirstInput(source)
        except Exception as err:
            # 清理半成品节点
            if new_node is not None:
                try:
                    new_node.destroy()
                except Exception:
                    pass
            _exit_undo_group(group)
            return {"status": "error", "message": (
                "set_detail_attrib failed: %s") % err,
                    "exception": err.__class__.__name__}
        # 成功路径下保留 undo group 由 Houdini 自然关闭
        _exit_undo_group(group)
        try:
            new_path = new_node.path()
        except Exception:
            new_path = ""
        return _success({
            "node_path": new_path,
            "source_path": source.path(),
            "attribute": {"name": name, "type": attrib_type,
                          "value": vec_value},
        })
    except ValueError as err:
        return {"status": "error", "message": str(err),
                "field": "node_path"}


# ---------------------------------------------------------------------------
# Section 11: geo_export — translator registry + 原子覆盖
# ---------------------------------------------------------------------------
def _check_translator(format_token):
    if format_token not in TRANSLATOR_REGISTRY:
        return _error(
            "unsupported_translator",
            "format %r not in translator registry; supported: %s"
            % (format_token, sorted(TRANSLATOR_REGISTRY.keys())),
            details={"format": format_token,
                      "available": sorted(TRANSLATOR_REGISTRY.keys())})
    return None


def _check_extension_match(output_path, format_token):
    expected = TRANSLATOR_REGISTRY[format_token]["extension"]
    # 不区分大小写匹配（Windows 路径）
    if not output_path.lower().endswith(expected.lower()):
        return _error(
            "extension_mismatch",
            "output_path must end with %s for format %s"
            % (expected, format_token),
            details={"format": format_token, "expected_extension": expected,
                      "output_path": output_path})
    return None


def geo_export(hou, node_path, format, output_path, overwrite=False):
    """基于 ``hou.Geometry.saveToFile`` 的原子导出。

    流程：校验 translator → 校验扩展名 → 解析节点 → 临时文件
    → ``saveToFile`` → flush/fsync → ``os.replace``（overwrite=False
    且目标存在则 ``target_exists`` 错误）。失败清理临时文件。

    H21.0.596 实测：``Geometry.saveToFile(file_name)`` 仅接受位置参数；
    文件格式由扩展名决定（``.bgeo`` / ``.geo`` / ``.bgeo.gz`` /
    ``.bgeo.lzma`` / ``.bgeo.bz2``）。因此我们先把临时文件写入扩展
    名版本，fsync 后再 ``os.replace`` 为最终路径。
    """
    if not isinstance(node_path, str) or not node_path.strip():
        return {"status": "error", "message": (
            "node_path must be a non-empty string"), "field": "node_path"}
    if not isinstance(format, str):
        return {"status": "error", "message": (
            "format must be a string"), "field": "format"}
    if not isinstance(output_path, str) or not output_path.strip():
        return {"status": "error", "message": (
            "output_path must be a non-empty string"), "field": "output_path"}
    if isinstance(overwrite, bool) is False and not isinstance(overwrite, bool):
        return {"status": "error", "message": (
            "overwrite must be a boolean"), "field": "overwrite"}
    # normalize overwrite to bool
    overwrite_flag = bool(overwrite)

    tr_err = _check_translator(format)
    if tr_err is not None:
        return tr_err
    ext_err = _check_extension_match(output_path, format)
    if ext_err is not None:
        return ext_err

    target_path = os.path.abspath(output_path)
    target_dir = os.path.dirname(target_path)
    if not target_dir:
        target_dir = os.getcwd()

    target_exists = os.path.exists(target_path)
    if target_exists and not overwrite_flag:
        return _error(
            "target_exists",
            "output_path already exists and overwrite=False",
            details={"output_path": target_path,
                      "format": format})

    # 解析节点
    try:
        sop = _resolve_geometry_node(hou, node_path)
    except ValueError as err:
        return {"status": "error", "message": str(err),
                "field": "node_path"}
    except Exception as err:
        return {"status": "error", "message": (
            "node resolve failed: %s") % err,
                "exception": err.__class__.__name__}

    # 准备临时文件：suffix 携带 format extension，让 saveToFile 按扩
    # 展名 dispatch；与目标不同名 → saveToFile 不会误命中现存文件。
    expected_extension = TRANSLATOR_REGISTRY[format]["extension"]
    temp_fd = None
    temp_path = None
    try:
        temp_fd, temp_path = tempfile.mkstemp(
            prefix="geo_export_", suffix=expected_extension, dir=target_dir)
        os.close(temp_fd)
        temp_fd = None
    except Exception as err:
        return {"status": "error", "message": (
            "failed to create temp file in %s: %s") % (target_dir, err),
                "exception": err.__class__.__name__}

    try:
        geo = sop.geometry()
        try:
            geo.saveToFile(temp_path, file_type=TRANSLATOR_REGISTRY[format]["save_type"])
        except TypeError:
            # H21+ 实测：saveToFile 仅接受位置参数（格式由扩展名决定）
            geo.saveToFile(temp_path)
    except Exception as err:
        _safe_remove(temp_path)
        return {"status": "error", "message": (
            "saveToFile failed: %s") % err,
                "exception": err.__class__.__name__,
                "translator": format,
                "error_details": str(err)}

    # fsync + os.replace
    try:
        with open(temp_path, "rb") as fh:
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                # 某些 Windows FS 可能不支持 fsync；best-effort
                pass
    except Exception as err:
        _safe_remove(temp_path)
        return {"status": "error", "message": (
            "fsync failed: %s") % err,
                "exception": err.__class__.__name__}

    try:
        os.replace(temp_path, target_path)
    except Exception as err:
        _safe_remove(temp_path)
        return {"status": "error", "message": (
            "atomic replace failed: %s") % err,
                "exception": err.__class__.__name__}

    try:
        size_bytes = os.path.getsize(target_path)
    except Exception:
        size_bytes = 0
    return _success({
        "translator": format,
        "output_path": target_path,
        "size_bytes": int(size_bytes),
        "atomic_replace": True,
    })


def _safe_remove(path):
    if not path:
        return
    try:
        os.remove(path)
    except Exception:
        pass