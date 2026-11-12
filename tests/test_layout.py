"""布局器的契约测试。

它是"做的时候做对"的那条路的入口，所以钉的不是"能跑"，而是四条**规则**：
1. 卡片位置由规范算：等宽、间距 = gutter、左右 = margin（不是从图上量的）；
2. 同级同大：同一角色的文字只能有一个字号；
3. 字色来自规范，改调色板就跟着变（不是布局器里写死）；
4. 放不下只许往下降，绝不放大；规范缺字段直接报错，不给默认值。
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))

import layout  # noqa: E402

VISUAL = {
    "palette": {"white": "#FFFFFF", "ink": "#1A1A1A", "navy": "#03288F", "accent": "#0A6AFA"},
    "type": {
        "scale": [16, 18, 20, 24, 28, 32],
        "roles": {
            "title": {"pct": 0.086, "weight": "bold", "color": "navy"},
            "label": {"pct": 0.052, "weight": "bold", "color": "white"},
            "body": {"pct": 0.037, "weight": "bold", "color": "navy"},
            "nav": {"pct": 0.032, "weight": "regular", "color": "navy"},
            "nav_active": {"pct": 0.037, "weight": "bold", "color": "white"},
            "footer": {"pct": 0.0425, "weight": "regular", "color": "ink"},
        },
    },
    "grid": {"margin": {"left": 40, "right": 40, "top": 140, "bottom": 73},
             "columns": 3, "gutter": 18, "card_padding": 32},
    "global": {"nav": {"x0": 40, "y0": 23, "h": 80}, "footer": {"y0": 868}},
    "style": {"card": {"fill": "white", "radius": 30},
              "tag": {"blue": ["accent"], "violet": ["accent"]}},
    "radius": {},
}
CANVAS = {"w_px": 1672, "h_px": 941, "dpi": 125.4}
CONTENT = {
    "nav": {"labels": ["一", "二", "三"], "active": 0},
    "title": "标题",
    "cards": [
        {"label": "甲", "bullets": ["短", "也短"]},
        {"label": "乙", "bullets": ["稍长一点的一句话", "再长一点的一句话内容"]},
        {"label": "丙", "bullets": ["第三条", "第四条"]},
    ],
    "footer": ["P01", "页名"],
}


def build(visual=None, content=None):
    th = layout.theme_from(visual or VISUAL, CANVAS)
    return layout.card_grid_page(content or CONTENT, th)


class GridTest(unittest.TestCase):
    def setUp(self):
        self.spec = build()
        self.cards = [c for c in self.spec["containers"] if c["id"].startswith("c_card")]

    def test_cards_are_equal_and_on_grid(self):
        """三张卡：等宽、间距 = gutter、左右离边距 = margin。位置是算的，不是量的。"""
        self.assertEqual(len(self.cards), 3)
        widths = [round(c["bbox"][2] - c["bbox"][0], 2) for c in self.cards]
        self.assertEqual(len(set(widths)), 1, widths)
        gut = VISUAL["grid"]["gutter"]
        for a, b in zip(self.cards, self.cards[1:]):
            # 允许 0.2px：每张卡的 x 各自四舍五入到 1 位小数，会累积出 0.1 的差
            self.assertAlmostEqual(b["bbox"][0] - a["bbox"][2], gut, delta=0.2)
        self.assertAlmostEqual(self.cards[0]["bbox"][0], VISUAL["grid"]["margin"]["left"], places=1)
        self.assertAlmostEqual(self.cards[-1]["bbox"][2],
                               CANVAS["w_px"] - VISUAL["grid"]["margin"]["right"], places=1)


class TypographyTest(unittest.TestCase):
    def test_same_role_same_size(self):
        """同一角色的文字只能有一个字号——这条是这个项目一直在治的病。"""
        spec = build()
        bullets = [e["size_pt"] for e in spec["elements"] if e["id"].startswith("t_b")]
        self.assertEqual(len(set(bullets)), 1, bullets)
        labels = [e["size_pt"] for e in spec["elements"] if e["id"].startswith("t_lab")]
        self.assertEqual(len(set(labels)), 1, labels)
        navs = [e["size_pt"] for e in spec["elements"] if e["id"] in ("t2", "t3")]
        self.assertEqual(len(set(navs)), 1, navs)

    def test_sizes_come_from_scale(self):
        spec = build()
        for e in spec["elements"]:
            if e["id"] in ("t_title", "t_lab1", "t_b1", "t1", "t_foot1"):
                self.assertIn(e["size_pt"], [float(s) for s in VISUAL["type"]["scale"]]
                              + [round(0.086 * CANVAS["h_px"] * 72 / CANVAS["dpi"], 1)],
                              f'{e["id"]}={e["size_pt"]}')

    def test_body_color_follows_theme(self):
        """字色来自规范：把 body 的颜色换掉，输出必须跟着变（布局器里不许写死）。"""
        spec = build()
        before = [e["color"] for e in spec["elements"] if e["id"].startswith("t_b")][0]
        self.assertEqual(before, VISUAL["palette"]["navy"])

        v2 = json.loads(json.dumps(VISUAL))
        v2["type"]["roles"]["body"]["color"] = "ink"
        after = [e["color"] for e in build(visual=v2)["elements"] if e["id"].startswith("t_b")][0]
        self.assertEqual(after, VISUAL["palette"]["ink"])

    def test_fit_only_goes_down(self):
        """放不下时只能往下降，绝不能放大到超过规范给的档。"""
        start = 28.0
        got = layout.fit_down("稍微长一点的一句话内容", 200.0, 2,
                              [16.0, 18.0, 20.0, 24.0, 28.0], 125.4, start)
        self.assertLessEqual(got, start)
        self.assertIn(got, [16.0, 18.0, 20.0, 24.0, 28.0])

    def test_impossible_content_raises(self):
        """最小档也放不下时抛错，而不是静默缩到 12pt 或让它挤出去。"""
        with self.assertRaises(layout.LayoutError):
            layout.fit_down("很长" * 40, 200.0, 2, [16.0, 18.0, 20.0], 125.4, 20.0)

    def test_missing_role_raises(self):
        """规范缺 pct 就报错，不给默认值（默认值 = 第二套规则）。"""
        v = json.loads(json.dumps(VISUAL))
        del v["type"]["roles"]["body"]["pct"]
        with self.assertRaises(ValueError):
            build(visual=v)

    def test_missing_color_raises(self):
        v = json.loads(json.dumps(VISUAL))
        del v["type"]["roles"]["label"]["color"]
        with self.assertRaises(ValueError):
            build(visual=v)


class WrapTest(unittest.TestCase):
    """断行规则：禁则（标点不打头）+ 语义优先（断在标点/连接词后）。"""

    def test_no_punctuation_at_line_start(self):
        """收尾标点不许出现在行首：原稿断出过「能说出宫颈癌“三阶梯 / ”诊断流程」。"""
        lines = layout.wrap_lines("能说出宫颈癌“三阶梯”诊断流程", 20.0, 125.4, 357.7, "bold")
        for ln in lines:
            self.assertNotIn(ln[0], layout.NO_LINE_START, lines)

    def test_break_after_punctuation_when_possible(self):
        """有标点可断时，优先断在标点后——而不是把两行长度硬拉平。"""
        lines = layout.wrap_lines("树立早筛查，早诊断、早治疗意识", 20.0, 125.4, 357.7, "bold")
        self.assertEqual(lines[0], "树立早筛查，", lines)

    def test_never_ends_with_open_quote(self):
        for text in ("能说出宫颈癌“三阶梯”诊断流程", "掌握临床表现与诊断路径"):
            for ln in layout.wrap_lines(text, 20.0, 125.4, 357.7, "bold"):
                self.assertNotIn(ln[-1], layout.NO_LINE_END, (text, ln))


if __name__ == "__main__":
    unittest.main()
