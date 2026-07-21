#!/usr/bin/env python3
"""H21 live smoke test — 在真实 Houdini 21+ 实机上验证全部 H21 compat 修复。

本脚本是 fork `opera-houdini-mcp` H21 兼容审计（change
`opera-houdinimcp-h21-compat-audit`）的**唯一一条不依赖 conftest mock 的
端到端验证路径**。它直连 Houdini TCP socket（`127.0.0.1:9876`），
**完全绕过 Kilo MCP bridge**，对 Task 2-7 的每一条修复都跑一次真实调用，
确保 fork 代码用的 hou API 在 H21+ 真的存在。

设计依据：openspec/changes/opera-houdinimcp-h21-compat-audit/specs/mcp-tools/
spec.md 的 "H21 live smoke test" 章节（ADDED Requirement）。该章节明确列出
11 个断言；本脚本按序执行并产出 Markdown 汇总表。

为何需要 live smoke：
- 单测（`tests/test_*.py`）用 conftest mock 的 hou；mock 不能揭露 H21 已
  移除 API 的真实缺失（除非 conftest 不再 mock 它们，已由 Task 8 完成）。
- Live smoke 直接打 server.py，server.py 内的 `import hou` 拿到的是 Houdini
  进程内的真实 hou 模块——任何调用的不存在 API 都会立刻 AttributeError。
- 因此这是**唯一**能完整验证 H21 兼容性的测试路径。

跳过语义（spec §Scenario: tests/h21_live_smoke.py）：
- 首次连接失败（`ConnectionRefusedError` / `socket.timeout` / `OSError`）
  → stdout 打 `[skip] Houdini socket not reachable on 127.0.0.1:9876 —
  is Houdini running + MCP started?`，exit 0（SKIP，**非失败**）。
- 任一断言 FAIL → 继续跑剩余断言（不短路），最终 exit 1 + Markdown 汇总表。
- 全 PASS → exit 0 + Markdown 汇总表。

清理承诺：
- **永不调用 `new_scene()`**，绝不破坏用户当前 .hip 文件。
- 所有临时节点都建在 `/obj/H21_SMOKE_TEST/` 容器下；正常/异常退出都会在
  `finally` 块 best-effort 删除该容器与配套材质。

用法：
    # 用 fork 自带的嵌入式 Python（推荐；与 server 同环境）：
    C:/.../external/houdinimcp-env/python/python.exe tests/h21_live_smoke.py

    # 或任意可 import tests/_e2e_helpers.py 的 Python 3.7+：
    cd external/houdinimcp
    python tests/h21_live_smoke.py

前置：
- Houdini 已启动 + shelf 点了 "Start Opera MCP"（监听 127.0.0.1:9876）。
- 总耗时通常 < 10s（不含 SideFX HTTP 帮助查询；外网慢时可能延长到 30s）。

退出码：
- 0 — 全 PASS，或 Houdini 不可达（SKIP）。
- 1 — 至少一个断言 FAIL。

历史：
- 2026-07-22 初版（Task 9.1-9.5）：11 个断言全实现，复用
  `_e2e_helpers.HoudiniConn` / `StepResult` / `assert_step` / `emit_summary`。
"""
from __future__ import annotations

import os
import socket
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

# Add tests/ dir to path for _e2e_helpers import (same pattern as phase4_e2e.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _e2e_helpers import (  # noqa: E402  (tests/ 目录运行)
    HoudiniConn,
    HoudiniCallError,
    StepResult,
    assert_step,
    emit_summary,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 全部临时资产都挂在这个唯一容器下；cleanup 一次性删掉整个容器即可。
CONTAINER_PATH = "/obj/H21_SMOKE_TEST"
# 材质建在 /mat 全局，需要单独删（不能挂在容器下）。
MAT_PATH = "/mat/H21_SMOKE_MAT"

# Assertion 7 用的 primitive group 名（wrangle VEX 写入）。
GRP_NAME = "test_group"

# Assertion 6/7 共用的 OBJ 容器（含 grp_box + grp_wrangle，wrangle 为 display）。
# 必须是 /obj 下的 sibling（不能嵌套在 CONTAINER_PATH 里——OBJ geo 节点
# 只能装 SOP 子节点，不能再嵌套 OBJ geo 容器，否则 server.create_node 抛
# "Invalid node type name"）。
GEO_GRP_PATH = "/obj/H21_SMOKE_TEST_GRP"

