"""`impose` 的契约测试——它是**唯一的写入口**（typescale / deckalign --apply 已删除）。

五条，都对着踩过的坑：
1. 字号吸附到最近档，且**不改入参**（返回新 spec）；
2. 超过吸附上限 → 报例外，不硬拽；
3. 导航条整组平移：条、胶囊、分隔线、条内标签一起走，条外的东西不动；
4. 页脚只动 y，超过护栏就报例外（有的页页脚本来就靠上）；
5. 幂等：连跑两次，第二次 0 改动。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))

import impose  # noqa: E402

C = {
    "scale": [16.0, 17.0, 18.0, 20.0],
    "cap_pt": 33.0,
    "max_snap": 2.0,
    "nav": {"x0": 40, "y0": 23},
    "footer": {"y0": 868, "max_move_px": 20},
}
DECL = {"nav": ["nav"], "nav_pill": ["pill"], "div": ["sep"],
        "deco_l": [], "deco_r": [], "footer": ["t_pg", "t_name"]}


def _spec(nav=(60, 28, 1600, 108), pill=(70, 30, 300, 100), sep=(500, 30, 500, 100),
          label=(120, 40, 260, 90), body_size=16.4, title_size=46.0,
          foot=(48, 880, 110, 910), outside=(1000, 400, 1200, 500)):
    return {
        "canvas": {"w_px": 1672, "h_px": 941},
        "containers": [
            {"id": "nav", "bbox": list(nav)},
            {"id": "pill", "bbox": list(pill)},
            {"id": "sep", "bbox": list(sep)},
        ],
        "elements": [
            {"id": "t1", "bbox": list(label), "size_pt": body_size, "color": "#000000"},
            {"id": "t_title", "bbox": [100, 150, 600, 200], "size_pt": title_size, "color": "#000000"},
            {"id": "t_pg", "bbox": list(foot), "size_pt": 16.0, "color": "#000000"},
            {"id": "t_name", "bbox": [120, foot[1], 300, foot[3]], "size_pt": 16.0, "color": "#000000"},
            {"id": "bodycard", "bbox": list(outside), "size_pt": body_size, "color": "#000000"},
        ],
    }


class SizeTest(unittest.TestCase):
    def test_snaps_and_does_not_mutate_input(self):
        spec = _spec(body_size=16.4)
        new, changes, _ = impose.normalize(spec, DECL, C)
        self.assertEqual(spec["elements"][0]["size_pt"], 16.4, "入参不许被改")
        self.assertEqual(new["elements"][0]["size_pt"], 16.0)
        self.assertTrue(any(c["kind"] == "size" for c in changes))

    def test_outlier_is_reported_not_forced(self):
        spec = _spec(body_size=14.0)      # 最近的档 16，差 2.0 —— 正好在护栏上
        new, changes, notes = impose.normalize(
            spec, DECL, {**C, "max_snap": 1.9})
        self.assertEqual(new["elements"][0]["size_pt"], 14.0)
        self.assertTrue(any("不吸" in n for n in notes), notes)

    def test_title_untouched(self):
        spec = _spec(title_size=46.0)
        new, changes, notes = impose.normalize(spec, DECL, C)
        self.assertEqual(new["elements"][1]["size_pt"], 46.0)
        self.assertTrue(any("标题级" in n for n in notes), notes)


class GeometryTest(unittest.TestCase):
    def test_nav_group_moves_together(self):
        spec = _spec()
        new, changes, _ = impose.normalize(spec, DECL, C)
        bb = {x["id"]: x["bbox"] for seg in ("containers", "elements") for x in new[seg]}
        # nav 从 (60,28) 移到 (40,23)：dx=-20, dy=-5
        self.assertEqual(bb["nav"][:2], [40, 23])
        self.assertEqual(bb["pill"][0], 50.0)       # 70 - 20
        self.assertEqual(bb["sep"][1], 25.0)        # 30 - 5
        self.assertEqual(bb["t1"][0], 100.0)        # 条里的标签也跟着
        self.assertEqual(bb["bodycard"][0], 1000.0, "条外的东西不许动")

    def test_footer_moves_y_only(self):
        spec = _spec()
        new, _, _ = impose.normalize(spec, DECL, C)
        bb = {x["id"]: x["bbox"] for x in new["elements"]}
        self.assertEqual(bb["t_pg"][0], 48.0, "x 不动")
        self.assertEqual(bb["t_pg"][1], 868.0, "y 吸到规范")

    def test_footer_beyond_guard_is_exception(self):
        """49 页实测页脚比别人高 40px：护栏 20px → 不硬动，报例外。"""
        spec = _spec(foot=(48, 828, 110, 858))
        new, _, notes = impose.normalize(spec, DECL, C)
        bb = {x["id"]: x["bbox"] for x in new["elements"]}
        self.assertEqual(bb["t_pg"][1], 828.0)
        self.assertTrue(any("页脚" in n for n in notes), notes)


class IdempotentTest(unittest.TestCase):
    def test_second_run_is_noop(self):
        spec = _spec()
        once, ch1, _ = impose.normalize(spec, DECL, C)
        twice, ch2, _ = impose.normalize(once, DECL, C)
        self.assertGreater(len(ch1), 0)
        self.assertEqual(ch2, [], "第二次必须 0 改动")
        self.assertEqual(once, twice)

    def test_no_visual_scale_raises(self):
        """规范缺字段就报错：给默认值等于种下第二套规则。"""
        with self.assertRaises(ValueError):
            impose.consts({"type": {}, "global": {"nav": {"x0": 40, "y0": 23}}})


class OverflowScaleTest(unittest.TestCase):
    """溢出求解也写字号——它必须服从同一张档位表，否则就是第二套规则。"""

    def setUp(self):
        import sys as _s
        _s.path.insert(0, str(ROOT / "scripts" / "delivery"))
        import cycle
        self.cycle = cycle

    def test_overflow_only_steps_down_to_scale(self):
        """字挤出卡片时：解出的字号必须落在档位上，且不高于上限（否则原地复发）。"""
        view = {
            "canvas": {"w_px": 400, "h_px": 300, "dpi": 96.0},
            "elements": [{"id": "t1", "bbox": [10, 10, 110, 40], "size_pt": 24.0,
                          "font": "微软雅黑", "text": "一段很长很长的文字需要换行很多次才能放得下"}],
        }
        rep = self.cycle.solve_overflow(view, 96.0, scale=[16.0, 17.0, 18.0, 20.0])
        for eid, pt in (rep.get("sizes") or {}).items():
            self.assertIn(pt, [16.0, 17.0, 18.0, 20.0], f"{eid} 解出 {pt}pt，不在档位上")

    def test_no_scale_keeps_legacy_behaviour(self):
        view = {
            "canvas": {"w_px": 400, "h_px": 300, "dpi": 96.0},
            "elements": [{"id": "t1", "bbox": [10, 10, 110, 40], "size_pt": 24.0,
                          "font": "微软雅黑", "text": "一段很长很长的文字需要换行很多次才能放得下"}],
        }
        rep = self.cycle.solve_overflow(view, 96.0)
        self.assertIsInstance(rep.get("sizes"), dict)


if __name__ == "__main__":
    unittest.main()
