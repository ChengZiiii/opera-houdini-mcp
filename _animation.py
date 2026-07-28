"""_animation.py — opera-houdini-mcp 动画与帧控制工具（PR 19）。

提供 10 个窄接口：``get_frame``、``set_frame``、``set_frame_range``、
``set_playback_range``、``set_keyframe``、``set_keyframes``、
``delete_keyframe``、``get_keyframes``、``playbar_control``、
``set_expression``。所有 frame / range 端点 / keyframe frame / value
均按 JSON number → Python float（拒绝 bool / NaN / ±inf / 非数值），
返回保留浮点，不做 ``int()`` 截断，sub-frame 精度对 Houdini
timeline 一致。

模块职责：
- hou 通过第一参数注入，顶层 ``import hou``。便于单测按需替换 mock。
- 全部返回结构化 dict（``status: success/error``）；异常一律捕获
  并降级为 error dict，**不抛异常**到 server wrapper。
- ``set_keyframes`` 预校验完整列表后才写入；任一项无效则整调用
  失败，避免「status=success 却只写一半」。全部有效时在单个
  ``hou.undos.group`` 中完成（模块级责任）。
- ``step_forward`` / ``step_backward`` 仅通过
  ``hou.setFrame(current ± hou.playbar.frameIncrement())`` 路径
  并 clamp 到 playback range 闭区间；不引入其他 step helper。
- ``playbar_control`` 的 ``play`` / ``reverse`` / ``stop`` 直接调
  SideFX HOM 同名方法，无 fallback 假设。
- ``goto_start`` / ``goto_end`` 直接设 playback range 端点。
- ``set_expression`` 仅接受 ``hscript`` / ``python``，映射到对应
  ``hou.exprLanguage``，不引入第三种语言。

设计依据：
- D1（number 校验与返回）：bool / NaN / ±inf / 非数值一律拒绝；
  frame / fps / time / range / keyframe value 统一 ``float``。
- D2（step 与边界）：读取 current + increment + playback range，
  要求 increment 为有限正数，目标帧分别为
  ``min(end, current + increment)`` /
  ``max(start, current - increment)``；越界也 clamp 到最近端点。
- D3（读写与 undo 分类）：get_* 只读；keyframe / range / expression
  走 ``MUTATING_COMMANDS``；``set_frame`` / ``playbar_control`` 走
  ``NO_UNDO_COMMANDS``，batch 中在 undo group 外执行。
- D4（批量关键帧）：预校验后单 undo group 写入，错误列表同样受
  response cap 限制（由 server wrapper 层负责）。

约束：
- hou 第一参数注入；不新增 pip 依赖。
- 4 空格缩进 / snake_case / 中文 docstring / 无 f-string / 无类型注解。
- 字符串参数 / 无 value 的 keyframe 明确拒绝。
- 错误含 ``status=error`` + ``message`` + 可选 ``field``，供 bridge
  透传到 AI 客户端。
"""
import math
import os

from . import _common as cmn


# ---------------------------------------------------------------------------
# Section 1: 常量
# ---------------------------------------------------------------------------
# Brief 19.1：expression 仅支持 hscript / python 两种语言，映射到
# ``hou.exprLanguage.Hscript`` / ``hou.exprLanguage.Python``。其他值一律
# 在 set_expression 入口拒绝并返回 status=error。
VALID_EXPRESSION_LANGUAGES = ("hscript", "python")

# playbar_control 接受的 action 取值（含 step / goto 子动作）；
# 错误 action 走 default 分支并返回 status=error。
VALID_PLAYBAR_ACTIONS = (
    "play", "reverse", "stop",
    "step_forward", "step_backward",
    "goto_start", "goto_end",
)


# ---------------------------------------------------------------------------
# Section 2: 内部 helper
# ---------------------------------------------------------------------------
def _coerce_finite_number(name, value):
    """接受 int / float、拒 bool / 非数值 / NaN / ±inf；返回 ``{"value": float}``。

    工具说明：``bool`` 是 ``int`` 的子类，``isinstance(True, int)`` 为 True，
    因此必须**先**判 bool 再判数值类型。返回值先转 ``float``，再过
    ``math.isfinite`` 同时排除 NaN（``NaN != NaN``）和 ±inf。

    返回 dict 而非抛异常，与本模块其它函数一致的「错误降级」契约。
    """
    if isinstance(value, bool):
        return {"status": "error", "message": (
            "must be a JSON number; bool is not accepted"), "field": name}
    if not isinstance(value, (int, float)):
        return {"status": "error", "message": (
            "must be a JSON number (int or float)"), "field": name}
    as_float = float(value)
    if not math.isfinite(as_float):
        return {"status": "error", "message": (
            "must be a finite number; NaN/inf not accepted"),
            "field": name}
    return {"value": as_float}


