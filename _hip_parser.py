"""`.hip`/`.hiplc`/`.hipnc` 离线只读解析器（纯 Python 标准库，不 import hou）。

本模块基于真实 Houdini 生成的 legacy cpio/odc archive 结构，best-effort
提取节点、连接、序列化参数、postit 文本与 netbox label，**不启动也不
import hou**，**不宣称完整还原 scene**。设计依据见
``openspec/changes/add-hip-offline-parser/design.md`` 与实测结论：

- archive 为 odc cpio：每个 entry 都是 76 字节 ASCII header，magic
  ``070707``；``namesize`` 位于 ``header[59:65]``、``filesize`` 位于
  ``header[65:76]``（均为 ASCII 八进制），随后是 name（含尾部 NUL）与
  body。entry 之间**无 padding**，archive 以 name 为 ``TRAILER!!!``、
  filesize 为 0 的 entry 正常结束。
- **不存在**额外的全局 header，header 里也没有节点数 / Houdini 版本 /
  段表。``save_version`` 仅从 ``.variables`` 的 ``_HIP_SAVEVERSION`` 明确
  提取，缺失时为 ``None``。
- 节点由所有 ``<path>.init`` 建立（``type = <t>``）；``<path>.def`` 提供
  ``comment``/``position``/``inputsNamed3``（或 ``inputs``）；``<path>.parm``
  在 ``include_params=True`` 时以**序列化原始文本**返回（不求值、不比较
  默认、不保证动画完整）；``<path>.postitinit``/``.postitdef`` 与
  ``<path>.netboxinit`` 分别提供 postit 文本与 netbox label。同 ``<path>``
  的各 section 通过精确前缀关联，**不依赖出现顺序**。
- reader 严格按 entry 边界读取，**绝不**扫描 body 内的 magic 来重新同步。

资源限额（D4）：file bytes / entry count / single-section bytes /
total-section bytes / node count 五类硬上限，环境变量只能收紧（取
``min(硬默认, env)``），不能放宽；``max_depth`` clamp 到 ``[1,64]``，仅
裁剪输出树，不替代上述限额。

错误契约（D6）：
- 扩展名非 ``.hip/.hiplc/.hipnc`` → ``unsupported_extension``（不读文件）。
- 首个 header bad magic / 非法八进制 / 不足 76 字节 → ``invalid_archive``
  （无 partial）。
- 至少一个完整 entry 后出现 bad magic / 非法八进制 → ``corrupt_archive`` +
  partial。
- 至少一个完整 entry 后 header/name/body 短读或 EOF 前无 ``TRAILER!!!`` →
  ``truncated_archive`` + partial（partial 不含截断 body）。
- 命中任一资源限额 → ``resource_limit_exceeded`` + 命中 limit/value + partial。
- I/O / 未预期异常 → ``hip_io_error`` / 结构化 error，不越过 bridge。
- 未知 section suffix 跳过并计 ``skipped_sections``，不算损坏。

重复 entry（D5）：相同完整 entry name 多次出现时，最后一个**完整读取且在
限额内**的 entry 覆盖前值并累加 ``duplicate_entries``；截断 / 损坏 / 超限
的重复 entry 不覆盖先前完整值；重复 ``<path>.init`` 不产生重复 node path。

R4：仅 stdlib，顶层不 import hou / 不 import 第三方库。
R6：``parse_hip_offline`` 的 success 与 error-partial 结果都过
``apply_response_cap``（defense-in-depth）。
"""

import os
import re

try:
    from . import _common as cmn
except ImportError:
    try:
        import _common as cmn  # type: ignore
    except ImportError:
        cmn = None


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MAGIC = b"070707"
HEADER_SIZE = 76
# header 切片：namesize=[59:65], filesize=[65:76]
_NAMESIZE_SLICE = slice(59, 65)
_FILESIZE_SLICE = slice(65, 76)

SUPPORTED_EXTENSIONS = (".hip", ".hiplc", ".hipnc")

# section suffix 白名单（其余 suffix → skipped_sections）
SUFFIX_INIT = "init"
SUFFIX_DEF = "def"
SUFFIX_PARM = "parm"
SUFFIX_POSTIT_INIT = "postitinit"
SUFFIX_POSTIT_DEF = "postitdef"
SUFFIX_NETBOX_INIT = "netboxinit"
SUFFIX_VARIABLES = "variables"  # 特殊：name 恰为 ".variables"
RECOGNIZED_SUFFIXES = frozenset((
    SUFFIX_INIT, SUFFIX_DEF, SUFFIX_PARM,
    SUFFIX_POSTIT_INIT, SUFFIX_POSTIT_DEF, SUFFIX_NETBOX_INIT,
    SUFFIX_VARIABLES,
))

