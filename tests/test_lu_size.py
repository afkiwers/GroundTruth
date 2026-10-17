"""A factorisation past SuperLU's size limit (its counts are 32-bit: past ~2.1 billion entries it reports "gstrf
was called with invalid arguments"; a 1.9 M-node power stage at 25x failed a 30-minute job on the user's server):
it is made again with diagonal pivots only (the least fill) and the run goes on, noting it; too large even so,
the frequency is left unsolved with a note naming the limit, the job does not fail."""
import pytest

from boardgen import microstrip_board
from conftest import make
from groundtruth.analysis.simulate import simulate
from groundtruth.solver import peec


def overflow(when):
    """A stand-in for hosted_splu that fails like SuperLU's overflow for the pivot thresholds in `when`."""
    real = peec.hosted_splu
    calls = []

    def fake(S, **kw):
        """Fail as SuperLU does when its count overflows, else factorise."""
        calls.append(kw.get("diag_pivot_thresh"))
        if kw.get("diag_pivot_thresh") in when:
            raise SystemError("gstrf was called with invalid arguments")
        return real(S, **kw)
    return fake, calls


def test_an_overflowing_factorisation_falls_back_to_diagonal_pivots(tmp_path, monkeypatch):
    """The usual pivoting overflows: the factorisation is made with diagonal pivots, later ones of that model go
    there at once, the results agree with a normal run, and the result says so."""
    board, sc, _ = make(tmp_path, microstrip_board(), "m")
    normal = simulate(board, sc.config).routes[0]
    fake, calls = overflow({peec.LU_PIVOT_THRESHOLD})
    monkeypatch.setattr(peec, "hosted_splu", fake)
    res = simulate(board, sc.config)
    r = res.routes[0]
    assert 0.0 in calls and calls.count(peec.LU_PIVOT_THRESHOLD) <= calls.count(0.0)
    assert r.worst["margin_db"] == pytest.approx(normal.worst["margin_db"], abs=0.05)
    assert any("diagonal pivots only" in n for n in res.notes)


def test_a_model_too_large_even_so_leaves_its_frequencies_unsolved_with_the_reason(tmp_path, monkeypatch):
    """Both attempts overflow: no crash; the frequencies are unsolved and the note names the limit and the cure."""
    board, sc, _ = make(tmp_path, microstrip_board(), "m")
    fake, calls = overflow({peec.LU_PIVOT_THRESHOLD, 0.0})
    monkeypatch.setattr(peec, "hosted_splu", fake)
    res = simulate(board, sc.config)
    notes = " ".join(n for r in res.routes for n in r.notes)
    assert "too large for the sparse solver" in notes and "lower the resolution" in notes
    assert calls.count(0.0) == 1                        # once known too large, no further attempts
