#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_backend.py -- 执行后端抽象：local-tectonic（默认） / ssh-xelatex（可选）。

被 runpaper.py / build_paper.py / verify_delivery.py 共用。不是命令行工具，
但它自己也能当工具跑（--help / --selftest），方便在 agent 会话里直接验证链路。

依赖
    Python 3.9+，仅标准库。
    实际编译还需要：
      local-tectonic : tectonic 可执行文件（见 TECTONIC_BIN / 环境变量 PATH）
      ssh-xelatex    : 本机 ssh 客户端 + 远端装有 xelatex / pandoc / pdfinfo

配置优先级（从低到高）
    1. 内置默认值（本文件 DEFAULT_* 常量）
    2. JSON 配置文件：<skill>/backend.json 或 --config 指定
    3. 环境变量：PAPER_SKILL_BACKEND / TECTONIC_BIN / PAPER_SSH_HOST ...
    4. 命令行参数（由调用方负责，本模块只提供 from_args 助手）

用法
    python scripts/_backend.py --help
    python scripts/_backend.py --show                 # 打印生效配置与探针结果
    python scripts/_backend.py --selftest DIR         # 在 DIR 里编译一个最小中文文档
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

try:  # 中文 Windows 控制台默认 GBK，打印非 ASCII 会 UnicodeEncodeError
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Workspace root (holds Paper/ .work/ out/, and in this repo also tools/).
# Imported defensively: _backend.py must stay importable on its own.
try:
    from _workspace import resolve_workspace_root
    WORKSPACE_ROOT = resolve_workspace_root(SKILL_ROOT)
except Exception:                                    # pragma: no cover
    WORKSPACE_ROOT = SKILL_ROOT


def _tools_dirs():
    """Directories to search for bundled tools, most specific first."""
    out = []
    for d in (os.path.join(SKILL_ROOT, 'tools'),
              os.path.join(WORKSPACE_ROOT, 'tools')):
        d = os.path.abspath(d)
        if d not in out:
            out.append(d)
    return out

# ---------------------------------------------------------------- 安装指引
INSTALL_HINTS = {
    'tectonic': (
        'tectonic 未找到。任选一种安装方式：\n'
        '  winget install --id TectonicProject.Tectonic -e\n'
        '  scoop install tectonic\n'
        '  cargo install tectonic\n'
        '  或从 https://github.com/tectonic-typesetting/tectonic/releases 下载单文件 exe，\n'
        '  然后设置环境变量 TECTONIC_BIN=<exe 路径> 或把它放进 PATH。\n'
        '  注意：首次编译会联网下载 TeX 宏包（约 100 MB），需可访问网络；\n'
        '  离线环境请先在线编译一次，之后可加 --only-cached。'
    ),
    'pandoc': (
        'pandoc 未找到（markdown -> LaTeX 必需）。任选一种：\n'
        '  winget install --id JohnMacFarlane.Pandoc -e\n'
        '  choco install pandoc\n'
        '  或从 https://github.com/jgm/pandoc/releases 下载后放进 PATH。'
    ),
    'ssh': (
        'ssh 未找到（ssh-xelatex 后端必需）。\n'
        '  Windows: 设置 -> 应用 -> 可选功能 -> 添加「OpenSSH 客户端」。'
    ),
    'xelatex': (
        'xelatex 未找到（ssh-xelatex 后端的远端需要它）。\n'
        '  Ubuntu/Debian: apt-get install -y texlive-xetex texlive-lang-chinese\n'
        '  或改装 tectonic，并把 backend 设为 local-tectonic。'
    ),
}

DEFAULT_TECTONIC_ARGS = ['--keep-logs', '--keep-intermediates']
DEFAULT_SSH_TIMEOUT = 900

# ------------------------------------------------------------ tectonic 缓存
# WHY THIS EXISTS (measured):
# tectonic resolves its cache through the Windows app-data / known-folder API to
#     $HOME\AppData\Local\TectonicProject
# which is normally fine.  But when that directory is NOT writable -- e.g. the
# process runs inside a file sandbox that only allows writes under the workspace
# -- tectonic dies while WRITING ITS FORMAT CACHE:
#     error: cannot write format file texput.fmt
#     caused by: 拒绝访问 (os error 5) at path "...\TectonicProject\Tectonic\formats\format_XXXX"
# and the pipeline then reports `compile ok=False rc=1` followed by
# `GATE-FAIL: gate-log-unreadable; no-output-pdf`.  A two-line minimal document
# fails identically, so this is an ENVIRONMENT problem, not a document problem.
#
# `TECTONIC_CACHE_DIR` is present in the 0.15.0 binary but is NOT honoured for
# that format write, and `--only-cached` does not help either (the format still
# has to be created).  What DOES work is redirecting the app-data environment
# variables to a writable directory.  So: probe the default location, and only
# when it is unusable fall back to a workspace-local cache, seeded from the
# user's existing cache so the ~100 MB bundle is not re-downloaded.
TECTONIC_APPDATA_SUBDIR = os.path.join('AppData', 'Local', 'TectonicProject')
_TECTONIC_CACHE_CACHE = {}


