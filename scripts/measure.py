#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""measure.py — 一条命令量一个区域里的墨迹：框、行数、颜色、字号。

为什么需要它（2026-11-09）：`extract.py` 里的 `ink_box` / `ink_density` / `ink_color`
早就写好了，但**没有命令行入口**。人为了量 20 个文本块的墨迹框与字号，手写了 30 个
一次性 numpy 脚本，占整页工时的 55%——重复劳动，而且每次都在重新发明阈值。

这里**不复制算法**：墨迹框与颜色直接调 `extract.ink_box` / `extract.ink_color`。
唯一的例外是 `--rows`：逐行切分要的是**掩膜**（哪一行有墨迹），而 ink_box 只回答
"墨迹框在哪"，所以 `_ink_mask` 把 ink_box 里那三条算式照着写了一份；漂没漂由
`_self_check` 用 `ink_box()` 的返回值当场比对（差 >1px 就打 NOTE），不靠人记得同步两份代码。

字号印三档墨高法 + 字宽法（仅参考）：`含AA边`（与 `extract.ink_box` 同口径）、`收缩1px`（下界）、
`字体墨高比`（`--text` 给出时按真字形的 墨高/em 反解，**这一档才与渲染对得上**——0.95 对雅黑偏高 3~4%）；
`字宽法` 偏低是因为参考文本带字距，它只用来估字距、不定字号。

用法：
  python measure.py --image reference.png --region 430,390,680,450
  python measure.py --image reference.png --region 430,390,680,450 --rows --json
  python measure.py --image reference.png --region 160,145,860,245 --text "宫颈癌的核心病因：" --bold
退出码：0 正常；2 = region 越界或为空（打印人话原因）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 唯一实现处：墨迹框 / 墨色 / dpi 换算都来自 extract.py，本文件不重新定义
from extract import dpi_of, ink_box, ink_color

# 中文墨高 / 字号的经验比：墨高定字号（见 SKILL.md §1「判字号只看两个量」）。
# 22.3pt 的值就是 ink_h 37px / 0.95 / 125.4dpi × 72。
INK_H_PER_EM = 0.95
# 字宽法要用真字体量 advance width；微软雅黑是本技能固定字体
FONT_DIR = Path("C:/Windows/Fonts")
FONT_FILES = {"bold": "msyhbd.ttc", "regular": "msyh.ttc"}


# ---------------------------------------------------------------- 掩膜（与 ink_box 同规则）
def _ink_mask(gray: np.ndarray, box, pad: int = 2) -> tuple[np.ndarray, int, int]:
    """region 内的墨迹掩膜。**与 extract.ink_box 同一条规则**（四边中位色 + 自适应阈值）。

    为什么要掩膜：`--rows` 要按"整行无墨迹"切行，ink_box 只给框。
    阈值算式（三行）照抄 ink_box，**不另发明一套**：它的阈值随底色明暗浮动
    （`max(18, 0.18*255*(1-|bg-128|/255))`），另写一份必然漂。
    漂没漂由 `_self_check` 当场比对——这是本文件唯一一处"重复"，且带自检。

    返回 (mask, 原点 x, 原点 y)：mask 的 [0,0] 对应灰度图的 (x, y)。
    """
    h_gray, w_gray = gray.shape
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(w_gray, x1 + pad), min(h_gray, y1 + pad)
    roi = gray[y0:y1, x0:x1]
    if roi.size == 0:
        return np.zeros((0, 0), bool), x0, y0
    border = np.concatenate([roi[0, :], roi[-1, :], roi[:, 0], roi[:, -1]])
    bg = float(np.median(border))
    diff = np.abs(roi.astype(np.int16) - bg)
    mask = diff > max(18, 0.18 * 255 * (1 - abs(bg - 128) / 255))
    return mask, x0, y0


