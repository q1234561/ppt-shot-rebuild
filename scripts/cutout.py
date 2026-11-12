#!/usr/bin/env python3
"""cutout.py — 用 birefnet-hr-matting 出透明素材（本地 onnx，不联网）。

一页只跑一次批次：读 spec.assets 里 src 为空的项，逐块抠，写 assets/<id>.png。
边缘反解：alpha 过渡带像素减掉底色的贡献，换深色底不露灰圈。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

MODEL_NAME = "birefnet-lite-640-fp16.onnx"
SKILL_ROOT = Path(__file__).resolve().parent.parent
# 模型不进分发包（88 MB），所以路径不是常量而是"找"出来的：见 resolve_model_path()。
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# 缺模型时的退出码：3 = 「先配模型」这类可修复的拒绝，区别于 1（真异常）。
EXIT_NO_MODEL = 3


def user_cache_dir() -> Path:
    """用户级模型缓存目录：Windows 用 %LOCALAPPDATA%，其他平台用 ~/.cache。

    为什么不塞进 skill 目录：skill 可能装在只读位置（系统级安装、容器镜像、
    agent 的只读 skills 目录），模型该落在"跟人不跟包"的地方。
    """
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".cache"
    return base / "ppt-shot-rebuild" / "models"


def local_model_slots() -> list[tuple[str, Path]]:
    """三个"按文件名找"的位置（**不含**环境变量），顺序即优先级。

    单独成一档是因为 resolve 与报错文案都要用同一份顺序；
    以前用 model_candidates()[1:] 取"除环境变量外"，未设环境变量时那一位恰恰是
    skill 内置目录，被整段跳掉——那就是"文件明明在却报找不到"的原因。
    """
    return [
        ("skill 内置 models/", SKILL_ROOT / "models" / MODEL_NAME),
        ("用户级缓存", user_cache_dir() / MODEL_NAME),
        ("当前目录 models/", Path.cwd() / "models" / MODEL_NAME),
    ]


def model_candidates() -> list[tuple[str, Path]]:
    """按优先级返回 [(来源标签, 期望路径)]。

    顺序即优先级：显式环境变量 > 随包 / 自放 skill 目录 > 用户级缓存 > 当前工作目录。
    后三档都是"同一个文件名"，只是位置不同——换机器/只读安装时不必改代码。
    """
    cands: list[tuple[str, Path]] = []
    env = os.environ.get("PPT_MATTE_MODEL")
    if env:
        cands.append(("PPT_MATTE_MODEL", Path(env).expanduser()))
    cands.extend(local_model_slots())
    return cands


def resolve_model_path() -> Path | None:
    """找模型文件；找不到返回 None，由调用方给指引（这里不抛异常、不 print）。

    显式设了 PPT_MATTE_MODEL 就以它为准：指错了直接判"没有"，**不回落**到其他位置。
    理由：环境变量是人的明确意图，静默换一个文件跑，等于让人以为用的是大盘模型
    却在用小模型——那种错比"报缺文件"难查得多。行为上也与改动前一致
    （以前指错路径就是 onnxruntime 报 NoSuchFile 直接失败）。
    """
    env = os.environ.get("PPT_MATTE_MODEL")
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    for _label, p in local_model_slots():
        if p.is_file():
            return p
    return None


def model_source_label(path: Path) -> str:
    """回显这份权重是从哪一档找到的（排查"到底用了哪个模型"用）。"""
    for label, p in model_candidates():
        try:
            if p.is_file() and p.resolve() == path.resolve():
                return label
        except OSError:
            continue
    return "未知位置"


def fastcut_mask_names() -> list[str]:
    """fastcut 自己能出 α 的 mask 名。真值只有一处：`fastcut.MASK_KINDS`。

    文案里**不许手抄**这份清单：2026-11-07 加 rectmask 时，手抄的元组立刻漏了一项，
    导致"fastcut 出的图被 matting 又覆盖一遍"。同一课在提示文案里再踩一次，
    就会变成"指引说的 mask 数量比代码少一个"——比不写更糟。
    """
    try:
        from fastcut import MASK_KINDS
        return list(MASK_KINDS)
    except Exception:            # 缺依赖/导入异常时退回"不列清单"，仍能给出行动指引
        return []


# 分发包里不含模型（88 MB，占整个 skill 体积的 97%），所以"模型不在"是**正常状态**，
# 不是崩溃。这里把 onnxruntime 的 NoSuchFile 调用栈换成一段能照着做的中文指引：
# 说清是什么、去哪拿、不给会怎样、什么情况下根本用不到。
_MISSING_MODEL_HINT = """\
REFUSED: 找不到抠图模型 {name}（88 MB，不进分发包）
  这是什么：BiRefNet lite-640 fp16 的本地 onnx 权重，只给 mask=cutout（matting）算 α，全程离线。
  去哪拿：下面四档放任意一处就够，文件名必须叫 {name}
    1) 随包：{skill_models}  ← 最省事，放这里零配置
    2) 显式：任放一处后设 PPT_MATTE_MODEL=<该文件绝对路径>（设了就以它为准，不再回落到别处）
    3) 缓存：{cache_models}
    4) 当前目录的 models\\ （按页隔离时用）
  没它怎样：{no_model_line}
    → 本页不需要 matting 就完全不用管这个文件。
  已查过的位置：
{checked}
  → 修：把 {name}（88 MB）放进上面任一档，或设 PPT_MATTE_MODEL=<绝对路径>；{ignore_line}
