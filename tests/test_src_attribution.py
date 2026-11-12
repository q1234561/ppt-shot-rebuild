#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""来源标记（`*_src`）与"可归因"判定的契约测试。

为什么需要这套标记：`override.json` 里的 `size_pt` / `weight` 是 autocorrect 追宽度时
写回的控制量，**不是这一页自己测出来的值**。判据若拿它当观测值，就会把别人的算法结论
当成"页面写了矛盾字重"，发出人根本改不掉的红牌。

所以：
  · `specmerge` 只加**元数据**（`size_pt_src` / `weight_src` = "override"），值语义不变；

四条：
  1. 被 override 覆盖的字段打 `override`；没被覆盖的保留元素自带的 `measured`；
  2. 分裂项里有不可归因的块 → `void(undecidable)`；
  3. 全部可归因且不一致 → 仍然 `fail`（**这条不许丢**，否则判据就废了）；
  4. `size_pt_src` 同样要打得出（精确比对 / 后续判据要用，这轮只打标）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(SKILL / "scripts"))

import specmerge   # noqa: E402


def _el(eid, size=None, weight=None, **extra):
    el = {"id": eid, "type": "text", "text": eid, "bbox": [0, 0, 10, 10]}
    if size is not None:
        el["size_pt"] = size
    if weight is not None:
        el["weight"] = weight
    el.update(extra)
    return el


class TestOverrideTagging(unittest.TestCase):
    def test_patched_fields_are_tagged_override(self):
        spec = {"elements": [_el("a", 21, "bold", size_pt_src="measured",
                                 weight_src="measured"),
                             _el("b", 18, "regular", size_pt_src="measured",
                                 weight_src="measured")]}
        ov = {"elements": {"a": {"size_pt": 17.5, "weight": "light"}}}
        out, _st = specmerge.apply(spec, ov)

        a, b = out["elements"][0], out["elements"][1]
        self.assertEqual((a["size_pt"], a["weight"]), (17.5, "light"))   # 值语义不变
        self.assertEqual((a["size_pt_src"], a["weight_src"]), ("override", "override"))
        # 没被覆盖的块保留自己的来源，不许被统一刷成 override
        self.assertEqual((b["size_pt_src"], b["weight_src"]), ("measured", "measured"))

    def test_untouched_field_keeps_its_own_src(self):
        """override 只改 weight 时，size_pt 的 measured 标签必须留着。"""
        spec = {"elements": [_el("a", 21, "bold", size_pt_src="measured")]}
        ov = {"elements": {"a": {"weight": "regular"}}}
        out, _ = specmerge.apply(spec, ov)
        self.assertEqual(out["elements"][0]["size_pt_src"], "measured")
        self.assertEqual(out["elements"][0]["weight_src"], "override")

    def test_added_element_is_tagged(self):
        spec = {"elements": [_el("a", 18, "regular")]}
        ov = {"add": {"elements": [_el("c", 18, "bold")]}}
        out, _ = specmerge.apply(spec, ov)
        c = [e for e in out["elements"] if e["id"] == "c"][0]
        self.assertEqual((c["size_pt_src"], c["weight_src"]), ("override", "override"))

    def test_load_merged_tags_through_override_file(self):
        """走真实读入口：override.json 里有的字段打标，没有的不打。"""
        with tempfile.TemporaryDirectory() as td:
            page = Path(td)
            spec = {"canvas": {"dpi": 125.4},
                    "elements": [_el("a", 21, "bold"), _el("b", 18, "regular")],
                    "containers": [], "assets": []}
            (page / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
            (page / "override.json").write_text(
                json.dumps({"elements": {"a": {"size_pt": 15.33}}}), encoding="utf-8")
            got = specmerge.load_merged(page / "spec.json", quiet=True)
            by = {e["id"]: e for e in got["elements"]}
            self.assertEqual(by["a"]["size_pt_src"], "override")
            self.assertIsNone(by["a"].get("weight_src"))     # weight 没被覆盖 → 不打标
            self.assertIsNone(by["b"].get("size_pt_src"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
