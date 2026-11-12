#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""覆盖检查只算「成品里真有的对象」（2.57.4 §3a）+ 阶段四态（§3b）。

用户口径（2026-11-10）：
  * 已经生成并插入 PPT 的插画继续计入覆盖；
  * OCR 找到但还没选用的候选、标记跳过的对象、生成失败或没插进 PPT 的对象，
    **不能用自己的框把残差盖掉**；
  * 同一张截图两组对照：插画做出来了不该报漏做；只有候选框、成品里没有插画时，
    原图那一块要留作待核对。

为什么要钉住：spec 的 bbox 是"打算画什么"，不是"画出来了什么"。旧实现把声明框统统
算成已覆盖，于是越没做越绿——一个从没产出的候选素材替自己作证，残差被擦干净。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
PY = sys.executable
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "delivery"))

ILL = [40, 40, 160, 160]        # 插画所在区域（页面像素）
TXT = [250, 200, 380, 260]      # 一块真文字


def _write(p: Path, doc) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def _page(root: Path, asset: dict | None) -> Path:
    page = root / "pg"
    (page / "assets").mkdir(parents=True, exist_ok=True)
    spec = {
        "canvas": {"w_px": 400, "h_px": 300, "dpi": 96.0,
                   "width_in": 400 / 96, "height_in": 300 / 96},
        "background": {"type": "solid", "color": "#FFFFFF"},
        "containers": [],
        "assets": [asset] if asset else [],
        "elements": [{"id": "t1", "type": "text", "text": "AAAA",
                      "bbox": TXT, "size_pt": 16,
                      "color": "#000000", "style": "body", "align": "left"}],
    }
    _write(page / "spec.json", spec)
    _write(page / "override.json", {})
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle(ILL, fill=(60, 90, 160))            # "插画"：一块深色实心
    d.rectangle([TXT[0], TXT[1], TXT[2], TXT[3] - 10], fill=(20, 20, 20))   # 文字墨迹
    img.save(page / "reference.png")
    return page


def _run(*cmd) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *[str(c) for c in cmd]], capture_output=True, text=True, timeout=300)


def _prepare(page: Path, *, build: bool = True) -> None:
    """出素材（如果有）、出 inventory、过 plan、出成品。"""
    sp = page / "spec.json"
    _run(ROOT / "scripts" / "fastcut.py", "--spec", sp,
         "--image", page / "reference.png", "--outdir", page / "assets")
    _run(ROOT / "scripts" / "delivery" / "inventory.py", "--page", page, "--write")
    if not build:
        return
    g = _run(ROOT / "scripts" / "delivery" / "gates.py", "--stage", "plan",
             "--page", page, "--spec", sp)
    assert g.returncode == 0, g.stdout + g.stderr
    b = _run(ROOT / "scripts" / "build.py", "--spec", sp, "--out", page / "out.pptx")
    assert b.returncode == 0, b.stdout + b.stderr


def _coverage(page: Path, *extra) -> dict:
    _run(ROOT / "scripts" / "coverage.py", "--page", page, *extra)
    return json.loads((page / "_check" / "coverage.json").read_text(encoding="utf-8"))


def _touches(rep: dict, rect) -> bool:
    """残差块与 rect 有交集就算"这块地方被报了"。"""
    x0, y0, x1, y1 = rect
    for b in rep.get("blocks") or []:
        bx0, by0, bx1, by1 = b["bbox"]
        if bx1 > x0 and bx0 < x1 and by1 > y0 and by0 < y1:
            return True
    return False


