"""tests/test_render_background.py — perf-mcp-round3 §5 start_render(background=True) 单测。

覆盖（tasks 5.2-5.4，mock-only，零真机 / 零 hython）：
- 缺省回归：``background`` 缺省 / 显式 False 时同步路径响应逐 dict 相等、
  render 同步发生、零子进程派生（硬门槛）。
- background=True 响应形状：``state="launched_background"`` + pid +
  log_path（$TEMP 规范目录、文件名带 pid）+ output_paths（白名单 parm
  尽力读取）+ monitor_hint + elapsed + hip_snapshot_path；mock
  subprocess.Popen 断言 detach 标志（Windows DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP / POSIX start_new_session）、close_fds、
  stdout/stderr 同一日志文件句柄且派生后立即关闭（不持句柄）、命令行含
  hip load + node render + frame_range。
- 未保存 hip：untitled / 空 path / "Untitled" / 无 hipFile 属性均返回
  ``background_requires_saved_hip`` 结构化 error，Popen 未被调用。
- policy 四层不因 background 松动：karma 无 token 仍 interrupt、opengl
  仍 redirect（Layer 3/4 mock 路径 + Layer 1 adapter 参数化 background=
  True 变体见 test_render_policy.py）；karma 有效 token + background=
  True 正常派生（consent 语义不受影响）。
- 命令行注入安全：节点路径 / hip 路径含引号与反斜杠时以 json.dumps
  字面量嵌入，原始未转义串不出现在 -c 代码中。
- 参数校验：background 非 bool 返回结构化 error；opengl 防御性 guard。
- 接线静态断言：server handler 与 bridge tool 的 background 参数贯通、
  Layer 1 preflight 参数不含 background。

约束：
- stdlib unittest + 简易 hou mock + monkeypatch（成对恢复，遵循 round3
  §4.3 stub 泄漏防护方法论）；不引入新依赖。
- 不依赖真实 Houdini / hython；hython 实机验收（5.4 验证行）归编排者。
"""
import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _ensure_pkg():
    """独立合成包（render_bg_test_pkg），避免与其他文件的模块副本互扰。"""
    pkg_name = "render_bg_test_pkg"
    if pkg_name in sys.modules and getattr(
            sys.modules[pkg_name], "__path__", None):
        return sys.modules[pkg_name]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [ROOT]
    sys.modules[pkg_name] = pkg
    return pkg


