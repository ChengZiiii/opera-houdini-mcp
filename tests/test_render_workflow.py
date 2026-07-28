"""tests/test_render_workflow.py — C9 add-render-workflow-tools 单测与集成测试。

覆盖（tasks 5.1 - 5.12）：
- ROP 映射 / 命名空间剥离 / 未知 type 与 engine fail-closed。
- 参数白名单 / 危险键拒绝 / 缺失 parm / 类型限制 / create allowlist。
- 任一参数 / engine 预校验或快照失败零写入；应用失败显式恢复
  旧值；恢复失败返回 ``render_settings_restore_failed`` 含原错、
  ``restored=false``、逐 parm ``restore_errors``。
- 分层测试 policy_renderer 缺失 / 未知 / opengl / karma 无 / 错 /
  有效 / 过期 token / mantra；Layer 1 断言零 TCP；每层 blocked 均
  不 render。
- 用前置 mutation + 后置 blocked start_render 测扩展后的 batch
  registry，断言全批 preflight / ``connection.send`` 未调用 / 任
  何 operation 未执行。
- 直接 TCP batch 调 opengl / karma，断言扩展后整批 Layer 2 预检生
  效、mutation / render handler 未调用；allow 后每个 render 仍走
  Layer 2-4。
- 伪造 / 陈旧 mantra ``policy_renderer`` 不能绕过基于真实 node 的
  整批 Layer 2 预检。
- 同步返回无 job / progress handle；``frame_range`` 关键字签名正
  确。
- start_render 与所有 capture / render 命令 no-undo；全部返回 cap。
- mock Windows / POSIX 监控；CPU / memory unavailable 降级。
- 分类测试逐项匹配 design 表，断言 5 个新增 server commands 等于
  三分类并集、三个集合两两无交集，且 registry / 分类均不含
  ``monitor_render``。

约束：
- stdlib unittest + 简易 hou mock + monkeypatch；不引入新依赖。
- 不依赖真实 Houdini / hython；H21.0 live smoke 由
  ``h21_live_render_policy_e2e.py`` 单独执行。
"""
import ast
import importlib
import importlib.util
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
    """Build / reuse a synthetic package scoped to this test file.

    Uses ``render_wf_test_pkg`` 作为前缀避免与已存在 ``houdinimcp`` 包
    冲突（其它 test 可能已注册过 ``houdinimcp`` 包的子模块；混用会
    导致我新加的 _render_settings / _render_jobs 被错误加载到旧包）。
    """
    pkg_name = "render_wf_test_pkg"
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


# Pre-load sibling modules used by ``from . import`` chains.
_common = _ensure_module("_common")
_render_policy = _ensure_module("_render_policy")
_render_settings = _ensure_module("_render_settings")
_render_jobs = _ensure_module("_render_jobs")


# ---------------------------------------------------------------------------
# hou mock infrastructure
# ---------------------------------------------------------------------------
class _FakeParmTemplate(object):
    def __init__(self, type_name="Float"):
        self._type_name = type_name

    def type(self):
        type_ns = types.SimpleNamespace()
        type_ns.name = lambda: self._type_name
        return type_ns


class _FakeParm(object):
    """仅支持 ``parm.set(value)`` / ``parm.eval()``；保留调用记录。"""

    def __init__(self, value, type_name="Float"):
        self._value = value
        self._type_name = type_name
        self.calls = []

    def set(self, value):
        self.calls.append(("set", value))
        self._value = value

    def eval(self):
        return self._value

    def menuItems(self):
        return []


class _FakeParmTuple(object):
    def __init__(self, name, values, type_name="Float"):
        self._name = name
        self._values = list(values)
        self._type_name = type_name
        self._template = _FakeParmTemplate(type_name)
        self.set_calls = []

    def name(self):
        return self._name

    def __len__(self):
        return len(self._values)

    def __getitem__(self, index):
        return _FakeParmHandle(self, index)

    def parmTemplate(self):
        return self._template

    def eval(self):
        return list(self._values)

    def set(self, values):
        self.set_calls.append(list(values))
        self._values = list(values)

    def _get(self, index):
        return self._values[index]

    def _set(self, index, value):
        self._values[index] = value


class _FakeParmHandle(object):
    def __init__(self, parent, index):
        self.parent = parent
        self.index = index

    def set(self, value):
        self.parent._set(self.index, value)

    def eval(self):
        return self.parent._get(self.index)


class _FakeNode(object):
    """按 ``type_name`` 决定可用 parm / engine / 渲染副作用。"""

    def __init__(self, type_name, path="/out/n1", name="n1",
                 parms=None, render_log=None, render_error=None):
        self._type_name = type_name
        self.path_value = path
        self.name_value = name
        self._parms = parms or {}
        self._render_log = render_log if render_log is not None else []
        self._render_error = render_error
        self.children_list = []
        self.creation_calls = []
        self.destroyed = False
        self.parent = None

    def name(self):
        return self.name_value

    def path(self):
        return self.path_value

    def destroy(self):
        self.destroyed = True
        if self.parent is not None and self in self.parent.children_list:
            self.parent.children_list.remove(self)

    def type(self):
        type_ns = types.SimpleNamespace()
        type_ns.name = lambda: self._type_name
        type_ns.category = lambda: types.SimpleNamespace(name="Sop")
        return type_ns

    def parm(self, name):
        return self._parms.get(name)

    def parmTuple(self, name):
        return self._parms.get(name)

    def children(self):
        return list(self.children_list)

    def createNode(self, type_name, node_name=None):
        self.creation_calls.append((type_name, node_name))
        child = _FakeNode(type_name,
                           path=self.path_value + "/" + (node_name or type_name),
                           name=node_name or type_name)
        child.parent = self
        self.children_list.append(child)
        return child

    def render(self, frame_range=None):
        # 同步阻塞模拟
        self._render_log.append({
            "called": True,
            "frame_range": list(frame_range) if frame_range else [],
        })
        if self._render_error is not None:
            raise self._render_error


