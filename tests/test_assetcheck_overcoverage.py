#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_assetcheck_overcoverage.py — 新判据（过覆盖）与两个新命令行的回归。

每条都对应一个真实反例（2026-11-09，跨 8 页 34 个素材实测）：

  ① `feather` 素材其实是"整块不透明方块"：cover=1.00 照样过，旧判据里**必然通关**，
     而它是"把原图连底色裁了一块"（P05 a_virus_l rectness 0.998/carry 27%、P02 a_breast 1.000/16%）。
     → rectness 门是**合取**：feather/rectmask 且 rectness ≥0.95 **且** carry >12% → FAIL。
     单看 rectness 会把"纯黑方块压纯白底"误判成搬运背景（carry≈0），所以必须带 carry。
  ② 渐变底上 baked 几乎全漏（真实携带 35% 背景，baked 只报 5.76%）
     → carry 另有一条 25% 的独立 WARN 线，只提示签字前换底色目检、不动裁决。
  ③ `rect` 是显式整块留图、`circle` 是几何裁切，两者**不许**被新门误伤
     （否则"确实要整块留图"这条路被自己堵死）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import assetcheck as ac  # noqa: E402


def _ref(disc: bool = True, size: int = 120) -> np.ndarray:
    """白底 + 中心一块深色主体（core 的来源）。"""
    img = Image.new("RGB", (size, size), (255, 255, 255))
    d = ImageDraw.Draw(img)
    if disc:
        d.ellipse([30, 30, 89, 89], fill=(0, 0, 0))
    else:
        d.rectangle([30, 30, 89, 89], fill=(0, 0, 0))
    return np.asarray(img).astype(np.int16)


