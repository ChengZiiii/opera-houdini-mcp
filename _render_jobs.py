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
  handle；调用完成即返回 terminal result。perf-mcp-round3 §5 的
  ``background=True`` 异步路径同样遵守 D1：主线程只做
  ``subprocess.Popen`` 派生 detached hython 子进程，**不**触任何 HOM
  渲染调用。H21.0.596 实测矩阵（2026-09-10 编排者 hython 直测）：
  ``RopNode.render(background=True)`` 对 ifd / opengl / karmarender 三
  类型全部 ``TypeError``（H21 无 background kwarg）；无
  ``renderThreaded``；hscript ``render`` 无后台标志——**H21 没有任何
  会话内异步渲染 API，异步只能真出进程**。
- D2（frame_range）：仅 2 或 3 个 number；不允许 list 长度 1 / 4+；
  缺省传空 tuple，让 ROP 自身设置生效。
- D3（policy 复用）：所有层都通过 ``_render_policy`` 既有函数验证；
  ``consume_consent_token`` 已是 fork-render-policy-defense-in-depth
  修复后的幂等语义（5 分钟窗口内允许多层重放）。``background`` 参数
  MUST NOT 影响任何层的 policy 判定，仅在四层全部 allow 后决定执行
  模式（同步 ``node.render`` 或派生子进程）。
- D4（Layer 4 时序）：必须在 ``node.render()`` 之前，**不**调用任何
  render 副作用。

background 模式契约（perf-mcp-round3 spec「同步渲染与 frame range」
/「progress handle 生命周期」）：
- 前置：``hou.hipFile.path()`` 非空且非 untitled——子进程渲染的是
  **磁盘上已保存的快照**，MUST NOT 自动保存/另存用户场景；未保存返
  ``background_requires_saved_hip`` 结构化 error 提示先调 save_scene。
- 成功响应：``state="launched_background"`` + ``pid`` + ``log_path``
  + ``output_paths``（ROP 白名单 parm 尽力读取）+ ``monitor_hint`` +
  ``elapsed`` + ``hip_snapshot_path``；不签发 job handle / registry /
  TTL / callback，运行中可观测性由 bridge-only ``monitor_render``
  best-effort 提供。

约束：
- hou 通过第一参数注入；顶层不 ``import hou``。
- 不签发 progress handle；不维护 job registry / callback / TTL。
- 4 空格缩进 / snake_case / 中文 docstring / 无 f-string / 无类型注解。
- 所有返回 dict 走 ``apply_response_cap``。
"""
import json
import math
import os
import subprocess
import sys
import time

from . import _common as cmn
from . import _render_policy as _rp
from . import _render_settings as _rset
from . import _capture_paths as _cpaths


# Windows detach 标志（subprocess 在 POSIX 平台不导出这两个名字，
# getattr 兜底为文档值：DETACHED_PROCESS=0x8、CREATE_NEW_PROCESS_GROUP
# =0x200，与 Win32 API 常量一致）。
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

# background 模式 output_paths 尽力读取的白名单 parm（design §5：
# ifd→vm_picture、karmarender→picture；opengl 理论上到不了 background
# 分支，列出仅为完整性）。eval() 会展开 $HIP/$F4 等变量。
_BG_OUTPUT_PARMS = {
    "ifd": ("vm_picture", "picture"),
    "karmarender": ("picture",),
    "opengl": ("picture",),
}


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
def start_render(hou, node_path, frame_range=None, consent_token=None,
                 background=False):
    """同步（或 background 派生）启动一次 ``hou.RopNode.render``。

    四层防御（design.md §"安全调用链"）：
    - Layer 1 在 ``houdini_mcp_server.py``（bridge 纯 helper）。
    - Layer 2 在 ``server.py``（handler）。
    - Layer 3 在本函数内：再次 resolve 真实 node / 推断 renderer /
      policy 校验；缺 / 错 / 未知均短路。
    - Layer 4 在 ``_render_node_sync`` 内 ``node.render()`` 紧前。

    ``background`` MUST NOT 影响任何层 policy 判定（spec）；缺省
    ``False`` 时本函数行为与 perf-mcp-round3 之前逐字节一致。

    Returns:
        dict: 同步路径 ``status="completed"|"failed"`` + ``state /
        elapsed / frame_range``；background 路径
        ``state="launched_background"`` + ``pid / log_path /
        output_paths / monitor_hint / elapsed / hip_snapshot_path``；
        或 redirect / interrupt / error。响应过
        ``apply_response_cap``。
    """
    if not isinstance(background, bool):
        return {"status": "error", "message": (
            "background must be a boolean; got %r") % background,
            "field": "background"}

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
        consent_token=consent_token, background=background)


# ---------------------------------------------------------------------------
# Section 4: node.render 紧前 gate（Layer 4）
# ---------------------------------------------------------------------------
def _render_node_sync(hou, node, type_name, renderer, frame_range_tuple,
                      consent_token=None, background=False):
    """Layer 4 入口：``node.render()`` 紧前最后一次 policy 校验。

    任何 redirect / interrupt / error 立即 return；**不**调
    ``node.render()``。仅最终 allow 时按 ``background`` 分流：
    ``False``（缺省）调一次同步 ``node.render`` 返 terminal state /
    elapsed / frame_range（行为与 perf-mcp-round3 之前逐字节一致）；
    ``True`` 派生 detached hython 子进程（见
    ``_render_node_background``）。

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
        background: 仅在四层 policy 全部 allow 后决定执行模式；
            MUST NOT 影响本层 policy 判定（spec 硬约束）。

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

    if background:
        return _render_node_background(
            hou, node, type_name, renderer, frame_range_tuple)

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


