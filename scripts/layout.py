# -*- coding: utf-8 -*-
"""layout.py — 按"规范 + 内容"算出每一页的坐标与字号（不抄图）。

这是与"事后修"相反的一条路：以前 `extract` 把图里的坐标当最终值，于是每页跟着图走、
同一档字差 0.8pt、要事后对齐。这里改成**先有内容，再按规范算**：

    内容（标题/几张卡/每张卡的标签与两条要点）  +  规范（边距/列/间距/内边距/档位）
        → 每个对象的 bbox 与 size_pt

三条保证（都是"做的时候就对"，不是事后检查）：
  1. **字号从档位里选**：从大到小试，放得下就用；放不下降一档。所以永远在档位上、永远不溢出。
  2. **位置从列/行算**：同一排的卡由同一个式子给出，左边缘天然对齐、间距天然相等。
  3. **放不下就报错**：内容超出卡片容量时抛 `LayoutError` 并说明哪个块超了多少，
     而不是偷偷缩到 12pt 或让它挤出去。

用法（试点）：
    python scripts/layout.py --page <页目录> --theme <visual.json> --out <输出目录>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import ImageFont

FONT_REGULAR = (r"C:/Windows/Fonts/msyh.ttc",)
FONT_BOLD = (r"C:/Windows/Fonts/msyhbd.ttc", r"C:/Windows/Fonts/msyh.ttc")
LINE_RATIO = 1.45          # 行高 = 字号 × 1.45（中文正文常用 1.4~1.5）
# 断行禁则：这些字符**不许出现在行首**（中文排版基本规则）。第一版没做，
# 断出过「能说出宫颈癌“三阶梯 / ”诊断流程」——后引号甩到下一行开头，一眼就是机器排的。
NO_LINE_START = "，。、；：？！）】》」』”’…—·%"
NO_LINE_END = "“‘（【「『《"          # 这些不该出现在行尾（引号/括号被甩在行末）
BREAK_AFTER_STRONG = "，。；：！？、—…"   # 断在这些后面最自然
BREAK_AFTER_WEAK = "与和及或等的地得并且而"  # 连接词/结构助词后面次之


class LayoutError(RuntimeError):
    """内容放不进规范给的框——必须报，不许静默缩字。"""


def _font(pt: float, dpi: float, weight: str = "regular"):
    """按**实际字重**取字体文件。

    第一版只用常规体量宽，于是加粗的正文被低估 8% 左右：布局器算"两行放得下"，
    build 画出来却折成三行 —— spec_lint 的溢出判据当场抓到了。量宽必须和画的一致。
    """
    px = max(1, round(pt * dpi / 72.0))
    for c in (FONT_BOLD if str(weight).lower() == "bold" else FONT_REGULAR):
        if Path(c).exists():
            return ImageFont.truetype(c, px)
    raise FileNotFoundError("找不到微软雅黑（msyh*.ttc）——排版要按真实字形量宽，不能估")


# PIL 量出的字宽比 PowerPoint 实际渲染**宽约 5%**（标定依据：原页 t14 在 20pt 下
# 一行放了 11 个汉字，其 bbox 宽 364px → 33.1px/字；20pt@125.4dpi 的 1em = 34.8px，比值 0.951）。
# 不标定的后果：布局器以为放不下 → 字号被迫降档、或断出「三/阶梯」这种半个词。
WIDTH_FACTOR = 0.95


def text_width(text: str, pt: float, dpi: float, weight: str = "regular") -> float:
    f = _font(pt, dpi, weight)
    return float(f.getlength(text)) * WIDTH_FACTOR


def wrap_lines(text: str, pt: float, dpi: float, max_w: float,
               weight: str = "regular") -> list[str]:
    """按真实字宽折行。中文逐字折，英文/数字按词折——只为了**判容量**，不用于渲染。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        if text_width(cur + ch, pt, dpi, weight) <= max_w:
            cur += ch
        else:
            lines.append(cur); cur = ch
    lines.append(cur)

    # 两行时按**中点**折，别按"能塞多少塞多少"：贪婪折法会断出「掌握临床表现与诊断路 / 径」
    # 这种半个词的尾巴——一眼就是机器排的。人排两行会尽量把两行长度拉平。
    plain = "".join(lines)
    if len(lines) == 2 and len(plain) > 3:
        total = text_width(plain, pt, dpi, weight)
        best_i, best_d = None, None
        for i in range(1, len(plain)):
            w = text_width(plain[:i], pt, dpi, weight)
            d = abs(w - total / 2)
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        # 打分挑断点：既要两行长度接近，又要断在人愿意断的地方
        scored = []
        for i in range(1, len(plain)):
            # 容差 6%：闸门自己按 BOX_PAD_RATIO + BOX_PAD_EM 留了内边距余量，
            # 布局器若按像素死卡，会把「能说出宫颈癌“三阶梯”」这种刚好超 8px 的正常行判死，
            # 于是断到「三 / 阶梯」。容差与闸门同量级，才不会一边过紧一边过松。
            if text_width(plain[:i], pt, dpi, weight) > max_w * 1.06:
                break
            head, tail = plain[:i], plain[i:]
            score = abs(text_width(head, pt, dpi, weight) - text_width(plain, pt, dpi, weight) / 2)
            # 断在标点/收引号后面是**加分项**（负分），不只是"不加罚"：
            # 否则"两行等长"会把语义断点压过去，断出「能结合病例进 / 行初步诊断思考」。
            if plain[i - 1] in BREAK_AFTER_STRONG:
                score -= 300
            elif plain[i - 1] in NO_LINE_START:      # 句末收引号/括号后面也很好
                score -= 250
            elif plain[i - 1] in BREAK_AFTER_WEAK:
                score += 20
            else:
                score += 180
            if plain[i] in NO_LINE_START or plain[i - 1] in NO_LINE_END:
                score += 2000
            if len(head) < 3 or len(tail) < 3:
                score += 400
            scored.append((score, head, tail))
        if scored:
            # 平手时取**靠后**的断点（头稍长）：中文里后半句通常更短，头长一点更像人排
            _, head, tail = min(scored, key=lambda x: (x[0], -len(x[1])))
            lines = [head, tail]

    # 禁则修正：把跑到行首的收尾标点拉回上一行（宁可那一行略宽，也不能让标点打头）
    fixed: list[str] = []
    for ln in lines:
        if ln and fixed and ln[0] in NO_LINE_START:
            fixed[-1] = fixed[-1] + ln[0]
            ln = ln[1:]
        fixed.append(ln)
    return [x for x in fixed if x != ""]


