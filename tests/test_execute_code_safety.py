"""Unit tests for PR 4 execute_code safety layer.

Covers:
- _common.validate_policy / _bypass_config_enabled / check_execute_code_policy / _build_audit
- _common.serialize_scene_state placeholder (mock hou)
- _common._run_code_thread 正常 / 异常 / 超时 三种情况
- bridge get_last_scene_diff round-trip (mock get_houdini_connection)

Stdlib unittest, no hython required. hou is mocked via a tiny stub class.
Run with:
    python -m unittest tests.test_execute_code_safety -v
"""
import io
import json
import os
import sys
import time
import types
import unittest
import importlib.util as _ilu

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Load _common.py directly as a top-level module (matches tests/test_common.py)
_spec = _ilu.spec_from_file_location("_common", os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec)
sys.modules["_common"] = common
_spec.loader.exec_module(common)
cmn = common  # short alias


# ---------------------------------------------------------------------------
# hou stub: minimal attribute bag for serialize_scene_state placeholder.
# Uses real classes so isinstance(value, hou.EnumValue) etc. would work.
# ---------------------------------------------------------------------------
class _FakeVector(list):
    pass


class _FakeColor(list):
    pass


class _FakeEnumValue(str):
    pass


class _FakeRamp(object):
    def __init__(self, points=None):
        self.points = points or []


class _FakeNode(object):
    """A minimal hou.Node stand-in for serialize_scene_state tests."""

    def __init__(self, name, type_name="geo", children=None):
        self._name = name
        self._type = type_name
        self._children = children or []

    def name(self):
        return self._name

    def type(self):
        t = types.SimpleNamespace()
        t.name = lambda: self._type
        return t

    def children(self):
        return list(self._children)

    def path(self):
        return "/obj/" + self._name


class _FakeHou(object):
    Vector = _FakeVector
    Color = _FakeColor
    EnumValue = _FakeEnumValue
    Ramp = _FakeRamp

    def __init__(self):
        root_child = _FakeNode("geo1", children=[
            _FakeNode("grid1"),
            _FakeNode("xform1", type_name="xform"),
        ])
        self._obj = _FakeNode("obj", children=[root_child])

    def node(self, path):
        if path == "/":
            return self._obj
        if path == "/obj":
            return self._obj
        if path == "/obj/geo1":
            return self._obj.children()[0]
        return None


# ===========================================================================
# Section A: validate_policy
# ===========================================================================
class ValidatePolicyTests(unittest.TestCase):
    def test_read_only_accepted(self):
        self.assertEqual(cmn.validate_policy("read-only"), "read-only")

    def test_normal_accepted(self):
        self.assertEqual(cmn.validate_policy("normal"), "normal")

    def test_privileged_accepted(self):
        self.assertEqual(cmn.validate_policy("privileged"), "privileged")

    def test_case_insensitive(self):
        self.assertEqual(cmn.validate_policy("Normal"), "normal")
        self.assertEqual(cmn.validate_policy("PRIVILEGED"), "privileged")
        self.assertEqual(cmn.validate_policy("Read-Only"), "read-only")

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            cmn.validate_policy("super")
        with self.assertRaises(ValueError):
            cmn.validate_policy("")
        with self.assertRaises(ValueError):
            cmn.validate_policy("read-only-extreme")