# ---------------------------------------------------------------------------
# Section 5: background 模式 — detached hython 子进程渲染器
# （perf-mcp-round3 §5；调用方保证四层 policy 已全部 allow）
# ---------------------------------------------------------------------------
# 子进程 -c 脚本。安全约定：
# - hip 路径 / 节点路径 / 日志模板一律用 ``json.dumps`` 嵌入（ensure_ascii
#   默认 True，命令行保持纯 ASCII），json 字符串字面量与 Python 完全
#   同构，任何引号 / 反斜杠 / Unicode 都被转义——节点路径含引号也无法
#   逃出字符串字面量，不存在命令注入面。
# - 子进程第一步把自己的 stdout/stderr dup2 到「带自身 pid」的日志文件
#   （父进程在 Popen 之前拿不到 pid，故由子进程用 ``os.getpid()`` 落到
#   与父进程同构的 ``{PID}`` 模板路径上；父进程随后用 Popen 返回的 pid
#   计算出同一文件名写进响应）。dup2 会顺带关闭继承来的 stdout 句柄，
#   使旋转成为原子切换；旋转 prologue 失败则静默继续写继承句柄
#   （_part.log 兜底可观测）。
# - ``hou.hipFile.load(..., suppress_save_prompt=True,
#   ignore_load_warnings=True)``：不弹保存框、不写回 hip；渲染的是
#   磁盘快照，零会话突变。
_CHILD_SCRIPT_TEMPLATE = "\n".join([
    "import os",
    "try:",
    "    _p = {log_tpl}.replace('{{PID}}', str(os.getpid()))",
    "    _fd = os.open(_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)",
    "    os.dup2(_fd, 1)",
    "    os.dup2(_fd, 2)",
    "    if _fd > 2:",
    "        os.close(_fd)",
    "except Exception:",
    "    pass",
    "import hou",
    "hou.hipFile.load({hip}, suppress_save_prompt=True, "
    "ignore_load_warnings=True)",
    "_n = hou.node({node})",
    "if _n is None:",
    "    raise SystemExit('ROP node not found: ' + {node})",
    "_fr = {frame_list}",
    "if _fr:",
    "    _n.render(frame_range=tuple(_fr))",
    "else:",
    "    _n.render()",
])


