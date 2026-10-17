"""Spread-spectrum receiver model (emi/spread.py) against analytic results."""
import math

import numpy as np
import pytest
from scipy.special import jv

from groundtruth.emi.spread import Spread, harmonic_gains, receiver_gain, spread_readings


def test_no_spread_reads_the_full_harmonic():
    assert receiver_gain(3, 750e3, Spread(0.0, "random"), 10e3) == 1.0


def test_fast_sinusoidal_fm_reads_the_largest_bessel_line():
    """Sinusoidal FM with deviation ±Δ at f_m splits the harmonic into lines at k·f_m of amplitude |J_k(Δ/f_m)|.
    With an RBW far below f_m the receiver resolves them, so its peak reading is the largest |J_k|."""
    f0, frac, fm, rbw = 500e3, 0.2, 30e3, 1e3          # fundamental swept 100 kHz: Δ = 50 kHz, β = 5/3
    beta = (f0 * frac / 2) / fm
    expected = max(abs(jv(k, beta)) for k in range(-10, 11))
    g = receiver_gain(1, f0, Spread(frac, "sine", fm), rbw, dwell=0.004)
    assert 20 * math.log10(g) == pytest.approx(20 * math.log10(expected), abs=0.3), (g, expected)


def test_a_slow_sweep_passes_the_peak_detector_at_full_amplitude():
    """A triangle sweep at 100 Hz moves 100 kHz in 5 ms: it dwells 0.5 ms in a 10 kHz filter, five times the
    filter's response time, so the peak detector reads (almost) the unmodulated amplitude."""
    g = receiver_gain(1, 500e3, Spread(0.2, "triangle", 100.0), 10e3)
    assert g > 0.95, g


def test_random_hopping_follows_rayleigh_statistics():
    """Random hops 300 000 times a second over the 13th harmonic's sweep W = 1.95 MHz: the power spreads evenly over
    W (each hop's own ~300 kHz spectrum is small next to W), the filter passes the share P = ∫|H|²df / W =
    0.753·RBW/W, and with ~30 hops per filter response time its envelope is Rayleigh distributed. The average
    detector reads sqrt(π·P)/2. The peak detector reads the largest of N ≈ RBW·dwell independent samples,
    sqrt(P·ln N), at one tuning, and up to sqrt(P·ln(N·W/RBW)) as the receiver picks the worst of the tunings."""
    f0, frac, rbw, dwell, n = 750e3, 0.2, 10e3, 0.015, 13
    width = n * f0 * frac
    p = 0.7528 * rbw / width
    sp = Spread(frac, "random", 300e3)
    peak = receiver_gain(n, f0, sp, rbw, "peak", dwell)
    avg = receiver_gain(n, f0, sp, rbw, "average", dwell)
    assert 20 * math.log10(avg) == pytest.approx(20 * math.log10(math.sqrt(math.pi * p) / 2), abs=1.0)
    # Only ~3 hops per filter response land close enough to a tuning to matter, too few for Gaussian statistics:
    # single hops push the peak above the Rayleigh value (measured 0.29 against 0.14-0.20). Lower bound only.
    assert peak > math.sqrt(p * math.log(rbw * dwell)) and peak > avg
    # Slower hops (50 000/s, 20 µs each, a third of the filter's response) more so: single hops on the tuning set
    # the peak. The simulation covers that regime; a closed formula would not.
    slow = receiver_gain(5, f0, Spread(frac, "random", 50e3), rbw, "peak", dwell)
    assert slow > 2 * math.sqrt(0.7528 * rbw / (5 * f0 * frac) * math.log(rbw * dwell))


def test_a_fast_sweep_through_the_filter_reads_the_chirp_gain():
    """A linear sweep at μ Hz/s through a Gaussian filter H = exp(-a·f²), a = 4·ln2/RBW², peaks at
    (1 + (μ·a/π)²)^(-1/4) of the tone's amplitude (the Gaussian-chirp convolution). Mid-band of a triangle sweep
    of the 13th harmonic (1.95 MHz up and down 2000 times a second: μ = 7.8 GHz/s, passes 250 µs apart, well
    beyond the filter's ~60 µs response, so they don't add up) the receiver reads that; at the turning points the
    sweep slows down, so the worst tuning reads more."""
    from groundtruth.emi.spread import _read, _signal
    f0, frac, rbw, rate, n = 750e3, 0.2, 10e3, 2e3, 13
    sp = Spread(frac, "triangle", rate)
    S, f, keep, width = _signal(n, f0, sp, rbw, 0.002, 1)
    mu, a = 2 * width * rate, 4 * math.log(2) / rbw ** 2
    expected = (1 + (mu * a / math.pi) ** 2) ** -0.25
    mid = _read(S, f, keep, width / 2, rbw, "peak")
    assert 20 * math.log10(mid) == pytest.approx(20 * math.log10(expected), abs=0.3), (mid, expected)
    assert receiver_gain(n, f0, sp, rbw) > 1.5 * mid


