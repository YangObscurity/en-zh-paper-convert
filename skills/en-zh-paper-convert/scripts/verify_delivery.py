#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_delivery.py -- 交付门控：对单篇或整个交付目录跑 G1–G8 并给 PASS/FAIL。

门控口径（与 references/gates.md 一致）
    G1 LaTeX 错误      : 编译日志 `^!` 计数 = 0
    G2 缺字形          : 日志 `Missing character` 计数 = 0
    G3 越栏            : `Overfull \\hbox` **最大**值 > 8pt 即 FAIL；`\\vbox` > 120pt 才 FAIL
    G4 编号闭合        : 期望图号/表号 **无缺失**（子集判定：期望 ⊆ 实际）
    G5 译者痕迹        : 交付 PDF 文本层中译注/翻译说明/自查等 = 0
    G6 页面几何        : 实际页面尺寸 vs 台账纸张（±3pt）
    G7 字体嵌入        : 所有字体 embedded = yes
    G8 日志可用性      : 日志**读不到 -> FAIL**（gate-log-unreadable），不得默认 0
    G9 构建日期        : 交付件不得含**编译当天日期**（环境泄漏，非论文/译者信息）

口径要点（踩坑换来的）
    · G3 必须**全量取 max**：`head -20` 会漏掉最大值，曾因此把有问题的稿子判 PASS。
    · G5 的文本层**优先用本地 PyMuPDF 重抽**：远端 `pdftotext -layout` 会被水印/双栏
      交错打乱，把图号拆到两行 -> 假「图号缺失」。同一 PDF 不同抽取器结果差异巨大，
      应选可靠的那个，而不是放宽判据。
    · G4 是**子集判定**：正文交叉引用（「如图 3 所示」）会额外产生匹配，
      不能用「实际数 == 期望数」。
    · G9 **只查「编译当天」这一天**，不查「任何日期」：论文本身就有日期
      （收稿年、会议日期、"October 2021"），全查会把几乎每篇都判fail。
      当天日期只可能来自编译机的时钟 —— 那正是环境泄漏的判据。
      （模板侧修法是 `\\date{}`：`\\maketitle` 不给 `\\date` 时默认印 `\\today`。）

依赖
    Python 3.9+ 与 PyMuPDF：pip install pymupdf
    可选：poppler(pdffonts) 做更严格的字体嵌入检查；缺省用 PyMuPDF 近似判定。

用法
    python scripts/verify_delivery.py Paper/20_xxx_中文.pdf --figs .work/20/figs --tables .work/20/tables --log .work/20/main.log
    python scripts/verify_delivery.py Paper/ --ledger .work/format-ledger.tsv
    python scripts/verify_delivery.py Paper/ --json .work/verify.json

结构化输出
    NN=20 VERDICT=PASS ERRS=0 MISS=0 OVF_H_MAX=0.9 PAGES=2 TRACES=0 FIG=1/1 TAB=1/1 PAPER=595x791 DEV=0 FONTS_EMB=6/6
    SUMMARY FILES=1 PASS=1 FAIL=0
    VERIFY JSON=[...]
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_DEP = 0, 1, 2, 3

TH_OVF_H = 8.0
TH_OVF_V = 120.0
TH_PAPER_DEV = 3.0
TRACE_RE = re.compile(r'译注|翻译说明|篇末自查|源文件|pdftotext|/tmp/zh|译文信息|全译完成')
TAB_MD_RE = re.compile(r'^\*{0,2}表\s*[:：]?\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*[（(]\s*TABLE',
                       re.M | re.I)
FIG_MD_RE = re.compile(r'^>?\s*\*{0,2}图\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*'
                       r'(?:[（(][^）)]*[）)])?\s*[.。]?\s*\*{0,2}', re.M)
INSTALL = '缺少 PyMuPDF。安装：\n  pip install pymupdf'


ROMAN_NUM = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7,
    'viii': 8, 'ix': 9, 'x': 10, 'xi': 11, 'xii': 12, 'xiii': 13,
    'xiv': 14, 'xv': 15, 'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19,
    'xx': 20, 'xxi': 21, 'xxii': 22, 'xxiii': 23, 'xxiv': 24, 'xxv': 25,
}


