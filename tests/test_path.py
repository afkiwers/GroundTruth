"""The signal path's circuit model (emi/path.py) against closed forms: a Thevenin source, line stretches, series
parts and the receivers' load."""
import math

import numpy as np
import pytest

from groundtruth.emi.path import PathLine, PathLoad, PathPart, PathSource, path_currents
from groundtruth.emi.spectrum import line_current, trapezoid_harmonics

F = np.geomspace(1e6, 3e9, 60)
V = np.ones_like(F)


def test_one_line_without_parts_is_the_old_line_current():
    got = path_currents(F, V, [PathLine(52.0, 3.2, 0.04)], PathSource(25.0), PathLoad(5e-12))
    want = line_current(F, V, 25.0, 52.0, 3.2, 0.04, 5e-12)
    assert np.allclose(np.abs(got.i_mean), want, rtol=1e-12, atol=0)


def test_short_path_with_a_series_inductor_is_a_series_lc():
    """A negligible line: |I| = V / |Rs + jωL + 1/jωC|, with its series resonance."""
    L, C, Rs = 100e-9, 10e-12, 10.0
    part = PathPart("L1", lambda f: 1j * 2 * math.pi * f * L)
    got = path_currents(F, V, [PathLine(50.0, 1.0, 1e-7), part, PathLine(50.0, 1.0, 1e-7)], PathSource(Rs),
                        PathLoad(C))
    w = 2 * np.pi * F
    want = 1 / np.abs(Rs + 1j * w * L + 1 / (1j * w * C))
    assert np.allclose(np.abs(got.i_in), want, rtol=1e-3)
    assert np.allclose(np.abs(got.i_mean), want, rtol=1e-3)
    assert got.part_z["L1"] == pytest.approx(w * L)


def test_an_ac_coupling_capacitor_is_a_high_pass():
    """Series C into a resistive load: |I| = V / |Rs + R + 1/jωC|."""
    C, R, Rs = 100e-9, 50.0, 25.0
    part = PathPart("C1", lambda f: 1 / (1j * 2 * math.pi * f * C))
    got = path_currents(F, V, [PathLine(50.0, 1.0, 1e-7), part], PathSource(Rs), PathLoad(1e-15, R))
    want = 1 / np.abs(Rs + R + 1 / (1j * 2 * np.pi * F * C))
    assert np.allclose(np.abs(got.i_in), want, rtol=1e-3)


def test_a_matched_line_carries_v_over_2z0_at_every_frequency():
    got = path_currents(F, V, [PathLine(50.0, 4.0, 0.3)], PathSource(50.0), PathLoad(1e-18, 50.0))
    assert np.allclose(np.abs(got.i_in), 1 / 100.0, rtol=1e-3)        # (the load keeps 1 fF)
    assert np.allclose(np.abs(got.v_end), 0.5, rtol=1e-3)


def test_source_inductance_rolls_the_current_off_at_20_db_per_decade():
    Rs, Ls = 10.0, 10e-9                      # corner R/(2πL) = 159 MHz; a short line into a short-ish load
    got = path_currents(F, V, [PathLine(50.0, 1.0, 1e-7)], PathSource(Rs, Ls), PathLoad(1e-6))
    w = 2 * np.pi * F
    assert np.allclose(np.abs(got.i_in), 1 / np.abs(Rs + 1j * w * Ls + 1 / (1j * w * 1e-6)), rtol=1e-3)
    hi = F > 2e9
    slope = np.polyfit(np.log10(F[hi]), 20 * np.log10(np.abs(got.i_in[hi])), 1)[0]
    assert slope == pytest.approx(-20, abs=1.0)


def test_a_source_capacitance_shunts_part_of_the_current():
    """Output capacitance Cs to the return: the path gets V·Zp/(Zs + Zp) with Zp = Z_path ∥ 1/jωCs."""
    Rs, Cs, R = 50.0, 20e-12, 50.0
    got = path_currents(F, V, [PathLine(50.0, 1.0, 1e-5)], PathSource(Rs, 0.0, Cs), PathLoad(1e-18, R))
    w = 2 * np.pi * F
    zp = 1 / (1 / R + 1j * w * Cs)
    want = np.abs(zp / (Rs + zp) / R)
    assert np.allclose(np.abs(got.i_in), want, rtol=1e-3)