# 资源限额硬默认（D4）。环境变量只能收紧（min）。
_DEFAULT_MAX_FILE_BYTES = 536870912            # 512 MiB
_DEFAULT_MAX_ENTRIES = 100000
_DEFAULT_MAX_SECTION_BYTES = 67108864          # 64 MiB
_DEFAULT_MAX_TOTAL_SECTION_BYTES = 536870912   # 512 MiB
_DEFAULT_MAX_NODES = 50000

DEFAULT_MAX_BYTES = 16384

_MAX_DEPTH_LOWER = 1
_MAX_DEPTH_UPPER = 64
_DEFAULT_MAX_DEPTH = 10

# 字段提取正则
_RE_SAVEVER = re.compile(r"_HIP_SAVEVERSION\s*=\s*'([^']+)'")
_RE_COMMENT = re.compile(r'^comment\s+"(.*)"\s*$', re.M)
_RE_POSITION = re.compile(r'^position\s+(-?\S+)\s+(-?\S+)\s*$', re.M)
_RE_INPUTS_NAMED = re.compile(
    r'^inputsNamed3\s*\r?\n\{(.*?)^\}', re.M | re.S)
_RE_INPUTS_PLAIN = re.compile(
    r'^inputs\s*\r?\n\{(.*?)^\}', re.M | re.S)
_RE_POSTIT_TEXT = re.compile(r'^text\s+"(.*)"\s*$', re.M)
_RE_NETBOX_LABEL = re.compile(r'comment\s*:=\s*([^;]+);')


# ---------------------------------------------------------------------------
# 统一 error（task 3.1）
# ---------------------------------------------------------------------------
class HipParseError(Exception):
    """归一化为稳定 code/message/details 的解析错误，可携带 partial。

    ``partial`` 为 None 表示「无 partial」（如 invalid_archive）；否则为本
    错误发生前由完整 entry 构建的结果 dict（nodes/connections/...）。
    """

    def __init__(self, code, message, details=None, partial=None):
        super(HipParseError, self).__init__(message)
        self.code = code
        self.message = message
        self.details = details if isinstance(details, dict) else {}
        self.partial = partial


