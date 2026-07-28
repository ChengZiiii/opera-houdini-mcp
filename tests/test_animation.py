"""Unit tests for external/houdinimcp/_animation.py + server.py handlers
(PR 19 add-animation-and-frame-control).

Stdlib unittest, no hython required. Mock hou.* with SimpleNamespace +
callable mocks；测试按设计契约验证：

- 数值输入先拒绝 bool / NaN / ±inf / 非数值，统一 float。
- frame / range / keyframe frame / value / fps / time / increment 返回
  float，不做 ``int()`` 截断。
- step_forward / step_backward 仅通过
  ``hou.setFrame(current ± hou.playbar.frameIncrement())`` 路径，并
  clamp 到 playback range 闭区间；越界也 clamp 回最近端点。
- set_keyframes 预校验完整列表后写入；任一项无效整调用失败
  （set_count=0, error_index 标记），全部有效时在单个 undo group
  中写入。
- play / reverse / stop 直接调 SideFX HOM 同名方法。
- set_expression 仅接受 hscript / python；映射到 hou.exprLanguage 对
  应枚举。
- server_mod handler 分类（MUTATING / READ_ONLY / NO_UNDO）正确且
  partition。
- batch dispatcher：mutating → no-undo → mutating 时第二个 mutating
  段强制重新开 segment（NO_UNDO 命令始终在 group 外）。
- 10 个 handler 均过 apply_response_cap。

Run with:
    python -m unittest tests.test_animation -v
    或
    python -m pytest tests/test_animation.py -v
"""
import importlib.util as _ilu
import inspect
import os
import sys
import types
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# Package bootstrap — 让 _animation.py / server.py 用 ``from . import _common``
# ---------------------------------------------------------------------------
def _ensure_pkg():
    """Build / reuse a synthetic ``houdinimcp`` package with ROOT on __path__."""
    if "houdinimcp" in sys.modules and getattr(
            sys.modules["houdinimcp"], "__path__", None):
        return sys.modules["houdinimcp"]
    pkg = types.ModuleType("houdinimcp")
    pkg.__path__ = [ROOT]
    sys.modules["houdinimcp"] = pkg
    return pkg


