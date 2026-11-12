# -*- coding: utf-8 -*-
"""deck.py — 多页：把 N 页合成一份 pptx。

为什么不用 PowerPoint 的 `Slides.InsertFromFile`（2.6.0 踩过）：
它**不搬幻灯片背景**，合并后每页底色变白（同页实测 MAE 5.9）。补救得逐页重设背景，
而重设就要知道每页的 spec——那还不如一开始就在 python-pptx 里搬。

**两个入口，语义不同（2026-11-10 拆开）**：

  正式合并  `--root <整套>`        只读**本轮验过的单页 out.pptx**，不重新 build。
            页数与顺序来自 `<整套>/deck.json` 的 `pages`（不是目录名排序）。
            每页先过身份闸：delivery 模式、非分段、verdict.deliverable=ready、
            成品摘要与结论一致、当前输入摘要与结论一致。任一不满足就**点名拒绝**，
            不自动退回重建（"自动重建"会让一份过期报告看起来像刚验过）。
  草稿合并  `--root <整套> --draft` 或 `--pages p1 p2`
            逐页 build 后合并。只用于"先看看长什么样"，不产生交付结论。

背景一律整段搬 `p:bg`（纯色/渐变/图片填充都在里面）。旧实现只按 spec 重设纯色，
渐变页合成后会变白——那是**静默降级**，比报错危险。

用法：
  python deck.py --root <整套目录> --out deck.pptx
  python deck.py --root <整套目录> --out draft.pptx --draft
  python deck.py --pages p1_dir p2_dir --out draft.pptx
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/，取 specmerge
import specmerge
import report_state as rs

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.oxml.ns import qn

SKILL = Path(__file__).resolve().parent.parent.parent
PY = sys.executable

R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _spec_of(page: Path) -> Path:
    for c in (page / "spec.json", page / "page" / "spec.json"):
        if c.exists():
            return c
    raise SystemExit(f"REFUSED: {page} 下没有 spec.json")


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:                     # noqa: BLE001——坏文件按"缺"处理，由调用方点名
        return None


def _build(spec: Path, out: Path) -> None:
    r = subprocess.run([PY, str(SKILL / "scripts" / "build.py"),
                        "--spec", str(spec), "--out", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"build 失败：{spec}\n{r.stdout}\n{r.stderr}")
    for line in (r.stdout or "").splitlines():
        if any(k in line for k in ("OVERRIDE", "MISS", "FALLBACK", "REFUSED")):
            print(f"    {line.strip()}")


# ---------------------------------------------------------------- 身份闸（正式入口）
def _verify_page(page: Path) -> tuple[Path | None, str]:
    """这一页能不能进正式合并。返回 (成品路径, "") 或 (None, 不能并入的原因)。

    判据全部来自盘上报告，不重算判据本身——重算一遍等于把 cycle 的判断复制成第二份实现
    （本库最大的事故来源）。这里只回答一个问题：**这些结论还属于当前这份成品与这版输入吗**。
    """
    check = page / "_check"
    cyc = _read_json(check / "cycle.json")
    if not cyc:
        return None, "缺 _check/cycle.json（这页没跑过 cycle）"
    if cyc.get("partial"):
        return None, "只有分段结论（cycle.json partial=true，未写交付结论）"
    if str(cyc.get("mode")) != "delivery":
        return None, f"最近一次是 {cyc.get('mode')} 模式；正式合并只认 delivery 整跑"
    if str(cyc.get("state")) != "ready":
        return None, f"cycle 状态 {cyc.get('state')}（不是 ready）"

    vdoc = _read_json(check / "verdict.json")
    if not vdoc:
        return None, "缺 _check/verdict.json（分段跑会把它改名成 verdict.stale.json）"
    if str(vdoc.get("mode")) == "preview":
        return None, "verdict 是 preview 快照，不参与交付裁决"
    dv = str(vdoc.get("deliverable") or "")
    if dv != "ready":
        why = "; ".join(str(w) for w in (vdoc.get("deliverable_why") or []))
        return None, f"交付结论 {dv or '(缺)'}" + (f"：{why}" if why else "")

    out = page / "out.pptx"
    if not out.exists():
        return None, "缺 out.pptx"
    cur = rs.file_digest(out)
    rec = str(cyc.get("out_md5") or "")
    if not rec or not cur or str(cur)[:16] != rec[:16]:
        return None, (f"成品摘要 {str(cur)[:16]} 与结论记录的 {rec[:16] or '(缺)'} 不一致"
                      f"（成品被改过，或这份结论不是对它出的）")

    try:
        spec_p = _spec_of(page)
        cur_in = rs.page_inputs_digest(page, spec_p)
    except SystemExit as exc:
        return None, str(exc)
    rec_in = str(cyc.get("inputs_md5") or "")
    if not rec_in:
        return None, "结论里没有输入摘要（旧版 cycle.json）→ 重跑该页"
    if cur_in != rec_in:
        return None, "输入已变化（源图/spec/素材/项目规范）→ 结论属于旧输入，重跑该页"
    return out, ""


# ---------------------------------------------------------------- 背景与形状
def _copy_bg(src_slide, dst_slide) -> str:
    """整段搬 `p:bg`（纯色/渐变/图片填充都在里面）。返回 none/copied。"""
    s_csld = src_slide._element.find(qn("p:cSld"))
    s_bg = s_csld.find(qn("p:bg")) if s_csld is not None else None
    d_csld = dst_slide._element.find(qn("p:cSld"))
    old = d_csld.find(qn("p:bg"))
    if old is not None:
        d_csld.remove(old)
    if s_bg is None:
        return "none"
    d_csld.insert(0, copy.deepcopy(s_bg))     # p:bg 必须是 cSld 的第一个子元素
    return "copied"


def _set_bg(slide, color: str | None) -> None:
    """兜底：源页没有 p:bg 时按 spec 的纯色设一层（渐变没有兜底值，留给 p:bg）。"""
    if not color:
        return
    hexv = str(color).lstrip("#").upper()
    if len(hexv) != 6:
        return
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor.from_string(hexv)


def _copy_shapes(src_slide, dst_slide, page_name: str = "") -> int:
    """把源页的形状整树搬过来；图片按字节重建部件，再挂新 rId。

    踩过的坑：一开始用 `dst.part.relate_to(src_part, RT.IMAGE)` 直接复用源包里的 ImagePart，
    python-pptx 能重新打开合并文件，但 **PowerPoint 打不开**（Presentations.Open 报
    0x80048264）。原因是 ImagePart 仍指向源包，partname 与目标包里的 media 撞名，
    存出来的包内部引用是错的。正确做法是把图片字节重新注册成目标包自己的部件
    （get_or_add_image_part 会分配不冲突的 partname）。
    教训：**"python-pptx 能重开"不能当验证**，必须让 PowerPoint 真的打开并导出。

    2026-11-10：遇到本函数搬不了的关系（图表、外部链接等）**直接终止并点名**，
    不再让它变成一个坏引用躺进成品——那种包 PowerPoint 会弹"需要修复"。
    """
    src_tree = src_slide.shapes._spTree
    dst_tree = dst_slide.shapes._spTree
    n = 0
    for el in list(src_tree):
        tag = el.tag.split("}")[-1]
        if tag in ("nvGrpSpPr", "grpSpPr"):     # 形状树的根属性，目标页已有
            continue
        new_el = copy.deepcopy(el)
        ok_rids = set()
        for blip in new_el.iter():
            if not blip.tag.endswith("}blip"):
                continue
            rid = blip.get(R_NS + "embed")
            if not rid:
                continue
            try:
                part = src_slide.part.related_part(rid)
            except KeyError:
                raise SystemExit(f"REFUSED: {page_name} 的图片关系 {rid} 在源包里找不到——"
                                 f"这一页的单页 pptx 不完整，重跑该页")
            image_part, new_rid = dst_slide.part.get_or_add_image_part(io.BytesIO(part.blob))
            blip.set(R_NS + "embed", new_rid)
            ok_rids.add(new_rid)
        for node in new_el.iter():
            for k, v in node.attrib.items():
                if k.startswith(R_NS) and v not in ok_rids:
                    raise SystemExit(
                        f"REFUSED: {page_name} 里有本合并器搬不了的关系"
                        f"（<{node.tag.split('}')[-1]} {k.split('}')[-1]}={v}>）——"
                        f"图表/外部链接暂不支持整树搬；这一页改用原生形状重做，或先删除该对象")
        dst_tree.append(new_el)
        n += 1
    return n


# ---------------------------------------------------------------- 页清单
def _pages_from_deck(root: Path) -> list[Path]:
    """正式入口的唯一页序来源：`<root>/deck.json` 的 pages。

    目录名排序看着"自然"，但 `P01, P10, P02` 会被排成 `P01, P02, P10`——
    顺序错了没人报错，翻页时才发现。声明式页序把这件事变成可校验的。
    """
    dj = root / "deck.json"
    if not dj.exists():
        raise SystemExit(f"REFUSED: 正式合并要 {dj}（页数与页序的唯一来源，不按目录名排序）")
    deck = _read_json(dj)
    if not deck or not isinstance(deck.get("pages"), list) or not deck["pages"]:
        raise SystemExit(f"REFUSED: {dj} 里的 pages 缺失或为空")
    names = [str(x) for x in deck["pages"]]
    dup = [n for n, c in Counter(names).items() if c > 1]
    if dup:
        raise SystemExit(f"REFUSED: deck.json 的 pages 有重复页：{', '.join(dup)}"
                         f"——一页只能出现一次，否则成品里会有两张同名幻灯片")

    pages, bad = [], []
    for n in names:
        p = root / n
        if not p.is_dir():
            bad.append(f"{n}（目录不存在）")
            continue
        try:
            _spec_of(p)
        except SystemExit:
            bad.append(f"{n}（缺 spec.json）")
            continue
        pages.append(p)
    if bad:
        raise SystemExit("REFUSED: 正式合并缺页/缺 spec：\n  " + "\n  ".join(bad))
    return pages


def _pages_from_dirs(root: Path) -> list[Path]:
    return sorted([p for p in Path(root).iterdir()
                   if p.is_dir() and any((p / c).exists()
                                         for c in ("spec.json", "page/spec.json"))])


# ---------------------------------------------------------------- 合并
def _assemble(entries: list[tuple[str, Path, str | None]], out_path: Path) -> int:
    """entries = [(页名, 单页 pptx 路径, spec 纯色兜底色)]，按给定顺序合并。

    先写临时文件，成功后再发布到目标路径：失败时**已存在的正式文件不能被半成品覆盖**。
    """
    first = Presentation(str(entries[0][1]))
    deck = Presentation()
    deck.slide_width, deck.slide_height = first.slide_width, first.slide_height
    total = 0
    for name, path, bg in entries:
        src = Presentation(str(path))
        if [src.slide_width, src.slide_height] != [deck.slide_width, deck.slide_height]:
            raise SystemExit(
                f"REFUSED: {name} 的画布 {src.slide_width}x{src.slide_height} 与其他页"
                f" {deck.slide_width}x{deck.slide_height} 不一致——整套必须同一画布")
        src_slide = src.slides[0]
        dst_slide = deck.slides.add_slide(deck.slide_layouts[6])
        how = _copy_bg(src_slide, dst_slide)
        if how == "none":
            _set_bg(dst_slide, bg)
        n = _copy_shapes(src_slide, dst_slide, name)
        total += n
        print(f"  + {name}: {n} 个形状，背景 {how if how != 'none' else (bg or '未声明')}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="deck.", suffix=".pptx", dir=str(out_path.parent))
    os.close(fd)
    try:
        deck.save(tmp)
        # 结构自检：合并件必须能被重新打开、页数对得上
        chk = Presentation(tmp)
        if len(chk.slides) != len(entries):
            raise SystemExit(f"REFUSED: 合并后页数 {len(chk.slides)} ≠ 期望 {len(entries)}")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"saved {out_path}  slides={len(deck.slides)}  shapes={total}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="整套目录：正式入口按 deck.json 的 pages 取页")
    ap.add_argument("--pages", nargs="*", help="直接给页面目录列表（草稿入口，不校验结论）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--draft", action="store_true",
                    help="草稿合并：逐页 build 后合并，不校验 cycle/verdict（不产生交付结论）")
    a = ap.parse_args()

    draft = bool(a.draft or a.pages)
    if a.pages:
        raw = [Path(p) for p in a.pages]
        pages = [p for p in raw if p.is_dir()]
        skipped = [p.name for p in raw if not p.is_dir()]
        if skipped:
            print(f"跳过非目录 {len(skipped)} 项：{', '.join(skipped[:3])}")
    elif a.root:
        pages = _pages_from_dirs(Path(a.root)) if draft else _pages_from_deck(Path(a.root))
    else:
        ap.error("要么给 --root，要么给 --pages")

    if not pages:
        raise SystemExit("REFUSED: 没有找到任何页面")
    out_path = Path(a.out)

    entries: list[tuple[str, Path, str | None]] = []
    tmp = Path(tempfile.mkdtemp(prefix="deck_"))
    try:
        if draft:
            print(f"草稿合并（不校验交付结论）：{len(pages)} 页")
            for i, page in enumerate(pages, 1):
                spec_p = _spec_of(page)
                built = tmp / f"p{i:02d}.pptx"
                print(f"[{i}/{len(pages)}] {page.name}")
                _build(spec_p, built)
                spec = specmerge.load_merged(spec_p)
                bgd = spec.get("background") or {}
                entries.append((page.name, built, bgd.get("color")))
        else:
            print(f"正式合并：校验 {len(pages)} 页的交付结论")
            bad = []
            for page in pages:
                pptx, why = _verify_page(page)
                if pptx is None:
                    bad.append(f"{page.name}：{why}")
                    continue
                spec = specmerge.load_merged(_spec_of(page))
                entries.append((page.name, pptx, (spec.get("background") or {}).get("color")))
            if bad:
                raise SystemExit(
                    "REFUSED: 以下页不能并入正式交付（逐页修好再合并，不会自动重建）：\n  "
                    + "\n  ".join(bad))

        for i, (name, path, _bg) in enumerate(entries, 1):
            print(f"[{i}/{len(entries)}] {name}")
        return _assemble(entries, out_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
