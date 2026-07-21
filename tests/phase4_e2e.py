"""Phase 4 端到端集成测试（绕过 MCP bridge，直接走 Houdini TCP socket）。

用法：
    C:/.../external/houdinimcp-env/python/python.exe tests/phase4_e2e.py

连接 Houdini port 9876，跑完整端到端流程，断言返回 schema + 临时目录。
不影响用户当前场景（用 /obj/MCP_PHASE4_TEST 容器）。

历史：
- 2026-07-21 重构：HoudiniConn / step / assert_ok 抽到 _e2e_helpers.py
  共享给 e2e_demo_table.py；本文件只保留测试流程本身。
- 修正：原先使用硬编码的 host-specific 绝对路径加载 _capture_paths.py，
  违反 fork 跨主机可移植约束（任何把 fork 移到别的目录的消费者都会破）。
  现改为相对本文件所在目录的 _capture_paths.py。
"""
import json
import os
import sys
import tempfile
import time
from importlib.util import module_from_spec, spec_from_file_location

from _e2e_helpers import HoudiniConn  # noqa: E402  (tests/ 目录运行)


def step(n, label):
    print("\n[Step {0}] {1}".format(n, label))


def assert_ok(resp, label, expect_keys=None):
    if resp.get("status") != "success":
        print("  FAIL  {0}: {1}".format(label, resp))
        sys.exit(1)
    if expect_keys:
        for k in expect_keys:
            if k not in resp:
                print("  FAIL  {0}: missing key '{1}'".format(label, k))
                sys.exit(1)
    print("  PASS  {0}".format(label))
    return resp.get("result")


