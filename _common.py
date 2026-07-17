"""_common.py — opera-houdini-mcp 基础设施模块（PR 3）。

集中存放 PR 4–16 都会复用的工具函数：
- 连接错误装饰器 + 错误结构化（handle_connection_errors / CONNECTION_ERRORS）
- 分辨率校验（validate_resolution）
- 危险代码 / 重几何 / 变更操作 检测正则与 AST 别名检测
- hou 模块导入尝试检测
- 输出截断与响应体大小二分封顶
- 列表分页 / 响应元数据补全
- hou 对象的 JSON 安全递归序列化（含 hou.Ramp/Vector/Color/EnumValue 分支）
- 参数模板扁平化辅助
- ExecutionTimeoutError 异常

约束：
- 仅依赖 Python 3.12 标准库
- hou 模块不在顶层 import；任何 hou 引用都通过参数 hou 传入（测试用 mock）
- 不引入 f-string 与类型注解，匹配 server.py 风格
- 函数签名稳定（PR 4–16 直接 import）
"""

import ast
import copy
import functools
import json
import re

__all__ = [
    "handle_connection_errors",
    "CONNECTION_ERRORS",
    "_handle_connection_error",
    "validate_resolution",
    "DANGEROUS_PATTERNS",
    "HEAVY_GEOMETRY_PATTERNS",
    "MUTATION_PATTERNS",
    "_detect_mutation_code",
    "_detect_dangerous_code",
    "_detect_heavy_geometry_code",
    "_detect_import_hou",
    "_truncate_output",
    "_estimate_response_size",
    "_serialized_size",
    "apply_response_cap",
    "paginate_list",
    "_add_response_metadata",
    "_json_safe_hou_value",
    "_flatten_parm_templates",
    "ExecutionTimeoutError",
]


# ---------------------------------------------------------------------------
# Section 1: connection-error handling
# ---------------------------------------------------------------------------
CONNECTION_ERRORS = (ConnectionError, TimeoutError, OSError)


def _handle_connection_error(e, operation):
    """把异常打包成结构化错误 dict（与 server.py 中 handlers 的风格一致）。"""
    return {
        "error": "[{0}] {1}: {2}".format(operation, type(e).__name__, str(e)),
        "operation": operation,
        "exception_type": type(e).__name__,
    }


def handle_connection_errors(operation_name):
    """装饰器：捕获连接类异常并返回错误 dict；保留原函数签名（functools.wraps）。"""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except CONNECTION_ERRORS as e:
                return _handle_connection_error(e, operation_name)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Section 2: resolution validation
# ---------------------------------------------------------------------------
def validate_resolution(resolution, min_size=64, max_size=4096):
    """校验分辨率在 [min_size, max_size] 范围内，必须为整数。"""
    if not isinstance(resolution, int) or isinstance(resolution, bool):
        raise ValueError(
            "resolution must be an int, got {0}".format(type(resolution).__name__)
        )
    if resolution < min_size or resolution > max_size:
        raise ValueError(
            "resolution {0} out of range [{1}, {2}]".format(resolution, min_size, max_size)
        )
    return resolution


