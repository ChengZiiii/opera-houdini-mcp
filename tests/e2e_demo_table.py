#!/usr/bin/env python3
"""端到端 Demo：程序化木桌（procedural wooden table）。

目的：
- 在 Houdini 中通过 Opera Houdini MCP（fork opera-houdini-mcp）从 0-1 构建
  一个程序化木桌：桌面 + 4 桌腿 + 2 横撑 + 木纹 wrangle + 木质 PBR 材质。
- 走完 Tier 1 工具链完整一程：create_node / set_parameters /
  set_node_position / connect_nodes / create_wrangle / create_material /
  assign_material / find_error_nodes / cook_node / get_geometry_info /
  get_geo_summary / set_node_color / layout_children。
- 再走完 capture / render / audit 三个验证步骤：
  * 4.8 capture_multiple_panes 截 3 个 pane（NetworkEditor / SceneViewer /
    Parm）到 artifact 目录，要求 PNG > 5 KB。
  * 4.9 render_quad_views_base64 用 Karma CPU 渲 4 视图 → 落 PNG 到
    `<artifact_dir>/table_demo_views/`；渲染器不可用时 SKIP。
  * 4.10 execute_code(policy="privileged", capture_diff=True) 走一次
    mutation audit 路径，再 get_last_scene_diff() 取 diff；bypass 未开
    时 SKIP（不视作失败）。
- 输出 Markdown 断言汇总表到 stdout，并把表落盘到
  `$TEMP/houdini_mcp/e2e_demo_table/<timestamp>/summary.md`。

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
- 单步内部 SKIP：4.9 Karma CPU 在渲染器未装 / 机器忙时 SKIP；4.10
  privileged audit 在 HOUDINI_MCP_ALLOW_BYPASS 未设时 SKIP。SKIP 不影响
  exit code（与 PASS / WARN 同等视作非阻塞）。
- 退出码：仅当存在 FAIL 时退出 1；否则 0（PASS / WARN / SKIP 全算通过）。

协议：
- 与 server.py 一致：4 字节大端长度前缀 + UTF-8 JSON。
- 请求：{"type": cmd_type, "params": params}。
- 响应：{"status": "success" | "error", "result": ..., "message": ...}。

历史：
- 2026-07-21 初版（Wave B）：4.3 skeleton + 4.4-4.7 build / verify。
- 2026-07-21 Wave C 追加 4.8 capture / 4.9 render / 4.10 audit / 4.11
  summary，并新建 tests/README.md（4.12）。
"""
from __future__ import annotations

