"""The speed-ups keep the results: GMRES with Givens rotations and an iteration budget, borrowed factorisations that
give up when a new one is cheaper, the compiled kernel multiply-add, the fix search's verification scope and its
progress plan."""
from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import box

from boardgen import layer_change_board
from conftest import make
from groundtruth.analysis.progress import Phases
from groundtruth.analysis.suggest import suggest_fixes, verify_scope
from groundtruth.solver import peec
from groundtruth.solver.peec import PlaneLayer, PlaneProblem, PlaneStack, TraceSegment, _gmres, build_grid


def test_gmres_solves_like_a_direct_solve_and_stops_at_its_budget():
    rng = np.random.default_rng(1)
    n = 60
    A = np.eye(n) * 4 + rng.standard_normal((n, n)) / np.sqrt(n) + 1j * rng.standard_normal((n, n)) / np.sqrt(n)
    b = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    x, its, ok = _gmres(lambda v: A @ v, b, 1e-10, restart=15, max_cycles=40)     # restarts on the way
    assert ok and its > 15
    assert np.linalg.norm(x - np.linalg.solve(A, b)) < 1e-8 * np.linalg.norm(x)
    D = np.arange(1.0, 201.0)                     # needs ~200 iterations: stopped after 7, unconverged
    x, its, ok = _gmres(lambda v: D * v, np.ones(200, complex), 1e-10, max_its=7)
    assert not ok and its == 7 and np.linalg.norm(D * x - 1) < np.sqrt(200)    # the best so far, not garbage


def test_gmres_goes_on_while_its_residual_falls_and_stops_when_it_stagnates():
    """Extra restarts beyond max_cycles while each one still lowers the residual (a short restart that converges
    slowly), none once it stalls: GMRES(m) on a matrix whose Krylov space needs all n directions stagnates."""
    rng = np.random.default_rng(2)
    n = 120
    A = np.eye(n) * 3 + rng.standard_normal((n, n)) / np.sqrt(n)
    b = rng.standard_normal(n).astype(complex)
    _, its, ok = _gmres(lambda v: A @ v, b, 1e-10, restart=4, max_cycles=3)
    assert not ok and its == 12
    x, its, ok = _gmres(lambda v: A @ v, b, 1e-10, restart=4, max_cycles=3, extra_cycles=40)
    assert ok and its > 12 and np.linalg.norm(A @ x - b) <= 1e-10 * np.linalg.norm(b)
    P = np.roll(np.eye(n), 1, axis=0)          # cyclic shift: GMRES(4) makes no progress at all
    _, its, ok = _gmres(lambda v: P @ v, np.eye(n)[0].astype(complex), 1e-10, restart=4, max_cycles=2,
                        extra_cycles=40)
    assert not ok and its <= 3 * 4


def microstrip():
    g = build_grid(box(0, 0, 40, 20), (0, 0, 40, 20), 0.25)

    def P(x, y, z):
        return np.array([(x - g.x0) * 1e-3, (y - g.y0) * 1e-3, z * 1e-3])
    trace = [TraceSegment(P(5, 10, 0), P(5, 10, 0.5), 2e-4), TraceSegment(P(5, 10, 0.5), P(35, 10, 0.5), 2e-4),
             TraceSegment(P(35, 10, 0.5), P(35, 10, 0), 2e-4)]
    return PlaneProblem(g, trace, g.nearest_node(5, 10), g.nearest_node(35, 10))


def test_a_borrowed_factorisation_gives_up_when_a_new_one_is_cheaper(monkeypatch):
    """A solve at 120 MHz borrows the 100 MHz factorisation (decided in an earlier round); when it takes more
    iterations than its budget, the solver factorises at 120 MHz and finishes from where it was: the same answer
    as a fresh solve."""
    pp = microstrip()
    pp.solve(100e6)
    home = pp.stack._made[(round(100e6, 3), False)]
    assert home.its and home.f_lu == 100e6                          # measured at home: the borrow budget's basis
    pp.stack.next_round()
    monkeypatch.setattr(peec._Operator, "borrow_budget", lambda self: 1)
    sol = pp.solve(120e6)
    assert pp.stack._ops[(round(120e6, 3), False)].f_lu == 120e6     # factorised at 120 MHz after giving up
    fresh = microstrip().solve(120e6)
    assert sol.Z_loop == pytest.approx(fresh.Z_loop, rel=1e-4)