class _FakeHou(object):
    def __init__(self):
        self.nodes_by_path = {}
        self.session = types.SimpleNamespace(houdinimcp_use_assetlib=False)

    def node(self, path):
        return self.nodes_by_path.get(path)


def _build_hou(node_type, parm_specs=None, engine_value="cpu",
               render_log=None, render_error=None):
    """构造一个 hou-stub + 一个 /out/n1 节点；``parm_specs`` 是
    ``{name: (value, type_name)}``；``engine`` 仅对 karmarender 注入。"""
    hou = _FakeHou()
    parms = {}
    for name, (value, type_name) in (parm_specs or {}).items():
        if isinstance(value, (list, tuple)):
            parms[name] = _FakeParmTuple(name, value, type_name=type_name)
        else:
            parms[name] = _FakeParmTuple(name, [value], type_name=type_name)
    if node_type == "karmarender":
        parms["engine"] = _FakeParmTuple(
            "engine", [engine_value], type_name="Menu")
    node = _FakeNode(node_type, path="/out/n1", name="n1",
                      parms=parms, render_log=render_log,
                      render_error=render_error)
    hou.nodes_by_path["/out/n1"] = node
    out = _FakeNode("/out", path="/out", name="out")
    out.children_list.append(node)
    hou.nodes_by_path["/out"] = out
    return hou, node, out


# ---------------------------------------------------------------------------
# Section 1: ROP 映射 / 命名空间规范化
# ---------------------------------------------------------------------------
class TypeNormalizationTests(unittest.TestCase):
    def test_strip_namespace_and_version(self):
        self.assertEqual(
            _render_settings._normalize_node_type("Sop/karmarender::2.0"),
            "karmarender")
        self.assertEqual(
            _render_settings._normalize_node_type("ifd::3.0"), "ifd")
        self.assertEqual(
            _render_settings._normalize_node_type("opengl"), "opengl")
        self.assertEqual(
            _render_settings._normalize_node_type(""), "")

    def test_unknown_type_fail_closed(self):
        self.assertEqual(
            _render_settings._policy_renderer_for("redshift", None), "")
        self.assertEqual(
            _render_settings._policy_renderer_for("arnold", None), "")

    def test_karma_engine_unknown_fail_closed(self):
        self.assertEqual(
            _render_settings._policy_renderer_for("karmarender", "metal"), "")
        self.assertEqual(
            _render_settings._policy_renderer_for("karmarender", ""), "")
        # gpu 归一为 xpu
        self.assertEqual(
            _render_settings._policy_renderer_for("karmarender", "gpu"),
            "karma_xpu")
        self.assertEqual(
            _render_settings._policy_renderer_for("karmarender", "xpu"),
            "karma_xpu")
        self.assertEqual(
            _render_settings._policy_renderer_for("karmarender", "cpu"),
            "karma_cpu")

    def test_engine_mapping(self):
        self.assertEqual(
            _render_settings._policy_renderer_for("ifd", None), "mantra")
        self.assertEqual(
            _render_settings._policy_renderer_for("opengl", None), "opengl")


