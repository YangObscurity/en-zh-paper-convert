# 图片提取规则（figures）

## 0. 首要原则

**图必须从原件"取"出来，不是"画"出来，也不是"整页截图"。**

- **无损提取**：原件内嵌位图直接取原生数据（`pdfimages -p -png`），**不做二次编码**。
- **矢量图**：原件是矢量绘制（流程图、曲线图）时，按图形簇 bbox **定界渲染**（600 dpi）。
- **绝对禁止**：把整页渲染成一张图当插图用（v1 的 2088×1712 整页裁剪）。

---

## 1. 反面教材：v1 的「图题搜索词 → 页面区域裁剪」为何整体失效

- **现象**：#20（JKSU/Springer）**24 幅图全部**被裁成 2088×1712 的**整页区域**，
  把英文原文的 **Algorithm 1、正文段落**当成插图嵌入 → 用户看到「同一段英文在多个位置重复出现」。
- **根因**：该刊**图题在图左侧窄栏、与图垂直重叠** → v1 的「横向重叠」判据**全部落空**。
- **后果**：用户判定 28 篇 PDF 全部有问题，全库返工。

> **教训**：裁剪式取图必须验证「**裁出来的东西真是图**」，
> 只验证「有没有裁到东西」是不够的。**真正的校验是目视。**

---

## 2. 一图不一定是一张位图

`pdfimages` 单张位图 + 图题匹配**只适用于**「一图 = 一张完整位图」（#20 侥幸如此）。
真实论文大量存在：

| 形态 | 处理 |
|---|---|
| 单张完整内嵌位图 | **原生提取**（`pdfimages -p -png`）→ 逐像素等价 |
| 多子图拼贴（(a)(b)(c)） | 图形簇并查集 → 区域渲染 |
| 位图 + SMask（透明通道） | 合并处理，不要只取其一 |
| 纯矢量图（流程图/曲线图） | 区域渲染 600 dpi |
| 跨页重复的 logo / 装饰位图 | **剔除**（≥3 页同尺寸同 x） |
| 不可见重复文本层 | 不要参与图形簇 |
| xref 损坏（如 1 页被报成 17 页） | 先 `qpdf` xref 归一化 |

### 2.1 图形簇并查集

节点 = images + rects + lines + curves；并查集参数 `gap_v = 26pt` / `gap_h = 9pt`。

**双重屏障**（防止簇越过表/栏）：
- **表行栈** + **宽横线（≥0.5 页宽）**
- **仅当垂直 AND 水平都相交时才阻断**（只用其一过杀）

**过滤规则**：

| 规则 | 阈值 | 理由 |
|---|---|---|
| 跨页重复装饰位图 | ≥3 页同尺寸同 x | logo / 水印图案 |
| 碎图 | `< 40pt` | 噪声 |
| 小簇面积惩罚 | `< 4000pt²` → cost **+300** | 避免选中图题旁的小符号 |
| 图幅 aspect | 阈值 **12** | 异常长条不是图 |
| 纯色 | `stddev < 5` → 拒绝 | 空白/色块 |
| 重复图 | md5 去重 | 同图被多次嵌入 |

### 2.2 分流

```
单张完整位图（且无相邻位图） → NATIVE：直接取原生 PNG
其余（拼贴/矢量/多片）        → CLUSTER：600 dpi 区域渲染
无簇                          → 图题旁空白带（POSFALLBACK）
仍失败                        → <NN>.figs_override.json 人工给定 bbox
```

> DECOR 装饰剔除要加**尺寸守卫**（仅小尺寸重复位图才判装饰）——
> 否则会把拼贴图的各片当 logo **误删**。

---

## 2.3 人工 bbox override：位置、格式、优先级

override 是**人工判读**，它顶替（或补上）算法没提取到的图。
如果仅放在临时构建目录 `.work/` 里容易在清理或重新构建时丢失。所以：

### 放哪里

