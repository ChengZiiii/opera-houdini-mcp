#!/usr/bin/env python
"""H21.0 真实 DOP network smoke for add-dops-tools。

使用 Houdini 21.0.596 hython + 真实 ``dopnet`` / ``emptyobject`` /
``gravity`` / ``merge`` / 默认 ``output``，覆盖：
- objects/findObject/relationships/time/timestep/memoryUsage 与 object record；
- timeline step + force cook + cache 生成；
- timeline reset + cache 清空/重建；
- owned simulation ``setTime(force_reset_sim=True)`` 的真实
  ``hou.PermissionError``；
- H21 force-reset live gate 结论（禁止可选路径）；
- response cap 与 READ_ONLY/NO_UNDO 唯一分类，运行后 undo stack 为空。

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
    pkg_name = "dops_live_pkg"
    for name in (pkg_name + "._dops", pkg_name + "._common", pkg_name):
        sys.modules.pop(name, None)
    package = types.ModuleType(pkg_name)
    package.__path__ = [ROOT]
    sys.modules[pkg_name] = package
    loaded = []
    for module_name in ("_common", "_dops"):
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


def main():
    failures = []
    total = 0
    try:
        import hou  # type: ignore
        version = tuple(hou.applicationVersion())
        print("DOPS-SMOKE-VERSION {0}.{1}.{2}".format(*version[:3]))
        cmn, dops = _load_modules()
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.playbar.setPlaybackRange(1.0, 24.0)

        total += 1
        _check(version[:2] == (21, 0), "H21.0 target version", failures)

        # 使用 dopnet 创建时已有且 display flag 打开的默认 output；不得再建
        # output1，否则 network 会 cook 未连接的默认 output，objects 为空。
        dopnet = hou.node("/obj").createNode("dopnet", "mcp_dops_smoke")
        dop_object = dopnet.createNode("emptyobject", "smoke_object")
        gravity = dopnet.createNode("gravity", "smoke_gravity")
        merge = dopnet.createNode("merge", "smoke_merge")
        merge.setInput(0, dop_object)
        merge.setInput(1, gravity)
        output = dopnet.node("output")
        output.setInput(0, merge)
        dopnet.layoutChildren()

        hou.setTime(hou.frameToTime(1.0))
        dopnet.cook(force=True)
        hou.undos.clear()
        total += 1
        _check(not dopnet.errors(), "real DOP network initial force cook", failures)

        info_before = dops.get_simulation_info(hou, dopnet.path())
        total += 1
        _check(info_before.get("status") == "success"
               and info_before.get("object_count") == 1
               and info_before.get("timestep") > 0,
               "get_simulation_info real simulation", failures)

        objects_result = dops.list_dop_objects(hou, dopnet.path(), limit=10)
        object_names = [item.get("name")
                        for item in objects_result.get("objects", [])]
        actual_object_name = object_names[0] if object_names else ""
        total += 1
        _check(objects_result.get("status") == "success"
               and objects_result.get("total") == 1
               and bool(actual_object_name),
               "list_dop_objects bounded real objects", failures)

        object_result = dops.get_dop_object(
            hou, dopnet.path(), actual_object_name, max_data=16)
        total += 1
        _check(object_result.get("status") == "success"
               and object_result.get("object", {}).get("object_id") == 0
               and "Options" in object_result.get("object", {}).get(
                   "record_types", []),
               "get_dop_object findObject + root record types", failures)

        field_result = dops.get_dop_field(
            hou, dopnet.path(), actual_object_name, ".", "name",
            record_type="Options", record_index=0)
        total += 1
        _check(field_result.get("status") == "success"
               and field_result.get("value") == actual_object_name,
               "get_dop_field real object Options/name", failures)

        relationships_result = dops.get_dop_relationships(
            hou, dopnet.path(), limit=10, max_objects=10)
        total += 1
        _check(relationships_result.get("status") == "success"
               and relationships_result.get("total", 0) >= 1
               and relationships_result.get("relationships", [])[0].get(
                   "name") == "smoke_merge",
               "get_dop_relationships real merge relationship", failures)

        memory_before = dops.get_sim_memory_usage(hou, dopnet.path())
        total += 1
        _check(memory_before.get("status") == "success"
               and memory_before.get("unit") == "bytes"
               and memory_before.get("memory_usage") >= 0,
               "get_sim_memory_usage unit + value", failures)

        step_result = dops.step_simulation(hou, dopnet.path(), frames=2)
        memory_after_step = dops.get_sim_memory_usage(hou, dopnet.path())
        total += 1
        _check(step_result.get("status") == "success"
               and step_result.get("old_frame") == 1.0
               and step_result.get("new_frame") == 3.0
               and hou.frame() == 3.0
               and not step_result.get("cook_errors"),
               "step timeline setTime then force cook", failures)

        total += 1
        _check(step_result.get("new_simulation_time")
               > step_result.get("old_simulation_time")
               and memory_after_step.get("memory_usage", 0) > 0
               and step_result.get("side_effects", {}).get(
                   "dop_cache_generated_or_replaced") is True,
               "step advances simulation time and creates cache", failures)

        reset_result = dops.reset_simulation(
            hou, dopnet.path(), reset_frame=1.0)
        memory_after_reset = dops.get_sim_memory_usage(hou, dopnet.path())
        total += 1
        _check(reset_result.get("status") == "warning"
               and reset_result.get("_warning", {}).get("code")
               == "force_reset_live_gate_blocked"
               and reset_result.get("new_frame") == 1.0
               and reset_result.get("new_simulation_time") == 0.0
               and not reset_result.get("cook_errors"),
               "timeline-first reset + blocked optional force path", failures)

        total += 1
        _check(memory_after_reset.get("memory_usage", 0)
               <= memory_after_step.get("memory_usage", 0)
               and reset_result.get("side_effects", {}).get(
                   "dop_cache_cleared_or_rebuilt") is True
               and reset_result.get("undoable") is False,
               "reset cache/no-undo side effects", failures)

        signature_confirmed = dops._probe_force_reset_signature(
            hou, dopnet.simulation())
        live_allowed = dops._force_reset_live_allowed(hou)
        total += 1
        _check(signature_confirmed and not live_allowed,
               "H21 signature confirmed but live gate fail-closed", failures)

        owned_permission = False
        builtin_confused = False
        try:
            dopnet.simulation().setTime(
                hou.frameToTime(1.0), force_reset_sim=True)
        except Exception as error:
            owned_permission = isinstance(error, hou.PermissionError)
            builtin_confused = isinstance(error, builtins.PermissionError)
            print("DOPS-FORCE-RESET {0}: {1}".format(
                error.__class__.__name__, error))
        total += 1
        _check(owned_permission and not builtin_confused,
               "owned simulation raises distinct hou.PermissionError", failures)

        responses = [
            info_before, objects_result, object_result, field_result,
            relationships_result, step_result, reset_result,
            memory_after_reset,
        ]
        total += 1
        _check(all(_response_size(response) <= 16384
                   for response in responses),
               "all eight public responses respect 16KB cap", failures)

        # Dynamic classification check without invoking a handler through an undo
        # group. Importing server uses the real H21 hou module but does not start it.
        server_name = "dops_live_pkg.server"
        server_spec = importlib.util.spec_from_file_location(
            server_name, os.path.join(ROOT, "server.py"))
        server_module = importlib.util.module_from_spec(server_spec)
        sys.modules[server_name] = server_module
        server_spec.loader.exec_module(server_module)
        commands = {
            "get_simulation_info", "list_dop_objects", "get_dop_object",
            "get_dop_field", "get_dop_relationships", "step_simulation",
            "reset_simulation", "get_sim_memory_usage",
        }
        read_only_expected = commands - {
            "step_simulation", "reset_simulation"}
        no_undo_expected = {"step_simulation", "reset_simulation"}
        cls = server_module.HoudiniMCPServer
        total += 1
        _check((cls.READ_ONLY_COMMANDS & commands) == read_only_expected
               and (cls.NO_UNDO_COMMANDS & commands) == no_undo_expected
               and not (cls.MUTATING_COMMANDS & commands),
               "DOP commands are disjoint/exhaustive READ_ONLY + NO_UNDO",
               failures)

        total += 1
        _check(tuple(hou.undos.undoLabels()) == (),
               "timeline/cook/cache operations created no undo entry", failures)

        print("DOPS-SMOKE-FORCE-RESET-ALLOWED H21.0=false")
        passed = total - len(failures)
        print("DOPS-SMOKE-RESULT {0}/{1} {2}".format(
            passed, total, "PASS" if not failures else "FAIL"))
        return 0 if not failures else 1
    except Exception:
        traceback.print_exc()
        failures.append("unexpected exception")
        print("DOPS-SMOKE-RESULT {0}/{1} FAIL".format(
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
