# ppt-shot-rebuild 2.57.4 · 离线包说明

把一页 PPT 截图重建成可编辑 `.pptx` 的工作流：

```
S0 派活（spec 里给每个素材声明 `mask`；不要的元素直接从 `elements` 删掉——没有 `lane` 字段，代码从不读它）
  → S1 量测（人读参考图写 spec：框、字号、字重、色、对齐、行距；可先拿 extract 的粗框起步）
  → S2 抠图（对声明了 `mask` 的素材按 mask 表自上而下挑第一条能过的；不写 `mask` = 走 matting）
  → S3 构建（cycle --mode delivery）                      ← 一条主链入口
  → S4 目检（看 _check/verdict.json 的 5 条布尔 + 眼睛扫错字/遮挡/接缝）
  → S5 修正（2.42.0 起**自动收敛已退役**：不达标由人改，`size_probe` 做字号精确比对）
  → S6 交付（四行说明）→ deck（多页合成一份）
```

判据只有一处：`_check/verdict.json` 的 **5 条布尔 + 人眼目检**；能不能交看同一份文件里的
`deliverable`（`ready` / `rework` / `reject`）与退出码。
素材 `assetcheck` 的 WARN **不拦交付，但每次交付都会在终端点名**（`WARN x 条 / 已签 y 条 / 未签 z 条`）。
人看过图可以写 `reviewed_on_bg` 留痕；`locked_reason` 是"cover 差属口径问题"的豁免，不算目检签字。
背景不做机器判定（`solid` 页由 `look` 查但不进裁决，渐变页不判）。

`cycle` 按依赖复用有效报告：成品与配置没变就不重建、不重渲；只改文字颜色不会重抠素材；只改 JSON 缩进不会让报告失效。
2.42.0 起 `verify` / `autocorrect` / `io_gate` / `residual` / `edit_quality` 全部退役，
`cycle` 不再有 `--diagnostics` / `--profile` 两个开关，主链就是一条。

**硬边界**：不调用生图模型补画/重绘/替换素材；不生成、不导入 SVG，也不用它做中间转换。文字/形状/线条走 PPT 原生对象，素材只来自原图裁剪、抠图、掩膜、羽化与透明 PNG。

**这是给 AI agent 读 `SKILL.md` 后按顺序调脚本用的，不是双击运行的软件。**

## 一、系统要求（硬门槛）

| 项 | 要求 | 不满足的后果 |
|---|---|---|
| 操作系统 | Windows 10 / 11 | 渲染与对账走 PowerPoint COM，macOS / Linux 不可用 |
| PowerPoint | **桌面版 Microsoft PowerPoint** | 没有它就没有渲染与对账，`cycle --mode delivery` 的闭环跑不通；WPS 不能替代（`build` / `deck` 仍可用） |
| Python | 3.10+，64 位 | 语法与 onnxruntime 依赖 |
| 磁盘 | 不用 matting：≥ 20 MB；要 matting：再留 88 MB 给模型 | 空间不足时装不完依赖，或模型加载失败 |

## 二、安装（4 步）

1. 解压，把顶层目录改名为 `ppt-shot-rebuild`，放到 `%USERPROFILE%\.workbuddy\skills\`
2. `<PY> -m pip install -r requirements.txt`
   （pillow / python-pptx / lxml / numpy / opencv / rapidocr / onnxruntime / pywin32 / **scipy**）
   **`<PY>` 指装了这些依赖的那个解释器**（venv 或系统 Python 都行）。本机 PATH 上的 `python`
   可能没装 python-pptx——直接用它会 `ModuleNotFoundError`，`cycle` 会以退出码 2 拒绝。
3. 抠图模型（**只有 `mask:cutout` 用**）：`birefnet-lite-640-fp16.onnx`，**88 MB，默认不随包分发**
   （瘦身说明见 §六）。放 `models/` 或设 `PPT_MATTE_MODEL=<绝对路径>` 都行；不放也能装完、
   只是 `mask:cutout` 那一条做不了。单块约 5s。
4. 首次联网一次：`extract.py` 第一次运行会下载 OCR 模型（约 15 MB），之后离线可用。

## 三、自检（装完先跑这三条）

```bash
# 文档↔代码一致性 + 默认入口 --help 契约（不需要 PowerPoint）：应输出 PASS（12/12）
<PY> scripts/lint_doc.py

