#!/usr/bin/env python3
"""look.py — 渲染 + 叠图 + 五条可判检查。只回答"有没有"，不做像素打分。

检查（全部机器可判）：
  1 text_complete  pptx 里的文字 == spec 里的文字（逐字）
  2 text_is_text   每个 text 元素在 pptx 里是文字帧，不是图片
  3 no_overflow   渲染图里每个文字框内的墨迹不越出框（3px 内算贴合）
  4 image_present  spec 声明的 assets 总数 == pptx 里 picture 数（缺 src 也算，避免 0==0）
  5 bg_color       环境栏写了纯色，四角实测必须 == 该色值 ±2

"像不像"不在这里判——逐块位置比对（dy/dx/宽比/墨量比/pitch）不在本工具范围内。
"""
from __future__ import annotations

import argparse
import json
import specmerge
import hashlib
import socket
import subprocess
import sys
import time
from pathlib import Path

import consts
import numpy as np
from PIL import Image



def render(pptx: Path, out_png: Path, width_px: int, height_px: int) -> float:
    """Windows + PowerPoint COM 导出 PNG。

    必须显式给宽和高：只给 ScaleWidth 时 PowerPoint 会用它自己的默认高度
    （实测 1672 宽导成 1672x900），下游一 resize 就把整页纵向拉伸 4.6%，
    文字被拉长、检查变成假通过。
    """
    # 2.6.0：渲染只有一条路——常驻服务 delivery/renderd.py（端口 8731）。
    # 原 COM 实现每次都要重新 Attach PowerPoint（冷 8.1s / 热 5.2s），占端到端 95% 的时间；
    # 服务常驻后单页 0.19~0.31s，产物与 COM 路径**逐字节同 md5**（实测 1f4cef46d532、3a268ca9ebd0 两次）。
    # 服务没起就直接启动它（4s，一次性），不再保留 COM 分支——两条路会各自漂移。
    _out = Path(out_png).resolve()
    _out.parent.mkdir(parents=True, exist_ok=True)
    _t0 = time.time()

    def _send(payload: str, timeout: float = 30.0) -> str:
        sk = socket.socket()
        sk.settimeout(timeout)
        sk.connect(("127.0.0.1", 8731))
        sk.sendall(payload.encode("utf-8"))
        resp = sk.recv(256).decode("utf-8").strip()
        sk.close()
        return resp

    def _alive() -> bool:
        try:
            return _send("ping\n", timeout=2).startswith("PONG2")   # 必须是本版协议
        except OSError:
            return False

    if not _alive():
        _daemon = Path(__file__).resolve().parent / "delivery" / "renderd.py"
        if not _daemon.exists():
            raise RuntimeError(f"缺渲染服务 {_daemon}")
        try:                      # 端口上可能是旧版残留：先让它退出
            _send("quit\n", timeout=1)
            time.sleep(0.5)
        except OSError:
            pass
        subprocess.Popen([sys.executable, str(_daemon)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            if _alive():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("渲染服务起不来或协议版本不符（delivery/renderd.py）")

    resp = _send(f"render\t{Path(pptx).resolve()}\t{_out}\t{width_px}\t{height_px}\n")
    if not resp.startswith("OK"):
        raise RuntimeError(f"渲染失败：{resp}")
    if not _out.exists():
        raise RuntimeError("渲染服务没有产出 PNG")
    return time.time() - _t0


def read_back(slide) -> tuple[dict[str, list[str]], int]:
    """从成品 pptx 读回「id → 文字」与图片数。**身份一律走 consts.shape_id。**

    抽成独立函数有两个理由：① 它是「同名文字帧 / 图片数」两条判据的唯一取值处，
    散在 main 里改一处漏一处；② 不渲染就能测——渲染要 PowerPoint COM，
    而"名字改了之后还认不认得出文字"这件事与像素无关。
    """
    texts: dict[str, list[str]] = {}
    pics = 0

    def _walk(shapes):
        """递归展开组合（2026-11-09）。

        不展开的后果不是"少算"，是**判错**：一个组合会被读成"未识别 shape"，
        组内的图算成 0 张 → `image_present` 与派生清单同时错。
        `editability._iter_shapes` 一直有递归，这边没有——又一处"两处实现不一致"。
        """
        for sh in shapes:
            sub = getattr(sh, "shapes", None)
            if sub is not None:            # MSO_SHAPE_TYPE.GROUP
                yield from _walk(sub)
            else:
                yield sh

    for sh in _walk(slide.shapes):
        if consts.is_picture(sh):
            if consts.shape_id(sh) != "bg":   # 背景图不算素材（身份走 shape_id，见 consts）
                pics += 1
            continue
        if sh.has_text_frame and sh.text_frame.text.strip():
            # 键走 shape_id：spec 起了自定义 name 后 name 不再等于 id，
            # 用 sh.name 会让「同名文字帧」这条假红/假绿。
            texts.setdefault(consts.shape_id(sh), []).append(sh.text_frame.text)
    return texts, pics


def _look_key(spec_path: Path, pptx_path: Path, rendered: Path) -> str | None:
    """look 报告的复用键（判据源码指纹在里面）。没有 report_state 就返回 None，不崩。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "delivery"))
        import report_state as rs
        return rs.key_look(spec_path, pptx_path, rendered)
    except Exception as exc:            # 报告里留痕，不静默
        print(f"  WARN 算不出 look 的 dependency_key（{exc}）——复用判断会退回只看产物摘要")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--pptx", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    spec = specmerge.load_merged(a.spec)   # 必须和 build 看同一份
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ref = Image.open(a.reference).convert("RGB")
    W, H = ref.size

    rendered = outdir / "rendered.png"
    # 渲染失败必须与"渲染成功但五条里有 FAIL"分开：两者原先都退 1，
    # 而 cycle 看到 rc!=0 时只要盘上**还留着上一轮的 rendered.png** 就会当成
    # "本次执行完毕、五条通过"（2026-11-09 实测：删 pptx + 留旧 png → verdict ready / 1 条 FAIL）。
    # 这里用 2 = 没渲染出来（没有新图），1 = 渲染出来了但判据不过。
    try:
        secs = render(Path(a.pptx).resolve(), rendered.resolve(), W, H)
        got = Image.open(rendered).convert("RGB")
    except Exception as exc:                      # noqa: BLE001
        print(f"REFUSED: 渲染失败（{exc}）——本次没有产出新的 rendered.png，旧图不作数")
        return 2
    if got.size != ref.size:
        # 不做 resize：尺寸不一致说明导出或页面比例有问题，静默拉伸会把缺陷藏起来
        raise RuntimeError(f"渲染尺寸 {got.size} != 参考图 {ref.size}，检查幻灯片页面尺寸与 Export 参数")

    # ---- 1/2/4 读回 pptx
    from pptx import Presentation
    prs = Presentation(a.pptx)
    slide = prs.slides[0]
    texts, pics = read_back(slide)

    fails, notes = [], []
    want = [e for e in spec.get("elements", []) if not e.get("_symbol")]
    for e in want:
        got_txt = "\n".join(texts.get(e["id"], []))
        if got_txt.replace("\n", "") != e["text"].replace("\n", ""):
            fails.append(f"[text_complete] {e['id']} 期望 {e['text']!r} 实得 {got_txt!r}")
    for e in want:
        if e["id"] not in texts:
            fails.append(f"[text_is_text] {e['id']} 在 pptx 里没有同名文字帧（可能被图片化）")
    # 数 assets 的**总数**，不是"有 src 的"。
    # 旧写法 `if x.get("src")` 在 src 被清空时 want_img=0、pics=0 → 0==0 直接 PASS，
    # 于是一个"图全丢"的事故能同时绕过 build（静默 return None）和这条检查。判据必须锚在 spec 声明上。
    want_img = len(spec.get("assets", []))
    if want_img != pics:
        fails.append(f"[image_present] spec 声明 {want_img} 张图，pptx 实得 {pics} 张"
                     f"（差集多半是 src 缺失或图不存在，看 build 输出的 MISS/FALLBACK）")

    # ---- 3 越界：比较"渲染墨迹 bbox"与 spec bbox（纯几何，不依赖背景色）
    got_rgb = np.array(got, dtype=int)
    overflow = []

    def ink_bb(arr, box, pad=10):
        H, W = arr.shape[:2]
        x0, y0, x1, y1 = [int(round(v)) for v in box]
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(W, x1 + pad), min(H, y1 + pad)
        roi = arr[y0:y1, x0:x1]
        if roi.size == 0:
            return None
        bd = np.concatenate([roi[0], roi[-1], roi[:, 0], roi[:, -1]]).astype(np.int16)
        bg = np.median(bd, axis=0)
        m = consts.ink_mask(roi)
        if m.sum() < 15:
            return None          # 找不到墨迹（白字压白底）→ 跳过，不判 FAIL
        ys, xs = np.where(m)
        return [x0 + xs.min(), y0 + ys.min(), x0 + xs.max(), y0 + ys.max()]

    for e in want:
        bb = ink_bb(got_rgb, e["bbox"])
        if bb is None:
            continue
        x0, y0, x1, y1 = e["bbox"]
        # 四条边都必须是"墨迹超出框的像素数"，符号统一为"外扩为正"。
        # 左边写成 bb[0]-x0 会与右边符号相反（墨迹在框内时为正），
        # 比 bbox 窄的文字只要左内缩 >12px 就会被误判为溢出
        over = max(x0 - bb[0], y0 - bb[1], bb[2] - x1, bb[3] - y1)
        if over > 12:
            overflow.append(f"{e['id']}(+{over:.0f}px)")
    if overflow:
        fails.append(f"[no_overflow] 字跑出框：{overflow}")

    # ---- 5 背景色落地：spec 写了纯色，就必须真的落到 slide background 上。
    # 2026-11-08 实测：改成"与参考四角比 ≤12"会误杀 3/4 已验收页（P03 34.8 / P04 59.8 / page06 24.5），
    # 因为 §0 明令背景装饰不做。渐变页不做机器判定，别再加。
    # 纯色的渲染结果可预测（#DCEFFD → 220,239,253），所以能做二值判据。
    # 采样四角 8x8；四角被元素贴边盖住说明版式本身有问题，报 FAIL 是对的。
    bg = spec.get("background")
    if bg and str(bg.get("type", "solid")).lower() == "solid":
        hexv = str(bg.get("color", "")).lstrip("#").upper()
        want = None
        if len(hexv) == 6:
            try:
                want = np.array([int(hexv[i:i + 2], 16) for i in (0, 2, 4)], dtype=int)
            except ValueError:
                want = None
        if want is None:
            fails.append(f"[bg_color] background.color={bg.get('color')!r} 不是 6 位十六进制")
        else:
            Hb, Wb = got_rgb.shape[:2]
            probes = [(0, 0), (Wb - 8, 0), (0, Hb - 8), (Wb - 8, Hb - 8)]
            dev = max(float(np.abs(got_rgb[y:y + 8, x:x + 8].reshape(-1, 3).mean(0) - want).max())
                      for x, y in probes)
            if dev > 2:
                fails.append(f"[bg_color] 背景实测与 #{hexv} 偏差 {dev:.0f}（>2）——"
                             f"没写进 slide background，或被形状盖住了")

    # ---- 对照图（人眼工具，只出图不出数）
    # 2026-11-09 改造：这里原来还算 MAE / 差异像素并打进终端、写进 look.json。
    # 那两个数的期望值来自参考图像素——"外观当标准"的残留。它们被当判据用过
    # （"还差 14px""MAE 12.86"），驱动了多轮返工，而"像不像"本来就不是目标。
    # 现在只留 stack/overlay 两张对照图：人能看，机器不比。
    blend = Image.blend(ref, got, 0.5)
    blend.save(outdir / "stack.png")
    # 参考图红 / 成品青 的双色叠加，偏移一眼可见
    rg = np.array(ref).astype(np.int16)
    gg = np.array(got).astype(np.int16)
    over = np.stack([rg[:, :, 0], gg[:, :, 1], gg[:, :, 2]], axis=2).clip(0, 255).astype(np.uint8)
    Image.fromarray(over).save(outdir / "overlay.png")

    for f in fails:
        print("FAIL", f)
    if not fails:
        print("PASS 五条全过（文字齐全 / 是文字帧 / 不越界 / 图片数对 / 背景色落地）")
    # dependency_key：把「判据源码指纹」写进报告（见 report_state.key_look）。
    # 没有它，判据改了而产物没变时，消费方会继续复用一个"用旧判据算出来的 PASS"。
    dep_key = _look_key(Path(a.spec), Path(a.pptx), rendered)
    (outdir / "look.json").write_text(json.dumps({
        "pptx_md5": hashlib.md5(Path(a.pptx).read_bytes()).hexdigest()[:16],
        "png_md5": hashlib.md5(rendered.read_bytes()).hexdigest()[:16],
        "render_secs": round(secs, 2),
        "dependency_key": dep_key,
        "fails": fails,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
