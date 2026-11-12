#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_state.py — 报告依赖摘要与有效性判定（唯一实现）。

为什么需要它（2026-11-07 根因）：
  旧报告用「spec+override 的原始字节 md5」当指纹，于是
    ① 只改 JSON 缩进 → 全部报告失效；
    ② 只改一个文字颜色 → 素材检查也失效；
    ③ quality.json 是上一版输入的失败报告，却能把修后的 plan/build 挡死。
  这里把「报告是否有效」改成一条可解释的合同：
    dependency_key(kind, dependencies, config, checker_version) -> str
    read_report(path, expected_key) -> {validity, outcome, report}
    write_report(path, payload, expected_key) -> None

约定（全体读写者必须一致）：
  * dependencies 是调用方明确给出的**语义摘要**（文件内容 md5 或规范化数据），不是 mtime；
  * JSON 规范化只排序对象键，**保留数组顺序**（数组顺序影响绘制，不能粗暴排序）；
  * validity 取 current / stale / missing / invalid；
  * outcome 取 pass / fail / error / skipped；
  * `passed` 只允许从「current 且 pass」派生——报告自称 passed 不算数；
  * 没有 dependency_key 的旧格式一律 invalid（需重验），不猜测通过；
  * 写入是原子的（临时文件 + os.replace），不存在半份报告。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

SCHEMA = 1

# 每类检查的实现版本。改了判据/阈值语义就升这里，旧报告自动失效。
# 2.35：plan 的 spec_lint ④ 溢出判据从「整块文本」改成「最宽的一行」（多行块不再被 continue 跳过），
#       字宽改用 style.sum_adv（带 weight），行尾标点按 0.5em 悬挂；
#       delivery 首次登记版本——此前 CHECKER_VERSION 没有 "delivery" 项，一直回落成 "0"，
#       残差判据改了也没法让旧 delivery 报告失效（gates 与 cycle 都走这个 dict，单点改）。
# 2.36：delivery 判据换了一处语义——残差「漏/多」的裁决从「一律不阻塞」改成
#       cycle 侧按四类分流（未豁免的漏 / 多 / 没写 reason 一律阻塞）。旧 delivery 报告
#       是拿旧判据算的 pass，必须失效，否则这一页会带着旧结论直接 ready。
# 2.36.1：两处判据换语义，对应的旧报告一律失效（不升版会让旧 pass 带着旧判据继续放行）：
#       plan       spec_lint 必填字段扩到 elements **全集**（text/size_pt/color 都要）——
#                  旧报告是"跳过一半元素"得出的 pass；
#       editability / verify / quality
#                  「文字块」判据从 is_text_el 改成 drawn_as_text（= elements 段全集），
#                  遍历范围变大，旧结论没量过新增的块。
# 2.37：delivery 的依赖子键少了一项（gates.delivery_deps 不再含已删除的那个子键），
#       旧 delivery 报告因此自然判 stale，不需要再升版本号——保持 2.36.3-delivery5。
# 第 ④ 步 4A：像素相似度判据整条退役，"io" 与 "residual" 两个版本条目随之删除
#       （它们的报告已无生产者与消费者）。delivery 的依赖子键再少两项，
#       旧 delivery 报告同样自然判 stale，仍然不必升号。
# 2.43.0：delivery 报告从"只有 errors"变成"多一行「素材：」note"（C5 素材 WARN 可见化），
#       结论取值不变、内容变了，所以升号——旧报告没有那行，读的人会以为这页没查过素材。
# 2.43.8：assetcheck 新增「过覆盖」判据（rectness + carry），feather/rectmask 的方块裁片
#       从 pass 变 fail——旧 assets 报告是拿旧判据算出来的 pass，必须失效。
#       delivery 的依赖键里含 assets 子键（`delivery_deps`），所以旧 delivery 报告自然判 stale，
#       不必再动它那一行（与 2.37 的处置一致）。
CHECKER_VERSION = {
    "plan": "2.36.1-plan5",
    "assets": "2.43.8-assets3",
    "build": "2.32-build2",
    "presence": "2.26-presence1",
    "editability": "2.36.1-edit3",
    "delivery": "2.36.4-delivery6",
}

