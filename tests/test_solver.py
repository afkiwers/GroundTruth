"""Solver validation against analytic results."""
import math

import numpy as np
import pytest
from shapely.geometry import box

from boardgen import microstrip_board
from conftest import make
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import NetSpec
from groundtruth.emi.radiation import loop_field
from groundtruth.emi.spectrum import microstrip_z0, trapezoid_harmonics
from groundtruth.solver.kernel import filament_mutual, strip_kernel, strip_self_numeric
from groundtruth.solver.peec import PlaneProblem, SolverError, TraceSegment, build_grid

H, W = 0.5, 0.2   # mm


def microstrip(geom, d=0.25, x0=5.0, x1=35.0, y=10.0):
    g = build_grid(geom, (0, 0, 40, 20), d)

    def P(x, yy, z):
        return np.array([(x - g.x0) * 1e-3, (yy - g.y0) * 1e-3, z * 1e-3])

    trace = [TraceSegment(P(x0, y, 0), P(x0, y, H), W * 1e-3), TraceSegment(P(x0, y, H), P(x1, y, H), W * 1e-3),
             TraceSegment(P(x1, y, H), P(x1, y, 0), W * 1e-3)]
    return g, PlaneProblem(g, trace, g.nearest_node(x0, y), g.nearest_node(x1, y))


def test_kernel_symmetry_and_self_term():
    K = strip_kernel(6, 6, 1e-3)
    assert np.allclose(K, K[::-1, ::-1])
    assert K[5, 5] == pytest.approx(strip_self_numeric(1e-3, 1e-3))
    assert K[5, 5] > K[6, 5] > K[7, 5] > 0
    # far away the strip kernel equals the filament formula
    assert K[5, 10] == pytest.approx(float(filament_mutual(1e-3, 0.0, 5e-3)), rel=1e-3)


def test_return_current_profile_matches_image_theory():
    g, pp = microstrip(box(0, 0, 40, 20))
    sol = pp.solve(1e8)
    i = int(round((20 - g.x0) / g.d))
    ys = g.y0 + np.arange(g.ny) * g.d - 10.0
    K = -sol.I_x[0, i, :].real / (g.d * 1e-3)
    analytic = (H * 1e-3) / (math.pi * ((H * 1e-3) ** 2 + (ys * 1e-3) ** 2))
    near = np.abs(ys) <= 2.0
    assert np.allclose(K[near], analytic[near], rtol=0.06)
    assert sol.I_x[0, i, :].sum().real == pytest.approx(-1.0, abs=1e-3)   # all current returns


def test_loop_inductance_matches_wheeler():
    _, pp = microstrip(box(0, 0, 40, 20))
    sol = pp.solve(1e8)
    wheeler = 2e-7 * math.log(8 * H / W + W / (4 * H)) * 0.030
    assert sol.L_loop == pytest.approx(wheeler, rel=0.03)
    assert np.linalg.norm(sol.m) == pytest.approx(30e-3 * H * 1e-3, rel=0.01)   # vertical loop area


def test_low_frequency_current_spreads():
    g, pp = microstrip(box(0, 0, 40, 20))
    lf, hf = pp.solve(1e3), pp.solve(1e8)
    i = int(round((20 - g.x0) / g.d))
    j = int(round((10 - g.y0) / g.d))
    assert abs(lf.I_x[0, i, j]) < 0.2 * abs(hf.I_x[0, i, j])   # DC: resistive spreading, not under the trace
    assert lf.L_loop > hf.L_loop


def test_slot_raises_inductance_monotonically():
    Ls = []
    for length in (0, 6, 12):
        geom = box(0, 0, 40, 20)
        if length:
            geom = geom.difference(box(19.5, 10 - length / 2, 20.5, 10 + length / 2))
        Ls.append(microstrip(geom)[1].solve(1e8).L_loop)
    assert Ls[0] < Ls[1] < Ls[2]


def test_split_from_edge_creates_horizontal_loop():
    geom = box(0, 0, 40, 20).difference(box(19.5, 4, 20.5, 20.1))
    sol = microstrip(geom)[1].solve(1e8)
    assert abs(sol.m[2]) > 2 * abs(sol.m[1])     # loop now mostly in the board plane


