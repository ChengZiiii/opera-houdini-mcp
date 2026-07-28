"""外部/houdinimcp/_graph_edit.py — PR 9 图编辑增强的纯函数实现。

提供 5 个独立、hou 可注入的图编辑操作。所有函数接受 hou 作为第一参数，
方便在单测中以 stub 替换。

设计原则：
- hou 隔离：函数第一参数是 hou（与 _materials / _hscript / _scene 风格一致）。
- 错误处理：节点不存在抛 ValueError；颜色分量超出 [0,1] 自动 clamp；
  network_box 中缺失的节点静默跳过。
- 返回值：所有函数返回 dict，便于 bridge 直接透传。
"""
import os
import re
import subprocess
import sys
import tempfile

from . import _common as cmn


# ---------------------------------------------------------------------------
# add-node-parameter-vex-tools：节点重命名 / 复制 / 移动。
# hou 注入；预检 target network category / locked HDA / name 冲突。
# hou.copyNodesTo / hou.moveNodesTo 由 Houdini 自身负责 unique name 兜底，
# 但我们额外做 name 冲突的显式预检，让错误更明确。
# ---------------------------------------------------------------------------
def rename_node(hou, path, new_name):
    """重命名同级节点。

    Args:
        hou: hou 模块或 stub。
        path: 目标节点路径。
        new_name: 期望的新名。

    Returns:
        {"path": ..., "old_name": ..., "new_name": ...}
    """
    node = hou.node(path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(path))
    if not isinstance(new_name, str) or not new_name:
        raise ValueError(u"new_name 必须为非空字符串")
    old_name = node.name()
    parent = node.parent()
    if parent is not None and new_name != old_name:
        if parent.node(new_name) is not None:
            raise ValueError(
                u"同名节点已存在: {0}/{1}".format(parent.path(), new_name)
            )
    node.setName(new_name)
    return {
        "path": node.path(),
        "old_name": old_name,
        "new_name": node.name(),
    }


def copy_node(hou, src_path, dest_parent, name=None):
    """复制单个节点到 dest_parent 下，返回新节点路径。

    hou.copyNodesTo（H21+）不接受 ``name`` kwarg；先 copy 再在 dest
    下做同名预检 + setName。

    Args:
        hou: hou 模块或 stub。
        src_path: 源节点路径。
        dest_parent: 目标父网络路径。
        name: 可选，新节点名；None 由 hou.copyNodesTo 决定。

    Returns:
        {"src_path": ..., "path": ..., "name": ...}
    """
    src = hou.node(src_path)
    if src is None:
        raise ValueError(u"源节点不存在: {0}".format(src_path))
    parent = hou.node(dest_parent)
    if parent is None:
        raise ValueError(u"目标父网络不存在: {0}".format(dest_parent))
    src_cat = src.parent().childTypeCategory() if src.parent() is not None else None
    dest_cat = parent.childTypeCategory()
    if src_cat is not None and dest_cat is not None and src_cat != dest_cat:
        raise ValueError(
            u"category 不匹配: src={0} dest={1}".format(src_cat, dest_cat)
        )
    if name is not None and parent.node(name) is not None:
        raise ValueError(
            u"同名节点已存在: {0}/{1}".format(dest_parent, name)
        )
    new_nodes = hou.copyNodesTo([src], parent)
    if not new_nodes:
        raise ValueError(u"copyNodesTo 未返回新节点")
    new_node = new_nodes[0]
    if name is not None and new_node.name() != name:
        new_node.setName(name)
    return {
        "src_path": src.path(),
        "path": new_node.path(),
        "name": new_node.name(),
    }


