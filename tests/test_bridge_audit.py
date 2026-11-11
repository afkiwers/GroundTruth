"""Audit round 10 (docs/VALIDATION_AUDIT.md "Round 10"): the motor bridges' physics against a reference that
shares no code with analysis/motor.py: a time-stepping simulation of the three-phase R-L-EMF circuit with ideal
switches and reverse-conducting clamps (tools/dev/bridge_audit.ref_drive), and closed forms.

F10-1 the bus capacitors' ripple is their share of the bus current's AC part, not the legs' AC RMS added in power
[A99]; F10-2 every switching instant is seen (a PWM period's end and the next one's start are one time); F10-3 a
low-side shunt carries the chopped part of its leg's current too (tests/test_bridge.py, on a simulated board). Each
fails on the code before the round.
"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from groundtruth.analysis.bridge import caps_at
from groundtruth.analysis.motor import drive_params, six_step
from groundtruth.config.settings import BridgeSpec, LegSpec, MotorSpec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dev"))
from bridge_audit import ref_drive  # noqa: E402  (the independent reference)

ALL = [{"name": "C", "legs": {"A": 1.0, "B": 1.0, "C": 1.0}}]


def _spec(**kw) -> BridgeSpec:
    """48 V, 20 kHz, 50 A at 3000 rpm into a 20 mΩ / 40 µH / 5 V/krpm / 7 pole-pair star motor, 2 mΩ switches."""
    motor = kw.pop("motor", MotorSpec("star", 0.02, 40e-6, 5.0, 7))
    b = BridgeSpec("b", "V", "GND", legs=[LegSpec(ph, f"PH{ph}", ["Q1"], ["Q2"]) for ph in "ABC"], v_bus=48.0,
                   f_pwm=20e3, i_phase=50.0, speed_rpm=3000.0, dead_time=500e-9, v_sd=2.0, rds_on=2e-3, motor=motor)
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _ref(b, w):
    """The time-stepped reference at the duty the drive model found."""
    p, _ = drive_params(b)
    return ref_drive(p.v, p.f_pwm, w.duty, p.dead, p.v_sd, p.r, p.l, p.e_ll, w.n // 6 if w.f_e > 0 else 0, p.emf,
                     p.pwm, p.synchronous)


# ----------------------------------------------------------------------------------- the drive, independently
@pytest.mark.parametrize("kw", [{}, {"pwm": "hon_lpwm"}, {"mode": "generating"},
                                {"i_phase": 5.0, "synchronous": False}],
                         ids=["hpwm_lon", "hon_lpwm", "generating", "light load, not synchronous"])
def test_the_drive_s_currents_are_the_time_stepped_circuit_s(kw):
    """At the same duty the event-driven drive and a plain time-stepping simulation started from rest give the same
    bus, phase and switch currents within 1 % (the reference conserves energy: bus = EMF + copper + clamps), the
    turn-ons' V·I and the nodes' swings within 3 %."""
    b = _spec(**kw)
    w = six_step(b)
    ref = _ref(b, w)
    assert abs(ref["balance"]) < 5e-3 * max(abs(48.0 * ref["bus_mean"]), 1.0)
    s = w.stats()
    assert s["bus_mean"] == pytest.approx(ref["bus_mean"], rel=0.01)
    assert s["bus_ac_rms"] == pytest.approx(ref["bus_ac_rms"], rel=0.01)
    assert s["phase_rms"][0] == pytest.approx(math.sqrt(ref["i2"][0]), rel=0.01)
    assert s["hs_rms"][0] == pytest.approx(math.sqrt(ref["hs2"][0]), rel=0.01)
    assert s["ls_rms"][0] == pytest.approx(math.sqrt(ref["ls2"][0]), rel=0.01)
    c, sw = w.conduction(), w.switching()
    assert c["hs_sq"].sum() == pytest.approx(ref["hs_ch2"].sum(), rel=0.02)
    assert c["ls_sq"].sum() == pytest.approx(ref["ls_ch2"].sum(), rel=0.02)
    assert c["hs_rev"].sum() + c["ls_rev"].sum() == pytest.approx(ref["hs_rev"].sum() + ref["ls_rev"].sum(), rel=0.02)
    assert sw["hs_vi"].sum() + sw["ls_vi"].sum() == pytest.approx(ref["hs_vi"].sum() + ref["ls_vi"].sum(), rel=0.03)
    assert sw["node_dv2"].sum() == pytest.approx(ref["node_dv2"].sum(), rel=0.03)


# ----------------------------------------------------------------------------- F10-2: every switching instant
@pytest.mark.parametrize("kw", [{"dead_time": 0.0}, {}, {"mode": "generating"}], ids=["no dead time", "500 ns", "gen"])
def test_F10_2_the_three_legs_switch_alike_over_an_electrical_period(kw):
    """Six-step repeats every 120° with the legs rotated, so over a period every leg has the same turn-ons and
    swings. Before, a PWM period's last breakpoint (m·T + T) and the next one's first ((m + 1)·T) differed by a
    rounding step at 17 of 60 boundaries and were not taken as one instant: with no dead time the hard turn-ons
    there were lost (10.8 / 7.8 / 8.7e6 V·A/s per leg), with one the swings into the dead time."""
    w = six_step(_spec(**kw))
    d = np.diff(w.t)
    assert not ((d != 0) & (np.abs(d) < 1e-12)).any() and (d >= 0).all()      # an instant is one time, exactly
    sw = w.switching()
    for key in ("hs_vi", "ls_vi", "node_dv2", "hs_dv2"):
        if sw[key].max() > 0:
            assert sw[key] == pytest.approx([sw[key][0]] * 3, rel=1e-6), key
    assert w.edges == pytest.approx([w.edges[0]] * 3, rel=1e-6)


# ------------------------------------------------------------------------- F10-1: the capacitors' ripple
@pytest.mark.parametrize("kw", [{}, {"pwm": "hon_lpwm"}, {"pwm": "bipolar"}, {"mode": "generating"}],
                         ids=["hpwm_lon", "hon_lpwm", "bipolar", "generating"])
def test_F10_1_capacitors_sharing_every_leg_alike_carry_the_bus_s_ac_current(kw):
    """One bank on one bus: a capacitor taking the share s of every leg's switching current carries s times the bus
    current's AC part (exactly: the high sides' covariance summed), which the reference's bus current confirms.
    Before: the legs' AC RMS added in power, 1.2 (H-PWM / L-ON) to 2.6 times (H-ON / L-PWM) that."""
    b = _spec(**kw)
    w = six_step(b)
    bus_ac = w.stats()["bus_ac_rms"]
    assert caps_at(b, ALL, w)[0]["rms_a"] == pytest.approx(bus_ac, rel=1e-6)
    assert caps_at(b, [{"name": "C", "legs": {"A": 0.3, "B": 0.3, "C": 0.3}}], w)[0]["rms_a"] == pytest.approx(
        0.3 * bus_ac, rel=1e-6)
    assert float(w.hs_cov().sum()) == pytest.approx(bus_ac ** 2, rel=1e-9)
    assert bus_ac == pytest.approx(_ref(b, w)["bus_ac_rms"], rel=0.01)
    in_power = math.sqrt(sum(x * x for x in w.hs_ac_rms()))                  # what it was
    assert in_power > 1.05 * bus_ac


