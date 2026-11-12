#!/usr/bin/env python3
"""style.py — 文字样式的唯一真值：字体、粗细、字号、字距、墨迹判据。

一个决策一个量，顺序不可逆：
  粗细 ← 墨量（墨迹/墨迹框面积，与字号无关）
  字号 ← 墨迹宽（用已定粗细的字体反解）
  字距 ← 剩余宽差（见 autocorrect），不碰字号

字体固定微软雅黑，三档：light / regular / bold。
extract（求解）、build（渲染）、verify（验收）都只从这里取，不要在别处另写字体常量。
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import consts

# 唯一字体来源：extract 求解、build 渲染都从这里取
FONT_FAMILY = "微软雅黑"
FONT_FILES = {
    "light": r"C:\Windows\Fonts\msyhl.ttc",
    "regular": r"C:\Windows\Fonts\msyh.ttc",
    "bold": r"C:\Windows\Fonts\msyhbd.ttc",
}
# 写进 pptx 的 typeface：bold 用同名 + bold=True（PowerPoint 会取 msyhbd）
FONT_TYPEFACE = {"light": "微软雅黑 Light", "regular": "微软雅黑", "bold": "微软雅黑"}

WEIGHTS = ("light", "regular", "bold")

# ── 逐字前进宽（2.6.0）：字号由墨迹宽解析反解，不再用 PIL 二分拟合。
# 旧 _fit_px 只能给整数 px，量化误差 0.57pt（实测 8 块中位偏 0.40pt）；
# 解析式连续，同样 8 块中位偏 0.14pt。按"每字 1em"算会让 (病因预防) 偏 29%，
# 因为 ASCII 括号只有 0.334em、数字 0.586em、大写拉丁 0.690em（bold 各不同）。
ADV = {
    "regular": {"cjk": 1.000, "digit": 0.586, "cap": 0.690, "paren": 0.334},
    "bold":    {"cjk": 1.000, "digit": 0.617, "cap": 0.752, "paren": 0.390},
    "light":   {"cjk": 1.000, "digit": 0.580, "cap": 0.684, "paren": 0.330},
}
BEARING_EM = 0.114          # 首末字左右旁白


def _char_class(ch: str) -> str:
    if ord(ch) > 0x2E80 or ch in "，。、：；？！（）【】《》""''—…":
        return "cjk"
    if ch.isdigit() or ch in "+-.":
        return "digit"
    if ch in "()":
        return "paren"
    if ch.isascii() and ch.isalpha():
        return "cap"
    return "cjk"


def sum_adv(line: str, weight: str) -> float:
    t = ADV.get(weight, ADV["regular"])
    return sum(t[_char_class(c)] for c in line if c.strip())


def size_from_ink(ink_w: float, line: str, weight: str, dpi: float) -> float:
    """墨迹宽 → 字号（pt）。px_per_pt = dpi/72；墨迹宽 = px_per_pt × (Σadv − 旁白) × pt。"""
    adv = sum_adv(line, weight)
    if adv <= BEARING_EM or ink_w <= 0:
        return 0.0
    return ink_w / ((dpi / 72.0) * (adv - BEARING_EM))


# 字距上限：±0.06em。超过说明不是字距问题（字号或断行错了），不要用字距硬凑
SPACING_MAX_EM = 0.06
# 残余宽差小于这个比例就不动字距（1.5% 在 300px 的串上是 4.5px，肉眼几乎不动）
SPACING_MIN_REL = 0.015


def resolve_weight(v) -> str:
    """weight 归一：接受 light/regular/bold 或布尔 bold，其余回落到 regular。"""
    if isinstance(v, bool):
        return "bold" if v else "regular"
    s = str(v or "regular").lower()
    return s if s in FONT_FILES else "regular"


def ink_mask(rgb: np.ndarray, text_color: str) -> np.ndarray:
    """墨迹掩膜：**转调** consts.ink_mask，不再保留第二份实现。

    合并前这里是一份独立实现，判据为
        (d_fg < d_bg - 25) & (d_fg < 110)
    即"比底色更接近文字色、且距底色至少 25、距文字色不到 110"。
    consts 版走的正是同一条判据，另加一个"文字色与底色几乎同色 → 退回灰度反差"的
    回退分支。本页 6 个真实文字块（深蓝标题 / 白字胶囊 / 深灰正文 / 卡片段落 /
    白字小字 / 页脚）上一次都没触发该分支——它们的文字色与底色相距很远，
    不存在"同色"的可能。复现方式：照 `consts.ink_mask` 里算 `bg` 的那两行
    （四边拼起来取 `np.median`）重算一遍，看 `|fg-bg|` 是否远大于"同色"阈值。
    注意该函数**只返回掩膜**，`d_bg` / `d_fg` 是内部局部量，从返回值里读不到。

    等价性证据（旧实现已于合并时删除，以下是当时的验证记录，不能原地复跑）：
      · 用原实现副本（**完整**判据，含 `& d_fg < 110`）与 consts 版逐区域比，
        6 个 ROI 的掩膜 array_equal 全为 True；
      · 少写 `& d_fg < 110` 的重建版与 consts 版 IoU 只有 0.891~0.972（页脚最低，
        按 consts 内部的底色采样口径算）——由此可见判据必须写全，只写前半句
        不是"等价"，而是另一份实现。
    注意：**不要**拿"转调后的 style 与 consts 比 IoU"当证据——那是构造性相等
    （本函数体就是一行 `return consts.ink_mask(...)`），不构成任何证据。

    保留 consts 那一版（带"知道文字色 / 不知道"两分支），这里只留函数名与签名，
    `extract` / `verify` / `measure` 等调用方一个都不用改。

    唯一可观测差异：传 3 位简写（如 `#4F8`）时旧实现在 `int("", 16)` 上抛
    ValueError，现在走"不知道该用什么色"的灰度+距离分支。真实调用方传进来的一律是
    `parse_hex_color` 归一化后的 6 位大写，这条路径到不了。
    """
    return consts.ink_mask(rgb, text_color)


def _render(line: str, weight: str, px: int):
    """用 PIL 渲染一行，返回 (墨迹宽, 墨迹高, 墨量%)。"""
    f = ImageFont.truetype(FONT_FILES[weight], max(int(px), 6))
    canvas = Image.new("L", (int(px * (len(line) + 4) * 1.4) + 80, px * 3 + 60), 0)
    ImageDraw.Draw(canvas).text((20, 20), line, fill=255, font=f)
    a = np.array(canvas) > 128
    ys, xs = np.where(a)
    if len(xs) == 0:
        return 0, 0, 0.0
    m = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    return int(m.shape[1]), int(m.shape[0]), float(m.mean() * 100.0)


def _fit_px(line: str, weight: str, target_w: float) -> int:
    """二分像素字号，使渲染墨迹宽贴近 target_w。"""
    lo, hi = 6, 220
    for _ in range(26):
        mid = (lo + hi) / 2.0
        w = _render(line, weight, int(round(mid)))[0]
        if w < target_w:
            lo = mid
        else:
            hi = mid
    return max(6, int(round((lo + hi) / 2.0)))


def solve(line: str, ink_w: float, ink_density: float, dpi: float) -> dict:
    """粗细 → 字号。

    line        取该块最宽的一行（多行块的字号由最宽行决定）
    ink_w       该行墨迹宽（px）
    ink_density 该行墨量 = 墨迹像素 / 墨迹框面积（%）。同字号下字越粗墨量越大，
                且与字号无关，所以能先于字号定档。
    """
    cands = []
    for w in WEIGHTS:
        px = _fit_px(line, w, ink_w)
        wpx, hpx, dens = _render(line, w, px)
        cands.append((abs(dens - ink_density), w, px, wpx, hpx, dens))
    err, weight, px, wpx, hpx, dens = min(cands, key=lambda t: t[0])
    return {
        "weight": weight,
        "size_pt": round(size_from_ink(ink_w, line, weight, dpi), 1),
        "letter_spacing_pt": 0.0,     # 首轮不猜；由 autocorrect 按渲染实测补
        "_weight_err": round(err, 1),  # 墨量差（百分点），>6 说明三档都不像，需人看
        "_dens": round(dens, 1),
    }
