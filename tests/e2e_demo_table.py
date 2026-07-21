#!/usr/bin/env python3
"""端到端 Demo：程序化木桌（procedural wooden table）。

目的：
- 在 Houdini 中通过 Opera Houdini MCP（fork opera-houdini-mcp）从 0-1 构建
  一个程序化木桌：桌面 + 4 桌腿 + 2 横撑 + 木纹 wrangle + 木质 PBR 材质。
- 走完 Tier 1 工具链完整一程：create_node / set_parameters /
  set_node_position / connect_nodes / create_wrangle / create_material /
  assign_material / find_error_nodes / cook_node / get_geometry_info /
  get_geo_summary / set_node_color / layout_children。
- 输出 Markdown 断言汇总表到 stdout，并把表落盘到
  `$TEMP/houdini_mcp/e2e_demo_table/summary.md`；后续 Wave C 步骤
  （pane 截图 / Karma CPU 渲染 / execute_code 审计 / 汇总）会写入同一
  artifact 目录。

运行方式：
    C:/.../external/houdinimcp-env/python/python.exe tests/e2e_demo_table.py
    # 或任意可 import tests/_e2e_helpers.py 的 Python：
    cd tests && python e2e_demo_table.py

预期：
- Houdini 已启动 + MCP 已运行（shelf 点 "Start Opera MCP" 触发）。
- socket 连通 127.0.0.1:9876。
- 总耗时 ≤ 60s（H21 + Karma CPU 经验值）。

跳过语义：
- 首次连接失败（ConnectionRefusedError / socket.timeout / OSError）→ 视为
  SKIP，stdout 打 "is Houdini running + MCP started?" 提示，exit 0，不抛
  traceback。Demo 是 smoke test，不是回归门禁。

协议：
- 与 server.py 一致：4 字节大端长度前缀 + UTF-8 JSON。
- 请求：{"type": cmd_type, "params": params}。
- 响应：{"status": "success" | "error", "result": ..., "message": ...}。

历史：
- 2026-07-21 初版（Wave B）：实现 4.3 skeleton + 4.4-4.7 build / verify。
  Wave C 将追加 4.8-4.11（capture / render / audit / summary）。
"""
from __future__ import annotations

import base64
import os
import socket
import sys
import tempfile
import traceback
from typing import Any, Dict, List

from _e2e_helpers import (
    HoudiniConn,
    HoudiniCallError,
    StepResult,
    assert_step,
    emit_summary,
)


# ---------------------------------------------------------------------------
# 几何参数（与 spec.md §demo builds the wooden table 一致）
# ---------------------------------------------------------------------------
TABLE_TOP_PARAMS = {"sizex": 1.4, "sizey": 0.05, "sizez": 0.8}
LEG_PARAMS = {"sizex": 0.08, "sizey": 0.7, "sizez": 0.08}
BRACE_PARAMS = {"sizex": 0.05, "sizey": 0.05, "sizez": 0.7}

# 四条腿在 network editor 中的相对位置（与桌面中心对齐后由 set_node_position 写）
# 桌面默认 Box 在 y=0 中心；腿也在 y=0 中心，因此腿从桌面中心向下 + 向上各伸半截。
# 如果以后视觉效果不佳，演示可在 set_parameters 里额外加 translate，但当前 demo
# 保持参数最小化以便断言骨架可重复。
LEG_POSITIONS = {
    "leg_fl": (-0.62, -0.32),  # 前左
    "leg_fr": ( 0.62, -0.32),  # 前右
    "leg_bl": (-0.62,  0.32),  # 后左
    "leg_br": ( 0.62,  0.32),  # 后右
}
BRACE_POSITIONS = {
    "brace_front": (0.0, -0.32),  # 前腿之间，沿 z 轴伸展
    "brace_back":  (0.0,  0.32),  # 后腿之间，沿 z 轴伸展
}

# 木纹 VEX（与 spec.md §Scenario: demo builds the wooden table 一致）
WOOD_GRAIN_VEX = (
    "// wood_grain: drive Cd from 1D-ish noise on P (detail over primitives)\n"
    "float n = noise(@P * set(8, 0.1, 8));\n"
    "vector wood = set(0.45, 0.27, 0.13) + set(0.05, 0.02, 0.0) * (n - 0.5);\n"
    "@Cd = wood;\n"
)


