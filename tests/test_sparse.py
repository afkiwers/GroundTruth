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