def fit_size(text: str, max_w: float, max_lines: int, scale: list[float], dpi: float,
             cap: float | None = None) -> float:
    """从大到小试档位，返回**放得下的最大档**。放不下抛 LayoutError。"""
    for pt in sorted([s for s in scale if cap is None or s <= cap], reverse=True):
        lines = wrap_lines(text, pt, dpi, max_w)
        if len(lines) <= max_lines:
            return float(pt)
    raise LayoutError(f"「{text[:14]}…」在 {max_w:.0f}px 宽里最多 {max_lines} 行也放不下最小档 "
                      f"{min(scale):g}pt —— 内容太多，请删字或加页")


def role_style(th: dict, role: str) -> tuple[float, str, str]:
    """按规范算某角色的字号与字重：`画布高 × pct` → 吸到最近档位。

    为什么不用"能塞多大就多大"：那是填满框，不是排版。第一版就栽在这——
    要点被算成 16pt、标签被撑到 32pt、字重全是 None，整页又细又挤。
    先由比例定"应该多大"，再有放不下时才往下降。
    """
    r = (th.get("roles") or {}).get(role)
    if not r or "pct" not in r:
        raise ValueError(f"visual.json 缺 type.roles.{role}.pct：字号必须由规范定，不猜")
    raw = float(r["pct"]) * th["H"] * 72.0 / th["dpi"]
    # 吸到**档位**上。第一版把 raw 本身也塞进候选，于是"最近"永远是它自己——等于没吸，
    # 结果标签算出 28.1pt 这种不在档位里的值（测试当场抓到）。
    # 只有超出档位上限的（标题 46.5）才保留原值：那是设计给的，不该被压到 32。
    pt = round(raw, 1) if raw > max(th["scale"]) else float(
        min(th["scale"], key=lambda x: (abs(x - raw), x)))
    color = r.get("color")
    if not color:
        raise ValueError(f"visual.json 缺 type.roles.{role}.color：字色也是规范的一部分，不猜")
    pal = th.get("palette") or {}
    hexv = pal.get(color, color)          # 既接受调色板名，也接受 #RRGGBB（便于个别例外）
    return pt, str(r.get("weight") or "regular"), str(hexv)