def _ensure_module(name):
    pkg = _ensure_pkg()
    full = pkg.__name__ + "." + name
    if full in sys.modules:
        del sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(ROOT, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# 预加载 ``from . import`` 依赖链（_render_jobs 还会惰性带出
# _capture_paths，pkg.__path__=[ROOT] 使其加载真实源文件）。
_common = _ensure_module("_common")
_render_policy = _ensure_module("_render_policy")
_render_settings = _ensure_module("_render_settings")
_render_jobs = _ensure_module("_render_jobs")


# ---------------------------------------------------------------------------
# hou mock 基础设施（够用即止：resolve / engine parm / render 记录 /
# hipFile saved-state）
# ---------------------------------------------------------------------------
class _FakeEvalParm(object):
    """eval() 返回固定值（字符串或 list）的最小 parm。"""

    def __init__(self, value):
        self._value = value

    def eval(self):
        return self._value


class _FakeNode(object):
    def __init__(self, type_name, path="/out/n1", parms=None):
        self._type_name = type_name
        self._path = path
        self._parms = parms or {}
        self.render_log = []

    def path(self):
        return self._path

    def name(self):
        return self._path.rsplit("/", 1)[-1]

    def type(self):
        type_ns = types.SimpleNamespace()
        type_ns.name = lambda: self._type_name
        return type_ns

    def parm(self, name):
        return self._parms.get(name)

    def render(self, frame_range=None):
        self.render_log.append(
            list(frame_range) if frame_range else [])
        # background 模式断言 render 不被（同步）调用，无需模拟错误。


class _FakeHou(object):
    def __init__(self, hip_path=None, untitled=False, no_hipfile=False):
        self.nodes_by_path = {}
        self.hip_materialized = False
        if no_hipfile:
            return  # 模拟无 hipFile 属性的极端 hou stub
        if hip_path is None:
            hip_path = os.path.join(tempfile.gettempdir(),
                                    "render_bg_test_scene.hip")
        if hip_path is not None:
            self.hipFile = types.SimpleNamespace(
                path=lambda: hip_path,
                isUntitled=lambda: untitled)
            # §5.4 后 _hip_saved_path 要求磁盘真实存在：非 untitled 且为
            # 绝对路径的假 hip 尽力物化为空文件；物化失败（Windows 非法
            # 文件名等）由测试自行 patch os.path.isfile（见注入用例）
            if (not untitled and isinstance(hip_path, str)
                    and os.path.isabs(hip_path)):
                try:
                    d = os.path.dirname(hip_path)
                    if d:
                        os.makedirs(d, exist_ok=True)
                    with open(hip_path, "w") as fh:
                        fh.write("")
                    self.hip_materialized = True
                except OSError:
                    pass

    def node(self, path):
        return self.nodes_by_path.get(path)


def _build_hou(node_type, parms=None, hip_path=None,
               untitled=False, node_path="/out/n1", no_hipfile=False):
    """构造 fake hou + 一个可分类 ROP 节点；karmarender 自动带 engine parm。"""
    parms = dict(parms or {})
    if node_type == "karmarender" and "engine" not in parms:
        parms["engine"] = _FakeEvalParm("cpu")
    hou = _FakeHou(hip_path=hip_path, untitled=untitled,
                   no_hipfile=no_hipfile)
    node = _FakeNode(node_type, path=node_path, parms=parms)
    hou.nodes_by_path[node_path] = node
    return hou, node


class _FakePopen(object):
    def __init__(self, pid):
        self.pid = pid


class _PopenRecorder(object):
    """记录 args/kwargs 的 Popen 替身；可选抛 OSError 模拟派生失败。"""

    def __init__(self, pid=4242, error=None):
        self.calls = []
        self._pid = pid
        self._error = error

    def __call__(self, args, **kwargs):
        self.calls.append({"args": list(args), "kwargs": dict(kwargs)})
        if self._error is not None:
            raise self._error
        return _FakePopen(self._pid)


class _PopenPatch(unittest.TestCase):
    """成对 patch subprocess.Popen 与日志目录基址（round3 §4.3 防泄漏）。"""

    def setUp(self):
        self.tmp_base = tempfile.mkdtemp(prefix="render_bg_test_")
        self.recorder = _PopenRecorder()
        self._orig_popen = subprocess.Popen
        subprocess.Popen = self.recorder
        self._orig_base = _render_jobs._cpaths.resolve_base_dir
        _render_jobs._cpaths.resolve_base_dir = (
            lambda hou=None, fallback=None: self.tmp_base)

    def tearDown(self):
        subprocess.Popen = self._orig_popen
        _render_jobs._cpaths.resolve_base_dir = self._orig_base
        import shutil
        shutil.rmtree(self.tmp_base, ignore_errors=True)

    def _single_call(self):
        self.assertEqual(len(self.recorder.calls), 1)
        return self.recorder.calls[0]


# ---------------------------------------------------------------------------
# Section 1: 缺省 False 回归（硬门槛）
# ---------------------------------------------------------------------------
class DefaultFalseRegressionTests(_PopenPatch):
    def test_omitted_equals_explicit_false(self):
        """缺省调用与显式 background=False 的响应逐 dict 相等，且都走
        同步 render（缺省路径行为不变的直接断言）。"""
        hou_a, node_a = _build_hou("ifd")
        hou_b, node_b = _build_hou("ifd")
        result_a = _render_jobs.start_render(
            hou_a, "/out/n1", frame_range=[1.0, 3.0])
        result_b = _render_jobs.start_render(
            hou_b, "/out/n1", frame_range=[1.0, 3.0], background=False)
        self.assertEqual(result_a, result_b)
        self.assertEqual(result_a.get("state"), "completed")
        self.assertEqual(node_a.render_log, [[1.0, 3.0]])
        self.assertEqual(node_b.render_log, [[1.0, 3.0]])
        # 同步路径零子进程
        self.assertEqual(self.recorder.calls, [])

    def test_sync_failure_semantics_unchanged(self):
        """同步失败路径仍返 failed + exception（既有契约回归）。"""
        hou, node = _build_hou("ifd")

        def _boom(frame_range=None):
            raise RuntimeError("cook fail")
        node.render = _boom
        result = _render_jobs.start_render(hou, "/out/n1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exception"], "RuntimeError")
        self.assertEqual(self.recorder.calls, [])


