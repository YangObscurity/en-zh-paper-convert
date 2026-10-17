import sys as _sys
if '--help' in _sys.argv or '-h' in _sys.argv:
    print('Audit scripts/*.py for encoding damage (BOM / non-UTF-8 / mojibake).\nUsage: python scripts/_audit_encoding.py')
    _sys.exit(0)
import ast, glob, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))

print('=== 全部 scripts/*.py 语法与编码体检 ===')
bad = []
for f in sorted(glob.glob(os.path.join(HERE, '*.py'))):
    b = open(f, 'rb').read()
    flags = []
    if b[:3] == b'\xef\xbb\xbf':
        flags.append('BOM!')
    try:
        src = b.decode('utf-8')
    except UnicodeDecodeError as e:
        flags.append('非UTF8:' + str(e))
        src = None
    if src is not None:
        try:
            ast.parse(src)
        except SyntaxError as e:
            flags.append('SYNTAX:%s@L%s' % (e.msg, e.lineno))
        # 检查常见 PowerShell 损坏迹象（mojibake 中文），忽略 audit 脚本自带的特征字面量
        if not os.path.basename(f).startswith('_audit'):
            if any(t in src for t in ('鏁板', '锛', '鈥', '\ufeff')):
                flags.append('MOJIBAKE')
    status = 'OK' if not flags else ' '.join(flags)
    if flags:
        bad.append(f)
    print('%-28s %8d B  %s' % (os.path.basename(f), len(b), status))

print('\n有问题的文件:', bad if bad else '无 ✓')
sys.exit(1 if bad else 0)
