# -*- coding: utf-8 -*-
"""fix-mcp-dead-tools-p0 task 1.2 验证：fastmcp 工具 schema 类型断言。

遍历全部 @mcp.tool() 注册工具，导出 inputSchema，断言目标参数的 JSON
type 非 string；同时全量列出仍是 string 且默认值非字符串的参数（潜在
漏改），输出最终工具×参数类型清单。
"""
import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # external/houdinimcp
_ENV = os.path.join(os.path.dirname(_ROOT), "houdinimcp-env", "pylibs")
sys.path.insert(0, _ENV)
sys.path.insert(0, _ROOT)
import houdini_mcp_server as m

TOOLS = asyncio.run(m.mcp.list_tools())

# (tool, param) -> 期望 type 子串（None 表示 anyOf 含该类型即可）
EXPECT = {
    ("set_frame", "frame"): "number",
    ("set_frame_range", "start"): "number",
    ("set_frame_range", "end"): "number",
    ("set_playback_range", "start"): "number",
    ("set_playback_range", "end"): "number",
    ("set_keyframe", "frame"): "number",
    ("set_keyframe", "value"): "number",
    ("delete_keyframe", "frame"): "number",
    ("set_node_position", "x"): "number",
    ("set_node_position", "y"): "number",
    ("set_node_color", "r"): "number",
    ("set_node_color", "g"): "number",
    ("set_node_color", "b"): "number",
    ("get_attrib_values", "offset"): "integer",
    ("get_attrib_values", "limit"): "integer",
    ("get_group_members", "offset"): "integer",
    ("get_group_members", "limit"): "integer",
    ("get_prim_intrinsics", "prim_index"): "integer",
    ("get_prim_intrinsics", "names"): "array",
    ("find_nearest_point", "max_distance"): "number",
    ("find_nearest_point", "position"): "array",
    ("create_material", "parameters"): "object",
    ("geo_export", "overwrite"): "boolean",
    ("get_houdini_help", "timeout"): "integer",
    ("verify_hou_api", "timeout"): "integer",
    ("ping_houdini", "timeout"): "number",
    ("search_docs", "limit"): "integer",
    ("get_houdini_events", "limit"): "integer",
    ("parse_hip_offline", "include_params"): "boolean",
    ("parse_hip_offline", "max_depth"): "integer",
    ("capture_workflow_snapshot", "include_vex"): "boolean",
    ("capture_workflow_snapshot", "max_nodes"): "integer",
    ("capture_workflow_snapshot", "include_connected"): "boolean",
    ("capture_workflow_snapshot", "offset"): "integer",
    ("capture_workflow_snapshot", "limit"): "integer",
    ("find_error_nodes", "include_warnings"): "boolean",
    ("find_error_nodes", "max_warnings"): "integer",
    ("get_geo_summary", "sample_size"): "integer",
    ("get_geo_summary", "max_points_for_full"): "integer",
    ("layout_children", "horizontal_spacing"): "number",
    ("layout_children", "vertical_spacing"): "number",
    ("get_node_info", "include_errors"): "boolean",
    ("get_node_info", "force_cook"): "boolean",
    ("get_node_info", "compact"): "boolean",
    ("capture_pane_screenshot", "fit_contents"): "boolean",
    ("render_node_network", "fit_contents"): "boolean",
    ("capture_sceneviewer_flipbook_views", "fit_contents"): "boolean",
    ("lock_parameter", "locked"): "boolean",
    ("create_spare_parameter", "num_components"): "integer",
    ("get_network_overview", "max_depth"): "integer",
    ("get_network_overview", "max_nodes"): "integer",
    ("get_cook_chain", "max_nodes"): "integer",
    ("explain_node", "include_params"): "boolean",
    ("explain_node", "max_params"): "integer",
    ("get_scene_summary", "max_nodes"): "integer",
    ("set_selection", "clear_others"): "boolean",
    ("list_dop_objects", "offset"): "integer",
    ("list_dop_objects", "limit"): "integer",
    ("get_dop_object", "max_data"): "integer",
    ("get_dop_field", "record_index"): "integer",
    ("get_dop_relationships", "max_objects"): "integer",
    ("step_simulation", "frames"): "integer",
    ("reset_simulation", "reset_frame"): "number",
    ("pdg_cook", "blocking"): "boolean",
    ("pdg_cook", "timeout_seconds"): "number",
    ("pdg_workitems", "max_items"): "integer",
    ("lop_stage_info", "max_prims"): "integer",
    ("lop_prim_get", "max_attributes"): "integer",
    ("lop_prim_search", "max_depth"): "integer",
    ("lop_layer_info", "max_layers"): "integer",
    ("list_usd_prims", "max_prims"): "integer",
    ("get_usd_attribute", "time"): "number",
    ("get_usd_composition", "max_arcs"): "integer",
    ("inspect_usd_layer", "max_layers"): "integer",
    ("list_lights", "max_lights"): "integer",
    ("get_cop_geometry", "output_index"): "integer",
    ("get_cop_geometry", "frame"): "number",
    ("get_cop_layer", "frame"): "number",
    ("get_cop_vdb", "output_index"): "integer",
    ("list_chop_channels", "output_index"): "integer",
    ("get_chop_data", "sample"): "integer",
    ("get_chop_data", "frame"): "number",
    ("get_chop_data", "time"): "number",
    ("get_chop_data", "start"): "integer",
    ("get_chop_data", "end"): "integer",
    ("export_chop_to_parm", "replace_existing"): "boolean",
    ("list_caches", "max_nodes"): "integer",
    ("clear_cache", "remove_disk_file"): "boolean",
}