def _coerce_pair(name_a, name_b, value_a, value_b):
    """校验两个端点；要求均为有限浮点且 ``value_a <= value_b``。

    用于 ``set_frame_range`` / ``set_playback_range``，start 不得大于
    end；端点相等合法（单帧范围）。
    """
    left = _coerce_finite_number(name_a, value_a)
    if left.get("status") == "error":
        return left
    right = _coerce_finite_number(name_b, value_b)
    if right.get("status") == "error":
        return right
    if left["value"] > right["value"]:
        return {"status": "error", "message": (
            "%s (%r) must be <= %s (%r)")
            % (name_a, left["value"], name_b, right["value"])}
    return {"start": left["value"], "end": right["value"]}


def _resolve_parm(hou, path, parameter):
    """通过 hou.node(path).parm(parameter) 解析目标 parm；失败返 error dict。

    节点路径不存在或 parm 不存在时返回 ``{"status": "error", ...}``
    而非抛异常，便于 server wrapper 透传。
    """
    if not isinstance(path, str) or not path.strip():
        return {"status": "error", "message": (
            "path must be a non-empty string"), "field": "path"}
    if not isinstance(parameter, str) or not parameter.strip():
        return {"status": "error", "message": (
            "parameter must be a non-empty string"), "field": "parameter"}
    node = hou.node(path)
    if node is None:
        return {"status": "error", "message": (
            "node not found at path %r") % path, "field": "path"}
    parm = node.parm(parameter)
    if parm is None:
        return {"status": "error", "message": (
            "parameter %r not found on node %r")
            % (parameter, path), "field": "parameter"}
    return {"parm": parm, "node_path": path, "parameter": parameter}


def _capture_undo_group(hou, label):
    """返回 ``hou.undos.group(label)`` 上下文或 None（HOM 不可用时）。

    单测与无 hou 环境（conftest stub hou）可能没有 ``undos`` 字段；
    此时返 None 由调用方跳过 ``with``。
    """
    undos = getattr(hou, "undos", None)
    if undos is None:
        return None
    return undos.group(label)


def _unwrap_undo_group(group):
    if group is None:
        return
    try:
        group.__enter__()
    except Exception:
        return


def _close_undo_group(group):
    if group is None:
        return
    try:
        group.__exit__(None, None, None)
    except Exception:
        return


# ---------------------------------------------------------------------------
# Section 3: get_frame — 时间线状态只读
# ---------------------------------------------------------------------------
def get_frame(hou):
    """读取当前 frame / time / fps / 三组 range / increment，全部 float。

    HOM 契约：
    - ``hou.frame()`` → float（H21+ 真实签名支持 sub-frame）
    - ``hou.time()`` → float 秒
    - ``hou.fps()`` → float（H21+ hou.fps 为函数）
    - ``hou.playbar.frameRange()`` → (float start, float end)
    - ``hou.playbar.playbackRange()`` → (float start, float end)
    - ``hou.playbar.frameIncrement()`` → float

    任一 hou 调用抛异常时返回 ``{"status": "error", ...}``，不抛到上层。
    返回 ``status=success`` 时字段全 float。
    """
    try:
        current_frame = float(hou.frame())
        current_time = float(hou.time())
        current_fps = float(hou.fps())
        global_range = hou.playbar.frameRange()
        playback_range = hou.playbar.playbackRange()
        increment = float(hou.playbar.frameIncrement())
    except Exception as error:
        return {"status": "error", "message": (
            "failed to read playbar state: %s")
            % error, "exception": error.__class__.__name__}
    return {
        "status": "success",
        "frame": current_frame,
        "time": current_time,
        "fps": current_fps,
        "frame_range": [float(global_range[0]), float(global_range[1])],
        "playback_range": [float(playback_range[0]),
                            float(playback_range[1])],
        "frame_increment": increment,
    }


