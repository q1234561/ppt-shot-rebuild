"""`deckalign --report` 的契约测试。

钉住三件事，都有反例：
1. 基准是**中位数**，不是第一页——第一页自己偏了，全表跟着偏；
2. 判据只算 `nav`，参考件（pill / div / deco）算进报告但**不进判据**——
   把 pill 算进极差会得到 499px 这种数，然后逼人去对齐一个"跟激活项走"的件；
3. 极差能指到具体件与字段（`range_on`），最大偏差能指到具体页（`max_abs_delta`）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))

import deckalign  # noqa: E402
import roles  # noqa: E402


def _write(page_dir: Path, boxes: dict) -> None:
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "spec.json").write_text(
        json.dumps({"containers": [{"id": k, "bbox": v} for k, v in boxes.items()]},
                   ensure_ascii=False), encoding="utf-8")


def _deck(pages):
    return {"visual": "t", "pages": pages,
            "roles": {p: {"nav": ["nav"], "nav_pill": ["pill"], "div": [],
                          "deco_l": [], "deco_r": [], "footer": []} for p in pages}}


class MedianBaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # nav.x0 = 40 / 42 / 44 → 中位 42。基准若取第一页会得 40，三页 Δ 变 0/2/4
        _write(self.root / "a", {"nav": [40, 20, 1640, 100]})
        _write(self.root / "b", {"nav": [42, 20, 1640, 100]})
        _write(self.root / "c", {"nav": [44, 20, 1640, 100]})
        self.deck = _deck(["a", "b", "c"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_median_not_first_page(self):
        rep = deckalign.build_report(self.deck, self.root)
        self.assertEqual(rep["median"]["nav"][0]["x0"], 42.0)
        self.assertEqual(rep["deltas"]["nav"]["a"][0]["dx0"], -2.0)
        self.assertEqual(rep["deltas"]["nav"]["c"][0]["dx0"], 2.0)

    def test_range_points_to_field(self):
        rep = deckalign.build_report(self.deck, self.root)
        self.assertEqual(rep["summary"]["range_px"], 4.0)
        self.assertEqual(rep["summary"]["range_on"], "nav.x0")

    def test_worst_page_is_named(self):
        rep = deckalign.build_report(self.deck, self.root)
        w = rep["summary"]["max_abs_delta"]
        self.assertEqual(w["abs_px"], 2.0)
        self.assertIn(w["page"], ("a", "c"))     # 两端并列，任取其一都必须是指到页的


class ReferencePartTest(unittest.TestCase):
    """参考件：位子差得再离谱，也不许把极差抬上去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write(self.root / "a", {"nav": [40, 20, 1640, 100], "pill": [100, 20, 300, 100]})
        _write(self.root / "b", {"nav": [40, 20, 1640, 100], "pill": [600, 20, 800, 100]})
        self.deck = _deck(["a", "b"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_pill_does_not_enter_range(self):
        rep = deckalign.build_report(self.deck, self.root)
        self.assertEqual(rep["summary"]["range_px"], 0.0, "pill 的 500px 位移不该进判据")
        self.assertEqual(rep["summary"]["range_on"], "nav.x0")
        self.assertIn("nav_pill", rep["summary"]["parts_reference_only"])

    def test_pill_still_reported(self):
        rep = deckalign.build_report(self.deck, self.root)
        self.assertEqual(rep["deltas"]["nav_pill"]["b"][0]["dx0"], 250.0)


@unittest.skipUnless((Path(os.environ.get("PPT_SERIES_ROOT") or "D:/WorkBuddy工作区/ppt制作")
                      / "spec.json").exists()
                     or Path(os.environ.get("PPT_SERIES_ROOT") or "D:/WorkBuddy工作区/ppt制作").is_dir(),
                     "整套目录不存在（设 PPT_SERIES_ROOT）")
class RealSeriesTest(unittest.TestCase):
    def test_position_is_aligned_after_apply(self):
        """这套在 2026-11-10 做过一次对齐（`--apply --write`）：对齐前 nav.x0 = 33~84（极差 51px），
        对齐后左边缘 0 误差。这里钉的是**当前状态**；历史值记在 CHANGELOG 2.54.0。

        宽高仍然不齐（宽 1552~1604、高 78~102）且**不进判据**：拉平它只能改字号/字距凑字宽，
        那是 SKILL §2 明令禁止的。"""
        deck = roles.load_deck(ROOT / "tests" / "fixtures" / "deck-9pages.json")
        rep = deckalign.build_report(deck, Path(os.environ.get("PPT_SERIES_ROOT")
                                                 or "D:/WorkBuddy工作区/ppt制作"))
        self.assertIn(rep["summary"]["range_on"], ("nav.x0", "nav.y0"))
        self.assertLessEqual(rep["summary"]["range_px"], 2.0, rep["summary"])
        self.assertEqual(rep["summary"]["judged_fields"], ["x0", "y0"])


if __name__ == "__main__":
    unittest.main()