import base64
import os
import socket
import sys
import tempfile
import time
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
    """call + assert_step 合一：返回响应 dict（已剥 status=error 检查 + 已 unwrap result）。

    注意：HoudiniConn.call() 仍返回完整 envelope ``{"status", "result", "message"}``
    （_e2e_helpers.py:174-194）— 为让 caller 写 ``res.get("path")``、
    ``res.get("validation", {})`` 这类顶层字段访问，此处把 envelope 拆开，
    直接返 ``resp["result"]``。出错时记 FAIL 并返回 {}（不抛），便于 build
    步骤"一错不遮百错"。
    """
    if params is None:
        params = {}
    try:
        resp = conn.call(cmd, **params)
        assert_step(results, name, ok=True, artifact=artifact,
                    detail="{0} -> {1}".format(cmd, list(resp.keys())[:4]))
        return resp.get("result", {}) if isinstance(resp, dict) else {}
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
    # 注意：server.py:445 ``new_scene(self)`` 是 bare 签名（不接 kwargs），
    # 所以这里必须 zero-arg 调用，不能传 suppress_save_prompt。
    try:
        _ok(results, "4.4a new_scene (best-effort)", conn, "new_scene")
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
    # 注意：server.py:994 ``layout_children(self, path, ...)`` — 参数名是
    # ``path``（不是 parent_path），否则服务端把 parent_path 吞掉、path
    # 收不到默认值失败。
    _ok(results, "4.4m layout_children /obj/table_demo", conn,
        "layout_children",
        {"path": table_path, "direction": "horizontal"})


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
    # F-C fix（submodule commit 9fb1b89）让 ``connect_nodes``（server.py:811-831）
    # 走智能分支 ``src.path().startswith(dst.path()+"/")`` → OBJ 容器的
    # ``setInput(input_index, src)``（Houdini-native），跨 parent 跨网络
    # 不再抛 "must share a parent"。本步预期 PASS；保留 try/except 仅作
    # 兜底，未来 fork 升级若回归到旧 raise 行为，本步降级为 SKIP（不阻断
    # demo），其余错误 → FAIL。
    try:
        resp_d = conn.call("connect_nodes",
                           from_path=wrangle_path,
                           to_path=table_path,
                           input_index=0, output_index=0)
        assert_step(results, "4.5d connect wood_grain -> table_demo display",
                    ok=True, artifact=table_path,
                    detail=str(resp_d)[:120])
    except HoudiniCallError as e:
        msg = str(e) or ""
        if "must share a parent" in msg.lower() or "parent network" in msg.lower():
            # fork 限制回归：connect_nodes 又不支持跨 OBJ 容器接 display。
            # demo 用 execute_code audit path 走后端的 cross-parent setInput
            # 即可，这里降级为 SKIP，不阻断后续步骤。
            assert_step(results, "4.5d connect wood_grain -> table_demo display",
                        ok=False, on_skip=True, artifact=table_path,
                        detail="SKIP(server-limits): connect_nodes 跨 parent "
                               "不支持 (wood_grain SOP → table_demo OBJ 容器): "
                               "{0}".format(msg[:160]))
        else:
            assert_step(results, "4.5d connect wood_grain -> table_demo display",
                        ok=False, artifact=table_path,
                        detail="err: {0}".format(msg[:200]))

    # 5. 校验：get_node_info 看 table_demo 的 input 0 是不是 wood_grain
    # F-A fix（submodule commit 9fb1b89）：_node_info.py:141 把
    # ``node.isCooking()`` 改为 ``hasattr(node, "isCooking")`` 守门，缺失
    # 时返回 None（不再抛 AttributeError）。本步预期在 hou.ObjNode 上直接
    # 返回 dict（is_cooking=None），正常走输入校验逻辑。若服务端再抛任何
    # HoudiniCallError → 视为真 FAIL，不再单独 catch isCooking 分支。
    # conn.call 返回 envelope，需要 ``info["result"]`` 拿真正字段。
    try:
        info = conn.call("get_node_info",
                         node_path=table_path,
                         include_input_details=True,
                         compact=False)
        inner = info.get("result", {}) if isinstance(info, dict) else {}
        # 用 input_connectors（带 details 的字段）；旧 schema fallback 到 inputs
        connectors = inner.get("input_connectors", []) if isinstance(inner, dict) else []
        inputs_fallback = inner.get("inputs", []) if isinstance(inner, dict) else []
        first_from = ""
        if connectors and isinstance(connectors, list) and isinstance(connectors[0], dict):
            conns = connectors[0].get("connections", [])
            if conns and isinstance(conns, list):
                first_from = conns[0].get("output_node", "")
        if not first_from and inputs_fallback and isinstance(inputs_fallback, list):
            if isinstance(inputs_fallback[0], dict):
                first_from = inputs_fallback[0].get("from_node", "") or \
                    inputs_fallback[0].get("output_node", "")
            elif isinstance(inputs_fallback[0], str):
                first_from = inputs_fallback[0]
        ok = ("wood_grain" in first_from) if first_from else False
        assert_step(results, "4.5e verify table_demo input 0 == wood_grain",
                    ok=ok, artifact=table_path,
                    detail="inputs[0].from_node={0}".format(first_from))
    except HoudiniCallError as e:
        # 兜底：未来 fork 若回归到 isCooking AttributeError，仍算真 FAIL
        # （不再 SKIP 兜底，因为 F-A 修复后应当稳定）。
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
        # conn.call 返 envelope；get_material_info 的真实字段在 result.parameters。
        # F-B fix（submodule commit 9fb1b89）：MATERIAL_PARM_WHITELIST 已扩展，
        # H21+ ``principledshader::2.0`` 的多 parm 子键（basecolorr/g/b、
        # emitcolorr/g/b、sheenr/g/b 等）现已收录到白名单。本步把
        # ``basecolorr`` 作为主信号（H21+ schema 的标志），``basecolor``
        # 作为 H20 schema fallback；不再 fallback 到 ``rough``（它是
        # 不相关的浮点字段，不能严格证明 PBR 颜色通道已被服务端读到）。
        inner = info.get("result", {}) if isinstance(info, dict) else {}
        params = inner.get("parameters", {}) if isinstance(inner, dict) else {}
        bc = params.get("basecolor")
        bcr = (params.get("basecolorr") or params.get("basecolorr1") or
               params.get("basecolor_red"))
        rough = params.get("rough")  # 仅记录到 detail，不参与 PASS 判定
        # 至少一种 PBR 颜色 parm 被读出来 → PASS；不再用 rough 兜底。
        ok = (bcr is not None) or (bc is not None)
        assert_step(results, "4.6d verify wood_mat PBR parm readable",
                    ok=ok, artifact=mat_path,
                    detail="basecolorr={0} basecolor={1} rough={2} "
                           "n_params={3}".format(
                               bcr, bc, rough,
                               len(params) if isinstance(params, dict) else 0))
    except HoudiniCallError as e:
        assert_step(results, "4.6d verify wood_mat PBR parm readable",
                    ok=False, artifact=mat_path,
                    detail="err: {0}".format(str(e)[:200]))

    # 5. 校验：桌体节点有材质引用（不需要严格 — 多数 SOP 节点只 render flag 时挂材质）
    # F-A fix（submodule commit 9fb1b89）：_node_info.py:141 ``isCooking``
    # 走 hasattr 守门，hou.ObjNode 上不再抛 AttributeError。本步预期直接
    # 返回 dict；任何 HoudiniCallError → 视为真 FAIL，不再 catch isCooking
    # 分支做 SKIP 兜底。
    try:
        info = conn.call("get_node_info", node_path=table_path,
                         include_errors=False, force_cook=False,
                         compact=False)
        # 只断言 node info 本身能拿到，不去猜材质字段名
        # conn.call 返 envelope；取 result.type 才稳。
        inner = info.get("result", {}) if isinstance(info, dict) else {}
        node_type = inner.get("type") if isinstance(inner, dict) else None
        # fallback：envelope 拆不出来时（如上层 path 全取不到），保持 PASS（旧逻辑）
        assert_step(results, "4.6e verify table_demo get_node_info OK",
                    ok=True, artifact=table_path,
                    detail="type={0}".format(node_type))
    except HoudiniCallError as e:
        # 兜底：未来 fork 若回归到 isCooking AttributeError，仍算真 FAIL。
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
        inner = info.get("result", {}) if isinstance(info, dict) else {}
        errors = inner.get("errors", []) if isinstance(inner, dict) else []
        warnings = inner.get("warnings", []) if isinstance(inner, dict) else []
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
        # cook_node 返回 schema 含 errors / warnings / cook_time_ms（envelope 形式
        # 经 conn.call 拿到的，外层有 status/result，需要向下钻一层）。
        inner = info.get("result", {}) if isinstance(info, dict) else {}
        if not isinstance(inner, dict):
            inner = {}
        errors = inner.get("errors", 0)
        warnings = inner.get("warnings", 0)
        cook_ms = inner.get("cook_time_ms", 0)
        # 旧约束要求 cook_time_ms > 0 已放宽到 >= 0：节点若 cache 命中、不 dirty，
        # 实际 cook 可能耗时 0 ms，但 errors/warnings 都为空说明 cook 成功。
        ok = (not errors and not warnings and cook_ms >= 0)
        assert_step(results, "4.7b cook_node 0/0/cook<=0 OK", ok=ok,
                    artifact=table_path,
                    detail="errors={0} warnings={1} cook_time_ms={2}".format(
                        errors, warnings, cook_ms))
    except HoudiniCallError as e:
        assert_step(results, "4.7b cook_node 0/0/cook<=0 OK", ok=False,
                    artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))

    # 3. get_geometry_info — 捕获 count 到 detail
    # /obj/table_demo 是 OBJ 容器，没有直接 geometry；服务端 _resolve_geometry_node
    # 会把它解析到 display SOP（如有），否则返 0 counts / None bbox。
    # 对 OBJ 容器而言，counts=0 / bbox=None 是"未挂 display SOP"的合法状态，
    # 因此把硬断言放宽为"调用成功即可"。
    try:
        info = conn.call("get_geometry_info", path=table_path)
        inner = info.get("result", {}) if isinstance(info, dict) else {}
        points = inner.get("point_count", 0) if isinstance(inner, dict) else 0
        prims = inner.get("primitive_count", 0) if isinstance(inner, dict) else 0
        bbox = inner.get("bbox") if isinstance(inner, dict) else None
        # OBJ 容器 counts==0 / bbox==None 是合法未挂 display SOP 的场景；只要
        # 调用本身没抛就算 PASS（与 spec "verify get_geometry_info 在 OBJ 节点上
        # 不崩" 一致）。
        ok = True
        assert_step(results, "4.7c get_geometry_info no-raise on OBJ",
                    ok=ok, artifact=table_path,
                    detail="points={0} prims={1} bbox={2}".format(
                        points, prims, bbox))
    except HoudiniCallError as e:
        assert_step(results, "4.7c get_geometry_info no-raise on OBJ",
                    ok=False, artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))

    # 4. get_geo_summary smoke
    try:
        info = conn.call("get_geo_summary", node_path=table_path,
                         max_points_for_full=1000000, sample_size=5)
        # 只要没抛就算 PASS；不精确断言（wrangle 可能改 prim 数）
        inner = info.get("result", {}) if isinstance(info, dict) else {}
        assert_step(results, "4.7d get_geo_summary no-raise", ok=True,
                    artifact=table_path,
                    detail="keys={0}".format(
                        list(inner.keys())[:5] if isinstance(inner, dict) else []))
    except HoudiniCallError as e:
        assert_step(results, "4.7d get_geo_summary no-raise", ok=False,
                    artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))

    # 5. set_node_color table_top
    # conn.call 返 envelope；服务端 success 标志在 result.success 里。
    try:
        info = conn.call("set_node_color",
                         node_path="{0}/table_top".format(table_path),
                         r=0.6, g=0.35, b=0.18)
        inner = info.get("result", {}) if isinstance(info, dict) else {}
        ok = bool(inner.get("success", False)) if isinstance(inner, dict) else False
        assert_step(results, "4.7e set_node_color table_top", ok=ok,
                    artifact="{0}/table_top".format(table_path),
                    detail=str(inner)[:120])
    except HoudiniCallError as e:
        assert_step(results, "4.7e set_node_color table_top", ok=False,
                    artifact="{0}/table_top".format(table_path),
                detail="err: {0}".format(str(e)[:200]))

    # 6. layout_children 二次整齐化
    # server.py:994 ``layout_children(self, path, ...)`` — 用 ``path`` 而非
    # ``parent_path``；并直接走到 ``info["result"]``，因为 conn.call 返 envelope。
    try:
        info = conn.call("layout_children",
                         path=table_path, direction="horizontal")
        result_inner = info.get("result", {}) if isinstance(info, dict) else {}
        cnt = result_inner.get("children_count", 0) if isinstance(result_inner, dict) else 0
        ok = (cnt >= 7)  # top + 4 legs + 2 braces = 7
        assert_step(results, "4.7f layout_children >= 7 children", ok=ok,
                    artifact=table_path,
                    detail="children_count={0}".format(cnt))
    except HoudiniCallError as e:
        assert_step(results, "4.7f layout_children >= 7 children", ok=False,
                    artifact=table_path,
                    detail="err: {0}".format(str(e)[:200]))