# ---------------------------------------------------------------------------
# Section 3: pattern tables (regex, description)
# ---------------------------------------------------------------------------
DANGEROUS_PATTERNS = [
    # 文件系统破坏
    (r"\bshutil\.rmtree\b", "shutil.rmtree 删除目录树"),
    (r"\bos\.remove\b", "os.remove 删除文件"),
    (r"\bos\.unlink\b", "os.unlink 删除文件"),
    (r"\bos\.rmdir\b", "os.rmdir 删除空目录"),
    (r"\bos\.system\b", "os.system 执行 shell 命令"),
    (r"\bos\.popen\b", "os.popen 执行 shell 命令"),
    (r"\bos\.execvpe?\b", "os.execvp 替换进程"),
    (r"\bos\.spawn[lv]?p?e?\b", "os.spawn* 派生子进程"),
    (r"\bsubprocess\.(run|call|Popen|check_output|check_call)\b", "subprocess 启动子进程"),
    # 远程下载 / 网络
    (r"\burlopen\b", "urllib.urlopen 远程访问"),
    (r"\brequests\.(get|post|put|delete|patch|head|request)\b", "requests 发起 HTTP 请求"),
    (r"\bhttp\.client\.", "http.client 直接 HTTP"),
    # 代码动态执行
    (r"\beval\s*\(", "eval 动态执行"),
    (r"\bexec\s*\(", "exec 动态执行"),
    (r"\bcompile\s*\(", "compile 动态编译"),
    (r"\b__import__\s*\(", "__import__ 动态导入"),
    (r"\bimportlib\.", "importlib 动态导入"),
    # 文件写入
    (r"\bopen\s*\([^)]*['\"](?:w|a|x|r\+|w\+|a\+)['\"]", "open 写模式打开文件"),
    (r"\.write\s*\(", "调用 .write 写入"),
    (r"\bpathlib\.Path\b[^)]*\.write_text\b", "Path.write_text 写文件"),
    (r"\bpathlib\.Path\b[^)]*\.write_bytes\b", "Path.write_bytes 写文件"),
    # 反射 / 进程控制
    (r"\bctypes\.", "ctypes 调用原生库"),
    (r"\bsocket\.socket\b", "socket.socket 建立套接字"),
    (r"\bpty\.spawn\b", "pty.spawn 伪终端"),
    (r"\bcommands\.(getoutput|getstatusoutput)\b", "commands.* shell 调用"),
    # 环境变量 / 系统信息泄漏
    (r"\bos\.environ\b", "访问 os.environ 环境变量"),
    (r"\bos\.getenv\b", "os.getenv 读环境变量"),
    # 反射式属性访问
    (r"\bgetattr\s*\([^,]+,\s*['\"][^'\"]+['\"]", "反射 getattr"),
    (r"\bsetattr\s*\(", "反射 setattr"),
    # Pickle / marshal 反序列化
    (r"\bpickle\.(load|loads)\b", "pickle 反序列化"),
    (r"\bmarshal\.loads\b", "marshal 反序列化"),
]

HEAVY_GEOMETRY_PATTERNS = [
    (r"\.geometry\s*\(\s*\)", ".geometry() 取完整几何"),
    (r"\.prims\s*\(\s*\)", ".prims() 遍历基元"),
    (r"\.points\s*\(\s*\)", ".points() 遍历点"),
    (r"\.vertices\s*\(\s*\)", ".vertices() 遍历顶点"),
    (r"\.attribValue\s*\(\s*['\"]", ".attribValue() 读属性"),
    (r"\bSopNode\.geometry\b", "SopNode.geometry"),
]

MUTATION_PATTERNS = [
    (r"\.destroy\s*\(", ".destroy() 删除节点"),
    (r"\.delete\s*\(", ".delete() 删除"),
    (r"\.deleteItems\s*\(", ".deleteItems() 批量删除"),
    (r"hou\.node\s*\([^)]+\)\.destroy\b", "hou.node().destroy"),
    (r"hou\.node\s*\([^)]+\)\.delete\b", "hou.node().delete"),
    (r"\.createNode\s*\(", ".createNode 新建节点"),
    (r"\.createDigitalAsset\s*\(", ".createDigitalAsset 新建数字资产"),
    (r"\.setFirstInput\s*\(", ".setFirstInput 连线"),
    (r"\.setNextInput\s*\(", ".setNextInput 连线"),
    (r"\.setInput\s*\(", ".setInput 连线"),
    (r"\.moveToGoodPosition\s*\(", ".moveToGoodPosition"),
    (r"\.setRenderFlag\s*\(", ".setRenderFlag 渲染标志"),
    (r"\.setDisplayFlag\s*\(", ".setDisplayFlag 显示标志"),
    (r"\.setSelected\s*\(\s*True", ".setSelected(True) 选中"),
    (r"\.saveChildrenToFile\s*\(", ".saveChildrenToFile 保存子节点"),
    (r"\.saveToFile\s*\(", ".saveToFile 保存到文件"),
    (r"\.parm\s*\([^)]+\)\.set\s*\(", ".parm().set 改参数"),
]


