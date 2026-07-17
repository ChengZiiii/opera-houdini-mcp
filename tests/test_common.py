"""Unit tests for external/houdinimcp/_common.py.

Stdlib unittest, no hython required. hou is mocked via a tiny stub class.
Run with:
    python -m unittest tests.test_common -v
"""
import os
import sys
import json
import ast
import unittest
import importlib
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Load _common.py directly as a top-level module so we don't pull in
# houdinimcp/__init__.py (which imports hou and is unavailable outside Houdini).
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("_common", os.path.join(ROOT, "_common.py"))
common = _ilu.module_from_spec(_spec)
sys.modules["_common"] = common
_spec.loader.exec_module(common)
cmn = common  # short alias


# ---------------------------------------------------------------------------
# hou stub: minimal attribute bag so _json_safe_hou_value can branch on type.
# Real classes are used so `isinstance(value, hou.Vector)` works.
# ---------------------------------------------------------------------------
class _FakeVector(list):
    """hou.Vector stand-in: behaves like a sequence of floats."""


class _FakeColor(list):
    """hou.Color stand-in: behaves like a sequence of floats (RGBA)."""


class _FakeEnumValue(str):
    """hou.EnumValue stand-in: str subclass carrying a menu token."""


class _FakeRamp(object):
    """hou.Ramp stand-in: exposes a .points iterable."""

    def __init__(self, points=None):
        self.points = points or []


class _FakeHou(object):
    Vector = _FakeVector
    Color = _FakeColor
    EnumValue = _FakeEnumValue
    Ramp = _FakeRamp


# ===========================================================================
# Section A: connection-error handling
# ===========================================================================
class HandleConnectionErrorsTests(unittest.TestCase):
    def test_returns_dict_on_connection_error(self):
        @cmn.handle_connection_errors("ping")
        def boom():
            raise ConnectionError("bridge down")
        result = boom()
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)
        self.assertIn("ping", result["error"])
        self.assertIn("bridge down", result["error"])

    def test_returns_dict_on_socket_timeout(self):
        @cmn.handle_connection_errors("download")
        def boom():
            raise socket_timeout()
        result = boom()
        self.assertIsInstance(result, dict)
        self.assertIn("download", result["error"])

    def test_returns_dict_on_oserror(self):
        @cmn.handle_connection_errors("open_port")
        def boom():
            raise OSError("address in use")
        result = boom()
        self.assertIsInstance(result, dict)
        self.assertIn("open_port", result["error"])

    def test_non_connection_errors_propagate(self):
        @cmn.handle_connection_errors("compute")
        def boom():
            raise ValueError("bad input")
        with self.assertRaises(ValueError):
            boom()

    def test_preserves_signature_with_functools_wraps(self):
        import functools

        @cmn.handle_connection_errors("op")
        def add(a, b):
            """add two numbers"""
            return a + b

        self.assertEqual(add.__name__, "add")
        self.assertEqual(add.__doc__, "add two numbers")
        # functools.wraps copies __wrapped__ so we can call the original
        wrapped = getattr(add, "__wrapped__", None)
        self.assertIsNotNone(wrapped)
        self.assertEqual(wrapped(2, 3), 5)
        # Sanity: decorator is functools.wraps-based
        self.assertTrue(hasattr(add, "__wrapped__"))

    def test_successful_call_returns_value_unchanged(self):
        @cmn.handle_connection_errors("ok")
        def good():
            return {"ok": True}
        self.assertEqual(good(), {"ok": True})


def socket_timeout():
    import socket
    return socket.timeout("timed out")


# ===========================================================================
# Section B: validate_resolution
# ===========================================================================
class ValidateResolutionTests(unittest.TestCase):
    def test_min_minus_one_raises(self):
        with self.assertRaises(ValueError):
            cmn.validate_resolution(63)

    def test_min_is_accepted(self):
        self.assertEqual(cmn.validate_resolution(64), 64)

    def test_max_is_accepted(self):
        self.assertEqual(cmn.validate_resolution(4096), 4096)

    def test_max_plus_one_raises(self):
        with self.assertRaises(ValueError):
            cmn.validate_resolution(4097)

    def test_non_integer_raises(self):
        with self.assertRaises(ValueError):
            cmn.validate_resolution(128.5)
        with self.assertRaises(ValueError):
            cmn.validate_resolution("256")

    def test_custom_bounds(self):
        self.assertEqual(cmn.validate_resolution(100, min_size=64, max_size=512), 100)
        with self.assertRaises(ValueError):
            cmn.validate_resolution(50, min_size=64, max_size=512)


