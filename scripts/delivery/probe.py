#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe.py — 读边、读色、读分段：把"点不了画布"时的临时扫描脚本收成一个入口。

为什么要有
----------
S1 量测是人读参考图写 spec，但"这条线切在哪 / 这块底色是 #F7F9FC 还是 #F2F5FA"
眼睛读不准也说不清；没有视觉的执行侧（脚本 / agent）更没得读。过去每页都现写一次性
扫描脚本——同一个"读一条线"在不同页被重写十几次，而且每次都要重新决定容差、
平滑窗口、圆角怎么避开。

这个工具只做三件确定性的事，不给建议、不写 spec：

  scan    沿一条线切色段。找分隔线、找渐变台阶、看某一行到底有几块。
  edges   给一个粗框，吸附到四条真实边界。找卡片 / 条带 / 图片的外框。
  color   读一块区域的填充：中位色、上下端色、是否渐变。

输出都是页面像素与 hex，可径直抄进 spec / override。

用法
----
  python probe.py scan  --page page --axis row --at 300 --from 0 --to 800 --tol 8
  python probe.py edges --page page --box 20,150,780,840 --search 40
  python probe.py color --page page --box 30,168,753,824

判据
----
  scan   相邻两列（行）中位色距离 > tol 即切段；短于 --min-run 的段并进左邻。
         先在垂直方向取中位，一行上的文字/噪点压不掉，所以 --band 给一段厚度。
  edges  沿目标边取 band（默认去掉两端 15% 以避开圆角），按列取中位色剖面；
         阶跃分数 = 两侧各 --win 宽的中位色距离；取峰值位置。
         峰值 < --min-step 标 weak（说明这里没有明确边界，别信这个数）。
  color  框内按行取中位色 → 上/下三分位中位色；两者距离 ≥ --grad-min 判渐变。

退出码：0 正常 / 2 输入有问题（框越界、线在画布外）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from consts import GRAD_MIN_DELTA as GRAD_MIN      # 判"是渐变"的最小端点差，只此一处定义
# 平滑实现已上移到 consts（原为本文件的 _smooth）。保留 _smooth 这个模块属性，是因为
# measure.py 有 `from probe import _smooth`；第 4 步 measure.py 退役后可改为直接 import consts。
from consts import smooth_profile as _smooth
DEFAULTS = dict(tol=8.0, min_run=6, band=24, win=6, search=40, min_step=15.0)


def _load(page: Path, image: str | None) -> np.ndarray:
    p = Path(image) if image else page / "reference.png"
    if not p.exists():
        raise SystemExit(f"REFUSED: 找不到参考图 {p}")
    return np.asarray(Image.open(p).convert("RGB")).astype(np.float32)


def _hex(c) -> str:
    return "#" + "".join("%02X" % int(round(min(255, max(0, v)))) for v in c)


