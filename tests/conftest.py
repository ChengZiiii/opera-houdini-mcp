"""conftest.py — opera-houdini-mcp 测试基础设施。

pytest 收集 tests/ 时会 import `houdinimcp/__init__.py`，其首行
`import hou` 在非 Houdini 环境（embedded Python）失败，导致所有测试
ERROR 而非收集。本 conftest 在 pytest 收集前 stub hou（与真实 hou 同
API surface 的 SimpleNamespace），使 __init__.py 可被 import，单测
按需在自己测试内部重新加载 fresh 模块。

历史：本仓库所有 test_*.py 在 pytest 单独跑某个文件时都会因 hou 缺失
ERROR；只有 `pytest tests/` 批量跑时，pytest 内部的 sys.path / 收集
逻辑容忍部分失败但仍能执行其他测试。本 conftest 一劳永逸解决。
"""
import sys
import types


def _stub_hou():
    """Install a minimal hou stub so `import hou` succeeds in unit tests.

    Returns the stub module so test code can also patch attributes on it.
    Only installed if `hou` is not already importable (i.e. not running
    inside hython).
    """
    if "hou" in sys.modules and hasattr(sys.modules["hou"], "__file__"):
        return sys.modules["hou"]  # real hou already present

    hou = types.ModuleType("hou")

    # hou.session 用于 shelf scripts；测试环境无 session，stub 空对象
    hou.session = types.SimpleNamespace()

    # hou.hipFile：scene-level state；测试极少真用，SimpleNamespace 足够
    hou.hipFile = types.SimpleNamespace(
        path=lambda: "",
        basename=lambda: "untitled",
        isUntitled=lambda: True,
        save=lambda **kw: None,
        load=lambda **kw: None,
        clear=lambda **kw: None,
    )

    # hou.paneTabType：测试只比对属性名，SimpleNamespace 即可
    hou.paneTabType = types.SimpleNamespace(
        NetworkEditor=object(),
        SceneViewer=object(),
        Compositor=object(),
        ChannelEditor=object(),
        ParameterEditor=object(),
        PythonPanel=object(),
    )

    # hou.ui / hou.expandString / hou.frame / hou.node / hou.session 由
    # 各测试 stub 替换；这里仅占位
    hou.ui = types.SimpleNamespace()
    hou.expandString = lambda s: ""
    hou.frame = lambda: 1
    hou.node = lambda p: None

    # hou.FlipbookSettings：Bug B 测试需要
    class _StubFlipbookSettings(object):
        pass

    hou.FlipbookSettings = _StubFlipbookSettings

    sys.modules["hou"] = hou
    return hou


def _stub_numpy():
    """Install a minimal numpy stub. HoudiniMCPRender (used by _render_b64)
    imports numpy at module top; embedded test env doesn't have numpy.
    Stub with SimpleNamespace so `import numpy as np` succeeds; tests that
    actually call numpy APIs should provide their own mock."""
    if "numpy" in sys.modules and hasattr(sys.modules["numpy"], "__file__"):
        return sys.modules["numpy"]
    np = types.ModuleType("numpy")
    np.array = lambda *a, **kw: None
    np.zeros = lambda *a, **kw: None
    np.linalg = types.SimpleNamespace()
    sys.modules["numpy"] = np
    return np


_stub_hou()
_stub_numpy()