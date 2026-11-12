#!/usr/bin/env python3
"""两个模块各有一份 EXIT_NO_MODEL，必须同步——否则静默失配。

背景（2026-11-09）
------------------
`cutout.py` 用退出码 3 表示"缺模型，先配模型再跑"这类**可修复的拒绝**（区别于 1 = 真异常）。
`cycle.py` 里镜像了一份同名常量，而不是 `import cutout` —— 理由写在 cycle.py:69 的注释里：
cutout 顶层就 import cv2/numpy/PIL，为了一个整数把渲染链的解释器拖进这些重依赖不值得，
而且缺依赖时 import 本身就会炸掉 cycle。这个取舍是对的。

代价是：**没有同步机制**。哪天有人把 cutout 的 3 改成 4，cycle 会静默判错——
它不再把"缺模型"识别成可修复的拒绝，而是走 `[-300:]` 截断，把 16 行中文指引切成残句，
人又看不到原因了。这正是本文件要钉住的那条缝。

为什么用 ast 读源文件而不是 import
----------------------------------
import 会拉进 onnxruntime / cv2 / pptx 一整条依赖链，测试从 0.02 秒变几秒，
且在没有 onnxruntime 的机器上直接 error。这里比的是**字面量是否一致**，
ast 足够，也更快。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
NAME = "EXIT_NO_MODEL"


def _module_const(path: Path, name: str):
    """从源文件顶层读一个常量赋值（只认字面量，不做求值）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    return None


class ExitCodeSyncTest(unittest.TestCase):
    def test_no_model_code_matches_between_cutout_and_cycle(self):
        cut = _module_const(SCRIPTS / "cutout.py", NAME)
        cyc = _module_const(SCRIPTS / "delivery" / "cycle.py", NAME)
        self.assertIsNotNone(cut, f"cutout.py 里找不到 {NAME}")
        self.assertIsNotNone(cyc, f"cycle.py 里找不到 {NAME}（镜像常量被删了？）")
        self.assertEqual(
            cyc, cut,
            f"两边 {NAME} 失配：cutout={cut} / cycle={cyc}。"
            f"cycle 靠这个数字区分'缺模型（可修复）'与'真异常'，失配会让 16 行指引被截断。",
        )

    def test_no_model_code_is_three(self):
        """钉住取值本身：3 是 cutout 与调用方之间的口头契约，改动必须双方一起。"""
        self.assertEqual(_module_const(SCRIPTS / "cutout.py", NAME), 3)


if __name__ == "__main__":
    unittest.main()