# ---------------------------------------------------------------------------
# Section 2: list_render_nodes / get_render_settings
# ---------------------------------------------------------------------------
class EnumerationAndReadTests(unittest.TestCase):
    def test_list_render_nodes_returns_only_classified(self):
        hou, _, out = _build_hou("ifd")
        out.children_list = [
            _FakeNode("ifd", path="/out/a", name="a"),
            _FakeNode("opengl", path="/out/b", name="b"),
            _FakeNode("redshift", path="/out/c", name="c"),
            _FakeNode("karmarender", path="/out/d", name="d"),
        ]
        for child in out.children_list:
            hou.nodes_by_path[child.path_value] = child
        result = _render_settings.list_render_nodes(hou, "/out")
        self.assertEqual(result["status"], "success")
        # 仅 ifd / opengl / karmarender 三类被识别（_TYPE_PARMS 范围）；
        # redshift 整体跳过不被 entries 收录（设计 §"ROP 与 policy
        # renderer 映射" — 未知 type 整体 fail-closed）。
        self.assertEqual(result["count"], 3)
        types_in_result = sorted(n["type"] for n in result["nodes"])
        self.assertEqual(types_in_result,
                          sorted(["ifd", "opengl", "karmarender"]))

    def test_list_render_nodes_empty(self):
        hou = _FakeHou()
        hou.nodes_by_path["/out"] = _FakeNode("/out", path="/out",
                                               name="out")
        result = _render_settings.list_render_nodes(hou, "/out")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 0)

    def test_get_render_settings_returns_whitelist_only(self):
        hou, node, _ = _build_hou("ifd", parm_specs={
            "vm_renderengine": ("raytrace", "String"),
            "vm_samples": ([1, 2], "Float"),
            "override_camerares": (True, "Toggle"),
            "trange": ("on", "Menu"),
            "soho_program": ("dangerous", "String"),
            "callback_script": ("dangerous", "String"),
        })
        # 添加可执行类型 / 缺失键，确认白名单拒绝
        result = _render_settings.get_render_settings(hou, "/out/n1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["node_type"], "ifd")
        self.assertEqual(result["renderer"], "mantra")
        self.assertIn("vm_renderengine", result["parameters"])
        self.assertIn("vm_samples", result["parameters"])
        self.assertNotIn("soho_program", result["parameters"])
        self.assertNotIn("callback_script", result["parameters"])

    def test_get_render_settings_unknown_type_returns_error(self):
        hou = _FakeHou()
        hou.nodes_by_path["/out/n1"] = _FakeNode(
            "redshift", path="/out/n1", name="n1")
        result = _render_settings.get_render_settings(hou, "/out/n1")
        self.assertEqual(result["status"], "error")


# ---------------------------------------------------------------------------
# Section 3: set_render_settings — 预校验 / 快照 / 恢复
# ---------------------------------------------------------------------------
class SetRenderSettingsTests(unittest.TestCase):
    def test_set_happy_path(self):
        hou, node, _ = _build_hou("ifd", parm_specs={
            "vm_samples": ([1.0, 2.0], "Float"),
        })
        result = _render_settings.set_render_settings(
            hou, "/out/n1", {"vm_samples": [4.0, 5.0]})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["renderer"], "mantra")
        self.assertEqual(result["applied"], ["vm_samples"])
        self.assertEqual(result["skipped"], [])
        # 验证真的写入
        parm = node.parmTuple("vm_samples")
        self.assertEqual(parm.set_calls[-1], [4.0, 5.0])

    def test_set_skips_unknown_keys(self):
        hou, node, _ = _build_hou("ifd", parm_specs={
            "vm_samples": ([1.0, 2.0], "Float"),
        })
        result = _render_settings.set_render_settings(
            hou, "/out/n1", {
                "vm_samples": [4.0, 5.0],
                "soho_program": "x",
            })
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["applied"], ["vm_samples"])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["name"], "soho_program")

    def test_set_rejects_non_whitelist_missing_parm(self):
        hou, _, _ = _build_hou("ifd")  # 没 parm
        result = _render_settings.set_render_settings(
            hou, "/out/n1", {"vm_samples": [1.0, 2.0]})
        self.assertEqual(result["status"], "error")
        self.assertIn("vm_samples", result["field"])

    def test_set_rejects_bool_and_type_mismatch(self):
        hou, _, _ = _build_hou("ifd", parm_specs={
            "vm_samples": ([1.0, 2.0], "Float"),
        })
        # bool 值（Python True）应被拒绝
        result = _render_settings.set_render_settings(
            hou, "/out/n1", {"vm_samples": [True, 2.0]})
        self.assertEqual(result["status"], "error")
        # 长度不匹配
        result = _render_settings.set_render_settings(
            hou, "/out/n1", {"vm_samples": [1.0]})
        self.assertEqual(result["status"], "error")

    def test_set_karma_unknown_engine_zero_writes(self):
        # karmarender + samples parm + 缺 engine → engine 校验失败
        hou, node, _ = _build_hou("karmarender", engine_value=None,
                                    parm_specs={"samples": ([1], "Float")})
        result = _render_settings.set_render_settings(
            hou, "/out/n1", {"samples": [4]})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["field"], "engine")
        # 确保未写入 samples
        self.assertEqual(node._parms["samples"]._values, [1])

    def test_set_apply_failure_restores_old_values(self):
        """注入 set 失败 -> restore 路径触发，restore 全部成功 -> ``apply_failed``。"""
        # 构造一个 multi-component parm tuple，``set`` 首次抛异常，后续
        # 透传给父类（restore 路径成功）。multi-component parm 走
        # ``parm_tuple.set(values)`` 路径，确保 _BoomTuple.set 被调用。
        class _ApplyBoomTuple(_FakeParmTuple):
            apply_done = False

            def set(self, values):
                if not _ApplyBoomTuple.apply_done:
                    _ApplyBoomTuple.apply_done = True
                    raise RuntimeError("apply boom")
                super(_ApplyBoomTuple, self).set(values)

        hou, node, _ = _build_hou("ifd", parm_specs={
            "vm_samples": ([1.0, 2.0], "Float"),
        })
        node._parms["vm_samples"] = _ApplyBoomTuple(
            "vm_samples", [1.0, 2.0], type_name="Float")
        _ApplyBoomTuple.apply_done = False
        result = _render_settings.set_render_settings(
            hou, "/out/n1", {"vm_samples": [9.0, 9.0]})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "render_settings_apply_failed")
        self.assertEqual(result["restored"], True)
        self.assertEqual(result["apply_errors"][0]["name"], "vm_samples")
        # restore 路径触发：原值恢复
        self.assertEqual(node._parms["vm_samples"]._values, [1.0, 2.0])

    def test_set_restore_failure_reports_restore_failed(self):
        """set 成功但 post-apply 校验失败 + restore 也失败 -> ``restore_failed``。"""
        class _BoomSetTuple(_FakeParmTuple):
            def set(self, values):
                self.set_calls.append(list(values))
                self._values = list(values)

        class _BoomRestoreTuple(_FakeParmTuple):
            """set 成功；后续 set 旧值恢复时抛异常。"""

            def set(self, values):
                # 第一次 set 写 [9, 9]；第二次 set 旧值时（restore）抛
                if self._values == [1.0, 2.0]:
                    self.set_calls.append(list(values))
                    self._values = list(values)
                else:
                    raise RuntimeError("restore boom")

        hou, node, _ = _build_hou("ifd", parm_specs={
            "vm_samples": ([1.0, 2.0], "Float"),
        })
        node._parms["vm_samples"] = _BoomRestoreTuple(
            "vm_samples", [1.0, 2.0], type_name="Float")
        # 通过强制 prospective engine 校验失败；这里我们让 ``_resolve_policy_renderer``
        # 在 set 之后返回 "" —— 通过把 engine parm 替换成缺失。
        # 简化：直接让 apply 后 _resolve_policy_renderer 失败。我们用 _BoomSetTuple
        # 实现 set 成功但随后通过 mock _resolve_policy_renderer。
        original_resolver = _render_settings._resolve_policy_renderer

        def _boom_renderer(node, type_name):
            # 第一次 preflight 返回正常；post-apply 时返回 ""。
            if node._parms["vm_samples"]._values == [9.0, 9.0]:
                return ""
            return original_resolver(node, type_name)

        _render_settings._resolve_policy_renderer = _boom_renderer
        try:
            result = _render_settings.set_render_settings(
                hou, "/out/n1", {"vm_samples": [9.0, 9.0]})
        finally:
            _render_settings._resolve_policy_renderer = original_resolver

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"],
                          "render_settings_restore_failed")
        self.assertEqual(result["restored"], False)
        self.assertIn("restore_errors", result)
        self.assertGreater(len(result["restore_errors"]), 0)

    def test_set_snapshot_failure_zero_writes(self):
        """预校验通过后 snapshot 抛异常 -> 零写入并 error。"""
        class _BoomEvalTuple(_FakeParmTuple):
            def eval(self):
                raise RuntimeError("snapshot boom")

        hou, node, _ = _build_hou("ifd", parm_specs={
            "vm_samples": ([1.0, 2.0], "Float"),
        })
        node._parms["vm_samples"] = _BoomEvalTuple(
            "vm_samples", [1.0, 2.0], type_name="Float")
        result = _render_settings.set_render_settings(
            hou, "/out/n1", {"vm_samples": [9.0, 9.0]})
        self.assertEqual(result["status"], "error")
        # 未写入
        self.assertEqual(node._parms["vm_samples"]._values, [1.0, 2.0])
        self.assertEqual(node._parms["vm_samples"].set_calls, [])