def test_a_fall_time_of_its_own_gives_the_trapezoids_spectrum():
    """Separate rise and fall times: the numerical Fourier series of the waveform; equal edges: the old formula."""
    f0, tr, tf, A, D = 10e6, 2e-9, 6e-9, 3.3, 0.4
    f, a = trapezoid_harmonics(f0, tr, A, D, 1e9, fall_time=tf)
    T = 1 / f0
    t = np.linspace(0, T, 200_000, endpoint=False)
    t1 = D * T + tr / 2 - tf / 2                 # the fall starts here (duty at half amplitude)
    x = np.clip(t / tr, 0, 1) * A
    x = np.where(t > t1, A * np.clip(1 - (t - t1) / tf, 0, 1), x)
    c = np.fft.rfft(x) / len(t) * 2
    n = np.round(f / f0).astype(int)
    assert np.allclose(a[:20], np.abs(c[n[:20]]), rtol=2e-3, atol=1e-6)
    f2, a2 = trapezoid_harmonics(f0, tr, A, D, 1e9, fall_time=tr)
    f3, a3 = trapezoid_harmonics(f0, tr, A, D, 1e9)
    assert np.array_equal(a2, a3)


def _path_setup(tmp_path, through=("C1",), **net):
    from boardgen import series_path_board
    from conftest import make
    from groundtruth.components.models import ComponentModel
    from groundtruth.config.settings import NetSpec
    spec = NetSpec("SIG_A", "U1.1", ["U2.1"], 20e6, 1e-9, through=list(through), **net)
    board, sc, _ = make(tmp_path, series_path_board(), "path", nets=(spec,), solve_points=3, max_solves=4)
    cfg = sc.config
    cfg.components["L1"].model = ComponentModel("inductor", {"L": 1e-6, "R": 0.1})
    return board, cfg


def test_a_path_through_r_l_and_an_ac_coupling_c_is_found_and_each_part_counts(tmp_path):
    """U1 → 33 Ω → 1 µH → 100 nF (named in `through`) → U2, four nets: the route runs through all three, the
    path has four stretches and three parts, and the inductor's |Z| = ωL at the worst harmonic; the current along
    the path above its resonance with the load is far below that of the same path with the inductor shorted."""
    from groundtruth.analysis.simulate import simulate
    from groundtruth.components.models import ComponentModel
    board, cfg = _path_setup(tmp_path)
    res = simulate(board, cfg)
    rr = res.routes[0]
    assert not rr.error, rr.error
    kinds = [e["kind"] for e in rr.path["elements"]]
    assert kinds == ["line", "part", "line", "part", "line", "part", "line"]
    parts = {e["ref"]: e for e in rr.path["elements"] if e["kind"] == "part"}
    assert set(parts) == {"R1", "L1", "C1"}
    f = rr.path["f"]
    assert parts["L1"]["z_ohm"] == pytest.approx(math.hypot(0.1, 2 * math.pi * f * 1e-6), rel=1e-6)
    cfg.components["L1"].model = ComponentModel("short", {})
    shorted = simulate(board, cfg).routes[0]
    i_l = {e["f"]: e["current_a"] for e in rr.emissions}
    i_s = {e["f"]: e["current_a"] for e in shorted.emissions}
    hi = [f for f in i_l if f > 200e6]          # above the inductor's resonance with the load (~60 MHz)
    assert hi and sum(i_l[f] for f in hi) < 0.2 * sum(i_s[f] for f in hi)


def test_a_capacitor_not_named_in_through_blocks_the_path_and_the_message_names_it(tmp_path):
    from groundtruth.rules.checks import run_checks
    board, cfg = _path_setup(tmp_path, through=())
    issues = [i for i in run_checks(board, cfg).issues if i.check == "route_missing"]
    assert issues and "C1 (cap)" in issues[0].message and "through" in issues[0].message


def test_the_source_and_path_fields_survive_the_settings_file(tmp_path):
    """fall_time, driver_nh, driver_pf, load_ohms and through are written (only when set) and read back."""
    from groundtruth.config.settings import SettingsFile, validate
    board, cfg = _path_setup(tmp_path, fall_time=3e-9, driver_nh=1.5, driver_pf=4.0, load_ohms=50.0)
    n = cfg.nets[0]
    assert n.fall_time == pytest.approx(3e-9)                   # written as "3ns"
    assert (n.driver_nh, n.driver_pf, n.load_ohms, n.through) == (1.5, 4.0, 50.0, ["C1"])
    path = next(tmp_path.glob("groundtruth/*.groundtruth.yaml"))
    text = path.read_text(encoding="utf-8")
    assert "fall_time: 3ns" in text and "through:" in text and "load_ohms: 50" in text
    assert validate(SettingsFile.load(path).config, board) == []
    n.through, n.load_ohms = ["X9"], -1.0
    errors = validate(cfg, board)
    assert any("X9" in e for e in errors) and any("load_ohms" in e for e in errors)


