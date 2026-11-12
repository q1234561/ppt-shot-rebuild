# -*- coding: utf-8 -*-
"""spec_lint.py — spec 层合法性检查（不渲染、不看图）。

为什么判定要放在这一层（实测结论）：
  用渲染图判字号，六种掩膜都失败（连通域 / 投影 / 颜色 / 列分位 / 长直段 / 字高比）。
  原因是两个物理事实叠在一起：
    ① 参考图是 JPEG，笔画被打散，连通域高度中位数随模糊漂移；
    ② 装饰横线压在文本框里，只贡献宽度不贡献高度。
  所以「字号对不对」不能从渲染像素反推，只能在 spec 层约束：
    - 字号由 pitch（行/字距）反解，参考墨宽只作**弱参考**：墨宽比字号系统性偏窄 4~7%，
      拿它反解字号会把手改过的值一律判错（原 ⑤ 就死在这条前提上，2026-11-08 已删）
    - 同一层（同一排、同一角色）只允许一个字号（消灭"五档字号"这类肉眼可见缺陷）
    - 每个素材必须有明确的掩膜方式（禁止 NULL 绕过）

这三条都是零渲染、纯结构检查，且能抓到我 27 页真实犯过的错（nav 五项 17.7/18.2/18.3/18.5/18.8）。

用法：
  python _tools/spec_lint.py --spec spec.json [--tol 0.6]
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/，取 specmerge
import specmerge
import style                                                          # noqa: E402
from consts import (DPI_DEFAULT, BOX_PAD_RATIO, BOX_PAD_EM, pt_to_px,   # noqa: E402
                    NON_TEXT_KINDS, SHAPE_NAMES, parse_hex_color, parse_line_style)

# ALPHA/BETA（墨宽→字号的反解系数）随 ⑤ 一起删除：⑤ 的输入 ref_ink_w 全库无人写，
# 判据从未触发，常量成了孤儿（2026-11-08）。字号口径见 style.size_from_ink（extract 侧）。
ROLE_KEYS = (
    ("t_nav", "nav"), ("t_title", "title"), ("t_hdr", "hdr"),
    ("t_sub", "sub"), ("t_h1", "colhead"), ("t_h2", "colhead"), ("t_h3", "colhead"),
    ("t_h4", "colhead"), ("t_lab", "label"), ("t_b", "body"),
    ("t_p09", "pageno"), ("t_foot", "footer"),
)


def role_of(eid: str) -> str:
    for pre, role in ROLE_KEYS:
        if eid.startswith(pre):
            return role
    return "other"


HANG_PUNCT = "”？。、，”"       # 行尾可悬挂出框的句读标点（PowerPoint 允许 ~0.5em）
HANG_EM = 0.5


def box_of(el) -> list[float] | None:
    """元素的 [x0, y0, x1, y1]；形状不对返回 None。

    缺 bbox 由 ① 报「缺字段」，这里只负责不崩——②④⑤⑥ 都要读数值，
    与其在四处各写一遍 try，不如在这里说清楚"读不出就是 None"。
    """
    b = (el or {}).get("bbox")
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return None
    try:
        return [float(v) for v in b]
    except (TypeError, ValueError):
        return None


def size_of(el) -> float | None:
    try:
        return float((el or {})["size_pt"])
    except (KeyError, TypeError, ValueError):
        return None


def layer_groups(els) -> tuple[list[tuple[tuple[str, int], list[list[dict]]]], list[str]]:
    """同层分组 —— ②「同层字号唯一」与 溢出求解共用的**唯一实现**。

    同层 = ① role 前缀（认不出 ID 前缀则取 style 令牌）② 12px 纵向带 ③ 相邻簇
    （同带内按 x 排序，gap > 3.0 × 行高 就断簇）。返回
    `[(bucket_key, [cluster, …]), …]` 与「说不出属于哪一层」的元素 **id 列表**。

    **刻意保留两层结构**：两个用途吃的是不同一层 ——
      ② 比的是 bucket：同一个（role, 带）里所有「≥2 元素的簇」合起来算字号极差，
         这是既有行为，改了就换了判据；
      溢出降字号比的是 cluster：只有**真正同簇**的邻块才一起降。按 bucket 降会把
         一整排跨列的卡片全降到同一个字号，而按 style 令牌分组又会让真正同簇的
         邻块没降——两种都会让 ② 的极差判据继续 FAIL，于是白降一轮。
    """
    buckets: dict[tuple[str, int], list[dict]] = defaultdict(list)
    no_style: list[str] = []
    for e in els:
        box = box_of(e)
        size = size_of(e)
        if box is None or size is None:
            continue          # 缺 bbox / size_pt 已在 ① 报过，这里跳过免得二次崩
        x0, y0, x1, y1 = box
        role = role_of(str(e.get("id")))
        # 认不出 id 前缀的元素不能统统塞进 "other"：那会把 banner_sub(18.2) 和 colhead(23.7)
        # 判成"同层字号分裂"（实测误报）。有 style 令牌就按 style 分组。
        if role == "other" and e.get("style"):
            role = f"style:{e['style']}"
        elif role == "other" and not e.get("style"):
            # 没有 style 令牌 = 说不出它属于哪一层。**这类块不能静默跳过**：
            # 跳过检查会被读成"检查过、没问题"（本页实测：extract 输出的 t1..tN 全部命中这里，
            # 18 个文字元素一个都没参与②，而终端打印的是 PASS）。这里只收集 id，判定在 main。
            # 硬判成 "other" 一层会误伤（banner_sub 18.2 与 colhead 23.7 被凑成一层），
            # 所以正确的处置不是猜层级，而是要求 spec 把 role 说清楚。
            no_style.append(str(e.get("id")))
            continue
        band = int(round((y0 + y1) / 2 / 12))       # 12px 分带，容忍 ±6px
        buckets[(role, band)].append(e)

    out: list[tuple[tuple[str, int], list[list[dict]]]] = []
    for key, items in buckets.items():
        items = sorted(items, key=lambda i: float(i["bbox"][0]))
        clusters, cur = [], [items[0]]
        for it in items[1:]:
            prev = cur[-1]
            gap = float(it["bbox"][0]) - float(prev["bbox"][2])
            h = max(float(prev["bbox"][3]) - float(prev["bbox"][1]), 8.0)
            if gap > 3.0 * h:
                clusters.append(cur)
                cur = [it]
            else:
                cur.append(it)
        clusters.append(cur)
        out.append((key, clusters))
    return out, no_style


def overflow_of(el, dpi: float) -> dict | None:
    """溢出判据的唯一实现：该元素**最宽那一行**会不会换行；溢出时给出闭式解上限。

    判据对象是最宽的一行（不是整块求和，也不是逐行跳过）：build.add_text 按 "\\n"
    一段一行、每行各自折行，所以「整块求和」会把正常多行块判成溢出，「有 \\n 就跳过」
    又会把多行块整个漏掉（实测把 t_c3a 的 bbox 改成错值也照样 PASS）。

    返回 None = 不溢出；否则
      {line, need, have, adv, adv_eff, bound, size, box_w, weight}
    `bound` 是**同一个不等式对 size 的移项逆解**，不是第二个判据：
        need > BOX_PAD_RATIO×W + BOX_PAD_EM×size×pxpt
      → size > BOX_PAD_RATIO×W / (pxpt × (adv_eff − BOX_PAD_EM))
    其中 adv_eff = need/(pxpt×size)。两个边都对 size 线性（含标点悬挂的 0.5em 也是
    线性扣减），所以这个解是**精确解**而不是一次线性化。

    **这里的 1.02 不算进去**（实测依据，2026-11-08）：判 FAIL 用的 `have*1.02` 里那个
    1.02 是**墨宽测量的噪声容差**（2%），不是版式余量。把它一并移项（即
    `1.02×1.06×W/(pxpt×(adv_eff−1.02×0.20))`）得到的是"刚好卡在 FAIL 线上"的字号，
    比"不换行"的字号大 2%；而中文按字折行是**离散**的——大 2% 就一定挤掉一个字。
    实测（page06 的 ct4，框宽由 241 缩到 205）：doc 式 21.96→21.9pt **渲染成 2 行**，
    本式 21.51→21.5pt **1 行**。所以「不会换行」的上限必须取本式。
    """
    txt = str((el or {}).get("text", ""))
    box, size = box_of(el), size_of(el)
    if not txt or box is None or size is None:
        return None
    weight = style.resolve_weight(el.get("weight", el.get("bold", False)))
    pxpt = pt_to_px(1.0, dpi)
    x0, _y0, x1, _y1 = box
    have = (x1 - x0) * BOX_PAD_RATIO + BOX_PAD_EM * size * pxpt
    worst_line, worst_need, worst_adv = "", 0.0, 0.0
    for line in txt.split("\n"):
        adv = style.sum_adv(line, weight)
        need = adv * pxpt * size
        # 标点悬挂：行尾是句读标点时 PowerPoint 允许它出框，不扣会把正常块判成溢出
        # （实测 t_c2b 以 “”” 结尾，need 394.7px / 可用 384.5px，扣 0.5em 后 376.7px 才合理）。
        if line and line[-1] in HANG_PUNCT:
            need -= HANG_EM * size * pxpt
        if need > worst_need:
            worst_line, worst_need, worst_adv = line, need, adv
    if worst_adv <= 0:            # 空行 / 全空白：没有可溢出的字
        return None
    if worst_need <= have * 1.02:
        return None
    adv_eff = worst_need / (pxpt * size)
    denom = adv_eff - BOX_PAD_EM
    box_w = x1 - x0
    bound = (BOX_PAD_RATIO * box_w / (pxpt * denom)) if denom > 1e-6 else None
    return {"line": worst_line, "need": worst_need, "have": have, "adv": worst_adv,
            "adv_eff": adv_eff, "bound": bound, "size": size, "box_w": box_w,
            "weight": weight}


HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
BARE_KEYS = {"size_pt": "字号", "radius_px": "圆角"}


def bare_literals(view: dict) -> list[dict]:
    """数**裸值**：没走 token 的六位色值、字号、圆角。

    只收这三类，理由各不相同：
      - 颜色：整套 268 色是"每页自己调"的直接后果，token 化的主目标
      - 字号：71 个字号同理；且字号是排版决策，改动要能被一次看见
      - 圆角：卡片圆角是全局件的形状，散落各处就会出现"每张卡圆角都不一样"
    bbox 不在内：几何由 deckalign 管（P1b），塞进裸值只会让两个判据互相打岔。
    """
    out: list[dict] = []

    def is_color(v) -> bool:
        """与 build 同一把尺：`consts.parse_hex_color` 认的才算颜色。

        第一版用 `^#[0-9A-Fa-f]{6}$` 自己写正则，后果有两个：`#4F8`（3 位短写）与不带 `#` 的写法数不到，
        而 build 是认的——闸门与 build 不同源，就会"build 画得出来、闸门说没有裸值"。
        """
        if not isinstance(v, str) or specmerge._is_token(v):
            return False
        try:
            parse_hex_color(v)
            return True
        except ValueError:
            return False

    def walk(node, where, key_hint=None):
        if isinstance(node, dict):
            for k, v in node.items():
                if is_color(v):
                    out.append({"where": f"{where}.{k}", "kind": "颜色", "value": v})
                elif (k in BARE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool)
                      and where.rsplit(".", 1)[-1] != "glow"):   # glow.radius_px 是柔光半径，不是圆角
                    out.append({"where": f"{where}.{k}", "kind": BARE_KEYS[k], "value": v})
                else:
                    walk(v, f"{where}.{k}", k)
        elif isinstance(node, list):
            # **list 分支必须也判颜色**：`gradient: ["#FEE5E7", …]` 全是裸值，
            # 第一版在这里只递归不判定，于是 9 页共 201 处渐变色一处都没数到——
            # 46 页报"裸值 0"就是这个假象。
            for i, v in enumerate(node):
                if is_color(v):
                    out.append({"where": f"{where}[{i}]", "kind": "颜色", "value": v})
                else:
                    walk(v, f"{where}[{i}]", key_hint)

    for sec in specmerge.SECTIONS:
        for el in view.get(sec) or []:
            walk(el, f"{sec}({el.get('id', '?')})")
    for key in specmerge.SCALARS:
        if key in view:
            walk(view[key], key)
    return out


def _line_index(spec_text: str) -> dict:
    """值 → 行号（首个出现位置）。给人指路用，不参与判定。"""
    idx = {}
    for i, line in enumerate(spec_text.splitlines(), 1):
        for m in re.finditer(r'"(#[0-9A-Fa-f]{6})"|(:\s*(-?\d+(?:\.\d+)?))', line):
            val = m.group(1) or m.group(3)
            if val is not None:
                idx.setdefault(str(val), i)
    return idx


def bare_report(spec_path, allow: int) -> int:
    """`--bare` 的实现：读 raw，数裸值，超 allow 就 FAIL。

    raw 是刻意的（D11）：若读展开视图，token 早已被替换成字面值，这个闸门永远数到 0 ——
    "裸值 = 0" 就成了自证。
    """
    raw_text = Path(spec_path).read_text(encoding="utf-8")
    view = specmerge.load_merged(spec_path, quiet=True)      # expand=False 默认就是 raw
    lits = bare_literals(view)
    idx = _line_index(raw_text)

    by_kind: dict[str, int] = {}
    for it in lits:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1

    page = Path(spec_path).resolve().parent.name
    head = " / ".join(f"{k} {v}" for k, v in sorted(by_kind.items())) or "无"
    ok = len(lits) <= allow
    print(f"裸值 [{page}]：{len(lits)} 条（{head}），允许 ≤{allow} → {'PASS' if ok else 'FAIL'}")
    for it in sorted(lits, key=lambda x: (x["kind"], x["where"]))[:allow + 12]:
        line = idx.get(str(it["value"]))
        print(f"  L{line if line else '?':>5} {it['kind']}  {it['where']} = {it['value']}")
    if len(lits) > allow:
        print(f"\n超出 {len(lits) - allow} 条：把值提到 visual.json 并用 {{\"组.名\"}} 引用；"
              f"确需保留的少量例外，用 --literal-allow 显式放行（放行数本身就是账）")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--tol", type=float, default=0.6, help="同层字号允许极差（pt）")
    ap.add_argument("--bare", action="store_true",
                    help="裸值模式（P3）：只数没走 token 的色值/字号/圆角，读**raw** 视图")
    ap.add_argument("--literal-allow", type=int, default=8,
                    help="允许的裸值条数上限（超出即 FAIL）；需人批的少量例外走这个数")
    a = ap.parse_args()

    # 裸值模式读 **raw**（expand=False）——这是 D11 的正解：闸门必须看得见没被替换的原值。
    if a.bare:
        return bare_report(a.spec, a.literal_allow)

    spec = specmerge.load_merged(a.spec)   # 必须与 build 看到同一份合并视图（raw）

    # 数值检查（溢出 / 出界 / 文字压图 / 字数）需要**数字**，而 token 在 raw 里是字符串。
    # 第一版没这一步：`size_of()` 拿 `"{type.size.t_s}"` 抛 ValueError 返回 None，
    # 于是 measurable=[] —— 整个 46 页的这几项**一次都没跑，却打印 PASS**。静默跳过比判红更坏。
    # 所以：有 token 就再取一份展开视图专门喂数值检查；展不开就**大声说跳过**，不许假装验过。
    numeric = spec
    if specmerge._has_token(spec):
        try:
            numeric = specmerge.load_merged(a.spec, quiet=True, expand=True)
        except Exception as exc:
            print(f"  WARN token 未展开（{exc}）——溢出/出界/压图三项对 token 化的块**未检查**")

    # 显式豁免：装饰层压文字但被 z 序盖住，是合法设计。asset_guard 早就允许 covers_text，
    # 这里不认就会自相矛盾——同一条设计资产守说可以、spec_lint 说不行，最后只能缩框留瑕疵。
    # 两处必须用同一个条件：covers_text + inventory 里有 locked_reason。
    import json as _json
    from pathlib import Path as _Path
    locked = {}
    inv_p = _Path(a.spec).parent / "inventory.json"
    if inv_p.exists():
        try:
            for it in (_json.loads(inv_p.read_text(encoding="utf-8")).get("items") or []):
                if it.get("locked") and str(it.get("locked_reason") or "").strip():
                    locked[it["id"]] = it["locked_reason"]
        except Exception:
            locked = {}
    # elements 段 **无条件**就是 build 的文字段：build.add_text 对每个元素都读
    # `el["text"]` 并 add_textbox（不读 type）。所以这里不再按 type/text 过滤——
    # 过滤一次就有一类元素掉出检查范围（2026-11-08 第二轮 QA：`{"type":"table"}` 无 text
    # 时 spec_lint/gates 双双 rc=0，build 却 KeyError traceback）。
    els = list(numeric.get("elements") or [])
    all_els = els
    containers = [c for c in numeric.get("containers", []) if c.get("bbox")]
    # extract 的素材候选（review 且无 src）不参与检查：build 根本不画它们
    assets = [s for s in numeric.get("assets", []) if s.get("bbox") and not (s.get("review") and not s.get("src"))]
    canvas = spec.get("canvas") or {}
    problems: list[str] = []
    warns: list[str] = []

    # ① 必填字段
    #    bbox：所有元素（画什么都需要位置）
    #    type：必填且必须在白名单内——这是唯一能拦住 `"Text"` / `"txt"` / `"tabel"` 的地方
    #          （写错的取值会让人误以为"这类块不用检查"，实际 build 照样画）。
    #    text / size_pt / color：elements 段的**每个**元素都要（P2-1）——build 对它们
    #          无条件 `el["text"]` + 默认 size_pt/color，缺 text 会直接 traceback，
    #          缺 size_pt/color 会静默画成 18pt 黑字（看着像"做了"，其实没声明）。
    AL = {"text"} | set(NON_TEXT_KINDS)
    for e in all_els:
        raw_t = e.get("type")
        tn = str(raw_t).strip().lower() if raw_t not in (None, "") else ""
        if not tn:
            problems.append(f"缺字段 type: {e.get('id')}"
                            "（缺 type 会让分组/归属类判断认不出它是什么）")
        elif tn not in AL:
            problems.append(f"type 取值不认识：{e.get('id')} type={raw_t!r}"
                            f"（只认 {sorted(AL)}；写错的取值会让人以为这一块不用检查）")
        if e.get("bbox") in (None, "", [], {}):
            problems.append(f"缺字段 bbox: {e.get('id')}")
        if not str(e.get("text") or "").strip():
            problems.append(f"缺字段 text（空）: {e.get('id')}"
                            "—— elements 段的每个元素 build 都当文字画，缺 text 会 KeyError")
        for k in ("size_pt", "color"):
            if e.get(k) in (None, ""):
                problems.append(f"缺字段 {k}: {e.get('id')}")
        if e.get("weight") in (None, ""):
            warns.append(f"缺 weight（build 会取默认 regular）：{e.get('id')}")
        # elements 段的 `shape` **不生效**：build 按段位派发，这段一律 add_text。
        # 不判死是对的（不生效的东西不判死），但**必须出声**——静默失效的字段会让人
        # 以为控制了外形。要形状/表格请写进 containers，或用 type:"table"。
        if e.get("shape") is not None:
            warns.append(f"{e.get('id')}: elements 段的 shape={e.get('shape')!r} 不生效"
                         f"（该段一律按文字画）；形状/表格请写进 containers 或 type:'table'")

    # ①b containers 段的 shape 值白名单（2026-11-09）
    # 为什么必须有：build 对不认识的 shape **静默兜底 rounded_rect**（SKILL 自己写着
    # "一律兜底，不报错"）——箭头写成 `cheveron` 就变成一个圆角块，客户拉不动端点，
    # 而所有闸全绿。这里在入口拦住，且不复写一份列表（常量在 consts.SHAPE_NAMES，
    # spec_lint 不能 import build：那会把 pptx/PIL 拖进 lint 层）。
    for c in containers:
        shp = str(c.get("shape") or "").strip()
        if not shp:
            continue
        if shp == "table":
            problems.append(f"{c.get('id')}: shape='table' —— table 是 type 不是 shape；"
                            f"要画表格请写 type:'table'（否则会被兜底成圆角矩形）")
        elif shp not in SHAPE_NAMES:
            problems.append(f"{c.get('id')}: shape={shp!r} 不在白名单 {sorted(SHAPE_NAMES)}"
                            f"（build 会静默兜底 rounded_rect，见 SKILL §1）")

    # 后面几段要读数值：缺 bbox / size_pt 的元素已在 ① 报过，这里跳过免得二次崩，
    # 但**不允许**因为跳过而让整体看起来通过（① 的 problems 已经把它算进去了）。
    # 读取走 box_of / size_of（模块级唯一实现，溢出求解也用同一对）。
    measurable = [e for e in els if box_of(e) is not None and size_of(e) is not None]


    # ② 同层字号唯一 —— **已退役（2026-11-09）**。
    # 它比的是"字号反解值"，而反解值来自墨迹宽度：正文 6 块在参考图上墨高**全部 35px**
    # （字高相同 ⇒ 同字号），却被反解成 20.8 / 19.2 / 19.91，于是报出 1.6pt 的"分裂"。
    # 同一份数据换"字高/0.96/pxpt"推算是 20.9，与当前值一致；当前值还经过渲染校准
    # （墨迹 359 vs 参考 356）。结论：**值是对的，判据比错了量**。
    # 目标函数是"对参考图的还原度"，不是"同层一致"——源图同类元素本来就会因字距、
    # 字符集（中文墨高 > 拉丁）而不同，强行统一等于降低还原度。
    # 保留 layer_groups：④ 溢出求解按同一份分组降字号（cycle.solve_overflow）。
    # ③ 素材必须有掩膜方式
    for s in assets:
        if not s.get("mask") and not s.get("src"):
            problems.append(f"素材无掩膜且无 src：{s['id']}")
        # ③b 小素材禁止"有 src 但无 mask"：矩形裁片在浅底上必然露方边。
        # 实测事故：右侧三个图标用 matting 出图（α 均值 1.00），成品是 76x77 的不透明方块，
        # 在卡片上看得见一圈方框；改用 mask=circle 后消失。这条在这里提前拦。
        x0, y0, x1, y1 = s["bbox"]
        short = min(x1 - x0, y1 - y0)
        if s.get("src") and not s.get("mask") and short <= 140:
            problems.append(
                f"小素材未声明掩膜：{s['id']}（短边 {short:.0f}px）——"
                f"小图标/圆盘直接铺矩形裁片会露方边，须给 mask=circle 或 chroma")
        if s.get("mask") == "circle" and not s.get("circle"):
            problems.append(f"mask=circle 但缺 circle:[cx,cy,r]：{s['id']}")
        if s.get("mask") == "circle" and s.get("circle"):
            # 羽化余量：bbox 必须比 2r 每边多 ≥2px，否则 1px 羽化直接啃掉盘缘，
            # 成品边缘发虚、锐度掉 30%（2.13.1 实测：88px 盒 + r=44 → 锐度 102→71）。
            cx, cy, r = [float(v) for v in s["circle"]]
            bx0, by0, bx1, by1 = [float(v) for v in s["bbox"]]
            margin = min(cx - r - bx0, cy - r - by0, bx1 - cx - r, by1 - cy - r)
            if margin < 2:
                problems.append(
                    f"圆形素材没有羽化余量：{s['id']} bbox 比 2r 只多 {margin:.0f}px（需 ≥2）——"
                    f"edge 会被羽化啃掉，成品发虚。把 bbox 每边外扩 3px，r 不动")
        # 死判据已删：note / step_delta 这两个字段全库无人写，这条永远不触发。

    # ④ 溢出预判：统一字号前必须先算上限，否则整组一起换行。
    #    判据对象是**最宽的那一行**（不是整块文本，也不是各行求和）：
    #      build.add_text 按 "\n" 一段一行，每行各自折行 —— 所以「整块求和」会把正常多行块
    #      全判溢出（假绿的反面）；而旧版 `if "\n" in text: continue` 又把多行块整个跳过，
    #      实测把 t_c3a 的 bbox 改成错值也照样 PASS（这条闸等于没开）。
    #    字宽取 style.sum_adv（上一版硬编码的 ADV 表只有 regular 系数：bold 的 cap/digit
    #      是 0.752/0.617，差 9%/5%，P04 这种 bold 混排块会算偏）。
    #    可用宽 = bbox 宽 × BOX_PAD_RATIO + BOX_PAD_EM×字号，与 build 的 w_px 同源（consts）。
    #    判据与「上限」都由 overflow_of 给（模块级唯一实现）：它返回的 bound 是**同一个
    #    不等式对 size 的移项逆解**，所以这里印的上限与 溢出降字号用的值是同一个数——
    #    上一版印的是 `have/(adv×pxpt)`，把 have 当常数相除，漏了"字号变小内边距也跟着
    #    变小"，实测对 page06 的 t_c3a 偏保守 1.7%（报 14.2pt，真解 14.44pt）。
    dpi = float((spec.get("canvas") or {}).get("dpi") or DPI_DEFAULT)
    for e in measurable:          # 缺 bbox/size_pt 的元素已在 ① 报错，这里跳过免得崩
        over = overflow_of(e, dpi)
        if over is None:
            continue
        bound = "—" if over["bound"] is None else f"{over['bound']:.1f}pt"
        problems.append(
            f"溢出风险 {e['id']}：{over['line'][:10]} 需 {over['need']:.0f}px > "
            f"可用 {over['have']:.0f}px，该行会换行"
            f"（上限约 {bound}，weight={over['weight']}）")

    # ⑤ 已删（2026-11-08）：原「字号与墨宽不符」用 ALPHA/BETA 从墨宽反解字号，
    # 而墨宽比字号系统性偏窄 4~7%（SKILL §5 已写），凡手改过字号的元素必然误报，
    # 且它抓的东西与 ④ 溢出、溢出求解的 pitch 反解完全重叠。判据的前提本身不成立，留着只会逼人改对的值。

    # ⑥ 出界 / 文字压素材（原 spotcheck 的两条机器判据，移到这里：不需要渲染，spec 层就能判）
    cw = int(canvas.get("w_px", 0))
    ch = int(canvas.get("h_px", 0))
    if cw and ch:
        for el in measurable + containers + assets:   # 缺 bbox 的已在 ① 报过
            x0, y0, x1, y1 = box_of(el) or [float(v) for v in el["bbox"]]
            if x0 < 0 or y0 < 0 or x1 > cw or y1 > ch:
                problems.append(f"出界：{el['id']} {el['bbox']}（画布 {cw}x{ch}）")
        for t in measurable:
            for s in assets:
                ax0, ay0, ax1, ay1 = box_of(t)
                bx0, by0, bx1, by1 = [float(v) for v in s["bbox"]]
                if ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0:
                    continue
                # rectmask 的排除矩形里 α=0：那块像素根本不会盖住文字，
                # 所以判"压字"要按**有效覆盖**算，而不是两个矩形相交。
                # 只放行完全落在排除矩形内的相交部分；剩一点没盖住就仍然报。
                rest = (min(ax1, bx1) - max(ax0, bx0)) * (min(ay1, by1) - max(ay0, by0))
                for r in (s.get("mask_rects") or []) if s.get("mask") == "rectmask" else []:
                    rx0, ry0, rx1, ry1 = [float(v) for v in r]
                    ow = min(ax1, bx1, rx1) - max(ax0, bx0, rx0)
                    oh = min(ay1, by1, ry1) - max(ay0, by0, ry0)
                    if ow > 0 and oh > 0:
                        rest -= ow * oh
                if rest > 1.0:
                    if s.get("covers_text") and s["id"] in locked:
                        warns.append(f"文字压图已豁免：{t['id']} × {s['id']}"
                                     f"（{locked[s['id']][:24]}）")
                        continue
                    problems.append(f"文字压图：{t['id']} × {s['id']} —— 改 z 序或挪框，"
                                    f"或给 {s['id']} 加 covers_text:true 并在 inventory 里 locked+locked_reason"
                                    + ("（已扣排除矩形，仍重叠 %d px²）" % int(rest) if s.get('mask') == 'rectmask' else ""))

    # ⑤ line 里的键必须是实现认识的：拼错（width_px / line_width）不会报错，
    # 只会静默失效——width_pt 就是这么失效了整整几轮。
    # 取值清单（dash / head / tail 各自认哪些值）由 consts.parse_line_style 一处给出；
    # 这里只判"键在不在白名单"，判据逻辑未动。
    KNOWN_LINE = {"color", "alpha", "width", "width_pt", "dash", "head", "tail"}
    for el in els + containers + assets:
        ln = el.get("line")
        if isinstance(ln, dict):
            bad = [k for k in ln if k not in KNOWN_LINE]
            if bad:
                problems.append(f"{el['id']}: line 里有实现不认识的键 {bad}"
                                f"（只认 {sorted(KNOWN_LINE)}）")
            # 取值也要查，且必须与 build 用同一个校验器、同一句话：
            # 只查"键在不在"会留下一个窄缝——`dash:"wavy"` 在闸这里绿着、到 build 才红，
            # 正是"同一份 spec 两处结论不一致"。校验器与 build 共用 consts.parse_line_style。
            try:
                parse_line_style(ln, str(el.get("id")))
            except ValueError as exc:
                problems.append(str(exc))

    # ⑦ 颜色写法白名单：与 build 共用 consts.parse_hex_color（**同源**，两处结论必须一致）。
    #    以前两处各写一份 "0123456789ABCDEF"：`#4F8`（3 位短写）/ `#GGG`（非法字符）在 build
    #    里把半成品 pptx 崩掉（traceback），在这一层却静默放过——同一份 spec 两处结论不一致。
    #    3 位在这里按 CSS 短写展开成 6 位（#4F8 → #44FF88），build 用同一个函数得到同一个值。
    #    只判「写法认不认识」，**不做**任何饱和度/对比度判断（硬饱和反证：slider:arrow 的
    #    蓝箭头压蓝底，按饱和度调阈值会把纯净色模板判成硬饱和）。
    def _chk_color(eid, field, value):
        # token（{palette.ink}）在 raw 视图里是**合法值**：它不是色值，是引用。
        # 闸门读 raw 是为了看得见裸值，不是为了把引用判成非法。
        if specmerge._is_token(value):
            return
        try:
            parse_hex_color(value, field)
        except ValueError as exc:
            problems.append(f"{eid}: {exc}")

    for el in all_els + containers + assets:
        if el.get("color") is not None:
            _chk_color(el.get("id"), "color", el["color"])
        if isinstance(el.get("fill"), str) and el["fill"].strip().lower() != "none":
            _chk_color(el.get("id"), "fill", el["fill"])
        for _i, _c in enumerate(el.get("gradient") or []):
            _chk_color(el.get("id"), f"gradient[{_i}]", _c)
        _ln = el.get("line")
        if isinstance(_ln, dict) and _ln.get("color") is not None:
            _chk_color(el.get("id"), "line.color", _ln["color"])
    # 背景色不在这一层检查：build 侧本来就有它自己的人话校验（background.color）。

    # 自定义 name **不再需要告警**：presence / editability / look 三处认元素都已改走
    # consts.shape_id（cNvPr@title 优先，否则 name），build 也把 id 写进了 cNvPr@title。
    # 上一版这里有一条「name ≠ id 会让那两道闸认不出」的 WARN——那是对旧口径的描述，
    # 留在库里就是假警报，删掉。

    for w in warns:
        print("  WARN " + w)
    if problems:
        for p in problems:
            print("  FAIL " + p)
        print(f"\n{len(problems)} 项问题")
        return 1
    print(f"  PASS 无缺字段 / 素材掩膜齐全 / 无溢出 / 无出界"
          f"（{len(warns)} 条 WARN；同层字号一致性已退役，不进本检查）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
