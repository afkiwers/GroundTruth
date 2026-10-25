"""Faster sparse factorisations of the preconditioner's Schur complement than SuperLU (docs/SPEED.md 5.3, 5.4).

PlaneStack._factorise hands S = Arᵀ·D⁻¹·Ar (complex symmetric, not Hermitian: D = R + jωL) to `factorise`, which
returns a Factor from the configured backend, or None when that backend is SuperLU or cannot serve (peec then makes
its SuperLU factorisation as before):

- "pardiso": Intel MKL PARDISO, complex symmetric (mtype 6) with Bunch-Kaufman pivoting [MKL PARDISO], through
  ctypes on mkl_rt (the optional `mkl` package; x86 Windows and Linux: MKL does not run on Apple Silicon).
  refboard's 4x matrices on the laptop: factorisations 10-50x faster than SuperLU, 40-50 % less fill, the board-wide
  model's solves 93 -> 59 ms, the inductive models' 53 -> 71 ms.
- "accelerate": Apple Accelerate's sparse LDLᵀ (SBK pivoting) for complex symmetric matrices [Apple Accelerate],
  macOS 15.5+, through a small C shim (accelerate_shim.c beside this file) compiled with the Mac's clang on first
  use and cached. The M2 at 4x: solves 30-91 ms, factorisations 0.25-0.9 s.
- "superlu" (or None): SuperLU in peec, unchanged.

"auto" (the default) takes Accelerate on macOS and PARDISO where mkl is installed, else SuperLU; GROUNDTRUTH_SPARSE
or `configure()` (the CLI's --sparse) chooses. Every backend runs single-threaded inside a solve: its results are
then the same bits on every run (Apple documents determinism only with one thread; PARDISO's threads needed its
reproducibility mode), and the solver already runs its solves side by side. A backend that fails to load or to
factorise falls back to SuperLU for that factorisation, counted in `fallbacks` (the run's statistics): never a
failed solve. Any of them is a preconditioner: GMRES's answer is the same to its tolerance [Saad 2003].
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import scipy.sparse as sp

CHOICES = ("auto", "superlu", "pardiso", "accelerate")
_choice = [os.environ.get("GROUNDTRUTH_SPARSE", "auto").strip().lower() or "auto"]
_LOCK = threading.Lock()
_LOADED: dict[str, object] = {}          # backend name -> its library, or an Exception why it can't load
fallbacks = [0]                          # factorisations a backend could not make (SuperLU made them)


def configure(name: Optional[str]):
    """Choose the backend for this process ("auto", "superlu", "pardiso", "accelerate"; None leaves it)."""
    if name is None:
        return
    name = name.strip().lower()
    if name not in CHOICES:
        raise ValueError(f"sparse solver {name!r}: one of {', '.join(CHOICES)}")
    _choice[0] = name


def chosen() -> str:
    """The backend asked for (configure / GROUNDTRUTH_SPARSE), before availability."""
    return _choice[0]


def active() -> str:
    """The backend factorisations use here: the chosen one when it loads, for "auto" the best that loads, else
    "superlu"."""
    want = _choice[0]
    order = [want] if want != "auto" else (["accelerate"] if sys.platform == "darwin" else ["pardiso"])
    for name in order:
        if name in ("pardiso", "accelerate") and not isinstance(_library(name), Exception):
            return name
    return "superlu"


# --------------------------------------------------------------------------- loading the libraries
def _library(name: str):
    """The backend's library (loaded once), or the Exception that kept it from loading."""
    with _LOCK:
        if name not in _LOADED:
            try:
                _LOADED[name] = _load_mkl() if name == "pardiso" else _load_accelerate()
            except Exception as e:      # noqa: BLE001 - not installed, wrong platform, no compiler: SuperLU
                _LOADED[name] = e
        return _LOADED[name]


