"""Unit tests for env dir derivation across all 3 production modules + 2 test scripts.

Verifies the convention: <package_dir_parent>/<package_dir_basename>-env/,
overridable via the HOUDINI_MCP_ENV_DIR absolute path.

Runs without Houdini / hython. Stubs the heavy third-party deps (mcp, hou)
before import so we can isolate ``_env_dir()`` behavior.
"""
import importlib.util as _ilu
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


_STUB_NAMES = (
    "mcp", "mcp.server", "mcp.server.fastmcp", "mcp.server.session",
    "mcp.types", "mcp.shared", "mcp.shared.exceptions",
    "requests", "dotenv",
    "hou",
)


class _StubModule(types.ModuleType):
    """Module stand-in: any attribute access or call returns another stub.

    Lets ``from foo import bar`` succeed and ``FastMCP(...)`` return a stub
    without raising, so top-level module construction (mcp = FastMCP(...))
    no longer blocks us from introspecting functions defined above it.
    """

    def __getattr__(self, name):
        full = f"{self.__name__}.{name}" if self.__name__ else name
        child = _StubModule(full)
        sys.modules[full] = child
        return child

    def __call__(self, *args, **kwargs):
        return _StubModule(f"{self.__name__}.<call>")


def _install_stubs():
    """Install no-op stub modules so top-level imports don't fail."""
    for name in _STUB_NAMES:
        if name not in sys.modules:
            sys.modules[name] = _StubModule(name)


def _load_module(name, path):
    """Load a module from disk, tolerating top-level construction errors.

    Some production modules build heavy objects at import time (e.g.
    ``mcp = FastMCP(...)``). We only need module-level functions defined
    above that construction, so we swallow construction-time errors and
    surface whatever did bind to the module namespace.
    """
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # Construction at module bottom (e.g. mcp = FastMCP(...)) may
        # still fail even with stubs. Definitions above the failure
        # point are intact on `mod` and we can still introspect them.
        pass
    return mod


class _EnvDirHelper(unittest.TestCase):
    """Shared assertions for the 3 production modules' _env_dir()."""

    MODULE_PATHS = {
        "houdini_mcp_server": os.path.join(ROOT, "houdini_mcp_server.py"),
        "_render_policy": os.path.join(ROOT, "_render_policy.py"),
        "headless_host": os.path.join(ROOT, "headless_host.py"),
    }

    def setUp(self):
        self._saved_env = os.environ.pop("HOUDINI_MCP_ENV_DIR", None)
        _install_stubs()
        for name in self.MODULE_PATHS:
            sys.modules.pop(name, None)
        for name, path in self.MODULE_PATHS.items():
            _load_module(name, path)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("HOUDINI_MCP_ENV_DIR", None)
        else:
            os.environ["HOUDINI_MCP_ENV_DIR"] = self._saved_env
        for name in self.MODULE_PATHS:
            sys.modules.pop(name, None)

    def _expected_default(self):
        return os.path.join(ROOT, "..", f"{os.path.basename(ROOT)}-env")

    def _assert_default(self, module_name):
        mod = sys.modules.get(module_name)
        self.assertIsNotNone(mod, f"{module_name} failed to load")
        self.assertTrue(
            hasattr(mod, "_env_dir"),
            f"{module_name} has no _env_dir()",
        )
        result = mod._env_dir()
        self.assertEqual(
            os.path.normpath(result),
            os.path.normpath(self._expected_default()),
            f"{module_name}._env_dir() default derivation broken: {result!r}",
        )

    def test_default_derives_from_package_dirname(self):
        for name in self.MODULE_PATHS:
            with self.subTest(module=name):
                self._assert_default(name)

    def test_absolute_override_takes_priority(self):
        os.environ["HOUDINI_MCP_ENV_DIR"] = "D:/shared/envs/opera-env"
        for name in self.MODULE_PATHS:
            with self.subTest(module=name):
                mod = sys.modules.get(name)
                self.assertIsNotNone(mod, f"{name} failed to load")
                result = mod._env_dir()
                self.assertEqual(
                    os.path.normpath(result),
                    os.path.normpath("D:/shared/envs/opera-env"),
                    f"{name}._env_dir() did not honor absolute override",
                )

    def test_relative_override_falls_back_to_default(self):
        """Relative override is unreliable across spawn contexts (cwd varies).

        Code must silently fall back to the default derivation rather than
        building a path that depends on the calling process's cwd.
        """
        os.environ["HOUDINI_MCP_ENV_DIR"] = "./relative-env"
        for name in self.MODULE_PATHS:
            with self.subTest(module=name):
                self._assert_default(name)

    def test_empty_override_falls_back_to_default(self):
        os.environ["HOUDINI_MCP_ENV_DIR"] = ""
        for name in self.MODULE_PATHS:
            with self.subTest(module=name):
                self._assert_default(name)

    def test_whitespace_only_override_falls_back_to_default(self):
        os.environ["HOUDINI_MCP_ENV_DIR"] = "   "
        for name in self.MODULE_PATHS:
            with self.subTest(module=name):
                self._assert_default(name)


class _TestScriptsEnvDir(unittest.TestCase):
    """Verify the two test scripts' ``_env_dir()`` follow the same rule."""

    def setUp(self):
        self._saved_env = os.environ.pop("HOUDINI_MCP_ENV_DIR", None)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("HOUDINI_MCP_ENV_DIR", None)
        else:
            os.environ["HOUDINI_MCP_ENV_DIR"] = self._saved_env

    def test_hython_headless_e2e_env_dir_derives(self):
        spec = _ilu.spec_from_file_location(
            "_hyp_check", os.path.join(HERE, "hython_headless_e2e.py"))
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            os.path.normpath(mod._env_dir()),
            os.path.normpath(
                os.path.join(ROOT, "..", f"{os.path.basename(ROOT)}-env")),
        )

    def test_h21_live_resources_smoke_env_dir_derives(self):
        spec = _ilu.spec_from_file_location(
            "_h21_check", os.path.join(HERE, "h21_live_resources_smoke.py"))
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            os.path.normpath(mod._env_dir()),
            os.path.normpath(
                os.path.join(ROOT, "..", f"{os.path.basename(ROOT)}-env")),
        )


class _CsrlibEmbeddedScenario(unittest.TestCase):
    """Sanity check: under the current CsrLib layout, derived env matches
    the historical hardcoded path (``external/houdinimcp-env/``)."""

    def test_current_layout_matches_legacy_path(self):
        # ROOT = external/houdinimcp/, so basename = houdinimcp
        # env = external/houdinimcp-env/
        derived = os.path.join(ROOT, "..", f"{os.path.basename(ROOT)}-env")
        self.assertEqual(
            os.path.normpath(derived),
            os.path.normpath(os.path.join(ROOT, "..", "houdinimcp-env")),
        )


if __name__ == "__main__":
    unittest.main()
