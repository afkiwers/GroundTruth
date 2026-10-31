"""The thermal solve (analysis/thermal.py, PCB Thermal's solver in groundtruth/thermal): GroundTruth's DC current
heating the board, checked against an analytic I²R and against PCB Thermal's own electrical model; the board
adapter against PCB Thermal's own parser; the parts' losses at an operating point."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

pytest.importorskip("pyamg")                       # the [thermal] extra
pytest.importorskip("numba")

from boardgen import BoardGen, boost_board, discrete_buck_board  # noqa: E402
from conftest import make  # noqa: E402

I, W, L, T_CU = 5.0, 4.0, 50.0, 0.035             # A, mm (strip width, length), mm (copper)
RHO = 1.724e-5                                    # Ω·mm at 20 °C


def strip_board():
    g = BoardGen(layers=2, size=(60.0, 20.0))
    g.zone("P", "F.Cu", [(5.0, 8.0), (55.0, 8.0), (55.0, 12.0), (5.0, 12.0)])
    for ref, x in (("J1", 5.5), ("J2", 54.5)):    # end pads as wide as the strip: little current crowding
        g.footprint(ref, "Pad", "Connector:Fake_1pin", (x, 10.0), [{"number": 1, "at": (0.0, 0.0), "net": "P",
                                                                  "size": (1.0, 4.0)}])
    return g


def strip_heat(board):
    """GroundTruth's route: a DC 'path' of uniform current density along the strip, through copper_heat."""
    from groundtruth.analysis.thermal import copper_heat
    from groundtruth.solver.peec import Grid
    d = 0.25
    nx, ny = int(L / d), int(W / d)
    node = np.ones((nx, ny), bool)
    g = Grid(5.0 + d / 2, 8.0 + d / 2, d, nx, ny, node, node[:-1], node[:, :-1])
    jx = np.full((nx, ny), I / (W * T_CU), np.float32)          # A/mm²
    path = NS(kind="converter", view_of=None, error=None, dc_vectors={"strip": {"F.Cu": (jx, 0 * jx)}},
              dc_grids={"strip": {"F.Cu": g}})
    return copper_heat(board, [path])


def test_dc_heat_is_i2r_and_heats_like_pcb_thermals_own_electrical_model(tmp_path):
    from groundtruth.analysis.thermal import solve_thermal
    from groundtruth.thermal.adapter import to_thermal_board
    from groundtruth.thermal.project import BoardSetup, Project, Terminal
    from groundtruth.thermal.solver.common import Progress
    from groundtruth.thermal.solver.coupled import run
    board, sc, _ = make(tmp_path, strip_board(), "strip", nets=())
    sc.doc["thermal"] = {"quality": "draft"}
    sc.save()
    from groundtruth.config.settings import SettingsFile
    cfg = SettingsFile.load(sc.path).config
    r_strip = RHO * L / (W * T_CU)
    heat = strip_heat(board)
    assert sum(float(p.sum()) for *_, p in heat.values()) == pytest.approx(I * I * r_strip, rel=1e-3)
    ours, _ = solve_thermal(board, cfg, heat, [])
    assert not ours.error and ours.copper_w20 == pytest.approx(I * I * r_strip, rel=1e-3)
    assert ours.heat_out["total"] == pytest.approx(ours.power_w, rel=1e-4)       # energy balance
    setup = BoardSetup(board_file="strip", terminals=[Terminal("in", ["J1.1"], "source", I),
                                                      Terminal("out", ["J2.1"], "sink")])
    setup.mesh.quality = "draft"
    ref = run(to_thermal_board(board), Project(boards=[setup]), 0, Progress(lambda m: None, None)).summary
    rise_ours, rise_ref = ours.t_max - 25.0, ref["t_max"] - 25.0
    assert rise_ref > 5                                                           # a real test: it heats
    assert rise_ours == pytest.approx(rise_ref, rel=0.05), (ours.t_max, ref["t_max"])
    assert ours.power_w == pytest.approx(ref["power_total"], rel=0.05)


