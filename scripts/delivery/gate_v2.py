#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_v2.py — 判据 v2 的实跑版（只读页面目录，只写 <page>/_check/gate_v2.json）

目的：证明"检查换掉之后，P03 这一页会变红，而且红在正确的地方"。
它不实现修复，只回答"这页现在该不该交付"。

四个检查（对应复盘文档 01 的 M5/M9/M6/M4/M6e 里去掉 M5 的那四项）：
  C2 形态保真   —— 饱和色容器（白卡测不出，记 n/a）：在声明 bbox 外扩 ±14px 的窗口里，按
                   「主色通道领先其余通道 >55 且该通道亮度 >140」取实测掩膜，量出色域 bbox，
                   与声明 bbox 逐边比差；再按 spec 声明的 `edge_type`（hard / soft / slanted）
                   决定这条边的差算不算错——**只有 hard 边 |差| > EDGE_TOL(4px) 才判红**，
                   柔边 / 斜边 / 外沿顶到搜索窗一律记「不可判」。跃变带与跨行跨度只作为
                   打印给人反驳的实测量，机器不自动猜边型。
  C3 分类封闭   —— 禁止 approx-shape；同一逻辑件拆成的形状数 ≤3
  C4 完整性     —— 参考图内容连通域被清单 bbox 覆盖的比例（第三方分母）
  C5 素材质量   —— 没人签字的 WARN（**只提示，不计红**：签字挡不住任何人，它证明不了
                   "真看过图"，所以不进退出码；主链里只作为交付记录出现）

接线状态（2026-11-09 交叉审计）：**C2 / C3 / C4 均未接入主链**——`gates.py` 只
`from gate_v2 import c5_assets`，交付时只取「素材 WARN 点名」那一段。本文件的 `main()` 是
独立实跑入口（证明判据能红在该红的地方），它写的 `_check/gate_v2.json` 不参与任何裁决；
`SHAPE_PER_UNIT` / `RATIO_MIN` / `MAX_LEVELS` 属未接线常量（C3 未接主链），别当成交付时在数形状。

（原 C1"字号阶梯"已整体删除：每页风格不同 → 档表不可复用，那套判据真阳性为 0。
  字号改用**精确比对**，见 `delivery/size_probe.py`：读产物 `a:rPr/@sz` 比合并视图，无容差。）

用法：
  $PY $D/gate_v2.py --page <页目录>
退出码：0 全绿 / 1 有红
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

import numpy as np
from PIL import Image

# 与 build / verify / cycle 看**同一份**合并视图（spec + override，都在 specmerge 里）。
SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
import specmerge

DPI = 125.4
SHAPE_PER_UNIT = 3    # 同一逻辑件允许的形状数


