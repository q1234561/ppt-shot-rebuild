#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""inventory.py — 每个元素一条决策记录（派生 + 校验）。

为什么要有它：
  以前的"决策"散在 SKILL 的散文、CHANGELOG、baseline_status.json 的 exceptions 里，
  三处维护必然漂移（实测：28 页 quality 不过 11 项，例外只登记了 8 条，3 条无记录）。
  这个工具把"这页每个元素是什么、为什么这样做、差在哪"变成一份可机器校验的数据。

两种模式：
  --write   从 spec+override 的合并视图**派生** inventory.json（人补的部分保留）
  --check   校验：覆盖完整 / 六类字段约束 / 参考图尺寸 / 裁片不压字 / id 唯一 / src 存在

六类 reason（前两类是原生，后四类用过栅格）：
  native-simple     原生简单对象（文字框 / 矩形 / 圆角矩形 / 椭圆 / 预设形状）
  native-freeform   任意多边形（缎带、斜切尾这类 7 种预设表达不了的）
  approx-shape      用近似形状代替原图（渐变代径向高光等），必须写 approx_of + gaps
  raster-circle     圆形素材（alpha 由几何圆给出）
  raster-matte      抠图素材（matting）
  raster-crop       整块原图裁片（带羽化），必须写 crop_note

`gaps` 不属于某一类：任何元素只要与参考图有**已知且接受**的差距，就登记一条。
它由真实决策触发，不是"质量报告不过"的免死金牌：plan 闸只校验结构与覆盖，
不读取 quality/verify 的结论（那两份是诊断，且可能来自上一版输入）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # scripts/
import specmerge

NATIVE, RASTER = "native", "raster"
REASON_KIND = {
    "native-simple": NATIVE,
    "native-freeform": NATIVE,
    "approx-shape": NATIVE,
    "raster-circle": RASTER,
    "raster-matte": RASTER,
    "raster-crop": RASTER,
}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


# ---------------------------------------------------------------- 派生
def _hex(el) -> str:
    f = el.get("fill") or (el.get("gradient") or [])
    return f if isinstance(f, str) else "/".join(f)


def derive(el: dict, sec: str) -> dict:
    """从字段推出 (reason, evidence)。推不出来的返回 reason=None，由人补。"""
    if sec == "elements":
        return {"reason": "native-simple",
                "evidence": f"text(len={len(str(el.get('text','')))}) style={el.get('style')} "
                            f"size_pt={el.get('size_pt')} color={el.get('color')}"}
    if sec == "containers":
        sh = el.get("shape")
        if sh == "freeform":
            return {"reason": "native-freeform",
                    "evidence": f"freeform points={len(el.get('points') or [])}"}
        return {"reason": "native-simple",
                # 只记"它是什么"，不记尺寸：尺寸是 spec 的派生值，改一次 bbox 这里的数字就过期，
                # 而且没有任何检查会发现三处不一致（spec / override / evidence）。
                "evidence": f"shape={sh or 'rounded_rect'} fill={_hex(el)}"}
    if sec == "assets":
        m = el.get("mask")
        if m == "circle" and el.get("circle"):
            return {"reason": "raster-circle",
                    "evidence": f"mask=circle r={el['circle'][2]} bbox={el['bbox'][2]-el['bbox'][0]}x{el['bbox'][3]-el['bbox'][1]}"}
        # α 来自图像本身（模型或颜色/几何判据）的三种归到 raster-matte：
        # cutout=matting、flat=去纯底、pictogram=蓝图元隔离。
        if m in ("cutout", "flat", "pictogram", "chroma"):
            return {"reason": "raster-matte", "evidence": f"mask={m} src={bool(el.get('src'))}"}
        # feather 是"整块留图 + 逐边羽化"，像素来源仍是原图 → 归 raster-crop
        # crop_note 由裁框与羽化参数直接推出来：这些数就是"为什么这么裁"的事实，
        # 让人再抄一遍只会变成空话（"按原图裁剪"）。
        bb = el.get("bbox") or [0, 0, 0, 0]
        fe = el.get("feather_px")
        note = f"原图裁片 ({bb[0]},{bb[1]})-({bb[2]},{bb[3]})"
        if m == "feather" and fe:
            note += f"，四边羽化 {fe}"
        elif m == "rectmask":
            note += f"，羽化 {fe} + 排除矩形 {len(el.get('mask_rects') or [])} 块"
        note += "，像素取自参考图，不做重绘"
        return {"reason": "raster-crop", "evidence": f"mask={m} src={bool(el.get('src'))}",
                "crop_note": note}
    return {"reason": None, "evidence": ""}


