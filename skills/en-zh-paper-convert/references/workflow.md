# 端到端流程与检查清单（workflow）

> 主流程简版在 `SKILL.md`；本文件是**逐阶段可执行版**，含每阶段的输入/产出/检查点/失败回退。

## 阶段总览

```
P0 环境预检
P1 版式审计 → 台账          ← 硬前置门控（不通过不得进入 P2）
P2 几何测量 → geometry 表
P3 模板生成 → main.tex 骨架
P4 内容准备（strip + 组装）
P5 图提取
P6 表矢量复刻
P7 编译（两遍）
P8 门控
P9 目视抽验 + 几何核对
P10 归档 commit
```

---

## P0 环境预检

**做什么**
```
python -c "import fitz; print(fitz.__doc__)"        # PyMuPDF
where xelatex ; where tectonic                      # 编译引擎
```
- 编译引擎优先 **tectonic**（单文件、自带包管理、无需 TeX Live）。
- CJK 字体：`Noto Serif SC` / `Noto Sans SC`（Windows 自带），**回退 SimSun**。
- 源 PDF 若无文本层（完全扫描件）→ **先 OCR**，否则后续所有"文本锚定"流程都不成立。

**检查点**
- [ ] `import fitz` 成功
- [ ] 编译器可用
- [ ] 字体存在（`fc-list | findstr Noto` 或目录检查）
- [ ] 源 PDF 有文本层（`page.get_text()` 非空）

---

## P1 版式审计 → 台账（**硬前置门控**）

**做什么**
```
① 机器初判（仅候选）
     python scripts/classify_papers.py Paper/ --json .work/classify.json
② 目视确认
     渲染每篇正文页 + 图表密集页 → 拼版联系表 → 逐篇看栏数
     边缘例（中缝比 0.2–0.6 / 块分布不对称）→ 逐页 110dpi 高清看
③ 登记台账 format-ledger.tsv
④ 出稿时省略模式→取台账；冲突→拒绝（exit 2）；无记录→拒绝
```

**产出**：`format-ledger.tsv`（含 栏数 + 纸张 + 页边距 + 依据 + 审计人 + 时间）

**检查点**
- [ ] 每篇都有**目视结论**（不是机器结论）
- [ ] 每篇都有**证据描述**（哪几页、看到了什么）
- [ ] 每篇都有**审计人与时间**
- [ ] **没有**任何一篇是"因为别的篇目是 X 所以这篇也是 X"

**失败回退**：机器判 `ambiguous` 或边缘例 → **必须**高清逐页看；实在无法判定 → **问用户**，不要赌。

---

## P2 几何测量 → geometry 表

**做什么**
```
① 只取正文页（文本块 ≥ 8）
② 文本块 ∪ 图块 并集 bbox
③ 3% / 97% 分位去离群
④ 取中位数 → paper / margins(T,R,B,L) / text_width / text_height
⑤ 落 geometry-per-paper.json
```

**检查点**
- [ ] 单位明确标注为 **PostScript point（bp 写入）**
- [ ] 与原件页面尺寸对比**逐篇**过一遍（不是只看几篇）
- [ ] 缺数据的篇目**在回报中标注**（不要静默用默认值）

**失败回退**：若 `text_width` 明显异常（远大于纸宽）→ 测量被水印/出血污染，换页重测。

---

## P3 模板生成

**做什么**
```latex
% 双栏
\documentclass[10pt,twocolumn]{article}
\usepackage[paperwidth=…bp,paperheight=…bp,left=…bp,right=…bp,top=…bp,bottom=…bp]{geometry}
% 单栏
\documentclass[10pt]{article}
\usepackage[paperwidth=…bp,…]{geometry}
```
+ IEEEtran 兼容 shim（若原作使用）
+ `\usepackage{ragged2e,etoolbox}` + `\AtBeginEnvironment{table}{...}`
+ `fvextra`（verbatim 断行）
+ 缺字形 `newunicodechar` 回退表（`runpaper.py::GLYPH_FALLBACKS`，**90 条**；
  缺字形**补进这张表**，不要一篇篇手加。`\prime` 必须 `\ensuremath{}` 包）
+ `\date{}`（G9：不给就印 `\today` = 交付件被打上编译当天日期）

