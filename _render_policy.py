"""_render_policy.py — opera-houdini-mcp fork render policy 拦截模块。

模块职责（fork-render-policy-redirect-and-consent）：
- 在所有 render 入口（MCP tool / server.py handler / HoudiniMCPRender 函数 /
  _render_b64.py）做防御性校验，阻止高危 renderer 在用户机 H21 缺 OGL 3.3
  环境下触发 Houdini 主线程死锁。
- ``opengl`` 路径：返回结构化 ``_redirect`` dict，强制 agent 改调
  ``capture_pane_screenshot(SceneViewer)``（fork 已实测稳定的 flipbook 路径）。
- ``karma_cpu`` / ``karma_xpu`` 路径：返回结构化 ``_interrupt`` dict
  （带 uuid4 consent token），要求 agent 中断工具调用循环、把 prompt
  文本原样发给用户、收到用户 ``yes`` / ``no`` / ``flipbook`` 后再决定
  带 ``consent_token`` 重调 / 中止 / 改 flipbook。

设计原则：
- hou 通过参数注入 / 不顶层 import hou，便于纯 Python 单测。
- 复用 fork 已有 ``external/houdinimcp-env/`` 目录作为 sentinel 文件
  落盘位置（与 ``houdini-mcp-env`` 嵌入式 env 同根），不引入新路径。
- 通用机制 = MCP 协议通用的结构化 dict 字段（``_redirect`` / ``_interrupt``），
  由 bridge 层透传到任何 AI 客户端（Kilo / Cursor / Claude Desktop / Cline
  / ZCode / OpenCode）。**不**依赖 ``hou.ui.displayMessage`` 弹窗（H21 主
  线程死锁场景下不可靠）。
- 4 空格缩进 / snake_case / 中文 docstring / 无 f-string / 无类型注解。

API：
    enforce_render_policy(renderer) -> (action, dict_or_None):
        入口校验。opengl -> ("redirect", dict)；karma_cpu/xpu ->
        ("interrupt", dict)；其他 -> ("allow", None)。
    create_consent_token(expires_in_seconds=300) -> str:
        生成 uuid4 + 写 sentinel 文件 + 返回 token 字符串。
    consume_consent_token(token, expires_in_seconds=300) -> bool:
        校验 sentinel 文件存在 + 未过期；通过则删除并返 True；其余返 False。
    _redirect_dict(renderer, fallback_tool, fallback_args, reason) -> dict:
        构造标准 redirect dict。
    _interrupt_dict(renderer, token, prompt, expires_in_seconds) -> dict:
        构造标准 interrupt dict。
    _consent_dir() -> str:
        返回 ``<fork_root>/../houdinimcp-env/.karma_consent/`` 绝对路径，
        ``os.makedirs(exist_ok=True)`` 确保存在。
"""
import json
import os
import time
import uuid


# fork 模块所在目录的父目录即为 ``external/houdinimcp-env/`` 的同级兄弟
# （与 ``houdinimcp-env`` 嵌入式 env 共享根）。绝对化以便 sentinel 文件
# 落盘路径在 fork 进程内一致；测试可通过 monkeypatch ``_env_dir`` 切到
# tmp_path 隔离。
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_FORK_PARENT = os.path.dirname(_MODULE_DIR)
_DEFAULT_ENV_DIR = os.path.join(_FORK_PARENT, "houdinimcp-env")
_DEFAULT_CONSENT_SUBDIR = ".karma_consent"


def _env_dir():
    """返回 ``external/houdinimcp-env/`` 绝对路径（fork 嵌入式 env 根）。

    设计上为函数而非常量，便于测试 monkeypatch 切换到 tmp_path。
    生产路径下 ``houdinimcp-env/`` 由 MCP Install 按钮下载（见
    ``AGENTS.md``），存在性由 ``_consent_dir`` 的 ``os.makedirs`` 兜底。
    """
    return _DEFAULT_ENV_DIR