def _self_check(gray: np.ndarray, region, mask: np.ndarray, x0: int, y0: int) -> str:
    """掩膜包围盒 vs `ink_box()` 的返回：>1px 就说明两份阈值已经漂了。"""
    if int(mask.sum()) < 8:
        # ink_box 在掩膜过稀时原样返回输入框（它自己的兜底），此时没有可比的量
        return ""
    ys, xs = np.where(mask)
    mine = [x0 + float(xs.min()), y0 + float(ys.min()),
            x0 + float(xs.max()) + 1, y0 + float(ys.max()) + 1]
    got = ink_box(gray, region)
    if any(abs(float(a) - float(b)) > 1.0 for a, b in zip(mine, got)):
        return (f"掩膜框 {[round(v, 1) for v in mine]} 与 ink_box {[round(float(v), 1) for v in got]} "
                f"不一致——extract.ink_box 的阈值改过了，measure.py 的 _ink_mask 需同步")
    return ""


# ---------------------------------------------------------------- 切行
def _row_of(mask: np.ndarray, ya: int, yb: int, x0: int, y0: int) -> dict:
    """半开区间 [ya, yb) 的一行：闭区间坐标（打印用）+ 高宽。"""
    cols = np.where(mask[ya:yb].any(axis=0))[0]
    return {"index": 0, "y0": y0 + ya, "y1": y0 + yb - 1, "h": yb - ya,
            "x0": x0 + int(cols.min()), "x1": x0 + int(cols.max()),
            "w": int(cols.max() - cols.min()) + 1}


def _rows(mask: np.ndarray, x0: int, y0: int) -> list[dict]:
    """按"整行无墨迹"切行：间隔 ≥1px 无墨迹即视为断行。"""
    if mask.size == 0:
        return []
    out: list[dict] = []
    start: int | None = None
    for i, has in enumerate(mask.any(axis=1)):
        if has and start is None:
            start = i
        elif not has and start is not None:
            out.append(_row_of(mask, start, i, x0, y0))
            start = None
    if start is not None:
        out.append(_row_of(mask, start, int(mask.shape[0]), x0, y0))
    for n, r in enumerate(out, start=1):
        r["index"] = n
    return out


def _shrink1(mask: np.ndarray) -> np.ndarray:
    """1px 四邻域腐蚀：去掉最外一圈抗锯齿边，只留"核心墨迹"。

    为什么给两个字号：`ink_box` 的阈值（bg 差值 >18~25）会把最外一圈抗锯齿像素也算成墨迹，
    墨高因此系统性偏大约 1~2px（1px ≈ 0.6pt）。手量时人用的是更严的核心阈值，两者必然差一点。
    与其让人猜该信哪个，不如把两个都印出来：含 AA 边的那条与 `extract` 的口径一致（可比），
    收缩后的那条与"字形实际高度"更接近。
    """
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return np.zeros_like(mask)
    core = np.zeros_like(mask)
    core[1:-1, 1:-1] = (mask[1:-1, 1:-1] & mask[:-2, 1:-1] & mask[2:, 1:-1]
                        & mask[1:-1, :-2] & mask[1:-1, 2:])
    return core


def _mask_h(mask: np.ndarray) -> int:
    """掩膜的墨高（闭区间行数）；空掩膜返回 0。"""
    if mask.size == 0 or not mask.any(axis=1).any():
        return 0
    ys = np.where(mask.any(axis=1))[0]
    return int(ys.max() - ys.min() + 1)


# ---------------------------------------------------------------- 字体度量（字宽法）
def _text_metrics(text: str, bold: bool) -> tuple[dict | None, str]:
    """量一串字的 宽/em 与 墨高/em。返回 (度量 | None, 用到的字体文件名)。

    字体文件不存在时返回 (None, 文件名)：字宽法是**附加证据**，缺它不该抛异常。
    """
    name = FONT_FILES["bold" if bold else "regular"]
    path = FONT_DIR / name
    if not path.exists():
        return None, name
    try:
        font = ImageFont.truetype(str(path), 200)      # em=200px，量化误差足够小
    except Exception:                                   # noqa: BLE001 — 字体坏了同样只是跳过
        return None, name
    em = float(font.size)
    bbox = font.getbbox(text)                           # 墨迹包围盒（相对书写原点）
    return {"font": name,
            "adv_per_em": float(font.getlength(text)) / em,
            "ink_h_per_em": float(bbox[3] - bbox[1]) / em}, name


