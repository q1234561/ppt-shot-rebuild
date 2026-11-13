# 素材分流与抠图规范

## 先答一个问题，再选 `mask`

**这个素材要不要脱离原底色？**（顺序不能反，也不能挑最省事的整块留图）

* **有圆底的图标（圆盘 + 内部图案）**：**只扣里面的图，圆底用 PPT 原生 `ellipse` 重建**。
  - 深色/纯色/渐变圆底上的白色图标：先建原生 `ellipse` 圆底，内部图案用 `mask: "duotone"` 单独抠取（裁框仅包住内部图案，不包含圆底外圈）。
  - 浅色圆底上的彩色图标：先建原生 `ellipse` 圆底，内部图案用 `mask: "pictogram"` 或 `mask: "flat"` 单独抠取。
* **要脱离原底色**（非圆底或不可拆分的独立主体）：
  只允许不贴整块底的 `mask`：`chroma` / `pictogram` / `flat` / `duotone` / `cutout`；
  `circle` 仅用于照片类或无法拆层的真圆徽章，不用于“圆底+单色/双色图标”。
* **不需要脱离原底色**（主体自带柔边，且落地底色就是原底色）：
  才用 `rect` / `feather` / `rectmask`。

## `mask` 参数速查

`spec` 里**没有 `lane` 字段**（代码不读它）。不写 `mask` 默认走 `cutout`（matting，约 3min/块，缺模型时 `cutout.py` 退出码 3）。不想画的元素直接从 `elements` 删掉，别用 `skip: true`（会连带把 `text_ok` / `text_is_text` 打成 false）。

| `mask` | 适用场景 | 关键参数与约束 |
|---|---|---|
| `rect` | 显式整块留图（图表/面板/截图） | 必须显式写 `mask: "rect"`；产出硬边整块 |
| `feather` | 保留源像素又融底 | `feather_px: [左, 上, 右, 下]` |
| `rectmask` | 框压住别的元素但被压的不是主体 | `feather_px` + `mask_rects` |
| `pictogram` | 蓝色图元压浅底，可多部件 | `picto_minbg`（浅蓝底盘给 40） |
| `flat` | 单一底色、内部有白高光 | `bg_color`、`flat_tol`（浅底 18，深底 60）；只删与裁框边界连通的底色 |
| `chroma` | 主体饱和、底色淡 | `chroma_sat`（默认 45）；保留多个连通域 |
| `circle` | 真圆（p95 ≤ 2px） | `circle: [cx, cy, r]`，**bbox 边长必须 ≥ 2r+4**（每边余量 ≥ 2px） |
| `duotone` | 单色前景压彩色/渐变底 | `duo_bg_deg=3`、`duo_noise=0.16`；裁框外扩 6px |

## 过覆盖检查（`assetcheck`）

抠完后看 `rectness` 与 `carry` 两个独立指标：

* **`rectness`**：除 `mask: "rect"` 外，所有 mask 的 `rectness ≥ 0.95` 直接判死（`fail`）。主体本身是矩形时正解是写 `mask: "rect"`，不是换 mask 蒙过去。
* **`carry`**：只在 `{feather, rectmask}` 且 `rectness < 0.95` 且 `carry > 12%` 时判死。底色量不出时记为 `null`，状态为 `unproven`（不阻塞交付）。
* **目检留痕**：交付时 `WARN` 会在终端点名但不阻塞。人看过多底色效果后，可在 `inventory.json` 对应行写 `reviewed_on_bg`（属人工白名单字段，重建 `inventory` 时会按 `id` 保留）。`locked_reason` 只服务 `spec_lint` 的「文字压图」豁免，不算目检签字。
* 实在抠不干净且不适合整块留图时，才退 `cutout`（matting）或退 `rect` 并在 `inventory` 记 `gaps`。
