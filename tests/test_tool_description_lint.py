# -*- coding: utf-8 -*-
"""feat-mcp-tool-guidance §3 描述 lint：pattern 禁令 + 长度上限。

契约（mcp-tools spec「工具描述紧凑化」）：
- description（docstring）不得含实现史 pattern：``PR \\d``、issue 号
  （``#\\d{1,4}``）、change 代号（``fix-mcp-`` / ``feat-mcp-`` /
  ``perf-mcp-`` 等）、章节号引用（``§``）；
- 长度默认 ≤1200 字符；历史 8 工具（大瘦身）≤60 字、12 工具（小瘦身）
  ≤120 字的更严上限继续生效；
- 豁免白名单：确需含 pattern 的极少数描述显式登记（工具名 → 理由）。

工具集口径：与 test_tool_guidance 相同的 AST 提取（活动 @mcp.tool() 函数，
含 impl 递归不影响 docstring 提取）。
"""

import ast
import os
import re
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")

FORBIDDEN_PATTERN = re.compile(
    r"PR \d|#\d{1,4}\b|fix-mcp-|feat-mcp-|perf-mcp-|§")

DEFAULT_MAX_CHARS = 1200

# 历史「大瘦身」8 工具：一行摘要（≤60 字）
TIGHT_60_TOOLS = {
    "create_take",
    "list_takes",
    "get_current_take",
    "set_current_take",
    "create_chop_node",
    "export_chop_to_parm",
    "get_chop_data",
    "list_chop_channels",
}

# 历史「小瘦身」12 工具：1-2 句（≤120 字）
TIGHT_120_TOOLS = {
    "get_houdini_events",
    "subscribe_houdini_events",
    "unsubscribe_houdini_events",
    "pdg_cook",
    "pdg_status",
    "pdg_workitems",
    "pdg_dirty",
    "pdg_cancel",
    "list_caches",
    "get_cache_status",
    "clear_cache",
    "write_cache",
}

# 豁免白名单：工具名 → 理由（当前为空——新增需在 PR 里说明）
PATTERN_EXEMPTIONS = {}


def _collect_tool_docstrings():
    """返回 [(tool_name, docstring)]——活动 @mcp.tool() 顶层函数。"""
    with open(BRIDGE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    collected = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "mcp" and target.attr == "tool"):
                collected.append((node.name, ast.get_docstring(node) or ""))
                break
    return collected


def _strip_code_spans(text):
    """去掉 ``...`` 代码 span 后再算长度（反引号标记不计入语义长度）。"""
    return re.sub(r"``[^`]*``", "", text)


def _violations():
    bad_patterns = []
    bad_lengths = []
    for name, docstring in _collect_tool_docstrings():
        if name in PATTERN_EXEMPTIONS:
            continue
        match = FORBIDDEN_PATTERN.search(docstring)
        if match:
            bad_patterns.append(
                "{0}: {1!r}".format(name, match.group(0)))
        limit = DEFAULT_MAX_CHARS
        if name in TIGHT_60_TOOLS:
            limit = 60
        elif name in TIGHT_120_TOOLS:
            limit = 120
        length = len(_strip_code_spans(docstring))
        if length > limit:
            bad_lengths.append(
                "{0}: {1}>{2}".format(name, length, limit))
    return bad_patterns, bad_lengths


class ToolDescriptionLintTests(unittest.TestCase):
    def test_tools_collected(self):
        items = _collect_tool_docstrings()
        self.assertGreaterEqual(len(items), 170,
                                "应收集到全部活动工具（当前 176）")

    def test_no_forbidden_patterns(self):
        bad_patterns, _ = _violations()
        self.assertEqual(
            bad_patterns, [],
            "description 含实现史 pattern（搬去函数体注释）:\n  "
            + "\n  ".join(bad_patterns))

    def test_length_limits(self):
        _, bad_lengths = _violations()
        self.assertEqual(
            bad_lengths, [],
            "description 超长:\n  " + "\n  ".join(bad_lengths))

    def test_help_tools_lead_with_trigger(self):
        """help 类工具首行写明触发时机（spec 锁定）。"""
        for name, docstring in _collect_tool_docstrings():
            if name in ("get_houdini_help", "verify_hou_api",
                        "search_docs", "search_lessons", "get_best_practices"):
                first_line = docstring.strip().splitlines()[0]
                self.assertRegex(
                    first_line,
                    r"(先调|遇|查询前|检索|何时|报错|重试|不知道|不认识)",
                    "{0} 首行应写明触发时机: {1!r}".format(name, first_line))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# feat-mcp-tool-guidance tasks 4.1：实现备忘搬家抽查
