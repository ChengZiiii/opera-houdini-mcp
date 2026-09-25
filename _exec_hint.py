# -*- coding: utf-8 -*-
"""execute_houdini_code 失败路径引导（feat-mcp-tool-guidance §2）。

机制（design D4，nudge 而非 hard block）：
- ``_ai_hint`` 行：返回文本出现 **错误上下文**（Traceback / --- Stderr ---
  段 / Error 行 / execution_error）且其中含 ``hou.*`` 调用模式时，在返回
  文本末尾追加一行引导——提取去重保序 ≤3 个 API 名，指向
  ``verify_hou_api`` / ``get_houdini_help``。无 hou.* 模式或无错误上下文
  时 MUST NOT 追加（零噪音：成功 stdout 里打印 hou.* 字符串不触发）。
- ``_hint`` 行：会话内（bridge 进程生命周期）``execute_houdini_code``
  调用计数达到阈值（``HOUDINI_MCP_EXEC_HINT_THRESHOLD``，默认 5，0 关闭）
  起，每次返回追加一行专用工具替代指引。不设重置（KISS，会话粒度）。

形态约束：hint 为**文本行追加**（``\\n_ai_hint: ...``），工具返回仍为 str，
不改 envelope 结构（spec：返回形态 MUST NOT 改变）；追加发生在 server 侧
``apply_response_cap`` 之后（bridge 侧生成，天然不受 cap 截断影响）。
"""

import os
import re

_HOU_API_RE = re.compile(r"hou\.(?:[A-Za-z_]\w*\.){0,2}[A-Za-z_]\w*")

_ERROR_CONTEXT_MARKERS = (
    "Traceback",
    "--- Stderr ---",
    "Error (",
    "execution_error",
)

# 会话计数器：bridge 进程生命周期 = 一个会话（design D4，无重置逻辑）
_EXEC_CALL_COUNT = 0


def hint_threshold():
    """读取阈值 env；非法值回退默认 5。"""
    try:
        return int(os.environ.get("HOUDINI_MCP_EXEC_HINT_THRESHOLD", "5"))
    except (TypeError, ValueError):
        return 5


def _has_error_context(text):
    return any(marker in text for marker in _ERROR_CONTEXT_MARKERS)


def extract_hou_apis(text, limit=3):
    """从文本提取 ``hou.*`` API 名（去重保序，≤limit）。"""
    seen = []
    for match in _HOU_API_RE.finditer(text or ""):
        name = match.group(0).rstrip(".")
        if name not in seen:
            seen.append(name)
        if len(seen) >= limit:
            break
    return seen


def append_ai_hint(return_text):
    """错误上下文 + hou.* 模式时追加 ``_ai_hint`` 行；否则原样返回。"""
    if not return_text or "\n_ai_hint:" in return_text:
        return return_text
    if not _has_error_context(return_text):
        return return_text
    apis = extract_hou_apis(return_text)
    if not apis:
        return return_text
    hint = (
        "\n_ai_hint: 返回中出现 hou API 调用痕迹（{0}）——跨版本 hou API "
        "会重命名/废弃，写码前先 verify_hou_api('<Class>.<method>') 核对"
        "当前版本签名；节点/参数类查询可改用 get_houdini_help / "
        "get_parameter_schema。"
    ).format(", ".join(apis))
    return return_text + hint


def count_and_maybe_hint(return_text):
    """递增会话计数；达到阈值后每次追加 ``_hint`` 专用工具指引行。"""
    global _EXEC_CALL_COUNT
    _EXEC_CALL_COUNT += 1
    threshold = hint_threshold()
    text = return_text or ""
    if threshold <= 0 or "\n_hint:" in text:
        return return_text
    if _EXEC_CALL_COUNT < threshold:
        return return_text
    hint = (
        "\n_hint: 本次会话已调用 execute_houdini_code {0} 次——多数操作有"
        "专用工具（set_parameters / create_wrangle / connect_nodes / "
        "batch / get_parameter_schema / find_nodes），优先改用可降低风险"
        "并提高可审计性。"
    ).format(_EXEC_CALL_COUNT)
    return text + hint


def apply_execute_hints(return_text):
    """execute_houdini_code 的统一出口后处理（计数 + 失败引导）。"""
    return count_and_maybe_hint(append_ai_hint(return_text))


def reset_counter():
    """测试辅助：清零会话计数。"""
    global _EXEC_CALL_COUNT
    _EXEC_CALL_COUNT = 0
