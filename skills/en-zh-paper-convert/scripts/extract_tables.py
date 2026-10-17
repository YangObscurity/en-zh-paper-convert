#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract_tables.py -- 把原件中每张表按「框线簇 + 表题锚定」定位，并以**矢量**方式
导出为单页 PDF（可直接被 graphicx \\includegraphics）。

    !! 绝不手写 LaTeX 表。!!  用户指令：「有些表怎么是被你手动写出来的，非常丑！
    你就不能找个办法复刻一下」。原件里的表是矢量对象，直接抽取嵌入才是「复刻」；
    手排必然引入丢行/错位/观感差三类问题（生产实测全部实际发生过）。

    !! status == ok 不等于区域正确 !!  历史上有 5 例 status 正常但导出的是
    正文段落 / 另一张表 / 折线图。只数「提取成功张数」永远发现不了。
    验收必须同时看 w/h 是否合理，并把全部表渲染成联系表**目视复核**。

依赖
    Python 3.9+ 与 PyMuPDF：pip install pymupdf
    可选：pdftoppm（poppler）用于 --contact-sheet 目视联系表
         Windows: winget install oschwartz10612.Poppler

用法
    python scripts/extract_tables.py orig.pdf .work/01/tables --expect 1,2,III
    python scripts/extract_tables.py orig.pdf .work/01/tables --expect 2.1-1 --contact-sheet
    python scripts/extract_tables.py orig.pdf out --expect 1 --trace-page 7   # 逐行解释表题判定

表号归一化
    原件 `Table: 3.2.-1` -> `表 3.2-1`；`TABLE III` -> `3`（罗马数字转阿拉伯）；
    附录式 `Table: C.3.1.1-1` 保留字母开头。
    **--expect 与输出同口径归一化**，否则比对全不中。

结构化输出
    TAB 1      p3   ok        w=512.0 h=213.4 file=tab_1.pdf
    OK-LIST: ['1', '2']
    MISSING: []
    NO-RULES: []
    TINY: []
    CLIPPED-TOP: []
    SUMMARY EXPECTED=2 OK=2 MISSING=0 NORULES=0 TINY=0 CLIPTOP=0
    TABLES JSON={...}

验收口径（脚本自动判定 + 人工目视）
    ✓ MISSING == []            （期望表号全部命中）
    ✓ 无 w < 60 或 h < 25 的 TINY 区域
    ✓ CLIPPED-TOP == []        （区域纵向不足、首行被裁；渲染量首次着墨 y）
    ✓ --contact-sheet 渲染后逐张目视，确认内容真是那张表

    前两条**查不出**「区域框对了那张表、但上边界落在表头行中间」：
    区域非空、尺寸也远大于 TINY。CLIPPED-TOP 专治这一类（见 references/tables.md）。
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import zlib

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

EXIT_OK, EXIT_USAGE, EXIT_DEP = 0, 2, 3
INSTALL_MISSING = '缺少 PyMuPDF。安装：\n  pip install pymupdf'
TINY_W, TINY_H = 60.0, 25.0

# --------------------------------------------------- CLIPPED-TOP（第 4 条口径）
# MISSING / TINY / 目视三条口径都查不出「区域纵向不足、表头行被裁一半」：
# 区域确实"框住了那张表"，尺寸也远大于 TINY 阈值。唯一可靠的判据是
# **渲染 crop 顶部条带、量首次着墨 y**：内容贴着裁切边上沿 = 第一行被切。
CLIP_PT = 1.0           # pt：首次着墨 < 此值即判贴边
CLIP_DPI = 288          # 4x 采样 -> 0.25pt/px，足够分辨 1pt
CLIP_RULE_MAX_H = 1.6   # pt：细于/等于此高度的顶部条带视为「框线/边框」
CLIP_RULE_COV = 0.90    # 顶部条带横向覆盖率 > 此值且很细 = 满宽框线
CLIP_INK_GREY = 200     # 灰度 < 此值算着墨
CLIP_MIN_COV = 0.02     # 顶部条带覆盖率 < 此值 = 杂点/抗锯齿鬼影，不算内容

ROMAN = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8,
         'ix': 9, 'x': 10, 'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 14, 'xv': 15,
         'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20, 'xxi': 21,
         'xxii': 22, 'xxiii': 23, 'xxiv': 24, 'xxv': 25, 'xxvi': 26, 'xxvii': 27,
         'xxviii': 28, 'xxix': 29, 'xxx': 30}


def roman_to_int(x):
    return ROMAN.get((x or '').strip().lower())


def _require_fitz():
    try:
        import fitz
        return fitz
    except ImportError:
        print('ERROR: ' + INSTALL_MISSING, file=sys.stderr)
        sys.exit(EXIT_DEP)


# ============================================================================
# 表题正则（**关键**）
# ----------------------------------------------------------------------------
# 最初写死 `Table:\\s*`（**要求冒号**）-> 全部期刊论文（`Table 1`/`TABLE III`）
# 提取不到表，全库仅 22/111 张（20%）。
# 放宽为 (?:TABLE|Table|Tab\\.?) + 允许无冒号 + 罗马数字 + 连续分隔符 `.-` 之后，
# 全库提取率 22 -> 87/111；再修「status==ok 但区域取错」后达 111/111（100%）。
# **凡「某功能全库无效」，先怀疑最窄的那个正则/判据。**
# 支持体例：Table 1 / TABLE III / Table: 7.3.-4 / Tab. 2 / Table: C.3.1.1-1
# ============================================================================
CAP = re.compile(r'(?i)\b(?:TABLE|Table|Tab\.?)\s*[:.\u2013-]?\s*'
                 r'([0-9A-Z][0-9A-Za-z]*(?:[.\-]+[0-9A-Za-z]+)*)')

# 「Table of Contents」这类题头不是表：编号位捕获到 of / contents 之类的常见词就否决。
# 罗马数字号与这些词不冲突（TABLE I / Table V 正常通过）。
_NOT_A_TABLE = re.compile(
    r'^(?:of|contents?|list|index|continued|cont\d*|appendix|page|figure|fig|table'
    r'|caption|summary|note|notes|legend)$', re.I)


def _is_real_tabno(no):
    return bool(no) and not _NOT_A_TABLE.match(no)


def norm(n):
    """原件编号 -> md 编号：7.3.-4 -> 7.3-4；TABLE III -> 3；保留字母开头（C.3.1.1-1）"""
    n = (n or '').strip().rstrip('.')
    r = roman_to_int(n)
    if r is not None:
        return str(r)
    return re.sub(r'\.(?=-)', '', n)


def norm_expect(items):
    """--expect 的取值与 norm() **同口径**归一化（否则比对全不中）。"""
    out = set()
    for x in items:
        x = x.strip()
        if not x:
            continue
        out.add(norm(x))
    return out


# 正文引用里紧跟编号的动词（'Table 3 reports the validation ...'）
_BODY_VERB = re.compile(
    r'^(shows?|lists?|outlines?|presents?|gives?|provides?|reports?|describ|summariz|compares?|'
    r'indicates?|contains?|depicts?|illustrates?|highlights?|details?|specifies?|defines?|'
    r'confirms?|reveals?|suggests?|demonstrates?)', re.I)


# ---------------------------------------------------------------- 框线
def rules_of(page):
    hl, vl = [], []
    for d in page.get_drawings():
        r = d['rect']
        if r.height < 1.5 and r.width > 30:
            hl.append((round(r.y0, 1), round(r.x0, 1), round(r.x1, 1)))
        elif r.width < 1.5 and r.height > 6:
            vl.append((round(r.x0, 1), round(r.y0, 1), round(r.y1, 1)))
    return hl, vl


# ---------------------------------------------------------------------------
# inline-image rules (defect 1)
# ---------------------------------------------------------------------------
# Some producers draw every table rule as a **1x1 bitmap** that the CTM
# stretches into a bar:
#     q 5089.2 0 0 -4.8 515.88 7081.08 cm
#     BI /IM true /W 1 /H 1 /BPC 1 ID \x00 EI
# Such rules are neither vector drawings nor reachable through the page
# resource dict, so on those pages
#     page.get_drawings()  -> 0
#     page.get_cdrawings() -> 0
#     page.get_images()    -> 0      (no /XObject, no /Image in resources)
# and the whole cluster_region() path is dead: every table silently degrades to
# the no-rules fallback (5 of 7 regions wrong on the reference sample).
# page.get_image_info() does list them (bbox + bpc=1), but its inline-image
# bboxes are per display-list entry, so a 1-rule-at-a-time regex parse of the
# content stream is both more faithful and cheaper: no rendering, no 300dpi
# raster scan, ~2 regex passes over one already-inflated stream per page
# (~58 KB here).  The bpc==1 subset of get_image_info() stays as a fallback and
# as the cross-check: on the reference sample both paths find the same 92 bars.
_NUM = r'[-+]?[0-9]*\.?[0-9]+'
_RE_CTM = re.compile((r'(' + _NUM + r')\s+(' + _NUM + r')\s+(' + _NUM + r')\s+(' +
                      _NUM + r')\s+(' + _NUM + r')\s+(' + _NUM + r')\s+cm\b'
                      ).encode('latin-1'))
_RE_BI = re.compile(rb'\bBI\b')
_RE_II_HDR = re.compile(rb'/(W|H|BPC|CS|F)\s*([^\s/]+)')
_INLINE_MIN_LEN = 30.0          # pt: shorter bars are underlines, not rules
_INLINE_MAX_THICK = 4.0         # pt: thicker blobs are logos/photos, not rules
_INLINE_MODE = [None]           # None = probe, 'inline' = ok, 'imageinfo' = degraded