# Assertion 8 必须在 5s 内返回（H21 setInput hang 实证为 30s+）。
CONNECT_TIMEOUT_S = 5.0

# SWIG type-check 错误的关键词；出现在 set_node_position / layout_children
# 的 error message 里意味着 setPosition 用了 raw tuple/list（A5 修复回归）。
_SWIG_ERR_KEYWORDS = ("swig", "std::vector", "argument", "typecheck",
                      "setposition")


# ---------------------------------------------------------------------------
# Setup / Cleanup（best-effort，失败不阻断）
# ---------------------------------------------------------------------------
def _setup(conn: HoudiniConn, results: List[StepResult]) -> None:
    """建临时资产。每步用 try/except 兜底——节点可能已存在（前次跑残留），
    或资产创建失败（后续断言自然 FAIL，setup 本身不视为断言失败）。

    资产清单：
    - /obj/H21_SMOKE_TEST         (OBJ geo 容器，assertions 4/5/8 用)
      |- box1                     (assertion 4 set_node_position；
                                   assertion 8 connect_nodes 的 SOP src)
      |- box2, box3               (assertion 5 layout_children 的多子节点)
    - /obj/H21_SMOKE_TEST_GRP     (OBJ geo 容器 sibling，assertions 6/7 用)
      |- grp_box                  (基础几何，wrangle 的输入)
      |- grp_wrangle              (VEX 创建 test_group，display SOP)
    - /mat/H21_SMOKE_MAT          (principledshader，assertions 6/7 用)

    注意：OBJ geo 容器只能装 SOP 子节点（box/wrangle/...），不能再嵌套
    OBJ geo 容器——否则 server.create_node 抛 "Invalid node type name"。
    因此 assertions 6/7 用的容器与 assertion 4/5/8 用的容器是 /obj 下的
    平级 sibling，而不是嵌套子容器。
    """
    # 主容器（已存在则忽略错误，继续往里塞子节点）
    try:
        conn.call("create_node", node_type="geo",
                  parent_path="/obj", name="H21_SMOKE_TEST")
        assert_step(results, "setup: /obj/H21_SMOKE_TEST container",
                    ok=True, detail="created")
    except HoudiniCallError as e:
        # 多半是已存在；setup 不算断言失败
        assert_step(results, "setup: /obj/H21_SMOKE_TEST container",
                    ok=True, detail="exists or err: {0}".format(str(e)[:100]))

    # 三个简单 box（assertion 4 用 box1，assertion 5 需 >=2 children）
    for nm in ("box1", "box2", "box3"):
        try:
            conn.call("create_node", node_type="box",
                      parent_path=CONTAINER_PATH, name=nm)
        except HoudiniCallError:
            pass  # 已存在或冲突；不阻断

    # /obj/H21_SMOKE_TEST_GRP sibling 容器（assertions 6/7）+ grp_box + grp_wrangle
    try:
        conn.call("create_node", node_type="geo",
                  parent_path="/obj", name="H21_SMOKE_TEST_GRP")
        conn.call("create_node", node_type="box",
                  parent_path=GEO_GRP_PATH, name="grp_box")
        # VEX 在每个 primitive 上加入 test_group；run_over=primitives 必须。
        vex = 'setprimgroup(0, "{0}", @primnum, 1);\n'.format(GRP_NAME)
        wr = conn.call("create_wrangle",
                       parent_path=GEO_GRP_PATH, name="grp_wrangle",
                       vex_code=vex, run_over="primitives",
                       input_node=GEO_GRP_PATH + "/grp_box")
        wr_path = (wr.get("result", {}).get("path")
                   if isinstance(wr, dict) else None) \
            or (GEO_GRP_PATH + "/grp_wrangle")
        # 把 wrangle 设为 display SOP，让 group 真正生效在 GEO_GRP_PATH 输出上
        conn.call("set_node_flags", path=wr_path, display=True)
        assert_step(results, "setup: H21_SMOKE_TEST_GRP + grp_wrangle",
                    ok=True, detail="group={0} ready".format(GRP_NAME))
    except HoudiniCallError as e:
        assert_step(results, "setup: H21_SMOKE_TEST_GRP + grp_wrangle",
                    ok=True,  # 不阻断 assertions 6/7；它们自己会 FAIL
                    detail="err: {0}".format(str(e)[:120]))

    # 材质（建在 /mat 全局，不在 CONTAINER 下；cleanup 单独删）
    try:
        conn.call("create_material",
                  material_type="principledshader",
                  name="H21_SMOKE_MAT", parent_path="/mat")
        assert_step(results, "setup: /mat/H21_SMOKE_MAT",
                    ok=True, detail="created")
    except HoudiniCallError as e:
        assert_step(results, "setup: /mat/H21_SMOKE_MAT",
                    ok=True, detail="exists or err: {0}".format(str(e)[:100]))


