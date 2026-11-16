"""The solver statistics (solver/stats.py, docs/SPEED.md): a Simulate keeps, per model, what the solver did and
where the time went, per scheduling round the threads, per phase its time; counting changes no number."""
import json
from types import SimpleNamespace

import numpy as np
from shapely.geometry import box

from boardgen import layer_change_board
from conftest import make
from groundtruth.analysis import simulate as sim
from groundtruth.config.settings import NetSpec
from groundtruth.solver import stats
from groundtruth.solver.peec import Branch, PlaneProblem, TraceSegment, build_grid


def _board(tmp_path):
    """Two nets on the layer-change board with the board-wide model (test_speed's order test)."""
    g = layer_change_board(None)
    g.ic("U3", (5.0, 4.0), "SIG2")
    g.ic("U4", (35.0, 4.0), "SIG2")
    g.track((5.0, 4.0), (35.0, 4.0), "SIG2", "F.Cu")
    for x in (5.0, 35.0):
        g.via((x, 5.6), "GND")
    return make(tmp_path, g, "stats", nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),
                                            NetSpec("SIG2", "U3.1", ["U4.1"], 55e6, 1e-9)),
                capacitance=True, solve_points=3, max_solves=5, cavity_max_nodes=12000)


def _numbers(res) -> str:
    """Every number of the routes' solves, emissions, worst cases and ideal references, as text."""
    return json.dumps([[r.solves, r.cavity_solves, r.emissions, r.worst, r.ideal] for r in res.routes], default=str)


def _microstrip():
    """A 30 mm microstrip over a 40 x 20 mm plane (test_speed's)."""
    g = build_grid(box(0, 0, 40, 20), (0, 0, 40, 20), 0.25)

    def P(x, y, z):
        """A point in metres relative to the grid's first node."""
        return np.array([(x - g.x0) * 1e-3, (y - g.y0) * 1e-3, z * 1e-3])
    trace = [TraceSegment(P(5, 10, 0), P(5, 10, 0.5), 2e-4), TraceSegment(P(5, 10, 0.5), P(35, 10, 0.5), 2e-4),
             TraceSegment(P(35, 10, 0.5), P(35, 10, 0), 2e-4)]
    return PlaneProblem(g, trace, g.nearest_node(5, 10), g.nearest_node(35, 10))


def test_a_simulate_keeps_what_the_solver_did_per_model_round_and_phase(tmp_path):
    board, sc, _ = _board(tmp_path)
    res = sim.simulate(board, sc.config)
    st = res.solver["stats"]
    json.dumps(st)                                  # plain data: the history writes it into run.json
    def total(label, key):
        """A count summed over the models of one kind (each route has a model and an ideal reference of its own)."""
        return sum(m[key] for m in st["models"] if m["label"] == label)
    assert {"board-wide", "route", "ideal reference"} <= {m["label"] for m in st["models"]}
    (bw,) = [m for m in st["models"] if m["label"] == "board-wide"]       # one, shared by both routes
    assert set(bw["uses"]) == {"SIG", "SIG2"} and bw["precond"] == "sheet" and bw["nodes"] > 0
    # every solve the routes report is counted on its model with its iterations, the ideal references apart
    assert total("route", "iterations") == sum(s["iterations"] for r in res.routes for s in r.solves)
    assert bw["iterations"] == sum(s["iterations"] for r in res.routes for s in r.cavity_solves)
    assert bw["solves"] == sum(len(r.cavity_solves) for r in res.routes)
    assert total("ideal reference", "solves") == len(res.routes)
    # where a solve's time went: its parts fit in their wholes
    assert bw["factorisations"] >= 1 and bw["factorise_s"] > 0 and bw["fill"] > 0
    assert bw["gmres_s"] > 0 and bw["lp_s"] > 0 and bw["lu_solve_s"] > 0 and bw["gs_s"] > 0
    assert bw["fft_s"] + bw["mac_s"] <= bw["lp_s"] + 1e-6 and bw["lp_s"] <= bw["matvec_s"] + 1e-6
    assert bw["lu_solve_s"] <= bw["precond_s"] + 1e-6
    assert bw["matvecs"] >= bw["iterations"] and bw["preconds"] >= bw["iterations"]
    phases = [p["phase"] for p in st["phases"]]
    assert phases[:2] == ["capacitor bias", "checks"] and {"models", "solves", "finishing", "issues"} <= set(phases)
    assert st["rounds"][0]["step"] == "planned"
    assert all(r["threads"] >= 1 and r["tasks"] >= 1 and r["wall_s"] >= 0 for r in st["rounds"])
    assert st["info"]["threads_cpu"] >= st["info"]["threads_memory"] >= 1
    assert st["totals"]["solves"] == sum(m["solves"] for m in st["models"])
    assert st["wall_s"] > 0 and st["machine"]["cpus"] >= 1


