#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_contracts.py — 针对本轮修的实际失败与缓存边界的聚焦回归。

运行（候选根目录）：
  python -m unittest discover -s tests -v
默认不需要 PowerPoint；设置 PPT_TEST_RENDER=1 才跑真实渲染的冒烟用例。

每个用例对应一条真实反例，不是"覆盖率装饰"：
  - 旧 quality 报告不许阻塞修后的 plan/build
  - 损坏报告必须报 invalid/error，不许当空失败集合
  - 只改文字颜色不让素材检查失效；只改 JSON 缩进谁都不失效；src 内容变必须失效
  - 旧通过报告不能验收新成品
  - 容器/形状的几何与版式意图不核验，但"漏块"必须拦（presence）

P3（2.38.x）删死代码时一并移除了 4 条"测已归档脚本"的用例（白字压深底量成浅色、深字浅底不被
带坏、字段级锁定的导入优先级、参考图不匹配拒绝导入）：对应脚本已归档到 `_archive/P3-20261108/`，
SKILL.md §1 的六步主链不再含测量/导入环节。判据本身没有放宽，只是没有实现可测——详见各删除点
旁的说明段。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import inspect
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
PY = sys.executable
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "delivery"))

import report_state as rs          # noqa: E402
import inventory as inv_mod        # noqa: E402
import specmerge                   # noqa: E402
import gates                       # noqa: E402
import cycle                       # noqa: E402
import spec_lint                   # noqa: E402
import numpy as np                 # noqa: E402
from PIL import Image, ImageDraw   # noqa: E402


# 第 ④ 步（residual 模块退役）后失去被测对象的用例统一理由。
# 逐条判定后单独打 skip，不按类整片 skip：这类用例断的是 residual 的运行时符号
# （DEFAULTS / run），不是文件路径字符串，随模块删除即无被测对象。
_SKIP_RESIDUAL = "residual 已退役（第 ④ 步）：本用例测的是 residual 内部判据/依赖键，随模块删除"

# 第 ④ 步 4A（io_gate / residual 退役）后失去被测对象的用例统一理由。
# 同样是逐条判定后单独打 skip，不做类级整片 skip：整类 IoPolicyEndToEndTest 的每条
# 用例都在测 io_policy→io_gate→cycle 的某一段，删了 io 就没有被测对象。
_SKIP_IO_RESIDUAL = "io_gate/residual 已退役（第 ④ 步 4A）：本用例测的是 io 阈值或残差分流，随模块删除"

# 第 ④ 步 4B（verify / autocorrect 退役）后失去被测对象的用例统一理由。
_SKIP_VERIFY_AUTOCORRECT = "verify/autocorrect 已退役（第 ④ 步 4B）：本用例测的是逐块位置验证或字号自动改写，随模块删除"


# ---------------------------------------------------------------- 夹具
def write_json(p: Path, doc) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def make_page(root: Path, color: str = "#000000") -> Path:
    """最小可构建页：320x180、一个文字元素、无素材。"""
    page = root / "page"
    (page / "assets").mkdir(parents=True, exist_ok=True)
    (page / "look").mkdir(exist_ok=True)
    spec = {
        "canvas": {"w_px": 320, "h_px": 180, "dpi": 96.0,
                   "width_in": 3.3333, "height_in": 1.875},
        "background": {"type": "solid", "color": "#FFFFFF"},
        "containers": [],
        "assets": [],
        "elements": [{"id": "t1", "type": "text", "text": "AAAA",
                      "bbox": [20, 60, 120, 90], "size_pt": 18,
                      "color": color, "style": "label", "align": "left"}],
    }
    write_json(page / "spec.json", spec)
    write_json(page / "override.json", {})
    inv = {"page": page.name, "spec": "spec.json", "canvas": spec["canvas"],
           "items": [{"id": "t1", "section": "elements", "kind": "native",
                      "reason": "native-simple", "decided_by": "auto",
                      "evidence": "text(len=4) style=label size_pt=18",
                      "approx_of": None, "gaps": [], "crop_note": None,
                      "locked": False, "locked_reason": None}]}
    write_json(page / "inventory.json", inv)
    ref = Image.new("RGB", (320, 180), (255, 255, 255))
    d = ImageDraw.Draw(ref)
    d.rectangle([22, 66, 118, 84], fill=(0, 0, 0))
    ref.save(page / "reference.png")
    # 与参考完全相同的"渲染图"：只用于验证闸的依赖逻辑，不声明视觉真实
    shutil.copyfile(page / "reference.png", page / "look" / "rendered.png")
    # 2.42.0：不再写 tolerance.json。它是 verify 的环境标定输入，verify 退役后
    # 全库没有任何代码读它（`grep -rn tolerance scripts/` = 0），写了只会让人以为
    # "缺标定会影响交付"——那条规则已随 s5a 一起删除。
    return page


def build_pptx(page: Path, out_name: str = "out.pptx") -> Path:
    out = page / out_name
    g = gate(page, "plan")
    if g.returncode != 0:
        raise AssertionError(f"plan gate failed: {g.stdout}{g.stderr}")
    r = subprocess.run([PY, str(ROOT / "scripts" / "build.py"),
                        "--spec", str(page / "spec.json"), "--out", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"build failed: {r.stdout}{r.stderr}")
    return out


def gate(page: Path, stage: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "gates.py"),
                           "--stage", stage, "--page", str(page), *extra],
                          capture_output=True, text=True)


def _strip_east_asian_fonts(pptx: Path) -> int:
    """删掉 pptx 里所有 `<a:ea .../>`，造一个"中文没有东亚字体"的坏成品。

    用途：制造一个 **gates 看得见、cycle 自己看不见**的错误——只有 gates 的
    OOXML 检查会报 `<a:ea` 缺失，cycle 的阶段清单里没有这一项。据此证明
    "gates 报错 → 交付 blocked(1)" 这条链没断。
    """
    import zipfile
    tmp = pptx.with_suffix(".tmpstripped.pptx")
    removed = 0
    with zipfile.ZipFile(pptx) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("ppt/slides/slide") and item.filename.endswith(".xml"):
                text = data.decode("utf-8")
                new, n = re.subn(r"<a:ea[^>]*/>", "", text)
                removed += n
                data = new.encode("utf-8")
            dst.writestr(item, data)
    tmp.replace(pptx)
    return removed


