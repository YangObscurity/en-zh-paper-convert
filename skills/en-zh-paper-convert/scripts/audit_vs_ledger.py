#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_vs_ledger.py -- 用「交付 PDF 实际页面尺寸 vs 台账纸张」做**全库**核对。

为什么值得单独跑
    用**产物客观量**做全库核对，能把「28 篇都要重做」收缩为「只剩 5 篇」——
    避免在已经正确的篇目上做无用功。这是基准实测有效的一条做法。

口径
    台账第 10 列「纸张(pt)」是 PostScript point（= bp）。
    容差 ±3pt：geometry 的舍入（取整）会带来 1-3pt 偏差。
    若**全库同方向、同比例**超差（如 612 -> 609.71，比例 0.99626），
    那不是阈值问题，而是**单位错**（用了 pt 而非 bp）——先查系统偏差，再谈阈值。

依赖
    Python 3.9+ 与 PyMuPDF：pip install pymupdf

用法
    python scripts/audit_vs_ledger.py Paper/ --ledger .work/format-ledger.tsv
    python scripts/audit_vs_ledger.py Paper/ --ledger .work/format-ledger.tsv --json .work/geom_audit.json

结构化输出
    NN=01 LEDGER=595x842 ACTUAL=593x839 DEV=3 VERDICT=OK
    SUMMARY FILES=28 OK=27 DEVIANT=1 MISSING=0
    DEVIANT: [05]
    LAYOUT-MIX: none
    AUDIT JSON=[...]
