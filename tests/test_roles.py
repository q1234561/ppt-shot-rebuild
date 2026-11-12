"""角色声明的契约测试。

钉住三件事：
1. `tests/fixtures/deck-9pages.json`（旧客户九页原件，仅作夹具）每页都声明了 6 个角色键（值可为空数组，键不能缺）；
2. 声明的 id 在对应 spec.json 里真实存在；
3. **反例真的会红**：同页一个 id 被两个角色认领、缺角色键、id 不存在，三种都必须报失败；
   而"跨页同名 id"必须**不报**——id 是页内命名空间，40 和 41 都有 `nav` 是常态。

第 3 点的反面用例是踩过坑的：第一版 `roles.py` 拿全局 `id -> 角色` 查重，
结果 41/45/46/48/49 五页全红，24 条"冲突"全是跨页同名。规则写错了，不是数据错了。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))

import roles  # noqa: E402

DECK_PATH = ROOT / "tests" / "fixtures" / "deck-9pages.json"
# 整套目录：默认指向本机工作区；换机器或打包分发时用环境变量覆盖，缺了就 skip（不是失败）
SERIES = Path(os.environ.get("PPT_SERIES_ROOT") or "D:/WorkBuddy工作区/ppt制作")


def _write_spec(page_dir: Path, ids) -> None:
    """造一个最小 spec：只要有 containers 段和这些 id 就够校验角色了。"""
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "spec.json").write_text(
        json.dumps({"containers": [{"id": i} for i in ids]}, ensure_ascii=False),
        encoding="utf-8")


class DeckDeclTest(unittest.TestCase):
    """只读 deck.json，不碰文件系统。"""

    def setUp(self):
        self.deck = roles.load_deck(DECK_PATH)

    def test_six_role_keys_on_every_page(self):
        self.assertTrue(self.deck["pages"], "pages 不能为空")
        for page in self.deck["pages"]:
            decl = self.deck["roles"].get(page)
            self.assertIsNotNone(decl, f"{page} 缺声明")
            for k in roles.ROLE_KEYS:
                self.assertIn(k, decl, f"{page} 缺角色键 {k}")
                self.assertIsInstance(decl[k], list, f"{page}/{k} 必须是数组（可以是空数组）")

    def test_roles_cover_pages_and_nothing_else(self):
        """声明了但不在 pages 里的页 = 死配置，会让人误以为生效。"""
        self.assertEqual(set(self.deck["roles"]), set(self.deck["pages"]))

    def test_visual_key_is_named(self):
        self.assertTrue(str(self.deck.get("visual") or "").strip())


@unittest.skipUnless((SERIES / "spec.json").exists() or SERIES.is_dir(),
                     "整套目录不存在（设 PPT_SERIES_ROOT 指向它）")
class SeriesResolveTest(unittest.TestCase):
    """对着真实 9 页跑一遍：判据 = 6 角色齐全、id 都存在、失败 0。"""

    def setUp(self):
        self.deck = roles.load_deck(DECK_PATH)

    def test_zero_failures(self):
        got, failures = roles.resolve(self.deck, SERIES)
        self.assertEqual(failures, [], "解析失败：\n" + "\n".join(failures))
        self.assertEqual(len(got), len(self.deck["pages"]))
        for page, r in got.items():
            self.assertEqual(set(r), set(roles.ROLE_KEYS), f"{page} 角色不全")


class NegativeTest(unittest.TestCase):
    """反例：每条判据都要能证明它真的会红。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write_spec(self.root / "p1", ["nav", "pill", "sep1", "foot"])
        _write_spec(self.root / "p2", ["nav", "pill", "sep1", "foot"])

    def tearDown(self):
        self.tmp.cleanup()

    def _deck(self, p1_roles, p2_roles=None):
        p2_roles = p2_roles or {
            "nav": ["nav"], "nav_pill": ["pill"], "div": ["sep1"],
            "deco_l": [], "deco_r": [], "footer": ["foot"]}
        return {"visual": "t", "pages": ["p1", "p2"],
                "roles": {"p1": p1_roles, "p2": p2_roles}}

    def test_cross_page_same_id_is_not_a_conflict(self):
        """40 和 41 都有 nav、44/45/48/49 都有 c_nav——跨页同名必须全绿。"""
        ok = {"nav": ["nav"], "nav_pill": ["pill"], "div": ["sep1"],
              "deco_l": [], "deco_r": [], "footer": ["foot"]}
        _, failures = roles.resolve(self._deck(ok), self.root)
        self.assertEqual(failures, [], f"跨页同名被误判成冲突：{failures}")

    def test_same_page_double_claim_fails(self):
        """同一页里 nav 和 nav_pill 都写 'nav' → 必须报冲突（否则对齐会拿同一个件当两个件）。"""
        bad = {"nav": ["nav"], "nav_pill": ["nav"], "div": ["sep1"],
               "deco_l": [], "deco_r": [], "footer": ["foot"]}
        _, failures = roles.resolve(self._deck(bad), self.root)
        self.assertTrue(any("同时被" in f for f in failures), failures)

    def test_missing_role_key_fails(self):
        bad = {"nav": ["nav"], "nav_pill": ["pill"], "div": ["sep1"], "footer": ["foot"]}
        _, failures = roles.resolve(self._deck(bad), self.root)
        self.assertTrue(any("缺角色键" in f for f in failures), failures)

    def test_unknown_id_fails(self):
        bad = {"nav": ["navv"], "nav_pill": ["pill"], "div": ["sep1"],
               "deco_l": [], "deco_r": [], "footer": ["foot"]}
        _, failures = roles.resolve(self._deck(bad), self.root)
        self.assertTrue(any("不在 spec 里" in f for f in failures), failures)

    def test_empty_role_is_legal(self):
        """某一页没有装饰件是设计事实，不是失败——空数组必须放行。"""
        ok = {"nav": ["nav"], "nav_pill": ["pill"], "div": [],
              "deco_l": [], "deco_r": [], "footer": ["foot"]}
        _, failures = roles.resolve(self._deck(ok), self.root)
        self.assertEqual(failures, [], failures)


if __name__ == "__main__":
    unittest.main()
