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
reproducibility mode), and the solver already runs its solves side by side. A backend that fails to load leaves
SuperLU; one that fails to factorise a matrix leaves that model to SuperLU (peec plans its memory as SuperLU's
then), counted in `fallbacks` with the reason (the run's statistics, per run): never a failed solve. A singular S
is no error for PARDISO (it perturbs the pivot; SuperLU says "exactly singular"): counted in `perturbed`. Any of
them is a preconditioner: GMRES's answer is the same to its tolerance [Saad 2003] (refboard's clock net at 1x:
the same GMRES iterations at every frequency with PARDISO and SuperLU, results within 4e-6 dB, audit round 8).
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
DEFAULT = os.environ.get("GROUNDTRUTH_SPARSE", "auto").strip().lower() or "auto"     # the process's own choice
if DEFAULT not in CHOICES:              # a mistyped variable: auto (it used to be kept, and meant SuperLU)
    DEFAULT = "auto"
_choice = [DEFAULT]
_LOCK = threading.Lock()
_LOADED: dict[str, object] = {}          # backend name -> its library, or an Exception why it can't load
fallbacks = [0]                          # factorisations a backend could not make (SuperLU made them)
last_fallback = [""]                     # why the latest of them could not be made (the run's statistics)
perturbed = [0]                          # pivots PARDISO perturbed (a singular or nearly singular S, below)
_INT32_MAX = 2 ** 31 - 1                 # PARDISO's (and the shim's) row / column indices are 32-bit
MKL_THREADING_SEQUENTIAL = 1             # mkl_rt's MKL_Set_Threading_Layer value (INTEL 0, SEQUENTIAL 1, ...)


def configure(name: Optional[str]):
    """Choose the backend ("auto", "superlu", "pardiso", "accelerate"); None: the process's default (DEFAULT:
    GROUNDTRUTH_SPARSE, else auto), as config.settings documents analysis.sparse. None used to leave whatever the
    process chose last: in the plugin a Simulate whose settings and Preferences named no backend ran on the one an
    earlier Simulate had asked for (audit round 8)."""
    if name is None:
        _choice[0] = DEFAULT
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
    # MKL's sequential threading layer, chosen before any other MKL call: one thread inside a solve (the same bits
    # every run), and no OpenMP runtime of MKL's own. Its default layer loads the mkl wheel's libiomp5md.dll, and a
    # process that had imported PyTorch (which brings its own copy; solver/gpu.py imports it at every Simulate's
    # start where it is installed) aborted there: "OMP: Error #15 ... already initialized", exit code 3, the plugin
    # or the compute server's worker gone (audit round 8: a GPU machine with tools/install_gpu.py and
    # tools/install_sparse.py both run) [MKL PARDISO]
    lib.MKL_Set_Threading_Layer.restype = ctypes.c_int
    lib.MKL_Set_Threading_Layer(ctypes.c_int(MKL_THREADING_SEQUENTIAL))
    lib.pardiso.argtypes = [ctypes.c_void_p] * 16
    lib.pardiso.restype = None
    lib.MKL_Set_Num_Threads(ctypes.c_int(1))       # (the sequential layer's own count; kept explicit)
    return lib


def _shim_cache() -> Path:
    """Where the compiled Accelerate shim is kept (per user)."""
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / "Library" / "Caches"))
    return base / "GroundTruth"


ACCELERATE_NO_MEMORY = -1000            # the shim's status when its own allocation fails (GT_NO_MEMORY there):
                                        # outside Accelerate's SparseStatus values (0 to -4, and -INT_MAX)
PREBUILT = [Path("/Library/Application Support/GroundTruth/gt_accelerate.dylib")]   # the server's (built once by
                                        # the user: a root service cannot answer Xcode's licence prompt, 2026-10-25)


def _load_accelerate():
    """The Accelerate shim: GROUNDTRUTH_ACCELERATE_SHIM, else a prebuilt one (PREBUILT, beside this file), else
    compiled from accelerate_shim.c with the Mac's clang on first use (cached, rebuilt when the source changes;
    the compiler's message in the error when it fails)."""
    if sys.platform != "darwin":
        raise OSError("Apple Accelerate is macOS only")
    src = Path(__file__).with_name("accelerate_shim.c")
    given = os.environ.get("GROUNDTRUTH_ACCELERATE_SHIM")
    found = [Path(given)] if given else []
    found += PREBUILT + [Path(__file__).with_name("gt_accelerate.dylib")]
    lib_path = next((p for p in found if p.exists()), None)
    if lib_path is None:
        import hashlib
        lib_path = _shim_cache() / f"gt_accelerate_{hashlib.sha256(src.read_bytes()).hexdigest()[:12]}.dylib"
    if not lib_path.exists():
        lib_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = lib_path.with_suffix(f".{os.getpid()}.tmp")     # two processes compiling at once: each its own file
        done = subprocess.run(["xcrun", "clang", "-O2", "-dynamiclib", "-o", str(tmp), str(src), "-framework",
                               "Accelerate"], capture_output=True, text=True, timeout=300)
        if done.returncode != 0:
            raise OSError(f"compiling the Accelerate shim failed: {(done.stderr or done.stdout).strip()[:300]}")
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
    time: the backends' handles are not safe for concurrent solves); `nnz` its stored entries and `bytes` what the
    backend reports it took at its peak (PARDISO's own count; Accelerate's factor size): peec.lu_bytes plans the
    model's next factorisations with both; `perturbed` the pivots the backend perturbed (PARDISO, else 0)."""

    def __init__(self, backend: str, n: int, nnz: int, nbytes: int, solve, free, perturbed: int = 0):
        """Wrap a backend's handle: its solve(b) -> x and free() callables."""
        self.backend, self.n, self.nnz, self.bytes, self.perturbed = backend, n, nnz, nbytes, perturbed
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


