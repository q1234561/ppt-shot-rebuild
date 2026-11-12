#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""editability.py — 证明「每块文字都是真文本」，不是贴图。

为什么这条最重要：交付物是给客户改的。像素像不像无所谓，
但"看起来是字、其实是图片"是致命缺陷——verify 只比墨迹，抓不到。

**默认静态读成品 pptx**：按 spec 的每个文字元素找到同名 shape，判它是不是
TEXT_BOX、有没有可编辑 run、文字是否为空，再单独查"有没有一张图盖住 ≥85% 画布"。
这能抓住这条闸真正要防的东西（文字被图片顶替、整页贴图），不渲染。

**--render-check 保留旧法**：把每个 run 的字号 +1pt 渲两次、比像素变化。
它能抓到静态看不出的"文字框里塞了图片填充"，代价是两次渲染。
只在怀疑成品被人动过手时用。

**2.17.4 改法（2026-11-07）**：直接改**成品 pptx 里每个 run 的字号**（+1pt）再重渲，
不再"bump spec → 重新 build"。原因：build 门闩会因临时 spec 指纹不符而 REFUSED；
文字若定义在 override.add 里，bump 未合并的 spec 等于没改，会报"20 块文字，真文本 0 块"的假失败。

**2.26**：报告绑定依赖（成品 md5 + 元素清单 + 渲染尺寸 + 检查版本）。
同一份成品只证明一次，改文字才重跑；delivery 不再每次盲渲染两次。
尺寸从 spec.canvas 读，不再写死 1672×941；成品路径可显式给（cycle 自定义 --out 也能对上）。

用法：
  python editability.py --page <页面目录> [--spec spec.json] [--out out.pptx]
                        [--width 1672 --height 941] [--json _check/editability.json]
退出码：0 全过 / 1 有疑似贴图
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Pt

_sys = sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/，取 specmerge
import specmerge
from consts import drawn_as_text, is_picture, shape_id    # build 画不画文字 / 这形状是谁 / 是不是图（三处唯一判据）
import report_state as rs

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import cycle  # noqa: E402

MIN_CHANGED = 3        # 该块区域里至少这么多像素变化才算"真的动过"
BUMP_PT = 1.0


def bump_runs(src: Path, dst: Path) -> int:
    """把成品 pptx 里每个文字 run 的字号 +1pt。返回改了几个 run。"""
    prs = Presentation(str(src))
    n = 0
    for slide in prs.slides:
        for sh in slide.shapes:
            if not getattr(sh, "has_text_frame", False):
                continue
            for para in sh.text_frame.paragraphs:
                for run in para.runs:
                    cur = run.font.size
                    if cur is None:
                        continue
                    run.font.size = Pt(round(cur.pt + BUMP_PT, 1))
                    n += 1
    prs.save(str(dst))
    return n


def _iter_shapes(shapes):
    for sh in shapes:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_shapes(sh.shapes)
        else:
            yield sh


def classify_shapes(prs) -> dict:
    """派生清单：把成品里每个 shape 归到「可改 / 需说明 / 未证明」。

    为什么要有这份清单（2026-11-09）：
    旧判据只回答"文字还是不是文字"，答不了客户的第一句话——"**哪些能改**"。
    而"能改/不能改"不能靠人肉清点，因为它必须**先合规、再分类**：
    若先按类型分堆、再补一句"这个位图已在 inventory 说明"，
    就等于给"文字被贴成图"留了一条"归到二级清单"的洗白路径。

    四步，顺序即正确性（改了顺序结论就变）：
      ① group 展开——不展开会把一个组合读成"未识别对象"，组内的图算成 0 张；
      ② `is_picture` 先摘出去——位图下面所有原生判定都不适用，
         先判它会得到"它有 text_frame 吗"这种无意义问题；
      ③ 四类原生：text / table / chart / shape —— 这四类是真的能改；
      ④ 其余（media / ole / ...）单列**未证明**：既不进"可改"，也不进"不可改"。
         这一格是本函数存在的另一半理由：**未知不该被默认成任何一边**。

    返回 {"total", "native":[...], "pictures":[...], "unproven":[...]}。
    """
    native, pictures, unproven = [], [], []
    for slide in prs.slides:
        for sh in _iter_shapes(slide.shapes):
            sid = shape_id(sh)
            if is_picture(sh):
                pictures.append({"id": sid, "why": "位图：客户改不了，需在 inventory 说明"})
                continue
            if getattr(sh, "has_text_frame", False) and str(sh.text_frame.text).strip():
                native.append({"id": sid, "kind": "text"})
            elif getattr(sh, "has_table", False):
                native.append({"id": sid, "kind": "table"})
            elif getattr(sh, "has_chart", False):
                native.append({"id": sid, "kind": "chart"})
            elif sh.shape_type in (MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.FREEFORM,
                                   MSO_SHAPE_TYPE.LINE, MSO_SHAPE_TYPE.TEXT_BOX):
                native.append({"id": sid, "kind": "shape"})
            else:
                # 未知形态：不猜、不归类，单列。这是清单里唯一"允许空"的一格，
                # 但它空着必须是因为"确实没有"，不能是因为"没遍历到"。
                unproven.append({"id": sid, "kind": str(sh.shape_type)})
    return {"total": len(native) + len(pictures) + len(unproven),
            "native": native, "pictures": pictures, "unproven": unproven}


