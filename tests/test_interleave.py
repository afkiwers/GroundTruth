"""Interleaved power stages (analysis/interleave.py): the stages' harmonics as phasors, checked against the discrete
Fourier transform of the sampled waveforms, and a group's sum: two equal sources 180° apart cancel at the odd
harmonics and double at the even ones, in phase they double everywhere, end to end through a Simulate."""
import math

import numpy as np
import pytest

from boardgen import hot_loop_board
from groundtruth.analysis.interleave import _add, group_phases, ripple_phase, trapezoid_phase
from groundtruth.analysis.power import ringing_rational, ringing_response, ripple_harmonics
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import ConfigError, ConverterSpec, config_from_data
from groundtruth.emi.spectrum import trapezoid_harmonics
from test_power import SPEC, make_conv

N = 1 << 15


def _dft(x: np.ndarray, n: np.ndarray) -> np.ndarray:
    """The complex Fourier coefficients X_n = (1/T)∫x e^(-j2πnt/T) of one sampled period."""
    t = np.arange(len(x)) / len(x)
    return np.array([np.mean(x * np.exp(-2j * np.pi * k * t)) for k in n])


@pytest.mark.parametrize("duty, start", [(0.3, 0.0), (0.42, 0.25), (0.7, 0.6)])
def test_the_trapezoid_s_phasors_are_its_fourier_coefficients(duty, start):
    """A trapezoid (half height from start·T to (start + D)·T, edges of tr centred there) sampled over one period:
    its DFT has the phase trapezoid_phase gives and 2|X_n| as spectrum.trapezoid_harmonics [Paul 2006]."""
    f, tr = 1e6, 20e-9
    t = np.arange(N) / N
    ramp = tr * f
    u = (t - start + 0.25) % 1.0 - 0.25              # time since the rising edge, in [-T/4, 3T/4): both edges inside
    x = np.clip(u / ramp + 0.5, 0.0, 1.0) - np.clip((u - duty) / ramp + 0.5, 0.0, 1.0)
    n = np.arange(1, 16)
    X = _dft(x, n)
    big = np.abs(X) > 1e-4
    assert np.angle(X[big] / trapezoid_phase(n[big], duty, f, tr, start)) == pytest.approx(0.0, abs=2e-3)
    fs, amps = trapezoid_harmonics(f, tr, 1.0, duty, 16 * f)
    ref = dict(zip(np.rint(fs / f).astype(int), amps))
    assert 2 * np.abs(X[big]) == pytest.approx([ref[k] for k in n[big]], rel=2e-3)


@pytest.mark.parametrize("duty", [0.25, 0.5, 0.8])
def test_the_ripple_s_phasors_are_its_fourier_coefficients(duty):
    """An asymmetric triangle (rising for D·T from t = 0): its DFT's phase is ripple_phase's, 2|X_n| is
    power.ripple_harmonics' (ΔI = 1)."""
    t = np.arange(N) / N
    x = np.where(t < duty, t / duty, (1 - t) / (1 - duty))
    n = np.arange(1, 12)
    X = _dft(x, n)
    big = np.abs(X) > 1e-5
    assert np.angle(X[big] / ripple_phase(n[big], duty)) == pytest.approx(0.0, abs=2e-3)
    fs, amps = ripple_harmonics(1.0, 1.0, duty, 12.0)
    ref = dict(zip(np.rint(fs).astype(int), amps))
    assert 2 * np.abs(X[big]) == pytest.approx([ref[k] for k in n[big]], rel=1e-3)


def test_the_ringing_rational_is_the_complex_response_whose_magnitude_the_stage_takes():
    """ringing_rational at s = jω (what loop_phase takes the phase of) has ringing_response's magnitude."""
    L, r, c = 2e-9, 0.15, 600e-12
    f = np.geomspace(1e6, 2e9, 50)
    num, den = ringing_rational(L, r, c)
    h = np.polyval(num, 2j * np.pi * f) / np.polyval(den, 2j * np.pi * f)
    f0, q = 1 / (2 * math.pi * math.sqrt(L * c)), math.sqrt(L / c) / r
    assert np.abs(h) == pytest.approx(ringing_response(f, f0, q), rel=1e-9)


