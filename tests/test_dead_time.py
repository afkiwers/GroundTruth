"""A synchronous stage's dead time and its rectifier's reverse recovery against closed forms (the user, of the
converters' edge time: "why aren't we looking at dead time as well?", then "do all of it", and "add a GT field ... to
the IC on the schematic for each converter that contains the intended dead time").

Losses [A48, A49]: the rectifier conducts in reverse for 2·t_dead per period at V_SD (2·t_dead·f_sw·I·V_SD), its
channel the rest of its share ((1 − D − 2·t_dead·f_sw)·I²·R); the main switch sweeps out Q_rr at V (Q_rr·V·f_sw).
Recovery [Mohan 2003]: I_rr = √(2·Q_rr·di/dt), t_rr = √(2·Q_rr/(di/dt)), a triangle of charge Q_rr; its spectrum with
the trapezoid against a direct numerical Fourier transform of the time waveform; the overshoot I_rr·√(L/C_oss).
The controller's GT field (dead:20ns, dead1:20ns, conv1_dead:20ns) and the FETs' Vsd / Qrr fields."""
import math
from types import SimpleNamespace

import numpy as np
import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.analysis import tags
from groundtruth.analysis.power import pulse_harmonics, recovery, recovery_overshoot
from groundtruth.analysis.thermal import DEFAULT_DEAD_TIME, V_SD_SI, part_losses
from groundtruth.components.fields import stage_values
from groundtruth.components.values import parse_quantity
from groundtruth.config.settings import ConverterSpec, SettingsFile

F_SW, V_IN, V_OUT, I_OUT, R_ON = 500e3, 55.0, 27.5, 10.0, 3.1e-3


def _spec(**kw) -> ConverterSpec:
    """The test buck (55 V → 27.5 V, D = ½, 10 A at 500 kHz, 3.1 mΩ FETs) on discrete_buck_board's parts."""
    return ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"],
                         **{"f_sw": F_SW, "i_out": I_OUT, "v_in": V_IN, "v_out": V_OUT, "rise_time": 5e-9,
                            "rds_on": R_ON, **kw})


def _losses(board, cfg, spec) -> tuple[dict, list]:
    """{ref: W} and the notes of part_losses for one stage alone (its result: D = ½, no DC paths)."""
    from dataclasses import replace
    one = replace(cfg, converters=[spec])
    rows, notes = part_losses(board, one, [SimpleNamespace(net=spec.name, duty=V_OUT / V_IN, v_in=V_IN, dc=[])])
    return {r["ref"]: r["power_w"] for r in rows}, notes


@pytest.fixture
def board(tmp_path):
    """discrete_buck_board with its controller U8 (SW1 on the switch node); the settings."""
    b, sc, _ = make(tmp_path, discrete_buck_board(loop=True), "dt", nets=())
    return b, sc.config


def test_dead_time_loss_is_the_closed_form(board):
    """Q2 (the rectifier): 2·t·f·I·V_SD in the dead times plus (1 − D − 2·t·f)·I²·R in its channel; without a
    dead time the old (1 − D)·I²·R; Q1 unchanged."""
    b, cfg = board
    t, vsd = 20e-9, 1.6
    with_dt, notes = _losses(b, cfg, _spec(dead_time=t, v_sd=vsd))
    dead = 2 * t * F_SW
    assert with_dt["Q2"] == pytest.approx(dead * I_OUT * vsd + (0.5 - dead) * I_OUT ** 2 * R_ON, rel=1e-12)
    # 2 × 20 ns at 500 kHz is 2 % of the period: 0.02 × 10 A × 1.6 V = 0.32 W, less 2 % of the channel's I²R
    assert with_dt["Q2"] - 0.5 * I_OUT ** 2 * R_ON == pytest.approx(0.32 - dead * I_OUT ** 2 * R_ON, rel=1e-12)
    none, _ = _losses(b, cfg, _spec(dead_time=0.0, v_sd=vsd))
    assert none["Q2"] == pytest.approx(0.5 * I_OUT ** 2 * R_ON, rel=1e-12)
    assert none["Q1"] == pytest.approx(with_dt["Q1"], rel=1e-12)
    assert not [n for n in notes if "dead" in n or "v_sd" in n]


