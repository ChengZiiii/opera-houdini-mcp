"""test_opus_optional.py — OPUS 可选模块边界测试
（refactor-opus-optional-and-debt-cleanup tasks 5.1-5.4）。

验证：
- _opus.py 顶层不 import requests / dotenv / langchain（AST 断言）。
- 无 RapidAPI key 时四个 API 入口返回稳定 disabled error，且 requests /
  langchain 不进入 import 链；model names 无 key 仍可用（5.1）。
- opus_import_model_url 是 bridge Houdini relay，不依赖 _opus._is_configured()，
  _opus import 失败也不禁用 URL relay（5.2）。
- dotenv 加载顺序：先 load_dotenv(urls.env, override=False) 再读 getenv，
  process environment 优先于文件（5.3）。
- 有 key 时四个 API 调用链、签名、返回结构与基线一致；schema 无 langchain
  时 raw JSON 降级（5.4）。

纯 stdlib + unittest.mock，无 hython / hou / 真实网络。
"""
import importlib
import importlib.util as _ilu
import json
import os
import sys
import types
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OPUS_PATH = os.path.join(ROOT, "_opus.py")
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


def _is_stub_module(mod):
    """识别 ``types.ModuleType`` 风格的 stub（无 ``__file__`` 也无 ``__path__``）。

    其他测试（如 test_headless_launch）会把 no-op stub 的 requests / dotenv /
    mcp 注入 sys.modules；_opus 的延迟 import 会误拿这些 stub。本 helper 用于
    在加载前清掉它们，使 _opus 拿到真实模块（其他测试会在自身 _load_bridge
    中重新安装所需 stub，互不影响）。
    """
    return (mod is not None
            and not hasattr(mod, "__file__")
            and not hasattr(mod, "__path__"))


def _load_opus_fresh():
    """Reload _opus module fresh from source（隔离 env 状态）。"""
    # 清掉其他测试注入的 stub dotenv / requests，确保 _opus 延迟 import 拿到真实模块。
    for k in ("dotenv", "requests"):
        if _is_stub_module(sys.modules.get(k)):
            del sys.modules[k]
    key = "_opus_test_fresh"
    if key in sys.modules:
        del sys.modules[key]
    spec = _ilu.spec_from_file_location(key, OPUS_PATH)
    mod = _ilu.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _top_level_imports(source):
    """解析源码顶层 import 的模块名列表。"""
    import ast
    tree = ast.parse(source)
    names = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


