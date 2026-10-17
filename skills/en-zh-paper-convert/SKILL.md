---
name: en-zh-paper-convert
description: 将英文论文 PDF 转换为格式保留的中文论文 PDF——单栏/双栏、纸张尺寸与页边距逐篇照原件，图从原件无损提取、表用 show_pdf_page 从原件矢量复刻（绝不手写 LaTeX 表），表内保留英文、图表题译中文，交付件不含任何译者信息。当用户要求「把英文论文/PDF 翻译成中文并保持原排版」「复刻这篇论文的版式和图表」「批量把论文做成中文 PDF」时使用。
license: MIT
compatibility: 需要 Python 3.11+ 与 PyMuPDF（fitz）；编译需要 xelatex/tectonic + Noto Serif SC / Noto Sans SC（Windows 自带，回退 SimSun）。源 PDF 若为完全扫描件（无文本层）需先 OCR。在 Windows 上运行：PowerShell 写 UTF-8 会带 BOM，脚本内容一律用 ASCII。
metadata:
  version: "1.0"
  domain: academic-typesetting
  source-project: 28-paper production benchmark
  repository: https://github.com/YangObscurity/en-zh-paper-convert
  pipeline-scripts: scripts/
allowed-tools: Bash Read Write Edit Glob Grep
---

# 英译中论文转换（格式保留 · 单双栏兼容）

把英文论文 PDF 转成中文论文 PDF。**核心矛盾**：译文是新的文字流，但版式、图、表必须与原件一致。
因此本 skill 的默认策略是——**能复刻的绝不重排，能目视的绝不只信机器**。

## 本技能在仓库中的位置 + 工作区根

本技能位于仓库的 `skills/en-zh-paper-convert/`（**目录名必须等于 frontmatter 的
`name`**，这是 agentskills.io 规范的硬要求；官方校验器会因此拒绝根目录直接当技能用）。

两件事要分清：

| 目录 | 放什么 |
|---|---|
| **技能根**（本目录） | `SKILL.md`、`scripts/`、`references/` —— 只有代码与文档 |
| **工作区根** | `Paper/`、`.work/`、`out/` —— 你的素材与产物 |

工作区根由 `scripts/_workspace.py` 解析（**显式 `--root` 永远优先**）：

1. 环境变量 `PAPER_SKILL_ROOT`
2. 否则：从技能根**向上**找最近的、含 `Paper/` 的祖先
3. 否则：最近的、含 `.git` 的祖先
4. 否则：技能根本身（单独拷走技能时，数据就地建）

自查：

```powershell
python scripts/_workspace.py --show
```

`setup_env.py` 是例外：`.venv/` 与 `tools/tectonic.exe` 装在**技能根内**
（它就是本技能的自带工具链）。

## P0：必需输入清单（**先读这一节，否则必然失败**）

**照做的最小序列**（命令相对**技能根目录**执行）：

```powershell
python scripts/setup_env.py            # 建 .venv + 下 tools/tectonic.exe（约 48MB，首次需联网）
#   ⚠️ 如果 tectonic 已在（TECTONIC_BIN 已设，或工作区根/技能根的 tools/ 里已有），先跑 check_env.py
#      确认环境可用，再决定是否需要 setup_env；
#      否则 setup_env 会四处找并尝试下载 48MB。
Copy-Item <素材>\*.pdf,<素材>\*.md Paper\   # 放入原件与译稿
python scripts/detect_format.py Paper\<原件>       # 版式**候选**
#   → ① **渲染正文页目视确认**（R3）② 登记 .work/format-ledger.tsv
python scripts/detect_geometry.py Paper\<原件> --json .work\<NN>\geometry.json
python scripts/runpaper.py <NN> --outdir out --geometry .work\<NN>\geometry.json
```

| 输入 | 从哪来 | 入库? |
|---|---|---|
| `Paper/<stub>.pdf` 原件 | 用户提供（**stub = 译稿除 `<NN>_` 前缀与 `_中文.md` 后缀**） | ❌ 体积大，忽略 |
| `Paper/<NN>_<stub>_中文.md` 译稿 | 用户/翻译流程 | ❌ 忽略 |
| **`.work/format-ledger.tsv`** 版式台账 | **必须重建**：`detect_format.py` → **目视确认** → 登记 | ❌ 忽略 |
| **`.work/<NN>/geometry.json`** 几何 | `detect_geometry.py` | ❌ 忽略 |
| `Paper/<NN>.figs_override.json` | **人工**（图题不在文本层时） | ❌ 本地规则（随 `Paper/` 忽略） |
| `Paper/<NN>.figs_source_gaps.json` | **人工**（原件确实缺该图时） | ❌ 本地规则（随 `Paper/` 忽略） |
| `tools/tectonic.exe` + `.venv/`（可能在技能根或工作区根） | `setup_env.py` | ❌ 忽略 |

