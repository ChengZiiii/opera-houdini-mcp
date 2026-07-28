"""_render_jobs.py — opera-houdini-mcp 同步 render 与四层 policy gate。

承载 ``start_render`` 的 Layer 3 / Layer 4 入口。Layer 1（bridge 纯
helper）和 Layer 2（server handler）分别在 ``houdini_mcp_server.py``
和 ``server.py``；本模块只负责：
- 同步阻塞 ``node.render()``；
- ``frame_range`` 关键字签名校验（2 或 3 个有限浮点，start<=end，
  increment>0；缺省传空 tuple）；
- Layer 3：在 ``_render_jobs.start_render`` 内部再次 resolve 真实
  node / 推断 renderer / 复用 ``_render_policy.enforce_render_policy``
  与 ``consume_consent_token``；
- Layer 4：在 ``_render_node_sync`` 内 ``node.render()`` 紧前最后
  一次校验，确保长时间 bridge / settings round-trip 后过期的 token
  无法启动渲染。

设计依据：
- D1（同步阻塞）：把 HOM 放进 background Python thread 不安全
  （参考批次1 sync design 决策），故本 change **不**签发 job / progress
  handle；调用完成即返回 terminal result。
- D2（frame_range）：仅 2 或 3 个 number；不允许 list 长度 1 / 4+；
  缺省传空 tuple，让 ROP 自身设置生效。
- D3（policy 复用）：所有层都通过 ``_render_policy`` 既有函数验证；
  ``consume_consent_token`` 已是 fork-render-policy-defense-in-depth
  修复后的幂等语义（5 分钟窗口内允许多层重放）。
- D4（Layer 4 时序）：必须在 ``node.render()`` 之前，**不**调用任何
  render 副作用。

约束：
- hou 通过第一参数注入；顶层不 ``import hou``。
- 不签发 progress handle；不维护 job registry / callback / TTL。
- 4 空格缩进 / snake_case / 中文 docstring / 无 f-string / 无类型注解。
- 所有返回 dict 走 ``apply_response_cap``。
"""
import math
import time

from . import _common as cmn
from . import _render_policy as _rp
from . import _render_settings as _rset


# ---------------------------------------------------------------------------
# Section 1: frame_range 校验
# ---------------------------------------------------------------------------
def _coerce_frame_range(frame_range):
    """校验 ``frame_range`` 形如 ``[start, end]`` 或 ``[start, end, inc]``。

    接受 ``None``（-> 空 tuple 由调用方处理）；list / tuple 长度必须为
    2 或 3；每个元素为有限浮点；``start <= end``；``increment > 0``。

    Returns:
        ``{"value": tuple_float}`` 或 ``{"status": "error", ...}``。
    """
    if frame_range is None:
        return {"value": ()}
    if not isinstance(frame_range, (list, tuple)):
        return {"status": "error", "message": (
            "frame_range must be a list or tuple of 2 or 3 numbers"),
            "field": "frame_range"}
    if len(frame_range) not in (2, 3):
        return {"status": "error", "message": (
            "frame_range must have 2 or 3 elements; got %d")
            % len(frame_range),
            "field": "frame_range"}
    coerced = []
    for index, item in enumerate(frame_range):
        if isinstance(item, bool):
            return {"status": "error", "message": (
                "frame_range[%d] must not be a bool") % index,
                "field": "frame_range"}
        if not isinstance(item, (int, float)):
            return {"status": "error", "message": (
                "frame_range[%d] must be a number; got %r")
                % (index, item), "field": "frame_range"}
        as_float = float(item)
        if not math.isfinite(as_float):
            return {"status": "error", "message": (
                "frame_range[%d] must be a finite number; got %r")
                % (index, item), "field": "frame_range"}
        coerced.append(as_float)
    if coerced[0] > coerced[1]:
        return {"status": "error", "message": (
            "frame_range start (%r) must be <= end (%r)")
            % (coerced[0], coerced[1]),
            "field": "frame_range"}
    if len(coerced) == 3 and coerced[2] <= 0:
        return {"status": "error", "message": (
            "frame_range increment must be > 0; got %r")
            % coerced[2], "field": "frame_range"}
    return {"value": tuple(coerced)}


