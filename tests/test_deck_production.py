#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正式合并（2.57.4 §2）：只读已验单页、按 deck.json 页序、拒绝过期/分段/被改过的结论。

为什么这些用例存在（2026-11-10 实测缺口）：
  旧 `deck.py --root` 的做法是「按目录名排序 + 逐页重新 build 再合并」。两个后果：
    ① 页序由目录名决定（`P01, P10, P02` → `P01, P02, P10`），错了没有任何提示；
    ② 合并的是"现在重新 build 出来的东西"，不是"验过的那份成品"——交付文件与
       `verdict.json` 说的那一页可以是两份不同的东西，且没人能发现。

本文件钉住的六件事：
  A. 页序 = deck.json.pages（不是目录名）
  B. 渐变背景整段保留（旧实现只认纯色，渐变页合并后变白）
  C. 缺页 / 缺 spec 点名拒绝
  D. 没交付结论（只有 preview / 分段 / 缺 verdict）拒绝
  E. 成品被改过（摘要不一致）拒绝
  F. 输入变了（源图/spec/规范）→ 结论过期，拒绝；且失败不覆盖已有输出文件
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

import report_state as rs      # noqa: E402

DECK = ROOT / "scripts" / "delivery" / "deck.py"
GRAD = {"type": "gradient", "gradient": ["#EEF7FE", "#D2E9FC", "#F2FAFE"],
        "gradient_pos": [0, 0.5, 1], "gradient_angle": 90}


def _write(p: Path, doc) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def make_page(root: Path, name: str, text: str = "AAAA", bg=None) -> Path:
    page = root / name
    (page / "assets").mkdir(parents=True, exist_ok=True)
    spec = {
        "canvas": {"w_px": 320, "h_px": 180, "dpi": 96.0,
                   "width_in": 320 / 96, "height_in": 180 / 96},
        "background": bg or {"type": "solid", "color": "#FFFFFF"},
        "containers": [], "assets": [],
        "elements": [{"id": "t1", "type": "text", "text": text,
                      "bbox": [20, 60, 160, 90], "size_pt": 18,
                      "color": "#000000", "style": "label", "align": "left"}],
    }
    _write(page / "spec.json", spec)
    _write(page / "override.json", {})
    img = Image.new("RGB", (320, 180), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([22, 66, 158, 84], fill=(0, 0, 0))
    img.save(page / "reference.png")
    return page


def _build(page: Path) -> Path:
    sp = page / "spec.json"
    subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "inventory.py"),
                    "--page", str(page), "--write"], capture_output=True, text=True)
    r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "gates.py"), "--stage", "plan",
                        "--page", str(page), "--spec", str(sp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"plan gate 失败：{r.stdout}{r.stderr}")
    out = page / "out.pptx"
    r = subprocess.run([PY, str(ROOT / "scripts" / "build.py"),
                        "--spec", str(sp), "--out", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"build 失败：{r.stdout}{r.stderr}")
    return out


def seed_verified(page: Path, deliverable: str = "ready") -> Path:
    """把一页种成「已通过 delivery 整跑」的样子（报告由真 build 产物支撑）。"""
    out = _build(page)
    _write(page / "_check" / "cycle.json", {
        "page": str(page), "mode": "delivery", "state": "ready",
        "out_md5": rs.file_digest(out)[:16],
        "inputs_md5": rs.page_inputs_digest(page, page / "spec.json"),
        "phases": [], "renders": {}, "to_fix": [], "secs": 0.0,
    })
    _write(page / "_check" / "verdict.json", {
        "schema": "verdict/3",
        "text_ok": True, "text_is_text": True, "in_canvas": True,
        "no_missing": True, "renders": True,
        "source": {}, "deliverable": deliverable, "deliverable_why": [],
    })
    return out


def deck_json(root: Path, pages: list[str]) -> None:
    _write(root / "deck.json", {"visual": "test", "pages": pages, "roles": {}})


def run_deck(root: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(DECK), "--root", str(root), "--out", str(out), *extra],
                          capture_output=True, text=True, timeout=300)


def slide_texts(pptx: Path) -> list[str]:
    from pptx import Presentation
    prs = Presentation(str(pptx))
    out = []
    for sl in prs.slides:
        txt = " ".join(sh.text_frame.text for sh in sl.shapes
                       if sh.has_text_frame and sh.text_frame.text.strip())
        out.append(txt)
    return out


def has_gradient_bg(pptx: Path, idx: int = 0) -> bool:
    from pptx import Presentation
    from pptx.oxml.ns import qn
    prs = Presentation(str(pptx))
    csld = prs.slides[idx]._element.find(qn("p:cSld"))
    bg = csld.find(qn("p:bg")) if csld is not None else None
    if bg is None:
        return False
    return bg.find(".//" + qn("a:gradFill")) is not None


