# 截图转可编辑 PPT

**目标**：在保证**可编辑性**与**整套一致性**的前提下，降低**每套**的制作成本。
生产单位是**一套**（一个客户的一套视觉），不是一张图——一套的视觉常量只定义一次（`visual.json`，【待建】P3），页面只写内容。

| 优先级 | 维度 | 指标 | 现状【实测】 | 目标 | 承载物 |
|---|---|---|---|---|---|
| 1（门槛） | 可编辑性 | 真文本率 / 无整页位图（含多图合计） | 100% | 保持 | `editability` |
| 2 | 一致性 | 导航条**位置**极差（`nav.x0` / `nav.y0`） | **0px**（规范 `global.nav`，出稿前由 `impose` 强制） | ≤2px | `impose.py` 施加 + `deckalign --check` 复核 |
| 3 | 一致性 | 同套色数 / 字号级数 | 268 色 / **17 种**（正文 8 档 + 标题 9 种） | ≤15 色 / 正文 ≤8 档 | 色：`spec_lint --bare`；字号：`impose.py`（档位表在 `visual.json` 的 `type.scale`） |
| 4 | 成本 | 首页 / 边际页 | 21 min / 未知 | ≤25 / ≤13 min | `_check/cost.json`【待建】P5 |
| 5（约束） | 保真 | 漏块数 | 1 处（「治疗」） | 0 | `coverage.py`（cycle 的 `coverage` 阶段已接线，四态，只有 `fail` 阻塞） |

> **非目标**：页内像素相似度不设判据（见禁令 2），保真只在"漏块 = 0"这一条上设闸。
> **禁止复活**的路径见 §7（D1~D13），那 13 条都已被证伪——改个名字重来一遍也算复活。

### 禁令（不留退路）

1. **不做母版 / `slideLayout` / 页型 layout 库。**（D1、D2）
2. **不做像素相似度判据**（SSIM / MAE / 差异像素率 / 逐元素比像素）。（D4）
3. **不新增第 6 条 verdict 布尔。** 裁决只走 `CHECK_KEYS`（5 条）+ blocking `to_fix`；新判据要拦交付就走 `to_fix`，不加布尔。（D12）
4. **禁止把 token 展开写进 `load_merged` 的默认路径。** 闸门只读 raw（`expand=False`），展开视图只喂 `build`——否则 `spec_lint` 看不到裸值，"裸值 = 0"这条判据自毁。（D11）

一张截图 → 一份可编辑 pptx，字体固定**微软雅黑**。三条命令跑完（量 → 改 → 跑；多页再补一条 `deck.py` 合并），判据看 `_check/verdict.json` 的 5 条布尔。

> **跑批前先读 [`验收口径速查.md`](验收口径速查.md)**（一页、16 项、只写动作）。
> 它管"怎么跑才不会自我欺骗"（解释器、版本、统计过滤、两栏报告）；
> 本文管"判据是什么"。两者冲突时以本文为准。

```bash
PY=C:/Users/<your-name>/.workbuddy/binaries/python/envs/default/Scripts/python.exe
S=C:/Users/<your-name>/.workbuddy/skills/ppt-shot-rebuild/scripts
D=$S/delivery
```

## 流程（三命令）

| 步 | 命令 | 产出 |
|---|---|---|
| 1 量 | `$PY $S/extract.py --image reference.png --out spec.draft.json` | 粗框 + OCR 文本 |
| 2 改 | 读 draft，改**文本/块划分/容器/素材/字号**，存 `spec.json` | spec |
| 3 跑 | `$PY $D/cycle.py --page . --mode delivery --width W --height H` | `out.pptx` + `_check/verdict.json` |

`--width/--height` 是**画布像素**（`cycle.py` 收 `type=int`，缺省取 `spec.canvas.w_px/h_px`），**不是英寸**——按英寸传 `13.3333` 会直接报错白跑一次。
**分段重跑**：`--only build,look`（阶段名见阶段表，逗号分隔）只跑指定阶段，其余记 `skipped`；
**分段跑不写 `verdict.json`**（半跑的结果不许冒充交付结论），终端末行是 `状态 partial … 未写 verdict.json`，
**上一次整跑留下的 `verdict.json` 会被改名成 `verdict.stale.json`**（保留"最后一次整跑的证据"，同时腾空"本页结论"那个位置），
终端会明说一句 `旧 verdict.json 已失效…`；`cycle.json` 里多一个 `"partial": true`。
写错阶段名直接退，不静默忽略。**不要在成稿页上为了省时间跑 `--only build,look`**：它把本页结论降级成
`verdict.stale.json`，撞上过期的 plan 报告就是假绿（D8）；改了 `visual.json` 一律整跑，因为 `plan` 的依赖键要重算。

**字号怎么定（口径重定 2026-11-10）**：字号是**排版决策**，不是从参考图反解出来的量。
`extract` 给的是初值，定稿由人按设计意图拍；**不按「渲染墨迹 vs 参考墨迹」校准**——
那是在拿不可复原的外观当标准（原字体、原渲染器、生成模型的随机性都丢了，对齐必然失败，
失败率 100%，返工是判据的必然结果而不是执行问题）。

四条口径，按顺序用：