# 素材路径（不需要模型）：应输出 OK / 待抠块数
<PY> scripts/fastcut.py --spec <spec.json> --image <reference.png> --outdir <out>

# 渲染对账（需要 PowerPoint + pywin32）：应产出 rendered.png / overlay.png
<PY> scripts/delivery/renderd.py            # 常驻渲染服务，一次 Attach
<PY> scripts/look.py --spec <spec.json> --pptx <out.pptx> --reference <reference.png> --outdir <look>
```

## 四、能力与边界

- **能做**：文字与原生形状重建（7 种预设 + `freeform` 任意多边形）、素材 8 条 α 路径（`rect` 整块留图 / `feather` 羽化 / `rectmask` 矩形掩膜 / `pictogram` 图元 / `flat` 平涂 / `chroma` 色键 / `circle` 圆形 / `duotone` 单色压彩底，即 `fastcut.MASK_KINDS` 全部八项）+ matting、
  每元素决策记录、依赖键失效、单页 delivery 闭环、
  多页合并、文档↔代码一致性检查。
- **只重建可编辑元素**：容器、形状、线条、文字，以及参与构图、需要独立移动的主体素材（人物、图标、徽标）。
- **三样原生能力**（都写在 `containers` 段，字段见 `SKILL.md` §1）：连线（实线 / 虚线 / 点线 + 起止箭头）、任意元素旋转（0~360°）、原生表格（行列 / 表头加粗 / 行高列宽 / 框线开关，**默认无框线**）。
- **不能做**：没有 PowerPoint 就没有渲染对账（其余步骤照常）；没有模型时 matting 那条不可用；
  背景只接受 solid / gradient，
  图片底直接拒绝；`>90%` 画布的容器也拒绝（环境不许画进内容层）。
  **参考图上的背景装饰（医生、器械、六边形、柔光、杂色底纹）一律不重建**——它们不是可编辑元素，
  裁片只会带进参考图的文字残影；这部分差异不重建、也不参与验收（残差报告已随 `residual` 退役）。
- **已知下界**：描边/圆角/柔边阴影/光晕测不出，只能估（详见 `SKILL.md` §4）。

## 五、体积构成

| 部分 | 大小 |
|---|---|
| 脚本 + 文档 + tests（不含 `models/`） | 约 0.8 MB（2026-11-09 清理后实测 802,590 字节；清理前 1.6 MB，多出来的部分是 `__pycache__` 与 `_archive/`） |
| `models/birefnet-lite-640-fp16.onnx`（可整目录排除，见 §六） | 88 MB（92,467,487 字节） |

> `_archive/`（历史版本快照）已于 2026-11-09 删除：它不被任何代码路径引用，README 旧版自己也标着"可删"。
> 压缩备份放在工作区 `_skill清理-20261109/_archive-20261109-backup.zip`（584 KB），skill 内不再保留。

## 六、分发时怎么瘦身（模型不进包）

**`models/` 整个目录可以直接从分发包里排除：93 MB → 约 0.8 MB**（2026-11-09 实测：总计 93,270,077 字节，
其中 `models/` 92,467,487 字节）。删掉或排除它不影响任何脚本的启动，也不影响除 matting 以外的任何一条 α 路径——
"包里没有模型"是**正常状态**，不是装错了。

收到包的人要 matting 时，把 `birefnet-lite-640-fp16.onnx` 放到下面任一位置（按查找优先级，先命中先用）：

| 优先级 | 位置 | 适用 |
|---|---|---|
| 1 | 环境变量 `PPT_MATTE_MODEL=<文件绝对路径>` | 模型在任意位置；**设了就以它为准，指错不会悄悄回落到别处** |
| 2 | `<skill>/models/birefnet-lite-640-fp16.onnx` | 最省事，零配置 |
| 3 | `%LOCALAPPDATA%\ppt-shot-rebuild\models\`（无 `LOCALAPPDATA` 时 `~/.cache/ppt-shot-rebuild/models/`） | 跟人不跟包，适合只读安装 / 多 skill 共用一份 |
| 4 | `<当前工作目录>/models/birefnet-lite-640-fp16.onnx` | 按项目、按页隔离 |

四档都没有时，`cutout.py` 以**退出码 3** 打印一段中文指引（这是什么、放哪、不放会怎样），
不再抛 onnxruntime 的 `NoSuchFile` 调用栈。

**不用 matting 的页面完全不受影响。** fastcut 的八种 mask —— `rect` / `feather` / `flat` / `chroma` /
`circle` / `duotone` / `pictogram` / `rectmask` —— 一条都不 import onnxruntime；只有 `mask:cutout`
会加载模型。spec 里没有 `mask:cutout` 就永远走不到那一步。

**为什么不做"自动下载"**（2026-11-08 查证：下面每个文件的大小与 sha256 都是从各站 API 实取，
不是搜出来的链接；本机直连被代理挡住，改由抓取工具取回完整 JSON）：

> 复现方式：`https://huggingface.co/api/models/<repo>/tree/main?recursive=true`，JSON 里取
> `size` 与 `lfs.oid`（即 sha256）。GitHub release 用 `https://api.github.com/repos/<repo>/releases/latest`。
> 只记 repo + 相对路径、不贴会腐烂的下载 URL —— 换版本时这两样比 URL 稳。