def move_node(hou, src_path, dest_parent):
    """移动单个节点到 dest_parent 下，返回新节点路径。

    Args:
        hou: hou 模块或 stub。
        src_path: 源节点路径。
        dest_parent: 目标父网络路径。

    Returns:
        {"src_path": ..., "path": ...}
    """
    src = hou.node(src_path)
    if src is None:
        raise ValueError(u"源节点不存在: {0}".format(src_path))
    parent = hou.node(dest_parent)
    if parent is None:
        raise ValueError(u"目标父网络不存在: {0}".format(dest_parent))
    src_cat = src.parent().childTypeCategory() if src.parent() is not None else None
    dest_cat = parent.childTypeCategory()
    if src_cat is not None and dest_cat is not None and src_cat != dest_cat:
        raise ValueError(
            u"category 不匹配: src={0} dest={1}".format(src_cat, dest_cat)
        )
    moved = hou.moveNodesTo([src], parent)
    if not moved:
        raise ValueError(u"moveNodesTo 未返回新节点")
    new_node = moved[0]
    return {
        "src_path": src_path,
        "path": new_node.path(),
    }


# ---------------------------------------------------------------------------
# add-node-parameter-vex-tools：VEX 编译/创建 helper。
# hou 为第一参数注入；vcc wrapper 与持久上下文不在本模块顶层 import。
# ---------------------------------------------------------------------------
# H21.0 实机 vcc --list-context 真实取值（H21/H22 同构）：
#   surface / displace / light / shadow / fog / chop / sop / cop2 / image3d / cvex
_VEX_CONTEXTS = ("surface", "displace", "light", "shadow", "fog",
                 "chop", "sop", "cop2", "image3d", "cvex")


def _resolve_hfs(hou):
    """从 hou.getenv("HFS") 取得绝对 HFS；必须存在且为目录。

    设计：D3 严禁从请求参数取得 HFS；MUST 用 hou.getenv("HFS")。
    不允许 os.environ.get("HFS") 之类的兜底，避免覆盖可信源。
    """
    hfs = hou.getenv("HFS")
    if not hfs:
        raise ValueError(u"HFS 未设置，无法定位 vcc")
    hfs_abs = os.path.abspath(hfs)
    if not os.path.isabs(hfs_abs):
        raise ValueError(u"HFS 不是绝对路径: {0}".format(hfs_abs))
    if not os.path.isdir(hfs_abs):
        raise ValueError(u"HFS 不是目录: {0}".format(hfs_abs))
    return hfs_abs


def _resolve_vcc(hfs_abs):
    """按平台构造 HFS/bin/vcc 路径；对 HFS/bin / executable 双重 realpath 校验。

    返回（绝对 vcc 路径，HFS/bin 绝对路径）。
    """
    hfs_bin = os.path.realpath(os.path.join(hfs_abs, "bin"))
    if not os.path.isdir(hfs_bin):
        raise ValueError(u"HFS/bin 不存在: {0}".format(hfs_bin))
    exe_name = "vcc.exe" if sys.platform.startswith("win") else "vcc"
    vcc_raw = os.path.join(hfs_bin, exe_name)
    vcc_real = os.path.realpath(vcc_raw)
    if not os.path.isfile(vcc_real):
        raise ValueError(u"vcc 不可执行或不是文件: {0}".format(vcc_real))
    # 防止 symlink 逃逸到 HFS/bin 之外
    common = os.path.commonpath([os.path.normcase(vcc_real), os.path.normcase(hfs_bin)])
    if common != os.path.normcase(hfs_bin):
        raise ValueError(
            u"vcc 实际路径 {0} 不在 HFS/bin {1} 内".format(vcc_real, hfs_bin)
        )
    return vcc_real, hfs_bin


# VCC 诊断输出解析：H21+ vcc 实际格式（Windows path 含反斜杠和冒号）
#   "C:\...\file.vfl:2:5: Error 1109: message"
#   "C:\...\file.vfl:2:5-7: Error 1109: message"
# 关键：先搜 ":line:col:" 锚点，再回溯到行首取 file path。
_LINE_COL_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)(?:-(?P<col2>\d+))?:\s*"
    r"(?P<severity>Error|Warning|Note|Info)\s*\d*\s*:?\s*(?P<msg>.*)$",
    re.MULTILINE,
)


