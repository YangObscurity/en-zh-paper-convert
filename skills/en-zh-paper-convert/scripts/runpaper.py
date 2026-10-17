#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""runpaper.py -- 单篇编排：台账门控 -> 几何 -> 模板 -> 剥离 -> 拆段 -> 图表提取
-> prep -> 编译 -> 门控 -> 产出 PDF。

设计要点
    · **版式硬门控**：省略 --mode 时从台账取；命令行与台账冲突即拒绝（exit 2）；
      台账无该篇记录也拒绝。这是把约束 R2「禁止全局覆盖」写成**代码强制**，
      不是文档约定 —— 只有硬门控才能防止「某次偷懒全局套用」。
    · **门控不得静默通过**：日志读不到即判 gate-log-unreadable 失败，绝不默认 0。
      实战血训：日志抓取失败被静默当成「0 个错误」=> 门控形同虚设。
    · **执行后端可配置**：local-tectonic（默认）/ ssh-xelatex。见 _backend.py。
    · 几何单位一律 **bp**（PostScript point），不是 pt。

依赖
    Python 3.9+ 与 PyMuPDF：pip install pymupdf
    pandoc（markdown -> LaTeX）：winget install --id JohnMacFarlane.Pandoc -e
    编译引擎：tectonic（默认，见 _backend.py）或远端 xelatex
    可选：pdftoppm/pdfimages（poppler）用于图提取与审计页渲染

用法
    python scripts/runpaper.py 20 --ledger .work/format-ledger.tsv
    python scripts/runpaper.py 20 --mode conf --allow-no-ledger
    python scripts/runpaper.py 20 --backend ssh-xelatex --ssh-host me@box:2222

台账 TSV 列（制表符分隔，首行为表头）
    NN  原件  原件版式  判定依据  审计方式  审计人  审计时间  采用模式  备注  [纸张]  [页边距]  [版心宽]
    第 8 列「采用模式」取 conf(2栏) / access(1栏) / tri(3栏)；其后可选列供几何使用。
    第 11 列「页边距」列序为 **L/T/R/B**（与 detect_geometry.py / 本脚本一致）。

版式支持门控（**拒绝静默降级**）
    · 只有 conf / access / tri 三种模板；4 栏及以上**无法表达**。
    · 当台账或自动检测判定原件栏数超出支持范围（且与所选模式不符）时，
      打印「REFUSING TO BUILD」并 **exit 2**，不产出 PDF。
    · 确需降级时须显式 --allow-layout-downgrade，并在台账备注留 DEVIATION。

结构化输出（末尾一行 SUMMARY 便于 agent 解析）
    [1] ... [9] 各阶段进度
    GATE ERRS=0 MISS=0 OVF_H_MAX=0.0 VBOX_MAX=0.0 PAGES=25 TRACES=0 FIGS=24/24 TABLES=20
    SUMMARY JSON={"nn": "20", "verdict": "PASS", "mode": "conf", ...}
    GATE-PASS -> out.pdf     /     GATE-FAIL: <原因>
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

try:  # 中文 Windows 控制台默认 GBK，打印含特殊符号的作者名会崩
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _backend  # noqa: E402
from _workspace import resolve_workspace_root, skill_dir_of  # noqa: E402

# Skill directory (holds SKILL.md / scripts/ / references/) vs. the runtime
# workspace root (holds Paper/ .work/ out/).  After the move into
# skills/en-zh-paper-convert/ these are two different directories; see
# _workspace.py for the resolution rules.  An explicit --root still wins.
SKILL_DIR = skill_dir_of(__file__)
DEFAULT_ROOT = resolve_workspace_root(SKILL_DIR)

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_DEP = 0, 1, 2, 3

# 门控阈值（基准全库收口口径）
TH_OVF_H = 8.0        # \hbox 越栏最大允许值（pt）
TH_OVF_V = 120.0      # \vbox 才允许到 120pt（浮动体位置常见无害告警）
TH_FIG_ASPECT = 12    # 图幅纵横比上限（规范类文档有合法超宽图，8 会误报）
TH_FIG_MIN_W, TH_FIG_MIN_H = 80, 60
TH_FIG_STD = 5.0      # 近纯色拒绝阈值

TRACE_RE = re.compile(r'译注|翻译说明|篇末自查|源文件|pdftotext|/tmp/zh|译文信息|全译完成')

# ---------------------------------------------------------------- 版式支持
# Supported template modes.  Adding a mode means adding a real template below;
# the gate refuses anything not in this set (no silent single-column fallback).
MODES = ('conf', 'access', 'tri')
# Human-readable column counts, used in refusal messages.
MODE_COLS = {'conf': 2, 'access': 1, 'tri': 3}
MAX_COLS = 3          # this skill can typeset 1..3 columns; >=4 must FAIL

# How many text columns the original PDF has.  Used only to DECIDE WHETHER TO
# FAIL -- never to override the ledger (R2/R3 still require visual audit).
NCOL_WORDS = {'single': 1, 'one': 1, '1': 1, '1col': 1, '1-col': 1,
              'double': 2, 'two': 2, '2': 2, '2col': 2, '2-col': 2,
              'triple': 3, 'three': 3, '3': 3, 'tri': 3, '3col': 3, '3-col': 3,
              'threecol': 3, 'three-col': 3}

INSTALL = {
    'pandoc': ('缺少 pandoc（markdown -> LaTeX 必需）。\n'
               '  winget install --id JohnMacFarlane.Pandoc -e\n'
               '  choco install pandoc'),
    'fitz': '缺少 PyMuPDF。安装：\n  pip install pymupdf',
}


# ---------------------------------------------------------------- 工具
def _log(msg):
    print(msg)
    sys.stdout.flush()


def sh(cmd, cwd=None, timeout=1800):
    """跑子进程并同时捕获 stdout/stderr（UTF-8 容错）。"""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                       shell=isinstance(cmd, str))
    out = (p.stdout or b'').decode('utf-8', 'replace')
    err = (p.stderr or b'').decode('utf-8', 'replace')
    return p.returncode, out, err


def require_python_dep():
    try:
        import fitz  # noqa: F401
    except ImportError:
        print('ERROR: ' + INSTALL['fitz'], file=sys.stderr)
        sys.exit(EXIT_DEP)


def esc(t):
    """LaTeX 特殊字符转义（标题页/元信息用）。"""
    t = t.replace('\\', r'\textbackslash{}')
    t = re.sub(r'([&%$#_{}])', r'\\\1', t)
    return t.replace('~', r'\textasciitilde{}').replace('^', r'\textasciicircum{}')


def nobreak(t):
    """把带连字符的复合词包进 \\mbox，避免标题页断行难看。"""
    return re.sub(r'(?<![\w\\{])([A-Za-z][A-Za-z0-9]*(?:--?[A-Za-z0-9]+)+)(?![\w}])',
                  r'\\mbox{\1}', t)


def _cols_from_text(txt):
    """Map a free-form layout description (ledger column or geometry field) to a
    column COUNT, or None when unrecognisable.

    Recognises 'single'/'double'/'tri'/'3'/'3col'/'three-col' and the Chinese
    forms used in the ledger (written as escapes so this file stays pure ASCII).
    """
    if not txt:
        return None
    s = str(txt).strip().lower()
    if not s:
        return None
    zh = {u'\u5355\u680f': 1, u'\u53cc\u680f': 2, u'\u4e09\u680f': 3,
          u'\u56db\u680f': 4, u'\u4e94\u680f': 5, u'\u516d\u680f': 6}
    for k, v in zh.items():
        if k in s:
            return v
    if s in NCOL_WORDS:
        return NCOL_WORDS[s]
    m = re.search(r'([0-9]+)\s*[- ]?\s*(?:col|column)', s)
    if m:
        return int(m.group(1))
    for w, v in (('single', 1), ('one', 1), ('double', 2), ('two', 2),
                 ('triple', 3), ('three', 3), ('tri', 3),
                 ('quad', 4), ('four', 4)):
        if re.search(r'(?<![a-z])%s(?![a-z])' % w, s):
            return v
    return None


def detect_columns(pdf, max_pages=6):
    """Count text columns of the ORIGINAL pdf -> (ncols, evidence).

    Heuristic ONLY: it exists so the skill can notice a layout it cannot
    typeset and refuse, instead of silently downgrading.  It never replaces the
    visual audit required by R3 -- the ledger still decides the mode.

    Method: per sampled body page, take the text-block x-intervals and find
    vertical whitespace gutters (>=6pt runs with zero body-text coverage that do
    not touch the page edge).  columns = gutters + 1, majority-voted.
    """
    try:
        import fitz
    except ImportError:
        return None, 'fitz-missing'
    try:
        doc = fitz.open(pdf)
    except Exception as exc:
        return None, 'open-failed:%s' % exc

    votes = {}
    try:
        for pno in range(min(doc.page_count, max_pages)):
            page = doc[pno]
            W = page.rect.width
            blocks = [b for b in page.get_text('blocks') if b[4].strip()]
            if len(blocks) < 8:              # cover / full-page figure / TOC
                continue
            body = [b for b in blocks if (b[2] - b[0]) < 0.72 * W]
            if len(body) < 6:
                continue
            Wn = int(W) + 2
            cov = [0] * Wn
            for (x0, x1) in [(b[0], b[2]) for b in body]:
                for x in range(max(0, int(x0)), min(Wn, int(x1) + 1)):
                    cov[x] += 1
            GUT_MIN = 6
            runs, run0 = [], None
            for x in range(Wn):
                if cov[x] == 0:
                    if run0 is None:
                        run0 = x
                else:
                    if run0 is not None and x - run0 >= GUT_MIN:
                        runs.append((run0, x))
                    run0 = None
            if run0 is not None and Wn - run0 >= GUT_MIN:
                runs.append((run0, Wn))
            inner = [(a, b) for (a, b) in runs
                     if a > 0.04 * W and b < 0.99 * W]
            n = len(inner) + 1
            if n > 1:
                votes[n] = votes.get(n, 0) + 1
    finally:
        doc.close()
    if not votes:
        return 1, 'no-gutter-detected'
    best = max(votes, key=lambda k: (votes[k], k))
    return best, 'gutters=%s' % votes


def _layout_name(ncols):
    return {1: 'single-column', 2: 'two-column',
            3: 'three-column', 4: 'four-column'}.get(ncols, '%d-column' % ncols)


def unsupported_layout_reason(mode, ncols, source, nn, orig, ncols_force=None):
    """Return a refusal message when the layout is NOT typesettable, else None.

    This is the anti-silent-downgrade gate.  Producing a single-column PDF from
    a three-column original and reporting success is precisely the failure mode
    this function exists to stop: the run must exit non-zero with a reason and
    an actionable next step.
    """
    if mode not in MODES:
        return ('REFUSING TO BUILD #%s: unsupported mode %r.\n'
                '  supported modes: %s (1/2/3 columns).'
                % (nn, mode, '|'.join(MODES)))
    ncols = ncols_force if ncols_force is not None else ncols
    parts = []
    if ncols is not None and ncols > MAX_COLS:
        parts.append('the original is a %s layout (%d columns), but this skill '
                     'typesets 1-%d columns only'
                     % (_layout_name(ncols), ncols, MAX_COLS))
    if ncols is not None and ncols <= MAX_COLS and ncols != MODE_COLS[mode]:
        parts.append('the original is %s (%d columns) but --mode %s produces %s'
                     % (_layout_name(ncols), ncols, mode, _layout_name(MODE_COLS[mode])))
    if not parts:
        return None
    out = ['REFUSING TO BUILD #%s: layout not supported by this skill.' % nn]
    for p in parts:
        out.append('  - ' + p)
    out.append('  detection source: %s' % source)
    out.append('  original: %s' % os.path.basename(orig or '?'))
    if ncols is not None and ncols > MAX_COLS:
        out.append('  Building it anyway would silently emit a DIFFERENT layout '
                   '(a fake success), so the run stops here.')
        out.append('  What you can do:')
        out.append('    * typeset this paper outside the skill (publisher/institution template), or')
        out.append('    * explicitly accept the reduced layout: --mode access '
                   '--allow-layout-downgrade, and record the deviation in the ledger.')
    else:
        out.append('  What you can do:')
        out.append('    * re-run with the matching mode: --mode %s' % '|'.join(MODES))
        out.append('    * or fix column 8 (mode) of the layout ledger.')
    return '\n'.join(out)


def read_ledger(path):
    """读版式台账 -> {NN: {'mode':..., 'orig':..., 'paper':..., 'margins':...}}"""
    led = {}
    if not path or not os.path.isfile(path):
        return led
    with open(path, encoding='utf-8') as fh:
        lines = fh.read().split('\n')
    for ln in lines[1:]:
        p = ln.split('\t')
        if len(p) >= 8 and p[0].strip():
            rec = {'orig': p[1].strip(), 'layout': p[2].strip(), 'mode': p[7].strip()}
            if len(p) >= 12:
                rec['paper'] = p[9].strip()
                rec['margins'] = p[10].strip()
                rec['text_width'] = p[11].strip()
            led[p[0].strip()] = rec
    return led


