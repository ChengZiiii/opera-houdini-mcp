"""Unit tests for feat-mcp-round2-hardening §2 渲染链路资源治理.

覆盖四个验收面（spec ADDED Requirement "渲染临时节点与产物治理"）：

A. HoudiniMCPRender rig finally 清理（成功 / 失败两路径、多视图单 rig
   建一次清一次、_cleanup_warning 报告、resolve_actual_backend 映射）
B. server.py：启动孤儿清扫（精确已知名单）、render_path 缺省回退
   $TEMP/houdini_mcp/<日期>/ 规范目录、响应附 requested_renderer /
   actual_backend / renderer（兼容）与 _cleanup_warning
C. _render_b64：karma 请求走截图回退时 actual_backend 如实标注
   （flipbook / qscreen_fallback，MUST NOT 仅标 karma）、quad 单 rig
   复用 + finally 清理
D. bridge AST 探针：三个渲染工具 render_path 默认 None（MUST NOT 默认
   发送 C:/temp/）+ docstring 如实描述

Stdlib unittest, no hython required. hou 通过可销毁节点注册表 stub。
Run with:
    python -m unittest tests.test_render_resource_governance -v
"""
import ast
import importlib.util as _ilu
import io
import os
import shutil
import sys
import tempfile
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 渲染临时节点精确名单（与 HoudiniMCPRender 常量一致）
RIG_PATHS = ("/obj/MCP_CAM_CENTER", "/obj/MCP_CAMERA")
OUT_NAMES = ("MCP_OGL_RENDER", "MCP_CPU_KARMA", "MCP_GPU_KARMA", "MCP_MANTRA")


# ===========================================================================
# Section A: HoudiniMCPRender rig finally 清理
# ===========================================================================
class _FakeParm(object):
    def __init__(self, value=0):
        self._value = value

    def set(self, v):
        self._value = v

    def eval(self):
        return self._value


class _FakeParmTuple(object):
    def __init__(self, values=None):
        self._values = list(values or [0, 0, 0])

    def set(self, v):
        self._values = list(v)

    def eval(self):
        return list(self._values)


class _FakeVector2(object):
    def __init__(self, *args):
        self.args = args


class _FakeVector3(list):
    def __init__(self, values=None):
        list.__init__(self, values or [0, 0, 0])


class _FakeRenderNode(object):
    """可销毁的节点 stub；parm/parmTuple 自动补全（mimic HOM 宽容读取）。"""

    def __init__(self, path, registry, kind="generic"):
        self._path = path
        self._registry = registry
        self._kind = kind
        self._destroyed = False
        self._parms = {}
        self._parm_tuples = {}
        self.render_calls = []
        self.render_raises = None

    def path(self):
        return self._path

    def name(self):
        return self._path.rsplit("/", 1)[-1]

    def type(self):
        kind = self._kind

        def _name():
            return kind
        return types.SimpleNamespace(name=_name)

    def parm(self, name):
        if name not in self._parms:
            self._parms[name] = _FakeParm(0)
        return self._parms[name]

    def parmTuple(self, name):
        if name not in self._parm_tuples:
            self._parm_tuples[name] = _FakeParmTuple()
        return self._parm_tuples[name]

    def setFirstInput(self, other):
        pass

    def setPosition(self, position):
        pass

    def parent(self):
        return None

    def render(self):
        self.render_calls.append(self._path)
        if self.render_raises is not None:
            raise self.render_raises

    def destroy(self):
        if self._destroyed:
            raise RuntimeError("already destroyed: " + self._path)
        self._destroyed = True
        self._registry.pop(self._path, None)


class _FakeParentNode(_FakeRenderNode):
    def createNode(self, node_type, node_name=None):
        name = node_name or node_type
        path = self._path + "/" + name
        suffix = 1
        while path in self._registry:
            path = self._path + "/" + name + str(suffix)
            suffix += 1
        child = _FakeRenderNode(path, self._registry, kind=node_type)
        self._registry[path] = child
        return child


class _FakeHouModule(object):
    """渲染流程用 hou stub：/obj、/out 两个父容器 + 可销毁注册表。"""

    Vector2 = _FakeVector2
    Vector3 = _FakeVector3

    def __init__(self):
        self._registry = {}
        self._registry["/obj"] = _FakeParentNode("/obj", self._registry)
        self._registry["/out"] = _FakeParentNode("/out", self._registry)

    def node(self, path):
        node = self._registry.get(path)
        if node is not None and node._destroyed:
            return None
        return node


# 独立 synthetic package，避免与 test_render_b64 的 key 互相踩踏
_PKG_KEY = "render_governance_pkg"
_RLIB_KEY = _PKG_KEY + ".HoudiniMCPRender"
_RPOLICY_KEY = _PKG_KEY + "._render_policy"