**规范位置（唯一推荐）：`Paper/<NN>.figs_override.json`** —— 与原件及译稿同目录（保存在本地 `Paper/` 中）。

查找顺序（**显式优先**；命中几份就打印几份，只有最高优先级那份生效）：

| 优先级 | 位置 | 说明 |
|---|---|---|
| 1 | `--override <PATH>` | 命令行显式指定；**文件不存在即报错 exit 2**，绝不静默回退 |
| 2 | `Paper/<NN>.figs_override.json` | **规范位置（本地 `Paper/` 目录）** |
| 3 | `<workdir>/<NN>.figs_override.json` | `.work/16/16.figs_override.json` |
| 4 | `Paper/<stub>.figs_override.json` | 按原件文件名 |
| 5 | `<workdir>/../<NN>.figs_override.json` | `.work/16.figs_override.json` |
| 6 | `<workdir>/figs_override.json` | **旧位置**（`.work/16/figs_override.json`）—— 兼容但易丢失，应尽快迁移 |
| 7 | `Paper/figs_override.json` | 单篇仓库的兜底 |

`--no-auto-override` 可禁用 2–7，只认 `--override`（用于确认「不依赖任何人工 bbox」的干净复现）。
`--override-priority R1,R2,...` 可重排优先级（取值 `explicit` / `paper-nn` / `paper-stem` /
`parent-nn` / `parent-legacy` / `paper-other`）。

同时存在多份且**命中同一个图号**时，会打印 `NOTE: override 歧义：图号 ... -> 取 ...`，
并说明哪一份不生效。

### 什么格式

```json
{
  "_README": ["以 _ 开头的键是文件元信息，不作为图号解析"],
  "4": {"page": 12, "box": [x0, top, x1, bottom], "dpi": 600, "caption_pos": "bottom"},
  "1": {"page": 2,  "box": [49, 50, 563, 213], "dpi": 600,
        "_why": "Fig.1 是跨栏矢量图，图形簇没连起来，自动提取报 missing"}
}
```

- 键 = 图号（与 `--expect` **同一口径**，如 `4-1`、`C.3.1.1-1`）
- `page` = 1 起的页号；`box` = `[x0, top, x1, bottom]`，单位 **pt**（不是像素）
- `dpi` 可省（默认用 `--dpi`）；`caption_pos` = `top` / `bottom`（决定 `\caption` 位置）
- 以 `_` 开头的键是**元信息**，用来写清「这个框从哪来、谁复核过」——**强烈建议写**，
  否则下一个人无法判断它是否还成立
- 文件带 UTF-8 BOM 也能解析（Notepad / PowerShell 存盘会加）

### 自校验（不合规即**拒绝该条**并报错，绝不静默出空白图）

| 检查 | 触发条件 | 行为 |
|---|---|---|
| 页号越界 | `page` 不在 `[1, 页数]` | 报错拒绝 |
| 框超出页面 | box 超出页面矩形 | 报错拒绝 |
| 框过小 | < 12×12pt | 报错拒绝 |
| **空白图** | 渲染后 stddev < 3.0 或墨迹占比 < 0.002 | 报错拒绝 + **删掉产物** |
| 坐标口径不符 | box 超出页面但整体可等比缩放回页面内 | 自动缩放 + 打印 `rescaled-suspect` |
| 坐标含算式 | `"x0": "left*2"` 之类 | 白名单字符集内求值，否则拒绝 |

拒绝的条目会以 `OVERRIDE-REJECTED: fig<N> (<file>) <reason>` 输出，并且**该图不会出现在结果里**
（走 `missing-figs`），因此「用了 override 却静默没生效」不可能发生。

### 两条硬规矩

1. **只增不减**：override 只应补上算法没提出的图。若它**顶替了算法已成功提取**的图号，
   默认**报错拒绝**（`override-replaced-an-auto-extracted-figure`）——人工框不该顶掉一个
   已验证的裁剪，这通常意味着图号口径写错（把 `4-1` 写成 `4`）。
   确实要顶替时显式加 `--allow-override-replace`。