1. **换算有唯一公式**：`size_pt = pitch_px × 72 / dpi`，`dpi` 取自 `spec.canvas`（本机现 125.4）。
   不再出现 `1.742` 这类手写常数——代码里 `grep -rn 1.742 scripts/` 为 0（本文这一次是说明它不存在，不是用法），写死它等于写死 dpi。
2. **`pitch` 只做校验，不做定稿**：它只在**多字块**（同字号 ≥2 字）可测；与参考 pitch 差 ≤3% 即过
   （实测 84.4px → 48.4pt，48pt 渲出 83.9px）。`extract` 目前只内部用 pitch 算行距、不输出该字段，
   `pitch_px` / `size_pt_from_pitch` 属【待建】P4。
3. **单行块**（一个块只有一行字，测不出 pitch）允许用墨宽反解，但**必须打标**
   `size_pt_src:"ink-width"`——它只是初值，不许当定稿依据，更不许拿它反推"参考字号应该是多少"。
4. **墨高不得用于定字号**：中文墨高 > 拉丁字符、字距差异都会被算进字号。P04 六块正文墨高全部 35px，
   被反解成 20.8 / 19.2 / 19.91——同一页同一档正文算出三个字号，这就是判据失效的样子（D3）。

**字距 `letter_spacing_pt` 是排版参数，默认 0**，只在"这一行放不下"时用；**不许拿它凑字宽**。
宽度差 3~8% 是雅黑与参考字体的固有差，不是缺陷，不要为它调参（真实事故：为把标题从 +14px 压到 +1px
加了 −1.82pt，而文档同页正写着"不做像素级复刻"）。
**不要再引入"反解器"**——2026-11-09 退役的 `refine.py` 干的就是"按 pitch / 墨宽反解 → 写回 spec 定稿"，
它把字距差异和墨高差都算进了字号，每次产出都要人改回来。

**要看差异就打开两张图**：`look/stack.png`（叠加）与 `look/overlay.png`（参考红 / 成品青），
人眼一眼看得出偏没偏。**机器不比。**

**不要再跑任何"逐元素比像素"的报告。** 这条路已经删过两次（`verify.py`、`diff_report.py`），
每删一次都会在别处再长一个出来。识别特征：报告里出现"最大边偏差 / 还差 N px / MAE / 差异像素%"
这类可追的数字——看到就停下，**不要照着它改 bbox / 字号 / 字距**。

深底白字仍无机器覆盖（亮部与文字同为高亮，量不准），靠人眼；这是已知下界，不是漏项。

`cycle` 内部按序跑（加粗的 10 个就是 `PHASE_NAMES` 的全集，`--only` 只认这些名字）：
**assets:provenance** → **assets:regenerate** → **plan** → **assets**（溢出闭式解夹在这一段内部，不是独立阶段）
→ **build** → **look** → **presence** → **editability** → **coverage** → **delivery**，最后 `spec:promote` 收尾
（只在 `state == "ready"` 时并回 spec，且 `--only` 分段跑时一律跳过）。
**收敛值会并回 spec**：`state == "ready"` 时 `cycle` 把 `override.json` 的 `elements` 并进
`spec.json`，原文件另存 `spec.baseline.json`（只写一次，保留人最初的排版意图），
`override` 只留素材来源摘要。理由：override 一丢（换目录/清缓存/手滑）就退回初值，前面十几轮收敛全白做。
半成品绝不固化——只在 ready 时并。另：`--only` 分段跑**一律不并**（`spec:promote` 只在整跑时执行），
spec.json 是用户的交付定义，半跑不许动它。

`inventory.json` 缺了/与 spec 不同步会被自动重建（按 spec+override 派生）；`assets/` 缺了由 assets 阶段重生成。
但 inventory 里的人工字段（`inventory.HUMAN_KEYS`，共 6 个：`approx_of` / `gaps` / `crop_note` / `locked` / `locked_reason` / `reviewed_on_bg`）**只能人写**：
重建按 id 搬回人工字段，id 一改名那行就跟着丢，NOTE 会点名是哪几个。

页面目录只要两样东西：`spec.json`、`reference.png`。

## §1 spec 的硬规矩（错了不报错，只画错）

- **文字块必须显式给 `color` 和 `size_pt`**：缺任何一个在主链里都是**硬错**——`spec_lint` 记 FAIL（`缺字段 color` / `缺字段 size_pt`）→ plan 不过 → build REFUSED「plan 不可用」。所谓"缺 color 落 #000000、缺字号落 18pt"只在**裸跑 build**（页面没有 `inventory.json`、不走新规）时成立，那种"看着做完了"的样子最容易骗人。报错会一次点名**所有**缺字号的块：
  `REFUSED: 2 个块没有 size_pt：t3, t9——字号必须由 spec / override 给出，不许默认（旧行为会静默画成 14pt 或 18pt）`
  深底白字把底色量成墨色是常态，别靠吸色。