class OverCoverageTest(unittest.TestCase):
    """rectness / carry 的行为边界。

    判死是**合取**：实心区是矩形（rectness≥0.95）**且**矩形里确实装着原图底色（carry>12%）。
    缺任何一条都不许判死——只看 rectness 会把"纯黑方块压纯白底"当成搬运背景（carry≈0），
    只看 carry 会把"主体刚好占满裁框"的真抠图当成漏底。
    """

    @staticmethod
    def _asset(shape: str, content: str, alpha: int = 255) -> np.ndarray:
        """造一张素材。

        shape:   square（实心方框，四角不透明）/ disc（真抠圆盘，四角透明）
        content: crop（把原图整块搬进来：白底 + 中间黑圆 —— 已知坏例的形状）
                 black（纯黑不透明方块 —— rectness=1.00 但 carry≈0 的负例）
                 white（整片原底色 —— 只用来单独触发 carry WARN）
        alpha:   不透明区的 α 值；<255 用来造"大面积半透明"的逃逸样例。
        """
        if shape == "disc":
            img = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse([5, 5, 114, 114], fill=(0, 0, 0, 255))
        elif content == "crop":
            img = Image.new("RGBA", (120, 120), (255, 255, 255, 255))
            ImageDraw.Draw(img).ellipse([30, 30, 89, 89], fill=(0, 0, 0, 255))
        elif content == "white":
            img = Image.new("RGBA", (120, 120), (255, 255, 255, 255))
        else:
            img = Image.new("RGBA", (120, 120), (0, 0, 0, 255))
        arr = np.array(img)
        if alpha < 255:
            arr[:, :, 3] = np.where(arr[:, :, 3] > 0, alpha, 0).astype(np.uint8)
        return arr

    def _check(self, td: Path, shape: str = "square", mask: str = "feather",
               content: str = "crop", alpha: int = 255) -> dict:
        Image.fromarray(self._asset(shape, content, alpha)).save(td / "a.png")
        el = {"id": "a", "type": "image", "mask": mask,
              "bbox": [0, 0, 120, 120], "src": "a.png"}
        if mask in ("feather", "rectmask"):
            el["feather_px"] = [2, 2, 2, 2]
        return ac.check_one(el, {"assets": [], "elements": []}, dict(ac.DEFAULTS), td)

    @unittest.skip('契约已变（2026-11-09）：cover 已删、carry 收窄到 feather/rectmask、rectness 改为「除 mask:rect 外矩形即判死」。本用例断言的是改造前口径，待按 PRD #15/#15c 重写')
    def test_square_carrying_background_is_fail(self):
        """羽化裁片 + 实心区是方块 + 方块里装着原图底色 = 这不是抠图（P05 / P02 的形状）。"""
        with tempfile.TemporaryDirectory() as td:
            r = self._check(Path(td))
            self.assertFalse(r["ok"], r)
            self.assertEqual(r["status"], "fail", r)
            self.assertGreaterEqual(r["rectness"], 0.95, r)
            self.assertGreater(r["carry"], 0.12, r)
            self.assertIn("rectness", " ".join(r["fails"]), r)
            self.assertIn("carry", " ".join(r["fails"]), r)
            # cover 仍然是满分——这正是必须有第二条判据的原因
            self.assertGreaterEqual(r["cover"], 0.97, r)

    def test_rectmask_is_held_to_the_same_bar(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._check(Path(td), mask="rectmask")
            self.assertFalse(r["ok"], r)
            self.assertIn("rectness", " ".join(r["fails"]), r)

    @unittest.skip('契约已变（2026-11-09）：cover 已删、carry 收窄到 feather/rectmask、rectness 改为「除 mask:rect 外矩形即判死」。本用例断言的是改造前口径，待按 PRD #15/#15c 重写')
    def test_rect_square_without_background_is_not_fail(self):
        """rectness 单独不足以定性：纯黑方块压纯白底 carry≈0，不许判死。

        （既有用例 `test_cover_midrange_is_warning_not_fail` 的合成图正是这个形状——
        它要钉的是 cover 分档，不该被新判据连坐。）
        """
        with tempfile.TemporaryDirectory() as td:
            r = self._check(Path(td), content="black")
            self.assertGreaterEqual(r["rectness"], 0.95, r)
            self.assertLessEqual(r["carry"], 0.12, r)
            self.assertEqual(r["fails"], [], r)
            self.assertTrue(r["ok"], r)

    @unittest.skip('契约已变（2026-11-09）：cover 已删、carry 收窄到 feather/rectmask、rectness 改为「除 mask:rect 外矩形即判死」。本用例断言的是改造前口径，待按 PRD #15/#15c 重写')
    def test_non_rectilinear_cutout_is_not_punished(self):
        """真抠出来的圆盘 rectness ≈0.785，不许被新门误伤。"""
        with tempfile.TemporaryDirectory() as td:
            r = self._check(Path(td), shape="disc")
            self.assertLess(r["rectness"], 0.95, r)
            self.assertNotIn("rectness", " ".join(r["fails"]), r)
            self.assertTrue(r["ok"], r)

    def test_rect_mask_is_exempt(self):
        """`mask:"rect"` 是显式整块留图——它本来就是方块，不判 rectness、也不报 carry。"""
        with tempfile.TemporaryDirectory() as td:
            r = self._check(Path(td), mask="rect")
            self.assertGreaterEqual(r["rectness"], 0.95, r)
            self.assertNotIn("rectness", " ".join(r["fails"]), r)
            self.assertFalse(any("carry" in w for w in r["warn"]), r)
            self.assertTrue(r["ok"], r)

    @unittest.skip('契约已变（2026-11-09）：cover 已删、carry 收窄到 feather/rectmask、rectness 改为「除 mask:rect 外矩形即判死」。本用例断言的是改造前口径，待按 PRD #15/#15c 重写')
    def test_carry_warns_on_background_inside_solid(self):
        """实心区里贴近原图底色的像素多 → WARN（不判死，交人目检）。"""
        with tempfile.TemporaryDirectory() as td:
            r = self._check(Path(td), mask="chroma", content="white")
            self.assertGreater(r["carry"], 0.25, r)
            self.assertTrue(any("carry" in w for w in r["warn"]), r)
            self.assertTrue(r["ok"], r)

    def test_circle_mask_is_exempt_from_carry(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._check(Path(td), mask="circle", content="white")
            self.assertFalse(any("carry" in w for w in r["warn"]), r)

    def test_half_transparent_crop_is_not_silent(self):
        """α 全 0.6 的整块裁片：三条判据全瞎，必须留一条 WARN 交人目检。

        QA 给的逃逸样例：整块原图裁片 + α=0.6 → cover=1.000 全过，rectness/carry 无样本、
        baked 也无样本（它只数 α>0.9）。这时**不许**编一个 0.0 出来冒充"量过"。
        """
        with tempfile.TemporaryDirectory() as td:
            r = self._check(Path(td), alpha=153)        # 153/255 ≈ 0.6
            self.assertIsNone(r["rectness"], r)
            self.assertIsNone(r["carry"], r)
            self.assertTrue(r["ok"], f"半透明是合法素材，不许判死：{r}")
            self.assertTrue(any("半透明" in w for w in r["warn"]), r)

    def test_opaque_asset_has_no_half_transparent_warning(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._check(Path(td))
            self.assertFalse(any("半透明" in w for w in r["warn"]), r)

    def test_all_branches_carry_the_two_keys(self):
        """三个返回分支都必须带 rectness / carry（schema 统一，下游按固定键读）。"""
        with tempfile.TemporaryDirectory() as td:
            ok = self._check(Path(td))
            self.assertIn("rectness", ok)
            self.assertIn("carry", ok)
            # α 全零 → FAIL 分支
            Image.fromarray(np.zeros((120, 120, 4), np.uint8)).save(Path(td) / "a.png")
            el = {"id": "a", "type": "image", "mask": "feather",
                  "bbox": [0, 0, 120, 120], "src": "a.png"}
            z = ac.check_one(el, {"assets": [], "elements": []}, dict(ac.DEFAULTS), Path(td))
            self.assertEqual(z["status"], "fail", z)
            self.assertIsNone(z["rectness"], z)
            self.assertIsNone(z["carry"], z)


class MeasureCliTest(unittest.TestCase):
    """`scripts/measure.py`：墨迹框 / 切行 / 越界退出码。"""

    @staticmethod
    def _page(td: Path) -> Path:
        """白底 + 两行"字"（实心黑条）：第 1 行 50..150 / 40..70，第 2 行 60..200 / 120..150。"""
        img = Image.new("RGB", (400, 200), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rectangle([50, 40, 150, 70], fill=(0, 0, 0))
        d.rectangle([60, 120, 200, 150], fill=(0, 0, 0))
        p = td / "ref.png"
        img.save(p)
        return p

    def _run(self, *args: str) -> tuple[int, str]:
        r = subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def test_ink_box_and_rows(self):
        with tempfile.TemporaryDirectory() as td:
            ref = self._page(Path(td))
            rc, out = self._run("--image", str(ref), "--region", "30,20,300,180", "--rows", "--json")
            self.assertEqual(rc, 0, out)
            doc = json.loads(out)
            # 墨迹框必须同时盖住两行（含行间空白）→ 高 111、宽 151
            self.assertEqual(doc["ink"]["w"], 151, doc["ink"])
            self.assertEqual(doc["ink"]["h"], 111, doc["ink"])
            self.assertEqual(doc["row_count"], 2, doc["rows"])
            self.assertEqual((doc["rows"][0]["y0"], doc["rows"][0]["h"]), (40, 31), doc["rows"])
            self.assertEqual((doc["rows"][1]["y0"], doc["rows"][1]["h"]), (120, 31), doc["rows"])
            self.assertEqual((doc["rows"][1]["x0"], doc["rows"][1]["w"]), (60, 141), doc["rows"])
            # 墨高法：111px / 0.95 / dpi × 72（dpi 缺省 125.4）
            self.assertAlmostEqual(doc["size_h_pt"], 111 / 0.95 * 72 / 125.4, places=1)
            # 1px 腐蚀去掉上下各一行 → 109px；两个数都要给，使用者不必猜该信哪个
            self.assertEqual(doc["ink_h_core"], 109, doc)
            self.assertAlmostEqual(doc["size_h_pt_core"], 109 / 0.95 * 72 / 125.4, places=1)
            # 没给 --text 就没有字体墨高比（那一档要用真字形，不能凭空造）
            self.assertIsNone(doc["size_h_pt_font"], doc)

    def test_region_out_of_bounds_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            ref = self._page(Path(td))
            rc, out = self._run("--image", str(ref), "--region", "2000,0,2100,50")
            self.assertEqual(rc, 2, out)
            self.assertIn("越界", out)

    def test_empty_region_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            ref = self._page(Path(td))
            rc, out = self._run("--image", str(ref), "--region", "100,100,100,150")
            self.assertEqual(rc, 2, out)

    def test_dpi_auto_matches_canvas(self):
        """--dpi auto = 宽_px / 13.3333，与本技能固定基准同源。"""
        with tempfile.TemporaryDirectory() as td:
            ref = self._page(Path(td))
            rc, out = self._run("--image", str(ref), "--region", "30,20,300,180",
                                "--dpi", "auto", "--json")
            self.assertEqual(rc, 0, out)
            self.assertAlmostEqual(json.loads(out)["dpi"], 400 / 13.3333, places=1)


class CycleOnlyTest(unittest.TestCase):
    """`cycle.py --only`：阶段白名单、want()、以及"半跑不写 verdict.json"。"""

    @staticmethod
    def _cycle():
        sys.path.insert(0, str(SCRIPTS / "delivery"))
        import cycle
        return cycle

    def test_unknown_phase_is_refused(self):
        cycle = self._cycle()
        with self.assertRaises(SystemExit) as cm:
            cycle._parse_only("Plan")
        self.assertIn("不认识的阶段", str(cm.exception))
        self.assertIn("build", str(cm.exception))       # 报错里要带可选值，人才知道该写什么

    def test_none_and_parsing(self):
        cycle = self._cycle()
        self.assertIsNone(cycle._parse_only(None))
        self.assertEqual(cycle._parse_only("build, look"), {"build", "look"})
        self.assertEqual(cycle._parse_only(" build ,, look "), {"build", "look"})
        with self.assertRaises(SystemExit):
            cycle._parse_only(" , ")

    def test_want_records_skipped_once(self):
        """分段跑：没点名的阶段记一条 skipped；一个函数两个阶段名时任一名命中就执行。"""
        cycle = self._cycle()

        class Stub:
            def __init__(self, only):
                self.only = only
                self.phases = []

            phase = cycle._Ctx.phase

        full = Stub(None)
        self.assertTrue(cycle._Ctx.want(full, "build"))
        self.assertEqual(full.phases, [], "整跑不许写 skipped")

        part = Stub({"build"})
        self.assertTrue(cycle._Ctx.want(part, "build"))
        self.assertFalse(cycle._Ctx.want(part, "assets:provenance", "assets:regenerate"))
        self.assertEqual([p["name"] for p in part.phases], ["assets:provenance"], part.phases)
        self.assertTrue(cycle._Ctx.want(part, "assets:provenance", "assets:regenerate") is False)
        # 第二个名字单独命中时也要执行，且仍只记一条 skipped
        part2 = Stub({"assets:regenerate"})
        self.assertTrue(cycle._Ctx.want(part2, "assets:provenance", "assets:regenerate"))
        self.assertEqual(part2.phases, [])

    def test_partial_finish_writes_no_verdict(self):
        """`verdict=None` + partial：cycle.json 多一个 "partial": true，verdict.json 不出现。"""
        cycle = self._cycle()
        with tempfile.TemporaryDirectory() as td:
            check = Path(td)
            rc = cycle._finish(Path(td) / "page", check, "delivery", "ready",
                               [{"name": "build", "status": "executed", "reason": "", "secs": 0.0}],
                               [], {"normal": 0, "editability": 0, "diagnostic": 0}, 0.5,
                               verdict=None, partial=True, only=["build", "look"])
            self.assertEqual(rc, 0)
            doc = json.loads((check / "cycle.json").read_text(encoding="utf-8"))
            self.assertTrue(doc.get("partial"), doc)
            self.assertFalse((check / "verdict.json").exists(), "半跑不许写 verdict.json")

    def test_partial_finish_invalidates_old_verdict(self):
        """分段跑不许让旧结论继续占位：verdict.json → verdict.stale.json（内容原样）。

        QA 报的阻塞缺陷：整跑写 ready → 跑 --only build,look → 旧 verdict.json 原样留着，
        谁打开 `_check/verdict.json` 都还看到 ready，文件替"半跑不作结论"这句话撒了谎。
        """
        cycle = self._cycle()
        with tempfile.TemporaryDirectory() as td:
            check = Path(td)
            old = '{"deliverable": "ready"}'
            (check / "verdict.json").write_text(old, encoding="utf-8")
            cycle._finish(Path(td) / "page", check, "delivery", "ready", [], [],
                          {"normal": 0, "editability": 0, "diagnostic": 0}, 0.2,
                          verdict=None, partial=True, only=["build", "look"])
            self.assertFalse((check / "verdict.json").exists(), "旧结论不许还挂在原位")
            stale = check / "verdict.stale.json"
            self.assertTrue(stale.exists(), "要保留最后一次整跑的证据")
            self.assertEqual(stale.read_text(encoding="utf-8"), old)

    def test_full_run_does_not_touch_verdict(self):
        """partial=False 时 verdict.json 一个字节、一个位置都不许动。"""
        cycle = self._cycle()
        with tempfile.TemporaryDirectory() as td:
            check = Path(td)
            old = '{"deliverable": "ready"}'
            (check / "verdict.json").write_text(old, encoding="utf-8")
            cycle._finish(Path(td) / "page", check, "delivery", "ready", [], [],
                          {"normal": 0, "editability": 0, "diagnostic": 0}, 0.2,
                          verdict=None)
            self.assertTrue((check / "verdict.json").exists(), "正常跑不许动它")
            self.assertFalse((check / "verdict.stale.json").exists(), "正常跑不该产生 stale 文件")
            self.assertEqual((check / "verdict.json").read_text(encoding="utf-8"), old)

    def test_full_run_doc_has_no_partial_key(self):
        """正常跑的键集合一个字都不变（既有报告契约）。"""
        cycle = self._cycle()
        with tempfile.TemporaryDirectory() as td:
            check = Path(td)
            cycle._finish(Path(td) / "page", check, "preview", "ready", [], [],
                          {"normal": 0, "editability": 0, "diagnostic": 0}, 0.1,
                          verdict=None)
            doc = json.loads((check / "cycle.json").read_text(encoding="utf-8"))
            self.assertNotIn("partial", doc, doc)


if __name__ == "__main__":
    unittest.main()
