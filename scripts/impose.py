# -*- coding: utf-8 -*-
"""impose.py — 把"一套的规范"施加到每一页上（**唯一的写入口**）。

为什么要有这个文件：字号和全局件位置以前是"每页照着参考图测出来的"，于是 9 页 57 种字号、
导航条左边缘差 51px。事后修过两次（`typescale.py` / `deckalign --apply`），但那是**修复器**——
新做一页还是会长回原样，而且两套规则并存（一套写死在脚本里，一套在数据里）就是新的坑。

现在规则只有一份：`visual.json` 的 `type.scale`（字号档位）与 `global.nav/footer`（位置）。
本文件负责把它落到每页 spec 上，**幂等**：已经是规范的样子就什么都不改，报告 0 处改动。

三条自我约束（都来自踩过的坑）：
  1. **只改必要的**：字号只在 `|Δ| ≤ max_snap_pt` 时吸附，超了报"离群"让人看，不硬拽。
  2. **只动位置，不动尺寸**：导航条的宽高随标签文字与高亮胶囊变，拉平它只能改字号字距凑字宽。
  3. **改完写回 spec**：不写回的话，闸门看的是旧值、出稿用的是新值——同一条坑这个仓库踩过两次。

用法：
    python scripts/impose.py --deck <整套>/deck.json --visual <整套>/visual.json --root <整套>          # 演练
    python scripts/impose.py ... --write                                                              # 落盘
    python scripts/impose.py ... --check                                                              # 规范符合性（0 改动=PASS）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import roles

DEFAULT_CAP_PT = 33.0
DEFAULT_MAX_SNAP = 2.0
DEFAULT_FOOTER_MOVE = 20.0


def load_visual(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def consts(visual: dict) -> dict:
    """从 visual.json 取规则，缺了就报错——默认值是"第二套规则"，宁可缺了停。"""
    t = visual.get("type") or {}
    g = visual.get("global") or {}
    if not t.get("scale"):
        raise ValueError("visual.json 缺 type.scale（字号档位表）：规范必须显式声明，不猜")
    if not (g.get("nav") or {}).get("x0"):
        raise ValueError("visual.json 缺 global.nav.x0：导航条位置必须显式声明，不猜")
    return {
        "scale": [float(s) for s in t["scale"]],
        "cap_pt": float(t.get("cap_pt") or DEFAULT_CAP_PT),
        "max_snap": float(t.get("max_snap_pt") or DEFAULT_MAX_SNAP),
        "nav": g["nav"],
        "footer": g.get("footer") or {},
    }


def _bbox_map(spec: dict) -> dict:
    out = {}
    for seg in ("containers", "elements"):
        for it in spec.get(seg) or []:
            if it.get("id") and it.get("bbox"):
                out[str(it["id"])] = it
    return out


def _inside(box, outer, pad: float = 2.0) -> bool:
    return (box[0] >= outer[0] - pad and box[1] >= outer[1] - pad
            and box[2] <= outer[2] + pad and box[3] <= outer[3] + pad)


def normalize(spec: dict, decl: dict, c: dict) -> tuple[dict, list[dict], list[str]]:
    """把一页吸附到规范。返回 (新的 spec, 改动清单, 例外清单)。不改入参。

    改动清单里每条都能反推：`{kind, id, old, new}`。
    """
    out = json.loads(json.dumps(spec, ensure_ascii=False))
    changes, notes = [], []
    els = out.get("elements") or []

    # ① 字号吸附
    for el in els:
        v = el.get("size_pt")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue                     # token（字符串）由 build 展开，不在这里动
        if float(v) > c["cap_pt"]:
            notes.append(f"{el.get('id')}: {v}pt > {c['cap_pt']:g}pt，标题级不吸")
            continue
        target = min(c["scale"], key=lambda s: (abs(s - float(v)), s))
        if target == float(v):
            continue
        if abs(target - float(v)) > c["max_snap"]:
            notes.append(f"{el.get('id')}: {v}pt 最近档 {target:g} 差 {abs(target-float(v)):.1f}pt > "
                         f"{c['max_snap']:g}，不吸")
            continue
        el["size_pt"] = float(target)
        changes.append({"kind": "size", "id": el.get("id"), "old": float(v), "new": float(target)})

    bb = _bbox_map(out)

    # ② 导航条整组平移（位置只认 x0/y0；宽高由内容决定，见文件头）
    nav_ids = [i for i in (decl.get("nav") or []) if i in bb]
    if nav_ids:
        nav = [float(x) for x in bb[nav_ids[0]]["bbox"]]
        dx = round(float(c["nav"]["x0"]) - nav[0], 1)
        dy = round(float(c["nav"]["y0"]) - nav[1], 1)
        if dx or dy:
            group = list(nav_ids)
            for part in ("nav_pill", "div"):
                group += [i for i in (decl.get(part) or []) if i in bb and i not in group]
            for i, it in bb.items():
                if i in group:
                    continue
                if _inside([float(x) for x in it["bbox"]], nav):
                    group.append(i)          # 条里的标签：不跟着走就会跑出去
            for i in group:
                it = bb[i]
                old = [float(x) for x in it["bbox"]]
                it["bbox"] = [round(old[0] + dx, 1), round(old[1] + dy, 1),
                              round(old[2] + dx, 1), round(old[3] + dy, 1)]
                changes.append({"kind": "nav", "id": i, "old": old, "new": it["bbox"]})

    # ③ 页脚：只动 y，且超过护栏就报例外（有的页页脚本来就靠上）
    footer = [i for i in (decl.get("footer") or []) if i in bb]
    fy = (c["footer"] or {}).get("y0")
    if footer and fy is not None:
        cur = float(bb[footer[0]]["bbox"][1])
        dy = round(float(fy) - cur, 1)
        lim = float((c["footer"] or {}).get("max_move_px") or DEFAULT_FOOTER_MOVE)
        if dy and abs(dy) <= lim:
            for i in footer:
                it = bb[i]
                old = [float(x) for x in it["bbox"]]
                it["bbox"] = [old[0], round(old[1] + dy, 1), old[2], round(old[3] + dy, 1)]
                changes.append({"kind": "footer", "id": i, "old": old, "new": it["bbox"]})
        elif dy:
            notes.append(f"页脚 y={round(cur,1)} → 规范 {fy} 需移动 {dy}pt > {lim:g}，"
                         f"不硬动（这一页页脚本来就靠上，需人判断）")

    return out, changes, notes


def apply_page(page_dir: Path, decl: dict, c: dict) -> tuple[list, list]:
    """读 spec → 归一到规范 → 有改动就写回。**这是全库唯一写 spec 的地方**。

    放在这里而不是 cycle 里：写入口只有一个，才谈得上"一套规则"。
    """
    sp = Path(page_dir) / "spec.json"
    spec = json.loads(sp.read_text(encoding="utf-8"))
    new, changes, notes = normalize(spec, decl, c)
    if changes:
        sp.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    return changes, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", required=True)
    ap.add_argument("--visual", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--page", default=None, help="只处理一页（给 cycle 用）")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true", help="有改动即 rc=1（规范符合性）")
    args = ap.parse_args()

    deck = roles.load_deck(Path(args.deck))
    c = consts(load_visual(Path(args.visual)))
    root = Path(args.root)
    pages = [args.page] if args.page else deck["pages"]

    total = 0
    for page in pages:
        sp = root / page / "spec.json"
        if not sp.exists():
            print(f"  ! {page}: 缺 spec.json")
            continue
        spec = json.loads(sp.read_text(encoding="utf-8"))
        new, changes, notes = normalize(spec, deck["roles"].get(page) or {}, c)
        total += len(changes)
        head = f"{page[:26]:28s} 改动 {len(changes):3d}"
        if notes:
            head += f"，例外 {len(notes)}"
        print(head)
        for ch in changes[:6]:
            print(f"    {ch['kind']:6s} {ch['id']:10s} {ch['old']} → {ch['new']}")
        if len(changes) > 6:
            print(f"    … 另 {len(changes)-6} 处")
        for nt in notes[:3]:
            print(f"    例外 {nt}")
        if args.write and changes:
            sp.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")

    verb = "已写入" if args.write else "演练（未写）"
    print(f"\n{verb}：共 {total} 处" + ("；全部符合规范" if total == 0 else ""))
    if args.check:
        return 0 if total == 0 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
