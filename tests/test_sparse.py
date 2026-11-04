"""solver/sparse.py: the faster factorisation backends (PARDISO, Accelerate) against SuperLU, where they load; the
fallback to SuperLU when none does (docs/SPEED.md 5.3, 5.4)."""
import numpy as np
import pytest
import scipy.sparse as sp

from groundtruth.solver import sparse


def _complex_symmetric(n=400, seed=0):
    """A sparse complex symmetric (not Hermitian) matrix like the Schur complement: a 2D Laplacian-like real part
    plus an imaginary part, diagonally dominant."""
    rng = np.random.default_rng(seed)
    m = int(np.sqrt(n))
    L = sp.diags([4.0, -1.0, -1.0, -1.0, -1.0], [0, 1, -1, m, -m], shape=(n, n)).tocsc().astype(complex)
    return (L + 1j * sp.diags(rng.uniform(0.1, 1.0, n))).tocsc()


def test_the_backend_choice_and_superlu_fallback(monkeypatch):
    """configure() takes the four names and refuses others; "superlu" factorises nothing here (peec does)."""
    with pytest.raises(ValueError):
        sparse.configure("umfpack")
    old = sparse.chosen()
    try:
        sparse.configure("superlu")
        assert sparse.active() == "superlu" and sparse.factorise(_complex_symmetric()) is None
    finally:
        sparse.configure(old)


@pytest.mark.parametrize("name", ["pardiso", "accelerate"])
def test_a_backend_solves_like_a_direct_solve(name):
    """Where the backend loads: one and several right-hand sides solved to rounding, the same bits twice."""
    if isinstance(sparse._library(name), Exception):
        pytest.skip(f"{name} not available here")
    old = sparse.chosen()
    try:
        sparse.configure(name)
        S = _complex_symmetric()
        f = sparse.factorise(S)
        assert f is not None and f.backend == name and f.nnz > 0
        b = np.arange(S.shape[0], dtype=complex)
        x = f.solve(b)
        assert np.linalg.norm(S @ x - b) < 1e-10 * np.linalg.norm(b)
        assert np.array_equal(x, f.solve(b))
        X = f.solve(np.stack([b, 1j * b], axis=1))
        assert np.allclose(X[:, 0], x) and np.allclose(X[:, 1], 1j * x)
    finally:
        sparse.configure(old)


def test_the_backend_is_a_setting_a_preference_and_a_cli_flag(tmp_path):
    """analysis.sparse in the settings file (unknown names reported), Preferences' "Sparse solver" (per computer,
    "From the settings file" leaves it), and --sparse setting it for one run."""
    from types import SimpleNamespace
    from groundtruth.cli import _solver_options
    from groundtruth.config.settings import _sparse
    from groundtruth.ui.prefs_dialog import FROM_FILE, overrides
    errors = []
    assert _sparse(None, errors) is None and _sparse("PARDISO", errors) == "pardiso" and not errors
    assert _sparse("umfpack", errors) is None and errors
    assert overrides({"sparse": "accelerate"})["sparse"] == "accelerate"
    assert "sparse" not in overrides({"sparse": FROM_FILE}) and "sparse" not in overrides({})
    cfg = SimpleNamespace(analysis=SimpleNamespace(gpu="auto", sparse=None, resolution=1.0))
    _solver_options(cfg, SimpleNamespace(gpu=None, sparse="superlu", resolution=None))
    assert cfg.analysis.sparse == "superlu"


# ------------------------------------------------------------------ audit round 8: failure paths and memory planning
def test_no_choice_is_the_default_not_an_earlier_runs(monkeypatch):
    """configure(None) (a Simulate whose settings and Preferences name no backend) takes the process's default
    (GROUNDTRUTH_SPARSE, else auto), as config.settings documents; it used to keep an earlier Simulate's choice."""
    monkeypatch.setattr(sparse, "DEFAULT", "auto", raising=False)      # whatever this machine's variable says
    old = sparse.chosen()
    try:
        sparse.configure("superlu")
        sparse.configure(None)
        assert sparse.chosen() == "auto"
    finally:
        sparse.configure(old)


def test_planning_takes_the_backends_peak_and_superlus_rate_after_a_fallback(monkeypatch):
    """lu_bytes plans a backend's factorisation with at least the peak it reported (PARDISO's was up to 1.96x
    BACKEND_MEMORY x its fill on the reference board's models, the process's memory 1.11-1.17x that [A86]), and a
    model SuperLU factorises in a backend run (the backend failed on it) at SuperLU's commit, not the backend's."""
    from types import SimpleNamespace
    from groundtruth.solver import peec
    monkeypatch.setattr(sparse, "active", lambda: "pardiso")
    st = SimpleNamespace(lu_nnz=1000, lu_peak=96_000, precond_mode="diag", nb=10)
    assert peec.lu_bytes(st) >= 1.1 * 96_000                # before: 1000 x 16 x 1.5 = 24 000
    st.lu_peak = 10_000
    assert peec.lu_bytes(st) == int(1000 * 16 * peec.BACKEND_MEMORY)
    st.lu_superlu = True
    assert peec.lu_bytes(st) == int(1000 * 16 * peec.lu_commit())