def test_series_interpolates_gains_and_adds_overlapping_sweeps():
    f0 = 100e3
    f = f0 * np.arange(1, 61)
    sp = Spread(0.2, "random")
    g = harmonic_gains(f, f0, sp, lambda x: 10e3, f_max=5e6)
    assert np.all(g[f > 5e6] == 1.0) and np.all(np.diff(g[f <= 5e6]) <= 0.05)   # wider sweeps, lower readings
    a = np.ones(len(f))
    r = spread_readings(f, a, f0, sp, lambda x: 10e3, f_max=5e6)
    # harmonic 10 sweeps 1.0-1.2 MHz and overlaps harmonic 11's 1.1-1.32 MHz: it reads more than its own share
    assert r[9] > g[9] and r[0] == pytest.approx(g[0])


def test_ce102_reading_of_a_dithered_converter():
    """conducted_arrays with spread: the fundamental (its sweep overlaps no other harmonic) reads exactly the
    simulated receiver gain lower; a converter switching below the receiver bandwidth keeps its plain reading."""
    from groundtruth.emi.conducted import conducted_arrays
    f0 = 787e3
    f = f0 * np.arange(1, 14)
    line = 0.1 / np.arange(1, 14)
    sp = Spread(0.2, "random")
    plain = conducted_arrays(f, line, "mil461")["v_dbuv"]
    dith = conducted_arrays(f, line, "mil461", spread=(f0, sp))["v_dbuv"]
    assert dith[0] - plain[0] == pytest.approx(20 * math.log10(receiver_gain(1, f0, sp, 10e3)), abs=1e-6)
    assert np.all(dith <= plain + 1e-9)
    low = 5e3 * np.arange(1, 50)                       # 5 kHz switching, harmonics closer than the 10 kHz RBW
    a = conducted_arrays(low, 1e-3 * np.ones(49), "mil461")["v_dbuv"]
    b = conducted_arrays(low, 1e-3 * np.ones(49), "mil461", spread=(5e3, sp))["v_dbuv"]
    assert np.allclose(a, b)


def test_spread_settings_round_trip():
    from groundtruth.config.settings import ConfigError, config_from_data
    c = config_from_data({"converters": [{"name": "x", "rail": "VIN", "ground": "GND", "switch": "U1", "spread": 0.2,
                                          "spread_mode": "triangle", "spread_rate": "4kHz"}]}).converters[0]
    assert (c.spread, c.spread_mode, c.spread_rate) == (0.2, "triangle", pytest.approx(4e3))
    with pytest.raises(ConfigError, match="needs spread_rate"):
        config_from_data({"converters": [{"name": "x", "rail": "VIN", "ground": "GND", "switch": "U1", "spread": 0.2,
                                          "spread_mode": "triangle"}]})


def test_spread_spectrum_lowers_a_power_stages_radiated_reading_too():
    """A power stage's radiated estimate reads its dithered harmonics with the receiver (as the conducted one does):
    a 500 kHz stage swept 10 % at 10 kHz reads lower at 60-100 MHz than undithered, and nothing changes where the
    switching frequency is below the receiver bandwidth (each reading then holds several harmonics, the
    undithered sum is kept)."""
    import numpy as np
    from groundtruth.emi.radiation import emission_arrays
    f0 = 500e3
    f = np.arange(120, 201) * f0                                # 60-100 MHz
    i = 1e-3 * 120 / np.arange(120, 201)                        # a 1/n harmonic series
    plain = emission_arrays(f, i, np.full(len(f), 1e-4))
    spread = emission_arrays(f, i, np.full(len(f), 1e-4), spread=(f0, Spread(0.1, "triangle", 10e3)))
    assert np.all(spread["e_dbuv"] <= plain["e_dbuv"] + 1e-9) and np.max(plain["e_dbuv"] - spread["e_dbuv"]) > 3
    f_low = np.arange(1, 400) * 100e3                           # 100 kHz < the 120 kHz CISPR bandwidth
    a = emission_arrays(f_low, np.full(len(f_low), 1e-4), np.full(len(f_low), 1e-4))
    b = emission_arrays(f_low, np.full(len(f_low), 1e-4), np.full(len(f_low), 1e-4),
                        spread=(100e3, Spread(0.1, "triangle", 10e3)))
    assert np.allclose(a["e_dbuv"], b["e_dbuv"])