VALIDITY = ("current", "stale", "missing", "invalid")
OUTCOMES = ("pass", "fail", "error", "skipped")

# assetcheck 的阈值默认值：键的唯一来源（改这里，旧素材报告自动 stale）。
# cover_fail：判「主体被吃掉」的线；cover_tol：只报警的线（边缘淡了一点，肉眼看不出来）。
# 两档的意义：0.955 那种差值判 FAIL，只会逼人放大裁框重抠，画面一个字都不变。
# rectness_fail / carry_dist / carry_warn / carry_fail：2026-11-09 新增的「过覆盖」四个阈值。
#   cover 是**单向**判据（只问该盖的主体盖住没有），整块不透明矩形贴上去也能 cover=1.00，
#   于是"把原图连底色裁一块"必然通关。rectness 补反向那一半：真抠图的实心区不可能是矩形。
#   但**rectness 单独不足以定性**：一块纯黑不透明方块同样 rectness=1.00，它并没有"携带底色"。
#   所以判死要两个条件同时成立：实心区是矩形（rectness≥0.95）**且**矩形里确实装着原图底色
#   （carry>12%）。后者才是"把背景搬进来"的定义。
#   标定（8 页 34 个真实素材）：真抠图 rectness 0.206~0.836、carry 0.00~0.11，两条线都够不着；
#   已知坏例 a_virus_l 0.998/27%、a_virus_r 0.995/31%、a_breast 1.000/16%、a_uterus 1.000/24%、
#   a_shield 1.000/51% —— 全部命中；纯黑压纯白的合成方块 carry≈0，被 carry 那条线排除。
#   carry_dist=25 是"算不算贴近原图底色"的颜色距离，carry_warn=25% 是只报警的线。
#   carry 是等色判据，不是形状判据：等色半径 25 ≈ 每通道 14.4（偏 14 仍 1.0，偏 15 归 0）。
#   渐变底上"离裁框四边中位色"只给 ±25，会把真实 100% 携带报成 47%——方向安全（仍 FAIL），
#   但文案里的百分比会低估真实携带量。
ASSETCHECK_DEFAULTS = {"cover_tol": 0.97, "cover_fail": 0.90, "core_dist": 70.0,
                       "clear_dist": 6.0, "baked_warn": 0.002,
                       "rectness_fail": 0.95, "carry_dist": 25.0, "carry_warn": 0.25,
                       "carry_fail": 0.12}