def test_two_equal_sources_180_degrees_apart_cancel_at_odd_and_double_at_even_harmonics():
    """Two equal phasor records, the second delayed by 180°: harmonic n turns by e^(-jnπ), so the odd ones cancel
    exactly and the even ones double; in phase all double. 120° apart (three sources): only multiples of 3 remain,
    tripled."""
    n = np.arange(1, 13)
    rec = {"n_cond": n, "cond": np.exp(0.3j * n) * (1.0 / n), "n_rad": n,
           "rad": np.outer(1.0 / n, [0.2, -0.1j, 1.0])}
    k, s = _add([(rec, 0.0), (rec, 180.0)], "cond")
    assert np.abs(s[k % 2 == 1]) == pytest.approx(0.0, abs=1e-12)
    assert s[k % 2 == 0] == pytest.approx(2 * rec["cond"][k % 2 == 0])
    k, s = _add([(rec, 0.0), (rec, 0.0)], "rad")
    assert s == pytest.approx(2 * rec["rad"])
    k, s = _add([(rec, 0.0), (rec, 120.0), (rec, 240.0)], "cond")
    assert np.abs(s[k % 3 != 0]) == pytest.approx(0.0, abs=1e-12)
    assert s[k % 3 == 0] == pytest.approx(3 * rec["cond"][k % 3 == 0])


def test_a_group_s_phases_default_to_an_even_spread_and_need_a_group():
    """Without phase_deg the k-th of n stages sits at 360°·k/n; a given phase is kept (mod 360); phase_deg without
    interleave is refused."""
    base = {"rail": "VIN", "ground": "GND", "switch": "U1"}
    cfg = config_from_data({"converters": [dict(base, name="a", interleave="g"), dict(base, name="b", interleave="g"),
                                           dict(base, name="c", interleave="g"),
                                           dict(base, name="d", interleave="h", phase_deg=-90)]})
    assert group_phases(cfg) == {"g": {"a": 0.0, "b": 120.0, "c": 240.0}, "h": {"d": 270.0}}
    with pytest.raises(ConfigError, match="phase_deg needs interleave"):
        config_from_data({"converters": [dict(base, name="a", phase_deg=180)]})


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    """One integrated stage with an input connector, simulated as two identical members of a group: 180° apart and
    in phase (their phasors are equal, so the sums are exact)."""
    out = {}
    for label, phase in (("apart", 180.0), ("together", 0.0)):
        stages = [ConverterSpec(**{**SPEC.__dict__, "name": name, "input_connector": ["J1.1", "J1.2"],
                                   "interleave": "g", "phase_deg": ph})
                  for name, ph in (("A", 0.0), ("B", phase))]
        board, sc, _ = make_conv(tmp_path_factory.mktemp(label), hot_loop_board(connector=True), "il", stages)
        res = simulate(board, sc.config)
        assert all(not r.error for r in res.converters), [r.error for r in res.converters]
        out[label] = res
    return out


def _at(rows, f, key):
    """The row's reading at harmonic frequency f (None when no row is within 1 %)."""
    r = next((r for r in rows if abs(r["f"] / f - 1) < 0.01), None)
    return None if r is None else r[key]


def test_a_group_of_two_equal_stages_180_degrees_apart_cancels_its_fundamental(pair):
    """End to end: the group's conducted reading at f_sw (200 kHz) falls away against the stage's own, at 2·f_sw it
    is the stage's + 6.02 dB; in phase it is + 6.02 dB at both, and the radiated worst too. Every member carries the
    same group figures; the phasor records are released."""
    res = pair["apart"]
    a, b = res.converters
    g = a.interleave
    assert g is b.interleave and g["stages"] == ["A", "B"] and g["phases_deg"] == {"A": 0.0, "B": 180.0}
    assert a.phasors is None and b.phasors is None
    f = SPEC.f_sw
    own1, own2 = _at(a.conducted, f, "v_dbuv"), _at(a.conducted, 2 * f, "v_dbuv")
    assert own1 is not None and own2 is not None
    got1 = _at(g["conducted"], f, "v_dbuv")
    assert got1 is None or got1 < own1 - 60
    assert _at(g["conducted"], 2 * f, "v_dbuv") == pytest.approx(own2 + 20 * math.log10(2), abs=0.01)
    gi = pair["together"].converters[0].interleave
    assert _at(gi["conducted"], f, "v_dbuv") == pytest.approx(own1 + 20 * math.log10(2), abs=0.01)
    assert gi["worst"]["e_dbuv"] == pytest.approx(a.worst["e_dbuv"] + 20 * math.log10(2), abs=0.05)
    assert g["conducted_in_phase_worst"]["margin_db"] == pytest.approx(gi["conducted_worst"]["margin_db"], abs=1e-6)
    assert "phasors" not in a.summary() and a.summary()["interleave"]["group"] == "g"


