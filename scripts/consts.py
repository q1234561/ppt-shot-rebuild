#!/usr/bin/env python3
"""consts.py — px/pt 换算与行高常量的唯一真值。

任何脚本需要「字号 → 行距 / 框高」的比例，都必须 import 这里，
不要在各自文件里再写一遍数字。

LINE_EM 的含义
--------------
PowerPoint 的「多倍行距 n」不是 n×字号，而是 n×**字体自然行高**。
微软雅黑 bold/regular 的自然行高实测 ≈1.20em（见下方实测记录）。
所以：给定目标行距 pitch_px，要写进 pptx 的倍数 = pitch_px / (字号px × LINE_EM)。

实测记录（2026-11-06，本机 PowerPoint，微软雅黑，125.4dpi 定稿三块）
  c1sub  size 19.7pt=34.31px  spacing 1.181 → pitch 48px → 反解 1.185
  c2sub  size 18.7pt=32.57px  spacing 1.224 → pitch 48px → 反解 1.204
  c3     size 26.1pt=45.46px  spacing 1.093 → pitch 59.5px → 反解 1.198
中值 1.20，散差 ±0.01（像素测量噪声量级）。
旧值 1.32 是错的，会让行距紧约 10%。
"""
from __future__ import annotations

# 字体自然行高 / 字号。换字体或换渲染器（WPS / LibreOffice）必须重标，否则必偏。
LINE_EM = 1.20

# 当前标定所依赖的环境。verify/autocorrect 用它判断"常量还成立吗"。
CALIB = {
    "font": "Microsoft YaHei",
    "renderer": "PowerPoint (Windows COM Export)",
    "dpi_basis": 125.4,
    "measured_on": "2026-11-06",
}

def px_to_pt(px: float, dpi: float) -> float:
    return px * 72.0 / dpi


def pt_to_px(pt: float, dpi: float) -> float:
    return pt * dpi / 72.0


def spacing_for_pitch(pitch_px: float, size_pt: float, dpi: float) -> float:
    """目标行距(px) → 写进 pptx 的行距倍数。"""
    return pitch_px / max(pt_to_px(size_pt, dpi) * LINE_EM, 1e-6)

# ---------------------------------------------------------------- 共用判据（唯一真值）
# 这些数字以前散在 4~6 个文件里各写一遍，同一个问题在不同脚本得到不同结论
# （2026-11-07 审计）。现在只在这里定义。

import numpy as _np

LUMA = _np.array([0.299, 0.587, 0.114], dtype=_np.float32)   # 灰度口径（= 原包 compare_reference）
INK_LUMA_MAX = 150          # 不知道文字色时：灰度 < 150 视为墨迹（深字压浅底）
INK_DIST_MIN = 55           # 不知道文字色时：与局部底色距离 > 55 视为墨迹（浅字压深底）
INK_MARGIN = 25             # 知道文字色时："比底色更接近文字色"至少要领先这么多
GRAD_MIN_DELTA = 12.0       # 判"是渐变"的最小端点色差（三处旧值 3 / 10 / 12，取最严）
BOX_PAD_RATIO = 1.06        # 文字框宽 = bbox 宽 × 此值 + BOX_PAD_EM×字号（build 与 spec_lint 共用）
BOX_PAD_EM = 0.20
CANVAS_W, CANVAS_H = 1672, 941   # 只作缺省；真实画布必须读 spec.canvas
DPI_DEFAULT = 125.4

