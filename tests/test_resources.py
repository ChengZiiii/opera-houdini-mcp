"""test_resources.py — add-mcp-resources 协议 + 行为单测。

覆盖：
- bridge 暴露 8 个 ``@mcp.resource``（4 静态 + 4 模板），按 design D4 表
  精确匹配 URI / name / mime_type / 模板 URI 不传 mimeType；
- 经 MCP protocol 调 ``resources/list`` / ``resources/templates/list``，
  严格 4 + 4 划分，不得跨列表混入；
- ``resources/read`` 对 4 静态 + 4 实例化模板 URI 返内容项
  ``mimeType=application/json``，handler 返回预序列化 str，可解析为
  dict envelope；
- 路径 codec ``/`` / ``/obj/geo1`` / ``/obj/测试`` / ``/obj/a%b`` /
  ``/obj/a/b`` 全部 round-trip；非规范或非法 UTF-8 token 返
  ``invalid_encoded_path``；
- mock ``_houdini_call`` 核对 8 个 name 对应 cmd + 参数；
- C10 / C17 底层失败时 ``hdas`` / ``usd_stage`` 仍在 list 中、read 返
  ``backend_capability_error`` JSON、异常不逃逸 MCP transport；
- 成功 / 错误 / 超 cap 集合分别断言 matched/returned/truncated =
  cap 后真实 list 长度；超 cap 集合直接断言
  ``TextResourceContents.text`` UTF-8 字节 <= 16384。
"""
import ast
import asyncio
import importlib
import json
import os
import re
import sys
import types
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


def _ensure_real_mcp():
    """清除其他测试注入的 stub mcp 模块（test_headless_launch 等会在
    sys.modules 放无 __file__/__path__ 的 fake mcp），确保 import 到真实
    mcp 1.12.2。"""
    for key in list(sys.modules):
        if key == "mcp" or key.startswith("mcp."):
            mod = sys.modules[key]
            if not hasattr(mod, "__file__") and not hasattr(mod, "__path__"):
                del sys.modules[key]
    # 真实 mcp 1.12.2
    return importlib.import_module("mcp.server.fastmcp")


# ---------------------------------------------------------------------------
# Resource 装饰器表（design D4 单一真源）
# ---------------------------------------------------------------------------
STATIC_RESOURCES = [
    ("houdini://scene/info", "scene_info", "get_scene_info"),
    ("houdini://scene/tree", "scene_tree", "serialize_scene"),
    ("houdini://errors", "errors", "find_error_nodes"),
    ("houdini://hdas", "hdas", "hda_list"),  # C10 hard
]
TEMPLATE_RESOURCES = [
    ("houdini://scene/nodes/{encoded_path}", "scene_node", "get_node_info",
     "path"),
    ("houdini://node-types/{context}", "node_types", "list_node_types",
     "category"),
    ("houdini://geometry/{encoded_node_path}/summary", "geometry_summary",
     "get_geo_summary", "node_path"),
    ("houdini://usd/{encoded_node_path}/stage", "usd_stage",
     "lop_stage_info", "lop_path"),  # C17 hard
]


def _extract_resource_decorators(source):
    """AST 解析 ``@mcp.resource(...)`` 装饰器列表。

    返回 ``[(uri, name, function_name)]``，顺序按源码出现顺序。
    """
    tree = ast.parse(source)
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            f = dec.func
            if not (isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "mcp" and f.attr == "resource"):
                continue
            uri = None
            name = None
            if dec.args and isinstance(dec.args[0], ast.Constant):
                uri = dec.args[0].value
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = kw.value.value
            if name is None:
                name = node.name
            out.append((uri, name, node.name))
    return out


def _import_bridge_with_mock(mock_call):
    """Import ``houdini_mcp_server`` 并把 ``_houdini_call`` 替换为 mock_call。

    每次返回全新模块对象以保证 handler / resource 闭包拿到 mock；返回
    (bridge_module, mcp_instance)。
    """
    # 清理已缓存的 houdini_mcp_server / houdinimcp.houdini_mcp_server
    for k in list(sys.modules):
        if k in ("houdini_mcp_server", "houdinimcp.houdini_mcp_server"):
            del sys.modules[k]
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "..", ".."))  # 让 houdinimcp 可作 package
    srv = importlib.import_module("houdini_mcp_server")
    srv._houdini_call = mock_call
    return srv


