"""_error_nodes.py — opera-houdini-mcp find_error_nodes 增强（PR 11）。

模块职责：
- find_error_nodes: 单次扫描 root 节点及其 allSubChildren，收集 errors
  与 warnings 节点，支持 max_warnings / max_errors 截断。
- 默认 include_warnings=True（与 PR 11 brief 对齐；旧实现默认 False）
- 默认 max_warnings=50；max_errors=None 表示不限

约束：
- hou 通过参数注入（测试 mock）
- 不引入类型注解与 f-string
- 不新增 pip 依赖
- 节点不存在抛 ValueError（与既有 _graph_edit / _hscript 风格一致）
- 单次扫描：root + root.allSubChildren()，不递归遍历 children
"""


def find_error_nodes(hou, root_path="/", include_warnings=True,
                     max_warnings=50, max_errors=None):
    """扫描场景中的错误与警告节点。

    Args:
        hou: hou 模块或 stub（参数注入，便于单测）
        root_path: 起始节点路径（默认 "/"）
        include_warnings: 是否包含 warnings（默认 True）
        max_warnings: warnings 上限（防过多；超过返 _warnings_truncated 标记）
        max_errors: errors 上限（None = 不限；超过返 _errors_truncated 标记）

    Returns:
        {
            "error_nodes": [
                {"path": str, "type": str, "errors": [str, ...]},
                ...
            ],
            "warning_nodes": [
                {"path": str, "type": str, "warnings": [str, ...]},
                ...
            ],
            "_warnings_truncated": bool,  # True if warnings 超过 max_warnings
            "_errors_truncated": bool,    # True if errors 超过 max_errors
            "scan_root": str              # root.path()
        }

    Raises:
        ValueError: root_path 不存在
    """
    root = hou.node(root_path)
    if root is None:
        raise ValueError(u"根节点不存在: {0}".format(root_path))

    # PR 11: 单次扫描 — root 自身 + allSubChildren，不递归遍历 .children()
    all_nodes = [root] + list(root.allSubChildren())

    error_nodes = []
    warning_nodes = []
    _warnings_truncated = False
    _errors_truncated = False

    for node in all_nodes:
        try:
            errs = node.errors()
        except Exception:
            errs = []
        errs_clean = [str(e) for e in errs if str(e).strip()]
        if errs_clean:
            if max_errors is not None and len(error_nodes) >= max_errors:
                _errors_truncated = True
            else:
                error_nodes.append({
                    "path": node.path(),
                    "type": node.type().name(),
                    "errors": errs_clean,
                })

        if include_warnings:
            try:
                warns = node.warnings()
            except Exception:
                warns = []
            warns_clean = [str(w) for w in warns if str(w).strip()]
            if warns_clean:
                if len(warning_nodes) >= max_warnings:
                    _warnings_truncated = True
                else:
                    warning_nodes.append({
                        "path": node.path(),
                        "type": node.type().name(),
                        "warnings": warns_clean,
                    })

    return {
        "error_nodes": error_nodes,
        "warning_nodes": warning_nodes,
        "_warnings_truncated": _warnings_truncated,
        "_errors_truncated": _errors_truncated,
        "scan_root": root.path(),
    }