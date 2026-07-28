"""H21.0 live smoke for add-hip-offline-parser（手动运行，不进 pytest 自动收集）。

在真实 H21.0 ``hython`` 下：
1. 构造一个确定性最小场景（/obj/geo1 + box1 + xform1 + postit + netbox）。
2. 保存为临时 ``.hip``。
3. 用纯 stdlib 的 ``_hip_parser`` 离线解析该文件。
4. 把解析结果与 ``hou`` 自身的 ground truth 逐项比对（**不使用 mock**）。

运行方式::

    "C:/Program Files/Side Effects Software/Houdini 21.0.596/bin/hython.exe" ^
        external/houdinimcp/tests/h21_live_hip_smoke.py

退出码 0 = 全部 PASS；非 0 = 有 FAIL。

H22 未安装时无法运行本 smoke（H22 live smoke 为本 change 的发布门禁之一，
显式记为阻塞，不用 mock 代替）。
"""
import os
import sys
import tempfile

import hou

# 让 hython 能 import 同仓的纯 stdlib parser
_HERE = os.path.dirname(os.path.abspath(__file__))
_FORK_ROOT = os.path.dirname(_HERE)
if _FORK_ROOT not in sys.path:
    sys.path.insert(0, _FORK_ROOT)

import _hip_parser as hip  # noqa: E402  纯 stdlib，hython 可直接 import


def _rel(path):
    return path.lstrip("/")


def build_and_save():
    hou.hipFile.clear(suppress_save_prompt=True)
    obj = hou.node("/obj")
    geo1 = obj.createNode("geo", "geo1")
    box1 = geo1.createNode("box", "box1")
    xform1 = geo1.createNode("xform", "xform1")
    xform1.setInput(0, box1)
    xform1.setDisplayFlag(True)
    xform1.setRenderFlag(True)
    geo1.layoutChildren()
    try:
        sticky = obj.createStickyNote("note1")
        sticky.setText("hello postit")
    except Exception as exc:
        sys.stderr.write("STICKY_FAIL: {0!r}\n".format(exc))
    try:
        nb = obj.createNetworkBox("nb1")
        nb.setComment("mynetbox")
        nb.addNode(geo1)
    except Exception as exc:
        sys.stderr.write("NETBOX_FAIL: {0!r}\n".format(exc))

    fd, path = tempfile.mkstemp(suffix=".hip")
    os.close(fd)
    hou.hipFile.save(path)
    return path


def ground_truth():
    obj = hou.node("/obj")
    node_types = {}
    for n in obj.allSubChildren():
        node_types[_rel(n.path())] = n.type().name()
    connections = []
    for rel in node_types:
        node = hou.node("/" + rel)
        for i, src in enumerate(node.inputs()):
            if src is not None:
                connections.append((_rel(src.path()), rel, i))
    postits = []
    try:
        for sn in obj.stickyNotes():
            postits.append(("obj", sn.name(), sn.text()))
    except Exception:
        pass
    netboxes = []
    try:
        for nb in obj.networkBoxes():
            netboxes.append(("obj", nb.name(), nb.comment()))
    except Exception:
        pass
    return {
        "node_types": node_types,
        "connections": set(connections),
        "postits": set(postits),
        "netboxes": set(netboxes),
        "save_version": hou.applicationVersionString(),
    }


def main():
    results = []

    def check(name, cond, detail=""):
        results.append(("PASS" if cond else "FAIL", name, detail))
        return bool(cond)

    path = build_and_save()
    try:
        gt = ground_truth()
        env = hip.parse_hip_offline(path, include_params=True, max_depth=10)
        check("status success", env["status"] == "success",
              env.get("error", ""))
        check("save_version matches hou",
              env.get("save_version") == gt["save_version"],
              "{0!r} vs {1!r}".format(env.get("save_version"),
                                      gt["save_version"]))
        got_paths = {n["path"] for n in env["nodes"]}
        check("node paths match hou",
              got_paths == set(gt["node_types"].keys()),
              "got={0} want={1}".format(sorted(got_paths),
                                        sorted(gt["node_types"])))
        type_ok = all(n["type"] == gt["node_types"].get(n["path"])
                      for n in env["nodes"])
        check("node types match hou", type_ok)
        got_c = {(c["from"], c["to"], c["input_index"])
                 for c in env["connections"]}
        check("connections match hou", got_c == gt["connections"],
              "got={0} want={1}".format(sorted(got_c),
                                        sorted(gt["connections"])))
        got_p = {(p["context"], p["name"], p["text"])
                 for p in env["postits"]}
        check("postits match hou", got_p == gt["postits"])
        got_n = {(nb["context"], nb["name"], nb["label"])
                 for nb in env["netboxes"]}
        check("netboxes match hou", got_n == gt["netboxes"])
        check("trailer seen", env["metadata"]["trailer_seen"])
        # include_params：xform1 应带 parameters 序列化文本
        xf = [n for n in env["nodes"] if n["path"].endswith("xform1")]
        check("include_params carries serialized parm",
              bool(xf and "parameters" in xf[0]
                   and "version 0.8" in xf[0]["parameters"]))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    print("\n=== add-hip-offline-parser H21.0 live smoke ===")
    for tag, name, detail in results:
        line = "{0}  {1}".format(tag, name)
        if detail:
            line += "  -> {0}".format(detail)
        print(line)
    fails = [r for r in results if r[0] != "PASS"]
    print("\nverdict: {0}".format(
        "all_pass" if not fails else "fails: {0}".format(
            [r[1] for r in fails])))
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