# elements 段里「会不会被画成文字」的**唯一判据**（本库只有一条，不要再造第二条）。
#
# 判据 = **段位**：build.py 按 section 分派，`elements` 段一律走 add_text——它不读 type，
# 并且无条件读 `el["text"]`。所以在 elements 段里「会不会画成文字」恒为真。
# 历史上这条判据被写成"看 type"（白名单）和"看 text 有没有"（排除法），两次都与实现分叉：
#   2026-11-08 第一轮：把 17 个文字元素的 type 全删掉 → spec_lint 打印「PASS 无缺字段」、
#     verify 0 行「PASS 全过」（白名单把"漏写字段"变成全链路静默放行）。
#   2026-11-08 第二轮（P2-1）：`{"type":"table","bbox":[…]}`（**没有 text**）被判成
#     "不是文字" → spec_lint 不查 text、verify 跳过它；而 build 走到 `txt = el["text"]`
#     直接 KeyError traceback，spec_lint / gates plan 却都 rc=0。
# 结论：判据只能看段位，不能看元素的任何字段（否则永远有一类元素落在判据外）。
#
# 由它派生、必须同源的五处：spec_lint 必填字段、editability（两处）、
# report_state（editability 的依赖键）、deck_lint 跨页字号。
NON_TEXT_KINDS = frozenset({"shape", "image", "table", "line", "chart", "picture"})

# containers 段允许的 `shape` 取值。**不含 `table`**——`table` 是 `type` 的取值
# （build.py 里 `if type=="table": return add_table`），把它写进 shape 会被静默兜底成圆角矩形。
# build.SHAPES 是同一份事实的另一个副本，由用例钉住两边相等（spec_lint 不能 import build：
# 那会把 pptx/PIL 拖进 lint 层）。
SHAPE_NAMES = frozenset({
    "rect", "rounded_rect", "ellipse", "chevron", "right_arrow", "left_arrow", "diamond",
    "freeform", "line",
})


def is_picture(sh) -> bool:
    """「这个形状是不是图片」——**全库唯一判据**。

    历史教训（2026-11-09 实测）：同一份产物，两处给出不同答案——
      look.py       : `shape_type == 13 or 类名 == "Picture"`
      editability.py: `shape_type == MSO_SHAPE_TYPE.PICTURE`
    这正是本库文档里反复写的"最大的事故来源"（同一份度量两份实现），
    而且已经发生过一次。现在两边都调这里。

    判定取"枚举 or 类名"两种线索的**并集**：枚举可靠但按 python-pptx 版本可能缺项，
    类名是兜底；两者任一命中即算图片，宁可多认不可漏认——漏认的后果是
    "文字被贴图化"这条闸看不见（客户拿到改不动的字）。

    不 import pptx：consts 被 lint 层 import，拖进 pptx 会让 lint 变重。
    魔数 13 = MSO_SHAPE_TYPE.PICTURE，只在这里出现。
    """
    if getattr(sh, "shape_type", None) == 13:
        return True
    return sh.__class__.__name__ == "Picture"


def is_text_el(el) -> bool:
    """**已降级**：只回答"它自报属于哪一类内容"（分组 / 归属用），**不用于**判断画不画文字。

    保留它是因为 assetcheck 的"地盘"划分需要"内容分类"这个语义；
    但凡涉及「build 会不会画成文字 / 要不要必填字段 / 要不要检查」，一律用 drawn_as_text。
    仍有调用方按 type 排除非文字块，所以这里不改语义，只改注释——改语义会动分组结果。
    """
    t = (el or {}).get("type")
    return str(t if t is not None else "text").strip().lower() not in NON_TEXT_KINDS


def drawn_as_text(el) -> bool:
    """build 会不会把它画成文字框 —— 判据是**段位**（elements 段的元素恒为 True）。

    build.add_text 对 elements 段的每个元素都无条件读 `el["text"]` 并 add_textbox，
    所以这个函数对 elements 段的任何元素都返回 True，即使它写着 type=table/image
    或者根本没有 text（那种情况要报"缺 text"，不是"跳过它"）。
    调用方必须只传 elements 段的元素；别的段（containers / assets）不走 add_text。
    """
    return isinstance(el, dict)


