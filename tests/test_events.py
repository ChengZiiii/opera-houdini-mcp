"""事件 collector、正式 HOM callback、server registry 和 bridge relay 测试。"""

import ast
import importlib.util
import json
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_events_module():
    name = "event_unit_houdinimcp._events"
    package_name = "event_unit_houdinimcp"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [ROOT]
        sys.modules[package_name] = package
    if name in sys.modules:
        return sys.modules[name]
    common_name = package_name + "._common"
    if common_name not in sys.modules:
        common_spec = importlib.util.spec_from_file_location(
            common_name, os.path.join(ROOT, "_common.py"))
        common = importlib.util.module_from_spec(common_spec)
        sys.modules[common_name] = common
        common_spec.loader.exec_module(common)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "_events.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_server_module():
    package_name = "event_server_houdinimcp"
    module_name = package_name + ".server"
    if module_name in sys.modules:
        return sys.modules[module_name]
    package = types.ModuleType(package_name)
    package.__path__ = [ROOT]
    sys.modules[package_name] = package

    for name in (
            "_scene", "_error_nodes", "_discovery", "_materials",
            "_hscript", "_graph_edit", "_node_info", "_geo_summary",
            "_pane_capture", "_capture_paths", "_render_b64", "_help",
            "HoudiniMCPRender"):
        sys.modules[package_name + "." + name] = types.ModuleType(
            package_name + "." + name)

    for name in ("_common", "_render_policy", "_events"):
        full_name = package_name + "." + name
        path = os.path.join(ROOT, name + ".py")
        spec = importlib.util.spec_from_file_location(full_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)

    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(ROOT, "server.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


events = _load_events_module()
server_mod = _load_server_module()


class _Clock(object):
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class EventCollectorTests(unittest.TestCase):
    def test_append_debounce_uses_identity_and_monotonic_clock(self):
        clock = _Clock()
        collector = events.EventCollector(
            maxlen=4, dedupe_window=1.0,
            monotonic_clock=clock, wall_clock=lambda: 123.0)
        first = collector.append("frame_changed", "playbar", {"frame": 1})
        clock.value = 0.5
        second = collector.append("frame_changed", "playbar", {"frame": 2})

        self.assertIs(first, second)
        self.assertEqual(first["_seq"], 1)
        self.assertEqual(first["count"], 2)
        self.assertEqual(first["payload"], {"frame": 2})
        self.assertEqual(len(collector.buffer), 1)
        self.assertIs(collector.key_index[("frame_changed", "playbar")][1], first)

        clock.value = 2.0
        third = collector.append("frame_changed", "playbar", {"frame": 3})
        self.assertIsNot(third, first)
        self.assertEqual(third["_seq"], 2)
        self.assertEqual(len(collector.buffer), 2)

    def test_eviction_does_not_delete_newer_same_key_index(self):
        clock = _Clock()
        collector = events.EventCollector(
            maxlen=2, dedupe_window=0.1,
            monotonic_clock=clock, wall_clock=lambda: 1.0)
        old = collector.append("node_created", "/obj/a")
        collector.append("node_created", "/obj/b")
        clock.value = 1.0
        newer = collector.append("node_created", "/obj/a")

        self.assertIsNot(old, newer)
        self.assertIs(
            collector.key_index[("node_created", "/obj/a")][1], newer)
        clock.value = 1.05
        debounced = collector.append("node_created", "/obj/a")
        self.assertIs(debounced, newer)
        self.assertEqual(len(collector.buffer), 2)

        collector.clear()
        clock.value = 2.0
        collector.append("node_created", "/obj/a")
        collector.append("node_created", "/obj/b")
        clock.value = 3.0
        collector.append("node_created", "/obj/c")
        self.assertNotIn(("node_created", "/obj/a"), collector.key_index)
        rebuilt = collector.append("node_created", "/obj/a")
        self.assertIs(
            collector.key_index[("node_created", "/obj/a")][1], rebuilt)
        self.assertGreaterEqual(collector.dropped_total, 2)


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.state = events.EventState(events.EventCollector(
            maxlen=20, dedupe_window=0.0))
        self.assertEqual(self.state.subscribe()["generation"], 1)

    def test_snapshot_excludes_new_append_until_next_drain(self):
        self.state.append("scene_saved", "/tmp/one.hip", {"id": 1})
        self.state.append("scene_saved", "/tmp/two.hip", {"id": 2})
        self.state.append("scene_saved", "/tmp/three.hip", {"id": 3})

        first = self.state.drain(limit=1)
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["events"][0]["payload"]["id"], 1)
        self.assertTrue(first["next_cursor"])

        self.state.append("scene_saved", "/tmp/four.hip", {"id": 4})
        second = self.state.drain(cursor=first["next_cursor"])
        self.assertEqual(
            [item["payload"]["id"] for item in second["events"]], [2, 3])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(second["remaining"], 1)

        third = self.state.drain()
        self.assertEqual([item["payload"]["id"] for item in third["events"]], [4])

    def test_invalid_and_stale_cursor_do_not_drain(self):
        self.state.append("scene_saved", "/tmp/one.hip")
        self.state.append("scene_saved", "/tmp/two.hip")
        first = self.state.drain(limit=1)
        invalid = self.state.drain(cursor="not-a-cursor")
        self.assertEqual(invalid["error"], "invalid_cursor")
        self.assertEqual(invalid["count"], 0)
        self.assertEqual(invalid["remaining"], 1)

        self.state.subscribe(["frame_changed"])
        stale = self.state.drain(cursor=first["next_cursor"])
        self.assertEqual(stale["error"], "stale_cursor")
        self.assertEqual(stale["remaining"], 0)
        self.assertEqual(len(self.state.collector.buffer), 1)

    def test_response_cap_keeps_unreturned_events(self):
        for index in range(4):
            self.state.append("scene_saved", "/tmp/{0}.hip".format(index),
                              {"index": index, "blob": "x" * 500})

        def cap(payload):
            capped = dict(payload)
            capped["events"] = list(payload["events"][:1])
            capped["_truncated"] = True
            return capped

        first = self.state.drain(limit=3, response_cap=cap)
        self.assertTrue(first["_truncated"])
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["remaining"], 3)
        self.assertEqual(len(self.state.collector.buffer), 3)
        second = self.state.drain(cursor=first["next_cursor"])
        self.assertEqual(
            [item["payload"]["index"] for item in second["events"]], [1, 2, 3])

    def test_apply_response_cap_keeps_buffer_tail(self):
        state = events.EventState(events.EventCollector(
            maxlen=200, dedupe_window=0.0))
        state.subscribe()
        for index in range(100):
            state.append("scene_saved", "/tmp/{0}.hip".format(index),
                         {"index": index, "blob": "y" * 300})
        result = state.drain(limit=100)
        self.assertTrue(result.get("_truncated"))
        self.assertLess(result["count"], 100)
        self.assertEqual(result["remaining"], 100 - result["count"])

    def test_single_oversized_event_is_bounded_and_cursor_advances(self):
        state = events.EventState(events.EventCollector(
            maxlen=4, dedupe_window=0.0))
        state.subscribe()
        state.append("scene_saved", "/tmp/huge.hip", {"blob": "z" * 100000})

        result = state.drain(limit=1)

        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["events"]), 1)
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["remaining"], 0)
        self.assertTrue(result["events"][0]["payload"]["_truncated"])
        self.assertLessEqual(len(json.dumps(result).encode("utf-8")), 16384)
        self.assertEqual(state.drain(limit=1)["count"], 0)

    def test_subscription_generation_changes_only_on_real_change(self):
        empty = events.EventState()
        first = empty.subscribe(["scene_saved", "frame_changed"])
        same = empty.subscribe(["frame_changed", "scene_saved"])
        removed_missing = empty.unsubscribe(["node_created"])
        changed = empty.unsubscribe(["scene_saved"])
        cleared = empty.unsubscribe()

        self.assertEqual(first["generation"], 1)
        self.assertEqual(same["generation"], 1)
        self.assertEqual(removed_missing["generation"], 1)
        self.assertEqual(changed["generation"], 2)
        self.assertEqual(cleared["generation"], 3)
        self.assertEqual(cleared["subscription"], [])

    def test_invalid_subscription_is_structured(self):
        state = events.EventState()
        result = state.subscribe(["future_event"])
        self.assertEqual(result["error"], "invalid_types")
        self.assertEqual(state.generation, 0)


