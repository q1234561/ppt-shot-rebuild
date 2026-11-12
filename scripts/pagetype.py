"""页型聚类：**先按角色排除顶部件，再问"这几页是不是同一个版式"**。

为什么要排除顶部件：导航条 + 页脚 9 页都在同一位置，它们会让任意两页的相似度凭空变高——
以前那次聚类（IoU 最高 0.80）就是被这个抬上去的，还叠了对象命名污染（`nav` / `c_nav` / `c_bar`）。
现在顶部件由 `deck.json` 的角色声明精确排除，剩下的**只有内容区**，问的才是"版式像不像"。

方法：把每页的内容件 bbox 画进一张粗网格（默认 40px/格），得到占用位图；
两页相似度 = 占用格子的 Jaccard；用**完全连接**聚类（簇内任意两页都 ≥ 阈值），
不用单连接——单连接会串成一条链（A≈B、B≈C 就把 A 和 C 并到一起，而 A、C 明明不像）。

输出 `page_types.json`：相似度矩阵 + 簇 + 最高 IoU。这是 P3 的开工门：
页型数决定 token 阶梯要细到什么程度。

用法：
    python scripts/pagetype.py --deck <整套>/deck.json --root <整套> --out <整套>/page_types.json
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import roles

CELL = 40          # 网格边长（px）。40 在 1672×941 上给出 42×24 的位图：够粗，不受 1~2px 噪声影响
THR = 0.62         # 完全连接阈值。0.62 ≈ "大结构一致、细节可以不同"；调它等于调页型数，必须写进报告


def content_cells(spec: dict, role_ids: set[str], W: int, H: int, cell: int) -> set[tuple[int, int]]:
    """内容件的占用格集合。角色件（nav/pill/div/deco/footer）一律排除。"""
    cells: set[tuple[int, int]] = set()
    for seg in ("containers", "elements"):
        for it in spec.get(seg) or []:
            if not isinstance(it, dict) or not it.get("bbox"):
                continue
            if str(it.get("id")) in role_ids:
                continue
            bb = [float(v) for v in it["bbox"]]
            if len(bb) != 4 or (bb[2] - bb[0]) <= 0 or (bb[3] - bb[1]) <= 0:
                continue           # [0,0,0,0] 的占位 freeform 不参与
            x0, y0 = max(0, int(bb[0] // cell)), max(0, int(bb[1] // cell))
            x1, y1 = min(int(bb[2] // cell), int(W // cell) - 1), min(int(bb[3] // cell), int(H // cell) - 1)
            for gx in range(x0, x1 + 1):
                for gy in range(y0, y1 + 1):
                    cells.add((gx, gy))
    return cells


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(len(a | b), 1)


def cluster(pages: list[str], sim: dict[tuple[str, str], float], thr: float) -> list[list[str]]:
    """**确定性**的完全连接层次聚类。

    第一版写的是"逐页塞进第一个装得下的簇"（贪心）。那是错的：同一个矩阵，
    按不同页序跑会得到不同的簇——实测把 9 页打乱一次就变成 [2,2,5]（原序 [1,1,7]），
    而 40↔45 相似度 0.794 ≥ 0.75 却被分进不同簇，说明它连"完全连接"这个自称都没做到。
    页序是实现细节，不该改变结论。

    现在：每轮找**最小两两相似度最高**的那对簇合并，直到最好的那对也 < thr。
    完全连接的定义保证"簇内任意两页 ≥ thr"，而合并停止条件保证"任意两簇之间 < thr"，
    两个方向都成立，且结果与输入顺序无关（并列时按页名字典序定序）。
    """
    clusters: list[list[str]] = [[p] for p in sorted(pages)]
    if len(clusters) < 2:
        return clusters

    while len(clusters) > 1:
        best_s, best_i, best_j = None, None, None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                # 完全连接：两簇之间的相似度 = 跨簇所有页对里**最小**的那个
                s = min(sim[(a, b)] for a in clusters[i] for b in clusters[j])
                # 并列时按 (名字序) 定序，避免依赖循环顺序
                key = (s, clusters[i][0], clusters[j][0])
                if best_s is None or key > (best_s, clusters[best_i][0], clusters[best_j][0]):
                    best_s, best_i, best_j = s, i, j
        if best_s < thr:
            break
        clusters[best_i] = sorted(clusters[best_i] + clusters[best_j])
        del clusters[best_j]

    # 输出稳定：大的在前，同大小按首页名
    return sorted(clusters, key=lambda c: (-len(c), c[0]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--cell", type=int, default=CELL)
    ap.add_argument("--thr", type=float, default=THR)
    args = ap.parse_args()

    deck = roles.load_deck(Path(args.deck))
    root = Path(args.root)
    grids: dict[str, set] = {}
    sizes: dict[str, tuple[int, int]] = {}

    for page in deck["pages"]:
        sp = root / page / "spec.json"
        if not sp.exists():
            continue
        spec = json.loads(sp.read_text(encoding="utf-8"))
        cv = spec.get("canvas") or {}
        W, H = int(cv.get("w_px") or 1672), int(cv.get("h_px") or 941)
        role_ids = {i for lst in (deck["roles"].get(page) or {}).values() for i in lst}
        grids[page] = content_cells(spec, set(role_ids), W, H, args.cell)
        sizes[page] = (W, H)

    pages = [p for p in deck["pages"] if p in grids]
    sim = {}
    for a, b in itertools.combinations(pages, 2):
        s = jaccard(grids[a], grids[b])
        sim[(a, b)] = sim[(b, a)] = s
    for p in pages:
        sim[(p, p)] = 1.0

    # 阈值敏感度：页型数对阈值高度敏感，只报一个数等于藏了另四个。
    # 这一列直接决定 P3 的 token 阶梯要分几档，所以必须摆出来让人挑。
    sweep = {}
    for t in (0.60, 0.65, 0.70, 0.75, 0.80, 0.85):
        sweep[f"{t:.2f}"] = [sorted(c) for c in cluster(pages, sim, t)]

    cl = cluster(pages, sim, args.thr)
    cl.sort(key=len, reverse=True)
    best = max(((sim[(a, b)], a, b) for a, b in itertools.combinations(pages, 2)), default=(0.0, None, None))

    rep = {
        "stage": "pagetype", "kind": "page_types",
        "method": "content-only occupancy grid + Jaccard + complete-linkage",
        "cell_px": args.cell, "threshold": args.thr,
        "excluded": "roles 里的 6 类件（nav / nav_pill / div / deco_l / deco_r / footer）",
        "pages": pages,
        "similarity": {f"{a}|{b}": round(v, 3) for (a, b), v in sim.items() if a < b},
        "clusters": cl,
        "n_types": len(cl),
        "threshold_sweep": {k: {"n_types": len(v), "clusters": v} for k, v in sweep.items()},
        "max_pair": {"iou": round(best[0], 3), "a": best[1], "b": best[2]},
        "note": "阈值与格宽都会改变 n_types；换参数必须改这一行并重跑，不许只报小的那个数",
    }
    out = Path(args.out) if args.out else root / "page_types.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"内容区相似度（{args.cell}px/格，完全连接阈值 {args.thr}）")
    head = "".join(f"{p.split('-')[0][:5]:>6s}" for p in pages)
    print(f"{'':>34s}{head}")
    for a in pages:
        row = "".join(f"{sim[(a, b)]:6.2f}" for b in pages)
        print(f"{a:34s}{row}")
    print(f"\n页型数 {len(cl)}：")
    for i, c in enumerate(cl, 1):
        print(f"  T{i}（{len(c)} 页）：{', '.join(x.split('-')[0] for x in c)}")
    print("\n阈值敏感度（同一个矩阵，只改判定线）：")
    for t, v in sweep.items():
        print(f"  {t} → {len(v)} 型  " + " | ".join(",".join(x.split('-')[0] for x in c) for c in v))
    print(f"\n最高相似 {best[0]:.3f}（{best[1]} ↔ {best[2]}）· 报告 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