def _load_mkl():
    """mkl_rt from this environment (the `mkl` package puts it in Library/bin on Windows, lib elsewhere)."""
    if sys.platform == "darwin":
        raise OSError("Intel MKL does not run on Apple Silicon")
    names = ["mkl_rt.3.dll", "mkl_rt.2.dll", "mkl_rt.dll"] if os.name == "nt" else ["libmkl_rt.so.2", "libmkl_rt.so"]
    for root in (Path(sys.prefix) / "Library" / "bin", Path(sys.prefix) / "lib", Path(sys.prefix)):
        for n in names:
            if (root / n).exists():
                lib = ctypes.CDLL(str(root / n))
                break
        else:
            continue
        break
    else:
        raise OSError("mkl_rt not found (pip install mkl)")
    lib.pardiso.argtypes = [ctypes.c_void_p] * 16
    lib.pardiso.restype = None
    lib.MKL_Set_Num_Threads(ctypes.c_int(1))       # one thread inside a solve: the same bits every run
    return lib


def _shim_cache() -> Path:
    """Where the compiled Accelerate shim is kept (per user)."""
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / "Library" / "Caches"))
    return base / "GroundTruth"


def _load_accelerate():
    """The Accelerate shim, compiled from accelerate_shim.c with the Mac's clang on first use (cached, rebuilt
    when the source changes)."""
    if sys.platform != "darwin":
        raise OSError("Apple Accelerate is macOS only")
    src = Path(__file__).with_name("accelerate_shim.c")
    code = src.read_bytes()
    import hashlib
    lib_path = _shim_cache() / f"gt_accelerate_{hashlib.sha256(code).hexdigest()[:12]}.dylib"
    if not lib_path.exists():
        lib_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = lib_path.with_suffix(".tmp")
        subprocess.run(["xcrun", "clang", "-O2", "-dynamiclib", "-o", str(tmp), str(src), "-framework", "Accelerate"],
                       check=True, capture_output=True, timeout=300)
        os.replace(tmp, lib_path)
    os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")       # deterministic (Apple: only single-threaded)
    lib = ctypes.CDLL(str(lib_path))
    lib.gt_factor.argtypes = [ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_long)]
    lib.gt_factor.restype = ctypes.c_void_p
    lib.gt_solve.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    lib.gt_solve.restype = ctypes.c_int
    lib.gt_free.argtypes = [ctypes.c_void_p]
    lib.gt_free.restype = None
    return lib


# --------------------------------------------------------------------------- factorisations
class Factor:
    """A factorisation of S from a backend: solve(rhs) for a vector or an (n, k) block, from any thread (one at a
    time: the backends' handles are not safe for concurrent solves); `nnz` its stored entries (memory planning,
    peec.lu_bytes) and `bytes` what the backend reports holding."""

    def __init__(self, backend: str, n: int, nnz: int, nbytes: int, solve, free):
        """Wrap a backend's handle: its solve(b) -> x and free() callables."""
        self.backend, self.n, self.nnz, self.bytes = backend, n, nnz, nbytes
        self._solve, self._free = solve, free
        self._lock = threading.Lock()

    def solve(self, rhs):
        """S⁻¹·rhs (a vector, or a block of columns)."""
        with self._lock:
            return self._solve(rhs)

    def __del__(self):
        """Release the backend's memory."""
        free, self._free = getattr(self, "_free", None), None
        if free is not None:
            try:
                free()
            except Exception:       # noqa: BLE001 - interpreter shutdown: nothing to give back
                pass


def factorise(S) -> Optional[Factor]:
    """S factorised by the active backend, or None for SuperLU (the active one, or a backend that failed on this
    matrix: counted in `fallbacks`)."""
    name = active()
    if name == "superlu":
        return None
    try:
        return _pardiso(S) if name == "pardiso" else _accelerate(S)
    except MemoryError:
        raise
    except Exception:               # noqa: BLE001 - this matrix on this backend: SuperLU makes it
        with _LOCK:
            fallbacks[0] += 1
        return None


def _upper_csr(S):
    """S's upper triangle as sorted CSR (both backends use one triangle of the symmetric matrix)."""
    U = sp.triu(S, format="csr")
    U.sort_indices()
    return U


