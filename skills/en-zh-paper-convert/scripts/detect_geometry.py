#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detect_geometry.py -- 逐篇测量原件纸张尺寸与正文版面（页边距），产出 geometry 表。

单位口径（**关键**）
    测量得到的数值是 PostScript point（PDF 页面坐标系，1/72 in），也就是 `bp`。
    写进 LaTeX 时必须用 `bp`：`pt` 是 TeX point（1/72.27 in），
    比值 72/72.27 = 0.99626 会让全库统一缩小 0.374%（612 -> 609.71）。
    本脚本同时输出 `BPARGS=`，可直接粘进模板的 geometry 选项。

测量口径
    ① 抽样 **从第 1 页开始**（正文常从 p1 起，单页/短篇尤其；旧版跳过 p1
       导致 Faulkner 这类「只有 p1 是正文页」的原件一律返回 no-body-page）
    ② 只取「正文页」（文本块 >= 8），跳过封面/目录/整页图页
    ③ 取该页文本块并集的 bbox
    ④ 按 3% / 97% 分位去离群（水印、页眉页脚、出血元素）
    ⑤ 取中位数 -> paper / margins(L,T,R,B) / text_width / text_height

失败口径（**不得静默降级**）
    真的测不出（全部是图版页 / 无文本层）时以 GEOMETRY=none 显式报告。
    加 --require 则整批以退出码 3 失败，供门控使用：调用方**禁止**在这种
    情况下回落到模板默认边距还报 PASS（那正是本脚本要修的假成功）。

依赖
    Python 3.9+ 与 PyMuPDF：pip install pymupdf

用法
    python scripts/detect_geometry.py Paper/ --json .work/geometry.json
    python scripts/detect_geometry.py Paper/ --require --json .work/geometry.json
    python scripts/detect_geometry.py Paper/x.pdf --baseline 612x792

结构化输出
    FILE=<名> PAPER=612x792 MM=216x279 MARGINS=L/T/R/B TEXT_W=514.1 TEXT_H=742.3
    BPARGS=paperwidth=612bp,paperheight=792bp,left=49.0bp,right=48.9bp,...
    SUMMARY FILES=n OK=n DEGENERATE=n
    GEOMETRY JSON=[...]