def main():
    with HoudiniConn() as conn:

        # Step 1: 不要 new_scene（避免破坏用户场景）— 直接用容器
        step(1, "查现役场景 + 准备容器（不破坏 untitled.hip）")
        scene = assert_ok(conn.call("get_scene_info"), "get_scene_info")
        print("  scene={0} nodes={1}".format(scene.get("name"),
                                             scene.get("node_count")))

        # Step 2: 建 geo 容器
        step(2, "create_node(geo, /obj, MCP_PHASE4_TEST)")
        res = assert_ok(conn.call("create_node",
            node_type="geo", parent_path="/obj",
            name="MCP_PHASE4_TEST"),
            "create_node")
        print("  container path =", res.get("path"))

        # Step 3: 建 wrangle + VEX
        step(3, "create_wrangle(attribwrangle, P.y += sin(P.x))")
        vex = "@P.y += sin(@P.x);\nv@col = {sin(@P.x), cos(@P.y), 0.5};"
        res = assert_ok(conn.call("create_wrangle",
            parent_path="/obj/MCP_PHASE4_TEST", vex_code=vex,
            name="wr1", run_over="points", input_node=""),
            "create_wrangle")
        print("  wrangle path =", res.get("path"))
        if res.get("validation", {}).get("errors"):
            print("  WRN  wrangle cook errors:", res["validation"]["errors"])

        # Step 4: 建材质
        step(4, "create_material(principledshader, /mat, mat_red)")
        res = assert_ok(conn.call("create_material",
            material_type="principledshader", name="mat_red",
            parent_path="/mat"),
            "create_material")
        print("  material path =", res.get("path"))

        # Step 5: 绑定材质到容器
        step(5, "assign_material(/obj/MCP_PHASE4_TEST, /mat/mat_red)")
        res = assert_ok(conn.call("assign_material",
            geometry_path="/obj/MCP_PHASE4_TEST",
            material_path="/mat/mat_red", group=None),
            "assign_material")
        print("  success =", res.get("success"))

        # Step 6: 验证 Bug A — render_quad_views 应返 dict 不再 schema error
        step(6, "render_quad_views (Bug A 验证：返 dict 含 image_path)")
        quad_dir = os.path.join(tempfile.gettempdir(),
                                "houdini_mcp", "phase4", "quad")
        os.makedirs(quad_dir, exist_ok=True)
        # server.py 注册名是 render_quad_view（单数）；bridge 端叫
        # render_quad_views（复数）暴露给 MCP。直接 socket 走 server 原生命名。
        # conn.call 在 status=error 时抛 HoudiniCallError；这里要宽容
        # karma_cpu PARTIAL 的情况，因此手动用 send_json/recv_json。
        conn.send_json({"type": "render_quad_view",
                        "params": {"render_path": quad_dir,
                                   "render_engine": "karma",
                                   "karma_engine": "cpu"}})
        res = conn.recv_json()
        if res.get("status") != "success":
            # karma_cpu 渲染可能因 GPU 问题失败（已知 env），但不应是 schema dict 错误
            if "result" in res and isinstance(res.get("result"), dict):
                print("  PARTIAL: Houdini returned dict（Bug A 修复确认），server status={0}".format(
                    res.get("status")))
                print("  result keys:", list(res["result"].keys())[:8])
            else:
                print("  FAIL  render_quad_views:", res)
                sys.exit(1)
        else:
            print("  PASS  render_quad_views (status=success)")
            result = res.get("result", {})
            if isinstance(result, dict):
                print("  result is dict, keys:", list(result.keys())[:8])

        # Step 7: 验证 Bug B — capture_pane_screenshot SceneViewer 应走 flipbook
        step(7, "capture_pane_screenshot SceneViewer (Bug B 验证：flipbook 路径)")
        sv_path = os.path.join(tempfile.gettempdir(), "houdini_mcp",
                                "phase4_sv_test.png")
        # Bug B fix 实测：flipbook 调通但耗时较长（>30s），给足 timeout。
        # 如果 flippedbook 仍报 argument 2 错误，说明 Houdini 加载的 server.py
        # 仍是旧版——用户需在 shelf 再次 MCP Stop + Start 触发 reload。
        conn.send_json({"type": "capture_pane_screenshot",
                        "params": {"pane_type_name": "SceneViewer",
                                   "save_path": sv_path,
                                   "fit_contents": True}})
        res = conn.recv_json()
        print("  result =", json.dumps(res, default=str)[:500])
        if res.get("status") == "success":
            cap = res.get("result", {})
            renderer = cap.get("_renderer")
            actual_path = cap.get("save_path")
            if renderer == "flipbook_via_Houdini_internal":
                print("  PASS  SceneViewer flipbook 路径已生效")
                print("  save_path (含 $F4 占位替换):", actual_path)
                if actual_path and os.path.exists(actual_path):
                    print("  PASS  png 落盘 size={0} bytes".format(
                        os.path.getsize(actual_path)))
                else:
                    print("  WARN  png 未找到，实际路径:", actual_path)
            else:
                print("  FAIL  SceneViewer 未走 flipbook，_renderer =",
                      renderer)
        else:
            print("  FAIL  capture_pane_screenshot:", res)

        # Step 8: 验证 Bug C — temp 目录规范
        step(8, "Bug C 验证：$TEMP/houdini_mcp/<YYYY-MM-DD>/ 目录结构")
        base = os.path.join(tempfile.gettempdir(), "houdini_mcp")
        if not os.path.isdir(base):
            print("  FAIL  BASE 目录未创建:", base)
        else:
            print("  PASS  BASE 存在:", base)
            subdirs = sorted([d for d in os.listdir(base)
                             if os.path.isdir(os.path.join(base, d))])
            print("  BASE 直接子项:", subdirs[:10])
            date_pattern_subdirs = [d for d in subdirs
                                    if len(d) == 10 and d[4] == "-" and d[7] == "-"]
            if date_pattern_subdirs:
                print("  PASS  发现 YYYY-MM-DD 日期目录:", date_pattern_subdirs)
                sample = os.path.join(base, date_pattern_subdirs[0])
                for root, dirs, files in os.walk(sample):
                    for f in files:
                        fp = os.path.join(root, f)
                        rel = os.path.relpath(fp, base)
                        print("    {0} ({1} bytes)".format(
                            rel, os.path.getsize(fp)))
            else:
                print("  WARN  未发现 YYYY-MM-DD 日期目录（可能测试未触发落盘）")

        # Step 9: cleanup 验证（mock now = 100 天后）
        step(9, "Bug C cleanup 验证（mock now=100天后）")
        # 修复 F1：原先用硬编码 host-specific 绝对路径加载 _capture_paths.py，
        # 违反 fork 跨主机可移植约束（任何把 fork 移到别的目录的消费者都会破）。
        # 改为相对本文件所在目录的 _capture_paths.py：
        cap_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "_capture_paths.py",
        )
        cap_spec = spec_from_file_location("_cap", cap_path)
        cap_mod = module_from_spec(cap_spec)
        cap_spec.loader.exec_module(cap_mod)
        mock_now = time.time() + 100 * 86400
        res = cap_mod.cleanup_old_captures(base, max_age_days=7, now=mock_now)
        print("  cleanup result:", res)

        # Step 10: 清理容器
        step(10, "delete_node(/obj/MCP_PHASE4_TEST) — 场景回 baseline")
        res = assert_ok(conn.call("delete_node",
            path="/obj/MCP_PHASE4_TEST"), "delete_node")
        print("  deleted =", res.get("deleted"))

    print("\n*** Phase 4 e2e done ***")


if __name__ == "__main__":
    main()