#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detect_format.py -- 判定原件版式（single / double / mixed）并给出证据。

    !! 机器判定只是「候选」，不是定稿。!!
    28 篇基准论文实测三种自动判据全部有假阳/假阴：
      · 行宽中位数  -> 图表密集页把中位数拉低，真单栏被判成 double（实测 S1 Warden_2023）
      · 中缝空白带  -> 被跨栏大图污染
      · 纯文本跨中缝 -> 把全部 28 篇双栏论文误判为单栏
    只有目视可靠（约束 R3）。本脚本的用途是「决定哪些篇目需要重点看」，
    判定结果必须经目视确认后才可写进 format-ledger.tsv。

依赖
    Python 3.9+ 与 PyMuPDF
        pip install pymupdf
    缺失时本脚本打印安装命令并以 2 退出。

用法
    python scripts/detect_format.py Paper/ --json .work/fmt.json
    python scripts/detect_format.py "Paper/Warden 等 - 2023 - Machine Learning Sensors.pdf"

结构化输出（每篇一行，便于 agent 解析）
    FILE=<名字> FORMAT=single|double|mixed|unknown MID_RATIO=0.618 L=.. M=.. R=..
    MED_W_RATIO=0.924 PAGES=4
    SUMMARY FILES=4 SINGLE=1 DOUBLE=3 MIXED=0 UNKNOWN=0
    VERDICT JSON={"..."}         # --json 时给出所有记录的完整 JSON