⚠️ **两个输入缺失时门控会「拒绝出稿」/判 FAIL —— 这是设计，不是 bug**：
- 缺台账 → `exit 1`「台账中无 #NN 的版式记录 -> 拒绝出稿」（R2/R3 的强制点）
- 缺几何 → `GATE-FAIL: geom-missing`

**两个登记文件（override / SOURCE-GAP）保存在本地 `Paper/` 中** —— 它们是用户的「人工校准规则」，
是本地出稿与评测复现的组成部分（保存在 `Paper/` 下便于随译稿一并管理）。

### 文件命名契约（实测踩过的坑）
- 译稿必须是 `Paper\<NN>_<stub>_中文.md`（末尾 `_中文.md` 是定位标志）
- 原件必须是 `Paper\<stub>.pdf`（**没有 `<NN>_` 前缀**），其中 `stub` 取自译稿名去掉 `<NN>_` 和 `_中文.md` 后的部分
- 例：译稿 `Paper\30_SRplot_2023_PLOS_ONE_中文.md` ⇒ `stub="SRplot_2023_PLOS_ONE"` ⇒ 原件 `Paper\SRplot_2023_PLOS_ONE.pdf`
- **如果原件带额外后缀**（如 `..._CC-BY.pdf`、`..._v2.pdf`），
  现在 `runpaper.py` 会按顺序尝试 `stub.pdf` → `stub*.pdf` → 含 stub 关键字的任意 PDF，
  找不到时**会列出 Paper/ 下所有 PDF** 让用户一行重命名。

## 三条铁律（违反即返工）

- **R1 版式照原件**：单栏/双栏、纸张尺寸、页边距、栏间距**逐篇**对齐原件，不许「统一风格」。
- **R2 禁止全局覆盖**：不得把某一个模式（如双栏）批量套用到所有篇目。逐篇判定、逐篇留证。
- **R3 先视觉审计，或以用户提供为准**：版式决定必须**渲染目视确认**，机器判定只是**候选**。

详见 `references/layout-rules.md`。

## 交付物定义

| 项 | 要求 |
|---|---|
| 版式 | 与原件**逐篇**同栏数、同纸张、同页边距 |
| 图 | 从原件**无损提取**（原生位图直取 / 矢量图定界渲染），**不是整页截图**；图题译中文；**图题位置照原件**（见下） |
| 表 | **从原件矢量复刻**（`show_pdf_page`）；**表内保留英文**，表题中文；**绝不手写 LaTeX 表** |
| 译者信息 | **0**——译注、翻译说明、自查清单、进度标记、分隔线一律不得进交付 PDF |
| 门控 | ERRS=0、MISS=0、越栏 ≤8pt、图表编号闭合、字体全嵌入 |
| 输出目录 | 交付 PDF 默认写到 **`<workspace root>/out/`**；可用 `--outdir DIR` 改 |

> 译注**可以留在工作用 md**里（那是审计线索），但**不得进入交付 PDF**。

### 图题位置（照原件：上 or 下）

**`\captionsetup[figure]{position=top}` 不管用** —— 实测（caption v3.6b / caption3 v2.3b）
图题仍在图下方。`position` 只管 caption 框**内部**的对齐与 `\abovecaptionskip` 交换，
**不移动图题相对图形的上下位置**；位置只由 float 内 `\caption` 与 `\includegraphics`
的**先后顺序**决定。详见 `references/figures.md` §5.2。

- 原件图题在图**上方**（如 CACM opinion）→ 把 `\caption` 写在 `\includegraphics` **之前**。
- 原件图题在图**下方**（LaTeX 默认）→ 保持默认顺序。
- 位置取自 `figs.json` 的 `caption_pos`（`extract_figs.py` 自动判定，或
  `figs_override.json` 手工指定 `"caption_pos": "top"`）；`prep.py` 据此排布。

