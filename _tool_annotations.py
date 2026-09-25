# -*- coding: utf-8 -*-
"""bridge 侧 tool annotations 注册表后处理（feat-mcp-tool-guidance D1）。

设计要点：
- 不逐装饰器传参（176 处 ``@mcp.tool()`` 不动签名/注册面），在全部注册
  完成后统一遍历 ``mcp._tool_manager.list_tools()`` 批量设置
  ``Tool.annotations``——mcp 1.12.2 下 ``Tool`` 为 pydantic BaseModel 且
  未开 validate_assignment，字段可写（spec-reviewer 2026-09-26 源码+实跑
  实证：``server/fastmcp/server.py:356`` 构造签名含 annotations、
  ``tools/base.py:34`` 字段可写）。
- 映射锚 = server 命令三分类（``server.py`` READ_ONLY / MUTATING /
  NO_UNDO）。bridge 工具名与 server 命令名存在改名对（``COMMAND_BY_TOOL``）
  外基本同名；``batch`` 是混合元命令（三分类验证明确排除），按常规变更
  处理（readOnly=false / destructive=false）。
- ``destructiveHint`` 规范默认为 true——所有工具 MUST 显式全量设置，不
  依赖默认值（非破坏工具显式 false）。
- annotations 是对客户端的 untrusted 提示，不是安全边界；权威执行层仍是
  server 三分类 + render policy。
- 对账完整性测试（tests/test_tool_annotations.py）以 AST 提取 bridge
  工具 → 命令映射，import server 三分类做双向比对，防两侧漂移。

分类口径（2026-09-26 生成，RO=55 + 8 bridge-local 只读）：
READ_ONLY = server READ_ONLY_COMMANDS 全集镜像 + bridge-local 只读工具
DESTRUCTIVE = 破坏性语义子集（节点删除/场景载入新建/execute 变更档/磁盘
清写/HDA registry 写），跨 MUTATING 与 NO_UNDO 两类
"""

import logging

logger = logging.getLogger(__name__)

# bridge 工具名 → server 命令名的改名映射对（其余均同名直转）
COMMAND_BY_TOOL = {
    "disconnect_node_input": "disconnect_input",
    "execute_houdini_code": "execute_code",
    "get_houdini_events": "get_pending_events",
    "layout_network": "layout_children",
    "render_quad_views": "render_quad_view",
    "subscribe_houdini_events": "subscribe_events",
    "unsubscribe_houdini_events": "unsubscribe_events",
}

# 不经 TCP 转发、bridge 进程内直调本地模块的工具（change A design D3 同款口径）
BRIDGE_LOCAL_TOOLS = frozenset({
    "get_best_practices",
    "get_doc",
    "knowledge_stats",
    "monitor_render",
    "parse_hip_offline",
    "read_lesson",
    "save_lesson",
    "save_recipe",
    "search_docs",
    "search_lessons",
})

# readOnlyHint=true（+idempotentHint=true）：server READ_ONLY_COMMANDS
# 镜像 + bridge-local 只读查询
READ_ONLY_TOOLS = frozenset({
    # --- server READ_ONLY_COMMANDS 镜像（55）---
    "capture_workflow_snapshot",
    "check_connection",
    "explain_node",
    "find_error_nodes",
    "find_nodes",
    "get_cache_status",
    "get_console_log",
    "get_cook_chain",
    "get_current_take",
    "get_dop_field",
    "get_dop_object",
    "get_dop_relationships",
    "get_expression",
    "get_frame",
    "get_geo_summary",
    "get_geometry_data",
    "get_geometry_info",
    "get_hda_section_content",
    "get_hda_sections",
    "get_houdini_help",
    "get_keyframes",
    "get_last_scene_diff",
    "get_material_info",
    "get_network_overview",
    "get_node_info",
    "get_parameter",
    "get_parameter_schema",
    "get_render_settings",
    "get_scene_info",
    "get_scene_summary",
    "get_selection",
    "get_sim_memory_usage",
    "get_simulation_info",
    "get_wrangle_code",
    "hda_get",
    "hda_list",
    "list_caches",
    "list_children",
    "list_cop_node_types",
    "list_dop_objects",
    "list_material_types",
    "list_materials",
    "list_node_types",
    "list_render_nodes",
    "list_takes",
    "list_visible_panes",
    "pdg_status",
    "pdg_workitems",
    "ping_houdini",
    "serialize_scene",
    "verify_hou_api",
    # --- bridge-local 只读（8）---
    "get_best_practices",
    "get_doc",
    "knowledge_stats",
    "monitor_render",
    "parse_hip_offline",
    "read_lesson",
    "search_docs",
    "search_lessons",
})

# destructiveHint=true：破坏性语义子集（跨 MUTATING / NO_UNDO）
DESTRUCTIVE_TOOLS = frozenset({
    "clear_cache",          # 运行态 cache 清空（可删磁盘文件）
    "delete_node",          # 节点删除
    "execute_houdini_code", # execute_code 变更档（任意 Python）
    "execute_hscript",      # 任意 HScript（可含场景写）
    "geo_export",           # 磁盘覆写导出
    "hda_install",          # HDA registry 全局写
    "load_scene",           # 场景载入（丢弃未保存改动）
    "new_scene",            # 场景清空
    "reload_hda",           # HDA registry 重载
    "uninstall_hda",        # HDA registry 卸载
    "update_hda",           # HDA definition 覆写
    "write_cache",          # 磁盘清写
})

# bridge-local 写类（save_lesson / save_recipe：知识库追加写，非破坏）
# → readOnly=false / destructive=false（落入默认变更桶，无需单列。


def apply_tool_annotations(fastmcp_obj):
    """对 ``fastmcp_obj`` 的全部已注册工具批量设置 tool annotations。

    幂等（重复调用重设同值）。返回设置成功的工具数；``ToolAnnotations``
    不可导入（SDK 异常）时返回 0 并 warning——元数据失败不影响工具可用性。
    集合中登记但未注册的工具名打 warning（防改名漂移），未登记的工具按
    常规变更默认（readOnly=false / destructive=false）显式设置。
    """
    try:
        from mcp.types import ToolAnnotations
    except Exception as import_err:  # pragma: no cover - SDK 锁 1.12.2
        logger.warning("ToolAnnotations 不可用，annotations 跳过: %s",
                       import_err)
        return 0
    manager = getattr(fastmcp_obj, "_tool_manager", None)
    if manager is None:
        return 0
    applied = 0
    registered = set()
    for tool in manager.list_tools():
        name = tool.name
        registered.add(name)
        if name in READ_ONLY_TOOLS:
            tool.annotations = ToolAnnotations(
                title=None,
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            )
        elif name in DESTRUCTIVE_TOOLS:
            tool.annotations = ToolAnnotations(
                title=None,
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
            )
        else:
            tool.annotations = ToolAnnotations(
                title=None,
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
            )
        applied += 1
    stale = (READ_ONLY_TOOLS | DESTRUCTIVE_TOOLS) - registered
    if stale:
        logger.warning(
            "annotations 集合含未注册工具名（可能已改名，请同步注册表）: %s",
            sorted(stale))
    return applied
