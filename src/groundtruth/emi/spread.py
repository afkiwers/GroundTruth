"""Spread-spectrum switching: what an EMI receiver reads from a dithered harmonic.

A controller with spread spectrum moves its switching frequency between f0 and f0·(1 + spread). Harmonic n then
sweeps across n·f0 … n·f0·(1 + spread). How much that lowers a reading depends on the receiver: its resolution
bandwidth (RBW), its detector and how long it dwells at each frequency, and on how the frequency moves. A slow
sweep passes through the receiver's filter at full amplitude, so a peak detector sees no reduction; fast
modulation or random hopping spreads the power, and the filter only catches its share [Hardin 1994].

Rather than a rule of thumb, `receiver_gain` simulates the receiver: the harmonic's complex baseband signal over
one dwell time, a Gaussian filter [A8] of the given 6 dB bandwidth tuned across the swept band, and the detector (peak:
largest envelope; average: mean envelope). The result is the reading relative to the unmodulated harmonic.
Validated in tests/test_spread.py against sinusoidal FM (the Bessel line amplitudes [Haykin 2001]), a slow sweep
(no reduction) and random hopping (Rayleigh statistics [Papoulis 2002]). Source keys: docs/REFERENCES.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import numpy as np

MODES = ("triangle", "random", "sine")
DWELL_S = 0.015                 # minimum dwell time per frequency above 1 kHz [MIL-STD-461G, Table II]
LN2 = math.log(2.0)


@dataclass(frozen=True)
class Spread:
    """fraction: the frequency moves between f0 and f0·(1 + fraction). mode: triangle (a linear sweep up and down at
    `rate` Hz), random (a new random frequency every switching cycle, or `rate` times per second) or sine
    (sinusoidal at `rate`, for tests)."""
    fraction: float
    mode: str = "triangle"
    rate: Optional[float] = None


def _modulation(spread: Spread, f0: float, t: np.ndarray, seed: int) -> np.ndarray:
    """m(t) in [0, 1]: the instantaneous frequency is f0·(1 + fraction·m(t))."""
    if spread.mode == "triangle":
        x = (t * spread.rate) % 1.0
        return 1.0 - np.abs(2.0 * x - 1.0)
    if spread.mode == "sine":
        return 0.5 * (1.0 + np.sin(2 * math.pi * spread.rate * t))
    rng = np.random.default_rng(seed)          # random: a new value every cycle (or at `rate` hops per second)
    k = np.floor(t * (spread.rate or f0)).astype(np.int64)
    return rng.random(int(k[-1]) + 1)[k]


def _signal(n: int, f0: float, spread: Spread, rbw: float, dwell: float, seed: int):
    """The harmonic's complex baseband signal (offset from n·f0) as a spectrum, its frequency axis, the samples kept
    for the detector (the filter's settling cut from both ends) and the sweep width."""
    width = n * f0 * spread.fraction            # the sweep of harmonic n (Hz)
    edge = 3.0 / rbw
    T = dwell + 2 * edge
    fs = 8.0 * (width + 4.0 * rbw)
    N = int(2 ** math.ceil(math.log2(max(T * fs, 1024))))
    t = np.arange(N) / fs
    dev = width * _modulation(spread, f0, t, seed)
    s = np.exp(1j * 2 * math.pi * np.cumsum(dev) / fs)
    return np.fft.fft(s), np.fft.fftfreq(N, 1 / fs), slice(int(edge * fs), int((edge + dwell) * fs)), width


FILTER_SPAN = 4.0               # bins within ±4 RBW of the tuning: the Gaussian filter is 5e-20 there


BATCH_POINTS = 2 ** 19          # tunings read together: rows × points per 2-D inverse FFT (8 MB)


def _read(S, f, keep, fc: float, rbw: float, detector: str) -> float:
    """Receiver tuned to fc (baseband offset): Gaussian filter, 6 dB at fc ± rbw/2 and unity at fc; peak or
    average of the envelope over the dwell. One tuning of _read_many."""
    return float(_read_many(S, f, keep, np.array([fc], float), rbw, detector)[0])


def _read_many(S, f, keep, fcs: np.ndarray, rbw: float, detector: str) -> np.ndarray:
    """_read at every tuning in fcs.

    Only the spectrum within ±FILTER_SPAN·RBW of a tuning passes, so the filter output is the inverse FFT of those
    2K+1 bins alone, placed in a short array of M (a power of two, ≥ 4K+2) points: the full inverse FFT at every
    N/M-th time sample, times a unit phase factor per sample that the envelope drops. M samples over the record
    sample the envelope at ≥ 8 times the filter bandwidth, enough for its peak; a wide radiated sweep reads in
    ~1000 points instead of a million. Tunings go through in batches, one row each of a 2-D inverse FFT."""
    N = len(S)
    df = float(f[1] - f[0])
    K = int(math.ceil(FILTER_SPAN * rbw / df))
    M = 1 << int(math.ceil(math.log2(4 * K + 2)))
    reduce = np.max if detector == "peak" else np.mean
    if M >= N or keep.stop - keep.start < 4 * (N // M):   # a short record (or dwell): the full inverse FFT
        return np.array([reduce(np.abs(np.fft.ifft(S * np.exp(-LN2 * ((f - fc) / (rbw / 2)) ** 2)))[keep])
                         for fc in fcs])
    q = np.arange(-K, K + 1)
    r = N // M
    lo, hi = -(-keep.start // r), keep.stop // r          # the kept samples on the coarse time grid
    out = np.empty(len(fcs))
    rows = max(1, BATCH_POINTS // M)
    for i in range(0, len(fcs), rows):
        fc = np.asarray(fcs[i:i + rows], float)[:, None]
        k = np.rint(fc / df).astype(np.int64) + q              # the bins near each tuning
        H = np.exp(-LN2 * ((k * df - fc) / (rbw / 2)) ** 2)
        sub = np.zeros((len(fc), M), complex)
        sub[:, q % M] = S[k % N] * H                           # circular: the window may straddle 0 Hz
        env = np.abs(np.fft.ifft(sub, axis=1)) * (M / N)        # ifft's 1/M → the full record's 1/N
        out[i:i + rows] = reduce(env[:, lo:hi], axis=1)
    return out


MAX_SAMPLES = 2 ** 20          # the largest synthesised signal (16 MB per array, ~50 ms per filter pass)
MIN_RANDOM_RBW_DWELL = 30.0    # a shortened random dwell keeps at least this many filter response times


def _dwell_plan(n: int, f0: float, spread: Spread, rbw: float, dwell: float) -> Optional[float]:
    """The dwell to synthesise for harmonic n, or None when even the shortest faithful one costs more than
    MAX_SAMPLES. The signal is sampled at 8·(sweep width + 4·RBW) over the dwell, so a radiated harmonic (a 20 %
    sweep at 1 GHz is 200 MHz wide) over MIL-STD-461's 15 ms would need ~10⁸ samples. Shortened instead:
    - triangle and sine modulation are periodic: one modulation period gives the same peak and the same mean
      envelope as any whole number of periods (the filter has settled, the kept samples start 3/RBW in);
    - random hopping is stationary: a shorter dwell gives the same mean (average detector) and fewer independent
      samples for the peak, corrected in _gain;
    - otherwise None: harmonic_gains extrapolates from the simulated harmonics."""
    width = n * f0 * spread.fraction
    fs = 8.0 * (width + 4.0 * rbw)
    edge = 3.0 / rbw
    if spread.mode in ("triangle", "sine"):
        # whole periods, at least 4 filter response times (the envelope is then sampled ≥ 64 times, see _read)
        d = min(dwell, math.ceil(4.0 * spread.rate / rbw) / spread.rate)
        return d if fs * (d + 2 * edge) <= MAX_SAMPLES else None
    d = min(dwell, MAX_SAMPLES / fs - 2 * edge)
    return d if d * rbw >= MIN_RANDOM_RBW_DWELL else None


@lru_cache(maxsize=4096)
def _gain(n: int, f0: float, spread: Spread, rbw: float, detector: str, dwell: float, seed: int) -> float:
    """The receiver's reading of harmonic n of a spread clock, relative to the same harmonic unspread (at most
    1): the modulated harmonic is synthesised (_signal) and read with the receiver tuned across its band, keeping
    the largest reading (a receiver scan finds the worst tuning). The unit-amplitude signal makes an unspread line
    read 1. NaN when the synthesis would be too large (_dwell_plan). Cached (lru_cache): the synthesis is the
    expensive part."""
    if n * f0 * spread.fraction <= 0:
        return 1.0
    d = _dwell_plan(n, f0, spread, rbw, dwell)
    if d is None:
        return math.nan
    S, f, keep, width = _signal(n, f0, spread, rbw, d, seed)
    # receiver tunings across the band (plus margins); a coarse grid, then refine around the best. A triangle or
    # sine sweep slows down near its ends, where it reads highest in peaks only a few RBW wide (a dense scan of
    # wide 10 kHz triangle sweeps found the worst tuning 1-3 % of the width in from an end): the coarse grid (1 MHz
    # apart across a 30 MHz radiated sweep) would miss them, so the outer 5 % at each end (at least 3 RBW) gets
    # tunings RBW/4 apart, at most 400 per end
    span = max(3.0 * rbw, 0.05 * width)
    ends = np.arange(-rbw, span, max(rbw / 4, (span + rbw) / 400))
    # and the whole band at ~RBW/2, as a receiver stepping through it finds the worst tuning (at most 2000): with the
    # modulation rate near the RBW the swept harmonic is a comb of lines one rate apart whose strongest can lie
    # anywhere (a 500 kHz triangle sweep at 10 kHz, RBW 9 kHz: worst at 110 kHz in, which 25 coarse tunings missed
    # by 0.5 dB before 2026-10-18; random hopping, noise-like, was scanned so already)
    band = np.arange(-rbw, width + rbw, max(rbw / 2, (width + 2 * rbw) / 2000))
    fcs = band if spread.mode == "random" else np.unique(np.concatenate([band, ends, width - ends]))
    coarse = _read_many(S, f, keep, fcs, rbw, detector)
    best = float(coarse.max())
    # refine around the five best tunings (one alone can climb a lesser local peak) until the tuning step is
    # below RBW/8 (≤ 0.2 dB off a line)
    for seed in fcs[np.argsort(coarse)[-5:]]:
        step, best_fc = max(rbw / 2, (width + 2 * rbw) / 2000), seed     # the band scan's spacing
        while step > rbw / 8:
            grid = np.linspace(best_fc - step, best_fc + step, 9)
            vals = _read_many(S, f, keep, grid, rbw, detector)
            i = int(np.argmax(vals))
            if vals[i] > best:
                best = vals[i]
            best_fc, step = grid[i], grid[1] - grid[0]
    if detector == "peak" and spread.mode == "random" and d < dwell:
        # a shortened random dwell: the peak of N ≈ RBW·dwell Rayleigh samples grows as sqrt(ln N) (the extreme
        # value of N Rayleigh samples, σ·sqrt(2 ln N) [Papoulis 2002]; tests:
        # test_random_hopping_follows_rayleigh_statistics), so scale to the full dwell's N
        best *= math.sqrt(math.log(rbw * dwell) / math.log(rbw * d))
    return min(best, 1.0)


def receiver_gain(n: int, f0: float, spread: Spread, rbw: float, detector: str = "peak",
                  dwell: float = DWELL_S, seed: int = 1) -> float:
    """Reading of harmonic n of a switching frequency f0 dithered by `spread`, relative to the undithered
    harmonic, for a receiver of 6 dB bandwidth `rbw` tuned to the worst frequency in the sweep, with a peak or
    average detector over `dwell` seconds. 1 = no reduction; NaN = too wide a sweep to simulate (_dwell_plan)."""
    if spread.mode not in MODES:
        raise ValueError(f"spread mode must be one of {', '.join(MODES)}")
    if spread.mode in ("triangle", "sine") and not spread.rate:
        raise ValueError(f"spread mode {spread.mode} needs a modulation rate")
    return _gain(int(n), float(f0), spread, float(rbw), detector, float(dwell), int(seed))


MAX_SIMULATED = 16              # harmonics simulated per series; the gain in between is interpolated (smooth in n)


def harmonic_gains(freqs, f0: float, spread: Spread, rbw_of, detector: str = "peak",
                   f_max: float = math.inf) -> np.ndarray:
    """receiver_gain for every harmonic in `freqs` (= n·f0) up to f_max (1 above): simulated at up to
    MAX_SIMULATED log-spaced harmonics, interpolated in log(n) in between. Harmonics whose sweep is too wide to
    simulate (receiver_gain NaN) are extrapolated from the simulated ones (_extrapolate); with none simulated
    they keep 1 (no credit for the spread)."""
    f = np.asarray(freqs, float)
    n = np.maximum(np.rint(f / f0).astype(int), 1)
    g = np.ones(len(f))
    inside = np.nonzero(f <= f_max)[0]
    if not len(inside):
        return g
    ns = np.unique(n[inside])
    # each receiver simulation costs FFTs of ~a million points: only MAX_SIMULATED harmonics, spaced evenly on a
    # log scale (dense where the gain changes most), the rest interpolated in log-log
    pick = ns if len(ns) <= MAX_SIMULATED else \
        np.unique(ns[np.rint(np.geomspace(1, len(ns), MAX_SIMULATED)).astype(int) - 1])
    gp = []
    for k in pick:
        gp.append(receiver_gain(int(k), f0, spread, float(rbw_of(k * f0)), detector))
        if math.isnan(gp[-1]):              # wider sweeps cost more still: the rest are extrapolated too
            gp += [math.nan] * (len(pick) - len(gp))
            break
    gp = np.array(gp)
    ok = ~np.isnan(gp)
    if not ok.any():
        return g
    log_g = np.log(np.maximum(gp[ok], 1e-9))
    log_n = np.log(n[inside].astype(float))
    g[inside] = np.exp(np.interp(log_n, np.log(pick[ok]), log_g))
    beyond = log_n > np.log(pick[ok][-1])
    if beyond.any():
        g[inside[beyond]] = np.exp(_extrapolate(np.log(pick[ok]), log_g, log_n[beyond]))
    return g


MAX_SLOPE = 0.5                 # a wide sweep's reading falls at most as 1/sqrt(n) (see _extrapolate)


def _extrapolate(log_n: np.ndarray, log_g: np.ndarray, at: np.ndarray) -> np.ndarray:
    """log gain beyond the last simulated harmonic: the slope of the last two simulated ones in log-log, kept
    between 0 (no further reduction: the safe side) and -MAX_SLOPE. A sweep much wider than the bandwidth reads
    ∝ width^(-1/2) for both modulations: a linear sweep through a Gaussian filter peaks at (1 + (μa/π)²)^(-1/4) (a
    Gaussian filter's response to a linear chirp, derived in closed form) ∝
    μ^(-1/2) with μ ∝ width (test_a_fast_sweep_through_the_filter_reads_the_chirp_gain), and random hopping's
    Rayleigh envelope at sqrt(RBW/width) (test_random_hopping_follows_rayleigh_statistics)."""
    slope = 0.0
    if len(log_n) >= 2 and log_n[-1] > log_n[-2]:
        slope = (log_g[-1] - log_g[-2]) / (log_n[-1] - log_n[-2])
    slope = min(max(slope, -MAX_SLOPE), 0.0)
    return log_g[-1] + slope * (at - log_n[-1])


def spread_readings(freqs, peaks, f0: float, spread: Spread, rbw_of, detector: str = "peak",
                    f_max: float = math.inf) -> np.ndarray:
    """Receiver readings of a harmonic series (peak amplitudes at f = n·f0) with spread spectrum, up to f_max
    (unchanged above). Harmonic n reads gain_n·a_n; where the sweeps of neighbouring harmonics overlap
    (n·fraction ≥ 1), the receiver at harmonic n also catches the neighbours whose sweeps pass through its band.
    The harmonics' instantaneous frequencies are f0 apart, more than the bandwidth (the callers' condition), so at
    any instant at most one of them is in the filter:
    - a peak detector reads the largest single pass: max(gain_n·a_n, gain_k·a_k of the neighbours passing). Adding
      them as power (as before 2026-10-16) read up to 6 dB above the undithered harmonic where a dozen sweeps
      overlap (a 10 % sweep at 60-100 MHz), which a peak detector can never do;
    - an averaging detector reads the time average of the power: the own power plus each neighbour's power times
      the share of its sweep inside this band (their phases are unrelated once the frequency moves).
    Exact (for the model) where sweeps don't overlap."""
    f = np.asarray(freqs, float)
    a = np.asarray(peaks, float)
    g = harmonic_gains(f, f0, spread, rbw_of, detector, f_max)
    out = a.copy()
    lo, hi = f, f * (1 + spread.fraction)          # the band each harmonic sweeps
    p_own = (g * a) ** 2
    for j in np.nonzero(f <= f_max)[0]:
        k = np.nonzero((hi > lo[j]) & (lo < hi[j]))[0]        # the harmonics whose sweeps overlap this one's
        k = k[k != j]
        if detector == "peak":
            out[j] = math.sqrt(max(p_own[j], float(np.max(p_own[k], initial=0.0))))
            continue
        # each neighbour spends a share of its time inside this sweep: that share of its power counts here
        share = (np.minimum(hi[k], hi[j]) - np.maximum(lo[k], lo[j])) / np.maximum(hi[k] - lo[k], 1e-12)
        out[j] = math.sqrt(p_own[j] + float(np.sum(share * p_own[k])))
    return out