def test_fully_cut_plane_is_broken():
    geom = box(0, 0, 40, 20).difference(box(19.5, -1, 20.5, 21))
    with pytest.raises(SolverError):
        microstrip(geom)


def test_spectrum_and_field_helpers():
    f, a = trapezoid_harmonics(10e6, 1e-9, 3.3, 0.5, 1e9)
    assert f[0] == 10e6 and a[0] == pytest.approx(2 * 3.3 / math.pi, rel=1e-3)
    assert f[1] == 30e6                                                  # even harmonics vanish at 50 % duty
    assert 45 < microstrip_z0(0.3, 0.17, 4.5) < 60                        # ~50 ohm 4-layer microstrip
    # the small loop's E_phi, broadside: the far field x |1 + 1/(jkr)| (k r = 6.29 at 100 MHz and 3 m: +1.3 %)
    kr = 2 * math.pi * 100e6 * 3.0 / 299_792_458.0
    far = 1.316e-14 * 1e16 * 1e-6 / 3
    assert loop_field(100e6, 1e-6, 3.0, ground_reflection=False) == pytest.approx(far * math.sqrt(1 + 1 / kr ** 2))
    # where k r = 1 (47.7 MHz at 1 m, MIL-STD-461's distance) the induction term equals the far field: x sqrt 2
    f1 = 299_792_458.0 / (2 * math.pi)
    assert loop_field(f1, 1e-6, 1.0, ground_reflection=False) == pytest.approx(1.316e-14 * f1 ** 2 * 1e-6 * math.sqrt(2))


def test_a_radiated_limit_of_the_users_own():
    """analysis.radiated_limit (MIL-STD-461 RE102 for a platform, typed from the standard by the user): straight
    on a log-frequency axis between its points, nothing outside, at its distance, with MIL-STD-461's receiver
    bandwidths; the settings read it (units allowed, any order) and name it in the reports."""
    from groundtruth.config.settings import config_from_data
    from groundtruth.emi.radiation import MIL461_RBW, RadiatedLimit, emission_arrays, limits, rbw_bands
    lim = RadiatedLimit("test curve", ((2e6, 24.0), (20e6, 44.0)), 1.0, "mil461")
    vals, dist = limits([1e6, 2e6, 6.3245553e6, 20e6, 30e6], lim)
    assert math.isnan(vals[0]) and math.isnan(vals[4]) and vals[1] == pytest.approx(24.0)
    assert vals[2] == pytest.approx(34.0, abs=1e-6) and vals[3] == pytest.approx(44.0) and set(dist) == {1.0}
    assert rbw_bands(lim) is MIL461_RBW and rbw_bands("B") is not MIL461_RBW
    a = emission_arrays([10e6], [1e-3], [1e-4], lim)
    assert a["distance_m"][0] == 1.0 and math.isfinite(a["margin_db"][0])
    cfg = config_from_data({"analysis": {"radiated_limit": {"name": "MIL-STD-461G RE102 (ground, Army)",
                                                            "distance_m": 1, "points": [["18GHz", 69], ["2MHz", 24]]}}})
    r = cfg.analysis.radiated_limit
    assert r.points == ((2e6, 24.0), (18e9, 69.0)) and r.bands == "mil461"
    assert cfg.analysis.limit_spec() is r and cfg.analysis.limit_text() == "MIL-STD-461G RE102 (ground, Army)"
    assert config_from_data({}).analysis.limit_text() == "CISPR 32 class B"
    from groundtruth.config.settings import ConfigError
    with pytest.raises(ConfigError, match="radiated_limit.points"):
        config_from_data({"analysis": {"radiated_limit": {"points": [[1e6, 20]]}}})


def test_simulate_end_to_end(tmp_path):
    board, sc, _ = make(tmp_path, microstrip_board(slot=(19.5, 4, 20.5, 20.5)), "split",
                        nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),))
    res = simulate(board, sc.config)
    rr = res.routes[0]
    assert rr.reference_layer == "B.Cu" and not rr.error
    assert rr.l_ratio > 1.05 and rr.m_excess_db > 2.0
    assert rr.worst is not None and rr.emissions
    assert rr.sheet_current["B.Cu"].shape == (rr.grid.nx, rr.grid.ny)
    assert rr.layer_share["B.Cu"] > 0.5   # 2-layer board: the B.Cu plane carries the return