def test_the_report_shows_the_signal_path(tmp_path):
    from groundtruth.analysis.simulate import simulate
    from groundtruth.report.html import render_html
    board, cfg = _path_setup(tmp_path)
    res = simulate(board, cfg)
    page = render_html(board, res.issues, res.routes, "simulate")
    assert "Signal path at" in page and "L1" in page and "inductor" in page


def test_a_shunt_at_the_end_is_the_same_as_a_load_resistance():
    from groundtruth.emi.path import PathShunt
    a = path_currents(F, V, [PathLine(50.0, 3.0, 0.02), PathShunt("R9", lambda f: 50.0)], PathSource(25.0),
                      PathLoad(5e-12))
    b = path_currents(F, V, [PathLine(50.0, 3.0, 0.02)], PathSource(25.0), PathLoad(5e-12, 50.0))
    assert np.allclose(a.i_in, b.i_in, rtol=1e-12) and np.allclose(a.v_end, b.v_end, rtol=1e-12)


def test_an_rc_low_pass_splits_the_current_into_its_loops():
    """Rs, then a shunt C to ground mid-path, then a 1 kΩ load (lines negligible): the closed forms, and the loop
    currents (the shunt's, the receivers') add up to the current into the path."""
    from groundtruth.emi.path import PathShunt
    Rs, C, R = 100.0, 1e-9, 1e3
    zc = lambda f: 1 / (1j * 2 * math.pi * f * C)       # noqa: E731
    got = path_currents(F, V, [PathLine(50.0, 1.0, 1e-7), PathShunt("C9", zc), PathLine(50.0, 1.0, 1e-7)],
                        PathSource(Rs), PathLoad(1e-15, R))
    w = 2 * np.pi * F
    zp = 1 / (1j * w * C + 1 / R)
    i_in = 1 / (Rs + zp)
    assert np.allclose(got.i_in, i_in, rtol=1e-3)
    assert np.allclose(got.shunt_i["C9"], i_in * zp * 1j * w * C, rtol=1e-3)
    loops = got.loop_currents()
    assert len(loops) == 2
    assert np.allclose(loops[0] + loops[1], got.group_mean[0], rtol=1e-12)
    assert np.allclose(loops[0], got.shunt_i["C9"], rtol=1e-3)      # (the line between is negligible)
    assert np.abs(loops[1][-1]) < 0.01 * np.abs(loops[0][-1])         # at 3 GHz the C takes it all


def _shunt_setup(tmp_path, shunt, name, **net):
    from boardgen import series_path_board
    from conftest import make
    from groundtruth.components.models import ComponentModel
    from groundtruth.config.settings import NetSpec
    spec = NetSpec("SIG_A", "U1.1", ["U2.1"], 20e6, 1e-9, through=["C1"], **net)
    board, sc, _ = make(tmp_path, series_path_board(shunt), name, nets=(spec,), solve_points=3, max_solves=4)
    cfg = sc.config
    cfg.components["L1"].model = ComponentModel("ferrite", {"L": 10e-9, "R": 0.05})
    return board, cfg


def test_a_termination_at_the_receiver_as_a_part_matches_load_ohms(tmp_path):
    """A 50 Ω part from SIG_D to GND just beyond the receiver is a shunt loop of its own; its loop nearly
    coincides with the receivers', so the superposition must give what load_ohms = 50 gives with one loop (within
    1 dB wherever the emission is not in a notch: the stub and the 2 mm between the GND vias differ). With the part
    2 mm before the receiver the loop is shorter: 0.9-1.3 dB less, as the geometry says."""
    from groundtruth.analysis.simulate import simulate
    board, cfg = _shunt_setup(tmp_path, ("R9", "50", "R", 46.5, "SIG_D"), "shunt")
    res = simulate(board, cfg).routes[0]
    assert not res.error, res.error
    assert any("R9" in n and "loop of its own" in n for n in res.notes), res.notes
    shunt_rows = [x for x in res.path["elements"] if x["kind"] == "shunt"]
    assert [x["ref"] for x in shunt_rows] == ["R9"] and shunt_rows[0]["z_ohm"] == pytest.approx(50.0)
    (tmp_path / "b").mkdir()
    board2, cfg2 = _shunt_setup(tmp_path / "b", None, "load", load_ohms=50.0)
    ref = simulate(board2, cfg2).routes[0]
    a = {r["f"]: r["e_dbuv"] for r in res.emissions}
    b = {r["f"]: r["e_dbuv"] for r in ref.emissions}
    diff = [abs(a[f] - b[f]) for f in a if f in b and b[f] > max(b.values()) - 20]
    assert diff and max(diff) < 1.0, max(diff)


