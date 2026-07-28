"""生成 HIP golden fixtures 与 manifest（在目标版本 Houdini 的 ``hython`` 下运行）。

用法（以 H21.0 为例）::

    "C:\\Program Files\\Side Effects Software\\Houdini 21.0.596\\bin\\hython.exe" ^
        external\\houdinimcp\\tests\\fixtures\\hip\\generate_fixtures.py --version h21

脚本会用 ``hou`` 构造一个**确定性**最小场景（/obj/geo1 + box1 + xform1 +
postit note1 + netbox nb1），分别保存 ``.hip`` / ``.hiplc`` / ``.hipnc``，
并用 ``hou`` 自身作为 ground truth 计算预期 node paths/types/connections/
postits/netboxes，连同每个文件的 SHA-256 写入/合并进 ``manifest.json``。

ground truth 取自 ``hou``（而非被测的 ``_hip_parser``），使 manifest 成为
独立的 golden 标准：fixture 解析结果必须与 manifest 一致，但不依赖 parser。
当 H20 / H22 环境可用时，用对应 hython 以 ``--version h20`` / ``--version h22``
重跑即可补齐 9-fixture 矩阵（见 change design D7）。
"""
import argparse
import hashlib
import json
import os
import sys

import hou


HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(HERE, "manifest.json")
SCHEMA = "hip-golden-fixtures/v1"


def build_scene():
    hou.hipFile.clear(suppress_save_prompt=True)
    obj = hou.node("/obj")
    geo1 = obj.createNode("geo", "geo1")
    box1 = geo1.createNode("box", "box1")
    xform1 = geo1.createNode("xform", "xform1")
    xform1.setInput(0, box1)
    xform1.setDisplayFlag(True)
    xform1.setRenderFlag(True)
    geo1.layoutChildren()
    # postit / netbox（确定性）
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


def _rel(path):
    return path.lstrip("/")


def collect_ground_truth():
    """用 hou 自身收集预期值（独立于被测 parser）。"""
    obj = hou.node("/obj")
    node_types = {}
    for n in obj.allSubChildren():
        node_types[_rel(n.path())] = n.type().name()

    connections = []
    for rel in node_types:
        node = hou.node("/" + rel)
        try:
            inputs = node.inputs()
        except Exception:
            inputs = []
        for i, src in enumerate(inputs):
            if src is not None:
                connections.append({
                    "from": _rel(src.path()),
                    "to": rel,
                    "input_index": i,
                    "source_output": 0,
                })

    postits = []
    try:
        for sn in obj.stickyNotes():
            text = ""
            try:
                text = sn.text()
            except Exception:
                pass
            postits.append({"context": "obj", "name": sn.name(), "text": text})
    except Exception:
        pass

    netboxes = []
    try:
        for nb in obj.networkBoxes():
            label = ""
            try:
                label = nb.comment()
            except Exception:
                pass
            netboxes.append({"context": "obj", "name": nb.name(), "label": label})
    except Exception:
        pass

    return {
        "node_paths": sorted(node_types.keys()),
        "node_types": node_types,
        "connections": connections,
        "postits": postits,
        "netboxes": netboxes,
    }


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("schema") == SCHEMA:
                return data
        except (OSError, ValueError):
            pass
    return {"schema": SCHEMA, "fixtures": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True,
                    help="版本目录名，如 h20 / h21 / h22")
    ap.add_argument("--houdini-version", default=None,
                    help="覆盖 manifest 记录的 Houdini 版本字符串"
                         "（默认取 hou.applicationVersionString()）")
    args = ap.parse_args()

    out_dir = os.path.join(HERE, args.version)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    build_scene()
    expected = collect_ground_truth()
    houdini_version = args.houdini_version or hou.applicationVersionString()

    manifest = load_manifest()
    existing = {
        (f.get("version"), f.get("edition")): f for f in manifest["fixtures"]}

    editions_saved = {}
    for ext in (".hip", ".hiplc", ".hipnc"):
        edition = ext.lstrip(".")
        path = os.path.join(out_dir, "minimal" + ext)
        try:
            hou.hipFile.save(path)
        except Exception as exc:
            sys.stderr.write("SAVE_FAIL {0}: {1!r}\n".format(path, exc))
            continue
        if not os.path.exists(path):
            continue
        digest = sha256_file(path)
        editions_saved[edition] = os.path.relpath(path, HERE).replace("\\", "/")
        fixture = {
            "version": args.version,
            "houdini_version": houdini_version,
            "edition": edition,
            "path": editions_saved[edition],
            "sha256": digest,
            "size_bytes": os.path.getsize(path),
            "expected": expected,
        }
        existing[(args.version, edition)] = fixture

    manifest["fixtures"] = [
        existing[k] for k in sorted(existing.keys(),
                                    key=lambda k: (k[0], k[1]))]

    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)

    print("VERSION", args.version, "houdini", houdini_version)
    print("EDITIONS", sorted(editions_saved.keys()))
    print("EXPECTED_NODES", expected["node_paths"])
    print("MANIFEST", MANIFEST_PATH,
          "total fixtures", len(manifest["fixtures"]))


if __name__ == "__main__":
    main()
