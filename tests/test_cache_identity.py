#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缓存身份回归（2.57.4 §1.3）：报告有效 ≠ 产物是同一份。

三条互不重叠的缺口，各自钉一条用例：

1. `build` 复用只有"报告对输入有效"这一条时，**盘上那份 pptx 被谁改过都不管**。
   现场：人手动改成品试色 → 下一轮 cycle 判 `build reused` → 终端照旧 ready，
   "构建通过"证明的是一份已经不存在的产物。
   现在多一条硬条件：盘上摘要必须等于 build 报告记的摘要，否则按 spec 重建。

2. 渲染阶段曾经用 `file_digest(out_path)` 回写 build 报告，等于**用渲染成功给一份
   被替换的成品重签构建摘要**。判据抽成 `cycle._refresh_build_evidence`，不一致时返回 None。

3. `editability` 的旧键只有文字指纹：往页面加一张位图，一个文字 run 都没变 →
   报告判 current、结论照旧"不是贴图"。现在位图清单（数量/位置/图内容摘要）进键。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
PY = sys.executable
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "delivery"))
sys.path.insert(0, str(ROOT / "tests"))     # discover 时已在路径里，显式再插一次保证单跑也成立

import report_state as rs          # noqa: E402
import cycle                       # noqa: E402
from test_contracts import make_page, build_pptx, gate   # noqa: E402


def _cycle(page: Path, *extra: str) -> tuple[subprocess.CompletedProcess, dict]:
    r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                        "--page", str(page), *extra],
                       capture_output=True, text=True, timeout=300)
    doc = json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))
    return r, doc


def _phase(doc: dict, name: str) -> dict:
    return next(p for p in doc["phases"] if p["name"] == name)


def _recolor_first_text(pptx: Path, rgb: str) -> None:
    """把成品里第一块文字改成别的颜色——**只动产物，不动 spec**。"""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    prs = Presentation(str(pptx))
    for sh in prs.slides[0].shapes:
        if sh.has_text_frame and sh.text_frame.text.strip():
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    r.font.color.rgb = RGBColor.from_string(rgb.lstrip("#"))
            prs.save(str(pptx))
            return
    raise AssertionError("成品里没有文字块，夹具失效")


def _first_text_color(pptx: Path) -> str:
    """成品里第一块文字的 run 颜色（06 位大写 hex）。"""
    from pptx import Presentation
    prs = Presentation(str(pptx))
    for sh in prs.slides[0].shapes:
        if sh.has_text_frame and sh.text_frame.text.strip():
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    c = r.font.color
                    if c and c.type is not None and c.rgb is not None:
                        return str(c.rgb).upper()
    return ""


class BuildReuseIdentityTest(unittest.TestCase):
    def test_hand_edited_product_is_rebuilt_not_reused(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            out = build_pptx(page)

            # 第一轮：正常构建出报告
            r1, doc1 = _cycle(page, "--mode", "preview", "--only", "plan,build")
            self.assertEqual(_phase(doc1, "build")["status"], "executed", r1.stdout + r1.stderr)
            good_md5 = rs.file_digest(out)
            self.assertTrue(good_md5)

            # 人手动改成品（只改颜色），spec 一个字没动
            _recolor_first_text(out, "#FF0000")
            edited_md5 = rs.file_digest(out)
            self.assertNotEqual(edited_md5, good_md5, "夹具没真的改动产物")

            # 第二轮：必须**拒绝**沿用旧结论，按 spec 重建
            r2, doc2 = _cycle(page, "--mode", "preview", "--only", "plan,build")
            self.assertEqual(_phase(doc2, "build")["status"], "executed",
                             f"被外部改过的成品不该 reused：{r2.stdout}")
            self.assertIn("被外部改过", r2.stdout, r2.stdout)
            # 断言落在**语义**上，不比字节：pptx 里带 docProps 时间戳，两次构建的字节
            # 本来就可能不同（实测偶发），拿 md5 相等当"重建成功"会变成一个假失败。
            self.assertEqual(_first_text_color(out), "000000",
                             "重建后必须回到 spec 所写的样子（手改的红字被覆盖）")

    def test_clean_product_is_still_reused(self):
        """反向对照：没被改过的成品必须 reused——不能把缓存改废（每轮重构建）。"""
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            out = build_pptx(page)
            _cycle(page, "--mode", "preview", "--only", "plan,build")
            r2, doc2 = _cycle(page, "--mode", "preview", "--only", "plan,build")
            self.assertEqual(_phase(doc2, "build")["status"], "reused", r2.stdout)
            self.assertTrue(rs.file_digest(out))


class RenderStageResignTest(unittest.TestCase):
    """渲染只补证据，不给被替换的成品重签构建摘要。"""

    def test_mismatched_product_gets_no_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.pptx"
            out.write_bytes(b"PK\x03\x04current")
            png = Path(td) / "rendered.png"
            png.write_bytes(b"\x89PNG")
            bdoc = {"pptx_md5": "0" * 32}
            self.assertIsNone(cycle._refresh_build_evidence(bdoc, out, png, 320, 180),
                              "摘要不一致时必须拒绝回写（否则渲染替构建作证）")

    def test_matched_product_keeps_recorded_digest(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.pptx"
            out.write_bytes(b"PK\x03\x04current")
            png = Path(td) / "rendered.png"
            png.write_bytes(b"\x89PNG")
            rec = rs.file_digest(out)
            ev = cycle._refresh_build_evidence({"pptx_md5": rec}, out, png, 320, 180)
            self.assertIsNotNone(ev)
            self.assertEqual(ev["pptx_md5"], rec, "构建摘要必须原样沿用，不能重算")
            self.assertEqual(ev["png_md5"], rs.file_digest(png))


class BuilderIdentityInKeyTest(unittest.TestCase):
    def test_builder_fingerprint_feeds_build_key(self):
        """构建器身份必须进 key_build：否则改构建器后 spec 未动 → 旧报告继续 reused。"""
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            out = build_pptx(page)
            k_before = rs.key_build(sp, out, 320, 180)
            saved = rs.CHECKER_SRC["build"]
            try:
                rs.CHECKER_SRC["build"] = ("build.py",)          # 换一组"构建器指纹"
                k_after = rs.key_build(sp, out, 320, 180)
            finally:
                rs.CHECKER_SRC["build"] = saved
            self.assertNotEqual(k_before, k_after, "key_build 没有把构建器身份算进去")


class EditabilityPictureKeyTest(unittest.TestCase):
    def test_added_picture_invalidates_editability_key(self):
        from PIL import Image
        from pptx import Presentation
        from pptx.util import Emu
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            out = build_pptx(page)
            k1 = rs.key_editability(sp, out, 320, 180)
            text1 = rs.text_fingerprint(out)

            blk = Path(td) / "blk.png"
            Image.new("RGB", (200, 120), (30, 30, 30)).save(blk)
            prs = Presentation(str(out))
            prs.slides[0].shapes.add_picture(str(blk), Emu(0), Emu(0),
                                             width=Emu(2000000), height=Emu(1200000))
            prs.save(str(out))

            self.assertEqual(rs.text_fingerprint(out)["n_text"], text1["n_text"],
                             "夹具前提：加图不改文字指纹")
            self.assertNotEqual(rs.key_editability(sp, out, 320, 180), k1,
                                "加了整页位图，editability 的复用键必须失效")


if __name__ == "__main__":
    unittest.main()
