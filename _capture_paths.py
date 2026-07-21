"""_capture_paths.py — opera-houdini-mcp 截图 / 渲染临时目录规范（PR 21 / Bug C）。

模块职责：
- 统一所有截图 / 渲染产物的落盘目录：`$TEMP/houdini_mcp/<YYYY-MM-DD>/...`。
  替代散落在 `C:/temp/MCP_CPU_KARMA_*` / `$TEMP/mcp_flipbook_test.png` 等
  unique 路径的旧行为（user spec 硬约束：❌ 不在 unique 目录堆截图）。
- 命名规范：`<HHMMSS>_<scene_basename>_<frame>_<engine>.png`，方便后续
  按时间 / 场景 / 引擎反查。
- 启动时清理 > 7 天的子目录（`_cleanup_old_captures`），避免磁盘膨胀。
  默认保留期 7 天（user spec）。

设计原则：
- hou 通过参数注入；不顶层 import hou，便于纯 Python 单测。
- 测试可显式传入 base_dir 覆盖 `$TEMP`，避免污染用户 home。
- 失败路径同样落盘到 `<date>/failed/<HHMMSS>_..._<engine>_error.png`
  便于事后排查（user spec）。

Args/Returns:
    default_capture_path(hou, pane_type, engine, scene_basename, frame):
        返回默认落盘路径（含目录创建）。scene_basename 来自
        hou.hipFile.basename()（去后缀）。
    cleanup_old_captures(base_dir, max_age_days=7, now=None):
        删除 base_dir 下 mtime > max_age_days 天的子目录。now 为 mock 时间
        （unix timestamp）便于测试。返回删除的子目录数与保留的子目录数。
"""
import os
import time


def _now_ts(now):
    """Return provided `now` or fall back to time.time().

    测试用：传 mock 时间避免依赖 wall clock。
    """
    return now if now is not None else time.time()


def resolve_base_dir(hou=None, fallback=None):
    """解析截图基础目录。

    H22/H21/H20 三态 fallback chain（H22 → H21 → H20 → 无 hou）：
      1. hou.text.expandString("$TEMP")  — H22+ 推荐
         （SideFX 文档：hou.text 模块 H22 引入，替代 hou.expandString）
      2. hou.expandString("$TEMP")       — H21 fallback（H22 已 deprecated）
         （SideFX 文档 hou.expandString：
          "This method is deprecated in favor of hou.text.expandString."）
      3. hou.text / hou.expandString 均不可用 → os.environ 兜底
         （H20 / 测试环境 / hou 未加载场景）

    Args:
        hou: hou 模块（注入）。可为 None（测试或 hou 未加载）。
        fallback: hou 不可用时的回退路径；默认 None → 自动选
                  os.environ.get("TEMP") or "TMP" or "/tmp"。

    Returns:
        str: 基础目录路径。默认 `<TEMP>/houdini_mcp`。
    """
    base = None
    if hou is not None:
        # H22+ 优先 hou.text.expandString（SideFX 文档推荐）
        if hasattr(hou, "text") and hasattr(hou.text, "expandString"):
            try:
                _t = hou.text.expandString("$TEMP")
                if _t:
                    base = _t
            except Exception:
                base = None
        # H21 / H22 兼容：fallback hou.expandString（顶层；H22 已 deprecated
        # 但仍可用；H20 仅有此 API）
        if base is None and hasattr(hou, "expandString"):
            try:
                _t = hou.expandString("$TEMP")
                if _t:
                    base = _t
            except Exception:
                base = None
    if not base:
        if fallback is None:
            fallback = (os.environ.get("TEMP")
                        or os.environ.get("TMP")
                        or "/tmp")
        base = fallback
    return os.path.join(base, "houdini_mcp")


def _date_subdir(now_ts):
    """Return YYYY-MM-DD string from a unix timestamp."""
    return time.strftime("%Y-%m-%d", time.localtime(now_ts))