| 来源 | 文件 | 字节数 | sha256 |
|---|---|---|---|
| 本机在用（基准） | `birefnet-lite-640-fp16.onnx` | 92,467,487 | `289c0289fc32caa081a949a097b531347e1626d1625b3f3ff244aaf332fea983`（本机自查用；模型可自备，换了文件哈希自然不同） |
| HF `onnx-community/BiRefNet-ONNX` | `onnx/model_fp16.onnx` | 489,666,272 | `3654c741eb80bd926ada8fed1713b506ccf8d30eb1f6487e87eb9f234f33df09` |
| HF `onnx-community/BiRefNet_lite-ONNX` | `onnx/model_fp16.onnx` | 114,538,221 | `d39b897ceb16ae654c1731f3dba0cf9b368d9cae74b5a57459b455cc8bfec402` |
| GitHub `ZhengPeng7/BiRefNet` release v1 | `BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx` | 224,005,088 | 无官方 sha256 |

**没有任何公开权重与本机这个文件字节等价**，所以没有可校验 sha256 的下载源。原因：这份权重是本机
自己导出的 —— `input_image [1,3,640,640]`、producer `pytorch`；官方 lite 是 1024 输入、fp32 224 MB。
**编一个没验证过的 URL 比没有更糟**，所以这里只做指引，不做自动下载：模型自己放到位，或用
`PPT_MATTE_MODEL` 指过去。表里那几个公开文件是同族但不等价的权重，**未在本工作流验证过**，
不要指望换上就能得到相同结果。

## 七、运维须知：别拿 `out.pptx` 的 md5 当"有没有变化"

**重建一次，`out.pptx` 的外层 md5 一定会变，但内容可能一个字节都没变。**
拿它当回归信号，会把"什么都没改"误报成"变了"，真变化反而淹没在噪声里。

依据（2026-11-09 实测，同一页内容不变、先后两次构建，按时间正序）：

| 比法 | 结果 |
|---|---|
| `out.pptx` 外层 md5 | `7c93fd…` → `29abe1…` —— **变了（假信号）** |
| 解开 zip 后 42 个内部 part 的 md5 | **42/42 逐个相同** |
| zip 条目时间戳 | `00:36:20` → `01:32:40` —— 唯一差别 |

原因：pptx 是 zip 容器，保存时会把当前时间写进条目元数据，容器字节必然不同，
而内部的 XML / 图片一个都没动。

**要看变化，看这三个地方：**
1. `look/rendered.png` 的 md5 —— 渲染图不带容器元数据，变了才是真的画面变了；
2. 结构层：`_check/look.json` 的 `fails` 清单 + `size_probe.py` 的字号比对结果，逐页比这两样；
3. 确要逐 part 比 pptx：解 zip 后比内部 part 的 md5（42 个全等 = 内容没变）。
