# 正式交付判据与边界

## 裁决出口（`_check/verdict.json`）

* **5 条布尔**全为 `true` 才算判据通过：`text_ok`、`text_is_text`、`in_canvas`、`no_missing`、`renders`。
* **能不能交**看 `deliverable`（`ready` / `rework` / `reject`）与进程退出码。
* `--only` 分段跑**不写 `verdict.json`**，且会把已有 `verdict.json` 改名为 `verdict.stale.json`，分段结果绝不能充当交付结论。

## 关键检查与已知边界

* **整页贴图兜底（`editability`）**：当前代码检测单张图覆盖画布 ≥85%，多张拼图合计覆盖属待补缺口；**严禁拆成多张图绕过检查**。
* **漏块检查（`coverage`）**：基于参考图墨迹减去**成品 PPTX 里实际存在**的 `spec` 对象框（读取 `--spec` 与 `--pptx`）及人工白名单 `coverage_whitelist.json`。
  * 四态：`pass`（无残差）/ `fail`（有白名单仍有残差，阻塞）/ `unproven`（无白名单或未覆盖，显示「待确认」，不阻塞）/ `error`（执行错误或显式成品不可读，阻塞）。
  * 白名单内容变更会使旧的 `page_inputs_digest` 失效，正式合并时必须重验。
* **背景不做机器判定**：渐变页不做背景机器判定，交付说明需注明「背景：不做机器判定（渐变页无此证据）」。
* **硬边界**：
  1. 不调用生图模型、不生成/导入 SVG、不做像素相似度判据（SSIM / MAE）。
  2. 只当底纹的背景装饰（人物、器械、六边形、柔光等）不建 asset，只写 `slide background`。
  3. 阴影/柔边/半透明/网页控件默认不做，确需近似需在 `inventory` 记 `approx_of` 与 `gaps`。

## 常见阻塞开关

* `assets:provenance` 未记录：核验素材后加 `--adopt-assets` 采纳现状，或删掉 `src` 让 `cycle` 重生成。
* 需要忽略所有缓存强制重建：加 `--force`。
* `verdict.json` 在 `state == "ready"` 整跑结束时，会自动把 `override.json` 的收敛值写回 `spec.json`（原文件另存一次 `spec.baseline.json`）。

## 多页正式合并与对齐

* `deck.json` 与 `visual.json` 必须放在整套根目录（技能内无默认配置兜底）。
* 合并前用 `deckalign.py --check` 复核跨页导航条极差（≤ 2px）；合并只认 `deck.py --root <整套> --out <目标.pptx>`（只读已通过 `delivery` 整跑且输入/成品摘要一致的单页 `out.pptx`）。
* 交付说明写清四行：原生对象数 / 抠图素材数 / 未做项 / 点名降级处。