# ---------------------------------------------------------------------------
# Section 2: background=True 成功路径（响应形状 + 命令行 + detach 标志）
# ---------------------------------------------------------------------------
class BackgroundLaunchTests(_PopenPatch):
    def test_response_shape_and_command_line(self):
        hou, node = _build_hou(
            "ifd", parms={
                "vm_picture": _FakeEvalParm("$HIP/out/mantra.$F4.exr")})
        result = _render_jobs.start_render(
            hou, "/out/n1", frame_range=[1.0, 3.0], background=True)
        hip_disk = hou.hipFile.path()
        # 响应形状
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["state"], "launched_background")
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(result["hip_snapshot_path"], hip_disk)
        self.assertEqual(result["node_path"], "/out/n1")
        self.assertEqual(result["node_type"], "ifd")
        self.assertEqual(result["renderer"], "mantra")
        self.assertEqual(result["frame_range"], [1.0, 3.0])
        self.assertEqual(result["output_paths"],
                          ["$HIP/out/mantra.$F4.exr"])
        self.assertNotIn("output_paths_note", result)
        self.assertTrue(isinstance(result["monitor_hint"], str)
                        and result["monitor_hint"])
        self.assertTrue(isinstance(result["elapsed"], float))
        # log_path：规范目录 + 文件名带 pid
        log_path = result["log_path"]
        self.assertTrue(log_path.startswith(self.tmp_base),
                        "log_path 应在规范基址下: %r" % log_path)
        self.assertIn("_p4242.log", os.path.basename(log_path))
        # 主线程不触 HOM 渲染；render 零同步调用
        self.assertEqual(node.render_log, [])
        # Popen 恰一次；命令行断言
        call = self._single_call()
        argv = call["args"]
        self.assertEqual(argv[1], "-c")
        self.assertIn("hython",
                      os.path.basename(argv[0]).lower())
        code = argv[2]
        self.assertIn("hou.hipFile.load", code)
        self.assertIn(json.dumps(hip_disk), code)
        self.assertIn(json.dumps("/out/n1"), code)
        self.assertIn("[1.0, 3.0]", code)
        self.assertIn("render(frame_range=tuple(_fr))", code)
        self.assertIn("suppress_save_prompt=True", code)
        # detach 与句柄
        kwargs = call["kwargs"]
        self.assertIs(kwargs["stdout"], kwargs["stderr"])
        self.assertTrue(isinstance(kwargs["stdout"], io.IOBase))
        self.assertTrue(kwargs["stdout"].closed,
                        "派生后父进程侧句柄必须立即关闭（不持句柄）")
        self.assertTrue(kwargs["close_fds"])
        if sys.platform.startswith("win"):
            flags = kwargs.get("creationflags", 0)
            self.assertTrue(flags & 0x00000008,
                            "缺 DETACHED_PROCESS: %r" % flags)
            self.assertTrue(flags & 0x00000200,
                            "缺 CREATE_NEW_PROCESS_GROUP: %r" % flags)
            self.assertNotIn("start_new_session", kwargs)
        else:
            self.assertTrue(kwargs.get("start_new_session"))
            self.assertNotIn("creationflags", kwargs)
        # 日志目录已创建（$TEMP 规范目录 + 日期子目录）
        log_dir = os.path.dirname(log_path)
        self.assertTrue(os.path.isdir(log_dir))
        # 子进程 rotate 用模板与最终路径同目录同前缀
        self.assertTrue(
            code.startswith("import os"),
            "子进程应先 rotate 日志再 import hou")

    def test_empty_frame_range_renders_with_rop_defaults(self):
        hou, node = _build_hou("ifd")
        result = _render_jobs.start_render(
            hou, "/out/n1", background=True)
        self.assertEqual(result["state"], "launched_background")
        self.assertEqual(result["frame_range"], [])
        call = self._single_call()
        code = call["args"][2]
        # 空 tuple -> 子进程走 _n.render()（ROP 自身设置）
        self.assertIn("_fr = []", code)
        self.assertIn("_n.render()", code)

    def test_output_parm_missing_returns_empty_with_note(self):
        hou, node = _build_hou("ifd")  # 无 vm_picture / picture parm
        result = _render_jobs.start_render(
            hou, "/out/n1", background=True)
        self.assertEqual(result["state"], "launched_background")
        self.assertEqual(result["output_paths"], [])
        self.assertTrue(result.get("output_paths_note"))

    def test_karma_valid_token_launches_background(self):
        """consent 语义不受 background 影响：有效 token + background=True
        正常派生子进程。"""
        original_env_dir = _render_policy._env_dir
        tmp = tempfile.mkdtemp(prefix="render_bg_karma_")
        _render_policy._env_dir = lambda: tmp
        try:
            token = _render_policy.create_consent_token()
            hou, node = _build_hou("karmarender")
            result = _render_jobs.start_render(
                hou, "/out/n1", consent_token=token, background=True)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["state"], "launched_background")
            self.assertEqual(result["renderer"], "karma_cpu")
            self.assertEqual(node.render_log, [])
            self._single_call()
        finally:
            _render_policy._env_dir = original_env_dir
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_spawn_failure_structured_error(self):
        self.recorder._error = OSError("no such hython")
        hou, node = _build_hou("ifd")
        result = _render_jobs.start_render(
            hou, "/out/n1", background=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "background_spawn_failed")
        self.assertEqual(node.render_log, [])


