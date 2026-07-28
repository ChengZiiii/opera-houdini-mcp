"""Batch dispatcher / undo segment / render preflight tests."""
import ast
import importlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_server_module():
    package_name = "batch_test_houdinimcp"
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

    for name in ("_common", "_render_policy"):
        full_name = package_name + "." + name
        path = os.path.join(ROOT, name + ".py")
        spec = importlib.util.spec_from_file_location(full_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)

    full_name = package_name + ".server"
    spec = importlib.util.spec_from_file_location(
        full_name, os.path.join(ROOT, "server.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


server_mod = _load_server_module()


class _UndoGroup(object):
    def __init__(self, owner, label):
        self.owner = owner
        self.label = label

    def __enter__(self):
        self.owner.events.append(("enter", self.label))
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.owner.events.append(("exit", self.label))
        return False


class _UndoApi(object):
    def __init__(self):
        self.events = []

    def group(self, label):
        self.events.append(("create", label))
        return _UndoGroup(self, label)


def _new_server(handlers):
    instance = server_mod.HoudiniMCPServer.__new__(
        server_mod.HoudiniMCPServer)
    instance._batch_active = False
    instance._get_command_handlers = lambda: handlers
    return instance


class RegistryTests(unittest.TestCase):
    def test_registry_is_complete_and_partitioned(self):
        original = getattr(server_mod.hou.session,
                           "houdinimcp_use_assetlib", False)
        try:
            server_mod.hou.session.houdinimcp_use_assetlib = False
            instance = object.__new__(server_mod.HoudiniMCPServer)
            handlers = instance._get_command_handlers()
            classified = (server_mod.HoudiniMCPServer.MUTATING_COMMANDS
                          | server_mod.HoudiniMCPServer.READ_ONLY_COMMANDS
                          | server_mod.HoudiniMCPServer.NO_UNDO_COMMANDS)
            self.assertTrue(set(handlers) - {"batch"} <= classified)
            self.assertFalse(
                server_mod.HoudiniMCPServer.MUTATING_COMMANDS
                & server_mod.HoudiniMCPServer.NO_UNDO_COMMANDS)
            self.assertNotIn("capture_pane_screenshot",
                             server_mod.HoudiniMCPServer.MUTATING_COMMANDS)

            server_mod.hou.session.houdinimcp_use_assetlib = True
            handlers_with_asset = instance._get_command_handlers()
            self.assertIn("get_asset_categories", handlers_with_asset)
            self.assertIn("search_assets", handlers_with_asset)
            self.assertIn("import_asset", handlers_with_asset)
            self.assertEqual(classified, set(handlers_with_asset) - {"batch"})
        finally:
            server_mod.hou.session.houdinimcp_use_assetlib = original

    def test_unclassified_future_handler_fails_closed(self):
        handlers = {"batch": object(), "future_command": object()}
        with self.assertRaises(AssertionError):
            server_mod.HoudiniMCPServer._validate_handler_classification(
                handlers)

    def test_render_registry_has_only_current_six_entries(self):
        self.assertEqual(
            set(server_mod.RENDER_POLICY_COMMANDS),
            {
                "render_single_view", "render_quad_view",
                "render_specific_camera", "render_viewport_base64",
                 "render_quad_views_base64", "render_specific_camera_base64",
            })

    def test_render_policy_defaults_only_fill_omitted_values(self):
        engine_adapter = server_mod.RENDER_POLICY_COMMANDS[
            "render_single_view"]
        renderer_adapter = server_mod.RENDER_POLICY_COMMANDS[
            "render_viewport_base64"]

        self.assertIn("_redirect", engine_adapter({}))
        self.assertIn("_redirect", renderer_adapter({}))
        self.assertIsNone(engine_adapter({
            "render_engine": None, "karma_engine": "cpu"}))
        self.assertIsNone(renderer_adapter({"renderer": None}))

    def test_registration_rejects_invalid_and_conflicting_entries(self):
        with self.assertRaises(ValueError):
            server_mod.register_render_policy_command("", lambda params: None)
        with self.assertRaises(TypeError):
            server_mod.register_render_policy_command("bad_adapter", None)

        command = "test_batch_policy_command"
        adapter = lambda params: {"_interrupt": "test"}
        try:
            server_mod.register_render_policy_command(command, adapter)
            self.assertIs(
                server_mod.register_render_policy_command(command, adapter),
                adapter)
            with self.assertRaises(ValueError):
                server_mod.register_render_policy_command(command, lambda p: None)
        finally:
            server_mod.RENDER_POLICY_COMMANDS.pop(command, None)


class BatchExecutionTests(unittest.TestCase):
    def setUp(self):
        self.original_undos = getattr(server_mod.hou, "undos", None)
        self.had_undos = hasattr(server_mod.hou, "undos")
        self.undo = _UndoApi()
        server_mod.hou.undos = self.undo

    def tearDown(self):
        if self.had_undos:
            server_mod.hou.undos = self.original_undos
        else:
            del server_mod.hou.undos

    def test_mutating_commands_share_segments_and_boundaries_close(self):
        calls = []

        def mutate(**params):
            calls.append("mutate")
            return {"ok": True}

        def read(**params):
            calls.append("read")
            return {"ok": True}

        def no_undo(**params):
            calls.append("no_undo")
            return {"ok": True}

        instance = _new_server({
            "create_node": mutate,
            "set_parameters": mutate,
            "ping": read,
            "save_scene": no_undo,
        })
        result = instance.batch([
            {"type": "create_node"},
            {"type": "set_parameters"},
            {"type": "ping"},
            {"type": "create_node"},
            {"type": "save_scene"},
        ])

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["requested"], 5)
        self.assertEqual(result["executed"], 5)
        self.assertEqual(result["succeeded"], 5)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(calls, ["mutate", "mutate", "read", "mutate", "no_undo"])
        self.assertEqual(
            self.undo.events,
            [("create", "MCP: batch"), ("enter", "MCP: batch"),
             ("exit", "MCP: batch"), ("create", "MCP: batch"),
             ("enter", "MCP: batch"), ("exit", "MCP: batch")])

    def test_control_response_stops_even_when_continue_is_true(self):
        calls = []

        def first(**params):
            calls.append("first")
            return {"ok": True}

        def control(**params):
            calls.append("control")
            return {"_redirect": "flipbook", "fallback_tool": "capture"}

        def never(**params):
            calls.append("never")
            return {"ok": True}

        instance = _new_server({
            "create_node": first,
            "ping": control,
            "save_scene": never,
        })
        result = instance.batch([
            {"type": "create_node"},
            {"type": "ping"},
            {"type": "save_scene"},
        ])

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["executed"], 2)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(calls, ["first", "control"])
        self.assertIn("_redirect", result["results"][1])
        self.assertEqual(result["results"][1]["response"]["_redirect"], "flipbook")

    def test_continue_and_stop_counts_normal_errors(self):
        calls = []

        def failed(**params):
            calls.append("failed")
            return {"status": "error", "error": "bad"}

        def succeeded(**params):
            calls.append("succeeded")
            return {"value": 1}

        handlers = {"ping": failed, "get_scene_info": succeeded}
        instance = _new_server(handlers)
        continued = instance.batch([
            {"type": "ping"}, {"type": "get_scene_info"}], True)
        self.assertEqual(continued["status"], "partial")
        self.assertEqual(continued["executed"], 2)
        self.assertEqual(continued["succeeded"], 1)
        self.assertEqual(continued["failed"], 1)

        calls[:] = []
        stopped = instance.batch([
            {"type": "ping"}, {"type": "get_scene_info"}], False)
        self.assertEqual(stopped["status"], "error")
        self.assertEqual(stopped["executed"], 1)
        self.assertEqual(stopped["succeeded"], 0)
        self.assertEqual(stopped["failed"], 1)
        self.assertEqual(calls, ["failed"])

    def test_classifier_covers_exception_error_redirect_and_interrupt(self):
        self.assertEqual(
            server_mod.HoudiniMCPServer._classify_handler_result({
                "status": "error"}), "error")
        self.assertEqual(
            server_mod.HoudiniMCPServer._classify_handler_result({
                "error": "failed"}), "error")
        self.assertEqual(
            server_mod.HoudiniMCPServer._classify_handler_result({
                "_redirect": "flipbook"}), "redirect")
        self.assertEqual(
            server_mod.HoudiniMCPServer._classify_handler_result({
                "_interrupt": "consent"}), "interrupt")

        def raises(**params):
            raise RuntimeError("boom")

        instance = _new_server({"ping": raises})
        result = instance.batch([{"type": "ping"}])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["results"][0]["response"]["exception"],
                         "RuntimeError")

    def test_structure_unknown_nested_and_limit_execute_nothing(self):
        calls = []
        instance = _new_server({
            "ping": lambda **params: calls.append("ping") or {"ok": True},
        })
        unknown = instance.batch([
            {"type": "ping"}, {"type": "unknown"}])
        self.assertEqual(unknown["status"], "error")
        self.assertEqual(unknown["executed"], 0)
        self.assertEqual(calls, [])

        nested = instance.batch([{"type": "batch", "params": {}}])
        self.assertEqual(nested["status"], "error")
        self.assertEqual(nested["executed"], 0)

        original = os.environ.get("HOUDINI_MCP_BATCH_MAX_OPERATIONS")
        os.environ["HOUDINI_MCP_BATCH_MAX_OPERATIONS"] = "1"
        try:
            limited = instance.batch([{"type": "ping"}, {"type": "ping"}])
        finally:
            if original is None:
                os.environ.pop("HOUDINI_MCP_BATCH_MAX_OPERATIONS", None)
            else:
                os.environ["HOUDINI_MCP_BATCH_MAX_OPERATIONS"] = original
        self.assertEqual(limited["status"], "error")
        self.assertEqual(limited["executed"], 0)
        self.assertEqual(calls, [])

    def test_indirect_nested_batch_is_rejected(self):
        instance = object.__new__(server_mod.HoudiniMCPServer)
        instance._batch_active = False

        def nested(**params):
            return instance.batch([{"type": "ping"}])

        instance._get_command_handlers = lambda: {
            "ping": nested,
        }
        result = instance.batch([{"type": "ping"}])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed"], 1)
        self.assertIn("nested batch", result["results"][0]["response"]["error"])

    def test_render_policy_preflight_blocks_prior_mutation(self):
        calls = []
        instance = _new_server({
            "create_node": lambda **params: calls.append("mutation"),
            "render_single_view": lambda **params: calls.append("render"),
        })
        result = instance.batch([
            {"type": "create_node"},
            {"type": "render_single_view",
             "params": {"render_engine": "opengl", "karma_engine": "cpu"}},
        ])
        self.assertIn("_redirect", result)
        self.assertEqual(result["operation_index"], 1)
        self.assertEqual(result["operation_type"], "render_single_view")
        self.assertEqual(calls, [])

    def test_server_omitted_params_block_all_six_with_batch_envelope(self):
        render_commands = [
            "render_single_view", "render_quad_view",
            "render_specific_camera", "render_viewport_base64",
            "render_quad_views_base64", "render_specific_camera_base64",
        ]
        calls = []
        handlers = {"create_node": lambda **params: calls.append("mutation")}
        for command in render_commands:
            handlers[command] = lambda **params: calls.append("render")
        instance = _new_server(handlers)
        envelope_keys = {
            "status", "requested", "executed", "succeeded", "failed",
            "results", "operation_index", "operation_type",
        }

        for command in render_commands:
            result = instance.batch([
                {"type": "create_node"},
                {"type": command},
            ])
            self.assertIn("_redirect", result)
            self.assertTrue(envelope_keys <= set(result))
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["requested"], 2)
            self.assertEqual(result["executed"], 0)
            self.assertEqual(result["succeeded"], 0)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["results"], [])
            self.assertEqual(result["operation_index"], 1)
            self.assertEqual(result["operation_type"], command)
        self.assertEqual(calls, [])

    def test_direct_tcp_preflight_blocks_all_six_render_handlers(self):
        render_commands = [
            ("render_single_view", {"render_engine": "opengl"}),
            ("render_quad_view", {"render_engine": "opengl"}),
            ("render_specific_camera", {"render_engine": "opengl"}),
            ("render_viewport_base64", {"renderer": "opengl"}),
            ("render_quad_views_base64", {"renderer": "opengl"}),
            ("render_specific_camera_base64", {"renderer": "opengl"}),
        ]
        calls = []
        handlers = {"create_node": lambda **params: calls.append("mutation")}
        for command, _params in render_commands:
            handlers[command] = lambda **params: calls.append(command)
        instance = _new_server(handlers)
        for command, params in render_commands:
            result = instance.batch([
                {"type": "create_node"},
                {"type": command, "params": params},
            ])
            self.assertEqual(result.get("operation_type"), command)
            self.assertIn("_redirect", result)
        self.assertEqual(calls, [])

    def test_batch_response_is_capped(self):
        instance = _new_server({
            "ping": lambda **params: {"blob": "x" * 20000},
        })
        result = instance.batch([{"type": "ping"}] * 10)
        self.assertTrue(result.get("_truncated"))


class BridgeBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        cls.bridge = importlib.import_module("houdini_mcp_server")

    def test_blocked_preflight_does_not_connect(self):
        bridge = self.bridge
        original = bridge.get_houdini_connection
        calls = []

        def unexpected_connection():
            calls.append("connect")
            raise AssertionError("blocked batch must not connect")

        bridge.get_houdini_connection = unexpected_connection
        try:
            result = bridge.batch(
                None,
                [{"type": "create_node"},
                 {"type": "render_single_view",
                  "params": {"render_engine": "opengl"}}])
        finally:
            bridge.get_houdini_connection = original
        self.assertIn("_redirect", result)
        self.assertEqual(result["operation_index"], 1)
        self.assertEqual(calls, [])

    def test_allow_path_relays_one_batch_command(self):
        bridge = self.bridge

        class Connection(object):
            def __init__(self):
                self.calls = []

            def send_command(self, command, params):
                self.calls.append((command, params))
                return {
                    "status": "success",
                    "result": {
                        "status": "success", "requested": 1,
                        "executed": 1, "succeeded": 1, "failed": 0,
                        "results": [{"status": "success"}],
                    },
                }

        connection = Connection()
        original = bridge.get_houdini_connection
        bridge.get_houdini_connection = lambda: connection
        try:
            result = bridge.batch(
                None, [{"type": "ping", "params": {}}], False)
        finally:
            bridge.get_houdini_connection = original
        self.assertEqual(len(connection.calls), 1)
        self.assertEqual(connection.calls[0][0], "batch")
        self.assertEqual(result["status"], "success")

    def test_all_six_render_entries_block_before_relay(self):
        bridge = self.bridge

        class Connection(object):
            def __init__(self):
                self.calls = []

            def send_command(self, command, params):
                self.calls.append((command, params))
                return {"status": "success", "result": {}}

        render_operations = [
            ("render_single_view", {"render_engine": "opengl"}),
            ("render_quad_view", {"render_engine": "opengl"}),
            ("render_specific_camera", {"render_engine": "opengl"}),
            ("render_viewport_base64", {"renderer": "opengl"}),
            ("render_quad_views_base64", {"renderer": "opengl"}),
            ("render_specific_camera_base64", {"renderer": "opengl"}),
        ]
        connection = Connection()
        original = bridge.get_houdini_connection
        bridge.get_houdini_connection = lambda: connection
        try:
            for command, params in render_operations:
                result = bridge.batch(
                    None, [{"type": "create_node"},
                           {"type": command, "params": params}])
                self.assertEqual(result.get("operation_type"), command)
                self.assertIn("_redirect", result)
                self.assertEqual(connection.calls, [])
        finally:
            bridge.get_houdini_connection = original

    def test_bridge_omitted_params_block_all_six_with_batch_envelope(self):
        bridge = self.bridge
        render_commands = [
            "render_single_view", "render_quad_view",
            "render_specific_camera", "render_viewport_base64",
            "render_quad_views_base64", "render_specific_camera_base64",
        ]

        class Connection(object):
            def __init__(self):
                self.calls = []

            def send_command(self, command, params):
                self.calls.append((command, params))
                return {"status": "success", "result": {}}

        connection = Connection()
        original = bridge.get_houdini_connection
        bridge.get_houdini_connection = lambda: connection
        envelope_keys = {
            "status", "requested", "executed", "succeeded", "failed",
            "results", "operation_index", "operation_type",
        }
        try:
            for command in render_commands:
                result = bridge.batch(
                    None, [{"type": "create_node"}, {"type": command}])
                self.assertIn("_redirect", result)
                self.assertTrue(envelope_keys <= set(result))
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["requested"], 2)
                self.assertEqual(result["executed"], 0)
                self.assertEqual(result["succeeded"], 0)
                self.assertEqual(result["failed"], 0)
                self.assertEqual(result["results"], [])
                self.assertEqual(result["operation_index"], 1)
                self.assertEqual(result["operation_type"], command)
                self.assertEqual(connection.calls, [])
        finally:
            bridge.get_houdini_connection = original

    def test_karma_missing_invalid_and_expired_tokens_stay_blocked(self):
        bridge = self.bridge
        original_env_dir = bridge._rp._env_dir
        temporary = tempfile.mkdtemp(prefix="batch_policy_live_")
        bridge._rp._env_dir = lambda: temporary
        original_connection = bridge.get_houdini_connection

        class Connection(object):
            def __init__(self):
                self.calls = []

            def send_command(self, command, params):
                self.calls.append((command, params))
                return {"status": "success", "result": {}}

        connection = Connection()
        bridge.get_houdini_connection = lambda: connection
        try:
            missing = bridge.batch(
                None, [{"type": "render_viewport_base64",
                        "params": {"renderer": "karma_cpu"}}])
            invalid = bridge.batch(
                None, [{"type": "render_viewport_base64",
                        "params": {"renderer": "karma_cpu",
                                   "consent_token": "invalid"}}])
            action, payload = bridge._rp.enforce_render_policy("karma_cpu")
            token_path = os.path.join(
                bridge._rp._consent_dir(), payload["consent_token"])
            with open(token_path, "w", encoding="utf-8") as handle:
                handle.write('{"created_at": 0}')
            expired = bridge.batch(
                None, [{"type": "render_viewport_base64",
                        "params": {"renderer": "karma_cpu",
                                   "consent_token": payload["consent_token"]}}])
        finally:
            bridge.get_houdini_connection = original_connection
            bridge._rp._env_dir = original_env_dir
            try:
                import shutil
                shutil.rmtree(temporary)
            except OSError:
                pass
        for result in (missing, invalid, expired):
            self.assertEqual(result.get("_interrupt"), "user_consent_required")
        self.assertEqual(connection.calls, [])

    def test_registry_extension_is_used_by_batch_preflight(self):
        bridge = self.bridge
        command = "test_batch_registered_render"
        adapter = lambda params: {"_redirect": "test-policy"}
        original = bridge.get_houdini_connection
        bridge.get_houdini_connection = lambda: self.fail(
            "registered blocked render must not connect")
        try:
            bridge.register_render_policy_command(command, adapter)
            result = bridge.batch(None, [{"type": command, "params": {}}])
        finally:
            bridge.RENDER_POLICY_COMMANDS.pop(command, None)
            bridge.get_houdini_connection = original
        self.assertEqual(result.get("_redirect"), "test-policy")
        self.assertEqual(result.get("operation_type"), command)

    def test_no_undo_classification_covers_capture_flipbook_and_render(self):
        no_undo = server_mod.HoudiniMCPServer.NO_UNDO_COMMANDS
        expected = {
            "capture_pane_screenshot", "capture_multiple_panes",
            "capture_sceneviewer_flipbook_views", "render_node_network",
            "render_single_view", "render_quad_view",
            "render_specific_camera", "render_viewport_base64",
            "render_quad_views_base64", "render_specific_camera_base64",
        }
        self.assertTrue(expected <= no_undo)
        self.assertFalse(expected & server_mod.HoudiniMCPServer.MUTATING_COMMANDS)

    def test_batch_docstring_states_segment_and_no_rollback_contract(self):
        doc = server_mod.HoudiniMCPServer.batch.__doc__ or ""
        self.assertIn("不回滚", doc)
        self.assertIn("render policy", self.bridge.batch.__doc__ or "")


class StaticPolicyTests(unittest.TestCase):
    def test_batch_method_does_not_use_python_exec_or_eval(self):
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
