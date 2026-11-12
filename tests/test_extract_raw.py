#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N1 契约测试：同排归一必须留下「归一前的原始测量」与「来源标记」。

背景（已查实）：`normalize_size_groups` 会就地改写同排块的字号与字重，
改写痕迹过去只存在于 `needs_review` 的中文散文串里，而且**只对被改的块留痕**。
于是"这块实测多少"没有任何机读数据源，任何测量类判据都拿不到分母。

四条：
  1. 归一前落盘 `size_pt_raw` / `weight_raw` / `size_pt_src` / `weight_src`；
  2. 被改写的块 `*_src == "row-normalized"`，没被改的仍是 `"measured"`；
  3. `size_pt` / `weight` 的语义不变（照旧被归一成同排中位数 / 多数票）；
  4. `normalized` 结构化列表逐条可读（id / kind / from / to / rule），且
     `needs_review` 的散文串**保持原样**（历史报告在比对）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(SKILL / "scripts"))

import extract   # noqa: E402


def _row(specs):
    """造一排文字块：同一 y 中心、同高度 → 必然进同一行带。

    specs: [(id, size_pt, weight)]
    """
    els = []
    for i, (eid, size, weight) in enumerate(specs):
        els.append({"id": eid, "type": "text", "bbox": [10 + i * 200, 100, 200 + i * 200, 130],
                    "size_pt": size, "weight": weight})
    return els


class TestRawMeasurement(unittest.TestCase):
    def test_raw_values_survive_normalization(self):
        """30.4 / 22.0 / 22.5 → 中位 22.5；只有 30.4 被改写，原始值全部留下。"""
        els = _row([("t11", 30.4, "bold"), ("t12", 22.0, "bold"), ("t16", 22.5, "bold")])
        norm: list[dict] = []
        notes = extract.normalize_size_groups(els, normalized_out=norm)

        self.assertEqual([e["size_pt"] for e in els], [22.5, 22.0, 22.5])
        self.assertEqual([e["size_pt_raw"] for e in els], [30.4, 22.0, 22.5])
        self.assertEqual([e["size_pt_src"] for e in els],
                         ["row-normalized", "measured", "measured"])
        # 散文串保持原样（历史报告在比对）
        self.assertEqual(notes, ["t11: 字号 30.4->22.5（同排归一）"])
        # 结构化记录只有一条：size / 从 30.4 到 22.5 / 规则 row-median
        self.assertEqual(norm, [{"id": "t11", "kind": "size", "from": 30.4,
                                 "to": 22.5, "rule": "row-median"}])

    def test_two_blocks_can_both_be_normalized(self):
        """中位数两侧各有一块偏离 >6% → 两块都被改写，三条记录可追溯。

        （用 30.4 / 22.0 / 22.5 这种取值时，22.0 距中位 22.5 只有 2.2%，按 6% 阈值**不该**
        被改写——所以"三条都变 + 两条记录"必须靠更宽的离散度才成立。）
        """
        els = _row([("a", 34.7, "regular"), ("b", 26.2, "regular"), ("c", 21.6, "regular")])
        norm: list[dict] = []
        extract.normalize_size_groups(els, normalized_out=norm)

        self.assertEqual([e["size_pt"] for e in els], [26.2, 26.2, 26.2])
        self.assertEqual([e["size_pt_raw"] for e in els], [34.7, 26.2, 21.6])
        self.assertEqual([r["id"] for r in norm], ["a", "c"])
        self.assertEqual({r["rule"] for r in norm}, {"row-median"})
        self.assertEqual([r["from"] for r in norm], [34.7, 21.6])

    def test_weight_majority_keeps_raw(self):
        """两 bold 一 regular → 全 bold；weight_raw 保留原值，来源分开记。"""
        els = _row([("t1", 22.3, "bold"), ("t12", 22.0, "bold"), ("t16", 21.6, "regular")])
        norm: list[dict] = []
        notes = extract.normalize_size_groups(els, normalized_out=norm)

        self.assertEqual([e["weight"] for e in els], ["bold", "bold", "bold"])
        self.assertEqual([e["weight_raw"] for e in els], ["bold", "bold", "regular"])
        self.assertEqual([e["weight_src"] for e in els],
                         ["measured", "measured", "row-normalized"])
        self.assertIn("t16: 粗细 regular->bold（同排多票）", notes)
        self.assertEqual([r for r in norm if r["kind"] == "weight"],
                         [{"id": "t16", "kind": "weight", "from": "regular",
                           "to": "bold", "rule": "row-majority"}])

    def test_single_block_row_untouched_but_still_tagged(self):
        """单块的行不做归一，但 raw / src 仍要落盘——"没被改"本身也是信息。"""
        els = _row([("solo", 19.9, "regular")])
        norm: list[dict] = []
        extract.normalize_size_groups(els, normalized_out=norm)

        self.assertEqual(els[0]["size_pt"], 19.9)
        self.assertEqual(els[0]["size_pt_raw"], 19.9)
        self.assertEqual(els[0]["size_pt_src"], "measured")
        self.assertEqual(norm, [])

    def test_rerun_is_idempotent(self):
        """同一批元素归一两次：raw 不被第二次改写（setdefault 保护）。"""
        els = _row([("t11", 30.4, "bold"), ("t12", 22.0, "bold"), ("t16", 22.5, "bold")])
        extract.normalize_size_groups(els)
        extract.normalize_size_groups(els)
        self.assertEqual([e["size_pt_raw"] for e in els], [30.4, 22.0, 22.5])


if __name__ == "__main__":
    unittest.main(verbosity=2)
