"""'Use all cores' (analysis.all_cores, Preferences / --all-cores): auto threads on every physical core and several
frequencies per refinement round while threads would idle, on a budget raised as much; off: one per round, the
same frequencies on every computer."""
import os
from dataclasses import replace

import pytest

from boardgen import microstrip_board
from conftest import make
from groundtruth.analysis.simulate import ALL_CORES_K, MAX_THREADS, refine_width, simulate, solver_threads


def cfg_with(config, **analysis):
    """`config` with some analysis settings changed."""
    return replace(config, analysis=replace(config.analysis, **analysis))


def test_the_width_fills_idle_threads_and_is_one_when_off(tmp_path):
    """Off: one frequency per run and round whatever the threads. On: threads / runs refining, 1 to ALL_CORES_K;
    auto threads become every physical core (not at most MAX_THREADS)."""
    _, sc, _ = make(tmp_path, microstrip_board(), "w")
    off, on = sc.config, cfg_with(sc.config, all_cores=True)
    assert refine_width(off, 16, 1) == 1
    assert refine_width(on, 16, 1) == ALL_CORES_K and refine_width(on, 16, 8) == 2 and refine_width(on, 4, 8) == 1
    physical = max(1, (os.cpu_count() or 2) // 2)
    assert solver_threads(on) == physical and solver_threads(off) == min(MAX_THREADS, physical)
    assert solver_threads(cfg_with(on, threads=3)) == 3          # a number stays the user's choice


def test_all_cores_solves_more_frequencies_and_agrees_with_the_default(tmp_path):
    """One net, 8 threads: with all cores the refinement takes several frequencies per round on a larger budget
    (more solves than the default) and the worst margin stays close; the result says it ran that way."""
    # with plane capacitance (cavity resonances): the refinement has gaps to split; a smooth model stops early
    # either way and solves the same frequencies
    board, sc, _ = make(tmp_path, microstrip_board(layers=4), "w", capacitance=True)
    base = cfg_with(sc.config, threads=8)
    one = simulate(board, base)
    many = simulate(board, cfg_with(base, all_cores=True))
    r1, rn = one.routes[0], many.routes[0]
    # the refinement runs on the board-wide model here (its spectrum run): the solves of both models count
    assert len(rn.solves) + len(rn.cavity_solves) > len(r1.solves) + len(r1.cavity_solves)
    assert rn.worst["margin_db"] == pytest.approx(r1.worst["margin_db"], abs=1.0)
    assert any(n.startswith("Use all cores") for n in many.notes) and not any("all cores" in n for n in one.notes)