def static_check(spec, spec_path: Path, src: Path, W: int, H: int, json_out) -> int:
    """读成品 pptx 判断「文字还是不是文字」。不渲染。"""
    prs = Presentation(str(src))
    by_name = {}
    for slide in prs.slides:
        for sh in _iter_shapes(slide.shapes):
            # 索引键走 shape_id：spec 起了自定义 name 之后 name 不再等于 id，
            # 直接拿 sh.name 建索引会让每个改了名的文字块都报"成品里没有同名形状"。
            by_name[shape_id(sh)] = sh

    sw = float(prs.slide_width or 0)
    shh = float(prs.slide_height or 0)
    full_pic = None
    for sh in by_name.values():
        if is_picture(sh) and sw and shh:
            if (float(sh.width) * float(sh.height)) / (sw * shh) >= 0.85:
                full_pic = shape_id(sh)
                break

    rows, n_fail, n_runs = [], 0, 0
    for e in spec.get("elements") or []:
        # 判据用 drawn_as_text（= elements 段全集）：build 对这一段无条件 add_text，
        # 所以每一块都得在这里证明"它是真文本、没被贴图化"（P2-2 口径统一）。
        if not drawn_as_text(e) or e.get("skip") is True:
            continue
        sh = by_name.get(e["id"])
        why = ""
        if sh is None:
            why = "成品里没有同名形状"
        elif is_picture(sh):
            why = "是图片（文字被贴图化）"
        elif not getattr(sh, "has_text_frame", False):
            why = "没有文字帧"
        else:
            tf = sh.text_frame
            runs = [r for para in tf.paragraphs for r in para.runs if str(r.text).strip()]
            n_runs += sum(len(para.runs) for para in tf.paragraphs)
            if not str(tf.text).strip():
                why = "文字为空"
            elif not runs:
                why = "没有可编辑 run"
        ok = not why
        n_fail += 0 if ok else 1
        rows.append((e["id"], ok, why, str(e.get("text", ""))[:14]))

    if full_pic and not n_fail:
        n_fail = 1
        rows.append((full_pic, False, "这张图覆盖 ≥85% 画布（整页贴图）", ""))

    print("（静态读成品，不渲染）")
    print(f"{'id':11s} {'结果':4s}  内容")
    for i, ok, why, t in sorted(rows, key=lambda r: (r[1], r[0])):
        print(f"{i:11s} {'PASS' if ok else 'FAIL'}  {t}{('  ← ' + why) if why else ''}")
    print(f"\n文字 {len(rows)} 块，真文本 {sum(1 for r in rows if r[1])} 块，疑似贴图 {n_fail} 块")

    key = rs.key_editability(spec_path, src, W, H)
    out = Path(json_out) if json_out else spec_path.parent / "_check" / "editability.json"
    rs.write_report(out, {
        "stage": "editability", "kind": "editability",
        "outcome": "pass" if n_fail == 0 else "fail",
        "method": "static",
        "pptx": str(src), "pptx_md5": rs.file_digest(src),
        "render_size": [W, H], "runs": n_runs or 1,
        "texts": len(rows), "real_text": sum(1 for r in rows if r[1]), "suspect": n_fail,
        "items": [{"id": i, "ok": ok, "why": why} for i, ok, why, _ in rows],
    }, key)
    return 0 if n_fail == 0 else 1

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--spec", default="spec.json")
    ap.add_argument("--out", default=None, help="成品 pptx（默认 page/out.pptx）")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--json", default=None, help="报告路径（默认 page/_check/editability.json）")
    ap.add_argument("--render-check", action="store_true",
                    help="用旧法（字号 +1pt 渲两次比像素）复核；默认只静态读 pptx")
    ap.add_argument("--list", action="store_true",
                    help="只输出派生清单（可改/位图/未证明），不判文字真伪")
    a = ap.parse_args()

    page = Path(a.page).resolve()
    spec_path = (page / a.spec).resolve() if not Path(a.spec).is_absolute() else Path(a.spec)
    spec = specmerge.load_merged(spec_path)   # bbox 与 build 看同一份
    cv = spec.get("canvas") or {}
    W = int(a.width or cv.get("w_px") or 1672)
    H = int(a.height or cv.get("h_px") or 941)

    src = Path(a.out) if a.out else spec_path.parent / "out.pptx"
    if not src.exists():
        raise SystemExit(f"缺 {src}（先 build）")
    if not a.render_check:
        if a.list:
            c = classify_shapes(Presentation(str(src)))
            print(f"{'id':14s} {'归到':9s} 说明")
            for it in c["native"]:
                print(f"{it['id']:14s} {it['kind']:9s} 可直接改")
            for it in c["pictures"]:
                print(f"{it['id']:14s} {'picture':9s} {it['why']}")
            for it in c["unproven"]:
                print(f"{it['id']:14s} {'unproven':9s} 形态 {it['kind']}：未证明，不进结论")
            print(f"\n共 {c['total']} 个对象：可改 {len(c['native'])} / "
                  f"位图 {len(c['pictures'])} / 未证明 {len(c['unproven'])}")
            return 0
        return static_check(spec, spec_path, src, W, H, a.json)
    bump = spec_path.parent / "_bump.pptx"
    n_runs = bump_runs(src, bump)
    if n_runs == 0:
        raise SystemExit("REFUSED: 成品 pptx 里没有可改字号的 run —— 这份文件里没有真文本")

    cycle.ensure_daemon(spec_path.parent / "_check" / "renderd.log")
    base_png = spec_path.parent / "look" / "_edit_base.png"
    bump_png = spec_path.parent / "look" / "_edit_bump.png"
    cycle.render(str(src.resolve()), str(base_png.resolve()), W, H)
    cycle.render(str(bump.resolve()), str(bump_png.resolve()), W, H)

    arr = np.asarray(Image.open(base_png).convert("RGB")).astype(int)
    b = np.asarray(Image.open(bump_png).convert("RGB")).astype(int)
    Hb, Wb = arr.shape[:2]
    rows, n_fail = [], 0
    for e in spec["elements"]:
        if not drawn_as_text(e) or e.get("skip") is True:   # 唯一判据，见 consts
            continue
        x0, y0, x1, y1 = [int(v) for v in e["bbox"]]
        pad = int(0.25 * max(x1 - x0, y1 - y0))
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(Wb, x1 + pad), min(Hb, y1 + pad)
        d = np.abs(arr[y0:y1, x0:x1] - b[y0:y1, x0:x1]).max(axis=2)
        changed = int((d > 8).sum())
        ok = changed >= MIN_CHANGED
        n_fail += 0 if ok else 1
        rows.append((e["id"], changed, ok, str(e.get("text") or "")[:14]))

    print(f"（成品 pptx 里改了 {n_runs} 个 run 的字号 +{BUMP_PT:g}pt）")
    print(f"{'id':11s} {'变化像素':>8s}  结果  内容")
    for i, c, ok, t in sorted(rows, key=lambda r: (r[2], r[1])):
        print(f"{i:11s} {c:8d}  {'PASS' if ok else 'FAIL'}  {t}")
    print(f"\n文字 {len(rows)} 块，真文本 {sum(1 for r in rows if r[2])} 块，"
          f"疑似贴图 {n_fail} 块")

    for f in (bump, base_png, bump_png):
        f.unlink(missing_ok=True)

    key = rs.key_editability(spec_path, src, W, H)
    out = Path(a.json) if a.json else spec_path.parent / "_check" / "editability.json"
    rs.write_report(out, {
        "stage": "editability", "kind": "editability",
        "outcome": "pass" if n_fail == 0 else "fail",
        "pptx": str(src), "pptx_md5": rs.file_digest(src),
        "method": "render", "render_size": [W, H], "runs": n_runs,
        "texts": len(rows), "real_text": sum(1 for r in rows if r[2]), "suspect": n_fail,
        "items": [{"id": i, "changed": c, "ok": ok} for i, c, ok, _ in rows],
    }, key)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
