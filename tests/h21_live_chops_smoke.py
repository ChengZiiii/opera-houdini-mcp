#!/usr/bin/env python
"""H21.0 真实 CHOP network smoke for add-chops-tools。

使用 Houdini 21.0.596 hython + 真实 ``chopnet`` / ``wave`` 网络，覆盖：
- list_chop_channels：ChopNode.clip() → Clip.tracks() 真实 channel 枚举 +
  sample range/rate/count；
- get_chop_data：allSamples 完整路径、sample range（evalAtSampleRange，闭区
  间夹取）、单点 sample/frame/time（各 evalAt*）、allSamples guard
  （numSamples>max 时改走 sample_range + truncated）；
- create_chop_node：MUTATING 写 + undo 条目；
- export_chop_to_parm：在目标 parm 建 HScript chop() channel reference +
  reference 求值 + undo 条目 + 默认拒绝 occupied target；
- 读取不产生 undo 条目（NO_UNDO）；create/export 产生 undo（MUTATING）；
- 16KB response cap；MUTATING/NO_UNDO/READ_ONLY 唯一分类。

本脚本只在临时未保存场景中工作；结束时清空场景，不落盘。
"""
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
    pkg_name = "chops_live_pkg"
    for name in (pkg_name + "._chops", pkg_name + "._common", pkg_name):
        sys.modules.pop(name, None)
    package = types.ModuleType(pkg_name)
    package.__path__ = [ROOT]
    sys.modules[pkg_name] = package
    loaded = []
    for module_name in ("_common", "_chops"):
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


CHOPS_4 = {
    "get_chop_data", "list_chop_channels",
    "create_chop_node", "export_chop_to_parm",
}
CHOPS_MUT = {"create_chop_node", "export_chop_to_parm"}
CHOPS_NO_UNDO = {"get_chop_data", "list_chop_channels"}
CHOPS_RO = set()