2. **必须报告**：门控会打印 `auto` / `override` 计数，用了 override 的 PASS 会**显式标注**。
   见 §2.4。

### 怎么生成

```bash
# 1) 先看原件里有哪些图题、以及在不在文本层
python scripts/extract_figs.py Paper/x.pdf out --list-captions --expect 1,2,3

# 2) 看自动提取的结果与缺图
python scripts/extract_figs.py Paper/x.pdf out --expect 1,2,3

# 3) 缺图时：渲染该页目视，量出 bbox，写 Paper/<NN>.figs_override.json
#    （可以从 extract_figs.py 自己报的 cluster box 起手，再放宽一点）

# 4) 重跑并**核对打印出的 path 与 auto/override 计数**
python scripts/extract_figs.py Paper/x.pdf out --expect 1,2,3 --nn 16
```

---

## 2.4 门控必须报告 override：`auto` / `override` / `missing`

**`FIGS=5/5` 本身是不可审计的** —— 它读起来一模一样，无论 5 张全是算法提的，
还是有 1 张是人手画的框。所以门控输出必须区分三态：

| 状态 | 含义 | 判定 |
|---|---|---|
| `auto` | 全部自动提取成功 | 无 override 参与，且期望图号齐 |
| `override` | 有 N 张来自人工 bbox | `figs.json` 里 `override:true` 的条数 |
| `missing` | 无 override 且缺图 | 期望图号未闭合 -> **FAIL** |

`extract_figs.py` 的输出：

```
OVERRIDE USED=1 BY=auto PATH=Paper/16.figs_override.json PRIORITY=2 LOADED=1 ENTRIES=1 REJECTED=0 FIGS=1
FIGS TOPO=override:1 vs_auto=4/5 verified=5/5
```

`runpaper.py` 的门控行：

```
[6] 提取图 5，缺 无  [FIGS=5/5 (auto:4 override:1) errs=0]
[9] GATE ERRS=0 MISS=0 OVF_H_MAX=1.0 VBOX_MAX=0.0 PAGES=6 TRACES=0 FIGS=5/5 (auto:4 override:1) TABLES=3 FIGS-TOPO=override:1
[10] GATE-PASS  [FIGS-TOPO=override:1] -> 16_...pdf
```

> **读到 `override:N` 的 PASS，含义是「图号闭合且门控指标达标」，
> 不是「图已正确提取」。** 复核者**必须打开 override 文件逐框核对**，
> 不能因为门控绿了就放行。`[10]` 行会再把这句话打印一遍。

三态计数是**从落盘产物独立复算**的（`figs.json` + `fig*.png`），不是照抄
extractor 的自述；两者不一致会报 `override-count-mismatch`。

### 证明 override 真的在起作用（控制变量）

「文件在、且 PASS」**不等于**「是它让 PASS 成立的」。`--prove-override`
会在一次运行里做对照：把 override 临时移开（不改动仓库，`finally` 还原）再抽一次图：

```
[6p] WITHOUT override: rc=1 override_used=0 missing=['1']
[6p] FIGS WITH=5/5 WITHOUT=4/5 => override 是**承重**的
```

`WITHOUT` 仍是 5/5 ⇒ 该 override **不承重**，可以删除（少一份人工判读就少一份不复现风险）。

---

## 3. 图题识别与图号配对

### 3.1 图题体例（必须全部支持，逐个都是实战踩出来的）

| 体例 | 示例 | 出处 |
|---|---|---|
| 标准 IEEE | `Fig. 1.` / `Fig.1.`（**无空格**） | 多数 |
| 全大写 | `FIGURE 1` | MDPI 等 |
| 中文体例（译文侧） | `图 1 \|` / `图 1：`（全角冒号） | 译稿 |
| 整句加粗 | `**图 N. 说明**`（粗体包住整条） | 译稿变体 |
| **标签加粗** | `**图 N.** 说明`（粗体只包图号，说明为普通文本） | 译稿常见写法 |
| 缩写 | `Fig. N`（无点） | 少数 |
| 附录字母编号 | `图 C.3.1.1-1.` | OMA 附录 |
| 粘连文本 | `Figure:4.-1TheProtocolStack`（PDF 抽取无空格） | OMA |

