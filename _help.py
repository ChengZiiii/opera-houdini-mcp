"""SideFX Houdini 文档查询（PR 15 + local-help-first-fallback）。

通过 stdlib `html.parser` 解析 SideFX 文档 HTML，再以 urllib.request
抓取对应页面。零新增 pip 依赖（不依赖 beautifulsoup4 / lxml /
requests-html）。

**local-help-first-fallback**：帮助查询**优先**打本地 help server
（Houdini GUI 启动时自带，默认 `http://127.0.0.1:48626/`），本地不可用
或白屏时**回退**到 SideFX 在线文档站。两条路径共用同一套
`SideFXDocParser`（本地与在线文档同源 Sphinx build，HTML 结构一致）。

设计要点：
- `SideFXDocParser(HTMLParser)`：解析 SideFX 文档主要结构片段
  （h1.title / p.summary / div.parameter / div#inputs-body /
  div#outputs-body / div.method）。
- `HELP_TYPE_URLS`：11 个 help_type 的 URL 前缀字典，**仍是在线权威
  base**；本地 URL 由在线 base 派生（剥 `https://www.sidefx.com/docs/houdini`
  前缀，拼 `LOCAL_HELP_BASE`）。覆盖节点类
  （sop/obj/dop/cop2/chop/vop/lop/top/rop）、VEX 函数（vex_function）、
  HOM Python（python_hou）。
- `get_houdini_help(help_type, item_name, timeout)`：local-first +
  fallback + 健康缓存 + 白屏识别。HTTP 4xx / 5xx / 网络异常 / timeout /
  白屏 全部降级为 status=error 字典或回退在线，不向调用方抛异常
  （友好降级）。
- 响应整体过 `_common.apply_response_cap` 截断大 payload。
- 返回 dict 新增两个 advisory 字段：
  - `_source`：`"local"` / `"online"` / `""`（禁用 local-first 时）
  - `_fallback_reason`：回退原因短串（local 成功或仅在线时为 `""`）
"""
from html.parser import HTMLParser
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# URL 字典：11 个 help_type
# ---------------------------------------------------------------------------
HELP_TYPE_URLS = {
    "sop": "https://www.sidefx.com/docs/houdini/nodes/sop/",
    "obj": "https://www.sidefx.com/docs/houdini/nodes/obj/",
    "dop": "https://www.sidefx.com/docs/houdini/nodes/dop/",
    "cop2": "https://www.sidefx.com/docs/houdini/nodes/cop2/",
    "chop": "https://www.sidefx.com/docs/houdini/nodes/chop/",
    "vop": "https://www.sidefx.com/docs/houdini/nodes/vop/",
    "lop": "https://www.sidefx.com/docs/houdini/nodes/lop/",
    "top": "https://www.sidefx.com/docs/houdini/nodes/top/",
    "rop": "https://www.sidefx.com/docs/houdini/nodes/rop/",
    "vex_function": "https://www.sidefx.com/docs/houdini/vex/functions/",
    "python_hou": "https://www.sidefx.com/docs/houdini/hom/hou/",
}


# ---------------------------------------------------------------------------
# 环境变量（import 时读一次 = 进程级常量）
# ---------------------------------------------------------------------------
def _env_float(name, default, lo, hi):
    """读取 env var 并 clamp 到 [lo, hi]；非法值回退默认。"""
    try:
        v = float(os.environ.get(name, ""))
        return max(lo, min(hi, v))
    except (ValueError, TypeError):
        return default


def _env_bool(name):
    """读取 env var 为 bool（1/true/yes/on → True，其余 False）。"""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# 本地 help server base URL（必须含 scheme，如 http://127.0.0.1:48626/）
LOCAL_HELP_BASE = os.environ.get("HOUDINI_MCP_LOCAL_HELP_URL") or "http://127.0.0.1:48626/"
# 本地请求短超时（短于在线 timeout，默认 2.5s，clamp [0.5, 30.0]）
LOCAL_HELP_TIMEOUT = _env_float("HOUDINI_MCP_LOCAL_HELP_TIMEOUT", 2.5, 0.5, 30.0)
# 本地不健康 cooldown 窗口（默认 60s，clamp [0.0, 600.0]）
LOCAL_HELP_COOLDOWN = _env_float("HOUDINI_MCP_LOCAL_HELP_COOLDOWN", 60.0, 0.0, 600.0)
# 完全禁用 local-first（行为退化到 change 前的"仅在线"，_source=""）
LOCAL_HELP_DISABLED = _env_bool("HOUDINI_MCP_LOCAL_HELP_DISABLE")


