"""裸值闸门的契约测试（P3.2）。

这个闸门存在的前提只有一句：**它必须读 raw**。读展开视图的话，token 早被换成字面值，
它永远数到 0——那就不是闸门，是自证（D11）。

四条：数得到裸值、token 不算裸值、glow 半径不算圆角、超上限即 FAIL。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "delivery"))

import spec_lint  # noqa: E402


def _page(tmp: Path, elements: list[dict]) -> Path:
    d = tmp / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.json").write_text(json.dumps(
        {"canvas": {"w_px": 100, "h_px": 100}, "elements": elements},
        ensure_ascii=False), encoding="utf-8")
    (d / "visual.json").write_text(json.dumps(
        {"palette": {"ink": "#1A1A1A"}, "type": {"size": {"t_s": 20.8}},
         "radius": {"card": 30}}, ensure_ascii=False), encoding="utf-8")
    return d


def _el(eid="t1", **kw):
    base = {"id": eid, "type": "text", "text": "x", "bbox": [0, 0, 10, 10],
            "size_pt": 20.8, "color": "#1A1A1A"}
    base.update(kw)
    return base


class BareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _count(self, elements):
        d = _page(self.root, elements)
        view = spec_lint.specmerge.load_merged(d / "spec.json", quiet=True)
        return spec_lint.bare_literals(view)

    def test_counts_color_and_size(self):
        lits = self._count([_el()])
        kinds = sorted({x["kind"] for x in lits})
        self.assertEqual(kinds, ["字号", "颜色"], lits)
        self.assertEqual(len(lits), 2)

    def test_token_is_not_a_literal(self):
        """token 是引用，不是裸值。数到它，等于把"正在改"的东西判成"还没改"。"""
        lits = self._count([_el(color="{palette.ink}", size_pt="{type.size.t_s}")])
        self.assertEqual(lits, [], lits)

    def test_glow_radius_is_not_corner_radius(self):
        """`glow.radius_px` 是柔光半径，和"卡片圆角"不是一个量；混进圆角会逼人把柔光也 token 化。"""
        lits = self._count([_el(color="{palette.ink}", size_pt="{type.size.t_s}",
                               glow={"color": "{palette.ink}", "radius_px": 6, "alpha": 0.2})])
        self.assertEqual([x["kind"] for x in lits], [], lits)

    def test_corner_radius_is_counted(self):
        lits = self._count([_el(radius_px=30, color="{palette.ink}", size_pt="{type.size.t_s}")])
        self.assertEqual([x["kind"] for x in lits], ["圆角"], lits)

    def test_over_allow_fails(self):
        """超过上限必须 FAIL（rc=1）：这是闸门，不是统计。"""
        d = _page(self.root, [_el(f"t{i}") for i in range(5)])   # 5 块 × 2 条 = 10 条
        self.assertEqual(spec_lint.bare_report(d / "spec.json", 8), 1)
        self.assertEqual(spec_lint.bare_report(d / "spec.json", 12), 0)

    def test_raw_view_is_used(self):
        """守住 D11：如果哪天有人把 bare_report 改成读展开视图，这条会红。"""
        d = _page(self.root, [_el()])
        self.assertEqual(len(spec_lint.bare_literals(
            spec_lint.specmerge.load_merged(d / "spec.json", quiet=True, expand=True))), 2,
            "展开视图当然也是 2 条——但换成 token 页之后就会变 0；下面那条才是关键")
        d2 = _page(self.root / "q", [_el(color="{palette.ink}", size_pt="{type.size.t_s}")])
        self.assertEqual(spec_lint.bare_report(d2 / "spec.json", 8), 0)
        self.assertEqual(len(spec_lint.bare_literals(
            spec_lint.specmerge.load_merged(d2 / "spec.json", quiet=True, expand=True))), 2,
            "展开视图会把 2 条 token 数成裸值——所以闸门必须读 raw")


class RegressionTest(unittest.TestCase):
    """两条都是复核打回来的真事故，钉死它们不许复发。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gradient_list_colors_are_counted(self):
        """`gradient: ["#A","#B"]` 是裸值。第一版 walk 的 list 分支只递归不判定，
        于是 9 页共 201 处渐变色一处都没数到，46 页报"裸值 0"。（复核实测）"""
        d = _page(self.root, [_el(gradient=["#FFFFFF", "#1A1A1A"], color="{palette.ink}",
                                  size_pt="{type.size.t_s}")])
        view = spec_lint.specmerge.load_merged(d / "spec.json", quiet=True)
        lits = spec_lint.bare_literals(view)
        cols = [x for x in lits if x["kind"] == "颜色"]
        self.assertEqual(len(cols), 2, lits)
        self.assertTrue(all("gradient[" in x["where"] for x in cols), cols)

    def test_numeric_checks_still_run_on_token_page(self):
        """token 页的 size_pt 在 raw 里是字符串 → `size_of()` 返回 None → measurable 空 →
        溢出/出界/压图**一次都没跑却打印 PASS**。这条用例造一个明显出界的元素：
        修好之后必须 FAIL；退回旧行为就会 PASS。"""
        d = _page(self.root, [_el(bbox=[200, 200, 300, 300],
                                  color="{palette.ink}", size_pt="{type.size.t_s}")])
        import subprocess
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "delivery" / "spec_lint.py"),
             "--spec", str(d / "spec.json")],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("出界", out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
