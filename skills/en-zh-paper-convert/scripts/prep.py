#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prep.py -- markdown -> LaTeX preprocessing for Chinese paper bodies.

Responsibilities
    1. Unicode math symbols / math alphanumeric blocks -> LaTeX commands
       (with a fallback when no glyph exists)
    2. Figure caption lines -> figure environments (image inserted when
       figs.json has a hit)
    3. Table captions + pipe tables -> **prefer the vector-recreated table
       PDF from the source** (tab_<no>.pdf);
       fall back to hand-built tabularx (with column-width solving) only
       when there is no hit
    4. ASCII-art block diagrams -> Verbatim (monospace preserved, **not**
       a table)

Key benchmark fixes (losing these reintroduces the bugs)
    - split_md_table detects the separator row by **content**: hardcoding
      cells[2:] assumes row 2 is always the separator ->
      a table with NO separator row silently loses its first real data row
      (measured: an acronym table lost its CID row).
    - Column-width solving follows **real tabularx semantics**: X columns
      share the remaining width, and an X column itself also carries
      2*tabcolsep of padding, so usable content width = share - pad.
      Skipping this underestimates the requirement.
    - **Natural-width tabular must be width-estimated**: a multi-column CJK
      table necessarily overflows the column (measured 23.8pt) -> if the
      estimate exceeds the available width, switch to X columns. The Latin
      character factor is 4.4 (calibrated: the old 4.0 underestimated by ~8%).
    - X columns use >{\\RaggedRight\\arraybackslash}X (not \\raggedright)
      -- combined with
      \\sloppy\\hyphenpenalty=50\\exhyphenpenalty=50 inside the table
      environment, this allows intra-word breaking,
      the **only** effective fix for long English words overflowing narrow
      X columns (measured 12.3 -> 0.0pt).
    - Wide tables upgrade to table* + \\textwidth + \\scriptsize (many
      columns / a >14 char token with no spaces).
    - A vector-recreated table **returns immediately** and is never modified
      by any layout rule (fidelity first, R-T1).

Dependencies
    Standard library only (Python 3.9+).

Usage
    python scripts/prep.py body-clean.md body.md --figs .work/01/figs --tables .work/01/tables --mode conf
    python scripts/prep.py body-clean.md body.md --tables .work/01/tables --mode access
    python scripts/prep.py body-clean.md body.md --check   # health check only

Available column width (AVAIL)
    Two-column columnwidth = (textwidth - columnsep) / 2;
    single-column columnwidth = textwidth.
    Passed in via --text-width (runpaper.py computes it from the geometry
    ledger); when unavailable, fall back to a conservative constant
    and report AVAIL_SOURCE=fallback (better conservative than optimistic).

Structured output
    PREP OK body.md AVAIL=239.6 AVAIL_SOURCE=geometry FIGENVS=3 TABIMGS=2 TABENVS=0
    SUMMARY OUTPUT=body.md FIGENVS=3 TABIMGS=2 TABENVS=0 TABULARX=0