def _ensure_module(name):
    """Ensure houdinimcp.<name> resolved to actual file; reload fresh."""
    pkg = _ensure_pkg()
    full = "houdinimcp." + name
    if full in sys.modules:
        del sys.modules[full]
    spec = _ilu.spec_from_file_location(
        full, os.path.join(ROOT, name + ".py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_hou():
    """Install a minimal callable hou stub for module imports."""
    if "hou" in sys.modules and getattr(
            sys.modules["hou"], "_is_animation_stub", False):
        return sys.modules["hou"]
    hou = types.ModuleType("hou")
    hou.session = types.SimpleNamespace(houdinimcp_use_assetlib=False)
    hou.hipFile = types.SimpleNamespace(
        path=lambda: "", basename=lambda: "",
        isNewFile=lambda: True, save=lambda **k: None,
        load=lambda **k: None, clear=lambda **k: None)
    hou.paneTabType = types.SimpleNamespace(
        NetworkEditor=object(), SceneViewer=object(),
        Compositor=object(), ChannelEditor=object(),
        ParameterEditor=object(), PythonPanel=object())
    hou.ui = types.SimpleNamespace()
    hou.expandString = lambda s: ""
    hou.frame = lambda: 1
    hou.node = lambda p: None
    hou._is_animation_stub = True
    sys.modules["hou"] = hou
    return hou


_stub_hou()
_pkg = _ensure_pkg()
# Pre-load _common so sibling modules can ``from . import _common as cmn``.
_common = _ensure_module("_common")
# Animation module under test.
anim = _ensure_module("_animation")


# ---------------------------------------------------------------------------
# Helpers — hou mocks for animation tests
# ---------------------------------------------------------------------------
class _FakeKeyframe(object):
    """Stand-in for hou.Keyframe。记录 frame 与 value 供断言。"""

    def __init__(self, value):
        self._value = float(value)
        self._frame = None

    def setFrame(self, frame):
        self._frame = float(frame)

    def frame(self):
        return self._frame

    def value(self):
        return self._value


class _FakeKeyframeList(object):
    """Fake parm.keyframes() return；持有 _FakeKeyframe 列表。

    deleteKeyframeAtFrame 删除匹配的 frame（含 sub-frame epsilon）。
    """

    def __init__(self, keyframes=None):
        self._items = list(keyframes or [])

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, idx):
        return self._items[idx]

    def setKeyframe(self, kf):
        self._items.append(kf)

    def deleteKeyframeAtFrame(self, target_frame):
        kept = [
            k for k in self._items
            if abs(float(k.frame()) - float(target_frame)) > 1e-6]
        self._items = kept

    def keyframes(self):
        return list(self._items)


class _FakeParm(object):
    """记录 setKeyframe / deleteKeyframeAtFrame / setExpression 调用。"""

    def __init__(self, keyframes=None):
        self._kf_list = _FakeKeyframeList(keyframes)
        self.calls = []

    def keyframes(self):
        return self._kf_list.keyframes()

    def setKeyframe(self, kf):
        self.calls.append(("set_keyframe", kf))
        self._kf_list.setKeyframe(kf)

    def deleteKeyframeAtFrame(self, frame):
        self.calls.append(("delete_keyframe_at_frame", float(frame)))
        self._kf_list.deleteKeyframeAtFrame(frame)

    def setExpression(self, expr, lang):
        self.calls.append(("set_expression", expr, lang))


class _FakeNode(object):
    def __init__(self, parms=None):
        self._parms = parms or {}

    def parm(self, name):
        return self._parms.get(name)


class _FakeUndoGroup(object):
    """Context manager that records enter / exit; never breaks."""

    def __init__(self, label):
        self.label = label
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        return False


class _FakeUndoApi(object):
    """Records group labels so tests can assert undo-segment lifecycle."""

    def __init__(self):
        self.events = []

    def group(self, label):
        group = _FakeUndoGroup(label)
        self.events.append(("create", label, group))
        return group


class _FakePlaybar(object):
    """Stand-in for hou.playbar; tracks calls and configurable state."""

    def __init__(self, frame_range=(1.0, 24.0),
                 playback_range=(1.0, 24.0),
                 increment=1.0):
        self._frame_range = (float(frame_range[0]), float(frame_range[1]))
        self._playback_range = (float(playback_range[0]),
                                float(playback_range[1]))
        self._increment = float(increment)
        self.calls = []

    def frameRange(self):
        return self._frame_range

    def setFrameRange(self, start, end):
        self.calls.append(("setFrameRange", float(start), float(end)))
        self._frame_range = (float(start), float(end))

    def playbackRange(self):
        return self._playback_range

    def setPlaybackRange(self, start, end):
        self.calls.append(("setPlaybackRange", float(start), float(end)))
        self._playback_range = (float(start), float(end))

    def frameIncrement(self):
        return self._increment

    def play(self):
        self.calls.append(("play",))

    def reverse(self):
        self.calls.append(("reverse",))

    def stop(self):
        self.calls.append(("stop",))


def _build_hou(frame=1.0, time_=0.0, fps_=24.0,
               frame_range=(1.0, 24.0),
               playback_range=(1.0, 24.0),
               increment=1.0,
               nodes=None,
               parms=None,
               keyframes_global=None,
               with_undos=True):
    """构造一个 hou-stub 合集，返回 (hou, calls_log) 二元组。

    calls_log 累积所有 hou 上的 setters / 副作用调用，方便断言。
    """
    calls_log = []

    class _Hou(object):
        def __init__(self):
            self.playbar = _FakePlaybar(
                frame_range=frame_range,
                playback_range=playback_range,
                increment=increment)
            self._frame = float(frame)
            self._time = float(time_)
            self._fps = float(fps_)
            self._nodes = nodes or {}
            self._parms = parms or {}
            self._keyframes_global = keyframes_global or {}

        def frame(self):
            return self._frame

        def setFrame(self, value):
            self._frame = float(value)
            calls_log.append(("setFrame", float(value)))

        def time(self):
            return self._time

        def fps(self):
            return self._fps

        def node(self, path):
            return self._nodes.get(path)

        def parm(self, path, name):
            """给 ``node.parm(name)`` 的间接通道；测试可直接传 _Hou.parm。"""
            if (path, name) in self._parms:
                return self._parms[(path, name)]
            return None

        def Keyframe(self, value):
            kf = _FakeKeyframe(value)
            calls_log.append(("Keyframe", float(value)))
            return kf

        class exprLanguage(object):
            Hscript = "Hscript"
            Python = "Python"

        def _resolve_parm(self, path, name):
            node = self.node(path)
            if node is None:
                return None
            return node.parm(name)

    hou = _Hou()
    if with_undos:
        hou.undos = _FakeUndoApi()
        calls_log.append(("undos_init",))
    else:
        hou.undos = None
    # 给 ``node.parm(name)`` 一个真实路径
    if hasattr(hou, "_resolve_parm"):
        pass
    return hou, calls_log


# ---------------------------------------------------------------------------
# Section A: number validation
# ---------------------------------------------------------------------------
class NumberValidationTests(unittest.TestCase):
    """`_coerce_finite_number` 与 `_coerce_pair` 的拒绝 / 接受矩阵。"""

    def test_int_passes(self):
        result = anim._coerce_finite_number("x", 3)
        self.assertEqual(result, {"value": 3.0})

    def test_float_int_passes(self):
        result = anim._coerce_finite_number("x", 1.5)
        self.assertEqual(result["value"], 1.5)

    def test_string_rejected(self):
        result = anim._coerce_finite_number("x", "1.0")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["field"], "x")

    def test_none_rejected(self):
        result = anim._coerce_finite_number("x", None)
        self.assertEqual(result["status"], "error")

    def test_bool_true_rejected(self):
        """bool 是 int 子类，先判 bool 才能正确拒绝。"""
        result = anim._coerce_finite_number("x", True)
        self.assertEqual(result["status"], "error")

    def test_bool_false_rejected(self):
        result = anim._coerce_finite_number("x", False)
        self.assertEqual(result["status"], "error")

    def test_nan_rejected(self):
        result = anim._coerce_finite_number("x", float("nan"))
        self.assertEqual(result["status"], "error")
        self.assertIn("finite", result["message"])

    def test_positive_inf_rejected(self):
        result = anim._coerce_finite_number("x", float("inf"))
        self.assertEqual(result["status"], "error")

    def test_negative_inf_rejected(self):
        result = anim._coerce_finite_number("x", float("-inf"))
        self.assertEqual(result["status"], "error")

    def test_pair_equal_endpoints_allowed(self):
        pair = anim._coerce_pair("start", "end", 24.0, 24.0)
        self.assertEqual(pair, {"start": 24.0, "end": 24.0})

    def test_pair_rejects_start_gt_end(self):
        pair = anim._coerce_pair("start", "end", 5.0, 1.0)
        self.assertEqual(pair["status"], "error")

    def test_pair_rejects_nan_endpoint(self):
        pair = anim._coerce_pair("start", "end", 1.0, float("nan"))
        self.assertEqual(pair["status"], "error")