def load_geometry(path, nn, stub=None):
    """几何 JSON -> {'paper_pt':[W,H], 'margins_pt':[L,T,R,B], 'text_width_pt':...}

    兼容三种形态（detect_geometry.py 产出的是第 2 种）：
      1. dict 按篇号：{"20": {...}}
      2. list 按文件名：[{"file": "<stub>.pdf", "paper_pt": ..., ...}]   <- detect_geometry 默认
      3. list 带 nn 字段：[{"nn": "20", ...}]
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception as exc:
        _log('WARN: geometry JSON 解析失败：%s' % exc)
        return None
    if isinstance(data, dict):
        rec = data.get(str(nn)) or data.get(nn)
        return rec if isinstance(rec, dict) else None
    if not isinstance(data, list):
        return None
    # 先按 nn 精确匹配
    for rec in data:
        if isinstance(rec, dict) and rec.get('nn') is not None \
                and str(rec['nn']).zfill(2) == str(nn).zfill(2):
            return rec
    # 再按文件名匹配（stub 可能含中文/空格，故用 basename 前缀比较）
    if stub:
        want = (stub + '.pdf').lower()
        for rec in data:
            if not isinstance(rec, dict):
                continue
            f = os.path.basename(str(rec.get('file', ''))).lower()
            if f == want or f.startswith(stub.lower()):
                return rec
    # 只有一条记录时直接用它（篇幅小、单篇测量的常见场景）
    if len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    return None


# ---------------------------------------------------------------- 字体
# 字体候选（按优先级）。**跨平台关键**：
#   Liberation/DejaVu 是 Linux 常见，Windows 上多数没有；反之 Times New Roman/SimSun
#   在 Linux 上常缺。因此不写死字体名，而是在本机探测出真实存在的那个再写进导言区。
FONT_CANDIDATES = {
    'latin': ['Liberation Serif', 'Times New Roman', 'DejaVu Serif', 'Georgia',
              'Cambria', 'Latin Modern Roman', 'FreeSerif'],
    'cjk': ['Noto Serif SC', 'Noto Serif CJK SC', 'Source Han Serif SC', 'SimSun',
            'Songti SC', 'AR PL UMing CN', 'WenQuanYi Zen Hei'],
    'cjksans': ['Noto Sans SC', 'Noto Sans CJK SC', 'Source Han Sans SC',
                'Microsoft YaHei', 'SimHei', 'PingFang SC', 'WenQuanYi Micro Hei'],
    'mono': ['DejaVu Sans Mono', 'Liberation Mono', 'Consolas', 'Courier New',
             'Noto Sans Mono', 'Latin Modern Mono'],
}


def _system_font_names():
    """返回本机已安装字体名的集合（小写、去空格），用于候选匹配。

    探测不到时返回 None（表示「无法枚举」-> 调用方直接采用首选候选，
    把结果交给编译日志暴露，而不是误判成「字体不存在」）。
    """
    names = set()
    try:
        if os.name == 'nt':
            d = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
            if os.path.isdir(d):
                for f in os.listdir(d):
                    base = os.path.splitext(f)[0].lower().replace(' ', '')
                    names.add(base)
                    for suf in ('-vf', '-regular', '-bold', '-italic', '-light', '-medium'):
                        if base.endswith(suf):
                            names.add(base[:-len(suf)])
                return names
        fc = shutil.which('fc-list')
        if fc:
            rc, out, _ = sh(['fc-list', '--format', '%{family}\n'])
            if rc == 0:
                for line in out.split('\n'):
                    for fam in line.split(','):
                        fam = fam.strip()
                        if fam:
                            names.add(fam.lower().replace(' ', ''))
                return names
        for d in ('/usr/share/fonts', '/Library/Fonts',
                  os.path.expanduser('~/Library/Fonts')):
            if os.path.isdir(d):
                for root, _dirs, files in os.walk(d):
                    for f in files:
                        if f.lower().endswith(('.ttf', '.otf', '.ttc')):
                            names.add(os.path.splitext(f)[0].lower().replace(' ', ''))
        return names or None
    except Exception:
        return None


_SYS_FONTS = None

# Family name -> on-disk basename fragment, for fonts whose FAMILY differs from
# their FILE name.  WHY: on Windows the enumeration above can only see FILE
# basenames, but fontspec resolves FAMILY names.  "Consolas" ships as
# consola.ttf, so the family name never appears in the file listing and the
# lookup reported "WARN: 未找到可用的 mono 字体" on machines that DO have it.
# Measured on this box: C:\Windows\Fonts\consola.ttf + consolab/i/z.ttf exist
# while the set contains only {consola, consolab, consolai, consolaz}.
#
# These are ADD-ONLY aliases: they are consulted after the exact and prefix
# matches, so an existing successful resolution can never be displaced by one.
FONT_FILE_ALIASES = {
    'consolas': ('consola',),
    'timesnewroman': ('times',),
    'couriernew': ('cour',),
    'microsoftyahei': ('msyh',),
    'simsun': ('simsun',),
    'simhei': ('simhei',),
    'segoeui': ('segoeui',),
    'cambria': ('cambria',),
    'calibri': ('calibri',),
    'arial': ('arial',),
    'helvetica': ('arial',),
    'dejavusansmono': ('dejavusansmono', 'dejavusans'),
    'dejavuserif': ('dejavuserif', 'dejavusans'),
    'liberationserif': ('liberationserif', 'liberation'),
    'liberationmono': ('liberationmono', 'liberation'),
    'latemodernmono': ('lmmono', 'lmroman'),
    'latinmodernroman': ('lmroman',),
    'notoserifsc': ('notoserifsc', 'notoserif'),
    'notosanssc': ('notosanssc', 'notosans'),
    'notoserifcjksc': ('notoserifcjk', 'notoserif'),
    'notosanscjksc': ('notosanscjk', 'notosans'),
    'sourcehanserifsc': ('sourcehanserif',),
    'sourcehansanssc': ('sourcehansans',),
}


def _font_keys(name):
    """All enumeration keys that could legitimately stand for this family name."""
    key = name.lower().replace(' ', '')
    out = [key]
    for alias in FONT_FILE_ALIASES.get(key, ()):
        if alias not in out:
            out.append(alias)
    return out


def resolve_font(preferred, candidates):
    """在候选列表里挑第一个本机真实存在的字体名；都探测不到则返回 preferred。

    匹配顺序（**先严格后宽松**，保证历史行为不变）：
      ① 精确同名   ② 已安装名以候选名开头（变体：NotoSerifSC-VF）
      ③ FONT_FILE_ALIASES 的「族名 -> 文件名片段」别名（仍走精确/前缀命中）
    ③ 是本次为「族名≠文件名」字体（Consolas/consola.ttf）新增的；它排在最后，
    因此不会改变任何原本就能解析成功的字体。

    NOTE: a reverse-prefix rule (key.startswith(have)) is deliberately NOT used.
    It looks tempting for consolas->consola, but it also matches names that do
    not exist at all ('Consolaxx', 'GeorgiaX' -> consola, georgia), handing
    fontspec a family that will fail to load.  The alias table is a WHITELIST of
    real family/file differences instead.
    """
    global _SYS_FONTS
    if _SYS_FONTS is None:
        _SYS_FONTS = _system_font_names()
    order = [preferred] + [c for c in candidates if c != preferred]
    if _SYS_FONTS is None:
        return preferred
    for name in order:
        for key in _font_keys(name):
            if key in _SYS_FONTS:
                return name
            for have in _SYS_FONTS:          # 变体匹配：NotoSerifSC-VF 之类
                if have.startswith(key):
                    return name
    return None


# ------------------------------------------------------------ glyph fallbacks
# U+XXXX -> LaTeX replacement, injected as \newunicodechar lines into the
# preamble.  WHY THIS TABLE EXISTS: the fonts picked by resolve_font() (serif /
# CJK / mono) have NO glyph for many math/Greek/operator codepoints that paper
# translators leave verbatim in the markdown.  Without a fallback XeLaTeX does
# not error -- it prints NOTHING and logs "Missing character: There is no <c> in
# font", which G2 counts.  Measured on S2 (IEEE WSN paper): MISS=52, dominated by
# U+2032 PRIME x38 (unit marks like "m/s2'" style quantities and derivatives),
# plus alpha/lambda/not-equal.
#
# NOTE ON \prime: unlike most entries this one is NOT a plain symbol command in
# text mode -- a bare '\prime' outside math mode raises "Missing $ inserted".
# Wrapping in \ensuremath{} makes it legal AND TeX/typeset as the ordinary
# (superscript-positioned) prime, so "B<U+2032>" renders as B-prime.  Verified by
# rendering the S2 page and reading the crop back: the prime sits at cap height
# right after the B, and the text layer still extracts U+2032 (77 occurrences).
#
# ASCII-ONLY SOURCE DISCIPLINE: every codepoint below is written as a \uXXXX
# escape.  PowerShell round-trips of non-ASCII .py files have already corrupted
# this repo twice (mojibake AND swallowed following lines), so the file must
# stay pure ASCII.
GLYPH_FALLBACKS = (
    # --- math operators / relations that were already present -----------------
    (0x2217, r'\ensuremath{\ast}'),
    (0x2218, r'\ensuremath{\circ}'),
    (0x2219, r'\ensuremath{\bullet}'),
    (0x22C5, r'\ensuremath{\cdot}'),
    (0x225C, r'\ensuremath{\triangleq}'),
    (0x2297, r'\ensuremath{\otimes}'),
    (0x2295, r'\ensuremath{\oplus}'),
    (0x2213, r'\ensuremath{\mp}'),
    (0x2216, r'\ensuremath{\setminus}'),
    (0x2225, r'\ensuremath{\parallel}'),
    (0x230A, r'\ensuremath{\lfloor}'),
    (0x230B, r'\ensuremath{\rfloor}'),
    (0x2308, r'\ensuremath{\lceil}'),
    (0x2309, r'\ensuremath{\rceil}'),
    (0x22EF, r'\ensuremath{\cdots}'),
    (0x22EE, r'\ensuremath{\vdots}'),
    (0x2234, r'\ensuremath{\therefore}'),
    (0x2235, r'\ensuremath{\because}'),
    (0x2254, r'\ensuremath{:=}'),
    (0x2205, r'\ensuremath{\emptyset}'),
    (0x22C6, r'\ensuremath{\star}'),
    (0x223C, r'\ensuremath{\sim}'),
    (0x2245, r'\ensuremath{\cong}'),
    (0x221D, r'\ensuremath{\propto}'),
    # --- ADDED for gap 1 (S2 MISS=52) ----------------------------------------
    # U+2032 PRIME -- the single biggest offender (38 hits on S2).  See the
    # \prime note above; \ensuremath is what makes it legal here.
    (0x2032, r'\ensuremath{\prime}'),
    # Greek lower case (alpha/lambda are on S2; the rest are the usual suspects)
    (0x03B1, r'\ensuremath{\alpha}'),
    (0x03B2, r'\ensuremath{\beta}'),
    (0x03B3, r'\ensuremath{\gamma}'),
    (0x03B4, r'\ensuremath{\delta}'),
    (0x03B5, r'\ensuremath{\epsilon}'),
    (0x03B6, r'\ensuremath{\zeta}'),
    (0x03B7, r'\ensuremath{\eta}'),
    (0x03B8, r'\ensuremath{\theta}'),
    (0x03B9, r'\ensuremath{\iota}'),
    (0x03BA, r'\ensuremath{\kappa}'),
    (0x03BB, r'\ensuremath{\lambda}'),
    (0x03BC, r'\ensuremath{\mu}'),
    (0x03BD, r'\ensuremath{\nu}'),
    (0x03BE, r'\ensuremath{\xi}'),
    (0x03C0, r'\ensuremath{\pi}'),
    (0x03C1, r'\ensuremath{\rho}'),
    (0x03C3, r'\ensuremath{\sigma}'),
    (0x03C4, r'\ensuremath{\tau}'),
    (0x03C5, r'\ensuremath{\upsilon}'),
    (0x03C6, r'\ensuremath{\phi}'),
    (0x03C7, r'\ensuremath{\chi}'),
    (0x03C8, r'\ensuremath{\psi}'),
    (0x03C9, r'\ensuremath{\omega}'),
    # Greek upper case (only the ones without a Latin look-alike)
    (0x0393, r'\ensuremath{\Gamma}'),
    (0x0394, r'\ensuremath{\Delta}'),
    (0x0398, r'\ensuremath{\Theta}'),
    (0x039B, r'\ensuremath{\Lambda}'),
    (0x039E, r'\ensuremath{\Xi}'),
    (0x03A0, r'\ensuremath{\Pi}'),
    (0x03A3, r'\ensuremath{\Sigma}'),
    (0x03A6, r'\ensuremath{\Phi}'),
    (0x03A8, r'\ensuremath{\Psi}'),
    (0x03A9, r'\ensuremath{\Omega}'),
    # Relations / arrows / misc operators
    (0x2260, r'\ensuremath{\neq}'),
    (0x2264, r'\ensuremath{\leq}'),
    (0x2265, r'\ensuremath{\geq}'),
    (0x2248, r'\ensuremath{\approx}'),
    (0x2261, r'\ensuremath{\equiv}'),
    (0x221E, r'\ensuremath{\infty}'),
    (0x2208, r'\ensuremath{\in}'),
    (0x2209, r'\ensuremath{\notin}'),
    (0x2202, r'\ensuremath{\partial}'),
    (0x2207, r'\ensuremath{\nabla}'),
    (0x2211, r'\ensuremath{\sum}'),
    (0x220F, r'\ensuremath{\prod}'),
    (0x222B, r'\ensuremath{\int}'),
    (0x221A, r'\ensuremath{\sqrt{}}'),
    (0x2192, r'\ensuremath{\rightarrow}'),
    (0x2190, r'\ensuremath{\leftarrow}'),
    (0x21D2, r'\ensuremath{\Rightarrow}'),
    (0x21D4, r'\ensuremath{\Leftrightarrow}'),
    (0x2200, r'\ensuremath{\forall}'),
    (0x2203, r'\ensuremath{\exists}'),
    (0x220B, r'\ensuremath{\ni}'),
    (0x222A, r'\ensuremath{\cup}'),
    (0x2229, r'\ensuremath{\cap}'),
    (0x2286, r'\ensuremath{\subseteq}'),
    (0x2282, r'\ensuremath{\subset}'),
    (0x2205, r'\ensuremath{\emptyset}'),
    (0x2016, r'\ensuremath{\|}'),
    (0x2308, r'\ensuremath{\lceil}'),
    (0x2032, r'\ensuremath{\prime}'),
    # Text-mode punctuation that some fonts lack
    (0x2033, r'\ensuremath{\prime\prime}'),
    (0x00B7, r'\ensuremath{\cdot}'),
    (0x2013, r'\textendash'),
    (0x2014, r'\textemdash'),
    (0x2026, r'\textellipsis'),
    (0x00A0, r'~'),
)


def _glyph_block():
    """Render GLYPH_FALLBACKS as \\newunicodechar lines (de-duplicated).

    Later duplicate codepoints win, which lets the gap-1 additions override the
    original, smaller table without having to keep two lists in sync by hand.
    """
    seen = {}
    for cp, rep in GLYPH_FALLBACKS:
        seen[cp] = rep
    out = []
    for cp in sorted(seen):
        out.append(r'\newunicodechar{%s}{%s}' % (chr(cp), seen[cp]))
    return '\n'.join(out)


# ---------------------------------------------------------------- 模板
COMMON_PREAMBLE = r'''\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{tabularx,booktabs,array}
\usepackage{caption}
\usepackage{fontspec}
\usepackage{xeCJK}
\setmainfont{@@LATIN@@}
\setCJKmainfont{@@CJK@@}
\setCJKsansfont{@@CJKSANS@@}
\setmonofont{@@MONO@@}
\usepackage{newunicodechar}
@@GLYPHS@@
\usepackage[hidelinks]{hyperref}
\usepackage{xurl}
\usepackage{longtable}
\usepackage{fvextra}
\DefineVerbatimEnvironment{verbatim}{Verbatim}{breaklines=true,breakanywhere=true,breaksymbolleft={},breaksymbolright={},fontsize=\scriptsize}
\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}
% ragged2e 提供 \RaggedRight：配合连字解决窄 X 列内长英文词越栏
\usepackage{ragged2e}
\usepackage{etoolbox}
% 所有表格环境内启用连字与 sloppy：窄 X 列遇长英文词（Exponential-Rayleigh 等）可词内断行
\AtBeginEnvironment{table}{\sloppy\hyphenpenalty=50\exhyphenpenalty=50\relax}
\AtBeginEnvironment{table*}{\sloppy\hyphenpenalty=50\exhyphenpenalty=50\relax}
% 浮动体参数放宽：填满页面、减少大片空白（表多且多为全宽大表）
\usepackage{morefloats}
\setcounter{topnumber}{6}
\setcounter{bottomnumber}{6}
\setcounter{totalnumber}{12}
\renewcommand{\topfraction}{0.95}
\renewcommand{\bottomfraction}{0.95}
\renewcommand{\textfraction}{0.05}
\renewcommand{\floatpagefraction}{0.80}
\renewcommand{\figurename}{图}
\setlength{\emergencystretch}{3em}
\sloppy
'''

IEEE_SHIM = r'''% article 类兼容原作的 IEEEtran 标题块命令
\providecommand{\IEEEauthorblockN}[1]{\textbf{#1}\par}
\providecommand{\IEEEauthorblockA}[1]{\par\small #1\par}
\providecommand{\IEEEauthorrefmark}[1]{#1}
'''


def _source_has_abstract(pdf_path):
    """True when the SOURCE paper actually contains an abstract.

    Used to keep the `abstract-empty` gate honest: an empty abstract is a defect
    only if the original had one.  Measured on #27, an OMA technical
    specification, whose source has no abstract at all.

    Heuristic on purpose: look for the word "Abstract" in a heading-like position
    on an early page.  Never raises -- unreadable PDFs answer True so the gate
    stays conservative (it is safer to ask for a check than to hide a loss).
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        for pno in range(min(3, doc.page_count)):
            txt = doc[pno].get_text()
            for ln in txt.splitlines():
                s = ln.strip()
                if not s or len(s) > 40:
                    continue
                low = s.lower()
                if low.startswith('abstract') or low == '摘要':
                    return True
        return False
    except Exception:
        return True


