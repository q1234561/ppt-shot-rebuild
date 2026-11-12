"""覆盖率判据的契约测试（合成图，不依赖真实页面）。

四条：能抓到没被 bbox 接住的块、接住的不报、白名单能压掉、页边忽略带生效。
每条都是"喂已知答案的图，断言输出"——不是"跑一下没报错"。

为什么必须用合成图：真实页面上"漏做"和"背景插画"混在一起，人工都难分；
只有已知答案的图才能证明判据本身是准的，而不是被背景噪声喂出了个好看的数字。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))

import coverage  # noqa: E402

W, H = 400, 300


def _page(tmp: Path, boxes: list[list[float]], ink_rects: list[tuple]) -> Path:
    """造一页：浅蓝渐变底 + 指定位置的深色块 + 指定 spec bbox。"""
    page = tmp / "p"
    page.mkdir(parents=True, exist_ok=True)
    img = np.zeros((H, W, 3), np.uint8)
    for y in range(H):                              # 竖向渐变，模拟真实页面底色
        img[y, :] = (235 - y // 12, 242 - y // 12, 250 - y // 18)
    for x0, y0, x1, y1 in ink_rects:
        cv2.rectangle(img, (x0, y0), (x1, y1), (40, 45, 60), -1)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    buf.tofile(str(page / "reference.png"))
    (page / "spec.json").write_text(json.dumps(
        {"canvas": {"w_px": W, "h_px": H},
         "containers": [{"id": f"b{i}", "bbox": b} for i, b in enumerate(boxes)]},
        ensure_ascii=False), encoding="utf-8")
    return page


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_uncovered_block_is_reported(self):
        page = _page(self.root, [], [(200, 150, 260, 210)])
        rep = coverage.run(page, None, coverage.MIN_FRAC, coverage.INK_DIFF)
        self.assertEqual(rep["n_blocks"], 1, rep["blocks"])
        # 没给白名单 = 背景未定义 → unproven（不是 fail，也不阻塞）
        self.assertEqual(rep["status"], "unproven")
        b = rep["blocks"][0]
        # 允许 ±6px：中值滤波估背景会把边缘吃掉一点
        self.assertTrue(194 <= b["bbox"][0] <= 206 and 144 <= b["bbox"][1] <= 156, b["bbox"])

    def test_covered_block_is_not_reported(self):
        """spec 里有框接住它 → 不该报。这条防的是"报了但其实是做过的"。"""
        page = _page(self.root, [[198, 148, 262, 212]], [(200, 150, 260, 210)])
        rep = coverage.run(page, None, coverage.MIN_FRAC, coverage.INK_DIFF)
        self.assertEqual(rep["n_blocks"], 0, rep["blocks"])

    def test_whitelist_given_but_not_covering_is_fail(self):
        """给了白名单还留下块 = 人已经定义过背景，剩下的就是漏做 → fail（该拦）。"""
        page = _page(self.root, [], [(200, 150, 260, 210)])
        wl = self.root / "wl.json"
        wl.write_text(json.dumps({"rects": [[0, 0, 10, 10]]}), encoding="utf-8")
        rep = coverage.run(page, wl, coverage.MIN_FRAC, coverage.INK_DIFF)
        self.assertEqual(rep["n_blocks"], 1, rep["blocks"])
        self.assertEqual(rep["status"], "fail")

    def test_whitelist_suppresses(self):
        """白名单压掉 = 人明说"这块是背景，不重建"。"""
        page = _page(self.root, [], [(200, 150, 260, 210)])
        wl = self.root / "wl.json"
        wl.write_text(json.dumps({"rects": [[195, 145, 265, 215]]}), encoding="utf-8")
        rep = coverage.run(page, wl, coverage.MIN_FRAC, coverage.INK_DIFF)
        self.assertEqual(rep["n_blocks"], 0, rep["blocks"])
        self.assertEqual(rep["whitelist_rects"], 1)

    def test_edge_band_is_ignored(self):
        """贴着页边的一条墨迹（页边阴影/抗锯齿的样子）不该被当成内容。

        第一版这条用例画的是**跨满整行**的横条——那正好是页边距背景估计会把它当底色的情形，
        于是"通过"与 EDGE 无关，等于什么都没验。现在画局部横条，让两侧页边保持干净，
        这条才真的在验忽略带。
        """
        page = _page(self.root, [], [(200, 3, 300, 9)])   # 局部 6px 横条，落在 14px 忽略带内
        rep = coverage.run(page, None, coverage.MIN_FRAC, coverage.INK_DIFF)
        self.assertEqual(rep["n_blocks"], 0, rep["blocks"])

    def test_full_width_band_is_marked_unjudgeable(self):
        """跨满整行的实心带：背景估计会把它当底色 → 残差 0。

        这种情形**机器判不了**（带子的颜色成了"背景"本身），所以判据的责任不是报块，
        而是把"这些行没有基准"说出来。第一版这里报的是"没有成块残差"——沉默的假绿。
        """
        page = _page(self.root, [], [(0, 100, W, 160)])
        rep = coverage.run(page, None, coverage.MIN_FRAC, coverage.INK_DIFF)
        self.assertGreaterEqual(rep["unjudgeable"]["rows"], 50, rep["unjudgeable"])
        self.assertIn("无基准", rep["verdict_hint"])

    def test_one_side_margin_dirty_falls_back(self):
        """一侧页边被**有纹理**的东西压住时，必须标成退化行。

        注意只能验"有纹理"的情形：一块**纯色**压住页边，与"这里本来就是这种底色"在像素上完全等价，
        任何背景估计都分不出（见 coverage 里写的盲区）。左右分开算 std 解决的正是"只有一侧脏、
        另一侧干净"被混合 std 平均掉的问题——这条用例验的是那个。
        """
        page = _page(self.root, [], [])
        # 在右页边画竖条纹：单侧 std 大，左侧仍是干净渐变
        img = cv2.imdecode(np.fromfile(str(page / "reference.png"), np.uint8), cv2.IMREAD_COLOR)
        for y in range(60, 121):
            for x in range(W - 60, W):
                img[y, x] = (30, 30, 30) if ((x // 3) % 2 == 0) else (250, 250, 250)
        cv2.imencode(".png", img)[1].tofile(str(page / "reference.png"))
        rep = coverage.run(page, None, coverage.MIN_FRAC, coverage.INK_DIFF)
        self.assertGreater(rep["params"]["bg_fallback_rows"], 0)

    def test_small_residual_below_threshold_is_silent(self):
        """门槛 0.0005×400×300 = 60px²；5×5 的噪点 25px² 低于门槛，必须静默。"""
        page = _page(self.root, [], [(100, 100, 105, 105)])   # 5×5 的噪点：25px² < 60px²
        rep = coverage.run(page, None, coverage.MIN_FRAC, coverage.INK_DIFF)
        self.assertEqual(rep["n_blocks"], 0, rep["blocks"])


if __name__ == "__main__":
    unittest.main()