class OpusTopLevelImportsTests(unittest.TestCase):
    """5.1/1.1：_opus.py 顶层不得 import requests / dotenv / langchain。"""

    def test_no_forbidden_top_level_imports(self):
        with open(OPUS_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        imports = _top_level_imports(src)
        for forbidden in ("requests", "dotenv", "langchain",
                          "langchain_classic"):
            self.assertNotIn(
                forbidden, imports,
                "_opus.py 顶层不得 import {0}（实际: {1}）".format(
                    forbidden, imports))


class FourApiEntriesNoKeyTests(unittest.TestCase):
    """5.1：无 key 时四个 API 入口返回稳定 disabled error，不 import
    requests / langchain。"""

    def setUp(self):
        # 清掉可能存在的 RapidAPI env，确保 is_configured() == False
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        for k in ("RAPIDAPI_HOST_URL", "RAPIDAPI_HOST", "RAPIDAPI_KEY"):
            os.environ.pop(k, None)
        self.mod = _load_opus_fresh()

    def tearDown(self):
        self._env_patch.stop()

    def _assert_no_requests_langchain(self, fn, *args, **kwargs):
        before = set(sys.modules.keys())
        result = fn(*args, **kwargs)
        loaded = set(sys.modules.keys()) - before
        self.assertNotIn(
            "requests", loaded,
            "无 key 路径不应 import requests")
        self.assertNotIn(
            "langchain", loaded,
            "无 key 路径不应 import langchain")
        return result

    def test_is_configured_false_without_key(self):
        self.assertFalse(self.mod.is_configured())

    def test_schema_no_key_returns_disabled(self):
        r = self._assert_no_requests_langchain(
            self.mod.get_formatted_opus_params, "Sofa")
        self.assertEqual(r["statusCode"], 500)
        self.assertIn("not configured", r["error"])

    def test_create_no_key_returns_disabled(self):
        r = self._assert_no_requests_langchain(
            self.mod.create_opus_component, "Sofa", {"x": 1}, 1)
        self.assertEqual(r["statusCode"], 500)
        self.assertIn("not configured", r["error"])

    def test_variate_no_key_returns_disabled(self):
        r = self._assert_no_requests_langchain(
            self.mod.variate_opus_result, "id", 2)
        self.assertEqual(r["statusCode"], 500)
        self.assertIn("not configured", r["error"])

    def test_check_job_status_no_key_returns_disabled(self):
        r = self._assert_no_requests_langchain(
            self.mod.get_opus_job_result, "bid")
        self.assertIn("error", r)
        self.assertIn("not configured", r["error"])

    def test_model_names_no_key_available(self):
        """5.1/1.6：model names 无 key 仍返回完整 catalog，不报 disabled。"""
        before = set(sys.modules.keys())
        names = self.mod.get_all_component_names()
        loaded = set(sys.modules.keys()) - before
        self.assertIsInstance(names, list)
        self.assertGreater(len(names), 10)
        self.assertIn("Sofa", names)
        self.assertNotIn("requests", loaded)
        self.assertNotIn("langchain", loaded)


class ImportUrlRelayIndependenceTests(unittest.TestCase):
    """5.2：opus_import_model_url 是 Houdini relay，不依赖 _opus 配置。"""

    def test_import_url_source_does_not_reference_opus_config(self):
        """AST 断言 opus_import_model_url 函数体不引用 _opus 模块变量 /
        is_configured（允许保留 'import_opus_url' 命令名等字符串字面量）。"""
        import ast
        with open(BRIDGE_PATH, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "opus_import_model_url":
                target = node
                break
        self.assertIsNotNone(target, "opus_import_model_url 未找到")
        # 收集函数体内所有 Name 节点的 id（排除字符串字面量）
        name_ids = set()
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                name_ids.add(sub.id)
        # relay wrapper 不应引用 _opus 模块变量或 is_configured
        self.assertNotIn(
            "_opus", name_ids,
            "opus_import_model_url 不应引用 _opus 模块: " + repr(sorted(name_ids)))
        self.assertNotIn(
            "is_configured", name_ids,
            "opus_import_model_url 不应检查 RapidAPI 配置")
        # 应保留 Houdini relay 行为
        body_src = ast.unparse(target)
        self.assertIn("get_houdini_connection", body_src)
        self.assertIn("import_opus_url", body_src)

    def test_import_url_runs_with_mock_connection_without_key(self):
        """5.2：无 key 时 URL import 仍执行 mock Houdini relay（不依赖配置）。"""
        # 通过 conftest stub 加载 bridge，mock 掉 get_houdini_connection
        sys.path.insert(0, ROOT)
        try:
            import tests.conftest  # noqa: stub hou + numpy
            import houdini_mcp_server as hms
            fake_conn = mock.Mock()
            fake_conn.send_command.return_value = {
                "status": "success",
                "result": {"node_path": "/obj/opus_import1"},
            }
            with mock.patch.object(hms, "get_houdini_connection",
                                   return_value=fake_conn):
                out = hms.opus_import_model_url(
                    hms.mcp, download_url="https://example.com/model.zip")
            self.assertIn("Import Result", out)
            fake_conn.send_command.assert_called_once()
            call_args = fake_conn.send_command.call_args
            self.assertEqual(call_args[0][0], "import_opus_url")
            params = call_args[0][1] if call_args[0][1] else call_args[1].get("params")
            self.assertEqual(params["url"], "https://example.com/model.zip")
        finally:
            sys.path.pop(0)


class DotenvLoadOrderTests(unittest.TestCase):
    """5.3：_load_config 先 load_dotenv(urls.env, override=False) 再读 getenv；
    process environment 优先于文件。"""

    def test_process_env_overrides_dotenv_file(self):
        """urls.env 文件写一个值，process env 写另一个；getenv 应返回 process env。"""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        mod = _load_opus_fresh()
        # 临时改 _MODULE_DIR 到 tmp，放一个 urls.env
        original_module_dir = mod._MODULE_DIR
        env_path = os.path.join(tmpdir, "urls.env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("RAPIDAPI_HOST_URL=https://from-file.example.com/\n")
            f.write("RAPIDAPI_HOST=from-file.example.com\n")
            f.write("RAPIDAPI_KEY=file-key\n")
        mod._MODULE_DIR = tmpdir
        env_patch = mock.patch.dict(os.environ, {
            "RAPIDAPI_HOST_URL": "https://from-process.example.com/",
            "RAPIDAPI_HOST": "from-process.example.com",
            "RAPIDAPI_KEY": "process-key",
        }, clear=False)
        try:
            env_patch.start()
            cfg = mod._load_config()
            # process env 应优先
            self.assertEqual(cfg["host_url"],
                             "https://from-process.example.com/")
            self.assertEqual(cfg["host"], "from-process.example.com")
            self.assertEqual(cfg["key"], "process-key")
            self.assertIsNotNone(cfg["urls"])
        finally:
            env_patch.stop()
            mod._MODULE_DIR = original_module_dir
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_dotenv_file_fills_unset_env(self):
        """无 process env 时 urls.env 文件值生效。"""
        import tempfile
        import shutil
        tmpdir = tempfile.mkdtemp()
        mod = _load_opus_fresh()
        original_module_dir = mod._MODULE_DIR
        env_path = os.path.join(tmpdir, "urls.env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("RAPIDAPI_HOST_URL=https://file-only.example.com/\n")
            f.write("RAPIDAPI_HOST=file-only.example.com\n")
            f.write("RAPIDAPI_KEY=file-only-key\n")
        mod._MODULE_DIR = tmpdir
        # 清掉 process env
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("RAPIDAPI_HOST_URL", "RAPIDAPI_HOST",
                              "RAPIDAPI_KEY")}
        env_patch = mock.patch.dict(os.environ, clean, clear=True)
        try:
            env_patch.start()
            cfg = mod._load_config()
            self.assertEqual(cfg["host_url"],
                             "https://file-only.example.com/")
            self.assertEqual(cfg["host"], "file-only.example.com")
            self.assertEqual(cfg["key"], "file-only-key")
            self.assertIsNotNone(cfg["urls"])
        finally:
            env_patch.stop()
            mod._MODULE_DIR = original_module_dir
            shutil.rmtree(tmpdir, ignore_errors=True)


class WithKeyApiChainTests(unittest.TestCase):
    """5.4：有 key 时四个 API 调用链 / 签名 / 返回结构与基线一致；
    schema 无 langchain 时 raw JSON 降级。"""

    def setUp(self):
        self.mod = _load_opus_fresh()
        self.env_patch = mock.patch.dict(os.environ, {
            "RAPIDAPI_HOST_URL": "https://opus.test.rapidapi.com/",
            "RAPIDAPI_HOST": "opus.test.rapidapi.com",
            "RAPIDAPI_KEY": "test-key-123",
        }, clear=False)
        self.env_patch.start()
        # 构造 fake requests 模块，阻止真实网络并捕获调用
        self._install_fake_requests()

    def tearDown(self):
        self.env_patch.stop()
        sys.modules.pop("_opus_test_fresh", None)
        # 清理可能被 _opus import 的 fake requests
        for k in ("requests",):
            if k in sys.modules and getattr(sys.modules[k], "__file__", None) is None:
                del sys.modules[k]

    def _install_fake_requests(self):
        """插入一个 fake requests 模块到 sys.modules，供 _opus 的
        ``import requests`` 拿到。"""
        fake = types.ModuleType("requests")
        fake_exceptions = types.ModuleType("requests.exceptions")
        class RequestException(Exception):
            pass
        class HTTPError(RequestException):
            def __init__(self, response=None):
                self.response = response
        fake_exceptions.RequestException = RequestException
        fake_exceptions.HTTPError = HTTPError
        fake.exceptions = fake_exceptions

        class _FakeResponse(object):
            def __init__(self, payload, status_code=200, text="ok"):
                self._payload = payload
                self.status_code = status_code
                self.text = text
            def json(self):
                return self._payload
            def raise_for_status(self):
                if self.status_code >= 400:
                    err = HTTPError(self)
                    raise err

        self._FakeResponse = _FakeResponse
        self.captured = []

        def _request(method, url, **kwargs):
            self.captured.append((method, url, kwargs))
            return _FakeResponse(self._next_payload())

        def _get(url, **kwargs):
            self.captured.append(("GET", url, kwargs))
            return _FakeResponse(self._next_payload())

        fake.request = _request
        fake.get = _get
        # _opus 做 ``import requests`` 再 ``requests.exceptions.RequestException``
        # 需保证 exceptions 属性可达。
        sys.modules["requests"] = fake
        sys.modules["requests.exceptions"] = fake_exceptions
        fake.exceptions = fake_exceptions

    def _next_payload(self):
        return getattr(self, "_payload", {})

    def _set_payload(self, payload):
        self._payload = payload

    def test_schema_with_key_calls_correct_endpoint_and_returns_200(self):
        """schema：GET /get_attributes_with_name?name=Sofa，返回 statusCode 200 +
        result。无 langchain 时走 raw JSON 降级（result 仍是可解析 JSON）。"""
        self._set_payload({
            "Sofa": {
                "assets": [
                    {"name": "Cushion", "parameters": [
                        {"name": "size", "range": "0..1", "type": "float"}]}
                ]
            }
        })
        r = self.mod.get_formatted_opus_params("Sofa")
        self.assertEqual(r["statusCode"], 200)
        # 至少发起了一次 GET 请求到正确 endpoint
        methods_urls = [(m, u) for (m, u, _kw) in self.captured]
        self.assertTrue(
            any(m == "GET" and "get_attributes_with_name" in u
                for (m, u) in methods_urls),
            "schema 应 GET /get_attributes_with_name: " + repr(methods_urls))
        # 请求头含 rapidapi-host / key
        _m, _u, kw = self.captured[0]
        headers = kw.get("headers", {})
        self.assertEqual(headers.get("x-rapidapi-host"),
                         "opus.test.rapidapi.com")
        self.assertEqual(headers.get("x-rapidapi-key"), "test-key-123")
        # params 含 name=Sofa
        self.assertEqual(kw.get("params", {}).get("name"), "Sofa")

    def test_create_with_key_posts_to_create_endpoint(self):
        """create：POST /create_opus_component，返回 statusCode 200 + batch_id。"""
        self._set_payload({"batch_job_id": "job-abc"})
        r = self.mod.create_opus_component("Sofa", {"size": 0.5}, 1)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(r["batch_id"], "job-abc")
        _m, u, _kw = self.captured[0]
        self.assertEqual(_m, "POST")
        self.assertIn("create_opus_component", u)

    def test_variate_with_key_posts_to_variate_endpoint(self):
        """variate：POST /variate_opus_result，返回 statusCode 200 + batch_id。"""
        self._set_payload({"batch_job_id": "var-xyz"})
        r = self.mod.variate_opus_result("base-id", 5)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(r["batch_id"], "var-xyz")
        _m, u, _kw = self.captured[0]
        self.assertEqual(_m, "POST")
        self.assertIn("variate_opus_result", u)

    def test_check_job_status_with_key_calls_get_job_result(self):
        """check-job-status：GET /get_opus_job_result?result_uid=bid。"""
        self._set_payload({"status": "done", "download_url": "http://x/y.zip"})
        r = self.mod.get_opus_job_result("bid")
        self.assertEqual(r["status"], "done")
        _m, u, kw = self.captured[0]
        self.assertEqual(_m, "GET")
        self.assertIn("get_opus_job_result", u)
        self.assertEqual(kw.get("params", {}).get("result_uid"), "bid")

    def test_schema_no_langchain_raw_json_fallback(self):
        """无 langchain 时 schema 走 raw JSON 降级：result 为可解析 JSON dict。"""
        # 测试环境无 langchain（embedded python），get_param_json 应走 fallback
        self.assertFalse(self.mod._get_langchain_parsers(),
                         "测试环境不应有 langchain")
        self._set_payload({
            "assets": [{"name": "Leg", "parameters": [
                {"name": "h", "range": "0..2", "type": "float"}]}]
        })
        r = self.mod.get_formatted_opus_params("Chair")
        self.assertEqual(r["statusCode"], 200)
        # result 必须是可解析的 JSON dict（降级路径产出的就是 JSON）
        self.assertIsInstance(r["result"], dict)


if __name__ == "__main__":
    unittest.main()
