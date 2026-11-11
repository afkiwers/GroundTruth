"""Audit round 9 (docs/VALIDATION_AUDIT.md "Round 9"): the power stages' physics against references made here, not
with GroundTruth's own functions: a sampled waveform's FFT, a time-domain simulation of the switched circuit
(tools/dev/converter_audit.py), closed forms written out.

F9-1 the hot loop's pulse carries the inductor's ripple on its top [A97]; F9-2 the hard edge's overlap at the
current of its instant [A96]; F9-3 a boost with both switches in one IC switches too; F9-4 a side's parallel
switches share its output charge; F9-6 L·di/dt at the ripple's peak. (F9-5, one efficiency for one operating point:
tests/test_efficiency.py.) Each fails on the code before the round.
"""
import math
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from boardgen import boost_board, discrete_buck_board
from conftest import make
from groundtruth.analysis.interleave import trapezoid_phase
from groundtruth.analysis.power import pulse_harmonics, pulse_shape
from groundtruth.analysis.thermal import hard_edge_current, part_losses
from groundtruth.config.settings import ConverterSpec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dev"))
from converter_audit import boost_reference, buck_reference, pulse_fft  # noqa: E402  (the independent references)


def _complex_fft(i_mean, ripple, duty, f_sw, n_max, samples=1 << 18):
    """X_1..X_n of a pulse with sharp edges starting at t = 0, its top a ramp from i − ripple/2 to i + ripple/2."""
    t = (np.arange(samples) + 0.5) / samples / f_sw
    tau = duty / f_sw
    x = np.where(t < tau, i_mean + ripple * (t / tau - 0.5), 0.0)
    return (np.fft.rfft(x) / samples)[1:n_max + 1]


# --------------------------------------------------------------------------------------- F9-1: the pulse
@pytest.mark.parametrize("i, di, duty, f, tr, boost", [
    (10.0, 4.9, 0.509, 500e3, 2e-9, False),          # a buck near D = ½: its even harmonics are the ripple's
    (10.0, 2.0, 0.095, 500e3, 2e-9, False),          # a low duty
    (3.0, 2.0, 1 / 3, 300e3, 5e-9, True),            # a boost's rectifier: the top falls
])
def test_F9_1_the_pulse_with_the_ripple_is_the_sampled_waveform_s(i, di, duty, f, tr, boost):
    """Harmonics 1-40 of the hot loop's pulse against the FFT of the sampled waveform, within 0.01 dB (the flat
    trapezoid: up to 13 dB low near its nulls)."""
    n = np.arange(1, 41)
    ref = pulse_fft(i, di, duty, f, tr, 40, rising=not boost)
    have = pulse_harmonics(n * f, f, i, duty, tr, boost=boost, ripple=di)
    big = ref > 1e-6 * i
    assert np.max(np.abs(20 * np.log10(have[big] / ref[big]))) < 0.01
    flat = pulse_harmonics(n * f, f, i, duty, tr, boost=boost)
    assert np.max(np.abs(20 * np.log10(np.maximum(flat[big], 1e-300) / ref[big]))) > 0.5     # the ripple matters


def test_F9_1_at_half_duty_the_even_harmonics_are_the_ripple_s():
    """At D = ½ the flat pulse has no even harmonic; with the ripple c_n = ΔI/(π·n) there (|X_n| = k/(2πn) per
    ampere, twice for the peak amplitude): what two phases 180° apart leave at 2·f_sw."""
    f, di = 500e3, 4.0
    c = pulse_harmonics(np.array([2, 4, 6]) * f, f, 10.0, 0.5, 0.0, ripple=di)
    assert c == pytest.approx(di / (np.pi * np.array([2, 4, 6])), rel=1e-9)
    assert pulse_harmonics(np.array([2.0]) * f, f, 10.0, 0.5, 0.0)[0] < 1e-12


def test_F9_1_without_a_ripple_nothing_changes():
    """ripple = 0 is the trapezoid as before, and pulse_shape the signed sinc."""
    from groundtruth.emi.spectrum import trapezoid_harmonics
    f = 400e3
    fs, amps = trapezoid_harmonics(f, 5e-9, 3.0, 0.21, 40e6)
    assert pulse_harmonics(fs, f, 3.0, 0.21, 5e-9) == pytest.approx(amps, rel=1e-12)
    n = np.arange(1, 30)
    assert pulse_shape(n, 0.21).imag == pytest.approx(0.0) and pulse_shape(n, 0.21).real == pytest.approx(
        0.21 * np.sinc(n * 0.21))


@pytest.mark.parametrize("duty, k, start", [(0.5, 0.49, 0.0), (0.3, 0.8, 0.0), (1 / 3, -0.6, 2 / 3)])
def test_F9_1_the_phase_an_interleave_group_adds_with(duty, k, start):
    """trapezoid_phase with the ripple against the phase of the sampled pulse's DFT (a pulse starting at start·T)."""
    f, n = 250e3, np.arange(1, 25)
    X = _complex_fft(1.0, k, duty, f, 24) * np.exp(-2j * np.pi * n * start)
    big = np.abs(X) > 1e-4
    assert np.angle(X[big] / trapezoid_phase(n[big], duty, f, 0.0, start, k)) == pytest.approx(0.0, abs=2e-3)


