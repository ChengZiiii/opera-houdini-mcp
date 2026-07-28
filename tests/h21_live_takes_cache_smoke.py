#!/usr/bin/env python
"""H21.0 真实 Takes + cache node smoke for add-takes-and-cache-tools。

使用 Houdini 21.0.596 hython + 真实 ``hou.takes`` 与 ``Sop/filecache``
网络，覆盖：
- list_takes / get_current_take：真实 Takes 树枚举 + current 切换。
- set_current_take：hou.takes.findTake 解析 Take 对象后传给
  setCurrentTake（**不**传字符串）。
- create_take：hou.takes.findTake 拒绝重复；pre-validate parm tuple
  解析为真实 hou.ParmTuple；include addParmTuple 临时切 current +
  finally 恢复；预校验失败零部分残留。
- list_caches：BFS 走 children，filecache 在白名单，Sop/file 不在。
- get_cache_status：status / output_path / file_exists 真实读取。
- clear_cache：loadfromdisk 切 0 + cook。
- write_cache：cook + geometry().saveToFile() 落真实磁盘文件（隔离
  临时目录），验证 file_exists + size_bytes > 0。
- 4 读取 + clear 不产生 undo（NO_UNDO），set/create 产生 undo
  （MUTATING）。
- 16KB response cap；8 命令三分类互斥穷尽。

本脚本只在临时未保存场景中工作；结束时清空场景 + 删除临时目录。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import traceback
import types


HYTHON = r"C:\Program Files\Side Effects Software\Houdini 21.0.596\bin\hython.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_modules():
    pkg_name = "takes_cache_live_pkg"
    for name in (pkg_name + "._scene", pkg_name + "._common",
                 pkg_name + "._cache_nodes", pkg_name):
        sys.modules.pop(name, None)
    package = types.ModuleType(pkg_name)
    package.__path__ = [ROOT]
    sys.modules[pkg_name] = package
    loaded = []
    for module_name in ("_common", "_scene", "_cache_nodes"):
        full_name = pkg_name + "." + module_name
        spec = importlib.util.spec_from_file_location(
            full_name, os.path.join(ROOT, module_name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded[0], loaded[1], loaded[2]


def _check(condition, label, failures):
    if condition:
        print("PASS: " + label)
        return True
    print("FAIL: " + label)
    failures.append(label)
    return False


def _response_size(value):
    return len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))


TC_8 = {
    "list_takes", "get_current_take", "set_current_take", "create_take",
    "list_caches", "get_cache_status", "clear_cache", "write_cache",
}
TC_MUT = {"set_current_take", "create_take"}
TC_NO_UNDO = {"clear_cache", "write_cache"}
TC_RO = {"list_takes", "get_current_take", "list_caches", "get_cache_status"}


def main():
    failures = []
    total = 0
    tmp = tempfile.mkdtemp(prefix="mcp_takes_cache_smoke_")
    try:
        import hou  # type: ignore
        version = tuple(hou.applicationVersion())
        print("TAKES-CACHE-SMOKE-VERSION {0}.{1}.{2}".format(*version[:3]))
        cmn, scene, cache = _load_modules()
        hou.hipFile.clear(suppress_save_prompt=True)

        total += 1
        _check(version[:2] == (21, 0), "H21.0 target version", failures)

        # --- list_takes / get_current_take (real hou.takes) ---
        initial_takes = scene.list_takes(hou)
        total += 1
        _check(initial_takes.get("status") == "success"
               and any(t.get("current") for t in initial_takes.get("takes", [])),
               "list_takes real takes enumeration", failures)
        cur = scene.get_current_take(hou)
        total += 1
        _check(cur.get("status") == "success"
               and cur.get("name") == "Main"
               and cur.get("path") == "Main"
               and cur.get("current") is True,
               "get_current_take reports Main", failures)

        # --- create_take + set_current_take ---
        # Real geo network with a parm to include
        geo = hou.node("/obj").createNode("geo", "mcp_takes_cache_geo")
        # Clean up auto-generated nodes (file1 etc) and create our own
        for c in list(geo.children()):
            c.destroy()
        box = geo.createNode("box", "smoke_box")
        box_path = box.path()
        parm_path = box_path + "/size"
        parm_x = box_path + "/sizex"
        # create a child take (no include first)
        hou.undos.clear()
        before_takes = len(hou.takes.takes())
        total += 1
        created = scene.create_take(hou, "mcp_smoke_a")
        _check(created.get("status") == "success"
               and created.get("name") == "mcp_smoke_a"
               and created.get("parent") == "Main"
               and len(hou.takes.takes()) == before_takes + 1,
               "create_take real hou.Take addChildTake", failures)
        total += 1
        _check(len(hou.undos.undoLabels()) >= 1,
               "create_take (no include) produces undo entry", failures)
        # destroy for cleanup before next test
        if created.get("status") == "success":
            hou.takes.findTake("mcp_smoke_a").destroy()

        # create with include + atomic pre-validation rollback
        hou.undos.clear()
        before_takes = len(hou.takes.takes())
        incl = scene.create_take(
            hou, "mcp_smoke_inc",
            include_parms=[parm_path])
        if incl.get("status") != "success":
            print("TAKES-CACHE-SMOKE-DEBUG incl", incl)
        total += 1
        _check(incl.get("status") == "success"
               and incl.get("name") == "mcp_smoke_inc"
               and len(incl.get("include_parms", [])) == 1
               and incl["include_parms"][0]["parm_tuple"] == "size",
               "create_take include parm tuple resolved", failures)
        total += 1
        _check(len(hou.undos.undoLabels()) >= 1,
               "create_take include produces undo entry", failures)
        # current must be Main (we never switched in caller)
        total += 1
        _check(hou.takes.currentTake().name() == "Main",
               "create_take restore current after include", failures)
        # Component parm path variant
        hou.undos.clear()
        incl2 = scene.create_take(
            hou, "mcp_smoke_inc2",
            include_parms=[parm_x])
        total += 1
        _check(incl2.get("status") == "success"
               and len(incl2.get("include_parms", [])) == 1
               and incl2["include_parms"][0]["parm_tuple"] == "size",
               "create_take include component path resolves to size tuple",
               failures)
        if incl2.get("status") == "success":
            hou.takes.findTake("mcp_smoke_inc2").destroy()

        # atomic pre-validation: invalid + valid -> rejected, no take
        before_takes = len(hou.takes.takes())
        rejected = scene.create_take(
            hou, "mcp_smoke_atomic",
            include_parms=[parm_path, "/obj/missing/ty"])
        total += 1
        _check(rejected.get("error", {}).get("code") == "parm_not_found"
               and len(hou.takes.takes()) == before_takes,
               "create_take atomic pre-validation rejects mix", failures)
        # duplicate name rejected
        dup = scene.create_take(hou, "mcp_smoke_inc")
        total += 1
        _check(dup.get("error", {}).get("code") == "take_name_conflict",
               "create_take duplicate name rejected", failures)
        # slash in name rejected
        slash = scene.create_take(hou, "a/b")
        total += 1
        _check(slash.get("error", {}).get("code") == "invalid_take_name",
               "create_take slash in name rejected", failures)
        # set_current_take: pass real Take object
        switch = scene.set_current_take(hou, "mcp_smoke_inc")
        total += 1
        _check(switch.get("status") == "success"
               and switch.get("name") == "mcp_smoke_inc"
               and switch.get("current") is True
               and hou.takes.currentTake().name() == "mcp_smoke_inc",
               "set_current_take uses real Take object", failures)
        # switch back
        scene.set_current_take(hou, "Main")

        # --- list_caches / get_cache_status (real filecache::2.0) ---
        # real filecache::2.0 in geo (need to keep box as source)
        fc = geo.createNode("filecache", "mcp_smoke_fc")
        fc_path = fc.path()
        target_file = os.path.join(tmp, "mcpcache.bgeo.sc")
        fc.parm("file").set(target_file)
        fc.parm("filemethod").set("explicit")
        fc.parm("loadfromdisk").set(0)
        fc.parm("timedependent").set(0)
        fc.parm("sopoutput").set(box.path())
        fc.setRenderFlag(True)
        fc.setDisplayFlag(True)
        # also add a regular Sop/file to verify it's excluded
        sop_file = geo.createNode("file", "mcp_smoke_sopfile")
        sop_file_path = sop_file.path()
        geo.layoutChildren()

        # list_caches real
        listing = cache.list_caches(hou, geo.path(), max_nodes=16)
        total += 1
        _check(listing.get("status") == "success"
               and any(c.get("path") == fc_path
                       and c.get("adapter") == "filecache"
                       for c in listing.get("caches", [])),
               "list_caches enumerates filecache (not Sop/file)", failures)
        total += 1
        _check(not any(c.get("path") == sop_file_path
                       for c in listing.get("caches", [])),
               "list_caches excludes plain Sop/file", failures)

        # get_cache_status real
        status = cache.get_cache_status(hou, fc_path)
        total += 1
        _check(status.get("status") == "success"
               and status.get("adapter") == "filecache"
               and status.get("cache_status", {}).get("type")
               == "filecache::2.0"
               and status.get("cache_status", {}).get("output_path")
               == target_file
               and status.get("cache_status", {}).get("file_exists") is False,
               "get_cache_status real filecache output_path", failures)

        # unsupported type: get on Sop/file
        bad = cache.get_cache_status(hou, sop_file_path)
        total += 1
        _check(bad.get("error", {}).get("code") == "unsupported_cache_type",
               "get_cache_status rejects plain Sop/file", failures)

        # --- write_cache real: cook + saveToFile creates file ---
        write = cache.write_cache(hou, fc_path)
        total += 1
        _check(write.get("status") == "success"
               and write.get("written", {}).get("written") is True
               and write.get("written", {}).get("file_exists") is True
               and (write.get("written", {}).get("size_bytes") or 0) > 0
               and os.path.isfile(target_file),
               "write_cache cooks + saveToFile writes real file", failures)

        # --- get_cache_status now reports file_exists True ---
        status2 = cache.get_cache_status(hou, fc_path)
        total += 1
        _check(status2.get("cache_status", {}).get("file_exists") is True,
               "get_cache_status file_exists after write", failures)

        # --- clear_cache: loadfromdisk=0 + cook, no disk delete by default ---
        hou.undos.clear()
        cleared = cache.clear_cache(hou, fc_path)
        total += 1
        _check(cleared.get("status") == "success"
               and cleared.get("cleared", {}).get("cleared") is True
               and fc.parm("loadfromdisk").eval() == 0
               and not cleared.get("disk_removed")
               and os.path.isfile(target_file),
               "clear_cache flips loadfromdisk + keeps disk file", failures)
        total += 1
        _check(tuple(hou.undos.undoLabels()) == (),
               "clear_cache produces no undo entry (NO_UNDO)", failures)

        # clear with remove_disk_file actually deletes
        cleared2 = cache.clear_cache(
            hou, fc_path, remove_disk_file=True)
        total += 1
        _check(cleared2.get("disk_removed") is True
               and not os.path.isfile(target_file),
               "clear_cache remove_disk_file deletes real file", failures)

        # --- read paths produce no undo (NO_UNDO/READ_ONLY) ---
        hou.undos.clear()
        _ = scene.list_takes(hou)
        _ = scene.get_current_take(hou)
        _ = cache.list_caches(hou, geo.path(), max_nodes=8)
        _ = cache.get_cache_status(hou, fc_path)
        total += 1
        _check(tuple(hou.undos.undoLabels()) == (),
               "read-only/no-undo queries create no undo entry", failures)

        # --- response cap (16KB) ---
        responses = [initial_takes, cur, created, incl, rejected,
                     switch, listing, status, write, status2, cleared,
                     cleared2]
        total += 1
        _check(all(_response_size(r) <= 16384 for r in responses),
               "all responses respect 16KB cap", failures)

        # --- 8 commands disjoint/exhaustive 3-way classification ---
        server_name = "takes_cache_live_pkg.server"
        server_spec = importlib.util.spec_from_file_location(
            server_name, os.path.join(ROOT, "server.py"))
        server_module = importlib.util.module_from_spec(server_spec)
        sys.modules[server_name] = server_module
        server_spec.loader.exec_module(server_module)
        cls = server_module.HoudiniMCPServer
        mut = cls.MUTATING_COMMANDS & TC_8
        ro = cls.READ_ONLY_COMMANDS & TC_8
        no_undo = cls.NO_UNDO_COMMANDS & TC_8
        total += 1
        _check(mut == TC_MUT and ro == TC_RO and no_undo == TC_NO_UNDO
               and not (mut & ro) and not (mut & no_undo)
               and not (ro & no_undo)
               and mut | ro | no_undo == TC_8,
               "8 takes-cache commands disjoint/exhaustive 3-way",
               failures)

        passed = total - len(failures)
        print("TAKES-CACHE-SMOKE-RESULT {0}/{1} {2}".format(
            passed, total, "PASS" if not failures else "FAIL"))
        return 0 if not failures else 1
    except Exception:
        traceback.print_exc()
        failures.append("unexpected exception")
        print("TAKES-CACHE-SMOKE-RESULT {0}/{1} FAIL".format(
            total - len(failures), total))
        return 1
    finally:
        try:
            import hou  # type: ignore
            hou.hipFile.clear(suppress_save_prompt=True)
            hou.undos.clear()
        except Exception:
            pass
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
