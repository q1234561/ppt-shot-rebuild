# ppt-shot-rebuild

把**一页 PPT 截图**重建成**可编辑的 `.pptx`** —— 文字是真文字，图形是真形状，不是把截图贴回去。

> 版本 **2.57.4** · 2026-11-10 · Windows 10/11 + 桌面版 PowerPoint · Python 3.10+ · 字体固定微软雅黑

---

## 它解决什么问题

你手上有一张参考图：客户发来的、竞品截的、自己去年做的。你想要的是一份**能改**的 PPT。

常规做法是照着重新排版。一页二十分钟，一整套几十页就是一整天，而且每一页的字体、色号、行距都会
悄悄跑偏。这个项目把这件事拆成一条有产物的流水线，每步都能验：

```
S0 派活    spec 里给每个素材声明 mask；不要的元素直接从 elements 删掉
S1 量测    extract.py 出粗框 + OCR 文本 → 人改成 spec.json（框、字号、字重、色、对齐、行距）
S2 抠图    按 mask 表自上而下挑第一条能过的；不写 mask 走 matting
S3 构建    cycle.py --mode delivery          ← 主链唯一入口
S4 目检    看 _check/verdict.json 的 5 条布尔 + 人眼扫错字/遮挡/接缝
S5 修正    不达标由人改，size_probe 做字号精确比对；自动收敛已于 2.42.0 退役
S6 交付    四行说明 → deck.py 多页合成
```

## 三条硬约束

这三条不是偏好，是设计的边界。越过它们，产出就不再是"可编辑"。

1. **不调用生图模型**补画、重绘、替换素材。
2. **不生成、不导入 SVG**，也不用它做中间转换。
3. **文字/形状/线条走 PPT 原生对象**；素材只来自原图裁剪、抠图、掩膜、羽化、透明 PNG。

另外：不按像素相似度做判据（SSIM / MAE / 差异像素率）。判据只有一处 ——
`_check/verdict.json` 的 **5 条布尔** 加人眼目检。

## 能做什么 / 不能做什么

| | 内容 |
|---|---|
| **能做** | 文字与原生形状重建（7 种预设 + `freeform` 任意多边形）；素材 8 条 α 路径（`rect` / `feather` / `rectmask` / `pictogram` / `flat` / `chroma` / `circle` / `duotone`）+ AI matting；连线（实线/虚线/点线 + 起止箭头）；任意元素 0~360° 旋转；原生表格（表头加粗、行高列宽、框线开关，默认无框线）；每元素决策记录；依赖键失效（改文字颜色不会重抠素材）；多页合并 |
| **不能做** | 没有 PowerPoint 就没有渲染对账（其余步骤照常）；没有模型时 matting 不可用；背景只接受 solid / gradient，图片底直接拒绝；`>90%` 画布的容器拒绝；参考图上的装饰性背景（柔光、杂色底纹、六边形）**一律不重建**，也不参与验收 |
| **已知下界** | 描边 / 圆角 / 柔边阴影 / 光晕测不出，只能估；深底白字无机器覆盖，靠人眼 |

## 环境要求

| 项 | 要求 | 不满足的后果 |
|---|---|---|
| 操作系统 | Windows 10 / 11 | 渲染与对账走 PowerPoint COM，macOS / Linux 不可用 |
| PowerPoint | **桌面版 Microsoft PowerPoint** | 没有它就没有渲染回图，`cycle --mode delivery` 闭环跑不通（WPS 不能替代；`build` / `deck` 仍可用） |
| Python | 3.10+，64 位 | 语法与 `onnxruntime` 依赖 |
| 磁盘 | 不用 matting ≥ 20 MB；要 matting 再留 88 MB 给模型 | 依赖装不完，或模型加载失败 |

## 安装

1. **放置** —— 解压到任意位置，建议 `%USERPROFILE%\.workbuddy\skills\ppt-shot-rebuild`
2. **装依赖**

   ```bash
   python -m pip install -r requirements.txt
   ```

   9 个包：`pillow` / `python-pptx` / `lxml` / `numpy` / `opencv-python-headless` /
   `rapidocr-onnxruntime` / `onnxruntime` / `scipy` / `pywin32`。
   先用 `python -c "import pptx, numpy"` 确认这个解释器是对的 —— PATH 上的 `python`
   经常没装 `python-pptx`，`cycle` 会以退出码 2 拒绝。

3. **改 `SKILL.md` 开头两行**（本仓库里已做脱敏，是本机路径的占位符）

   ```bash
   cd <本目录>
   sed -i "s|^PY=.*|PY=$(command -v python)|" SKILL.md
   sed -i "s|^S=.*|S=<本目录>/scripts|" SKILL.md
   head -8 SKILL.md
   ```

   `PY` 必须是装了 `onnxruntime` 的那个解释器 —— 这是最常见的"环境阻塞"来源。

4. **模型（只有 `mask:cutout` 用）** —— 88 MB，不在本仓库，见下文「抠图模型」。
5. **首次联网一次** —— `extract.py` 第一次运行会下载 OCR 模型（约 15 MB），之后离线可用。