class DeckProductionTest(unittest.TestCase):
    def test_page_order_comes_from_deck_json_not_directory_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 目录名排序会给出 1,2,3；声明顺序故意写成 3,1,2
            # deck.json 必须先落盘：它是「项目规范」的一部分，进输入摘要——
            # 先种结论后加 deck.json = 输入变了，会被正确判成过期。
            deck_json(root, ["p3", "p1", "p2"])
            for n, t in (("p1", "ONE"), ("p2", "TWO"), ("p3", "THREE")):
                seed_verified(make_page(root, n, t))
            out = root / "final.pptx"
            r = run_deck(root, out)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(slide_texts(out), ["THREE", "ONE", "TWO"],
                             "页序必须按 deck.json 的声明，不按目录名")

    def test_gradient_background_survives_merge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            deck_json(root, ["p1"])          # 先落规范，再种结论（见上一条注释）
            seed_verified(make_page(root, "p1", "ONE", bg=GRAD))
            out = root / "final.pptx"
            r = run_deck(root, out)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(has_gradient_bg(out, 0),
                            "渐变底必须在合并件里完整保留（旧实现会变成纯白）")

    def test_missing_page_and_missing_spec_are_named(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seed_verified(make_page(root, "p1", "ONE"))
            (root / "p9").mkdir()                      # 目录在，但没有 spec
            deck_json(root, ["p1", "p9", "p_missing"])
            out = root / "final.pptx"
            r = run_deck(root, out)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("p9", r.stdout + r.stderr)
            self.assertIn("p_missing", r.stdout + r.stderr)
            self.assertFalse(out.exists(), "拒绝时不许产出文件")

    def test_unverified_page_is_refused(self):
        cases = {
            "没有 cycle 报告": lambda p: (p / "_check" / "cycle.json").unlink(),
            "preview 模式": lambda p: _patch_cycle(p, {"mode": "preview"}),
            "分段跑": lambda p: _patch_cycle(p, {"partial": True}),
            "not ready": lambda p: _patch_cycle(p, {"state": "blocked"}),
            "缺 verdict": lambda p: (p / "_check" / "verdict.json").unlink(),
            "deliverable=rework": lambda p: _patch_verdict(p, {"deliverable": "rework"}),
        }
        for label, breaker in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                page = make_page(root, "p1", "ONE")
                seed_verified(page)
                breaker(page)
                deck_json(root, ["p1"])
                r = run_deck(root, root / "final.pptx")
                self.assertNotEqual(r.returncode, 0, f"{label} 不该被放行：{r.stdout}")
                self.assertIn("p1", r.stdout + r.stderr, f"{label} 必须点名页")

    def test_hand_edited_product_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            page = make_page(root, "p1", "ONE")
            out = seed_verified(page)
            out.write_bytes(out.read_bytes() + b"\0")     # 成品被外部改过
            deck_json(root, ["p1"])
            r = run_deck(root, root / "final.pptx")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("成品摘要", r.stdout + r.stderr)

    def test_changed_input_makes_verdict_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            page = make_page(root, "p1", "ONE")
            seed_verified(page)
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["elements"][0]["color"] = "#FF0000"      # 改输入但不重跑
            _write(page / "spec.json", spec)
            deck_json(root, ["p1"])
            r = run_deck(root, root / "final.pptx")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("输入已变化", r.stdout + r.stderr)

    def test_failure_does_not_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            page = make_page(root, "p1", "ONE")
            seed_verified(page)
            deck_json(root, ["p1"])
            out = root / "final.pptx"
            out.write_bytes(b"OLD-DELIVERABLE")
            (page / "_check" / "verdict.json").unlink()   # 让这一轮必然失败
            r = run_deck(root, out)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(out.read_bytes(), b"OLD-DELIVERABLE",
                             "失败的合并不许覆盖已有的正式交付文件")

    def test_dash_draft_still_builds_without_reports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_page(root, "p2", "TWO")
            make_page(root, "p1", "ONE")
            out = root / "draft.pptx"
            r = run_deck(root, out, "--draft")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(slide_texts(out), ["ONE", "TWO"], "草稿入口按目录名排序")


def _patch_cycle(page: Path, patch: dict) -> None:
    p = page / "_check" / "cycle.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc.update(patch)
    _write(p, doc)


def _patch_verdict(page: Path, patch: dict) -> None:
    p = page / "_check" / "verdict.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc.update(patch)
    _write(p, doc)


if __name__ == "__main__":
    unittest.main()
