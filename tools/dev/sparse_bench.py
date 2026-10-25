"""The preconditioner's sparse factorisation by other solvers than SuperLU, on the matrices Simulate factorises
(tools/dev/export_schur.py): the M3 spike of the speed work (docs/SPEED.md).

    python tools/dev/sparse_bench.py OUT_DIR/*.npz [--solvers superlu,pardiso] [--threads 1,4] [--repeat 2]

Per matrix and solver: the analysis (ordering, symbolic factorisation), the numeric factorisation, a second one
reusing the analysis (the next frequency of the same model: the same pattern), a 1-column and an 8-column solve,
the relative residual ‖S·x − b‖ / ‖b‖, the factor's entries and the solver's own peak memory, and whether two runs
give the same bits. SuperLU is called exactly as peec does (MMD_AT_PLUS_A, diag_pivot_thresh 0.1, symmetric
mode); PARDISO (Intel MKL, `pip install mkl` in a venv of its own: never the project's until decided) as complex
symmetric, mtype 6 (S = Arᵀ·D⁻¹·Ar with D complex symmetric is complex symmetric, not Hermitian), through ctypes
on mkl_rt as pypardiso does for real matrices.
"""
import argparse
import ctypes
import glob
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu


# --------------------------------------------------------------------------- SuperLU (peec's call)
def superlu(S, threads):
    """SuperLU as PlaneStack._factorise calls it. Returns {analysis_s, factor_s, refactor_s, solve(b), nnz}."""
    t0 = time.perf_counter()
    lu = splu(S, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.1, options={"SymmetricMode": True})
    t1 = time.perf_counter()
    lu2 = splu(S, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.1, options={"SymmetricMode": True})
    t2 = time.perf_counter()
    del lu2
    return {"analysis_s": None, "factor_s": t1 - t0, "refactor_s": t2 - t1,      # no analysis apart: every call
            "solve": lu.solve, "nnz": lu.L.nnz + lu.U.nnz, "peak_mb": None}


# --------------------------------------------------------------------------- PARDISO through ctypes
def _mkl():
    """mkl_rt from the active environment, or None."""
    names = ["mkl_rt.3.dll", "mkl_rt.2.dll", "mkl_rt.dll"] if os.name == "nt" else \
        ["libmkl_rt.so.2", "libmkl_rt.so", "libmkl_rt.2.dylib", "libmkl_rt.dylib"]
    roots = [Path(sys.prefix) / "Library" / "bin", Path(sys.prefix) / "lib", Path(sys.prefix)]
    for r in roots:
        for n in names:
            p = r / n
            if p.exists():
                return ctypes.CDLL(str(p))
    for n in names:
        try:
            return ctypes.CDLL(n)
        except OSError:
            continue
    return None


class Pardiso:
    """One complex symmetric (mtype 6) PARDISO handle: analyse, factorise, solve, release [MKL PARDISO]."""

    def __init__(self, lib, S, threads: int):
        """For the matrix S (any sparse format; its upper triangle is used, as PARDISO's symmetric modes want)."""
        self.lib = lib
        lib.MKL_Set_Num_Threads(ctypes.c_int(threads))
        U = sp.triu(S, format="csr")
        U.sort_indices()
        self.n = U.shape[0]
        self.a = np.ascontiguousarray(U.data, dtype=np.complex128)
        self.ia = np.ascontiguousarray(U.indptr, dtype=np.int32)          # zero-based (iparm[34] = 1)
        self.ja = np.ascontiguousarray(U.indices, dtype=np.int32)
        self.pt = np.zeros(64, dtype=np.int64)
        self.iparm = np.zeros(64, dtype=np.int32)
        self.iparm[0] = 1        # these values, not the defaults
        self.iparm[1] = 2        # nested dissection ordering (METIS)
        self.iparm[7] = 0        # no iterative refinement (a preconditioner: GMRES corrects it)
        self.iparm[9] = 8        # pivot perturbation 1e-8
        self.iparm[10] = 0       # no scaling (symmetric matrices without weighted matching)
        self.iparm[12] = 0
        self.iparm[17] = -1      # report the factor's nonzeros
        self.iparm[20] = 1       # Bunch-Kaufman 1x1 and 2x2 pivoting: solves 3x faster on the inductive models than
                                 # 1x1 diagonal pivots (235 -> 71 ms, refboard's conducted model at 4x), the same residual
        self.iparm[26] = 0
        self.iparm[33] = threads if threads > 1 else 0   # conditional numerical reproducibility: the same bits on
                                 # every run with this many threads (without it, 4 threads gave different solves)
        self.iparm[34] = 1       # zero-based indices
        self.mtype = 6
        self.lib.pardiso.argtypes = [ctypes.c_void_p] * 16
        self.lib.pardiso.restype = None

    def call(self, phase: int, b=None):
        """One PARDISO call (phase 11 analysis, 22 factorisation, 33 solve, -1 release); returns x for a solve."""
        i32 = lambda v: ctypes.byref(ctypes.c_int32(v))       # noqa: E731
        nrhs = 1 if b is None or b.ndim == 1 else b.shape[1]
        bb = np.zeros(self.n, np.complex128) if b is None else np.asfortranarray(b, dtype=np.complex128)
        x = np.zeros_like(bb)
        err = ctypes.c_int32(0)
        self.lib.pardiso(self.pt.ctypes.data, i32(1), i32(1), i32(self.mtype), i32(phase), i32(self.n),
                         self.a.ctypes.data, self.ia.ctypes.data, self.ja.ctypes.data, None, i32(nrhs),
                         self.iparm.ctypes.data, i32(0), bb.ctypes.data, x.ctypes.data, ctypes.byref(err))
        if err.value != 0:
            raise RuntimeError(f"PARDISO phase {phase}: error {err.value}")
        return x

    def release(self):
        """Free PARDISO's memory for this handle."""
        self.call(-1)