# ---------------------------------------------------------------------------
# 本地 URL 派生（task 1.2）
# ---------------------------------------------------------------------------
_ONLINE_PREFIX = "https://www.sidefx.com/docs/houdini"


def _local_url_for(online_url):
    """把在线 URL 转成本地 help server URL。

    在线 `https://www.sidefx.com/docs/houdini/nodes/sop/box.html`
    → 本地 `http://127.0.0.1:48626/nodes/sop/box.html`
    （剥 `_ONLINE_PREFIX`，拼 `LOCAL_HELP_BASE`）。
    非标准 base（非 sidefx 前缀）直接原样返回（兜底，不期望命中）。
    """
    if online_url.startswith(_ONLINE_PREFIX):
        return LOCAL_HELP_BASE.rstrip("/") + "/" + \
               online_url[len(_ONLINE_PREFIX):].lstrip("/")
    return online_url


# ---------------------------------------------------------------------------
# 进程内健康缓存（task 1.3，用 time.monotonic 不受系统时钟跳变影响）
# ---------------------------------------------------------------------------
_local_unhealthy_until = 0.0  # monotonic 时间戳；cooldown 过期前都视为不健康


def _local_healthy():
    """本地 help server 当前是否健康（cooldown 未过期）。"""
    return time.monotonic() >= _local_unhealthy_until


def _mark_local_unhealthy():
    """标记本地不健康，进入 cooldown 窗口。"""
    global _local_unhealthy_until
    _local_unhealthy_until = time.monotonic() + LOCAL_HELP_COOLDOWN


def _reset_local_health():
    """清除 cooldown（恢复 healthy）；测试 / 本地恢复探测成功时调用。"""
    global _local_unhealthy_until
    _local_unhealthy_until = 0.0


# ---------------------------------------------------------------------------
# 白屏校验（task 1.4）
# ---------------------------------------------------------------------------
# 节点类 help_type（要求 title 非空 且 summary/parameters/inputs/outputs 任一非空）
_NODE_HELP_TYPES = frozenset(
    ("sop", "obj", "dop", "cop2", "chop", "vop", "lop", "top", "rop"))


def _validate_local_content(result, help_type):
    """白屏校验：本地 200 响应解析后内容是否实质有效。

    - 节点类：`title` 非空 **且**（`summary` 非空 **或**
      `parameters` / `inputs` / `outputs` 任一非空）
    - `python_hou`：`title` 非空 **或** `methods` 非空
    - `vex_function`：`title` 非空
    """
    title = (result.get("title") or "").strip()
    summary = (result.get("summary") or "").strip()
    parameters = result.get("parameters") or []
    inputs = result.get("inputs") or []
    outputs = result.get("outputs") or []
    methods = result.get("methods") or []

    if help_type in _NODE_HELP_TYPES:
        has_body = bool(summary or parameters or inputs or outputs)
        return bool(title) and has_body
    if help_type == "python_hou":
        return bool(title) or bool(methods)
    if help_type == "vex_function":
        return bool(title)
    # 未知 help_type 兜底：要求 title 非空
    return bool(title)


# ---------------------------------------------------------------------------
# HTML 解析器
# ---------------------------------------------------------------------------
class SideFXDocParser(HTMLParser):
    """解析 SideFX Houdini 在线文档 HTML。

    提取：
    - <h1 class="title"> ... </h1>      → title
    - <p class="summary"> ... </p>      → summary
    - <div class="parameter"> ... </div>→ parameters (list of {text})
    - <div id="inputs-body"> ... </div> → inputs (list of {text})
    - <div id="outputs-body"> ... </div>→ outputs (list of {text})
    - <div class="method"> ... </div>   → methods (list of {text})
    """
    def __init__(self):
        super(SideFXDocParser, self).__init__()
        self.title = ""
        self.summary = ""
        self.parameters = []
        self.inputs = []
        self.outputs = []
        self.methods = []
        self._element_stack = []
        self._section_stack = []
        self._title_set = False
        self._summary_set = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = set((attrs_dict.get("class") or "").split())
        element = object()
        self._element_stack.append((tag, element))

        section_name = None
        if tag == "h1" and "title" in classes and not self._title_set:
            section_name = "title"
        elif tag == "p" and "summary" in classes and not self._summary_set:
            section_name = "summary"
        elif tag == "div":
            if "parameter" in classes:
                section_name = "parameter"
            elif "method" in classes:
                section_name = "method"
            elif attrs_dict.get("id") == "inputs-body":
                section_name = "inputs-body"
            elif attrs_dict.get("id") == "outputs-body":
                section_name = "outputs-body"

        if section_name:
            self._section_stack.append({
                "name": section_name,
                "tag": tag,
                "root": element,
                "buffer": [],
            })

    def handle_data(self, data):
        for section in self._section_stack:
            section["buffer"].append(data)

    def _commit_section(self, section):
        content = "".join(section["buffer"]).strip()
        section_name = section["name"]
        if section_name == "title" and content:
            self.title = content
            self._title_set = True
        elif section_name == "summary" and content:
            self.summary = content
            self._summary_set = True
        elif section_name == "parameter" and content:
            self.parameters.append({"text": content})
        elif section_name == "inputs-body" and content:
            self.inputs.append({"text": content})
        elif section_name == "outputs-body" and content:
            self.outputs.append({"text": content})
        elif section_name == "method" and content:
            self.methods.append({"text": content})

    def handle_endtag(self, tag):
        element_index = None
        for index in range(len(self._element_stack) - 1, -1, -1):
            if self._element_stack[index][0] == tag:
                element_index = index
                break
        if element_index is None:
            return

        closed_elements = {
            element for _tag, element
            in self._element_stack[element_index:]
        }
        del self._element_stack[element_index:]

        while (self._section_stack
               and self._section_stack[-1]["root"] in closed_elements):
            self._commit_section(self._section_stack.pop())


