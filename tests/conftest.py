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
import os
import sys
import types

import pytest


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
    # Task 8（conftest 揭露性增强 / opera-houdinimcp-h21-compat-audit）：
    # 移除 isUntitled lambda —— H21 已无 hou.hipFile.isUntitled()，改用 isNewFile()。
    # 保留 path / basename / save / load / clear（H21 仍存在）。
    # 注意：本 stub 不提供 isNewFile —— 各测试按需在自己 mock 里设置；
    # 这样 fork 代码若误调 hou.hipFile.isUntitled() 会抛 AttributeError 让单测 FAIL。
    hou.hipFile = types.SimpleNamespace(
        path=lambda: "",
        basename=lambda: "untitled",
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


# ---------------------------------------------------------------------------
# fix-mcp-test-suite-repair：泄漏防护（autouse）
# ---------------------------------------------------------------------------
# 用户生产 MCP 常驻 127.0.0.1:9876。任何测试若经桥的默认 _houdini_port 发
# 命令（如 load_scene），会打到真机 Houdini（曾导致弹保存对话框）。本
# fixture 在每个测试开始前把已加载 bridge 模块的 _houdini_port 改指死端口
# （连接立即被拒），使全量套件默认零真机访问。
#
# - 死端口可用 HOUDINI_MCP_TEST_PORT 覆盖；
# - 显式 opt-in HOUDINI_MCP_TEST_ALLOW_LIVE=1 时完全放行（真机 e2e /
#   手动 smoke 用）；
# - 测试中途懒加载的桥（如 test_lessons_tools 的缓存加载）由各自文件的
#   加载守卫兜底（见 test_lessons_tools._apply_leak_guard）。
_DEAD_PORT = 1


@pytest.fixture(autouse=True)
def _isolate_from_live_houdini(monkeypatch):
    """默认隔离：禁止套件触达用户生产 MCP（默认 127.0.0.1:9876）。"""
    if os.environ.get("HOUDINI_MCP_TEST_ALLOW_LIVE") == "1":
        yield
        return
    dead_port = int(os.environ.get("HOUDINI_MCP_TEST_PORT", str(_DEAD_PORT)))
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        port = getattr(module, "_houdini_port", None)
        # 以 int 型 _houdini_port 属性识别 bridge 模块（唯一连接向量：
        # get_houdini_connection 是 _houdini_port 的唯一消费方）
        if isinstance(port, int) and port != dead_port:
            monkeypatch.setattr(module, "_houdini_port", dead_port)
    yield