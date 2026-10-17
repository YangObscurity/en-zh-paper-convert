"""Detect 'comment swallowed the next code line' damage from PowerShell ANSI round-trips.

Symptom: a Chinese comment gets mojibake'd AND merged with the following code line,
so that code becomes part of the comment -> file still parses (hard to notice)
but functionality is silently lost.

ASCII-only on purpose: writing non-ASCII here via PowerShell corrupts the file
(the very bug this script detects).
"""

import sys as _sys
if '--help' in _sys.argv or '-h' in _sys.argv:
    print(__doc__.strip() if __doc__ else 'internal audit helper')
    _sys.exit(0)

import re, sys, glob, os

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Mojibake markers (UTF-8 Chinese read as GBK/ANSI produces these)
MARKERS = ('\u93c1\u677f', '\u951b', '\u9225', '\u7039\u6a3b')
CODE_AFTER_HASH = re.compile(
    r'#.*?(?:\bif\b|\bfor\b|\belif\b|\belse\b|\breturn\b|\bimport\b|\bdef\b)')

HERE = os.path.dirname(os.path.abspath(__file__))

bad = []
for f in sorted(glob.glob(os.path.join(HERE, '*.py'))):
    if os.path.basename(f).startswith('_audit'):
        continue
    for i, line in enumerate(open(f, encoding='utf-8').read().split('\n'), 1):
        s = line.strip()
        if not s.startswith('#'):
            continue
        if any(m in line for m in MARKERS) and CODE_AFTER_HASH.search(line):
            bad.append((f, i, line[:120]))

if bad:
    print('!! FOUND comment-swallowed-code damage:')
    for f, i, l in bad:
        print('  %s:%d  %s' % (f, i, l))
    print('\nFix: break the line after the comment, restore the swallowed code,')
    print('     and rewrite the comment in ASCII.')
    sys.exit(1)
print('OK: no comment-swallowed-code damage')