"""
import argparse
import json
import os
import re
import sys
import unicodedata

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

EXIT_OK, EXIT_USAGE = 0, 2

# Conservative fallback when the real column width is unavailable
# (better conservative -- an optimistic value misses overflow)
AVAIL_CONF_FALLBACK = 236.0
AVAIL_ACCESS_FALLBACK = 460.0
COLSEP = 14.0                 # \\setlength{\\columnsep}{14pt} in the template
TABCOLSEP_PAD = 12.0          # 2*tabcolsep padding per column
LAT_PT_PER_CHAR = 4.4         # Latin char width factor (calibrated;
                              # the old 4.0 underestimated by ~8%)
CJK_PT_PER_CHAR = 8.0
XX = r'>{\RaggedRight\arraybackslash}X'


# ============================================================ mathify
MATHMAP = {
    '\u2131': r'\ensuremath{\mathcal{F}}',
    '\u223c': r'\ensuremath{\sim}',
    '\u2245': r'\ensuremath{\cong}',
    '\u2205': r'\ensuremath{\emptyset}',
    '\u221d': r'\ensuremath{\propto}',
    '\u2192': r'\ensuremath{\rightarrow}',
    '\U0001d4b2': r'\ensuremath{\mathcal{W}}',
    '\U0001d4b3': r'\ensuremath{\mathcal{X}}',
    '\U0001d4a6': r'\ensuremath{\mathcal{K}}',
    '\U0001d4a5': r'\ensuremath{\mathcal{J}}',
    '\U0001d4b0': r'\ensuremath{\mathcal{U}}',
    '\U0001d49f': r'\ensuremath{\mathcal{D}}',
    '\U0001d54f': r'\ensuremath{\mathbb{L}}',
    '\U0001d54a': r'\ensuremath{\mathbb{S}}',
    '\U0001d54e': r'\ensuremath{\mathbb{W}}',
    '\u210f': r'\ensuremath{\hbar}',
    '\u2112': r'\ensuremath{\mathcal{L}}',
    '\U0001d4ae': r'\ensuremath{\mathcal{S}}',
    '\u210b': r'\ensuremath{\mathcal{H}}',
    '\u2713': r'\ensuremath{\checkmark}',
    'c\u20dd': r'\textcopyright{}',
    '\u2208': r'\ensuremath{\in}',
    '\u226a': r'\ensuremath{\ll}',
    '\u207b': r'\textsuperscript{-}',
    '\u2460': r'\textcircled{\scriptsize 1}',
    '\u2461': r'\textcircled{\scriptsize 2}',
    '\u2462': r'\textcircled{\scriptsize 3}',
    '\u2463': r'\textcircled{\scriptsize 4}',
    '\u2464': r'\textcircled{\scriptsize 5}',
    '\u2465': r'\textcircled{\scriptsize 6}',
    '\u2466': r'\textcircled{\scriptsize 7}',
    '\u2467': r'\textcircled{\scriptsize 8}',
    '\u2468': r'\textcircled{\scriptsize 9}',
    '\u2469': r'\textcircled{\scriptsize 10}',
    '\u2299': r'\ensuremath{\odot}',
    '\u2282': r'\ensuremath{\subset}',
    '\u2286': r'\ensuremath{\subseteq}',
    '\u2207': r'\ensuremath{\nabla}',
    '\u2200': r'\ensuremath{\forall}',
    '\u21a6': r'\ensuremath{\mapsto}',
    '\u229b': r'\ensuremath{\circledast}',
    '\u25b3': r'\ensuremath{\triangle}',
    '\u227b': r'\ensuremath{\succ}',
    '\u2aaf': r'\ensuremath{\preceq}',
    '\u2ab0': r'\ensuremath{\succeq}',
    '\u220e': r'\ensuremath{\blacksquare}',
    '\u2103': r'\ensuremath{^{\circ}\text{C}}',
    '\u1d40': r'\ensuremath{^{\mathrm{T}}}',
    '\u22c6': r'\ensuremath{\star}',
    '\u03b1': r'\ensuremath{\alpha}',
    '\u03b2': r'\ensuremath{\beta}',
    '\u03b3': r'\ensuremath{\gamma}',
    '\u03b4': r'\ensuremath{\delta}',
    '\u03b5': r'\ensuremath{\epsilon}',
    '\u03b6': r'\ensuremath{\zeta}',
    '\u03b7': r'\ensuremath{\eta}',
    '\u03b8': r'\ensuremath{\theta}',
    '\u03b9': r'\ensuremath{\iota}',
    '\u03ba': r'\ensuremath{\kappa}',
    '\u03bb': r'\ensuremath{\lambda}',
    '\u03bc': r'\ensuremath{\mu}',
    '\u03bd': r'\ensuremath{\nu}',
    '\u03be': r'\ensuremath{\xi}',
    '\u03bf': r'\ensuremath{\omicron}',
    '\u03c0': r'\ensuremath{\pi}',
    '\u03c1': r'\ensuremath{\rho}',
    '\u03c3': r'\ensuremath{\sigma}',
    '\u03c4': r'\ensuremath{\tau}',
    '\u03c5': r'\ensuremath{\upsilon}',
    '\u03c6': r'\ensuremath{\phi}',
    '\u03c7': r'\ensuremath{\chi}',
    '\u03c8': r'\ensuremath{\psi}',
    '\u03c9': r'\ensuremath{\omega}',
    '\u0391': r'\ensuremath{\Alpha}',
    '\u0392': r'\ensuremath{\Beta}',
    '\u0393': r'\ensuremath{\Gamma}',
    '\u0394': r'\ensuremath{\Delta}',
    '\u0395': r'\ensuremath{\Epsilon}',
    '\u0396': r'\ensuremath{\Zeta}',
    '\u0397': r'\ensuremath{\Eta}',
    '\u0398': r'\ensuremath{\Theta}',
    '\u0399': r'\ensuremath{\Iota}',
    '\u039a': r'\ensuremath{\Kappa}',
    '\u039b': r'\ensuremath{\Lambda}',
    '\u039c': r'\ensuremath{\Mu}',
    '\u039d': r'\ensuremath{\Nu}',
    '\u039e': r'\ensuremath{\Xi}',
    '\u039f': r'\ensuremath{\Omicron}',
    '\u03a0': r'\ensuremath{\Pi}',
    '\u03a1': r'\ensuremath{\Rho}',
    '\u03a3': r'\ensuremath{\Sigma}',
    '\u03a4': r'\ensuremath{\Tau}',
    '\u03a5': r'\ensuremath{\Upsilon}',
    '\u03a6': r'\ensuremath{\Phi}',
    '\u03a7': r'\ensuremath{\Chi}',
    '\u03a8': r'\ensuremath{\Psi}',
    '\u03a9': r'\ensuremath{\Omega}',
    '\u2209': r'\ensuremath{\notin}',
    '\u03c2': r'\ensuremath{\varsigma}',
    '\u22c2': r'\ensuremath{\bigcap}',
    '\u2202': r'\ensuremath{\partial}',
    '\u221e': r'\ensuremath{\infty}',
    '\u2264': r'\ensuremath{\leq}',
    '\u2265': r'\ensuremath{\geq}',
    '\u2260': r'\ensuremath{\neq}',
    '\u2248': r'\ensuremath{\approx}',
    '\u00d7': r'\ensuremath{\times}',
    '\u00f7': r'\ensuremath{\div}',
    '\u00b1': r'\ensuremath{\pm}',
    '\u2211': r'\ensuremath{\sum}',
    '\u220f': r'\ensuremath{\prod}',
    '\u222b': r'\ensuremath{\int}',
    '\u221a': r'\ensuremath{\sqrt{}}',
    '\u22c5': r'\ensuremath{\cdot}',
    '\u00b7': r'\ensuremath{\cdot}',
    '\u2217': r'\ensuremath{\ast}',
    '\u2218': r'\ensuremath{\circ}',
    '\u2219': r'\ensuremath{\bullet}',
    '\u225c': r'\ensuremath{\triangleq}',
    '\u2297': r'\ensuremath{\otimes}',
    '\u2295': r'\ensuremath{\oplus}',
    '\u2213': r'\ensuremath{\mp}',
    '\u2216': r'\ensuremath{\setminus}',
    '\u2225': r'\ensuremath{\parallel}',
    '\u230a': r'\ensuremath{\lfloor}',
    '\u230b': r'\ensuremath{\rfloor}',
    '\u2308': r'\ensuremath{\lceil}',
    '\u2309': r'\ensuremath{\rceil}',
    '\u22ef': r'\ensuremath{\cdots}',
    '\u22ee': r'\ensuremath{\vdots}',
    '\u2234': r'\ensuremath{\therefore}',
    '\u2235': r'\ensuremath{\because}',
    '\u2254': r'\ensuremath{:=}',
}
for _i, _cp in enumerate(range(0x2460, 0x246A), 1):
    MATHMAP[chr(_cp)] = r'\textcircled{\scriptsize %d}' % _i


_MA_RE = re.compile('[\U0001D400-\U0001D7FF]')
_DOT_RE = re.compile('(.)\u0307')
_DDOT_RE = re.compile('(.)\u0308')
_SUB_RE = re.compile('[\u2080-\u209F\u1D62-\u1D6A]')
_SUP_RE = re.compile('[\u00B2\u00B3\u00B9\u2070-\u207F\u1D2C-\u1D61]')
_GREEK_NAMES = {'ALPHA': '\u03b1', 'BETA': '\u03b2', 'GAMMA': '\u03b3',
                'DELTA': '\u03b4', 'EPSILON': '\u03b5', 'ZETA': '\u03b6',
                'ETA': '\u03b7', 'THETA': '\u03b8', 'IOTA': '\u03b9',
                'KAPPA': '\u03ba', 'LAMBDA': '\u03bb', 'MU': '\u03bc',
                'NU': '\u03bd', 'XI': '\u03be', 'OMICRON': '\u03bf',
                'PI': '\u03c0', 'RHO': '\u03c1', 'SIGMA': '\u03c3',
                'TAU': '\u03c4', 'UPSILON': '\u03c5', 'PHI': '\u03c6',
                'CHI': '\u03c7', 'PSI': '\u03c8', 'OMEGA': '\u03c9',
                'THETA SYMBOL': '\u03d1', 'KAPPA SYMBOL': '\u03ba',
                'RHO SYMBOL': '\u03f1', 'PHI SYMBOL': '\u03d5',
                'EPSILON SYMBOL': '\u03b5', 'DIGAMMA': '\u03dd'}
_DIGIT_NAMES = {'ZERO': '0', 'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4',
                'FIVE': '5', 'SIX': '6', 'SEVEN': '7', 'EIGHT': '8', 'NINE': '9'}
_GREEK_CMD = {'ALPHA': 'alpha', 'BETA': 'beta', 'GAMMA': 'gamma', 'DELTA': 'delta',
              'EPSILON': 'epsilon', 'EPSILON SYMBOL': 'epsilon', 'ZETA': 'zeta',
              'ETA': 'eta', 'THETA': 'theta', 'THETA SYMBOL': 'vartheta', 'IOTA': 'iota',
              'KAPPA': 'kappa', 'KAPPA SYMBOL': 'varkappa', 'LAMBDA': 'lambda', 'MU': 'mu',
              'NU': 'nu', 'XI': 'xi', 'OMICRON': 'o', 'PI': 'pi', 'PI SYMBOL': 'pi',
              'RHO': 'rho', 'RHO SYMBOL': 'varrho', 'SIGMA': 'sigma',
              'SIGMA SYMBOL': 'varsigma', 'TAU': 'tau', 'UPSILON': 'upsilon',
              'PHI': 'phi', 'PHI SYMBOL': 'varphi', 'CHI': 'chi', 'PSI': 'psi',
              'OMEGA': 'omega', 'DIGAMMA': 'digamma'}


def _map_sub(m):
    ch = m.group(0)
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return ch
    if 'SUBSCRIPT' not in name:
        return ch
    # Unicode name is e.g. "SUBSCRIPT PLUS SIGN" / "MINUS SIGN" / "EQUALS SIGN"
    if 'PLUS' in name:
        b = '+'
    elif 'MINUS' in name:
        b = '-'
    elif 'EQUALS' in name:
        b = '='
    elif 'LEFT PARENTHESIS' in name:
        b = '('
    elif 'RIGHT PARENTHESIS' in name:
        b = ')'
    else:
        ent = name.split()[-1]
        if ent in _DIGIT_NAMES:
            b = _DIGIT_NAMES[ent]
        elif len(ent) == 1:
            b = ent
        else:
            return ch
    return r'\ensuremath{_{%s}}' % b.lower()


def _map_sup(m):
    ch = m.group(0)
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return ch
    if 'SUPERSCRIPT' not in name and 'MODIFIER' not in name:
        return ch
    if 'PLUS SIGN' in name:
        b = '+'
    elif 'MINUS SIGN' in name:
        b = '-'
    elif 'EQUALS SIGN' in name:
        b = '='
    else:
        ent = name.split()[-1]
        if ent in _DIGIT_NAMES:
            b = _DIGIT_NAMES[ent]
        elif len(ent) == 1:
            b = ent
        else:
            return ch
    return r'\ensuremath{^{%s}}' % (b if 'MODIFIER' in name else b.lower())


def _map_ma(m, plain=False):
    ch = m.group(0)
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return ch
    parts = name.split()
    if not parts or parts[0] != 'MATHEMATICAL':
        return ch
    ent = parts[-1]
    if ent in _GREEK_NAMES:
        base = _GREEK_NAMES[ent]
        cmd = _GREEK_CMD.get(ent)
        if cmd and not plain:
            # Greek letters have no Unicode glyph in math mode (cmmi10),
            # so a command is required
            return r'\ensuremath{\%s}' % cmd
        return base
    if len(ent) == 1 and ent.isalpha():
        base = ent
    elif ent in _DIGIT_NAMES:
        base = _DIGIT_NAMES[ent]
    elif ent == 'NABLA':
        return '\u2207' if plain else r'\ensuremath{\nabla}'
    elif ent == 'PARTIAL':
        return '\u2202' if plain else r'\ensuremath{\partial}'
    else:
        return ch
    if plain:
        return base
    style = ' '.join(parts[1:-1])
    if 'SCRIPT' in style:
        return r'\ensuremath{\mathcal{%s}}' % base
    if 'DOUBLE-STRUCK' in style:
        return r'\ensuremath{\mathbb{%s}}' % base
    if 'FRAKTUR' in style:
        return r'\ensuremath{\mathfrak{%s}}' % base
    return base


def _base_char(m):
    ch = m.group(0)
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return ch
    if 'PLUS' in name and ('SUBSCRIPT' in name or 'SUPERSCRIPT' in name):
        return '+'
    if 'MINUS' in name and ('SUBSCRIPT' in name or 'SUPERSCRIPT' in name):
        return '-'
    ent = name.split()[-1]
    if ent in _DIGIT_NAMES:
        return _DIGIT_NAMES[ent]
    if len(ent) == 1:
        return ent.lower() if 'SMALL' in name else ent
    return ch


def _vec_arrow(t):
    # Must run BEFORE MATHMAP: otherwise the char is already split into a
    # command and the arrow rule never fires
    COMB_ARROW = '\u20d7'          # combining arrow above U+20D7
    t = t.replace('\u2131' + COMB_ARROW, r'\ensuremath{\vec{\mathcal{F}}}')
    t = re.sub(r'\\mathcal\{([A-Za-z])\}' + COMB_ARROW,
               lambda m: r'\ensuremath{\vec{\mathcal{%s}}}' % m.group(1), t)
    return re.sub(r'([A-Za-z])' + COMB_ARROW,
                  lambda m: r'\ensuremath{\vec{%s}}' % m.group(1), t)


EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
# Long Latin tokens that contain no space/hyphen: in a 1- or 2- or 3-column
# layout they cannot break and therefore overflow the column.  Adding
# \allowbreak after the separators costs nothing visually and fixes the gate.
LONGTOK_RE = re.compile(r'(?<![\w\\{}])([A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z0-9]+){2,})(?![\w{}])')




# ==== merged: breaklong patch for #28 (long slash/comma runs) ====
MARK = r'\hspace{0pt}'

# ---- the one constant that changes -------------------------------------
# Tokens at least this many characters long get breakpoints at their
# separators.  Chosen by sweep on the real pipeline; the FAIL->PASS boundary
# is between 18 and 17.

MIN_RUN_LEN = 17

# A run of alphanumerics joined by any separator the existing `ins()` already
# treats as breakable.  This is the widened LONGTOK_RE: '/' and ',=@+' were
# added, and the {2,} block requirement became a plain + so that two-block
# runs like a/b/c (which appear constantly in these papers) are covered.

RUN_RE = re.compile(
    r'(?<![\w\\{}])'
    r'([A-Za-z0-9]+(?:[_/.,=@+\-][A-Za-z0-9]+)+)'
    r'(?![\w{}])')



def _dispw(s):
    """Display width: East-Asian wide glyphs count 2, others 1."""
    w = 0
    for ch in s:
        o = ord(ch)
        if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
                or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
                or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
                or 0xFFE0 <= o <= 0xFFE6 or 0x3000 <= o <= 0x303F):
            w += 2
        else:
            w += 1
    return w



def breaklong(t):
    """Long URLs / e-mails / identifiers / acronym runs may break at separators.

    Uses a zero-width breakpoint (\\hspace{0pt}) rather than \\allowbreak:
    \\allowbreak only helps where TeX already sees a feasible breakpoint
    (glue), while a bare e-mail address is one word with NO breakpoint at all,
    which is exactly what overflows a narrow column -- author bios in the
    3-column CACM layout overflowed by up to 95pt.

    RULE 3 (narrow-column overflow, #28) is separate from rules 1-2 and only
    widens what they already do; it never narrows them.
    """
    def ins(s):
        # UNCHANGED from the original rule -- existing behaviour is preserved.
        # NOTE: the replacement must be a CALLABLE, not r'\1' + MARK: a string
        # replacement is parsed as a regex template, and '\hspace' is then read
        # as the unknown escape '\h' -> "re.error: bad escape \h".
        return re.sub(r'([_/(),=@])', lambda m: m.group(1) + MARK, s)

    def ins_run(s):
        # Like ins(), but '.' is INCLUDED.
        #
        # This is load-bearing and cost a whole debugging round: the pre-existing
        # ins() breaks only at [_/(),=@], which is correct for it -- dot-joined
        # tokens were LONGTOK_RE's job, and that rule is gated at >=24 chars.
        # So 'cards.withgoogle.com' (20 chars) matched RUN_TOK but received NO
        # marker, and #28 stayed at 11.96426pt (gate 8.0) on exactly that token.
        # A rule that claims dot-runs must be able to break at the dot.
        return re.sub(r'([_/.,=@])', lambda m: m.group(1) + MARK, s)

    t = re.sub(r'(https?://[^\s\\{}]+)', lambda m: ins(m.group(0)), t)
    t = EMAIL_RE.sub(lambda m: ins(m.group(0)), t)
    # existing narrow rule (underscore/dot identifiers), unchanged
    t = LONGTOK_RE.sub(
        lambda m: ins(m.group(1)) if len(m.group(1)) >= 24 else m.group(1), t)
    # NEW: slash/dot/comma/equals joined runs, gated on display width
    t = RUN_RE.sub(
        lambda m: ins_run(m.group(1)) if _dispw(m.group(1)) >= MIN_RUN_LEN
        else m.group(1), t)
    return t




# ============================================ display-math equation numbers
# DEFECT #01 (Konings 2016): an author wrote the equation number INSIDE the
# display math as a width command:
#
#     $$d(x_i, x_l) = \sqrt{...} \qquad (2)$$
#
# pandoc renders that as \[...\qquad (2)\].  \qquad is 2em of REAL WIDTH, so
# the display box is widened by the whole equation plus 2em plus the number.
# In a 2-column text block that cannot fit and the box bursts out of the
# column: measured 33.19702pt overfull (Overfull \hbox ... at line 274).
#
# REPAIR (variant B): take the trailing number OUT of the math box and
# typeset it on its own line with \hfill, i.e. a right-aligned tag:
#
#     \[<math>\]
#     \hfill (2)
#
# WHY B and not the alternatives that also measured 1.0pt:
#   * A (\qquad -> \hfill inside the box) still measures 13.19702pt -> FAIL.
#     \hfill is infinitely stretchable, but it is *inside* the box, so the
#     box still has to be as wide as math + tag before the glue can shrink.
#   * C (\resizebox to \linewidth) shrinks the GLYPHS -- the equation would be
#     set at a different point size from every other equation in the paper.
#     A typographic fix must not silently resize the author's math.
#   * D (amsmath split) rewrites the equation body, i.e. it would have to
#     invent a line-break point.  That is a CONTENT change; getting it wrong
#     on some other paper's math is silently wrong output.
#   B changes neither the math nor its size: the identical math box stays
#   centred, and the number moves to the right margin, which is also how the
#   \tag{} path already behaves in every other paper.
#
# SCOPE -- deliberately narrow, because 53 of the 59 display blocks in this
# corpus use \tag{...} and those already fit; they must NOT be touched:
#   * display math only ($$...$$); inline math is never matched, so inline
#     \qquad (a legitimate spacing command) keeps working;
#   * only a TRAILING spacing-command + "(number)" group is moved, and only
#     when the spacing command is \quad/\qquad (or \hspace{...}) -- the exact
#     defect signature;
#   * \tag{} is left byte-identical.
#
# The emitted form is validated: `$$<math>$$\hfill (N)` survives
# `pandoc -f markdown+raw_tex -t latex` as `\[<math>\]\hfill (N)`, which is
# exactly variant B.  Writing the \hfill on a SEPARATE line is NOT safe -- in
# markdown a lone `\hfill (2)` line becomes paragraph text, and pandoc escapes
# it to `\textbackslash{}hfill`, which typesets a literal backslash.
EQNUM_STATS = {'moved': 0, 'left': 0, 'duplicate': 0}

# Trailing equation number, optionally preceded by a width command.
#   \qquad (2)      \quad(2)      \hspace{1em}(2)      (2)
_EQNUM_TAIL_RE = re.compile(
    r'\s*(?:\\(?:qquad|quad)\b|\\hspace\*?\{[^{}]*\})?\s*'
    r'\((\d+[a-z]?)\)\s*$')

# A trailing \tag{...} means the author already numbered it the fitting way.
_EQNUM_TAG_RE = re.compile(r'\\tag\s*\{')

# Display math: $$...$$ (single line or fenced).  Non-greedy and DOTALL so
# multi-line blocks are handled; the fence form is consumed whole.
_EQNUM_DISP_RE = re.compile(r'\$\$(.+?)\$\$', re.S)

# One output line whose TAIL is an already-placed number tag.  The tag is
# appended to the closing `$$` of its display (`$$\hfill (2)`), so the guard
# matches the END of the line, not a whole line.
_EQNUM_ONLY_RE = re.compile(r'\\hfill\s*\(\s*(\d+[a-z]?)\s*\)\s*$')


def _eqnum_split(body):
    """Return (math, number) when `body` ends in a movable number, else None.

    `\\tag{}` blocks return None: they are the fitting path and must stay
    byte-identical.
    """
    if _EQNUM_TAG_RE.search(body):
        return None
    m = _EQNUM_TAIL_RE.search(body)
    if not m:
        return None
    math = body[:m.start()].rstrip()
    if not math:
        # Nothing but a number -- moving it out would leave an empty display.
        return None
    return math, m.group(1)


def move_disp_eqnum(t):
    """Move display-math trailing equation numbers out of the math box.

    Unconditional counter (EQNUM_STATS) so that a silent no-op -- the failure
    mode this repo has already been bitten by -- is visible in the prep log
    as EQTAG moved=0 left=N.
    """
    def _out(m):
        body = m.group(1)
        sp = _eqnum_split(body)
        if sp is None:
            EQNUM_STATS['left'] += 1
            return m.group(0)
        math, num = sp
        EQNUM_STATS['moved'] += 1
        # \hfill on the SAME line as the closing $$, so pandoc keeps it as
        # raw LaTeX rather than a markdown paragraph.
        return '$$%s$$\\hfill (%s)' % (math, num)

    return _EQNUM_DISP_RE.sub(_out, t)


def mathify(t):
    t = _vec_arrow(t)
    t = move_disp_eqnum(t)
    for k, v in MATHMAP.items():
        t = t.replace(k, v)
    t = _MA_RE.sub(_map_ma, t)
    t = _SUB_RE.sub(_map_sub, t)
    t = _SUP_RE.sub(_map_sup, t)
    t = _DOT_RE.sub(lambda m: r'\ensuremath{\dot{%s}}' % m.group(1), t)
    t = _DDOT_RE.sub(lambda m: r'\ensuremath{\ddot{%s}}' % m.group(1), t)
    return breaklong(t)


def mathify_plain(t):
    """Inside code blocks: Unicode math letters/sub-superscripts -> base
    characters (verbatim cannot use commands)."""
    t = _MA_RE.sub(lambda m: _map_ma(m, True), t)
    t = _SUB_RE.sub(_base_char, t)
    t = _SUP_RE.sub(_base_char, t)
    return t.replace('\u0307', '').replace('\u0308', '')


# ================================================= inline code spans (`...`)
# ROOT CAUSE of the \\texttt double-escaping overflow (#03 / #16).
#
# prep.py emits MARKDOWN; pandoc later converts it to LaTeX.  mathify() used to
# run over the WHOLE line, including the inside of backtick code spans, turning
# a purely-textual span into one that carries a raw LaTeX command:
#
#     markdown   `\u03b6`                     (zeta, no backslash at all)
#     mathify()  `\\ensuremath{\\zeta}`        (raw LaTeX smuggled into a span)
#     pandoc     \\texttt{\\textbackslash{}ensuremath\\{\\textbackslash{}zeta\\}}
#
# pandoc necessarily escapes the content of a markdown code span, because in
# that context a backslash is ordinary *code text* rather than markup.  The
# command therefore degrades into literal words AND fuses with its neighbours
# into one unbreakable monospace token, roughly 8x its intended width.
# Measured: 36.9pt overfull on #03 (4 spans) and 127.4pt on #16 (8 spans,
# 55 \\textbackslash{} occurrences) against a gate limit of 8pt.
#
# THE FIX.  A markdown code span is the wrong container once its body needs a
# LaTeX command, so prep.py stops emitting one: it emits a RAW '\\texttt{...}'
# instead, with the body already escaped the way \\texttt needs.  pandoc treats
# '\\texttt{...}' as raw inline LaTeX under +raw_tex and passes it through
# byte for byte (verified), so nothing is escaped a second time.
#
# Bodies are still made breakable, because pandoc used to render every space as
# '\\ ' and every '_' as '\\_', none of which is a TeX breakpoint:
#   * '_' '%' '&' '#' '$' are escaped (\\texttt is not verbatim, so these are
#     live characters there),
#   * U+200B is inserted inside long unspaced runs -- pandoc is out of the
#     picture now, so it is emitted directly as '\\hspace{0pt}', which is a
#     feasible breakpoint inside \\texttt (verified by compile: an 85-char
#     \\texttt run overflowed by 258pt without it and by 0pt with it),
#   * '\\allowbreak' is added after separators, matching the cell idiom.
BT = chr(96)
CODE_SPAN_RE = re.compile(BT + '([^' + BT + ']+)' + BT)
# A run inside a code span worth giving breakpoints to: a long unspaced
# alphanumeric/identifier soup.  Spaces already break; this targets the rest.
_CODE_RUN_RE = re.compile(r'[0-9A-Za-z_./\\{}=+<>|^~%$#-]{8,}')
ZERO_BREAK = r'\hspace{0pt}'
# Placeholders for the breakpoints so that the escaping / command-mapping
# stages cannot see them.  Private-use code points: they cannot occur in the
# source markdown and are not matched by any of the escapes below.
ZW_PLACEHOLDER = '\uf8e0'
AB_PLACEHOLDER = '\uf8e1'


# Characters next to which a breakpoint MUST NOT be placed: '^' and '_' are
# math-only syntax, '{' '}' are group boundaries, '\\' starts a command name.
# A breakpoint in front of '^' produced "! Missing $ inserted" (measured on
# #16 body line 155), and one inside '\\ensuremath' produced
# "Undefined control sequence" via '\\ensur\\hspace{0pt}emath'.
_NOBREAK_NEIGHBOUR = set(r'^_{}\\')


def _zwsp_run(tok, mark=ZERO_BREAK, every=6):
    """Insert a breakpoint every `every` characters, only at SAFE positions.

    A position is safe only when both neighbours are ordinary characters.
    This is the rule that keeps TeX syntax intact: no breakpoint may land
    before/after '^', '_', '{', '}' or a backslash.
    """
    out = []
    for i, ch in enumerate(tok):
        if i and i % every == 0:
            prev = tok[i - 1]
            if prev not in _NOBREAK_NEIGHBOUR and ch not in _NOBREAK_NEIGHBOUR:
                out.append(mark)
        out.append(ch)
    return ''.join(out)


def code_span_body(s):
    """Escape a code-span body for use inside a raw \\texttt{...}.

    Correct order, and each step exists because doing it in another order was
    measured to break:
      1. breakpoints go in FIRST, on the raw text, behind placeholders.
         Anywhere later and the run-splitter chews on the escapes and commands
         we just produced -- '\\ensuremath' became '\\ensur\\hspace{0pt}emath'
         and '\\_' became '\\\\hspace{0pt}_'.
      2. escape the characters that are live inside \\texttt.  Before the
         command step, or our own command braces get escaped into
         '\\ensuremath\\{', which is the original defect wearing a new hat.
      3. base characters for Unicode math letters / sub- / superscripts.
      4. command form for symbols with no monospace glyph: a bare U+2299 in
         Consolas does not render (measured MISS=1), '\\ensuremath{\\odot}' does.
      5. the placeholders become real breakpoint commands.
    """
    # 1. breakpoints, marked on the raw text.  Both insertion passes respect
    #    _NOBREAK_NEIGHBOUR: a breakpoint may only sit between two ordinary
    #    characters, never against '^' '_' '{' '}' or a backslash.
    s = _CODE_RUN_RE.sub(lambda m: _zwsp_run(m.group(0), ZW_PLACEHOLDER), s)
    s = re.sub(r'([/(),=])(?=[^' + re.escape(''.join(sorted(_NOBREAK_NEIGHBOUR)))
               + r']|$)', r'\1' + AB_PLACEHOLDER, s)
    # 2. escape the live characters.  '^' and '~' are the ones that bite:
    #    inside \texttt they are still math/live characters, and a bare '^'
    #    raises "Missing $ inserted" (measured, body line 155 of #16).
    s = re.sub(r'([_%&#$])', r'\\\1', s)
    s = re.sub(r'([{}])', r'\\\1', s)
    s = s.replace('^', r'\^{}').replace('~', r'\textasciitilde{}')
    # 3. base characters
    s = mathify_plain(s)
    # 4. renderable command form where the monospace font has no glyph
    for k, v in MATHMAP.items():
        if len(k) == 1 and k in s:
            s = s.replace(k, v)
    # 5. placeholders -> real commands
    return s.replace(AB_PLACEHOLDER, r'\allowbreak ').replace(
        ZW_PLACEHOLDER, ZERO_BREAK)


# Placeholder carrying a code span across the mathify() pass.  Private-use
# sentinels rather than backticks or control chars, so that mathify() cannot
# rewrite it and the str.replace chains inside it cannot corrupt it.
PLACEHOLDER_TMPL = '\uf8f0%d\uf8f1'
PLACEHOLDER_RE = re.compile('\uf8f0(\\d+)\uf8f1')

# Self-validation counters.  This defect class has already produced one SILENT
# no-op in this repo (a sentinel-value mismatch meant the post-processing never
# ran, and the gate readings were bit-for-bit identical to the unpatched run --
# easy to misread as "the theory was wrong" rather than "the code never ran").
# prep.py therefore reports what it actually did, and _audit asserts on it.
SPAN_STATS = {'spans': 0, 'spans_with_break': 0, 'cmd_spans': 0}


def _texttt(s):
    """A raw \\texttt{} with an already-escaped body."""
    body = code_span_body(s)
    SPAN_STATS['spans'] += 1
    if ZERO_BREAK in body or r'\allowbreak' in body:
        SPAN_STATS['spans_with_break'] += 1
    if r'\ensuremath' in body or r'\text' in body:
        SPAN_STATS['cmd_spans'] += 1
    return r'\texttt{' + body + '}'


def mathify_line(t):
    """mathify() for a whole body line, with inline code spans shielded.

    ORDER IS CRITICAL: the spans are taken out of the text BEFORE mathify()
    runs, otherwise mathify() has already rewritten a span body into a LaTeX
    command and code_span_body() would only be mapping the wreckage (that
    mistake is observable: a span holding zeta came out as the literal words
    "ensuremath{zeta}").
    """
    spans = []

    def _stash(m):
        spans.append(m.group(1))
        return PLACEHOLDER_TMPL % (len(spans) - 1)

    t = CODE_SPAN_RE.sub(_stash, t)
    t = mathify(t)

    def _restore(m):
        i = int(m.group(1))
        body = spans[i] if 0 <= i < len(spans) else ''
        return _texttt(body)

    return PLACEHOLDER_RE.sub(_restore, t)


# ============================================================ cells
def cellfmt(t):
    """Inline typesetting for a table cell. Escape order is **critical**:
    escape raw ^ ~ first,
    then mathify (the ^ produced by mathify is a math superscript and must
    not be touched again)."""
    t = re.sub(r'<br\s*/?>', r' \newline ', t, flags=re.I)
    t = t.strip()
    t = t.replace('^', r'\^{}').replace('~', r'\textasciitilde{}')
    t = re.sub(r'<sub>(.*?)</sub>', lambda m: '\x01' + m.group(1) + '\x02', t)
    # Shield inline code spans BEFORE mathify(): otherwise mathify() rewrites a
    # span body into a LaTeX command, and the raw \\texttt{} built below would
    # then carry that command as literal text (the #03/#16 defect).
    _cells_spans = []

    def _cell_stash(m):
        _cells_spans.append(m.group(1))
        return PLACEHOLDER_TMPL % (len(_cells_spans) - 1)

    t = CODE_SPAN_RE.sub(_cell_stash, t)
    # Single-backslash textsubscript: it must be written as '\\textsubscript{' so that it    # means "backslash + t"; a plain \t would be parsed as TAB.
    t = mathify(t)
    t = t.replace('&', r'\&').replace('%', r'\%').replace('#', r'\#')
    t = t.replace('$', r'\$').replace('_', r'\_')
    # textsubscript needs a single backslash: writing '\\textsubscript{'
    # yields the intended command; a bare \t would be parsed as TAB.
    t = t.replace('\x01', '\\textsubscript{').replace('\x02', '}')
    t = t.replace('\x03', '\\textsuperscript{').replace('\x04', '}')
    t = re.sub(r'\*\*(.+?)\*\*', lambda m: r'\textbf{' + m.group(1) + '}', t)
    t = re.sub(r'\*([^*]+?)\*', lambda m: r'\emph{' + m.group(1) + '}', t)

    def _cell_restore(m):
        i = int(m.group(1))
        body = _cells_spans[i] if 0 <= i < len(_cells_spans) else ''
        return _texttt(body)

    t = PLACEHOLDER_RE.sub(_cell_restore, t)
    # Long digit/letter runs (e.g. 17,280/L) cannot break inside an X column
    # -> allow a break at the separators
    t = re.sub(r'(?<=[0-9A-Za-z])([,/])(?=[0-9A-Za-z])', r'\1\\allowbreak ', t)
    t = re.sub(r'(?<=[0-9A-Za-z])([_\.\-\+/])(?=[0-9A-Za-z])', r'\1\\allowbreak ', t)
    return t


# ============================================================ tables
SEP_CELL = re.compile(r'^:?-{2,}:?$')


def split_md_table(rows):
    """markdown rows -> (header, body): detect the separator row by
    **content**; never drop data rows.

    !! The original hardcoded cells[2:] !! it assumed row 2 is always the
    separator ->
    a table with NO separator row lost its first real data row (measured:
    an acronym table lost its CID row).
    """
    cells = [[c.strip() for c in r.strip().strip('|').split('|')] for r in rows]
    sep_idx = [i for i, r in enumerate(cells) if r and all(SEP_CELL.match(c or '-') for c in r)]
    if sep_idx:
        first = sep_idx[0]
        header = cells[first - 1] if first > 0 else (cells[1] if len(cells) > 1 else cells[0])
        body = [r for i, r in enumerate(cells)
                if (i != first - 1 or first == 0) and i not in sep_idx]
    else:
        header, body = cells[0], cells[1:]
    return header, body


def col_specs(header, body):
    """Infer each column type from its content: short columns use natural
    width l/c (balanced look), long columns use X to fill the

    Returns (spec, kinds); if no column is X, spec=None (tabularx needs at
    least one X).
    """
    ncol = len(header)
    kinds = []
    for c in range(ncol):
        vals = [r[c] for r in [header] + body if c < len(r)]
        data = [v for v in vals[1:] if v]
        maxlen = max((len(v) for v in vals), default=0)
        numeric = bool(data) and all(
            re.fullmatch(r'[-\u2212\u2013\u2014 0-9.,%\u00b1+/]*', v) for v in data)
        kinds.append('c' if (numeric and maxlen <= 12) else ('l' if maxlen <= 12 else 'X'))
    if 'X' not in kinds:
        return None, kinds
    return '@{}' + ''.join(XX if k == 'X' else k for k in kinds) + '@{}', kinds


def _estimate_cols(header, body, ncol):
    cols = []
    for c in range(ncol):
        w = 0.0
        for r in [header] + body:
            if c < len(r):
                t = r[c]
                cjk = sum(1 for ch in t if '\u4e00' <= ch <= '\u9fff')
                w = max(w, cjk * CJK_PT_PER_CHAR + (len(t) - cjk) * LAT_PT_PER_CHAR)
        cols.append(w)
    return cols


def _solve(cols, avail, kinds, pad=TABCOLSEP_PAD):
    """tabularx semantics: available width = sum over non-X columns of
    (natural width + padding) + the widths of k X columns.

    X columns share the remainder, and an X column itself also carries
    2*tabcolsep of padding, so its usable content width
    = share - pad. Skipping this underestimates the requirement (measured:
    0.65pt deteriorated to 16.1pt).
    Each X column needs content >= its own estimate to avoid overflow. Set
    the largest estimates as X first (X compresses,
    l/c does not) and take the smallest feasible k. Returns (spec, k) or
    (None, None) meaning unsolvable at this width.
    """
    nc = len(cols)
    order = sorted(range(nc), key=lambda c: cols[c], reverse=True)
    for k in range(1, nc + 1):
        xs = set(order[:k])
        nat = sum(cols[j] + pad for j in range(nc) if j not in xs)
        share = (avail - nat) / float(k)
        content = share - pad
        if all(content + 1e-6 >= cols[j] for j in xs):
            sp = [XX if j in xs else kinds[j] for j in range(nc)]
            return '@{}' + ''.join(sp) + '@{}', k
    return None, None


def looks_like_art(rows):
    """Decide whether a block of pipe rows is an ASCII-art diagram (e.g. a
    TPDU field layout).

    Traits: >=2 +---+ rules, many |, very little CJK (diagrams are mostly
    English/digits).
    Such a block is **not** a table; converting it to tabularx always
    misaligns."""
    blk = '\n'.join(rows)
    rules = len(re.findall(r'\+[-+]{3,}\+', blk))
    pipes = blk.count('|')
    cjk = len(re.findall(r'[\u4e00-\u9fff]', blk))
    return rules >= 2 and pipes >= 6 and cjk < 40


def emit_art(rows):
    out = [r'\begin{Verbatim}[fontsize=\scriptsize,breaklines=true,breakanywhere=true]']
    for a in rows:
        out.append(a.strip('|').rstrip('|').rstrip())
    out.append(r'\end{Verbatim}')
    return '\n'.join(out)


_HY = r'\sloppy\hyphenpenalty=50\exhyphenpenalty=50\relax'


def tabenv(tabno, taben, titlelines, rows, mode, tabimg, avail, stats, tabdir=None):
    """Build a table environment. A **vector-recreated table returns
    immediately** and is never modified by any layout rule (R-T1)."""
    ti = tabimg(tabno)
    if ti:
        stats['tabimgs'] += 1
        captext = ('\u8868 %s\uff08%s\uff09' % (tabno, taben)) if (taben or '').strip() else ('\u8868 %s' % tabno)
        if titlelines:
            captext += '. ' + ' '.join(titlelines)
        # Escape '^' and '~' BEFORE mathify(), exactly as cellfmt() does (L769).
        # WHY: a bare '^' is only legal in math mode, so a caption such as
        # "#11 Table 2 ... [2(E^-1 - C_L)]" produced
        #     ! Missing $ inserted.   at body.tex l.280
        # and the whole run failed.  The hand-built table path already escaped it
        # via cellfmt(); this vector-table path used mathify() alone and did not.
        # mathify() must run AFTER the escaping so that '^' cannot be turned into
        # a math superscript by the mapper.
        captext = captext.replace('^', r'\^{}').replace('~', r'\textasciitilde{}')
        cap = mathify(captext)
        cap = (cap.replace('&', r'\&').replace('%', r'\%').replace('#', r'\#')
                  .replace('_', r'\_'))
        cap = cap.replace(r'\textbf', '').replace(r'\emph', '')
        # Width: source table width / page width > 0.62 -> span both columns
        pw = ti.get('pagew') or 612.0
        ratio = (ti.get('w') or 300) / pw
        star = ratio > 0.62
        env = 'table*' if (star and mode in ('conf', 'tri')) else 'table'
        width = r'\textwidth' if (star and mode in ('conf', 'tri')) else r'\columnwidth'
        return (r'\begin{%s}[!htbp]\centering' '\n'
                r'\includegraphics[width=%s,height=0.85\textheight,keepaspectratio]{%s}' '\n'
                r'\captionsetup{labelformat=empty}\caption{%s}' '\n'
                r'\end{%s}' % (env, width, _subrel(ti['file'], tabdir), cap, env))
    # ---- manual layout branch (used only when the original table cannot be located) ----
    # WHY a table is hand-built is recorded by the caller's _tab wrapper (see
    # convert()): landing here while tables.json HAS an ok record for this number
    # is a DEFECT, whereas having no record at all is a legitimate fallback (the
    # source table genuinely cannot be recreated, e.g. DESIRE6G's three no-rules
    # tables).  Before that distinction existed the pipeline printed a NOTE and
    # still reported "7/7 ok" and shipped -- 7 vector tables silently fell back.
    # The R1 gate in runpaper.py reads stats['handmade_despite_ok'] and
    # stats['handmade_no_record'].
    header, body = split_md_table(rows)
    ncol = max([len(header)] + [len(r) for r in body])
    fixed = [list(r) + [''] * (ncol - len(r)) for r in [header] + body]
    header, body = fixed[0], fixed[1:]
    captext = ('\u8868 %s\uff08%s\uff09' % (tabno, taben)) if (taben or '').strip() else ('\u8868 %s' % tabno)
    if titlelines:
        captext += '. ' + ' '.join(titlelines)
    cap = cellfmt(captext).replace(r'\textbf', '').replace(r'\emph', '')
    # Long-token detection: a cell containing a >14 char token with no space
    # necessarily overflows a narrow two-column layout (measured 47.7pt)
    # -> upgrade to full width (measured improvement to 4.3pt)
    longtok = any(len(t) > 14 for r in [header] + body for c in r for t in c.split())
    star = (ncol >= 6 or longtok)
    env = 'table*' if (star and mode in ('conf', 'tri')) else 'table'
    width = r'\textwidth' if (star and mode in ('conf', 'tri')) else r'\columnwidth'
    fsize = r'\scriptsize' if (ncol >= 6 or longtok) else r'\footnotesize'
    spec, kinds = col_specs(header, body)
    stats['tabenvs'] += 1
    if spec is None:
        # All columns are short: candidate natural-width tabular.
        # Natural width MUST be estimated: a multi-column CJK table without
        # a width constraint always overflows (measured 23.8pt) -> if the
        # estimate exceeds the available width, switch to X columns instead.
        cols = _estimate_cols(header, body, ncol)
        if sum(cols) + TABCOLSEP_PAD * ncol > avail:
            # Overflow fallback. Historical counter-examples:
            #   all columns split evenly as X -> 38.2pt each < 45pt needed
            #     (overflow 6.9pt)
            #   first columns take full natural width -> X columns left with
            #     only 20.1pt (overflow 8.7pt)
            # First solve for a feasible X set under real tabularx semantics;
            # if there is no solution at this width, the WIDTH itself is too
            # small -> upgrade to scriptsize and/or full-width table*,
            # rather than forcing column widths.
            spec, _k = _solve(cols, avail, kinds)
            if spec is None:
                scaled = [c * 0.875 for c in cols]      # scriptsize 7pt / footnotesize 8pt
                spec, _k = _solve(scaled, avail, kinds)
                if spec is not None:
                    fsize = r'\scriptsize'
                else:
                    if mode in ('conf', 'tri'):        # multi-column page:
                                                        # textwidth ~= 2*columnwidth
                                                        # (conf) or 3* (tri);
                                                        # a wide float still uses
                                                        # the full \textwidth
                        star = True; env = 'table*'; width = r'\textwidth'
                    spec, _k = _solve(scaled, avail * 2.0 + COLSEP, kinds)
                    if spec is None:
                        spec, _k = _solve(scaled, avail * 2.0 + COLSEP, ['X'] * ncol)
                    if spec is None:                    # extreme fallback: all X
                        spec = '@{}' + XX * ncol + '@{}'
        else:
            spec = '@{}' + ''.join(kinds) + '@{}'
            env = 'table'
            width = None
    if width is None:
        lines_out = [r'\begin{%s}[!t]\centering' % env + fsize, _HY,
                     r'\textbf{%s}\par\vspace{4pt}' % cap,
                     r'\begin{tabular}{%s}' % spec, r'\toprule']
    else:
        lines_out = [r'\begin{%s}[!t]\centering' % env + fsize, _HY,
                     r'\textbf{%s}\par\vspace{4pt}' % cap,
                     r'\begin{tabularx}{%s}{%s}' % (width, spec), r'\toprule']
    lines_out.append(' & '.join(cellfmt(c) for c in header) + r' \\')
    lines_out.append(r'\midrule')
    for r in body:
        lines_out.append(' & '.join(cellfmt(c) for c in r) + r' \\')
    lines_out.append(r'\bottomrule')
    lines_out.append((r'\end{tabular}' if width is None else r'\end{tabularx}')
                     + r'\end{%s}' % env)
    return '\n'.join(lines_out)


# ============================================================ figures
# Figure caption styles (all must be supported; each was found in practice):
#   Fig. 1. / Fig.1. (no space) / FIGURE 1 / figure 1 | / figure 1: /
#   **figure N. description**   (bold spans the whole caption)
#   **figure N.** description   (bold wraps the LABEL only)
#   appendix letter numbering: figure C.3.1.1-1
#
# The "bold closes before the description" form used to fall through all three
# regexes and fail SILENTLY: the line stayed plain text, no figure environment
# was emitted, and the figure simply never appeared (only the downstream
# figenv-count gate noticed).  RE_FIG_D covers that case explicitly.
RE_FIG_A = re.compile(r'^>?\s*\*\*\u56fe\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*'
                      r'(?:[\uff08(][^\uff09)]*[\uff09)])?\s*[.\u3002]?\s*\*\*\s*(.*)$')
RE_FIG_B = re.compile(r'^>\s*\*{0,2}\u56fe\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*'
                      r'(?:[\uff08(][^\uff09)]*[\uff09)])?\s*[.\u3002]?\s*\*{0,2}\s*(.*)$')
RE_FIG_C = re.compile(r'^\*\*\u56fe\s*([0-9A-Za-z][0-9A-Za-z.\-]*?)\s*[.\u3002\uff1a:|\uff5c]\s*(.+?)\*\*\s*$')
# Bold closes right after the label; the description that follows is PLAIN:
#   **\u56fe 1.** \u8bf4\u660e\u6587\u5b57   (also with \uff08...\uff09)
RE_FIG_D = re.compile(r'^\s*>?\s*\*\*\u56fe\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*'
                      r'(?:[\uff08(][^\uff09)]*[\uff09)])?\s*[.\u3002\uff1a:|\uff5c]?\s*\*\*\s*(.*)$')
RE_TAB = re.compile(r'^> \*\*\u8868\s*([A-Z0-9.\-]+)\s*'
                    r'(?:[\uff08(]([^*\uff09)]*)[\uff09)])?[.\u3002]?\*\*\s*(.*)$')

# ---------------------------------------------------------------------------
# DEFECT (blocking): table captions that name a table which ALREADY has a
# vector recreation in tables.json were silently dropped and re-typeset by
# hand, violating R1 ("tables must be vector-recreated, never hand-type-set").
#
# Root cause: the label is followed by a period that may sit INSIDE the bold
# run ("**\u8868 1.** title" -- 74% of the corpus), and/or by a parenthesised
# English label ("**\u8868 II\uff08TABLE II\uff09.** title").  None of RE_TAB..RE_TAB4 matched those, so
# tabimg() was never called, no image was emitted, and the run still reported
# success.  Measured on the corpus survey: bold_period_in 6 hits, bold_paren
# 75 hits -- i.e. the two DOMINANT styles had no handler at all.
#
# RE_TABCAP is the single unified caption matcher that covers all four styles:
#   1) > **\u8868 1.** title            period inside the bold, descid plain
#   2) > **\u8868 I\uff08TABLE I\uff09.** title     Roman + paren English label
#   3) **\u8868 1. title**          bold spans the whole caption
#   4) \uff08\u8868\uff1a2.1.-1 title\uff09      term/clause style (handled by RE_TABCAP_TERM)
#
# CRITICAL: group(1) may carry a TRAILING PERIOD ("1.") because the number
# class is greedy.  Never look the raw capture string up in tables.json --
# always pass it through _norm_tabno(), which strips the period and maps
# Roman numerals to Arabic ("I." -> "1"), exactly like the extractor keys.
#
# Ordering hazard: style 3 also matches the LABEL-ONLY line that RE_TAB4
# consumes ("**\u8868 7**" with the table body on the FOLLOWING line).  Hence RE_TABCAP is
# consulted only AFTER the rows-follow path has had its chance (see convert()).
_RE_TAB_LABEL = r'([0-9A-Za-z][0-9A-Za-z.\-]*)'
# Two tail forms, because the bold run either CLOSES after the label
#   > **\u8868 1.** desc            > **\u8868 I\uff08TABLE I\uff09.** desc
# or SPANS the whole caption, description included
#   **\u8868 1. desc**              **\u8868 1 | desc**
# The description class must exclude '*' in form (a), otherwise the greedy
# capture swallows the closing "**" of its own delimiter.
_RE_TAB_TAIL_A = r'\s*(?:[\uff08(]([^*\uff09)]*)[\uff09)])?\s*[.\u3002：:\-|\uff5c]?\s*\*\*\s*([^*]*)$'
_RE_TAB_TAIL_B = r'\s*(?:[\uff08(]([^*\uff09)]*)[\uff09)])?\s*[.\u3002：:\-|\uff5c]?\s*([^*]+?)\s*\*\*\s*$'
# One regex, five groups -- but only three SEMANTIC slots, because the label,
# the English parenthetical and the trailing separator repeat per branch:
#   1 number  2 en-a  3 desc-a  4 en-b  5 desc-b
# _tabcap_parts() below folds them back down.
RE_TABCAP = re.compile(r'^\s*>?\s*\*\*\u8868\s*' + _RE_TAB_LABEL
                       + r'(?:' + _RE_TAB_TAIL_A + '|' + _RE_TAB_TAIL_B + r')')
RE_TABCAP_EARLY = re.compile(r'^> \*\*\u8868\s*' + _RE_TAB_LABEL
                             + r'(?:' + _RE_TAB_TAIL_A + '|' + _RE_TAB_TAIL_B + r')')
# Does the caption name tables.json's own English label (e.g. "TABLE II")?
# Used ONLY as a fallback: it is prone to false positives (see _en_tabno()).
_RE_TAB_EN_LABEL = re.compile(r'TABLE\s*[:：]?\s*([0-9IVXLCivxlc][0-9A-Za-z.\-]*)')
# Trailing enumerator that is NOT part of the number: "1." / "1" / "III".
_TAB_NUM_STRIP = re.compile(r'[.\u3002]$')


def _tabcap_parts(m):
    """Fold RE_TABCAP's five groups back to (raw_id, english_label, desc)."""
    if m.group(3) is not None:
        return m.group(1), (m.group(2) or ''), m.group(3)
    return m.group(1), (m.group(4) or ''), (m.group(5) or '')


def _split_tabcap(raw, rest):
    """Decide the real table id inside a greedily-captured label.

    The label class accepts '.' and '-', so a greedy capture swallows the
    clause numbering of some journals.  Returns (number, description).

    Measured cases:
        ('1',   '')                 plain
        ('1.',  '')                 trailing period -> _norm_tabno() drops it
        ('7.3-4','')                genuine clause number, kept whole
        ('1.',  '5 \u57fa\u7ebf...')       "**\u8868 1. 5 \u57fa\u7ebf...**" -- the real number is
                                    '1', and '5' starts the description
        ('2.1.-1','')               term-style clause number, kept whole
    """
    if not rest:
        return raw, ''
    # A trailing "-<digits>" is a clause suffix only when the rest continues
    # with the description; if the rest starts with '-' it is the separator.
    m = re.match(r'^(\d+)\.\s+(.*)$', raw)
    if m:
        return m.group(1), (m.group(2) + ' ' + rest).strip()
    return raw, rest

RE_TAB2 = re.compile(r'^\*\*\u8868 ([A-Z0-9.]+)\uff08([^*]*)\uff09\.\*\*\s*(.*)$')
# Unified table-caption style (measured hit rate was once 0 for several papers):
RE_TAB3 = re.compile(r'^\s*>?\s*\*\*\u8868\s*([0-9A-Z][0-9A-Za-z.\-]*)\s*'
                     r'(?:[\uff08(]([^*\uff09)]*)[\uff09)])?\s*[.\u3002\uff1a:]\s*(.*?)\*\*\s*$')
RE_TAB4 = re.compile(r'^\s*>?\s*\*\*\u8868\s*([0-9A-Z][0-9A-Za-z.\-]*)\s*\*\*\s*$')
RE_BQH = re.compile(r'^(>\s*)#{1,6}\s*(.*)$')

RE_TABCAP_ANY = re.compile(r'^\s*>?\s*\*\*\u8868\s*[:\uff1a]?\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*'
                           r'(?:[\uff08(]([^*\uff09)]*)[\uff09)])?\s*[.\u3002\uff1a:]?\s*(.*?)\*\*\s*$')
RE_TABCAP_TERM = re.compile(r'^\s*>?\s*[\uff08(]\s*\u8868\s*[:\uff1a]\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s+'
                            r'(.*?)[\uff09)]\s*$')


def _subrel(name, subdir):
    # Relative path on the LaTeX side.
    # prep reads/writes at the working-directory root (e.g.
    # .work/20/body.md),
    # while figures/tables live in figs/ and tables/ subdirectories; LaTeX
    # also compiles from the working-directory root,
    # so the path must be figs/figN.png or tables/tab_N.pdf,
    # otherwise LaTeX reports "Unable to load picture or PDF file".
    if not subdir:
        return name
    return os.path.join(os.path.basename(os.path.abspath(subdir)), name).replace('\\', '/')


def _figm(L):
    return (RE_FIG_A.match(L) or RE_FIG_B.match(L) or RE_FIG_C.match(L)
            or RE_FIG_D.match(L))


def figenv(n, captext, figinfo, mode, stats, figsdir=None):
    info = figinfo.get(n, {})
    f = _subrel('fig%s.png' % n, figsdir) if str(n) in figinfo else None
    star = info.get('fullwidth', False) and mode in ('conf', 'tri')
    env = 'figure*' if star else 'figure'
    width = r'0.95\textwidth' if star else r'0.95\columnwidth'
    captext = re.sub(r'\*+\s*$', '', (captext or '').strip())
    cap = ('\u56fe %s. %s' % (n, captext)).strip()
    cap = cap.replace('%', r'\%').replace('&', r'\&').replace('#', r'\#').replace('_', r'\_')
    cap = re.sub(r'\*([^*]+)\*', r'\1', cap)
    cap = mathify(cap)
    if f:
        stats['figenvs'] += 1
        # CAPTION POSITION -- the caption is emitted BEFORE the graphic.
        #
        # Why not \captionsetup[figure]{position=top}?  That key does NOT move
        # the caption relative to the figure content.  caption3 uses `position`
        # only to decide the *internal* layout of the caption block (how the
        # label and the following lines are justified) and to swap
        # \abovecaptionskip / \belowcaptionskip INSIDE the caption box.  The
        # vertical order of caption vs. graphic is fixed purely by the order in
        # which \caption and \includegraphics are executed inside the float.
        #
        # Verified locally with caption v3.6b / caption3 v2.3b and tectonic:
        #   A) preamble-level \captionsetup[figure]{position=top}
        #      -> rule at y=86.9..146.9, caption text at y=150.1  => BELOW
        #   B) \captionsetup[figure]{position=top} inside the figure
        #      -> rule at y=234.1..294.1, caption text at y=297.3 => BELOW
        #   C) \caption before the graphic
        #      -> rule at y=397.2..457.2, caption text at y=380.9 => ABOVE (ok)
        #
        # Publishing convention varies per venue, so honour the original: the
        # extractor records `caption_pos` ('top'/'bottom') per figure; default
        # is 'bottom' (LaTeX's normal behaviour).
        cap_pos = str(info.get('caption_pos', 'bottom') or 'bottom').lower()
        # callers (and older tests) may pass a stats dict without these keys
        _k = 'cap_top' if cap_pos == 'top' else 'cap_bottom'
        stats[_k] = stats.get(_k, 0) + 1
        graphic = (r'\includegraphics[width=%s,height=0.85\textheight,'
                   r'keepaspectratio]{%s}' % (width, f))
        caption = r'\captionsetup{labelformat=empty}\caption{%s}' % cap
        if cap_pos == 'top':
            inner = caption + '\n' + graphic
        else:
            inner = graphic + '\n' + caption
        return (r'\begin{%s}[!htbp]\centering' '\n' % env) + inner + ('\n' + r'\end{%s}' % env)
    return None


# ============================================================ main flow
def convert(src, figinfo, tabinfo, mode, avail, stats, figsdir=None, tabdir=None):
    lines = src.split('\n')
    n = len(lines)
    outl, i, in_fence = [], 0, False

    def _fig(*a):
        return figenv(a[0], a[1], a[2], a[3], a[4], figsdir=figsdir)

    def tabimg(no):
        # Normalise FIRST: group(1) of the caption regexes greedily swallows a
        # trailing period ("1.") and journals keep Roman numerals ("I"/"II"),
        # while tables.json is keyed Arabic-without-period ("1"/"2").  Looking
        # up the raw capture string is the defect that silently dropped every
        # already-recreated vector table.
        v = tabinfo.get(_norm_tabno(str(no)))
        if isinstance(v, dict) and v.get('status') == 'ok' and v.get('file'):
            return v
        return None

    def tabcap(L):
        """Unified table-caption matcher -> (id, en_label, desc) or None.

        Tries the Chinese id first; only if that misses tables.json does it
        fall back to the English "(TABLE N)" label, which is a guess.
        """
        m = RE_TABCAP.match(L)
        if not m:
            return None
        _raw, _en_raw, _desc_raw = _tabcap_parts(m)
        no, desc = _split_tabcap(_raw, _desc_raw.strip())
        no = _norm_tabno(no)
        en = _en_raw.strip()
        if tabimg(no) is None and en:
            alt = _en_tabno(en)
            if alt and tabimg(alt) is not None:
                no = alt
        return (no, en, desc)

    def _tab(*a):
        """tabenv() wrapper that records WHY a table ended up hand-built.

        The classification is the basis of the R1 gate (runpaper.py):
          handmade_despite_ok -- tables.json has an ok record for this number, so
              a vector copy EXISTS, yet tabenv() took the manual branch.  That is
              a defect (number normalisation / body-skip), never a valid fallback.
          handmade_no_record  -- no ok record for this number: the source table
              could not be extracted, so hand-built LaTeX is the only option and
              is allowed (reported as INFO, not a failure).
        """
        res = tabenv(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], tabdir=tabdir)
        if a[5](a[0]) is None:            # a[5] is tabimg: not embedded as vector
            no = _norm_tabno(str(a[0]))
            rec = tabinfo.get(no) if isinstance(tabinfo, dict) else None
            if isinstance(rec, dict) and rec.get('status') == 'ok':
                stats.setdefault('handmade_despite_ok', []).append(str(a[0]))
            else:
                stats.setdefault('handmade_no_record', []).append(str(a[0]))
        return res

    def collect_pipe(j):
        rows = []
        while j < n and lines[j].lstrip().lstrip('>').lstrip().startswith('|'):
            s = lines[j]
            rows.append(s.lstrip().lstrip('>').lstrip() if s.lstrip().startswith('>') else s)
            j += 1
        return rows, j

    def _after_blank(k):
        """Index of the first non-blank / non-blockquote-marker line after k."""
        j = k + 1
        while j < n and lines[j].strip() in ('', '>', '> '):
            j += 1
        return j

    def _skip_tab_body(k):
        """Index just past every markdown body that belongs to table at line k.

        Walks blank lines, fenced code blocks and *all* pipe-table runs (not
        just the first: the markdown splits one table into several pipe blocks
        with prose in between, and skipping only the first leaves an orphan
        tabularx behind -- which is exactly what the R1 gate then reports).
        """
        j = k + 1
        while j < n:
            t = lines[j].strip()
            if not t:
                j += 1; continue
            if t.startswith('```'):
                j += 1
                while j < n and not lines[j].strip().startswith('```'):
                    j += 1
                j += 1; continue
            if lines[j].lstrip().lstrip('>').lstrip().startswith('|'):
                while j < n and lines[j].lstrip().lstrip('>').lstrip().startswith('|'):
                    j += 1
                continue
            break
        return j

    while i < n:
        L = lines[i]
        ls = L.lstrip()
        # Code-fence tracking: inside ``` / ~~~ only do plain character
        # mapping
        if ls.startswith('```') or ls.startswith('~~~'):
            in_fence = not in_fence
            outl.append(L); i += 1
            continue
        if in_fence:
            outl.append(mathify_plain(L)); i += 1
            continue
        # Standalone --- rule: pad with blank lines (prevents pandoc from
        # misparsing it as a multi-line table separator / Setext heading)
        if re.match(r'^-{3,}\s*$', L):
            if outl and outl[-1].strip() != '':
                outl.append('')
            outl.append(L)
            i += 1
            if i < n and lines[i].strip() != '' and not re.match(r'^-{3,}\s*$', lines[i]):
                outl.append('')
            continue
        # Level-4+ headings -> bold paragraph (pandoc emits
        # \paragraph/\subparagraph which do not work in narrow columns)
        m = re.match(r'^#{4,6}\s+(.*)$', L)
        if m:
            outl.append('**%s**' % m.group(1).strip()); i += 1
            continue
        m = RE_BQH.match(L)
        if m:
            outl.append('%s**%s**' % (m.group(1), m.group(2))); i += 1
            continue
        m = _figm(L)
        if m:
            env = _fig(m.group(1).rstrip('.'), m.group(2).strip(), figinfo, mode, stats)
            outl.append(env if env else mathify_line(L))
            i += 1
            continue

        # ---- Vector-recreated tables first: on a hit, insert the image and
        # skip **all** interleaved table bodies of that table in the md ----
        # (skipping only the first block leaves an orphan tabularx behind)
        #
        # `_vec_early` is deliberately narrow: it fires only when the caption
        # is on the "> **\u8868 N...**" form prep itself emits upstream.  The bare
        # "**\u8868 N**"-with-rows-on-the-NEXT-line form must keep going to RE_TAB4
        # further down, otherwise the table body would be swallowed.
        _mt = RE_TABCAP_EARLY.match(L) or None
        if _mt is None:
            _mt2 = RE_TABCAP_TERM.match(L)
            if _mt2 and tabimg(_norm_tabno(_mt2.group(1))):
                no2 = _norm_tabno(_mt2.group(1))
                outl.append(_tab(no2, '', [_mt2.group(2).strip()], [], mode, tabimg, avail, stats))
                i = _skip_tab_body(i)
                continue
        if _mt is not None:
            _cap = tabcap(L)
            _cap = _cap if (_cap and tabimg(_cap[0])) else None
            if _cap:
                no, en, desc = _cap
                outl.append(_tab(no, en, [desc] if desc else [],
                                 [], mode, tabimg, avail, stats))
                i = _skip_tab_body(i)
                continue

        # ---- Remaining table-caption styles ----
        m = RE_TAB.match(L)
        if m:
            j = i + 1
            titlelines = []
            while j < n and lines[j].strip() in ('>', '> '):
                j += 1
            while j < n and re.match(r'^> \*\*[^*]+\*\*\s*$', lines[j]):
                titlelines.append(re.match(r'^> \*\*([^*]+)\*\*\s*$', lines[j]).group(1).strip())
                j += 1
                while j < n and lines[j].strip() in ('>', '> '):
                    j += 1
            while j < n and lines[j].strip() == '':
                j += 1
            rows, j = collect_pipe(j)
            if rows:
                extra = titlelines + ([m.group(3).strip()] if m.group(3).strip() else [])
                if looks_like_art(rows):
                    outl.append(emit_art(rows))
                else:
                    outl.append(_tab(m.group(1), m.group(2), extra, rows, mode, tabimg, avail, stats))
                i = j
                continue
            outl.append(mathify_line(L)); i += 1
            continue
        m = RE_TAB2.match(L)
        if m:
            j = i + 1
            while j < n and lines[j].strip() == '':
                j += 1
            rows, j = collect_pipe(j)
            if rows:
                if looks_like_art(rows):
                    outl.append(emit_art(rows))
                else:
                    outl.append(_tab(m.group(1), m.group(2), [m.group(3).strip()], rows,
                                       mode, tabimg, avail, stats))
                i = j
                continue
            outl.append(mathify_line(L)); i += 1
            continue
        m = RE_TAB3.match(L)
        if m:
            j = i + 1
            while j < n and lines[j].strip() in ('', '>', '> '):
                j += 1
            rows, j = collect_pipe(j)
            if rows:
                if looks_like_art(rows):
                    outl.append(emit_art(rows))
                else:
                    outl.append(_tab(m.group(1), (m.group(2) or '').strip(),
                                       [m.group(3).strip()] if m.group(3).strip() else [],
                                       rows, mode, tabimg, avail, stats))
                i = j
                continue
        m = RE_TAB4.match(L)
        if m:
            j = i + 1
            title = []
            while j < n and lines[j].strip() in ('', '>', '> '):
                j += 1
            m2 = re.match(r'^\s*>?\s*\*\*([^*]+)\*\*\s*$', lines[j]) if j < n else None
            if m2:
                title = [m2.group(1).strip()]; j += 1
            while j < n and lines[j].strip() in ('', '>', '> '):
                j += 1
            rows, j = collect_pipe(j)
            if rows:
                if looks_like_art(rows):
                    outl.append(emit_art(rows))
                else:
                    outl.append(_tab(m.group(1), '', title, rows, mode, tabimg, avail, stats))
                i = j
                continue
        # ---- Unified caption styles (defect fix).  LAZY ON PURPOSE:
        # RE_TABCAP also matches the label-only lines that RE_TAB2/RE_TAB3/
        # RE_TAB4 handle above, but those handlers own the rows that FOLLOW.
        # Emitting from here unconditionally would duplicate every table, so
        # this branch fires only on a caption whose table ALREADY has a vector
        # recreation -- which is precisely the case that used to fall through
        # and be silently dropped.  The branch must NOT run when no vector
        # record exists: that leaves the caption to the normal handlers below
        # (which build the hand-made copy, or skip it as a real caption-only
        # line).  Guarding on `tabimg(...) is None` here instead is INVERTED
        # and measured to turn every vector table back into a hand-made one.
        _cap = tabcap(L)
        if _cap and tabimg(_cap[0]) is not None:
            no, en, desc = _cap
            outl.append(_tab(no, en, [desc] if desc else [],
                             [], mode, tabimg, avail, stats))
            i = _skip_tab_body(i)
            continue
        # ---- ASCII-art diagram: not a table, preserve verbatim with
        # Verbatim ----
        if L.lstrip().lstrip('>').lstrip().startswith('|'):
            art, j = collect_pipe(i)
            if looks_like_art(art):
                outl.append(emit_art(art)); i = j
                continue
        # ---- Generic fallback: any pipe table starting with | becomes
        # tabularx ----
        if L.lstrip().lstrip('>').lstrip().startswith('|'):
            rows, j = collect_pipe(i)
            cap_no, cap_en = '', ''
            for k in range(len(outl) - 1, max(-1, len(outl) - 4), -1):
                mm2 = re.match(r'^> \*\*\u8868\s*([A-Z0-9.\-]+)[\uff08(]([^*\uff09)]*)[\uff09)]?\.?\*\*',
                               outl[k].strip()) or \
                      re.match(r'^\*\*\u8868\s*([A-Z0-9.\-]+)[\uff08(]([^*\uff09)]*)[\uff09)]?\.?\*\*',
                               outl[k].strip())
                if mm2:
                    cap_no, cap_en = mm2.group(1), mm2.group(2)
                    outl.pop(k)
                    break
            if looks_like_art(rows):
                outl.append(emit_art(rows))
            elif cap_no:
                outl.append(_tab(cap_no, cap_en, [], rows, mode, tabimg, avail, stats))
            else:
                # A pipe table with NO caption number at all.  It cannot match
                # any tables.json key, so it can never be the "vector exists but
                # was not used" defect -- but it must still be COUNTED, otherwise
                # the HANDMADE line under-reports and the gate loses sight of it.
                stats.setdefault('handmade_no_record', []).append('<no-caption#%d>' % (i + 1))
                header, body_rows = split_md_table(rows)
                ncol = max([len(header)] + [len(r) for r in body_rows])
                header = header + [''] * (ncol - len(header))
                body_rows = [r + [''] * (ncol - len(r)) for r in body_rows]
                lt = any(len(t) > 14 for r in [header] + body_rows for c in r for t in c.split())
                star = (ncol >= 6 or lt)
                _env = 'table*' if (star and mode in ('conf', 'tri')) else 'table'
                _w = r'\textwidth' if (star and mode in ('conf', 'tri')) else r'\columnwidth'
                _fs = r'\scriptsize' if star else r'\footnotesize'
                spec, kinds = col_specs(header, body_rows)
                if spec is None:
                    spec = '@{}' + ''.join(kinds) + '@{}'
                stats['tabenvs'] += 1
                lines_out = [r'\begin{%s}[!t]\centering%s' % (_env, _fs), _HY,
                             r'\begin{tabularx}{%s}{%s}' % (_w, spec), r'\toprule',
                             ' & '.join(cellfmt(c) for c in header) + r' \\', r'\midrule']
                for r in body_rows:
                    lines_out.append(' & '.join(cellfmt(c) for c in r) + r' \\')
                lines_out += [r'\bottomrule', r'\end{tabularx}\end{%s}' % _env]
                outl.append('\n'.join(lines_out))
            i = j
            continue

        outl.append(mathify_line(L))
        i += 1
    # An equation number moved out of the display box is appended to its
    # display as `$$\hfill (N)`.  Two IDENTICAL displays would otherwise get
    # the SAME tag twice, which is visibly wrong in the output; drop the
    # duplicate tag instead (the equation itself is untouched).  Only an exact
    # repeat is dropped, so a paper that restates eq. (2) across sections with
    # different math keeps both numbers.
    seen_eqnum = set()
    for k, ln in enumerate(outl):
        m = _EQNUM_ONLY_RE.search(ln)
        if not m:
            continue
        key = m.group(1)
        if key in seen_eqnum:
            outl[k] = ln[:m.start()].rstrip()
            EQNUM_STATS['duplicate'] += 1
        else:
            seen_eqnum.add(key)
    return '\n'.join(outl)


