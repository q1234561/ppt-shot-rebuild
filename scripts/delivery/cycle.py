# -*- coding: utf-8 -*-
"""cycle.py — 单页默认执行入口：准备 → 构建 → 渲染 → 必需检查 → 待修清单。

它取代的不是"某个检查"，而是**执行者自己安排顺序**：
  旧流程每改一项就跑一轮 构建 + 逐块比对 + 质量诊断，改一项重抠一次素材，
  交付通过后还会继续跑 diagnose。根因不是命令太多，而是没有一条
  "输入 → 执行 → 验收 → 停止"的合同。
  （第 ④ 步 4B 之后，"改完自动收敛"那条支线已随三个模块退役：
  只剩 构建 → 渲染 → 必需检查 → 裁决。）

两个模式：
  preview   只到"看得见"：plan / 受影响的 assets / build / look。
            输出完整待修清单。
  delivery  执行当前必需检查（含可编辑性）；所有有效报告复用，不重复渲染。

报告有效性由 report_state 的依赖键决定：成品和配置没变就不重建不重渲。

用法：
  python cycle.py --page <页> [--mode preview|delivery] [--spec spec.json] [--out out.pptx]
                  [--width W --height H] [--adopt-assets] [--force]
                  [--only 阶段,阶段]      # 分段重跑；不给就是整跑
输出：_check/cycle.json（各阶段 执行/复用/跳过、耗时、渲染次数、待修项、状态）
退出码：0 ready / 1 blocked 或 error

`--width/--height` 是**画布像素**（缺省取 spec.canvas.w_px/h_px），不是英寸。
`--only` 只跑点名的阶段（名字见阶段表，`PHASE_NAMES` 是唯一来源），其余记 skipped；
它**不写 verdict.json**：半跑的页面没有完整证据，写一份结论等于让它冒充交付结论。
改一处卡片颜色用 `--only build,look` 就够，不必把素材重抠一遍。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(HERE))
import impose
import specmerge
import report_state as rs
import verdict as vd

PY = sys.executable
HOST, PORT = "127.0.0.1", 8731

# ---------------------------------------------------------------- 溢出求解（常量）
# 降字号超过这个幅度就交人工（S5b）：肉眼可辨的变小属**版式决策**，不替用户定。
MAX_SIZE_DROP_PT = 2.0

# cutout.py 的退出码语义：3 = 「先配模型」这类**可修复的拒绝**，1 = 真异常。只有 3 值得把
# 整段中文指引摊开给人看，其它非零值仍按尾部切片处理（真异常看栈尾更省地方）。
# 这里镜像一份而不 `import cutout`：cutout 顶层就 import cv2/numpy/PIL，为了一个整数
# 把渲染链的解释器拖进这些重依赖不值得，而且缺依赖时 import 本身就会炸掉 cycle。
# 与 cutout.EXIT_NO_MODEL 同步；改动前先 grep 全仓 —— 这是**口头契约**，改一边必须改另一边，
# 否则有人把 cutout 的 3 改成 4 时，这里会静默失效（又变回"截成残句"而没人报错）。
EXIT_NO_MODEL = 3

# `--only` 认哪些阶段名：**唯一来源是 `_Ctx.phase()` 的第一参数**（也就是阶段表的 name 列）。
# 这里不再立一套别名——两套名字迟早对不上，而人写错 --only 时正是靠这张表纠错的。
# 不在表里的三个阶段是故意的：
#   `overflow-check`（assets 段之后的 0 轮求解，跟着 assets 跑）、
#   `spec:promote`（只在整跑时执行——半跑绝不写 spec.json）、
#   `assets:regenerate` 与 `assets:provenance` 在同一个函数里，见 `want()`。
PHASE_NAMES = ("assets:provenance", "assets:regenerate", "plan", "assets",
               "build", "look", "presence", "editability", "coverage", "delivery")

# 阶段表里"给眼睛看的"状态名。机器读的是 status 原值（cycle.json 里不改），
# 只是打印时把 unproven 说成「待确认」——不然它和 pass 一样是四个字母的英文，
# 一眼扫过去分不出"验过了"和"没验成"（2026-11-10 用户拍板）。
PHASE_LABELS = {"unproven": "待确认"}


def _parse_only(raw: str | None) -> set[str] | None:
    """解析 `--only`：逗号分隔的阶段名 → 集合；None = 整跑。

    写错名字**直接退**，不静默忽略：忽略的后果是"以为只跑了两段、其实什么都没跑"，
    而那次跑还会照常打印出一张全 skipped 的阶段表——比报错难查得多。
    """
    if raw is None:
        return None
    names = {n.strip() for n in str(raw).split(",") if n.strip()}
    if not names:
        raise SystemExit(f"--only 是空的（{raw!r}）——要么不写，要么给阶段名：{' / '.join(PHASE_NAMES)}")
    bad = sorted(names - set(PHASE_NAMES))
    if bad:
        raise SystemExit(f"--only 里有不认识的阶段：{', '.join(bad)}"
                         f"（可选：{' / '.join(PHASE_NAMES)}）")
    return names


class _Ctx:
    """main 的共享状态容器。字段就是原来那些局部变量。

    拆函数不是为了"更优雅"，是因为 757 行的 main 让每次改动都要通读全文——
    改一个参数（比如溢出求解的降字号上限）要穿越 700 行才敢下手。

    它同时把**隐式共享**变成显式：旧 main 里有十来个局部变量被多段读改写
    （`state` 被三段改写、`inv` 被 plan 段按盘上报告重读、
    `rebuilt` 从 3) 传到 4)），全靠 Python 的作用域偷渡，读代码时看不出谁改了什么。
    现在字段表就是契约表。
    """

    def __init__(self, a) -> None:
        self.a = a                      # argparse 结果（--mode / --force / --width …）
        self.only: set[str] | None = _parse_only(a.only)   # --only：分段重跑；None = 整跑
        # ---- 路径：由 --page / --spec / --out 派生（原来是 main 里的一段赋值） ----
        page = Path(a.page).resolve()
        spec_path = Path(a.spec) if Path(a.spec).is_absolute() else page / a.spec
        if not spec_path.exists():
            raise SystemExit(f"REFUSED: 缺 {spec_path}")
        d = spec_path.parent
        out_path = Path(a.out) if Path(a.out).is_absolute() else d / a.out
        ref_path = d / "reference.png"
        stem = "" if out_path.name == "out.pptx" else "_" + out_path.stem
        look_dir = d / f"look{stem}"
        self.page: Path = page
        self.spec_path: Path = spec_path
        self.d: Path = d
        self.out_path: Path = out_path
        self.ref_path: Path = ref_path
        self.look_dir: Path = look_dir
        self.png_path: Path = look_dir / "rendered.png"
        self.check_dir: Path = d / "_check"
        # ---- 画布：--width/--height 优先，否则 spec.canvas ----
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except Exception as exc:                  # noqa: BLE001
            raise SystemExit(f"REFUSED: {spec_path.name} 读不出来（{exc}）——先修好 spec 再跑 cycle")
        cv = spec.get("canvas") or {}
        W = int(a.width or cv.get("w_px") or 1672)
        H = int(a.height or cv.get("h_px") or 941)
        if [W, H] != [int(cv.get("w_px") or 0), int(cv.get("h_px") or 0)]:
            print(f"  NOTE 渲染尺寸 {W}x{H} 与 canvas {cv.get('w_px')}x{cv.get('h_px')} 不一致"
                  f"——检查会对不上，先核对 spec.canvas")
        self.W: int = W
        self.H: int = H
        # ---- 跨阶段可变状态（谁读谁写都在这张表里） ----
        self.inv: dict = {"items": []}    # 0) 素材段从盘上读入，1) plan 段会重读
        self.ov_path: Path | None = None  # override.json：0) 素材段定，7b) promote 用
        self.rebuilt: bool = False        # 3) build 重建过 → 4) look 不许判复用
        self.phases: list = []
        self.to_fix: list = []
        self.renders: dict = {"normal": 0, "editability": 0, "diagnostic": 0}
        self.state: str = "ready"
        self.t_all: float = 0.0           # 起点由 main 在 preflight 之后打

    # ---- 原 main 里的四个内部函数，签名一字未改，只是从闭包变成方法 ----
    def phase(self, name: str, status: str, reason: str = "", secs: float = 0.0) -> None:
        self.phases.append({"name": name, "status": status, "reason": reason, "secs": round(secs, 2)})

    def want(self, *names: str) -> bool:
        """这个阶段要不要跑（`--only` 的开关）。整跑时恒 True。

        收多个名字是因为**一个函数里跑两个阶段**这一处真实例外：
        `_stage_assets_provenance` 同时产出 `assets:provenance` 与 `assets:regenerate`。
        只传第一个名字的话，`--only assets:regenerate` 会把整块跳过、阶段表里连一行记录
        都没有——人看到的是一张"我要的那段根本没出现"的表。任一名字命中就执行。
        被跳过时只记一条 `skipped`（用第一个名字），不重复记。
        """
        if not self.only:
            return True
        for n in names:
            if n in self.only:
                return True
        self.phase(names[0], "skipped", "--only 未包含")
        return False

    def fix(self, stage: str, message: str, blocking: bool = True) -> None:
        self.to_fix.append({"stage": stage, "message": message, "blocking": blocking})

    def finish(self, state_: str) -> int:
        """唯一收尾口：带裁决产物 + 阶段表 + 退出码。

        以前三处 `return _finish(...)` 各写一遍参数，加一项就得改三处；
        统一到这里之后，「什么时候写 verdict.json」只有一个答案：**每次收尾都写**。
        崩在 build/look 时也写——那时的结论是"未证明"（false），不是"跳过"：
        五条布尔里没有"未知"这一态，缺证据必须落 false。

        唯一的例外是 `--only` 分段跑：只跑了两三段的页面**没有**完整证据，
        写一份 verdict.json 等于让半跑的结果冒充交付结论，所以传 `verdict=None`
        （`_finish` 已有该分支：只写 cycle.json，不写 verdict.json）。
        """
        partial = bool(self.only)
        # 输入身份：正式合并靠它判"这份结论是不是这版输入的"。算不出来就留 None
        # （deck 会把 None 当"缺身份，待重验"），绝不用 0/"" 之类占位冒充已核验。
        try:
            inputs_md5 = rs.page_inputs_digest(self.page, self.spec_path)
        except Exception:                     # noqa: BLE001——收尾阶段不许因此崩掉
            inputs_md5 = None
        return _finish(self.page, self.check_dir, self.a.mode, state_, self.phases, self.to_fix,
                       self.renders, time.perf_counter() - self.t_all,
                       verdict=None if partial else
                       _verdict_now(self.spec_path, self.check_dir, self.look_dir,
                                    self.png_path, self.out_path, self.a.mode,
                                    self.inv, self.W, self.H),
                       partial=partial, only=sorted(self.only) if partial else None,
                       out_path=self.out_path, inputs_md5=inputs_md5)

    def errs_from(self, path: Path, key: str) -> tuple[dict, list[str], str]:
        rd = rs.read_report(path, key)
        rep = rd.get("report") or {}
        if rd["validity"] in ("missing", "stale", "invalid"):
            return rd, [], rs.describe(rd)
        return rd, rep.get("errors") or [], ""


# ---------------------------------------------------------------- 基础
def run(cmd: list, timeout: float = 300.0) -> tuple[int, str, float]:
    t0 = time.perf_counter()
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or ""), time.perf_counter() - t0
    except subprocess.TimeoutExpired as exc:
        return 124, f"TIMEOUT {timeout}s：{exc}", time.perf_counter() - t0


def _ping() -> bool:
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((HOST, PORT))
        s.sendall(b"ping\n")
        return s.recv(64).startswith(b"PONG2")
    except OSError:
        return False
    finally:
        s.close()


def ensure_daemon(log_path: Path | None = None) -> None:
    log_path = Path(log_path) if log_path else Path.cwd() / "_check" / "renderd.log"
    if _ping():
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lf = open(log_path, "a", encoding="utf-8")
    lf.write(f"\n--- cycle ensure_daemon {time.strftime('%Y-%m-%d %H:%M:%S')} · {PY} ---\n")
    lf.flush()
    subprocess.Popen([PY, str(HERE / "renderd.py")], stdout=lf, stderr=lf)
    for _ in range(80):
        if _ping():
            return
        time.sleep(0.1)
    raise RuntimeError(f"渲染服务起不来（解释器 {PY}，日志 {log_path}）")


def render(pptx: Path, png: Path, w: int, h: int, log_path: Path | None = None) -> tuple[float, str]:
    ensure_daemon(log_path)
    s = socket.socket()
    s.settimeout(90)
    s.connect((HOST, PORT))
    s.sendall(f"render\t{pptx}\t{png}\t{w}\t{h}\n".encode("utf-8"))
    resp = s.recv(4096).decode("utf-8", "replace").strip()
    s.close()
    parts = resp.split("\t")
    if parts[0] != "OK":
        raise RuntimeError(f"渲染失败：{resp}")
    return float(parts[1]), parts[2]


def _json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _write_atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 溢出求解
def _activation_head(sized: list) -> tuple[dict | None, float]:
    """认出「激活页签」这一档：最大那块比其余中位大 1.05~1.30 倍。

    判据与 spec_lint ② 完全一致（它把 head 摘出去再判其余极差，ratio 落在这个区间
    才算合法的"激活页签档"，否则报「孤立的大字号」）。降字号时**保留这个倍数**：
    整组一律压平会毁掉一个合法设计，也会让 ② 改成报「孤立的大字号」。
    """
    items = sorted(sized, key=lambda t: (t[0], str((t[1] or {}).get("id"))))
    if len(items) < 3:          # rest 不足 2 块时 ② 不做倍数判断，这里也不认
        return None, 1.0
    (head_size, head_el), rest = items[-1], items[:-1]
    med = sorted(s for s, _ in rest)[len(rest) // 2]
    ratio = head_size / med if med else 1.0
    return (head_el, ratio) if 1.05 <= ratio <= 1.30 else (None, 1.0)


def solve_overflow(view: dict, dpi: float, scale: list[float] | None = None) -> dict:
    """溢出一次求解（**不占循环轮次**）：按同层分组把字号降到组内最小上限。

    `scale`（规范的字号档位表）不是可选项：溢出求解也在写字号，如果它给出 15.7pt，
    就和 `impose` 的 8 档规范成了**两套规则**（用户原话："旧的要删掉的，不然是两套规则"）。
    给了 scale 就**只往下取档**：取 ≤ 目标值的最大档——向上取会重新溢出。
    取不到档（目标比最小档还小）就交人工，不自己发明一个值。

    为什么要单独一步：溢出是用户唯一自己看得见、也最常出现的缺陷（字挤出卡片）。
    若它落进"改不完轮数就接受现状"，那这个轮数上限对用户的真实含义就变成"字挤出来也得接受"。
    闭式解是确定的，所以 0 轮；`spec_lint.overflow_of` 给的 bound 是**同一个不等式**
    对 size 的移项逆解，所以"批过的"与"降过的"结论必然自洽。

    分组用 `spec_lint.layer_groups` 的 **cluster** 层（同 role 前缀或 style 令牌、
    同 12px 纵向带、相邻簇），整组一起降：只降溢出的那一块，同簇邻块字号不变，
    spec_lint ② 的极差判据立刻 FAIL —— 那就等于把溢出换成了另一种不合格。
    上限取组内最小 bound：组里任一块溢出都必须被覆盖。

    「>2pt 转人工」与「≥2 组 = 版式密度超了」两条都**不改**，只写进 manual 交 S5b。

    返回 {"sizes": {id: pt}, "groups": n, "manual": [{"why"}],
          "unsolvable": [id], "advice": str}
    """
    import spec_lint as sl

    els = list((view or {}).get("elements") or [])
    over: dict[str, dict] = {}
    for e in els:
        got = sl.overflow_of(e, dpi)
        if got is not None:
            over[str(e.get("id"))] = got
    out = {"sizes": {}, "groups": 0, "manual": [], "unsolvable": [], "advice": ""}
    if not over:
        return out

    groups: list[list[dict]] = []
    group_of: dict[str, int] = {}
    buckets, _no_style = sl.layer_groups(els)
    for _key, clusters in buckets:
        for cl in clusters:
            groups.append(cl)
    for i, cl in enumerate(groups):
        for e in cl:
            group_of[str(e.get("id"))] = i

    bound_of: dict[int, float] = {}
    for eid, got in over.items():
        if got.get("bound") is None:
            # adv_eff ≤ 1.02×BOX_PAD_EM：内边距一项就吃掉了整行，字再小也放不下
            # （框宽为 0 或文案长得离谱）。这不是字号能解决的问题。
            out["unsolvable"].append(eid)
            continue
        g = group_of.get(eid)
        if g is None:
            # 落不进任何簇（无 role 也无 style 令牌 / 单元素簇）：自成一组。
            # 单块溢出同样要能降——否则 page06 这种没有 style 令牌的页永远修不了。
            groups.append([next(e for e in els if str(e.get("id")) == eid)])
            g = len(groups) - 1
            group_of[eid] = g
        bound_of[g] = min(bound_of.get(g, got["bound"]), got["bound"])

    if len(bound_of) >= 2:
        # 与 assetcheck「一页走豁免 ≥3 = 策略错」同一判据思路：一页有 ≥2 组要降字号，
        # 说明版式密度超了；继续降只是把这个错平摊到每块字上（每块都更小、还是挤）。
        out["advice"] = (f"本页有 {len(bound_of)} 组文字需要降字号（≥2 = 版式密度超了）："
                         f"建议拆页/加卡片，而不是继续降字号。已交人工，未自动改。")
        for g, bound in sorted(bound_of.items()):
            ids = "/".join(str(e.get("id")) for e in groups[g])
            out["manual"].append({"why": f"溢出组 {ids} 需降到 {bound:.1f}pt"
                                         f"（本页 ≥2 组要降 → 见「建议拆页/加卡片」）"})
        return out

    for g, bound in sorted(bound_of.items()):
        cl = groups[g]
        sized = [(sl.size_of(e), e) for e in cl if sl.size_of(e) is not None]
        if not sized:
            continue
        head, head_ratio = _activation_head(sized)
        plan_g: dict[str, float] = {}
        for cur, e in sized:
            eid = str(e.get("id"))
            # 普通块 = 组内最小上限；激活页签 = 上限 × 它既有的倍数（保住那一档）
            tgt = bound * head_ratio if (head is not None and e is head) else bound
            if cur - tgt > 0.05:
                # **向下**取到 0.1pt：向上取整会让 new > bound，溢出判据原地复发
                new = math.floor(tgt * 10) / 10
                if scale:
                    fit = [x for x in scale if x <= new + 1e-9]
                    if not fit:
                        out["manual"].append({"why": (
                            f"{eid}: 溢出要降到 {new:.1f}pt，比最小档 {min(scale):g}pt 还小——"
                            f"这是版式问题，不靠改字号解决")})
                        continue
                    new = max(fit)
                plan_g[eid] = new
        if not plan_g:
            continue
        cur_of = {str(e.get("id")): sl.size_of(e) for e in cl}
        drop = max(cur_of[eid] - new for eid, new in plan_g.items())
        if drop > MAX_SIZE_DROP_PT:
            out["manual"].append({"why": (
                f"溢出组 {'/'.join(sorted(plan_g))} 要降 {drop:.1f}pt（> {MAX_SIZE_DROP_PT:.0f}pt）"
                f"——降幅属版式决策，交人工（S5b），未自动改")})
            continue
        out["sizes"].update(plan_g)
        out["groups"] += 1
    return out


def write_size_override(spec_path, sizes: dict) -> None:
    """把溢出一处求解的字号写进 override（此处不回写 spec；ready 时由 _promote_override 统一并回）。

    直接写、不经过自动改写：四类修正里**没有字号**——字号是 style.solve 的求解
    结果，按宽比反解会把求解误差吸收成"宽比 1.000"，同一排文字会出现两种字号。
    写之前先读，只加 elements[*].size_pt；override 里的 assets.src 与 _meta 一律保留
    （后者是素材来源摘要，冲掉会让下次 cycle 判"来源未记录"、四张图全部要求重核验）。
    """
    sp = Path(spec_path)
    ovp = sp.parent / "override.json"
    ov = specmerge.load(ovp)
    els = ov.setdefault("elements", {})
    for eid, pt in (sizes or {}).items():
        els.setdefault(eid, {})["size_pt"] = float(pt)
    ovp.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")



# ---------------------------------------------------------------- 主流程
# inventory 里"人工签字"字段：行被重建/丢弃时，这些内容是不可再生的（不是测量值）。
HUMAN_FIELDS = ("approx_of", "gaps", "crop_note", "locked", "locked_reason",
                "reviewed_on_bg")


def _expected_inv_ids(spec_path: Path) -> set:
    """inventory.json 里**应该**有哪些 id —— 直接复用 inventory 的派生+剪枝。

    为什么必须复用、不能自己算：留不留行由 build_inventory 的规则决定
    （`native-simple` 且无人工字段不留行、extract 的素材候选不留行）。在 cycle 里另写一份
    "spec 的 id 全集"，两边判据不同步的后果是"永远判不同步 → 每次 cycle 都重写 inventory"，
    2026-11-08 实测就是这样（P07 have=5 / want=24）。
    """
    import io
    from contextlib import redirect_stdout
    import inventory as inv_mod
    quiet = io.StringIO()
    with redirect_stdout(quiet):      # build_inventory 会打印"跳过 N 个素材候选"，主链别刷屏
        inv, _ = inv_mod.build_inventory(spec_path.parent, spec_path)
    return {i["id"] for i in (inv.get("items") or []) if i.get("id")}


def _has_error_phase(phases: list[dict]) -> bool:
    """阶段崩了（status=error）就不能 ready。

    2.42.0：原来这里对 `diagnostics:*` 开了个"error 不算崩"的豁免，那是给按需诊断阶段用的。
    诊断脚本（verify / edit_quality）与 `--diagnostics` 一起退役后，全库再没有任何代码能产出
    `diagnostics:*` 阶段，豁免就成了永不执行的死分支——删掉，判据回到"只有 error 才算崩"。
    """
    return any(str(p.get("status")) == "error" for p in phases)


# ---------------------------------------------------------------- 阶段函数
# 原来这 757 行都在 main() 里，靠注释分段。分段是天然的拆分边界，就按它搬：
# 一段一个 `_stage_*`，段内代码逐字不动（连 print 的字面量都不动），
# 只在原来的"局部变量"前加 `ctx.` —— 因为它们现在是 _Ctx 的字段。
# 提前返回的两处（3) build 崩、4) look 崩）改成"stage 返回 True"，
# 由 main 决定 `return ctx.finish(...)`：退出码与收尾时机完全一致。


def _stage_preflight(ctx: _Ctx) -> int | None:
    """0) 前置：缺文件就别往下走。

    返回 None = 继续；返回 int = 立刻以该退出码收工（原来的两处 `return 1`）。
    """
    d, page, spec_path = ctx.d, ctx.page, ctx.spec_path
    # 流程问题以前要等到 plan 阶段才报（那时页面已做完大半），这里把"该有的文件"在门口点一遍。
    if not (d / "spec.json").exists() or not (d / "reference.png").exists():
        lack = [n for n in ("spec.json", "reference.png") if not (d / n).exists()]
        print(f"REFUSED: 缺 {', '.join(lack)}（extract 还没跑）")
        return 1
    # inventory.json 原来是硬前置：缺了直接 REFUSED，与 spec 不同步则拖到 plan 阶段才炸，
    # 于是每改一次 asset/mask 都得人手跑一遍 inventory.py（2026-11-08 实测：一页里被卡 3 次）。
    # 它只是「每块一条决策记录」的派生物，能由 spec+override 算出来 → 缺了/不同步就当场补。
    # 但"该有哪些行"只能由 inventory 自己的派生+剪枝给（见 _expected_inv_ids），
    # 不能拿 spec 的 id 全集顶替 —— 那样 want 恒大于 have，每跑一次都判"不同步"并重写一遍。
    inv_path = d / "inventory.json"
    ov_raw = specmerge.load(d / "override.json")
    want_ids = _expected_inv_ids(spec_path)
    have_ids: set = set()
    old_rows: dict = {}
    if inv_path.exists():
        try:
            _prev_items = json.loads(inv_path.read_text(encoding="utf-8")).get("items") or []
        except Exception as exc:
            # 手改坏的 JSON 不能静默重建：一次重建就把 locked / locked_reason / gaps
            # 抹成"零人工签字"，而且没人会知道它曾经存在过。
            print(f"REFUSED: {inv_path.name} 解析失败（手改坏了？）：{exc}")
            return 1
        old_rows = {i.get("id"): i for i in _prev_items}
        have_ids = set(old_rows)
    # 悬空 src（override 指着一个已被删掉的文件）也在这里清掉：那个文件本来就该由 fastcut
    # 重生成，留着只会让 plan 报 "src 文件不存在"，而人得跳出去手工删一遍。
    # 基准与 inventory.check 一致（页目录 + 上一级）：只认页目录会把"相对整套根"的合法 src
    # 误判成悬空删掉，逼 fastcut 重抠一遍（matting 约 3min/块）。
    dropped = []
    for eid, rec in (ov_raw.get("assets") or {}).items():
        src = (rec or {}).get("src")
        if src:
            p = Path(src)
            found = p.exists() if p.is_absolute() else ((d / p).exists() or (d.parent / p).exists())
            if not found:
                rec.pop("src", None)
                ((ov_raw.get("_meta") or {}).get("asset_sources") or {}).pop(eid, None)
                dropped.append(eid)
    if dropped:
        (d / "override.json").write_text(
            json.dumps(ov_raw, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  NOTE 清掉 {len(dropped)} 条悬空 src（{', '.join(dropped)}），交给 fastcut 重生成")
    existed = inv_path.exists()
    if not existed or have_ids != want_ids:
        # 重建会按 id 搬回人工字段；id 消失（改名/删元素）的那些行连同签字一起没了，
        # 所以必须点名，不能只说"已重建"。
        lost = [eid for eid in sorted(have_ids - want_ids)
                if any((old_rows.get(eid) or {}).get(k) for k in HUMAN_FIELDS)]
        run([PY, HERE / "inventory.py", "--page", page, "--write"], 120)
        note = f"  NOTE inventory.json {'缺失' if not existed else '与派生集合不同步'}，已按 spec+override 重建"
        if lost:
            note += f"；以下 id 的人工字段随之消失：{', '.join(lost)}"
        print(note)
    return None


def _stage_assets_provenance(ctx: _Ctx) -> None:
    """0) 素材：来源与失效。"""
    d, spec_path, ref_path = ctx.d, ctx.spec_path, ctx.ref_path
    inv = _json(d / "inventory.json") or {"items": []}
    ov_path = d / "override.json"
    ov = specmerge.load(ov_path)
    view = rs.merged_view(spec_path)
    ctx.inv, ctx.ov_path = inv, ov_path
    meta = (ov.get("_meta") or {})
    src_meta = dict(meta.get("asset_sources") or {})
    regen_fast, regen_cut, unverified = [], [], []
    for el in view.get("assets") or []:
        eid = el.get("id")
        mask = str(el.get("mask") or "cutout")
        # 2026-11-10：这里原来对 `mask=="rect" 且无 src` 直接 continue，等于"显式留整块原图"
        # 永远不生成 PNG → build 报 MISS。fastcut 早就支持 rect（整块裁、不切 α），
        # 只是调度层把它漏了。现在与其它 mask 一样进 regen 列表。
        key = rs.asset_gen_key(spec_path, el)
        rec = src_meta.get(eid)
        if not el.get("src"):
            (regen_cut if mask == "cutout" else regen_fast).append(eid)
        elif not rec or not rec.get("src_md5"):
            # 没有来源记录、或只有旧记录没有内容摘要：都按“来源不明”处理，
            # 由人核验后 --adopt-assets 采纳，或删 src 让 cycle 重生成。
            unverified.append(eid)
        else:
            p_src = Path(el["src"])
            p_src = p_src if p_src.is_absolute() else (spec_path.parent / p_src)
            cur_md5 = rs.file_digest(p_src)
            if rec.get("key") != key:
                # 生成输入变了 → 必须重生成。但**人工采纳过的不覆盖**：那份文件是人放进去的，
                # 自动重抠会把它盖掉（matting 约 3min/块，重跑也回不来）。转为待核对，
                # 由人用 --adopt-assets 重新采纳，或删 src 让 cycle 重生成。
                if rec.get("adopted"):
                    unverified.append(eid)
                else:
                    (regen_cut if mask == "cutout" else regen_fast).append(eid)
            elif rec.get("src_md5") != cur_md5:
                # 输入没变但素材文件被改过 → 按当前输入重生成，不沿用被改的文件
                (regen_cut if mask == "cutout" else regen_fast).append(eid)

    t0 = time.perf_counter()
    if ctx.a.adopt_assets and unverified:
        for eid in unverified:
            el = next((e for e in view.get("assets", []) if e.get("id") == eid), None)
            if el:
                p_src = Path(el["src"])
                p_src = p_src if p_src.is_absolute() else (spec_path.parent / p_src)
                src_meta[eid] = {"key": rs.asset_gen_key(spec_path, el), "adopted": True,
                                 "src_md5": rs.file_digest(p_src)}
        ov.setdefault("_meta", {})["asset_sources"] = src_meta
        ov_path.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
        ctx.phase("assets:provenance", "executed", f"采纳历史素材 {len(unverified)} 项（当前输入已核验）",
                  time.perf_counter() - t0)
        unverified = []
    elif unverified:
        msg = (f"素材来源未记录（{', '.join(unverified[:6])}）：先核验，或 --adopt-assets 采纳现状，"
               f"或删除 src 让 cycle 重生成")
        ctx.fix("assets", msg, blocking=(ctx.a.mode == "delivery"))
        ctx.phase("assets:provenance", "skipped", msg, time.perf_counter() - t0)
    else:
        ctx.phase("assets:provenance", "reused", "素材来源摘要齐全", time.perf_counter() - t0)

    if regen_fast or regen_cut:
        t0 = time.perf_counter()
        ok = True
        no_model = False
        if regen_fast:
            rc, out, _ = run([PY, SKILL / "scripts" / "fastcut.py", "--spec", spec_path,
                              "--image", ref_path, "--outdir", d / "assets",
                              "--force-ids", ",".join(regen_fast)], 300)
            ok = ok and rc == 0
            if rc != 0:
                ctx.fix("assets", f"fastcut 重生成失败：{out.strip()[-300:]}")
        if regen_cut:
            rc, out, _ = run([PY, SKILL / "scripts" / "cutout.py", "--spec", spec_path,
                              "--image", ref_path, "--outdir", d / "assets",
                              "--force-ids", ",".join(regen_cut)], 600)
            ok = ok and rc == 0
            if rc == EXIT_NO_MODEL:
                # 缺模型是"可修复的拒绝"，不是"素材生成失败"：那段指引 1061 字符 / 16 行，
                # 而关键信息全在**头部**（REFUSED 那行、"这是什么/去哪拿/没它怎样"），
                # `[-300:]` 只留尾部 → 人看到的是一段切在词中间的路径残渣，指引等于白写。
                # 所以全文照打，blocking 里只留一句能照着做的摘要指向它。
                no_model = True
                print(out.strip(), flush=True)
                ctx.fix("assets", "cutout 需要抠图模型，当前四档都没找到（完整指引见上方）；"
                                  "放好模型或设 PPT_MATTE_MODEL 后重跑，或用 fastcut 的 mask 绕开")
            elif rc != 0:
                ctx.fix("assets", f"cutout 重生成失败：{out.strip()[-300:]}")
        if ok:
            ov = specmerge.load(ov_path)
            view = rs.merged_view(spec_path)
            src_meta = dict((ov.get("_meta") or {}).get("asset_sources") or {})
            for eid in regen_fast + regen_cut:
                el = next((e for e in view.get("assets", []) if e.get("id") == eid), None)
                if el and el.get("src"):
                    p_src = Path(el["src"])
                    p_src = p_src if p_src.is_absolute() else (spec_path.parent / p_src)
                    src_meta[eid] = {"key": rs.asset_gen_key(spec_path, el),
                                     "src_md5": rs.file_digest(p_src), "generated": True}
            ov.setdefault("_meta", {})["asset_sources"] = src_meta
            ov_path.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
        # 阶段表也要能看出根因：只写 "cutout 1 / 0" 时，人看不出这是缺模型还是抠图失败。
        _done = f"fastcut {len(regen_fast)} / cutout {len(regen_cut)}"
        ctx.phase("assets:regenerate", "executed" if ok else "error",
                  "缺抠图模型（可修复）" if no_model else _done, time.perf_counter() - t0)
    else:
        ctx.phase("assets:regenerate", "reused", "素材文件与当前输入一致")



def _stage_normalize(ctx: _Ctx) -> None:
    """0.5) 规范归一：字号吸到档位表、导航/页脚搬到规范位置（写回 spec，幂等）。

    放在 plan 之前，因为后面的检查必须看**最终要出稿的那份数**。
    以前的顺序是"出稿 → 事后修"，于是闸门看的是旧值、修完还得再出一遍。
    """
    t0 = time.perf_counter()
    try:
        visual = specmerge.load_visual(None, ctx.page)
    except FileNotFoundError:
        ctx.phase("normalize", "skipped", "没有 visual.json（这套未定规范）", time.perf_counter() - t0)
        return
    try:
        c = impose.consts(visual)
    except ValueError as exc:
        # 规范缺字段就停：给默认值等于悄悄种下第二套规则
        ctx.phase("normalize", "error", str(exc), time.perf_counter() - t0)
        ctx.state = "error"
        ctx.fix("normalize", str(exc))
        return

    # 角色声明只认**项目自己的** `deck.json`（页目录或整套根）。
    # 2026-11-10 删掉了第三条回落：`SKILL/templates/deck.json`。那是旧客户的九页清单，
    # 留着会让"没有声明"的项目悄悄套上别人的页名与角色——声明错了没人看得见，
    # 而且它随技能版本变，等于把一份客户数据当成了全局默认值。
    # 那份原件现在在 `tests/fixtures/deck-9pages.json`，只作夹具与示例，谁都不许当缺省回退。
    import roles as _roles
    deck_path = None
    for cand in (Path(ctx.page) / "deck.json", Path(ctx.page).parent / "deck.json"):
        if cand.exists():
            deck_path = cand
            break
    if deck_path is None:
        ctx.phase("normalize", "skipped",
                  f"这套没有 deck.json（找过 {Path(ctx.page).name}/ 与上一级）——没角色声明就不做归一；"
                  f"要归一请在整套根写 deck.json（示例见 tests/fixtures/deck-9pages.json，照抄结构、换成自己的页）",
                  time.perf_counter() - t0)
        return
    deck = _roles.load_deck(deck_path)
    page_key = Path(ctx.page).resolve().name
    # 写入只有一处：impose.apply_page（读→归一→写）。cycle 不自己写 spec。
    changes, notes = impose.apply_page(Path(ctx.page), deck["roles"].get(page_key) or {}, c)
    if not changes:
        ctx.phase("normalize", "pass", f"符合规范（例外 {len(notes)} 条）", time.perf_counter() - t0)
        return
    kinds: dict[str, int] = {}
    for ch in changes:
        kinds[ch["kind"]] = kinds.get(ch["kind"], 0) + 1
    ctx.phase("normalize", "executed",
              "已归一 " + "/".join(f"{k} {v}" for k, v in kinds.items())
              + (f"；例外 {len(notes)}" if notes else ""), time.perf_counter() - t0)


def _stage_plan(ctx: _Ctx) -> None:
    """1) plan。"""
    d, page, spec_path, check_dir = ctx.d, ctx.page, ctx.spec_path, ctx.check_dir
    pkey = rs.key_plan(spec_path, ctx.inv)
    rd = rs.read_report(check_dir / "plan.json", pkey)
    t0 = time.perf_counter()
    if rd["validity"] == "current" and not ctx.a.force:
        # 有效报告一律复用：通过就不再跑；已经判失败也不再重跑，等输入变了才失效
        ctx.phase("plan", "reused", rs.describe(rd), 0)
        for e in (rd.get("report") or {}).get("errors") or []:
            ctx.fix("plan", e)
    else:
        rc, out, secs = run([PY, HERE / "gates.py", "--stage", "plan", "--page", page,
                             "--spec", spec_path], 120)
        ctx.inv = _json(d / "inventory.json") or ctx.inv
        rd, errs, why = ctx.errs_from(check_dir / "plan.json", rs.key_plan(spec_path, ctx.inv))
        status = "executed" if rc == 0 else "error"
        # 括号必须有：`why or f"…" if errs else ""` 会被解析成 `(why or f"…") if errs else ""`，
        # errs 为空（报告 stale / 闸崩了）时把原因一起吞掉，只剩一个没有说明的 fail。
        ctx.phase("plan", status, why or (f"{len(errs)} 项问题" if errs else ""), secs)
        for e in errs:
            ctx.fix("plan", e)
        if rc != 0 and not errs:
            ctx.fix("plan", out.strip()[-300:] or "gates plan 失败")


def _stage_assets(ctx: _Ctx) -> None:
    """2) assets。"""
    page, spec_path, check_dir = ctx.page, ctx.spec_path, ctx.check_dir
    akey = rs.key_assets(spec_path)
    rd = rs.read_report(check_dir / "assets.json", akey)
    t0 = time.perf_counter()
    if rd["validity"] == "current" and not ctx.a.force:
        ctx.phase("assets", "reused", rs.describe(rd), 0)
        for e in (rd.get("report") or {}).get("errors") or []:
            ctx.fix("assets", e)
    else:
        rc, out, secs = run([PY, HERE / "gates.py", "--stage", "assets", "--page", page,
                             "--spec", spec_path, "--produce"], 300)
        rd, errs, why = ctx.errs_from(check_dir / "assets.json", rs.key_assets(spec_path))
        ctx.phase("assets", "executed" if rc == 0 else "error",
                  why or (f"{len(errs)} 项问题" if errs else ""), secs)
        for e in errs:
            ctx.fix("assets", e)
        if rc != 0 and not errs:
            ctx.fix("assets", out.strip()[-300:] or "gates assets 失败")


def _stage_overflow(ctx: _Ctx) -> None:
    """2b) 溢出求解（0 轮，spec 层，闭式解）。"""
    # 必须落在 build **之前**：build 对 `plan current/fail` 是 REFUSED，而溢出正是 spec_lint
    # 的一条 FAIL —— 放在 look 之后的话，带溢出的页在 build 就被拒，永远走不到这一步
    # （第一版就这么写的，实测溢出段：build error「REFUSED: plan 不可用」，0 渲染）。
    # 放在这里也符合它的性质：闭式解、不需要渲染、不占轮次。
    page, spec_path, check_dir = ctx.page, ctx.spec_path, ctx.check_dir
    _view = rs.merged_view(spec_path)
    _dpi = float((_view.get("canvas") or {}).get("dpi") or 125.4)
    # 档位表来自规范；没有规范（老页/单测）就退回旧行为，但绝不自己造档
    try:
        _scale = impose.consts(specmerge.load_visual(None, page))["scale"]
    except (FileNotFoundError, ValueError):
        _scale = None
    solved = solve_overflow(_view, _dpi, scale=_scale)
    for m in solved["manual"]:
        ctx.fix("overflow", m["why"], blocking=False)
    for eid in solved["unsolvable"]:
        ctx.fix("overflow", f"{eid} 溢出但闭式解退化（adv_eff ≤ BOX_PAD_EM）——"
                       f"改框宽或改文案，字号解不出来", blocking=False)
    if solved["advice"]:
        ctx.fix("overflow", solved["advice"], blocking=False)
    # 降过字号的块：自动收敛退役后已没有下游消费者（`bad_items` 随 verify 一起退役），
    # 这里只当"有没有降过"的布尔用，不再单独维护一个集合。
    if solved["sizes"]:
        write_size_override(spec_path, solved["sizes"])
        detail = "、".join(f"{eid}→{pt:.1f}pt" for eid, pt in sorted(solved["sizes"].items()))
        print(f"\n溢出求解（0 轮）：{solved['groups']} 组降字号 —— {detail}")
        for eid, pt in sorted(solved["sizes"].items()):
            ctx.fix("overflow", f"{eid} 溢出：字号降到 {pt:.1f}pt（整组一起降，不占轮次）",
                    blocking=False)
        # 降完必须**重跑一次**溢出判据确认 PASS（自洽由工具自己验，不靠人记）。
        # 顺带把 plan 报告刷新到当前 override —— build 与裁决都只认 current 的 plan。
        rc, out, secs = run([PY, HERE / "gates.py", "--stage", "plan", "--page", page,
                             "--spec", spec_path], 120)
        _prd, _plan_errs, why = ctx.errs_from(check_dir / "plan.json",
                                              rs.key_plan(spec_path, ctx.inv))
        ctx.to_fix[:] = [f for f in ctx.to_fix if f.get("stage") != "plan"]
        for e in _plan_errs:
            ctx.fix("plan", e)
        still = [e for e in _plan_errs if "溢出风险" in e]
        ctx.phase("overflow-check", "pass" if not still else "fail",
                  "溢出复核：spec_lint 重跑无溢出（闭式解与判据自洽）" if not still
                  else f"溢出复核仍有 {len(still)} 处：" + "；".join(still[:2]), secs)
        for p in ctx.phases:
            if p["name"] == "plan":
                p["status"], p["secs"] = "executed", round(p["secs"] + secs, 2)
                p["reason"] = ((why or (f"{len(_plan_errs)} 项问题" if _plan_errs else ""))
                               + " · 溢出求解后复核")


def _stage_build(ctx: _Ctx) -> bool:
    """3) build。返回 True 表示"构建崩了，该收尾"（原来这里 `return finish(state)`）。"""
    page, spec_path, out_path, check_dir = ctx.page, ctx.spec_path, ctx.out_path, ctx.check_dir
    bkey = rs.key_build(spec_path, out_path, ctx.W, ctx.H)
    rd = rs.read_report(check_dir / "build.json", bkey)
    t0 = time.perf_counter()
    # 复用要同时满足两条（2026-11-10 补第二条）：
    #   ① 报告对当前**输入**有效（rd["passed"]：key 一致且 outcome=pass）；
    #   ② 盘上这份 pptx 就是**报告记录的那份**（摘要相等）。
    # 只有①时，人手动改过/换过 out.pptx（改颜色、删形状、拿别页的文件顶替）也照样 reused，
    # 于是"构建通过"这句话证明的是一份已不存在的产物（实测：手改成品后 cycle 仍 ready）。
    rec_md5 = str((rd.get("report") or {}).get("pptx_md5") or "")
    cur_md5 = rs.file_digest(out_path)
    same_out = bool(out_path.exists() and rec_md5 and cur_md5 and rec_md5 == cur_md5)
    if rd["passed"] and same_out and not ctx.a.force:
        ctx.phase("build", "reused", rs.describe(rd), 0)
    else:
        if rd["passed"] and not same_out and not ctx.a.force:
            print(f"  NOTE build 报告有效的成品摘要 {rec_md5[:8]}… ≠ 盘上 {str(cur_md5)[:8]}…"
                  f"（被外部改过/换过）→ 按当前 spec 重建，不沿用旧结论")
        rc, out, secs = run([PY, SKILL / "scripts" / "build.py", "--spec", spec_path,
                             "--out", out_path], 300)
        if rc != 0:
            ctx.phase("build", "error", out.strip()[-400:], secs)
            ctx.fix("build", f"构建被拒：{out.strip().splitlines()[-1] if out.strip() else '见输出'}")
            ctx.state = "error"
        else:
            ctx.rebuilt = True
            ctx.phase("build", "executed", out.strip().splitlines()[-1] if out.strip() else "", secs)
            rs.write_report(check_dir / "build.json", {
                "stage": "build", "kind": "build", "outcome": "pass",
                "pptx": str(out_path), "pptx_md5": rs.file_digest(out_path),
                "out_size": [ctx.W, ctx.H],
            }, rs.key_build(spec_path, out_path, ctx.W, ctx.H))

    return ctx.state == "error"


def _refresh_build_evidence(bdoc: dict, out_path: Path, png_path: Path,
                            W: int, H: int) -> dict | None:
    """渲染成功后要给 build 报告补的证据；**不符合身份时返回 None**（不许重签）。

    规则（2026-11-10）：构建阶段是唯一能认定"这份 pptx 由当前输入构建"的角色。
    渲染阶段只补 png 证据，`pptx_md5` 一律沿用构建阶段记下的值；一旦盘上成品与
    构建记录不一致（被人手改过、被别的页顶替），返回 None——宁可这一轮少一条渲染证据，
    也不能让"渲染成功"给一份已被替换的成品背书。
    """
    rec_md5 = str((bdoc or {}).get("pptx_md5") or "")
    cur_md5 = rs.file_digest(out_path)
    if rec_md5 and rec_md5 != cur_md5:
        return None
    return {"stage": "build", "kind": "build", "outcome": "pass",
            "pptx": str(out_path), "pptx_md5": rec_md5 or cur_md5,
            "png": str(png_path), "png_md5": rs.file_digest(png_path),
            "out_size": [int(W), int(H)]}


def _stage_look(ctx: _Ctx) -> bool:
    """4) look（唯一正常渲染）。返回 True 表示"渲染崩了，该收尾"（原来这里 return finish(state)）。"""
    spec_path, out_path, check_dir = ctx.spec_path, ctx.out_path, ctx.check_dir
    look_dir, png_path, ref_path = ctx.look_dir, ctx.png_path, ctx.ref_path
    rd = rs.read_report(check_dir / "build.json", rs.key_build(spec_path, out_path, ctx.W, ctx.H))
    bdoc = rd.get("report") or {}
    look_json = _json(look_dir / "look.json") or {}
    png_ok = (png_path.exists() and look_json.get("png_md5")
              and rs.file_digest(png_path).startswith(str(look_json.get("png_md5"))[:16])
              and look_json.get("pptx_md5") == str(rs.file_digest(out_path))[:16]
              # 判据源码指纹：判据（look.py / spec_lint.py / 本键函数）改了，旧 look.json 一律不算复用。
              # 没有这一条时，改完判据直接跑会 reused，终端照旧 6/6，新判据一次都没执行（假绿源头）。
              and look_json.get("dependency_key") == rs.key_look(spec_path, out_path, png_path))
    t0 = time.perf_counter()
    if png_ok and not ctx.rebuilt and not ctx.a.force:
        ctx.phase("look", "reused", f"渲染图对应当前成品 ({look_json.get('png_md5')})", 0)
    else:
        rc, out, secs = run([PY, SKILL / "scripts" / "look.py", "--spec", spec_path,
                             "--pptx", out_path, "--reference", ref_path,
                             "--outdir", look_dir], 300)
        ctx.renders["normal"] += 1
        if rc == 2 or (rc != 0 and not png_path.exists()):
            # rc==2 是 look.py 的"没渲染出来"；旧实现只看"png 不存在"，于是盘上留着
            # 上一轮的 rendered.png 时，渲染失败会被当成"五条通过"（2026-11-09 实测）。
            ctx.phase("look", "error", out.strip()[-400:], secs)
            ctx.fix("look", "渲染失败：本次没有产出新的渲染图（盘上的旧图不算数）")
            ctx.state = "error"
        else:
            fails = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
            ctx.phase("look", "executed", f"{len(fails)} 条 FAIL" if fails else "五条通过", secs)
            for f in fails:
                ctx.fix("look", f.replace("FAIL ", ""))
            # 把渲染结果写进 build 报告，供 gates 复用（含 png md5）。
            # 只补渲染证据，**不重签构建摘要**（2026-11-10）：pptx_md5 一律沿用构建阶段记下的值。
            # 以前这里写 `file_digest(out_path)`，于是「构建后被换掉的 pptx」只要渲染成功一次，
            # 就被重新认证成"由当前输入构建"——渲染阶段没有资格替构建作证。
            ev = _refresh_build_evidence(bdoc, out_path, png_path, ctx.W, ctx.H)
            if ev is None:
                print(f"  NOTE 渲染的成品与构建报告摘要不一致（{str(bdoc.get('pptx_md5'))[:8]}… ≠ "
                      f"{str(rs.file_digest(out_path))[:8]}…）→ 不补写 build 证据，下轮由 build 重判")
            else:
                rs.write_report(check_dir / "build.json", ev,
                                rs.key_build(spec_path, out_path, ctx.W, ctx.H))

    return ctx.state == "error"


def _stage_presence(ctx: _Ctx) -> None:
    """5) presence（便宜、抓整块缺失）。"""
    page, spec_path, out_path, check_dir = ctx.page, ctx.spec_path, ctx.out_path, ctx.check_dir
    pkey2 = rs.key_presence(spec_path, out_path)
    prd = rs.read_report(check_dir / "presence.json", pkey2)
    t0 = time.perf_counter()
    if prd["validity"] == "current" and not ctx.a.force:
        ctx.phase("presence", "reused", rs.describe(prd), 0)
        perrs = (prd.get("report") or {}).get("errors") or []
    else:
        rc, out, secs = run([PY, HERE / "gates.py", "--stage", "presence", "--page", page,
                             "--spec", spec_path, "--out", out_path], 60)
        pd, perrs, pwhy = ctx.errs_from(check_dir / "presence.json", pkey2)
        ctx.phase("presence", "executed" if rc == 0 else "error",
                  pwhy or (f"{len(perrs)} 项缺失" if perrs else ""), secs)
        # 崩掉的闸不会产出 errors；不补这一条，error 阶段就只剩一个标签，
        # 裁决看不到它，preview 会带着洞返回 ready（2026-11-07 实测）。
        if rc != 0 and not perrs:
            ctx.fix("presence", pwhy or "gates presence 失败")
    for e in perrs:
        ctx.fix("presence", e)


def _stage_editable(ctx: _Ctx) -> None:
    """6) 可编辑（delivery 必需；单独计渲染）。"""
    page, spec_path, out_path, check_dir = ctx.page, ctx.spec_path, ctx.out_path, ctx.check_dir
    if ctx.a.mode == "delivery":
        ekey = rs.key_editability(spec_path, out_path, ctx.W, ctx.H)
        rd = rs.read_report(check_dir / "editability.json", ekey)
        t0 = time.perf_counter()
        if rd["validity"] == "current" and not ctx.a.force:
            ctx.phase("editability", "reused", rs.describe(rd), 0)
            if not rd["passed"]:
                rep = rd.get("report") or {}
                ctx.fix("editability", f"疑似贴图 {rep.get('suspect')} 块（报告 current/fail）")
        else:
            rc, out, secs = run([PY, HERE / "editability.py", "--page", page,
                                 "--spec", spec_path, "--out", out_path,
                                 "--width", ctx.W, "--height", ctx.H], 600)
            # 可编辑性默认静态读 pptx，不渲染；只有显式 --render-check 才双渲（计入 diagnostic）
            rd, eerrs, why = ctx.errs_from(check_dir / "editability.json", ekey)
            ctx.phase("editability", "executed" if rc == 0 else "fail",
                      why or (f"{len(eerrs)} 项" if eerrs else ""), secs)
            if rc != 0:
                rep = rd.get("report") or {}
                tail = out.strip().splitlines()[-1] if out.strip() else "见输出"
                if rep:
                    ctx.fix("editability", f"疑似贴图 {rep.get('suspect')} 块")
                else:
                    ctx.fix("editability", tail)
    else:
        ctx.phase("editability", "skipped", "preview 不验证可编辑性；delivery 才证明")


def _stage_coverage(ctx: _Ctx) -> None:
    """6.5) 漏块（判据 7）。**四种结果分开显示**，不新增交付布尔（禁令 3）。

      pass      白名单之外没有成块残差
      fail      给了白名单仍有残差 → blocking `to_fix`（按现有规则处理）
      unproven  证据不足（没给白名单 / 白名单没覆盖）→ 显示「待确认」，**只提示不阻塞**
      error     工具崩了 / 没产出新报告 / 报告状态不认识 → 执行错误，整页不能 ready

    为什么 unproven 不能再写成 pass（2026-11-10 用户拍板）：两者在阶段表里长得一模一样，
    人看到 pass 就以为验过了，而机器其实什么都没证明——"未验证"被冒充成"通过"，
    正是本库反复踩的那个坑。缺证据不是失败，但也绝不是通过。
    白名单放 `<页>/coverage_whitelist.json`（`{"rects": [[x0,y0,x1,y1], …]}`，人写）。
    """
    t0 = time.perf_counter()
    page, check_dir = ctx.page, ctx.check_dir
    wl = Path(page) / "coverage_whitelist.json"
    rep_path = check_dir / "coverage.json"
    before = rep_path.stat().st_mtime_ns if rep_path.exists() else None
    cmd = [PY, SKILL / "scripts" / "coverage.py", "--page", page, "--check"]
    if wl.exists():
        cmd += ["--whitelist", str(wl)]
    rc, out, secs = run(cmd, 180)
    # 只认**这一轮新写出来的**报告：进程崩在写文件之前时，盘上留着的是上一轮的结论，
    # 拿它判"这次通过"等于让上一次的运行替这一次作证。
    fresh = rep_path.exists() and (before != rep_path.stat().st_mtime_ns)
    j = (_json(rep_path) or {}) if fresh else {}
    st = str(j.get("status") or "")
    n = int(j.get("n_blocks") or 0)
    hint = str(j.get("verdict_hint") or "")
    skipped = ((j.get("boxes_basis") or {}).get("skipped") or [])

    if rc not in (0, 1) or st not in ("pass", "fail", "unproven"):
        tail = ((out or "").strip().splitlines() or ["无输出"])[-1][:200]
        why = "没产出新报告" if rc == 0 else f"rc={rc}"
        ctx.phase("coverage", "error", f"覆盖率检查没跑成（{why}）：{tail}", secs)
        ctx.fix("coverage", f"覆盖率检查执行错误（{why}）：{tail}")
        return
    if st == "fail":
        ctx.phase("coverage", "fail", f"{n} 块残差（给了白名单仍在）", secs)
        ctx.fix("coverage",
                f"漏块 {n} 块：{hint}——改版式/补块，或确认是背景后写进 "
                f"coverage_whitelist.json（别为了变绿把可疑块塞进白名单）")
    elif st == "unproven":
        ctx.phase("coverage", "unproven",
                  f"{'无白名单' if not wl.exists() else '白名单不覆盖'}，{n} 块残差未判", secs)
        ctx.fix("coverage", f"待确认（不阻塞）：{hint}", blocking=False)
    else:
        ctx.phase("coverage", "pass",
                  "白名单之外无残差"
                  + (f"；{len(skipped)} 个未产出对象未计入覆盖" if skipped else ""), secs)
        if skipped:
            ctx.fix("coverage",
                    f"{len(skipped)} 个声明对象没进成品、其框未计入覆盖："
                    f"{', '.join(str(s.get('id')) for s in skipped[:8])}", blocking=False)


def _stage_verdict(ctx: _Ctx) -> None:
    """7) 统一裁决。"""
    page, spec_path, out_path = ctx.page, ctx.spec_path, ctx.out_path
    png_path, check_dir = ctx.png_path, ctx.check_dir
    blocking = [f for f in ctx.to_fix if f.get("blocking", True)]
    # 阶段崩了（status=error）不能算 ready：以前只读 to_fix，presence 崩掉时
    # to_fix 为空，preview 会带着 error 阶段返回 ready/exit 0（2026-11-07 实测）。
    # diagnostics:* 的 error 是有意的“没有就不跑”，不参与裁决。
    has_error = _has_error_phase(ctx.phases)
    if ctx.a.mode == "delivery" and not (has_error or blocking):
        # 全部必需报告就绪时，由 gates 给唯一裁决（它还会做 OOXML 检查）
        # --width/--height 必须传：渲染尺寸不是 spec.canvas 时，gates 若按画布算键，
        # build 报告永远对不上 → 永远「build 不可用」（2026-11-08 QA 实测）。
        rc, out, secs = run([PY, HERE / "gates.py", "--stage", "delivery", "--page", page,
                             "--spec", spec_path, "--out", out_path,
                             "--rendered", png_path,
                             "--width", ctx.W, "--height", ctx.H], 300)
        gd, gerrs, why = ctx.errs_from(check_dir / "delivery.json",
                                       rs.dependency_key("delivery", _delivery_deps(
                                           spec_path, out_path, png_path, ctx.W, ctx.H, ctx.inv)))
        # 运算符优先级坑：`why or f"..." if gerrs else ""` 会被解析成
        # `(why or f"...") if gerrs else ""`，于是 stale 报告（gerrs 为空）时
        # 连 why 一起被吞掉，只剩一个没有原因的 `delivery fail`（2026-11-08 实测）。
        _why = why or (f"{len(gerrs)} 项" if gerrs else "") or (out.strip().splitlines()[-1][:120] if out.strip() else "")
        ctx.phase("delivery", "pass" if rc == 0 else "fail", _why, secs)
        for e in gerrs:
            ctx.fix("delivery", e)
        # 素材 WARN 与 inventory 的 WARN 都只记录、不阻塞：签字证明不了"看过图"
        # （乱写三个字母也能过），但它们从来没出现在终端是真缺口——这里统一印进"记录 N 项"。
        # 两个前缀都要收：`素材：`（gates 里 C5 的接线）与 `inventory：`（inventory.check 的 warns）。
        # 只认**有效**报告：stale/invalid 的 notes 属于上一版输入，印出来会被当成本次结论
        # （2026-11-09 审计实测：stale 文件里的"未签 3 条"原样进了 tips）。
        if gd.get("validity") == "current":
            for n in (gd.get("report") or {}).get("notes") or []:
                if str(n).startswith(("素材：", "inventory：")):
                    ctx.fix("delivery", str(n), blocking=False)
        ctx.state = "ready" if rc == 0 else "blocked"
    else:
        ctx.state = "blocked" if (blocking or has_error) else "ready"


def _stage_promote(ctx: _Ctx) -> None:
    """7b) 收敛值并回 spec。"""
    # 必须放在 finish 之前、且**紧接一次 plan 重刷**：spec 的 md5 一变，plan 报告立刻 stale，
    # 而 _verdict_now 读到 stale 会把 plan_errors 当成空 → 文字压图/出界两条证据凭空消失（假绿）。
    page, spec_path = ctx.page, ctx.spec_path
    if ctx.state == "ready":
        moved = _promote_override(spec_path, ctx.ov_path)
        if moved:
            run([PY, HERE / "gates.py", "--stage", "plan", "--page", page,
                 "--spec", spec_path], 120)
            ctx.phase("spec:promote", "executed",
                      f"{moved} 项收敛值已写回 spec（原值→spec.baseline.json，plan 已重刷）", 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--spec", default="spec.json")
    ap.add_argument("--out", default="out.pptx")
    ap.add_argument("--mode", choices=("preview", "delivery"), default="preview")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--adopt-assets", action="store_true",
                    help="把历史素材按当前输入记为已核验（不重生成）；没有 _meta 记录的素材默认要求核验")
    ap.add_argument("--force", action="store_true", help="忽略复用，强制重建")
    ap.add_argument("--only", default=None,
                    help="只跑这些阶段（逗号分隔，按字母名匹配，见阶段表）；其余记为 skipped。"
                         "分段跑不写 verdict.json——半跑的结果不许冒充交付结论。")
    a = ap.parse_args()

    # 解释器哨兵：sys.executable 只保证“和当前解释器一致”，不保证它装了依赖。
    # 打印出去的命令与子进程都继承 PY，缺依赖时必须在这里拦住，别等到子进程里炸。
    try:
        import pptx  # noqa: F401
    except ImportError:
        # 环境错与内容错分开：2 = 解释器/依赖不对，1 = 页面还没做完。
        print(f"REFUSED: 当前解释器 {PY} 没有 python-pptx；"
              f"按 SKILL.md §1 换成装了依赖的解释器", file=sys.stderr)
        return 2

    ctx = _Ctx(a)                      # 准备共享状态（路径 / 画布 / 阶段表 / 待修清单）
    rc = _stage_preflight(ctx)         # 0) 前置：缺文件就别往下走
    if rc is not None:
        return rc
    ctx.t_all = time.perf_counter()    # 计时起点与原来一样：preflight 之后、素材之前

    # 每个阶段块前问一句 ctx.want(...)：不给 --only 时恒 True（行为与改前逐字一致），
    # 给了就只跑点名的段、其余记 skipped。
    if ctx.want("assets:provenance", "assets:regenerate"):   # 0) 素材：来源与失效
        _stage_assets_provenance(ctx)          # 一个函数产两个阶段名 → want 收两个
    if ctx.want("normalize"):                  # 0.5) 规范归一（字号/全局件）——必须在 plan 前
        _stage_normalize(ctx)
    if ctx.want("plan"):                       # 1) plan
        _stage_plan(ctx)
    if ctx.want("assets"):                     # 2) assets
        _stage_assets(ctx)
        _stage_overflow(ctx)                   # 2b) 溢出求解（0 轮，spec 层）：跟着 assets 跑
    if ctx.want("build"):                      # 3) build
        if _stage_build(ctx):
            return ctx.finish(ctx.state)
    if ctx.want("look"):                       # 4) look（唯一正常渲染）
        if _stage_look(ctx):
            return ctx.finish(ctx.state)
    if ctx.want("presence"):                   # 5) presence
        _stage_presence(ctx)
    if ctx.want("editability"):                # 6) 可编辑（delivery 必需）
        _stage_editable(ctx)
    if ctx.want("coverage"):                   # 6.5) 漏块（判据 7，三态，不阻塞）
        _stage_coverage(ctx)
    if ctx.want("delivery"):                   # 7) 统一裁决
        _stage_verdict(ctx)
    if not ctx.only:                           # 7b) 收敛值并回 spec
        # 只在整跑时并：`--only` 半跑的 state 可能仍是初值 "ready"，那时并回会把
        # override 的收敛值写进 spec.json —— spec.json 是用户的交付定义，半跑不许动。
        _stage_promote(ctx)
    return ctx.finish(ctx.state)


def _verdict_now(spec_path: Path, check_dir: Path, look_dir: Path, png_path: Path,
                 out_path: Path, mode: str, inv: dict | None,
                 W: int, H: int) -> dict:
    """把五条布尔的证据从盘上收上来，交给 verdict.collect 出结论。

    为什么读报告而不读阶段变量：阶段变量只在"这次执行"的分支里存在（复用分支里是空的），
    而报告是"这一版输入+成品"的唯一事实。两条路径读同一份，结论才不会因为
    「这轮是跑的、上轮是复用的」而不同。
    """
    look = _json(look_dir / "look.json") or {}
    png_md5 = rs.file_digest(png_path)
    pptx_md5 = rs.file_digest(out_path)
    # renders = 「这张图就是这一版成品渲出来的」：png 与 look.json 记录的 png 摘要一致，
    # 且 look.json 记的成品摘要也是当前成品。任一不一致都说明图对不上成品（换过成品没重渲）。
    renders = bool(
        png_md5 and pptx_md5 and look.get("png_md5") and look.get("pptx_md5")
        and png_md5.startswith(str(look.get("png_md5"))[:16])
        and pptx_md5.startswith(str(look.get("pptx_md5"))[:16]))

    prd = rs.read_report(check_dir / "presence.json", rs.key_presence(spec_path, out_path))
    presence_ok = bool(prd["validity"] == "current" and prd["passed"])

    # editability 只在 delivery 跑：preview 阶段它是 skipped，此时 None 表示"本次没有这条证据"，
    # 不能当成 false（那会把 preview 的每一页都印成 5/6）。
    edit_ok: bool | None = None
    if mode == "delivery":
        erd = rs.read_report(check_dir / "editability.json",
                             rs.key_editability(spec_path, out_path, W, H))
        edit_ok = bool(erd["validity"] == "current" and erd["passed"])

    # spec_lint 的两条只在 plan 报告有效时可读；过期报告里的结论属于上一版 spec。
    plrd = rs.read_report(check_dir / "plan.json",
                          rs.key_plan(spec_path, inv if inv else {"items": []}))
    plan_errors = ((plrd.get("report") or {}).get("errors") or []
                   if plrd["validity"] == "current" else [])

    return vd.collect(look=look, renders=renders, presence_ok=presence_ok,
                      editability_ok=edit_ok, plan_errors=plan_errors)


def _promote_override(spec_path: Path, ov_path: Path) -> int:
    """把 override 里的收敛值并回 spec.json，override 只留 _meta。

    为什么要并回去（用户 2026-11-09 提出）：override 是"渲染后修正"，它一直是**唯一**
    记得收敛值的地方；一旦 override 丢失（换目录、清缓存、手滑），下次就跑回初值，
    前面十几轮的收敛白做。并回 spec 后 spec 就是"已收敛的基线"，override 回归临时层。

    安全措施：
    * **spec.baseline.json 只写一次**——它保存人最初写的排版意图，后续再并也不覆盖；
    * 只并 `elements`，assets 的 src 与 _meta 留在 override（那是素材来源摘要，不是页面数据）；
    * 由调用方保证只在 `state == "ready"` 时调用：半成品绝不固化。
    """
    ov = _json(ov_path) or {}
    els = ov.get("elements") or {}
    if not els:
        return 0
    spec = _json(spec_path) or {}
    by_id = {e.get("id"): e for e in spec.get("elements") or []}
    moved = 0
    for eid, patch in els.items():
        el = by_id.get(eid)
        if not isinstance(patch, dict) or el is None:
            continue
        el.update(patch)
        moved += 1
    if not moved:
        return 0
    base = spec_path.with_name("spec.baseline.json")
    if not base.exists():
        base.write_text(spec_path.read_text(encoding="utf-8"), encoding="utf-8")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    ov.pop("elements", None)
    ov_path.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
    return moved


def _delivery_deps(spec_path, out_path, png_path, W, H, inv) -> dict:
    # 唯一实现处是 gates.delivery_deps。这里不再自己拼一份：两份实现算出的键必然漂，
    # 后果是 gates 刚写的报告被 cycle 判 stale、读不到 errors、只显示 fail 不给原因
    # （2026-11-08 实测：gates 与 cycle 的子键各写一份，算出两个不同的键）。
    # W/H 必须一起传下去：以前被这个函数吞掉，gates 一律按 spec.canvas 算，
    # `--width 999 --height 555` 时 build/editability 两个子键永远对不上。
    import gates as _gates
    return _gates.delivery_deps(Path(spec_path), Path(out_path), Path(png_path),
                                Path(spec_path).parent, int(W), int(H))


def _finish(page, check_dir, mode, state, phases, to_fix, renders, secs,
            verdict: dict | None = None, partial: bool = False,
            only: list | None = None, out_path=None, inputs_md5: str | None = None) -> int:
    """收尾：写 cycle.json（过程）+ verdict.json（结论），打印阶段表与最后一行裁决。

    `verdict=None`（直接调用本函数的测试/内嵌用法）时只写 cycle.json：
    没有证据就不产出结论文件，比产出 5 个 false 更诚实。

    `partial=True`（`--only` 分段跑）走的就是上面那条无结论的路，另外：
    * cycle.json 里多一个 `"partial": true` —— **只在分段跑时出现**，正常跑的键集合
      一个字都不变（既有报告契约不受影响）；
    * **旧 verdict.json 改名成 verdict.stale.json**：整跑写下的结论会原样留着，谁打开
      `_check/verdict.json` 都还看到 `ready`（包括 deck.py 和下一个接手的人）——文件会替
      "半跑不作结论"这句话撒谎。改名既保留了"最后一次整跑的证据"，又让"本页结论"这个位置空出来；
    * 末行改写 `状态 partial … 未写 verdict.json`，别让人把半跑的表当成交付结论。
    """
    # 分段跑不许让旧结论继续冒充：这一步必须发生在写 cycle.json **之前**。
    # partial=False 时一个字节都不动（正常跑的产物位置与键集合保持原样）。
    stale = None
    if partial:
        vp = check_dir / "verdict.json"
        if vp.exists():
            stale = check_dir / "verdict.stale.json"
            os.replace(vp, stale)
    doc = {
        "page": str(page), "mode": mode, "state": state,
        # 产物身份：`verdict.json` 里没有产物路径/md5，"这份结论是对哪个 pptx 出的"
        # 在磁盘上读不出来，只能靠人记。48 页正好撞上（out 与 out2 同字节数、不同 md5、
        # 锁未释放），结论无法自证。cyc.json 是过程记录不是契约文件，加这个键不破 verdict 契约。
        "out_md5": (__import__("hashlib").md5(out_path.read_bytes()).hexdigest()[:16]
                    if (out_path is not None and out_path.exists()) else None),
        # 输入身份（2026-11-10）：正式合并前的"这版输入"校验用。
        # 与 out_md5 一起回答两个不同的问题——「结论对吗」看 out_md5，「结论还属于这版输入吗」看这个。
        "inputs_md5": inputs_md5,
        "phases": phases,
        "renders": renders,
        "to_fix": to_fix,
        "secs": round(secs, 2),
        "written_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    if partial:
        doc["partial"] = True
    _write_atomic(check_dir / "cycle.json", doc)
    print(f"\n{'阶段':22s} {'状态':12s} {'秒':>6s}  说明")
    for p in phases:
        print(f"{p['name']:22s} {PHASE_LABELS.get(str(p['status']), p['status']):12s} "
              f"{p['secs']:6.2f}  {p['reason']}")
    print(f"\n渲染：正常 {renders['normal']} / 可编辑扰动 {renders['editability']} / "
          f"诊断 {renders['diagnostic']}")
    # 「待修」只数阻塞项。提示项（背景装饰未重建这类已决定不做/只记录的东西）
    # 单独一行印，否则页面明明 ready，终端永远是「待修 2 项」——用户据此以为没做完。
    blk = [f for f in to_fix if f.get("blocking", True)]
    tips = [f for f in to_fix if not f.get("blocking", True)]
    if blk:
        print(f"\n待修 {len(blk)} 项：")
        for f in blk:
            print(f"  [{f['stage']}] {f['message']}")
    else:
        print("\n待修 0 项")
    if tips:
        print(f"记录 {len(tips)} 项（不阻塞交付）：")
        for f in tips:
            print(f"  · {f['stage']}: {f['message']}")
    if partial:
        if stale:
            print(f"\n旧 verdict.json 已失效（改名 {stale.name}）——分段跑不作交付结论。")
        print(f"\n状态 partial（{secs:.2f}s）→ 只跑 {', '.join(only or [])}，未写 verdict.json"
              f" → {check_dir / 'cycle.json'}")
    else:
        print(f"\n状态 {state}（{secs:.2f}s）→ {check_dir / 'cycle.json'}")
    if verdict is not None:
        # 唯一裁决产物：一个文件、五条布尔。写完立刻把同一份内容印成**一行**——
        # 执行者不必打开 JSON，"这页对不对"在终端末尾就能读完（含没通过的那几条的名字）。
        # `deliverable` 是**交付结论**，不是第六条布尔：五条布尔只证明"文字是真文字/没出画布/
        # 没漏块/渲染成功"，它们测不到"报告没写完、整块漏掉却仍 5/5"这类事
        # （实测：漏 3 块时五条布尔照样全 true）。
        # 只在 delivery 写：preview 回答的是"还差什么"，不回答"能不能交付"，多两键会破坏
        # 既有键集合契约（test_preview_... 就是钉这条）。
        if mode == "delivery":
            dv, dv_why = vd.deliverable_of(state, to_fix, checks=verdict)
            verdict["deliverable"] = dv
            verdict["deliverable_why"] = dv_why
        vd.write(check_dir, verdict, mode)
        # 原因块必须印在 verdict 行**之前**：终端最后一行永远是那句结论，脚本和人都只看得到它。
        if mode == "delivery":
            dv = verdict.get("deliverable")
            if dv != "ready" and verdict.get("deliverable_why"):
                print(f"交付判定 {dv} 的原因：")
                for w in verdict["deliverable_why"]:
                    print(f"  · {w}")
        print(vd.line(verdict))
    # 退出码必须与交付结论一致：只看 state 的话，「缺标定 → rework」这种页面
    # state 仍是 ready、exit 0，下游拿退出码判成败的脚本会照样放行——等于把
    # 刚堵上的口子在另一个入口又开一次（stability 审计 2026-11-08 点名）。
    code = {"ready": 0, "blocked": 1, "error": 1}.get(state, 1)
    # 只在 delivery 下按交付结论改退出码：preview 是现状快照，不参与裁决
    # （verdict.write 也只在 preview 里多写一个 "mode":"preview"，同一个口径）。
    if mode == "delivery" and verdict is not None \
            and verdict.get("deliverable") in ("rework", "reject"):
        code = 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