def test_the_adapter_reproduces_pcb_thermals_own_board_reading(tmp_path):
    from groundtruth.adapters.file_source import load_board
    from groundtruth.thermal.adapter import to_thermal_board
    from groundtruth.thermal.kicad.board import load_board as pcb_load
    path = discrete_buck_board(out_connector=True).write(tmp_path / "buck.kicad_pcb")
    a, b = pcb_load(path), to_thermal_board(load_board(path))
    assert b.outline.area == pytest.approx(a.outline.area)
    assert [(l.name, l.kind, l.thickness) for l in b.stackup] == [(l.name, l.kind, l.thickness) for l in a.stackup]
    assert (len(b.pads), len(b.tracks), len(b.vias)) == (len(a.pads), len(a.tracks), len(a.vias))
    for layer in a.copper_layers:
        for get in (lambda x: sum(c.shape.area for c in x.copper if c.layer == layer),
                    lambda x: sum(p.shape.area for p in x.pads if layer in p.layers)):
            assert get(b) == pytest.approx(get(a), rel=1e-6, abs=1e-9)


def test_part_losses_follow_the_operating_point(tmp_path):
    """Synchronous buck at 24 → 5 V, 5 A, 500 kHz, 2 ns: D = 5/24. High side D·I²·R, one hard edge ½·V·I·t·f (at
    its turn-off the inductor current carries the switch node), and the output charge C_oss·V²·f, or Q_oss·V·f
    when Q_oss is given; low side (1−D)·I²·R; inductor I²·DCR. No R_DS(on) or DCR: left out, with a note. No dead
    time here (dead_rise, dead_fall 0): its reverse conduction is test_dead_time's."""
    from dataclasses import replace
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.thermal import part_losses
    board, sc, _ = make(tmp_path, discrete_buck_board(), "buck", nets=())
    spec = replace(detect_converters(board, sc.config)[0].spec, i_out=5.0, v_in=24.0, v_out=5.0, f_sw=500e3,
                   rise_time=2e-9, c_oss=600e-12, rds_on=0.010, dcr=0.020, dead_rise=0.0, dead_fall=0.0)
    cfg = replace(sc.config, converters=[spec])
    d = 5.0 / 24.0
    res = NS(net=spec.name, duty=d, v_in=24.0, dc=[])
    rows = {r["ref"]: r for r in part_losses(board, cfg, [res])[0]}
    edge = 0.5 * 24 * 5 * 2e-9 * 500e3
    assert rows["Q1"]["power_w"] == pytest.approx(d * 25 * 0.010 + edge + 600e-12 * 24 ** 2 * 500e3)
    assert "turn-on edge" in rows["Q1"]["what"] and "C_oss·V²" in rows["Q1"]["what"]
    assert rows["Q2"]["power_w"] == pytest.approx((1 - d) * 25 * 0.010)
    assert rows["L1"]["power_w"] == pytest.approx(25 * 0.020)
    with_q = replace(cfg, converters=[replace(spec, q_oss=38e-9)])               # the EPC2306's Q_OSS
    rows = {r["ref"]: r for r in part_losses(board, with_q, [res])[0]}
    assert rows["Q1"]["power_w"] == pytest.approx(d * 25 * 0.010 + edge + 38e-9 * 24 * 500e3)
    assert "Q_oss·V" in rows["Q1"]["what"]
    bare = replace(cfg, converters=[replace(spec, rds_on=None, dcr=None)])
    rows, notes = part_losses(board, bare, [res])
    assert {r["ref"] for r in rows} == {"Q1"} and any("rds_on" in n for n in notes) and any("dcr" in n for n in notes)