def test_counting_changes_no_number(tmp_path, monkeypatch):
    """The same Simulate with and without the statistics: every number bit-identical."""
    board, sc, _ = _board(tmp_path)
    counted = _numbers(sim.simulate(board, sc.config))
    monkeypatch.setattr(stats, "ENABLED", False)
    res = sim.simulate(board, sc.config)
    assert "stats" not in res.solver
    assert _numbers(res) == counted


def test_the_last_progress_message_sums_the_run_up(tmp_path):
    """A server job's last step (the Server jobs tab's details) says what the solver did."""
    board, sc, _ = _board(tmp_path)
    msgs = []
    sim.simulate(board, sc.config, progress=lambda m, f: msgs.append(m))
    assert msgs[-1].startswith("done: ")
    assert all(w in msgs[-1] for w in ("solves", "GMRES iterations", "factorisations", "solve thread"))


def test_a_derived_stack_counts_apart_and_a_freed_factorisation_counts_as_made_again():
    pp = _microstrip()
    base = pp.stack
    a, b = (0, 40, 30), (0, 44, 30)
    with stats.collect() as c:
        pp.solve(100e6)
        derived = base.with_branches([Branch(a, b, lambda f: 0.5 + 0j, "test part", "part")])
        pp.on(derived).solve(100e6)                 # a low-rank update of the base's factorisation
        base._ops.clear()                           # freed (drop_factorisations, OPERATOR_CACHE)
        pp.solve(100e6)                             # made again
        d = c.as_dict()
    assert stats.active() is None
    by = {m["derived"]: m for m in d["models"]}
    assert by[False]["factorisations"] == 2 and by[False]["remade"] == 1 and by[False]["solves"] == 2
    assert by[True]["updates"] == 1 and by[True]["factorisations"] == 0 and by[True]["solves"] == 1
    assert by[True]["extra_branches"] == 1


def test_no_collector_outside_a_run_and_the_outer_one_inside():
    assert stats.active() is None
    stats.label(object(), "x")                      # no collector: nothing to do
    stats.mark("x")
    with stats.collect() as outer:
        with stats.collect() as inner:
            assert inner is outer and stats.active() is outer
        assert stats.active() is outer
    assert stats.active() is None


def test_windows_is_not_asked_for_its_swap_traffic(monkeypatch):
    """psutil's swap_memory on Windows reports 0 / 0 and its first call took ~335 ms holding the GIL: a Run-checks
    job's collector froze the plugin window for 392 ms (gui_probe). On Windows `_swap` is None without calling it,
    and the run's statistics say so (swap figures None) instead of a measured zero."""
    from groundtruth.solver import stats
    asked = []

    class Psutil:
        """A psutil that notes being asked."""

        @staticmethod
        def swap_memory():
            asked.append(1)
            raise AssertionError("asked")
    monkeypatch.setattr(stats, "_psutil", lambda: Psutil)
    monkeypatch.setattr(stats.sys, "platform", "win32")
    assert stats._swap() is None and asked == []
    monkeypatch.setattr(stats.sys, "platform", "linux")
    assert stats._swap() is None and asked == [1]       # elsewhere it asks (the error is swallowed: None)


def test_a_simulate_keeps_its_memory_against_the_plan(tmp_path):
    """Planned against measured memory (the user's 8x jobs ran out on a 62.7 GB server, 2026-11-16): each phase's
    memory at its end, per round the memory planned at once beside the process's sampled extremes, per model the
    factorisation memory planned before its first factorisation (the estimate peec.lu_bytes gives from its unknowns,
    before any fill is measured), per group what it was planned to keep."""
    from groundtruth.solver import peec
    board, sc, _ = _board(tmp_path)
    st = sim.simulate(board, sc.config).solver["stats"]
    json.dumps(st)
    models = {p["phase"]: p for p in st["phases"]}["models"]
    assert models["rss_mb"] > 0 and models["private_mb"] > 0
    for r in st["rounds"]:
        m = r["memory"]
        assert m["planned_at_once_mb"] >= 0 and m["rss_max_mb"] >= m["rss_start_mb"] > 0
        assert m["private_max_mb"] >= m["private_start_mb"] > 0
    (bw,) = [m for m in st["models"] if m["label"] == "board-wide"]
    # the estimate from its unknowns alone: a model of the same size and kind with no fill measured yet
    fresh = SimpleNamespace(lu_nnz=0, precond_mode=bw["precond"], nb=bw["unknowns"] - bw["nodes"])
    assert bw["planned_first_mb"] == round(peec.lu_bytes(fresh) / 2 ** 20) > 0 and bw["fill"] > 0
    assert st["info"]["group_plan"] and all(g["keeps_mb"] >= 0 and g["runs"] >= 1 for g in st["info"]["group_plan"])


def test_the_round_sampler_sees_a_peak_between_the_rounds_own_samples():
    """RoundSampler's thread catches memory taken and given back inside the stretch it watches."""
    import time
    with stats.RoundSampler() as s:
        block = np.ones(64 * 2 ** 20 // 8)                  # 64 MB, touched
        time.sleep(3 * stats.SAMPLE_S)
        del block
    f = s.figures()
    assert f["rss_max_mb"] >= f["rss_start_mb"] + 50
