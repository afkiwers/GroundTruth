"""The electro-thermal coupling (analysis/electrothermal.py; the user: "rising heat will increase via resistance which
will force other vias to carry more load", then "add this as a settable option, with a user settable max for rounds
... I don't want it to get stuck in the loop").

Validation against closed forms: the solver's resistance factors (PlaneStack.set_resistance_scale) scale a path's
resistance exactly by a uniform factor with the current pattern unchanged, and split the current between two equal
parallel tracks in the inverse ratio of their factors (parallel resistors, R(T) = R₂₀(1 + αΔT) [IEC 60028]); the
temperatures become factors 1 + α(T − 20 °C). The loop on a buck: it settles, the round cap ends it (said), a round
changing more than the one before ends it with the round before kept, a cancel ends it."""
import threading
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from boardgen import BoardGen, discrete_buck_board
from conftest import make
from groundtruth.analysis import electrothermal as et
from groundtruth.analysis.power import DC_F, _area_shares
from groundtruth.analysis.simulate import BoardModel
from groundtruth.solver.peec import RouteExcitation
from groundtruth.thermal.materials import COPPER


def _two_tracks(tmp_path):
    """Two 4 x 8 mm pours of net N on F.Cu, 40 mm apart, joined by two 0.5 mm tracks (y = 6 and 10); a pad on each
    pour feeds 1 A in and out. (board, the DC model on F.Cu at 0.1 mm, the injections)."""
    g = BoardGen(layers=2, size=(52.0, 16.0))
    for x0 in (2.0, 46.0):
        g.zone("N", "F.Cu", [(x0, 4.0), (x0 + 4, 4.0), (x0 + 4, 12.0), (x0, 12.0)])
    for y in (6.0, 10.0):
        g.track((5.0, y), (47.0, y), "N", width=0.5)
    for ref, x in (("J1", 3.5), ("J2", 48.5)):
        g.footprint(ref, "X", "Connector:Fake", (x, 8.0), [{"number": 1, "at": (0.0, 0.0), "net": "N",
                                                             "size": (1.5, 1.5)}])
    board, sc, _ = make(tmp_path, g, "tracks", nets=())
    model = BoardModel(board, sc.config)
    st = model.build((0.0, 0.0, 52.0, 16.0), ["F.Cu"], {"N"}, 0.1, False)
    pads = [board.find_pin("J1.1"), board.find_pin("J2.1")]
    inj = _area_shares(st, [pads[0]], "F.Cu", 1.0) + _area_shares(st, [pads[1]], "F.Cu", -1.0)
    return board, st, inj


def _track_currents(st, sol, x=26.0):
    """The current through each track at x (mm): the x strips crossing it, below and above y = 8."""
    g = st.grids[0]
    i = int(round((x - g.x0) / g.d))
    ys = g.y0 + np.arange(g.ny) * g.d
    col = sol.I_x[0, i, :].real
    return float(col[ys < 8.0].sum()), float(col[ys > 8.0].sum())


def test_a_uniform_factor_scales_the_resistance_and_keeps_the_currents(tmp_path):
    """Every strip x 1.25: the path's resistance exactly 1.25 times, the tracks' currents as before."""
    _, st, inj = _two_tracks(tmp_path)
    sol0 = RouteExcitation(st.plane, [], injections=inj).solve(DC_F)
    st.plane.set_resistance_scale(np.full(st.plane.ns, 1.25), None)
    sol1 = RouteExcitation(st.plane, [], injections=inj).solve(DC_F)
    assert sol1.Z_loop.real == pytest.approx(1.25 * sol0.Z_loop.real, rel=1e-6)
    a0, b0 = _track_currents(st, sol0)
    a1, b1 = _track_currents(st, sol1)
    assert a0 == pytest.approx(0.5, rel=1e-3) and (a1, b1) == (pytest.approx(a0, rel=1e-6), pytest.approx(b0, rel=1e-6))