# ---------------------------------------------------------------------------
# Section 2: policy 校验（Layer 3 / Layer 4 共用）
# ---------------------------------------------------------------------------
def _enforce_policy(renderer, consent_token):
    """在 ``_render_jobs`` 内部统一 policy 校验。

    Args:
        renderer: ``_render_settings._resolve_policy_renderer`` 推断结果。
        consent_token: agent 重调携带的 token（karma 路径需要）。

    Returns:
        ``(decision, payload)``：
        - ``("allow", None)``：继续 render。
        - ``("redirect", dict)``：opengl，必须立即 return dict。
        - ``("interrupt", dict)``：karma 缺 / 错 / 过期 token，必须
          立即 return dict；有效 token 则降级为 ``("allow", None)``。
    """
    if not renderer:
        return ("error", {"status": "error", "message": (
            "unsupported ROP type / engine; cannot map to policy renderer"),
            "renderer": renderer})
    action, payload = _rp.enforce_render_policy(renderer)
    if action == "allow":
        return ("allow", None)
    if action == "redirect":
        return ("redirect", payload)
    if action == "interrupt":
        if consent_token and _rp.consume_consent_token(consent_token):
            return ("allow", None)
        return ("interrupt", payload)
    return ("error", {"status": "error",
                       "message": "unknown render policy action"})


# ---------------------------------------------------------------------------
# Section 3: start_render 入口（Layer 3）
# ---------------------------------------------------------------------------
def start_render(hou, node_path, frame_range=None, consent_token=None):
    """同步启动一次 ``hou.RopNode.render``。

    四层防御（design.md §"安全调用链"）：
    - Layer 1 在 ``houdini_mcp_server.py``（bridge 纯 helper）。
    - Layer 2 在 ``server.py``（handler）。
    - Layer 3 在本函数内：再次 resolve 真实 node / 推断 renderer /
      policy 校验；缺 / 错 / 未知均短路。
    - Layer 4 在 ``_render_node_sync`` 内 ``node.render()`` 紧前。

    Returns:
        dict: ``status="completed"|"failed"`` + ``state / elapsed /
        frame_range``；或 redirect / interrupt / error。响应过
        ``apply_response_cap``。
    """
    frame_check = _coerce_frame_range(frame_range)
    if frame_check.get("status") == "error":
        return frame_check

    resolved = _rset._resolve_rop_node(hou, node_path)
    if resolved.get("status") == "error":
        return resolved
    node = resolved["node"]
    type_name = resolved["type"]
    renderer = _rset._resolve_policy_renderer(node, type_name)

    decision, payload = _enforce_policy(renderer, consent_token)
    if decision == "redirect":
        return cmn.apply_response_cap(payload)
    if decision == "interrupt":
        return cmn.apply_response_cap(payload)
    if decision == "error":
        return cmn.apply_response_cap(payload)

    return _render_node_sync(
        hou, node, type_name, renderer, frame_check["value"],
        consent_token=consent_token)


# ---------------------------------------------------------------------------
# Section 4: node.render 紧前 gate（Layer 4）
# ---------------------------------------------------------------------------
def _render_node_sync(hou, node, type_name, renderer, frame_range_tuple,
                       consent_token=None):
    """Layer 4 入口：``node.render()`` 紧前最后一次 policy 校验。

    任何 redirect / interrupt / error 立即 return；**不**调
    ``node.render()``。仅最终 allow 调一次同步 ``node.render``，
    返 terminal state / elapsed / frame_range。

    Args:
        hou: hou 模块（参数注入）。
        node: hou.RopNode 真实实例。
        type_name: 规范化后的 node type。
        renderer: 推断出的 policy renderer。
        frame_range_tuple: 已校验的 ``(start, end[, inc])`` tuple；
            空 tuple 表示 ROP 自身设置。
        consent_token: 上层已验证的 token；Layer 4 在 ``node.render()``
            紧前再次 consume 验证（fork-render-policy-defense-in-depth
            要求每层都校验，且 sentinel 5 分钟窗口内幂等通过）。

    Returns:
        dict: 响应过 ``apply_response_cap``。
    """
    decision, payload = _enforce_policy(renderer, consent_token)
    if decision == "redirect":
        return cmn.apply_response_cap(payload)
    if decision == "interrupt":
        return cmn.apply_response_cap(payload)
    if decision == "error":
        return cmn.apply_response_cap(payload)

    start = time.time()
    state = "completed"
    error_message = None
    exception_type = None
    try:
        if frame_range_tuple:
            node.render(frame_range=frame_range_tuple)
        else:
            node.render()
    except Exception as error:
        state = "failed"
        error_message = str(error)
        exception_type = error.__class__.__name__
    elapsed = round(time.time() - start, 3)

    result = {
        "status": "success",
        "state": state,
        "elapsed": elapsed,
        "node_path": node.path(),
        "node_type": type_name,
        "renderer": renderer,
        "frame_range": list(frame_range_tuple),
    }
    if state == "failed":
        result["error"] = error_message
        result["exception"] = exception_type
    return cmn.apply_response_cap(result)