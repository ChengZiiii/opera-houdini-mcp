"""Unit tests for Bug 2: Pane type alias map (opera-houdinimcp-h21-compat).

测试 capture_pane_screenshot 用 hou.paneTabType 解析 pane_type_name 时对
历史 pane 名（如 ParameterEditor / ChannelEditorPane 等）做 alias 兜底，
同时保证真实名优先（D5 决策）。

依据 SideFX hou.paneTabType H22 文档：
- ParameterEditor 已移除，真实名是 Parm / ParmSpreadsheet / DetailsView。
- ChannelEditorPane 已 deprecated（hou.ChannelEditorPane.html 显式标注
  "Deprecated: Use ChannelEditor"），真实名是 ChannelEditor。
- hou.ui.paneTabOfType(type, index=0) 参数类型是 hou.paneTabType enum。
- 历史名仅在 H21+ 用户安装老 plugin 时可能仍存在于 hou.paneTabType 上
  （D4 alias 不覆盖真实名字）。

复用 test_pane_capture.py 的 fake 框架（_FakePaneTabType / _FakePaneTab /
_FakeUI / _FakeHou / _FakeQWidget / _FakeQtFixture / _load_pane_capture_fresh
等），通过 importlib.util 私下加载避免 pytest 把 test_pane_capture 的模块
级 `pcp` baseline 与本文件的测试相互污染。
"""
import importlib.util as _ilu
import os
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# 私下加载 test_pane_capture.py 取 fake 框架（避免重复定义）。
# 使用独立 sys.modules key 与 pytest 缓存隔离，本文件只读其 fake 类。
# ---------------------------------------------------------------------------
_TPC_PATH = os.path.join(HERE, "test_pane_capture.py")
_TPC_KEY = "_test_pane_capture_fakes_for_aliases"
_spec = _ilu.spec_from_file_location(_TPC_KEY, _TPC_PATH)
_tpc = _ilu.module_from_spec(_spec)
sys.modules[_TPC_KEY] = _tpc
_spec.loader.exec_module(_tpc)


_FakePaneTabType = _tpc._FakePaneTabType
_FakePaneTab = _tpc._FakePaneTab
_FakeUI = _tpc._FakeUI
_FakeHou = _tpc._FakeHou
_FakeQWidget = _tpc._FakeQWidget
_FakeQApplication = _tpc._FakeQApplication
_FakeQtFixture = _tpc._FakeQtFixture
_load_pane_capture_fresh = _tpc._load_pane_capture_fresh
_restore_pcp_module = _tpc._restore_pcp_module
_PCP_KEY = _tpc._PCP_KEY


# ---------------------------------------------------------------------------
# 扩展 _FakePaneTabType：H21+ 真实名 + 历史名（仅 test 5 需要历史名）。
# _ExtendedPaneTabType 不含历史名，对应纯 H21+ 环境；_LegacyPluginPaneTabType
# 用于模拟装了老 plugin 时历史名仍可能存在于 hou.paneTabType（H20 + 老 plugin）。
# ---------------------------------------------------------------------------
class _ExtendedPaneTabType(_FakePaneTabType):
    """H21+ 真实 pane 名（SideFX hou.paneTabType H22 文档枚举）。

    注意：故意不含 ParameterEditor / ParameterPane / ChannelEditorPane /
    ChannelViewerPane / ChannelListPane 等历史名，模拟 H21+ hou.paneTabType
    上没有这些属性。
    """
    Parm = "Parm"
    ParmSpreadsheet = "ParmSpreadsheet"
    DetailsView = "DetailsView"
    # ChannelEditor 在父类 _FakePaneTabType 中已定义 → 不重复
    ChannelList = "ChannelList"
    ChannelViewer = "ChannelViewer"


class _LegacyPluginPaneTabType(_ExtendedPaneTabType):
    """装了老 plugin / H20 兼容模式的 hou.paneTabType — 历史名仍存在。

    用于验证 D5：alias 不覆盖真实名字。
    """
    ParameterEditor = "ParameterEditor"
    ChannelEditorPane = "ChannelEditorPane"
    ParameterPane = "ParameterPane"
    ChannelViewerPane = "ChannelViewerPane"
    ChannelListPane = "ChannelListPane"


class _SpyingUI(_FakeUI):
    """Spy 包装 _FakeUI，记录 hou.ui.paneTabOfType 被调用的所有参数，
    便于断言 alias 链中哪几个名字被试过（real-name 路径 vs alias fallback）。"""
    def __init__(self, pane_tabs_by_type=None, desktops=None):
        super().__init__(pane_tabs_by_type=pane_tabs_by_type, desktops=desktops)
        self.calls = []

    def paneTabOfType(self, pane_type):
        self.calls.append(pane_type)
        return super().paneTabOfType(pane_type)


