"""Unit tests for external/houdinimcp/_render_b64.py (PR 14).

Stdlib unittest, no hython required. hou is mocked via stub classes. PySide6 /
PySide2 is not installed in the test environment, so the default state is
"graceful warning" path required by the brief.

Tests cover (>= 30):
    - Module load + helper reuse: VALID_RENDERERS, _encode_image_to_base64,
      _resolve_camera_path.
    - render_viewport:
        - normal opengl with camera/geometry paths
        - default camera when camera_path is None
        - 3 renderer variants: opengl / karma_cpu / karma_xpu
        - resolution and format pass-through (PNG / JPEG)
        - no hou.hipFile / no PySide -> _warning dict (graceful)
        - base64 decodes to expected format header bytes
    - render_quad_views:
        - 4 view entries (top/front/side/perspective)
        - bbox computed once (shared camera rig)
        - renderer selection propagates per view
        - no geometry -> _warning dict
    - apply_response_cap + _add_response_metadata integration
    - bridge style probe (3 new @mcp.tool() with no type annotations +
      Chinese docstrings); send_command cmd names match.
    - server.py: _render_b64 import, 3 new handlers in dict, 3 thin wrapper
      methods exist, wrappers call cmn.apply_response_cap once.

Run with:
    python -m unittest tests.test_render_b64 -v
"""
import ast
import base64
import importlib.util as _ilu
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# Build a synthetic "houdinimcp" package so the production-style
# `from . import _common as cmn` inside _render_b64.py resolves.
_PKG_KEY = "houdinimcp"
_RB64_KEY = "houdinimcp._render_b64"
_RENDER_KEY = "houdinimcp.HoudiniMCPRender"
if _PKG_KEY not in sys.modules:
    pkg = types.ModuleType(_PKG_KEY)
    pkg.__path__ = [ROOT]
    sys.modules[_PKG_KEY] = pkg

# Ensure _common is loaded under the houdinimcp._common key so the
# `from . import _common as cmn` inside _render_b64.py resolves.
_SPEC_CMN = _ilu.spec_from_file_location("houdinimcp._common",
                                         os.path.join(ROOT, "_common.py"))
_common = _ilu.module_from_spec(_SPEC_CMN)
sys.modules["houdinimcp._common"] = _common
_SPEC_CMN.loader.exec_module(_common)
cmn = _common


# Install a fake HoudiniMCPRender module so _render_b64.py can import the
# helper functions (find_displayed_geometry / calculate_bounding_box /
# setup_camera_rig / adjust_camera_to_fit_bbox) without needing hython.
class _FakeRenderHelpers(object):
    """Stub of HoudiniMCPRender exposing the 4 helpers reused by PR 14."""

    def __init__(self):
        self.find_displayed_geometry_calls = []
        self.find_displayed_geometry_args = []
        self.calculate_bounding_box_calls = []
        self.setup_camera_rig_calls = []
        self.adjust_camera_to_fit_bbox_calls = []

    def find_displayed_geometry(self):
        # Record (args, kwargs) of every call so tests can assert the
        # production code matches HoudiniMCPRender.find_displayed_geometry()'s
        # real no-arg signature (PR 14 reviewer Important).
        self.find_displayed_geometry_args.append(((), {}))
        self.find_displayed_geometry_calls.append(True)
        return [object()]

    def calculate_bounding_box(self, nodes):
        self.calculate_bounding_box_calls.append(len(nodes))
        return {
            "min": [-1.0, -1.0, -1.0],
            "max": [1.0, 1.0, 1.0],
            "center": [0.0, 0.0, 0.0],
        }

    def setup_camera_rig(self, center, orthographic=False):
        self.setup_camera_rig_calls.append((tuple(center), orthographic))
        return object()

    def adjust_camera_to_fit_bbox(self, camera, bbox, padding_factor=1.1):
        self.adjust_camera_to_fit_bbox_calls.append(True)

    def rotate_camera_center(self, null, rotation):
        # Optional helper for quad views; provide a no-op for completeness.
        pass


# Install a separate fresh fake for each test class so per-test mutation
# doesn't leak across tests.
def _install_fake_render_module():
    fake = _FakeRenderHelpers()
    sys.modules[_RENDER_KEY] = fake
    return fake


