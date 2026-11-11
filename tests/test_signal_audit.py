"""Audit round 11 (docs/VALIDATION_AUDIT.md "Round 11"): the signal paths' physics, for what the earlier rounds left
open, against references made in tools/dev/signal_audit.py (a travelling-wave simulation of the line in time, the
envelope of the harmonics' sum, the loop's field with exact constants).

F11-1 a thinned choice of harmonics (a path through a logic buffer [A76]) reads what all the harmonics within the
receiver's bandwidth add up to [A12]; F11-2 the floor's reflection [A7] is CISPR 32's below 1 GHz only. Each fails
on the code before the round. Verified as shipped: the path's circuit against the time-domain line.
"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from groundtruth.analysis.simulate import simulate, thin_harmonics
from groundtruth.emi.path import PathLine, PathLoad, PathSource, path_currents
from groundtruth.emi import radiation
from groundtruth.emi.radiation import (RE102_LIMITS, emission_arrays, floor_absorbed, loop_field, receiver_sum,
                                       window_weight)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dev"))
from signal_audit import (C0, MU0, edge_harmonics, envelope_peak, line_reference, trace_far_field,  # noqa: E402
                          trapezoid_samples)


# ------------------------------------------------------------------------ the path's circuit, in time
@pytest.mark.parametrize("z0, length, r_s, c_load, f_target", [(50.0, 0.100, 22.0, 8e-12, 50e6),
                                                               (65.0, 0.040, 10.0, 15e-12, 100e6)])
def test_the_path_circuit_is_the_travelling_wave_simulation_s(z0, length, r_s, c_load, f_target):
    """A trapezoid behind a resistance, a lossless line and a capacitive load stepped in time (travelling waves,
    unit-delay sections) and Fourier-transformed: the mean current along the line, the current into it and the end
    voltage of harmonics 1-15, as complex values, within 0.2 % of emi/path's (a frequency-domain cascade)."""
    eeff, sections = 3.3, 60
    dt = length * math.sqrt(eeff) / C0 / sections
    n = int(round(1.0 / (f_target * dt)))
    f0 = 1.0 / (n * dt)
    rise = max(int(round(0.6e-9 / dt)), 4)
    ref = line_reference(z0, sections, n, trapezoid_samples(n, 0.5, rise, rise, 3.3), r_s, c_load, dt, periods=40)
    got = path_currents(np.arange(1, 16) * f0, ref["v_src"][:15], [PathLine(z0, eeff, length)], PathSource(r_s),
                        PathLoad(c_load))
    for key in ("i_mean", "i_in", "v_end"):
        want, have = ref[key][:15], getattr(got, key)
        big = np.abs(want) > 0.02 * np.abs(want).max()
        assert np.max(np.abs(have[big] - want[big]) / np.abs(want[big])) < 2e-3, key


# ----------------------------------------------------------------------------------- the loop's field
def test_the_loop_s_field_with_exact_constants():
    """η0·k²·m/(4π·r)·|1 + 1/(jkr)| with η0 = μ0·c: the code's 1.316e-14 (c = 3e8) is 0.07 % low."""
    for f, r in ((100e6, 10.0), (30e6, 1.0), (2e6, 1.0)):
        k = 2 * math.pi * f / C0
        want = MU0 * C0 * k * k * 1e-6 / (4 * math.pi * r) * abs(1 + 1 / (1j * k * r))
        assert float(loop_field(f, 1e-6, r, False)) == pytest.approx(want, rel=2e-3)