def _is_writable_dir(path):
    """True only if we can really create and delete a file in `path`.

    Deliberately a live probe, not an ACL inspection: ACLs do not tell you what
    the effective token can do, and this project has already been burned once by
    inferring permissions instead of testing them.
    """
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, '.write_probe_%d' % os.getpid())
        with open(probe, 'w') as fh:
            fh.write('x')
        os.remove(probe)
        return True
    except Exception:
        return False


def _default_tectonic_cache_root():
    """Where tectonic will put its cache if we change nothing."""
    home = os.environ.get('USERPROFILE') or os.environ.get('HOME')
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA')
        if local:
            return os.path.join(local, 'TectonicProject')
        if home:
            return os.path.join(home, 'AppData', 'Local', 'TectonicProject')
        return None
    xdg = os.environ.get('XDG_CACHE_HOME')
    if xdg:
        return os.path.join(xdg, 'Tectonic')
    if home:
        return os.path.join(home, '.cache', 'Tectonic')
    return None


def _cache_file_count(path):
    """How many files a candidate cache root holds (0 when unreadable)."""
    n = 0
    try:
        for _, _, files in os.walk(path):
            n += len(files)
    except OSError:
        return 0
    return n


def _tectonic_cache_sources():
    """Existing caches we may copy from, RICHEST FIRST.

    The primary source is the normal location.  We also consider the real
    Windows user profile's location, because some sandboxes redirect
    LOCALAPPDATA to an empty directory while the populated cache still lives
    under the original profile -- seeding from there avoids a ~100 MB
    re-download of the TeX bundle.

    Ranking by file count is NOT cosmetic: a redirected-but-empty cache root
    still exists and would otherwise be copied first, producing a stub cache
    that fails with "LaTeX requires expl3" (measured: 8 files vs 470).
    """
    cands = []
    primary = _default_tectonic_cache_root()
    if primary:
        cands.append(primary)
    if os.name == 'nt':
        up = os.environ.get('USERPROFILE') or os.environ.get('HOME')
        if up:
            cands.append(os.path.join(up, 'AppData', 'Local', 'TectonicProject'))
        users = os.path.join(os.environ.get('SystemDrive', 'C:') + os.sep, 'Users')
        try:
            for name in os.listdir(users):
                cands.append(os.path.join(users, name, 'AppData', 'Local',
                                          'TectonicProject'))
        except OSError:
            pass
    uniq = []
    for c in cands:
        if c and c not in uniq and os.path.isdir(c):
            uniq.append(c)
    ranked = [(c, _cache_file_count(c)) for c in uniq]
    ranked = [x for x in ranked if x[1] > 0]
    ranked.sort(key=lambda x: -x[1])
    return [c for c, _ in ranked]