def _parse_vex_diagnostics(stdout, stderr, wrapper_line_offset=0):
    """解析 vcc 输出为结构化 diagnostics；wrapper 行偏移在 line 上校正。

    返回 diagnostic 字段：severity / line / column / message。
    """
    diagnostics = []
    for source in (stdout, stderr):
        for match in _LINE_COL_RE.finditer(source or ""):
            line_no = int(match.group("line"))
            col_no = int(match.group("col"))
            corrected = max(0, line_no - wrapper_line_offset)
            diagnostics.append({
                "severity": match.group("severity").lower(),
                "line": corrected,
                "column": col_no,
                "message": match.group("msg").strip(),
            })
    return diagnostics


def validate_vex(hou, code, context="cvex"):
    """用真实 HFS/bin/vcc(.exe) 编译用户 VEX，返回结构化 diagnostics。

    约束（R3 / D3）：
    - 不调用 Python exec/eval/compile；不调用 execute_code / hou.hscript / hou.vexLint /
      hou.text.vexSyntaxCheck；不执行编译产物。
    - 编译用 subprocess argv、shell=False；不接受调用方 compiler flags。
    - 10 秒超时；输出上限 64 KB；finally 清理临时 .vfl 与可能的二进制产物。
    - context 走白名单 cvex / surface / displace / vertex / pixel /
      geometry / point / primitive / detail。
    - HFS 必须绝对目录；vcc 必须解析到 HFS/bin 内。
    """
    if not isinstance(code, str):
        raise ValueError(u"code 必须为字符串")
    if context not in _VEX_CONTEXTS:
        raise ValueError(
            u"context 必须是 {0} 之一, got {1!r}".format(list(_VEX_CONTEXTS), context)
        )

    hfs = _resolve_hfs(hou)
    vcc, _hfs_bin = _resolve_vcc(hfs)

    # wrapper：固定函数体；用户代码放函数体内。wrapper 行偏移在解析时
    # 校正到用户代码自身的行号。
    # H21+ vcc --context 必须看到对应 context 的 entry 函数体
    # （cvex entry() / surface entry() 等），用户代码放进函数体。
    wrapper = (
        "// server-controlled vcc wrapper\n"
        "{CONTEXT} entry(int input=0) {{\n"
        "{USER_CODE}\n"
        "}}\n"
    )
    # 计算 wrapper 头部偏移 = 头部到 {USER_CODE} 行首之间的固定行数
    wrapper_line_offset = wrapper.count("\n", 0, wrapper.find("{USER_CODE}"))

    # 在用户代码前给每行加固定缩进，避免 wrapper 自身破坏代码语义
    indented = "\n".join(
        ("    " + line) if line else line for line in code.split("\n")
    )
    full = (wrapper
            .replace("{CONTEXT}", context)
            .replace("{USER_CODE}", indented))

    src_file = None
    out_file = None
    try:
        fd, src_path = tempfile.mkstemp(prefix="mcp_vex_", suffix=".vfl", text=True)
        src_file = src_path
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(full)
        except Exception:
            os.close(fd) if not os.path.exists(src_path) else None
            raise
        out_path = src_path + ".o"
        out_file = out_path

        try:
            proc = subprocess.run(
                [vcc, "--context", context, src_path, "-o", out_path],
                shell=False,
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "valid": False,
                "context": context,
                "timed_out": True,
                "diagnostics": [
                    {
                        "severity": "error",
                        "line": 0,
                        "column": 0,
                        "message": "vcc 编译超时 (10s)",
                    }
                ],
            }
        except (OSError, ValueError) as exc:
            return {
                "valid": False,
                "context": context,
                "error": "vcc 启动失败: {0}: {1}".format(type(exc).__name__, exc),
                "diagnostics": [],
            }

        stdout = (proc.stdout or b"")[:65536].decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"")[:65536].decode("utf-8", errors="replace")
        diagnostics = _parse_vex_diagnostics(stdout, stderr, wrapper_line_offset)
        valid = proc.returncode == 0 and not any(
            d["severity"] == "error" for d in diagnostics
        )
        return {
            "valid": bool(valid),
            "context": context,
            "diagnostics": diagnostics,
            "returncode": proc.returncode,
        }
    finally:
        for path in (src_file, out_file):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def create_vex_expression(hou, parent_path, code, attrib_class="point", name=None):
    """在 SOP parent 下创建 Attribute Wrangle，并设置 run-over 与 snippet。

    Args:
        hou: hou 模块或 stub。
        parent_path: SOP 父网络路径（如 "/obj/geo1"）。
        code: VEX snippet 文本。
        attrib_class: point / primitive / vertex / detail / number。
        name: 可选新节点名。

    Returns:
        {"path": ..., "name": ..., "attrib_class": ...}
    """
    parent = hou.node(parent_path)
    if parent is None:
        raise ValueError(u"父节点不存在: {0}".format(parent_path))
    cat = parent.childTypeCategory()
    if cat is None or cat.name() != "Sop":
        raise ValueError(
            u"父节点 {0} 不是 SOP 网络 (category={1})".format(
                parent_path, getattr(cat, "name", lambda: str(cat))()
            )
        )
    try:
        node = parent.createNode("attribwrangle", node_name=name)
    except hou.OperationFailed:
        node = None
    if node is None:
        raise ValueError(u"创建 attribwrangle 失败: {0}".format(parent_path))
    snippet = node.parm("snippet")
    if snippet is None:
        try:
            node.destroy()
        except Exception:
            pass
        raise ValueError(u"attribwrangle 无 snippet parm")
    snippet.set(code)
    # H21+ attribwrangle 的 run-over 实际是 ``class`` parm，menu tokens：
    # detail / primitive / point / vertex / number
    parm_map = {
        "point": "point",
        "primitive": "primitive",
        "vertex": "vertex",
        "detail": "detail",
        "number": "number",
    }
    if attrib_class not in parm_map:
        try:
            node.destroy()
        except Exception:
            pass
        raise ValueError(
            u"attrib_class 必须是 {0}, got {1!r}".format(
                list(parm_map.keys()), attrib_class
            )
        )
    cls_parm = node.parm("class")
    if cls_parm is None:
        try:
            node.destroy()
        except Exception:
            pass
        raise ValueError(u"attribwrangle 无 class parm")
    cls_parm.set(parm_map[attrib_class])
    return {
        "path": node.path(),
        "name": node.name(),
        "attrib_class": attrib_class,
    }