# ---------------------------------------------------------------------------
# 1. 源码装饰器契约（AST）
# ---------------------------------------------------------------------------
class ResourceDecoratorAstTests(unittest.TestCase):
    """bridge 源码层 AST 断言 8 个 @mcp.resource 装饰器全部存在。"""

    @classmethod
    def setUpClass(cls):
        with open(BRIDGE_PATH, "r", encoding="utf-8") as f:
            cls.source = f.read()
        cls.decorators = _extract_resource_decorators(cls.source)

    def test_eight_resource_decorators(self):
        self.assertEqual(
            len(self.decorators), 8,
            f"expected 8 @mcp.resource, got {len(self.decorators)}: "
            f"{self.decorators}")

    def test_four_static_four_template(self):
        static = [d for d in self.decorators if "{" not in d[0]]
        tmpl = [d for d in self.decorators if "{" in d[0]]
        self.assertEqual(len(static), 4, f"static: {static}")
        self.assertEqual(len(tmpl), 4, f"templates: {tmpl}")

    def test_static_uris_match_design(self):
        static = {d[0]: (d[1], d[2]) for d in self.decorators
                  if "{" not in d[0]}
        for uri, name, _ in STATIC_RESOURCES:
            self.assertIn(uri, static, f"missing static uri {uri}")
            self.assertEqual(static[uri][0], name, uri)

    def test_template_uris_match_design(self):
        tmpl = {d[0]: (d[1], d[2]) for d in self.decorators
                if "{" in d[0]}
        for uri, name, _, _ in TEMPLATE_RESOURCES:
            self.assertIn(uri, tmpl, f"missing template uri {uri}")
            self.assertEqual(tmpl[uri][0], name, uri)

    def test_independent_mcp_resources_header(self):
        """独立 ``# --- mcp resources ---`` header 必须在文件中存在。"""
        self.assertIn("# --- mcp resources ---", self.source)


# ---------------------------------------------------------------------------
# 2. Path codec round-trip + invalid token
# ---------------------------------------------------------------------------
class PathCodecTests(unittest.TestCase):
    """design D3：Houdini 节点路径可逆百分号编码。"""

    GOOD_PATHS = ["/", "/obj/geo1", "/obj/测试", "/obj/a%b", "/obj/a/b"]

    def setUp(self):
        # 清除其他测试注入的 stub mcp（test_headless_launch 等会在
        # sys.modules 放无 __file__/__path__ 的 fake mcp）以确保 import 到
        # 真实 mcp 1.12.2；并清掉 houdini_mcp_server 缓存。
        for k in list(sys.modules):
            if k == "mcp" or k.startswith("mcp."):
                mod = sys.modules[k]
                if not hasattr(mod, "__file__") and not hasattr(mod, "__path__"):
                    del sys.modules[k]
        for k in list(sys.modules):
            if k in ("houdini_mcp_server", "houdinimcp.houdini_mcp_server"):
                del sys.modules[k]
        sys.path.insert(0, ROOT)
        from houdini_mcp_server import (
            _encode_houdini_path, _decode_houdini_path,
        )
        self.encode = _encode_houdini_path
        self.decode = _decode_houdini_path

    def test_canonical_round_trip(self):
        for p in self.GOOD_PATHS:
            enc = self.encode(p)
            dec = self.decode(enc)
            self.assertEqual(dec, p, p)
            self.assertEqual(self.encode(dec), enc, p)

    def test_canonical_url_escape(self):
        """``/`` 必须编码为 ``%2F``，字面 ``%`` 编码为 ``%25``。"""
        self.assertEqual(self.encode("/"), "%2F")
        self.assertEqual(self.encode("/obj/geo1"), "%2Fobj%2Fgeo1")
        self.assertEqual(self.encode("/obj/a%b"), "%2Fobj%2Fa%25b")

    def test_non_canonical_token_rejected(self):
        """``%2f`` 小写（不规范）必须拒绝。"""
        with self.assertRaises((ValueError, UnicodeError)):
            self.decode("%2fobj")  # lowercase
        with self.assertRaises((ValueError, UnicodeError)):
            self.decode("%2Fobj%2Fgeo1%2")  # trailing % without 2 hex digits

    def test_invalid_utf8_token_rejected(self):
        """非法 UTF-8 percent-encoding 序列必须拒绝。"""
        # %FF%FE 单独不构成合法 UTF-8 起始
        with self.assertRaises((ValueError, UnicodeError)):
            self.decode("%FF%FEobj")


