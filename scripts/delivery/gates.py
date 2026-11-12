#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gates.py — 阶段闸：plan / assets / presence / delivery。

两条铁律：
  1. **闸只消费有效报告**。报告的有效性由 dependency_key 决定（report_state.py），
     不再用「spec+override 原始字节相同」当指纹：改 JSON 缩进不该失效，改文字颜色不该
     让素材检查失效，上一版输入的 quality.json 也不该挡住修后的 build。
  2. **阶段不可跳**。build 读 plan 报告；delivery 要求 plan/assets/build 都 current+pass。

默认只读不渲染：需要渲染的必需报告（editability）由 cycle 统一准备；
直接调 gates 时缺报告会明确报错。要就地生成加 `--produce`（会显式说明成本）。

用法：
  python gates.py --stage plan      --page <页>
  python gates.py --stage assets    --page <页> [--produce]
  python gates.py --stage presence  --page <页> [--out out.pptx]
  python gates.py --stage delivery  --page <页> [--produce]
                                    [--width 1672 --height 941]
退出码：0 过 / 1 不过
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))            # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))                   # delivery/
import specmerge
import inventory as inv_mod
import report_state as rs
from consts import shape_id    # 「这形状是谁」的唯一取值处（name 可以是人写的别名）
# 素材质量判据（C5）的实现留在 gate_v2 里，这里只接线——两条检查共用一份代码，
# 免得同一判据在库里长出两份（本库历史上为此吃过多次"两份实现算出两个结论"的亏）。
from gate_v2 import c5_assets

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent.parent
PY = sys.executable


def _md5(p: Path) -> str:
    return rs.file_digest(p) or ""


def _find_spec(page: Path) -> Path:
    for c in (page / "spec.json", page / "page" / "spec.json"):
        if c.exists():
            return c
    raise SystemExit(f"REFUSED: {page} 下没有 spec.json")


