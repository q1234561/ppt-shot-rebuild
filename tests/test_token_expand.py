"""token 双视图的契约测试（P3.1）。

四条，每条都在钉一个会被误解的边界：
1. **默认不展开**（raw）：闸门必须看得见 `{palette.ink}` 这种原文，否则"裸值 = 0"是自证（D11）；
2. **只替换整串**：`"{size}/2"` 这种拼接不替换——一混拼接，没人能静态知道值是多少；
3. **未知 token 报错**：不原样保留（保留会让 build 拿到字符串，在深处炸出莫名其妙的错）；
4. **不改原文件**：展开只动返回值，spec.json 一个字节都不能变。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))

import specmerge  # noqa: E402


def _page(tmp: Path, spec: dict, field: str = "palette.white") -> Path:
    d = tmp / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    (d / "visual.json").write_text(json.dumps(
        {"palette": {"white": "#FFFFFF", "ink": "#1A1A1A"},
         "type": {"size": {"t_s": 20.8}}}, ensure_ascii=False), encoding="utf-8")
    return d


def _spec(**el):
    base = {"id": "t1", "type": "text", "text": "x", "bbox": [0, 0, 10, 10],
            "size_pt": "{type.size.t_s}", "color": "{palette.ink}"}
    base.update(el)
    return {"canvas": {"w_px": 100, "h_px": 100}, "elements": [base]}


class DualViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_raw(self):
        d = _page(self.root, _spec())
        raw = specmerge.load_merged(d / "spec.json", quiet=True)
        self.assertEqual(raw["elements"][0]["color"], "{palette.ink}")
        self.assertEqual(raw["elements"][0]["size_pt"], "{type.size.t_s}")

    def test_expand_replaces_values(self):
        d = _page(self.root, _spec())
        exp = specmerge.load_merged(d / "spec.json", quiet=True, expand=True)
        self.assertEqual(exp["elements"][0]["color"], "#1A1A1A")
        self.assertEqual(exp["elements"][0]["size_pt"], 20.8)

    def test_partial_token_is_not_expanded(self):
        """拼接串不替换：它既不是裸值也不是纯 token，静态不可知，必须留在 raw 里暴露出来。"""
        d = _page(self.root, _spec(size_pt="{type.size.t_s}/2"))
        exp = specmerge.load_merged(d / "spec.json", quiet=True, expand=True)
        self.assertEqual(exp["elements"][0]["size_pt"], "{type.size.t_s}/2")

    def test_unknown_token_raises(self):
        d = _page(self.root, _spec(color="{palette.nope}"))
        with self.assertRaises(ValueError) as cm:
            specmerge.load_merged(d / "spec.json", quiet=True, expand=True)
        self.assertIn("palette.nope", str(cm.exception))

    def test_missing_visual_raises(self):
        d = _page(self.root, _spec())
        (d / "visual.json").unlink()
        with self.assertRaises(FileNotFoundError):
            specmerge.load_merged(d / "spec.json", quiet=True, expand=True)

    def test_original_file_untouched(self):
        d = _page(self.root, _spec())
        before = (d / "spec.json").read_bytes()
        specmerge.load_merged(d / "spec.json", quiet=True, expand=True)
        self.assertEqual((d / "spec.json").read_bytes(), before, "展开不许写回 spec")

    def test_expand_is_idempotent(self):
        """展开两次 == 展开一次（值里没有花括号，第二次无事可做）。"""
        d = _page(self.root, _spec())
        once = specmerge.load_merged(d / "spec.json", quiet=True, expand=True)
        visual = specmerge.load_visual(None, d)
        twice = specmerge.expand_tokens(once, visual)
        self.assertEqual(once, twice)


class RealSeriesTest(unittest.TestCase):
    """真实页：展开前后叶子数一致、raw 里确实还剩 token。"""

    SERIES = Path(os.environ.get("PPT_SERIES_ROOT") or "D:/WorkBuddy工作区/ppt制作")
    PAGE = "46-独立-P04教学目标-1109"

    @unittest.skipUnless((SERIES / "spec.json").exists() or SERIES.is_dir(), "整套目录不存在")
    def test_pilot_page_expands_cleanly(self):
        sp = self.SERIES / self.PAGE / "spec.json"
        if not sp.exists():
            self.skipTest("缺 spec.json")
        raw = specmerge.load_merged(sp, quiet=True)
        exp = specmerge.load_merged(sp, quiet=True, expand=True)

        def leaves(n, out):
            if isinstance(n, dict):
                for v in n.values():
                    leaves(v, out)
            elif isinstance(n, list):
                for v in n:
                    leaves(v, out)
            else:
                out.append(n)

        a, b = [], []
        leaves(raw, a)
        leaves(exp, b)
        self.assertEqual(len(a), len(b), "展开改变了结构")
        self.assertGreater(sum(1 for x in a if isinstance(x, str) and x.startswith("{")), 0,
                           "这一页应该已经有 token")
        self.assertEqual(sum(1 for x in b if isinstance(x, str) and x.startswith("{")), 0,
                         "展开后不该剩 token")


class PlanDepTest(unittest.TestCase):
    """plan 的依赖必须含常量源指纹：改了 `visual.json` 而 plan 报告还是绿的，就是假绿。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sys.path.insert(0, str(ROOT / "scripts" / "delivery"))
        import inventory
        self.inventory = inventory

    def tearDown(self):
        self.tmp.cleanup()

    def test_visual_md5_changes_dep(self):
        d = _page(self.root, _spec())
        inv = {"items": []}
        before = self.inventory.plan_deps(d / "spec.json", inv)
        self.assertIn("visual_md5", before.get("sources", {}), before.get("sources"))

        v = json.loads((d / "visual.json").read_text(encoding="utf-8"))
        v["palette"]["ink"] = "#000000"          # 只改常量源，spec 一个字不动
        (d / "visual.json").write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
        after = self.inventory.plan_deps(d / "spec.json", inv)

        self.assertNotEqual(before["sources"]["visual_md5"], after["sources"]["visual_md5"])
        self.assertNotEqual(before, after, "改了常量源，plan 依赖必须变（否则读到旧报告=假绿）")


if __name__ == "__main__":
    unittest.main()