def test_capacity_scales_it_linearly(board):
    """Current capacity's loss quadratic a·s² + b·s + c: the dead-time loss is linear in the load scale s (b), the
    channel's I²R quadratic (a)."""
    from groundtruth.analysis.thermal import capacity_inputs
    from dataclasses import replace
    b, cfg = board
    spec = _spec(dead_time=20e-9, v_sd=1.6)
    stage = SimpleNamespace(kind="converter", net=spec.name, view_of=None, error=None, duty=V_OUT / V_IN, v_in=V_IN,
                            dc=[], dc_vectors={}, dc_grids={})
    a, lin, c = capacity_inputs(b, replace(cfg, converters=[spec]), [stage])["loss_terms"][spec.name]["Q2"]
    dead = 2 * 20e-9 * F_SW
    assert lin == pytest.approx(dead * I_OUT * 1.6) and a == pytest.approx((0.5 - dead) * I_OUT ** 2 * R_ON)
    assert c == pytest.approx(0.0, abs=1e-12)


def test_defaults_are_said(board):
    """No dead_time: DEFAULT_DEAD_TIME; no v_sd: a silicon body diode's 0.8 V (SiR500 is no GaN part); both noted."""
    b, cfg = board
    got, notes = _losses(b, cfg, _spec())
    dead = 2 * DEFAULT_DEAD_TIME * F_SW
    assert got["Q2"] == pytest.approx(dead * I_OUT * V_SD_SI + (0.5 - dead) * I_OUT ** 2 * R_ON, rel=1e-12)
    assert any("no dead_time" in n and "dead:20ns" in n for n in notes)
    assert any("no v_sd" in n and "silicon" in n for n in notes)
    _, quiet = _losses(b, cfg, _spec(dead_time=0.0))         # no dead time: V_SD is not used, nothing to say
    assert not [n for n in quiet if "v_sd" in n or "dead" in n]


def test_recovery_loss_needs_the_body_diode_to_conduct(board):
    """Q_rr·V·f_sw in the main switch (Q1) when the dead time lets the body diode conduct; none with dead_time 0."""
    b, cfg = board
    q = 60e-9
    on, _ = _losses(b, cfg, _spec(dead_time=20e-9, v_sd=0.8, q_rr=q))
    off, _ = _losses(b, cfg, _spec(dead_time=20e-9, v_sd=0.8))
    assert on["Q1"] - off["Q1"] == pytest.approx(q * V_IN * F_SW, rel=1e-12)
    zero, _ = _losses(b, cfg, _spec(dead_time=0.0, v_sd=0.8, q_rr=q))
    assert zero["Q1"] == pytest.approx(_losses(b, cfg, _spec(dead_time=0.0, v_sd=0.8))[0]["Q1"], rel=1e-12)


def test_recovery_peak_and_duration():
    """I_rr = √(2·Q_rr·di/dt), t_rr = √(2·Q_rr/(di/dt)), the triangle's area ½·I_rr·t_rr = Q_rr; none without q_rr
    or for a synchronous FET with dead_time 0; the overshoot I_rr·√(L/C_oss)."""
    q, tr = 60e-9, 5e-9
    rec = recovery(_spec(q_rr=q), I_OUT)
    di_dt = I_OUT / tr
    assert rec["i_rr"] == pytest.approx(math.sqrt(2 * q * di_dt)) and rec["t_rr"] == pytest.approx(math.sqrt(2 * q / di_dt))
    assert 0.5 * rec["i_rr"] * rec["t_rr"] == pytest.approx(q)
    assert recovery(_spec(), I_OUT) is None and recovery(_spec(q_rr=q, dead_time=0.0), I_OUT) is None
    L, c = 1.2e-9, 600e-12
    assert recovery_overshoot(rec, L, c)["overshoot_v"] == pytest.approx(rec["i_rr"] * math.sqrt(L / c))
    assert "overshoot_v" not in recovery_overshoot(rec, L, None)


def _numeric_harmonics(f_sw, amp, duty, tr, rec, boost, n_max, samples=2 ** 20):
    """|c_n| = 2·|X_n| of the pulse sampled in time (trapezoid from t = 0, 50 % points at tr/2 and D·T + tr/2, plus
    the recovery's triangle after the turn-on edge, or below zero after a boost's falling edge), by FFT."""
    T = 1 / f_sw
    t = np.arange(samples) * (T / samples)
    x = np.clip(t / tr, 0, 1) - np.clip((t - duty * T) / tr, 0, 1)
    x = amp * x
    if rec is not None:
        start = (duty * T if boost else 0.0) + tr
        half = rec["t_rr"] / 2
        tri = rec["i_rr"] * np.clip(1 - np.abs(t - start - half) / half, 0, None)
        x = x + (-tri if boost else tri)
    X = np.fft.rfft(x) / samples
    return 2 * np.abs(X[1:n_max + 1])