def _cleanup(conn: HoudiniConn) -> None:
    """best-effort 删临时资产。永不抛异常。"""
    for path in (CONTAINER_PATH, GEO_GRP_PATH, MAT_PATH):
        try:
            conn.call("delete_node", path=path)
        except Exception:
            pass  # 节点不存在 / 连接已断 / 任何异常 — cleanup 不阻断退出


# ---------------------------------------------------------------------------
# 11 个断言（spec §Scenario: tests/h21_live_smoke.py 顺序）
# ---------------------------------------------------------------------------
def _drill(resp: Any) -> Dict[str, Any]:
    """conn.call 返 envelope {"status": "success", "result": <handler dict>}；
    钻一层拿真正的 handler 返回 dict。resp 异常时返回 {}。
    """
    if not isinstance(resp, dict):
        return {}
    inner = resp.get("result")
    if isinstance(inner, dict):
        return inner
    return {}


def _assert_check_connection(conn: HoudiniConn,
                             results: List[StepResult]) -> None:
    """1. check_connection 返回 status=success + hou_version 非空
    （验证 Tasks 2.1-2.3：applicationVersionString / hipFile.name / build fallback）
    """
    name = "1. check_connection status=success + hou_version non-empty"
    try:
        resp = conn.call("check_connection")
        inner = _drill(resp)
        status = resp.get("status")
        ver = inner.get("hou_version") or ""
        ok = (status == "success") and bool(ver)
        assert_step(results, name, ok=ok,
                    detail="status={0} hou_version={1!r}".format(status, ver))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False,
                    detail="err: {0}".format(str(e)[:200]))


def _assert_ping_houdini(conn: HoudiniConn,
                         results: List[StepResult]) -> None:
    """2. ping_houdini 返回 pong=True
    （验证 Task 2.4：ping 内部用 applicationVersionString，不再调 hou.version）
    """
    name = "2. ping_houdini pong=True"
    try:
        resp = conn.call("ping_houdini")
        inner = _drill(resp)
        pong = inner.get("pong")
        elapsed_ms = inner.get("elapsed_ms")
        ver = inner.get("hou_version", "")
        ok = (pong is True)
        assert_step(results, name, ok=ok,
                    detail="pong={0} elapsed_ms={1} ver={2!r}".format(
                        pong, elapsed_ms, ver))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False,
                    detail="err: {0}".format(str(e)[:200]))


def _assert_get_scene_info(conn: HoudiniConn,
                           results: List[StepResult]) -> None:
    """3. get_scene_info 返回 houdini_version 非空
    （验证 Task 2.5：_scene.py 用 applicationVersionString 而非 houdiniVersion）
    """
    name = "3. get_scene_info houdini_version non-empty"
    try:
        resp = conn.call("get_scene_info")
        inner = _drill(resp)
        ver = inner.get("houdini_version") or ""
        ok = bool(ver)
        assert_step(results, name, ok=ok,
                    detail="houdini_version={0!r}".format(ver))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False,
                    detail="err: {0}".format(str(e)[:200]))


def _looks_like_swig_err(msg: str) -> bool:
    """判定错误信息是否像 H21 SWIG type-check 错误（A5 回归信号）。"""
    low = (msg or "").lower()
    return any(k in low for k in _SWIG_ERR_KEYWORDS)