def _load_render_lib_fresh(hou_stub):
    """加载真实 HoudiniMCPRender + fresh _render_policy，绑定注入的 hou stub。"""
    for key in (_PKG_KEY, _RLIB_KEY, _RPOLICY_KEY):
        sys.modules.pop(key, None)
    package = types.ModuleType(_PKG_KEY)
    package.__path__ = [ROOT]
    sys.modules[_PKG_KEY] = package

    saved_hou = sys.modules.get("hou")
    sys.modules["hou"] = hou_stub
    try:
        spec = _ilu.spec_from_file_location(
            _RPOLICY_KEY, os.path.join(ROOT, "_render_policy.py"))
        policy = _ilu.module_from_spec(spec)
        sys.modules[_RPOLICY_KEY] = policy
        spec.loader.exec_module(policy)

        spec = _ilu.spec_from_file_location(
            _RLIB_KEY, os.path.join(ROOT, "HoudiniMCPRender.py"))
        mod = _ilu.module_from_spec(spec)
        sys.modules[_RLIB_KEY] = mod
        spec.loader.exec_module(mod)
    finally:
        # HoudiniMCPRender 已在 exec 时绑定本测试的 stub；恢复全局
        # sys.modules["hou"]（conftest stub），避免污染其他测试的加载。
        if saved_hou is not None:
            sys.modules["hou"] = saved_hou
    return mod


def _allow_engine_policy(mod):
    """放行 engine policy（opengl redirect / karma consent 均绕过）。"""
    mod._rp.enforce_render_engine_policy = (
        lambda render_engine, karma_engine=None: ("allow", None))


class _BboxMixin(object):
    """公共 stub：跳过 numpy 依赖的几何扫描，直接返回固定 bbox。"""

    def _stub_scene(self, mod):
        mod.find_displayed_geometry = lambda: [object()]
        mod.calculate_bounding_box = lambda nodes: {
            "min": [-1.0, -1.0, -1.0],
            "max": [1.0, 1.0, 1.0],
            "center": [0.0, 0.0, 0.0],
        }


