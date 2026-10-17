import sys as _sys
if '--help' in _sys.argv or '-h' in _sys.argv:
    print('Inspect scripts/prep.py: syntax, size, and comment-swallowed-code.\nUsage: python scripts/_audit_prep.py')
    _sys.exit(0)
import ast, re, sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
p = os.path.join(HERE, 'prep.py')
if not os.path.exists(p):
    print('MISSING', p); sys.exit(1)
src = open(p, encoding='utf-8').read()
print('file: %s  bytes=%d  lines=%d' % (p, len(src.encode('utf-8')), len(src.split('\n'))))

try:
    ast.parse(src)
    print('SYNTAX: OK')
except SyntaxError as e:
    print('SYNTAX: ERROR %s at line %s' % (e.msg, e.lineno))

MARKS = '\u93c1\u951b\u9225\u7039\u7026\u5a03'
mo = re.findall('[' + MARKS + ']', src)
print('mojibake chars: %d' % len(mo))
for m in re.finditer('[' + MARKS + ']', src):
    ln = src[:m.start()].count('\n') + 1
    print('   L%d: %s' % (ln, src.split('\n')[ln - 1].strip()[:100]))

# table caption regex sanity: must match both conventions
try:
    spec = {}
    exec(compile(src.split('def ')[0], '<head>', 'exec'), spec)
except Exception:
    spec = {}

# find the table-caption regexes by name
names = re.findall(r'^(RE_TAB\w*)\s*=\s*re\.compile', src, re.M)
print('table regex consts found:', names)
for n in names:
    mm = re.search(r'^' + n + r"\s*=\s*re\.compile\((r?'.*?'|r?\".*?\")\)", src, re.M)
    if not mm:
        continue
    try:
        rx = eval(mm.group(1))
        for sample in ['**表 7.3-4（Table: 7.3.-4）. title**',
                       '（表：3.2.-1 定义 Definitions）']:
            print('   %-10s match(%-46s) = %s' % (n, sample[:44], bool(rx.match(sample))))
    except Exception as e:
        print('   %-10s build/eval failed: %s' % (n, e))