# ---------------------------------------------------------------------------
# Section 4: set_frame — 运行态时间线写（no-undo）
# ---------------------------------------------------------------------------
def set_frame(hou, frame):
    """写入当前帧；frame 转 float 后调 ``hou.setFrame(float(frame))``。

    设计依据 R10：``set_frame`` 是运行态时间线写，不进入 undo group，
    调用方（server batch）必须在 undo segment 外执行。

    校验：bool / 非数值 / NaN / ±inf 一律拒绝。float 后的 sub-frame
    值透传给 hou，不截断。
    """
    coerced = _coerce_finite_number("frame", frame)
    if coerced.get("status") == "error":
        return coerced
    target = coerced["value"]
    try:
        hou.setFrame(target)
    except Exception as error:
        return {"status": "error", "message": (
            "hou.setFrame failed: %s")
            % error, "exception": error.__class__.__name__}
    return {"status": "success", "frame": target}


# ---------------------------------------------------------------------------
# Section 5: set_frame_range / set_playback_range — 全局 / 回放范围
# ---------------------------------------------------------------------------
def set_frame_range(hou, start, end):
    """写入全局 frame range；调 ``hou.playbar.setFrameRange``。

    要求 start / end 均为有限浮点且 start <= end。end 为 float
    保留 sub-frame。
    """
    pair = _coerce_pair("start", "end", start, end)
    if pair.get("status") == "error":
        return pair
    try:
        hou.playbar.setFrameRange(pair["start"], pair["end"])
    except Exception as error:
        return {"status": "error", "message": (
            "hou.playbar.setFrameRange failed: %s")
            % error, "exception": error.__class__.__name__}
    return {"status": "success",
            "frame_range": [pair["start"], pair["end"]]}


def set_playback_range(hou, start, end):
    """写入 playback range；调 ``hou.playbar.setPlaybackRange``。"""
    pair = _coerce_pair("start", "end", start, end)
    if pair.get("status") == "error":
        return pair
    try:
        hou.playbar.setPlaybackRange(pair["start"], pair["end"])
    except Exception as error:
        return {"status": "error", "message": (
            "hou.playbar.setPlaybackRange failed: %s")
            % error, "exception": error.__class__.__name__}
    return {"status": "success",
            "playback_range": [pair["start"], pair["end"]]}


# ---------------------------------------------------------------------------
# Section 6: 关键帧 CRUD
# ---------------------------------------------------------------------------
def set_keyframe(hou, path, parameter, frame, value):
    """单关键帧写入；``parm.setKeyframe(hou.Keyframe(float(value)))``
    且 ``Keyframe.setFrame(float(frame))``。
    """
    frame_check = _coerce_finite_number("frame", frame)
    if frame_check.get("status") == "error":
        return frame_check
    value_check = _coerce_finite_number("value", value)
    if value_check.get("status") == "error":
        return value_check
    parm_info = _resolve_parm(hou, path, parameter)
    if parm_info.get("status") == "error":
        return parm_info
    parm = parm_info["parm"]
    try:
        keyframe = hou.Keyframe(value_check["value"])
        keyframe.setFrame(frame_check["value"])
        parm.setKeyframe(keyframe)
    except Exception as error:
        return {"status": "error", "message": (
            "setKeyframe failed: %s")
            % error, "exception": error.__class__.__name__}
    return {
        "status": "success",
        "node_path": parm_info["node_path"],
        "parameter": parm_info["parameter"],
        "frame": frame_check["value"],
        "value": value_check["value"],
    }


def get_keyframes(hou, path, parameter):
    """读取 parm 的全部关键帧；list 中每个 item 为 ``{"frame": float, "value": float}``。

    不做 ``int()`` 截断；空关键帧列表返回 ``keyframes=[]``。
    字符串 parm 调 ``keyframes()`` 在 HOM 内可能抛 ``TypeError``，本函数
    同样捕获降级为 error dict。
    """
    parm_info = _resolve_parm(hou, path, parameter)
    if parm_info.get("status") == "error":
        return parm_info
    parm = parm_info["parm"]
    try:
        keys = list(parm.keyframes())
    except Exception as error:
        return {"status": "error", "message": (
            "keyframes() failed: %s")
            % error, "exception": error.__class__.__name__}
    items = []
    for index, key in enumerate(keys):
        try:
            frame = float(key.frame())
            value = float(key.value())
        except Exception as error:
            return {"status": "error", "message": (
                "keyframe %d has no numeric frame/value: %s")
                % (index, error),
                "exception": error.__class__.__name__}
        items.append({"frame": frame, "value": value})
    return {
        "status": "success",
        "node_path": parm_info["node_path"],
        "parameter": parm_info["parameter"],
        "keyframes": items,
        "count": len(items),
    }