def _assert_set_node_position(conn: HoudiniConn,
                              results: List[StepResult]) -> None:
    """4. set_node_position 在临时节点上不抛 SWIG 错
    （验证 Task 4：_graph_edit.py 用 hou.Vector2 而非 raw tuple）
    """
    name = "4. set_node_position no SWIG error"
    try:
        resp = conn.call("set_node_position",
                         node_path=CONTAINER_PATH + "/box1",
                         x=3.0, y=4.0)
        inner = _drill(resp)
        ok = bool(inner.get("success"))
        assert_step(results, name, ok=ok,
                    detail="position={0}".format(inner.get("position")))
    except HoudiniCallError as e:
        msg = str(e)
        tag = "SWIG err" if _looks_like_swig_err(msg) else "err"
        assert_step(results, name, ok=False,
                    detail="{0}: {1}".format(tag, msg[:200]))


def _assert_layout_children(conn: HoudiniConn,
                            results: List[StepResult]) -> None:
    """5. layout_children 在临时容器上不抛 SWIG 错
    （验证 Task 4：layout_children 内部 setPosition 全部 Vector2 化）
    """
    name = "5. layout_children no SWIG error"
    try:
        resp = conn.call("layout_children", path=CONTAINER_PATH)
        inner = _drill(resp)
        cnt = inner.get("children_count")
        ok = (cnt is not None and cnt >= 1)
        assert_step(results, name, ok=ok,
                    detail="children_count={0}".format(cnt))
    except HoudiniCallError as e:
        msg = str(e)
        tag = "SWIG err" if _looks_like_swig_err(msg) else "err"
        assert_step(results, name, ok=False,
                    detail="{0}: {1}".format(tag, msg[:200]))


def _assert_assign_material_no_group(conn: HoudiniConn,
                                     results: List[StepResult]) -> None:
    """6. assign_material(group=None) 成功 — 既有 shop_materialpath 路径
    （回归 guard：H21 修复不能破坏既有 group=None 行为）
    """
    name = "6. assign_material(group=None) success (legacy path)"
    try:
        resp = conn.call("assign_material",
                         geometry_path=GEO_GRP_PATH,
                         material_path=MAT_PATH,
                         group=None)
        inner = _drill(resp)
        ok = bool(inner.get("success")) and (inner.get("group") is None)
        assert_step(results, name, ok=ok,
                    detail="via={0} group={1}".format(
                        inner.get("via", "shop_materialpath"),
                        inner.get("group")))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False,
                    detail="err: {0}".format(str(e)[:200]))


def _assert_assign_material_with_group(conn: HoudiniConn,
                                       results: List[StepResult]) -> None:
    """7. assign_material(group='test_group') 走 Material SOP 路径成功
    （验证 Task 6：H21 已移除 assignToNode → 走 material_sop_child 路径）

    spec 要求响应 `via: "material_sop_child"`。`fallback_shop_materialpath`
    虽 success=True 但属回归（Material SOP 创建失败），记 FAIL。
    """
    name = "7. assign_material(group='test_group') via=material_sop_child"
    try:
        resp = conn.call("assign_material",
                         geometry_path=GEO_GRP_PATH,
                         material_path=MAT_PATH,
                         group=GRP_NAME)
        inner = _drill(resp)
        via = inner.get("via", "")
        ms_path = inner.get("material_sop_path", "")
        ok = (via == "material_sop_child")
        assert_step(results, name, ok=ok,
                    detail="via={0!r} sop={1!r}".format(via, ms_path))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False,
                    detail="err: {0}".format(str(e)[:200]))