# ---------------------------------------------------------------------------
# 抓取 + 解析入口
# ---------------------------------------------------------------------------
_DEFAULT_TIMEOUT = 10
_USER_AGENT = "Mozilla/5.0 (HoudiniMCP-PR15)"


def _error_payload(help_type, item_name, url, status_code, error_msg):
    """构造统一 error 响应 dict（含 advisory 字段 `_source` / `_fallback_reason`）。"""
    return {
        "help_type": help_type,
        "item_name": item_name,
        "url": url,
        "title": "",
        "summary": "",
        "parameters": [],
        "inputs": [],
        "outputs": [],
        "methods": [],
        "status": "error",
        "error": error_msg,
        "status_code": status_code,
        "_response_size": 0,
        "_source": "",
        "_fallback_reason": "",
    }


# ---------------------------------------------------------------------------
# fetch + parse 主体（task 1.5，可复用 helper）
# ---------------------------------------------------------------------------
def _fetch_and_parse(url, timeout, help_type, item_name):
    """对单个 URL 走 urlopen + SideFXDocParser，返回既有形状 dict。

    返回 dict 含 `_source=""` / `_fallback_reason=""` 占位（统一形状，
    便于调用方覆盖）。HTTP 错 / 网络错 / timeout / HTML 解析失败 → 返回
    `status="error"` dict（含 `status_code` / `error` / `_response_size`
    等既有字段）。
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = getattr(resp, "status", 200)
            html_bytes = resp.read()
            html = html_bytes.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return _error_payload(
            help_type, item_name, url=url, status_code=e.code,
            error_msg="HTTP %s: %s" % (e.code, e.reason))
    except urllib.error.URLError as e:
        return _error_payload(
            help_type, item_name, url=url, status_code=None,
            error_msg="网络错误: %s: %s" % (type(e).__name__, e))
    except (socket.timeout, TimeoutError) as e:
        return _error_payload(
            help_type, item_name, url=url, status_code=None,
            error_msg="网络错误: %s: %s" % (type(e).__name__, e))

    parser = SideFXDocParser()
    try:
        parser.feed(html)
    except Exception as e:
        # HTML 解析失败：仍返 200 但带 error 提示，便于 bridge 识别
        return {
            "help_type": help_type,
            "item_name": item_name,
            "url": url,
            "title": "",
            "summary": "",
            "parameters": [],
            "inputs": [],
            "outputs": [],
            "methods": [],
            "status": "error",
            "error": "HTML 解析失败: %s: %s" % (type(e).__name__, e),
            "status_code": status_code,
            "_response_size": len(html_bytes),
            "_source": "",
            "_fallback_reason": "",
        }

    return {
        "help_type": help_type,
        "item_name": item_name,
        "url": url,
        "status": "success",
        "status_code": status_code,
        "title": parser.title,
        "summary": parser.summary,
        "parameters": parser.parameters,
        "inputs": parser.inputs,
        "outputs": parser.outputs,
        "methods": parser.methods,
        "_response_size": len(html_bytes),
        "_source": "",
        "_fallback_reason": "",
    }


# ---------------------------------------------------------------------------
# 本地探测包装（task 1.6）
# ---------------------------------------------------------------------------
def _try_local(url, timeout, help_type, item_name):
    """对本地 URL 走 `_fetch_and_parse` + 白屏校验。

    返回 `(result, ok, reason)`：
    - `ok=True`：本地成功且内容有效，`result` 是 success dict
    - `ok=False`：本地失败（超时 / HTTP 错 / 网络错 / 白屏），
      `result` 是 error dict，`reason` 是分类短串
    """
    result = _fetch_and_parse(url, timeout, help_type, item_name)
    status = result.get("status")
    error = result.get("error") or ""
    status_code = result.get("status_code")

    if status == "success":
        # HTTP 200 + 解析成功：再做白屏校验
        if _validate_local_content(result, help_type):
            return result, True, ""
        return result, False, "local_empty_content"

    # status == "error"：按异常类型分类 reason
    if "timeout" in error.lower():
        return result, False, "local_timeout"
    if status_code is not None and isinstance(status_code, int):
        return result, False, "local_http_%d" % status_code
    # URLError / 其它网络错误
    if "URLError" in error or "网络错误" in error:
        return result, False, "local_network_error"
    return result, False, "local_network_error"


# ---------------------------------------------------------------------------
# 主入口：local-first + fallback（task 1.7）
# ---------------------------------------------------------------------------
def get_houdini_help(help_type, item_name, timeout=_DEFAULT_TIMEOUT):
    """查询 Houdini 帮助文档：**本地优先** + **在线回退**。

    Args:
        help_type: HELP_TYPE_URLS 中的键之一（sop/obj/dop/cop2/chop/
            vop/lop/top/rop/vex_function/python_hou）。
        item_name: 节点名 / VEX 函数名 / hou 方法名。
        timeout: 在线 HTTP 请求超时秒数，默认 10。本地用 `LOCAL_HELP_TIMEOUT`
            （默认 2.5s）。

    Returns:
        dict：始终包含 help_type / item_name / status / error /
        status_code / `_source` / `_fallback_reason`；status=success 时
        另含 title / summary / parameters / inputs / outputs / methods /
        url / _response_size。任何 4xx/5xx/网络错误/timeout/白屏 都降级为
        status=error 或回退在线，不抛异常。

    `_source`：`"local"`（本地命中）/ `"online"`（在线命中，可能经 fallback）
    / `""`（local-first 被禁用，等同 change 前"仅在线"行为）。
    `_fallback_reason`：回退原因短串（local 成功 / 仅在线时为 `""`）。
    """
    if help_type not in HELP_TYPE_URLS:
        return _error_payload(
            help_type, item_name, url=None, status_code=None,
            error_msg="未知 help_type: %s; 有效值: %s" % (
                help_type, sorted(HELP_TYPE_URLS.keys())))

    online_url = HELP_TYPE_URLS[help_type] + urllib.parse.quote(item_name, safe="")

    # ── 分支 1：local-first 被禁用 → 仅在线（_source=""，等同既有行为）
    if LOCAL_HELP_DISABLED:
        result = _fetch_and_parse(
            online_url, timeout, help_type, item_name)
        # 既有 error_payload 已含 _source="" / _fallback_reason=""，
        # success 路径补上 advisory 字段
        if result.get("status") == "success":
            result["_source"] = ""
            result["_fallback_reason"] = ""
        return result

    # ── 分支 2：健康缓存有效 → 先试本地
    if _local_healthy():
        local_url = _local_url_for(online_url)
        result, ok, reason = _try_local(
            local_url, LOCAL_HELP_TIMEOUT, help_type, item_name)
        if ok:
            # 本地命中：补 advisory 字段并清除可能残留的 cooldown
            _reset_local_health()
            result["_source"] = "local"
            result["_fallback_reason"] = ""
            return result
        # 本地失败：标记不健康 + 记 reason，继续回退在线
        _mark_local_unhealthy()
    else:
        # cooldown 内：跳过本地，直接查在线
        reason = "local_unhealthy_skipped"

    # ── 分支 3：回退在线（用原 timeout）
    online_result = _fetch_and_parse(
        online_url, timeout, help_type, item_name)
    online_result["_source"] = "online"
    online_result["_fallback_reason"] = reason
    # spec Scenario 1：两边都失败时 `error` 字段需含本地与在线两次失败原因。
    # 仅当在线也失败（status=="error"）且本地是真实探测失败（非 cooldown
    # 跳过）时合并进 error；cooldown 跳过（reason=="local_unhealthy_skipped"）
    # 不是"本地失败"而是"跳过原因"，保持 error 只是在线原因，
    # `_fallback_reason` 仍记录它。
    if (online_result.get("status") == "error"
            and reason
            and reason != "local_unhealthy_skipped"):
        online_result["error"] = "[local: %s] %s" % (
            reason, online_result.get("error", ""))
    return online_result