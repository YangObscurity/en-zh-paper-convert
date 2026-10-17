#!/usr/bin/env python3
"""gate_encoding.py -- detect 'successfully rendered garbage' (mojibake) in a delivered PDF.

Why this exists (real incident): a corrupted string literal in the preprocessing
step produced figure captions containing U+9365 instead of U+56FE. The character
HAS a glyph in Noto Serif SC, so the LaTeX gate "Missing character = 0" stayed
green and the garbage shipped. A missing-glyph check cannot catch
corrupted-but-valid text.

Approach (principled, not a brittle marker list):
  1. private-use-area codepoints   -> broken font mapping
  2. U+FFFD replacement char
  3. obscure CJK blocks (Ext-A/B..F, Compatibility, Radicals) -- mojibake almost
     always lands there, because the UTF-8 bytes of a common character get
     reinterpreted as a rare codepoint. Reported with samples for fast human judgement.
  4. known exact mojibake markers (cheap, high precision)

Usage:
  python scripts/gate_encoding.py <pdf> [more.pdf ...] [--json out.json]
Exit: 0 = clean, 1 = suspicious text found, 2 = usage/IO error
"""
import argparse
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Exact markers observed in real incidents (high precision, cheap)
MOJIBAKE_MARKERS = ("\u93c1", "\u93c5", "\u951b", "\u9225", "\u7039",
                    "\u7026", "\u7eef", "\u93a5", "\u6d94", "\u5a03",
                    "\u93a8", "\u951f", "\u9227")


def extract_text(pdf):
    import fitz
    d = fitz.open(pdf)
    try:
        return "\n".join(p.get_text() for p in d), d.page_count
    finally:
        d.close()


def scan(pdf):
    text, pages = extract_text(pdf)
    findings = []

    # 1) exact markers
    for ch in MOJIBAKE_MARKERS:
        n = text.count(ch)
        if n:
            idx = text.find(ch)
            findings.append({
                "kind": "mojibake-marker",
                "char": "U+%04X" % ord(ch),
                "count": n,
                "sample": text[max(0, idx - 30): idx + 30].replace("\n", " "),
            })

    # 2) replacement char
    if "\ufffd" in text:
        findings.append({"kind": "replacement-char", "count": text.count("\ufffd")})

    # 3) private use area
    pua = sorted(set(re.findall(r"[\ue000-\uf8ff]", text)))
    if pua:
        findings.append({"kind": "private-use-area", "count": len(pua),
                         "sample": "".join(pua[:8])})

    # 4) obscure CJK blocks -> strong mojibake signal
    rare = {}
    for ch in text:
        cp = ord(ch)
        if (0x3400 <= cp <= 0x4DBF          # Ext-A
                or 0x20000 <= cp <= 0x3FFFF  # Ext-B..F
                or 0xF900 <= cp <= 0xFAFF    # Compatibility Ideographs
                or 0x2E80 <= cp <= 0x2FDF):  # Radicals / Kangxi
            rare[ch] = rare.get(ch, 0) + 1
    if rare:
        top = sorted(rare.items(), key=lambda kv: -kv[1])[:8]
        findings.append({
            "kind": "rare-cjk-block",
            "count": sum(rare.values()),
            "sample": "".join(c for c, _ in top),
            "chars": ["U+%04X x%d" % (ord(c), n) for c, n in top],
        })

    return {"pages": pages, "chars": len(text), "findings": findings}


def main():
    ap = argparse.ArgumentParser(
        prog="gate_encoding.py",
        description="Gate: detect successfully-rendered garbage (mojibake) in a "
                    "delivered PDF. Complements the missing-glyph gate, which "
                    "cannot catch corrupted-but-valid characters.",
        epilog="Exit 0 = clean, 1 = suspicious text, 2 = usage/IO error.")
    ap.add_argument("pdfs", nargs="+", help="PDF file(s) to check")
    ap.add_argument("--json", metavar="OUT.json", help="write machine-readable report")
    args = ap.parse_args()

    results, bad = {}, 0
    for p in args.pdfs:
        if not os.path.exists(p):
            print("ERROR: not found: %s" % p, file=sys.stderr)
            return 2
        try:
            r = scan(p)
        except Exception as e:
            print("ERROR: %s: %s" % (p, e), file=sys.stderr)
            return 2
        results[os.path.basename(p)] = r
        fs = r["findings"]
        print("%-40s pages=%-4s chars=%-7s %s" % (
            os.path.basename(p)[:38], r["pages"], r["chars"],
            "CLEAN" if not fs else "SUSPECT(%d)" % len(fs)))
        for f in fs:
            print("     - %s" % json.dumps(f, ensure_ascii=False))
        if fs:
            bad += 1

    if args.json:
        d = os.path.dirname(args.json)
        if d:
            os.makedirs(d, exist_ok=True)
        json.dump(results, open(args.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("report ->", args.json)

    print("\nVERDICT=%s" % ("FAIL" if bad else "PASS"))
    if bad:
        print("Fix the SOURCE text (usually a corrupted string literal in the "
              "preprocessing script), then rebuild. Re-running the missing-glyph "
              "gate is not enough -- it cannot see this defect class.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