# ===========================================================================
# Section B: _bypass_config_enabled
# ===========================================================================
class BypassConfigEnabledTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("HOUDINI_MCP_ALLOW_BYPASS")
        os.environ.pop("HOUDINI_MCP_ALLOW_BYPASS", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("HOUDINI_MCP_ALLOW_BYPASS", None)
        else:
            os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = self._saved

    def test_unset_returns_false(self):
        self.assertFalse(cmn._bypass_config_enabled())

    def test_zero_returns_false(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "0"
        self.assertFalse(cmn._bypass_config_enabled())

    def test_false_returns_false(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "false"
        self.assertFalse(cmn._bypass_config_enabled())

    def test_one_returns_true(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "1"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_true_returns_true(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "true"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_yes_returns_true(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "yes"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_on_returns_true(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "on"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_case_insensitive(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "TRUE"
        self.assertTrue(cmn._bypass_config_enabled())
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "Yes"
        self.assertTrue(cmn._bypass_config_enabled())

    def test_random_string_returns_false(self):
        os.environ["HOUDINI_MCP_ALLOW_BYPASS"] = "maybe"
        self.assertFalse(cmn._bypass_config_enabled())


# ===========================================================================
# Section C: check_execute_code_policy — 7 combo cases
# ===========================================================================
class CheckExecuteCodePolicyTests(unittest.TestCase):
    def _safe_code(self):
        return "x = 1 + 1\nprint(x)"

    def _mutation_code(self):
        return "hou.node('/obj/geo1').destroy()"

    def _dangerous_code(self):
        return "subprocess.run(['ls'])"

    def _heavy_code(self):
        return "geo = hou.node('/obj/geo1').geometry()"

    def _import_hou_code(self):
        return "import hou\nhou.node('/obj')"

    # ---- read-only: any mutation rejected ----
    def test_read_only_rejects_mutation(self):
        r = cmn.check_execute_code_policy(self._mutation_code(), "read-only",
                                           False, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("mutation", r["reason"])
        self.assertIn("destroy", r["hits"]["mutation"][0])

    # ---- normal + dangerous code + no allow ----
    def test_normal_rejects_dangerous_without_allow(self):
        r = cmn.check_execute_code_policy(self._dangerous_code(), "normal",
                                           False, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("dangerous", r["reason"])
        self.assertTrue(len(r["hits"]["dangerous"]) >= 1)

    # ---- normal + heavy code + no allow ----
    def test_normal_rejects_heavy_without_allow(self):
        r = cmn.check_execute_code_policy(self._heavy_code(), "normal",
                                           False, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("heavy", r["reason"])
        self.assertTrue(len(r["hits"]["heavy"]) >= 1)

    # ---- normal + safe code ----
    def test_normal_accepts_safe_code(self):
        r = cmn.check_execute_code_policy(self._safe_code(), "normal",
                                           False, False, False)
        self.assertTrue(r["allowed"], r)

    # ---- privileged + dangerous + allow + bypass OFF ----
    def test_privileged_dangerous_requires_bypass(self):
        r = cmn.check_execute_code_policy(self._dangerous_code(), "privileged",
                                           True, False, False)
        self.assertFalse(r["allowed"])
        self.assertIn("bypass", r["reason"].lower())

    # ---- privileged + dangerous + allow + bypass ON ----
    def test_privileged_dangerous_with_bypass_allowed(self):
        r = cmn.check_execute_code_policy(self._dangerous_code(), "privileged",
                                           True, False, True)
        self.assertTrue(r["allowed"], r)

    # ---- import hou detection ----
    def test_import_hou_flagged(self):
        r = cmn.check_execute_code_policy(self._import_hou_code(), "normal",
                                           False, False, False)
        # The code is safe in terms of dangerous/mutation, but import hou is
        # detected. In normal policy this should be allowed but flagged in hits.
        self.assertTrue(r["hits"]["import_hou"])
        # read-only + import hou should be rejected
        r2 = cmn.check_execute_code_policy(self._import_hou_code(), "read-only",
                                            False, False, False)
        self.assertFalse(r2["allowed"])
        self.assertTrue(r2["hits"]["import_hou"])


# ===========================================================================
# Section D: _build_audit
# ===========================================================================
class BuildAuditTests(unittest.TestCase):
    def test_minimal_dict_has_required_fields(self):
        audit = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=12,
            undo_group=None,
        )
        self.assertEqual(audit["policy"], "normal")
        self.assertFalse(audit["bypass_used"])
        self.assertEqual(audit["elapsed_ms"], 12)
        # empty hits fields should be omitted, not None
        self.assertNotIn("dangerous_hits", audit)
        self.assertNotIn("heavy_hits", audit)
        self.assertNotIn("mutation_hits", audit)

    def test_with_hits_includes_fields(self):
        audit = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=["subprocess 启动子进程"],
            heavy_hits=[],
            mutation_hits=[".destroy() 删除节点"],
            elapsed_ms=42,
            undo_group="MCP: execute_code",
        )
        self.assertEqual(audit["dangerous_hits"], ["subprocess 启动子进程"])
        self.assertNotIn("heavy_hits", audit)
        self.assertEqual(audit["mutation_hits"], [".destroy() 删除节点"])
        self.assertEqual(audit["undo_group"], "MCP: execute_code")

    def test_exception_recorded(self):
        audit = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=5,
            undo_group=None,
            exception_type="ValueError",
            exception_message="bad code",
        )
        self.assertEqual(audit["exception_type"], "ValueError")
        self.assertEqual(audit["exception_message"], "bad code")

    def test_timed_out_recorded(self):
        audit = cmn._build_audit(
            policy="normal",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=30000,
            undo_group=None,
            timed_out=True,
        )
        self.assertTrue(audit["timed_out"])

    def test_undo_group_none_omitted(self):
        audit = cmn._build_audit(
            policy="read-only",
            bypass_used=False,
            dangerous_hits=[],
            heavy_hits=[],
            mutation_hits=[],
            elapsed_ms=1,
            undo_group=None,
        )
        self.assertNotIn("undo_group", audit)


# ===========================================================================
# Section E: serialize_scene_state placeholder
# ===========================================================================
class SerializeSceneStateTests(unittest.TestCase):
    def test_returns_dict(self):
        hou = _FakeHou()
        result = cmn.serialize_scene_state(hou, root_path="/")
        self.assertIsInstance(result, dict)

    def test_returns_non_empty(self):
        hou = _FakeHou()
        result = cmn.serialize_scene_state(hou, root_path="/")
        self.assertTrue(len(result) > 0)

    def test_default_root_path(self):
        hou = _FakeHou()
        # no root_path arg → must still work
        result = cmn.serialize_scene_state(hou)
        self.assertIsInstance(result, dict)

    def test_nonexistent_path_returns_empty(self):
        hou = _FakeHou()
        result = cmn.serialize_scene_state(hou, root_path="/nope")
        # placeholder may return {} for missing path or error marker
        self.assertIsInstance(result, dict)


# ===========================================================================
# Section F: _run_code_thread — 正常 / 异常 / 超时 / 安全拒绝
# ===========================================================================
class RunCodeThreadTests(unittest.TestCase):
    def test_normal_code_captures_stdout(self):
        ns = {"hou": _FakeHou(), "x": 0}
        result = cmn._run_code_thread(
            "x = 1 + 2\nprint('hello', x)", ns, timeout=5
        )
        self.assertIn("stdout", result)
        self.assertIn("hello 3", result["stdout"])
        self.assertFalse(result.get("timed_out", False))
        self.assertIsNone(result.get("exception_type"))
        self.assertGreaterEqual(result.get("elapsed_ms", 0), 0)

    def test_exception_recorded(self):
        ns = {"hou": _FakeHou()}
        result = cmn._run_code_thread(
            "raise ValueError('boom')", ns, timeout=5
        )
        self.assertIsNotNone(result.get("exception_type"))
        self.assertEqual(result["exception_type"], "ValueError")
        self.assertIn("boom", result.get("exception_message", ""))

    def test_timeout_marks_timed_out(self):
        ns = {"hou": _FakeHou()}
        start = time.time()
        result = cmn._run_code_thread(
            "import time; time.sleep(5)", ns, timeout=1
        )
        elapsed_real = time.time() - start
        # Must return within ~ timeout + small slack, not wait full sleep
        self.assertLess(elapsed_real, 4.0)
        self.assertTrue(result.get("timed_out", False))
        self.assertGreaterEqual(result.get("elapsed_ms", 0), 900)

    def test_timeout_does_not_block_forever(self):
        ns = {"hou": _FakeHou()}
        result = cmn._run_code_thread(
            "import time; time.sleep(10)", ns, timeout=0.5
        )
        # daemon thread may still be alive; we must not block on it
        self.assertTrue(result.get("timed_out", False))

    def test_exception_in_redirected_stdout_still_recorded(self):
        ns = {"hou": _FakeHou()}
        result = cmn._run_code_thread(
            "print('before'); raise RuntimeError('nope')", ns, timeout=5
        )
        self.assertEqual(result["exception_type"], "RuntimeError")
        # stdout before raise should still be captured
        self.assertIn("before", result.get("stdout", ""))


# ===========================================================================
# Section G: bridge get_last_scene_diff round-trip
# ===========================================================================
class BridgeGetLastSceneDiffTests(unittest.TestCase):
    """Mock get_houdini_connection to verify send_command is called with the
    right cmd_type. We stub the mcp module before importing the bridge."""

    @classmethod
    def setUpClass(cls):
        # Stub mcp.server.fastmcp so houdini_mcp_server can be imported
        # without the real mcp package installed.
        mcp_pkg = types.ModuleType("mcp")
        server_pkg = types.ModuleType("mcp.server")
        fastmcp_mod = types.ModuleType("mcp.server.fastmcp")

        class _FakeFastMCP(object):
            def __init__(self, *args, **kwargs):
                self.lifespan = None

            def tool(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        fastmcp_mod.FastMCP = _FakeFastMCP

        class _FakeContext(object):
            pass

        fastmcp_mod.Context = _FakeContext
        sys.modules["mcp"] = mcp_pkg
        sys.modules["mcp.server"] = server_pkg
        sys.modules["mcp.server.fastmcp"] = fastmcp_mod

        # Stub langchain imports the bridge does defensively
        for name in ("langchain_classic", "langchain_classic.output_parsers",
                     "langchain", "langchain.output_parsers"):
            mod = types.ModuleType(name)
            sys.modules[name] = mod

        # Bridge module imports HoudiniMCPRender if it's a sibling in the
        # houdinimcp package — we want to avoid that. Import as a flat file
        # and set up the module's globals minimally.
        bridge_path = os.path.join(ROOT, "houdini_mcp_server.py")
        spec = _ilu.spec_from_file_location(
            "houdini_mcp_server_under_test", bridge_path
        )
        cls.bridge = _ilu.module_from_spec(spec)
        spec.loader.exec_module(cls.bridge)

    def _make_mock_conn(self):
        """Build a mock HoudiniConnection whose send_command records calls.

        Mock returns the real server.py:604 get_last_scene_diff shape
        ({available, changed, before, after}) so the test catches
        field-name mismatches between bridge and server.
        """
        sent = []

        class MockConn(object):
            def send_command(self, cmd_type, params=None):
                sent.append((cmd_type, params))
                return {
                    "status": "success",
                    "result": {
                        "available": True,
                        "changed": True,
                        "before": {"nodes": ["obj1"]},
                        "after": {"nodes": ["obj1", "obj2"]},
                    },
                }

            def disconnect(self):
                pass

        return MockConn(), sent

    def test_get_last_scene_diff_calls_send_command(self):
        conn, sent = self._make_mock_conn()
        self.bridge.get_houdini_connection = lambda: conn
        result = self.bridge.get_last_scene_diff(None)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "get_last_scene_diff")
        self.assertEqual(sent[0][1], {})

    def test_get_last_scene_diff_returns_string(self):
        conn, _ = self._make_mock_conn()
        self.bridge.get_houdini_connection = lambda: conn
        result = self.bridge.get_last_scene_diff(None)
        self.assertIsInstance(result, str)

    def test_get_last_scene_diff_output_contains_before_and_after(self):
        """Regression: bridge must serialize real {available, changed, before, after}.

        Prior bug (C1 in PR 4 review): bridge read result.get('diff', {}) which
        server never emits, so agent always saw '{}'. With mock returning the
        real server shape, bridge output must be non-empty and contain the
        before / after keys.
        """
        conn, _ = self._make_mock_conn()
        self.bridge.get_houdini_connection = lambda: conn
        result = self.bridge.get_last_scene_diff(None)
        # Must not be the placeholder '{}' from the old implementation
        self.assertNotEqual(result.strip(), "{}")
        # Must contain the real server keys
        self.assertIn("before", result)
        self.assertIn("after", result)
        # And the stubbed scene payload
        self.assertIn("obj1", result)
        self.assertIn("obj2", result)

    def test_get_last_scene_diff_handles_status_error(self):
        class MockConn(object):
            def send_command(self, cmd_type, params=None):
                return {"status": "error", "message": "no scene yet",
                        "origin": "houdini"}

            def disconnect(self):
                pass

        self.bridge.get_houdini_connection = lambda: MockConn()
        result = self.bridge.get_last_scene_diff(None)
        self.assertIn("no scene yet", result)


if __name__ == "__main__":
    unittest.main()