class _FakeHipFile(object):
    def __init__(self):
        self.added = []
        self.removed = []
        self.fail_remove = False

    def addEventCallback(self, callback):
        self.added.append(callback)

    def removeEventCallback(self, callback):
        if self.fail_remove:
            raise RuntimeError("remove callback failed")
        self.removed.append(callback)

    def path(self):
        return "/tmp/live.hip"


class _FakeOpNode(object):
    def __init__(self):
        self.added = []
        self.removed = []

    def addEventCallback(self, event_types, callback):
        self.added.append((event_types, callback))

    def removeEventCallback(self, event_types, callback):
        self.removed.append((event_types, callback))

    def path(self):
        return "/obj"


class _FakeChild(object):
    def __init__(self, path, type_name="geo"):
        self._path = path
        self._type_name = type_name

    def path(self):
        return self._path

    def type(self):
        return self

    def name(self):
        return self._type_name


class _InvalidChild(object):
    def path(self):
        raise RuntimeError("deleted child")

    def type(self):
        raise RuntimeError("deleted child")


class _FakePlaybar(object):
    def __init__(self):
        self.added = []
        self.removed = []

    def addEventCallback(self, callback):
        self.added.append(callback)

    def removeEventCallback(self, callback):
        self.removed.append(callback)


def _fake_hou():
    hip_after_save = object()
    child_created = object()
    child_deleted = object()
    frame_changed = object()
    hip_file = _FakeHipFile()
    obj = _FakeOpNode()
    playbar = _FakePlaybar()
    hou = types.SimpleNamespace(
        hipFile=hip_file,
        hipFileEventType=types.SimpleNamespace(AfterSave=hip_after_save),
        nodeEventType=types.SimpleNamespace(
            ChildCreated=child_created, ChildDeleted=child_deleted),
        playbarEvent=types.SimpleNamespace(FrameChanged=frame_changed),
        OpNode=_FakeOpNode,
        node=lambda path: obj,
        playbar=playbar,
        ui=types.SimpleNamespace(),
    )
    return hou, {
        "hip_after_save": hip_after_save,
        "child_created": child_created,
        "child_deleted": child_deleted,
        "frame_changed": frame_changed,
        "hip_file": hip_file,
        "obj": obj,
        "playbar": playbar,
    }