# ---------------------------------------------------------------------------
# Section 4: detection helpers
# ---------------------------------------------------------------------------
def _detect_dangerous_code(code):
    """先正则匹配 DANGEROUS_PATTERNS，再走 AST 别名检测（核心安全要求）。"""
    hits = []
    if not isinstance(code, str):
        return hits
    for pat, desc in DANGEROUS_PATTERNS:
        if re.search(pat, code):
            hits.append(desc)

    # AST 别名检测：import os as o / from os import path as p / os = __import__('os')
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return hits

    sensitive_roots = {
        "os", "subprocess", "sys", "shutil", "socket", "ctypes",
        "importlib", "pickle", "marshal", "commands", "pty",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in sensitive_roots and alias.asname:
                    hits.append(
                        "AST alias: import {0} as {1}".format(alias.name, alias.asname)
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in sensitive_roots:
                for alias in node.names:
                    if alias.asname:
                        hits.append(
                            "AST alias: from {0} import {1} as {2}".format(
                                mod, alias.name, alias.asname
                            )
                        )
        elif isinstance(node, ast.Assign):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "__import__"
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and isinstance(value.args[0].value, str)
            ):
                mod = value.args[0].value.split(".")[0]
                if mod in sensitive_roots:
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            hits.append(
                                "AST alias: {0} = __import__('{1}')".format(tgt.id, mod)
                            )
    return hits


def _detect_heavy_geometry_code(code):
    """检测会拉取完整几何的高开销调用。"""
    if not isinstance(code, str):
        return []
    hits = []
    for pat, desc in HEAVY_GEOMETRY_PATTERNS:
        if re.search(pat, code):
            hits.append(desc)
    return hits


def _detect_mutation_code(code):
    """检测会修改场景/节点的变更操作。"""
    if not isinstance(code, str):
        return []
    hits = []
    for pat, desc in MUTATION_PATTERNS:
        if re.search(pat, code):
            hits.append(desc)
    return hits


def _detect_import_hou(code):
    """检测任何试图导入 hou 模块的形式：直接 / try 内 / 字符串拼接。"""
    if not isinstance(code, str):
        return []
    hits = []
    # 直接 import / from import（行首允许缩进）
    if re.search(r"(?m)^\s*import\s+hou\b", code):
        hits.append("direct: import hou")
    if re.search(r"(?m)^\s*from\s+hou\b", code):
        hits.append("direct: from hou import ...")
    # try 块内的 import hou
    if re.search(r"try\s*:[^\n]*\n[^\n]*import\s+hou\b", code):
        hits.append("try-block: import hou")
    # 字符串拼接 __import__("ho"+"u") / "h"+"ou"
    if re.search(
        r"""__import__\s*\(\s*['"][^'"]*['"]\s*\+\s*['"][^'"]*['"]""",
        code,
    ):
        hits.append("dynamic: __import__('ho'+'u')")
    if re.search(r"""__import__\s*\(\s*['"]hou['"]\s*\)""", code):
        hits.append("dynamic: __import__('hou')")
    return hits


# ---------------------------------------------------------------------------
# Section 5: truncation / response cap
# ---------------------------------------------------------------------------
def _truncate_output(output, max_size):
    """字符串截断到 max_size 字符。返回 (truncated, was_truncated)。"""
    if not output:
        return output, False
    if len(output) <= max_size:
        return output, False
    return output[:max_size], True


def _serialized_size(obj):
    """json.dumps 后的字节数。无法序列化时退回 repr 字节数。"""
    try:
        return len(json.dumps(obj, default=str).encode("utf-8"))
    except Exception:
        return len(repr(obj).encode("utf-8"))


def _estimate_response_size(data):
    """对 response 数据估算字节大小。"""
    return _serialized_size(data)


def _find_truncation_target(obj):
    """找到 dict 中最大的 list 值；找不到再递归一层；返回 (key, list_ref) 或 None。"""
    best_key = None
    best_list = None
    best_len = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list) and len(v) > best_len:
                best_key = k
                best_list = v
                best_len = len(v)
        if best_key is not None:
            return best_key, best_list
        for k, v in obj.items():
            if isinstance(v, dict):
                sub = _find_truncation_target(v)
                if sub is not None:
                    return sub
    return None