class RenderLibCleanupTests(_BboxMixin, unittest.TestCase):
    """§2.1：渲染流程 finally 段清理（成功 / 失败两路径）。"""

    def setUp(self):
        self.hou = _FakeHouModule()
        self.mod = _load_render_lib_fresh(self.hou)
        _allow_engine_policy(self.mod)
        self._stub_scene(self.mod)
        self.tmp_dir = tempfile.mkdtemp(prefix="mcp_render_gov_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _registry_paths(self):
        return set(self.hou._registry.keys())

    def test_constants_list_exact_rig_names(self):
        """临时节点精确名单覆盖 OBJ rig + /out ROP 全集。"""
        self.assertEqual(
            self.mod.RENDER_TEMP_OBJ_PATHS, RIG_PATHS)
        for name in OUT_NAMES:
            self.assertIn(name, self.mod.RENDER_TEMP_OUT_NODE_NAMES)
        self.assertEqual(
            len(self.mod.RENDER_TEMP_ALL_PATHS),
            len(RIG_PATHS) + len(OUT_NAMES))

    def test_single_view_success_cleans_all_temp_nodes(self):
        """成功路径：rig（2）+ ROP（1）在 finally 全部销毁。"""
        filepath = self.mod.render_single_view(
            rotation=(0, 90, 0), render_path=self.tmp_dir,
            render_engine="karma", karma_engine="cpu")
        self.assertIsNotNone(filepath)
        self.assertTrue(os.path.exists(filepath) or filepath.endswith(".jpg"))
        for path in RIG_PATHS:
            self.assertNotIn(path, self._registry_paths(),
                             "rig {0} 未被 finally 清理".format(path))
        self.assertNotIn("/out/MCP_CPU_KARMA", self._registry_paths())
        # 正常清理 → 无失败报告
        report = []
        self.mod.render_single_view(
            rotation=(0, 0, 0), render_path=self.tmp_dir,
            render_engine="karma", karma_engine="cpu",
            cleanup_report=report)
        self.assertEqual(report, [])

    def test_single_view_render_failure_still_cleans(self):
        """失败路径：render() 抛异常时 finally 仍清空临时节点，异常向上传播。"""
        original_setup = self.mod.setup_render_node

        def poisoning_setup(**kwargs):
            node, filepath = original_setup(**kwargs)
            node.render_raises = RuntimeError("render boom")
            return node, filepath

        self.mod.setup_render_node = poisoning_setup
        try:
            with self.assertRaises(RuntimeError):
                self.mod.render_single_view(
                    render_path=self.tmp_dir,
                    render_engine="karma", karma_engine="cpu")
        finally:
            self.mod.setup_render_node = original_setup
        for path in RIG_PATHS:
            self.assertNotIn(path, self._registry_paths())
        self.assertNotIn("/out/MCP_CPU_KARMA", self._registry_paths())

    def test_single_view_destroy_failure_reported_not_fatal(self):
        """清理失败不吞渲染结果：失败描述进 cleanup_report + 返回值保留。"""
        original_setup = self.mod.setup_render_node

        def poisoning_setup(**kwargs):
            node, filepath = original_setup(**kwargs)
            # 让 rig 相机的 destroy 抛错（模拟 HOM 拒绝销毁）
            cam = self.hou.node("/obj/MCP_CAMERA")
            original_destroy = cam.destroy

            def refusing_destroy():
                raise RuntimeError("destroy refused")
            cam.destroy = refusing_destroy
            # 保底恢复（正常 finally 清理会调用一次；之后节点仍在
            # registry，不影响断言）
            object.__setattr__(cam, "_orig_destroy", original_destroy)
            return node, filepath

        self.mod.setup_render_node = poisoning_setup
        report = []
        try:
            filepath = self.mod.render_single_view(
                render_path=self.tmp_dir,
                render_engine="karma", karma_engine="cpu",
                cleanup_report=report)
        finally:
            self.mod.setup_render_node = original_setup
        self.assertIsNotNone(filepath, "清理失败不得吞掉渲染结果")
        self.assertEqual(len(report), 1)
        self.assertIn("/obj/MCP_CAMERA", report[0])
        self.assertIn("destroy refused", report[0])
        # 其余节点（rig null + ROP）仍被清理
        self.assertNotIn("/obj/MCP_CAM_CENTER", self._registry_paths())
        self.assertNotIn("/out/MCP_CPU_KARMA", self._registry_paths())

    def test_quad_view_single_rig_built_once_cleaned_once(self):
        """多视图：rig 建一次（原实现 4 次 destroy+create）、清一次。"""
        original_rig = self.mod.setup_camera_rig
        rig_calls = []

        def counting_rig(center, orthographic=False):
            rig_calls.append(tuple(center))
            return original_rig(center, orthographic)
        self.mod.setup_camera_rig = counting_rig
        try:
            filepaths = self.mod.render_quad_view(
                orthographic=True, render_path=self.tmp_dir,
                render_engine="opengl")
        finally:
            self.mod.setup_camera_rig = original_rig
        self.assertEqual(len(filepaths), 4)
        self.assertEqual(len(rig_calls), 1,
                         "单 rig 复用：setup_camera_rig 必须只调一次")
        for path in RIG_PATHS:
            self.assertNotIn(path, self._registry_paths())
        self.assertNotIn("/out/MCP_OGL_RENDER", self._registry_paths())

    def test_quad_view_rotation_reset_between_views(self):
        """单 rig 复用下逐视图先归零旋转（rotate 是叠加式）。"""
        resets = []
        original_reset = self.mod.reset_camera_center
        self.mod.reset_camera_center = lambda null: resets.append(True)
        try:
            self.mod.render_quad_view(
                orthographic=True, render_path=self.tmp_dir,
                render_engine="opengl")
        finally:
            self.mod.reset_camera_center = original_reset
        self.assertEqual(len(resets), 4,
                         "每视图迭代各归零一次（首视图冗余但幂等）")

    def test_quad_view_render_failure_still_cleans(self):
        """多视图失败路径：视图渲染抛异常 → finally 仍清理全部节点。"""
        original_setup = self.mod.setup_render_node

        def poisoning_setup(**kwargs):
            node, filepath = original_setup(**kwargs)
            node.render_raises = RuntimeError("quad boom")
            return node, filepath

        self.mod.setup_render_node = poisoning_setup
        try:
            with self.assertRaises(RuntimeError):
                self.mod.render_quad_view(
                    render_path=self.tmp_dir, render_engine="opengl")
        finally:
            self.mod.setup_render_node = original_setup
        for path in RIG_PATHS:
            self.assertNotIn(path, self._registry_paths())
        self.assertNotIn("/out/MCP_OGL_RENDER", self._registry_paths())

    def test_specific_camera_user_camera_not_destroyed(self):
        """指定相机流程只清理自建 ROP，用户相机保留。"""
        cam = _FakeRenderNode("/obj/user_cam", self.hou._registry, kind="cam")
        self.hou._registry["/obj/user_cam"] = cam
        try:
            filepath = self.mod.render_specific_camera(
                "/obj/user_cam", render_path=self.tmp_dir,
                render_engine="opengl")
            self.assertIsNotNone(filepath)
            self.assertFalse(cam._destroyed, "用户相机不得被清理")
            self.assertNotIn("/out/MCP_OGL_RENDER", self._registry_paths())
        finally:
            self.hou._registry.pop("/obj/user_cam", None)

    def test_cleanup_temp_nodes_missing_nodes_are_success(self):
        """目标已不存在（hou.node 返 None）视为清理成功，不进失败列表。"""
        failures = self.mod.cleanup_temp_nodes(
            ("/obj/MCP_CAM_CENTER", "/obj/never_existed"))
        self.assertEqual(failures, [])

    def test_resolve_actual_backend_mapping(self):
        """actual_backend 分支映射表（§2c）。"""
        resolve = self.mod.resolve_actual_backend
        self.assertEqual(resolve("opengl"), "opengl_rop")
        self.assertEqual(resolve("OpenGL"), "opengl_rop")
        self.assertEqual(resolve(None), "opengl_rop")
        self.assertEqual(resolve("karma"), "husk")
        self.assertEqual(resolve("Karma"), "husk")
        self.assertEqual(resolve("mantra"), "mantra_rop")


# ===========================================================================
# Section B: server.py 孤儿清扫 / path 回退 / 响应语义
# ===========================================================================
_SRV_PKG_KEY = "render_governance_srv_pkg"
_SRV_KEY = _SRV_PKG_KEY + ".server"


def _load_server_module():
    """加载真实 server.py（模式同 tests/test_execute_code_safety._load_server_module）。

    差异：_capture_paths 与 HoudiniMCPRender 也从真实文件加载
    （孤儿扫打名单 / resolve_actual_backend / cap.default_capture_path
    均为被测对象）；其余渲染无关兄弟模块仍 stub，缺省项经
    package.__path__ 真实加载（hou 由 conftest stub 提供）。

    防半成品缓存（feat-mcp-round2-hardening §2 实测踩坑）：若 sys.modules
    里同名模块缺少本文件被测的新符号（如 _attach_render_semantics），
    视为此前 exec 中途失败留下的残缺模块，整体弹出后重载。
    """
    module_name = _SRV_KEY
    if module_name in sys.modules:
        cached = sys.modules[module_name]
        if hasattr(cached, "_attach_render_semantics"):
            return cached
        # 残缺模块（上游测试污染第三方模块导致 exec 半途而废）→ 清场重载
        for key in [k for k in sys.modules if k == _SRV_PKG_KEY
                    or k.startswith(_SRV_PKG_KEY + ".")]:
            del sys.modules[key]

    package = types.ModuleType(_SRV_PKG_KEY)
    package.__path__ = [ROOT]
    sys.modules[_SRV_PKG_KEY] = package

    for name in (
            "_scene", "_error_nodes", "_discovery", "_materials",
            "_hscript", "_graph_edit", "_node_info", "_geo_summary",
            "_pane_capture", "_render_b64", "_help"):
        sys.modules[_SRV_PKG_KEY + "." + name] = types.ModuleType(
            _SRV_PKG_KEY + "." + name)

    for name in ("_common", "_render_policy", "_capture_paths",
                 "HoudiniMCPRender"):
        full_name = _SRV_PKG_KEY + "." + name
        path = os.path.join(ROOT, name + ".py")
        spec = _ilu.spec_from_file_location(full_name, path)
        module = _ilu.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)

    spec = _ilu.spec_from_file_location(_SRV_KEY, os.path.join(ROOT, "server.py"))
    module = _ilu.module_from_spec(spec)
    sys.modules[_SRV_KEY] = module
    spec.loader.exec_module(module)
    return module