def cmd_scan(a: np.ndarray, args) -> int:
    H, W = a.shape[:2]
    band = max(1, int(args.band))
    if args.axis == "row":
        y = int(args.at)
        if not 0 <= y < H:
            print(f"REFUSED: y={y} 在画布外（0..{H - 1}）")
            return 2
        y0, y1 = max(0, y - band // 2), min(H, y + band // 2 + 1)
        x0, x1 = max(0, int(args.frm)), min(W, int(args.to))
        prof = np.median(a[y0:y1, x0:x1], axis=0)
        base, head, width = x0, "x", W
    else:
        x = int(args.at)
        if not 0 <= x < W:
            print(f"REFUSED: x={x} 在画布外（0..{W - 1}）")
            return 2
        x0, x1 = max(0, x - band // 2), min(W, x + band // 2 + 1)
        y0, y1 = max(0, int(args.frm)), min(H, int(args.to))
        prof = np.median(a[y0:y1, x0:x1], axis=1)
        base, head, width = y0, "y", H

    prof = _smooth(prof, 3 if len(prof) > 8 else 1)
    n = len(prof)
    cuts = [0]
    for i in range(1, n):
        if float(np.linalg.norm(prof[i] - prof[i - 1])) > args.tol:
            cuts.append(i)
    cuts.append(n)

    runs = []
    for k in range(len(cuts) - 1):
        s, e = cuts[k], cuts[k + 1]
        if e - s < args.min_run and runs:      # 太短：并进左邻（抗锯齿/文字边缘）
            runs[-1][1] = e
            continue
        seg = prof[s:e]
        runs.append([s, e, seg])

    print(f"扫描 {'行' if args.axis == 'row' else '列'} {head}={args.at}  "
          f"范围 {base}..{base + n - 1}  tol={args.tol:g}  band={band}")
    print(f"{'起':>6s}{'止':>6s}{'宽':>6s}  {'中位色':9s}{'段内差':>7s}")
    for s, e, seg in runs:
        mid = np.median(seg, axis=0)
        d = float(np.linalg.norm(seg.max(0) - seg.min(0)))
        print(f"{base + s:>6d}{base + e - 1:>6d}{e - s:>6d}  {_hex(mid):9s}{d:7.1f}")
    if args.json:
        out = [{"start": base + s, "end": base + e - 1, "w": e - s,
                "hex": _hex(np.median(seg, axis=0)),
                "inner_delta": round(float(np.linalg.norm(seg.max(0) - seg.min(0))), 1)}
               for s, e, seg in runs]
        Path(args.json).write_text(json.dumps({"axis": args.axis, "at": args.at,
                                               "runs": out}, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n→ {args.json}")
    return 0


def edge_candidates(a: np.ndarray, box, side: str, win: int, search: int, topn: int = 3):
    """沿一条边搜阶跃候选。返回 [(坐标, 分数), ...]，已做非极大抑制。

    为什么要候选列表而不是一个数：一条带上常有两三条平行的强边界（卡右边、卡间背景、
    相邻卡的左边），分数几乎一样。自动只报一个数，等于替人猜"你想吸哪条"——
    而这个判断只能由看得见画面的人做。所以全列出来，附相距粗框边多远。
    """
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    H, W = a.shape[:2]
    if side in ("left", "right"):
        # 取中间 70% 的行，避开圆角
        pad = int((y1 - y0) * 0.15)
        ys, ye = max(0, y0 + pad), min(H, y1 - pad)
        coord = x0 if side == "left" else x1
        lo, hi = max(0, coord - search), min(W, coord + search)
        band = np.median(a[ys:ye, lo:hi], axis=0) if ye > ys else None
        base = lo
    else:
        pad = int((x1 - x0) * 0.15)
        xs, xe = max(0, x0 + pad), min(W, x1 - pad)
        coord = y0 if side == "top" else y1
        lo, hi = max(0, coord - search), min(H, coord + search)
        band = np.median(a[lo:hi, xs:xe], axis=1) if xe > xs else None
        base = lo
    if band is None or len(band) < 2 * win + 3:
        return None

    scored = []
    for i in range(win, len(band) - win):
        left = np.median(band[i - win:i], axis=0)
        right = np.median(band[i:i + win], axis=0)
        scored.append((float(np.linalg.norm(left - right)), base + i))
    if not scored:
        return []
    scored.sort(reverse=True)
    picked = []
    for score, coord in scored:
        if any(abs(coord - c) <= max(2 * win, 6) for c, _ in picked):   # 同一个峰的平台，不是第二个峰
            continue
        picked.append((coord, score))
        if len(picked) >= topn:
            break
    # 距离加权：粗框是人给的近似，真边界更可能落在附近。
    # 只按分数排会让"隔壁那块更强的边"抢走位置（实卡顶会吸到导航条下沿，差 50px）。
    # 权重用高斯，σ 取搜索半径的一半：偏出半径就基本出局。
    sigma = max(search / 2.0, 1.0)
    anchor = box[0] if side == "left" else box[1] if side == "top" else box[2] if side == "right" else box[3]
    picked.sort(key=lambda cs: cs[1] * math.exp(-abs(cs[0] - anchor) / sigma), reverse=True)
    return picked


def cmd_edges(a: np.ndarray, args) -> int:
    box = [float(v) for v in args.box.split(",")]
    if len(box) != 4:
        raise SystemExit("REFUSED: --box 要 x0,y0,x1,y1")
    H, W = a.shape[:2]
    want = [s.strip() for s in (args.sides or "left,top,right,bottom").split(",") if s.strip()]
    out = {"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}
    print(f"粗框 {[int(v) for v in box]}  搜索半径 ±{args.search}  窗口 ±{args.win}")
    weak = []
    pick = {}
    for kv in (args.pick or "").split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            pick[k.strip()] = int(v) - 1
    print(f"{'边':8s}{'原值':>7s}  候选（坐标 / 阶跃 / 距原值），按分数排：判据 strong={args.min_step:g}")
    for side in ("left", "top", "right", "bottom"):
        if side not in want:
            continue
        r = edge_candidates(a, box, side, int(args.win), int(args.search))
        orig = box[0] if side == "left" else box[1] if side == "top" else box[2] if side == "right" else box[3]
        if not r:
            print(f"{side:8s}{orig:>7.0f}  weak（band 太窄，框可能太小）")
            continue
        cells = []
        for coord, score in r:
            cells.append(f"{coord}(s={score:.0f},{coord - orig:+.0f})")
        idx = pick.get(side, 0)
        idx = max(0, min(idx, len(r) - 1))
        coord, score = r[idx]
        key = {"left": "x0", "top": "y0", "right": "x1", "bottom": "y1"}[side]
        if score >= args.min_step or args.allow_weak:
            note = ""
            if len(r) > 1 and r[1][1] >= args.min_step:
                gap = abs(coord - r[1][0])
                # 两条平行强边之间的缝（卡与卡之间 20~30px 很常见）：粗框落进缝里就无从判断
                # 该吸哪条。这时明说"有歧义"，比给一个看起来很确定的数有用。
                note = (f"  ⚠ 平行边 {coord} 与 {r[1][0]}（相距 {gap}px），"
                        f"粗框可能落在缝里，用 --pick {side}=2 取另一条") if gap < 60 \
                    else "  另一条边界：" + str(r[1][0])
            print(f"{side:8s}{orig:>7.0f}  " + "  ".join(cells) + f"   → 取 {coord}  ok{note}")
            out[key] = coord
        else:
            # 低对比边界（卡与底色差 10~14）在像素上就没有阶跃，硬吸只会吸到隔壁的强边。
            # 这时候保留原值比给一个错的数有用——错的数会被当成测量结果写进 spec。
            print(f"{side:8s}{orig:>7.0f}  " + "  ".join(cells)
                  + f"   → 保留 {int(orig)}  weak（最高阶跃 {score:.0f} < {args.min_step:g}，此处无明确边界）")
            out[key] = orig
            weak.append(side)
    bbox = [out["x0"], out["y0"], out["x1"], out["y1"]]
    print(f"\nbbox = [{', '.join(str(int(round(v))) for v in bbox)}]  "
          f"({bbox[2] - bbox[0]:.0f}x{bbox[3] - bbox[1]:.0f})"
          + (f"   未吸附：{', '.join(weak)}（沿用粗框值）" if weak else ""))
    if weak:
        print("  这几条边在像素上没有阶跃（卡与底色差 10~14 就属于这种），"
              "用 scan 读剖面定，或按视觉判断。")
    if args.json:
        Path(args.json).write_text(json.dumps({"bbox": [int(round(v)) for v in bbox]},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ {args.json}")
    return 0


def cmd_color(a: np.ndarray, args) -> int:
    x0, y0, x1, y1 = [int(round(float(v))) for v in args.box.split(",")]
    H, W = a.shape[:2]
    x0, x1 = max(0, min(x0, x1)), min(W, max(x0, x1))
    y0, y1 = max(0, min(y0, y1)), min(H, max(y0, y1))
    if x1 - x0 < 4 or y1 - y0 < 4:
        print("REFUSED: 框太小（至少 4x4）")
        return 2
    sub = a[y0:y1, x0:x1]
    rows = np.median(sub, axis=1)
    third = max(1, len(rows) // 3)
    top = np.median(rows[:third], axis=0)
    bot = np.median(rows[-third:], axis=0)
    mid = np.median(rows, axis=0)
    delta = float(np.linalg.norm(top - bot))
    kind = "gradient" if delta >= args.grad_min else "flat"
    print(f"区域 ({x0},{y0})-({x1},{y1})  {x1 - x0}x{y1 - y0}")
    print(f"  fill_mid  {_hex(mid)}")
    print(f"  top       {_hex(top)}")
    print(f"  bottom    {_hex(bot)}")
    print(f"  上下差    {delta:.1f}   → {kind}"
          + ("" if kind == "flat" else "（写 gradient 时用这组端点）"))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"bbox": [x0, y0, x1, y1], "fill_mid": _hex(mid), "top": _hex(top),
             "bottom": _hex(bot), "delta": round(delta, 1), "kind": kind},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ {args.json}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="读边 / 读色 / 读分段")
    ap.add_argument("cmd", choices=["scan", "edges", "color"])
    ap.add_argument("--page", default="page")
    ap.add_argument("--image", default=None)
    ap.add_argument("--axis", default="row", choices=["row", "col"])
    ap.add_argument("--at", type=int, default=0)
    ap.add_argument("--from", dest="frm", type=int, default=0)
    ap.add_argument("--to", type=int, default=0)
    ap.add_argument("--tol", type=float, default=DEFAULTS["tol"])
    ap.add_argument("--min-run", type=int, default=DEFAULTS["min_run"])
    ap.add_argument("--band", type=int, default=DEFAULTS["band"])
    ap.add_argument("--box", default="")
    ap.add_argument("--sides", default="")
    ap.add_argument("--win", type=int, default=DEFAULTS["win"])
    ap.add_argument("--search", type=int, default=DEFAULTS["search"])
    ap.add_argument("--min-step", type=float, default=DEFAULTS["min_step"])
    ap.add_argument("--pick", default="", help="right=2 指定取该边的第 2 个候选")
    ap.add_argument("--allow-weak", action="store_true", help="连弱边也采信（默认保留粗框值）")
    ap.add_argument("--grad-min", type=float, default=GRAD_MIN)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    page = Path(args.page).resolve()
    a = _load(page, args.image)
    H, W = a.shape[:2]
    if args.cmd == "scan":
        if not args.to:
            args.to = W if args.axis == "row" else H
        return cmd_scan(a, args)
    if args.cmd == "edges":
        if not args.box:
            raise SystemExit("REFUSED: edges 要给 --box x0,y0,x1,y1（粗框即可）")
        return cmd_edges(a, args)
    if not args.box:
        raise SystemExit("REFUSED: color 要给 --box x0,y0,x1,y1")
    return cmd_color(a, args)


if __name__ == "__main__":
    sys.exit(main())
