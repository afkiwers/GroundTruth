"""A ceramic capacitor at its own DC voltage (components/bias.py: the curve; analysis/bias.py: the voltage across
each capacitor and the run's copy of the settings) against closed forms. The user: "cbias is saved as a single
value, this is probably not ideal as it will be used with different DC voltages", "rather than having a real value
we could use an array like this (90nF@10V 75nF@50V, 20nF@70V)", and of C304 at an ADC input: "Worst case the opamp
is a rail-to-rail IC so in this case it would be 3.3 V"."""
import math

import numpy as np
import pytest

from boardgen import opamp_circuit_board, rc_lowpass_board
from conftest import make
from groundtruth.components.bias import (BIAS_POINTS_MAX, BiasPoint, bias_text, c_at, check_points, one_point,
                                         parse_bias, resolve, v1_through)
from groundtruth.components.models import ComponentModel

C0 = 100e-9


def test_the_one_point_curve_is_exact_at_zero_and_at_its_point():
    """C0 / (1 + (V/V1)^2) [A56] with V1 = Vb / sqrt(C0/Cb − 1): C0 at 0 V, Cb at Vb (75 nF at 50 V: V1 = 86.6 V),
    falling monotonically; a point at or above C0 derates nothing."""
    v1 = v1_through(C0, 75e-9, 50.0)
    assert v1 == pytest.approx(50 * math.sqrt(3), rel=1e-12)
    assert c_at([(50.0, 75e-9)], C0, 0.0) == pytest.approx(C0, rel=1e-12)
    assert c_at([(50.0, 75e-9)], C0, 50.0) == pytest.approx(75e-9, rel=1e-12)
    assert c_at([(50.0, 75e-9)], C0, -50.0) == pytest.approx(75e-9, rel=1e-12)       # the sign does not matter
    vs = np.linspace(0, 200, 41)
    cs = [c_at([(50.0, 75e-9)], C0, v) for v in vs]
    assert all(a > b for a, b in zip(cs, cs[1:]))
    assert c_at([(50.0, 120e-9)], C0, 30.0) == C0 and v1_through(C0, C0, 10.0) is None


def test_the_users_datasheet_curve_fits_the_one_point_form():
    """The user's DC-bias chart (a 100 nF 100 V X7R): ΔC −20 / −50 / −67 / −78 / −85 % at 20 / 40 / 60 / 80 / 100 V.
    C0 / (1 + (V/40 V)^2) is within 3 points of every one [A56]; Johnson's (1 + (V/V1)^2)^(−1/3) through 40 V
    misses by more than 10 [Johnson 1962]."""
    read = {20: -20, 40: -50, 60: -67, 80: -78, 100: -85}
    for v, pct in read.items():
        assert 100 * (one_point(1.0, 40.0, v) - 1) == pytest.approx(pct, abs=3.0)
    v1_j = 40.0 / math.sqrt(2 ** 3 - 1)                     # Johnson's form through −50 % at 40 V
    assert max(abs(100 * ((1 + (v / v1_j) ** 2) ** (-1 / 3) - 1) - pct) for v, pct in read.items()) > 10


def test_a_list_passes_through_every_point_and_continues_into_the_tail():
    """Several points: a monotone cubic through (0, C0) and each point, no overshoot between them, continuous into
    the one-point curve through the last point above it."""
    pts = [(10.0, 95e-9), (30.0, 66e-9), (50.0, 39e-9), (80.0, 20e-9)]
    for v, c in pts:
        assert c_at(pts, C0, v) == pytest.approx(c, rel=1e-9)
    vs = np.linspace(0, 80, 161)
    cs = np.array([c_at(pts, C0, v) for v in vs])
    assert np.all(np.diff(cs) <= 1e-21) and cs.max() <= C0 * (1 + 1e-12)
    assert c_at(pts, C0, 80.0 + 1e-6) == pytest.approx(20e-9, rel=1e-6)          # continuous at the last point
    v1 = v1_through(C0, 20e-9, 80.0)
    assert c_at(pts, C0, 120.0) == pytest.approx(20e-9 * one_point(C0, v1, 120.0) / one_point(C0, v1, 80.0))