### 3.2 图号归一化

**贪婪捕获「数字+点+连字符」再归一化**：
- `Figure:4.-1TheProtocolStack` → `4-1`
- `5.3.2.1.1.-1` → `5.3.2.1.1-1`
- STRICT 遍用 lookahead 排除 `.` 分隔符回溯截断
- 宽松遍**去掉词边界**以兼容 OMA 粘连文本

⚠️ **编号首位可能是字母**：OMA 附录编号以字母开头（`C.3.1.1-1`）。
图号正则必须放宽为 `[0-9A-Za-z]` 开头 —— 曾因「要求首位数字」导致**附录 5 图从未进入期望图号**。

### 3.3 字符级分栏重建

原 PDF 可能**没有空格字符**，左右栏同高会被并成一行 → 图题行必须**按字符级 x 坐标分栏重建**。

### 3.4 图号配对：代价匹配

```python
cost = 垂直距离 + 0.5 * 水平间隙
```

**为什么不能用「横向重叠」**：该刊图题在图左侧窄栏、与图垂直重叠 → 横向重叠判据全部落空。
代价匹配对**侧栏图题**是必需的。

### 3.5 排除项

- **TOC 点导引行**跳过（`......  12`）
- `len > 5` 才判图题（`FIGURE 1` 只有 8 字符，阈值不能太高）
- 正文里的 `See Fig. 3` 交叉引用不是图题

### 3.6 同页多图：去重必须在**区域级**，不能在**簇级**

> **这是「同一页有多张图时必然丢图」的根因。** 实测 #12 丢图 1/4、#20 一次丢 **13/24**。

**旧实现（错误）**：`used_c` 保证「一个图形簇只服务一张图」。

```python
used_c = set()
for cost, ci, fno, side, ln in pairs:      # pairs 按 cost 升序
    if ci in used_c or fno in mapping:     # <-- 一簇一图
        continue
    used_c.add(ci)
```

一页有上下两张图时，两张图的图题都可能把**同一个大簇**算作最近邻：
cost 低的那张抢占成功，另一张**永远被判 continue**，从此提取不到。

**正确口径**：去重判据是**最终裁剪框是否真的重叠**（`bbox_overlap`），
不是「是否用过同一个簇」。一个簇可以合法地服务多张图——每张图各拿**自己那一条带**。

```python
if any(bbox_overlap(box, e) for e in emitted):   # 同页同像素才算重复
    continue
```

三处必须**同时**修好，缺一个就仍然丢图或切碎图：

| # | 根因 | 修法 |
|---|---|---|
| 1 | 簇级去重（一簇一图） | 改为**区域级**去重（`bbox_overlap`，重叠 >35% 才算重复） |
| 2 | 上下堆叠的图被并成一个簇 | `split_cluster_by_caps()` 按**元素行间空白**切带（**不是**图题中点！） |
| 3 | 图题几何判据过严 | 短图题在缩进图左侧（水平框不重叠）；左栏图题 vs 右栏图（间隙 ~180pt） |

**第 2 条尤其容易做错**：按「相邻图题的 y 中点」切，会**从图中间切开**
（#20 p18 三张堆叠柱状图 → 第一张只剩 77pt 高，柱子和 x 轴标签全没了）。
必须按**图形元素行之间的空白**切：切点落在两张图之间的真实空隙里。

```text
元素行合并：  [257..391] [405..538] [552..700]
行间空白：           391..405    538..552     <-- 切这里
错（图题中点）：y=334, y=482                  <-- 把两张图各切一半
```

