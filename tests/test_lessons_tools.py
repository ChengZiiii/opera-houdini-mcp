"""test_lessons_tools.py — bridge 4 个 lessons MCP 工具 + 自动捕获 hook 测试
（add-self-evolving-knowledge-base tasks 6.6 / 6.7）。

覆盖：
1. 工具接线：search_lessons 在 seed published lesson + root recipes 后返回
   success envelope；category/severity 过滤透传；未知 scope → ls_unknown_root。
2. save_lesson：成功 → 个人库 draft，id L-YYYYMMDD-NNN，strength 1；同 symptom
   再次保存 → strength 2 且内容保留；非法 severity → 结构化错误（列出合法值）；
   团队 root writable=false → root_not_writable 零写入。
3. read_lesson：全文 markdown（含 ## Problem/## Symptom/## Fix + front matter）；
   未知 id → ls_lesson_not_found（提示先 search_lessons）；draft 可读。
4. knowledge_stats：seed lessons + inbox 事件后计数正确；registry root
   unavailable → 状态上报 + search_lessons _warning 透传。
5. 捕获 hook 走真实 MCP 协议（create_connected_server_and_client_session 挂
   bridge 的 mcp 实例）：status=error 工具 → inbox/events.jsonl 记录一条事件
   （tool + code 正确）；success 工具 → 不新增事件；响应体与工具直返字节一致
   （hook 不改响应）。
6. 捕获降级：record_error_event 抛异常 → 工具响应原样返回，不被打断。
7. 4 个工具存在 + docstring 含触发时机关键词（触发时机/先调用本工具检索）
   与 advisory 关键词（不替代）。

全部使用 TemporaryDirectory + monkeypatch `_lessons._base_dir`（绝不触碰
真实 ~/.opera-houdini-mcp）。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _lessons  # noqa: E402
import _lessons_search as lssearch  # noqa: E402

BRIDGE_PATH = os.path.join(ROOT, "houdini_mcp_server.py")


# ---------------------------------------------------------------------------
# seed helpers（与 test_lessons_search.py 同款渲染，保证可 parse_lesson round-trip）
# ---------------------------------------------------------------------------
def _iso_ago(days):
    """返回距 now 恰好 days 天的 ISO 8601 字符串（带 tz offset）。"""
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat()


def _render_lesson(lesson):
    """把 lesson dict 渲染为可 parse_lesson 的 markdown 文本。"""
    lines = ["---"]
    for key in ("id", "status", "strength", "root", "created_at", "updated_at",
                "category", "severity", "affected_versions",
                "verified_versions", "source", "advisory", "fingerprint"):
        value = lesson.get(key)
        if value is None:
            continue
        if key in ("created_at", "updated_at"):
            lines.append('{0}: "{1}"'.format(key, value))
        elif key == "advisory":
            lines.append("advisory: " + ("true" if value else "false"))
        elif key == "strength":
            lines.append("strength: {0}".format(int(value)))
        else:
            lines.append("{0}: {1}".format(key, value))
    lines.append("---")
    lines.append("")
    lines.append("# " + lesson["title"])
    for section in ("problem", "symptom", "fix"):
        lines.append("")
        lines.append("## " + section.capitalize())
        lines.append(lesson[section])
    return "\n".join(lines) + "\n"


def _seed_lesson_file(root_path, lesson):
    """把 lesson dict 落盘到 root_path/lessons/<id>.md。"""
    path = os.path.join(root_path, "lessons", lesson["id"] + ".md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_render_lesson(lesson))
    return path


def _seed_recipes(root_path):
    """在 root_path/recipes/BEST_PRACTICES.md 写一条含 alpha 的 recipe。"""
    text = (
        "# personal recipes\n\n"
        "> advisory\n\n"
        "### BP-001\n\n"
        "- category: rendering\n"
        "- severity: high\n"
        "- affected_versions: H21.0\n"
        "- verified_versions: H21.0 fork live smoke\n"
        "- source: tests/unit\n"
        "- advisory: true\n"
        "- problem: the problem alpha\n"
        "- symptom: the symptom alpha\n"
        "- fix: the fix alpha\n"
    )
    path = os.path.join(root_path, "recipes", "BEST_PRACTICES.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _seed_published(root_path, lesson_id, title="Test lesson alpha"):
    """seed 一条 published lesson（默认今日 id + alpha 内容）。"""
    return _seed_lesson_file(root_path, {
        "id": lesson_id,
        "status": "published",
        "strength": 1,
        "root": "personal",
        "created_at": _iso_ago(10),
        "updated_at": _iso_ago(2),
        "category": "rendering",
        "severity": "high",
        "affected_versions": "H21, H22",
        "verified_versions": "H21.0",
        "source": "unit-test",
        "advisory": True,
        "fingerprint": "a" * 64,
        "title": title,
        "problem": "Problem body alpha",
        "symptom": "camera near plane large",
        "fix": "Fix body alpha",
    })


# ---------------------------------------------------------------------------
# bridge 隔离加载（test_best_practices._load_bp 模式 + 清除 stub mcp）
# ---------------------------------------------------------------------------
def _purge_stub_mcp():
    """清除其他测试注入的 stub mcp（无 __file__/__path__），确保 import 到
    真实 mcp 1.12.2（test_headless_launch 会在 sys.modules 放 fake mcp）。"""
    for key in list(sys.modules):
        if key == "mcp" or key.startswith("mcp."):
            mod = sys.modules[key]
            if not hasattr(mod, "__file__") and not hasattr(mod, "__path__"):
                del sys.modules[key]


def _load_bridge():
    """以独立 module name 加载 bridge；flat import 复用测试进程内的真实
    _lessons / _lessons_search 模块对象（monkeypatch _base_dir 因此可见）。"""
    _purge_stub_mcp()
    name = "test_lessons_tools_bridge_module"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_bridge = None


def _get_bridge():
    global _bridge
    if _bridge is None:
        _bridge = _load_bridge()
    return _bridge


def _events_path(base):
    """个人库 inbox 事件文件路径。"""
    return os.path.join(base, "knowledge", "inbox", "events.jsonl")


def _read_events(base):
    """读 inbox 事件列表（逐行 JSON），文件不存在返回 []。"""
    path = _events_path(base)
    if not os.path.isfile(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if stripped:
                events.append(json.loads(stripped))
    return events


def _session_call(bridge, name, arguments):
    """经真实 MCP 内存会话调用工具，返回 (client_result, direct_env, expected)。

    client_result：client.call_tool 的 CallToolResult；direct_env：直接调用
    工具函数（绕过 hook）得到的 dict；expected：协议侧序列化文本
    （mcp 的 pydantic_core.to_json 默认 ensure_ascii=False，与
    json.dumps(..., indent=2, ensure_ascii=False) 对纯 JSON 数据一致）。
    响应体必须与 expected 字节一致（hook 不改响应）。
    """
    from mcp.shared.memory import create_connected_server_and_client_session
    import asyncio

    async def _run():
        async with create_connected_server_and_client_session(
                bridge.mcp._mcp_server) as client:
            await client.initialize()
            return await client.call_tool(name, arguments)

    result = asyncio.run(_run())
    direct = getattr(bridge, name)(None, **arguments)
    expected = json.dumps(direct, indent=2, ensure_ascii=False)
    return result, direct, expected


def _protocol_call(bridge, name, arguments):
    """仅走真实 MCP 内存会话调用（不做 direct 对比），返回 client 的
    CallToolResult；工具抛异常时由 mcp 协议包装为 isError 结果。"""
    from mcp.shared.memory import create_connected_server_and_client_session
    import asyncio

    async def _run():
        async with create_connected_server_and_client_session(
                bridge.mcp._mcp_server) as client:
            await client.initialize()
            return await client.call_tool(name, arguments)

    return asyncio.run(_run())


class LessonsToolsBase(unittest.TestCase):
    """所有测试的基类：每个测试独立 tempdir + _base_dir 指向它。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self._patch = mock.patch.object(_lessons, "_base_dir",
                                        return_value=self.base)
        self._patch.start()
        lssearch.clear_cache()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def personal(self):
        """personal root 路径（base/knowledge）。"""
        return os.path.join(self.base, "knowledge")