def fit_down(text: str, max_w: float, max_lines: int, scale: list[float], dpi: float,
             start_pt: float, weight: str = "regular") -> float:
    """从规范给的档开始试，放不下才**往下降**；绝不往上放大。"""
    if len(wrap_lines(text, start_pt, dpi, max_w, weight)) <= max_lines:
        return float(start_pt)
    for pt in sorted([x for x in scale if x < start_pt], reverse=True):
        if len(wrap_lines(text, pt, dpi, max_w, weight)) <= max_lines:
            return float(pt)
    raise LayoutError(f"「{text[:14]}…」在 {max_w:.0f}px 里最多 {max_lines} 行都放不下，最小档也不行")


def fit_size_group(texts: list[str], max_w: float, max_lines: int, scale: list[float],
                   dpi: float, cap: float | None = None) -> float:
    """一组**同角色**文字的统一字号。

    第一版是每条各自取"能放下的最大档"，结果同一排要点出现 22pt 和 18pt 并存——
    正是这个项目一直在治的病（同级不同大）。一组只能有一个字号：取组内**最紧的那条**
    决定档位，全组共用。
    """
    return min(fit_size(t, max_w, max_lines, scale, dpi, cap=cap) for t in texts)


def theme_from(visual: dict, canvas: dict) -> dict:
    """把 visual.json 的规范 + 画布合成布局参数。缺字段报错，不给默认值（默认值=第二套规则）。"""
    t = visual.get("type") or {}
    g = visual.get("global") or {}
    grid = visual.get("grid") or {}
    for k in ("scale",):
        if not t.get(k):
            raise ValueError(f"visual.json 缺 type.{k}")
    for k in ("margin", "columns", "gutter", "card_padding"):
        if k not in grid:
            raise ValueError(f"visual.json 缺 grid.{k}：布局必须由规范算，不猜")
    return {
        "W": int(canvas["w_px"]), "H": int(canvas["h_px"]),
        "dpi": float(canvas.get("dpi") or 125.4),
        "scale": [float(s) for s in t["scale"]],
        "title_pt": float(t.get("title_pt") or max(t["scale"])),
        "margin": grid["margin"], "cols": int(grid["columns"]),
        "gutter": float(grid["gutter"]), "pad": float(grid["card_padding"]),
        "nav": g.get("nav") or {}, "footer": g.get("footer") or {},
        "radius": visual.get("radius") or {},
        "roles": t.get("roles") or {},
        "palette": visual.get("palette") or {},
        "style": visual.get("style") or {},
    }


def _hex(th: dict, name: str) -> str:
    """调色板名 → #RRGGBB。允许直接写 hex，方便个别例外。"""
    return str((th.get("palette") or {}).get(name, name))


def _apply_style(container: dict, spec_style: dict, th: dict, key_map=None) -> dict:
    """把样式规范套到对象上：渐变/圆角/填充。缺样式就不改，但绝不在布局器里现编色。"""
    if not spec_style:
        return container
    if spec_style.get("gradient"):
        container["gradient"] = [_hex(th, c) for c in spec_style["gradient"]]
        container.pop("fill", None)
        if spec_style.get("angle") is not None:
            container["gradient_angle"] = spec_style["angle"]
    if spec_style.get("fill"):
        container["fill"] = _hex(th, spec_style["fill"])
    if spec_style.get("radius") is not None:
        container["radius_px"] = spec_style["radius"]
    return container


