#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_workspace.py -- 工作区根目录解析（确定性的，不依赖 cwd）。

背景
    本 skill 在仓库中位于 `skills/en-zh-paper-convert/`（目录名必须等于
    frontmatter 的 `name`，这是 agentskills.io 规范的硬要求），而运行时数据
    （`Paper/`、`.work/`、`out/`）在**仓库根**。用户单独拷走 skill 目录时，
    数据目录则应当在 skill 目录里就地创建。

解析顺序（先到先得）
    1. 环境变量 PAPER_SKILL_ROOT（显式指定，最高优先级）
    2. 从 skill 目录向上，最近的、包含 `Paper/` 子目录的祖先
    3. 否则：最近的、包含 `.git` 的祖先
    4. 否则：skill 目录本身（独立拷贝场景）
    显式传入的 --root 仍然优先于以上全部。

用法
    from _workspace import resolve_workspace_root
    ROOT = resolve_workspace_root(os.path.dirname(os.path.abspath(__file__)))
"""
import os

ENV_VAR = 'PAPER_SKILL_ROOT'


def _nearest_ancestor_with(start, marker):
    """从 start 向上找最近的、含 marker 的祖先目录；含 start 本身。"""
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, marker)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:            # 到达盘符根
            return None
        cur = parent


def resolve_workspace_root(skill_dir, env=None, start=None):
    """解析运行时工作区根目录。

    skill_dir: skill 根目录（即含 SKILL.md 的那一级）
    env:       环境变量映射，默认 os.environ（便于测试注入）
    start:     向上查找的起点，默认 skill_dir（便于测试注入）
    """
    env = os.environ if env is None else env
    explicit = (env.get(ENV_VAR) or '').strip()
    if explicit:
        return os.path.abspath(explicit)

    base = os.path.abspath(skill_dir if start is None else start)

    # 先看 skill 目录自身：单独拷走的 skill 若已就地建了 Paper/，就地即根。
    if os.path.isdir(os.path.join(base, 'Paper')):
        return base

    hit = _nearest_ancestor_with(base, 'Paper')
    if hit:
        return hit

    hit = _nearest_ancestor_with(base, '.git')
    if hit:
        return hit

    return base


def skill_dir_of(script_file):
    """由某个脚本的 __file__ 推出 skill 根目录（scripts/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(script_file)))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog='_workspace.py',
        description='打印工作区根目录解析结果（排查「数据目录跑哪去了」用）。',
        epilog='解析顺序：PAPER_SKILL_ROOT > 最近的含 Paper/ 的祖先 > '
               '最近的含 .git 的祖先 > skill 目录本身。\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--skill-dir', default=None, metavar='DIR',
                    help='skill 根目录（默认取本脚本的上一级）')
    ap.add_argument('--show', action='store_true', help='打印解析过程与结果')
    args = ap.parse_args(argv)

    skill = os.path.abspath(args.skill_dir) if args.skill_dir else skill_dir_of(__file__)
    root = resolve_workspace_root(skill)
    print('SKILL_DIR=%s' % skill)
    print('PAPER_SKILL_ROOT=%s' % (os.environ.get(ENV_VAR) or '(unset)'))
    print('WORKSPACE_ROOT=%s' % root)
    print('  Paper/ exists here: %s' % os.path.isdir(os.path.join(root, 'Paper')))
    print('  .git  exists here: %s' % os.path.isdir(os.path.join(root, '.git')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