## 核心流程

### 1. 环境预检（一次即可）
确认 `fitz`、`xelatex|tectonic`、CJK 字体可用。编译引擎优先 `tectonic`（单文件、自带包管理）。

### 2. 逐篇版式审计 → 写台账（**出稿前置门控，不可跳过**）
```
① 机器初判（仅候选）：python scripts/classify_papers.py Paper/ --json .work/classify.json
      · 行宽中位数 ≤0.50 → 候选 double；≥0.60 → 候选 single；中间 → ambiguous
      · ⚠️ 图表密集页会把中位数拉低 → 真单栏被判成 double（基准实测 S1 即如此）
② 目视确认：把原件正文页（含正文页 + 图表密集页）渲染成拼版图，逐篇看栏数
      · 中缝比 0.2–0.6、或块分布不对称（L/M/R 悬殊）→ **必须逐页高清看**
③ 登记台账（列数以代码为准，模板见 §2b）：原件 | 机器初判 | 目视结论 | 审计人 | 时间 | 采用模式 | 纸张 | 边距 | 备注
      · 「采用模式」列取 **access(1栏) / conf(2栏) / tri(3栏)**；三栏写 `tri`。
      · 「边距」列序是 **L/T/R/B**（左/上/右/下），与 detect_geometry.py、runpaper.py 一致。
        **不要写成 T/R/B/L** —— 会让边距整体旋转，且很难发现。
④ 出稿时按台账取模式；命令行模式与台账冲突 → **拒绝执行**
   台账栏数超出 1–3 栏 → **拒绝出稿并 exit 2**（绝不降级）
```

### 2b. 台账模板（以代码为唯一真相源）

⚠️ 列序历史上在文档里有三种写法，**唯一真相源是代码**。
`runpaper.py::read_ledger()` 读 `p[7]`=模式、`p[9]`=纸张、
`p[10]`=边距、`p[11]`=版心宽，即下表的**第 8/10/11/12 列**。
**错一位就会报「台账中无 #NN 的版式记录 -> 拒绝出稿」，
而提示不会告诉你错在哪。**

| 列（1-based） | 1 | 2 | 3 | 4 | 5 | 6 | 7 | **8** | 9 | **10** | **11** | **12** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 字段 | NN | 原件 | 原件版式 | 判定依据 | 审计方式 | 审计人 | 审计时间 | **采用模式** | 备注 | 纸张 | 页边距L/T/R/B | 版心宽 |

可直接复制的**真实数据行**（制表符分隔，下面的 `<TAB>` 要换成真制表符）：

```
15<TAB>Zhan 等 - 2026 - HearthNet Edge Multi-Agent Orchestration for Smart Homes.pdf<TAB>double<TAB>目视 双栏：正文页 200dpi 渲染确认<TAB>拼版图+正文页目视<TAB>你的名字<TAB>2026-10-17<TAB>conf<TAB><TAB>612x792<TAB>53.5/84.7/52.3/82.9<TAB>506.2
```

> **台账必须用 Python 写**（`open(...,'w',encoding='utf-8')`，
> **UTF-8 无 BOM、LF**）。用 PowerShell 的 here-string / `Set-Content`
> 写这份 TSV，会得到**整份 GBK 乱码**，
> 并且可能悄悄丢掉一个制表符使整列错位。**实测被这个坑了两次。**


**机器判定只作候选**——基准实测三种自动判据**全部有假阳/假阴**：行宽中位数漏判图表密集的单栏篇；中缝空白带被跨栏图污染；纯文本跨中缝法甚至把**全部**双栏论文误判为单栏。**只有目视可靠。**

### 3. 测量几何（照原件）
取正文页（文本块 ≥8）的文本/图块并集，3%/97% 分位去离群后取中位数 → `paper`/`margins`/`text_width`。
**单位必须是 `bp`（PostScript point）= `pt`×1.00375**。详见 `references/layout-rules.md` 的几何段。

### 4. 模板选择
| 原件版式 | 模板 |
|---|---|
| 三栏 | `\documentclass[10pt]{article}` + `geometry` + `\usepackage{multicol}`，正文包 `\begin{multicols}{3}`（`\columnsep` 按实测栏距设，单位 `bp`） |
| 双栏 | `\documentclass[10pt,twocolumn]{article}` + `geometry`（**不要用 IEEEtran 硬掰纸张**） |
| 单栏 | `\documentclass[10pt]{article}` + `geometry` |