def delete_keyframe(hou, path, parameter, frame):
    """删除指定帧的关键帧；通过 ``parm.deleteKeyframeAtFrame(float(frame))``。

    设计 D4：目标是 sub-frame 精确删除，因此 frame 接受 float。
    语义契约：目标帧不存在时返回 status=error（"no keyframe found"），
    不写。流程先读 ``parm.keyframes()`` 预检目标帧是否存在；存在则
    ``deleteKeyframeAtFrame``，再读一次确认已删除；任一异常降级
    error dict。
    """
    frame_check = _coerce_finite_number("frame", frame)
    if frame_check.get("status") == "error":
        return frame_check
    parm_info = _resolve_parm(hou, path, parameter)
    if parm_info.get("status") == "error":
        return parm_info
    parm = parm_info["parm"]
    target = frame_check["value"]
    epsilon = max(abs(target) * 1e-6, 1e-6)

    # 预检：目标帧必须存在
    try:
        existing = [float(k.frame()) for k in parm.keyframes()]
    except Exception as error:
        return {"status": "error", "message": (
            "keyframes() read failed: %s")
            % error, "exception": error.__class__.__name__}
    if not any(abs(k - target) <= epsilon for k in existing):
        return {"status": "error", "message": (
            "no keyframe found at frame %r on %s.%s")
            % (target, parm_info["node_path"], parm_info["parameter"])}

    try:
        parm.deleteKeyframeAtFrame(target)
    except Exception as error:
        return {"status": "error", "message": (
            "deleteKeyframeAtFrame failed: %s")
            % error, "exception": error.__class__.__name__}
    # 二次验证：frame 仍存在 → 报错
    try:
        remaining = [float(k.frame()) for k in parm.keyframes()]
    except Exception:
        remaining = []
    still_present = any(
        abs(existing - target) <= epsilon for existing in remaining)
    if still_present:
        return {"status": "error", "message": (
            "deleteKeyframeAtFrame did not remove frame %r on %s.%s")
            % (target, parm_info["node_path"], parm_info["parameter"])}
    return {
        "status": "success",
        "node_path": parm_info["node_path"],
        "parameter": parm_info["parameter"],
        "frame": target,
    }


def set_keyframes(hou, keyframes):
    """批量写入：预校验全部 → 任一项无效整调用失败；全部有效时单 undo group。

    输入 ``keyframes`` 是 list，每项 dict 至少含
    ``path`` / ``parameter`` / ``frame`` / ``value`` 四项；缺任一项视为
    无效。所有数值校验与 ``set_keyframe`` 单调用相同（拒绝 bool / NaN /
    ±inf / 非数值）。

    任一项无效 → 返回 ``status=error`` + ``error_index`` 与
    ``set_count=0``，不写任何关键帧。全部有效 → 在 ``hou.undos.group``
    内逐项写入；返回 ``set_count``。

    单调约束：写入顺序与输入顺序一致；HOM ``setKeyframe`` 接受重复同
    帧关键帧（生成新 keyframe），因此不去重；由调用方控制。
    """
    if not isinstance(keyframes, list):
        return {"status": "error", "message": (
            "keyframes must be a list of objects"), "field": "keyframes"}
    if not keyframes:
        return {"status": "error", "message": (
            "keyframes must be a non-empty list"), "field": "keyframes"}

    # Phase 1：预校验全部项（field 校验 + 端点校验 + 节点 / parm 解析）
    validated = []
    for index, entry in enumerate(keyframes):
        if not isinstance(entry, dict):
            return {"status": "error", "message": (
                "keyframes[%d] must be an object") % index,
                "error_index": index, "set_count": 0}
        path = entry.get("path")
        parameter = entry.get("parameter")
        frame = entry.get("frame")
        value = entry.get("value")
        if not isinstance(path, str) or not path.strip():
            return {"status": "error", "message": (
                "keyframes[%d].path must be a non-empty string")
                % index, "error_index": index, "set_count": 0}
        if not isinstance(parameter, str) or not parameter.strip():
            return {"status": "error", "message": (
                "keyframes[%d].parameter must be a non-empty string")
                % index, "error_index": index, "set_count": 0}
        frame_check = _coerce_finite_number("frame", frame)
        if frame_check.get("status") == "error":
            return {"status": "error", "message": (
                "keyframes[%d].frame invalid: %s")
                % (index, frame_check.get("message")),
                "error_index": index, "set_count": 0,
                "field": "frame"}
        value_check = _coerce_finite_number("value", value)
        if value_check.get("status") == "error":
            return {"status": "error", "message": (
                "keyframes[%d].value invalid: %s")
                % (index, value_check.get("message")),
                "error_index": index, "set_count": 0,
                "field": "value"}
        parm_info = _resolve_parm(hou, path, parameter)
        if parm_info.get("status") == "error":
            return {"status": "error", "message": (
                "keyframes[%d]: %s")
                % (index, parm_info.get("message")),
                "error_index": index, "set_count": 0}
        validated.append({
            "parm": parm_info["parm"],
            "node_path": parm_info["node_path"],
            "parameter": parm_info["parameter"],
            "frame": frame_check["value"],
            "value": value_check["value"],
        })

    # Phase 2：预校验全部通过；进入单 undo group 写入。
    group = _capture_undo_group(hou, "MCP: set_keyframes")
    if group is not None:
        try:
            group.__enter__()
        except Exception as error:
            return {"status": "error", "message": (
                "failed to open undo group: %s")
                % error, "exception": error.__class__.__name__}
    errors = []
    written = 0
    try:
        for index, item in enumerate(validated):
            try:
                keyframe = hou.Keyframe(item["value"])
                keyframe.setFrame(item["frame"])
                item["parm"].setKeyframe(keyframe)
                written += 1
            except Exception as error:
                errors.append({"index": index,
                                "message": str(error),
                                "exception": error.__class__.__name__})
    finally:
        if group is not None:
            try:
                group.__exit__(None, None, None)
            except Exception:
                pass
    if errors:
        return {"status": "error", "message": (
            "set_keyframes wrote %d of %d before error")
            % (written, len(validated)),
            "set_count": written,
            "requested": len(validated),
            "errors": errors}
    return {
        "status": "success",
        "set_count": written,
        "requested": len(validated),
    }