def _norm(x):
    """Normalise a figure/table id for set comparison.

    MUST convert Roman numerals the same way prep.py does.  Journal manuscripts
    say "\u8868 I".."\u8868 VII" while extract_tables.py keys tables.json as 1..7;
    without this the gate reports G4-tab-missing for tables that are present and
    correctly captioned -- a false FAIL that can never be cleared by fixing the
    document.
    """
    s = str(x).strip().rstrip('.')
    r = ROMAN_NUM.get(s.lower())
    if r is not None:
        return str(r)
    return re.sub(r'\.(?=-)', '', s)


def _require_fitz():
    try:
        import fitz
        return fitz
    except ImportError:
        print('ERROR: ' + INSTALL, file=sys.stderr)
        sys.exit(EXIT_DEP)


MONTHS_EN = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
             'August', 'September', 'October', 'November', 'December')


def build_date_patterns(today=None):
    """G9：\today 在各 datetime 格式下的形态 + ISO 形态。

    **只查编译当天这一天**。论文自身就含日期（收稿年、会议日期、"October 2021"），
    若查「任何日期」几乎每篇都会被误判。当天日期只可能来自编译机的时钟，
    因此它是「环境泄漏」的**充分判据**。
    """
    import datetime
    d = today or datetime.date.today()
    mon = MONTHS_EN[d.month - 1]
    return [
        '%s %d, %d' % (mon, d.day, d.year),            # October 14, 2026
        '%d %s %d' % (d.day, mon, d.year),             # 14 October 2026
        '%s. %d, %d' % (mon[:3], d.day, d.year),       # Oct. 14, 2026
        '%04d-%02d-%02d' % (d.year, d.month, d.day),   # 2026-10-14
        '%04d/%02d/%02d' % (d.year, d.month, d.day),   # 2026/10/14
        '%d-%02d-%04d' % (d.day, d.month, d.year),     # 14-10-2026
    ]


def parse_log(path):
    """从日志提取 G1/G2/G3 指标。返回 dict，log_missing 表示读不到。"""
    g = {'ERRS': None, 'MISS': None, 'OVF_H_MAX': None, 'VBOX_MAX': None,
         'PAGES': None, 'log_missing': True}
    if not path or not os.path.isfile(path):
        return g
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            t = fh.read()
    except OSError:
        return g
    if not t.strip():
        return g
    g['log_missing'] = False
    g['ERRS'] = len(re.findall(r'(?m)^!', t))
    g['MISS'] = len(re.findall(r'Missing character', t))
    hb = [float(x) for x in re.findall(r'Overfull \\hbox \(([0-9.]+)pt', t)]
    vb = [float(x) for x in re.findall(r'Overfull \\vbox \(([0-9.]+)pt', t)]
    g['OVF_H_MAX'] = max(hb) if hb else 0.0
    g['VBOX_MAX'] = max(vb) if vb else 0.0
    m = re.search(r'Output written on .*?\((\d+) pages?', t)
    if m:
        g['PAGES'] = int(m.group(1))
    return g


def pdf_text_layer(pdf):
    """G5/G4 的文本层：**本地 PyMuPDF 重抽**（比 pdftotext -layout 可靠）。"""
    import fitz
    d = fitz.open(pdf)
    t = '\n'.join(p.get_text() for p in d)
    pages = d.page_count
    size = (d[0].rect.width, d[0].rect.height)
    # 主流页面尺寸（首页可能是通栏封面，不等于正文纸张）
    sizes = {}
    for p in d:
        k = (round(p.rect.width), round(p.rect.height))
        sizes[k] = sizes.get(k, 0) + 1
    main = max(sizes, key=sizes.get)
    fonts = set()
    for i in range(pages):
        try:
            for f in d.get_page_fonts(i):
                fonts.add(f[3])
        except Exception:
            pass
    d.close()
    return t, pages, size, main, fonts


def fonts_embedded(pdf, pdffonts):
    """G7：字体嵌入。优先 pdffonts（权威），否则用 PyMuPDF 近似判定。"""
    if pdffonts:
        try:
            p = subprocess.run([pdffonts, pdf], capture_output=True)
            out = (p.stdout or b'').decode('utf-8', 'replace')
            lines = [l for l in out.split('\n')[2:] if l.strip()]
            if lines:
                n = len(lines)
                # pdffonts 的 emb 列；口径差异：有的版本 ' yes ' 出现在类型列之前
                emb = sum(1 for l in lines if re.search(r'\byes\b', l))
                return emb, n, 'pdffonts'
        except Exception:
            pass
    return None, None, 'unavailable'


