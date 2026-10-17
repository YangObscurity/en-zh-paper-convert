#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""strip_meta.py -- 剥离译者痕迹，产出干净的正文排版源。

交付 PDF 必须**完全不含**译者信息（这是门控 G5，任何一项不过即不放行）：
    题头元信息表 / 翻译说明整节 / 篇末自查与译注汇总整块 / 行内〔译注…〕 /
    悬空引用（「其统一译法见翻译说明第 5 条。」）/ 进度标记 / 分隔线

**必须保留**（属论文自身元信息，由标题页模板渲染）：
    中文题 + 英文原题 + 作者/单位/联系/DOI/收稿日期/出处 + 摘要 + 关键词

⚠ 工作用 md 里的〔译注〕**不要**清（那是翻译方法与审计线索，属设计而非残留；
   实测 28/28 篇都有，共 487 条）。本脚本只处理**输出副本**，绝不改原始译稿。

依赖
    仅标准库（Python 3.9+）。

用法
    python scripts/strip_meta.py 译稿.md .work/01/clean.md
    python scripts/strip_meta.py 译稿.md clean.md --report .work/01/strip.json
    python scripts/strip_meta.py 译稿.md clean.md --keep-hr     # 保留 --- 分隔线

结构化输出
    REMOVED JSON={"duplicate_title": 3, "meta_table": 8, ...}
    WROTE clean.md IN=12345 OUT=9876
    TRACE-HITS=0
    SUMMARY IN_BYTES=12345 OUT_BYTES=9876 TRACE_HITS=0