def test_switching_losses_of_a_diode_buck_and_a_boost(tmp_path):
    """With a rectifier diode both edges are hard and only the switch's own output energy is lost (≈ ½·Q_oss·V);
    the diode (1−D)·I·V_f. A boost's hard-switched transistor is its low side, switching V_out at the inductor
    current I_out / (1 − D); its diode is the high side."""
    from groundtruth.analysis.thermal import part_losses
    from groundtruth.config.settings import ConverterSpec
    board, sc, _ = make(tmp_path, boost_board(), "boost", nets=())
    f, t, q = 400e3, 5e-9, 20e-9
    buck = ConverterSpec("buck", "VIN", "GND", "SW", ["Q1"], ["D1"], f_sw=f, i_out=2.0, v_in=12.0, v_out=5.0,
                         rise_time=t, q_oss=q, rds_on=0.02, diode_vf=0.4)
    from dataclasses import replace
    d = 5.0 / 12.0
    rows = {r["ref"]: r for r in part_losses(board, replace(sc.config, converters=[buck]),
                                             [NS(net="buck", duty=d, v_in=12.0, dc=[])])[0]}
    assert rows["Q1"]["power_w"] == pytest.approx(d * 4 * 0.02 + 0.5 * 12 * 2 * 2 * t * f + 0.5 * q * 12 * f)
    assert "both edges" in rows["Q1"]["what"] and "½·Q_oss·V" in rows["Q1"]["what"]
    assert rows["D1"]["power_w"] == pytest.approx((1 - d) * 2 * 0.4)
    boost = ConverterSpec("boost", "VOUT", "GND", "SW", ["D1"], ["Q1"], topology="boost", f_sw=f, i_out=1.0,
                          v_in=12.0, v_out=24.0, rise_time=t, q_oss=q, rds_on=0.02, diode_vf=0.4)
    d = 0.5
    i_l = 1.0 / (1 - d)
    rows = {r["ref"]: r for r in part_losses(board, replace(sc.config, converters=[boost]),
                                             [NS(net="boost", duty=d, v_in=12.0, dc=[])])[0]}
    assert rows["Q1"]["power_w"] == pytest.approx(d * i_l ** 2 * 0.02 + 0.5 * 24 * i_l * 2 * t * f
                                                  + 0.5 * q * 24 * f)
    assert rows["D1"]["power_w"] == pytest.approx((1 - d) * i_l * 0.4) and "boost diode" in rows["D1"]["what"]


def test_the_plugins_thermal_process_gives_the_same_result_and_cancels(tmp_path):
    """The plugin solves in a child process (pyamg holds the GIL: a 528 ms window stall in a thread): the same
    temperatures as in-process, and a cancel ends the process within a second or two."""
    import threading
    import time
    from dataclasses import replace
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.thermal import run_thermal
    from groundtruth.analysis.thermal_proc import ThermalCancelled, run_in_process
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "buck", nets=(), solve_points=1)
    spec = replace(detect_converters(board, sc.config)[0].spec, i_out=5.0, v_in=24.0, v_out=5.0, rds_on=0.01)
    cfg = replace(sc.config, converters=[spec], thermal=replace(sc.config.thermal, quality="draft"))
    stages = simulate(board, cfg).converters
    here, _ = run_thermal(board, cfg, stages)
    seen = []
    there, issues = run_in_process(board, cfg, stages, progress=lambda m, f: seen.append(m))
    assert not there.error and there.t_max == pytest.approx(here.t_max, abs=1e-6)
    assert any("thermal" in m for m in seen)
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    t = time.perf_counter()
    with pytest.raises(ThermalCancelled):
        run_in_process(board, cfg, stages, cancel=cancel)
    assert time.perf_counter() - t < 6.0


# ------------------------------------------------------------------------------------------- memory watchdog
def test_the_memory_watchdog_stops_a_solve_over_its_limit(tmp_path):
    """PCB Thermal's rule: the solver may use 60 % of the RAM; past its limit the parent stops the child (counting
    the venv launcher's interpreter child too) with a message instead of letting the computer swap."""
    import time
    from dataclasses import replace
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.thermal_proc import MEMORY_SHARE, ThermalMemoryError, memory_limits, run_in_process
    import psutil
    limit, keep_free = memory_limits()
    assert limit == pytest.approx(psutil.virtual_memory().total / 2 ** 20 * MEMORY_SHARE)
    assert keep_free >= 512
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "buck", nets=(), solve_points=1)
    spec = replace(detect_converters(board, sc.config)[0].spec, i_out=5.0, v_in=24.0, v_out=5.0, rds_on=0.01)
    cfg = replace(sc.config, converters=[spec], thermal=replace(sc.config.thermal, quality="draft"))
    stages = simulate(board, cfg).converters
    t = time.perf_counter()
    with pytest.raises(ThermalMemoryError, match="over its limit"):
        run_in_process(board, cfg, stages, limits=(20.0, 0.0))        # 20 MB: Python alone needs more
    assert time.perf_counter() - t < 10.0


