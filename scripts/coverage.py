"""覆盖率：参考图上**有哪些墨迹没有被任何 spec bbox 接住**。

这是唯一一条能抓"整块漏做"的判据。为什么不用 OCR：漏掉「治疗」的根因就是 OCR 没识别出那块，
用 OCR 当基准等于用"看漏的东西"去查"看漏的东西"。

做法（三步，顺序不能反）：
1. **估背景**：大核中值滤波。细笔画的文字/线条在中值核里被吃掉，剩下的就是底色（渐变页也成立）。
2. **墨迹掩码** = 与背景的通道差 > 阈值。抗锯齿边缘会带一点差，靠阈值和 3px 膨胀吸收。
3. **残差** = 墨迹 −（∪所有 spec bbox 膨胀 3px）− 背景白名单；连通域 ≥ `min_frac × W × H` 才报。

`min_frac` 默认 0.0005（1672×941 → 787px²，约 28×28）。**"漏块 = 0" 不可达**：
抗锯齿、渐变噪声、字体渲染差都会留小残差，所以判据是"没有**成块**的残差"，不是"像素级为 0"。

背景白名单是一份人写的矩形列表（`--whitelist`）。它为什么必须人写：哪些残差是"本来就不该重建的
背景装饰"（见 SKILL §4 第 2 条），机器判不了——那是一句话的意图，不是像素特征。**没人写就一份都不放行**。

用法：
    python scripts/coverage.py --page <页目录> [--out <页>/_check/coverage.json]
    python scripts/coverage.py --page <页目录> --whitelist <白名单.json> --check
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

MIN_FRAC = 0.0005   # 残差连通域面积门槛（占画布比例）；写死在这里，改了要在报告里体现
# 与背景的通道差阈值。18 → 40 是量出来的，不是拍的：实测 49 页背景渐变带的对比度 max 31 / p99 29，
# 而同页文字区 max 226 / p99 163 —— 取 40 正好卡在"背景软边"与"真文字"之间，
# 高的那侧留 4 倍余量。阈值低于 35 会把渐变带的软边当成漏块（第一版就踩了）。
INK_DIFF = 40
DILATE = 3          # spec bbox 外扩（px）：bbox 通常贴着墨迹，不外扩会把字的抗锯齿边算成漏块
EDGE = 14           # 画布四边忽略带（px）：页边阴影/圆角抗锯齿是渲染痕迹，不是内容


def estimate_bg(rgb: np.ndarray) -> tuple[np.ndarray, dict]:
    """估背景：两侧页边距的逐行中位数；边距不干净的行退回中值滤波。

    **左右分开判**：第一版把两侧 40 列混在一起算 std，只要一侧干净就被判成"干净"，
    另一侧压着内容也发现不了（实测 46 页右页边压着紫蓝横带，左页边是干净渐变，混算 std=12.5 → 误判干净）。
    两个尺度的阈值都是量出来的：9 页每侧 row_std 落在 10~17（渐变噪声），25 以上才说明边距里混进了东西。

    中值滤波为什么只当兜底：中值核会把**比核大的实心块**吸进背景（60×60 实心块在 9 核下只剩 68px 边缘墨迹），
    那正好是"整块漏做"要抓的情形。页边距逐行中位数没有这个毛病——内容不画到页边。
    """
    h, w = rgb.shape[:2]
    med = cv2.medianBlur(rgb, 9)
    band, probe = EDGE, 20
    lcols = list(range(band, min(band + probe, w)))
    rcols = list(range(max(band, w - band - probe), w - band))
    out = med.astype(np.int16).copy()
    ok_rows = np.zeros(h, bool)
    if lcols and rcols:
        lmed = np.median(rgb[:, lcols, :].astype(np.int16), axis=1)
        rmed = np.median(rgb[:, rcols, :].astype(np.int16), axis=1)
        lstd = rgb[:, lcols, :].astype(np.int16).reshape(h, -1).std(axis=1)
        rstd = rgb[:, rcols, :].astype(np.int16).reshape(h, -1).std(axis=1)
        ok_rows = (lstd < 25) & (rstd < 25)
        row_med = (lmed + rmed) / 2.0
        out[ok_rows] = row_med[ok_rows][:, None, :]

    # 连续退化段：报告要能说"哪几行不可信"，不是一个数字
    ranges, start = [], None
    for y, good in enumerate(ok_rows):
        if not good and start is None:
            start = y
        elif good and start is not None:
            ranges.append([start, y - 1]); start = None
    if start is not None:
        ranges.append([start, h - 1])
    ranges = [r for r in ranges if r[1] - r[0] >= 3]      # 3 行以内的抖动不算段
    return np.clip(out, 0, 255).astype(np.uint8), {
        "bg_fallback_rows": int((~ok_rows).sum()),
        "bg_fallback_ranges": ranges,
    }


def product_ids(page: Path, out_name: str = "out.pptx") -> tuple[set[str] | None, str]:
    """成品里**实际存在**的对象 id 集合；(None, 原因) 表示没有可读成品。

    为什么必须问成品（2026-11-10）：spec 里的 bbox 是"打算画什么"，不是"画出来了什么"。
    把声明框直接当成已覆盖，等于让一个从未产出的对象替自己作证——
    OCR 找到但人还没选用的候选素材（`review:true` 且无 `src`/无 `mask`）、
    生成失败没有 PNG 的素材、因为 build MISS 根本没进 pptx 的对象，
    都会用自己那块框把原图上的内容残差擦掉，报"没有成块残差"。
    这就是"越没做越绿"——判据比不判更危险。

    取值走 `consts.shape_id`（cNvPr@title 优先，否则 name），与 presence 闸同一处实现，
    否则 spec 起了自定义 name 的页会在这里全被判成"没产出"。
    """
    p = Path(page) / out_name
    if not p.exists():
        return None, f"缺 {out_name}（没有成品可比）"
    try:
        from pptx import Presentation
        from consts import shape_id
        prs = Presentation(str(p))
        return {shape_id(sh) for sh in prs.slides[0].shapes}, "pptx"
    except Exception as exc:                     # noqa: BLE001
        return None, f"成品读不出来：{exc}"


def spec_boxes(spec: dict, keep: set[str] | None = None) -> list[list[float]]:
    """spec 里**能算作已覆盖**的框。

    keep 给了就严格按它过滤（只留成品里真有的对象）；没给（没有成品）时退回结构声明，
    但**素材候选与没有 src 的素材一律不算**——它们还停在"打算做"的状态。
    """
    out = []
    # assets 必须算进来：素材（人物/图标/裁片）是 spec 声明过的对象，
    # 只算 containers+elements 会让每个带插画的页面永远报"漏做"（2026-11-10 实测：
    # 50 页 9 块残差里 1 块整片是人物插画，逼人把素材当"背景"塞进白名单）。
    for seg in ("containers", "elements", "assets"):
        for it in spec.get(seg) or []:
            if not isinstance(it, dict) or not it.get("bbox"):
                continue
            eid = str(it.get("id") or "")
            if keep is not None:
                if eid not in keep:
                    continue
            elif seg == "assets":
                # 候选 = 明确要求 review 且既没 src 也没显式 mask → 只是"提款"，没做过
                if it.get("review") and not it.get("src") and not it.get("mask"):
                    continue
                if not it.get("src"):
                    continue
            bb = [float(v) for v in it["bbox"]]
            if len(bb) == 4 and bb[2] > bb[0] and bb[3] > bb[1]:
                out.append(bb)
    return out


def skipped_boxes(spec: dict, keep: set[str] | None) -> list[dict]:
    """声明了但没算进覆盖的框 —— 报告里要点名，否则人看不出"为什么这块没被盖"。"""
    used = spec_boxes(spec, keep)
    out = []
    for seg in ("containers", "elements", "assets"):
        for it in spec.get(seg) or []:
            if not isinstance(it, dict) or not it.get("bbox"):
                continue
            bb = [float(v) for v in it["bbox"]]
            if len(bb) != 4 or bb[2] <= bb[0] or bb[3] <= bb[1]:
                continue
            if bb in used:
                continue
            out.append({"id": str(it.get("id") or ""), "section": seg,
                        "why": ("不在成品里" if keep is not None else "未产出（候选/无 src）")})
    return out


def whitelist_mask(rects: list[list[float]], shape) -> np.ndarray:
    m = np.zeros(shape[:2], np.uint8)
    for r in rects or []:
        if len(r) != 4:
            continue
        x0, y0, x1, y1 = [int(v) for v in r]
        m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 1
    return m.astype(bool)


def run(page: Path, whitelist_path: Path | None, min_frac: float, ink_diff: int) -> dict:
    # 必须读**合并视图**：fastcut 把生成的 src 写进 override.json，spec.json 保持只读。
    # 只读 spec 会让"已经产出的素材"看起来永远没有 src → 被当成候选排除掉（实测 bug）。
    import specmerge
    spec = specmerge.load_merged(page / "spec.json", quiet=True)
    ref_p = page / "reference.png"
    # cv2.imread 在 Windows 上读不了非 ASCII 路径（中文目录名直接返回 None），
    # 先用 np.fromfile 读字节再 imdecode——这是本机目录全是中文的必须写法。
    buf = np.fromfile(str(ref_p), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"读不到参考图：{ref_p}")
    H, W = img.shape[:2]

    bg, bg_info = estimate_bg(img)
    diff = np.abs(img.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    ink = diff > ink_diff

    # 只有**成品里实际存在**的对象才算已覆盖（2026-11-10，见 product_ids 的注释）
    keep, basis = product_ids(page)
    boxes = spec_boxes(spec, keep)
    skipped = skipped_boxes(spec, keep)

    covered = np.zeros((H, W), np.uint8)
    for x0, y0, x1, y1 in boxes:
        cv2.rectangle(covered, (int(x0) - DILATE, int(y0) - DILATE),
                      (int(x1) + DILATE, int(y1) + DILATE), 1, -1)
    covered = covered.astype(bool)

    white = whitelist_mask((json.loads(whitelist_path.read_text(encoding="utf-8")).get("rects")
                            if whitelist_path else []), img.shape)
    # 判据无基准的行：整行没有任何 spec bbox。这些行**判不了**有没有漏做——
    # 一个跨满整行的漏块在这里会和背景长得一模一样（实测：整幅横带铺满画布时，两侧页边也变成那个颜色，
    # 背景估计直接把带子当成底色，残差为 0，报"没有成块残差"）。不制造假绿，就把"判不了"说出来。
    row_has_box = covered.any(axis=1)
    blind_rows = int((~row_has_box).sum())
    blind_ranges, st = [], None
    for y, good in enumerate(row_has_box):
        if not good and st is None:
            st = y
        elif good and st is not None:
            blind_ranges.append([st, y - 1]); st = None
    if st is not None:
        blind_ranges.append([st, H - 1])
    blind_ranges = [r for r in blind_ranges if r[1] - r[0] >= 5]

    residual = ink & ~covered & ~white
    if EDGE > 0:                              # 页边抗锯齿 / 阴影：结构上不可能承载内容
        residual[:EDGE, :] = residual[-EDGE:, :] = False
        residual[:, :EDGE] = residual[:, -EDGE:] = False

    n, lab, stats, cent = cv2.connectedComponentsWithStats(residual.astype(np.uint8), connectivity=8)
    thr_area = min_frac * W * H
    blocks = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area >= thr_area:
            blocks.append({"bbox": [int(x), int(y), int(x + w), int(y + h)],
                           "area": int(area), "area_frac": round(float(area) / (W * H), 5),
                           "cx": round(float(cent[i][0]), 1), "cy": round(float(cent[i][1]), 1)})
    blocks.sort(key=lambda b: -b["area"])

    # 三态，和 assetcheck 的 unproven 同一套逻辑：
    #   没有块                      → pass
    #   有块 + 有白名单（背景已定义） → fail（该人看的都看过了，剩下的就是漏做）
    #   有块 + 没白名单（背景未定义） → unproven（**不是失败**：机器分不出插画和漏做，只有人能定义）
    # 这一条是刻意的：若"没白名单"直接判红，9 页会全红，人会开始乱签白名单来换绿——
    # 那正是 cover 时代"开 locked 换过闸"的老路。宁可标 unproven，也不制造一个靠签字消掉的失败。
    if not blocks:
        status = "pass"
    elif whitelist_path:
        status = "fail"
    else:
        status = "unproven"

    return {
        "stage": "coverage", "kind": "coverage",
        "status": status,
        "page": page.name, "canvas": [W, H],
        "method": "ink mask (median-blur bg) − dilated spec bboxes − whitelist",
        "params": {"ink_diff": ink_diff, "min_frac": min_frac, "dilate_px": DILATE, "edge_ignore_px": EDGE,
                   "bg_fallback_rows": bg_info["bg_fallback_rows"],
                   "min_area_px": round(thr_area, 1)},
        # 覆盖框的来源必须写在报告里：同一张图，"按成品算"与"按声明算"会得出两个结论，
        # 看不出用的是哪一个，就无法解释两次运行为什么不一样。
        "boxes_basis": {"mode": "pptx" if keep is not None else "spec-only",
                        "reason": basis,
                        "n_used": len(boxes),
                        "n_declared": len(boxes) + len(skipped),
                        "skipped": skipped[:20]},
        "spec_boxes": len(boxes),
        "whitelist_rects": 0 if not whitelist_path else len(json.loads(
            whitelist_path.read_text(encoding="utf-8")).get("rects") or []),
        "ink_px": int(ink.sum()),
        "residual_px": int(residual.sum()),
        "residual_frac": round(float(residual.sum()) / (W * H), 5),
        "blocks": blocks,
        "n_blocks": len(blocks),
        "bg_fallback_ranges": bg_info["bg_fallback_ranges"],
        "unjudgeable": {
            "rows": blind_rows,
            "frac": round(blind_rows / H, 4) if H else 0.0,
            "ranges": blind_ranges,
            "why": "这些行整行没有 spec bbox，漏块在这里与背景同形，判不了",
        },
        "verdict_hint": {
            "pass": ("没有成块残差" if blind_rows / max(H, 1) <= 0.10
                     else f"没有成块残差，但 {blind_rows / H:.0%} 的行无基准——别当成整页验过"),
            "fail": f"{len(blocks)} 块残差 ≥ {thr_area:.0f}px²，白名单之外的墨迹应视为漏做",
            "unproven": (f"{len(blocks)} 块残差，但**没给背景白名单**——机器分不出插画与漏做，"
                         f"先跑 --suggest-whitelist 逐块确认"
                         + (f"；另有 {len(skipped)} 个声明对象没进成品、其框未被计入"
                            f"（{', '.join(s['id'] for s in skipped[:6])}）" if skipped else "")),
        }[status],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--whitelist", default=None, help="人写的背景白名单（{\"rects\": [[x0,y0,x1,y1], …]}）")
    ap.add_argument("--min-frac", type=float, default=MIN_FRAC)
    ap.add_argument("--ink-diff", type=int, default=INK_DIFF)
    ap.add_argument("--check", action="store_true", help="有残差块时 rc=1（供 cycle 转成 blocking to_fix）")
    ap.add_argument("--suggest-whitelist", action="store_true",
                    help="把残差块写成**候选**白名单草稿（机器生成，必须人逐块确认）")
    args = ap.parse_args()

    page = Path(args.page)
    rep = run(page, Path(args.whitelist) if args.whitelist else None, args.min_frac, args.ink_diff)
    out = Path(args.out) if args.out else page / "_check" / "coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    p = rep["params"]
    print(f"{rep['page']}：墨迹 {rep['ink_px']}px（{rep['ink_px']/(rep['canvas'][0]*rep['canvas'][1]):.1%}）"
          f" · 残差 {rep['residual_px']}px（{rep['residual_frac']:.2%}）")
    print(f"覆盖框 {rep['spec_boxes']} 个（外扩 {p['dilate_px']}px）· 白名单 {rep['whitelist_rects']} 块"
          f" · 门槛 {p['min_area_px']:.0f}px²")
    bb = rep.get("boxes_basis") or {}
    print(f"覆盖框来源：{bb.get('mode')}（用 {bb.get('n_used')} / 声明 {bb.get('n_declared')}）"
          + (f" · 未计入 {len(bb.get('skipped') or [])} 个" if bb.get("skipped") else ""))
    if rep["blocks"]:
        print(f"\n{rep['n_blocks']} 块残差（按面积）：")
        for b in rep["blocks"][:12]:
            print(f"  {str(b['bbox']):26s} {b['area']:6d}px  {b['area_frac']:.3%}")
    if args.suggest_whitelist and rep["blocks"]:
        cand = out.parent / "coverage.whitelist.candidate.json"
        cand.write_text(json.dumps({
            "_status": "机器生成的候选，尚未批准",
            "_为什么不能直接用": "残差里混着两类东西：真的漏做，和本来就不重建的背景插画（SKILL §4 第 2 条）。"
                              "机器分不出这两类——那需要知道设计意图。逐块看，是背景就留下、是内容就删掉并回去补。",
            "_批准动作": "人工确认后把留下的矩形抄进自己的 whitelist.json，并在 reviewed_by/reviewed_on 写清",
            "rects": [b["bbox"] for b in rep["blocks"]],
            "areas": [b["area"] for b in rep["blocks"]],
            "n": len(rep["blocks"]),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"候选白名单（{len(rep['blocks'])} 块，待你逐块确认）：{cand}")
    print(f"结论：[{rep['status']}] {rep['verdict_hint']}  →  {out}")
    # unproven 不阻塞（同 assetcheck）：没定义背景≠失败，只是没证明
    return 1 if (args.check and rep["status"] == "fail") else 0


if __name__ == "__main__":
    raise SystemExit(main())