def test_a_hotter_track_carries_less_in_the_inverse_ratio(tmp_path):
    """The lower track's copper 30 % more resistive (ΔT ≈ 76 K): the two split as parallel resistors, 1 : 1.3, to
    1.5 % (the pours' own spreading resistance, shared by neither track, is the difference)."""
    _, st, inj = _two_tracks(tmp_path)
    g = st.grids[0]
    xa, ya = st.plane.xa, st.plane.ya
    y_x = g.y0 + xa[:, 2] * g.d                      # each x strip's y
    in_track = (y_x < 8.0) & (g.x0 + xa[:, 1] * g.d > 6.5) & (g.x0 + xa[:, 1] * g.d < 45.5)
    y_y = g.y0 + (ya[:, 2] + 0.5) * g.d
    in_track_y = (y_y < 8.0) & (g.x0 + ya[:, 1] * g.d > 6.5) & (g.x0 + ya[:, 1] * g.d < 45.5)
    scale = np.concatenate([np.where(in_track, 1.3, 1.0), np.where(in_track_y, 1.3, 1.0)])
    st.plane.set_resistance_scale(scale, None)
    a, b = _track_currents(st, RouteExcitation(st.plane, [], injections=inj).solve(DC_F))
    assert a + b == pytest.approx(1.0, rel=1e-6)
    assert b / a == pytest.approx(1.3, rel=0.015)
    assert a == pytest.approx(1 / 2.3, rel=0.015)


def test_temperatures_become_resistance_factors(tmp_path):
    """A thermal map at 70 °C everywhere: every strip and via barrel 1 + α·50 K, other branches 1."""
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "scales", nets=())
    model = BoardModel(board, sc.config)
    from groundtruth.analysis.power import dc_board_build
    st = dc_board_build(model)
    th = SimpleNamespace(ambient_c=25.0, layer_grids=dict(zip(st.names, st.grids)),
                         sheet_current={n: np.full((g.nx, g.ny), 70.0) for n, g in zip(st.names, st.grids)})
    strips, branches = et.resistance_scales(st, th)
    f = 1 + COPPER.alpha * 50.0
    assert strips.shape == (st.plane.ns,) and np.allclose(strips, f)
    vias = [k for k, b in enumerate(st.plane.branches) if b.kind == "via" and not b.name.startswith("pad ")]
    others = [k for k in range(len(st.plane.branches)) if k not in vias]
    assert vias and np.allclose(branches[vias], f) and np.allclose(branches[others], 1.0)


@pytest.fixture(scope="module")
def buck(tmp_path_factory):
    """The discrete buck (8 A, 24 → 5 V) simulated once, its Thermal at draft quality: (board, config, stages)."""
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    tmp = tmp_path_factory.mktemp("et")
    board, sc, _ = make(tmp, discrete_buck_board(out_connector=True), "et", nets=(), solve_points=1)
    spec = replace(detect_converters(board, sc.config)[0].spec, i_out=8.0, v_in=24.0, v_out=5.0, rise_time=5e-9,
                   rds_on=0.02, dcr=0.02, dead_rise=0.0, dead_fall=0.0)
    cfg = replace(sc.config, converters=[spec], thermal=replace(sc.config.thermal, quality="draft"))
    stages = simulate(board, cfg).converters
    assert stages[0].dc_plan and stages[0].dc_plan["runs"]
    return board, cfg, stages


def _on(cfg, **kw):
    """The config with the coupling on (and these thermal settings)."""
    return replace(cfg, thermal=replace(cfg.thermal, electrothermal=True, **kw))


def test_the_coupling_settles_and_reports_the_vias_at_temperature(buck):
    """On: rounds until the change is within 1 %: settled in a few, the temperatures of the last round, the vias'
    currents at 20 °C and at temperature, the DC paths at temperature from the coupled solve; Simulate's results
    untouched."""
    from groundtruth.analysis.thermal import run_thermal
    board, cfg, stages = buck
    before = [dict(p) for p in stages[0].dc]
    th, _ = run_thermal(board, _on(cfg, electrothermal_rounds=5), stages)
    info = th.electrothermal
    assert info["settled"] and 1 <= info["rounds"] <= 5 and info["changes"][-1] <= 0.01
    assert info["vias"] and info["vias"][0]["current20_a"] is not None
    assert any(n.startswith("electro-thermal: settled in") for n in th.notes)
    assert th.dc_hot and all(r["factor"] > 1.0 for r in th.dc_hot)          # the copper is hotter than 20 °C
    assert [p["loss_w"] for p in stages[0].dc] == [p["loss_w"] for p in before]
    off, _ = run_thermal(board, cfg, stages)
    assert off.electrothermal == {}