class _StubCamNode(object):
    """_process_rendered_image 用：parm() 返 None 即跳过分辨率读取。"""

    def parm(self, name):
        return None


class _FakeSweepHou(object):
    """孤儿清扫用 hou stub：注册表 + 可注入 destroy 失败。"""

    def __init__(self, paths=(), failing=()):
        self._registry = {}
        self._failing = set(failing)
        self.destroyed = []
        for path in paths:
            self._registry[path] = self._make_node(path)

    def node(self, path):
        return self._registry.get(path)

    def _make_node(self, path):
        outer = self

        def _do_destroy(_path=path):
            # 默认参数绑定 _path（函数体内求值），避免闭包晚绑定
            if _path in outer._failing:
                raise RuntimeError("destroy refused")
            outer._registry.pop(_path, None)
            outer.destroyed.append(_path)

        return types.SimpleNamespace(
            path=lambda _p=path: _p, destroy=_do_destroy)


class OrphanSweepTests(unittest.TestCase):
    """§2.2：server 启动孤儿清扫（精确已知名字）。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server_module()

    def test_sweep_destroys_exact_known_names_only(self):
        paths = ("/obj/MCP_CAM_CENTER", "/obj/MCP_CAMERA",
                 "/out/MCP_CPU_KARMA", "/out/MCP_MANTRA")
        fake_hou = _FakeSweepHou()
        for path in paths:
            fake_hou._registry[path] = fake_hou._make_node(path)
        # 无关节点必须幸免
        fake_hou._registry["/obj/my_camera"] = fake_hou._make_node(
            "/obj/my_camera")
        fake_hou._registry["/out/MCP_NOT_IN_LIST"] = fake_hou._make_node(
            "/out/MCP_NOT_IN_LIST")

        result = self.srv._sweep_orphan_render_nodes(hou_module=fake_hou)

        self.assertEqual(sorted(result["found"]), sorted(paths))
        self.assertEqual(result["destroyed"], 4)
        self.assertEqual(result["failed"], [])
        self.assertEqual(sorted(fake_hou.destroyed), sorted(paths))
        # 无关节点幸存
        self.assertIn("/obj/my_camera", fake_hou._registry)
        self.assertIn("/out/MCP_NOT_IN_LIST", fake_hou._registry)

    def test_sweep_empty_scene_noop(self):
        fake_hou = _FakeSweepHou()
        result = self.srv._sweep_orphan_render_nodes(hou_module=fake_hou)
        self.assertEqual(result["found"], [])
        self.assertEqual(result["destroyed"], 0)
        self.assertEqual(result["failed"], [])

    def test_sweep_collects_destroy_failures(self):
        paths = ("/obj/MCP_CAM_CENTER", "/out/MCP_OGL_RENDER")
        fake_hou = _FakeSweepHou(paths=paths, failing=("/out/MCP_OGL_RENDER",))
        result = self.srv._sweep_orphan_render_nodes(hou_module=fake_hou)
        self.assertEqual(result["destroyed"], 1)
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("/out/MCP_OGL_RENDER", result["failed"][0])
        self.assertIn("destroy refused", result["failed"][0])

    def test_sweep_covers_full_registry_names(self):
        """名单必须与 HoudiniMCPRender 常量一致（rig + ROP 全集）。"""
        expected = set(RIG_PATHS) | {
            "/out/" + name for name in OUT_NAMES}
        self.assertEqual(set(self.srv.RENDER_TEMP_ALL_PATHS), expected)


class DefaultRenderDirTests(unittest.TestCase):
    """§2.3：render_path 缺省回退 $TEMP/houdini_mcp/<日期>/ 规范目录。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server_module()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcp_render_dir_")
        self._orig_dcp = self.srv.cap.default_capture_path

    def tearDown(self):
        self.srv.cap.default_capture_path = self._orig_dcp
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_dcp(self, make_dirs=True, raise_error=False):
        outer = self

        def fake_dcp(hou=None, pane_type="unknown", engine="capture",
                     **kwargs):
            if raise_error:
                raise RuntimeError("dcp boom")
            day_dir = os.path.join(outer.tmp, "houdini_mcp", "2026-09-09")
            if make_dirs:
                os.makedirs(day_dir, exist_ok=True)
            return os.path.join(day_dir, "120000_scene_1_{0}.png".format(
                engine))
        self.srv.cap.default_capture_path = fake_dcp

    def test_fallback_returns_dated_convention_dir(self):
        self._patch_dcp()
        result = self.srv._default_render_output_dir("karma")
        expected = os.path.join(self.tmp, "houdini_mcp", "2026-09-09")
        self.assertEqual(result, expected)
        self.assertTrue(os.path.isdir(result))

    def test_fallback_explicit_path_untouched(self):
        """显式 render_path 不经回退（handler 内 if not render_path 守卫）。"""
        self._patch_dcp(make_dirs=False)
        explicit = os.path.join(self.tmp, "custom_out")
        # 直接验证 helper 语义：非空路径永不触发 helper（行为断言在 handler 测试）
        self.assertTrue(explicit)  # trivial guard
        result = self.srv._default_render_output_dir("opengl")
        self.assertNotEqual(result, explicit)

    def test_fallback_dir_missing_falls_back_to_tempfile(self):
        self._patch_dcp(make_dirs=False)
        result = self.srv._default_render_output_dir("opengl")
        self.assertEqual(result, tempfile.gettempdir())

    def test_fallback_exception_falls_back_to_tempfile(self):
        self._patch_dcp(raise_error=True)
        result = self.srv._default_render_output_dir("karma")
        self.assertEqual(result, tempfile.gettempdir())


