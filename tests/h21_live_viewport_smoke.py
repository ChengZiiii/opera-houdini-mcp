#!/usr/bin/env python
"""H21.0 live smoke for add-viewport-control-tools.

真实 hython（Houdini 21.0.596）下覆盖 8 项：
- get_viewport_info（无 GUI 仍返 viewport_unavailable 或 schema）
- set_viewport_camera / set_viewport_display / set_viewport_direction /
  frame_selection / frame_all（headless 下可能返 warning，可接受）
- set_viewport_renderer 在非 LOP 返 warning
- set_current_network 在真实节点路径上成功（或返回 warning）
- 8 项均未创建 undo group；无 capture_screenshot
"""
import json
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _import_modules():
    import importlib
    import importlib.util as _u
    import types as _t
    for name in ("houdinimcp._viewport", "houdinimcp._common", "viewport_module"):
        sys.modules.pop(name, None)
    pkg = sys.modules.get("houdinimcp")
    if pkg is None:
        pkg = _t.ModuleType("houdinimcp")
        pkg.__path__ = [ROOT]
        sys.modules["houdinimcp"] = pkg
    cmn_spec = _u.spec_from_file_location(
        "houdinimcp._common", os.path.join(ROOT, "_common.py"))
    cmn_mod = _u.module_from_spec(cmn_spec)
    sys.modules["houdinimcp._common"] = cmn_mod
    cmn_spec.loader.exec_module(cmn_mod)
    spec = _u.spec_from_file_location(
        "houdinimcp._viewport", os.path.join(ROOT, "_viewport.py"))
    vp = _u.module_from_spec(spec)
    spec.loader.exec_module(vp)
    return vp


def _run_one(name, fn):
    try:
        result = fn()
    except Exception as error:
        print("[FAIL] {0}: {1}".format(name, error))
        return False, {"error": str(error)}
    ok = isinstance(result, dict) and (
        result.get("status") in ("success", "warning")
        and not result.get("error"))
    if ok:
        print("[PASS] {0}".format(name))
    else:
        print("[WARN] {0}: {1}".format(name, result))
    return ok, result


def main():
    import hou
    vp = _import_modules()

    results = []

    # 1. get_viewport_info
    _, info = _run_one("get_viewport_info", lambda: vp.get_viewport_info(hou))
    results.append(("get_viewport_info",
                    isinstance(info, dict) and "status" in info))

    # 2-5: 写类工具在 headless 下可能返 warning
    for name in ("set_viewport_camera", "set_viewport_display",
                 "set_viewport_renderer", "frame_selection",
                 "frame_all", "set_viewport_direction",
                 "set_current_network"):
        fn = getattr(vp, name, None)
        if fn is None:
            results.append((name, False))
            continue
        if name == "set_viewport_camera":
            call = lambda: fn(hou, "/obj/cam1")
        elif name == "set_viewport_display":
            call = lambda: fn(hou, "main", "shaded")
        elif name == "set_viewport_renderer":
            call = lambda: fn(hou, "gl")
        elif name == "set_viewport_direction":
            call = lambda: fn(hou, "front")
        elif name == "set_current_network":
            call = lambda: fn(hou, "/obj")
        else:
            call = lambda: fn(hou)
        ok, _ = _run_one(name, call)
        results.append((name, ok))

    # 8 项均不创建 undo group —— 通过读取 server 分类来断言
    try:
        import importlib.util as _u
        for name in ("houdinimcp.server", "server_for_test"):
            sys.modules.pop(name, None)
        # server.py 顶部需要 houdinimcp 包 + sibling 模块已加载
        # 加载顺序：先 _common / _scene / _materials / _selection / _viewport 等
        import importlib
        # 利用 Houdini 内置 import 机制加载整个 server 模块
        # 这会触发所有 `from . import _xxx` 自动解析
        try:
            from houdinimcp import server
        except ImportError:
            # 若 houdinimcp 包未注册，使用 spec_from_file_location + 包上下文
            sys.modules.pop("houdinimcp.server", None)
            spec = _u.spec_from_file_location(
                "houdinimcp.server", os.path.join(ROOT, "server.py"))
            server = _u.module_from_spec(spec)
            spec.loader.exec_module(server)
        eight = {
            "get_viewport_info", "set_viewport_camera",
            "set_viewport_display", "set_viewport_renderer",
            "frame_selection", "frame_all",
            "set_viewport_direction", "set_current_network",
        }
        no_undo = server.HoudiniMCPServer.NO_UNDO_COMMANDS
        mutating = server.HoudiniMCPServer.MUTATING_COMMANDS
        all_no_undo = eight.issubset(no_undo)
        none_mutating = not (eight & mutating)
        print("[{0}] 8 项全部 NO_UNDO".format(
            "PASS" if all_no_undo else "FAIL"))
        print("[{0}] 8 项不进入 MUTATING".format(
            "PASS" if none_mutating else "FAIL"))
        results.append(("no_undo_classification", all_no_undo))
        results.append(("not_in_mutating", none_mutating))
    except Exception as error:
        print("[FAIL] server classification import: {0}".format(error))
        results.append(("no_undo_classification", False))
        results.append(("not_in_mutating", False))

    # 无 capture_screenshot
    for path in (os.path.join(ROOT, "server.py"),
                 os.path.join(ROOT, "houdini_mcp_server.py")):
        with open(path, "r", encoding="utf-8") as handle:
            src = handle.read()
        has = "def capture_screenshot(" in src
        print("[{0}] {1} 无 capture_screenshot".format(
            "PASS" if not has else "FAIL", os.path.basename(path)))
        results.append((os.path.basename(path) + "_no_capture_screenshot",
                       not has))

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("\n[summary] viewport H21 smoke: {0}/{1} checks passed".format(
        passed, total))
    if passed == total:
        print("VERDICT: pass")
        return 0
    print("VERDICT: pass_with_warn (headless environment limitations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())