"""页型聚类的确定性测试。

**这个文件存在的唯一理由**：第一版 `cluster()` 是"逐页塞进第一个装得下的簇"，
同一个相似度矩阵、换个页序就得到不同的簇（实测 [1,1,7] ↔ [2,2,5]），而且 40↔45 相似度 0.794 ≥ 阈值
却被分进不同簇——它连自己 docstring 里写的"完全连接"都没做到。页序是实现细节，不能改变结论。

三条：顺序无关、完全连接不串链、阈值单调。
"""
import os
import sys
import unittest
from itertools import permutations
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))

import pagetype  # noqa: E402


def _sim(pairs):
    d = {}
    for (a, b), v in pairs.items():
        d[(a, b)] = d[(b, a)] = v
    return d


def _all_same(sim, pages):
    return all(sim.get((a, b), 1.0) >= 0 for a in pages for b in pages)


class DeterminismTest(unittest.TestCase):
    def setUp(self):
        # 这个矩阵是冲着贪心实现设计的：按 a,b,c,d 顺序塞，d 会塞进第一个簇；
        # 换成 d,c,b,a 顺序，d 自己成簇。a↔d 只有 0.40，本来就不该同簇。
        self.pages = ["a", "b", "c", "d"]
        self.sim = _sim({
            ("a", "b"): 0.95, ("a", "c"): 0.90, ("a", "d"): 0.40,
            ("b", "c"): 0.92, ("b", "d"): 0.45, ("c", "d"): 0.42,
        })
        for p in self.pages:
            self.sim[(p, p)] = 1.0

    def test_order_independent(self):
        """4 个页的**所有 24 种排列**必须给出同一组簇。"""
        base = pagetype.cluster(self.pages, self.sim, 0.75)
        for perm in permutations(self.pages):
            got = pagetype.cluster(list(perm), self.sim, 0.75)
            self.assertEqual([sorted(c) for c in got],
                             [sorted(c) for c in base],
                             f"排列 {perm} 给出了不同的簇")

    def test_complete_linkage_does_not_chain(self):
        """A~B 高、B~C 高、A~C 低：A 和 C 不能因为都像 B 就被并在一起（这是单连接的错误）。"""
        sim = _sim({("a", "b"): 0.95, ("b", "c"): 0.95, ("a", "c"): 0.50})
        for p in ("a", "b", "c"):
            sim[(p, p)] = 1.0
        cl = pagetype.cluster(["a", "b", "c"], sim, 0.75)
        self.assertEqual(len(cl), 2, cl)
        # 关键性质：a 和 c **不能同簇**（不管并列时先并 a~b 还是 b~c，那是任意的，不影响结论）
        self.assertFalse(any({"a", "c"} <= set(c) for c in cl), cl)

    def test_threshold_monotonic(self):
        """阈值升高，簇数只能不变或变多——变少说明实现有问题。"""
        counts = [len(pagetype.cluster(self.pages, self.sim, t))
                  for t in (0.40, 0.60, 0.80, 0.90, 0.99)]
        self.assertEqual(counts, sorted(counts), counts)

    def test_below_threshold_never_merges(self):
        """所有跨页相似度都低于阈值 → 必须一页一簇。"""
        sim = _sim({("a", "b"): 0.30, ("a", "c"): 0.20, ("b", "c"): 0.25})
        for p in ("a", "b", "c"):
            sim[(p, p)] = 1.0
        self.assertEqual(len(pagetype.cluster(["a", "b", "c"], sim, 0.75)), 3)


class RealMatrixTest(unittest.TestCase):
    """真实矩阵也要顺序无关——这条防的是"合成数据过了、真数据还是抖"。"""

    SERIES = Path(os.environ.get("PPT_SERIES_ROOT") or "D:/WorkBuddy工作区/ppt制作")

    @unittest.skipUnless((SERIES / "spec.json").exists() or SERIES.is_dir(), "整套目录不存在")
    def test_real_order_independent(self):
        import json
        import roles
        deck = roles.load_deck(ROOT / "tests" / "fixtures" / "deck-9pages.json")
        grids = {}
        for page in deck["pages"]:
            sp = self.SERIES / page / "spec.json"
            if not sp.exists():
                self.skipTest("缺 spec.json")
            spec = json.loads(sp.read_text(encoding="utf-8"))
            cv = spec.get("canvas") or {}
            W, H = int(cv.get("w_px") or 1672), int(cv.get("h_px") or 941)
            rids = {i for lst in (deck["roles"].get(page) or {}).values() for i in lst}
            grids[page] = pagetype.content_cells(spec, rids, W, H, 40)
        sim = {}
        pages = list(grids)
        for i, a in enumerate(pages):
            for b in pages[i + 1:]:
                sim[(a, b)] = sim[(b, a)] = pagetype.jaccard(grids[a], grids[b])
        for p in pages:
            sim[(p, p)] = 1.0
        base = pagetype.cluster(pages, sim, 0.75)
        rev = pagetype.cluster(pages[::-1], sim, 0.75)
        self.assertEqual([sorted(c) for c in base], [sorted(c) for c in rev])


if __name__ == "__main__":
    unittest.main()