# ===========================================================================
# Section C: DANGEROUS_PATTERNS / HEAVY_GEOMETRY_PATTERNS / MUTATION_PATTERNS
# ===========================================================================
class PatternTableTests(unittest.TestCase):
    def test_dangerous_patterns_is_list_of_tuples(self):
        self.assertIsInstance(cmn.DANGEROUS_PATTERNS, list)
        self.assertGreaterEqual(len(cmn.DANGEROUS_PATTERNS), 25)
        for entry in cmn.DANGEROUS_PATTERNS:
            self.assertEqual(len(entry), 2)
            pat, desc = entry
            self.assertIsInstance(desc, str)
            self.assertTrue(len(desc) > 0)

    def test_heavy_geometry_patterns_present(self):
        self.assertIsInstance(cmn.HEAVY_GEOMETRY_PATTERNS, list)
        self.assertGreaterEqual(len(cmn.HEAVY_GEOMETRY_PATTERNS), 6)

    def test_mutation_patterns_present(self):
        self.assertIsInstance(cmn.MUTATION_PATTERNS, list)
        self.assertGreaterEqual(len(cmn.MUTATION_PATTERNS), 15)

    def test_dangerous_patterns_compile_as_regex(self):
        for pat, _ in cmn.DANGEROUS_PATTERNS:
            import re
            re.compile(pat)  # raises if invalid

    def test_heavy_geometry_patterns_compile_as_regex(self):
        import re
        for pat, _ in cmn.HEAVY_GEOMETRY_PATTERNS:
            re.compile(pat)

    def test_mutation_patterns_compile_as_regex(self):
        import re
        for pat, _ in cmn.MUTATION_PATTERNS:
            re.compile(pat)


# ===========================================================================
# Section D: _detect_dangerous_code (regex + AST aliases)
# ===========================================================================
class DetectDangerousCodeTests(unittest.TestCase):
    def test_detects_subprocess_run(self):
        hits = cmn._detect_dangerous_code("subprocess.run(['ls'])")
        self.assertTrue(any("subprocess" in h.lower() for h in hits), hits)

    def test_detects_os_system(self):
        hits = cmn._detect_dangerous_code("os.system('rm -rf /')")
        self.assertTrue(any("os" in h.lower() for h in hits), hits)

    def test_detects_eval(self):
        hits = cmn._detect_dangerous_code("eval(user_input)")
        self.assertTrue(any("eval" in h.lower() for h in hits), hits)

    def test_detects_open_in_write_mode(self):
        hits = cmn._detect_dangerous_code("open('/etc/passwd', 'w').write('x')")
        self.assertTrue(any("file" in h.lower() or "write" in h.lower() for h in hits), hits)

    # ---- AST alias detection (PR 3 core safety requirement) -------------
    def test_ast_alias_import_os_as_o(self):
        hits = cmn._detect_dangerous_code("import os as o\no.system('rm -rf /')")
        self.assertTrue(any("os" in h.lower() or "alias" in h.lower() for h in hits), hits)

    def test_ast_alias_from_os_import_path_as_p(self):
        hits = cmn._detect_dangerous_code(
            "from os import path as p\np.join('/etc', 'passwd')"
        )
        self.assertTrue(any("os" in h.lower() or "alias" in h.lower() for h in hits), hits)

    def test_ast_alias_dynamic_import(self):
        hits = cmn._detect_dangerous_code(
            "os = __import__('os')\nos.system('whoami')"
        )
        self.assertTrue(any("os" in h.lower() or "import" in h.lower() for h in hits), hits)

    def test_safe_code_returns_empty(self):
        hits = cmn._detect_dangerous_code("x = 1 + 2\nprint('hello')")
        self.assertEqual(hits, [])


