"""A buck's control loop (analysis/loop.py, the plan's M5) against closed forms: the parts found on the board (the
controller by its SW pin, the ITH network, the divider, the sense shunt), the loop gain against Ridley's
current-mode model written out here, its sampling double pole, the asymptotic crossover gm·Rc·H/(2π·R_i·C), the
voltage-mode LC pair, the margins read where |T| = 1, and what is said when a figure is missing."""
import math

import numpy as np
import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.analysis.loop import Rat, analyse_loop, find_loop, roots_scaled
from groundtruth.analysis.transfer import pz_rows
from groundtruth.config.settings import ConverterSpec

F_SW, V_IN, V_OUT, I_OUT = 500e3, 12.0, 3.3, 2.0
L, C, R_SENSE, K, GM = 4.7e-6, 22e-6, 0.01, 0.05, 1.8e-3


def _setup(tmp_path, name, **fields):
    """The loop board with the controller's fields; the stage's spec; C2 22 µF without ESR, L1 4.7 µH without DCR."""
    g = discrete_buck_board(loop=True)
    g.fields("U8", **{"Control Mode": "peak current", "EA gm": "1.8mmho", "Current Sense Gain": str(K), **fields})
    b, sc, _ = make(tmp_path, g, name, nets=())
    cfg = sc.config
    cfg.components["C2"].model.params.update(C=C, ESR=0.0)
    cfg.components["L1"].model.params.update(L=L, R=0.0)
    spec = ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"], f_sw=F_SW,
                         i_out=I_OUT, v_in=V_IN, v_out=V_OUT)
    return b, cfg, spec


def test_the_loops_parts_are_found(tmp_path):
    """U8 by its SW1 pin on the switch node (channel 1); RC1 / CC1 / CH1 on ITH1; RT1 from VOUT over RB1 on V_FB1;
    the 10 mΩ RS between the SENSE pins' 10R filters; the controller's figures from its fields."""
    b, cfg, spec = _setup(tmp_path, "parts")
    lp = find_loop(b, cfg, spec)
    assert (lp.controller, lp.channel, lp.mode) == ("U8", "1", "current")
    assert lp.rc == pytest.approx(2.2e3) and lp.cc == pytest.approx(22e-9) and lp.chf == pytest.approx(22e-12)
    assert [x[0] for x in lp.top] == ["RT1"] and [x[0] for x in lp.bottom] == ["RB1"]
    assert lp.r_sense == pytest.approx(R_SENSE) and lp.sense_parts == ["RS"]
    assert lp.params["gm"][0] == pytest.approx(GM) and lp.params["cs_gain"][0] == pytest.approx(K)


def ridley(f, se):
    """Ridley's peak-current-mode loop gain written out: gm·Z(ITH)·H · (R/R_i)/(1 + R·T_s/L·(m_c·D' − ½)) ·
    1/(1 + s/ω_p) · F_h(s), with no ESR."""
    s = 2j * np.pi * np.asarray(f)
    r, ri, ts, d = V_OUT / I_OUT, R_SENSE / K, 1 / F_SW, V_OUT / V_IN
    sn = R_SENSE * (V_IN - V_OUT) / L
    mc = 1 + se / sn
    q = mc * (1 - d) - 0.5
    wp = 1 / (C * r) + ts * q / (L * C)
    wn, qp = math.pi * F_SW, 1 / (math.pi * q)
    fh = 1 / (1 + s / (wn * qp) + s * s / wn ** 2)
    gvc = (r / ri) / (1 + r * ts / L * q) / (1 + s / wp) * fh
    zc = 1 / (1 / (2.2e3 + 1 / (s * 22e-9)) + s * 22e-12)
    return GM * zc * (25e3 / 125e3) * gvc, wn, qp


def test_the_loop_gain_is_ridleys(tmp_path):
    """With 2 mV/µs of slope compensation (m_c 1.11): T(f) equals the written-out model (10⁻⁹), the sampling pole
    pair sits at f_sw/2 with Q_p = 1/(π·(m_c·D' − ½)) ≈ 1.05, exact. (50 mV/µs, 2.7 times the sensed up-slope,
    damps it to Q_p 0.15: two real poles, as the model gives too.)"""
    b, cfg, spec = _setup(tmp_path, "ridley", **{"Slope Compensation": "2mV/us"})
    res = analyse_loop(b, cfg, spec)
    assert not res.error, res.error
    t = res.response
    want, wn, qp = ridley(t.f, 2e3)
    assert np.allclose(t.h, want, rtol=1e-9)
    pair = [p for p in pz_rows(t.poles) if p["q"] and abs(p["f0"] / (F_SW / 2) - 1) < 0.01]
    assert len(pair) == 1 and pair[0]["q"] == pytest.approx(qp, rel=1e-6) and t.exact