class AttachRenderSemanticsTests(unittest.TestCase):
    """§2.4：_attach_render_semantics + resolve_actual_backend 契约。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server_module()

    def test_opengl_mapping(self):
        result = self.srv._attach_render_semantics({}, "opengl")
        self.assertEqual(result["requested_renderer"], "opengl")
        self.assertEqual(result["actual_backend"], "opengl_rop")
        self.assertEqual(result["renderer"], "opengl")

    def test_karma_cpu_mapping(self):
        result = self.srv._attach_render_semantics({}, "karma", "cpu")
        self.assertEqual(result["requested_renderer"], "karma_cpu")
        self.assertEqual(result["actual_backend"], "husk")

    def test_karma_gpu_mapping(self):
        result = self.srv._attach_render_semantics({}, "karma", "gpu")
        self.assertEqual(result["requested_renderer"], "karma_xpu")
        self.assertEqual(result["actual_backend"], "husk")

    def test_mantra_mapping(self):
        result = self.srv._attach_render_semantics({}, "mantra")
        self.assertEqual(result["requested_renderer"], "mantra")
        self.assertEqual(result["actual_backend"], "mantra_rop")

    def test_cleanup_warning_only_when_failures(self):
        result = self.srv._attach_render_semantics({}, "opengl", None, [])
        self.assertNotIn("_cleanup_warning", result)
        result = self.srv._attach_render_semantics(
            {}, "opengl", None, ["/obj/MCP_CAMERA: destroy refused"])
        self.assertIn("MCP_CAMERA", result["_cleanup_warning"])

    def test_existing_fields_not_clobbered(self):
        result = self.srv._attach_render_semantics(
            {"actual_backend": "flipbook"}, "karma", "cpu")
        self.assertEqual(result["actual_backend"], "flipbook")

    def test_non_dict_passthrough(self):
        self.assertIsNone(
            self.srv._attach_render_semantics(None, "opengl"))


class _HandlerTestBase(unittest.TestCase):
    """handler 级公共装置：policy 放行 + render 函数 monkeypatch。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server_module()

    def setUp(self):
        self.server = self.srv.HoudiniMCPServer()
        self.tmp = tempfile.mkdtemp(prefix="mcp_handler_gov_")
        self._saved = {}

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.srv, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self, name, value):
        if name not in self._saved:
            self._saved[name] = getattr(self.srv, name)
        setattr(self.srv, name, value)

    def _allow_policy(self):
        self._patch("_evaluate_render_policy_command",
                    lambda *a, **k: None)

    def _patch_default_dir(self):
        day_dir = os.path.join(self.tmp, "houdini_mcp", "2026-09-09")
        os.makedirs(day_dir, exist_ok=True)

        def fake_dcp(hou=None, pane_type="unknown", engine="capture",
                     **kwargs):
            return os.path.join(day_dir, "120000_scene_1_{0}.png".format(
                engine))
        self._expected_dir = day_dir
        # patch 模块级 cap 引用的 default_capture_path（handler 经
        # _default_render_output_dir → cap.default_capture_path 调用）
        if "cap" not in self._saved:
            self._saved["cap"] = self.srv.cap
        self.srv.cap = types.SimpleNamespace(default_capture_path=fake_dcp)
        return day_dir

    def _tmp_file(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"fake-jpeg")
        return path


