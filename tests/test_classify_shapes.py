# -*- coding: utf-8 -*-
"""派生清单（classify_shapes）的契约。

它回答客户的第一句话「哪些能改」，所以它错的方式不是报错，是**给出一份看起来
合理的错清单**——比如果先按类型分堆、再补一句"这个位图已说明"，
就等于给"文字被贴成图"留了洗白路径。

三条用例对应三条契约：
  ① 真文本 → native（可改）
  ② 位图 → pictures，**不进 native**
  ③ 未知形态 → unproven，**既不进 native 也不进 pictures**
"""
import unittest
from pathlib import Path
import tempfile

from PIL import Image
from pptx import Presentation
from pptx.util import Pt

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "delivery"))

from editability import classify_shapes   # noqa: E402


def _blank():
    prs = Presentation()
    return prs, prs.slides.add_slide(prs.slide_layouts[6])


class ClassifyShapesTest(unittest.TestCase):
    def test_text_is_native(self):
        prs, slide = _blank()
        tb = slide.shapes.add_textbox(Pt(10), Pt(10), Pt(100), Pt(40))
        tb.text_frame.text = "能改"
        tb.name = "t1"
        c = classify_shapes(prs)
        self.assertIn("t1", [i["id"] for i in c["native"]])
        self.assertEqual(c["native"][0]["kind"], "text")

    def test_picture_is_not_native(self):
        """位图必须被摘到 pictures——它是"客户改不了"的唯一正当出口。"""
        prs, slide = _blank()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.png"
            Image.new("RGBA", (20, 20), (200, 30, 40, 255)).save(p)
            pic = slide.shapes.add_picture(str(p), Pt(10), Pt(10), Pt(40), Pt(40))
            pic.name = "a1"
            c = classify_shapes(prs)
        ids_native = [i["id"] for i in c["native"]]
        ids_pic = [i["id"] for i in c["pictures"]]
        self.assertIn("a1", ids_pic)
        self.assertNotIn("a1", ids_native, "位图不得进 native——那就是洗白入口")

    def test_counts_add_up(self):
        """三格之和必须等于 total：漏遍历会让总数对不上，而不是静默少数。"""
        prs, slide = _blank()
        tb = slide.shapes.add_textbox(Pt(10), Pt(10), Pt(100), Pt(40))
        tb.text_frame.text = "x"
        tb.name = "t1"
        c = classify_shapes(prs)
        self.assertEqual(c["total"], len(c["native"]) + len(c["pictures"]) + len(c["unproven"]))

    def test_empty_textbox_is_shape_not_text(self):
        """空文字帧不归 "text"，但仍是原生形状（客户能往里打字）→ kind 是 shape。

        这条实测纠正过一次：我原以为空文本帧该被排除，实际它是个可编辑的空框，
        排除掉会告诉客户"这里不能改"，而它能改。**判据要按客户能做什么，不按我以为它是什么。**
        """
        prs, slide = _blank()
        tb = slide.shapes.add_textbox(Pt(10), Pt(10), Pt(100), Pt(40))
        tb.text_frame.text = "   "
        tb.name = "t0"
        kinds = {i["id"]: i["kind"] for i in classify_shapes(prs)["native"]}
        self.assertNotEqual(kinds.get("t0"), "text", "空框不该算「文字块」")
        self.assertIn("t0", kinds, "但它仍是原生可改对象，不能排除")


if __name__ == "__main__":
    unittest.main()
