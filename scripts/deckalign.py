"""deck 级全局件对齐：先出报告，再谈改。

本文件当前只实现**报告**（P1a 的 1.2）：读每页 `spec.json` 的全局件 bbox，按 `deck.json` 的角色
取件，与**中位数**比，算出逐页 × 逐件的 Δpx，写 `align.json`。**不改任何 pptx、不写回 spec**。

为什么不拿"第一页"当基准：第一页自己就可能偏；中位数不会被单页带跑，且**可逐页反推撤销**
（报告里每个 Δ 都能反算回原值）。

**本工具只读**：写入统一走 `impose.py`（规范在 `visual.json`）。这里只负责量偏差、出报告、做判据。
量而不改，是因为"偏离多少"要能被独立复核；改的那一侧只有一处，才不会出现两套规则。
对齐后的代价是"这一页偏离本页截图"（实测 45 号页要偏 42px），那是**取舍**，不是 bug 修复。

用法：
    python scripts/deckalign.py --deck <整套>/deck.json --root <整套> --out <整套>/align.json
判据：`range_px` 与手算的全局件极差一致；`max_abs_delta` 能指到具体页与件。
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import roles

# 判据件：只有 nav（导航条容器）。
# 其它件**看着像全局件，实际不是**，各自被语义钉死在一个会变的位置上：
#   · nav_pill  位置 = 当前激活的是哪一节。对齐它等于让 9 页高亮同一节 —— 语义错误，不是不一致
#   · div[]     分隔线夹在标签之间，x 由每个标签的文字宽度决定（实测 x0 跨页极差 1267px）
#   · deco_l/r  标题两侧横线随标题宽度伸缩（极差 260px）
#   · footer    宽度是文本宽度，比它等于比字号
# 把它们塞进"极差"会得到一个吓人但无意义的数，然后逼人去"对齐"一个不该对齐的东西。
ALIGN_PARTS = ("nav",)
# 判据只看**位置**（左边缘 / 上边缘）。宽高不判，因为导航条的宽高是"跟着标签文字走"的：
# 9 页实测宽度 1552~1604（差 52）、高度 78~102（差 24），而这 9 页的 6 个标签字都是同一批字。
# 要把宽高拉平，只能改字号或字距去凑字宽——那正是 SKILL 明令禁止的那件事。
# 所以：位置 = 0 是本判据，宽高进参考（报了让人看见，但不判死）。
JUDGED_FIELDS = ("x0", "y0")
REFERENCE_PARTS = ("nav_pill", "div", "deco_l", "deco_r")
COMPARE_PARTS = ALIGN_PARTS + REFERENCE_PARTS
FIELDS = ("x0", "y0", "x1", "y1")


def _bbox_map(spec: dict) -> dict[str, list[float]]:
    """id → bbox。容器段与元素段都要（nav 是容器、footer 是文字块）。"""
    out: dict[str, list[float]] = {}
    for seg in ("containers", "elements"):
        for item in spec.get(seg) or []:
            if isinstance(item, dict) and item.get("id") and item.get("bbox"):
                bb = [float(v) for v in item["bbox"]]
                if len(bb) == 4 and any(bb):        # [0,0,0,0] 的 freeform 是占位，跳过
                    out[str(item["id"])] = bb
    return out


def collect(deck: dict, root: Path) -> dict[str, dict[str, list[list[float]]]]:
    """{part: {page: [bbox, ...]}}。div/deco 这类多个的按声明顺序保持。"""
    got: dict[str, dict[str, list[list[float]]]] = {p: {} for p in COMPARE_PARTS}
    for page in deck["pages"]:
        spec_p = Path(root) / page / "spec.json"
        if not spec_p.exists():
            continue
        bb = _bbox_map(json.loads(spec_p.read_text(encoding="utf-8")))
        decl = deck["roles"].get(page) or {}
        for part in COMPARE_PARTS:
            got[part][page] = [bb[i] for i in (decl.get(part) or []) if i in bb]
    return got


def _as_fields(bb: list[float]) -> dict[str, float]:
    return dict(zip(FIELDS, bb))


def build_report(deck: dict, root: Path) -> dict:
    got = collect(deck, root)
    pages = [p for p in deck["pages"]]

    # 基准：每件每字段取**中位数**（只用声明了这一件的页；空数组的页不参与，也不被惩罚）
    median: dict[str, list[dict[str, float]]] = {}
    for part in COMPARE_PARTS:
        n = max((len(v) for v in got[part].values()), default=0)
        median[part] = []
        for i in range(n):
            vals = {f: [] for f in FIELDS}
            for page in pages:
                lst = got[part].get(page) or []
                if i < len(lst):
                    for f, v in _as_fields(lst[i]).items():
                        vals[f].append(v)
            median[part].append({f: (statistics.median(vals[f]) if vals[f] else None) for f in FIELDS})

    deltas: dict[str, dict[str, list[dict]]] = {part: {} for part in COMPARE_PARTS}
    worst = {"abs_px": 0.0, "page": None, "part": None, "index": None, "field": None}
    field_range: dict[str, dict[str, float]] = {}

    for part in COMPARE_PARTS:
        for f in FIELDS:
            vals = []
            for page in pages:
                for i, bb in enumerate(got[part].get(page) or []):
                    m = median[part][i][f]
                    if m is None:
                        continue
                    vals.append(bb[FIELDS.index(f)])
            if vals:
                field_range[f"{part}.{f}"] = {"min": min(vals), "max": max(vals),
                                              "range": round(max(vals) - min(vals), 1)}

        for page in pages:
            rows = []
            for i, bb in enumerate(got[part].get(page) or []):
                m = median[part][i]
                row = {"i": i, "bbox": [round(v, 1) for v in bb]}
                for f, v in _as_fields(bb).items():
                    if m.get(f) is None:
                        continue
                    d = round(v - m[f], 1)
                    row["d" + f] = d             # dx0 / dy0 / dx1 / dy1
                    if part in ALIGN_PARTS and abs(d) > worst["abs_px"]:
                        worst = {"abs_px": abs(d), "page": page, "part": part,
                                 "index": i, "field": f}
                rows.append(row)
            deltas[part][page] = rows

    # 极差只取判据件（nav）；参考件的 range 照样报，但不参与判据
    judged = {k: v for k, v in field_range.items()
              if k.split(".")[0] in ALIGN_PARTS and k.split(".")[-1] in JUDGED_FIELDS}
    # pill 越出 nav 边框多少：这不是"不一致"，但它会让视觉上出现 2~4px 的错位，
    # 属于该被看见的事实。9 页里若普遍存在，说明 pill 高度是独立摆的，不是 nav 的内缩。
    pill_over = {}
    for page in pages:
        nav = (got["nav"].get(page) or [])
        pill = (got["nav_pill"].get(page) or [])
        if nav and pill:
            n, q = nav[0], pill[0]
            pill_over[page] = {
                "left": round(max(0.0, n[0] - q[0]), 1),
                "top": round(max(0.0, n[1] - q[1]), 1),
                "right": round(max(0.0, q[2] - n[2]), 1),
                "bottom": round(max(0.0, q[3] - n[3]), 1),
            }

    rng_key = max(judged, key=lambda k: judged[k]["range"]) if judged else None
    return {
        "stage": "deckalign", "kind": "align",
        "baseline": "median",           # 写死：换基准等于换结论，必须显式改这一行
        "deck_visual": deck.get("visual"),
        "pages": pages,
        "median": {p: [{f: (None if m.get(f) is None else round(m[f], 1)) for f in FIELDS}
                       for m in median[p]] for p in COMPARE_PARTS},
        "deltas": deltas,
        "summary": {
            "range_px": judged[rng_key]["range"] if rng_key else None,
            "range_on": rng_key,                      # 极差出在哪个件哪个字段——不写就是黑箱
            "judged_fields": list(JUDGED_FIELDS),
            "why_not_size": "宽高随标签文字宽度变化；拉平它只能改字号/字距凑字宽（SKILL §2 禁止）",
            "max_abs_delta": worst,
            "parts_judged": list(ALIGN_PARTS),
            "parts_reference_only": list(REFERENCE_PARTS),
            "why_reference_only": "pill 位置跟激活项、div 随标签宽、deco 随标题宽、footer 随文本宽——对齐它们会改语义",
            "footer_excluded": "footer 宽度由文本决定，比它等于比字号",
        },
        "field_range": field_range,
        "pill_outside_nav": pill_over,
    }



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--check", action="store_true",
                    help="判据模式：判据件极差 > --tol 时 rc=1（只判不改）")
    ap.add_argument("--tol", type=float, default=2.0, help="极差容差（px），默认 2")
    args = ap.parse_args()

    deck = roles.load_deck(Path(args.deck))
    rep = build_report(deck, Path(args.root))
    out = Path(args.out) if args.out else Path(args.root) / "align.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    s = rep["summary"]
    print(f"{'页':34s} {'nav max|Δ|':>11s}   （参考件不参与判据）")
    for page in deck["pages"]:
        rows = rep["deltas"]["nav"].get(page) or []
        mx = max((max(abs(r.get("dx0", 0)), abs(r.get("dy0", 0)),
                      abs(r.get("dx1", 0)), abs(r.get("dy1", 0))) for r in rows), default=0.0)
        print(f"{page:34s} {mx:11.1f}")
    print(f"\n极差 {s['range_px']}px（{s['range_on']}）"
          f" · max|Δ| {s['max_abs_delta']['abs_px']}px"
          f"（{s['max_abs_delta']['part']}[{s['max_abs_delta']['index']}]"
          f" {s['max_abs_delta']['field']} @ {s['max_abs_delta']['page']}）")
    over = {p: max(v.values()) for p, v in rep["pill_outside_nav"].items()}
    if over and max(over.values()) > 0:
        worst = max(over, key=over.get)
        print(f"注：nav_pill 越出 nav 边框最大 {over[worst]}px（{worst.split('-')[0]}）——"
              f"不是不一致，但会让高亮块看起来没对齐")
    print(f"报告：{out}（基准 {rep['baseline']}；本工具**只读**，写入统一走 impose.py）")

    if args.check:
        rng = s["range_px"]
        ok = rng is not None and rng <= args.tol
        print(f"\n判据：{s['range_on']} 极差 {rng}px ≤ {args.tol}px → "
              f"{'PASS' if ok else 'FAIL'}")
        if not ok:
            # 报最该先看的那一页：偏差最大的页，不是"平均偏"
            w = s["max_abs_delta"]
            print(f"  先看 {w['page']}（{w['part']}.{w['field']} 偏 {w['abs_px']}px）；"
                  f"逐页 Δ 见报告的 deltas")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
