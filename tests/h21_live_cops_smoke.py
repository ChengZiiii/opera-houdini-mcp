#!/usr/bin/env python
"""H21.0 真实 Copernicus (COP) network smoke for add-cops-tools。

使用 Houdini 21.0.596 hython + 真实 ``copnet`` / ``checkerboard`` 网络，覆盖：
- get_cop_info：input/output data types + outputCableStructure；
- get_cop_geometry：len(points())/len(prims()) counts + bbox；
- get_cop_layer：真实 ImageLayer metadata（bufferResolution/displayWindow/
  channelCount/storageType）；
- get_cop_vdb：真实 ``vdb()`` 入口；image 源无 NanoVDB wire 时返回
  结构化 ``vdb_unavailable`` warning（NanoVDB metadata 提取由 mock 单测覆盖，
  完整 VDB cable chain 超出最小 smoke 范围）；
- create_cop_node / set_cop_flags：MUTATING 写；
- list_cop_node_types：Cop registry 枚举；
- cable wire surface 固化（H21 实测 CopNode 无 cable()，结构走
  outputCableStructure）；
- response cap 与 MUTATING/NO_UNDO/READ_ONLY 唯一分类；
- 读取不产生 undo 条目；create/flags 产生 undo 条目（证明 MUTATING）。

本脚本只在临时未保存场景中工作；结束时清空场景，不落盘。
"""
import builtins
import importlib.util
import json
import os
import sys
import traceback
import types


HYTHON = r"C:\Program Files\Side Effects Software\Houdini 21.0.596\bin\hython.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_modules():
    pkg_name = "cops_live_pkg"
    for name in (pkg_name + "._cops", pkg_name + "._common", pkg_name):
        sys.modules.pop(name, None)
    package = types.ModuleType(pkg_name)
    package.__path__ = [ROOT]
    sys.modules[pkg_name] = package
    loaded = []
    for module_name in ("_common", "_cops"):
        full_name = pkg_name + "." + module_name
        spec = importlib.util.spec_from_file_location(
            full_name, os.path.join(ROOT, module_name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded[0], loaded[1]


def _check(condition, label, failures):
    if condition:
        print("PASS: " + label)
        return True
    print("FAIL: " + label)
    failures.append(label)
    return False


def _response_size(value):
    return len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))


COPS_7 = {
    "get_cop_info", "get_cop_geometry", "get_cop_layer", "get_cop_vdb",
    "create_cop_node", "set_cop_flags", "list_cop_node_types",
}
COPS_MUT = {"create_cop_node", "set_cop_flags"}
COPS_NO_UNDO = {"get_cop_info", "get_cop_geometry", "get_cop_layer",
                "get_cop_vdb"}
COPS_RO = {"list_cop_node_types"}