# ---------------------------------------------------------------- 颜色解析（唯一实现）
# build 与 delivery/spec_lint 必须共用下面两个函数，**不许各写一份** "0123456789ABCDEF"：
# 各写一份的后果是同一个 `#4F8` 在一边把半成品 pptx 崩掉、在另一边被静默放过
# （2026-11-08 P2c：颜色解析会崩，不是报错）。
# 白名单是大小写都认的十六进制位；3 位是 CSS 短写，先展开成 6 位再解析。
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def parse_hex_color(value, what: str = "color") -> str:
    """'#RRGGBB' / '#RGB' → 'RRGGBB'（6 位大写）；认不出抛 ValueError，带人话。

    3 位展开规则：每个十六进制位**重复一次**（#4F8 → #44FF88，不是 #44F888）——
    与 CSS / DrawingML 的短写语义一致，否则 R/G/B 三通道会被错位取到。
    """
    raw = str("" if value is None else value).strip()
    h = raw[1:] if raw.startswith("#") else raw
    if len(h) == 3 and all(c in HEX_DIGITS for c in h):
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or any(c not in HEX_DIGITS for c in h):
        raise ValueError(f"颜色写法不认识：{raw or '(空)'}"
                         f"（要 #RRGGBB 或 #RGB；位置：{what}）")
    return h.upper()


# ---------------------------------------------------------------- 线型 / 箭头（唯一实现）
# spec 的 `line` 段里，除宽度与颜色外还有三样：虚实、起点箭头、终点箭头。
#
# 取值直接采用 OOXML `a:prstDash@val` 与 `a:headEnd@type` 的**原值**，不另造一套中文/蛇形词：
# 这样"写进 spec 的值"与"从成品读回的 val"逐字相同，验收可以断言相等，而不是"看着像虚线"。
# 宁可让作者查一眼清单，也不要为了好看造一层别名——别名一多，"写 dot 得到 sysDot"这种
# 不可断言的映射就会变成又一处两套说法。
LINE_DASH = ("solid", "dash", "dashDot", "lgDash", "lgDashDot", "lgDashDotDot",
             "sysDot", "sysDash")
LINE_HEAD = ("none", "triangle", "stealth", "oval", "diamond", "arrow")
LINE_HEAD_SIZE = ("sm", "med", "lg")


def parse_line_style(cfg: dict, eid: str = "line") -> dict:
    """校验并规范化 `line` 段的 `dash` / `head` / `tail`。不合法抛 ValueError（人话）。

    **build 与 spec_lint 共用这一处**（与 parse_hex_color 同一条规矩：同一份 spec 在两处
    必须得到同一个结论）。静默忽略是最坏的失败方式——写错的线型会退化成默认实线，
    而画面上"虚线变实线"没人会归因到拼写。
    """
    out: dict = {"dash": None, "head": None, "tail": None}
    dash = cfg.get("dash")
    if dash not in (None, ""):
        key = str(dash).strip()
        if key not in LINE_DASH:
            raise ValueError(f"{eid}: line.dash={dash!r} 不认识（只认 {list(LINE_DASH)}；"
                             f"虚线写 'dash'，点线写 'sysDot'，长划线写 'lgDash'）")
        out["dash"] = key
    for side, label in (("head", "起点"), ("tail", "终点")):
        raw = cfg.get(side)
        if raw in (None, ""):
            continue
        if isinstance(raw, dict):
            kind = str(raw.get("type", "none")).strip()
            wide = str(raw.get("w", "med")).strip()
            leng = str(raw.get("len", "med")).strip()
        else:
            kind, wide, leng = str(raw).strip(), "med", "med"
        if kind not in LINE_HEAD:
            raise ValueError(f"{eid}: line.{side}.type={kind!r} 不认识"
                             f"（只认 {list(LINE_HEAD)}；箭头一般写 'triangle'）")
        if wide not in LINE_HEAD_SIZE or leng not in LINE_HEAD_SIZE:
            raise ValueError(f"{eid}: line.{side} 的 w/len={wide!r}/{leng!r} 不认识"
                             f"（只认 {list(LINE_HEAD_SIZE)}）")
        out[side] = {"type": kind, "w": wide, "len": leng}
    return out


