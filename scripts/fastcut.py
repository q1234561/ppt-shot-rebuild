#!/usr/bin/env python3
"""fastcut.py — 圆形底盘图标：椭圆遮罩 + 边缘反解 + 裁断自检。

设计约束（2.0.1 修）：
  * 不给"自动找圆"：白圆盘与卡片渐变在 R 通道平滑过渡（实测无跳变），
    任何阈值分割都会把整块卡片吃进连通域，拟合出 r=120 的假圆。宁可让人给准框。
  * bbox 语义 = **圆盘外接框**，由人给（放大 4 倍 + 10px 网格图读，误差 ≤2px）。
  * 给不准不会被静默放过：裁框最外 1px 的 alpha 均值 > 0.15 直接 FAIL，
    提示"框小了，主体被裁断"。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# 圆形 α 的过渡带宽度（占半径比例）。0.03 → 76px 框约 1px 羽化：
# 与 2026-11-01 记录的 alpha=clip((1-r)/0.03) 同一口径，别再引入第二套常量。
RAMP = 0.03

# fastcut 支持的 α 来源（唯一真值）。lint_doc.py 用它核对 SKILL：
#   只有两条：圆（几何 α）+ 颜色键（chroma）。
#   照片/插画/半透明主体走 cutout.py（matting），不在本文件。
MASK_KINDS = ("circle", "chroma", "duotone", "feather", "flat", "pictogram", "rect", "rectmask")
# 注意：MASK_KINDS 是"fastcut 能产 PNG 的 mask"，不是全集。
# cutout 由 cutout.py 产，不在这个表里 —— 以前注释写"唯一真值"，让 lint 与人都误判。


def _emit(el: dict, rgb: np.ndarray, alpha: np.ndarray, outdir: Path) -> Path:
    """统一出口：存 RGBA + 存 -dark.png 深底预览。"""
    rgba = np.dstack([np.clip(rgb, 0, 255).astype(np.uint8),
                      (np.clip(alpha, 0, 1) * 255).astype(np.uint8)])
    p = outdir / f"{el['id']}.png"
    Image.fromarray(rgba).save(p)
    el["src"] = str(p)
    dark = np.zeros_like(rgb)
    dark[:] = (18, 22, 30)
    for c in range(3):
        dark[:, :, c] = rgb[:, :, c] * alpha + dark[:, :, c] * (1 - alpha)
    Image.fromarray(dark.astype(np.uint8)).save(outdir / f"{el['id']}-dark.png")
    return p


def _hex2rgb(v, default=(255, 255, 255)):
    if not v:
        return np.array(default, dtype=np.float32)
    s = str(v).lstrip("#")
    return np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def _write_src_to_override(spec: dict, spec_path: Path) -> None:
    """只把 assets[*].src 写进 override.json，spec.json 保持只读。

    为什么必须改：spec 的唯一写者是 extract.py。这两个工具原来整份回写 spec，
    等于给 spec 加了第二、第三个写者——extract 一重跑就把 src 冲掉（2.4.5 事故的结构成因）。
    只写 src，且只写相对 spec 的路径，换机器也能用。
    """
    import specmerge
    ov_path = spec_path.parent / "override.json"
    ov = specmerge.load(ov_path)
    patch = {}
    for el in spec.get("assets", []):
        src = el.get("src")
        if not src:
            continue
        p = Path(src)
        try:
            rel = p.resolve().relative_to(spec_path.parent.resolve())
            src = str(rel).replace("\\", "/")
        except ValueError:
            src = str(p)
        patch.setdefault("assets", {}).setdefault(el["id"], {})["src"] = src
    if patch:
        for k, v in patch["assets"].items():
            ov.setdefault("assets", {}).setdefault(k, {}).update(v)
        ov_path.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  src → {ov_path.name}（{len(patch['assets'])} 项；spec.json 未动）")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--force-ids", default="",
                    help="逗号分隔：这些素材即使已有 src 也重生成（依赖摘要变了时由 cycle 调用）")
    a = ap.parse_args()
    force = {x.strip() for x in a.force_ids.split(",") if x.strip()}

    import specmerge
    spec = specmerge.load_merged(a.spec)   # 必须看见 override.add 的素材（人手补的圆形素材就在那）
    img = np.array(Image.open(a.image).convert("RGB"))
    H, W = img.shape[:2]
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    todo = [x for x in spec.get("assets", [])
            if (not x.get("src") or x.get("id") in force) and x.get("mask") in MASK_KINDS]
    fails = 0
    for el in todo:
        if el.get("mask") == "circle":
            # 几何 α（2.3.0 起「圆盘/圆环/柔光球体」的默认路径）。
            # 唯一原则：**α 必须表达形状**。这里 α 由人给的 (圆心, 半径) 算出，与矩形框无关；
            # 框只决定裁多少像素，决定不了"哪里透明"。
            # 为什么不再用 bake：bake 的 α 是"到矩形边的距离"，与主体形状无关，
            # 形状全靠 RGB 与底色相同来伪装 → 换底色/放大立刻变回方块。
            if not el.get("circle"):
                print(f"  FAIL {el['id']} mask=circle 必须给 circle:[cx,cy,r]（页面像素，r 误差 ≤2px）")
                fails += 1
                continue
            cx, cy, r = [float(v) for v in el["circle"]]
            x0, y0, x1, y1 = [int(round(v)) for v in el["bbox"]]
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
            sub = img[y0:y1, x0:x1].astype(np.float32)
            h, w = sub.shape[:2]
            # 圆形的 α 只由 (圆心, 半径) 决定，四个角必然落在圆外。框不是正方形时
            # 短边那一侧的角会有 α>0，报出来的是"角内 α 偏高"，看不出真因。
            if abs(w - h) > 2:
                print(f"  FAIL {el['id']} mask=circle 要求裁框是正方形（当前 {w}x{h}）——"
                      f"四角必须有 α=0，否则角检查必挂；把 bbox 改成边长 2r+4 的正方形（每边留 2px 羽化余量；写成 2r 会被 spec_lint / inventory 判红）")
                fails += 1
                continue
            yy, xx = np.mgrid[0:h, 0:w]
            d = np.sqrt((xx + x0 - cx) ** 2 + (yy + y0 - cy) ** 2)
            alpha = np.clip((1.0 - d / max(r, 1e-6)) / RAMP, 0, 1)
            rgba = np.dstack([sub.astype(np.uint8), (alpha * 255).astype(np.uint8)])
            path = outdir / f"{el['id']}.png"
            Image.fromarray(rgba).save(path)
            el["src"] = str(path)
            # 自检：角内必须空（圆的角天然是空的），不透明占比不能超过几何期望太多
            corner = max(float(alpha[2:12, 2:12].mean()), float(alpha[2:12, -12:-2].mean()),
                         float(alpha[-12:-2, 2:12].mean()), float(alpha[-12:-2, -12:-2].mean())) \
                if min(h, w) > 24 else 0.0
            exp = float((d <= r).mean())
            ok = corner < 0.15 and float((alpha > 0.9).mean()) <= max(0.92, exp + 0.06)
            print(f"  {'OK  ' if ok else 'FAIL'} {el['id']} 圆形α {w}x{h} r={r:g} "
                  f"角内α={corner:.2f} 不透明={float((alpha > 0.9).mean()):.0%}(期望{exp:.0%})")
            if not ok:
                fails += 1
            dark = np.zeros_like(sub)
            dark[:] = (18, 22, 30)
            for c in range(3):
                dark[:, :, c] = sub[:, :, c] * alpha + dark[:, :, c] * (1 - alpha)
            Image.fromarray(dark.astype(np.uint8)).save(outdir / f"{el['id']}-dark.png")
            continue

        if el.get("mask") == "chroma":
            # 颜色键：饱和图形压在白底盘上（红水滴 / 蓝图标）。
            # α 来自 chroma（max-min）——是"颜色本身"给出的形状，符合「α 必须表达形状」。
            # 为什么另起一条：icon 模式只认 B-R>80（蓝），红图标 B-R 为负 → 覆盖率 0 直接 FAIL；
            # cutout 的 matting 把 90px 小图标上采样到 2048 再下采，细节会化掉。
            # 坑：水滴上的白色高光 sat≈0 会被判成背景，必须按"洞"填回（floodFill 从边界淹）。
            # 一块只出一件：下面只保留面积最大的连通域，所以"主体 + 血滴/角标"
            # 这类多部件图会把小件整只丢掉（本页躯干 ic4 丢了血滴，调 chroma_sat 也连不上）。
            # 判据：小件是独立色块 → 拆成多个 asset 各给 bbox，按原位叠回，别指望这一条路径包办。
            x0, y0, x1, y1 = [int(round(v)) for v in el["bbox"]]
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
            sub = img[y0:y1, x0:x1].astype(np.float32)
            h, w = sub.shape[:2]
            sat = sub.max(axis=2) - sub.min(axis=2)
            m = (sat > float(el.get("chroma_sat", 45))).astype(np.uint8)
            ncc, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
            # 一块可以出多件：多部件图标（柱状图三根柱、主体+角标、躯干+血滴）必须整组保留。
            # 以前只留面积最大的连通域 → 柱状图只剩 1/3，且覆盖率仍落在判据区间里所以静默 PASS
            # （2026-11-07 实测）。现在只滤掉「相对最大件明显是噪点」的碎片。
            if ncc > 2:
                areas = st[1:, cv2.CC_STAT_AREA].astype(np.float64)
                amax = float(areas.max())
                keep = np.zeros(ncc, np.uint8)
                for i in range(1, ncc):
                    av = float(areas[i - 1])
                    if av >= 6 and av >= max(0.02 * amax, 0.0015 * w * h):
                        keep[i] = 1
                if keep.sum():
                    m = keep[lab].astype(np.uint8)
            ff = m.copy()
            holes = np.zeros((h + 2, w + 2), np.uint8)
            cv2.floodFill(ff, holes, (0, 0), 1)
            m = np.clip(m + (ff == 0).astype(np.uint8), 0, 1)
            alpha = np.clip(cv2.GaussianBlur(m.astype(np.float32), (3, 3), 0.6), 0, 1)
            bg = np.array([255.0, 255.0, 255.0])          # 图标所在底盘是白的
            band = (alpha > 0.05) & (alpha < 0.95)
            clean = sub.copy()
            if band.any():
                aa = alpha[band][:, None]
                clean[band] = np.clip((sub[band] - (1 - aa) * bg[None, :]) / np.maximum(aa, 1e-3), 0, 255)
            rgba = np.dstack([clean.astype(np.uint8), (alpha * 255).astype(np.uint8)])
            p = outdir / f"{el['id']}.png"
            Image.fromarray(rgba).save(p)
            el["src"] = str(p)
            cov = float((alpha > 0.5).mean())
            # 上限只用来抓"根本没抠掉底色"：裁框贴紧主体时覆盖率天然很高（实测 0.94），
            # 卡在 0.92 会把正常素材判死。真没抠的情况接近 1.00。
            ok = 0.02 < cov < 0.98
            print(f"  {'OK  ' if ok else 'FAIL'} {el['id']} chroma {w}x{h} 覆盖率={cov:.1%}")
            if not ok:
                fails += 1
            dark = np.zeros_like(sub); dark[:] = (18, 22, 30)
            for c in range(3):
                dark[:, :, c] = clean[:, :, c] * alpha + dark[:, :, c] * (1 - alpha)
            Image.fromarray(dark.astype(np.uint8)).save(outdir / f"{el['id']}-dark.png")
            continue

        if el.get("mask") == "rect":
            # 整块留图：不切 α，直接裁原图。SKILL §3 表的第一行就是它，但以前没有工具产这张 PNG，
            # 落到 build 就是 MISS（2026-11-07 page 32 实测：小图标改 rect 后全部缺图）。
            x0, y0, x1, y1 = [int(round(v)) for v in el["bbox"]]
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
            sub = img[y0:y1, x0:x1].astype(np.float32)
            alpha = np.ones(sub.shape[:2], dtype=np.float32)
            _emit(el, sub, alpha, outdir)
            print(f"  OK   {el['id']} rect {sub.shape[1]}x{sub.shape[0]}（整块留图，不透明）")
            continue

        if el.get("mask") == "feather":
            # 裁原图 + 逐边线性羽化（原包 crop-original-with-feathered-edges 的等价物）。
            # 用在：照片自带柔白边、插画带上下文要混进别的底色。
            # 关键差别：**颜色一个字节都不动**（羽化只改 α），所以不会像 matting 那样
            # 把柔边去掉、把深色主体判成背景。
            x0, y0, x1, y1 = [int(round(v)) for v in el["bbox"]]
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
            sub = img[y0:y1, x0:x1].astype(np.float32)
            h, w = sub.shape[:2]
            f = list(el.get("feather_px") or [0, 0, 0, 0])
            L, T, R, B = [max(0.0, float(v)) for v in (f + [0, 0, 0, 0])[:4]]
            alpha = np.ones((h, w), dtype=np.float32)
            if L:
                alpha = np.minimum(alpha, np.broadcast_to((np.arange(w) / L)[None, :], (h, w)))
            if R:
                alpha = np.minimum(alpha, np.broadcast_to(((w - 1 - np.arange(w)) / R)[None, :], (h, w)))
            if T:
                alpha = np.minimum(alpha, np.broadcast_to((np.arange(h) / T)[:, None], (h, w)))
            if B:
                alpha = np.minimum(alpha, np.broadcast_to(((h - 1 - np.arange(h)) / B)[:, None], (h, w)))
            alpha = np.clip(alpha, 0, 1)
            _emit(el, sub, alpha, outdir)
            corners = [float(alpha[0, 0]), float(alpha[0, -1]), float(alpha[-1, 0]), float(alpha[-1, -1])]
            need_zero = (L > 0 and T > 0) or (R > 0 and T > 0) or (L > 0 and B > 0) or (R > 0 and B > 0)
            ok = (max(corners) < 0.02) if need_zero else True
            print(f"  {'OK  ' if ok else 'FAIL'} {el['id']} feather {w}x{h} 羽化(L{L:g}T{T:g}R{R:g}B{B:g}) 角={[round(c, 2) for c in corners]}")
            if not ok:
                fails += 1
            continue

        if el.get("mask") == "rectmask":
            # 裁原图 + 逐边羽化 + 指定矩形内 α 置零（原包 crop-original-with-rect-exclusion-masks）。
            # 用在：素材框必然覆盖到别的元素（正文、标签），但被覆盖那一块不是主体。
            # 以前只能沿墨迹把一块图拆成两片，再额外验证接缝；现在一片裁片 + 排除矩形，
            # 主体一个像素都不损失，也不产生接缝。
            # 坐标用**页面像素** [x0,y0,x1,y1]，不用裁片局部坐标：局部原点随 bbox 变，
            # 人改框时没人重算，而这类错从产物上根本看不出来。
            rects = el.get("mask_rects") or []
            if not rects:
                print(f"  FAIL {el['id']} mask=rectmask 必须给 mask_rects:[[x0,y0,x1,y1],…]（页面像素）")
                fails += 1
                continue
            x0, y0, x1, y1 = [int(round(v)) for v in el["bbox"]]
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
            sub = img[y0:y1, x0:x1].astype(np.float32)
            h, w = sub.shape[:2]
            f = list(el.get("feather_px") or [0, 0, 0, 0])
            L, T, R, B = [max(0.0, float(v)) for v in (f + [0, 0, 0, 0])[:4]]
            alpha = np.ones((h, w), dtype=np.float32)
            if L:
                alpha = np.minimum(alpha, np.broadcast_to((np.arange(w) / L)[None, :], (h, w)))
            if R:
                alpha = np.minimum(alpha, np.broadcast_to(((w - 1 - np.arange(w)) / R)[None, :], (h, w)))
            if T:
                alpha = np.minimum(alpha, np.broadcast_to((np.arange(h) / T)[:, None], (h, w)))
            if B:
                alpha = np.minimum(alpha, np.broadcast_to(((h - 1 - np.arange(h)) / B)[:, None], (h, w)))
            alpha = np.clip(alpha, 0, 1)
            cleared, bad = 0, False
            for i, r in enumerate(rects):
                if len(r) != 4:
                    print(f"  FAIL {el['id']} mask_rects[{i}]={r} 不是 [x0,y0,x1,y1]")
                    fails += 1
                    bad = True
                    continue
                rx0, ry0, rx1, ry1 = [int(round(float(v))) for v in r]
                cx0, cy0 = max(0, rx0 - x0), max(0, ry0 - y0)
                cx1, cy1 = min(w, rx1 - x0), min(h, ry1 - y0)
                if cx1 <= cx0 or cy1 <= cy0:
                    # 与裁片不相交 = 坐标写错（写成局部坐标或首尾写反），必须拦，不能静默
                    print(f"  FAIL {el['id']} mask_rects[{i}]={r} 与裁框 {el['bbox']} 不相交"
                          f"——坐标必须是页面像素 [x0,y0,x1,y1]")
                    fails += 1
                    bad = True
                    continue
                alpha[cy0:cy1, cx0:cx1] = 0
                cleared += (cy1 - cy0) * (cx1 - cx0)
            if bad:
                continue
            _emit(el, sub, alpha, outdir)
            corners = [float(alpha[0, 0]), float(alpha[0, -1]), float(alpha[-1, 0]), float(alpha[-1, -1])]
            need_zero = (L > 0 and T > 0) or (R > 0 and T > 0) or (L > 0 and B > 0) or (R > 0 and B > 0)
            ok = (max(corners) < 0.02) if need_zero else True
            print(f"  {'OK  ' if ok else 'FAIL'} {el['id']} rectmask {w}x{h} "
                  f"羽化(L{L:g}T{T:g}R{R:g}B{B:g}) 排除{len(rects)}块/{cleared}px 角={[round(c, 2) for c in corners]}")
            if not ok:
                fails += 1
            continue

        if el.get("mask") == "flat":
            # 去纯底（原包 crop-and-remove-flat-background）：**只删与裁框边界连通**、且颜色
            # 接近底色的像素。内部白色高光不连通边界，所以不会被吃掉——这条正是
            # "白底上的浅色图标" 与色键的分界：色键按饱和度删，会把白色高光一起删。
            x0, y0, x1, y1 = [int(round(v)) for v in el["bbox"]]
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
            sub = img[y0:y1, x0:x1].astype(np.float32)
            h, w = sub.shape[:2]
            tol = float(el.get("flat_tol", 18))
            edge_tol = max(42.0, 2 * tol)
            bg = _hex2rgb(el.get("bg_color")) if el.get("bg_color") else \
                np.median(np.concatenate([sub[0], sub[-1], sub[:, 0], sub[:, -1]]), axis=0)
            d = np.linalg.norm(sub - bg[None, None, :], axis=2)
            allowed = (d <= edge_tol).astype(np.uint8)
            ff = allowed.copy()
            flood_mask = np.zeros((h + 2, w + 2), np.uint8)
            for yy in range(h):
                for xx in (0, w - 1):
                    if ff[yy, xx] == 1:
                        cv2.floodFill(ff, flood_mask, (xx, yy), 2)
            for xx in range(w):
                for yy in (0, h - 1):
                    if ff[yy, xx] == 1:
                        cv2.floodFill(ff, flood_mask, (xx, yy), 2)
            bg_px = ff == 2
            alpha = np.clip(1.0 - bg_px.astype(np.float32), 0, 1)
            alpha = np.clip(cv2.GaussianBlur(alpha, (3, 3), 0.6), 0, 1)
            # 排除矩形：去纯底只删"与边框连通"的底色，落在角上的邻接内容
            # （放射状光芒、相邻元素）删不掉，会露出直角。这里按页面坐标挖掉。
            for r in (el.get("mask_rects") or []):
                rx0, ry0, rx1, ry1 = [float(v) for v in r]
                ox0, oy0 = int(max(0, round(rx0) - x0)), int(max(0, round(ry0) - y0))
                ox1, oy1 = int(min(w, round(rx1) - x0)), int(min(h, round(ry1) - y0))
                if ox1 > ox0 and oy1 > oy0:
                    alpha[oy0:oy1, ox0:ox1] = 0
            _emit(el, sub, alpha, outdir)
            cov = float((alpha > 0.5).mean())
            hexbg = ''.join('%02X' % int(v) for v in bg)
            print(f"  {'OK  ' if 0.01 < cov < 0.95 else 'FAIL'} {el['id']} flat {w}x{h} 底色=#{hexbg} 保留={cov:.1%}")
            em = max(float(alpha[0].mean()), float(alpha[-1].mean()),
                     float(alpha[:, 0].mean()), float(alpha[:, -1].mean()))
            if em > 0.15:
                print("       注意：贴边还留着内容（可能是底盘边缘/相邻元素）——这类素材应改 pictogram，"
                      "或把 bbox 收进主体")
            if not 0.01 < cov < 0.95:
                fails += 1
            continue

        if el.get("mask") == "pictogram":
            # 蓝色图元隔离（原包 foreground_isolation: blue-pictogram 的等价物）：
            # **保留所有不贴裁框边的蓝色连通域**（多部件图标必须整组留：柱状图三根柱、
            # 主体+角标、躯干+血滴），贴边的蓝色一律当污染（邻接连接线/边框/文字）。
            # 孔洞填回：图标内部的白色高光不是蓝色，不填会变透明。
            x0, y0, x1, y1 = [int(round(v)) for v in el["bbox"]]
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
            sub = img[y0:y1, x0:x1].astype(np.float32)
            h, w = sub.shape[:2]
            R_, G_, B_ = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
            # 底盘带浅蓝圆环/浅蓝渐变时，环的 B-G 只有 ~20，会和主体连成一块并贴到框边
            # → 整块被当污染丢掉（2026-11-07 徽章实测：覆盖率 0.1%）。给一个可调下限：
            # picto_minbg 默认 12（纯白底），底盘偏蓝时提到 40 左右可把环与主体分开。
            minbg = float(el.get("picto_minbg", 12))
            fg = ((B_ >= 80) & (B_ - R_ >= 24) & (B_ - G_ >= minbg)).astype(np.uint8)
            ncc, lab, st, _ = cv2.connectedComponentsWithStats(fg, 8)
            keep = np.zeros_like(fg)
            kept = 0
            for i in range(1, ncc):
                x, y, bw, bh, area = st[i]
                touches = x == 0 or y == 0 or x + bw >= w or y + bh >= h
                if (not touches) and area >= 4:
                    keep[lab == i] = 1
                    kept += 1
            ff = keep.copy()
            holes = np.zeros((h + 2, w + 2), np.uint8)
            cv2.floodFill(ff, holes, (0, 0), 1)
            keep = np.clip(keep + (ff == 0).astype(np.uint8), 0, 1)
            alpha = np.clip(cv2.GaussianBlur(keep.astype(np.float32), (3, 3), 0.6), 0, 1)
            _emit(el, sub, alpha, outdir)
            cov = float((alpha > 0.5).mean())
            ok = kept >= 1 and 0.01 < cov < 0.92
            print(f"  {'OK  ' if ok else 'FAIL'} {el['id']} pictogram {w}x{h} 保留连通域={kept} 覆盖={cov:.1%}")
            if not ok:
                fails += 1
            continue

        if el.get("mask") == "duotone":
            # 单色前景 + 彩色渐变底（第 8 条）：图标是**一种颜色**（本页 #FFFFFF）、
            # 背景是**有色渐变**（蓝色页签上的白图标）。α 不必猜形状——它在
            # "底色 → 前景色" 那条线上做**正交投影**：
            #     α = clamp( <sub-bg, fg-bg> / |fg-bg|² , 0, 1 )
            # 为什么不复用其它三条：
            #   chroma    —— 按色相键控，蓝底上的**白色**键不出来（白没有色相）；
            #   flat      —— 只认纯色底，渐变带会留一圈半透明脏边；
            #   pictogram —— 它挑"蓝色主体"，而这里主体恰好是白色、底色才是蓝。
            # RGB 一律写成 fg（单一前景色），让 α 独自表达形状：这样换任何底色都不会露白边，
            # 也不会把原图里的蓝残留烤进素材（那是 duotone 最容易犯的错）。
            x0, y0, x1, y1 = [int(round(v)) for v in el["bbox"]]
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
            sub = img[y0:y1, x0:x1].astype(np.float32)
            h, w = sub.shape[:2]

            # ① 底色模型：逐行取左右 edge 条带的中位数，拟 deg 次多项式（吃掉竖向渐变）。
            #    为什么按行而不是整块取一个常数：胶囊本身有竖向明暗，一个常数会在亮的那头留脏雾。
            deg = int(el.get("duo_bg_deg", 3))
            edge = max(2, int(el.get("duo_bg_edge", 4)))
            strip = np.concatenate([sub[:, :edge], sub[:, -edge:]], axis=1)
            per_row = np.median(strip, axis=1)
            ys = np.arange(h, dtype=np.float32)
            bg = np.empty_like(sub)
            for c in range(3):
                bg[:, :, c] = np.polyval(np.polyfit(ys, per_row[:, c], deg), ys)[:, None]

            # ② 前景色：给了 duo_fg 就用；否则取最亮 1% 的均值（单色前景必然在最亮的那些像素里）
            lum = sub.mean(axis=2)
            fg = (_hex2rgb(el["duo_fg"]) if el.get("duo_fg")
                  else sub[lum >= np.percentile(lum, 99)].mean(axis=0))
            vec = fg[None, None, :] - bg
            alpha = np.clip(((sub - bg) * vec).sum(axis=2)
                            / np.maximum((vec * vec).sum(axis=2), 1e-6), 0, 1)
            # ③ 去底色噪声：投影对底色的轻微噪声会给出 0.02~0.1 的假 α，深底上就是一层脏雾。
            #    默认 0.16 而不是 0.12：角上底色是**多项式外推**出来的，偏差更大，实测 0.12 会留
            #    α=32~36/255 的假角（assetcheck 会报"方片底会露直角"），0.16 时四角全 0，
            #    且连通域数与不透明占比一字不变（2/10、50.2%/30.9%）。
            alpha[alpha < float(el.get("duo_noise", 0.16))] = 0
            rgb = np.broadcast_to(fg, alpha.shape + (3,)).astype(np.float32)
            _emit(el, rgb, alpha, outdir)

            # ④ 自检（三件，任一不过就 FAIL，不静默）
            #    a) 贴边不许有内容：框小了会把主体切断（与 circle 的角检查同一哲学）
            # 用 **max** 不用 mean：细射线被裁时只在边上留 3~4 个 α>0.5 像素，mean 会把它摊薄
            # 到 0.05 判 OK（实测 ic_bulb 的右缘 max=0.94 / mean=0.046）。这条自检存在的意义
            # 就是拦"细特征被裁"，用均值等于对它最该防的失效模式免疫。
            rim = max(float(alpha[0].max()), float(alpha[-1].max()),
                      float(alpha[:, 0].max()), float(alpha[:, -1].max()))
            #    b) 前景色纯度：α>0.5 的核心区里，平均色应贴近 fg（否则说明投影把底色调了进来）
            core = alpha > 0.5
            tint = (float(np.abs(sub[core] - fg).mean()) if core.sum() else 0.0)
            #    c) 连通域数（可给 expect_cc）：设计件数对不上＝有部件被吃掉或有多余脏点
            ncc, _, st, _ = cv2.connectedComponentsWithStats(core.astype(np.uint8), 8)
            cc = int(sum(1 for i in range(1, ncc) if st[i, cv2.CC_STAT_AREA] >= 6))
            want_cc = el.get("expect_cc")
            ok = rim <= 0.15 and tint <= float(el.get("duo_tint_max", 24))
            if want_cc is not None:
                ok = ok and cc == int(want_cc)
            print(f"  {'OK  ' if ok else 'FAIL'} {el['id']} duotone {w}x{h} "
                  f"fg=#{''.join('%02X' % int(v) for v in fg)} 覆盖={float(core.mean()):.1%} "
                  f"贴边α={rim:.3f} 色偏={tint:.1f} 连通域={cc}"
                  + (f"/期望{int(want_cc)}" if want_cc is not None else ""))
            if not ok:
                fails += 1
            continue

    _write_src_to_override(spec, Path(a.spec))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