# ---------------------------------------------------------------------------
# Section 3: 未保存 hip 前置校验
# ---------------------------------------------------------------------------
class UnsavedHipTests(_PopenPatch):
    def _assert_blocked(self, hou):
        result = _render_jobs.start_render(
            hou, "/out/n1", background=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"],
                          "background_requires_saved_hip")
        self.assertIn("save_scene", result["message"])
        self.assertEqual(self.recorder.calls, [])

    def test_untitled_flag_blocks(self):
        hou, _ = _build_hou("ifd", hip_path="C:/tmp/scene.hip",
                             untitled=True)
        self._assert_blocked(hou)

    def test_untitled_literal_path_blocks(self):
        # isUntitled() 假但 path 为 "Untitled" 字面量（H21 未保存态）
        hou, _ = _build_hou("ifd", hip_path="Untitled", untitled=False)
        self._assert_blocked(hou)

    def test_empty_path_blocks(self):
        hou, _ = _build_hou("ifd", hip_path="", untitled=False)
        self._assert_blocked(hou)

    def test_missing_hipfile_attr_blocks(self):
        hou, _ = _build_hou("ifd", no_hipfile=True)
        self._assert_blocked(hou)

    def test_relative_path_blocks(self):
        hou, _ = _build_hou("ifd", hip_path="relative/scene.hip",
                             untitled=False)
        self._assert_blocked(hou)

    def test_untitled_basename_blocks(self):
        # H21 实测（§5.4）：未保存会话 path() = <cwd>/untitled.hip ——
        # 绝对路径 + 磁盘可能存在（旧 litter），basename 判定必须先行
        hip = os.path.join(tempfile.gettempdir(), "untitled.hip")
        hou, _ = _build_hou("ifd", hip_path=hip)
        self.assertTrue(hou.hip_materialized)  # 文件真实存在仍要拦截
        self._assert_blocked(hou)

    def test_stale_missing_file_blocks(self):
        # 保存后被移动/删除：绝对路径、basename 正常，但磁盘不存在
        hip = os.path.join(tempfile.gettempdir(),
                           "render_bg_stale_scene.hip")
        hou, _ = _build_hou("ifd", hip_path=hip)
        os.remove(hip)  # 物化后删除
        self._assert_blocked(hou)