class ReportStateTest(unittest.TestCase):
    def test_format_only_change_keeps_valid_and_array_order_matters(self):
        k1 = rs.dependency_key("plan", {"a": {"y": 1, "x": 2}, "b": [1, 2, 3]}, {"t": 1})
        k2 = rs.dependency_key("plan", {"a": {"x": 2, "y": 1}, "b": [1, 2, 3]}, {"t": 1})
        k3 = rs.dependency_key("plan", {"a": {"x": 2, "y": 1}, "b": [3, 2, 1]}, {"t": 1})
        self.assertEqual(k1, k2, "键顺序/缩进不应改变摘要")
        self.assertNotEqual(k1, k3, "数组顺序影响绘制，不能被排序抹掉")

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "r.json"
            rs.write_report(p, {"kind": "plan", "outcome": "pass", "errors": []}, k1)
            self.assertTrue(rs.read_report(p, k1)["passed"])
            doc = json.loads(p.read_text(encoding="utf-8"))
            p.write_text(json.dumps(dict(reversed(list(doc.items()))), indent=4), encoding="utf-8")
            self.assertEqual(rs.read_report(p, k1)["validity"], "current")
            self.assertEqual(rs.read_report(p, k3)["validity"], "stale")
            self.assertFalse(rs.read_report(p, k3)["passed"])

    def test_corrupt_and_legacy_are_not_pass(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "r.json"
            p.write_text("{bad", encoding="utf-8")
            r = rs.read_report(p, "whatever")
            self.assertEqual((r["validity"], r["outcome"], r["passed"]), ("invalid", "error", False))
            p.write_text(json.dumps({"passed": True}), encoding="utf-8")
            r = rs.read_report(p, "whatever")
            self.assertEqual(r["validity"], "invalid")
            self.assertFalse(r["passed"], "旧格式不能凭自称 passed 通过")


class PlanDecoupleTest(unittest.TestCase):
    def test_old_quality_failure_does_not_block_plan(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            # 旧版输入的失败报告：c_dot3 判不过、inventory 没有 gaps
            write_json(page / "quality.json",
                       {"items": [{"id": "t1", "ok": False}], "n_fail": 1})
            inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
            errs, _ = inv_mod.check(page, page / "spec.json", inv)
            self.assertFalse([e for e in errs if "quality" in e],
                             "plan 校验不许再读 quality 的结论")
            r = gate(page, "plan")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_corrupt_inventory_is_error_not_empty(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            (page / "inventory.json").write_text("{bad", encoding="utf-8")
            r = gate(page, "plan")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("解析失败", (r.stdout or "") + (r.stderr or ""))


# ---------------------------------------------------------------- 因 P3 删脚本而移除
# `MeasureTest`、`MeasureImportTest` 整类删除：它们测的是"按 ID 批量测量"与"把测量结果导入
# 修正层"这两个已归档的脚本（`_archive/P3-20261108/scripts/delivery/`）——SKILL.md §1 的六步
# 主链已不含测量/导入环节。删掉的是"测一个不存在的模块"的用例，不是"断言不成立"：
# **没有任何断言被放松，也没有动任何判据**。


class DependencyBoundaryTest(unittest.TestCase):
    def test_text_color_only_invalidates_build_not_assets(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            out = build_pptx(page)
            sp = page / "spec.json"
            k_assets = rs.key_assets(sp)
            k_build = rs.key_build(sp, out, 320, 180)
            ov = {"elements": {"t1": {"color": "#123456"}}}
            write_json(page / "override.json", ov)
            self.assertEqual(k_assets, rs.key_assets(sp), "只改文字颜色不该重抠素材")
            self.assertNotEqual(k_build, rs.key_build(sp, out, 320, 180), "改文字颜色必须重建")
            write_json(page / "override.json", json.loads(json.dumps(ov)))
            self.assertEqual(k_assets, rs.key_assets(sp), "只改缩进/键顺序不该失效")

    def test_asset_file_change_invalidates_assets(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            img = Image.new("RGBA", (10, 10), (1, 2, 3, 255))
            img.save(page / "assets" / "i1.png")
            base = {"canvas": {"w_px": 320, "h_px": 180, "dpi": 96},
                    "containers": [], "elements": [],
                    "assets": [{"id": "i1", "type": "image", "bbox": [0, 0, 10, 10],
                                "mask": "feather", "src": "assets/i1.png"}]}
            write_json(page / "spec.json", base)
            k0 = rs.key_assets(page / "spec.json")
            img.putpixel((0, 0), (9, 9, 9, 255))
            img.save(page / "assets" / "i1.png")
            self.assertNotEqual(k0, rs.key_assets(page / "spec.json"),
                                "src 路径不变、内容变了，素材报告必须失效")


class DeliveryContractTest(unittest.TestCase):
    def _seed_reports(self, page: Path, out: Path):
        sp = page / "spec.json"
        inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
        ref = page / "reference.png"
        ren = page / "look" / "rendered.png"
        rs.write_report(page / "_check" / "plan.json",
                        {"stage": "plan", "kind": "plan", "outcome": "pass", "errors": []},
                        rs.key_plan(sp, inv))
        rs.write_report(page / "_check" / "assets.json",
                        {"stage": "assets", "kind": "assets", "outcome": "pass", "errors": []},
                        rs.key_assets(sp))
        rs.write_report(page / "_check" / "build.json",
                        {"stage": "build", "kind": "build", "outcome": "pass",
                         "pptx_md5": rs.file_digest(out)},
                        rs.key_build(sp, out, 320, 180))
        rs.write_report(page / "_check" / "editability.json",
                        {"stage": "editability", "kind": "editability", "outcome": "pass",
                         "suspect": 0},
                        rs.key_editability(sp, out, 320, 180))
        # io.json / residual.json 不再种：io_gate 与 residual 已在第 ④ 步 4A 退役，
        # check_delivery 也不再读这两份报告（原先种它们是为了让 gates 的"只记录"分支闭嘴）。

    def test_ready_when_reports_current_then_stale_after_new_product(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            out = build_pptx(page)
            self._seed_reports(page, out)
            errs, notes = gates.check_delivery(page, page / "spec.json", out,
                                               page / "look" / "rendered.png")
            self.assertEqual(errs, [], f"应 ready：{errs} {notes}")

            # 改字色 → 重新构建：旧通过报告不许验收新成品
            write_json(page / "override.json", {"elements": {"t1": {"color": "#FF0000"}}})
            inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
            rs.write_report(page / "_check" / "plan.json",
                            {"stage": "plan", "kind": "plan", "outcome": "pass", "errors": []},
                            rs.key_plan(page / "spec.json", inv))
            out = build_pptx(page)
            errs2, _ = gates.check_delivery(page, page / "spec.json", out,
                                            page / "look" / "rendered.png")
            blob = " ".join(errs2)
            # 成品重建 → build 必失效，交付仍被挡（这条安全属性不变）
            self.assertTrue(any("build" in e for e in errs2), blob)
            # 2.26.2 起：editability 只认「文字清单」，改字色不再逼它重证（它要额外渲两次）
            self.assertFalse(any("editability" in e for e in errs2), blob)
            # 2.37 起这条用例不再断言「渲染图变了 → 签名作废」：人眼签名闸整条移除，
            # 该断言与它的夹具字段一并删除（不是被削弱，是被删除的机制带走的）。


class MicroEditTest(unittest.TestCase):
    """微调不该触发重证：判定对象是「文字清单」，不是成品字节。

    反例（2026-11-07 P05 实测）：一次交付里改字距/加阴影共 6 次，
    每次都让 editability 重跑（额外渲两次、约 7 秒），累计比做图还久。
    这条用例把边界钉死：
      * 改字距 / 改形状名 → editability 的 key 不许变
      * 删文字框（模拟贴图化）→ editability 必须失效

    2.37 起删除 `test_new_render_invalidates_review`（整个方法）与
    `test_letter_spacing_change_does_not_invalidate` 里的 review 断言：
    人眼签名闸整条移除，`report_state.key_review` 已不存在，这两处**因签名链移除而删除**。
    """

    def test_letter_spacing_change_does_not_invalidate(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            out = build_pptx(page)
            k_e = rs.key_editability(page / "spec.json", out, 320, 180)

            from pptx import Presentation
            prs = Presentation(str(out))
            sh = next(s for s in prs.slides[0].shapes if s.has_text_frame and s.text_frame.text.strip())
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    r._r.get_or_add_rPr().set("spc", "120")
            prs.save(str(out))

            self.assertEqual(k_e, rs.key_editability(page / "spec.json", out, 320, 180),
                             "改字距不该让「文字是否真文本」重证")

    def test_text_removed_still_invalidates_editability(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            out = build_pptx(page)
            k_e = rs.key_editability(page / "spec.json", out, 320, 180)
            from pptx import Presentation
            prs = Presentation(str(out))
            sl = prs.slides[0]
            for sh in list(sl.shapes):
                if sh.has_text_frame and sh.text_frame.text.strip():
                    sh._element.getparent().remove(sh._element)
                    break
            prs.save(str(out))
            self.assertNotEqual(k_e, rs.key_editability(page / "spec.json", out, 320, 180),
                                "文字被贴图化/删掉必须让 editability 失效")


class GatePositioningTest(unittest.TestCase):
    """「和参考不像」只报警，「做错了」才拦——这条边界是本包的主契约，必须有测试守着。"""

    _seed_reports = DeliveryContractTest._seed_reports

    def _ready(self, td):
        page = make_page(Path(td))
        out = build_pptx(page)
        self._seed_reports(page, out)
        return page, out

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_io_overrun_is_recorded_not_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            page, out = self._ready(td)
            ref, ren = page / "reference.png", page / "look" / "rendered.png"
            thr = {"max_mae": 14, "max_changed": 0.18,
                   "region_max_mae": 28, "region_max_changed": 0.55}
            regs = [{"r": r, "c": c, "bbox": [c * 107, r * 60, (c + 1) * 107, (r + 1) * 60],
                     "mae": 40.0, "changed": 0.9} for r in range(3) for c in range(3)]
            # 超线：报告照实写 fail，但交付不该被它挡住
            rs.write_report(page / "io.json",
                            {"stage": "io", "kind": "io", "outcome": "fail",
                             "errors": ["整页 mae(all) 40.0 > 14"], "mae": 40.0,
                             "changed": 0.9, "diff_t": 16, "regions": regs,
                             "calibration": {"source": "cli", "thresholds": thr}},
                            rs.key_io_images(ref, ren, thr, 16))
            errs, notes = gates.check_delivery(page, page / "spec.json", out, ren)
            self.assertFalse([e for e in errs if "整图" in e], errs)
            self.assertTrue([n for n in notes if "整图偏差超线" in n], notes)

    def _ready_with_waivers(self, td, waived):
        """建页 → 先写 residual_waived → 再生成报告（plan 依赖里含豁免，顺序不能反）。"""
        page = make_page(Path(td))
        inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
        inv["residual_waived"] = waived
        write_json(page / "inventory.json", inv)
        out = build_pptx(page)          # 内含 plan 闸：会用上面这份 inventory 算 plan 键
        self._seed_reports(page, out)
        return page, out

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_residual_missing_and_extra_are_judged(self):
        """残差「漏/多」参与裁决：该拦的拦，登记过 reason 的豁免只进 notes。

        2026-11-08 起 gates 不再对残差做裁决（唯一出口是 cycle），但四类分流仍然
        必须在 notes 里可区分：未豁免的漏 / 已豁免的漏 / 豁免没写 reason / 多。
        历史上这里踩过两次：一次把所有「残差[」降级成 note（闸形同虚设，
        差异放大 24 倍仍 pass），一次把"登记理由"当万能后门。
        """
        miss = {"kind": "漏", "bbox": [10, 10, 60, 40], "area": 1500}
        waived_ok = {"kind": "漏", "bbox": [10, 10, 60, 40], "reason": "背景装饰，不重建"}
        waived_no_reason = {"kind": "漏", "bbox": [10, 10, 60, 40], "reason": "  "}

        def run(waived, items):
            with tempfile.TemporaryDirectory() as td:
                page, out = self._ready_with_waivers(td, waived)
                ref, ren = page / "reference.png", page / "look" / "rendered.png"
                rp = page / "_check" / "residual" / "residual.json"
                rs.write_report(rp, {"stage": "residual", "kind": "residual",
                                     "outcome": "fail", "items": items},
                                rs.key_residual(ref, ren, gates._res_cfg()))
                return page, out, gates.check_delivery(page, page / "spec.json", out, ren)

        # 2026-11-08 契约变更：交付裁决的唯一出口是 cycle，gates 只记录不裁决
        # （方案 §七#14）。因此下面一律断言"在 notes 里、不在 errs 里"，
        # 但每条各自的可辨识文案照旧保留——记录不等于不区分。
        # ①「漏」未被 residual_waived 匹配 → 记录并标明待交付裁决
        _, _, (errs, notes) = run([], [dict(miss)])
        self.assertFalse([e for e in errs if "残差" in e], errs)
        self.assertTrue([n for n in notes if "待交付裁决" in n and "整块缺失" in n], notes)

        # ②「漏」已匹配且写了 reason → 记录并标明已豁免（两种状态在 notes 里可区分）
        _, _, (errs, notes) = run([waived_ok], [dict(miss)])
        self.assertFalse([e for e in errs if "残差" in e], errs)
        self.assertTrue([n for n in notes if "已豁免" in n], notes)

        # ③「漏」已匹配但没写 reason → 仍记录，且必须点出"没写 reason"
        _, _, (errs, notes) = run([waived_no_reason], [dict(miss)])
        self.assertFalse([e for e in errs if "残差" in e], errs)
        self.assertTrue([n for n in notes if "没写 reason" in n], notes)

        # ④「多」→ 记录（有豁免也不行：豁免按 kind 匹配，漏的豁免不能抵多的）
        extra = {"kind": "多", "bbox": [10, 10, 60, 40], "area": 1500}
        _, _, (errs, notes) = run([dict(waived_ok)], [extra])
        self.assertFalse([e for e in errs if "残差" in e], errs)
        self.assertTrue([n for n in notes if "待交付裁决" in n and "残差[多]" in n], notes)

        # 没算过 ≠ 算出来不合格：报告缺失同样降为记录（它也是诊断产物）
        with tempfile.TemporaryDirectory() as td:
            page, out = self._ready_with_waivers(td, [])
            ren = page / "look" / "rendered.png"
            (page / "_check" / "residual" / "residual.json").unlink()
            errs, notes = gates.check_delivery(page, page / "spec.json", out, ren)
            self.assertFalse([e for e in errs if "残差" in e], errs)
            self.assertTrue([n for n in notes if "残差检查不可用" in n], notes)

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_residual_waiver_matches_nested_box(self):
        """豁免匹配的两条族：逐边 ≤24px 永远生效；「包含」**默认关闭**，逐块 `containment` 才开。

        回归背景：residual 改成 content 掩膜后只比对声明区域，检测框被裁窄 —— p37 登记
        `[1488,215,1665,482]`、报告 `[1528,328,1648,482]`（左差 40、上差 113）→ 逐边失配。
        但「包含」是逐边 ≤24 的**严格超集**，`reason` 又是按框登记一次的：无条件生效就等于
        让一块"背景不重建"的框吸收区域内任意大小的漏（连真漏掉的内容元素也能吞）。所以放水
        必须逐块显式、可审计——**默认必须仍失配**，这是本用例的第一条断言。
        """
        waived = {"kind": "漏", "bbox": [1488, 215, 1665, 482], "reason": "背景装饰，不重建"}
        waived_contain = dict(waived, containment=True)
        nested = {"kind": "漏", "bbox": [1528, 328, 1648, 482], "area": 1886}   # 完全在框内
        # 下面三条与豁免框的上边界都差 85px（逐边判据必然失配），只在左边界上做边界对照：
        inside = {"kind": "漏", "bbox": [1488, 300, 1665, 482], "area": 1900}   # 正好贴边 = 在框内
        spill1 = {"kind": "漏", "bbox": [1487, 300, 1665, 482], "area": 1900}   # 外溢 1px → 不算
        partial = {"kind": "漏", "bbox": [1448, 300, 1665, 482], "area": 1900}  # 部分重叠 → 不算
        # 逐边 ≤24 的"同一块"：两条路径都必须匹配（旧行为不能破）
        near = {"kind": "漏", "bbox": [1492, 219, 1661, 486], "area": 1800}

        def check(waivers, items):
            with tempfile.TemporaryDirectory() as td:
                page, out = self._ready_with_waivers(td, [dict(w) for w in waivers])
                ref, ren = page / "reference.png", page / "look" / "rendered.png"
                rp = page / "_check" / "residual" / "residual.json"
                rs.write_report(rp, {"stage": "residual", "kind": "residual",
                                     "outcome": "fail", "items": [dict(i) for i in items]},
                                rs.key_residual(ref, ren, gates._res_cfg()))
                return gates.check_delivery(page, page / "spec.json", out, ren)[1]

        def waived_of(waivers, items):
            return [n for n in check(waivers, items) if "已豁免" in n]

        # ① 未开开关 + 小框 ∈ 大框 → **必须仍失配**（钉"默认严格"，最重要的一条）
        self.assertTrue([n for n in check([waived], [nested]) if "待交付裁决" in n],
                        "containment 未开启时，包含关系一律不生效")
        # ② 未开开关 + 逐边 ≤24 → 匹配（旧行为不能破）
        self.assertTrue(waived_of([waived], [near]), "逐边 ≤24px 的匹配必须保持无条件有效")
        # ③ 开了开关 + 完全在框内 → 匹配；贴边（外溢 0）也算在框内
        self.assertTrue(waived_of([waived_contain], [nested]), "显式开了 containment 才允许包含匹配")
        self.assertTrue(waived_of([waived_contain], [inside]), "贴边（外溢 0）属于包含")
        # ④ **不留余量**：外溢 1px 必须失配（8px 余量已按实测收掉），部分重叠同样失配
        self.assertTrue([n for n in check([waived_contain], [spill1]) if "待交付裁决" in n],
                        "即使开了开关，外溢 1px 也不算在框内：包含不留容差")
        self.assertTrue([n for n in check([waived_contain], [partial]) if "待交付裁决" in n],
                        "即使开了开关，部分重叠也不算匹配")

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_residual_waiver_absorption_is_blocked_beyond_two(self):
        """吸收数：**≤2 项放行，>2 项阻塞**（`WAIVER_ABSORB_MAX = 2`）。

        项数按**检测框**计（p38 左竖带那处底纹被 residual 切成两段），所以 2 项不能做闸：
        某次切得细就会误判。但 >2 项已经不是"一处设计决定"——2026-11-08 实测：换深色底后
        整块标题消失，正好 3 项被一条豁免吸收，交付照样全绿。更早的 QA 实测一块
        `[836,0,1672,941]` + reason「背景装饰」一次吞掉 42 个真漏、交付直接 ready。
        误拦是响亮、可修、给动作的；漏放是静默、毁整页的。上界 2 = p38 真实最大值，不留余量。
        """
        waived = {"kind": "漏", "bbox": [0, 100, 400, 700], "reason": "背景装饰，不重建",
                  "containment": True}
        three = [{"kind": "漏", "bbox": [60, 180, 140, 260], "area": 6000},
                 {"kind": "漏", "bbox": [60, 300, 140, 380], "area": 6000},
                 {"kind": "漏", "bbox": [60, 420, 140, 500], "area": 6000}]
        five = three + [{"kind": "漏", "bbox": [200, 180, 280, 260], "area": 6000},
                        {"kind": "漏", "bbox": [200, 300, 280, 380], "area": 6000}]

        def residual_of(items):
            """返回 (r_errs, r_notes, d_errs, d_notes)。

            前两项来自 `_check_residual`（**真正的裁决层**）；后两项来自 `check_delivery`。
            必须在 `_check_residual` 上断言"是否阻塞"——`check_delivery` 会把它的所有 errs
            无条件降级成 notes（`notes += [f"残差（待交付裁决）：{e}" …]`），在那里断言
            恒真（2026-11-08 QA 用 mutI 证明过：提示改成 errs.append 后用例照样绿）。
            """
            with tempfile.TemporaryDirectory() as td:
                page, out = self._ready_with_waivers(td, [dict(waived)])
                ref, ren = page / "reference.png", page / "look" / "rendered.png"
                rp = page / "_check" / "residual" / "residual.json"
                rs.write_report(rp, {"stage": "residual", "kind": "residual",
                                     "outcome": "fail", "items": [dict(i) for i in items]},
                                rs.key_residual(ref, ren, gates._res_cfg()))
                inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
                r_errs, r_notes = gates._check_residual(page / "spec.json", inv, ren)
                d_errs, d_notes = gates.check_delivery(page, page / "spec.json", out, ren)
                return r_errs, r_notes, d_errs, d_notes

        # ① 2 项（= 上限）→ 不阻塞、也不出提示：正常页面最多 2 项（p38 同一处底纹被切两段）
        r_errs, r_notes, d_errs, d_notes = residual_of(three[:2])
        self.assertFalse([e for e in r_errs if "吸收" in e],
                         f"2 项是正常值（上限 2），不该阻塞：{r_errs}")
        self.assertFalse([n for n in r_notes if "吸收" in n], "2 项不该出提示")

        # ② 3 项（> 上限 2）→ **阻塞**：第 3 项起就不是"同一处被切碎"，是新问题被旧豁免盖住
        #    （2026-11-08 实测：换深色底后整块标题消失，正好 3 项被一条豁免吸收，交付全绿）
        r_errs, r_notes, d_errs, d_notes = residual_of(three)
        hit3 = [e for e in r_errs if "吸收了 3 项" in e]
        self.assertTrue(hit3, f"3 项必须阻塞（上限 2）：{r_errs}")
        self.assertIn("[60,180,140,260]", hit3[0], "阻塞文案要列出被吞的 bbox")
        self.assertIn("超过上限 2", hit3[0], "要写明上限值，人才知道改到几")

        # ② 5 项（> 上限）→ **阻塞**，且文案给动作
        r_errs, r_notes, _, _ = residual_of(five)
        blocked = [e for e in r_errs if "吸收" in e]
        self.assertTrue(blocked, f"超过上限 4 必须进 errs：{r_errs}")
        self.assertIn(f"超过上限 {gates.WAIVER_ABSORB_MAX}", blocked[0], "文案必须点名上限")
        self.assertIn("拆成多块", blocked[0], "文案必须给出动作（拆登记或修图）")
        self.assertIn("不要用一块大框覆盖整片区域", blocked[0], "文案必须说清禁止的做法")
        self.assertIn("共 5", blocked[0], "阻塞文案同样要可审计：列出被吞的框与总数")

    @unittest.skip("inventory.residual_waived 已退役（2.42.0）：被测的是该字段的类型契约，字段已删")
    def test_residual_waiver_containment_must_be_bool(self):
        """`containment` 写成字符串 "true" → inventory 校验必须报错，不许静默退化成没开。

        `w.get("containment") is True` 对字符串为假，页面会莫名变 blocked 而没人知道原因，
        正是本库一路在消灭的静默降级。所以在记录侧（`inventory.check`）就点名拦下。
        """
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
            inv["residual_waived"] = [
                {"kind": "漏", "bbox": [0, 100, 400, 700], "reason": "背景装饰", "containment": "true"}]
            errs, _ = inv_mod.check(page, page / "spec.json", inv)
            self.assertTrue([e for e in errs if "containment='true'" in e],
                            f"字符串 containment 必须被点名：{errs}")
            self.assertTrue([e for e in errs if "必须是布尔" in e], errs)
            # 真正的布尔值 / 不写 都合法（默认严格，不报错）
            for ok in (True, False, None):
                inv["residual_waived"] = [{"kind": "漏", "bbox": [0, 100, 400, 700], "reason": "背景装饰"}]
                if ok is not None:
                    inv["residual_waived"][0]["containment"] = ok
                errs2, _ = inv_mod.check(page, page / "spec.json", inv)
                self.assertFalse([e for e in errs2 if "containment" in e],
                                 f"{ok!r} 是合法值，不该报错：{errs2}")


    @unittest.skip("残差的 containment 提示已退役（2.42.0）：该 note 写在已删的 _stage_residual 里")
    def test_cycle_reports_containment_blocks(self):
        """cycle 必须把「本页有几块豁免开了 containment」打在 note 里——放水要看得见。

        这条 note **刻意放在 residual 的「复用 / 重跑」两个分支之外**：日常路径是报告
        current 直接复用，若只写进重跑分支，平时永远不打印（与 io 那个"只在重算时判
        stale"是同一类错误）。0 块时不打印（输出「有 0 块」是噪音）。
        它不改判据、不加闸：只让"开了几块"在终端可见。
        """
        two = [{"kind": "漏", "bbox": [0, 100, 400, 700], "reason": "背景装饰",
                "containment": True},
               {"kind": "漏", "bbox": [500, 100, 900, 700], "reason": "右上底纹",
                "containment": True}]
        with tempfile.TemporaryDirectory() as td:
            page, out = self._ready_with_waivers(td, [dict(w) for w in two])

            def run_cycle() -> list[str]:
                r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                                    "--page", str(page), "--mode", "delivery"],
                                   capture_output=True, text=True, timeout=180)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                doc = json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))
                self.assertEqual(doc["state"], "ready", doc["state"])
                return [str(x.get("message") or "") for x in doc["to_fix"]]

            msgs = run_cycle()
            hint = [m for m in msgs if "本页有 2 块豁免开了 containment" in m]
            self.assertTrue(hint, f"应汇总 containment 块数：{msgs}")
            self.assertIn("[0,100,400,700]", hint[0], hint[0])
            self.assertIn("[500,100,900,700]", hint[0], hint[0])
            self.assertIn(f"上限 {gates.WAIVER_ABSORB_MAX} 项", hint[0], hint[0])

            # 去掉第二块的字段 → 变 1 块（其余一字不动）
            inv_p = page / "inventory.json"
            inv = json.loads(inv_p.read_text(encoding="utf-8"))
            inv["residual_waived"][1].pop("containment")
            write_json(inv_p, inv)
            msgs = run_cycle()
            self.assertTrue([m for m in msgs if "本页有 1 块豁免开了 containment" in m], msgs)
            self.assertFalse([m for m in msgs if "[500,100,900,700]" in m and "containment" in m],
                             msgs)

            # 两块都去掉 → 完全不打印（0 块是噪音）
            inv["residual_waived"][0].pop("containment")
            write_json(inv_p, inv)
            msgs = run_cycle()
            self.assertFalse([m for m in msgs if "开了 containment" in m], msgs)

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_thresholds_read_back_when_calibration_has_scope(self):
        """两条分支的 key 载荷各守一半：回退分支（无 policy）只许回 IO_KEYS 四项，
        policy 分支必须带 mae_scope 与归一化的 region_overrides。一边多一项、一边少一项，
        都会让 cycle 算出的 key 与 io_gate 写进 io.json 的那份不同源 → 每轮判 stale。

        2026-11-08 追加：回退分支拿到「形状不对」的载荷必须**响亮 REFUSED**，不许
        return None 掉回 harmony 档——那会把页面声明的 14/0.18/28/0.55 悄悄换成
        18/0.35/40/0.70，日志只写一句「· harmony 默认档」，比 stale 更隐蔽。
        """
        import cycle as cy
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            self.assertIsNone(cy._thresholds(page))          # 无 policy / 无 io.json
            # ① 正常形状（io_gate 无 policy 写的就是这四项）→ 读回四个数
            write_json(page / "io.json", {"calibration": {
                "source": "cli",
                "thresholds": {"max_mae": 14, "max_changed": 0.18,
                               "region_max_mae": 28, "region_max_changed": 0.55}}})
            thr = cy._thresholds(page)
            self.assertIsNotNone(thr, "四项载荷应能读回")
            self.assertEqual(thr["max_mae"], 14.0)
            # 载荷必须与 io_gate 无 policy 时写的四项逐键相同：mae_scope 不进 key。
            self.assertEqual(set(thr["thresholds"]), set(rs.IO_KEYS))
            self.assertEqual(thr["thresholds"],
                             {"max_mae": 14.0, "max_changed": 0.18,
                              "region_max_mae": 28.0, "region_max_changed": 0.55})
            # ② 多一项（有人把回退分支改回 io_settings 的样子）→ REFUSED，不是 harmony
            write_json(page / "io.json", {"calibration": {
                "source": "cli",
                "thresholds": {"max_mae": 14, "max_changed": 0.18,
                               "region_max_mae": 28, "region_max_changed": 0.55,
                               "mae_scope": "content"}}})
            with self.assertRaises(SystemExit) as cm:
                cy._thresholds(page)
            self.assertIn("IO_KEYS 四项", str(cm.exception))
            # ③ 缺一项同样 REFUSED（不能悄悄降级成 harmony 档）
            write_json(page / "io.json", {"calibration": {
                "source": "cli",
                "thresholds": {"max_mae": 14, "max_changed": 0.18, "region_max_mae": 28}}})
            with self.assertRaises(SystemExit) as cm:
                cy._thresholds(page)
            self.assertIn("region_max_changed", str(cm.exception))
            # ④ 「没有 thresholds」这条正常回退仍在：返回 None 走 harmony 默认档
            write_json(page / "io.json", {"calibration": {"source": "cli"}})
            self.assertIsNone(cy._thresholds(page))
            write_json(page / "io.json", {"calibration": {"source": "manual"}})
            self.assertIsNone(cy._thresholds(page))          # 不认识来源 → 仍走 harmony
            # 另一条分支（policy 在场，cycle 的第一分支）：`io_settings` 的载荷**必须带**
            # mae_scope 与归一化后的 region_overrides —— io_gate 的 `extra` 正是这么取的
            # （`settings_norm` 去掉 IO_KEYS 后的剩余键）。剥成 4 键 → policy 页每轮判
            # stale，与上面回退分支那条是同一类问题，方向相反。
            full = rs.io_settings({"max_mae": 14, "max_changed": 0.18,
                                   "region_max_mae": 28, "region_max_changed": 0.55,
                                   "mae_scope": "content",
                                   "region_overrides": [{"r": 2, "c": 0, "max_mae": 55.0,
                                                         "reason": "底部装饰条版本不同"}]})
            self.assertEqual(set(full), set(rs.IO_KEYS) | {"mae_scope", "region_overrides"})
            self.assertEqual(full["mae_scope"], "content")
            # reason 只用于声明，归一化后剥掉；两档阈值留下来、缺的写 None。
            self.assertEqual(full["region_overrides"],
                             [{"r": 2, "c": 0, "max_mae": 55.0, "max_changed": None}])
            ref_p, ren_p = page / "reference.png", page / "look" / "rendered.png"
            ref_p.write_bytes(b"ref")
            ren_p.write_bytes(b"ren")
            k_policy = rs.key_io_images(ref_p, ren_p, full)
            self.assertEqual(k_policy, rs.key_io_images(ref_p, ren_p, dict(full)))
            # 这两条才是咬人的：谁把 mae_scope / region_overrides 从载荷里丢掉，
            # policy 分支就会和 io_gate 的 extra 不同源（key 永不相等），这里必须红。
            for drop in ("mae_scope", "region_overrides"):
                self.assertNotEqual(
                    k_policy,
                    rs.key_io_images(ref_p, ren_p,
                                     {k: v for k, v in full.items() if k != drop}),
                    f"两份载荷必须算出不同 key（少了 {drop} 就该不同源）——"
                    f"policy 分支的 key 载荷少了 {drop}，会与 io_gate 的 extra 不同源")
            # 缺阈值时的兜底档位存在且是四个数
            self.assertEqual(set(cy.HARMONY_DEFAULT), {"max_mae", "max_changed",
                                                       "region_max_mae", "region_max_changed"})


class IoPolicyEndToEndTest(unittest.TestCase):
    """`io_policy.json` → `io_gate` → `cycle` 的端到端（P2.5）。

    为什么补这组：`grep io_policy tests/` 以前是 **0** —— 这不是"少一条用例"，
    而是全库唯一没有端到端覆盖的分支，历史 bug 正好长在这条缝里：
      * gates 与 cycle 各自拼 io 子键 → io_gate 刚写的 io.json 下一轮被判 stale
        （每轮白算 0.47s，日志永远 stale，2026-11-08 page06 实测）；
      * 回退分支过一遍 io_settings() 会多补 mae_scope → 4 键变 5 键 → 同上；
      * 页面声明的 14/0.18/28/0.55 被悄悄换成 harmony 默认档，日志只写一句
        「· harmony 默认档」（比 stale 更隐蔽的静默降级）。
    这三条都只有在"真的跑一遍 io_gate + 真的用 cycle 的判据读回来"时才会露出来。
    """

    POLICY = {"max_mae": 14.0, "max_changed": 0.18,
              "region_max_mae": 28.0, "region_max_changed": 0.55, "mae_scope": "content"}

    def _page(self, td, **over):
        page = make_page(Path(td))
        write_json(page / "io_policy.json", {**self.POLICY, **over})
        return page

    def _cycle(self, page: Path, *extra):
        return subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                               "--page", str(page), "--mode", "delivery", *extra],
                              capture_output=True, text=True, timeout=300)

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_policy_reaches_io_json_and_key_is_same_source_as_cycle(self):
        """policy 的四项阈值必须真的进 io.json，且 cycle 算出的键与它同源（不 stale）。"""
        import cycle
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            r = self._cycle(page)
            self.assertIn("页面阈值", r.stdout, f"没走 policy 分支：{r.stdout[-500:]}")
            io = json.loads((page / "io.json").read_text(encoding="utf-8"))
            cal = io.get("calibration") or {}
            self.assertEqual(cal.get("source"), "policy:io_policy.json", cal)
            thr = cal.get("thresholds") or {}
            for k in rs.IO_KEYS:
                self.assertEqual(float(thr[k]), float(self.POLICY[k]), f"{k} 没进 io.json：{thr}")

            # 同源：用 cycle 自己的取阈值函数 + 同一个 key 函数去读 io_gate 刚写的那份
            derived = cycle._thresholds(page)
            self.assertIsNotNone(derived, "cycle 没认出 io_policy（会退成 harmony 默认档）")
            key = rs.key_io_images(page / "reference.png", page / "look" / "rendered.png",
                                   derived["thresholds"], io["diff_t"])
            rd = rs.read_report(page / "io.json", key)
            self.assertEqual(rd["validity"], "current",
                             f"io.json 刚写完就被判 {rd['validity']}：cycle 与 io_gate 的 key 不同源")

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_second_run_reuses_io_instead_of_stale(self):
        """第二轮必须 reused：这是那条 stale bug 的用户可见判据。"""
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            self._cycle(page)
            r2 = self._cycle(page)
            io_line = next((ln for ln in r2.stdout.splitlines() if ln.startswith("io")), "")
            self.assertIn("reused", io_line, f"第二轮 io 没被复用：{io_line!r}\n{r2.stdout[-600:]}")
            self.assertNotIn("stale", io_line, io_line)

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_policy_threshold_is_the_real_gate_not_a_label(self):
        """把 max_mae 卡到 0 → io 必须 fail：阈值是闸门，不是日志里的一行字。"""
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, max_mae=0.0, max_changed=0.0,
                              region_max_mae=0.0, region_max_changed=0.0)
            self._cycle(page)
            io = json.loads((page / "io.json").read_text(encoding="utf-8"))
            self.assertEqual(io.get("outcome"), "fail", io)
            blob = " ".join(io.get("errors") or [])
            self.assertIn("mae", blob, io)

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_bad_policy_is_refused_loudly(self):
        """policy 读不出阈值必须 REFUSED（人话），不许静默退成 harmony 默认档。"""
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, mae_scope="page")
            r = self._cycle(page)
            both = r.stdout + r.stderr
            self.assertNotIn("状态 ready", both, "非法 mae_scope 被静默降级成 ready")
            self.assertIn("mae_scope", both, f"没点名 mae_scope：{both[-500:]}")

    # ---------------- 三种坏 policy：按 SKILL.md:144 的契约（REFUSED + 退出码 1）
    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_bad_policy_three_states_refused_with_documented_exit_code(self):
        """3 键 / 坏 JSON / mae_scope:"all" —— 三种都要 REFUSED 且退出码 1（SKILL.md:144）。"""
        cases = [
            ("三键", {"max_mae": 14.0, "max_changed": 0.18, "region_max_mae": 28.0},
             "region_max_changed"),
            ("坏 JSON", "__BROKEN__", "读不出设置"),
            ("mae_scope=all", {**self.POLICY, "mae_scope": "all"}, "mae_scope"),
        ]
        for tag, pol, must in cases:
            with tempfile.TemporaryDirectory() as td:
                page = make_page(Path(td))
                if pol == "__BROKEN__":
                    (page / "io_policy.json").write_text("{ 这不是 json", encoding="utf-8")
                else:
                    write_json(page / "io_policy.json", pol)
                r = self._cycle(page)
                both = r.stdout + r.stderr
                self.assertEqual(r.returncode, 1, f"[{tag}] 退出码应为 1（SKILL.md:144）：{both[-400:]}")
                self.assertIn("REFUSED", both, f"[{tag}] 没 REFUSED：{both[-400:]}")
                self.assertIn(must, both, f"[{tag}] 没点名 {must}：{both[-400:]}")
                self.assertNotIn("状态 ready", both, f"[{tag}] 坏 policy 竟然 ready")

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_old_five_key_io_json_without_policy_prints_delete_hint(self):
        """policy 被删掉、io.json 还是 5 键旧产物 → REFUSED 并要求「删掉 io.json」（SKILL.md:144）。

        注意：**policy 还在时**这份旧 io.json 不会触发它（policy 分支根本不读 io.json，
        旧报告只是判 stale 然后被重写）。这条 REFUSED 只在回退分支出现 —— 实测口径。
        """
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            self.assertFalse((page / "io_policy.json").exists(), "这个夹具要的是「policy 被删掉」")
            thr5 = {**self.POLICY}                      # 5 键：四项 + mae_scope
            rs.write_report(page / "io.json",
                            {"stage": "io", "kind": "io", "outcome": "pass", "errors": [],
                             "mae": 0.0, "changed": 0.0, "diff_t": 16, "regions": [],
                             "calibration": {"source": "policy:io_policy.json",
                                             "thresholds": thr5}},
                            rs.key_io_images(page / "reference.png",
                                             page / "look" / "rendered.png",
                                             {k: thr5[k] for k in rs.IO_KEYS}, 16))
            r = self._cycle(page)
            both = r.stdout + r.stderr
            self.assertEqual(r.returncode, 1, both[-500:])
            self.assertIn("回退分支的阈值载荷不是 IO_KEYS 四项", both, both[-500:])
            self.assertIn("删掉 io.json", both, f"没给修法：{both[-500:]}")

    # ---------------- mae_scope=content 必须是真口径，不是标签
    def _run_io_gate(self, page: Path, *extra):
        return subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "io_gate.py"),
                               "--page", str(page), *extra],
                              capture_output=True, text=True, timeout=120)

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_content_scope_decides_not_full_page(self):
        """背景区域有差异、内容区域零差异：content 口径要 PASS，全页口径会超线。

        make_page 的参考图与渲染图本来是同一张（全页 mae=0），所以这里**只在背景角落**
        涂一块深色——content 掩膜不含那块，于是 mae_content=0 而全页 mae>0。
        阈值卡在两者之间：口径若真是 content 就过，若悄悄按全页算就会超线。
        """
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, max_mae=1.0, max_changed=0.2,
                              region_max_mae=100.0, region_max_changed=1.0)
            ren = Image.open(page / "look" / "rendered.png").convert("RGB")
            d = ImageDraw.Draw(ren)
            d.rectangle([260, 8, 315, 50], fill=(0, 0, 0))   # 背景角落，不在任何 spec 对象框内
            ren.save(page / "look" / "rendered.png")

            r = self._run_io_gate(page)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            io = json.loads((page / "io.json").read_text(encoding="utf-8"))
            self.assertEqual(io.get("scope"), "content", io)
            self.assertEqual(io.get("outcome"), "pass", io)
            self.assertEqual(io.get("mae_content"), 0.0, f"内容口径不该被背景污染：{io}")
            self.assertGreater(io.get("mae") or 0, 1.0,
                              f"这块背景差异本该让全页 mae 超过阈值 1.0：{io}")

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_stdout_drops_background_number_json_keeps_it(self):
        """`mae_bg` 从 stdout 拿掉（用户的痛点是"还在惦记背景"），但 io.json 必须留着。"""
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            r = self._run_io_gate(page)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("背景", r.stdout, f"stdout 还在提背景：{r.stdout}")
            io = json.loads((page / "io.json").read_text(encoding="utf-8"))
            self.assertIn("mae_bg", io, "io.json 里的 mae_bg 不许删（诊断证据）")
            self.assertIn("mae_bg 只报不闸", io.get("note") or "", "口径说明那句也要留")