"""
import argparse
import glob
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

EXIT_OK, EXIT_USAGE, EXIT_DEP = 0, 2, 3
INSTALL = '缺少 PyMuPDF。安装：\n  pip install pymupdf'
TOL = 3.0
# 已知的单位错比例：pt/bp = 72/72.27
PT_OVER_BP = 72.0 / 72.27


def _require_fitz():
    try:
        import fitz
        return fitz
    except ImportError:
        print('ERROR: ' + INSTALL, file=sys.stderr)
        sys.exit(EXIT_DEP)


def read_ledger(path):
    led = {}
    if not path or not os.path.isfile(path):
        return led
    lines = open(path, encoding='utf-8').read().split('\n')
    for ln in lines[1:]:
        p = ln.split('\t')
        if len(p) >= 10 and p[0].strip():
            led[p[0].strip()] = {'orig': p[1].strip(),
                                 'layout': p[2].strip() if len(p) > 2 else '',
                                 'mode': p[7].strip() if len(p) > 7 else '',
                                 'paper': p[9].strip() if len(p) > 9 else '',
                                 'margins': p[10].strip() if len(p) > 10 else ''}
    return led


def actual_paper(pdf, fitz):
    """交付 PDF 的主流页面尺寸（首页可能是通栏封面，不等于正文纸张）。"""
    d = fitz.open(pdf)
    sizes = {}
    for pg in d:
        k = (round(pg.rect.width), round(pg.rect.height))
        sizes[k] = sizes.get(k, 0) + 1
    d.close()
    if not sizes:
        return None, {}
    return max(sizes, key=sizes.get), sizes


def find_pdf(paper_dir, nn):
    hits = [f for f in glob.glob(os.path.join(paper_dir, '%s_*.pdf' % nn))
            if '-new' not in f and not f.endswith('.tmp.pdf')]
    return sorted(hits)[0] if hits else None


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='audit_vs_ledger.py',
        description='全库核对：交付 PDF 实际页面尺寸 vs 台账纸张（±3pt），并识别「单位错」型系统偏差。',
        epilog='示例：\n'
               '  python scripts/audit_vs_ledger.py Paper/ --ledger .work/format-ledger.tsv\n'
               '  python scripts/audit_vs_ledger.py Paper/ --ledger .work/format-ledger.tsv --json .work/geom_audit.json\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paper_dir', nargs='?', default='Paper', metavar='DIR',
                    help='交付 PDF 所在目录（默认 Paper）')
    ap.add_argument('--ledger', required=True, metavar='TSV', help='版式台账（必需）')
    ap.add_argument('--tol', type=float, default=TOL, help='容差 pt（默认 3）')
    ap.add_argument('--json', metavar='OUT.json', help='写出结构化结果')
    args = ap.parse_args(argv)

    fitz = _require_fitz()
    led = read_ledger(args.ledger)
    if not led:
        print('ERROR: 台账 %s 读不到或没有有效记录。\n'
              '  修法：确认路径；台账须是制表符分隔、首行表头、第 10 列为纸张（如 595x842）。'
              % args.ledger, file=sys.stderr)
        return EXIT_USAGE

    rows, deviants, missing = [], [], []
    ratios = []
    for nn in sorted(led):
        rec = led[nn]
        pdf = find_pdf(args.paper_dir, nn)
        if not pdf:
            missing.append(nn)
            print('NN=%s LEDGER=%s ACTUAL=- DEV=- VERDICT=NO-PDF' % (nn, rec['paper'] or '?'))
            continue
        try:
            size, sizes = actual_paper(pdf, fitz)
        except Exception as exc:
            print('NN=%s VERDICT=ERROR %s' % (nn, exc))
            rows.append({'nn': nn, 'verdict': 'ERROR', 'error': str(exc)})
            continue
        if not size:
            print('NN=%s VERDICT=EMPTY-PDF' % nn)
            continue
        try:
            lw, lh = [float(x) for x in rec['paper'].lower().split('x')]
        except Exception:
            print('NN=%s LEDGER=%s ACTUAL=%dx%d VERDICT=NO-LEDGER-PAPER'
                  % (nn, rec['paper'] or '?', size[0], size[1]))
            rows.append({'nn': nn, 'actual': '%dx%d' % size, 'verdict': 'NO-LEDGER-PAPER'})
            continue
        dev = max(abs(size[0] - lw), abs(size[1] - lh))
        ok = dev <= args.tol
        if ok and size[0]:
            ratios.append(size[0] / lw)
        if not ok:
            deviants.append(nn)
        print('NN=%s LEDGER=%gx%g ACTUAL=%dx%d DEV=%.1f VERDICT=%s'
              % (nn, lw, lh, size[0], size[1], dev, 'OK' if ok else 'DEVIANT'))
        rows.append({'nn': nn, 'ledger': '%gx%g' % (lw, lh),
                     'actual': '%dx%d' % size, 'dev': round(dev, 2),
                     'mode': rec['mode'], 'layout': rec['layout'],
                     'verdict': 'OK' if ok else 'DEVIANT'})

    npass = sum(1 for r in rows if r.get('verdict') == 'OK')
    print('SUMMARY FILES=%d OK=%d DEVIANT=%d MISSING=%d'
          % (len(led), npass, len(deviants), len(missing)))
    if deviants:
        print('DEVIANT: %s' % json.dumps(deviants))
    if missing:
        print('MISSING-PDF: %s' % json.dumps(missing))
        print('  NEXT: 这些篇目还没出稿 -> 跑 runpaper.py <NN>。')
    # 系统偏差识别：全库同方向同比例 -> 单位错，不是阈值问题
    if ratios:
        avg = sum(ratios) / len(ratios)
        if abs(avg - PT_OVER_BP) < 0.002:
            print('SYSTEMATIC-BIAS: 实测/台账 平均比例 = %.5f ≈ 72/72.27 = %.5f'
                  % (avg, PT_OVER_BP))
            print('  DIAGNOSIS: 这是**单位错**——geometry 用了 pt（TeX point）而非 bp'
                  '（PostScript point），全库统一缩小 0.374%。')
            print('  NEXT: 把模板里的 geometry 单位改成 bp（见 references/layout-rules.md §3.2）。'
                  '不要靠调阈值绕过。')
        elif abs(avg - 1.0) > 0.002:
            print('SYSTEMATIC-BIAS: 平均比例 = %.5f（偏离 1.0）-> 检查几何台账或模板纸张设定。'
                  % avg)
    print('REMINDER: 本核对只查页面尺寸（客观量）。栏数、栏间距、页边距观感仍须**并排目视对读**。')
    print('AUDIT JSON=%s' % json.dumps(rows, ensure_ascii=False, default=str))

    if args.json:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            with open(args.json, 'w', encoding='utf-8') as fh:
                json.dump(rows, fh, ensure_ascii=False, indent=1, default=str)
            print('WROTE %s' % args.json)
        except OSError as exc:
            print('ERROR: 写 %s 失败：%s' % (args.json, exc), file=sys.stderr)
            return EXIT_USAGE
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