# ------------------------------------------------------------------------------------- the losses' fixtures
@pytest.fixture(scope="module")
def buck(tmp_path_factory):
    """discrete_buck_board and its settings."""
    b, sc, _ = make(tmp_path_factory.mktemp("r9"), discrete_buck_board(), "r9", nets=())
    return b, sc.config


@pytest.fixture(scope="module")
def boost(tmp_path_factory):
    """boost_board and its settings."""
    b, sc, _ = make(tmp_path_factory.mktemp("r9b"), boost_board(), "r9b", nets=())
    return b, sc.config


def _by(board, cfg, spec, duty, v):
    """{ref: {category: W}} of part_losses for one stage alone."""
    rows, _ = part_losses(board, replace(cfg, converters=[spec]),
                          [SimpleNamespace(net=spec.name, duty=duty, v_in=v, dc=[])])
    return {r["ref"]: r["by"] for r in rows}


BUCK = dict(switch_node="SW", f_sw=500e3, i_out=5.0, v_in=48.0, v_out=12.0, rise_time=5e-9, rds_on=5e-3,
            q_oss=40e-9, dead_rise=0.0, dead_fall=0.0)


# ------------------------------------------------------------------------------- F9-2: the hard edge's current
def test_F9_2_a_synchronous_stage_turns_on_at_the_ripple_s_valley(buck):
    """½·V·(I − ΔI/2)·t·f_sw: the main switch turns on where the dead-time model [A48] already has the current."""
    board, cfg = buck
    spec = ConverterSpec("buck", "VIN", "GND", high_side=["Q1"], low_side=["Q2"], **BUCK)
    di = 48.0 * 0.25 * 0.75 / (cfg.model_for("L1").get("L") * 500e3)
    assert 0 < di < 10
    sw = _by(board, cfg, spec, 0.25, 48.0)["Q1"]["switching"]
    assert sw == pytest.approx(0.5 * 48.0 * (5.0 - di / 2) * 5e-9 * 500e3, rel=1e-9)
    # a valley below zero turns the switch on at zero voltage: no overlap, never a negative loss
    light = _by(board, cfg, replace(spec, i_out=di / 4), 0.25, 48.0)["Q1"]
    assert light.get("switching", 0.0) == 0.0


def test_F9_2_with_a_diode_both_edges_valley_and_peak():
    """A diode stage's main switch is hard at both edges: the valley at turn-on, the peak at turn-off, 2·I_L
    together; a negative valley is clamped."""
    diode = ConverterSpec("b", "VIN", "GND", high_side=["Q1"], low_side=["D1"])
    sync = ConverterSpec("b", "VIN", "GND", high_side=["Q1"], low_side=["Q2"])
    assert hard_edge_current(diode, 4.0, 3.0) == pytest.approx(8.0)
    assert hard_edge_current(sync, 4.0, 3.0) == pytest.approx(2.5)
    assert hard_edge_current(sync, 1.0, 3.0) == 0.0
    assert hard_edge_current(diode, 1.0, 3.0) == pytest.approx(2.5)        # only the turn-off, at the peak


# ---------------------------------------------------------------------------------- F9-3: an integrated boost
def test_F9_3_a_boost_in_one_ic_has_its_switching_losses(boost):
    """The same boost drawn as an IC with both switches inside loses what its main switch loses: the overlap at
    the valley and Q_oss·V_out·f_sw (before: none at all)."""
    board, cfg = boost
    kw = dict(switch_node="SW", topology="boost", f_sw=300e3, i_out=1.0, v_in=12.0, v_out=36.0, rise_time=10e-9,
              rds_on=20e-3, q_oss=5e-9)
    by = _by(board, cfg, ConverterSpec("boost", "VOUT", "GND", switch=["Q1"], **kw), 12.0 / 36.0, 36.0)["Q1"]
    rip = 12.0 * (2 / 3) / (cfg.model_for("L1").get("L") * 300e3)
    assert by["switching"] == pytest.approx(0.5 * 36.0 * (3.0 - rip / 2) * 10e-9 * 300e3, rel=1e-9)
    assert by["output charge"] == pytest.approx(5e-9 * 36.0 * 300e3, rel=1e-9)
    assert by["conduction"] == pytest.approx((3.0 ** 2 + rip ** 2 / 12) * 20e-3, rel=1e-9)   # always in the IC