class ProbeTest(unittest.TestCase):
    """scan 的核心价值：穿过文字也能读出真实边界（掩膜法会在这里断掉）。"""

    def test_scan_reads_edge_through_text(self):
        with tempfile.TemporaryDirectory() as td:
            page = Path(td) / "page"
            page.mkdir(parents=True)
            img = Image.new("RGB", (320, 180), (255, 255, 255))
            d = ImageDraw.Draw(img)
            d.rectangle([0, 0, 159, 179], fill=(0, 102, 204))       # 左半蓝底
            for i in range(3):                                      # 边界两侧压白色"文字"
                d.rectangle([120 + i * 30, 70, 140 + i * 30, 90], fill=(255, 255, 255))
            img.save(page / "reference.png")
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "probe.py"),
                                "scan", "--page", str(page), "--axis", "row",
                                "--at", "75", "--from", "100", "--to", "220",
                                "--tol", "20", "--band", "120", "--json",
                                str(page / "s.json")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            runs = json.loads((page / "s.json").read_text(encoding="utf-8"))["runs"]
            # band 远大于字高（120 vs 20）时，中位能压掉文字，边界落在 160 附近（±3）
            edges = [r["end"] for r in runs]
            self.assertTrue(any(abs(e - 159) <= 3 for e in edges),
                            f"没读出 160 附近的边界：{runs}")
            self.assertGreaterEqual(len(runs), 2, runs)
class GateCostTest(unittest.TestCase):
    """闸门的成本与松紧：「贵而空转」和「过紧造成无意义返工」都要能被测出来。"""

    def test_editability_static_passes_without_render(self):
        """默认可编辑性检查不渲染：报告标 method=static，且不产出中间图。"""
        import pptx
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            out = build_pptx(page)
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "editability.py"),
                                "--page", str(page), "--spec", str(page / "spec.json"),
                                "--out", str(out), "--width", "320", "--height", "180"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            rep = json.loads((page / "_check" / "editability.json").read_text(encoding="utf-8"))
            self.assertEqual(rep.get("method"), "static", rep)
            self.assertEqual(rep.get("suspect"), 0, rep)
            self.assertFalse((page / "look" / "_edit_base.png").exists(),
                             "静态检查不该留渲染中间图")

    def test_editability_static_catches_picture_text(self):
        """文字被图片顶替（同名占位）必须判失败——这是这条闸存在的唯一理由。"""
        from pptx import Presentation
        from pptx.util import Emu
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            out = build_pptx(page)
            prs = Presentation(str(out))
            sl = prs.slides[0]
            target = next((sh for sh in sl.shapes if sh.name == "t1"), None)
            self.assertIsNotNone(target, "构建产物里应有 t1")
            box = (target.left, target.top, target.width, target.height)
            target._element.getparent().remove(target._element)
            pic = sl.shapes.add_picture(str(page / "reference.png"), *box)
            pic.name = "t1"                      # 同名占位：静态检查必须认出来是图
            fake = page / "fake.pptx"
            prs.save(str(fake))
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "editability.py"),
                                "--page", str(page), "--spec", str(page / "spec.json"),
                                "--out", str(fake), "--width", "320", "--height", "180"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            rep = json.loads((page / "_check" / "editability.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(rep.get("suspect", 0), 1, rep)

    def test_spec_lint_still_blocks_text_overlap(self):
        """assets 阶段不再判压字，所以 plan 阶段必须真的还在管——否则就是两头落空。"""
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            img = Image.new("RGBA", (70, 80), (10, 10, 10, 255))
            img.save(page / "assets" / "block.png")
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["assets"] = [{"id": "blk", "type": "image", "mask": "feather",
                               "bbox": [10, 40, 80, 120], "feather_px": [4, 4, 4, 4],
                               "src": "assets/block.png"}]
            write_json(page / "spec.json", spec)
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "spec_lint.py"),
                                "--spec", str(page / "spec.json")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("文字压图", r.stdout)

            # 给了 covers_text 但 inventory 没 locked → 仍必须拦（豁免要两件套）
            spec["assets"][0]["covers_text"] = True
            write_json(page / "spec.json", spec)
            r2 = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "spec_lint.py"),
                                 "--spec", str(page / "spec.json")],
                                capture_output=True, text=True)
            self.assertEqual(r2.returncode, 1, "只写 covers_text 不算豁免：" + r2.stdout)

    def test_asset_guard_no_longer_duplicates_text_overlap(self):
        """压文字只由 plan 阶段的 spec_lint 判一次，assets 阶段不再重复报 I1。"""
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            img = Image.new("RGBA", (70, 80), (10, 10, 10, 255))
            img.save(page / "assets" / "block.png")
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["assets"] = [{"id": "blk", "type": "image", "mask": "feather",
                               "bbox": [10, 40, 80, 120], "feather_px": [4, 4, 4, 4],
                               "src": "assets/block.png"}]
            write_json(page / "spec.json", spec)
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "asset_guard.py"),
                                "--page", str(page), "--spec", str(page / "spec.json")],
                               capture_output=True, text=True)
            self.assertNotIn("I1", r.stdout, f"assets 阶段不该再判压字：{r.stdout}")

# ---------------------------------------------------------------- 因 P3 删脚本而移除
# `SimulateTest` 整类删除：它测的是"抠图前先试算候选掩膜"的那个已归档脚本
# （`_archive/P3-20261108/scripts/delivery/`）。SKILL.md §1 的 S2 已改为"按 mask 表自上而下
# 挑第一条能过的，都过不了才 matting"，预测试算不在主链上。**没有断言被放松**。

class RenderPathTest(unittest.TestCase):
    """冷路径：这些分支从没在测试里跑过，连「参数怎么给」都没有可执行的说明。"""

    def _page(self, td):
        return make_page(Path(td))

    def _cut(self, page, eid):
        r = subprocess.run([PY, str(ROOT / "scripts" / "fastcut.py"),
                            "--spec", str(page / "spec.json"),
                            "--image", str(page / "reference.png"),
                            "--outdir", str(page / "assets"), "--force-ids", eid],
                           capture_output=True, text=True)
        return r

    def test_circle_needs_square_box(self):
        """圆形的框必须是正方，否则四角一定有 α——报错要说这句，不能只报角内 α 偏高。"""
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            img = Image.new("RGB", (320, 180), (255, 255, 255))
            ImageDraw.Draw(img).ellipse([80, 20, 240, 180], fill=(200, 40, 40))
            img.save(page / "reference.png")
            spec["assets"] = [{"id": "ci", "type": "image", "mask": "circle",
                               "circle": [160, 100, 80], "bbox": [80, 20, 240, 180],
                               "src": None}]
            write_json(page / "spec.json", spec)
            r = self._cut(page, "ci")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            png = np.asarray(Image.open(page / "assets" / "ci.png").convert("RGBA"))
            self.assertEqual(int(png[0, 0, 3]), 0, "左上角必须透明")
            self.assertEqual(int(png[-1, -1, 3]), 0, "右下角必须透明")
            self.assertGreater(int(png[80, 80, 3]), 250, "圆心必须不透明")

            # 同一个圆，给了长方形的框 → 必须报「框要方」，而不是让人去猜角内 α
            spec["assets"][0]["bbox"] = [80, 20, 240, 140]
            write_json(page / "spec.json", spec)
            r2 = self._cut(page, "ci")
            self.assertNotEqual(r2.returncode, 0, "非方框必须拒绝")
            self.assertIn("方", r2.stdout + r2.stderr, f"报错要指向方形裁框：{r2.stdout}")

    def test_rectmask_zeros_excluded_area(self):
        """rectmask 的排除矩形里 α 必须为 0，区域外保持不透明。"""
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            img = Image.new("RGB", (200, 200), (255, 255, 255))
            ImageDraw.Draw(img).rectangle([40, 40, 160, 160], fill=(30, 90, 200))
            img.save(page / "reference.png")
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["assets"] = [{"id": "rm", "type": "image", "mask": "rectmask",
                               "bbox": [40, 40, 160, 160],
                               "feather_px": [2, 2, 2, 2],
                               "mask_rects": [[40, 40, 100, 160]], "src": None}]
            write_json(page / "spec.json", spec)
            r = self._cut(page, "rm")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            png = np.asarray(Image.open(page / "assets" / "rm.png").convert("RGBA"))
            self.assertEqual(int(png[60, 20, 3]), 0, "排除区（左半）α 必须为 0")
            self.assertGreater(int(png[60, 100, 3]), 250, "排除区外必须不透明")

    def test_chroma_keeps_saturated_subject(self):
        """色键只留饱和主体，纯白底要全透明。"""
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            img = Image.new("RGB", (120, 120), (255, 255, 255))
            ImageDraw.Draw(img).ellipse([30, 30, 90, 90], fill=(230, 30, 40))
            img.save(page / "reference.png")
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["assets"] = [{"id": "ch", "type": "image", "mask": "chroma",
                               "chroma_sat": 45, "bbox": [30, 30, 90, 90], "src": None}]
            write_json(page / "spec.json", spec)
            r = self._cut(page, "ch")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            png = np.asarray(Image.open(page / "assets" / "ch.png").convert("RGBA"))
            self.assertGreater(int(png[30, 30, 3]), 250, "主体中心必须不透明")
            self.assertEqual(int(png[1, 1, 3]), 0, "白底必须透明")

    def test_freeform_and_preset_shapes_render(self):
        """freeform 与 6 个预设形状必须真的落成对应形状，不能退化成矩形。"""
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["containers"] = [
                {"id": "ff", "type": "shape", "shape": "freeform",
                 "points": [[10, 10], [80, 10], [80, 60], [10, 60]], "fill": "#CCDDEE"},
                {"id": "chv", "type": "shape", "shape": "chevron", "bbox": [90, 10, 150, 50], "fill": "#CCDDEE"},
                {"id": "dia", "type": "shape", "shape": "diamond", "bbox": [160, 10, 220, 50], "fill": "#CCDDEE"},
                {"id": "arr", "type": "shape", "shape": "left_arrow", "bbox": [230, 10, 300, 50], "fill": "#CCDDEE"},
            ]
            write_json(page / "spec.json", spec)
            inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
            for cid, reason in (("ff", "native-freeform"), ("chv", "native-simple"),
                                ("dia", "native-simple"), ("arr", "native-simple")):
                inv["items"].append({"id": cid, "section": "containers", "kind": "native",
                                     "reason": reason, "decided_by": "human",
                                     "evidence": f"shape test {cid}", "approx_of": None,
                                     "gaps": [], "crop_note": None, "locked": False,
                                     "locked_reason": None})
            write_json(page / "inventory.json", inv)
            out = build_pptx(page)
            prs = Presentation(str(out))
            by = {sh.name: sh for sh in prs.slides[0].shapes}
            self.assertIn("ff", by)
            self.assertEqual(by["ff"].shape_type, MSO_SHAPE_TYPE.FREEFORM, "freeform 没落成任意多边形")
            for cid, want in (("chv", "CHEVRON"), ("dia", "DIAMOND"), ("arr", "LEFT_ARROW")):
                self.assertIn(cid, by, f"{cid} 没画出来")
                self.assertEqual(str(by[cid].auto_shape_type).split()[0], want,
                                 f"{cid} 形状被退化：{by[cid].auto_shape_type}")

    def test_multiline_center_spacing_light(self):
        """多行 / 居中 / 字距 / light 字重四个参数必须各自生效。"""
        from pptx import Presentation
        from pptx.enum.text import PP_ALIGN
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["elements"] = [{"id": "t1", "type": "text", "text": "第一行\n第二行",
                                 "bbox": [40, 40, 280, 130], "size_pt": 18,
                                 "color": "#112233", "align": "center",
                                 "line_spacing": 1.4, "letter_spacing_pt": 1.5,
                                 "weight": "light"}]
            write_json(page / "spec.json", spec)
            out = build_pptx(page)
            prs = Presentation(str(out))
            sh = next(s for s in prs.slides[0].shapes if s.name == "t1")
            tf = sh.text_frame
            self.assertEqual(len(tf.paragraphs), 2, "换行没拆成两段")
            self.assertEqual(tf.paragraphs[0].alignment, PP_ALIGN.CENTER, "居中没生效")
            self.assertAlmostEqual(float(tf.paragraphs[0].line_spacing), 1.4, places=2,
                                   msg="行距没生效")
            r0 = tf.paragraphs[0].runs[0]
            self.assertEqual(r0.font.name, "微软雅黑 Light", f"light 字重没生效：{r0.font.name}")
            spc = r0._r.find("{http://schemas.openxmlformats.org/drawingml/2006/main}rPr").get("spc")
            self.assertEqual(int(spc), 150, f"字距没写进 rPr@spc：{spc}")