def test_a_derived_stack_borrows_only_its_own_factorisations(monkeypatch):
    """A fix candidate's stack (with_branches: one more branch) on a base whose operator at f was borrowed: its
    operator is judged by the base's source (`ref`) but must never lend the base's smaller factorisation to its
    own next frequency. It did once (the plugin's Suggest fixes after Simulate freed the factorisations):
    "matmul: dimension mismatch (n,k=59814),(k=59815)". (No budget here, so the borrowed operators stay.)"""
    from groundtruth.solver.peec import Branch
    monkeypatch.setattr(peec._Operator, "borrow_budget", lambda self: None)
    pp = microstrip()
    base = pp.stack
    pp.solve(100e6)                                         # exact
    base.next_round()
    pp.solve(110e6)                                         # borrows the 100 MHz factorisation
    assert base.operator(110e6).f_lu == 100e6
    a, b = (0, 40, 30), (0, 44, 30)
    assert base.nid[a] >= 0 and base.nid[b] >= 0
    derived = base.with_branches([Branch(a, b, lambda f: 0.5 + 0j, "test part", "part")])
    exc = pp.on(derived)
    exc.solve(110e6)                                        # derived op on the borrowed base op
    op = derived.operator(110e6)
    derived.next_round()
    sol = exc.solve(105e6)                                  # borrows within the derived stack
    base100 = base._made[(round(100e6, 3), False)]
    assert op.f_lu == 100e6 and op.src is None and op.ref is base100 and op.made.ref is base100
    assert derived.operator(105e6).src is op.made
    fresh = microstrip()
    ref = fresh.on(fresh.stack.with_branches([Branch(a, b, lambda f: 0.5 + 0j, "test part", "part")])).solve(105e6)
    assert sol.Z_loop == pytest.approx(ref.Z_loop, rel=1e-4)


def test_borrowing_does_not_depend_on_what_finished_first_or_is_still_cached():
    """Which factorisation a frequency uses is decided from those of earlier rounds only, and kept: a frequency
    of the same round never borrows, whichever finished first; a freed factorisation is made again and lent
    the same way (it was the nearest one cached at that moment: two runs of a board differed by 0.002 dB)."""
    pp = microstrip()
    st = pp.stack
    pp.solve(100e6)
    pp.solve(105e6)                                         # same round: its own factorisation
    assert st.operator(105e6).f_lu == 105e6
    st.next_round()
    pp.solve(112e6)                                         # nearest earlier one: 105 MHz
    assert st.operator(112e6).f_lu == 105e6
    pp.solve(95e6)
    assert st.operator(95e6).f_lu == 100e6
    first = pp.solve(112e6).Z_loop
    st._ops.clear()                                         # freed (drop_factorisations, OPERATOR_CACHE)
    st.next_round()
    pp.solve(130e6)                                         # 112 MHz is no factorisation of its own: 105 MHz
    assert st.operator(130e6).f_lu == 105e6
    assert pp.solve(112e6).Z_loop == first                  # the same preconditioner, made again: bit-identical


def test_a_simulate_does_not_depend_on_the_order_its_solves_finish(tmp_path, monkeypatch):
    """Two nets 50 and 55 MHz apart share the board-wide model: their first solves are in one round. Run in
    order, then in reverse (what thread timing does to a parallel round), every solve takes the same iterations
    and every number is bit-identical. Before, the second of them borrowed the first one's factorisation:
    which one that was depended on the order."""
    import json
    from groundtruth.analysis import simulate as sim
    from groundtruth.config.settings import NetSpec
    g = layer_change_board(None)
    g.ic("U3", (5.0, 4.0), "SIG2")
    g.ic("U4", (35.0, 4.0), "SIG2")
    g.track((5.0, 4.0), (35.0, 4.0), "SIG2", "F.Cu")
    for x in (5.0, 35.0):
        g.via((x, 5.6), "GND")
    board, sc, _ = make(tmp_path, g, "order", nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),
                                                    NetSpec("SIG2", "U3.1", ["U4.1"], 55e6, 1e-9)),
                        capacitance=True, solve_points=3, max_solves=5, cavity_max_nodes=12000)

    def in_order(reverse):
        def run_parallel(tasks, work, threads):
            out = [None] * len(tasks)
            for k in (reversed(range(len(tasks))) if reverse else range(len(tasks))):
                out[k] = work(tasks[k])
            return out
        return run_parallel
    runs = []
    for reverse in (False, True):
        monkeypatch.setattr(sim, "run_parallel", in_order(reverse))
        res = sim.simulate(board, sc.config)
        assert len(res.routes) == 2 and not any(r.error for r in res.routes)
        runs.append(json.dumps([[r.solves, r.cavity_solves, r.emissions, r.worst] for r in res.routes],
                               default=str))
    assert runs[0] == runs[1]