def _hip_saved_path(hou):
    """返回已保存 hip 的磁盘路径；未保存 / 不可判定返回 ``None``。

    H21 实测语义（2026-09-10 编排者 hython 验收发现，§5.4）：
    - ``hou.hipFile`` **没有** ``isUntitled()`` 方法（调用即
      AttributeError，被 mock 单测掩盖——真机全炸）；
    - 未保存会话的 ``path()`` 返回 **绝对路径** ``<cwd>/untitled.hip``
      而非 ``"Untitled"`` 字符串，字面量比较拦不住。

    正确判据：``basename()`` 非忽略大小写的 ``untitled.hip``，且
    ``path()`` 为绝对路径**且磁盘上真实存在**（同时覆盖「保存后被移动」
    的陈旧路径情形）。``isUntitled()`` 仅在存在的版本上作冗余加固。
    """
    try:
        hip_file = getattr(hou, "hipFile", None)
        if hip_file is None:
            return None
        path = hip_file.path()
        if not isinstance(path, str):
            return None
        path = path.strip()
        if not path or not os.path.isabs(path):
            return None
        try:
            base = hip_file.basename()
        except Exception:
            base = os.path.basename(path)
        if isinstance(base, str) and base.strip().lower() == "untitled.hip":
            return None
        # 冗余加固：存在 isUntitled 的版本上再问一次
        is_untitled_fn = getattr(hip_file, "isUntitled", None)
        if callable(is_untitled_fn) and is_untitled_fn():
            return None
        # 磁盘真实存在（untitled.hip 场景通常不存在于磁盘）
        if not os.path.isfile(path):
            return None
        return path
    except Exception:
        return None


def _hython_executable():
    """从 ``sys.executable`` 推导 hython 可执行路径。

    - hython 内运行（headless host / MCP server 起在 hython 里）：
      ``sys.executable`` 即 hython 本体，basename 以 ``hython`` 开头，
      直接返回。
    - GUI Houdini 内运行：嵌入式 Python 的 ``sys.executable`` 指向主
      程序（houdini.exe / hindie.exe / houdini 等），hython 与其在
      **同一 bin 目录**（Windows / Linux / macOS 均 True），取 sibling
      即得。不做存在性预检（缺失时由 Popen 的 OSError 走
      ``background_spawn_failed`` 结构化 error，测试也更易 mock）。
    """
    exe = sys.executable
    if not exe:
        return None
    if os.path.basename(exe).lower().startswith("hython"):
        return exe
    hython_name = "hython.exe" if os.name == "nt" else "hython"
    return os.path.join(os.path.dirname(exe), hython_name)


def _read_output_paths(node, type_name):
    """尽力读取 ROP 输出图片 parm（eval 展开 $HIP/$F4 等变量）。

    Returns:
        ``(paths, note)``：读到至少一条时 note 为 ``None``；否则
        paths 为空列表、note 说明白名单内无可读 parm。
    """
    parm_names = _BG_OUTPUT_PARMS.get(type_name, ())
    paths = []
    for name in parm_names:
        try:
            parm = node.parm(name)
            if parm is None:
                continue
            value = parm.eval()
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
        elif isinstance(value, (list, tuple)) and value:
            first = value[0]
            if isinstance(first, str) and first.strip():
                paths.append(first.strip())
    if paths:
        return paths, None
    return [], (
        "no output parm readable via whitelist (%s); check the ROP "
        "picture parm after render" % ", ".join(parm_names))