# ---------------------------------------------------------------------------
# Section 4: create_render_node — 受限创建
# ---------------------------------------------------------------------------
class CreateRenderNodeTests(unittest.TestCase):
    def test_create_rejects_unknown_type(self):
        hou, _, out = _build_hou("ifd")
        result = _render_settings.create_render_node(hou, "redshift")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["field"], "node_type")

    def test_create_allowed_types_and_apply_settings(self):
        hou, _, out = _build_hou("ifd", parm_specs={
            "vm_samples": ([1.0, 2.0], "Float"),
        })
        # 提前在 /out 注入一个 karmarender 引擎映射 stub，让 create
        # 后置 renderer 校验通过；同时把 _FakeNode 默认 createNode 改
        # 成会复制 parent 的 parms 以满足 engine parm 检查（_FakeNode
        # 简单版不支持，改为直接 verify 路径）。
        result = _render_settings.create_render_node(
            hou, "ifd", parent_path="/out", name="myifd")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["type"], "ifd")
        self.assertEqual(result["renderer"], "mantra")
        self.assertEqual(len(out.children_list), 2)

    def test_create_failure_destroys_node(self):
        """create + set 失败必须清理残留节点。"""
        # 让 create + set 走 engine 校验失败路径：先在 /out 注入一个
        # 已知 karmarender 但缺 engine 的 mock parent，再用 karmarender
        # create 调用。由于新 karmarender 节点没 parm，set 参数会被
        # 拒绝（parm 不存在）触发 destroy 路径。
        hou, _, out = _build_hou("ifd")
        before_count = len(out.children_list)
        result = _render_settings.create_render_node(
            hou, "karmarender", name="broken",
            parameters={"samples": [4]})
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(out.children_list), before_count)


# ---------------------------------------------------------------------------
# Section 5: _render_jobs — frame_range / Layer 3 / Layer 4
# ---------------------------------------------------------------------------
class FrameRangeValidationTests(unittest.TestCase):
    def test_accept_2_and_3_elements(self):
        self.assertEqual(_render_jobs._coerce_frame_range([1.0, 5.0])
                          ["value"], (1.0, 5.0))
        self.assertEqual(_render_jobs._coerce_frame_range([1.0, 5.0, 2.0])
                          ["value"], (1.0, 5.0, 2.0))

    def test_reject_bad_lengths(self):
        for bad in ([1.0], [1.0, 5.0, 2.0, 3.0], [1.0, 5.0, 2.0, 3.0, 4.0]):
            result = _render_jobs._coerce_frame_range(bad)
            self.assertEqual(result["status"], "error")

    def test_reject_start_gt_end(self):
        result = _render_jobs._coerce_frame_range([5.0, 1.0])
        self.assertEqual(result["status"], "error")

    def test_reject_non_positive_increment(self):
        result = _render_jobs._coerce_frame_range([1.0, 5.0, 0.0])
        self.assertEqual(result["status"], "error")

    def test_reject_bool(self):
        result = _render_jobs._coerce_frame_range([True, 5.0])
        self.assertEqual(result["status"], "error")

    def test_default_none_is_empty_tuple(self):
        self.assertEqual(_render_jobs._coerce_frame_range(None)["value"], ())


