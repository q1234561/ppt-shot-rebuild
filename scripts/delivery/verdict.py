# -*- coding: utf-8 -*-
"""verdict.py — 单页裁决产物（`_check/verdict.json`）的唯一生成处。

为什么要有它（2026-11-08）：
  「这页到底对不对」以前散在 7 份报告 + 一串终端输出里：
    look.json 只有 `fails` 字符串数组、io.json 有 mae/regions、presence.json 有
    errors、editability.json 有 suspect、plan.json 里的 spec_lint 条目还混在
    inventory 错误中间……谁来回答「能不能交付」得同时读 7 个文件并自己拼结论。
  于是同一页会出现两种说法（执行者看终端说做完了、复核者看报告说缺一块），
  而两种说法都能自证。这里把结论收成**一个文件、五条布尔**。

五条布尔**全部映射到已有判据**，这里不新造任何检查（也不许新造——新造一条就等于
给"看起来通过"多开一个入口）：

  text_ok        文字逐字正确/无遮挡/无截断   look.text_complete + spec_lint 文字压图
  text_is_text   每块字是真文本框           look.text_is_text + editability
  in_canvas      无越界                      look.no_overflow + spec_lint 出界
  no_missing     无整块缺失                  presence
  renders        能打开并重渲                look 渲染成功（图与当前成品同源）

  背景没有布尔（2026-11-08 删掉 `bg_parametric`，用户拍板）：它只认 `background.type=="solid"`，
  而本项目所有页都是渐变 → 这条**永远真空 PASS**，形同虚设。`look` 阶段对纯色背景的
  四角检查保留（纯色页上它是真判据），只是不再进裁决；"整页被贴成一张图"由
  `editability` 的"一张图盖住 ≥85% 画布"兜住。渐变页没有机器证据，就不给它一个假布尔。

两条口径纪律：
  ① **不许有数字**：五条布尔已覆盖全部量化结论，报告里不塞任何 0~255 的中间量
     （diff_t / peak / 阈值这些留在各自的报告里）。曾经留过一个 `mae` 标量
     （"这页离参考有多远"），它在第 ④ 步 4A 随像素相似度判据一起退役——
     读数没有唯一来源时，留在裁决文件里就是一行没人能复算的数字。
  ② **未证明 = false**：报告缺失、过期、渲染没跑，一律不是"通过"。五条布尔里没有
     "未知"这一态，所以缺证据必须落 false —— 这是本库一贯的方向（误拦可修、漏放毁整页）。

`preview` 模式也写这份文件，但带 `"mode": "preview"` 且**不参与裁决**：
它回答的是"现在离做完还差什么"，与交付结论无关（交付结论只在 delivery 模式给）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

# 五条布尔的名字与顺序 = verdict.json 的键序（下游按名字读，不按位置）。
# 2026-11-08 删掉 bg_parametric：它只认 solid 背景，本项目全渐变页 → 永远真空 PASS。
CHECK_KEYS = ("text_ok", "text_is_text", "in_canvas", "no_missing", "renders")

# 每条布尔的来源。写进 verdict.json 的 `source`，让人一眼能回到原判据去复核——
# 没有这一项，"5/5 通过"就只是一句自我声明。
SOURCE_OF = {
    # 2026-11-09 改造：这里原来写的是"哪两个脚本合起来算的"（look.text_complete+...），
    # 读起来像"两个像素量的交集"。现在写**期望值来自哪**——这是契约口径：
    #   内容（用户文案/声明）｜自洽（产物 vs 自己的 spec）｜环境（画布/底色）
    # 没有一条来自参考图的坐标或像素，这是"目标是否真换了"的可读证据。
    "text_ok": "内容：pptx 文字必须等于 spec 声明的文字（不是等于参考图上的字）",
    "text_is_text": "自洽：真文本帧 + 有可编辑 run，不是被贴成图",
    "in_canvas": "自洽：墨迹在自己的 bbox 内、bbox 在画布内",
    "no_missing": "自洽：spec 声明的每个 id 在成品里恰有一个同名对象",
    "renders": "自洽：能渲染，且尺寸 == spec.canvas（画布是环境量）",
}

# look.py 写进 look.json 的 `fails` 是 "[判据名] 说明" 形式（见 look.py 的 fails.append）。
# 裁决文件格式版本：6 条布尔 → 5 条（删 bg_parametric）时升到 verdict/2；
# verdict/3：删除 mae 键（像素相似度判据退役后它没有来源——那条判据不再参与裁决，
# 裁决文件里一个数字都不许留）。旧报告因此显式失效，不做静默兼容。
VERDICT_SCHEMA = "verdict/3"

_LOOK_TAG = re.compile(r"\s*\[([a-z_]+)\]")


def look_tags(look: dict | None) -> set[str]:
    """从 look.json 的 fails 里取出**判据名**集合（不含说明文字）。

    只读 `[tag]` 前缀，不嗅探说明里的中文：说明是给人看的，会随文案改；
    方括号里的判据名是 look.py 与这里之间的接口。
    """
    tags: set[str] = set()
    for f in ((look or {}).get("fails") or []):
        m = _LOOK_TAG.match(str(f))
        if m:
            tags.add(m.group(1))
    return tags


def spec_lint_hits(plan_errors) -> dict:
    """从 plan 报告的错误里取出 spec_lint 的两类（look 看不到、spec 层才判得出的那两条）。

    plan 闸（gates.check_plan）把 spec_lint 的每条 FAIL 记成
    `spec_lint: FAIL <原文>`，所以这里按「来源标记 + 判据关键词」两个条件取，
    取不到的类别保持 false（= 没有这条证据，不算失败）。

    为什么不用"再跑一遍 spec_lint"：那是同一个判据的第二份实现，
    两份实现迟早给出两个结论（本库最大的事故来源）。plan 报告已经写着结论，读它。
    """
    hit = {"overlap": False, "out_of_canvas": False}
    for e in plan_errors or []:
        s = str(e)
        if "spec_lint" not in s:
            continue
        if "文字压图" in s:
            hit["overlap"] = True
        if "出界" in s:
            hit["out_of_canvas"] = True
    return hit


def collect(*, look: dict | None = None, renders: bool = False,
            presence_ok: bool = False, editability_ok: bool | None = None,
            plan_errors=None) -> dict:
    """把五条布尔从已有报告收上来，返回 verdict.json 的内容（不含 mode）。

    参数就是证据本身，判定只在这里写一次——cycle 负责取数，本函数负责结论：

      look              look.json 的内容（fails）；None = 没有
      renders           look 是否**为当前成品**渲出了图（图与成品同源）
      presence_ok       presence 报告 current 且 pass
      editability_ok   editability 报告 current 且 pass；None = 这次没跑（preview）
      plan_errors      plan 报告里的错误串（只取 spec_lint 那两类）

    `renders` 是 look 五条的前置：图不是当前成品渲出来的，look 的结论就不属于这一版，
    所以它 false 时 look 派生的四条也一律 false（未证明 ≠ 通过）。
    """
    tags = look_tags(look)
    lint = spec_lint_hits(plan_errors)
    look_ev = bool(renders)          # look 的结论只对「当前成品渲出来的那张图」有效

    text_ok = look_ev and ("text_complete" not in tags) and not lint["overlap"]
    # editability 只在 delivery 跑；没跑（None）时这条只由 look 的文字帧判据支撑，
    # source 里写明，免得读报告的人以为它被"贴图检查"验证过。
    text_is_text = look_ev and ("text_is_text" not in tags) and (editability_ok is not False)
    in_canvas = look_ev and ("no_overflow" not in tags) and not lint["out_of_canvas"]

    source = dict(SOURCE_OF)
    if editability_ok is None:
        source["text_is_text"] = "look.text_is_text（本次未跑 editability）"

    return {
        "text_ok": bool(text_ok),
        "text_is_text": bool(text_is_text),
        "in_canvas": bool(in_canvas),
        "no_missing": bool(presence_ok),
        "renders": bool(renders),
        "source": source,
    }


def count_ok(v: dict) -> int:
    return sum(1 for k in CHECK_KEYS if v.get(k) is True)


def failed(v: dict) -> list[str]:
    """没通过的布尔名（顺序同 CHECK_KEYS），用于在终端点名。"""
    return [k for k in CHECK_KEYS if v.get(k) is not True]


def line(v: dict) -> str:
    """终端最后那一行：`verdict: 5/5 通过 · 交付 ready`（有假布尔就点名）。

    格式是契约（用例断言 `verdict: 5/5 通过` 这个子串），段落之间只用 ` · ` 分隔。
    曾经在 4 与 mae 之间插过 `mae 12.86`；像素相似度判据退役后那个数没有来源，整段删除，
    不留"mae 无"这种占位——一行里只允许出现可复算的结论。
    """
    head = f"verdict: {count_ok(v)}/{len(CHECK_KEYS)} 通过"
    bad = failed(v)
    s = head + (f" · 假: {','.join(bad)}" if bad else "")
    dv = v.get("deliverable")
    return s + (f" · 交付 {dv}" if dv else "")


# 交付结论的取值域（顺序 = 从好到坏）。
# 加它不是因为"多一行更安心"，是因为五条布尔**测不到**客户会踩的事：
#   ① 整块内容漏掉（实测：漏 3 块，verdict 仍 5/5 全 true —— presence 只比 id 集合，
#      报「有」不等于报「对」）
#   ② 某道闸没跑完（崩掉的闸不产出 false，它产出一个 error 阶段；布尔里没有这一态）
# 只要交付决策读布尔、不读这里，就会把残页发出去。
DELIVERABLE_VALUES = ("ready", "rework", "reject")


def deliverable_of(state: str, to_fix, checks: dict | None = None) -> tuple[str, list[str]]:
    """这页能不能交给客户 → 一个词 + 原因清单。

      reject   构建/检查阶段崩了，连成品都没有
      rework   跑完了但有阻塞项
      ready    五条全绿 + 无阻塞

    2026-11-08 删掉 `degraded`：它想表达"能交但没验全"，实际上等于给了一条
    "没验也能交"的合法通道。参照系（kimi-ppt-screenshot-rebuild 的
    compare_reference.py:71-82）做法是：未验证时 passed 写 None 而不是 True，
    而交付闸只认 True——**没验就是不通过**，不是"降级通过"。
    （第 ④ 步 4B：随自动收敛一起退役的那一档"没验完"已不存在，
    结论只剩"崩了 / 有阻塞 / 全绿"三种。）
    """
    blk = [f for f in (to_fix or []) if f.get("blocking", True)]
    why = [f"{f.get('stage')}: {f.get('message')}" for f in blk][:5]
    if state == "error":
        return "reject", why or ["阶段崩了：没有可看的成品"]
    if state == "blocked" or blk:
        return "rework", why or ["状态 blocked"]
    # 五条布尔是"判据通过"，ready 是"能不能交"——后者以前只读 state+to_fix，
    # 于是出现过 `1/5 通过 · 交付 ready`（2026-11-09 实测：渲染失败被当成通过）。
    # checks=None 表示调用方没有证据（preview 不写 deliverable），此时不追加判断，
    # 保持纯函数可单独调用；传了就必须全 true。
    if checks is not None:
        miss = [k for k in CHECK_KEYS if checks.get(k) is not True]
        if miss:
            return "rework", (why + [f"五条布尔未全过：{', '.join(miss)}"])[:5]
    return "ready", []


def write(check_dir, v: dict, mode: str) -> Path:
    """原子写 `_check/verdict.json`。

    `mode` 只在这里落地：preview 额外带 `"mode": "preview"`，因为同一份文件在两种模式
    下含义不同（preview = 现状快照，不参与裁决）；delivery 不带 `mode`，但多两个
    `deliverable` / `deliverable_why`（交付结论，见 `deliverable_of`）——所以
    **delivery 的键集合 = schema + 五条布尔 + source + deliverable + deliverable_why**，
    preview 的键集合 = 上面这些去掉后两项、再加 `mode`。两套键集合都由用例锁死，
    加键必须同时改用例与本节说明。

    `schema` 是**格式版本**（字符串，不是数字：verdict 里不许有数字——唯一那个 `mae`
    已随像素相似度判据退役）：布尔从 6 条减到 5 条时升到 `verdict/2`，删掉 `mae` 键时升到
    `verdict/3`；旧报告因此显式失效，不做静默兼容（少一条布尔、或带着一个没有来源的
    标量的旧结论，不许被当成同一件事读）。
    """
    doc = dict(v)
    doc["schema"] = VERDICT_SCHEMA
    if mode == "preview":
        doc["mode"] = "preview"
    p = Path(check_dir) / "verdict.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return p