def shape_id(sh) -> str:
    """成品 pptx 里「这个形状是谁」的**唯一取值处**：`cNvPr@title` 优先，否则 `name`。

    为什么需要它：A（2026-11-08 P2c）让 spec 的 `name` 真正写进 `cNvPr@name`，于是
    `name` 不再恒等于 spec 的 id。所有「按名字认元素」的检查器（presence 的集合比对、
    editability 的按名建索引、look 的同名文字帧）都必须走这一处，否则改一处漏一处。

    为什么用 `title` 而不是 `descr`：python-pptx 的 add_picture **会把图片文件名写进
    descr**（实测 id=i1 的图 descr="i1.png"）——拿 descr 当 id 会让每一张图都被认成
    "i1.png"，presence 立刻报缺 i1。`title` 没有这个占用，且只在 build 真的写了自定义
    name 时才存在，所以历史成品（无 title）取值与以前逐字相同。
    """
    try:
        found = sh._element.xpath(".//p:cNvPr")
        if found:
            return found[0].get("title") or sh.name
    except Exception:               # 非 DrawingML 元素（不该出现）：退回 name，不崩
        pass
    return sh.name


def ink_mask(rgb, text_color=None):
    """墨迹掩膜：全流程唯一实现。

    知道文字色 → "比底色更接近文字色，且领先 INK_MARGIN"（低对比也能分）；
    不知道 → 深字压浅底用灰度、浅字压深底用距离，取并集。

    2026-11-07 修：以前调用方把"是否白字"当成布尔值传进来，真实色值被忽略，
    白字压蓝底时整条蓝带都被判成墨迹（t_nav3 量出 #055BCE、suspect=False）。
    现在必须传真实色值：有文字色就一律按"离谁更近"分，浅色字不再走暗色取墨。
    """
    roi = _np.asarray(rgb).astype(_np.int16)
    border = _np.concatenate([roi[0], roi[-1], roi[:, 0], roi[:, -1]])
    bg = _np.median(border, axis=0)
    if isinstance(text_color, str) and text_color.strip():
        h = text_color.strip().lstrip("#")
        if len(h) == 6:
            fg = _np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=_np.int16)
            if _np.abs(fg - bg).max() < 24:
                g = roi.mean(axis=2)
                return _np.abs(g - _np.median(g)) > INK_DIST_MIN
            d_fg = _np.linalg.norm(roi - fg, axis=2)
            d_bg = _np.linalg.norm(roi - bg, axis=2)
            return (d_fg < d_bg - INK_MARGIN) & (d_fg < 110)
    g = roi.astype(_np.float32) @ LUMA
    far = _np.linalg.norm(roi - bg, axis=2) > INK_DIST_MIN
    return (g < INK_LUMA_MAX) | far


# ---------------------------------------------------------------- 剖面平滑（唯一实现）
# 本模块本就依赖 numpy（`import numpy as _np`，供 LUMA / ink_mask 用）；
# 下面两个内核沿用同一别名与同一依赖，不新增 import。
# 原来只在 delivery/probe.py 里叫 _smooth，measure.py 反过来 `from probe import _smooth`
# 借用了它——一份 3 行卷积逻辑被两个方向相反的模块共用。原样搬到 consts，probe 与 measure
# 各自从同一处取用；第 4 步 measure.py 退役后，这里就是唯一真值。
def smooth_profile(a: _np.ndarray, k: int = 3) -> _np.ndarray:
    """一维剖面的 k 点均值平滑（_np.repeat 端部延拓），供 probe 与 measure 共用一份实现。"""
    if k <= 1 or len(a) < k:
        return a
    pad = k // 2
    padded = _np.concatenate([_np.repeat(a[:1], pad, 0), a, _np.repeat(a[-1:], pad, 0)])
    return _np.stack([_np.convolve(padded[:, i], _np.ones(k) / k, "valid") for i in range(a.shape[1])], 1)