def main():
    failures = []
    total = 0
    wave = None
    target_null = None
    try:
        import hou  # type: ignore
        version = tuple(hou.applicationVersion())
        print("CHOPS-SMOKE-VERSION {0}.{1}.{2}".format(*version[:3]))
        cmn, chops = _load_modules()
        hou.hipFile.clear(suppress_save_prompt=True)

        total += 1
        _check(version[:2] == (21, 0), "H21.0 target version", failures)

        # 真实 CHOP 网络：/obj -> chopnet（childTypeCategory "Chop" 容器）
        # -> wave 生成器。/ch 的 childTypeCategory 是 "ChopNet"，只接受
        # chopnet 容器；wave 等 operator 必须在 chopnet 容器内创建。
        chopnet = hou.node("/obj").createNode("chopnet", "mcp_chops_smoke")
        wave = chopnet.createNode("wave", "smoke_wave")
        wave.setDisplayFlag(True)
        chopnet.layoutChildren()
        wave.cook(force=True)
        wave_path = wave.path()
        total += 1
        _check(not wave.errors(),
               "real CHOP network initial force cook", failures)

        # --- list_chop_channels（clip/tracks 真实入口）---
        listing = chops.list_chop_channels(hou, wave_path, 0)
        total += 1
        _check(listing.get("status") == "success"
               and bool(listing.get("channels")),
               "list_chop_channels real clip/tracks enumeration", failures)
        first_channel = (listing.get("channels") or [{}])[0].get("name", "")
        sample_rate = listing.get("sample_rate")
        sr = listing.get("sample_range") or [None, None]
        print("CHOPS-SMOKE-CHANNEL first={0} rate={1} range={2}".format(
            first_channel, sample_rate, sr))
        total += 1
        _check(isinstance(sample_rate, (int, float)),
               "list_chop_channels real sample rate", failures)
        total += 1
        _check(sr[0] is not None and sr[1] is not None
               and sr[1] >= sr[0],
               "list_chop_channels real sample range (closed)", failures)

        # --- get_chop_data：完整 track（allSamples 路径）---
        full = chops.get_chop_data(hou, wave_path)
        total += 1
        _check(full.get("status") == "success"
               and bool(full.get("channels"))
               and full["channels"][0].get("query_mode") == "all_samples",
               "get_chop_data full track via allSamples", failures)
        total += 1
        _check(bool(full["channels"][0].get("samples")),
               "get_chop_data returns real sample values", failures)

        # --- get_chop_data：sample range（evalAtSampleRange，闭区间）---
        lo, hi = sr[0], sr[1]
        q_lo, q_hi = lo, min(lo + 2, hi)
        ranged = chops.get_chop_data(
            hou, wave_path, start=q_lo, end=q_hi)
        total += 1
        _check(ranged.get("status") == "success"
               and ranged["channels"][0].get("query_mode") == "sample_range"
               and ranged["channels"][0].get("actual_range") == [q_lo, q_hi],
               "get_chop_data sample range via evalAtSampleRange (closed)",
               failures)

        # --- get_chop_data：单点 sample/frame/time ---
        single_s = chops.get_chop_data(hou, wave_path, sample=q_lo)
        total += 1
        _check(single_s.get("status") == "success"
               and single_s["channels"][0].get("query_mode") == "sample",
               "get_chop_data single sample via evalAtSample", failures)
        single_f = chops.get_chop_data(hou, wave_path, frame=1.0)
        total += 1
        _check(single_f.get("status") == "success"
               and single_f["channels"][0].get("query_mode") == "frame",
               "get_chop_data single frame via evalAtFrame", failures)
        single_t = chops.get_chop_data(hou, wave_path, time=0.0)
        total += 1
        _check(single_t.get("status") == "success"
               and single_t["channels"][0].get("query_mode") == "time",
               "get_chop_data single time via evalAtTime", failures)

        # --- allSamples guard：max_samples_per_channel 小于 numSamples ---
        guard = chops.get_chop_data(
            hou, wave_path, max_samples_per_channel=2)
        total += 1
        _check(guard.get("status") == "success"
               and guard["channels"][0].get("query_mode") == "sample_range"
               and guard["channels"][0].get("truncated") is True,
               "allSamples guard falls back to sample_range + truncated",
               failures)

        # --- 读取不产生 undo 条目（NO_UNDO）---
        hou.undos.clear()
        _ = chops.list_chop_channels(hou, wave_path)
        _ = chops.get_chop_data(hou, wave_path)
        _ = chops.get_chop_data(hou, wave_path, sample=0)
        total += 1
        _check(tuple(hou.undos.undoLabels()) == (),
               "read-only/no-undo queries create no undo entry", failures)

        # --- create_chop_node（MUTATING，产生 undo）---
        hou.undos.clear()
        created = chops.create_chop_node(
            hou, chopnet.path(), "wave", node_name="smoke_created")
        total += 1
        _check(created.get("status") == "success"
               and created.get("created_path", "").endswith("smoke_created"),
               "create_chop_node real CHOP node creation", failures)
        total += 1
        _check(len(hou.undos.undoLabels()) >= 1,
               "create_chop_node produces undo entry (proves MUTATING)",
               failures)

        # --- export_chop_to_parm：在 obj null parm 建 chop() reference ---
        hou.undos.clear()
        target_null = hou.node("/obj").createNode(
            "null", "mcp_chops_target")
        target_path = target_null.path()
        # 默认 target tx 无 expression/keyframe → 直接建 reference
        exported = chops.export_chop_to_parm(
            hou, wave_path, first_channel, target_path, "tx")
        total += 1
        _check(exported.get("status") == "success"
               and exported.get("channel_path", "").startswith(wave_path)
               and exported.get("expression_language") == "Hscript",
               "export_chop_to_parm creates Hscript chop() reference",
               failures)
        # reference 求值：target tx eval 应为数值（CHOP 驱动）
        try:
            driven_val = target_null.parm("tx").eval()
        except Exception:
            driven_val = None
        total += 1
        _check(isinstance(driven_val, (int, float)),
               "chop() reference evaluates to numeric value", failures)
        print("CHOPS-SMOKE-DRIVEN value={0}".format(driven_val))
        total += 1
        _check(len(hou.undos.undoLabels()) >= 1,
               "export_chop_to_parm produces undo entry (proves MUTATING)",
               failures)

        # --- export 默认拒绝 occupied target ---
        # 在 ty 上先手动设 expression，再尝试 export（不带 replace）
        target_null.parm("ty").setExpression(
            "1 + 1", hou.exprLanguage.Hscript)
        before_occ = len(hou.undos.undoLabels())
        occupied = chops.export_chop_to_parm(
            hou, wave_path, first_channel, target_path, "ty")
        total += 1
        _check(occupied.get("status") == "warning"
               and occupied.get("_warning", {}).get("code")
               == "target_occupied"
               and occupied.get("existing_expression") == "1 + 1",
               "export rejects occupied target by default", failures)
        # replace_existing=True 替换并披露
        replaced = chops.export_chop_to_parm(
            hou, wave_path, first_channel, target_path, "ty",
            replace_existing=True)
        total += 1
        _check(replaced.get("status") == "success"
               and replaced.get("replaced_existing") is True
               and replaced.get("previous_expression") == "1 + 1",
               "export replace_existing discloses old/new value", failures)

        # --- response cap ---
        responses = [listing, full, ranged, single_s, single_f, single_t,
                     guard, created, exported, occupied, replaced]
        total += 1
        _check(all(_response_size(r) <= 16384 for r in responses),
               "all responses respect 16KB cap", failures)

        # --- 动态分类断言（import server，不启动）---
        server_name = "chops_live_pkg.server"
        server_spec = importlib.util.spec_from_file_location(
            server_name, os.path.join(ROOT, "server.py"))
        server_module = importlib.util.module_from_spec(server_spec)
        sys.modules[server_name] = server_module
        server_spec.loader.exec_module(server_module)
        cls = server_module.HoudiniMCPServer
        mut = cls.MUTATING_COMMANDS & CHOPS_4
        ro = cls.READ_ONLY_COMMANDS & CHOPS_4
        no_undo = cls.NO_UNDO_COMMANDS & CHOPS_4
        total += 1
        _check(mut == CHOPS_MUT and ro == CHOPS_RO and no_undo == CHOPS_NO_UNDO
               and not (mut & ro) and not (mut & no_undo)
               and not (ro & no_undo)
               and mut | ro | no_undo == CHOPS_4,
               "4 CHOP commands disjoint/exhaustive 3-way classification",
               failures)

        passed = total - len(failures)
        print("CHOPS-SMOKE-RESULT {0}/{1} {2}".format(
            passed, total, "PASS" if not failures else "FAIL"))
        return 0 if not failures else 1
    except Exception:
        traceback.print_exc()
        failures.append("unexpected exception")
        print("CHOPS-SMOKE-RESULT {0}/{1} FAIL".format(
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
