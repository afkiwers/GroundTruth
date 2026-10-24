"""DC-DC power stages: hot-loop inductance, capacitor current sharing, conducted emission through a LISN,
detection and the settings entries. The physics is checked against analytic results."""
import math

import numpy as np
import pytest

from boardgen import discrete_buck_board, hot_loop_board
from conftest import make
from groundtruth.analysis.detect import detect_converters, detect_nets
from groundtruth.analysis.power import ringing_response
from groundtruth.analysis.simulate import MOUNT_L, simulate
from groundtruth.config.settings import ConfigError, ConverterSpec, SettingsFile, config_from_data
from groundtruth.emi.conducted import LISNS

MU0 = 4e-7 * math.pi
SPEC = ConverterSpec("hot", "VIN", "GND", switch=["U1"], f_sw=200e3, i_out=2.0, v_in=12.0, v_out=5.0,
                     rise_time=10e-9)


def make_conv(tmp_path, gen, name, converters, **analysis):
    board, sc, path = make(tmp_path, gen, name, nets=(), **analysis)
    sc.set_converters(list(converters))
    sc.save()
    return board, SettingsFile.load(sc.path), path


def run(tmp_path, gen, name, spec=SPEC, **analysis):
    board, sc, _ = make_conv(tmp_path, gen, name, [spec], **analysis)
    res = simulate(board, sc.config)
    r = res.converters[0]
    assert not r.error, r.error
    return r, res, sc


@pytest.fixture(scope="module")
def strip_loops(tmp_path_factory):
    out = {}
    for length in (20.0, 40.0):
        out[length] = run(tmp_path_factory.mktemp(f"hl{int(length)}"), hot_loop_board(length), "hl")[0]
    return out


def test_hot_loop_inductance_per_length_matches_a_wide_microstrip(strip_loops):
    """The switching current runs along a 4 mm VIN pour 0.235 mm over the GND plane: the hot-loop inductance grows
    by Hammerstad's per-length value of that microstrip (capacitor, IC and via terms cancel in the difference)."""
    per_m = (strip_loops[40.0].loop_nh - strip_loops[20.0].loop_nh) * 1e-9 / 0.020
    h, w = 0.235, 4.0
    hammerstad = MU0 / (w / h + 1.393 + 0.667 * math.log(w / h + 1.444))
    assert per_m == pytest.approx(hammerstad, rel=0.10), (per_m * 1e9, hammerstad * 1e9)


def test_the_series_lc_fit_removes_the_capacitor_reactance(strip_loops):
    """Im(Z)/ω at 30 MHz still contains -1/(ω²C) of the 10 µF capacitor (tiny) and the fit agrees with the
    100 MHz value; the loop is inductive."""
    r = strip_loops[20.0]
    by_f = {round(s["f"]): s["L_nH"] for s in r.solves}
    assert r.loop_nh == pytest.approx(by_f[100_000_000], rel=0.02)
    assert 1.0 < r.loop_nh < 20.0
    assert r.overshoot_v == pytest.approx(r.loop_nh * 1e-9 * 2.0 / 10e-9, rel=1e-6)


def test_identical_capacitors_placed_symmetrically_share_equally(tmp_path):
    caps = (("C1", "1u", "top", 2.0), ("C2", "1u", "bottom", 2.0))
    r = run(tmp_path, hot_loop_board(20.0, caps=caps), "sym")[0]
    share = {c["name"]: c for c in r.caps}
    assert share["C1"]["share_hf"] == pytest.approx(share["C2"]["share_hf"], rel=0.03)
    assert share["C1"]["share_hf"] + share["C2"]["share_hf"] == pytest.approx(1.0, rel=0.05)


def test_a_small_capacitor_next_to_the_switch_takes_the_fast_edges_the_bulk_one_the_ripple(tmp_path):
    caps = (("C1", "100n", "top", 17.0), ("C2", "10u", "end", 0.0))
    r = run(tmp_path, hot_loop_board(20.0, caps=caps), "near_far")[0]
    share = {c["name"]: c for c in r.caps}
    assert share["C1"]["share_hf"] > 0.6 > share["C2"]["share_hf"]          # inductance decides at 100 MHz
    assert share["C2"]["share_sw"] > 0.95                                    # capacitance at 200 kHz


def _analytic_transfer(sc, f):
    """I_lisn / I_loop: the loop current divides between C1 and the two LISNs in series."""
    m = sc.config.model_for("C1")
    w = 2 * math.pi * f
    z_c = complex(m.get("ESR"), w * (m.get("ESL") + MOUNT_L) - 1 / (w * m.get("C")))
    z_lisn = 2 * LISNS["cispr32"].impedance(f)[0]
    return abs(z_c / (z_c + z_lisn))


def test_lisn_current_divider_matches_the_capacitor_and_lisn_impedances(tmp_path):
    """Below C1's self-resonance the loop current divides between C1 (1 µF) and the LISNs (2 × 50 µH ∥ 50 Ω).
    Both branches leave the strip at its left end, so the strip and the switch belong to neither: the divider is
    C1's own impedance against the LISNs'. (Above it, C1's pad, via and plane path add ~0.4 nH.)"""
    spec = ConverterSpec(**{**SPEC.__dict__, "input_connector": ["J1.1", "J1.2"]})
    r, _, sc = run(tmp_path, hot_loop_board(20.0, caps=(("C1", "1u", "end", 0.0),), connector=True), "lisn",
                   spec=spec)
    assert r.conducted_standard.startswith("CISPR 32")
    tf = {t["f"]: t["ratio"] for t in r.input_transfer}
    for target in (150e3, 900e3):
        f = min(tf, key=lambda x: abs(math.log(x / target)))
        assert tf[f] == pytest.approx(_analytic_transfer(sc, f), rel=0.03), f
    # between solves the log-log interpolation the conducted estimate uses holds too (the refinement only splits
    # gaps where the slope bends)
    fs = np.array(sorted(tf))
    hs = np.array([tf[f] for f in fs])
    for f in (300e3, 600e3):
        interp = float(np.exp(np.interp(np.log(f), np.log(fs), np.log(hs))))
        assert interp == pytest.approx(_analytic_transfer(sc, f), rel=0.05), f
    # conducted rows: receiver readings within 150 kHz-30 MHz against the class B average limit
    assert r.conducted and r.conducted_worst is not None
    assert all(150e3 <= row["f"] <= 30e6 for row in r.conducted)
    lisn = next(c for c in r.caps if c["lisn"])
    assert lisn["share_hf"] < 0.05


def test_series_parts_on_the_input_path_are_in_the_model(tmp_path):
    """J1 reaches VIN through a 0R far outside the switch/capacitor/connector box (like a surge stopper beyond
    the connector). The model region must hold it, or the LISN sees no current at all."""
    spec = ConverterSpec(**{**SPEC.__dict__, "input_connector": ["J1.1", "J1.2"]})
    gen = hot_loop_board(20.0, caps=(("C1", "1u", "end", 0.0),), connector=True, far_series=True)
    r, _, sc = run(tmp_path, gen, "far", spec=spec)
    assert [n for n in r.notes if "ceramic, nominal capacitance" not in n
            and not n.startswith("input current")] == []      # the model closed
    tf = {t["f"]: t["ratio"] for t in r.input_transfer}
    f = min(tf, key=lambda x: abs(math.log(x / 150e3)))
    assert tf[f] == pytest.approx(_analytic_transfer(sc, f), rel=0.03)