def test_the_crossover_is_the_asymptote_and_the_margins_are_read_there(tmp_path):
    """Above the load pole and the compensator's zero and below f_sw/2, T ≈ gm·Rc·H/(R_i·sC): f_c within 10 % of
    gm·Rc·H/(2π·R_i·C) (29 kHz); |T(f_c)| = 1 and PM = 180° + ∠T(f_c); a gain margin where the phase reaches
    -180° above it."""
    b, cfg, spec = _setup(tmp_path, "fc")
    res = analyse_loop(b, cfg, spec)
    asym = GM * 2.2e3 * 0.2 / (2 * math.pi * (R_SENSE / K) * C)
    assert res.fc == pytest.approx(asym, rel=0.10)
    t = res.response
    h = np.interp(math.log(res.fc), np.log(t.f), np.abs(t.h))
    assert h == pytest.approx(1.0, rel=1e-3)
    assert res.pm == pytest.approx(180 + np.interp(math.log(res.fc), np.log(t.f), t.phase_deg()), abs=1e-6)
    assert 30 < res.pm < 120 and res.f180 > res.fc and res.gm_db > 0
    assert any("half the inductor's down-slope" in n for n in res.notes)         # no Slope Compensation field


def test_voltage_mode_has_the_lc_pair(tmp_path):
    """Voltage mode (Ramp 1 V): the plant's pole pair at 1/(2π·sqrt(LC)) with Q = R_load·sqrt(C/L) (no ESR, no
    DCR), exact."""
    b, cfg, spec = _setup(tmp_path, "vm", **{"Control Mode": "voltage", "Ramp": "1V"})
    res = analyse_loop(b, cfg, spec)
    assert not res.error, res.error
    f0 = 1 / (2 * math.pi * math.sqrt(L * C))
    q = (V_OUT / I_OUT) * math.sqrt(C / L)
    pair = [p for p in pz_rows(res.response.poles) if p["q"] and abs(p["f0"] / f0 - 1) < 1e-6]
    assert len(pair) == 1 and pair[0]["q"] == pytest.approx(q, rel=1e-6)


def test_what_is_said_without_a_model(tmp_path):
    """Constant on-time: no loop model; a peak-current controller without its current-sense gain: the fields to
    give named."""
    b, cfg, spec = _setup(tmp_path, "cot", **{"Control Mode": "COT"})
    assert "constant on-time" in analyse_loop(b, cfg, spec).error
    g = discrete_buck_board(loop=True)
    g.fields("U8", **{"Control Mode": "peak current", "EA gm": "1.8mmho"})
    b, sc, _ = make(tmp_path, g, "missing", nets=())
    spec.loop = {}
    err = analyse_loop(b, sc.config, spec).error
    assert "Current Sense Gain (or Vsense Max and ITH Range)" in err
    spec.loop = {"cs_gain": 0.05}                                   # the settings' override instead
    assert not analyse_loop(b, sc.config, spec).error


def test_rationals_and_scaled_roots():
    """Rat arithmetic against direct evaluation; roots found in s/ω0 for coefficients spanning decades."""
    a = Rat([1.0, 2.0], [1.0, 0.0, 3.0])
    b = Rat([2.0])
    s = 1j * 3.7
    assert (a * b + a.inv())(s) == pytest.approx(a(s) * 2 + 1 / a(s))
    w0 = 2 * math.pi * 1e6
    poly = np.polymul([1 / w0 ** 2, 1 / (w0 * 5), 1.0], [1 / (3 * w0), 1.0])
    rows = pz_rows(roots_scaled(poly, w0))
    assert [round(r["f0"] / 1e6, 6) for r in rows] == [1.0, 3.0] and rows[0]["q"] == pytest.approx(5.0)


