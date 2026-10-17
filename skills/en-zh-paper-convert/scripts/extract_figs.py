#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract_figs.py -- 从原件按「图题锚定 + 图形簇聚类」提取插图。

绝不整页截图（v1 的 2088x1712 整页裁剪把 Algorithm 1/正文段落当成插图，
是早期全库返工的根因）。流程：

    A. 字符级、**分栏**重建文本行（原 PDF 常无空格字符，左右栏同高会被并成一行）
    B. 图题识别两遍：严格（Fig. N. / FIGURE 1 独占行）优先，宽松（粘连文本）兜底
    C. 图形元素（images+rects+lines+curves）并查集聚类 -> 图题**代价匹配**
       cost = 垂直距离 + 0.5 x 水平间隙   （侧栏图题必需：该刊图题在图左侧窄栏、
       与图垂直重叠，v1 的「横向重叠」判据全部落空）
    D. 单张完整位图 -> **原生无损分流**（pdfimages，逐像素等价）；其余 -> 600 dpi 区域渲染
    E. 无图形簇 -> 图题旁空白带（VECTORBAND）；仍失败 -> figs_override.json 人工给定 bbox

依赖
    Python 3.9+ 与 PyMuPDF：pip install pymupdf
    可选外部工具（缺失不影响运行，只影响保真度/能力）：
      pdfimages  (poppler)  —— 原生位图无损分流；缺失则全部走区域渲染
      pdftoppm   (poppler)  —— 区域渲染**首选**后端；缺失自动回退 PyMuPDF 渲染
                               （--native-only 则完全跳过区域渲染，不要求 poppler）
      qpdf                  —— xref 损坏的原件先归一化；缺失则跳过
    Windows 获取 poppler：winget install oschwartz10612.Poppler
    （安装后确认 pdfimages.exe / pdftoppm.exe 在 PATH 里）

用法
    python scripts/extract_figs.py orig.pdf .work/01/figs --expect 1,2,3
    python scripts/extract_figs.py orig.pdf .work/01/figs --expect 1,2 --override Paper/01.figs_override.json
    python scripts/extract_figs.py orig.pdf out --list-captions        # 只列出识别到的图题
    python scripts/extract_figs.py orig.pdf out --expect 1 --no-auto-override

结构化输出
    fig1  p3   NATIVE     cost=12.4  els=1  imgs=1  crop=460x213pt
    CAPTIONS-IN-PDF: {'1': 1, '2': 2}
    MAPPED: ['1', '2']
    UNMATCHED: []
    MISSING-EXPECT: []
    SUMMARY EXTRACTED=2 EXPECTED=2 MISSING=0 NATIVE=1 RENDERED=1
    OVERRIDE USED=1 BY=explicit PATH=Paper/01.figs_override.json LOADED=1 ENTRIES=1
    FIGS TOPO=override:1 vs_auto=4/4 verified=5/5
    FIGS JSON={...}            # 与 figs.json 同内容，便于 agent 直接读

figs_override.json 格式（人工定位兜底，正文图题不在文本层时用）
    {"4": {"page": 12, "box": [x0, top, x1, bottom], "dpi": 600}}
    以 `_` 开头的键是**文件元信息**（如 "_README"/"_why"），不作为图号解析，
    因此可在文件头写清「这个框从哪来、谁复核过」。

override 文件查找顺序（**显式优先**，找到几份就打印几份，只生效最高优先级的那份）
    1. --override <PATH>                        命令行显式指定
    2. <outdir>/../<NN>.figs_override.json      .work/16/figs -> .work/16.figs_override.json
    3. <outdir>/../figs_override.json           旧位置（.work/<NN>/figs_override.json）
    4. <原件目录>/<原件名>.figs_override.json     Paper/<stub>.pdf -> Paper/<stub>.figs_override.json
    5. <原件目录>/<NN>.figs_override.json         Paper/<NN>.figs_override.json  <- **规范位置**
    6. <原件目录>/figs_override.json
    **自动发现的只有 2-6**；显式给的第 1 项若文件不存在 → 报错退出（不静默忽略）。
    规范位置是第 5 项：Paper/<NN>.figs_override.json（本地校准规则，与译稿同目录）。
    注意：`--expect` 只约束**自动提取**的图号；override 条目一旦给出即生效
    （第 3、6 项旧位置的文件若含 expect 之外的条目，会自动收窄为 expect）。
    禁用全部自动发现：--no-auto-override。

override 自校验（不合规即拒绝该条，绝不静默出空白图）
    · 路径必须相对本文件所在目录解析（不再依赖 CWD；无 / 前缀的老文件按相对值处理）
    · page 必须在 [1, 原件页数] 内；box 必须是 4 个有限数且 x0<x1、top<bottom
    · box 必须落在页面矩形内
    · 渲染后图不得近空白（std >= OVERRIDE_MIN_STD 且墨迹占比 >= OVERRIDE_MIN_INK）
    · 多份文件命中同一个图号 -> 低优先级那份报歧义（由 --override-priority 决定谁赢）