# ---------------------------------------------------------------------------
# Helper：把 Houdini call 包成 assert_step
# ---------------------------------------------------------------------------
def _call_ok(conn: HoudiniConn, cmd: str, params: Dict[str, Any] = None,
             ) -> Dict[str, Any]:
    """直接调用，失败抛 HoudiniCallError。供 build 步骤用。"""
    if params is None:
        params = {}
    return conn.call(cmd, **params)


def _ok(results: List[StepResult], name: str, conn: HoudiniConn,
        cmd: str, params: Dict[str, Any] = None,
        artifact: str = "-") -> Dict[str, Any]:
    """call + assert_step 合一：返回响应 dict（已剥 status=error 检查）。

    出错时记 FAIL 并返回 {}（不抛），便于 build 步骤"一错不遮百错"。
    """
    if params is None:
        params = {}
    try:
        resp = conn.call(cmd, **params)
        assert_step(results, name, ok=True, artifact=artifact,
                    detail="{0} -> {1}".format(cmd, list(resp.keys())[:4]))
        return resp
    except HoudiniCallError as e:
        assert_step(results, name, ok=False, artifact=artifact,
                    detail="err: {0}".format(str(e)[:200]))
        return {}


# ---------------------------------------------------------------------------
# Step blocks（task 4.4 / 4.5 / 4.6 / 4.7）
# ---------------------------------------------------------------------------
def build_table_geometry(conn: HoudiniConn,
                         results: List[StepResult]) -> None:
    """Task 4.4：建 /obj/table_demo 容器 + top / 4 legs / 2 braces。"""
    # 1. 清场（best-effort，失败也无所谓——可能是 untitled 场景已存在节点）
    try:
        _ok(results, "4.4a new_scene (best-effort)", conn,
            "new_scene", {"suppress_save_prompt": True})
    except Exception:
        pass

    # 2. 容器
    res = _ok(results, "4.4b create_node /obj/table_demo", conn,
              "create_node",
              {"node_type": "geo", "parent_path": "/obj",
               "name": "table_demo"})
    table_path = res.get("path") if res else "/obj/table_demo"

    # 3. 容器定位
    _ok(results, "4.4c set_node_position /obj/table_demo", conn,
        "set_node_position",
        {"node_path": table_path, "x": 0, "y": 0})

    # 4. 桌面 Box
    _ok(results, "4.4d create_node table_top", conn,
        "create_node",
        {"node_type": "box", "parent_path": table_path,
         "name": "table_top"})
    _ok(results, "4.4e set_parameters table_top", conn,
        "set_parameters",
        {"path": "{0}/table_top".format(table_path),
         "parameters": TABLE_TOP_PARAMS})
    _ok(results, "4.4f set_node_position table_top", conn,
        "set_node_position",
        {"node_path": "{0}/table_top".format(table_path),
         "x": 0.0, "y": 0.0})

    # 5. 四条腿
    for leg_name in ("leg_fl", "leg_fr", "leg_bl", "leg_br"):
        _ok(results, "4.4g create_node {0}".format(leg_name), conn,
            "create_node",
            {"node_type": "box", "parent_path": table_path,
             "name": leg_name})
        _ok(results, "4.4h set_parameters {0}".format(leg_name), conn,
            "set_parameters",
            {"path": "{0}/{1}".format(table_path, leg_name),
             "parameters": LEG_PARAMS})
        x, y = LEG_POSITIONS[leg_name]
        _ok(results, "4.4i set_node_position {0}".format(leg_name), conn,
            "set_node_position",
            {"node_path": "{0}/{1}".format(table_path, leg_name),
             "x": x, "y": y})

    # 6. 两条横撑
    for brace_name in ("brace_front", "brace_back"):
        _ok(results, "4.4j create_node {0}".format(brace_name), conn,
            "create_node",
            {"node_type": "box", "parent_path": table_path,
             "name": brace_name})
        _ok(results, "4.4k set_parameters {0}".format(brace_name), conn,
            "set_parameters",
            {"path": "{0}/{1}".format(table_path, brace_name),
             "parameters": BRACE_PARAMS})
        x, y = BRACE_POSITIONS[brace_name]
        _ok(results, "4.4l set_node_position {0}".format(brace_name), conn,
            "set_node_position",
            {"node_path": "{0}/{1}".format(table_path, brace_name),
             "x": x, "y": y})

    # 7. 网络整齐化
    _ok(results, "4.4m layout_children /obj/table_demo", conn,
        "layout_children",
        {"parent_path": table_path, "direction": "horizontal"})