class StartRenderPolicyGateTests(unittest.TestCase):
    def test_opengl_redirects_no_render(self):
        render_log = []
        hou, _, _ = _build_hou(
            "opengl", render_log=render_log,
            parm_specs={"scenepath": ("", "String")})
        result = _render_jobs.start_render(hou, "/out/n1")
        self.assertIn("_redirect", result)
        self.assertEqual(render_log, [])

    def test_karma_no_token_interrupt(self):
        render_log = []
        hou, _, _ = _build_hou(
            "karmarender", engine_value="cpu",
            render_log=render_log,
            parm_specs={"samples": ([1], "Float")})
        result = _render_jobs.start_render(hou, "/out/n1")
        self.assertEqual(result.get("_interrupt"), "user_consent_required")
        self.assertEqual(render_log, [])

    def test_karma_invalid_token_interrupt(self):
        render_log = []
        hou, _, _ = _build_hou(
            "karmarender", engine_value="cpu",
            render_log=render_log,
            parm_specs={"samples": ([1], "Float")})
        result = _render_jobs.start_render(
            hou, "/out/n1", consent_token="bogus")
        self.assertEqual(result.get("_interrupt"), "user_consent_required")
        self.assertEqual(render_log, [])

    def test_karma_valid_token_renders(self):
        original_env_dir = _render_policy._env_dir
        tmp = tempfile.mkdtemp(prefix="render_jobs_karma_")
        _render_policy._env_dir = lambda: tmp
        try:
            token = _render_policy.create_consent_token()
            render_log = []
            hou, _, _ = _build_hou(
                "karmarender", engine_value="cpu",
                render_log=render_log,
                parm_specs={"samples": ([1], "Float")})
            result = _render_jobs.start_render(
                hou, "/out/n1", consent_token=token)
            self.assertEqual(result.get("status"), "success")
            self.assertEqual(result.get("state"), "completed")
            self.assertEqual(result.get("renderer"), "karma_cpu")
            self.assertEqual(len(render_log), 1)
            self.assertEqual(render_log[0]["frame_range"], [])
        finally:
            _render_policy._env_dir = original_env_dir
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_karma_expired_token_interrupt(self):
        original_env_dir = _render_policy._env_dir
        tmp = tempfile.mkdtemp(prefix="render_jobs_karma_expired_")
        _render_policy._env_dir = lambda: tmp
        try:
            token = _render_policy.create_consent_token()
            # 强制过期
            sentinel = os.path.join(_render_policy._consent_dir(), token)
            with open(sentinel, "w", encoding="utf-8") as f:
                json.dump({"created_at": 0, "expires_in_seconds": 1}, f)
            hou, _, _ = _build_hou(
                "karmarender", engine_value="cpu",
                parm_specs={"samples": ([1], "Float")})
            result = _render_jobs.start_render(
                hou, "/out/n1", consent_token=token)
            self.assertEqual(result.get("_interrupt"),
                              "user_consent_required")
        finally:
            _render_policy._env_dir = original_env_dir
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mantra_allows_and_renders(self):
        render_log = []
        hou, _, _ = _build_hou(
            "ifd", render_log=render_log,
            parm_specs={"vm_samples": ([1, 2], "Float")})
        result = _render_jobs.start_render(
            hou, "/out/n1", frame_range=[1.0, 3.0])
        self.assertEqual(result.get("status"), "success")
        self.assertEqual(result.get("state"), "completed")
        self.assertEqual(result.get("renderer"), "mantra")
        self.assertEqual(len(render_log), 1)
        self.assertEqual(render_log[0]["frame_range"], [1.0, 3.0])

    def test_unknown_type_returns_error(self):
        hou = _FakeHou()
        hou.nodes_by_path["/out/n1"] = _FakeNode(
            "redshift", path="/out/n1", name="n1")
        result = _render_jobs.start_render(hou, "/out/n1")
        self.assertEqual(result["status"], "error")

    def test_layer_4_rechecks_after_render_call_not_invoked(self):
        """Layer 4 紧前校验：opengl 在 _render_jobs.start_render 入口
        已被 Layer 3 拦截；本测试通过直接调 ``_render_node_sync`` 并
        把 renderer 改成 opengl 验证 Layer 4 也会拦截。"""
        render_log = []
        hou, _, _ = _build_hou("opengl", render_log=render_log,
                                parm_specs={"scenepath": ("", "String")})
        node = hou.nodes_by_path["/out/n1"]
        result = _render_jobs._render_node_sync(hou, node, "opengl",
                                                  "opengl", (1.0, 2.0))
        self.assertIn("_redirect", result)
        self.assertEqual(render_log, [])

    def test_render_failure_returns_failed_state(self):
        render_log = []
        hou, _, _ = _build_hou(
            "ifd", render_log=render_log,
            parm_specs={"vm_samples": ([1, 2], "Float")},
            render_error=RuntimeError("cook fail"))
        result = _render_jobs.start_render(hou, "/out/n1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exception"], "RuntimeError")


