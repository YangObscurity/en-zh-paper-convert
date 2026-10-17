#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_env.py -- P0 环境预检：一次确认所有依赖，缺什么就给**安装命令**。

检查项
    必需  Python >= 3.9
    必需  PyMuPDF（fitz）        —— 图题/表题定位、门控文本层重抽
    必需  pandoc                —— markdown -> LaTeX
    必需  编译引擎（二选一）    —— tectonic（默认）或 远端 xelatex
    必需  中文字体（至少一个）  —— 否则中文印不出来
    推荐  poppler: pdftoppm / pdfimages / pdffonts / pdfinfo
    推荐  qpdf                  —— xref 损坏原件先归一化

行为
    缺**必需**项 -> 打印安装命令并 exit 3（调用方应中止后续流程）。
    缺**推荐**项 -> 打印警告但 exit 0（部分能力降级，仍可出稿）。
    绝不交互（agent 无法应答）：全部靠参数 + 环境探测。

依赖
    仅标准库。

用法
    python scripts/check_env.py
    python scripts/check_env.py --backend ssh-xelatex --ssh-host me@box:2222
    python scripts/check_env.py --probe-tectonic        # 真编译一个最小中文文档验证链路
    python scripts/check_env.py --json .work/env.json

结构化输出
    OK   python      3.14.7
    OK   pymupdf     1.27.2
    MISS pandoc      <安装命令>
    READY=yes|no     MISSING_REQUIRED=...  MISSING_OPTIONAL=...
    SUMMARY REQUIRED_MISSING=1 OPTIONAL_MISSING=0 READY=no
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _backend  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_DEPS = 0, 2, 3

# 与 runpaper.py / _backend.py 保持一致的字体候选
CJK_CANDIDATES = ['Noto Serif SC', 'Noto Serif CJK SC', 'Source Han Serif SC',
                  'SimSun', 'Songti SC', 'AR PL UMing CN', 'WenQuanYi Zen Hei']
CJK_FILES = ['notoserifsc', 'notoserifcjksc', 'sourcehanserif', 'simsun',
             'songti', 'uming', 'wqyzenhei', 'msyh', 'simhei']

INSTALL = {
    'python': '需要 Python 3.9+：https://www.python.org/downloads/',
    'pymupdf': 'pip install pymupdf',
    'pandoc': ('winget install --id JohnMacFarlane.Pandoc -e   # Windows\n'
               '  brew install pandoc                              # macOS\n'
               '  apt-get install -y pandoc                        # Debian/Ubuntu'),
    'tectonic': _backend.INSTALL_HINTS['tectonic'],
    'xelatex': _backend.INSTALL_HINTS['xelatex'],
    'poppler': ('winget install oschwartz10612.Poppler   # Windows\n'
                '  brew install poppler                    # macOS\n'
                '  apt-get install -y poppler-utils        # Debian/Ubuntu'),
    'qpdf': ('winget install qpdf        # Windows\n'
             '  brew install qpdf        # macOS\n'
             '  apt-get install -y qpdf  # Debian/Ubuntu'),
    'ssh': _backend.INSTALL_HINTS['ssh'],
    'cjk-font': ('Windows: 自带 SimSun / Noto Serif SC（或装 Noto Serif CJK SC）\n'
                 '  Linux:   apt-get install -y fonts-noto-cjk\n'
                 '  macOS:   自带 Songti SC/PingFang SC'),
}


def has_cjk_font():
    """探测本机是否有可用的中文字体（用于 xeCJK）。"""
    cands = _backend.system_font_names()
    if not cands:
        return None, []          # 无法探测
    found = []
    for want in CJK_FILES:
        for have in cands:
            if want in have:
                found.append(have)
                break
    return bool(found), found[:5]