#（spec「工具描述紧凑化」Scenario「实现备忘搬家不丢失」）。
# 每个 entry 为 (工具名, 函数体注释中的标志性片段)；断言片段存在于该工具
# 函数源码段——证明原 docstring 的实现备忘搬家到了注释而非被裁剪。
# ---------------------------------------------------------------------------
MEMO_SPOT_CHECKS = [
    # 批次 3.2 help/知识类
    ("get_houdini_help", "PR 15（help 链路"),
    ("verify_hou_api", "PR 18（wrapper"),
    ("search_lessons", "主动沉淀工作流（advisory）"),
    ("save_lesson", "加深方法论（advisory）"),
    ("save_recipe", "方法论协议（advisory）"),
    ("capture_workflow_snapshot", "probe_mode=auto 细则"),
    # 批次 3.3 渲染/截图/分页族
    ("render_single_view", "redirect 实证史"),
    ("start_render", "background 模式"),
    ("list_material_types", "add-scene-context-selection-materials"),
    # 批次 3.4 图编辑/节点/参数族
    ("execute_houdini_code", "执行模型出自"),
    ("list_node_types", "PR 6——relay 到"),
    ("find_error_nodes", "PR 11 行为——"),
    ("layout_children", "PR 9 推荐"),
    # 批次 3.5 渲染设置/缓存族
    ("set_render_settings", "受限策略细节见"),
    ("manage_cache", "PR 6——relay"),
    # 批次 3.6 场景/时间线/连接族
    ("load_scene", "PR 6——server 侧"),
    ("get_frame", "PR 19（时间线"),
    ("playbar_control", "PR 19——step 仅走"),
    ("check_connection", "PR 16（连接诊断"),
    ("ping_houdini", "PR 16——不持久化"),
]


def _function_source_segments():
    """返回 {函数名: 源码段}（顶层函数，含 docstring 与函数体注释）。"""
    with open(BRIDGE_PATH, "r", encoding="utf-8") as handle:
        src = handle.read()
    lines = src.splitlines(True)
    segments = {}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segments[node.name] = "".join(
                lines[node.lineno - 1:node.end_lineno])
    return segments


class MemoRelocationSpotCheckTests(unittest.TestCase):
    def test_spot_check_count_covers_all_batches(self):
        self.assertGreaterEqual(len(MEMO_SPOT_CHECKS), 10)
        tools = {name for name, _ in MEMO_SPOT_CHECKS}
        # 覆盖 lint 五批任务域各至少一员
        for required in (("search_lessons", "get_houdini_help"),
                         ("render_single_view", "start_render"),
                         ("execute_houdini_code", "list_node_types"),
                         ("set_render_settings", "manage_cache"),
                         ("load_scene", "get_frame", "check_connection")):
            self.assertTrue(
                tools & set(required),
                "抽查未覆盖批次域: {0}".format(required))

    def test_memos_relocated_to_body_comments(self):
        segments = _function_source_segments()
        missing = []
        for name, snippet in MEMO_SPOT_CHECKS:
            segment = segments.get(name)
            if segment is None or snippet not in segment:
                missing.append("{0}: 缺 {1!r}".format(name, snippet))
        self.assertEqual(
            missing, [],
            "实现备忘搬家丢失（应在函数体注释中）:\n  "
            + "\n  ".join(missing))