- **`type` 也是必填**：`spec_lint` 要求 elements 段每个元素都有 `type`，且取值在白名单内（`text` / 图片类 / 表格类 / 线条类）。缺 `type` → plan FAIL；把文字块写成 `type:"shape"` 会被 `size_probe` **静默跳过**（它们只认文字类），字号从此没人管。
- **`style` 令牌可选，只影响溢出分组**：`layer_groups` 用 id 前缀（`t_b1` 这类）或 `style` 令牌把相邻块归组，溢出时「同组一起降字号」。它**不再**是「同层字号必须一致」的依据——那条判据已于 2026-11-09 退役：它比的是**字号反解值**，而反解值来自墨迹宽度，会把「字距差异」和「中文墨高 > 拉丁」误判成字号分裂（P04 六块正文墨高全部 35px，却被反解成 20.8/19.2/19.91，报出 1.6pt 假分裂）。
- **宽度差不是缺陷**：雅黑与参考字体的固有差 3~8%，为它调参只会得到永不收敛的循环。放不下时先改版式或框宽；`letter_spacing_pt` 只作最后手段，默认 0。
- **层序**默认 容器100 / 图片200 / 文字300，要压住就写 `z_index`。
- **容器**的形状只认 7 种预设：`rect` / `rounded_rect` / `ellipse` / `chevron` / `right_arrow` / `left_arrow` / `diamond`；另加 `freeform`(points) / `line`(落成 p:cxnSp，端点可拖)。连线就写 `line`，不要拿 0 高的矩形凑。
  **表格不是 shape**：它是 `type:"table"`（见 §6）。把 `table` 写进形状字段会被静默画成圆角矩形——build 对不认识的形状一律兜底 `rounded_rect`，不报错。
- 渲染顺序 = 同层内 spec 里的先后，圆点这类小件放容器数组末尾。
- `gradient_angle`：0=左→右，90=上→下，**45=左上→右下**，315=左下→右上（2026-11-09 实测：spec 写 45 → XML `ang=2700000`，两条路径一致；不存在"斜向反的"这回事）。

### spec 字段速查（2026-11-09 前这一节不存在，五个字段命中数为 0，只能翻 `build.py`）

| 字段 | 作用 | 取值 / 内部换算 |
|---|---|---|
| `capsule` | 圆角矩形变**胶囊** | `true` → `adjustments[0]=0.5`；药丸/胶囊标签用它，此时 `radius_px` 不再生效 |
| `radius_px` | 圆角矩形的圆角半径（页面像素） | → `adj = min(0.5, radius_px / min(w,h))`；不写走默认 0.08 |
| `gradient` + `gradient_pos` | 多档渐变 | 2 档不需要 `pos`；**≥3 档必须给 `gradient_pos`**（0~1 递增、与档数等长），build 会重写 `gsLst` |
| `glow` | 白色柔光亮边 | `{color, radius_px, alpha}`；卡片边缘只画硬线"像贴上去的"，柔边靠它 |
| `shadow` | 外阴影 | `{color, blur_px, dist_px, dir_deg, alpha}`；缺省 `#123B7A / 8 / 4 / 90 / 0.12` |
| `adj` | 箭头两档调整 | **只对 `right_arrow` / `left_arrow` 生效**：`[尾高÷总高, 头长÷总宽]`；别的形状写了无效 |
| `z_index` | 覆盖层序 | 越小越靠下；默认 容器100 / 图片200 / 文字300（同层内仍按 spec 先后） |
| `letter_spacing_pt` | 字距 | 落成 `a:rPr@spc`（1/100pt）；**排版参数，默认 0**，不是凑宽工具 |
| `covers_text` | 素材压文字豁免的**第一件** | 还必须在该 id 的 `inventory` 行写 `locked` + `locked_reason`，缺一不可 |
| `line` | 描边 | `{color, width_pt, alpha, dash, head, tail}`；`dash` 只认 `dash` / `sysDot` / `lgDash` |

## §2 素材：先选 mask，不是先抠图

**spec 里没有 `lane` 字段**：2026-11-09 交叉审计实测 `grep -rn "lane" scripts/` 零命中——代码从不读它，写 native/cutout/skip 只是空指令（旧文档在教一件不存在的事）。真正生效的是两个字段：

- **`mask`** 决定走哪条 α 路径（下表八种之一）。**不写 `mask` = 走 `cutout`（matting，最贵那条）**，缺模型时 `cutout.py` 以退出码 3 收场。
- **`skip: true`** 让 `build` 不画这个元素（只对 elements 段生效），但 `look` 仍要求它有同名文字帧，所以"不想画"别用 `skip`——它会同时把 `text_ok` / `text_is_text` 打成 false。要少画一块就直接从 `elements` 删掉。

`fastcut` 只处理 `mask` 命中下表八种之一的素材；`review:true` 且没有 `src` 的条目只是"候选提示"，`inventory` 会跳过、不要求逐条记录。**缺 `src` 的报错在 assets 阶段**（`gates.py` 的「raster 没有 src」），plan 不报。

**先答一个问题，再选 mask —— 顺序不能反，也不能"挑最省事的"。**

**这个素材要不要脱离原底色？**

- **要**（原图底色 ≠ 素材最终落地的底色，例如白底素材放进蓝卡片）
  → 只有"不贴整块底"的 mask 合格：`chroma` / `pictogram` / `flat` / `duotone` / `cutout`；
    `circle` 仅当主体本身是真圆、圆内即主体时可用（圆内留底是设计意图，不是漏抠）
