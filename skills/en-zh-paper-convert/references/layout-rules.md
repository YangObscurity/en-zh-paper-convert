# 版式判定与几何（layout-rules）

## 1. 三条铁律 R1/R2/R3

| # | 条文 | 含义 |
|---|---|---|
| **R1** | **版式照原件** | 每篇按**原件自身版式**出稿：单栏/双栏、纸张、页边距、栏间距一律对齐原件；不得以「统一风格」为由改成别的版式 |
| **R2** | **禁止全局覆盖** | 不许把某一模式（如 `conference` 双栏）批量套用到所有篇目；**逐篇判定、逐篇留证** |
| **R3** | **先视觉审计，或以用户提供为准** | 版式决定必须**先渲染目视确认**（或用户明确指定）；机器判定只能作为**候选**，不得直接定稿 |

用户原话：「原格式是什么格式就使用什么格式，不允许全局覆盖，必须要先视觉审计，或者由用户提供。」

---

## 2. 版式判定

### 2.1 机器判定**仅作候选**

`scripts/classify_papers.py` 的口径：取正文若干页的文本块宽度（过滤 `<40pt` 宽或 `<10pt` 高的块），算**行宽中位数 / 页宽**。

| 中位数 | 输出 |
|---|---|
| ≤ 0.50 | 候选 `double` |
| ≥ 0.60 | 候选 `single` |
| 中间 | `ambiguous` |

### 2.2 为什么机器判定**必须**目视复核

基准实测三种自动判据**全部**有假阳/假阴：

| 判据 | 失效模式 | 实例 |
|---|---|---|
| 行宽中位数 | **图表密集页把中位数拉低** → 真单栏被判成 `double` | S1 `Warden_2023` 机器判 double、**实为单栏**（整页信息图）；#11/#08/#28 漏判 |
| 中缝空白带 | 被**跨栏图**污染 | 块分布不对称时数值无意义 |
| 纯文本跨中缝 | **把全部 28 篇误判为单栏** | 灾难级误判 |

**结论**：机器判定只能产出**候选清单**，用于决定"哪些需要重点看"，**不能直接定稿**。

### 2.3 目视确认操作

```
① 渲染每个候选篇目的正文页（含 1 页正文 + 1 页图表密集页）为图像
② 拼成联系表（contact sheet），逐篇目视，记录栏数
③ 边缘例（中缝比 0.2–0.6，或 L/M/R 块分布悬殊）→ 必须**逐页 110dpi 高清**看
④ 用户明确提供了版式 → 直接采用，不再争议
```

### 2.4 判定结果必须写台账（出稿硬前置）

台账列（TSV）：

```
NN  原件  原件版式  判定依据  审计方式  审计人  审计时间  采用模式  备注  纸张  页边距L/T/R/B  版心宽
```

- **第 8 列「采用模式」**取 `access`(1栏) / `conf`(2栏) / `tri`(3栏)。
- **第 11 列「页边距」列序是 `L/T/R/B`**（左/上/右/下），与 `detect_geometry.py`、
  `runpaper.py` 的 `margins_pt` 一致。**代码为准**。
  ⚠️ 早期文档误写为 `T/R/B/L`；照抄会把四个边距整体错位（等于把版心旋转），
  而且页面尺寸仍然正确、门控仍然 PASS，**极难发现**。
- **出稿时省略模式 → 从台账取**
- **命令行模式与台账冲突 → 拒绝执行（exit 2）**
- **台账无该篇记录 → 拒绝出稿**
- **台账栏数超出 1–3 栏 → 拒绝出稿（exit 2），不得降级**

> 这是**技术强制**，不是文档约定——把 R2 写成代码里的硬门控，才能防止"某次偷懒全局套用"。

### 2.5 实测分布（28 篇基准论文）

| 原件版式 | 采用模式 | 篇号 | 数量 |
|---|---|---|---|
| 单栏 | `access`（`article` 单栏） | 07 08 09 11 12 13 25 26 27 28 | **10** |
| 双栏 | `conf`（`article[twocolumn]`） | 01–06 10 14–24 | **18** |