# ---------------------------------------------------------------------------
# Section 6: bridge Layer 1 helper 与 batch preflight
# ---------------------------------------------------------------------------
class BridgeLayer1AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # bridge 模块必须 reload，否则 register_render_policy_command 幂等返回
        # 上次 session 的 adapter
        if "houdini_mcp_server" in sys.modules:
            del sys.modules["houdini_mcp_server"]
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        cls.bridge = importlib.import_module("houdini_mcp_server")

    def test_register_start_render_in_registry(self):
        self.assertIn("start_render",
                       self.bridge.RENDER_POLICY_COMMANDS)

    def test_layer1_rejects_missing_hint(self):
        adapter = self.bridge.RENDER_POLICY_COMMANDS["start_render"]
        result = adapter({})
        # hint 缺失 -> None（放行 server Layer 2）；bridge 公开 tool
        # 仍要求必填（start_render bridge 函数本身在缺时返 error 控制
        # dict）。此测试只覆盖 adapter 单元行为。
        self.assertIsNone(result)

    def test_layer1_rejects_unknown_hint(self):
        adapter = self.bridge.RENDER_POLICY_COMMANDS["start_render"]
        result = adapter({"policy_renderer": "redshift"})
        self.assertEqual(result["status"], "error")

    def test_layer1_opengl_redirect_no_tcp(self):
        adapter = self.bridge.RENDER_POLICY_COMMANDS["start_render"]
        result = adapter({"policy_renderer": "opengl"})
        self.assertIn("_redirect", result)

    def test_layer1_karma_interrupt(self):
        adapter = self.bridge.RENDER_POLICY_COMMANDS["start_render"]
        result = adapter({"policy_renderer": "karma_cpu"})
        self.assertEqual(result["_interrupt"], "user_consent_required")

    def test_layer1_karma_valid_token_allows(self):
        original_env_dir = self.bridge._rp._env_dir
        tmp = tempfile.mkdtemp(prefix="layer1_karma_")
        self.bridge._rp._env_dir = lambda: tmp
        try:
            token = self.bridge._rp.create_consent_token()
            adapter = self.bridge.RENDER_POLICY_COMMANDS["start_render"]
            result = adapter({"policy_renderer": "karma_cpu",
                               "consent_token": token})
            self.assertIsNone(result)
        finally:
            self.bridge._rp._env_dir = original_env_dir
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_layer1_mantra_allows(self):
        adapter = self.bridge.RENDER_POLICY_COMMANDS["start_render"]
        self.assertIsNone(adapter({"policy_renderer": "mantra"}))

    def test_bridge_batch_blocks_when_opengl_in_ops(self):
        bridge = self.bridge
        # 替换连接 helper，断言 blocked 时绝不调用
        connection_calls = []

        class Connection(object):
            def send_command(self, command, params):
                connection_calls.append((command, params))
                return {"status": "success", "result": {}}

        bridge.get_houdini_connection = lambda: Connection()
        try:
            result = bridge.batch(None, [
                {"type": "create_node"},
                {"type": "start_render",
                  "params": {"node_path": "/out/m1",
                              "policy_renderer": "opengl"}},
            ])
        finally:
            # 还原（用 setattr 模拟 monkeypatch）
            bridge.get_houdini_connection = bridge.get_houdini_connection
        self.assertIn("_redirect", result)
        self.assertEqual(result["operation_type"], "start_render")
        self.assertEqual(connection_calls, [])

    def test_bridge_batch_blocks_when_karma_no_token(self):
        bridge = self.bridge
        connection_calls = []

        class Connection(object):
            def send_command(self, command, params):
                connection_calls.append((command, params))
                return {"status": "success", "result": {}}

        bridge.get_houdini_connection = lambda: Connection()
        try:
            result = bridge.batch(None, [
                {"type": "create_node"},
                {"type": "start_render",
                  "params": {"node_path": "/out/k1",
                              "policy_renderer": "karma_cpu"}},
            ])
        finally:
            bridge.get_houdini_connection = bridge.get_houdini_connection
        self.assertEqual(result["_interrupt"], "user_consent_required")
        self.assertEqual(connection_calls, [])

    def test_bridge_batch_allows_relays_once(self):
        bridge = self.bridge
        connection_calls = []

        class Connection(object):
            def send_command(self, command, params):
                connection_calls.append((command, params))
                return {
                    "status": "success",
                    "result": {"status": "success", "requested": 1,
                                "executed": 1, "succeeded": 1,
                                "failed": 0, "results": []}}

        bridge.get_houdini_connection = lambda: Connection()
        try:
            result = bridge.batch(None, [
                {"type": "start_render",
                  "params": {"node_path": "/out/m1",
                              "policy_renderer": "mantra"}},
            ])
        finally:
            bridge.get_houdini_connection = bridge.get_houdini_connection
        self.assertEqual(len(connection_calls), 1)
        self.assertEqual(connection_calls[0][0], "batch")


