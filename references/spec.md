# `spec.json` 与字号规范

## 硬规矩（错了不报错，只画错）

* **文字块必填 `color`、`size_pt`、`type`**：缺 `color` 或 `size_pt` 在 `spec_lint` 阶段直接判 FAIL；`type` 必须落在白名单内（文字 / 图片 / 表格 / 线条），把文字写成 `type: "shape"` 会被 `size_probe` 静默跳过。
* **字体固定微软雅黑**：雅黑与参考字体的固有宽差约 3~8%，**禁止拿 `letter_spacing_pt` 凑字宽**（默认 0，只在“这一行放不下”且改框宽无效时作最后手段）。
* **层序**：默认 容器 100 / 图片 200 / 文字 300，同层按 `spec` 数组先后渲染；需覆盖时显式写 `z_index`。
* **容器预设形状**（仅 7 种 + 2 类）：`rect` / `rounded_rect` / `ellipse` / `chevron` / `right_arrow` / `left_arrow` / `diamond`，另加 `freeform`（`points`）与 `line`（`p:cxnSp`）。连线写 `line`，不拿 0 高矩形凑；不认识的形状会被 `build.py` 静默兜底为 `rounded_rect`。
* **原生表格三前提**：① 放在 `containers` 段；② 必须给 `size_pt`；③ 写 `type: "table"`（字段 `rows`/`cols`/`cells`，默认无框线，需框线写 `borders: true`）。
* **`gradient_angle`**：0=左→右，90=上→下，45=左上→右下，315=左下→右上。

## 字号定稿口径

字号是**排版决策**，不按墨高、墨宽或总宽反解（反解会把中英文墨高差与字距差算进字号）：

1. **换算公式**：`size_pt = pitch_px × 72 / dpi`（`dpi` 取自 `spec.canvas`）。
2. **多字块校验**：同字号 ≥ 2 行的块可用 `pitch` 校验，与参考差 ≤ 3% 即过。
3. **正文吸档**：整套视觉规范写在根目录 `visual.json`（`type.scale`、`type.cap_pt`、`type.max_snap_pt`、`global.nav`、`global.footer`）。`cycle` 的 `normalize` 阶段会在 `plan` 前自动把 ≤ `cap_pt` 的正文字号吸到最近档位（超 `max_snap_pt` 报离群），之后由 `spec_lint` 检查是否溢出。

## `token` 写法

* 值写成**整串** `"{组.名}"`（如 `"{palette.ink}"`），禁止拼串（如 `"{size}/2"`），未知 token 直接报错。
* `spec_lint --bare` 只读 raw（`expand=False`），`build` 才消费展开视图，防止闸门自毁。

## 常用字段速查

| 字段 | 作用 | 取值 / 内部换算 |
|---|---|---|
| `capsule` | 圆角矩形变胶囊 | `true` → `adjustments[0]=0.5`，此时 `radius_px` 失效 |
| `radius_px` | 圆角半径（页面像素） | `adj = min(0.5, radius_px / min(w, h))`，默认 0.08 |
| `gradient` + `gradient_pos` | 多档渐变 | ≥3 档必须给递增 `0~1` 的 `gradient_pos` |
| `glow` | 白色柔光亮边 | `{color, radius_px, alpha}` |
| `shadow` | 外阴影 | `{color, blur_px, dist_px, dir_deg, alpha}` |
| `adj` | 箭头调整 | 仅对 `right_arrow` / `left_arrow` 生效：`[尾高÷总高, 头长÷总宽]` |
| `z_index` | 覆盖层序 | 越小越靠下 |
| `letter_spacing_pt` | 字距 | 默认 0，非凑宽工具 |
| `covers_text` | 文字压图豁免 | 需同时在 `inventory.json` 对应行写 `locked` + `locked_reason` |
| `line` | 描边 | `{color, width_pt, alpha, dash, head, tail}`，`dash` 仅认 `dash` / `sysDot` / `lgDash` |