纸张/边距**逐值**写进 geometry，单位 `bp`。

> **三栏为什么不用 `article[twocolumn]`**：`twocolumn` 类选项只有两栏一档，没有三栏。
> 必须用 `multicol` 的 `multicols` 环境；且 geometry **仍**负责纸张与边距（三栏栏宽由
> multicol 自行等分 `\textwidth`，不要手写栏宽）。

#### 4.1 **不支持的版式必须失败（禁止静默降级）**

本 skill 只能表达 **1–3 栏**（`access` / `conf` / `tri`）。

- 台账或自动检测判定原件**超出 1–3 栏**（例如 4 栏），或栏数与所选 `--mode` 不符时，
  `runpaper.py` **打印 `REFUSING TO BUILD` 并 `exit 2`，不产出 PDF**。
- **不得**把不支持的版式悄悄降级成单栏再报"成功"——那是**假成功**，
  交付件的版式与原件不同却无人知晓。
- 确需降级时**必须显式** `--allow-layout-downgrade`，并在台账「备注」列写 `DEVIATION`。

```powershell
# 三栏（CACM opinion 等）
python scripts/runpaper.py S1 --mode tri --outdir out
# 不支持时会被拒绝，并给出原因与可选做法
python scripts/runpaper.py X9 --mode access    # -> REFUSING TO BUILD ... exit 2
```

### 5. 内容准备（md 真相源 → LaTeX）
建 `main.tex`：标题（中文题 + 英文原题）、作者/单位/DOI/收稿日期、摘要 `abstract` 环境、关键词、正文。
- 章节标题用 `\section*{}`（译文自带编号，防止 LaTeX 自动编号叠加）
- 图用 `\includegraphics`，表用 `\includegraphics{tab_X.pdf}` + 中文 `\caption`（`labelformat=empty` 防重复编号）

### 6. 图提取
按**图题锚定 + 图形簇区域**取图：单张完整内嵌位图 → 直接无损取原 PNG；多子图拼贴/矢量图 → 定界区域渲染。
**不要**用「图题搜索词 → 整页裁剪」这类 v1 思路（会把正文段落、Algorithm 当作插图）。详见 `references/figures.md`。

### 7. 表矢量复刻
`page.show_pdf_page()` 把原件表区抽成单页矢量 PDF → `\includegraphics[width=\textwidth]{tab_X.pdf}`。
表题由 LaTeX 渲染中文，**导出区域不含英文表题**。详见 `references/tables.md`（含三态判定与区域定位算法）。

### 8. 编译
编译一次即可（后端自行重跑到收敛）。日志必须落盘并**53ef读**（local-tectonic 为 `.work/<NN>/main.log`）。

### 9. 门控 + 目视抽验
跑门控清单（见下），**任何一项不过 → 回到步骤 5–8 修**，不得放行。
门控过 ≠ 正确：**必须随机抽验若干页目视**（图表对不对、有没有译者痕迹、表区是不是那张表）。

### 10. 归档
逐篇 commit（**显式列文件名，不要 `git add` 目录**）。记录台账与实际页尺寸。

## 门控清单（可执行）

| # | 项 | 判据 | 不过怎么办 |
|---|---|---|---|
| G1 | LaTeX 错误 | 编译日志（默认 local-tectonic 后端 => `.work/<NN>/main.log`）中 `^!` 计数 = **0** | 读 `!` 后第一行定位（见 gates.md） |
| G2 | 缺字形 | `Missing character` 计数 = **0** | 补 `newunicodechar` 回退映射 |
| G3 | 越栏 | `Overfull \hbox` **最大** >8pt 即失败（`\vbox` >120pt 才失败） | 见 gates.md「越栏三步定位」 |
| G4 | 图表编号闭合 | 期望图号/表号**无缺失**（子集判定） | 缺号 → 查提取器正则与区域 |
| G5 | 译者痕迹 | 交付 PDF 文本层中译注/翻译说明/自查 = **0** | 回到 strip 步骤 |
| G6 | 页面几何 | 实际页面尺寸 ≈ 台账纸张（±3pt 舍入） | 查单位是不是用了 `pt` |
| G7 | 字体嵌入 | 所有字体 embedded = yes | 换字体或改编译链 |
| G8 | 门控日志 | 日志**读不到 → 判定失败**（`gate-log-unreadable`），**不得默认 0** | 修抓取链路后重跑 |
| G9 | 构建日期 | 交付件**不得含编译当天日期**（环境泄漏） | 模板写 `\date{}`；出版日期进信息行 |
| G10 | 矢量表未使用 | `tables.json` 有 `ok` 记录却**未走矢量嵌入** → FAIL | 修表号归一化/表体跳过；确属不可复刻才 `--allow-handmade-tables` |