def _run(cmd: list) -> tuple[int, str]:
    r = subprocess.run([PY] + [str(c) for c in cmd], capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _read_json(path: Path) -> tuple[dict | None, str]:
    """读 JSON：损坏就报错，禁止把解析异常当成空集合（然后"没有失败项"通过）。"""
    if not path.exists():
        return None, f"缺 {path.name}"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"{path.name} 解析失败：{exc}"
    if not isinstance(doc, dict):
        return None, f"{path.name} 不是对象"
    return doc, ""


# ---------------------------------------------------------------- plan
def check_plan(page: Path, spec_path: Path) -> tuple[list[str], list[str]]:
    errs, notes = [], []
    inv_path = spec_path.parent / "inventory.json"
    inv, err = _read_json(inv_path)
    if err:
        return [f"{err}（先跑 inventory.py --write）"], notes
    e1, w1 = inv_mod.check(page, spec_path, inv)
    errs += [f"inventory：{e}" for e in e1]
    notes += [f"inventory：{w}" for w in w1]

    rc, out = _run([SKILL / "scripts/delivery/spec_lint.py", "--spec", spec_path])
    if rc != 0:
        errs += [f"spec_lint: {ln.strip()}" for ln in out.splitlines() if ln.strip().startswith("FAIL")]

    notes.append("composite_panels: 未实现（缺 group_id 字段），本阶段不检查")
    notes.append("plan 不读取 quality/verify 的结论：那两份是诊断，且可能来自上一版输入")
    return errs, notes


# ---------------------------------------------------------------- assets
def _assetcheck_key(spec_path: Path) -> str:
    return rs.key_assets(spec_path)


def check_assets(page: Path, spec_path: Path, produce: bool = False) -> tuple[list[str], list[str]]:
    errs, notes = [], []
    # 0) 素材裁框的两条硬不变量（压字 / 空产物）。以前这套逻辑散在 inventory.check 里，
    #    只管"文字中心点是否落在裁片内"，小图标压住半个字会漏；统一交给 asset_guard。
    rc, out = _run_tool("asset_guard.py", ["--page", str(page)])
    if rc != 0:
        tail = [ln for ln in out.strip().splitlines() if ln.startswith("FAIL")][:6]
        errs += ["asset_guard: " + t.replace("FAIL ", "") for t in tail] or ["asset_guard 未过"]
    base = json.loads(spec_path.read_text(encoding="utf-8"))
    view, _ = specmerge.apply(base, specmerge.load(spec_path.parent / "override.json"))
    inv, _ = _read_json(spec_path.parent / "inventory.json")
    raster = {i["id"]: i for i in (inv or {}).get("items", []) if str(i.get("kind")) == "raster"}

    # 1) 每个 raster 的 src 必须存在
    from PIL import Image
    for el in view.get("assets", []):
        src = el.get("src")
        if not src:
            errs.append(f"{el.get('id')}: raster 没有 src")
            continue
        p = Path(src)
        cand = [p] if p.is_absolute() else [spec_path.parent / p, spec_path.parent.parent / p]
        f = next((c for c in cand if c.exists()), None)
        if not f:
            errs.append(f"{el.get('id')}: src 文件不存在 {src}")
            continue
        # 2) 四角 alpha=0（方片底会露直角）；羽化裁片（raster-crop）不查——它的四角本来就该是半透明渐变
        reason = (raster.get(el.get("id")) or {}).get("reason")
        try:
            with Image.open(f) as im:
                im = im.convert("RGBA")
                a = im.getchannel("A")
                if a.getbbox() is None:
                    errs.append(f"{el.get('id')}: 素材全透明（alpha 全 0）")
                    continue
                corners = [a.getpixel((0, 0)), a.getpixel((im.width - 1, 0)),
                           a.getpixel((0, im.height - 1)), a.getpixel((im.width - 1, im.height - 1))]
                if reason in ("raster-circle", "raster-matte") and max(corners) > 8:
                    errs.append(f"{el.get('id')}: 四角 alpha={corners}（应≈0）——方片底会露直角")
        except Exception as exc:
            errs.append(f"{el.get('id')}: 读素材失败 {exc}")

    # 3) assetcheck 报告：必须存在且依赖 current（源图/素材/裁框/掩膜/邻接几何）
    ac = spec_path.parent / "assetcheck.json"
    key = _assetcheck_key(spec_path)
    rd = rs.read_report(ac, key)
    if produce and not rd["passed"]:
        rc, out = _run([SKILL / "scripts/assetcheck.py", "--spec", spec_path,
                        "--reference", spec_path.parent / "reference.png",
                        "--out", ac])
        rd = rs.read_report(ac, _assetcheck_key(spec_path))
        notes.append(f"assetcheck 已重跑（exit={rc}）")
    if not rd["passed"]:
        if rd["validity"] in ("missing", "stale", "invalid"):
            errs.append(f"assetcheck 不可用：{rs.describe(rd)}")
        else:
            d = rd.get("report") or {}
            bad = [i.get("id") for i in d.get("items", []) if not i.get("ok")]
            errs.append(f"assetcheck 未过：{', '.join(str(b) for b in bad) or '见报告'}")
    else:
        for i in (rd.get("report") or {}).get("items", []):
            # **否定式判断**，不枚举状态名：枚举会给下一个新状态留一次漏的机会
            # （这轮已经漏过两次：uncovered→unproven、_border_bg→_asset_bg）。
            # 非 pass（含 unproven：颜色类判据没评）一律进 notes 点名，否则它在统计里
            # 与 pass 长得一样，"未证明"又一次被当通过。
            # 但它**永不阻塞**——阻塞会逼人去开 locked 旁路，那就重建了 cover 时代的老路。
            st = str(i.get("status") or "pass")
            if st != "pass":
                notes.append(f"{i.get('id')}: 未证明（{st}）——不阻塞，交付说明需点名")
            elif i.get("warn") and i.get("id") in raster:
                notes.append(f"{i['id']}: assetcheck 有 WARN（{i['warn']}）——需换底色目检")
    return errs, notes


# ---------------------------------------------------------------- presence
def _run_tool(name: str, args: list[str]) -> tuple[int, str]:
    r = subprocess.run([PY, str(HERE / name)] + [str(a) for a in args], capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def check_presence(page: Path, spec_path: Path, out_path: Path) -> tuple[list[str], list[str]]:
    """元素在场检查：spec 里声明的每个 id，成品 pptx 里必须恰好有一个同名 shape。

    为什么必须有这条（2026-11-07）：所有闸都是"看差异"，一条图缺了只贡献零点几像素，
    平均下来谁都看不出来。page 32 就有 4 张图 spec 有、pptx 里根本没有（rect 没有产出路径），
    而 look/verify/quality 全部 PASS——这是最典型的"闸全绿但东西不对"。
    这条不看图，只比集合，所以它抓的是"在不在"，不是"像不像"。

    「同名」的取值来源是 consts.shape_id（cNvPr@title 优先，否则 name）——spec 起了
    自定义 name 之后 name 不再等于 id，判据不变但取值必须走那一处，否则这条闸会报
    「spec 声明了但 pptx 里没有」。
    """
    errs: list[str] = []
    if not out_path.exists():
        return [f"presence 无法执行：缺成品 {out_path.name}"], []
    try:
        from pptx import Presentation
        spec = rs.merged_view(spec_path)
        want = [e["id"] for sec in ("containers", "assets", "elements") for e in spec.get(sec, [])]
        prs = Presentation(str(out_path))
        got = [shape_id(sh) for sh in prs.slides[0].shapes]
    except Exception as exc:
        return [f"presence 无法执行：在场检查读不出来：{exc}"], []
    missing = [i for i in want if i not in got]
    extra = sorted({i for i in got if i not in set(want)})
    dup = sorted({i for i in got if got.count(i) > 1})
    if missing:
        errs.append(f"spec 声明了但 pptx 里没有（{len(missing)} 个）：{', '.join(missing[:8])}"
                    f"—— 素材缺 src / mask 没人产图 / 文字被 skip 都会这样")
    if dup:
        errs.append(f"pptx 里重复的 shape 名：{', '.join(dup[:8])}")
    if extra and len(extra) <= 5:
        errs.append(f"pptx 里有 spec 没声明的 shape：{', '.join(extra)}")
    return errs, []


# ---------------------------------------------------------------- delivery
def check_delivery(page: Path, spec_path: Path, out_path: Path, rendered: Path,
                   allow_produce: bool = False,
                   width: int | None = None, height: int | None = None) -> tuple[list[str], list[str]]:
    """交付闸。`width/height` = **实际渲染尺寸**（cycle 的 --width/--height）。

    以前这里一律按 spec.canvas 算 size，于是 `cycle --width 999 --height 555` 时
    build 报告是 999×555、gates 却按 1672×941 去算键 → 「build/render 不可用」永远成立
    （2026-11-08 QA 实测）。现在以调用方传入值为准，缺省才回退 spec.canvas。
    """
    errs, notes = [], []
    d = spec_path.parent
    for rel in (out_path, rendered):
        if not rel.exists():
            errs.append(f"缺产物 {rel.name}")

    # 便宜条件先查：plan / assets / build 的 key 只需要读文件与摘要
    inv, inv_err = _read_json(d / "inventory.json")
    plan_rd = rs.read_report(d / "_check" / "plan.json",
                             rs.key_plan(spec_path, inv if inv else {"items": []}))
    assets_rd = rs.read_report(d / "_check" / "assets.json", rs.key_assets(spec_path))
    for name, rd in (("plan", plan_rd), ("assets", assets_rd)):
        if not rd["passed"]:
            errs.append(f"{name} 闸不可用：{rs.describe(rd)}")
            rep = rd.get("report") or {}
            if rep.get("errors"):
                errs.append(f"{name} 闸的问题：{rep['errors'][0]}")
    if inv_err:
        errs.append(inv_err)

    cw, ch = _canvas_size(spec_path)
    width = int(width or cw)
    height = int(height or ch)
    build_rd = rs.read_report(d / "_check" / "build.json",
                              rs.key_build(spec_path, out_path, width, height))
    if not build_rd["passed"]:
        errs.append(f"build/render 不可用：{rs.describe(build_rd)}")

    # 元素在场：读 pptx 很便宜，且它抓的是"整块缺失"，每次成品变了都重算
    if out_path.exists():
        e_pres, _ = check_presence(page, spec_path, out_path)
        pkey = rs.key_presence(spec_path, out_path)
        rs.write_report(d / "_check" / "presence.json",
                        {"stage": "presence", "kind": "presence",
                         "outcome": "pass" if not e_pres else "fail", "errors": e_pres},
                        pkey)
        errs += e_pres

    # 可编辑：有有效报告就复用，不重复渲染
    edit_rd = rs.read_report(d / "_check" / "editability.json",
                             rs.key_editability(spec_path, out_path, width, height))

    # 便宜条件有阻塞时不再启动渲染型报告（editability 要渲两次）
    if allow_produce and not errs:
        if not edit_rd["passed"] and out_path.exists():
            rc, out = _run_tool("editability.py", ["--page", str(page), "--spec", str(spec_path),
                                                   "--out", str(out_path),
                                                   "--width", width, "--height", height])
            notes.append(f"editability 已重跑（exit={rc}）")
            edit_rd = rs.read_report(d / "_check" / "editability.json",
                                     rs.key_editability(spec_path, out_path, width, height))

    if not edit_rd["passed"]:
        errs.append(f"可编辑检查不可用：{rs.describe(edit_rd)}")

    # 决策记录自洽（不读 quality）
    if inv:
        e2, w2 = inv_mod.check(page, spec_path, inv)
        errs += [f"inventory：{e}" for e in e2]
        notes += [f"inventory：{w}" for w in w2]

    # 素材质量：WARN 必须**被看见**，但不拦交付（C5）。
    # 曾经接成阻塞（没签就 blocked），当天就撤了——实测 `{"at":"x","by":"x","verdict":"x"}`
    # 与"真看过图"在机器眼里完全一样，它只能证明"有人打了字"，不能证明"有人看了画"。
    # 项目自己的规矩是"豁免只能靠修正判据，不许开旁路"，而签字就是一条旁路。
    # 真正缺的不是签字，是**可见性**：cycle 只读 errors、把 notes 丢掉，WARN 从来没出现在终端。
    # 所以这里只往 notes 里塞（带「素材：」前缀，cycle 会把它印成"记录 N 项（不阻塞交付）"）。
    try:
        c5 = c5_assets(page)
        notes += [f"素材：{n}" for n in c5["notes"]]
        notes += [f"素材：{f}" for f in c5["fails"]]
    except Exception as exc:                       # noqa: BLE001
        notes.append(f"素材：质量检查跑不起来（只记录）：{exc}")

    # OOXML：每个文字 run 必须有东亚字体
    if out_path.exists():
        try:
            with zipfile.ZipFile(out_path) as z:
                names = [n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
                miss_ea = 0
                runs = 0
                for n in names:
                    xml = z.read(n).decode("utf-8")
                    runs += xml.count("<a:rPr")
                    miss_ea += max(0, xml.count("<a:rPr") - xml.count("<a:ea "))
                if runs and miss_ea:
                    errs.append(f"{miss_ea}/{runs} 个文字 run 缺 <a:ea typeface>（中文会回退宋体）")
        except Exception as exc:
            errs.append(f"读 pptx 失败：{exc}")
    return errs, notes


def delivery_deps(spec_path: Path, out_path: Path, rendered: Path, d: Path,
                  width: int | None = None, height: int | None = None) -> dict:
    """交付依赖键的唯一定义处。

    以前 gates 与 cycle 各写一份，两份算出不同的键 → gates 刚写完的 delivery.json
    立刻被 cycle 判 stale → cycle 读不到 errors，只能显示「delivery fail」但说不出原因。
    这类"同一判据两份实现"已在本库制造过多次事故，这里收成一处。

    `width/height` 必须由调用方传下来（cycle 的 --width/--height 就是渲染尺寸）：
    以前这两个参数被签名吞掉，一律按 spec.canvas 算 → 自定义尺寸时 build 键必然对不上，
    `--width 999 --height 555` 永远「build 不可用」（2026-11-08 QA 实测）。

    `rendered` 保留在签名里：它仍是 delivery 的输入之一（缺产物即 fail），只是
    像素相似度判据退役后不再有以它为主题的子键。
    """
    cw, ch = _canvas_size(spec_path)
    w = int(width or cw)
    h = int(height or ch)
    return {
        "plan": rs.key_plan(spec_path, (_read_json(d / "inventory.json")[0] or {"items": []})),
        "assets": rs.key_assets(spec_path),
        "build": rs.key_build(spec_path, out_path, w, h),
        "presence": rs.key_presence(spec_path, out_path),
        "editability": rs.key_editability(spec_path, out_path, w, h),
    }


def _canvas_size(spec_path: Path) -> tuple[int, int]:
    try:
        cv = json.loads(spec_path.read_text(encoding="utf-8")).get("canvas") or {}
        return int(cv.get("w_px") or 1672), int(cv.get("h_px") or 941)
    except Exception:
        return 1672, 941


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("plan", "assets", "presence", "delivery"),
                    help="要跑的阶段")
    ap.add_argument("--page", required=True, help="页面目录")
    ap.add_argument("--spec", default=None)
    ap.add_argument("--out", default=None, help="成品 pptx（默认 out.pptx）")
    ap.add_argument("--rendered", default=None, help="渲染图（默认 look/rendered.png）")
    ap.add_argument("--width", type=int, default=None, help="实际渲染宽（缺省取 spec.canvas）")
    ap.add_argument("--height", type=int, default=None, help="实际渲染高（缺省取 spec.canvas）")
    ap.add_argument("--produce", action="store_true",
                    help="就地生成缺失的必需报告（会明确说明；默认只消费有效报告）")
    a = ap.parse_args()

    page = Path(a.page).resolve()
    spec_path = Path(a.spec).resolve() if a.spec else _find_spec(page)
    d = spec_path.parent
    out_path = Path(a.out).resolve() if a.out else d / "out.pptx"
    if a.rendered:
        rendered = Path(a.rendered).resolve()
    elif out_path.name == "out.pptx":
        rendered = d / "look" / "rendered.png"
    else:
        rendered = d / f"look_{out_path.stem}" / "rendered.png"
    (d / "_check").mkdir(exist_ok=True)

    if a.stage == "plan":
        errs, notes = check_plan(page, spec_path)
        inv, _ = _read_json(d / "inventory.json")
        key = rs.key_plan(spec_path, inv if inv else {"items": []})
    elif a.stage == "assets":
        errs, notes = check_assets(page, spec_path, produce=a.produce)
        key = rs.key_assets(spec_path)
    elif a.stage == "presence":
        errs, notes = check_presence(page, spec_path, out_path)
        key = rs.key_presence(spec_path, out_path)
    else:
        errs, notes = check_delivery(page, spec_path, out_path, rendered,
                                     allow_produce=a.produce,
                                     width=a.width, height=a.height)
        key = rs.dependency_key("delivery", delivery_deps(spec_path, out_path, rendered, d,
                                                          a.width, a.height))

    rs.write_report(d / "_check" / f"{a.stage}.json",
                    {"stage": a.stage, "kind": a.stage, "outcome": "pass" if not errs else "fail",
                     "errors": errs, "notes": notes,
                     "spec_md5": _md5(spec_path)},
                    key)

    for n in notes:
        print(f"  NOTE {n}")
    if errs:
        for e in errs:
            print(f"  FAIL {e}")
        print(f"\n{len(errs)} 项问题 —— {a.stage} 未通过")
        return 1
    print(f"  PASS {a.stage}（{len(notes)} 条说明）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