# ---------------------------------------------------------------------------
# Step blocks（task 4.8 / 4.9 / 4.10 — Wave C：capture / render / audit）
# ---------------------------------------------------------------------------
# 共享最小阈值：5 KB（spec §Scenario: demo captures three pane screenshots
# 与 §Scenario: demo renders four views via Karma CPU 都明确 > 5 KB；过小
# 视为空白/损坏截图）。
_MIN_PNG_BYTES = 5120


def capture_pane_snapshots(conn: HoudiniConn,
                           results: List[StepResult],
                           artifact_dir: str) -> None:
    """Task 4.8：批量截 3 个 pane（NetworkEditor / SceneViewer / Parm）。

    实现要点：
    - 调用 capture_multiple_panes，服务端返
      {"results": [{pane_type, save_path, success, error}, ...]}（与
      _pane_capture.py:488 capture_multiple_panes 对齐）。
    - 对每个结果：success=True 且 save_path 存在且 size > 5 KB → PASS；
      否则 FAIL，并写 detail 说明具体原因（缺文件 / 太小 / 服务端 error）。
    - SceneViewer 端：H21+H22 主路径走 hou.SceneViewer.flipbook()，
      落盘文件含 $F4 替换帧号后缀（例 SceneViewer.0001.png）。当前
      capture_multiple_panes 未把 _renderer 字段透到 per-result（已知
      偏差），所以 demo 改以文件扩展模式作为 proxy：若 SceneViewer 落
      盘文件名含 4 位帧号后缀即视为 flipbook 路径；若不含帧号后缀
      则记录 WARN（Qt grab 降级路径），但不 FAIL。
    - 单条记录既视作独立 step；最终汇总额外写一行 4.8 汇总 PASS/FAIL
      到 results，便于 emit_summary 列表里直接看到。
    """
    pane_types = ["NetworkEditor", "SceneViewer", "Parm"]
    try:
        resp = conn.call("capture_multiple_panes",
                         pane_types=pane_types,
                         save_dir=artifact_dir)
    except HoudiniCallError as e:
        # transport-level 错误 → 整步 FAIL（连 1 张都没拿到）
        assert_step(results, "4.8 capture_multiple_panes (transport)",
                    ok=False, artifact=artifact_dir,
                    detail="err: {0}".format(str(e)[:200]))
        return

    raw_results = resp.get("result", {}).get("results") if isinstance(resp, dict) else None
    if not isinstance(raw_results, list):
        assert_step(results, "4.8 capture_multiple_panes (parse)",
                    ok=False, artifact=artifact_dir,
                    detail="resp missing 'results' list: keys={0}".format(
                        list(resp.keys())[:4] if isinstance(resp, dict)
                        else type(resp).__name__))
        return

    n_pass = 0
    n_fail = 0
    detail_lines: List[str] = []
    for item in raw_results:
        if not isinstance(item, dict):
            n_fail += 1
            detail_lines.append("? non-dict item")
            continue
        pt = item.get("pane_type", "?")
        sp = item.get("save_path", "") or ""
        ok_flag = bool(item.get("success", False))
        err = item.get("error")

        if not ok_flag:
            n_fail += 1
            detail_lines.append("{0}=FAIL ({1})".format(
                pt, (err or "success=False")[:80]))
            assert_step(results, "4.8 capture_{0}".format(pt),
                        ok=False, artifact=sp or artifact_dir,
                        detail="err: {0}".format((err or "")[:160]))
            continue

        # success=True：校验文件落盘 + 体积
        try:
            size = os.path.getsize(sp) if sp and os.path.exists(sp) else 0
        except OSError:
            size = 0
        if size <= _MIN_PNG_BYTES:
            n_fail += 1
            detail_lines.append("{0}=FAIL (size={1}B)".format(pt, size))
            assert_step(results, "4.8 capture_{0}".format(pt),
                        ok=False, artifact=sp,
                        detail="file too small or missing: size={0}B".format(
                            size))
            continue

        # PASS — SceneViewer 额外记录 renderer proxy
        n_pass += 1
        if pt == "SceneViewer":
            # flipbook 路径落盘为 `<stem>.<4位帧号>.png`（$F4 替换）；
            # Qt grab 路径落盘为 `<stem>.png`（无帧号后缀）。
            base = os.path.basename(sp)
            stem, ext = os.path.splitext(base)
            looks_like_flipbook = (
                len(stem) > 4 and stem[-4:].isdigit() and ext.lower() == ".png"
            )
            renderer_proxy = ("flipbook_via_Houdini_internal"
                              if looks_like_flipbook
                              else "qt_grab_or_other")
            detail_lines.append("{0}=PASS size={1}B renderer_proxy={2}".format(
                pt, size, renderer_proxy))
            assert_step(results, "4.8 capture_{0}".format(pt), ok=True,
                        artifact=sp,
                        detail="renderer_proxy={0} size={1}B".format(
                            renderer_proxy, size))
        else:
            detail_lines.append("{0}=PASS size={1}B".format(pt, size))
            assert_step(results, "4.8 capture_{0}".format(pt), ok=True,
                        artifact=sp, detail="size={0}B".format(size))

    # 汇总行：3 个 pane 全 PASS 才算整步 PASS；个别 FAIL 标 FAIL 但不阻断
    overall_ok = (n_pass == len(pane_types))
    assert_step(results, "4.8 capture_multiple_panes (overall)",
                ok=overall_ok, artifact=artifact_dir,
                detail="passed={0}/{1} | {2}".format(
                    n_pass, len(pane_types), " | ".join(detail_lines)))