# ===========================================================================
# Section E: _detect_heavy_geometry_code / _detect_mutation_code
# ===========================================================================
class DetectPatternsTests(unittest.TestCase):
    def test_heavy_geometry_hits(self):
        hits = cmn._detect_heavy_geometry_code("geo = hou.node('/obj/geo1').geometry()")
        self.assertTrue(len(hits) >= 1, hits)

    def test_heavy_geometry_safe(self):
        hits = cmn._detect_heavy_geometry_code("x = 1")
        self.assertEqual(hits, [])

    def test_mutation_hits(self):
        hits = cmn._detect_mutation_code("hou.node('/obj/geo1').destroy()")
        self.assertTrue(len(hits) >= 1, hits)

    def test_mutation_safe(self):
        hits = cmn._detect_mutation_code("x = node.path()")
        self.assertEqual(hits, [])


# ===========================================================================
# Section F: _detect_import_hou (direct / try-block / string concat)
# ===========================================================================
class DetectImportHouTests(unittest.TestCase):
    def test_detects_direct_import(self):
        hits = cmn._detect_import_hou("import hou\nhou.node('/obj')")
        self.assertTrue(len(hits) >= 1, hits)

    def test_detects_from_import(self):
        hits = cmn._detect_import_hou("from hou import node")
        self.assertTrue(len(hits) >= 1, hits)

    def test_detects_try_block_import(self):
        hits = cmn._detect_import_hou(
            "try:\n    import hou\n    hou.node('/obj')\nexcept Exception:\n    pass"
        )
        self.assertTrue(len(hits) >= 1, hits)

    def test_detects_string_concat_import(self):
        hits = cmn._detect_import_hou("mod = __import__('ho' + 'u')")
        self.assertTrue(len(hits) >= 1, hits)

    def test_no_hou_is_clean(self):
        hits = cmn._detect_import_hou("import os\nprint(os.getcwd())")
        self.assertEqual(hits, [])


# ===========================================================================
# Section G: _truncate_output
# ===========================================================================
class TruncateOutputTests(unittest.TestCase):
    def test_empty_string(self):
        out, flag = cmn._truncate_output("", 100)
        self.assertEqual(out, "")
        self.assertFalse(flag)

    def test_exactly_max_size(self):
        s = "a" * 50
        out, flag = cmn._truncate_output(s, 50)
        self.assertEqual(out, s)
        self.assertFalse(flag)

    def test_one_char_over(self):
        s = "a" * 51
        out, flag = cmn._truncate_output(s, 50)
        self.assertEqual(len(out), 50)
        self.assertTrue(flag)

    def test_far_over(self):
        s = "x" * 10000
        out, flag = cmn._truncate_output(s, 100)
        self.assertEqual(len(out), 100)
        self.assertTrue(flag)


# ===========================================================================
# Section H: apply_response_cap (binary search)
# ===========================================================================
class ApplyResponseCapTests(unittest.TestCase):
    def test_empty_dict_under_cap(self):
        self.assertEqual(cmn.apply_response_cap({}, 16384), {})

    def test_at_16k_boundary_not_truncated(self):
        # Build a dict whose serialized size is exactly at the boundary.
        # "x": "aaaa..." sized to be just under cap.
        payload = {"items": [{"i": i, "pad": "a" * 8} for i in range(40)]}
        # size should be well under 16384; tweak until we hit the boundary.
        cap = 16384
        result = cmn.apply_response_cap(payload, cap)
        # Make sure result is the same object if no truncation needed
        self.assertEqual(result.get("_truncated", False), False)

    def test_just_over_16k_truncated(self):
        payload = {"items": [{"i": i, "pad": "a" * 200} for i in range(200)]}
        cap = 16384
        result = cmn.apply_response_cap(payload, cap)
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("_truncated"))
        # size after cap should be <= cap
        size = len(json.dumps(result, default=str).encode("utf-8"))
        self.assertLessEqual(size, cap + 200)  # small slack for metadata

    def test_huge_base64_png_gets_truncated(self):
        big = "A" * (2 * 1024 * 1024)  # ~2MB base64
        payload = {"image_b64": big, "meta": "screenshot"}
        result = cmn.apply_response_cap(payload, 16384)
        size = len(json.dumps(result, default=str).encode("utf-8"))
        self.assertLessEqual(size, 16384 + 200)
        self.assertTrue(result.get("_truncated"))

    def test_binary_search_optimality(self):
        # Build a list of N items where M fit. Verify we get M (not 0 or 1).
        # Each item ~ 200 bytes serialized -> 16384 / 200 = ~80 items fit.
        item_count = 400
        payload = {"items": [{"i": i, "pad": "x" * 150} for i in range(item_count)]}
        cap = 16384
        result = cmn.apply_response_cap(payload, cap)
        kept = len(result["items"])
        self.assertGreater(kept, 5)        # not zero, not trivially small
        self.assertLess(kept, item_count)  # actually truncated
        # size should be <= cap + slack
        size = len(json.dumps(result, default=str).encode("utf-8"))
        self.assertLessEqual(size, cap + 500)

    def test_nested_dict_truncation(self):
        payload = {"a": {"b": [{"i": i, "pad": "x" * 40} for i in range(200)]}}
        cap = 1024
        result = cmn.apply_response_cap(payload, cap)
        self.assertLess(len(result["a"]["b"]), 200)
        self.assertTrue(result.get("_truncated"))
        size = len(json.dumps(result, default=str).encode("utf-8"))
        self.assertLessEqual(size, cap)


