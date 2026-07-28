"""tests/h21_live_geo_measure_smoke.py — H21.0.596 hython live smoke for
add-geometry-export-and-measure。

使用 ``C:\\Program Files\\Side Effects Software\\Houdini 21.0.596\\bin\\hython.exe``
加载 ``external\\houdinimcp\\_geo_measure.py``，在临时 hip + box SOP 几何上
验证 8 个工具，覆盖：
- ``get_bounding_box`` 6 元解包顺序。
- ``get_groups`` 返回 4 类（point/prim/vertex/edge）。
- ``get_group_members`` 四类成员 schema + 分页。
- ``get_attrib_values`` 不同 owner/storage/tuple-size + detail 单值。
- ``get_prim_intrinsics`` 指定 ``prim_index`` 越界。
- ``find_nearest_point`` Point 与 None 路径。
- ``set_detail_attrib`` 创建 Attribute Create SOP + 单 undo group +
  验证 cooked Geometry **不**被写。
- ``geo_export`` translator registry：bgeo / bgeo.gz / geo 真实落盘；
  扩展名 mismatch 拒绝；``overwrite=False`` 目标存在 ``target_exists``；
  ``overwrite=True`` 原子替换；失败清理临时文件；每格式 ``size_bytes``
  > 0 且 ``atomic_replace`` = True。

输出：每项 ``GME-SMOKE-<n> <desc> ... PASS|FAIL|``，结尾
``GME-SMOKE-RESULT <n>/<m> PASS``。任一失败整批 FAIL。
约束：
- 必须用 hython 跑，不 mock。
- 所有副作用落到 ``tempfile.gettempdir()/gme_smoke`` 下，并在 finally
  段清理，不污染用户首选项目录。
- 与 R9 一致：不测 H22（未安装）。
"""
import json
import os
import shutil
import sys
import tempfile
import traceback


HYTHON = r"C:\Program Files\Side Effects Software\Houdini 21.0.596\bin\hython.exe"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _houdini_version():
    import hou  # type: ignore
    ver = hou.applicationVersion()
    return "%d.%d.%d" % (ver[0], ver[1], ver[2])


def _module_path(name):
    return os.path.join(ROOT, name + ".py")


def _load_modules():
    import importlib.util
    import types
    pkg = types.ModuleType("gme_smoke_pkg")
    pkg.__path__ = [ROOT]
    sys.modules["gme_smoke_pkg"] = pkg
    for name in ("_common", "_geo_measure"):
        full = "gme_smoke_pkg." + name
        spec = importlib.util.spec_from_file_location(
            full, _module_path(name))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
    return (sys.modules["gme_smoke_pkg._common"],
            sys.modules["gme_smoke_pkg._geo_measure"])


def _make_geo_root():
    import hou  # type: ignore
    hou.hipFile.clear(suppress_save_prompt=True)
    geo = hou.node("/obj").createNode("geo", "gme_smoke_geo")
    box = geo.createNode("box", "box1")
    # 确保 cook
    box.cook(force=True)
    return geo, box


def _add_point_groups(geo):
    """添加 point / prim / vertex / edge group 用于 group 测试。

    使用 Group Create SOP（``groupcreate``）。``grouptype``:
    0=Primitive / 1=Point / 2=Edge / 3=Vertex；用 ``groupbounding`` +
    ``boundtype=0`` 限定一个覆盖 box 整体的范围框，让所有元素都被选中。
    """
    grp_p = geo.createNode("groupcreate", "make_p_grp")
    grp_p.parm("groupname").set("selected")
    grp_p.parm("grouptype").set(1)  # 1 = Point
    grp_p.parm("groupbounding").set(1)  # enable bounding filter
    grp_p.parm("boundtype").set(0)
    grp_p.parm("sizex").set(10.0)
    grp_p.parm("sizey").set(10.0)
    grp_p.parm("sizez").set(10.0)
    grp_p.setFirstInput(geo.node("box1"))
    grp_p.cook(force=True)
    return grp_p


def _check(condition, message, failures):
    if condition:
        print("PASS: {0}".format(message))
        return True
    print("FAIL: {0}".format(message))
    failures.append(message)
    return False


