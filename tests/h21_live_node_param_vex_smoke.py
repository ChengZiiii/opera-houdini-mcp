"""h21_live_node_param_vex_smoke.py — add-node-parameter-vex-tools H21.0 live smoke。

执行前置：本机已安装 H21.0.596（HFS=C:/PROGRA~1/SIDEF~1/HOUDIN~1.596）。
H22 未安装 → 所有 H22 路径 SKIP 阻塞，不允许 mock。

覆盖：
- 节点：rename / copy / move 真实 hou.copyNodesTo / moveNodesTo
- 参数：get / set / get_expression / revert / lock / link（Parm.set(Parm)）
- spare：单/批量 PTG 提交，验证 parm 真存在且 name/type 正确
- batch 失败零部分提交
- VEX：validate_vex 合法 / 非法 / 错误 context
- VEX：create_vex_expression + get_wrangle_code 圆形
- modify_node flags 扩展：display / bypass / selectable / template

不依赖 H22；H22 live smoke 阻塞 SKIP。
"""
import os
import sys
import unittest


# 让 hou 来自 hython 自身
import hou  # noqa: E402


# hython 不自动把 houdinimcp 加到 sys.path；显式 prepend 父目录
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE)  # houdinimcp/
_PARENT_DIR = os.path.dirname(_SRC_DIR)  # external/
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)


HFS = hou.getenv("HFS")
VCC = os.path.join(HFS, "bin", "vcc.exe")


def _make_sop_parent():
    """每次创建全新 SOP 网络，避免 undo 干扰。"""
    geo = hou.node("/obj").createNode("geo", "NPV_smoke_geo")
    geo.moveToGoodPosition()
    return geo


def _drain_undo():
    """清理当前 undo stack（用 hou.undos.performUndo + undoLabels 迭代）。"""
    hnds = hou.undos
    for _ in range(50):
        if not hnds.undoLabels():
            break
        hnds.performUndo()


class H21RenameCopyMoveTests(unittest.TestCase):

    def test_rename_node(self):
        _drain_undo()
        geo = _make_sop_parent()
        box = geo.createNode("box", "npv_box1")
        box_path = box.path()
        from houdinimcp._graph_edit import rename_node
        result = rename_node(hou, box_path, "npv_box2")
        self.assertEqual(result["old_name"], "npv_box1")
        self.assertEqual(result["new_name"], "npv_box2")
        self.assertEqual(hou.node(result["path"]).name(), "npv_box2")
        hou.undos.performUndo()

    def test_copy_node(self):
        _drain_undo()
        geo = _make_sop_parent()
        box = geo.createNode("box", "npv_box3")
        box_path = box.path()
        other = geo.createNode("subnet", "npv_other_net")
        other_path = other.path()
        from houdinimcp._graph_edit import copy_node
        result = copy_node(hou, box_path, other_path, name="npv_box_copy")
        self.assertEqual(result["name"], "npv_box_copy")
        self.assertIsNotNone(hou.node(other_path + "/npv_box_copy"))
        hou.undos.performUndo()

    def test_move_node(self):
        _drain_undo()
        geo = _make_sop_parent()
        box = geo.createNode("box", "npv_box4")
        box_path = box.path()
        other = geo.createNode("subnet", "npv_other_net2")
        other_path = other.path()
        from houdinimcp._graph_edit import move_node
        result = move_node(hou, box_path, other_path)
        self.assertEqual(result["src_path"], box_path)
        self.assertIsNotNone(hou.node(other_path + "/npv_box4"))
        hou.undos.performUndo()


class H21ParameterTests(unittest.TestCase):

    def test_get_set_revert(self):
        _drain_undo()
        geo = _make_sop_parent()
        box = geo.createNode("box", "npv_parm1")
        box_path = box.path()
        from houdinimcp._parameters import (
            get_parameter, set_parameter, revert_parameter,
        )
        gp = get_parameter(hou, box_path, "sizex")
        self.assertEqual(gp["parameter"], "sizex")
        sp = set_parameter(hou, box_path, "sizex", 9.5)
        self.assertEqual(sp["old"], gp["value"])
        self.assertEqual(sp["new"], 9.5)
        rp = revert_parameter(hou, box_path, "sizex")
        self.assertEqual(rp["value"], gp["value"])
        hou.undos.performUndo()  # revert
        hou.undos.performUndo()  # set

    def test_get_expression_and_link(self):
        _drain_undo()
        geo = _make_sop_parent()
        a = geo.createNode("box", "npv_a")
        a_path = a.path()
        b = geo.createNode("box", "npv_b")
        b_path = b.path()
        from houdinimcp._parameters import (
            get_expression, link_parameters, lock_parameter,
        )
        # 显式 setExpression 后 get_expression 应返回非空
        hou.node(b_path).parm("sizex").setExpression(
            "ch('../%s/sizex')" % hou.node(a_path).name())
        ge = get_expression(hou, b_path, "sizex")
        self.assertIsNotNone(ge["expression"])
        # link_parameters: a.sizey 链接到 b.sizey
        link_parameters(hou, a_path + ".sizey", b_path + ".sizey")
        self.assertEqual(
            hou.node(a_path).parm("sizey").eval(),
            hou.node(b_path).parm("sizey").eval(),
        )
        # lock
        lr = lock_parameter(hou, b_path, "sizex", True)
        self.assertTrue(lr["locked"])
        hou.undos.performUndo()
        hou.undos.performUndo()


