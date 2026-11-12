#!/usr/bin/env python3
"""build.py — spec.json -> rebuilt.pptx（只有三个原语：text / shape / image）

写进代码的默认值（不靠人记）：
  * 文本框内部 margin 一律 0（默认 0.1in 会让每个字偏 7~9px）
  * 行距用倍数（float），不用 spcPts —— spcPts 会把首行压低 (行距-自然行高)/2
  * 文字框 anchor=MIDDLE，框高 = 行数×行高×LINE_EM + 0.34em，视觉中心对齐墨迹中心
  * 字体/粗细/字距只从 style.py 取，本文件不设字体常量
  * 图片按 bbox 拉伸，不做二次缩放
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from lxml import etree
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

import style
import specmerge
from consts import LINE_EM, parse_hex_color, parse_line_style

# 内置表样式 "No Style, No Grid"：显式指定它 = 0 条框线 + 0 隔行底色。
# 为什么需要它（实测，见 add_table 注释）：`tblPr` 缺 `tableStyleId` 时会回落到
# Default Table Style（框线照旧），所以"不要框线"必须**主动声明**一个无框样式。
TABLE_STYLE_NO_GRID = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"

SHAPES = {
    "rect": MSO_SHAPE.RECTANGLE,
    "rounded_rect": MSO_SHAPE.ROUNDED_RECTANGLE,
    "ellipse": MSO_SHAPE.OVAL,
    "chevron": MSO_SHAPE.CHEVRON,
    "right_arrow": MSO_SHAPE.RIGHT_ARROW,
    "left_arrow": MSO_SHAPE.LEFT_ARROW,
    "diamond": MSO_SHAPE.DIAMOND,
}


def _set_alpha(color_el, alpha_pct: float) -> None:
    a = etree.SubElement(color_el, qn("a:alpha"))
    a.set("val", str(int(alpha_pct * 1000)))


def _hex6(value, what: str = "颜色") -> str:
    """颜色取值的唯一出口：交给 consts.parse_hex_color（与 spec_lint 同源）。

    为什么要有这层：以前本文件散着 6 处 `RGBColor.from_string(v.lstrip("#").upper())`，
    `#4F8`（3 位）会在这里抛 ValueError/KeyError 崩在建了一半的 pptx 上，
    `#GGG`（非法字符）也一样——用户看到的是 traceback，不是"这个颜色写法不认识"。
    现在非法值一律 REFUSED（SystemExit，人话），3 位按 CSS 短写展开成 6 位。
    """
    try:
        return parse_hex_color(value, what)
    except ValueError as exc:
        raise SystemExit(f"REFUSED: {exc}") from None


def _rgb(value, what: str = "颜色") -> RGBColor:
    """_hex6 的 RGBColor 版本。"""
    return RGBColor.from_string(_hex6(value, what))


def _shape_name(el: dict) -> str:
    """A：spec 写了 name 就用 name，没写仍是 id。三处写入点共用（形状 / 文字 / 图片）。

    为什么：以前三处都是 `xxx.name = el["id"]` **无条件覆盖**，作者写进 spec 的 name 被
    静默丢弃；而 apply_effects 读 el["glow"]/el["shadow"]，编辑器里两个一模一样的圆角块
    分不清哪个带特效、哪个是纯色（选中窗格只能看到 id 这种机器名）。
    连带代价（已实测）：presence / editability 按 name 认元素，name ≠ id 时那两道闸会
    "找不到"，所以调用方必须同时调 _set_descr 把 id 留一份。
    """
    return str(el.get("name") or el["id"])


def _set_id_attr(sp, eid: str) -> None:
    """把 id 另存一份到 `cNvPr@title`（只在起了自定义 name 时写）。

    为什么：name 被抢走之后，id 在 pptx 里就没有第二处落点了。
    为什么是 title 而不是 descr：add_picture 自己会把图片文件名写进 descr
    （实测 id=i1 的图 descr="i1.png"），拿 descr 兜底会让每张图都被认成文件名。
    title 没有这个占用，且历史成品没有这个属性 → 不写的时候产物逐字节不变。
    cNvPr 的父节点形状类不同（p:nvSpPr / p:nvCxnSpPr），所以用 xpath 取，不猜属性名。
    """
    found = sp._element.xpath(".//p:cNvPr")
    if found:
        found[0].set("title", eid)


def _style_run(run, size_pt: float, weight: str, hexcolor: str):
    """给一个 run 写字形三件套 + 东亚字体，返回它的 `a:rPr`。**文字框与表格单元格共用一份**。

    逐行抄自 add_text 原来的内联写法（顺序一致，产物逐字节不变）：字号 → 字重 → 字体
    → 字色 → `a:ea`。`a:ea` 必须单独设，否则中文回退宋体，而 `a:latin` 管不到中文。
    表格单元格如果各写一份，就会出现"文字框是雅黑、表里是宋体"这种只有渲染才看得见的差。
    返回 rPr 是因为 add_text 还要在它上面写字距（`@spc`），不能只当副作用函数用。
    """
    run.font.size = Pt(size_pt)
    typeface = style.FONT_TYPEFACE[weight]
    run.font.bold = (weight == "bold")
    run.font.name = typeface
    run.font.color.rgb = RGBColor.from_string(hexcolor)
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}ea")
    if ea is None:
        ea = etree.SubElement(rPr, "{http://schemas.openxmlformats.org/drawingml/2006/main}ea")
    ea.set("typeface", typeface)
    return rPr


def apply_rotation(sp, el: dict) -> None:
    """spec 的 `rotation`（度，顺时针，0~360）→ `sp.rotation`。

    为什么归一化：python-pptx 的写入口接受任意浮点，读回却是 0~360（实测写 -15 读 345）。
    取模之后"读回值 = 写入值"才恒成立，否则同一个角度有两种写法、两种读回值。
    不写这个键 = 不动（历史产物逐字节不变）。
    """
    rot = el.get("rotation")
    if rot is None or rot == "":
        return
    try:
        deg = float(rot)
    except (TypeError, ValueError):
        raise SystemExit(f"REFUSED: {el.get('id')} 的 rotation={rot!r} 不是数字（单位：度，0~360）")
    sp.rotation = deg % 360.0


def _line_cfg(el: dict) -> dict:
    """线属性的取值处：**只认 `el["line"]` 这一段**，不合并元素顶层字段。

    核实记录（2026-11-08 P2c）：方案文档说"顶层写 color 会被静默忽略、要改成
    {**el, **el["line"]}"——**判不成立，不做**。理由两条：
      ① 全库 24 份 spec 里 `shape:"line"` 0 例、顶层线色 0 例，没有任何页面这么写过；
      ② `set_line` 读的就是 line 这一段，顶层 `color` 从来不是"线色"的声明位
         （元素的 `color` 是文字色），把两者合并等于给同一个键造两种含义。
    这里只保留"没写 line 段时给个空 dict"这一层，供连接线取默认值用。
    """
    ln = el.get("line")
    return dict(ln) if isinstance(ln, dict) else {}


# spec 的 dash 取值 → python-pptx 枚举。键就是 `a:prstDash@val` 的原值（实测八个成员一一对应：
# SOLID→solid / DASH→dash / DASH_DOT→dashDot / LONG_DASH→lgDash / LONG_DASH_DOT→lgDashDot /
# DASH_DOT_DOT→lgDashDotDot / ROUND_DOT→sysDot / SQUARE_DOT→sysDash），所以读回可断言相等。
_DASH_ENUM = {
    "solid": MSO_LINE.SOLID,
    "dash": MSO_LINE.DASH,
    "dashDot": MSO_LINE.DASH_DOT,
    "lgDash": MSO_LINE.LONG_DASH,
    "lgDashDot": MSO_LINE.LONG_DASH_DOT,
    "lgDashDotDot": MSO_LINE.DASH_DOT_DOT,
    "sysDot": MSO_LINE.ROUND_DOT,
    "sysDash": MSO_LINE.SQUARE_DOT,
}


def set_line(sp, cfg: dict, eid: str = "line") -> None:
    ln = sp.line
    # 文档与 spec 里写的是 width_pt；老页面写过 width。两个都认，前者优先。
    # 以前只读 width，于是 width_pt 静默失效、线宽永远 1pt（实测 0.75 → 1.0）。
    ln.width = Pt(float(cfg.get("width_pt", cfg.get("width", 1))))
    ln.color.rgb = _rgb(cfg.get("color", "#FFFFFF"), "线色")
    if cfg.get("alpha") is not None:
        solid = ln.color._xFill
        _set_alpha(solid.find(qn("a:srgbClr")), cfg["alpha"])
    # 虚实 / 箭头端点。校验与规范化只在 consts.parse_line_style 一处（与 spec_lint 同源）。
    st = parse_line_style(cfg, eid)
    if st["dash"]:
        # 取值就是 prstDash@val 的原值，所以 MSO_LINE 成员与它一一对应、读回逐字相同。
        ln.dash_style = _DASH_ENUM[st["dash"]]
    ln_el = ln._get_or_add_ln()
    for side, tag in (("head", "a:headEnd"), ("tail", "a:tailEnd")):
        if not st[side]:
            continue
        # python-pptx 没有暴露箭头端点，直接写元素。顺序必须在 prstDash 之后（schema），
        # 这里按 head → tail 依次 append 到末尾即为合法顺序（实测子节点为
        # solidFill / prstDash / headEnd / tailEnd）。
        node = ln_el.find(qn(tag))
        if node is None:
            node = etree.SubElement(ln_el, qn(tag))
        node.set("type", st[side]["type"])
        node.set("w", st[side]["w"])
        node.set("len", st[side]["len"])



def _write_gradfill(gf, colors, poss=None, angle=90.0) -> None:
    """把任意 a:gradFill 重写成 N 档（N>=2）。形状与 slide background 共用这一份。"""
    old = gf.find(qn("a:gsLst"))
    if old is not None:
        gf.remove(old)
    gsLst = etree.Element(qn("a:gsLst"))
    n = len(colors)
    for i, hexv in enumerate(colors):
        pos = float(poss[i]) if (poss and i < len(poss)) else (i / (n - 1))
        gs = etree.SubElement(gsLst, qn("a:gs"))
        gs.set("pos", str(int(round(max(0.0, min(1.0, pos)) * 100000))))
        clr = etree.SubElement(gs, qn("a:srgbClr"))
        clr.set("val", hexv)
    gf.insert(0, gsLst)
    # 角度同样要翻符号（见形状分支的实测记录）
    lin = gf.find(qn("a:lin"))
    if lin is None:
        lin = etree.SubElement(gf, qn("a:lin"))
    lin.set("ang", str(int(round(((360.0 - float(angle)) % 360.0) * 60000))))
    lin.set("scaled", "0")

def _write_gradient_stops(sp, colors, poss=None, angle=90.0) -> None:
    """把 gradFill 的 gsLst 重写成 N 档（N≥3）。

    为什么要有这条：参考页的蓝色是「两端亮、中段暗」的三档以上渐变（实测六点色阶非单调），
    两档线性只能取近似——2026-11-07 一页里 16 条 quality 不过有 10 条是这个原因，
    而 1.2.0 存档版本来就支持多停靠，是 2.x 重写时退化成两档的（属于回归，不是已知下界）。
    """
    spPr = sp._element.spPr
    gf = spPr.find(qn("a:gradFill"))
    if gf is None:
        raise SystemExit("REFUSED: gradient 写了 3 档以上，但 fill.gradient() 没产出 a:gradFill")
    old = gf.find(qn("a:gsLst"))
    if old is not None:
        gf.remove(old)
    gsLst = etree.Element(qn("a:gsLst"))
    n = len(colors)
    for i, hexv in enumerate(colors):
        pos = float(poss[i]) if (poss and i < len(poss)) else (i / (n - 1))
        gs = etree.SubElement(gsLst, qn("a:gs"))
        gs.set("pos", str(int(round(max(0.0, min(1.0, pos)) * 100000))))
        clr = etree.SubElement(gs, qn("a:srgbClr"))
        clr.set("val", hexv)
    gf.insert(0, gsLst)          # gradFill 子元素顺序固定：gsLst 必须在 lin/path 之前
    lin = gf.find(qn("a:lin"))
    if lin is not None:
        lin.set("ang", str(int(round(((360.0 - float(angle)) % 360.0) * 60000))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--assets", default=None, help="assets 目录（图片 src 相对此目录）")
    ap.add_argument("--override", default=None,
                    help="修正层（默认取 spec 同目录的 override.json；不存在＝只有初值）")
    a = ap.parse_args()

    # 读 spec 走**唯一入口**（spec + override 合并都在 specmerge 里）。
    # 原先这里是 json.loads + 自己 merge override：同一件事两处实现，
    # 且"元素没写 size_pt"时只有这条自建分支能算出来，别的消费者算不出来。
    # expand=True：build 看**展开视图**（token 代入后的值）。闸门看 raw，两边分工见 specmerge。
    spec = specmerge.load_merged(Path(a.spec), override_path=(a.override or None), expand=True)

    # 门闩：有 inventory.json 的页面走新规——plan 报告必须对当前制作数据有效且通过。
    # 有效性看 dependency_key（语义摘要），不再看 spec/override 的原始字节 md5：
    #   ① 只改 JSON 缩进不会失效；② 旧 quality.json 不会挡住修后的构建。
    # 没 inventory 的老页面走旧路（打一声提示），避免把历史页一次性搞死。
    _d = Path(a.spec).parent
    if (_d / "inventory.json").exists():
        sys.path.insert(0, str(Path(__file__).resolve().parent / "delivery"))
        import json as _json
        import report_state as _rs
        try:
            _inv = _json.loads((_d / "inventory.json").read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"REFUSED: inventory.json 解析失败（{exc}）——先修好决策记录再构建")
        _rd = _rs.read_report(_d / "_check" / "plan.json", _rs.key_plan(Path(a.spec), _inv))
        if not _rd["passed"]:
            head = "；".join((_rd.get("report") or {}).get("errors", [])[:3]) or _rd.get("reason")
            raise SystemExit(f"REFUSED: plan 不可用（{_rs.describe(_rd)}）{'：' + head if head else ''}")

    cv = spec["canvas"]
    dpi = cv["dpi"]
    W_in = cv["width_in"]
    H_in = cv["height_in"]

    # elements 段一律走 add_text，而 add_text 无条件读 el["text"]：缺 text 时会在建了一半
    # 形状之后 KeyError traceback，只留下一个半成品 pptx（2026-11-08 第二轮 QA 实测，
    # 而 spec_lint / gates plan 当时都 rc=0）。这里先一次性点名，REFUSED 而不是崩。
    _notext = [str(e.get("id")) for e in (spec.get("elements") or [])
               if not str(e.get("text") or "").strip()]
    if _notext:
        raise SystemExit(f"REFUSED: elements 段里 {len(_notext)} 个元素没有 text："
                         f"{', '.join(_notext[:8])}——build 对 elements 段一律 add_text，"
                         f"缺 text 无法绘制（spec_lint 的 ① 必填检查会报同一件事）")

    # 字号**不许有默认值**：旧实现缺 size_pt 时静默画成 14pt（表格）或 18pt（文字），
    # 两处还不一样——产物里因此会出现一个谁都没声明过的字号，且没有检查能发现它。
    # 这里一次性点名**所有**缺字号的块（不是只报第一个），进门就拒，不留半成品。
    _nosize = [str(e.get("id")) for e in (spec.get("elements") or [])
               if str(e.get("type")) in ("text", "table") and e.get("size_pt") is None]
    if _nosize:
        raise SystemExit(f"REFUSED: {len(_nosize)} 个块没有 size_pt：{', '.join(_nosize)}"
                         f"——字号必须由 spec / override 给出，不许默认"
                         f"（旧行为会静默画成 14pt 或 18pt）")

    # 线型 / 箭头在动笔之前先校验一遍：错值必须"进门就拒"，不能画到一半才炸。
    # 校验器只有 consts.parse_line_style 一处（与 spec_lint 同源同句）。
    for _sec in ("containers", "assets", "elements"):
        for _el in spec.get(_sec) or []:
            _ln = _el.get("line")
            if isinstance(_ln, dict):
                try:
                    parse_line_style(_ln, str(_el.get("id")))
                except ValueError as _exc:
                    raise SystemExit(f"REFUSED: {_exc}")

    prs = Presentation()
    prs.slide_width = Inches(W_in)
    prs.slide_height = Inches(H_in)
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 空白版式，不继承占位符

    def P(v):  # px -> Emu
        return Emu(int(round(v / dpi * 914400)))

    def apply_effects(sp, el):
        """glow / shadow —— python-pptx 没有 API，直接写 a:effectLst。

        为什么必须支持：参考图的卡片是「4~6px 柔和白亮边 + 轻微外阴影」，只画 1.5px 硬线
        看着像贴上去的；而这个缺陷所有闸门都看不见（mae 按整卡平均被面积稀释到 2.3，
        residual 按最小面积把它滤掉）。实测参考：亮边峰值 251、带宽 4~6px；
        卡片正下方比底色暗 11 级、扩散约 10px。
        """
        glow, sh = el.get("glow"), el.get("shadow")
        if not glow and not sh:
            return
        ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
        spPr = sp._element.spPr
        eff = spPr.find(f"{{{ns}}}effectLst")
        if eff is None:
            eff = etree.SubElement(spPr, f"{{{ns}}}effectLst")
        if glow:
            g = etree.SubElement(eff, f"{{{ns}}}glow")
            g.set("rad", str(int(P(float(glow.get("radius_px", 5))))))
            clr = etree.SubElement(g, f"{{{ns}}}srgbClr")
            clr.set("val", _hex6(glow.get("color", "#FFFFFF"), "glow.color"))
            etree.SubElement(clr, f"{{{ns}}}alpha").set(
                "val", str(int(float(glow.get("alpha", 1.0)) * 100000)))
        if sh:
            o = etree.SubElement(eff, f"{{{ns}}}outerShdw")
            o.set("blurRad", str(int(P(float(sh.get("blur_px", 8))))))
            o.set("dist", str(int(P(float(sh.get("dist_px", 4))))))
            o.set("dir", str(int(float(sh.get("dir_deg", 90)) * 60000)))
            o.set("algn", "ctr")
            o.set("rotWithShape", "0")
            clr = etree.SubElement(o, f"{{{ns}}}srgbClr")
            clr.set("val", _hex6(sh.get("color", "#123B7A"), "shadow.color"))
            etree.SubElement(clr, f"{{{ns}}}alpha").set(
                "val", str(int(float(sh.get("alpha", 0.12)) * 100000)))

    def add_line(el):
        """D：`shape:"line"` 是连接线（p:cxnSp），不是矩形。

        MSO_SHAPE 里没有 line，以前 `SHAPES.get("line", ROUNDED_RECTANGLE)` 把它兜底成
        圆角矩形：一条 0 高的线变成一个细长块，编辑器里拖不动端点、也改不了走线方向。
        落成 connector（add_connector → p:cxnSp）后端点可拖、走线在 OOXML 里是真的。
        name 写 id（同 A）：presence 按 name 认元素，name 不是 id 就等于"没画出来"。
        """
        x0, y0, x1, y1 = el["bbox"]
        conn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,
                                          P(x0), P(y0), P(x1), P(y1))
        conn.name = _shape_name(el)
        if conn.name != str(el["id"]):
            _set_id_attr(conn, str(el["id"]))
        conn.shadow.inherit = False     # 主题阴影会在细线上糊出一条粗灰带
        set_line(conn, _line_cfg(el), str(el["id"]))   # 线色只认 el["line"]（顶层 color 是文字色，不是线色）
        apply_rotation(conn, el)
        return conn

    def add_table(el):
        """原生表格：`p:graphicFrame/a:tbl` —— 真表格，不是"文字框 + 若干条线"拼的格线。

        为什么写在 containers 段：`elements` 段在 build 里恒走 add_text，且被
        consts.drawn_as_text 当作"必是文字"（spec_lint 必填 text / verify 遍历 /
        editability 依赖键 / deck_lint 同层字号 / build 自己的 _notext 门闩全按这条走）。
        表格放进 elements 会同时打崩那五处；放进 containers 一条判据都不用改
        （presence 的 want 已含 containers，层序表 LAYER_Z 也有 containers）。
        """
        rows = int(el.get("rows") or 0)
        cols = int(el.get("cols") or 0)
        cells = el.get("cells") or []
        if rows <= 0:
            rows = len(cells)              # rows/cols 可省：从 cells 推，少一处能写错的地方
        if cols <= 0:
            cols = max((len(r) for r in cells), default=0)
        if rows <= 0 or cols <= 0:
            raise SystemExit(f"REFUSED: {el['id']} 的原生表格需要 rows/cols，"
                             f"或给 cells 让它们能推出来")
        if cells and len(cells) != rows:
            raise SystemExit(f"REFUSED: {el['id']} 的 cells 有 {len(cells)} 行，与 rows={rows} 不一致")
        for _i, _r in enumerate(cells):
            if len(_r) > cols:
                raise SystemExit(f"REFUSED: {el['id']} 的 cells 第 {_i + 1} 行有 {len(_r)} 列，"
                                 f"超过 cols={cols}")
        x0, y0, x1, y1 = el["bbox"]
        gf = slide.shapes.add_table(rows, cols, P(x0), P(y0), P(x1 - x0), P(y1 - y0))
        tbl = gf.table
        gf.name = _shape_name(el)
        if gf.name != str(el["id"]):
            _set_id_attr(gf, str(el["id"]))
        # 框线：PPT 原生表的框线来自**主题表样式**。两条实测（2026-11-08，PowerPoint 导
        # 1600×900 PNG、空单元格以隔离文字）：
        #   ① 只移除 `<a:tableStyleId>` **不够**：缺样式时回落到 Default Table Style，
        #      两行之间的分隔线照旧（分界线上暗像素 1350，与 borders:true 一样）；
        #   ② 显式指定内置样式 **No Style, No Grid**（下面的 GUID）→ 表内暗像素 **0**，
        #      且 tbl 里 **0 个 ln\* 节点**。
        # 所以"默认无框线"= 指定 No-Grid（一个显式声明），而不是往每格写 noFill、也不是"不写"。
        # borders:true 时仍逐格写显式 ln*：这样框线不依赖主题，换机器也不会忽然变样。
        tblPr = tbl._tbl.tblPr
        stale = tblPr.find(qn("a:tableStyleId"))
        if stale is not None:
            tblPr.remove(stale)
        style_ref = etree.SubElement(tblPr, qn("a:tableStyleId"))
        style_ref.text = TABLE_STYLE_NO_GRID
        tblPr.set("firstRow", "0")
        tblPr.set("bandRow", "0")
        if el.get("borders"):
            ln_w = str(int(Pt(float(el.get("border_pt", 1.0)))))
            ln_c = _hex6(el.get("border_color", "#BFBFBF"), f"{el['id']} 的 border_color")
            for r in range(rows):
                for c in range(cols):
                    tcPr = tbl.cell(r, c)._tc.get_or_add_tcPr()
                    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):   # 顺序按 schema，打乱会判非法
                        ln = etree.SubElement(tcPr, qn(tag))
                        ln.set("w", ln_w)
                        ln.set("cap", "flat")
                        ln.set("cmpd", "sng")
                        ln.set("algn", "ctr")
                        etree.SubElement(etree.SubElement(ln, qn("a:solidFill")),
                                         qn("a:srgbClr")).set("val", ln_c)
        size_pt = float(el["size_pt"])      # 必有：进门已校验，缺就 REFUSED（不再默认 14pt）
        color = _hex6(el.get("color", "#000000"), f"{el['id']} 的 color")
        header = bool(el.get("header"))
        pad = el.get("pad_px")
        for r in range(rows):
            for c in range(cols):
                cell = tbl.cell(r, c)
                raw = cells[r][c] if (r < len(cells) and c < len(cells[r])) else ""
                if isinstance(raw, dict):
                    txt = "" if raw.get("text") is None else str(raw["text"])
                    csize = float(raw.get("size_pt", size_pt))
                    ccolor = _hex6(raw.get("color", color), f"{el['id']} 单元格颜色")
                    cbold = raw.get("bold")
                else:
                    txt = "" if raw is None else str(raw)
                    csize, ccolor, cbold = size_pt, color, None
                if cbold is None:
                    cbold = header and r == 0          # header:true 只作用于首行
                weight = style.resolve_weight(
                    cbold if cbold is not None else el.get("weight", False))
                if pad is not None:
                    cell.margin_left = cell.margin_right = P(pad)
                    cell.margin_top = cell.margin_bottom = P(pad)
                tf = cell.text_frame
                tf.word_wrap = True
                for i, line in enumerate(txt.split("\n")):
                    para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    run = para.add_run()
                    run.text = line
                    _style_run(run, csize, weight, ccolor)
        for i, w in enumerate(el.get("col_w") or []):      # 像素，与 bbox 同一套单位
            if i < cols:
                tbl.columns[i].width = P(float(w))
        for i, h in enumerate(el.get("row_h") or []):
            if i < rows:
                tbl.rows[i].height = P(float(h))
        apply_rotation(gf, el)
        return gf

    def add_shape(el):
        if str(el.get("type")) == "table":
            return add_table(el)      # 原生表格走自己的分支，不掉进预设形状兜底
        if str(el.get("shape")) == "line":
            return add_line(el)       # D：连接线走自己的分支，不掉进预设形状兜底
        x0, y0, x1, y1 = el["bbox"]
        st = None                     # freeform 分支不设预设形状，箭头/圆角判断靠它区分
        if el.get("shape") == "freeform":
            # 任意多边形：7 个预设形状表达不了「缎带/斜切尾」这类轮廓。
            # points 用的是页面像素绝对坐标（和 bbox 同一套），不是相对偏移——
            # 少一层换算就少一处能算错的地方。
            pts = el.get("points") or []
            if len(pts) < 3:
                raise SystemExit(f"REFUSED: {el['id']} freeform 需要 ≥3 个点")
            fb = slide.shapes.build_freeform(P(pts[0][0]), P(pts[0][1]), scale=1.0)
            fb.add_line_segments([(P(p[0]), P(p[1])) for p in pts[1:]], close=True)
            sp = fb.convert_to_shape()
        else:
            st = SHAPES.get(el.get("shape", "rounded_rect"), MSO_SHAPE.ROUNDED_RECTANGLE)
            sp = slide.shapes.add_shape(st, P(x0), P(y0), P(x1 - x0), P(y1 - y0))
        sp.shadow.inherit = False
        grad = el.get("gradient")
        if grad:
            # 卡片/胶囊的上下渐变是原图立体感的主要来源，纯平涂一眼假。
            # 2 档走原路径（保证已交付页面的 pptx 逐字节不变）；3 档以上重写 gsLst。
            # 档位逐个解析（与 spec_lint 的 gradient[i] 同一套话术）：非法档位以前是
            # 原样写进 srgbClr@val（实测写入 "GGG" 三个字符），渲染出来是怪色/黑且 rc=0。
            grad = [_hex6(c, f"gradient[{i}]") for i, c in enumerate(grad)]
            sp.fill.gradient()
            if len(grad) > 2:
                _write_gradient_stops(sp, grad, el.get("gradient_pos"),
                                      float(el.get("gradient_angle", 90)))
            else:
                stops = sp.fill.gradient_stops
                c0, c1 = grad[0], grad[-1]
                stops[0].position = 0.0
                stops[0].color.rgb = RGBColor.from_string(c0)
                if len(stops) > 1:
                    stops[1].position = 1.0
                    stops[1].color.rgb = RGBColor.from_string(c1)
            # 角度必须翻符号：python-pptx 的 gradient_angle 是"数学逆时针"，而 spec 用的是
            # "PPT 语义顺时针"（90 = 从上到下，stop0 在上）。实测（400×400 纯黑白两块）：
            #   spec 90 直接写 90  → 顶=236/底=70  → stop0 跑到下面（反向）
            #   spec 90 写 270     → 顶=100/底=242 → stop0 在上面（正确）
            # 0 和 180 是自逆的，所以以前只写 0 时看不出来；全库 21 个渐变容器里 15 个是 90°，
            # 也就是 71% 的垂直渐变一直是反的（2026-11-07 审计）。
            ang = float(el.get("gradient_angle", 90))
            sp.fill.gradient_angle = (360.0 - ang) % 360.0
        elif str(el.get("fill", "")).lower() == "none":
            # 只画描边的环（卡片内亮边用它叠）：显式 "none" 才生效，不改变默认白底
            sp.fill.background()
        else:
            fill = el.get("fill", "#FFFFFF")
            sp.fill.solid()
            sp.fill.fore_color.rgb = _rgb(fill, f"{el['id']} 的 fill")
        if el.get("line"):
            set_line(sp, el["line"], str(el["id"]))
        else:
            sp.line.fill.background()
        if st in (MSO_SHAPE.RIGHT_ARROW, MSO_SHAPE.LEFT_ARROW):
            # 箭头两档调整：adj[0]=尾高/总高，adj[1]=头长/总宽（DrawingML 原生语义）
            adj = el.get("adj")
            if adj:
                try:
                    sp.adjustments[0] = float(adj[0])
                    sp.adjustments[1] = float(adj[1])
                except Exception:
                    pass
        if el.get("shape") != "freeform" and st == MSO_SHAPE.ROUNDED_RECTANGLE:
            try:
                if el.get("capsule"):
                    sp.adjustments[0] = 0.5
                elif el.get("radius_px"):
                    w = x1 - x0
                    h = y1 - y0
                    sp.adjustments[0] = min(0.5, float(el["radius_px"]) / max(min(w, h), 1))
                else:
                    sp.adjustments[0] = 0.08
            except Exception:
                pass
        sp.name = _shape_name(el)      # A：有 name 用 name，否则 id
        if sp.name != str(el["id"]):
            _set_id_attr(sp, str(el["id"]))
        apply_effects(sp, el)
        apply_rotation(sp, el)
        return sp

    def add_text(el):
        x0, y0, x1, y1 = el["bbox"]
        txt = el["text"]
        lines = max(1, txt.count("\n") + 1)
        size_pt = float(el["size_pt"])      # 必有：进门已校验，缺就 REFUSED（不再默认 18pt）
        # 框高稍微给足，避免 PowerPoint 判定溢出；anchor 居中后视觉中心不变
        line_px = size_pt * dpi / 72.0 * LINE_EM
        h_px = max(y1 - y0, line_px * lines) + size_pt * dpi / 72.0 * 0.34
        cy = (y0 + y1) / 2
        top = cy - h_px / 2
        # 宽度：墨迹宽 + 左右各 3% 余量，防止换行
        w_px = (x1 - x0) * 1.06 + size_pt * dpi / 72.0 * 0.2
        # 对齐：spec 的 align 决定段落对齐；居中时框必须绕墨迹中心对称展开，
        # 否则 x0 固定 + 框变宽会把整段文字推向右边（2.0 之前这里写死 LEFT，居中的卡片文字全变左齐）
        align = str(el.get("align", "left")).lower()
        left_px = ((x0 + x1) / 2 - w_px / 2) if align == "center" else x0
        box = slide.shapes.add_textbox(P(left_px), P(top), P(w_px), P(h_px))
        box.name = _shape_name(el)     # A：三处写入点同一条规则（形状 / 文字 / 图片）
        if box.name != str(el["id"]):
            _set_id_attr(box, str(el["id"]))
        box.shadow.inherit = False      # 主题阴影会在文字后面糊出一层，必须在三类元素上统一关
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = 0
        tf.margin_right = 0
        tf.margin_top = 0
        tf.margin_bottom = 0
        try:
            tf.auto_size = None
        except Exception:
            pass
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER if str(el.get("align", "left")).lower() == "center" else PP_ALIGN.LEFT
        p.line_spacing = float(el.get("line_spacing", 1.0) or 1.0)
        color = _hex6(el.get("color", "#000000"), f"{el['id']} 的 color")
        for i, line in enumerate(txt.split("\n")):
            para = p if i == 0 else tf.add_paragraph()
            if i:
                para.alignment = p.alignment
                para.line_spacing = float(el.get("line_spacing", 1.0) or 1.0)
            run = para.add_run()
            run.text = line
            weight = style.resolve_weight(el.get("weight", el.get("bold", False)))
            rPr = _style_run(run, size_pt, weight, color)
            # 字距：a:rPr/@spc，单位 1/100 pt。只改字间，不改字形——所以它能吸收
            # "宽度残余"而不牺牲字号
            # 字距照用 spec 里的值：实测关掉它会让整块墨迹覆盖从 0.914 掉到 0.688
            # （字距不只是"凑宽"，它让每个字对准参考位置）。harmony 档省的是收敛循环，
            # 不是把字距清零——这条我一开始判断错了。
            spc = float(el.get("letter_spacing_pt", 0) or 0)
            if abs(spc) > 0.005:
                rPr.set("spc", str(int(round(spc * 100))))
        apply_rotation(box, el)
        return box

    def _resolve_asset(src: str | None, eid: str) -> Path | None:
        """src 的解析顺序固定，不依赖调用时的 CWD。

        事故背景：spec 里两种写法并存——`assets/x.png`（相对 page/）和
        `page\\assets\\x.png`（相对项目根）。老代码先试 CWD，再试 spec 同级，
        于是「从哪个目录调 build」会改变结果：同一个 spec 在项目根跑能出图，
        换到 page/ 里跑就 MISS，而 MISS 只是打印，不是失败。
        现在改为「只认确定的基准」，CWD 放到最后并且打 NOTE。
        """
        bases: list[Path] = []
        if a.assets:
            bases.append(Path(a.assets))
        sp_dir = Path(a.spec).resolve().parent
        bases += [sp_dir, sp_dir.parent]
        if src:
            p = Path(src)
            if p.is_absolute():
                if p.exists():
                    return p
            for b in bases:
                cand = b / src
                if cand.exists():
                    return cand
            if p.exists():                      # 仅作最后兜底，且明确告警
                print(f"  NOTE {eid} 的 src={src!r} 只在当前工作目录下存在，"
                      f"换目录跑就会 MISS——请改成相对 spec 的路径")
                return p
            return None
        # 没给 src：按 id 找 assets/<id>.png。
        # 事故记录：extract 重跑会把 src 清成 null，build 过去在这里静默 return None，
        # 结果 7 张图全丢、shapes 54→47，而 look 的 image_present 只数"有 src 的 assets"
        # → 两边都是 0，报"全过"。一个错误同时绕过两条检查，必须在源头堵。
        for b in bases:
            cand = b / "assets" / f"{eid}.png"
            if cand.exists():
                print(f"  FALLBACK {eid} -> assets/{eid}.png（spec 缺 src，按 id 兜底）")
                return cand
        return None

    def add_image(el):
        path = _resolve_asset(el.get("src"), el["id"])
        if path is None:
            print(f"  MISS {el['id']}（spec 无 src，且 assets/{el['id']}.png 不存在）")
            return None
        x0, y0, x1, y1 = el["bbox"]
        pic = slide.shapes.add_picture(str(path), P(x0), P(y0), P(x1 - x0), P(y1 - y0))
        pic.name = _shape_name(el)     # A：同上；id 落 cNvPr@title 供 presence / editability 追溯
        if pic.name != str(el["id"]):
            _set_id_attr(pic, str(el["id"]))
        pic.shadow.inherit = False
        apply_rotation(pic, el)
        return pic

    # 槽位闸：环境与内容是两栏。
    #   环境栏（slide background）只收 solid / gradient；
    #   内容栏（shapes）不收全页容器（>90% 画布＝把环境画进内容层）。
    canvas_area = float(cv["w_px"]) * float(cv["h_px"])
    for el in spec.get("containers", []):
        x0, y0, x1, y1 = el["bbox"]
        frac = (x1 - x0) * (y1 - y0) / canvas_area if canvas_area else 0.0
        if frac > 0.90:
            raise SystemExit(
                f"REFUSED: 容器 {el['id']} 占画布 {frac:.0%}——这是把环境画进内容层。\n"
                f"  环境（底色）只能走 spec.background 的 solid/gradient 槽位，不能做成 shapes 里的全页形状：\n"
                f"  形状会占 z 序、被当成内容选中、换主题要逐个删。\n"
                f'  改法：删掉这个容器，改用 {{"type":"solid","color":"#XXXXXX"}}。')

    # 环境（slide background）先落地，内容层序见下方 draw 排序；两件事分开写，别混成一句注释
    bg = spec.get("background")
    if bg:
        # 环境栏允许的形态。判据是「可参数化描述」而不是面积：
        #   纯色   = 3 个字节，逐像素可预测；
        #   渐变   = 3~5 个参数，逐像素也可预测（2.22 起支持；以前写"渐变不占优"，
        #            那条结论是在 gradient_angle 反号 bug 还在时测的，已作废）；
        #   图片底 = N×M×3 字节像素，无参数 → 整页截图当底就是这条路径，**永久禁止**。
        btype = str(bg.get("type", "solid")).lower()
        if btype not in ("solid", "gradient"):
            raise SystemExit(
                f"REFUSED: background.type={btype!r} 不接受。环境栏只收 solid / gradient。\n"
                f"  图片底 = 整页截图当底（要防的就是这个，永久禁止）；\n"
                f'  gradient 写 {{"type":"gradient","gradient":["#EEF7FE","#D2E9FC","#F2FAFE"],'
                f'"gradient_pos":[0,0.5,1],"gradient_angle":90}}。')
        if btype == "gradient":
            cols = [_hex6(c, "background.gradient") for c in (bg.get("gradient") or [])]
            if len(cols) < 2:
                raise SystemExit("REFUSED: background.gradient 至少要 2 档色")
            sld_bg = slide.background
            sld_bg.fill.gradient()
            # sld_bg._element 是 p:cSld，真正的填充挂在 p:bg/p:bgPr 下
            bg_el = sld_bg._element.find(qn("p:bg"))
            bgPr = bg_el.find(qn("p:bgPr")) if bg_el is not None else None
            gf = bgPr.find(qn("a:gradFill")) if bgPr is not None else None
            if gf is None:
                raise SystemExit("REFUSED: 背景渐变的 a:gradFill 没建出来（python-pptx 版本？）")
            _write_gradfill(gf, cols, bg.get("gradient_pos"),
                            float(bg.get("gradient_angle", 90)))
        if btype == "solid":
            # 背景色这条人话校验原有（2026-11-08 P2c 核实：它早就在，不动它）。
            hexv = str(bg.get("color", "")).lstrip("#").upper()
            if len(hexv) != 6 or any(c not in "0123456789ABCDEF" for c in hexv):
                raise SystemExit(f"REFUSED: background.color={bg.get('color')!r} 不是 6 位十六进制")
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = RGBColor.from_string(hexv)
    # 层序：默认仍是「容器 100 → 图片 200 → 文字 300」，与历史产物逐字节一致；
    # 元素可显式给 z_index 覆盖所在层的默认值。原来写死"文字永远最后画"，
    # 于是"卡片压住文字""图片压住卡片"这类设计根本画不出来（2026-11-07 审计）。
    # 同值时按「层序 → 声明顺序」排序，不赌 sort 的稳定性。
    LAYER_Z = {"containers": 100.0, "assets": 200.0, "elements": 300.0}
    draw = []
    for sec in ("containers", "assets", "elements"):
        for i, el in enumerate(spec.get(sec, [])):
            # 只认显式标记：以前用 "review 里含『符号』" 这个中文子串当条件，
            # 等于用文案决定行为——改一句话就静默丢元素，且没有任何账本。
            if sec == "elements" and el.get("skip") is True:
                continue
            z = el.get("z_index")
            draw.append((LAYER_Z[sec] if z is None else float(z), LAYER_Z[sec], i, sec, el))
    draw.sort(key=lambda t: (t[0], t[1], t[2]))
    skipped = [el.get("id") for el in spec.get("elements", []) if el.get("skip") is True]
    for _z, _layer, _i, sec, el in draw:
        if sec == "containers":
            add_shape(el)
        elif sec == "assets":
            add_image(el)
        else:
            add_text(el)
    if skipped:
        print(f"  SKIP {len(skipped)} 个元素（spec.skip=true）：{', '.join(str(x) for x in skipped)}")

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    prs.save(a.out)
    n = len(slide.shapes)
    # texts 只数**真的画上去的**：以前按 _symbol 估，skip 掉的元素仍被算进去，统计会说谎
    print(f"saved {a.out}  shapes={n}  texts={len([e for e in spec.get('elements', []) if e.get('skip') is not True])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
