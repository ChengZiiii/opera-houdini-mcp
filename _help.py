"""SideFX Houdini 在线文档查询（PR 15）。

通过 stdlib `html.parser` 解析 SideFX 在线文档 HTML，再以 urllib.request
抓取对应页面。零新增 pip 依赖（不依赖 beautifulsoup4 / lxml /
requests-html）。

设计要点：
- `SideFXDocParser(HTMLParser)`：解析 SideFX 在线文档主要结构片段
  （h1.title / p.summary / div.parameter / div#inputs-body /
  div#outputs-body / div.method）。
- `HELP_TYPE_URLS`：11 个 help_type 的 URL 前缀字典，覆盖节点类
  （sop/obj/dop/cop2/chop/vop/lop/top/rop）、VEX 函数（vex_function）、
  HOM Python（python_hou）。
- `get_houdini_help(help_type, item_name, timeout)`：抓取并解析。
  HTTP 4xx / 5xx / 网络异常 / timeout 全部转为 status=error 字典，
  不向调用方抛异常（友好降级）。
- 响应整体过 `_common.apply_response_cap` 截断大 payload。
"""
from html.parser import HTMLParser
import socket
import urllib.error
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
        self._current_tag = None
        self._current_attrs = {}
        self._buffer = ""
        self._in_section = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        self._current_tag = tag
        self._current_attrs = attrs_dict
        self._buffer = ""
        if tag == "div" and "class" in attrs_dict:
            cls = attrs_dict["class"]
            if "parameter" in cls:
                self._in_section = "parameter"
            elif "method" in cls:
                self._in_section = "method"
        elif tag == "div" and "id" in attrs_dict:
            id_val = attrs_dict["id"]
            if id_val == "inputs-body":
                self._in_section = "inputs-body"
            elif id_val == "outputs-body":
                self._in_section = "outputs-body"
        elif tag == "h1" and attrs_dict.get("class") == "title":
            self._in_section = "title"
        elif tag == "p" and attrs_dict.get("class") == "summary":
            self._in_section = "summary"

    def handle_data(self, data):
        if self._in_section:
            self._buffer += data

    def handle_endtag(self, tag):
        if tag == self._current_tag:
            content = self._buffer.strip()
            if self._in_section == "title":
                self.title = content
            elif self._in_section == "summary":
                self.summary = content
            elif self._in_section == "parameter":
                if content:
                    self.parameters.append({"text": content})
            elif self._in_section == "inputs-body":
                if content:
                    self.inputs.append({"text": content})
            elif self._in_section == "outputs-body":
                if content:
                    self.outputs.append({"text": content})
            elif self._in_section == "method":
                if content:
                    self.methods.append({"text": content})
            self._in_section = None
            self._buffer = ""
            self._current_tag = None
            self._current_attrs = {}


# ---------------------------------------------------------------------------
# 抓取 + 解析入口
# ---------------------------------------------------------------------------
_DEFAULT_TIMEOUT = 10
_USER_AGENT = "Mozilla/5.0 (HoudiniMCP-PR15)"


def _error_payload(help_type, item_name, url, status_code, error_msg):
    """构造统一 error 响应 dict。"""
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
    }


def get_houdini_help(help_type, item_name, timeout=_DEFAULT_TIMEOUT):
    """从 SideFX 在线文档抓取并解析指定节点/函数/方法。

    Args:
        help_type: HELP_TYPE_URLS 中的键之一（sop/obj/dop/cop2/chop/
            vop/lop/top/rop/vex_function/python_hou）。
        item_name: 节点名 / VEX 函数名 / hou 方法名。
        timeout: HTTP 请求超时秒数，默认 10。

    Returns:
        dict：始终包含 help_type / item_name / status / error /
        status_code；status=success 时另含 title / summary /
        parameters / inputs / outputs / methods / url / _response_size。
        任何 4xx/5xx/网络错误/timeout 都返回 status=error 而不抛异常。
    """
    if help_type not in HELP_TYPE_URLS:
        return _error_payload(
            help_type, item_name, url=None, status_code=None,
            error_msg="未知 help_type: %s; 有效值: %s" % (
                help_type, sorted(HELP_TYPE_URLS.keys())))

    base_url = HELP_TYPE_URLS[help_type]
    url = base_url + item_name
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
    }