# samples/ — 示例论文（可直接用来试跑本 skill）

## `SRplot_2023_PLOS_ONE_CC-BY.pdf`

| 项 | 值 |
|---|---|
| 标题 | SRplot: A free online platform for data visualization and graphing |
| 作者 | Doudou Tang, Mingjie Chen, Xinhua Huang, Guicheng Zhang, Lin Zeng, Guangsen Zhang, Shangjie Wu, Yewei Wang |
| 期刊 | PLOS ONE, 2023 |
| DOI | [10.1371/journal.pone.0294236](https://doi.org/10.1371/journal.pone.0294236) |
| 许可 | **CC BY 4.0**（Creative Commons Attribution License） |
| 页数 | 8 |
| 版式 | 单栏，612×792 pt（US Letter） |
| 图表 | 1 张表（Table 1）、3 张图（Fig 1–3，其中 Fig 3 为多面板） |

### 为什么选它
1. **许可清晰、可再分发**：PLOS ONE 全部采用 CC BY 4.0，PDF 内即印有
   「Creative Commons Attribution License」字样，可随本仓库一起分发
   （保留署名即可）。
2. **是真正的「没见过的论文」**：本 skill 的开发语料是 IEEE / ACM / Elsevier /
   Springer / OMA 规范共 28 篇；PLOS 的模板（单栏、宽边距、`RESEARCH ARTICLE`
   页眉、图表放在正文流中）与它们都不同，因此这是有效的 out-of-sample 检验。
3. **规模合适**：8 页、约 1.97 万字符，够小到能在一次会话内完整翻译，
   又足以覆盖摘要/章节/表格/多面板图/参考文献这些真实结构。

### 怎么用它试跑
```powershell
# 1) 把示例放进工作区
New-Item -ItemType Directory -Path Paper -Force | Out-Null
Copy-Item samples\SRplot_2023_PLOS_ONE_CC-BY.pdf Paper\

# 2) 然后按 README「从零到出稿」七步执行；
#    第 1 步需要你自己（或你的翻译流程）产出 Paper\29_SRplot_..._中文.md
```

> ⚠️ 注意：本 skill 的输入契约是「原件 PDF + **中文译稿 md**」。
> 本目录只提供原件，**译稿需要你自己产出**（这也是 skill 明确划归
> 「用户/翻译流程」的那一步）。

### 署名（CC BY 4.0 要求）
> Tang D, Chen M, Huang X, Zhang G, Zeng L, Zhang G, et al. (2023)
> SRplot: A free online platform for data visualization and graphing.
> PLOS ONE 18(11): e0294236. https://doi.org/10.1371/journal.pone.0294236
>
> Licensed under CC BY 4.0. This file is redistributed unmodified.