# ===========================================================================
# Section I: paginate_list
# ===========================================================================
class PaginateListTests(unittest.TestCase):
    def test_empty_list(self):
        page, cursor = cmn.paginate_list([], limit=10, cursor=0)
        self.assertEqual(page, [])
        self.assertEqual(cursor, 0)

    def test_limit_zero_returns_empty(self):
        page, cursor = cmn.paginate_list([1, 2, 3], limit=0, cursor=0)
        self.assertEqual(page, [])
        self.assertEqual(cursor, 0)

    def test_cursor_out_of_range(self):
        page, cursor = cmn.paginate_list([1, 2, 3], limit=10, cursor=99)
        self.assertEqual(page, [])
        self.assertEqual(cursor, 99)  # cursor echoed; consumer clamps

    def test_normal_pagination(self):
        items = list(range(25))
        page, cursor = cmn.paginate_list(items, limit=10, cursor=0)
        self.assertEqual(page, list(range(10)))
        self.assertEqual(cursor, 10)
        page, cursor = cmn.paginate_list(items, limit=10, cursor=cursor)
        self.assertEqual(page, list(range(10, 20)))
        self.assertEqual(cursor, 20)
        page, cursor = cmn.paginate_list(items, limit=10, cursor=cursor)
        self.assertEqual(page, list(range(20, 25)))
        # past the end: next_cursor should signal end (None or cursor>=len)
        self.assertTrue(cursor is None or cursor >= len(items))

    def test_last_page_partial(self):
        items = list(range(5))
        page, cursor = cmn.paginate_list(items, limit=10, cursor=0)
        self.assertEqual(page, [0, 1, 2, 3, 4])
        self.assertTrue(cursor is None or cursor >= len(items))