## 快速开始

```bash
PY=<你的解释器>
S=<本目录>/scripts
D=$S/delivery

# 1 量：粗框 + OCR 文本
$PY $S/extract.py --image reference.png --out spec.draft.json

# 2 改：读 draft，改文本/块划分/容器/素材/字号，存 spec.json

# 3 跑
$PY $D/cycle.py --page . --mode delivery --width 1920 --height 1080
```

`--width/--height` 是**画布像素**，不是英寸 —— 传 `13.3333` 会直接报错白跑一次。

页面目录只需要两样东西：`spec.json` 和 `reference.png`。

## 判据：5 条布尔

`_check/verdict.json` 里的 `CHECK_KEYS`，外加 `deliverable`（`ready` / `rework` / `reject`）与退出码：

| 键 | 期望值来源 |
|---|---|
| `text_ok` | 内容：pptx 文字等于 spec 声明的文字（不是等于参考图上的字） |
| `text_is_text` | 自洽：真文本帧 + 有可编辑 run，不是被贴成图 |
| `in_canvas` | 环境：元素在画布内 |
| `no_missing` | 自洽：spec 声明的元素都在成品里 |
| `renders` | 环境：能渲染出图 |

能不能交看同一份文件的 `deliverable`。素材 `assetcheck` 的 WARN 不拦交付，但每次交付都会在终端点名
（`WARN x 条 / 已签 y 条 / 未签 z 条`）。背景不做机器判定。

## 目录结构

```
.
├── SKILL.md                  # 完整规范：流程、spec 硬规矩、13 条已被证伪的禁令
├── CHANGELOG.md              # 2.26.0 起的逐版变更（含"修前 → 修后"与承载测试）
├── 00-先读我-安装与自检.md     # 一分钟装好 + 三条自检命令
├── 验收口径速查.md             # 一页 16 项，只写动作（跑批前后怎么不自欺）
├── docs/离线包说明.md         # 详细运维说明：体积构成、瘦身、坑
├── scripts/
│   ├── extract.py            # 量：粗框 + OCR
│   ├── build.py              # 建：写 OOXML
│   ├── fastcut.py            # 8 条 α 路径
│   ├── cutout.py             # AI matting（需模型）
│   ├── look.py               # 渲染回图（需 PowerPoint COM）
│   ├── deck.py / deckalign.py / impose.py / ...
│   └── delivery/             # cycle 主链、gates、spec_lint、verdict、report_state
└── tests/                    # 23 个测试文件 + fixtures
```

## 抠图模型

`models/birefnet-lite-640-fp16.onnx`（92,467,487 字节）**不随仓库分发** —— 它是包体的 95%，
放进 Git 会让每个版本都多 88 MB 历史。

需要 matting 时放到下面任一位置，按优先级命中：

| 优先级 | 位置 |
|---|---|
| 1 | 环境变量 `PPT_MATTE_MODEL=<绝对路径>`（设了就以它为准，指错不会悄悄回落） |
| 2 | `<本目录>/models/birefnet-lite-640-fp16.onnx` |
| 3 | `%LOCALAPPDATA%\ppt-shot-rebuild\models\` |
| 4 | `<当前工作目录>/models/birefnet-lite-640-fp16.onnx` |

四档都没有时，`cutout.py` 以**退出码 3** 打印中文指引，不抛调用栈。
完整包（含模型）在仓库的 **Releases** 页下载。

**不用 matting 的页面完全不受影响** —— 8 条 α 路径没有一条 import `onnxruntime`。

## 文档索引

| 想知道 | 去哪 |
|---|---|
| 怎么装、怎么自检 | [`00-先读我-安装与自检.md`](00-先读我-安装与自检.md) |
| 判据是什么、哪些路已经证伪 | [`SKILL.md`](SKILL.md) |
| 跑批前后不能做什么 | [`验收口径速查.md`](验收口径速查.md) |
| 体积构成、怎么瘦身、运维坑 | [`docs/离线包说明.md`](docs/离线包说明.md) |
| 每一版改了什么、为什么改 | [`CHANGELOG.md`](CHANGELOG.md) |

## 版本

当前 **2.57.4**（2026-11-10）。这一版的主线是**缓存身份**：

- 构建复用要求「盘上摘要 == 报告记录的摘要」，人工换过 `out.pptx` 不再被当成 `reused`
- 渲染不再给被替换的成品重签构建摘要
- `editability` 的键加入位图清单
- 素材生成键接回原图摘要，同页换截图不再复用旧 PNG
- 覆盖只计成品里真有的对象；四态 `pass / fail / unproven / error`，`unproven` 不再冒充 `pass`
- 正式合并只认已验单页（逐页过身份闸，不满足点名拒绝且不自动重建）

细节见 [`CHANGELOG.md`](CHANGELOG.md)。

## 说明

- 包内 `00-先读我-安装与自检.md` 的版本头停留在 2.44.0，正文的安装步骤仍然有效；
  版本以 `SKILL.md` / `CHANGELOG.md` 为准。
- 许可：暂未指定（默认保留所有权利）。要复用请先联系作者。