- **不要**（主体自带柔边，且落地底色就是原底色）
  → 才轮到 `rect` / `feather` / `rectmask`

教训（2026-11-09，P05）：为了遮住羽化接缝而保留整块底色，结果素材实心区里 35~40% 是原图背景，
换底色即露出方块；随后又去调卡片渐变、又加形状补背景——**省事的路线返工最贵**。

下表**只是参数速查，不是优先级** —— 先用上面那个问题定 mask，再照表查参数：

| mask | 只在什么情况用 | 关键参数 |
|---|---|---|
| `mask:"rect"` | 自带柔边/白底，换底色看不出 | 要整块留图必须显式写它——**不写 ≠ rect**。注意 `rect` 产出的是**硬边整块**（跨边台阶实测 7~19，卡片内部同口径仅 0~1.6）；**「整块留图 + 柔边」目前没有合法组合**——写 `feather` 会因 `rectness=1.0` 判死。这是工具缺口，不是页面缺陷，别在页面上找说法 |
| `mask:"feather"` | 保留源像素又融底 | `feather_px:[左,上,右,下]` |
| `mask:"rectmask"` | 框压住别的元素但被压的不是主体 | `feather_px`+`mask_rects` |
| `mask:"pictogram"` | 蓝色图元压浅底，可多部件 | `picto_minbg`（底盘浅蓝给 40） |
| `mask:"flat"` | 单一底色、内部有白高光 | `bg_color`、`flat_tol`：浅底18，深底60 |
| `mask:"chroma"` | 主体饱和、底色淡 | `chroma_sat` 默认45；保留多个连通域（见下） |
| `mask:"circle"` | 真圆（p95≤2px） | `circle:[cx,cy,r]`，**bbox 边长 ≥ 2r+4**（每边余量 ≥2px）——`spec_lint` 与 `inventory` 都逐边算余量，写成"2r 正方形"会被判红 |
| `mask:"duotone"` | 单色前景压彩色/渐变底 | `duo_bg_deg`=3、`duo_noise`=0.16；**裁框必须外扩 6px**（4px 才刚过线） |

- `chroma` **保留多个连通域**（2026-11-07 改：只滤掉相对最大件明显是噪点的碎片）——柱状图三根柱、躯干+血滴这类一块就能出全，不用拆成多个 asset。
- `flat` 只删与**裁框边界连通**的底色，内部高光不会被吃。
- **`cover` 已于 2.44.0 删除，不要再去读它**——它当初是反的：实测 6 个素材换黑底，cover 最高的
  0.995/0.994/0.985 全露方框，最低的 0.818 反而干净（见 §7 D5）。低对比主体（浅色烟雾）这几个数都没有区分力，
  靠人眼或记 `unproven`，别调阈值凑绿。
- **抠完再看 `assetcheck` 的两个数（过覆盖，专抓"把背景一起搬过来"）**：
  `rectness`（实心区矩形度 = 实心像素 / 其实心包围盒面积）与 `carry`（实心区里**素材自己的像素**
  离**素材自身底色**的占比——底色由 `_asset_bg` 从素材本身取，**不读参考图**，`assetcheck.py:61-98`）。
  两条判据**不联立**，各自独立成立（`assetcheck.py:236-256`）：

  | 判据 | 判死条件 | 为什么不联立 |
  |---|---|---|
  | `rectness` | **除 `mask:"rect"` 外，所有 mask** 的 `rectness ≥0.95` | 矩形是**几何事实**，mask 是**声明**——声明不能推翻几何。真抠（chroma/flat/cutout/pictogram/duotone）产出的实心区必然是主体形状，不可能是矩形；主体本身就是矩形（图表/面板/截图）时，正解是**显式写 `mask:"rect"`**，不是改 mask 蒙过去 |
  | `carry` | 只在 `{feather, rectmask}` **且** `rectness < 0.95` **且** `>12%` 时判死 | 已被矩形判据抓到的不再报第二条理由；真抠类里 carry 高多半是主体自身颜色接近底色（实测浅蓝日历 44%），在这两类上没有区分力 |

  **`carry >25%` 不是第二道闸，是 `elif` 附加提醒**（`assetcheck.py:254`）：只有 `rectness ≥0.95`
  （也就是**已经判死**）时才走到它，所以它永远伴随一条 FAIL，从不单独出现。别把它当成"还有一关没过的宽容线"。
  **只有 `mask:"rect"` 从判死门豁免**（显式整块留图）；`circle` **不豁免**——圆内实心区是矩形照样判死。

  **底色量不出时两条判据不同命**（这是最容易看错的一处，`assetcheck.py:196-208`）：
  - `rectness` **与底色无关，永远算**——几何不该被颜色失效连坐。
  - `carry` 量不出 → `null`（不是 0.0），该素材 `status` 记 **`unproven`**（不冒充 `pass`，不阻塞交付）。
  - 但 `status` 的判定顺序是「有 FAIL 先记 `fail`」：`rectness` 判死时即使 `carry` 是 `null`，
    这一行也是 `fail` 而不是 `unproven`。
  - 唯一两者同时为 `null` 的情况是**实心区为空**（α>0.9 的像素一个都没有）。

  **大面积半透明**（α>0.9 的像素不到不透明区一半）另发一条 WARN"过覆盖判据在这类素材上失效，需人目检"，
  **只对 `{feather, rectmask}` 发**（`assetcheck.py:258-261`）——半透明玻璃/气泡是合法素材，不判死，但**不再静默**。