def test_a_filter_capacitor_mid_path_is_a_loop_and_a_large_pull_down_is_not(tmp_path):
    """1 nF from SIG_B to GND right after the 33 Ω (an RC low-pass, corner 4.8 MHz): above it the current returns
    through the capacitor, so its loop (driver to x = 20 mm) carries most of it and the emission above 100 MHz
    changes against the same path without it. A 1 MΩ pull-down there carries < 1 % of the current: in the
    circuit, but no loop of its own (no solves; 100 kΩ carries ~1 % at the fundamental on this lightly loaded path)."""
    from groundtruth.analysis.simulate import simulate
    (tmp_path / "c").mkdir()
    (tmp_path / "r").mkdir()
    (tmp_path / "n").mkdir()
    board, cfg = _shunt_setup(tmp_path / "c", ("C9", "1n", "C", 20.0, "SIG_B"), "rc")
    rc = simulate(board, cfg).routes[0]
    assert any("C9" in n and "loop of its own" in n for n in rc.notes)
    row = next(x for x in rc.path["elements"] if x["kind"] == "shunt")
    assert row["ref"] == "C9" and row["i_ma"] > 0.5 * rc.path["i_in_ma"]
    board, cfg = _shunt_setup(tmp_path / "r", ("R9", "1M", "R", 20.0, "SIG_B"), "pd")
    pd = simulate(board, cfg)
    assert not any("loop of its own" in n for n in pd.routes[0].notes)
    assert [x["ref"] for x in pd.routes[0].path["elements"] if x["kind"] == "shunt"] == ["R9"]
    assert not pd.setups[0].fine.loops
    board, cfg = _shunt_setup(tmp_path / "n", None, "none")
    none = simulate(board, cfg).routes[0]
    a = {r["f"]: r["e_dbuv"] for r in rc.emissions}
    b = {r["f"]: r["e_dbuv"] for r in none.emissions}
    assert max(abs(a[f] - b[f]) for f in a if f > 100e6) > 3.0


def test_the_report_shows_a_shunt_on_the_path(tmp_path):
    from groundtruth.analysis.simulate import simulate
    from groundtruth.report.html import render_html
    board, cfg = _shunt_setup(tmp_path, ("C9", "1n", "C", 20.0, "SIG_B"), "rep")
    res = simulate(board, cfg)
    page = render_html(board, res.issues, res.routes, "simulate")
    assert "C9" in page and "cap to GND" in page


def _amp(gain=1.0, bandwidth=None, slew=None, z_out=1e-6, c_in=1e-15):
    from groundtruth.emi.path import PathAmp
    g = (lambda f: gain / (1 + 1j * f / bandwidth)) if bandwidth else (lambda f: complex(gain))
    return PathAmp("U5", g, lambda f: 1 / (1j * 2 * math.pi * f * c_in), z_out, slew)


def test_an_op_amp_stage_passes_gain_times_its_input_voltage_on():
    """Gain 1, an ideal output into a high-impedance receiver: the output side's receiver voltage is the input
    side's (the amp input's) voltage; gain -10 inverts and multiplies it."""
    from groundtruth.emi.path import amplified_path
    line = [PathLine(50.0, 1.0, 1e-7)]
    for gain in (1.0, -10.0):
        a, b = amplified_path(F, V, [line, line], [_amp(gain)], PathSource(25.0), PathLoad(1e-15))
        assert np.allclose(b.v_end, gain * a.v_end, rtol=1e-6)
        assert np.allclose(np.abs(a.v_end), 1.0, rtol=1e-3)       # 25 Ω into 1 fF: the open-circuit voltage