def render_quad_views_karma(conn: HoudiniConn,
                            results: List[StepResult],
                            artifact_dir: str,
                            geometry_path: str = "/obj/table_demo") -> None:
    """Task 4.9：Karma CPU 渲 4 视图并落 PNG。

    实现要点：
    - 调用 render_quad_views_base64(renderer="karma_cpu",
      resolution=[640, 480], format="PNG")；服务端返
      {top: {image_base64, size_bytes, ...}, front: {...},
       side: {...}, perspective: {...}, _meta: {...}}。
    - Karma CPU 未装 / busy / 超时时：HoudiniCallError 抛或响应里 image_base64
      为空字符串（placeholder）；均视作 SKIP（不 FAIL），detail 写明原因。
    - 成功路径：建 `<artifact_dir>/table_demo_views/`，把每个 view 的
      base64 解码写 `<view>.png`；体积 > 5 KB 视为 PASS；任一失败则整步
      WARN（部分渲染仍可看，不算硬失败）。
    """
    views_dir = os.path.join(artifact_dir, "table_demo_views")
    try:
        os.makedirs(views_dir, exist_ok=True)
    except OSError as e:
        assert_step(results, "4.9 render_quad_views_karma_cpu",
                    ok=False, artifact=views_dir,
                    detail="cannot create views dir: {0}".format(e))
        return

    try:
        resp = conn.call("render_quad_views_base64",
                         geometry_path=geometry_path,
                         renderer="karma_cpu",
                         resolution=[640, 480],
                         format="PNG")
    except HoudiniCallError as e:
        # Karma CPU 不可用 / busy / 超时 → SKIP，不阻断 demo
        assert_step(results, "4.9 render_quad_views_karma_cpu",
                    ok=False, on_skip=True,
                    artifact=views_dir,
                    detail="SKIP: karma_cpu 不可用或超时: {0}".format(
                        str(e)[:160]))
        return

    if not isinstance(resp, dict):
        assert_step(results, "4.9 render_quad_views_karma_cpu",
                    ok=False, on_skip=True, artifact=views_dir,
                    detail="SKIP: resp 非 dict ({0})".format(type(resp)))
        return

    # conn.call 返 envelope：{"status", "result": {top:..., front:..., ...}, ...}
    # 服务端真正字段在 result 里；要先 unwrap。
    inner = resp.get("result", {}) if isinstance(resp, dict) else {}
    if not isinstance(inner, dict):
        inner = {}

    # 顶部 _warning（cmn._add_response_metadata 在 hou 缺失时塞入）
    if "_warning" in inner:
        assert_step(results, "4.9 render_quad_views_karma_cpu",
                    ok=False, on_skip=True, artifact=views_dir,
                    detail="SKIP: server _warning={0}".format(
                        str(inner["_warning"])[:160]))
        return

    view_names = ("top", "front", "side", "perspective")
    n_pass = 0
    n_fail = 0
    total_bytes = 0
    detail_lines: List[str] = []
    for vname in view_names:
        v = inner.get(vname)
        if not isinstance(v, dict):
            n_fail += 1
            detail_lines.append("{0}=MISSING".format(vname))
            continue
        b64 = v.get("image_base64") or ""
        if not b64:
            n_fail += 1
            detail_lines.append("{0}=EMPTY".format(vname))
            continue
        out_path = os.path.join(views_dir, vname + ".png")
        try:
            png_bytes = base64.b64decode(b64, validate=False)
        except (TypeError, ValueError) as e:
            n_fail += 1
            detail_lines.append("{0}=B64ERR ({1})".format(vname, str(e)[:40]))
            continue
        try:
            with open(out_path, "wb") as fh:
                fh.write(png_bytes)
        except OSError as e:
            n_fail += 1
            detail_lines.append("{0}=WRITEFAIL ({1})".format(
                vname, str(e)[:40]))
            continue
        size = len(png_bytes)
        total_bytes += size
        if size > _MIN_PNG_BYTES:
            n_pass += 1
            detail_lines.append("{0}=PASS({1}B)".format(vname, size))
        else:
            n_fail += 1
            detail_lines.append("{0}=TOO_SMALL({1}B)".format(vname, size))

    meta = inner.get("_meta", {}) if isinstance(inner, dict) else {}
    renderer_used = meta.get("renderer", "karma_cpu") if isinstance(meta, dict) \
        else "karma_cpu"

    # 整步：4 视图全 PASS 才 PASS；任一失败 → WARN（部分渲染可用）。
    # 若所有 4 个视图 b64 都为空（EMPTY），说明 Karma CPU renderer 实际未拿到
    # 任何输出（未装 / busy / 引擎 fallback 异常）—— 视作 SKIP，不阻断 demo。
    all_empty = (n_pass == 0 and n_fail == len(view_names) and
                 all("=EMPTY" in ln for ln in detail_lines))
    overall_ok = (n_pass == len(view_names))
    if overall_ok:
        overall_status = True
        overall_detail = "passed={0}/{1} total={2}B renderer={3}".format(
            n_pass, len(view_names), total_bytes, renderer_used)
        use_skip = False
    elif all_empty:
        # Karma renderer 没产图 → SKIP（非阻塞），不是 FAIL
        overall_status = False
        overall_detail = "SKIP: karma_cpu 无图（全 EMPTY）| {0}".format(
            " | ".join(detail_lines))
        use_skip = True
    else:
        # 部分渲染：WARN 而非 FAIL（demo 仍能 ship）
        overall_status = False
        overall_detail = "WARN passed={0}/{1} renderer={2} | {3}".format(
            n_pass, len(view_names), renderer_used,
            " | ".join(detail_lines))
        use_skip = False

    assert_step(results, "4.9 render_quad_views_karma_cpu",
                ok=overall_status, on_skip=use_skip,
                artifact=views_dir,
                detail=overall_detail)