class HandlerRenderSingleViewTests(_HandlerTestBase):

    def test_none_path_falls_back_and_fields_attached(self):
        self._allow_policy()
        day_dir = self._patch_default_dir()
        calls = {}

        def fake_render(**kwargs):
            calls.update(kwargs)
            return self._tmp_file("out.jpg")
        self._patch("render_single_view", fake_render)
        result = self.server.handle_render_single_view(
            rotation=(0, 90, 0), render_engine="karma", karma_engine="cpu")
        self.assertEqual(calls["render_path"], day_dir,
                         "None render_path 必须回退规范目录")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["requested_renderer"], "karma_cpu")
        self.assertEqual(result["actual_backend"], "husk")
        self.assertEqual(result["renderer"], "karma_cpu")
        self.assertNotIn("_cleanup_warning", result)
        self.assertEqual(calls.get("cleanup_report"), [])

    def test_explicit_render_path_passthrough(self):
        self._allow_policy()
        self._patch_default_dir()
        calls = {}

        def fake_render(**kwargs):
            calls.update(kwargs)
            return self._tmp_file("out.jpg")
        self._patch("render_single_view", fake_render)
        explicit = os.path.join(self.tmp, "explicit_out")
        os.makedirs(explicit, exist_ok=True)
        self.server.handle_render_single_view(
            render_path=explicit, render_engine="opengl")
        self.assertEqual(calls["render_path"], explicit,
                         "显式传参行为不变")

    def test_cleanup_warning_attached_on_success(self):
        self._allow_policy()
        self._patch_default_dir()

        def fake_render(**kwargs):
            kwargs["cleanup_report"].append(
                "/obj/MCP_CAMERA: destroy refused")
            return self._tmp_file("out.jpg")
        self._patch("render_single_view", fake_render)
        result = self.server.handle_render_single_view(
            render_engine="opengl")
        self.assertEqual(result["status"], "success")
        self.assertIn("MCP_CAMERA", result["_cleanup_warning"])

    def test_error_path_still_has_semantics_and_warning(self):
        self._allow_policy()
        self._patch_default_dir()

        def fake_render(**kwargs):
            kwargs["cleanup_report"].append("/out/MCP_CPU_KARMA: boom")
            raise RuntimeError("render failed")
        self._patch("render_single_view", fake_render)
        result = self.server.handle_render_single_view(
            render_engine="karma", karma_engine="cpu")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["requested_renderer"], "karma_cpu")
        self.assertEqual(result["actual_backend"], "husk")
        self.assertIn("MCP_CPU_KARMA", result["_cleanup_warning"])


class HandlerRenderQuadViewTests(_HandlerTestBase):

    def test_quad_none_path_falls_back_and_fields_attached(self):
        self._allow_policy()
        day_dir = self._patch_default_dir()
        calls = {}

        def fake_render(**kwargs):
            calls.update(kwargs)
            return [self._tmp_file("a.jpg"), self._tmp_file("b.jpg")]
        self._patch("render_quad_view", fake_render)
        result = self.server.handle_render_quad_view(render_engine="opengl")
        self.assertEqual(calls["render_path"], day_dir)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["requested_renderer"], "opengl")
        self.assertEqual(result["actual_backend"], "opengl_rop")


class HandlerRenderSpecificCameraTests(_HandlerTestBase):

    def test_specific_camera_none_path_falls_back(self):
        self._allow_policy()
        day_dir = self._patch_default_dir()
        calls = {}

        def fake_render(**kwargs):
            calls.update(kwargs)
            return self._tmp_file("cam.jpg")
        self._patch("render_specific_camera", fake_render)
        # handler 用 srv.hou.node 验证相机存在
        self._patch("hou", types.SimpleNamespace(
            node=lambda path: _StubCamNode()))
        result = self.server.handle_render_specific_camera(
            "/obj/user_cam", render_engine="mantra")
        self.assertEqual(calls["render_path"], day_dir)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["requested_renderer"], "mantra")
        self.assertEqual(result["actual_backend"], "mantra_rop")


# ===========================================================================
# Section C: _render_b64 actual_backend 语义 + quad 单 rig 复用
# ===========================================================================
_B64_PKG_KEY = "render_governance_b64_pkg"
_B64_KEY = _B64_PKG_KEY + "._render_b64"


class _FakeImg(object):
    """_encode_image_to_base64 可用的最小 img stub。"""

    def __init__(self, payload=b"\x89PNG\r\n\x1a\nfake"):
        self._payload = payload

    def save(self, buf, fmt):
        buf.write(self._payload)

    def width(self):
        return 320

    def height(self):
        return 240


class _FakeB64Hou(object):
    def __init__(self, rig_nodes=()):
        self.hipFile = object()
        self._registry = {}
        self._destroyed_paths = []
        outer = self
        for path in rig_nodes:
            def _do_destroy(_path=path):
                # 默认参数绑定 _path（函数体内求值），避免闭包晚绑定
                if _path in outer._destroyed_paths:
                    raise RuntimeError("already destroyed")
                outer._destroyed_paths.append(_path)
            self._registry[path] = types.SimpleNamespace(destroy=_do_destroy)

    def node(self, path):
        return self._registry.get(path)


class _FakeB64RenderHelpers(object):
    def __init__(self):
        self.setup_calls = []
        self.rotations = []
        self.resets = 0

    def find_displayed_geometry(self):
        return [object()]

    def calculate_bounding_box(self, nodes):
        return {"min": [-1.0, -1.0, -1.0], "max": [1.0, 1.0, 1.0],
                "center": [0.0, 0.0, 0.0]}

    def setup_camera_rig(self, center, orthographic=False):
        self.setup_calls.append((tuple(center), orthographic))
        return object()

    def rotate_camera_center(self, null, rotation):
        self.rotations.append(tuple(rotation))

    def reset_camera_center(self, null):
        self.resets += 1

    def adjust_camera_to_fit_bbox(self, camera, bbox, padding_factor=1.1):
        pass