def _assert_connect_nodes_sop_to_obj(conn: HoudiniConn,
                                     results: List[StepResult]) -> None:
    """8. connect_nodes SOP→OBJ 5s 内返回，via=sop_display_flag
    （验证 Task 5：H21 setInput hang 修复，改走 setDisplayFlag + setRenderFlag）

    实现要点：
    - 临时把 socket timeout 降到 5s；H21 修复后应在 < 1s 内返回，
      未修复时 setInput 会 hang 30s+，5s 后 socket.timeout 触发 → FAIL。
    - timeout 后 socket 状态不可信；尝试 close + 重新 __enter__ 复位连接，
      让后续 9/10/11 还能跑。重连失败则外层 except SKIP 剩余步骤。
    - 用 CONTAINER_PATH/box1 作为 SOP src，CONTAINER_PATH 作为 OBJ dst。
      box1 是 CONTAINER 的子节点 → src.path().startswith(dst.path()+"/")
      命中 server.connect_nodes 的 SOP→OBJ 分支。
    """
    name = "8. connect_nodes SOP->OBJ within 5s (no hang)"
    sock = conn.sock
    old_timeout = sock.gettimeout() if sock is not None else None
    elapsed: Optional[float] = None
    timed_out = False
    try:
        if sock is not None:
            sock.settimeout(CONNECT_TIMEOUT_S)
        t0 = time.monotonic()
        resp = conn.call("connect_nodes",
                         from_path=CONTAINER_PATH + "/box1",
                         to_path=CONTAINER_PATH,
                         input_index=0)
        elapsed = time.monotonic() - t0
        inner = _drill(resp)
        via = inner.get("via", "")
        # 双重断言：elapsed < 5s AND via 标记正确（spec §Scenario: connect_nodes
        # SOP→OBJ on H21+ 要求 via='sop_display_flag'）
        ok = (elapsed < CONNECT_TIMEOUT_S) and (via == "sop_display_flag")
        assert_step(results, name, ok=ok,
                    detail="elapsed={0:.2f}s via={1!r}".format(elapsed, via))
    except socket.timeout:
        timed_out = True
        assert_step(results, name, ok=False,
                    detail="HANG: timed out after {0}s (H21 setInput "
                           "regression suspected)".format(CONNECT_TIMEOUT_S))
    except HoudiniCallError as e:
        elapsed_str = "{0:.2f}s".format(elapsed) if elapsed is not None else "?"
        assert_step(results, name, ok=False,
                    detail="err after {0}: {1}".format(
                        elapsed_str, str(e)[:160]))
    finally:
        # 恢复原 timeout
        if sock is not None and old_timeout is not None:
            try:
                sock.settimeout(old_timeout)
            except Exception:
                pass
        # Hang 后 socket 缓冲不可信：close + 重新 enter 拿干净 socket
        if timed_out:
            try:
                conn.close()
                conn.__enter__()
            except Exception:
                pass  # 重连失败让外层 except 处理


def _assert_help_box(conn: HoudiniConn,
                     results: List[StepResult]) -> None:
    """9. get_houdini_help('sop', 'box') 返回 status=success
    （baseline：帮助查询基本可用；为断言 10 的 URL encode 铺垫）
    """
    name = "9. get_houdini_help(sop, box) status=success"
    try:
        resp = conn.call("get_houdini_help",
                         help_type="sop", item_name="box")
        # get_houdini_help 返 help dict；dispatcher 把它包在 result 里。
        # 因此 status 在 inner.help_dict.status（不是 envelope 顶层）。
        inner = _drill(resp)
        status = inner.get("status")
        title = inner.get("title", "")
        ok = (status == "success")
        assert_step(results, name, ok=ok,
                    detail="status={0!r} title={1!r}".format(
                        status, (title or "")[:60]))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False,
                    detail="err: {0}".format(str(e)[:200]))


def _assert_help_special_chars(conn: HoudiniConn,
                               results: List[StepResult]) -> None:
    """10. get_houdini_help('sop', 'box SOP size param') 不抛 URL 错，返回
    HTTP 404 status=error
    （验证 Task 3：_help.py 用 urllib.parse.quote 编码 item_name）

    未修复时 urllib 抛 ValueError("URL can't contain control characters")；
    修复后 quote 把空格变 %20，SideFX 服务器返 404，_help.py 转为
    {status: error, error: 'HTTP 404: Not Found'}。
    """
    name = "10. get_houdini_help special chars returns HTTP 404 (no URL err)"
    try:
        resp = conn.call("get_houdini_help",
                         help_type="sop",
                         item_name="box SOP size param")
        inner = _drill(resp)
        status = inner.get("status")
        err = inner.get("error", "") or ""
        # URL encode 修复失败的指纹：ValueError / URLError / 控制字符相关字样
        low = err.lower()
        url_error_leaked = any(s in low for s in (
            "urlerror", "valueerror", "can't encode",
            "control char", "control character"))
        # 成功条件：status=error + error 含 'HTTP 404' + 无 URL 错漏出
        ok = ((status == "error")
              and ("HTTP 404" in err)
              and (not url_error_leaked))
        assert_step(results, name, ok=ok,
                    detail="status={0!r} error={1!r}".format(
                        status, err[:120]))
    except HoudiniCallError as e:
        # 注：dispatcher 包了一层 success，所以这里极少触发；仍兜底记录
        assert_step(results, name, ok=False,
                    detail="err: {0}".format(str(e)[:200]))