def tectonic_env(workdir=None, log=None):
    """Return (env, note) for running tectonic.

    Normal case: the user's own cache is writable -> pass the environment
    through untouched, so nothing about a normal machine changes.

    Fallback: it is not writable -> point HOME / USERPROFILE / APPDATA /
    LOCALAPPDATA at a workspace-local cache root and seed it from the user's
    existing cache when one exists.
    """
    key = (workdir or '')
    if key in _TECTONIC_CACHE_CACHE:
        env, note = _TECTONIC_CACHE_CACHE[key]
        return dict(env), note

    env = dict(os.environ)
    default_root = _default_tectonic_cache_root()

    # Usable already?  Then do not touch anything.
    if default_root and _is_writable_dir(default_root):
        note = 'cache=user:%s' % default_root
        _TECTONIC_CACHE_CACHE[key] = (env, note)
        return dict(env), note

    # Fallback root: inside the workspace, so a sandbox that only allows
    # workspace writes still works.
    base = workdir or SKILL_ROOT
    root = os.path.join(base, '.tectonic-cache')
    local = os.path.join(root, 'AppData', 'Local')
    roaming = os.path.join(root, 'AppData', 'Roaming')
    if not _is_writable_dir(local):
        # last resort: the skill directory itself
        root = os.path.join(SKILL_ROOT, '.tectonic-cache')
        local = os.path.join(root, 'AppData', 'Local')
        roaming = os.path.join(root, 'AppData', 'Roaming')
        if not _is_writable_dir(local):
            note = 'cache=unavailable'
            _TECTONIC_CACHE_CACHE[key] = (env, note)
            return dict(env), note
    os.makedirs(roaming, exist_ok=True)

    # Seed from an existing cache so we do not re-download the ~100 MB bundle.
    # A previous run may have left a STUB cache here (measured: 8 files), which
    # makes tectonic die with "LaTeX requires expl3".  So compare counts and
    # re-seed when the local copy is clearly poorer than an available source.
    dst = os.path.join(local, 'TectonicProject')
    sources = _tectonic_cache_sources()
    have = _cache_file_count(dst) if os.path.isdir(dst) else 0
    seeded = have
    if sources:
        best = sources[0]
        best_n = _cache_file_count(best)
        if best_n > have:
            if os.path.isdir(dst):
                try:
                    shutil.rmtree(dst)
                except Exception as exc:
                    if log:
                        log('WARN: cannot replace stub tectonic cache: %s' % exc)
            try:
                shutil.copytree(best, dst)
                seeded = _cache_file_count(dst)
            except Exception as exc:  # a partial copy is still useful
                if log:
                    log('WARN: tectonic cache seed incomplete from %s: %s' % (best, exc))
                seeded = _cache_file_count(dst) if os.path.isdir(dst) else have

    env['HOME'] = root
    env['USERPROFILE'] = root
    env['APPDATA'] = roaming
    env['LOCALAPPDATA'] = local
    note = 'cache=fallback:%s seeded_files=%d' % (root, seeded)
    _TECTONIC_CACHE_CACHE[key] = (env, note)
    return dict(env), note


class BackendError(RuntimeError):
    """后端不可用或执行失败；消息已包含「下一步做什么」。"""


# ---------------------------------------------------------------- 小工具
def which(name_or_path):
    """Find an executable: explicit path > PATH > the skill's own tools/ dir.

    The tools/ search matters because setup_env.py installs tectonic THERE
    (about 50 MB, git-ignored).  Without it the bundled engine is invisible and
    every run reports "tectonic not found" even though it ships with the skill.
    """
    if not name_or_path:
        return None
    if os.path.isfile(name_or_path):
        return os.path.abspath(name_or_path)
    for cand in (name_or_path, name_or_path + '.exe', name_or_path + '.cmd', name_or_path + '.bat'):
        p = shutil.which(cand)
        if p:
            return p

    # Bundled tools/ (populated by scripts/setup_env.py).  Two places matter:
    #   * the SKILL directory -- setup_env.py installs there, so a copied-away
    #     skill carries its own toolchain;
    #   * the WORKSPACE ROOT -- inside this repo, setup_env.py was run when the
    #     scripts still lived at the repo root, so tools/ is one level ABOVE the
    #     skill (and it is git-ignored, so it will never be moved).
    base = os.path.basename(name_or_path)
    for cand in (base, base + '.exe'):
        for tools_dir in _tools_dirs():
            p = os.path.join(tools_dir, cand)
            if os.path.isfile(p):
                return p
    return None


def system_font_names():
    """Return the font FILE names available on this machine (lowercased).

    check_env.py uses this to decide whether a usable CJK / mono font exists.
    Font family names and file names differ, and LaTeX only sees families, so we
    enumerate FILES and let callers match fragments (e.g. 'notoserifsc').

        Windows: %WINDIR%\\Fonts
        macOS:   /System/Library/Fonts, /Library/Fonts, ~/Library/Fonts
        Linux:   /usr/share/fonts, ~/.fonts, ~/.local/share/fonts

    Returns [] when nothing can be enumerated. Callers MUST treat [] as
    "unknown", not as "no fonts", and report that honestly instead of failing.
    """
    names = []
    roots = []
    if os.name == 'nt':
        windir = os.environ.get('WINDIR') or r'C:\Windows'
        roots.append(os.path.join(windir, 'Fonts'))
    elif sys.platform == 'darwin':
        roots += ['/System/Library/Fonts', '/Library/Fonts',
                  os.path.expanduser('~/Library/Fonts')]
    else:
        roots += ['/usr/share/fonts', '/usr/local/share/fonts',
                  os.path.expanduser('~/.fonts'),
                  os.path.expanduser('~/.local/share/fonts')]

    exts = ('.ttf', '.ttc', '.otf', '.otc')
    for r in roots:
        if not os.path.isdir(r):
            continue
        try:
            for entry in os.listdir(r):
                low = entry.lower()
                if low.endswith(exts):
                    names.append(low)
                    continue
                sub = os.path.join(r, entry)
                if os.path.isdir(sub):
                    for _dirpath, _dirs, files in os.walk(sub):
                        for f in files:
                            if f.lower().endswith(exts):
                                names.append(f.lower())
        except OSError:
            continue
    return sorted(set(names))