def test_the_round_cap_ends_it_and_says_so(buck):
    """A tolerance nothing meets: exactly the most rounds, not settled, said in the notes."""
    from groundtruth.analysis.thermal import run_thermal
    board, cfg, stages = buck
    th, _ = run_thermal(board, _on(cfg, electrothermal_rounds=2, electrothermal_tolerance=1e-15), stages)
    assert th.electrothermal["rounds"] == 2 and not th.electrothermal["settled"]
    assert any("not settled after 2 round(s) of at most 2" in n for n in th.notes)


def test_a_growing_change_ends_it_with_the_round_before_kept(buck, monkeypatch):
    """A round changing more than DIVERGE times the one before (it should not happen): stopped, that round left
    out."""
    from groundtruth.analysis.thermal import run_thermal
    board, cfg, stages = buck
    seq = iter([0.2, 0.9, 0.9, 0.9])
    monkeypatch.setattr(et, "change", lambda a, b: next(seq))
    th, _ = run_thermal(board, _on(cfg, electrothermal_rounds=4), stages)
    info = th.electrothermal
    assert not info["settled"] and "changed more" in info["stopped"] and info["changes"] == [0.2, 0.9]


def test_a_cancel_ends_it(buck):
    """A cancel set before the rounds: the solver's Cancelled, no round made."""
    from groundtruth.analysis.thermal import solve_thermal, thermal_inputs
    from groundtruth.thermal.solver.common import Cancelled
    board, cfg, stages = buck
    th, issues = solve_thermal(board, cfg, *thermal_inputs(board, cfg, stages))
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        et.couple(board, _on(cfg), stages, th, issues, lambda r: (th, issues), cancel=stop)


def test_the_settings(tmp_path):
    """thermal.electrothermal on, 4 rounds, "2%" (or 0.02); rounds outside 1-10 refused."""
    from groundtruth.config.settings import ConfigError, SettingsFile
    _, sc, _ = make(tmp_path, discrete_buck_board(), "s", nets=())
    sc.doc["thermal"] = {"electrothermal": True, "electrothermal_rounds": 4, "electrothermal_tolerance": "2%"}
    sc.save()
    t = SettingsFile.load(sc.path).config.thermal
    assert (t.electrothermal, t.electrothermal_rounds) == (True, 4) and t.electrothermal_tolerance == pytest.approx(0.02)
    sc.doc["thermal"] = {"electrothermal_rounds": 11}
    sc.save()
    with pytest.raises(ConfigError, match="electrothermal_rounds"):
        SettingsFile.load(sc.path)


def test_the_coupling_runs_whole_on_the_compute_server_in_one_job(buck):
    """With a compute server the electro-thermal rounds run there too (the user, of the rounds' DC solves on the
    laptop between the server's thermal solves: "option 1 please"): run_in_process sends one "thermal_coupled" job
    with the stages' results made portable (thermal_proc.portable_results: through the server's protocol and its
    restricted unpickler), and the worker's coupling gives what the plugin's own does; without the coupling the plain
    "thermal" job, as before."""
    from groundtruth.analysis.thermal import run_thermal
    from groundtruth.analysis.thermal_proc import run_in_process
    from groundtruth.remote import worker
    from groundtruth.remote.protocol import dumps, loads
    board, cfg, stages = buck
    on = _on(cfg, electrothermal_rounds=5)

    class Server:
        """A compute server in this process: each job through the wire format and the worker."""
        kinds = []

        def run(self, kind, args, progress=None):
            self.kinds.append(kind)
            return worker.run(kind, loads(dumps(args)), progress=progress)
    server = Server()
    th, issues = run_in_process(board, on, stages, server=server)
    assert server.kinds == ["thermal_coupled"]
    here, _ = run_thermal(board, on, stages)
    assert th.electrothermal["rounds"] == here.electrothermal["rounds"] and th.electrothermal["settled"]
    assert th.t_max == pytest.approx(here.t_max, abs=1e-6)
    assert [r["r_mohm"] for r in th.dc_hot] == pytest.approx([r["r_mohm"] for r in here.dc_hot], rel=1e-9)
    assert any(n.startswith("electro-thermal: settled in") for n in th.notes)
    server.kinds.clear()
    run_in_process(board, cfg, stages, server=server)
    assert server.kinds == ["thermal"]
