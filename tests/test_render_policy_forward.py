"""回归测试 Bug B：bridge -> Houdini-side consent_token wire 透传。

背景（fork-render-policy-redirect-and-consent 565308d 遗漏环）：
    6 个 render MCP tool 在 bridge 层做完 policy 校验后，调 send_command /
    _houdini_call 透传给 Houdini-side 时根本没带 consent_token。导致
    Houdini-side server.py 3 个 base64 handler 收到 renderer=karma_cpu 但
    无 token -> 转手调 houdinimcp._render_b64.render_* 时 Layer 4 永远
    interrupt + 新 token，agent 永远拿不到真 render 结果。1d06dfb 把
    consume 改成幂等后该 wire 截断更明显。

修复覆盖（本文件验证两个修复面）：
    1. bridge 6 个 render tool 的 send_command / _houdini_call params dict
       必须含 consent_token（真 import bridge + mock get_houdini_connection
       抓 wire params）。
    2. server.py 3 个 base64 handler 收到 consent_token 后必须透传给
       rb64.render_*（synthetic package 加载 server + mock rb64 抓 kwargs）。

不动 _render_policy.py / test_render_policy.py（本文件独立验证 wire 层）。

Run with:
    python -m pytest tests/test_render_policy_forward.py -v
"""
import importlib.util as _ilu
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # external/houdinimcp


# ---------------------------------------------------------------------------
# 环境准备：stub hou / numpy（conftest.py 已做时幂等；本文件独立跑也兼容）
# ---------------------------------------------------------------------------
def _ensure_hou_stub():
    if "hou" in sys.modules and hasattr(sys.modules["hou"], "__file__"):
        return  # 真实 hou（hython 内）
    if "hou" not in sys.modules:
        hou = types.ModuleType("hou")
        hou.session = types.SimpleNamespace()
        hou.hipFile = types.SimpleNamespace(
            path=lambda: "", basename=lambda: "x",
            save=lambda **kw: None, load=lambda **kw: None,
            clear=lambda **kw: None)
        sys.modules["hou"] = hou


def _ensure_numpy_stub():
    if "numpy" in sys.modules and hasattr(sys.modules["numpy"], "__file__"):
        return
    if "numpy" not in sys.modules:
        np = types.ModuleType("numpy")
        np.array = lambda *a, **kw: None
        np.zeros = lambda *a, **kw: None
        np.linalg = types.SimpleNamespace()
        sys.modules["numpy"] = np


_ensure_hou_stub()
_ensure_numpy_stub()


# bridge: flat import（bridge 顶层已 sys.path.insert sibling 目录，但本测试
# 从 tests/ 跑时 external/houdinimcp 不一定在 sys.path，这里显式补上）
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import houdini_mcp_server as bridge  # noqa: E402