**违规案例（引以为戒）**：2026-10-13/14 全库 28 篇一律双栏 → 其中 **10 篇单栏原件被错套**。#27 因此出现「一页一张全宽表 + 大半页空白」（文本覆盖率 24–44%，原件 98%）——全宽表在双栏里只能当浮动体，LaTeX 排不下就往后推。

---

## 3. 几何测量（照原件）

### 3.1 测量口径

```
① 只取"正文页"（文本块 ≥ 8），跳过封面/目录/整页图页
② 取该页的 文本块 ∪ 图块 的并集 bbox
③ 按 3% / 97% 分位去离群（防水印、页眉页脚、出血元素）
④ 取中位数 → paper(width,height) / margins(T,R,B,L) / text_width / text_height
⑤ 逐篇落到 geometry-per-paper.json
```

### 3.2 ⚠️ 单位：**必须用 `bp`**

| 单位 | 定义 | 结果 |
|---|---|---|
| `pt` | TeX point = **1/72.27 in** | 全库统一缩小 **0.374%**（612 → 609.71） |
| `bp` | PostScript point = **1/72 in** | 与 PDF 页面坐标一致，偏差 **0.00** |

`geometry` 的 `pt` 是 **TeX point**；而 PDF/PDF 阅读器报的页面尺寸是 **PostScript point**。
比值 `72 / 72.27 = 0.99626`。

**这条错误的隐蔽性**：每篇只差 1–3pt，看起来"像是阈值没调好"，导致有人去调阈值。
**先查系统偏差（是否全库同方向、同比例），再谈阈值。**

实测分布（`pt`，交付用 `bp` 写入 geometry）：

| 纸张 | 篇数 | 备注 |
|---|---|---|
| letter 612×792 | 13 | IEEE 系 |
| A4 595×842 | 6 | MDPI / Frontiers 等 |
| 自定义 | 5 | #03 `576×783`、#14 `576×783`、#13 `595×782`、#28 `585×783`、#07 `486×720`（ACM 小开本） |

### 3.3 geometry 写法

```latex
% 单栏
\documentclass[10pt]{article}
\usepackage[paperwidth=486bp,paperheight=720bp,
            left=44.6bp,right=46.8bp,top=61.2bp,bottom=38.4bp]{geometry}

% 双栏
\documentclass[10pt,twocolumn]{article}
\usepackage[paperwidth=612bp,paperheight=792bp,
            left=49.0bp,right=48.9bp,top=29.9bp,bottom=19.8bp]{geometry}
```

- 列宽随 `textwidth` **重算**，不要手写 `\columnwidth`。
- 缺几何数据时：打印**警告**并使用模板默认，**在回报中标注**（不要静默）。

---

## 4. 模板选择

| 原件版式 | 模板 | 理由 |
|---|---|---|
| 三栏 | `\documentclass[10pt]{article}` + `geometry` + `\usepackage{multicol}` + `\begin{multicols}{3}` | **`article[twocolumn]` 只有两栏一档**，做不出三栏 |
| 双栏 | `\documentclass[10pt,twocolumn]{article}` + `geometry` | **IEEEtran 不支持自定义纸张**，`\geometry{paperwidth=}` 与 `\pdfpagewidth` 都被忽略 |
| 单栏 | `\documentclass[10pt]{article}` + `geometry` | 单栏无此限制，但仍统一用 `article`+geometry 以保持管线同构 |

### 4.0 三栏（`tri`）写法

```latex
\documentclass[10pt]{article}
\usepackage[paperwidth=585bp,paperheight=783bp,
            left=36bp,right=40.1bp,top=51.6bp,bottom=42bp]{geometry}
\usepackage{multicol}
\setlength{\columnsep}{10.1bp}   % 照原件实测栏距（单位 bp）
\begin{document}
...
\begin{multicols}{3}
\input{abstract.tex}
\input{body.tex}
\end{multicols}
\end{document}
```