**检查点**
- [ ] **单位是 `bp`**（不是 `pt`）
- [ ] 类选项**逗号分隔**（`[conference,letterpaper]` 不是 `[conferenceletterpaper]`）
- [ ] 作者块用 `\parbox[t]{\columnwidth}`（**不是** `\pageTW`）
- [ ] 章节用 `\section*{}`（译文自带编号，防自动编号叠加）
- [ ] 摘要用独立 `abstract` 环境
- [ ] 有 `\date{}`（G9）
- [ ] 字体探测**无 WARN**（族名≠文件名如 `Consolas`/`consola.ttf` 要被认出来）

---

## P4 内容准备

**做什么**
```
① 剥离译者信息 → clean.md（保留审计用译稿不动）
     · 题头元信息表 / 翻译说明整节 / 篇末自查整块
     · 行内〔译注：…〕/ 悬空引用 / 进度标记 / 分隔线
② 拆分：标题（中+英）→ 作者/单位/DOI/收稿日期 → 摘要 → 关键词 → 正文
③ 正文转 LaTeX（prep）
     · 表格列数 = 全表最大列数 + 全行补齐
     · 特殊字符转义（& % $ # _ { } ^ ~）
     · 数学符号 / Unicode 数学字母块 → LaTeX 命令
     · 代码围栏内只做纯字符映射（verbatim 不能用命令）
```

**检查点**
- [ ] 交付 PDF 将包含的文本中，译者痕迹 = 0
- [ ] 标题页只保留**论文自身**元信息
- [ ] 关键词行匹配用 `^[ \t>*]*`（**不要** `\s*`——会跨行导致关键词为空）
- [ ] 元信息字段名用**前缀匹配**（`联系`/`通讯作者`、`日期`/`收稿…`/`出版日期`、`DOI`/`DOI / 数字对象标识`）
- [ ] 章节编号体例两套（阿拉伯 `1.` / 罗马 `I.`）都要能拆

**失败回退**：出现"少字漏字" → 优先查**硬编码切片**（`cells[2:]` 类）与**正则**。

---

## P5 图提取

**做什么**：见 `figures.md` 全文。

**检查点**
- [ ] 单张完整位图走**原生提取**（不是渲染）
- [ ] 图题体例全覆盖（`Fig. N.`/`FIGURE N`/`图 N |`/全角冒号/整句加粗/附录字母编号）
- [ ] 图号归一化（`4.-1` → `4-1`）
- [ ] 配对用**代价匹配**（侧栏图题）
- [ ] 装饰位图剔除**带尺寸守卫**
- [ ] 无整页渲染图（尺寸接近页面尺寸 → 高度可疑）
- [ ] 图幅 md5 去重 + 纯色拒绝 + aspect ≤12

**验收**：**渲染联系表逐图目视** + 逐图核对「图内容 ↔ 图题」。

---

## P6 表矢量复刻

**做什么**：见 `tables.md` 全文。

**检查点**
- [ ] 三态判定正确（**区分「文件不存在」与「空 `{}`」**）
- [ ] 表题正则**不要求冒号**（覆盖 `Table 1`/`TABLE III`）
- [ ] 提取结果 `MISSING == []`
- [ ] 无 `w < 60 or h < 25` 的 TINY 区域
- [ ] 导出区域**不含英文表题**
- [ ] 表内保留英文，表题中文 + `\captionsetup{labelformat=empty}`
- [ ] 矢量表**未被任何排版规则改动**（R-T1 的提前 return 还在原位）
- [ ] 命中表体在 md 中**全部交错块**都被跳过（无孤儿 `tabularx`）

**验收**：**渲染全部表为联系表目视复核**——`status == ok` 不可信。

---

## P7 编译（两遍）

```
python scripts/runpaper.py <NN> --outdir out     # 内部编译，日志落在 .work/<NN>/main.log
# 第二遍仅在使用 ssh-xelatex 后端时需要；local-tectonic 自行重跑到收敛
```

**检查点**
- [ ] 两遍都跑完（第二遍解交叉引用）
- [ ] 编译日志落盘且**可读**（local-tectonic：`.work/<NN>/main.log`）—— G8 依赖它

---

## P8 门控

跑 `gates.md` 的 G1–G10。

**检查点**
- [ ] G1 ERRS = 0
- [ ] G2 MISS = 0
- [ ] G3 OVF_H_max ≤ 8pt（**全量取 max**）、OVF_V_max ≤ 120pt
- [ ] G4 图号闭合 + 表号闭合（子集判定）
- [ ] G5 译者痕迹 = 0
- [ ] G6 页面尺寸 ≈ 台账（±3pt）
- [ ] G7 字体全嵌入
- [ ] G8 日志可读（读不到 = FAIL）
- [ ] G9 交付件**不含编译当天日期**（`\date{}`；只查当天，不查「任何日期」）
- [ ] G10 `tables.json` 有 `ok` 记录的表**全部走了矢量嵌入**
      （无 `handmade-despite-ok`；无记录的手排表只算 INFO）

