# -*- coding: utf-8 -*-
"""specmerge.py — spec 与 override 的合并/差分（唯一实现，build 与收敛值回写共用）。

为什么要拆成两个文件（2.4.5 事故的结构性修法）：
  spec.json 原来是三个写者：extract.py 生成、人改坐标、收敛值回写。
  事故链：extract 重跑 → 把人和收敛值（含 assets[*].src）一起冲掉
  → 7 张图全丢，而 build 静默跳过、look 数的是"有 src 的 assets"，两道闸同时放行。

  现在每个文件只有一个写者：
    spec.json      只由 extract.py 写（生成物，可随时重建，重建＝回到初值）
    override.json  只由收敛值回写（溢出的字号求解）与人写（src/手调 bbox）产生
    build.py       只读，合并两者后构建，不写回任何文件

  「重跑生成器会把我的修改冲掉」这件事从流程上不再可能发生，而不是靠提醒。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

SECTIONS = ("containers", "assets", "elements")
SCALARS = ("background",)
# 合并 override 时给这两个字段打来源标记（值语义不变，只加元数据）。
# 其余字段没有"测量"语义，不需要来源。
SRC_TAGGED_FIELDS = ("size_pt", "weight")
DEFAULT_DPI = 125.4        # 只在 spec 没写 canvas.dpi 时兜底


def load(path: Path | None) -> dict:
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_merged(spec_path, override_path=None, quiet: bool = False,
                expand: bool = False, visual_path=None) -> dict:
    """读 spec + 同目录 override，返回**合并后**的视图。

    **所有读 spec 的工具都必须走这里**（build / spec_lint / assetcheck /
    inventory / deck / deck_lint / gates / cycle）。
    否则会出现最隐蔽的一种不一致：build 按修正后的坐标画，量测按初值坐标量，
    两边都"自洽"，报出来的 dy/dx 全是假的，而且没人会怀疑。
    约定：override.json 必须与 spec.json 同目录，不提供路径参数——少一个能配错的地方。

    只做一件事：**合并 override**（值语义：几何、素材 src、字号的唯一真相）。
    它只动**返回值**，spec.json 一个字节都不写。

    `expand`（P3 双视图，默认 **False**）：
      - `False` = **raw 视图**：token 原样保留（`"{palette.ink}"` 还是字符串）。
        闸门必须看这份——否则 `spec_lint` 的"裸值 = 0"会永远读不到裸值，判据自毁（D11）。
      - `True` = 展开视图：把整串 token 换成 `visual.json` 里的值，只喂 `build`。
    展开前后的**渲染必须一致**：token 只是把"写死的值"换成"引用同一个值"。

    没有第二步：档表 / 阶梯解析已整体删除（每页风格不同，档表不可复用）。
    字号必须由 spec.json 或 override.json 明确给出——缺就是错误，
    build 会 REFUSED 并点名所有缺字号的块（不再有 14pt / 18pt 的静默默认值）。
    """
    sp = Path(spec_path)
    spec = json.loads(sp.read_text(encoding="utf-8"))
    ovp = Path(override_path) if override_path else sp.parent / "override.json"
    ov = load(ovp)
    # 统一走 apply()：它恒深拷贝（入参 spec 不被改），并顺手补 freeform 的 bbox。
    # 原来"无 override 就直接 return spec"的快路径少走 derive_bbox，于是同一种输入
    # 在"有 override"和"没 override"两条路上给出不同视图 —— 正是本文件要消灭的那类不一致。
    merged, st = apply(spec, ov)
    if expand and _has_token(merged):
        # 没 token 的页**不需要** visual.json：老页面 / 单元测试里的临时 spec 都属这类。
        # 反过来（无条件要求 visual）会把"没做 token 化"变成"构建失败"，
        # 那是把可选的迁移步骤做成了硬前置。
        merged = expand_tokens(merged, load_visual(visual_path, sp.parent))
    if ov and not quiet:
        tail = f"，未命中 {len(st['miss'])} 项（{', '.join(st['miss'][:3])}）" if st["miss"] else ""
        print(f"  OVERRIDE {st['hits']} 项 ← {ovp.name}{tail}")
    return merged


def load_visual(visual_path=None, spec_dir: Path | None = None) -> dict:
    """找 `visual.json`：显式路径 → spec 同目录 → 上一级（整套目录）。

    找不到就**报错**，不返回空 dict——空 dict 会让所有 token 变成"未知"，错误信息会指向
    "token 不存在"，而真实原因是"文件不在"，那是两种完全不同的修法。
    """
    cands = []
    if visual_path:
        cands.append(Path(visual_path))
    if spec_dir:
        cands += [Path(spec_dir) / "visual.json", Path(spec_dir).parent / "visual.json"]
    for c in cands:
        if c.exists():
            return json.loads(c.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        f"找不到 visual.json（试过 {[str(c) for c in cands]}）；"
        f"要展开 token 必须先有常量源")


def _dig(visual: dict, dotted: str):
    cur = visual
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur


TOKEN_PREFIX, TOKEN_SUFFIX = "{", "}"


def _is_token(v) -> bool:
    """只认**整串**是 token 的字符串。

    不做"字符串里插 token"（`"{size}/2"` 这种）：一混进拼接就没人能静态判断值是多少，
    裸值闸门也会跟着失效。要算就让人先算好再写。
    """
    return (isinstance(v, str) and len(v) > 2
            and v.startswith(TOKEN_PREFIX) and v.endswith(TOKEN_SUFFIX)
            and TOKEN_PREFIX not in v[1:-1] and TOKEN_SUFFIX not in v[1:-1])


def _has_token(view: dict) -> bool:
    """这一份视图里有没有 token。只用于决定"要不要去找 visual.json"。"""
    found = False

    def walk(n):
        nonlocal found
        if found:
            return
        if isinstance(n, dict):
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
        elif _is_token(n):
            found = True

    for sec in SECTIONS:
        walk(view.get(sec))
    for key in SCALARS:
        walk(view.get(key))
    return found


def expand_tokens(view: dict, visual: dict) -> dict:
    """把整串 token 换成 visual 里的值（在 SECTIONS + SCALARS 范围内，递归到标量）。

    未知 token 一律 **raise**，不做"原样保留"：原样保留会让 build 拿到 `"{palette.ink}"`
    这种字符串，最后在 write_color 里炸出一句莫名其妙的错误；在展开点报错，能直接指到
    `元素 id / 字段 / token 名`。
    """
    out = copy.deepcopy(view)
    unresolved: list[str] = []

    def walk(node, where):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if _is_token(v):
                    try:
                        node[k] = copy.deepcopy(_dig(visual, v[1:-1]))
                    except KeyError:
                        unresolved.append(f"{where}.{k} → {v}")
                else:
                    walk(v, f"{where}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if _is_token(v):
                    try:
                        node[i] = copy.deepcopy(_dig(visual, v[1:-1]))
                    except KeyError:
                        unresolved.append(f"{where}[{i}] → {v}")
                else:
                    walk(v, f"{where}[{i}]")

    for sec in SECTIONS:
        for i, el in enumerate(out.get(sec) or []):
            walk(el, f"{sec}[{i}]({el.get('id', '?')})")
    for key in SCALARS:
        if key in out:
            walk(out[key], key)

    if unresolved:
        raise ValueError("未知 token（visual.json 里没有对应路径）：\n  " + "\n  ".join(unresolved[:10]))
    return out


def derive_bbox(el: dict) -> dict:
    """freeform 只给 points，但下游（build / spec_lint / presence）一律按 bbox 取值。
    在这里补一次，比在每个下游各写一段"没有 bbox 就用 points 算"安全。"""
    if el.get("shape") == "freeform" and not el.get("bbox"):
        pts = el.get("points") or []
        if len(pts) >= 3:
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            el["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
    return el


def apply(spec: dict, ov: dict) -> tuple[dict, dict]:
    """把 override 盖到 spec 上（不改入参）。返回 (合并结果, 统计)。

    override 有两种作用：`patch`（改已有元素的字段，按 id）与 `add`（新增元素，按 id 去重）。
    只有 patch 的话，"人补了一个容器"这种最常见的改动就无处安放——会被静默丢掉，
    所以 add 是一等公民，不是补丁。
    """
    out = copy.deepcopy(spec)
    if not ov:
        for sec in SECTIONS:
            for el in out.get(sec, []) or []:
                derive_bbox(el)
        return out, {"hits": 0, "added": 0, "miss": []}
    idx = {}
    for sec in SECTIONS:
        for el in out.get(sec, []):
            if el.get("id"):
                idx[el["id"]] = el
    hits, added, miss = 0, 0, []
    # 顺序必须是 add 先、patch 后：patch 段按 id 改字段，而"只存在于 add 里的元素"
    # 只有先挂进 idx 才可能被 patch 命中。反过来（2.17.3 及以前）会静默 miss ——
    # fastcut/cutout 正是把素材 src 写进 patch 段，于是新页每个素材的 src 都生效不了，
    # 下游 cutout 认不出"已抠过"→ 全部重抠并覆盖色键结果（2026-11-07 实测，单页多花 8~10 分钟）。
    for sec in SECTIONS:
        for el in (ov.get("add", {}) or {}).get(sec, []) or []:
            eid = el.get("id")
            if not eid or eid in idx:
                if eid in idx:
                    miss.append(f"add/{sec}/{eid}（id 已存在，跳过）")
                continue
            new = copy.deepcopy(el)
            # 新增元素整块来自 override，所以它带的这两个字段同样是"override 提供的值"
            for k in SRC_TAGGED_FIELDS:
                if k in new:
                    new[f"{k}_src"] = "override"
            out.setdefault(sec, []).append(new)
            idx[eid] = new
            added += 1
    for sec in SECTIONS:
        for eid, fields in (ov.get(sec) or {}).items():
            el = idx.get(eid)
            if el is None:
                miss.append(f"{sec}/{eid}")      # id 打错或元素已删：报出来，不静默丢
                continue
            for k, v in fields.items():
                if k == "bbox" and isinstance(v, list):
                    el[k] = [float(x) for x in v]
                else:
                    el[k] = copy.deepcopy(v)
                # 来源标记：这两个字段的**值本身没变**，但它们不再等于"extract 测出来的"。
                # 下游据此区分
                # "页面写的观测值"与"人后来盖上去的值"，所以只给这两个字段打标，
                # 且只在该字段确实由 override 提供时才打——没被覆盖的元素保留自己的 *_src。
                if k in SRC_TAGGED_FIELDS:
                    el[f"{k}_src"] = "override"
                hits += 1
    for key in SCALARS:
        if isinstance(ov.get(key), dict):
            out.setdefault(key, {}).update(copy.deepcopy(ov[key]))
            hits += 1
    for sec in SECTIONS:
        for el in out.get(sec, []) or []:
            derive_bbox(el)
    return out, {"hits": hits, "added": added, "miss": miss}