- 纸张与边距**仍由 `geometry` 精确控制**（单位 **`bp`**）；
  三栏栏宽由 `multicol` 自动等分 `\textwidth`，**不要手写栏宽**。
- `\columnsep` 是**真开关**（multicol 的，不是 geometry 的）。
  未指定时默认 10bp。
- **实测（S1，CACM 585×783，tectonic）**：正文块 x 区间
  `36.0–206.6 / 209.0–378.4 / 382.0–544.9`，与原件
  `36.0–198.9 / 209.0–371.9 / 382.0–544.9` 逐栏起点一致；
  页面尺寸 585×783。

### 4.0.1 **不支持的版式必须失败**

本 skill 只能表达 **1–3 栏**（`access` / `conf` / `tri`）。
当台账或自动检测判定原件栏数**超出 1–3 栏**、
或与所选 `--mode` 不符时：

```
runpaper.py -> REFUSING TO BUILD #<NN>: layout not supported by this skill.
               <这是什么版式 / 为什么不支持>
               What you can do: ...
               => exit 2，不产出 PDF
```

**禁止静默降级**：把 3 栏原件降级成单栏再报“成功”是**假成功**——
交付件的版式与原件不同，却没有任何信号提示。
确需降级时**必须显式** `--allow-layout-downgrade`，
并在台账「备注」列留 `DEVIATION`。

### 4.1 为什么不用 IEEEtran 出稿

IEEEtran 只认 `letterpaper`/`a4paper` 两个类选项。实验 a–h 逐一证伪了所有"绕过"手段：

| 尝试 | 结果 |
|---|---|
| `\documentclass[conferenceletterpaper]{IEEEtran}` | 缺逗号 → **两个选项都失效**，回落 letter |
| `\documentclass[conference,letterpaper]{IEEEtran}` | 只有 letter/A4 两档，非标准纸张无解 |
| `\geometry{paperwidth=…}` | **被忽略** |
| `\pdfpagewidth=\paperwidth` | **被忽略** |

实验 j 证明 `article[10pt,twocolumn]` + geometry 可精确得到 576×783 **且是真双栏**。

### 4.2 IEEEtran 兼容 shim

原作若用 IEEEtran 标题页写法，需提供最小 shim：

```latex
\providecommand{\IEEEauthorblockN}[1]{\par\noindent\textbf{#1}\par}
\providecommand{\IEEEauthorblockA}[1]{\par\noindent\small #1\par}
\providecommand{\IEEEauthorrefmark}[1]{\textsuperscript{#1}}
```

- **作者块必须用 `\parbox[t]{\columnwidth}`**，不要用 `\pageTW`。
  双栏上下文里可用宽度只有栏宽（如 229.5pt），用整页宽会越栏 510–531pt。

### 4.3 单栏 vs 双栏的内容差异

| 项 | 单栏 | 双栏 |
|---|---|---|
| 浮动体 | 宽表可原样放 | 全宽表只能进 `table*`；排不下会被推到后面（**造成大页空白**） |
| verbatim 算法块 | 通常够宽 | 长行必越栏 → `fvextra` + `breaklines,breakanywhere` |
| 图 | 可全宽 | 需要判断 `fullwidth`，全宽图进 `figure*` |

> **判定错误的代价不对称**：单栏原件错用双栏 → 全宽表/图被迫浮动 → 大面积空白（#27 实测）；双栏原件错用单栏 → 栏内文字过宽、阅读体验差但**不会崩**。所以**拿不准时先渲染目视，不要赌**。

---

## 5. 强制流程总览

```
① 机器初判（候选）→ classify_papers.py
② 目视审计（定稿）→ 渲染正文页 + 图表密集页，逐篇确认栏数
③ 登记台账 → format-ledger.tsv（栏数 + 纸张 + 页边距 + 证据 + 审计人 + 时间）
④ 出稿 → 按台账模式；冲突即拒绝（exit 2）；无记录拒绝
⑤ 测量几何 → geometry-per-paper.json（单位 bp）
⑥ 编译 → 门控（ERRS/MISS/hbox/译者痕迹/图号闭合）
⑦ 核对实际页面尺寸 vs 台账（±3pt）→ 冒烟目视 → commit
```

