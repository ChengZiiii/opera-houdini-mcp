"""H21.0 live smoke for lessons knowledge base (manual, not auto-collected).

在真实 H21 hython 中对 ``_lessons`` / ``_lessons_search`` 做端到端冒烟：
保存 / 累积 / inbox 晋升 / 检索命中 / read_lesson / 多 root 降级 /
默认路径推导，外加 bridge 探针（按实际环境降级）。**不使用 mock**，
全程沙箱化（HOUDINI_MCP_HOME 指向临时目录），绝不触碰真实
``~/.opera-houdini-mcp``。

运行方式（需真实 H21 hython，workdir=external/houdinimcp）：
    "C:/Program Files/Side Effects Software/Houdini 21.0.596/bin/hython.exe" \\
        tests/h21_live_lessons_smoke.py

退出码 0 = 全部 PASS；非 0 = 有 FAIL。
"""
import atexit
import json
import os
import re
import shutil
import sys
import tempfile

# ---- 沙箱：必须在 import _lessons 之前设置 HOUDINI_MCP_HOME ----
# 之后所有路径 helper（_base_dir / knowledge_dir / cache_index_dir）都从
# 该临时目录派生；atexit 清理，进程退出后环境变量也随之消失。
SANDBOX = tempfile.mkdtemp(prefix="lessons_smoke_")
os.environ["HOUDINI_MCP_HOME"] = SANDBOX
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))

# hython 以脚本方式运行：包目录（tests/ 的上级）放入 sys.path，
# 使 _lessons_search 内部的裸 import 回退链（import _lessons 等）可用。
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PKG_DIR)

import _lessons  # noqa: E402
import _lessons_search  # noqa: E402

# ---- 测试数据：真实 Houdini VEX 报错场景（中文） ----
FIELDS = {
    "title": "wrangle 读取未定义属性导致 cook 失败",
    "category": "vex",
    "severity": "medium",
    "affected_versions": "H21.0",
    "verified_versions": "unknown",
    "source": "h21-live-smoke",
    "advisory": False,
    "problem": "在 Attribute Wrangle 中直接读取尚未创建的属性 age，"
               "节点报错并中断 cook，下游节点全部标红。",
    "symptom": "wrangle 中访问未定义属性 age 报错 'undefined attribute'，"
               "节点标红，cook 中断",
    "fix": "先用 haspointattrib(0, 'age') 守卫判断，或在上游先创建该属性，"
           "再执行读取。",
}
FIX2 = "改用 detail 属性缓存该值，并在 wrangle 内用 haspointattrib() 检查后读取。"
QUERY = "未定义属性"          # 特色中文检索词（symptom 中的连续 CJK 串）
FRESH_MSG = "ROP 提交失败：输出目录不存在或不可写，Deadline 任务被拒绝"