# ---------------------------------------------------------------------------
# Section B: get_frame
# ---------------------------------------------------------------------------
class GetFrameTests(unittest.TestCase):
    """`get_frame` 返回的 frame / time / fps / 三组 range 全 float。"""

    def test_returns_all_floats(self):
        hou, _ = _build_hou(
            frame=2.5, time_=0.1041666, fps_=24.0,
            frame_range=(1.0, 100.0),
            playback_range=(1.0, 50.0),
            increment=0.5, with_undos=False)
        result = anim.get_frame(hou)
        self.assertEqual(result["status"], "success")
        for key in ("frame", "time", "fps", "frame_increment"):
            self.assertIsInstance(result[key], float)
        for key in ("frame_range", "playback_range"):
            self.assertIsInstance(result[key], list)
            for v in result[key]:
                self.assertIsInstance(v, float)

    def test_sub_frame_preserved(self):
        hou, _ = _build_hou(
            frame=3.75, fps_=30.0, increment=0.25,
            frame_range=(1.0, 240.0),
            playback_range=(1.0, 240.0),
            with_undos=False)
        result = anim.get_frame(hou)
        self.assertEqual(result["frame"], 3.75)
        self.assertEqual(result["fps"], 30.0)
        self.assertEqual(result["frame_increment"], 0.25)