"""
import argparse
import glob
import json
import os
import sys

try:  # Windows 控制台默认 GBK，遇非 ASCII 文件名会 UnicodeEncodeError 崩掉
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

EXIT_OK, EXIT_USAGE, EXIT_DEP = 0, 2, 3

# 经验阈值（基准实测）：中缝块比例高 -> 单栏；左右两簇且中缝少 -> 双栏
MID_SINGLE = 0.55
MID_DOUBLE = 0.35
LR_DOUBLE = 0.70
# 边缘例：数值落在这个区间必须逐页高清目视
EDGE_LO, EDGE_HI = 0.20, 0.60

INSTALL_MISSING = ('缺少 PyMuPDF。安装：\n'
                   '  pip install pymupdf\n'
                   '若使用虚拟环境，请确认调用的是同一个 python。')


def _require_fitz():
    try:
        import fitz  # noqa: F401
        return fitz
    except ImportError:
        print('ERROR: ' + INSTALL_MISSING, file=sys.stderr)
        sys.exit(EXIT_DEP)


def detect(path, fitz):
    """返回 (fmt, evidence)。evidence 里带全部判据，供目视审计时参考。"""
    doc = fitz.open(path)
    n = doc.page_count
    left = right = middle = 0
    widths = []
    for i in range(n):
        page = doc[i]
        W = page.rect.width
        for b in page.get_text('dict')['blocks']:
            if b.get('type') != 0:
                continue
            x0, y0, x1, y1 = b['bbox']
            w = x1 - x0
            if w < 60 or (y1 - y0) < 12:   # 太窄/太矮的块（页码、表题碎片）不计
                continue
            cx = (x0 + x1) / 2
            widths.append(w / W)
            if cx < W * 0.42:
                left += 1
            elif cx > W * 0.58:
                right += 1
            else:
                middle += 1
    doc.close()
    total = left + right + middle
    if not total:
        return 'unknown', {'pages': n, 'blocks': 0}
    mid_ratio = middle / total
    lr_ratio = (left + right) / total
    if mid_ratio > MID_SINGLE:
        fmt = 'single'
    elif lr_ratio > LR_DOUBLE and mid_ratio < MID_DOUBLE:
        fmt = 'double'
    else:
        fmt = 'mixed'
    ev = {'pages': n, 'blocks': total, 'left': left, 'right': right, 'middle': middle,
          'mid_ratio': round(mid_ratio, 3), 'lr_ratio': round(lr_ratio, 3),
          'median_block_width_ratio': round(sorted(widths)[len(widths) // 2], 3) if widths else 0}
    return fmt, ev


def needs_visual(ev):
    """该篇是否属于「必须逐页高清目视」的边缘例。"""
    if not ev or ev.get('blocks', 0) == 0:
        return True
    mr = ev.get('mid_ratio', 0)
    if EDGE_LO <= mr <= EDGE_HI:
        return True
    l, m, r = ev.get('left', 0), ev.get('middle', 0), ev.get('right', 0)
    tot = max(1, l + m + r)
    return abs(l - r) / float(tot) > 0.6   # 块分布严重不对称


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
            seen.add(a)
            uniq.append(p)
    return uniq


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='detect_format.py',
        description='按正文文本块的水平分布给出「单栏/双栏」候选判定与证据（仅候选，须目视复核）。',
        epilog='示例：\n'
               '  python scripts/detect_format.py Paper/\n'
               '  python scripts/detect_format.py Paper/ --json .work/fmt.json\n'
               '  python scripts/detect_format.py Paper/x.pdf --edge-only\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*', default=['Paper'], metavar='PDF|DIR',
                    help='PDF 文件或包含 PDF 的目录（默认 Paper）')
    ap.add_argument('--json', metavar='OUT.json', help='把完整记录写成 JSON')
    ap.add_argument('--edge-only', action='store_true',
                    help='只输出需要目视复核的边缘例')
    args = ap.parse_args(argv)

    fitz = _require_fitz()
    files = collect(args.paths)
    if not files:
        print('ERROR: 没找到任何 PDF（路径：%s）。\n'
              '  修法：确认路径正确，或把原件放进 Paper/ 后重跑。' % ', '.join(args.paths),
              file=sys.stderr)
        return EXIT_USAGE

    rows = []
    for f in files:
        name = os.path.basename(f)
        try:
            fmt, ev = detect(f, fitz)
        except Exception as exc:
            print('FILE=%s FORMAT=error ERROR=%s' % (name, str(exc).replace('\n', ' ')))
            rows.append({'file': name, 'format': 'error', 'error': str(exc)})
            continue
        edge = needs_visual(ev)
        rec = {'file': name, 'format': fmt, 'edge_case': edge}
        rec.update(ev or {})
        rows.append(rec)
        if args.edge_only and not edge:
            continue
        print('FILE=%s FORMAT=%s MID_RATIO=%s L=%s M=%s R=%s MED_W_RATIO=%s PAGES=%s EDGE=%s'
              % (name, fmt, ev.get('mid_ratio', '?'), ev.get('left', '?'), ev.get('middle', '?'),
                 ev.get('right', '?'), ev.get('median_block_width_ratio', '?'),
                 ev.get('pages', '?'), 'yes' if edge else 'no'))

    n = {'single': 0, 'double': 0, 'mixed': 0, 'unknown': 0, 'error': 0}
    for r in rows:
        n[r.get('format', 'unknown')] = n.get(r.get('format', 'unknown'), 0) + 1
    edges = [r['file'] for r in rows if r.get('edge_case')]
    print('SUMMARY FILES=%d SINGLE=%d DOUBLE=%d MIXED=%d UNKNOWN=%d ERROR=%d EDGE=%d'
          % (len(rows), n['single'], n['double'], n['mixed'], n['unknown'], n['error'], len(edges)))
    if edges:
        print('NEEDS-VISUAL: ' + ', '.join(edges))
        print('REMINDER: 机器判定仅为候选；上述篇目必须渲染正文页逐页目视后，'
              '才可写入 format-ledger.tsv（约束 R3）。')
    print('VERDICT JSON=%s' % json.dumps(rows, ensure_ascii=False))

    if args.json:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            with open(args.json, 'w', encoding='utf-8') as fh:
                json.dump(rows, fh, ensure_ascii=False, indent=1)
            print('WROTE %s' % args.json)
        except OSError as exc:
            print('ERROR: 写 %s 失败：%s\n  修法：换一个可写路径。' % (args.json, exc),
                  file=sys.stderr)
            return EXIT_USAGE
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
