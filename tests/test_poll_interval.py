"""perf-mcp-round3 §1 单测：QTimer 轮询间隔 env 解析矩阵。

覆盖 specs/mcp-tools delta「命令派发轮询间隔」的 ADDED requirement：
- 默认 10ms（未设 env）
- HOUDINI_MCP_POLL_INTERVAL_MS 合法值直通（含边界 1 / 1000）
- 非整数 / 越界（0、1001、负数）回退默认并打日志
- server.start() 的 timer 启动值来自 _poll_interval_ms()（源码级断言，
  防止硬编码 100ms 回归）

Stdlib unittest，无 hython / 无 MCP 实机调用（遵守子代理铁律）。
加载真实 server.py 的模式同 tests/test_execute_code_safety.py；对
requests 做成对 pop/restore 防护（round2 requests.exceptions 残桩
方法论，见 test_render_workflow._load_server_module）。
"""
import contextlib
import importlib.util as _ilu
import io
import os
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_server_module():
    """加载真实 server.py（模式同 tests/test_execute_code_safety.py）。

    只有 ``_common`` / ``_render_policy`` 从真实文件加载，其余兄弟模块
    stub（本文件只用到模块级 ``_poll_interval_ms``，不触 handler 路径）。
    requests 成对防护：若 sys.modules 里残留无 ``__file__`` 的 fake
    requests（其他测试文件注入），加载期间临时移除、finally 恢复——
    残桩会让真实 ``import requests`` 失败（2026-09-09 实测根因）。
    """
    package_name = "poll_interval_test_houdinimcp"
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
        spec = _ilu.spec_from_file_location(full_name, path)
        module = _ilu.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)

    # requests 成对防护（fake stub 无 __file__ → 临时移除）
    saved_requests = {}
    for key in ("requests", "requests.exceptions"):
        mod = sys.modules.get(key)
        if mod is not None and not hasattr(mod, "__file__"):
            saved_requests[key] = sys.modules.pop(key)
    try:
        full_name = package_name + ".server"
        spec = _ilu.spec_from_file_location(
            full_name, os.path.join(ROOT, "server.py"))
        module = _ilu.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
    finally:
        for key, value in saved_requests.items():
            sys.modules[key] = value
    return module


class PollIntervalEnvMatrixTests(unittest.TestCase):
    """env 解析矩阵：未设 / 合法 / 非整数 / 越界。"""

    @classmethod
    def setUpClass(cls):
        cls.server_mod = _load_server_module()

    def setUp(self):
        self._saved = os.environ.pop("HOUDINI_MCP_POLL_INTERVAL_MS", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._saved is None:
            os.environ.pop("HOUDINI_MCP_POLL_INTERVAL_MS", None)
        else:
            os.environ["HOUDINI_MCP_POLL_INTERVAL_MS"] = self._saved

    def _set(self, value):
        os.environ["HOUDINI_MCP_POLL_INTERVAL_MS"] = value

    def test_default_when_env_unset(self):
        self.assertEqual(self.server_mod._poll_interval_ms(), 10)

    def test_valid_values_pass_through(self):
        for raw, expected in (("10", 10), ("25", 25), ("1", 1),
                              ("1000", 1000), ("  50  ", 50)):
            self._set(raw)
            self.assertEqual(self.server_mod._poll_interval_ms(), expected,
                             "env=%r" % raw)

    def test_non_integer_falls_back_with_log(self):
        for raw in ("abc", "1.5", "", "10ms"):
            self._set(raw)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                value = self.server_mod._poll_interval_ms()
            self.assertEqual(value, 10, "env=%r" % raw)
            self.assertIn("回退默认", captured.getvalue(), "env=%r" % raw)

    def test_out_of_range_falls_back_with_log(self):
        for raw in ("0", "1001", "-5"):
            self._set(raw)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                value = self.server_mod._poll_interval_ms()
            self.assertEqual(value, 10, "env=%r" % raw)
            self.assertIn("越界", captured.getvalue(), "env=%r" % raw)

    def test_boundary_values_accepted(self):
        # 边界值本身合法（1 与 1000 都是闭区间端点）
        self._set("1")
        self.assertEqual(self.server_mod._poll_interval_ms(), 1)
        self._set("1000")
        self.assertEqual(self.server_mod._poll_interval_ms(), 1000)


class PollIntervalWiringTests(unittest.TestCase):
    """源码级断言：start() 的 timer 间隔来自 _poll_interval_ms()。"""

    def test_start_uses_helper_not_hardcoded_100(self):
        with open(os.path.join(ROOT, "server.py"), "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("timer.start(100)", source,
                         "硬编码 100ms 轮询回归（perf-mcp-round3 §1）")
        self.assertIn("interval = _poll_interval_ms()", source)
        self.assertIn("self.timer.start(interval)", source)


if __name__ == "__main__":
    unittest.main()