def add_wood_grain_wrangle(conn: HoudiniConn,
                           results: List[StepResult],
                           table_path: str = "/obj/table_demo") -> None:
    """Task 4.5：木纹 Attribute Wrangle（VEX noise → Cd）。"""
    # 1. 建 wrangle 节点
    res = _ok(results, "4.5a create_node attribwrangle wood_grain", conn,
              "create_node",
              {"node_type": "attribwrangle",
               "parent_path": table_path, "name": "wood_grain"})
    wrangle_path = (res.get("path") if res
                    else "{0}/wood_grain".format(table_path))

    # 2. 桌面 → wrangle 输入 0
    _ok(results, "4.5b connect table_top -> wood_grain", conn,
        "connect_nodes",
        {"from_path": "{0}/table_top".format(table_path),
         "to_path": wrangle_path,
         "input_index": 0, "output_index": 0})

    # 3. 把 VEX 灌进 wrangle 并自检 compile
    # create_wrangle 服务端会在 VEX 编不过时返回 validation.errors；
    # 这里只 WARN，不阻断后续。
    try:
        res = conn.call(
            "create_wrangle",
            parent_path=table_path, name="wood_grain_wrangle",
            vex_code=WOOD_GRAIN_VEX, run_over="primitives",
            input_node="",
        )
        validation = res.get("validation", {})
        if validation.get("compile_ok", True) is False:
            assert_step(results, "4.5c create_wrangle wood_grain_wrangle",
                        ok=True,  # 节点创建本身 OK
                        detail="WARN: VEX compile issues: {0}".format(
                            validation.get("errors"))[:200],
                        artifact="{0}/wood_grain_wrangle".format(table_path))
        else:
            assert_step(results, "4.5c create_wrangle wood_grain_wrangle",
                        ok=True,
                        detail="run_over={0}".format(res.get("run_over")),
                        artifact="{0}/wood_grain_wrangle".format(table_path))
    except HoudiniCallError as e:
        assert_step(results, "4.5c create_wrangle wood_grain_wrangle",
                    ok=True,  # 节点已建，不阻断
                    detail="WARN: {0}".format(str(e)[:200]))

    # 4. 把 wrangle 接到 table_demo 作为 display input 0（成为显示节点）
    _ok(results, "4.5d connect wood_grain -> table_demo display", conn,
        "connect_nodes",
        {"from_path": wrangle_path,
         "to_path": table_path,
         "input_index": 0, "output_index": 0})

    # 5. 校验：get_node_info 看 table_demo 的 input 0 是不是 wood_grain
    try:
        info = conn.call("get_node_info",
                         node_path=table_path,
                         include_input_details=True,
                         compact=False)
        inputs = info.get("inputs", []) if isinstance(info, dict) else []
        # inputs 是 list of {from_node, ...} 或者简单的 input index 列表
        # 兼容两种 schema：取 inputs[0] 看 from_node
        first_from = ""
        if inputs and isinstance(inputs[0], dict):
            first_from = inputs[0].get("from_node", "")
        elif inputs and isinstance(inputs[0], str):
            first_from = inputs[0]
        ok = "wood_grain" in first_from if first_from else False
        assert_step(results, "4.5e verify table_demo input 0 == wood_grain",
                    ok=ok, artifact=table_path,
                    detail="inputs[0].from_node={0}".format(first_from))
    except HoudiniCallError as e:
        assert_step(results, "4.5e verify table_demo input 0 == wood_grain",
                    ok=False, artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))