# ---------------------------------------------------------------------------
# 3. 经 MCP protocol 验证 list/templates/read 契约
# ---------------------------------------------------------------------------
class _RecordingCallMock:
    """记录所有 ``_houdini_call`` 调用，并按 cmd 返回受控 payload。"""

    def __init__(self, payloads_by_cmd):
        self.payloads_by_cmd = payloads_by_cmd
        self.calls = []

    def __call__(self, cmd, params=None):
        self.calls.append((cmd, params or {}))
        if cmd in self.payloads_by_cmd:
            return {"status": "success", "result": self.payloads_by_cmd[cmd]}
        return {
            "status": "error",
            "message": f"no mock payload for cmd={cmd}",
            "origin": "test",
        }


def _run(coro):
    return asyncio.run(coro)


class McpListContractTests(unittest.TestCase):
    """经 MCP protocol 调 ``list_resources`` / ``list_resource_templates``。"""

    @classmethod
    def setUpClass(cls):
        _ensure_real_mcp()
        cls.bridge = _import_bridge_with_mock(
            _RecordingCallMock({
                "get_scene_info": {"scene": "ok"},
                "serialize_scene": {"tree": []},
                "find_error_nodes": {"items": []},
                "hda_list": {"hdas": []},
                "get_node_info": {"node": "ok"},
                "list_node_types": {"items": []},
                "get_geo_summary": {"geo": "ok"},
                "lop_stage_info": {"stage": "ok"},
            }))

    def _run_async(self, coro):
        return _run(coro)

    def test_resources_list_has_exactly_four_static(self):
        result = self._run_async(self.bridge.mcp.list_resources())
        uris = [str(r.uri) for r in result]
        names = [r.name for r in result]
        expected_uris = {u for u, _, _ in STATIC_RESOURCES}
        self.assertEqual(set(uris), expected_uris, uris)
        # 不得混入模板 URI
        for u in uris:
            self.assertNotIn("{", u, f"template leaked into static list: {u}")
        # 精确 name
        self.assertEqual(
            set(names), {n for _, n, _ in STATIC_RESOURCES}, names)
        # 4 静态资源 mimeType 全为 application/json
        for r in result:
            self.assertEqual(r.mimeType, "application/json",
                             f"static {r.uri} mime={r.mimeType}")

    def test_resource_templates_list_has_exactly_four(self):
        result = self._run_async(self.bridge.mcp.list_resource_templates())
        uris = [t.uriTemplate for t in result]
        names = [t.name for t in result]
        expected_templates = {u for u, _, _, _ in TEMPLATE_RESOURCES}
        self.assertEqual(set(uris), expected_templates, uris)
        for u in uris:
            self.assertIn("{", u, f"static leaked into templates: {u}")
        self.assertEqual(
            set(names), {n for _, n, _, _ in TEMPLATE_RESOURCES}, names)
        # FastMCP 1.12.2 templates 不传 mimeType → 不得要求

    def test_static_and_template_lists_disjoint(self):
        s = self._run_async(self.bridge.mcp.list_resources())
        t = self._run_async(self.bridge.mcp.list_resource_templates())
        s_uris = {str(r.uri) for r in s}
        t_uris = {tt.uriTemplate for tt in t}
        self.assertEqual(s_uris & t_uris, set())
        s_names = {r.name for r in s}
        t_names = {tt.name for tt in t}
        self.assertEqual(s_names & t_names, set())