# ---------------------------------------------------------------------------
# Section C: set_frame
# ---------------------------------------------------------------------------
class SetFrameTests(unittest.TestCase):
    """`set_frame` number 校验 + 调用 hou.setFrame 并保留 float。"""

    def test_passes_float_through(self):
        hou, calls = _build_hou(frame=1.0, with_undos=False)
        result = anim.set_frame(hou, 12.5)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["frame"], 12.5)
        self.assertEqual(hou._frame, 12.5)
        self.assertEqual(calls, [("setFrame", 12.5)])

    def test_int_accepted_as_float(self):
        hou, calls = _build_hou(frame=1.0, with_undos=False)
        result = anim.set_frame(hou, 5)
        self.assertEqual(result["frame"], 5.0)
        self.assertEqual(calls, [("setFrame", 5.0)])

    def test_bool_rejected_no_setFrame(self):
        hou, calls = _build_hou(frame=1.0, with_undos=False)
        result = anim.set_frame(hou, True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])

    def test_nan_rejected_no_setFrame(self):
        hou, calls = _build_hou(frame=1.0, with_undos=False)
        result = anim.set_frame(hou, float("nan"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])

    def test_string_rejected(self):
        hou, calls = _build_hou(frame=1.0, with_undos=False)
        result = anim.set_frame(hou, "12.0")
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# Section D: set_frame_range / set_playback_range
# ---------------------------------------------------------------------------
class RangeWriteTests(unittest.TestCase):
    def test_set_frame_range_calls_hou(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.set_frame_range(hou, 1.0, 240.0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(hou.playbar.calls, [("setFrameRange", 1.0, 240.0)])
        self.assertEqual(result["frame_range"], [1.0, 240.0])

    def test_set_frame_range_sub_frame_endpoint_preserved(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.set_frame_range(hou, 0.5, 100.25)
        self.assertEqual(result["status"], "success")
        self.assertEqual(hou.playbar.calls, [("setFrameRange", 0.5, 100.25)])

    def test_set_frame_range_start_gt_end_rejected(self):
        hou, calls = _build_hou(with_undos=False)
        result = anim.set_frame_range(hou, 5.0, 1.0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])

    def test_set_playback_range_calls_hou(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.set_playback_range(hou, 1.0, 240.0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(hou.playbar.calls, [("setPlaybackRange", 1.0, 240.0)])


# ---------------------------------------------------------------------------
# Section E: keyframe CRUD
# ---------------------------------------------------------------------------
class KeyframeCrudTests(unittest.TestCase):
    def _build_with_parm(self, keyframes=None):
        parm = _FakeParm(keyframes)
        node = _FakeNode(parms={"tx": parm})
        hou, _ = _build_hou(
            nodes={"/obj/geo1": node}, with_undos=True)
        return hou, parm, node

    def test_set_keyframe_preserves_float(self):
        hou, parm, _ = self._build_with_parm()
        result = anim.set_keyframe(hou, "/obj/geo1", "tx", 12.5, 3.75)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["frame"], 12.5)
        self.assertEqual(result["value"], 3.75)
        kf = parm._kf_list[0]
        self.assertEqual(kf.frame(), 12.5)
        self.assertEqual(kf.value(), 3.75)

    def test_set_keyframe_rejects_nan_value(self):
        hou, _, _ = self._build_with_parm()
        result = anim.set_keyframe(hou, "/obj/geo1", "tx", 1.0,
                                     float("nan"))
        self.assertEqual(result["status"], "error")

    def test_set_keyframe_rejects_bool_frame(self):
        hou, _, _ = self._build_with_parm()
        result = anim.set_keyframe(hou, "/obj/geo1", "tx", True, 1.0)
        self.assertEqual(result["status"], "error")

    def test_get_keyframes_returns_floats_no_truncation(self):
        seed = [_FakeKeyframe(1.0), _FakeKeyframe(2.5)]
        seed[0].setFrame(1.5)
        seed[1].setFrame(3.25)
        hou, _, _ = self._build_with_parm(keyframes=seed)
        result = anim.get_keyframes(hou, "/obj/geo1", "tx")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["keyframes"],
                          [{"frame": 1.5, "value": 1.0},
                           {"frame": 3.25, "value": 2.5}])
        for kf in result["keyframes"]:
            self.assertIsInstance(kf["frame"], float)
            self.assertIsInstance(kf["value"], float)

    def test_delete_keyframe_sub_frame(self):
        seed = [_FakeKeyframe(1.0)]
        seed[0].setFrame(2.5)
        hou, parm, _ = self._build_with_parm(keyframes=seed)
        result = anim.delete_keyframe(hou, "/obj/geo1", "tx", 2.5)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(parm._kf_list), 0)

    def test_delete_keyframe_missing_returns_error(self):
        seed = [_FakeKeyframe(1.0)]
        seed[0].setFrame(2.5)
        hou, parm, _ = self._build_with_parm(keyframes=seed)
        result = anim.delete_keyframe(hou, "/obj/geo1", "tx", 99.0)
        self.assertEqual(result["status"], "error")
        # 原 keyframe 仍在
        self.assertEqual(len(parm._kf_list), 1)

    def test_resolve_parm_node_missing_returns_error(self):
        hou, _ = _build_hou(nodes={}, with_undos=True)
        result = anim.set_keyframe(hou, "/obj/missing", "tx", 1.0, 1.0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["field"], "path")

    def test_resolve_parm_parameter_missing_returns_error(self):
        parm = _FakeParm()
        node = _FakeNode(parms={"tx": parm})
        hou, _ = _build_hou(nodes={"/obj/geo1": node}, with_undos=True)
        # node.parm("ty") -> None
        result = anim.set_keyframe(hou, "/obj/geo1", "ty", 1.0, 1.0)
        self.assertEqual(result["status"], "error")


# ---------------------------------------------------------------------------
# Section F: set_keyframes 预校验与单 undo group
# ---------------------------------------------------------------------------
class SetKeyframesTests(unittest.TestCase):
    def _make_parm(self):
        parm = _FakeParm()
        node = _FakeNode(parms={"tx": parm})
        return parm, node

    def test_partial_invalid_prevents_any_write(self):
        parm_a, node_a = self._make_parm()
        parm_b, node_b = self._make_parm()
        hou, calls = _build_hou(
            nodes={
                "/obj/a": node_a,
                "/obj/b": node_b,
            }, with_undos=True)
        result = anim.set_keyframes(hou, [
            {"path": "/obj/a", "parameter": "tx",
             "frame": 1.0, "value": 1.0},
            {"path": "/obj/b", "parameter": "tx",
             "frame": 2.0, "value": float("nan")},
            {"path": "/obj/a", "parameter": "tx",
             "frame": 3.0, "value": 3.0},
        ])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["set_count"], 0)
        self.assertEqual(result["error_index"], 1)
        self.assertEqual(result["field"], "value")
        # a 与 b 都没写
        self.assertEqual(len(parm_a._kf_list), 0)
        self.assertEqual(len(parm_b._kf_list), 0)

    def test_full_valid_writes_in_single_undo_group(self):
        parm_a, node_a = self._make_parm()
        parm_b, node_b = self._make_parm()
        hou, _ = _build_hou(
            nodes={
                "/obj/a": node_a,
                "/obj/b": node_b,
            }, with_undos=True)
        result = anim.set_keyframes(hou, [
            {"path": "/obj/a", "parameter": "tx",
             "frame": 1.0, "value": 0.5},
            {"path": "/obj/b", "parameter": "tx",
             "frame": 2.5, "value": 3.75},
        ])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["set_count"], 2)
        self.assertEqual(result["requested"], 2)

        # 断言确切的单 undo group 生命周期
        events = hou.undos.events
        create_events = [e for e in events if e[0] == "create"]
        self.assertEqual(len(create_events), 1)
        self.assertEqual(create_events[0][1], "MCP: set_keyframes")
        group = create_events[0][2]
        self.assertTrue(group.entered)
        self.assertTrue(group.exited)

    def test_empty_list_rejected(self):
        hou, _ = _build_hou(with_undos=True)
        result = anim.set_keyframes(hou, [])
        self.assertEqual(result["status"], "error")

    def test_missing_path_field_rejected(self):
        hou, _ = _build_hou(with_undos=True)
        result = anim.set_keyframes(hou, [
            {"parameter": "tx", "frame": 1.0, "value": 1.0}
        ])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_index"], 0)

    def test_bool_frame_rejected(self):
        parm_a, node_a = self._make_parm()
        hou, _ = _build_hou(
            nodes={"/obj/a": node_a}, with_undos=True)
        result = anim.set_keyframes(hou, [
            {"path": "/obj/a", "parameter": "tx",
             "frame": True, "value": 1.0},
        ])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["set_count"], 0)
        self.assertEqual(len(parm_a._kf_list), 0)

    def test_node_not_found_rejected_no_write(self):
        hou, _ = _build_hou(nodes={}, with_undos=True)
        result = anim.set_keyframes(hou, [
            {"path": "/obj/missing", "parameter": "tx",
             "frame": 1.0, "value": 1.0},
        ])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["set_count"], 0)

    def test_no_undos_attribute_skips_group(self):
        """无 hou.undos 时模块走 None 路径，不抛异常。"""
        parm_a, node_a = self._make_parm()
        hou, _ = _build_hou(
            nodes={"/obj/a": node_a}, with_undos=False)
        result = anim.set_keyframes(hou, [
            {"path": "/obj/a", "parameter": "tx",
             "frame": 1.0, "value": 1.0},
        ])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["set_count"], 1)