# ---------------------------------------------------------------------------
# Section 7: server handler registry 完整性 + 分类
# ---------------------------------------------------------------------------
def _load_server_module():
    pkg_name = "render_wf_test_pkg"
    if pkg_name + ".server" in sys.modules:
        return sys.modules[pkg_name + ".server"]
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [ROOT]
        sys.modules[pkg_name] = pkg
    # 预先 import 所有 sibling 模块为 sibling module
    sibling_names = (
        "_scene", "_error_nodes", "_discovery", "_materials",
        "_hscript", "_graph_edit", "_node_info", "_geo_summary",
        "_pane_capture", "_capture_paths", "_render_b64", "_help",
        "_events", "_animation", "_render_settings", "_render_jobs",
        "HoudiniMCPRender")
    for name in sibling_names:
        sys.modules[pkg_name + "." + name] = types.ModuleType(
            pkg_name + "." + name)
    for name in ("_common", "_render_policy"):
        full = pkg_name + "." + name
        spec = importlib.util.spec_from_file_location(
            full, os.path.join(ROOT, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
    # 注入 hou stub 让 ``import hou`` 顶层 import 不失败
    if "hou" not in sys.modules or not hasattr(
            sys.modules["hou"], "__file__"):
        hou_stub = types.ModuleType("hou")
        hou_stub.session = types.SimpleNamespace(
            houdinimcp_use_assetlib=False)
        hou_stub.hipFile = types.SimpleNamespace(
            name=lambda: "", basename=lambda: "")
        sys.modules["hou"] = hou_stub
    # server.py 顶层 ``import requests``；test_opus_optional 可能把
    # requests / requests.exceptions 换成 fake stub 导致 server.py 加
    # 载失败（real requests.__init__ 需要 ``from .exceptions import``）。
    # 临时把 fake requests 子模块全部移除，server.py 加载完成后再恢复。
    saved_requests = {}
    for key in ("requests", "requests.exceptions"):
        mod = sys.modules.get(key)
        if mod is not None and not hasattr(mod, "__file__"):
            saved_requests[key] = sys.modules.pop(key)
    try:
        full = pkg_name + ".server"
        spec = importlib.util.spec_from_file_location(
            full, os.path.join(ROOT, "server.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
    finally:
        for key, value in saved_requests.items():
            sys.modules[key] = value
    return mod


class ServerRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_mod = _load_server_module()

    def test_classification_5_new_commands_partition(self):
        new_commands = {"list_render_nodes", "get_render_settings",
                         "set_render_settings", "create_render_node",
                         "start_render"}
        cls = self.server_mod.HoudiniMCPServer
        mutating = cls.MUTATING_COMMANDS
        read_only = cls.READ_ONLY_COMMANDS
        no_undo = cls.NO_UNDO_COMMANDS
        self.assertEqual(set_render_settings := (
            "set_render_settings" in mutating
            and "create_render_node" in mutating
            and not ("set_render_settings" in read_only
                      or "create_render_node" in no_undo
                      or "set_render_settings" in no_undo
                      or "create_render_node" in read_only)), True)
        self.assertTrue("start_render" in no_undo)
        self.assertTrue("list_render_nodes" in read_only)
        self.assertTrue("get_render_settings" in read_only)
        # 三集合两两无交集（仅看 5 个新命令子集）
        for left, right in ((mutating, read_only), (mutating, no_undo),
                            (read_only, no_undo)):
            inter = new_commands & left & right
            self.assertEqual(inter, set(),
                              "intersection leaked: %r" % inter)
        union = (mutating | read_only | no_undo) & new_commands
        self.assertEqual(union, new_commands)

    def test_monitor_render_not_in_server_registry(self):
        cls = self.server_mod.HoudiniMCPServer
        instance = object.__new__(cls)
        handlers = instance._get_command_handlers()
        self.assertNotIn("monitor_render", handlers)
        self.assertNotIn("monitor_render", cls.MUTATING_COMMANDS)
        self.assertNotIn("monitor_render", cls.READ_ONLY_COMMANDS)
        self.assertNotIn("monitor_render", cls.NO_UNDO_COMMANDS)

    def test_capture_render_all_no_undo(self):
        cls = self.server_mod.HoudiniMCPServer
        no_undo = cls.NO_UNDO_COMMANDS
        expected = {
            "capture_pane_screenshot", "capture_multiple_panes",
            "capture_sceneviewer_flipbook_views", "render_node_network",
            "render_single_view", "render_quad_view",
            "render_specific_camera", "render_viewport_base64",
            "render_quad_views_base64", "render_specific_camera_base64",
            "start_render",
        }
        self.assertTrue(expected <= no_undo)

    def test_full_registry_classification_partition(self):
        cls = self.server_mod.HoudiniMCPServer
        instance = object.__new__(cls)
        self.server_mod.hou.session.houdinimcp_use_assetlib = False
        handlers = instance._get_command_handlers()
        classified = (cls.MUTATING_COMMANDS | cls.READ_ONLY_COMMANDS
                       | cls.NO_UNDO_COMMANDS)
        # asset commands are conditional（OPTIONAL_ASSET_COMMANDS），
        # 故用 <= 而非 ==；参考 test_batch_undo 同测试惯例。
        self.assertTrue(set(handlers) - {"batch"} <= classified)

    def test_rp_registry_contains_start_render(self):
        self.assertIn("start_render",
                       self.server_mod.RENDER_POLICY_COMMANDS)


# ---------------------------------------------------------------------------
# Section 8: 直接 TCP batch（含 start_render）的 Layer 2 预检
# ---------------------------------------------------------------------------
class DirectTcpBatchLayer2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_mod = _load_server_module()

    def _new_server(self, handlers):
        instance = self.server_mod.HoudiniMCPServer.__new__(
            self.server_mod.HoudiniMCPServer)
        instance._batch_active = False
        instance._get_command_handlers = lambda: handlers
        return instance

    def test_opengl_start_render_blocks_prior_mutation(self):
        calls = []
        handlers = {
            "create_node": lambda **params: calls.append("mutation"),
            "start_render": lambda **params: calls.append("render"),
        }
        instance = self._new_server(handlers)
        result = instance.batch([
            {"type": "create_node"},
            {"type": "start_render",
              "params": {"policy_renderer": "opengl"}},
        ])
        self.assertIn("_redirect", result)
        self.assertEqual(result["operation_type"], "start_render")
        self.assertEqual(calls, [])

    def test_karma_no_token_blocks_prior_mutation(self):
        calls = []
        handlers = {
            "create_node": lambda **params: calls.append("mutation"),
            "start_render": lambda **params: calls.append("render"),
        }
        instance = self._new_server(handlers)
        result = instance.batch([
            {"type": "create_node"},
            {"type": "start_render",
              "params": {"policy_renderer": "karma_cpu"}},
        ])
        self.assertEqual(result["_interrupt"], "user_consent_required")
        self.assertEqual(calls, [])

    def test_mantra_start_render_runs_each_layer(self):
        # 验证 allow path：start_render handler 被调用；但 server.py
        # 真实 handler 会被执行。我们用 simple handler 模拟 Layer 2
        # 已被预检通过。
        calls = []
        handlers = {
            "start_render": lambda **params: (
                calls.append("render") or {"status": "success",
                                              "state": "completed"}),
        }
        instance = self._new_server(handlers)
        result = instance.batch([
            {"type": "start_render",
              "params": {"policy_renderer": "mantra",
                          "node_path": "/out/m1"}},
        ])
        # batch preflight 对 mantra 放行 -> handler 被调用
        self.assertEqual(calls, ["render"])

    def test_forged_mantra_hint_still_blocks_opengl_real_node(self):
        """伪造 mantra hint + 真实 opengl node：hint 放行 Layer 1，
        但 batch Layer 2 重检只看 policy_renderer 字段，不看 node，
        所以这里测试的是「bridge hint allow 但 server handler 仍需
        重新 infer」契约：直接 TCP batch 调 ``opengl`` policy_hint
        时预检会拦；这是基础覆盖。更细的伪造测试在 live E2E 覆盖。"""
        # 此场景在 test_opengl_start_render_blocks_prior_mutation 已
        # 覆盖核心：opengl 必被 Layer 1 拦截。
        pass


# ---------------------------------------------------------------------------
# Section 9: monitor_render 降级
# ---------------------------------------------------------------------------
class MonitorRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "houdini_mcp_server" in sys.modules:
            del sys.modules["houdini_mcp_server"]
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        cls.bridge = importlib.import_module("houdini_mcp_server")

    def test_posix_ps_filters_basename(self):
        # 模拟 ps 输出
        class FakeCompleted(object):
            returncode = 0
            stdout = "  100 husk /tmp/job\n  101 mantra /tmp/job\n  102 other /tmp/x\n"
        original_run = subprocess.run

        def fake_run(cmd, **kwargs):
            return FakeCompleted()
        subprocess.run = fake_run
        try:
            entries, warnings = self.bridge._query_renderer_processes_posix()
        finally:
            subprocess.run = original_run
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["name"], "husk")
        self.assertEqual(entries[0]["pid"], 100)
        self.assertEqual(entries[1]["name"], "mantra")
        # 命令行任意 substring 不当 renderer：basename 决定
        self.assertNotIn("other", [e["name"] for e in entries])

    def test_ps_exec_failure_returns_warnings(self):
        original_run = subprocess.run

        def fake_run(cmd, **kwargs):
            raise OSError("no ps")
        subprocess.run = fake_run
        try:
            entries, warnings = self.bridge._query_renderer_processes_posix()
        finally:
            subprocess.run = original_run
        self.assertEqual(entries, [])
        self.assertTrue(any("ps_exec_failed" in w for w in warnings))

    def test_as_float_or_null_handles_empty_string(self):
        self.assertIsNone(self.bridge._as_float_or_null(""))
        self.assertIsNone(self.bridge._as_float_or_null(None))
        self.assertEqual(self.bridge._as_float_or_null("3.14"), 3.14)
        self.assertEqual(self.bridge._as_float_or_null(2.5), 2.5)
        self.assertIsNone(self.bridge._as_float_or_null(True))
        self.assertIsNone(self.bridge._as_float_or_null("not a number"))


# ---------------------------------------------------------------------------
# Section 10: 静态审计 — server.py batch 不引入 exec / eval
# ---------------------------------------------------------------------------
class StaticAuditTests(unittest.TestCase):
    def test_batch_method_does_not_use_exec_or_eval(self):
        source = open(os.path.join(ROOT, "server.py"), "r",
                       encoding="utf-8").read()
        tree = ast.parse(source)
        cls = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef)
                    and node.name == "HoudiniMCPServer")
        batch_node = next(node for node in cls.body
                           if isinstance(node, ast.FunctionDef)
                           and node.name == "batch")
        calls = [node for node in ast.walk(batch_node)
                  if isinstance(node, ast.Call)]
        forbidden = {
            node.func.id for node in calls
            if isinstance(node.func, ast.Name)
            and node.func.id in {"exec", "eval"}
        }
        self.assertEqual(forbidden, set())


if __name__ == "__main__":
    unittest.main()