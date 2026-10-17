import sys as _sys
if '--help' in _sys.argv or '-h' in _sys.argv:
    print('Report mojibake string literals inside scripts/*.py (they would leak\ninto deliverables).\nUsage: python scripts/_audit_mojibake_literals.py')
    _sys.exit(0)
import re, sys, glob, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 已知 mojibake 标记：UTF-8 中文被当 GBK 解读后的典型字符
MARKS = ['\u93c1', '\u951b', '\u9225', '\u7039', '\u7026', '\u5a03',
         '\ue5e5', '\ufffd']

HERE = os.path.dirname(os.path.abspath(__file__))

print('=== scripts/*.py 中的 mojibake 字面量（会污染交付件）===')
total = 0
for f in sorted(glob.glob(os.path.join(HERE, '*.py'))):
    if '_audit' in f:
        continue
    lines = open(f, encoding='utf-8').read().split('\n')
    for i, L in enumerate(lines, 1):
        # 只看会被渲染进 PDF 的字符串字面量（含引号的行）
        if not ("'" in L or '"' in L):
            continue
        hits = [m for m in MARKS if m in L]
        if hits:
            total += 1
            print('  %s:%d  %s' % (f, i, L.strip()[:100]))
print('\n合计可疑行:', total)
if total:
    print('\n这些字符是「渲染成功的乱码」—— Missing character=0 抓不到。')