def test_out_of_memory_in_parallel_is_retried_alone():
    """refboard's full Simulate ran out of memory with ~2 GB per model solved in parallel and ~5 GB
    free: a solve that runs out of memory beside others is retried alone once they are done (results in order);
    one that fails even alone says what to do."""
    import threading
    import time
    from groundtruth.analysis.simulate import run_parallel
    lock, running, alone = threading.Lock(), [0], []

    def work(t):
        with lock:
            running[0] += 1
            crowded = running[0] > 1
        try:
            time.sleep(0.05)
            if t % 2 == 0 and crowded:
                raise MemoryError("Not enough memory to perform factorization.")
            if not crowded:
                alone.append(t)
            return 10 * t
        finally:
            with lock:
                running[0] -= 1
    assert run_parallel(list(range(6)), work, 3) == [0, 10, 20, 30, 40, 50]
    assert {0, 2, 4} <= set(alone)

    def never(t):
        raise MemoryError("Not enough memory to perform factorization.")
    with pytest.raises(MemoryError, match="lower analysis.max_nodes"):
        run_parallel([1, 2], never, 2)


def _host_idle(timeout: float = 10.0):
    """Wait until every factorisation host has worked through what was handed to it (frees are asynchronous)."""
    import threading
    import time
    from groundtruth.solver import peec
    deadline = time.monotonic() + timeout
    for host in list(peec._LU_IDLE):
        done = threading.Event()
        host.jobs.put(done.set)
        assert done.wait(max(deadline - time.monotonic(), 0.1)), "a factorisation host did not answer"


def test_a_factorisation_is_made_and_freed_in_its_host_thread(monkeypatch):
    """scipy's SuperLU books its C memory per thread: freed from another thread than the one that made it, a
    factorisation never gave its memory back (the user saw 5.4 GB kept after a failed Simulate). hosted_splu makes
    it in a host thread, which also frees it, wherever it is dropped; solving works from any thread; an error
    reaches the caller."""
    import threading
    from groundtruth.solver import peec
    seen = {}

    class Fake:
        def __init__(self, S, **kw):
            if kw.get("fail"):
                raise RuntimeError("SUPERLU_MALLOC fails for buf")
            seen["made"] = threading.current_thread().name

        def solve(self, rhs):
            return 2 * rhs

        def __del__(self):
            seen["freed"] = threading.current_thread().name
    monkeypatch.setattr(peec, "splu", Fake)
    box = []
    t = threading.Thread(target=lambda: box.append(peec.hosted_splu(None)), name="a solve thread")
    t.start()
    t.join()
    lu = box.pop()
    assert lu.solve(np.ones(3)).tolist() == [2.0, 2.0, 2.0]
    del lu                                          # dropped here, in the main thread
    _host_idle()
    assert seen["made"] == seen["freed"] == "groundtruth-lu"
    # The worst interleaving, forced: the host runs each job before put() returns (in a Simulate, freeing an
    # operator's other big arrays gave Python time to switch to the host there). The old __del__ handed the host
    # the factorisation while still holding it in a local: the host let go first, the local was the last
    # reference, and it was freed in the dropping thread (+330 MB per Simulate on a test board).
    class Eager:
        def __init__(self, q):
            self.q = q

        def put(self, job):
            self.q.put(job)
            ran = threading.Event()
            self.q.put(ran.set)
            ran.wait()

        def get(self):
            return self.q.get()
    for host in list(peec._LU_IDLE):
        monkeypatch.setattr(host, "jobs", Eager(host.jobs))
    freed = []
    monkeypatch.setattr(Fake, "__del__", lambda self: freed.append(threading.current_thread().name))
    for _ in range(5):
        lu = peec.hosted_splu(None)
        del lu
    _host_idle()
    assert freed == ["groundtruth-lu"] * 5, freed
    with pytest.raises(RuntimeError, match="SUPERLU_MALLOC"):
        peec.hosted_splu(None, fail=True)