**失败回退**：读编译日志的 `!` 与 `Overfull` 行（local-tectonic 为 `.work/<NN>/main.log`），按 `gates.md` §7 定位。

---

## P9 目视抽验 + 几何核对

**做什么**
```
① 全库几何核对：交付 PDF 实际页面尺寸 vs 台账 → audit_vs_ledger
     （把"28 篇都要重做"收缩为"只剩 5 篇"——用客观量做全库核对）
② 抽验目视：随机页 + 全部图表联系表 + 与原件并排对读
③ 版式抽验：栏数、栏间距、页边距观感
```

**检查点**
- [ ] 几何核对**全库**跑，不是抽样
- [ ] 图表联系表**逐张**看过
- [ ] 版式与原件并排对读通过
- [ ] 交付 PDF **无**任何译者信息（含"看似无害"的说明段）

---

## P10 归档

**做什么**
```
git add <显式文件列表>     # 不要 git add 目录
git commit -m "<NN> <描述>"
```

**检查点**
- [ ] 每篇**单独** commit（便于回退单篇）
- [ ] commit 只含**已验证**的文件
- [ ] 台账与 manifest 同步更新
- [ ] 记录 SHA256 与复现命令（供独立验证）

---

## 检查清单汇总（打印/粘贴用）

### 出稿前
- [ ] 版式台账有该篇记录，且**目视审计**完成
- [ ] 纸张/页边距已测，单位换算为 `bp`
- [ ] 模式与台账一致（冲突即拒绝）

### 交付前
- [ ] ERRS = 0
- [ ] MISS = 0
- [ ] OVF_H_max ≤ 8pt（全量 max）
- [ ] 图号闭合 ✓ 表号闭合 ✓
- [ ] 译者痕迹 = 0
- [ ] 页面尺寸 vs 台账 ±3pt
- [ ] 字体全嵌入
- [ ] 门控日志可读（否则 FAIL）
- [ ] 交付件**无编译当天日期**（G9；`\date{}`）
- [ ] `tables.json` 有 `ok` 记录的表**全部走矢量嵌入**（G10；无 `handmade-despite-ok`）
- [ ] 图表联系表**逐张目视**通过
- [ ] 栏目数/页边距与原件并排对读通过

### 提交前
- [ ] 显式文件列表（未 `git add` 目录）
- [ ] 只提交已验证文件
- [ ] 台账/manifest 同步

---

## 并行与批次纪律

| 规则 | 理由 |
|---|---|
| **任何时刻只允许一个批次** | 两个批次并发写同一工作目录 → 大量假 `gate-log-unreadable` |
| **一篇一 owner** | 同一篇并发会互相清空工作目录 |
| **按篇号切分写范围** | 天然隔离，避免冲突 |
| **并行前先确认子智能体路由配额** | 限额中断会导致反复重启；失效时 Lead 收回关键路径自己跑更稳 |
| **SSH 合并为单次 tar 管道上传** | 短时间大量 ssh/scp 触发限频封 IP |
| **上传用显式文件列表** | `cmd.exe` 不展开通配符 → 静默失败 |

---

## 环境陷阱速查

| 陷阱 | 表现 | 对策 |
|---|---|---|
| PowerShell 写 UTF-8 带 BOM | LaTeX 报错 | 写文件不带 BOM |
| PS 脚本内含中文 | `missing the terminator` | **脚本一律 ASCII** |
| PS 内联 `$(...)` / 双引号 | 被本地展开或吞掉 | 先写脚本文件再传 |
| Python 源码 1 个反斜杠 | `\a`→BEL、`\f`→FF（**静默**） | 用 2 个反斜杠；注入后校验控制字符 |
| Windows 控制台 GBK | `UnicodeEncodeError` 崩溃 | `sys.stdout.reconfigure(encoding='utf-8')` |
| 目标 PDF 被 Acrobat 占用 | `PermissionError` / 产出 `-new.pdf` | 退避写 `-new.pdf` 并提示用户 |

> **日志文件名**：默认后端 `local-tectonic` 会自行重跑到收敛，日志落在 `.work/<NN>/main.log`（**不是 b1.log / b2.log**）。只有可选的 `ssh-xelatex` 后端才用 xelatex 两遍、写 `b1.log`/`b2.log`。上面的门控都指向“当前编译日志”。