def _pardiso(S) -> Factor:
    """PARDISO complex symmetric: analysis, factorisation, solves; one thread, Bunch-Kaufman pivoting, no
    iterative refinement (a preconditioner) [MKL PARDISO] (tools/dev/sparse_bench.py measured the settings)."""
    lib = _library("pardiso")
    U = _upper_csr(S)
    n = U.shape[0]
    a = np.ascontiguousarray(U.data, dtype=np.complex128)
    ia = np.ascontiguousarray(U.indptr, dtype=np.int32)
    ja = np.ascontiguousarray(U.indices, dtype=np.int32)
    pt = np.zeros(64, dtype=np.int64)
    iparm = np.zeros(64, dtype=np.int32)
    iparm[0] = 1        # these values, not the defaults
    iparm[1] = 2        # nested dissection (METIS)
    iparm[7] = 0        # no iterative refinement
    iparm[9] = 8        # pivot perturbation 1e-8
    iparm[17] = -1      # report the factor's entries
    iparm[20] = 1       # Bunch-Kaufman pivoting: 3x faster solves than 1x1 pivots on the inductive models
    iparm[34] = 1       # zero-based indices
    i32 = lambda v: ctypes.byref(ctypes.c_int32(v))       # noqa: E731
    state = {"alive": True}

    def call(phase: int, b=None):
        """One PARDISO call; returns x for a solve (phase 33)."""
        nrhs = 1 if b is None or b.ndim == 1 else b.shape[1]
        bb = np.zeros(n, np.complex128) if b is None else np.asfortranarray(b, dtype=np.complex128)
        x = np.zeros_like(bb)
        err = ctypes.c_int32(0)
        lib.pardiso(pt.ctypes.data, i32(1), i32(1), i32(6), i32(phase), i32(n), a.ctypes.data, ia.ctypes.data,
                    ja.ctypes.data, None, i32(nrhs), iparm.ctypes.data, i32(0), bb.ctypes.data, x.ctypes.data,
                    ctypes.byref(err))
        if err.value == -2:
            raise MemoryError(f"PARDISO ran out of memory ({n} unknowns)")
        if err.value != 0:
            raise RuntimeError(f"PARDISO phase {phase}: error {err.value}")
        return x

    def free():
        """Release PARDISO's memory for this factorisation (once)."""
        if state["alive"]:
            state["alive"] = False
            call(-1)
    try:
        call(12)        # analysis + numeric factorisation
    except BaseException:
        free()
        raise
    peak_kb = max(int(iparm[14]), int(iparm[15]) + int(iparm[16]))
    return Factor("pardiso", n, int(iparm[17]), peak_kb * 1024, lambda b: call(33, b), free)


def _accelerate(S) -> Factor:
    """Accelerate's complex symmetric LDLᵀ (SBK pivoting) through the shim: gt_factor, gt_solve, gt_free."""
    lib = _library("accelerate")
    U = sp.triu(S, format="csc")
    U.sort_indices()
    n = U.shape[0]
    cs = np.ascontiguousarray(U.indptr, dtype=np.int64)
    ri = np.ascontiguousarray(U.indices, dtype=np.int32)
    v = np.ascontiguousarray(U.data, dtype=np.complex128)
    info = (ctypes.c_long * 2)()
    h = lib.gt_factor(n, cs.ctypes.data, ri.ctypes.data, v.ctypes.data, info)
    if not h:
        if info[0] == -2:
            raise MemoryError(f"Accelerate ran out of memory ({n} unknowns)")
        raise RuntimeError(f"Accelerate factorisation: status {info[0]}")

    def solve(b):
        """Solve in place on a Fortran-ordered complex copy of b."""
        x = np.array(b, dtype=np.complex128, order="F", copy=True)
        k = 1 if x.ndim == 1 else x.shape[1]
        if lib.gt_solve(h, x.ctypes.data, k) != 0:
            raise RuntimeError("Accelerate solve failed")
        return x
    return Factor("accelerate", n, int(info[1]), int(info[1]) * 16, solve, lambda: lib.gt_free(h))