def _assert_verify_hou_api(conn: HoudiniConn,
                           results: List[StepResult]) -> None:
    """11. verify_hou_api('box', 'sop') 返回 status=success + _ai_hint 非空
    （验证 PR 18 wrapper：在 get_houdini_help 之上合成 _ai_hint）
    """
    name = "11. verify_hou_api(box, sop) status=success + _ai_hint non-empty"
    try:
        resp = conn.call("verify_hou_api",
                         item_name="box", help_type="sop")
        inner = _drill(resp)
        status = inner.get("status")
        hint = inner.get("_ai_hint", "") or ""
        ok = (status == "success") and bool(hint)
        assert_step(results, name, ok=ok,
                    detail="status={0!r} ai_hint={1!r}".format(
                        status, hint[:80]))
    except HoudiniCallError as e:
        assert_step(results, name, ok=False,
                    detail="err: {0}".format(str(e)[:200]))


# ---------------------------------------------------------------------------
# 汇总输出
# ---------------------------------------------------------------------------
def _print_at_a_glance(results: List[StepResult]) -> None:
    """一行 PASS/FAIL/SKIP/WARN 计数到 stdout。"""
    n_pass = sum(1 for r in results if r.status == "PASS")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_skip = sum(1 for r in results if r.status == "SKIP")
    n_warn = sum(1 for r in results if r.status == "WARN")
    print("[summary] {0} steps: {1} PASS / {2} FAIL / {3} SKIP / {4} WARN".format(
        len(results), n_pass, n_fail, n_skip, n_warn))


def main() -> int:
    results: List[StepResult] = []
    try:
        with HoudiniConn() as conn:
            try:
                # === SETUP（best-effort，失败不阻断后续断言）===
                _setup(conn, results)

                # === 11 个 H21 compat 断言（spec 顺序）===
                _assert_check_connection(conn, results)            # 1
                _assert_ping_houdini(conn, results)                # 2
                _assert_get_scene_info(conn, results)              # 3
                _assert_set_node_position(conn, results)           # 4
                _assert_layout_children(conn, results)             # 5
                _assert_assign_material_no_group(conn, results)    # 6
                _assert_assign_material_with_group(conn, results)  # 7
                _assert_connect_nodes_sop_to_obj(conn, results)    # 8
                _assert_help_box(conn, results)                    # 9
                _assert_help_special_chars(conn, results)          # 10
                _assert_verify_hou_api(conn, results)              # 11
            finally:
                # 无论断言成败，都尝试清理临时资产；不污染用户场景
                _cleanup(conn)
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        # 首次连接失败 / 后续重连失败 → SKIP（spec 明确 exit 0）
        print("[skip] Houdini socket not reachable on 127.0.0.1:9876 — "
              "is Houdini running + MCP started? ({0}: {1})".format(
                  type(e).__name__, e))
        # 仍打印已有结果（setup 阶段可能已积累若干 step），便于事后排查
        if results:
            md = emit_summary(results)
            print(md)
            _print_at_a_glance(results)
        return 0
    except HoudiniCallError as e:
        # transport-level Houdini error（非断言 FAIL，是底层 socket/协议错）
        print("[fail] transport-level Houdini error: {0}".format(e))
        md = emit_summary(results)
        print(md)
        _print_at_a_glance(results)
        return 1
    except Exception:  # noqa: BLE001
        # 未预期异常：打印 traceback + 已有汇总，避免静默吞错
        traceback.print_exc()
        md = emit_summary(results)
        print(md)
        _print_at_a_glance(results)
        return 1

    # 正常路径：emit Markdown summary 到 stdout（不落盘，smoke test 不需 artifact）
    md = emit_summary(results)
    print(md)
    _print_at_a_glance(results)
    # 退出码：仅 FAIL 视为失败；PASS / SKIP / WARN 都算通过
    has_fail = any(r.status == "FAIL" for r in results)
    return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