class CallbackTests(unittest.TestCase):
    def test_official_signatures_idempotence_and_symmetric_cleanup(self):
        hou, handles = _fake_hou()
        state = events.EventState(events.EventCollector(dedupe_window=0.0))
        state.subscribe()
        state.attach_callbacks(hou)
        state.attach_callbacks(hou)

        hip_callback = handles["hip_file"].added[0]
        node_event_types, node_callback = handles["obj"].added[0]
        playbar_callback = handles["playbar"].added[0]
        hip_callback(handles["hip_after_save"])
        node_callback(
            handles["obj"], handles["child_created"],
            child_node=_FakeChild("/obj/new_geo"))
        node_callback(
            handles["obj"], handles["child_deleted"],
            child_node=_InvalidChild(), child_path="/obj/deleted")
        playbar_callback(handles["frame_changed"], 24)

        result = state.drain(limit=10)
        self.assertEqual(
            [item["type"] for item in result["events"]],
            ["scene_saved", "node_created", "node_deleted", "frame_changed"])
        self.assertEqual(result["events"][2]["path"], "/obj/deleted")
        self.assertEqual(len(handles["hip_file"].added), 1)
        self.assertEqual(len(handles["obj"].added), 1)
        self.assertEqual(node_event_types, (
            handles["child_created"], handles["child_deleted"]))

        state.detach_callbacks()
        state.detach_callbacks()
        self.assertEqual(handles["hip_file"].removed, [hip_callback])
        self.assertEqual(handles["obj"].removed,
                         [(node_event_types, node_callback)])
        self.assertEqual(handles["playbar"].removed, [playbar_callback])
        self.assertFalse(state.attached)
        self.assertEqual(list(state.collector.buffer), [])

    def test_missing_api_or_enum_only_records_warning(self):
        state = events.EventState()
        obj = _FakeOpNode()
        hou = types.SimpleNamespace(
            hipFile=types.SimpleNamespace(),
            hipFileEventType=types.SimpleNamespace(),
            nodeEventType=types.SimpleNamespace(ChildCreated=object()),
            playbarEvent=types.SimpleNamespace(),
            OpNode=_FakeOpNode,
            node=lambda path: obj,
            playbar=types.SimpleNamespace(),
            ui=types.SimpleNamespace(),
        )
        state.attach_callbacks(hou)
        warning_types = {warning["event_type"] for warning in state.warnings}
        self.assertTrue({"scene_saved", "node_deleted", "frame_changed"}
                        <= warning_types)
        self.assertFalse(hasattr(hou.ui, "addEventCallback"))

    def test_delete_callback_does_not_require_child_object_access(self):
        hou, handles = _fake_hou()
        state = events.EventState(events.EventCollector(dedupe_window=0.0))
        state.subscribe()
        state.attach_callbacks(hou)
        _event_types, callback = handles["obj"].added[0]
        callback(handles["obj"], handles["child_deleted"],
                 child_path="/obj/expired")
        result = state.drain()
        self.assertEqual(result["events"][0]["path"], "/obj/expired")

    def test_detach_then_reattach_has_no_duplicate_or_dangling_callback(self):
        hou, handles = _fake_hou()
        state = events.EventState(events.EventCollector(dedupe_window=0.0))
        state.subscribe()
        state.attach_callbacks(hou)
        handles["hip_file"].added[0](handles["hip_after_save"])
        state.detach_callbacks()
        self.assertEqual(len(state.callback_handles), 0)
        self.assertEqual(len(state.collector.buffer), 0)

        state.attach_callbacks(hou)
        self.assertEqual(len(handles["hip_file"].added), 2)
        handles["hip_file"].added[1](handles["hip_after_save"])
        result = state.drain()
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(handles["hip_file"].removed), 1)

    def test_failed_remove_invalidates_old_listener_before_new_attach(self):
        hou, handles = _fake_hou()
        state = events.EventState(events.EventCollector(dedupe_window=0.0))
        state.subscribe(["scene_saved"])
        state.attach_callbacks(hou)
        old_callback = handles["hip_file"].added[0]

        handles["hip_file"].fail_remove = True
        state.detach_callbacks()
        state.subscribe(["scene_saved"])
        state.attach_callbacks(hou)

        old_callback(handles["hip_after_save"])
        self.assertEqual(len(state.collector.buffer), 0)
        new_callback = handles["hip_file"].added[-1]
        new_callback(handles["hip_after_save"])
        self.assertEqual(state.drain()["count"], 1)

    def test_node_callback_warning_uses_actual_event_name(self):
        hou, handles = _fake_hou()
        state = events.EventState(events.EventCollector(dedupe_window=0.0))
        state.subscribe()
        state.attach_callbacks(hou)
        _event_types, callback = handles["obj"].added[0]
        state.append = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("append failed"))

        callback(handles["obj"], handles["child_deleted"],
                 child_path="/obj/deleted")

        self.assertTrue(any(
            warning["event_type"] == "node_deleted"
            and warning["code"] == "callback_error"
            for warning in state.warnings))