def _load_config(path=None):
    """读 backend.json；不存在就返回空 dict（不算错误）。"""
    candidates = []
    if path:
        candidates.append(path)
    candidates.append(os.path.join(SKILL_ROOT, 'backend.json'))
    candidates.append(os.path.join(os.getcwd(), '.paper-skill-backend.json'))
    for c in candidates:
        if c and os.path.isfile(c):
            try:
                with open(c, encoding='utf-8') as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    data['_config_path'] = c
                    return data
            except Exception as exc:  # 配置坏了要报出来，不能静默用默认
                raise BackendError(
                    'backend.json 解析失败: %s (%s)\n'
                    '  修法：修正 JSON 语法，或删除该文件改用默认配置。' % (c, exc))
    return {}


def _first(*vals):
    for v in vals:
        if v not in (None, '', []):
            return v
    return None


# ---------------------------------------------------------------- 后端对象
class Backend(object):
    """执行后端的基类。子类实现 compile()，其余接口共用。"""

    name = 'base'

    def __init__(self, cfg):
        self.cfg = cfg

    # ---- 需要的可执行文件（子类覆盖）----
    def required_tools(self):
        return []

    def check(self):
        """返回 (ok, 消息列表)。不抛异常，供 --show / check_env 汇总。"""
        msgs, ok = [], True
        for tool in self.required_tools():
            p = which(tool)
            if p:
                msgs.append('OK   %-12s %s' % (tool, p))
            else:
                ok = False
                msgs.append('MISS %-12s %s' % (tool, INSTALL_HINTS.get(tool, '请安装 ' + tool)))
        return ok, msgs

    def describe(self):
        return {'backend': self.name, 'required_tools': self.required_tools()}

    def compile(self, workdir, main='main.tex', **kw):
        """编译 workdir/main.tex，返回 CompileResult。"""
        raise NotImplementedError

    def read_log(self, workdir):
        """返回编译日志文本；**读不到必须抛错**，绝不返回空串。

        这是实战血训：日志抓取失败被静默当成 0 个错误 => 门控形同虚设。
        """
        raise NotImplementedError


class CompileResult(object):
    """一次编译的结构化结果。"""

    def __init__(self, ok, backend, pdf, log_path, returncode, stdout='', stderr='', note=''):
        self.ok = ok
        self.backend = backend
        self.pdf = pdf
        self.log_path = log_path
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.note = note

    def as_dict(self):
        return {'ok': self.ok, 'backend': self.backend, 'pdf': self.pdf,
                'log': self.log_path, 'returncode': self.returncode, 'note': self.note}

    def __repr__(self):
        return 'CompileResult(ok=%s, pdf=%s, rc=%s)' % (self.ok, self.pdf, self.returncode)


def _purge_corrupt_intermediates(workdir, stem):
    """Delete LaTeX scratch files that are invalid on their face.

    MEASURED on #07: `main.aux` was 7870 bytes of pure NUL, and tectonic then
    died with `! Text line contains an invalid character. l.1 ^^@` -- the paper
    reported ERRS=1 / GATE-FAIL even though nothing about the document was
    wrong.  Only .aux/.out/.toc are touched (the engine regenerates them); .log
    is deliberately KEPT because the gate must still be able to read it.

    A file counts as corrupt when it is non-empty and more than half its first
    64 KiB is NUL.  Returns the names dropped (empty when nothing was wrong).
    """
    dropped = []
    for ext in ('.aux', '.out', '.toc', '.synctex.gz'):
        p = os.path.join(workdir, stem + ext)
        if not os.path.isfile(p):
            continue
        try:
            size = os.path.getsize(p)
            if size == 0:
                continue
            with open(p, 'rb') as fh:
                blob = fh.read(65536)
            if not blob:
                continue
            if blob.count(b'\x00') * 2 > len(blob):
                os.remove(p)
                dropped.append(stem + ext)
        except OSError:
            continue
    return dropped