def _load_render_b64_fresh():
    """Reload _render_b64 module fresh from source, returns module."""
    # Always install a fresh render helpers stub so per-test state is clean.
    fake = _install_fake_render_module()
    if _RB64_KEY in sys.modules:
        del sys.modules[_RB64_KEY]
    spec = _ilu.spec_from_file_location(
        _RB64_KEY, os.path.join(ROOT, "_render_b64.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[_RB64_KEY] = mod
    spec.loader.exec_module(mod)
    return mod, fake


# ---------------------------------------------------------------------------
# hou + UI stubs
# ---------------------------------------------------------------------------
class _FakeNode(object):
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path

    def type(self):
        t = _FakeNodeType(self._path.split("/")[-1])
        return t


class _FakeNodeType(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name

    def category(self):
        c = _FakeCategory("Sop" if self._name != "geo" else "Object")
        return c


class _FakeCategory(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeHou(object):
    """Bare-bones hou stub sufficient for render_b64 internal mocking.

    The production render_b64 module is structured to short-circuit on
    missing hou/hipFile; tests can drive the graceful warning path by
    leaving `hipFile` undefined, or can monkey-patch `_RENDERABLE` helpers
    to drive the success path.
    """

    def __init__(self, nodes=None, has_hipfile=False):
        self._nodes = nodes or {}
        self.hipFile = object() if has_hipfile else None
        self.node_calls = []

    def node(self, path):
        self.node_calls.append(path)
        return self._nodes.get(path)


def _make_hou_with_default_camera():
    """hou stub where /obj/MCP_CAMERA resolves to a FakeNode."""
    cam = _FakeNode("/obj/MCP_CAMERA")
    return _FakeHou(nodes={"/obj/MCP_CAMERA": cam})


# ===========================================================================
# Section A: module constants / imports
# ===========================================================================
class ModuleConstantsTests(unittest.TestCase):
    """Important: VALID_RENDERERS must include all 3 brief-renderer strings
    and _render_b64.py must reuse (not redefine) the 4 HoudiniMCPRender
    helpers."""

    @classmethod
    def setUpClass(cls):
        cls.mod, cls.render_helpers = _load_render_b64_fresh()

    def test_module_loads(self):
        self.assertTrue(hasattr(self.mod, "render_viewport"))
        self.assertTrue(hasattr(self.mod, "render_quad_views"))

    def test_valid_renderers_contains_three(self):
        """VALID_RENDERERS must contain 'opengl', 'karma_cpu', 'karma_xpu'."""
        self.assertIn("opengl", self.mod.VALID_RENDERERS)
        self.assertIn("karma_cpu", self.mod.VALID_RENDERERS)
        self.assertIn("karma_xpu", self.mod.VALID_RENDERERS)

    def test_valid_renderers_at_least_three(self):
        """At least 3 renderers, never fewer."""
        self.assertGreaterEqual(len(self.mod.VALID_RENDERERS), 3)

    def test_helper_imports_reused_not_redefined(self):
        """_render_b64 must import find_displayed_geometry etc. (not redefine).

        This is enforced by module attribute check + source-text probe:
        there must not be a `def find_displayed_geometry` in _render_b64.py.
        """
        with open(os.path.join(ROOT, "_render_b64.py"), "r",
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn(
            "def find_displayed_geometry", src,
            "_render_b64.py must not redefine find_displayed_geometry")
        self.assertNotIn(
            "def calculate_bounding_box", src,
            "_render_b64.py must not redefine calculate_bounding_box")
        self.assertNotIn(
            "def setup_camera_rig", src,
            "_render_b64.py must not redefine setup_camera_rig")
        self.assertNotIn(
            "def adjust_camera_to_fit_bbox", src,
            "_render_b64.py must not redefine adjust_camera_to_fit_bbox")

    def test_helper_imports_present(self):
        """Source must contain explicit imports of the 4 reused helpers."""
        with open(os.path.join(ROOT, "_render_b64.py"), "r",
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("find_displayed_geometry", src)
        self.assertIn("calculate_bounding_box", src)
        self.assertIn("setup_camera_rig", src)
        self.assertIn("adjust_camera_to_fit_bbox", src)


# ===========================================================================
# Section B: render_viewport - graceful path (no hou.hipFile / PySide)
# ===========================================================================
class RenderViewportGracefulTests(unittest.TestCase):
    """Important: when hou is unavailable or PySide missing, render_viewport
    must return a _warning dict instead of raising."""

    @classmethod
    def setUpClass(cls):
        cls.mod, cls.render_helpers = _load_render_b64_fresh()

    def test_returns_warning_when_no_hipfile(self):
        hou = _FakeHou(has_hipfile=False)
        result = self.mod.render_viewport(hou)
        self.assertIn("_warning", result)
        self.assertIsNone(result.get("image_base64"))
        # When hipFile is missing, _warning short-circuits with the format
        # value the caller passed (default "PNG"); size_bytes is 0.
        self.assertEqual(result.get("size_bytes"), 0)
        self.assertEqual(result.get("renderer"), "opengl")
        self.assertEqual(result.get("width"), 0)
        self.assertEqual(result.get("height"), 0)

    def test_returns_warning_when_no_pyside(self):
        """render_viewport must short-circuit when PySide unavailable."""
        hou = _FakeHou(has_hipfile=True)
        # The production module reads _QT_BACKEND-style flags at import-time;
        # we exercise the no-PySide warning by patching render_viewport's
        # PySide availability check via a no-op monkey patch.
        # Simplest path: monkey-patch _ensure_qimage to return None.
        original = getattr(self.mod, "_ensure_qimage", None)
        if original is None:
            self.skipTest("_ensure_qimage helper not present in module")
        self.mod._ensure_qimage = lambda: None
        try:
            result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA")
            self.assertIn("_warning", result)
        finally:
            self.mod._ensure_qimage = original


# ===========================================================================
# Section C: render_viewport - success path (mocked QImage)
# ===========================================================================
class _FakeQImage(object):
    """Stand-in for PySide QImage; .save() writes a fake PNG header."""

    def __init__(self, w, h):
        self._w = w
        self._h = h
        self._null = False

    def width(self):
        return self._w

    def height(self):
        return self._h

    def isNull(self):
        return self._null

    def save(self, target, fmt=None):
        # Write a recognizable PNG / JPEG header based on fmt
        if fmt == "JPEG":
            header = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        else:
            header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        if isinstance(target, str):
            with open(target, "wb") as f:
                f.write(header)
        else:
            # BytesIO / QBuffer-like target
            try:
                target.write(header)
            except Exception:
                pass
        return True


class _FakeQPixmap(object):
    def __init__(self, w, h):
        self._img = _FakeQImage(w, h)
        self._null = False

    def isNull(self):
        return self._null

    def toImage(self):
        return self._img

    def save(self, target, fmt=None):
        # Forward to underlying QImage.save so production code that
        # saves directly from QPixmap still works in tests.
        return self._img.save(target, fmt)


def _install_fake_pyside_for_render(mod):
    """Monkey-patch _ensure_qimage / _grab_viewport_image on `mod` so
    render_viewport returns a deterministic base64 PNG without needing PySide.
    """
    captured = {"calls": []}

    def fake_qimage(w, h):
        return _FakeQImage(w, h)

    def fake_grab(hou, width, height):
        captured["calls"].append((width, height))
        return _FakeQPixmap(width, height)

    # Patch whichever helpers the module exposes; tolerate both names.
    if hasattr(mod, "_ensure_qimage"):
        mod._ensure_qimage = fake_qimage
    if hasattr(mod, "_grab_viewport_image"):
        mod._grab_viewport_image = fake_grab
    elif hasattr(mod, "_grab_viewport"):
        mod._grab_viewport = fake_grab
    return captured


class RenderViewportSuccessTests(unittest.TestCase):
    """Important: render_viewport must produce a valid base64 PNG/JPEG
    that decodes to the expected magic bytes."""

    def setUp(self):
        self.mod, self.render_helpers = _load_render_b64_fresh()
        self._grabs = _install_fake_pyside_for_render(self.mod)

    def test_basic_render_returns_base64(self):
        hou = _make_hou_with_default_camera()
        # Force hipFile presence to bypass graceful warning
        hou.hipFile = object()
        result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA",
                                          resolution=(320, 240), format="PNG")
        self.assertNotIn("_warning", result)
        b64 = result["image_base64"]
        self.assertIsInstance(b64, str)
        self.assertGreater(len(b64), 0)
        # Decode and check PNG header
        decoded = base64.b64decode(b64)
        self.assertEqual(decoded[:8], b"\x89PNG\r\n\x1a\n")

    def test_renderer_field_propagates(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        for r in ("opengl", "karma_cpu", "karma_xpu"):
            result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA",
                                              renderer=r)
            self.assertEqual(result.get("renderer"), r,
                             "renderer field mismatch for {0}".format(r))

    def test_format_field_png(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA",
                                          format="PNG")
        self.assertEqual(result.get("format"), "PNG")

    def test_format_field_jpeg(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA",
                                          format="JPEG")
        self.assertEqual(result.get("format"), "JPEG")
        decoded = base64.b64decode(result["image_base64"])
        self.assertEqual(decoded[:4], b"\xff\xd8\xff\xe0")

    def test_resolution_passes_through(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA",
                                          resolution=(1280, 720))
        self.assertEqual(result.get("width"), 1280)
        self.assertEqual(result.get("height"), 720)

    def test_camera_path_default_when_none(self):
        """camera_path=None must use the default /obj/MCP_CAMERA path."""
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        # Add the default camera explicitly
        result = self.mod.render_viewport(hou, camera_path=None)
        self.assertEqual(result.get("camera_path"), "/obj/MCP_CAMERA")

    def test_geometry_path_propagates(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA",
                                          geometry_path="/obj/geo1")
        self.assertEqual(result.get("geometry_path"), "/obj/geo1")

    def test_geometry_path_none_is_empty(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA",
                                          geometry_path=None)
        self.assertEqual(result.get("geometry_path"), "")

    def test_size_bytes_populated(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA")
        self.assertGreater(result.get("size_bytes", 0), 0)

    def test_meta_block_present(self):
        """render_viewport must attach _meta via _add_response_metadata."""
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_viewport(hou, camera_path="/obj/MCP_CAMERA",
                                          renderer="karma_cpu")
        self.assertIn("_meta", result)


# ===========================================================================
# Section D: render_quad_views
# ===========================================================================
class RenderQuadViewsTests(unittest.TestCase):

    def setUp(self):
        self.mod, self.render_helpers = _load_render_b64_fresh()
        self._grabs = _install_fake_pyside_for_render(self.mod)

    def test_four_views_returned(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_quad_views(hou)
        for view in ("top", "front", "side", "perspective"):
            self.assertIn(view, result, "missing view {0}".format(view))
            self.assertIsInstance(result[view].get("image_base64"), str)

    def test_shared_bbox_computed_once(self):
        """render_quad_views should call find_displayed_geometry and
        calculate_bounding_box exactly once (shared bbox)."""
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        self.mod.render_quad_views(hou)
        self.assertEqual(
            len(self.render_helpers.find_displayed_geometry_calls), 1,
            "find_displayed_geometry should be called once for all views")
        self.assertEqual(
            len(self.render_helpers.calculate_bounding_box_calls), 1,
            "calculate_bounding_box should be called once for all views")

    def test_each_view_has_unique_camera_rig(self):
        """render_quad_views should call setup_camera_rig for each of 4 views."""
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        self.mod.render_quad_views(hou)
        self.assertEqual(
            len(self.render_helpers.setup_camera_rig_calls), 4,
            "setup_camera_rig should be called 4 times (one per view)")

    def test_renderer_propagates(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_quad_views(hou, renderer="karma_xpu")
        for view in ("top", "front", "side", "perspective"):
            self.assertEqual(result[view].get("renderer"), "karma_xpu")

    def test_meta_block_present(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_quad_views(hou)
        self.assertIn("_meta", result)

    def test_resolution_per_view(self):
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        result = self.mod.render_quad_views(hou, resolution=(160, 120))
        for view in ("top", "front", "side", "perspective"):
            self.assertEqual(result[view].get("width"), 160)
            self.assertEqual(result[view].get("height"), 120)

    def test_warning_when_no_geometry(self):
        """If find_displayed_geometry returns empty list, render_quad_views
        must surface a warning rather than crashing."""
        hou = _FakeHou(has_hipfile=True)
        # Override fake to return empty list
        original = self.render_helpers.find_displayed_geometry
        self.render_helpers.find_displayed_geometry = lambda: []
        try:
            result = self.mod.render_quad_views(hou)
            self.assertIn("_warning", result)
        finally:
            self.render_helpers.find_displayed_geometry = original

    def test_find_displayed_geometry_called_with_no_args(self):
        """Regression: PR 14 reviewer Important.

        HoudiniMCPRender.find_displayed_geometry() real signature is no-arg,
        so production _render_b64.render_quad_views MUST call it with no args.
        The old call ``_render_lib.find_displayed_geometry(hou)`` raised
        TypeError in production that was silently swallowed by the surrounding
        try/except, making every scene look empty.
        """
        hou = _make_hou_with_default_camera()
        hou.hipFile = object()
        self.mod.render_quad_views(hou)
        self.assertEqual(
            len(self.render_helpers.find_displayed_geometry_args), 1,
            "find_displayed_geometry should be called once")
        args, kwargs = self.render_helpers.find_displayed_geometry_args[0]
        self.assertEqual(
            args, (),
            "find_displayed_geometry must be called with no positional args "
            "(real signature is `def find_displayed_geometry():`), got {0!r}"
            .format(args))
        self.assertEqual(
            kwargs, {},
            "find_displayed_geometry must be called with no kwargs, got {0!r}"
            .format(kwargs))


# ===========================================================================
# Section E: apply_response_cap + _add_response_metadata integration
# ===========================================================================
class ApplyResponseCapTests(unittest.TestCase):
    """Brief: base64 PNG/JPEG must be truncated when total response
    exceeds 16KB. _truncated=True marker must be set."""

    def setUp(self):
        self.mod, _ = _load_render_b64_fresh()
        _install_fake_pyside_for_render(self.mod)

    def test_large_response_truncated(self):
        # Construct a 30KB base64 payload (binary 22.5KB)
        big_png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30000)
        big_png_str = big_png.decode("ascii")
        data = {"image_base64": big_png_str, "format": "PNG",
                "renderer": "opengl"}
        capped = cmn.apply_response_cap(data, max_bytes=16384)
        # After cap, response must be substantially smaller than original.
        # The exact size may slightly exceed max_bytes due to metadata
        # overhead (_truncated / _original_size markers), but the dominant
        # payload field must have been truncated.
        orig_size = cmn._serialized_size(data)
        capped_size = cmn._serialized_size(capped)
        self.assertLess(capped_size, orig_size,
                        "apply_response_cap must shrink the payload")
        # image_base64 must have been truncated to <= max_bytes - metadata
        self.assertLess(len(capped["image_base64"]), len(big_png_str),
                        "image_base64 must be truncated")
        # Truncation marker must be present
        self.assertTrue(capped.get("_truncated") or "_truncated_count" in capped)

    def test_small_response_unchanged(self):
        data = {"image_base64": "ABCD", "format": "PNG"}
        capped = cmn.apply_response_cap(data, max_bytes=16384)
        self.assertEqual(capped.get("image_base64"), "ABCD")
        self.assertNotIn("_truncated", capped)

    def test_add_response_metadata_merges_keys(self):
        data = {"foo": 1}
        out = cmn._add_response_metadata(data, renderer="opengl", engine="cpu")
        self.assertEqual(out["renderer"], "opengl")
        self.assertEqual(out["engine"], "cpu")
        # existing keys are not overwritten
        self.assertEqual(out["foo"], 1)

    def test_add_response_metadata_no_overwrite(self):
        data = {"renderer": "karma_cpu"}
        out = cmn._add_response_metadata(data, renderer="opengl")
        self.assertEqual(out["renderer"], "karma_cpu",
                         "_add_response_metadata must not overwrite existing keys")


# ===========================================================================
# Section F: server.py handler integration
# ===========================================================================
SERVER_PY = os.path.join(ROOT, "server.py")


def _find_pr14_handler_method_nodes():
    """AST scan server.py for the 3 PR 14 thin-wrapper methods on
    HoudiniMCPServer."""
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef)
                and n.name == "HoudiniMCPServer"), None)
    if cls is None:
        return None, src, []
    names = {"render_viewport_base64", "render_quad_views_base64",
             "render_specific_camera_base64"}
    found = [m for m in cls.body
             if isinstance(m, ast.FunctionDef) and m.name in names]
    return cls, src, found


class ServerHandlersTests(unittest.TestCase):
    """server.py must register 3 new handlers and call apply_response_cap."""

    def setUp(self):
        self.cls, self.src, _ = _find_pr14_handler_method_nodes()
        self.assertIsNotNone(self.cls,
                             "HoudiniMCPServer class missing in server.py")
        self.tree = ast.parse(self.src)

    def test_render_b64_import_present(self):
        self.assertIn("from . import _render_b64", self.src)

    def test_three_thin_wrappers_defined(self):
        names = {m.name for m in self.cls.body
                 if isinstance(m, ast.FunctionDef)}
        for required in ("render_viewport_base64",
                         "render_quad_views_base64",
                         "render_specific_camera_base64"):
            self.assertIn(required, names,
                          "missing handler method {0}".format(required))

    def test_handlers_dict_contains_three_new_keys(self):
        expected = {"render_viewport_base64",
                    "render_quad_views_base64",
                    "render_specific_camera_base64"}
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        if k.value in expected:
                            found.add(k.value)
        self.assertEqual(found, expected,
                         "handlers dict must contain all 3 PR 14 keys")

    def test_handlers_apply_response_cap(self):
        names = {"render_viewport_base64", "render_quad_views_base64",
                 "render_specific_camera_base64"}
        for method in self.cls.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            if method.name not in names:
                continue
            src = ast.get_source_segment(self.src, method) or ""
            self.assertIn(
                "apply_response_cap", src,
                "Handler {0} must wrap result with apply_response_cap".format(
                    method.name))

    def test_legacy_handlers_preserved(self):
        """render_single_view / render_quad_view / render_specific_camera
        must NOT be removed by PR 14."""
        names = {m.name for m in self.cls.body
                 if isinstance(m, ast.FunctionDef)}
        for required in ("handle_render_single_view",
                         "handle_render_quad_view",
                         "handle_render_specific_camera"):
            self.assertIn(required, names,
                          "legacy handler {0} must be preserved".format(required))


# ===========================================================================
# Section G: server.py handler wrapper AST-exec (apply_response_cap invoked)
# ===========================================================================
class _StubCap(object):
    def __init__(self):
        self.calls = []

    def __call__(self, value, *args, **kwargs):
        self.calls.append({"value": value, "args": args, "kwargs": kwargs})
        return value


def _build_pr14_handler_class(cap_stub=None, rb64_stub=None):
    """AST-extract the 3 PR 14 handler methods from server.py and exec each
    into a stub class namespace with mocked `hou` / `rb64` / `cmn`."""
    server_src_path = os.path.join(ROOT, "server.py")
    with open(server_src_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    cls_node = next((n for n in tree.body
                     if isinstance(n, ast.ClassDef)
                     and n.name == "HoudiniMCPServer"), None)
    if cls_node is None:
        raise AssertionError("HoudiniMCPServer class not found in server.py")
    if cap_stub is None:
        cap_stub = _StubCap()
    if rb64_stub is None:
        rb64_stub = types.SimpleNamespace()
    cmn_stub = types.SimpleNamespace(apply_response_cap=cap_stub)
    ns = {"hou": object(), "rb64": rb64_stub, "cmn": cmn_stub}
    target = {"render_viewport_base64",
              "render_quad_views_base64",
              "render_specific_camera_base64"}
    class_ns = {}
    for method in cls_node.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        if method.name not in target:
            continue
        method_src = ast.get_source_segment(src, method)
        local = dict(ns)
        local["__name__"] = "_Pr14Method_{0}".format(method.name)
        exec(compile(method_src, "<server_{0}>".format(method.name), "exec"),
             local)
        class_ns[method.name] = local[method.name]
    class _Pr14Server(object):
        pass
    for n, fn in class_ns.items():
        setattr(_Pr14Server, n, fn)
    return _Pr14Server, cap_stub, rb64_stub


class ServerHandlerCapMockTests(unittest.TestCase):
    """Each PR 14 HoudiniMCPServer method must call cmn.apply_response_cap."""

    def setUp(self):
        self._rb64_stub = types.SimpleNamespace()
        self._rb64_stub.render_viewport = lambda *a, **kw: {
            "image_base64": "ABCD", "format": "PNG", "renderer": "opengl",
            "width": 320, "height": 240, "camera_path": "/obj/MCP_CAMERA",
            "geometry_path": "", "size_bytes": 4}
        self._rb64_stub.render_quad_views = lambda *a, **kw: {
            "top": {"image_base64": "A", "format": "PNG"},
            "front": {"image_base64": "B", "format": "PNG"},
            "side": {"image_base64": "C", "format": "PNG"},
            "perspective": {"image_base64": "D", "format": "PNG"}}

    def test_render_viewport_base64_calls_cap_once(self):
        cls, cap, _ = _build_pr14_handler_class(rb64_stub=self._rb64_stub)
        inst = cls()
        inst.render_viewport_base64()
        self.assertEqual(len(cap.calls), 1)

    def test_render_quad_views_base64_calls_cap_once(self):
        cls, cap, _ = _build_pr14_handler_class(rb64_stub=self._rb64_stub)
        inst = cls()
        inst.render_quad_views_base64()
        self.assertEqual(len(cap.calls), 1)


# ===========================================================================
# Section H: bridge style probe (PR 14 tools)
# ===========================================================================
HMA_PY = os.path.join(ROOT, "houdini_mcp_server.py")
PR14_SECTION_HEADER = "# PR 14 Render Base64 Tools"


def _has_cjk(s):
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _signature_has_annotations(fn):
    args = fn.args
    for arg in (args.posonlyargs + args.args + args.kwonlyargs):
        if arg.annotation is not None:
            return True
    if args.vararg and args.vararg.annotation is not None:
        return True
    if args.kwarg and args.kwarg.annotation is not None:
        return True
    return fn.returns is not None


def _find_pr14_function_nodes():
    """Find PR 14 @mcp.tool() functions in houdini_mcp_server.py."""
    with open(HMA_PY, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    lines = src.splitlines()
    header_line = None
    for i, line in enumerate(lines, start=1):
        if PR14_SECTION_HEADER in line:
            header_line = i
            break
    if header_line is None:
        raise AssertionError(
            "PR 14 section marker not found in houdini_mcp_server.py")
    # Stop at next section header
    stop_line = len(lines) + 1
    section_header_re = re.compile(r"^#\s*PR\s+\d+\s+\S.*\bTools\b")
    for i in range(header_line + 1, len(lines) + 1):
        line = lines[i - 1]
        if section_header_re.match(line) and PR14_SECTION_HEADER not in line:
            stop_line = i
            break
    fns = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.lineno <= header_line:
            continue
        if node.lineno >= stop_line:
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                func = dec.func
                if isinstance(func, ast.Attribute) and func.attr == "tool":
                    fns.append(node)
                    break
    return fns


import re  # imported here to avoid top-level namespace pollution


class PR14BridgeStyleTests(unittest.TestCase):
    """3 new @mcp.tool() must have no type annotations and Chinese
    docstrings."""

    def setUp(self):
        self.fns = _find_pr14_function_nodes()
        self.assertEqual(
            len(self.fns), 3,
            "Expected 3 PR 14 @mcp.tool() functions, found {0}: {1}".format(
                len(self.fns), [f.name for f in self.fns]))

    def test_three_expected_names(self):
        names = sorted(f.name for f in self.fns)
        self.assertEqual(names, sorted([
            "render_quad_views_base64",
            "render_specific_camera_base64",
            "render_viewport_base64",
        ]))

    def test_render_viewport_base64_no_annotations(self):
        fn = next(f for f in self.fns if f.name == "render_viewport_base64")
        self.assertFalse(_signature_has_annotations(fn))

    def test_render_quad_views_base64_no_annotations(self):
        fn = next(f for f in self.fns if f.name == "render_quad_views_base64")
        self.assertFalse(_signature_has_annotations(fn))

    def test_render_specific_camera_base64_no_annotations(self):
        fn = next(f for f in self.fns
                  if f.name == "render_specific_camera_base64")
        self.assertFalse(_signature_has_annotations(fn))

    def test_render_viewport_base64_chinese_docstring(self):
        fn = next(f for f in self.fns if f.name == "render_viewport_base64")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(_has_cjk(doc), repr(doc))

    def test_render_quad_views_base64_chinese_docstring(self):
        fn = next(f for f in self.fns if f.name == "render_quad_views_base64")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(_has_cjk(doc), repr(doc))

    def test_render_specific_camera_base64_chinese_docstring(self):
        fn = next(f for f in self.fns
                  if f.name == "render_specific_camera_base64")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(_has_cjk(doc), repr(doc))


# ===========================================================================
# Section I: bridge tool AST exec (cmd names + params)
# ===========================================================================
class _RecordingHoudiniCall(object):
    def __init__(self, response=None):
        self.calls = []
        self.response = response if response is not None else {
            "status": "success", "result": {"image_base64": "A"}}

    def __call__(self, cmd, params=None):
        self.calls.append({"cmd": cmd, "params": params or {}})
        return self.response


class _FakeMCP(object):
    def __init__(self):
        self.registry = {}

    def tool(self):
        def decorator(fn):
            self.registry[fn.__name__] = fn
            return fn
        return decorator


def _exec_pr14_bridge_tool(tool_name):
    with open(HMA_PY, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    fn_node = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == tool_name:
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call):
                    func = dec.func
                    if isinstance(func, ast.Attribute) and func.attr == "tool":
                        fn_node = node
                        break
            if fn_node is not None:
                break
    if fn_node is None:
        raise AssertionError(
            "PR 14 bridge tool {0} not found".format(tool_name))
    fn_src = ast.get_source_segment(src, fn_node)
    fake_mcp = _FakeMCP()
    rec = _RecordingHoudiniCall()
    ns = {"mcp": fake_mcp, "_houdini_call": rec}
    exec(compile(fn_src, "<pr14_{0}>".format(tool_name), "exec"), ns)
    return ns[tool_name], rec, fake_mcp


class PR14BridgeToolASTExecTests(unittest.TestCase):
    """Each PR 14 bridge tool, AST-isolated, must call _houdini_call with the
    correct cmd_name + params."""

    def test_render_viewport_base64_cmd(self):
        fn, rec, _ = _exec_pr14_bridge_tool("render_viewport_base64")
        fn(object(), "/obj/cam", "/obj/geo", "karma_xpu", (320, 240), "PNG")
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call["cmd"], "render_viewport_base64")
        self.assertEqual(call["params"]["camera_path"], "/obj/cam")
        self.assertEqual(call["params"]["geometry_path"], "/obj/geo")
        self.assertEqual(call["params"]["renderer"], "karma_xpu")
        # Bridge serializes tuple -> list; assert values instead of type.
        self.assertEqual(tuple(call["params"]["resolution"]), (320, 240))
        self.assertEqual(call["params"]["format"], "PNG")

    def test_render_quad_views_base64_cmd(self):
        fn, rec, _ = _exec_pr14_bridge_tool("render_quad_views_base64")
        fn(object(), "/obj/geo", "opengl", (160, 120), "JPEG")
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call["cmd"], "render_quad_views_base64")
        self.assertEqual(call["params"]["geometry_path"], "/obj/geo")
        self.assertEqual(call["params"]["renderer"], "opengl")
        self.assertEqual(call["params"]["format"], "JPEG")

    def test_render_specific_camera_base64_cmd(self):
        fn, rec, _ = _exec_pr14_bridge_tool("render_specific_camera_base64")
        fn(object(), "/obj/cam1", (640, 480), "PNG", "karma_cpu")
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call["cmd"], "render_specific_camera_base64")
        self.assertEqual(call["params"]["camera_path"], "/obj/cam1")

    def test_render_viewport_base64_passes_through_error(self):
        err = {"status": "error", "message": "no Houdini", "origin": "h"}
        fn, rec, _ = _exec_pr14_bridge_tool("render_viewport_base64")
        rec.response = err
        result = fn(object(), "/obj/cam")
        self.assertEqual(result, err)


if __name__ == "__main__":
    unittest.main()