HUMAN_KEYS = ("approx_of", "gaps", "crop_note", "locked", "locked_reason",
              "reviewed_on_bg")


def build_inventory(page: Path, spec_path: Path) -> tuple[dict, dict]:
    base = json.loads(spec_path.read_text(encoding="utf-8"))
    view, st = specmerge.apply(base, specmerge.load(spec_path.parent / "override.json"))
    old_path = spec_path.parent / "inventory.json"
    old = {}
    if old_path.exists():
        try:
            old = {i["id"]: i for i in json.loads(old_path.read_text(encoding="utf-8")).get("items", [])}
        except Exception:
            old = {}

    items, hints, skipped_native = [], 0, 0
    for sec in specmerge.SECTIONS:
        for el in view.get(sec, []):
            # extract 的素材候选（review=true 且没 src）只是提示，不是元素，不要求记录
            if sec == "assets" and el.get("review") and not el.get("src"):
                hints += 1
                continue
            d = derive(el, sec)
            # 原生简单形状（rect/rounded_rect/ellipse/freeform 预设）没有"决策"可记：
            # 它是什么、怎么画的，spec 里已经写着。逐条记一遍只是把 spec 抄成散文，
            # P09 的 49 条里 29 条是这种空壳（59%）。
            prev = old.get(el["id"], {})
            # 空壳一律不留（包括旧页面里的）：原生形状 + 没写过任何人工字段，
            # 这条记录的信息量等于零。老页面重跑一次就瘦身。
            # 2026-11-09：判"有没有写过"必须走完整的 HUMAN_KEYS——以前只认 gaps/approx_of/locked，
            # 于是"只写了 reviewed_on_bg 的块"会被当成空壳连行带签名一起删掉（实测 min 页复现）。
            # 但必须先 `bool(prev)` 兜住"根本没有旧行"的情况：prev={} 时 prev.get("reason") 是 None，
            # `None != "native-simple"` 恒真 → 会把**所有**空壳都当成人工写过而留下（实测 40 页 14→50 行、
            # 而且不幂等：第二次重建又缩回去，plan 依赖键在两种行数之间来回翻）。
            prev_human = bool(prev) and (
                any(prev.get(k) for k in HUMAN_KEYS)
                or prev.get("reason") != d["reason"])
            if d["reason"] == "native-simple" and not prev_human:
                skipped_native += 1
                continue
            row = {"id": el["id"], "section": sec, "kind": REASON_KIND.get(d["reason"], NATIVE),
                   "reason": d["reason"], "decided_by": "auto", "decided_at_q": None,
                   "evidence": d["evidence"], "approx_of": None, "gaps": [],
                   "crop_note": d.get("crop_note"), "locked": False, "locked_reason": None,
                   "reviewed_on_bg": None}
            # 人写过的字段一律保留（这是"决策记录"，不是可重算的测量值）
            for k in ("reason",) + HUMAN_KEYS:
                if k in prev and prev[k] is not None:
                    row[k] = prev[k]
            # 只有"存在旧记录且被改过"才算人工；首次生成时 prev 为空，不能算
            if prev and (any(prev.get(k) for k in HUMAN_KEYS) or prev.get("reason") != d["reason"]):
                row["decided_by"] = "human"
            row["kind"] = REASON_KIND.get(row["reason"], row["kind"])
            items.append(row)
    if hints:
        print(f"  跳过 {hints} 个素材候选（extract 的 hint，未定 mask/src）")
    out = {"page": page.name, "spec": spec_path.name,
           "canvas": (base.get("canvas") or {}), "items": items}
    return out, st