class LocalTectonicBackend(Backend):
    """默认后端：本机单文件 LaTeX 引擎 tectonic。

    关键点（已实测，tectonic 0.15.0 / Windows）：
      · tectonic 自己决定跑几遍（PassSetting::Default：一直重跑到收敛），
        **不需要**像 xelatex 那样手工跑两遍。
      · 主输入文件名决定引擎日志名：主输入 main.tex => <outdir>/main.log。
      · --keep-logs 保留 .log；--keep-intermediates 保留 .aux 等。
      · 失败时 tectonic 退出码非 0，但日志仍可能已落盘 => 先读日志再判。
    """

    name = 'local-tectonic'

    def __init__(self, cfg):
        Backend.__init__(self, cfg)
        self.bin = _first(cfg.get('tectonic_bin'),
                          os.environ.get('TECTONIC_BIN'),
                          which('tectonic'))
        self.extra_args = list(_first(cfg.get('tectonic_args'), DEFAULT_TECTONIC_ARGS))
        self.only_cached = bool(_first(cfg.get('only_cached'), False))

    def required_tools(self):
        return [self.bin] if self.bin else ['tectonic']

    def describe(self):
        d = Backend.describe(self)
        d.update({'tectonic_bin': self.bin, 'tectonic_args': self.extra_args,
                  'only_cached': self.only_cached})
        return d

    def compile(self, workdir, main='main.tex', timeout=1800):
        if not self.bin:
            raise BackendError(INSTALL_HINTS['tectonic'])
        main_path = os.path.join(workdir, main)
        if not os.path.isfile(main_path):
            raise BackendError(
                '找不到 %s。\n  修法：先跑 prep.py 生成 body.tex，再由 runpaper.py 写出 %s。'
                % (main_path, main))
        cmd = [self.bin, main_path, '--outdir', workdir] + self.extra_args
        if self.only_cached:
            cmd.append('--only-cached')

        # --- stale/corrupt intermediates guard -----------------------------
        # MEASURED on #07: a previous run left `main.aux` filled with 7870 NUL
        # bytes.  tectonic then read it back and aborted with
        #     ! Text line contains an invalid character.  l.1 ^^@
        # producing `compile ok=False rc=1` and `GATE-FAIL: ERRS=1`, i.e. the
        # paper looked BROKEN when only a stale scratch file was wrong.  A
        # .aux that is mostly NULs is never valid LaTeX, so drop it and let the
        # engine regenerate.  This is a local, reproducible repair: removing the
        # file made #07 PASS (30 pages, ERRS=0).
        dropped = _purge_corrupt_intermediates(workdir,
                                               os.path.splitext(main)[0])
        if dropped:
            print('[8] dropped corrupt intermediates: %s' % ', '.join(dropped))

        # Redirect tectonic's cache when its default location is not writable
        # (see the TECTONIC CACHE block near the top for the measured failure).
        env, cache_note = tectonic_env(workdir)
        if cache_note.startswith('cache=fallback') and not getattr(self, '_cache_logged', False):
            print('[8] tectonic %s' % cache_note)
            self._cache_logged = True
        try:
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True,
                                  timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise BackendError(
                'tectonic 超过 %ds 未结束。\n'
                '  常见原因：首次编译正在联网下载宏包。\n'
                '  修法：先手工跑一次 `tectonic main.tex --outdir .` 完成缓存，再重试。' % timeout)
        out = (proc.stdout or b'').decode('utf-8', 'replace')
        err = (proc.stderr or b'').decode('utf-8', 'replace')
        pdf = os.path.join(workdir, os.path.splitext(main)[0] + '.pdf')
        log = os.path.join(workdir, os.path.splitext(main)[0] + '.log')
        note = ''
        if proc.returncode != 0 and not os.path.isfile(log):
            note = ('tectonic 返回 %d 且没有产出日志；多半是宏包下载失败或引擎自身报错。\n'
                    '  修法：手工执行 `%s %s --outdir %s --keep-logs --print` 看完整输出。'
                    % (proc.returncode, self.bin, main, workdir))
            if 'os error 5' in err or '\u62d2\u7edd\u8bbf\u95ee' in err:
                note += ('\n  另注：stderr 出现「拒绝访问 / os error 5」时，几乎总是 tectonic 的\n'
                         '  缓存目录不可写（默认写到用户 AppData，可能在工作区之外）。\n'
                         '  本次缓存策略：%s\n'
                         '  修法：让工作区或 .tectonic-cache 可写，或把 HOME/LOCALAPPDATA\n'
                         '  指向可写目录后重试。' % cache_note)
        return CompileResult(proc.returncode == 0, self.name,
                             pdf if os.path.isfile(pdf) else None,
                             log if os.path.isfile(log) else None,
                             proc.returncode, out, err, note)

    def read_log(self, workdir, main='main.tex', result=None):
        log = os.path.join(workdir, os.path.splitext(main)[0] + '.log')
        if not os.path.isfile(log):
            raise BackendError(
                'gate-log-unreadable: %s 不存在。\n'
                '  修法：确认 tectonic 带 --keep-logs 运行；若编译早期就崩，日志可能确实没生成，\n'
                '        手工跑一次加上 --print 看 stderr。' % log)
        with open(log, encoding='utf-8', errors='replace') as fh:
            txt = fh.read()
        if not txt.strip():
            raise BackendError('gate-log-unreadable: %s 是空文件。\n'
                               '  修法：同上次编译的命令加 --print 重跑。' % log)
        return txt