def _consent_dir():
    """返回 consent sentinel 文件目录，绝对路径，自动 ``os.makedirs``。

    路径：``<fork 模块父目录>/houdinimcp-env/.karma_consent/``。
    父目录不存在时 ``os.makedirs(exist_ok=True)`` 兜底创建，避免上层
    入口在 MCP Install 尚未跑过的环境里崩。
    """
    d = os.path.join(_env_dir(), _DEFAULT_CONSENT_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _redirect_dict(renderer, fallback_tool, fallback_args, reason):
    """构造标准 ``_redirect`` dict（4 个必填键 + _meta）。

    Args:
        renderer: 触发 redirect 的 renderer 字符串（``opengl``）。
        fallback_tool: 推荐 agent 改调的工具名（``capture_pane_screenshot``）。
        fallback_args: 透传给 fallback 工具的参数 dict（含
            ``pane_type_name="SceneViewer"`` 等）。
        reason: 触发原因的中文文案（硬件 + 路径说明）。

    Returns:
        dict: 包含 ``_redirect`` / ``fallback_tool`` / ``fallback_args`` /
        ``reason`` / ``renderer`` / ``_meta`` 键；不含 ``image_base64``
        / ``filepath`` / ``size_bytes`` 等成功渲染字段。
    """
    return {
        "_redirect": "flipbook",
        "fallback_tool": fallback_tool,
        "fallback_args": dict(fallback_args) if fallback_args else {},
        "reason": reason,
        "renderer": renderer,
        "_meta": {"policy": "redirect",
                  "renderer": renderer,
                  "fallback_tool": fallback_tool},
    }


def _interrupt_dict(renderer, token, prompt, expires_in_seconds):
    """构造标准 ``_interrupt`` dict（4 个必填键 + _meta）。

    Args:
        renderer: 触发 interrupt 的 renderer（``karma_cpu`` / ``karma_xpu``）。
        token: ``create_consent_token`` 返回的 uuid4 字符串。
        prompt: 给用户的交互文案（中文）。
        expires_in_seconds: 过期秒数（默认 300）。

    Returns:
        dict: 包含 ``_interrupt`` / ``consent_token`` / ``prompt`` /
        ``expires_in_seconds`` / ``renderer`` / ``_meta`` 键；不含
        ``image_base64`` / ``filepath`` 等成功渲染字段。
    """
    return {
        "_interrupt": "user_consent_required",
        "consent_token": token,
        "prompt": prompt,
        "expires_in_seconds": int(expires_in_seconds),
        "renderer": renderer,
        "_meta": {"policy": "interrupt",
                  "renderer": renderer,
                  "expires_in_seconds": int(expires_in_seconds)},
    }


def _default_redirect(renderer):
    """opengl 的标准 redirect 构造（fallback = capture_pane_screenshot）。

    ``fallback_args`` 含 ``pane_type_name="SceneViewer"`` + ``fit_contents=True``
    与既有 ``capture_pane_screenshot`` 调用签名对齐（参见 spec scenario
    "opengl renderer triggers redirect at MCP tool entry"）。
    """
    return _redirect_dict(
        renderer=renderer,
        fallback_tool="capture_pane_screenshot",
        fallback_args={"pane_type_name": "SceneViewer",
                       "fit_contents": True,
                       "save_path": None},
        reason=("H21 缺 OGL 3.3 驱动，opengl output node 在本机不可用；"
                "请改用 capture_pane_screenshot(SceneViewer) 走 flipbook 路径"
                "（fork 已实测稳定）"),
    )


def _default_interrupt(renderer, token, expires_in_seconds):
    """karma 的标准 interrupt 构造（中文 prompt + 5 分钟过期）。

    Prompt 文本按 design.md "karma interrupt + consent flow" 给出三选一：
    ``yes`` 继续 / ``no`` 中止 / ``flipbook`` 改用 SceneViewer flipbook。
    """
    return _interrupt_dict(
        renderer=renderer,
        token=token,
        prompt=("检测到 karma 渲染调用（renderer={0}），是否执行？"
                "回复 yes 继续 / no 中止 / flipbook 改用 SceneViewer"
                " flipbook 截图路径").format(renderer),
        expires_in_seconds=expires_in_seconds,
    )


def create_consent_token(expires_in_seconds=300):
    """生成 uuid4 token + 写 sentinel 文件，返回 token 字符串。

    Args:
        expires_in_seconds: 过期窗口秒数（默认 300）。仅作为元数据写入
            sentinel，不影响 consume 时校验（consume 仍以自身参数为准）。

    Returns:
        str: 32 字符 uuid4 hex（不可预测，agent 端无法伪造）。

    Sentinel 文件格式：``{"created_at": <unix_ts>, "expires_in_seconds": int}``。
    父目录 ``_consent_dir()`` 由 ``_consent_dir`` 自身保证存在。
    """
    token = uuid.uuid4().hex
    sentinel_path = os.path.join(_consent_dir(), token)
    payload = {"created_at": time.time(),
               "expires_in_seconds": int(expires_in_seconds)}
    with open(sentinel_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return token


def consume_consent_token(token, expires_in_seconds=300):
    """校验 sentinel 文件存在 + 未过期；通过则删除并返 True。

    Args:
        token: ``create_consent_token`` 返回的 uuid4 hex。
        expires_in_seconds: 过期窗口秒数（默认 300）。

    Returns:
        bool: True = 校验通过 + 已删除 sentinel；False = 文件不存在 /
        已过期（过期情况下会顺手删除过期 sentinel，保持 ``_consent_dir``
        不堆积脏文件）。

    防御性细节：
    - 文件不存在直接返 False（无副作用）。
    - 文件存在但 ``created_at`` 缺失 / 非数字 → 视为过期，删除 + 返 False。
    - 文件存在但读取异常（损坏 / 权限） → 静默返 False，不抛异常
      （调用方是 MCP tool 入口，抛异常会让 bridge 层返 error envelope
      而不是 interrupt dict，破坏设计契约）。
    """
    if not token:
        return False
    sentinel_path = os.path.join(_consent_dir(), token)
    if not os.path.exists(sentinel_path):
        return False
    try:
        with open(sentinel_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        created_at = float(payload.get("created_at", 0))
    except (OSError, ValueError, TypeError):
        # 文件损坏 / 读失败 → 视为过期清理掉
        try:
            os.remove(sentinel_path)
        except OSError:
            pass
        return False
    now = time.time()
    if now - created_at <= expires_in_seconds:
        try:
            os.remove(sentinel_path)
        except OSError:
            # 已校验通过；删除失败不阻塞（agent 5 分钟内无法重放，但
            # 主流程不应崩）。返 True。
            pass
        return True
    # 过期 → 删除 + 返 False
    try:
        os.remove(sentinel_path)
    except OSError:
        pass
    return False


def enforce_render_policy(renderer):
    """入口校验：返回 ``(action, payload)`` 三元组。

    三种结果：
    - ``("allow", None)``：renderer 既不是 opengl 也不是 karma_*
      （含 ``mantra`` / 未知值），原逻辑继续。
    - ``("redirect", dict)``：renderer == ``opengl``；调用方应立即
      return redirect dict（不进入实际 render 引擎调用链路）。
    - ``("interrupt", dict)``：renderer in ``("karma_cpu", "karma_xpu")``；
      调用方应检查 kwargs 里的 ``consent_token``，调
      ``consume_consent_token`` 通过才放行原逻辑，否则立即 return
      interrupt dict。

    Args:
        renderer: renderer 字符串（``opengl`` / ``karma_cpu`` /
            ``karma_xpu`` / 其他）。

    Returns:
        (str, dict_or_None): ``(action, payload)`` 二元组。
    """
    if renderer == "opengl":
        return ("redirect", _default_redirect(renderer))
    if renderer in ("karma_cpu", "karma_xpu"):
        token = create_consent_token()
        return ("interrupt", _default_interrupt(renderer, token, 300))
    return ("allow", None)


# karma render_engine 字符串（来自 HoudiniMCPRender 的 render_engine 维度）
# 映射到 fork 的 renderer 维度；HoudiniMCPRender 接受 ``karma`` +
# ``karma_engine``（``cpu`` / ``gpu``）两个参数，对应 ``karma_cpu`` /
# ``karma_xpu`` 两个 renderer。
_KARMA_RENDER_ENGINE = "karma"


def render_engine_to_renderer(render_engine, karma_engine=None):
    """把 ``render_engine`` + ``karma_engine`` 归一为 renderer 字符串。

    - ``opengl`` -> ``opengl``
    - ``karma`` + ``cpu`` -> ``karma_cpu``
    - ``karma`` + ``gpu`` -> ``karma_xpu``
    - 其他 -> 原样返回
    """
    if render_engine == _KARMA_RENDER_ENGINE:
        if karma_engine == "gpu":
            return "karma_xpu"
        return "karma_cpu"
    return render_engine


def enforce_render_engine_policy(render_engine, karma_engine=None):
    """``HoudiniMCPRender`` 风格的入口校验。

    Args:
        render_engine: ``render_engine`` 参数（``opengl`` / ``karma`` /
            ``mantra``）。
        karma_engine: ``karma_engine`` 参数（``cpu`` / ``gpu``），仅
            ``render_engine == "karma"`` 时有意义。

    Returns:
        (str, dict_or_None): 同 ``enforce_render_policy``。
    """
    return enforce_render_policy(
        render_engine_to_renderer(render_engine, karma_engine))