> G3 的 Overfull **必须全量取 max**（`head -20` 会漏掉最大值）。G8 是血的教训：日志抓取失败被静默当成 0，等于门控失效。
>
> **G9 只查「编译当天」这一天**，不查「任何日期」——论文本身就有日期（收稿年、`October 2021`），全查会误判几乎每篇；当天日期只可能来自编译机时钟。
>
> **G10 的关键是分类，不是「有手写表就失败」**：表号在 `tables.json` 里有 `ok` 记录却没走矢量嵌入 = **缺陷**（矢量件存在却没用上）；表号**根本没有**记录 = **允许手写**（原件不可复刻，如 DESIRE6G 的 3 张 `no-rules`），只打印 INFO。另有独立复核（表格图计数）兜底，防止 prep 输出格式变化让门控失效。

## 验证循环（失败怎么定位）

```
编译失败 → 读编译日志（local-tectonic: `.work/<NN>/main.log`）
  ① grep '^!' .work/<NN>/main.log   → 取错误行号，看该行源码
     · "Missing $ inserted"     → 数学符号未进数学模式（希腊字母必须 \ensuremath 包）
     · "Misplaced alignment tab"→ 表题/文本里的 & 未转义
     · "Extra alignment tab"    → 表格列数与列格式不符
     · "Undefined control sequence" → 宏未定义或多反斜杠被吞
  ② grep 'Overfull \\hbox' .work/<NN>/main.log → 取**最大**值（不是前 20 条）
     · 越栏是"列宽"还是"断行"？若某列宽 < 该列最长单词宽 → 加断点无解，改列宽/列数/字号/全宽
  ③ grep 'Missing character' .work/<NN>/main.log
  ④ 仍不过 → 目视渲染该页，别继续猜
```

**回归纪律**：改脚本后跑回归用例集（至少覆盖「侧栏图题」「矢量多子图」「无框线表」「长标识符表」四类）。**当改动收益是纯观感、而失败模式是回归时，回到已验证状态**。

## Gotchas（22 条，完整版见 `references/gotchas.md`）