# ------------------------------------------------------------------------------------------- current capacity
def _strip_stage(scale=1.0, net="strip", sign=1.0, half=False):
    """A power stage's DC path along the strip (strip_heat's grid): current I·scale in +x (sign −1: −x), over
    the whole strip or its left half."""
    from groundtruth.solver.peec import Grid
    d = 0.25
    nx, ny = int(L / d), int(W / d)
    node = np.ones((nx, ny), bool)
    g = Grid(5.0 + d / 2, 8.0 + d / 2, d, nx, ny, node, node[:-1], node[:, :-1])
    jx = np.full((nx, ny), sign * scale * I / (W * T_CU), np.float32)
    if half:
        jx[nx // 2:] = 0.0
    return NS(kind="converter", net=net, view_of=None, error=None, duty=0.5, v_in=None, dc=[],
              dc_vectors={"path": {"F.Cu": (jx, 0 * jx)}}, dc_grids={"path": {"F.Cu": g}})


def _strip_config(sc, **thermal):
    from dataclasses import replace
    from groundtruth.config.settings import ConverterSpec
    specs = [ConverterSpec(n, "P", "GND", i_out=I, f_sw=500e3) for n in ("strip", "other")]
    return replace(sc.config, converters=specs, thermal=replace(sc.config.thermal, quality="draft", **thermal))


def test_capacity_follows_the_square_root_law_when_the_problem_is_linear(tmp_path, monkeypatch):
    """Analytic check: with temperature-independent copper (α = 0), fixed film coefficients and no radiation the
    temperature rise is proportional to the heat, which is ∝ I². So the current that gives 4x the operating
    point's rise is exactly 2x the operating current."""
    from dataclasses import replace
    from groundtruth.analysis.thermal import run_capacity, run_thermal
    from groundtruth.thermal import materials
    monkeypatch.setattr(materials, "COPPER", replace(materials.COPPER, alpha=0.0))
    board, sc, _ = make(tmp_path, strip_board(), "strip", nets=())
    cfg = _strip_config(sc, convection="fixed", radiation=False)
    stage = _strip_stage()
    nominal, _ = run_thermal(board, cfg, [stage])
    rise = nominal.t_max - cfg.thermal.ambient_c
    assert rise > 5
    cfg = replace(cfg, thermal=replace(cfg.thermal, capacity_c=cfg.thermal.ambient_c + 4 * rise))
    (view,) = run_capacity(board, cfg, [stage])
    c = view.capacity
    assert not view.error and not c["bound"]
    assert c["scale"] == pytest.approx(2.0, rel=5e-3), c
    assert c["i_capacity"] == pytest.approx(2 * I, rel=5e-3)
    assert abs(c["t_hottest"] - cfg.thermal.capacity_c) <= 0.25
    assert view.t_max == pytest.approx(cfg.thermal.ambient_c + 4 * rise, abs=0.3)
    assert c["solves"] <= 5                                            # the search is quick when it is linear
    assert view.label() == f"Thermal: strip at its capacity ({c['i_capacity']:.3g} A)"
    assert view.receiver == "capacity strip" and "copper" in c["limited_by"]


def test_capacity_inputs_rescale_shared_copper_with_the_cross_term(tmp_path):
    """Two stages in the same copper, one against the other on half the strip: the stage's copper heat at a
    scale s is |s·J_stage + J_other|², which capacity_inputs splits into a·s² + b·s + c (b < 0 where they oppose).
    Checked against copper_heat of the stage with its current maps scaled."""
    from groundtruth.analysis.thermal import capacity_inputs, copper_heat
    board, sc, _ = make(tmp_path, strip_board(), "strip", nets=())
    cfg = _strip_config(sc)
    other = _strip_stage(net="other", sign=-0.5, half=True)
    inputs = capacity_inputs(board, cfg, [_strip_stage(), other])
    a, b, c = inputs["terms"]["strip"]["F.Cu"]
    assert b.sum() < 0
    for s in (0.0, 0.6, 1.0, 1.7, 3.0):
        want = sum(float(p.sum()) for *_, p in copper_heat(board, [_strip_stage(scale=s), other]).values())
        assert float((a * s * s + b * s + c).sum()) == pytest.approx(want, rel=1e-6, abs=1e-12)   # float32 maps
    assert float(inputs["heat"]["F.Cu"][3].sum()) == pytest.approx(float((a + b + c).sum()), rel=1e-9)
    assert set(inputs["stages"]) == {"strip", "other", "all power stages"}
    a, b, c = inputs["terms"]["all power stages"]["F.Cu"]                # both scaled: all ∝ s², nothing left
    assert float(abs(b).sum()) == 0 and float(abs(c).sum()) == 0
    want = sum(float(p.sum()) for *_, p in copper_heat(board, [_strip_stage(scale=1.3),
                                                                _strip_stage(net="other", sign=-0.65, half=True)]).values())
    assert float((a * 1.69).sum()) == pytest.approx(want, rel=1e-6)


def test_capacity_inputs_scale_the_part_losses_with_the_load_current(tmp_path):
    """The losses as quadratics in the load-current scale s, from three part_losses evaluations: at s = 1.7 the
    high side has d·(1.7·I)²·R + ½·V·(1.7·I)·t_r·f + C_oss·V²·f (C_oss stays), the low side and the inductor
    their I² terms (no dead time: dead_rise, dead_fall 0)."""
    from dataclasses import replace
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.thermal import capacity_inputs
    board, sc, _ = make(tmp_path, discrete_buck_board(), "buck", nets=())
    spec = replace(detect_converters(board, sc.config)[0].spec, i_out=5.0, v_in=24.0, v_out=5.0, f_sw=500e3,
                   rise_time=2e-9, c_oss=600e-12, rds_on=0.010, dcr=0.020, dead_rise=0.0, dead_fall=0.0)
    cfg = replace(sc.config, converters=[spec])
    d, s, i = 5.0 / 24.0, 1.7, 1.7 * 5.0
    stage = NS(kind="converter", net=spec.name, view_of=None, error=None, duty=d, v_in=24.0, dc=[], dc_vectors={},
               dc_grids={})
    terms = capacity_inputs(board, cfg, [stage])["loss_terms"][spec.name]
    at = {ref: a * s * s + b * s + c for ref, (a, b, c) in terms.items()}
    assert at["Q1"] == pytest.approx(d * i * i * 0.010 + 0.5 * 24 * i * 2e-9 * 500e3 + 600e-12 * 24 ** 2 * 500e3)
    assert at["Q2"] == pytest.approx((1 - d) * i * i * 0.010)
    assert at["L1"] == pytest.approx(i * i * 0.020)
    assert terms["Q1"][2] == pytest.approx(600e-12 * 24 ** 2 * 500e3)     # at no load only C_oss·V²·f is left


def test_capacity_of_a_real_stage_is_confirmed_by_a_fresh_solve(tmp_path):
    """Nonlinear (natural convection, radiation, copper α) with conduction, switching and C_oss losses: a fresh
    thermal solve with the stage at the capacity current (its own mesh, planned for that heat) reaches the limit;
    a θJB makes the junction the limit; the plugin's thermal process gives the same answer."""
    from dataclasses import replace
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.thermal import _scaled, run_capacity, run_thermal
    from groundtruth.analysis.thermal_proc import capacity_in_process
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "buck", nets=(), solve_points=1)
    spec = replace(detect_converters(board, sc.config)[0].spec, i_out=3.0, v_in=24.0, v_out=5.0, rise_time=5e-9,
                   c_oss=300e-12, rds_on=0.02, dcr=0.02)
    cfg = replace(sc.config, converters=[spec], thermal=replace(sc.config.thermal, quality="draft"))
    stages = simulate(board, cfg).converters
    nominal, _ = run_thermal(board, cfg, stages)
    limit = nominal.t_max + 25.0
    cfg = replace(cfg, thermal=replace(cfg.thermal, capacity_c=limit))
    (view,) = capacity_in_process(board, cfg, stages)                 # the plugin's way: in the thermal process
    c = view.capacity
    assert not view.error and not c["bound"] and c["scale"] > 1.0 and view.mesh.get("memory_mb", 1) > 0
    assert abs(c["t_hottest"] - limit) <= 0.25 and "copper" in c["limited_by"]
    s = c["scale"]
    cfg2, _ = _scaled(cfg, stages, spec.name, s)
    full = [replace(st, dc=[dict(p, current_a=p["current_a"] * s) for p in st.dc],
                    dc_vectors={k: {L: (jx * s, jy * s) for L, (jx, jy) in v.items()} for k, v in st.dc_vectors.items()})
            for st in stages]
    fresh, _ = run_thermal(board, cfg2, full)
    assert fresh.t_max == pytest.approx(limit, abs=0.5), (fresh.t_max, limit, c)
    # with a large junction-to-board resistance on the high side its junction limits first, at a lower current
    hot = "Q1"
    cfg3 = replace(cfg, thermal=replace(cfg.thermal, theta_jb={hot: 100.0}))
    (junction,) = run_capacity(board, cfg3, stages)
    assert junction.capacity["limited_by"] == f"{hot}'s junction"
    assert junction.capacity["scale"] < s
    p = next(p for p in junction.parts if p["ref"] == hot)
    assert p["t_junction"] == pytest.approx(limit, abs=0.3)