def test_accelerates_singular_status_is_no_memory_error(monkeypatch):
    """Accelerate's status -2 is SparseMatrixIsSingular [Apple Accelerate]: SuperLU makes that factorisation (a
    fallback with the reason), it is not "out of memory" (which sent it down the memory ladder and left its frequency
    unsolved for memory); only the shim's own GT_NO_MEMORY is. A fake library: runs on any platform."""
    class Lib:
        """The shim's gt_factor failing with `status`."""
        def __init__(self, status):
            """Fail with this status."""
            self.status = status

        def gt_factor(self, n, cs, ri, v, info):
            """No handle; the status in info[0], as the shim reports a failure."""
            info[0] = self.status
            return None
    old = sparse.chosen()
    try:
        monkeypatch.setattr(sparse, "active", lambda: "accelerate")
        monkeypatch.setattr(sparse, "_library", lambda name: Lib(-2))
        before = sparse.fallbacks[0]
        assert sparse.factorise(_complex_symmetric(16)) is None      # (before: MemoryError)
        rep = sparse.report(since=(before, 0))
        assert rep["fallbacks"] == 1 and "singular" in rep["fallback_reason"]
        monkeypatch.setattr(sparse, "_library", lambda name: Lib(sparse.ACCELERATE_NO_MEMORY))
        with pytest.raises(MemoryError):
            sparse.factorise(_complex_symmetric(16))
    finally:
        sparse.configure(old)


def test_a_matrix_past_32_bit_indices_falls_back_instead_of_wrapping(monkeypatch):
    """PARDISO's indices are int32: a matrix with more entries would wrap silently in the cast and be read out of
    bounds (a crash); it is refused (SuperLU then makes it, or names its own limit). The limit lowered to test it."""
    monkeypatch.setattr(sparse, "_INT32_MAX", 5, raising=False)
    with pytest.raises(RuntimeError, match="32-bit"):
        sparse._pardiso(_complex_symmetric(16))


def _fake_backend(calls, fail=False, peak=3):
    """A backend standing in for PARDISO where mkl is missing (CI): scipy's splu behind sparse.Factor, reporting
    `peak` x its fill x 16 bytes as its peak; with `fail` every factorisation fails as a backend's error would."""
    from scipy.sparse.linalg import splu

    def factorise(S):
        """sparse.factorise's stand-in: a Factor, or None after counting a failure."""
        calls.append(S.shape[0])
        if fail:
            try:
                raise RuntimeError("PARDISO phase 12: error -3")
            except RuntimeError as e:
                with sparse._LOCK:          # as sparse.factorise counts a backend's failure
                    sparse.fallbacks[0] += 1
                    getattr(sparse, "last_fallback", [""])[0] = f"pardiso: {e}"
            return None
        lu = splu(S.tocsc(), permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.1, options={"SymmetricMode": True})
        return sparse.Factor("pardiso", S.shape[0], lu.nnz, lu.nnz * 16 * peak, lu.solve, lambda: None)
    return factorise


@pytest.fixture
def small_board(tmp_path):
    """The layer-change board's route model alone (no plane capacitance): a few seconds."""
    from boardgen import layer_change_board
    from conftest import make
    board, sc, _ = make(tmp_path, layer_change_board(None), "sparse", solve_points=2, max_solves=3)
    return board, sc.config


@pytest.fixture
def own_stacks(monkeypatch):
    """The test's stacks only (peec._STACKS), and no factorisation counted for other tests."""
    import weakref
    from groundtruth.solver import peec
    monkeypatch.setattr(peec, "_STACKS", weakref.WeakSet())
    return peec._STACKS


def test_a_runs_statistics_count_its_own_backend_fallbacks_and_plan_them_as_superlu(small_board, monkeypatch,
                                                                                  own_stacks):
    """Every backend factorisation fails: SuperLU makes them, the run's statistics count this run's fallbacks with
    the reason, not an earlier run's in the process (they read the process's total before the solves: always 0 in a
    fresh process, an earlier run's count in the plugin), and the models then plan as SuperLU."""
    from groundtruth.analysis.simulate import simulate
    from groundtruth.solver import peec, stats
    monkeypatch.setattr(stats, "ENABLED", True)
    calls = []
    monkeypatch.setattr(sparse, "active", lambda: "pardiso")
    monkeypatch.setattr(sparse, "factorise", _fake_backend(calls, fail=True))
    sparse.fallbacks[0] += 5                # an earlier run's in this process (the count only grows)
    board, cfg = small_board
    res = simulate(board, cfg)
    assert res.routes and not res.routes[0].error
    info = res.solver["stats"]["info"]["sparse"]
    assert calls and info["fallbacks"] == len(calls) and "error -3" in info["fallback_reason"]
    made = [st for st in own_stacks if getattr(st, "lu_nnz", 0)]
    assert made and all(st.lu_superlu and peec.lu_bytes(st) == int(st.lu_nnz * 16 * peec.lu_commit())
                        for st in made)