# ---------------------------------------------------------------------------
# Section 4: policy 四层不因 background 松动
# ---------------------------------------------------------------------------
class BackgroundPolicyGateTests(_PopenPatch):
    def test_karma_background_no_token_still_interrupts(self):
        hou, node = _build_hou("karmarender")
        result = _render_jobs.start_render(
            hou, "/out/n1", background=True)
        self.assertEqual(result.get("_interrupt"), "user_consent_required")
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(node.render_log, [])

    def test_opengl_background_still_redirects(self):
        hou, node = _build_hou("opengl")
        result = _render_jobs.start_render(
            hou, "/out/n1", background=True)
        self.assertIn("_redirect", result)
        self.assertEqual(result["_redirect"], "flipbook")
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(node.render_log, [])

    def test_layer4_direct_call_opengl_redirects(self):
        """Layer 4 直调路径同样 redirect（background 不松动紧前 gate）。"""
        hou, node = _build_hou("opengl")
        result = _render_jobs._render_node_sync(
            hou, node, "opengl", "opengl", (1.0, 2.0), background=True)
        self.assertIn("_redirect", result)
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(node.render_log, [])

    def test_layer1_adapter_ignores_background(self):
        """bridge/server batch 共用的 Layer 1 adapter 只看
        policy_renderer / consent_token：background=True 时 karma 仍
        interrupt、opengl 仍 redirect、mantra 仍放行。"""
        r = _render_policy.evaluate_render_policy_command(
            "start_render", {"policy_renderer": "karma_cpu",
                              "background": True})
        self.assertEqual(r["_interrupt"], "user_consent_required")
        r = _render_policy.evaluate_render_policy_command(
            "start_render", {"policy_renderer": "opengl",
                              "background": True})
        self.assertEqual(r["_redirect"], "flipbook")
        r = _render_policy.evaluate_render_policy_command(
            "start_render", {"policy_renderer": "mantra",
                              "background": True})
        self.assertIsNone(r)


# ---------------------------------------------------------------------------
# Section 5: 命令行注入安全（json.dumps 字面量嵌入）
# ---------------------------------------------------------------------------
class CommandLineInjectionTests(_PopenPatch):
    def test_quoted_paths_stay_json_escaped(self):
        hip = 'C:\\tmp\\x"a.hip'
        node_path = '/out/we"ird\\name'
        hou, node = _build_hou("ifd", hip_path=hip, node_path=node_path)
        # 含引号的路径在 Windows 无法物化为真实文件 → patch isfile 过
        # _hip_saved_path 的磁盘存在检查（成对恢复，round3 §4.3 纪律）
        orig_isfile = os.path.isfile
        os.path.isfile = lambda p: True
        try:
            result = _render_jobs.start_render(
                hou, node_path, background=True)
        finally:
            os.path.isfile = orig_isfile
        self.assertEqual(result["state"], "launched_background")
        call = self._single_call()
        code = call["args"][2]
        # json.dumps 字面量存在；原始未转义串不存在（引号前必有反斜杠）
        self.assertIn(json.dumps(hip), code)
        self.assertIn(json.dumps(node_path), code)
        self.assertNotIn('x"a.hip', code)
        self.assertNotIn('we"ird', code)

    def test_non_ascii_path_ascii_safe(self):
        hip = "C:\\tmp\\场景 保存.hip"
        hou, node = _build_hou("ifd", hip_path=hip)
        result = _render_jobs.start_render(
            hou, "/out/n1", background=True)
        self.assertEqual(result["state"], "launched_background")
        code = self._single_call()["args"][2]
        # ensure_ascii=True：命令行保持纯 ASCII（Windows 代码页安全）
        code.encode("ascii")
        self.assertIn(json.dumps(hip), code)


