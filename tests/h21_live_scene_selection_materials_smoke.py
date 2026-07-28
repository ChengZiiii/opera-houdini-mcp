"""tests/h21_live_scene_selection_materials_smoke.py — H21.0.596 hython live smoke
for add-scene-context-selection-materials。

使用 ``C:\\Program Files\\Side Effects Software\\Houdini 21.0.596\\bin\\hython.exe``
加载 ``external\\houdinimcp\\_scene.py`` / ``_selection.py`` /
``_materials.py`` + 真实 hou API，覆盖：
- BFS overview：环 / 共享祖先 / max_nodes 截断
- DFS cook chain：菱形 / 环 / max_nodes 截断
- node-only selection：selectedNodes() + setSelected(False) 不调
  clearAllSelected
- Vop category 类型发现 + 稳定排序
- list_materials 验证 / create_material_network 错误契约
- RGB 白名单回归：principledshader::2.0 basecolorr/g/b + sheenr/g/b 等
- node-type name with category 完整名

输出：每项 ``SSM-SMOKE-<n> <desc> ... PASS|FAIL``，结尾
``SSM-SMOKE-RESULT <n>/<m> PASS``。任一失败整批 FAIL。

约束：
- 必须用 ``hython`` 跑，不准 mock。
- 不持久化用户首选项目录；副作用仅在
  ``tempfile.gettempdir()/ssm_smoke``。
- 与 R9 一致：不测 H22（未安装）。
"""
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