# ---------------------------------------------------------------------------
# 加载 server.py（synthetic houdinimcp package + sibling stub）
#
# 不依赖 conftest 的 __init__ 加载顺序；与 test_render_b64.py 的 synthetic
# package 风格一致。server.py 顶部 import 14 个 sibling，但 base64 handler
# 路径只用 cmn.apply_response_cap + rb64.render_*，其余 sibling 仅被 import
# 不在 handler 调用链上，stub 成空 ModuleType 即可。
# ---------------------------------------------------------------------------
def _load_server_module():
    if "houdinimcp.server" in sys.modules:
        return sys.modules["houdinimcp.server"]
    if "houdinimcp" not in sys.modules:
        pkg = types.ModuleType("houdinimcp")
        pkg.__path__ = [ROOT]
        sys.modules["houdinimcp"] = pkg
    _SIBLINGS = (
        "_common", "_scene", "_error_nodes", "_discovery", "_materials",
        "_hscript", "_graph_edit", "_render_policy", "_node_info",
        "_geo_summary", "_pane_capture", "_capture_paths", "_render_b64",
        "_help",
    )
    for name in _SIBLINGS:
        full = "houdinimcp." + name
        if full not in sys.modules:
            sys.modules[full] = types.ModuleType(full)
    # cmn.apply_response_cap：handler 路径需要它做 passthrough。仅当
    # _common 是本文件创建的空 stub（无该属性）时才补；若 test_render_b64
    # 等先跑了真实 _common（已含 apply_response_cap），绝不覆盖以免污染。
    _cmn_mod = sys.modules["houdinimcp._common"]
    if not hasattr(_cmn_mod, "apply_response_cap"):
        _cmn_mod.apply_response_cap = lambda *a, **kw: (a[0] if a else None)
    spec = _ilu.spec_from_file_location(
        "houdinimcp.server", os.path.join(ROOT, "server.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules["houdinimcp.server"] = mod
    spec.loader.exec_module(mod)
    return mod


server_mod = _load_server_module()


# ===========================================================================
# Section A：bridge wire 透传（6 个 render tool）
# ===========================================================================
class _FakeConn(object):
    """记录 send_command 的 (cmd, params) 调用序列。"""

    def __init__(self):
        self.calls = []

    def send_command(self, cmd, params):
        self.calls.append((cmd, dict(params)))
        return {"status": "success", "result": {"_test": True}}


class BridgeConsentTokenForwardTests(unittest.TestCase):
    """Bug B 回归：6 个 bridge render tool 必须把 consent_token 透传到 wire。

    mock get_houdini_connection 返回 FakeConn 抓 send_command 的 params dict；
    mock policy 入口返回 None（放行），聚焦验证 wire 透传而非 policy 语义
    （policy 语义由 test_render_policy.py 覆盖）。
    """

    def setUp(self):
        self._orig_conn = bridge.get_houdini_connection
        self._orig_eng = bridge._apply_render_policy_to_engine
        self._orig_ren = bridge._apply_render_policy_to_renderer
        self.conn = _FakeConn()
        bridge.get_houdini_connection = lambda: self.conn
        bridge._apply_render_policy_to_engine = lambda *a, **kw: None
        bridge._apply_render_policy_to_renderer = lambda *a, **kw: None

    def tearDown(self):
        bridge.get_houdini_connection = self._orig_conn
        bridge._apply_render_policy_to_engine = self._orig_eng
        bridge._apply_render_policy_to_renderer = self._orig_ren

    def _last_params(self):
        self.assertTrue(self.conn.calls, "send_command 未被调用")
        return self.conn.calls[-1][1]

    def test_render_single_view_forwards_token(self):
        bridge.render_single_view(ctx=None, render_engine="karma_cpu",
                                   consent_token="T-single")
        self.assertEqual(self._last_params().get("consent_token"), "T-single")

    def test_render_quad_views_forwards_token(self):
        bridge.render_quad_views(ctx=None, render_engine="karma_cpu",
                                  consent_token="T-quad")
        self.assertEqual(self._last_params().get("consent_token"), "T-quad")

    def test_render_specific_camera_forwards_token(self):
        bridge.render_specific_camera(ctx=None, camera_path="/obj/cam1",
                                       render_engine="karma_cpu",
                                       consent_token="T-cam")
        self.assertEqual(self._last_params().get("consent_token"), "T-cam")

    def test_render_viewport_base64_forwards_token(self):
        bridge.render_viewport_base64(ctx=None, renderer="karma_cpu",
                                       consent_token="T-vp")
        self.assertEqual(self._last_params().get("consent_token"), "T-vp")

    def test_render_quad_views_base64_forwards_token(self):
        bridge.render_quad_views_base64(ctx=None, renderer="karma_cpu",
                                         consent_token="T-quadb64")
        self.assertEqual(self._last_params().get("consent_token"), "T-quadb64")

    def test_render_specific_camera_base64_forwards_token(self):
        bridge.render_specific_camera_base64(
            ctx=None, camera_path="/obj/cam1", renderer="karma_cpu",
            consent_token="T-camb64")
        self.assertEqual(self._last_params().get("consent_token"), "T-camb64")

    def test_none_token_still_present_on_wire(self):
        """不带 token 时 wire 仍应带 consent_token=None。

        Houdini-side handler 签名已声明 consent_token=None 默认值，多传
        None 不报错；且保证 wire 字段集合稳定，便于 Layer 2/3/4 统一判定。
        """
        bridge.render_viewport_base64(ctx=None, renderer="karma_cpu")
        self.assertIn("consent_token", self._last_params())
        self.assertIsNone(self._last_params()["consent_token"])

    def test_all_six_tools_send_cmd_names(self):
        """附带验证 6 个 tool 的 wire command 名不回归。"""
        bridge.render_single_view(ctx=None, render_engine="karma_cpu",
                                   consent_token="a")
        bridge.render_quad_views(ctx=None, render_engine="karma_cpu",
                                  consent_token="b")
        bridge.render_specific_camera(ctx=None, camera_path="/obj/c",
                                       render_engine="karma_cpu",
                                       consent_token="c")
        bridge.render_viewport_base64(ctx=None, renderer="karma_cpu",
                                       consent_token="d")
        bridge.render_quad_views_base64(ctx=None, renderer="karma_cpu",
                                         consent_token="e")
        bridge.render_specific_camera_base64(
            ctx=None, camera_path="/obj/f", renderer="karma_cpu",
            consent_token="f")
        cmds = [c[0] for c in self.conn.calls]
        self.assertEqual(cmds, [
            "render_single_view", "render_quad_view", "render_specific_camera",
            "render_viewport_base64", "render_quad_views_base64",
            "render_specific_camera_base64",
        ])


# ===========================================================================
# Section B：server.py base64 handler 透传给 rb64（mock rb64 抓 kwargs）
# ===========================================================================
class ServerBase64ConsentTokenForwardTests(unittest.TestCase):
    """Bug B 回归：server.py 3 个 base64 handler 必须把 consent_token
    透传给 rb64.render_*（Layer 4 兜底校验需要真 token）。

    直接调用 HoudiniMCPServer 的 unbound method（Python 3 即普通函数），
    传任意 self（handler 体只用 rb64 / cmn 模块全局，不依赖 self 状态）。
    mock rb64 的 3 个 render 函数抓 kwargs。
    """

    def setUp(self):
        self.rb64 = server_mod.rb64
        self._captured = {}
        self._orig = {}
        for fn_name in ("render_viewport", "render_quad_views",
                        "render_specific_camera_base64"):
            self._orig[fn_name] = getattr(self.rb64, fn_name, None)
            self._install_fake(fn_name)

    def _install_fake(self, fn_name):
        captured = self._captured

        def _fake(*args, **kwargs):
            captured[fn_name] = dict(kwargs)
            return {"_warning": "stub", "renderer": kwargs.get("renderer")}
        setattr(self.rb64, fn_name, _fake)

    def tearDown(self):
        for fn_name, orig in self._orig.items():
            if orig is None:
                try:
                    delattr(self.rb64, fn_name)
                except AttributeError:
                    pass
            else:
                setattr(self.rb64, fn_name, orig)

    def test_render_viewport_base64_forwards_to_rb64(self):
        server_mod.HoudiniMCPServer.render_viewport_base64(
            object(), renderer="karma_cpu", consent_token="S-vp")
        self.assertEqual(
            self._captured["render_viewport"].get("consent_token"), "S-vp")

    def test_render_quad_views_base64_forwards_to_rb64(self):
        server_mod.HoudiniMCPServer.render_quad_views_base64(
            object(), renderer="karma_cpu", consent_token="S-quad")
        self.assertEqual(
            self._captured["render_quad_views"].get("consent_token"),
            "S-quad")

    def test_render_specific_camera_base64_forwards_to_rb64(self):
        server_mod.HoudiniMCPServer.render_specific_camera_base64(
            object(), camera_path="/obj/cam1", renderer="karma_cpu",
            consent_token="S-cam")
        self.assertEqual(
            self._captured["render_specific_camera_base64"]
            .get("consent_token"), "S-cam")

    def test_base64_handler_signatures_accept_consent_token(self):
        """3 个 base64 handler 签名必须含 consent_token 形参（否则 bridge
        透传过来会因 handler(**params) 报 TypeError）。"""
        import inspect
        for fn_name in ("render_viewport_base64", "render_quad_views_base64",
                        "render_specific_camera_base64"):
            fn = getattr(server_mod.HoudiniMCPServer, fn_name)
            sig = inspect.signature(fn)
            self.assertIn("consent_token", sig.parameters,
                          "%s 缺少 consent_token 形参" % fn_name)


if __name__ == "__main__":
    unittest.main()