**第 3 条**：图题判据不能只看「水平框重叠」。
- 短图题 `FIGURE 1`（x1=85）在缩进图（x0=106）**左侧** → 框不重叠但间隙只有 20pt
- 左栏图题（x0=51）配右栏图（x0=232）→ 间隙 181pt，但那正是**该图的题注**

判据：水平间隙按**整条图形带**（同侧 90pt 内的全部元素）而非单个碎片来量；
另接受「列首对齐」（`|ln.x0 - c.x0| < 24`）作为兜底。

**必须避免的两种反向错误**：

| 错误 | 表现 | 检测 |
|---|---|---|
| **过度合并** | 上下两图合成一张（张数够但内容错） | 同页各框**纵向不得重叠** |
| **重复提取** | 同一图被提两次（张数虚高） | 同页各框**重叠率 ≤35%** |

开发评测已对 #12 / #20 的真实数据固化这四条断言
（caption 数 == 提取数、无缺失、同页框不重叠、无碎片），
并在**修复前**的代码上验证会 FAIL（#12 6/8、#20 11/24）。

---


## 4. 译稿侧图题处理

- 图题**必须译成中文**。
- 译稿图题缺英文原文时，`figextract` 退化到「任一侧有文字的首个 `Fig. N.`」命中。
- 图题行也要过数学化（`mathify`），否则图题里的符号会缺字形。
- **图幅命名统一**（`figN.png`），`figs.json` 作**单一事实源**；`pdftoppm` 会给输出名附加页号（`figN-PP.png`）→ **必须统一重命名**。

---

## 5. LaTeX 侧

```latex
\begin{figure}[t]
  \centering
  \includegraphics[width=\columnwidth]{fig3.png}   % 双栏用 \columnwidth
  \caption{中文图题}
  \label{fig:3}
\end{figure}
```

| 情况 | 写法 |
|---|---|
| 全宽图（原件跨栏） | `figure*` + `\textwidth` |
| 单栏文档 | `\textwidth` |
| 图号闭合 | 期望图号**无缺失**即通过（**子集判定**——正文交叉引用会产生额外匹配） |

### 5.1 图号闭合的正确口径

**子集判定**：`期望图号集合 ⊆ 交付 PDF 中出现的图号集合`。

- 期望图号来自 `figs.json`（提取阶段产出）。
- **不要**用「交付 PDF 图号数 == 期望数」——正文交叉引用会多出匹配。
- 曾因放宽正则把 20 张误计成 40，正确做法是**收紧为「只接受期望图号集合内的编号」**。

---

### 5.2 图题位置（在图上方还是下方）

**结论：`\captionsetup[figure]{position=top}` 不能把图题移到图上方。**

实测（caption v3.6b / caption3 v2.3b，tectonic）三种写法对比：

| 写法 | 结果 | 实测 y 坐标 |
|---|---|---|
| A 导言区 `\captionsetup[figure]{position=top}` | 图题仍在**下方** | 图 y=86.9–146.9，图题 y=150.1 |
| B 在 `figure` 环境**内** `\captionsetup[figure]{position=top}` | 图题仍在**下方** | 图 y=234.1–294.1，图题 y=297.3 |
| C `\caption` 写在 `\includegraphics` **之前** | 图题在**上方** ✓ | 图 y=397.2–457.2，图题 y=380.9 |

**原因**：`caption3` 的 `position` 选项只决定 caption 框**内部**的排版
（标签与后续行的对齐），并在 caption 框内交换
`\abovecaptionskip` / `\belowcaptionskip`。它**不移动** caption 相对
图形的上下位置 —— 那个位置完全由 float 内
`\caption` 与 `\includegraphics` 的**执行先后顺序**决定。
因此“加一行 `position=top`”是无效的（也不报错，导致静默失败）。

**可行做法**：按原件照排——图题在上时，把 `\caption` 写在
`\includegraphics` 之前：