class CoverageUsesProductTest(unittest.TestCase):
    def test_produced_illustration_is_covered(self):
        with tempfile.TemporaryDirectory() as td:
            page = _page(Path(td), {"id": "ill", "mask": "rect", "bbox": ILL})
            _prepare(page)
            rep = _coverage(page)
            self.assertEqual(rep["boxes_basis"]["mode"], "pptx", "有成品时必须按成品算")
            self.assertFalse(_touches(rep, ILL),
                             f"插画已经做进 pptx，不许再报那块是漏做：{rep['blocks']}")
            self.assertEqual(rep["status"], "pass", rep)

    def test_candidate_box_does_not_cover_residual(self):
        """只有候选框、成品里没有插画 → 原图那块必须留作待核对。"""
        with tempfile.TemporaryDirectory() as td:
            page = _page(Path(td), {"id": "ill", "review": True, "bbox": ILL})
            _prepare(page)
            rep = _coverage(page)
            self.assertEqual(rep["boxes_basis"]["mode"], "pptx")
            self.assertIn("ill", [s["id"] for s in rep["boxes_basis"]["skipped"]],
                          "未产出的候选必须被点名（它的框没算进覆盖）")
            self.assertTrue(_touches(rep, ILL),
                            f"候选框不许盖掉残差，那块应留作待确认：{rep['blocks']}")
            self.assertEqual(rep["status"], "unproven",
                             "没白名单时是待确认，不是通过也不阻塞")

    def test_asset_without_pptx_is_not_counted(self):
        """没有成品时退回结构声明，但无 src 的素材照样不算。"""
        with tempfile.TemporaryDirectory() as td:
            page = _page(Path(td), {"id": "ill", "mask": "rect", "bbox": ILL})
            _prepare(page, build=False)      # 出了 PNG 但没有成品
            _run(ROOT / "scripts" / "coverage.py", "--page", page)
            rep = json.loads((page / "_check" / "coverage.json").read_text(encoding="utf-8"))
            self.assertEqual(rep["boxes_basis"]["mode"], "spec-only")
            self.assertFalse(_touches(rep, ILL), "有 src 的素材在无成品时按声明计入")


class CoverageStageFourStatesTest(unittest.TestCase):
    """阶段表四态：pass / fail(to_fix) / unproven(待确认) / error(执行错误)。"""

    def _cycle(self, page: Path) -> tuple[subprocess.CompletedProcess, dict]:
        r = _run(ROOT / "scripts" / "delivery" / "cycle.py", "--page", page,
                 "--mode", "preview", "--only", "coverage")
        return r, json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))

    def _phase(self, doc: dict) -> dict:
        return next(p for p in doc["phases"] if p["name"] == "coverage")

    def test_unproven_is_its_own_state_and_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            page = _page(Path(td), {"id": "ill", "review": True, "bbox": ILL})
            _prepare(page)
            r, doc = self._cycle(page)
            ph = self._phase(doc)
            self.assertEqual(ph["status"], "unproven", r.stdout)
            self.assertIn("待确认", r.stdout, "阶段表要把 unproven 显示成「待确认」")
            self.assertFalse([f for f in doc["to_fix"] if f.get("blocking", True)],
                             "待确认不许阻塞")

    def test_whitelist_with_residual_is_blocking_fail(self):
        with tempfile.TemporaryDirectory() as td:
            page = _page(Path(td), {"id": "ill", "mask": "rect", "bbox": ILL})
            (page / "assets").mkdir(exist_ok=True)
            _prepare(page)
            # 画一根白名单覆盖不到的墨迹：直接在文字区外补一笔
            img = Image.open(page / "reference.png").convert("RGB")
            ImageDraw.Draw(img).rectangle([40, 250, 120, 280], fill=(10, 10, 10))
            img.save(page / "reference.png")
            _write(page / "coverage_whitelist.json", {"rects": []})
            r, doc = self._cycle(page)
            ph = self._phase(doc)
            self.assertEqual(ph["status"], "fail", r.stdout)
            self.assertTrue([f for f in doc["to_fix"] if f.get("blocking", True)])

    def test_crash_is_error_not_pass(self):
        with tempfile.TemporaryDirectory() as td:
            page = _page(Path(td), None)
            _prepare(page)
            (page / "reference.png").write_bytes(b"not-a-png")   # 让覆盖率工具读不到图
            r, doc = self._cycle(page)
            ph = self._phase(doc)
            self.assertEqual(ph["status"], "error", f"工具崩了必须报执行错误：{r.stdout}")
            self.assertTrue([f for f in doc["to_fix"] if f.get("blocking", True)],
                            "执行错误不能悄悄变成通过")


if __name__ == "__main__":
    unittest.main()