# ---------------------------------------------------------------------------
# Section 6: 参数校验与防御性 guard
# ---------------------------------------------------------------------------
class GuardTests(_PopenPatch):
    def test_background_non_bool_rejected(self):
        hou, _ = _build_hou("ifd")
        for bad in ("yes", 1, "true", [True]):
            result = _render_jobs.start_render(
                hou, "/out/n1", background=bad)
            self.assertEqual(result["status"], "error",
                              "background=%r 应被拒" % (bad,))
            self.assertEqual(result["field"], "background")
        self.assertEqual(self.recorder.calls, [])

    def test_opengl_background_guard_direct(self):
        """防御性 guard：policy 未来变化使 opengl 实测可达时显式拒绝，
        而不是静默走子进程路径。"""
        hou, node = _build_hou("opengl")
        result = _render_jobs._render_node_background(
            hou, node, "opengl", "opengl", ())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"],
                          "opengl_background_unsupported")
        self.assertEqual(self.recorder.calls, [])

    def test_hython_executable_derivation(self):
        original = sys.executable
        try:
            sys.executable = "C:/hf/bin/hython.exe"
            self.assertEqual(_render_jobs._hython_executable(),
                              "C:/hf/bin/hython.exe")
            sys.executable = "C:/hf/bin/houdini.exe"
            expected = os.path.join(
                "C:/hf/bin",
                "hython.exe" if os.name == "nt" else "hython")
            self.assertEqual(_render_jobs._hython_executable(), expected)
            sys.executable = ""
            self.assertIsNone(_render_jobs._hython_executable())
        finally:
            sys.executable = original

    def test_spawn_failure_closes_log_handle(self):
        self.recorder._error = OSError("boom")
        hou, _ = _build_hou("ifd")
        result = _render_jobs.start_render(
            hou, "/out/n1", background=True)
        self.assertEqual(result["error_code"], "background_spawn_failed")
        kwargs = self.recorder.calls[0]["kwargs"]
        self.assertTrue(kwargs["stdout"].closed,
                        "派生失败时日志句柄也必须关闭")


# ---------------------------------------------------------------------------
# Section 7: 接线静态断言（server handler / bridge tool 参数贯通）
# ---------------------------------------------------------------------------
def _parse_source(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


class WiringStaticTests(unittest.TestCase):
    def test_server_handler_has_background_passthrough(self):
        tree = _parse_source(os.path.join(ROOT, "server.py"))
        func = _find_function(tree, "handle_start_render")
        self.assertIsNotNone(func)
        arg_names = [arg.arg for arg in func.args.args]
        self.assertIn("background", arg_names)
        # 缺省 False
        defaults = func.args.defaults
        self.assertIn(False, [d.value for d in defaults
                              if isinstance(d, ast.Constant)])
        # 转发到 _rjobs.start_render 的调用带 background 关键字
        calls = [n for n in ast.walk(func)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "start_render"]
        self.assertTrue(calls, "handle_start_render 应调用 _rjobs.start_render")
        keywords = [kw.arg for kw in calls[-1].keywords]
        self.assertIn("background", keywords)

    def test_bridge_tool_signature_and_wire_param(self):
        tree = _parse_source(os.path.join(ROOT, "houdini_mcp_server.py"))
        func = _find_function(tree, "start_render")
        self.assertIsNotNone(func)
        arg_names = [arg.arg for arg in func.args.args]
        self.assertIn("background", arg_names)
        # 注解为 bool 且缺省 False（Annotation 为 ast.Name 'bool'）
        for arg in func.args.args:
            if arg.arg == "background":
                self.assertIsNotNone(arg.annotation)
        source_target = None
        for node in ast.walk(func):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "params"
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value == "background"):
                source_target = node
        self.assertIsNotNone(
            source_target, "bridge 应在 background 真值时写入 params")
        # Layer 1 preflight 字典只含 policy_renderer / consent_token
        preflight_dicts = [
            n for n in ast.walk(func)
            if isinstance(n, ast.Dict)
            and {isinstance(k, ast.Constant) and k.value for k in n.keys}
            == {"policy_renderer", "consent_token"}]
        self.assertTrue(preflight_dicts,
                        "Layer 1 preflight 参数不得包含 background")
        for dict_node in preflight_dicts:
            keys = {k.value for k in dict_node.keys
                    if isinstance(k, ast.Constant)}
            self.assertNotIn("background", keys)

    def test_render_jobs_signatures_have_background_default_false(self):
        tree = _parse_source(os.path.join(ROOT, "_render_jobs.py"))
        for func_name in ("start_render", "_render_node_sync"):
            func = _find_function(tree, func_name)
            self.assertIsNotNone(func, func_name)
            arg_names = [arg.arg for arg in func.args.args]
            self.assertIn("background", arg_names)
            default_map = dict(zip(
                arg_names[len(arg_names) - len(func.args.defaults):],
                func.args.defaults))
            self.assertIsInstance(default_map.get("background"),
                                  ast.Constant)
            self.assertIs(default_map["background"].value, False)


if __name__ == "__main__":
    unittest.main()