def _inflate(buf):
    """Undo the stream filter; return raw bytes when that is not Flate."""
    if not buf:
        return b''
    try:
        if buf[:1] == b'\x78':
            return zlib.decompress(buf)
        return zlib.decompressobj().decompress(buf)
    except Exception:
        try:
            return zlib.decompressobj().decompress(buf)
        except Exception:
            return buf


def _mul(m, n):
    """Compose two PDF matrices [a b c d e f] (m then n, PDF row-vector order)."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + b1 * c2,
            a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2,
            c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2,
            e1 * b2 + f1 * d2 + f2)


# Graphics-state operators we must see.  A plain regex over the raw stream is
# NOT safe here: PDF text strings -- `[(E)-2.1667(n)]TJ` -- contain arbitrary
# bytes, so a "Q" inside a string literal was matched as an operator, popped
# the state stack one time too many and silently dropped the CTM of every
# later rule on the page (page 13: 34 inline images, only the first 20 got
# usable coordinates).  We therefore tokenize, skipping string/hex/comment
# literals, and only then look at operators.
_RE_GSTATE = re.compile(rb'(?<![A-Za-z0-9*])(q|Q|cm|BI)(?![A-Za-z0-9])')
_WHITESPACE = b' \t\r\n\f\x00'
_DELIM = b'()<>[]{}/%'


def _stream_tokens(buf):
    """Yield (offset, token) for one content stream, quotes/comments skipped."""
    n = len(buf)
    i = 0
    while i < n:
        ch = buf[i:i + 1]
        if ch in _WHITESPACE:
            i += 1
            continue
        if ch == b'%':                      # comment to end of line
            j = buf.find(b'\n', i)
            i = n if j == -1 else j + 1
            continue
        if ch == b'(':                      # literal string, nesting-aware
            depth = 1
            i += 1
            while i < n and depth:
                c = buf[i:i + 1]
                if c == b'\\':
                    i += 2
                    continue
                if c == b'(':
                    depth += 1
                elif c == b')':
                    depth -= 1
                i += 1
            continue
        if ch == b'<':
            if buf[i:i + 2] == b'<<':
                yield i, b'<<'
                i += 2
                continue
            j = buf.find(b'>', i)           # hex string
            i = n if j == -1 else j + 1
            continue
        if ch == b'>' and buf[i:i + 2] == b'>>':
            yield i, b'>>'
            i += 2
            continue
        if ch in _DELIM:
            yield i, ch
            i += 1
            continue
        j = i
        while j < n and buf[j:j + 1] not in _WHITESPACE and buf[j:j + 1] not in _DELIM:
            j += 1
        yield i, buf[i:j]
        i = j


def _ctm_at_inline_images(buf):
    """Yield (pos_of_BI, effective_CTM) for every inline image in one stream.

    The CTM is NOT just the last `cm`: these pages are rendered under an outer
    `q 0.1 0 0 0.1 0 0 cm` page scale, so each rule's own `cm` must be composed
    with everything still on the graphics-state stack (taking only the last
    `cm` yields coordinates 10x too large and off-page).  One linear token pass
    over the already-inflated buffer; no rendering involved.
    """
    stack = []
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    operands = []
    for off, tok in _stream_tokens(buf):
        if tok in (b'q', b'Q', b'cm', b'BI', b'BI\n'):
            op = tok.strip()
            if op == b'q':
                stack.append(ctm)
            elif op == b'Q':
                if stack:
                    ctm = stack.pop()
            elif op == b'cm':
                if len(operands) >= 6:
                    try:
                        ctm = _mul(tuple(float(x) for x in operands[-6:]), ctm)
                    except ValueError:
                        pass
            else:                                   # BI
                yield off, ctm
            operands = []
            continue
        if op_first_byte(tok):
            operands.append(tok)
            if len(operands) > 12:
                del operands[:-12]


def op_first_byte(tok):
    """True when tok looks like a PDF number (an operator operand)."""
    c = tok[:1]
    return c in (b'-', b'+', b'.') or c.isdigit()


def _parse_inline_rules(page):
    """Horizontal bars drawn as stretched tiny inline images (device coords)."""
    doc = page.parent
    page_h = page.rect.height
    out = []
    for xref in page.get_contents():
        try:
            buf = doc.xref_stream(xref)
        except Exception:
            buf = None
        if not buf:
            continue
        buf = _inflate(buf)
        if b'BI' not in buf:
            continue
        # (BI offset -> effective CTM), so the dict walk below can look it up
        ctms = dict(_ctm_at_inline_images(buf))
        for m in _RE_BI.finditer(buf):
            p = m.end()
            w = h = bpc = None
            # Walk the inline-image dict key by key.  Unknown keys (/IM true,
            # /D ...) are legal, so each iteration consumes one "/Key value"
            # pair -- and must skip the *value* too, otherwise scanning stops
            # on the operand token and the whole entry is lost.
            while True:
                while p < len(buf) and buf[p:p + 1] in b' \r\n\t':
                    p += 1
                if buf[p:p + 1] != b'/':
                    break
                hm = _RE_II_HDR.match(buf, p)
                if hm:
                    k, v = hm.group(1), hm.group(2)
                    if k == b'W':
                        w = v
                    elif k == b'H':
                        h = v
                    elif k == b'BPC':
                        bpc = v
                    p = hm.end()
                    continue
                q = p + 1                      # unknown key: skip key ...
                while q < len(buf) and buf[q:q + 1] not in b' \r\n\t/':
                    q += 1
                if q == p + 1:
                    break
                p = q
                while p < len(buf) and buf[p:p + 1] in b' \r\n\t':
                    p += 1
                if buf[p:p + 1] in (b'[', b'<'):
                    depth = 0                  # ... then its array/dict operand
                    while p < len(buf):
                        ch = buf[p:p + 1]
                        if ch == b'[' or buf[p:p + 2] == b'<<':
                            depth += 1
                            p += 1 if ch == b'[' else 2
                            continue
                        if ch == b']' or buf[p:p + 2] == b'>>':
                            depth -= 1
                            p += 1 if ch == b']' else 2
                            if depth <= 0:
                                break
                            continue
                        p += 1
                else:
                    while p < len(buf) and buf[p:p + 1] not in b' \r\n\t/':
                        p += 1
            if buf[p:p + 3] != b'ID ':
                continue
            e = buf.find(b'EI', p + 3)
            while e != -1 and buf[e + 2:e + 3] not in (b' ', b'\n', b'\r', b'\t',
                                                       b'', b'Q'):
                e = buf.find(b'EI', e + 2)
            if e == -1:
                continue
            # 1x1 / 1-bit images only: anything larger is real artwork
            tiny = True
            for v in (w, h):
                if v is None:
                    continue
                try:
                    if int(v) > 4:
                        tiny = False
                except ValueError:
                    tiny = False
            if not tiny or (bpc is not None and bpc not in (b'1',)):
                continue
            ctm = ctms.get(m.start())
            if ctm is None:
                continue
            a, b, c, dd, e2, f = ctm
            # device rect of the unit square under the effective CTM
            xs = [e2, e2 + a]
            ys = [page_h - f, page_h - (f + dd)]
            if b or c:
                xs += [e2 + b, e2 + a + b]
                ys += [page_h - (f + c), page_h - (f + dd + c)]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            out.append((round(y0, 1), round(x0, 1), round(x1, 1),
                        round(y1 - y0, 1)))
    return out


def _inline_rules_imageinfo(page):
    """Fallback path: get_image_info() bboxes of bpc==1 inline images."""
    out = []
    try:
        infos = page.get_image_info()
    except Exception:
        return out
    for im in infos:
        if im.get('bpc') != 1 or im.get('cs-name'):
            continue
        bb = im.get('bbox')
        if not bb:
            continue
        x0, y0, x1, y1 = (float(v) for v in bb[:4])
        if y0 < 0 or y1 > page.rect.height + 1:
            # raw user-space y (origin bottom-left): flip into device space
            y0, y1 = page.rect.height - y1, page.rect.height - y0
        out.append((round((y0 + y1) / 2.0, 1), round(x0, 1), round(x1, 1),
                    round(abs(y1 - y0), 1)))
    return out


def inline_rules(page):
    """Logical horizontal rules hidden inside 1x1 stretched inline images."""
    bars = None
    if _INLINE_MODE[0] != 'imageinfo':
        try:
            bars = _parse_inline_rules(page)
            _INLINE_MODE[0] = 'inline'
        except Exception as exc:
            print('WARN: inline-image rule parse failed (%s); '
                  'fallback = get_image_info().' % exc, file=sys.stderr)
            _INLINE_MODE[0] = 'imageinfo'
    if bars is None:
        bars = _inline_rules_imageinfo(page)
    out = []
    for y0, x0, x1, hgt in bars:
        if x1 - x0 < _INLINE_MIN_LEN or hgt > _INLINE_MAX_THICK:
            continue
        out.append((round(y0 + hgt / 2.0, 1), round(x0, 1), round(x1, 1)))
    return out


def merge_with_inline(hl_m, inl):
    """Fold inline-image bars into the merged logical rule list (y, x0, x1).

    A row line may be drawn as one bar per cell (y=660.6 appears as
    x=373.3..433.2 and x=482.6..537.5), so same-y bars merge; the 6pt gap keeps
    two side-by-side column tables from fusing into a single rule.
    """
    if not inl:
        return list(hl_m)
    items = [list(x) for x in hl_m] + [[y, x0, x1] for y, x0, x1 in inl]
    out = []
    for y, x0, x1 in sorted(items):
        placed = False
        for m in out:
            if abs(m[0] - y) > 2.0:
                continue
            if x1 < m[1] - 6.0 or x0 > m[2] + 6.0:
                continue
            m[0] = (m[0] + y) / 2.0
            m[1] = min(m[1], x0)
            m[2] = max(m[2], x1)
            placed = True
            break
        if not placed:
            out.append([y, x0, x1])
    return [(round(a, 1), round(b, 1), round(c, 1)) for a, b, c in out]


def _merge_rules(hl, ytol=2.0, gap=6.0):
    """把同一条表格行线上的**分段**框线合并成一条逻辑框线。

    很多 PDF（尤其规范文档与 LaTeX 排版的期刊）把一行表格线画成**每单元格一段**，
    例如某页 y=534.6 被拆成 x 49.8-98.5 / 100.7-134.3 / ... / 261.5-297.1。
    不合并则 _same_width 只能匹配到第一格，导出的表被裁成单列（实测 55x65 空白）。

    `gap` 保持较小（6pt）是**有意的**：行内分段通常只隔 1-2pt；
    放宽到几十 pt 会把**相邻栏的表**并成一行，反而让区域判定崩掉
    （实测放宽到 40pt：#20 T13/T14 高度从 213 掉到 22、#04 T3 变回 30）。
    """
    out = []
    for y, x0, x1 in sorted(hl):
        placed = False
        for m in out:
            if abs(m[0] - y) > ytol:
                continue
            if x1 < m[1] - gap or x0 > m[2] + gap:
                continue
            m[0] = min(m[0], y); m[1] = min(m[1], x0); m[2] = max(m[2], x1)
            placed = True
            break
        if not placed:
            out.append([y, x0, x1])
    return [(round(a, 1), round(b, 1), round(c, 1)) for a, b, c in out]


def _same_width(a, b, tol=6.0):
    """两条框线是否属于同一张表（x 起止相近）。"""
    return abs(a[1] - b[1]) <= tol and abs(a[2] - b[2]) <= tol


def _same_col(a, b, page_w):
    """两矩形是否可能同栏。

    判据一（重叠）：横向有实质重叠 -> 一定同栏。
      期刊里表题在左、表体**加宽**越过中线时，只按中线归类会把两者判成不同栏
      -> cluster_region 返回 None -> 落到 fallback_region，把下方正文整段框进表里。
    判据二（中线）：跨中线的**居中表题**（如居中 TABLE III 骑在中线上）
      与左右两栏都兼容；否则整张 no-rules。
    """
    ov = min(a.x1, b.x1) - max(a.x0, b.x0)
    if ov > 0 and ov > 0.3 * min(a.width, b.width):
        return True
    mid = page_w / 2.0
    tol = page_w * 0.06          # 中线 +-6% 视为「骑中线」

    def side(r):
        cx = (r.x0 + r.x1) / 2.0
        if abs(cx - mid) <= tol:
            return 0             # 居中/跨栏 -> 与左右都兼容
        return -1 if cx < mid else 1

    sa, sb = side(a), side(b)
    return sa == 0 or sb == 0 or sa == sb


def _has_adjacent_rules(hl, cap_rect, max_gap=60.0, need=2, page_w=None, cap_rects=None):
    """表题紧邻处是否存在 >=need 条同宽度框线（真表体的标志）。

    page_w     给定时只认**与表题同栏**的框线，否则左栏正文里的交叉引用
               会被右栏表格的框线「救活」。
    cap_rects  给定时框线必须**离本表题比离其他表题更近**，
               否则正文引用会被下方另一张表的框线救活。
    """
    for side in ('down', 'up'):
        near = [h for h in hl
                if (h[0] >= cap_rect.y0 if side == 'down' else h[0] <= cap_rect.y1)]
        if page_w is not None:
            near = [h for h in near
                    if _same_col(fitz.Rect(h[1], h[0], h[2], h[0] + 0.1), cap_rect, page_w)]
        if not near:
            continue
        near.sort(key=lambda h: h[0], reverse=(side == 'up'))
        first = near[0]
        gap = (first[0] - cap_rect.y1) if side == 'down' else (cap_rect.y0 - first[0])
        if gap > max_gap:
            continue
        w = [h for h in near if abs(h[1] - first[1]) <= 6 and abs(h[2] - first[2]) <= 6]
        if len(w) < need:
            continue
        if cap_rects:
            mine = abs(first[0] - (cap_rect.y1 if side == 'down' else cap_rect.y0))
            others = [min(abs(first[0] - c.y1), abs(first[0] - c.y0))
                      for c in cap_rects if c is not cap_rect]
            if others and min(others) < mine:
                continue
        return True
    return False


def caption_like(txt, m, hl=None, cap_rect=None, page_w=None, cap_rects=None, prev_gap=None):
    """判断一行 CAP 匹配是**真表题**还是正文交叉引用。

    !! **不要**恢复旧的 `^(summar|shows|...)` 一票否决守卫 !!
    真表题也叫 "Summary of the fitted statistical distributions ..."，
    那个守卫让两张真表整张丢失（MISSING，非 no-rules）。

    **最强判据是版面上下文**（prev_gap = 与同栏上一文本行的行距）：
      · 正文交叉引用嵌在段落里，上一行与本行行距 ~= 行内行距（<5pt）
      · 真表题前必有段间距（>=8pt），因为表题是独立浮动体
    ⚠ 不能只用行距：规范文档的表题也紧贴上文（gap≈0），单独用会**整篇误杀**。
    """
    after = txt[m.end():].strip()
    hard_prose = False
    prose = False
    # (0) 嵌在段落里（行距 < 5pt = 行内行距）**且**整行较长 -> 交叉引用（硬判据）
    if prev_gap is not None and prev_gap < 5.0 and len(txt) > 55:
        hard_prose = prose = True
    # (0b) 极短且带句点的编号式（'Table VI.' / 'Table 14.'）紧贴上一行 -> 行内交叉引用。
    #      真表题（'TABLE I'，无句点）不受影响。
    elif prev_gap is not None and prev_gap < 5.0 and len(txt) <= 16 \
            and re.match(r'^[\s]*[.。]\s*$', txt[m.end():]):
        hard_prose = prose = True
    # (1) 编号后紧跟**小写词** = 正文句（真表题要么无文字、要么紧跟大写标题词）。
    #     硬判据：旁边就是表体也不许翻案，否则它会顶替真表题、把区域拉进正文段落。
    elif re.match(r'^[a-z]', after) and len(txt) > 40:
        hard_prose = prose = True
    elif _BODY_VERB.match(after) and len(txt) > 40:
        hard_prose = prose = True
    elif after.startswith('.') and re.match(r'^\.[\s]*[A-Z]', after) and len(txt) > 60:
        prose = True
    # (2) 「标题短语 + )。再接一个完整句子」-> 交叉引用
    elif len(txt) > 60 and re.search(r'[)\]]\s*\.\s+[A-Z][a-z]', txt):
        prose = True
    # (3) 整行 >90 且以句末标点收尾 -> 通常是正文段落行
    elif len(txt) > 90 and re.search(r'[.]\s*$', txt):
        prose = True
    if not prose:
        return True
    if hard_prose:
        return False
    # 命中 (1)(2)(3) **不是**一票否决：真表题紧邻自身表体时才推翻。
    # 必须同时满足：框线与表题同栏，且这些框线离本表题比离任何**其他**表题更近。
    if hl and cap_rect is not None and \
            _has_adjacent_rules(hl, cap_rect, page_w=page_w, cap_rects=cap_rects):
        return True
    return False


# ---------------------------------------------------------------- 区域
def _expand_bottom_for_notes(page_lines, bot, x0, x1, cap_rects=None, max_rows=4):
    """把区域下边界从末条框线**下移到表注**（"Please refer to the datasheets"、
    "1 Based on 3V and WDT disabled" 等）。

    表注在框线之外、且往往**居中于表宽**，因此多数在 [x0, x1] 之内；也有
    略窄/略宽于表体的（T1 的 "Please refer to the datasheets" 只覆盖
    217.2..394.8，落在 48.6..563.5 内）。判据：
      · 行距小（<= 1.6 倍表内行距，这里取绝对 16pt）
      · 水平上落在表宽内（有重叠即可，允许略窄）
      · 不是下一个表题（由 cap_rects 挡住）
    """
    if not page_lines:
        return bot
    out = bot
    taken = 0
    rows = [r for r, _t in page_lines if r.y0 >= bot - 1]
    rows.sort(key=lambda r: r.y0)
    for r in rows:
        if taken >= max_rows:
            break
        if r.y0 - out > 16:                     # 行距过大 -> 正文段落，停止
            break
        if cap_rects and any(abs(r.y0 - c.y0) < 6 and abs(r.x0 - c.x0) < 40
                             for c in cap_rects):
            break                               # 下一张表的表题
        # 与表宽完全不相交的行 -> **跳过**，不能 break。
        # 双栏版式下右栏的行与左栏表宽不相交，且它们往往
        # 恰好排在左栏表注之间（实测 #Lee p13：表注 "1 Based on 3V..."
        # 之前就是右栏的 "1"/"5"/"Tss" 行），一旦 break
        # 就一条表注都扩不到。
        if min(r.x1, x1) - max(r.x0, x0) <= 0:
            continue
        # 明显的正文长行（横跨整栏且很长）不吞
        if r.width > 0.95 * (x1 - x0) and len(_t_of(r, page_lines)) > 90:
            break
        out = max(out, r.y1)
        taken += 1
    return out


def _t_of(rect, page_lines):
    for r, t in page_lines:
        if r is rect or (abs(r.y0 - rect.y0) < 0.01 and abs(r.x0 - rect.x0) < 0.01):
            return t
    return ''


def _caption_is_beside(cap_rect, x0, x1):
    """True when the caption sits to the LEFT of the table's own x-span.

    Some journals (booktabs house style, e.g. #Agu) set the caption in the left
    margin, *vertically centred* against a table that starts further right:

        Table 1 True and estimated       Quantity   True value   Estimated value
        Gamma-Weibull parameters         Gamma shape a0   2.000    2.031
        along with CUSUM design ...

    The caption block spans several short lines, so its ``y1`` can sit BELOW the
    table's own header row.  Using ``cap_rect.y1`` as the ceiling therefore
    chops the header in half.  Detected as "caption and table x-spans do not
    overlap" -- a caption stacked *above* a full-width table overlaps it, so
    this never fires for the ordinary layout.
    """
    if cap_rect is None:
        return False
    ov = min(cap_rect.x1, x1) - max(cap_rect.x0, x0)
    return ov <= 2.0


def _toprule_above(hl, top, x0, x1):
    """Return the y of the booktabs TOPRULE directly above ``top``, else None.

    booktabs body: ``toprule / header row / midrule / data rows / bottomrule``.
    ``cluster_region`` seeds on the rule nearest the caption, which for that
    body is the **midrule** -- so the whole header row falls outside the region
    while the region still "contains a table" (status stays ``ok``, MISSING is
    empty, the size clears TINY: every existing acceptance check passes).

    Only the rule **immediately** above ``top`` is considered.  Searching
    further up is wrong: on the reference sample the page-header rule
    (y=45.9, full measure) would be picked instead of the real toprule at
    y=598.4, and the scan then walks into the running head.

    That nearest rule still has to *look* like a toprule, i.e. span >= 70% of
    the table x-span; a shorter one is run-in filler or a decorative segment,
    and the caller then keeps its ordinary "no rule above the header" path.
    """
    if not hl:
        return None
    norm = x1 - x0
    if norm <= 0:
        return None
    above = [h[0] for h in hl if h[0] < top - 1]
    if not above:
        return None
    y = max(above)
    sp = max((h[2] - h[1]) for h in hl if abs(h[0] - y) < 0.6)
    return y if sp >= 0.70 * norm else None


def _expand_top_for_header(page_lines, top, cap_rect, x0, x1, max_rows=6, hl=None):
    """把区域上边界从首条框线**上移到表头行**。

    Many tables draw no rule above the header row (``Models | Performance |
    Power Consumption`` sits above the first rule), so anchoring the region at
    the first rule silently amputates the header row -- the table then reads as
    if it lost its column titles.  Grow upward while the lines above stay
    "table-like": they overlap the table's horizontal span and are not
    full-measure paragraph text.  The caption (when present) is a hard ceiling.
    """
    if not page_lines:
        return top
    limit = cap_rect.y1 + 2 if cap_rect is not None else None
    # 上方最近的一条框线是**上界**：页眉/页框线
    # （p4 y=45.9 x=51..544.3）不是表内容，不能跳过它向上扩，
    # 否则导出的表顶部会带一条黑细线（实测 T1/T2/T3/T6/T7）。
    if hl:
        above = [h[0] for h in hl if h[0] < top - 1]
        if above:
            rule_ceil = max(above)
            limit = rule_ceil if limit is None else max(limit, rule_ceil)
    # booktabs: 表头行夹在 toprule 与 midrule 之间。表题在左侧的体例下
    # cap_rect.y1 不是硬顶（它会落到表头行下方）-> 改用 toprule，并把扫描
    # 起点从「首条框线」改到「toprule」，否则表头行仍被判在区域之上。
    toprule = _toprule_above(hl, top, x0, x1) if hl else None
    by_side = _caption_is_beside(cap_rect, x0, x1) if cap_rect is not None else False
    if toprule is not None and by_side:
        # 表题在左侧：它可能有多行，y1 会落到表头行下方，不能当硬顶。
        # 硬顶改由「扫描过程中撞到表题自身」来兜（见下方 by_side 判据）。
        limit = limit if limit is None else max(limit, toprule - 2)
    elif toprule is not None:
        limit = toprule - 2 if limit is None else max(limit, toprule - 2)
    # 候选 = 首条框线**之上**的行（原判据），外加一种 booktabs 特例：
    # 表头行**横跨** toprule 时（rule 落在字形中段），r.y1 会略大于 top，
    # 只按 `r.y1 <= top+1` 会把它整行漏掉——那正是本缺陷的表头。
    # 仅在确实找到 toprule 时才放宽，避免把表体第一行也吸进来。
    rows = [r for r, _t in page_lines
            if r.y1 <= top + 1 or
            (toprule is not None and r.y0 <= toprule and r.y1 >= toprule - 1)]
    # 自下而上扫：候选是「表头行 + 其上的表头续行」，越靠上越先被拒。
    rows.sort(key=lambda r: (r.y1, r.x0), reverse=True)
    want = (x1 - x0)
    out = top
    picked = []
    for r in rows:
        if len(picked) >= max_rows:
            break
        # 已经越过硬顶（toprule / 上一条框线 / 上方的表题）-> 停。
        # 用 `r.y0`（不是 `r.y1`）判：判定标准是「这一行的**起点**是否已越过
        # 硬顶」。用 y1 会把**跨过**硬顶的那一行也放进来——实测 #Zhan T1
        # 表题第二行 "End-to-end time includes LLM inference..." (y1=102.4)
        # 就是这样被吸进区域，导出后表题被横切一半（真正的表头反而没问题）。
        if limit is not None and r.y0 < limit:
            break
        # 行必须落在表宽之内：表头单元格
        # （"Amount of energy" 只占 x=194.9..265.8）远窄于表宽，
        # 用“占比”判会被误杀，改用“**完全包含**”。
        if not (r.x0 >= x0 - 4 and r.x1 <= x1 + 4):
            if min(r.x1, x1) - max(r.x0, x0) <= 0:
                continue
            if r.width > want + 6:
                break
        if out - r.y0 > 90:                     # runaway guard
            break
        # 表题在左侧、且候选行与表题 **x 与 y 都重叠**：那是表题自己的
        # 第 2、3 行（表题贴在表格左缘、行高与之交错），不是表头。
        # 实测 #Agu T1/T15/T16 的表题续行都会在这儿被误吸。
        if by_side and cap_rect is not None and \
                r.y0 < cap_rect.y1 - 1 and r.y1 > cap_rect.y0 + 1 and \
                r.x0 < min(cap_rect.x1, x0) + 2:
            break
        # 表题行本身不能当成表头扩进来（表头多行的体例里它紧贴首条框线之上）。
        if cap_rect is not None and \
                r.x0 >= cap_rect.x0 - 2 and r.x1 <= cap_rect.x1 + 2 and \
                r.y1 <= cap_rect.y1 + 2 and r.y0 >= cap_rect.y0 - 2:
            break
        out = min(out, r.y0)
        picked.append(r)
    return out


def cluster_region(hl, vl, anchor_y, page_rect, band=(None, None), margin=48.0,
                   cap_rect=None, cap_rects=None, max_gap=60.0, step_gap=400.0,
                   used=None, page_lines=None):
    """在「同栏相邻表题」界定的 band 内，取与表题**相邻**的一簇同宽度框线。

    旧实现取「框线更多的一侧」，会把远处/跨栏另一张表的框线当成表体：
      · 两张表导出内容完全相同（band 上界取到同页上一张表）
      · 某表只剩 16pt（band 下界取到**右栏**表题 -> 左栏表体被截断）
      · 某表高 563pt 吞到页尾（同栏无中间表题，band 无上界）
    新实现四条判据：
      (1) band 只由**同栏**的相邻表题界定（双栏版式里右栏表题不得截断左栏表体）
      (2) 取**离表题最近**的一侧，而不是更长的一侧
      (3) 只收集与首条框线**同宽度**且与当前簇相邻（间隔 <= step_gap）的框线
      (4) used 里已被上一张表认领的框线不再复用（同页多张紧邻表互不抢框线）
    """
    if cap_rect is not None and cap_rects:
        samec = [c for c in cap_rects
                 if c is not cap_rect and _same_col(c, cap_rect, page_rect.width)]
        lo_cap = max([c.y1 for c in samec if c.y1 < cap_rect.y0], default=None)
        hi_cap = min([c.y0 for c in samec if c.y0 > cap_rect.y1], default=None)
    else:                                       # 兼容旧调用（仅 y 带）
        lo_cap, hi_cap = band
    best = None
    for side in ('down', 'up'):
        if cap_rect is not None:
            base = [h for h in hl
                    if (h[0] >= cap_rect.y0 if side == 'down' else h[0] <= cap_rect.y1)]
            if used:                            # 已被上一张表认领的框线不再复用
                base = [h for h in base if h not in used]
            # 只认与表题**同栏**的框线：双栏里表题在右栏时，左栏里 y 更近的图/表框线
            # 会被误当成表体（曾抓到左栏 Fig. 5 的折线图，导出成一张「图不是表」）。
            # ⚠ 但有些篇目是「左边距窄表题 + 右侧/全宽表体」的约定，
            #   按栏过滤会一条都不剩 -> 此时退回不过滤栏。
            seed = [h for h in base
                    if _same_col(fitz.Rect(h[1], h[0], h[2], h[0] + 0.1),
                                 cap_rect, page_rect.width)]
            # 表题在**左侧边距**、与表体 x 不相交时，同栏判据没有意义：
            # 它只会偶然命中一条 x 区间**恰好压到表题右缘**的远处框线
            # （实测 #Agu p23 的 T15 就命中 y=345.7 x=141.7..453.7，
            #  于是真正的 toprule/midrule 60.4/76.6 一条都进不了 seed，
            #  且因为 seed 非空，下面的 margin_like 兜底也不会触发）。
            if base:
                # 「同一条框线是否 x 上压住表题」用**严格重叠**判，不用 _same_col：
                # _same_col 还带「骑中线两侧都兼容」这条豁免，会把**通栏页眉线**
                # （x=51..544.3，必然压住左边距表题）也算成同栏，于是下面的
                # 分支永远不触发。这里只问几何重叠。
                #
                # 也要允许**部分**框线重叠（#Agu p23 有一条 x=141.7..453.7 的
                # 远处框线压在表题右缘上）：把重叠的那些丢掉再取最宽的一族，
                # 而不是「全部不重叠才启用」。
                def _x_ov(h):
                    # 「框线压在表题上」= 表题**落在框线横向范围之内**（有实质重叠）。
                    # 只问重叠，不用 _same_col：后者带「骑中线两侧都兼容」豁免，
                    # 会把通栏页眉线（x=0..page_w）也算成同栏。
                    return min(h[2], cap_rect.x1) - max(h[1], cap_rect.x0) > 2.0
                # 判据必须落在**框线自己**身上，不能拿「全页框线的整体 x 范围」去比：
                # 本刊每页都有一条通栏页眉线 x=51..544.3，它一定压住左边距的表题。
                #
                # ⚠ 只在表题**又窄又靠左**时启用（真·左边距表题）。
                # 表题本身就在栏内时（#Agu p22 的 T14 表题 x=306.2..544.3
                # 恰等于表体宽度），「框线压住表题」恒为真，会误触发并把
                # 邻栏框线拉进来 -> 该表直接 no-rules。
                #
                # ⚠⚠ 还必须要求「同栏判据选错了族」：即它选出的最近框线
                # **比 base 里最近的框线更远**。#Mohammed p6 的 T4 表题
                # x=45.4..78.9 就在表体 x=45.4..549.9 **之内**（全宽表 + 左对齐
                # 表题），重叠判据为真但同栏判据其实是对的；不加这一条会把它
                # 唯一正确的框线族丢掉，退回 fallback 并截窄横向范围。
                if base and seed and \
                        cap_rect.x1 <= page_rect.x0 + 0.30 * page_rect.width:
                    near_seed = min(seed, key=lambda h: abs(h[0] - cap_rect.y0))
                    near_base = min(base, key=lambda h: abs(h[0] - cap_rect.y0))
                    off = abs(near_seed[0] - cap_rect.y0) - \
                        abs(near_base[0] - cap_rect.y0)
                    if off > 1.0:
                        free = [h for h in base if not _x_ov(h)]
                        pool = free or base
                        wmax = max(h[2] - h[1] for h in pool)
                        widest = [h for h in pool if (h[2] - h[1]) >= 0.6 * wmax]
                        if widest:
                            seed = widest
            if not seed:
                # 兜底「不过滤栏」在两种情形下允许：
                #  (a) 本页没有其他候选框线；
                #  (b) 表题落在**很窄的左边距**里，而框线在右侧——
                #      这是「左边距窄表题 + 宽表体」的排版约定
                #      （#Agu 每页如此：表题 x=51..144，表体 x=178.6..544.3）。
                # 必须排除双栏页里「左栏表拿右栏框线」的情形
                # （实测 #Lee p13 的 T5 被定到 x=309..567，把右栏 T6 整张框了进来）：
                # 那种情形下**表题自己就在某一栏内**，而不是在边距里。
                margin_like = cap_rect.x1 <= page_rect.x0 + 0.30 * page_rect.width
                others = [h for h in hl if h not in base]
                if not others or margin_like:
                    seed = base
            if side == 'down' and hi_cap is not None:
                seed = [h for h in seed if h[0] < hi_cap]
            if side == 'up' and lo_cap is not None:
                seed = [h for h in seed if h[0] > lo_cap]
            # 同页紧邻两表：不靠「中点分界」猜边界（表高不等时会切错，实测把某表
            # 底框线切掉只剩 19pt），而是由**上层循环按顺序认领**（used）解决。
        else:
            top_lim = max(margin, lo_cap if lo_cap is not None else margin)
            bot_lim = min(page_rect.y1 - margin,
                          hi_cap if hi_cap is not None else page_rect.y1 - margin)
            seed = [h for h in hl if top_lim - 1 <= h[0] <= bot_lim + 1
                    and (h[0] <= anchor_y + 2 if side == 'up' else h[0] >= anchor_y - 2)]
        if not seed:
            continue
        seed.sort(key=lambda h: h[0], reverse=(side == 'up'))
        first = seed[0]
        ref = cap_rect if cap_rect is not None else fitz.Rect(0, anchor_y, 0, anchor_y)
        gap = max(0.0, (first[0] - ref.y1) if side == 'down' else (ref.y0 - first[0]))
        if gap > max_gap:
            continue
        grp = [first]
        for h in seed:
            if h is first:
                continue
            if not _same_width(h, first):
                continue
            if min(abs(h[0] - g[0]) for g in grp) > step_gap:
                continue
            grp.append(h)
        top = min(h[0] for h in grp); bot = max(h[0] for h in grp)
        x0 = min(h[1] for h in grp); x1 = max(h[2] for h in grp)
        # 同宽度簇可能只有一个「部分分段」（同行另一段中间空档超出 6pt 合并容差）
        # -> 该侧还有与首段 **y 相邻**的完整行框线时，用它们的并集定横向范围，
        #    避免整侧被判过窄而丢弃（曾把一张表导出成折线图）。
        if bot - top < 8 or x1 - x0 < 40:
            extra = [h for h in seed if abs(h[0] - first[0]) <= 60]
            if extra:
                top = min(h[0] for h in extra); bot = max(h[0] for h in extra)
                x0 = min(h[1] for h in extra); x1 = max(h[2] for h in extra)
        if bot - top < 8 or x1 - x0 < 40:
            continue
        if best is None or gap < best[0]:
            best = (gap, top, bot, x0, x1)
    if best is None:
        return None
    _, top, bot, x0, x1 = best
    # 竖向框线只用来微调横向范围，且必须落在**已选中的横向范围内附近**。
    # 旧写法取「所有与 [top,bot] 有 y 重叠的竖线」，会把**另一栏**的竖线算进来：
    # 表体在右栏，但左栏图表的竖线与表体 y 区间重叠 -> 横向被拉到左栏，
    # 导出的是一张折线图（图不是表！）。
    xs = [x for x, y0, y1 in vl
          if not (y1 < top - 2 or y0 > bot + 2) and x0 - 8 <= x <= x1 + 8]
    # 只有当竖线给出**合理宽度**时才用它收窄横向范围。
    # 否则（例如只命中表内一条装饰竖线）会把区域压成几 pt 宽的细条，
    # 整张表被判过小后落到 fallback，反而框进几百 pt 高的正文。
    if xs and len(xs) >= 2 and (max(xs) - min(xs)) >= 0.5 * (x1 - x0):
        x0, x1 = min(xs), max(xs)
    # 表体首条框线之上往往还有表头行（无顶框线）：不把上边界提上去，
    # 导出的表就缺一整行表头（本样本 7 张表实测全部缺）。
    top = _expand_top_for_header(page_lines, top, cap_rect, x0, x1, hl=hl)
    # 表注（"Please refer to the datasheets ..."）在末条框线之下，不向下扩就会被截掉。
    bot = _expand_bottom_for_notes(page_lines, bot, x0, x1, cap_rects=cap_rects)
    pad = 3
    return fitz.Rect(max(page_rect.x0, x0 - pad), max(page_rect.y0, top - pad),
                     min(page_rect.x1, x1 + pad), min(page_rect.y1, bot + pad))


def _fallback_core(segs, cap_rect, page, margin=48.0, stop_lo=None, stop_hi=None):
    """fallback_region 的纯几何内核：给定段集合，求无框线表区域。

    stop_lo/stop_hi：同栏相邻表题把范围夹住（下方不超过下一个表题，
    上方不低于上一个表题），避免一张表的区域吞掉紧邻的另一张表。
    """
    segs = sorted(segs, key=lambda r: (r.y0, r.x0))
    rows = []
    for r in segs:                              # 视觉行聚类：y 区间重叠者并入
        placed = False
        for row in rows:
            if r.y0 < row['y1'] - 1 and r.y1 > row['y0'] + 1:
                row['segs'].append(r)
                row['y0'] = min(row['y0'], r.y0); row['y1'] = max(row['y1'], r.y1)
                placed = True
                break
        if not placed:
            rows.append({'y0': r.y0, 'y1': r.y1, 'segs': [r]})
    rows.sort(key=lambda x: x['y0'])

    def is_tablerow(row):
        """表格行 = 含 >=2 个**横向分离**的段（各单元格独立成行）。"""
        ss = sorted(row['segs'], key=lambda r: r.x0)
        n, prev_x1 = 0, None
        for r in ss:
            if prev_x1 is None or r.x0 > prev_x1 + 2:
                n += 1
            prev_x1 = max(prev_x1 or 0, r.x1)
        return n >= 2

    flags = [is_tablerow(r) for r in rows]
    idx = None
    for i, row in enumerate(rows):
        if row['y0'] - 2 <= cap_rect.y0 <= row['y1'] + 2 or abs(row['y0'] - cap_rect.y0) < 3:
            idx = i
            break
    if idx is None:
        return None

    def grow(i0, step):
        picked, i, pad_ok = [], i0, 0
        while 0 <= i < len(rows):
            if stop_hi is not None and rows[i]['y0'] >= stop_hi:
                break
            if stop_lo is not None and rows[i]['y1'] <= stop_lo:
                break
            if picked:      # 行间距过大（>32pt，约 2.5 倍行距）-> 表已结束，不再吞正文
                edge = picked[-1]['y0'] if step < 0 else picked[-1]['y1']
                gap = abs(rows[i]['y1'] - edge) if step < 0 else abs(rows[i]['y0'] - edge)
                if gap > 32:
                    break
            if flags[i]:
                picked.append(rows[i]); pad_ok = 0
            else:
                pad_ok += 1
                # 还没捡到任何表格行时，允许跳过表题下方的**表标题**若干行
                # （无框线表在 TABLE IV 之后可能还有两行居中标题；
                #  只容忍 1 行会在到达表体前提前 break，整张表判无区域）。
                if pad_ok > (4 if not picked else 1):
                    break
            i += step
        return picked

    best = None
    for i0, step in ((idx + 1, 1), (idx - 1, -1)):
        pk = grow(i0, step)
        if len(pk) >= 2:
            top = min(r['y0'] for r in pk); bot = max(r['y1'] for r in pk)
            x0 = min(s.x0 for r in pk for s in r['segs'])
            x1 = max(s.x1 for r in pk for s in r['segs'])
            h = bot - top
            if h >= 15 and (best is None or h > best[0]):
                best = (h, top, bot, x0, x1)
    if best is None:
        return None
    _, top, bot, x0, x1 = best
    top, bot = max(top, margin), min(bot, page.rect.y1 - margin)
    if bot - top < 15:
        return None
    return fitz.Rect(x0 - 6, top - 4, x1 + 6, bot + 4)


def fallback_region(page, cap_rect, margin=48.0, taken=None, cap_rects=None):
    """无框线表的兜底：按「视觉行（y 重叠）+ 多单元格（同 y 多段）」判定表格区域。

    适用整页 drawings=0 的期刊表（实测 0/7 -> 7/7）。
      (1) 把行按 y 重叠聚成「视觉行」，视觉行内 >=2 个横向分离的段 -> 表格行
      (2) 连续（行间距 < 26/32pt）的表格行构成表体
      (3) 表题上方或下方取更长的一侧
      (4) **同栏限定（仅双栏版式）**：期刊常见「左右两栏各一张无框线表」，
          不限定栏会把两张表并成一张、一路吞到页脚（实测整页 684pt）。
          但「左栏窄表题 + 全宽表体」的篇目**不能**按栏过滤，否则被判无区域。
          判据：页面中线附近存在**成对的左右文本块**时才启用同栏限定。
    """
    mid = page.rect.width / 2.0
    segs = []
    # !! 必须用**行级**矩形，不用 span !!
    # 一行正文/表注常被拆成十几段 span，用 span 判「多单元格」会把**每一行正文**
    # 都误判成表格行，于是 region 一路吞到页脚（实测 684pt 整页）。
    # 行级下表格行与正文行的区别才干净：
    #   表格行 = 同一 y 上有多个**横向分离**的行矩形
    #   正文行 = 一条横跨整栏的长行矩形
    all_lines = []
    for b in page.get_text('dict')['blocks']:
        if b.get('type') != 0:
            continue
        for l in b.get('lines', []):
            t = ''.join(sp['text'] for sp in l['spans']).strip()
            if not t:
                continue
            all_lines.append((fitz.Rect(l['bbox']), t))
    _left = [r for r, _ in all_lines if (r.x0 + r.x1) / 2 < mid]
    _right = [r for r, _ in all_lines if (r.x0 + r.x1) / 2 >= mid]
    two_col = False
    if _left and _right:
        pair = sum(1 for r in _left if any(abs(r.y0 - q.y0) < 6 for q in _right))
        two_col = pair >= 8                     # 有足够多的左右同行块 -> 双栏
    for r, _t in all_lines:
        if two_col and not _same_col(r, cap_rect, page.rect.width):
            continue
        # 已被上一张表占用的文本行不再复用。**仅双栏版式**启用：
        # 单栏长文里上下相邻的表本来就共享整栏宽度，一律排除会把正当表判成无区域。
        if two_col and taken and any(
                tr.y0 - 2 <= r.y0 <= tr.y1 + 2 and tr.intersects(r) for tr in taken):
            continue
        segs.append(r)
    if not segs:
        return None
    stop_lo = stop_hi = None
    if cap_rects:
        _same = [c for c in cap_rects
                 if c is not cap_rect and _same_col(c, cap_rect, page.rect.width)]
        stop_lo = max([c.y1 for c in _same if c.y1 < cap_rect.y0], default=None)
        stop_hi = min([c.y0 for c in _same if c.y0 > cap_rect.y1], default=None)
    reg = _fallback_core(segs, cap_rect, page, margin, stop_lo=stop_lo, stop_hi=stop_hi)
    if reg is None and two_col:
        # 同栏限定没找到有效表体 -> 该篇其实是「窄表题 + 全宽表体」，
        # 退回不过滤栏，避免整张表判 no-rules 或掉进正文。
        segs = []
        for r, _t in all_lines:
            if taken and any(tr.y0 - 2 <= r.y0 <= tr.y1 + 2 and tr.intersects(r)
                             for tr in taken):
                continue
            segs.append(r)
        reg = _fallback_core(segs, cap_rect, page, margin, stop_lo=stop_lo, stop_hi=stop_hi)
    return reg


# ---------------------------------------------------------------- 逐页
def trace_page(doc, pno, expect):
    """调试入口：逐行显示该页的表题判定与理由（排查 MISSING / no-rules）。"""
    page = doc[pno - 1]
    hl, vl = rules_of(page)
    _inl = inline_rules(page)
    hl_m = merge_with_inline(_merge_rules(hl), _inl)
    print('PAGE=%d size=%.0fx%.0f rules_h=%d inline_rules=%d rules_h_merged=%d '
          'rules_v=%d drawings=%d images=%d'
          % (pno, page.rect.width, page.rect.height, len(hl), len(_inl), len(hl_m),
             len(vl), len(page.get_drawings()), len(page.get_image_info())))
    for y, x0, x1 in sorted(hl_m):
        print('  HRULE y=%.1f x=%.1f..%.1f' % (y, x0, x1))
    lines = []
    for b in page.get_text('dict')['blocks']:
        if b.get('type') != 0:
            continue
        for l in b.get('lines', []):
            t = ''.join(s['text'] for s in l['spans']).strip()
            if t:
                lines.append((fitz.Rect(l['bbox']), t))
    lines.sort(key=lambda x: x[0].y0)

    def prev_gap(cr):
        best = None
        for r, t in lines:
            if r.y1 <= cr.y0 + 1 and _same_col(r, cr, page.rect.width):
                g = cr.y0 - r.y1
                if best is None or g < best:
                    best = g
        return best

    caps = [(r, t, CAP.match(t)) for r, t in lines
            if CAP.match(t) and _is_real_tabno(norm(CAP.match(t).group(1)))]
    for r, t, m in caps:
        no = norm(m.group(1))
        pg = prev_gap(r)
        keep = caption_like(t, m, hl=hl_m, cap_rect=r, page_w=page.rect.width,
                            cap_rects=[c[0] for c in caps], prev_gap=pg)
        print('  CAP no=%s gap=%s len=%d KEEP=%s | %s' % (no, pg, len(t), keep, t[:70]))
    return EXIT_OK


def _top_ink_band(path, dpi=CLIP_DPI):
    """crop 顶部首个**实质**着墨带的 (y0_pt, y1_pt, height_pt, coverage)。

    坐标系是 **crop 自己的**：y0≈0 表示内容顶到裁切边上沿。
    coverage = 该带内最宽一行的着墨像素 / 画布宽。

    ⚠ 会跳过覆盖率 < CLIP_MIN_COV 的条带。抽取用 ``show_pdf_page`` 会把
    相邻栏/相邻表的抗锯齿鬼影一并带进来，实测在 crop 上沿留下 2-8 px 宽、
    覆盖率 0.2%~0.8% 的碎点（#Agu T11/T12/T13）。那是噪声不是内容，
    不跳过会把好表误判成 CLIPPED-TOP。
    """
    d = fitz.open(path)
    try:
        pg = d[0]
        pm = pg.get_pixmap(dpi=dpi)
        W, H, n, stride, buf = pm.width, pm.height, pm.n, pm.stride, pm.samples
        cnt = []
        for y in range(H):
            off = y * stride
            c = 0
            for x in range(W):
                if buf[off + x * n] < CLIP_INK_GREY:
                    c += 1
            cnt.append(c)
    finally:
        d.close()
    s = 72.0 / dpi
    y = 0
    while y < H:
        if cnt[y] <= 0 or cnt[y] / float(W) < CLIP_MIN_COV:
            y += 1
            continue
        y0 = y
        while y + 1 < H and cnt[y + 1] > 0:
            y += 1
        y1 = y
        cov = max(cnt[y0:y1 + 1]) / float(W)
        if cov < CLIP_MIN_COV:          # 整带都是碎点 -> 继续往下找
            y += 1
            continue
        return (y0 * s, (y1 + 1) * s, (y1 + 1 - y0) * s, cov)
    return None


# --------------------------------------------------------------- info scrubbing
INFO_KEY_LIMIT = 60000      # PDF spec limit on a single string is 65535


def strip_oversized_info(nd):
    """Drop inherited metadata that breaks the FINAL pdf link step.

    WHY THIS EXISTS (paper #23, measured):
      ``show_pdf_page`` copies the source document's Info dictionary into the
      output, and an embedded figure exported from drawio/Chrome may carry an
      enormous ``/Subject`` (measured **810,012 bytes** -- a UTF-16BE hex string).
      The XObject keeps a hard reference via ``/PTEX.InfoDict``, so
      ``set_metadata({})`` and even ``save(garbage=4)`` leave the blob in place.

      xdvipdfmx then cannot serialise it:
          PDF string length too long. (limit: 65535)
          Didn't find "endobj"
          error: pdf_link_obj(): passed invalid object.
      and **no final PDF is produced** -- while TeX itself reported success
      ("Output written on main.xdv"), so the gate saw zero ``!`` errors and could
      only report ``no-output-pdf`` with no attribution.

    Fix: neutralise ``PTEX.InfoDict`` on every xref that has one, and blank the
    document metadata.  Measured effect on the culprit file: largest object
    810,012 -> 58,973 bytes (file 1,032,503 -> 261,087).
    """
    n = 0
    try:
        for xref in range(1, nd.xref_length()):
            try:
                key = nd.xref_get_key(xref, "PTEX.InfoDict")
            except Exception:
                continue
            if not key or key[0] in ("null", None):
                continue
            try:
                nd.xref_set_key(xref, "PTEX.InfoDict", "null")
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    try:
        md = nd.metadata or {}
    except Exception:
        md = {}
    big = [k for k, v in md.items() if v and len(str(v)) > INFO_KEY_LIMIT]
    if big or n:
        try:
            nd.set_metadata({})
        except Exception:
            pass
    return {'ptex_info_cleared': n, 'oversized_meta_keys': big}


def detect_clip_top(path):
    """判定导出的表 crop 是否**顶部被裁**（表头行只余下半截）。

    判据（三者同时成立）：
      1. 首个着墨带贴住裁切边上沿：``y0 < CLIP_PT``
      2. 该带**比一条框线厚**：``height > CLIP_RULE_MAX_H``
      3. 它不是一条满宽细线：``coverage < CLIP_RULE_COV``

    (2)(3) 是必需的：很多体例（grid 表、页框）本来就有一条**贴边上沿的
    满宽细框线**，那是正常的（实测 #OMA 38 张里 32 张如此）；只有
    「贴边 + 较厚 + 横向不连续」才是被切掉字形的残片。
    返回 (bool, info)；info 含实测值，便于人工复核与门控报错。
    """
    try:
        b = _top_ink_band(path)
    except Exception as exc:                    # 渲染失败不该让整轮提取崩掉
        return False, {'error': str(exc)}
    if b is None:
        return False, {'empty': True}
    y0, y1, h, cov = b
    clipped = (y0 < CLIP_PT) and (h > CLIP_RULE_MAX_H) and (cov < CLIP_RULE_COV)
    return clipped, {'ink_top': round(y0, 2), 'band_h': round(h, 2),
                     'coverage': round(cov, 3)}


def contact_sheet(doc, records, outdir, pdftoppm=None, dpi=110, cols=3):
    """把全部导出表渲染成联系表，供**目视复核**（status==ok 不可信）。

    渲染用 PyMuPDF（已是硬依赖），**不再要求
    pdftoppm**：本仓库实测环境里 poppler 并未安装，而旧
    写法在缺 pdftoppm 时直接返回 None，于是“必须目视复核”
    这条硬要求在实际使用中总是无法执行。pdftoppm 仍可提供
    作为替代渲染器（留参数以保持接口后向兼容）。
    """
    try:
        import fitz
    except ImportError:
        return None
    ok = sorted([k for k, v in records.items() if v.get('status') == 'ok'],
                key=lambda x: (len(x), x))
    if not ok:
        return None
    thumbs = []
    tmp = os.path.join(outdir, '_sheet')
    os.makedirs(tmp, exist_ok=True)
    for k in ok:
        p = os.path.join(outdir, records[k]['file'])
        if not os.path.isfile(p):
            continue
        try:
            d = fitz.open(p)
            pix = d[0].get_pixmap(dpi=dpi)
            ip = os.path.join(tmp, 't%s.png' % k)
            pix.save(ip)
            d.close()
            thumbs.append((k, ip, pix.width, pix.height))
        except Exception as exc:
            print('WARN: 渲染表 %s 失败：%s' % (k, exc), file=sys.stderr)
    if not thumbs:
        return None
    cw = max(t[2] for t in thumbs)
    rowh = [max(t[3] for t in thumbs[i:i + cols]) for i in range(0, len(thumbs), cols)]
    W = cw * cols
    H = sum(rowh) + 20 * len(rowh)
    sheet = fitz.open()
    pg = sheet.new_page(width=W, height=H)
    y = 0
    for ri in range(0, len(thumbs), cols):
        row = thumbs[ri:ri + cols]
        rh = max(t[3] for t in row)
        for ci, (k, ip, w, h) in enumerate(row):
            rect = fitz.Rect(ci * cw, y, ci * cw + w, y + h)
            pg.insert_image(rect, filename=ip)
            pg.insert_text((ci * cw + 4, y + 12), 'T%s' % k, fontsize=10)
        y += rh + 20
    out = os.path.join(outdir, 'contact_sheet.pdf')
    sheet.save(out)
    sheet.close()
    for _k, ip, _w, _h in thumbs:
        try:
            os.remove(ip)
        except OSError:
            pass
    print('CONTACT-SHEET %s (%d tables)' % (out, len(thumbs)))
    print('NEXT: 打开联系表**逐张目视**，确认导出的确实是对应表号的那张表——'
          'status==ok 只说明「有区域」，不说明「区域对」。')
    return out


def run(args):
    global fitz
    fitz = _require_fitz()
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    for f in glob.glob(os.path.join(outdir, 'tab_*.pdf')):
        os.remove(f)
    pdf = os.path.abspath(args.pdf)
    if not os.path.isfile(pdf):
        print('ERROR: 找不到原件 %s。\n  修法：确认路径，或把原件放进 Paper/。' % pdf,
              file=sys.stderr)
        return EXIT_USAGE
    expect = norm_expect(args.expect.split(',')) if args.expect else None

    doc = fitz.open(pdf)
    if args.trace_page:
        return trace_page(doc, args.trace_page, expect)

    records, skipped_pages = {}, set()
    for pno in range(doc.page_count):
        page = doc[pno]
        text = page.get_text()
        caps = list(CAP.finditer(text))
        if not caps:
            continue
        # 目录页判定：需**同时**满足「匹配数多」且「这些行都很短且以编号开头」。
        # 旧实现只看数量 >=5，会把正文引用多的页误判为目录并整页跳过（真表因此丢失）。
        if len(caps) >= 5:
            _short = 0
            for _b in page.get_text('dict')['blocks']:
                for _l in _b.get('lines', []):
                    _t = ''.join(_s['text'] for _s in _l['spans']).strip()
                    _m = CAP.match(_t)
                    if _m:
                        _after = _t[_m.end():].strip()
                        if len(_t) < 90 and not re.match(r'^[a-z]', _after):
                            _short += 1
            if _short >= 5:
                skipped_pages.add(pno + 1)
                continue
        hl, _vl = rules_of(page)
        # 逻辑框线（同行分段先合并）+ 1x1 拉伸内联图像画出的框线（见 inline_rules）
        hl_m = merge_with_inline(_merge_rules(hl), inline_rules(page))
        # -- 第一遍：收齐本页所有 CAP 命中行（不判定），供「谁离表体更近」比较 --
        raw_caps = []
        for b in page.get_text('dict')['blocks']:
            for l in b.get('lines', []):
                txt = ''.join(s['text'] for s in l['spans']).strip()
                m = CAP.match(txt)
                if not m:
                    continue
                no = norm(m.group(1))
                if not _is_real_tabno(no):      # 排除 "Table of Contents" 这类题头
                    continue
                if expect is not None and no not in expect:
                    continue
                raw_caps.append((fitz.Rect(l['bbox']), txt, m, no))
        raw_caps.sort(key=lambda c: c[0].y0)
        all_rects = [c[0] for c in raw_caps]
        page_lines = []
        for b in page.get_text('dict')['blocks']:
            if b.get('type') != 0:
                continue
            for l in b.get('lines', []):
                t = ''.join(s['text'] for s in l['spans']).strip()
                if t:
                    page_lines.append((fitz.Rect(l['bbox']), t))
        page_lines.sort(key=lambda x: x[0].y0)

        def _prev_gap(cr):
            best = None
            for r, t in page_lines:
                if r.y1 <= cr.y0 + 1 and _same_col(r, cr, page.rect.width):
                    g = cr.y0 - r.y1
                    if best is None or g < best:
                        best = g
            return best

        # -- 第二遍：判定哪些是真表题（需 cap_rects 做「最近者优先」）--
        cap_list = []
        for cr, txt, m, no in raw_caps:
            if not caption_like(txt, m, hl=hl_m, cap_rect=cr, page_w=page.rect.width,
                                cap_rects=all_rects, prev_gap=_prev_gap(cr)):
                continue
            cap_list.append((cr, txt, no))
        cap_rects = [c[0] for c in cap_list]
        # -- 第三遍：逐张表定区域并导出（按 y 顺序，先认领的框线不再给后面的表）--
        used_rules, used_regions = set(), []
        for cap_rect, txt, no in cap_list:
            if no in records:
                continue
            reg = cluster_region(hl_m, _vl, cap_rect.y0, page.rect, band=(None, None),
                                 cap_rect=cap_rect, cap_rects=cap_rects, used=used_rules,
                                 page_lines=page_lines)
            if reg is not None:
                # 表题若**落在区域内部**才裁掉（表题由 LaTeX 渲染中文）。
                # 上边界已经由 _expand_top_for_header 顶到表题之下时，
                # 区域与表题只是相邻而不相交，绝不能按「含表题」处理：
                # 旧判据 `reg.y0 <= cap_rect.y0 <= reg.y1` 在那种情形下为真，
                # 会把区域**下边界**改到表题上方 -> 整张表被压成表题行。
                #
                # 但「表题在左侧边距、与表体 x 不相交」的体例（booktabs 社内
                # 风格，实测 #Agu）必须排除：那时表题与表体只是**并排**，
                # 区域本就该把表题排除在外。若仍走 _newtop = cap_rect.y1 + 2，
                # 上边界会被压到表头行**中间**（T1: 608.1，而表头占
                # 602.2..610.7），整行表头被横切一半——这正是本缺陷。
                if reg.y0 <= cap_rect.y0 and cap_rect.y1 <= reg.y1 and \
                        not _caption_is_beside(cap_rect, reg.x0, reg.x1):
                    _newbot = cap_rect.y0 - 2
                    if _newbot - reg.y0 >= 8:
                        reg = fitz.Rect(reg.x0, reg.y0, reg.x1, _newbot)
                    else:
                        _newtop = cap_rect.y1 + 2
                        if reg.y1 - _newtop >= 8:
                            reg = fitz.Rect(reg.x0, _newtop, reg.x1, reg.y1)
            if reg is not None and (reg.height < 12 or reg.width < 40):
                reg = None
            if reg is None:
                reg = fallback_region(page, cap_rect, margin=48.0, taken=used_regions,
                                      cap_rects=cap_rects)
            if reg is None:
                records[no] = {'status': 'no-rules', 'page': pno + 1}
                continue
            used_regions.append(reg)
            for _h in hl_m:                     # 认领本次区域内的框线
                # 必须**同时**落在 y 与 x 区间内：只按 y 认领会把同一页左右
                # 两栏的表格混为一谈（右栏框线被左栏表认领 -> 右栏表可选框线
                # 被滤空、cluster_region 返回 None 后退回 fallback，把两张表并成
                # 一张；实测 #Lee p13 的 T6/T7 因此全错）。
                if reg.y0 - 2 <= _h[0] <= reg.y1 + 2 and \
                        _h[2] > reg.x0 - 2 and _h[1] < reg.x1 + 2:
                    used_rules.add(_h)
            full = reg
            out = os.path.join(outdir, 'tab_%s.pdf' % no)
            nd = fitz.open()
            pg = nd.new_page(width=full.width, height=full.height)
            pg.show_pdf_page(pg.rect, doc, pno, clip=full)   # **矢量**导出
            strip_oversized_info(nd)
            nd.save(out, garbage=4, clean=True)
            nd.close()
            records[no] = {'status': 'ok', 'page': pno + 1,
                           'pagew': round(page.rect.width, 1),
                           'bbox': [round(full.x0, 1), round(full.y0, 1),
                                    round(full.x1, 1), round(full.y1, 1)],
                           'w': round(full.width, 1), 'h': round(full.height, 1),
                           'file': os.path.basename(out), 'size': os.path.getsize(out)}

    with open(os.path.join(outdir, 'tables.json'), 'w', encoding='utf-8') as fh:
        json.dump(records, fh, ensure_ascii=False, indent=1)

    ok = sorted([k for k, v in records.items() if v['status'] == 'ok'], key=lambda x: (len(x), x))
    norules = sorted([k for k, v in records.items() if v['status'] == 'no-rules'],
                     key=lambda x: (len(x), x))
    tiny = sorted([k for k, v in records.items()
                   if v['status'] == 'ok' and (v.get('w', 0) < TINY_W or v.get('h', 0) < TINY_H)],
                  key=lambda x: (len(x), x))
    missing = sorted((expect or set()) - set(records), key=lambda x: (len(x), x))
    # -- 第 4 条口径：渲染 crop 顶部条带，量首次着墨 y（区域纵向不足）--
    # 必须在 crop 落盘之后做。顶部贴着裁切边上沿 + 比框线厚 + 横向不连续
    # = 第一行（通常正是表头）被横切一半 -> CLIPPED-TOP。
    clip_top = []
    for k in ok:
        v = records[k]
        clipped, info = detect_clip_top(os.path.join(outdir, v['file']))
        v['clip_top'] = bool(clipped)
        v['clip_info'] = info
        if clipped:
            clip_top.append(k)
    with open(os.path.join(outdir, 'tables.json'), 'w', encoding='utf-8') as fh:
        json.dump(records, fh, ensure_ascii=False, indent=1)

    for k in ok:
        v = records[k]
        print('TAB %-10s p%-4s %-8s w=%.1f h=%.1f file=%s'
              % (k, v['page'], 'ok', v['w'], v['h'], v['file']))
    for k in norules:
        print('TAB %-10s p%-4s %-8s (区域判定失败 -> 需人工介入)' % (k, records[k].get('page'), 'no-rules'))
    print('OK-LIST: %s' % json.dumps(ok, ensure_ascii=False))
    print('MISSING: %s' % json.dumps(missing, ensure_ascii=False))
    print('NO-RULES: %s' % json.dumps(norules, ensure_ascii=False))
    print('TINY: %s' % json.dumps(tiny, ensure_ascii=False))
    print('CLIPPED-TOP: %s' % json.dumps(clip_top, ensure_ascii=False))
    print('SKIPPED-TOC-PAGES: %s' % json.dumps(sorted(skipped_pages)))
    print('SUMMARY EXPECTED=%s OK=%d MISSING=%d NORULES=%d TINY=%d CLIPTOP=%d'
          % (len(expect) if expect else 'n/a', len(ok), len(missing), len(norules),
             len(tiny), len(clip_top)))
    if missing or norules or tiny or clip_top:
        print('NEXT: 不能当成「已复刻」放行。')
        if missing:
            print('  MISSING %s -> 表题正则可能漏了该体例，或该表题不在文本层；'
                  '用 --trace-page <页> 看逐行判定。' % ','.join(missing))
        if norules:
            print('  NO-RULES %s -> 有表题但没定到区域；用 --trace-page 看框线，'
                  '必要时改按 fallback（无框线兜底）判定。' % ','.join(norules))
        if tiny:
            print('  TINY %s -> 区域过小（可能裁到单列/单行），必须人工检查。' % ','.join(tiny))
        if clip_top:
            print('  CLIPPED-TOP %s -> 区域**纵向不足**：首行（通常是表头）被裁一半。'
                  '看 clip_info.ink_top≈0 且 band_h/coverage 说明是字形残片；'
                  '多为 booktabs 体例取到了 midrule 而非 toprule（见 references/tables.md）。'
                  % ','.join(clip_top))
    print('REMINDER: status==ok 只表示「有区域」，不表示「区域正确」。'
          '务必 --contact-sheet 后**目视复核**每张表。')
    print('TABLES JSON=%s' % json.dumps(records, ensure_ascii=False))

    if args.contact_sheet:
        contact_sheet(doc, records, outdir, shutil.which(args.pdftoppm or 'pdftoppm'))
    doc.close()
    return EXIT_OK if not (missing or norules or clip_top) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='extract_tables.py',
        description='用 show_pdf_page 把原件表区抽成单页**矢量** PDF（表格复刻，绝不手写 LaTeX 表）。',
        epilog='示例：\n'
               '  python scripts/extract_tables.py Paper/x.pdf .work/01/tables --expect 1,2,III\n'
               '  python scripts/extract_tables.py Paper/x.pdf .work/01/tables --expect 2.1-1 --contact-sheet\n'
               '  python scripts/extract_tables.py Paper/x.pdf out --expect 1 --trace-page 7\n\n'
               '表号归一化（--expect 与输出同口径）：TABLE III -> 3；Table: 7.3.-4 -> 7.3-4；\n'
               '附录式 Table: C.3.1.1-1 保留字母开头。\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pdf', metavar='ORIG.pdf', help='原件 PDF')
    ap.add_argument('outdir', metavar='OUTDIR', help='输出目录（写 tab_<号>.pdf 与 tables.json）')
    ap.add_argument('--expect', metavar='1,2,III',
                    help='期望表号（逗号分隔）；缺失即门控失败')
    ap.add_argument('--contact-sheet', action='store_true',
                    help='导出后把全部表渲染成联系表 contact_sheet.pdf，供目视复核')
    ap.add_argument('--trace-page', type=int, metavar='N',
                    help='只调试第 N 页：逐行打印表题判定与框线，然后退出')
    ap.add_argument('--pdftoppm', metavar='PATH', help='pdftoppm 路径（--contact-sheet 用）')
    args = ap.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