class SpecExtensionTest(unittest.TestCase):
    """P2c（2026-11-08）：规格扩展里**核实成立**的三条，加上一条「判不成立」的记录。

    A name 在形状 / 文字 / 图片**三处**都被 `xxx.name = el["id"]` 无条件覆盖 → 成立，已修；
    C 形状填充走 `RGBColor.from_string(...)`，`#4F8` / `#GGG` 抛 ValueError traceback
      （实测：`int() with base 16: ''`）→ 成立，已修（背景纯色那条本来就有人员校验，未动）；
    D `MSO_SHAPE` 里没有 line，`shape:"line"` 被 `SHAPES.get(...,ROUNDED_RECTANGLE)` 兜底成
      圆角矩形 → 成立，已修；
    B「顶层 color 被静默忽略、要合并 {**el, **el["line"]}」→ **判不成立，不做**：
      全库 24 份 spec 里 `shape:"line"` 0 例、顶层线色 0 例，且 `set_line` 读的就是 line 段，
      元素的 `color` 是文字色——合并等于给同一个键造两种含义。
    这四条的共同点：**闸全绿而东西不对**，所以只能在这一层钉住。
    """

    def _spec_page(self, td):
        page = make_page(Path(td))
        (page / "inventory.json").unlink()      # 直测 build 本体的分支，不经过 plan 门闩
        return page

    def _build(self, page: Path, out: str = "o.pptx"):
        return subprocess.run([PY, str(ROOT / "scripts" / "build.py"),
                               "--spec", str(page / "spec.json"), "--out", str(page / out)],
                              capture_output=True, text=True)

    def _lint(self, page: Path):
        return subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "spec_lint.py"),
                               "--spec", str(page / "spec.json")],
                              capture_output=True, text=True)

    def _set_containers(self, page: Path, containers) -> None:
        spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
        spec["containers"] = containers
        write_json(page / "spec.json", spec)

    def test_custom_name_is_kept_on_all_three_writers(self):
        """A：形状 / 文字 / 图片**三处**写入点同一条规则；id 落 `cNvPr@title`，三闸都认得出。

        只改一处等于没修——三处原来都是 `xxx.name = el["id"]` 无条件覆盖。
        三条认元素的检查器（presence / editability / look）改成走 `consts.shape_id`，
        所以名字被换掉之后它们仍按 id 认人。图片这条尤其要测：add_picture **自己会写
        `descr`=图片文件名**，拿 descr 当 id 兜底会让每张图都被认成 "i1.png"。
        """
        from pptx import Presentation
        from consts import shape_id
        with tempfile.TemporaryDirectory() as td:
            page = self._spec_page(td)
            self._set_containers(page, [
                {"id": "arr1", "type": "shape", "shape": "right_arrow",
                 "bbox": [30, 30, 150, 80], "fill": "#3060C0", "name": "箭头A"},
                {"id": "box1", "type": "shape", "shape": "rounded_rect",
                 "bbox": [170, 30, 290, 80], "fill": "#DDEEFF"},
            ])
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["elements"][0]["name"] = "标题文字"
            Image.new("RGB", (20, 20), (10, 20, 30)).save(page / "assets" / "i1.png")
            spec["assets"] = [{"id": "i1", "type": "image", "bbox": [200, 100, 260, 160],
                               "src": "assets/i1.png", "name": "小图"}]
            write_json(page / "spec.json", spec)

            r = self._build(page)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            shapes = list(Presentation(str(page / "o.pptx")).slides[0].shapes)
            self.assertEqual({sh.name for sh in shapes}, {"箭头A", "box1", "标题文字", "小图"},
                             "三处 name 规则不一致")
            self.assertEqual([shape_id(sh) for sh in shapes], ["arr1", "box1", "i1", "t1"],
                             "shape_id 取不到 id（presence/editability/look 会全线报缺）")

            # presence：判据不变（集合比对），只换取值来源
            errs, _notes = gates.check_presence(page, page / "spec.json", page / "o.pptx")
            self.assertEqual(errs, [], f"presence 认不出改了名的元素：{errs}")

            # editability：静态检查按 shape_id 建索引，改名后仍要全过
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "editability.py"),
                                "--page", str(page), "--spec", str(page / "spec.json"),
                                "--out", str(page / "o.pptx"), "--width", "320", "--height", "180"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            rep = json.loads((page / "_check" / "editability.json").read_text(encoding="utf-8"))
            self.assertEqual(rep.get("suspect"), 0, rep)

    def test_three_digit_color_expands_and_bad_color_is_human(self):
        """C：`#4F8` 展开成 `#44FF88`；`#GGG` 是人话错误，不是 traceback，也不留半成品。"""
        import zipfile
        from consts import parse_hex_color
        self.assertEqual(parse_hex_color("#4F8"), "44FF88", "3 位短写展开规则不对")
        with tempfile.TemporaryDirectory() as td:
            page = self._spec_page(td)
            self._set_containers(page, [
                {"id": "c1", "type": "shape", "shape": "rounded_rect",
                 "bbox": [30, 30, 200, 80], "fill": "#4F8"}])
            r = self._build(page)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            xml = zipfile.ZipFile(str(page / "o.pptx")).read("ppt/slides/slide1.xml").decode()
            self.assertIn('val="44FF88"', xml, "3 位色没展开成 44FF88")
            self.assertEqual(self._lint(page).returncode, 0, "spec_lint 与 build 对 #4F8 结论不一致")

            self._set_containers(page, [
                {"id": "c1", "type": "shape", "shape": "rounded_rect",
                 "bbox": [30, 30, 200, 80], "fill": "#GGG"}])
            out = page / "bad.pptx"
            r = subprocess.run([PY, str(ROOT / "scripts" / "build.py"),
                                "--spec", str(page / "spec.json"), "--out", str(out)],
                               capture_output=True, text=True)
            both = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0, both)
            self.assertIn("颜色写法不认识", both, f"不是人话错误：{both}")
            self.assertIn("#GGG", both)
            self.assertNotIn("Traceback", both, f"崩成 traceback：{both}")
            self.assertFalse(out.exists(), "报错还留下了半成品 pptx")
            lr = self._lint(page)
            self.assertEqual(lr.returncode, 1, "spec_lint 放过了 build 会拒的颜色")
            self.assertIn("颜色写法不认识", lr.stdout)

    def test_look_read_back_keys_are_ids_not_names(self):
        """look 的「同名文字帧 / 图片数」也走 shape_id：改名后仍要认得出，且认得对。

        不渲染就能测——这条判据与像素无关，渲染只影响像素那三条。
        """
        from pptx import Presentation
        import look
        with tempfile.TemporaryDirectory() as td:
            page = self._spec_page(td)
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["elements"][0]["name"] = "标题文字"
            Image.new("RGB", (20, 20), (10, 20, 30)).save(page / "assets" / "i1.png")
            spec["assets"] = [{"id": "i1", "type": "image", "bbox": [200, 100, 260, 160],
                               "src": "assets/i1.png", "name": "小图"}]
            write_json(page / "spec.json", spec)
            r = self._build(page)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            texts, pics = look.read_back(Presentation(str(page / "o.pptx")).slides[0])
            self.assertEqual(list(texts), ["t1"], f"文字帧的键不是 id：{texts}")
            self.assertEqual(texts["t1"], ["AAAA"], texts)
            self.assertEqual(pics, 1, "图片数被 custom name 影响了")

    def test_line_shape_is_connector_not_rectangle(self):
        """D：`shape:"line"` 必须落成 p:cxnSp（连接线），不是圆角矩形。"""
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        with tempfile.TemporaryDirectory() as td:
            page = self._spec_page(td)
            self._set_containers(page, [
                {"id": "ln1", "type": "shape", "shape": "line",
                 "bbox": [20, 20, 300, 160], "line": {"color": "#112233", "width_pt": 1}}])
            r = self._build(page)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            sh = next(s for s in Presentation(str(page / "o.pptx")).slides[0].shapes
                      if s.name == "ln1")
            self.assertEqual(sh.shape_type, MSO_SHAPE_TYPE.LINE, f"被画成了 {sh.shape_type}")
            self.assertTrue(sh._element.tag.endswith("cxnSp"),
                            f"OOXML 不是连接线：{sh._element.tag}")


class TopologyTest(unittest.TestCase):
    """三层数据的边界：决策记录不存派生值、流程问题在门口拦。

    P3 删脚本后，「容器测量只给建议框、不给确定 bbox」那条用例随之移除：它跑的是已归档的测量
    脚本（`_archive/P3-20261108/`），不是断言不成立。判据没变——容器吸附结果历来只是建议，
    自动导入会让人把错框当测量结果（SKILL.md §1 S1）。
    """

    def test_evidence_has_no_size_snapshot(self):
        """evidence 只记"它是什么"，不记尺寸——改 bbox 后尺寸快照会静默过期。"""
        import inventory as inv_mod
        ev = inv_mod.derive({"id": "c1", "type": "shape", "shape": "rounded_rect",
                             "bbox": [10, 10, 120, 90], "fill": "#FFFFFF"},
                            "containers")["evidence"]
        self.assertNotIn("bbox=", ev, f"决策记录不该存尺寸快照：{ev}")
        self.assertIn("shape=", ev, ev)
        self.assertIn("fill=", ev, ev)

    def test_cycle_refuses_without_inventory(self):
        """inventory 是**派生物**：缺了当场重建；但解析失败必须门店硬拒。

        原断言（缺文件 → rc=1）在 2026-11-08 被判为判据错位：inventory.json 能由
        spec+override 完整派生，缺文件不是流程问题；真正的风险是**手改坏**——那时
        `old={}` 会把 locked / locked_reason / gaps / residual_waived 静默抹成零签字。
        所以牙齿挪到了解析失败这一侧：必须 rc=1，且不许覆盖原文。
        """
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            (page / "inventory.json").unlink()
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                                "--page", str(page), "--mode", "preview"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("inventory.json", r.stdout)
            self.assertIn("重建", r.stdout)

            # 手改坏的 JSON：拒，且原文一字不动（它可能是人工签字的唯一副本）
            (page / "inventory.json").write_text("{oops", encoding="utf-8")
            r2 = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                                 "--page", str(page), "--mode", "preview"],
                                capture_output=True, text=True)
            self.assertEqual(r2.returncode, 1, r2.stdout + r2.stderr)
            self.assertIn("REFUSED", r2.stdout)
            self.assertIn("inventory.json", r2.stdout)
            self.assertEqual((page / "inventory.json").read_text(encoding="utf-8"), "{oops",
                             "解析失败时不许覆盖 inventory.json（人工签字会随之消失）")


