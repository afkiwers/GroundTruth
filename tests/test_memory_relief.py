"""Memory under pressure: a Simulate works around a shortage instead of failing (the user, 2026-10-19, after an 8x
Simulate on the compute server failed "out of memory" with 45 GB free: "It should not fail, it should recognise the
shortage and proactive work around it to finish the simulation!").

The solver's memory gate (peec.GATE) admits factorisations by what they will commit, frees the cached ones and waits
for the others when memory is short, and takes the least-fill pivots when even that is not enough; what still runs
out goes through simulate.MemoryRelief's ladder (alone with the caches freed, then the least fill, then the
frequency left unsolved). The server names what ran out and keeps the worker's traceback."""
import threading
import time
import weakref

import pytest

from boardgen import layer_change_board
from conftest import make
from groundtruth.analysis.simulate import MemoryRelief, run_parallel, simulate
from groundtruth.core import sysmem
from groundtruth.solver import peec

MALLOC = "SUPERLU_MALLOC fails for buf in intCalloc() at line 173 in file memory.c"


@pytest.fixture
def own_stacks(monkeypatch):
    """The test's stacks only: the ladder's second rung must not switch the stacks other tests keep alive."""
    monkeypatch.setattr(peec, "_STACKS", weakref.WeakSet())


def test_the_gate_admits_waits_and_takes_the_least_fill(monkeypatch, own_stacks):
    """With room, a factorisation goes ahead at once. Without room while another is being made, it waits for that
    one to finish (counted); alone and still short, it goes ahead with the least fill (short=True)."""
    gate = peec._MemoryGate()
    monkeypatch.setattr(sysmem, "available_bytes", lambda: 100)
    with gate.admit(50) as short:
        assert not short                                    # 50 <= 0.8 x 100
    with gate.admit(500) as short:
        assert short                                        # nothing else being made: the least fill
    order = []
    first_in, release = threading.Event(), threading.Event()

    def first():
        with gate.admit(50):
            order.append("first in")
            first_in.set()
            release.wait(5)
        order.append("first out")

    def second():
        first_in.wait(5)
        with gate.admit(50) as short:                       # 50 + 50 > 80: waits for the first
            order.append(f"second in, short={short}")
    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    b.start()
    time.sleep(0.3)
    assert order == ["first in"]                            # the second is waiting
    release.set()
    a.join(5)
    b.join(5)
    assert order == ["first in", "first out", "second in, short=False"] and gate.waits == 1
    assert gate.held == 0 and gate.making == 0


def test_the_gate_frees_cached_factorisations_first(monkeypatch, own_stacks):
    """Short of memory, the gate drops every live stack's cached factorisations before anything else (they are
    made again identically when needed)."""
    class Stack:
        def __init__(self):
            self._lock, self._ops = threading.Lock(), {"a": 1, "b": 2}
    stacks = [Stack(), Stack()]
    for s in stacks:
        peec._STACKS.add(s)
    monkeypatch.setattr(peec.gpu, "release", lambda st: None)
    gate = peec._MemoryGate()
    monkeypatch.setattr(sysmem, "available_bytes", lambda: 100)
    with gate.admit(500):
        pass
    assert all(not s._ops for s in stacks) and gate.freed == 4


def test_the_ladder_frees_then_takes_the_least_fill_then_gives_up(monkeypatch):
    """A solve out of memory: again with the caches freed, again with the least fill, then the last rung (on_short:
    Simulate leaves the frequency unsolved); without on_short the MemoryError says what to do."""
    switched = []
    monkeypatch.setattr(peec, "low_memory_everywhere", lambda stacks=None: switched.append(stacks))
    monkeypatch.setattr(peec, "free_factorisations", lambda: 3)
    relief = MemoryRelief()

    def after_two(t, tries={}):
        tries[t] = tries.get(t, 0) + 1
        if tries[t] <= 2:
            raise MemoryError("Not enough memory to perform factorization.")
        return t * 10
    assert run_parallel([1, 2], after_two, 1, relief) == [10, 20]
    assert relief.low and relief.freed == 12 and len(switched) == 2 and relief.unsolved == 0

    def never(t):
        raise MemoryError("Not enough memory to perform factorization.")
    given_up = MemoryRelief(on_short=lambda task, e: f"unsolved {task}")
    assert run_parallel([1, 2, 3], never, 3, given_up) == ["unsolved 1", "unsolved 2", "unsolved 3"]
    assert given_up.unsolved == 3 and given_up.shorts == 3
    with pytest.raises(MemoryError, match="lower analysis.max_nodes"):
        run_parallel([1], never, 1)