def test_the_least_fill_rung_keeps_the_backend(small_board, monkeypatch, own_stacks):
    """Memory short (every model switched to the least fill, as MemoryRelief's second rung does): a backend keeps
    making the factorisations (its fill is less than SuperLU's least, with no growth commit) instead of SuperLU,
    which took 3.7-10x PARDISO's memory on the reference board's models [A86]; the backend's reported peak plans the
    model's next ones."""
    from groundtruth.analysis.simulate import simulate
    from groundtruth.solver import peec
    calls, slu = [], []
    monkeypatch.setattr(sparse, "active", lambda: "pardiso")
    monkeypatch.setattr(sparse, "factorise", _fake_backend(calls))
    real = peec.hosted_splu
    monkeypatch.setattr(peec, "hosted_splu", lambda *a, **k: slu.append(1) or real(*a, **k))
    monkeypatch.setattr(peec.PlaneStack, "lu_low_memory", True, raising=False)
    board, cfg = small_board
    res = simulate(board, cfg)
    assert res.routes and not res.routes[0].error
    assert calls and not slu
    made = [st for st in own_stacks if getattr(st, "lu_peak", 0)]
    assert made and all(peec.lu_bytes(st) >= 1.1 * st.lu_peak and st.lu_peak == st.lu_nnz * 16 * 3 for st in made)


_OMP_CHECK = r'''
import sys
if sys.argv[1] == "torch":
    import torch                                    # its own OpenMP runtime, as solver/gpu.py imports it
import numpy as np, scipy.sparse as sp
from groundtruth.solver import sparse
sparse.configure("pardiso")
n = 50
S = (sp.diags([4.0, -1.0, -1.0], [0, 1, -1], shape=(n, n)).tocsc().astype(complex) + 1j * sp.eye(n)).tocsc()
F = sparse.factorise(S)
assert F is not None and F.backend == "pardiso"
assert np.linalg.norm(S @ F.solve(np.ones(n)) - 1) < 1e-10
if sys.platform == "win32":
    import ctypes
    print("iomp loaded" if ctypes.windll.kernel32.GetModuleHandleW("libiomp5md.dll") else "no iomp")
else:
    maps = open("/proc/self/maps").read() if sys.platform.startswith("linux") else ""
    print("iomp loaded" if "libiomp5" in maps else "no iomp")
'''


@pytest.mark.parametrize("first", ["none", "torch"])
def test_pardiso_brings_no_openmp_runtime_of_its_own(first):
    """Where mkl is installed: PARDISO runs on MKL's sequential layer, so loading it neither brings MKL's OpenMP
    runtime nor (after PyTorch, which brings its own) aborts the process with "OMP: Error #15" (exit code 3: every
    Simulate on a machine with both, the plugin or the server's worker gone). In a fresh process each."""
    import subprocess
    import sys
    if isinstance(sparse._library("pardiso"), Exception):
        pytest.skip("pardiso not available here")
    if first == "torch":
        pytest.importorskip("torch")
    import os
    from pathlib import Path
    src = str(Path(sparse.__file__).resolve().parents[2])           # this tree's groundtruth in the child too
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (src, os.environ.get("PYTHONPATH")) if p))
    done = subprocess.run([sys.executable, "-c", _OMP_CHECK, first], capture_output=True, text=True, timeout=300,
                          env=env)
    assert done.returncode == 0, done.stderr[-2000:]
    if first == "none":
        assert "no iomp" in done.stdout
    assert "Error #15" not in done.stderr


def test_pardiso_counts_the_pivots_it_perturbs():
    """Where mkl is installed: a singular S (a node with no branch) is no error for PARDISO, unlike SuperLU's
    "exactly singular": it perturbs the pivot (1e-8) and the run's statistics count it (report)."""
    if isinstance(sparse._library("pardiso"), Exception):
        pytest.skip("pardiso not available here")
    old = sparse.chosen()
    try:
        sparse.configure("pardiso")
        S = sp.diags([4.0 + 1j, -1, -1], [0, 1, -1], shape=(6, 6)).tolil()
        S[2, 2] = S[1, 2] = S[2, 1] = S[2, 3] = S[3, 2] = 0
        before = sparse.counts()
        F = sparse.factorise(S.tocsc())
        assert F is not None and F.perturbed >= 1
        assert sparse.report(since=before)["perturbed_pivots"] == F.perturbed
    finally:
        sparse.configure(old)