SIGMA_CU = 5.8e7


def _strip_dc(tmp_path, length, cu_mm=0.035):
    """DC resistance and middle current density of the 4 mm PWR pour of test_pours' board, fed over its end pads."""
    from test_pours import WIDTH, X0, Y, pour_board
    from groundtruth.analysis.power import _area_shares, dc_path
    from groundtruth.analysis.simulate import BoardModel
    gen = pour_board(length)
    gen.cu_thickness = cu_mm
    board, sc, _ = make(tmp_path, gen, f"dc{int(length)}_{int(cu_mm * 1000)}", nets=())
    model = BoardModel(board, sc.config)
    st = model.build((X0 - 2, Y - 4, X0 + length + 2, Y + 4), ["F.Cu"], {"PWR"}, 0.1, False)
    inj = _area_shares(st, [board.find_pin("U1.1")], "F.Cu", 1.0) + \
        _area_shares(st, [board.find_pin("U2.1")], "F.Cu", -1.0)
    path, maps = dc_path(st, inj, 2.0, "strip")
    g = st.grids[0]
    i = int(round((X0 + length / 2 - g.x0) / g.d))
    j = int(round((Y - g.y0) / g.d))
    return path, maps["F.Cu"][i, j], WIDTH


def test_dc_resistance_of_a_pour_per_length_and_its_current_density(tmp_path):
    """R per length of a 4 mm × 35 µm pour is 1/(σ·w·t) (end effects cancel in the 20/40 mm difference), the
    current density in its middle is I/(w·t), and twice the copper halves both."""
    p20, k20, w = _strip_dc(tmp_path, 20.0)
    p40, k40, _ = _strip_dc(tmp_path, 40.0)
    t = 35e-6
    per_m = (p40["r_mohm"] - p20["r_mohm"]) * 1e-3 / 0.020
    assert per_m == pytest.approx(1 / (SIGMA_CU * w * 1e-3 * t), rel=0.03)
    assert k40 == pytest.approx(2.0 / (w * 0.035), rel=0.03)                 # A/mm² at 2 A
    assert p40["drop_mv"] == pytest.approx(p40["r_mohm"] * 2.0)
    assert p40["loss_w"] == pytest.approx(p40["r_mohm"] * 1e-3 * 4.0)
    p40h, k40h, _ = _strip_dc(tmp_path, 40.0, 0.070)
    assert p40h["r_mohm"] == pytest.approx(p40["r_mohm"] / 2, rel=0.02)
    assert k40h == pytest.approx(k40 / 2, rel=0.02)


def test_dc_paths_of_a_discrete_buck_flag_a_narrow_switch_node_track(tmp_path):
    """3 A from the FETs through the SW pour and a 1 mm wide track to L1: 3 / (1 mm × 35 µm) ≈ 86 A/mm² in the
    track, flagged; the input path from J1 to Q1 carries the input current: I·D plus the losses over V_in (here
    only the switching edge: 27 mW / 12 V)."""
    board, sc, _ = make(tmp_path, discrete_buck_board(), "dcbuck", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.0
    r, res, _ = run(tmp_path, discrete_buck_board(), "dcbuck2", spec=spec)
    paths = {p["name"]: p for p in r.dc}
    sw = paths["switch node → L1"]
    assert sw["current_a"] == 3.0 and sw["r_mohm"] > 0
    assert 60 < sw["sustained_a_mm2"] < 110
    inp = paths["input J1.1 → Q1"]
    assert inp["current_a"] == pytest.approx(3.0 * 3.0 / 12.0, rel=0.01)
    assert inp["current_a"] > 3.0 * 3.0 / 12.0
    assert inp["current_a"] == pytest.approx(r.input["i_in_a"])     # scaled to the estimate with the copper
    assert any(i.check == "current_density" for i in res.issues)


def test_dc_output_path_and_ground_return_of_a_buck(tmp_path):
    """The load at J2 draws 3 A through a 1 mm VOUT track from L1: its resistance is the track's squares between
    the pad edges (two 90° bends count 0.56 instead of 1 each) times 1/(σt). The load current returns through J2's
    GND via and splits in the plane by the duty cycle: (1-D)·I to the low side Q2 (three vias), D·I to the input
    connector J1 (the input current), exactly (Kirchhoff)."""
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "dcout", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    assert spec.output_connector == ["J2.1", "J2.2"]
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.0
    r, res, _ = run(tmp_path, discrete_buck_board(out_connector=True), "dcout2", spec=spec)
    paths = {p["name"]: p for p in r.dc}
    out = paths["output L1 → J2.1"]
    squares = (28.5 - 27.9) + 4.0 + (35.25 - 28.5) - 2 * (1 - 0.56)        # L1 pad edge … J2.1 pad edge
    assert out["current_a"] == 3.0
    assert out["r_mohm"] == pytest.approx(squares / (SIGMA_CU * 35e-6) * 1e3, rel=0.10)   # measured 5.03 vs 5.16
    gnd = paths["ground J2.2 → Q2 + J1.2"]

    def via_current(x, y):
        return sum(v["current_a"] for v in gnd["vias"] if math.dist((v["x"], v["y"]), (x, y)) < 0.5)
    assert gnd["current_a"] == 3.0
    assert via_current(36.0, 13.0) == pytest.approx(3.0, rel=0.01)
    assert via_current(3.0, 10.5) == pytest.approx(3.0 * 0.25, rel=0.01)
    assert sum(via_current(x, 24.5) for x in (16.0, 19.5, 23.0)) == pytest.approx(3.0 * 0.75, rel=0.01)
    # 3 A in a 1 mm × 35 µm track (86 A/mm², more at the bends) is flagged
    assert any(i.check == "current_density" and "output L1 → J2.1" in i.message for i in res.issues)
    # no series part: R is the copper's and the solder joints' it crosses (L1's, J2's pads [A65]), a small share
    assert out["parts"] == [] and out["r_copper_mohm"] + out["r_joints_mohm"] == pytest.approx(out["r_mohm"])
    assert 0 <= out["r_joints_mohm"] < 0.05 * out["r_mohm"]


def test_a_connector_pin_is_a_solid_conductor_not_a_plated_via(tmp_path):
    """A through-hole connector's soldered pin carries the current between the layers over the hole's whole
    cross-section, R = l/(σ·πr²) instead of the plating's l/(σ·2πr·t); and it is not a via for the IPC-2221
    check: refboard's XT30 pins read 113-187 % of a 25 µm plating's capacity."""
    from groundtruth.solver.peec import SIGMA_CU, VIA_PLATING, via_impedance
    l, d = 1.4e-3, 1.0e-3
    assert via_impedance(l, d, filled=True)(10.0).real == pytest.approx(l / (SIGMA_CU * math.pi * (d / 2) ** 2))
    assert via_impedance(l, d)(10.0).real == pytest.approx(l / (SIGMA_CU * math.pi * d * VIA_PLATING))
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector="tht"), "tht", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 20.0, 12.0, 3.0
    r, res, _ = run(tmp_path, discrete_buck_board(out_connector="tht"), "tht2", spec=spec)
    gnd = next(p for p in r.dc if p["name"].startswith("ground J2.2"))
    assert gnd["current_a"] == 20.0 and gnd["r_mohm"] > 0
    assert not [v for p in r.dc for v in p["vias"] if v["name"].startswith("pad ")]
    assert not [i for i in res.issues if i.check == "via_overload" and "pad J2" in i.message]