@pytest.fixture(scope="module")
def route_board(tmp_path_factory):
    """The layer-change board with plane capacitance: a route model and a board-wide one, solved normally."""
    tmp = tmp_path_factory.mktemp("relief")
    board, sc, _ = make(tmp, layer_change_board(None), "relief", capacitance=True, solve_points=3, max_solves=5,
                        cavity_max_nodes=12000)
    return board, sc.config, simulate(board, sc.config)


def _short_unless_least_fill(real):
    """splu that runs out of memory unless asked for diagonal pivots only (the least fill)."""
    def splu(*a, **kw):
        if kw.get("diag_pivot_thresh", 1.0) != 0.0:
            raise RuntimeError(MALLOC)
        return real(*a, **kw)
    return splu


def test_a_simulate_short_of_memory_finishes_with_the_same_results(route_board, monkeypatch, own_stacks):
    """Every usual factorisation runs out of memory (as SuperLU reports it), in parallel: the ladder takes the least
    fill and the Simulate finishes with every frequency solved and the same results to GMRES's tolerance; its notes
    say what it did."""
    from dataclasses import replace
    board, cfg, base = route_board
    monkeypatch.setattr(peec, "splu", _short_unless_least_fill(peec.splu))
    cfg2 = replace(cfg, analysis=replace(cfg.analysis, threads=2))
    res = simulate(board, cfg2)
    a, b = base.routes[0], res.routes[0]
    assert not b.error and [s["f"] for s in b.cavity_solves] == [s["f"] for s in a.cavity_solves]
    assert b.worst["margin_db"] == pytest.approx(a.worst["margin_db"], abs=0.05)
    assert any("memory was short" in n and "least fill" in n for n in res.notes), res.notes
    assert not any("left unsolved" in n for n in res.notes)


def test_a_simulate_that_cannot_get_the_memory_still_finishes(route_board, monkeypatch, own_stacks):
    """No factorisation can be made at all: the Simulate still returns (each frequency unsolved with a note, the
    route with an error) instead of failing the whole run."""
    board, cfg, _ = route_board

    def never(*a, **kw):
        raise RuntimeError(MALLOC)
    monkeypatch.setattr(peec, "splu", never)
    res = simulate(board, cfg)
    assert res.routes and res.routes[0].error
    assert any("left unsolved" in n for n in res.notes), res.notes
    assert any("not enough memory" in n for n in res.routes[0].notes)


def test_the_worker_is_told_its_memory_limit(monkeypatch):
    """A job under the server's watchdog plans within its limit: available_bytes is at most the limit less what the
    process holds."""
    monkeypatch.setenv(sysmem.LIMIT_ENV, "1")                       # 1 MB: less than this process holds
    assert sysmem.limit_room() == 0 and sysmem.available_bytes() == 0
    monkeypatch.setenv(sysmem.LIMIT_ENV, str(10 ** 9))
    assert sysmem.limit_room() > 10 ** 14
    monkeypatch.delenv(sysmem.LIMIT_ENV)
    assert sysmem.limit_room() is None


def test_the_server_names_what_ran_out_and_keeps_the_traceback(monkeypatch, tmp_path):
    """A worker that runs out of memory itself: the job's error says the job ran out (it said "the thermal solve"
    for a Simulate) and its details keep the worker's traceback (it was dropped)."""
    from groundtruth.remote.client import Remote, RemoteError
    from groundtruth.remote.server import ComputeServer
    monkeypatch.setenv("GROUNDTRUTH_SERVER_TEST", "1")
    srv = ComputeServer("test-key-0123456789abcdef", "127.0.0.1", 0, workers=1, data_dir=tmp_path / "jobs")
    srv.start()
    try:
        ended = []
        with pytest.raises(RemoteError, match="the job ran out of memory") as e:
            Remote(f"127.0.0.1:{srv.port}", "test-key-0123456789abcdef", client="t").run(
                "sleep", ("out of memory",), watch=lambda ev, jid, info: ended.append(info) if ev == "end" else None)
        assert "thermal" not in str(e.value)
        detail = ended[-1].get("detail") or ""
        assert "Traceback" in detail and "the test worker ran out of memory" in detail
    finally:
        srv.stop()