# ---------------------------------------------------------------- 摘要
def canon(obj):
    """规范化：对象按键排序，数组保持原序；整数/整值浮点归一。"""
    if isinstance(obj, dict):
        return {str(k): canon(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [canon(v) for v in obj]
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    if isinstance(obj, Path):
        return str(obj)
    return obj


def digest(obj) -> str:
    raw = json.dumps(canon(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def file_digest(path) -> str | None:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dependency_key(kind: str, dependencies, config=None, checker_version: str | None = None) -> str:
    payload = {
        "kind": kind,
        "checker": checker_version or CHECKER_VERSION.get(kind, "0"),
        "dependencies": dependencies or {},
        "config": config or {},
    }
    return digest(payload)


# ---------------------------------------------------------------- 读
def _outcome_of(doc: dict) -> str:
    o = str(doc.get("outcome") or "").strip().lower()
    if o in OUTCOMES:
        return o
    # 兼容：只有 passed 字段的旧报告——仍然受 validity 约束，不会凭自称通过
    if doc.get("passed") is True:
        return "pass"
    if doc.get("passed") is False:
        return "fail"
    return "unknown"


def read_report(path, expected_key: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"validity": "missing", "outcome": "unknown", "report": None,
                "reason": f"缺 {p.name}", "passed": False}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"validity": "invalid", "outcome": "error", "report": None,
                "reason": f"{p.name} 解析失败：{exc}", "passed": False}
    if not isinstance(doc, dict):
        return {"validity": "invalid", "outcome": "error", "report": doc,
                "reason": f"{p.name} 不是对象", "passed": False}
    key = doc.get("dependency_key")
    if not key:
        return {"validity": "invalid", "outcome": _outcome_of(doc), "report": doc,
                "reason": f"{p.name} 是旧格式（无 dependency_key），需重验", "passed": False}
    if key != expected_key:
        return {"validity": "stale", "outcome": _outcome_of(doc), "report": doc,
                "reason": f"{p.name} 的依赖已变化（报告 key {str(key)[:8]}… ≠ 当前 {expected_key[:8]}…）",
                "passed": False}
    outcome = _outcome_of(doc)
    return {"validity": "current", "outcome": outcome, "report": doc,
            "reason": "", "passed": outcome == "pass"}


# ---------------------------------------------------------------- 写
def write_report(path, payload: dict, expected_key: str) -> dict:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    outcome = str((payload or {}).get("outcome") or "").strip().lower()
    if outcome not in OUTCOMES:
        outcome = "pass" if (payload or {}).get("passed") is True else "fail"
    doc = dict(payload or {})
    doc.update({
        "schema": SCHEMA,
        "kind": str((payload or {}).get("kind") or ""),
        "dependency_key": expected_key,
        "outcome": outcome,
        "passed": outcome == "pass",      # 只从 outcome 派生，不接受调用方自填
        "written_at": datetime.now().isoformat(timespec="seconds"),
    })
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return doc


def describe(reading: dict) -> str:
    """一行可读的「为什么有效/为什么过期」。"""
    v, o = reading.get("validity"), reading.get("outcome")
    if v == "current":
        return f"current/{o}"
    return f"{v}/{o}：{reading.get('reason') or ''}"


# ================================================================
# 项目侧依赖键：gates / build / cycle / 各报告生产者共用同一套实现。
# 各算一套是这类系统最隐蔽的错误来源（同一份成品，两个脚本判出不同结论）。
# ================================================================
import sys

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def src_fingerprint(*rel_paths: str) -> dict:
    """判据源码指纹：判据改了，旧报告必须失效。

    为什么不能靠人维护 CHECKER_VERSION：那条路线要求"改判据的人记得升版本号"。
    2026-11-08 实测失效——改完 look.py 判据忘了升版本，cycle 直接 `reused`，
    终端照旧 6/6，**新判据一次都没执行**。这就是"改完自测全绿"的真相：
    复用键里只有产物摘要，没有判据身份。

    只回答"判据代码是哪一版"，与产物内容无关；键里带上它，判据一改就自动 stale。
    rel_paths 相对于 scripts/（例："look.py" / "delivery/gates.py"）。
    """
    h = hashlib.md5()
    got = {}
    for rel in rel_paths:
        p = _HERE.parent / rel
        try:
            blob = p.read_bytes()
            h.update(rel.encode("utf-8") + b"\0" + blob)
            got[rel] = hashlib.md5(blob).hexdigest()[:16]
        except OSError:
            # 文件不在：用固定串占位，别让"读不到"变成"不变"（否则删文件也能保住旧结论）
            h.update(rel.encode("utf-8") + b"\0MISSING")
            got[rel] = "MISSING"
    return {"fp": h.hexdigest()[:16], "files": got}


# 判据主体所在文件（键里只放指纹，不放内容）
CHECKER_SRC = {
    "presence": ("delivery/gates.py", "delivery/report_state.py"),
    "editability": ("delivery/editability.py", "delivery/report_state.py"),
    "assetcheck": ("assetcheck.py", "delivery/report_state.py"),
    "look": ("look.py", "delivery/spec_lint.py", "delivery/report_state.py"),
    # plan 阶段的判据主体是 spec_lint（②同层字号 / 溢出闭式解 / 文字压图 / 出界）。
    # 不加这一项时的实测（2026-11-09）：改完 spec_lint 判据后跑 cycle，plan 直接 `reused`
    # 旧报告、判 ready 并出片；同一条 spec 单跑 `gates.py --stage plan` 却报 fail。
    # 也就是"判据改了，新判据一次都没执行"——和 key_look 当年缺指纹是同一个坑。
    "plan": ("delivery/spec_lint.py", "delivery/gates.py", "delivery/report_state.py"),
    # build 的判据是「构建器本身」：build.py 决定怎么画，style.py/consts.py 决定画的尺寸与颜色。
    # 2026-11-10 前的实测缺口：构建器改了，spec 一字未动 → key_build 不变 → cycle 判 reused，
    # 盘上留着上一版构建器产出的 pptx，新代码一次都没跑（与 key_look 当年缺指纹同一个坑）。
    "build": ("build.py", "style.py", "consts.py", "specmerge.py"),
}


def key_look(spec_path, out_path, rendered_path=None) -> str:
    """look 报告的复用键：成品摘要 + 渲图摘要 + **判据源码指纹**。

    以前 look 阶段根本没有键（cycle 里直接比 look.json 的 png_md5/pptx_md5），
    于是"判据改了"这件事在复用判断里不存在。
    """
    return dependency_key("look", {"pptx_md5": file_digest(out_path),
                                   "rendered_md5": file_digest(rendered_path),
                                   "checks": src_fingerprint(*CHECKER_SRC["look"])})


def merged_view(spec_path):
    import specmerge
    return specmerge.load_merged(spec_path, quiet=True)


def _resolve_src(spec_dir: Path, src: str | None) -> Path | None:
    if not src:
        return None
    p = Path(src)
    if p.is_absolute():
        return p if p.exists() else None
    for base in (spec_dir, spec_dir.parent):
        c = base / src
        if c.exists():
            return c
    return None


def _production(spec_path) -> dict:
    view = merged_view(spec_path)
    return {sec: view.get(sec) or [] for sec in ("containers", "assets", "elements")} | {
        "background": view.get("background") or {},
        "canvas": view.get("canvas") or {},
    }


def key_plan(spec_path, inv_doc: dict | None) -> str:
    import inventory
    deps = inventory.plan_deps(spec_path, inv_doc or {"items": []})
    # 判据身份必须进键：plan 的结论来自 spec_lint，判据一改旧报告就得 stale。
    # 不写这行的话，"改判据 → cycle reused → 判 ready" 会静默发生（2026-11-09 实测）。
    deps["checks"] = src_fingerprint(*CHECKER_SRC["plan"])
    return dependency_key("plan", deps)


def assets_payload(spec_path) -> dict:
    """assets 检查的依赖：源图内容、素材内容、素材参数、邻接元素几何。

    刻意**不含**文字颜色/字号：只改配色不该让素材检查失效（旧实现按整份 override
    原始字节算指纹，改一个颜色就重抠全部素材）。文字 bbox 参与，因为它影响
    assetcheck 的"别人地盘"排除判据——几何动了就必须重查。
    """
    sd = Path(spec_path).resolve().parent
    view = merged_view(spec_path)
    assets = []
    for el in view.get("assets") or []:
        params = {k: v for k, v in el.items()
                  if k not in ("id", "bbox", "src", "review", "name")}
        assets.append({"id": el.get("id"), "bbox": el.get("bbox"),
                       "params": params, "src_md5": file_digest(_resolve_src(sd, el.get("src")) or "")})
    neighbours = [{"id": e.get("id"), "bbox": e.get("bbox")}
                  for e in (view.get("containers") or []) + (view.get("elements") or [])
                  if e.get("id") and e.get("bbox")]
    # 2026-11-09：这里原来还有 "reference_md5"。删掉的理由不是"少算一次哈希"，
    # 而是它把"本报告依赖参考图"这句话写进了**复用键**——改一张参考图就会让素材报告
    # 失效重跑，代码里也继续暗示资产闸该看参考图。资产闸只判素材自身。
    return {"assets": assets,
            "neighbours": neighbours}


def key_assets(spec_path, config: dict | None = None) -> str:
    # 检查配置进键：改阈值只让素材报告失效，不动别的报告。
    # checks：判据源码指纹（assetcheck.py 的 cover/baked/UNCOVERED 判据改了，旧报告必须失效）。
    deps = dict(assets_payload(spec_path))
    deps["checks"] = src_fingerprint(*CHECKER_SRC["assetcheck"])
    return dependency_key("assets", deps,
                          config if config is not None else ASSETCHECK_DEFAULTS)


def key_build(spec_path, out_path, width: int, height: int) -> str:
    sd = Path(spec_path).resolve().parent
    view = merged_view(spec_path)
    asset_files = []
    for el in view.get("assets") or []:
        asset_files.append({"id": el.get("id"),
                            "src_md5": file_digest(_resolve_src(sd, el.get("src")) or "")})
    return dependency_key("build", {"production": _production(spec_path),
                                    "asset_files": asset_files,
                                    "out_size": [int(width), int(height)],
                                    # 构建器身份：builder 一改，旧 build 报告自动 stale
                                    "builder": src_fingerprint(*CHECKER_SRC["build"])})


def page_inputs_digest(page_dir, spec_path) -> str:
    """本页「当前有效输入」的摘要——正式合并用它判"这份结论属于这版输入吗"。

    为什么需要（2026-11-10）：`verdict.json` 只说"这页 ready"，那是**当时那版输入**
    的结论。改完源图 / 改了 spec 颜色 / 换了项目规范（visual.json）之后直接合并，
    旧结论会被原样搬进整套交付——翻页时才看出来某页和别的页不是一个版。
    所以 cycle 收尾时把这份摘要写进 cycle.json，deck 合并前重算一遍比对。

    纳入：参考图内容、最终 spec+override 合并视图（含文字、坐标、样式、配色）、
          素材文件内容、画布、本页用到的项目规范（visual.json / deck.json）。
    不纳入：时间戳、报告本身、inventory 的人工签字（不影响出稿）。
    """
    sd = Path(spec_path).resolve().parent
    view = merged_view(spec_path)
    assets = [{"id": el.get("id"),
               "src_md5": file_digest(_resolve_src(sd, el.get("src")) or "")}
              for el in view.get("assets") or []]
    norms = {}
    for name in ("visual.json", "deck.json"):
        for cand in (sd / name, sd.parent / name):
            if cand.exists():
                norms[f"{cand.parent.name}/{name}"] = file_digest(cand)
                break
    return digest({"ref": file_digest(sd / "reference.png"),
                   "spec": view, "assets": assets,
                   "canvas": view.get("canvas"),
                   "norms": norms})


def key_presence(spec_path, out_path) -> str:
    view = merged_view(spec_path)
    want = [{"id": e.get("id"), "text": e.get("text")}
            for sec in ("containers", "assets", "elements") for e in view.get(sec) or []]
    # checks：判据源码指纹——presence 的判据在 gates.check_presence，改它旧报告必须失效。
    return dependency_key("presence", {"pptx_md5": file_digest(out_path), "want": want,
                                       "checks": src_fingerprint(*CHECKER_SRC["presence"])})


def _pt(v):
    return None if v is None else round(float(v), 2)


def text_fingerprint(out_path) -> dict:
    """成品里「文字清单」的语义摘要：只回答“还是不是真文本”，不管排版微调。

    为什么不用 pptx 字节 md5（2026-11-07 实测）：
      改一个字距、加一个阴影、把形状改个名，pptx 字节就变了 → editability 重跑
      （它要额外渲两次，约 6 秒），而“每块字都是真文本”这个结论一点没变。
      一次交付里这种微调常有 5~6 次，光重跑就够再做一页。
    依赖换成文字清单本身后，仍然会失效的（也正是它该抓的）：
      文字被删 / 被换成图片（runs 消失）、字号/字重/字体被改、文本框数量变化。
    故意不纳入：颜色、字距、形状名、阴影、渐变——这些改了也还是文本。

    只读 pptx、不读 spec：本检查回答“成品里的字是不是文本”，
    与“制作数据是否变了”解耦（后者由 plan/build 负责）。
    """
    from pptx import Presentation   # 延迟导入：本模块被 gates/cycle 大量 import
    prs = Presentation(str(out_path))
    items = []
    for sl in prs.slides:
        for sh in sl.shapes:
            if not (sh.has_text_frame and sh.text_frame.text.strip()):
                continue
            runs = []
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    f = r.font
                    runs.append([r.text, _pt(f.size), bool(f.bold), f.name])
            items.append(runs)
    return {"n_text": len(items), "runs": items}


def pics_fingerprint(out_path) -> dict:
    """成品里「位图清单」的语义摘要：几张、多大、画的哪张图。

    为什么文字指纹不够（2026-11-10 实测缺口）：editability 判的是"有没有整页贴图"，
    而它的旧键只有 text_fp。往页面上**加一张覆盖半页的位图**时一个文字 run 都没变 →
    报告判 current、结论照旧"不是贴图"，可成品已经不是原来的东西了。
    这里只认位图的存在与位置，不认颜色/字距那些不影响"是不是真文本"的微调。
    """
    from pptx import Presentation
    prs = Presentation(str(out_path))
    items = []
    for i, sl in enumerate(prs.slides):
        for sh in sl.shapes:
            img = getattr(sh, "image", None)
            if img is None:
                continue
            blob = getattr(img, "blob", b"") or b""
            items.append([i + 1,
                          int(round(float(sh.left or 0))), int(round(float(sh.top or 0))),
                          int(round(float(sh.width or 0))), int(round(float(sh.height or 0))),
                          hashlib.md5(blob).hexdigest()[:16]])
    return {"n_pics": len(items), "pics": items}


def key_editability(spec_path, out_path, width: int, height: int) -> str:
    view = merged_view(spec_path)          # 它内部已把 scripts/ 注入 sys.path
    # 判据与 spec_lint / verify 同源：唯一实现是 consts.drawn_as_text（= elements 段全集）。
    # 以前这里用 is_text_el，于是 type=table 的块不进依赖键 → 它的文字改了，editability
    # 报告仍判 current，复用一个看不见新文字的旧结论（P2-2）。
    from consts import drawn_as_text
    els = [{"id": e.get("id"), "bbox": e.get("bbox"), "text": e.get("text")}
           for e in view.get("elements") or [] if drawn_as_text(e)]
    return dependency_key("editability", {"text_fp": text_fingerprint(out_path),
                                          "pics_fp": pics_fingerprint(out_path),
                                          "elements": els, "render_size": [int(width), int(height)],
                                          "checks": src_fingerprint(*CHECKER_SRC["editability"])})


def asset_gen_key(spec_path, el: dict) -> str:
    """一块素材"是否仍由当前输入生成"的键。

    记录的是**当前参考图内容** + 裁框 + 掩膜参数 + 生成器身份；与文字颜色无关。
    摘要变了，cycle 重生成对应 PNG；只改文字配色时它不变，不该重抠。

    2026-11-10 把参考图摘要加回来（用户决定）：前一版（11-09）只算素材自身参数，
    于是"同一页换了截图的颜色/图形、裁框和 mask 一字未改"时键不变 → 旧 PNG 被当成
    "由当前截图生成"，产物还是上一张图的颜色。素材参数回答的是"抠哪里"，
    参考图回答的是"从哪张图抠"——两件事缺一个都不叫"由当前输入生成"。
    （素材**质量检查**的依赖仍不含参考图，见 assets_payload：资产闸只判素材自身。）
    """
    sd = Path(spec_path).resolve().parent
    params = {k: v for k, v in el.items() if k not in ("src", "id", "name")}
    gen = "cutout" if str(el.get("mask") or "cutout") == "cutout" else "fastcut"
    return dependency_key("asset-gen", {
        "reference_md5": file_digest(sd / "reference.png"),
        "bbox": el.get("bbox"), "params": params, "generator": gen,
    })
