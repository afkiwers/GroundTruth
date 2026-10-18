"""Progress reporting, cooperative cancellation and the sparse SPD solver."""

from __future__ import annotations

import math
import time

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla


class Cancelled(Exception):
    """The user cancelled the solve (raised at the next check())."""
    pass


class SolverError(Exception):
    """A problem with the model the user can fix (shown without a traceback)."""


class Progress:
    """Sends progress messages to ``sink`` (callable taking a dict) and checks for cancellation.

    Overall progress is split into weighted stages so the bar moves steadily.
    """

    def __init__(self, sink=None, cancel_event=None):
        """sink(msg) receives the messages; cancel_event (a threading or multiprocessing Event) cancels."""
        self.sink = sink or (lambda msg: None)
        self.cancel_event = cancel_event
        self._stage = ""
        self._lo = 0.0
        self._hi = 1.0
        self._last_send = 0.0
        self.t0 = time.perf_counter()

    def check(self):
        """Raise Cancelled if cancellation was asked for."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled()

    def stage(self, name: str, lo: float, hi: float):
        """A new stage, taking the overall progress from lo to hi."""
        self.check()
        self._stage, self._lo, self._hi = name, lo, hi
        self.update(0.0, name, force=True)

    def update(self, frac: float, text: str = "", force: bool = False):
        """The current stage is `frac` done; sent at most every 0.1 s unless forced (CG reports every iteration)."""
        now = time.perf_counter()
        if not force and now - self._last_send < 0.1:
            return
        self._last_send = now
        frac = min(1.0, max(0.0, frac))
        self.sink({
            "type": "progress",
            "stage": self._stage,
            "text": text or self._stage,
            "frac": frac,
            "overall": self._lo + (self._hi - self._lo) * frac,
            "elapsed": now - self.t0,
        })

    def log(self, text: str):
        """A line for the solver log."""
        self.sink({"type": "log", "text": text})

    def send(self, msg: dict):
        """Any other message."""
        self.sink(msg)


class SPDSolver:
    """AMG-preconditioned conjugate gradient [Hestenes 1952] with a reusable hierarchy (pyamg [Bell 2022]: smoothed
    aggregation [Vanek 1996], else Ruge-Stüben [Ruge 1987]).

    The AMG hierarchy is built once and reused while the matrix only changes
    moderately between coupled iterations (temperature-dependent coefficients),
    which is much cheaper than rebuilding it. If the reused hierarchy converges
    much more slowly than it did when fresh, it is rebuilt mid-solve.
    """

    DIRECT_LIMIT = 3000

    def __init__(self, label: str, method: str = "classical"):
        """label: names the system in progress messages; method: "classical" (Ruge-Stuben) or "aggregation"."""
        self.label = label
        self.method = method
        self.ml = None
        self.rate = None  # CG iterations per decade of residual reduction with a fresh hierarchy
        self.last_iters = 0

    def _build(self, A, progress):
        """Build the multigrid hierarchy for A (the expensive setup; the rate is measured again afterwards)."""
        import pyamg

        progress.update(0.0, f"{self.label}: building multigrid ({A.shape[0]:,} unknowns)", force=True)
        if self.method == "aggregation":
            # Smoothed aggregation with evolution strength: robust on long, thin copper paths
            # (a meandering trace is nearly one-dimensional), where classical coarsening stalls.
            self.ml = pyamg.smoothed_aggregation_solver(A, strength="evolution", max_coarse=800, keep=False)
        else:
            # Classical (Ruge-Stuben) AMG copes best with the strongly anisotropic
            # layered coefficients of PCB stacks (thin copper next to resin).
            self.ml = pyamg.ruge_stuben_solver(A, max_coarse=800, keep=False)
        self.rate = None

    def solve(self, A: sp.csr_matrix, b: np.ndarray, x0: np.ndarray | None, progress: Progress,
              tol: float = 1e-9, maxiter: int = 1000) -> np.ndarray:
        """Solve A x = b (A symmetric positive definite) to a relative residual `tol`, starting from x0 (the
        previous iteration's solution: a warm start). Small systems are solved directly (sparse LU)."""
        n = A.shape[0]
        if n == 0:
            return np.zeros(0)
        progress.check()
        if n <= self.DIRECT_LIMIT:
            return spla.spsolve(A.tocsc(), b)
        import pyamg

        if self.ml is None:
            self._build(A, progress)
        t_start = time.perf_counter()
        b_norm = max(float(np.linalg.norm(b)), 1e-300)
        r0 = float(np.linalg.norm(b - A @ x0)) if x0 is not None else b_norm
        decades = max(1.0, math.log10(max(r0, 1e-300) / (tol * b_norm)))
        res: list[float] = []  # one residual history across a rebuild, for progress

        def run_cg(x_start, iters):
            """CG preconditioned by one multigrid V-cycle per iteration: (x, info, iterations, decades of
            residual reduction reached)."""
            hist: list[float] = []

            def cb(_xk):
                """Per CG iteration: check for cancel, report progress as decades of residual reduction done."""
                progress.check()
                if hist:
                    res.append(hist[-1])
                if res and res[-1] > 0:
                    done = math.log10(max(r0, 1e-300) / res[-1]) / decades
                    progress.update(done, f"{self.label}: iteration {len(res)}, residual {res[-1] / b_norm:.1e}")

            x_, info_ = pyamg.krylov.cg(A, b, x0=x_start, tol=tol, maxiter=iters, M=self.ml.aspreconditioner(),
                                        callback=cb, residuals=hist)
            got = math.log10(max(hist[0], 1e-300) / max(hist[-1], 1e-300)) if len(hist) > 1 else 0.0
            return x_, info_, len(hist) - 1, got

        fresh = self.rate is None
        # A reused hierarchy can go stale when coefficients move a lot (hot copper): give it
        # a few times the fresh rate, then rebuild and carry on from where CG got to.
        cap = maxiter if fresh else min(maxiter, int(3 * self.rate * decades) + 30)
        x, info, iters, got = run_cg(x0, cap)
        if info > 0 and not fresh:
            progress.log(f"{self.label}: multigrid out of date after {iters} iterations; rebuilding")
            self._build(A, progress)
            fresh = True
            x, info, it2, got = run_cg(x, maxiter - iters)
            iters += it2
            iters_fresh = it2
        else:
            iters_fresh = iters
        if fresh and got > 0.5:
            self.rate = iters_fresh / got
        self.last_iters = iters
        if info > 0:
            progress.log(f"{self.label}: CG stopped after {iters} iterations "
                         f"(residual {res[-1] / b_norm if res else float('nan'):.1e})")
        progress.log(f"{self.label}: {n:,} unknowns, {iters} CG iterations, {time.perf_counter() - t_start:.2f} s")
        return x


def laplacian(n: int, a: np.ndarray, b: np.ndarray, g: np.ndarray) -> sp.csr_matrix:
    """Weighted graph Laplacian for edges (a, b) with conductances g.

    Each edge adds +g to both diagonal entries and -g to the two off-diagonal ones (the nodal-analysis stamp of a
    conductance); duplicate (row, col) pairs are summed when the COO data becomes CSR. Self-loops are dropped."""
    keep = a != b
    a, b, g = a[keep], b[keep], g[keep]
    rows = np.concatenate([a, b, a, b])
    cols = np.concatenate([b, a, a, b])
    vals = np.concatenate([-g, -g, g, g])
    return sp.csr_matrix((vals, (rows, cols)), shape=(n, n))