def card_grid_page(content: dict, th: dict) -> dict:
    """一页"标题 + N 张等宽卡 + 页脚"的版式。**所有坐标由规范算**，与参考图无关。

    content 只需要说"有什么"：
        {"nav": {"labels": [...], "active": 0},
         "title": "...", "cards": [{"label": "...", "bullets": ["...", "..."]}],
         "footer": ["P04", "教学目标"]}
    """
    W, H, dpi = th["W"], th["H"], th["dpi"]
    m = th["margin"]
    cols = int(content.get("columns") or th["cols"])
    gut = th["gutter"]
    col_w = (W - 2 * m["left"] - (cols - 1) * gut) / cols

    # 导航条：位置与高度由规范给（不在页里写死）
    nav = th["nav"]
    nav_h = float(nav.get("h") or 80)
    nav_bar = [m["left"], float(nav.get("y0") or 23), W - m["left"], float(nav.get("y0") or 23) + nav_h]

    # 标题：内容区顶部起，按单行容量选档
    title_top = float(m["top"])
    title_pt, title_w, title_c = role_style(th, "title")
    title_pt = fit_down(content["title"], W - 2 * m["left"] - 2 * 190, 1, th["scale"], dpi,
                        title_pt, title_w)
    t_h = title_pt * dpi / 72 * 1.2
    t_w = text_width(content["title"], title_pt, dpi, title_w)
    t_mid = W / 2
    # 标题居中：装饰线要挂在**标题实际宽度**两侧，所以框按文字宽算，不是撑满内容区
    title_box = [round(t_mid - t_w / 2, 1), title_top, round(t_mid + t_w / 2, 1),
                 round(title_top + t_h, 1)]

    # 卡片区：从标题下 40 起，到页脚上方 23 止——上下边都由规范推出来
    cards_top = round(title_box[3] + 40, 1)
    footer_y = float((th["footer"] or {}).get("y0") or (H - 73))
    cards_bottom = round(footer_y - 23, 1)
    card_h = round(cards_bottom - cards_top, 1)
    if card_h < 200:
        raise LayoutError(f"卡片区只剩 {card_h:.0f}px 高，标题太大或页脚太靠上")

    pad = th["pad"]
    inner_w = col_w - 2 * pad
    label_pt, label_w, label_c = role_style(th, "label")
    for c in content["cards"]:
        label_pt = min(label_pt, fit_down(c["label"], inner_w - 24, 1, th["scale"], dpi,
                                          label_pt, label_w))

    out = {"canvas": {"w_px": W, "h_px": H, "dpi": dpi,
                      "width_in": round(W / dpi, 4), "height_in": round(H / dpi, 4)},
           "containers": [], "elements": [], "assets": []}

    # ---- 导航（全局件，位置来自规范）
    out["containers"].append(_apply_style(
        {"id": "c_bar", "shape": "rounded_rect", "bbox": nav_bar},
        (th["style"] or {}).get("nav_bar"), th))
    labels = content["nav"]["labels"]
    active = int(content["nav"].get("active", 0))
    # 标签横向均分，分隔线落在相邻标签之间——位置是**算出来的**，所以每页一致
    slot = (nav_bar[2] - nav_bar[0]) / len(labels)
    nav_els = []
    nav_pt, nav_w, nav_c = role_style(th, "nav")
    nav_a_pt, nav_a_w, nav_a_c = role_style(th, "nav_active")
    for i, lab in enumerate(labels):
        pt, wt, cl = (nav_a_pt, nav_a_w, nav_a_c) if i == active else (nav_pt, nav_w, nav_c)
        cx = nav_bar[0] + slot * (i + 0.5)
        w = text_width(lab, pt, dpi, wt)
        nav_els.append({"id": f"t{i+1}", "type": "text", "text": lab,
                        "size_pt": pt, "weight": wt,
                        "bbox": [round(cx - w / 2, 1), round(nav_bar[1] + 26, 1),
                                 round(cx + w / 2, 1), round(nav_bar[1] + 58, 1)],
                        "color": cl})
        if 0 < i:
            x = round(nav_bar[0] + slot * i, 1)
            out["containers"].append({"id": f"c_div{i}", "shape": "line",
                                      "bbox": [x, round(nav_bar[1] + 22, 1), x, round(nav_bar[1] + 64, 1)],
                                      "line": {"color": "#AEC8EC", "width": 1}})
    # 高亮胶囊：落在激活标签后面，宽度 = 标签宽 + 2×24
    lab_i = nav_els[active]
    out["containers"].append(_apply_style({
        "id": "c_navpill", "shape": "rounded_rect",
        "bbox": [round(lab_i["bbox"][0] - 24, 1), round(nav_bar[1] + 3, 1),
                 round(lab_i["bbox"][2] + 24, 1), round(nav_bar[1] + 78, 1)]},
        (th["style"] or {}).get("nav_pill"), th))
    out["elements"] += nav_els

    # ---- 标题 + 两侧装饰线（长度按标题实际宽度算）
    out["elements"].append({"id": "t_title", "type": "text", "text": content["title"],
                            "size_pt": title_pt, "weight": title_w, "bbox": title_box,
                            "color": title_c})
    tw_mid = (title_box[0] + title_box[2]) / 2
    tw = text_width(content["title"], title_pt, dpi)
    for side, key in ((-1, "c_lineL"), (1, "c_lineR")):
        a = tw_mid + side * (tw / 2 + 40)
        b = a + side * 144
        y0 = round(title_box[1] + title_pt * 1.3, 1)
        # bbox 必须 left<right：左侧线是往左延伸的，直接写 a→b 会得到负宽度，
        # PowerPoint 打开即报"发生意外"（这次就是这么撞的）
        out["containers"].append({"id": key, "shape": "rect",
                                  "bbox": [round(min(a, b), 1), y0, round(max(a, b), 1), round(y0 + 3, 1)],
                                  "fill": "#5FBDFF"})

    # ---- 卡片：等宽、等距，位置由列号算
    body_pt, body_w, body_c = role_style(th, "body")
    # 全页要点共用一个字号：由规范定档，再按最紧的那条向下兜底一次（绝不放大）
    # 要点文本框的真实宽度：卡宽 − 徽标左边距(54) − 徽标到文字(85) − 右侧内边距(pad)。
    # 第一版写成 col_w - 2*pad - 54 - 85，多扣了一个 pad，于是 20pt 被判成放不下、一路降到 16pt。
    _avail = col_w - 54 - 75 - pad
    for c in content["cards"]:
        for b in c["bullets"]:
            body_pt = min(body_pt, fit_down(b, _avail, 2, th["scale"], dpi, body_pt, body_w))
    bullet_pt = body_pt
    for ci, card in enumerate(content["cards"]):
        x0 = round(m["left"] + ci * (col_w + gut), 1)
        x1 = round(x0 + col_w, 1)
        cb = [x0, cards_top, x1, cards_bottom]
        out["containers"].append(_apply_style(
            {"id": f"c_card{ci+1}", "shape": "rounded_rect", "bbox": cb},
            (th["style"] or {}).get("card"), th))
        # 顶部光晕：圆心在卡片中轴，直径 = 卡宽 40%
        d = round(col_w * 0.40, 1)
        cx = (x0 + x1) / 2
        out["containers"].append(_apply_style(
            {"id": f"c_glow{ci+1}", "shape": "ellipse",
             "bbox": [round(cx - d / 2, 1), round(cards_top + 14, 1),
                      round(cx + d / 2, 1), round(cards_top + 14 + d, 1)]},
            (th["style"] or {}).get("card_glow"), th))
        # 卡顶图标：素材由内容指定，位置由光晕圆心算（不抄图）
        if card.get("icon"):
            iw = d * 0.62
            # 图片必须进 **assets 段**：elements 段的每个元素 build 都当文字画（缺 text 直接 KeyError）
            out["assets"].append({"id": f"a_ic{ci+1}", "type": "image",
                                  "src": f"assets/{card['icon']}",
                                  "mask": "pictogram",
                                  "bbox": [round(cx - iw / 2, 1), round(cards_top + 14 + (d - iw) / 2, 1),
                                           round(cx + iw / 2, 1), round(cards_top + 14 + (d + iw) / 2, 1)]})
        # 标签胶囊：宽 = 卡宽 - 2×内边距，高 68，位置按标题下缘算
        pill_h = 68.0
        pill = [round(x0 + pad, 1), round(cards_top + 195, 1),
                round(x1 - pad, 1), round(cards_top + 195 + pill_h, 1)]
        _tag = (th["style"] or {}).get("tag") or {}
        _tag_cols = _tag.get(card.get("tag") or "blue") or _tag.get("blue") or []
        out["containers"].append(_apply_style(
            {"id": f"c_pill{ci+1}", "shape": "rounded_rect", "bbox": pill},
            {"gradient": _tag_cols, "angle": 90, "radius": 18} if _tag_cols else {}, th))
        lab_pt = label_pt
        lw = text_width(card["label"], lab_pt, dpi, label_w)
        lcx = (pill[0] + pill[2]) / 2
        out["elements"].append({"id": f"t_lab{ci+1}", "type": "text", "text": card["label"],
                                "size_pt": lab_pt, "weight": label_w,
                                "bbox": [round(lcx - lw / 2, 1), round(pill[1] + 17, 1),
                                         round(lcx + lw / 2, 1), round(pill[1] + 52, 1)],
                                "color": label_c})
        # 要点：从胶囊下 42 起，每行 118 高；每行前一个圆形徽标（直径 52）
        row_y = pill[3] + 42
        for bi, bullet in enumerate(card["bullets"]):
            bx = x0 + 54
            halo_d = 72.0
            out["containers"].append(_apply_style(
                {"id": f"c_halo{ci*2+bi+1}", "shape": "ellipse",
                 "bbox": [round(bx - 10, 1), round(row_y - 3, 1),
                          round(bx - 10 + halo_d, 1), round(row_y - 3 + halo_d, 1)]},
                (th["style"] or {}).get("badge_halo"), th))
            out["containers"].append(_apply_style(
                {"id": f"c_badge{ci*2+bi+1}", "shape": "ellipse",
                 "bbox": [round(bx, 1), round(row_y + 7, 1),
                          round(bx + 52, 1), round(row_y + 59, 1)]},
                (th["style"] or {}).get("badge"), th))
            # 勾：把原图里 6 个点的勾归一化后缩放——形状是几何，不该每页各画一个
            uw, uh = 28.3, 21.2
            check = [[0.0, 13.2], [8.0, 21.2], [28.3, 4.2], [23.9, 0.0], [8.0, 17.0], [4.2, 9.0]]
            ox, oy = bx + 12.0, row_y + 22.0
            out["containers"].append({
                "id": f"c_check{ci*2+bi+1}", "shape": "freeform",
                "bbox": [round(ox, 1), round(oy, 1), round(ox + uw, 1), round(oy + uh, 1)],
                "points": [[round(ox + px, 1), round(oy + py, 1)] for px, py in check],
                "fill": "#FFFFFF"})
            tx0 = round(bx + 75, 1)
            tw_avail = round(x1 - pad - tx0, 1)
            bpt = bullet_pt if bullet_pt else fit_size(bullet, tw_avail, 2, th["scale"], dpi, cap=body_pt)
            lines = wrap_lines(bullet, bpt, dpi, tw_avail, body_w)
            line_h = bpt * dpi / 72 * LINE_RATIO
            # **必须把断行写进文本**：出稿侧按 "\n" 分段、每段再折行，而闸门要求每段不折行
            # （否则判"溢出风险"）。所以布局器算完宽度后要自己断好，不能指望渲染器帮它换行。
            out["elements"].append({
                "id": f"t_b{ci*2+bi+1}", "type": "text", "text": "\n".join(lines),
                "size_pt": bpt, "weight": body_w,
                "bbox": [tx0, round(row_y, 1), round(tx0 + tw_avail, 1),
                         round(row_y + line_h * len(lines), 1)],
                "color": body_c})
            row_y += 118

    # ---- 页脚：左对齐到内容区左边距，字号取档位最小档之上的那一档
    fy = float((th["footer"] or {}).get("y0") or (H - 73))
    f_pt, f_w, f_c = role_style(th, "footer")
    fx = m["left"] + 4
    for i, txt in enumerate(content.get("footer") or []):
        w = text_width(txt, f_pt, dpi, f_w)
        out["elements"].append({"id": f"t_foot{i+1}", "type": "text", "text": txt,
                                "size_pt": f_pt, "weight": f_w,
                                "bbox": [round(fx, 1), round(fy, 1),
                                         round(fx + w, 1), round(fy + f_pt * dpi / 72 * 1.3, 1)],
                                "color": f_c})
        fx = round(fx + w + 30, 1)

    return out