def _render_node_background(hou, node, type_name, renderer,
                            frame_range_tuple):
    """background 执行模式：派生 detached hython 子进程渲染磁盘快照。

    调用方（``_render_node_sync``）保证四层 policy 已全部 allow。主线程
    只做 ``subprocess.Popen``（D1：不触 HOM 渲染、不把 HOM 放进自管
    thread），**不等待、不持句柄、不签发 job handle**（无 registry /
    TTL / callback）。运行中可观测性归 bridge-only ``monitor_render``（子
    进程内再派生的 mantra / husk 孙进程按 basename 匹配）。

    日志布局沿用 round2 规范：``$TEMP/houdini_mcp/<YYYY-MM-DD>/``；最终
    文件名带 pid（``bg_render_<HHMMSS>_p<PID>.log``，由子进程 rotate 落
    位）；父进程预开的 ``*_part.log`` 承接 hython 启动 banner，旋转后
    通常为空，**保留不删**（子进程 rotate 失败时它是唯一输出），随 7 天
    清理回收。
    """
    start = time.time()
    # 防御性 guard：opengl 在四层 redirect 契约下到不了这里；若 policy
    # 未来变化实测可达，显式拒绝而非静默落到 flipbook 之外的路径。
    if renderer not in ("mantra", "karma_cpu", "karma_xpu"):
        return cmn.apply_response_cap({
            "status": "error",
            "error_code": "opengl_background_unsupported",
            "message": (
                "background start_render is not supported for renderer "
                "%r; opengl is redirected by render policy before this "
                "point") % renderer,
            "renderer": renderer})

    hip_path = _hip_saved_path(hou)
    if hip_path is None:
        return cmn.apply_response_cap({
            "status": "error",
            "error_code": "background_requires_saved_hip",
            "message": (
                "background start_render requires a saved .hip file: the "
                "detached hython subprocess renders the last saved "
                "on-disk snapshot and MUST NOT auto-save the session. "
                "Call save_scene first, then retry with background=True"),
            "field": "background"})

    hython = _hython_executable()
    if not hython:
        return cmn.apply_response_cap({
            "status": "error",
            "error_code": "background_hython_unresolved",
            "message": "cannot derive hython executable from "
                       "sys.executable (%r)" % (sys.executable,)})

    base_dir = _cpaths.resolve_base_dir(hou=hou)
    log_dir = os.path.join(base_dir, time.strftime("%Y-%m-%d"))
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as error:
        return cmn.apply_response_cap({
            "status": "error",
            "error_code": "background_log_dir_failed",
            "message": "failed to create background render log dir %r: "
                       "%s" % (log_dir, error)})

    timestamp = time.strftime("%H%M%S")
    part_log = os.path.join(log_dir, "bg_render_%s_part.log" % timestamp)
    log_template = os.path.join(
        log_dir, "bg_render_%s_p{PID}.log" % timestamp)
    child_code = _CHILD_SCRIPT_TEMPLATE.format(
        log_tpl=json.dumps(log_template),
        hip=json.dumps(hip_path),
        node=json.dumps(node.path()),
        frame_list=json.dumps(list(frame_range_tuple)))

    popen_kwargs = {"close_fds": True}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP)
    else:
        popen_kwargs["start_new_session"] = True

    log_handle = None
    try:
        log_handle = open(part_log, "wb")
        popen_kwargs["stdout"] = log_handle
        popen_kwargs["stderr"] = log_handle
        process = subprocess.Popen([hython, "-c", child_code],
                                   **popen_kwargs)
    except OSError as error:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass
        return cmn.apply_response_cap({
            "status": "error",
            "error_code": "background_spawn_failed",
            "message": "failed to spawn detached hython subprocess: "
                       "%s" % error,
            "hython": hython})

    pid = process.pid
    # 不等待、不持句柄：立即关闭父进程侧日志句柄，子进程持有继承副本
    # 并在 -c 开头 rotate 到带 pid 的最终日志（见 _CHILD_SCRIPT_TEMPLATE
    # 注释）。process 对象就此丢弃——无 wait / poll / registry。
    try:
        log_handle.close()
    except Exception:
        pass

    final_log = log_template.replace("{PID}", str(pid))
    output_paths, output_note = _read_output_paths(node, type_name)
    elapsed = round(time.time() - start, 3)

    result = {
        "status": "success",
        "state": "launched_background",
        "pid": pid,
        "log_path": final_log,
        "hip_snapshot_path": hip_path,
        "node_path": node.path(),
        "node_type": type_name,
        "renderer": renderer,
        "frame_range": list(frame_range_tuple),
        "output_paths": output_paths,
        "monitor_hint": (
            "detached hython subprocess renders the saved hip snapshot; "
            "use monitor_render (bridge) to observe husk/mantra child "
            "processes, verify completion via output_paths file "
            "existence and log_path content"),
        "elapsed": elapsed,
    }
    if output_note:
        result["output_paths_note"] = output_note
    return cmn.apply_response_cap(result)