# ===========================================================================
# Section J: _add_response_metadata
# ===========================================================================
class AddResponseMetadataTests(unittest.TestCase):
    def test_adds_keys(self):
        result = cmn._add_response_metadata({"a": 1}, truncated=True, size=1234)
        self.assertEqual(result["a"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["size"], 1234)

    def test_no_overwrite_existing(self):
        result = cmn._add_response_metadata({"size": 1}, size=2)
        self.assertEqual(result["size"], 1)


# ===========================================================================
# Section K: _json_safe_hou_value
# ===========================================================================
class JsonSafeHouValueTests(unittest.TestCase):
    def test_passes_through_primitives(self):
        for v in [1, 1.5, "s", True, False, None]:
            self.assertEqual(cmn._json_safe_hou_value(_FakeHou(), v, max_depth=3), v)

    def test_passes_through_dict_of_primitives(self):
        self.assertEqual(
            cmn._json_safe_hou_value(_FakeHou(), {"a": 1, "b": [1, 2]}, max_depth=3),
            {"a": 1, "b": [1, 2]},
        )

    def test_circular_reference_does_not_loop(self):
        a = {}
        a["self"] = a
        out = cmn._json_safe_hou_value(_FakeHou(), a, max_depth=5)
        # circular ref should be replaced by a marker
        self.assertIn("self", out)
        self.assertTrue(isinstance(out["self"], (str, dict)))

    def test_max_depth_truncates(self):
        deep = {"lvl": 0}
        node = deep
        for i in range(1, 20):
            node["child"] = {"lvl": i}
            node = node["child"]
        out = cmn._json_safe_hou_value(_FakeHou(), deep, max_depth=3)
        # walk down: after 3 levels we should see a truncation marker
        def walk(o, depth):
            if depth >= 3:
                return None
            if isinstance(o, dict) and "child" in o:
                return walk(o["child"], depth + 1)
            return o
        self.assertIsNone(walk(out, 0))

    def test_hou_vector_serialized_as_list(self):
        v = _FakeVector([1.0, 2.0, 3.0])
        out = cmn._json_safe_hou_value(_FakeHou(), v, max_depth=3)
        self.assertEqual(out, [1.0, 2.0, 3.0])

    def test_hou_color_serialized_as_list(self):
        c = _FakeColor([0.1, 0.2, 0.3, 0.4])
        out = cmn._json_safe_hou_value(_FakeHou(), c, max_depth=3)
        self.assertEqual(out, [0.1, 0.2, 0.3, 0.4])

    def test_hou_enum_value_serialized_as_string(self):
        e = _FakeEnumValue("menu_token")
        out = cmn._json_safe_hou_value(_FakeHou(), e, max_depth=3)
        self.assertEqual(out, "menu_token")

    def test_hou_ramp_serialized_as_points(self):
        class P(object):
            def __init__(self, pos, val):
                self.position = pos
                self.value = val
        r = _FakeRamp(points=[P(0.0, 0.0), P(0.5, 1.0), P(1.0, 0.0)])
        out = cmn._json_safe_hou_value(_FakeHou(), r, max_depth=3)
        self.assertEqual(out, {"points": [(0.0, 0.0), (0.5, 1.0), (1.0, 0.0)]})


# ===========================================================================
# Section L: _flatten_parm_templates
# ===========================================================================
class FlattenParmTemplatesTests(unittest.TestCase):
    def test_returns_list(self):
        hou = _FakeHou()
        result = cmn._flatten_parm_templates(hou, [], max_depth=2)
        self.assertIsInstance(result, list)

    def test_handles_dict_template(self):
        hou = _FakeHou()
        tpl = {"name": "size", "label": "Size", "type": "float"}
        result = cmn._flatten_parm_templates(hou, [tpl], max_depth=2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "size")


# ===========================================================================
# Section M: ExecutionTimeoutError
# ===========================================================================
class ExecutionTimeoutErrorTests(unittest.TestCase):
    def test_is_exception_subclass(self):
        self.assertTrue(issubclass(cmn.ExecutionTimeoutError, Exception))

    def test_carries_message(self):
        err = cmn.ExecutionTimeoutError("timed out after 30s")
        self.assertIn("timed out after 30s", str(err))

    def test_can_be_raised_and_caught(self):
        with self.assertRaises(cmn.ExecutionTimeoutError):
            raise cmn.ExecutionTimeoutError("nope")


# ===========================================================================
# Section N: __all__ export contract
# ===========================================================================
class ExportContractTests(unittest.TestCase):
    def test_all_contains_expected_names(self):
        expected = {
            "handle_connection_errors",
            "CONNECTION_ERRORS",
            "_handle_connection_error",
            "validate_resolution",
            "DANGEROUS_PATTERNS",
            "HEAVY_GEOMETRY_PATTERNS",
            "MUTATION_PATTERNS",
            "_detect_mutation_code",
            "_detect_dangerous_code",
            "_detect_heavy_geometry_code",
            "_detect_import_hou",
            "_truncate_output",
            "_estimate_response_size",
            "_serialized_size",
            "apply_response_cap",
            "paginate_list",
            "_add_response_metadata",
            "_json_safe_hou_value",
            "_flatten_parm_templates",
            "ExecutionTimeoutError",
        }
        self.assertTrue(expected.issubset(set(cmn.__all__)),
                        "missing from __all__: " + str(expected - set(cmn.__all__)))


if __name__ == "__main__":
    unittest.main()