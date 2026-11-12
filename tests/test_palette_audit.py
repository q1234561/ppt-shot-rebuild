"""色板审计的契约测试（palette_audit）。

这份文件钉的不是"函数能跑通"，而是**结论能不能被复算和质疑**。六条主线，每条都带反例：

1. **ΔE 数值必须对得上外部标尺**：黑↔白 ≈100、红↔绿 ≈86.6、同色 =0，容差 ±1.0；
   另加 Sharma 参考对（Lab 直给，不经过 sRGB 转换）——它验证公式本身，
   而 sRGB 那几对验证转换链。两者都过，才说明"这个 ΔE00 是别人也能算出来的那个 ΔE00"。
2. **不许用 ΔE76**：ΔE76 与 ΔE00 不是常数倍（本套数据比值 0.94~4.93），
   把 JND=2.3 套到 ΔE76 上是量纲错误。这里钉死一个具体色对的两个值，
   防止有人日后"简化"成 `math.dist()`——那会让报告数字全变而测试全绿。
3. **完全连接不许串链**：A~B 近、B~C 近、A~C 远时，A 与 C 必须不同簇。
   单连接会把三者并成一簇，这是本项目明确排除的做法。
4. **阈值单调**：阈值升高，簇数只能减少或不变。这是"判据"必须有的性质
   （贪心覆盖算法缺这条，所以它只能当对照，不能当主判据）。
5. **顺序无关**：打乱 palette 的键序，簇划分必须一模一样。
   仓库刚因为"顺序依赖"被打回过一次，这条是回归闸门。
6. **只读**：跑完之后 `visual.json` 与所有 `spec.json` 的 md5 不变。
   审计是"给建议"，任何写回都会让"建议"变成"已执行"，而没人知道。

另有一条关于用法的反例：**token 与裸值混写都要数到**。
只统计 token 引用会让 8 个尚未 token 化的页面在报告里显示为"该色没人用"，
代表色于是被单页偏好决定——这类错误不会报错，只会给出错结论。
"""
import hashlib
import io
import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("PPT_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))

import palette_audit  # noqa: E402

# 整套目录：默认指向本机工作区；换机器或打包分发时用环境变量覆盖，缺了就 skip（不是失败）
SERIES = Path(os.environ.get("PPT_SERIES_ROOT") or "D:/WorkBuddy工作区/ppt制作")
VISUAL = SERIES / "visual.json"

# 一组"人工可查"的灰阶 + 三原色，用于不依赖真实数据的聚类测试。
# gray_a/b/c 三档特意取成 A~B 最近（1.495）、B~C 次近（2.162）、A~C 最远（3.657）：
# 这个形状才能区分"完全连接"与"单连接"——B~C 也在阈值内，单连接会把三者串成一簇。
SYNTH_PALETTE = {
    "palette.gray_a": "#808080",
    "palette.gray_b": "#848484",
    "palette.gray_c": "#8A8A8A",
    "palette.gray_d": "#A0A0A0",
    "palette.gray_e": "#A4A4A4",
    "palette.red": "#FF0000",
    "palette.green": "#00FF00",
    "palette.blue": "#0000FF",
    "palette.black": "#000000",
    "palette.white": "#FFFFFF",
}


def _lab(hex_value: str):
    return palette_audit.hex_to_lab(hex_value)


def _dist(palette: dict) -> dict:
    names = sorted(palette)
    labs = {n: _lab(palette[n]) for n in names}
    return names, palette_audit.distance_matrix(names, labs)


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _run_cli(argv: list[str]) -> int:
    """跑 CLI 但吞掉它的控制台输出。

    `main()` 会打印分组摘要（对人是必要的）。在 `unittest discover` 里这些行会混进
    测试汇总，把 "Ran 205 tests ... OK" 埋掉——于是没人看得清到底全绿没有。
    这里吞掉的是**stdout**，返回值照样传递，所以 rc 断言仍然有效。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        return palette_audit.main(argv)


def _write_page(root: Path, name: str, spec: dict) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")


def _write_visual(root: Path, palette: dict) -> Path:
    """palette 传的是扁平 token 名（`palette.x`），写成 visual.json 时还原成嵌套结构。"""
    nested: dict = {}
    for token, value in palette.items():
        parts = token.split(".")
        cur = nested
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    p = root / "visual.json"
    p.write_text(json.dumps(nested, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------- 1/2. ΔE 数值

class DeltaETest(unittest.TestCase):
    """ΔE00 必须与外部标尺对得上，容差 ±1.0（题目要求的量级）。"""

    def test_known_pairs(self):
        cases = [
            ("#000000", "#FFFFFF", 100.0, "黑↔白"),
            ("#FF0000", "#00FF00", 86.6, "红↔绿"),
            ("#0A55E0", "#0A55E0", 0.0, "同色"),
            ("#FF0000", "#0000FF", 52.9, "红↔蓝"),
        ]
        for a, b, expected, label in cases:
            with self.subTest(label=label):
                got = palette_audit.delta_e_2000(_lab(a), _lab(b))
                self.assertAlmostEqual(got, expected, delta=1.0,
                                       msg=f"{label} {a}↔{b} 期望≈{expected}，实得 {got}")

    def test_symmetric(self):
        """ΔE 必须对称：矩阵只用上三角填一次，不对称会让报告两处数字打架。"""
        a, b = _lab("#03288F"), _lab("#02319D")
        self.assertAlmostEqual(palette_audit.delta_e_2000(a, b),
                               palette_audit.delta_e_2000(b, a), places=9)

    def test_sharma_reference_pairs(self):
        """Sharma, Wu & Dalal (2005) 的**全部 34 组**参考向量，Lab 直接给值。

        绕开 sRGB 转换单独校验公式，容差 1e-4（论文给 4 位小数）。
        为什么必须是全表而不是挑几组：CIEDE2000 的坑集中在**色相绕回**与**C'=0 的灰色**
        这两类边界，而挑样本很容易只挑到"中间的、好算的"那几组，正好把坑漏掉。
        这 34 组里第 7~16 组专打色相绕回、第 21~24 组专打低彩度、第 31~34 组打暗部。
        跑全表还有一个副作用：它是"实现正确"的最强证据——任何一处权重写错都会红一大片。
        """
        cases = [
            ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
            ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
            ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
            ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
            ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
            ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
            ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
            ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
            ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
            ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
            ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
            ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
            ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
            ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
            ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
            ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
            ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
            ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
            ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
            ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
            ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
            ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
            ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
            ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
            ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
            ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
            ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
            ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
            ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
            ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
            ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
            ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
            ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
            ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
        ]
        self.assertEqual(len(cases), 34, "Sharma 参考向量应为 34 组")
        for idx, (lab1, lab2, expected) in enumerate(cases, start=1):
            with self.subTest(pair=idx, expected=expected):
                self.assertAlmostEqual(palette_audit.delta_e_2000(lab1, lab2),
                                       expected, delta=1e-4)

    def test_white_point_gives_l100(self):
        """#FFFFFF 的 L* 必须是 100。

        这条钉的是一个真实踩过的坑：XYZ 矩阵算出的是 0..1 归一化值，而 D65 白点是 0..100
        标度。忘了乘 100 时 #FFFFFF 会得到 L*=9，于是所有 ΔE 缩小一个量级——
        不报错、不崩，只是"什么都能合并"。sRGB 那几对已知值也拦不住（它们一起缩小）。
        """
        l, a, b = _lab("#FFFFFF")
        self.assertAlmostEqual(l, 100.0, delta=0.01)
        self.assertAlmostEqual(a, 0.0, delta=0.01)
        self.assertAlmostEqual(b, 0.0, delta=0.01)
        self.assertAlmostEqual(_lab("#000000")[0], 0.0, delta=0.01)

    def test_not_delta_e76(self):
        """同一个深蓝色对，ΔE00 与 ΔE76 必须不同——证明判据没退化成欧氏距离。"""
        lab_a, lab_b = _lab("#03288F"), _lab("#02319D")
        e00 = palette_audit.delta_e_2000(lab_a, lab_b)
        e76 = palette_audit.delta_e_76(lab_a, lab_b)
        self.assertAlmostEqual(e00, 3.02, delta=0.05)
        self.assertAlmostEqual(e76, 4.51, delta=0.05)
        self.assertGreater(abs(e76 - e00), 1.0,
                           "两种标度在这个色对上必须明显不同，否则测试失去鉴别力")

    def test_bad_hex_raises(self):
        for bad in ("03288F", "#3288F", "#GGGGGG", "", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    palette_audit.hex_to_rgb(bad)


# ---------------------------------------------------------------- 3/4/5. 聚类

class ClusterTest(unittest.TestCase):
    def setUp(self):
        self.names, self.dist = _dist(SYNTH_PALETTE)

    def test_complete_linkage_does_not_chain(self):
        """A~B 近、B~C 近、A~C 远 → A 与 C 绝不能同簇。

        实测：#808080↔#848484 = 1.495（最近）、#848484↔#8A8A8A = 2.162、首尾 = 3.657。
        阈值 2.3 时：A、B 该合（1.495）；B、C 单看也够（2.162 ≤ 2.3），
        但并成 {A,B,C} 会让簇内出现 A↔C=3.657 的可分辨差 —— 单连接正是这样串链的。
        所以合格结果只有一种：{A,B} + {C}，A 与 C 分离。
        """
        a, b, c = "palette.gray_a", "palette.gray_b", "palette.gray_c"
        self.assertAlmostEqual(self.dist[a][b], 1.495, delta=0.02)
        self.assertAlmostEqual(self.dist[b][c], 2.162, delta=0.02)
        self.assertAlmostEqual(self.dist[a][c], 3.657, delta=0.02)
        self.assertLessEqual(self.dist[b][c], 2.3,
                             "测试自身失效：B~C 必须落在阈值内，否则单连接也无从串链")

        groups = palette_audit.cluster_complete_linkage(self.names, self.dist, 2.3)
        group_of = {m: gi for gi, g in enumerate(groups) for m in g}
        self.assertEqual(group_of[a], group_of[b], "最近的 A~B 对必须同簇")
        self.assertNotEqual(group_of[a], group_of[c], "串链发生了：首尾 3.66 被并进同一簇")

        # 同一个输入贪心团覆盖也必须不串链（它是另一个合法实现，性质不能更差）
        groups_cc = palette_audit.cluster_clique_cover(self.names, self.dist, 2.3)
        gof_cc = {m: gi for gi, g in enumerate(groups_cc) for m in g}
        self.assertNotEqual(gof_cc[a], gof_cc[c], "团覆盖不该串链")

    def test_every_group_is_a_clique(self):
        """簇内任意两色 ΔE ≤ 阈值——这是"合并安全"的唯一判据，两种算法都要满足。"""
        for threshold in (0.5, 1.0, 2.3, 5.0):
            for algo, fn in palette_audit.ALGORITHMS.items():
                with self.subTest(threshold=threshold, algo=algo):
                    for g in fn(self.names, self.dist, threshold):
                        worst = palette_audit.cluster_max_delta(g, self.dist)
                        self.assertLessEqual(worst, threshold + 1e-9,
                                             f"{algo} 产生了非团簇：{g} maxΔE={worst}")

    def test_threshold_monotonic(self):
        """阈值升高，簇数只能减少或不变。

        这条挡的是"把阈值当调参旋钮"：如果组数不单调，就没法回答
        "为什么阈值 2.3 是 25 组、2.4 变成 26 组"——判据就失效了。
        主判据（complete）必须单调；贪心覆盖不保证（已知，见其 docstring），故不纳入断言。
        """
        thresholds = [0.1 * i for i in range(1, 101)]
        counts = [len(palette_audit.cluster_complete_linkage(self.names, self.dist, t))
                  for t in thresholds]
        for prev, cur, t in zip(counts, counts[1:], thresholds[1:]):
            self.assertLessEqual(cur, prev,
                                 f"阈值 {t:.1f} 时组数从 {prev} 反弹到 {cur}（不单调）")
        self.assertEqual(counts[0], len(self.names), "极小阈值时每个色各自成簇")
        # 单独验证上界：扫到 10.0 还没全并完（黑白相距 100），所以用远超最大 ΔE 的阈值
        self.assertEqual(len(palette_audit.cluster_complete_linkage(self.names, self.dist, 200.0)),
                         1, "足够大的阈值必须全部并成一簇")

    def test_order_independence(self):
        """打乱 palette 键序，簇划分必须完全一致（回归：仓库曾因顺序依赖被打回）。"""
        names, dist = _dist(SYNTH_PALETTE)
        baseline = palette_audit.cluster_complete_linkage(names, dist, 2.3)

        # 真随机会引入不可复现的抖动，这里用确定性反转 + 步长重排
        shuffled = list(SYNTH_PALETTE.items())
        shuffled = shuffled[::-1]
        shuffled = shuffled[::2] + shuffled[1::2]
        names2, dist2 = _dist(dict(shuffled))
        self.assertNotEqual(list(SYNTH_PALETTE), list(dict(shuffled)),
                            "测试自身失效：重排后顺序居然没变")

        self.assertEqual(palette_audit.cluster_complete_linkage(names2, dist2, 2.3), baseline)
        # 团覆盖也必须顺序无关（它内部按名字迭代，不按 dict 顺序）
        self.assertEqual(palette_audit.cluster_clique_cover(names2, dist2, 2.3),
                         palette_audit.cluster_clique_cover(names, dist, 2.3))

    def test_representative_rule(self):
        """代表色：用量最多；并列时取 L* 中位（下中位）；再并列按名字。

        规则不写清的话，下一个人无法解释"为什么 navy_900 留下、navy_900b 被删"。
        """
        labs = {n: _lab(SYNTH_PALETTE[n]) for n in SYNTH_PALETTE}
        members = ["palette.gray_a", "palette.gray_b", "palette.gray_c"]
        usage = {m: {"count": 0} for m in SYNTH_PALETTE}

        usage["palette.gray_b"]["count"] = 5
        self.assertEqual(palette_audit.pick_representative(members, usage, labs),
                         "palette.gray_b", "用量最多的必须被选中")

        # 全部并列 0 次：取 L* 中位（gray_b 在中间）
        for m in members:
            usage[m]["count"] = 0
        self.assertEqual(palette_audit.pick_representative(members, usage, labs),
                         "palette.gray_b", "并列时取亮度中位")

        # 并列且亮度不同：取 L* 中位。两个成员时取下中位，即**偏暗**的那个
        # （white L*≈100、gray_d L*≈64 → 选 gray_d）。规则本身要能解释，所以钉死方向。
        same = ["palette.white", "palette.gray_d"]
        for m in same:
            usage[m]["count"] = 3
        self.assertEqual(palette_audit.pick_representative(same, usage, labs),
                         "palette.gray_d", "并列时取 L* 下中位（两个成员时选偏暗者）")

    def test_unknown_algorithm_raises(self):
        with self.assertRaises(ValueError):
            palette_audit.cluster(self.names, self.dist, 2.3, "single")


# ---------------------------------------------------------------- 6. 用法与只读

class UsageTest(unittest.TestCase):
    """用法统计：token 与裸值混写都要数到；审计不许改任何输入文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.palette = {
            "palette.ink": "#1A1A1A",
            "palette.blue": "#0A55E0",
        }
        self.visual = _write_visual(self.root, self.palette)

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_token_and_bare(self):
        """token 引用与裸值同权：漏了裸值，未 token 化的页面就会被当成"该色没人用"。"""
        _write_page(self.root, "p1", {"elements": [
            {"id": "t1", "color": "{palette.ink}"},
            {"id": "t2", "color": "{palette.ink}"},
        ]})
        _write_page(self.root, "p2", {"elements": [
            {"id": "t1", "color": "#1A1A1A"},               # 裸值，同一个色
        ], "background": {"gradient": ["#1A1A1A", "#0A55E0"]}})   # 渐变里的裸值也要数

        payload = palette_audit.audit(self.visual, self.root, 2.3)
        ink = payload["usage"]["palette.ink"]
        self.assertEqual(ink["count"], 4, f"p1×2 + p2 裸值×2（含渐变端点），实得 {ink}")
        self.assertEqual(ink["token_refs"], 2)
        self.assertEqual(ink["bare_refs"], 2)
        self.assertEqual(ink["pages"], ["p1", "p2"])
        self.assertEqual(ink["per_page"], {"p1": 2, "p2": 2})

        blue = payload["usage"]["palette.blue"]
        self.assertEqual(blue["count"], 1)
        self.assertEqual(blue["pages"], ["p2"])

    def test_unmapped_bare_reported(self):
        """调色板外的裸色要单独报出来：它们是"色数真正的大头"，但不在合并范围内。"""
        _write_page(self.root, "p1", {"elements": [{"id": "t1", "color": "#123456"}]})
        payload = palette_audit.audit(self.visual, self.root, 2.3)
        self.assertIn("#123456", payload["unmapped_bare"])
        self.assertEqual(payload["unmapped_bare"]["#123456"]["count"], 1)
        for tok in self.palette:
            self.assertEqual(payload["usage"][tok]["count"], 0, f"{tok} 不该被算进用量")

    def test_non_color_tokens_ignored(self):
        """`{radius.card}`、`{type.size.t_s}` 这类非颜色 token 不能被当颜色统计。"""
        visual = json.loads(self.visual.read_text(encoding="utf-8"))
        visual["radius"] = {"card": 24}
        self.visual.write_text(json.dumps(visual), encoding="utf-8")
        _write_page(self.root, "p1", {"containers": [
            {"id": "c1", "radius_px": "{radius.card}", "fill": "{palette.ink}"}]})
        payload = palette_audit.audit(self.visual, self.root, 2.3)
        self.assertEqual(payload["usage"]["palette.ink"]["count"], 1)
        self.assertEqual(sum(u["count"] for u in payload["usage"].values()), 1)

    def test_inputs_untouched(self):
        """跑完之后 visual.json 与所有 spec.json 的 md5 不变。

        审计产出的是**建议**。任何写回都会让"建议"变成"已执行"，
        而执行合并是需要重渲染 + 重新验收的动作，不能藏在一次审计里。
        """
        _write_page(self.root, "p1", {"elements": [
            {"id": "t1", "color": "{palette.ink}"}, {"id": "t2", "color": "#0A55E0"}]})
        _write_page(self.root, "p2", {"elements": [{"id": "t1", "color": "#1A1A1A"}]})
        tracked = [self.visual,
                   self.root / "p1" / "spec.json",
                   self.root / "p2" / "spec.json"]
        before = {p: _md5(p) for p in tracked}
        palette_audit.audit(self.visual, self.root, 2.3)
        _run_cli(["--visual", str(self.visual), "--root", str(self.root),
                            "--out-json", str(self.root / "o.json"),
                            "--out-md", str(self.root / "o.md")])
        for p, digest in before.items():
            self.assertEqual(_md5(p), digest, f"{p.name} 被审计改动了")

    def test_outputs_written(self):
        _write_page(self.root, "p1", {"elements": [{"id": "t1", "color": "{palette.ink}"}]})
        rc = _run_cli(["--visual", str(self.visual), "--root", str(self.root),
                                 "--out-json", str(self.root / "sub" / "o.json"),
                                 "--out-md", str(self.root / "sub" / "o.md")])
        self.assertEqual(rc, 0)
        payload = json.loads((self.root / "sub" / "o.json").read_text(encoding="utf-8"))
        for key in ("params", "matrix", "groups", "usage", "order"):
            self.assertIn(key, payload)
        for key in ("threshold", "color_space", "algorithm", "delta_e", "representative_rule"):
            self.assertIn(key, payload["params"], f"params 缺 {key}，别人无法复算")
        md = (self.root / "sub" / "o.md").read_text(encoding="utf-8")
        self.assertIn("分组表", md)
        self.assertIn("#1A1A1A", md)

    def test_repeat_run_identical(self):
        """同输入两次跑出**逐字节相同**的报告：报告里不许有时间戳/随机序。"""
        _write_page(self.root, "p1", {"elements": [{"id": "t1", "color": "{palette.ink}"}]})
        outs = []
        for i in (1, 2):
            j = self.root / f"r{i}.json"
            m = self.root / f"r{i}.md"
            _run_cli(["--visual", str(self.visual), "--root", str(self.root),
                                "--out-json", str(j), "--out-md", str(m)])
            outs.append((j.read_bytes(), m.read_bytes()))
        self.assertEqual(outs[0], outs[1], "两次运行的报告不一致（有隐藏的随机/时间依赖）")


class ThresholdValidationTest(unittest.TestCase):
    """阈值越界必须报错退出，不能静默用默认值。

    静默夹到合法区间是最坏的处理：报出来的分组对应的是**另一个**阈值，
    而报告上写的是你填的那个数，读到的人无从发现。
    """

    def test_rejects_out_of_range(self):
        for bad in (0.0, -1.0, -0.001, 20.1, 25.0, 100.0, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    palette_audit.validate_threshold(bad)

    def test_accepts_jnd_and_bounds(self):
        for good in (0.001, 1.0, 2.3, 5.0, 20.0):
            with self.subTest(good=good):
                self.assertEqual(palette_audit.validate_threshold(good), good)

    def test_cli_returns_rc2(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            visual = _write_visual(root, {"palette.ink": "#1A1A1A"})
            for bad in ("0", "-3", "21"):
                with self.subTest(bad=bad):
                    rc = _run_cli(["--visual", str(visual), "--root", str(root),
                                             "--out-json", str(root / "o.json"),
                                             "--out-md", str(root / "o.md"),
                                             "--threshold", bad])
                    self.assertEqual(rc, 2, "越界阈值必须以 rc=2 退出")
                    self.assertFalse((root / "o.json").exists(), "参数错误时不许产出报告")
        finally:
            tmp.cleanup()


class FlattenPaletteTest(unittest.TestCase):
    def test_nested_shape(self):
        got = palette_audit.flatten_palette(
            {"palette": {"sky_120": "#D5E7F8", "radius": 24,
                         "deep": {"x": "#000000"}}})
        self.assertEqual(got, {"palette.sky_120": "#D5E7F8", "palette.deep.x": "#000000"})

    def test_flat_dotted_shape(self):
        """设计文档里描述的是 `{"palette.xxx": "#RRGGBB"}` 扁平形状，也必须认。"""
        got = palette_audit.flatten_palette({"palette": {"palette.ink": "#1a1a1a"}})
        self.assertEqual(got, {"palette.ink": "#1A1A1A"})

    def test_missing_visual_raises(self):
        with self.assertRaises(FileNotFoundError):
            palette_audit.load_visual(Path("definitely/not/here/visual.json"))


# ---------------------------------------------------------------- 真实数据

@unittest.skipUnless(VISUAL.exists(), "visual.json 不存在（设 PPT_SERIES_ROOT 指向整套目录）")
class RealSeriesTest(unittest.TestCase):
    """对着真实的 35 色跑一遍。这些数字是报告的骨架，变了必须有人知道。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = palette_audit.audit(VISUAL, SERIES, palette_audit.JND)
        cls.groups = cls.payload["groups"]
        cls.dist = cls.payload["matrix"]

    def test_token_and_group_counts(self):
        self.assertEqual(self.payload["params"]["token_count"], 35)
        # 钉死具体数值，不留区间：区间断言在阈值语义漂移时会**静默**跟着漂
        # （例如阈值被误改成 3.0，组数变 23，`1 < n < 35` 照样绿）。
        # 这个数字是给主理人做决策用的，变了必须有人知道、并且是刻意改的。
        self.assertEqual(self.payload["params"]["group_count"], 25)
        self.assertEqual(self.payload["params"]["merged_away"], 10)
        self.assertEqual(self.payload["params"]["group_count"], len(self.groups))

    def test_known_drift_pairs_are_merged(self):
        """这道题就是为它们出的：同一种深蓝被测量漂移成多个色号。"""
        group_of = {m: gi for gi, g in enumerate(self.groups) for m in g["members"]}
        self.assertEqual(group_of["palette.navy_900"], group_of["palette.navy_900b"],
                         "navy_900 与 navy_900b 必须同组（这是本审计要抓的漂移）")

    def test_groups_are_cliques(self):
        for g in self.groups:
            worst = palette_audit.cluster_max_delta(g["members"], self.payload["matrix"])
            self.assertLessEqual(worst, palette_audit.JND + 0.005,
                                 f"{g['id']} 簇内最大 ΔE {worst} 超过 JND")

    def test_usage_total_is_value_deduplicated(self):
        """`usage_total` 必须等于**按色值去重**后的处数，不是成员 count 的直和。

        这条是 D1 的回归闸门：`bg_grad_11` 与 `blue_700` 都是 #0A55E0，
        两个 token 的 count 都是"该色值出现处数"（4），直接 Σ 会报 8 处，
        而库里 #0A55E0 只出现 4 次。表格最后一列被用来判断"改动面多大"，
        算两遍会让人以为要改的元素数是实际的两倍。
        """
        usage = self.payload["usage"]
        for g in self.groups:
            by_value = {}
            for m in g["members"]:
                by_value[usage[m]["hex"]] = usage[m]["count"]
            self.assertEqual(g["usage_total"], sum(by_value.values()),
                             f"{g['id']} 的 usage_total 没有按色值去重")
            self.assertLessEqual(g["usage_total"], sum(usage[m]["count"] for m in g["members"]))

        # 真实存在同值多 token 的簇，必须真的被去重过（否则这条测试等于没测）
        dup = [g for g in self.groups
               if len({usage[m]["hex"] for m in g["members"]}) < len(g["members"])]
        self.assertTrue(dup, "本套数据应存在同值多 token 的簇（bg_grad_11 / blue_700）")
        for g in dup:
            naive = sum(usage[m]["count"] for m in g["members"])
            self.assertLess(g["usage_total"], naive,
                            f"{g['id']} 有同值 token，去重后必须小于直和")
            self.assertGreater(naive, 0)

    def test_every_token_covered_exactly_once(self):
        seen = [m for g in self.groups for m in g["members"]]
        self.assertEqual(sorted(seen), self.payload["order"], "有 token 漏掉或被分进两个簇")
        self.assertEqual(len(seen), len(set(seen)))

    def test_representatives_are_members(self):
        for g in self.groups:
            self.assertIn(g["representative"], g["members"], f"{g['id']} 的代表不在簇内")

    def test_matrix_is_symmetric_with_zero_diagonal(self):
        names = self.payload["order"]
        for a in names:
            self.assertEqual(self.payload["matrix"][a][a], 0.0)
            for b in names:
                # 落盘精度 2 位，允许 0.01 的舍入差
                self.assertAlmostEqual(self.payload["matrix"][a][b],
                                       self.payload["matrix"][b][a], delta=0.011)

    def test_per_page_stats_sum_to_global(self):
        """逐页明细必须与全局口径对得上。

        这页统计是给"某一页有几种颜色"这类问题提供出处的：数字一旦不在产物里，
        转述时就会变成查不到出处的错值（曾出现"page06 = 53 种"这种取不到的数）。
        对得上，才敢引用。
        """
        per_page = self.payload["params"]["per_page"]
        self.assertEqual(sorted(per_page), sorted(self.payload["params"]["pages"]))

        total_all = sum(v["occurrences_all"] for v in per_page.values())
        total_unmapped = sum(v["occurrences_unmapped"] for v in per_page.values())
        self.assertGreater(total_all, 0)

        global_unmapped = sum(v["count"] for v in self.payload["unmapped_bare"].values())
        self.assertEqual(total_unmapped, global_unmapped,
                         "逐页「调色板外」处数之和必须等于全局 unmapped_bare 总处数")
        for page, v in per_page.items():
            self.assertLessEqual(v["distinct_unmapped"], v["distinct_all"], page)
            self.assertLessEqual(v["occurrences_unmapped"], v["occurrences_all"], page)
            self.assertLessEqual(v["distinct_all"], v["occurrences_all"], page)

    def test_threshold_robustness_is_consistent(self):
        """误差棒两端的定义必须自洽：lo/hi 处仍是同一组数，below/above 处必须不同。

        这条钉的是一个真实的 bookkeeping bug：扫边界时我把"组数已经变了的那一步"
        也写进了 lo，于是报告写出 1.6~2.6（真值 1.61~2.59），并且"再往外一步 1.6 → 26 组"
        把 1.6 和 26 错配在一起。只差 0.01，肉眼看不出来，但结论的误差棒就是错的。
        """
        rb = self.payload["params"]["threshold_robustness"]
        thr = self.payload["params"]["threshold"]
        self.assertEqual(rb["group_count"], self.payload["params"]["group_count"])
        self.assertLess(rb["lo"], rb["hi"])
        self.assertLessEqual(rb["lo"], thr)
        self.assertGreaterEqual(rb["hi"], thr)
        if rb["below"]:
            self.assertNotEqual(rb["below"]["group_count"], rb["group_count"],
                                "below 必须是**已经变了**的那一步")
            self.assertLess(rb["below"]["threshold"], rb["lo"])
        if rb["above"]:
            self.assertNotEqual(rb["above"]["group_count"], rb["group_count"],
                                "above 必须是**已经变了**的那一步")
            self.assertGreater(rb["above"]["threshold"], rb["hi"])

    def test_usage_covers_nine_pages(self):
        # 页数不再硬编码 9：整套在长（2026-11-10 已到 11 页），硬编码会让"加一页"
        # 必然打红一条与加页无关的用例。判据强度不变——仍要求**扫描到的页 == 真实页**。
        expect = sorted(p.name for p in SERIES.iterdir()
                        if p.is_dir() and (p / "spec.json").exists()
                        and not p.name.startswith("_"))   # 工具本就跳过 _ 前缀的临时目录
        got = sorted(self.payload["params"]["pages"])
        self.assertEqual(got, expect,
                         f"扫描到的页与真实页不一致：{got} vs {expect}")
        self.assertTrue(any(u["count"] > 0 for u in self.payload["usage"].values()),
                        "所有 token 用量都是 0，说明用法统计没读到 spec（路径或展开方式错了）")

    def test_real_series_untouched(self):
        """真实数据只读：9 个 spec.json 与 visual.json 的 md5 前后一致。"""
        tracked = [VISUAL] + [SERIES / p / "spec.json"
                              for p in self.payload["params"]["pages"]]
        missing = [p for p in tracked if not p.exists()]
        self.assertEqual(missing, [], f"待校验的文件不存在：{missing}")
        before = {p: _md5(p) for p in tracked}

        tmp = tempfile.TemporaryDirectory()
        try:
            rc = _run_cli(["--visual", str(VISUAL), "--root", str(SERIES),
                                     "--out-json", str(Path(tmp.name) / "a.json"),
                                     "--out-md", str(Path(tmp.name) / "a.md")])
            self.assertEqual(rc, 0)
        finally:
            tmp.cleanup()
        changed = [p for p, d in before.items() if _md5(p) != d]
        self.assertEqual(changed, [], f"审计改动了输入文件：{changed}")


if __name__ == "__main__":
    unittest.main()