"""
import argparse
import glob
import json
import os
import statistics as st
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

EXIT_OK, EXIT_USAGE, EXIT_DEP = 0, 2, 3
PT2MM = 25.4 / 72.0
INSTALL_MISSING = '缺少 PyMuPDF。安装：\n  pip install pymupdf'


def _require_fitz():
    try:
        import fitz
        return fitz
    except ImportError:
        print('ERROR: ' + INSTALL_MISSING, file=sys.stderr)
        sys.exit(EXIT_DEP)


TEXT_MIN_W, TEXT_MIN_H = 60.0, 12.0


def _body_boxes(page):
    """Text blocks big enough to be body matter (not running heads/footers)."""
    out = []
    for b in page.get_text('dict')['blocks']:
        if b.get('type') != 0:
            continue
        bb = b['bbox']
        if (bb[2] - bb[0]) >= TEXT_MIN_W and (bb[3] - bb[1]) >= TEXT_MIN_H:
            out.append(bb)
    return out


def _sample_idx(n, sample, cover=False):
    """Page indexes to measure.

    DEFECT (blocking): sampling used to start at index 1, i.e. it skipped page
    1 outright.  Measured on the Faulkner 2022 original (14 pages): every page
    has 8-11 raw text blocks, but only page 1 has >= 8 blocks that are ALSO
    wide/tall enough to count as body matter -- pages 2..14 are two-column
    plates whose per-column blocks are narrower than the 60pt body-block floor,
    so they never qualify.  The sampler therefore found no body page at all,
    returned GEOMETRY=none, and the caller silently substituted the template's
    default letter margins.  The downstream gate compares only the PAGE SIZE,
    so the run still reported PASS: a false success shipping wrong margins.

    Page 1 is consequently sampled FIRST -- but not unconditionally.  On a
    paper with a COVER page (measured on Warden 2023) page 1 is a masthead
    whose stray blocks sit at y=-12.9/9.3 above the real masthead, while the
    body starts on page 2; forcing page 1 in drags `top` to 51.6 and `left` to
    36.0 and CORRUPTS a previously good measurement.  So `cover=True` (decided
    by _has_cover_page) keeps the original behaviour and starts after page 1.

    The final page is always excluded: it is normally the reference list or an
    appendix figure plate.
    """
    if n <= 1:
        return [0]
    first = 1 if cover else 0
    last = n - 1                          # exclusive bound, never the final page
    return list(range(first, max(first + 1, min(last, first + sample))))


def _census(doc, idx):
    """(all text blocks, qualifying body boxes) per page."""
    out = {}
    for p in idx:
        page = doc[p]
        out[p] = (sum(1 for b in page.get_text('dict')['blocks'] if b.get('type') == 0),
                  len(_body_boxes(page)))
    return out


def _has_cover_page(doc, sample, min_blocks):
    """True when page 1 must NOT be measured.

    Signature of a genuine front-matter page (Warden 2023, 4 pages): page 1 is
    not itself a body page -- it holds a masthead plus stray blocks above it --
    yet SOME later page already is, so page 1 adds nothing but noise.  Forcing
    it in drags `top` to 51.6 and `left` to 36.0 and corrupts a previously good
    measurement.

    The Faulkner case is the exact opposite and must KEEP page 1: page 1 is the
    only body page and NO later page qualifies.

    Witnessing page 2 alone (the original plan) is not enough: Warden's page 2
    yields only 5 qualifying boxes and so fails the bar even though page 1 is
    plainly front matter.  Witnessing the whole tail would instead hand
    Faulkner's later plate pages to the sampler and, measured, does re-break
    the very defect being fixed -- so the witness is the tail seen WITHOUT page
    1 and WITHOUT the final page, which is what this call does.
    """
    if doc.page_count <= 2:
        return False
    try:
        c = _census(doc, _sample_idx(doc.page_count, sample, cover=True))
    except Exception:
        return False
    p1 = c.get(0, (0, 0))
    if p1[0] >= min_blocks and p1[1] >= min_blocks:
        return False                      # page 1 is itself a body page
    return any(v[1] >= min_blocks for k, v in c.items() if k != 0)


def geometry(path, fitz, sample=14, min_blocks=8, require=True):
    """Measure paper size and page margins from the ORIGINAL PDF.

    `require` is accepted for interface symmetry with --require; the body-page
    test is always enforced here, so "nothing measurable" surfaces as the
    explicit 'no-body-page' failure and never as a silent default.
    """
    doc = fitz.open(path)
    n = doc.page_count
    cover = _has_cover_page(doc, sample, min_blocks)
    idx = _sample_idx(n, sample, cover)
    sizes = {}
    x0s, x1s, y0s, y1s = [], [], [], []
    for p in idx:
        page = doc[p]
        sizes[(round(page.rect.width), round(page.rect.height))] = \
            sizes.get((round(page.rect.width), round(page.rect.height)), 0) + 1
        # Drop running heads / footers / page numbers before measuring margins.
        # They sit at the very top/bottom and are far smaller than body blocks,
        # so letting them through drags `top`/`bottom` to the page edge
        # (measured on a CACM page: top=12.8pt, bottom=20.0pt while the real
        # body starts at y=51.6 -- a bogus floor-to-ceiling text area that then
        # stole the whole vertical space from the layout).
        boxes = _body_boxes(page)
        if len(boxes) < min_blocks:        # body-page test: enough text blocks
            continue
        xs0 = sorted(b[0] for b in boxes)
        xs1 = sorted(b[2] for b in boxes)
        ys0 = sorted(b[1] for b in boxes)
        ys1 = sorted(b[3] for b in boxes)
        # Outlier trim: 3%/97% percentile.
        #
        # DO NOT "fix" this to "drop exactly one box per side" -- that was tried
        # and reverted.  A fresh-agent trial reported the fraction as a defect
        # (claiming #19's top should be ~57pt, not 72.4pt).  Measuring across all
        # 28 papers (Temp/harness/eval_geom_rules.py) showed the opposite:
        #
        #     rule      #19    #04    #07    #09    #20    #26    #27    #28
        #     frac      72.4   58.9   85.2   76.9   58.4   50.9   49.4   68.5
        #     one       98.5  243.8  177.1  218.1  113.5   71.5   90.2  100.1
        #
        # "one" is worse on 24 of 28 papers.  Nor is the quoted 57pt attainable:
        # on #19 the per-page MINIMUM top is genuinely heterogeneous -- 56.9pt on
        # text pages but 212-280pt on algorithm/figure pages -- so the pooled
        # distribution has no single correct value, and a 15pt error does not
        # exist to be removed.  The percentile survives precisely because it
        # adapts to how many stray boxes a page happens to carry.
        k = max(0, int(len(xs0) * 0.03))
        x0s.append(xs0[k]); x1s.append(xs1[-1 - k])
        y0s.append(ys0[k]); y1s.append(ys1[-1 - k])
    doc.close()
    if not sizes:
        return None, 'no-pages'
    W, H = max(sizes, key=sizes.get)
    if not x0s:
        return None, 'no-body-page'
    left, right = st.median(x0s), st.median(x1s)
    top, bot = st.median(y0s), st.median(y1s)
    rec = {'paper_pt': [W, H], 'paper_mm': [round(W * PT2MM), round(H * PT2MM)],
           'margins_pt': [round(left, 1), round(top, 1), round(W - right, 1), round(H - bot, 1)],
           'text_width_pt': round(right - left, 1), 'text_height_pt': round(bot - top, 1),
           'size_counts': {'%dx%d' % k: v for k, v in sizes.items()},
           'pages_sampled': len(idx), 'body_pages': len(x0s)}
    warn = None
    if rec['margins_pt'][0] < -1 or rec['margins_pt'][2] < -1:
        warn = 'negative-margin'          # 测量被水印/出血污染
    elif rec['text_width_pt'] > W:
        warn = 'text-wider-than-paper'
    elif require and rec['body_pages'] < 1:
        warn = 'no-body-page'
    return rec, warn


def bp_args(rec):
    """生成可直接粘进 geometry 的 bp 选项串（**不是** pt）。"""
    W, H = rec['paper_pt']
    L, T, R, B = rec['margins_pt']
    return ('paperwidth=%gbp,paperheight=%gbp,left=%gbp,right=%gbp,top=%gbp,bottom=%gbp'
            % (W, H, L, R, T, B))


def collect(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out.extend(sorted(glob.glob(os.path.join(p, '*.pdf'))))
        else:
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        a = os.path.abspath(p)
        if a not in seen:
            seen.add(a); uniq.append(p)
    return uniq


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='detect_geometry.py',
        description='逐篇测量原件纸张与页边距，输出 bp 单位的几何参数（照原件出稿的输入）。',
        epilog='示例：\n'
               '  python scripts/detect_geometry.py Paper/ --json .work/geometry.json\n'
               '  python scripts/detect_geometry.py Paper/x.pdf --baseline 612x792\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*', default=['Paper'], metavar='PDF|DIR',
                    help='PDF 文件或目录（默认 Paper）')
    ap.add_argument('--json', metavar='OUT.json', help='写出 geometry JSON（供 runpaper 使用）')
    ap.add_argument('--sample', type=int, default=14, help='每篇抽样的正文页数（默认 14）')
    ap.add_argument('--min-blocks', type=int, default=8,
                    help='判定「正文页」的最少文本块数（默认 8）')
    ap.add_argument('--baseline', metavar='WxH',
                    help='基准纸张（如 612x792）；超过 --tol 的偏差会标记 OUTLIER')
    ap.add_argument('--tol', type=float, default=6.0, help='基准偏差容差 pt（默认 6）')
    ap.add_argument('--require', action='store_true',
                    help='任一篇测不出正文页即以退出码 3 显式失败（门控用；'
                         '禁止静默回落到模板默认边距）')
    args = ap.parse_args(argv)

    fitz = _require_fitz()
    files = collect(args.paths)
    if not files:
        print('ERROR: 没找到任何 PDF（路径：%s）。\n  修法：确认路径或把原件放进 Paper/。'
              % ', '.join(args.paths), file=sys.stderr)
        return EXIT_USAGE

    base = None
    if args.baseline:
        try:
            bw, bh = [float(x) for x in args.baseline.lower().split('x')]
            base = (bw, bh)
        except Exception:
            print('ERROR: --baseline 格式应为 WxH（如 612x792）。', file=sys.stderr)
            return EXIT_USAGE

    records, n_ok, n_bad = [], 0, 0
    for f in files:
        name = os.path.basename(f)
        try:
            rec, warn = geometry(f, fitz, args.sample, args.min_blocks)
        except Exception as exc:
            print('FILE=%s GEOMETRY=error ERROR=%s' % (name, str(exc).replace('\n', ' ')))
            n_bad += 1
            continue
        if rec is None:
            # NEVER fail silently: this is an explicit, loud failure.  The
            # old message guessed "the PDF is a scan, run OCR" for every case,
            # which is wrong for the far more common picture of a page whose
            # text blocks are simply all figure-plate fragments.
            print('FILE=%s GEOMETRY=none REASON=%s' % (name, warn))
            if warn == 'no-body-page':
                print('  FAIL: 抽样页（含第 1 页）里没有任何一页满足「正文页」判据'
                      '（文本块 >= %d，且宽/高 >= 60/12pt）。' % args.min_blocks)
                print('  NEXT: 该篇是整页图版式（插图册/扫描件）。'
                      '必须先 OCR 或人工量取，',
                      end='')
                print('然后在台账里写死纸张与边距；'
                      '**不得**让它回落到模板默认 letter 边距后仍报 PASS。')
                if args.require:
                    print('  --require 已开启：以退出码 %d 显式失败。' % EXIT_DEP)
                    n_bad += 1
                    return EXIT_DEP
            else:
                print('  NEXT: 该 PDF 可能无文本层（扫描件）。先做 OCR，'
                      '否则文本锚定流程都不成立。')
            n_bad += 1
            continue
        W, H = rec['paper_pt']
        L, T, R, B = rec['margins_pt']
        flags = []
        if warn:
            flags.append(warn.upper())
        if base and (abs(W - base[0]) > args.tol or abs(H - base[1]) > args.tol):
            flags.append('OUTLIER')
        rec['file'] = name
        rec['flags'] = flags
        rec['bpargs'] = bp_args(rec)
        records.append(rec)
        n_ok += 1
        print('FILE=%s PAPER=%dx%d MM=%dx%d MARGINS=%.1f/%.1f/%.1f/%.1f TEXT_W=%s TEXT_H=%s FLAGS=%s'
              % (name, W, H, rec['paper_mm'][0], rec['paper_mm'][1], L, T, R, B,
                 rec['text_width_pt'], rec['text_height_pt'], ','.join(flags) or '-'))
        print('  BPARGS=%s' % rec['bpargs'])

    print('SUMMARY FILES=%d OK=%d DEGENERATE=%d' % (len(files), n_ok, n_bad))
    if n_ok and n_ok < len(files):
        print('REMINDER: 缺几何数据的篇目必须显式标注，不得静默用模板默认值（模板会打印警告）。')
    print('GEOMETRY JSON=%s' % json.dumps(records, ensure_ascii=False))

    if args.json and records:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            with open(args.json, 'w', encoding='utf-8') as fh:
                json.dump(records, fh, ensure_ascii=False, indent=1)
            print('WROTE %s' % args.json)
        except OSError as exc:
            print('ERROR: 写 %s 失败：%s\n  修法：换一个可写路径。' % (args.json, exc),
                  file=sys.stderr)
            return EXIT_USAGE
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