def test_a_zero_capacity_names_the_stage_already_over_the_limit():
    """Over the limit with a stage at no load (the user's conv2 at "0 A", why?: conv1's Q301 at 143.7 °C): the
    stages whose own loss heats the part at the hottest spot, i.e. grows with their load, are named; a part whose
    loss is a constant of every stage, or a spot at no part, names none; the all-stages group never."""
    from groundtruth.analysis.thermal import _Capacity
    k = object.__new__(_Capacity)
    k.inputs = {"stages": {"conv1": {"i_out": 5.0}, "conv2": {"i_out": 5.0},
                           "all power stages": {"i_out": None, "members": {"conv1": 5.0, "conv2": 5.0}}},
                "loss_terms": {"conv1": {"Q301": (1.5, 0.9, 0.2), "Q303": (0.0, 0.0, 2.0), "R1": (0.0, 0.0, 0.3)},
                               "conv2": {"Q301": (0.0, 0.0, 2.4), "Q303": (1.4, 0.8, 0.0), "R1": (0.0, 0.0, 0.3)},
                               "all power stages": {"Q301": (1.5, 0.9, 0.0), "Q303": (1.4, 0.8, 0.0)}}}
    assert k.blocked_by("conv2", "copper at Q301 (F.Cu)") == ["conv1"]
    assert k.blocked_by("conv2", "Q301's junction") == ["conv1"]
    assert k.blocked_by("conv1", "copper at Q303 (In1.Cu)") == ["conv2"]
    assert k.blocked_by("conv2", "copper at Q303 (F.Cu)") == []       # its own part: its no-load heat
    assert k.blocked_by("conv2", "copper at R1 (F.Cu)") == []         # a constant of every stage
    assert k.blocked_by("conv2", "copper (inside the board)") == []