def test_a_ceramic_capacitor_at_its_dc_bias(tmp_path):
    """C_bias, a ceramic's capacitance at its DC voltage, replaces C in the circuit: C1 (10 µF) derated to 4 µF lets
    |ESR + 1/(jωC)| / |Z_LISN| more of the loop current reach the input at 100 kHz (where the LISNs' 2 × ~27 Ω
    dwarf C1 and the copper), and the note naming C1 as nominal goes. Optional: no model is incomplete without it."""
    from groundtruth.components.models import ComponentModel
    m = ComponentModel.from_dict({"model": "cap", "C": 10e-6, "ESR": 0.02, "ESL": 1e-9, "C_bias": 4e-6})
    w = 2 * math.pi * 1e6
    assert m.impedance(1e6) == pytest.approx(complex(0.02, w * 1e-9 - 1 / (w * 4e-6)))
    assert m.to_dict()["C_bias"] == 4e-6 and ComponentModel("cap", {"C": 1e-6, "ESR": 0.0, "ESL": 0.0}).missing_params() == []
    board, sc, _ = make(tmp_path, discrete_buck_board(), "bias", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.0
    ratios = []
    for name, c_bias in (("nominal", None), ("derated", 4e-6)):
        b, s, _ = make_conv(tmp_path, discrete_buck_board(), name, [spec])
        if c_bias:
            s.set_component("C1", ComponentModel("cap", {**s.config.model_for("C1").params, "C_bias": c_bias}))
            s.save()
        r = simulate(b, s.config).converters[0]
        assert any(n.startswith("C1: ceramic, nominal") for n in r.notes) == (c_bias is None)
        fs, hs = zip(*((t["f"], t["ratio"]) for t in r.input_transfer))
        ratios.append(float(np.exp(np.interp(np.log(1e5), np.log(fs), np.log(hs)))))
    w = 2 * math.pi * 1e5
    expect = abs(complex(0.02, w * 1e-9 - 1 / (w * 4e-6))) / abs(complex(0.02, w * 1e-9 - 1 / (w * 10e-6)))
    assert ratios[1] / ratios[0] == pytest.approx(expect, rel=0.03)


def test_a_stage_fed_by_another_bucks_output_is_named_as_cascaded(tmp_path):
    """refboard's LT3970 runs from the LM5008 buck's +12 V: its conducted note named surge-stopper FETs, when
    the reason is the buck in between. feeding_stage finds the buck whose inductor feeds the rail."""
    from dataclasses import replace
    from groundtruth.analysis.power import feeding_stage
    board, sc, _ = make(tmp_path, discrete_buck_board(), "cascade", nets=())
    buck = detect_converters(board, sc.config)[0].spec
    post = replace(buck, name="post", rail="VOUT", switch_node=None, high_side=[], low_side=[], switch=["U9"])
    cfg = replace(sc.config, converters=[buck, post])
    assert feeding_stage(board, cfg, post) == buck.name
    assert feeding_stage(board, cfg, buck) is None


def test_a_cascaded_stage_reports_no_conducted_emission(tmp_path):
    """The note says 'not estimated', and so must the result: conv4 on refboard printed 5.9 dBµV beside it
    (the connector was kept for that branch)."""
    from dataclasses import replace
    board, sc, _ = make(tmp_path, discrete_buck_board(post_stage=True), "post", nets=())
    buck = detect_converters(board, sc.config)[0].spec
    buck.f_sw, buck.i_out, buck.v_in, buck.v_out = 300e3, 3.0, 12.0, 5.0
    post = ConverterSpec("post", "VOUT", "GND", switch=["U9"], f_sw=1e6, i_out=0.3, v_in=5.0, v_out=3.3,
                         input_connector=list(buck.input_connector))
    b, s, _ = make_conv(tmp_path, discrete_buck_board(post_stage=True), "post2", [replace(buck), post])
    res = simulate(b, s.config, nets=["post"])
    r = res.converters[0]
    assert any("cascaded stage" in n and buck.name in n for n in r.notes)
    assert not r.conducted and r.conducted_worst is None
    assert not [i for i in res.issues if i.check == "conducted_emission"]


def test_dc_path_resistance_names_a_series_shunt(tmp_path):
    """A 10 mΩ current-sense resistor in the output path: the path's R is the copper's plus the resistor's, and the
    split names it (Tellegen: R = Σ|I_b|²·Re Z_b over the branches, so a part's share is its own I²R)."""
    from groundtruth.report.formats import dc_resistance
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True, shunt=True), "shunt", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    assert spec.output_connector == ["J2.1", "J2.2"]                     # found through the shunt
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.0
    r, _, _ = run(tmp_path, discrete_buck_board(out_connector=True, shunt=True), "shunt2", spec=spec)
    out = next(p for p in r.dc if p["name"] == "output L1 → J2.1")
    assert [p["name"] for p in out["parts"]] == ["R10"]
    assert out["parts"][0]["r_mohm"] == pytest.approx(10.0, rel=1e-3)
    assert out["parts"][0]["current_a"] == pytest.approx(3.0, rel=1e-3)
    # the part's share is its 10 mΩ (one branch between its soldered sides, simulate.part_lid); its joints' is apart
    assert out["parts"][0]["r_mohm"] == pytest.approx(10.0, rel=1e-6)
    assert "R10" in [j["name"] for j in out["joints"]]
    assert out["r_mohm"] == pytest.approx(out["r_copper_mohm"] + 10.0 + out["r_joints_mohm"], rel=1e-9)
    assert 0 < out["r_joints_mohm"] < 0.5
    assert 3.0 < out["r_copper_mohm"] < 6.0                              # the track, as without the shunt
    assert "(copper " in dc_resistance(out) and "R10 10.00, solder joints " in dc_resistance(out)


def test_suggest_stitches_an_unstitched_capacitor_ground(tmp_path):
    """C1's GND pad reaches the plane only through a via 6 mm away: the hot loop detours through it. The fix search
    proposes a GND via next to the pad, predicts a shorter loop, and the verification run confirms it."""
    from groundtruth.analysis.suggest import suggest_fixes
    gen = hot_loop_board(20.0, caps=(("C1", "10u", "end", 0.0),), gnd_via_mm=6.0)
    board, sc, _ = make_conv(tmp_path, gen, "stitch", [SPEC])
    res = suggest_fixes(board, sc.config)
    vias = [s for s in res.suggestions if s.net == "hot" and s.kind == "via"]
    assert vias, [(s.title, s.delta_l, s.note) for s in res.suggestions]
    via = vias[0]
    assert via.modification.net == "GND" and via.delta_l < -0.02           # helps on its own
    assert math.dist(via.modification.at, board.find_pin("C1.2").position) < 2.5
    # a capacitor right at U1 (also suggested) is the bigger fix; with it in place the via adds < 1 %
    assert via.selected or "less than 1 %" in via.note
    row = next(r for r in res.comparison if r.get("kind") == "converter")
    assert row["loop_nh"][1] < row["loop_nh"][0] * (1 + via.delta_l / 2)


