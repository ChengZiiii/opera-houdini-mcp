"""H21.0 live smoke for lessons knowledge base (manual, not auto-collected).

在真实 H21 hython 中对 ``_lessons`` / ``_lessons_search`` 做端到端冒烟：
保存 / 累积 / inbox 晋升 / 检索命中 / read_lesson / 多 root 降级 /
默认路径推导，外加 capture_workflow_snapshot 真实场景快照、save_recipe
全链路（个人库 + 团队库门禁）、HDA 内部研究（include_hda_internals +
资产级标识 + recipe 原地更新全链路）与 bridge 探针（按实际环境降级）。
**不使用 mock**，全程沙箱化（HOUDINI_MCP_HOME 指向临时目录），绝不触碰
真实 ``~/.opera-houdini-mcp``。

运行方式（需真实 H21 hython，workdir=external/houdinimcp）：
    "C:/Program Files/Side Effects Software/Houdini 21.0.596/bin/hython.exe" \\
        tests/h21_live_lessons_smoke.py

退出码 0 = 全部 PASS；非 0 = 有 FAIL。
"""
import atexit
import getpass
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

import _best_practices  # noqa: E402
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
# section h sticky note：headless 创建通道（网络容器 createStickyNote）
# 的两条不同文本，用于验证快照逐条捕获。
STICKY_TEXT_A = "sticky 真实中文文本 工作流备注"
STICKY_TEXT_B = "第二条 sticky 备注 不同文本"