"""


def _missing_model_message() -> str:
    """生成缺模型指引。checked 只列**实际检查过**的路径，不留空话。"""
    rows: list[str] = []
    env = os.environ.get("PPT_MATTE_MODEL")
    if env:
        p = Path(env).expanduser()
        rows.append(f"    - PPT_MATTE_MODEL = {env}  （{'是目录' if p.is_dir() else '文件不存在'}）")
        rows.append("      ↑ 环境变量已设，此时不再回落到其他位置；改正它或清空它")
    else:
        rows.append("    - PPT_MATTE_MODEL = （未设置）")
    for label, p in local_model_slots():
        # 真去探测，不写死"不存在"：环境变量设错时，下面几档其实可能是有的，
        # 报"存在"才能让人一眼看出"是我 env 填错了"，而不是"我压根没放模型"。
        rows.append(f"    - {label}  →  {p}  （{'有文件，但被上面的设置覆盖' if p.is_file() else '不存在'}）")

    # mask 清单从 fastcut 派生，不手抄（理由见 fastcut_mask_names 的 docstring）。
    masks = fastcut_mask_names()
    if masks:
        no_model_line = (f"只有 mask=cutout 的块做不了，其余 {len(masks)} 种 mask\n"
                         f"    （{'/'.join(masks)}）走 fastcut，一条都不碰 onnxruntime。")
    else:
        no_model_line = "只有 mask=cutout 的块做不了，其余走 fastcut 的 mask 一条都不碰 onnxruntime。"

    return _MISSING_MODEL_HINT.format(
        name=MODEL_NAME,
        skill_models=str(SKILL_ROOT / "models"),
        cache_models=str(user_cache_dir()),
        no_model_line=no_model_line,
        ignore_line="只用 fastcut 的 mask 则忽略本提示",
        checked="\n".join(rows),
    )


def load_session(path: str):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = 0
    return ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])


def model_size(sess) -> int:
    """输入边长**从模型读**，不写死。
    尺寸是模型属性，不是脚本常量：写死 2048 配 640 的 lite 会形状不匹配崩掉，
    反过来也一样。所以在这里读，换模型零改动。"""
    shp = sess.get_inputs()[0].shape
    side = shp[2] if len(shp) == 4 else None
    return int(side) if isinstance(side, int) and side > 0 else 2048


def predict(sess, rgb_u8: np.ndarray, size: int | None = None) -> np.ndarray:
    if size is None:
        size = model_size(sess)
    h, w = rgb_u8.shape[:2]
    x = cv2.resize(rgb_u8, (size, size), interpolation=cv2.INTER_AREA)
    x = x.astype(np.float32) / 255.0
    x = (x - MEAN) / STD
    x = np.transpose(x, (2, 0, 1))[None]
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
    a = out[0, 0]
    if a.max() > 1.5 or a.min() < -0.5:      # logits -> sigmoid
        a = 1.0 / (1.0 + np.exp(-a))
    a = cv2.resize(a.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(a, 0, 1)


def decontaminate(rgb: np.ndarray, a: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """C = a*F + (1-a)*B  ->  F = (C - (1-a)*B)/a，只在 0.05<a<0.95 用。"""
    out = rgb.astype(np.float32).copy()
    m = (a > 0.05) & (a < 0.95)
    if m.any():
        aa = a[m][:, None]
        out[m] = np.clip((out[m] - (1 - aa) * bg[None, :]) / np.maximum(aa, 1e-3), 0, 255)
    return out.astype(np.uint8)


def _write_src_to_override(spec: dict, spec_path: Path) -> None:
    """只把 assets[*].src 写进 override.json，spec.json 保持只读。

    为什么必须改：spec 的唯一写者是 extract.py。这两个工具原来整份回写 spec，
    等于给 spec 加了第二、第三个写者——extract 一重跑就把 src 冲掉（2.4.5 事故的结构成因）。
    只写 src，且只写相对 spec 的路径，换机器也能用。
    """
    import specmerge
    ov_path = spec_path.parent / "override.json"
    ov = specmerge.load(ov_path)
    patch = {}
    for el in spec.get("assets", []):
        src = el.get("src")
        if not src:
            continue
        p = Path(src)
        try:
            rel = p.resolve().relative_to(spec_path.parent.resolve())
            src = str(rel).replace("\\", "/")
        except ValueError:
            src = str(p)
        patch.setdefault("assets", {}).setdefault(el["id"], {})["src"] = src
    if patch:
        for k, v in patch["assets"].items():
            ov.setdefault("assets", {}).setdefault(k, {}).update(v)
        ov_path.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  src → {ov_path.name}（{len(patch['assets'])} 项；spec.json 未动）")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--pad", type=int, default=60)
    ap.add_argument("--force-ids", default="",
                    help="逗号分隔：这些素材即使已有 src 也重抠（依赖摘要变了时由 cycle 调用）")
    a = ap.parse_args()
    force = {x.strip() for x in a.force_ids.split(",") if x.strip()}

    import specmerge
    spec = specmerge.load_merged(a.spec)   # 必须看见 override.add 的素材（人手补的圆形素材就在那）
    img = np.array(Image.open(a.image).convert("RGB"))
    H, W = img.shape[:2]

    # 只抠「走 matting 这一路」的素材：circle 由 fastcut 的几何 α 负责、chroma 由色键负责、
    # rect 是整块留图不需要 α。原写法只按"src 为空"筛，会把 fastcut 刚生成的色键结果
    # 再抠一遍并写到同名文件上（覆盖）。src 因为历史 bug 常常读不到，所以要同时按 mask 排除。
    # 跳过名单从 fastcut.MASK_KINDS 派生，不再自己维护一份：
    # 2026-11-07 加了 rectmask 之后，这份手抄的元组立刻漏了一项，
    # 表现就是"fastcut 出的图被 matting 再覆盖一遍"——正是这条注释上面写的那个老 bug。
    from fastcut import MASK_KINDS as _FASTCUT_MASKS
    SKIP_MASKS = tuple(_FASTCUT_MASKS)
    todo = [x for x in spec.get("assets", [])
            if (not x.get("src") or x.get("id") in force)
            and str(x.get("mask") or "cutout") not in SKIP_MASKS]

    # 模型只在"真有块要抠"时才有用。0 块就没有 onnxruntime 什么事，不该因为包里没模型
    # 把一个根本不走 matting 的页面挡在门外（否则下面那句"不需要 matting 就别管它"是空话）。
    # 判据放在建 outdir 之前：缺模型时不留一个空目录掩盖退出原因。
    model_path = resolve_model_path()
    if todo and model_path is None:
        print(_missing_model_message(), flush=True)
        return EXIT_NO_MODEL
    # 只有"不是默认那档"时才回显来源，常规情形输出保持不变。
    if model_path is not None and model_path.resolve() != (SKILL_ROOT / "models" / MODEL_NAME).resolve():
        print(f"模型：{model_path}（来自 {model_source_label(model_path)}）")

    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    sess = load_session(str(model_path)) if todo else None
    print(f"待抠 {len(todo)} 块")
    for el in todo:
        t0 = time.time()
        x0, y0, x1, y1 = [int(round(v)) for v in el["bbox"]]
        px0, py0 = max(0, x0 - a.pad), max(0, y0 - a.pad)
        px1, py1 = min(W, x1 + a.pad), min(H, y1 + a.pad)
        crop = img[py0:py1, px0:px1]
        alpha_full = predict(sess, crop)
        # pad 内的外扩部分按比例裁回
        sx, sy = alpha_full.shape[1] / (px1 - px0), alpha_full.shape[0] / (py1 - py0)
        alpha = alpha_full[int((y0 - py0) * sy):int((y1 - py0) * sy),
                           int((x0 - px0) * sx):int((x1 - px0) * sx)]
        sub = img[y0:y1, x0:x1]
        alpha = alpha[:sub.shape[0], :sub.shape[1]]
        bg = np.median(sub[alpha < 0.05].reshape(-1, 3), axis=0) if (alpha < 0.05).any() else np.array([255, 255, 255])
        clean = decontaminate(sub, alpha, bg)
        rgba = np.dstack([clean, (alpha * 255).astype(np.uint8)])
        p = outdir / f"{el['id']}.png"
        Image.fromarray(rgba).save(p)
        el["src"] = str(p)
        # 深底预览：alpha 一致性一眼可判
        dark = np.zeros_like(sub); dark[:] = (18, 22, 30)
        for c in range(3):
            dark[:, :, c] = (clean[:, :, c] * alpha + dark[:, :, c] * (1 - alpha)).astype(np.uint8)
        Image.fromarray(dark).save(outdir / f"{el['id']}-dark.png")
        # α≈1 = 什么都没去掉 = 这块根本不该走 matting（圆盘/图标/色块该走 circle/chroma）。
        # 本机实测：右侧三个图标的 cutout α 均值 0.99~1.00，成品保留整块方形底，
        # 在卡片上露出一圈方边；后续只能靠四角硬边判据才发现（本文件 §四角检测）。
        # 在这里提前喊，比事后返工便宜。
        # 判据用"边缘 α"而不是"均值 α"：均值会被主体占空比稀释
        # （实测同一块图标：紧框 matting 均值 1.00、松框 0.81，都不说明对错；
        #   而圆遮罩素材边缘 ≈0、方片边缘 ≈1，分得开。）
        # 口径与 fastcut 的四角判据同源，不引入第二套。
        am = float(alpha.mean())
        # 判据 = 四角 8x8 的 α 均值（与 fastcut 同源）。
        # 实测：matting 出的方片四角 0.99；circle 素材四角 0.00；羽化矩形 0.45。
        k = 8
        ea = float(np.mean([alpha[:k, :k].mean(), alpha[:k, -k:].mean(),
                            alpha[-k:, :k].mean(), alpha[-k:, -k:].mean()]))
        warn = ""
        if ea > 0.75:
            warn = f"  WARN 四角α={ea:.2f}，整块没抠开（图标/圆盘请改用 mask=circle 或 chroma）"
        print(f"  {el['id']} {sub.shape[1]}x{sub.shape[0]}  alpha均值={am:.2f}  {time.time()-t0:.1f}s{warn}")

    _write_src_to_override(spec, Path(a.spec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
