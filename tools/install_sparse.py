"""Install Intel MKL where GroundTruth runs on Windows (or x86 Linux), so Sparse solver "auto" (the default) uses
PARDISO for the preconditioner's factorisations (solver/sparse.py; docs/SPEED.md 5.3: 10-50x faster factorisations
than SuperLU in less memory, the same results to the solver's tolerance).

    python tools/install_sparse.py                   # the KiCad 10 plugin venv
    python tools/install_sparse.py --python PATH     # another environment (the CLI's, a server's venv)
    python tools/install_sparse.py --dry-run         # show what would run
    python tools/install_sparse.py --uninstall       # remove it again (SuperLU, as before)

`pip install mkl` (~250 MB with intel-openmp and tbb; Intel Simplified Software License), then a check that mkl_rt
loads and PARDISO factorises a small complex symmetric matrix in that environment. Not needed on a Mac (Apple
Accelerate is part of macOS; a Mac compute server's installer builds its helper) and not possible on Apple Silicon
or ARM Windows (MKL is x86 only). Without it everything runs on SuperLU as before. Restart KiCad afterwards.
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_gpu import plugin_python, run_shown  # noqa: E402  (the same plugin venv, the same output relay)

PACKAGE = "mkl"

# run in the target environment: does mkl_rt load, and does PARDISO factorise and solve a small complex symmetric
# matrix (mtype 6) there, as solver/sparse.py will
CHECK = r'''
import ctypes, os, sys
from pathlib import Path
import numpy as np
names = ["mkl_rt.3.dll", "mkl_rt.2.dll", "mkl_rt.dll"] if os.name == "nt" else ["libmkl_rt.so.2", "libmkl_rt.so"]
lib = None
for root in (Path(sys.prefix) / "Library" / "bin", Path(sys.prefix) / "lib", Path(sys.prefix)):
    for n in names:
        if (root / n).exists():
            lib = ctypes.CDLL(str(root / n)); break
    if lib: break
if lib is None:
    print("mkl_rt not found in", sys.prefix); sys.exit(1)
lib.pardiso.argtypes = [ctypes.c_void_p] * 16
n = 4
a = np.array([4, 1j, 4, 1, 4, 1j, 4], complex)          # upper triangle of a tridiagonal complex symmetric matrix
ia = np.array([0, 2, 4, 6, 7], np.int32); ja = np.array([0, 1, 1, 2, 2, 3, 3], np.int32)
pt = np.zeros(64, np.int64); iparm = np.zeros(64, np.int32)
iparm[0] = 1; iparm[1] = 2; iparm[20] = 1; iparm[34] = 1
b = np.ones(n, complex); x = np.zeros(n, complex); err = ctypes.c_int32(0)
i = lambda v: ctypes.byref(ctypes.c_int32(v))
for phase in (13, -1):
    lib.pardiso(pt.ctypes.data, i(1), i(1), i(6), i(phase), i(n), a.ctypes.data, ia.ctypes.data, ja.ctypes.data,
                None, i(1), iparm.ctypes.data, i(0), b.ctypes.data, x.ctypes.data, ctypes.byref(err))
    if err.value:
        print("PARDISO error", err.value); sys.exit(1)
S = np.diag([4] * 4).astype(complex)
for k, v in ((0, 1j), (1, 1), (2, 1j)):
    S[k, k + 1] = S[k + 1, k] = v
r = np.linalg.norm(S @ x - b)
print(f"PARDISO works here ({lib._name}), residual {r:.1e}")
sys.exit(0 if r < 1e-10 else 1)
'''


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", help="the environment's python (default: the KiCad 10 plugin venv's)")
    ap.add_argument("--uninstall", action="store_true", help="remove mkl again")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if platform.machine().lower() not in ("amd64", "x86_64"):
        print(f"Intel MKL runs on x86 only, not on this {platform.machine()} machine"
              + (": on a Mac, Apple Accelerate is used instead (nothing to install)" if sys.platform == "darwin"
                 else "") + ".")
        return 1
    py = Path(args.python) if args.python else plugin_python()
    if not py.exists():
        print(f"{py} not found: start the GroundTruth plugin once in KiCad first (KiCad makes its environment then), "
              "or give --python.")
        return 1
    cmd = [str(py), "-m", "pip", "uninstall" if args.uninstall else "install", "-y" if args.uninstall else "--upgrade",
           PACKAGE]
    if args.uninstall:
        cmd += ["intel-openmp", "tbb", "tcmlib", "umf"]
    print(" ".join(cmd))
    if args.dry_run:
        return 0
    rc = run_shown(cmd)
    if rc or args.uninstall:
        if rc:
            print("\npip failed: see above (no internet? a proxy? KiCad still running and holding the environment?)")
        else:
            print("\nRemoved: GroundTruth uses SuperLU again. Restart KiCad.")
        return rc
    print("\nChecking it...")
    rc = run_shown([str(py), "-c", CHECK])
    print("\nDone: restart KiCad. Preferences... > Sparse solver: auto (or pardiso) uses it; after a Simulate the "
          "run's solver statistics say \"sparse solver: pardiso\"." if rc == 0 else
          "\nmkl is installed but PARDISO did not work here: GroundTruth keeps using SuperLU (nothing breaks).")
    return rc


if __name__ == "__main__":
    sys.exit(main())
