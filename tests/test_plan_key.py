# -*- coding: utf-8 -*-
"""plan 的复用键必须含判据身份（2026-11-09 E1 回归）。

实测踩法：改完 `spec_lint` 的②判据后跑 cycle，plan 阶段直接 `reused` 旧报告、
判 ready 并出片；同一条 spec 单跑 `gates.py --stage plan` 却报 fail。
也就是"判据改了，新判据一次都没执行"，而终端显示全部通过。
`key_look` 当年缺判据指纹踩的是同一个坑（它的 docstring 里写着），plan 当时漏了。

这里不测"键长什么样"，只测两件可证伪的事：
1. plan 的判据清单里必须有 spec_lint（改它就得 stale）；
2. `key_plan` 不能等于"只用 plan_deps 算出来的键"——相等就说明 checks 没进键。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DELIVERY = ROOT / "scripts" / "delivery"
for p in (str(ROOT / "scripts"), str(DELIVERY)):
    if p not in sys.path:
        sys.path.insert(0, p)

import inventory            # noqa: E402
import report_state as rs   # noqa: E402


SPEC = {
    "canvas": {"w_px": 1672, "h_px": 941, "dpi": 125.4,
               "width_in": 13.3333, "height_in": 7.504},
    "background": {"type": "gradient", "gradient": ["#FDFEFF", "#E6F4FE"],
                   "gradient_angle": 90},
    "containers": [],
    "assets": [],
    "elements": [{"id": "t_b1", "type": "text", "text": "样例",
                  "bbox": [100, 600, 400, 640], "size_pt": 20,
                  "weight": "regular", "color": "#03288F"}],
}


class PlanKeyCarriesCheckerTest(unittest.TestCase):
    def test_spec_lint_is_a_plan_checker(self):
        self.assertIn("delivery/spec_lint.py", rs.CHECKER_SRC["plan"],
                      "spec_lint 是 plan 的判据主体，改它必须让旧 plan 报告失效")

    def test_key_plan_is_not_plain_plan_deps(self):
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "spec.json"
            sp.write_text(json.dumps(SPEC, ensure_ascii=False), encoding="utf-8")
            inv = {"items": []}
            signed = rs.key_plan(sp, inv)
            plain = rs.dependency_key("plan", inventory.plan_deps(sp, inv))
        self.assertNotEqual(signed, plain,
                            "key_plan 等于裸 plan_deps 的键 → checks 没进键，改判据不会 stale")

    def test_key_plan_is_stable_for_same_input(self):
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "spec.json"
            sp.write_text(json.dumps(SPEC, ensure_ascii=False), encoding="utf-8")
            a = rs.key_plan(sp, {"items": []})
            b = rs.key_plan(sp, {"items": []})
        self.assertEqual(a, b, "同一份输入必须得到同一个键，否则每次都会重跑")


if __name__ == "__main__":
    unittest.main()