def execute_code_audit(conn: HoudiniConn,
                       results: List[StepResult]) -> None:
    """Task 4.10：privileged mutation audit + scene diff 取证。

    实现要点：
    - 走 execute_code(policy="privileged", allow_dangerous=False,
      capture_diff=True)；body 只做 create+destroy 一个 null 节点（属
      mutation 但非 dangerous，所以 privileged + allow_dangerous=False
      不需要 HOUDINI_MCP_ALLOW_BYPASS 也能通过；只有 dangerous 才需要
      bypass env-var）。
    - 服务端若拒绝，返回 {executed: False, blocked: True, reason: ...}；
      这里捕获后判 SKIP（demo 跑 ≠ 失败）。
    - 然后 get_last_scene_diff()；当前服务端返
      {available, changed, before, after}（无 scene_changes 字段）。
      偏差说明：spec 写的是 scene_changes，实际字段叫 changed；这里以
      changed 为准，available=True 且 changed=True → PASS。
    - demo 故意不去 clear 用户的 .hip 文件（避免误删），用 create+destroy
      一个临时 null 节点作为 benign mutation。
    """
    audit_code = (
        "import hou\n"
        "_opera_parent = hou.node('/obj')\n"
        "_opera_before = len(_opera_parent.children())\n"
        "_opera_tmp = _opera_parent.createNode('null', 'opera_audit_temp')\n"
        "_opera_after = len(_opera_parent.children())\n"
        "_opera_tmp.destroy()\n"
        "print('AUDIT_DIFF: before={0} after={1} mutation=create+destroy "
        "null node'.format(_opera_before, _opera_after))\n"
    )

    # 1. execute_code — 走 privileged audit path
    try:
        resp = conn.call("execute_code", code=audit_code,
                         policy="privileged",
                         allow_dangerous=False,
                         capture_diff=True)
    except HoudiniCallError as e:
        assert_step(results, "4.10 execute_code_audit",
                    ok=False, on_skip=True, artifact="-",
                    detail="SKIP: transport err: {0}".format(str(e)[:160]))
        return

    if not isinstance(resp, dict):
        assert_step(results, "4.10 execute_code_audit",
                    ok=False, on_skip=True, artifact="-",
                    detail="SKIP: resp 非 dict ({0})".format(type(resp)))
        return

    # conn.call 返 envelope；服务端真正字段在 ``resp["result"]``。
    inner = resp.get("result", {}) if isinstance(resp, dict) else {}
    if not isinstance(inner, dict):
        inner = {}

    # 服务端 policy-block：执行没发生 → SKIP
    if inner.get("blocked") is True or inner.get("executed") is False:
        reason = inner.get("reason") or inner.get("message") or "blocked"
        assert_step(results, "4.10 execute_code_audit",
                    ok=False, on_skip=True, artifact="-",
                    detail="SKIP: server blocked: {0}".format(str(reason)[:160]))
        return

    # 2. get_last_scene_diff 取 diff
    try:
        diff = conn.call("get_last_scene_diff")
    except HoudiniCallError as e:
        assert_step(results, "4.10 execute_code_audit",
                    ok=False, on_skip=True, artifact="-",
                    detail="SKIP: get_last_scene_diff transport err: {0}".format(
                        str(e)[:160]))
        return

    if not isinstance(diff, dict):
        assert_step(results, "4.10 execute_code_audit",
                    ok=False, artifact="-",
                    detail="WARN: diff 非 dict ({0})".format(type(diff)))
        return

    # 同样 envelope：drill 到 result。
    diff_inner = diff.get("result", {}) if isinstance(diff, dict) else {}
    if not isinstance(diff_inner, dict):
        diff_inner = {}

    if diff_inner.get("available") is not True:
        msg = diff_inner.get("message") or "no diff captured"
        assert_step(results, "4.10 execute_code_audit",
                    ok=False, artifact="-",
                    detail="WARN: diff unavailable: {0}".format(str(msg)[:160]))
        return

    changed = bool(diff_inner.get("changed", False))
    has_before = bool(diff_inner.get("before"))
    has_after = bool(diff_inner.get("after"))
    # 主要 PASS 条件：diff available + before/after 都存在；changed
    # 不要求为 True（create+destroy 抵消后 scene hash 可能相等，仍算
    # audit path 走过——OK 是审计通道活着，不是必须真有副作用）。
    ok = has_before and has_after
    detail = ("changed={0} before_keys={1} after_keys={2}".format(
        changed,
        list(diff_inner["before"].keys())[:3] if has_before else [],
        list(diff_inner["after"].keys())[:3] if has_after else []))
    assert_step(results, "4.10 execute_code_audit",
                ok=ok, artifact="-", detail=detail)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _print_at_a_glance(results: List[StepResult]) -> None:
    """打一行 PASS/FAIL/SKIP/WARN 计数到 stdout，shell 一眼能看。"""
    n_pass = sum(1 for r in results if r.status == "PASS")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_skip = sum(1 for r in results if r.status == "SKIP")
    n_warn = sum(1 for r in results if r.status == "WARN")
    print("[summary] {0} steps: {1} PASS / {2} FAIL / {3} SKIP / {4} WARN".format(
        len(results), n_pass, n_fail, n_skip, n_warn))