def main():
    failures = []
    total = 0
    try:
        import hou  # type: ignore
        version = tuple(hou.applicationVersion())
        print("COPS-SMOKE-VERSION {0}.{1}.{2}".format(*version[:3]))
        cmn, cops = _load_modules()
        hou.hipFile.clear(suppress_save_prompt=True)

        total += 1
        _check(version[:2] == (21, 0), "H21.0 target version", failures)

        # 真实 Copernicus 网络：/img -> copnet -> checkerboard (image 源)。
        # Copernicus NanoVDB 需完整 cable chain（image->vdbfromlayer 等），
        # 最小 smoke 用 image 源覆盖 get_cop_vdb 的真实 vdb() 入口与结构化
        # 不可得 fallback；NanoVDB metadata 提取由 mock 单测覆盖。
        img = hou.node("/img")
        copnet = img.createNode("copnet", "mcp_cops_smoke")
        checker = copnet.createNode("checkerboard", "smoke_checker")
        checker.setDisplayFlag(True)
        copnet.layoutChildren()
        checker.cook(force=True)
        total += 1
        _check(not checker.errors(),
               "real Copernicus network initial force cook", failures)

        # cable wire surface 固化：H21 实测 CopNode 无 cable() 方法。
        total += 1
        _check(not hasattr(checker, "cable"),
               "H21 CopNode has no cable() method (cemented)", failures)
        print("COPS-CABLE-METHOD-PRESENT cable={0}".format(
            hasattr(checker, "cable")))

        # outputCableStructure 真实返回（带 output_index）。
        try:
            ocs = checker.outputCableStructure(0)
            print("COPS-CABLE-STRUCTURE {0}".format(type(ocs).__name__))
        except Exception as error:
            print("COPS-CABLE-STRUCTURE-ERR {0}".format(error))

        # --- get_cop_info ---
        info = cops.get_cop_info(hou, checker.path())
        total += 1
        _check(info.get("status") == "success"
               and info.get("node_type") == "checkerboard"
               and bool(info.get("output_data_types")),
               "get_cop_info real types + structure", failures)
        total += 1
        _check(info.get("cable_structure", {}).get("available") is True
               and not info.get("outputs", [{}])[0].get("cable_available"),
               "get_cop_info cable_structure via outputCableStructure, "
               "no cable() method", failures)

        # --- get_cop_geometry ---
        geo = cops.get_cop_geometry(hou, checker.path(), 0)
        total += 1
        _check(geo.get("status") == "success"
               and geo.get("geometry_entry") == "geometry"
               and geo.get("geometry", {}).get("available") is True
               and isinstance(geo.get("geometry", {}).get("point_count"),
                              int),
               "get_cop_geometry bounded counts via len()", failures)

        # --- get_cop_layer ---
        layer = cops.get_cop_layer(hou, checker.path(), 0)
        total += 1
        _check(layer.get("status") == "success"
               and layer.get("layer_entry") == "layer"
               and layer.get("layer", {}).get("available") is True
               and layer.get("layer", {}).get("resolution") == [1024, 1024],
               "get_cop_layer real ImageLayer metadata", failures)
        layer_surface = layer.get("layer", {}).get("surface", [])
        print("COPS-IMAGELAYER-SURFACE {0}".format(
            ",".join(layer_surface[:20])))

        # --- get_cop_vdb（真实 vdb() 入口；image 源无 NanoVDB wire）---
        vdb_result = cops.get_cop_vdb(hou, checker.path(), 0)
        total += 1
        _check(vdb_result.get("status") == "warning"
               and vdb_result.get("_warning", {}).get("code")
               == "vdb_unavailable"
               and vdb_result.get("vdb_entry") in (None, "vdb"),
               "get_cop_vdb real vdb() entry + structured unavailable "
               "for image-only generator", failures)

        # --- list_cop_node_types ---
        types_result = cops.list_cop_node_types(hou, "Cop")
        total += 1
        _check(types_result.get("status") == "success"
               and types_result.get("total", 0) > 100
               and any(t.get("name") == "checkerboard"
                       for t in types_result.get("node_types", [])),
               "list_cop_node_types real Cop registry enumeration", failures)

        # --- 读取不产生 undo 条目（NO_UNDO / READ_ONLY）---
        hou.undos.clear()
        _ = cops.get_cop_info(hou, checker.path())
        _ = cops.get_cop_geometry(hou, checker.path(), 0)
        _ = cops.get_cop_layer(hou, checker.path(), 0)
        _ = cops.get_cop_vdb(hou, checker.path(), 0)
        _ = cops.list_cop_node_types(hou, "Cop")
        total += 1
        _check(tuple(hou.undos.undoLabels()) == (),
               "read-only/no-undo queries create no undo entry", failures)

        # --- create_cop_node + set_cop_flags（MUTATING，产生 undo）---
        hou.undos.clear()
        created = cops.create_cop_node(
            hou, copnet.path(), "blur", node_name="smoke_created")
        total += 1
        _check(created.get("status") == "success"
               and created.get("created_path", "").endswith("smoke_created"),
               "create_cop_node real Copernicus node creation", failures)
        created_path = created.get("created_path")

        flags_result = cops.set_cop_flags(
            hou, created_path, {"display": True, "bypass": False,
                                "template": True})
        total += 1
        _check(flags_result.get("status") == "success"
               and flags_result.get("applied_flags")
               == ["display", "bypass", "template"],
               "set_cop_flags atomic whitelist + official setters", failures)

        total += 1
        _check(len(hou.undos.undoLabels()) >= 2,
               "create/flags produce undo entries (proves MUTATING)",
               failures)

        # --- flag 原子性：未知键在写入前拒绝 ---
        before_unknown = len(hou.undos.undoLabels())
        rejected = cops.set_cop_flags(
            hou, created_path, {"display": True, "bogus": True})
        total += 1
        _check(rejected.get("error", {}).get("code") == "unsupported_flag"
               and len(hou.undos.undoLabels()) == before_unknown,
               "unknown flag rejected before any write (atomic)", failures)

        # --- response cap ---
        responses = [info, geo, layer, vdb_result, types_result, created,
                     flags_result, rejected]
        total += 1
        _check(all(_response_size(response) <= 16384
                   for response in responses),
               "all responses respect 16KB cap", failures)

        # --- legacy COP2 拒绝（/img 下旧 COP2 节点）---
        legacy_result = cops.get_cop_info(hou, img.path())
        total += 1
        _check(legacy_result.get("error", {}).get("code") in (
                   "unsupported_legacy_cop2", "not_a_cop_node"),
               "/img network is not a Copernicus CopNode", failures)

        # --- 动态分类断言（import server，不启动）---
        server_name = "cops_live_pkg.server"
        server_spec = importlib.util.spec_from_file_location(
            server_name, os.path.join(ROOT, "server.py"))
        server_module = importlib.util.module_from_spec(server_spec)
        sys.modules[server_name] = server_module
        server_spec.loader.exec_module(server_module)
        cls = server_module.HoudiniMCPServer
        mut = cls.MUTATING_COMMANDS & COPS_7
        ro = cls.READ_ONLY_COMMANDS & COPS_7
        no_undo = cls.NO_UNDO_COMMANDS & COPS_7
        total += 1
        _check(mut == COPS_MUT and ro == COPS_RO and no_undo == COPS_NO_UNDO
               and not (mut & ro) and not (mut & no_undo)
               and not (ro & no_undo)
               and mut | ro | no_undo == COPS_7,
               "7 COP commands disjoint/exhaustive 3-way classification",
               failures)

        print("COPS-SMOKE-CABLE-H21 cable_method=false "
              "structure=outputCableStructure(index)")
        passed = total - len(failures)
        print("COPS-SMOKE-RESULT {0}/{1} {2}".format(
            passed, total, "PASS" if not failures else "FAIL"))
        return 0 if not failures else 1
    except Exception:
        traceback.print_exc()
        failures.append("unexpected exception")
        print("COPS-SMOKE-RESULT {0}/{1} FAIL".format(
            total - len(failures), total))
        return 1
    finally:
        try:
            import hou  # type: ignore
            hou.hipFile.clear(suppress_save_prompt=True)
            hou.undos.clear()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
