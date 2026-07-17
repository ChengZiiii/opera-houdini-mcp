"""_hscript.py — opera-houdini-mcp HScript 执行包装（PR 8）。

模块职责：
- execute_hscript: 在 Houdini 端执行 HScript 命令字符串，包装 hou.hscript。

约束：
- 不顶层 import hou；hou 通过参数注入（测试 mock）
- 不引入类型注解与 f-string
- 不新增 pip 依赖
- hou.hscript 返回 (stdout, stderr) tuple；对 None / falsy 返回值做空串
  规范化，保证下游 JSON 序列化稳定
- 空字符串 / 纯空白 code 直接抛 ValueError，不静默返空 stdout
"""


def execute_hscript(hou, code):
    """在 Houdini 端执行 HScript 命令。

    Args:
        hou: hou 模块（参数注入）
        code: HScript 命令字符串（如 "cd /obj; ls"）

    Returns:
        {
            "stdout": "...",   # hou.hscript 输出的 stdout（已规范化 None -> ""）
            "stderr": "...",   # 任何 stderr 捕获（HScript 通常输出到 stdout）
            "return_code": 0   # HScript 不返 return code，固定 0
        }

    Raises:
        ValueError: code 为空字符串 / None / 纯空白
    """
    if not code or not code.strip():
        raise ValueError("HScript code 不能为空")

    raw = hou.hscript(code)
    # hou.hscript 在 Houdini 中返回 (stdout, stderr) tuple。容错：返 None
    # 或 (None, None) 时统一规范化为空串。
    if raw is None:
        stdout, stderr = "", ""
    else:
        stdout, stderr = raw
        if stdout is None:
            stdout = ""
        if stderr is None:
            stderr = ""

    return {
        "stdout": stdout,
        "stderr": stderr,
        "return_code": 0,
    }