def test_a_far_input_path_gets_its_own_model_and_leaves_the_hot_loop_alone(tmp_path):
    """With a node budget (20k) that one model of switch, capacitor, connector and the far 0R would have to spread
    thinner, the input path is modelled separately: the hot loop keeps the grid (and the inductance) it has without
    any connector, and the LISN divider is still right."""
    spec = ConverterSpec(**{**SPEC.__dict__, "input_connector": ["J1.1", "J1.2"]})
    gen = hot_loop_board(20.0, caps=(("C1", "1u", "end", 0.0),), connector=True, far_series=True)
    r, _, sc = run(tmp_path, gen, "split", spec=spec, max_nodes=20000)
    assert any("input path modelled separately" in n for n in r.notes), r.notes
    alone = run(tmp_path, gen, "alone", spec=SPEC, max_nodes=20000)[0]
    assert r.grid.d == pytest.approx(alone.grid.d)
    assert r.loop_nh == pytest.approx(alone.loop_nh, rel=1e-6)
    tf = {t["f"]: t["ratio"] for t in r.input_transfer}
    f = min(tf, key=lambda x: abs(math.log(x / 150e3)))
    assert tf[f] == pytest.approx(_analytic_transfer(sc, f), rel=0.03)
    names = {p["name"] for p in r.dc}
    # the DC paths are on the whole-board DC model (power.dc_board_build, DC_BUDGET x the node budget): never
    # coarser than the hot loop's grid
    assert "input J1.1 → U1" in names and r.dc_grids["input J1.1 → U1"]["F.Cu"].d <= r.grid.d * (1 + 1e-9)


def test_suggest_a_capacitor_right_at_the_switch(tmp_path):
    """The only input capacitor sits 20 mm from U1 at the far end of the VIN strip: a 100 nF capacitor next to U1
    closes the hot loop there. It is suggested, predicted to shorten the loop a lot and verified."""
    from groundtruth.analysis.suggest import suggest_fixes
    board, sc, _ = make_conv(tmp_path, hot_loop_board(20.0), "capfix", [SPEC])
    res = suggest_fixes(board, sc.config)
    caps = [s for s in res.suggestions if s.net == "hot" and s.kind == "cap"]
    assert caps, [(s.kind, s.title, s.note) for s in res.suggestions]
    best = min(caps, key=lambda s: s.delta_l)
    assert best.selected and best.delta_l < -0.3
    assert math.dist(best.modification.at, board.find_pin("U1.1").position) < 5.0
    row = next(r for r in res.comparison if r.get("kind") == "converter")
    assert row["loop_nh"][1] < 0.7 * row["loop_nh"][0]


def test_input_filter_advice_meets_every_harmonic():
    """−38 dB at 500 kHz and −30 dB at 1 MHz (6 dB wanted): 44 and 36 dB are missing, so f_c ≤ 500k·10^(−44/40)
    = 39.7 kHz binds. The smallest standard C keeping L ≤ 4.7 µH is 4.7 µF (L ≈ 3.4 µH). The 40 dB/decade
    attenuation above f_c then covers both harmonics."""
    from groundtruth.analysis.power import input_filter_advice
    rows = [{"f": 500e3, "margin_db": -38.0}, {"f": 1e6, "margin_db": -30.0}, {"f": 50e6, "margin_db": None}]
    a = input_filter_advice(rows, 6.0)
    assert a["f_c"] == pytest.approx(500e3 * 10 ** (-44 / 40))
    assert a["C"] == 4.7e-6 and a["L"] == pytest.approx(1 / ((2 * math.pi * a["f_c"]) ** 2 * 4.7e-6))
    assert a["L"] <= 4.7e-6 and a["R_damp"] == pytest.approx(math.sqrt(a["L"] / a["C"]))
    for r in rows[:2]:
        assert 40 * math.log10(r["f"] / a["f_c"]) >= 6.0 - r["margin_db"] - 1e-9
    assert input_filter_advice([{"f": 1e6, "margin_db": 10.0}], 6.0) is None


def test_greedy_selection_skips_redundant_vias():
    """Vias a and b close the same detour (together barely better than a alone), c is independent: the greedy
    pick takes a, then c, and leaves b."""
    from types import SimpleNamespace as NS
    from groundtruth.analysis.suggest import greedy_pick
    a, b, c = (NS(name="a", delta_db=-4.0, delta_l=-0.31), NS(name="b", delta_db=-4.0, delta_l=-0.30),
               NS(name="c", delta_db=-1.0, delta_l=-0.08))

    def evaluate_set(cands):
        names = {x.name for x in cands}
        d_l = -0.31 if "a" in names else (-0.30 if "b" in names else 0.0)
        if {"a", "b"} <= names:
            d_l -= 0.005
        if "c" in names:
            d_l -= 0.08
        return 0.0, d_l

    picks = greedy_pick([a, b, c], evaluate_set, 3, 0.01)
    assert [p[0].name for p in picks] == ["a", "c"]
    assert [round(p[1], 3) for p in picks] == [-0.31, -0.39]