def apply_response_cap(data, max_bytes=16384):
    """二分查找最优截断点：先把列表长度折半试探，直到序列化结果 <= max_bytes。"""
    if _serialized_size(data) <= max_bytes:
        return data
    capped = copy.deepcopy(data)
    target = _find_truncation_target(capped)
    if target is None:
        # 没有任何可截断的 list；尝试字符串字段二分截断
        capped = _try_str_truncate(capped, max_bytes)
        if not capped.get("_truncated"):
            capped["_truncated"] = True
        capped["_original_size"] = _serialized_size(data)
        return capped

    key, original = target
    # 二分搜索前缀长度
    lo, hi = 0, len(original)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        capped[key] = original[:mid]
        capped["_truncated_count"] = len(original) - mid
        if _serialized_size(capped) <= max_bytes:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    capped[key] = original[:best]
    if best == 0:
        del capped[key]
    capped["_truncated"] = True
    capped["_original_size"] = _serialized_size(data)
    return capped


def _try_str_truncate(obj, max_bytes):
    """对 obj 中过大的字符串值做二分字符截断；至少标记 _truncated=True。"""
    if not isinstance(obj, dict):
        return obj
    changed = False
    for k, v in list(obj.items()):
        if isinstance(v, str) and len(v) > 0:
            # 二分寻找最大字符前缀
            lo, hi = 0, len(v)
            best = 0
            probe = copy.deepcopy(obj)
            while lo <= hi:
                mid = (lo + hi) // 2
                probe[k] = v[:mid]
                if _serialized_size(probe) <= max_bytes:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best > 0 and best < len(v):
                obj[k] = v[:best]
                changed = True
    obj["_truncated"] = True
    return obj


# ---------------------------------------------------------------------------
# Section 6: pagination
# ---------------------------------------------------------------------------
def paginate_list(items, limit, cursor):
    """分页切片。cursor 越界返回空页；limit<=0 返回空页；末尾 cursor 为 None。"""
    if limit <= 0:
        return [], 0
    start = max(0, cursor)
    end = start + limit
    if start >= len(items):
        return [], cursor
    page = items[start:end]
    next_cursor = end if end < len(items) else None
    return page, next_cursor


# ---------------------------------------------------------------------------
# Section 7: response metadata
# ---------------------------------------------------------------------------
def _add_response_metadata(result, **kwargs):
    """给 result 字典补上元数据键；已存在的键不覆盖。"""
    for k, v in kwargs.items():
        if k not in result:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Section 8: JSON-safe hou value serialization