def _load_b64_fresh():
    for key in (_B64_PKG_KEY, _B64_KEY, _B64_PKG_KEY + "._render_policy",
                _B64_PKG_KEY + "._common", _B64_PKG_KEY + "._pane_capture"):
        sys.modules.pop(key, None)
    package = types.ModuleType(_B64_PKG_KEY)
    package.__path__ = [ROOT]
    sys.modules[_B64_PKG_KEY] = package

    # _common：真实加载（_add_response_metadata 被调用）
    spec = _ilu.spec_from_file_location(
        _B64_PKG_KEY + "._common", os.path.join(ROOT, "_common.py"))
    common = _ilu.module_from_spec(spec)
    sys.modules[_B64_PKG_KEY + "._common"] = common
    spec.loader.exec_module(common)

    spec = _ilu.spec_from_file_location(
        _B64_PKG_KEY + "._render_policy",
        os.path.join(ROOT, "_render_policy.py"))
    policy = _ilu.module_from_spec(spec)
    sys.modules[_B64_PKG_KEY + "._render_policy"] = policy
    spec.loader.exec_module(policy)

    fake_helpers = _FakeB64RenderHelpers()
    sys.modules[_B64_PKG_KEY + ".HoudiniMCPRender"] = fake_helpers

    spec = _ilu.spec_from_file_location(
        _B64_KEY, os.path.join(ROOT, "_render_b64.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[_B64_KEY] = mod
    spec.loader.exec_module(mod)
    return mod, fake_helpers


def _install_fake_pane_capture(payload=b"\x89PNG\r\n\x1a\nflipbook"):
    fake_pc = types.ModuleType(_B64_PKG_KEY + "._pane_capture")

    def fake_capture(hou, pane_type_name, save_path=None, fit_contents=True,
                     **kwargs):
        with open(save_path, "wb") as f:
            f.write(payload)
        return {"status": "success", "save_path": save_path,
                "pane_type": pane_type_name, "width": 320, "height": 240,
                "size_bytes": len(payload),
                "_renderer": "flipbook_via_Houdini_internal"}
    fake_pc.capture_pane_screenshot = fake_capture
    sys.modules[_B64_PKG_KEY + "._pane_capture"] = fake_pc
    return fake_pc


class B64ActualBackendTests(unittest.TestCase):
    """§2.4：karma 请求走截图回退时 MUST NOT 仅标 karma。"""

    def setUp(self):
        self.mod, self.helpers = _load_b64_fresh()
        self.mod._rp.enforce_render_policy = (
            lambda renderer: ("allow", None))

    def tearDown(self):
        sys.modules.pop(_B64_PKG_KEY + "._pane_capture", None)

    def test_flipbook_fallback_marked(self):
        """saveImage 缺失 + _pane_capture 成功 → actual_backend=flipbook。"""
        self.mod._saveimage_available = lambda hou: False
        _install_fake_pane_capture()
        hou = _FakeB64Hou()
        result = self.mod.render_viewport(hou, renderer="karma_cpu")
        self.assertNotIn("_warning", result)
        self.assertEqual(result["requested_renderer"], "karma_cpu")
        self.assertEqual(result["renderer"], "karma_cpu")
        self.assertEqual(result["actual_backend"], "flipbook")
        # 旧 B4 标记保留兼容
        self.assertEqual(result["_renderer"], "qscreen_fallback")

    def test_saveimage_path_marked_qscreen(self):
        """saveImage 直读视口快照 → actual_backend=qscreen_fallback。"""
        self.mod._saveimage_available = lambda hou: True
        self.mod._grab_viewport_image = lambda hou, w, h: _FakeImg()
        hou = _FakeB64Hou()
        result = self.mod.render_viewport(hou, renderer="karma_xpu")
        self.assertNotIn("_warning", result)
        self.assertEqual(result["requested_renderer"], "karma_xpu")
        self.assertEqual(result["actual_backend"], "qscreen_fallback")

    def test_warning_path_has_requested_renderer_only(self):
        """未实际渲染（无 hipFile）→ 只有 requested_renderer。"""
        hou = types.SimpleNamespace(hipFile=None)
        result = self.mod.render_viewport(hou, renderer="opengl")
        self.assertIn("_warning", result)
        self.assertEqual(result["requested_renderer"], "opengl")
        self.assertNotIn("actual_backend", result)


class B64QuadSingleRigTests(unittest.TestCase):
    """§2.1（base64 版）：quad 单 rig 复用 + finally 清理。"""

    def setUp(self):
        self.mod, self.helpers = _load_b64_fresh()
        self.mod._rp.enforce_render_policy = (
            lambda renderer: ("allow", None))
        self.mod._saveimage_available = lambda hou: True
        self.mod._grab_viewport_image = lambda hou, w, h: _FakeImg()

    def tearDown(self):
        sys.modules.pop(_B64_PKG_KEY + "._pane_capture", None)

    def test_rig_built_once_reset_three_times_destroyed(self):
        hou = _FakeB64Hou(rig_nodes=("/obj/MCP_CAM_CENTER",
                                     "/obj/MCP_CAMERA"))
        result = self.mod.render_quad_views(hou)
        for view in ("top", "front", "side", "perspective"):
            self.assertIn(view, result)
        self.assertEqual(len(self.helpers.setup_calls), 1,
                         "单 rig：setup_camera_rig 只调一次")
        self.assertEqual(self.helpers.resets, 3,
                         "其余 3 视图先归零旋转再叠加")
        self.assertEqual(
            sorted(hou._destroyed_paths),
            sorted(["/obj/MCP_CAM_CENTER", "/obj/MCP_CAMERA"]),
            "rig 必须在 finally 清理")

    def test_rig_cleanup_failure_does_not_kill_result(self):
        hou = _FakeB64Hou(rig_nodes=("/obj/MCP_CAM_CENTER",
                                     "/obj/MCP_CAMERA"))
        # 让其中一个 rig 节点 destroy 抛错
        rig_node = hou._registry["/obj/MCP_CAMERA"]

        def refusing():
            raise RuntimeError("destroy refused")
        rig_node.destroy = refusing
        result = self.mod.render_quad_views(hou)
        # 渲染结果不受清理失败影响
        for view in ("top", "front", "side", "perspective"):
            self.assertIn(view, result)


# ===========================================================================
# Section D: bridge / server 静态 AST 探针
# ===========================================================================
BRIDGE_PY = os.path.join(ROOT, "houdini_mcp_server.py")
SERVER_PY = os.path.join(ROOT, "server.py")
RENDER_LIB_PY = os.path.join(ROOT, "HoudiniMCPRender.py")
BRIDGE_RENDER_TOOLS = ("render_single_view", "render_quad_views",
                       "render_specific_camera")
SERVER_RENDER_HANDLERS = ("handle_render_single_view", "handle_render_quad_view",
                          "handle_render_specific_camera")


def _parse(path):
    with open(path, "r", encoding="utf-8") as f:
        return ast.parse(f.read())


def _find_funcs(tree, names):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out[node.name] = node
    return out


class BridgeToolStaticTests(unittest.TestCase):
    """§2.3/§2.4：bridge 三工具 render_path 默认 None + docstring 如实。"""

    @classmethod
    def setUpClass(cls):
        cls.tree = _parse(BRIDGE_PY)
        cls.funcs = _find_funcs(cls.tree, set(BRIDGE_RENDER_TOOLS))

    def test_render_path_default_is_none(self):
        for name in BRIDGE_RENDER_TOOLS:
            node = self.funcs[name]
            defaults = node.args.defaults
            kwdefaults = node.args.kw_defaults
            values = [d for d in defaults + kwdefaults if d is not None]
            const_values = [d.value for d in values
                            if isinstance(d, ast.Constant)]
            self.assertNotIn(
                "C:/temp/", const_values,
                "{0} 不得默认发送 C:/temp/".format(name))
            # render_path 位置参数默认必须是 None
            arg_names = [a.arg for a in node.args.args]
            idx = arg_names.index("render_path")
            # defaults 对齐：前面无默认参数个数 = len(args) - len(defaults)
            offset = len(arg_names) - len(defaults)
            render_path_default = defaults[idx - offset]
            self.assertIsInstance(render_path_default, ast.Constant)
            self.assertIsNone(render_path_default.value,
                              "{0}.render_path 默认必须为 None".format(name))

    def test_docstrings_mention_actual_backend(self):
        for name in BRIDGE_RENDER_TOOLS:
            doc = ast.get_docstring(self.funcs[name]) or ""
            self.assertIn("actual_backend", doc,
                          "{0} docstring 须描述 actual_backend 语义".format(name))
            self.assertIn("requested_renderer", doc)


class ServerHandlerStaticTests(unittest.TestCase):
    """server 端静态守卫：handler 走规范目录回退，不再 fallback tempfile。"""

    @classmethod
    def setUpClass(cls):
        cls.tree = _parse(SERVER_PY)
        cls.funcs = _find_funcs(cls.tree, set(SERVER_RENDER_HANDLERS))
        cls.lib_tree = _parse(RENDER_LIB_PY)
        cls.lib_funcs = _find_funcs(cls.lib_tree, {
            "render_single_view", "render_quad_view", "render_specific_camera"})

    def test_handlers_use_default_render_output_dir(self):
        for name in SERVER_RENDER_HANDLERS:
            src = ast.get_source_segment(
                open(SERVER_PY, "r", encoding="utf-8").read(),
                self.funcs[name])
            self.assertIn("_default_render_output_dir", src,
                          "{0} 必须走规范目录回退".format(name))
            self.assertNotIn(
                "tempfile.gettempdir()", src,
                "{0} 不得直接回退 tempfile.gettempdir()".format(name))

    def test_handlers_pass_cleanup_report(self):
        for name in SERVER_RENDER_HANDLERS:
            src = ast.get_source_segment(
                open(SERVER_PY, "r", encoding="utf-8").read(),
                self.funcs[name])
            self.assertIn("cleanup_report", src,
                          "{0} 必须透传 cleanup_report".format(name))

    def test_render_lib_functions_have_cleanup_report(self):
        for name in ("render_single_view", "render_quad_view",
                     "render_specific_camera"):
            arg_names = [a.arg for a in self.lib_funcs[name].args.args]
            self.assertIn("cleanup_report", arg_names,
                          "{0} 缺 cleanup_report 参数".format(name))


if __name__ == "__main__":
    unittest.main()
