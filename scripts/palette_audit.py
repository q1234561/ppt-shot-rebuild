# -*- coding: utf-8 -*-
"""palette_audit.py — 色板审计：35 个 token 按**人眼能不能分辨**，应该合成几组。

为什么要做这件事（上一轮验收的结论是 `token 化 ≠ 统一`）：
  `visual.json` 的 palette 是从 46 页机械搬出来的，同一种颜色在测量时漂移成多个色号
  （`navy_900 #03288F` / `navy_900b #032C93` / `navy_900c #02319D`，`sky_100/120/150/200`
  是连续几档）。token 只是给"写死的值"换了个写法，**它本身不会减少色数**。
  真正的判据是：两个色号在**人眼里是不是同一个色**。这就是 ΔE 和高斯阈值要回答的问题。

为什么用 CIEDE2000 而不是 ΔE76（欧氏距离）：
  ΔE76 是 Lab 里的**等权**欧氏距离，没有明度/彩度/色相的权重归一，所以同一个数值在
  不同色区代表的"可辨程度"不同。实测本套 palette：两两 ΔE76/ΔE00 的比值中位数 1.8、
  范围 0.94~4.93（不是常数倍，不可线性换算）；把判据换成 ΔE76 而阈值仍填 2.3，
  在阈值 5.0 那一档会给出 22 组、而 ΔE00 给 15 组（`params.alt_metric_check` 里每次都重算）。
  2.3 这个 JND 是 **CIEDE2000 标度上的**常数，套到 ΔE76 上是量纲错误——所以判据只能是
  CIEDE2000（Sharma, Wu & Dalal 2005 的实现，无第三方依赖）。
  注意方向：ΔE76 多数情况下**更大**（更严），不是"更松所以乱合"；问题不是松紧，是不可比。

为什么阈值是 2.3 而不是"调到刚好 15 组"：
  2.3 是 JND（just-noticeable difference）的文献值，是**物理量**，不是调参旋钮。
  算出来几组就报几组。为了让别人能复算，`params` 里写清了色空间、白点、
  公式名、聚类算法与并列规则；报告文件里**不写时间戳**，同输入必得同输出。

为什么聚类必须"完全连接"：
  单连接（single linkage）会**串链**：A~B 近、B~C 近，于是 A 和 C 被并进一簇，
  哪怕 A 与 C 差得一眼能看出来。色板合并的判据是"簇内任意两色都不可分辨"，
  所以簇间距离必须取 **max**（complete linkage），不是 min。

为什么定序不能依赖 dict 顺序：
  这个仓库刚因为"顺序依赖"被打回过一次（同输入不同结论，报告不可复现）。
  本文件的聚类在入口就把 token 名排序，并列一律按名字定序，结果与
  `visual.json` 里的键序无关（`tests/test_palette_audit.py` 有反例钉住这条）。

**只读**：本脚本不写任何 `spec.json` / `visual.json`，只新增两个报告文件。
合并建议是**建议**，不是执行——改色号会改渲染像素，必须重渲染 + 重新验收。

用法：
    python scripts/palette_audit.py --visual D:/.../visual.json --root D:/.../ppt制作 \\
        --out-json D:/.../palette_audit.json --out-md D:/.../palette_proposal.md
    python scripts/palette_audit.py ... --threshold 3.0     # 改判据（≤0 或 >20 直接报错）
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path

# 与本仓库其它脚本共用 token 的**同一套**字面定义（`{...}` 整串才算 token）。
# 复制一份正则的代价是：specmerge 哪天改了 token 语法，这里会静默给出不同的用法统计。
import specmerge

# D65 白点（2° 观察者）。换白点必须同时改报告里的 params，否则别人复算不出同一个数。
WHITE_D65 = (95.047, 100.0, 108.883)
JND = 2.3                      # just-noticeable difference（CIEDE2000 标度下的文献值）
THRESHOLD_MIN_EXCL = 0.0       # 必须 > 0
THRESHOLD_MAX = 20.0           # 超过 20 已不是"不可分辨"，是"随便合"——不允许
DECIMALS = 2                   # 矩阵落盘的精度（两位足够复算，且不会因浮点末位抖动而变）

HEX6_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
PALETTE_TOKEN_PREFIX = "palette."


# ---------------------------------------------------------------- 色彩科学

def hex_to_rgb(value: str) -> tuple[float, float, float]:
    """`#RRGGBB` → (r, g, b)，分量 0..1。格式不对就 raise，不猜。

    宽松解析是这里最危险的一种"友好"：`#03288F` 写成 `03288F` 或 `#3288F`
    都能被宽松解析成某个颜色，而错的那个色会被当作事实进入报告。
    """
    s = (value or "").strip()
    if not HEX6_RE.match(s):
        raise ValueError(f"不是 #RRGGBB：{value!r}")
    return (int(s[1:3], 16) / 255.0, int(s[3:5], 16) / 255.0, int(s[5:7], 16) / 255.0)


def _srgb_to_linear(c: float) -> float:
    """sRGB 电信号 → 线性光。跳过这一步直接拿 0..1 当线性值，暗部会整体偏亮（深蓝尤其明显）。"""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """sRGB(0..1) → CIELAB（D65）。"""
    r, g, b = (_srgb_to_linear(c) for c in rgb)
    # sRGB(D65) → XYZ。矩阵给的是 Y∈0..1 的**归一化**值，而 D65 白点是 0..100 标度；
    # 这里统一乘 100 再除白点。忘了这一步的后果是 L* 被整体压掉一个数量级
    # （#FFFFFF 会算成 L*=9 而不是 100），ΔE 随之小到"什么都能合并"——不会报错，只会静默给出错结论。
    x = 100.0 * (0.4124564 * r + 0.3575761 * g + 0.1804375 * b)
    y = 100.0 * (0.2126729 * r + 0.7151522 * g + 0.0721750 * b)
    z = 100.0 * (0.0193339 * r + 0.1191920 * g + 0.9503041 * b)

    def f(t: float) -> float:
        # (6/29)^3 是 Lab 的分段点：更暗的用线性段，否则斜率在 0 附近发散
        return t ** (1.0 / 3.0) if t > 216.0 / 24389.0 else (t * 841.0 / 108.0) + 4.0 / 29.0

    fx, fy, fz = f(x / WHITE_D65[0]), f(y / WHITE_D65[1]), f(z / WHITE_D65[2])
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def hex_to_lab(value: str) -> tuple[float, float, float]:
    return rgb_to_lab(hex_to_rgb(value))


def delta_e_2000(lab1, lab2) -> float:
    """CIEDE2000（kL=kC=kH=1）。按 Sharma, Wu & Dalal (2005) 的公式写。

    这几处最容易写错，都按原文处理了：
      - C̄ 的 25^7 项要先算 G 再做 a' 修正，顺序反了就退化成 ΔE94；
      - h' 在 C'=0（灰/黑）时**不参与**平均，否则黑↔彩色的色相差会凭空出现一个数；
      - 平均色相 h̄' 要处理跨 0°/360° 的绕回，|h1-h2|>180 时不能直接取算术平均。
    """
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2

    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0
    g = 0.5 * (1.0 - math.sqrt(c_bar ** 7 / (c_bar ** 7 + 25.0 ** 7))) if c_bar > 0 else 0.0

    a1p, a2p = (1.0 + g) * a1, (1.0 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)

    # 色相角：C'=0 时定义为 0，且下面所有色相项都要按"无定义"跳过
    h1p = 0.0 if c1p == 0 else math.degrees(math.atan2(b1, a1p)) % 360.0
    h2p = 0.0 if c2p == 0 else math.degrees(math.atan2(b2, a2p)) % 360.0

    dlp = l2 - l1
    dcp = c2p - c1p

    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180.0:
        dhp = h2p - h1p
    elif h2p - h1p > 180.0:
        dhp = h2p - h1p - 360.0
    else:
        dhp = h2p - h1p + 360.0
    d_hp = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2.0))

    l_bar = (l1 + l2) / 2.0
    c_bar_p = (c1p + c2p) / 2.0

    if c1p * c2p == 0:
        h_bar_p = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        h_bar_p = (h1p + h2p) / 2.0
    elif h1p + h2p < 360.0:
        h_bar_p = (h1p + h2p + 360.0) / 2.0
    else:
        h_bar_p = (h1p + h2p - 360.0) / 2.0

    t = (1.0
         - 0.17 * math.cos(math.radians(h_bar_p - 30.0))
         + 0.24 * math.cos(math.radians(2.0 * h_bar_p))
         + 0.32 * math.cos(math.radians(3.0 * h_bar_p + 6.0))
         - 0.20 * math.cos(math.radians(4.0 * h_bar_p - 63.0)))

    d_theta = 30.0 * math.exp(-(((h_bar_p - 275.0) / 25.0) ** 2))
    rc = 2.0 * math.sqrt(c_bar_p ** 7 / (c_bar_p ** 7 + 25.0 ** 7)) if c_bar_p > 0 else 0.0
    sl = 1.0 + (0.015 * (l_bar - 50.0) ** 2) / math.sqrt(20.0 + (l_bar - 50.0) ** 2)
    sc = 1.0 + 0.045 * c_bar_p
    sh = 1.0 + 0.015 * c_bar_p * t
    rt = -math.sin(math.radians(2.0 * d_theta)) * rc

    tl, tc, th = dlp / sl, dcp / sc, d_hp / sh
    return math.sqrt(max(tl * tl + tc * tc + th * th + rt * tc * th, 0.0))


def delta_e_76(lab1, lab2) -> float:
    """只用于**自证不用它**（`params.alt_metric_check` 与测试里对比两种标度的分歧）。

    不参与任何判据：它是等权欧氏距离，与 CIEDE2000 不是常数倍关系
    （本套数据 0.94~4.93 倍），而 JND=2.3 只在 CIEDE2000 标度上有意义。
    """
    return math.dist(lab1, lab2)


# ---------------------------------------------------------------- 读 palette

def load_visual(path: Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到 visual.json：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def flatten_palette(visual: dict) -> dict[str, str]:
    """把 palette 拍平成 `{"palette.sky_120": "#D5E7F8"}`，键即 spec 里写的 token 名。

    两种历史形状都要认：本仓库是嵌套的 `{"palette": {"sky_120": ...}}`，
    而设计文档里描述的是**已经带前缀**的 `{"palette.sky_120": ...}`。
    只认一种的后果是"换一份 visual.json 就报 0 个 token"——那种静默的空结果最坑。

    非 `#RRGGBB` 的值一律**跳过**：palette 里可能混进 `null` 或非颜色常量，
    把它们当颜色会让 ΔE 矩阵里出现 NaN。
    """
    out: dict[str, str] = {}

    def walk(node, prefix: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                # 已经带 `palette.` 前缀的键（设计文档里的扁平写法）不再加第二层前缀，
                # 否则会同时产出 `palette.ink` 与 `palette.palette.ink` 两个 token，
                # 报告里凭空多出一倍，而且没人看得出是解析错了。
                if key.startswith(PALETTE_TOKEN_PREFIX):
                    walk(v, key)
                else:
                    walk(v, f"{prefix}.{key}" if prefix else key)
        elif isinstance(node, str):
            if HEX6_RE.match(node.strip()):
                out[prefix] = node.strip().upper()

    # 起点带上前缀：palette 内部第一层是 `sky_120`，token 名必须是 `palette.sky_120`。
    # 起点写成 "" 时所有键都会因为 startswith("palette.") 被过滤掉 → 静默得到 0 个 token。
    walk(visual.get("palette") or {}, PALETTE_TOKEN_PREFIX[:-1])
    return {k: v for k, v in out.items() if k.startswith(PALETTE_TOKEN_PREFIX)}


# ---------------------------------------------------------------- 用法统计

def find_pages(root: Path) -> list[str]:
    """整套目录下每个含 spec.json 的子目录 = 一页。

    跳过 `.` / `_` 开头：这套工作区里 `_archive` `_retired-*` `_fix-*` 都是退役快照，
    把它们数进来会让"某色被用了 3 次"变成"被用了 30 次"，而且没人会怀疑。
    """
    out = []
    for d in sorted(Path(root).iterdir(), key=lambda x: x.name):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        if (d / "spec.json").exists():
            out.append(d.name)
    return out


def _walk_colors(node, visit) -> None:
    """递归扫出所有颜色叶子：`{palette.x}` 与裸 `#RRGGBB` 都算。

    不按字段名白名单（fill / color / gradient / bg_color / glow.color …）过滤：
    字段是会长出来的（`shadow.color`、`line.color`、`duo_fg` 都是后加的），
    漏一个字段的后果是"这个色没人用"→ 代表色选错，而且报告看不出来。
    """
    if isinstance(node, dict):
        for v in node.values():
            _walk_colors(v, visit)
    elif isinstance(node, list):
        for v in node:
            _walk_colors(v, visit)
    elif isinstance(node, str):
        visit(node)


def _resolve_token(visual: dict, dotted: str):
    """`palette.sky_120` → 值。走不通返回 None（不 raise）。

    这里**容忍**解析失败，是因为统计面向的是"整套数据"，某页引用了尚未定义的 token
    应当被记成一条告警继续跑；而 specmerge.expand_tokens 面向构建，那里必须 raise。
    """
    cur = visual
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def collect_usage(visual: dict, root: Path, palette: dict[str, str],
                  visual_file: Path | None = None) -> dict:
    """统计每个色值被用了多少次、在哪几页。

    计数按**色值**归并，不按写法归并：token 与裸值指的是同一个颜色，
    "token 化前后渲染必须一致"这条契约要求它们在审计里也必须同权。
    否则"只统计 token 引用"会让还没 token 化的 8 个页面在报告里显示为 0 使用，
    代表色就被单页偏好决定——这类错误不报错，只给出错结论。

    `visual_file` 只用来告诉 load_merged 去哪找常量源；不传时 load_merged 会按
    "spec 同目录 → 上一级"自行查找（对临时目录的测试更方便）。

    返回值：
      usage[tok] = {hex, count, token_refs, bare_refs, pages, per_page, same_value_tokens}
      unmapped   = {色值: {count, pages}}  —— 调色板外的裸值（token 化缺口）
      per_page   = {页: {distinct_all, occurrences_all, distinct_unmapped, occurrences_unmapped}}
    """
    pages = find_pages(root)
    # 同一色值可能对应多个 token（实测 `blue_700` 与 `bg_grad_11` 都是 #0A55E0）。
    # 这种情况裸值的词频**同时**算给两个 token，且用 same_value_tokens 标出来，
    # 免得有人把两行次数相加当成总用量。
    value_tokens: dict[str, list[str]] = {}
    for tok, val in sorted(palette.items()):
        value_tokens.setdefault(val, []).append(tok)

    token_refs: dict[str, int] = {tok: 0 for tok in palette}
    bare_refs: dict[str, int] = {tok: 0 for tok in palette}
    value_count: dict[str, int] = {}
    value_pages: dict[str, set[str]] = {}
    value_per_page: dict[str, dict[str, int]] = {}
    unresolved: dict[str, int] = {}

    for page in pages:
        merged = specmerge.load_merged(Path(root) / page / "spec.json",
                                       quiet=True, expand=False,
                                       visual_path=visual_file)
        # 显式传 visual_file（不走模块级全局）：全局会让"用哪个 visual.json 统计"取决于
        # 上一次谁调过 audit()，是那种"单跑对、连跑错"的隐藏状态。

        def visit(s: str, page=page) -> None:
            s = s.strip()
            tok = None
            if specmerge._is_token(s):
                dotted = s[1:-1]
                resolved = _resolve_token(visual, dotted)
                if isinstance(resolved, str) and HEX6_RE.match(resolved.strip()):
                    val = resolved.strip().upper()
                    tok = dotted if dotted in palette else None
                    if tok is None:
                        unresolved[dotted] = unresolved.get(dotted, 0) + 1
                else:
                    return      # 非颜色的 token（字号 / 圆角），与本审计无关
            elif HEX6_RE.match(s):
                val = s.upper()
            else:
                return
            value_count[val] = value_count.get(val, 0) + 1
            value_pages.setdefault(val, set()).add(page)
            value_per_page.setdefault(val, {})
            value_per_page[val][page] = value_per_page[val].get(page, 0) + 1
            if tok is not None:
                token_refs[tok] += 1
            else:
                for t in value_tokens.get(val, []):
                    bare_refs[t] += 1

        _walk_colors(merged, visit)

    usage: dict[str, dict] = {}
    for tok, val in sorted(palette.items()):
        usage[tok] = {
            "hex": val,
            "count": value_count.get(val, 0),
            "token_refs": token_refs.get(tok, 0),
            "bare_refs": bare_refs.get(tok, 0),
            "pages": sorted(value_pages.get(val, set())),
            "per_page": dict(sorted(value_per_page.get(val, {}).items())),
            "same_value_tokens": [t for t in value_tokens.get(val, []) if t != tok],
        }

    unmapped = {}
    for val, n in sorted(value_count.items()):
        if val in value_tokens:
            continue
        unmapped[val] = {"count": n, "pages": sorted(value_pages.get(val, set()))}

    # 逐页统计：报告里凡是提到"某一页有多少种颜色"的数字都必须能从这里取到。
    # 之前这类数字只在我的口头汇报里出现，被转述时记成了错的（"page06 = 53 种"），
    # 而产物里没有对应字段可以核对——数字只有落在产物里才算数。
    per_page: dict[str, dict] = {}
    for page in pages:
        here = {val for val, ps in value_pages.items() if page in ps}
        here_unmapped = {val for val in here if val not in value_tokens}
        per_page[page] = {
            "distinct_all": len(here),
            "occurrences_all": sum(value_per_page.get(v, {}).get(page, 0) for v in here),
            "distinct_unmapped": len(here_unmapped),
            "occurrences_unmapped": sum(value_per_page.get(v, {}).get(page, 0)
                                        for v in here_unmapped),
        }

    return {
        "usage": usage,
        "unmapped": unmapped,
        "unresolved_tokens": dict(sorted(unresolved.items())),
        "pages": pages,
        "per_page": per_page,
    }


# 调用方（audit）负责把 visual_file 显式传进来；这里不再用模块级全局。


# ---------------------------------------------------------------- 聚类

def distance_matrix(names: list[str], labs: dict[str, tuple[float, float, float]]) -> dict:
    """对称矩阵。内部判据用**未截断**的浮点值，只在落盘时 round 到 2 位——
    先截断再比较会让"恰好 2.30001"变成"2.30 ≤ 2.30"，边界判断会随浮点末位抖动。"""
    return {a: {b: (0.0 if a == b else delta_e_2000(labs[a], labs[b])) for b in names}
            for a in names}


def cluster_complete_linkage(names: list[str], dist: dict, threshold: float) -> list[list[str]]:
    """完全连接凝聚聚类：簇间距离 = 两簇成员距离的 **max**，只合并 ≤ threshold 的对。

    合并顺序完全确定：
      1. 候选对按 (簇间距离, 两簇成员名排序后的元组) 取最小；
      2. 并列（距离完全相同）时按名字定序，不看插入顺序；
    所以同一份 palette 无论键序怎么打乱，划分结果都一样。

    退出条件是"再也找不到 ≤ threshold 的对"，即**簇内任意两色都不可分辨**；
    单连接会在 A~B、B~C 近而 A~C 远时把三者并成一簇（串链），本函数不会。

    **已知特性（不是 bug，但必须写进报告）**：完全连接只保证"同簇 ⇒ 两两 ≤ 阈值"，
    不保证反向。实测本套 palette：有 12 对 ΔE ≤ 2.3 的色被分进了不同簇
    （最极端的一对只有 0.63），因为它们各自的簇已经装不下对方（簇间 max 距离 > 阈值）。
    这会让报告里"最勉强的切分"看起来自相矛盾，所以 `audit()` 同时给出
    `cluster_clique_cover` 的对照，并在报告里明写这件事，而不是等读者自己发现。
    """
    clusters: list[list[str]] = [[n] for n in sorted(names)]
    while True:
        best_key = None
        best_ij = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                d = max(dist[a][b] for a in clusters[i] for b in clusters[j])
                if d > threshold:
                    continue
                key = (round(d, 9), tuple(sorted(clusters[i] + clusters[j])))
                if best_key is None or key < best_key:
                    best_key, best_ij = key, (i, j)
        if best_ij is None:
            break
        i, j = best_ij
        clusters[i] = sorted(clusters[i] + clusters[j])
        del clusters[j]
    return sorted((sorted(c) for c in clusters), key=lambda c: (-len(c), c[0]))


def cluster_clique_cover(names: list[str], dist: dict, threshold: float) -> list[list[str]]:
    """最大团贪心覆盖：按 token 名顺序，每个未分配的色当种子，把"加进来后整簇仍然两两 ≤ 阈值"
    的色依次拉进来。

    为什么需要它：合并的语义是"这些色在人眼里是同一个色"，也就是**簇必须是团**
    （任意两两 ≤ 阈值）。完全连接给出的划分是"团覆盖"里的一种，但**不是最优的**：
    它会把本可以同簇的近白族拆成两半（实测 ΔE=0.63 的一对被拆开），
    于是报告里"最勉强的切分"比"最勉强的合并"还小，读者会合理地质疑结论。

    为什么不做"按距离排序取最大团"：那是 NP-hard，而且结果会依赖距离排序的浮点末位。
    按名字贪心是确定的，且与完全连接只差 1~2 组（本套数据 25 → 24）。

    **为什么不把它当主判据（实测）**：贪心覆盖**不保证阈值单调**——
    阈值升高时贪心顺序会变，组数会反弹。本套 35 色在 0.1 步长下至少 4 处反弹：
    6.5→6.6（14→15 组）、8.1→8.2（13→14）、11.6→11.7（9→10）、12.7→12.8（9→10）。
    完全连接是单调的（阈值升高只可能少组，不可能多组），这是"判据"必须有的性质：
    否则"把阈值放松一点"反而得到更多色，没人能解释。所以本函数只用于**对照**，
    默认 `--algorithm complete`。
    """
    remaining = set(names)
    out: list[list[str]] = []
    for seed in sorted(names):
        if seed not in remaining:
            continue
        cluster = [seed]
        remaining.discard(seed)
        for cand in sorted(names):
            if cand not in remaining:
                continue
            if all(dist[cand][m] <= threshold for m in cluster):
                cluster.append(cand)
                remaining.discard(cand)
        out.append(sorted(cluster))
    return sorted(out, key=lambda c: (-len(c), c[0]))


ALGORITHMS = {
    "complete": cluster_complete_linkage,
    "clique": cluster_clique_cover,
}


def cluster(names: list[str], dist: dict, threshold: float, algorithm: str = "complete") -> list:
    try:
        fn = ALGORITHMS[algorithm]
    except KeyError:
        raise ValueError(f"未知聚类算法 {algorithm!r}；可选 {sorted(ALGORITHMS)}") from None
    return fn(names, dist, threshold)


def cross_cluster_pairs(names: list[str], dist: dict, groups: list[list[str]],
                        threshold: float) -> list[tuple[float, str, str]]:
    """落在不同簇、却 ΔE ≤ 阈值的色对（按 ΔE 升序）。

    这是"团覆盖"的**自检**：数量越少，划分越接近"同簇 ⇔ 不可分辨"的理想双向关系。
    有它才能诚实地回答"为什么这两个只差 0.6 却不在同一组"。
    """
    group_of = {m: gi for gi, g in enumerate(groups) for m in g}
    out = [(dist[a][b], a, b)
           for i, a in enumerate(names) for b in names[i + 1:]
           if group_of[a] != group_of[b] and dist[a][b] <= threshold]
    return sorted(out)


def cluster_max_delta(members: list[str], dist: dict) -> float:
    worst = 0.0
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            worst = max(worst, dist[a][b])
    return worst


def pick_representative(members: list[str], usage: dict, labs: dict) -> str:
    """代表色规则（报告里必须写清，否则下一个人不知道这个 token 是怎么选出来的）：

      1. 簇内**使用次数最多**的那个 token —— 合并后最多页面不用改；
      2. 次数并列时，在并列者中按 CIE **L\\* 中位**取（偶数个取偏暗的那个下中位）；
      3. 连 L* 都相同的（同色重复定义）按 token 名定序。

    为什么不是"取簇的质心色"：质心往往是一个**谁都没用过的**新色号，
    合并后每一页都要改，且新色号在原始素材里找不到参照，回归对比会全面失效。
    """
    order = sorted(members, key=lambda m: (-usage.get(m, {}).get("count", 0), labs[m][0], m))
    top_count = usage.get(order[0], {}).get("count", 0)
    tied = [m for m in order if usage.get(m, {}).get("count", 0) == top_count]
    if len(tied) == 1:
        return tied[0]
    by_l = sorted(tied, key=lambda m: (labs[m][0], m))
    return by_l[(len(by_l) - 1) // 2]


def _name_family(token: str) -> str:
    """`palette.navy_900b` → `navy`。用来标出"簇里混了不同命名族"的可疑分组。"""
    base = token.split(".", 1)[-1]
    for sep in ("_", "-"):
        if sep in base:
            return base.split(sep, 1)[0]
    return base


# ---------------------------------------------------------------- 主流程

def validate_threshold(value: float) -> float:
    """阈值是物理量，不做"修正"也不做兜底。

    静默夹到合法区间是最坏的一种处理：报出来的分组数对应的是**另一个**阈值，
    而报告上写的还是用户填的那个数，读到的人无从发现。
    """
    t = float(value)
    if not math.isfinite(t):
        raise ValueError(f"--threshold 必须是有限数：{value!r}")
    if t <= THRESHOLD_MIN_EXCL:
        raise ValueError(f"--threshold 必须 > {THRESHOLD_MIN_EXCL}（收到 {t}）：ΔE ≤ 0 只允许同色合并")
    if t > THRESHOLD_MAX:
        raise ValueError(f"--threshold 必须 ≤ {THRESHOLD_MAX}（收到 {t}）："
                         f"超过 JND 一个数量级已不是“不可分辨”，会把肉眼可分的色合掉")
    return t


def audit(visual_file: Path, root: Path, threshold: float,
          algorithm: str = "complete") -> dict:
    """跑一遍审计，返回可直接落盘的 payload。不写任何输入文件。"""
    visual_file = Path(visual_file)
    root = Path(root)
    threshold = validate_threshold(threshold)
    visual = load_visual(visual_file)

    palette = flatten_palette(visual)
    if not palette:
        raise ValueError(f"{visual_file} 里没有可用的 palette 颜色（要求 #RRGGBB）")
    names = sorted(palette)
    labs = {n: hex_to_lab(palette[n]) for n in names}
    dist = distance_matrix(names, labs)
    groups = cluster(names, dist, threshold, algorithm)
    # 两种算法都算一遍：差异（以及"有多少对不可分辨的色被分开了"）必须可见，
    # 否则读者只会看到一套数字，无从判断它是不是算法的产物。
    groups_alt = cluster(names, dist, threshold,
                         "clique" if algorithm == "complete" else "complete")
    alt_name = "clique" if algorithm == "complete" else "complete"

    stats = collect_usage(visual, root, palette, visual_file=visual_file)
    usage = stats["usage"]

    group_rows = []
    for idx, members in enumerate(groups, start=1):
        rep = pick_representative(members, usage, labs)
        outside = [o for o in names if o not in members]
        # 按**色值**去重后求和：同值多 token 时（`bg_grad_11` 与 `blue_700` 都是 #0A55E0）
        # 每个 token 的 count 都是"该色值出现处数"，直接 Σ 会把同一个色算两遍
        # （G05 曾因此报 8 处，而库里 #0A55E0 只有 4 处）。报告里"簇用量"就是这一列，
        # 它要能直接用来判断"改动面有多大"，所以必须是去重后的处数。
        value_counts: dict[str, int] = {}
        for m in members:
            value_counts[usage[m]["hex"]] = usage[m]["count"]
        group_rows.append({
            "id": f"G{idx:02d}",
            "members": members,
            "members_hex": [palette[m] for m in members],
            "max_delta": round(cluster_max_delta(members, dist), DECIMALS),
            "representative": rep,
            "representative_hex": palette[rep],
            "usage_total": sum(value_counts.values()),
            "name_families": sorted({_name_family(m) for m in members}),
            # 簇内最大 ΔE 说明"合得对不对"，簇外最小 ΔE 说明"该不该合得更多"。
            # 只看前者会漏掉"这一簇其实跟隔壁几乎同色"的情况。
            "min_cross_delta": (round(min(dist[m][o] for m in members for o in outside), DECIMALS)
                                if outside else None),
        })

    merged_away = len(names) - len(groups)

    # 自证"判据不是随便换的"：同一阈值下用 ΔE76 会得到什么。
    # 这段不进判据，只进报告——没有它，"为什么不用 ΔE76"就只是一句主张。
    # 注意：在**当前阈值**上两者很可能恰好一致（本套 2.3 处都是 25 组），
    # 那只是巧合（没有色对落进两种标度的分歧区），不能拿来当"可以换用 ΔE76"的证据。
    # 所以这里额外扫出**第一个分歧点**与**最大分歧**，让结论建立在"两者会分开"上。
    dist76 = {a: {b: (0.0 if a == b else delta_e_76(labs[a], labs[b])) for b in names}
              for a in names}
    groups76 = cluster(names, dist76, threshold, algorithm)
    ratios = [dist76[a][b] / dist[a][b]
              for i, a in enumerate(names) for b in names[i + 1:] if dist[a][b] > 0.5]
    divergence = []
    probe = 1
    while probe <= 200:
        t = round(threshold + probe / 10.0, 2)
        n00 = len(cluster(names, dist, t, algorithm))
        n76 = len(cluster(names, dist76, t, algorithm))
        if n00 != n76:
            divergence.append({"threshold": t, "groups_delta_e00": n00, "groups_delta_e76": n76})
        probe += 1
    alt_metric = {
        "metric": "ΔE76（等权欧氏距离，仅作对照，不参与判据）",
        "groups_at_same_threshold": len(groups76),
        "same_partition": sorted(sorted(g) for g in groups76) == sorted(sorted(g) for g in groups),
        "first_divergence": divergence[0] if divergence else None,
        "max_divergence": (max(divergence, key=lambda d: abs(d["groups_delta_e76"]
                                                             - d["groups_delta_e00"]))
                           if divergence else None),
        "ratio_over_0005": {
            "median": round(statistics.median(ratios), 2) if ratios else 0.0,
            "min": round(min(ratios), 2) if ratios else 0.0,
            "max": round(max(ratios), 2) if ratios else 0.0,
        },
    }

    # 结论对阈值有多敏感：围绕当前阈值向两侧走到"组数第一次变化"为止。
    # 这是"25 组"这个数字的误差棒——不写它，读者无法判断自己手上的数值鲁不鲁棒。
    # 两个坑都踩过：
    #   1) 步进必须用整数步数推进（t = threshold ± k*step），不能写 t -= 0.01 累减，
    #      二进制浮点累减会漂掉边界；
    #   2) 撞到"组数变了"的那一步时**不能**把它写进 lo/hi——lo/hi 是"仍然成立"的边界，
    #      变了的那一步要单独放 below/above。混在一起会报出 1.6~2.6（真值是 1.61~2.59），
    #      而且"再往外一步 → 26 组"这种句子会把 1.6 和 26 错配在一起。
    step = 0.01
    robustness = {"group_count": len(groups), "lo": threshold, "hi": threshold,
                  "step": step, "algorithm": algorithm,
                  "below": None, "above": None}
    k = 1
    while threshold - k * step > 0:
        t = round(threshold - k * step, 2)
        n = len(cluster(names, dist, t, algorithm))
        if n != len(groups):
            robustness["below"] = {"threshold": t, "group_count": n}
            break
        robustness["lo"] = t
        k += 1
    k = 1
    while threshold + k * step <= THRESHOLD_MAX:
        t = round(threshold + k * step, 2)
        n = len(cluster(names, dist, t, algorithm))
        if n != len(groups):
            robustness["above"] = {"threshold": t, "group_count": n}
            break
        robustness["hi"] = t
        k += 1

    # 两种聚类算法的差异 + "有多少对不可分辨的色被分进了不同簇"（团覆盖自检）。
    inconsistent = cross_cluster_pairs(names, dist, groups, threshold)
    inconsistent_alt = cross_cluster_pairs(names, dist, groups_alt, threshold)
    algorithm_check = {
        "primary": algorithm,
        "primary_group_count": len(groups),
        "primary_cross_cluster_pairs_le_threshold": len(inconsistent),
        "alternate": alt_name,
        "alternate_group_count": len(groups_alt),
        "alternate_cross_cluster_pairs_le_threshold": len(inconsistent_alt),
        "same_partition": sorted(sorted(g) for g in groups_alt) == sorted(sorted(g) for g in groups),
    }

    # 两个"最勉强的判断"：最接近阈值的合并 与 最接近阈值的切分。
    # 它们是这份报告里唯一需要人复核的两行——其余要么远在阈值内、要么远在阈值外。
    weakest_merge = None
    for g in groups:
        for i, a in enumerate(g):
            for b in g[i + 1:]:
                cand = (dist[a][b], a, b)
                if weakest_merge is None or cand[0] > weakest_merge[0]:
                    weakest_merge = cand
    group_of = {m: gi for gi, g in enumerate(groups) for m in g}
    closest_split = None
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if group_of[a] == group_of[b]:
                continue
            cand = (dist[a][b], a, b)
            if closest_split is None or cand[0] < closest_split[0]:
                closest_split = cand
    closest_calls = {
        "weakest_merge": None if not weakest_merge else {
            "a": weakest_merge[1], "b": weakest_merge[2],
            "delta": round(weakest_merge[0], DECIMALS)},
        "closest_split": None if not closest_split else {
            "a": closest_split[1], "b": closest_split[2],
            "delta": round(closest_split[0], DECIMALS)},
    }

    payload = {
        "params": {
            "threshold": threshold,
            "threshold_unit": "CIEDE2000 ΔE00（JND 文献值 2.3）",
            "color_space": "CIELAB, D65 白点, 2° 观察者",
            "conversion": "sRGB(0..1) → 线性光 → XYZ(D65, ×100 标度) → Lab（标准矩阵，分段 f(t)）",
            "algorithm": ("complete-linkage 凝聚聚类（簇间距离 = 成员 ΔE00 的 max），"
                          "只合并 ≤ threshold 的对；并列按成员名排序定序"
                          if algorithm == "complete" else
                          "最大团贪心覆盖（按 token 名顺序，簇内保持两两 ≤ threshold）"),
            "algorithm_key": algorithm,
            "delta_e": "CIEDE2000（Sharma, Wu & Dalal 2005），kL=kC=kH=1",
            "representative_rule": "簇内使用次数最多；并列时在并列者中取 CIE L* 中位"
                                   "（偶数取下中位）；仍并列按 token 名定序",
            "usage_rule": "按**色值**计数：{palette.x} 引用与裸 #RRGGBB 同权归并；"
                          "同色值多 token 时每个 token 的 count 都是该色值的出现处数，"
                          "簇用量已按色值去重（见 same_value_tokens）",
            "matrix_decimals": DECIMALS,
            "token_count": len(names),
            "group_count": len(groups),
            "merged_away": merged_away,
            "threshold_robustness": robustness,
            "alt_metric_check": alt_metric,
            "algorithm_check": algorithm_check,
            "closest_calls": closest_calls,
            "pages": stats["pages"],
            "per_page": stats["per_page"],
            "visual": str(visual_file),
            "root": str(root),
            "note": "同输入必得同输出（本文件不含时间戳）",
        },
        "order": names,
        "matrix": {a: {b: round(dist[a][b], DECIMALS) for b in names} for a in names},
        "groups": group_rows,
        # 全量列出：这是"为什么这两色只差 0.6 却不同组"的唯一证据链
        "inconsistent_pairs": [{"a": a, "b": b, "delta": round(d, DECIMALS)}
                               for d, a, b in inconsistent],
        "usage": usage,
        "unmapped_bare": stats["unmapped"],
        "unresolved_tokens": stats["unresolved_tokens"],
    }
    return payload


# ---------------------------------------------------------------- 报告

def render_md(payload: dict) -> str:
    p = payload["params"]
    groups = payload["groups"]
    usage = payload["usage"]
    n_tok = p["token_count"]
    n_grp = p["group_count"]
    unmapped = payload["unmapped_bare"]
    unmapped_occ = sum(v["count"] for v in unmapped.values())

    out: list[str] = []
    out.append("# 色板合并建议（palette audit）\n")
    out.append(f"**一句话结论**：`visual.json` 的 **{n_tok}** 个 palette token，"
               f"按 CIEDE2000 ΔE00 ≤ **{p['threshold']}**（JND）判定，"
               f"应合并为 **{n_grp}** 组；"
               f"{n_tok - n_grp} 个 token 可作为冗余移除。\n")
    out.append("> 本文件是**建议**，没有改动任何 `spec.json` / `visual.json` 的取值。"
               "合并会改变渲染像素，执行前需重渲染 + 重新验收。\n")

    out.append("## 判据与参数（可复算）\n")
    out.append("| 项 | 值 |")
    out.append("| --- | --- |")
    out.append(f"| 阈值 | {p['threshold']}（{p['threshold_unit']}） |")
    out.append(f"| 色彩空间 | {p['color_space']} |")
    out.append(f"| 转换 | {p['conversion']} |")
    out.append(f"| 色差公式 | {p['delta_e']} |")
    out.append(f"| 聚类算法 | {p['algorithm']} |")
    out.append(f"| 代表色规则 | {p['representative_rule']} |")
    out.append(f"| 用法统计规则 | {p['usage_rule']} |")
    out.append(f"| 扫描页面 | {len(p['pages'])} 页：{', '.join(p['pages'])} |")
    out.append("")
    out.append("阈值不是调参旋钮：2.3 是“刚能分辨”的物理量。"
               "把阈值调大到刚好得到某个好看的组数，等于把判据换成了目标，报告就失去意义。\n")

    rb = p["threshold_robustness"]
    out.append(f"**“{n_grp} 组”对阈值有多鲁棒**：以 {rb['step']} 为步长向两侧扫，"
               f"阈值落在 **{rb['lo']} ~ {rb['hi']}** 之间时组数都保持 {n_grp} 组"
               + (f"（再往外一步：{rb['below']['threshold']} → {rb['below']['group_count']} 组、"
                  if rb["below"] else "（下限已到 0 附近：")
               + (f"{rb['above']['threshold']} → {rb['above']['group_count']} 组）。"
                  if rb["above"] else "上限已到校验上界）。")
               + f"也就是说这个数字不是卡在悬崖边上的巧合——但也不能拿它去反推阈值。\n")

    alt = p["alt_metric_check"]
    calls = p["closest_calls"]
    r = alt["ratio_over_0005"]
    out.append("**为什么判据必须是 ΔE00，不能换成 ΔE76**\n")
    out.append(f"- 在当前阈值 {p['threshold']} 上，ΔE76 恰好也给出 "
               f"{alt['groups_at_same_threshold']} 组"
               + ("（划分相同）" if alt["same_partition"] else "（划分不同）")
               + "。**这是巧合**，不能当作“可以换用 ΔE76”的证据：它只说明本套色板"
                 "恰好没有色对落在两种标度的分歧区。")
    if alt["first_divergence"]:
        fd = alt["first_divergence"]
        out.append(f"- 往上走一格就分开了：阈值 **{fd['threshold']}** 时"
                   f"ΔE00 给 {fd['groups_delta_e00']} 组、ΔE76 给 {fd['groups_delta_e76']} 组；")
    if alt["max_divergence"]:
        md = alt["max_divergence"]
        out.append(f"- 分歧最大处在阈值 **{md['threshold']}**："
                   f"ΔE00 {md['groups_delta_e00']} 组 vs ΔE76 {md['groups_delta_e76']} 组。")
    out.append(f"- 根因是两种标度不成比例：两两 ΔE76/ΔE00 的比值中位数 {r['median']}、"
               f"范围 {r['min']}~{r['max']}（只统计 ΔE00>0.5 的对），不是常数倍、不可换算。")
    out.append(f"- 而 2.3 这个 JND **只在 CIEDE2000 标度上成立**。所以判据只能是 ΔE00。\n")

    out.append("## 分组表\n")
    out.append("| 簇 | 成员 token（色值出现处数） | 成员色 | 簇内最大 ΔE00 | 建议代表 | 簇用量（按色值去重） |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for g in groups:
        # 每个成员后面带**色值出现处数**（不是该 token 被引用的次数）：
        # 合并决策看的是"哪个用得最多"，只有簇合计看不出这件事。
        # 同值多 token 时这个数会重复出现两行，所以最后一列必须按色值去重——
        # 否则 G05（#0A55E0 出现 4 处）会被算成 8 处，直接误导"改动面有多大"的判断。
        members = "<br>".join(f"`{m}` {usage[m]['count']} 处" for m in g["members"])
        colors = "<br>".join(f"`{h}`" for h in g["members_hex"])
        rep = f"`{g['representative']}` {g['representative_hex']}"
        out.append(f"| {g['id']} | {members} | {colors} | {g['max_delta']} | {rep} | {g['usage_total']} |")
    out.append("")
    dup_groups = [g for g in groups if len(g["members"]) > 1
                  and len(set(g["members_hex"])) < len(g["members"])]
    if dup_groups:
        out.append(f"> 口径：成员后的「N 处」是**该色值在 9 页里出现的处数**，不是 token 被引用的次数；"
                   f"同一色值被定义成多个 token 时（"
                   + "、".join(f"{g['id']}" for g in dup_groups)
                   + "），这个数会在多行重复出现，而最后一列「簇用量」已按色值去重，"
                     f"所以**它小于等于成员数字之和**。\n")

    multi = [g for g in groups if len(g["members"]) > 1]
    single = [g for g in groups if len(g["members"]) == 1]
    # 字面同色（ΔE=0）才是最该先处理的一类：它不需要权衡、不需要重渲染，纯属重复定义
    literal_dup = [g for g in multi if g["max_delta"] == 0.0]
    out.append("## 为什么可以合并\n")
    if multi:
        example = calls["weakest_merge"] or {}
        ex_a = example.get("a", "").split(".", 1)[-1] if example.get("a") else ""
        ex_b = example.get("b", "").split(".", 1)[-1] if example.get("b") else ""
        out.append("同簇成员两两之间 ΔE00 都 ≤ 阈值，即在 **2° 视角下不可分辨**"
                   "（JND 的含义：并排对比时刚能被注意到的差别）。"
                   "把不可分辨的两个色号分开定义，收益是零，代价是："
                   "每加一个色号，就多一处需要人手确认"
                   + (f"“这一页该用 {ex_a} 还是 {ex_b}”，" if ex_a else "“这一页该用哪一个”，")
                   + "而这个问题**没有正确答案**——它们在人眼里是同一个色。\n")
        sizes = "/".join(f"{len(g['members'])}" for g in multi)
        out.append(f"本次有 **{len(multi)}** 个簇含 2 个以上 token（各簇成员数 {sizes}），"
                   f"合并后少 {n_tok - n_grp} 个定义。\n")
        if literal_dup:
            out.append("其中 "
                       + "、".join(f"**{g['id']}**" for g in literal_dup)
                       + " 的成员是**字面同色**（ΔE00 = 0，同一个 `#RRGGBB` 被定义了两遍）。"
                         "这一类不是“合并”，是**删重复**：不涉及任何像素变化，可以先做、零风险。\n")
    else:
        out.append("本次没有任何簇含两个以上 token——所有色号都超过了 JND。\n")

    out.append("## 为什么有些要保留\n")
    if single:
        far = [g for g in single
               if g["min_cross_delta"] is None or g["min_cross_delta"] > p["threshold"]]
        near = [g for g in single
                if g["min_cross_delta"] is not None and g["min_cross_delta"] <= p["threshold"]]
        out.append(f"{len(single)} 个 token 独占一簇。其中 **{len(far)}** 个与最近邻居的 "
                   f"ΔE00 也 > {p['threshold']}：它们是**真实存在的不同颜色**，不是测量漂移，"
                   "合并会让人眼可见的色相/明度变化进入渲染。\n")
        if far:
            out.append("真正不同色（保留）：" + "、".join(f"`{g['members'][0]}`" for g in far) + "\n")
        if near:
            # 这一句是"诚实"的关键：不把算法约束造成的拆分说成"色本来就不一样"
            out.append(f"另有 **{len(near)}** 个独占簇，与最近邻居的 ΔE00 ≤ {p['threshold']}，"
                       "却没有并进邻居的簇——原因是**那一簇装不下它**"
                       "（并进去会让簇内某个色对的 ΔE00 超过阈值），属于上节所说的“被拆开的近邻对”："
                       + "、".join(f"`{g['members'][0]}`（最近 {g['min_cross_delta']}）" for g in near)
                       + "。这些恰恰是“要不要更激进地合”的**唯一**可讨论空间。\n")

    if calls["weakest_merge"] or calls["closest_split"]:
        out.append("## 两个“最勉强”的判断（复核时只看这两行就够）\n")
        out.append("本节两个数直接取自 JSON 的 `params.closest_calls`，"
                   "**以产物为准**——口头转述过这两个数时曾出现过错值，"
                   "引用时请从这里复制，不要凭记忆复述。\n")
        wm = calls["weakest_merge"]
        cs = calls["closest_split"]
        if wm:
            ratio = wm["delta"] / p["threshold"] if p["threshold"] else 0.0
            out.append(f"- **最接近阈值的合并**：`{wm['a']}` ↔ `{wm['b']}`，"
                       f"ΔE00 = **{wm['delta']}**（占阈值的 {100.0 * ratio:.0f}%）。"
                       f"这一对是“合并”结论里最容易被推翻的：它离可分辨只差 {round(p['threshold'] - wm['delta'], 2)}。")
        if cs:
            if cs["delta"] <= p["threshold"]:
                # 这种情形必须说清楚，否则读者看到"最勉强的切分 = 0.63 < 阈值 2.3"会以为程序有 bug
                out.append(f"- **最接近阈值的切分**：`{cs['a']}` ↔ `{cs['b']}`，"
                           f"ΔE00 = **{cs['delta']}**。注意它**已经小于阈值**却被分在两簇——"
                           f"这不是判据失效，是“同簇必须两两都不可分辨”这条约束的必然结果"
                           f"（详见下一节）。")
            else:
                out.append(f"- **最接近阈值的切分**：`{cs['a']}` ↔ `{cs['b']}`，"
                           f"ΔE00 = **{cs['delta']}**（阈值的 {100.0 * cs['delta'] / p['threshold']:.0f}%）。"
                           f"这一对是“保留”结论里最容易被推翻的：再高 "
                           f"{round(cs['delta'] - p['threshold'], 2)} 就会被合成一组。")
        out.append("")

    ac = p["algorithm_check"]
    out.append("## 为什么有一对只差 0.6 却被分在两簇（算法自检）\n")
    out.append(f"本次主算法是 **{ac['primary']}**，给出 **{ac['primary_group_count']}** 组；"
               f"另一个算法 **{ac['alternate']}** 给出 **{ac['alternate_group_count']}** 组"
               + ("（划分相同）" if ac["same_partition"] else "（划分不同）") + "。\n")
    out.append(f"主算法下，有 **{ac['primary_cross_cluster_pairs_le_threshold']}** 对"
               f"“ΔE00 ≤ 阈值”（即人眼不可分辨）的色被分进了不同簇；"
               f"另一个算法下是 {ac['alternate_cross_cluster_pairs_le_threshold']} 对。"
               "这不是缺陷，是**团覆盖问题的固有性质**："
               "把不可分辨看成一张图上的边，它不满足传递性——A 像 B、B 像 C 时 A 与 C 未必像，"
               "所以任何“合法”的分组（每簇内部两两都不可分辨）都会拆掉一部分近邻对。"
               "完全连接的拆法更保守（拆得多），最大团覆盖的拆法更紧凑（拆得少）。\n")
    if payload.get("inconsistent_pairs"):
        out.append("主算法下被拆开的近邻对（全量）：")
        out.append("")
        for item in payload["inconsistent_pairs"]:
            out.append(f"- `{item['a']}` ↔ `{item['b']}`：ΔE00 = {item['delta']}")
        out.append("")
    out.append(f"→ 另一个算法**只作对照，不建议当判据**：贪心覆盖不保证阈值单调"
               f"（放松阈值时组数会反弹：本套 0.1 步长下至少 4 处，最早在 6.5→6.6 由 14 涨到 15），"
               f"而“放松阈值只会更少或一样多”是判据必须有的性质。"
               f"两者相差 {abs(ac['primary_group_count'] - ac['alternate_group_count'])} 组，"
               f"不影响“大多数 token 其实是真不同的颜色”这个结论。\n")

    out.append("## 预期收益与代价\n")
    out.append(f"**收益**\n")
    out.append(f"- 色板从 **{n_tok} → {n_grp}** 个定义，少 {n_tok - n_grp} 个"
               f"（减少 {100.0 * (n_tok - n_grp) / max(n_tok, 1):.0f}%）。")
    out.append("- “一套视觉只定义一次”从口号变成可核对的数字："
               "调色板条目数就是“这套设计里有几种颜色”的上界。")
    out.append("- 新增页面时的选色歧义消失：不存在“该用哪个 navy”这个问题。\n")
    out.append("**代价（必须一起说）**\n")
    out.append(f"- 合并会**改变渲染像素**。ΔE00 < {p['threshold']} 的部分人眼不可见，"
               "但**像素值确实变了**，所以：")
    out.append("  1. 所有引用被合并 token 的元素要改成代表色，然后**重渲染**；")
    out.append("  2. 重渲染后要**重新验收**（逐页对比 `reference.png`），"
               "不能拿“ΔE 很小”代替验收——ΔE 只保证这一对色的差别不可见，"
               "不保证改动没有波及别处（渐变端点顺序、透明度叠色都会放大差异）。")
    out.append("- 代表色选“用得最多的那个”而不是质心色，是为了让改动面最小；"
               "但**残余差异不会归零**，被合并掉的色号仍然会以代表色的形式出现在画面上。\n")

    if unmapped:
        out.append("## 范围外的现实：调色板没覆盖到的颜色\n")
        out.append(f"审计只读 palette，但同一份 spec 里还有 **{len(unmapped)}** 个"
                   f"**不在 palette 中**的裸色值，共出现 **{unmapped_occ}** 次。"
                   "这些色不在本次合并范围内，但它们说明："
                   "**token 化的覆盖面本身就是色数的主要来源**，"
                   "只合并 palette 里的 token 不会让这套 PPT 变成“一套视觉”。\n")
        top = sorted(unmapped.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:12]
        out.append("| 色值 | 次数 | 页数 |")
        out.append("| --- | --- | --- |")
        for hexv, info in top:
            out.append(f"| `{hexv}` | {info['count']} | {len(info['pages'])} |")
        out.append("")
        out.append("（上表只列前 12 个，全量见 JSON 的 `unmapped_bare`。）\n")

        # 逐页数字必须落在产物里：否则"某一页有几种颜色"只能靠口头转述，
        # 一经转述就可能变成一个查不到出处的数（曾出现"page06=53 种"这种错值）。
        out.append("**逐页明细（两种口径分别给，勿混用）**\n")
        out.append("| 页 | 全部色（含 palette） | 其中调色板外 |")
        out.append("| --- | --- | --- |")
        per_page = p["per_page"]
        for page in p["pages"]:
            s = per_page.get(page) or {}
            out.append(f"| {page} | {s.get('distinct_all', 0)} 种 / "
                       f"{s.get('occurrences_all', 0)} 处 | "
                       f"{s.get('distinct_unmapped', 0)} 种 / "
                       f"{s.get('occurrences_unmapped', 0)} 处 |")
        out.append("")
        out.append("口径：「种」= 去重后的色值个数；「处」= 出现次数（渐变端点、阴影色、描边色各算一处）。"
                   "「全部色」包含已在 palette 里的色，「调色板外」只算不在 palette 里的。"
                   "**引用本表数字时必须连口径一起引**——同一页在两种口径下差得很远。\n")

    if payload["unresolved_tokens"]:
        out.append("## 未解析的 token 引用\n")
        out.append("这些引用在 `visual.json` 里找不到（或是非颜色值），用法的统计不到，需要人工确认：\n")
        for k, v in payload["unresolved_tokens"].items():
            out.append(f"- `{{{k}}}` × {v}")
        out.append("")

    mixed = [g for g in groups if len(g["name_families"]) > 1]
    out.append("## 读这张表时要注意的三件事\n")
    if mixed:
        out.append("1. **名字不同族的簇需要人工确认意图**："
                   + "、".join(f"{g['id']}（{'/'.join(g['name_families'])}）" for g in mixed)
                   + "。名字族不同但 ΔE 达标，说明它们的**测量**是同一个色；"
                     "但如果它们在设计上承担不同职责（例如一个是文字墨色、一个是渐变端点），"
                     "合并后虽然看不出差别，语义上的区分会丢失。")
    else:
        out.append("1. 没有跨命名族的簇。")
    zero = [t for t, u in usage.items() if u["count"] == 0]
    if zero:
        out.append(f"2. **{len(zero)} 个 token 在 {len(p['pages'])} 页 spec 里一次都没被用到**"
                   + f"（{'、'.join(f'`{t}`' for t in zero[:8])}{'…' if len(zero) > 8 else ''}）"
                   + "。它们的代表色只能靠亮度中位决定，样本量为 0，"
                     "结论应当弱化：优先当作“可以直接删除的迁移残留”处理，而不是“合并”。")
    else:
        out.append(f"2. {n_tok} 个 token 在 {len(p['pages'])} 页 spec 里**全部被用到过**"
                   f"（无 0 用量项），所以代表色规则的第一条（取用量最多者）每次都能生效，"
                   f"不会退化到“按亮度中位猜”。")
    out.append("3. 用法次数按**色值**归并（token 引用与裸值同权）。"
               "如果某两个 token 的值完全相同（`same_value_tokens` 非空），"
               "它们的「N 处」是同一个计数的两行投影，**不要把这些行相加**；"
               "表格最后一列「簇用量」已经按色值去重，可以直接用来估改动面。\n")
    out.append("---\n")
    out.append(f"数据源：`{p['visual']}`；页面根目录：`{p['root']}`；"
               f"矩阵精度 {p['matrix_decimals']} 位；{p['note']}。\n")
    return "\n".join(out)


def write_outputs(payload: dict, out_json: Path, out_md: Path) -> None:
    """落盘。父目录不存在就建——CLI 的常见失败是路径少一级目录。"""
    out_json = Path(out_json)
    out_md = Path(out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.write_text(render_md(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="palette 审计：按 CIEDE2000 JND 给出合并建议（只读）")
    ap.add_argument("--visual", required=True, help="visual.json（palette 常量源）")
    ap.add_argument("--root", required=True, help="整套目录（每页一个子目录，含 spec.json）")
    ap.add_argument("--out-json", required=True, help="审计明细 JSON 输出路径")
    ap.add_argument("--out-md", required=True, help="合并建议 MD 输出路径")
    ap.add_argument("--threshold", type=float, default=JND,
                    help=f"CIEDE2000 ΔE00 阈值（默认 {JND} = JND；必须 >0 且 ≤{THRESHOLD_MAX}）")
    ap.add_argument("--algorithm", default="complete", choices=sorted(ALGORITHMS),
                    help="聚类算法：complete=完全连接（默认，阈值单调，可作判据）；"
                         "clique=最大团贪心覆盖（组数更少，但不保证阈值单调，只作对照）")
    args = ap.parse_args(argv)

    try:
        threshold = validate_threshold(args.threshold)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    try:
        payload = audit(Path(args.visual), Path(args.root), threshold, algorithm=args.algorithm)
    except (FileNotFoundError, ValueError) as exc:
        print(f"审计失败：{exc}", file=sys.stderr)
        return 2

    write_outputs(payload, Path(args.out_json), Path(args.out_md))

    p = payload["params"]
    print(f"palette token {p['token_count']} 个 → {p['group_count']} 组"
          f"（threshold ΔE00 ≤ {p['threshold']}，合并掉 {p['merged_away']} 个）")
    for g in payload["groups"]:
        if len(g["members"]) > 1:
            print(f"  {g['id']} maxΔE={g['max_delta']:<6} 代表 {g['representative']:<26} "
                  f"← {', '.join(g['members'])}")
    print(f"  单成员簇 {sum(1 for g in payload['groups'] if len(g['members']) == 1)} 个")
    alt = p["alt_metric_check"]
    print(f"  对照：同阈值下 ΔE76 给 {alt['groups_at_same_threshold']} 组"
          f"（划分{'相同' if alt['same_partition'] else '不同'}）")
    wm, cs = p["closest_calls"]["weakest_merge"], p["closest_calls"]["closest_split"]
    if wm:
        print(f"  最勉强的合并 {wm['a']} ↔ {wm['b']} = {wm['delta']}")
    if cs:
        print(f"  最勉强的切分 {cs['a']} ↔ {cs['b']} = {cs['delta']}")
    print(f"  调色板外裸色 {len(payload['unmapped_bare'])} 个"
          f"（{sum(v['count'] for v in payload['unmapped_bare'].values())} 次）")
    print(f"→ {args.out_json}")
    print(f"→ {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