def content_from_spec(spec: dict, th: dict) -> dict:
    """从一份**已有的** spec 反推"这一页有什么"（不抄坐标，只认内容与角色）。

    为什么需要它：布局器要输入"内容"，而现有 9 页的输入是 spec。没有这一步，
    就只能手写 content.json——那不叫改 skill，叫改这一页。

    认法（按几何与字号，不看命名，因为 9 页命名有 5 种）：
      · 导航：落在导航条矩形里、y 在条内的文字，按 x 排序；字号最大的那个是激活项
      · 标题：内容区顶部字号最大的单块文字
      · 卡片：内容区里面积够大的圆角矩形，按 x 排序；张数 = 一行的数量
      · 卡片标签：卡片上半部、字号较大且**居中**的文字
      · 要点：卡片里其余文字，按 y 排序
      · 图标：卡片范围内的 assets
    """
    W, H = th["W"], th["H"]
    m = th["margin"]
    nav = th["nav"]
    nav_y1 = float(nav.get("y0") or 23) + float(nav.get("h") or 80)
    boxes = {}
    for seg in ("containers", "elements", "assets"):
        for it in spec.get(seg) or []:
            if it.get("id") and it.get("bbox"):
                boxes[str(it["id"])] = it

    def bb(i):
        return [float(x) for x in boxes[i]["bbox"]]

    # 导航：条内的文字
    nav_labels = sorted(
        [i for i, it in boxes.items()
         if it.get("text") and bb(i)[1] < nav_y1 and bb(i)[0] >= 0],
        key=lambda i: bb(i)[0])
    # 激活项：字号最大（同大取第一个）
    active = 0
    if nav_labels:
        sizes = [float(boxes[i].get("size_pt") or 0) for i in nav_labels]
        active = sizes.index(max(sizes))
    labels = [str(boxes[i]["text"]).replace("\n", "") for i in nav_labels]

    # 标题：内容区顶部、字号最大的单块文字（排除导航与页脚）
    footer_y = float((th.get("footer") or {}).get("y0") or (H - 73))
    body_texts = [i for i, it in boxes.items()
                  if it.get("text") and bb(i)[1] >= nav_y1 and bb(i)[3] <= footer_y - 5]
    title_id = max(body_texts, key=lambda i: (float(boxes[i].get("size_pt") or 0), -bb(i)[1]),
                   default=None)
    title = str(boxes[title_id]["text"]).replace("\n", "") if title_id else ""

    # 卡片：内容区里面积大的圆角矩形
    cards_ids = [i for i, it in boxes.items()
                 if it.get("shape") in ("rounded_rect", "rect")
                 and bb(i)[1] >= nav_y1 and bb(i)[3] <= footer_y
                 and (bb(i)[2] - bb(i)[0]) * (bb(i)[3] - bb(i)[1]) > 0.08 * W * H]
    cards_ids.sort(key=lambda i: bb(i)[0])

    cards = []
    for cid in cards_ids:
        cb = bb(cid)
        inner = [i for i, it in boxes.items()
                 if it.get("text") and i != title_id
                 and bb(i)[0] >= cb[0] - 1 and bb(i)[2] <= cb[2] + 1
                 and bb(i)[1] >= cb[1] - 1 and bb(i)[3] <= cb[3] + 1]
        inner.sort(key=lambda i: (bb(i)[1], bb(i)[0]))
        if not inner:
            continue
        lab = max(inner, key=lambda i: float(boxes[i].get("size_pt") or 0))
        lab_center = abs((bb(lab)[0] + bb(lab)[2]) / 2 - (cb[0] + cb[2]) / 2) < 0.15 * (cb[2] - cb[0])
        label = str(boxes[lab]["text"]).replace("\n", "")
        # **不继承硬换行**：原图里的换行位置是当时排版的痕迹（可能就是为了凑宽度），
        # 继承过来会让字号被迫降档（实测 20pt 被压到 18pt）。折行交给布局器按宽度算。
        bullets = [str(boxes[i]["text"]).replace("\n", "") for i in inner
                   if i != lab and not (lab_center and abs(bb(i)[0] - bb(lab)[0]) < 2)]
        icon = None
        for aid, it in boxes.items():
            if "asset" not in str(it.get("type", "")) and not str(aid).startswith("a_"):
                continue
            if bb(aid)[0] >= cb[0] - 1 and bb(aid)[2] <= cb[2] + 1 and bb(aid)[1] >= cb[1] - 1 and bb(aid)[3] <= cb[3] + 1:
                icon = Path(str(it.get("src") or f"{aid}.png")).name
                break
        cards.append({"label": label, "bullets": bullets, **({"icon": icon} if icon else {})})

    footer = [str(boxes[i]["text"]).replace("\n", "") for i in sorted(boxes, key=lambda i: bb(i)[0])
              if boxes[i].get("text") and bb(i)[1] >= footer_y - 5]
    return {"nav": {"labels": labels, "active": active}, "title": title,
            "cards": cards, "footer": footer,
            **({"columns": len(cards_ids)} if cards_ids else {})}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--content", default=None, help="内容 json（这一页有什么）")
    ap.add_argument("--from-spec", default=None, help="从已有 spec 反推内容（不抄坐标）")
    ap.add_argument("--dump-content", default=None, help="把反推出的内容写到这个文件，便于人改")
    ap.add_argument("--theme", required=True, help="visual.json（规范）")
    ap.add_argument("--canvas", required=True, help="画布 json（w_px/h_px/dpi）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    th = theme_from(json.loads(Path(a.theme).read_text(encoding="utf-8")),
                    json.loads(Path(a.canvas).read_text(encoding="utf-8")))
    if a.from_spec:
        # 必须看**展开视图**：raw 里 size_pt 可能是 `{type.size.t_m}` 这种 token，
        # 拿它算字号会直接崩（第一版就这么崩的）。反推内容要的是最终数值。
        import specmerge
        spec_in = specmerge.load_merged(Path(a.from_spec), quiet=True, expand=True)
        content = content_from_spec(spec_in, th)
        if a.dump_content:
            Path(a.dump_content).write_text(json.dumps(content, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    else:
        if not a.content:
            raise SystemExit("要么给 --content，要么给 --from-spec")
        content = json.loads(Path(a.content).read_text(encoding="utf-8"))
    spec = card_grid_page(content, th)
    Path(a.out).write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已生成 {a.out}：对象 {len(spec['containers']) + len(spec['elements'])} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