def pardiso(S, threads):
    """PARDISO complex symmetric: analysis, factorisation, a refactorisation on the same analysis."""
    lib = _mkl()
    if lib is None:
        raise RuntimeError("mkl_rt not found (pip install mkl in this environment)")
    h = Pardiso(lib, S, threads)
    t0 = time.perf_counter()
    h.call(11)
    t1 = time.perf_counter()
    h.call(22)
    t2 = time.perf_counter()
    h.call(22)
    t3 = time.perf_counter()
    # iparm[14..16]: peak memory of the analysis / permanent / factorisation, kB; iparm[17]: factor nonzeros
    peak = max(int(h.iparm[14]), int(h.iparm[15]) + int(h.iparm[16])) / 1024
    return {"analysis_s": t1 - t0, "factor_s": t2 - t1, "refactor_s": t3 - t2, "solve": lambda b: h.call(33, b),
            "nnz": int(h.iparm[17]), "peak_mb": peak, "release": h.release}


SOLVERS = {"superlu": superlu, "pardiso": pardiso}


def bench(path: Path, solver: str, threads: int, repeat: int) -> dict:
    """One matrix, one solver, one thread count: timings (best of `repeat` for the solves), accuracy, memory."""
    S = sp.load_npz(path).tocsc()
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8")) if path.with_suffix(".json").exists() \
        else {}
    rng = np.random.default_rng(0)
    b = rng.standard_normal(S.shape[0]) + 1j * rng.standard_normal(S.shape[0])
    B = rng.standard_normal((S.shape[0], 8)) + 1j * rng.standard_normal((S.shape[0], 8))
    f = SOLVERS[solver](S, threads)
    times1, times8 = [], []
    for _ in range(repeat):
        t = time.perf_counter()
        x = f["solve"](b)
        times1.append(time.perf_counter() - t)
        t = time.perf_counter()
        X = f["solve"](B)
        times8.append(time.perf_counter() - t)
    x_again = f["solve"](b)
    res = float(np.linalg.norm(S @ x - b) / np.linalg.norm(b))
    res8 = float(np.linalg.norm(S @ X - B) / np.linalg.norm(B))
    out = {"matrix": path.name, "kind": meta.get("kind"), "n": S.shape[0], "nnz_S": S.nnz, "solver": solver,
           "threads": threads, "analysis_s": f["analysis_s"], "factor_s": f["factor_s"], "refactor_s": f["refactor_s"],
           "solve1_s": min(times1), "solve8_s": min(times8), "residual": res, "residual8": res8,
           "fill": f["nnz"], "peak_mb": f["peak_mb"], "repeatable": bool(np.array_equal(x, x_again))}
    if "release" in f:
        f["release"]()
    return out


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("matrices", nargs="+", help=".npz files from export_schur.py (globs are expanded)")
    ap.add_argument("--solvers", default="superlu,pardiso")
    ap.add_argument("--threads", default="1,4", help="thread counts for the solvers that use threads")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--json", help="write every row to this file")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    paths = [Path(p) for m in args.matrices for p in (glob.glob(m) or [m])]
    rows = []
    print(f"{'matrix':38s} {'solver':8s} {'thr':>3s} {'analyse':>8s} {'factor':>8s} {'refact':>8s} {'solve1':>8s} "
          f"{'solve8':>8s} {'fill M':>7s} {'peak MB':>8s} {'residual':>9s} same")
    for p in paths:
        for solver in args.solvers.split(","):
            for th in ([1] if solver == "superlu" else [int(t) for t in args.threads.split(",")]):
                try:
                    r = bench(p, solver, th, args.repeat)
                except Exception as e:      # noqa: BLE001 - a solver that cannot run here: say so, go on
                    print(f"{p.name:38s} {solver:8s} {th:3d}  failed: {e}")
                    continue
                rows.append(r)
                fmt = lambda v: f"{v:8.2f}" if isinstance(v, float) else f"{'—':>8s}"     # noqa: E731
                print(f"{p.name[:38]:38s} {solver:8s} {th:3d} {fmt(r['analysis_s'])} {fmt(r['factor_s'])} "
                      f"{fmt(r['refactor_s'])} {r['solve1_s']:8.3f} {r['solve8_s']:8.3f} {r['fill'] / 1e6:7.1f} "
                      f"{fmt(r['peak_mb'])} {r['residual']:9.1e} {'yes' if r['repeatable'] else 'NO'}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
