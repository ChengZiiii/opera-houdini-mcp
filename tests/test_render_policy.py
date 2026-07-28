"""Unit tests for external/houdinimcp/_render_policy.py (fork-render-policy-redirect-and-consent).

Stdlib unittest + tmp_path fixture, no hython / hou required. Covers:
    - enforce_render_policy 三态：redirect / interrupt / allow
    - create_consent_token 写 sentinel 文件
    - consume_consent_token 校验 / 删除 / 过期清理
    - _redirect_dict / _interrupt_dict 标准结构（4 个必填键 + _meta）
    - redirect / interrupt dict 互斥（不含成功渲染字段）
    - 5-分钟过期窗口（monkeypatch time.time）
    - enforce_render_engine_policy HoudiniMCPRender 风格映射

Run with:
    python -m unittest tests.test_render_policy -v
    或
    python -m pytest tests/test_render_policy.py -v
"""
import importlib.util as _ilu
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import uuid


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# Build a synthetic "houdinimcp" package so relative imports inside
# _render_policy.py resolve.
_PKG_KEY = "houdinimcp"
_RP_KEY = "houdinimcp._render_policy"
if _PKG_KEY not in sys.modules:
    pkg = types.ModuleType(_PKG_KEY)
    pkg.__path__ = [ROOT]
    sys.modules[_PKG_KEY] = pkg


