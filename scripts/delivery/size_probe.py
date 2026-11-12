#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""size_probe.py — 产物侧字号精确检查：pptx 里实际写了几号字，与 spec 期望逐块对账。

为什么需要它（这条判据的特殊性）：其他判据都在**参考图/测量**这一侧，都带误差、都要容差、
都在"差多少算差"上打官司。字号这条不用：`build` 把 `Pt(size_pt)` 写进
`a:rPr/@sz`（单位 1/100 pt，整数），读回来就是真值，误差只来自浮点。
所以这里**没有容差可配**——判据的全部价值就在于它不需要容差。

它还能抓一类别的检查抓不到的错：`build` 在缺 `size_pt` 时**静默退回默认字号**
（14pt / 18pt 两处），而 `specmerge` 对"解析不出字号"是 REFUSED 的。
也就是说：合并视图里没有 `size_pt` 的块会被画成一个没人声明过的字号，且当前无人报警。
本工具把这类块单独标成 `defaulted`。

用法：
  python size_probe.py --page <页目录> [--pptx <路径>]
退出码：0 全 match（WARN 不算失败）/ 1 有 mismatch / missing / defaulted / 2 输入有问题
只读，不写盘，不渲染，不需要 reference.png。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import consts       # noqa: E402  shape_id：成品里「这个形状是谁」的唯一取值处
import specmerge    # noqa: E402

# 只吸收浮点：@sz 是 1/100 pt 的整数，换算回 pt 后与 spec 的差最多是表示误差。
# 这不是"容差"，不需要也不允许做成参数。
FLOAT_EPS_PT = 0.005


def _iter_shapes(shapes):
    """展开组合形状：组的成员也要算，否则组里的文字块会整块漏检。"""
    for sh in shapes:
        yield sh
        if getattr(sh, "shapes", None) is not None:      # group shape
            yield from _iter_shapes(sh.shapes)


def _run_sizes_pt(shape) -> set[float]:
    """取这个形状里所有 run 的 `a:rPr/@sz`（÷100 得 pt）。没写 sz 的 run 不算进来。"""
    out: set[float] = set()
    try:
        vals = shape._element.xpath(".//a:rPr/@sz")
    except Exception:
        return out
    for v in vals:
        try:
            out.add(round(float(v) / 100.0, 4))
        except (TypeError, ValueError):
            continue
    return out


def expected_sizes(spec: dict) -> dict[str, float | None]:
    """期望字号表：文字块 → size_pt（None = 合并视图里没有，build 会走默认值）。"""
    want: dict[str, float | None] = {}
    for el in spec.get("elements", []) or []:
        if el.get("type") != "text":
            continue
        eid = el.get("id")
        if not eid:
            continue
        sz = el.get("size_pt")
        want[str(eid)] = None if sz is None else float(sz)
    return want


def probe(spec: dict, prs) -> dict:
    """逐块比对。纯函数（给定 spec 与 Presentation），便于测试。"""
    want = expected_sizes(spec)
    seen: dict[str, set[float]] = {}
    for slide in prs.slides:
        for sh in _iter_shapes(slide.shapes):
            sizes = _run_sizes_pt(sh)
            if not sizes:
                continue                      # 没有字号的形状（纯图形/图片）不参与
            seen[str(consts.shape_id(sh))] = sizes

    match, mismatch, missing, defaulted, unexpected = [], [], [], [], []
    for eid, exp in sorted(want.items()):
        got = seen.get(eid)
        if got is None:
            missing.append({"id": eid, "expected_pt": exp})
            continue
        if exp is None:
            # 合并视图里没有 size_pt → build 用了静默默认值，这个字号没人声明过
            defaulted.append({"id": eid, "in_pptx_pt": sorted(got)})
            continue
        bad = [g for g in got if abs(g - exp) > FLOAT_EPS_PT]
        if bad:
            mismatch.append({"id": eid, "expected_pt": exp, "actual_pt": sorted(got),
                             "delta_pt": [round(g - exp, 4) for g in sorted(got)]})
        else:
            match.append({"id": eid, "size_pt": exp})
    for name in sorted(set(seen) - set(want)):
        unexpected.append({"shape": name, "in_pptx_pt": sorted(seen[name])})

    fails = bool(mismatch or missing or defaulted)
    return {"match": match, "mismatch": mismatch, "missing": missing,
            "defaulted": defaulted, "unexpected": unexpected, "ok": not fails}


def pick_pptx(page: Path, explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    cand = sorted(page.glob("*.pptx"))
    if len(cand) == 1:
        return cand[0]
    return None          # 0 个或多个：让调用方报清楚，不替人猜


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--pptx", default=None, help="成品路径；不给则要求页目录下恰好一个 .pptx")
    ap.add_argument("--spec", default=None)
    a = ap.parse_args()

    from pptx import Presentation

    page = Path(a.page)
    spec_p = Path(a.spec) if a.spec else page / "spec.json"

    cand = sorted(page.glob("*.pptx"))
    pptx_p = pick_pptx(page, a.pptx)
    if pptx_p is None:
        if a.pptx:
            print(f"REFUSED: 找不到 pptx {a.pptx}", file=sys.stderr)
        else:
            print(f"REFUSED: {page} 下有 {len(cand)} 个 .pptx"
                  f"（{[p.name for p in cand]}）：请用 --pptx 指定哪一个才是成品",
                  file=sys.stderr)
        return 2

    try:
        spec = specmerge.load_merged(spec_p, quiet=True)
    except SystemExit as exc:
        print(f"REFUSED: spec 合并没有通过（{exc}）", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"REFUSED: 读不了 spec（{spec_p}）：{exc}", file=sys.stderr)
        return 2

    try:
        prs = Presentation(str(pptx_p))
    except Exception as exc:
        print(f"REFUSED: 打不开 pptx（{pptx_p}）：{exc}", file=sys.stderr)
        return 2

    res = probe(spec, prs)
    n_txt = len(expected_sizes(spec))
    print(f"\n=== 字号精确检查：{page.name} ===")
    print(f"pptx  = {pptx_p}")
    print(f"spec  = {spec_p}（合并视图，阶梯已覆盖）")
    print(f"文字块 {n_txt}：match {len(res['match'])} / mismatch {len(res['mismatch'])}"
          f" / missing {len(res['missing'])} / defaulted {len(res['defaulted'])}"
          f" / unexpected {len(res['unexpected'])}（WARN）")
    for m in res["mismatch"]:
        print(f"  FAIL mismatch {m['id']}: 期望 {m['expected_pt']:g}pt，"
              f"实际 {m['actual_pt']}pt，差 {m['delta_pt']}pt")
    for m in res["missing"]:
        print(f"  FAIL missing  {m['id']}: spec 里有这块，pptx 里找不到对应形状")
    for d in res["defaulted"]:
        print(f"  FAIL defaulted {d['id']}: 合并视图里没有 size_pt，"
              f"pptx 里是 {d['in_pptx_pt']}pt —— 来自 build 的静默默认值（14/18pt），没人声明过")
    for u in res["unexpected"]:
        print(f"  WARN unexpected {u['shape']}: pptx 有文字，spec 里没有这个 id")
    print(f"结论：{'PASS' if res['ok'] else 'FAIL'}"
          f"（不设容差：@sz 是 1/100pt 整数，只吸收浮点 {FLOAT_EPS_PT}pt）")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
