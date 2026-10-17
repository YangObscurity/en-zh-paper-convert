#!/usr/bin/env python3
"""classify_papers.py -- 按版式/出版方风格给测试论文分类（用于挑选代表性样本）。

用法:
    python scripts/classify_papers.py [Paper目录] [--json out.json]

输出每篇：页数、页面尺寸、单/双栏判定（行宽中位数 + 文本块水平分布）、预估风格标签。
"""
import sys, os, glob, json, statistics as st

# Windows 控制台默认 GBK，遇到非 ASCII 文件名会 UnicodeEncodeError → 统一切到 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: 需要 PyMuPDF。安装: pip install pymupdf", file=sys.stderr)
    sys.exit(2)


def analyze(path, sample=12):
    d = fitz.open(path)
    n = d.page_count
    idx = [p for p in range(1, max(2, min(n - 1, 1 + sample)))] or [0]
    sizes, widths = {}, []
    for p in idx:
        page = d[p]
        k = (round(page.rect.width, 1), round(page.rect.height, 1))
        sizes[k] = sizes.get(k, 0) + 1
        for b in page.get_text('dict')['blocks']:
            if b.get('type') != 0:
                continue
            x0, y0, x1, y1 = b['bbox']
            if (x1 - x0) < 40 or (y1 - y0) < 10:
                continue
            widths.append((x1 - x0) / page.rect.width)
    d.close()
    W, H = max(sizes, key=sizes.get) if sizes else (0, 0)
    med = st.median(widths) if widths else 0
    # 经验阈值（基准实测）：≤0.50 双栏；≥0.60 单栏；中间存疑
    layout = 'double' if med <= 0.50 else ('single' if med >= 0.60 else 'ambiguous')
    return {'pages': n, 'size': [W, H], 'median_line_w': round(med, 3), 'layout': layout}


def main():
    import argparse
    ap = argparse.ArgumentParser(
        prog='classify_papers.py',
        description='按版式给论文 PDF 分类（单栏/双栏候选）。'
                    '⚠️ 输出仅为**候选**：图表密集页会把行宽中位数拉低，'
                    '导致真单栏被判成 double。必须目视确认（见 references/layout-rules.md R3）。',
        epilog='示例: python scripts/classify_papers.py Paper --json .work/classify.json')
    ap.add_argument('root', nargs='?', default='Paper',
                    help='含 PDF 的目录（默认 Paper）')
    ap.add_argument('--json', metavar='OUT.json', help='同时写出 JSON 结果')
    ap.add_argument('--sample', type=int, default=12,
                    help='每篇抽样的正文页数（默认 12）')
    args = ap.parse_args()

    root = args.root
    files = sorted(glob.glob(os.path.join(root, '*.pdf')))
    if not files:
        print(f"ERROR: {root} 下没有 PDF 文件。请确认路径，或把论文放进 Paper/ 目录。", file=sys.stderr)
        sys.exit(2)

    rows = []
    for f in files:
        name = os.path.basename(f)
        try:
            info = analyze(f, sample=args.sample)
        except Exception as e:
            rows.append({'file': name, 'error': str(e)})
            continue
        info['file'] = name
        info['mb'] = round(os.path.getsize(f) / 1048576, 1)
        rows.append(info)

    print('%-52s %-5s %-12s %-10s %s' % ('文件', '页', '页面(pt)', '行宽中位', '版式(候选)'))
    for r in rows:
        if 'error' in r:
            print('%-52s ERROR %s' % (r['file'][:50], r['error'][:40]))
            continue
        print('%-52s %-5d %-12s %-10s %s' % (
            r['file'][:50], r['pages'], '%gx%g' % tuple(r['size']),
            r['median_line_w'], r['layout']))

    if args.json:
        d = os.path.dirname(args.json)
        if d:
            os.makedirs(d, exist_ok=True)
        json.dump(rows, open(args.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('\n已写出', args.json)

    s = sum(1 for r in rows if r.get('layout') == 'single')
    d_ = sum(1 for r in rows if r.get('layout') == 'double')
    a = sum(1 for r in rows if r.get('layout') == 'ambiguous')
    print('\n合计 %d 篇：单栏候选 %d、双栏候选 %d、存疑 %d' % (len(rows), s, d_, a))
    print('⚠️ 以上均为**候选**。图表密集页会漏判（实测：整页信息图的单栏论文被判 double）。'
          '出稿前必须目视确认并登记台账。')


if __name__ == '__main__':
    main()