def _content_coverage(md_path, pdf_path, min_run=12):
    """Fraction of the source markdown's CJK content that reached the PDF.

    Returns (ratio, n_runs, n_missing, sample_missing) or None when the check
    cannot run.  Deliberately tolerant: it detects GROSS loss (a dropped
    section), not typographic differences.
    """
    try:
        import fitz
    except Exception:
        return None
    try:
        src = open(md_path, encoding='utf-8').read()
    except OSError:
        return None
    # Candidate runs: lines of CJK-heavy prose from the cleaned markdown.
    runs = []
    for line in src.split('\n'):
        s = line.strip()
        if len(s) < min_run:
            continue
        if s.startswith('```'):
            continue
        cjk = sum(1 for ch in s if '\u4e00' <= ch <= '\u9fff')
        if cjk < min_run:
            continue
        # keep only the CJK characters: markdown/LaTeX noise is irrelevant here
        pure = ''.join(ch for ch in s if '\u4e00' <= ch <= '\u9fff')
        if len(pure) >= min_run:
            runs.append(pure)
    if not runs:
        return None
    try:
        doc = fitz.open(pdf_path)
        hay = ''.join(p.get_text() for p in doc)
        doc.close()
    except Exception:
        return None
    # Compare CJK-ONLY on both sides.  The markdown runs are already
    # CJK-only, but the PDF interleaves Latin (e.g. 机器学习（Machine Learning）
    # 传感器), so a raw substring test would miss every such run and report a
    # false content loss.  Filtering the PDF text to CJK makes the two
    # representations comparable.
    hay = ''.join(ch for ch in hay
                  if '\u4e00' <= ch <= '\u9fff')
    total = sum(len(r) for r in runs)
    missing_chars = 0
    missing = []
    for r in runs:
        # tolerate a few OCR-ish gaps inside one run: probe in 8-char windows
        w = 8
        hit = 0
        for i in range(0, len(r) - w + 1, w):
            if r[i:i + w] in hay:
                hit += w
        if hit < 0.5 * len(r):
            missing_chars += len(r)
            if len(missing) < 5:
                missing.append(r[:24])
    return (1.0 - missing_chars / float(total), len(runs), missing_chars, missing)



def _abstract_env(abstract_txt=''):
    r'''LaTeX for the abstract environment, or empty when there is no abstract.

    MEASURED: the delivered #27 (an OMA technical specification whose SOURCE
    has no abstract at all) still printed a bare Abstract heading with nothing
    beneath it -- a visible defect in the PDF. Hard-coding the environment
    cannot distinguish a paper that genuinely has no abstract from one where
    the abstract was lost. The abstract-empty GATE reports the second case;
    here we must simply not typeset an empty heading.
    '''
    t = (abstract_txt or '').strip()
    if not t:
        return ''
    return (r'\begin{abstract}' + '\n' + r'\input{abstract.tex}' + '\n'
            + r'\end{abstract}' + '\n')


def build_template(mode, geom, meta, fonts, abstract_tex=''):
    r"""生成 main.tex。

    **关键**：
      · geometry 单位用 **bp**（PostScript point）。用 pt（TeX point）会引入
        72/72.27 = 0.99626 的系统性缩放，全库统一缩小 0.374%（612 -> 609.71）。
        这条错误的隐蔽性在于「每篇只差 1-3pt，看起来像阈值没调好」，害人去调阈值。
      · 双栏用 **article[twocolumn] + geometry**，不用 IEEEtran：
        IEEEtran 只认 letterpaper/a4paper，自定义纸张无解
        （geometry 的 paperwidth 与 \pdfpagewidth **都被忽略**，实验 a-h 逐一证伪）。
      · 作者块用 **\parbox[t]{\columnwidth}**，不用 \textwidth/\pageTW：
        双栏上下文里可用宽度只有栏宽（如 229.5pt），用整页宽会越栏 510-531pt。
      · 三栏用 **multicol**（`\begin{multicols}{3}`）+ geometry，**不能用**
        `article[twocolumn]`：article 的 twocolumn 选项只做两栏，无三栏档；
        geometry 的 `columnsep` 也只对 article 自身分栏生效。multicol 的
        `\columnsep` 才是三栏栏距的真开关。实测 S1（CACM 585x783）三栏复刻后
        栏 x 区间 36.0-206.6 / 209.0-378.4 / 382.0-544.9，与原件
        36.0-198.9 / 209.0-371.9 / 382.0-544.9 逐栏起点一致。
    """
    if geom:
        W, H = geom['paper_pt']
        L, T, R, B = geom['margins_pt']
        geo = (r'\usepackage[paperwidth=%gbp,paperheight=%gbp,'
               r'left=%gbp,right=%gbp,top=%gbp,bottom=%gbp]{geometry}' % (W, H, L, R, T, B))
        note = 'geom-from-ledger'
    else:
        geo = (r'\usepackage[letterpaper,left=52bp,right=52bp,top=56bp,bottom=62bp]{geometry}')
        note = 'geom-default-WARNING'
    # 用逐项 replace 而不是 %-格式化：导言区里含大量 LaTeX 的 % 号，会与格式化冲突
    # 字体按「候选列表里第一个系统真实存在的」解析：
    # fontspec 的逗号分隔**不是**回退语法（会被当成一个字体名整体查找而报
    # cannot be found），必须在本层选好再写进去。这是换机器最容易踩的坑。
    fam = {
        'latin': resolve_font(fonts['latin'], FONT_CANDIDATES['latin']),
        'cjk': resolve_font(fonts['cjk'], FONT_CANDIDATES['cjk']),
        'cjksans': resolve_font(fonts['cjksans'], FONT_CANDIDATES['cjksans']),
        'mono': resolve_font(fonts['mono'], FONT_CANDIDATES['mono']),
    }
    for _k in ('latin', 'cjk', 'cjksans', 'mono'):
        if fam[_k] is None:
            print('WARN: 未找到可用的 %s 字体（候选：%s）。\n'
                  '  修法：装一个候选字体，或用 --%s-font 指定本机已有的字体名。'
                  % (_k, ', '.join(FONT_CANDIDATES[_k]),
                     {'latin': 'latin', 'cjk': 'cjk', 'cjksans': 'cjk-sans',
                      'mono': 'mono'}[_k]), file=sys.stderr)
    pre = COMMON_PREAMBLE
    # Glyph fallback table (gap 1).  Done FIRST: the replacement text contains no
    # @@...@@ placeholder, but keeping it ahead of the font loop makes the order
    # explicit and immune to a future font key colliding with a glyph macro name.
    pre = pre.replace('@@GLYPHS@@', _glyph_block())
    _KEY = {'@@LATIN@@': 'LATIN', '@@CJK@@': 'CJK', '@@CJKSANS@@': 'CJKSANS', '@@MONO@@': 'MONO'}
    _CMD = {'@@LATIN@@': 'setmainfont', '@@CJK@@': 'setCJKmainfont',
            '@@CJKSANS@@': 'setCJKsansfont', '@@MONO@@': 'setmonofont'}
    for _k, _v in (('@@LATIN@@', fam['latin']), ('@@CJK@@', fam['cjk']),
                   ('@@CJKSANS@@', fam['cjksans']), ('@@MONO@@', fam['mono'])):
        if _v is None:
            # 缺字体时把**整行**注释掉，让 fontspec 用默认字体继续。
            # 不能只替换花括号里的内容：`%` 会吞掉该行剩余部分（含右花括号），
            # 导致 "File ended while scanning use of \setmonofont"。
            pre = pre.replace(r'\%s{%s}' % (_CMD[_k], _k),
                              '%% font-missing: ' + _CMD[_k])
        else:
            pre = pre.replace(_k, _v)
    # Author / affiliation blocks are typeset inside a \parbox of ONE column
    # width.  In a 3-column layout that is only ~143bp, so a long e-mail
    # address (no hyphen, no breakpoint) overflows by ~95pt and trips the G3
    # gate.  We let TeX break inside long Latin tokens by inserting a
    # zero-width breakpoint (\hspace{0pt}) after the usual e-mail separators,
    # and make the paragraph sloppy.  The visible text is unchanged.
    def breakable(t):
        return re.sub(r'([@._/:])', r'\1\\hspace{0pt}', t)

    _AUX = r'\sloppy\emergencystretch=3em\hyphenpenalty=50\exhyphenpenalty=50 '
    au = (r'\parbox[t]{\columnwidth}{\centering ' + _AUX
          + breakable(nobreak(esc(meta['authors']))) + '}')
    af = (r'\parbox[t]{\columnwidth}{\centering\small ' + _AUX
          + breakable(meta['info']) + '}')
    if mode == 'conf':
        body = (r'''\documentclass[10pt,twocolumn]{article}
''' + geo + '\n' + r'''\setlength{\columnsep}{14pt}
\newlength{\pageTW}\setlength{\pageTW}{\textwidth}
''' + IEEE_SHIM + pre + r'''\begin{document}
% G9 (no build date): \maketitle prints \today in the title block unless \date{}
% is given.  The build date is NEITHER paper metadata NOR translator metadata --
% it is environment leakage that silently stamps the delivery with the machine's
% clock ("October 14, 2026").  Suppress it; a real publication date, when the
% source gives one, belongs in the info block (meta['info']), not here.
\date{}
\title{''' + nobreak(esc(meta['cn_title'])) + r'''\\[3pt]
{\large\normalfont (''' + nobreak(esc(meta['en_title'])) + r''')}}
\author{\IEEEauthorblockN{''' + au + r'''}
\IEEEauthorblockA{''' + af + r'''}}
\twocolumn[\maketitle]
''' + _abstract_env(abstract_tex) + r'''\noindent\input{keywords.tex}
\input{body.tex}
\end{document}
''')
    elif mode == 'tri':
        # Three columns: multicol + geometry (article[twocolumn] cannot do 3).
        # The column width is NOT hard-coded: multicol divides \textwidth
        # itself, so geometry keeps owning paper/margins (in bp).
        csep = geom.get('colsep_pt') if geom else None
        try:
            csep = float(csep)
        except (TypeError, ValueError):
            csep = None
        if csep is None or csep <= 0:
            csep = 10.0            # CACM measured value; see the ledger evidence
            note += '/colsep-default'
        body = (r'''\documentclass[10pt]{article}
''' + geo + '\n' + (r'''\usepackage{multicol}
\setlength{\columnsep}{%gbp}
\setlength{\columnseprule}{0pt}
\newlength{\pageTW}\setlength{\pageTW}{\textwidth}
''' % csep) + pre + r'''\begin{document}
% G9 (no build date): this branch typesets the title block by hand, so \today is
% not printed here today -- but \date{} is set anyway so the gate keeps holding
% if the block is ever switched to \maketitle.  See the conf branch for rationale.
\date{}
\begin{center}
{\LARGE\bfseries ''' + nobreak(esc(meta['cn_title'])) + r'''}\\[4pt]
{\large ''' + nobreak(esc(meta['en_title'])) + r'''}\\[8pt]
{\small ''' + au + r'''}\\[4pt]
{\footnotesize ''' + af + r'''}
\end{center}
\vspace{8pt}
\begin{multicols}{3}
''' + _abstract_env(abstract_tex) + r'''\noindent\input{keywords.tex}
\input{body.tex}
\end{multicols}
\end{document}
''')
    else:
        body = (r'''\documentclass[10pt]{article}
''' + geo + '\n' + pre + r'''\begin{document}
% G9 (no build date): hand-typeset title block, \date{} is set defensively.  See
% the conf branch for the full rationale.
\date{}
\begin{center}
{\LARGE\bfseries ''' + nobreak(esc(meta['cn_title'])) + r'''}\\[4pt]
{\large ''' + nobreak(esc(meta['en_title'])) + r'''}\\[8pt]
{\small ''' + au + r'''}\\[4pt]
{\footnotesize ''' + af + r'''}
\end{center}
\vspace{8pt}
''' + _abstract_env(abstract_tex) + r'''\noindent\input{keywords.tex}
\input{body.tex}
\end{document}
''')
    return body, geo, note