class SshXelatexBackend(Backend):
    """可选后端：远端容器/服务器上跑 pandoc + xelatex（远端容器形态）。

    主输入名固定为 main.tex（显式 cd 到工作目录后编译），因此日志恒为 main.log，
    与 V1 tectonic 后端的口径完全一致 —— 门控代码无需分支。
    """

    name = 'ssh-xelatex'

    def __init__(self, cfg):
        Backend.__init__(self, cfg)
        self.host = _first(cfg.get('ssh_host'), os.environ.get('PAPER_SSH_HOST'))
        self.ssh_bin = _first(cfg.get('ssh_bin'), which('ssh'))
        self.remote_root = _first(cfg.get('ssh_remote_root'), '~/paperbuild')
        self.passes = int(_first(cfg.get('xelatex_passes'), 2))
        self.timeout = int(_first(cfg.get('ssh_timeout'), DEFAULT_SSH_TIMEOUT))
        self.xelatex = _first(cfg.get('xelatex_bin'), 'xelatex')
        self.pandoc = _first(cfg.get('pandoc_bin'), 'pandoc')

    def required_tools(self):
        return [self.ssh_bin] if self.ssh_bin else ['ssh']

    def describe(self):
        d = Backend.describe(self)
        d.update({'ssh_host': self.host, 'ssh_bin': self.ssh_bin,
                  'remote_root': self.remote_root, 'xelatex_passes': self.passes,
                  'xelatex_bin': self.xelatex, 'pandoc_bin': self.pandoc})
        if not self.host:
            d['warning'] = ('未配置 ssh_host。修法：设 PAPER_SSH_HOST=user@host:port，'
                            '或在 backend.json 写 {"ssh_host": "..."}。')
        return d

    # ---- 远端执行 ----
    def _ssh(self, remote_cmd, timeout=None):
        if not self.ssh_bin:
            raise BackendError(INSTALL_HINTS['ssh'])
        if not self.host:
            raise BackendError(
                'ssh-xelatex 后端缺少 ssh_host。\n'
                '  修法：设环境变量 PAPER_SSH_HOST=user@host 或 -p 端口写成 user@host:2222，\n'
                '        或改用默认后端（去掉 --backend ssh-xelatex）。')
        target, port_args = self.host, []
        if ':' in self.host and not self.host.startswith('['):
            h, _, p = self.host.rpartition(':')
            if p.isdigit():
                target, port_args = h, ['-p', p]
        cmd = [self.ssh_bin] + port_args + ['-o', 'BatchMode=yes',
                                            '-o', 'ConnectTimeout=20', target, remote_cmd]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout or self.timeout)
        return proc

    def remote_dir(self, workdir):
        """远端工作目录名（用本地目录名，便于多篇隔离）。"""
        return '%s/%s' % (self.remote_root.rstrip('/'), os.path.basename(os.path.abspath(workdir)))

    def push(self, workdir):
        """把工作目录整体推上去。用 tar 单次管道传输 —— 短时间大量 scp 会触发 fail2ban。"""
        rdir = self.remote_dir(workdir)
        self._ssh('mkdir -p %s /tmp' % rdir)
        tar = shutil.which('tar')
        if not tar:
            raise BackendError(
                '未找到 tar，无法用单次管道推送工作目录（避免多次 scp 触发限频）。\n'
                '  Windows 10+ 自带 tar.exe；若缺失，改用 --backend local-tectonic。')
        pack = subprocess.run([tar, 'cf', '-', '-C', workdir, '.'], capture_output=True)
        if pack.returncode != 0:
            raise BackendError('tar 打包失败: ' + (pack.stderr or b'').decode('utf-8', 'replace'))
        unpack_cmd = [self.ssh_bin] + (['-p', self.host.rpartition(':')[2]]
                                       if ':' in self.host and self.host.rpartition(':')[2].isdigit()
                                       else []) + \
                      [self.host, 'tar xf - -C %s' % rdir]
        proc = subprocess.run(unpack_cmd, input=pack.stdout, capture_output=True,
                              timeout=self.timeout)
        if proc.returncode != 0:
            raise BackendError('远端解包失败: ' + (proc.stderr or b'').decode('utf-8', 'replace'))
        return rdir

    def compile(self, workdir, main='main.tex', **kw):
        rdir = self.push(workdir)
        cmds = []
        for i in range(1, self.passes + 1):
            cmds.append('cd %s && %s -interaction=nonstopmode %s > b%d.log 2>&1'
                        % (rdir, self.xelatex, main, i))
        cmds.append('cp %s/main.log %s/main.log.keep 2>/dev/null || true' % (rdir, rdir))
        proc = self._ssh(' ; '.join(cmds))
        out = (proc.stdout or b'').decode('utf-8', 'replace')
        pdf = os.path.join(workdir, os.path.splitext(main)[0] + '.pdf')
        # 拉回产物与日志
        fetch = ('cd %s && tar cf - %s %s 2>/dev/null'
                 % (rdir, os.path.splitext(main)[0] + '.pdf',
                    os.path.splitext(main)[0] + '.log'))
        fp = self._ssh(fetch)
        note = ''
        if fp.returncode == 0 and fp.stdout:
            tar = shutil.which('tar')
            ex = subprocess.run([tar, 'xf', '-', '-C', workdir], input=fp.stdout,
                                capture_output=True)
            if ex.returncode != 0:
                note = '回传解包失败：' + (ex.stderr or b'').decode('utf-8', 'replace')
        else:
            note = ('未取回 PDF/日志（远端可能未编译成功）。\n'
                    '  修法：手工 ssh %s "cd %s && cat b2.log | head -50" 查看。'
                    % (self.host, rdir))
        log = os.path.join(workdir, os.path.splitext(main)[0] + '.log')
        return CompileResult(proc.returncode == 0, self.name,
                             pdf if os.path.isfile(pdf) else None,
                             log if os.path.isfile(log) else None,
                             proc.returncode, out, '', note)

    def read_log(self, workdir, main='main.tex', result=None):
        log = os.path.join(workdir, os.path.splitext(main)[0] + '.log')
        if not os.path.isfile(log) or not open(log, encoding='utf-8', errors='replace').read().strip():
            raise BackendError(
                'gate-log-unreadable: 未能从远端取回非空 %s。\n'
                '  修法：检查 ssh 连通性（ssh %s echo __OK__），确认远端 xelatex 跑完；\n'
                '        不要把这种情况当成「0 个错误」放行。' % (log, self.host))
        return open(log, encoding='utf-8', errors='replace').read()