def plan_deps(spec_path: Path, inv: dict) -> dict:
    """plan 闸的依赖：只含当前制作数据与决策字段，**不含任何渲染报告**。

    以前 plan 会读 quality.json 的失败项来要求 gaps，于是「上一版输入的失败报告」
    能把修后的 build 挡死（2026-11-07 P08 实测：改完红点必须先 mv quality.json 才能重建）。
    现在 plan 的输入就是这份函数返回的内容；quality/verify 改不改都不影响它。
    """
    # 与 build/spec_lint **共用同一解析**（原来这里自己 apply(load(override))，是第二套合并；
    # 两边一旦分叉，改 token 不会让 plan 报告失效 → 读到旧报告 → 假绿）。
    # 注意 expand=False：plan 依赖键要记的是**raw**（文件里真正写了什么），不是展开值。
    view = specmerge.load_merged(spec_path, quiet=True)
    production = {sec: view.get(sec) or [] for sec in specmerge.SECTIONS}
    production["background"] = view.get("background") or {}
    production["canvas"] = view.get("canvas") or {}
    decisions = []
    for row in (inv.get("items") or []):
        decisions.append({k: row.get(k) for k in
                          ("id", "reason", "kind", "approx_of", "gaps", "crop_note",
                           "locked", "locked_reason", "evidence")})
    # 常量源也是 plan 的输入：`visual.json` 改了，plan 的结论就该失效。
    # 不把它的 md5 放进依赖，等于允许"改了调色板但 plan 还是绿的"——这正是 report_state 记过的事故形状。
    sources = {}
    for name, cand in (("visual", [Path(spec_path).parent / "visual.json",
                                   Path(spec_path).parent.parent / "visual.json"]),
                       ("deck", [Path(spec_path).parent / "deck.json",
                                 Path(spec_path).parent.parent / "deck.json"])):
        for c in cand:
            if c.exists():
                sources[f"{name}_md5"] = hashlib.md5(c.read_bytes()).hexdigest()
                sources[f"{name}_path"] = c.name
                break
    return {"production": production, "decisions": decisions, "sources": sources}