def test_every_format_parses_and_the_limits_hold():
    """'75nF', '75nF@50V', a list with commas, semicolons or spaces, '75 nF @ 50 V', percent points, a bare number
    in µF; refused: more than BIAS_POINTS_MAX points, a list without voltages, two points at one voltage, 0 V, a
    change outside −99 … +20 %, a point far above the nominal."""
    assert parse_bias("75nF")[0] == [BiasPoint(c=pytest.approx(75e-9))]
    for text in ("90nF@10V 75nF@50V, 20nF@70V", "90nF@10V; 75nF@50V; 20nF@70V", "20nF@70V, 90n@10, 75 nF @ 50 V"):
        pts, problems = parse_bias(text)
        assert not problems and [p.v for p in pts] == [10.0, 50.0, 70.0], text
        assert [p.c for p in pts] == pytest.approx([90e-9, 75e-9, 20e-9])
    pts, _ = parse_bias("-5%@10V, -50%@40V")
    assert [p.capacitance(C0) for p in pts] == pytest.approx([95e-9, 50e-9])
    assert parse_bias("0.075")[0][0].c == pytest.approx(75e-9)                   # a bare number: µF
    assert parse_bias(7.5e-8)[0][0].c == pytest.approx(75e-9)                    # an old settings file's float
    assert bias_text(parse_bias("20nF@70V, 90nF@10V")[0]) == "90nF@10V, 20nF@70V"
    many = ", ".join(f"{90 - k}nF@{k + 1}V" for k in range(BIAS_POINTS_MAX + 1))
    for bad in (many, "75nF, 50nF", "75nF@10V, 60nF@10V", "75nF@0V", "-100%@10V", "x@10V", "75nF@abc"):
        assert parse_bias(bad)[1] and not parse_bias(bad)[0], bad
    assert check_points(parse_bias("750nF@10V")[0], C0) and not check_points(parse_bias("75nF@10V")[0], C0)


def test_a_single_value_without_a_voltage_is_taken_at_half_the_rating_or_flat():
    """[A57]: '75nF' with a rated voltage of 100 V is the point 75 nF @ 50 V; without a rating it is used as it is."""
    pts, _ = parse_bias("75nF")
    assert resolve(pts, C0, 50.0) == ([(50.0, pytest.approx(75e-9))], "default voltage")
    assert resolve(pts, C0, None) == ([], "flat")


def test_the_settings_keep_old_numbers_and_write_curves_as_text(tmp_path):
    """C_bias: an old file's number loads (and is written back as that number); a curve is kept as its text."""
    m = ComponentModel.from_dict({"model": "cap", "C": 1e-7, "C_bias": 7.5e-8})
    assert m.capacitance() == pytest.approx(75e-9) and m.to_dict()["C_bias"] == pytest.approx(7.5e-8)
    m = ComponentModel.from_dict({"model": "cap", "C": 1e-7, "C_bias": "95nF@10V, 39nF@50V"})
    assert m.bias == "95nF@10V, 39nF@50V" and m.to_dict()["C_bias"] == "95nF@10V, 39nF@50V"
    assert m.capacitance() == pytest.approx(1e-7)              # outside a run: the nominal (the curve needs a V)
    with pytest.raises(ValueError):
        ComponentModel.from_dict({"model": "cap", "C": 1e-7, "C_bias": "75nF@10V, 60nF@10V"})


def _voltage_board():
    """C1 on +12V; C2 at an op-amp's output (its V+ on +3V3); C3 at a 10k / 10k divider's midpoint from +12V; C4 on
    a net nothing else knows; C5 a C0G part on +12V; every one a 100 nF 100 V part with a DC-bias curve."""
    parts = [("C1", "100n", "C", "+12V", "GND"), ("C2", "100n", "C", "AMP_OUT", "GND"),
             ("R1", "10k", "R", "+12V", "MID"), ("R2", "10k", "R", "MID", "GND"), ("C3", "100n", "C", "MID", "GND"),
             ("C4", "100n", "C", "LONELY", "GND"), ("C5", "100n C0G", "C", "+12V", "GND"),
             ("C6", "100n", "C", "SS", "GND")]
    pins = {3: ("+IN", "SIG_IN"), 2: ("-IN", "AMP_OUT"), 1: ("OUT", "AMP_OUT"), 5: ("V+", "+3V3"), 4: ("V-", "GND")}
    # U4: a 48 V controller (its V+ on +48V) with a soft-start pin SS: not bounded by its input [A58]
    ctrl = {1: ("V+", "+48V"), 2: ("SS", "SS"), 3: ("V-", "GND")}
    g = opamp_circuit_board(parts, [("U3", pins), ("U4", ctrl)], out_net="AMP_OUT")
    for ref in ("C1", "C2", "C3", "C4", "C5", "C6"):
        g.fields(ref, **{"Cbias": "40nF@40V", "Rated Voltage": "100V", "Dielectric": "C0G" if ref == "C5" else "X7R"})
    return g


