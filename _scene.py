"""_scene.py — opera-houdini-mcp 场景 CRUD 与序列化（PR 5）。

模块职责：
- get_scene_info: 场景元信息（含 houdini_version / node_count / file_path）
- save_scene: hou.hipFile.save 包装
- load_scene: hou.hipFile.load 包装 + 缓存失效
- new_scene: hou.hipFile.clear 包装 + 缓存失效
- serialize_scene: 全场景递归序列化（thin wrapper around cmn.serialize_scene_state）

注意：
- get_last_scene_diff 已在 server.py 实现（PR 4），不在本模块重复。
- hou 隔离：本模块不顶层 import hou；hou 通过参数注入（测试用 mock）。
- 缓存失效：load_scene / new_scene 调用 cmn.invalidate_all_caches()。
  PR 5 占位实现为 no-op，PR 6 替换为 NodeTypeCache-aware 版本。
"""
from . import _common as cmn


def get_scene_info(hou):
    """返回场景元信息 dict。

    字段：
    - houdini_version: hou.houdiniVersion() 字符串
    - node_count: 全场景节点数（hou.node('/').allSubChildren() 长度，失败回退 0）
    - file_path: hou.hipFile.name() 字符串
    - fps / start_frame / end_frame: 时间线相关
    """
    info = {}
    try:
        info["houdini_version"] = hou.houdiniVersion()
    except Exception:
        info["houdini_version"] = ""
    try:
        root = hou.node("/")
        if root is not None:
            try:
                children = root.allSubChildren()
                info["node_count"] = len(children)
            except Exception:
                # 部分 hou 版本无 allSubChildren；退化用 children()
                info["node_count"] = len(root.children())
        else:
            info["node_count"] = 0
    except Exception:
        info["node_count"] = 0
    try:
        info["file_path"] = hou.hipFile.name() or ""
    except Exception:
        info["file_path"] = ""
    # 补充轻量时间线信息，便于上层 UI 展示
    try:
        info["fps"] = hou.fps()
    except Exception:
        info["fps"] = 0
    try:
        fr = hou.playbar.frameRange()
        info["start_frame"] = fr[0]
        info["end_frame"] = fr[1]
    except Exception:
        info["start_frame"] = 0
        info["end_frame"] = 0
    return info


def save_scene(hou, file_path):
    """保存当前 .hip 文件到 file_path，返回成功 dict。异常向上传播。"""
    hou.hipFile.save(file_path=file_path)
    return {
        "saved": True,
        "file_path": file_path,
    }


def load_scene(hou, file_path):
    """加载 file_path 为当前 .hip 文件，返回成功 dict。

    加载完成后调用 cmn.invalidate_all_caches() 让上层缓存模块感知场景切换
    （PR 5 占位 no-op，PR 6 替换为真实清空）。
    """
    hou.hipFile.load(file_path)
    cmn.invalidate_all_caches()
    return {
        "loaded": True,
        "file_path": file_path,
    }


def new_scene(hou):
    """新建空白场景（hou.hipFile.clear），返回成功 dict。

    suppress_save_prompt=True 避免在 MCP 流程中触发交互式保存提示。
    完成后调用 cmn.invalidate_all_caches()。
    """
    hou.hipFile.clear(suppress_save_prompt=True)
    cmn.invalidate_all_caches()
    return {
        "cleared": True,
    }


def serialize_scene(hou, root_path=None, include_params=False, max_depth=3):
    """全场景递归序列化 thin wrapper，转发到 cmn.serialize_scene_state。

    保留独立入口便于上层 (_scene.*) 调用语义统一；具体序列化逻辑集中在
    _common.serialize_scene_state。
    """
    return cmn.serialize_scene_state(
        hou,
        root_path=root_path,
        include_params=include_params,
        max_depth=max_depth,
    )