def prop_type(schema, name):
    """取参数 schema 的 type 描述（处理 anyOf 与 $ref/嵌套）。"""
    props = schema.get("properties", {})
    p = props.get(name)
    if p is None:
        return None
    if "type" in p:
        return p["type"]
    if "anyOf" in p:
        types = [t.get("type", "?") for t in p["anyOf"]]
        return "|".join(sorted(types))
    return list(p.keys()) or "?"


tools_by_name = {t.name: t for t in TOOLS}
failures = []
for (tool, param), want in sorted(EXPECT.items()):
    t = tools_by_name.get(tool)
    if t is None:
        failures.append("%s.%s: TOOL NOT FOUND" % (tool, param))
        continue
    got = prop_type(t.inputSchema, param)
    if got is None:
        failures.append("%s.%s: PARAM NOT FOUND (props=%s)" % (
            tool, param, sorted(t.inputSchema.get("properties", {}))))
    elif want not in got.split("|"):
        failures.append("%s.%s: want %s, got %s" % (tool, param, want, got))

print("TOOLS TOTAL:", len(TOOLS))
print("ASSERTS:", len(EXPECT))
if failures:
    print("FAILURES:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL TYPE ASSERTIONS PASSED")

# 全量类型清单（改动核对用）：列出每个工具每个参数的 schema type + default
print("\n=== FULL PARAM TYPE MAP (changed tools) ===")
changed = sorted({t for (t, _p) in EXPECT})
for name in changed:
    t = tools_by_name[name]
    parts = []
    for pname, pschema in t.inputSchema.get("properties", {}).items():
        parts.append("%s=%s" % (pname, prop_type(t.inputSchema, pname)))
    print("%s: %s" % (name, ", ".join(parts)))

# 漏改扫描：schema type 为 string 但 default 是数值/布尔的参数
print("\n=== LEFTOVER string-typed params with non-string defaults ===")
leftover = 0
for t in TOOLS:
    for pname, pschema in t.inputSchema.get("properties", {}).items():
        ptype = prop_type(t.inputSchema, pname)
        default = pschema.get("default", "__unset__")
        if "string" in (ptype or "") and default != "__unset__" and default is not None \
                and isinstance(default, (int, float, bool)):
            print("  %s.%s: type=%s default=%r" % (t.name, pname, ptype, default))
            leftover += 1
print("LEFTOVER COUNT:", leftover)
