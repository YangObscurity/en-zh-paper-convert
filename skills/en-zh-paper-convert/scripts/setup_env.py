#!/usr/bin/env python3
"""setup_env.py -- one-click setup of the skill's local tool environment.

Downloads/installs everything the skill needs into THIS repository directory
(kept out of git via .gitignore):

    .venv/                 Python virtualenv with pymupdf / pypdf / pillow
    tools/tectonic.exe     LaTeX engine (single self-contained binary)

Why bundle instead of assuming: the previous pipeline compiled on a Linux
container with TeX Live.  A local run has no LaTeX at all, so the toolchain is
an explicit prerequisite -- we fetch it for you, but it must exist before any
conversion starts.

Exit codes:  0 = all prerequisites satisfied
             3 = something missing/unfixable (message says what to install)

ASCII-only source on purpose: PowerShell round-trips corrupt non-ASCII files.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# setup_env.py is the SELF-CONTAINED toolchain installer: .venv/ and tools/ must
# land INSIDE the skill directory, so that copying just `en-zh-paper-convert/`
# gives the user a working toolchain.  This deliberately does NOT use the
# workspace-root resolver (which would point at the repo root here).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(ROOT, ".venv")
TOOLS = os.path.join(ROOT, "tools")
TECTONIC = os.path.join(TOOLS, "tectonic.exe" if os.name == "nt" else "tectonic")

TECTONIC_URL = (
    "https://github.com/tectonic-typesetting/tectonic/releases/download/"
    "tectonic%400.15.0/tectonic-0.15.0-x86_64-pc-windows-msvc.zip"
)

PY_DEPS = ["pymupdf", "pypdf", "pillow"]

# distribution name -> import name (they differ for these packages!)
IMPORT_NAME = {"pymupdf": "fitz", "pypdf": "pypdf", "pillow": "PIL"}

OK = "  [OK]  "
MISS = "  [--]  "


def venv_python():
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    return os.path.join(VENV, sub, exe)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


# ---------------------------------------------------------------- detection
def check_tectonic():
    if os.path.exists(TECTONIC):
        return True, TECTONIC
    w = shutil.which("tectonic")
    return (True, w) if w else (False, None)


def check_pandoc():
    w = shutil.which("pandoc")
    if w:
        return True, w
    # common Windows install location that is not always on PATH
    for p in (r"C:\Program Files\Pandoc\pandoc.exe",
              r"C:\Program Files (x86)\Pandoc\pandoc.exe"):
        if os.path.exists(p):
            return True, p
    return False, None


def check_py_deps(py):
    missing = []
    for m in PY_DEPS:
        mod = IMPORT_NAME.get(m, m)
        r = run([py, "-c", "import %s" % mod])
        if r.returncode != 0:
            missing.append(m)
    return missing


def check_fonts():
    """Report which CJK / serif fonts the LaTeX engine can actually use."""
    found = {"cjk": [], "serif": []}
    if os.name != "nt":
        return found
    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    names = []
    try:
        for f in os.listdir(fonts_dir):
            names.append(f.lower())
    except OSError:
        return found
    blob = " ".join(names)
    for fam, keys in (("cjk", ("msyh", "simsun", "simhei", "noto")),
                      ("serif", ("times", "cambria", "georgia", "constan"))):
        for k in keys:
            if k in blob:
                found[fam].append(k)
    return found


# ---------------------------------------------------------------- actions
def do_venv(recreate=False):
    py = venv_python()
    if recreate and os.path.isdir(VENV):
        print("  removing existing .venv ...")
        shutil.rmtree(VENV, ignore_errors=True)
        py = venv_python()
    if not os.path.exists(py):
        print("  creating virtualenv at .venv ...")
        r = run([sys.executable, "-m", "venv", VENV])
        if r.returncode != 0:
            print("  ERROR: venv creation failed:\n", (r.stderr or "")[:400])
            return False
        py = venv_python()
    missing = check_py_deps(py)
    if missing:
        print("  installing:", ", ".join(missing))
        r = run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        r = run([py, "-m", "pip", "install", "--quiet"] + PY_DEPS)
        if r.returncode != 0:
            print("  ERROR: pip install failed:\n", (r.stderr or "")[:600])
            return False
        missing = check_py_deps(py)
    if missing:
        print("  ERROR: still missing after install:", missing)
        return False
    print("  " + MISS.strip() + " python deps ready:", ", ".join(PY_DEPS))
    return True


def do_tectonic(force=False):
    ok, path = check_tectonic()
    if ok and not force:
        print("  tectonic present:", path)
        return True
    os.makedirs(TOOLS, exist_ok=True)
    if platform.system() != "Windows":
        print("  ERROR: auto-download only implemented for Windows.")
        print("         Install manually: https://tectonic-typesetting.github.io/")
        return False
    zpath = os.path.join(TOOLS, "tectonic.zip")
    print("  downloading tectonic (about 18 MB) ...")
    try:
        urllib.request.urlretrieve(TECTONIC_URL, zpath)
    except Exception as e:
        print("  ERROR: download failed:", e)
        print("         Get it manually:", TECTONIC_URL)
        return False
    try:
        with zipfile.ZipFile(zpath) as z:
            z.extractall(TOOLS)
    except Exception as e:
        print("  ERROR: unzip failed:", e)
        return False
    if not os.path.exists(TECTONIC):
        print("  ERROR: extracted but %s not found" % TECTONIC)
        return False
    print("  tectonic installed:", TECTONIC)
    return True


def verify_all():
    print("\n=== verdict ===")
    bad = []
    ok, tpath = check_tectonic()
    print(("  tectonic      : " + (tpath or "MISSING")))
    if not ok:
        bad.append("tectonic")
    ok, ppath = check_pandoc()
    print("  pandoc        : " + (ppath or "MISSING"))
    if not ok:
        bad.append("pandoc")
    py = venv_python()
    if os.path.exists(py):
        miss = check_py_deps(py)
        print("  python deps   : " + ("OK" if not miss else "MISSING " + str(miss)))
        if miss:
            bad.append("python-deps")
    else:
        print("  python deps   : no .venv")
        bad.append("venv")
    f = check_fonts()
    print("  CJK fonts     : " + (", ".join(f["cjk"]) if f["cjk"] else "MISSING"))
    print("  serif fonts   : " + (", ".join(f["serif"]) if f["serif"] else "MISSING"))
    if not f["cjk"]:
        bad.append("cjk-font")
    if bad:
        print("\nRESULT=INCOMPLETE missing=%s" % ",".join(bad))
        print("Install the missing items above, then re-run: "
              "python skills/en-zh-paper-convert/scripts/setup_env.py")
        return 3
    print("\nRESULT=READY")
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="setup_env.py",
        description="One-click setup of the skill's local toolchain "
                    "(.venv + tools/tectonic). Run this BEFORE any conversion.",
        epilog="Exit 0 = ready. Exit 3 = something missing (see the message).")
    ap.add_argument("--check", action="store_true",
                    help="only report status, change nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-download tectonic / recreate venv")
    ap.add_argument("--recreate-venv", action="store_true",
                    help="delete and rebuild .venv")
    args = ap.parse_args()

    print("=== skill environment setup ===")
    print("repo root:", ROOT)
    if args.check:
        return verify_all()

    do_venv(recreate=args.recreate_venv)
    do_tectonic(force=args.force)
    return verify_all()


if __name__ == "__main__":
    sys.exit(main())
