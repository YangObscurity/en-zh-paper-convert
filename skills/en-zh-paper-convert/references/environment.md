# 环境策略（Environment is primary）

> 用户指示（2026-10-14）：
> 「如果发现并确定是下载的不同的工具功能没有我们之前的强。则说明环境是首要的，必须要要求用户下载要求的工具才能执行任务。
>  我们由于是建立的环境。由你给我下载 venv 的工具。添加到 .gitignore」

## 1. 实测结论：**能力等价，环境是首要条件**

用**真实基准构建源**（#03，双栏、`xeCJK`+`tabularx`+`booktabs`+`caption`+`fontspec`+`ragged2e`+`etoolbox`）
在本地便携工具链上编译，与容器 xelatex 对比：

| 指标 | tectonic 0.15.0（本机） | xelatex（远端容器） | 结论 |
|---|---|---|---|
| 返回码 | **0** | 0 | 等价 |
| LaTeX 错误 | **0** | 0 | 等价 |
| 页面尺寸 | **576.0 × 783.0**（9/9 页） | 576 × 783 | **精确一致** |
| 中文渲染 | 10,587 CJK 字符 | 正常 | 等价 |
| 失败点 | 仅 `Liberation Serif` 缺失（Linux 字体） | — | **字体问题，非引擎问题** |

→ **tectonic 不是"更弱的工具"**：它能编出与容器**逐值相同**的页面尺寸、零错误、中文正常。
**但它有一个硬前提：字体与宏包必须可得。**

## 2. 因此的环境策略

### P0 环境预检（**硬门控，不通过不得开始**）
```
python scripts/check_env.py
```
必须检查并在缺失时**报错 + 给出精确安装命令**，且**非 0 退出**：

| 依赖 | 必要性 | 本机现状 | 安装方式 |
|---|---|---|---|
| `tectonic` | **必需**（LaTeX 引擎） | ✅ 已随库提供 `tools/tectonic.exe` | 下载 exe（见 §3） |
| `pandoc` | **必需**（md→tex） | ✅ `C:\Program Files\Pandoc\pandoc.exe` v3.12.1 | `winget install JohnMacFarlane.Pandoc` |
| Python `pymupdf` | **必需**（图/表抽取、门控） | ✅ venv 内 | `pip install pymupdf` |
| Python `pillow` | 可选（图幅检查、联系表） | ✅ venv 内 | `pip install pillow` |
| 中文字体 | **必需** | ✅ `Noto Serif SC`/`Noto Sans SC`/`SimSun` | Windows 自带 |
| 西文衬线字体 | **必需** | ✅ `Times New Roman`/`Cambria`/`Georgia` | Windows 自带 |
| poppler (`pdftoppm`/`pdfimages`) | **可替代** → 用 PyMuPDF | ❌ 未装 | 不必装（脚本已内建回退） |
| `qpdf` | 可选 | ❌ 未装 | 不必装 |
| ghostscript | 可选 | ❌ 未装 | 不必装 |

**关键设计**：原来依赖 poppler 的功能（页面渲染、位图提取）**已用 PyMuPDF 内建回退**替代，
因此**不必要求用户额外安装 poppler**。这是"环境为首要"与"最少依赖"的平衡点。

### 字体缺失的处置（**实测踩到的第一个坑**）
- 容器用的 `Liberation Serif` 本机没有 → 编译直接失败
- **策略**：`scripts/_backend.py` 做**字体探测与回退链**，并在日志里明确报告用了哪个回退；
  若必需字体真的都没有 → **报错并告知装哪个**，不得静默用错字体出稿

## 3. 由我们提供的工具（venv + tools/，均入 .gitignore）

用户授权："由你给我下载 venv 的工具，添加到 .gitignore"

```
.venv/                  # Python 虚拟环境（pymupdf / pypdf / pillow）
tools/tectonic.exe      # LaTeX 引擎（~48 MB，单文件）
tools/tectonic.zip      # 原始分发包（~18 MB，可选保留）
```

**这些都不入库**（`.gitignore` 已排除），改为提供**一键获取脚本** `scripts/setup_env.py`：
- 检测已有 → 跳过
- 缺失 → 下载/安装 → 验证
- 全部落盘到 `.venv/` 与 `tools/`，与仓库代码解耦

## 4. 与"环境为首要"配套的纪律

1. **预检先于一切**：`check_env.py` 不通过 → 不进入版式审计，直接失败
2. **错误必须可操作**：缺什么就打印"装什么、怎么装"，不是裸 traceback
3. **不得静默降级**：字体缺失、引擎缺失、宏包拉取失败，都要**显式失败**而不是产出低质 PDF
4. **可复现性**：`tectonic` 首次编译会联网拉宏包并缓存；离线环境需预拉（`--only-cached` 可离线）

## 5. 未验证/风险

- **首次编译需联网**（拉 TeX 宏包）；离线场景未测试 → 待补 `--only-cached` 预拉流程
- `ssh-xelatex` 后端**未在真实远端验证**
- 三栏版式**不支持**（见待决项）