# ---------------------------------------------------------------------------
# task 6.6（1）：工具接线 — search_lessons
# ---------------------------------------------------------------------------
class SearchLessonsToolTests(LessonsToolsBase):

    def test_seeded_lesson_and_recipe_return_success(self):
        bridge = _get_bridge()
        lesson_id = "L-{0}-001".format(datetime.now().strftime("%Y%m%d"))
        _seed_published(self.personal(), lesson_id)
        _seed_recipes(self.personal())

        env = bridge.search_lessons(None, query="alpha")
        self.assertEqual(env["status"], "success")
        self.assertGreater(env["matched"], 0)
        kinds = set(r["kind"] for r in env["results"])
        self.assertIn("lesson", kinds)
        self.assertIn("recipe", kinds)
        for result in env["results"]:
            self.assertEqual(result["source_root"], "personal")

    def test_category_and_severity_filters_pass_through(self):
        bridge = _get_bridge()
        lesson_id = "L-{0}-001".format(datetime.now().strftime("%Y%m%d"))
        _seed_published(self.personal(), lesson_id)
        _seed_recipes(self.personal())

        env = bridge.search_lessons(None, query="alpha", category="rendering")
        self.assertEqual(env["status"], "success")
        self.assertGreater(env["matched"], 0)
        env = bridge.search_lessons(None, query="alpha", category="shading")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["matched"], 0)
        env = bridge.search_lessons(None, query="alpha", severity="critical")
        self.assertEqual(env["matched"], 0)

    def test_unknown_scope_returns_ls_unknown_root(self):
        bridge = _get_bridge()
        env = bridge.search_lessons(None, query="alpha", scope="bogus_root")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_unknown_root")

    def test_hook_inert_for_direct_calls(self):
        """直接调用工具函数绕过 hook：error envelope 也不写 inbox。"""
        bridge = _get_bridge()
        env = bridge.search_lessons(None, query="alpha", scope="bogus_root")
        self.assertEqual(env["status"], "error")
        self.assertFalse(os.path.exists(_events_path(self.base)))