class McpReadContractTests(unittest.TestCase):
    """经 MCP protocol 调 ``read_resource``，断言 mime + 预序列化 str。"""

    @classmethod
    def setUpClass(cls):
        _ensure_real_mcp()

    def _new_bridge(self, mock):
        return _import_bridge_with_mock(mock)

    def _read(self, bridge, uri):
        return _run(bridge.mcp.read_resource(uri))

    def test_static_read_returns_application_json(self):
        mock = _RecordingCallMock({"get_scene_info": {"scene": "ok"}})
        bridge = self._new_bridge(mock)
        items = self._read(bridge, "houdini://scene/info")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].mime_type, "application/json")
        body = json.loads(items[0].content)
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["resource"], "scene_info")
        self.assertEqual(body["dependency"], "get_scene_info")
        self.assertEqual(body["matched"], 1)
        self.assertEqual(body["returned"], 1)
        self.assertFalse(body["truncated"])
        self.assertEqual(mock.calls, [("get_scene_info", {})])

    def test_template_read_relays_decoded_path(self):
        mock = _RecordingCallMock({"get_node_info": {"node": "ok"}})
        bridge = self._new_bridge(mock)
        # /obj/geo1 → %2Fobj%2Fgeo1
        items = self._read(bridge, "houdini://scene/nodes/%2Fobj%2Fgeo1")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].mime_type, "application/json")
        body = json.loads(items[0].content)
        self.assertEqual(body["resource"], "scene_node")
        self.assertEqual(mock.calls, [("get_node_info", {"path": "/obj/geo1"})])

    def test_template_read_invalid_path_returns_error(self):
        """非规范 token 返 ``code=invalid_encoded_path``，不得 relay cmd。"""
        mock = _RecordingCallMock({})
        bridge = self._new_bridge(mock)
        # %2f 小写不规范
        items = self._read(bridge, "houdini://scene/nodes/%2fobj")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].mime_type, "application/json")
        body = json.loads(items[0].content)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "invalid_encoded_path")
        self.assertEqual(body["resource"], "scene_node")
        self.assertEqual(mock.calls, [])  # 不得调用 Houdini

    def test_hda_list_resource_uses_c10(self):
        mock = _RecordingCallMock({"hda_list": {"hdas": []}})
        bridge = self._new_bridge(mock)
        items = self._read(bridge, "houdini://hdas")
        self.assertEqual(len(items), 1)
        body = json.loads(items[0].content)
        self.assertEqual(body["resource"], "hdas")
        self.assertEqual(body["dependency"], "hda_list")
        self.assertEqual(mock.calls, [("hda_list", {})])

    def test_usd_stage_resource_uses_c17(self):
        mock = _RecordingCallMock({"lop_stage_info": {"stage": "ok"}})
        bridge = self._new_bridge(mock)
        items = self._read(bridge, "houdini://usd/%2Fobj%2Fusd/stage")
        self.assertEqual(len(items), 1)
        body = json.loads(items[0].content)
        self.assertEqual(body["resource"], "usd_stage")
        self.assertEqual(body["dependency"], "lop_stage_info")
        self.assertEqual(mock.calls,
                         [("lop_stage_info", {"lop_path": "/obj/usd"})])