def get_wrangle_code(hou, path):
    """读取 Attribute Wrangle SOP 的 snippet 文本。

    Args:
        hou: hou 模块或 stub。
        path: Wrangle 节点路径。

    Returns:
        {"path": ..., "code": ..., "name": ..., "type": ...}
    """
    node = hou.node(path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(path))
    snippet = node.parm("snippet")
    if snippet is None:
        raise ValueError(u"节点 {0} 无 snippet parm（非 wrangle）".format(path))
    try:
        type_name = node.type().name()
    except Exception:
        type_name = "unknown"
    return {
        "path": node.path(),
        "name": node.name(),
        "type": type_name,
        "code": snippet.eval(),
    }


def reorder_inputs(hou, node_path, new_order):
    """重新排列节点的输入顺序。

    Args:
        hou: hou 模块或 stub。
        node_path: 目标节点路径。
        new_order: list of input_index，按新顺序排列（如 [2, 0, 1] 表示把
                   原 input 2 移到 input 0，原 input 0 移到 input 1，原
                   input 1 移到 input 2）。空 list 表示全部断开。

    Returns:
        {"path": ..., "old_order": [...], "new_order": [...], "success": True}
    """
    node = hou.node(node_path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(node_path))

    # 收集当前所有已连接输入 (input_index, output_node, output_index)
    current = []
    for conn in node.inputConnectors():
        current.append((conn.input_index, conn.output_node,
                        getattr(conn, "output_index", 0)))

    old_order = sorted(idx for idx, _, _ in current)

    # 全部断开
    for idx, _, _ in current:
        node.setInput(idx, None)

    # 按 new_order 重连：new_order[i] 是 old input index，要放到新位置 i
    for new_idx, old_idx in enumerate(new_order):
        src_node = None
        src_out = 0
        found = False
        for idx, candidate, out_idx in current:
            if idx == old_idx:
                src_node = candidate
                src_out = out_idx
                found = True
                break
        if not found:
            # old_idx 没在 current 中（可能原本就没连接），跳过
            continue
        node.setInput(new_idx, src_node, src_out)

    return {
        "path": node.path(),
        "old_order": old_order,
        "new_order": list(new_order),
        "success": True,
    }


