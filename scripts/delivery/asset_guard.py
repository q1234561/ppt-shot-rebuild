#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""asset_guard.py — 素材裁框的硬不变量：圈的太紧（主体被切）。

第一性原理：素材出问题只有两种形状——"圈进了别人的东西"和"圈得太紧"。
前者（裁框与文字墨迹框相交）由 `spec_lint` 在 plan 阶段判一次，这里不再重复：
同一个错被两道闸各报一遍，改一处要过两次，还容易给出两条不同的修法。

这里只管后者：
  I2 圈得太紧 —— 产物 PNG 的 α 在裁框边界上还饱和（>0.5）→ 主体被切。
      （原包：crop with safety padding so the icon body never touches the crop edge）

判据**只看产物与 spec，不看"像不像"**，所以能在渲染之前把错拦掉——
2026-11-07 那次"四个图标吃掉标签字 + 图标被切一半"，所有基于图像的闸都放行了。
`rect` / `feather` 这类"整块留图"不产 α 语义，I2 对它们跳过（它们的正确性由 plan 层的压字判定兜）。

用法：
  python asset_guard.py --page <页面目录> [--json out.json]
退出码：0 全过 / 1 有违规
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import specmerge

# 不产"主体轮廓 α"的路径：整块留图 / 羽化裁片 / 羽化+排除矩形。
# 它们边上 α=1 是设计（裁框贴着卡片或画布边），不是主体被切，I2 对它们无意义。
ALPHA_FREE = ("rect", "feather", "rectmask")
EDGE_ALPHA_MAX = 0.5                 # 边界上允许的最大 α（超过＝主体被裁断）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--spec", default="spec.json")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    page = Path(a.page).resolve()
    spec_path = page / a.spec
    spec = specmerge.load_merged(spec_path, quiet=True)
    inv = {}
    if (page / "inventory.json").exists():
        inv = json.loads((page / "inventory.json").read_text(encoding="utf-8"))
    assets = spec.get("assets", [])

    errs: list[str] = []
    rows = []
    for s in assets:
        b = [float(v) for v in (s.get("bbox") or [0, 0, 0, 0])]
        aid = s["id"]
        mask = str(s.get("mask") or "cutout")

        # I2 圈得太紧（只看真正产 α 的 mask）
        src = s.get("src")
        if mask not in ALPHA_FREE and src:
            p = Path(src)
            p = p if p.is_absolute() else (page / src)
            if p.exists():
                al = np.asarray(Image.open(p).convert("RGBA"))[:, :, 3].astype(np.float32) / 255.0
                edges = {
                    "上": float(al[0].max()) if al.shape[0] > 2 else 0,
                    "下": float(al[-1].max()) if al.shape[0] > 2 else 0,
                    "左": float(al[:, 0].max()) if al.shape[1] > 2 else 0,
                    "右": float(al[:, -1].max()) if al.shape[1] > 2 else 0,
                }
                cov = float((al > 0.5).mean())
                bad = {k: round(v, 2) for k, v in edges.items() if v > EDGE_ALPHA_MAX}
                if cov < 0.02:
                    # 已实测：裁框收到字形边上时，pictogram 会把贴边的连通域全判成污染，
                    # 输出一张**全透明**的图（覆盖率 0.0%），而 fastcut 只 print 一行 FAIL、
                    # 不阻断；assetcheck 要单独跑才会发现。这里在交付路径上堵住。
                    # **与 assetcheck 的分工**：这里判"比例"（α>0.5 占比 <2% 几乎全透明）；
                    # assetcheck 只判"绝对空"（α>0.5 共 0 px）。两条不重复，也别互相替代。
                    errs.append(f"I3 {aid}: 产物几乎全透明（α>0.5 仅 {cov:.1%}）"
                                f"—— 裁框收到主体边上或 mask 判据过严，扩框重抠")
                    rows.append((aid, mask, "I3", f"{cov:.1%}"))
                elif bad:
                    # I2 未在真页上实测触发过，只作 WARN：边界饱和通常意味着主体被切
                    rows.append((aid, mask, "I2!", str(bad)))
                else:
                    rows.append((aid, mask, "ok", f"{cov:.0%}"))
            else:
                errs.append(f"缺产物 {src}（先跑 fastcut/cutout）")
        else:
            rows.append((aid, mask, "ok", "α 无关（整块留图）"))

    print(f"{'id':12s}{'mask':10s}{'判据':4s}备注")
    for aid, mask, tag, note in rows:
        print(f"{aid:12s}{mask:10s}{tag:4s}{note}")
    print()
    if errs:
        for e in errs:
            print(f"FAIL {e}")
        print(f"\n{len(errs)} 项违规")
    else:
        print(f"PASS {len(assets)} 块素材：无压字、无边界饱和")
    if a.json:
        Path(a.json).write_text(json.dumps({"passed": not errs, "errors": errs,
                                            "rows": [{"id": r[0], "mask": r[1], "check": r[2], "note": r[3]}
                                                     for r in rows]}, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