# ---------------------------------------------------------------------------
# 4. 8 个 name 全部经 MCP protocol 验证 relay 正确 cmd + 参数
# ---------------------------------------------------------------------------
class RelayParamTests(unittest.TestCase):
    """8 个 resource 经 protocol 调 ``read_resource``，核对 cmd / 参数。"""

    @classmethod
    def setUpClass(cls):
        _ensure_real_mcp()

    def _read(self, bridge, uri):
        return _run(bridge.mcp.read_resource(uri))

    def test_eight_resources_relay_correctly(self):
        cases = [
            ("houdini://scene/info", "get_scene_info", {}),
            ("houdini://scene/tree", "serialize_scene", {}),
            ("houdini://errors", "find_error_nodes", {}),
            ("houdini://hdas", "hda_list", {}),
            ("houdini://scene/nodes/%2Fobj%2Fgeo1", "get_node_info",
             {"path": "/obj/geo1"}),
            ("houdini://node-types/Sop", "list_node_types",
             {"category": "Sop"}),
            ("houdini://geometry/%2Fobj%2Fgeo1/summary", "get_geo_summary",
             {"node_path": "/obj/geo1"}),
            ("houdini://usd/%2Fobj%2Fusd/stage", "lop_stage_info",
             {"lop_path": "/obj/usd"}),
        ]
        for uri, expected_cmd, expected_params in cases:
            mock = _RecordingCallMock({expected_cmd: {"ok": True}})
            bridge = _import_bridge_with_mock(mock)
            items = self._read(bridge, uri)
            self.assertEqual(len(items), 1, uri)
            self.assertEqual(items[0].mime_type, "application/json", uri)
            body = json.loads(items[0].content)
            self.assertEqual(body["status"], "success", uri)
            self.assertEqual(mock.calls, [(expected_cmd, expected_params)],
                             f"{uri} → {mock.calls}")


# ---------------------------------------------------------------------------
# 5. C10 / C17 失败时 8 资源全部仍注册，read 返 stable error 不逃逸
# ---------------------------------------------------------------------------
class BackendFailureTests(unittest.TestCase):
    """C10/C17 底层能力失败：list 仍有该 entry，read 返 stable JSON error。"""

    @classmethod
    def setUpClass(cls):
        _ensure_real_mcp()

    def _read(self, bridge, uri):
        return _run(bridge.mcp.read_resource(uri))

    def test_hdas_resource_always_listed_even_when_capability_fails(self):
        """即使 ``_houdini_call`` 返 error，``hdas`` 仍在 list + read 不抛。"""
        def _fail(cmd, params=None):
            return {"status": "error", "message": "capability unavailable",
                    "origin": "houdini"}

        bridge = _import_bridge_with_mock(_fail)
        listed = _run(bridge.mcp.list_resources())
        uris = [str(r.uri) for r in listed]
        self.assertIn("houdini://hdas", uris)
        items = self._read(bridge, "houdini://hdas")
        self.assertEqual(len(items), 1)
        body = json.loads(items[0].content)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "backend_capability_error")
        self.assertEqual(body["resource"], "hdas")
        self.assertEqual(body["dependency"], "hda_list")
        self.assertEqual(body["matched"], 0)
        self.assertEqual(body["returned"], 0)
        self.assertFalse(body["truncated"])

    def test_usd_stage_resource_always_listed_even_when_capability_fails(self):
        def _fail(cmd, params=None):
            return {"status": "error", "message": "capability unavailable",
                    "origin": "houdini"}

        bridge = _import_bridge_with_mock(_fail)
        listed = _run(bridge.mcp.list_resource_templates())
        names = [t.name for t in listed]
        self.assertIn("usd_stage", names)
        items = self._read(bridge, "houdini://usd/%2Fobj%2Fusd/stage")
        self.assertEqual(len(items), 1)
        body = json.loads(items[0].content)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "backend_capability_error")
        self.assertEqual(body["resource"], "usd_stage")
        self.assertEqual(body["dependency"], "lop_stage_info")
        self.assertEqual(body["matched"], 0)
        self.assertEqual(body["returned"], 0)
        self.assertFalse(body["truncated"])

    def test_houdini_call_exception_does_not_escape_transport(self):
        """``_houdini_call`` 抛异常时 bridge 必须返回 stable error envelope，
        不得把异常转交到 MCP transport。"""
        def _boom(cmd, params=None):
            raise RuntimeError("houdini connection lost")

        bridge = _import_bridge_with_mock(_boom)
        items = self._read(bridge, "houdini://hdas")
        self.assertEqual(len(items), 1)
        body = json.loads(items[0].content)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "backend_capability_error")
        self.assertEqual(body["resource"], "hdas")