# ---------------------------------------------------------------------------
# Section 7: playbar_control — 播放 / 步进 / 跳转
# ---------------------------------------------------------------------------
def _step_target(hou, direction):
    """step_forward / step_backward 的内部 helper。

    返回 ``{"frame": float}`` 或 error。设计 D2：当前帧 + increment 且
    clamp 到 playback range 闭区间；increment 非有限正数 / range 不可用
    → error，不动 ``hou.setFrame``。
    """
    if direction not in ("forward", "backward"):
        return {"status": "error", "message": (
            "internal: direction must be forward or backward")}
    try:
        current = float(hou.frame())
        increment = float(hou.playbar.frameIncrement())
        playback_start, playback_end = hou.playbar.playbackRange()
        playback_start = float(playback_start)
        playback_end = float(playback_end)
    except Exception as error:
        return {"status": "error", "message": (
            "failed to read playbar state: %s")
            % error, "exception": error.__class__.__name__}
    if (not math.isfinite(increment)) or increment <= 0:
        return {"status": "error", "message": (
            "frame_increment must be a finite positive number; got %r")
            % increment}
    if (not math.isfinite(playback_start)
            or not math.isfinite(playback_end)
            or playback_start > playback_end):
        return {"status": "error", "message": (
            "playback_range must be a finite ordered pair; got (%r, %r)")
            % (playback_start, playback_end)}
    if direction == "forward":
        target = min(playback_end, current + increment)
    else:
        target = max(playback_start, current - increment)
    return {"frame": float(target), "current": current,
            "playback_range": [playback_start, playback_end],
            "increment": increment}


