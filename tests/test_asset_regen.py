#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""素材生成键（2.57.4 §1.2）：换图必须重抠，只改文字必须复用，rect 无 src 必须能出图。

三条缺口（用户 2026-11-10 拍板的口径）：

A. 生成键里曾有 `reference_md5`，11-09 被删成"只看素材自身参数"。后果：同一页换了截图
   （同一裁框、同一 mask，图标从红变蓝）→ 键不变 → 旧 PNG 被当成"由当前截图生成"，
   产物还是上一张图的颜色。现在把参考图内容摘要加回生成键。
   **素材质量检查**（assets_payload）仍不含参考图——资产闸只判素材自身。

B. `mask=="rect" 且无 src` 在 cycle 里被 `continue` 跳过 → 永远不生成 PNG → build 报 MISS。
   fastcut 本来就支持 rect，是调度层漏了它。

C. 只改文字颜色时 PNG 必须复用（守住"改一个颜色不重抠全部素材"这条既有合同）。
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

import report_state as rs     # noqa: E402

ICON = [40, 40, 140, 140]     # mask=chroma 的裁框（页面像素）
BLOCK = [200, 40, 300, 140]   # mask=rect 的裁框


def _write(p: Path, doc) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def _make_page(root: Path, icon_rgb=(200, 30, 30)) -> Path:
    """400×300 页：一个 chroma 图标 + 一个 rect 整块（无 src）+ 一块文字。"""
    page = root / "page"
    (page / "assets").mkdir(parents=True, exist_ok=True)
    spec = {
        "canvas": {"w_px": 400, "h_px": 300, "dpi": 96.0,
                   "width_in": 400 / 96, "height_in": 300 / 96},
        "background": {"type": "solid", "color": "#FFFFFF"},
        "containers": [],
        "assets": [
            {"id": "ic1", "mask": "chroma", "bbox": ICON},
            {"id": "blk", "mask": "rect", "bbox": BLOCK},
        ],
        "elements": [{"id": "t1", "type": "text", "text": "AAAA",
                      "bbox": [20, 200, 200, 230], "size_pt": 18,
                      "color": "#000000", "style": "label", "align": "left"}],
    }
    _write(page / "spec.json", spec)
    _write(page / "override.json", {})
    _paint(page / "reference.png", icon_rgb)
    return page


def _paint(path: Path, icon_rgb, block_rgb=(40, 150, 90)) -> None:
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.ellipse(ICON, fill=icon_rgb)              # 图标
    d.rectangle(BLOCK, fill=block_rgb)          # 整块留图的区域
    img.save(path)


def _run(page: Path) -> tuple[subprocess.CompletedProcess, dict]:
    r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                        "--page", str(page), "--mode", "preview",
                        "--only", "assets:provenance,assets:regenerate"],
                       capture_output=True, text=True, timeout=300)
    doc = json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))
    return r, doc


def _phase(doc: dict, name: str) -> dict:
    return next(p for p in doc["phases"] if p["name"] == name)


def _center_rgb(png: Path) -> tuple[int, int, int]:
    im = Image.open(png).convert("RGBA")
    w, h = im.size
    return im.getpixel((w // 2, h // 2))[:3]


class AssetRegenTest(unittest.TestCase):
    def test_icon_color_change_recuts_and_rect_without_src_generates(self):
        with tempfile.TemporaryDirectory() as td:
            page = _make_page(Path(td), icon_rgb=(200, 30, 30))
            r1, doc1 = _run(page)
            self.assertEqual(_phase(doc1, "assets:regenerate")["status"], "executed",
                             r1.stdout + r1.stderr)

            ic_png = page / "assets" / "ic1.png"
            blk_png = page / "assets" / "blk.png"
            # B：rect 只有裁框、没有 src —— 以前被 continue 跳过，这里必须有图
            self.assertTrue(blk_png.exists(), f"rect 无 src 必须生成 PNG：{r1.stdout}")
            self.assertTrue(ic_png.exists(), r1.stdout)

            red_center = _center_rgb(ic_png)
            self.assertGreater(red_center[0], 150, f"第一轮图标应该是红的：{red_center}")
            ic_md5_v1 = rs.file_digest(ic_png)
            blk_md5_v1 = rs.file_digest(blk_png)

            # 换参考图：图标变蓝、整块换色；裁框与 mask 一字未改
            _paint(page / "reference.png", (30, 80, 200), block_rgb=(200, 120, 30))

            r2, doc2 = _run(page)
            self.assertEqual(_phase(doc2, "assets:regenerate")["status"], "executed", r2.stdout)
            blue_center = _center_rgb(ic_png)
            self.assertGreater(blue_center[2], 150,
                               f"换图后必须重抠出蓝色图标，实际中心像素 {blue_center}")
            self.assertLess(blue_center[0], 120, f"蓝色图标的红通道应偏低：{blue_center}")
            self.assertNotEqual(rs.file_digest(ic_png), ic_md5_v1, "PNG 摘要必须变化")
            self.assertNotEqual(rs.file_digest(blk_png), blk_md5_v1,
                                "rect 裁片同样来自参考图，换图必须重出")

    def test_text_color_only_reuses_png(self):
        with tempfile.TemporaryDirectory() as td:
            page = _make_page(Path(td))
            _run(page)
            ic_png = page / "assets" / "ic1.png"
            before = rs.file_digest(ic_png)

            sp = page / "spec.json"
            spec = json.loads(sp.read_text(encoding="utf-8"))
            spec["elements"][0]["color"] = "#123456"     # 只改文字颜色
            _write(sp, spec)

            r2, doc2 = _run(page)
            self.assertEqual(_phase(doc2, "assets:regenerate")["status"], "reused",
                             f"只改文字颜色不该重抠：{r2.stdout}")
            self.assertEqual(rs.file_digest(ic_png), before, "PNG 必须原样复用")

    def test_adopted_asset_is_not_overwritten_by_reference_change(self):
        """人工采纳的素材：换参考图后**不自动覆盖**，转为待核对。"""
        with tempfile.TemporaryDirectory() as td:
            page = _make_page(Path(td))
            _run(page)
            ic_png = page / "assets" / "ic1.png"
            ov = json.loads((page / "override.json").read_text(encoding="utf-8"))
            meta = (ov.get("_meta") or {}).get("asset_sources") or {}
            meta["ic1"] = dict(meta["ic1"], adopted=True)     # 标记为人工采纳
            ov.setdefault("_meta", {})["asset_sources"] = meta
            _write(page / "override.json", ov)
            adopted_md5 = rs.file_digest(ic_png)

            _paint(page / "reference.png", (30, 80, 200))     # 参考图变了
            r2, doc2 = _run(page)
            self.assertEqual(rs.file_digest(ic_png), adopted_md5,
                             "人工采纳的素材不许被自动重抠覆盖")
            self.assertIn("ic1", r2.stdout + json.dumps(doc2, ensure_ascii=False),
                          "必须点名转待核对的是哪一块")


if __name__ == "__main__":
    unittest.main()