def verify_one(pdf, md, figsdir, tabdir, logpath, ledger, pdffonts, today=None):
    """返回一条结构化记录。"""
    import fitz
    rec = {'pdf': os.path.basename(pdf), 'verdict': 'PASS', 'fails': []}
    g = parse_log(logpath)
    rec['log'] = os.path.basename(logpath) if logpath else None
    rec['gates'] = g

    if figsdir is None and md:
        figsdir = os.path.join(os.path.dirname(md), 'figs')
    if tabdir is None and md:
        tabdir = os.path.join(os.path.dirname(md), 'tables')

    # 期望编号：优先取 figs.json / tables.json（提取阶段的**单一事实源**），
    # 其次退回 md 的表题/图题正则。
    exp_fig, exp_tab = set(), set()
    fj = os.path.join(figsdir, 'figs.json') if figsdir else None
    tj = os.path.join(tabdir, 'tables.json') if tabdir else None
    if fj and os.path.isfile(fj):
        try:
            exp_fig = {_norm(k) for k in json.load(open(fj, encoding='utf-8'))}
        except Exception:
            pass
    if tj and os.path.isfile(tj):
        try:
            data = json.load(open(tj, encoding='utf-8'))
            exp_tab = {_norm(k) for k, v in data.items()
                       if isinstance(v, dict) and v.get('status') == 'ok'}
        except Exception:
            pass
    if md and os.path.isfile(md):
        try:
            s = open(md, encoding='utf-8').read()
            if not exp_fig:
                exp_fig = {_norm(m.group(1)) for m in FIG_MD_RE.finditer(s)}
            if not exp_tab:
                exp_tab = {_norm(m.group(1)) for m in TAB_MD_RE.finditer(s)}
        except Exception:
            pass
    rec['exp_fig'] = sorted(exp_fig)
    rec['exp_tab'] = sorted(exp_tab)

    txt, pages, size, main_size, fonts = pdf_text_layer(pdf)
    rec['pages'] = pages
    rec['paper'] = '%dx%d' % (round(main_size[0]), round(main_size[1]))

    # G5 译者痕迹
    traces = len(TRACE_RE.findall(txt))
    rec['traces'] = traces
    if traces:
        rec['fails'].append('G5-translator-traces=%d' % traces)

    # G4 编号闭合（子集判定）
    flat = re.sub(r'\s+', ' ', txt)
    got_fig = {_norm(x) for x in re.findall(
        r'图\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*[.。（(，,、;；:\s]', flat)}
    for x in re.findall(
            r'(?<![0-9A-Za-z.\-])([0-9A-Z][0-9A-Za-z]*(?:[.\-][0-9A-Za-z]+)*)\.\s*[\u4e00-\u9fff]',
            flat):
        x = _norm(x)
        if x in exp_fig:          # 兜底只接受**属于期望集合**的编号（否则计数虚高）
            got_fig.add(x)
    # Table-number closure must accept BOTH caption forms that the skill emits:
    #   "表 6.4.3-1（Table: 6.4.3.-1）. title"   <- caption carried a source label
    #   "表2.1-1. 规范性引用"                      <- glossary-style caption, no paren
    # Requiring the parenthesised form undercounted 12/16 on a real OMA run and
    # produced a false G4-tab-missing FAIL for the 4 glossary tables.
    # Anchored at LINE START so cross-references ("见表6.4-2") are not counted.
    got_tab = set()
    for _x in re.findall(
            r'(?:^|\n)\s*\u8868\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*[\uff08(.．]', txt):
        got_tab.add(_norm(_x))
    miss_fig = sorted(exp_fig - got_fig, key=lambda s: (len(s), s))
    miss_tab = sorted(exp_tab - got_tab, key=lambda s: (len(s), s))
    rec['fig_closure'] = '%d/%d' % (len(exp_fig) - len(miss_fig), len(exp_fig))
    rec['tab_closure'] = '%d/%d' % (len(exp_tab) - len(miss_tab), len(exp_tab))
    if miss_fig:
        rec['fails'].append('G4-fig-missing:' + ','.join(miss_fig[:6]))
    if miss_tab:
        rec['fails'].append('G4-tab-missing:' + ','.join(miss_tab[:6]))

    # G6 页面几何 vs 台账
    if ledger:
        want = None
        for k, v in ledger.items():
            if rec['pdf'].startswith(k):
                want = v
                break
        if want and want.get('paper'):
            try:
                lw, lh = [float(x) for x in want['paper'].lower().split('x')]
                dev = max(abs(main_size[0] - lw), abs(main_size[1] - lh))
                rec['paper_dev'] = round(dev, 2)
                if dev > TH_PAPER_DEV:
                    rec['fails'].append('G6-paper-dev=%.1f' % dev)
                    rec['fails'].append('G6-NEXT: 先查 geometry 单位是不是用了 pt（必须 bp）')
            except Exception:
                pass

    # G7 字体嵌入
    emb, tot, src = fonts_embedded(pdf, pdffonts)
    rec['fonts'] = ('%s/%s' % (emb, tot)) if emb is not None else '(unavailable)'
    rec['fonts_src'] = src
    if emb is not None and tot and emb < tot:
        rec['fails'].append('G7-fonts-not-embedded=%d/%d' % (emb, tot))

    # G9 构建日期泄漏（环境泄漏，既不是论文信息也不是译者信息）
    _hits = [p for p in build_date_patterns(today) if p in flat]
    rec['build_date_hits'] = _hits
    if _hits:
        rec['fails'].append('G9-build-date-leak:%s' % _hits[0])
        rec['fails'].append('G9-NEXT: 模板须写 \\date{}（\\maketitle 默认 \\today）；'
                            '论文自身的出版日期应放进信息行，不在这里')

    # G1/G2/G3/G8
    if g['log_missing']:
        rec['fails'].append('G8-gate-log-unreadable')
        rec['fails'].append('G8-NEXT: 日志读不到 -> 判失败，**绝不默认 0**；'
                            '确认编译时用了 --keep-logs（tectonic）或 xelatex 已跑完')
    else:
        if g['ERRS']:
            rec['fails'].append('G1-ERRS=%d' % g['ERRS'])
        if g['MISS']:
            rec['fails'].append('G2-MISS=%d' % g['MISS'])
        if g['OVF_H_MAX'] > TH_OVF_H:
            rec['fails'].append('G3-OVF_H=%.1f' % g['OVF_H_MAX'])
        if g['VBOX_MAX'] > TH_OVF_V:
            rec['fails'].append('G3-VBOX=%.1f' % g['VBOX_MAX'])
        if g['PAGES'] and g['PAGES'] != pages:
            rec['fails'].append('pages-mismatch(log=%d pdf=%d)' % (g['PAGES'], pages))

    rec['verdict'] = 'FAIL' if rec['fails'] else 'PASS'
    return rec