# ---------------------------------------------------------------- 门控
def parse_gates(log_text):
    """从 LaTeX 日志里提门控指标。

    !! 必须**全量取 max** !!  `head -20` 会漏掉最大值 —— 曾因此把有问题的稿子判 PASS。
    """
    g = {}
    g['ERRS'] = len(re.findall(r'(?m)^!', log_text))
    g['MISS'] = len(re.findall(r'Missing character', log_text))
    hbox = [float(x) for x in re.findall(r'Overfull \\hbox \(([0-9.]+)pt', log_text)]
    vbox = [float(x) for x in re.findall(r'Overfull \\vbox \(([0-9.]+)pt', log_text)]
    g['OVF_H'] = len(hbox)
    g['OVF_H_MAX'] = max(hbox) if hbox else 0.0
    g['OVF_V'] = len(vbox)
    g['VBOX_MAX'] = max(vbox) if vbox else 0.0
    m = re.search(r'Output written on .*?\((\d+) pages?', log_text)
    g['PAGES'] = int(m.group(1)) if m else None
    return g


def gate_build_date(pdf_path, today=None):
    """G9: the deliverable must not carry the BUILD date.

    HOW IT CAN HAPPEN: \\maketitle defaults \\date to \\today, so the title block
    silently stamps the compile day ("October 14, 2026") into the PDF.  That is
    neither paper metadata nor translator metadata -- it is environment leakage,
    and it makes the "same source -> same delivery" property false.

    WHY NOT JUST SEARCH FOR ANY DATE: a paper legitimately contains dates
    (submission year, conference date, "October 2021").  Flagging those would be
    a false positive on almost every document.  So this gate looks ONLY for the
    CURRENT build date in the several shapes LaTeX renders \\today as, plus the
    ISO form.  It cannot prove a date is absent from the source; it proves the
    build day did not leak.

    Returns (hits, patterns_checked).  hits == [] means PASS.
    """
    import datetime
    d = today or datetime.date.today()
    months = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
              'August', 'September', 'October', 'November', 'December')
    mon = months[d.month - 1]
    # LaTeX \today under the default (US) datetime format.
    pats = [
        '%s %d, %d' % (mon, d.day, d.year),          # October 14, 2026
        '%d %s %d' % (d.day, mon, d.year),           # 14 October 2026
        '%s. %d, %d' % (mon[:3], d.day, d.year),     # Oct. 14, 2026
        '%04d-%02d-%02d' % (d.year, d.month, d.day),  # 2026-10-14
        '%04d/%02d/%02d' % (d.year, d.month, d.day),  # 2026/10/14
        '%d-%02d-%04d' % (d.day, d.month, d.year),    # 14-10-2026
    ]
    try:
        import fitz
        doc = fitz.open(pdf_path)
        txt = '\n'.join(p.get_text() for p in doc)
        doc.close()
    except Exception as exc:
        return None, ['pdf-unreadable:%s' % exc]
    flat = re.sub(r'\s+', ' ', txt)
    hits = [p for p in pats if p in flat]
    return hits, pats


def gate_figs(pdf_path, exp_figs, workdir):
    """图号闭合 + 图幅异常检查。返回 (n_fig_pdf, anomalies, error)。"""
    bad = []
    try:
        import fitz
    except ImportError:
        return -1, [], 'fitz-missing'
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        return -1, [], 'pdf-open-failed:%s' % exc
    text = '\n'.join(p.get_text() for p in doc)
    n_pages = doc.page_count
    p0 = doc[0].rect
    doc.close()
    # 图幅异常：尺寸过小 / aspect 过大 / md5 重复 / 近纯色
    import hashlib
    try:
        from PIL import Image, ImageStat
        have_pil = True
    except ImportError:
        have_pil = False
    seen = {}
    figdir = os.path.join(workdir, 'figs')
    for f in sorted(glob.glob(os.path.join(figdir, 'fig*.png'))):
        name = os.path.basename(f)
        try:
            data = open(f, 'rb').read()
        except OSError:
            bad.append(name + ':unreadable')
            continue
        d = hashlib.md5(data).hexdigest()
        if d in seen:
            bad.append('%s==%s(dup)' % (name, seen[d]))
        else:
            seen[d] = name
        if have_pil:
            try:
                im = Image.open(f)
                w, h = im.size
                ar = max(w, h) / max(1, min(w, h))
                if w < TH_FIG_MIN_W or h < TH_FIG_MIN_H or ar > TH_FIG_ASPECT:
                    bad.append('%s:%dx%d' % (name, w, h))
                st = ImageStat.Stat(im.convert('L'))
                if st.stddev[0] < TH_FIG_STD:
                    bad.append('%s:solid(std=%.1f)' % (name, st.stddev[0]))
            except Exception:
                pass
    # 图号闭合（子集判定：期望 ⊆ 实际）
    flat = re.sub(r'\s+', ' ', text)
    got = {x.rstrip('.') for x in re.findall(
        r'图\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*[.。（(，,、;；:\s]', flat)}
    exp_set = {str(x).rstrip('.') for x in exp_figs}
    # 兜底：水印把「图」与编号拆到相邻行时，只接受**属于期望集合**的编号
    for x in re.findall(
            r'(?<![0-9A-Za-z.\-])([0-9A-Z][0-9A-Za-z]*(?:[.\-][0-9A-Za-z]+)*)\.\s*[\u4e00-\u9fff]',
            flat):
        x = x.rstrip('.')
        if x in exp_set:
            got.add(x)
    missing = sorted(exp_set - got, key=lambda s: (len(s), s))
    return len(got), bad, (missing if missing else None)