# ---------------------------------------------------------------- 校验
def check(page: Path, spec_path: Path, inv: dict) -> tuple[list[str], list[str]]:
    errs, warns = [], []
    if not isinstance(inv, dict):
        return ["inventory.json 不是对象（报告损坏）"], warns
    if not isinstance(inv.get("items"), list):
        return ["inventory.json 缺 items 数组（报告损坏，不能当成空清单通过）"], warns
    try:
        base = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"spec.json 解析失败：{exc}"], warns
    view, _ = specmerge.apply(base, specmerge.load(spec_path.parent / "override.json"))
    els = {}
    for sec in specmerge.SECTIONS:
        for el in view.get(sec, []):
            if not el.get("id"):
                continue
            # 与 build 同口径：extract 的素材候选（review 且无 src）不画，也不要求记录
            if sec == "assets" and el.get("review") and not el.get("src"):
                continue
            els[el["id"]] = (sec, el)

    rows = inv.get("items") or []
    seen = set()
    for row in rows:
        eid = row.get("id")
        if eid in seen:
            errs.append(f"id 重复：{eid}")
        seen.add(eid)
        if eid not in els:
            errs.append(f"inventory 有孤儿行：{eid}（spec/override 里没有这个元素）")
            continue
        sec, el = els[eid]
        reason = row.get("reason")
        if reason not in REASON_KIND:
            errs.append(f"{eid}: reason={reason!r} 不在六类内")
            continue
        if row.get("kind") != REASON_KIND[reason]:
            errs.append(f"{eid}: kind={row.get('kind')!r} 与 reason={reason} 不匹配")
        if not str(row.get("evidence") or "").strip():
            errs.append(f"{eid}: 缺 evidence（决策记录不许是空话）")
        # reviewed_on_bg：换底色目检的签字。它不在白名单里的时候会被 rebuild 静默抹掉
        # （2026-11-09 实测：40 页 4 条签字跑一次 --write 全没了），所以要能搬回。
        # 校验只进 warns：签字是**记录**不是闸（它证明不了"真看过图"），所以
        # "签残了"不该阻塞交付；但至少要提醒一句，否则拼错一个键就等于悄悄没签。
        rv = row.get("reviewed_on_bg")
        if rv is not None:
            if not isinstance(rv, dict):
                warns.append(f"{eid}: reviewed_on_bg 必须是对象（at/by/verdict），"
                             f"现在是 {type(rv).__name__}（签字无效，按未签处理）")
            else:
                miss = [k for k in ("at", "by", "verdict")
                        if not str(rv.get(k) or "").strip()]
                if miss:
                    warns.append(f"{eid}: reviewed_on_bg 缺 {', '.join(miss)}"
                                 f"（签字无效，按未签处理；签名要能追溯到人、时间与结论）")
        # 分类 ↔ 字段
        if reason == "native-simple":
            if el.get("src") or el.get("mask") or el.get("points"):
                errs.append(f"{eid}: 声称 native-simple 但有 src/mask/points")
        elif reason == "native-freeform":
            if el.get("shape") != "freeform" or len(el.get("points") or []) < 3:
                errs.append(f"{eid}: 声称 native-freeform 但 shape={el.get('shape')} points={len(el.get('points') or [])}")
        elif reason == "approx-shape":
            if not el.get("shape"):
                errs.append(f"{eid}: 声称 approx-shape 但没有 shape")
            if not str(row.get("approx_of") or "").strip():
                errs.append(f"{eid}: approx-shape 缺 approx_of（近似的是什么）")
            if not [g for g in (row.get("gaps") or []) if str(g).strip()]:
                errs.append(f"{eid}: approx-shape 缺 gaps（差在哪，要带数字）")
        elif reason == "raster-circle":
            c = el.get("circle")
            if el.get("mask") != "circle" or not c:
                errs.append(f"{eid}: 声称 raster-circle 但 mask={el.get('mask')} circle={bool(c)}")
            else:
                cx, cy, r = [float(v) for v in c]
                bx0, by0, bx1, by1 = [float(v) for v in el["bbox"]]
                margin = min(cx - r - bx0, cy - r - by0, bx1 - cx - r, by1 - cy - r)
                if margin < 2:
                    errs.append(f"{eid}: 圆形素材没有羽化余量（只多 {margin:.0f}px，需 ≥2）——bbox 每边外扩 3px")
        elif reason in ("raster-matte", "raster-crop"):
            if not el.get("src"):
                errs.append(f"{eid}: {reason} 缺 src")
            else:
                p = Path(el["src"])
                cand = [p] if p.is_absolute() else [spec_path.parent / p, spec_path.parent.parent / p]
                if not any(c.exists() and c.suffix.lower() in IMG_EXT for c in cand):
                    errs.append(f"{eid}: src 文件不存在或格式不支持：{el['src']}")
            if reason == "raster-crop" and not str(row.get("crop_note") or "").strip() \
                    and not el.get("mask"):
                errs.append(f"{eid}: raster-crop 既没有 crop_note 也没有 mask 参数，无法判断它怎么来的")

        # gaps 通用约束（不专属某一类）
        gaps = row.get("gaps")
        if gaps not in (None, []) and not isinstance(gaps, list):
            errs.append(f"{eid}: gaps 必须是数组")
        for g in (gaps or []):
            if not str(g).strip():
                errs.append(f"{eid}: gaps 里有空条目")

    # 漏记只对"有决策可记"的元素报：原生简单形状由 spec 自证，不要求逐条记录。
    missing = [m for m in sorted(set(els) - seen)
               if derive(els[m][1], els[m][0]).get("reason") != "native-simple"]
    if missing:
        errs.append(f"inventory 漏记 {len(missing)} 个元素：{', '.join(missing[:6])}")

    # 参考图尺寸
    canvas = base.get("canvas") or {}
    ref_img = None
    for n in ("reference.png", "reference.jpg", "reference.jpeg"):
        if (spec_path.parent / n).exists():
            ref_img = spec_path.parent / n
            break
    if not ref_img:
        errs.append("找不到 reference.png/jpg，无法核对尺寸")
    else:
        try:
            from PIL import Image
            with Image.open(ref_img) as im:
                actual = im.size
            if (int(canvas.get("w_px", 0)), int(canvas.get("h_px", 0))) != actual:
                errs.append(f"参考图尺寸不符：spec {canvas.get('w_px')}x{canvas.get('h_px')} vs 实际 {actual[0]}x{actual[1]}")
        except Exception as exc:
            errs.append(f"读参考图失败：{exc}")

    # 这里**不再**读 quality.json：
    # 旧规则「quality 任一失败项必须写 gaps 才能过 plan」会把上一版输入的诊断
    # 变成修后构建的硬门闩（2026-11-07 P08 实测：改红点后必须手工移走 quality.json）。
    # 真实近似/允许差异仍要写 gaps——由 approx-shape 的分类约束和人的判断触发，
    # 不靠一份可能过期的报告兜底。
    #
    # 「裁片是否压住文字」统一由 delivery/asset_guard.py 的 I1 判（按像素相交，比"文字中心点"
    # 严且不误伤）。这里不再重复一份判据 —— 两套规则并存时，改了一处另一处就变成假错/假过。

    return errs, warns

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    page = Path(a.page).resolve()
    spec_path = next((p for p in (page / "spec.json", page / "page" / "spec.json") if p.exists()), None)
    if not spec_path:
        raise SystemExit(f"REFUSED: {page} 下没有 spec.json")

    if a.check:
        # 校验的是**磁盘上那份**记录，不是刚生成的副本——否则人改了文件也校验不到，
        # 记录就成了摆设（实测：删掉 evidence 仍能 PASS）。
        inv_path = spec_path.parent / "inventory.json"
        if not inv_path.exists():
            raise SystemExit(f"REFUSED: 缺 {inv_path.name}，先跑 --write 生成")
        inv = json.loads(inv_path.read_text(encoding="utf-8"))
    else:
        inv, st = build_inventory(page, spec_path)
    if a.write or not a.check:
        out = spec_path.parent / "inventory.json"
        out.write_text(json.dumps(inv, ensure_ascii=False, indent=1), encoding="utf-8")
        from collections import Counter
        c = Counter(i["reason"] for i in inv["items"])
        print(f"写 {out.name}：{len(inv['items'])} 条  " + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
        print(f"  其中 approx={sum(1 for i in inv['items'] if i['reason'] == 'approx-shape')}  "
              f"有 gaps={sum(1 for i in inv['items'] if i.get('gaps'))}  "
              f"人工改过={sum(1 for i in inv['items'] if i['decided_by'] == 'human')}")

    if a.check:
        errs, warns = check(page, spec_path, inv)
        for w in warns:
            print("  WARN " + w)
        if errs:
            for e in errs:
                print("  FAIL " + e)
            print(f"\n{len(errs)} 项问题")
            return 1
        print(f"  PASS {len(inv['items'])} 条记录；覆盖完整 / 六类约束 / 参考尺寸 / 裁片不压字 / id 唯一")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
