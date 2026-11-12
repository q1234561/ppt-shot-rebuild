# -*- coding: utf-8 -*-
"""冒烟：assetcheck 到底能不能跑。

为什么要有这一条（2026-11-09）：
这一轮改造里出现三次"红"，全是**能不能跑**级别的（NameError / TypeError），
而 `import` 检查抓不到它们——那两个异常只在执行路径上抛。
一条 2 秒的 main() 调用，比"7 页回归崩在第 5 分钟、还不知道崩在哪"便宜得多。

它不属于判据，不参与交付裁决，只回答一个问题：**这个工具还活着吗**。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

SKILL = Path(__file__).resolve().parent.parent
PY = sys.executable


def _make_page(root: Path) -> Path:
    (root / "assets").mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    for x in range(12, 20):
        for y in range(12, 20):
            im.putpixel((x, y), (200, 30, 40, 255))
    im.save(root / "assets" / "a1.png")
    spec = {
        "canvas": {"w_px": 100, "h_px": 100, "dpi": 96},
        "assets": [{"id": "a1", "type": "image", "bbox": [10, 10, 50, 50],
                    "src": "assets/a1.png", "mask": "chroma"}],
    }
    (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return root / "spec.json"


class AssetcheckSmokeTest(unittest.TestCase):
    def test_runs_without_crashing(self):
        with tempfile.TemporaryDirectory() as td:
            spec = _make_page(Path(td))
            r = subprocess.run(
                [PY, str(SKILL / "scripts" / "assetcheck.py"), "--spec", str(spec)],
                capture_output=True, text=True, timeout=120,
            )
            # 通过(0) 或 判定不过(1) 都可以；崩（非 0/1 或 traceback）不行
            self.assertIn(r.returncode, (0, 1),
                          f"assetcheck 崩了（rc={r.returncode}）\n"
                          f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")
            self.assertNotIn("Traceback", r.stderr,
                             f"assetcheck 抛异常：\n{r.stderr}")


if __name__ == "__main__":
    unittest.main()