def test_each_capacitor_gets_the_worst_dc_voltage_across_it(tmp_path):
    """A rail's capacitor its rail (12 V), an op-amp output's its op-amp's supply (3.3 V, rail-to-rail worst case), a
    10k/10k divider's midpoint its DC operating point, 6 V (audit round 7, AA-1: the bound before gave it its top
    rail, 12 V), a net nothing knows none (a note; the nominal C), a 48 V controller's soft-start pin none either (not
    its input, [A58])."""
    from groundtruth.analysis.bias import cap_voltages
    board, sc, _ = make(tmp_path, _voltage_board(), "volts", nets=())
    v = cap_voltages(board, sc.config)
    assert v["C1"][0] == pytest.approx(12.0) and v["C2"][0] == pytest.approx(3.3)
    assert "U3" in v["C2"][1] and v["C3"][0] == pytest.approx(6.0) and "DC operating point" in v["C3"][1]
    assert v["C4"][0] is None and v["C6"][0] is None


def test_a_run_uses_each_capacitors_own_capacitance(tmp_path):
    """at_dc: the copy's C_bias is the curve at each part's voltage (40 nF @ 40 V: V1 = 32.66 V), the settings
    themselves unchanged, C0G nominal, the unknown one nominal and named in a note."""
    from groundtruth.analysis.bias import at_dc
    board, sc, _ = make(tmp_path, _voltage_board(), "run", nets=())
    cfg, rows, notes = at_dc(board, sc.config)
    by = {r.ref: r for r in rows}
    v1 = v1_through(C0, 40e-9, 40.0)
    for ref, volts in (("C1", 12.0), ("C2", 3.3), ("C3", 6.0)):
        want = C0 / (1 + (volts / v1) ** 2)
        assert cfg.model_for(ref).capacitance() == pytest.approx(want, rel=1e-9) and by[ref].c == pytest.approx(want)
    assert cfg.model_for("C5").capacitance() == pytest.approx(C0) and by["C5"].how == "class I"
    assert cfg.model_for("C4").capacitance() == pytest.approx(C0)
    assert any("unknown voltage" in n and "C4" in n for n in notes)
    assert sc.config.model_for("C1").params.get("C_bias") is None and sc.config.model_for("C1").bias == "40nF@40V"


def test_an_rc_filters_corner_moves_with_its_capacitors_dc_voltage(tmp_path):
    """The RC low-pass (R1 1 kΩ after the 25 Ω source, C9, the receiver's 5 pF) with C9 = 10 nF and Cbias
    '5nF@10V', SIG_B held at 5 V (safety: voltages): C9 at 5 V = 10 nF / (1 + (5/10)^2) = 8 nF, so the Bode corner
    is 1 / (2π·1025 Ω·8.005 nF) (1 %), not the nominal's."""
    from groundtruth.analysis import simulate as sim
    from groundtruth.analysis.transfer import gain_figures
    from groundtruth.config.settings import NetSpec
    g = rc_lowpass_board()
    g.fields("C9", Cbias="5nF@10V")
    spec = NetSpec("SIG_A", "U1.1", ["U2.1"], 1e6, 10e-9, sweep_from=1.0, sweep_to=10e6, sweep_points=300)
    board, sc, _ = make(tmp_path, g, "rcbias", nets=(spec,))
    sc.config.safety.voltages["SIG_B"] = 5.0
    (r,) = sim.bode_paths(board, sc.config)
    assert not r.error, r.error
    c = 10e-9 / (1 + (5.0 / 10.0) ** 2)
    assert c == pytest.approx(8e-9)
    assert gain_figures(r.responses[0])["f_3db"] == pytest.approx(1 / (2 * math.pi * 1025.0 * (c + 5e-12)), rel=0.01)