def _size_head(size_h_pt: float, size_core_pt: float | None) -> str:
    """字号行的前半段：含 AA 边 / 收缩 1px 两个数并排（让人不必猜该信哪个）。"""
    head = f"墨高法(含AA边) {size_h_pt:.1f}pt"
    if size_core_pt:
        head += f" / 墨高法(收缩1px) {size_core_pt:.1f}pt"
    else:
        head += " / 墨高法(收缩1px) —（掩膜太薄，腐蚀后不足原高 60%）"
    return head


# ---------------------------------------------------------------- CLI
def _parse_region(text: str) -> list[int]:
    parts = [p.strip() for p in str(text).replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise SystemExit(f"--region 要给四个整数 x0,y0,x1,y1：{text!r}")
    try:
        return [int(round(float(p))) for p in parts]
    except ValueError:
        raise SystemExit(f"--region 里有不是数字的东西：{text!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="量一个区域里的墨迹：框 / 行 / 颜色 / 字号")
    ap.add_argument("--image", required=True, help="参考图（页面截图）")
    ap.add_argument("--region", required=True,
                    help="页面像素坐标 x0,y0,x1,y1（逗号分隔）")
    ap.add_argument("--rows", action="store_true", help="逐行输出墨迹框")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供脚本消费）")
    ap.add_argument("--dpi", default="125.4",
                    help="本技能固定基准 125.4；写 auto 则按 宽_px / 13.3333 算")
    ap.add_argument("--text", default=None, help="额外用字宽法反推字号（需 --bold 指定字重）")
    ap.add_argument("--bold", action="store_true", help="--text 用 msyhbd.ttc（粗体）量")
    a = ap.parse_args()

    img_path = Path(a.image)
    if not img_path.exists():
        raise SystemExit(f"REFUSED: 图片不存在：{img_path}")
    pil = Image.open(img_path)
    gray = np.asarray(pil.convert("L"))
    rgb = np.asarray(pil.convert("RGB"))
    h_img, w_img = gray.shape
    notes: list[str] = []

    if str(a.dpi).strip().lower() == "auto":
        dpi = dpi_of(w_img)
    else:
        try:
            dpi = float(a.dpi)
        except ValueError:
            raise SystemExit(f"--dpi 要给数字或 auto：{a.dpi!r}")
    if dpi <= 0:
        raise SystemExit(f"--dpi 必须为正：{a.dpi!r}")

    region = _parse_region(a.region)
    x0, y0, x1, y1 = region
    if not (0 <= x0 < x1 <= w_img and 0 <= y0 < y1 <= h_img):
        print(f"region {x0},{y0} → {x1},{y1} 越界或为空：图只有 {w_img}x{h_img}"
              f"（要求 0≤x0<x1≤{w_img} 且 0≤y0<y1≤{h_img}）", file=sys.stderr)
        return 2

    # 墨迹框 / 颜色：直接复用 extract 的实现（阈值不在这里另发明）
    box = ink_box(gray, [x0, y0, x1, y1])
    bx0, by0, bx1, by1 = [int(round(v)) for v in box]
    ink_w, ink_h = bx1 - bx0, by1 - by0
    color = ink_color(rgb, box)

    mask, mx, my = _ink_mask(gray, [x0, y0, x1, y1])
    has_ink = int(mask.sum()) >= 8
    drift = _self_check(gray, [x0, y0, x1, y1], mask, mx, my)
    if drift:
        notes.append(drift)
    rows = _rows(mask, mx, my)
    if not has_ink:
        notes.append("该区域内没有墨迹（阈值内 0 像素）——框与字号都不成立，换 region")

    size_h_pt = (ink_h / INK_H_PER_EM) * 72.0 / dpi if has_ink else None
    # 收缩 1px 的"核心墨迹"：只保留够厚的部分。掩膜太薄（一条细线/小号字被腐蚀光）时不报，
    # 免得印出一个凭空小一半的字号让人误解。
    h_core = _mask_h(_shrink1(mask)) if has_ink else 0
    if not h_core or h_core < 0.6 * ink_h:
        h_core = 0
    size_h_core = (h_core / INK_H_PER_EM) * 72.0 / dpi if h_core else None
    size_w_pt = None
    size_h_font = None
    diff_pct = None
    metrics = None
    font_name = FONT_FILES["bold" if a.bold else "regular"]
    if a.text:
        metrics, font_name = _text_metrics(a.text, a.bold)
        if metrics is None:
            notes.append(f"未找到 {font_name}，跳过字宽法与字体墨高比")
        elif has_ink and metrics["adv_per_em"] > 0:
            size_w_pt = (ink_w / metrics["adv_per_em"]) * 72.0 / dpi
            if size_h_pt:
                diff_pct = (size_w_pt - size_h_pt) / size_h_pt * 100.0
            # 字体墨高比：用真字形的 墨高/em 反解，替掉 0.95 这个粗估。
            # 0.95 对微软雅黑偏高约 3~4%（实测 ink_h/em = 0.980~0.985），
            # 所以 h=38 用 0.95 得 23.0pt，用真字形比得 22.3pt —— 后者才与渲染对得上。
            if metrics["ink_h_per_em"] > 0:
                size_h_font = (ink_h / metrics["ink_h_per_em"]) * 72.0 / dpi
                if size_h_pt and abs(size_h_font - size_h_pt) / size_h_pt > 0.03:
                    notes.append(
                        f"0.95 是粗估：本块按真字形（墨高/em {metrics['ink_h_per_em']:.3f}）"
                        f"应为 {size_h_font:.1f}pt，与 0.95 估的 {size_h_pt:.1f}pt 差 "
                        f"{(size_h_font - size_h_pt) / size_h_pt * 100:+.1f}%——定字号优先取「字体墨高比」")

    if a.json:
        doc = {
            "image": str(img_path), "dpi": round(dpi, 2),
            "region": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            "ink": {"x0": bx0, "y0": by0, "x1": bx1 - 1, "y1": by1 - 1,
                    "w": ink_w, "h": ink_h} if has_ink else None,
            "row_count": len(rows), "rows": rows, "color": color,
            "size_h_pt": None if size_h_pt is None else round(size_h_pt, 2),
            "size_h_pt_core": None if size_h_core is None else round(size_h_core, 2),
            "size_h_pt_font": None if size_h_font is None else round(size_h_font, 2),
            "ink_h_core": h_core or None,
            "size_w_pt": None if size_w_pt is None else round(size_w_pt, 2),
            "diff_pct": None if diff_pct is None else round(diff_pct, 2),
            "text": a.text, "bold": bool(a.bold),
            "font": metrics["font"] if metrics else None,
            "adv_per_em": round(metrics["adv_per_em"], 4) if metrics else None,
            "ink_h_per_em": round(metrics["ink_h_per_em"], 4) if metrics else None,
            "notes": notes,
        }
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0

    print(f"region {x0},{y0} → {x1},{y1}")
    if has_ink:
        print(f"ink    x {bx0}..{bx1 - 1}  y {by0}..{by1 - 1}   w {ink_w}  h {ink_h}")
    else:
        print("ink    —（区域内没有墨迹）")
    print(f"行数   {len(rows)}")
    if a.rows:
        for r in rows:
            print(f"行{r['index']} y {r['y0']}..{r['y1']} h {r['h']}  "
                  f"x {r['x0']}..{r['x1']} w {r['w']}")
    print(f"颜色   {color}")
    if not has_ink:
        print("字号   —（区域内没有墨迹）")
    elif a.text and size_w_pt is not None:
        head = _size_head(size_h_pt, size_h_core)
        if size_h_font:
            head += f" / 墨高法(字体墨高比) {size_h_font:.1f}pt"
        print(f"字号   {head} / 字宽法 {size_w_pt:.1f}pt（仅参考：宽度差来自字距，定字号不看它）")
    else:
        shrunk = f"→{h_core}" if h_core else "→—"
        print(f"字号   {_size_head(size_h_pt, size_h_core)}"
              f"（ink_h {ink_h}{shrunk} / {INK_H_PER_EM}）")
    for n in notes:
        print(f"NOTE {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
