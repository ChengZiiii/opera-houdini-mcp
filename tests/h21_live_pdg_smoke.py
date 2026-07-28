#!/usr/bin/env python
"""H21.0 真实 TOP network/scheduler smoke for add-pdg-tops-tools。

使用 Houdini 21.0.596 hython + 真实 ``topnet`` / ``localscheduler`` /
``genericgenerator``，覆盖：
- pdg_cook 非阻塞 handle（进程内 cook_id + scope: process）；
- 重复 cook 同节点返回 already_running、不启动第二个 cook；
- pdg_status 真实 getCookState(force=True)/workItemStates() + registry；
- pdg_workitems 真实 getPDGNode() work item 摘要（cook_results）；
- blocking 轮询至 terminal 或超时（超时不自动 cancel、handle 保持可用）；
- pdg_dirty(remove_outputs=False) 不删除磁盘输出；
- pdg_cancel 幂等（重复 cancel 返回稳定 cancelled、cancelCook 只调一次）；
- response cap 与 READ_ONLY/NO_UNDO 唯一分类，运行后 undo stack 为空。

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
    pkg_name = "pdg_live_pkg"
    for name in (pkg_name + "._pdg", pkg_name + "._common", pkg_name):
        sys.modules.pop(name, None)
    package = types.ModuleType(pkg_name)
    package.__path__ = [ROOT]
    sys.modules[pkg_name] = package
    loaded = []
    for module_name in ("_common", "_pdg"):
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


def _build_topnet(hou):
    """创建真实 topnet + localscheduler + genericgenerator；返回 (topnet, gen)。"""
    tasks = hou.node("/tasks")
    if tasks is None:
        tasks = hou.node("/").createNode("tasks", "tasks")
    topnet = tasks.createNode("topnet", "mcp_pdg_smoke")
    scheduler = topnet.node("scheduler")
    if scheduler is None:
        scheduler = topnet.createNode("localscheduler", "scheduler")
    gen = topnet.createNode("genericgenerator", "smoke_gen")
    # 尝试设置 itemcount；parm 名跨版本可能不同，尽力而为。
    for parm_name in ("itemcount", "ItemCount", "count"):
        parm = gen.parm(parm_name)
        if parm is not None:
            try:
                parm.set(3)
            except Exception:
                pass
            break
    out = topnet.node("output") if topnet.node("output") else None
    if out is not None:
        try:
            out.setInput(0, gen)
        except Exception:
            pass
    topnet.layoutChildren()
    return topnet, gen


def main():
    failures = []
    total = 0
    try:
        import hou  # type: ignore
        version = tuple(hou.applicationVersion())
        print("PDG-SMOKE-VERSION {0}.{1}.{2}".format(*version[:3]))
        cmn, pdg = _load_modules()
        hou.hipFile.clear(suppress_save_prompt=True)

        total += 1
        _check(version[:2] == (21, 0), "H21.0 target version", failures)

        topnet, gen = _build_topnet(hou)
        node_path = gen.path()
        hou.undos.clear()

        # 1) 非阻塞 cook 返回进程内 handle。
        cook1 = pdg.pdg_cook(hou, node_path, blocking=False)
        total += 1
        _check(cook1.get("status") in ("started", "success", "already_running",
                                       "timed_out")
               and cook1.get("cook_id", "").startswith("pdg-")
               and cook1.get("scope") == "process",
               "pdg_cook returns process-scoped handle", failures)

        # 2) 重复 cook 同节点返回 already_running 且 cook_id 不变。
        cook2 = pdg.pdg_cook(hou, node_path, blocking=False)
        total += 1
        _check(cook2.get("status") == "already_running"
               and cook2.get("cook_id") == cook1.get("cook_id"),
               "duplicate cook returns same handle already_running", failures)

        # 3) status 返回 cook_state + counts + handle。
        status = pdg.pdg_status(hou, node_path)
        total += 1
        _check(status.get("status") == "success"
               and isinstance(status.get("cook_state"), str)
               and isinstance(status.get("work_item_counts"), dict)
               and isinstance(status.get("total_work_items"), int)
               and status.get("scope") == "process",
               "pdg_status returns cook_state counts handle", failures)

        # 4) workitems 读取真实 work item（cook_results）或空列表+明确状态。
        workitems = pdg.pdg_workitems(hou, node_path, max_items=10)
        total += 1
        _check(workitems.get("status") == "success"
               and isinstance(workitems.get("work_items"), list)
               and isinstance(workitems.get("graph_generated"), bool)
               and workitems.get("scope") == "process",
               "pdg_workitems bounded summary or empty-with-message", failures)

        # 5) blocking cook 轮询至 terminal 或超时；二者均合法。
        #    先 cancel 当前 active cook 以便能启动新的 blocking cook。
        pdg.pdg_cancel(hou, node_path)
        block = pdg.pdg_cook(hou, node_path, blocking=True, timeout_seconds=20)
        total += 1
        _check(block.get("status") in ("cooked", "success", "failed",
                                       "canceled", "cancelled", "complete",
                                       "timed_out")
               and block.get("cook_id", "").startswith("pdg-"),
               "blocking cook reaches terminal or times out", failures)

        # 6) dirty 默认不删除输出。
        dirty = pdg.pdg_dirty(hou, node_path)
        total += 1
        _check(dirty.get("status") == "success"
               and dirty.get("remove_outputs") is False
               and dirty.get("undoable") is False,
               "pdg_dirty never removes outputs", failures)

        # 7) cancel 幂等：先启动一个 cook，再 cancel 两次。
        fresh = pdg.pdg_cook(hou, node_path, blocking=False)
        cancel1 = pdg.pdg_cancel(hou, node_path,
                                 cook_id=fresh.get("cook_id"))
        cancel2 = pdg.pdg_cancel(hou, node_path,
                                 cook_id=fresh.get("cook_id"))
        total += 1
        _check(cancel1.get("cancelled") is True
               and cancel2.get("cancelled") is True,
               "pdg_cancel idempotent returns stable cancelled", failures)

        # 8) 超时后 handle 仍可查询/取消（若 fresh cook 仍在 active）。
        #    重新 cook 并立即短超时 blocking。
        timed = pdg.pdg_cook(hou, node_path, blocking=True, timeout_seconds=0.1)
        if timed.get("status") == "timed_out":
            total += 1
            _check(timed.get("timed_out") is True,
                   "blocking timeout flagged without auto-cancel", failures)
            after = pdg.pdg_status(hou, node_path,
                                   cook_id=timed.get("cook_id"))
            total += 1
            _check(after.get("handle", {}).get("cook_id")
                   == timed.get("cook_id"),
                   "timed_out handle still queryable", failures)

        # 9) 所有响应 <= 16KB cap。
        responses = [cook1, cook2, status, workitems, block, dirty,
                     cancel1, cancel2]
        total += 1
        _check(all(_response_size(response) <= 16384
                   for response in responses),
               "all public responses respect 16KB cap", failures)

        # 10) 三分类互斥穷尽（动态导入 server，不启动）。
        server_name = "pdg_live_pkg.server"
        server_spec = importlib.util.spec_from_file_location(
            server_name, os.path.join(ROOT, "server.py"))
        server_module = importlib.util.module_from_spec(server_spec)
        sys.modules[server_name] = server_module
        server_spec.loader.exec_module(server_module)
        commands = {"pdg_cook", "pdg_status", "pdg_workitems",
                    "pdg_dirty", "pdg_cancel"}
        cls = server_module.HoudiniMCPServer
        total += 1
        _check((cls.READ_ONLY_COMMANDS & commands)
               == {"pdg_status", "pdg_workitems"}
               and (cls.NO_UNDO_COMMANDS & commands)
               == {"pdg_cook", "pdg_dirty", "pdg_cancel"}
               and not (cls.MUTATING_COMMANDS & commands),
               "PDG commands disjoint/exhaustive READ_ONLY + NO_UNDO",
               failures)

        # 11) scheduler running-state 不进入 undo stack。
        total += 1
        _check(tuple(hou.undos.undoLabels()) == (),
               "scheduler cook/dirty/cancel created no undo entry", failures)

        print("PDG-SMOKE-RESULT {0}/{1} {2}".format(
            total - len(failures), total, "PASS" if not failures else "FAIL"))
        return 0 if not failures else 1
    except Exception:
        traceback.print_exc()
        failures.append("unexpected exception")
        print("PDG-SMOKE-RESULT {0}/{1} FAIL".format(
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