def jload(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def c3_classify(inv, spec):
    fails = []
    items = inv.get("items", [])
    approx = [i["id"] for i in items if i.get("reason") == "approx-shape"]
    if approx:
        fails.append(f"approx-shape 这个类别已废，但仍有 {len(approx)} 条：{', '.join(approx)}")

    # 同一逻辑件被拆成几个形状：按 id 前缀（首两段）分组
    groups = {}
    # 2026-11-08：原来只遍历 containers，于是"删容器 → 加素材"这个动作两个方向都看不见，
    # C3 的绿只能证明"容器被删了"，不能证明"图标现在对了"。assets 必须一起纳入分组计数。
    for c in list(spec.get("containers", [])) + list(spec.get("assets", [])):
        cid = str(c["id"])
        key = "_".join(cid.split("_")[:2]) if "_" in cid else cid
        groups.setdefault(key, []).append(cid)
    fat = {k: v for k, v in groups.items() if len(v) > SHAPE_PER_UNIT}
    for k, v in sorted(fat.items(), key=lambda kv: -len(kv[1])):
        fails.append(f"`{k}*` 用 {len(v)} 个形状拼一个件（上限 {SHAPE_PER_UNIT}）：{', '.join(v[:6])}…")
    return {"fails": fails, "groups": {k: len(v) for k, v in groups.items()}}


def c5_assets(page: Path):
    """C5 素材质量：WARN 不是错，**没人签的 WARN 才是错**。

    为什么这样定：`assetcheck` 的 WARN 有两类——
      · baked X%——染色相近区域被烤成不透明，必须换底色目检
      · cover 低但已豁免（locked + 量化理由）
    两类都不是"错了"，都是"需要人看一眼"。所以判据不是"不许有 WARN"，
    而是"每条 WARN 必须有一条人的记录"：换底色目检记录 `reviewed_on_bg`。
    2026-11-09 修正：旧实现把 `locked_reason`（cover 差属口径问题）也算作签字，
    等于给 cover 豁免白送一张目检签字；现在只认 `reviewed_on_bg`。

    输出口径：返回的 `fails` 只是**提示清单**——列出"没人签字的 WARN"，供交付记录点名；
    `main()` 不把它计入 `red`、也不影响退出码（终端标签是「提示」而不是「FAIL」）。
    签与不签都不改变 pass/fail：签字证明不了"真看过图"，所以它不配当闸（WARN 原样留在 notes 里，不隐藏）。
    """
    fails, notes = [], []
    a = jload(page / "_check" / "assets.json")
    if not a:
        return {"fails": ["没有素材检查报告：按不通过处理"], "notes": notes}
    inv = jload(page / "inventory.json", {"items": []}) or {"items": []}
    items = {i.get("id"): i for i in inv.get("items", [])}
    warns, unsigned = [], []
    for n in (a.get("notes") or []):
        s = str(n)
        # 认三种"要人看一眼"的写法：assetcheck 的 "有 WARN"、带 WARN 的行、
        # 以及 UNCOVERED 的 "需人目检"（2026-11-09 发现：45 页 a_bulb 那条写的是
        # "…需人目检——未证明（不阻塞）"，不含 WARN 字样，旧过滤把它整个漏掉了）。
        if not ("WARN" in s or "需人目检" in s):
            continue
        eid = s.split(":")[0].strip()
        if not eid or eid.startswith("assetcheck"):
            continue
        warns.append(eid)
        it = items.get(eid) or {}
        # 只认 reviewed_on_bg：locked_reason 是"cover 差属口径问题"的豁免，
        # 与"换底色目检过"是两件事。旧实现用 `or` 把两者混用，等于给 cover 豁免
        # 白送一张目检签字（40 页的 ic_bulb / ic_clip 就是这么过的）。
        if not it.get("reviewed_on_bg"):
            unsigned.append(eid)
    notes.append(f"WARN {len(warns)} 条 / 已签 {len(warns)-len(unsigned)} 条 / 未签 {len(unsigned)} 条"
                 f"（outcome={a.get('outcome')}）")
    if unsigned:
        fails.append(f"{len(unsigned)} 条 WARN 没人签：{', '.join(sorted(set(unsigned)))}"
                     f"——仅记录，不拦交付；要留痕就在该 id 的 inventory 行写 reviewed_on_bg{{at,by,verdict}}")
    if not (a.get("passed") if a.get("passed") is not None
            else str(a.get("outcome") or "").lower() == "pass"):
        fails.append("assetcheck 有 FAIL 项")
    return {"fails": fails, "notes": notes, "warns": warns, "unsigned": unsigned}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--spec", default=None, help="覆盖 spec 路径（用于验证解析后副本）")
    a = ap.parse_args()
    page = Path(a.page)
    spec_file = Path(a.spec) if a.spec else page / "spec.json"
    try:
        spec = specmerge.load_merged(spec_file, quiet=True)
    except (OSError, ValueError):
        spec = None      # 文件缺失 / JSON 坏 → None，由各检查各自报
    if not isinstance(spec, dict):
        # 输入错要给 rc=2，不许把 None 传进各检查后 Traceback——
        # 原来这里靠"各检查各自报"，但 c3_classify 直接 spec.get() 就崩了。
        print(f"REFUSED: {spec_file} 读不出来（缺失或 JSON 坏）", file=sys.stderr)
        return 2
    inv = jload(page / "inventory.json", {"items": []})
    ref = page / "reference.png"

    r3 = c3_classify(inv, spec)
    r5 = c5_assets(page)

    # C2（形态保真）与 C4（连通域完整性）已退役：它们是"复刻语境"的判据
    # （比对参考图的边缘位置/覆盖面积），而目标是"客户能改的 PPT"。
    # 保留 C3：同一逻辑件用几个形状拼，直接影响客户编辑成本。
    checks = [("C3 分类封闭", r3), ("C5 素材质量", r5)]
    print(f"\n=== 判据 v2 实跑：{page.name} ===")
    print(f"{'检查':<14}{'结论':<6}要点")
    print("-" * 96)
    n_red = 0
    for name, r in checks:
        fails = r.get("fails") or []
        voids = r.get("void") or []
        # C5 只提示、不计红（2.43.0）：签字证明不了"真看过图"——乱写三个字母也能过，
        # 所以它不配当闸；主链里它只作为「素材：」note 出现在交付记录里。
        if fails and not name.startswith("C5"):
            n_red += 1
        if name.startswith("C2"):
            head = f"{len(r['rows'])} 个彩色容器"
        elif name.startswith("C3"):
            head = f"{len(r3['groups'])} 组容器"
        elif name.startswith("C4"):
            head = f"连通域 {r['total']}，未覆盖 {len(r['uncovered'])}（{r['ratio']:.0%}）"
        else:
            head = (r.get("notes") or [""])[0]
        tag = ("提示" if name.startswith("C5") else "FAIL") if fails \
            else ("VOID" if voids else "PASS")
        print(f"{name:<14}{tag:<6}{head}")
        for f in fails:
            print(f"{'':<20}- {f}")
        for v in voids:
            print(f"{'':<20}! {v}")
    print("-" * 96)
    n_void = sum(1 for _, r in checks if (r.get("void") or []) and not (r.get("fails") or []))
    print(f"结论：{len(checks) - n_red - n_void}/{len(checks)} 真通过 · {n_red} 红 · {n_void} 无效"
          + ("" if n_red == 0 else f" → 这页不能交付"))

    def clean(o):
        import numpy as _np
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (_np.integer,)):
            return int(o)
        if isinstance(o, (_np.floating,)):
            return float(o)
        return o

    out = clean({"page": str(page), "C3": r3, "C5": r5, "red": n_red})
    outpath = page / "_check" / "gate_v2.json"
    outpath.parent.mkdir(parents=True, exist_ok=True)
    outpath.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if n_red == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