def test_F10_1_a_chopped_constant_current_is_the_closed_form():
    """Locked rotor, a large inductance: the bus carries I for D·T, so a capacitor taking all of it carries
    I·√(D(1 − D)) (no slow part: the period is one PWM period)."""
    b = _spec(speed_rpm=0.0, dead_time=0.0, rds_on=None, motor=MotorSpec("star", 0.02, 0.1, 5.0, 7))
    w = six_step(b)
    assert caps_at(b, ALL, w)[0]["rms_a"] == pytest.approx(50.0 * math.sqrt(w.duty * (1 - w.duty)), rel=0.01)


def test_F10_1_a_leg_s_own_capacitor_carries_its_pwm_part_not_its_envelope():
    """A capacitor that takes one leg's switching current only (its share 1 of leg A, none of B and C) carries that
    leg's current within the PWM periods (the reference: the variance within each period) and a third of a share
    of the bus's slow part; the leg's 120° conduction envelope, a current at the electrical frequency, is not a
    ripple the capacitor's share at f_pwm describes (it is the larger part of the leg's AC RMS)."""
    b = _spec()
    w = six_step(b)
    ref = _ref(b, w)
    got = caps_at(b, [{"name": "C", "legs": {"A": 1.0, "B": 0.0, "C": 0.0}}], w)[0]["rms_a"]
    want = math.sqrt(ref["hs_fast"][0, 0] + ref["hs_slow"].sum() / 9.0)
    assert got == pytest.approx(want, rel=0.03)
    assert w.hs_ac_rms()[0] > 1.1 * got