# ---------------------------------------------------------------------------
# task 6.6（2）：save_lesson
# ---------------------------------------------------------------------------
class SaveLessonToolTests(LessonsToolsBase):

    def _save(self, bridge, **overrides):
        fields = {
            "problem": "Problema X",
            "symptom": "unique symptom xyz",
            "fix": "Fix it now",
            "category": "rendering",
            "severity": "high",
            "affected_versions": "H21",
        }
        fields.update(overrides)
        return bridge.save_lesson(None, **fields)

    def test_success_writes_draft_to_personal_root(self):
        bridge = _get_bridge()
        env = self._save(bridge)
        self.assertEqual(env["status"], "success")
        self.assertRegex(env["lesson_id"], r"^L-\d{8}-\d{3,}$")
        self.assertEqual(env["lesson_status"], "draft")
        self.assertEqual(env["strength"], 1)
        self.assertEqual(env["root"], "personal")
        file_path = os.path.join(self.personal(), "lessons",
                                 env["lesson_id"] + ".md")
        self.assertTrue(os.path.isfile(file_path))

    def test_same_symptom_accumulates_strength_and_preserves_content(self):
        bridge = _get_bridge()
        first = self._save(bridge)
        second = self._save(bridge)
        self.assertEqual(first["lesson_id"], second["lesson_id"])
        self.assertEqual(second["strength"], 2)
        # 文件内容保留：problem/fix 未被覆盖
        file_path = os.path.join(self.personal(), "lessons",
                                 second["lesson_id"] + ".md")
        with open(file_path, "r", encoding="utf-8") as handle:
            lesson = _lessons.parse_lesson(handle.read())
        self.assertEqual(lesson["problem"], "Problema X")
        self.assertEqual(lesson["fix"], "Fix it now")
        self.assertEqual(lesson["strength"], 2)

    def test_invalid_severity_returns_actionable_error(self):
        bridge = _get_bridge()
        env = self._save(bridge, severity="bogus")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_write_error")
        self.assertIn("severity", env["error"]["message"])
        # 合法取值在 details.valid（消息引用校验错误，避免与
        # _lessons._validate_save_fields 重复措辞）
        valid = env["error"]["details"]["valid"]
        self.assertIn("low", valid)
        self.assertIn("critical", valid)
        # 零写入：lessons 目录不存在或为空
        lessons_dir = os.path.join(self.personal(), "lessons")
        self.assertFalse(
            os.path.isdir(lessons_dir) and os.listdir(lessons_dir))

    def test_team_root_writable_false_returns_root_not_writable(self):
        bridge = _get_bridge()
        os.makedirs(os.path.join(self.base, "teamx"))
        with open(os.path.join(self.base, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump([{"name": "teamx", "path": "teamx",
                        "priority": 0.5, "writable": False}], handle)
        env = self._save(bridge, root="teamx")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "root_not_writable")
        # 零写入
        lessons_dir = os.path.join(self.base, "teamx", "lessons")
        self.assertFalse(
            os.path.isdir(lessons_dir) and os.listdir(lessons_dir))


# ---------------------------------------------------------------------------
# task 6.6（3）：read_lesson
# ---------------------------------------------------------------------------
class ReadLessonToolTests(LessonsToolsBase):

    def test_full_markdown_returned(self):
        bridge = _get_bridge()
        env = bridge.save_lesson(
            None, problem="Problema X", symptom="unique symptom xyz",
            fix="Fix it now", category="rendering", severity="high",
            affected_versions="H21")
        self.assertEqual(env["status"], "success")

        read = bridge.read_lesson(None, env["lesson_id"])
        self.assertEqual(read["status"], "success")
        self.assertEqual(read["id"], env["lesson_id"])
        md = read["markdown"]
        self.assertTrue(md.startswith("---"))
        self.assertIn("id: {0}".format(env["lesson_id"]), md)
        self.assertIn("## Problem", md)
        self.assertIn("## Symptom", md)
        self.assertIn("## Fix", md)

    def test_draft_readable_by_id(self):
        bridge = _get_bridge()
        env = bridge.save_lesson(
            None, problem="P", symptom="draft symptom 42",
            fix="F", category="api", severity="low",
            affected_versions="H21")
        self.assertEqual(env["lesson_status"], "draft")
        read = bridge.read_lesson(None, env["lesson_id"])
        self.assertEqual(read["status"], "success")
        self.assertIn("draft symptom 42", read["markdown"])

    def test_unknown_id_returns_ls_lesson_not_found(self):
        bridge = _get_bridge()
        read = bridge.read_lesson(None, "L-19990101-999")
        self.assertEqual(read["status"], "error")
        self.assertEqual(read["error"]["code"], "ls_lesson_not_found")
        self.assertIn("search_lessons", read["error"]["message"])


# ---------------------------------------------------------------------------
# task 6.6（4）：knowledge_stats
# ---------------------------------------------------------------------------
class KnowledgeStatsToolTests(LessonsToolsBase):

    def setUp(self):
        super(KnowledgeStatsToolTests, self).setUp()
        lesson_id = "L-{0}-001".format(datetime.now().strftime("%Y%m%d"))
        _seed_published(self.personal(), lesson_id)
        _seed_recipes(self.personal())
        # draft：走真实存储 API 写入
        _lessons.save_lesson(self.personal(), {
            "title": "draft seed",
            "category": "unclassified",
            "severity": "medium",
            "affected_versions": "unknown",
            "verified_versions": "unknown",
            "source": "unit-test",
            "advisory": False,
            "problem": "",
            "symptom": "draft seed symptom",
            "fix": "",
        })
        # inbox 事件 ×2（不同 message → 2 条记录）
        _lessons.record_error_event(self.personal(), tool="t1",
                                    error_code="e1",
                                    message="stats message one")
        _lessons.record_error_event(self.personal(), tool="t2",
                                    error_code="e2",
                                    message="stats message two")

    def test_counts_correct_after_seeding(self):
        bridge = _get_bridge()
        stats = bridge.knowledge_stats(None, scope="personal")
        self.assertEqual(stats["status"], "success")
        self.assertEqual(len(stats["roots"]), 1)
        entry = stats["roots"][0]
        self.assertEqual(entry["name"], "personal")
        self.assertEqual(entry["state"], "ok")
        self.assertEqual(entry["lesson_count"], 2)
        self.assertEqual(entry["draft_count"], 1)
        self.assertEqual(entry["published_count"], 1)
        self.assertEqual(entry["inbox_count"], 2)
        self.assertEqual(entry["recipes_count"], 1)

    def test_unavailable_registry_root_reported_with_warning(self):
        bridge = _get_bridge()
        os.environ["TEAMX_DIR"] = os.path.join(self.base, "missing-team-dir")
        try:
            with open(os.path.join(self.base, "config.json"), "w",
                      encoding="utf-8") as handle:
                json.dump([{"name": "teamx", "path": "${TEAMX_DIR}",
                            "priority": 0.5, "writable": False}], handle)
            stats = bridge.knowledge_stats(None, scope="all")
            self.assertEqual(stats["status"], "success")
            names = [r["name"] for r in stats["roots"]]
            self.assertIn("teamx", names)
            teamx = next(r for r in stats["roots"] if r["name"] == "teamx")
            self.assertEqual(teamx["state"], "unavailable")
            self.assertIn("_warning", stats)
            self.assertTrue(any("teamx" in w for w in stats["_warning"]))

            # search_lessons 同样透传 _warning
            env = bridge.search_lessons(None, query="alpha", scope="all")
            self.assertEqual(env["status"], "success")
            self.assertIn("_warning", env)
            self.assertTrue(any("teamx" in w for w in env["_warning"]))
        finally:
            os.environ.pop("TEAMX_DIR", None)

    def test_unknown_scope_returns_ls_unknown_root(self):
        bridge = _get_bridge()
        stats = bridge.knowledge_stats(None, scope="nope_root")
        self.assertEqual(stats["status"], "error")
        self.assertEqual(stats["error"]["code"], "ls_unknown_root")


# ---------------------------------------------------------------------------
# task 6.7（5）：捕获 hook 走真实 MCP 协议
# ---------------------------------------------------------------------------
class CaptureHookProtocolTests(LessonsToolsBase):

    def test_error_tool_records_event_then_success_tool_does_not(self):
        bridge = _get_bridge()

        # 1) error 工具 → 记录一条事件
        result, direct, expected = _session_call(
            bridge, "search_lessons", {"query": "x", "scope": "bogus_root"})
        self.assertEqual(direct["status"], "error")
        # 响应体与工具直返字节一致（hook 不改响应）
        self.assertEqual(result.content[0].text, expected)
        events = _read_events(self.base)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool"], "search_lessons")
        self.assertEqual(events[0]["error_code"], "ls_unknown_root")
        self.assertEqual(events[0]["source"], "bridge-capture")
        self.assertGreaterEqual(events[0]["count"], 1)

        # 2) success 工具 → 不新增事件
        result, direct, expected = _session_call(
            bridge, "knowledge_stats", {"scope": "personal"})
        self.assertEqual(direct["status"], "success")
        self.assertEqual(result.content[0].text, expected)
        events = _read_events(self.base)
        self.assertEqual(len(events), 1)

    def test_old_shape_error_code_extracted_from_origin(self):
        """旧 shape（顶层 message/origin，无 error 子 dict）也能捕获。

        load_scene 是既有 relay 工具：无 Houdini 连接时经 _houdini_call
        返回 {"status": "error", "message": ..., "origin": "connection"}。
        """
        bridge = _get_bridge()
        result, direct, expected = _session_call(
            bridge, "load_scene", {"file_path": "C:/nope.hip"})
        self.assertEqual(direct["status"], "error")
        self.assertEqual(result.content[0].text, expected)
        events = _read_events(self.base)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool"], "load_scene")
        self.assertEqual(events[0]["error_code"], direct["origin"])


# ---------------------------------------------------------------------------
# task 6.7（6）：捕获降级 — record_error_event 抛异常也不打断响应
# ---------------------------------------------------------------------------
class CaptureHookDegradationTests(LessonsToolsBase):

    def test_record_error_event_raising_is_swallowed(self):
        bridge = _get_bridge()
        with mock.patch.object(_lessons, "record_error_event",
                               side_effect=RuntimeError("kaput")):
            result, direct, expected = _session_call(
                bridge, "search_lessons", {"query": "x", "scope": "bogus_root"})
        self.assertEqual(direct["status"], "error")
        # 原响应未被修改（字节一致）
        self.assertEqual(result.content[0].text, expected)
        # 没有事件被写入（抛异常被吞掉）
        self.assertFalse(os.path.exists(_events_path(self.base)))


# ---------------------------------------------------------------------------
# compose-review MINOR 1：错误 envelope 同样过 apply_response_cap
# （错误路径此前绕过 cap；修复后 error 返回与 success 同走 _lessons_capped）
# ---------------------------------------------------------------------------
class ErrorEnvelopeCapTests(LessonsToolsBase):
    """monkeypatch cmn.apply_response_cap 为 recorder，断言每个错误返回路径
    恰好调用一次 cap，且传入的 error envelope 原样返回（行为不变）。"""

    def _recorder(self, calls):
        def _cap(payload):
            calls.append(payload)
            return payload
        return _cap

    def test_search_lessons_error_envelope_passes_through_cap(self):
        bridge = _get_bridge()
        calls = []
        with mock.patch.object(bridge.cmn, "apply_response_cap",
                               side_effect=self._recorder(calls)):
            env = bridge.search_lessons(None, query="x", scope="bogus_root")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_unknown_root")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], env)
        self.assertEqual(calls[0]["status"], "error")

    def test_read_lesson_not_found_envelope_passes_through_cap(self):
        bridge = _get_bridge()
        calls = []
        with mock.patch.object(bridge.cmn, "apply_response_cap",
                               side_effect=self._recorder(calls)):
            env = bridge.read_lesson(None, "L-19990101-999")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_lesson_not_found")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], env)

    def test_save_lesson_severity_envelope_passes_through_cap(self):
        bridge = _get_bridge()
        calls = []
        with mock.patch.object(bridge.cmn, "apply_response_cap",
                               side_effect=self._recorder(calls)):
            env = bridge.save_lesson(
                None, problem="P", symptom="S", fix="F", category="c",
                severity="bogus", affected_versions="H21")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_write_error")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], env)

    def test_knowledge_stats_error_envelope_passes_through_cap(self):
        bridge = _get_bridge()
        calls = []
        with mock.patch.object(bridge.cmn, "apply_response_cap",
                               side_effect=self._recorder(calls)):
            env = bridge.knowledge_stats(None, scope="nope_root")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_unknown_root")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], env)


# ---------------------------------------------------------------------------
# compose-review MINOR 2(a)：工具函数自身抛异常 → hook 捕获后原样重抛
# ---------------------------------------------------------------------------
class ToolExceptionReraiseTests(LessonsToolsBase):

    def test_tool_exception_recorded_and_surfaces_as_error(self):
        """patch 注册工具的 fn 抛 RuntimeError：hook 记录 tool_exception
        事件（含工具名），随后原样重抛；mcp 协议把异常包装为 isError 结果
        返回客户端（响应不被 hook 吞掉）。"""
        bridge = _get_bridge()
        tool = bridge.mcp._tool_manager.get_tool("search_lessons")
        self.assertIsNotNone(tool)
        with mock.patch.object(tool, "fn",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            result = _protocol_call(bridge, "search_lessons", {"query": "x"})
        # 客户端收到错误结果（mcp 包装的异常文本，包含原始异常消息）
        self.assertTrue(result.isError)
        text = result.content[0].text
        self.assertIn("Error executing tool search_lessons", text)
        self.assertIn("boom", text)
        # inbox 记录一条 tool_exception 事件（工具名 + 异常类名）
        events = _read_events(self.base)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool"], "search_lessons")
        self.assertEqual(events[0]["error_code"], "tool_exception")
        self.assertEqual(events[0]["source"], "bridge-capture")
        self.assertIn("Error executing tool search_lessons",
                      events[0]["message"])


# ---------------------------------------------------------------------------
# compose-review MINOR 2(b)：ls_internal_error envelope 不泄漏 traceback
# ---------------------------------------------------------------------------
class InternalErrorEnvelopeTests(LessonsToolsBase):

    def test_internal_error_envelope_hides_traceback_and_raw_message(self):
        """delegate 抛非 LessonsError 异常 → 桥返回 ls_internal_error
        envelope；message 只含异常类名，不含 traceback 与原始异常文本。"""
        bridge = _get_bridge()
        with mock.patch.object(
                lssearch, "search_lessons",
                side_effect=RuntimeError("secret detail 42")):
            env = bridge.search_lessons(None, query="x")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_internal_error")
        self.assertEqual(env["error"]["details"], {})
        self.assertIn("RuntimeError", env["error"]["message"])
        self.assertNotIn("secret detail 42", env["error"]["message"])
        self.assertNotIn("Traceback", env["error"]["message"])


# ---------------------------------------------------------------------------
# task 6.7（7）：4 工具存在 + docstring 触发时机 / advisory 关键词
# ---------------------------------------------------------------------------
class LessonsToolsDocstringTests(LessonsToolsBase):

    TOOLS = ("search_lessons", "save_lesson", "read_lesson",
             "knowledge_stats")

    def test_four_tools_registered_on_bridge(self):
        bridge = _get_bridge()
        registered = set()
        for tool in bridge.mcp._tool_manager.list_tools():
            registered.add(tool.name)
        for name in self.TOOLS:
            self.assertIn(name, registered)
            self.assertTrue(callable(getattr(bridge, name)))

    def test_docstrings_contain_trigger_timing_and_advisory_keywords(self):
        bridge = _get_bridge()
        for name in self.TOOLS:
            doc = getattr(bridge, name).__doc__ or ""
            self.assertTrue(
                "触发时机" in doc or "先调用本工具检索" in doc,
                "{0} docstring 缺少触发时机关键词".format(name))
            self.assertIn(
                "不替代", doc,
                "{0} docstring 缺少 advisory 关键词（不替代）".format(name))


# ---------------------------------------------------------------------------
# add-workflow-knowledge-capture（tasks 4.4）：2 个新工具 + 行为注解 docstring
# ---------------------------------------------------------------------------
class WorkflowKnowledgeToolRegistrationTests(LessonsToolsBase):
    """新工具注册 + docstring 触发时机 / advisory 关键词 + 既有工具注解增补。

    覆盖 tasks 4.4：capture_workflow_snapshot / save_recipe 在 bridge 可调用，
    docstring 含触发时机关键词（沉淀 / 触发时机）与 advisory 关键词（不替代）；
    save_lesson / search_lessons docstring 含「主动沉淀工作流」注解片段
    （capture_workflow_snapshot）；read_lesson / knowledge_stats 零改动
    （不含该注解）。
    """

    NEW_TOOLS = ("capture_workflow_snapshot", "save_recipe")
    ANNOTATED_TOOLS = ("save_lesson", "search_lessons")
    UNTOUCHED_TOOLS = ("read_lesson", "knowledge_stats")

    def test_new_tools_registered_and_callable(self):
        bridge = _get_bridge()
        registered = set()
        for tool in bridge.mcp._tool_manager.list_tools():
            registered.add(tool.name)
        for name in self.NEW_TOOLS:
            self.assertIn(name, registered)
            self.assertTrue(callable(getattr(bridge, name)))

    def test_new_tool_docstrings_contain_trigger_and_advisory_keywords(self):
        bridge = _get_bridge()
        for name in self.NEW_TOOLS:
            doc = getattr(bridge, name).__doc__ or ""
            self.assertTrue(
                "沉淀" in doc or "触发时机" in doc,
                "{0} docstring 缺少触发时机关键词".format(name))
            self.assertIn(
                "不替代", doc,
                "{0} docstring 缺少 advisory 关键词（不替代）".format(name))

    def test_annotated_tool_docstrings_mention_capture_flow(self):
        """save_lesson / search_lessons docstring 增补主动沉淀工作流注解。"""
        bridge = _get_bridge()
        for name in self.ANNOTATED_TOOLS:
            doc = getattr(bridge, name).__doc__ or ""
            self.assertIn(
                "capture_workflow_snapshot", doc,
                "{0} docstring 缺少主动沉淀工作流注解".format(name))

    def test_docstrings_contain_methodology_and_update_keywords(self):
        """improve-knowledge-capture（tasks 4.3）：docstring 方法论协议。

        save_recipe / capture_workflow_snapshot docstring 含方法论 / 原地
        更新 / 资产标识 / 禁路径关键词；save_lesson / search_lessons 含
        加深 / 原地更新引导。
        """
        bridge = _get_bridge()
        for name in self.NEW_TOOLS:
            doc = getattr(bridge, name).__doc__ or ""
            self.assertIn("方法论", doc,
                          "{0} docstring 缺少方法论协议".format(name))
            self.assertIn("本机路径", doc,
                          "{0} docstring 缺少禁路径协议".format(name))
        save_doc = (bridge.save_recipe.__doc__ or "")
        self.assertIn("recipe_id", save_doc)
        self.assertIn("原地更新", save_doc)
        self.assertIn("不得新增一条重复知识", save_doc)
        capture_doc = (bridge.capture_workflow_snapshot.__doc__ or "")
        self.assertIn("include_hda_internals", capture_doc)
        self.assertIn("type_full", capture_doc)
        for name in self.ANNOTATED_TOOLS:
            doc = getattr(bridge, name).__doc__ or ""
            self.assertIn("加深", doc,
                          "{0} docstring 缺少加深引导".format(name))
            self.assertIn("原地更新", doc,
                          "{0} docstring 缺少原地更新引导".format(name))

    def test_untouched_tool_docstrings_have_no_capture_annotation(self):
        """read_lesson / knowledge_stats 零改动：不含主动沉淀注解。"""
        bridge = _get_bridge()
        for name in self.UNTOUCHED_TOOLS:
            doc = getattr(bridge, name).__doc__ or ""
            self.assertNotIn(
                "capture_workflow_snapshot", doc,
                "{0} docstring 不应含主动沉淀注解（零改动）".format(name))


# ---------------------------------------------------------------------------
# add-workflow-knowledge-capture（tasks 4.4）：save_recipe 端到端
# ---------------------------------------------------------------------------
class SaveRecipeToolTests(LessonsToolsBase):

    def _save(self, bridge, **overrides):
        fields = {
            "title": "Custom HDA usage flow",
            "problem": "how to wire the custom hda",
            "symptom": "recipe symptom zeta unique",
            "fix": "connect the output to a null",
            "category": "workflow",
            "severity": "high",
            "affected_versions": "H21",
        }
        fields.update(overrides)
        return bridge.save_recipe(None, **fields)

    def test_success_writes_personal_and_is_immediately_searchable(self):
        bridge = _get_bridge()
        env = self._save(bridge)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["recipe_id"], "BP-001")
        self.assertEqual(env["root"], "personal")
        self.assertEqual(env["severity"], "high")
        self.assertEqual(env["source"], "agent")
        self.assertTrue(env["immediately_searchable"])
        # 落盘校验：title 渲染为块上方 `> title` 注释行（位于 ### BP-001 之前）
        path = os.path.join(self.personal(), "recipes",
                            "BEST_PRACTICES.md")
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("> Custom HDA usage flow", text)
        self.assertLess(text.index("> Custom HDA usage flow"),
                        text.index("### BP-001"))
        # 写入即被 search_lessons 命中（recipes 通道无 draft 门槛）
        found = bridge.search_lessons(None, query="zeta")
        self.assertEqual(found["status"], "success")
        ids = [r["id"] for r in found["results"]]
        self.assertIn("BP-001", ids)
        hit = next(r for r in found["results"] if r["id"] == "BP-001")
        self.assertEqual(hit["kind"], "recipe")
        self.assertEqual(hit["source_root"], "personal")

    def test_second_write_increments_to_bp_002(self):
        bridge = _get_bridge()
        first = self._save(bridge)
        # 第二条带真实 title：追加到已有块的文件时 title 不落盘（strict
        # parser 只允许首个 heading 前的 `>` 行，块间 `> title` 会被判为
        # 前一块的非法正文），但照常校验并返回
        second = self._save(bridge, title="Second zeta flow",
                            symptom="second zeta flow")
        self.assertEqual(first["recipe_id"], "BP-001")
        self.assertEqual(second["recipe_id"], "BP-002")
        self.assertEqual(second["status"], "success")
        # 文件只有首块的一条 `> title`，没有第二条
        path = os.path.join(self.personal(), "recipes",
                            "BEST_PRACTICES.md")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.count("> Second zeta flow"), 0)
        self.assertEqual(text.count("> Custom HDA usage flow"), 1)
        # 两条都被 search_lessons 命中（recipes 通道无 draft 门槛）
        found = bridge.search_lessons(None, query="zeta")
        self.assertEqual(found["status"], "success")
        ids = [r["id"] for r in found["results"]]
        self.assertIn("BP-001", ids)
        self.assertIn("BP-002", ids)

    def test_invalid_severity_returns_ls_write_error_with_recipes_values(self):
        bridge = _get_bridge()
        env = self._save(bridge, severity="critical")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_write_error")
        self.assertIn("severity", env["error"]["message"])
        # 错误消息写清 recipes severity 合法取值（3 值，与 lesson 4 值不同）
        self.assertIn("recipes", env["error"]["message"])
        self.assertEqual(env["error"]["details"]["field"], "severity")
        self.assertEqual(env["error"]["details"]["value"], "critical")
        self.assertEqual(env["error"]["details"]["valid"],
                         ["high", "low", "medium"])
        # 零写入
        recipes_dir = os.path.join(self.personal(), "recipes")
        self.assertFalse(
            os.path.isdir(recipes_dir) and os.listdir(recipes_dir))

    def test_team_root_writable_true_annotates_source_with_username(self):
        bridge = _get_bridge()
        os.makedirs(os.path.join(self.base, "teamx"))
        with open(os.path.join(self.base, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump([{"name": "teamx", "path": "teamx",
                        "priority": 0.5, "writable": True}], handle)
        with mock.patch.object(_lessons.getpass, "getuser",
                               return_value="alice"):
            env = self._save(bridge, root="teamx")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["root"], "teamx")
        self.assertEqual(env["source"], "agent@alice")
        path = os.path.join(self.base, "teamx", "recipes",
                            "BEST_PRACTICES.md")
        self.assertTrue(os.path.isfile(path))

    def test_team_root_writable_false_returns_root_not_writable(self):
        bridge = _get_bridge()
        os.makedirs(os.path.join(self.base, "teamx"))
        with open(os.path.join(self.base, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump([{"name": "teamx", "path": "teamx",
                        "priority": 0.5, "writable": False}], handle)
        env = self._save(bridge, root="teamx")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "root_not_writable")
        # 零写入
        recipes_dir = os.path.join(self.base, "teamx", "recipes")
        self.assertFalse(
            os.path.isdir(recipes_dir) and os.listdir(recipes_dir))

    def test_unavailable_team_root_returns_ls_unknown_root(self):
        bridge = _get_bridge()
        # registry 声明但目录不存在 → state=unavailable → ls_unknown_root
        with open(os.path.join(self.base, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump([{"name": "teamx", "path": "teamx",
                        "priority": 0.5, "writable": True}], handle)
        env = self._save(bridge, root="teamx")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_unknown_root")

    # ---- improve-knowledge-capture（tasks 4.3）：recipe_id 原地更新 ----

    def test_append_returns_action_created(self):
        bridge = _get_bridge()
        env = self._save(bridge)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["action"], "created")
        self.assertEqual(env["recipe_id"], "BP-001")

    def test_update_in_place_returns_action_updated_and_searchable(self):
        bridge = _get_bridge()
        first = self._save(bridge)
        self.assertEqual(first["recipe_id"], "BP-001")
        env = self._save(bridge, recipe_id="BP-001",
                         problem="加深后的原理：按资产级标识组织正文。",
                         symptom="recipe symptom zeta unique v2")
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["recipe_id"], "BP-001")
        self.assertEqual(env["action"], "updated")
        # 文件仍只有一块（原地更新不新增块）
        path = os.path.join(self.personal(), "recipes",
                            "BEST_PRACTICES.md")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.count("### BP-001"), 1)
        self.assertNotIn("BP-002", text)
        self.assertIn("加深后的原理", text)
        # 更新后仍可被检索命中（原地更新不破坏索引通道）
        found = bridge.search_lessons(None, query="zeta unique v2")
        self.assertEqual(found["status"], "success")
        ids = [r["id"] for r in found["results"]]
        self.assertIn("BP-001", ids)

    def test_update_unknown_id_returns_ls_recipe_not_found(self):
        bridge = _get_bridge()
        first = self._save(bridge)
        self.assertEqual(first["recipe_id"], "BP-001")
        env = self._save(bridge, recipe_id="BP-999")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "ls_recipe_not_found")
        self.assertIn("BP-001", env["error"]["message"])
        self.assertEqual(env["error"]["details"]["existing_ids"],
                         ["BP-001"])
        # 零写入：文件仍只有创建的那一块
        path = os.path.join(self.personal(), "recipes",
                            "BEST_PRACTICES.md")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.count("### BP-001"), 1)
        self.assertNotIn("BP-999", text)

    def test_update_invalid_format_returns_ls_write_error(self):
        bridge = _get_bridge()
        for bad in ("BP-1", "BP-0001", "bp-001", "BP-ABC"):
            env = self._save(bridge, recipe_id=bad)
            self.assertEqual(env["status"], "error")
            self.assertEqual(env["error"]["code"], "ls_write_error")
            self.assertIn("BP-NNN", env["error"]["message"])