def test_an_op_amp_pole_rolls_off_at_its_bandwidth_and_slew_caps_the_output():
    from groundtruth.emi.path import amplified_path
    line = [PathLine(50.0, 1.0, 1e-7)]
    f = np.array([1e5, 1e6, 1e7, 1e8])
    a, b = amplified_path(f, np.ones(4), [line, line], [_amp(10.0, bandwidth=1e6)], PathSource(25.0),
                          PathLoad(1e-15))
    gain = np.abs(b.v_end / a.v_end)
    assert gain[1] == pytest.approx(10 / math.sqrt(2), rel=1e-3)              # -3 dB at the bandwidth
    assert 20 * np.log10(gain[2] / gain[3]) == pytest.approx(20.0, abs=0.1)  # -20 dB per decade above
    a, b = amplified_path(f, np.ones(4), [line, line], [_amp(10.0, slew=1e8)], PathSource(25.0), PathLoad(1e-15))
    cap = 1e8 / (2 * np.pi * f)                                               # 100 V/µs: 159 V at 100 kHz ...
    assert np.abs(b.v_end[0]) == pytest.approx(10.0, rel=1e-3)               # ... no limit there
    assert np.abs(b.v_end[3]) == pytest.approx(cap[3], rel=1e-3)             # 0.16 V at 100 MHz: capped


def _opamp_setup(tmp_path, **fields):
    from boardgen import opamp_path_board
    from conftest import make
    from groundtruth.config.settings import NetSpec
    spec = NetSpec("SIG_A", "U1.1", ["U2.1"], 20e6, 1e-9)
    board, sc, _ = make(tmp_path, opamp_path_board(**fields), "amp", nets=(spec,), solve_points=3, max_solves=4)
    return board, sc.config


def test_a_path_through_an_op_amp_is_two_loops_and_its_gain_counts(tmp_path):
    """U1 → 33 Ω → op-amp U5 (a Gain field makes it an amplifier) → U2: the route runs through U5 from +IN to OUT;
    the path is two stages, each a current loop: the input side returns at U5's GND pin, the output side from
    the receiver to U5's GND pin (the main loop). Gain 10 against gain 1 with a tiny input capacitance (the input
    loop carries almost nothing): the output side's current is 10 times, +20 dB below 1 GHz. (With the
    default 2 pF the input loop is comparable to the output's at gain 1, and the two loops add as vectors: +17 dB
    at 20 MHz there, more where they cancel at gain 1.)"""
    from groundtruth.analysis.simulate import simulate
    (tmp_path / "g1").mkdir()
    (tmp_path / "g10").mkdir()
    board, cfg = _opamp_setup(tmp_path / "g10", Gain="10", Cin="0.01pF")
    assert cfg.model_for("U5").kind == "amplifier" and cfg.model_for("U5").params["gain"] == 10
    res = simulate(board, cfg)
    rr = res.routes[0]
    assert not rr.error, rr.error
    kinds = [e["kind"] for e in rr.path["elements"]]
    assert kinds == ["line", "part", "line", "amp", "line"], kinds
    assert next(e for e in rr.path["elements"] if e["kind"] == "amp")["gain"] == pytest.approx(10.0)
    assert any("op-amp(s) U5" in n and "2 stages" in n for n in rr.notes), rr.notes
    s = res.setups[0]
    assert len(s.fine.loops) == 1 and s.drv_ret.pin == "U5.2"          # the output side returns at U5's GND
    board1, cfg1 = _opamp_setup(tmp_path / "g1", Gain="1", Cin="0.01pF")
    one = simulate(board1, cfg1).routes[0]
    a = {r["f"]: r["e_dbuv"] for r in rr.emissions}
    b = {r["f"]: r["e_dbuv"] for r in one.emissions}
    d = {f: a[f] - b[f] for f in a if f in b}
    low = [x for f, x in d.items() if f < 1e9]       # the input stage's own line charge: 0.5-1.1 dB short of 20
    assert low and max(abs(x - 20.0) for x in low) < 1.5, low
    assert min(d.values()) > 19.0                    # (above ~2 GHz the loops cancel at gain 1: more than 20)


def test_an_op_amps_gbw_from_its_fields_rolls_its_gain_off(tmp_path):
    """GBW 10 MHz at gain 10: a 1 MHz bandwidth, so at the 20 MHz fundamental the gain is ~0.5 (|A| ≈ 10/20)."""
    from groundtruth.analysis.simulate import simulate
    board, cfg = _opamp_setup(tmp_path, Gain="10", GBW="10MHz")
    rr = simulate(board, cfg).routes[0]
    amp = next(e for e in rr.path["elements"] if e["kind"] == "amp")
    f = rr.path["f"]
    assert amp["gain"] == pytest.approx(10 / math.hypot(1, f / 1e6), rel=1e-6)