# ------------------------------------------------------------------------------ F9-4: parallel switches' charge
def test_F9_4_parallel_switches_share_the_side_s_output_charge(buck):
    """q_oss is the side's parallel switches together: two parts on the high side lose Q_oss·V·f_sw between them
    (before: each the whole). C1 stands in for the second FET: the loss model asks only that the part exists."""
    board, cfg = buck
    one = _by(board, cfg, ConverterSpec("buck", "VIN", "GND", high_side=["Q1"], low_side=["Q2"], **BUCK), 0.25, 48.0)
    two = _by(board, cfg, ConverterSpec("buck", "VIN", "GND", high_side=["Q1", "C1"], low_side=["Q2"], **BUCK),
              0.25, 48.0)
    want = 40e-9 * 48.0 * 500e3
    assert one["Q1"]["output charge"] == pytest.approx(want)
    assert two["Q1"]["output charge"] == pytest.approx(want / 2) and two["C1"]["output charge"] == pytest.approx(want / 2)
    assert two["Q1"]["switching"] + two["C1"]["switching"] == pytest.approx(one["Q1"]["switching"])


# ------------------------------------------------------------------- the circuit in time against the loss model
def test_the_conduction_dead_time_and_winding_losses_are_the_simulated_circuit_s(buck):
    """A synchronous buck integrated in time (on-state resistances, V_SD in the dead times, the DCR, the on-time
    regulated to the load current) against part_losses' closed forms: the dead times and the winding within 0.1 %,
    the switches' conduction within 1 % (the model takes the ideal duty; the real one is a loss's share longer)."""
    board, cfg = buck
    l_h = cfg.model_for("L1").get("L")
    v_in, v_out, i, f = 55.0, 27.5, 10.0, 500e3
    ref = buck_reference(v_in, v_out, i, f, l_h, 3.1e-3, 4.0e-3, 6.9e-3, 20e-9, 20e-9, 2.0)
    assert abs(ref["balance"]) < 1e-6                                 # the reference conserves energy
    spec = ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"], f_sw=f, i_out=i,
                         v_in=v_in, v_out=v_out, rise_time=5e-9, rds_on=3.1e-3, rds_on_low=4.0e-3, dcr=6.9e-3,
                         dead_rise=20e-9, dead_fall=20e-9, v_sd=2.0)
    by = _by(board, cfg, spec, v_out / v_in, v_in)
    assert by["Q1"]["conduction"] == pytest.approx(ref["hs"], rel=0.01)
    assert by["Q2"]["conduction"] == pytest.approx(ref["ls"], rel=0.01)
    assert by["Q2"]["dead time"] == pytest.approx(ref["dead"], rel=1e-3)
    assert by["L1"]["inductor"] == pytest.approx(ref["dcr"], rel=1e-3)


def test_a_boost_s_closed_forms_are_the_simulated_circuit_s(boost):
    """A synchronous boost in time against the model's rules written out [A48, A80]: I_L = I_out/d, the low side
    (1 − d)(I_L² + ΔI²/12)R, the high side's channel without both dead times, the dead times at valley and peak;
    within 5 % (the ideal duty against the regulated one)."""
    _, cfg = boost
    l_h = cfg.model_for("L1").get("L")
    v_in, v_out, i, f, dead, v_sd = 12.0, 36.0, 1.0, 300e3, 30e-9, 0.8
    ref = boost_reference(v_in, v_out, i, f, l_h, 20e-3, 30e-3, 40e-3, dead, dead, v_sd)
    assert abs(ref["balance"]) < 1e-6
    d = v_in / v_out
    i_l, rip = i / d, v_in * (1 - d) / (l_h * f)
    i_sq = i_l ** 2 + rip ** 2 / 12
    assert i_l == pytest.approx(ref["i_l"], rel=0.03)
    assert (1 - d) * i_sq * 20e-3 == pytest.approx(ref["ls"], rel=0.05)
    assert (d - 2 * dead * f) * i_sq * 30e-3 == pytest.approx(ref["hs"], rel=0.05)
    assert v_sd * f * 2 * i_l * dead == pytest.approx(ref["dead"], rel=0.05)


# ------------------------------------------------------------------------------------ F9-6: L·di/dt at the peak
def test_F9_6_the_overshoot_is_at_the_ripple_s_peak(tmp_path):
    """Simulated: L·(I + ΔI/2)/t_r, the current the high side turns off (before: the mean current's), and the
    stage's spectrum is the pulse with its ripple."""
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    board, sc, _ = make(tmp_path, discrete_buck_board(), "ov", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out, spec.rise_time = 300e3, 3.0, 12.0, 6.0, 5e-9
    sc.set_converters([spec])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    r = simulate(board, cfg).converters[0]
    assert not r.error
    di = 12.0 * 0.5 * 0.5 / (cfg.model_for("L1").get("L") * 300e3)
    assert r.pulse_ripple_a == pytest.approx(di)
    assert r.overshoot_v == pytest.approx(r.loop_nh * 1e-9 * (3.0 + di / 2) / 5e-9, rel=1e-9)
    assert math.isfinite(r.overshoot_v) and r.overshoot_v > r.loop_nh * 1e-9 * 3.0 / 5e-9