@pytest.mark.parametrize("boost", [False, True])
def test_recovery_spectrum_against_a_numerical_transform(boost):
    """pulse_harmonics with the recovery equals the FFT of the time waveform to 0.1 % of the fundamental at every
    harmonic up to 200 MHz (the triangle in phase with the trapezoid, a boost's below zero after the falling
    edge); without it, the trapezoid alone."""
    q, tr, duty = 60e-9, 5e-9, 0.3
    spec = _spec(q_rr=q, rise_time=tr)
    rec = recovery(spec, I_OUT)
    n = np.arange(1, 401)
    ours = pulse_harmonics(n * F_SW, F_SW, I_OUT, duty, tr, rec, boost=boost)
    ref = _numeric_harmonics(F_SW, I_OUT, duty, tr, rec, boost, len(n))
    assert np.max(np.abs(ours - ref)) < 1e-3 * ref[0]
    plain = pulse_harmonics(n * F_SW, F_SW, I_OUT, duty, tr)
    assert np.max(np.abs(plain - _numeric_harmonics(F_SW, I_OUT, duty, tr, None, boost, len(n)))) < 1e-3 * ref[0]
    # the spike lifts the high harmonics: at 100 MHz (n = 200) more than the trapezoid alone
    assert ours[199] > 1.5 * plain[199]


def test_time_values_read_with_their_unit():
    """A time from a field: 20ns, 0.02us, "20 nanoseconds", a bare 20 in ns, 1s as one second; a charge is no
    time."""
    for text, want in (("20ns", 20e-9), ("0.02us", 20e-9), ("20 nanoseconds", 20e-9), ("20", 20e-9), ("1s", 1.0)):
        assert parse_quantity(text, "s", 1e-9)[0] == pytest.approx(want)
    assert parse_quantity("20nC", "s", 1e-9)[0] is None


def test_gt_dead_time_words():
    """dead:, dead<channel>: and conv<id>_dead: in a GT field; a converter's own word first, then its channel's,
    then the plain one; unreadable values are problems, not labels."""
    fp = SimpleNamespace(fields={"GT": "dead:50n, dead1:20ns, conv2_dead:35 ns"})
    assert tags.dead_time_of(fp, "2", "2") == (pytest.approx(35e-9), "conv2_dead:35 ns")
    assert tags.dead_time_of(fp, "", "1") == (pytest.approx(20e-9), "dead1:20ns")
    assert tags.dead_time_of(fp, "", "3") == (pytest.approx(50e-9), "dead:50n")
    assert tags.dead_time_of(SimpleNamespace(fields={"GT": "dead2:20n"}), "", "1") is None
    found, ignore, problems = tags.parse("dead:20ns, conv1_dead:2x, dead2:-1n", "U9")
    assert found == [] and not ignore and len(problems) == 2


def test_fields_give_the_stage_its_values(tmp_path):
    """The controller U8's GT dead time reaches the stage on its SW1 pin (dead1:), the low-side FET's Vsd and Qrr
    its v_sd and q_rr; the high side's fields are not the rectifier's."""
    g = discrete_buck_board(loop=True)
    g.fields("U8", GT="dead1:25ns, dead2:40ns")
    g.fields("Q2", Vsd="0.9V", Qrr="62 nanocoulomb")
    g.fields("Q1", Vsd="5V")
    b, sc, _ = make(tmp_path, g, "fields", nets=())
    values, notes = stage_values(b, sc.config, _spec())
    assert values["dead_time"].value == pytest.approx(25e-9) and values["dead_time"].source == "U8 GT dead1:25ns"
    assert values["v_sd"].value == pytest.approx(0.9) and values["q_rr"].value == pytest.approx(62e-9)
    assert not notes


def test_settings_round_trip(tmp_path):
    """dead_time, v_sd and q_rr are written and read back; a dead time of half the period is refused."""
    from groundtruth.config.settings import ConfigError
    b, sc, _ = make(tmp_path, discrete_buck_board(loop=True), "rt", nets=())
    sc.set_converters([_spec(dead_time=18e-9, v_sd=1.6, q_rr=40e-9)])
    sc.save()
    back = SettingsFile.load(sc.path).config.converters[0]
    assert (back.dead_time, back.v_sd, back.q_rr) == (pytest.approx(18e-9), pytest.approx(1.6), pytest.approx(40e-9))
    with pytest.raises(ConfigError, match="dead_time"):    # 2 × 500 ns at 500 kHz: half the period
        sc.set_converters([_spec(dead_time=500e-9)])
        sc.save()
        SettingsFile.load(sc.path)