def test_gt_labels_name_a_dual_op_amps_input_and_output(tmp_path):
    """Pins named so the name rule can't tell (a dual op-amp's IN1+ / OUT1): GT = amp_in:3, amp_out:1 on the symbol
    makes the part an amplifier with those pins; the path runs through it."""
    import re as _re
    from boardgen import opamp_path_board
    from conftest import make
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.tags import parse
    from groundtruth.config.settings import NetSpec
    tags, _, problems = parse("amp_in:3, AMP_OUT:OUTA", "U5")
    assert [(t.kind, t.ident, t.role) for t in tags] == [("amp", "3", "in"), ("amp", "OUTA", "out")] and not problems
    g = opamp_path_board()
    g.items = [_re.sub(r'pinfunction "\+IN_3"', 'pinfunction "IN1+_3"', it).replace('pinfunction "OUT_1"',
                                                                                  'pinfunction "OUT1_1"') for it in g.items]
    g.label("U5", "amp_in:3, amp_out:1")
    spec = NetSpec("SIG_A", "U1.1", ["U2.1"], 20e6, 1e-9)
    board, sc, _ = make(tmp_path, g, "dual", nets=(spec,), solve_points=2, max_solves=3)
    cfg = sc.config
    assert cfg.model_for("U5").kind == "amplifier"
    rr = simulate(board, cfg).routes[0]
    assert not rr.error, rr.error
    assert [e["kind"] for e in rr.path["elements"]].count("amp") == 1


def test_the_report_shows_an_op_amp_stage(tmp_path):
    from groundtruth.analysis.simulate import simulate
    from groundtruth.report.html import render_html
    board, cfg = _opamp_setup(tmp_path, Gain="10")
    res = simulate(board, cfg)
    page = render_html(board, res.issues, res.routes, "simulate")
    assert "op-amp stage, gain 10" in page


def test_a_series_part_after_the_op_amp_keeps_its_own_impedance(tmp_path):
    """U1 → R1 → op-amp U5 → R2 4.7 kΩ → U2 (refboard's ADC path: buffer, then R303): each op-amp stage
    gets its own series parts. Before, the stage after U5 paired its part step with the route's first part left
    over, U5 itself: R2 was modelled as the op-amp (1 Ω) instead of 4.7 kΩ."""
    from boardgen import opamp_path_board
    from conftest import make
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import NetSpec
    spec = NetSpec("SIG_A", "U1.1", ["U2.1"], 20e6, 1e-9, through=["R2"])
    board, sc, _ = make(tmp_path, opamp_path_board(r_after="4k7", Gain="1"), "amp", nets=(spec,), solve_points=3,
                        max_solves=4)
    rr = simulate(board, sc.config).routes[0]
    assert not rr.error, rr.error
    parts = [(e["kind"], e.get("ref")) for e in rr.path["elements"] if e["kind"] != "line"]
    assert parts == [("part", "R1"), ("amp", "U5"), ("part", "R2")], parts
    r2 = next(e for e in rr.path["elements"] if e.get("ref") == "R2")
    assert r2["model"] == "resistor" and r2["z_ohm"] == pytest.approx(4700, rel=0.01)


def test_labelled_ends_join_into_one_path_through_an_op_amp(tmp_path):
    """GT labels as on refboard's current sense: the driver's pin (sig_drv), the op-amp's input and output
    (amp_in / amp_out) and the receiver's pin (sig_rx) give one net from U1 to U2 through U5, with the 4.7 kΩ R2
    (which doesn't pass a signal by default) in `through`; the nets in between carry no label of their own."""
    from boardgen import opamp_path_board
    from groundtruth.adapters.file_source import loads_board
    from groundtruth.analysis.detect import detect_nets
    from groundtruth.config.settings import Config
    g = opamp_path_board(r_after="4k7")
    g.label("U1", "sig_drv:1")
    g.label("U5", "amp_in:3, amp_out:1")
    g.label("U2", "sig_rx:1")
    board = loads_board(g.text(), "amp.kicad_pcb")
    found = [d for d in detect_nets(board, Config(), 0) if d.spec.driver == "U1.1"]
    assert len(found) == 1, [(d.spec.name, d.spec.driver, d.spec.receivers) for d in detect_nets(board, Config(), 0)]
    spec = found[0].spec
    assert spec.receivers == ["U2.1"] and spec.through == ["R2"], (spec.receivers, spec.through, found[0].reasons)
    assert not any(d.spec.driver == "U2.1" for d in detect_nets(board, Config(), 0))
    from groundtruth.analysis.tags import board_tags
    assert "op-amp labels on U5" in board_tags(board).summary()