# ---------------------------------------------------------------------------
# 限额（env 只能收紧）
# ---------------------------------------------------------------------------
def _env_int(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return default
    if val <= 0:
        return default
    return val


def effective_limits():
    """返回当前生效的 5 类硬限额 dict（env 取 min 收紧，不可放宽）。"""
    return {
        "max_file_bytes": min(
            _DEFAULT_MAX_FILE_BYTES,
            _env_int("HOUDINI_MCP_HIP_MAX_FILE_BYTES", _DEFAULT_MAX_FILE_BYTES)),
        "max_entries": min(
            _DEFAULT_MAX_ENTRIES,
            _env_int("HOUDINI_MCP_HIP_MAX_ENTRIES", _DEFAULT_MAX_ENTRIES)),
        "max_section_bytes": min(
            _DEFAULT_MAX_SECTION_BYTES,
            _env_int("HOUDINI_MCP_HIP_MAX_SECTION_BYTES", _DEFAULT_MAX_SECTION_BYTES)),
        "max_total_section_bytes": min(
            _DEFAULT_MAX_TOTAL_SECTION_BYTES,
            _env_int("HOUDINI_MCP_HIP_MAX_TOTAL_SECTION_BYTES",
                     _DEFAULT_MAX_TOTAL_SECTION_BYTES)),
        "max_nodes": min(
            _DEFAULT_MAX_NODES,
            _env_int("HOUDINI_MCP_HIP_MAX_NODES", _DEFAULT_MAX_NODES)),
    }


def _clamp_depth(max_depth):
    try:
        depth = int(max_depth)
    except (ValueError, TypeError):
        depth = _DEFAULT_MAX_DEPTH
    if depth < _MAX_DEPTH_LOWER:
        depth = _MAX_DEPTH_LOWER
    if depth > _MAX_DEPTH_UPPER:
        depth = _MAX_DEPTH_UPPER
    return depth


# ---------------------------------------------------------------------------
# 流式 entry reader
# ---------------------------------------------------------------------------
def _read_exact(fh, n):
    """从二进制文件读取恰好 n 字节；EOF 时返回实际读到的字节（可能 < n）。"""
    if n <= 0:
        return b""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = fh.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if not chunks:
        return b""
    if len(chunks) == 1:
        return chunks[0]
    return b"".join(chunks)


class _ReadStream(object):
    """封装 fh 与已消费字节数，便于在 partial 中报告 bytes_consumed。"""

    def __init__(self, fh):
        self._fh = fh
        self.consumed = 0  # 已从流中读出的总字节数

    def take(self, n):
        data = _read_exact(self._fh, n)
        self.consumed += len(data)
        return data


def _stream_entries(stream, limits, include_params, depth):
    """逐 entry 读取，返回 (section_map, meta)。

    section_map: name(str) -> body(bytes)，last-complete-wins。
    meta: dict(complete_entries, duplicate_entries, skipped_sections,
              bytes_consumed, trailer_seen, section_bytes)。

    任何损坏/截断/限额命中都抛 HipParseError，其 partial 由已完成的
    section_map 构建（complete_entries==0 时 partial 为空结果）。partial
    的 include_params / max_depth 与正式构建一致。
    """
    section_map = {}
    complete_entries = 0
    duplicate_entries = 0
    skipped_sections = 0
    total_section_bytes = 0
    bytes_consumed = 0  # 最后一次完整 entry 边界 / trailer 结束位置
    trailer_seen = False
    saw_any_header = False

    def current_partial():
        return _build_result(
            section_map,
            duplicate_entries=duplicate_entries,
            skipped_sections=skipped_sections,
            complete_entries=complete_entries,
            bytes_consumed=bytes_consumed,
            trailer_seen=trailer_seen,
            section_bytes=total_section_bytes,
            include_params=include_params,
            max_depth=depth,
            limits=limits,
            for_partial=True)

    while True:
        # ---- header ----
        header = stream.take(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            # 短读（含完全 EOF：len==0）
            if not saw_any_header:
                # 首个 header 即不足 → invalid_archive，无 partial
                raise HipParseError(
                    "invalid_archive",
                    "首 header 不足 76 字节（实际 {0}）".format(len(header)),
                    {"got": len(header)}, partial=None)
            # 已有完整 entry → truncated
            raise HipParseError(
                "truncated_archive",
                "header 短读（实际 {0}，需 {1}）".format(
                    len(header), HEADER_SIZE),
                {"got": len(header), "need": HEADER_SIZE},
                partial=current_partial())
        saw_any_header = True

        if header[0:6] != MAGIC:
            magic = header[0:6]
            if complete_entries == 0:
                raise HipParseError(
                    "invalid_archive",
                    "首 header magic 非 070707（{0!r}）".format(
                        magic.decode("latin-1", "replace")),
                    {"magic": magic.decode("latin-1", "replace")},
                    partial=None)
            raise HipParseError(
                "corrupt_archive",
                "entry 边界 magic 非 070707（{0!r}）".format(
                    magic.decode("latin-1", "replace")),
                {"magic": magic.decode("latin-1", "replace")},
                partial=current_partial())

        try:
            namesize = int(header[_NAMESIZE_SLICE], 8)
            filesize = int(header[_FILESIZE_SLICE], 8)
        except ValueError:
            if complete_entries == 0:
                raise HipParseError(
                    "invalid_archive", "首 header 含非法八进制长度",
                    {"namesize_field": header[_NAMESIZE_SLICE].decode(
                        "latin-1", "replace"),
                     "filesize_field": header[_FILESIZE_SLICE].decode(
                        "latin-1", "replace")},
                    partial=None)
            raise HipParseError(
                "corrupt_archive", "header 含非法八进制长度",
                {"namesize_field": header[_NAMESIZE_SLICE].decode(
                    "latin-1", "replace"),
                 "filesize_field": header[_FILESIZE_SLICE].decode(
                    "latin-1", "replace")},
                partial=current_partial())

        if namesize <= 0:
            code = "corrupt_archive" if complete_entries else "invalid_archive"
            raise HipParseError(
                code, "namesize 非法（{0}）".format(namesize),
                {"namesize": namesize},
                partial=(None if complete_entries == 0
                         else current_partial()))

        # ---- 声明 body 的 section/total 字节限额（读 body 之前）----
        if filesize > limits["max_section_bytes"]:
            raise HipParseError(
                "resource_limit_exceeded",
                "单 section 声明 {0} 字节超 max_section_bytes={1}".format(
                    filesize, limits["max_section_bytes"]),
                {"limit": "max_section_bytes",
                 "value": filesize, "max": limits["max_section_bytes"]},
                partial=current_partial())
        if total_section_bytes + filesize > limits["max_total_section_bytes"]:
            raise HipParseError(
                "resource_limit_exceeded",
                "累计 section 声明 {0} 字节超 max_total_section_bytes={1}".format(
                    total_section_bytes + filesize,
                    limits["max_total_section_bytes"]),
                {"limit": "max_total_section_bytes",
                 "value": total_section_bytes + filesize,
                 "max": limits["max_total_section_bytes"]},
                partial=current_partial())

        # ---- name ----
        name_buf = stream.take(namesize)
        if len(name_buf) < namesize:
            raise HipParseError(
                "truncated_archive",
                "name 短读（实际 {0}，需 {1}）".format(
                    len(name_buf), namesize),
                {"got": len(name_buf), "need": namesize},
                partial=current_partial())
        name = name_buf.rstrip(b"\x00").decode("latin-1", "replace")

        # ---- trailer ----
        if name == "TRAILER!!!":
            trailer_seen = True
            bytes_consumed = stream.consumed
            break

        # ---- body ----
        body = stream.take(filesize)
        if len(body) < filesize:
            raise HipParseError(
                "truncated_archive",
                "section {0!r} body 短读（实际 {1}，需 {2}）".format(
                    name, len(body), filesize),
                {"section": name, "got": len(body), "need": filesize},
                partial=current_partial())

        # ---- entry 数量限额（在落账前判定，partial 不含超限 entry）----
        if complete_entries + 1 > limits["max_entries"]:
            raise HipParseError(
                "resource_limit_exceeded",
                "完整 entry 数将达到 {0} 超 max_entries={1}".format(
                    complete_entries + 1, limits["max_entries"]),
                {"limit": "max_entries",
                 "value": complete_entries + 1, "max": limits["max_entries"]},
                partial=current_partial())

        # ---- 完整 entry 落账（last-complete-wins）----
        if name in section_map:
            duplicate_entries += 1
        section_map[name] = body
        total_section_bytes += filesize
        complete_entries += 1
        bytes_consumed = stream.consumed

    meta = {
        "complete_entries": complete_entries,
        "duplicate_entries": duplicate_entries,
        "skipped_sections": skipped_sections,
        "bytes_consumed": bytes_consumed,
        "trailer_seen": trailer_seen,
        "section_bytes": total_section_bytes,
    }
    # 未知 section 统计放在 builder 里做（builder 知道 suffix 白名单）
    return section_map, meta


# ---------------------------------------------------------------------------
# 路径与 section 辅助
# ---------------------------------------------------------------------------
def _split_suffix(name):
    """返回 (path, suffix)。无 '.' 时 suffix=''。

    注意：以 '.' 开头的特殊 section（如 '.variables'）→ path='', suffix='variables'。
    """
    idx = name.rfind(".")
    if idx < 0:
        return name, ""
    return name[:idx], name[idx + 1:]


def _base_name(path):
    if "/" in path:
        return path.rsplit("/", 1)[1]
    return path


def _parent_path(path):
    if "/" in path:
        return path.rsplit("/", 1)[0]
    return ""


def _resolve_source(src_name, parent):
    """把 def 里的 sibling source 解析为完整路径。

    含 '/' 或疑似 op 表达式的原样返回；纯名字按目标节点 parent 拼成完整路径。
    """
    if not src_name:
        return src_name
    if "/" in src_name or src_name.startswith("op"):
        return src_name
    if parent:
        return parent + "/" + src_name
    return src_name


def _decode(body):
    if not body:
        return ""
    return body.decode("utf-8", "replace")


def _parse_type(init_text):
    # type = geo
    m = re.search(r'^type\s*=\s*(\S+)\s*$', init_text, re.M)
    return m.group(1) if m else None


def _parse_comment_position(def_text):
    comment = None
    m = _RE_COMMENT.search(def_text)
    if m:
        comment = m.group(1)
    position = None
    m = _RE_POSITION.search(def_text)
    if m:
        try:
            position = [float(m.group(1)), float(m.group(2))]
        except ValueError:
            position = None
    return comment, position


def _parse_inputs(def_text, parent):
    """优先 inputsNamed3，缺失时回退 inputs。返回 input dict 列表。"""
    block = None
    m = _RE_INPUTS_NAMED.search(def_text)
    if m:
        block = m.group(1)
    else:
        m = _RE_INPUTS_PLAIN.search(def_text)
        if m:
            block = m.group(1)
    if block is None:
        return []
    inputs = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line in ("{", "}"):
            continue
        tokens = line.split()
        if len(tokens) < 3:
            continue
        try:
            idx = int(tokens[0])
        except ValueError:
            continue
        src_name = tokens[1]
        try:
            out = int(tokens[2])
        except ValueError:
            out = 0
        inputs.append({
            "input_index": idx,
            "source": _resolve_source(src_name, parent),
            "source_output": out,
        })
    return inputs


# ---------------------------------------------------------------------------
# 由 section map 构建结果（path-based，顺序无关）
# ---------------------------------------------------------------------------
def _build_result(section_map, *, duplicate_entries, skipped_sections,
                  complete_entries, bytes_consumed, trailer_seen,
                  section_bytes, include_params, max_depth, limits,
                  for_partial=False):
    """从完整 entry 的 section map 构建 nodes/connections/postits/netboxes。

    顺序无关：先收集所有 .init 路径，再用精确 path 关联 .def/.parm。
    for_partial=True 时跳过 max_nodes 限额强约束（partial 只如实反映已读）。
    """
    # ---- save_version ----
    save_version = None
    variables_body = section_map.get(".variables")
    if variables_body is not None:
        m = _RE_SAVEVER.search(_decode(variables_body))
        if m:
            save_version = m.group(1)

    # ---- skipped sections 统计（caller 未必算，这里兜底精确计算）----
    if skipped_sections == 0:
        skipped_sections = sum(
            1 for name in section_map
            if _split_suffix(name)[1] not in RECOGNIZED_SUFFIXES)

    # ---- node paths（来自 .init 唯一路径）----
    init_paths = sorted({
        path for path, suffix in (
            _split_suffix(name) for name in section_map)
        if suffix == SUFFIX_INIT and path
    })

    node_limit_hit = False
    node_count_limit_value = len(init_paths)
    if not for_partial and len(init_paths) > limits["max_nodes"]:
        node_limit_hit = True
        init_paths = init_paths[:limits["max_nodes"]]

    nodes = []
    connections = []
    for path in init_paths:
        init_body = section_map.get(path + "." + SUFFIX_INIT)
        node_type = None
        if init_body is not None:
            node_type = _parse_type(_decode(init_body))
        node = {
            "path": path,
            "name": _base_name(path),
            "parent": _parent_path(path),
            "type": node_type,
        }

        def_body = section_map.get(path + "." + SUFFIX_DEF)
        if def_body is not None:
            def_text = _decode(def_body)
            comment, position = _parse_comment_position(def_text)
            if comment is not None:
                node["comment"] = comment
            if position is not None:
                node["position"] = position
            inputs = _parse_inputs(def_text, node["parent"])
            if inputs:
                node["inputs"] = inputs
                for inp in inputs:
                    connections.append({
                        "from": inp["source"],
                        "to": path,
                        "input_index": inp["input_index"],
                        "source_output": inp["source_output"],
                    })

        if include_params:
            parm_body = section_map.get(path + "." + SUFFIX_PARM)
            if parm_body is not None:
                # 序列化原始文本：不求值、不比较默认、不保证动画完整。
                node["parameters"] = _decode(parm_body)

        nodes.append(node)

    # ---- postits（.postitdef 提供文本）----
    postits = []
    for name in sorted(section_map):
        path, suffix = _split_suffix(name)
        if suffix == SUFFIX_POSTIT_DEF and path:
            text = None
            m = _RE_POSTIT_TEXT.search(_decode(section_map[name]))
            if m:
                text = m.group(1)
            postits.append({
                "context": _parent_path(path),
                "name": _base_name(path),
                "text": text,
            })

    # ---- netboxes（.netboxinit 提供 label）----
    netboxes = []
    for name in sorted(section_map):
        path, suffix = _split_suffix(name)
        if suffix == SUFFIX_NETBOX_INIT and path:
            label = None
            m = _RE_NETBOX_LABEL.search(_decode(section_map[name]))
            if m:
                label = m.group(1).strip()
            netboxes.append({
                "context": _parent_path(path),
                "name": _base_name(path),
                "label": label,
            })

    # ---- 输出树（max_depth 仅裁剪结构树）----
    structure = _build_structure(nodes, max_depth)

    result = {
        "save_version": save_version,
        "nodes": nodes,
        "connections": connections,
        "postits": postits,
        "netboxes": netboxes,
        "structure": structure,
        "metadata": {
            "complete_entries": complete_entries,
            "bytes_consumed": bytes_consumed,
            "trailer_seen": trailer_seen,
            "duplicate_entries": duplicate_entries,
            "skipped_sections": skipped_sections,
            "section_count": len(section_map),
            "node_count": len(nodes),
            "section_bytes": section_bytes,
            "max_depth": max_depth,
            "limits": dict(limits),
            "node_limit_hit": node_limit_hit,
            "node_count_limit_value": node_count_limit_value,
        },
    }
    return result


def _build_structure(nodes, max_depth):
    """按 path 层级构建 forest，depth>max_depth 处裁剪。flat nodes 不受影响。"""
    by_path = {n["path"]: n for n in nodes}
    children_map = {}
    roots = []
    for n in nodes:
        parent = n["parent"]
        if parent in by_path:
            children_map.setdefault(parent, []).append(n)
        else:
            roots.append(n)
    roots.sort(key=lambda n: n["path"])
    for key in children_map:
        children_map[key].sort(key=lambda n: n["path"])

    def build(n, depth):
        node = {
            "path": n["path"],
            "name": n["name"],
            "type": n.get("type"),
        }
        kids = children_map.get(n["path"], [])
        if depth >= max_depth:
            node["children"] = []
            if kids:
                node["truncated"] = True
        else:
            node["children"] = [build(k, depth + 1) for k in kids]
        return node

    return [build(r, 1) for r in roots]


# ---------------------------------------------------------------------------
# envelope 组装（task 3.x）
# ---------------------------------------------------------------------------
def _success_envelope(file_path, result):
    env = {"status": "success", "file_path": file_path}
    env.update(result)
    return env


def _error_envelope(exc, file_path):
    env = {
        "status": "error",
        "file_path": file_path,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    }
    if isinstance(exc.partial, dict):
        env.update(exc.partial)
    else:
        # 无 partial（invalid_archive 等）：仍给出一致 shape 的空结果。
        env.update({
            "save_version": None,
            "nodes": [],
            "connections": [],
            "postits": [],
            "netboxes": [],
            "structure": [],
            "metadata": {
                "complete_entries": 0,
                "bytes_consumed": 0,
                "trailer_seen": False,
                "duplicate_entries": 0,
                "skipped_sections": 0,
                "section_count": 0,
                "node_count": 0,
                "section_bytes": 0,
                "max_depth": _DEFAULT_MAX_DEPTH,
                "limits": dict(effective_limits()),
                "node_limit_hit": False,
                "node_count_limit_value": 0,
            },
        })
    return env


# ---------------------------------------------------------------------------
# 顶层入口（task 3.4 / 3.5）
# ---------------------------------------------------------------------------
def _validate_and_stat(file_path, limits):
    if not isinstance(file_path, str) or not file_path:
        raise HipParseError(
            "unsupported_extension", "file_path 必须是非空字符串",
            {"file_path": file_path}, partial=None)
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HipParseError(
            "unsupported_extension",
            "仅支持 .hip/.hiplc/.hipnc，得到扩展名 {0!r}".format(ext),
            {"extension": ext}, partial=None)
    try:
        st = os.stat(file_path)
    except FileNotFoundError:
        raise HipParseError(
            "hip_not_found", "文件不存在: {0}".format(file_path),
            {"path": file_path}, partial=None)
    except OSError as exc:
        raise HipParseError(
            "hip_io_error", "无法 stat 文件: {0}: {1}".format(file_path, exc),
            {"path": file_path, "exception": str(exc)}, partial=None)
    if st.st_size > limits["max_file_bytes"]:
        raise HipParseError(
            "resource_limit_exceeded",
            "文件 {0} 字节超 max_file_bytes={1}".format(
                st.st_size, limits["max_file_bytes"]),
            {"limit": "max_file_bytes",
             "value": st.st_size, "max": limits["max_file_bytes"]},
            partial=None)
    return st.st_size


def _parse_impl(file_path, include_params, max_depth):
    limits = effective_limits()
    depth = _clamp_depth(max_depth)
    _validate_and_stat(file_path, limits)

    try:
        fh = open(file_path, "rb")
    except FileNotFoundError:
        raise HipParseError(
            "hip_not_found", "文件不存在: {0}".format(file_path),
            {"path": file_path}, partial=None)
    except OSError as exc:
        raise HipParseError(
            "hip_io_error", "无法打开文件: {0}: {1}".format(file_path, exc),
            {"path": file_path, "exception": str(exc)}, partial=None)

    try:
        stream = _ReadStream(fh)
        section_map, meta = _stream_entries(stream, limits, include_params, depth)
    finally:
        try:
            fh.close()
        except OSError:
            pass

    # _stream_entries 仅在见到 TRAILER!!! 时正常返回；EOF 前无 trailer 的
    # 情况已在 reader 内以 truncated_archive + partial 抛出，故此处
    # meta["trailer_seen"] 恒为 True。
    result = _build_result(
        section_map,
        duplicate_entries=meta["duplicate_entries"],
        skipped_sections=meta["skipped_sections"],
        complete_entries=meta["complete_entries"],
        bytes_consumed=meta["bytes_consumed"],
        trailer_seen=True,
        section_bytes=meta["section_bytes"],
        include_params=include_params,
        max_depth=depth,
        limits=limits,
        for_partial=False)

    # max_nodes 限额命中（build 阶段）→ resource_limit_exceeded + partial
    if result["metadata"]["node_limit_hit"]:
        raise HipParseError(
            "resource_limit_exceeded",
            ".init 唯一路径数超 max_nodes={0}".format(limits["max_nodes"]),
            {"limit": "max_nodes",
             "value": result["metadata"]["node_count_limit_value"],
             "max": limits["max_nodes"]},
            partial=result)

    return _success_envelope(file_path, result)


def parse_hip_offline(file_path, include_params=False, max_depth=10,
                      response_cap_fn=None, max_bytes=DEFAULT_MAX_BYTES):
    """离线 best-effort 解析 ``.hip``/``.hiplc``/``.hipnc``，无需 Houdini 连接。

    本函数 **不建立 Houdini TCP 连接、不 import hou**。它按真实 legacy
    cpio/odc entry 逐个流式读取，best-effort 提取节点 type、def
    comment/position/connections、可选 parm 序列化原始值、postit 文本与
    netbox label，并对不可信输入施加 file/entry/section/total/node 五类
    硬限额（``max_depth`` 仅裁输出树）。

    返回统一 envelope：
    - success：``{"status":"success","file_path":..,"save_version":..,
      "nodes":[..],"connections":[..],"postits":[..],"netboxes":[..],
      "structure":[..],"metadata":{...}}``
    - error：同形 dict 加 ``error:{code,message,details}``，并尽可能附带由
      完整 entry 构成的 partial（``trailer_seen=false``，不含截断 body）。

    error code：``unsupported_extension`` / ``invalid_archive`` /
    ``corrupt_archive`` / ``truncated_archive`` /
    ``resource_limit_exceeded`` / ``hip_not_found`` / ``hip_io_error``。

    success 与 error-partial 结果**均过** ``apply_response_cap``
    （``response_cap_fn`` 为 None 时回退到 ``_common.apply_response_cap``，
    再为 None 时直接返回）。

    best-effort 边界：**不**求值参数、不比较默认、不保证动画/表达式完整、
    不还原 sticky/netbox geometry/membership、不解析 embedded HDA。
    """
    try:
        env = _parse_impl(file_path, include_params, max_depth)
    except HipParseError as exc:
        env = _error_envelope(exc, file_path)
    except (OSError, IOError) as exc:
        env = _error_envelope(
            HipParseError("hip_io_error", str(exc),
                          {"exception": str(exc)}),
            file_path)
    except Exception as exc:  # noqa: BLE001  统一兜底，绝不越过 bridge
        env = _error_envelope(
            HipParseError("hip_io_error", "unexpected error: {0}".format(exc),
                          {"exception_type": type(exc).__name__}),
            file_path)

    # R6：success / error 均过 response cap（defense-in-depth）。
    cap_fn = response_cap_fn
    if cap_fn is None and cmn is not None:
        cap_fn = getattr(cmn, "apply_response_cap", None)
    if callable(cap_fn):
        try:
            capped = cap_fn(env, max_bytes)
        except Exception:
            capped = None
        if isinstance(capped, dict):
            return capped
    return env