"""
import argparse
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

EXIT_OK, EXIT_USAGE, EXIT_DEP = 0, 2, 3

INSTALL_HINTS = {
    'fitz': '缺少 PyMuPDF。安装：\n  pip install pymupdf',
    'pdftoppm': ('缺少 pdftoppm（区域渲染首选后端）。\n'
                 '  Windows: winget install oschwartz10612.Poppler\n'
                 '  Debian/Ubuntu: apt-get install -y poppler-utils\n'
                 '  装好后确认 pdftoppm 在 PATH 里，或用 --pdftoppm PATH 指定。\n'
                 '  不装也能跑：会自动回退到 PyMuPDF(fitz) 内置渲染，结果等价，\n'
                 '  只是速度略慢；--native-only 则完全跳过区域渲染。'),
    'pdfimages': ('缺少 pdfimages（原生无损分流用）。\n'
                  '  Windows: winget install oschwartz10612.Poppler\n'
                  '  Debian/Ubuntu: apt-get install -y poppler-utils\n'
                  '  缺失不致命：会退回 600 dpi 区域渲染（保真度略降）。'),
    'qpdf': ('缺少 qpdf（xref 损坏原件的归一化用）。\n'
             '  winget install qpdf   /   apt-get install -y qpdf\n'
             '  缺失不致命：个别 xref 损坏的原件可能只被识别出 1 页。'),
}


def _require_fitz():
    try:
        import fitz
        return fitz
    except ImportError:
        print('ERROR: ' + INSTALL_HINTS['fitz'], file=sys.stderr)
        sys.exit(EXIT_DEP)


def which(name, explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else (shutil.which(explicit) or None)
    return shutil.which(name)


# ---------------------------------------------------------------- 文本行重建
def build_lines(page):
    """字符级、**按栏**重建文本行。

    原 PDF 常没有空格字符；左右栏同一高度的字被并成一行会导致图题识别错位，
    因此先按页面中线分栏，再在每栏内按 top 聚类。
    """
    mid = page.rect.width / 2.0
    cols = {0: [], 1: []}
    for blk in page.get_text('rawdict')['blocks']:
        if blk.get('type') != 0:
            continue
        for ln in blk.get('lines', []):
            for sp in ln.get('spans', []):
                for ch in sp.get('chars', []):
                    c = ch['c']
                    if not c.strip():
                        continue
                    bb = ch['bbox']
                    cc = (bb[0] + bb[2]) / 2.0
                    cols[0 if cc < mid else 1].append(
                        {'text': c, 'x0': bb[0], 'x1': bb[2], 'top': bb[1], 'bottom': bb[3]})
    lines = []
    for col, cs in cols.items():
        cs.sort(key=lambda c: (c['top'], c['x0']))
        cur = []
        for c in cs:
            if cur:
                mt = sum(x['top'] for x in cur) / len(cur)
                if abs(c['top'] - mt) > 2.6:
                    lines.append(_mkline(cur, col)); cur = []
            cur.append(c)
        if cur:
            lines.append(_mkline(cur, col))
    lines.sort(key=lambda l: (round(l['top'], 1), l['x0']))
    return lines


def _mkline(cs, col):
    cs = sorted(cs, key=lambda c: c['x0'])
    parts, prev, big = [], None, 0
    for c in cs:
        if prev is not None and (c['x0'] - prev['x1']) > 1.0:
            parts.append(' ')
            if (c['x0'] - prev['x1']) > 12.0:
                big += 1
        parts.append(c['text'])
        prev = c
    return {'top': min(c['top'] for c in cs), 'bottom': max(c['bottom'] for c in cs),
            'x0': min(c['x0'] for c in cs), 'x1': max(c['x1'] for c in cs),
            'text': ''.join(parts), 'col': col, 'big_gaps': big}


# ---------------------------------------------------------------- 图题识别
# 严格：数字后为 . / : 或行尾（"FIGURE 1" 独占行也命中）；宽松：Fig N 后接任意内容。
# 编号捕获为「数字+点+连字符」串后再归一化：避免正则回溯把 OMA 的 `4.-1` 截成 `4`。
CAP_STRICT = re.compile(
    r'^\s*(?:fig(?:ure)?|abb)\.?\s*:?\s*(?=[A-Za-z0-9.\-]*\d)([0-9A-Z][A-Za-z0-9.\-]*?)'
    r'\s*(?:[.:](?!\s*[-\d])|\s*$)', re.I)
CAP_LOOSE = re.compile(
    r'^\s*(?:fig(?:ure)?|abb)\.?\s*:?\s*(?=[A-Za-z0-9.\-]*\d)([0-9A-Z][A-Za-z0-9.\-]*)', re.I)


def is_listing_page(page, cap_hits):
    """True when a page is a Table/List of Figures rather than a figure page.

    WHY this exists: a ToF/LoF page repeats the REAL figure numbers as ordinary
    caption-looking text ("Figure: 6.4.2.-1 Example of ..."), so the registry
    accepts every one of them as a caption of a live figure.  Because
    match_pass() walks pages ASCENDING and `fno in mapping` is permanent, the
    listing page -- front matter, hence always visited first -- wins the number
    and the REAL figure on a later page can never be emitted.

    Measured on #27: page 6 is 'Table of Figures' with 20 such rows, p45 holds
    the real figure 6.4.2-1.  Before this guard the stroke fix made p6 emit the
    figure instead, and `fig6.4.2-1.png` was a 3432x150pt strip of the index's
    own hyperlink text -- a figure count that rose while the figure was never
    found.

    The discriminator is the ratio of caption-looking rows to real artwork,
    which separates the two page kinds cleanly on this library:

        #27 p6  (Table of Figures)  20 caption rows / 0 artwork
        #27 p45 (real figure)        1 caption row  / 6 artwork
        #05 p7  (List of Figures)    7 caption rows / 0 big artwork
        #26 p6-8(List of Figures)   10,16,5 rows   / 0 big artwork

    A page is a listing when it carries MANY captions and essentially no
    artwork big enough to be a figure.  `cap_hits` is the number of caption-like
    lines already matched on this page; the thresholds are deliberately far
    apart (5 rows, but 2 artwork) so an ordinary page holding three small
    figures is never mistaken for an index.
    """
    if cap_hits < LISTING_MIN_CAPS:
        return False
    art = 0
    for g in page.get_drawings():
        r = g['rect']
        if r.width >= LISTING_MIN_ART and r.height >= LISTING_MIN_ART:
            art += 1
            if art >= 2:
                return False
    return len(page.get_image_info()) < 2


def norm_no(x):
    """图号归一化：`4.-1` -> `4-1`；`5.3.2.1.1.-1` -> `5.3.2.1.1-1`（与 md `图 4-1` 体例一致）

    与 prep.py::_norm_tabno 的对应关系（两处必须保持同一口径，本项目已因
    「口径不同步」多次出缺陷）：

        extract_figs.norm_no(s)  ==  prep._norm_tabno(s)     对全部图号成立

    两者都只做两件事：去尾部点、把「点+连字符」`.-` 折成 `-`。
    prep 的版本另外带 ROMAN_NUM（罗马数字->阿拉伯）用于**表号**；图号**不
    采用**该规则，因为已普查全库 28 篇、40+ 图题体例，**没有任何一篇使用
    罗马数字图号**（`Fig. I` 之类零命中），照搬只会把合法的字母编号
    （如 OMA 附录的 `C.3.1.1-1`）误伤。故此处**有意**不引入 ROMAN_NUM。
    """
    x = (x or '').strip().rstrip('.')
    return x.replace('.-', '-')


# Half the stroke width added to each side of a zero-thickness line so that a
# STROKED line keeps a real, non-empty box.  A line reported as
# (x, y0, x, y1) really inks (x - width/2 .. x + width/2); the element set needs
# a positive extent before it can cluster.  #27 p45 lines carry width=0.633, so
# 0.5pt each side both reflects the ink and clears the 0.4pt cull.
STROKE_MIN_HALF = 0.5

# A restored zero-thickness line may span at most this fraction of the page
# width.  Above it the drawing is a header/footer or table RULE, not figure art
# (see stroke_extent).  #06 has 52 such rules and #20 has 64; admitting them cost
# each paper one figure.  #27's sequence-diagram lifelines are 37% of the page,
# so 0.55 separates the two cases with margin on both sides.
STROKE_MAX_SPAN_FRAC = 0.55

# TABULAR-ADJACENCY discriminator for restored zero-thickness rules.
#
# WHY it exists (measured regression): #20 p11 carries three 238.1pt horizontal
# rules that belong to Table 2.  They sit 7.0pt above a RIGHT-column cross
# reference to "Fig. 6", so the caption pairing stole fig6 from its real figure
# on p13 and produced a 238x17pt fragment -- while the gate still reported
# PASS and FIGS=24/24.  The page-width ceiling cannot catch them: they are only
# 40% of the page, i.e. BELOW the 0.55 ceiling, and raising the ceiling instead
# breaks #06 (6 -> 5 figures).
#
# A column-width ratio is no better: #27 p47's genuine diagram line is 0.98 of
# its column, so it would be rejected too.
#
# What DOES separate the populations is whether the rule lies inside a table
# body:
#     REJECT  #20 p11 : table text within 20pt  -> 1,1,1,3,1   (always >= 1)
#     KEEP    #27 p45 : table text within 20pt  -> all 0
#     KEEP    #27 p47 : 0
# Library-wide effect: 660 rules rejected by the ceiling, 29 by this test,
# 2392 restored lines kept.
STROKE_TAB_NEAR = 20.0
STROKE_TABISH_RE = re.compile(
    r"(LogLik|AIC|BIC|Gamma|Pareto|Weibull|Dist\.)|(\d+\.\d+\s+\d+\.\d+)")

# ---- Listing-page (ToF/LoF) thresholds, see is_listing_page() --------------
# A page with at least this many caption-looking rows AND fewer than 2 pieces of
# artwork at least this big is a Table/List of Figures, not a figure page.
# Measured spread on this library: listings carry 5-25 caption rows with 0
# artwork; the busiest genuine figure page carries far fewer rows alongside real
# artwork.  Both limits are set to the permissive side of that gap so an
# ordinary multi-figure page is never reclassified as an index.
LISTING_MIN_CAPS = 5
LISTING_MIN_ART = 20.0


def stroke_extent(rect, width, page_rect=None):
    """Box for a STROKED drawing whose rect has collapsed on one axis.

    `get_drawings()` reports a line's BOUNDING BOX: a vertical line at x=194.3
    spanning y=414.1..640.0 comes back as width 0.0, height 225.9.  The line is
    real artwork with `width` = 0.633pt of ink, and the `w <= 0.4 or h <= 0.4`
    cull in run() deleted it -- which on #27 p45 removed the lifelines and
    message arrows that make up figure 6.4.2-1's sequence diagram, so no cluster
    reached the caption and the figure was never extracted.

    LENGTH CEILING: a restored line must not span most of the page.  WHY this is
    not optional -- restoring EVERY stroked line regressed two previously
    passing papers, and the cause is exactly the page-wide rules:

        #06  52 of its 167 zero-thickness drawings are >=60% page width
             (e.g. (49.0,206.1,563.0,206.1) = 514pt = 84% of 612pt)
        #20  64 of its 143 are (e.g. (51.0,45.9,544.3,45.9) = 493pt = 83%)

    Those are header/footer and table rules.  As elements they union-join real
    figures and drag the cluster across the page, so the caption pairing then
    rejects it: measured, #06 fell 7 -> 6 and #20 24 -> 23 with the ceiling
    absent and both were restored with it present.

    #27's lifelines are 225.9pt (37% of 612) and its arrows 219.8pt (36%), so
    they sit well under the ceiling -- the discriminator is real, not a
    coincidence of one document.

    Returns a fattened (x0, top, x1, bottom), or None when there is nothing to
    recover -- an all-zero rect, a page-spanning rule, or no stroke at all.
    Callers must treat None as "must not enter the element set".
    """
    x0, top, x1, bot = rect.x0, rect.y0, rect.x1, rect.y1
    w, h = x1 - x0, bot - top
    if w > 0.4 and h > 0.4:
        return (x0, top, x1, bot)          # already a real box
    if w <= 0.0 and h <= 0.0:
        return None                        # a point, not a stroke
    if page_rect is not None and \
            max(w, h) > STROKE_MAX_SPAN_FRAC * page_rect.width:
        return None                        # a page-wide rule, not figure art
    half = STROKE_MIN_HALF
    try:
        if width:
            half = max(STROKE_MIN_HALF, float(width) / 2.0)
    except (TypeError, ValueError):
        half = STROKE_MIN_HALF
    if w <= 0.4:
        x0 -= half
        x1 += half
    if h <= 0.4:
        top -= half
        bot += half
    return (x0, top, x1, bot)


# How far (in pt) a caption may sit from a figure band and still own it.  The
# pairing cost already refuses more than 60pt of vertical distance; the band
# slicing must use the same reach or it would cut clusters using captions that
# belong to a completely different figure.
REACH = 60.0

# How far a graphic cluster may overlap a caption's box and still count as
# sitting ABOVE / BELOW it rather than BESIDE it.
#
# WHY this exists: a figure built from an EMBEDDED RASTER has its rect taken
# from the bitmap's own placement, not from a vector bounding box, so the
# artwork commonly runs right up to the caption's first baseline.  Measured on
# this library: #06 fig4 cluster bottom 502.4 vs caption top 501.9 (0.5pt of
# overlap) and #10 fig4 301.9 vs 297.8 (4.1pt).  Both are ordinary "figure on
# top, caption underneath" layouts, but the old strict comparison classified
# them as 'side', and the side band walk (written for a caption in the text
# gutter BESIDE a figure) then produced a crop only as tall as the caption
# itself -- 5.6pt and 8.1pt -- which the final guard rejected as `degenerate`.
#
# 12pt is deliberately larger than any of those abutments yet far smaller than
# a real sidebar label (whose caption is typically swallowed by the figure's
# own height, checked separately below), so it cannot reclassify a genuine
# side-by-side pairing.
ABUT_TOL = 12.0

# ---- Raster element guards (see elems()) -----------------------------------
# An embedded bitmap is only admitted to the element set when it is big enough
# to be artwork.  WHY: a raster logo / header strip / watermark is a perfectly
# ordinary image to get_image_info(), and admitting one lets it union-join a
# nearby figure or -- worse -- win the caption pairing outright and be emitted
# as "the figure".  #17 p3 carries a 33.4x28.5pt conference logo at
# (39.7, 26.0, 73.1, 54.5) that must never become a crop.
#
# The two limits are ANDed (an image must clear BOTH).  Both are derived from a
# library-wide measurement of the 116 bitmaps that ARE extracted figures, plus
# the furniture that must be refused:
#
#   quantity        | smallest real figure | furniture to refuse
#   ----------------+----------------------+---------------------------
#   shorter side    |  62.8pt (#22 p11)    | 28.5pt (#17 logo)
#   area            | 6515pt^2 (1.34%)     |  952pt^2 (0.196%, #17 logo)
#
# RASTER_MIN_SIDE = 40pt sits between 28.5 and 62.8.  It must stay well under
# 62.8: an earlier value of 48pt silently dropped a genuine 250.2x42.2pt figure
# panel on #22 p9 (its height is only 42.2pt) and cost that paper one figure.
# RASTER_MIN_AREA_FRAC = 0.008 (0.8% of the page) sits between 0.196% and
# 1.34%, and is what actually rejects the logo -- the side test alone would
# admit any thin 40x40 sliver.
#
# Such an image is DROPPED from the element set, never clamped: shrinking it
# would still let its centre pull a cluster towards the page margin.
RASTER_MIN_SIDE = 40.0
RASTER_MIN_AREA_FRAC = 0.008

# How far a caption may sit from the page's ONLY artwork raster before the
# pairing stops being credible.  Used exclusively by the lone-raster fallback
# in match_pass(): when a page carries exactly one candidate raster and exactly
# one caption, the two must belong together whatever the vertical distance,
# because there is nothing else on the page for that caption to label.
# #17 p3 measures 346.6pt (caption at the top of the left column, artwork far
# down the right column), so the limit is set at 420pt -- comfortably past
# every two-column layout in this library while still refusing a pairing that
# would span an entire page height (792pt tabloid) and clearly be nonsense.
LONE_RASTER_MAX_REACH = 420.0

# ---- TEXT-AS-FIG detection ------------------------------------------------
# A crop is a real figure only if what it captured is artwork.  The gate could
# not see the opposite failure: a crop that is BODY PROSE rendered at figure
# size.  It passes every "did we extract a picture" test because it genuinely IS
# a non-blank bitmap, with the right count and the right filename.  Three
# delivered papers shipped exactly that (#03 fig1, #09 fig6, #11 fig5).
#
# The telling invariant on all three is the PAIRING COST.  When the band walk
# failed to find artwork it fell back to the caption's own text band, so the
# cluster that "won" the caption was the text block itself.  Such a pairing is
# geometrically perfect -- zero vertical distance, zero horizontal gap -- and
# therefore scores a cost of exactly 0.0.  A genuine figure pairing virtually
# never does: it carries at least a few points of caption-to-artwork gap
# (#03 fig3 = 9.1, the tightest legitimate case in this library).
#
#     #03 fig1  p2  cost=0.0  n_el=807  <- two columns of body prose
#     #09 fig6  p10 cost=0.0  n_el=5    <- body prose + original redaction bar
#     #11 fig5  p8  cost=0.0  n_el=6    <- body prose, right edge clipped
#     #03 fig3  p5  cost=9.1  n_el=12   <- the genuine figure
#
# The element count guards against a genuinely empty/decorative cluster, which
# can also pair at zero distance without being prose.
#
# CALIBRATION (measured over the whole 28-paper library -- see the TEXT-AS-FIG
# line the run always prints): the rule matches exactly 5 sidecar entries, of
# which the 3 above are confirmed defects and the 2 remaining (#28 fig1, fig2)
# were visually re-checked and ARE legitimate figures.  Those two are reported
# as a WARNING, never deleted.  Precision is therefore 3/5 with zero false
# deletions, which is the correct trade for an advisory check: a missed prose
# crop ships a defect, while a false warning costs a reviewer one glance.
TAF_COST_ZERO = 0.0         # the pairing cost that marks a text-block pairing
TAF_MIN_ELEMENTS = 4        # below this a zero-cost cluster is just decor
TAF_SUSPECT_ASPECT = 6.0    # a crop far wider than tall is more suspicious


def _text_as_fig_probe(fitz, page, rbox, cost, n_el):
    """Return (verdict, evidence) for one emitted crop.

    Called for EVERY emitted crop, so the caller can print an unconditional
    count and prove this branch actually ran.

    verdict is one of:
      'none'    - the pairing cost is non-zero, or the cluster is too small to
                  be a text block; treated as a normal figure
      'suspect' - cost is exactly zero with a substantial cluster, but the crop
                  does not look like a paragraph band
      'confirm' - as 'suspect', AND the crop is a thin full-measure strip, which
                  is the shape a body-text band takes
    """
    rx0, ry0, rx1, ry1 = rbox
    box = fitz.Rect(rx0, ry0, rx1, ry1)
    area = box.get_area() if not box.is_empty else 0.0
    w, h = rx1 - rx0, ry1 - ry0
    aspect = (w / h) if h > 0 else 0.0
    txt = page.get_text('text', clip=box).strip() if area > 0 else ''
    n_chars = len(txt)
    ev = {'cost': cost, 'n_el': n_el, 'chars': n_chars,
          'aspect': round(aspect, 2),
          'sample': ' '.join(txt.split())[:60]}

    if cost > TAF_COST_ZERO or n_el < TAF_MIN_ELEMENTS:
        return 'none', ev
    if aspect > TAF_SUSPECT_ASPECT:
        return 'confirm', ev
    return 'suspect', ev


def raster_is_artwork(bbox, page_rect):
    """True when an embedded bitmap is plausibly a FIGURE rather than furniture.

    Returns False for logos, watermarks, header strips and the near-degenerate
    slivers that some producers emit for shading.  Only a size test is applied
    here: the cross-page decor detector (`make_decor_detector`) already removes
    images that repeat at the same size and x-position on >=3 pages, and this
    guard covers the rest of the furniture that does NOT repeat.
    """
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    if w < RASTER_MIN_SIDE or h < RASTER_MIN_SIDE:
        return False
    page_area = max(1.0, (page_rect.x1 - page_rect.x0) * (page_rect.y1 - page_rect.y0))
    return (w * h) >= RASTER_MIN_AREA_FRAC * page_area



def split_cluster_by_caps(c, caps, elems_in=None):
    """Split ONE figure cluster into per-caption vertical slices.

    A vertically stacked strip of figures is often joined into a single cluster
    because the whitespace between two stacked panels (< gap_v = 26pt) is
    smaller than the clustering gap.  Handing the whole cluster to whichever
    caption has the lowest cost made every other figure on the page
    unextractable, so each caption must own its own band.

    The band edges are taken from the WHITESPACE between element rows, not from
    the caption positions: a caption usually sits at the top (or bottom) of its
    own figure, so the midpoint between two captions cuts straight through the
    middle of both figures (#20 p18 produced a bar chart sliced in half).
    We therefore pick the widest interior whitespace gaps, keeping only gaps
    that are consistent with the captions' order.

    Returns a list of cluster dicts.  A cluster that already sits beside a
    single caption is returned unchanged, so single-figure pages are untouched.
    """
    if len(caps) <= 1:
        return [c]
    top, bot = c['top'], c['bottom']
    # Only captions that are actually ADJACENT to this cluster may cut it.  A
    # caption far away belongs to another figure above/below and must not
    # generate a cut line, otherwise a spurious band is carved out of this
    # cluster and then collides with the real figure's crop (#27 p46: caption
    # 6.4.2-2 at y=150 cut the 6.4.2-3 diagram at y=226..412).
    near = [(no, ln) for (no, ln) in caps
            if not (ln['bottom'] < top - REACH or ln['top'] > bot + REACH)]
    if len(near) <= 1:
        return [c]
    want = len(near) - 1            # number of cuts needed for these captions

    # Candidate cut lines = whitespace rows between element rows inside the
    # cluster.  Each candidate must lie between two captions so a caption never
    # ends up outside its own band.
    ys = sorted((ln['top'] + ln['bottom']) / 2.0 for (_, ln) in near)
    lo_lim, hi_lim = ys[0], ys[-1]
    cuts = []
    if elems_in:
        rows = sorted(((e[2], e[4]) for e in elems_in), key=lambda t: t[0])
        merged = []
        for (t0, b0) in rows:                    # merge overlapping element rows
            if merged and t0 <= merged[-1][1] + 1.0:
                merged[-1][1] = max(merged[-1][1], b0)
            else:
                merged.append([t0, b0])
        for i in range(len(merged) - 1):
            g0, g1 = merged[i][1], merged[i + 1][0]
            if g1 - g0 <= 0.5:
                continue                          # rows touch/overlap: not a gap
            mid = (g0 + g1) / 2.0
            if lo_lim < mid < hi_lim and top < mid < bot:
                cuts.append((-(g1 - g0), mid))    # widest gap first
    cuts.sort()
    picked = []
    for _w, mid in cuts:
        if len(picked) >= want:
            break
        if all(abs(mid - p) > 12 for p in picked):
            picked.append(mid)
    if len(picked) < want:
        # Whitespace alone is not conclusive (a chart's own gap may be wider
        # than the gap between two charts).  Fall back to the caption midpoints
        # so every caption still gets its own band.
        for i in range(len(ys) - 1):
            mid = (ys[i] + ys[i + 1]) / 2.0
            if len(picked) >= want:
                break
            if top + 12 < mid < bot - 12 and all(abs(mid - p) > 12 for p in picked):
                picked.append(mid)
    edges = [top] + sorted(picked) + [bot]
    if len(edges) <= 2:
        return [c]
    out = []
    for i in range(len(edges) - 1):
        b0, b1 = edges[i], edges[i + 1]
        if (b1 - b0) < 12:
            continue
        out.append(dict(c, top=b0, bottom=b1))
    return out or [c]


def bbox_overlap(a, b, tol=2.0):
    """True when two final crop boxes materially overlap (same figure twice).

    Region-level de-duplication: two different figures on one page may share a
    graphic cluster, but their *final* crops must not cover the same pixels.
    """
    ax0, at, ax1, ab = a
    bx0, bt, bx1, bb = b
    ox = min(ax1, bx1) - max(ax0, bx0)
    oy = min(ab, bb) - max(at, bt)
    if ox <= tol or oy <= tol:
        return False
    inter = ox * oy
    smaller = min((ax1 - ax0) * (ab - at), (bx1 - bx0) * (bb - bt))
    return smaller > 0 and inter > 0.35 * smaller


def _png_size(path):
    """Read (width, height) straight from the PNG IHDR chunk.

    Used to pair pdfimages output with page images by PIXEL SIZE rather than by
    list index -- the two orders are not the same (see the ORDERING BUG note in
    the native-index section).  Stdlib only; no Pillow dependency.
    """
    with open(path, 'rb') as fh:
        head = fh.read(26)
    if len(head) < 26 or head[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG: %s' % path)
    w = int.from_bytes(head[16:20], 'big')
    h = int.from_bytes(head[20:24], 'big')
    return w, h


def table_stacks(lines):
    """表行栈：连续 >=3 条「内部存在 >12pt 间隙」的行 -> 表格区（y 屏障）。

    目的：阻止表体元素与相邻插图被并成同一图形簇（#22 图 10 曾误含表 III）。
    """
    stacks, cur = [], []
    for ln in sorted(lines, key=lambda l: l['top']):
        if ln.get('big_gaps', 0) >= 1:
            if cur and (ln['top'] - cur[-1]['top']) > 20.0:
                if len(cur) >= 3:
                    stacks.append((cur[0]['top'], cur[-1]['bottom'],
                                   min(c['x0'] for c in cur), max(c['x1'] for c in cur)))
                cur = []
            cur.append(ln)
        else:
            if len(cur) >= 3:
                stacks.append((cur[0]['top'], cur[-1]['bottom'],
                               min(c['x0'] for c in cur), max(c['x1'] for c in cur)))
            cur = []
    if len(cur) >= 3:
        stacks.append((cur[0]['top'], cur[-1]['bottom'],
                       min(c['x0'] for c in cur), max(c['x1'] for c in cur)))
    return stacks


# ---------------------------------------------------------------- 图形元素
# Max gap (pt) between two bitmaps of the SAME page for them to count as tiles of
# one figure.  Measured on #11 p5: the 4 rows abut at y=368/405/441 (gap 0.0pt)
# and the two columns are 13pt apart (366 -> 379); the union of all 8 is
# 388x146pt.  #17 p3's conference logo is a single 33x28pt bitmap with no
# neighbour anywhere on the page, so any threshold keeps it out of a group.
DECOR_TILE_GAP = 6.0

# Minimum AREA (pt^2) of a bitmap before it may count as a real tile of a
# collage.  WHY this is needed on top of the union test: a page can carry dozens
# of sub-point fragments (a scanned halftone, a hatched fill) that all touch each
# other, so union-find chains them into one large bounding box and the union
# alone would then "rescue" pure noise.  Measured separation between the two
# populations across #01/#04/#11/#22 (min tile area within a group):
#     genuine figure tiles : 2247 .. 9192 pt^2   (#11 p5 -> 7591; #22 p8 -> 4087)
#     noise dust           :    0.1 .. 41.5 pt^2 (#04 p8's 89-piece 250x187 chain
#                                                 has a min tile area of 0.1)
# That is a 54x gap, so 400pt^2 sits far from both: ~10x the worst noise tile and
# ~5x below the smallest genuine tile.  It also keeps the isolated 34x20pt noise
# block (680pt^2) out, because such a box stands alone and never reaches the
# union branch at all.
DECOR_TILE_MIN_AREA = 400.0

# Maximum UNION-FILL ratio (sum of tile areas / union area) for a tile group to
# be accepted as a mosaic.  WHY: `DECOR_TILE_GAP` chains any bitmaps that merely
# touch, so unrelated page furniture also forms one big "group" whose union is
# large enough to pass a size test.  A REAL mosaic partitions its union -- each
# cell covers its own patch exactly once, so the areas sum to the union area --
# whereas a false group either overlaps heavily or leaves the union sparse.
# Measured on every group that the rescue would newly admit:
#     genuine mosaics  : 1.000 (11 p5) .. 1.004 (#22 p8)
#     false groups     : 1.007, 1.008, 1.028, 1.095, 1.556, 1.730, 2.038, 2.286
# 1.005 sits inside that gap.  The gap is narrow by nature (both quantities are
# areas), which is why the ratio is used ONLY as an additional filter on top of
# the size test, never as the sole test.
DECOR_TILE_FILL_MAX = 1.005

# Bookkeeping for the tile-union branch of `is_decor` below.  Printed
# unconditionally by the caller so a reviewer can tell "the new clause ran" from
# "the new clause never executed" -- this repo's recurring silent-no-op trap.
DECOR_TILE_STATS = {'groups': 0, 'rescued': 0, 'alone_small': 0, 'dust': 0,
                    'loose': 0, 'ambig': 0}


def _tile_groups(boxes, gap=DECOR_TILE_GAP):
    """Union-find over `boxes`: adjacency (gap-tolerant overlap on BOTH axes).

    Returns a list of groups, each a list of bboxes.  Two boxes join when their
    x-intervals overlap within `gap` AND their y-intervals overlap within `gap`,
    which is exactly the "tiles of one collage" relation: consecutive rows/cols
    of a grid touch or nearly touch on both axes.
    """
    n = len(boxes)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = boxes[i], boxes[j]
            if (a[0] - gap <= b[2] and b[0] - gap <= a[2]
                    and a[1] - gap <= b[3] and b[1] - gap <= a[3]):
                parent[find(i)] = find(j)
    out = {}
    for i in range(n):
        out.setdefault(find(i), []).append(boxes[i])
    return list(out.values())


def make_decor_detector(doc):
    """跨页重复装饰（logo/水印/页眉位图）：同尺寸(+-2pt)且同 x 区间在 >=3 页重复 -> 剔除。"""
    sig = defaultdict(set)
    for p in range(doc.page_count):
        for im in doc[p].get_image_info():
            w = round(im['bbox'][2] - im['bbox'][0])
            h = round(im['bbox'][3] - im['bbox'][1])
            sig[(round(w / 2), round(h / 2), round(im['bbox'][0] / 4))].add(p + 1)
    decor = {k for k, v in sig.items() if len(v) >= 3}

    # ---- per-page caption rows, for the caption-proximity exemption below ----
    # A caption line begins with "Fig"/"Figure"/"Table" (English source).  Built
    # eagerly here, like the tile-union table, so a lookup can never find an
    # empty structure that was simply never populated.
    _cap_rows = {}
    for p in range(doc.page_count):
        ys = []
        try:
            for blk in doc[p].get_text('dict')['blocks']:
                if blk.get('type') != 0:
                    continue
                t = ''.join(s['text'] for l in blk.get('lines', [])
                            for s in l['spans']).strip()
                if re.match(r'^(Fig\.?|Figure|Table)\s*\d', t):
                    ys.append((blk['bbox'][1] + blk['bbox'][3]) / 2.0)
        except Exception:
            ys = []
        _cap_rows[p] = tuple(ys)

    # ---- per-page tile-union table, built EAGERLY (see `_union_for`) ----
    # Clause B below ("w < 40 or h < 40") exists to drop near-degenerate slivers
    # (23-byte 34x20 black blocks).  It also killed #11 p5's Figure 2, which is
    # exported as 8 bitmap tiles of 209x36 / 167x36pt: every tile is 36pt tall,
    # so every tile was vetoed individually and the whole 388x146pt figure
    # vanished (measured: #11 stuck at 7/8, `MISSING-EXPECT: ["2"]`).  A tile of
    # a collage must therefore be judged on the UNION of its tile group.
    #
    # The table is a plain dict keyed by rounded page number -> list of bboxes,
    # populated HERE, at detector-construction time, before any is_decor() call.
    # An earlier attempt instead attached the table as an attribute on the
    # closure (`is_decor._boxes`) and read it from inside; it was never
    # populated, every lookup missed, and the run collapsed to 0 figures.  A
    # structure that is filled in the same breath as it is created cannot be
    # "not yet filled", so that failure mode is designed out.  The public
    # signature `is_decor(bbox) -> bool` is unchanged; the page is recovered
    # from the bbox via an exact-key index (page -> {rounded bbox: page}).
    _page_boxes = []
    _owner = {}
    for p in range(doc.page_count):
        bb = [tuple(im['bbox']) for im in doc[p].get_image_info()]
        _page_boxes.append(bb)
        for b in bb:
            _owner.setdefault(_round_box(b), []).append(p)
    _union = []
    _ngroups = 0
    for bb in _page_boxes:
        groups = _tile_groups(bb) if bb else []
        m = {}
        for g in groups:
            ux0 = min(t[0] for t in g); uy0 = min(t[1] for t in g)
            ux1 = max(t[2] for t in g); uy1 = max(t[3] for t in g)
            if len(g) > 1:
                _ngroups += 1
            _uarea = max(1e-9, (ux1 - ux0) * (uy1 - uy0))
            _fill = sum((t[2] - t[0]) * (t[3] - t[1]) for t in g) / _uarea
            for t in g:
                m[_round_box(t)] = (ux1 - ux0, uy1 - uy0, len(g), _fill)
        _union.append(m)

    def _page_of(bbox):
        """Page index owning `bbox`, or None.  Uses the same rounded key as the
        tile-union table, so the two lookups agree by construction."""
        cands = _owner.get(_round_box(bbox))
        if not cands:
            return None
        first = cands[0]
        return first if all(c == first for c in cands) else None

    def _union_for(bbox):
        """(uw, uh, n, fill) for `bbox`'s tile group, else None when alone.

        Returns None when the box cannot be matched to a page, or when its group
        has only one member.  Falling back to None means "judge the single box",
        i.e. the original clause-B behaviour -- the rescue can only ever be
        triggered by a matched, genuinely multi-tile group.

        The same bbox can occur on several pages (a header logo repeated on
        every page is the common case; measured on 6 of the library's documents,
        all of them furniture).  Such a box must resolve to ONE group, so the
        candidates are required to AGREE: if the pages disagree, the box is
        treated as unmatched and the conservative per-box test applies.  That
        keeps a shared bbox from being rescued by some unrelated page's collage.
        """
        key = _round_box(bbox)
        cands = [_union[p].get(key) for p in _owner.get(key, ())]
        cands = [c for c in cands if c is not None]
        if not cands:
            return None
        first = cands[0]
        if all(c == first for c in cands):
            return first
        DECOR_TILE_STATS['ambig'] += 1
        return None

    def is_decor(bbox):
        x0, y0, x1, y1 = bbox
        w, h = round(x1 - x0), round(y1 - y0)
        key = (round(w / 2), round(h / 2), round(x0 / 4))
        # **尺寸守卫**：仅「小尺寸」重复位图视为装饰；拼贴图的单片往往尺寸较大且相似，
        # 不加守卫会被误删，导致整幅拼贴图残缺（#22 fig1/10/11 实测）。
        small = (w < 200 and h < 80) or (w * h < 20000)
        if key in decor and small:
            # CAPTION-PROXIMITY EXEMPTION.  WHY: a paper may REUSE the same panel
            # image across several figures (its experimental-setup photograph
            # appears in Fig. 1, Fig. 10 and Fig. 11).  Clause A only sees
            # "same size + same x on >=3 pages", which such a panel satisfies
            # exactly like running furniture does -- the two are the same shape.
            # Measured on #22 p11: its 94x104 panels recur on pages 2/10/11, so
            # both were dropped, `n_el` fell to 1, and the crop covered only the
            # third bitmap with its labels cut ("ondition E5", "irflow rate").
            #
            # A bitmap that sits within the vertical reach of a caption ON THIS
            # PAGE is part of that page's figure, not page furniture -- furniture
            # is precisely what is NOT tied to a caption.  So the exemption is
            # scoped to this page's own caption rows and changes nothing for a
            # logo in the header.
            _caps = _cap_rows.get(_page_of(bbox) or -1) or ()
            if any(abs(y1 - cy) <= DECOR_CAP_REACH or abs(y0 - cy) <= DECOR_CAP_REACH
                   for cy in _caps):
                DECOR_TILE_STATS['figkeep'] = DECOR_TILE_STATS.get('figkeep', 0) + 1
                return False
            return True
        if w < 40 or h < 40:      # 近纯色/极小碎图（如 23 字节 34x20 黑块）
            # Tile-union rescue.  FOUR conditions, all required:
            #   (1) the box matches an indexed page (and every page carrying it
            #       agrees on the group -- see _union_for);
            #   (2) its tile group has more than one member (a lone box is
            #       judged alone, exactly as before);
            #   (3) the box's own AREA clears DECOR_TILE_MIN_AREA (dust floor);
            #   (4) the group UNION clears 40pt on BOTH axes.
            # The per-tile SIZE is deliberately not consulted -- the whole point
            # is that each tile is thin while the collage is not -- but the
            # per-tile AREA is, via DECOR_TILE_MIN_AREA, and the group must
            # actually PARTITION its union, via DECOR_TILE_FILL_MAX.
            u = _union_for(bbox)
            if u is not None and u[2] > 1:
                if w * h < DECOR_TILE_MIN_AREA:
                    DECOR_TILE_STATS['dust'] += 1
                    return True
                if u[3] > DECOR_TILE_FILL_MAX:
                    DECOR_TILE_STATS['loose'] += 1
                    return True
                if u[0] >= 40 and u[1] >= 40:
                    DECOR_TILE_STATS['rescued'] += 1
                    return False
                DECOR_TILE_STATS['alone_small'] += 1
            else:
                DECOR_TILE_STATS['alone_small'] += 1
            return True
        return False

    DECOR_TILE_STATS['groups'] = _ngroups
    return is_decor


def _round_box(b):
    """Stable key for a bitmap bbox: values rounded to 1/10pt.

    The extractor hands the same tuple through several layers
    (`get_image_info()` -> `elems()` -> stats), so an exact float key would be
    fragile; 0.1pt is far below any real layout distance yet far above the
    float noise of a round-tripped PDF coordinate.
    """
    return tuple(round(float(v), 1) for v in b)


# ---------------------------------------------------------------- page-clip invariant
# How far beyond the page box an element may stick before it is treated as an
# authoring artifact rather than real content.  A hairline stroke on the trim
# edge is normal (a 0.5pt border centred on the boundary reaches ~0.25pt out);
# anything past 1pt is a genuinely oversized object.
PAGE_TOL = 1.0

# Stddev of grey levels below which an automatically extracted crop is pure
# background.  Far lower than OVERRIDE_MIN_STD (3.0) on purpose: an override
# is a human claim that must be held to a high bar, whereas this test only
# rejects images that are LITERALLY a single flat colour (std == 0.0 for the
# #08 blank bar).  0.5 admits a few stray anti-aliased pixels while still
# catching every blank bar, so it cannot silently delete a faint real figure.
CROP_MIN_STD = 0.5

# ---- CLIP-EXTEND: the crop must cover the diagram's own LABEL text ----------
# A sequence diagram draws its action labels as ORDINARY TEXT, and the message
# text between the lifelines likewise.  `elems()` emits only 'img' and 'rect'
# elements, so NO text is ever in the element set and `union_find_clusters()`
# cannot see it.  When a label overhangs the outermost lifeline -- which is the
# normal layout whenever the label is wider than the lifeline gap -- the cluster
# stops well short of the label and the crop cuts the glyphs in half.
#
# Measured on #27 (OMA LwM2M, 110pp, 20 sequence diagrams).  `slack` is
# (cluster right edge - rightmost text the diagram owns):
#
#   fig          page   cluster x1   rightmost text   slack    human verdict
#   -----------  -----  -----------  ---------------  -------  -------------
#   6.4.2-1      45     538          434.9            +103.1   COMPLETE (Lead)
#   6.4.3-1      48     459          454.6              +4.4   complete
#   C.3.1.1-1    100    485          481.3              +3.7   complete
#   6.4.4-1      52     435          454.5             -19.5   CLIPPED: 'Rea'
#   6.5-2        58     415          435.3             -20.3   CLIPPED
#   6.5-1        57     406          426.8             -20.8   CLIPPED (Lead)
#   6.6.1-1      59     404          424.9             -20.9   CLIPPED
#   6.4.5-1      55     434          468.0             -34.0   CLIPPED
#   C.3.2.2-1    102    458          528.0             -70.0   CLIPPED
#
# The sign of `slack` separates the two populations with NO overlap, and the
# Lead's independently verified figures are on the correct side of it
# (6.4.2-1 COMPLETE at +103.1; 6.4.4-1 and 6.5-1 CLIPPED at -19.5 / -20.8).
#
# WHY NOT the four cheaper signals -- all were measured on the full library and
# rejected because they do NOT separate:
#   * verdict by OVERHANG MAGNITUDE: 136/324 figures hit (42%).  A cluster's
#     y-band spans the whole figure height, so page prose sharing the band
#     "overhangs" by ~264pt and swamps the signal (#06 p7, #01 p5, ...).
#   * verdict by ink DENSITY inside vs outside: `dens_in` is 1.00 on EVERY #27
#     edge including both known-clipped figures -- the crop edge lands ON the
#     glyphs, so "quiet inside" is never true for a real clip.
#   * verdict by ink-run with a leading whitespace gap: same reason.
#   * verdict by "ink straddles the edge" alone: fires on the control's left
#     edge (beyond=1 dark px) and on prose boundaries.
# The Lead's own calibration recorded the same lesson for the sibling
# "straddling ink" signal (158/323 = 49%).
#
# `slack` avoids all of that because it is measured against the text the
# DIAGRAM owns (a word is the diagram's if it BEGINS at or left of the cluster's
# right edge -- i.e. the cluster already overlaps it), not against whatever text
# shares the page band.  Prose in the next column begins far to the right of the
# edge and is therefore never counted, so no page-width or gutter constant is
# needed anywhere in this test.
#
# The extension is capped and every extension is printed.  A cap is required
# rather than optional: without it a mis-paired cluster on a prose page would
# grow to the column edge and swallow body text -- the #03 failure mode, where
# cluster merging produced a two-column prose crop.  A figure whose label column
# is further than CLIP_EXTEND_MAX from the cluster edge is REPORTED as a soft
# warning and left alone: this repo has already paid for "silently widen until
# something fits".
CLIP_EXTEND_MAX = 120.0        # pt; largest extension ever applied automatically
CLIP_EXTEND_SOFT_WARN = 90.0   # pt; beyond this, warn even though we still extend
CLIP_EXTEND_EPS = 0.5          # pt; slack must be at least this negative to act


def clip_extent(page, box):
    """Text the FIGURE owns, and how far the crop must grow to hold it.

    `box` is the cluster rectangle (x0, top, x1, bottom).  Returns
    (x0, x1, report) where x0/x1 are the extended edges, or None when no
    extension is warranted.

    A word counts as the figure's own when it lies in the cluster's y-band AND
    the cluster already overlaps the LINE it belongs to.  The test is applied per
    TEXT LINE, not per word, and that distinction is load-bearing:

      p57 fig6.5-1, label `Device Registration with` at y=98.7.  The cluster's
      right edge is 406.0.  `Device` spans x[392.6,416.5] and so IS overlapped,
      but `Registration` starts at 418.8 and `with` at 465.2 -- both entirely
      past the edge.  A per-WORD test therefore extends only to 416.5 and leaves
      the label reading `Device Re`.  Extending to the END OF THE LINE the
      overlapped word belongs to covers the whole label.

    A line whose FIRST word already starts well past the edge is not the
    figure's: nothing of it is inside the crop.  That is the prose case, and it
    is what keeps a neighbouring column's body text out.

    WHY this is not the rejected "overhang magnitude" test: that one measured
    every word in the band, including words whose line begins entirely outside
    the figure.  Requiring the line to be OVERLAPPED by the cluster removes the
    prose population by construction, which is what makes the sign of the slack
    meaningful.
    """
    cx0, cy0, cx1, cy1 = box
    try:
        words = page.get_text('words')
    except Exception:
        return None
    # Group the band's words into text lines by their vertical position.  A
    # 2pt tolerance merges superscripts/subscripts onto their parent line and
    # keeps genuinely separate rows apart (measured line pitch here is ~5.9pt).
    band = [w for w in words if w[1] >= cy0 - 2 and w[3] <= cy1 + 2]
    lines = {}
    for w in band:
        key = None
        for k in lines:
            if abs(k - w[1]) <= 2.0:
                key = k
                break
        lines.setdefault(key if key is not None else w[1], []).append(w)
    lo, hi = cx0, cx1
    n_lo = n_hi = 0
    for ws in lines.values():
        lx0 = min(w[0] for w in ws)
        lx1 = max(w[2] for w in ws)
        # A line belongs to the figure when the crop already OVERLAPS it, i.e.
        # some part of the line lies inside the cluster horizontally.  A line
        # sitting wholly outside is another column's text and is ignored.
        if not (lx0 < cx1 and lx1 > cx0):
            continue
        if lx1 > hi:
            hi = lx1
            n_hi += 1
        if lx0 < lo:
            lo = lx0
            n_lo += 1
    grow_r = hi - cx1
    grow_l = cx0 - lo
    rep = {'grow_l': round(grow_l, 2), 'grow_r': round(grow_r, 2),
           'n_l': n_lo, 'n_r': n_hi}
    if grow_r < CLIP_EXTEND_EPS and grow_l < CLIP_EXTEND_EPS:
        return None
    rep['capped'] = bool(grow_r > CLIP_EXTEND_MAX or grow_l > CLIP_EXTEND_MAX)
    return lo, hi, rep


def clip_to_page(box, page_rect, tol=PAGE_TOL):
    """Intersect `box` with the page rectangle -> (box, overflow_report).

    THE INVARIANT: nothing this module emits may lie outside the page.  A
    PDF may legitimately contain geometry that sticks out past the MediaBox
    (authoring artifacts, crop marks, a figure drawn on a larger canvas and
    then clipped by the page), and feeding such a box to a renderer silently
    produces a crop of whatever happens to be under the surviving sliver --
    typically blank paper.

    `overflow_report` is None when the box is already inside the page,
    otherwise a dict describing how far it stuck out, so the caller can
    report WHY a crop was clamped instead of quietly changing the number.

    NOTE a zero-area box (a horizontal or vertical rule, e.g. y0 == y1) is
    NOT an overflow: it is a degenerate element that is discarded elsewhere
    (`w <= 0.4 or h <= 0.4`).  Reporting it here would flood the log with
    harmless 0.0pt "overflows" and hide the real ones.
    """
    x0, top, x1, bot = box
    px0, py0, px1, py1 = page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1
    dx0, dtop = px0 + tol - x0, py0 + tol - top
    dx1, dbot = x1 - (px1 - tol), bot - (py1 - tol)
    over = max(dx0, dtop, dx1, dbot, 0.0)
    cx0, ctop = max(x0, px0), max(top, py0)
    cx1, cbot = min(x1, px1), min(bot, py1)
    # A degenerate (zero/negative-extent) box is not an out-of-page problem.
    if x1 <= x0 or bot <= top:
        return (cx0, ctop, cx1, cbot), None
    if cx1 <= cx0 or cbot <= ctop:
        return (cx0, ctop, cx0, ctop), {'overflow': over, 'collapsed': True,
                                        'raw': [round(v, 2) for v in box]}
    rep = None
    if over > tol:
        rep = {'overflow': round(over, 2),
               'raw': [round(v, 2) for v in box],
               'clipped': [round(v, 2) for v in (cx0, ctop, cx1, cbot)],
               'page': [round(v, 2) for v in (px0, py0, px1, py1)]}
    return (cx0, ctop, cx1, cbot), rep


def elems(page, is_decor, stats=None):
    """图形元素；宽横线（表格框线，>=0.5 页宽且高<3pt）不进元素集，改作屏障。

    Oversized elements are dropped, not clamped.  WHY: an element whose box
    sticks far outside the page is an authoring artifact (Morales p18 carries
    a 663pt-wide PURE WHITE `re` fill on a 595pt page).  Such a panel silently
    UNION-JOINS every figure it overlaps into one giant cluster, which then
    defeats both the caption pairing and the band clamp and yields a blank
    crop.  Clamping it instead would only shrink its corners while leaving it
    free to bridge the same figures, so it must not enter the element set at
    all.  A white/near-white panel that covers most of a column is background,
    never content.

    RASTER elements come from `page.get_image_info()`, i.e. the embedded
    bitmaps, and are subject to `raster_is_artwork()` -- the size guard that
    keeps a header logo out of the figure set (see RASTER_MIN_SIDE).

    `stats`, when given, accumulates the raster bookkeeping printed as
    RASTER-ELEMS / RASTER-SKIP so a reviewer can tell "the raster path ran and
    admitted nothing" apart from "the raster path never ran" -- this repo's
    recurring silent-no-op trap.
    """
    es, wide = [], []
    pw = page.rect.width
    # `is_decor` is applied HERE, before any tile grouping: it is the gate that
    # rejects page furniture, and it is counted explicitly so the reported
    # RASTER-ELEMS DECOR= figure stays the number of bitmaps actually vetoed.
    _imgs = []
    for im in page.get_image_info():
        if is_decor(im['bbox']):
            if stats is not None:
                stats['decor'] = stats.get('decor', 0) + 1
            continue
        _imgs.append(im)
    # TILE-AWARE admission.  WHY: some producers export one figure as a grid of
    # small bitmap tiles.  #11 p5 is Figure 2 ("(a) Sensor structure diagram,
    # (b) Film sensor cross-section") rendered as 8 tiles of 209x36 / 167x36 pt:
    # every tile is below RASTER_MIN_SIDE=40 on its short axis, so a per-tile test
    # rejects all of them and the whole figure disappears (measured: #11 stayed
    # at 7/8 missing figure 2).
    #
    # The union of those tiles is 388x146pt = 11.3% of the page -- far above the
    # thresholds, while furniture stays alone and small: #17 p3's conference logo
    # is a single 33x28pt bitmap (0.18% of page).  So judge the UNION OF ADJACENT
    # TILES, not each tile in isolation.
    #
    # NOTE this stage is only reached by bitmaps that already survived
    # `is_decor` above, and `is_decor` applies the SAME tile-group relation to
    # clause B.  Both must agree on what "adjacent" means, so the grouping is
    # shared via `_tile_groups(DECOR_TILE_GAP)` rather than re-implemented here
    # (two copies of the relation would drift apart and re-open this defect).
    _tile_ok = {}
    if _imgs:
        _boxes = [tuple(im['bbox']) for im in _imgs]
        for _g in _tile_groups(_boxes):
            ux0 = min(t[0] for t in _g)
            uy0 = min(t[1] for t in _g)
            ux1 = max(t[2] for t in _g)
            uy1 = max(t[3] for t in _g)
            # a group of >=2 tiles is judged on its union; a lone bitmap keeps
            # the original per-bitmap test
            for _b in _g:
                _tile_ok[_b] = raster_is_artwork((ux0, uy0, ux1, uy1), page.rect) \
                    if len(_g) > 1 else raster_is_artwork(_b, page.rect)

    for im in _imgs:
        if stats is not None:
            stats['seen'] = stats.get('seen', 0) + 1
        if not _tile_ok.get(tuple(im['bbox']),
                            raster_is_artwork(im['bbox'], page.rect)):
            if stats is not None:
                stats['small'] = stats.get('small', 0) + 1
                stats.setdefault('skipped', []).append(
                    [round(v, 1) for v in im['bbox']])
            continue
        if stats is not None:
            stats['kept'] = stats.get('kept', 0) + 1
        es.append(('img',) + tuple(im['bbox']))
    d = page.get_drawings()
    for dr in d:
        r = dr['rect']
        w, h = r.x1 - r.x0, r.y1 - r.y0
        if w >= 0.5 * pw and h < 3.0:
            wide.append((r.y0, r.y1, r.x0, r.x1))
            continue
        es.append(('rect', r.x0, r.y0, r.x1, r.y1))
    return es, wide


def drop_oversized(es, page_rect, pno=0, log=None, tol=PAGE_TOL):
    """Remove elements whose box sticks out of the page -> (kept, dropped).

    Applied BEFORE clustering so an oversized panel can neither bridge two
    figures nor inflate the cluster's bounding box.  Every removal is logged:
    a silent filter here would look exactly like "the figure was never there".
    """
    kept, dropped = [], []
    for e in es:
        box, rep = clip_to_page((e[1], e[2], e[3], e[4]), page_rect, tol)
        if rep is None:
            # Inside the page, or a zero-area rule that the caller discards.
            kept.append(e)
            continue
        # Genuine content that merely grazes the edge (a figure drawn to the
        # trim line) is kept, CLAMPED: its overflow is tiny, so it cannot have
        # been bridging anything.  Only a material overflow is dropped.
        if rep['overflow'] <= max(tol, 2.0) and not rep.get('collapsed'):
            kept.append((e[0], box[0], box[1], box[2], box[3]))
            continue
        dropped.append((e, rep))
    if dropped and log is not None:
        log('FIGCLIP: p%d excluded %d oversized element(s) from clustering '
            '(max overflow %.1fpt; page %.0fx%.0f)'
            % (pno, len(dropped),
               max(d[1]['overflow'] for d in dropped),
               page_rect.x1 - page_rect.x0, page_rect.y1 - page_rect.y0))
    return kept, dropped


# Widest horizontal seam still treated as ONE figure when both elements are
# bitmap tiles with substantial vertical overlap (see the IMAGE-TILE HORIZONTAL
# JOIN branch in union_find_clusters).  #11 p5's seam is 12.9pt; 16 leaves margin
# while staying far below a column gutter (~30pt+), so a neighbouring column
# cannot be absorbed.
TILE_JOIN_GAP_H = 16.0

# Two bitmaps count as "the same grid row" -- i.e. two tiles of one figure -- only
# when their top and bottom edges agree within this many points.  Measured on the
# real case (#11 p5): paired tiles differ by <=0.7pt, whereas merely adjacent
# figures differ by much more.  This tolerance is what keeps the join from
# merging separate figures.
TILE_JOIN_ROW_TOL = 2.0

# A repeated bitmap within this many points of one of THIS PAGE's caption rows is
# treated as part of that page's figure rather than as running furniture (see the
# caption-proximity exemption in is_decor).  #22 p11's reused panels sit ~150pt
# above their "Fig. 11." line, so the reach has to be generous; it stays far
# below a page height, so a header logo cannot qualify.
DECOR_CAP_REACH = 320.0

# Optional dict for the cluster self-check counters (TILE-HJOIN).  None disables.
_CLUSTER_STATS = None


def union_find_clusters(es, gap_v=26.0, gap_h=9.0, stacks=()):
    """并查集聚类。gap_v=26pt / gap_h=9pt（基准实测值）。"""
    n = len(es)
    par = list(range(n))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]; a = par[a]
        return a

    def uni(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            par[rb] = ra

    def blocked(at, ab, ax0, ax1, bt, bb, bx0, bx1):
        """与任一屏障（表行栈/宽横线）在**垂直且水平**方向都相交 -> 禁止合并。

        只看垂直会把并排栏的表格误当屏障（#20 图簇被拆散）；
        水平判据同时定位 #22 fig10 误含表 III。
        """
        lo, hi = min(ab, bb), max(at, bt)
        if min(ax1, bx1) <= max(ax0, bx0):          # 两元素本身不水平重叠 -> 不是同一图版
            return False
        for (s0, s1, sx0, sx1) in stacks:
            if s0 < hi and lo < s1 and min(ax1, sx1) > max(ax0, sx0) and min(bx1, sx1) > max(bx0, sx0):
                return True
        return False

    order = sorted(range(n), key=lambda i: es[i][2])
    for oi in range(n):
        i = order[oi]
        _, ax0, at, ax1, ab = es[i]
        for oj in range(oi + 1, n):
            j = order[oj]
            _, bx0, bt, bx1, bb = es[j]
            if bt - ab > gap_v:
                break
            vo = min(ab, bb) - max(at, bt)
            ho = min(ax1, bx1) - max(ax0, bx0)
            # 注：曾尝试把「图<->非图」间隙收紧到 12pt 以排除表格，但会切散矢量框图
            # （#03 图1 回归为单框），故恢复统一 gap_v；表格排除由屏障负责。
            if ho > 0 and vo > -gap_v:
                if not blocked(at, ab, ax0, ax1, bt, bb, bx0, bx1):
                    uni(i, j)
            elif vo > 0 and ho > -gap_h:
                if not blocked(at, ab, ax0, ax1, bt, bb, bx0, bx1):
                    uni(i, j)
            # IMAGE-TILE HORIZONTAL JOIN.  WHY: some producers export ONE figure
            # as a grid of bitmap tiles whose columns are separated by a seam
            # wider than gap_h but far narrower than a column gutter.  #11 p5
            # measures left column x=157.5..366.2 and right column x=379.1..545.7,
            # a 12.9pt seam against gap_h=9.0 -- so the columns never joined and
            # only the LEFT one was captured, losing panel (b) entirely (measured:
            # els=4, crop=233x164; with the join: els=8, crop=412x164).
            #
            # Scoped deliberately to two IMAGE elements that sit on the SAME
            # GRID ROW, so it cannot pull body text or a neighbouring column (or a
            # separate figure) into a cluster.
            #
            # The "same row" test is what makes this safe.  An earlier version
            # accepted any vertical overlap and REGRESSED two papers, losing one
            # figure each: measured with the broad rule #04 fell 16->15 (fig10) and
            # and disabling only this branch restored both.
            # #11 p5's paired tiles agree to within ~0.7pt on both the top and the
            # bottom edge, so a 2pt tolerance admits the real grid while rejecting
            # merely-adjacent figures.
            elif (vo > 0 and es[i][0] == 'img' and es[j][0] == 'img'
                    and ho > -TILE_JOIN_GAP_H
                    and vo > 0.5 * min(ab - at, bb - bt)
                    and abs(at - bt) <= TILE_JOIN_ROW_TOL
                    and abs(ab - bb) <= TILE_JOIN_ROW_TOL):
                if not blocked(at, ab, ax0, ax1, bt, bb, bx0, bx1):
                    uni(i, j)
                    if _CLUSTER_STATS is not None:
                        _CLUSTER_STATS['tile_hjoin'] = \
                            _CLUSTER_STATS.get('tile_hjoin', 0) + 1
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(es[i])
    return [{'x0': min(e[1] for e in g), 'top': min(e[2] for e in g),
             'x1': max(e[3] for e in g), 'bottom': max(e[4] for e in g),
             'n': len(g), 'nimg': sum(1 for e in g if e[0] == 'img')}
            for g in groups.values()]


# ---------------------------------------------------------------- 渲染
def render_region(pdf, pno, box, dpi, out_path, pdftoppm, tmpdir, tag,
                  render_ok=True):
    """Render one page region to PNG.

    pdftoppm (poppler) is the preferred backend but is OPTIONAL: PyMuPDF is
    already a hard dependency, so when poppler is missing we render with
    fitz.Page.get_pixmap(clip=...) instead of returning None.

    The old code called pdftoppm unconditionally, so with poppler absent even
    the --native-only path crashed with
        TypeError: expected str, bytes or os.PathLike object, not NoneType
    although that flag is documented as "skip region rendering".
    """
    x0, top, x1, bot = box
    if pdftoppm:
        sx = dpi / 72.0
        prefix = os.path.join(tmpdir, tag)
        cmd = [pdftoppm, '-f', str(pno), '-l', str(pno), '-r', str(dpi),
               '-x', str(int(x0 * sx)), '-y', str(int(top * sx)),
               '-W', str(int((x1 - x0) * sx)), '-H', str(int((bot - top) * sx)),
               '-png', pdf, prefix]
        subprocess.run(cmd, capture_output=True)
        got = sorted(glob.glob(prefix + '-*.png')) or sorted(glob.glob(prefix + '.png'))
        if got:
            os.replace(got[0], out_path)
            return out_path
        # else fall through to the fitz backend rather than failing outright
    if not render_ok:
        return None
    return render_region_fitz(pdf, pno, box, dpi, out_path)


def render_region_fitz(pdf, pno, box, dpi, out_path):
    """PyMuPDF fallback for render_region().  Returns path or None."""
    try:
        import fitz
    except ImportError:
        return None
    doc = None
    try:
        doc = fitz.open(pdf)
        page = doc[pno - 1]
        pm = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                             clip=fitz.Rect(*box))
        pm.save(out_path)
        return out_path
    except Exception as exc:
        print('WARN: fitz \u533a\u57df\u6e32\u67d3\u5931\u8d25 (p%d %s): %s'
              % (pno, box, exc), file=sys.stderr)
        return None
    finally:
        if doc is not None:
            doc.close()


# ---------------------------------------------------------------- override
# A hand-written bbox file is a HUMAN decision that shadows the automatic
# extraction for one figure number.  Three defects made the old version
# un-reviewable:
#   * runpaper.py never passed --override; the file only worked because this
#     module GUESSED `<outdir>/../figs_override.json`.  An implicit path that
#     cannot be seen in --help and breaks as soon as the output layout changes.
#   * the guessed directory was `.work/`, which is git-ignored, so a PASS that
#     depended on it could not be reproduced from a fresh clone.
#   * nothing reported WHETHER an override was used, so "PASS" could not be
#     told apart from "the algorithm actually found the figure".
# The search list below is therefore ordered, PRINTED in full, and the winner
# is echoed in the structured output so the gate can label the run.
OVERRIDE_CANDIDATES = 6          # number of documented search locations
OVERRIDE_MIN_STD = 3.0           # stddev of grey levels below which a crop is blank
OVERRIDE_MIN_INK = 0.002         # fraction of non-background pixels below which it is blank
OVERRIDE_INK_DELTA = 40          # grey distance from the corner background to count as ink


def override_candidates(args, outdir, pdf):
    """Ordered, de-duplicated list of (path, kind, exists, priority) to try.

    Priority 1 (the explicit --override) is never silently skipped: if the user
    names a file and it is not there, the run must fail rather than quietly fall
    back to an automatic location.
    """
    paper_dir = os.path.dirname(os.path.abspath(pdf))
    stem = os.path.splitext(os.path.basename(os.path.abspath(pdf)))[0]
    parent = os.path.dirname(os.path.abspath(outdir))
    # The paper number: given outright by runpaper.py (--nn), else inferred from
    # the parent directory name of the output dir (.work/16/figs -> 16).
    nn = str(getattr(args, 'nn', '') or '').strip()
    if not nn:
        pref = os.path.basename(parent.rstrip('\\/'))
        if re.fullmatch(r'[0-9]{1,3}', pref or ''):
            nn = pref.zfill(2)
    cands = []
    if args.override:
        cands.append((os.path.abspath(args.override), 'explicit', 1))
    # Derived from the ORIGINAL layout so the historical back-compat still works
    # for someone who has not migrated yet.
    pref = os.path.basename(parent.rstrip('\\/'))
    auto = []
    if nn:
        # THE CANONICAL TRACKED LOCATION.  Listed first so it wins over every
        # back-compat path below.
        auto.append(os.path.join(paper_dir, nn + '.figs_override.json'))
        auto.append(os.path.join(parent, nn + '.figs_override.json'))
    auto += [
        os.path.join(paper_dir, stem + '.figs_override.json'),
        os.path.join(parent, pref + '.figs_override.json'),
        os.path.join(parent, 'figs_override.json'),           # legacy .work/<NN>/
        os.path.join(paper_dir, 'figs_override.json'),
    ]
    if args.no_auto_override:
        auto = []
    for i, p in enumerate(auto):
        cands.append((os.path.abspath(p), 'auto', i + 2))
    out, seen = [], set()
    for path, kind, prio in cands:
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            continue
        seen.add(key)
        out.append({'path': path, 'kind': kind, 'priority': prio,
                    'exists': os.path.isfile(path)})
    return out


def _prio_map(args):
    """Parse --override-priority (comma-separated ranks) into {rank: priority}."""
    raw = args.override_priority or 'explicit,paper-nn,paper-stem,parent-nn,parent-legacy,paper-other'
    out = {}
    for i, name in enumerate(x.strip() for x in raw.split(',') if x.strip()):
        out[name] = i + 1
    return out


def _nn_of(args, pdf, outdir):
    """Paper number for this run: --nn if given, else the output dir's basename."""
    nn = str(getattr(args, 'nn', '') or '').strip()
    if nn:
        return nn.zfill(2) if nn.isdigit() else nn
    parent = os.path.dirname(os.path.abspath(outdir))
    pref = os.path.basename(parent.rstrip('\\/'))
    if re.fullmatch(r'[0-9]{1,3}', pref or ''):
        return pref.zfill(2)
    return ''


def override_rank(path, pdf, outdir, nn=''):
    """Classify a candidate path into one of the documented priority ranks."""
    paper_dir = os.path.dirname(os.path.abspath(pdf))
    stem = os.path.splitext(os.path.basename(os.path.abspath(pdf)))[0]
    parent = os.path.dirname(os.path.abspath(outdir))
    pref = os.path.basename(parent.rstrip('\\/'))
    p = os.path.abspath(path)
    if nn and os.path.normcase(p) == os.path.normcase(
            os.path.join(paper_dir, nn + '.figs_override.json')):
        return 'paper-nn'
    if nn and os.path.normcase(p) == os.path.normcase(
            os.path.join(parent, nn + '.figs_override.json')):
        return 'parent-nn'
    if os.path.normcase(p) == os.path.normcase(
            os.path.join(paper_dir, pref + '.figs_override.json')):
        return 'paper-nn'
    if os.path.normcase(p) == os.path.normcase(
            os.path.join(paper_dir, stem + '.figs_override.json')):
        return 'paper-stem'
    if os.path.normcase(p) == os.path.normcase(
            os.path.join(parent, pref + '.figs_override.json')):
        return 'parent-nn'
    if os.path.normcase(p) == os.path.normcase(
            os.path.join(parent, 'figs_override.json')):
        return 'parent-legacy'
    return 'paper-other'


def _resolve_rel_box(val, dpi, page_w, page_h):
    """Interpret one bbox value: [x0,top,x1,bottom], a /MediaBox-like rectangle,
    or a bare string/formula such as \"0 0 612 792\".

    WHY: hand-written override files have been seen with x0/x1 taken from a
    DIFFERENT render resolution than the page box (e.g. a 1240x1754 pixel
    screenshot fed through a 150 dpi renderer).  Leaving them alone silently
    produced a blank crop; the honest fix is to detect the discrepancy and
    SCALE the box, reporting the correction so a reviewer can see it.
    """
    if isinstance(val, str):
        parts = [p for p in re.split(r'[\s,]+', val.strip()) if p]
        if len(parts) != 4:
            raise ValueError('cannot parse bbox string %r' % val)
        val = parts
    if not isinstance(val, (list, tuple)) or len(val) < 4:
        raise ValueError('box must be a 4-element list')
    nums = []
    for v in list(val)[:4]:
        if isinstance(v, str):
            v = re.sub(r'^(?:top|left|right|bottom)\s*:\s*', '', v.strip())
            if re.search(r'[+\-*/]', v):
                if not re.fullmatch(r'[0-9eE.+\-*/\s()]+', v):
                    raise ValueError('unsafe arithmetic in box value %r' % v)
                v = eval(v, {'__builtins__': {}}, {})      # noqa: S307 - char-whitelisted
        f = float(v)
        if not math.isfinite(f):
            raise ValueError('non-finite box value %r' % v)
        nums.append(f)
    x0, top, x1, bot = nums
    if x0 > x1:
        x0, x1 = x1, x0
    if top > bot:
        top, bot = bot, top
    note = ''
    if x1 > page_w + 1 or bot > page_h + 1:
        sx = page_w / x1 if x1 > 0 else 1.0
        sy = page_h / bot if bot > 0 else 1.0
        # Only accept a uniform rescale that brings BOTH axes inside the page:
        # that is the signature of pixel coordinates in a differently sized
        # rendering.  Anything else is a genuinely wrong box and must be
        # reported, not "fixed" into a plausible-looking crop.
        cands = [s for s in (sx, sy) if 0.05 < s < 1.0]
        if cands:
            cands.sort(key=lambda s: abs(s - sx) + abs(s - sy))
            s = cands[0]
            if x0 * s <= page_w + 1 and bot * s <= page_h + 1:
                note = ('rescaled-suspect:box %s exceeded page %.0fx%.0f -> x%.4f'
                        % (nums, page_w, page_h, s))
                nums = [v * s for v in nums]
                x0, top, x1, bot = nums
    if (x1 - x0) > 1.05 * page_w or (bot - top) > 1.05 * page_h:
        note = 'WIDE:box spans more than one page ' + note
    return x0, top, x1, bot, note


def _ink_ratio(path, dpi):
    """(stddev, ink fraction) of a rendered crop, or (None, None) if unreadable."""
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return None, None
    try:
        with Image.open(path) as im:
            g = im.convert('L')
            st = ImageStat.Stat(g).stddev[0]
            small = g.copy()
            small.thumbnail((400, 400))
            px = list(small.getdata())
        bg = max(px[0], px[-1])
        ink = sum(1 for v in px if abs(v - bg) > OVERRIDE_INK_DELTA) / float(len(px) or 1)
        return st, ink
    except Exception:
        return None, None


def load_override(path, dpi, doc, expect=None, log=print):
    """Read one override file -> {figno: spec} with self-checks applied.

    Every rejected entry is reported with the reason; nothing is dropped in
    silence, because a silently ignored override looks exactly like "the
    algorithm failed".
    """
    entries, rejected = {}, []
    try:
        # utf-8-sig: a hand-edited override is very likely to carry a BOM
        # (Notepad and PowerShell's `Set-Content -Encoding utf8` both add one).
        # Rejecting it would look like "the override silently did nothing",
        # which is the exact failure mode this whole rework is about.
        with open(path, encoding='utf-8-sig') as fh:
            raw = json.load(fh)
    except Exception as exc:
        log('ERROR: override %s 解析失败：%s' % (path, exc))
        return entries, [('-', 'unparsable:%s' % exc)]
    if not isinstance(raw, dict):
        log('ERROR: override %s 顶层必须是对象（{图号: {page,box}}）。' % path)
        return entries, [('-', 'not-an-object')]
    base = os.path.dirname(os.path.abspath(path))
    n_expect = 0
    for k, spec in raw.items():
        # Keys starting with "_" are FILE METADATA, not figure entries: this is
        # what lets a tracked override carry a provenance header (where the box
        # came from, who checked it) without the loader treating "provenance" as
        # a figure number.  Without it the header is reported as a rejected
        # entry and the run looks broken to every reviewer.
        if str(k).startswith('_'):
            continue
        no = norm_no(str(k))
        if not isinstance(spec, dict):
            rejected.append((no, 'entry-not-an-object'))
            continue
        if expect is not None and no not in expect:
            n_expect += 1
            continue                       # out of scope for THIS run: not an error
        try:
            pno = int(spec['page'])
        except Exception:
            rejected.append((no, 'bad-page:%r' % (spec.get('page'),)))
            continue
        try:
            box_raw = spec['box']
            try:
                from PIL import Image           # noqa: F401
                _have_pil = True
            except ImportError:
                _have_pil = False
            page = doc[pno - 1] if 1 <= pno <= doc.page_count else None
            if page is None:
                rejected.append((no, 'page-out-of-range:%d (1..%d)' % (pno, doc.page_count)))
                continue
            x0, top, x1, bot, note = _resolve_rel_box(
                box_raw, int(spec.get('dpi', dpi)), page.rect.width, page.rect.height)
        except Exception as exc:
            rejected.append((no, 'bad-box:%s' % exc))
            continue
        pgx0, pgy0, pgx1, pgy1 = page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1
        if x0 < pgx0 - 1 or top < pgy0 - 1 or x1 > pgx1 + 1 or bot > pgy1 + 1:
            rejected.append((no, 'box-outside-page:%s vs %.0f,%.0f,%.0f,%.0f'
                             % ([round(v, 1) for v in (x0, top, x1, bot)],
                                pgx0, pgy0, pgx1, pgy1)))
            continue
        if (x1 - x0) < 12 or (bot - top) < 12:
            rejected.append((no, 'box-too-small:%.1fx%.1fpt' % (x1 - x0, bot - top)))
            continue
        entries[no] = {'page': pno, 'box': [x0, top, x1, bot],
                       'dpi': int(spec.get('dpi', dpi)),
                       'caption_pos': str(spec.get('caption_pos', 'bottom')).lower(),
                       'source': os.path.abspath(path), 'notes': note,
                       'why': str(spec.get('_why', '')),
                       'raw_box': box_raw}
    if n_expect:
        log('NOTE: override %s 有 %d 条不在本次 --expect 范围内，已忽略（不是错误）。'
            % (os.path.basename(path), n_expect))
    for no, why in rejected:
        log('ERROR: override %s 条目 %s 被拒绝：%s' % (os.path.basename(path), no, why))
    return entries, rejected


# ---------------------------------------------------------------- 主流程
def run(args):

    fitz = _require_fitz()
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    # 清空旧的 fig*.png / figs.json（避免上一轮残留被误当本轮产物）
    for f in glob.glob(os.path.join(outdir, 'fig*.png')) + \
             glob.glob(os.path.join(outdir, 'figs.json')):
        if os.path.isfile(f):
            os.remove(f)
    tmp = os.path.join(outdir, '_crop')
    os.makedirs(tmp, exist_ok=True)

    expect = None
    if args.expect:
        expect = {x.strip() for x in args.expect.split(',') if x.strip()}

    pdf = os.path.abspath(args.pdf)
    if not os.path.isfile(pdf):
        print('ERROR: 找不到原件 %s。\n  修法：确认路径，或把原件放进 Paper/。' % pdf,
              file=sys.stderr)
        return EXIT_USAGE

    pdfimages = which('pdfimages', args.pdfimages)
    pdftoppm = which('pdftoppm', args.pdftoppm)
    qpdf = which('qpdf', args.qpdf)

    # `render_ok` is the single source of truth for "may we render regions".
    # --native-only must REALLY skip rendering: without this flag the PyMuPDF
    # fallback below would happily render the figures anyway, making the switch
    # a lie (exactly the class of bug being fixed here).
    render_ok = not args.native_only
    if args.native_only:
        if pdftoppm:
            print('NOTE: --native-only \u5df2\u6307\u5b9a -> \u8df3\u8fc7'
                  '\u533a\u57df\u6e32\u67d3\uff08\u5373\u4f7f\u672c\u673a\u6709 pdftoppm\uff09\u3002')
        pdftoppm = None
    elif not pdftoppm and not args.list_captions:
        # poppler absent: still runnable via the PyMuPDF fallback, but the user
        # must know which backend produced the images.
        print('WARN: \u672a\u627e\u5230 pdftoppm\uff0c\u6539\u7528 PyMuPDF(fitz) '
              '\u5185\u7f6e\u6e32\u67d3\u3002\u7ed3\u679c\u7b49\u4ef7\uff0c\u901f\u5ea6\u7565\u6162\u3002\n'
              '      ' + INSTALL_HINTS['pdftoppm'], file=sys.stderr)
    if not pdfimages:
        print('WARN: ' + INSTALL_HINTS['pdfimages'], file=sys.stderr)

    # ---- xref 归一化：个别原件 xref 损坏，只见 1 页；qpdf 幂等修复 ----
    if qpdf and not args.no_qpdf:
        norm = os.path.join(tmp, 'norm.pdf')
        try:
            subprocess.run([qpdf, pdf, norm], capture_output=True, timeout=600)
            if os.path.exists(norm) and os.path.getsize(norm) > 1024:
                pdf = norm
                print('QPDF-NORMALIZED: norm.pdf')
        except Exception as exc:
            print('WARN: qpdf 归一化失败（继续用原件）：%s' % exc, file=sys.stderr)

    doc = fitz.open(pdf)
    if args.list_captions:
        return list_captions(doc, expect)

    # ---- override 解析（显式优先；全部候选都打印，绝不隐式） ----
    ov_files, ov_entries, ov_rejected = [], {}, []
    ov_meta = {'used': 0, 'path': '', 'kind': '', 'priority': None,
               'loaded': 0, 'entries': 0, 'rejected': 0, 'auto': 0}
    prio = _prio_map(args)
    if args.override and not os.path.isfile(os.path.abspath(args.override)):
        # An explicit path that does not exist is a USAGE error: silently
        # falling back to an automatic location is how the old implicit
        # convention hid itself for a whole round.
        print('ERROR: --override 指定的文件不存在：%s' % os.path.abspath(args.override),
              file=sys.stderr)
        print('       修法：确认路径；或去掉 --override 让它按文档化的顺序自动查找；',
              file=sys.stderr)
        print('             或用 --no-auto-override 明确表示「本次不使用 override」。',
              file=sys.stderr)
        doc.close()
        return EXIT_USAGE
    cands = [c for c in override_candidates(args, outdir, pdf)
             if c['exists'] or c['kind'] == 'explicit']
    print('OVERRIDE-SEARCH:')
    for c in cands:
        print('  [%s] %s  prio=%s  %s'
              % ('FOUND' if c['exists'] else 'absent', c['path'], c['priority'],
                 c['kind']))
    for c in cands:
        if not c['exists']:
            continue
        ent, rej = load_override(c['path'], args.dpi, doc, expect)
        rk = override_rank(c['path'], pdf, outdir, _nn_of(args, pdf, outdir))
        ov_files.append({'path': c['path'], 'kind': c['kind'],
                         'rank': rk, 'prank': prio.get(rk, 99),
                         'n': len(ent), 'rejected': len(rej),
                         'entries': ent, 'rej': rej})
        ov_rejected += [(os.path.basename(c['path']), no, why) for no, why in rej]
    # Explicit --override always wins; otherwise the documented rank order does.
    explicit_files = [f for f in ov_files if f['kind'] == 'explicit']
    others = [f for f in ov_files if f['kind'] != 'explicit']
    if explicit_files:
        winner = explicit_files[0]
        ov_meta['kind'] = 'explicit'
    elif others:
        winner = sorted(others, key=lambda f: (f['prank'], f['path']))[0]
        ov_meta['kind'] = 'auto'
        ov_meta['auto'] = 1
    else:
        winner = None
    if winner is not None:
        ov_entries = dict(winner['entries'])
        ov_meta.update({'winner': winner['path'], 'rank': winner['rank'],
                        'priority': winner['prank'],
                        'loaded': len(ov_files), 'entries': len(ov_entries)})
    # Ambiguity: several files claim the SAME figure number.  Say which one wins
    # and which is ignored -- two hand-written boxes for one figure is exactly
    # the situation where a silent choice makes a PASS unreviewable.
    for f in ov_files:
        if winner is None or f is winner:
            continue
        clash = sorted(set(f['entries']) & set(ov_entries))
        if clash:
            print('NOTE: override 歧义：图号 %s 同时出现在 %s 与生效的 %s —— '
                  '按优先级取后者（%s 不生效）。'
                  % (','.join(clash), os.path.basename(f['path']),
                     os.path.basename(winner['path']), f['path']))
    if ov_files:
        _w = winner['path'] if winner else '(none)'
        print('OVERRIDE FILES=%d WINNER=%s RANK=%s ENTRIES=%d REJECTED=%d'
              % (len(ov_files), _w, (winner['rank'] if winner else '-'),
                 len(ov_entries), len(ov_rejected)))

    is_decor = make_decor_detector(doc)


    # ---- 原生位图索引（pdfimages 输出 vs 页面位图）----
    # NATIVE extraction is what --native-only is FOR, so it always runs when
    # pdfimages exists.  The previous guard was written `not args.native_only
    # is False`, which Python parses as `(not args.native_only) is False` --
    # it happened to work, but it is unreadable; kept explicit below.
    #
    # ORDERING BUG (fixed here, measured on #19):
    #   The old code assumed "pdfimages order == flattened page image order
    #   (sorted by top,x0)" and paired them by INDEX.  That is false.  pdfimages
    #   emits in page/object order, while get_image_info() is sorted by position,
    #   so as soon as a page mixes real figures with 1x1 stencils (rule lines)
    #   the two lists desynchronise.  Measured on #19: 18/117 indices mismatched,
    #   and the native path then copied a 1x1 stencil PNG (88 bytes) for
    #   fig4/fig6/fig9/fig13 -- four BLANK figures that the gate only caught
    #   later as `fig-anomaly: 1x1 solid(dup)`.
    #   Fix: pair by (page, pixel width, pixel height) using a per-key queue,
    #   which is what actually identifies an image, and refuse the native path
    #   when the pairing is ambiguous instead of guessing.
    natdir = os.path.join(tmp, '_native')
    os.makedirs(natdir, exist_ok=True)
    NAT = []
    if pdfimages:
        try:
            subprocess.run([pdfimages, '-png', pdf, os.path.join(natdir, 'im')],
                           check=True, capture_output=True, timeout=1800)
            NAT = sorted(glob.glob(os.path.join(natdir, 'im-*.png')))
        except Exception as exc:
            print('WARN: pdfimages 失败，全部改走区域渲染：%s' % exc, file=sys.stderr)
    # Pair each page image with the pdfimages output that has the SAME page and
    # the SAME pixel dimensions.  Pixel size is what identifies an image; list
    # order does not (see the ORDERING BUG note above).  Each pdfimages file may
    # be used once, so a repeated (page,w,h) triple is served by successive
    # files via a per-key queue.
    from collections import deque as _deque
    _nat_by_key = {}
    for _i, _f in enumerate(NAT):
        try:
            _w, _h = _png_size(_f)
        except Exception:
            continue
        _nat_by_key.setdefault((_w, _h), _deque()).append(_f)
    nat_index = {}
    _ambiguous = 0
    for p in range(doc.page_count):
        page = doc[p]
        for im in sorted(page.get_image_info(), key=lambda x: (x['bbox'][1], x['bbox'][0])):
            bb = tuple(round(v, 1) for v in im['bbox'])
            nat_index[(p, bb)] = (im.get('width'), im.get('height'))
    native_ok = bool(NAT)
    if not native_ok and NAT:
        print('WARN: pdfimages 无输出 -> 关闭原生分流', file=sys.stderr)

    def native_for(pno, box):
        """图形簇恰为一张原生位图（>=50x35pt）时返回原生 PNG 路径，否则 None。

        Matching is by (page, pixel w, pixel h) -- never by list index.
        """
        x0, top, x1, bot = box
        if not native_ok or (x1 - x0) < 50 or (bot - top) < 35:
            return None
        hit = None
        for (p, bb), wh in nat_index.items():
            if p != pno - 1:
                continue
            if abs(bb[0] - x0) < 1.5 and abs(bb[1] - top) < 1.5 and \
               abs(bb[2] - x1) < 1.5 and abs(bb[3] - bot) < 1.5:
                hit = wh
                break
        if hit is None:
            return None
        q = _nat_by_key.get(hit)
        if not q:
            return None
        path = q.popleft()          # consume: one file serves one placement
        # A 1x1 stencil is a rule line, never a figure.
        try:
            w, h = _png_size(path)
            if w < 8 or h < 8:
                return None
        except Exception:
            return None
        return path

    # ---- 逐页扫描图题 ----
    page_cache, found_caps, cap_lines = {}, {}, {}
    # Every caption seen on each page, INDEPENDENT of --expect (see the
    # lone-raster fallback in match_pass).
    page_caps_all = {}
    lc = rc = (36.0, 36.0)
    # Listing-page (ToF/LoF) bookkeeping, accumulated during the caption scan
    # below and folded into `_chk` once that dict exists (see is_listing_page).
    _listing_pages = 0
    _listing_all = []
    for pno in range(1, doc.page_count + 1):
        page = doc[pno - 1]
        L = build_lines(page)
        page_cache[pno] = L
        # ---- LISTING-PAGE GUARD (see is_listing_page) ----
        # Count the caption-looking rows first, then drop the whole page if it is
        # a Table/List of Figures.  This must happen BEFORE anything is written
        # to page_caps_all / found_caps / cap_lines: a listing's rows reuse the
        # real figure numbers, and once registered they claim those numbers
        # permanently (match_pass walks pages ascending, and `fno in mapping` is
        # a hard stop).  #27 p6 -- 'Table of Figures' -- would otherwise take
        # 6.4.2-1 from its real page 45.
        _cap_hits = 0
        for ln in L:
            _t = ln['text']
            if len(_t) <= 5 or re.search(r'\.{4,}', _t):
                continue
            if CAP_STRICT.match(_t) or CAP_LOOSE.match(_t):
                _cap_hits += 1
        if is_listing_page(page, _cap_hits):
            _listing_pages += 1
            _listing_all.append((pno, _cap_hits))
            if L:
                lcol = [l for l in L if l['col'] == 0]
                rcol = [l for l in L if l['col'] == 1]
                lc = (min([l['x0'] for l in lcol], default=36) - 2,
                      max([l['x1'] for l in lcol], default=page.rect.width / 2 - 6) + 2)
                rc = (min([l['x0'] for l in rcol], default=page.rect.width / 2 + 6) - 2,
                      max([l['x1'] for l in rcol], default=page.rect.width - 36) + 2)
            continue
        for ln in L:
            txt = ln['text']
            if len(txt) <= 5:                 # "FIGURE 1" 只有 8 字符，阈值不能太高
                continue
            if re.search(r'\.{4,}', txt):     # 跳过 List of Figures 的点导引条目
                continue
            m = CAP_STRICT.match(txt)
            strict = bool(m)
            if not m:
                m = CAP_LOOSE.match(txt)
            if not m:
                continue
            no = norm_no(m.group(1))
            # 编号首位可以是字母（OMA 附录 C.3.1.1-1）——曾因强制首位数字导致附录 5 图从未被识别
            if not no or not no[0].isalnum():
                continue
            # EVERY caption on the page is recorded here, BEFORE the --expect
            # filter below.  The lone-raster fallback must judge a page by what
            # is actually printed on it, never by the subset this run happens
            # to be asking for: with `--expect 2` the caption of figure 3 is
            # invisible, and a fallback keyed on that subset would happily hand
            # figure 3's artwork to figure 2 (#17 p3 -- measured).
            page_caps_all.setdefault(pno, []).append((no, ln))
            if expect is not None and no not in expect:
                continue
            found_caps[no] = found_caps.get(no, 0) + 1
            cap_lines.setdefault(no, []).append((pno, ln, strict))
        if L:
            lcol = [l for l in L if l['col'] == 0]
            rcol = [l for l in L if l['col'] == 1]
            lc = (min([l['x0'] for l in lcol], default=36) - 2,
                  max([l['x1'] for l in lcol], default=page.rect.width / 2 - 6) + 2)
            rc = (min([l['x0'] for l in rcol], default=page.rect.width / 2 + 6) - 2,
                  max([l['x1'] for l in rcol], default=page.rect.width - 36) + 2)

    mapping, report, unmatched, all_clusters = {}, [], [], []
    # Self-check counters for the page-clip invariant.  They make the fix
    # FALSIFIABLE: a run on a document that DOES contain oversized geometry
    # must show a non-zero count, so "the numbers did not change" can be told
    # apart from "the new code never executed" (this repo's recurring
    # silent-no-op trap).
    # Held in a dict because match_pass() is a nested function: rebinding a
    # plain local there would make it local to match_pass instead.
    _chk = {'oob': 0, 'oob_all': [], 'clamped': 0, 'clamped_all': [],
            'blank': 0, 'blank_all': [],
            'stroke_kept': 0,
            'listing_pages': _listing_pages, 'listing_all': _listing_all,
            'raster_seen': 0, 'raster_kept': 0, 'raster_small': 0,
            'raster_decor': 0, 'raster_skip_all': [],
            'lone': 0, 'lone_all': [],
            # ---- TEXT-AS-FIG: "the crop is body prose, not artwork" ----
            # Counted for EVERY emitted crop, so a zero here means "the branch
            # ran and found nothing", never "the branch was skipped".
            'taf_seen': 0, 'taf_suspect': 0, 'taf_all': [],
            'taf_conf': 0, 'taf_conf_all': [],
            # ---- CLIP-EXTEND: "the crop cut the diagram's own label text" ----
            # `ce_seen` is incremented for EVERY crop that reaches the hook, so
            # a zero cannot be confused with "the hook was never wired in".
            'ce_seen': 0, 'ce_ext': 0, 'ce_all': [],
            'ce_capped': 0, 'ce_capped_all': []}
    # (figure number, page) pairs the lone-raster fallback promoted: their
    # vertical REACH test is waived in the pairing loop below.
    # The key must NOT be built from `id(ln)`: CPython recycles object ids as
    # soon as a page's line list is garbage-collected, so a page-local id can
    # collide with one from an unrelated page and silently waive the reach test
    # there.  Measured damage was real and severe: #20 fell from 24 extracted
    # figures to 10.  (pno, figure number) is stable, unique per page, and is
    # exactly the granularity the fallback decides at.
    _forced = set()

    def match_pass(strict_only):
        for pno in range(1, doc.page_count + 1):
            page = doc[pno - 1]
            L = page_cache[pno]
            ttop = min(l['top'] for l in L) if L else 0
            tbot = max(l['bottom'] for l in L) if L else page.rect.height
            caps = []
            for no, lst in cap_lines.items():
                if no in mapping:
                    continue
                for (p, ln, strict) in lst:
                    if p != pno:
                        continue
                    if strict_only and not strict:
                        continue
                    if (not strict_only) and strict:
                        continue
                    caps.append((no, ln))
            if not caps:
                continue
            pw = page.rect.width
            _rs = {}
            raw, wide = elems(page, is_decor, stats=_rs)
            _chk['raster_seen'] += _rs.get('seen', 0)
            _chk['raster_kept'] += _rs.get('kept', 0)
            _chk['raster_small'] += _rs.get('small', 0)
            _chk['raster_decor'] += _rs.get('decor', 0)
            for _b in _rs.get('skipped', []):
                _chk['raster_skip_all'].append((pno, _b))
            # ---- PAGE-CLIP INVARIANT (see clip_to_page/drop_oversized) ----
            # An element that sticks out of the page cannot be trusted as a
            # figure fragment: it is the signature of a panel drawn on a
            # larger canvas, and its enormous box would union-join every
            # figure it overlaps (Morales p18: 663pt white panel on a 595pt
            # page -> one 196-element cluster -> blank crop).
            raw, _oob = drop_oversized(raw, page.rect, pno, log=print)
            wide = [w for w in wide
                    if clip_to_page((w[2], w[0], w[3], w[1]), page.rect)[1] is None]
            _chk['oob'] += len(_oob)
            for _e, _rep in _oob:
                _chk['oob_all'].append((pno, _rep))
            # ---- ZERO-THICKNESS STROKES (see stroke_extent) ----
            # A stroked VECTOR LINE arrives here with a rect that has collapsed
            # on one axis: `get_drawings()` reports the path's BOUNDING BOX, so
            # a vertical line at x=194.3 spanning y=414.1..640.0 has width 0.0,
            # and a horizontal arrow has height 0.0.  The line is nonetheless
            # real artwork with a real stroke width.
            #
            # The `w <= 0.4 or h <= 0.4` cull below then deletes every one of
            # them.  On #27 p45 that removed the 2 lifelines and 8 message
            # arrows that are figure 6.4.2-1 -- a sequence diagram -- leaving
            # only two 55pt label boxes and some 5.5x5.5 arrowheads, so no
            # cluster reached the caption at y=648.3 and the figure was never
            # extracted (the caption itself normalised fine: `CAPTIONS-IN-PDF`
            # lists it, and `UNMATCHED: []` only means no pair was ever built).
            #
            # Measured scope: 5131 zero-width + 5943 zero-height drawings over
            # the 28-paper library, 23.2% of all 47683 -- a systematic gap, not
            # a #27 quirk.
            #
            # Restore ONLY the ones that are genuinely stroked, using the stroke
            # width the element tuples no longer carry; a truly empty rect still
            # yields None and is still dropped by the cull.
            _zero = {}
            for _g in page.get_drawings():
                _r = _g['rect']
                if (_r.width <= 0.4 or _r.height <= 0.4) and \
                        (_r.width, _r.height) != (0.0, 0.0):
                    _zero[(round(_r.x0, 1), round(_r.y0, 1),
                           round(_r.x1, 1), round(_r.y1, 1))] = _g
            _stroke_added = []
            # Table-body text lines on this page, used by the TABULAR-ADJACENCY
            # test below.  A restored rule that sits inside a table body is
            # table furniture, not figure art.
            _tabtext = []
            try:
                for _b in page.get_text('dict')['blocks']:
                    if _b.get('type') != 0:
                        continue
                    _t = ''.join(_s['text'] for _l in _b.get('lines', [])
                                 for _s in _l['spans'])
                    if STROKE_TABISH_RE.search(_t):
                        _tabtext.append(_b['bbox'])
            except Exception:
                _tabtext = []
            for _g in _zero.values():
                _se = stroke_extent(_g['rect'], _g.get('width'), page.rect)
                if _se is None:
                    continue
                # TABULAR-ADJACENCY discriminator (frame-independent).
                # WHY: a page-width ceiling cannot separate the two populations,
                # because the offending rules are only ~40% of the page while
                # #27's real diagram lines run 36-44% -- they overlap in length.
                # Measured on the library:
                #   REJECT  #20 p11 rules : table text within 20pt -> 1,1,1,3,1
                #   KEEP    #27 p45 lines : table text within 20pt -> all 0
                #   KEEP    #27 p47 line  : 0
                # So "does this rule belong to a table body?" separates them
                # cleanly, independent of page/column width assumptions.
                _rr = _g['rect']
                _is_tab = any(_bb[1] - STROKE_TAB_NEAR <= _rr.y0 <= _bb[3] + STROKE_TAB_NEAR
                              or _bb[1] - STROKE_TAB_NEAR <= _rr.y1 <= _bb[3] + STROKE_TAB_NEAR
                              for _bb in _tabtext)
                if _is_tab:
                    _chk['stroketab'] = _chk.get('stroketab', 0) + 1
                    continue
                _e = ('rect',) + tuple(_se)
                if _e[3] - _e[1] > 0.4 and _e[4] - _e[2] > 0.4:
                    _stroke_added.append(_e)
            _stroke_kept = len(_stroke_added)
            _chk['stroke_kept'] += _stroke_kept
            es = []
            for e in list(raw) + _stroke_added:
                w = e[3] - e[1]; h = e[4] - e[2]
                if w <= 0.4 or h <= 0.4:
                    continue
                if w > 0.85 * pw and h < 2.5:
                    continue
                if e[2] < ttop - 26 or e[4] > tbot + 26:
                    continue
                es.append(e)
            barriers = list(table_stacks(L)) + list(wide)
            clusters = [c for c in union_find_clusters(es, stacks=barriers)
                        if (c['x1'] - c['x0']) >= 8 and (c['bottom'] - c['top']) >= 8]
            # ---- LONE-RASTER FALLBACK (see LONE_RASTER_MAX_REACH) ----
            # A wide two-panel figure may have its caption at the TOP of one
            # column while its artwork sits far down the OTHER column -- the
            # publisher's own layout, not an error.  #17 p3: `Figure 2.` at
            # (60.0, 71.4) in the left column, its only figure raster at
            # (338.5, 417.1, 521.8, 688.5) in the right column, 346pt away.
            # No cluster can reach that caption within REACH, so the figure was
            # never extracted.
            #
            # The test is deliberately narrow, because widening REACH is a
            # powerful move.  The pairing is only forced when, on this page:
            #   * exactly ONE cluster carries an artwork raster (`nimg >= 1`) --
            #     a page may also hold stray vector rules (p3 has a pair of
            #     0.5pt table rules at y=623..636) and those must not make the
            #     page look like a multi-figure layout;
            #   * NO caption printed on the page can reach ANY artwork cluster
            #     within REACH -- if an ordinary pairing exists, the normal
            #     rules are working and must be left alone (this is what keeps
            #     #20's gutter captions out of here, and what stops #17 p3's
            #     `Figure 3.` caption from losing its artwork);
            #   * the nearest such caption is within LONE_RASTER_MAX_REACH and
            #     that caption is one this run is actually looking for.
            # Then, and only then, is the distance test waived for that single
            # (figure, page) pair.
            #
            # The page's OWN caption list is used, never `caps`: `caps` is
            # filtered by --expect, so judging with it made the outcome depend
            # on which figures were requested.  Measured on #17 p3: with
            # `--expect 2` the rule handed figure 3's raster to figure 2 and
            # reported a figure that is not on the page at all.
            _art = [c for c in clusters if c['nimg'] >= 1]
            _allcaps = page_caps_all.get(pno, [])
            if len(_art) == 1 and _allcaps:
                _lone = _art[0]

                def _vgap(c, ln):
                    if c['bottom'] <= ln['top']:
                        return ln['top'] - c['bottom']
                    if c['top'] >= ln['bottom']:
                        return c['top'] - ln['bottom']
                    return 0.0                   # vertically overlapping
                _reachable = any(_vgap(_a, _ln) <= REACH
                                 for _a in _art for (_n, _ln) in _allcaps)
                _want = {n for (n, _l) in caps}
                _cands = sorted(((_vgap(_lone, _ln), _n) for (_n, _ln) in _allcaps),
                                key=lambda t: t[0])
                _pick = next(((g, n) for (g, n) in _cands if n in _want), None)
                if not _reachable and _pick and 0.0 < _pick[0] <= LONE_RASTER_MAX_REACH:
                    _gap, _fno = _pick
                    _chk['lone'] += 1
                    _chk['lone_all'].append((pno, _fno, round(_gap, 1),
                                             [round(v, 1) for v in
                                              (_lone['x0'], _lone['top'],
                                               _lone['x1'], _lone['bottom'])]))
                    _forced.add((pno, _fno))
            for _c in clusters:
                if (_c['x1'] - _c['x0']) >= 100 and (_c['bottom'] - _c['top']) >= 60:
                    all_clusters.append((pno, _c))
            # ---- Region-level ownership ----
            # The old loop consumed one whole cluster per figure (`used_c`), so
            # when several captions on a page shared a cluster exactly one of
            # them won and the rest were dropped forever (#12 fig1/4,
            # #20 13 of 24 missing).  Two changes fix that without ever merging
            # neighbouring figures into one crop:
            #   1. a cluster that spans several caption bands is SLICED along
            #      the caption mid-gap lines, so each caption owns its own band;
            #   2. de-duplication happens on the FINAL crop rectangle, so the
            #      same pixels are still never emitted twice.
            cand_clusters, sliced_ids = [], set()
            for _c in clusters:
                _inner = [e for e in es
                          if e[1] >= _c['x0'] - 0.5 and e[3] <= _c['x1'] + 0.5
                          and e[2] >= _c['top'] - 0.5 and e[4] <= _c['bottom'] + 0.5]
                _parts = split_cluster_by_caps(_c, caps, _inner)
                for _p in _parts:
                    if len(_parts) > 1:
                        sliced_ids.add(len(cand_clusters))
                    cand_clusters.append(_p)
            pairs = []
            for ci, c in enumerate(cand_clusters):
                for fno, ln in caps:
                    ho = min(c['x1'], ln['x1']) - max(c['x0'], ln['x0'])
                    vo = min(c['bottom'], ln['bottom']) - max(c['top'], ln['top'])
                    # ---- ABUT vs OVERLAP (see ABUT_TOL) ----
                    # A RASTER figure's drawn box routinely ends a fraction of a
                    # point ON or PAST the caption's top edge: the embedded
                    # bitmap was cropped to the artwork, so its bottom lands at
                    # y=502.4 while the caption starts at y=501.9 (#06 fig4).
                    # The old strict test `c['bottom'] <= ln['top']` therefore
                    # classified a figure-sitting-directly-above-its-caption as
                    # 'side', and the 'side' band walk -- which is written for a
                    # caption in the text gutter next to a figure, not for one
                    # underneath -- collapsed the crop to the 5.6pt sliver
                    # between the caption top and its own bottom, which was then
                    # rejected as `degenerate` (#06 fig4, #10 fig4: both purely
                    # raster, both check out geometrically otherwise).
                    #
                    # ONLY the abutment is reclassified.  The 'side' case is
                    # left exactly as it was, because it carries #20's
                    # gutter-caption layout (a left-column label beside a
                    # right-column figure overlapping it vertically): widening
                    # this test to any overlap dropped #20 from 24 figures to
                    # 10, since a gutter caption is *inside* the figure's
                    # vertical span and every such pairing then lost its
                    # dedicated cost rule.  See ABUT_TOL.
                    if c['bottom'] <= ln['top']:
                        d = ln['top'] - c['bottom']; side = 'above'
                    elif c['top'] >= ln['bottom']:
                        d = c['top'] - ln['bottom']; side = 'below'
                    elif c['bottom'] <= ln['top'] + ABUT_TOL and ho > 0:
                        # Figure sits ABOVE and only grazes the caption's top
                        # edge: an abutment, not a side pairing.
                        # `ho > 0` (the boxes overlap HORIZONTALLY) is what
                        # separates this from #20's gutter captions, whose
                        # label sits at x=51..~180 while the figure starts at
                        # x>=184 -- horizontally DISJOINT, and ~8pt below the
                        # figure's top edge.  Those are genuine side pairings
                        # and must keep the 'side' cost rule; requiring
                        # horizontal overlap restores all 24 of #20's figures
                        # while still repairing #06/#10, where the caption sits
                        # directly under a full-width raster (ho = 230pt).
                        d = 0.0; side = 'above'
                    elif c['top'] >= ln['bottom'] - ABUT_TOL and ho > 0:
                        # Mirror case: figure sits BELOW, grazing the caption's
                        # bottom edge.
                        d = 0.0; side = 'below'
                    else:
                        d = 0.0; side = 'side'
                    # Horizontal reach is judged against the whole graphic BAND
                    # on that side, not just this one cluster.  A wide diagram is
                    # frequently split into several clusters by the union-find
                    # (x-gaps larger than gap_h), and a caption that overlaps the
                    # band but not the particular fragment looked "far away" and
                    # was rejected (#27 p46: caption 6.4.2-2 vs the diagram
                    # fragments at y=47..107 gave hgap 85pt and lost the figure).
                    if side == 'above':
                        _band = [e for e in es if e[4] <= ln['top'] + 2
                                 and e[4] > ln['top'] - 90]
                    elif side == 'below':
                        _band = [e for e in es if e[2] >= ln['bottom'] - 2
                                 and e[2] < ln['bottom'] + 90]
                    else:
                        _band = []
                    if _band:
                        _bx0 = min(e[1] for e in _band)
                        _bx1 = max(e[3] for e in _band)
                        ho_b = min(_bx1, ln['x1']) - max(_bx0, ln['x0'])
                    else:
                        _bx0, _bx1 = c['x0'], c['x1']
                        ho_b = ho
                    hgap = 0.0 if ho > 0 else min(abs(ln['x0'] - c['x1']), abs(c['x0'] - ln['x1']))
                    hgap_b = 0.0 if ho_b > 0 else \
                        min(abs(ln['x0'] - _bx1), abs(_bx0 - ln['x1']))
                    # A caption's first line is often a RUN-ON line that keeps
                    # going into the paragraph (`Figure 1 illustrates ...`), so
                    # its right edge is a poor anchor.  The figure label itself
                    # never runs on: measure the gap from the caption's LEFT edge
                    # as well and accept a pairing when EITHER edge is close.
                    hgap_l = 0.0 if min(c['x1'], ln['x0']) > max(c['x0'], ln['x0']) \
                        else min(abs(ln['x0'] - c['x1']), abs(c['x0'] - ln['x0']))
                    if side == 'side':
                        # A caption may sit in the LEFT text gutter while its
                        # figure occupies the right column: the horizontal gap is
                        # then a whole text column wide (~180pt) yet the caption
                        # is the label of that very figure.  Requiring a small
                        # hgap rejected every such pair, which is how #20 lost
                        # 13 of 24 figures (p18: three stacked figures, three
                        # left-gutter captions, 0 accepted pairs).
                        # The gap is therefore allowed to be wide when the
                        # caption overlaps this band vertically.  Cost keeps the
                        # wide pairing as the LAST resort: a caption that also
                        # sees a close band still prefers it.
                        _vovl = vo > 0.5 * max(1.0, ln['bottom'] - ln['top'])
                        _near_edge = min(abs(ln['top'] - c['bottom']),
                                         abs(ln['bottom'] - c['top'])) < 60.0
                        _inside = (hgap <= 60 or hgap_l <= 60 or _vovl or _near_edge)
                        if not _inside:
                            continue
                        cost = min(hgap, hgap_l) if (hgap > 60 or hgap_l > 60) else hgap
                    else:
                        # The lone-raster fallback already established that this
                        # caption and this cluster are the page's only possible
                        # partners, so neither the vertical reach nor the
                        # horizontal-align check may veto the pairing.
                        _is_forced = (pno, fno) in _forced
                        if d > 60 and not _is_forced:
                            continue
                        # `above`/`below` already means the boxes are vertically
                        # disjoint (`vo < 0`), so a `vo` test here would reject
                        # every legitimate caption; only the horizontal gap and
                        # the column-start alignment are meaningful.  Note also
                        # that an indented figure (cl.x0 = 105.7) does not
                        # overlap a short caption line (`FIGURE 1` x1 = 85.4),
                        # so the box-to-box gap hgap - not hgap_l - is the test.
                        _align = abs(ln['x0'] - c['x0']) < 24
                        if ho <= 0 and min(hgap, hgap_l, hgap_b) > 40 \
                                and not _align and not _is_forced:
                            continue
                        # **代价函数**：垂直距离 + 0.5 x 水平间隙
                        cost = d + 0.5 * hgap
                    # 面积惩罚：装饰性小位图/碎片不得抢占真图（#13 图1=57x61、#16 图1=472x68）
                    area = (c['x1'] - c['x0']) * (c['bottom'] - c['top'])
                    if area < 4000:
                        cost += 300
                    pairs.append((cost, ci, fno, side, ln))
            pairs.sort(key=lambda p: p[0])
            # Region-level de-duplication.
            # `used_c` (one CLUSTER serves one figure) is what dropped figures:
            # when several captions shared a cluster the cheapest one locked the
            # cluster and every other caption could never fire.  Ownership is now
            # decided on the FINAL crop rectangle, so a cluster may legitimately
            # serve several captions (each gets its own band from
            # split_cluster_by_caps) while the same pixels are still never
            # written twice.
            used_c, used_k = set(), set()
            emitted = []            # final boxes already produced for this page
            claimed = []            # element boxes already owned by a figure
            for cost, ci, fno, side, ln in pairs:
                if (fno, id(ln)) in used_k or fno in mapping:
                    continue
                if ci in used_c:
                    # This band is taken; a different band may still serve this
                    # caption, so only this pairing is skipped.
                    continue
                c = cand_clusters[ci]
                sliced = ci in sliced_ids
                dst = os.path.join(outdir, 'fig%s.png' % fno)
                # ---- 图区并集增长：把图题一侧「图区带」内全部图形元素并入裁剪框 ----
                # 多子图拼贴的各片间距常 >9pt（并查集合不上）；按图题带取并集可一次收全
                _col = [l for l in L if l['col'] == ln['col']]
                _above = [l['bottom'] for l in _col if l['bottom'] <= ln['top'] and l is not ln]
                _below = [l['top'] for l in _col if l['top'] >= ln['bottom'] and l is not ln]
                if side == 'above':
                    _bt = max([b for b in _above if b < c['top'] + 1] or [ttop - 20])
                    _bb = min(ln['top'] - 1, c['bottom'] + 40)
                else:
                    _bt = max(c['top'] - 40, ln['bottom'] + 1)
                    _bb = min([b for b in _below if b > c['bottom'] - 1] or [tbot + 20])
                if _bb - _bt < 12:
                    _bt, _bb = c['top'] - 4, c['bottom'] + 14
                # A SLICED band must not look outside itself: the text-line walk
                # above was written for a whole cluster and happily reaches the
                # neighbouring figure's band, which then produced boxes whose
                # bottom sat above their top (#17 p7 fig9: bt=139.9 from the
                # unsliced cluster while the band started at 557.1).
                if sliced:
                    _bt = max(_bt, c['top'] - 6)
                    _bb = min(_bb, c['bottom'] + 6)
                # Caption fence: the band must not grow past another caption on
                # this page that already owns its own figure.  Without this a
                # tall diagram reached page top and swallowed the small figure
                # whose caption sat above it (#27 p46: 6.4.2-3 grew to y=221 and
                # covered 6.4.2-2's shape, which then had to be rejected as a
                # duplicate region).
                for (_ono, _oln) in caps:
                    if _oln is ln:
                        continue
                    # Only captions lying BETWEEN this figure's caption and the
                    # band edge may fence it.  A caption on the far side of the
                    # page (or below this caption when the figure sits above it)
                    # must not clamp the band, which otherwise inverted the box
                    # (#17 p7 fig9: bot was clamped above top -> degenerate).
                    if side == 'above':
                        if ln['bottom'] < _oln['top'] < _bt:
                            _bt = _oln['top']
                    elif side == 'below':
                        if _bb < _oln['bottom'] < ln['top']:
                            _bb = _oln['bottom']
                if _bb - _bt < 12:
                    _bt, _bb = c['top'] - 4, c['bottom'] + 14
                _grp = [e for e in es
                        if e[4] > _bt - 2 and e[2] < _bb + 2
                        and min(e[3], c['x1'] + 60) > max(e[1], c['x0'] - 60)]
                # Element exclusivity: an element that already belongs to a
                # figure emitted on this page must not be pulled into this band.
                # Without it a tall diagram's band absorbed the small shape that
                # is another caption's figure, and that figure could then only be
                # rejected as a duplicate region (#27 p46 6.4.2-3 vs 6.4.2-2).
                # When nothing remains the band simply stays as this cluster -
                # `_grp` keeps element tuples so the union below stays valid.
                if emitted and _grp:
                    _keep = [e for e in _grp if not any(
                        e[3] > bx0 + 2 and e[1] < bx1 - 2 and
                        e[4] > bt0 + 2 and e[2] < bb0 - 2
                        for (bx0, bt0, bx1, bb0) in claimed)]
                    if _keep:
                        _grp = _keep
                    else:
                        _grp = [e for e in es
                                if abs(e[1] - c['x0']) < 1 and abs(e[2] - c['top']) < 1]
                if _grp:
                    _gx0 = min(e[1] for e in _grp); _gx1 = max(e[3] for e in _grp)
                    _mid = page.rect.width / 2.0
                    _lim = (page.rect.width - 36) if (c['x0'] < _mid - 20 and c['x1'] > _mid + 20) \
                        else (page.rect.width - 36 if ln['col'] == 1 else _mid - 6)
                    _gx1 = min(_gx1, _lim)
                    if (_gx1 - _gx0) > 0.6 * page.rect.width:
                        _grp = [c]
                # 相邻位图检测：簇旁边还有位图时不做原生分流（那是拼贴图的一片）
                _adj = 0
                for im in page.get_image_info():
                    if is_decor(im['bbox']):
                        continue
                    if abs(im['bbox'][0] - c['x0']) < 1 and abs(im['bbox'][1] - c['top']) < 1:
                        continue
                    if im['bbox'][2] > c['x0'] - 12 and im['bbox'][0] < c['x1'] + 12 and \
                       im['bbox'][3] > c['top'] - 12 and im['bbox'][1] < c['bottom'] + 12:
                        _adj += 1
                _sel = _grp if (isinstance(_grp, list) and len(_grp) > 1) else None
                if _sel is None and c['n'] == 1 and c['nimg'] == 1 and _adj == 0:
                    nat = native_for(pno, (c['x0'], c['top'], c['x1'], c['bottom']))
                    if nat:
                        nbox = (c['x0'], c['top'], c['x1'], c['bottom'])
                        if any(bbox_overlap(nbox, e) for e in emitted):
                            continue
                        shutil.copyfile(nat, dst)
                        emitted.append(nbox)
                        claimed.append(nbox)
                        used_c.add(ci); used_k.add((fno, id(ln)))
                        fw = (c['x1'] - c['x0']) > pw * 0.55
                        mapping[fno] = {'page': pno, 'fullwidth': bool(fw), 'native': True,
                                        'caption_pos': ('bottom' if c['bottom'] <= ln['top']
                                                        else 'top'),
                                        'bbox': [round(c['x0']), round(c['top']),
                                                 round(c['x1']), round(c['bottom'])],
                                        'cost': round(cost, 1)}
                        report.append((fno, pno, 'NATIVE', round(cost, 1), c['n'], c['nimg'],
                                       round(c['x1'] - c['x0'], 1), round(c['bottom'] - c['top'], 1)))
                        continue
                mid = page.rect.width / 2.0
                cross = (c['x0'] < mid - 20 and c['x1'] > mid + 20)
                # Column clamp follows the CLUSTER, not the caption.  A figure in
                # the right column may carry its label in the left text gutter
                # (#20 fig12: caption x0=51 col=0, figure x0=300 col=1); clamping
                # to the caption's column collapsed the crop to a 0pt sliver and
                # the figure was reported `degenerate`.
                if cross:
                    clo, chi = lc[0], rc[1]
                elif c['x0'] >= mid:
                    clo, chi = rc
                elif c['x1'] <= mid:
                    clo, chi = lc
                else:
                    clo, chi = (lc if ln['col'] == 0 else rc)
                if _sel:
                    bx0 = min(e[1] for e in _sel); bx1 = max(e[3] for e in _sel)
                    btop = min(e[2] for e in _sel); bbot = max(e[4] for e in _sel)
                else:
                    bx0, bx1, btop, bbot = c['x0'], c['x1'], c['top'], c['bottom']
                # Band clamp: when a vertically stacked strip was sliced into
                # per-caption bands, the element union above would grow the crop
                # back over the neighbouring figure.  Never let a sliced band
                # claim pixels outside its own band.
                if sliced:
                    btop = max(btop, c['top'] - 2)
                    bbot = min(bbot, c['bottom'] + 2)
                x0 = max(clo, bx0 - 16)
                x1 = min(chi, bx1 + 8)
                top = btop - 4
                bot = bbot + 14
                if side == 'above':
                    bot = min(bot, ln['top'] - 1)
                    top = max(top, ttop - 20)
                else:
                    top = max(top, ln['bottom'] + 1)
                    bot = min(bot, tbot + 20)
                # ---- CLIP-EXTEND (see CLIP_EXTEND_MAX / clip_extent) ----------
                # Runs on the FINAL box, after every clamp that can shrink it
                # (column clamp, band clamp, caption clamp), so it is the last
                # word on the crop's horizontal extent.  Placed here on purpose:
                # an earlier hook would be overwritten by the `x1 = min(chi,
                # bx1 + 8)` line above and the repair would be a silent no-op --
                # the exact failure this repo has hit before with `is_decor`.
                #
                # ONLY grows.  A figure whose text already fits (slack >= 0)
                # returns None and its box is left bit-for-bit identical; that
                # is what keeps the Lead-verified control fig6.4.2-1 (slack
                # +103.1) unchanged.
                _ce = clip_extent(page, (x0, top, x1, bot))
                _chk['ce_seen'] = _chk.get('ce_seen', 0) + 1
                if _ce is not None:
                    _ce_lo, _ce_hi, _ce_rep = _ce
                    _grow_l = max(0.0, x0 - _ce_lo)
                    _grow_r = max(0.0, _ce_hi - x1)
                    if _ce_rep['capped']:
                        # Soft warning, NO blind extension: a jump this large
                        # means the cluster is mis-paired or the text belongs to
                        # another element, and widening would drag in prose.
                        _chk['ce_capped'] = _chk.get('ce_capped', 0) + 1
                        _chk.setdefault('ce_capped_all', []).append(
                            (fno, pno, _ce_rep['grow_l'], _ce_rep['grow_r']))
                        print('CLIP-EXTEND-WARN: fig%s p%d needs grow L=%.1fpt '
                              'R=%.1fpt > cap %.0fpt -> NOT extended '
                              '(soft warning, review by eye)'
                              % (fno, pno, _ce_rep['grow_l'], _ce_rep['grow_r'],
                                 CLIP_EXTEND_MAX), file=sys.stderr)
                    else:
                        if _grow_r > CLIP_EXTEND_EPS or _grow_l > CLIP_EXTEND_EPS:
                            _chk['ce_ext'] = _chk.get('ce_ext', 0) + 1
                            _chk.setdefault('ce_all', []).append(
                                (fno, pno, round(x0, 1), round(x1, 1),
                                 round(x0 - _grow_l, 1), round(x1 + _grow_r, 1),
                                 _ce_rep['n_l'], _ce_rep['n_r']))
                            if max(_grow_l, _grow_r) > CLIP_EXTEND_SOFT_WARN:
                                print('CLIP-EXTEND-WARN: fig%s p%d grew %.1fpt '
                                      '(> soft threshold %.0fpt) -- verify by eye'
                                      % (fno, pno, max(_grow_l, _grow_r),
                                         CLIP_EXTEND_SOFT_WARN), file=sys.stderr)
                        x0 = min(x0, _ce_lo)
                        x1 = max(x1, _ce_hi)
                if x1 - x0 < 12 or bot - top < 12:
                    unmatched.append((fno, pno, 'degenerate'))
                    _chk['blank'] += 1
                    _chk['blank_all'].append((fno, pno, 'degenerate',
                                              [round(v, 1) for v in (x0, top, x1, bot)]))
                    continue
                # ---- TEXT-AS-FIG SIDE RECOVERY ----------------------------------
                # When the artwork and the body text on the far side of a caption
                # are joined into ONE cluster (they touch, so both gaps are under
                # the clustering thresholds), the caption is a single candidate
                # and the box can end up on the WRONG side of it -- the prose
                # side.  #03 p2 is the measured case: one cluster of 807 elements
                # covering x[9.7,524.8] y[66.1,363.7], the real Figure 1
                # flowchart above the caption (y=309.7) and two columns of body
                # text below it.  The emitted crop was 499x60pt of pure prose
                # with pairing cost 0.0.
                #
                # The repair is evidence-based, not heuristic: count the elements
                # that actually sit on each side of the caption inside this
                # cluster and keep the side that carries the artwork.  Only an
                # EMPTY or clearly one-sided box is flipped, so a normal figure
                # (whose box already holds its own cluster) is never touched.
                def _side_elems(_t, _b):
                    return [e for e in es
                            if e[2] >= _t - 1 and e[4] <= _b + 1
                            and min(e[3], c['x1'] + 8) > max(e[1], c['x0'] - 16)]
                _here = _side_elems(top, bot)
                _n_here = len(_here)
                # Artwork = images plus vector art, the same two signals the
                # TEXT-AS-FIG probe uses.  Prose contributes neither.
                _art_here = sum(1 for e in _here
                                if e[0] == 'img' or len(e) > 5)
                if _n_here < 4 or _art_here == 0:
                    # Try the other side of the caption, bounded by the cluster.
                    if side != 'above':
                        _alt_t, _alt_b = max(c['top'] - 4, ttop - 20), ln['top'] - 1
                    else:
                        _alt_t, _alt_b = ln['bottom'] + 1, min(c['bottom'] + 14,
                                                               tbot + 20)
                    if _alt_b - _alt_t >= 12:
                        _alt = _side_elems(_alt_t, _alt_b)
                        _art_alt = sum(1 for e in _alt
                                       if e[0] == 'img' or len(e) > 5)
                        if _art_alt > _art_here and len(_alt) >= max(4, _n_here):
                            _chk['taf_flip'] = _chk.get('taf_flip', 0) + 1
                            _chk.setdefault('taf_flip_all', []).append(
                                (fno, pno, round(top, 1), round(bot, 1),
                                 round(_alt_t, 1), round(_alt_b, 1),
                                 _n_here, len(_alt)))
                            top, bot = _alt_t, _alt_b
                            side = 'above' if side != 'above' else 'below'
                # ---- PAGE-CLIP INVARIANT: the emitted box must be inside the
                # page.  Everything upstream (cluster box, band walk, caption
                # clamp) can push a crop out of the page when the source
                # geometry itself is oversized; the renderer then silently
                # returns whatever sliver intersects the page -- typically
                # blank paper.  Clamp, and RECORD it, so a reviewer can see
                # that the geometry was repaired rather than believing the
                # cluster was always sane.
                _cb, _crep = clip_to_page((x0, top, x1, bot), page.rect)
                if _crep is not None:
                    if _crep.get('collapsed') or (_cb[2] - _cb[0]) < 12 \
                            or (_cb[3] - _cb[1]) < 12:
                        unmatched.append((fno, pno, 'out-of-page-collapsed'))
                        _chk['blank'] += 1
                        _chk['blank_all'].append(
                            (fno, pno, 'out-of-page-collapsed', _crep['raw']))
                        continue
                    _chk['clamped'] += 1
                    _chk['clamped_all'].append((fno, pno, _crep))
                    x0, top, x1, bot = _cb
                rbox = (x0, top, x1, bot)
                # ---- TEXT-AS-FIG probe (runs for EVERY crop, no early exit) --
                # Judged on the FINAL box and the pairing that produced it; see
                # _text_as_fig_probe for the calibration.
                _taf_v, _taf_ev = _text_as_fig_probe(
                    fitz, page, rbox, cost, c['n'])
                _chk['taf_seen'] += 1
                if _taf_v != 'none':
                    if _taf_v == 'confirm':
                        _chk['taf_conf'] += 1
                        _chk['taf_conf_all'].append((fno, pno, _taf_ev))
                    _chk['taf_suspect'] += 1
                    _chk['taf_all'].append((fno, pno, _taf_v, _taf_ev))
                if any(bbox_overlap(rbox, e) for e in emitted):
                    # Exactly the pixels of a figure already emitted on this
                    # page: this is a duplicate pairing, not a second figure.
                    unmatched.append((fno, pno, 'dup-region'))
                    continue
                got = render_region(pdf, pno, rbox, args.dpi, dst,
                                    pdftoppm, tmp, 'c%s' % fno, render_ok)
                if not got:
                    unmatched.append((fno, pno, 'render-empty'))
                    continue
                # ---- CONTENT SANITY: never hand back a blank image ----
                # A crop of pure background is not a figure, whatever the
                # geometry said.  #08 fig15 was emitted as a 4410x159 pure
                # white bar (std=0.0) and only the downstream gate noticed;
                # the extractor must refuse it and SAY WHY, so the gate can
                # report the real cause instead of a generic fig-anomaly.
                _cstd, _cink = _ink_ratio(dst, args.dpi)
                if _cstd is not None and _cstd < CROP_MIN_STD:
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
                    _why = ('blank-crop:std=%.2f ink=%s cluster=%s cost=%s'
                            % (_cstd,
                               'n/a' if _cink is None else '%.4f' % _cink,
                               [round(v, 1) for v in (c['x0'], c['top'],
                                                      c['x1'], c['bottom'])],
                               round(cost, 1)))
                    unmatched.append((fno, pno, _why))
                    _chk['blank'] += 1
                    _chk['blank_all'].append((fno, pno, _why,
                                              [round(v, 1) for v in rbox]))
                    continue
                emitted.append(rbox)
                if _sel is not None:
                    for e in _sel:
                        claimed.append((e[1], e[2], e[3], e[4])
                                       if isinstance(e, tuple) else
                                       (e['x0'], e['top'], e['x1'], e['bottom']))
                else:
                    claimed.append((c['x0'], c['top'], c['x1'], c['bottom']))
                used_c.add(ci); used_k.add((fno, id(ln)))
                fw = (x1 - x0) > pw * 0.55
                mapping[fno] = {'page': pno, 'fullwidth': bool(fw), 'native': False,
                                'caption_pos': 'bottom' if side == 'above' else 'top',
                                'cluster': [round(c['x0']), round(c['top']),
                                            round(c['x1']), round(c['bottom'])],
                                'n_el': c['n'], 'n_img': c['nimg'], 'cost': round(cost, 1)}
                report.append((fno, pno, side, round(cost, 1), c['n'], c['nimg'],
                               round(x1 - x0, 1), round(bot - top, 1)))

    match_pass(True)    # 第一遍：严格图题（避免正文交叉引用误配）
    match_pass(False)   # 第二遍：宽松图题兜底

    # ---- 兜底 1：图题存在但无图形簇 -> 取图题旁最大空白带 ----
    for fno in sorted(found_caps):
        if fno in mapping or not pdftoppm:
            continue
        for (pno, ln, strict) in cap_lines.get(fno, []):
            page = doc[pno - 1]
            L = page_cache[pno]
            col0, col1 = (lc[0], lc[1]) if ln['col'] == 0 else (rc[0], rc[1])
            others = [l for l in L if l['col'] == ln['col'] and abs(l['top'] - ln['top']) > 2]
            above = [l for l in others if l['bottom'] <= ln['top']]
            below = [l for l in others if l['top'] >= ln['bottom']]
            cands = [('dn', ln['bottom'], min([l['top'] for l in below], default=page.rect.height - 40)),
                     ('up', max([l['bottom'] for l in above], default=36), ln['top'])]
            cands = [c for c in cands if c[2] - c[1] >= 40]
            if not cands:
                continue
            side, bt, bb = max(cands, key=lambda c: c[2] - c[1])
            bt = max(bt - 4, 36); bb = min(bb + 2, page.rect.height - 40)
            dst = os.path.join(outdir, 'fig%s.png' % fno)
            got = render_region(pdf, pno, (col0, bt, col1, bb), args.dpi, dst, pdftoppm, tmp, 'v%s' % fno, render_ok)
            if got:
                mapping[fno] = {'page': pno, 'fullwidth': bool((col1 - col0) > page.rect.width * 0.55),
                                'native': False,
                                'vector_band': [round(col0), round(bt), round(col1), round(bb)]}
                report.append((fno, pno, 'VECTORBAND', 0, 0, 0,
                               round(col1 - col0, 1), round(bb - bt, 1)))
                break

    missing = sorted(set(expect) - {str(k) for k in mapping}) if expect else []

    # ---- 兜底 2：位置兜底（图号与未占用的大簇数量相等时按页序配对）----
    used_boxes = set()
    for _v in mapping.values():
        _b = _v.get('bbox') or _v.get('cluster')
        if _b:
            used_boxes.add((_v.get('page'), round(_b[0]), round(_b[1])))
    leftover = [(p, c) for (p, c) in all_clusters
                if (p, round(c['x0']), round(c['top'])) not in used_boxes]
    leftover.sort(key=lambda t: (t[0], t[1]['top']))
    if expect and missing and len(missing) == len(leftover) and pdftoppm:
        for fno, c in zip(missing, [dict(x[1], page=x[0]) for x in leftover]):
            pno = c['page']
            page = doc[pno - 1]
            x0 = max(36, c['x0'] - 8); x1 = min(page.rect.width - 36, c['x1'] + 8)
            top = c['top'] - 4; bot = c['bottom'] + 14
            dst = os.path.join(outdir, 'fig%s.png' % fno)
            got = render_region(pdf, pno, (x0, top, x1, bot), args.dpi, dst, pdftoppm, tmp, 'f%s' % fno, render_ok)
            if got:
                mapping[fno] = {'page': pno, 'fullwidth': bool((x1 - x0) > page.rect.width * 0.55),
                                'native': False, 'posfallback': True,
                                'bbox': [round(x0), round(top), round(x1), round(bot)]}
                report.append((fno, pno, 'POSFALLBACK', 0, c['n'], c['nimg'],
                               round(x1 - x0, 1), round(bot - top, 1)))
        missing = sorted(set(expect) - {str(k) for k in mapping})

    # ---- 兜底 3：人工预置 override（已在上文解析并自校验；显式优先） ----
    # Snapshot the automatic result first: an override must only ever ADD a
    # missing figure, never replace or drop one that the algorithm already got.
    stage2_keys = {str(k) for k in mapping}
    ov_used_now = []
    if ov_entries:
        if not pdftoppm:
            # NOT fatal: render_region() has a built-in PyMuPDF fallback
            # (render_region_fitz). Breaking here made the documented
            # "no poppler needed" path unreachable, so every override silently
            # produced nothing on a poppler-less host.
            print('NOTE: pdftoppm not found; using the built-in PyMuPDF renderer '
                  'for overrides.', file=sys.stderr)
        for k in sorted(ov_entries):
            spec = ov_entries[k]
            pno = spec['page']
            x0, top, x1, bot = spec['box']
            d = spec['dpi']
            dst = os.path.join(outdir, 'fig%s.png' % k)
            got = render_region(pdf, pno, (x0, top, x1, bot), d, dst, pdftoppm,
                                tmp, 'o%s' % k, render_ok)
            if not got:
                ov_rejected.append((os.path.basename(spec['source']), k, 'render-empty'))
                continue
            # ---- 自校验：不能拿一张空白图冒充「提取成功」 ----
            page = doc[pno - 1]
            _st, _ink = _ink_ratio(dst, d)
            _blank = (_st is not None and _st < OVERRIDE_MIN_STD) or \
                     (_ink is not None and _ink < OVERRIDE_MIN_INK)
            if _blank:
                ov_rejected.append((os.path.basename(spec['source']), k,
                                    'blank-image:std=%s ink=%.4f box=%s page=%d'
                                    % ('n/a' if _st is None else '%.1f' % _st,
                                       _ink if _ink is not None else -1,
                                       [round(v, 1) for v in (x0, top, x1, bot)], pno)))
                print('ERROR: override 图 %s 抽出的是**空白图**（std=%s ink=%s）-> 拒绝采用。\n'
                      '       bbox=%s page=%d 很可能是套用了别处的坐标；'
                      '请核对页号与坐标（--list-captions / 渲染该页目视）。'
                      % (k, 'n/a' if _st is None else '%.1f' % _st,
                         'n/a' if _ink is None else '%.4f' % _ink,
                         [round(v, 1) for v in (x0, top, x1, bot)], pno),
                      file=sys.stderr)
                try:
                    os.remove(dst)
                except OSError:
                    pass
                continue
            mapping[str(k)] = {'page': pno, 'native': False, 'override': True,
                               'override_source': (os.path.relpath(spec['source'])
                                                   if os.path.isabs(spec['source'])
                                                   else spec['source']),
                               'caption_pos': spec['caption_pos'],
                               'bbox': [round(x0), round(top), round(x1), round(bot)],
                               'std': None if _st is None else round(_st, 2),
                               'ink': None if _ink is None else round(_ink, 4)}
            if spec['notes']:
                print('NOTE: override 图 %s %s' % (k, spec['notes']))
            ov_used_now.append(str(k))
            report.append((k, pno, 'OVERRIDE', 0, 0, 0, round(x1 - x0, 1), round(bot - top, 1)))
        missing = sorted(set(expect) - {str(k) for k in mapping}) if expect else []

    # ---- 兜底 4：编号/覆盖守卫 ----
    # Two ways a HAND-WRITTEN file can silently make the result worse:
    #   (a) it is keyed by a DIFFERENT numbering than the run is using
    #       ("1" when the figure in scope is "4-1"), so it adds an out-of-scope
    #       figure and the real one stays missing;
    #   (b) it OVERWRITES a figure the algorithm had already extracted correctly,
    #       replacing a machine-verified crop with a hand-guessed box.  Adding is
    #       the intended use; replacing is almost always a mistake.
    # Neither may pass unremarked: (a) leaves a hole, (b) downgrades quality.
    lost = sorted(set(stage2_keys) - {str(k) for k in mapping})
    if lost:
        print('ERROR: override 使已提取的图号 %s 从结果中消失（编号口径不一致？）。\n'
              '       NEXT: 核对 override 的键与 --expect 的口径（如 1 vs 4-1），'
              '或把该条目移出 override 文件。' % ','.join(lost), file=sys.stderr)
        ov_rejected.append(('(regression-guard)', ','.join(lost),
                            'override-shadowed-an-extracted-figure'))
        for _k in lost:
            _p = os.path.join(outdir, 'fig%s.png' % _k)
            if os.path.isfile(_p):
                os.remove(_p)
    replaced = sorted(set(stage2_keys) & set(ov_used_now))
    if replaced and not args.allow_override_replace:
        print('ERROR: override 覆盖了**算法已成功提取**的图号 %s —— '
              '人工 bbox 只是自动提取的兜底，不该顶掉一个已经验证过的裁剪。\n'
              '       NEXT: 确认图号口径（如 1 vs 4-1）是否写错；'
              '确实要用人工框顶替时，显式加 --allow-override-replace。'
              % ','.join(replaced), file=sys.stderr)
        ov_rejected.append(('(regression-guard)', ','.join(replaced),
                            'override-replaced-an-auto-extracted-figure'))
        for _k in replaced:
            _p = os.path.join(outdir, 'fig%s.png' % _k)
            if os.path.isfile(_p):
                os.remove(_p)
            mapping.pop(str(_k), None)
        missing = sorted(set(expect) - {str(k) for k in mapping}) if expect else []

    with open(os.path.join(outdir, 'figs.json'), 'w', encoding='utf-8') as fh:
        json.dump({str(k): v for k, v in mapping.items()}, fh, ensure_ascii=False, indent=1)

    for r in sorted(report, key=lambda x: str(x[0])):
        print('fig%-5s p%-4s %-12s cost=%-7s els=%-4s imgs=%-3s crop=%.0fx%.0fpt' % r)
    print('CAPTIONS-IN-PDF: %s' % json.dumps({k: found_caps[k] for k in sorted(found_caps)},
                                             ensure_ascii=False))
    print('MAPPED: %s' % json.dumps(sorted(mapping.keys()), ensure_ascii=False))
    print('UNMATCHED: %s' % json.dumps(unmatched, ensure_ascii=False))
    print('MISSING-EXPECT: %s' % json.dumps(missing, ensure_ascii=False))
    # ---- page-clip / content self-check (see the counters in run()) ----
    # Printed UNCONDITIONALLY, including the zeros: a reviewer must be able to
    # tell "the new guards ran and found nothing" apart from "the new guards
    # never ran", which is the only way to trust a green result here.
    print('FIGCLIP OVERSIZED=%d CLAMPED=%d BLANK=%d'
          % (_chk['oob'], _chk['clamped'], _chk['blank']))
    # ---- ZERO-THICKNESS STROKE + LISTING-PAGE self-check ----
    # Printed UNCONDITIONALLY, zeros included, so "the new path ran and kept
    # nothing" can be told apart from "the new path never ran" -- this repo's
    # recurring silent-no-op trap.
    #   STROKE-KEPT  : stroked lines the element collector would previously have
    #                  thrown away and has now kept (see stroke_extent).
    #   LISTING-SKIP : ToF/LoF pages excluded from the caption registry because
    #                  their caption-looking rows are an index, not figures
    #                  (see is_listing_page).
    print('STROKE-KEPT LINES=%d LISTING-SKIP PAGES=%d STROKE-TABREJECT=%d'
          % (_chk['stroke_kept'], _chk['listing_pages'],
             _chk.get('stroketab', 0)))
    for (_p, _n) in _chk['listing_all'][:8]:
        print('LISTING-SKIP-DETAIL: p%d caption-rows=%d (index page, no figure)' % (_p, _n))
    # ---- RASTER self-check: printed UNCONDITIONALLY, zeros included ----
    # SEEN counts every embedded bitmap on every page that reached the element
    # collector; KEPT counts those admitted as artwork; SMALL/DECOR are the two
    # reasons one was refused.  When SEEN is 0 on a document that visibly has
    # raster figures, the raster path did not run -- exactly the failure this
    # line exists to make visible.
    print('RASTER-ELEMS SEEN=%d KEPT=%d SMALL=%d DECOR=%d'
          % (_chk['raster_seen'], _chk['raster_kept'],
             _chk['raster_small'], _chk['raster_decor']))
    # ---- DECOR tile-union self-check: printed UNCONDITIONALLY, zeros included ----
    # GROUPS = multi-bitmap tile groups found on any page; RESCUED = bitmaps that
    # clause B would have vetoed but that the group UNION admitted (#11 p5's 8
    # tiles -> 8 rescues); DUST = thin bitmaps still refused because their own
    # area is below DECOR_TILE_MIN_AREA even though they chain into a big union;
    # LOOSE = thin bitmaps whose group overlap exceeds DECOR_TILE_FILL_MAX, i.e.
    # a chained clump rather than a mosaic; AMBIG = boxes carried by several
    # pages whose groups disagreed, resolved conservatively as unmatched;
    # ALONE-SMALL = thin bitmaps refused because they stand alone (#17 p3's
    # 33x28pt logo).  A run reporting GROUPS=0 on a document that visibly
    # contains a collage means the union table was never consulted -- the
    # silent-no-op that turned a previous attempt at this fix into 0 figures.
    print('DECOR-TILEUNION GROUPS=%d RESCUED=%d DUST=%d LOOSE=%d AMBIG=%d '
          'ALONE-SMALL=%d'
          % (DECOR_TILE_STATS['groups'], DECOR_TILE_STATS['rescued'],
             DECOR_TILE_STATS['dust'], DECOR_TILE_STATS['loose'],
             DECOR_TILE_STATS['ambig'], DECOR_TILE_STATS['alone_small']))
    for (_p, _b) in _chk['raster_skip_all'][:6]:
        print('RASTER-SKIP: p%d bbox=%s (below %.0fpt/side or %.2f%% area)'
              % (_p, _b, RASTER_MIN_SIDE, RASTER_MIN_AREA_FRAC * 100))
    # LONE counts the pages where a distant caption was paired with the page's
    # only artwork raster (see LONE_RASTER_MAX_REACH).
    print('RASTER-LONE PAIRED=%d' % _chk['lone'])
    for (_p, _f, _g, _b) in _chk['lone_all'][:6]:
        print('RASTER-LONE-DETAIL: fig%s p%d gap=%.1fpt cluster=%s'
              % (_f, _p, _g, _b))
    # ---- TEXT-AS-FIG: the check that can see "the crop is prose, not art" ----
    # SEEN is incremented for EVERY emitted crop, so `SEEN=0` proves the branch
    # never executed while `SEEN>0 SUSPECT=0` is a real clean reading.  Without
    # an unconditional counter a broken branch reads exactly like a clean one.
    print('TEXT-AS-FIG SEEN=%d SUSPECT=%d CONFIRM=%d'
          % (_chk['taf_seen'], _chk['taf_suspect'], _chk['taf_conf']))
    # SIDE-RECOVERED counts the boxes that were moved to the other side of their
    # caption because the chosen side held no artwork.  Also unconditional: a
    # repair that changes a crop must never be silent, and a zero here is only
    # meaningful if the counter was actually reached.
    print('TEXT-AS-FIG SIDE-RECOVERED=%d' % _chk.get('taf_flip', 0))
    # ---- CLIP-EXTEND self-check: printed UNCONDITIONALLY, zeros included ----
    # SEEN = crops that reached the hook (proves the branch is wired in and
    # runs for EVERY emitted crop, so a zero here can never be confused with a
    # silent no-op).  EXTENDED = crops actually grown; CAPPED = crops whose
    # needed growth exceeded CLIP_EXTEND_MAX and were therefore left alone with
    # a soft warning.  Cross-check required by the defect report: on #27 the
    # figure set has 6 genuine clips, so EXTENDED must be a small number, NOT
    # the whole document -- an EXTENDED equal to the figure count is the
    # signature of over-expansion and must fail review.
    print('CLIP-EXTEND SEEN=%d EXTENDED=%d CAPPED=%d'
          % (_chk.get('ce_seen', 0), _chk.get('ce_ext', 0),
             _chk.get('ce_capped', 0)))
    for (_f, _p, _ox0, _ox1, _nx0, _nx1, _nl, _nr) in _chk.get('ce_all', [])[:12]:
        print('CLIP-EXTEND-DETAIL: fig%s p%d x[%.1f,%.1f] -> [%.1f,%.1f] '
              '(grew L=%.1f R=%.1f, words %d/%d)'
              % (_f, _p, _ox0, _ox1, _nx0, _nx1, _ox0 - _nx0, _nx1 - _ox1,
                 _nl, _nr))
    for (_f, _p, _gl, _gr) in _chk.get('ce_capped_all', [])[:6]:
        print('CLIP-EXTEND-CAPPED: fig%s p%d needs L=%.1fpt R=%.1fpt '
              '(> cap %.0fpt) -- left unextended, review by eye'
              % (_f, _p, _gl, _gr, CLIP_EXTEND_MAX))
    for (_f, _p, _t0, _b0, _t1, _b1, _nh, _na) in _chk.get('taf_flip_all', [])[:6]:
        print('TEXT-AS-FIG-SIDE: fig%s p%d y[%.1f,%.1f]->[%.1f,%.1f] '
              'els_here=%d els_alt=%d' % (_f, _p, _t0, _b0, _t1, _b1, _nh, _na))
    for (_f, _p, _v, _ev) in _chk['taf_all'][:10]:
        _e = _ev or {}
        print('TEXT-AS-FIG-DETAIL: fig%s p%d verdict=%s cost=%s n_el=%s '
              'chars=%s aspect=%s sample=%r'
              % (_f, _p, _v, _e.get('cost'), _e.get('n_el'),
                 _e.get('chars'), _e.get('aspect'), _e.get('sample')))
    for (_p, _r) in _chk['oob_all'][:10]:
        print('FIGCLIP-OVERSIZED: p%d overflow=%.1fpt raw=%s clipped=%s page=%s'
              % (_p, _r['overflow'], _r['raw'],
                 _r.get('clipped'), _r.get('page')))
    for (_f, _p, _r) in _chk['clamped_all'][:10]:
        print('FIGCLIP-CLAMPED: fig%s p%d overflow=%.1fpt raw=%s -> clipped=%s'
              % (_f, _p, _r['overflow'], _r['raw'], _r['clipped']))
    for (_f, _p, _w, _b) in _chk['blank_all'][:10]:
        print('FIGCLIP-BLANK: fig%s p%d %s box=%s' % (_f, _p, _w, _b))
    n_native = sum(1 for v in mapping.values() if v.get('native'))
    print('SUMMARY EXTRACTED=%d EXPECTED=%s MISSING=%d NATIVE=%d RENDERED=%d'
          % (len(mapping), len(expect) if expect else 'n/a', len(missing),
             n_native, len(mapping) - n_native))
    # ---- override 使用情况：让上游门控能区分 auto / override / missing ----
    ov_used = sorted(k for k, v in mapping.items() if v.get('override'))
    ov_meta['used'] = len(ov_used)
    ov_meta['path'] = (winner['path'] if winner else '')
    ov_meta['priority'] = (winner['prank'] if winner else None)
    ov_meta['rejected_total'] = len(ov_rejected)
    n_auto = len(mapping) - len(ov_used)
    print('OVERRIDE USED=%d BY=%s PATH=%s PRIORITY=%s LOADED=%d ENTRIES=%d REJECTED=%d FIGS=%s'
          % (len(ov_used), ov_meta['kind'] or 'none', ov_meta['path'] or '-',
             ov_meta['priority'] if ov_meta['priority'] is not None else '-',
             ov_meta['loaded'], ov_meta['entries'], len(ov_rejected),
             ','.join(ov_used) if ov_used else '-'))
    for (_f, _no, _why) in ov_rejected:
        print('OVERRIDE-REJECTED: fig%s (%s) %s' % (_no, _f, _why))
    print('FIGS TOPO=%s vs_auto=%d/%d verified=%d/%d'
          % ('auto' if not ov_used else 'override:%d' % len(ov_used),
             n_auto, len(expect) if expect else 0, len(mapping),
             len(expect) if expect else 0))
    if ov_used and not ov_meta['auto']:
        print('FIGS-SOURCE: PASS/PARTIAL for %s rests on a HAND-WRITTEN override '
              '(%s), not on automatic extraction.' % (','.join(ov_used),
                                                      os.path.basename(ov_meta['path'])))
    if missing:
        print('NEXT: 缺图号 %s。\n'
              '  · 先看该图题在原件里是否不在文本层（PDF 用图形字体画的图题）→ 用 --list-captions 核对；\n'
              '  · 文本层确实没有图题 → 写 figs_override.json 人工给定 bbox（格式见 --help）；\n'
              '  · 有图题但没配上簇 → 检查是否被表行栈屏障切断，必要时用 override。'
              % ','.join(missing))
    print('FIGS JSON=%s' % json.dumps({str(k): v for k, v in mapping.items()}, ensure_ascii=False))
    doc.close()
    return EXIT_OK if not missing else 1


def list_captions(doc, expect):
    """只列出各页识别到的图题（排查「图题不在文本层」用）。"""
    total = 0
    for pno in range(1, doc.page_count + 1):
        page = doc[pno - 1]
        for ln in build_lines(page):
            txt = ln['text']
            if len(txt) <= 5 or re.search(r'\.{4,}', txt):
                continue
            m = CAP_STRICT.match(txt)
            kind = 'strict'
            if not m:
                m = CAP_LOOSE.match(txt)
                kind = 'loose'
            if not m:
                continue
            no = norm_no(m.group(1))
            if expect is not None and no not in expect:
                continue
            total += 1
            print('CAP page=%d no=%s kind=%s col=%s text=%s' % (pno, no, kind, ln['col'], txt[:70]))
    print('SUMMARY CAPTIONS=%d EXPECTED=%s' % (total, len(expect) if expect else 'n/a'))
    if total == 0:
        print('NEXT: 一个图题都没识别到。\n'
              '  · 该 PDF 可能没有文本层（扫描件）→ 需先 OCR；\n'
              '  · 或图题体例不在支持列表内 → 检查 --expect 是否与 PDF 内编号口径一致。')
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='extract_figs.py',
        description='按图题锚定 + 图形簇聚类从原件提取插图（原生位图无损分流 / 矢量图 600dpi 定界渲染）。',
        epilog='示例：\n'
               '  python scripts/extract_figs.py Paper/x.pdf .work/01/figs --expect 1,2,3\n'
               '  python scripts/extract_figs.py Paper/x.pdf .work/01/figs --expect 1,2'
               ' --override Paper/01.figs_override.json\n'
               '  python scripts/extract_figs.py Paper/x.pdf out --list-captions\n'
               '  python scripts/extract_figs.py Paper/x.pdf out --expect 1 --native-only\n'
               '  python scripts/extract_figs.py Paper/x.pdf out --expect 1 --no-auto-override\n\n'
               'figs_override.json 格式：\n'
               '  {"4": {"page": 12, "box": [x0, top, x1, bottom], "dpi": 600}}\n\n'
               'override 查找顺序（显式优先；找到几份打印几份，只生效最高优先级那份）：\n'
               '  1 --override <PATH>                       显式指定（文件不存在 -> 报错退出）\n'
               '  2 <outdir>/../<NN>.figs_override.json     .work/16.figs_override.json\n'
               '  3 <outdir>/../figs_override.json          旧位置（.work/16/figs_override.json）\n'
               '  4 <原件目录>/<原件名>.figs_override.json   Paper/<stub>.figs_override.json\n'
               '  5 <原件目录>/<NN>.figs_override.json       Paper/16.figs_override.json  <- 规范位置\n'
               '  6 <原件目录>/figs_override.json\n'
               '自动发现的只有 2-6；--no-auto-override 可全部禁用。\n'
               'override 会自校验（页号/框越界/空白图 -> 报错并拒绝该条），\n'
               '且**只增不减**：若它使一个已提取的图号消失，直接判失败。\n\n'
               '结构化输出里的 override 标记：\n'
               '  OVERRIDE USED=N BY=explicit|auto|none PATH=... REJECTED=... FIGS=...\n'
               '  FIGS TOPO=auto | override:N   vs_auto=A/E   verified=V/E\n'
               '  TOPO=auto     全部来自算法自动提取\n'
               '  TOPO=override 有 N 张是人工 bbox（PASS 的含义不同，必须标注）\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pdf', metavar='ORIG.pdf', help='原件 PDF')
    ap.add_argument('outdir', metavar='OUTDIR', help='输出目录（写 fig<N>.png 与 figs.json）')
    ap.add_argument('--expect', metavar='1,2,3',
                    help='期望图号（缺失即门控失败）；编号已归一化，如 4-1、C.3.1.1-1')
    ap.add_argument('--override', metavar='JSON',
                    help='人工 bbox 覆盖文件（显式指定；**文件不存在即报错退出**，'
                         '不再静默回退到自动查找的路径）。'
                         '不指定时才按文档化的顺序自动查找，顺序见 --help 末尾。')
    ap.add_argument('--no-auto-override', action='store_true',
                    help='禁用全部自动发现的 override 位置（只认 --override）。')
    ap.add_argument('--nn', metavar='NN', default='',
                    help='篇号（如 16）：用于自动查找 Paper/<NN>.figs_override.json 这一'
                         '**仓库可追踪**的规范位置。省略时从 <outdir>/.. 的目录名推断。')
    ap.add_argument('--allow-override-replace', action='store_true',
                    help='允许 override 顶替**算法已成功提取**的图号。'
                         '默认**拒绝**并报错：人工 bbox 是自动提取的兜底，'
                         '顶掉一个已验证的裁剪通常意味着图号口径写错了'
                         '（例如把 4-1 写成 4）。')
    ap.add_argument('--override-priority', metavar='R1,R2,...', default=None,
                    help='多份 override 同时存在时的优先级（从左到右，越靠前越优先）。'
                         '取值：explicit, paper-nn, paper-stem, parent-nn, '
                         'parent-legacy, paper-other。'
                         '默认 explicit,paper-nn,paper-stem,parent-nn,parent-legacy,paper-other')
    ap.add_argument('--dpi', type=int, default=int(os.environ.get('CROP_DPI', '600')),
                    help='区域渲染 dpi（默认 600，也可用环境变量 CROP_DPI）')
    ap.add_argument('--list-captions', action='store_true', help='只列出识别到的图题后退出（排查用）')
    ap.add_argument('--native-only', action='store_true',
                    help='真正跳过区域渲染：只用原生位图（pdfimages）分流，'
                         '需要渲染的图直接记为未提取。不再要求 pdftoppm。')
    ap.add_argument('--no-qpdf', action='store_true', help='跳过 qpdf xref 归一化')
    ap.add_argument('--pdftoppm', metavar='PATH', help='pdftoppm 可执行文件路径')
    ap.add_argument('--pdfimages', metavar='PATH', help='pdfimages 可执行文件路径')
    ap.add_argument('--qpdf', metavar='PATH', help='qpdf 可执行文件路径')
    args = ap.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
