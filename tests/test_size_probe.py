#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""产物侧字号精确检查的契约测试（size_probe）。

为什么这条判据值得单测：它没有容差，三态由 `a:rPr/@sz` 的整数直接决定，
所以测试可以断言**精确值**；任何"差一点点也算过"的改动都会立刻变红。

四条：
  1. 全部 match → ok；
  2. 改一个 run 的字号 → mismatch（含 delta）；
  3. 合并视图里没有 size_pt 但 pptx 里有这块 → defaulted（build 的静默默认值）；
  4. pptx 里多出 spec 没有的文字形状 → unexpected，但**不算失败**（WARN）。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "scripts" / "delivery"))

import size_probe                      # noqa: E402
from pptx import Presentation          # noqa: E402
from pptx.util import Inches, Pt       # noqa: E402


def _pptx(path: Path, blocks: dict[str, float]) -> Path:
    """造一份最小 pptx：每个 (名字 → 字号) 一个文本框。"""
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])     # 6 = Blank
    for name, size in blocks.items():
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(0.6))
        box.name = name                                     # consts.shape_id 取 cNvPr@name
        run = box.text_frame.paragraphs[0].add_run()
        run.text = name
        run.font.size = Pt(size)
    prs.save(str(path))
    return path


def _page(td, sizes: dict[str, float | None]) -> Path:
    """造一个最小页目录：spec.json 只有 elements（无 override）。"""
    page = Path(td)
    els = []
    for i, (eid, sz) in enumerate(sizes.items()):
        el = {"id": eid, "type": "text", "text": eid, "weight": "regular",
              "bbox": [10, 10 + i * 40, 200, 40 + i * 40]}
        if sz is not None:
            el["size_pt"] = sz
        els.append(el)
    spec = {"canvas": {"w": 400, "h": 300, "dpi": 125.4}, "elements": els,
            "containers": [], "assets": []}
    (page / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return page


def _run(page: Path, pptx: Path) -> tuple[int, str]:
    old = sys.argv
    sys.argv = ["size_probe.py", "--page", str(page), "--pptx", str(pptx)]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = size_probe.main()
    finally:
        sys.argv = old
    return rc, buf.getvalue()


class TestSizeProbe(unittest.TestCase):
    def test_all_match(self):
        with tempfile.TemporaryDirectory() as td:
            page = _page(td, {"t1": 18.0, "t2": 22.5})
            pptx = _pptx(page / "a.pptx", {"t1": 18, "t2": 22.5})
            rc, out = _run(page, pptx)
            self.assertEqual(rc, 0)
            self.assertIn("match 2 / mismatch 0", out)

    def test_mismatch_is_fail_with_exact_delta(self):
        with tempfile.TemporaryDirectory() as td:
            page = _page(td, {"t1": 18.0})
            pptx = _pptx(page / "a.pptx", {"t1": 17.5})
            rc, out = _run(page, pptx)
            self.assertEqual(rc, 1)
            self.assertIn("mismatch t1", out)
            self.assertIn("-0.5", out)          # 差值精确，不是"小于容差"

    def test_missing_shape_is_fail(self):
        with tempfile.TemporaryDirectory() as td:
            page = _page(td, {"t1": 18.0, "t2": 22.5})
            pptx = _pptx(page / "a.pptx", {"t1": 18})     # t2 没有形状
            rc, out = _run(page, pptx)
            self.assertEqual(rc, 1)
            self.assertIn("missing  t2", out)

    def test_defaulted_when_merged_has_no_size(self):
        """合并视图里没有 size_pt → build 会画成默认 14/18pt，这条必须抓出来。"""
        with tempfile.TemporaryDirectory() as td:
            page = _page(td, {"t1": 18.0, "t9": None})    # t9 没有 size_pt
            pptx = _pptx(page / "a.pptx", {"t1": 18, "t9": 18})
            rc, out = _run(page, pptx)
            self.assertEqual(rc, 1)
            self.assertIn("defaulted t9", out)
            self.assertIn("静默默认值", out)

    def test_unexpected_is_warn_not_fail(self):
        with tempfile.TemporaryDirectory() as td:
            page = _page(td, {"t1": 18.0})
            pptx = _pptx(page / "a.pptx", {"t1": 18, "extra": 20})
            res = size_probe.probe(
                json.loads((page / "spec.json").read_text(encoding="utf-8")),
                Presentation(str(pptx)))
            self.assertTrue(res["ok"])                      # WARN 不算失败
            self.assertEqual([u["shape"] for u in res["unexpected"]], ["extra"])

    def test_ambiguous_pptx_requires_explicit(self):
        """页目录下多个 pptx 时必须 REFUSED，不替人猜哪个是成品。"""
        with tempfile.TemporaryDirectory() as td:
            page = _page(td, {"t1": 18.0})
            _pptx(page / "a.pptx", {"t1": 18})
            _pptx(page / "b.pptx", {"t1": 18})
            old = sys.argv
            sys.argv = ["size_probe.py", "--page", str(page)]
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    rc = size_probe.main()
            finally:
                sys.argv = old
            self.assertEqual(rc, 2)
            self.assertIn("请用 --pptx 指定", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
