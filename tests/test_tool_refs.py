# -*- coding: utf-8 -*-
"""工具引用完整性（2026-11-09 两次误删的产物）。

① `asset_guard.py` 被判成"0 引用死代码"删掉——它其实被 `gates.py:105`
   用 `_run_tool("asset_guard.py", ...)` 以 **subprocess** 调用，`grep import` 抓不到，
   任何单元测试也抓不到（没人 import 它）。删完跑到 delivery 阶段才炸。
② `gate_v2` 删了 `c2_geometry`/`c4_completeness` 后，`main` 里还有
   `r2` / `r4` 与硬编码的 `4/4` 两处引用没改，跑到 CLI 才 `NameError`。

两条都不是逻辑错误，是"删除不彻底"。靠跑一遍才知道太慢，所以钉成测试：
- subprocess 引用的脚本必须存在（静态扫源码）
- 相关 CLI 必须能跑起来（不崩、退出码合法）
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DELIVERY = ROOT / "scripts" / "delivery"


class SubprocessToolRefsTest(unittest.TestCase):
    """`gates.py` 里 `_run_tool("x.py", ...)` 的每个 x.py 都必须真的存在。"""

    def test_all_run_tool_targets_exist(self):
        src = (DELIVERY / "gates.py").read_text(encoding="utf-8")
        names = set(re.findall(r'_run_tool\(\s*"([\w.\-]+)"', src))
        self.assertTrue(names, "没扫到任何 _run_tool 调用——正则失效了，先修这个测试")
        missing = [n for n in sorted(names)
                   if not (DELIVERY / n).exists() and not (ROOT / "scripts" / n).exists()]
        self.assertEqual(missing, [],
                         f"这些脚本被 gates.py 以 subprocess 调用但已不存在：{missing}")

    def test_no_dangling_script_mentions_in_delivery(self):
        """delivery 下的代码里出现的 `xxx.py` 文件名，不能是已经删掉的脚本。"""
        gone = {"refine.py", "residual.py", "io_gate.py", "verify.py",
                "autocorrect.py", "fidelity.py", "edit_quality.py", "deck_lint.py"}
        hits = []
        for f in DELIVERY.glob("*.py"):
            if f.name in gone:
                continue
            text = f.read_text(encoding="utf-8")
            for name in gone:
                # 只算"当成命令/模块用"的写法，注释里提到退役历史不算
                if re.search(r'_run_tool\(\s*"%s"' % re.escape(name), text):
                    hits.append(f"{f.name} → {name}")
        self.assertEqual(hits, [], f"还在 subprocess 调用已退役的脚本：{hits}")


class CliSmokeTest(unittest.TestCase):
    """改完引用关系的 CLI 必须还能启动——`NameError` 这类只有真跑才暴露。"""

    def _run(self, script: str, *args: str) -> tuple[int, str]:
        r = subprocess.run([sys.executable, str(DELIVERY / script), *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def test_gate_v2_help(self):
        rc, out = self._run("gate_v2.py", "--help")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Traceback", out)

    def test_gate_v2_runs_on_missing_page_without_traceback(self):
        """没有该页时允许非 0 退出，但不许 traceback（那说明内部引用断了）。"""
        rc, out = self._run("gate_v2.py", "--page", str(ROOT / "_no_such_page"))
        self.assertNotIn("Traceback", out, out)
        self.assertIn(rc, (0, 1, 2), out)

    def test_asset_guard_help(self):
        rc, out = self._run("asset_guard.py", "--help")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