# ---------------------------------------------------------------------------
# Section G: playbar_control (run-state, no-undo)
# ---------------------------------------------------------------------------
class PlaybarControlTests(unittest.TestCase):
    def test_play_calls_hou_playbar_play(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.playbar_control(hou, "play")
        self.assertEqual(result["status"], "success")
        self.assertEqual(hou.playbar.calls, [("play",)])

    def test_reverse_calls_hou_playbar_reverse(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.playbar_control(hou, "reverse")
        self.assertEqual(result["status"], "success")
        self.assertEqual(hou.playbar.calls, [("reverse",)])

    def test_stop_calls_hou_playbar_stop(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.playbar_control(hou, "stop")
        self.assertEqual(result["status"], "success")
        self.assertEqual(hou.playbar.calls, [("stop",)])

    def test_step_forward_uses_frameIncrement_and_setFrame(self):
        hou, calls = _build_hou(
            frame=10.0, increment=1.0,
            playback_range=(1.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "step_forward")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["frame"], 11.0)
        # 仅出现 frameIncrement + setFrame；不调 play / reverse / stop / 其他
        self.assertEqual(
            calls,
            [("setFrame", 11.0)],
        )

    def test_step_forward_clamp_to_end(self):
        hou, calls = _build_hou(
            frame=23.5, increment=1.0,
            playback_range=(1.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "step_forward")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["frame"], 24.0)
        self.assertEqual(calls, [("setFrame", 24.0)])

    def test_step_forward_clamps_even_when_current_out_of_range(self):
        """当前帧大于 end 时也 clamp，不 wrap。"""
        hou, calls = _build_hou(
            frame=100.0, increment=1.0,
            playback_range=(1.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "step_forward")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["frame"], 24.0)

    def test_step_backward_uses_frameIncrement_and_setFrame(self):
        hou, calls = _build_hou(
            frame=10.0, increment=1.0,
            playback_range=(1.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "step_backward")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["frame"], 9.0)
        self.assertEqual(calls, [("setFrame", 9.0)])

    def test_step_backward_clamp_to_start(self):
        hou, calls = _build_hou(
            frame=1.0, increment=1.0,
            playback_range=(1.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "step_backward")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["frame"], 1.0)
        self.assertEqual(calls, [("setFrame", 1.0)])

    def test_step_forward_invalid_increment_zero_no_setFrame(self):
        hou, calls = _build_hou(
            frame=10.0, increment=0.0,
            playback_range=(1.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "step_forward")
        self.assertEqual(result["status"], "error")
        # 不能调 hou.setFrame
        self.assertEqual(calls, [])

    def test_step_forward_negative_increment_no_setFrame(self):
        hou, calls = _build_hou(
            frame=10.0, increment=-1.0,
            playback_range=(1.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "step_forward")
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])

    def test_step_forward_nan_increment_no_setFrame(self):
        hou, calls = _build_hou(
            frame=10.0, increment=float("nan"),
            playback_range=(1.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "step_forward")
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])

    def test_step_invalid_playback_range_no_setFrame(self):
        hou, calls = _build_hou(
            frame=10.0, increment=1.0,
            playback_range=(50.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "step_forward")
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])

    def test_goto_start_uses_playback_range_start(self):
        hou, calls = _build_hou(
            frame=24.0, increment=1.0,
            playback_range=(5.0, 24.0), with_undos=False)
        result = anim.playbar_control(hou, "goto_start")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["frame"], 5.0)
        self.assertEqual(calls, [("setFrame", 5.0)])

    def test_goto_end_uses_playback_range_end(self):
        hou, calls = _build_hou(
            frame=1.0, increment=1.0,
            playback_range=(5.0, 50.0), with_undos=False)
        result = anim.playbar_control(hou, "goto_end")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["frame"], 50.0)
        self.assertEqual(calls, [("setFrame", 50.0)])

    def test_unknown_action_rejected(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.playbar_control(hou, "pause")
        self.assertEqual(result["status"], "error")


# ---------------------------------------------------------------------------
# Section H: set_expression
# ---------------------------------------------------------------------------
class SetExpressionTests(unittest.TestCase):
    def _build_with_parm(self):
        parm = _FakeParm()
        node = _FakeNode(parms={"tx": parm})
        hou, _ = _build_hou(nodes={"/obj/geo1": node},
                              with_undos=False)
        return hou, parm

    def test_hscript_calls_set_expression_with_Hscript(self):
        hou, parm = self._build_with_parm()
        result = anim.set_expression(hou, "/obj/geo1", "tx",
                                      "$F", language="hscript")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["language"], "hscript")
        self.assertEqual(parm.calls,
                          [("set_expression", "$F", "Hscript")])

    def test_python_calls_set_expression_with_Python(self):
        hou, parm = self._build_with_parm()
        result = anim.set_expression(hou, "/obj/geo1", "tx",
                                      "hou.frame()", language="python")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["language"], "python")
        self.assertEqual(parm.calls,
                          [("set_expression", "hou.frame()", "Python")])

    def test_unknown_language_rejected(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.set_expression(hou, "/obj/geo1", "tx",
                                      "x", language="vex")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["field"], "language")

    def test_empty_expression_rejected(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.set_expression(hou, "/obj/geo1", "tx", "  ")
        self.assertEqual(result["status"], "error")

    def test_non_string_expression_rejected(self):
        hou, _ = _build_hou(with_undos=False)
        result = anim.set_expression(hou, "/obj/geo1", "tx",
                                      123, language="hscript")
        self.assertEqual(result["status"], "error")

    def test_node_missing_rejected(self):
        hou, _ = _build_hou(nodes={}, with_undos=False)
        result = anim.set_expression(hou, "/obj/missing", "tx",
                                      "x", language="hscript")
        self.assertEqual(result["status"], "error")


# ---------------------------------------------------------------------------
# Section I: server.py handler registration + classification
# ---------------------------------------------------------------------------
def _load_server_module():
    """Load server.py fresh; siblings already boot-strapped by anim tests."""
    return _ensure_module("server")


class ServerClassificationTests(unittest.TestCase):
    """10 个 PR 19 命令分类正确性 + 完整 partition。"""

    @classmethod
    def setUpClass(cls):
        cls.server_mod = _load_server_module()

    def test_read_only_commands_contain_get(self):
        s = self.server_mod.HoudiniMCPServer
        self.assertIn("get_frame", s.READ_ONLY_COMMANDS)
        self.assertIn("get_keyframes", s.READ_ONLY_COMMANDS)

    def test_mutating_commands_contain_data_writes(self):
        s = self.server_mod.HoudiniMCPServer
        for cmd in ("set_frame_range", "set_playback_range",
                    "set_keyframe", "set_keyframes",
                    "delete_keyframe", "set_expression"):
            self.assertIn(cmd, s.MUTATING_COMMANDS)

    def test_no_undo_commands_contain_run_state_writes(self):
        s = self.server_mod.HoudiniMCPServer
        self.assertIn("set_frame", s.NO_UNDO_COMMANDS)
        self.assertIn("playbar_control", s.NO_UNDO_COMMANDS)

    def test_no_overlap_with_other_categories(self):
        s = self.server_mod.HoudiniMCPServer
        new_cmds = {
            "get_frame", "get_keyframes",
            "set_frame_range", "set_playback_range",
            "set_keyframe", "set_keyframes", "delete_keyframe",
            "set_expression",
            "set_frame", "playbar_control",
        }
        overlap_read = new_cmds & s.READ_ONLY_COMMANDS
        overlap_mut = new_cmds & s.MUTATING_COMMANDS
        overlap_no = new_cmds & s.NO_UNDO_COMMANDS
        # each new cmd lives in exactly one bucket
        for cmd in new_cmds:
            count = sum(cmd in s for s in (
                s.READ_ONLY_COMMANDS, s.MUTATING_COMMANDS,
                s.NO_UNDO_COMMANDS))
            self.assertEqual(count, 1, "%s in %d categories" % (cmd, count))
        self.assertEqual(
            overlap_mut & overlap_no, set(),
            "MUT and NO_UNDO overlap: %r"
            % (overlap_mut & overlap_no))

    def test_handlers_registered(self):
        s = self.server_mod.HoudiniMCPServer
        instance = object.__new__(s)
        handlers = instance._get_command_handlers()
        for name in (
            "get_frame", "set_frame", "set_frame_range",
            "set_playback_range", "set_keyframe", "set_keyframes",
            "delete_keyframe", "get_keyframes", "playbar_control",
            "set_expression",
        ):
            self.assertIn(name, handlers,
                          "handler %r missing" % name)

    def test_classification_passes_validate(self):
        s = self.server_mod.HoudiniMCPServer
        instance = object.__new__(s)
        handlers = instance._get_command_handlers()
        # 没有丢失或意外的分类条目
        s._validate_handler_classification(handlers)


# ---------------------------------------------------------------------------
# Section J: handler 方法签名 + apply_response_cap 包裹
# ---------------------------------------------------------------------------
import ast as _ast


def _ast_parse(src):
    return _ast.parse(src)


class HandlerWiringTests(unittest.TestCase):
    """验证 10 个 server.py handler 调 `_animation.x` + `cmn.apply_response_cap`。"""

    @classmethod
    def setUpClass(cls):
        cls.server_mod = _load_server_module()
        with open(os.path.join(ROOT, "server.py"),
                  "r", encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = _ast.parse(cls.source)

    def test_each_handler_calls_animation_module(self):
        cls_node = next(node for node in self.tree.body
                         if isinstance(node, _ast.ClassDef)
                         and node.name == "HoudiniMCPServer")
        for cmd in (
            "get_frame", "set_frame", "set_frame_range",
            "set_playback_range", "set_keyframe", "set_keyframes",
            "delete_keyframe", "get_keyframes", "playbar_control",
            "set_expression",
        ):
            method = next(node for node in cls_node.body
                            if isinstance(node, _ast.FunctionDef)
                            and node.name == cmd)
            src = _ast.unparse(method)
            self.assertIn("anim.", src,
                          "%s handler must call anim.* " % cmd)
            self.assertIn("cmn.apply_response_cap", src,
                          "%s must apply response cap" % cmd)

    def test_animation_module_does_not_import_hou_at_top(self):
        """模块在 import 时不应访问 hou；hou 通过参数注入。"""
        with open(os.path.join(ROOT, "_animation.py"),
                  "r", encoding="utf-8") as handle:
            mod_src = handle.read()
        mod_tree = _ast.parse(mod_src)
        for node in mod_tree.body:
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "hou",
                                          "_animation 顶层不得 import hou")
            elif isinstance(node, _ast.ImportFrom):
                self.assertNotEqual(node.module, "hou",
                                      "_animation 顶层不得 from hou import")


# ---------------------------------------------------------------------------
# Section K: batch dispatcher 关闭 NO_UNDO 命令的 segment
# ---------------------------------------------------------------------------
class BatchSegmentationTests(unittest.TestCase):
    """batch 中 mutating → no-undo → mutating 强制 no-undo 在 segment 外。"""

    @classmethod
    def setUpClass(cls):
        cls.server_mod = _load_server_module()

    def setUp(self):
        self._had_undos = hasattr(self.server_mod.hou, "undos")
        self._undo = _FakeUndoApi()
        self.server_mod.hou.undos = self._undo

    def tearDown(self):
        if self._had_undos:
            # leave real undos; tests don't pollute.
            pass
        # remove our injected undo api
        try:
            del self.server_mod.hou.undos
        except AttributeError:
            pass

    def test_no_undo_command_closes_segment(self):
        sequence = []

        def mutating(**kwargs):
            sequence.append("mutating")
            return {"ok": True}

        def no_undo(**kwargs):
            sequence.append("no_undo_run_state")
            return {"ok": True}

        sm = self.server_mod.HoudiniMCPServer
        instance = self._fresh_server({
            "set_keyframe": mutating, "set_frame": no_undo,
            "get_frame": lambda **k: {"ok": True},
        })
        result = instance.batch([
            {"type": "set_keyframe"},
            {"type": "set_frame"},
            {"type": "set_keyframe"},
        ])
        self.assertEqual(result["status"], "success")
        # 操作序列
        self.assertEqual(sequence,
                          ["mutating", "no_undo_run_state", "mutating"])
        # events: 第一段 mutating → open；no-undo 关闭 segment；
        # 第二个 mutating 再开新 segment
        labels = [ev[1] for ev in self._undo.events
                   if ev[0] == "create"]
        self.assertEqual(len(labels), 2)
        for label in labels:
            self.assertEqual(label, "MCP: batch")

    def test_read_only_does_not_open_segment(self):
        sm = self.server_mod.HoudiniMCPServer
        instance = self._fresh_server({
            "get_frame": lambda **k: {"ok": True},
            "get_keyframes": lambda **k: {"ok": True},
        })
        result = instance.batch([
            {"type": "get_frame"},
            {"type": "get_keyframes"},
        ])
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(self._undo.events, [])

    def _fresh_server(self, handlers):
        sm = self.server_mod.HoudiniMCPServer
        instance = sm.__new__(sm)
        instance._batch_active = False
        instance._get_command_handlers = lambda: handlers
        return instance


# ---------------------------------------------------------------------------
# Section L: bridge 工具：source-level checks
# ---------------------------------------------------------------------------
class BridgeSourceTests(unittest.TestCase):
    """bridge 在 houdini_mcp_server.py 中加 10 个 @mcp.tool() relay。"""

    BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")

    @classmethod
    def setUpClass(cls):
        with open(cls.BRIDGE_PATH, "r", encoding="utf-8") as handle:
            cls.src = handle.read()
        cls.tree = _ast.parse(cls.src)

    def test_ten_bridge_tools_for_animation(self):
        bridge_tree = self.tree
        tool_funcs = []
        for node in _ast.walk(bridge_tree):
            if not isinstance(node, _ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                # bridge decorator is @mcp.tool() → ast.Call(func=ast.Attribute(attr='tool'))
                if (isinstance(dec, _ast.Call)
                        and isinstance(dec.func, _ast.Attribute)
                        and dec.func.attr == "tool"):
                    tool_funcs.append(node.name)
        expected = {
            "get_frame", "set_frame", "set_frame_range",
            "set_playback_range", "set_keyframe", "set_keyframes",
            "delete_keyframe", "get_keyframes", "playbar_control",
            "set_expression",
        }
        self.assertTrue(expected <= set(tool_funcs),
                          "missing bridge tools: %r"
                          % (expected - set(tool_funcs)))

    def test_each_bridge_tool_calls_houdini_call(self):
        bridge_tree = self.tree
        for node in _ast.walk(bridge_tree):
            if not isinstance(node, _ast.FunctionDef):
                continue
            if node.name not in (
                "get_frame", "set_frame", "set_frame_range",
                "set_playback_range", "set_keyframe", "set_keyframes",
                "delete_keyframe", "get_keyframes", "playbar_control",
                "set_expression",
            ):
                continue
            src = _ast.unparse(node)
            self.assertIn("_houdini_call(", src,
                          "%s must relay via _houdini_call" % node.name)


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    unittest.main()
