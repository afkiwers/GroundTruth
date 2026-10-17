"""Does the plugin's code compile on KiCad's Python (3.11)? The dev venv is 3.13, which accepts syntax 3.11 rejects
(PEP 701 f-strings: the same quote inside an f-string's braces, backslashes and comments there); the plugin would
then fail to start inside KiCad. Compiles every .py file of src/groundtruth and plugin/ with the given Python
(default: KiCad 10's bundled one) and lists the files it rejects.

    .venv/Scripts/python tools/dev/py311_check.py [--python "C:/Program Files/KiCad/10.0/bin/python.exe"]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KICAD_PYTHON = Path("C:/Program Files/KiCad/10.0/bin/python.exe")
DIRS = ("src/groundtruth", "plugin")

# run inside the other interpreter (-I: isolated, nothing from the repo imported): compile, never execute
PROBE = r"""
import pathlib, sys
bad = 0
for d in sys.argv[1:]:
    for p in sorted(pathlib.Path(d).rglob("*.py")):
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec", dont_inherit=True)
        except SyntaxError as e:
            bad += 1
            print(f"{p}:{e.lineno}: {e.msg}")
print(f"{sys.version.split()[0]}: {bad} file(s) rejected")
sys.exit(1 if bad else 0)
"""


def check(python: Path) -> subprocess.CompletedProcess:
    """Compile the plugin's files with `python`; its output lists the rejected files, exit code 1 if any."""
    return subprocess.run([str(python), "-I", "-c", PROBE, *DIRS], cwd=ROOT, capture_output=True, text=True)


def main() -> int:
    """The command line: --python (default KiCad 10's); prints the result and returns its exit code."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", type=Path, default=KICAD_PYTHON)
    args = ap.parse_args()
    if not args.python.exists():
        print(f"no Python at {args.python}", file=sys.stderr)
        return 2
    r = check(args.python)
    print(r.stdout + r.stderr, end="")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