def _read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _dir_signature(path):
    """递归目录签名（relpath, size, mtime_ns）——证明 smoke 未写真实目录。"""
    sig = []
    for root, _dirs, files in os.walk(path):
        for fname in files:
            full = os.path.join(root, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            sig.append((os.path.relpath(full, path), st.st_size, st.st_mtime_ns))
    return sorted(sig)


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
        # 实机可能已存在真实 base dir（先前开发/人工使用产生，本 smoke 不触碰）；
        # 此时改验「本次运行未向真实目录写入」：目录签名不变即 PASS。
        if os.path.isdir(expected):
            sig_before = _dir_signature(expected)
        else:
            sig_before = None
        check("g no writes to real default dir",
              (sig_before is None and not os.path.exists(expected))
              or (sig_before is not None and _dir_signature(expected) == sig_before),
              "dir=%s pre_existed=%s" % (expected, sig_before is not None))
    except Exception as exc:
        check("g no writes to real default dir", False, "%r" % (exc,))
    finally:
        if saved_home is not None:
            os.environ["HOUDINI_MCP_HOME"] = saved_home
        else:
            os.environ.pop("HOUDINI_MCP_HOME", None)

    # ---- h. capture_workflow_snapshot：真实场景快照（server handler 直调）----
    # hython 无 GUI（hou.ui 不存在），但 sticky note 有 headless 创建通道：
    # 网络容器（SopNode/ObjNode 等）的 createStickyNote() + setText()，
    # H21.0.596 hython 实测可用，不依赖 pane。快照 handler 通过父网络
    # iterStickyNotes() 去重采集（server.py 3603-3637），此处创建两条
    # 真实 sticky note，断言其文本/结构被快照捕获。
    try:
        geo = hou.node("/obj").createNode("geo", "capture_smoke")
        box = geo.createNode("box", "capture_box")
        wrangle = geo.createNode("attribwrangle", "capture_wrangle")
        wrangle.parm("snippet").set("f@age = 1.0;")
        wrangle.setComment("真实场景注释")
        # 真实连线：box 输出 → wrangle 输入（OBJ→SOP 直连在 hython 下
        # setInput 抛 OperationFailed，属 hython 环境限制，改用 SOP→SOP）。
        wrangle.setInput(0, box)
        check("h scene nodes created", geo is not None and wrangle is not None,
              "geo=%r wrangle=%r" % (geo.path() if geo else None,
                                     wrangle.path() if wrangle else None))
    except Exception as exc:
        check("h scene nodes created", False, "%r" % (exc,))
        finish()
        return
    # sticky note：headless 创建通道 = 网络容器 createStickyNote()（不依赖
    # hou.ui / pane）。创建在 wrangle.parent()（= geo 容器 /obj/capture_smoke）
    # 上两条不同文本；失败不提前终止，后续断言会如实 FAIL 展示。
    try:
        note_a = wrangle.parent().createStickyNote()
        note_a.setText(STICKY_TEXT_A)
        note_b = wrangle.parent().createStickyNote()
        note_b.setText(STICKY_TEXT_B)
        check("h sticky notes created (headless createStickyNote)",
              note_a is not None and note_b is not None,
              "parent=%s" % (wrangle.parent().path(),))
    except Exception as exc:
        check("h sticky notes created (headless createStickyNote)",
              False, "createStickyNote/setText 失败: %r" % (exc,))
    try:
        # server.py 用相对导入（from . import _common），必须按包路径导入：
        # 把包的父目录（external/）加入 sys.path 后 import houdinimcp.server。
        # BFS 闭包只沿 inputs()/outputs() 走连接，OBJ 的 child SOP 不在其中，
        # 因此以 wrangle 为 seed（其 input 指向 box，闭包同时收录两者）。
        sys.path.insert(0, os.path.dirname(_PKG_DIR))
        import houdinimcp.server as server  # noqa: E402
        instance = server.HoudiniMCPServer.__new__(server.HoudiniMCPServer)
        resp = instance.handle_capture_workflow_snapshot(
            node_path=wrangle.path(), include_vex=True)
    except Exception as exc:
        check("h handle_capture_workflow_snapshot", False, "%r" % (exc,))
        finish()
        return
    check("h status success", resp.get("status") == "success",
          "status=%r" % (resp.get("status"),))
    check("h node_count >= 1", resp.get("node_count", 0) >= 1,
          "node_count=%r" % (resp.get("node_count"),))
    nodes = resp.get("nodes") or []
    wr_entries = [n for n in nodes if n.get("type") == "attribwrangle"]
    check("h wrangle entry present", bool(wr_entries),
          "nodes=%d types=%r" % (len(nodes), [n.get("type") for n in nodes]))
    if wr_entries:
        w0 = wr_entries[0]
        check("h wrangle path/comment",
              w0.get("path") == wrangle.path()
              and w0.get("comment") == "真实场景注释",
              "path=%r comment=%r" % (w0.get("path"), w0.get("comment")))
        check("h wrangle vex snippet", "f@age = 1.0;" in (w0.get("vex") or ""),
              "vex=%r" % ((w0.get("vex") or "")[:60],))
    conns = resp.get("connections")
    check("h connections is list", isinstance(conns, list),
          "connections=%r" % (str(conns)[:120],))
    check("h connections entry shape",
          all(isinstance(c, dict) and "from" in c and "to" in c
              and "input_index" in c for c in (conns or [])),
          "conns=%d" % len(conns or []))
    for gk in ("point", "prim", "vertex", "geometry", "P"):
        check("h no geometry key %s" % gk, gk not in resp,
              "keys=%r" % (sorted(resp.keys()),))
    for n in nodes:
        for gk in ("point", "prim", "vertex", "geometry", "P"):
            if gk in n:
                check("h node entry no geometry key %s" % gk, False,
                      "node=%s" % (n.get("path"),))
    sticky = resp.get("sticky_notes")
    check("h sticky_notes is list", isinstance(sticky, list),
          "sticky=%r" % (str(sticky)[:120],))
    # 快照必须捕获真实创建的 sticky note：非空、含两条文本、条目结构
    # 含 parent/text/position 三键（position 为 list 类值）。
    check("h sticky_notes non-empty", bool(sticky),
          "notes=%r" % (str(sticky)[:160],))
    sticky_texts = [s.get("text") for s in (sticky or [])]
    check("h sticky note text captured", STICKY_TEXT_A in sticky_texts,
          "texts=%r" % (sticky_texts,))
    check("h second sticky note text captured", STICKY_TEXT_B in sticky_texts,
          "texts=%r" % (sticky_texts,))
    check("h sticky entry parent/text/position shape",
          all(isinstance(s, dict) and "parent" in s and "text" in s
              and "position" in s and isinstance(s["position"], list)
              and len(s["position"]) >= 2 for s in (sticky or [])),
          "shape=%r" % ([sorted(s.keys()) for s in (sticky or [])],))
    check("h sticky parent is geo container",
          all(s.get("parent") == wrangle.parent().path()
              for s in (sticky or [])),
          "parents=%r" % ({s.get("parent") for s in (sticky or [])},))
    check("h truncated False", resp.get("truncated") is False,
          "truncated=%r" % (resp.get("truncated"),))
    # 错误路径实测：无选择 → no_selection；不存在路径 → invalid_node_path
    try:
        err_resp = instance.handle_capture_workflow_snapshot(node_path=None)
        check("h no_selection error",
              err_resp.get("status") == "error"
              and err_resp.get("error", {}).get("code") == "no_selection",
              "resp=%r" % (str(err_resp)[:160],))
    except Exception as exc:
        check("h no_selection error", False, "%r" % (exc,))
    try:
        err_resp2 = instance.handle_capture_workflow_snapshot(
            node_path="/obj/不存在的节点")
        check("h invalid_node_path error",
              err_resp2.get("status") == "error"
              and err_resp2.get("error", {}).get("code") == "invalid_node_path",
              "resp=%r" % (str(err_resp2)[:160],))
    except Exception as exc:
        check("h invalid_node_path error", False, "%r" % (exc,))
    try:
        hou.node("/obj/capture_smoke").destroy()
        check("h cleanup nodes", hou.node("/obj/capture_smoke") is None)
    except Exception as exc:
        check("h cleanup nodes", False, "%r" % (exc,))

    # ---- i. save_recipe 全链路（真实磁盘沙箱）----
    # 与 _agent_source 完全一致的用户名推导（hython 下 getpass.getuser 取真值）
    try:
        real_user = getpass.getuser()
    except Exception:
        real_user = os.environ.get("USERNAME")
    if not real_user:
        real_user = "unknown-user"
    recipe_fields = {
        "title": "wrangle 未定义属性处理沉淀",
        "category": "vex",
        "severity": "high",
        "affected_versions": "H21.0",
        "problem": "Attribute Wrangle 直接读取未定义属性导致 cook 失败。",
        "symptom": "wrangle 读取未定义属性 cook 失败节点标红",
        "fix": "先用 haspointattrib 守卫判断再读取属性。",
        "advisory": True,
    }
    try:
        recipe = _lessons.save_recipe(knowledge, dict(recipe_fields))
    except Exception as exc:
        check("i save_recipe", False, "%r" % (exc,))
        finish()
        return
    check("i id BP-NNN", bool(re.match(r"^BP-\d{3}$", recipe["id"])),
          recipe["id"])
    check("i root personal", recipe["root"] == "personal", recipe["root"])
    check("i source agent", recipe["source"] == "agent", recipe["source"])
    recipes_file = _lessons.recipes_path(knowledge)
    check("i recipes file exists", os.path.isfile(recipes_file), recipes_file)
    try:
        entries = _best_practices.parse_best_practices(_read_text(recipes_file))
        check("i parse_best_practices round-trip",
              any(e["id"] == recipe["id"] for e in entries),
              "entries=%d" % len(entries))
    except Exception as exc:
        check("i parse_best_practices round-trip", False, "%r" % (exc,))
    try:
        search_resp = _lessons_search.search_lessons(
            query="读取未定义属性", scope=None)
        ids = [r["id"] for r in search_resp.get("results", [])]
        check("i search hits recipe id", recipe["id"] in ids,
              "matched=%s ids=%r" % (search_resp.get("matched"), ids))
        hit = next((r for r in search_resp.get("results", [])
                    if r.get("id") == recipe["id"]), None)
        if hit is not None:
            check("i hit kind recipe", hit.get("kind") == "recipe",
                  "kind=%r" % (hit.get("kind"),))
            check("i hit source_root personal",
                  hit.get("source_root") == "personal",
                  "source_root=%r" % (hit.get("source_root"),))
    except Exception as exc:
        check("i search hits recipe id", False, "%r" % (exc,))
    try:
        recipe2 = _lessons.save_recipe(knowledge, dict(recipe_fields))
        check("i id increments", recipe2["id"] == "BP-002",
              "id=%s" % (recipe2["id"],))
    except Exception as exc:
        check("i id increments", False, "%r" % (exc,))
    # 团队库门禁：writable=false → root_not_writable 且零写入
    team_env = "LESSONS_SMOKE_TEAM"
    team_path = os.path.join(SANDBOX, "team_recipes")
    try:
        os.makedirs(team_path, exist_ok=True)
        os.environ[team_env] = team_path
        with open(os.path.join(SANDBOX, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump([{"name": "team_recipe",
                        "path": "${%s}" % team_env,
                        "priority": 0.9, "writable": False}],
                      handle, ensure_ascii=False)
        try:
            _lessons.save_recipe(team_path, dict(recipe_fields))
            check("i team writable=false -> root_not_writable", False,
                  "no error raised")
        except _lessons.LessonsError as exc:
            check("i team writable=false -> root_not_writable",
                  exc.code == "root_not_writable",
                  "code=%s msg=%s" % (exc.code, exc.message))
        check("i zero write on gate",
              not os.path.isfile(_lessons.recipes_path(team_path)))
    except Exception as exc:
        check("i team writable=false -> root_not_writable", False, "%r" % (exc,))
    # 团队库 writable=true → 写入成功，source 带真实用户名
    try:
        with open(os.path.join(SANDBOX, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump([{"name": "team_recipe",
                        "path": "${%s}" % team_env,
                        "priority": 0.9, "writable": True}],
                      handle, ensure_ascii=False)
        team_recipe = _lessons.save_recipe(team_path, dict(recipe_fields))
        check("i team source starts agent@",
              team_recipe["source"].startswith("agent@"),
              "source=%r" % (team_recipe["source"],))
        check("i team source ends real username",
              team_recipe["source"] == "agent@" + real_user,
              "source=%r user=%r" % (team_recipe["source"], real_user))
        check("i team recipe file written",
              os.path.isfile(_lessons.recipes_path(team_path)))
    except Exception as exc:
        check("i team writable=true write", False, "%r" % (exc,))
    # 团队库 source 归属的 save_lesson 侧（tasks 4.6 补充验证）
    try:
        lesson_team = _lessons.save_lesson(team_path, dict(FIELDS))
        check("i save_lesson team source @user",
              lesson_team["source"].endswith("@" + real_user),
              "source=%r" % (lesson_team["source"],))
    except Exception as exc:
        check("i save_lesson team source @user", False, "%r" % (exc,))

    # ---- j. HDA 内部研究 + 资产级标识 + recipe 原地更新全链路 ----
    # 真实 HDA：subnet → createDigitalAsset（外部 .hda 库文件）→ 内部
    # attribwrangle VEX；capture include_hda_internals=True 研究内部；
    # hda 字段无本机路径（definition_source 正确）；随后在隔离团队 root
    # 上走 save_recipe 创建 → recipe_id 原地更新（文件仅一块、内容替换、
    # action=updated）→ search 命中 → 未知 id 错误全链路。
    try:
        geo_hda = hou.node("/obj").createNode("geo", "hda_research")
        subnet = geo_hda.createNode("subnet", "asset_src")
        inner = subnet.createNode("attribwrangle", "inner_wrangle")
        inner.parm("snippet").set("f@activation = @P.y > 0.1;")
        inner.setComment("激活算法：按高度阈值")
        hda_lib = os.path.join(SANDBOX, "hda_lib")
        os.makedirs(hda_lib, exist_ok=True)
        hda_file = os.path.join(hda_lib, "research_asset.hda")
        subnet.createDigitalAsset("research_asset", hda_file, "research")
        check("j hda created", True, "file=%s" % hda_file)
    except Exception as exc:
        check("j hda created", False, "%r" % (exc,))
        finish()
        return
    # HDA 转换后旧 node 引用失效（ObjectWasDeleted）→ 按 path/definition 重取
    asset = None
    for child in hou.node("/obj/hda_research").children():
        try:
            if child.type().definition() is not None:
                asset = child
                break
        except Exception:
            continue
    check("j hda instance re-fetched by path", asset is not None,
          "asset=%r" % (asset,))
    if asset is None:
        finish()
        return
    try:
        internals = asset.children()
        inner_names = [n.name() for n in internals]
        check("j hda children reachable", "inner_wrangle" in inner_names,
              "children=%r" % (inner_names,))
    except Exception as exc:
        check("j hda children reachable", False, "%r" % (exc,))
    try:
        resp = instance.handle_capture_workflow_snapshot(
            node_path=asset.path(), include_vex=True, max_nodes=200,
            include_hda_internals=True)
    except Exception as exc:
        check("j capture with hda internals", False, "%r" % (exc,))
        finish()
        return
    check("j capture status success", resp.get("status") == "success",
          "status=%r" % (resp.get("status"),))
    nodes = resp.get("nodes") or []
    by_path = {n.get("path"): n for n in nodes}
    inner_key = asset.path() + "/inner_wrangle"
    check("j internal wrangle entry present", inner_key in by_path,
          "paths=%r" % (sorted(by_path)[:12],))
    if inner_key in by_path:
        ie = by_path[inner_key]
        check("j internal wrangle vex visible",
              "f@activation" in (ie.get("vex") or ""),
              "vex=%r" % ((ie.get("vex") or "")[:60],))
        check("j internal entry type_full asset-level",
              ie.get("type_full") == "Sop/attribwrangle",
              "type_full=%r" % (ie.get("type_full"),))
        check("j internal entry is_hda False", ie.get("is_hda") is False,
              "is_hda=%r" % (ie.get("is_hda"),))
    hda_entries = [n for n in nodes if n.get("is_hda")]
    check("j hda entry flagged is_hda", bool(hda_entries),
          "count=%d" % len(hda_entries))
    if hda_entries:
        h0 = hda_entries[0]
        hda_ref = h0.get("hda") or {}
        check("j hda type_name asset-level",
              hda_ref.get("type_name") == "Sop/research_asset",
              "hda=%r" % (hda_ref,))
        check("j hda definition_source external",
              hda_ref.get("definition_source") == "external",
              "hda=%r" % (hda_ref,))
        check("j hda entry type_full",
              h0.get("type_full") == "Sop/research_asset",
              "type_full=%r" % (h0.get("type_full"),))
        check("j hda response has no library_path",
              "library_path" not in h0 and "library_path" not in hda_ref,
              "keys=%r" % (sorted(h0.keys()),))
    # 响应绝不输出本机路径 / 沙箱路径
    blob = json.dumps(resp, default=str)
    check("j no hda file path in snapshot", hda_file not in blob,
          "contains hda file path")
    check("j no sandbox path in snapshot", SANDBOX not in blob,
          "contains sandbox path")
    check("j hip_file basename only",
          bool(resp.get("hip_file"))
          and "/" not in resp.get("hip_file", "")
          and "\\" not in resp.get("hip_file", ""),
          "hip_file=%r" % (resp.get("hip_file"),))
    # 默认 include_hda_internals=False 不展开内部（仅 HDA 节点本身）
    try:
        resp_no = instance.handle_capture_workflow_snapshot(
            node_path=asset.path(), include_vex=True, max_nodes=200)
        check("j internals off by default",
              resp_no.get("node_count", 0) == 1,
              "node_count=%r" % (resp_no.get("node_count"),))
    except Exception as exc:
        check("j internals off by default", False, "%r" % (exc,))
    # 官方内建节点（OPlib HDA，如 attribwrangle）内容锁定 → 不拆解分析
    try:
        official_w = geo_hda.createNode("attribwrangle", "official_w")
        official_w.parm("snippet").set("f@x = 1.0;")
        check("j official node is locked hda",
              official_w.type().definition() is not None
              and official_w.isEditable() is False,
              "has_def=%r isEditable=%r" % (
                  official_w.type().definition() is not None,
                  official_w.isEditable()))
        resp_official = instance.handle_capture_workflow_snapshot(
            node_path=official_w.path(), include_vex=True, max_nodes=200,
            include_hda_internals=True)
        check("j official node not expanded",
              resp_official.get("node_count", 0) == 1,
              "node_count=%r" % (resp_official.get("node_count"),))
        entry_o = (resp_official.get("nodes") or [{}])[0]
        check("j official node is_hda False",
              entry_o.get("is_hda") is False,
              "is_hda=%r" % (entry_o.get("is_hda"),))
        official_w.destroy()
    except Exception as exc:
        check("j official node not expanded", False, "%r" % (exc,))
    # 官方 subnet（可编辑网络容器，isEditable True）含用户内容 → 参与分析
    try:
        official_sub = geo_hda.createNode("subnet", "official_sub")
        sub_w = official_sub.createNode("attribwrangle", "sub_w")
        sub_w.parm("snippet").set("f@y = 2.0;")
        resp_sub = instance.handle_capture_workflow_snapshot(
            node_path=official_sub.path(), include_vex=True, max_nodes=200,
            include_hda_internals=True)
        sub_paths = [n.get("path") for n in resp_sub.get("nodes") or []]
        check("j editable network container expanded",
              resp_sub.get("node_count", 0) >= 2
              and official_sub.path() + "/sub_w" in sub_paths,
              "count=%r paths=%r" % (resp_sub.get("node_count"), sub_paths))
        official_sub.destroy()
    except Exception as exc:
        check("j editable network container expanded", False, "%r" % (exc,))
    # 官方 Editable Nodes 声明节点（bulletrbdsolver，Type Properties 中
    # Editable Nodes = dopnet/forces）：展开依据 = children() 非空（不是
    # isEditable——实机 rbdbulletsolver1 锁定态 isEditable False 但
    # children 307 全可读）。hython 下新建实例内容未实例化（children
    # 空）→ 无可展开，capture 自洽 node_count=1；实机 GUI 使用中的实例
    # children 300+（含 dopnet/forces 子网络）→ 参与分析。
    try:
        probe_dopnet = hou.node("/obj").createNode("dopnet", "bullet_probe")
        probe_solver = probe_dopnet.createNode("bulletrbdsolver", "solver1")
        check("j bullet solver editable state",
              probe_solver.isEditable() is True
              and probe_solver.type().definition() is None,
              "isEditable=%r defNone=%r" % (
                  probe_solver.isEditable(),
                  probe_solver.type().definition() is None))
        check("j bullet solver children are conditional",
              list(probe_solver.children()) == [],
              "children=%r" % ([c.name() for c in probe_solver.children()],))
        resp_bullet = instance.handle_capture_workflow_snapshot(
            node_path=probe_solver.path(), include_vex=True, max_nodes=300,
            include_hda_internals=True)
        check("j bullet solver capture self-consistent",
              resp_bullet.get("status") == "success"
              and resp_bullet.get("node_count", 0) == 1
              and not resp_bullet.get("truncated"),
              "status=%r count=%r" % (
                  resp_bullet.get("status"),
                  resp_bullet.get("node_count")))
        probe_dopnet.destroy()
        check("j bullet probe cleanup",
              hou.node("/obj/bullet_probe") is None)
    except Exception as exc:
        check("j bullet solver probe", False, "%r" % (exc,))
    # 隔离团队 root：文件仅一块 → 创建 → 原地更新 → 检索 → 未知 id
    hda_team_env = "LESSONS_SMOKE_HDA_TEAM"
    hda_team = os.path.join(SANDBOX, "hda_team")
    try:
        os.makedirs(hda_team, exist_ok=True)
        os.environ[hda_team_env] = hda_team
        with open(os.path.join(SANDBOX, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump([{"name": "hda_team",
                        "path": "${%s}" % hda_team_env,
                        "priority": 0.9, "writable": True}],
                      handle, ensure_ascii=False)
        check("j team root config written", True)
    except Exception as exc:
        check("j team root config written", False, "%r" % (exc,))
    recipe_fields = {
        "title": "自制 HDA 激活算法研究",
        "category": "sop",
        "severity": "medium",
        "affected_versions": "H21.0",
        "problem": ("research_asset（Sop/research_asset）的激活算法原理："
                    "内部 wrangle 按高度阈值输出激活值。"),
        "symptom": "需要在自定义资产内部按条件激活几何。",
        "fix": ("用 include_hda_internals=True 研究内部；正文索引用资产全名"
                " + 版本，不写本机路径。"),
        "advisory": True,
    }
    try:
        created = _lessons.save_recipe(hda_team, dict(recipe_fields))
        check("j recipe created", created["id"] == "BP-001"
              and created["action"] == "created",
              "id=%s action=%s" % (created["id"], created.get("action")))
    except Exception as exc:
        check("j recipe created", False, "%r" % (exc,))
        finish()
        return
    hda_recipes_file = _lessons.recipes_path(hda_team)
    try:
        text1 = _read_text(hda_recipes_file)
        check("j recipes file single block",
              text1.count("### BP-001") == 1 and "BP-002" not in text1,
              "blocks=%d" % text1.count("### BP-"))
    except Exception as exc:
        check("j recipes file single block", False, "%r" % (exc,))
    try:
        updated_fields = dict(recipe_fields)
        updated_fields["problem"] = ("加深后：activation 阈值由高度决定，"
                                     "核心原理是……（原地更新）")
        updated = _lessons.save_recipe(hda_team, updated_fields,
                                       recipe_id="BP-001")
        check("j recipe updated in place",
              updated["id"] == "BP-001" and updated["action"] == "updated",
              "id=%s action=%s" % (updated["id"], updated.get("action")))
    except Exception as exc:
        check("j recipe updated in place", False, "%r" % (exc,))
    try:
        text2 = _read_text(hda_recipes_file)
        check("j update replaces content",
              "加深后" in text2 and "按高度阈值输出激活值" not in text2,
              "has_new=%s" % ("加深后" in text2,))
        check("j update file still single block",
              text2.count("### BP-001") == 1 and "BP-002" not in text2,
              "blocks=%d" % text2.count("### BP-"))
        entries = _best_practices.parse_best_practices(text2)
        check("j update round-trip", len(entries) == 1
              and entries[0]["id"] == "BP-001",
              "entries=%d" % len(entries))
    except Exception as exc:
        check("j update file checks", False, "%r" % (exc,))
    try:
        found = _lessons_search.search_lessons(query="加深后", scope="hda_team")
        ids = [r["id"] for r in found.get("results", [])]
        check("j search hits updated recipe", "BP-001" in ids,
              "matched=%s ids=%r" % (found.get("matched"), ids))
        hit = next((r for r in found.get("results", [])
                    if r.get("id") == "BP-001"), None)
        if hit is not None:
            check("j hit source_root hda_team",
                  hit.get("source_root") == "hda_team",
                  "source_root=%r" % (hit.get("source_root"),))
    except Exception as exc:
        check("j search hits updated recipe", False, "%r" % (exc,))
    try:
        try:
            _lessons.save_recipe(hda_team, dict(recipe_fields),
                                 recipe_id="BP-999")
            check("j unknown id raises ls_recipe_not_found", False,
                  "no error raised")
        except _lessons.LessonsError as exc:
            check("j unknown id raises ls_recipe_not_found",
                  exc.code == "ls_recipe_not_found"
                  and "BP-001" in exc.message,
                  "code=%s msg=%s" % (exc.code, exc.message[:80]))
        text3 = _read_text(hda_recipes_file)
        check("j unknown id zero write",
              text3.count("### BP-001") == 1 and "BP-999" not in text3,
              "blocks=%d" % text3.count("### BP-"))
    except Exception as exc:
        check("j unknown id raises ls_recipe_not_found", False, "%r" % (exc,))
    # 清理：destroy HDA 场景节点
    try:
        hou.node("/obj/hda_research").destroy()
        check("j cleanup hda scene", hou.node("/obj/hda_research") is None)
    except Exception as exc:
        check("j cleanup hda scene", False, "%r" % (exc,))

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
                        "knowledge_stats", "capture_workflow_snapshot",
                        "save_recipe"):
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