def playbar_control(hou, action):
    """播放控制：``play`` / ``reverse`` / ``stop`` /
    ``step_forward`` / ``step_backward`` / ``goto_start`` / ``goto_end``。

    ``play`` / ``reverse`` / ``stop`` 直接调 SideFX HOM 同名方法；运行态
    写，不进入 undo group（设计 D3）。

    ``step_forward`` / ``step_backward`` 仅通过
    ``hou.setFrame(current ± hou.playbar.frameIncrement())`` 路径
    并 clamp 到 playback range 闭区间（设计 D2）；increment 或 range
    无效 → error 且**不**调 ``hou.setFrame``。

    ``goto_start`` / ``goto_end`` 设 playback range 端点；端点不可用
    → error。
    """
    if not isinstance(action, str) or action not in VALID_PLAYBAR_ACTIONS:
        return {"status": "error", "message": (
            "action must be one of %r; got %r")
            % (list(VALID_PLAYBAR_ACTIONS), action), "field": "action"}

    if action == "play":
        try:
            hou.playbar.play()
        except Exception as error:
            return {"status": "error", "message": (
                "hou.playbar.play failed: %s")
                % error, "exception": error.__class__.__name__}
        return {"status": "success", "action": "play"}
    if action == "reverse":
        try:
            hou.playbar.reverse()
        except Exception as error:
            return {"status": "error", "message": (
                "hou.playbar.reverse failed: %s")
                % error, "exception": error.__class__.__name__}
        return {"status": "success", "action": "reverse"}
    if action == "stop":
        try:
            hou.playbar.stop()
        except Exception as error:
            return {"status": "error", "message": (
                "hou.playbar.stop failed: %s")
                % error, "exception": error.__class__.__name__}
        return {"status": "success", "action": "stop"}

    if action in ("step_forward", "step_backward"):
        direction = ("forward" if action == "step_forward"
                     else "backward")
        target = _step_target(hou, direction)
        if target.get("status") == "error":
            return target
        try:
            hou.setFrame(target["frame"])
        except Exception as error:
            return {"status": "error", "message": (
                "hou.setFrame failed: %s")
                % error, "exception": error.__class__.__name__}
        return {
            "status": "success",
            "action": action,
            "frame": target["frame"],
        }

    # goto_start / goto_end
    try:
        playback_start, playback_end = hou.playbar.playbackRange()
        start = float(playback_start)
        end = float(playback_end)
    except Exception as error:
        return {"status": "error", "message": (
            "failed to read playback range: %s")
            % error, "exception": error.__class__.__name__}
    if not (math.isfinite(start) and math.isfinite(end)):
        return {"status": "error", "message": (
            "playback_range endpoints must be finite; got (%r, %r)")
            % (start, end)}
    target = start if action == "goto_start" else end
    try:
        hou.setFrame(target)
    except Exception as error:
        return {"status": "error", "message": (
            "hou.setFrame failed: %s")
            % error, "exception": error.__class__.__name__}
    return {"status": "success", "action": action, "frame": float(target)}


# ---------------------------------------------------------------------------
# Section 8: set_expression — 参数表达式（可撤销写）
# ---------------------------------------------------------------------------
def set_expression(hou, path, parameter, expression,
                   language="hscript"):
    """写入 parm 表达式；只接受 ``hscript`` / ``python`` 两种语言。

    设计：表达式**是参数通道持久写**，与 ``set_keyframe`` 同属
    ``MUTATING_COMMANDS``，**不得归类为只读或 no-undo**（设计 D3）。

    校验：
    - language 必须在 ``VALID_EXPRESSION_LANGUAGES`` 内；
    - expression 必须为非空字符串；
    - 节点与 parm 必须可解析，失败返 error。
    """
    if not isinstance(expression, str) or not expression.strip():
        return {"status": "error", "message": (
            "expression must be a non-empty string"),
            "field": "expression"}
    if not isinstance(language, str) or language not in VALID_EXPRESSION_LANGUAGES:
        return {"status": "error", "message": (
            "language must be one of %r; got %r")
            % (list(VALID_EXPRESSION_LANGUAGES), language),
            "field": "language"}

    parm_info = _resolve_parm(hou, path, parameter)
    if parm_info.get("status") == "error":
        return parm_info
    parm = parm_info["parm"]

    # hou.exprLanguage 取值因 Houdini 版本而异（H21 有 Hscript / Python）。
    # 不依赖具体符号枚举值，按字符串名查属性；查不到时回退错误。
    expr_language = getattr(hou, "exprLanguage", None)
    if expr_language is None:
        return {"status": "error", "message": (
            "hou.exprLanguage is unavailable in this Houdini build")}
    target_lang = getattr(expr_language,
                           "Hscript" if language == "hscript"
                           else "Python", None)
    if target_lang is None:
        return {"status": "error", "message": (
            "language %r is not available in hou.exprLanguage")
            % language}

    try:
        parm.setExpression(expression, target_lang)
    except Exception as error:
        return {"status": "error", "message": (
            "parm.setExpression failed: %s")
            % error, "exception": error.__class__.__name__}
    return {
        "status": "success",
        "node_path": parm_info["node_path"],
        "parameter": parm_info["parameter"],
        "expression": expression,
        "language": language,
    }