def counts() -> tuple[int, int]:
    """The process's fallback and perturbed-pivot counts so far: a run takes them at its start and hands them to
    report(since=...), so that its statistics count its own factorisations only."""
    return fallbacks[0], perturbed[0]


def report(since: tuple[int, int] = (0, 0)) -> dict:
    """What the factorisations here use, for a run's statistics: the backend chosen and active, why the chosen one
    (or auto's) did not load, how many factorisations fell back to SuperLU since `since` (counts() at the run's
    start) and why the latest did, and how many pivots PARDISO perturbed. (The run's statistics read the process's
    total before its solves until audit round 8: a fresh process always said 0, a later one an earlier run's.)"""
    out = {"chosen": chosen(), "active": active(), "fallbacks": fallbacks[0] - since[0]}
    if out["fallbacks"] and last_fallback[0]:
        out["fallback_reason"] = last_fallback[0]
    if perturbed[0] - since[1]:
        out["perturbed_pivots"] = perturbed[0] - since[1]
    why = {k: str(v)[:300] for k, v in _LOADED.items() if isinstance(v, Exception)}
    if why:
        out["not_loaded"] = why
    return out


def factorise(S) -> Optional[Factor]:
    """S factorised by the active backend, or None for SuperLU (the active one, or a backend that failed on this
    matrix: counted in `fallbacks`, its reason kept in `last_fallback`)."""
    name = active()
    if name == "superlu":
        return None
    try:
        F = _pardiso(S) if name == "pardiso" else _accelerate(S)
    except MemoryError:
        raise
    except Exception as e:          # noqa: BLE001 - this matrix on this backend: SuperLU makes it
        with _LOCK:
            fallbacks[0] += 1
            last_fallback[0] = f"{name}: {e}"[:300]
        return None
    if F.perturbed:
        with _LOCK:
            perturbed[0] += F.perturbed
    return F


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
    if U.nnz > _INT32_MAX:
        # the int32 cast below would wrap silently and PARDISO read out of bounds (a crash, not an error code)
        raise RuntimeError(f"PARDISO: {U.nnz} entries exceed its 32-bit indices")
    a = np.ascontiguousarray(U.data, dtype=np.complex128)
    ia = np.ascontiguousarray(U.indptr, dtype=np.int32)
    ja = np.ascontiguousarray(U.indices, dtype=np.int32)
    pt = np.zeros(64, dtype=np.int64)
    iparm = np.zeros(64, dtype=np.int32)
    iparm[0] = 1        # these values, not the defaults
    iparm[1] = 2        # nested dissection (METIS)
    iparm[7] = 0        # no iterative refinement, except PARDISO's own two steps after perturbed pivots [MKL PARDISO]
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
    # its peak memory [MKL PARDISO, iparm(15)-(17)]: the symbolic phase's peak, or its permanent memory plus the
    # numerical factorisation's; the factor's entries in iparm(18), its pivots perturbed by 1e-8 in iparm(14) (a
    # singular S is not an error here, unlike SuperLU's "exactly singular": counted, report())
    peak_kb = max(int(iparm[14]), int(iparm[15]) + int(iparm[16]))
    nnz = int(iparm[17]) if iparm[17] > 0 else peak_kb * 1024 // 16      # a factor past int32: its memory's size
    return Factor("pardiso", n, nnz, peak_kb * 1024, lambda b: call(33, b), free, perturbed=int(iparm[13]))


def _accelerate(S) -> Factor:
    """Accelerate's complex symmetric LDLᵀ (SBK pivoting) through the shim: gt_factor, gt_solve, gt_free."""
    lib = _library("accelerate")
    U = sp.triu(S, format="csc")
    U.sort_indices()
    n = U.shape[0]
    cs = np.ascontiguousarray(U.indptr, dtype=np.int64)
    ri = np.ascontiguousarray(U.indices, dtype=np.int32)
    v = np.ascontiguousarray(U.data, dtype=np.complex128)
    if U.nnz > _INT32_MAX:
        raise RuntimeError(f"Accelerate: {U.nnz} entries exceed its 32-bit row indices")
    info = (ctypes.c_long * 2)()
    h = lib.gt_factor(n, cs.ctypes.data, ri.ctypes.data, v.ctypes.data, info)
    if not h:
        # the shim's own allocation failed: out of memory (the simulate.MemoryRelief ladder takes it). Accelerate's
        # statuses [Apple Accelerate, SparseStatus] are not: -1 factorisation failed, -2 the matrix is singular
        # (taken for "out of memory" until audit round 8, which sent a singular S down the memory ladder and left
        # its frequency "unsolved for memory"), -3 internal, -4 a parameter: SuperLU makes it (and says what it
        # finds), counted in `fallbacks` with this text
        if info[0] == ACCELERATE_NO_MEMORY:
            raise MemoryError(f"Accelerate ran out of memory ({n} unknowns)")
        what = {-1: "factorisation failed", -2: "the matrix is singular", -3: "internal error",
                -4: "parameter error"}.get(int(info[0]), "failed")
        raise RuntimeError(f"Accelerate factorisation: status {info[0]} ({what})")

    def solve(b):
        """Solve in place on a Fortran-ordered complex copy of b."""
        x = np.array(b, dtype=np.complex128, order="F", copy=True)
        k = 1 if x.ndim == 1 else x.shape[1]
        if lib.gt_solve(h, x.ctypes.data, k) != 0:
            raise RuntimeError("Accelerate solve failed")
        return x
    return Factor("accelerate", n, int(info[1]), int(info[1]) * 16, solve, lambda: lib.gt_free(h))