- **`assetcheck` 的 WARN 会在交付时被点名**（2.43.0 起）：`baked`（换底色才看得出接缝）与 `unproven`（底色量不出）都不会自己消失，
  每次跑到 `delivery` 阶段就在终端"记录 N 项（不阻塞交付）"里写清 `WARN x 条 / 已签 y 条 / 未签 z 条`
  （口径：`notes` 里带 `WARN` 的行**按行计、不去重**——同一 id 出现两行就计两条（`gate_v2.c5_assets` 里是 `warns.append(eid)`，没有去重）；带"需人目检"的 WARN 行（半透明失效、`unproven`）同样按行计入；签没签按 id 查 `reviewed_on_bg`。
  这行文字被 `tests/test_contracts.py` 的 `AssetSignTest` 钉住，改文案要同步改用例）。
  **它不拦交付**——签字证明不了"看过图"（乱写三个字母也能过），所以只当记录、不当闸。
  要留痕就在该素材的 `inventory.json` 行里写
  `"reviewed_on_bg": {"at": "2026-11-09", "by": "人", "verdict": "三种底色下均干净", "evidence": "ic_ondiffbg.png"}`
  （这个字段在白名单里，`inventory` 重建会搬回来，不会被抹掉）。**`locked_reason` 不算目检签字**——
  它在 `assetcheck` 里**已不参与任何判定**（`assetcheck.py:299-300` 收集进 `locked` 后从未使用），现在只服务 `spec_lint` 的「文字压图」豁免
  （配合 `covers_text`）。别把它当成"判据放过我"的开关，那是两件事。
  签字是**记录**，不是豁免：它不参与任何裁决，签不签都不改变 `deliverable`。
- 都过不了才 matting（约 3 分钟/块）；仍不干净就退 `rect` 整块留图，并在 `inventory` 记 gaps。

## §3 判据（只有这一处）

`_check/verdict.json` 的 5 条布尔全 true 才算**判据通过**；**能不能交**看同一份文件里的
`deliverable`（`ready` / `rework` / `reject`）与进程退出码——布尔测不到"整块漏掉""某道闸没跑完"这类事，
所以结论是第二个出口，不是第六条布尔。素材 WARN 既不进这 5 条、也不进交付结论——
它只在交付时以"记录 N 项（不阻塞交付）"的形式点名，让人看见（见 §2）。
文件名叫 `verdict.stale.json` 时**这不是本页当前结论**：那是上一次整跑的证据，被 `--only` 分段跑改名让位的（见流程表）。

| 布尔 | 来源 |
|---|---|
| `text_ok` | look.text_complete + spec_lint.文字压图 |
| `text_is_text` | look.text_is_text + editability |
| `in_canvas` | look.no_overflow + spec_lint.出界 |
| `no_missing` | presence |
| `renders` | look.render |

背景不做机器判定：`solid` 背景仍由 `look` 阶段查（写进 slide background 没有 / 四角被形状盖住），
但那条检查**不进裁决**（本项目所有页都是渐变，它在该场景下永远真空 PASS，等于没开）；
渐变页不判，"整页被贴成一张图"由 `editability` 的覆盖检查兜住——判据是**多张图合计覆盖 ≥85% 画布**，
单张 ≥85% 只是其中一种情形。**当前代码只查单张**（`editability.py:139-143`，命中即 `break`），
合计覆盖属【待建】；补上之前"拆成两张各 50%"能绕过去（D13）——**别把拆图当合规**。
另有一个前提：这条检查只在**本页没有文字块失败**时才追加（`editability.py:171` 的 `and not n_fail`），
文字已经红了就别指望它再报整页贴图。

**判据总表（每行必须有承载物；没有承载物的一律标【待建】，不许写成"已生效"）**