def test_common_mode_budget_is_the_stray_capacitance_at_the_limit(tmp_path):
    """The switch node's harmonics V_n drive I_n = 2πf·C·V_n through the stray capacitance; half of it reads on
    each LISN. The budget is the C that just meets the limit: with c_stray = budget the margin is 0 dB, twice
    the budget gives −6.02 dB, and the budget follows from the harmonic formula by hand."""
    from groundtruth.emi.conducted import limit_line
    r = run(tmp_path, hot_loop_board(20.0), "cm")[0]
    cm = r.cm
    assert cm and cm["budget_pf"] > 0 and 150e3 <= cm["f"] <= 30e6
    f0, v, d, tr = SPEC.f_sw, SPEC.v_in, SPEC.v_out / SPEC.v_in, SPEC.rise_time
    n = np.arange(1, int(30e6 // f0) + 1)
    f = n * f0
    keep = f >= 150e3
    v_n = 2 * v * d * np.abs(np.sinc(n * d)) * np.abs(np.sinc(n * f0 * tr))
    z = np.abs(LISNS["cispr32"].impedance(f)[0])
    reading = 20 * np.log10(2 * np.pi * f * 1e-12 * v_n / 2 * z / math.sqrt(2) / 1e-6)
    margin = limit_line(f, "cispr32", "B") - reading
    assert cm["budget_pf"] == pytest.approx(10 ** (np.nanmin(margin[keep]) / 20), rel=1e-6)
    for factor, expect in ((1.0, 0.0), (2.0, -20 * math.log10(2))):
        spec = ConverterSpec(**{**SPEC.__dict__, "c_stray": cm["budget_pf"] * factor * 1e-12})
        r2 = run(tmp_path, hot_loop_board(20.0), f"cm{factor:g}", spec=spec)[0]
        assert r2.cm["worst"]["margin_db"] == pytest.approx(expect, abs=0.01)


def test_ringing_with_c_oss_peaks_at_the_resonance_by_its_q(tmp_path):
    spec = ConverterSpec(**{**SPEC.__dict__, "c_oss": 1e-9, "r_damp": 0.05})
    r = run(tmp_path, hot_loop_board(20.0), "ring", spec=spec)[0]
    ring = r.ringing
    assert ring["f"] == pytest.approx(1 / (2 * math.pi * math.sqrt(r.loop_nh * 1e-9 * 1e-9)), rel=1e-6)
    assert ring["q"] == pytest.approx(math.sqrt(r.loop_nh * 1e-9 / 1e-9) / ring["r_ohm"], rel=1e-6)
    assert float(ringing_response(ring["f"], ring["f"], ring["q"])) == pytest.approx(ring["q"], rel=1e-9)
    assert ring["overshoot_v"] > 0


@pytest.mark.parametrize("snubber", [None, (2e-9, 1.0)])
def test_the_hot_loop_current_carries_the_node_charge_current_on_top_of_the_pulse(snubber):
    """The switching cell as a linear circuit, integrated in time: a stiff input V, the loop L + r, the high side's
    voltage falling and rising in tr (the node driven by V·s(t)), the load pulse I·s(t) out of the node, C_oss (and
    an RC snubber) at the node. Its steady-state loop current per harmonic is exactly
    pulse · |H| · |1 + Y_node·V/I|: at the ringing the node's charge current is several times the pulse."""
    from scipy.integrate import solve_ivp
    from groundtruth.analysis.power import edge_charge_factor, snubbed_response
    from groundtruth.emi.spectrum import trapezoid_harmonics
    f0, tr, duty, L, r, C, V, I = 5e6, 2e-9, 0.4, 2e-9, 0.15, 600e-12, 48.0, 4.0
    T = 1 / f0

    def s(t):
        t = t % T
        return min(t / tr, 1.0) if t < duty * T else max(1.0 - (t - duty * T) / tr, 0.0)

    def rhs(t, y):
        i, vn, vs = y
        i_s = 0.0 if snubber is None else (vn - vs) / snubber[1]
        return [(V * s(t) - r * i - vn) / L, (i - I * s(t) - i_s) / C,
                0.0 if snubber is None else i_s / snubber[0]]

    periods = 6
    sol = solve_ivp(rhs, (0, periods * T), [0.0, 0.0, 0.0], method="RK45", max_step=tr / 10, rtol=1e-9, atol=1e-12,
                    dense_output=True)
    N = 8192
    i = sol.sol((periods - 1) * T + np.arange(N) * T / N)[0]
    measured = np.abs(np.fft.rfft(i))[1:80] * 2 / N
    f, pulse = trapezoid_harmonics(f0, tr, I, duty, f_max=79 * f0)
    if snubber is None:
        f_ring = 1 / (2 * math.pi * math.sqrt(L * C))
        h = ringing_response(f, f_ring, math.sqrt(L / C) / r)
    else:
        h = snubbed_response(f, L, r, C, *snubber)
    model = pulse * h * edge_charge_factor(f, I, V, C, snubber)
    n = np.rint(f / f0).astype(int) - 1
    big = model > 1e-2 * model.max()
    assert measured[n[big]] == pytest.approx(model[big], rel=2e-3)
    hi = f > 100e6                                                        # around the ringing (145 MHz)
    k = int(np.argmax(np.where(hi, model, 0)))
    assert edge_charge_factor(f[k], I, V, C, snubber) > 3             # the pulse alone is far too low there


def test_detects_the_discrete_buck(tmp_path):
    board, sc, _ = make(tmp_path, discrete_buck_board(), "buck", nets=())
    found = detect_converters(board, sc.config)
    assert len(found) == 1
    s = found[0].spec
    assert (s.topology, s.rail, s.ground, s.switch_node, s.high_side, s.low_side) == \
        ("buck", "VIN", "GND", "SW", ["Q1"], ["Q2"])
    assert s.input_connector == ["J1.1", "J1.2"]
    assert s.source == "auto" and not s.reviewed
    assert "SW" not in {d.spec.name for d in detect_nets(board, sc.config, 0, skip={"SW"})}


def test_a_surge_stopper_fet_does_not_hide_the_input_connector(tmp_path):
    """refboard: the surge stopper's FET Q601 sits between J101's V_BATT and +VDC, and with its default
    'open' model no input connector was found. A gate-driven FET whose drain and source are on two supply nets
    (no ground, no inductor: not a switching FET) is a series pass switch: detection crosses it closed, names it,
    and suggests a short model for it, which is set only where the model is still 'open'."""
    from groundtruth.analysis.detect import pass_switches
    from groundtruth.analysis.power import input_path
    from groundtruth.components.models import ComponentModel
    board, sc, _ = make(tmp_path, discrete_buck_board(surge_fet=True), "surge", nets=())
    assert pass_switches(board, sc.config) == {"Q9": ("VBATT", "VIN")}        # not Q1 / Q2: they touch L1's SW
    d, = detect_converters(board, sc.config)
    assert d.spec.input_connector == ["J1.1", "J1.2"] and set(d.models) == {"Q9"}
    assert d.models["Q9"].kind == "short" and any("series FET Q9" in r for r in d.reasons)
    assert input_path(board, sc.config, "VIN", "VBATT", {"GND"}) is None      # still cut off with Q9 open
    sc.add_detected_converters([d.spec], d.models)
    assert sc.config.model_for("Q9").kind == "short"
    assert input_path(board, sc.config, "VIN", "VBATT", {"GND"}) == ["VIN", "VBATT"]   # the LISN path closes
    sc.set_component("Q9", ComponentModel("resistor", {"R": 0.0065}))     # the user's R_DS(on) is kept
    assert sc.add_suggested_models(d.models) == [] and sc.config.model_for("Q9").kind == "resistor"
    plain, psc, _ = make(tmp_path, discrete_buck_board(), "plain", nets=())
    assert detect_converters(plain, psc.config)[0].models == {}


def test_detects_an_integrated_converter(tmp_path):
    board, sc, _ = make(tmp_path, hot_loop_board(20.0, sw_pin=True), "int", nets=())
    found = detect_converters(board, sc.config)
    assert len(found) == 1
    s = found[0].spec
    assert (s.switch, s.rail, s.ground, s.switch_node, s.v_in) == (["U1"], "VIN", "GND", None, None)


def test_a_stages_power_path_is_never_suggested_as_a_signal(tmp_path):
    """Net-(U302-Vs+) on refboard: the copper between the inductor and the current-sense shunts, with a
    sense amplifier's input pin on it, was auto-detected as a signal net. Every net of a stage's power flow (input
    path, rail, switch node, the inductor's output side, the output path through the shunt) is the stage's, with
    its role; detection skips them and 'Add selected net' refuses them with that role."""
    from groundtruth.analysis.detect import power_nets, suggest_net
    g = discrete_buck_board(out_connector=True, shunt=True)
    g.ic("U9", (30.0, 18.0), "VOUT", pintype="input", pinfunction="Vs+")          # the sense amplifier's Vs+ pin
    g.items = [i.replace('(net "VOUT")', '(net "Net-(U9-Vs+)")') for i in g.items]  # no supply-like name left
    board, sc, _ = make(tmp_path, g, "buck", nets=())
    sc.add_detected_converters([d.spec for d in detect_converters(board, sc.config)])
    spec = sc.config.converters[0]
    roles = power_nets(board, sc.config, [spec])
    assert "inductor L1's output side" in roles["Net-(U9-Vs+)"] and "output path" in roles["VOUT_S"]
    assert "switch node" in roles["SW"] and "rail" in roles["VIN"] and spec.name in roles["SW"]
    assert not set(roles) & {d.spec.name for d in detect_nets(board, sc.config, 0)}
    found, why = suggest_net(board, sc.config, "Net-(U9-Vs+)")
    assert found is None and "inductor L1's output side" in why and "not a signal" in why


def test_an_integrated_stage_takes_its_sw_pin_as_the_switch_node(tmp_path):
    """The LT3970 (U503) on refboard has a V_OUT pin on the inductor's far side (+5V): the switch node is
    the net of its SW pin, not whichever of its inductor nets sorts first."""
    from groundtruth.analysis.power import switch_inductor
    g = hot_loop_board(20.0, sw_pin=True, out_pin=True)
    g.items = [i.replace('(net "VOUT")', '(net "+5V")') for i in g.items]          # sorts before "SW"
    board, sc, _ = make(tmp_path, g, "int", nets=())
    spec = ConverterSpec("int", "VIN", "GND", switch=["U1"])
    sw, inductor = switch_inductor(board, sc.config, spec)
    assert sw == "SW" and inductor.reference == "L1"


def test_boost_conducted_emission_comes_from_the_inductor_ripple(tmp_path):
    """A boost draws its inductor current from the input: the triangular ripple ΔI = V_in·D/(L·f_sw),
    D = 1 − V_in/V_out, divides between the input capacitor and the LISNs (the divider below C1's self-resonance),
    and its fundamental c_1 = ΔI·sin(πD)/(π²·D·(1−D)) sets the reading at f_sw."""
    from boardgen import boost_board
    board, sc, _ = make(tmp_path, boost_board(), "boost", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    assert spec.topology == "boost" and spec.input_connector == ["J1.1", "J1.2"]
    spec.f_sw, spec.v_in, spec.v_out, spec.i_out = 300e3, 5.0, 12.0, 0.5
    r, _, sc2 = run(tmp_path, boost_board(), "boost2", spec=spec)
    d = 1 - 5.0 / 12.0
    L = sc2.config.model_for("L1").get("L")
    assert r.ripple["delta_i_pp"] == pytest.approx(5.0 * d / (L * 300e3))
    tf = {t["f"]: t["ratio"] for t in r.input_transfer}
    f150 = min(tf, key=lambda x: abs(math.log(x / 150e3)))
    assert tf[f150] == pytest.approx(_analytic_transfer(sc2, f150), rel=0.05)
    fs = np.array(sorted(tf))
    h300 = float(np.exp(np.interp(np.log(300e3), np.log(fs), np.log([tf[f] for f in fs]))))
    c1 = r.ripple["delta_i_pp"] * math.sin(math.pi * d) / (math.pi ** 2 * d * (1 - d))
    z = abs(LISNS["cispr32"].impedance(300e3)[0])
    row = next(x for x in r.conducted if abs(x["f"] - 300e3) < 1.0)
    assert row["v_dbuv"] == pytest.approx(20 * math.log10(c1 * h300 * z / math.sqrt(2) / 1e-6), abs=0.3)
    assert any(p["name"] == "input J1.1 → L1" for p in r.dc)


def test_copies_of_a_stage_get_distinct_names():
    """Two copies of a stage on different sheets share the switch-node leaf name: they are named by more of the
    net path, and by their switch when even that is the same."""
    from groundtruth.analysis.detect import DetectedConverter, _unique_names
    mk = lambda sw, hs: DetectedConverter(ConverterSpec("buck SW1", "VIN", "GND", sw, [hs], ["Q9"]), "high")  # noqa: E731
    found = [mk("/RAIL A/SW1", "Q1"), mk("/RAIL A1/SW1", "Q2"), mk("/X/SW1", "Q3"), mk("/X/SW1", "Q4")]
    _unique_names(found)
    names = [d.spec.name for d in found]
    assert names[:2] == ["buck RAIL A/SW1", "buck RAIL A1/SW1"]
    assert names[2:] == ["buck X/SW1 (Q3)", "buck X/SW1 (Q4)"]


def test_discrete_buck_loop_runs_through_the_switch_node_pour(tmp_path):
    board, sc, _ = make(tmp_path, discrete_buck_board(), "buck", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.3
    r, res, _ = run(tmp_path, discrete_buck_board(), "buck2", spec=spec)
    # the whole loop current crosses the SW pour between Q1's source and Q2's drain
    g, K = r.layer_grids["F.Cu"], r.sheet_current["F.Cu"]
    j = int(round((15.0 - g.y0) / g.d))
    cut = [K[i, j] for i in range(g.nx) if 17.0 <= g.x0 + i * g.d <= 22.0 and g.node[i, j]
           and g.nets[g.label[i, j]] == "SW"]
    assert sum(cut) * g.d * 1e-3 == pytest.approx(1.0, rel=0.1)
    assert 2.0 < r.loop_nh < 30.0
    assert r.conducted_worst is not None
    assert any(i.net == spec.name for i in res.issues) or r.conducted_worst["margin_db"] >= 6.0


def test_converter_settings_round_trip(tmp_path):
    board, sc, _ = make(tmp_path, discrete_buck_board(), "rt", nets=())
    spec = ConverterSpec("buck 5V", "VIN", "GND", "SW", ["Q1"], ["Q2"], f_sw=400e3, i_out=3.0, v_in=24.0,
                         v_out=5.0, rise_time=4e-9, c_oss=1.2e-9, input_connector=["J1.1", "J1.2"],
                         output_connector=["C2.1", "C2.2"], conducted="cispr25")
    sc.set_converters([spec])
    sc.save()
    back = SettingsFile.load(sc.path).config.converters[0]
    for k in ("name", "rail", "ground", "switch_node", "high_side", "low_side", "switch", "topology",
              "input_connector", "output_connector", "conducted"):
        assert getattr(back, k) == getattr(spec, k), k
    for k in ("f_sw", "i_out", "v_in", "v_out", "rise_time", "c_oss"):
        assert getattr(back, k) == pytest.approx(getattr(spec, k)), k
    assert back.pulse() == pytest.approx((3.0, 5.0 / 24.0))


def test_converter_entries_are_validated():
    with pytest.raises(ConfigError, match="high_side and low_side"):
        config_from_data({"converters": [{"name": "x", "rail": "VIN", "ground": "GND", "high_side": "Q1"}]})
    with pytest.raises(ConfigError, match="switch_node"):
        config_from_data({"converters": [{"name": "x", "rail": "VIN", "ground": "GND", "high_side": "Q1",
                                          "low_side": "Q2"}]})
    with pytest.raises(ConfigError, match=r"output_connector needs two pins"):
        config_from_data({"converters": [{"name": "x", "rail": "VIN", "ground": "GND", "switch": "U1",
                                          "output_connector": "J2.1"}]})
    cfg = config_from_data({"converters": [{"name": "x", "rail": "VIN", "ground": "GND", "switch": "U1",
                                            "f_sw": "400kHz", "rise_time": "5ns", "c_oss": "800pF"}]})
    c = cfg.converters[0]
    assert (c.f_sw, c.rise_time, c.c_oss) == (pytest.approx(400e3), pytest.approx(5e-9), pytest.approx(800e-12))


def test_boost_pulse_is_the_input_current():
    s = ConverterSpec("b", "VOUT", "GND", "SW", ["D1"], ["Q1"], topology="boost", i_out=1.0, v_in=5.0, v_out=20.0)
    assert s.pulse() == pytest.approx((4.0, 0.25))
    assert np.isclose(ConverterSpec("c", "V", "G", switch=["U"], topology="custom", duty=0.3).pulse()[1], 0.3)


def test_input_current_is_output_power_plus_losses_over_the_input_voltage(tmp_path):
    """Energy balance, V_in·I_in = V_out·I_out + P_loss, term by term against the hand sums: a synchronous buck,
    12 V → 3 V at 3 A (D = 0.25), 300 kHz, 4 ns edges: conduction D·I²·R_on (high side) + (1-D-2·t_d·f)·I²·R_on,low,
    the low side's reverse conduction in two 25 ns dead times, (2·I·t_d − 2·Q_oss)·f·V_SD (the soft edge's node
    swing taken off) [A48], one hard edge ½·V·I·t_r·f, output
    charge Q_oss·V·f, the inductor's I²·DCR; then a DC path's copper I²·R."""
    from types import SimpleNamespace
    from groundtruth.analysis.power import input_estimate
    board, sc, _ = make(tmp_path, discrete_buck_board(), "inbal", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out, spec.rise_time = 300e3, 3.0, 12.0, 3.0, 4e-9
    spec.rds_on, spec.rds_on_low, spec.q_oss, spec.dcr = 10e-3, 5e-3, 20e-9, 8e-3
    spec.dead_rise = spec.dead_fall = 25e-9
    spec.v_sd = 0.8
    res = SimpleNamespace(net=spec.name, duty=0.25, v_in=12.0)
    est = input_estimate(board, sc.config, spec, res)
    dead = 2 * 25e-9 * 300e3                                                # 1.5 % of the period
    p_loss = (0.25 * 9 * 10e-3 + (0.75 - dead) * 9 * 5e-3 + (2 * 3 * 25e-9 - 2 * 20e-9) * 300e3 * 0.8
              + 0.5 * 12 * 3 * 4e-9 * 300e3
              + 20e-9 * 12 * 300e3 + 9 * 8e-3)
    assert est["p_out_w"] == pytest.approx(9.0) and est["p_loss_w"] == pytest.approx(p_loss)       # 0.248 W
    assert est["i_in_a"] == pytest.approx((9.0 + p_loss) / 12.0) and est["i_in_ideal_a"] == pytest.approx(0.75)
    assert est["efficiency"] == pytest.approx(9.0 / (9.0 + p_loss)) and not est["missing"]
    copper = [{"name": "output L1 → J2.1", "current_a": 3.0, "r_mohm": 10.0, "r_copper_mohm": 10.0, "parts": []}]
    with_cu = input_estimate(board, sc.config, spec, res, copper)
    assert with_cu["copper_w"] == pytest.approx(0.09) and with_cu["p_loss_w"] == pytest.approx(p_loss + 0.09)
    spec.rds_on = spec.rds_on_low = spec.dcr = None                         # unknown values: said, not guessed
    assert len(input_estimate(board, sc.config, spec, res)["missing"]) == 2
    assert input_estimate(board, sc.config, ConverterSpec(**{**spec.__dict__, "v_in": None}),
                          SimpleNamespace(net=spec.name, duty=0.25, v_in=None)) is None


def test_a_boosts_ground_return_splits_by_kirchhoff(tmp_path):
    """A boost 12 V → 36 V at 1 A (D = 2/3) draws I_in ≈ 3 A from J1: the load returns its 1 A through J2's GND
    via, the low side Q1 (conducting the on-time) the other ≈ 2 A, and all of I_in leaves through J1's GND via.
    (Before, the load's and the low side's shares were swapped: D·I_in = 2 A from the load.) The input path and
    the ground run carry input_estimate's current, which includes the boost diode's drop."""
    from boardgen import boost_board
    board, sc, _ = make(tmp_path, boost_board(out_connector=True), "bst", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 1.0, 12.0, 36.0
    spec.input_connector, spec.output_connector = ["J1.1", "J1.2"], ["J2.1", "J2.2"]
    r, _, _ = run(tmp_path, boost_board(out_connector=True), "bst2", spec=spec)
    i_in = r.input["i_in_a"]
    assert 3.0 < i_in < 3.3                                       # + the diode's 0.5 V for 1 A, the edges
    gnd = next(p for p in r.dc if p["name"].startswith("ground"))

    def via_current(x, y):
        return sum(v["current_a"] for v in gnd["vias"] if math.dist((v["x"], v["y"]), (x, y)) < 0.5)
    assert gnd["current_a"] == pytest.approx(i_in, rel=0.01)      # solved before the copper's losses were known
    assert via_current(34.5, 9.5) == pytest.approx(1.0, rel=0.01)                 # the load's return
    assert via_current(19.5, 17.5) == pytest.approx(i_in - 1.0, rel=0.01)         # the low side
    assert via_current(3.0, 9.5) == pytest.approx(i_in, rel=0.01)                 # out at the input connector


def _split_strip_r(tmp_path, name, joined, d=0.1, bond=None):
    """DC resistance (mΩ) of split_strip_board (R1 = 1 mΩ across the gap, or the joined reference strip) fed over
    its end pads, on a grid of d mm; also the path's parts and the model's part and bond branches. `bond`: R1's
    solder thickness, µm (its pin value; None: built in)."""
    from boardgen import split_strip_board
    from groundtruth.analysis import simulate
    from groundtruth.analysis.power import _area_shares, dc_path
    from groundtruth.components.models import ComponentModel
    board, sc, _ = make(tmp_path, split_strip_board(joined=joined), name, nets=())
    if not joined:
        sc.set_component("R1", ComponentModel("resistor", {"R": 1e-3}))
        if bond is not None:
            sc.set_component_pins("R1", {"bond": bond})
    st = simulate.BoardModel(board, sc.config).build((2.0, 6.0, 30.0, 14.0), ["F.Cu"], {"PWR", "OUT"}, d, False)
    inj = _area_shares(st, [board.find_pin("U1.1")], "F.Cu", 1.0) + \
        _area_shares(st, [board.find_pin("U2.1")], "F.Cu", -1.0)
    path, _ = dc_path(st, inj, 1.0, "strip")
    return (path["r_mohm"], path["parts"], [b for b in st.plane.branches if b.kind == "part"],
            [b for b in st.plane.branches if b.kind == "bond"])


def test_a_part_is_joined_to_the_copper_over_its_whole_pads(tmp_path):
    """A 1 mΩ part whose pads (0.8 mm deep, 3.8 mm wide) span two 4 mm × 35 µm strips at their 0.6 mm gap (a shunt, a
    FET's drain and source rows). Each side's metal is soldered to every node under its pads (simulate.part_lid,
    [A65]), so the current hands over at the pad's outer edge, within the transfer length λ = √(r_c / R_sheet)
    ≈ 0.14 mm, as a transmission-line contact [Berger 1972]: the part adds its 1 mΩ and two contacts of
    (R_sheet·λ/w)·coth(depth/λ) ≈ 0.018 mΩ and bypasses the gap and both pads' copper, 2.2 mm at 0.123 mΩ/mm: ≈ +0.766
    mΩ over the joined strip; on two grids. Before 2026-10-13 one node per side crowded the current into a point
    (+1.45 mΩ); until 2026-10-24 up to 8 sampled nodes per side (+0.84: about half of each pad's copper used)."""
    from groundtruth.analysis.simulate import BOND_RC
    rs = 1 / (5.8e7 * 35e-6)                                                    # Ω per square
    lam = math.sqrt(BOND_RC / rs)
    contact = rs * lam / 3.8e-3 / math.tanh(0.8e-3 / lam)
    expect = (1e-3 + 2 * contact - rs * 2.2e-3 / 4e-3) * 1e3                   # mΩ
    ref, _, _, _ = _split_strip_r(tmp_path, "joined", True)
    r, parts, branches, bonds = _split_strip_r(tmp_path, "split", False)
    assert len(branches) == 1 and branches[0].name == "R1"
    assert len(bonds) > 100 and {b.name for b in bonds} == {"R1"}           # every node under both pads
    assert r - ref == pytest.approx(expect, abs=0.025)                         # 0.1 mm cells: λ is 1.4 of them
    assert parts[0]["name"] == "R1" and parts[0]["current_a"] == pytest.approx(1.0, rel=1e-6)
    assert parts[0]["r_mohm"] == pytest.approx(1.0, rel=0.01)                  # the part's own share of R
    ref_f, _, _, _ = _split_strip_r(tmp_path, "joined_f", True, d=0.05)
    r_f, _, _, _ = _split_strip_r(tmp_path, "split_f", False, d=0.05)
    assert (r_f - ref_f) == pytest.approx(expect, abs=0.015)                   # closer on the finer grid


def test_a_parts_solder_thickness_sets_its_contact(tmp_path):
    """R1's pin value `bond` (µm of SAC305, analysis/pins.py): 0 is a perfect bond (no contact: +0.729 mΩ, the part
    less the bypassed copper), 300 µm a joint four times the built-in one (λ = 0.28 mm: two contacts of
    (R_sheet·λ/w)·coth(depth/λ) ≈ 0.037 mΩ each, +0.802) [Berger 1972]."""
    from groundtruth.analysis.pins import SOLDER_RHO
    rs = 1 / (5.8e7 * 35e-6)
    ref, _, _, _ = _split_strip_r(tmp_path, "joined", True)
    for um, name in ((0.0, "perfect"), (300.0, "thick")):
        lam = math.sqrt(SOLDER_RHO * max(um, 0.1) * 1e-6 / rs)
        contact = rs * lam / 3.8e-3 / math.tanh(0.8e-3 / lam)
        expect = (1e-3 + 2 * contact - rs * 2.2e-3 / 4e-3) * 1e3
        r, _, _, _ = _split_strip_r(tmp_path, name, False, bond=um)
        assert r - ref == pytest.approx(expect, abs=0.025), um


def test_the_stages_dc_currents_add_up_on_one_board_model(tmp_path):
    """Every stage's DC paths are solved on one whole-board model (power.dc_board_build), so the combined view
    (power.combine_dc) is their superposition. Two identical stages on the same copper: the combined current density
    is exactly twice one stage's total (all its paths' vectors added), every via carries twice its current, the
    input current is the sum; the current-density and via checks run on the total (net "all power stages") instead of
    each stage's own paths. The user: "the currents are not added together. So one plot shows 5.15 A the other says
    1 A" (refboard's two bucks share J101, Q601, the +VDC pours and the ground)."""
    from dataclasses import replace
    from groundtruth.analysis.power import COMBINED_NET, COMBINED_VIEW, dc_view
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "twins", nets=())
    a = detect_converters(board, sc.config)[0].spec
    a.f_sw, a.i_out, a.v_in, a.v_out = 300e3, 3.0, 12.0, 3.0
    b = replace(a, name="twin")
    board, sc, _ = make_conv(tmp_path, discrete_buck_board(out_connector=True), "twins2", [a, b])
    res = simulate(board, sc.config)
    one, two = res.converters
    combo = res.dc_combined
    assert combo is not None and combo.net == COMBINED_NET and combo.dc[0]["name"] == COMBINED_VIEW
    # one shared model: every path's grids are the same objects (dc_branch, which held the model, is freed after
    # Simulate: it kept the whole-board model alive and blocked pickling for the compute server)
    grids = {tuple(id(g) for g in r.dc_grids[name].values()) for r in (one, two) for name in r.dc_grids}
    assert len(grids) == 1 and any("whole board" in n for n in one.notes)
    assert all(not r.dc_branch for r in (one, two))
    path = combo.dc[0]
    i_in = sum(p["current_a"] for r in (one, two) for p in r.dc if p["name"].startswith("input "))
    assert path["current_a"] == pytest.approx(i_in)
    for layer, m in combo.dc_maps[COMBINED_VIEW].items():
        jx = sum(one.dc_vectors[n][layer][0] for n in one.dc_vectors)
        jy = sum(one.dc_vectors[n][layer][1] for n in one.dc_vectors)
        # the mean currents superpose (twice one stage's); each stage's switch-node variance adds in quadrature
        # (two stages switching independently: twice one stage's variance; VALIDATION_AUDIT.md ISSUE-7)
        var = sum(one.dc_ripple_sq[n][layer] for n in one.dc_ripple_sq)
        assert np.allclose(m, np.sqrt((2 * np.hypot(jx, jy)) ** 2 + 2 * var), rtol=1e-4, atol=1e-6)
    # the combined copper loss and the heat Thermal gets from the two stages (each from its own code: the summed maps
    # over each node's copper area with the vias' R·I², and the stages' vectors, variances and via segments) are the
    # same total (round 5: the combined loss had taken whole cells and no barrels)
    from groundtruth.analysis.thermal import copper_heat
    heat = sum(float(p.sum()) for *_, p in copper_heat(board, [one, two]).values())
    assert path["loss_w"] == pytest.approx(heat, rel=0.005)
    single = {v["name"]: v["current_a"] for p in one.dc for v in p["vias"]}
    busiest = path["vias"][0]
    assert busiest["current_a"] <= 2 * sum(single.values())                 # bounded by the paths' sum
    dc_issues = [i for i in res.issues if i.check in ("current_density", "via_overload")]
    assert dc_issues and all(i.net == COMBINED_NET for i in dc_issues)        # the total is what is checked
    v = dc_view(combo, COMBINED_VIEW)
    assert v.view_of == COMBINED_VIEW and v.heat_title.startswith("All power stages: DC currents together")
    # the HTML report shows it first under Power stages: its figures, the vias on the total, the map; from the
    # result and from the plugin's view alike
    from groundtruth.report.html import render_html
    for given in (combo, v):
        page = render_html(board, res.issues, res.routes, "simulation", converters=res.converters,
                           config=sc.config, dc_combined=given)
        art = page.split("All power stages: DC currents together</h3>", 1)
        assert len(art) == 2 and art[1].index("<img") < art[1].index("</article>")
        assert "Vias on the total current" in art[1] and f"{path['current_a']:.3g} A" in art[1]
        assert page.index("All stages: DC together") < page.index(f">{one.net}</a>")      # first in the contents