def test_factorisations_dropped_in_another_thread_give_their_memory_back():
    """The real SuperLU: made in a worker thread, solved and dropped in the main thread, 5 rounds. Before
    hosted_splu every round kept its ~80 MiB (Windows, scipy 1.18)."""
    import threading
    psutil = pytest.importorskip("psutil")
    import scipy.sparse as sp
    from groundtruth.solver import peec
    if not hasattr(psutil.Process().memory_info(), "private"):
        pytest.skip("private bytes are a Windows figure")
    n = 120
    T = sp.diags([-1.0, 4.0, -1.0], [-1, 0, 1], shape=(n, n))
    M = (sp.kron(sp.eye(n), T) + sp.kron(sp.diags([-1.0, -1.0], [-1, 1], shape=(n, n)), sp.eye(n))
         + 1j * sp.eye(n * n)).tocsc()
    growth = []
    for _ in range(5):
        before = psutil.Process().memory_info().private
        box = []
        t = threading.Thread(target=lambda: box.append(peec.hosted_splu(M, permc_spec="MMD_AT_PLUS_A")))
        t.start()
        t.join()
        box[0].solve(np.ones(n * n, complex))
        box.clear()
        _host_idle()
        growth.append((psutil.Process().memory_info().private - before) / 2**20)
    assert sum(growth[1:]) < 40, [f"{g:.0f} MiB" for g in growth]


def test_a_solutions_state_is_rebuilt_exactly_from_its_currents_and_potentials(tmp_path, monkeypatch):
    """Solutions no longer store the solver's unknown vector (a copy of their currents and potentials, ~half a
    solution's memory); Solution.state_for rebuilds it for the fix search's warm starts: every number the same as
    the solver's own vector, plane-pair capacitance and via branches included."""
    from boardgen import layer_change_board
    from groundtruth.solver import peec
    captured = {}
    real = peec.PlaneStack.solve

    def solve(self, f, *a, **k):
        out = real(self, f, *a, **k)
        captured[(id(self), f)] = out[3]
        return out
    monkeypatch.setattr(peec.PlaneStack, "solve", solve)
    board, sc, _ = make(tmp_path, layer_change_board(None), "st", capacitance=True, solve_points=3, max_solves=4,
                        cavity_max_nodes=12000)
    res = simulate(board, sc.config)
    checked = 0
    for s in res.setups:
        for run in (s.fine, s.cavity):
            if run is None:
                continue
            for f, sol in run.ok().items():
                st = run.exc.stack
                assert sol.state is None
                assert np.array_equal(sol.state_for(st), captured[(id(st), f)])
                checked += 1
                assert run is not s.cavity or st.ncap > 0
    assert checked >= 4


def test_a_run_ends_with_its_own_exit_code_while_factorisations_are_alive():
    """A factorisation host (daemon thread) still alive at exit made Python print "Exception ignored on flushing
    sys.stdout: SystemError" and exit with 120 instead of the run's own code, whenever its output was
    redirected (a CLI run in CI or a log). _lu_exit ends the hosts first."""
    import subprocess
    import sys
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src"
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import numpy as np, scipy.sparse as sp\n"
            "from groundtruth.solver import peec\n"
            "n = 40\n"
            "M = (sp.kron(sp.eye(n), sp.diags([-1.0, 4.0, -1.0], [-1, 0, 1], shape=(n, n))) + 1j * sp.eye(n * n)).tocsc()\n"
            "lu = peec.hosted_splu(M)\n"
            "print(abs(lu.solve(np.ones(n * n, complex))).sum())\n"
            "sys.exit(3)\n" % str(src))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 3, out.stderr
    assert "Exception ignored" not in out.stderr, out.stderr


def test_parallel_solves_are_capped_by_the_free_memory():
    """analysis.threads "auto": no more parallel solves than fit in 80 % of the free memory (each: a factorisation
    being made, as SuperLU commits it, plus the GMRES basis), at least one; unknown free memory changes nothing."""
    from types import SimpleNamespace
    from groundtruth.analysis.simulate import memory_threads, solve_bytes
    board_wide = SimpleNamespace(nb=90_000, n_nodes=34_000, precond_mode="sheet")
    route = SimpleNamespace(nb=150_000, n_nodes=50_000, precond_mode="diag")
    need = solve_bytes(board_wide)
    assert 0.5e9 < need < 2e9                     # ~1 GB per board-wide solve at 1x on refboard
    assert memory_threads(4, [route, board_wide], None) == 4
    assert memory_threads(4, [route, board_wide], 100 * need) == 4
    assert memory_threads(4, [route, board_wide], int(2.6 * need)) == 2
    assert memory_threads(4, [route, board_wide], 1000) == 1
    assert memory_threads(4, [], 1000) == 4