1. **`geometry` 的 `pt` 是 TeX point（1/72.27in），不是 PostScript point** → 全库统一缩小 0.374%（612→609.71）。**必须用 `bp`**。
2. **IEEEtran 只认 `letterpaper`/`a4paper`**：`\geometry{paperwidth=}` 与 `\pdfpagewidth` **都被忽略**。要自定义纸张就换 `article[twocolumn]`。
3. **`\documentclass[conferenceletterpaper]` 少个逗号** → 选项不被识别，首页回落 letter。类选项必须逗号分隔。
4. **表题正则写死 `Table:\s*`（要求冒号）** → 全部期刊论文提取不到表（`Table 1`/`TABLE III`）。全库提取率只有 20%。**凡「某功能全库无效」，先怀疑最窄的那个正则。**
5. **`cells[2:]` 假设第 2 行是分隔行** → 无分隔行的表**丢掉首行数据**（缩略语表丢 CID 行）。
6. **ASCII 线框图（字段布局图）不是表** → 转 tabularx 必然错位。应保持代码围栏/verbatim。
7. **门控日志抓不到时不得默认 0** → 必须判失败（`gate-log-unreadable`）；用哨兵（`echo __SSHOK__`）区分「无匹配」与「链路失败」。
8. **`pdftotext -layout` 会被水印/双栏打乱**（把图号拆到两行 → 假「图号缺失」）→ **用本地 PyMuPDF 重抽文本层**，比放宽正则本质。
9. **PowerShell 写 UTF-8 会带 BOM** → LaTeX 报错；**脚本内避免中文**（PS 5.1 按 ANSI 读会吞引号）。脚本一律 ASCII。
10. **`article[twocolumn]` 做不出三栏** → 三栏必须用 `multicol` 的 `multicols`；`\columnsep` 也要设 multicol 的那个。
11. **`\captionsetup[figure]{position=top}` 不生效** → 只影响 caption 框内对齐，**不移动图题位置**。图题在上方必须把 `\caption` 写在 `\includegraphics` **之前**（实测 A/B 都不动，C 才对）。
12. **台账边距列序是 `L/T/R/B`**（代码为准）→ 照早期文档写成 `T/R/B/L` 会把边距整体旋转，而页尺寸与门控仍然正确，**极难发现**。
13. **不支持的版式要拒绝出稿，不许静默降级** → 3 栏原件降级单栏再报"成功"是**假成功**；`runpaper.py` 判 `REFUSING TO BUILD` + exit 2。
14. **`**图 N.** 说明`（粗体只包图号）曾不被识别** → 静默不插图；`prep.py::RE_FIG_D` 已覆盖，改图题正则后必须跑 `_figm` 回归。
15. **`--native-only` 过去并没有真的跳过渲染**（无 poppler 时仍调 `render_region` → `TypeError: not NoneType`）→ 现在 `render_ok` 单点控制，且缺 pdftoppm 时回退 PyMuPDF 渲染。
16. **窄栏里的长邮箱/长标识符会越栏**（如 3 栏里的 author bio 越栏 95pt）→ `prep.py::breaklong` 在这些 token 内插入 `\hspace{0pt}` 零宽断点。
17. **`status == ok` ≠ 区域正确**：导出正常的可能是**正文段落**或**另一张**表（生产实测 5 例）。只数张数永远发现不了 → **必须目视抽验**。
18. **不要写「表号后跟某词就否决」的守卫**：真表题也叫 "Summary of the fitted …" → 会连真表题一起杀掉。
19. **`\maketitle` 不给 `\date` 就印 `\today`** → 交付件被打上**编译当天日期**（环境泄漏，非论文信息）。模板必须写 `\date{}`；这是 G9。
20. **`\prime` 不能在文本模式直接用**（`Missing $ inserted`）→ 必须 `\ensuremath{\prime}`。写成 `\ensuremath{^\prime}` 会多抬一层、与 `12′` 这类上下文不一致。
21. **字体「族名 ≠ 文件名」会让探测误报「未找到」** → `Consolas` 实际是 `consola.ttf`，枚举得到的名字里没有 `consolas`。用**白名单别名表**（`FONT_FILE_ALIASES`）修，**不要**用反向前缀匹配（`Consolaxx` 会误配 `consola`）。
22. **手写表不都是错的**：判据要分「`tables.json` 有 `ok` 记录却没用上」（= 缺陷）与「`tables.json` 里没有该表号」（= 允许，原件不可复刻）。一律判失败会误伤后者。这是 G10。

## 不做的事

- 不手写 LaTeX 表（要复刻原件矢量）。
- 不把某一版式全局套用。
- 不在没有目视确认的情况下声称「版式已对齐」。
- **不把不支持的版式静默降级**——必须 `REFUSING TO BUILD` + exit 2，或显式声明降级。
- 不解释什么是 PDF、什么是双栏——直接执行。
- 不修改 `Paper/**` 与 `.gitignore`。

## references/

| 文件 | 何时读 |
|---|---|
| `references/workflow.md` | 端到端流程与逐阶段检查清单 |
| `references/layout-rules.md` | 版式判定、几何测量、模板选择（R1/R2/R3 + bp/geometry） |
| `references/tables.md` | 表格三态判定与矢量复刻区域定位算法 |
| `references/figures.md` | 图提取、图题锚定、图形簇算法 |
| `references/gates.md` | 门控口径明细与失败定位手册 |
| `references/gotchas.md` | 踩坑清单完整版（20 条，含根因与修法） |
| `references/environment.md` | **环境为首要**：P0 预检、所需工具/字体、`setup_env.py`、缺依赖如何显式失败 |

> **日志文件名**：默认后端 `local-tectonic` 会自行重跑到收敛，日志落在 `.work/<NN>/main.log`（**不是 b1.log / b2.log**）。只有可选的 `ssh-xelatex` 后端才用 xelatex 两遍、写 `b1.log`/`b2.log`。上面的门控都指向“当前编译日志”。