# ---------------------------------------------------------------------------
# 6. matched/returned/truncated 计数：cap 后真实 list 长度
# ---------------------------------------------------------------------------
class EnvelopeCountTests(unittest.TestCase):
    """matched = cap 前逻辑匹配项数；returned = cap 后真实 list 长度；
    truncated = returned < matched 或 ``_truncated`` 真值。"""

    @classmethod
    def setUpClass(cls):
        _ensure_real_mcp()

    def _read(self, bridge, uri):
        return _run(bridge.mcp.read_resource(uri))

    def test_single_object_envelope_matched_returned_one(self):
        mock = _RecordingCallMock({"get_scene_info": {"scene": "ok"}})
        bridge = _import_bridge_with_mock(mock)
        items = self._read(bridge, "houdini://scene/info")
        body = json.loads(items[0].content)
        self.assertEqual(body["matched"], 1)
        self.assertEqual(body["returned"], 1)
        self.assertFalse(body["truncated"])

    def test_small_list_under_cap_no_truncation(self):
        items_data = [{"i": i, "v": "x" * 4} for i in range(10)]
        mock = _RecordingCallMock({"find_error_nodes": {"items": items_data}})
        bridge = _import_bridge_with_mock(mock)
        items = self._read(bridge, "houdini://errors")
        body = json.loads(items[0].content)
        self.assertEqual(body["matched"], 10)
        self.assertEqual(body["returned"], 10)
        self.assertFalse(body["truncated"])
        # 真实 list 长度 == data.items 长度
        self.assertEqual(len(body["data"]["items"]), 10)

    def test_oversize_payload_truncated_with_matched_gt_returned(self):
        """单 item 字符串超大，触发 cap：matched > returned, truncated=true。"""
        huge = "X" * 20000  # 单 item 远超 16KB cap
        items_data = [{"i": 0, "blob": huge}, {"i": 1, "blob": "small"}]
        mock = _RecordingCallMock({"find_error_nodes": {"items": items_data}})
        bridge = _import_bridge_with_mock(mock)
        items = self._read(bridge, "houdini://errors")
        text = items[0].content
        # 字节预算：直接断言 FastMCP 最终 text 的 UTF-8 长度
        self.assertLessEqual(
            len(text.encode("utf-8")), 16384,
            f"text {len(text.encode('utf-8'))} bytes exceeds 16KB cap")
        body = json.loads(text)
        # matched = cap 前逻辑匹配项数（2），returned = cap 后真实 list 长度
        self.assertEqual(body["matched"], 2)
        self.assertLess(body["returned"], body["matched"])
        self.assertTrue(body["truncated"])

    def test_does_not_use_preserved_count(self):
        """实现 MUST NOT 引用不存在的 ``<field>_preserved_count``。"""
        with open(BRIDGE_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("_preserved_count", src,
                         "bridge 不得引用不存在的 _preserved_count 字段")

    def test_does_not_skip_apply_response_cap(self):
        """每个 resource handler 路径都必须经过 apply_response_cap 入口。"""
        with open(BRIDGE_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        # 8 个 resource handler 全部调用 _resource_response
        names = ["scene_info_resource", "scene_tree_resource",
                 "errors_resource", "hdas_resource",
                 "scene_node_resource", "node_types_resource",
                 "geometry_summary_resource", "usd_stage_resource"]
        for fn in names:
            self.assertIn(f"def {fn}", src, fn)
        # _resource_response 走 apply_response_cap
        self.assertIn("cmn.apply_response_cap", src)


# ---------------------------------------------------------------------------
# 7. 确定性序列化 + UTF-8 字节预算 + indent/转义/Unicode 预算
# ---------------------------------------------------------------------------
class DeterministicSerializeTests(unittest.TestCase):
    """``json.dumps(..., ensure_ascii=False, sort_keys=True, 紧凑, no NaN)``"""

    @classmethod
    def setUpClass(cls):
        _ensure_real_mcp()

    def _read(self, bridge, uri):
        return _run(bridge.mcp.read_resource(uri))

    def test_text_budget_uses_utf8_bytes_not_chars(self):
        """中文 + 引号 + 反斜杠 payload 必须按 UTF-8 字节预算，不按字符数。"""
        # 用多个多字节 unicode + 引号 + 反斜杠的 item，触发 16KB cap；
        # 即使 cap 把 list 清空，剩余 envelope 也必须按 UTF-8 字节预算 ≤ 16KB
        items_data = [{
            "i": i,
            "unicode": "测试中文字符串超过预算" * 2000,  # 多字节
            "quote": 'has "double" and \\back\\slash\\',
        } for i in range(5)]
        mock = _RecordingCallMock({"find_error_nodes": {"items": items_data}})
        bridge = _import_bridge_with_mock(mock)
        items = self._read(bridge, "houdini://errors")
        text = items[0].content
        # UTF-8 字节预算断言（多字节 Unicode 一定 > 字符数）
        self.assertLessEqual(len(text.encode("utf-8")), 16384)
        # 序列化后中文是字面字符（非 \uXXXX）；cap 后 data 仍可能残留元数据
        # 直接断言 ensure_ascii=False 让中文字面保留
        self.assertNotIn("\\u", text)

    def test_no_nan_in_serialization(self):
        """NaN / inf 不得出现在 text（json 序列化的 allow_nan=False）。"""
        with open(BRIDGE_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        # 确认 ensure_ascii=False, sort_keys=True, allow_nan=False
        self.assertIn("allow_nan=False", src)
        self.assertIn("ensure_ascii=False", src)
        self.assertIn("sort_keys=True", src)
        # separators 用常量
        self.assertIn("_RESOURCE_JSON_SEP = (\",\", \":\")", src)

    def test_cap_is_called_first(self):
        """response envelope 首次必经 ``apply_response_cap``。"""
        with open(BRIDGE_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        # _resource_envelope 是 envelope/cap 入口；首次强制走 cmn.apply_response_cap
        self.assertIn("cmn.apply_response_cap(envelope)", src)


# ---------------------------------------------------------------------------
# 8. 8 个 resource read 全部为 application/json
# ---------------------------------------------------------------------------
class AllResourcesReadTests(unittest.TestCase):
    """8 个 URI（4 静态 + 4 实例化模板）read 均 application/json + 可解析。"""

    @classmethod
    def setUpClass(cls):
        _ensure_real_mcp()

    def _read(self, bridge, uri):
        return _run(bridge.mcp.read_resource(uri))

    def test_all_eight_read_application_json(self):
        uris = [
            "houdini://scene/info",
            "houdini://scene/tree",
            "houdini://errors",
            "houdini://hdas",
            "houdini://scene/nodes/%2Fobj%2Fgeo1",
            "houdini://node-types/Sop",
            "houdini://geometry/%2Fobj%2Fgeo1/summary",
            "houdini://usd/%2Fobj%2Fusd/stage",
        ]
        # 为每个 URI 提供对应 mock cmd
        mock_payloads = {
            "get_scene_info": {"scene": "ok"},
            "serialize_scene": {"tree": []},
            "find_error_nodes": {"items": []},
            "hda_list": {"hdas": []},
            "get_node_info": {"node": "ok"},
            "list_node_types": {"items": []},
            "get_geo_summary": {"geo": "ok"},
            "lop_stage_info": {"stage": "ok"},
        }
        mock = _RecordingCallMock(mock_payloads)
        bridge = _import_bridge_with_mock(mock)
        for uri in uris:
            items = self._read(bridge, uri)
            self.assertEqual(len(items), 1, uri)
            self.assertEqual(items[0].mime_type, "application/json", uri)
            # 预序列化 str 必须可解析为 dict envelope
            body = json.loads(items[0].content)
            self.assertIsInstance(body, dict, uri)
            self.assertIn("status", body, uri)
            self.assertIn("resource", body, uri)
            self.assertIn("matched", body, uri)
            self.assertIn("returned", body, uri)
            self.assertIn("truncated", body, uri)


if __name__ == "__main__":
    unittest.main()