def test_models_solved_in_groups_when_memory_is_short_keep_every_number(tmp_path, monkeypatch):
    """With too little free memory for every model's factorisations at once, Simulate solves the models in groups,
    one after the other (solve_groups): the route and power-stage models first, the board-wide one last. Models
    shared by several runs stay in one group, so every number is the same as solving them together: a route with
    plane capacitance (route model + board-wide), and two power stages (their own models with the conducted band
    refined, DC paths, the shared board-wide model)."""
    import json
    from dataclasses import replace
    from boardgen import discrete_buck_board, layer_change_board
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.config.settings import ConverterSpec
    from groundtruth.core import sysmem
    board, sc, _ = make(tmp_path, layer_change_board(None), "route", capacitance=True, solve_points=3, max_solves=5,
                        cavity_max_nodes=12000)
    buck_board, bsc, _ = make(tmp_path, discrete_buck_board(out_connector=True, post_stage=True), "buck", nets=(),
                              capacitance=True, solve_points=3, max_solves=5, cavity_max_nodes=12000)
    buck = detect_converters(buck_board, bsc.config)[0].spec
    buck.f_sw, buck.i_out, buck.v_in, buck.v_out = 300e3, 3.0, 12.0, 5.0
    post = ConverterSpec("post", "VOUT", "GND", switch=["U9"], f_sw=1e6, i_out=0.3, v_in=5.0, v_out=3.3,
                         rise_time=5e-9)
    bsc.set_converters([replace(buck), post])
    bsc.save()
    bsc = type(bsc).load(bsc.path)

    def timeless(x):            # the wall-clock time a stage took is the one thing that may differ
        if isinstance(x, dict):
            return {k: timeless(v) for k, v in x.items() if k != "seconds"}
        return [timeless(v) for v in x] if isinstance(x, list) else x

    def dump(res):
        return json.dumps([[r.solves, r.cavity_solves, r.emissions, r.worst] for r in res.routes]
                          + [timeless(c.summary()) for c in res.converters], default=str, sort_keys=True)
    for b, config in ((board, sc.config), (buck_board, bsc.config)):
        # plenty of memory: one group (the real free memory varies; a loaded machine split it in two in the suite)
        monkeypatch.setattr(sysmem, "available_bytes", lambda: 1 << 40)
        together = simulate(b, config)
        monkeypatch.undo()
        assert together.groups == 1
        monkeypatch.setattr(sysmem, "available_bytes", lambda: 1000)       # nothing fits: one group per model
        grouped = simulate(b, config)
        monkeypatch.undo()
        assert grouped.groups >= 2
        assert dump(grouped) == dump(together)
        assert together.converters or together.routes[0].cavity_solves


