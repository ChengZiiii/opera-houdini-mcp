# -*- coding: utf-8 -*-
"""moveToGoodPosition 调用守卫（社区对证修复，2026-09-10）。

背景：fxhoudinimcp issue #37 实锤——``moveToGoodPosition()`` 的
``move_inputs`` / ``move_outputs`` / ``move_unconnected`` 默认 True，
放置一个新节点会把用户手工排布的既有节点一起拖走。修复 = 调用点全部
pin 三参数为 False（fork 当前唯一调用点在 server.py create_wrangle）。

本测试用 AST 扫描生产源码：
1. 所有 moveToGoodPosition 调用 MUST 显式传三个 kwargs 且值为 False；
2. 禁止新的裸调用混入（fxhoudinimcp 用同款 source guard 防回归）。
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PINNED = ("move_inputs", "move_outputs", "move_unconnected")
# 只扫生产模块（tests/ 与 e2e 脚本里的调用不在守卫范围）
_SCAN_FILES = ["server.py"]


def _iter_calls(source, filename):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "moveToGoodPosition":
                yield filename, node


class MoveToGoodPositionGuardTests(unittest.TestCase):

    def test_all_calls_pin_move_flags_false(self):
        violations = []
        total = 0
        for name in _SCAN_FILES:
            path = os.path.join(ROOT, name)
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
            for filename, call in _iter_calls(source, name):
                total += 1
                kwargs = {kw.arg: kw.value for kw in call.keywords
                          if kw.arg is not None}
                for flag in _PINNED:
                    val = kwargs.get(flag)
                    if not (isinstance(val, ast.Constant)
                            and val.value is False):
                        violations.append(
                            "{0}: moveToGoodPosition 缺 {1}=False".format(
                                filename, flag))
        self.assertTrue(total >= 1,
                        "守卫目标消失：server.py 应至少有一个调用点")
        self.assertEqual(
            violations, [],
            "moveToGoodPosition 必须显式 pin move_* 三参数为 False：{0}".format(
                violations))


if __name__ == "__main__":
    unittest.main()