class PresenceContractTest(unittest.TestCase):
    """check_presence 的两种“跑不了”必须报错，不许崩成 ValueError。"""

    def test_missing_product_returns_pair(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            none = page / "none.pptx"
            got = gates.check_presence(page, sp, none)
            self.assertIsInstance(got, tuple)
            self.assertEqual(len(got), 2)
            self.assertIsInstance(got[0], list)
            self.assertIsInstance(got[1], list)
            self.assertIn("缺成品", got[0][0])

            g = gate(page, "presence", "--spec", str(sp), "--out", str(none))
            self.assertEqual(g.returncode, 1, g.stdout + g.stderr)
            self.assertNotIn("Traceback", g.stderr)
            rep = json.loads((page / "_check" / "presence.json").read_text(encoding="utf-8"))
            self.assertEqual(rep["outcome"], "fail")

    def test_corrupt_pptx_returns_pair(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            out = build_pptx(page)
            out.write_bytes(b"PK\x03\x04junk")
            got = gates.check_presence(page, sp, out)
            self.assertIsInstance(got, tuple)
            self.assertEqual(len(got), 2)
            self.assertIn("读不出来", got[0][0])

            g = gate(page, "presence", "--spec", str(sp), "--out", str(out))
            self.assertEqual(g.returncode, 1, g.stdout + g.stderr)
            self.assertNotIn("Traceback", g.stderr)

    def test_all_checks_return_pairs(self):
        """四个 check_* 都必须是 (list, list)；return "msg", [] 要能挂在这里。"""
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            out = build_pptx(page)
            cases = [
                ("plan", lambda: gates.check_plan(page, sp)),
                ("assets", lambda: gates.check_assets(page, sp)),
                ("presence", lambda: gates.check_presence(page, sp, out)),
                ("delivery", lambda: gates.check_delivery(
                    page, sp, out, page / "look" / "rendered.png")),
            ]
            for name, fn in cases:
                got = fn()
                self.assertIsInstance(got, tuple, name)
                self.assertEqual(len(got), 2, f"{name} 必须返回 (errs, notes)：{got}")
                self.assertIsInstance(got[0], list, name)
                self.assertIsInstance(got[1], list, f"{name} 第二个元素必须是 list")


class DeliveryGuardTest(unittest.TestCase):
    """交付守卫的公开契约（presence 键稳定、退出码映射、error 阶段不 ready）。

    P3 后删掉那条"命令行渲染"用例：被删的命令行渲染函数只服务于 2.37 已移除的「下一步签名
    命令」，全库早已无调用方（该模块已归档到 `_archive/P3-20261108/`）。**没有断言被放松**
    ——剩下三条测的都是仍在主链上的行为。
    """

    def test_key_presence_stable(self):
        """不 bump CHECKER_VERSION 也安全：成品没变，presence 键必须逐字节相同。"""
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            out = build_pptx(page)
            k1 = rs.key_presence(sp, out)
            self.assertEqual(rs.key_presence(sp, out), k1)
            out.write_bytes(b"PK\x03\x04broken")
            self.assertNotEqual(rs.key_presence(sp, out), k1)

    def test_finish_exit_codes(self):
        """两态 + 未知态兜底：改 _finish 的映射等于改 CLI 公开契约。

        2.37：`needs_review(3)` 一态因签名链移除而删除（原始包只有 ready/blocked），
        这里保留的两条 + 未知态兜底是删后仍在的契约；`bogus` 必须落 1，不许沉默成 0。
        """
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            check = page / "_check"
            buf = io.StringIO()
            with redirect_stdout(buf):
                for state, want in {"ready": 0, "blocked": 1, "error": 1,
                                    "bogus": 1}.items():
                    rc = cycle._finish(page, check, "preview", state, [], [],
                                       {"normal": 0, "editability": 0, "diagnostic": 0},
                                       0.1)
                    self.assertEqual(rc, want, state)
            self.assertTrue((check / "cycle.json").exists())

    def test_has_error_phase_counts_only_error_status(self):
        """裁决的唯一新增判据：只有 status=error 的阶段不许 ready。

        2.42.0 改写：原用例拿 `diagnostics:verify` 与 `io` 两个阶段名做断言，而这两个阶段
        随诊断脚本与 io_gate 一起退役，名字已不可能出现——断言变成永不失败的空断言。
        现在只钉语义本身：error 算崩，fail / skipped / reused 都不算（fail 是"判据没过"，
        由 to_fix 的 blocking 决定，不是"阶段崩了"）。
        """
        self.assertTrue(cycle._has_error_phase([{"name": "presence", "status": "error"}]))
        self.assertFalse(cycle._has_error_phase([{"name": "presence", "status": "fail"}]))
        self.assertFalse(cycle._has_error_phase([{"name": "editability", "status": "skipped"}]))
        self.assertFalse(cycle._has_error_phase([{"name": "look", "status": "reused"}]))
        self.assertFalse(cycle._has_error_phase([]))


class CycleVerdictTest(unittest.TestCase):
    """两态退出码 + P0-3（error 阶段不得 ready）。全部 seed 报告，0 渲染。

    2.37：`needs_review(3)` 与其用例 `test_needs_review_exit_3`、
    `test_next_command_is_executable_and_correct_interpreter`（读 `pending_review`）
    **因签名链移除而删除**——没有"等人签名"这个状态，就不存在它的退出码与下一步命令。
    """

    def _prep(self, td):
        page = make_page(Path(td))
        sp = page / "spec.json"
        out = build_pptx(page)
        rs.write_report(page / "_check" / "build.json",
                        {"stage": "build", "kind": "build", "outcome": "pass",
                         "pptx": str(out), "pptx_md5": rs.file_digest(out),
                         "profile": "harmony", "out_size": [320, 180]},
                        rs.key_build(sp, out, 320, 180))
        return page, sp, out

    def _seed_look(self, page, out):
        look = page / "look"
        png = look / "rendered.png"
        # dependency_key 必须一起签：B1 之后 look 的复用键里含**判据源码指纹**，
        # 没有这一项的报告按"旧格式"一律不算有效（否则改完判据跑 cycle 会 reused，
        # 新判据一次都不执行）。夹具要模拟的是"一份对当前成品+渲图+判据都有效的报告"。
        (look / "look.json").write_text(json.dumps({
            "pptx_md5": rs.file_digest(out)[:16],
            "png_md5": rs.file_digest(png)[:16],
            "dependency_key": rs.key_look(page / "spec.json", out, png),
            "render_secs": 0.0, "mae": 0.0, "changed": 0.0, "fails": []},
            ensure_ascii=False, indent=1), encoding="utf-8")

    def _cycle(self, page, mode="delivery"):
        r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                            "--page", str(page), "--mode", mode],
                           capture_output=True, text=True, timeout=120)
        doc = json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))
        return r, doc

    def _resign_build(self, page, sp, out):
        """把 build 报告重新签成「当前这份成品」。

        2026-11-10 起 build 复用多了一条硬条件：盘上 pptx 的摘要必须等于报告记的摘要，
        不一致就按 spec 重建。于是「先手改成品、再跑 cycle」这种夹具会被**修好**——
        成品被重建回 spec 的样子，要测的坏状态当场消失。
        本方法还原夹具想说的事：**构建阶段本身就产出了这份坏成品**（真实现场是
        build.py 静默漏块/产出坏文件却报了 pass）。它不放松任何断言，只让坏状态活到
        presence / look 那一步，被测的判据才有对象可判。
        """
        rs.write_report(page / "_check" / "build.json",
                        {"stage": "build", "kind": "build", "outcome": "pass",
                         "pptx": str(out), "pptx_md5": rs.file_digest(out),
                         "profile": "harmony", "out_size": [320, 180]},
                        rs.key_build(sp, out, 320, 180))

    def test_ready_exit_0(self):
        with tempfile.TemporaryDirectory() as td:
            page, sp, out = self._prep(td)
            self._seed_look(page, out)
            r, doc = self._cycle(page)          # 第一轮：出齐报告并由 gates 裁决
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(doc["state"], "ready")
            r2, doc2 = self._cycle(page)        # 第二轮：全部报告有效 → 仍然 ready
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertEqual(doc2["state"], "ready")
            self.assertFalse([p for p in doc2["phases"] if p["status"] == "error"],
                             doc2["phases"])

    def test_error_phase_blocks(self):
        """P0-3 回归：presence 崩掉时 preview 以前返回 ready/0。"""
        with tempfile.TemporaryDirectory() as td:
            page, sp, out = self._prep(td)
            out.write_bytes(b"PK\x03\x04broken")
            self._resign_build(page, sp, out)   # 坏成品是"构建阶段产出的"，别让重建把它修好
            self._seed_look(page, out)  # 让 look 复用，别去跑 36s 超时
            r, doc = self._cycle(page, "preview")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertEqual(doc["state"], "blocked")
            self.assertTrue(any(p["name"] == "presence" and p["status"] == "error"
                                for p in doc["phases"]))

    def test_fail_report_does_not_outlive_repaired_product(self):
        """坏状态不许被缓存毒死：成品换回真件，presence 必须自己转 pass。"""
        with tempfile.TemporaryDirectory() as td:
            page, sp, out = self._prep(td)
            good = out.read_bytes()
            out.write_bytes(b"PK\x03\x04junk")
            g1 = gate(page, "presence", "--spec", str(sp), "--out", str(out))
            self.assertEqual(g1.returncode, 1, g1.stdout + g1.stderr)
            rep1 = json.loads((page / "_check" / "presence.json").read_text(encoding="utf-8"))
            self.assertEqual(rep1["outcome"], "fail")

            out.write_bytes(good)
            g2 = gate(page, "presence", "--spec", str(sp), "--out", str(out))
            self.assertEqual(g2.returncode, 0, g2.stdout + g2.stderr)
            rep2 = json.loads((page / "_check" / "presence.json").read_text(encoding="utf-8"))
            self.assertEqual(rep2["outcome"], "pass")


class LintDocChannelTest(unittest.TestCase):
    """lint_doc 的 --skill 通道以前零测试；L12 的正反两侧都要钉住。"""

    def _run(self, doc: Path):
        return subprocess.run([PY, str(ROOT / "scripts" / "lint_doc.py"), "--skill", str(doc)],
                              capture_output=True, text=True, timeout=180)

    def test_clean_skill_passes(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "SKILL.md"
            shutil.copyfile(ROOT / "SKILL.md", p)
            r = self._run(p)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("PASS", r.stdout)

    def test_retired_phrase_fails_L7(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "SKILL.md"
            p.write_text((ROOT / "SKILL.md").read_text(encoding="utf-8") + "\n结构闸门\n",
                         encoding="utf-8")
            r = self._run(p)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("L7", r.stdout)

    def test_l12_catches_hardcoded_python_both_sides(self):
        import lint_doc as ld
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.py"
            bad.write_text('PY="x"\nprint(f"python x.py")\n', encoding="utf-8")
            self.assertEqual(len(ld._hardcoded_python(bad)), 1, "旧写法必须被抓到")
            good = Path(td) / "good.py"
            good.write_text('PY="x"\nprint(f\'{PY} "x.py"\')\n', encoding="utf-8")
            self.assertEqual(ld._hardcoded_python(good), [], "新写法不许误判")
            doc = Path(td) / "note.py"
            doc.write_text('"""用法：python x.py"""\n', encoding="utf-8")
            self.assertEqual(ld._hardcoded_python(doc), [], "docstring 示例不许误伤")


class ElementTypeTest(unittest.TestCase):
    """type 写成非文字时，块会被 build 画成文字、却被所有检查器跳过（2026-11-08 QA 实测）。"""

    def _with_table(self, page: Path, color: str | None = "#000000") -> dict:
        spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
        el = {"id": "t2", "type": "table", "text": "BBBB",
              "bbox": [150, 60, 280, 90], "size_pt": 18, "style": "label", "align": "left"}
        if color is not None:
            el["color"] = color
        spec["elements"].append(el)
        write_json(page / "spec.json", spec)
        # 参考图里也给它一块墨迹，verify 量它才有意义
        ref = Image.open(page / "reference.png").convert("RGB")
        ImageDraw.Draw(ref).rectangle([152, 66, 278, 84], fill=(0, 0, 0))
        ref.save(page / "reference.png")
        shutil.copyfile(page / "reference.png", page / "look" / "rendered.png")
        return spec

    def test_table_missing_color_fails_spec_lint(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            self._with_table(page, color=None)
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "spec_lint.py"),
                                "--spec", str(page / "spec.json")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("缺字段 color", r.stdout)
            self.assertIn("t2", r.stdout)

    def test_unknown_type_value_fails_spec_lint(self):
        """`Text` / `txt` 这类写错的值只能靠白名单拦：它们会被当成"非文字"静默跳过。"""
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            spec = self._with_table(page)
            spec["elements"][1]["type"] = "txt"
            write_json(page / "spec.json", spec)
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "spec_lint.py"),
                                "--spec", str(page / "spec.json")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("type 取值不认识", r.stdout)

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_table_block_is_measured_by_verify(self):
        """build 把 elements 段一律画成文字 → verify 必须量到**全部**文字块。"""
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            self._with_table(page)
            out = build_pptx(page)          # 内含 plan 闸：type/必填字段都得先过
            write_json(page / "tolerance.json", {"floor": {}})
            r = subprocess.run([PY, str(ROOT / "scripts" / "verify.py"),
                                "--spec", str(page / "spec.json"),
                                "--reference", str(page / "reference.png"),
                                "--rendered", str(page / "look" / "rendered.png"),
                                "--out", str(page / "verify.json"),
                                "--tolerance", str(page / "tolerance.json")],
                               capture_output=True, text=True, timeout=120)
            self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
            rep = json.loads((page / "verify.json").read_text(encoding="utf-8"))
            ids = {it["id"] for it in rep["items"]}
            self.assertEqual(ids, {"t1", "t2"},
                             f"type=table 的块被漏量了：{ids} / pptx={out.name}")


class DeliveryDepsTest(unittest.TestCase):
    """W/H 不许被吞：自定义渲染尺寸时两个键必须对得上，否则永远「build 不可用」。"""

    def test_gates_and_cycle_agree_and_size_matters(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            out = build_pptx(page)
            sp, png, d = page / "spec.json", page / "look" / "rendered.png", page
            inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
            for w, h in ((320, 180), (999, 555)):
                self.assertEqual(gates.delivery_deps(sp, out, png, d, w, h),
                                 cycle._delivery_deps(sp, out, png, w, h, inv),
                                 f"{w}x{h}：gates 与 cycle 的交付依赖键必须逐键相同")
            # 缺省回退 spec.canvas
            self.assertEqual(gates.delivery_deps(sp, out, png, d),
                             gates.delivery_deps(sp, out, png, d, 320, 180))
            # 尺寸必须真的进键（否则 build 报告永远是「对不上」或反过来永远「对得上」）
            self.assertNotEqual(gates.delivery_deps(sp, out, png, d, 320, 180)["build"],
                                gates.delivery_deps(sp, out, png, d, 999, 555)["build"])

    def test_custom_render_size_does_not_report_build_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            out = build_pptx(page)
            sp, png = page / "spec.json", page / "look" / "rendered.png"
            # 模拟 cycle --width 999 --height 555：build/editability 报告都按 999×555 写
            rs.write_report(page / "_check" / "build.json",
                            {"stage": "build", "kind": "build", "outcome": "pass",
                             "pptx_md5": rs.file_digest(out), "out_size": [999, 555]},
                            rs.key_build(sp, out, 999, 555))
            rs.write_report(page / "_check" / "editability.json",
                            {"stage": "editability", "kind": "editability",
                             "outcome": "pass", "suspect": 0},
                            rs.key_editability(sp, out, 999, 555))
            ok_errs, _ = gates.check_delivery(page, sp, out, png, width=999, height=555)
            self.assertFalse([e for e in ok_errs if "build" in e or "editability" in e],
                             f"999x555 不该判 build 不可用：{ok_errs}")
            # 反证：不传尺寸（按 canvas 320×180 算）时，同一份报告必须判不可用
            bad_errs, _ = gates.check_delivery(page, sp, out, png)
            self.assertTrue([e for e in bad_errs if "build/render 不可用" in e], bad_errs)


class VerifyScopeTest(unittest.TestCase):
    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_zero_blocks_exits_2(self):
        """一个块都没量到 ≠ 全过：必须是 REFUSED(2)，不是 PASS(0)。

        2.36.1 起 verify 的遍历范围是 elements 段全集（不再按 type 过滤），
        所以"一个都量不到"只剩 elements 为空这一种情况。
        """
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
            spec["elements"] = []
            write_json(page / "spec.json", spec)
            write_json(page / "tolerance.json", {"floor": {}})
            r = subprocess.run([PY, str(ROOT / "scripts" / "verify.py"),
                                "--spec", str(page / "spec.json"),
                                "--reference", str(page / "reference.png"),
                                "--rendered", str(page / "look" / "rendered.png"),
                                "--tolerance", str(page / "tolerance.json")],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("REFUSED", r.stdout)


class PresenceMissingTest(unittest.TestCase):
    _prep = CycleVerdictTest._prep
    _seed_look = CycleVerdictTest._seed_look
    _resign_build = CycleVerdictTest._resign_build

    def test_missing_block_blocks_delivery(self):
        """整块缺失是 MUST #4：pptx 里少一个 shape 必须 blocked，不许 ready。"""
        from pptx import Presentation
        with tempfile.TemporaryDirectory() as td:
            page, sp, out = self._prep(td)                 # 已含 plan + build 报告
            prs = Presentation(str(out))
            sl = prs.slides[0]
            target = next(sh for sh in sl.shapes if sh.name == "t1")
            target._element.getparent().remove(target._element)   # 整块不见（pptx 仍合法）
            prs.save(str(out))
            errs, _ = gates.check_presence(page, sp, out)
            self.assertTrue([e for e in errs if "spec 声明了但 pptx 里没有" in e], errs)

            # cycle 侧：presence 报告按当前 pptx 重算 → 阻塞（不渲染，look 复用）
            self._resign_build(page, sp, out)   # 缺块是"构建产出的成品缺块"，不许被重建抹掉
            self._seed_look(page, out)
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                                "--page", str(page), "--mode", "preview"],
                               capture_output=True, text=True, timeout=180)
            doc = json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertEqual(doc["state"], "blocked")
            self.assertTrue([f for f in doc["to_fix"]
                             if f["stage"] == "presence" and f.get("blocking", True)], doc["to_fix"])


class ElementTextFieldTest(unittest.TestCase):
    """P2-1（2026-11-08 第二轮 QA）：elements 段缺 text 曾让 spec_lint 与 gates 双双 rc=0，
    而 build 在 `txt = el["text"]` 上 KeyError traceback。

    根因是"会不会被画成文字"用「text 非空」判断：`{"type":"table","bbox":[…]}` 没有 text
    → 被判成非文字 → 必填检查不查 text、verify 跳过它 → 没人发现 build 会崩。
    现在 elements 段 = 文字段（判据只看段位），三处必须一致地拦。
    """

    def _with_textless(self, page: Path) -> None:
        spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
        spec["elements"].append({"id": "t2", "type": "table", "bbox": [150, 60, 280, 90]})
        write_json(page / "spec.json", spec)

    def test_spec_lint_reports_missing_text(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            self._with_textless(page)
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "spec_lint.py"),
                                "--spec", str(page / "spec.json")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertNotIn("PASS", r.stdout, "缺 text 不许 PASS")
            self.assertIn("缺字段 text", r.stdout)
            self.assertIn("t2", r.stdout)

    def test_plan_gate_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            self._with_textless(page)
            r = gate(page, "plan")
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_verify_measures_it_instead_of_skipping(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            self._with_textless(page)
            write_json(page / "tolerance.json", {"floor": {}})
            r = subprocess.run([PY, str(ROOT / "scripts" / "verify.py"),
                                "--spec", str(page / "spec.json"),
                                "--reference", str(page / "reference.png"),
                                "--rendered", str(page / "look" / "rendered.png"),
                                "--out", str(page / "verify.json"),
                                "--tolerance", str(page / "tolerance.json")],
                               capture_output=True, text=True, timeout=120)
            self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
            rep = json.loads((page / "verify.json").read_text(encoding="utf-8"))
            ids = {it["id"] for it in rep["items"]}
            self.assertEqual(ids, {"t1", "t2"}, f"缺 text 的块被 verify 跳过了：{ids}")

    def test_build_refuses_without_traceback(self):
        """绕过闸直调 build 时也不许 traceback + 半成品 pptx。

        没 inventory 的老路径会跳过 plan 门闩（否则它会先以「缺 plan.json」拒跑），
        这样才落到 build 自己的 elements 检查上——缺 text 必须点名，不是 KeyError。
        """
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            self._with_textless(page)
            (page / "inventory.json").unlink()
            out = page / "direct.pptx"
            r = subprocess.run([PY, str(ROOT / "scripts" / "build.py"),
                                "--spec", str(page / "spec.json"), "--out", str(out)],
                               capture_output=True, text=True, timeout=120)
            blob = (r.stdout or "") + (r.stderr or "")
            self.assertNotEqual(r.returncode, 0, blob)
            self.assertIn("REFUSED", blob)
            self.assertIn("t2", blob)
            self.assertNotIn("Traceback", blob, "缺 text 必须是 REFUSED，不是 traceback")


class DeliveryGateErrorTest(unittest.TestCase):
    """gates 报的错必须落到交付结论上：剩下的唯一状态是 blocked(1)。

    这个用例原属已删除的 `ReviewOnlyVerdictTest`（`test_cycle_blocks_on_non_review_gate_error`）：
    签名链移除后，它那一步 `gates --stage delivery --reviewed-by 张三` 本身已不存在，
    **因签名链移除**只删掉那个夹具步骤，断言（"gates 报 <a:ea> 缺失 → blocked(1)"）原样保留——
    这条覆盖的是"cycle 自己看不见、只有 gates 看得见"的错误不许被漏掉。
    """

    def test_ooxml_missing_east_asian_font_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            page, sp, out = self._prep(td)                 # plan + build 报告
            # 删掉东亚字体 → gates 的 OOXML 检查会报错，而 cycle 自己不看这条
            removed = _strip_east_asian_fonts(out)
            self.assertGreater(removed, 0, "夹具不成立：pptx 里没有 <a:ea>")
            # 成品字节变了，build 报告要按新字节重签，否则 cycle 会去重建（把 ea 写回来）
            rs.write_report(page / "_check" / "build.json",
                            {"stage": "build", "kind": "build", "outcome": "pass",
                             "pptx_md5": rs.file_digest(out), "out_size": [320, 180]},
                            rs.key_build(sp, out, 320, 180))
            self._seed_look(page, out)
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                                "--page", str(page), "--mode", "delivery"],
                               capture_output=True, text=True, timeout=180)
            doc = json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["state"], "blocked", f"{r.stdout}\n{doc['to_fix']}")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertTrue([f for f in doc["to_fix"] if "<a:ea" in f["message"]], doc["to_fix"])

    _prep = CycleVerdictTest._prep
    _seed_look = CycleVerdictTest._seed_look