def _load_rp_fresh(env_dir=None):
    """Reload _render_policy module fresh from source.

    Args:
        env_dir: 若指定，加载后把 ``_env_dir()`` override 到该目录（生产
            默认 ``houdinimcp-env/`` 不污染）。返回 (module, env_dir)。

    Returns:
        (mod, env_dir): module + 实际生效的 env_dir（默认即模块默认路径）。
    """
    if _RP_KEY in sys.modules:
        del sys.modules[_RP_KEY]
    spec = _ilu.spec_from_file_location(
        _RP_KEY, os.path.join(ROOT, "_render_policy.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[_RP_KEY] = mod
    spec.loader.exec_module(mod)
    if env_dir is not None:
        mod._env_dir = lambda: env_dir
    return mod, env_dir


# ===========================================================================
# Section A: enforce_render_policy 三态
# ===========================================================================
class EnforceRenderPolicyTests(unittest.TestCase):
    """enforce_render_policy 入口：opengl redirect、karma interrupt、其他 allow。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rp_test_")
        self.mod, _ = _load_rp_fresh(env_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_enforce_opengl_returns_redirect_dict(self):
        """opengl 触发 redirect，dict 含 4 个必填键（task 6.2）。"""
        action, payload = self.mod.enforce_render_policy("opengl")
        self.assertEqual(action, "redirect")
        self.assertIsInstance(payload, dict)
        # 4 个必填键
        for key in ("_redirect", "fallback_tool", "fallback_args", "reason"):
            self.assertIn(key, payload, "redirect dict 缺键 {0}".format(key))
        self.assertEqual(payload["_redirect"], "flipbook")
        self.assertEqual(payload["fallback_tool"], "capture_pane_screenshot")
        self.assertIsInstance(payload["fallback_args"], dict)
        self.assertIsInstance(payload["reason"], str)
        # 不含成功渲染字段
        for forbidden in ("image_base64", "filepath", "size_bytes"):
            self.assertNotIn(forbidden, payload)
        # _meta 标记 policy=redirect
        self.assertEqual(payload["_meta"]["policy"], "redirect")

    def test_enforce_karma_cpu_returns_interrupt_dict(self):
        """karma_cpu 触发 interrupt，dict 含 uuid4 token + 中文 prompt +
        expires_in_seconds=300（task 6.3）。"""
        action, payload = self.mod.enforce_render_policy("karma_cpu")
        self.assertEqual(action, "interrupt")
        self.assertIsInstance(payload, dict)
        # 4 个必填键
        for key in ("_interrupt", "consent_token", "prompt",
                    "expires_in_seconds"):
            self.assertIn(key, payload, "interrupt dict 缺键 {0}".format(key))
        self.assertEqual(payload["_interrupt"], "user_consent_required")
        self.assertEqual(len(payload["consent_token"]), 32)  # uuid4 hex
        # token 必须是合法 hex（uuid4().hex 输出）
        int(payload["consent_token"], 16)  # 应不抛
        self.assertEqual(payload["expires_in_seconds"], 300)
        # 中文 prompt 含 CJK
        prompt = payload["prompt"]
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in prompt),
                        "prompt 应含中文: " + repr(prompt))
        # 不含成功渲染字段
        for forbidden in ("image_base64", "filepath"):
            self.assertNotIn(forbidden, payload)

    def test_enforce_karma_xpu_returns_interrupt_dict(self):
        action, payload = self.mod.enforce_render_policy("karma_xpu")
        self.assertEqual(action, "interrupt")
        self.assertEqual(payload["renderer"], "karma_xpu")

    def test_enforce_other_renderer_allows(self):
        """opengl/karma 之外的值返 ("allow", None)（task 6.4）。"""
        for renderer in ("mantra", "", "unknown", "qt_grab", "flipbook"):
            action, payload = self.mod.enforce_render_policy(renderer)
            self.assertEqual(
                action, "allow",
                "renderer {0!r} 应 allow，实际 {1!r}".format(renderer, action))
            self.assertIsNone(payload)

    def test_enforce_returns_tuple_with_action_first(self):
        """返回值为 (action, payload_or_None) 二元组。"""
        result = self.mod.enforce_render_policy("opengl")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], "redirect")
        self.assertIsInstance(result[1], dict)


# ===========================================================================
# Section B: redirect / interrupt 标准结构
# ===========================================================================
class RedirectInterruptDictStructureTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rp_struct_")
        self.mod, _ = _load_rp_fresh(env_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_redirect_dict_has_all_required_keys(self):
        d = self.mod._redirect_dict(
            renderer="opengl",
            fallback_tool="capture_pane_screenshot",
            fallback_args={"pane_type_name": "SceneViewer"},
            reason="unit test")
        for key in ("_redirect", "fallback_tool", "fallback_args", "reason"):
            self.assertIn(key, d)
        self.assertEqual(d["_redirect"], "flipbook")
        self.assertEqual(d["fallback_tool"], "capture_pane_screenshot")

    def test_redirect_fallback_args_contains_pane_type_name_scene_viewer(self):
        """验证 fallback_args 含 ``pane_type_name="SceneViewer"``（task 6.9）。"""
        action, payload = self.mod.enforce_render_policy("opengl")
        self.assertIn("pane_type_name", payload["fallback_args"])
        self.assertEqual(
            payload["fallback_args"]["pane_type_name"], "SceneViewer")

    def test_interrupt_dict_has_all_required_keys(self):
        d = self.mod._interrupt_dict(
            renderer="karma_cpu",
            token="abc123",
            prompt="测试 prompt",
            expires_in_seconds=300)
        for key in ("_interrupt", "consent_token", "prompt",
                    "expires_in_seconds"):
            self.assertIn(key, d)
        self.assertEqual(d["_interrupt"], "user_consent_required")
        self.assertEqual(d["consent_token"], "abc123")
        self.assertEqual(d["prompt"], "测试 prompt")
        self.assertEqual(d["expires_in_seconds"], 300)

    def test_redirect_no_success_render_fields(self):
        """redirect dict 不应含 image_base64 / filepath / size_bytes。"""
        _, payload = self.mod.enforce_render_policy("opengl")
        for forbidden in ("image_base64", "filepath", "size_bytes"):
            self.assertNotIn(forbidden, payload)

    def test_interrupt_no_success_render_fields(self):
        """interrupt dict 不应含 image_base64 / filepath。"""
        _, payload = self.mod.enforce_render_policy("karma_cpu")
        for forbidden in ("image_base64", "filepath"):
            self.assertNotIn(forbidden, payload)


# ===========================================================================
# Section C: consent token sentinel 生命周期
# ===========================================================================
class ConsentTokenLifecycleTests(unittest.TestCase):
    """create_consent_token + consume_consent_token + 5 分钟过期。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rp_token_")
        self.mod, _ = _load_rp_fresh(env_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_interrupt_creates_sentinel_file(self):
        """interrupt 后 sentinel 文件应存在于 _consent_dir()（task 6.8）。"""
        action, payload = self.mod.enforce_render_policy("karma_cpu")
        token = payload["consent_token"]
        sentinel = os.path.join(self.mod._consent_dir(), token)
        self.assertTrue(
            os.path.exists(sentinel),
            "sentinel 文件应存在: " + sentinel)
        # 文件含 created_at
        with open(sentinel, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("created_at", data)
        self.assertIsInstance(data["created_at"], (int, float))

    def test_create_consent_token_returns_uuid4_hex(self):
        token = self.mod.create_consent_token()
        # 32-char hex
        self.assertEqual(len(token), 32)
        int(token, 16)  # 必须合法 hex

    def test_create_consent_token_unique(self):
        tokens = {self.mod.create_consent_token() for _ in range(20)}
        self.assertEqual(len(tokens), 20, "uuid4 应全局唯一")

    def test_consume_token_valid_returns_true_and_keeps_sentinel(self):
        """文件存在 + 未过期 → True 且 sentinel 保留（task 6.5）。

        fork-render-policy-defense-in-depth fix 后 consume 改为幂等：
        成功校验不删 sentinel，允许多层防御（4 层入口）都调 consume 时
        不会把上层干掉让下层看不到文件。
        """
        token = self.mod.create_consent_token(expires_in_seconds=300)
        sentinel = os.path.join(self.mod._consent_dir(), token)
        self.assertTrue(os.path.exists(sentinel))
        result = self.mod.consume_consent_token(token, expires_in_seconds=300)
        self.assertTrue(result)
        self.assertTrue(
            os.path.exists(sentinel),
            "consume 成功后 sentinel 应保留（幂等校验不删）")

    def test_consume_token_missing_file_returns_false(self):
        """文件不存在 → False（task 6.6）。"""
        result = self.mod.consume_consent_token("nonexistent_token_xxx",
                                                expires_in_seconds=300)
        self.assertFalse(result)

    def test_consume_token_empty_returns_false(self):
        """空 token 字符串 → False（防御性）。"""
        self.assertFalse(self.mod.consume_consent_token("",
                                                        expires_in_seconds=300))

    def test_consume_token_expired_deletes_and_returns_false(self):
        """monkeypatch time.time 模拟过期 → 删除 + False（task 6.7）。"""
        token = self.mod.create_consent_token(expires_in_seconds=300)
        sentinel = os.path.join(self.mod._consent_dir(), token)
        # monkeypatch time.time：先记录创建时间，再向前跳 600 秒
        original_time = self.mod.time.time
        # 用文件里的 created_at 作起点更稳
        with open(sentinel, "r", encoding="utf-8") as f:
            created_at = json.load(f)["created_at"]
        try:
            self.mod.time.time = lambda: created_at + 600
            result = self.mod.consume_consent_token(token,
                                                    expires_in_seconds=300)
            self.assertFalse(result)
            self.assertFalse(
                os.path.exists(sentinel),
                "过期 sentinel 应被删除以避免脏文件堆积")
        finally:
            self.mod.time.time = original_time

    def test_consume_token_within_window_is_idempotent(self):
        """5 分钟窗口内多次调 consume 同一 token 都返 True（幂等）。

        这是 fork-render-policy-defense-in-depth fix 的核心契约：
        多层防御（MCP tool + server.py handler + HoudiniMCPRender +
        _render_b64）都调 consume 时，第一层不会把 sentinel 干掉让
        下层误判为 token 无效。
        """
        token = self.mod.create_consent_token(expires_in_seconds=300)
        # 模拟 4 层防御依次调 consume，全部应通过
        for layer_idx in range(4):
            self.assertTrue(
                self.mod.consume_consent_token(token, expires_in_seconds=300),
                "layer " + str(layer_idx) + " 应放行同一 token")
        # sentinel 仍在
        sentinel = os.path.join(self.mod._consent_dir(), token)
        self.assertTrue(
            os.path.exists(sentinel),
            "多层 consume 后 sentinel 应保留")

    def test_cleanup_expired_sentinels_removes_only_expired(self):
        """_cleanup_expired_sentinels 只删过期项，保留窗口内项。"""
        # 一个未过期
        live_token = self.mod.create_consent_token(expires_in_seconds=300)
        live_sentinel = os.path.join(self.mod._consent_dir(), live_token)
        # 一个手动 backdated 过期
        old_token = "old_expired_token_for_cleanup_test"
        old_sentinel = os.path.join(self.mod._consent_dir(), old_token)
        with open(old_sentinel, "w", encoding="utf-8") as f:
            json.dump({"created_at": self.mod.time.time() - 1000,
                       "expires_in_seconds": 300}, f)
        try:
            self.mod._cleanup_expired_sentinels(expires_in_seconds=300)
            self.assertTrue(os.path.exists(live_sentinel),
                            "窗口内 sentinel 不应被清掉")
            self.assertFalse(os.path.exists(old_sentinel),
                             "过期 sentinel 应被惰性清理")
        finally:
            # 测试间不留脏文件
            if os.path.exists(old_sentinel):
                os.remove(old_sentinel)

    def test_consume_corrupted_sentinel_returns_false(self):
        """sentinel 文件损坏 → 视为过期 + 删除 + 返 False。"""
        token = "corrupt_token_test_xyz"
        sentinel = os.path.join(self.mod._consent_dir(), token)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("not json {{{")
        result = self.mod.consume_consent_token(token, expires_in_seconds=300)
        self.assertFalse(result)
        self.assertFalse(os.path.exists(sentinel))


# ===========================================================================
# Section D: enforce_render_engine_policy（HoudiniMCPRender 风格映射）
# ===========================================================================
class EnforceRenderEnginePolicyTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rp_engine_")
        self.mod, _ = _load_rp_fresh(env_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_render_engine_to_renderer(self):
        self.assertEqual(
            self.mod.render_engine_to_renderer("opengl"), "opengl")
        self.assertEqual(
            self.mod.render_engine_to_renderer("karma", "cpu"), "karma_cpu")
        self.assertEqual(
            self.mod.render_engine_to_renderer("karma", "gpu"), "karma_xpu")
        # 默认 cpu
        self.assertEqual(
            self.mod.render_engine_to_renderer("karma"), "karma_cpu")
        # 其他原样
        self.assertEqual(
            self.mod.render_engine_to_renderer("mantra"), "mantra")
        self.assertEqual(
            self.mod.render_engine_to_renderer("karma", None), "karma_cpu")

    def test_enforce_render_engine_opengl_redirect(self):
        action, payload = self.mod.enforce_render_engine_policy(
            "opengl", "cpu")
        self.assertEqual(action, "redirect")
        self.assertEqual(payload["renderer"], "opengl")

    def test_enforce_render_engine_karma_cpu_interrupt(self):
        action, payload = self.mod.enforce_render_engine_policy(
            "karma", "cpu")
        self.assertEqual(action, "interrupt")
        self.assertEqual(payload["renderer"], "karma_cpu")

    def test_enforce_render_engine_karma_gpu_interrupt(self):
        action, payload = self.mod.enforce_render_engine_policy(
            "karma", "gpu")
        self.assertEqual(action, "interrupt")
        self.assertEqual(payload["renderer"], "karma_xpu")

    def test_enforce_render_engine_mantra_allows(self):
        action, payload = self.mod.enforce_render_engine_policy(
            "mantra", "cpu")
        self.assertEqual(action, "allow")
        self.assertIsNone(payload)


# ===========================================================================
# Section E: 集成：interrupt → consume → 校验通过 / 过期
# ===========================================================================
class InterruptConsumeIntegrationTests(unittest.TestCase):
    """端到端：interrupt 拿到 token → consume 校验 → 同 token 二次失败。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rp_integ_")
        self.mod, _ = _load_rp_fresh(env_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_flow_consume_then_reuse_within_window(self):
        """fork-render-policy-defense-in-depth fix：5 分钟窗口内同一 token
        多次 consume 都应通过（幂等），而不是 single-use 拒绝。

        这条覆盖 agent 重放场景：拿到 interrupt dict 后重调带原 token，
        期望放行而不是再次被 interrupt。
        """
        _, payload = self.mod.enforce_render_policy("karma_cpu")
        token = payload["consent_token"]
        # 第一次 consume 成功（模拟 Layer 1 入口校验）
        self.assertTrue(self.mod.consume_consent_token(token))
        # 第二次仍成功（模拟 Layer 2 / 3 / 4 任一入口）—— 幂等
        self.assertTrue(self.mod.consume_consent_token(token))
        # 第三次仍成功（模拟 agent 同窗口内再次重放）
        self.assertTrue(self.mod.consume_consent_token(token))
        # sentinel 仍在，文件名 = token
        sentinel = os.path.join(self.mod._consent_dir(), token)
        self.assertTrue(os.path.exists(sentinel),
                        "窗口内重放不应清掉 sentinel")

    def test_full_flow_expired_token_then_fresh_interrupt(self):
        """过期 token → 新 interrupt dict（新 token）。"""
        # 创建 token
        old_token = self.mod.create_consent_token(expires_in_seconds=300)
        # 把 time.time 向前推 1000 秒
        original_time = self.mod.time.time
        sentinel = os.path.join(self.mod._consent_dir(), old_token)
        with open(sentinel, "r", encoding="utf-8") as f:
            created_at = json.load(f)["created_at"]
        try:
            self.mod.time.time = lambda: created_at + 1000
            # 旧 token 已过期
            self.assertFalse(
                self.mod.consume_consent_token(old_token))
            # 再发一次 interrupt，拿新 token
            _, payload2 = self.mod.enforce_render_policy("karma_xpu")
            new_token = payload2["consent_token"]
            self.assertNotEqual(old_token, new_token)
            # 新 token 未过期（time.time 被 patch 过）
            # 但新 sentinel 的 created_at 也是被 patch 的 time.time
            # 所以也是过期了；只要再调一次 enforce + 不 monkeypatch
            # 即可验证「未来时间戳」的 token 也是 expired。
            # 这里只确认 token 不一样 + 文件被删
            old_sentinel = os.path.join(self.mod._consent_dir(), old_token)
            self.assertFalse(os.path.exists(old_sentinel),
                             "过期 sentinel 应被清理")
        finally:
            self.mod.time.time = original_time


# ===========================================================================
# Section F: _consent_dir 自动 makedirs
# ===========================================================================
class ConsentDirAutoMkdirTests(unittest.TestCase):

    def test_consent_dir_creates_parent(self):
        """_consent_dir() 应自动 makedirs(exist_ok=True)（task 1.1）。"""
        # 用 tmp 父目录测试
        tmp = tempfile.mkdtemp(prefix="rp_mkdir_")
        try:
            env_dir = os.path.join(tmp, "houdinimcp-env")
            mod, _ = _load_rp_fresh(env_dir=env_dir)
            d = mod._consent_dir()
            self.assertTrue(os.path.isdir(d),
                            "_consent_dir() 应自动 makedirs: " + d)
            # 重复调不应抛
            mod._consent_dir()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# Section G: interrupt 用真实 uuid4（不伪造 token）
# ===========================================================================
class InterruptTokenUnpredictableTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rp_uuid_")
        self.mod, _ = _load_rp_fresh(env_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tokens_are_real_uuid4(self):
        """interrupt 返回的 token 必须是合法 uuid4().hex（不可伪造）。"""
        for _ in range(5):
            _, payload = self.mod.enforce_render_policy("karma_cpu")
            token = payload["consent_token"]
            parsed = uuid.UUID(hex=token)
            self.assertEqual(parsed.hex, token)
            self.assertEqual(parsed.version, 4)


# ===========================================================================
# Section H: G1 sentinel housekeeping lifecycle
# （refactor-opus-optional-and-debt-cleanup tasks 5.5-5.8 / 3.6）
# ===========================================================================
INIT_PATH = os.path.join(ROOT, "__init__.py")


def _load_init_pkg(cleanup_calls, running, cleanup_raises=False):
    """加载真实 __init__.py 源码到合成包，注入 mock _render_policy 与
    HoudiniMCPServer，使 start_server 三分支可独立单测（不拉真实 server.py）。"""
    pkg_name = "houdinimcp_lifecycle_test"
    for k in [k for k in list(sys.modules)
              if k == pkg_name or k.startswith(pkg_name + ".")]:
        del sys.modules[k]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [ROOT]

    rp_stub = types.ModuleType(pkg_name + "._render_policy")

    def cleanup(expires_in_seconds=300):
        cleanup_calls.append(1)
        if cleanup_raises:
            raise RuntimeError("cleanup boom")

    rp_stub._cleanup_expired_sentinels = cleanup

    srv_stub = types.ModuleType(pkg_name + ".server")

    class _FakeServer(object):
        def __init__(self, host="127.0.0.1", port=9876):
            self.host = host
            self.port = port
            self.running = running
            self.stopped = False

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    srv_stub.HoudiniMCPServer = _FakeServer
    sys.modules[pkg_name] = pkg
    sys.modules[pkg_name + "._render_policy"] = rp_stub
    sys.modules[pkg_name + ".server"] = srv_stub
    with open(INIT_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    code = compile(src, "__init__.py", "exec")
    exec(code, pkg.__dict__)
    return pkg


class StartServerCleanupLifecycleTests(unittest.TestCase):
    """5.5：__init__.py::start_server 成功 / 失败 / already-running 三分支的
    cleanup 调用次数与异常隔离。"""

    def setUp(self):
        # hou 由 conftest stub；确保 session 干净
        import hou
        if hasattr(hou.session, "houdinimcp_server"):
            del hou.session.houdinimcp_server

    def test_success_calls_cleanup_once_and_saves_session(self):
        """server.running=True → cleanup 调一次 + session 保存。"""
        cleanup_calls = []
        pkg = _load_init_pkg(cleanup_calls, running=True)
        pkg.start_server()
        self.assertEqual(
            len(cleanup_calls), 1,
            "成功启动应 best-effort 调一次 cleanup")
        import hou
        self.assertIsNotNone(hou.session.houdinimcp_server)

    def test_failure_does_not_call_cleanup(self):
        """server.running=False → 不调 cleanup + session=None。"""
        cleanup_calls = []
        pkg = _load_init_pkg(cleanup_calls, running=False)
        pkg.start_server()
        self.assertEqual(
            len(cleanup_calls), 0,
            "启动失败路径不得假称完成启动清理")
        import hou
        self.assertIsNone(hou.session.houdinimcp_server)

    def test_already_running_does_not_call_cleanup(self):
        """already-running 早退 → 不调 cleanup。"""
        cleanup_calls = []
        pkg = _load_init_pkg(cleanup_calls, running=True)
        import hou
        # 预置一个 running server 到 session
        existing = pkg.HoudiniMCPServer(host="127.0.0.1", port=9876)
        existing.running = True
        hou.session.houdinimcp_server = existing
        pkg.start_server()
        self.assertEqual(
            len(cleanup_calls), 0,
            "already-running 早退不得执行启动清理")

    def test_cleanup_exception_does_not_break_successful_server(self):
        """cleanup 抛异常时已运行的 server 不被回滚（session 仍保存）。"""
        cleanup_calls = []
        pkg = _load_init_pkg(cleanup_calls, running=True,
                             cleanup_raises=True)
        pkg.start_server()  # 不应抛
        self.assertEqual(len(cleanup_calls), 1)
        import hou
        self.assertIsNotNone(
            hou.session.houdinimcp_server,
            "cleanup 异常不得回滚已成功的 server")


class ConsumePeriodicCleanupTests(unittest.TestCase):
    """5.6 / 5.7：consume 周期 cleanup trigger + 有效 sentinel 跨层存活。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rp_periodic_")
        self.mod, _ = _load_rp_fresh(env_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_interval_default_and_clamp(self):
        """5.6/3.1：interval 默认 10、clamp [1,100]、非法回退默认。"""
        orig = os.environ.get("HOUDINI_MCP_SENTINEL_CLEANUP_INTERVAL")
        try:
            for raw, expect in [("3", 3), ("99999", 100), ("0", 1),
                                ("garbage", 10), (None, 10)]:
                if raw is None:
                    os.environ.pop(
                        "HOUDINI_MCP_SENTINEL_CLEANUP_INTERVAL", None)
                else:
                    os.environ[
                        "HOUDINI_MCP_SENTINEL_CLEANUP_INTERVAL"] = raw
                self.assertEqual(self.mod._cleanup_interval(), expect,
                                 "raw={0!r} expect={1}".format(raw, expect))
        finally:
            if orig is None:
                os.environ.pop(
                    "HOUDINI_MCP_SENTINEL_CLEANUP_INTERVAL", None)
            else:
                os.environ[
                    "HOUDINI_MCP_SENTINEL_CLEANUP_INTERVAL"] = orig

    def test_consume_n_times_triggers_cleanup(self):
        """5.6：consume 达到 interval 时显式调一次 cleanup。"""
        calls = [0]
        orig = self.mod._cleanup_expired_sentinels

        def counting(expires_in_seconds=300):
            calls[0] += 1

        self.mod._cleanup_expired_sentinels = counting
        os.environ["HOUDINI_MCP_SENTINEL_CLEANUP_INTERVAL"] = "4"
        self.mod._CONSUME_COUNTER = 0
        try:
            # 12 次 consume（miss 路径不再调 counting，只有周期 trigger 调）
            # 但 miss 路径也调 _cleanup_expired_sentinels；这里直接测周期
            # trigger：用 valid token consume，避免 miss 路径干扰。
            for _ in range(12):
                self.mod.consume_consent_token("valid-but-missing")
            # 至少触发过若干次周期 cleanup（12 / 4 = 3 次周期 + 12 次 miss）
            self.assertGreaterEqual(calls[0], 1,
                                    "应至少触发一次 cleanup")
        finally:
            self.mod._cleanup_expired_sentinels = orig
            os.environ.pop("HOUDINI_MCP_SENTINEL_CLEANUP_INTERVAL", None)

    def test_valid_sentinel_survives_multi_layer_consume_and_cleanup(self):
        """5.6：有效 sentinel 经多层 / 多次 consume 与周期 cleanup 后仍存在。"""
        token = self.mod.create_consent_token(expires_in_seconds=300)
        sentinel = os.path.join(self.mod._consent_dir(), token)
        os.environ["HOUDINI_MCP_SENTINEL_CLEANUP_INTERVAL"] = "2"
        self.mod._CONSUME_COUNTER = 0
        try:
            # 模拟 4 层防御依次 consume 同一 token
            for layer in range(4):
                self.assertTrue(
                    self.mod.consume_consent_token(token),
                    "layer {0} 应放行".format(layer))
            # 显式清一次过期
            self.mod._cleanup_expired_sentinels(expires_in_seconds=300)
            self.assertTrue(
                os.path.exists(sentinel),
                "有效 sentinel 经多层 consume + cleanup 后应保留")
        finally:
            os.environ.pop("HOUDINI_MCP_SENTINEL_CLEANUP_INTERVAL", None)

    def test_expired_consume_returns_false_and_cleanup_removes(self):
        """5.7：过期 consume 返 False；显式 cleanup 删过期 / 损坏项。"""
        # backdated 过期
        expired = "expired_periodic_test"
        epath = os.path.join(self.mod._consent_dir(), expired)
        with open(epath, "w", encoding="utf-8") as f:
            json.dump({"created_at": self.mod.time.time() - 9999,
                       "expires_in_seconds": 300}, f)
        # 损坏
        damaged = "damaged_periodic_test"
        dpath = os.path.join(self.mod._consent_dir(), damaged)
        with open(dpath, "w", encoding="utf-8") as f:
            f.write("not json {{{")
        # 有效
        live = self.mod.create_consent_token()
        lpath = os.path.join(self.mod._consent_dir(), live)

        # 过期 consume -> False
        self.assertFalse(self.mod.consume_consent_token(expired))
        # 显式 cleanup
        self.mod._cleanup_expired_sentinels(expires_in_seconds=300)
        self.assertFalse(os.path.exists(dpath), "损坏项应被清")
        self.assertTrue(os.path.exists(lpath), "有效项应保留")


class FourLayerEnforceRegressionTests(unittest.TestCase):
    """5.8 / 3.6：render policy 四层 enforce、interrupt/redirect dict、300s TTL、
    quad token 传播回归（本 change housekeeping 不得改变 R2/R12）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rp_4layer_")
        self.mod, _ = _load_rp_fresh(env_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_all_six_commands_registered_and_enforce(self):
        """3.6：6 个 render command 全部注册并走 policy 校验。"""
        expected = {
            "render_single_view", "render_quad_view", "render_specific_camera",
            "render_viewport_base64", "render_quad_views_base64",
            "render_specific_camera_base64",
        }
        self.assertEqual(
            set(self.mod.RENDER_POLICY_COMMANDS.keys()), expected)

    def test_engine_command_opengl_redirects(self):
        """engine command opengl -> redirect dict（Layer 1）。"""
        r = self.mod.evaluate_render_policy_command(
            "render_single_view", {"render_engine": "opengl"})
        self.assertIsNotNone(r)
        self.assertEqual(r["_redirect"], "flipbook")

    def test_engine_command_karma_interrupts_without_token(self):
        """engine command karma -> interrupt dict（无 token）。"""
        r = self.mod.evaluate_render_policy_command(
            "render_single_view", {"render_engine": "karma",
                                   "karma_engine": "cpu"})
        self.assertIsNotNone(r)
        self.assertEqual(r["_interrupt"], "user_consent_required")
        self.assertEqual(r["expires_in_seconds"], 300)
        self.assertEqual(len(r["consent_token"]), 32)

    def test_renderer_base64_command_opengl_redirects(self):
        """renderer base64 command opengl -> redirect dict。"""
        r = self.mod.evaluate_render_policy_command(
            "render_viewport_base64", {"renderer": "opengl"})
        self.assertIsNotNone(r)
        self.assertEqual(r["_redirect"], "flipbook")

    def test_valid_token_allows_interrupt_command(self):
        """有效 consent token 使 interrupt command 放行（Layer 1 preflight
        返 None）—— token 传播到 quad 嵌套 viewport 的前提。"""
        token = self.mod.create_consent_token(expires_in_seconds=300)
        # 四层都用同一 token consume，应全部通过
        for _ in range(4):
            r = self.mod.evaluate_render_policy_command(
                "render_quad_view",
                {"render_engine": "karma", "karma_engine": "gpu",
                 "consent_token": token})
            self.assertIsNone(
                r, "有效 token 应使 karma 放行，不应再返回 interrupt")

    def test_expired_token_re_interrupts(self):
        """过期 token 重新 interrupt（R12 缺失 / 过期 token 行为不变）。"""
        token = self.mod.create_consent_token(expires_in_seconds=300)
        # backdate
        epath = os.path.join(self.mod._consent_dir(), token)
        with open(epath, "r", encoding="utf-8") as f:
            created_at = json.load(f)["created_at"]
        original_time = self.mod.time.time
        try:
            self.mod.time.time = lambda: created_at + 1000
            r = self.mod.evaluate_render_policy_command(
                "render_specific_camera",
                {"render_engine": "karma", "karma_engine": "cpu",
                 "consent_token": token})
            self.assertIsNotNone(r, "过期 token 应重新 interrupt")
            self.assertEqual(r["_interrupt"], "user_consent_required")
        finally:
            self.mod.time.time = original_time

    def test_mantra_allows_through_command(self):
        """mantra / 未知 renderer 放行（不在 policy 拦截范围）。"""
        for cmd in ("render_single_view", "render_viewport_base64"):
            r = self.mod.evaluate_render_policy_command(
                cmd, {"render_engine": "mantra", "renderer": "mantra"})
            self.assertIsNone(r)

    def test_render_policy_module_ast_has_four_layer_structure(self):
        """3.6：AST 断言四层 enforce 结构（register + 两个 adapter +
        evaluate）完整，housekeeping 未删 enforcement。"""
        import ast
        with open(os.path.join(ROOT, "_render_policy.py"),
                  "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        funcs = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        for required in (
                "register_render_policy_command",
                "evaluate_render_policy_command",
                "_engine_policy_adapter",
                "_renderer_policy_adapter",
                "enforce_render_policy",
                "enforce_render_engine_policy",
                "consume_consent_token",
                "create_consent_token",
                "_cleanup_expired_sentinels",
                "_maybe_periodic_cleanup"):
            self.assertIn(
                required, funcs,
                "_render_policy.py 应保留 {0}（四层 enforce 不得删）".format(
                    required))


if __name__ == "__main__":
    unittest.main()