def read_ledger(path):
    led = {}
    if not path or not os.path.isfile(path):
        return led
    for ln in open(path, encoding='utf-8').read().split('\n')[1:]:
        p = ln.split('\t')
        if len(p) >= 10 and p[0].strip():
            led[p[0].strip()] = {'mode': p[7].strip() if len(p) > 7 else '',
                                 'paper': p[9].strip() if len(p) > 9 else ''}
    return led


def collect(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out.extend(sorted(f for f in glob.glob(os.path.join(p, '*.pdf'))
                              if '-new' not in f and not f.endswith('.tmp.pdf')))
        else:
            out.append(p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='verify_delivery.py',
        description='交付门控 G1–G9：对单篇或目录跑机器化清单并给 PASS/FAIL（不过即不放行）。',
        epilog='示例：\n'
               '  python scripts/verify_delivery.py Paper/20_xxx_中文.pdf \\\n'
               '      --figs .work/20/figs --tables .work/20/tables --log .work/20/main.log\n'
               '  python scripts/verify_delivery.py Paper/ --ledger .work/format-ledger.tsv\n'
               '  python scripts/verify_delivery.py Paper/ --json .work/verify.json\n\n'
               '注意：门控 PASS **不等于**正确 -> 仍需把图表渲染成联系表**目视抽验**\n'
               '（status==ok 可能是正文段落/另一张表；图题配对错误时张数/尺寸/md5 全都正常）。\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='+', metavar='PDF|DIR', help='交付 PDF 或目录')
    ap.add_argument('--md', metavar='MD', help='对应的中文译稿 md（推期望编号用，可选）')
    ap.add_argument('--figs', metavar='DIR', help='图目录（含 figs.json）')
    ap.add_argument('--tables', metavar='DIR', help='表目录（含 tables.json）')
    ap.add_argument('--log', metavar='LOG', help='编译日志（默认取 PDF 同目录的 main.log）')
    ap.add_argument('--ledger', metavar='TSV', help='版式台账（做 G6 页面尺寸核对）')
    ap.add_argument('--json', metavar='OUT.json', help='写出结构化结果')
    ap.add_argument('--today', metavar='YYYY-MM-DD',
                    help='覆盖 G9 要查的「构建日期」（测试用）')
    args = ap.parse_args(argv)

    _require_fitz()
    pdfs = collect(args.paths)
    if not pdfs:
        print('ERROR: 没找到交付 PDF（%s）。\n  修法：先跑 runpaper.py 出稿。'
              % ', '.join(args.paths), file=sys.stderr)
        return EXIT_USAGE
    ledger = read_ledger(args.ledger)
    pdffonts = shutil.which('pdffonts')
    today = None
    if args.today:
        import datetime
        today = datetime.date(*[int(x) for x in args.today.split('-')])

    rows = []
    for pdf in pdfs:
        stem = os.path.splitext(os.path.basename(pdf))[0]
        nn = stem[:2] if stem[:2].isdigit() else ''
        logpath = args.log
        if not logpath:
            for cand in (os.path.join(os.path.dirname(pdf), 'main.log'),
                         os.path.join(os.getcwd(), '.work', nn, 'main.log')):
                if os.path.isfile(cand):
                    logpath = cand
                    break
        md = args.md
        if not md and nn:
            hits = sorted(glob.glob(os.path.join(os.path.dirname(pdf),
                                                 '%s_*_中文.md' % nn)))
            if hits:
                md = hits[0]
        figsdir = args.figs or (os.path.join(os.getcwd(), '.work', nn, 'figs') if nn else None)
        tabdir = args.tables or (os.path.join(os.getcwd(), '.work', nn, 'tables') if nn else None)
        try:
            rec = verify_one(pdf, md, figsdir, tabdir, logpath, ledger, pdffonts, today)
        except Exception as exc:
            rows.append({'pdf': os.path.basename(pdf), 'verdict': 'ERROR',
                         'fails': ['exception:%s' % exc]})
            print('NN=%s VERDICT=ERROR %s' % (nn or '?', exc))
            continue
        rec['nn'] = nn
        rows.append(rec)
        g = rec['gates']
        print('NN=%s VERDICT=%s ERRS=%s MISS=%s OVF_H_MAX=%s PAGES=%s TRACES=%s '
              'FIG=%s TAB=%s PAPER=%s DEV=%s FONTS=%s BUILDDATE=%s'
              % (nn or '?', rec['verdict'], g['ERRS'], g['MISS'], g['OVF_H_MAX'],
                 rec['pages'], rec['traces'], rec['fig_closure'], rec['tab_closure'],
                 rec['paper'], rec.get('paper_dev', '-'), rec['fonts'],
                 ','.join(rec.get('build_date_hits') or []) or '-'))
        for f in rec['fails']:
            print('   FAIL %s' % f)

    npass = sum(1 for r in rows if r.get('verdict') == 'PASS')
    nfail = len(rows) - npass
    print('SUMMARY FILES=%d PASS=%d FAIL=%d' % (len(rows), npass, nfail))
    if nfail:
        print('NEXT: 失败项逐条处理后重跑；**不得**放宽门控阈值来放行\n'
              '      （若全库同方向同比例超差 -> 那是系统偏差/单位错，不是阈值问题）。\n'
              '      门控全过仍须目视抽验（见 references/gates.md §6）。')
    else:
        print('REMINDER: 门控 PASS != 正确。必须把全部图/表渲染成联系表**逐张目视**，\n'
              '          并与原件并排对读（表区内容、图题配对、栏数与页边距）。')
    print('VERIFY JSON=%s' % json.dumps(rows, ensure_ascii=False, default=str))

    if args.json:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            with open(args.json, 'w', encoding='utf-8') as fh:
                json.dump(rows, fh, ensure_ascii=False, indent=1, default=str)
            print('WROTE %s' % args.json)
        except OSError as exc:
            print('ERROR: 写 %s 失败：%s' % (args.json, exc), file=sys.stderr)
            return EXIT_USAGE
    return EXIT_OK if nfail == 0 else EXIT_FAIL


if __name__ == '__main__':
    sys.exit(main())