def _read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def main():
    results = []

    def check(name, condition, detail=""):
        tag = "PASS" if condition else "FAIL"
        results.append((tag, name, detail))
        return bool(condition)

    def finish():
        print("=" * 60)
        fails = 0
        for tag, name, detail in results:
            print("[%s] %s -- %s" % (tag, name, detail))
            if tag == "FAIL":
                fails += 1
        print("=" * 60)
        print("H21.0 live lessons smoke: %d checks, %d FAIL" % (len(results), fails))
        print("sandbox: %s" % SANDBOX)
        sys.exit(1 if fails else 0)

    # ---- 环境证明：真实 hython + Houdini 21.0 ----
    print("python:", sys.version.replace("\n", " "))
    try:
        import hou
        check("env Houdini 21.0", tuple(hou.applicationVersion()[:2]) == (21, 0),
              "version=%r" % (hou.applicationVersion(),))
    except Exception as exc:
        check("env Houdini 21.0", False, "hou 不可用: %r" % (exc,))
        finish()
        return

    knowledge = _lessons.knowledge_dir()
    check("a sandbox base under temp dir",
          knowledge == os.path.join(SANDBOX, "knowledge"), knowledge)

    # ---- a. 保存 ----
    try:
        lesson = _lessons.save_lesson(knowledge, FIELDS)
    except Exception as exc:
        check("a save_lesson", False, "%r" % (exc,))
        finish()
        return
    lesson_id = lesson["id"]
    lesson_file = os.path.join(_lessons.lessons_dir(knowledge), lesson_id + ".md")
    check("a id format L-YYYYMMDD-NNN", bool(re.match(r"^L-\d{8}-\d{3,}$", lesson_id)),
          lesson_id)
    check("a status draft", lesson["status"] == "draft", lesson["status"])
    check("a strength 1", lesson["strength"] == 1, "strength=%r" % (lesson["strength"],))
    check("a root personal", lesson["root"] == "personal", lesson["root"])
    check("a fingerprint == sha256(symptom)",
          lesson.get("fingerprint") == _lessons.make_fingerprint(FIELDS["symptom"]))
    check("a file exists", os.path.isfile(lesson_file), lesson_file)
    try:
        lessons, errors = _lessons.load_root_lessons(knowledge)
        check("a load_root_lessons re-parses", not errors
              and lesson_id in [l["id"] for l in lessons], "errors=%r" % (errors,))
    except Exception as exc:
        check("a load_root_lessons re-parses", False, "%r" % (exc,))

    # ---- b. 累积（同 symptom 不同 fix -> strength 2，内容绝不覆盖）----
    try:
        fields2 = dict(FIELDS)
        fields2["fix"] = FIX2
        updated = _lessons.save_lesson(knowledge, fields2)
    except Exception as exc:
        check("b accumulate save", False, "%r" % (exc,))
        finish()
        return
    check("b strength 2", updated["strength"] == 2,
          "strength=%r" % (updated["strength"],))
    check("b same id", updated["id"] == lesson_id)
    check("b same fingerprint", updated.get("fingerprint") == lesson.get("fingerprint"))
    try:
        text = _read_text(lesson_file)
        reparsed = _lessons.parse_lesson(text)
        check("b fix byte-identical in file", reparsed["fix"] == FIELDS["fix"],
              "fix=%r" % (reparsed["fix"][:40],))
        check("b fix2 NOT written", FIX2 not in text)
        check("b strength line bumped", "strength: 2" in text)
    except Exception as exc:
        check("b fix byte-identical in file", False, "%r" % (exc,))

    # 同 symptom 3 次错误事件 -> inbox 单行 count 3
    try:
        ok_all = all(_lessons.record_error_event(
            knowledge, "search_lessons", "x", FIELDS["symptom"]) for _i in range(3))
        check("b record_error_event x3 all True", ok_all)
    except Exception as exc:
        check("b record_error_event x3 all True", False, "%r" % (exc,))
    try:
        inbox_lines = [line for line in
                       _read_text(_lessons.inbox_path(knowledge)).splitlines()
                       if line.strip()]
        record = json.loads(inbox_lines[0]) if inbox_lines else {}
        check("b inbox single line", len(inbox_lines) == 1,
              "lines=%d" % len(inbox_lines))
        check("b inbox count 3", record.get("count") == 3,
              "count=%r" % (record.get("count"),))
        check("b inbox fingerprint matches lesson",
              record.get("fingerprint") == lesson.get("fingerprint"))
    except Exception as exc:
        check("b inbox single line / count 3", False, "%r" % (exc,))
    try:
        lessons, _errs = _lessons.load_root_lessons(knowledge)
        check("b auto-promote skipped (fingerprint already a lesson)",
              len(lessons) == 1, "lessons=%d" % len(lessons))
    except Exception as exc:
        check("b auto-promote skipped (fingerprint already a lesson)",
              False, "%r" % (exc,))

    # ---- c. 统计晋升（按实际行为：count>=3 在第 3 次事件时自动晋升）----
    try:
        created = _lessons.promote_inbox_to_drafts(knowledge)
        check("c explicit promote no-op for existing fingerprint", created == [],
              "created=%r" % (created,))
    except Exception as exc:
        check("c explicit promote no-op for existing fingerprint",
              False, "%r" % (exc,))
    # 全新 fingerprint 3 次事件 -> 第 3 次事件即自动生成 draft 骨架
    try:
        for _i in range(3):
            _lessons.record_error_event(knowledge, "deadline_submit", "x", FRESH_MSG)
    except Exception as exc:
        check("c fresh 3 events", False, "%r" % (exc,))
    skeleton_id = None
    try:
        lessons, _errs = _lessons.load_root_lessons(knowledge)
        skeletons = [l for l in lessons if l.get("source") == "inbox-auto"]
        skel = skeletons[0] if skeletons else None
        if skel is not None:
            skeleton_id = skel["id"]
        check("c auto-promote skeleton at 3rd event", skel is not None,
              "total=%d" % len(lessons))
        if skel is not None:
            check("c skeleton status draft", skel["status"] == "draft",
                  skel["status"])
            check("c skeleton symptom non-empty", bool(skel["symptom"]),
                  skel["symptom"][:30])
            check("c skeleton problem/fix empty",
                  skel["problem"] == "" and skel["fix"] == "",
                  "problem=%r fix=%r" % (skel["problem"], skel["fix"]))
            check("c skeleton category/severity/source",
                  skel["category"] == "unclassified"
                  and skel["severity"] == "medium"
                  and skel["source"] == "inbox-auto",
                  "cat=%s sev=%s src=%s" % (skel["category"], skel["severity"],
                                            skel["source"]))
            check("c skeleton fingerprint matches fresh symptom",
                  skel.get("fingerprint") == _lessons.make_fingerprint(FRESH_MSG))
    except Exception as exc:
        check("c auto-promote skeleton at 3rd event", False, "%r" % (exc,))
    try:
        created2 = _lessons.promote_inbox_to_drafts(knowledge)
        lessons2, _errs2 = _lessons.load_root_lessons(knowledge)
        check("c explicit promote idempotent (no dup)",
              created2 == [] and len(lessons2) == len(lessons),
              "created=%r total=%d" % (created2, len(lessons2)))
    except Exception as exc:
        check("c explicit promote idempotent (no dup)", False, "%r" % (exc,))

    # ---- d. 检索命中（先翻转 status: draft -> published）----
    try:
        text = _read_text(lesson_file)
        new_text = text.replace("status: draft", "status: published", 1)
        check("d flipped status line", "status: published" in new_text)
        with open(lesson_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        lessons, errs = _lessons.load_root_lessons(knowledge)
        flipped = [l for l in lessons if l["id"] == lesson_id]
        check("d re-parse after flip",
              not errs and flipped and flipped[0]["status"] == "published",
              "errs=%r" % (errs,))
    except Exception as exc:
        check("d flip draft->published", False, "%r" % (exc,))
    try:
        resp = _lessons_search.search_lessons(query=QUERY, scope=None)
        check("d search status success", resp.get("status") == "success",
              "status=%r" % (resp.get("status"),))
        ids = [r["id"] for r in resp.get("results", [])]
        check("d search hits lesson id", lesson_id in ids,
              "matched=%s ids=%r" % (resp.get("matched"), ids))
        hit = next((r for r in resp.get("results", [])
                    if r.get("id") == lesson_id), None)
        if hit is not None:
            check("d source_root personal", hit.get("source_root") == "personal",
                  "source_root=%r" % (hit.get("source_root"),))
            check("d no full text in summary",
                  "problem" not in hit and "search_text" not in hit
                  and "body_problem" not in hit,
                  "keys=%r" % (sorted(hit.keys()),))
            check("d kind lesson + strength 2",
                  hit.get("kind") == "lesson" and hit.get("strength") == 2,
                  "kind=%r strength=%r" % (hit.get("kind"), hit.get("strength")))
            check("d hint per contract (inbox count>=3)",
                  hit.get("hint") == "已踩 3 次，请补充 fix",
                  "hint=%r" % (hit.get("hint"),))
        if skeleton_id is not None:
            sug = [s for s in resp.get("draft_suggestions", [])
                   if s.get("id") == skeleton_id]
            check("d draft_suggestions includes skeleton",
                  bool(sug) and sug[0].get("count") == 3,
                  "sug=%r" % (resp.get("draft_suggestions"),))
    except Exception as exc:
        check("d search_lessons", False, "%r" % (exc,))

    # ---- e. read_lesson：find_lesson_by_id + markdown 渲染 ----
    try:
        found = _lessons_search.find_lesson_by_id(lesson_id, scope=None)
        check("e find_lesson_by_id found", found is not None)
        if found is not None:
            check("e body_problem/body_symptom/body_fix non-empty",
                  bool(found.get("body_problem"))
                  and bool(found.get("body_symptom"))
                  and bool(found.get("body_fix")),
                  "problem=%d symptom=%d fix=%d" % (
                      len(found.get("body_problem") or ""),
                      len(found.get("body_symptom") or ""),
                      len(found.get("body_fix") or "")))
            check("e root personal", found.get("root") == "personal",
                  "root=%r" % (found.get("root"),))
            check("e file_path exists", os.path.isfile(found.get("file_path", "")),
                  found.get("file_path"))
            md = _lessons._render_lesson_markdown(found)
            check("e markdown has ## Problem", "## Problem" in md)
            check("e markdown front matter id",
                  bool(re.search(r"^id:\s*" + re.escape(lesson_id) + r"$",
                                 md, re.M)))
            check("e markdown round-trips",
                  _lessons.parse_lesson(md)["id"] == lesson_id)
    except Exception as exc:
        check("e find_lesson_by_id / render", False, "%r" % (exc,))

    # ---- f. 多 root 降级：unconfigured / unavailable / 只读门禁 ----
    config = [
        {"name": "team_a", "path": "${LESSONS_SMOKE_UNDEFINED}",
         "priority": 0.8, "writable": False},
        {"name": "team_b", "path": "${LESSONS_SMOKE_MISSING}",
         "priority": 0.7, "writable": False},
    ]
    os.environ["LESSONS_SMOKE_MISSING"] = os.path.join(SANDBOX, "team_b_missing")
    os.environ.pop("LESSONS_SMOKE_UNDEFINED", None)  # 保持 unconfigured
    try:
        with open(os.path.join(SANDBOX, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False)
        check("f config.json written", os.path.isfile(
            os.path.join(SANDBOX, "config.json")))
    except Exception as exc:
        check("f config.json written", False, "%r" % (exc,))
    try:
        resp = _lessons_search.search_lessons(query=QUERY, scope=None)
        warnings = resp.get("_warning") or []
        check("f search _warning mentions team_b", any("team_b" in w for w in warnings),
              "warnings=%r" % (warnings,))
        check("f unconfigured team_a silently skipped",
              not any("team_a" in w for w in warnings))
        ids = [r["id"] for r in resp.get("results", [])]
        check("f personal results intact", lesson_id in ids,
              "matched=%s" % (resp.get("matched"),))
    except Exception as exc:
        check("f search with unavailable root", False, "%r" % (exc,))
    try:
        stats = _lessons_search.compute_stats(scope=None)
        by_name = {r["name"]: r for r in stats.get("roots", [])}
        check("f stats team_a unconfigured",
              by_name.get("team_a", {}).get("state") == "unconfigured",
              "team_a=%r" % (by_name.get("team_a"),))
        check("f stats team_b unavailable",
              by_name.get("team_b", {}).get("state") == "unavailable",
              "team_b=%r" % (by_name.get("team_b"),))
        check("f stats personal ok",
              by_name.get("personal", {}).get("state") == "ok",
              "personal=%r" % (by_name.get("personal"),))
        check("f stats personal counts",
              by_name["personal"]["lesson_count"] >= 2
              and by_name["personal"]["draft_count"] >= 1
              and by_name["personal"]["published_count"] >= 1,
              "lessons=%d drafts=%d published=%d" % (
                  by_name["personal"]["lesson_count"],
                  by_name["personal"]["draft_count"],
                  by_name["personal"]["published_count"]))
    except Exception as exc:
        check("f compute_stats", False, "%r" % (exc,))
    # team_b：state=unavailable + writable=false -> save_lesson 抛 root_not_writable
    try:
        roots = {r["name"]: r for r in _lessons.resolve_roots()}
        team_b_path = roots["team_b"]["path"]
        try:
            _lessons.save_lesson(team_b_path, FIELDS)
            check("f save to team_b -> root_not_writable", False, "no error raised")
        except _lessons.LessonsError as exc:
            check("f save to team_b -> root_not_writable",
                  exc.code == "root_not_writable",
                  "code=%s msg=%s" % (exc.code, exc.message))
    except Exception as exc:
        check("f save to team_b -> root_not_writable", False, "%r" % (exc,))
    # team_a：unconfigured 时无 path 可写（ls_unknown_root）；补设 env 后
    # writable=false 只读门禁生效（root_not_writable）——按实际行为验证
    try:
        try:
            _lessons.resolve_root_for_write("team_a")
            check("f team_a unconfigured not writable", False, "no error raised")
        except _lessons.LessonsError as exc:
            check("f team_a unconfigured not writable",
                  exc.code == "ls_unknown_root", "code=%s" % exc.code)
        live_team_a = os.path.join(SANDBOX, "team_a_live")
        os.makedirs(live_team_a, exist_ok=True)
        os.environ["LESSONS_SMOKE_UNDEFINED"] = live_team_a
        try:
            _lessons.save_lesson(live_team_a, FIELDS)
            check("f save to team_a (writable=false) -> root_not_writable",
                  False, "no error raised")
        except _lessons.LessonsError as exc:
            check("f save to team_a (writable=false) -> root_not_writable",
                  exc.code == "root_not_writable",
                  "code=%s msg=%s" % (exc.code, exc.message))
    except Exception as exc:
        check("f team_a writable=false gate", False, "%r" % (exc,))

    # ---- g. 默认路径推导（不写真实目录）----
    saved_home = os.environ.get("HOUDINI_MCP_HOME")
    try:
        os.environ.pop("HOUDINI_MCP_HOME", None)
        default_dir = _lessons._base_dir()
        home = os.path.expanduser("~")
        if not home or home == "~":
            home = os.environ.get("USERPROFILE", "")
        expected = os.path.join(home, _lessons.BASE_DIRNAME)
        check("g default base dir = ~/.opera-houdini-mcp",
              default_dir == expected, "got=%s expected=%s" % (default_dir, expected))
        check("g real default dir absent (no writes)",
              not os.path.exists(expected), expected)
    except Exception as exc:
        check("g default base dir = ~/.opera-houdini-mcp", False, "%r" % (exc,))
    finally:
        if saved_home is not None:
            os.environ["HOUDINI_MCP_HOME"] = saved_home
        else:
            os.environ.pop("HOUDINI_MCP_HOME", None)

    # ---- bridge 探针（hython 3.11 无 mcp 包，预期 ModuleNotFoundError）----
    try:
        import houdini_mcp_server as hms
    except ImportError as exc:
        if "mcp" in str(exc):
            check("bridge import houdini_mcp_server (mcp missing under hython)",
                  True, "KNOWN env limitation: %r" % (exc,))
        else:
            check("bridge import houdini_mcp_server (mcp missing under hython)",
                  False, "unexpected ImportError: %r" % (exc,))
    except Exception as exc:
        check("bridge import houdini_mcp_server (mcp missing under hython)",
              False, "%r" % (exc,))
    else:
        for fn_name in ("search_lessons", "save_lesson", "read_lesson",
                        "knowledge_stats"):
            check("bridge has function %s" % fn_name,
                  callable(getattr(hms, fn_name, None)))
        try:
            os.environ["HOUDINI_MCP_HOME"] = SANDBOX
            resp = hms.search_lessons(None, query=QUERY)
            ids = ([r.get("id") for r in resp.get("results", [])]
                   if isinstance(resp, dict) else [])
            check("bridge search_lessons finds lesson", lesson_id in ids,
                  "resp=%r" % (str(resp)[:200],))
        except Exception as exc:
            check("bridge search_lessons finds lesson", False, "%r" % (exc,))

    finish()


main()