class RegistryAndBridgeTests(unittest.TestCase):
    def test_three_event_server_commands_are_no_undo_only(self):
        instance = object.__new__(server_mod.HoudiniMCPServer)
        handlers = instance._get_command_handlers()
        event_commands = {
            "get_pending_events", "subscribe_events", "unsubscribe_events",
        }
        self.assertEqual(event_commands, event_commands & set(handlers))
        self.assertTrue(event_commands <= server_mod.HoudiniMCPServer.NO_UNDO_COMMANDS)
        self.assertFalse(event_commands & server_mod.HoudiniMCPServer.MUTATING_COMMANDS)
        self.assertFalse(event_commands & server_mod.HoudiniMCPServer.READ_ONLY_COMMANDS)
        self.assertNotIn("get_houdini_events", handlers)
        self.assertNotIn("subscribe_houdini_events", handlers)
        self.assertNotIn("unsubscribe_houdini_events", handlers)

    def test_event_commands_never_open_scene_undo(self):
        self.assertNotIn("get_pending_events",
                         server_mod.HoudiniMCPServer.MUTATING_COMMANDS)
        self.assertNotIn("subscribe_events",
                         server_mod.HoudiniMCPServer.MUTATING_COMMANDS)
        self.assertNotIn("unsubscribe_events",
                         server_mod.HoudiniMCPServer.MUTATING_COMMANDS)

    def test_bridge_event_tools_relay_mapped_commands(self):
        source_path = os.path.join(ROOT, "houdini_mcp_server.py")
        source = open(source_path, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        functions = {
            node.name: node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {
                "get_houdini_events", "subscribe_houdini_events",
                "unsubscribe_houdini_events",
            }
        }

        class FakeMCP(object):
            def tool(self):
                return lambda function: function

        class Relay(object):
            def __init__(self):
                self.calls = []

            def __call__(self, command, params=None):
                self.calls.append((command, params))
                return {"status": "success", "result": {}}

        expected = {
            "get_houdini_events": ("get_pending_events",
                                    {"limit": 7, "cursor": "c"}),
            "subscribe_houdini_events": ("subscribe_events",
                                          {"types": ["scene_saved"]}),
            "unsubscribe_houdini_events": ("unsubscribe_events",
                                            {"types": None}),
        }
        for name, (command, params) in expected.items():
            self.assertIn(name, functions)
            function_source = ast.get_source_segment(source, functions[name])
            relay = Relay()
            namespace = {"mcp": FakeMCP(), "_houdini_call": relay}
            exec(compile(function_source, "<event_bridge>", "exec"), namespace)
            if name == "get_houdini_events":
                namespace[name](object(), **params)
            else:
                namespace[name](object(), **params)
            self.assertEqual(relay.calls, [(command, params)])

    def test_event_module_keeps_forbidden_fallbacks_out(self):
        source = open(os.path.join(ROOT, "_events.py"),
                      "r", encoding="utf-8").read()
        self.assertNotIn("addPreSavePostSaveCallback", source)
        self.assertNotIn("addChildrenUpdatedCallback", source)
        self.assertNotIn("addChildCreatedCallback", source)
        self.assertNotIn("ui.addEventCallback", source)
        self.assertNotIn("import hou", source)


if __name__ == "__main__":
    unittest.main()