def test_F11_2_cispr_32_has_a_reflecting_floor_below_1_ghz_only():
    """Up to 1 GHz the site's floor reflects (the worst case of the height scan: twice the field [A7]); above it
    CISPR 32 measures over absorber (free-space conditions): the free-space field (before: +6 dB there too). A
    user limit (RE102, a bench over a ground plane) keeps the reflection; switched off, there is none anywhere."""
    def e_free(f, r):
        k = 2 * math.pi * f / C0
        return MU0 * C0 * k * k * 1e-6 / (4 * math.pi * r) * abs(1 + 1 / (1j * k * r))
    for f, cls, floor in ((500e6, "B", 2.0), (1e9, "B", 2.0), (1.5e9, "B", 1.0), (4e9, "A", 1.0),
                          (1.5e9, RE102_LIMITS["re102_ground_army"], 2.0)):
        a = emission_arrays([f], [1.0], [1e-6], cls)
        want = 20 * math.log10(floor * e_free(f, float(a["distance_m"][0])) / math.sqrt(2) / 1e-6)
        assert float(a["e_dbuv"][0]) == pytest.approx(want, abs=0.05), (f, cls)
    off = emission_arrays([500e6, 1.5e9], [1.0, 1.0], [1e-6, 1e-6], "B", ground_reflection=False)
    on = emission_arrays([500e6, 1.5e9], [1.0, 1.0], [1e-6, 1e-6], "B")
    assert on["e_dbuv"] - off["e_dbuv"] == pytest.approx([6.02, 0.0], abs=0.01)


# -------------------------------------------------------------------------------------- the receiver
def test_the_in_phase_sum_is_the_envelope_s_peak_at_half_duty_and_an_upper_bound_otherwise():
    """[A12] against the envelope of the harmonics' sum in time (a rectangular filter of the bandwidth): a 1 kHz
    trapezoid with 2.5 ns edges in 120 kHz at 100 MHz. At 50 % duty the sum of the magnitudes is the peak; at 30 %
    the two edges of a period beat (|sin(nπD)|) and the sum is 2 dB over it, never under."""
    f0, tr = 1e3, 2.5e-9
    n = np.arange(100_000 - 60, 100_000 + 61)
    for duty, lo, hi in ((0.5, -0.01, 0.01), (0.3, 1.5, 2.5)):
        x = edge_harmonics(n, f0, duty, tr, 5.0)
        live = np.abs(x) > 1e-12 * np.abs(x).max()
        peak = envelope_peak(x[live], n[live].astype(float))
        over = 20 * math.log10(float(receiver_sum(n * f0, 2 * np.abs(x))[60]) / peak)
        assert lo <= over <= hi, (duty, over)