def test_detection_groups_the_stages_one_controller_switches(tmp_path):
    """A discrete stage's controller is the IC on its switch node (discrete_buck_board(loop=True): U8, like the
    LTC7890); two stages switched by one IC become a group named after it, 0° and 180° (in name order)."""
    from boardgen import discrete_buck_board
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.detect import DetectedConverter, _interleave_proposals, _stage_controller
    board = load_board(discrete_buck_board(loop=True).write(tmp_path / "loop.kicad_pcb"))
    by_net = board.parts_by_net()
    spec = ConverterSpec("SW1", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"])
    assert _stage_controller(board, spec, by_net) == "U8"
    found = [DetectedConverter(ConverterSpec(name, "VIN", "GND", switch_node="SW", high_side=["Q1"],
                                             low_side=["Q2"]), "high") for name in ("b", "a")]
    _interleave_proposals(board, found, by_net)
    assert {d.spec.name: (d.spec.interleave, d.spec.phase_deg) for d in found} == \
        {"a": ("U8 phases", 0.0), "b": ("U8 phases", 180.0)}
    assert "datasheet" in found[0].reasons[-1]


def test_a_slow_pair_180_degrees_apart_cancels_its_ce101_fundamental(tmp_path):
    """CE101 (MIL-STD-461, aircraft) of two identical stages switching at 2 kHz, 180° apart: the input lead's
    current at f_sw cancels and at 2·f_sw reads the stage's own + 6.02 dB (each 2 kHz harmonic alone in the probe's
    100 Hz bandwidth); the group says so in its text and keeps each stage's own margin beside it."""
    from groundtruth.analysis.interleave import interleave_text
    stages = [ConverterSpec(**{**SPEC.__dict__, "name": name, "f_sw": 2e3, "input_connector": ["J1.1", "J1.2"],
                               "interleave": "slow", "phase_deg": ph}) for name, ph in (("A", 0.0), ("B", 180.0))]
    board, sc, _ = make_conv(tmp_path, hot_loop_board(20.0, caps=(("C1", "470u", "end", 0.0),), connector=True), "ce",
                             stages, conducted="mil461", ce101="aircraft")
    res = simulate(board, sc.config)
    a = res.converters[0]
    g = a.interleave
    assert a.ce101 and g["ce101"] and g["ce101_limit"].startswith("MIL-STD-461G CE101")
    own1, own2 = _at(a.ce101, 2e3, "i_dbua"), _at(a.ce101, 4e3, "i_dbua")
    assert own1 is not None and own2 is not None
    got1 = _at(g["ce101"], 2e3, "i_dbua")
    assert got1 is None or got1 < own1 - 60
    assert _at(g["ce101"], 4e3, "i_dbua") == pytest.approx(own2 + 20 * math.log10(2), abs=0.01)
    assert g["ce101_in_phase_worst"]["margin_db"] < g["ce101_worst"]["margin_db"]
    assert any(t.startswith("CE101 margin together") for t in interleave_text(g))


def test_a_boost_pair_180_degrees_apart_cancels_its_ripple_fundamental(tmp_path):
    """Two identical boosts (tests/boardgen.boost_board, 5 V to 12 V at 300 kHz) 180° apart: their conducted estimate
    is the inductor's ripple triangle (ripple_phase), so at f_sw the input's reading cancels and at 2·f_sw it is the
    stage's own + 6.02 dB; in phase the radiated worst is + 6.02 dB."""
    from boardgen import boost_board
    from conftest import make
    from dataclasses import replace
    from groundtruth.analysis.detect import detect_converters
    board, sc, _ = make(tmp_path, boost_board(), "boost", nets=())
    base = detect_converters(board, sc.config)[0].spec
    base.f_sw, base.v_in, base.v_out, base.i_out = 300e3, 5.0, 12.0, 0.5
    got = {}
    for label, ph in (("apart", 180.0), ("together", 0.0)):
        stages = [replace(base, name=n, interleave="b", phase_deg=p) for n, p in (("A", 0.0), ("B", ph))]
        (tmp_path / label).mkdir()
        b2, sc2, _ = make_conv(tmp_path / label, boost_board(), "boost", stages)
        res = simulate(b2, sc2.config)
        assert all(not r.error for r in res.converters), [r.error for r in res.converters]
        got[label] = res.converters[0]
    a = got["apart"]
    assert a.topology == "boost" and a.ripple
    g = a.interleave
    f = 300e3
    own1, own2 = _at(a.conducted, f, "v_dbuv"), _at(a.conducted, 2 * f, "v_dbuv")
    got1 = _at(g["conducted"], f, "v_dbuv")
    assert got1 is None or got1 < own1 - 60
    assert _at(g["conducted"], 2 * f, "v_dbuv") == pytest.approx(own2 + 20 * math.log10(2), abs=0.01)
    gi = got["together"].interleave
    assert gi["worst"]["e_dbuv"] == pytest.approx(got["together"].worst["e_dbuv"] + 20 * math.log10(2), abs=0.05)
