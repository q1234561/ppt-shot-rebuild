"""页角色解析：把 `deck.json` 的声明解析成「每页 6 个角色 → 一组对象 id」。

为什么用声明、不用几何启发式：同一个件在不同页叫 `nav` / `c_nav` / `c_bar` / `nav_panel`
（实测 5 种命名），按"顶部长条"猜会在边界页猜错，而且**错得静默**——
猜错的后果是对齐工具把标题条当成导航条去对齐，没人会发现。
声明式的好处是：猜错变成"声明里没有"，而"没有"会被 `--check` 直接报出来。

**角色值可以是空数组**：某一页确实没有这个件（例如 41 页没有标题两侧的装饰横线），
`deco_l: []` 是合法声明，不是失败。失败只有三种：缺角色键、声明的 id 不存在、同一个 id 被两个角色认领。

用法：
    python scripts/roles.py --deck <整套>/deck.json --root <整套目录> --check
    python scripts/roles.py --deck <整套>/deck.json --root <整套目录>   # 只打印解析结果

判据（`--check` 的 rc）：0 = 9 页全部解析出 6 个角色且零冲突；1 = 有失败（逐条打印）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# 6 个角色：顶部件 5 类 + 页脚。顺序即报告列顺序，不要改（改了对齐报告的列会错位）
ROLE_KEYS = ("nav", "nav_pill", "div", "deco_l", "deco_r", "footer")
# 这三类每页必须至少有一个：缺了不是"这页没有这个件"，是声明漏了或对象改名了。
# div / deco 允许为空（page06 真的没有分隔线、41 页真的没有标题横线）。
REQUIRED_NONEMPTY = ("nav", "nav_pill", "footer")


def load_deck(path: Path) -> dict:
    """读 deck.json。只做结构校验：三个顶层键 + roles 覆盖 pages。"""
    deck = json.loads(Path(path).read_text(encoding="utf-8"))
    for k in ("visual", "pages", "roles"):
        if k not in deck:
            raise ValueError(f"deck.json 缺顶层键：{k}")
    if not deck["pages"]:
        raise ValueError("deck.json 的 pages 是空的")
    return deck


def spec_ids(spec: dict) -> set[str]:
    """spec 里所有可被角色认领的对象 id（容器段 + 元素段）。"""
    ids: set[str] = set()
    for seg in ("containers", "elements"):
        for item in spec.get(seg) or []:
            if isinstance(item, dict) and item.get("id"):
                ids.add(str(item["id"]))
    return ids


def resolve(deck: dict, root: Path) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    """按声明解析每页角色。返回 (解析结果, 失败清单)。

    失败清单里的每一条都能直接改：缺键 / id 不存在 / 一个 id 被两个角色认领 / 缺 spec.json。
    """
    out: dict[str, dict[str, list[str]]] = {}
    failures: list[str] = []

    for page in deck["pages"]:
        spec_p = Path(root) / page / "spec.json"
        roles_decl = deck["roles"].get(page)
        if roles_decl is None:
            failures.append(f"{page}：deck.json 没有这个页的角色声明")
            out[page] = {k: [] for k in ROLE_KEYS}
            continue
        # 1) 六个键齐全（值可为 []）
        missing = [k for k in ROLE_KEYS if k not in roles_decl]
        if missing:
            failures.append(f"{page}：缺角色键 {missing}")
        # 2) 声明的 id 必须真实存在（缺 spec.json 时只报这一条，不再连环报 id 不存在）
        if not spec_p.exists():
            failures.append(f"{page}：缺 spec.json（无法校验 id）")
            out[page] = {k: list(roles_decl.get(k) or []) for k in ROLE_KEYS}
            continue
        ids = spec_ids(json.loads(spec_p.read_text(encoding="utf-8")))
        # 重复认领只在本页内算冲突：id 是**页内**命名空间，跨页同名是常态
        # （40 和 41 都有 `nav`、44/45/48/49 都有 `c_nav`，那不是错误）
        claimed: dict[str, str] = {}
        page_roles: dict[str, list[str]] = {}
        for k in ROLE_KEYS:
            want = [str(x) for x in (roles_decl.get(k) or [])]
            page_roles[k] = want
            for i in want:
                if i not in ids:
                    failures.append(f"{page}/{k}：{i} 不在 spec 里（改名了？）")
                if i in claimed and claimed[i] != k:
                    failures.append(f"{page}：{i} 同时被 {claimed[i]} 和 {k} 认领")
                else:
                    claimed[i] = k
            # 同一个角色里重复写同一个 id：等于把一件当两件，词法上就该拦
            dup = sorted({i for i in want if want.count(i) > 1})
            if dup:
                failures.append(f"{page}/{k}：重复声明 {dup}")
        for k in REQUIRED_NONEMPTY:
            if not page_roles.get(k):
                failures.append(f"{page}/{k}：不能为空（缺了是声明漏了，不是这页没有这个件）")
        out[page] = page_roles
    return out, failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", required=True, help="整套根的项目 deck.json（页清单 + 6 角色声明）")
    ap.add_argument("--root", required=True, help="整套目录（下面每页一个子目录）")
    ap.add_argument("--check", action="store_true", help="有失败时返回 rc=1")
    args = ap.parse_args()

    deck = load_deck(Path(args.deck))
    roles, failures = resolve(deck, Path(args.root))

    head = f"{'页':34s} " + " ".join(f"{k:9s}" for k in ROLE_KEYS)
    print(head)
    for page in deck["pages"]:
        cells = []
        for k in ROLE_KEYS:
            n = len(roles.get(page, {}).get(k) or [])
            # 0 就是 0：不写成 "-"，让"这一页没有这个件"和"解析失败"在视觉上不同
            cells.append(f"{n:<9d}")
        print(f"{page:34s} " + " ".join(cells))

    n_pages = len(deck["pages"])
    if failures:
        print(f"\n解析失败 {len(failures)} 条（{n_pages} 页）：")
        for f in failures:
            print("  -", f)
    else:
        print(f"\n{n_pages} 页 × {len(ROLE_KEYS)} 角色，解析失败 0")
    return 1 if (failures and args.check) else 0


if __name__ == "__main__":
    raise SystemExit(main())