# Roman numeral -> Arabic, for table-number lookup.
# WHY: the extraction side writes tables.json keys as Arabic (1..7) because the
# source captions say "TABLE I".."TABLE VII" and the extractor normalises them.
# Journal translations keep the Roman form in the markdown ("\u8868 I").  Without
# this conversion tabimg('I') misses every hit and the table SILENTLY falls back
# to a hand-built tabularx -- which violates the skill's own R1 ("tables must be
# vector-recreated, never hand-typeset") while the run still reports success
# (measured on a 7-table IEEE paper: 7/7 extracted, 0/7 used).
ROMAN_NUM = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7,
    'viii': 8, 'ix': 9, 'x': 10, 'xi': 11, 'xii': 12, 'xiii': 13,
    'xiv': 14, 'xv': 15, 'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19,
    'xx': 20, 'xxi': 21, 'xxii': 22, 'xxiii': 23, 'xxiv': 24, 'xxv': 25,
}


def _norm_tabno(n):
    n = (n or '').strip().rstrip('.')
    _r = ROMAN_NUM.get(n.lower())
    if _r is not None:
        return str(_r)
    return re.sub(r'\.(?=-)', '', n)


def _en_tabno(text):
    """Pull the table id out of an English parenthesised label, or None.

    "\uff08TABLE II\uff09" -> '2'   "\uff08Table: 7.3.-4\uff09" -> '7.3-4'

    WHY a strict pattern and not a general "first number" search: this is a
    GUESS used only when the Chinese id misses tables.json entirely.  A loose
    pattern turns "**\u8868 7.3-4\uff08Table 7.3.-4\uff09**" into '7' -- i.e. it silently
    embeds the WRONG table, which is strictly worse than falling back to a
    hand-built copy.  So we accept only:
      * pure Roman numerals (normalised through ROMAN_NUM), or
      * clause numbering that contains a '.' or '-', which is what the
        extractor keys look like ("7.3-4", "2.1.-1", "C.3.1.1-1").
    A bare Arabic integer ("\uff08Table 1\uff09") is deliberately NOT guessed: the Chinese
    id is the authority, and when it is missing the caller must notice.
    """
    if not text:
        return None
    m = _RE_TAB_EN_LABEL.search(text)
    if not m:
        return None
    raw = m.group(1).strip().rstrip('.')
    if not raw:
        return None
    if ROMAN_NUM.get(raw.lower()) is not None:
        # Guard against a Roman-looking WORD ("TABLE I" in "TABLE Indoor ..."):
        # a real Roman label is either <=3 chars ("I".."XXV" fit) or followed
        # by a delimiter, never a bare long letter run.
        if len(raw) <= 3 or re.fullmatch(r'[IVXLC]+', raw):
            return str(ROMAN_NUM[raw.lower()])
        return None
    if re.fullmatch(r'[0-9]+(?:\.[0-9A-Za-z]*)*\.[0-9A-Za-z]*', raw):
        return _norm_tabno(raw)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='prep.py',
        description='markdown -> LaTeX \u6b63\u6587\u9884\u5904\u7406\uff1a'
                    '\u6570\u5b66\u5316\u3001\u56fe\u73af\u5883\u3001'
                    '\u8868\u683c\uff08\u77e2\u91cf\u590d\u523b\u4f18\u5148 / '
                    '\u624b\u6392\u542b\u5217\u5bbd\u6c42\u89e3\uff09\u3002',
        epilog='\u793a\u4f8b\uff1a\n'
               '  python scripts/prep.py body-clean.md body.md --figs .work/01/figs '
               '--tables .work/01/tables --mode conf\n'
               '  python scripts/prep.py body-clean.md body.md --tables .work/01/tables '
               '--mode access --text-width 394.6\n'
               '  python scripts/prep.py body-clean.md body.md --check\n\n'
               '\u53ef\u7528\u680f\u5bbd\uff08--text-width\uff09\u7531 runpaper.py '
               '\u4ece\u51e0\u4f55\u53f0\u8d26\u7b97\u597d\u4f20\u5165\uff1b\n'
               '\u53d6\u4e0d\u5230\u65f6\u7528\u4fdd\u5b88\u56de\u9000\u5e38\u91cf'
               '\u5e76\u5728\u8f93\u51fa\u6807 AVAIL_SOURCE=fallback\u3002\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src', metavar='INPUT.md',
                    help='\u8f93\u5165 markdown\uff08\u901a\u5e38\u662f '
                         'strip_meta \u7684\u4ea7\u7269\uff09')
    ap.add_argument('dst', nargs='?', metavar='OUTPUT.md',
                    help='\u8f93\u51fa markdown\uff08--check \u65f6\u53ef\u7701\uff09')
    ap.add_argument('--figs', metavar='DIR',
                    help='\u56fe\u76ee\u5f55\uff08\u542b figs.json\uff09')
    ap.add_argument('--tables', metavar='DIR',
                    help='\u8868\u76ee\u5f55\uff08\u542b tables.json\uff0c'
                         '\u77e2\u91cf\u590d\u523b\uff09')
    ap.add_argument('--mode', choices=['conf', 'access', 'tri'], default='conf',
                    help='\u7248\u5f0f\u6a21\u5f0f\uff1aconf=\u53cc\u680f / '
                         'access=\u5355\u680f\uff08\u9ed8\u8ba4 conf\uff09')
    ap.add_argument('--text-width', type=float, default=None, metavar='PT',
                    help='\u7248\u5fc3\u5bbd pt\uff08PostScript point\uff09\uff1b'
                         '\u7528\u4e8e\u7b97\u53ef\u7528\u680f\u5bbd')
    ap.add_argument('--check', action='store_true',
                    help='\u53ea\u505a\u4f53\u68c0\u5e76\u6253\u5370\u7edf\u8ba1\uff0c'
                         '\u4e0d\u5199\u6587\u4ef6')
    args = ap.parse_args(argv)

    if not os.path.isfile(args.src):
        print('ERROR: \u627e\u4e0d\u5230 %s\u3002\n'
              '  \u4fee\u6cd5\uff1a\u5148\u8dd1 strip_meta.py \u4ea7\u51fa clean.md\u3002'
              % args.src, file=sys.stderr)
        return EXIT_USAGE
    if not args.check and not args.dst:
        print('ERROR: \u7f3a\u5c11\u8f93\u51fa\u8def\u5f84 OUTPUT.md'
              '\uff08\u6216\u7528 --check \u53ea\u4f53\u68c0\uff09\u3002', file=sys.stderr)
        return EXIT_USAGE

    try:
        with open(args.src, encoding='utf-8') as fh:
            src = fh.read()
    except UnicodeDecodeError as exc:
        print('ERROR: %s \u4e0d\u662f UTF-8\uff1a%s' % (args.src, exc), file=sys.stderr)
        return EXIT_USAGE

    figinfo, tabinfo = {}, {}
    if args.figs and os.path.isfile(os.path.join(args.figs, 'figs.json')):
        try:
            with open(os.path.join(args.figs, 'figs.json'), encoding='utf-8') as fh:
                figinfo = json.load(fh)
        except Exception as exc:
            print('WARN: figs.json \u89e3\u6790\u5931\u8d25\uff0c\u56fe\u5c06\u4e0d\u63d2\u5165\uff1a%s'
                  % exc, file=sys.stderr)
    if args.tables and os.path.isfile(os.path.join(args.tables, 'tables.json')):
        try:
            with open(os.path.join(args.tables, 'tables.json'), encoding='utf-8') as fh:
                tabinfo = json.load(fh)
        except Exception as exc:
            print('WARN: tables.json \u89e3\u6790\u5931\u8d25\uff0c\u8868\u683c\u5c06\u8d70'
                  '\u624b\u6392\u5206\u652f\uff1a%s' % exc, file=sys.stderr)

    if args.text_width:
        if args.mode == 'conf':
            avail = max((args.text_width - COLSEP) / 2.0, 120.0)
        elif args.mode == 'tri':
            # three columns: two gutters, then split the remainder in three
            avail = max((args.text_width - 2.0 * COLSEP) / 3.0, 80.0)
        else:
            avail = max(args.text_width, 240.0)
        avail_src = 'geometry'
    else:
        if args.mode == 'conf':
            avail = AVAIL_CONF_FALLBACK
        elif args.mode == 'tri':
            avail = max(AVAIL_CONF_FALLBACK / 1.5, 90.0)
        else:
            avail = AVAIL_ACCESS_FALLBACK
        avail_src = 'fallback'

    stats = {'figenvs': 0, 'tabimgs': 0, 'tabenvs': 0,
             'cap_top': 0, 'cap_bottom': 0}
    body = convert(src, figinfo, tabinfo, args.mode, avail, stats,
                   figsdir=args.figs, tabdir=args.tables)

    n_fig_cap = body.count(r'\captionsetup')
    print('PREP %s AVAIL=%.1f AVAIL_SOURCE=%s FIGENVS=%d TABIMGS=%d TABENVS=%d '
          'CAPTIONSETUP=%d TABULARX=%d VERBATIM=%d CAP_TOP=%d CAP_BOTTOM=%d'
          % ('CHECK' if args.check else 'OK', avail, avail_src, stats['figenvs'],
             stats['tabimgs'], stats['tabenvs'], n_fig_cap,
             body.count(r'\begin{tabularx}'), body.count(r'\begin{Verbatim}'),
             stats['cap_top'], stats['cap_bottom']))
    print('SUMMARY OUTPUT=%s FIGENVS=%d TABIMGS=%d TABENVS=%d TABULARX=%d'
          % (args.dst or '-', stats['figenvs'], stats['tabimgs'], stats['tabenvs'],
             body.count(r'\begin{tabularx}')))
    # R1 machine-readable hand-built-table classification (see _tab wrapper).
    # runpaper.py turns HANDMADE_DESPITE_OK into a gate FAILURE; the other bucket
    # is legitimate but must still be visible in the log.
    _hdok = sorted(set(stats.get('handmade_despite_ok', [])))
    _hdno = sorted(set(stats.get('handmade_no_record', [])))
    print('HANDMADE HANDMADE_DESPITE_OK=%s HANDMADE_NO_RECORD=%s'
          % (','.join(_hdok) or '-', ','.join(_hdno) or '-'))
    # Self-validation for the inline-code-span fix: a silent no-op here is
    # exactly the failure mode this repo already hit once, so report the
    # counter that proves the code-span path actually ran.
    print('TEXT TTSPANS=%d TTSPANS_WITH_BREAK=%d TTSPANS_WITH_CMD=%d'
          % (SPAN_STATS['spans'], SPAN_STATS['spans_with_break'],
             SPAN_STATS['cmd_spans']))
    # Self-validation for the display-math equation-number fix (#01).  Printed
    # UNCONDITIONALLY so that "the new code never ran" cannot pass unnoticed:
    # on a paper whose numbers are all \tag{} this reads moved=0 left=N.
    print('EQTAG moved=%d left=%d duplicate=%d'
          % (EQNUM_STATS['moved'], EQNUM_STATS['left'],
             EQNUM_STATS['duplicate']))
    if _hdok:
        print('ERROR: \u8868 %s \u5728 tables.json \u91cc\u6709 ok \u8bb0\u5f55\uff0c'
              '\u5374\u672a\u8d70\u77e2\u91cf\u5d4c\u5165 -> \u8fdd\u53cd R1\u3002\n'
              '  \u4fee\u6cd5\uff1a\u67e5\u8868\u53f7\u5f52\u4e00\u5316'
              '\uff08\u7f57\u9a6c/\u963f\u62c9\u4f2f\uff09\u4e0e\u8868\u4f53\u8df3\u8fc7'
              '\u903b\u8f91\uff08\u89c1 references/tables.md\uff09\u3002'
              % ','.join(_hdok), file=sys.stderr)
    if avail_src == 'fallback':
        print('WARN: \u672a\u6536\u5230 --text-width\uff0c\u7528\u4e86\u4fdd\u5b88\u56de\u9000'
              '\u680f\u5bbd %.1fpt -> \u5bbd\u8868\u5224\u5b9a\u53ef\u80fd\u504f\u4fdd\u5b88\u3002\n'
              '  \u4fee\u6cd5\uff1a\u7531 runpaper.py \u4f20\u5165'
              '\uff08\u5b83\u4f1a\u4ece\u51e0\u4f55\u53f0\u8d26\u7b97\u771f\u5b9e\u680f\u5bbd\uff09\u3002'
              % avail)
    if not figinfo:
        print('NOTE: \u65e0 figs.json -> \u56fe\u9898\u4e0d\u4f1a\u63d2\u56fe\u3002'
              '\u82e5\u786e\u6709\u56fe\uff0c\u5148\u8dd1 extract_figs.py\u3002')
    if not tabinfo:
        print('NOTE: \u65e0 tables.json -> \u8868\u683c\u5168\u90e8\u8d70\u624b\u6392\u5206\u652f'
              '\uff08**\u5fc5\u987b\u76ee\u89c6\u786e\u8ba4\u662f\u5426\u53ef\u63a5\u53d7**\uff09\u3002')
    if args.check:
        return EXIT_OK
    d = os.path.dirname(os.path.abspath(args.dst))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(args.dst, 'w', encoding='utf-8') as fh:
        fh.write(body)
    print('WROTE %s' % args.dst)
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