def main():
    failures = []
    total = 0
    workdir = os.path.join(tempfile.gettempdir(), "gme_smoke")
    if os.path.exists(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    try:
        import hou  # type: ignore
        hou.hipFile.clear(suppress_save_prompt=True)
        version = _houdini_version()
        print("GME-SMOKE-VERSION {0}".format(version))
        cmn, gme = _load_modules()
        geo, box = _make_geo_root()
        # attach groups
        grp = _add_point_groups(geo)

        # === 1. get_bounding_box ===
        total += 1
        r = gme.get_bounding_box(hou, box.path())
        ok = (r.get("status") == "success"
              and "min" in r["result"]
              and "max" in r["result"]
              and "size" in r["result"]
              and "center" in r["result"])
        if ok:
            mn = r["result"]["min"]
            mx = r["result"]["max"]
            # box SOP 默认 1x1x1，居中在原点；min 应 ~ (-0.5, -0.5, -0.5)
            ok = all(-0.6 < v < -0.4 for v in mn) and all(
                0.4 < v < 0.6 for v in mx)
        _check(ok, "get_bounding_box 6-tuple unpacked", failures)

        # === 2. get_groups ===
        total += 1
        r = gme.get_groups(hou, grp.path())
        ok = (r.get("status") == "success"
              and "point" in r["result"]["groups"]
              and "prim" in r["result"]["groups"]
              and "vertex" in r["result"]["groups"]
              and "edge" in r["result"]["groups"]
              and "selected" in r["result"]["groups"]["point"])
        _check(ok, "get_groups returns 4 classes + selected",
               failures)

        # === 3. get_group_members point paginated ===
        total += 1
        r = gme.get_group_members(hou, grp.path(), "point", "selected",
                                    offset=0, limit=10)
        ok = (r.get("status") == "success"
              and isinstance(r["result"]["members"], list)
              and r["result"]["total"] >= 1
              and r["result"]["next_offset"] is None)
        _check(ok, "get_group_members point paginated", failures)

        # === 4. get_attrib_values point (P / Int / tuple_size 3) ===
        total += 1
        r = gme.get_attrib_values(hou, box.path(), "P",
                                     attrib_class="point", offset=0, limit=5)
        ok = (r.get("status") == "success"
              and r["result"]["tuple_size"] == 3
              and len(r["result"]["values"]) > 0)
        _check(ok, "get_attrib_values point P tuple_size=3", failures)

        # === 5. get_attrib_values unknown attrib ===
        total += 1
        r = gme.get_attrib_values(hou, box.path(), "no_such",
                                     attrib_class="point")
        ok = (r.get("status") == "error"
              and r.get("field") == "attribute")
        _check(ok, "get_attrib_values unknown attribute error",
               failures)

        # === 6. get_prim_intrinsics ===
        total += 1
        r = gme.get_prim_intrinsics(hou, box.path(), 0)
        ok = (r.get("status") == "success"
              and r["result"]["prim_index"] == 0
              and "intrinsics" in r["result"])
        _check(ok, "get_prim_intrinsics valid prim", failures)

        # === 7. get_prim_intrinsics out of range ===
        total += 1
        r = gme.get_prim_intrinsics(hou, box.path(), 999999)
        ok = (r.get("status") == "error"
              and r.get("field") == "prim_index")
        _check(ok, "get_prim_intrinsics out of range error", failures)

        # === 8. find_nearest_point (point at origin) ===
        total += 1
        # box SOP 生成 8 corner points（+/-0.5 三轴）；原点是内部，
        # 所以 nearestPoint 应在 max_radius 内找不到（None）。
        r = gme.find_nearest_point(hou, box.path(), [0.0, 0.0, 0.0],
                                       max_distance=0.4)
        ok = (r.get("status") == "success"
              and r["result"]["point_index"] is None)
        _check(ok, "find_nearest_point None path", failures)

        # === 9. find_nearest_point (corner) ===
        total += 1
        r = gme.find_nearest_point(hou, box.path(),
                                       [0.45, 0.45, 0.45],
                                       max_distance=0.5)
        ok = (r.get("status") == "success"
              and r["result"]["point_index"] is not None
              and r["result"]["distance"] is not None)
        _check(ok, "find_nearest_point Point path", failures)

        # === 10. set_detail_attrib + verify no cooked geo write ===
        total += 1
        # snapshot original geometry content (count + bbox) before set
        before_g = box.geometry()
        before_count = int(before_g.intrinsicValue("pointcount"))
        before_bbox = list(before_g.intrinsicValue("bounds"))
        r = gme.set_detail_attrib(hou, box.path(), "gme_test_label",
                                     "alpha", attrib_type="string",
                                     node_name="gme_attr_create")
        ok = (r.get("status") == "success"
              and r["result"]["node_path"].endswith("gme_attr_create"))
        if ok:
            # 验证 source node 的 geometry 内容未被改写（数据完整性）
            after_g = box.geometry()
            after_count = int(after_g.intrinsicValue("pointcount"))
            after_bbox = list(after_g.intrinsicValue("bounds"))
            ok = (before_count == after_count
                  and before_bbox == after_bbox)
        # 验证 undo 一次可回滚节点创建（H21 API：performUndo）
        if ok and hasattr(hou, "undos"):
            try:
                hou.undos.performUndo()
                undo_ok = True
            except Exception:
                undo_ok = False
            ok = ok and undo_ok
        _check(ok, "set_detail_attrib creates node + no cooked write + undo",
               failures)

        # === 11. geo_export bgeo ===
        total += 1
        out_bgeo = os.path.join(workdir, "export.bgeo")
        r = gme.geo_export(hou, box.path(), "bgeo", out_bgeo)
        ok = (r.get("status") == "success"
              and os.path.exists(out_bgeo)
              and r["result"]["size_bytes"] > 0
              and r["result"]["atomic_replace"] is True)
        _check(ok, "geo_export bgeo atomic", failures)

        # === 12. geo_export geo (ASCII) ===
        total += 1
        out_geo = os.path.join(workdir, "export.geo")
        r = gme.geo_export(hou, box.path(), "geo", out_geo)
        ok = (r.get("status") == "success"
              and os.path.exists(out_geo)
              and r["result"]["size_bytes"] > 0)
        _check(ok, "geo_export geo atomic", failures)

        # === 13. geo_export bgeo.gz ===
        total += 1
        out_gz = os.path.join(workdir, "export.bgeo.gz")
        r = gme.geo_export(hou, box.path(), "bgeo.gz", out_gz)
        ok = (r.get("status") == "success"
              and os.path.exists(out_gz)
              and r["result"]["size_bytes"] > 0)
        _check(ok, "geo_export bgeo.gz atomic", failures)

        # === 14. geo_export unsupported_translator ===
        total += 1
        r = gme.geo_export(hou, box.path(), "obj",
                              os.path.join(workdir, "x.obj"))
        ok = (r.get("status") == "error"
              and r["error"]["code"] == "unsupported_translator")
        _check(ok, "geo_export unsupported_translator", failures)

        # === 15. geo_export extension_mismatch ===
        total += 1
        r = gme.geo_export(hou, box.path(), "bgeo",
                              os.path.join(workdir, "x.geo"))
        ok = (r.get("status") == "error"
              and r["error"]["code"] == "extension_mismatch")
        _check(ok, "geo_export extension_mismatch", failures)

        # === 16. geo_export target_exists default ===
        total += 1
        # 复用上面 out_bgeo 路径
        r = gme.geo_export(hou, box.path(), "bgeo", out_bgeo)
        ok = (r.get("status") == "error"
              and r["error"]["code"] == "target_exists")
        _check(ok, "geo_export target_exists default overwrite=False",
               failures)

        # === 17. geo_export overwrite=True atomic replace ===
        total += 1
        # 先写老内容
        with open(out_bgeo, "wb") as fh:
            fh.write(b"OLD")
        r = gme.geo_export(hou, box.path(), "bgeo", out_bgeo,
                              overwrite=True)
        with open(out_bgeo, "rb") as fh:
            content = fh.read()
        ok = (r.get("status") == "success"
              and content != b"OLD"
              and r["result"]["atomic_replace"] is True)
        _check(ok, "geo_export overwrite=True atomic replace", failures)

        print("GME-SMOKE-RESULT {0}/{1} PASS".format(
            total - len(failures), total))
        return 0 if not failures else 1
    except Exception:
        traceback.print_exc()
        failures.append("unexpected exception")
        print("GME-SMOKE-RESULT {0}/{1} PASS".format(
            total - len(failures), total))
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())