def default_capture_path(hou=None, pane_type="unknown", engine="capture",
                         scene_basename=None, frame=None, now=None,
                         fallback_base=None):
    """生成默认截图落盘路径（含目录自动创建）。

    路径格式：
        <BASE>/<YYYY-MM-DD>/<HHMMSS>_<scene_basename>_<frame>_<engine>.png

    Args:
        hou: hou 模块（注入）。可为 None（测试场景）。
        pane_type: pane 类型名（暂未纳入文件名，仅留作扩展点）。
        engine: 渲染 / 截图引擎标识（"flipbook" / "karma_cpu" / "opengl" /
                "qt_grab"）。
        scene_basename: 场景文件名（去后缀）。默认 "untitled"。
        frame: 当前帧号。默认 1。
        now: mock 时间戳（unix）。None 用 wall clock。
        fallback_base: 调用方已拼好完整 BASE 的路径（含 houdini_mcp 子目录）。
            传 None 时走 resolve_base_dir（仍拼一次 houdini_mcp）。

    Returns:
        str: 落盘绝对路径（含 .png 扩展名）。目录会被自动创建。
    """
    now_ts = _now_ts(now)
    # caller 显式传 fallback_base 时信任 caller（caller 已拼 houdini_mcp）；
    # 不调 resolve_base_dir，避免重复拼 "houdini_mcp" 造成
    # "<tmp>/houdini_mcp/houdini_mcp/<date>/..." 的双层目录。
    if fallback_base is not None:
        base = fallback_base
    else:
        base = resolve_base_dir(hou=hou, fallback=None)
    date_dir = _date_subdir(now_ts)
    full_dir = os.path.join(base, date_dir)
    os.makedirs(full_dir, exist_ok=True)

    ts = time.strftime("%H%M%S", time.localtime(now_ts))
    scene = scene_basename or "untitled"
    if scene.endswith(".hip") or scene.endswith(".hipnc"):
        scene = scene.rsplit(".", 1)[0]
    frame_str = str(frame) if frame is not None else "1"
    # 清理 scene basename 中的不安全字符
    safe_scene = "".join(c if (c.isalnum() or c in "_-") else "_"
                         for c in scene)
    fname = "{ts}_{scene}_{frame}_{engine}.png".format(
        ts=ts, scene=safe_scene, frame=frame_str, engine=engine)
    return os.path.join(full_dir, fname)


def failed_capture_path(hou=None, pane_type="unknown", engine="capture",
                        scene_basename=None, frame=None, now=None,
                        fallback_base=None):
    """生成失败截图落盘路径（failed/ 子目录）。详见 default_capture_path。"""
    now_ts = _now_ts(now)
    # 同 default_capture_path：caller 传 fallback_base 时信任 caller，
    # 不再走 resolve_base_dir 重复拼 houdini_mcp。
    if fallback_base is not None:
        base = fallback_base
    else:
        base = resolve_base_dir(hou=hou, fallback=None)
    date_dir = _date_subdir(now_ts)
    full_dir = os.path.join(base, date_dir, "failed")
    os.makedirs(full_dir, exist_ok=True)

    ts = time.strftime("%H%M%S", time.localtime(now_ts))
    scene = scene_basename or "untitled"
    if scene.endswith(".hip") or scene.endswith(".hipnc"):
        scene = scene.rsplit(".", 1)[0]
    frame_str = str(frame) if frame is not None else "1"
    safe_scene = "".join(c if (c.isalnum() or c in "_-") else "_"
                         for c in scene)
    fname = "{ts}_{scene}_{frame}_{engine}_error.png".format(
        ts=ts, scene=safe_scene, frame=frame_str, engine=engine)
    return os.path.join(full_dir, fname)


def cleanup_old_captures(base_dir, max_age_days=7, now=None):
    """清理 base_dir 下 mtime > max_age_days 天的子目录。

    仅清理 base_dir 直接子项（YYYY-MM-DD 格式日期目录）；不动 deeper
    的文件层级。保留期默认 7 天（user spec）。

    Args:
        base_dir: 截图基础目录（resolve_base_dir 输出）。
        max_age_days: 保留天数（默认 7）。
        now: mock 当前时间戳（unix）。None 用 wall clock。

    Returns:
        dict: {
            "scanned": int,        # base_dir 直接子项数
            "deleted": int,        # 删除的子目录数
            "kept": int,           # 保留的子目录数
            "errors": list[str],   # 删除失败的原因
        }
    """
    now_ts = _now_ts(now)
    cutoff = now_ts - max_age_days * 86400
    result = {"scanned": 0, "deleted": 0, "kept": 0, "errors": []}
    if not os.path.isdir(base_dir):
        return result
    for name in os.listdir(base_dir):
        path = os.path.join(base_dir, name)
        result["scanned"] += 1
        if not os.path.isdir(path):
            # 文件（非日期子目录）跳过，不在清理范围
            result["kept"] += 1
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError as e:
            result["errors"].append(str(e))
            result["kept"] += 1
            continue
        if mtime < cutoff:
            try:
                # 递归删除整个子目录（含 failed/ 子目录与所有 png）
                import shutil
                shutil.rmtree(path)
                result["deleted"] += 1
            except OSError as e:
                result["errors"].append(str(e))
                result["kept"] += 1
        else:
            result["kept"] += 1
    return result