def test_the_helps_chart_draws_the_plugins_curve_through_its_points():
    """The help's Cbias section (the user: "draw a chart with a fake curve and place 3-4 points ... create the string
    that would go into the HTTP lib's parameter and draw some line in between that show how the plugin would
    approximate it. Also mention what the min/max number of samples can be"): the browser view's SVG has the four
    points as dots on the solid curve (vertices of its polyline), the curve is components/bias.c_at's, the HTTP lib
    text under it parses back to those points, the limits are said; wx's simple view has the numbers as a table."""
    import re
    from groundtruth.ui import help as h
    text, pct, pts = h.cbias_example()
    assert len(pts) == 4 and parse_bias(text)[0] and not parse_bias(text)[1]
    assert [(p.v, p.c) for p in parse_bias(text)[0]] == [(v, pytest.approx(c)) for v, c in pts]
    assert [p.capacitance(h.CBIAS_C0) for p in parse_bias(pct)[0]] == pytest.approx([c for _, c in pts], rel=0.02)
    svg = h.cbias_svg()
    fit = re.search(r'<polyline id="cbias-fit" fill="none" points="([^"]+)"', svg).group(1)
    vertices = {tuple(round(float(x), 1) for x in p.split(",")) for p in fit.split()}
    dots = re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', svg)
    assert len(dots) == 4 and all((round(float(x), 1), round(float(y), 1)) in vertices for x, y in dots)
    assert text in svg and f"at most {BIAS_POINTS_MAX}" in svg and "var(--accent)" in svg
    sections = dict((a, b) for a, _, b in h.sections()[1])
    assert "<svg" in sections["cbias"] and "@CBIAS_CHART@" not in sections["cbias"]
    classic = h.page()
    assert "@CBIAS_CHART@" not in classic and "<svg" not in classic and text in classic and "(a point)" in classic


def _feedback_board():
    """An AC-coupled inverting stage on a single 5 V supply (VREF 2.5 V from a 10k/10k divider at +IN; C14 couples
    the input, R3 10k, R4 100k with C13 across it), and C7 a 16 V part between +12V and −12V: what audit round 7's
    B6 and B7 benchmarks check (tools/dev/analog_audit.py)."""
    parts = [("R5", "10k", "R", "+5V", "VREF"), ("R6", "10k", "R", "VREF", "GND"),
             ("C14", "1u", "C", "SIG_IN", "RIN"), ("R3", "10k", "R", "RIN", "INN"),
             ("R4", "100k", "R", "INN", "AMP_OUT"), ("C13", "1n", "C", "INN", "AMP_OUT"),
             ("C7", "100n 16V", "C", "+12V", "-12V")]
    pins = {3: ("+IN", "VREF"), 2: ("-IN", "INN"), 1: ("OUT", "AMP_OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}
    g = opamp_circuit_board(parts, [("U3", pins)], out_net="AMP_OUT")
    g.fields("C13", **{"Cbias": "0.4nF@40V", "Rated Voltage": "50V"})
    g.fields("C7", **{"Cbias": "40nF@8V"})
    return g


def test_a_feedback_capacitor_sits_at_its_dc_operating_point(tmp_path):
    """Audit round 7, AA-1: C13 across the feedback resistor of an AC-coupled inverting stage sees 0 V at DC (the
    virtual ground: out = in− = VREF, no DC current in R4) and keeps its nominal 1 nF; before, each end was bounded
    by the op-amp's supply on its own and C13 read 5 V."""
    from groundtruth.analysis.bias import at_dc, cap_voltages
    from groundtruth.components.models import ComponentModel
    board, sc, _ = make(tmp_path, _feedback_board(), "fb", nets=())
    sc.config.components["U3"].model = ComponentModel("amplifier", {"gbw": 1e6})
    v = cap_voltages(board, sc.config)
    assert v["C13"][0] == pytest.approx(0.0, abs=1e-6) and "DC operating point" in v["C13"][1]
    _, rows, _ = at_dc(board, sc.config)
    assert next(r for r in rows if r.ref == "C13").c == pytest.approx(1e-9, rel=1e-6)


def test_a_capacitor_across_plus_and_minus_rails_sees_both(tmp_path):
    """Audit round 7, AA-2: C7 between +12V and −12V sees 24 V (the rail's sign kept: safety.signed_rail_voltage);
    it is derated there, and the cap_voltage check flags its 16 V rating. Before, −12V read +12 V: 0 V across C7."""
    from groundtruth.analysis.bias import cap_voltages
    from groundtruth.analysis.safety import check_capacitors, voltage_model
    board, sc, _ = make(tmp_path, _feedback_board(), "pm", nets=())
    assert cap_voltages(board, sc.config)["C7"][0] == pytest.approx(24.0)
    found = []
    check_capacitors(board, sc.config, voltage_model(board, sc.config), sc.config.safety,
                     lambda check, name, msg, *a, **k: found.append((check, name, k.get("severity"))))
    assert ("cap_voltage", "C7", "error") in found