def probe_tectonic(binpath):
    """真编译一个最小中文文档，验证链路（而不仅是「文件存在」）。"""
    import tempfile
    d = tempfile.mkdtemp(prefix='paper-skill-probe-')
    with open(os.path.join(d, 'main.tex'), 'w', encoding='utf-8') as fh:
        fh.write(_backend.MINIMAL_TEX)
    cmd = [binpath, os.path.join(d, 'main.tex'), '--outdir', d,
           '--keep-logs', '--keep-intermediates']
    try:
        p = subprocess.run(cmd, cwd=d, capture_output=True, timeout=900)
    except Exception as exc:
        return False, 'probe failed: %s' % exc
    pdf = os.path.join(d, 'main.pdf')
    log = os.path.join(d, 'main.log')
    if p.returncode != 0 or not os.path.isfile(pdf):
        err = ((p.stderr or b'') + (p.stdout or b'')).decode('utf-8', 'replace')
        return False, ('\u7f16\u8bd1\u5931\u8d25\uff08rc=%d\uff09\n'
                       '  \u5e38\u89c1\u539f\u56e0\uff1a\u9996\u6b21\u7f16\u8bd1\u9700\u8054\u7f51'
                       '\u4e0b\u8f7d\u5b8f\u5305\uff1b\u6216\u4e2d\u6587\u5b57\u4f53\u4e0d\u53ef\u7528\u3002\n'
                       '  \u4fee\u6cd5\uff1a\u5148\u5728\u8054\u7f51\u73af\u5883\u8dd1\u4e00\u6b21 '
                       'tectonic main.tex\uff0c\u6216\u6539\u6210 ssh-xelatex \u540e\u7aef\u3002\n'
                       '  \u5b8c\u6574\u8f93\u51fa\uff1a%s' % (p.returncode, err[-500:]))
    return True, ('\u7f16\u8bd1\u6210\u529f\uff1apages/log \u5df2\u751f\u6210 '
                  '(%s)' % ('main.log \u5b58\u5728' if os.path.isfile(log) else '\u65e0\u65e5\u5fd7'))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='check_env.py',
        description='P0 环境预检：确认 Python/PyMuPDF/pandoc/编译引擎/中文字体，缺则给安装命令。',
        epilog='示例：\n'
               '  python scripts/check_env.py\n'
               '  python scripts/check_env.py --backend ssh-xelatex --ssh-host me@box:2222\n'
               '  python scripts/check_env.py --probe-tectonic\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--probe-tectonic', action='store_true',
                    help='真编译一个最小中文文档验证链路（首次会联网下载宏包）')
    ap.add_argument('--json', metavar='OUT.json', help='写出结构化结果')
    _backend.add_backend_args(ap)
    args = ap.parse_args(argv)

    report = {'required': [], 'optional': [], 'missing_required': [], 'missing_optional': []}

    def need(name, ok, detail='', fix=None):
        tag = 'OK  ' if ok else 'MISS'
        print('%s %-12s %s' % (tag, name, detail or ('-' if ok else INSTALL.get(fix or name, ''))))
        report['required'].append({'name': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            report['missing_required'].append(name)
            if fix or name in INSTALL:
                print('     FIX: %s' % INSTALL.get(fix or name, '').replace('\n', '\n          '))

    def opt(name, ok, detail=''):
        tag = 'OK  ' if ok else 'WARN'
        print('%s %-12s %s' % (tag, name, detail or ('-' if ok else INSTALL.get(name, ''))))
        report['optional'].append({'name': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            report['missing_optional'].append(name)

    # 1) Python 版本
    v = sys.version_info
    need('python', v >= (3, 9), '%d.%d.%d' % (v[0], v[1], v[2]), 'python')

    # 2) PyMuPDF
    try:
        import fitz
        need('pymupdf', True, getattr(fitz, '__version__', '?'))
    except ImportError:
        need('pymupdf', False, '', 'pymupdf')

    # 3) pandoc
    pd = shutil.which('pandoc')
    ver = ''
    if pd:
        try:
            p = subprocess.run([pd, '--version'], capture_output=True, timeout=30)
            ver = (p.stdout or b'').decode('utf-8', 'replace').split('\n')[0]
        except Exception:
            ver = pd
    need('pandoc', bool(pd), ver or '', 'pandoc')
    report['pandoc'] = pd

    # 4) 编译引擎（按后端）
    try:
        be = _backend.backend_from_args(args)
    except _backend.BackendError as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return EXIT_USAGE
    report['backend'] = be.name
    print('-- \u540e\u7aef\uff1a%s' % be.name)
    if isinstance(be, _backend.SshXelatexBackend):
        need('ssh', bool(be.ssh_bin), be.ssh_bin or '', 'ssh')
        if not be.host:
            need('ssh_host', False, '\u672a\u914d\u7f6e',
                 'ssh_host') if 'ssh_host' in INSTALL else None
            print('MISS ssh_host     \u672a\u914d\u7f6e\n'
                  '     FIX: \u8bbe PAPER_SSH_HOST=user@host[:port] \u6216 --ssh-host\uff1b'
                  '\u6216\u6539\u7528\u9ed8\u8ba4\u540e\u7aef local-tectonic')
            report['missing_required'].append('ssh_host')
        else:
            print('OK   ssh_target   %s' % be.host)
            # 远端引擎探测（只读命令，不交互）
            try:
                p = be._ssh('command -v xelatex || which xelatex; echo __DONE__', timeout=60)
                out = (p.stdout or b'').decode('utf-8', 'replace')
                ok = '__DONE__' in out and ('xelatex' in out)
                need('remote-xelatex', ok,
                     out.split('__DONE__')[0].strip() or '\u672a\u627e\u5230',
                     'xelatex')
            except Exception as exc:
                need('remote-xelatex', False, '\u8fde\u63a5\u5931\u8d25\uff1a%s' % exc, 'xelatex')
    else:
        tb = be.bin
        need('tectonic', bool(tb), tb or '', 'tectonic')
        if tb and args.probe_tectonic:
            ok, detail = probe_tectonic(tb)
            need('probe-compile', ok, detail, 'tectonic')

    # 5) 中文字体
    found, sample = has_cjk_font()
    if found is None:
        opt('cjk-font', True, '\u65e0\u6cd5\u679a\u4e3e\u7cfb\u7edf\u5b57\u4f53 -> '
                              '\u5c06\u7531\u7f16\u8bd1\u65e5\u5fd7\u66b4\u9732\u7f3a\u5b57\u5f62\u95ee\u9898')
    else:
        need('cjk-font', found,
             ('\u53d1\u73b0\uff1a' + ', '.join(sample)) if found else '',
             'cjk-font')

    # 6) 可选：poppler / qpdf
    for tool in ('pdftoppm', 'pdfimages', 'pdffonts', 'pdfinfo'):
        opt(tool, bool(shutil.which(tool)), shutil.which(tool) or '', )
    opt('qpdf', bool(shutil.which('qpdf')), shutil.which('qpdf') or '')

    missing_req = report['missing_required']
    missing_opt = report['missing_optional']
    print('MISSING_REQUIRED=%s' % (','.join(missing_req) or 'none'))
    print('MISSING_OPTIONAL=%s' % (','.join(missing_opt) or 'none'))
    print('READY=%s' % ('no' if missing_req else 'yes'))
    print('SUMMARY REQUIRED_MISSING=%d OPTIONAL_MISSING=%d READY=%s'
          % (len(missing_req), len(missing_opt), 'no' if missing_req else 'yes'))
    if missing_req:
        print('NEXT: \u5148\u88c5\u9f50\u4e0a\u9762 MISS \u7684**\u5fc5\u9700**\u9879'
              '\uff08\u5df2\u7ed9\u51fa\u5b89\u88c5\u547d\u4ee4\uff09\u518d\u8dd1\u6d41\u6c34\u7ebf\u3002\n'
              '      \u7f3a\u63a8\u8350\u9879\u4e0d\u963b\u65ad\uff0c\u4f46\u80fd\u529b\u4f1a\u964d\u7ea7'
              '\uff08\u5982\u7f3a pdftoppm -> \u56fe\u53ea\u80fd\u8d70\u539f\u751f\u4f4d\u56fe\u5206\u6d41\uff09\u3002')
    else:
        print('READY: \u73af\u5883\u53ef\u7528\u3002\u4e0b\u4e00\u6b65\uff1a'
              'detect_format.py \u53d6\u7248\u5f0f\u5019\u9009 -> **\u76ee\u89c6\u5ba1\u8ba1** -> '
              '\u767b\u8bb0\u53f0\u8d26\u3002')

    report['ready'] = not missing_req
    if args.json:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            with open(args.json, 'w', encoding='utf-8') as fh:
                json.dump(report, fh, ensure_ascii=False, indent=1)
            print('WROTE %s' % args.json)
        except OSError as exc:
            print('ERROR: \u5199 %s \u5931\u8d25\uff1a%s' % (args.json, exc), file=sys.stderr)
            return EXIT_USAGE
    return EXIT_OK if not missing_req else EXIT_DEPS


if __name__ == '__main__':
    sys.exit(main())