"""
import argparse
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

EXIT_OK, EXIT_USAGE = 0, 2

# 交付 PDF 里不允许出现的关键词（与 verify_delivery.py / 门控 G5 同口径）
TRACE_RE = re.compile(r'译注|翻译说明|篇末自查|源文件|pdftotext|/tmp/zh|译文信息|全译完成')

# 裸行自查标记（无 # 标题，如「篇末自查」「译注汇总」）
BARE_MARK = re.compile(
    r'^[ \t]*\**\s*(篇末自查|译者自查|自查清单|交付自查|译后自查|质量自查|'
    r'结构与覆盖自查|译注汇总|译注清单)\s*\**\s*$')
# 自查/译注汇总类**标题**（带 # 层级）
HEAD_MARK = re.compile(
    r'^(#{1,6})\s*.*(?:篇末自查|自查清单|译者自查|交付自查|译后自查|质量自查|'
    r'结构与覆盖自查|译注汇总|译注清单)')


def strip(src, keep_hr=False):
    """返回 (输出文本, 统计字典)。"""
    lines = src.split('\n')
    out = []
    i, n = 0, len(lines)
    removed = {'duplicate_title': 0, 'meta_table': 0, 'notes_section': 0,
               'checklist': 0, 'checklist_bare': 0, 'progress': 0, 'hr': 0}
    while i < n:
        L = lines[i]

        # 原始译稿的双语标题由排版模板呈现，避免正文再重复一遍
        if i == 0 and re.match(r'^#\s+', L):
            removed['duplicate_title'] += 1
            i += 1
            while i < n and not lines[i].strip():
                removed['duplicate_title'] += 1; i += 1
            if i < n and lines[i].startswith('**') and lines[i].endswith('**'):
                removed['duplicate_title'] += 1; i += 1
            while i < n and not lines[i].strip():
                removed['duplicate_title'] += 1; i += 1
            continue

        # 题头元信息表（| 项 | 内容 |）整块删除
        if re.match(r'^\| 项 \| 内容 \|', L):
            j = i + 1
            while j < n and re.match(r'^\|', lines[j]):
                j += 1
            removed['meta_table'] += j - i
            i = j
            continue

        # 翻译说明标题及整个连续 quote 块
        if re.match(r'^>\s*(?:#{1,6}\s*)?翻译说明', L):
            j = i + 1
            while j < n and (re.match(r'^>', lines[j]) or not lines[j].strip()):
                j += 1
            removed['notes_section'] += j - i
            i = j
            continue

        # 兼容：标题格式变化时，以第 1 条翻译规则为锚点
        if re.match(r'^>\s*1\.\s*\*\*底本\*\*', L):
            j = i + 1
            while j < n and (re.match(r'^>', lines[j]) or not lines[j].strip()):
                j += 1
            removed['notes_section'] += j - i
            i = j
            continue

        # 裸行自查标记：同样按整节删除
        if BARE_MARK.match(L):
            j = i + 1
            while j < n and not re.match(r'^#{1,6}\s', lines[j]) \
                    and not re.match(r'^-{3,}\s*$', lines[j]):
                j += 1
            removed['checklist_bare'] += j - i
            i = j
            continue

        # 自查/译注汇总类整节：按**标题层级**整块删除
        # —— 删到同级或更高级标题 / --- / 文末。
        # （实测：某篇「## 附：自查清单」下带 ### 子节，若只删到第一个 ### 会漏，
        #   导致 traces>0。这是按层级整块删的原因。）
        m_chk = HEAD_MARK.match(L)
        if m_chk:
            lvl = len(m_chk.group(1))
            j = i + 1
            while j < n:
                m2 = re.match(r'^(#{1,6})\s', lines[j])
                if m2 and len(m2.group(1)) <= lvl:
                    break
                if re.match(r'^-{3,}\s*$', lines[j]):
                    break
                j += 1
            removed['checklist'] += j - i
            i = j
            continue

        # 译文完成状态/进度标记
        if re.match(r'^\*\*（[^）]*完成[^）]*）\*\*\s*$', L.strip()):
            removed['progress'] += 1
            i += 1
            continue

        # 全篇用于分隔翻译说明/自查清单的水平线
        if not keep_hr and re.match(r'^-{3,}\s*$', L):
            removed['hr'] += 1
            i += 1
            continue

        out.append(L)
        i += 1

    body = '\n'.join(out)
    # 行内译者注；不删方括号参考文献、数学符号等原文内容
    body, n_sub = re.subn(r'〔[^〕]*〕', '', body)
    removed['inline_notes'] = n_sub
    # 行内跨引译者节（如「（详见文首「翻译说明」第 7 条）」）
    body = re.sub(r'[（(]\s*详见[^）)]{0,40}?(?:翻译说明|译注清单|自查清单)[^）)]*[）)]', '', body)
    # 全角括号内**纯星号串**（（*）（**）（***））是脚注符号，不是 markdown 强调 -> 转义
    # （pandoc 会跨行把它们配成强调符，渲染出「（\emph{）」这类坏输出）
    body = re.sub(r'（(\*+)）', lambda m: '（' + r'\*' * len(m.group(1)) + '）', body)
    # 悬空引用译者节（该节已被删除，正文不应再指向它）
    body = re.sub(
        r'[^。；\n]{0,24}见\s*[「『]?\s*(?:翻译说明|译注清单|自查清单)\s*[」』]?\s*第\s*[0-9]+\s*条[。]?',
        '。', body)
    body = re.sub(r'  +', ' ', body)
    body = '\n'.join(l.rstrip() for l in body.split('\n'))
    # 删除因移除译注而变空的残行（如 "> ****"、">"、"**"）
    body = re.sub(r'^[ \t]*>[ \t]*\**[ \t]*$', '', body, flags=re.M)
    body = re.sub(r'^[ \t]*\**[ \t]*$', '', body, flags=re.M)
    body = re.sub(r'\n{3,}', '\n\n', body).strip() + '\n'
    return body, removed


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='strip_meta.py',
        description='剥离译者痕迹（元信息表/翻译说明/篇末自查/译注/进度标记/分隔线），产出干净排版源。',
        epilog='示例：\n'
               '  python scripts/strip_meta.py 译稿.md .work/01/clean.md\n'
               '  python scripts/strip_meta.py 译稿.md clean.md --report .work/01/strip.json\n'
               '  python scripts/strip_meta.py 译稿.md clean.md --keep-hr\n\n'
               '注意：本脚本只写输出副本，**不修改**原始译稿（译注是审计线索，应保留）。\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src', metavar='译稿.md', help='原始译稿 markdown')
    ap.add_argument('dst', metavar='clean.md', help='输出路径（干净排版源）')
    ap.add_argument('--report', metavar='JSON', help='把剥离统计写成 JSON')
    ap.add_argument('--keep-hr', action='store_true', help='保留 --- 水平分隔线')
    args = ap.parse_args(argv)

    if not os.path.isfile(args.src):
        print('ERROR: 找不到 %s。\n  修法：确认译稿路径（通常是 <NN>_<stub>_中文.md）。' % args.src,
              file=sys.stderr)
        return EXIT_USAGE
    if os.path.abspath(args.src) == os.path.abspath(args.dst):
        print('ERROR: 输入与输出是同一个文件。\n'
              '  修法：输出写成另一个路径（如 .work/<NN>/clean.md）——'
              '绝不能就地覆盖原始译稿。', file=sys.stderr)
        return EXIT_USAGE

    try:
        with open(args.src, encoding='utf-8') as fh:
            src = fh.read()
    except UnicodeDecodeError as exc:
        print('ERROR: %s 不是 UTF-8：%s\n  修法：先转码为 UTF-8 再重跑。' % (args.src, exc),
              file=sys.stderr)
        return EXIT_USAGE

    body, removed = strip(src, keep_hr=args.keep_hr)
    trace_hits = len(TRACE_RE.findall(body))

    d = os.path.dirname(os.path.abspath(args.dst))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(args.dst, 'w', encoding='utf-8') as fh:
        fh.write(body)

    print('REMOVED JSON=%s' % json.dumps(removed, ensure_ascii=False, sort_keys=True))
    print('WROTE %s IN=%d OUT=%d' % (args.dst, len(src), len(body)))
    print('TRACE-HITS=%d' % trace_hits)
    print('SUMMARY IN_BYTES=%d OUT_BYTES=%d TRACE_HITS=%d' % (len(src), len(body), trace_hits))
    if trace_hits:
        for m in TRACE_RE.finditer(body):
            s = max(0, m.start() - 40)
            print('  HIT ...%s...' % body[s:m.end() + 40].replace('\n', ' '))
        print('NEXT: 仍有译者痕迹 -> 该处措辞不在剥离规则内。\n'
              '  修法：把该措辞加进 strip_meta.py 的 TRACE/规则（若属译者元信息），\n'
              '        或在译稿里改写；**不要**靠放宽门控 G5 放行。')
        return 1
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