def test_one_modulation_period_reads_like_the_full_dwell():
    """A triangle sweep is periodic: the receiver reads the same peak and mean envelope over one modulation period
    as over five (what _dwell_plan relies on to read wide radiated sweeps), and the band-limited read (_read on
    the bins near the tuning) equals the full inverse FFT."""
    from groundtruth.emi.spread import LN2, _read, _signal
    sp, n, f0, rbw = Spread(0.2, "triangle", 20e3), 60, 500e3, 120e3
    one = _signal(n, f0, sp, rbw, 1 / sp.rate, 1)
    five = _signal(n, f0, sp, rbw, 5 / sp.rate, 1)
    for fc in (0.0, 0.4 * one[3], one[3]):
        for det in ("peak", "average"):
            a, b = _read(*one[:3], fc, rbw, det), _read(*five[:3], fc, rbw, det)
            assert 20 * math.log10(a / b) == pytest.approx(0, abs=0.1), (fc, det)
        S, f, keep, _ = one
        full = np.abs(np.fft.ifft(S * np.exp(-LN2 * ((f - fc) / (rbw / 2)) ** 2)))[keep]
        assert _read(S, f, keep, fc, rbw, "peak") == pytest.approx(full.max(), rel=0.01)


def test_sweeps_too_wide_to_simulate_are_extrapolated(monkeypatch):
    """Harmonics whose sweep would need more than MAX_SAMPLES are not simulated: their reading continues the last
    simulated ones' slope, no steeper than 1/sqrt(n) (the wide-sweep limit) and never rising."""
    from groundtruth.emi import spread as s
    f0, sp = 500e3, Spread(0.2, "triangle", 10e3)
    f = f0 * np.arange(10, 2001)
    full = harmonic_gains(f, f0, sp, lambda x: 120e3)
    s._gain.cache_clear()
    monkeypatch.setattr(s, "MAX_SAMPLES", 2 ** 15)               # simulated only up to a ~20 MHz wide sweep
    try:
        cut = harmonic_gains(f, f0, sp, lambda x: 120e3)
    finally:
        s._gain.cache_clear()
    simulated = [k for k in range(10, 2001) if s._dwell_plan(k, f0, sp, 120e3, s.DWELL_S) is not None]
    last = max(simulated)
    out = np.arange(10, 2001) > last
    assert out.any() and np.all(np.diff(cut[out]) <= 1e-12)
    g_last = cut[np.arange(10, 2001) == last][0]
    assert np.all(cut[out] >= g_last * np.sqrt(last / np.arange(10, 2001)[out]) - 1e-12)
    # the extrapolation stays within a few dB of the simulated reading
    assert np.max(np.abs(20 * np.log10(cut[out] / full[out]))) < 3


def test_overlapping_sweeps_read_by_peak_and_average_detectors():
    """Where the sweeps of neighbouring harmonics overlap, a peak detector reads the largest single pass (at most
    one harmonic is in the filter at a time: they are f0 apart, more than the bandwidth), never above the largest
    undithered harmonic; an averaging detector adds the neighbours' power by the share of their sweep in the band."""
    f0 = 500e3
    f = f0 * np.arange(100, 141)                                 # 10 % sweeps: ~10 neighbours overlap each band
    a = np.ones(len(f))
    sp = Spread(0.1, "triangle", 10e3)
    peak = spread_readings(f, a, f0, sp, lambda x: 120e3, "peak")
    g = harmonic_gains(f, f0, sp, lambda x: 120e3, "peak")
    assert np.all(peak <= 1.0 + 1e-12) and np.all(peak >= g - 1e-12)
    lo, hi = f, f * 1.1
    passing = (hi > lo[20]) & (lo < hi[20])                      # the sweeps reaching harmonic 120's band
    assert peak[20] == pytest.approx(g[passing].max())            # the strongest single pass among them
    avg = spread_readings(f, a, f0, sp, lambda x: 120e3, "average")
    g_avg = harmonic_gains(f, f0, sp, lambda x: 120e3, "average")
    assert np.all(avg > g_avg)                                   # the neighbours' power adds