| # | 判据 | 容差 | 承载物（怎么判） | 判绿人 | 状态 |
|---|---|---|---|---|---|
| 1 | 导航条**位置**跨页一致 | 0 | 写入只走 `impose.py`（读 `visual.json` 的 `global.nav`，整组平移 nav+pill+div+条内标签）；`deckalign --check` 只读复核（与中位比）。**只判位置（x0/y0），宽高不判**——宽高随标签文字与胶囊高度走（实测宽 1552~1604 / 高 78~102），拉平它只能改字号字距凑字宽，那是禁止的 | 机器 | **生效**（出稿前强制） |
| 2 | 页内同类元素位置一致 | ±2px | `audit.py` 按角色聚类；**单页独有元素不适用**（报告里写明） | 机器 | 【待建】P4 |
| 3 | 色板 / 字号阶梯 | token 命中；`literal` ≤8 | `spec_lint --bare`（读 **raw**）列出文件行号 + 值；超 `--literal-allow` 即 FAIL | 机器 + **人批 literal** | 可用（P3.1/P3.2）；**只有 46 页已迁**，其余 8 页仍全裸值 |
| 4 | 复用素材一致 | md5 差 = 0 | — | — | **【取消】P2**：一客一套视觉，`role:reuse` 实测 **0**（47 个素材全无该字段），没有跨套复用场景 |
| 5 | 内容图槽位 / 尺寸 | ±2px；仅大图放宽到 1.5%（先 2px） | `audit.py`，基数 = 画布宽 | 机器 | 【待建】P4 |
| 6 | 裁切形状（mask / circle r） | 一致 | 现在只查声明（`spec_lint.py:296-307`）；要判实际圆度得读回 alpha | 机器 + 人目检接缝 | 【待建】P4 |
| 7 | 漏块 | 残差连通域 ≥ `0.0005×W×H` 才报（0 不可达） | `coverage.py`：墨迹 −（∪**成品里真有**的 spec bbox 外扩 3px + 页边 14px 忽略带 + 背景白名单）。**覆盖框只算成品里实际存在的对象**（`consts.shape_id` 对齐 presence）：候选素材（`review:true` 且无 `src`/无 `mask`）、无 `src` 的素材、没进 pptx 的对象一律不算，否则"没做的对象替自己作证"把残差擦干净。**四态**：无块 `pass` / 有白名单且仍有块 `fail`（blocking to_fix）/ 没白名单 `unproven`（显示「待确认」，不阻塞）/ 工具崩了或没产出新报告 `error`（执行错误，整页不能 ready）。另报 `unjudgeable`：整行没有任何 bbox 的行——**铺满整行的纯色块与背景同形，判不了**，别把 `pass` 当整页验过 | 人确认白名单 | 已接线（`cycle` 的 `coverage` 阶段） |
| 8 | 像素内容 | — | 禁令 2：永不上秤 | — | 生效 |

**2026-11-10 实测（9 页，`align.json`）**：`nav.x0` 极差 **51.0px**（33~84），最大偏差 **44px @ 45 号页**
（基准 = 9 页中位 40；执行计划里的 42px 用的是 5 页中位 42——同一组数据的两个基准，不是矛盾）。
参考件若一起算：`nav_pill.x0` 499px、`div.x0` 1267px、`deco_l.x0` 260px——**这三个数没有意义**，
因为它们本来就该随激活项 / 标签宽度 / 标题宽度变化。把无意义的数当判据，会逼人去"修"一个正确的设计。

**字号不要跟着图走**：9 页 150 个文字块原本有 **57 种字号**——那不是档位，是每页照参考图反解出来的测量值。
现在整套只有 **8 档正文**：`16 / 17 / 18 / 20 / 22 / 24 / 28 / 32`，每个字号**吸到最近的档**：

- 只吸 **≤33pt**；标题（38~59pt）**不动**——那是设计判断，硬吸会把 38 吸成 32、59 吸成 56
- 吸附距离**上限 2pt**，超了就报"离群"，不硬拽（13.8 不会变成 16）
- 代价说清：文字与参考图最多差 **1.7pt**（约 5% 字宽）。所以吸完必须重跑 `spec_lint` 看**溢出**，不是吸完就算完
- 规范写在 `visual.json`：`type.scale`（档位）、`type.cap_pt`、`type.max_snap_pt`、`global.nav{x0,y0}`、`global.footer{y0,max_move_px}`；缺字段直接报错，**不给默认值**（默认值就是第二套规则）
- **有两条路，别混**：
  - **老路（9 页仍在用）**：`extract` 量出坐标字号 → `impose.py` 归一（字号吸档 + 导航/页脚移位）→ `build`。
    `cycle` 的 `normalize` 阶段（`plan` 之前）自动执行、幂等；`deckalign` 只读复核。
  - **新路（试点 1 页）**：`layout.py --from-spec` 读内容 → 按规范**算**坐标字号 → 出 spec。
    已跑通 46 页（标题+3 卡+胶囊+要点）；41/44/48/page06 报 `LayoutError`，其余 4 页构建失败。
    **尚未接进 cycle**，也没覆盖其他版式——所以现在删掉 `impose.py` 会让 8 页直接废。
- `typescale.py` 已删除（逻辑并入 `impose`）；`pagetype.py` / `palette_audit.py` 是**分析工具，不进交付链**。
  `coverage.py` 已不是分析工具：它由 `cycle` 的 `coverage` 阶段调用（2026-11-10 接线），四态里只有 `fail` 阻塞交付。

**token 怎么写（P3）**：值写成**整串** `{组.名}`（如 `{palette.ink}` / `{type.size.t_s}` / `{radius.card}`），
常量在整套根目录的 `visual.json` 里。三条硬规矩：① 不许拼串（`{size}/2` 不展开——静态不可知的值等于没有判据）；
② **闸门一律读 raw**（`expand=False`），展开视图只喂 `build`，否则"裸值 = 0"是自证（D11）；
③ 未知 token 直接报错，不原样保留。`visual.json` 的 md5 已进 plan 依赖——改调色板会让旧 plan 报告失效。

