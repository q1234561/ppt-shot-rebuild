#!/usr/bin/env python3
"""extract.py — 一张截图 -> draft spec（文字层自动，容器/贴图半自动）

设计约束（写进代码，不写进文档）：
  * 坐标一律用参考图像素，px->pt/inch 只在这里换算一次
  * 字号由"渲染-测宽"反解，不靠墨迹高猜
  * 每行文字都给 confidence 与 needs_review，模型只审 exception
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# 行高/换算常量的唯一真值在 consts.py，这里不重复定义（2.0.1 起）
from consts import px_to_pt, spacing_for_pitch
# 字体/粗细/字号/字距的唯一真值在 style.py（本文件不设字体常量）
import style

SLIDE_W_IN = 13.3333


# ---------------------------------------------------------------- 基础换算
def dpi_of(w_px: int) -> float:
    return w_px / SLIDE_W_IN


# ---------------------------------------------------------------- OCR 行
def ocr_lines(img_path: Path) -> list[dict]:
    from rapidocr_onnxruntime import RapidOCR

    im = Image.open(img_path).convert("RGB")
    ocr = RapidOCR()
    res, _ = ocr(np.array(im)[:, :, ::-1])
    out = []
    for box, txt, conf in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({
            "text": txt.strip(),
            "bbox": [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))],
            "conf": float(conf),
        })
    return out


# ---------------------------------------------------------------- 墨迹框 / 颜色
def ink_box(gray: np.ndarray, box, pad: int = 2):
    """在 OCR 框内收缩到真实墨迹，去掉 OCR 多给的留白。"""
    H, W = gray.shape
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(W, x1 + pad), min(H, y1 + pad)
    roi = gray[y0:y1, x0:x1]
    if roi.size == 0:
        return box
    # 背景取四边中位数，反差 >= 阈值的算墨迹
    border = np.concatenate([roi[0, :], roi[-1, :], roi[:, 0], roi[:, -1]])
    bg = float(np.median(border))
    diff = np.abs(roi.astype(np.int16) - bg)
    mask = diff > max(18, 0.18 * 255 * (1 - abs(bg - 128) / 255))
    if mask.sum() < 8:
        return box
    ys, xs = np.where(mask)
    return [x0 + float(xs.min()), y0 + float(ys.min()),
            x0 + float(xs.max()) + 1, y0 + float(ys.max()) + 1]


def ink_density(rgb: np.ndarray, box, text_color: str, pad: int = 10) -> float:
    """墨量 = 墨迹像素 / 墨迹框面积（%）。粗细指纹，尺度不变（与字号无关）。

    掩膜必须在外扩 pad 的 ROI 上算，再裁回原框统计：
    行框紧贴字（OCR 墨迹框），边框直接穿过笔画，拿它当底色样本会把笔画本身当背景，
    墨量算低三分之一，三档里就会永远选中最细的那档。
    """
    H, W = rgb.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    ox0, oy0 = max(0, x0 - pad), max(0, y0 - pad)
    ox1, oy1 = min(W, x1 + pad), min(H, y1 + pad)
    outer = rgb[oy0:oy1, ox0:ox1]
    if outer.size < 48:
        return 0.0
    m = style.ink_mask(outer, text_color)
    inner = m[y0 - oy0:y1 - oy0, x0 - ox0:x1 - ox0]
    if inner.size < 16:
        return 0.0
    return round(float(inner.mean() * 100.0), 1)


def ink_color(rgb: np.ndarray, box) -> str:
    """以框四边中位色为背景，取"离背景最远"的 12% 像素当墨迹。

    不能按亮度中位数分侧：深蓝字 + 白描边时框内中位亮度偏暗，会被判成"深底"，
    于是取最亮 12% → 拿到白描边。判据必须是"与背景的颜色距离"。
    """
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    roi = rgb[max(0, y0):y1, max(0, x0):x1].reshape(-1, 3).astype(np.int16)
    if roi.size == 0:
        return "#000000"
    h = max(1, y1 - y0)
    w = max(1, x1 - x0)
    grid = rgb[max(0, y0):y1, max(0, x0):x1].astype(np.int16)
    if grid.size == 0:
        return "#000000"
    border = np.concatenate([grid[0, :], grid[-1, :], grid[:, 0], grid[:, -1]])
    bg = np.median(border, axis=0)
    dist = np.linalg.norm(roi - bg, axis=1)
    k = max(8, int(len(roi) * 0.12))
    idx = np.argsort(-dist)[:k]
    # 只保留足够远的（避免整框都是同色时随机取）
    idx = idx[dist[idx] > 40]
    if len(idx) == 0:
        return "#000000"
    c = np.median(roi[idx], axis=0).astype(int)
    return "#%02X%02X%02X" % tuple(int(v) for v in c)


# ---------------------------------------------------------------- 段落合并
def _c_dist(a: str, b: str) -> float:
    pa = np.array([int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)], dtype=float)
    pb = np.array([int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)], dtype=float)
    return float(np.linalg.norm(pa - pb))


SYMBOLS = set("></\\-=·•◆◇■□●○▲△▼▽→←↑↓✓✔✗✘√×+*|▲▸▶◀◆♦")


def _infer_align(group: list[dict], tol: float = 10.0) -> str:
    """多行块：各行墨迹中心彼此差 <= tol px 判居中，否则左对齐。

    单行无法判定，返回 left（对渲染无影响：单行居中/左齐只有半个字宽差，
    且 build 会用 bbox 中心展开，位置由 bbox 决定）。

    tol=10px 的依据：真左对齐的两行，中心差 = 两行宽度差的一半，通常几十到上百 px；
    而居中块的中心差只剩抗锯齿/字形边距带来的 5~8px。6px 会把居中块判成左对齐，10px 才分得开。
    """
    if len(group) < 2:
        return "left"
    cs = [(g["bbox"][0] + g["bbox"][2]) / 2.0 for g in group]
    return "center" if (max(cs) - min(cs)) <= tol else "left"


def _is_symbol_text(t: str) -> bool:
    s = "".join(t.split())
    return bool(s) and all(ch in SYMBOLS for ch in s)


def merge_lines(lines: list[dict], dpi: float) -> list[dict]:
    """同列、行距接近、左/中对齐、字号与颜色接近的行 -> 一段。"""
    lines = sorted(lines, key=lambda d: (d["bbox"][1], d["bbox"][0]))
    used = [False] * len(lines)
    paras = []
    for i, ln in enumerate(lines):
        if used[i]:
            continue
        group = [ln]
        used[i] = True
        changed = True
        while changed:
            changed = False
            last = group[-1]
            lh = last["bbox"][3] - last["bbox"][1]
            for j, cand in enumerate(lines):
                if used[j]:
                    continue
                ch = cand["bbox"][3] - cand["bbox"][1]
                if abs(ch - lh) > 0.25 * lh:
                    continue
                # 颜色不同不成段：红色 HPV16/18 与下面深灰正文不是一段
                if _c_dist(last["color"], cand["color"]) > 70:
                    continue
                gap = cand["bbox"][1] - last["bbox"][3]
                if not (-0.3 * lh <= gap <= 0.95 * lh):
                    continue
                ov = min(last["bbox"][2], cand["bbox"][2]) - max(last["bbox"][0], cand["bbox"][0])
                if ov < 0.5 * min(last["bbox"][2] - last["bbox"][0],
                                  cand["bbox"][2] - cand["bbox"][0]):
                    continue
                group.append(cand)
                used[j] = True
                changed = True
        group.sort(key=lambda d: d["bbox"][1])
        paras.append(group)
    return paras


def normalize_size_groups(elements: list[dict], *,
                          normalized_out: list | None = None) -> list[str]:
    """同一视觉行带（y 中心接近、墨迹高接近）的字号取中位数。

    OCR 框在描边字/底色字上会多吃留白，导致同排导航 16.7 与 20.1 并存；
    同一排视觉上必然同字号，用组中位数抹平。

    归一**改动**了 `size_pt` / `weight`（语义不变，下游 build/emit 仍读它们），所以
    归一前的逐块原值另存一份：`size_pt_raw` / `weight_raw` + 来源标记 `size_pt_src` /
    `weight_src`（`measured` = 未被改写；`row-normalized` = 被本条改写）。
    没有这四个字段，"这块实测多少"就只能去正则抠 `needs_review` 的中文散文串。

    **判据优先级（写给下游）**：`size_pt_src == "measured"` 时才允许把 `size_pt` 当见证；
    `"row-normalized"` 的块必须用 `size_pt_raw` —— 否则"归一过"和"没归一过"两种证据
    会混成一种，读报告的人无从分辨。

    `normalized_out`：可选出参（就地 append），收结构化改写记录
    `[{"id","kind":"size"|"weight","from","to","rule"}]`，供下游机读。
    `needs_review` 的散文串保持原样（历史报告在比对）。
    """
    # 先给**所有**元素落盘原值与来源，再归一：只给被改的块留痕是上一版的缺口——
    # "没被改"本身也是信息（它才是可当见证的那一类）。
    for e in elements:
        e.setdefault("size_pt_raw", e.get("size_pt"))
        e.setdefault("weight_raw", e.get("weight", "regular"))
        e.setdefault("size_pt_src", "measured")
        e.setdefault("weight_src", "measured")

    rows: list[list[dict]] = []
    for e in elements:
        yc = (e["bbox"][1] + e["bbox"][3]) / 2
        hh = e["bbox"][3] - e["bbox"][1]
        for row in rows:
            ryc = (row[0]["bbox"][1] + row[0]["bbox"][3]) / 2
            rhh = row[0]["bbox"][3] - row[0]["bbox"][1]
            # 分母取两者较大的那个（除以 row[0] 的高度是不对称的：高字先入组时
            # 42px vs 30px 只算 28.6%，<30% 被并组，放大的激活项会被抹成同排最小字号）
            if abs(yc - ryc) < 8 and abs(hh - rhh) / max(hh, rhh, 1) < 0.25:
                row.append(e)
                break
        else:
            rows.append([e])
    notes = []
    for row in rows:
        if len(row) < 2:
            continue
        med = float(np.median([e["size_pt"] for e in row]))
        for e in row:
            if abs(e["size_pt"] - med) / med > 0.06:
                notes.append(f"{e['id']}: 字号 {e['size_pt']}->{med:.1f}（同排归一）")
                if normalized_out is not None:
                    normalized_out.append({"id": e["id"], "kind": "size",
                                           "from": e["size_pt_raw"],
                                           "to": round(med, 1), "rule": "row-median"})
                e["size_pt"] = round(med, 1)
                e["size_pt_src"] = "row-normalized"
        # 同排粗细也归一（多数票）：同一排文字视觉上必然同档，
        # 不能让墨量的逐块噪声把其中一项判成另一档
        ws = [e.get("weight", "regular") for e in row]
        major = max(set(ws), key=ws.count)
        for e in row:
            if e.get("weight") != major:
                notes.append(f"{e['id']}: 粗细 {e.get('weight')}->{major}（同排多票）")
                if normalized_out is not None:
                    normalized_out.append({"id": e["id"], "kind": "weight",
                                           "from": e["weight_raw"],
                                           "to": major, "rule": "row-majority"})
                e["weight"] = major
                e["weight_src"] = "row-normalized"
    return notes


# ---------------------------------------------------------------- 容器探测
def detect_containers(rgb: np.ndarray, text_boxes: list[list[float]]) -> list[dict]:
    """找"纯色大面积块"当卡片/胶囊/色条。只在文字框外取色，避免被字污染。"""
    H, W = rgb.shape[:2]
    small = cv2.resize(rgb, (W // 4, H // 4), interpolation=cv2.INTER_AREA)
    q = (small // 12 * 12).astype(np.uint8)
    out = []
    seen = []
    for color in _top_colors(q, n=14):
        mask = np.all(np.abs(q.astype(int) - np.array(color)) <= 12, axis=2).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        for k in range(1, n):
            x, y, w, h, area = stats[k]
            if area < (W // 4 * H // 4) * 0.012:
                continue
            bx = [x * 4, y * 4, (x + w) * 4, (y + h) * 4]
            if _overlaps_any(bx, text_boxes, 0.75):
                continue  # 文字块本身形成的连通域
            if _is_dup(bx, seen):
                continue
            seen.append(bx)
            rectness = area / max(w * h, 1)
            out.append({
                "bbox": [float(v) for v in bx],
                "fill": "#%02X%02X%02X" % tuple(int(c) for c in color),
                "rectness": round(float(rectness), 3),
                "shape": _guess_shape(bx),
            })
    return sorted(out, key=lambda d: -((d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1])))


def _top_colors(q: np.ndarray, n: int) -> list[tuple]:
    flat = q.reshape(-1, 3)
    uniq, cnt = np.unique(flat, axis=0, return_counts=True)
    order = np.argsort(-cnt)[:n * 2]
    return [tuple(int(v) for v in uniq[i]) for i in order]


def _overlaps_any(bx, boxes, ratio):
    for b in boxes:
        ix = min(bx[2], b[2]) - max(bx[0], b[0])
        iy = min(bx[3], b[3]) - max(bx[1], b[1])
        if ix > 0 and iy > 0:
            inter = ix * iy
            if inter / max((bx[2] - bx[0]) * (bx[3] - bx[1]), 1) > ratio:
                return True
    return False


def _is_dup(bx, seen, tol=0.12):
    for b in seen:
        if all(abs(bx[i] - b[i]) < tol * max(bx[2] - bx[0], bx[3] - bx[1], 1) for i in range(4)):
            return True
    return False


def _guess_shape(bx) -> str:
    # 只产 build.SHAPES 里有的值。以前细条返回 "line"（build 里没有），
    # 落到 add_shape 的 .get(默认值) 就静默变成圆角矩形 —— 三条形状枚举打架的典型。
    return "rect" if (bx[3] - bx[1]) <= 6 else "rounded_rect"


# ---------------------------------------------------------------- 贴图候选
def detect_assets(rgb: np.ndarray, text_boxes, dpi: float) -> list[dict]:
    """高梯度 + 高色数 + 不与文字重合的大块 -> 贴图候选。只报，不裁。

    **不排除容器区域**：原 docstring 承诺过"排除容器区域"，但函数体从没用过 containers，
    那个参数只是摆设（2026-11-09 交叉审计发现的第二处"文档说了、代码没做"）。
    参数与承诺一起删掉：落在容器内部的候选照常报出来，靠人看 review 列表时自己筛掉。
    """
    H, W = rgb.shape[:2]
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(g, 40, 110)
    density = cv2.blur((edges > 0).astype(np.float32), (25, 25))
    mask = (density > 0.10).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for k in range(1, n):
        x, y, w, h, area = stats[k]
        if area < 0.01 * W * H or w < 40 or h < 40:
            continue
        bx = [float(x), float(y), float(x + w), float(y + h)]
        if _overlaps_any(bx, text_boxes, 0.55):
            continue
        out.append({"bbox": bx, "why": "high-edge-density", "w_px": w, "h_px": h})
    return sorted(out, key=lambda d: -((d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1])))[:8]



# ---------------------------------------------------------------- 样式探测
def probe_style(rgb: np.ndarray, bbox, obstacles, bg, min_delta: int = 12):
    """只测"能用安全区采样测准"的两件事：是否渐变、渐变两端色。

    做法：容器内缩 12%，排除文字/素材/更小容器的遮挡，对剩下的有效像素
    按 y（或 x）排序取最上 10% 与最下 10% 的中位色。用分位数而不是固定条带，
    这样即使半个容器被胶囊压住，也能从剩余部分采到上下两端。

    为什么不做描边/阴影/圆角：三者都要求"精确边缘"，而容器 bbox 本身有 ±20px 误差
    （实测红胶囊左边界差 22px），边缘采样会采到卡外或背景渐变里。
    这三项按第一性原理不做自动判断，写进 SKILL 由人目视给出。
    """
    H, W = rgb.shape[:2]
    x0, y0, x1, y1 = [float(v) for v in bbox]
    w, h = x1 - x0, y1 - y0
    if w < 24 or h < 24:
        return None
    pad = max(6, int(min(w, h) * 0.12))
    ix0, iy0, ix1, iy1 = x0 + pad, y0 + pad, x1 - pad, y1 - pad
    if ix1 - ix0 < 10 or iy1 - iy0 < 10:
        return None

    yy, xx = np.mgrid[0:H, 0:W]
    valid = (xx >= ix0) & (xx < ix1) & (yy >= iy0) & (yy < iy1)
    for o in obstacles:
        valid &= ~((xx >= o[0] - 2) & (xx <= o[2] + 2) & (yy >= o[1] - 2) & (yy <= o[3] + 2))
    if valid.sum() < 300:
        return None

    def ends(axis_coord):
        c = np.where(valid)[0] if axis_coord is None else None
        coords = axis_coord[valid]
        lo, hi = np.percentile(coords, 10), np.percentile(coords, 90)
        mA = valid & (axis_coord <= lo)
        mB = valid & (axis_coord >= hi)
        if mA.sum() < 40 or mB.sum() < 40:
            return None
        A = np.median(rgb[mA], axis=0).astype(int)
        B = np.median(rgb[mB], axis=0).astype(int)
        if np.abs(A - bg).max() < 10 or np.abs(B - bg).max() < 10:
            return None
        return A, B

    # 胶囊（宽高比 > 3）只测纵向：横向两端是圆头，bbox 偏差 20px 就会采到卡外
    dirs = ((yy, 90),) if w / max(h, 1) > 3 else ((yy, 90), (xx, 0))
    best = None
    for coord, angle in dirs:
        r = ends(coord)
        if r is None:
            continue
        A, B = r
        delta = int(np.abs(A - B).max())
        if best is None or delta > best[0]:
            best = (delta, A, B, angle)
    if best is None:
        return None
    delta, A, B, angle = best
    hexa = "#%02X%02X%02X" % tuple(int(v) for v in A)
    hexb = "#%02X%02X%02X" % tuple(int(v) for v in B)
    if delta >= min_delta:
        return {"gradient": [hexa, hexb], "gradient_angle": angle, "probe_delta": delta}
    return {"fill": hexa, "probe_delta": delta}


def page_bg(rgb: np.ndarray) -> np.ndarray:
    H, W = rgb.shape[:2]
    k = 10
    c = np.concatenate([rgb[:k, :k].reshape(-1, 3), rgb[:k, -k:].reshape(-1, 3),
                        rgb[-k:, :k].reshape(-1, 3), rgb[-k:, -k:].reshape(-1, 3)])
    return np.median(c, axis=0)


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--assets-dir", default=None)
    a = ap.parse_args()

    img_path = Path(a.image)
    pil = Image.open(img_path).convert("RGB")
    W, H = pil.size
    dpi = dpi_of(W)
    rgb = np.array(pil)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    raw = ocr_lines(img_path)
    for ln in raw:
        ln["bbox"] = box = ink_box(gray, ln["bbox"])
        ln["color"] = ink_color(rgb, box)
        ln["ink_w"] = box[2] - box[0]
        ln["ink_h"] = box[3] - box[1]

    paras = merge_lines(raw, dpi)
    elements, review = [], []

    for i, group in enumerate(paras):
        bbox = [min(g["bbox"][0] for g in group), min(g["bbox"][1] for g in group),
                max(g["bbox"][2] for g in group), max(g["bbox"][3] for g in group)]
        text = "\n".join(g["text"] for g in group)
        # 粗细 → 字号：先按墨量定粗细（尺度不变），再用该档字体解宽度。
        # 顺序不可逆——反过来（先解字号再判粗细）会让字号吸收粗细的错。
        widest = max(group, key=lambda g: g["ink_w"])
        dens = ink_density(rgb, widest["bbox"], widest["color"])
        st = style.solve(widest["text"], widest["ink_w"], dens, dpi)
        size_px = st["size_pt"] * dpi / 72.0
        if st["_weight_err"] > 6:
            review.append(f"t{i+1}: 三档粗细的墨量都差 {st['_weight_err']} 点（{widest['text'][:10]}）")
        # 行距 px -> 行距倍数
        if len(group) > 1:
            pitch = (group[-1]["bbox"][1] - group[0]["bbox"][1]) / (len(group) - 1)
            # 倍数作用在字体自然行高上（LINE_EM），不是字号本身 —— 见 consts.py 实测记录
            spacing = float(spacing_for_pitch(pitch, px_to_pt(size_px, dpi), dpi))
        else:
            spacing = 1.0
        elements.append({
            "id": f"t{i+1}", "type": "text", "text": text,
            "bbox": [round(v, 1) for v in bbox],
            "size_pt": st["size_pt"],
            "weight": st["weight"],
            "letter_spacing_pt": 0.0,
            "_ink_density": dens,      # 历史字段，当前无消费者；改字后重解字号用逐字 pitch 反解（SKILL.md §4）
            "color": _most_common([g["color"] for g in group]),
            "line_spacing": round(spacing, 2),
            "lines": len(group),
            "align": _infer_align(group),
            "conf": round(float(min(g["conf"] for g in group)), 2),
        })

    text_boxes = [e["bbox"] for e in elements]
    for e in elements:
        if _is_symbol_text(e["text"]):
            e["review"] = "疑似图形符号，应改为原生形状（chevron/箭头/圆点），不是文字"
            e["_symbol"] = True
            review.append(f"{e['id']}: 符号行 {e['text']!r} → 需人工改 shape")
    # 符号行不参与同排字号归一（它不是文字，字号无意义）
    normalized: list[dict] = []
    review += normalize_size_groups([e for e in elements if not e.get("_symbol")],
                                    normalized_out=normalized)
    containers = detect_containers(rgb, text_boxes)
    assets = detect_assets(rgb, text_boxes, dpi)

    # 容器去重：只留"含文字"或"面积 > 2% 页"的，避免背景碎块
    keep = []
    for c in containers:
        cx = (c["bbox"][0] + c["bbox"][2]) / 2
        cy = (c["bbox"][1] + c["bbox"][3]) / 2
        contains = any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in text_boxes)
        big = (c["bbox"][2] - c["bbox"][0]) * (c["bbox"][3] - c["bbox"][1]) > 0.02 * W * H
        if contains or big:
            keep.append(c)

    bgc = page_bg(rgb)
    base_obs = [e["bbox"] for e in elements] + [x["bbox"] for x in assets]
    styled = []
    for i, c in enumerate(keep):
        # 其他容器（胶囊压卡片）也必须排除，否则会采到胶囊的红色/蓝色
        # 只排除"更小的"容器：胶囊压在大卡上要排除胶囊；大卡包着胶囊时不能排除大卡
        a_self = (c["bbox"][2] - c["bbox"][0]) * (c["bbox"][3] - c["bbox"][1])
        others = [o["bbox"] for j, o in enumerate(keep) if j != i and
                  (o["bbox"][2] - o["bbox"][0]) * (o["bbox"][3] - o["bbox"][1]) < a_self * 0.8]
        st = probe_style(rgb, c["bbox"], base_obs + others, bgc)
        item = {"id": f"s{i+1}", "type": "shape", "shape": c["shape"],
                "bbox": [round(v, 1) for v in c["bbox"]],
                "rectness": c["rectness"], "review": st is None}
        if st:
            item.update(st)
        else:
            item["fill"] = c["fill"]
            review.append(f"s{i+1}: 容器样式探测失败（bbox 偏大或内部太满），需人给 fill/gradient")
        styled.append(item)

    spec = {
        "source": img_path.name,
        "canvas": {"w_px": W, "h_px": H, "dpi": round(dpi, 2),
                   "width_in": SLIDE_W_IN, "height_in": round(H / dpi, 4)},
        "stats": {"ocr_lines": len(raw), "paragraphs": len(elements),
                  "container_hints": len(keep), "asset_candidates": len(assets)},
        "needs_review": review,
        # 结构化版的同排归一记录（needs_review 里是散文串，历史报告仍比对它，故保留）：
        # 有了它，下游不必用正则去抠中文才能知道"哪个是归一来的、从多少改到多少"。
        "normalized": normalized,
        "_note": "container_hints 是低可信候选（半透明卡压渐变，rectness 实测 0.09~0.59，"
                 "没有一个可信）。build 不会画它；要容器就照 hints 的 bbox 人工核准"
                 "（放大到 ±5px 量边）后写进 spec.containers。",
        "elements": elements,
        "container_hints": styled,
        "assets": [{"id": f"i{i+1}", "type": "image",
                    "bbox": [round(v, 1) for v in x["bbox"]],
                    "src": None, "review": True} for i, x in enumerate(assets)],
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(_clean(spec), ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"canvas {W}x{H} dpi={dpi:.1f}")
    print(f"OCR {len(raw)} 行 -> {len(elements)} 段 | 容器候选 {len(keep)}（低可信，不进成品）| 贴图候选 {len(assets)}")
    for r in review:
        print("  REVIEW", r)
    return 0


def _most_common(vals):
    from collections import Counter
    return Counter(vals).most_common(1)[0][0]


def _clean(o):
    """numpy 标量 -> 原生类型，保证 json 可序列化。"""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return o


if __name__ == "__main__":
    raise SystemExit(main())