class ResidualVerdictTest(unittest.TestCase):
    """P3-3 变异自证（二）：cycle 侧残差的 blocking 断言（此前只有 gates 侧）。

    2.36.0 #4 的修法是把裁决归给 cycle：「多」/未豁免的「漏」/豁免没写 reason → 阻塞。
    变异自证：把 cycle.py 里 `fix("residual", e, blocking=hard)` 改成 `blocking=False`，
    本用例会从 blocked(1) 变成 ready(0) 而变红（2.37 起没有 needs_review 这一态了）。
    """

    _prep = CycleVerdictTest._prep
    _seed_look = CycleVerdictTest._seed_look

    @unittest.skip(_SKIP_IO_RESIDUAL)
    def test_unwaived_residual_blocks_in_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            page, sp, out = self._prep(td)
            self._seed_look(page, out)
            ref, ren = page / "reference.png", page / "look" / "rendered.png"
            rs.write_report(page / "_check" / "residual" / "residual.json",
                            {"stage": "residual", "kind": "residual", "outcome": "fail",
                             "items": [{"kind": "漏", "bbox": [10, 10, 60, 40], "area": 1500},
                                       {"kind": "多", "bbox": [70, 10, 120, 40], "area": 1500}]},
                            rs.key_residual(ref, ren, cycle._res_cfg()))
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                                "--page", str(page), "--mode", "delivery"],
                               capture_output=True, text=True, timeout=240)
            doc = json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["state"], "blocked", f"{r.stdout}\n{doc['to_fix']}")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            hard = [f for f in doc["to_fix"]
                    if f["stage"] == "residual" and f.get("blocking", True)]
            self.assertEqual(len(hard), 2, f"未豁免的漏/多必须是阻塞项：{doc['to_fix']}")


class VerdictArtifactTest(unittest.TestCase):
    """P4-1：`_check/verdict.json` 是单页裁决的唯一产物（五条布尔 + source + 交付结论）。

    反例（2026-11-08）：「这页对不对」以前要读 7 份报告 + 终端输出才能拼出来，
    于是同一页能同时存在"终端说做完了"和"报告说缺一块"两种自证。
    这里钉三件事：键集合、一个数字都不许有、假布尔必须在终端点名。
    """

    _prep = CycleVerdictTest._prep
    _seed_look = CycleVerdictTest._seed_look
    _resign_build = CycleVerdictTest._resign_build

    def _cycle(self, page, mode="delivery"):
        r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                            "--page", str(page), "--mode", mode],
                           capture_output=True, text=True, timeout=240)
        vp = page / "_check" / "verdict.json"
        doc = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else None
        return r, doc

    def test_five_booleans_and_no_numeric_field(self):
        with tempfile.TemporaryDirectory() as td:
            page, sp, out = self._prep(td)
            self._seed_look(page, out)
            r, v = self._cycle(page)
            self.assertIsNotNone(v, r.stdout + r.stderr)
            self.assertEqual(set(v), {"schema", "text_ok", "text_is_text", "in_canvas",
                                      "no_missing", "renders", "source",
                                      "deliverable", "deliverable_why"},
                             f"delivery 的键集合 = schema + 五条布尔 + source + 交付结论：{sorted(v)}")
            for k in ("text_ok", "text_is_text", "in_canvas", "no_missing", "renders"):
                self.assertIsInstance(v[k], bool, k)
            # io_gate 退役（第 ④ 步 4A）后 verdict 里**一个数字都不许有**：
            # 原来的 `mae` 是 io.json 的读数，唯一来源没了就整键删除，不留"永远 None"的占位。
            nums = [k for k, val in v.items()
                    if isinstance(val, (int, float)) and not isinstance(val, bool)]
            self.assertEqual(nums, [], f"verdict 里不许再有数字（mae 已随 io_gate 退役）：{nums}")
            self.assertIn("verdict: 5/5 通过", r.stdout, r.stdout[-400:])

    def test_boolean_set_is_exactly_five(self):
        """布尔键集合必须**恰好**是这 5 个：不许再往里加一条"听起来该查"的。

        `bg_parametric` 就是这么进来的：只认 `background.type=="solid"`，而本项目所有页
        都是渐变 → 这条在该场景下永远真空 PASS，看着 6/6 其实有一条从没执行过。
        2026-11-08 用户拍板删除。新增布尔的门槛是"有机器可判的证据"，
        不是"报告上多一行更安心"——没有证据的布尔就是假绿入口。
        """
        import sys
        sys.path.insert(0, str(ROOT / "scripts" / "delivery"))
        import verdict as vd
        self.assertEqual(set(vd.CHECK_KEYS),
                         {"text_ok", "text_is_text", "in_canvas", "no_missing", "renders"})
        v = vd.collect(look={"fails": []}, renders=True,
                       presence_ok=True, editability_ok=True)
        self.assertEqual({k for k, val in v.items() if isinstance(val, bool)},
                         set(vd.CHECK_KEYS), f"多出/少了布尔：{sorted(v)}")
        self.assertNotIn("bg_parametric", v, "背景没有机器证据，不许给它一个假布尔")

    def test_preview_is_marked_and_not_adjudicated(self):
        with tempfile.TemporaryDirectory() as td:
            page, sp, out = self._prep(td)
            self._seed_look(page, out)
            r, v = self._cycle(page, "preview")
            self.assertIsNotNone(v, r.stdout + r.stderr)
            self.assertEqual(v.get("mode"), "preview", "preview 必须自报模式，不能与裁决产物同形")
            # preview **不写**交付结论：它回答"还差什么"，不回答"能不能交付"。
            self.assertEqual(set(v) - {"mode"}, {"schema", "text_ok", "text_is_text",
                                                "in_canvas", "no_missing", "renders",
                                                "source"})
            self.assertNotIn("deliverable", v, "preview 不许写交付结论（delivery-only）")
            # preview 不跑 editability：source 要写明这条只由 look 支撑（不许假装验证过）
            self.assertIn("未跑 editability", v["source"]["text_is_text"])

    def test_deliverable_three_states(self):
        """交付结论三态是**代码可判的映射**，不是文案：崩=reject / 有阻塞=rework / 全绿=ready。

        它是五条布尔之外的**第二个出口**：五条布尔测不到"整块漏掉"与"报告没写完"
        （实测都能在 5/5 全绿下发生），所以必须有一条独立结论承接。

        第 ① 态「缺标定=rework」随 s5a 退役（第 ④ 步 4B）删除：逐块位置验证本身不存在了，
        留着这一态等于让每一页都判 rework、永远交不出去。这不是放宽，是没有被测对象。
        """
        import sys
        sys.path.insert(0, str(ROOT / "scripts" / "delivery"))
        import verdict as vd
        self.assertEqual(vd.deliverable_of("error", [])[0], "reject")
        self.assertEqual(vd.deliverable_of("blocked", [])[0], "rework")
        self.assertEqual(vd.deliverable_of(
            "ready", [{"stage": "presence", "message": "漏 3 块"}])[0], "rework")
        # 提示项（blocking=False）不许把 ready 拖成 rework
        self.assertEqual(vd.deliverable_of(
            "ready", [{"stage": "plan", "message": "提示", "blocking": False}]),
            ("ready", []))
        self.assertEqual(vd.deliverable_of("ready", []), ("ready", []))

    def test_missing_shape_turns_no_missing_false_and_is_named(self):
        """删掉一个 shape → no_missing 必须 false，且终端那一行点名它。

        变异自证：把 verdict.collect 里 `no_missing` 改成常量 True，
        本用例会在"false"这一步变红。
        """
        with tempfile.TemporaryDirectory() as td:
            page, sp, out = self._prep(td)
            from pptx import Presentation
            prs = Presentation(str(out))
            for sh in list(prs.slides[0].shapes):
                if sh.has_text_frame and sh.text_frame.text.strip():
                    sh._element.getparent().remove(sh._element)
                    break
            prs.save(str(out))
            self._resign_build(page, sp, out)   # 同上：缺块存在于"报告认过的成品"里
            self._seed_look(page, out)      # 改动后再种 look，避免真去渲染
            r, v = self._cycle(page)
            self.assertIsNotNone(v, r.stdout + r.stderr)
            self.assertFalse(v["no_missing"], f"整块缺失必须是 false：{v}")
            tail = r.stdout.strip().splitlines()[-1]
            self.assertTrue(tail.startswith("verdict: "), tail)
            self.assertIn("no_missing", tail.replace("假: ", " 假: ").split("假: ")[-1],
                          f"没通过的那条必须在终端点名：{tail}")
            doc = json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["state"], "blocked", doc["to_fix"])


class ResidualEdgeBandTest(unittest.TestCase):
    """P4-1：residual 的 10px 边缘带（S1 量框 ±10px 的机器余量）。

    反例：放宽量框精度后，渲染框边与参考差 10px → residual 判成「多」→
    每页 3~8 条假失败，于是"放宽"变成负收益（人被迫给每条假失败写豁免）。

    变异自证：
      * 把 residual.run 里的边缘带分支删掉（或 edge_band_px 置 0）→ 第一条变红；
      * 把核心区判据（`not _overlaps(bbox, _core_of(...))`）删掉 → 第三条变红
        （框内整块缺失会被当成边缘带吞掉）。
    """

    BOX = [100, 60, 290, 140]        # 声明框：190x80

    def _run(self, gap: int = 0, inner_miss: bool = False, band: int | None = None) -> dict:
        ref = Image.new("RGB", (320, 180), (255, 255, 255))
        ImageDraw.Draw(ref).rectangle([110, 70, 280, 130], fill=(0, 0, 0))
        ren = ref.copy()
        d = ImageDraw.Draw(ren)
        if inner_miss:                # 框内整块缺失：成品这块是平的
            d.rectangle([120, 80, 200, 120], fill=(255, 255, 255))
        else:                         # 框外 8px 厚的条带（1520px²）
            y0 = 140 + int(gap)
            d.rectangle([100, y0, 289, y0 + 7], fill=(0, 0, 0))
        cfg = dict(residual.DEFAULTS, topn=5, declared=[list(self.BOX)])
        if band is not None:
            cfg["edge_band_px"] = band
        with tempfile.TemporaryDirectory() as td:
            rp, npth = Path(td) / "ref.png", Path(td) / "ren.png"
            ref.save(rp)
            ren.save(npth)
            return residual.run(rp, npth, cfg, None)

    @staticmethod
    def _struct(res: dict) -> list[dict]:
        return [i for i in res["items"] if i["kind"] in ("漏", "多")]

    @unittest.skip(_SKIP_RESIDUAL)
    def test_fringe_8px_outside_is_absorbed(self):
        res = self._run(gap=8)
        self.assertEqual(self._struct(res), [], f"贴框边 8px 的差异不该报：{res['items']}")
        self.assertEqual(res["n_edge"], 1, f"要留在 edge_items 里可审计：{res}")
        self.assertTrue([n for n in res["notes"] if "边缘带" in n], res["notes"])

    @unittest.skip(_SKIP_RESIDUAL)
    def test_20px_outside_is_reported(self):
        res = self._run(gap=20)
        self.assertTrue(self._struct(res), f"挪到 20px 外必须照报：{res['items']}")
        self.assertEqual(res["n_edge"], 0, res["edge_items"])

    @unittest.skip(_SKIP_RESIDUAL)
    def test_in_box_block_is_not_absorbed(self):
        """核心区判据：框内的整块缺失是**真漏**，边缘带不许吞它。"""
        res = self._run(inner_miss=True)
        self.assertTrue(self._struct(res), f"框内整块缺失必须照报：{res['items']}")
        self.assertEqual(res["n_edge"], 0, f"不许被边缘带吸收：{res['edge_items']}")

    @unittest.skip(_SKIP_RESIDUAL)
    def test_band_width_is_in_dependency_key(self):
        """带宽固定、不进 CLI，但必须进依赖键：改宽 → 旧 residual 报告自动失效。"""
        self.assertEqual(residual.DEFAULTS["edge_band_px"], 10)
        self.assertEqual(cycle._res_cfg()["edge_band_px"], 10)
        self.assertEqual(gates._res_cfg(), cycle._res_cfg(),
                         "gates 与 cycle 的 residual 键配置必须同源")
        self.assertNotIn("--edge-band", (ROOT / "scripts" / "delivery" / "residual.py")
                         .read_text(encoding="utf-8"), "带宽不进 CLI")
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            ref, ren = page / "reference.png", page / "look" / "rendered.png"
            k10 = rs.key_residual(ref, ren, cycle._res_cfg())
            k4 = rs.key_residual(ref, ren, dict(cycle._res_cfg(), edge_band_px=4))
            self.assertNotEqual(k10, k4, "带宽变了，旧报告不许继续被当成 current")


