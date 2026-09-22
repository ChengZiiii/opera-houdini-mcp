"""versioned-rag-index task 2.1/2.2 单测：get_scene_info 的 hfs_path 字段。

server.py 顶层 ``import hou``，非 Houdini 环境不能直接 import——沿用
test_connection.py 的 AST 提取 + exec 技术：把 ``_resolve_hfs_path`` /
``_hfs_long_path`` / ``get_scene_info`` 源段 exec 到带 mock hou 的
namespace 里跑。

覆盖：
- 三级回退链：hou.text.expandString → hou.expandStringAt（单参/双参
  两种 HOM 签名）→ env HFS
- 8.3 短名 realpath 归一为长名
- 全部失败：字段缺省（不抛错、不回空串误导）；既有字段零回归

hython 隔离实机验证（真实 HFS 路径）属 apply 记录的手动步骤，不在此重复。
"""

import ast
import importlib.util as _ilu
import os
import sys
import traceback as traceback_mod
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_PY = os.path.join(ROOT, "server.py")

# Package bootstrap（同 test_connection.py：exec 的源段引用 `from . import
# _scene` 不在此路径，但保持一致习惯；get_scene_info 引用 scn 为全局名）
_PKG_KEY = "houdinimcp"
_CMN_KEY = "houdinimcp._common"
if _PKG_KEY not in sys.modules:
    _pkg = types.ModuleType(_PKG_KEY)
    _pkg.__path__ = [ROOT]
    sys.modules[_PKG_KEY] = _pkg
if _CMN_KEY not in sys.modules:
    _spec = _ilu.spec_from_file_location(
        _CMN_KEY, os.path.join(ROOT, "_common.py"))
    _cmn = _ilu.module_from_spec(_spec)
    sys.modules[_CMN_KEY] = _cmn
    _spec.loader.exec_module(_cmn)


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _extract_functions(source, names):
    tree = ast.parse(source)
    found = {}
    # get_scene_info 是类方法（嵌在 ClassDef 里），walk 同时覆盖模块级
    # 函数（_resolve_hfs_path / _hfs_long_path）与类方法
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = ast.get_source_segment(source, node)
    missing = set(names) - set(found)
    assert not missing, "functions not found in server.py: %s" % missing
    return found


_SOURCES = _extract_functions(
    _read(SERVER_PY), ("_resolve_hfs_path", "_hfs_long_path",
                       "get_scene_info"))

_HFS_SHORT = "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.596"
_HFS_LONG = os.path.realpath(_HFS_SHORT) if os.path.isdir(_HFS_SHORT) \
    else _HFS_SHORT  # 非 Windows CI 无 8.3 短名，退化为等值


def _make_hou(hfs_via_text=None, hfs_via_at=None, at_two_arg=False,
              fail_text=False, fail_at=False):
    """构造 get_scene_info 所需的 mock hou（hfs 三级回退可配置）。"""
    hou = mock.Mock()
    hou.applicationVersionString.return_value = "21.0.596"
    hou.hipFile.name.return_value = "C:/t/unit.hipnc"
    hou.hipFile.isNewFile.return_value = False
    hou.fps.return_value = 24
    hou.playbar.frameRange.return_value = (1, 240)
    root = mock.Mock()
    root.node.return_value = None  # contexts 全部跳过
    hou.node.return_value = root

    if fail_text:
        hou.text.expandString.side_effect = AttributeError("no text")
    else:
        hou.text.expandString.return_value = hfs_via_text
    if fail_at:
        hou.expandStringAt.side_effect = AttributeError("no at")
    elif at_two_arg:
        # 单参形态抛 TypeError → 代码重试双参形态
        hou.expandStringAt.side_effect = [
            TypeError("missing at"), hfs_via_at]
    else:
        hou.expandStringAt.return_value = hfs_via_at
    return hou


def _make_scn_meta():
    return {
        "houdini_version": "21.0.596",
        "node_count": 3,
        "file_path": "C:/t/unit.hipnc",
        "fps": 24,
        "start_frame": 1,
        "end_frame": 240,
    }


def _exec_get_scene_info(hou):
    """exec 三个函数源段到共享 namespace 并调用 get_scene_info。"""
    scn = mock.Mock()
    scn.get_scene_info.return_value = _make_scn_meta()
    namespace = {"hou": hou, "os": os, "scn": scn, "traceback": traceback_mod}
    exec(compile(_SOURCES["_hfs_long_path"], "<hfs_long>", "exec"), namespace)
    exec(compile(_SOURCES["_resolve_hfs_path"], "<resolve_hfs>", "exec"),
         namespace)
    exec(compile(_SOURCES["get_scene_info"], "<get_scene_info>", "exec"),
         namespace)
    # 类方法源段带 self 形参（方法体不使用），传占位对象即可
    return namespace["get_scene_info"](object()), scn


class ResolveHfsPathTests(unittest.TestCase):

    def test_primary_text_expand_string_with_shortname_norm(self):
        hou = _make_hou(hfs_via_text=_HFS_SHORT)
        info, _ = _exec_get_scene_info(hou)
        self.assertEqual(info["hfs_path"], _HFS_LONG)

    def test_fallback_to_expand_string_at_single_arg(self):
        hou = _make_hou(fail_text=True, hfs_via_at=_HFS_SHORT)
        info, _ = _exec_get_scene_info(hou)
        self.assertEqual(info["hfs_path"], _HFS_LONG)

    def test_fallback_to_expand_string_at_two_arg_signature(self):
        hou = _make_hou(fail_text=True, hfs_via_at=_HFS_SHORT,
                        at_two_arg=True)
        info, _ = _exec_get_scene_info(hou)
        self.assertEqual(info["hfs_path"], _HFS_LONG)

    def test_fallback_to_env_hfs(self):
        hou = _make_hou(fail_text=True, fail_at=True)
        env = {"HFS": _HFS_SHORT}
        with mock.patch.dict(os.environ, env, clear=False):
            info, _ = _exec_get_scene_info(hou)
        self.assertEqual(info["hfs_path"], _HFS_LONG)

    def test_total_failure_field_absent_not_raised(self):
        hou = _make_hou(fail_text=True, fail_at=True)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HFS", None)
            info, _ = _exec_get_scene_info(hou)
        self.assertNotIn("hfs_path", info,
                         "解析失败时字段必须缺省，不得回空串误导 bridge")


class SceneInfoRegressionTests(unittest.TestCase):

    def test_existing_fields_unchanged(self):
        hou = _make_hou(hfs_via_text=_HFS_SHORT)
        info, scn = _exec_get_scene_info(hou)
        for key in ("name", "filepath", "houdini_version", "node_count",
                    "file_path", "fps", "start_frame", "end_frame",
                    "contexts"):
            self.assertIn(key, info, "既有字段回归缺失: %s" % key)
        self.assertEqual(info["houdini_version"], "21.0.596")
        self.assertEqual(info["name"], "unit.hipnc")
        self.assertEqual(info["contexts"], {})
        scn.get_scene_info.assert_called_once()

    def test_hfs_absent_when_unresolvable_keeps_scene_fields(self):
        hou = _make_hou(fail_text=True, fail_at=True)
        os.environ.pop("HFS", None)
        try:
            info, _ = _exec_get_scene_info(hou)
        finally:
            pass
        self.assertNotIn("hfs_path", info)
        for key in ("name", "houdini_version", "contexts"):
            self.assertIn(key, info)


if __name__ == "__main__":
    unittest.main()