# ---------------------------------------------------------------------------
def _json_safe_hou_value(hou, value, max_depth=5, _seen=None):
    """递归把 hou 值转换为 JSON-safe 结构。

    - hou 必须由调用方注入（不在本模块顶层 import）
    - hou.Ramp -> {"points": [...]}
    - hou.Vector / hou.Color / hou.EnumValue -> 各自适合的 JSON 表示
    - dict / list / tuple 递归；_seen 防循环，max_depth 截断
    """
    if _seen is None:
        _seen = set()

    # primitive passthrough
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    # cycle detection
    if id(value) in _seen:
        return "<circular reference>"

    _seen.add(id(value))

    if max_depth <= 0:
        return "<max depth exceeded>"

    # hou.EnumValue
    if getattr(hou, "EnumValue", None) is not None and isinstance(value, hou.EnumValue):
        return str(value)

    # hou.Vector / hou.Color -> list of floats
    for marker in ("Vector", "Color"):
        hou_type = getattr(hou, marker, None)
        if hou_type is not None and isinstance(value, hou_type):
            try:
                return [float(x) for x in value]
            except Exception:
                return str(value)

    # hou.Ramp -> {points: [...]}
    hou_ramp = getattr(hou, "Ramp", None)
    if hou_ramp is not None and isinstance(value, hou_ramp):
        try:
            points = []
            for p in getattr(value, "points", []) or []:
                pos = getattr(p, "position", None)
                val = getattr(p, "value", None)
                if pos is None and val is None:
                    # try iter
                    try:
                        pts = list(p)
                        pos, val = (pts + [None, None])[:2]
                    except Exception:
                        pos, val = None, None
                points.append((float(pos) if pos is not None else None,
                               float(val) if val is not None else None))
            return {"points": points}
        except Exception:
            return "<ramp>"

    # dict
    if isinstance(value, dict):
        return {
            str(k): _json_safe_hou_value(hou, v, max_depth - 1, _seen)
            for k, v in value.items()
        }

    # list / tuple
    if isinstance(value, (list, tuple)):
        return [
            _json_safe_hou_value(hou, v, max_depth - 1, _seen)
            for v in value
        ]

    # fall back to str()
    try:
        return str(value)
    except Exception:
        return "<unserializable>"


def _flatten_parm_templates(hou, parm_templates, max_depth=3):
    """把嵌套 parm template group 扁平化为列表。hou 参数由调用方注入。"""
    if not parm_templates:
        return []
    out = []
    _walk_templates(hou, parm_templates, out, max_depth, _seen=set())
    return out


def _walk_templates(hou, templates, out, depth, _seen):
    if depth <= 0:
        return
    for tpl in templates:
        if id(tpl) in _seen:
            continue
        _seen.add(id(tpl))
        try:
            # Accept either real hou parm-template objects or plain dicts.
            if isinstance(tpl, dict):
                out.append({
                    "name": tpl.get("name", ""),
                    "label": tpl.get("label", ""),
                    "type": str(tpl.get("type", "unknown")),
                    "num_components": tpl.get("num_components", tpl.get("numComponents", 1)),
                })
                continue
            out.append({
                "name": _call_or_attr(tpl, "name", ""),
                "label": _call_or_attr(tpl, "label", ""),
                "type": _parm_type_name(tpl),
                "num_components": _call_or_attr(tpl, "numComponents", 1),
            })
        except Exception:
            out.append({"name": "<error>", "label": "", "type": "unknown", "num_components": 0})


def _call_or_attr(obj, name, default):
    """若 obj.name 是 callable 则调用；否则直接返回属性值。"""
    val = getattr(obj, name, default)
    if callable(val):
        try:
            return val()
        except Exception:
            return default
    return val


def _parm_type_name(tpl):
    """提取 parm 类型名；与 hou 实际类型解耦，做 best-effort。"""
    type_name = None
    type_obj = getattr(tpl, "type", None)
    if type_obj is not None:
        if hasattr(type_obj, "name"):
            try:
                type_name = type_obj.name()
            except Exception:
                type_name = None
        if type_name is None:
            type_name = str(type_obj)
    return type_name or "unknown"


# ---------------------------------------------------------------------------
# Section 9: timeout exception
# ---------------------------------------------------------------------------
class ExecutionTimeoutError(Exception):
    """执行超时时抛出；handler 捕获后转为 error dict。"""

    def __init__(self, message="execution timed out"):
        super().__init__(message)
        self.message = message