# ---------------------------------------------------------------------------
# add-workflow-knowledge-capture（tasks 4.4）：capture_workflow_snapshot
# envelope / cap 透传（mock 连接层，不真连 Houdini）
# ---------------------------------------------------------------------------
class CaptureWorkflowSnapshotToolTests(LessonsToolsBase):

    SUCCESS_RESULT = {
        "status": "success",
        "root": "selection",
        "node_count": 1,
        "truncated": False,
        "nodes": [],
        "sticky_notes": [],
        "connections": [],
    }

    def test_success_result_passed_through_verbatim(self):
        bridge = _get_bridge()
        with mock.patch.object(
                bridge, "_houdini_call",
                return_value={"status": "success",
                              "result": self.SUCCESS_RESULT}) as call_mock:
            env = bridge.capture_workflow_snapshot(None)
        self.assertEqual(env, self.SUCCESS_RESULT)
        call_mock.assert_called_once_with(
            "capture_workflow_snapshot",
            {"node_path": None, "include_vex": True, "max_nodes": 50,
             "probe_mode": "auto", "include_connected": False,
             "include_hda_internals": None, "offset": None, "limit": None})

    def test_include_hda_internals_passed_through(self):
        bridge = _get_bridge()
        with mock.patch.object(
                bridge, "_houdini_call",
                return_value={"status": "success",
                              "result": self.SUCCESS_RESULT}) as call_mock:
            env = bridge.capture_workflow_snapshot(
                None, include_hda_internals=True, max_nodes=500)
        self.assertEqual(env["status"], "success")
        call_mock.assert_called_once_with(
            "capture_workflow_snapshot",
            {"node_path": None, "include_vex": True, "max_nodes": 500,
             "probe_mode": "auto", "include_connected": False,
             "include_hda_internals": True, "offset": None, "limit": None})

    def test_layered_probe_params_passed_through(self):
        bridge = _get_bridge()
        with mock.patch.object(
                bridge, "_houdini_call",
                return_value={"status": "success",
                              "result": self.SUCCESS_RESULT}) as call_mock:
            env = bridge.capture_workflow_snapshot(
                None, node_path="/obj/geo1/rbdbulletsolver1",
                probe_mode="auto", include_connected=True,
                offset=10, limit=20)
        self.assertEqual(env["status"], "success")
        call_mock.assert_called_once_with(
            "capture_workflow_snapshot",
            {"node_path": "/obj/geo1/rbdbulletsolver1",
             "include_vex": True, "max_nodes": 50,
             "probe_mode": "auto", "include_connected": True,
             "include_hda_internals": None, "offset": 10, "limit": 20})

    def test_connection_error_converted_to_lessons_envelope(self):
        bridge = _get_bridge()
        with mock.patch.object(
                bridge, "_houdini_call",
                return_value={"status": "error", "message": "conn down",
                              "origin": "connection"}):
            env = bridge.capture_workflow_snapshot(None)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "capture_connection_error")
        self.assertEqual(env["error"]["message"], "conn down")
        self.assertEqual(env["error"]["details"]["origin"], "connection")

    def test_large_payload_passes_cap_with_truncation_marker(self):
        bridge = _get_bridge()
        big = {
            "status": "success",
            "root": "selection",
            "node_count": 3000,
            "truncated": False,
            "nodes": [{"path": "/obj/geo1/node{0}".format(i),
                       "name": "node{0}".format(i), "type": "box"}
                      for i in range(3000)],
            "sticky_notes": [],
            "connections": [],
        }
        with mock.patch.object(
                bridge, "_houdini_call",
                return_value={"status": "success", "result": big}):
            env = bridge.capture_workflow_snapshot(None)
        # 过 _lessons_capped 后仍为 dict，超大 payload 触发 _truncated 标记
        self.assertIsInstance(env, dict)
        self.assertEqual(env["status"], "success")
        self.assertIn("_truncated", env)
        self.assertIn("_original_size", env)


if __name__ == "__main__":
    unittest.main()
