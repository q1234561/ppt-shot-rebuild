#!/usr/bin/env python3
"""lint_doc.py — 文档 ↔ 代码一致性检查（12 条）。

为什么需要它：文档容易写成"我当时想到的"，不是"代码能做到的"——
少写一条能力，下次就会用错；多写一条不存在的能力，跑起来才崩。
这类偏差没法靠人记，只能靠脚本对账。

判据：
  L1 SKILL 里出现的每个 mask 值 ⊆ fastcut.MASK_KINDS    （文档不能虚构能力）
  L2 fastcut.MASK_KINDS ⊆ SKILL 全文出现的词             （代码能力不能漏写）
  L3 SKILL 里出现的每个 shape 值 ⊆ build.SHAPES          （同上）
  L4 build.SHAPES 的每个键 ⊆ SKILL 全文出现的词          （同上）
  L5 SKILL 引用的每个 scripts/*.py 都真实存在            （断链）
  L6 没有枚举式措辞残留（"只有三件事" / "文字、卡片、图片"）——枚举必漏
  L7 没有废口径残留（被判死的旧说法，见 RETIRED_PHRASES）——同一件事不能两套说法
  L8 文档提到的每个 CLI 参数（--xxx）都真实存在于某个脚本的 argparse（删参数不忘改文档）
  L9 默认入口真的可执行：cycle / gates 的 --help
     必须列出文档依赖的关键参数（不是只查字符串存在）
  L10 SKILL 命令行的脚本-参数必须匹配到**那个脚本**（L8 是全库并集，会漏配错）
  L11 凡是调用 cycle.py 的代码块都不得**直接调用**辅助检查脚本（登记表见
      DIAGNOSTIC_SCRIPTS，当前为空）—— 防止把"按需诊断"又写回主链。
      判据与 --mode 无关：早先只扫「含 cycle.py 且含 --mode preview」的块，
      主链换成 --mode delivery 之后匹配块变成 0 个，闸空转成真空 PASS
      （2026-11-08 实测）。第 ④ 步 4B 之后诊断脚本全部退役，表为空 → 本闸恒过
  L12 打印出去的命令不得写死 python（必须用 {PY}/sys.executable）
      —— 2026-11-07 实测：cycle 打印的下一步命令写死了 PATH 上的解释器，
      本机那个没装 python-pptx，照抄必崩，且崩进 presence 的解包 bug 里

用法：
  python scripts/lint_doc.py                 # 检查本 skill
  python scripts/lint_doc.py --skill X.md    # 检查指定文档（负向测试用）

退出码：0 全过，1 有 FAIL。
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build as B          # noqa: E402  SHAPES 是唯一真值
import fastcut as F        # noqa: E402  MASK_KINDS 是唯一真值

# 枚举式措辞：一旦出现，说明又在用"举例"代替"判据"
BANNED_PHRASES = ["只有三件事", "文字、卡片、图片"]

# 废口径：已被实测判死的旧说法，不许再回到 SKILL 里（改文档时
# "新段落写对、旧段落留着错"是默认结局）。只查 SKILL.md，CHANGELOG 不拦。
RETIRED_PHRASES = {
    "2min/块": "cutout 实测 175~206s → 写 3min/块",
    "单行可信": "可信与否按字数（≤15 字），不按行数",
    # 2.15+ 术语：测量工具不再叫闸门（闸只有 gates.py 的阶段），交付验收不再叫"唯一验收"
    "结构闸门": "改叫「结构检查」（测量不阻塞）",
    "跨页闸门": "改叫「跨页检查」",
    "素材闸门": "改叫「素材预测」",
    "可编辑闸门": "改叫「可编辑检查」",
    "唯一验收": "改叫「回归测试」（它是 spec 生成器往返一致性，不是交付验收）",
    "阴影不写": "改叫「阴影默认不写」（build 已支持 shadow，绝对禁止与能力矛盾）",
    # 2.26：旧合同已被依赖键取代
    "忽略字距": "字距照用（build 两档共用同一份 spec）",
    "闸门只读产物、不渲染": "闸只消费有效报告；需要渲染的必需报告由 cycle 准备",
    "最多 3 版": "通常不超过 3 版（返工预警，不是硬合格条件）",
    "最多3版": "通常不超过 3 版（返工预警，不是硬合格条件）",
}

# L9：默认入口的关键参数（缺一个就说明文档写的命令跑不通）
ENTRY_CONTRACT = {
    "scripts/delivery/cycle.py": ["--page", "--mode", "--adopt-assets", "--out"],
    "scripts/delivery/gates.py": ["--stage", "--page", "--out", "--rendered", "--produce"],
}

# L11 的登记表：哪些脚本算"只经主链按需拉、不许在文档里直接调"的辅助检查。
# 第 ④ 步 4B 之后两个诊断脚本都已退役（连同两个调用它的开关），表为空 → L11 恒过。
# 保留这段与 L11 本身，是为了下次有人再往主链塞诊断脚本时，必须先在这里登记、再被判红。
DIAGNOSTIC_SCRIPTS: tuple[str, ...] = ()


def words_in(text: str, k: str) -> bool:
    """k 是否作为独立词出现（避免 rect 命中 direct 之类）。"""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(k)}(?![A-Za-z0-9_])", text) is not None


def _flags(py: Path) -> set[str]:
    return set(re.findall(r'add_argument\(\s*"(--[a-z][a-z0-9-]*)"',
                          py.read_text(encoding="utf-8")))


def _code_blocks(text: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.S)


def _code_units(text: str) -> list[tuple[str, str]]:
    """文档里的"代码"单元：围栏块 **和** 内联 ``...`` 都算，带位置标签。

    为什么要内联：本 SKILL 把主链命令写在表格与项目符号的内联代码里，围栏块
    只有 2 个且不含 cycle.py——只扫围栏块的判据在这个文档上恒为 0 命中，
    等于没查（L11 就是为了不再出现这种空转）。
    """
    units = [(f"第 {i} 个围栏代码块", b)
             for i, b in enumerate(_code_blocks(text), 1)]
    for m in re.finditer(r"`([^`\n]+)`", text):
        ln = text.count("\n", 0, m.start()) + 1
        units.append((f"第 {ln} 行内联代码", m.group(1)))
    return units


def _hardcoded_python(py: Path) -> list[str]:
    """L12：f-string 里写死 python 的位置（会打印/拼给人抄的那种）。

    只扫 ast.JoinedStr：纯字符串常量是 docstring 与用法示例，扫它会把 20+ 条
    合法示例判红（实测 24 处）。三条同时满足才算违规：
      - 出现 python / python3 / python.exe（后面不能接 -，避开 python-pptx）
      - 出现 .py
      - 没有 {PY} / {sys.executable} / $PY（有就是正确写法）
    """
    src = py.read_text(encoding="utf-8")
    try:
        rel = py.resolve().relative_to(HERE.parent.resolve()).as_posix()
    except ValueError:
        rel = py.name
    hits: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.JoinedStr):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if not re.search(r"\bpython3?(?:\.exe)?\b(?!-)", seg):
            continue
        if ".py" not in seg:
            continue
        if any(k in seg for k in ("{PY}", "sys.executable", "$PY")):
            continue
        hits.append(f"{rel}:{node.lineno}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", default=str(HERE.parent / "SKILL.md"))
    a = ap.parse_args()
    doc = Path(a.skill)
    if not doc.exists():
        print(f"FAIL 文档不存在：{doc}")
        return 1
    text = doc.read_text(encoding="utf-8")
    fails, lines = [], []

    # L1 / L2
    used_masks = set(re.findall(r'mask"?\s*:\s*"([a-z_]+)"', text))
    unknown = sorted(used_masks - set(F.MASK_KINDS))
    if unknown:
        fails.append(f"L1 文档写了代码没有的 mask：{unknown}（代码只有 {list(F.MASK_KINDS)}）")
    missing = [k for k in F.MASK_KINDS if not words_in(text, k)]
    if missing:
        fails.append(f"L2 代码有但文档没写：{missing}（能力没进文档 = 下次必然用错）")
    lines.append(f"  L1 mask 值 {len(used_masks)} 个，未知 {len(unknown)}")
    lines.append(f"  L2 代码 mask {len(F.MASK_KINDS)} 种，文档缺 {len(missing)}")

    # L3 / L4
    used_shapes = set(re.findall(r'shape"?\s*:\s*"([a-z_]+)"', text))
    unknown_s = sorted(used_shapes - set(B.SHAPES))
    if unknown_s:
        fails.append(f"L3 文档写了代码没有的 shape：{unknown_s}（代码只有 {list(B.SHAPES)}）")
    missing_s = [k for k in B.SHAPES if not words_in(text, k)]
    if missing_s:
        fails.append(f"L4 代码有但文档没写：{missing_s}")
    lines.append(f"  L3 shape 值 {len(used_shapes)} 个，未知 {len(unknown_s)}")
    lines.append(f"  L4 代码 shape {len(B.SHAPES)} 种，文档缺 {len(missing_s)}")

    # L5
    # 必须带上子目录：原来只认 `scripts/xxx.py`，把 `scripts/delivery/*.py` 全漏了，
    # 文档里引用一个不存在的 delivery 脚本也照样绿（L5 等于空转）。
    refs = sorted(set(re.findall(r"scripts/([\w/]+\.py)", text)))
    gone = [r for r in refs if not (HERE / r).exists()]
    if gone:
        fails.append(f"L5 文档引用了不存在的脚本：{gone}")
    lines.append(f"  L5 引用脚本 {len(refs)} 个，缺失 {len(gone)}")

    # L6
    hit = [p for p in BANNED_PHRASES if p in text]
    if hit:
        fails.append(f"L6 枚举式措辞残留：{hit}——枚举必漏，改成判据")
    lines.append(f"  L6 禁用措辞 {len(hit)} 处")

    # L7
    retired = [p for p in RETIRED_PHRASES if p in text]
    if retired:
        fails.append("L7 废口径残留：" + "；".join(f"{p}（应改为 {RETIRED_PHRASES[p]}）" for p in retired))
    lines.append(f"  L7 废口径 {len(retired)} 处")

    # L8 文档提到的 CLI 参数必须真实存在。
    declared = set()
    for py in sorted(list(HERE.glob("*.py")) + list(HERE.glob("delivery/*.py"))):
        declared |= _flags(py)
    doc_args = set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", text))
    ghost = sorted(a for a in doc_args if a not in declared)
    if ghost:
        fails.append(f"L8 文档提到不存在的 CLI 参数：{ghost}")
    lines.append(f"  L8 CLI 参数 {len(doc_args)} 个，不存在 {len(ghost)}")

    # L9 默认入口可执行（--help 真的列出关键参数）
    for rel, need in ENTRY_CONTRACT.items():
        py = HERE.parent / rel
        try:
            r = subprocess.run([sys.executable, str(py), "--help"],
                               capture_output=True, text=True, timeout=60)
            out = (r.stdout or "") + (r.stderr or "")
        except Exception as exc:
            fails.append(f"L9 {rel} 无法执行 --help：{exc}")
            continue
        absent = [n for n in need if n not in out]
        if r.returncode != 0 or absent:
            fails.append(f"L9 {rel} 的 --help 缺关键参数 {absent}（exit={r.returncode}）")
    lines.append(f"  L9 默认入口 {len(ENTRY_CONTRACT)} 个，已执行 --help")

    # L10 命令行里的脚本-参数必须匹配到那个脚本
    wrong = []
    for block in _code_blocks(text):
        for m in re.finditer(r"\$(?:S|D)/([a-z0-9_]+\.py)([^\n]*)", block):
            name, tail = m.group(1), m.group(2)
            py = HERE / name if (HERE / name).exists() else HERE / "delivery" / name
            if not py.exists():
                continue          # L5 已报
            ok = _flags(py)
            for flag in re.findall(r"(--[a-z][a-z0-9-]*)", tail):
                if flag not in ok:
                    wrong.append(f"{name} {flag}")
    if wrong:
        fails.append(f"L10 命令行把参数配错了脚本：{sorted(set(wrong))}")
    lines.append(f"  L10 命令行参数错配 {len(wrong)} 处")

    # L11 主链不得混入辅助检查脚本。判据与 --mode 无关：凡是调 cycle.py 的代码单元就是主链，
    # delivery / preview 都覆盖（旧版只扫「含 cycle.py 且含 --mode preview」的围栏块，
    # 主链换成 --mode delivery 之后命中数变成 0，闸空转成真空 PASS）。
    # 登记表 DIAGNOSTIC_SCRIPTS 现在为空（两个诊断脚本已在第 ④ 步 4B 退役）→ leak 恒为 0。
    main_units = [(k, u) for k, u in _code_units(text) if "cycle.py" in u]
    leak = []
    for where, unit in main_units:
        for d in DIAGNOSTIC_SCRIPTS:
            if re.search(rf"(?<![\w.-]){re.escape(d)}", unit):
                leak.append(f"{where}（{d}）")
    if leak:
        fails.append("L11 主链代码混入辅助检查脚本：" + "；".join(sorted(set(leak)))
                     + "——按需诊断只能经主链统一入口，或放进单独小节")
    lines.append(f"  L11 含 cycle.py 的代码单元 {len(main_units)} 个，直接调诊断 {len(set(leak))} 处")

    # L12 打印出去的命令不许写死 python
    hard = []
    for py in sorted(list(HERE.glob("*.py")) + list(HERE.glob("delivery/*.py"))):
        hard += _hardcoded_python(py)
    if hard:
        fails.append(f"L12 打印命令写死了 python：{hard}——用 sys.executable/命令渲染函数")
    lines.append(f"  L12 打印命令解释器 违规 {len(hard)} 处")

    print(f"lint_doc: {doc}")
    for ln in lines:
        print(ln)
    print()
    if fails:
        for f in fails:
            print(f"FAIL {f}")
        return 1
    print(f"PASS 文档与代码一致（{len(lines)}/{len(lines)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