def test_solve_groups_keep_shared_models_together_and_the_board_wide_last():
    """solve_groups: everything in one group when it fits or the free memory is unknown; otherwise whole components
    (runs sharing a stack, or a derived stack's base), packed in order, the board-wide components last."""
    from types import SimpleNamespace as NS
    from groundtruth.analysis.simulate import factor_bytes, solve_groups

    def stack(nb, base=None):
        return NS(nb=nb, n_nodes=nb // 3, precond_mode="sheet", _base=base)
    a, b, wide = stack(10_000), stack(10_000), stack(30_000)
    derived = stack(10_000, base=a)
    s1, s2 = NS(cavity=None), NS(cavity=None)
    run = lambda st: NS(exc=NS(stack=st))
    r_wide1, r_wide2 = run(wide), run(wide)
    s1.cavity, s2.cavity = r_wide1, r_wide2
    runs = [(s1, r_wide1), (s1, run(a)), (s1, run(derived)), (s2, run(b)), (s2, r_wide2)]
    assert solve_groups(runs, 1, None) == [runs]
    assert solve_groups(runs, 1, 10**12) == [runs]
    groups = solve_groups(runs, 1, 1000)
    assert [[id(r.exc.stack) for _, r in g] for g in groups] == [[id(a), id(derived)], [id(b)], [id(wide)] * 2]
    # room for the first two components together: packed
    need = 2 * (factor_bytes(a) + factor_bytes(derived) + factor_bytes(b))
    from groundtruth.analysis.simulate import MEMORY_SHARE, solve_bytes
    avail = int((need + solve_bytes(wide) + 1) / MEMORY_SHARE) + 1
    assert [len(g) for g in solve_groups(runs, 1, avail)] == [3, 2]


def test_groups_count_what_models_really_keep_and_one_solve_in_flight_when_admitted():
    """solve_groups / kept_bytes (2026-10-25, docs/SPEED.md 5.2): a derived model keeps no factorisation of its own
    (its solves update its base's), a model solved at one frequency without refining keeps one, a refining one
    OPERATOR_CACHE; with the rounds admitted by memory one solve is in flight, not `threads` (refboard's job B ran as
    13 groups on the M2 because four of its DC model's estimate filled the room)."""
    from types import SimpleNamespace as NS
    from groundtruth.analysis import simulate as sim

    def stack(nb, base=None):
        return NS(nb=nb, n_nodes=nb // 3, precond_mode="sheet", _base=base)
    dc, wide = stack(30_000), stack(30_000)
    derived = stack(30_000, base=wide)
    s = NS(cavity=None, spectrum_run=None, cond=None)
    run = lambda st, fs: NS(exc=NS(stack=st), planned=list(fs))      # noqa: E731
    dc_runs = [(s, run(dc, [10.0])) for _ in range(4)]
    assert sim.kept_bytes(dc_runs) == sim.factor_bytes(dc)                 # one frequency: one factorisation
    r_wide, r_derived = run(wide, [1e8]), run(derived, [1e8, 2e8, 3e8])
    s.spectrum_run = r_wide
    assert sim.kept_bytes([(s, r_wide), (s, r_derived)]) == sim.peec.OPERATOR_CACHE * sim.factor_bytes(wide)
    runs = dc_runs + [(s, r_wide), (s, r_derived)]
    need = sim.run_bytes(r_wide)
    avail = int((sim.kept_bytes(runs) + need) / sim.MEMORY_SHARE) + 1
    assert len(sim.solve_groups(runs, 4, avail, admitted=True)) == 1          # room for everything with one solve
    assert len(sim.solve_groups(runs, 4, avail)) > 1                          # four in flight: no room


def test_freeing_finished_models_between_rounds_keeps_every_number(tmp_path):
    """Simulate frees the factorisations of models no run still refines after each round (solve_setups free_done):
    on the layer-change board with plane capacitance every number is the same as keeping them all (a freed one is
    made again identically), and the result's models can be counted and dropped."""
    import json
    from boardgen import layer_change_board
    board, sc, _ = make(tmp_path, layer_change_board(None), "free", capacitance=True, solve_points=3, max_solves=5,
                        cavity_max_nodes=12000)
    kept = simulate(board, sc.config, keep_factorisations=True)
    freed = simulate(board, sc.config)
    dump = lambda res: json.dumps([[r.solves, r.cavity_solves, r.emissions, r.worst] for r in res.routes], default=str)
    assert dump(kept) == dump(freed)
    assert freed.model_bytes() > 1e5
    freed.drop_models()
    assert freed.model_bytes() == 0 and freed.routes and freed.setups == []


def test_free_memory_is_the_smaller_of_ram_and_windows_commit(monkeypatch):
    """Windows refuses memory once the commit is used up, however much RAM is free (SuperLU commits ~6.5x what it
    touches: a server job failed at 50 % RAM): the parallel solves are planned by the smaller figure."""
    psutil = pytest.importorskip("psutil")
    from types import SimpleNamespace
    from groundtruth.core import sysmem
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=40 << 30))
    monkeypatch.setattr(sysmem, "commit_available", lambda: 6 << 30)
    assert sysmem.available_bytes() == 6 << 30
    monkeypatch.setattr(sysmem, "commit_available", lambda: None)          # not Windows, or unknown: the RAM
    assert sysmem.available_bytes() == 40 << 30