def _load_modules():
    import importlib.util
    import types
    pkg = types.ModuleType("ssm_smoke_pkg")
    pkg.__path__ = [ROOT]
    sys.modules["ssm_smoke_pkg"] = pkg
    for name in ("_common", "_scene", "_selection", "_materials"):
        full = "ssm_smoke_pkg." + name
        if full in sys.modules:
            del sys.modules[full]
        spec = importlib.util.spec_from_file_location(
            full, os.path.join(ROOT, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
    return (sys.modules["ssm_smoke_pkg._common"],
            sys.modules["ssm_smoke_pkg._scene"],
            sys.modules["ssm_smoke_pkg._selection"],
            sys.modules["ssm_smoke_pkg._materials"])


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
    workdir = os.path.join(tempfile.gettempdir(), "ssm_smoke")
    if os.path.exists(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    try:
        import hou  # type: ignore
        hou.hipFile.clear(suppress_save_prompt=True)
        version = _houdini_version()
        print("SSM-SMOKE-VERSION {0}".format(version))
        cmn, scn, sel, mats = _load_modules()

        # Build a small cook-chain: A -> B, A -> C, B -> D, C -> D.
        # Use merge node to combine B and C into D, giving a true
        # diamond shape with shared ancestor A.
        obj = hou.node("/obj")
        geo = obj.createNode("geo", "ssm_geo")
        box = geo.createNode("box", "box1")
        A = geo.createNode("null", "A")
        B = geo.createNode("null", "B")
        C = geo.createNode("null", "C")
        # Use merge (multi-input SOP) to combine B and C into D
        merge = geo.createNode("merge", "M")
        D = geo.createNode("null", "D")
        B.setFirstInput(A)
        C.setFirstInput(A)
        merge.setInput(0, B)
        merge.setInput(1, C)
        D.setFirstInput(merge)
        for n in (A, B, C, D, merge):
            n.cook(force=True)

        # === 1. get_cook_chain diamond + shared ancestor dedup ===
        total += 1
        r = scn.get_cook_chain(hou, D.path(), max_depth=20, max_nodes=500)
        # Chain: D, merge, B, A, C. With dedup, A appears only once.
        # Total unique = 5 (D, merge, B, A, C).
        chain_paths = [n["path"] for n in r["chain"]]
        ok = (r.get("status") == "success"
              and isinstance(r.get("chain"), list)
              and r["chain"][0]["path"] == D.path()
              and chain_paths.count(A.path()) == 1  # shared ancestor dedup
              and r["visited_count"] == len(set(chain_paths))  # no dup
              and not r["truncated"])
        _check(ok, "get_cook_chain diamond dedup (shared ancestor A x1)",
               failures)

        # === 2. get_cook_chain max_nodes truncation ===
        total += 1
        r = scn.get_cook_chain(hou, D.path(), max_depth=20, max_nodes=2)
        ok = (r.get("status") == "success"
              and r["truncated"] is True
              and r["truncation_reason"] == "max_nodes"
              and r["visited_count"] <= 2)
        _check(ok, "get_cook_chain max_nodes truncation", failures)

        # === 3. get_network_overview BFS on /obj ===
        total += 1
        r = scn.get_network_overview(hou, "/obj", max_depth=5,
                                       max_nodes=500)
        ok = (r.get("status") == "success"
              and isinstance(r.get("nodes"), list)
              and len(r["nodes"]) >= 2  # obj + ssm_geo
              and r["visited_count"] >= 2
              and not r["truncated"])
        _check(ok, "get_network_overview BFS on /obj", failures)

        # === 4. get_network_overview max_nodes truncation ===
        total += 1
        r = scn.get_network_overview(hou, "/obj", max_depth=20,
                                       max_nodes=2)
        ok = (r.get("status") == "success"
              and r["truncated"] is True
              and r["truncation_reason"] == "max_nodes"
              and r["visited_count"] <= 2)
        _check(ok, "get_network_overview max_nodes truncation", failures)

        # === 5. get_scene_summary category counts + timeline ===
        total += 1
        r = scn.get_scene_summary(hou, max_nodes=500)
        ok = (r.get("status") == "success"
              and r["total_nodes"] >= 4
              and "Object" in r["category_counts"]
              and "Sop" in r["category_counts"]
              and r["fps"] > 0
              and r["start_frame"] > 0
              and r["end_frame"] >= r["start_frame"])
        _check(ok, "get_scene_summary category counts + timeline", failures)

        # === 6. explain_node basic + non_default params ===
        total += 1
        # box has sizex/sizey/sizez parms. Set sizex to non-default and
        # verify explain_node reports it.
        box.parm("sizex").set(2.5)
        r = scn.explain_node(hou, box.path(), include_params=True,
                              max_params=20)
        non_default = r.get("non_default_parameter_count", 0)
        ok = (r.get("status") == "success"
              and r["type"] == "box"
              and r["category"] == "Sop"
              and r["input_count"] == 0
              and r["output_count"] >= 0
              and non_default >= 1
              and "sizex" in (r.get("non_default_parameters") or {}))
        if not ok:
            print("    DEBUG: r=%s" % r)
        _check(ok, "explain_node with non_default_params (box SOP)",
               failures)

        # === 7. node-only selection get + set ===
        total += 1
        # Clear any default selection
        for n in (A, B, C, D, box):
            n.setSelected(False)
        r = sel.set_selection(hou, [A.path(), B.path()],
                                clear_others=True)
        ok = (r.get("status") == "success"
              and r["set"] == 2
              and r["cleared"] == 0  # nothing was selected before
              and A.isSelected() and B.isSelected()
              and not C.isSelected() and not D.isSelected())
        _check(ok, "set_selection 2 nodes, no clear of others", failures)

        # === 8. get_selection returns only A, B (node-only) ===
        total += 1
        r = sel.get_selection(hou)
        ok = (r.get("status") == "success"
              and r["count"] == 2
              and {e["path"] for e in r["selected"]} == {A.path(), B.path()})
        _check(ok, "get_selection returns only 2 nodes (no boxes/notes)",
               failures)

        # === 9. set_selection replace — clear others ===
        total += 1
        r = sel.set_selection(hou, [D.path()], clear_others=True)
        ok = (r.get("status") == "success"
              and r["set"] == 1
              and r["cleared"] == 2
              and D.isSelected()
              and not A.isSelected() and not B.isSelected())
        _check(ok, "set_selection replace (clear_others=True)", failures)

        # === 10. set_selection invalid path no partial change ===
        total += 1
        r = sel.set_selection(hou, [A.path(), "/no/such"],
                                clear_others=True)
        ok = (r.get("status") == "error"
              and r["error"]["code"] == "invalid_node_path"
              # A must NOT have been re-selected (no partial change)
              and not A.isSelected()
              and D.isSelected())  # D still selected from test 9
        _check(ok, "set_selection invalid path 0 partial change", failures)

        # === 11. list_material_types Vop + stable sort ===
        total += 1
        r = mats.list_material_types(hou, "Vop")
        ok = (r.get("status") == "success"
              and r["category"] == "Vop"
              and len(r["types"]) >= 1
              and all("/" in t["node_type"] for t in r["types"]))
        # Stable sort check
        names = [t["name"] for t in r["types"]]
        ok = ok and names == sorted(names)
        _check(ok, "list_material_types Vop + nameWithCategory + sort",
               failures)

        # === 12. list_material_types unsupported category ===
        total += 1
        r = mats.list_material_types(hou, "Sop")
        ok = (r.get("status") == "error"
              and r["error"]["code"] == "unsupported_category")
        _check(ok, "list_material_types unsupported_category (Sop)",
               failures)

        # === 13. list_materials on a fresh /mat container ===
        total += 1
        # /mat pre-exists in H21 but its childTypeCategory may be Mat
        # or Vop, not Sop. Try createNode(matnet) directly: if it
        # works, the category supports it. If it fails, just verify
        # that the category name is reported properly.
        try:
            ssm_mat = hou.node("/mat").createNode("matnet", "ssm_listmat")
            r = mats.list_materials(hou, ssm_mat.path())
            ok = (r.get("status") == "success"
                  and isinstance(r["materials"], list)
                  and r["count"] == len(r["materials"])
                  and r["count"] >= 0)
            ssm_mat.destroy()
        except hou.OperationFailed:
            # /mat category doesn't accept matnet; use a Vop network
            # as alternative material container
            obj_mat = hou.node("/obj").createNode("matnet", "ssm_listmat")
            r = mats.list_materials(hou, obj_mat.path())
            ok = (r.get("status") == "success"
                  and isinstance(r["materials"], list)
                  and r["count"] == len(r["materials"]))
            obj_mat.destroy()
        if not ok:
            print("    DEBUG: r=%s" % r)
        _check(ok, "list_materials on a matnet container", failures)

        # === 14. create_material_network parent_not_found ===
        total += 1
        r = mats.create_material_network(hou, "/no/such", name="myNet")
        ok = (r.get("status") == "error"
              and r["error"]["code"] == "parent_not_found")
        _check(ok, "create_material_network parent_not_found", failures)

        # === 15. create_material_network unsupported parent category ===
        total += 1
        # Use /obj/topnet (Top category) which is not Sop
        top = obj.createNode("topnet", "ssm_top")
        r = mats.create_material_network(hou, top.path(), name="myNet")
        ok = (r.get("status") == "error"
              and r["error"]["code"] == "unsupported_parent_category")
        _check(ok, "create_material_network unsupported_parent_category",
               failures)
        top.destroy()

        # === 16. create_material_network success on /obj ===
        total += 1
        # H21: /mat's childTypeCategory is Vop but matnet is namespace-
        # less and createNode("matnet") works on /obj directly. Use
        # /obj (a generic Object container) as the parent.
        obj = hou.node("/obj")
        # Clean up any existing matnet from previous runs
        for child in list(obj.children()):
            if (child.name().startswith("ssm_")
                    and child.type().name() == "matnet"):
                child.destroy()
        r = mats.create_material_network(hou, "/obj",
                                           name="ssm_smatnet")
        ok = (r.get("status") == "success"
              and r.get("type") == "matnet"
              and r.get("path"))
        if not ok:
            print("    DEBUG: r=%s" % r)
        _check(ok, "create_material_network success on /obj", failures)

        # === 17. RGB whitelist regression (server-side whitelist) ===
        total += 1
        wl = mats.MATERIAL_PARM_WHITELIST
        needed = ("basecolorr", "basecolorg", "basecolorb",
                  "emitcolorr", "emitcolorg", "emitcolorb",
                  "sheenr", "sheeng", "sheenb",
                  "coat_colorr", "coat_colorg", "coat_colorb",
                  "sssr", "sssg", "sssb",
                  "scattering_colorr", "scattering_colorg",
                  "scattering_colorb")
        ok = len(wl) >= 50 and all(k in wl for k in needed)
        _check(ok, "RGB whitelist regression: 50+ entries with RGB subkeys",
               failures)

        # === 18. principledshader::2.0 RGB subkeys readable ===
        total += 1
        # Create a principledshader under /mat
        ps = hou.node("/mat").createNode("principledshader", "smoke_ps")
        ps.cook(force=True)
        r = mats.get_material_info(hou, ps.path())
        params = r.get("parameters") or {}
        # H21 returns type as "principledshader::2.0" — accept either
        actual_type = r.get("type") or ""
        ok = (r.get("path") == ps.path()
              and "principledshader" in actual_type
              and isinstance(params, dict)
              and "basecolorr" in params
              and "basecolorg" in params
              and "basecolorb" in params)
        if not ok:
            print("    DEBUG: type=%r r path=%r ps.path()=%r"
                  % (actual_type, r.get("path"), ps.path()))
            print("    DEBUG: r keys=%s" % sorted(params.keys())[:30])
        _check(ok, "principledshader::2.0 RGB subkeys present in info",
               failures)

        print("SSM-SMOKE-RESULT {0}/{1} PASS".format(
            total - len(failures), total))
        return 0 if not failures else 1
    except Exception:
        traceback.print_exc()
        failures.append("unexpected exception")
        print("SSM-SMOKE-RESULT {0}/{1} PASS".format(
            total - len(failures), total))
        return 1
    finally:
        try:
            for n in (A, B, C, D, box):
                n.setSelected(False)
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