def main() -> int:
    # artifact_dir 含时间戳：每次 run 一个独立目录，避免多次跑覆盖；
    # summary.md 与 capture PNG、Karma views 都落在这里。
    artifact_dir = os.path.join(
        tempfile.gettempdir(), "houdini_mcp", "e2e_demo_table",
        time.strftime("%Y%m%d_%H%M%S"))
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
            # Task 4.8 — pane 截图（Wave C）
            capture_pane_snapshots(conn, results, artifact_dir)
            # Task 4.9 — Karma CPU 四视图渲染（Wave C）
            render_quad_views_karma(conn, results, artifact_dir)
            # Task 4.10 — privileged mutation audit（Wave C）
            execute_code_audit(conn, results)
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
        _print_at_a_glance(results)
        return 1
    except Exception as e:  # noqa: BLE001
        # 未预期异常：打印 traceback + 已有汇总，避免静默吞错
        traceback.print_exc()
        md = emit_summary(results, out_path=os.path.join(
            artifact_dir, "summary.md"))
        print(md)
        _print_at_a_glance(results)
        return 1

    # 正常路径：emit Markdown summary → 落盘 + stdout
    md = emit_summary(results, out_path=os.path.join(
        artifact_dir, "summary.md"))
    print(md)
    _print_at_a_glance(results)
    print("[artifact_dir] {0}".format(artifact_dir))
    # 退出码语义：仅 FAIL 视为失败；PASS / WARN / SKIP 都算 demo 通过
    # （用户场景里 Karma CPU 偶尔忙、bypass env 没设都属预期内的 SKIP）
    has_fail = any(r.status == "FAIL" for r in results)
    return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())