def test_a_simulated_stage_carries_its_loop(tmp_path):
    """Simulate of the loop board's buck: the stage's result has its loop figures, the loop gain among its Bode
    responses, a phase-margin tile and a console line; a low phase margin is an issue (Cc 220 pF: the compensation
    zero at 329 kHz, far above the 29 kHz crossover, leaves ~9°)."""
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    from groundtruth.report.formats import converter_text
    from groundtruth.ui.kpi import kpis
    g = discrete_buck_board(loop=True)
    g.fields("U8", **{"Control Mode": "peak current", "EA gm": "1.8mmho", "Current Sense Gain": str(K)})
    b, sc, _ = make(tmp_path, g, "sim", nets=())
    spec = ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"], f_sw=F_SW,
                         i_out=I_OUT, v_in=V_IN, v_out=V_OUT)
    sc.add_detected_converters([spec], {})
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    res = simulate(b, cfg)
    (c,) = res.converters
    assert c.loop and c.loop["fc"] > 0 and c.loop["controller"] == "U8"
    assert any(r.quantity == "loop gain" for r in c.responses)
    assert any(t[0] == "phase margin" for t in kpis(c, cfg.rules))
    assert any("control loop (current mode, U8)" in line for line in converter_text(c))
    b.footprint("CC1").value = "220p"
    low = simulate(b, cfg)
    assert any(i.check == "loop_phase_margin" for i in low.issues), [n for n in low.converters[0].notes]


# ------------------------------------------------------------------------------------------------ boost
B_IN, B_OUT, B_I = 12.0, 24.0, 1.0


def _boost(tmp_path, name, **fields):
    """The boost loop board (12 V to 24 V at 1 A, f_sw 500 kHz) with the controller's fields; C2 22 µF without ESR,
    L1 4.7 µH without DCR."""
    from boardgen import boost_board
    g = boost_board(loop=True)
    g.fields("U8", **{"Control Mode": "peak current", "EA gm": "1.8mmho", "Current Sense Gain": str(K), **fields})
    b, sc, _ = make(tmp_path, g, name, nets=())
    cfg = sc.config
    cfg.components["C2"].model.params.update(C=C, ESR=0.0)
    cfg.components["L1"].model.params.update(L=L, R=0.0)
    spec = ConverterSpec("boost", "VOUT", "GND", switch_node="SW", high_side=["D1"], low_side=["Q1"],
                         topology="boost", f_sw=F_SW, i_out=B_I, v_in=B_IN, v_out=B_OUT)
    return b, cfg, spec


def boost_model(f, se):
    """The peak-current-mode boost's loop gain written out: gm·Z(ITH)·H · (D' − a·sL) / (sC + 1/R + a·D' +
    G·(D' − a·sL)) / R_i · F_h(s) with a = I_L/V_out, G = T_s·D'²·(m_c − ½)/L, no ESR or DCR."""
    s = 2j * np.pi * np.asarray(f)
    r, ri, ts, dp = B_OUT / B_I, R_SENSE / K, 1 / F_SW, B_IN / B_OUT
    a = (B_I / dp) / B_OUT
    mc = 1 + se / (R_SENSE * B_IN / L)
    g = ts * dp * dp * (mc - 0.5) / L
    wn, qp = math.pi * F_SW, 1 / (math.pi * (mc * dp - 0.5))
    fh = 1 / (1 + s / (wn * qp) + s * s / wn ** 2)
    fwd = dp - a * s * L
    gvc = fwd / (s * C + 1 / r + a * dp + g * fwd) / ri * fh
    zc = 1 / (1 / (10e3 + 1 / (s * 10e-9)) + s * 100e-12)
    return GM * zc * (10e3 / 300e3) * gvc, mc, g


def test_a_boosts_parts_are_found_on_its_output(tmp_path):
    """A boost's output is its rail (VOUT, behind the diode), not the inductor's far side (its input): RT1 is the
    divider's top; RS (the input shunt in series with L1) the sense resistor."""
    b, cfg, spec = _boost(tmp_path, "bparts")
    lp = find_loop(b, cfg, spec)
    assert (lp.controller, lp.channel, lp.mode) == ("U8", "1", "current")
    assert [x[0] for x in lp.top] == ["RT1"] and [x[0] for x in lp.bottom] == ["RB1"]
    assert lp.r_sense == pytest.approx(R_SENSE) and lp.sense_parts == ["RS"]
    assert lp.rc == pytest.approx(10e3) and lp.cc == pytest.approx(10e-9) and lp.chf == pytest.approx(100e-12)


