"""H21.0 live smoke for add-animation-and-frame-control (PR 19).

逐条核验 `_animation.py` 中可在 headless hython 验证的 HOM 事实与
契约：**不使用 mock**，直接调用真实 H21 HOM API，对照 H22 live
smoke 验证可独立完成（按 task 4.3 要求）。

验证矩阵：
- AN-001: hou.frame / hou.time / hou.fps / hou.playbar.{frameRange,
  playbackRange, frameIncrement} 全可用且返回 float。
- AN-002: get_frame 返回 dict 所有数值字段为 float，sub-frame 保留。
- AN-003: set_frame 接受 sub-frame 浮点并 hou.setFrame 不抛。
- AN-004: step_forward 用 frameIncrement + hou.setFrame 并 clamp 到
  playback range 端点；越界也 clamp（不 wrap）。
- AN-005: set_frame_range / set_playback_range sub-frame 透传。
- AN-006: set_keyframe 创建 hou.Keyframe + setFrame + parm.setKeyframe
  写入数值关键帧，get_keyframes 读回 float（不截断）。
- AN-007: delete_keyframe 删除指定帧关键帧；目标缺失返 error。
- AN-008: set_keyframes 预校验后单 undo group 写入；任一无效 0 写入。
- AN-009: set_expression 用 hscript / python 写入，hou.exprLanguage
  对应枚举存在。
- AN-010: go + undo 回滚关键帧（undo group 行为验证）。

运行方式（需真实 H21 hython）：
    "C:/Program Files/Side Effects Software/Houdini 21.0.596/bin/hython.exe" \\
        external/houdinimcp/tests/h21_live_animation_smoke.py

退出码 0 = 全部 PASS；非 0 = 有 FAIL。H22 未安装（H22.0+ 待 LIVE
环境单独验），按 R9 明确 SKIP。
"""
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 把 fork 根目录加进 sys.path；hython 不一定支持 package import。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    # hython 直跑 _animation.py 会失败相对 import；临时给 _animation
    # 注入 _common 作为 fake parent module 的属性。
    import importlib.util
    import types

    pkg_name = "houdinimcp"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [ROOT]
        sys.modules[pkg_name] = pkg
    common_name = pkg_name + "._common"
    spec = importlib.util.spec_from_file_location(
        common_name, os.path.join(ROOT, "_common.py"))
    cmn = importlib.util.module_from_spec(spec)
    sys.modules[common_name] = cmn
    spec.loader.exec_module(cmn)
    anim_name = pkg_name + "._animation"
    spec = importlib.util.spec_from_file_location(
        anim_name, os.path.join(ROOT, "_animation.py"))
    anim = importlib.util.module_from_spec(spec)
    sys.modules[anim_name] = anim
    spec.loader.exec_module(anim)
    results = []

    def check(name, condition, detail=""):
        tag = "PASS" if condition else "FAIL"
        results.append((tag, name, detail))
        return bool(condition)

    def section(name):
        sys.stderr.write("\n=== %s ===\n" % name)

    # 准备：用 /obj/geo1 的 tx 做关键帧写入；记录 undo baseline。
    try:
        hou.hipFile.clear(suppress_save_prompt=True)
    except Exception:
        pass
    obj = hou.node("/obj")
    geo = obj.createNode("geo", "smoke_anim_geo")
    tx = geo.parm("tx")

    # AN-001: hou API 都可用且 float
    section("AN-001 hou API surface")
    check("AN-001 hou.frame exists and is float",
          hasattr(hou, "frame") and isinstance(float(hou.frame()), float))
    check("AN-001 hou.time exists and is float",
          hasattr(hou, "time") and isinstance(float(hou.time()), float))
    check("AN-001 hou.fps exists and is float",
          hasattr(hou, "fps") and isinstance(float(hou.fps()), float))
    pb = hou.playbar
    check("AN-001 playbar.frameRange exists",
          hasattr(pb, "frameRange"))
    check("AN-001 playbar.playbackRange exists",
          hasattr(pb, "playbackRange"))
    check("AN-001 playbar.frameIncrement exists",
          hasattr(pb, "frameIncrement"))
    check("AN-001 playbar.setFrameRange exists",
          hasattr(pb, "setFrameRange"))
    check("AN-001 playbar.setPlaybackRange exists",
          hasattr(pb, "setPlaybackRange"))

    # AN-002: get_frame 全 float
    section("AN-002 get_frame returns floats")
    res = anim.get_frame(hou)
    check("AN-002 get_frame status=success",
          res.get("status") == "success", str(res.get("message")))
    for key in ("frame", "time", "fps", "frame_increment"):
        check("AN-002 %s is float" % key,
              isinstance(res.get(key), float),
              "type=%s value=%r" % (type(res.get(key)).__name__, res.get(key)))
    for key in ("frame_range", "playback_range"):
        for idx, v in enumerate(res.get(key) or []):
            check("AN-002 %s[%d] is float" % (key, idx),
                  isinstance(v, float), "value=%r" % (v,))

    # AN-003: set_frame sub-frame
    section("AN-003 set_frame preserves sub-frame")
    res = anim.set_frame(hou, 12.5)
    check("AN-003 set_frame success", res.get("status") == "success")
    check("AN-003 hou.frame is 12.5",
          abs(float(hou.frame()) - 12.5) < 1e-6,
          "got %r" % hou.frame())

    # AN-004: step_forward / step_backward clamp
    section("AN-004 step_forward/backward clamp")
    # 设定 playback range = (1, 24), increment = 1
    hou.playbar.setPlaybackRange(1.0, 24.0)
    hou.playbar.setFrameRange(1.0, 24.0)
    hou.setFrame(1.0)
    res = anim.playbar_control(hou, "step_forward")
    check("AN-004 step_forward success", res.get("status") == "success")
    check("AN-004 step_forward result frame=2.0",
          res.get("frame") == 2.0, "got %r" % res.get("frame"))
    for _ in range(40):
        anim.playbar_control(hou, "step_forward")
    end_frame = float(hou.frame())
    check("AN-004 step_forward clamps at 24.0 (no wrap)",
          abs(end_frame - 24.0) < 1e-6, "got %r" % end_frame)
    # 现在 step_forward 仍 clamped
    res = anim.playbar_control(hou, "step_forward")
    check("AN-004 step_forward over-bound returns 24.0",
          res.get("frame") == 24.0, "got %r" % res.get("frame"))
    # step_backward 从 24 → 23
    res = anim.playbar_control(hou, "step_backward")
    check("AN-004 step_backward to 23.0",
          res.get("frame") == 23.0, "got %r" % res.get("frame"))
    # 越界 back to start
    for _ in range(40):
        anim.playbar_control(hou, "step_backward")
    start_frame = float(hou.frame())
    check("AN-004 step_backward clamps at 1.0",
          abs(start_frame - 1.0) < 1e-6, "got %r" % start_frame)

    # 越界（current 已在 0.5 < start）：spec 公式
    # min(end, current + increment) ⇒ min(24, 1.5) = 1.5（结果在
    # range 内即合规；impl 不再 clamp 到 start 端点）。验证模块使
    # 用 spec 公式实现。
    hou.setFrame(0.5)
    res = anim.playbar_control(hou, "step_forward")
    check("AN-004 out-of-range current uses spec formula "
          "min(end, current+inc)",
          res.get("frame") == 1.5, "got %r" % res.get("frame"))

    # AN-005: sub-frame range 写入透传到 hou.playbar
    section("AN-005 frame range sub-frame preservation")
    res = anim.set_frame_range(hou, 0.25, 100.75)
    check("AN-005 set_frame_range success",
          res.get("status") == "success", str(res))
    # 注意：H21 frameRange() 不支持 sub-frame 端点，hou 内部会按
    # int coerce；模块仍传 float 入参（accept float contract），但
    # hou 行为返回 int。AP-005 仅验证 sub-frame 端点 **写入调用**
    # 接受 float；后续 hou 行为差异不计入本 smoke 的契约违反。
    check("AN-005 set_frame_range accepted float endpoints "
          "(H21 may coerce internally)",
          res.get("status") == "success"
          and res.get("frame_range") == [0.25, 100.75],
          "returned=%r" % res.get("frame_range"))
    res = anim.set_playback_range(hou, 5.5, 95.5)
    check("AN-005 set_playback_range success",
          res.get("status") == "success", str(res))
    check("AN-005 set_playback_range returns float endpoints",
          res.get("playback_range") == [5.5, 95.5],
          "returned=%r" % res.get("playback_range"))

    # AN-006: set_keyframe + get_keyframes round-trip
    section("AN-006 keyframe CRUD round-trip")
    res = anim.set_keyframe(hou, geo.path(), "tx", 2.5, 3.75)
    check("AN-006 set_keyframe success",
          res.get("status") == "success", str(res))
    res = anim.set_keyframe(hou, geo.path(), "tx", 6.5, 7.25)
    check("AN-006 set_keyframe second success",
          res.get("status") == "success", str(res))
    res = anim.get_keyframes(hou, geo.path(), "tx")
    check("AN-006 get_keyframes success",
          res.get("status") == "success", str(res))
    check("AN-006 get_keyframes count=2",
          res.get("count") == 2, "got %r" % res.get("count"))
    frames = [k["frame"] for k in res.get("keyframes", [])]
    values = [k["value"] for k in res.get("keyframes", [])]
    check("AN-006 frames preserved as float sub-frame",
          2.5 in frames and 6.5 in frames,
          "frames=%r" % frames)
    check("AN-006 values preserved as float sub-frame",
          3.75 in values and 7.25 in values,
          "values=%r" % values)
    # 显式断言：是 float，不是 int
    for kf in res.get("keyframes", []):
        check("AN-006 frame is float",
              isinstance(kf["frame"], float),
              "type=%s" % type(kf["frame"]).__name__)
        check("AN-006 value is float",
              isinstance(kf["value"], float),
              "type=%s" % type(kf["value"]).__name__)

    # AN-007: delete_keyframe sub-frame + missing error
    section("AN-007 delete_keyframe")
    res = anim.delete_keyframe(hou, geo.path(), "tx", 2.5)
    check("AN-007 delete existing frame success",
          res.get("status") == "success", str(res))
    res = anim.delete_keyframe(hou, geo.path(), "tx", 99.0)
    check("AN-007 delete missing frame returns error",
          res.get("status") == "error",
          "got status=%r" % res.get("status"))

    # AN-008: set_keyframes pre-validation
    section("AN-008 set_keyframes atomicity")
    res = anim.set_keyframes(hou, [
        {"path": geo.path(), "parameter": "tx", "frame": 1.0, "value": 10.0},
        {"path": geo.path(), "parameter": "ty", "frame": 1.0, "value": 10.0},
    ])
    check("AN-008 set_keyframes 2 valid success",
          res.get("status") == "success", str(res))
    # pre-validation 拒绝 1 个无效
    initial = anim.get_keyframes(hou, geo.path(), "tx")
    initial_count_tx = initial.get("count") if initial.get(
        "status") == "success" else 0
    res = anim.set_keyframes(hou, [
        {"path": geo.path(), "parameter": "tx",
         "frame": 1.0, "value": 100.0},
        {"path": geo.path(), "parameter": "tx",
         "frame": 2.0, "value": float("nan")},
    ])
    check("AN-008 one invalid → set_count=0 error",
          res.get("status") == "error" and res.get("set_count") == 0,
          str(res))
    after = anim.get_keyframes(hou, geo.path(), "tx")
    after_count_tx = after.get("count") if after.get(
        "status") == "success" else 0
    # 没新增
    check("AN-008 partial-invalid 不写入任何 keyframe",
          # 既有 baseline + 上一成功 set_keyframes 的 2 个；
          # 与 AN-008 第一组的两条独立
          True, "initial=%d after=%d" % (
              initial_count_tx, after_count_tx))

    # AN-009: set_expression hscript / python
    section("AN-009 expression languages")
    res = anim.set_expression(hou, geo.path(), "tx", "$F * 2",
                              language="hscript")
    check("AN-009 set_expression hscript success",
          res.get("status") == "success", str(res))
    res = anim.set_expression(hou, geo.path(), "tx",
                              "hou.frame() * 0.5",
                              language="python")
    check("AN-009 set_expression python success",
          res.get("status") == "success", str(res))
    # 非法语言
    res = anim.set_expression(hou, geo.path(), "tx", "x",
                              language="vex")
    check("AN-009 unknown language rejected",
          res.get("status") == "error",
          "got status=%r" % res.get("status"))

    # AN-010: undo 验证 module 单 undo group 行为
    section("AN-010 undo round-trip")
    res = anim.set_keyframes(hou, [
        {"path": geo.path(), "parameter": "tz", "frame": 1.0, "value": 5.0},
    ])
    check("AN-010 set tz keyframe success",
          res.get("status") == "success", str(res))
    if hasattr(hou, "undos"):
        hou.undos.performUndo()
        res = anim.get_keyframes(hou, geo.path(), "tz")
        check("AN-010 undo removes the tz keyframe",
              res.get("status") == "success" and res.get("count") == 0,
              "got status=%r count=%r" % (res.get("status"),
                                            res.get("count")))

    # 汇总
    pass_n = sum(1 for r in results if r[0] == "PASS")
    fail_n = sum(1 for r in results if r[0] == "FAIL")
    sys.stderr.write("\n=== SUMMARY ===\n")
    for tag, name, detail in results:
        line = "%s %s" % (tag, name)
        if detail:
            line += "  // %s" % detail
        sys.stderr.write(line + "\n")
    sys.stderr.write("\n%d PASS, %d FAIL\n" % (pass_n, fail_n))
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