def _labelled_opamp_board():
    """The op-amp test board with refboard's labels: U1 drives (sig_drv:1), U5 is the op-amp stage
    (amp_in:3, amp_out:1), U2 receives (sig_rx:1), R2 4.7 kΩ after the op-amp."""
    from boardgen import opamp_path_board
    from groundtruth.adapters.file_source import loads_board
    g = opamp_path_board(r_after="4k7")
    g.label("U1", "sig_drv:1")
    g.label("U5", "amp_in:3, amp_out:1")
    g.label("U2", "sig_rx:1")
    return loads_board(g.text(), "amp.kicad_pcb")


def test_stale_detected_entries_are_replaced_and_their_pieces_removed(tmp_path):
    """Settings made before the labels list the path's pieces (as refboard's did: /CPU/ADC_I_RAIL_1 from
    C304.1, Net-(U301-+) and Net-(U301--) on their own), unreviewed, and the op-amp as 'open'. Loading the board
    makes the labelled op-amp an amplifier; Detect replaces the unreviewed entry of the same name (keeping its
    tick) and removes the pieces inside the new path; a reviewed entry is never touched."""
    from groundtruth.analysis.detect import detect_nets, refreshes
    from groundtruth.components.models import ComponentModel
    from groundtruth.config.settings import NetSpec, SettingsFile
    board = _labelled_opamp_board()
    sc = SettingsFile.new(tmp_path / "amp.groundtruth.yaml", board)
    name = next(d for d in detect_nets(board, sc.config, 0) if d.spec.driver == "U1.1").spec.name
    sc.set_component("U5", ComponentModel("open"))                         # read before it had its labels
    piece, kept = [n for n in ("SIG_B", "SIG_C", "SIG_D") if n != name][:2]
    sc.set_nets([NetSpec(name, "R2.2", ["U2.1"], 10e6, 2e-9, source="auto", reviewed=False, enabled=False),
                 NetSpec(piece, "R1.2", ["U5.3"], 10e6, 2e-9, source="auto", reviewed=False),
                 NetSpec(kept, "U5.1", ["R2.1"], 10e6, 2e-9, source="auto", reviewed=True)])
    sc.merge_board(board)
    assert sc.config.model_for("U5").kind == "amplifier"
    found = detect_nets(board, sc.config, 0)
    updates, fragments = refreshes(found, sc.config.nets)
    assert [(d.spec.name, old.driver, d.spec.driver) for d, old in updates] == [(name, "R2.2", "U1.1")]
    assert fragments == [piece]                                           # the reviewed entry is not touched
    replaced, dropped = sc.refresh_detected([d.spec for d, _ in updates], fragments)
    assert (replaced, dropped) == ([name], [piece])
    nets = {n.name: n for n in sc.config.nets}
    assert set(nets) == {name, kept} and nets[kept].reviewed
    assert nets[name].driver == "U1.1" and nets[name].through == ["R2"] and nets[name].enabled is False


def test_selecting_a_listed_net_lights_its_whole_path():
    """The Nets tab's highlight in KiCad: a listed net lights every net of its path (through R1, the op-amp and its
    Pass through R2) up to its driver, not only its own copper."""
    from groundtruth.analysis.highlight import nets_highlight
    from groundtruth.components.models import ComponentModel
    from groundtruth.config.settings import ComponentEntry, Config, NetSpec
    board = _labelled_opamp_board()
    cfg = Config()
    cfg.components = {"R1": ComponentEntry("R1", "33", "R", ComponentModel("resistor", {"R": 33.0})),
                      "U5": ComponentEntry("U5", "OPA376", "SOT-23-5", ComponentModel("amplifier"))}
    cfg.nets = [NetSpec("SIG_D", "U1.1", ["U2.1"], 10e6, 2e-9, through=["R2"])]
    h = nets_highlight(["SIG_D"], board, cfg)
    lit = {p.net for p in board.pads() if p.uuid in set(h.focus)}
    assert {"SIG_A", "SIG_B", "SIG_C", "SIG_D"} <= lit, lit
    assert "GND" not in lit and "its path through 4 nets" in h.text