def _make_hou(pane_tab_type_cls=_ExtendedPaneTabType,
              pane_tabs_by_type=None, desktops=None):
    """构造一个 hou stub，使用给定 _ExtendedPaneTabType 子类 + _SpyingUI。

    pane_tabs_by_type: dict 映射 hou.paneTabType.X -> _FakePaneTab 或 None；
    返回 (hou, ui)，调用方可读 ui.calls 验证解析路径。
    """
    ui = _SpyingUI(pane_tabs_by_type=pane_tabs_by_type or {},
                   desktops=desktops or [])
    hou = _FakeHou(pane_tabs_by_type={}, desktops=[])
    hou.paneTabType = pane_tab_type_cls()
    hou.ui = ui
    return hou, ui


def _run_with_fake_pyside6(body):
    """注入 fake PySide6 后跑 body(pcp_module)，退出时还原 sys.modules。

    与 test_pane_capture.py 的 _run_with_fake_pyside6 同形；多包一步
    _FakeQApplication._instance 重置，规避先前测试留下的 singleton 污染。
    """
    orig_pcp = sys.modules.get(_PCP_KEY)
    fx = _FakeQtFixture("PySide6")
    fx.__enter__()
    try:
        pcp2 = _load_pane_capture_fresh()
        _FakeQApplication._instance = None
        body(pcp2)
    finally:
        fx.__exit__(None, None, None)
        _restore_pcp_module(orig_pcp)


