#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧客户九页配置只能当夹具/示例，**不许回到自动查找路径**（2026-11-10）。

背景：`cycle._stage_normalize` 原来有一条 `SKILL/templates/deck.json` 的兜底。
它是旧客户（P01 系列九页）的页清单 + 角色声明。兜底的后果不是"多找一个文件"，而是：
  ① 一个**没有声明**的项目会悄悄套上别人的九个页名和角色——声明错了没人看得见；
  ② 这份客户数据随技能版本走，等于把客户配置当成了全局默认值；
  ③ 换客户时最可能发生的事故：改了自家 deck.json 却有个字段没写对，兜底补上，
     于是"改了跟没改一样"，而阶段表上写着 `normalize pass`。

现在这份原件在 `tests/fixtures/deck-9pages.json`：**只作夹具与示例**。
本文件钉住两件事：
  A. 任何脚本都不许再引用 `templates/` 这个目录（回退一旦复活就在这里变红）；
  B. 夹具本身还是完整的 9 页 × 6 角色（防它被当成"没用的老文件"顺手删掉/改瘦）。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "delivery"))

import roles          # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "deck-9pages.json"
VISUAL_FIX = ROOT / "tests" / "fixtures" / "visual-9pages.json"


class NoClientConfigFallbackTest(unittest.TestCase):
    def test_scripts_do_not_reference_templates_dir(self):
        """`templates/` 目录已删；脚本里再出现它，就是回退又长回来了。"""
        offenders = []
        for p in (ROOT / "scripts").rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            # 只认代码行里的路径拼接/字面量，避开注释里讲历史的那几行
            for ln, line in enumerate(text.splitlines(), 1):
                if "templates" not in line:
                    continue
                code = line.split("#", 1)[0]
                if "templates" in code:
                    offenders.append(f"{p.relative_to(ROOT)}:{ln}: {line.strip()}")
        self.assertEqual(offenders, [], "脚本里又出现了对 templates/ 的引用：\n" + "\n".join(offenders))
        self.assertFalse((ROOT / "templates").exists(),
                         "templates/ 目录不该再存在（旧客户配置已移到 tests/fixtures/）")

    def test_normalize_only_looks_at_project_deck(self):
        """归一阶段的两个候选位置就是页目录与整套根——不多不少。"""
        src = (ROOT / "scripts" / "delivery" / "cycle.py").read_text(encoding="utf-8")
        block = src[src.index("deck_path = None"):src.index("if deck_path is None")]
        self.assertIn('Path(ctx.page) / "deck.json"', block)
        self.assertIn('Path(ctx.page).parent / "deck.json"', block)
        self.assertEqual(block.count('"deck.json"'), 2,
                         f"归一阶段查 deck.json 的位置多了/少了，实际：\n{block}")
        self.assertNotIn("fixtures", block, "夹具不许出现在运行时查找路径里")

    def test_fixture_is_still_a_complete_example(self):
        deck = roles.load_deck(FIXTURE)
        self.assertEqual(len(deck["pages"]), 9, "旧客户九页原件不该被改瘦")
        for page in deck["pages"]:
            roles_of_page = deck["roles"].get(page)
            self.assertIsNotNone(roles_of_page, f"{page} 没有角色声明")
            self.assertEqual(set(roles_of_page), set(roles.ROLE_KEYS),
                             f"{page} 的角色键不全：{sorted(roles_of_page)}")
        self.assertIn("测试/示例数据", json.dumps(deck, ensure_ascii=False)[:400],
                      "夹具顶部要写明它是测试数据，别让人当成默认配置")

    def test_visual_fixture_is_marked(self):
        doc = json.loads(VISUAL_FIX.read_text(encoding="utf-8"))
        self.assertIn("测试/示例数据", str(doc.get("_note") or ""),
                      "visual 夹具的 _note 要写明它只作示例")
        self.assertIn("palette", doc, "夹具仍是完整常量源，不能只剩个空壳")


if __name__ == "__main__":
    unittest.main()