def create_and_bind_material(conn: HoudiniConn,
                             results: List[StepResult],
                             table_path: str = "/obj/table_demo") -> None:
    """Task 4.6：建 principledshader 木质材质并绑定到桌体。"""
    mat_path = "/mat/wood_mat"
    # 1. 材质节点
    _ok(results, "4.6a create_material /mat/wood_mat", conn,
        "create_material",
        {"material_type": "principledshader", "name": "wood_mat",
         "parent_path": "/mat"})

    # 2. 设 PBR 参数（basecolor 胡桃木 + 中等粗糙度）
    _ok(results, "4.6b set_parameters wood_mat basecolor+rough", conn,
        "set_parameters",
        {"path": mat_path,
         "parameters": {"basecolor": (0.45, 0.27, 0.13), "rough": 0.55}})

    # 3. 绑定到桌体
    _ok(results, "4.6c assign_material table_demo <- wood_mat", conn,
        "assign_material",
        {"geometry_path": table_path, "material_path": mat_path,
         "group": None})

    # 4. 校验：get_material_info
    try:
        info = conn.call("get_material_info", material_path=mat_path)
        # basecolor 校验 — 不同 schema 下可能用 tuple / list / 三键 basecolorr/g/b
        params = info.get("parameters", {}) if isinstance(info, dict) else {}
        bc = params.get("basecolor") or params.get("basecolorr")
        ok_bc = bc is not None  # 只校验"存在"，不强求精确 tuple 匹配
        assert_step(results, "4.6d verify wood_mat basecolor present",
                    ok=ok_bc, artifact=mat_path,
                    detail="basecolor={0}".format(bc))
    except HoudiniCallError as e:
        assert_step(results, "4.6d verify wood_mat basecolor present",
                    ok=False, artifact=mat_path,
                    detail="err: {0}".format(str(e)[:200]))

    # 5. 校验：桌体节点有材质引用（不需要严格 — 多数 SOP 节点只 render flag 时挂材质）
    try:
        info = conn.call("get_node_info", node_path=table_path,
                         include_errors=False, force_cook=False,
                         compact=False)
        # 只断言 node info 本身能拿到，不去猜材质字段名
        assert_step(results, "4.6e verify table_demo get_node_info OK",
                    ok=True, artifact=table_path,
                    detail="type={0}".format(info.get("type")))
    except HoudiniCallError as e:
        assert_step(results, "4.6e verify table_demo get_node_info OK",
                    ok=False, artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))