def count_tables_pdf(pdf_path):
    """Count table numbers visible in the delivered PDF (same rule as the gate).

    Two traps, both measured on real runs:
      * matching only pure digits (assumed `\u8868\\s*(\\d+)\\s*\uff08Table`) reported
        a 38-table OMA spec as 0 (ids look like `2.1-1`, `D.8.3-1`).
      * requiring the parenthesised `\uff08Table ...\uff09` form undercounted 16 -> 12,
        because glossary-style captions render as `\u88682.1-1. \u89c4\u8303\u6027\u5f15\u7528`
        with no parentheses at all.
    Accept both caption forms, anchored at LINE START so that cross-references
    such as "\u89c1\u88686.4-2" are not counted as captions:
        "\u8868N. title"    /    "\u8868 N\uff08Table: ...\uff09. title"
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        text = '\n'.join(p.get_text() for p in doc)
        doc.close()
    except Exception:
        return -1
    return len(set(re.findall(
        r'(?:^|\n)\s*\u8868\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*[\uff08(.．]', text)))


def _line_kv(text, key):
    """Read `KEY=<int>` out of a child process's structured output, or None.

    Returns None -- never a default of 0 -- when the key is absent: a missing
    line means "we could not read it", which must stay distinguishable from a
    real zero (the same rule the build-log gate learned the hard way).
    """
    m = re.search(re.escape(key) + r'=(\d+)', text or '')
    return int(m.group(1)) if m else None


def resolve_figs_override(nn, orig, workdir, explicit, root):
    """Locate the human bbox override for this paper, and SAY WHERE IT CAME FROM.

    WHY THIS IS EXPLICIT NOW: extract_figs.py used to
    guess `<outdir>/../figs_override.json` on its own and runpaper.py never passed
    `--override`.  The #16 PASS therefore rested on a hand-placed file inside
    `.work/`, which is git-ignored and temporary -- a clean environment could not reproduce it, and
    `--help` gave no hint that the path existed at all.

    The canonical location is `Paper/<NN>.figs_override.json` (stored
    next to the translation).  Everything else that used to
    work is still accepted, but the winner is always printed.

    Also returns controlled-variable helpers so a run can PROVE the override is
    doing the work rather than merely coexisting with a PASS.
    """
    papdir = os.path.dirname(os.path.abspath(orig)) if orig else \
        os.path.join(os.path.abspath(root), 'Paper')
    stem = os.path.splitext(os.path.basename(orig))[0] if orig else ''
    cands = []
    if explicit:
        cands.append((os.path.abspath(explicit), 'explicit'))
    cands += [
        (os.path.join(papdir, nn + '.figs_override.json'), 'paper-nn'),      # canonical
        (os.path.join(papdir, stem + '.figs_override.json'), 'paper-stem'),
        (os.path.join(workdir, 'figs_override.json'), 'workdir-legacy'),
        (os.path.join(os.path.dirname(os.path.abspath(workdir)),
                      os.path.basename(os.path.abspath(workdir)) + '.figs_override.json'),
         'workdir-sibling'),
    ]
    seen, listed = set(), []
    for path, kind in cands:
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            continue
        seen.add(key)
        listed.append((path, kind, os.path.isfile(path)))
    return listed


def gate_figs_topology(mapping, figs_dir):
    """Classify EVERY extracted figure as auto / override / missing.

    A bare "FIGS=5/5" cannot tell "the algorithm found all five" apart from
    "a human drew one of the boxes".  The three-state tally is what makes a PASS
    auditable.  Operates on the on-disk artifacts (figs.json + fig*.png) so it is
    an INDEPENDENT re-derivation, not a restatement of extract_figs' stdout --
    the same "verify the artifact, not the report" rule used by 7c.

    Returns (auto, override, missing_nos, expected_nos).
    """
    exp = mapping.get('exp') or []
    auto = override = 0
    seen = set()
    jpath = os.path.join(figs_dir, 'figs.json')
    if os.path.isfile(jpath):
        try:
            with open(jpath, encoding='utf-8') as fh:
                info = json.load(fh)
        except Exception:
            info = {}
        for k, v in (info or {}).items():
            if not isinstance(v, dict):
                continue
            seen.add(str(k))
            if v.get('override'):
                override += 1
            else:
                auto += 1
    for f in glob.glob(os.path.join(figs_dir, 'fig*.png')):
        no = os.path.basename(f)[3:-4]
        if no and no not in seen:
            seen.add(no)
            auto += 1
    missing = sorted(set(str(x) for x in exp) - seen, key=lambda s: (len(s), s))
    return auto, override, missing, [str(x) for x in exp]


def figure_override_probe(args, orig, workdir, figdir, exp_figs, cmd):
    """TEMPORARY counterfactual run: what happens with NO override at all?

    Prints `FIGS WITH=... WITHOUT=... => override is load-bearing|inert`.

    This exists because "the override is present and the run passes" is NOT
    evidence that the override is what made it pass.  Re-running the extractor
    with the file moved aside produces that evidence inside the same gate output,
    which is the difference between an observable claim and a guess.

    The control must NOT touch the real artifacts: an earlier version re-ran into
    the same `figdir`, so the gate went on to judge the WITHOUT result and
    reported a FAIL for a run whose override works (the probe destroyed its own
    evidence).  The control therefore writes to a scratch dir of its own.
    """
    if not args.prove_override:
        return
    hit = resolve_figs_override(args.nn, orig, workdir, args.override,
                                args.root)
    live = [c for c in hit if c[2]]
    if not live:
        print('[6p] 证明模式：未找到任何 override，无可对照（WITHOUT 即本次结果）。')
        return
    print('[6p] 证明模式：把 override 全部移开后再抽一次（对照 WITHOUT）。')
    aside = os.path.join(os.path.abspath(workdir), '_ovprobe')
    shutil.rmtree(aside, ignore_errors=True)
    os.makedirs(aside)
    ctrl_dir = os.path.join(aside, 'figs')       # separate from the real figdir
    moved = []
    probe_cmd = list(cmd)
    try:
        for i, (path, kind, _ex) in enumerate(live):
            tmp_dst = os.path.join(aside, 'ov%d.json' % i)
            try:
                shutil.move(path, tmp_dst)
                moved.append((tmp_dst, path))
            except OSError as exc:
                print('[6p] WARN: 无法移开 %s（%s）-> 对照不完整。' % (path, exc))
        # `--override <missing>` would be a usage error, so the probe drops
        # --override and disables every automatic location instead.
        # `--allow-override-replace` goes too: with no override in play it is
        # inert, but leaving a replacement-enabling flag in the CONTROL argv
        # invites exactly the "the control proved nothing" failure this probe
        # exists to prevent, so the control is kept minimal and explicit.
        drop, skip = [], False
        for a in probe_cmd:
            if skip:
                skip = False
                continue
            if a == '--override':
                skip = True
                continue
            if a == '--allow-override-replace':
                continue
            drop.append(a)
        # Redirect the control's OUTPUT DIR to the scratch.  argv is
        # [python, extract_figs.py, ORIG.pdf, OUTDIR, ...] so the outdir is
        # index 3 -- NOT 2, which is the input PDF.  Clobbering index 2 made the
        # control run against a directory as its PDF and silently report "no
        # override" for the wrong reason.
        assert len(drop) > 3, 'unexpected extract_figs argv: %r' % (drop,)
        drop[3] = ctrl_dir
        probe_cmd = drop + ['--no-auto-override']
        rc, out, err = sh(cmd=probe_cmd)
        txt = out + err
        mm = re.search(r'MISSING-EXPECT: (\[[^\]]*\])', txt)
        miss = json.loads(mm.group(1)) if mm else None
        ovl = re.search(r'OVERRIDE USED=(\d+)', txt)
        got_ov = int(ovl.group(1)) if ovl else None
        # Self-check: the control is only meaningful if it really ran without an
        # override.  If it still reports one, the comparison proves nothing and
        # must be reported as inconclusive rather than as a result.
        if got_ov != 0:
            print('[6p] FIGS WITH=CONTROL-INVALID: 对照运行仍报告 '
                  'override_used=%s（移开失败？）-> 本次对照**不可采信**。'
                  % ('?' if got_ov is None else got_ov))
            return
        print('[6p] WITHOUT override: rc=%s override_used=%s missing=%s'
              % (rc, got_ov, miss if miss is not None else '?'))
        n_with = len(exp_figs)
        n_without = (n_with - len(miss)) if miss is not None else -1
        tag = 'WITHOUT=%d/%d' % (n_without, n_with)
        if miss is None:
            print('[6p] FIGS WITH=%d/%d %s => 对照失败（未读到 MISSING-EXPECT）'
                  % (n_with, n_with, tag))
        elif miss:
            print('[6p] FIGS WITH=%d/%d %s => override 是**承重**的'
                  % (n_with, n_with, tag))
        else:
            print('[6p] FIGS WITH=%d/%d %s => override 在这篇上**不承重**'
                  '（自动提取本来就够），可考虑删除该 override。'
                  % (n_with, n_with, tag))
    finally:
        for tmp_dst, path in moved:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                shutil.move(tmp_dst, path)
            except OSError as exc:
                print('[6p] ERROR: 还原 %s 失败：%s —— 请手工把它移回！' % (path, exc),
                      file=sys.stderr)
        shutil.rmtree(aside, ignore_errors=True)
        # The real figdir was never written by the control, so it still holds the
        # WITH-override artifacts the gate will judge.  Verify that, because a
        # silent overwrite is exactly the failure this guard exists to prevent.
        if not os.path.isfile(os.path.join(figdir, 'figs.json')):
            print('[6p] ERROR: 对照运行污染了正式 figs 目录（figs.json 丢失）'
                  '-> 重跑正式提取。', file=sys.stderr)
            sh(cmd)


# ---------------------------------------------------------------- 主流程
def run(args):
    require_python_dep()
    root = os.path.abspath(args.root)
    papers_dir = os.path.join(root, 'Paper')
    nn = str(args.nn).zfill(2)
    workdir = os.path.join(os.path.abspath(args.workdir or os.path.join(root, '.work', nn)))
    os.makedirs(workdir, exist_ok=True)
    fails = []
    summary = {'nn': nn, 'mode': None, 'workdir': workdir}

    # ---------- 0) 版式硬门控（约束 R2 的代码强制） ----------
    ledger_path = args.ledger or os.path.join(root, '.work', 'format-ledger.tsv')
    led = read_ledger(ledger_path)
    rec = led.get(nn)
    if args.mode:
        mode = args.mode
        if rec and rec.get('mode') and rec['mode'] != mode:
            print('[GATE] 版式与台账不符：台账=%s（原件 %s） vs 命令行=%s -> 拒绝执行'
                  % (rec['mode'], rec.get('layout', '?'), mode))
            print('       如确需变更，请先更新 %s（需视觉审计或用户确认）。' % ledger_path)
            return EXIT_USAGE
        if not rec and not args.allow_no_ledger:
            print('[GATE] 台账 %s 中无 #%s 的版式记录 -> 拒绝出稿。' % (ledger_path, nn))
            print('       修法：先做**目视**版式审计并登记台账；')
            print('             或用 --allow-no-ledger 显式承担「未审计」风险（不推荐）。')
            return EXIT_USAGE
    else:
        if not rec or not rec.get('mode'):
            print('[GATE] 台账 %s 中无 #%s 的版式记录 -> 拒绝出稿。' % (ledger_path, nn))
            print('       修法：① python scripts/detect_format.py <原件> 取候选；')
            print('             ② **渲染正文页目视确认**（机器判定只是候选，约束 R3）；')
            print('             ③ 登记台账第 8 列「采用模式」= %s 后重跑。'
                  % '|'.join(MODES))
            return EXIT_USAGE
        mode = rec['mode']
        print('[0] 版式取自台账：%s（原件 %s）' % (mode, rec.get('layout', '?')))
    if mode not in MODES:
        print('[GATE] 台账第 8 列的值 %r 非法，只能是 %s。'
              % (mode, ' / '.join(MODES)), file=sys.stderr)
        return EXIT_USAGE
    summary['mode'] = mode

    # ---------- 1) 定位译稿与原件 ----------
    mds = sorted(glob.glob(os.path.join(papers_dir, '%s_*_中文.md' % nn)))
    if not mds:
        mds = sorted(glob.glob(os.path.join(root, '%s_*_中文.md' % nn)))
    if not mds:
        print('[1] 找不到 #%s 的译稿（%s/%s_*_中文.md）。\n'
              '  修法：确认译稿命名形如 <NN>_<stub>_中文.md 并放在 Paper/ 下。' % (nn, papers_dir, nn),
              file=sys.stderr)
        return EXIT_USAGE
    md = mds[0]
    stub = os.path.basename(md)[len(nn) + 1:-len('_中文.md')]

    # Resolve the source PDF against `stub`.  First try the exact <stub>.pdf.
    # Then accept any PDF in Paper/ whose name *starts with* the stub (covers
    # the common case of extra suffixes like `_CC-BY`, `_v2`, `(final)` etc.)
    # -- and only ever look INSIDE Paper/.  If still nothing, fail loudly and
    # list the available PDFs so the user can rename in one line.
    def _resolve_orig(stub_):
        exact = os.path.join(papers_dir, stub_ + ".pdf")
        if os.path.isfile(exact):
            return exact
        prefix = os.path.join(papers_dir, stub_ + "*.pdf")
        matches = sorted(glob.glob(prefix))
        for m in matches:
            if re.match(r'^' + re.escape(stub_) + r'(\..*)?$', os.path.basename(m)):
                return m
        # Last resort: any PDF whose name contains the stub
        for f in sorted(os.listdir(papers_dir)):
            if f.lower().endswith('.pdf') and stub_.lower() in f.lower():
                return os.path.join(papers_dir, f)
        return None
    orig = _resolve_orig(stub)
    if orig is None:
        avail = ', '.join(sorted(f for f in os.listdir(papers_dir)
                                   if f.lower().endswith('.pdf'))) or '(none)'
        print('[1] 找不到 #%s 的原件 PDF（按译稿 stub=%r 在 Paper/ 下匹配）'
              % (nn, stub), file=sys.stderr)
        print('    Paper/ 下现有的 PDF: %s' % avail, file=sys.stderr)
        print('    修法：要么把原件重命名成 %r，要么改译稿的 stub 使其等于现有 PDF 的 basename（不含扩展名）。'
              % (stub + ".pdf"), file=sys.stderr)
        return EXIT_USAGE
    # Deliverable goes to <root>/out by default (configurable via --outdir).
    # It used to be written next to the source in Paper/, which surprised users
    # because the skill never said so.
    outdir = os.path.abspath(args.outdir or os.path.join(root, 'out'))
    os.makedirs(outdir, exist_ok=True)
    out_pdf = os.path.join(outdir, '%s_%s_中文.pdf' % (nn, stub))
    summary['md'] = os.path.basename(md)
    summary['stub'] = stub
    summary['outdir'] = outdir
    print('[1] paper=%s mode=%s md=%s orig=%s'
          % (nn, mode, os.path.basename(md), os.path.basename(orig)))
    print('[1b] 输出目录 = %s' % outdir)

    # ---------- 1c) 版式支持门控：不支持即**报错退出**，绝不静默降级 ----------
    # Order matters: the LEDGER wins (R3 visual audit is authoritative).  Only
    # when the ledger is silent do we run the detector, and even then it is used
    # solely to DECIDE WHETHER TO REFUSE -- never to pick a mode for the user.
    ncols, evidence = None, 'ledger-absent'
    if rec and rec.get('layout'):
        ncols = _cols_from_text(rec['layout'])
        evidence = 'ledger-column-3: %s' % rec['layout']
    if args.ncols:
        ncols, evidence = int(args.ncols), 'command-line --ncols'
    if ncols is None and os.path.isfile(orig):
        ncols, det = detect_columns(orig)
        evidence = 'auto-detect (%s)' % det
        if ncols is not None:
            print('[0b] 自动检测原件栏数 = %s [%s]（仅用于判定「是否支持」，不用于选模式）'
                  % (_layout_name(ncols), det))
    summary['ncols_detected'] = ncols
    if not args.allow_layout_downgrade:
        reason = unsupported_layout_reason(mode, ncols, evidence, nn, orig)
        if reason:
            print(reason, file=sys.stderr)
            summary['verdict'] = 'REFUSE'
            summary['refuse_reason'] = reason.split('\n')[0]
            print('SUMMARY JSON=%s' % json.dumps(summary, ensure_ascii=False, default=str))
            return EXIT_USAGE
    elif ncols is not None and MODE_COLS.get(mode) != ncols:
        print('[GATE] --allow-layout-downgrade 生效：原件 %s，改用 %s 出稿。\n'
              '       这是**已声明的降级**，必须在台账「备注」列留 DEVIATION 记录。'
              % (_layout_name(ncols), _layout_name(MODE_COLS.get(mode) or 0)))
        summary['layout_downgrade'] = '%s->%s' % (_layout_name(ncols), mode)

    src = open(md, encoding='utf-8').read()
    lines = src.split('\n')

    # ---------- 2) 元信息 -> 模板 ----------
    cn_title = lines[0].lstrip('# ').strip()
    en_title = ''
    for L in lines[1:8]:
        s = L.strip()
        if s.startswith('**') and s.endswith('**') and len(s) > 10:
            en_title = s.strip('*').strip()
            break
    rows = {}
    for L in lines[:60]:
        if L.startswith('|') and L.count('|') >= 3:
            parts = [p.strip() for p in L.strip().strip('|').split('|')]
            if len(parts) == 2 and parts[0] not in ('项', '---', ''):
                rows[parts[0]] = parts[1]

    def pick(*keys):
        for k in keys:
            for rk, rv in rows.items():
                if rk.startswith(k) or k in rk:
                    return re.sub(r'〔[^〕]*〕', '', rv).strip()
        return ''

    authors = re.sub(r'[¹²³⁴⁵*]+', '', pick('作者')).strip('、 ')
    authors = re.sub(r'[，,]+(?=、)', '', authors)
    affil = pick('单位')
    contact = pick('联系', '通讯作者', '通讯')
    dates = pick('日期', '收稿', '出版日期')
    source = pick('出处')
    doi = pick('DOI', '数字对象标识')
    info_bits = [x for x in [esc(affil), esc(contact)] if x]
    if doi:
        info_bits.append('DOI: ' + esc(doi))
    if dates:
        info_bits.append(esc(dates))
    if source:
        info_bits.insert(0, esc(source))
    meta = {'cn_title': cn_title, 'en_title': en_title, 'authors': authors,
            'info': '\\\\\n'.join(info_bits) or ' '}

    geom = load_geometry(args.geometry, nn, stub)
    # The abstract text is produced later (step 4), but build_template()
    # needs it up front so it can decide whether to typeset an abstract
    # environment at all.  Initialise it here; the real value overwrites.
    abstract_txt = ''
    fonts = {'latin': args.latin_font, 'cjk': args.cjk_font,
             'cjksans': args.cjk_sans_font, 'mono': args.mono_font}
    tpl, geo_line, geo_note = build_template(
        mode, geom, meta, fonts, abstract_tex=abstract_txt)
    with open(os.path.join(workdir, 'main.tex'), 'w', encoding='utf-8') as fh:
        fh.write(tpl)
    with open(os.path.join(workdir, 'tpl.tex'), 'w', encoding='utf-8') as fh:
        fh.write(tpl)
    if geom:
        W, H = geom['paper_pt']
        L, T, R, B = geom['margins_pt']
        print('[2] tpl ok 几何照原件 %gx%gpt 边距 L=%g T=%g R=%g B=%g（bp 写入）'
              % (W, H, L, T, R, B))
        summary['paper'] = '%gx%g' % (W, H)
        text_width = geom.get('text_width_pt') or geom.get('text_width')
    else:
        text_width = None
        print('[2] tpl ok **警告**：无 #%s 几何数据 -> 用模板默认纸张/边距。'
              '（必须在回报里标注，不得静默）' % nn)
        print('    修法：python scripts/detect_geometry.py "%s" --json %s'
              % (orig, os.path.join(root, '.work', args.nn, 'geometry.json')))
        print('    （注意：默认几何路径是 .work/<NN>/geometry.json，'
              '与 detect_geometry.py 的写法一致；不要写成无 NN 的 .work/geometry.json）')
        print('    （若你确实想让所有篇共用一份几何，也可显式传 '
              '--geometry .work\\geometry.json）')
        fails.append('geom-missing')
    summary['authors'] = authors[:40]

    # ---------- 3) 剥离译者信息 ----------
    clean = os.path.join(workdir, 'clean.md')
    rc, out, err = sh([sys.executable, os.path.join(_HERE, 'strip_meta.py'), md, clean])
    if rc != 0 or not os.path.isfile(clean):
        print('[3] strip 失败 rc=%d\n%s' % (rc, (out + err)[-600:]))
        fails.append('strip-failed')
        clean_txt = src
    else:
        clean_txt = open(clean, encoding='utf-8').read()
        dev = re.search(r'TRACE-HITS=(\d+)', out)
        print('[3] strip ok 残留译者痕迹=%s' % (dev.group(1) if dev else '?'))

    # ---------- 4) 拆摘要 / 关键词 / 正文 ----------
    m_kw = re.search(r'^[ \t>*]*\**\s*\**(关键词|索引词|关键字|主题词|Index Terms)', clean_txt, re.M)
    if not m_kw:
        kw_line = ''
        # No keyword anchor.  Split on the ABSTRACT heading instead: the old code
        # looked for the first `#{1,6}` heading and used it as the abstract/body
        # boundary, but `## 摘要（Abstract）` IS that first heading, so b0 became 0
        # and the ENTIRE abstract fell into the body (rendered as a \section*).
        # The deliverable then still showed the abstract text, which made the loss
        # look harmless -- but it was in the wrong environment and the ACM keyword
        # block that follows got the same treatment.
        # NOTE: never anchor with `$` -- these .md files are CRLF and Python's
        # `$` matches before '\\n' but AFTER '\\r', so a `...$` pattern can
        # never match a CRLF line (measured on #07).
        m_abs = re.search(r'^[ \t>*#{}\-\s]*\**\s*摘\s*要[^\r\n]*', clean_txt, re.M | re.I)
        if m_abs:
            # The abstract heading is usually at offset 0 (title is in the
            # translated .md's front matter).  Everything BEFORE the heading is
            # front matter; everything after it, up to the keyword line or the
            # first real section, is the abstract.
            _head_end = m_abs.end()
            _next = re.search(r'^#{1,6}[ \t]+\S', clean_txt[_head_end:], re.M)
            _body_start = _head_end + (_next.start() if _next else 0)
            abstract_txt = clean_txt[_head_end:_body_start].strip('\r\n ')
            body_txt = clean_txt[_body_start:]
            if abstract_txt:
                print('[4] 摘要锚点：命中「摘要/Abstract」标题行（无关键词锚点）')
        else:
            m_head = re.search(r'^#{1,6}[ \t]+\S', clean_txt, re.M)
            b0 = m_head.start() if m_head else 0
            abstract_txt, body_txt = clean_txt[:b0], clean_txt[b0:]
    else:
        ls = clean_txt.rfind('\n', 0, m_kw.start()) + 1
        le = clean_txt.find('\n', m_kw.end())
        le = len(clean_txt) if le < 0 else le
        kw_line = clean_txt[ls:le].strip()
        abstract_txt, body_txt = clean_txt[:ls], clean_txt[le:]
    abstract_txt = re.sub(r'^\s*#{1,6}\s*\**\s*摘要[^\n]*\n', '', abstract_txt, count=1).strip()
    body_txt = body_txt.strip()
    # ---- structure-aware override ---------------------------------------------
    # Decide whether this document HAS an abstract from the TRANSLATION's own
    # markdown structure -- not from a heuristic over the original PDF.
    # If clean.md has no 摘要/Abstract heading and no keyword anchor, the
    # translation simply has no abstract section, so do not invent one.
    #
    # Measured, both directions:
    #  * without any check, #28 (CACM Opinion, opens with a drop-cap lede and
    #    has no abstract) had its first 11 BODY paragraphs typeset as an
    #    "Abstract" the original never had.
    #  * a source-PDF heuristic (_source_has_abstract) wrongly stripped the
    #    real abstracts of #01/#07/#24/#25, so that approach was abandoned.
    _has_abs_heading = bool(re.search(
        r'^[ \t>*#{}\-\s]*\**\s*(\u6458\s*\u8981|Abstract)',
        clean_txt, re.M | re.I))
    if not kw_line and not _has_abs_heading:
        abstract_txt = ''
        body_txt = clean_txt
        print('[4] 译稿无摘要小节 -> 全部归正文，不建 abstract 环境')
    for name, txt in (('abstract.md', abstract_txt), ('keywords.md', kw_line),
                      ('body-clean.md', body_txt)):
        with open(os.path.join(workdir, name), 'w', encoding='utf-8') as fh:
            fh.write(txt + '\n')
    # Distinguish the two very different reasons the counter can be small.
    # A trial agent saw `abstract=0` and concluded -- and reported as a top
    # severity defect -- that the abstract was LOST, when in fact it was present
    # and only the keyword ANCHOR was missing.  Wording matters: this counter is
    # read as evidence by whoever is auditing the run.
    if kw_line:
        kw_note = kw_line[:30]
    elif abstract_txt:
        # an abstract exists but we could not anchor a keyword line
        kw_note = '(anchor-not-found)'
    else:
        kw_note = '(none)'
    print('[4] abstract=%d keywords=%s body=%d'
          % (len(abstract_txt), kw_note, len(body_txt)))
    if not abstract_txt:
        # main.tex unconditionally wraps \begin{abstract}\input{abstract.tex},
        # so an empty file compiles silently and the deliverable loses the whole
        # abstract.  Nothing else in the gate stack can see this, so check here.
        #
        # But an empty abstract is only a DEFECT when the source actually has
        # one.  Measured: #27 (an OMA technical specification) has no abstract in
        # its source at all, and must not be marked FAIL for that.
        _src_has = _source_has_abstract(orig)
        if _src_has:
            print('[4] **FAIL: abstract 为空** — 而原件**有**摘要 => 摘要被我们弄丢了。')
            print('    修法：检查译稿是否有「摘要/Abstract」小节；'
                  '或确认 step 4 的分割锚点（关键词行）是否存在。')
            fails.append('abstract-empty')
        else:
            print('[4] INFO: abstract 为空，且原件本身无摘要（规范类文档常见）'
                  ' — 不作为缺陷，模板会省略 abstract 环境。')

    # ---------- 4b/5) 期望表号与图号 ----------
    TAB_RE = re.compile(r'^\*{0,2}表\s*[:：]?\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*[（(]\s*TABLE',
                        re.M | re.I)
    TAB_RE2 = re.compile(r'^\s*>?\s*[（(]\s*表\s*[:：]\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s+', re.M)
    FIG_RE = re.compile(r'^>?\s*\*{0,2}图\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*'
                        r'(?:[（(][^）)]*[）)])?\s*[.。]?\s*\*{0,2}', re.M)

    def _norm(n):
        return re.sub(r'\.(?=-)', '', (n or '').strip().rstrip('.'))

    exp_tabs = sorted({_norm(m.group(1)) for m in TAB_RE.finditer(body_txt)} |
                      {_norm(m.group(1)) for m in TAB_RE2.finditer(body_txt)},
                      key=lambda x: (len(x), x))
    exp_figs = sorted({_norm(m.group(1)) for m in FIG_RE.finditer(body_txt)},
                      key=lambda x: (len(x), x))
    print('[4b] expected tables=%d figs=%d' % (len(exp_tabs), len(exp_figs)))

    # ---------- 5b) 表格矢量复刻（本地，PyMuPDF） ----------
    tabdir = os.path.join(workdir, 'tables')
    tj = os.path.join(tabdir, 'tables.json')
    n_tab_ok = 0
    if os.path.isfile(orig):
        cmd = [sys.executable, os.path.join(_HERE, 'extract_tables.py'), orig, tabdir]
        if exp_tabs:
            cmd += ['--expect', ','.join(exp_tabs)]
        rc, out, err = sh(cmd)
        if os.path.isfile(tj):
            with open(tj, encoding='utf-8') as fh:
                n_tab_ok = sum(1 for v in json.load(fh).values() if v.get('status') == 'ok')
        print('[5b] 矢量复刻表 %d/%d' % (n_tab_ok, len(exp_tabs)))
        if exp_tabs and n_tab_ok < len(exp_tabs):
            mm = re.search(r'MISSING: (\[[^\]]*\])', out)
            print('     缺表：%s' % (mm.group(1) if mm else '?'))
            print('     NEXT: 未复刻的表会退回**手排** LaTeX 表——'
                  '按用户要求这是不可接受的，须先修提取（见 references/tables.md）。')
            fails.append('tables-incomplete=%d/%d' % (n_tab_ok, len(exp_tabs)))
    else:
        print('[5b] 原件 %s 不存在 -> 跳过表格复刻（-pdf/--no-figures 场景）' % os.path.basename(orig))

    # ---------- 6) 原图提取 ----------
    figdir = os.path.join(workdir, 'figs')
    shutil.rmtree(figdir, ignore_errors=True)
    os.makedirs(figdir, exist_ok=True)
    fig_topo = {'auto': 0, 'override': 0, 'missing': list(exp_figs),
                'exp': list(exp_figs), 'source': 'none', 'path': '', 'by': 'none'}
    # Registered SOURCE GAPS: figures a human has verified are ABSENT from the
    # original PDF (caption present, artwork absent).  Kept in the tracked
    # location Paper/<NN>.figs_source_gaps.json so the exception is reviewable
    # and reproducible -- never inferred by code.
    fig_topo['source_gaps'] = {}
    # The tracked papers directory: same base resolve_figs_override() uses, so the
    # gap registry sits beside the override registry.
    _papdir = os.path.join(args.root, 'Paper')
    _gapf = os.path.join(_papdir, nn + '.figs_source_gaps.json')
    if os.path.isfile(_gapf):
        try:
            _gj = json.load(open(_gapf, encoding='utf-8'))
            fig_topo['source_gaps'] = _gj if isinstance(_gj, dict) else {}
            print('[6a] SOURCE-GAP 登记：%s（%d 条）'
                  % (os.path.basename(_gapf), len(fig_topo['source_gaps'])))
        except Exception as exc:
            print('     WARN: %s 解析失败，忽略：%s' % (_gapf, exc), file=sys.stderr)
    if os.path.isfile(orig) and exp_figs:
        cmd = [sys.executable, os.path.join(_HERE, 'extract_figs.py'), orig, figdir,
               '--expect', ','.join(exp_figs), '--nn', nn]
        if args.no_auto_override:
            cmd += ['--no-auto-override']
        if not args.dpi or args.dpi == 600:
            pass
        else:
            cmd += ['--dpi', str(args.dpi)]
        # ---- override：显式优先，路径与优先级全部打印（不再依赖隐式自动加载）----
        ov_hits = resolve_figs_override(args.nn, orig, workdir, args.override, args.root)
        ov_live = [c for c in ov_hits if c[2]]
        print('[6a] override 候选（显式优先；只生效第一份存在的）：')
        for path, kind, ex in ov_hits:
            print('     [%s] prio=%-4s %-14s %s'
                  % ('FOUND' if ex else 'absent',
                     '1' if kind == 'explicit' else 'auto', kind, path))
        if args.override and not os.path.isfile(os.path.abspath(args.override)):
            print('ERROR: --override 指定的文件不存在：%s' % args.override, file=sys.stderr)
            fails.append('override-missing:%s' % os.path.basename(args.override))
        if ov_live:
            ov_path, ov_kind = ov_live[0][0], ov_live[0][1]
            cmd += ['--override', ov_path]
            # An in-repo override may legitimately REPLACE an auto result rather
            # than merely fill a hole.  The regression guard in extract_figs.py
            # refuses that by default -- correctly, because a stale override that
            # shadows a good crop is a silent quality downgrade -- so the escape
            # hatch must be opted into explicitly.
            #
            # Without this the TEXT-AS-FIG class could not be repaired at all:
            # #09 fig6 and #11 fig5 were "successfully" auto-extracted (a crop
            # existed, was non-blank, and had the right filename), so an override
            # was the only way to put the REAL figure in place, and the guard
            # rejected it, leaving the paper with 12/13 figures instead of 13/13.
            # The guard still reports every replacement through
            # OVERRIDE USED=/FIGS TOPO=, and the counterfactual probe below
            # re-runs the extraction without the override, so the swap stays
            # visible and auditable instead of silent.
            cmd += ['--allow-override-replace']
            fig_topo.update({'source': 'has-override', 'path': ov_path, 'by': ov_kind})
            print('[6a] 本次使用 override：%s（来源 %s）' % (ov_path, ov_kind))
        elif args.no_auto_override:
            cmd += ['--no-auto-override']
            print('[6a] 无 override（--no-auto-override 已禁用自动查找）。')
        else:
            print('[6a] 无 override：候选位置都不存在 -> 全部走自动提取。')
        rc, out, err = sh(cmd)
        for _l in (out + err).split('\n'):
            if _l.startswith(('OVERRIDE ', 'OVERRIDE-REJECTED', 'FIGS TOPO', 'FIGS-SOURCE')):
                print('     | ' + _l[:200])
        fig_topo['extractor'] = _line_kv(out + err, 'OVERRIDE USED')
        # Counterfactual control: prove the override is load-bearing (or not).
        figure_override_probe(args, orig, workdir, figdir, exp_figs, cmd)
        mm = re.search(r'MISSING-EXPECT: (\[[^\]]*\])', out + err)
        missing_exp = json.loads(mm.group(1)) if mm else []
        nfigs = len(glob.glob(os.path.join(figdir, 'fig*.png')))
        # Distinguish a CRASH from a clean "some figures are missing" report.
        # extract_figs.py returns 1 when it simply reported missing figures
        # (``return EXIT_OK if not missing else 1``) -- that is a normal result,
        # already carried by MISSING-EXPECT, so treating it as a crash produced a
        # false fig-extract-crashed on 6 papers and polluted failure attribution.
        # A real crash returns >=2 (usage/dependency) or dies before printing the
        # SUMMARY line at all.
        saw_summary = 'SUMMARY EXTRACTED=' in (out + err)
        if rc >= 2 or (rc != 0 and not saw_summary):
            tail = (err or out or '').strip().split('\n')[-3:]
            print('     ERROR: extract_figs.py 退出码 %d 且未产出 SUMMARY —— 图提取未完成，'
                  '禁止当作「缺 无」。' % rc)
            for _l in tail:
                print('       | ' + _l[:160])
            fails.append('fig-extract-crashed:rc=%d' % rc)
        elif rc == 1 and not missing_exp:
            # rc=1 is documented as "missing figures", so with an empty
            # MISSING-EXPECT list the two disagree -> do not silently accept.
            print('     ERROR: extract_figs.py 退出码 1 但未报告缺图清单（口径矛盾）。')
            fails.append('fig-extract-inconsistent:rc=1')
        # ---- 三态门控：[6] 行必须写明 auto / override / missing ----
        # "FIGS=5/5" alone cannot be audited: it hides whether a human supplied a
        # bbox.  Derived from the ARTIFACTS (figs.json + fig*.png), not from the
        # extractor's own summary line, so the two can be cross-checked.
        _auto, _ov, _miss_disk, _exp = gate_figs_topology(fig_topo, figdir)
        if set(_miss_disk) != set(str(x) for x in missing_exp):
            # A CRASHED extractor prints no MISSING-EXPECT at all, so the disk
            # view differs from the (empty) report by construction.  Do not add a
            # second, confusing failure on top of fig-extract-crashed: say which
            # case this is.
            if not saw_summary:
                print('[6] NOTE: extractor 崩溃未产出摘要，缺图以**落盘产物**为准：%s'
                      % (_miss_disk or '无'))
            else:
                print('[6] WARN: 落盘复核的缺图 %s 与 extractor 报告 %s 不一致（以落盘为准）。'
                      % (_miss_disk, missing_exp))
                fails.append('fig-missing-mismatch:disk=%s/report=%s'
                             % (','.join(_miss_disk) or '-', ','.join(missing_exp) or '-'))
            missing_exp = _miss_disk
        fig_topo.update({'auto': _auto, 'override': _ov, 'missing': _miss_disk})
        print('[6] 提取图 %d，缺 %s  [FIGS=%d/%d (auto:%d override:%d) errs=%d]'
              % (nfigs, missing_exp or '无', _auto + _ov, len(exp_figs), _auto, _ov, rc))
        if _ov:
            print('     FIGS-SOURCE: %d/%d 张来自**人工 override**（%s）——'
                  '本次 PASS 的含义与全自动不同，必须按此记录。'
                  % (_ov, len(exp_figs), os.path.basename(fig_topo['path'] or '?')))
        if fig_topo.get('extractor') and fig_topo['extractor'] != _ov:
            # Self-check against a silent no-op: the extractor SAID it used N
            # overrides but the artifacts show a different number.
            print('     ERROR: extract_figs 报告 override 用量 %s，落盘复核为 %d（口径矛盾）。'
                  % (fig_topo['extractor'], _ov))
            fails.append('override-count-mismatch:report=%s/disk=%d'
                         % (fig_topo['extractor'], _ov))
        if missing_exp:
            # SOURCE-GAP: a human has目视确认 that the ORIGINAL PDF simply does
            # not contain this figure (the caption exists, the artwork does not).
            # WHY this is separate from `missing-figs`: "we failed to extract it"
            # and "it is not in the source" need different actions -- fix code vs
            # obtain material from the author.  Collapsing both into one failure
            # hid that distinction (measured on #17: page 3 carries the Figure 2
            # caption with NO artwork anywhere near it, while Figure 3 on the same
            # page has a full diagram).
            #
            # A source gap must be REGISTERED, never inferred: it is a human claim
            # like an override, so an unregistered gap stays a hard FAIL.
            _gaps = fig_topo.get('source_gaps') or {}
            _gap_hit = sorted(set(missing_exp) & set(str(k) for k in _gaps))
            _still = [m for m in missing_exp if str(m) not in _gap_hit]
            if _gap_hit:
                for _g in _gap_hit:
                    print('     SOURCE-GAP: 图 %s 在**原件中不存在**（已登记）：%s'
                          % (_g, _gaps.get(_g, _gaps.get(str(_g), ''))[:70]))
                print('     ^ 该缺图不作为失败；这是**源文档缺陷**，不是提取失败。'
                      '登记位置：Paper/%s.figs_source_gaps.json' % nn)
                fig_topo['source_gap'] = _gap_hit
            if _still:
                print('     NEXT: 写 figs_override.json 人工给 bbox（格式见 extract_figs.py --help），'
                      '**放进仓库可追踪的位置 Paper/%s.figs_override.json**，再重跑；'
                      '正文图题不在文本层时必须走这条。' % nn)
                print('     （若该图在原件中根本不存在，请**目视确认后**登记 '
                      'Paper/%s.figs_source_gaps.json，而不是凭空造图。）' % nn)
                fails.append('missing-figs:' + ','.join(_still))
    else:
        nfigs = 0
        print('[6] 跳过图提取（无原件或无期望图号）')

    # ---------- 7) prep ----------
    prep_out = ''
    for name, outp in (('abstract', 'abstract.md'), ('keywords', 'keywords.md'),
                       ('body-clean', 'body.md')):
        cmd = [sys.executable, os.path.join(_HERE, 'prep.py'),
               os.path.join(workdir, name + '.md'), os.path.join(workdir, outp),
               '--figs', figdir, '--tables', tabdir, '--mode', mode]
        if text_width:
            cmd += ['--text-width', str(text_width)]
        rc, out, err = sh(cmd)
        if name == 'body-clean':
            prep_out = out + err
        if rc != 0:
            print('[7] prep %s 失败 rc=%d\n%s' % (name, rc, (out + err)[-500:]))
    body_md = os.path.join(workdir, 'body.md')
    body_tex = open(body_md, encoding='utf-8').read() if os.path.isfile(body_md) else ''
    n_env = (body_tex.count(r'\begin{figure}') + body_tex.count(r'\begin{figure*}')
             + body_tex.count(r'\captionof{figure}'))
    n_tabimg = body_tex.count(r'\includegraphics')
    # A REGISTERED source gap reduces the number of figure environments the
    # document can possibly contain: the caption is translated but there is no
    # artwork to place.  Comparing against the raw expectation would keep the run
    # failing forever on a defect that is not ours (measured on #17: 8/9).
    _n_gap = len(fig_topo.get('source_gap') or [])
    _exp_env = len(exp_figs) - _n_gap
    print('[7] prep: figenvs=%d/%d includegraphics=%d%s' % (
        n_env, _exp_env, n_tabimg,
        '  (期望数已扣除 %d 个已登记 SOURCE-GAP)' % _n_gap if _n_gap else ''))
    if exp_figs and n_env != _exp_env:
        fails.append('figenv-count=%d/%d' % (n_env, _exp_env))

    # ---------- 7b) R1 门控：矢量表不得静默退回手排 ----------
    # 判据（**分两类**，因为并非所有手写表都是错的）：
    #   · 该表号在 tables.json 里有 ok 记录，却没走矢量嵌入  -> **失败**
    #     （矢量复刻件确实存在，却没用上；这是表号归一化/表体跳过的缺陷）
    #   · tables.json 里根本没有该表号                       -> 允许手写，但打印 INFO
    #     （如 DESIRE6G 的 3 张 no-rules 表，原件本就无法复刻）
    # 真实事故：罗马/阿拉伯表号不归一化，使 7 张矢量表**静默退回**手排，
    # 管线只打印 NOTE 却仍报 7/7 成功并出稿。所以这里必须是门控，不是提示。
    _hm = re.search(r'HANDMADE_DESPITE_OK=(\S+)\s+HANDMADE_NO_RECORD=(\S+)', prep_out)
    hm_ok = []
    hm_no = []
    if _hm:
        hm_ok = [x for x in _hm.group(1).split(',') if x and x != '-']
        hm_no = [x for x in _hm.group(2).split(',') if x and x != '-']
    if hm_ok:
        if args.allow_handmade_tables:
            print('[7b] WARNING: 表 %s 有矢量复刻件却走手排；'
                  '--allow-handmade-tables 已显式放行（违反 R1，须在台账留 DEVIATION）。'
                  % ','.join(hm_ok))
            summary['handmade_allowed'] = ','.join(hm_ok)
        else:
            print('[7b] R1 门控失败：表 %s 在 tables.json 里有 ok 记录，'
                  '却**没有**走矢量嵌入（静默退回手排）。' % ','.join(hm_ok))
            print('     NEXT: 查表号归一化（罗马/阿拉伯，见 prep.py::_norm_tabno）'
                  '与表体跳过逻辑；references/tables.md 有三态判定。')
            print('     确属原件无法复刻时才可显式 --allow-handmade-tables（会在交付件留违规记录）。')
            fails.append('handmade-despite-ok:' + ','.join(hm_ok))
    if hm_no:
        print('[7b] INFO: 表 %s 无 tables.json 记录 -> 允许手排'
              '（原件不可复刻；须目视确认表区内容正确）。' % ','.join(hm_no))
    if not _hm and exp_tabs and body_tex.count(r'\begin{tabularx}') > 0:
        # prep 的输出格式变了或没跑起来：**不得静默放行**（同 G8 的教训）。
        print('[7b] R1 门控无法从 prep 输出判定手排分类 -> 按失败处理。\n'
              '     NEXT: 确认 prep.py 打印了 HANDMADE 行（版本不匹配？）。')
        fails.append('handmade-classification-unreadable')

    # ---------- 7c) R1 独立复核：不依赖 prep 的 stdout ----------
    # 7b 依赖 prep 打印的 HANDMADE 行。为了不让「输出格式变了」变成放行漏洞，
    # 这里**独立**再数一遍：tables.json 里 status==ok 的记录数（矢量复刻件数）
    # 与 body.tex 里真正嵌入的表格图数（图目录之外的 \includegraphics）。
    # 这是与 7b 口径不同的第二条独立判据（同 G4/G5 的「换抽取口径复核」思路）。
    if tj and os.path.isfile(tj):
        try:
            _tinfo = json.load(open(tj, encoding='utf-8'))
        except Exception:
            _tinfo = {}
        _n_ok_files = 0
        for _k, _v in _tinfo.items():
            if isinstance(_v, dict) and _v.get('status') == 'ok' and _v.get('file'):
                try:
                    if os.path.isfile(os.path.join(tabdir, os.path.basename(_v['file']))):
                        _n_ok_files += 1
                except Exception:
                    pass
        # 表格图与插图都走 \includegraphics，但插图在 figN.png 命名下且数量已知；
        # 用「总数 - 图数」得到表格图数（保守：只在下界判定）。
        _tab_graphics = max(0, n_tabimg - nfigs) if nfigs else 0
        if _n_ok_files and _tab_graphics < _n_ok_files:
            print('[7c] R1 复核：tables.json 有 %d 张可用的矢量复刻件，'
                  'body.tex 里只嵌入约 %d 张表格图 -> 有 %d 张未走矢量嵌入。'
                  % (_n_ok_files, _tab_graphics, _n_ok_files - _tab_graphics))
            print('     NEXT: 查表号归一化与表体跳过逻辑（references/tables.md）。')
            if not args.allow_handmade_tables:
                fails.append('vector-tables-unused=%d/%d'
                             % (_tab_graphics, _n_ok_files))
            else:
                print('     --allow-handmade-tables 已显式放行（须在台账留 DEVIATION）。')
        elif _n_ok_files:
            print('[7c] R1 复核通过：矢量复刻件 %d 张，body.tex 表格图约 %d 张。'
                  % (_n_ok_files, _tab_graphics))

    # ---------- 7d) 重建模板（必须在摘要分割之后） ----------
    # THE ORDER MATTERS.  `_abstract_env()` emits the abstract environment only
    # when the abstract text is non-empty, but build_template() used to run in
    # step 2 -- BEFORE step 4 computed the split.  So the template always saw ''
    # and never emitted \input{abstract.tex}: everything step 4 wrote into
    # abstract.tex was silently dropped from the deliverable.
    # Measured blast radius of that ordering bug: 27 of 28 papers lost their
    # abstract (22,796 chars total) and EVERY gate stayed green.  So re-render
    # the template here, where the real text is known, and rewrite main.tex.
    try:
        tpl2, geo_line2, geo_note2 = build_template(
            mode, geom, meta, fonts, abstract_tex=abstract_txt)
        if abstract_txt.strip():
            print('[7d] 模板重建：摘要 %d 字符已装入 '
                  'abstract 环境' % len(abstract_txt.strip()))
        else:
            print('[7d] 模板重建：无摘要，'
                  '不排 abstract 环境')
        with open(os.path.join(workdir, 'main.tex'), 'w', encoding='utf-8') as fh:
            fh.write(tpl2)
        with open(os.path.join(workdir, 'tpl.tex'), 'w', encoding='utf-8') as fh:
            fh.write(tpl2)
        geo_line, geo_note = geo_line2, geo_note2
    except Exception as _exc:
        print('[7d] WARN: 模板重建失败（保留 step 2 版本）：%s'
              % _exc)

    # ---------- 8) 编译（后端抽象） ----------
    be = _backend.backend_from_args(args)
    print('[8] 后端=%s' % be.name)
    if args.pandoc:
        pandoc = args.pandoc
    else:
        pandoc = shutil.which('pandoc')
    if not pandoc:
        print('ERROR: ' + INSTALL['pandoc'], file=sys.stderr)
        fails.append('pandoc-missing')
        res = None
    else:
        for nm in ('abstract', 'keywords', 'body'):
            src_md = os.path.join(workdir, nm + '.md')
            dst_tex = os.path.join(workdir, nm + '.tex')
            rc, out, err = sh([pandoc, src_md, '-f', 'markdown+raw_tex', '-t', 'latex',
                               '--no-highlight', '-o', dst_tex])
            if rc != 0:
                print('ERROR: pandoc %s 失败：%s' % (nm, (err or out)[-400:]), file=sys.stderr)
                fails.append('pandoc-%s' % nm)
        # 译文自带章节编号 -> 用 starred 版本，防止 LaTeX 自动编号叠加
        btex = os.path.join(workdir, 'body.tex')
        if os.path.isfile(btex):
            s = open(btex, encoding='utf-8').read()
            s = s.replace(r'\subsubsection{', r'\subsection*{')
            s = s.replace(r'\subsection{', r'\section*{')
            with open(btex, 'w', encoding='utf-8') as fh:
                fh.write(s)
        # 用后端编译；主输入恒为 main.tex，因此日志恒为 <workdir>/main.log
        try:
            res = be.compile(workdir)
        except _backend.BackendError as exc:
            print('ERROR: 编译后端失败：\n%s' % exc, file=sys.stderr)
            fails.append('backend-error')
            res = None
        if res is not None:
            print('[8] compile ok=%s pdf=%s rc=%s'
                  % (res.ok, os.path.basename(res.pdf) if res.pdf else '-', res.returncode))
            if res.note:
                print('     NOTE: %s' % res.note)

    # ---------- 9) 门控 ----------
    g = {}
    traces = n_fig_pdf = n_tab_pdf = -1
    outpdf = os.path.join(workdir, 'main.pdf')
    if res is not None:
        try:
            log_text = be.read_log(workdir)
        except _backend.BackendError as exc:
            # **绝不静默按 0 放行**：读不到日志 = 门控失效 = 判失败
            print('ERROR: %s' % exc, file=sys.stderr)
            fails.append('gate-log-unreadable')
            log_text = ''
        if log_text:
            g = parse_gates(log_text)
            if g['ERRS']:
                fails.append('ERRS=%d' % g['ERRS'])
            if g['MISS']:
                fails.append('MISS=%d' % g['MISS'])
            if g['OVF_H_MAX'] > TH_OVF_H:
                fails.append('OVF_H=%.1f' % g['OVF_H_MAX'])
            if g['VBOX_MAX'] > TH_OVF_V:
                fails.append('VBOX=%.1f' % g['VBOX_MAX'])
        if os.path.isfile(outpdf):
            n_fig_pdf, anomalies, missing_fig = gate_figs(outpdf, exp_figs, workdir)
            if anomalies:
                fails.append('fig-anomaly:' + ','.join(anomalies))
            if missing_fig:
                fails.append('fig-closure-missing:' + ','.join(missing_fig[:6]))
            n_tab_pdf = count_tables_pdf(outpdf)
            # 译者痕迹：**优先用本地 PyMuPDF 重抽文本层**
            # 远端 pdftotext -layout 会被水印/双栏交错打乱，把图号拆到两行 -> 假失败。
            # 教训：放宽正则前先查「是不是抽取口径不同」；应选可靠的那个。
            try:
                import fitz
                doc = fitz.open(outpdf)
                t = '\n'.join(p.get_text() for p in doc)
                doc.close()
            except Exception as exc:
                print('[9w] 本地文本重抽失败：%s' % exc)
                t = ''
            traces = len(TRACE_RE.findall(t))
            if traces:
                fails.append('translator-traces=%d' % traces)
            if not anomalies and not missing_fig and n_fig_pdf == 0 and exp_figs:
                fails.append('fig-text-unreadable')
            # G9: no build date (environment leakage) in the delivery
            date_hits, _date_pats = gate_build_date(outpdf)
            if date_hits is None:
                print('[9w] G9 构建日期检查无法读取 PDF：%s' % _date_pats[0])
            elif date_hits:
                print('[9c] G9 交付件含**构建日期** %s -> 失败。\n'
                      '     NEXT: 模板必须写 \\date{}（\\maketitle 默认 \\today 会把'
                      '编译当天日期打进标题块）；论文自身的出版日期应放进信息行。'
                      % '; '.join(date_hits))
                fails.append('build-date-leak:%s' % date_hits[0])
        else:
            fails.append('no-output-pdf')
    # ---- 图编号来源三态（auto / override / missing）----
    # `FIGS=5/5` cannot be audited on its own: it reads identically whether the
    # algorithm found all five figures or a human supplied one bbox.  The topo
    # string below makes the difference visible in the gate line itself.
    _tp = fig_topo
    if _tp.get('exp'):
        topo = ('auto' if not _tp.get('override')
                else 'override:%d' % _tp['override'])
        figs_str = '%s/%s (auto:%d override:%d%s)' % (
            _tp.get('auto', 0) + _tp.get('override', 0), len(_tp['exp']),
            _tp.get('auto', 0), _tp.get('override', 0),
            '' if not _tp.get('missing') else ' missing:%s' % ','.join(_tp['missing']))
    else:
        topo, figs_str = 'n/a', '%s/%s' % (n_fig_pdf, len(exp_figs))
    print('[9] GATE ERRS=%s MISS=%s OVF_H_MAX=%s VBOX_MAX=%s PAGES=%s TRACES=%s '
          'FIGS=%s TABLES=%s FIGS-TOPO=%s'
          % (g.get('ERRS', '?'), g.get('MISS', '?'), g.get('OVF_H_MAX', '?'),
             g.get('VBOX_MAX', '?'), g.get('PAGES', '?'), traces,
             figs_str, n_tab_pdf, topo))
    if _tp.get('override'):
        print('[9f] 图来源：auto=%d override=%d (来源 %s%s)'
              % (_tp.get('auto', 0), _tp['override'],
                 _tp.get('by', '?'), (': ' + _tp['path']) if _tp.get('path') else ''))
        print('     **本次 PASS 依赖人工 override**（%d 张）。'
              'override 是人工判读，不是算法结果；复核者必须打开 %s 逐框核对。'
              % (_tp['override'], _tp.get('path') or '?'))

    # ---------- 9c) CONTENT COVERAGE (G11) ----------
    # "Did the text we were supposed to deliver actually make it into the PDF?"
    # Nothing else in the gate stack asks this question, and the answer was "no"
    # for 27 of 28 papers without a single gate complaining.
    if os.path.isfile(outpdf):
        _cov = _content_coverage(clean, outpdf)
        if _cov is None:
            print('[9c] G11 content-coverage: SKIP (无可比对的正文样本)')
        else:
            _ratio, _runs, _miss, _sample = _cov
            print('[9c] G11 content-coverage=%.3f (runs=%d missing_chars=%d)'
                  % (_ratio, _runs, _miss))
            if _ratio < 0.85:
                print('     **FAIL: 交付件缺少大段正文**（源 md 的内容只有 %.1f%% 进入 PDF）'
                      % (_ratio * 100))
                for _s in _sample:
                    print('       缺失样本：%s…' % _s)
                print('    修法：先确认 main.tex 里是否漏了某个 \\input{...}'
                      '（本项目的真实案例：模板在摘要分割之前构建，'
                      '导致 abstract.tex 从未被 \\input）。')
                fails.append('content-coverage=%.3f' % _ratio)

    # ---------- 10) 产出 ----------
    verdict = 'FAIL' if fails else 'PASS'
    if not fails and os.path.isfile(outpdf):
        # 走台账/几何路径时也要核对实际页尺寸（G6）
        if geom:
            try:
                import fitz
                d = fitz.open(outpdf)
                W = round(d[0].rect.width); H = round(d[0].rect.height)
                sizes = {}
                for pg in d:
                    k = (round(pg.rect.width), round(pg.rect.height))
                    sizes[k] = sizes.get(k, 0) + 1
                d.close()
                W, H = max(sizes, key=sizes.get)
                lw, lh = geom['paper_pt']
                dev = max(abs(W - lw), abs(H - lh))
                summary['paper_actual'] = '%dx%d' % (W, H)
                summary['paper_dev'] = dev
                if dev > 3:
                    print('[9b] 页面尺寸 %dx%d 与几何台账 %gx%g 偏差 %gpt > 3pt -> 失败。\n'
                          '     NEXT: 先查 geometry 单位是不是用了 pt（必须 bp）。'
                          % (W, H, lw, lh, dev))
                    fails.append('paper-dev=%g' % dev)
            except Exception as exc:
                print('[9w] 页面尺寸核对失败：%s' % exc)
        try:
            tmp = out_pdf + '.tmp'
            shutil.copyfile(outpdf, tmp)
            os.replace(tmp, out_pdf)
            print('[10] GATE-PASS%s -> %s'
                  % ('' if topo == 'auto' else '  [FIGS-TOPO=%s]' % topo,
                     os.path.basename(out_pdf)))
            if topo != 'auto' and topo != 'n/a':
                print('[10] 注：本次通过**依赖人工 override**（%s，%s）。'
                      '「PASS」仅表示图号闭合与门控指标达标，'
                      '不表示人工给的 bbox 已被复核。'
                      % (topo, _tp.get('path') or '?'))
        except PermissionError:
            alt = out_pdf[:-4] + '-new.pdf'
            shutil.copyfile(outpdf, alt)
            print('[10] GATE-PASS%s（目标被占用，已写 %s；关闭阅读器后重跑即可转正）'
                  % ('' if topo == 'auto' else '  [FIGS-TOPO=%s]' % topo,
                     os.path.basename(alt)))
            summary['output'] = alt
        except Exception as exc:
            fails.append('copy-failed:%s' % type(exc).__name__)
    if fails:
        verdict = 'FAIL'
        print('[10] GATE-FAIL: %s' % '; '.join(fails)[:400])
    elif 'output' not in summary:
        summary['output'] = out_pdf

    summary.update({'verdict': verdict, 'gates': g, 'traces': traces,
                    'figs_pdf': n_fig_pdf, 'exp_figs': len(exp_figs),
                    'figs_topology': topo, 'figs_auto': _tp.get('auto', 0),
                    'figs_override': _tp.get('override', 0),
                    'figs_override_path': _tp.get('path', ''),
                    'figs_override_by': _tp.get('by', 'none'),
                    'figs_missing': _tp.get('missing', []),
                    'tab_pdf': n_tab_pdf, 'exp_tabs': len(exp_tabs),
                    'tables_vector': n_tab_ok, 'figs_extracted': nfigs,
                    'fails': fails, 'backend': be.name, 'geom_note': geo_note})
    print('SUMMARY JSON=%s' % json.dumps(summary, ensure_ascii=False, default=str))
    return EXIT_OK if verdict == 'PASS' else EXIT_FAIL


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='runpaper.py',
        description='单篇编排：台账门控 -> 几何 -> 模板 -> 剥离 -> 拆段 -> 图/表提取 -> prep '
                    '-> 编译 -> 门控 -> 产出 PDF。',
        epilog='示例：\n'
               '  python scripts/runpaper.py 20 --ledger .work/format-ledger.tsv\n'
               '  python scripts/runpaper.py 20 --mode conf --allow-no-ledger\n'
               '  python scripts/runpaper.py S1 --mode tri --outdir out\n'
               '  python scripts/runpaper.py 20 --backend ssh-xelatex --ssh-host me@box:2222\n\n'
               '版式门控：省略 --mode 从台账取；与台账冲突即拒绝（exit 2）；无记录也拒绝。\n'
               '台账 TSV 第 8 列 = 采用模式（conf/access/tri）。\n'
               '不支持的版式（如 4 栏以上）一律拒绝出稿（exit 2），绝不静默降级；\n'
               '确需降级时必须显式加 --allow-layout-downgrade 并在台账留 DEVIATION。\n'
               '交付 PDF 默认写到 <root>/out/（可用 --outdir 改）。\n\n'
               '人工图 bbox（figs_override.json）：\n'
               '  规范位置 = Paper/<NN>.figs_override.json（本地校准规则，与译稿同目录），\n'
               '  旧位置 .work/<NN>/figs_override.json 仍兼容（临时构建目录，易丢失）。\n'
               '  门控会打印 [6a] 候选与命中项、[6]/[9] 的 auto/override 计数与 FIGS-TOPO；\n'
               '  用了 override 的 PASS 会在 [10] 行显式标注。\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('nn', metavar='NN', help='篇号（如 20；会补零为两位）')
    ap.add_argument('--root', default=DEFAULT_ROOT, metavar='DIR',
                    help='工作区根目录（含 Paper/ 与 .work/）。默认按 PAPER_SKILL_ROOT '
                         '> 最近的含 Paper/ 的祖先 > 最近的含 .git 的祖先 > skill 目录 '
                         '的顺序解析（见 _workspace.py）')
    ap.add_argument('--workdir', default=None, metavar='DIR',
                    help='工作目录（默认 <root>/.work/<NN>）')
    ap.add_argument('--mode', choices=list(MODES), default=None,
                    help='版式模式：access(1栏)/conf(2栏)/tri(3栏)；省略则从台账取，与台账冲突即拒绝')
    ap.add_argument('--outdir', default=None, metavar='DIR',
                    help='交付 PDF 输出目录（默认 <root>/out）')
    ap.add_argument('--ncols', type=int, default=None, metavar='N',
                    help='显式声明原件栏数，覆盖自动检测（仅影响「是否支持」判定）')
    ap.add_argument('--allow-layout-downgrade', action='store_true',
                    help='显式接受降级出稿（例：3 栏原件用单栏出）。'
                         '默认**不降级**：不支持的版式一律拒绝出稿并 exit 2')
    ap.add_argument('--ledger', default=None, metavar='TSV',
                    help='版式台账（默认 <root>/.work/format-ledger.tsv）')
    ap.add_argument('--geometry', default=None, metavar='JSON',
                    help='几何 JSON（默认 <root>/.work/geometry.json）')
    ap.add_argument('--allow-no-ledger', action='store_true',
                    help='台账无该篇记录时仍继续（违反 R3，**不推荐**）')
    ap.add_argument('--allow-handmade-tables', action='store_true',
                    help='允许「tables.json 有 ok 记录却未走矢量嵌入」的表以手排 LaTeX 出稿。'
                         '默认**拒绝**（门控失败）：矢量复刻件存在却没用上是缺陷——'
                         '静默退回手排曾导致 7 张表全部丢矢量保真却仍报成功。'
                         '仅当已确认原件该表确实无法复刻时才可显式放行。')
    ap.add_argument('--dpi', type=int, default=600, help='图区域渲染 dpi（默认 600）')
    ap.add_argument('--override', default=None, metavar='JSON',
                    help='人工 bbox override 文件（显式路径，传给 extract_figs.py --override）。'
                         '省略时按顺序自动查找并**打印命中项**：'
                         'Paper/<NN>.figs_override.json（规范位置，与译稿同目录）> '
                         'Paper/<stub>.figs_override.json > '
                         '<workdir>/figs_override.json（旧位置）> '
                         '<workdir>.figs_override.json。'
                         '指定的文件不存在即判失败。')
    ap.add_argument('--no-auto-override', action='store_true',
                    help='禁用 override 的自动查找（只认 --override）。'
                         '用于确认「不依赖任何人工 bbox」的干净复现。')
    ap.add_argument('--prove-override', action='store_true',
                    help='证明模式：把 override 临时移开再抽一次图（不改动仓库），'
                         '于是门控能打印 FIGS WITH/WITHOUT 对照——'
                         '用来区分「override 承重」与「override 只是躺着」。')
    ap.add_argument('--pandoc', metavar='PATH', help='pandoc 可执行文件路径')
    ap.add_argument('--latin-font', default='Liberation Serif', help='西文字体')
    ap.add_argument('--cjk-font', default='Noto Serif SC', help='中文字体（正文）')
    ap.add_argument('--cjk-sans-font', default='Noto Sans SC', help='中文无衬线字体')
    ap.add_argument('--mono-font', default='DejaVu Sans Mono', help='等宽字体')
    _backend.add_backend_args(ap)
    args = ap.parse_args(argv)
    if args.geometry is None:
        # Per-paper geometry is the canonical location (.work/<NN>/geometry.json,
        # exactly what detect_geometry.py writes in SKILL.md's own example).
        # The NN-less .work/geometry.json is still accepted as a fallback, but
        # the DEFAULT must not be a path that the documented sequence never
        # creates -- that mismatch is what produced `geom-missing` during the
        # fresh-agent trials (measured).
        per_nn = os.path.join(args.root, '.work', args.nn, 'geometry.json')
        flat = os.path.join(args.root, '.work', 'geometry.json')
        if os.path.isfile(per_nn):
            args.geometry = per_nn
        elif os.path.isfile(flat):
            args.geometry = flat
        else:
            # Neither exists: report the path the agent should have created.
            args.geometry = per_nn
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