```latex
% 图题在上方（如 CACM opinion）
\begin{figure}[!htbp]\centering
  \captionsetup{labelformat=empty}\caption{中文图题}
  \includegraphics[width=\columnwidth]{fig3.png}
\end{figure}

% 图题在下方（LaTeX 默认）
\begin{figure}[!htbp]\centering
  \includegraphics[width=\columnwidth]{fig3.png}
  \captionsetup{labelformat=empty}\caption{中文图题}
\end{figure}
```

位置由 `figs.json` 的 **`caption_pos`** 字段决定（`extract_figs.py`
自动判定：图题行在图形簇上方则为 `top`）；
手工时在 `<NN>.figs_override.json` 写 `"caption_pos": "top"`。
`prep.py` 据此决定 `\caption` 与 `\includegraphics` 的先后。

> 若某版本确实做不到图题置顶，必须**明确写出**
> “图题固定在图下方”，不得留模糊。当前实现**可以**置顶。

## 6. 常见失败模式与对策

| 失败 | 根因 | 对策 |
|---|---|---|
| 图被裁成整页 | 用「图题搜索词 + 横向重叠」 | 改**图题锚定 + 图形簇** |
| 图与图题配错 | 侧栏图题 | **代价匹配** `垂直 + 0.5×水平` |
| 拼贴图只取到一片 | 未做簇并集 | 并查集 + **图区并集增长**（实测 63×137 → 146×252） |
| 拼贴图各片被删 | DECOR 误判 | 装饰剔除加**尺寸守卫** |
| 提取器中途崩溃 → 0 图 | 图号键改字符串后遗留 `%d` 格式化 | 全部改 `%s` |
| 无簇 | 图形被表框线割断 | `<NN>.figs_override.json` 人工给定 bbox（见 §2.3） |
| **override 容易丢失** | override 放在临时目录 `.work/` | 迁到 **`Paper/<NN>.figs_override.json`**（见 §2.3） |
| **override 静默没生效** | 路径猜错 / 图号口径不符 | 门控打印 `OVERRIDE USED/PATH`；缺图仍报 `missing-figs`（见 §2.4） |
| **override 出空白图** | bbox 用了别处的坐标 | 载入时自校验 + 渲染后空白检测，拒绝该条（见 §2.3） |
| **PASS 含义不明** | 门控只报 `FIGS=5/5` | 三态 `auto`/`override:N`/`missing`（见 §2.4） |
| 图号缺失（假阳） | **`pdftotext -layout` 被水印打乱** | 用**本地 PyMuPDF** 重抽文本层 |
| 附录图从未被期望 | 图号正则要求首位数字 | 放宽为 `[0-9A-Za-z]` 开头 |
| **同页多图丢图** | 去重在**簇级**（一簇一图） | 去重改**区域级**，见 §3.6 |
| **同页双图切碎** | 按**图题中点**切带 | 按**元素行间空白**切，见 §3.6 |

---

## 7. 验证（**必须目视**）

```
① 把全部提取图渲染成联系表（contact sheet），逐图看内容
② 逐图核对「图内容 ↔ 图题」是否配对正确
③ 与原件并排对读同号图
④ 检查是否有整页渲染图（尺寸接近页面尺寸 → 高度可疑）
⑤ 检查纯色/极窄条/重复 md5
⑥ **同页多图逐张目视**：张数对不代表内容对——
   过度合并（两图合一）与切碎（图只剩一半）都只能靠眼睛发现
   （#20 p18 三张堆叠柱状图曾被从中间切开，张数 24/24 全对但内容是半张图）
```

> #20 v2 抽验页目视：图 1/3/4/5/6/7/8/9/18/19/20 与图题**全部配对正确**——这类抽验是本步骤的验收标准。
>
> #20 v3 修复后目视：p18 的 13/14/15 三张分别为「abrupt 柱状图」「gradual 柱状图」
> 「伪标签质量曲线」，各自完整、互不重叠；#12 p3 的 1/2 分别为「数字孪生概念图」
> 「跌倒检测三栏流程」，各自完整（383×240pt / 474×255pt）。