BACKENDS = {
    LocalTectonicBackend.name: LocalTectonicBackend,
    SshXelatexBackend.name: SshXelatexBackend,
}


def make_backend(name=None, config=None, **overrides):
    """工厂：name 为空时按 环境变量 > 配置文件 > 默认 的顺序决定。"""
    cfg = dict(_load_config((config or {}).get('config_path') if isinstance(config, dict) else None)
               if isinstance(config, dict) else _load_config())
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if k != 'config_path'})
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    name = _first(name, cfg.get('backend'), os.environ.get('PAPER_SKILL_BACKEND'),
                  LocalTectonicBackend.name)
    if name not in BACKENDS:
        raise BackendError(
            '未知后端 %r。可选：%s\n  修法：--backend 只接受这两个值。'
            % (name, ' | '.join(sorted(BACKENDS))))
    return BACKENDS[name](cfg)


def add_backend_args(parser):
    """给调用方的 argparse 挂上统一的后端参数。"""
    g = parser.add_argument_group('执行后端（编译）')
    g.add_argument('--backend', choices=sorted(BACKENDS), default=None,
                   help='执行后端；默认 local-tectonic。也可用 PAPER_SKILL_BACKEND 指定')
    g.add_argument('--config', default=None, metavar='JSON',
                   help='后端配置文件（默认读 <skill>/backend.json）')
    g.add_argument('--ssh-host', default=None, metavar='USER@HOST[:PORT]',
                   help='ssh-xelatex 后端的目标主机')
    g.add_argument('--tectonic-bin', default=None, metavar='PATH',
                   help='tectonic 可执行文件路径（默认从 PATH 找）')
    return g