**裁决路径**：#7 只有 `fail` 走 blocking 的 `to_fix`——`deliverable_of` 只遍历 `CHECK_KEYS`（`verdict.py:205-207`），
走 `checks=` 的键会被静默忽略（D12）；加第 6 条布尔违反禁令 3；`degraded` 已于 2026-11-08 删除，别捡回来。
`unproven`（待确认）只进「记录 N 项（不阻塞）」，`error` 才让整页不能 ready——四种结果必须分得开，
把 `unproven` 写成 `pass` 等于让"未验证"冒充"通过"。

其余检查只进 `notes`，不拦交付。
`spec_lint` 报「文字压图」只有两条路：改 z 序/挪框，或给素材 `covers_text:true` **且** inventory 同 id 写 `locked`+`locked_reason`（缺一不可）。
`blocked` 最常见的两种非内容原因，各有一个开关，别去删文件：
- `assets:provenance` 未记录（素材有 `src` 但输入摘要对不上，delivery 下阻塞）→ 人看过图后 `--adopt-assets`：把现有素材按**当前输入**记为已核验，不重抠；或删掉该素材的 `src` 让它重生成。
- 报告过期（成品/配置变了但复用键没变）→ `--force`：忽略所有复用，强制重建 + 重渲。

## §4 硬边界

1. 只做两类：容器/形状/线条/文字 → 原生；参与构图的主体 → α 抠图或裁片。
2. **背景不做**：只当底纹的人像、器材、六边形、柔光不建 asset、不补裁片，只给 `slide background`（纯色或渐变）。
3. **阴影/柔边/半透明/图表数据/网页控件默认不做**。要写就在 spec 显式给 `shadow`，并登记 `approx-shape`+`gaps`。
4. 不调用生图模型、不生成 SVG、不做像素级复刻（雅黑与参考字宽差 3~8%、字重差 ±30%）。
5. **素材只含主体。** 原图的光晕、投影、反射、卡片底色一律不进素材——它们属于背景（第 2 条）。
   主体周围那圈柔光宁可丢掉（§5 已列为已知下界），也不许拿"看不见接缝"当理由把整块底色一起裁进来。
   机器判据两条，**各自独立**（见 §2）：`mask ≠ "rect"` 且 `rectness ≥0.95` → FAIL；
   `mask ∈ {feather, rectmask}` 且 `rectness < 0.95` 且 `carry >12%` → FAIL。
   确实要整块留图，就写 `mask:"rect"`（显式声明）并在 `inventory` 记 `locked_reason`。

## §5 已知下界（不追求，但要知道）

| 现象 | 原因 |
|---|---|
| 球底阴影残片、主体周围柔光晕 | matting 把投影当主体，抠不干净就记 gaps |
| 背景装饰缺失 | 背景本就不重建（见 §4 第 2 条），不报也不用登记 |

## §6 其余

- **多页 = 一套，不是 N 个单页各自为政。** 顺序不可反：**整本先定 `visual.json`**（一套一次，7 类常量：
  色板 / 字号阶梯 / 圆角 / 阴影 / 字体 / 全局件几何 / 页脚），再逐页写内容，最后合并（2026-11-10 起**两个入口**）：

  | 入口 | 命令 | 语义 |
  |---|---|---|
  | 正式 | `$PY $D/deck.py --root <整套> --out deck.pptx` | 只读**验过的单页 `out.pptx`**，不重新 build；页序取自 `<整套>/deck.json` 的 `pages`（不是目录名排序）。每页过身份闸：`delivery` 整跑、非分段、`verdict.deliverable=ready`、成品摘要 == 结论记录的、输入摘要 == 当前输入。任一不满足就**点名拒绝**，不自动重建 |
  | 草稿 | `$PY $D/deck.py --root <整套> --out draft.pptx --draft`（或 `--pages p1 p2`） | 逐页 build 后合并，不校验结论。只看"长什么样"，不产生交付件 |

  背景按 `p:bg` 整段搬运（纯色/渐变/图片填充都在里面）；旧实现只按 spec 重设纯色，
  渐变页合成后会**静默变白**。合并先写临时文件，成功才发布——失败不覆盖已有的正式文件。
  对齐在 build→look 之间与 deck 合并后各跑一次 `deckalign --check`（【待建】P1b）。`visual.json` 文件本身【待建】P3，这里先落口径。
  **项目必须自带 `deck.json` / `visual.json`**（放整套根）：归一阶段只找「页目录」和「上一级」两处。
  技能里**没有**默认配置可回退——旧客户九页的那份原件已移到 `tests/fixtures/deck-9pages.json`
  与 `tests/fixtures/visual-9pages.json`，**只作夹具与示例**（照抄结构、换成自己的页）；
  它以前是 `templates/deck.json` 的自动兜底，会让没声明的项目悄悄套上别人的九个页名与角色。
  没有 `deck.json` 时 `normalize` 阶段记 `skipped` 并说明找过哪两处，不猜。
  **反例就是现状**：9 页各自收敛再拼 → 268 色 / 71 个字号 / 全局件极差 51px（【实测】）。
  逐页调参调不出整套一致，那一层只能靠单一来源注入 + 静态闸门（`spec_lint` 裸值模式）保证。