class S5aConvergeTest(unittest.TestCase):
    """S5a（自动收敛）的合同：溢出闭式解 / 档位锁死 / 轮次上限 / 降字号隔离。

    每条都能被一次「看起来无害的简化」打红，逐条对应：
      - 闭式解不是判据的逆解 → 批过的又被降（自相矛盾）
      - 判据档位放成 fidelity → 3 轮烧在 1~2px 噪声上
      - 轮次做成 CLI 参数 → "改 3 轮就宣布完成" 变成 "改 N 轮"
      - 降过字号的块进 autocorrect 追宽比 → 溢出修好了、宽比继续烧
      - 降幅 >2pt 不转人工 → 机器替用户决定版式
    """

    DPI = 96.0

    # ------------------------------------------------------------ 夹具
    def _el(self, eid, text, bbox, size, **kw):
        el = {"id": eid, "type": "text", "text": text, "bbox": list(bbox),
              "size_pt": size, "color": "#000000"}
        el.update(kw)
        return el

    def _spec(self, elements):
        return {"canvas": {"w_px": 640, "h_px": 480, "dpi": self.DPI},
                "background": {"type": "solid", "color": "#FFFFFF"},
                "containers": [], "assets": [], "elements": elements}

    # ------------------------------------------------------------ ① 闭式解自洽
    def test_closed_form_is_the_inverse_of_the_overflow_criterion(self):
        """bound 必须是溢出判据的移项逆解：bound 处不溢出，bound+0.5 处溢出。

        这条是"批过的又被降"的唯一防线：两边解的是同一个不等式，结论不可能互相打脸。
        """
        el = self._el("t1", "十个字的标题在这里", [100, 100, 313, 130], 20)
        got = spec_lint.overflow_of(el, self.DPI)
        self.assertIsNotNone(got, "构造的元素必须真的溢出（否则用例什么都没证明）")
        bound = got["bound"]
        self.assertIsNotNone(bound)

        at = dict(el, size_pt=bound)
        self.assertIsNone(spec_lint.overflow_of(at, self.DPI),
                          f"size=bound({bound:.2f}) 必须判不溢出")
        above = dict(el, size_pt=bound + 0.5)
        self.assertIsNotNone(spec_lint.overflow_of(above, self.DPI),
                             f"size=bound+0.5({bound + 0.5:.2f}) 必须仍判溢出（解是真的边界）")

    def test_bound_is_tighter_than_the_fail_tolerance(self):
        """1.02 是墨宽测量的噪声容差，不是版式余量：bound 必须比 FAIL 线更小。

        实测依据（page06 的 ct4，框由 241 缩到 205）：按 FAIL 线解 21.96→21.9pt 渲染
        还是 2 行，按 bound 解 21.51→21.5pt 才是 1 行。中文按字折行是离散的，
        大 2% 就一定挤掉一个字。
        """
        el = self._el("t1", "浸润性宫颈癌", [1327, 303, 1532, 342], 23.4)
        got = spec_lint.overflow_of(el, 125.4)
        self.assertIsNotNone(got)
        fail_line = got["have"] * 1.02 / (got["adv_eff"] * (125.4 / 72.0))
        self.assertLess(got["bound"], fail_line,
                        f"bound({got['bound']:.2f}) 必须比 FAIL 线上限({fail_line:.2f}) 更小")

    # ------------------------------------------------------------ ② 档位锁死（变异点 1）
    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_verify_profile_is_locked_to_harmony(self):
        """变异自证：把锁改成 fidelity / 让它跟着 build 的 --profile 走 → 本用例必红。

        fidelity（dy±2 / 宽比 2%）是「追 1~2px 渲染噪声」的唯一来源；S5a 的 3 轮预算
        花在那上面，结构问题（溢出/换行）一轮都轮不到。
        """
        p = Path("/tmp/x")
        cmd = cycle.verify_cmd(p, p, p, p, p)
        self.assertEqual(cycle.S5A_PROFILE, "harmony", "S5a 判据档位必须是 harmony")
        self.assertIn("--profile", cmd)
        self.assertEqual(cmd[cmd.index("--profile") + 1], "harmony")
        self.assertNotIn("fidelity", cmd)
        # 不许有"把 build 的 profile 传进来"的入口：多一个参数就多一次配错的机会
        self.assertNotIn("profile", inspect.signature(cycle.verify_cmd).parameters,
                         "verify_cmd 不许接受 profile 形参（否则用户能把它调成 fidelity）")

    # ------------------------------------------------------------ ③ 轮次上限
    @unittest.skip("s5a 轮次机制已退役（2.42.0）：S5A_ROUNDS 与收敛循环一起删除")
    def test_round_cap_is_one_and_not_a_cli_flag(self):
        """轮数上限是承诺的范围，不是调参项；做成 CLI 就等于允许无限拖。

        2026-11-08 由 3 降到 1：41 页实测第 1 轮即过，第 2、3 轮只在"改完又摆回去"时起作用。
        """
        self.assertEqual(cycle.S5A_ROUNDS, 1)
        with redirect_stdout(io.StringIO()) as buf:
            with self.assertRaises(SystemExit):
                sys.argv = ["cycle.py", "--help"]
                cycle.main()
        self.assertNotIn("--rounds", buf.getvalue())
        self.assertNotIn("--max-rounds", buf.getvalue())

    # ------------------------------------------------------------ ④ 降幅 >2pt 转人工（变异点 2）
    def test_drop_over_2pt_goes_manual(self):
        """变异自证：删掉 `drop > MAX_SIZE_DROP_PT` 这段 → 本用例必红。

        肉眼可辨的变小属版式决策，机器不许替用户定（S5b）。
        """
        self.assertEqual(cycle.MAX_SIZE_DROP_PT, 2.0)
        # 宽 150 / 20pt / 9 字：bound≈13.5pt，降幅≈6.4pt —— 远超 2pt
        el = self._el("t_b1", "十个字的标题在这里", [100, 100, 250, 130], 20)
        got = cycle.solve_overflow(self._spec([el]), self.DPI)
        self.assertEqual(got["sizes"], {}, "降幅 >2pt 不许自动写 override")
        self.assertTrue(got["manual"], "必须交人工并留下一条 manual")
        why = got["manual"][0]["why"]
        self.assertIn("S5b", why)
        self.assertIn("6.5", why, "要说清降了多少 pt")

    def test_drop_within_2pt_is_applied(self):
        """同一判据的另一侧：降幅 0.8pt → 自动降（否则这条规则会把正常页也拦下）。"""
        el = self._el("t_b1", "十个字的标题在这里", [100, 100, 313, 130], 20)
        got = cycle.solve_overflow(self._spec([el]), self.DPI)
        self.assertTrue(got["sizes"], "降幅 ≤2pt 必须自动修掉")
        new = got["sizes"]["t_b1"]
        self.assertAlmostEqual(new, 19.2, places=1)
        self.assertGreater(new, 20.0 - cycle.MAX_SIZE_DROP_PT - 0.15)

    # ------------------------------------------------------------ ⑤ 整组一起降
    def test_group_is_lowered_together_and_stays_consistent(self):
        """只降溢出的那一块、同簇邻块不动 → spec_lint 的极差判据立刻 FAIL。

        所以降字号必须按 spec_lint 的同层 cluster 整组降。降完还要能过 ②。
        """
        bad = self._el("t_b1", "十个字的标题在这里", [100, 100, 313, 130], 20)
        ok = self._el("t_b2", "短", [323, 100, 450, 130], 20)
        view = self._spec([bad, ok])
        got = cycle.solve_overflow(view, self.DPI)
        self.assertEqual(set(got["sizes"]), {"t_b1", "t_b2"},
                         "同簇两块必须一起降，否则极差判据 FAIL")
        self.assertEqual(got["sizes"]["t_b1"], got["sizes"]["t_b2"], "整组降到同一个字号")
        # 降完：溢出没了，且同层字号一致（② 与 ④ 同时成立）
        for el in view["elements"]:
            el["size_pt"] = got["sizes"][el["id"]]
        self.assertIsNone(spec_lint.overflow_of(view["elements"][0], self.DPI))
        sizes = [e["size_pt"] for e in view["elements"]]
        self.assertLessEqual(max(sizes) - min(sizes), 0.6, "降完同层极差不许超 0.6pt")

    def test_two_groups_advise_split_but_do_not_lower(self):
        """≥2 组要降 = 版式密度超了：出「建议拆页/加卡片」，不继续降。

        与 assetcheck「一页走豁免 ≥3 = 策略错」同一判据思路。
        """
        a = self._el("t_b1", "十个字的标题在这里", [100, 100, 313, 130], 20)
        b = self._el("t_b2", "另一段十个字的正文", [100, 300, 313, 330], 20)
        got = cycle.solve_overflow(self._spec([a, b]), self.DPI)
        self.assertIn("拆页", got["advice"])
        self.assertEqual(got["sizes"], {}, "密度问题不许靠继续降字号掩盖")
        self.assertEqual(len(got["manual"]), 2)

    # ------------------------------------------------------------ ⑥ 降字号隔离
    @unittest.skip("cycle.bad_items 已退役（2.42.0）：它消费 verify.json 的 items，verify 删除后无生产者")
    def test_width_only_residual_on_lowered_block_is_not_blocking(self):
        """降过字号的块只剩宽比 FAIL → 不算不达标（只进 S6 第四行点名）。

        不隔离的话：降字号必然把宽比推开（−10% 宽度 vs harmony 容差 4%），
        3 轮会从「溢出」转移到「宽比」继续烧，而宽比是视觉接近度、契约 1 不追。
        但**位置/换行**仍然算 —— 那两项能修，放过它们就是假装收敛。
        """
        wide = {"id": "ct4", "ok": False, "fails": ["L0:宽比0.936"], "ratio": 0.936}
        shifted = {"id": "ct4", "ok": False, "fails": ["位置偏-6.5"], "ratio": 1.0}
        err = {"id": "ct4", "ok": False, "err": "行数不一致 ref=1 ren=2"}
        other = {"id": "b1", "ok": False, "fails": ["L0:宽比0.80"], "ratio": 0.8}
        self.assertEqual(cycle.bad_items([wide], {"ct4"}), [], "只剩宽比 → 不算不达标")
        self.assertEqual(len(cycle.bad_items([shifted], {"ct4"})), 1, "位置偏仍然算")
        self.assertEqual(len(cycle.bad_items([err], {"ct4"})), 1, "换行（err）仍然算")
        self.assertEqual(len(cycle.bad_items([other], {"ct4"})), 1,
                         "没降过字号的块，宽比照样算")

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_skip_ids_keeps_position_but_drops_width_channels(self):
        """autocorrect --skip-ids：位置照做，粗细/字距不做。

        只锁"宽度类"两项。第一版把整块都跳过，结果降字号后墨迹位置必然变了却没人补，
        页面直接 blocked（实测 ct4 位置偏 -6.5 一直挂着）。
        """
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            spec = json.loads(sp.read_text(encoding="utf-8"))
            spec["elements"][0].update({"text": "行一\n行二", "size_pt": 20})
            write_json(sp, spec)
            vf = {"items": [{
                "id": "t1", "ok": False, "lines": 2, "ratio": 0.90,
                "dy": 0, "dx": 9, "dc": 9, "dens_ratio": 1.0, "limit": False,
                "pitch_ref": 40, "pitch_ren": 40,
                "rows": [{"line": 0, "dy": 0, "dx": 9, "dc": 9, "ratio": 0.9,
                          "ref_w": 200, "ren_w": 180, "ink_h": 20},
                         {"line": 1, "dy": 0, "dx": 9, "dc": 9, "ratio": 0.9,
                          "ref_w": 200, "ren_w": 180, "ink_h": 20}]}]}
            vjson = page / "verify.json"
            write_json(vjson, vf)

            def run(*extra):
                r = subprocess.run([PY, str(ROOT / "scripts" / "autocorrect.py"),
                                    "--spec", str(sp), "--verify", str(vjson), *extra],
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                ov = json.loads((page / "override.json").read_text(encoding="utf-8"))
                return (ov.get("elements") or {}).get("t1") or {}

            locked = run("--skip-ids", "t1")
            self.assertIn("bbox", locked, "位置必须照做（降字号后墨迹位置必然变了）")
            self.assertNotIn("letter_spacing_pt", locked, "字距追的就是宽比，必须锁")
            self.assertNotIn("weight", locked, "粗细也改宽度，必须锁")

            free = run()                     # 不传 --skip-ids：宽度类必须被修正
            # 2026-11-08 起宽度类的主通道是**字号**（ratio 0.90 → size 20→22，字距保持 0），
            # 只有字号那条路走不通时才会动字距。所以这里验的是"宽度类被修过"，
            # 不是"必须写字距"——后者会把"字号优先"这条新规则判红。
            self.assertTrue({"letter_spacing_pt", "size_pt"} & set(free),
                            f"没锁的块宽度类必须照修（字号或字距）：{free}")

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_autocorrect_keeps_meta_block(self):
        """override 里的 _meta（素材来源摘要）不许被 diff 冲掉。

        冲掉 → 下次 cycle 判「素材来源未记录」，所有素材要求重新核验（实测 page06
        的 override 正是 {assets, _meta} 这种形状）。
        """
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            meta = {"asset_sources": {"a1": {"key": "k", "src_md5": "m", "adopted": True}}}
            write_json(page / "override.json", {"_meta": meta})
            vjson = page / "verify.json"
            write_json(vjson, {"items": [{"id": "t1", "ok": True, "limit": False,
                                          "lines": 1, "rows": [{"ref_w": 10}]}]})
            r = subprocess.run([PY, str(ROOT / "scripts" / "autocorrect.py"),
                                "--spec", str(sp), "--verify", str(vjson)],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            ov = json.loads((page / "override.json").read_text(encoding="utf-8"))
            self.assertEqual(ov.get("_meta"), meta, "_meta 必须原样带过去")

    # ------------------------------------------------------------ ⑦ 写字号只动 size_pt
    def test_write_size_override_touches_only_size_pt(self):
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            sp = page / "spec.json"
            write_json(page / "override.json", {"assets": {"i1": {"src": "assets/i1.png"}},
                                                "_meta": {"asset_sources": {"i1": {}}}})
            cycle.write_size_override(sp, {"t1": 12.5})
            ov = json.loads((page / "override.json").read_text(encoding="utf-8"))
            self.assertEqual(ov["elements"]["t1"], {"size_pt": 12.5})
            self.assertEqual(ov["assets"], {"i1": {"src": "assets/i1.png"}})
            self.assertIn("_meta", ov)
            # spec.json 只读：extract 重跑不会冲掉收敛值
            self.assertEqual(json.loads(sp.read_text(encoding="utf-8"))["elements"][0]["size_pt"],
                             18)

    # ------------------------------------------------------------ ⑧ 接进 cycle 的位置与可见性
    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_s5a_autofills_floor_and_runs_between_look_and_presence(self):
        """没有 tolerance.json 时 S5a **自动补零噪声地板并照常跑**，且终端留 NOTE。

        旧行为是 skipped —— 而 skipped 的后果是"5/5 全绿但逐块位置一次没验"。
        同时钉住位置：S5a 必须在 look 之后、presence 之前 —— 收敛换了成品，
        下游必须按新成品重算，否则裁决读的是上一版。
        """
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            # 本用例的前提就是"没有 tolerance.json"：make_page 现在默认会造一份标定文件
            # （缺标定 → deliverable=rework），这里必须显式删掉，否则测的就不是这条路径了。
            (page / "tolerance.json").unlink(missing_ok=True)
            sp = page / "spec.json"
            out = build_pptx(page)
            rs.write_report(page / "_check" / "build.json",
                            {"stage": "build", "kind": "build", "outcome": "pass",
                             "pptx": str(out), "pptx_md5": rs.file_digest(out),
                             "profile": "harmony", "out_size": [320, 180]},
                            rs.key_build(sp, out, 320, 180))
            png = page / "look" / "rendered.png"
            (page / "look" / "look.json").write_text(json.dumps({
                "pptx_md5": rs.file_digest(out)[:16], "png_md5": rs.file_digest(png)[:16],
                # B1 之后复用键含判据源码指纹：夹具要模拟"对当前成品+渲图+判据都有效"的报告
                "dependency_key": rs.key_look(sp, out, png),
                "render_secs": 0.0, "mae": 0.0, "changed": 0.0, "fails": []}),
                encoding="utf-8")
            self.assertFalse((page / "tolerance.json").exists())
            r = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "cycle.py"),
                                "--page", str(page), "--mode", "preview"],
                               capture_output=True, text=True, timeout=180)
            doc = json.loads((page / "_check" / "cycle.json").read_text(encoding="utf-8"))
            names = [p["name"] for p in doc["phases"]]
            # 2026-11-08 行为变更：缺 tolerance **不再跳过**，而是自动补一份零噪声地板后
            # 照常跑 s5a（cycle.py:721-736）。跳过 = 「逐块位置一次没验」，那才是假绿；
            # 补的 floor 全 0 是最严取值，只会更紧不会放宽，但文件里要写明"未经标定"。
            self.assertTrue((page / "tolerance.json").exists(), "缺标定必须被自动补上")
            self.assertIn("不代表本页已标定", (page / "tolerance.json").read_text(encoding="utf-8"))
            rounds = [i for i, n in enumerate(names) if n.startswith("s5a:r")]
            self.assertTrue(rounds, f"缺 s5a 实跑：{names}")
            self.assertLess(names.index("look"), rounds[0], "S5a 必须在 look 之后")
            self.assertLess(rounds[0], names.index("presence"), "S5a 必须在 presence 之前")
            self.assertEqual(doc["renders"]["normal"], 0, "全过的页不许因为 S5a 多渲一次")
            self.assertIn("已补零噪声地板", r.stdout + r.stderr, "终端必须留一行，别让人以为已标定")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class NativeCapabilityTest(unittest.TestCase):
    """三样原生能力：`rotation` / 原生表格 / 连接线。**只读回 OOXML，不看画得像不像。**

    为什么必须读回而不是看图：旋转 15° 与 345° 的缩略图几乎一样；"文字框 + 几条线"拼的
    假表格与真表格在渲染图上无法区分。只有 `a:xfrm@rot` / `p:graphicFrame` / `p:cxnSp`
    这三个机器可读的事实能证明能力真的落到了成品里。
    """

    @staticmethod
    def _inv_row(eid: str, section: str, evidence: str) -> dict:
        return {"id": eid, "section": section, "kind": "native", "reason": "native-simple",
                "decided_by": "auto", "evidence": evidence, "approx_of": None,
                "gaps": [], "crop_note": None, "locked": False, "locked_reason": None}

    def _page(self, td, containers, elements=None) -> Path:
        """在 make_page 的最小页上追加容器/文字，并补齐 inventory（plan 闸要求逐块有记录）。"""
        page = make_page(Path(td))
        spec = json.loads((page / "spec.json").read_text(encoding="utf-8"))
        spec["containers"] = containers
        if elements:
            spec["elements"] += elements
        write_json(page / "spec.json", spec)
        inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
        inv["items"] += [self._inv_row(c["id"], "containers",
                                       f"shape={c.get('shape') or c.get('type')}") for c in containers]
        inv["items"] += [self._inv_row(e["id"], "elements", f"text={e['text']}")
                         for e in (elements or [])]
        write_json(page / "inventory.json", inv)
        return page

    def test_rotation_round_trip_equals_written_value(self):
        """写入 15.5° 读回必须还是 15.5（不是"看起来转了"）；负角归一化到 0~360。"""
        from pptx import Presentation
        for written, expected in ((15.5, 15.5), (350, 350.0), (-15, 345.0)):
            with tempfile.TemporaryDirectory() as td:
                page = self._page(td, [{"id": "c1", "type": "shape", "shape": "rect",
                                        "bbox": [10, 10, 100, 60], "fill": "#CCCCCC",
                                        "rotation": written}])
                prs = Presentation(str(build_pptx(page)))
                got = {s.name: s for s in prs.slides[0].shapes}["c1"]
                self.assertAlmostEqual(got.rotation, expected, places=6,
                                       msg=f"rotation={written} 读回 {got.rotation}")

    def test_rotation_absent_keeps_zero(self):
        """不写 rotation 的元素必须仍是 0——否则历史页面会被"顺手转一下"。"""
        from pptx import Presentation
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, [{"id": "c1", "type": "shape", "shape": "rect",
                                    "bbox": [10, 10, 100, 60], "fill": "#CCCCCC"}])
            prs = Presentation(str(build_pptx(page)))
            got = {s.name: s for s in prs.slides[0].shapes}["c1"]
            self.assertEqual(got.rotation, 0)

    def test_table_is_a_real_graphic_frame(self):
        """必须是 p:graphicFrame/a:tbl（真表格），不是文字框拼的格线。"""
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        from pptx.util import Emu
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, [{
                "id": "tb1", "type": "table", "bbox": [20, 20, 300, 160],
                "rows": 2, "cols": 3,
                "cells": [["项目", "A", "B"], ["值", "1", "2"]],
                "header": True, "size_pt": 12, "color": "#112233",
                "col_w": [160, 60, 60], "row_h": [40, 60], "borders": False}])
            prs = Presentation(str(build_pptx(page)))
            frames = [s for s in prs.slides[0].shapes if s.shape_type == MSO_SHAPE_TYPE.TABLE]
            self.assertEqual(len(frames), 1, "成品里没有表格（graphicFrame）")
            gf = frames[0]
            self.assertTrue(gf._element.tag.endswith("graphicFrame"), gf._element.tag)
            tbl = gf.table
            # 行列数 / 单元格文本
            self.assertEqual((len(tbl.rows), len(tbl.columns)), (2, 3))
            self.assertEqual(tbl.cell(0, 0).text, "项目")
            self.assertEqual(tbl.cell(1, 2).text, "2")
            # 表头加粗只作用于首行；字号/字色按 spec
            r_head = tbl.cell(0, 1).text_frame.paragraphs[0].runs[0]
            r_body = tbl.cell(1, 1).text_frame.paragraphs[0].runs[0]
            self.assertTrue(r_head.font.bold, "header:true 时首行必须加粗")
            self.assertFalse(r_body.font.bold, "非表头行不许跟着加粗")
            self.assertEqual(r_head.font.size.pt, 12.0)
            self.assertEqual(str(r_head.font.color.rgb), "112233")
            self.assertEqual(r_head.font.name, "微软雅黑", "表格不许掉回宋体")
            # 行高 / 列宽按 spec（像素 @ dpi 96）
            self.assertEqual(tbl.columns[0].width, Emu(round(160 / 96.0 * 914400)))
            self.assertEqual(tbl.rows[1].height, Emu(round(60 / 96.0 * 914400)))

    def test_table_has_no_border_by_default(self):
        """默认无框线 = **显式指定 No-Style-No-Grid 样式**，且 `tbl` 内 `ln*` 节点数 = 0。

        实测（2026-11-08，PowerPoint 导 1600×900 PNG、空单元格以隔离文字）：
          · 只移除 `<a:tableStyleId>` **不够**——缺样式会回落到 Default Table Style，
            两行之间的分隔线照旧（分界线上暗像素 1350，与 borders:true 一模一样）；
          · 显式指定内置样式 No Style, No Grid → 表内暗像素 **0**，`ln*` 节点 **0**。
        所以"默认无框"是一个**主动声明**（选无框样式），不是"什么都不写"。
        """
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        from pptx.oxml.ns import qn
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, [{
                "id": "tb1", "type": "table", "bbox": [20, 20, 300, 160],
                "rows": 2, "cols": 2, "cells": [["a", "b"], ["c", "d"]],
                "size_pt": 18}])
            prs = Presentation(str(build_pptx(page)))
            gf = [s for s in prs.slides[0].shapes if s.shape_type == MSO_SHAPE_TYPE.TABLE][0]
            tblPr = gf.table._tbl.tblPr
            self.assertEqual(tblPr.find(qn("a:tableStyleId")).text,
                             "{2D5ABB26-0587-4C30-8999-92F81FD0307C}",
                             "默认必须显式选 No Style, No Grid")
            self.assertEqual(tblPr.get("firstRow"), "0")
            self.assertEqual(tblPr.get("bandRow"), "0")
            lines = gf.table._tbl.xpath(".//a:lnL | .//a:lnR | .//a:lnT | .//a:lnB")
            self.assertEqual(len(lines), 0, f"默认无框线时不许有 ln* 节点，实有 {len(lines)} 个")

    def test_table_borders_true_draws_explicit_lines(self):
        """borders:true 才画框线，而且是显式的 ln*（不依赖主题）。"""
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        from pptx.oxml.ns import qn
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, [{
                "id": "tb1", "type": "table", "bbox": [20, 20, 300, 160],
                "rows": 2, "cols": 2, "cells": [["a", "b"], ["c", "d"]],
                "size_pt": 18,
                "borders": True, "border_color": "#808080", "border_pt": 1.5}])
            prs = Presentation(str(build_pptx(page)))
            gf = [s for s in prs.slides[0].shapes if s.shape_type == MSO_SHAPE_TYPE.TABLE][0]
            tbl = gf.table
            self.assertIsNotNone(tbl._tbl.tblPr.find(qn("a:tableStyleId")), "样式引用要在")
            lines = tbl._tbl.xpath(".//a:lnL | .//a:lnR | .//a:lnT | .//a:lnB")
            self.assertEqual(len(lines), 4 * 2 * 2, "每个格子四条边")
            self.assertEqual(lines[0].find(qn("a:solidFill")).find(qn("a:srgbClr")).get("val"),
                             "808080")

    def test_line_dash_and_arrow_round_trip(self):
        """虚实与箭头端点必须落进 `a:prstDash` / `a:headEnd` / `a:tailEnd`，且**读回等于写入值**。

        不许只看渲染图"像不像虚线"：`prstDash@val` 与 `headEnd@type` 是机器可读的唯一事实。
        取值直接采用 OOXML 原值（不造别名），所以断言可以做相等比较。
        """
        from pptx import Presentation
        from pptx.oxml.ns import qn
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, [{
                "id": "l1", "type": "shape", "shape": "line", "bbox": [20, 120, 280, 120],
                "line": {"color": "#FF0000", "width_pt": 2, "dash": "lgDashDotDot",
                         "head": {"type": "triangle", "w": "med", "len": "lg"},
                         "tail": "stealth"}}])
            prs = Presentation(str(build_pptx(page)))
            shp = {s.name: s for s in prs.slides[0].shapes}["l1"]
            ln = shp._element.spPr.find(qn("a:ln"))
            self.assertEqual(ln.find(qn("a:prstDash")).get("val"), "lgDashDotDot")
            head = ln.find(qn("a:headEnd"))
            self.assertEqual((head.get("type"), head.get("w"), head.get("len")),
                             ("triangle", "med", "lg"))
            tail = ln.find(qn("a:tailEnd"))
            self.assertEqual((tail.get("type"), tail.get("w"), tail.get("len")),
                             ("stealth", "med", "med"), "只写类型时 w/len 取 med")
            # 子元素顺序必须合法（schema）：填充 → 虚实 → 起点箭头 → 终点箭头
            self.assertEqual([ch.tag.split("}")[-1] for ch in ln],
                             ["solidFill", "prstDash", "headEnd", "tailEnd"])
            self.assertEqual(str(shp.line.color.rgb), "FF0000")
            self.assertAlmostEqual(shp.line.width.pt, 2.0, places=3)

    def test_bad_dash_and_arrow_are_refused_with_human_message(self):
        """非法取值：**plan 闸与 build 都点名报错**，无 traceback、不留半成品 pptx。

        两道都要拦：plan 闸（spec_lint）与 build 共用 consts.parse_line_style，所以同一句话。
        只在 build 拦是不够的——那会变成"闸绿着、构建红掉"，正是同一份 spec 两处结论不一致。
        """
        for bad, must in (({"dash": "wavy"}, "line.dash"),
                          ({"head": "triangle9"}, "line.head")):
            # ① plan 闸（spec_lint）进门口就拦
            with tempfile.TemporaryDirectory() as td:
                page = self._page(td, [{
                    "id": "l1", "type": "shape", "shape": "line", "bbox": [20, 120, 280, 120],
                    "line": {"color": "#FF0000", "width_pt": 2, **bad}}])
                plan = gate(page, "plan")
                self.assertNotEqual(plan.returncode, 0, f"坏值必须在 plan 阶段就拦：{bad}")
                self.assertIn(must, plan.stdout + plan.stderr, f"要点名到键：{bad}")
            # ② build 自己也要拦（用没有 inventory 的页绕开 plan 闸，只看它那一句）
            with tempfile.TemporaryDirectory() as td:
                d = Path(td) / "page"
                d.mkdir(parents=True)
                write_json(d / "spec.json", {
                    "canvas": {"w_px": 320, "h_px": 180, "dpi": 96.0,
                               "width_in": 3.3333, "height_in": 1.875},
                    "containers": [{"id": "l1", "type": "shape", "shape": "line",
                                    "bbox": [20, 120, 280, 120],
                                    "line": {"color": "#FF0000", "width_pt": 2, **bad}}],
                    "assets": [], "elements": []})
                out = d / "bad.pptx"
                r = subprocess.run([PY, str(ROOT / "scripts" / "build.py"),
                                    "--spec", str(d / "spec.json"), "--out", str(out)],
                                   capture_output=True, text=True)
                both = r.stdout + r.stderr
                self.assertNotEqual(r.returncode, 0, both)
                self.assertIn("REFUSED", both)
                self.assertIn(must, both, f"报错要点名是哪个键：{both}")
                self.assertNotIn("Traceback", both, f"不许 traceback：{both}")
                self.assertFalse(out.exists(), "不许留半成品 pptx")

    def test_bad_dash_reported_by_lint_and_build_in_the_same_sentence(self):
        """同一份 spec 的坏线型，plan 闸（spec_lint）与 build 必须报**同一句**。

        只查"键在不在白名单"会留一道窄缝：`dash:"wavy"` 在闸这里绿着、到 build 才红，
        正是"同一份 spec 两处结论不一致"。校验器只有 consts.parse_line_style 一处，
        所以两边逐字相同——这条断言的就是那个"逐字相同"。
        """
        line = {"color": "#FF0000", "width_pt": 2, "dash": "wavy"}
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, [{"id": "l1", "type": "shape", "shape": "line",
                                    "bbox": [20, 120, 280, 120], "line": dict(line)}])
            lint = subprocess.run([PY, str(ROOT / "scripts" / "delivery" / "spec_lint.py"),
                                   "--spec", str(page / "spec.json")],
                                  capture_output=True, text=True)
            self.assertNotEqual(lint.returncode, 0, lint.stdout + lint.stderr)
            from_lint = re.search(r"line\.dash=.*", lint.stdout)
            self.assertIsNotNone(from_lint, f"spec_lint 没点名：{lint.stdout}")
        # build 侧不写 inventory，避免先撞 plan 闸——这里只看它自己那句
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "page"
            d.mkdir(parents=True)
            write_json(d / "spec.json", {
                "canvas": {"w_px": 320, "h_px": 180, "dpi": 96.0,
                           "width_in": 3.3333, "height_in": 1.875},
                "containers": [{"id": "l1", "type": "shape", "shape": "line",
                                "bbox": [20, 120, 280, 120], "line": dict(line)}],
                "assets": [], "elements": []})
            out = d / "o.pptx"
            b = subprocess.run([PY, str(ROOT / "scripts" / "build.py"),
                                "--spec", str(d / "spec.json"), "--out", str(out)],
                               capture_output=True, text=True)
            both = b.stdout + b.stderr
            self.assertNotEqual(b.returncode, 0, both)
            self.assertIn("REFUSED", both)
            self.assertNotIn("Traceback", both, both)
            self.assertFalse(out.exists(), "不许留半成品 pptx")
            from_build = re.search(r"line\.dash=.*", both)
            self.assertIsNotNone(from_build, both)
        self.assertEqual(from_lint.group(0).strip(), from_build.group(0).strip(),
                         "spec_lint 与 build 必须是同一句话")

    def test_line_is_a_connector_not_a_rectangle(self):
        """`shape: line` 必须是 p:cxnSp（端点可拖），不是被兜底成的圆角矩形。"""
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, [{"id": "l1", "type": "shape", "shape": "line",
                                    "bbox": [20, 120, 280, 120],
                                    "line": {"color": "#FF0000", "width_pt": 2}}])
            prs = Presentation(str(build_pptx(page)))
            shp = {s.name: s for s in prs.slides[0].shapes}["l1"]
            self.assertTrue(shp._element.tag.endswith("cxnSp"),
                            f"线落成了 {shp._element.tag}，不是 cxnSp")
            self.assertEqual(shp.shape_type, MSO_SHAPE_TYPE.LINE)
            self.assertEqual(str(shp.line.color.rgb), "FF0000")
            self.assertAlmostEqual(shp.line.width.pt, 2.0, places=3)

    def test_presence_sees_the_new_native_objects(self):
        """三样新对象都要能被 presence 认出来（认不出的元素等于"没画"）。"""
        import gates as g
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td, [
                {"id": "tb1", "type": "table", "bbox": [20, 20, 300, 100],
                 "rows": 1, "cols": 2, "cells": [["a", "b"]], "size_pt": 18},
                {"id": "l1", "type": "shape", "shape": "line",
                 "bbox": [20, 150, 280, 150], "line": {"color": "#FF0000", "width_pt": 2}},
            ])
            out = build_pptx(page)
            errs, _ = g.check_presence(page, page / "spec.json", out)
            self.assertEqual(errs, [], f"presence 没认出新的原生对象：{errs}")