# ===========================================================================
# 测试 1-6
# ===========================================================================
class PaneCaptureAliasTests(unittest.TestCase):
    """Bug 2：capture_pane_screenshot 解析 hou.paneTabType 的 alias 兜底逻辑。

    6 个测试覆盖：
    1. 成功 alias 兜底（ParameterEditor → ParmSpreadsheet）
    2. fallback chain（ParmSpreadsheet + Parm 失败 → DetailsView 命中）
    3. Channel 旧名（ChannelEditorPane → ChannelEditor）
    4. 所有 alias 失败 → ValueError 含 attempted list
    5. 真实名字优先（D5：装了老 plugin 时不走 alias）
    6. 完全未知名 → ValueError 含 attempted 列表（即便 alias dict 无该 key）
    """

    def test_parameter_editor_alias_to_parmspreadsheet(self):
        """H21+ hou.paneTabType 已无 ParameterEditor → 真实名查失败 → 走
        alias list → 试 ParmSpreadsheet 命中 → 截图成功；返回 dict 的
        pane_type 仍是用户原始传入的名字 'ParameterEditor'。"""
        widget = _FakeQWidget(w=800, h=600)
        ps_pane = _FakePaneTab(widget=widget)
        hou, ui = _make_hou(pane_tabs_by_type={
            _ExtendedPaneTabType.ParmSpreadsheet: ps_pane,
        })

        def body(pcp):
            self.assertEqual(pcp._QT_BACKEND, "PySide6")
            result = pcp.capture_pane_screenshot(hou, "ParameterEditor")
            # spy 仅记录 ParmSpreadsheet 一次（命中即 break）
            self.assertEqual(ui.calls,
                [_ExtendedPaneTabType.ParmSpreadsheet])
            # 用户原始名字保留在返回 dict（与修复前一致）
            self.assertEqual(result["pane_type"], "ParameterEditor")
            self.assertGreater(result["size_bytes"], 0)
            self.assertEqual(result["_qt_backend"], "PySide6")
        _run_with_fake_pyside6(body)

    def test_parameter_editor_alias_fallback_chain(self):
        """alias chain：ParmSpreadsheet / Parm 都返 None → DetailsView 命中
        → 走 DetailsView pane 实例。spy.calls 必须按 alias dict 顺序记录。"""
        widget_dv = _FakeQWidget(w=640, h=480)
        dv_pane = _FakePaneTab(widget=widget_dv)
        # 只放 DetailsView；ParmSpreadsheet / Parm 查 _by_type 返 None
        hou, ui = _make_hou(pane_tabs_by_type={
            _ExtendedPaneTabType.DetailsView: dv_pane,
        })

        def body(pcp):
            result = pcp.capture_pane_screenshot(hou, "ParameterEditor")
            # 三个 alias 都查询过；前两个 None 后一个命中
            self.assertEqual(ui.calls, [
                _ExtendedPaneTabType.ParmSpreadsheet,
                _ExtendedPaneTabType.Parm,
                _ExtendedPaneTabType.DetailsView,
            ])
            # 走 DetailsView pane 的 widget.grab()（D5 验证：不去试 ParmSpread.）
            self.assertEqual(widget_dv.grab_calls, [True])
            self.assertEqual(result["pane_type"], "ParameterEditor")
            self.assertEqual(result["width"], 640)
            self.assertEqual(result["height"], 480)
        _run_with_fake_pyside6(body)

    def test_channel_editor_pane_alias(self):
        """历史名 ChannelEditorPane → alias ['ChannelEditor'] → HouChEditor
        pane 命中。"""
        widget = _FakeQWidget(w=1024, h=768)
        ce_pane = _FakePaneTab(widget=widget)
        hou, ui = _make_hou(pane_tabs_by_type={
            _ExtendedPaneTabType.ChannelEditor: ce_pane,
        })

        def body(pcp):
            result = pcp.capture_pane_screenshot(hou, "ChannelEditorPane")
            self.assertEqual(ui.calls,
                [_ExtendedPaneTabType.ChannelEditor])
            self.assertEqual(result["pane_type"], "ChannelEditorPane")
            self.assertEqual(result["width"], 1024)
            self.assertEqual(result["height"], 768)
        _run_with_fake_pyside6(body)

    def test_all_aliases_fail_raises_valueerror_with_list(self):
        """所有 alias 查询都返 None → ValueError，message 含 attempted list
        ['ParameterEditor', 'ParmSpreadsheet', 'Parm', 'DetailsView'] 全部
        名字，便于排查未映射的旧名。"""
        hou, ui = _make_hou(pane_tabs_by_type={})

        def body(pcp):
            with self.assertRaises(ValueError) as cm:
                pcp.capture_pane_screenshot(hou, "ParameterEditor")
            msg = str(cm.exception)
            for name in ("ParameterEditor", "ParmSpreadsheet",
                         "Parm", "DetailsView"):
                self.assertIn(name, msg)
            # spy 记录所有 alias 都被尝试
            self.assertEqual(ui.calls, [
                _ExtendedPaneTabType.ParmSpreadsheet,
                _ExtendedPaneTabType.Parm,
                _ExtendedPaneTabType.DetailsView,
            ])
        _run_with_fake_pyside6(body)

    def test_real_name_takes_precedence_over_alias(self):
        """装了老 plugin → hou.paneTabType.ParameterEditor 仍存在 → 解析走
        真实名路径，不进入 alias block。spy.calls 必须 == [ParameterEditor]
        单次调用，且使用的 widget 是 ParameterEditor pane 的 widget，
        非 ParmSpreadsheet pane 的 widget（D5 决策）。"""
        widget_pe = _FakeQWidget(w=400, h=300)
        widget_ps = _FakeQWidget(w=800, h=600)
        pe_pane = _FakePaneTab(widget=widget_pe)
        ps_pane = _FakePaneTab(widget=widget_ps)
        hou, ui = _make_hou(
            pane_tab_type_cls=_LegacyPluginPaneTabType,
            pane_tabs_by_type={
                _LegacyPluginPaneTabType.ParameterEditor: pe_pane,
                _LegacyPluginPaneTabType.ParmSpreadsheet: ps_pane,
            })

        def body(pcp):
            result = pcp.capture_pane_screenshot(hou, "ParameterEditor")
            # spy 仅记录一次 real-name 调用，不进入 alias 链
            self.assertEqual(ui.calls,
                [_LegacyPluginPaneTabType.ParameterEditor])
            # 真实名路径用 pe_pane 的 widget（400x300），绝不是 ps_pane
            self.assertEqual(widget_pe.grab_calls, [True])
            self.assertEqual(widget_ps.grab_calls, [])
            # 返回 dict 仍以用户原始传入名字为准
            self.assertEqual(result["pane_type"], "ParameterEditor")
            self.assertEqual(result["width"], 400)
            self.assertEqual(result["height"], 300)
        _run_with_fake_pyside6(body)

    def test_unknown_pane_type_raises_with_attempted_list(self):
        """完全未知名（如 'UnknownXYZ'）→ real-name 查 None → alias dict 无
        该 key → alias list 为空 → for 循环 0 次 → ValueError，attempted
        列表至少含原名 'UnknownXYZ'。message 形如：
          未找到 pane 类型: UnknownXYZ（已尝试别名: ['UnknownXYZ']）"""
        hou, ui = _make_hou(pane_tabs_by_type={})

        def body(pcp):
            with self.assertRaises(ValueError) as cm:
                pcp.capture_pane_screenshot(hou, "UnknownXYZ")
            msg = str(cm.exception)
            self.assertIn("UnknownXYZ", msg)
            # attempted 列表至少含原名（alias dict 无该 key → list 长度 = 1）
            self.assertIn("['UnknownXYZ']", msg)
            # 无 alias 查询被发出（alias list 为空）
            self.assertEqual(ui.calls, [])
        _run_with_fake_pyside6(body)


if __name__ == "__main__":
    unittest.main()