def layout_children(hou, parent_path, horizontal_spacing=2.0,
                    vertical_spacing=1.5, direction="horizontal"):
    """布局父节点下的子节点。

    通过手动 setPosition 实现，跨 Houdini 版本可移植。
    direction=horizontal 时子节点沿 x 轴排列；vertical 时沿 y 轴排列。

    Args:
        hou: hou 模块或 stub。
        parent_path: 父节点路径。
        horizontal_spacing: 水平间距（Houdini units）。
        vertical_spacing: 垂直间距。
        direction: "horizontal"（默认）或 "vertical"。

    Returns:
        {"parent_path": ..., "children_count": N, "direction": ...,
         "spacing": [h, v]}
    """
    parent = hou.node(parent_path)
    if parent is None:
        raise ValueError(u"父节点不存在: {0}".format(parent_path))

    children = list(parent.children())
    for i, child in enumerate(children):
        if direction == "vertical":
            pos = (0.0, -i * vertical_spacing)
        else:
            pos = (i * horizontal_spacing, 0.0)
        # H21+ SWIG 要求 hou.Vector2 实例，raw tuple 会抛
        # 'argument 2 of type std::vector<double>...' type-check 错。
        # 参考 HoudiniMCPRender.py:124,133 的正确用法。
        child.setPosition(hou.Vector2(pos[0], pos[1]))

    return {
        "parent_path": parent.path(),
        "children_count": len(children),
        "direction": direction,
        "spacing": [horizontal_spacing, vertical_spacing],
    }


def set_node_position(hou, node_path, x, y):
    """设置节点在 network editor 中的位置。

    Args:
        hou: hou 模块或 stub。
        node_path: 节点路径。
        x: x 坐标。
        y: y 坐标。

    Returns:
        {"path": ..., "position": [x, y], "success": True}
    """
    node = hou.node(node_path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(node_path))
    # H21+ SWIG 要求 hou.Vector2 实例，raw tuple 会抛 type-check 错。
    node.setPosition(hou.Vector2(x, y))
    return {
        "path": node.path(),
        "position": [x, y],
        "success": True,
    }


def set_node_color(hou, node_path, r, g, b):
    """设置节点颜色（自动 clamp 到 [0, 1]）。

    Args:
        hou: hou 模块或 stub。
        node_path: 节点路径。
        r, g, b: 颜色分量。

    Returns:
        {"path": ..., "color": [r, g, b], "success": True}
    """
    r = max(0.0, min(1.0, float(r)))
    g = max(0.0, min(1.0, float(g)))
    b = max(0.0, min(1.0, float(b)))
    node = hou.node(node_path)
    if node is None:
        raise ValueError(u"节点不存在: {0}".format(node_path))
    node.setColor(hou.Color((r, g, b)))
    return {
        "path": node.path(),
        "color": [r, g, b],
        "success": True,
    }


def create_network_box(hou, parent_path, name=None, node_paths=None):
    """在父节点下创建 network box，可选包含若干节点。

    Args:
        hou: hou 模块或 stub。
        parent_path: 父节点路径。
        name: 可选，box 名；None 时由 Houdini 自动命名。
        node_paths: 可选，要包含到此 box 的节点路径列表；缺失节点静默跳过。

    Returns:
        {"path": ..., "name": ..., "nodes_in_box": [...]}
    """
    parent = hou.node(parent_path)
    if parent is None:
        raise ValueError(u"父节点不存在: {0}".format(parent_path))
    box = parent.createNetworkBox(name=name) if name else parent.createNetworkBox()
    if node_paths:
        for np in node_paths:
            n = hou.node(np)
            if n is not None:
                box.addNode(n)
    return {
        "path": parent.path(),
        "name": box.name(),
        "nodes_in_box": list(node_paths) if node_paths else [],
    }