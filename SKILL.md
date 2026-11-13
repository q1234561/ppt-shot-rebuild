---
name: ppt-shot-rebuild
description: 把 PPT 截图和已栅格化的幻灯片重建为可编辑 PowerPoint。用于用户提供单页或多页截图并要求保留原有内容、版式与对象级编辑能力的任务。
---

# 截图转可编辑 PPT

**目标**：把单页或多页 PPT 截图重建为对象级可编辑、整套视觉一致的 `.pptx`（字体固定**微软雅黑**）。
**硬边界**：不调用生图模型补画、不生成 SVG、不做像素级复刻（不设 SSIM/MAE 判据）、严禁整页或拆图贴位图。

> **跑批前先读 [`验收口径速查.md`](验收口径速查.md)**（管怎么跑不自欺；判据细节按需查阅下方参考文档）。

```powershell
$pythonPath = "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
$skillRoot  = "$env:USERPROFILE\.workbuddy\skills\ppt-shot-rebuild"
```

---

## 主流程（5 步）

| 步 | 目标 | 核心命令 | 过关条件 / 产物 |
|---|---|---|---|
| **1. 识别源图** | 明确页序、画布、文字/表格/容器与独立素材 | `extract.py` | `spec.draft.json`（人工/AI 校对漏字与块划分） |
| **2. 代表页预览** | 先定 1 张代表页的版式、层级与留白 | `cycle.py --mode preview` | `look/rendered.png` 与源图并排确认结构 |
| **3. 共用样式与收敛** | 多页从代表页提炼 `visual.json` / `deck.json`，逐页写 `spec.json` 并按故障来源修 | 改 `spec.json` + `cycle.py` | 共享色板/字阶由 `impose` 自动归一 |
| **4. 单页正式验收** | 验证真文本、出界、压图、素材与漏块 | `cycle.py --mode delivery` | 退出码 0 且 `_check/verdict.json` 为 `ready` |
| **5. 合并与交付** | 跨页对齐复核、按 `deck.json` 合并并开 PPT 检查 | `deckalign.py` + `deck.py` | 桌面 PowerPoint 打开终稿逐页目检通过 |

---

### 步骤 1　识别源图与对象

对源图通看一遍，明确画布尺寸、阅读顺序、原生对象（文字/表格/形状/线条）与必须保留为独立图片的复杂主体。运行提取器拿到粗框与 OCR 初值，结合源图人工校对漏字、误字与漏对象。

```powershell
& $pythonPath "$skillRoot/scripts/extract.py" --image "$pageDir/reference.png" --out "$pageDir/spec.draft.json"
```

### 步骤 2　先确认代表页的版式

单页直接处理该页；多页先挑 1 张能覆盖共享视觉的代表页（版式差异大时补 1 张）。重点确认画布、层级、留白与主体占位，复杂素材未定版前可用清楚标记的临时占位。

```powershell
& $pythonPath "$skillRoot/scripts/delivery/cycle.py" --page "$pageDir" --mode preview
```

* **目录约定**：若预览阶段使用了临时占位素材或假对象，必须放在独立 `preview/<页>` 目录，进入正式 `delivery/<页>` 时重新核对真实素材与引用；若直接用真实素材起稿，可在同目录从 `--mode preview` 推进到 `--mode delivery` 以复用缓存。预览结果与临时对象不得作为交付证据。
* **编写/修改 `spec.json` 与 `mask` 选择时**：按需查阅 [`references/spec.md`](references/spec.md)（必填字段、7 种预设形状、表格三前提、字号公式与 token）与 [`references/assets.md`](references/assets.md)（8 种 `mask` 选型与 `assetcheck` 过覆盖判据）。

### 步骤 3　落实整套共用样式，逐页收敛

多页项目从通过的代表页提炼整套根目录的 `visual.json`（色板、字号档位、圆角、全局件坐标）与 `deck.json`（真实页序与页面角色）；单页无需整套配置。

* **逐页收敛原则**：错字改文本、素材边缘改该素材 `mask`、位置问题改该页坐标；仅当修改 `visual.json` 时复核受影响页面。
* **不要按像素差反调字号或凑字宽**：微软雅黑与参考字体的固有宽差（3~8%）不是缺陷，`letter_spacing_pt` 默认 0；正文吸档与全局件平移由 `cycle` 内部的 `normalize`（`impose.py`）在 `plan` 前自动完成。

### 步骤 4　验每一页的正式成品

```powershell
& $pythonPath "$skillRoot/scripts/delivery/cycle.py" --page "$pageDir" --mode delivery
```

* `--width` / `--height` 默认取 `spec.canvas`（传参必须为整数像素）。
* `cycle` 内部按序执行：**assets:provenance** → **assets:regenerate** → **normalize** → **plan** → **assets** → **build** → **look** → **presence** → **editability** → **coverage** → **delivery**（整跑 `ready` 时由 `spec:promote` 把 `override.json` 并回 `spec.json`）。
* 详细判据、阻塞开关（`--adopt-assets` / `--force`）与 `coverage` 四态见 [`references/delivery.md`](references/delivery.md)。**分段重跑（`--only`）不写 `verdict.json`，禁止把分段结果当作交付结论。**

### 步骤 5　合并、打开、交付

```powershell
& $pythonPath "$skillRoot/scripts/deckalign.py" --deck "$deliveryRoot/deck.json" --root "$deliveryRoot" --check
& $pythonPath "$skillRoot/scripts/delivery/deck.py" --root "$deliveryRoot" --out "$finalPptx"
```

* 单页交付正式 `out.pptx`；多页按 `deck.json` 页序只合并已通过 `delivery` 整跑且输入/成品摘要一致的单页。
* 必须用桌面 PowerPoint 打开**最终交付文件**目检页序、渐变背景、遮挡、素材接缝与可编辑性，并附四行交付说明（原生 / 抠图 / 未做 / 降级处，注明渐变页背景不做机器判定）。
