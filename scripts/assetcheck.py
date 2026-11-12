#!/usr/bin/env python3
"""assetcheck.py — 素材检查：只判**这块素材自己**，不读参考图。

2026-11-09 改造：原来的第一判据是 cover（"参考图里确定是主体的像素，α 盖住多少"），
整条删除。原因是它量的东西不可测、也不该测：
  · 主体自带光晕时，光晕必然被算进"应覆盖区"、又按规则必须切掉 → 永远过不了 0.90
    （48 页实测 0.818、解析天花板 0.822），只能靠人写 locked_reason 开后门；
  · QA 实测 6 个素材换黑底：cover 最高的 0.995/0.994/0.985 全露方框，最低的 0.818
    反而干净——它系统性奖励坏素材、惩罚好素材，调阈值救不了。

现在只判五件事，全部来自素材自身：
  α 非空     没有任何不透明像素 → FAIL（图是空的或被清零）。
             **与 asset_guard 的分工**：这里只判"α>0.5 共 0 px"（绝对空）；
             "几乎全透明"（α>0.5 占比 <2%，多因裁框切到主体边上）由 asset_guard 判，别在这里重复。
  rectness   实心区（α>0.9）矩形度 = 实心像素 / 实心包围盒面积。
             **除 mask:"rect" 外**，rectness ≥0.95 → FAIL：真抠产出的实心区必然是主体形状，
             不可能是矩形。唯一反例是"主体本身就是矩形"（图表/面板/截图），那时就声明 rect。
  carry      实心区里"素材自己的像素离底色 <carry_dist"的占比。
             只在 `feather`/`rectmask` **且 rectness 没抓到**时判死——避免同一素材两条重复理由；
             真抠类（chroma/flat/pictogram/cutout/circle）上连 WARN 都不出：那类素材 carry 高
             多半是主体自身颜色接近底色（实测浅蓝日历 44%），没有区分力，
             而噪声 WARN 会训练人忽略 WARN，连带让 baked 一起失效。
  baked      α>0.9 且颜色≈底色的像素占比。只 WARN、不判死：白描边、半透明玻璃、
             白底白主体都会让它偏高，没有阈值能同时不误伤。
  expect_cc  声明了就连通域数必须对上，**与 locked 完全解耦**（旧写法只在开了豁免时才查，
             等于"先承认这块有问题，机器才被允许发现它有问题"）。

底色 bg 由 `_asset_bg` 从**素材自身**取，不读参考图：carry 问的是"这块素材携带了多少原底色、
换底色会不会露馅"，参考图在那儿是什么颜色与它无关。
量不出时（bg=None / 无实心区）baked、carry 一律 None，**不写 0.0**——0.0 会被读成"很干净"。


排除"别人的地盘"：文字框，以及与本素材框重合 <80% 的容器。
素材框常蹭到相邻元素，不排除会把邻居算成"该覆盖却没覆盖"。

已知边界：半透明主体（玻璃/气泡）与白底上的白主体，颜色判据无解。
这类素材必须换底色目检（合成到对比色上看一眼），并在交付说明点名。

用法：
  python assetcheck.py --spec page/spec.json --reference page/reference.jpg --out page/assetcheck.json
退出码：0 无 FAIL（可能有 UNPROVEN，不阻塞），1 有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import sys
import specmerge
from consts import is_text_el       # 「哪些是文字块」的唯一判据（不再各写一份 type=="text"）
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent / "delivery"))
import report_state
# 阈值默认值的唯一来源（改这里，旧素材报告自动 stale）
DEFAULTS = dict(report_state.ASSETCHECK_DEFAULTS)


def _asset_bg(rgba: np.ndarray, al: np.ndarray):
    """素材自身「换底色时会露出来的那个色」——**不再从参考图取**。

    2026-11-09 改造：原名 `_border_bg(roi)`，roi 是参考图在裁框内的切片。于是
    「资产闸不看参考图」只是半句话：cover 删了，baked/carry 仍在用「参考图在该位置的
    颜色」。而 carry 的语义是「这块素材携带了多少原底色、换底色会不会露馅」——
    量它必须用**素材自己的像素**：参考图在那儿是什么色，与它换底后露不露馅无关。

    取法（按可靠性）：
      ① α<0.05 的区域：透明区里残留的 RGB 就是原底色（抠图留下的），最准；
      ② 无透明区（如 mask:rect 整块不透明）→ 退回四边像素中位色；
      ③ 都取不到 → None：**量不出**，不当 0 也不当 PASS（0 会被读成「很干净」）。
    """
    solid = al > 0.9
    n_solid = int(solid.sum())
    has_hole = float((al < 0.05).mean()) > 0.01

    # ② 有透明区：实心区靠 α 边界 ≤3px 那一圈（腐蚀 3 次取内芯，外沿 = 实心 − 内芯）
    if n_solid >= 16 and has_hole:
        try:
            from scipy import ndimage as _nd
            core = _nd.binary_erosion(solid, iterations=3)
            ring = solid & ~core
            if int(ring.sum()) >= 100:
                return np.median(rgba[ring][:, :3].astype(np.float32), axis=0)
        except Exception:
            pass

    # ① / ③ 素材最外 2px —— **只取不透明的那部分像素**。
    # 透明像素的 RGB 没有意义（QA 的 uc2b 构型正是这样：外沿环 <100 退到这里，
    # 而最外 2px 全是透明 → RGB=(0,0,0) 被当成底色 → baked 21% 假红）。
    edge = np.concatenate([
        rgba[0:2].reshape(-1, 4), rgba[-2:].reshape(-1, 4),
        rgba[:, 0:2].reshape(-1, 4), rgba[:, -2:].reshape(-1, 4)])
    keep = edge[:, 3] > 12
    if int(keep.sum()) < 16:
        return None          # ④ 量不出：不写 0，不当 PASS
    return np.median(edge[keep][:, :3].astype(np.float32), axis=0)


def _bb(mask: np.ndarray) -> list[int] | None:
    """布尔掩膜的包围盒 [x0,y0,x1,y1]（闭区间）；空掩膜返回 None。"""
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]


# core 是"应覆盖区"的两个自证条件（不新造判据，只用已有的 core_dist 结果）


def _exclude_map(el: dict, spec: dict, w: int, h: int) -> np.ndarray:
    """排除"别人的地盘"：文字框，以及和本素材框重合度 <80% 的容器。

    为什么要排：素材框常蹭到相邻元素（本页载玻片右上角蹭到 TCT 蓝条），
    不排除会把邻居算成"该覆盖却没覆盖"，cover 从 0.998 掉到 0.944——
    是量测污染，不是素材坏。重合度 ≥80% 的容器是宿主（盾牌压在胶囊上），不排。
    """
    x0, y0, x1, y1 = el["bbox"]
    area = max((x1 - x0) * (y1 - y0), 1)
    out = np.zeros((h, w), bool)

    def mark(bx):
        ax0, ay0 = max(0, int(bx[0] - x0)), max(0, int(bx[1] - y0))
        ax1, ay1 = min(w, int(bx[2] - x0)), min(h, int(bx[3] - y0))
        if ax1 > ax0 and ay1 > ay0:
            out[ay0:ay1, ax0:ax1] = True

    for t in spec.get("elements", []):
        # 判据用 is_text_el（缺 type 也算文字）：漏写 type 的文字块也要占"别人地盘"。
        if is_text_el(t):
            mark(t["bbox"])
    for c in spec.get("containers", []):
        cx0, cy0, cx1, cy1 = c["bbox"]
        ow = max(0, min(cx1, x1) - max(cx0, x0))
        oh = max(0, min(cy1, y1) - max(cy0, y0))
        if ow * oh / area < 0.8:
            mark(c["bbox"])
    return out


def _resolve_src(src: str, spec_dir: Path) -> Path | None:
    """按固定基准解析，不看 CWD（在别的目录调就会 MISS，这是老 bug）。"""
    p = Path(src)
    if p.is_absolute():
        return p if p.exists() else None
    for base in (spec_dir, spec_dir.parent):
        cand = base / src
        if cand.exists():
            return cand
    cand = Path("assets") / p.name
    return cand if cand.exists() else None


def check_one(el: dict, spec: dict, cfg: dict, spec_dir: Path,
              locked_reason: str = "") -> dict:
    """只判**素材自己**：不读参考图（2026-11-09，理由见 `_asset_bg`）。

    locked_reason 保留只为兼容调用方；它**不再参与任何判定**——
    "判据挂在登记后面"就是逃生门的成因（先开后门再验）。
    """
    p = _resolve_src(el["src"], spec_dir)
    if p is None:
        return {"id": el["id"], "ok": False, "fails": [f"MISS {el['src']}（相对 {spec_dir}）"]}

    a = np.array(Image.open(p).convert("RGBA"))
    h, w = a.shape[0], a.shape[1]
    al = a[:, :, 3].astype(np.float32) / 255.0
    rgb = a[:, :, :3].astype(np.float32)

    bg = _asset_bg(a, al)
    skip = _exclude_map(el, spec, w, h)
    solid = al > 0.5
    solid_n = int(solid.sum())
    a_bb = _bb(solid)

    if solid_n == 0:
        return {"id": el["id"], "type": el.get("type"), "mask": el.get("mask"),
                "file": p.name, "ok": False, "status": "fail",
                "fails": ["素材没有任何不透明像素（α>0.5 共 0 px）——图是空的或被清零"],
                "warn": [], "baked": None, "rectness": None, "carry": None,
                "asset_bb": a_bb}

    fails, warn = [], []
    # 底色量不出（无透明区且四条边全黑）：颜色类判据一律 None + 出声，不许当 0（0 会被读成"很干净"）
    if bg is None:
        warn.append("底色量不出——baked/carry 本素材不判（不等于通过）")

    clear = np.zeros((h, w), bool)
    if bg is not None:
        clear = (np.linalg.norm(rgb - bg[None, None, :], axis=2) < cfg["clear_dist"]) & ~skip

    s9 = al > 0.9
    solid9_n = int(s9.sum())
    s_bb = _bb(s9)
    sq = ((s_bb[2] - s_bb[0] + 1) * (s_bb[3] - s_bb[1] + 1)) if s_bb else 0
    if solid9_n == 0:
        rectness = carry = None
    else:
        # **几何判据与底色无关，永远算**；只有颜色判据才需要 bg。
        # 把两者合在一个条件里，等于"底色量不出 ⇒ 矩形度免检"——与 cover 时代
        # UNCOVERED 把三条判据一起打哑是同一个形状：一个判据失效不该污染另一个。
        rectness = float(solid9_n) / max(sq, 1)
        if bg is None:
            carry = None
        else:
            d_a = np.linalg.norm(rgb - bg[None, None, :], axis=2)
            carry = float(((d_a < cfg["carry_dist"]) & s9).sum()) / solid9_n

    # expect_cc：声明即查，与 locked 完全解耦（2026-11-09 修）
    if el.get("expect_cc") is not None:
        try:
            from scipy import ndimage as _nd
            _lab, _n = _nd.label(solid)
            _sizes = _nd.sum(solid, _lab, range(1, _n + 1))
            _cc = int((_sizes >= 6).sum())
            if _cc != int(el["expect_cc"]):
                fails.append(f"连通域 {_cc} != 声明 expect_cc {int(el['expect_cc'])}"
                             f"——主体部件数不符（被切断，或混进了碎片）")
        except Exception:
            warn.append("expect_cc 无法核验（scipy 不可用）")

    baked = None
    if bg is not None:
        baked = float((clear & s9).sum()) / max(solid_n, 1)
        if baked > cfg["baked_warn"]:
            warn.append(f"baked {baked:.2%}——染色相近区域被烤成不透明，必须换底色目检")

    # (1) 声明与几何矛盾：声明了羽化/遮罩，实心区却是硬边矩形。
    #     这一条**不与 carry 联立**（2026-11-09 修）：联立会放过"纯色不透明方块"
    #     （rectness=1.0、carry=0），那是"根本没抠图"里最粗暴的一种。
    # (1) 声明与几何矛盾：**除 rect 外**，实心区是矩形就判死。
    #     理由：矩形是几何事实，mask 是声明——声明不能推翻几何。
    #     真抠（chroma/flat/cutout/pictogram/duotone）产出的实心区必然是主体形状，不可能是矩形。
    #     唯一反例是"主体本身就是矩形"（图表/面板/截图），那时正确做法就是声明 mask:"rect"。
    if (rectness is not None and el.get("mask") != "rect"
            and rectness >= float(cfg.get("rectness_fail", 0.95))):
        fails.append(
            f"rectness {rectness:.3f}——实心区是矩形：真抠不可能产出矩形。"
            f"若主体本身就是矩形（图表/面板/截图），请显式声明 mask:'rect'，别改 mask 蒙过去")
    # (2) 携带原底色过多：换底色会露底
    # carry 判死只在"整块留图/柔边裁片"类生效：
    # 真抠类（chroma/flat/pictogram/cutout/circle）里，底色去留由 α 决定，
    # "与底色颜色接近"多半是主体自己的颜色接近底色（实测 a_ic_cal 浅蓝日历 carry=44%）。
    # 判据只在自己的定义域内成立——这是 cover 之后第二个"代理量越界"的教训。
    # (2) 携带原底色过多——只在「柔边/遮罩裁片」且**矩形判据没抓到**时判：
    #     rect 类已由 rectness 判死，再报一次是重复理由；真抠类 carry 高多半是主体自身
    #     颜色接近底色（实测 a_ic_cal 浅蓝日历 44%），在这两类上都没有区分力。
    if (carry is not None and el.get("mask") in ("feather", "rectmask")
            and (rectness is None or rectness < float(cfg.get("rectness_fail", 0.95)))
            and carry > float(cfg.get("carry_fail", 0.12))):
        fails.append(f"carry {carry:.0%}——实心区里这么多像素就是原底色，换底色会露底："
                     f"改 chroma/pictogram/flat 真抠，或写 mask:'rect' 并在 inventory 说明")
    elif (carry is not None and el.get("mask") in ("feather", "rectmask")
            and carry > float(cfg.get("carry_warn", 0.25))):
        warn.append(f"carry {carry:.0%}——实心区里这么多像素贴近原底色，签字前先换底色目检")

    solid9_frac = solid9_n / max(solid_n, 1)
    if el.get("mask") in ("feather", "rectmask") and solid9_frac < 0.5:
        warn.append(f"实心区(α>0.9)只占不透明区 {solid9_frac:.0%}——素材大面积半透明，"
                    f"过覆盖判据在这类素材上失效，需人目检")

    return {"id": el["id"], "type": el.get("type"), "mask": el.get("mask"),
            "file": p.name, "ok": not fails,
            # 三态：pass / fail / unproven。底色量不出时**不能报 pass**——
            # 那句 warn"不等于通过"只在人读报告时成立，按 status 统计的地方仍会算通过，
            # 等于把"未证明当通过"藏进 status 字段。unproven 不阻塞交付，但可被数出来。
            "status": "fail" if fails else ("unproven" if bg is None else "pass"),
            "fails": fails, "warn": warn,
            "baked": None if baked is None else round(baked, 4),
            "rectness": None if rectness is None else round(rectness, 3),
            "carry": None if carry is None else round(carry, 4),
            "asset_bb": a_bb}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--reference", default=None, help="不再使用（保留兼容）：资产闸只读素材自身")
    ap.add_argument("--out", default=None)
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k}", type=float, default=v)
    args = ap.parse_args()
    cfg = {k: getattr(args, k) for k in DEFAULTS}

    spec = specmerge.load_merged(args.spec)   # 必须和 build 看同一份
    ref = None  # 2026-11-09：资产闸不再读参考图
    spec_dir = Path(args.spec).resolve().parent
    # 显式豁免：inventory 里 locked + locked_reason 的素材，cover 失败降为警告
    locked = {}
    inv_p = spec_dir / "inventory.json"
    if inv_p.exists():
        try:
            for it in (json.loads(inv_p.read_text(encoding="utf-8")).get("items") or []):
                if it.get("locked") and str(it.get("locked_reason") or "").strip():
                    locked[it["id"]] = it["locked_reason"]
        except Exception:
            locked = {}
    items = [check_one(el, spec, cfg, spec_dir, "")
             for el in spec.get("assets", []) if el.get("src")]
    nfail = sum(1 for r in items if not r["ok"])
    nwarn = sum(1 for r in items if r.get("warn"))

    print(f"{'id':9s} {'mask':7s} {'baked':>7s}  结果")
    n_unprov = 0
    for r in items:
        bak = "      —" if r.get("baked") is None else f"{r['baked']:7.2%}"
        # 渲染按 **status**，不按 ok。ok 是"不阻塞"的意思，不是"通过"的意思——
        # 之前 `tail = "PASS" if r["ok"]` 会让 unproven 那行印 PASS，同一行又写
        # "不等于通过"，汇总还说"全过"。终端是唯一交付现场，人扫一眼就走了，
        # 所以这里是"未证明当通过"的第三个藏身处（前两处：verdict 假布尔、status 字段）。
        st = str(r.get("status") or ("fail" if not r["ok"] else "pass"))
        if st == "fail":
            tail = "FAIL " + "; ".join(r["fails"])
        elif st == "unproven":
            n_unprov += 1
            tail = "UNPROVEN（颜色类判据未评，不阻塞）"
        else:
            tail = "PASS"
        if r.get("warn"):
            tail += " | WARN " + "; ".join(r["warn"])
        print(f"{r['id']:9s} {str(r.get('mask')):7s} {bak}  {tail}")
    print()
    if nfail:
        head = f"FAIL {nfail} 个"
    elif n_unprov:
        head = f"{len(items) - n_unprov} 个通过、{n_unprov} 个未证明（不阻塞）"
    else:
        head = "全过"
    print(f"{len(items)} 个素材，{head}"
          + (f"（{nwarn} 个需换底色目检）" if nwarn else ""))

    if args.out:
        out = Path(args.out)
        key = report_state.key_assets(Path(args.spec), cfg)
        report_state.write_report(out, {
            "stage": "assetcheck", "kind": "assetcheck",
            "outcome": "pass" if nfail == 0 else "fail",
            "config": cfg, "items": items,
        }, key)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