## 6. 常见版式陷阱

- **首页与正文页纸张不同**：部分期刊首页为整页通栏。以**正文页**为准测几何，首页单独处理。
- **跨栏大图/大表**：会污染中缝判据，这正是机器判定失效的主因。
- **整页信息图**（如 CACM opinion）：整页只有一张图 → 文本块极少且短 → 行宽中位数失真。
- **带水印的规范文档**（如 OMA）：水印在文本层里交错 → 既污染版式判定，也污染门控文本抽取（见 `gates.md`）。
- **罗马数字页码 / 双套页码**：不要用于定纸张。

---

## 7. 行间公式编号溢出（#01）

### 7.1 缺陷

作者把**编号写进了行间公式盒内**，且用一个**真宽度**命令当间隔：

```markdown
$$d(x_i, x_l) = \sqrt{\cdots} \qquad (2)$$
```

`\qquad` 是 **2em 实际宽度**（不是弹性胶）。公式盒宽度 = 公式 + 2em + 编号，
在双栏版心（列宽 256.6pt）里放不下，盒子直接破栏：

```
Overfull \hbox (33.19702pt too wide) detected at line 274
```

对照：全库 59 个行间公式块里 **53 个用 `\tag{}`**（`\tag` 不撑宽度），
只有 #01 的 **2 个**用 `\qquad (N)` → **只有 #01 溢出**。

### 7.2 修法（编号移出公式盒，右对齐）

`prep.py` 的 `move_disp_eqnum()`：把 **行间公式**末尾的 `\qquad/\quad/\hspace{...}` + `(N)`
从公式盒里**移出**，改为右对齐标签：

```latex
\[ <math> \]
\hfill (2)
```

实测把 `OVF_H_MAX` 从 **33.19702 → 1.0pt**（阈值 8），`ERRS=0`。

### 7.3 为什么不用另外两种也能降到 1.0 的写法

| 候选 | 实测 | 否决理由 |
|---|---|---|
| `\qquad` → `\hfill`（**仍在盒内**） | 13.19702（**FAIL**） | 弹性胶在盒内不起作用，盒子仍要按 math+tag 撑开 |
| `\resizebox{\linewidth}` 缩放 | 1.0 | **改变字号**：该公式与全篇其他公式点数不一致；且是视觉欺骗「靠缩小藏问题」 |
| `amsmath split` 拆行 | 1.0 | 需**自行发明断行点** = 改**内容**；换一篇的公式就可能是**静默错误** |
| **编号移出公式盒** | **1.0** | **不改公式、不改字号**，编号右对齐，与全库 `\tag{}` 观感一致 |

### 7.4 作用域（窄，刻意）

- 只处理**行间公式** `$$...$$`；**行内公式不动**（行内 `\qquad` 是合法间距命令）。
- 只移**末尾**的「间距命令 + `(数字)`」；`\tag{}` **逐位不变**（53 个块）。
- 只处理**完全相同的重复显示**的重复编号（丢重复的那个，公式保留）。

### 7.5 落盘形式很关键

`\hfill` **必须与收尾 `$$` 同行**：

```
$$<math>$$\hfill (2)      ✅  pandoc -> \[<math>\]\hfill (2)
$$<math>$$                ❌  单独一行 \hfill (2) 被当正文
\hfill (2)                     -> pandoc 转义成 \textbackslash{}hfill，排出字面反斜杠
```

### 7.6 自校验

`prep.py` **无条件**打印计数，防止静默 no-op：

```
EQTAG moved=2 left=0 duplicate=0
```

`moved=0` 且 `left=N` 表示该篇没有此类编号（正常，如全用 `\tag{}`）；
`moved` 与 `left` 之和应等于该篇行间公式块数。

回归证据：经 16 条单测用例、边界用例及全库 30 篇逐位比对验证（`\tag{}` 53→53、
行内公式 406→406、行间块 59→59 全不变）。