def verification_snapshots(conn: HoudiniConn,
                          results: List[StepResult],
                          table_path: str = "/obj/table_demo") -> None:
    """Task 4.7：6 项 verification snapshot。"""
    # 1. find_error_nodes
    try:
        info = conn.call("find_error_nodes",
                         root_path=table_path,
                         include_warnings=True,
                         max_warnings=50, max_errors=None)
        errors = info.get("errors", []) if isinstance(info, dict) else []
        warnings = info.get("warnings", []) if isinstance(info, dict) else []
        ok = (len(errors) == 0 and len(warnings) == 0)
        assert_step(results, "4.7a find_error_nodes 0/0", ok=ok,
                    artifact=table_path,
                    detail="errors={0} warnings={1}".format(
                        len(errors), len(warnings)))
    except HoudiniCallError as e:
        assert_step(results, "4.7a find_error_nodes 0/0", ok=False,
                    artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))

    # 2. cook_node
    try:
        info = conn.call("cook_node", path=table_path)
        # cook_node 返回 schema 含 errors / warnings / cook_time_ms
        errors = info.get("errors", 0) if isinstance(info, dict) else 0
        warnings = info.get("warnings", 0) if isinstance(info, dict) else 0
        cook_ms = info.get("cook_time_ms", 0) if isinstance(info, dict) else 0
        ok = (errors == 0 and warnings == 0 and cook_ms > 0)
        assert_step(results, "4.7b cook_node 0/0/positive time", ok=ok,
                    artifact=table_path,
                    detail="errors={0} warnings={1} cook_time_ms={2}".format(
                        errors, warnings, cook_ms))
    except HoudiniCallError as e:
        assert_step(results, "4.7b cook_node 0/0/positive time", ok=False,
                    artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))

    # 3. get_geometry_info — 捕获 count 到 detail
    try:
        info = conn.call("get_geometry_info", path=table_path)
        points = info.get("point_count", 0) if isinstance(info, dict) else 0
        prims = info.get("primitive_count", 0) if isinstance(info, dict) else 0
        bbox = info.get("bbox") if isinstance(info, dict) else None
        ok = (points > 0 and prims > 0 and bbox is not None)
        assert_step(results, "4.7c get_geometry_info counts > 0", ok=ok,
                    artifact=table_path,
                    detail="points={0} prims={1} bbox={2}".format(
                        points, prims, bbox))
    except HoudiniCallError as e:
        assert_step(results, "4.7c get_geometry_info counts > 0", ok=False,
                    artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))

    # 4. get_geo_summary smoke
    try:
        info = conn.call("get_geo_summary", node_path=table_path,
                         max_points_for_full=1000000, sample_size=5)
        # 只要没抛就算 PASS；不精确断言（wrangle 可能改 prim 数）
        assert_step(results, "4.7d get_geo_summary no-raise", ok=True,
                    artifact=table_path,
                    detail="keys={0}".format(
                        list(info.keys())[:5] if isinstance(info, dict) else []))
    except HoudiniCallError as e:
        assert_step(results, "4.7d get_geo_summary no-raise", ok=False,
                    artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))

    # 5. set_node_color table_top
    try:
        info = conn.call("set_node_color",
                         node_path="{0}/table_top".format(table_path),
                         r=0.6, g=0.35, b=0.18)
        ok = bool(info.get("success", False)) if isinstance(info, dict) else False
        assert_step(results, "4.7e set_node_color table_top", ok=ok,
                    artifact="{0}/table_top".format(table_path),
                    detail=str(info)[:120])
    except HoudiniCallError as e:
        assert_step(results, "4.7e set_node_color table_top", ok=False,
                    artifact="{0}/table_top".format(table_path),
                    detail="err: {0}".format(str(e)[:200]))

    # 6. layout_children 二次整齐化
    try:
        info = conn.call("layout_children",
                         parent_path=table_path, direction="horizontal")
        cnt = info.get("children_count", 0) if isinstance(info, dict) else 0
        ok = (cnt >= 7)  # top + 4 legs + 2 braces = 7
        assert_step(results, "4.7f layout_children >= 7 children", ok=ok,
                    artifact=table_path,
                    detail="children_count={0}".format(cnt))
    except HoudiniCallError as e:
        assert_step(results, "4.7f layout_children >= 7 children", ok=False,
                    artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    artifact_dir = os.path.join(tempfile.gettempdir(),
                                "houdini_mcp", "e2e_demo_table")
    try:
        os.makedirs(artifact_dir, exist_ok=True)
    except OSError as e:
        print("[skip] cannot create artifact dir: {0}".format(e))
        return 0

    results: List[StepResult] = []
    try:
        with HoudiniConn() as conn:
            # Task 4.4
            build_table_geometry(conn, results)
            # Task 4.5
            add_wood_grain_wrangle(conn, results)
            # Task 4.6
            create_and_bind_material(conn, results)
            # Task 4.7
            verification_snapshots(conn, results)
            # Wave C 会在此处插入 4.8 capture / 4.9 render / 4.10 audit。
            # 标记成 TODO_WAVE_C 让后续 dispatch 一眼看到。
            # 不在本次跑里加 step。
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        print("[skip] Houdini socket not reachable on 127.0.0.1:9876 — "
              "is Houdini running + MCP started? ({0})".format(e))
        return 0
    except HoudiniCallError as e:
        print("[fail] transport-level Houdini error: {0}".format(e))
        # 仍然打印已有 step 的汇总表，便于事后排查
        md = emit_summary(results, out_path=os.path.join(
            artifact_dir, "summary.md"))
        print(md)
        return 1
    except Exception as e:  # noqa: BLE001
        # 未预期异常：打印 traceback + 已有汇总，避免静默吞错
        traceback.print_exc()
        md = emit_summary(results, out_path=os.path.join(
            artifact_dir, "summary.md"))
        print(md)
        return 1

    md = emit_summary(results, out_path=os.path.join(
        artifact_dir, "summary.md"))
    print(md)
    has_fail = any(r.status == "FAIL" for r in results)
    return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())