def test_a_path_chain_lists_its_parts_in_route_order_and_what_is_across_it(tmp_path):
    """The Signal paths tab's Chain column (analysis.route.path_chain): driver, the series parts with their value
    and the op-amp with its gain in the order the route passes them, the receivers; without R2 in Pass through
    (4.7 kΩ passes no signal by default) there is no route, and the chain says why."""
    from conftest import make
    from boardgen import opamp_path_board
    from groundtruth.analysis.route import path_chain
    from groundtruth.config.settings import NetSpec
    spec = NetSpec("SIG_A", "U1.1", ["U2.1"], 20e6, 1e-9, through=["R2"])
    board, sc, _ = make(tmp_path, opamp_path_board(r_after="4k7", Gain="2"), "amp", nets=(spec,))
    c = path_chain(board, sc.config, spec)
    assert not c.error, c.error
    assert c.text() == "U1.1 → R1 33 → U5 ×2 → R2 4k7 → U2.1", c.text()
    blocked = path_chain(board, sc.config, NetSpec("SIG_A", "U1.1", ["U2.1"], 20e6, 1e-9))
    assert blocked.error.startswith("no copper path") and blocked.parts == []


def _pozar_alpha(f, w, z0, eeff, er, tan_d, sigma=5.8e7):
    """A microstrip's attenuation (Np/m) by Pozar's closed forms: conductor α_c = R_s/(Z0·W) with R_s = √(ωμ0/2σ)
    (δ ≪ t), dielectric α_d = k0·εr(εeff - 1)·tan δ / (2√εeff(εr - 1)) [Pozar 2012]."""
    rs = math.sqrt(2 * math.pi * f * 4e-7 * math.pi / (2 * sigma))
    k0 = 2 * math.pi * f / 299792458.0
    return rs / (z0 * w) + k0 * er * (eeff - 1) * tan_d / (2 * math.sqrt(eeff) * (er - 1))


@pytest.mark.parametrize("f", [300e6, 1e9, 3e9])
def test_a_matched_lossy_line_attenuates_as_pozars_microstrip(f):
    """A 0.2 mm wide, 35 µm thick microstrip (Z0 50 Ω, εeff 3.4 on εr 4.5, tan δ 0.02), 100 mm long, into its own Z0:
    the end voltage is the input's times e^(-αl), α from Pozar's microstrip formulas (δ ≪ t at these frequencies:
    t_eff = δ within 0.01 %), within 0.5 %; the lossless line (no width) keeps |V| (A36)."""
    z0, eeff, er, tan_d, w, length = 50.0, 3.4, 4.5, 0.02, 0.2e-3, 0.1
    line = PathLine(z0, eeff, length, width_m=w, thickness_m=35e-6, er=er, tan_d=tan_d)
    r = path_currents([f], [1.0], [line], PathSource(z0), PathLoad(0.0, z=lambda _f: complex(z0)))
    v_in = 1.0 / 2                                        # the matched source divides the open-circuit 1 V by 2
    got = abs(r.v_end[0]) / v_in
    assert got == pytest.approx(math.exp(-_pozar_alpha(f, w, z0, eeff, er, tan_d) * length), rel=0.005)
    lossless = path_currents([f], [1.0], [PathLine(z0, eeff, length)], PathSource(z0),
                             PathLoad(0.0, z=lambda _f: complex(z0)))
    assert abs(lossless.v_end[0]) == pytest.approx(v_in, rel=1e-9)


def test_an_open_quarter_wave_stub_is_z0_tanh_alpha_l_not_a_short():
    """A stripline (εeff = εr 4.2, tan δ 0.02) a quarter wave long at 1 GHz with its end open: its input is
    Z0·tanh(αl) [Pozar 2012] (lossless: 0, so a shunt beside it would see an unbounded resonance); the current
    into it from a 1 Ω source is 1 V / (1 Ω + Z_in), within 0.5 %."""
    f, z0, er = 1e9, 40.0, 4.2
    length = 299792458.0 / (f * math.sqrt(er)) / 4
    line = PathLine(z0, er, length, width_m=0.15e-3, thickness_m=35e-6, er=er, tan_d=0.02)
    alpha = float(line.alpha([f])[0])
    z_in = z0 * math.tanh(alpha * length)
    assert z_in > 0.5                                     # a real loss: half an ohm and more, not 0
    r = path_currents([f], [1.0], [line], PathSource(1.0), PathLoad(1e-18))
    assert abs(r.i_in[0]) == pytest.approx(1.0 / (1.0 + z_in), rel=0.005)