@pytest.mark.parametrize("duty", [0.5, 0.3, 0.1])
def test_F11_1_thinned_harmonics_read_what_all_of_them_do(duty):
    """A 1 kHz signal with a 2.5 ns edge has 1.3 million harmonics to its band's top; a path through a logic buffer
    evaluates 4000 log-spaced ones [A76]. The reading from them (each weighed by its bandwidth's harmonics,
    window_weight) is the reading from all of them within 0.5 dB, near 200 MHz and at the highest from 30 MHz.
    Before: one harmonic per bandwidth, 36 dB low at 50 % duty (60 odd harmonics in 120 kHz)."""
    f0, tr = 1e3, 2.5e-9
    n_max = int(1.3e9 // f0)
    n_all = np.arange(1, n_max + 1)
    c_all = 2 * np.abs(edge_harmonics(n_all, f0, duty, tr, 5.0)) * 1e-9
    live = c_all > 1e-12 * c_all.max()
    full = emission_arrays(n_all[live] * f0, np.ones(int(live.sum())), c_all[live], "B")["e_dbuv"]
    f_all = n_all[live] * f0
    thin = thin_harmonics(n_max, duty, 4000)
    assert len(thin) <= 4000 and np.abs(np.sin(np.pi * duty * thin[thin > 50])).min() > 0.9      # on the envelope
    c_thin = 2 * np.abs(edge_harmonics(thin, f0, duty, tr, 5.0)) * 1e-9
    got = emission_arrays(thin * f0, np.ones(len(thin)), c_thin, "B", harmonic_of=(f0, duty))["e_dbuv"]
    alone = emission_arrays(thin * f0, np.ones(len(thin)), c_thin, "B")["e_dbuv"]                # as it was
    assert got[thin * f0 >= 30e6].max() == pytest.approx(full[f_all >= 30e6].max(), abs=0.5)
    k = int(np.argmin(np.abs(thin * f0 - 200e6)))
    near = np.abs(f_all - thin[k] * f0) < 300e3
    assert got[k] == pytest.approx(full[near].max(), abs=0.5)
    assert got[k] - alone[k] > 30.0


def test_F11_1_the_weight_is_one_where_harmonics_are_further_apart_than_the_bandwidth():
    """A 10 MHz clock: one harmonic per 120 kHz, nothing to add; a 20 kHz signal at 50 % duty: its three odd
    harmonics within ±60 kHz (n, n ± 2)."""
    assert window_weight(np.array([30e6, 500e6, 2e9]), (10e6, 0.5)) == pytest.approx([1.0, 1.0, 1.0])
    assert window_weight(np.array([20e3 * 5001]), (20e3, 0.5)) == pytest.approx([3.0])


def test_F11_2_the_refinement_solves_the_same_frequencies_whatever_the_floor_above_1_ghz(tmp_path, monkeypatch):
    """The floor's reflection is left out of the reported field above 1 GHz (CISPR 32), but not out of the
    refinement's choice of gaps [A100]: a board with cavity resonances above 1 GHz is solved at the same
    frequencies as on a site that reflects everywhere, and its rows above 1 GHz then differ by the 6.02 dB alone.
    Before, the gaps above 1 GHz fell out of the refinement's window: a solve fewer here (2150 MHz), and on the
    refboard a resonance near 1.07 GHz read off an interpolation, 8 dB better than its solve."""
    from boardgen import microstrip_board
    from conftest import make
    board, sc, _ = make(tmp_path, microstrip_board(layers=4), "w", capacitance=True)
    cispr = simulate(board, sc.config).routes[0]
    monkeypatch.setattr(radiation, "CISPR_FLOOR_TOP", 1e99)
    everywhere = simulate(board, sc.config).routes[0]
    solved = [s["f"] for s in cispr.cavity_solves]
    assert solved == [s["f"] for s in everywhere.cavity_solves]
    assert sum(1e9 < f < 3e9 for f in solved) >= 2                 # the gaps above 1 GHz were split
    rows = [(a["f"], b["e_dbuv"] - a["e_dbuv"]) for a, b in zip(cispr.emissions, everywhere.emissions)]
    assert any(f > 1.2e9 for f, _ in rows)
    for f, d in rows:
        if f > 1.2e9:                                              # clear of the receiver windows at 1 GHz
            assert d == pytest.approx(20 * math.log10(2), abs=0.01)
        elif f < 0.9e9:
            assert d == pytest.approx(0.0, abs=0.01)


def test_floor_absorbed_is_cispr_above_1_ghz_only():
    """Above 1 GHz for CISPR 32's classes; never for a user limit (RE102) or with the reflection off."""
    f = np.array([0.5e9, 1e9, 1.5e9])
    assert floor_absorbed(f, "B").tolist() == [False, False, True]
    assert not floor_absorbed(f, "B", ground_reflection=False).any()
    assert not floor_absorbed(f, RE102_LIMITS["re102_ground_army"]).any()


@pytest.mark.parametrize("length", [0.02, 0.05, 0.15])
def test_E_1_a_long_trace_radiates_with_its_currents_phase_along_the_route(length):
    """The current a path radiates with (PathResult.i_rad: the maximum over direction of the route's radiation
    integral) against the exact far field of the trace over a plane (image theory, the end posts and the
    displacement current included): within 0.3 dB from 30 MHz to 6 GHz; it is the mean current while the trace is
    short, and never under it. Before (audit round 12, E-1) the mean alone: through nulls where the trace radiates
    towards its ends, more than 10 dB low somewhere above a quarter wavelength of route."""
    fs = np.geomspace(30e6, 6e9, 40)
    got = path_currents(fs, np.ones(len(fs)), [PathLine(50.0, 3.0, length)], PathSource(25.0), PathLoad(5e-12))
    want = np.array([trace_far_field(float(f), length) for f in fs])
    assert np.max(np.abs(20 * np.log10(got.i_rad / want))) < 0.3
    assert np.all(got.i_rad >= np.abs(got.i_mean) * (1 - 1e-9))
    short = fs * length * math.sqrt(3.0) / C0 < 0.05                 # under a twentieth of a wavelength
    assert got.i_rad[short] == pytest.approx(np.abs(got.i_mean[short]), rel=0.01)
    if length >= 0.05:
        assert np.max(20 * np.log10(want / np.abs(got.i_mean))) > 10.0