def test_capacity_bounds_zero_and_above(tmp_path):
    """A limit below the no-load temperature gives 0 A; one that ten times the current cannot reach gives
    "≥ 10x" (the search stops there)."""
    from dataclasses import replace
    from groundtruth.analysis.thermal import CAPACITY_MAX_SCALE, capacity_text, run_capacity, run_thermal
    board, sc, _ = make(tmp_path, strip_board(), "strip", nets=())
    cfg = _strip_config(sc)
    nominal, _ = run_thermal(board, cfg, [_strip_stage()])
    rise = nominal.t_max - 25.0                                       # ~9 K at 5 A
    far = replace(cfg, thermal=replace(cfg.thermal, capacity_c=25.0 + 5 * rise))
    (view,) = run_capacity(board, far, [_strip_stage(scale=0.1)])     # 0.5 A: 10x is the 5 A rise, under 5x it
    assert view.capacity["bound"] == "above" and view.capacity["scale"] == pytest.approx(CAPACITY_MAX_SCALE)
    assert capacity_text(view.capacity).startswith("≥ ")
    # the other stage alone already heats the strip past the limit
    hot = replace(cfg, thermal=replace(cfg.thermal, capacity_c=25.0 + 0.5 * rise))
    views = run_capacity(board, hot, [_strip_stage(), _strip_stage(net="other")])
    strip = next(v for v in views if v.capacity["stage"] == "strip")
    assert strip.capacity["bound"] == "zero" and capacity_text(strip.capacity) == "0 A"
    assert strip.capacity["blocked_by"] == []          # the spot is copper at no part: no stage to name
    # all stages together: both currents scaled by one factor; together they carry 2x the strip's current, so a
    # quarter of the heat at the load factor 1/2 gives the single stage's rise: half of it under 0.5·rise ≈ 0.35
    both = next(v for v in views if v.capacity["stage"] == "all power stages")
    c = both.capacity
    assert not c["bound"] and 0.2 < c["scale"] < 0.5 and set(c["currents"]) == {"strip", "other"}
    assert c["currents"]["strip"] == pytest.approx(c["scale"] * I)
    assert capacity_text(c) == f"{c['scale'] * 100:.0f} %"
    assert both.label().startswith("Thermal: all power stages at their capacity (")