def backend_from_args(args):
    """从 argparse 命名空间构造后端。"""
    cfg = {'config_path': getattr(args, 'config', None)}
    return make_backend(getattr(args, 'backend', None), cfg,
                        ssh_host=getattr(args, 'ssh_host', None),
                        tectonic_bin=getattr(args, 'tectonic_bin', None))


# ---------------------------------------------------------------- 自测
MINIMAL_TEX = r"""\documentclass[10pt,twocolumn]{article}
\usepackage[paperwidth=612bp,paperheight=792bp,left=49bp,right=48.9bp,top=29.9bp,bottom=19.8bp]{geometry}
\usepackage{booktabs,tabularx,array,graphicx,caption}
\usepackage{fontspec}
\usepackage{xeCJK}
\setCJKmainfont{Noto Serif SC}
\begin{document}
\twocolumn[\begin{center}{\LARGE 后端自测}\end{center}]
\section*{1 引言}
中文编译自测文本 abc 123。
\begin{table}[!t]\centering\scriptsize
\textbf{表 1（Table 1）. 自测}\par\vspace{4pt}
\begin{tabular}{@{}ll@{}}
\toprule
A & B \\ \midrule
中文 & English \\ \bottomrule
\end{tabular}
\end{table}
\end{document}
"""


def cmd_show(args):
    be = backend_from_args(args)
    print('BACKEND=%s' % be.name)
    print('SKILL_ROOT=%s' % SKILL_ROOT)
    cfgpath = getattr(be.cfg, 'get', lambda *_: None)('_config_path')
    print('CONFIG=%s' % (cfgpath or '(none)'))
    for k, v in sorted(be.describe().items()):
        print('CFG %s=%s' % (k, v))
    ok, msgs = be.check()
    for m in msgs:
        print(m)
    print('READY=%s' % ('yes' if ok else 'no'))
    return 0 if ok else 3


def cmd_selftest(args):
    be = backend_from_args(args)
    ok, msgs = be.check()
    if not ok:
        print('后端不可用，自测中止：')
        for m in msgs:
            if m.startswith('MISS'):
                print(m)
        return 3
    d = os.path.abspath(args.dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'main.tex'), 'w', encoding='utf-8') as fh:
        fh.write(MINIMAL_TEX)
    print('自测目录: %s' % d)
    res = be.compile(d)
    print('COMPILE_OK=%s' % ('yes' if res.ok else 'no'))
    print('PDF=%s' % (res.pdf or '(none)'))
    print('LOG=%s' % (res.log_path or '(none)'))
    if res.note:
        print('NOTE=%s' % res.note)
    try:
        txt = be.read_log(d)
        import re
        print('ERRS=%d' % len(re.findall(r'(?m)^!', txt)))
        print('MISS=%d' % len(re.findall(r'Missing character', txt)))
        hb = [float(x) for x in re.findall(r'Overfull \\hbox \(([0-9.]+)pt', txt)]
        print('OVF_H_MAX=%s' % (max(hb) if hb else 0.0))
    except BackendError as exc:
        print('LOG_UNREADABLE=yes')
        print(str(exc))
        return 4
    return 0 if res.ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='_backend.py',
        description='执行后端抽象：local-tectonic（默认，本机单文件引擎）或 ssh-xelatex（远端两遍编译）。',
        epilog='示例：\n'
               '  python scripts/_backend.py --show\n'
               '  python scripts/_backend.py --selftest .work/backend-selftest\n'
               '  python scripts/_backend.py --backend ssh-xelatex --ssh-host me@box:2222 --show\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--show', action='store_true', help='打印生效配置与依赖探测结果')
    ap.add_argument('--selftest', action='store_true', help='编译一个最小中文文档验证链路')
    ap.add_argument('dir_pos', nargs='?', default=None, metavar='DIR',
                    help='位置参数形式的自测目录（等价于 --dir DIR）')
    ap.add_argument('--dir', default='.work/backend-selftest', metavar='DIR',
                    help='--selftest 使用的工作目录（默认 .work/backend-selftest）')
    add_backend_args(ap)
    args = ap.parse_args(argv)
    if getattr(args, 'dir_pos', None):
        args.dir = args.dir_pos
    try:
        if args.selftest:
            return cmd_selftest(args)
        return cmd_show(args)
    except BackendError as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