class H21SpareParameterTests(unittest.TestCase):

    def test_single_spare_float(self):
        _drain_undo()
        geo = _make_sop_parent()
        box = geo.createNode("box", "npv_spare1")
        box_path = box.path()
        from houdinimcp._parameters import create_spare_parameter
        create_spare_parameter(
            hou, box_path, "myFloat", "float",
            label="My Float", default=(0.5,),
        )
        p = hou.node(box_path).parm("myFloat")
        self.assertIsNotNone(p)
        self.assertEqual(p.eval(), 0.5)
        hou.undos.performUndo()

    def test_batch_spare_atomic(self):
        _drain_undo()
        geo = _make_sop_parent()
        box = geo.createNode("box", "npv_spare2")
        box_path = box.path()
        from houdinimcp._parameters import create_spare_parameters
        # 全部合法
        create_spare_parameters(
            hou, box_path,
            [
                {"name": "b1", "data_type": "float", "default": (0.0,)},
                {"name": "b2", "data_type": "int", "default": (1,)},
                {"name": "b3", "data_type": "string", "default": ("x",)},
            ],
        )
        box = hou.node(box_path)
        self.assertIsNotNone(box.parm("b1"))
        self.assertIsNotNone(box.parm("b2"))
        self.assertIsNotNone(box.parm("b3"))
        hou.undos.performUndo()
        # 第二项非法 → 全部拒绝
        try:
            create_spare_parameters(
                hou, box_path,
                [
                    {"name": "ok1", "data_type": "float"},
                    {"name": "bad", "data_type": "invalid"},
                ],
            )
            self.fail("must raise")
        except ValueError:
            pass
        # ok1 也不应存在
        self.assertEqual(hou.node(box_path).parm("ok1") is None, True)


class H21VexTests(unittest.TestCase):

    def test_validate_vex_legal(self):
        from houdinimcp._graph_edit import validate_vex
        # cvex 函数体内合法的赋值语句
        result = validate_vex(hou, "vector p = {0,0,0};", context="cvex")
        self.assertTrue(result["valid"], "diagnostics: {0}, rc={1}".format(
            result.get("diagnostics"), result.get("returncode")))
        self.assertEqual(result["context"], "cvex")

    def test_validate_vex_illegal(self):
        from houdinimcp._graph_edit import validate_vex
        # 故意写错语法：未声明的 unknown token
        result = validate_vex(hou, "@P = {0,0,0};", context="cvex")
        self.assertFalse(result["valid"])
        # 必须有结构化 diagnostics
        self.assertTrue(len(result["diagnostics"]) > 0,
                        "diagnostics must be non-empty; got rc={0}".format(
                            result.get("returncode")))
        d = result["diagnostics"][0]
        self.assertIn("severity", d)
        self.assertIn("line", d)
        self.assertIn("column", d)
        self.assertIn("message", d)

    def test_validate_vex_invalid_context(self):
        from houdinimcp._graph_edit import validate_vex
        with self.assertRaises(ValueError):
            validate_vex(hou, "vector p = {0,0,0};", context="bogus")

    def test_create_and_get_wrangle_code(self):
        _drain_undo()
        geo = _make_sop_parent()
        geo_path = geo.path()
        from houdinimcp._graph_edit import create_vex_expression, get_wrangle_code
        path = create_vex_expression(
            hou, geo_path, "@P.y += 1.0;", attrib_class="point",
            name="npv_wrangle")
        self.assertEqual(path["name"], "npv_wrangle")
        self.assertEqual(path["attrib_class"], "point")
        wr = get_wrangle_code(hou, path["path"])
        self.assertEqual(wr["code"], "@P.y += 1.0;")
        hou.undos.performUndo()


class H21ModifyNodeFlagsTests(unittest.TestCase):

    def test_modify_node_no_flags_backward_compatible(self):
        _drain_undo()
        geo = _make_sop_parent()
        box = geo.createNode("box", "npv_modify1")
        box_path = box.path()
        from houdinimcp._parameters import _flag_helper
        # 不传 flags 时 _flag_helper 返回 ({}, [])，不影响 scene
        node = hou.node(box_path)
        applied, unsupported = _flag_helper(hou, node, None)
        self.assertEqual(applied, {})
        self.assertEqual(unsupported, [])
        hou.undos.performUndo()

    def test_modify_node_with_flags(self):
        _drain_undo()
        geo = _make_sop_parent()
        box = geo.createNode("box", "npv_modify2")
        box_path = box.path()
        from houdinimcp._parameters import _flag_helper
        node = hou.node(box_path)
        applied, unsupported = _flag_helper(
            hou, node, {"display": True, "bypass": True,
                       "selectable": False, "template": True})
        self.assertEqual(applied["display"], True)
        self.assertEqual(applied["bypass"], True)
        self.assertTrue(hou.node(box_path).isDisplayFlagSet())
        self.assertTrue(box.isBypassed())
        self.assertTrue(box.isTemplateFlagSet())
        hou.undos.performUndo()


def _skip_h22(reason):
    """H22 未安装；显式 SKIP，不 mock。"""
    print("[H22 SKIP] {0}".format(reason))


if __name__ == "__main__":
    print("H21.0 live smoke: HFS={0}".format(HFS))
    print("VCC: {0} exists={1}".format(VCC, os.path.isfile(VCC)))
    unittest.main(verbosity=2)
    _skip_h22("H22 not installed in this environment")