class AssetUncoveredTest(unittest.TestCase):
    """assetcheck 的第三态：core 不在本素材身上 → UNCOVERED（不通过、也不算素材坏）。

    背景（2026-11-08）：旧写法 `cover = ... if core.sum() > 200 else 1.0` 两头都错——
    core 是空集时判 1.0（假绿）；淡色主体（浅蓝烟雾压浅底）的 core 落在框底的邻元素
    （香烟）上时 cover≈0 → FAIL（假红）。这一组用例锁住新三态，并锁住"α 清零仍 FAIL"。
    """

    @staticmethod
    def _scene(alpha_zero: bool) -> tuple[dict, "np.ndarray", Path]:
        import tempfile
        d = Path(tempfile.mkdtemp())
        ref_img = Image.new("RGB", (320, 180), (240, 240, 240))
        ImageDraw.Draw(ref_img).rectangle([60, 120, 279, 149], fill=(40, 40, 40))  # 框底的"邻元素"
        ref = np.asarray(ref_img).astype(np.int16)
        a = np.zeros((140, 240, 4), np.uint8)
        a[:, :, :3] = (200, 225, 245)          # 淡蓝"烟雾"
        a[0:60, :, 3] = 0 if alpha_zero else 255
        Image.fromarray(a).save(d / "a.png")
        el = {"id": "smoke", "type": "image", "mask": "feather",
              "bbox": [50, 20, 290, 160], "src": "a.png", "feather_px": [2, 2, 2, 2]}
        return el, ref, d

    def test_zero_alpha_is_still_fail_not_uncovered(self):
        """素材 α 全清零必须仍判 FAIL——否则"真缺件"会被降级成"未覆盖"，闸门等于漏了。"""
        import assetcheck as ac
        el, ref, d = self._scene(alpha_zero=True)
        r = ac.check_one(el, {"assets": [], "elements": []}, dict(ac.DEFAULTS), d)
        self.assertEqual(r.get("status"), "fail", r)
        self.assertFalse(r.get("ok"), r)
        self.assertTrue(any("不透明像素" in f for f in (r.get("fails") or [])), r)


class AutocorrectSizeActionTest(unittest.TestCase):
    """字号必须是宽比偏差的**主通道**，不能继续靠字距凑（2026-11-08 结构修）。

    反例：P07 的 t_title 字号偏小约 6%（宽比 0.94），旧动作空间里没有字号，
    于是被加了 2.97pt/字 = 41.5px ≈ 773px 块的 5.35% 字距 —— 指标回到 1.000，
    字形还是错的，行内还多出 5.35% 空白。这是"看起来收敛"的假收敛。
    """

    def _el(self, **kw):
        el = {"id": "t1", "type": "text", "text": "哪些女性风险更高？",
              "bbox": [100, 100, 800, 180], "size_pt": 49.5, "weight": "bold",
              "color": "#02288D"}
        el.update(kw)
        return el

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_ratio_below_one_grows_size_by_inverse(self):
        """ratio 是"渲染/参考"：渲染偏窄（0.94）要**放大**字号，所以是 ÷ratio 不是 ×ratio。"""
        import autocorrect as ac
        p = ac.size_plan(self._el(), {"id": "t1", "ratio": 0.94}, 125.4)
        self.assertTrue(p["eligible"], p)
        self.assertAlmostEqual(p["to"], round(49.5 / 0.94, 2), places=2)
        self.assertGreater(p["to"], 49.5, "偏窄必须放大字号")

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_single_round_step_is_capped_at_ten_percent(self):
        """ratio 0.80（差 20%）不许一步到位：单轮上限 ±10%。"""
        import autocorrect as ac
        p = ac.size_plan(self._el(), {"id": "t1", "ratio": 0.80}, 125.4)
        self.assertAlmostEqual(p["to"], round(49.5 * 1.10, 2), places=2)

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_deadband_reports_eligible_but_no_action(self):
        """2% 死区里不动字号；但 eligible=True —— 调用方据此**不许**回落到字距。"""
        import autocorrect as ac
        p = ac.size_plan(self._el(), {"id": "t1", "ratio": 0.99}, 125.4)
        self.assertTrue(p["eligible"], p)
        self.assertIsNone(p["to"], p)

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_spacing_already_used_blocks_size_action(self):
        """字距已被用来凑过宽比（>0.02em）时不动字号，并说明要先清字距。"""
        import autocorrect as ac
        el = self._el(letter_spacing_pt=2.97)     # 2.97pt / 49.5pt = 0.06em
        p = ac.size_plan(el, {"id": "t1", "ratio": 0.94}, 125.4)
        self.assertFalse(p["eligible"], p)
        self.assertIsNone(p["to"], p)
        self.assertIn("字距", p["reason"])

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_disagreeing_lines_block_size_action(self):
        """多行块各行"要的字号"不一致时不许动字号（字号是块的属性，不能一行一个）。

        实测反例（P07 的 b5）：L1 与 L2 的宽比差 5%，按 L2 放大 3% 把 L1 推到宽比 1.053 FAIL。
        """
        import autocorrect as ac
        it = {"id": "t1", "ratio": 0.97,
              "rows": [{"ratio": 0.97}, {"ratio": 1.02}]}
        p = ac.size_plan(self._el(), it, 125.4)
        self.assertFalse(p["eligible"], p)
        self.assertIn("各行宽比不一致", p["reason"])

    @unittest.skip(_SKIP_VERIFY_AUTOCORRECT)
    def test_overflowing_block_defers_to_existing_bound_path(self):
        """有溢出的块不在字号动作里抢：交给原来的"降字号到 bound"路径。"""
        import autocorrect as ac
        el = self._el(text="很长的一行" * 8, bbox=[100, 100, 400, 180])
        p = ac.size_plan(el, {"id": "t1", "ratio": 0.94}, 125.4)
        self.assertFalse(p["eligible"], p)
        self.assertIsNotNone(p["bound"], "溢出时必须带出 bound（同一个不等式的逆解）")


class AssetSignTest(unittest.TestCase):
    """素材 WARN 的处置：**必须被看见，但不拦交付**；人写的签字不许被重建抹掉。

    2026-11-09 两次实测定的设计：
      · 签字不可审计——`{"at":"x","by":"x","verdict":"x"}` 与"真看过图"在机器眼里完全一样，
        它只能证明"有人打了字"。所以只能当记录，不能当闸（初版接成阻塞，当天撤掉）。
      · 但 WARN 从来没出现在终端（cycle 只读 errors、把 notes 丢掉）是真缺口——
        现在 cycle 把「素材：」开头的 note 印成"记录 N 项（不阻塞交付）"。
    """

    SIGN = {"at": "2026-11-09", "by": "人", "verdict": "三种底色下均为干净圆盘",
            "evidence": "ic_ondiffbg.png"}
    WARN = "t1: WARN baked 7.0%（染色相近的区域被烤成不透明，需换底色目检）"

    def _page(self, td) -> Path:
        page = make_page(Path(td))
        write_json(page / "_check" / "assets.json",
                   {"stage": "assets", "kind": "assets", "outcome": "pass",
                    "passed": True, "notes": [self.WARN]})
        return page

    def _sign(self, page: Path, sign) -> None:
        inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
        for i in inv["items"]:
            if i["id"] == "t1":
                if sign is None:
                    i.pop("reviewed_on_bg", None)
                else:
                    i["reviewed_on_bg"] = sign
        write_json(page / "inventory.json", inv)

    def _delivery(self, page: Path):
        out = build_pptx(page)
        return gates.check_delivery(page, page / "spec.json", out,
                                    page / "look" / "rendered.png")

    def test_unsigned_warn_is_visible_but_not_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            self._sign(page, None)
            errs, notes = self._delivery(page)
            # 直接查 C5 的话术，而不是查"素材"这个壳：把 c5 结果改回 errs 时这条必须红
            self.assertFalse([e for e in errs if "没人签" in e or "素材" in e],
                             f"未签的 WARN 不许拦交付：{errs}")
            # 钉住数字：mock 掉"未签统计"这条会红（只查"未签"二字的话，'未签 0 条' 也算命中）
            hit = [n for n in notes if n.startswith("素材：") and "未签 1 条" in n]
            self.assertTrue(hit, f"未签 1 条必须在 notes 里点名：{notes}")

    def test_cover_waiver_does_not_count_as_signature(self):
        """`locked_reason`（cover 差属口径问题）不是目检签字，不许冒充。"""
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
            for i in inv["items"]:
                if i["id"] == "t1":
                    i["locked"] = True
                    i["locked_reason"] = "cover 低是掩膜口径问题，不是素材坏"
            write_json(page / "inventory.json", inv)
            _, notes = self._delivery(page)
            hit = [n for n in notes if n.startswith("素材：") and "未签 1 条" in n]
            self.assertTrue(hit, f"只有 cover 豁免时统计上仍算没签：{notes}")

    def test_shell_rows_are_not_resurrected(self):
        """没有旧行的空壳不许留（2026-11-09 审计：`prev_human` 漏了 `prev and` 时，
        40 页从 14 行涨到 50 行，而且不幂等——第二次重建又缩回去，plan 依赖键两头翻）。"""
        with tempfile.TemporaryDirectory() as td:
            page = make_page(Path(td))
            first, _ = inv_mod.build_inventory(page, page / "spec.json")
            self.assertEqual(first["items"], [],
                             f"纯空壳不该留行：{[i['id'] for i in first['items']]}")
            again, _ = inv_mod.build_inventory(page, page / "spec.json")
            self.assertEqual([i["id"] for i in again["items"]], [],
                             "第二次重建也不许冒出空壳（幂等）")

    def test_inventory_rebuild_keeps_signature(self):
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            self._sign(page, self.SIGN)
            again, _ = inv_mod.build_inventory(page, page / "spec.json")
            rows = {i["id"]: i for i in again["items"]}
            self.assertIn("t1", rows, "只写了签字的块不许被当成空壳连行删掉")
            self.assertEqual(rows["t1"].get("reviewed_on_bg"), self.SIGN,
                             "重建不许抹掉人写的签字（HUMAN_KEYS 必须含 reviewed_on_bg）")

    def test_signed_warn_says_zero_unsigned(self):
        with tempfile.TemporaryDirectory() as td:
            page = self._page(td)
            self._sign(page, self.SIGN)
            _, notes = self._delivery(page)
            hit = [n for n in notes if n.startswith("素材：")
                   and "WARN 1 条" in n and "未签 0 条" in n]
            self.assertTrue(hit, f"签了就该显示「WARN 1 条 / 未签 0 条」：{notes}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

