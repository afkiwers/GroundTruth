"""The test suite one file per process (development aid): a single pytest process grows over the 400 tests until
the laptop runs out of memory (MemoryError, "Not enough memory to perform factorization") late in the run; separate
processes give every file a fresh one. Prints one line per file and the failures at the end.

    .venv/Scripts/python tools/dev/run_tests_split.py [-k EXPR] [files ...]
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    """Run pytest on each test file in its own process (all of tests/ by default, -k passed on), print each file's
    time and result line, and the failing files at the end."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("-k", default="")
    args = ap.parse_args()
    files = args.files or sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests").rglob("test_*.py"))
    bad = []
    t0 = time.time()
    for f in files:
        cmd = [sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", f] + (["-k", args.k] if args.k else [])
        t = time.time()
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        last = [line for line in r.stdout.splitlines() if line.strip()][-1:] or ["(no output)"]
        print(f"{f:45s} {time.time() - t:6.0f} s  {last[0]}", flush=True)
        bad += [line for line in r.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))]
        if r.returncode not in (0, 5) and not any(line.startswith(("FAILED", "ERROR")) for line in r.stdout.splitlines()):
            bad.append(f"{f}: exit {r.returncode}")
    print(f"\n{len(files)} files in {time.time() - t0:.0f} s; {len(bad)} failure(s)")
    for line in bad:
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