def test_the_borrow_budget_counts_iterations():
    made = peec._Made(1e8, False, 0, f_lu=1e8, its=55)
    borrowed = peec._Operator(1.0, np.zeros(0), np.zeros(0), None, None, 1e8, src=made)
    assert borrowed.borrow_budget() == int(1.5 * 55) + peec.BORROW_EXTRA_ITS
    made.its = None
    assert borrowed.borrow_budget() is None                 # never solved at home: only a failed solve factorises
    derived = peec._Made(1.1e8, False, 0, f_lu=1e8, ref=peec._Made(1e8, False, 0, f_lu=1e8, its=40))
    on_derived = peec._Operator(1.0, np.zeros(0), np.zeros(0), None, None, 1e8, src=derived)
    assert on_derived.borrow_budget() == int(1.5 * 40) + peec.BORROW_EXTRA_ITS   # the base's source decides


def test_compiled_multiply_add_matches_numpy(monkeypatch):
    pytest.importorskip("numba")
    g = build_grid(box(0, 0, 12, 9), (0, 0, 12, 9), 0.5)
    st = PlaneStack([PlaneLayer(f"L{k}", z, g) for k, z in enumerate((0.0, 0.2e-3, 0.7e-3))])
    I = np.random.default_rng(2).standard_normal(st.ns) + 1j * np.random.default_rng(3).standard_normal(st.ns)
    assert peec._kernel_mac() is not None
    compiled = st.Lp_matvec(I)
    monkeypatch.setattr(peec, "USE_NUMBA", False)
    plain = st.Lp_matvec(I)
    assert np.abs(compiled - plain).max() <= 1e-12 * np.abs(plain).max()


def test_phases_split_what_is_left_and_never_go_back():
    seen = []
    ph = Phases(lambda m, f: seen.append((m, f)))
    ph.start(1, rest=3)                       # a quarter of the bar
    ph.report("a", 1.0)
    assert seen[-1][1] == pytest.approx(0.25)
    ph.start(1, rest=1, steps=2)              # half of what is left: 0.25 .. 0.625
    with ph.step("b"):                        # the bar moves when a step is done, not when it starts
        assert seen[-1] == ("b (1 of 2)", pytest.approx(0.25))
    assert seen[-1][1] == pytest.approx(0.4375)
    with ph.step("b"):
        pass
    assert seen[-1] == ("b (2 of 2)", pytest.approx(0.625))
    ph.report("late", 0.0)                    # a step reporting late cannot move the bar back
    assert seen[-1][1] == pytest.approx(0.625)
    nested = ph.nested("sim: ")
    nested("x", 1.0)
    assert seen[-1] == ("sim: x", pytest.approx(0.625))
    ph.start(0.0)
    ph.report("done", 1.0)
    assert seen[-1][1] == 1.0
    assert all(b[1] >= a[1] for a, b in zip(seen, seen[1:]))


def test_verify_scope_keeps_what_no_fix_can_change():
    """Simulated again: the route with a fix, a route near the limit, the power stage with a fix inside its model.
    Kept: a route far below the limit and a power stage far from every fix."""
    route = lambda net, margin: SimpleNamespace(net=net, worst={"margin_db": margin})       # noqa: E731
    stage = lambda name, bbox: SimpleNamespace(spec=SimpleNamespace(name=name),            # noqa: E731
                                                fine=SimpleNamespace(build=SimpleNamespace(bbox=bbox)))
    before = SimpleNamespace(routes=[route("FIXED", 40.0), route("NEAR", 12.0), route("FAR", 40.0)],
                             converter_setups=[stage("SW1", (0, 0, 10, 10)), stage("SW2", (50, 50, 60, 60))],
                             converters=[SimpleNamespace(net="SW1"), SimpleNamespace(net="SW2")],
                             issues=[SimpleNamespace(net="UNROUTED")])     # a net whose route was not found
    fix = SimpleNamespace(net="FIXED", modification=SimpleNamespace(at=(5.0, 5.0)), location=(5.0, 5.0))
    cfg = SimpleNamespace(rules=SimpleNamespace(emi_margin_db=6.0))
    scope, kept = verify_scope(before, [fix], cfg)
    assert scope == ["FIXED", "NEAR", "SW1"] and kept == ["FAR", "SW2", "UNROUTED"]


def test_fix_search_reports_steps_and_ends_at_one(tmp_path):
    board, sc, _ = make(tmp_path, layer_change_board(None), "prog", solve_points=1)
    seen = []
    res = suggest_fixes(board, sc.config, progress=lambda m, f: seen.append((m, f)))
    assert res.selected and res.after is not None
    fracs = [f for _, f in seen]
    assert all(b >= a for a, b in zip(fracs, fracs[1:])) and fracs[-1] == 1.0
    assert any(m.startswith("evaluating candidates:") and " of " in m for m, _ in seen)
    assert any(m.startswith("verifying the selected fixes together:") for m, _ in seen)