- 表格（build 写出真的 `p:graphicFrame/a:tbl`，不是文字框拼的）：**三个前提缺一不可**——① 放在 **`containers`** 段（放 `elements` 会被当文字画）；② **必须给 `size_pt`**（`elements` 段有必填校验、容器段没有，所以容器里的表格缺它是在 build 里 `KeyError` 崩，不是 REFUSED）；③ 写 `type:"table"`。字段 `rows/cols/cells`，默认无框线，要框线写 `borders:true`。
- `gate_v2` 只剩两项：`c5_assets`（已被 `gates.py` 接进主链）和 `c3_classify`（`SHAPE_PER_UNIT=3`，**未接线**，单独跑 `gate_v2.py --page` 才生效）。C2 形态保真 / C4 连通域完整性已于 2026-11-09 退役——它们是复刻语境的判据。
- 测试入口：`$PY -m unittest discover -s tests`（条数以输出为准，别在这里抄数字）。PATH 上的 python 没 numpy，看到"1 条测试 import 错"先换解释器。
- 交付四行：原生 N / 抠图 N / 未做 N / 点名降级处。图片可移动 ≠ 内部可编辑，分开写。
- **交付前可选：字号精确比对** `$PY $D/size_probe.py --page <页目录> [--pptx <路径>]`。它读产物 `a:rPr/@sz`（1/100pt 整数）比合并视图的 `size_pt`，**无容差**（`FLOAT_EPS_PT=0.005` 只吸收浮点）。五类结果 `match`/`mismatch`/`missing`/`defaulted`/`unexpected`(仅 WARN)；页目录**零个** pptx 或**多个未指定** `--pptx` → `rc=2`。它的用途是查**历史产物**（新产物缺字号已经过不了 build）。
- **字号的来源标签**（分清"这个值是谁写的"，别自己猜）：`size_pt_raw` / `weight_raw` 是归一**前**的原值（`extract.py:214-217`）；`size_pt_src` 取值 `measured`（直接测出）| `row-normalized`（被同排归一改写）| `override`（由 `override.json` 提供，打标在 `specmerge.apply()`，见 `specmerge.py:115-137`）| `ink-width`（**单行块墨宽反解，【待建】P4**；只作初值，不许当定稿依据）；`weight_src` 取值同前三项；`normalized[]` 是结构化改写记录（`extract.py:531`）。只有 `*_src == "measured"` 才算这一页自己的观测。
- 交付说明补一句：**背景：不做机器判定（渐变页无此证据）**——别让读者以为背景被验过。

## §7 死路登记表（禁止复活）

遇到"这条路好像更快/更准"的时候先查这张表。表里有的，**不再讨论，也不再试一次**——每条都有证伪证据。
要新封一条死路，必须先进这张表再改代码；反过来（先改代码再说"这是死路"）不算，那是事后解释。

| # | 死路 | 证伪证据 | 本文对应 |
|---|---|---|---|
| D1 | 母版 / `slideLayout` | 需搬 XML（非标准 API）；普通视图里客户拖不动 | 禁令 1 |
| D2 | 页型 layout 库 | 是母版的变体；9 页 IoU 最高 0.80，且受对象命名污染 | 禁令 1 |
| D3 | 字号反解（墨宽 / 墨高 / 总宽均分） | P04 六块正文墨高全 35px，被解成 20.8 / 19.2 / 19.91；同一页算不出同一个字号 | 字号口径第 3、4 条 |
| D4 | 像素相似度判据（SSIM / MAE / 差异像素率 / 逐元素比像素） | 已删两次（`verify.py`、`diff_report.py`），每次都从别处长回来；51px 在 4 页中肉眼不可见 | 禁令 2 |
| D5 | `feather` 用于"源底色 = 落地底色" | `rectness=1.0` 直接判死，本来就不需要"融"；为接缝保留底色 → 换底色露方块 | §2 先答"要不要脱离原底色" |
| D6 | 用 OCR 当覆盖率基准 | 漏「治疗」的根因就是 OCR 根本没识别出那块 | 判据 7：coverage 走墨迹残差 |
| D7 | 用页路径当素材身份 | key 实测不含源图内容 → 同 bbox 不同图会串 | 素材 key 必须含源图指纹（**P2 已取消**，本套 `role:reuse`=0；跨套复用时再开） |
| D8 | `--only build,look` 省时间 | 撞 stale plan → 假绿；还会把本页 `verdict.json` 降级 | 流程段（建议句已删） |
| D9 | 靠加高文本框避溢出 | 框高撞上素材框 → 文字压图 FAIL 2px | §1 硬规矩 + `spec_lint` 文字压图 |
| D10 | "PowerPoint 会自动缩字号" | 实测 pitch = 44.1pt = spec 原值，没有缩 | 字号口径第 1 条 |
| D11 | 把 token 展开写进 `load_merged` 默认路径 | `spec_lint.py:190` 读同一份 → 闸门永远看不到裸值，判据自毁 | 禁令 4 |
| D12 | coverage 走 `deliverable_of(checks=…)` | 只遍历 `CHECK_KEYS`，多余的键被静默忽略 → 判据永不生效 | 禁令 3 + 判据总表裁决路径 |
| D13 | 用单图 85% 阈值判"整页贴图" | 拆成两张各 50% 即绕过 | §3 `editability`（含多图合计） |