def test_the_current_mode_boost_has_its_rhp_zero_and_pole(tmp_path):
    """Peak current mode (10 mV/µs of slope compensation): T equals the written-out model (10⁻⁹); a zero in the right
    half plane at D'²·R/L (203 kHz, exact), the dominant pole at (2/R + G·D')/(C − G·a·L) (Ridley's 2/(RC) plus the
    current loop's D'³ term, exact: above Ridley's simplified 2/(RC)), the sampling pair at f_sw/2 with
    Q_p = 1/(π·(m_c·D' − ½))."""
    b, cfg, spec = _boost(tmp_path, "bcm", **{"Slope Compensation": "10mV/us"})
    res = analyse_loop(b, cfg, spec)
    assert not res.error, res.error
    t = res.response
    want, mc, g = boost_model(t.f, 10e3)
    assert np.allclose(t.h, want, rtol=1e-9)
    dp, r = B_IN / B_OUT, B_OUT / B_I
    a = (B_I / dp) / B_OUT
    f_rhp = dp * dp * r / L / (2 * math.pi)
    rhp = [z for z in t.zeros if z.real > 0]
    assert len(rhp) == 1 and abs(rhp[0]) / (2 * math.pi) == pytest.approx(f_rhp, rel=1e-6)
    assert res.figures["f_rhp"] == pytest.approx(f_rhp)
    wp = (2 / r + g * dp) / (C - g * a * L)
    assert any(abs(p.imag) < 1e-6 * abs(p) and abs(-p.real / wp - 1) < 1e-6 for p in t.poles)
    qp = 1 / (math.pi * (mc * dp - 0.5))
    pair = [p for p in pz_rows(t.poles) if p["q"] and abs(p["f0"] / (F_SW / 2) - 1) < 0.01]
    assert len(pair) == 1 and pair[0]["q"] == pytest.approx(qp, rel=1e-6)
    assert wp > 2 / (r * C)


def test_the_voltage_mode_boost_has_its_lc_pair_at_d_prime(tmp_path):
    """Voltage mode (Ramp 1 V): the pole pair at D'/(2π·sqrt(LC)) with Q = D'·R·sqrt(C/L) and the right-half-plane
    zero at D'²·R/L (Erickson & Maksimovic ch. 8), exact."""
    b, cfg, spec = _boost(tmp_path, "bvm", **{"Control Mode": "voltage", "Ramp": "1V"})
    res = analyse_loop(b, cfg, spec)
    assert not res.error, res.error
    dp, r = B_IN / B_OUT, B_OUT / B_I
    f0 = dp / (2 * math.pi * math.sqrt(L * C))
    pair = [p for p in pz_rows(res.response.poles) if p["q"] and abs(p["f0"] / f0 - 1) < 1e-6]
    assert len(pair) == 1 and pair[0]["q"] == pytest.approx(dp * r * math.sqrt(C / L), rel=1e-6)
    rhp = [z for z in res.response.zeros if z.real > 0]
    assert len(rhp) == 1 and abs(rhp[0]) == pytest.approx(dp * dp * r / L, rel=1e-6)


def test_a_boost_crossing_over_near_its_rhp_zero_is_an_issue(tmp_path):
    """Simulate of the boost with Rc 100k: the crossover (below f_sw/5) above a fifth of the 203 kHz
    right-half-plane zero: a loop_crossover issue naming it; with Rc 10k (~11 kHz) none."""
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    from boardgen import boost_board
    g = boost_board(loop=True)
    g.fields("U8", **{"Control Mode": "peak current", "EA gm": "1.8mmho", "Current Sense Gain": str(K)})
    b, sc, _ = make(tmp_path, g, "bsim", nets=())
    spec = ConverterSpec("boost", "VOUT", "GND", switch_node="SW", high_side=["D1"], low_side=["Q1"],
                         topology="boost", f_sw=F_SW, i_out=B_I, v_in=B_IN, v_out=B_OUT)
    sc.add_detected_converters([spec], {})
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    res = simulate(b, cfg)
    (c,) = res.converters
    assert c.loop["fc"] and not c.loop["error"] and c.loop["fc"] < 0.2 * c.loop["figures"]["f_rhp"]
    assert not any(i.check == "loop_crossover" for i in res.issues)
    b.footprint("RC1").value = "100k"
    high = simulate(b, cfg)
    msgs = [i.message for i in high.issues if i.check == "loop_crossover"]
    assert msgs and "right-half-plane zero" in msgs[0], [i.message for i in high.issues]
