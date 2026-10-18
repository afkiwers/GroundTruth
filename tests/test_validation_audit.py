"""Independent validation of Signal paths, Converters and DC loads (docs/VALIDATION_AUDIT.md).

Every expected value here is computed by a method of its own, not by the function under test: a sampled FFT for a
spectrum, a nodal solve of a lumped ladder for a transmission-line path, a time-domain integration for a switching
cell, the textbook closed forms typed in again for a loop gain, a hand-made resistor ladder for a DC rail. The ids
(SP-…, CV-…, DC-…) are the validation matrix's rows in the audit report.
"""
import math

import numpy as np
import pytest

C0 = 299_792_458.0
MU0 = 4e-7 * math.pi
ETA0 = MU0 * C0


# ===================================================================================================== Signal paths
def _fft_harmonics(f0, amplitude, duty, tr, tf, n_max, samples=2 ** 18):
    """Peak amplitudes of harmonics 1..n_max of a trapezoid sampled over one period: rising edge tr from t = 0, high
    until the falling edge's midpoint sits at duty·T (the duty measured at half amplitude), falling edge tf."""
    T = 1.0 / f0
    t = np.arange(samples) * T / samples
    rise_mid, fall_mid = tr / 2, tr / 2 + duty * T
    x = np.clip((t - (rise_mid - tr / 2)) / tr, 0, 1) - np.clip((t - (fall_mid - tf / 2)) / tf, 0, 1)
    X = np.fft.rfft(x * amplitude) / samples
    return 2 * np.abs(X[1:n_max + 1])


@pytest.mark.parametrize("duty, tr, tf", [(0.5, 1e-9, None), (0.3, 2e-9, None), (0.5, 1e-9, 3e-9),
                                          (0.2, 0.5e-9, 2e-9)])
def test_SP1_trapezoid_harmonics_match_a_sampled_fft(duty, tr, tf):
    """SP-1: c_n of the trapezoid (equal edges and a fall time of its own) against an FFT of the sampled wave."""
    from groundtruth.emi.spectrum import trapezoid_harmonics
    f0, amp = 50e6, 3.3
    f, c = trapezoid_harmonics(f0, tr, amp, duty, f_max=40 * f0, fall_time=tf)
    ref = _fft_harmonics(f0, amp, duty, tr, tf if tf else tr, 40)
    n = np.rint(f / f0).astype(int)
    big = ref[n - 1] > amp * 1e-4             # nulls (even harmonics at 50 %) are dropped by the code
    assert np.allclose(c[big[: len(c)]], ref[n - 1][big[: len(c)]], rtol=2e-3, atol=amp * 1e-6)


def test_SP2_microstrip_and_stripline_closed_forms_against_the_2d_field_solver():
    """SP-2: the path's Z0 and εeff (Hammerstad microstrip, IPC-2141 stripline) against the project's 2D electrostatic
    solver (itself validated against Cohn's exact results) and against Cohn's exact zero-thickness stripline [Cohn 1954]
    Z0 = 30π/√εr · K(k)/K(k'), k = sech(πw/2b). Closed forms are expected within a few per cent."""
    from scipy.special import ellipk
    from groundtruth.emi.spectrum import microstrip, stripline
    from groundtruth.solver.xsection import Rect, Section, Slab, solve
    # Cohn's exact stripline at zero thickness (scipy's ellipk takes m = k²)
    for w, b, er in ((0.1e-3, 0.5e-3, 4.3), (0.25e-3, 0.4e-3, 3.8), (1.0e-3, 0.5e-3, 4.5), (2.0e-3, 0.3e-3, 4.3)):
        k = 1 / math.cosh(math.pi * w / (2 * b))
        exact = 30 * math.pi / math.sqrt(er) * ellipk(k * k) / ellipk(1 - k * k)
        z, e = stripline(w, b, er, t=0.0)
        assert e == er and z == pytest.approx(exact, rel=0.01), (w, b, z, exact)
    # ISSUE-5: with the strip's thickness, against the field solver over the widths boards use (IPC-2141 read 4.7 Ω
    # for 18 Ω at w/b = 2 and went negative beyond)
    for b, wb, t in ((0.3e-3, 0.3, 18e-6), (0.3e-3, 0.8, 35e-6), (0.5e-3, 2.0, 35e-6), (0.27e-3, 4.0, 18e-6)):
        er, w = 4.3, wb * b
        W, y0 = max(20 * b, 4 * w), (b - t) / 2
        r = solve(Section([Rect(-W, W, 0, 0, 0), Rect(-W, W, b, b, 0), Rect(-w / 2, w / 2, y0, y0 + t, 1)],
                          [Slab(0, b, er)], (-W, W, 0, b)))
        assert stripline(w, b, er, t)[0] == pytest.approx(r.z0(), rel=0.01), (wb, t)
    # Hammerstad & Jensen's microstrip with thickness against the field solver (εeff of thick narrow strips read
    # up to 26 % high before)
    for w, h, er, t in ((0.3e-3, 0.2e-3, 4.3, 35e-6), (0.1e-3, 0.2e-3, 4.3, 18e-6), (1.0e-3, 0.2e-3, 3.5, 70e-6),
                        (0.2e-3, 0.1e-3, 4.5, 35e-6), (0.2e-3, 0.2e-3, 4.3, 0.0)):
        X = max(20 * h, 4 * w)
        r = solve(Section([Rect(-X, X, 0, 0, 0), Rect(-w / 2, w / 2, h, h + t, 1)], [Slab(0, h, er)],
                          (-X, X, 0, 21 * h + t)))
        z, e = microstrip(w, h, er, t)
        assert z == pytest.approx(r.z0(), rel=0.01) and e == pytest.approx(r.eeff(), rel=0.02), (w, h, t, z, r.z0())


def _ladder(freqs, v_s, elements, src, load, sections=600):
    """An independent solver for a signal path: every line a ladder of `sections` lumped cells (L' = Z0·√εeff/c,
    C' = √εeff/(Z0·c); with its width the losses as their own R' and G': the strip's and the plane's skin-depth
    resistance 2/(σ·w·δ(1 - e^(-t/δ))), G' = ωC'·tan δ times the microstrip filling factor [Pozar 2012]), parts as
    series or shunt impedances, the Thevenin source and the load, solved by nodal
    analysis per frequency. Returns (mean current along the path weighted by length, current into the path, voltage
    at the end) per frequency, the mean over the line cells' series currents and the parts' currents."""
    from groundtruth.emi.path import PathLine, PathShunt
    out_mean, out_in, out_end = [], [], []
    for f, vs in zip(freqs, v_s):
        w = 2 * math.pi * f
        # nodes: 0 = the source's output; series elements add a node, shunts hang at the current node
        series, shunts, n = [], [], 1           # series: (node a, node b, Z, length weight); shunts: (node, Y)
        cur = 0
        for e in elements:
            if isinstance(e, PathLine):
                dl = e.length_m / sections
                lp, cp = e.z0 * math.sqrt(e.eeff) / C0, math.sqrt(e.eeff) / (e.z0 * C0)
                rp = gp = 0.0
                if e.width_m:
                    delta = 1 / math.sqrt(math.pi * f * 4e-7 * math.pi * e.sigma)
                    rp = 2 / (e.sigma * e.width_m * delta * (1 - math.exp(-e.thickness_m / delta)))
                    er = e.er or e.eeff
                    fill = 1.0 if e.eeff >= er else er * (e.eeff - 1) / (e.eeff * (er - 1))
                    gp = w * cp * e.tan_d * fill
                for _ in range(sections):            # symmetric π cells: C·dl/2 at both ends (second order)
                    series.append((cur, n, (rp + 1j * w * lp) * dl, dl))
                    shunts.append((cur, (gp + 1j * w * cp) * dl / 2))
                    shunts.append((n, (gp + 1j * w * cp) * dl / 2))
                    cur, n = n, n + 1
            elif isinstance(e, PathShunt):
                z = e.z(f)
                shunts.append((cur, 0.0 if z is None else 1 / z))
            else:
                z = e.z(f)
                series.append((cur, n, 1e9 if z is None else z, e.length_m))
                cur, n = n, n + 1
        zl = 1 / (1j * w * max(load.c, 1e-15))
        if load.r is not None:
            zl = 1 / (1 / zl + 1 / load.r)
        shunts.append((cur, 1 / zl))
        Y = np.zeros((n, n), complex)
        for a, b, z, _ in series:
            y = 1 / z
            Y[a, a] += y
            Y[b, b] += y
            Y[a, b] -= y
            Y[b, a] -= y
        for a, y in shunts:
            Y[a, a] += y
        zs = src.r + 1j * w * src.l
        Y[0, 0] += 1 / zs + 1j * w * src.c
        rhs = np.zeros(n, complex)
        rhs[0] = vs / zs                        # the Thevenin source as its Norton equivalent
        V = np.linalg.solve(Y, rhs)
        tot = sum(L for *_, L in series)
        mean = sum((V[a] - V[b]) / z * L for a, b, z, L in series) / tot
        # the current into the path: the source's current minus its own output capacitance's
        i_in = (vs - V[0]) / zs - 1j * w * src.c * V[0]
        out_mean.append(mean); out_in.append(i_in); out_end.append(V[cur])
    return np.array(out_mean), np.array(out_in), np.array(out_end)


def _paths():
    """Signal paths of every element kind path_currents supports."""
    from groundtruth.components.models import ComponentModel
    from groundtruth.emi.path import PathLine, PathLoad, PathPart, PathShunt, PathSource
    r33 = ComponentModel("resistor", {"R": 33.0})
    bead = ComponentModel("ferrite", {"R": 0.1, "L": 1e-6})
    cap = ComponentModel("cap", {"C": 100e-9, "ESR": 0.02, "ESL": 0.5e-9})
    shunt = ComponentModel("cap", {"C": 22e-12, "ESR": 0.05, "ESL": 0.3e-9})
    term = ComponentModel("resistor", {"R": 50.0})
    return {
        "one line": ([PathLine(55.0, 3.2, 0.05)], PathSource(25.0), PathLoad(5e-12)),
        "line, series R, line": ([PathLine(50.0, 3.0, 0.02), PathPart("R1", r33.impedance, 1e-3),
                                  PathLine(60.0, 4.0, 0.03)], PathSource(25.0, 2e-9, 3e-12), PathLoad(10e-12)),
        "bead and AC cap": ([PathLine(50.0, 3.0, 0.01), PathPart("FB1", bead.impedance, 1.6e-3),
                             PathLine(50.0, 3.0, 0.01), PathPart("C1", cap.impedance, 1.0e-3),
                             PathLine(45.0, 3.4, 0.04)], PathSource(15.0), PathLoad(5e-12, 1e3)),
        "shunt cap and termination": ([PathLine(50.0, 3.0, 0.02), PathShunt("C2", shunt.impedance),
                                       PathLine(50.0, 3.0, 0.02), PathShunt("R9", term.impedance),
                                       PathLine(50.0, 3.0, 0.005)], PathSource(25.0), PathLoad(5e-12, 50.0)),
    }


@pytest.mark.parametrize("name", list(_paths()))
def test_SP3_the_path_circuit_matches_a_lumped_ladder_solved_by_nodal_analysis(name):
    """SP-3: path_currents (exact line transformations, ABCD stepping, the closed-form mean current) against a
    600-cell lumped ladder per line solved as one nodal system: the mean current that drives the loop model, the
    current into the path and the receiver voltage, from 1 MHz to 1.5 GHz (beyond a quarter wave on these lines)."""
    from groundtruth.emi.path import path_currents
    elements, src, load = _paths()[name]
    f = np.geomspace(1e6, 1.5e9, 25)
    v = np.full(len(f), 1.0)
    pr = path_currents(f, v, elements, src, load)
    mean, i_in, v_end = _ladder(f, v, elements, src, load)
    for got, ref in ((pr.i_mean, mean), (pr.i_in, i_in), (pr.v_end, v_end)):
        scale = np.maximum(np.abs(ref), 1e-3 * np.max(np.abs(ref)))
        assert np.max(np.abs(got - ref) / scale) < 0.02, name


def test_SP3b_loop_currents_split_the_mean_current_by_superposition():
    """SP-3b: the loops of a path with shunts add up to the path's own groups: Σ loop currents = the first group's
    mean, and the receivers' loop carries the last group's mean."""
    from groundtruth.emi.path import path_currents
    elements, src, load = _paths()["shunt cap and termination"]
    f = np.geomspace(1e6, 1e9, 12)
    pr = path_currents(f, np.ones(len(f)), elements, src, load)
    lc = pr.loop_currents()
    assert np.allclose(sum(lc), pr.group_mean[0])
    assert np.allclose(lc[-1], pr.group_mean[-1]) or pr.group_len[-1] < 0.02 * sum(pr.group_len)


def test_SP4_an_op_amp_stage_is_its_gain_times_its_input_behind_its_output_impedance():
    """SP-4: amplified_path: stage 2's source is gain(f)·v_in(amp) behind z_out; a single pole at its bandwidth is
    −3.01 dB there; the slew cap limits each harmonic to slew/2πf."""
    from groundtruth.components.models import ComponentModel
    from groundtruth.emi.path import PathAmp, PathLine, PathLoad, PathSource, amplified_path, path_currents
    amp = ComponentModel("amplifier", {"gain": 10.0, "bandwidth": 1e6, "z_out": 2.0})
    s1 = [PathLine(50.0, 3.0, 0.01)]
    s2 = [PathLine(60.0, 3.0, 0.02)]
    f = np.geomspace(1e4, 1e8, 30)
    v = np.ones(len(f))
    a = PathAmp("U1", amp.amp_gain, amp.amp_z_in, amp.amp_z_out())
    r1, r2 = amplified_path(f, v, [s1, s2], [a], PathSource(25.0), PathLoad(5e-12))
    # independently: stage 1 loaded by the op-amp's input (2 pF), stage 2 driven by 10/(1+jf/1MHz) x that voltage
    st1 = path_currents(f, v, s1, PathSource(25.0), PathLoad(2e-12))
    g = 10.0 / (1 + 1j * f / 1e6)
    st2 = path_currents(f, g * st1.v_end, s2, PathSource(2.0), PathLoad(5e-12))
    assert np.allclose(r1.v_end, st1.v_end) and np.allclose(r2.v_end, st2.v_end)
    assert 20 * math.log10(abs(amp.amp_gain(1e6)) / 10) == pytest.approx(-3.0103, abs=1e-3)
    capped = PathAmp("U1", amp.amp_gain, amp.amp_z_in, amp.amp_z_out(), slew=1e6)
    _, r2c = amplified_path(f, v * 5, [s1, s2], [capped], PathSource(25.0), PathLoad(5e-12))
    # the stage-2 source never exceeds slew/(2πf): its open-circuit voltage is r2c's source; check via the end voltage
    _, r2u = amplified_path(f, v * 5, [s1, s2], [a], PathSource(25.0), PathLoad(5e-12))
    vin_amp = np.abs(path_currents(f, v * 5, s1, PathSource(25.0), PathLoad(2e-12)).v_end * g)
    cap = 1e6 / (2 * math.pi * f)
    expect = np.where(vin_amp > cap, cap / vin_amp, 1.0)
    assert np.allclose(np.abs(r2c.v_end) / np.abs(r2u.v_end), expect, rtol=1e-9)


def test_SP5_the_loop_field_is_the_magnetic_dipole_s_with_its_induction_term():
    """SP-5: |E| of a small loop, broadside: η0·k²·|m|/(4πr)·√(1 + 1/(kr)²), ×2 for the ground reflection; the
    1.316e-14 constant is η0·π/c² to 0.1 %."""
    from groundtruth.emi.radiation import loop_field
    f = np.array([1e6, 30e6, 300e6, 3e9])
    m, r = 1e-6, 3.0
    k = 2 * math.pi * f / C0
    ref = ETA0 * k ** 2 * m / (4 * math.pi * r) * np.sqrt(1 + 1 / (k * r) ** 2)
    assert np.allclose(loop_field(f, m, r, ground_reflection=False), ref, rtol=1e-3)
    assert np.allclose(loop_field(f, m, r, ground_reflection=True), 2 * ref, rtol=1e-3)


def test_SP6_the_radiated_limits_are_cispr_32_s():
    """SP-6: CISPR 32 Table A.4/A.5 (class B) and A.2/A.3 (class A): quasi-peak at 10 m below 1 GHz, average at 3 m
    above; at a transition frequency the lower limit applies."""
    from groundtruth.emi.radiation import limits
    f = np.array([30e6, 100e6, 230e6, 231e6, 999e6, 1e9, 1.01e9, 3e9, 3.01e9, 6e9, 6.1e9, 29e6])
    lb, db = limits(f, "B")
    la, _ = limits(f, "A")
    exp_b = [30, 30, 30, 37, 37, 37, 50, 50, 54, 54, np.nan, np.nan]
    exp_a = [40, 40, 40, 47, 47, 47, 56, 56, 60, 60, np.nan, np.nan]
    assert np.allclose(lb, exp_b, equal_nan=True) and np.allclose(la, exp_a, equal_nan=True)
    assert list(db[:6]) == [10.0] * 6 and list(db[6:10]) == [3.0] * 4


def test_SP7_the_receiver_adds_the_harmonics_within_its_bandwidth():
    """SP-7: receiver_sum against a brute-force window sum (±RBW/2, 120 kHz below 1 GHz, 1 MHz above)."""
    from groundtruth.emi.radiation import receiver_sum
    rng = np.random.default_rng(1)
    f = np.sort(np.concatenate([np.arange(30e6, 31e6, 20e3), np.arange(1.0e9, 1.004e9, 150e3)]))
    p = rng.uniform(0.1, 1.0, len(f))
    got = receiver_sum(f, p)
    rbw = np.where(f < 1e9, 120e3, 1e6)
    ref = np.array([p[np.abs(f - fk) <= rbw[k] / 2 + 1e-6].sum() for k, fk in enumerate(f)])
    assert np.allclose(got, ref)


def test_SP8_the_emission_table_is_current_times_moment_through_the_field_formula():
    """SP-8: emission_arrays end to end for one harmonic: E = 2·η0k²·|m|·I/(4πr)·√(1+1/(kr)²) peak, read as RMS, in
    dBµV/m against the limit at the standard's distance."""
    from groundtruth.emi.radiation import emission_arrays
    f, i, m = np.array([150e6]), np.array([0.01]), 2e-6          # 10 mA on a 2 mm² loop
    a = emission_arrays(f, i, m, "B")
    k = 2 * math.pi * f[0] / C0
    e = 2 * ETA0 * k ** 2 * m * 0.01 / (4 * math.pi * 10) * math.sqrt(1 + 1 / (k * 10) ** 2) / math.sqrt(2)
    assert a["e_dbuv"][0] == pytest.approx(20 * math.log10(e / 1e-6), abs=0.02)
    assert a["margin_db"][0] == pytest.approx(30.0 - a["e_dbuv"][0])
    # at a distance of your own the limit moves by 20·log(d_std / r)
    b = emission_arrays(f, i, m, "B", distance_m=3.0)
    assert b["limit_dbuv"][0] == pytest.approx(30.0 + 20 * math.log10(10 / 3))


# ======================================================================================================= Converters
def test_CV1_the_pulse_of_a_buck_and_a_boost():
    """CV-1: the hot loop's pulse: a buck switches I_out with D = V_out/V_in; a boost its inductor current
    I_out·V_out/V_in, the high side (rectifier) conducting V_in/V_out."""
    from groundtruth.config.settings import ConverterSpec
    b = ConverterSpec("b", "VIN", "GND", topology="buck", i_out=3.0, v_in=12.0, v_out=3.3)
    assert b.pulse() == pytest.approx((3.0, 3.3 / 12.0))
    bo = ConverterSpec("o", "VOUT", "GND", topology="boost", i_out=1.0, v_in=5.0, v_out=20.0)
    assert bo.pulse() == pytest.approx((4.0, 0.25))


def test_CV2_the_series_lc_fit_recovers_l_and_c_from_two_reactances():
    """CV-2: loop_inductance: X(ω) = ωL − 1/(ωC) sampled at 30 and 100 MHz gives L and C back exactly."""
    from types import SimpleNamespace as NS
    from groundtruth.analysis.power import loop_inductance
    L, C = 2.4e-9, 47e-9
    def sol(f):
        w = 2 * math.pi * f
        return NS(f=f, Z_loop=complex(0.01, w * L - 1 / (w * C)))
    got_l, got_c = loop_inductance(sol(30e6), sol(100e6))
    assert got_l == pytest.approx(L, rel=1e-9) and got_c == pytest.approx(C, rel=1e-6)


def _rlc_ramp(L, C, R, V, tr, t_end, n=200000):
    """Series R-L-C driven by a voltage ramp 0 → V in tr: the capacitor's voltage (RK4)."""
    dt = t_end / n
    def vin(t):
        return V * min(max(t / tr, 0.0), 1.0)
    def d(t, y):
        q, i = y
        return np.array([i, (vin(t) - R * i - q / C) / L])
    y, t, vc = np.zeros(2), 0.0, np.empty(n)
    for k in range(n):
        k1 = d(t, y)
        k2 = d(t + dt / 2, y + dt / 2 * k1)
        k3 = d(t + dt / 2, y + dt / 2 * k2)
        k4 = d(t + dt, y + dt * k3)
        y = y + dt / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        t += dt
        vc[k] = y[0] / C
    return vc


@pytest.mark.parametrize("tr_cycles", [0.05, 0.3, 0.7])
def test_CV3_the_ringing_overshoot_matches_a_time_domain_integration(tr_cycles):
    """CV-3: stage_spectrum's ringing: f_r = 1/(2π√(LC)), Q = √(L/C)/R, and the overshoot V·exp(−πζ/√(1−ζ²))·|sinc(f_r
    tr)| against an RK4 integration of the series R-L-C driven by the edge (a ramp of tr)."""
    from groundtruth.analysis.power import stage_spectrum
    from groundtruth.config.settings import ConverterSpec
    L, C, r_damp, V = 2e-9, 600e-12, 0.12, 48.0
    f_r = 1 / (2 * math.pi * math.sqrt(L * C))
    tr = tr_cycles / f_r
    spec = ConverterSpec("s", "VIN", "GND", c_oss=C, r_damp=r_damp, rise_time=tr)
    f = np.array([f_r])
    _, ring = stage_spectrum(spec, f, np.ones(1), V, 10.0, L, 0.0, tr)
    assert ring["f"] == pytest.approx(f_r) and ring["q"] == pytest.approx(math.sqrt(L / C) / r_damp)
    vc = _rlc_ramp(L, C, r_damp, V, tr, tr + 6 / f_r)
    sim = vc.max() - V
    assert ring["overshoot_v"] == pytest.approx(sim, rel=0.03, abs=0.01 * V)


def _switching_cell(L, r, C, V, I, tr, f_sw, duty, cycles=12, n_per=40000, snub=None):
    """The hot loop of an ideal buck cell in the time domain: a stiff input V, the loop L + r into the switch node
    capacitance C (and an RC snubber), the node forced by the edges (the gated load current I as a trapezoid
    with the node's edges, the node voltage a trapezoid 0..V): KCL at the node, i_loop = I_pulse + C·dv/dt (+ the
    snubber's). Returns (t, loop current) over the last cycle; the harmonics by FFT."""
    T = 1 / f_sw
    n = cycles * n_per
    t = np.arange(n) * T / n_per
    ph = (t % T)
    up = np.clip(ph / tr, 0, 1)
    down = np.clip((ph - duty * T) / tr, 0, 1)
    shape = up - down                                   # 0..1 trapezoid
    v_node, i_pulse = V * shape, I * shape
    dt = T / n_per
    dv = np.gradient(v_node, dt)
    i_c = C * dv
    if snub is not None:
        cs, rs = snub
        # the snubber: dvs/dt = (v - vs)/(rs cs), i_s = (v - vs)/rs
        vs, i_s = np.zeros(n), np.zeros(n)
        for k in range(1, n):
            i_s[k - 1] = (v_node[k - 1] - vs[k - 1]) / rs
            vs[k] = vs[k - 1] + dt * i_s[k - 1] / cs
        i_s[-1] = (v_node[-1] - vs[-1]) / rs
        i_c = i_c + i_s
    i_drive = i_pulse + i_c                             # what the node draws from the loop (forced)
    # the loop L, r carries what the node draws, filtered by the loop's resonance with C: solve the linear cell in
    # the frequency domain of the periodic steady state instead of integrating (exact for the linear model)
    seg = slice(n - n_per, n)
    X = np.fft.rfft(i_drive[seg]) / n_per
    return X


def test_CV4_the_loop_current_carries_the_node_charge_edge_charge_factor():
    """CV-4: edge_charge_factor: the hot loop carries the load pulse plus the node's charge current C·dv/dt (and the
    snubber's); both are trapezoids with the same edges, so the sum is the pulse × |1 + Y_node·V/I| per harmonic.
    Checked against the FFT of the summed time-domain currents."""
    from groundtruth.analysis.power import edge_charge_factor
    f_sw, V, I, C, tr, duty = 500e3, 48.0, 10.0, 600e-12, 3e-9, 0.3
    for snub in (None, (2.2e-9, 1.5)):
        X = _switching_cell(2e-9, 0.05, C, V, I, tr, f_sw, duty, snub=snub)
        pulse = _fft_harmonics(f_sw, I, duty, tr, tr, 400)
        n = np.array([1, 3, 11, 51, 151, 301])         # away from the pulse's nulls (n·D integer)
        got = edge_charge_factor(n * f_sw, I, V, C, snub)
        ref = 2 * np.abs(X[n]) / pulse[n - 1]
        assert np.allclose(got, ref, rtol=0.02), (snub, got, ref)


def test_CV5_the_boost_ripple_harmonics_are_the_asymmetric_triangle_s():
    """CV-5: ripple_harmonics against the FFT of an asymmetric triangle (up for D·T, down for the rest, ΔI p-p)."""
    from groundtruth.analysis.power import ripple_harmonics
    f_sw, di, D = 300e3, 1.7, 0.62
    f, c = ripple_harmonics(f_sw, di, D, 30 * f_sw)
    N = 2 ** 16
    ph = np.arange(N) / N
    x = np.where(ph < D, ph / D, (1 - ph) / (1 - D)) * di
    X = 2 * np.abs(np.fft.rfft(x) / N)
    n = np.rint(f / f_sw).astype(int)
    assert np.allclose(c, X[n], rtol=1e-3, atol=di * 1e-6)


def test_CV6_the_lisn_networks_and_the_receiver_s_share():
    """CV-6: each LISN's impedance at the EUT terminal and the 50 Ω port's share, from the networks drawn in the
    standards: CISPR 16-1-2 50 Ω ∥ (50 µH + 5 Ω); CISPR 25 50 Ω ∥ 5 µH; MIL-STD-461G Figure 6 (50 µH, 8 µF + 5 Ω on the
    source side, 0.25 µF to the signal port with 1 kΩ across it, the receiver's 50 Ω in parallel; the impedance with
    the power input unterminated, 4.3.6 c). ISSUE-9: the 1 kΩ was left out (0.4 dB high)."""
    from groundtruth.emi.conducted import LISNS
    f = np.geomspace(10e3, 100e6, 20)
    w = 2 * np.pi * f
    def par(a, b):
        """Two impedances in parallel."""
        return a * b / (a + b)
    z, k = LISNS["cispr32"].impedance(f)
    assert np.allclose(z, par(50.0, 5.0 + 1j * w * 50e-6)) and np.allclose(k, 1.0)
    z, k = LISNS["cispr25"].impedance(f)
    assert np.allclose(z, par(50.0, 1j * w * 5e-6))
    z, k = LISNS["mil461"].impedance(f)
    port = par(50.0, 1e3)
    zm = port + 1 / (1j * w * 0.25e-6)
    assert np.allclose(z, par(zm, 1j * w * 50e-6 + 5.0 + 1 / (1j * w * 8e-6))) and np.allclose(k, port / zm)


def test_CV26_the_mil_std_461g_ce101_and_re102_limits_are_its_figures():
    """CV-26 / SP-25: MIL-STD-461G (11 Dec 2015) read from the figures. CE101-4 (aircraft): #1 above 28 V 110 dBµA
    30 Hz-1 kHz falling to 90 at 10 kHz, #2 100 → 80; CE101-1 (ships, DC): 95 to 2.6 kHz → 76 at 10 kHz, relaxed by
    20·log(I/3) from 3 to 185 A, 35 dB above. RE102-1..-4 at their breakpoints (dBµV/m, 1 m)."""
    from groundtruth.emi.conducted import Ce101, ce101_limit
    from groundtruth.emi.radiation import RE102_LIMITS, limits
    f = np.array([30.0, 500.0, 1e3, 2.6e3, 10e3])
    assert np.allclose(ce101_limit(f, Ce101("aircraft", 270.0)), [110, 110, 110, 110 - 20 * math.log10(2.6), 90])
    assert np.allclose(ce101_limit(f, Ce101("aircraft", 28.0)), [100, 100, 100, 100 - 20 * math.log10(2.6), 80])
    assert np.allclose(ce101_limit(f, Ce101("ships", 28.0, 2.0)), [95, 95, 95, 95, 76])
    assert np.allclose(ce101_limit(f, Ce101("ships", 28.0, 30.0)), np.array([95, 95, 95, 95, 76]) + 20)
    assert np.allclose(ce101_limit(f, Ce101("ships", 28.0, 300.0)), np.array([95, 95, 95, 95, 76]) + 35)
    fig = {"re102_ship_above_deck": [(10e3, 70), (100e6, 36), (18e9, 82)],
           "re102_ship_below_deck": [(10e3, 90), (100e6, 56), (18e9, 102)],
           "re102_submarine_external": [(10e3, 60), (2e6, 24), (100e6, 24), (18e9, 69)],
           "re102_submarine_internal": [(10e3, 88), (100e6, 50), (18e9, 95)],
           "re102_aircraft_external": [(2e6, 24), (100e6, 24), (18e9, 69)],
           "re102_helicopter": [(10e3, 60), (2e6, 24), (100e6, 24), (18e9, 69)],
           "re102_space": [(10e3, 60), (2e6, 24), (100e6, 24), (18e9, 69)],
           "re102_aircraft_internal": [(2e6, 34), (100e6, 34), (18e9, 79)],
           "re102_aircraft_internal_25m": [(2e6, 44), (100e6, 44), (18e9, 89)],
           "re102_ground_army": [(2e6, 24), (100e6, 24), (18e9, 69)],
           "re102_ground_navy_fixed": [(2e6, 44), (100e6, 44), (18e9, 89)]}
    for name, pts in fig.items():
        lim, dist = limits(np.array([p[0] for p in pts]), RE102_LIMITS[name])
        assert np.allclose(lim, [p[1] for p in pts]) and np.all(dist == 1.0), name


def test_CV7_the_conducted_limits_are_the_standards():
    """CV-7: CISPR 32 class A/B average (AC mains, Tables A.9 / A.10: 56→46 dBµV over 0.15-0.5 MHz linear in log f,
    46 to 5 MHz, 50 to 30 MHz; class A 66, then 60), CISPR 25 class 5 average and its class steps, MIL-STD-461G CE102
    (94 dBµV at 10 kHz falling 20 dB/decade to 60 at 500 kHz, flat to 10 MHz; +6 dB for 115 V)."""
    from groundtruth.emi.conducted import limit_line
    f = np.array([0.15e6, 0.3e6, 0.5e6, 1e6, 5e6, 5.01e6, 30e6])
    b = limit_line(f, "cispr32", "B")
    exp = [56.0, 56 - 10 * math.log10(2) / math.log10(0.5 / 0.15), 46, 46, 46, 50, 50]
    assert np.allclose(b, exp, atol=1e-6)
    assert np.allclose(limit_line(f, "cispr32", "A"), [66, 66, 60, 60, 60, 60, 60])
    c25 = limit_line(np.array([0.2e6, 1e6, 6e6, 27e6, 50e6, 100e6]), "cispr25", "5")
    assert np.allclose(c25, [50, 34, 33, 24, 24, 18])
    c25_1 = limit_line(np.array([0.2e6, 1e6, 6e6, 100e6]), "cispr25", "1")
    assert np.allclose(c25_1, [90, 66, 57, 42])
    m = limit_line(np.array([10e3, 100e3, 500e3, 10e6]), "mil461", "basic")
    assert np.allclose(m, [94, 94 - 34 * 1 / math.log10(50), 60, 60], atol=1e-6)
    assert np.allclose(limit_line(np.array([10e3]), "mil461", "115v"), [100.0])


def test_CV8_a_conducted_reading_is_line_current_through_the_lisn_s_port():
    """CV-8: conducted_arrays for one harmonic: V_port = |I|·|Z_LISN|·|50/Z_meas| peak, read as RMS dBµV."""
    from groundtruth.emi.conducted import conducted_arrays
    f, i = np.array([1e6]), np.array([1e-3])
    w = 2 * math.pi * 1e6
    z = 50 * (5 + 1j * w * 50e-6) / (55 + 1j * w * 50e-6)
    v = 1e-3 * abs(z) / math.sqrt(2)
    a = conducted_arrays(f, i, "cispr32", "B")
    assert a["v_dbuv"][0] == pytest.approx(20 * math.log10(v / 1e-6), abs=1e-6)
    assert a["margin_db"][0] == pytest.approx(46.0 - a["v_dbuv"][0])


def test_CV9_the_input_filter_advice_meets_the_missing_attenuation():
    """CV-9: input_filter_advice: f_c = min f·10^(−A/40) over the harmonics short of the margin, L·C = 1/(2πf_c)², L
    under 4.7 µH with the smallest standard C, damping R = √(L/C) with 4·C."""
    from groundtruth.analysis.power import input_filter_advice
    rows = [{"f": 500e3, "margin_db": -14.0}, {"f": 1e6, "margin_db": -6.0}, {"f": 2e6, "margin_db": 8.0}]
    adv = input_filter_advice(rows, 6.0)
    fc = min(500e3 * 10 ** (-20 / 40), 1e6 * 10 ** (-12 / 40))
    assert adv["f_c"] == pytest.approx(fc)
    assert 1 / (2 * math.pi * math.sqrt(adv["L"] * adv["C"])) == pytest.approx(fc)
    assert adv["L"] <= 4.7e-6 and adv["R_damp"] == pytest.approx(math.sqrt(adv["L"] / adv["C"]))
    assert adv["C_damp"] == pytest.approx(4 * adv["C"])


def test_CV10_the_common_mode_budget_is_linear_in_the_stray_capacitance():
    """CV-10: cm_estimate: harmonics of the switch-node trapezoid through C (I = 2πf·C·V_n), half in each LISN; the
    budget C·10^(margin/20) gives 0 dB, twice it −6.02 dB."""
    from types import SimpleNamespace as NS
    from groundtruth.analysis.power import cm_estimate
    from groundtruth.config.settings import ConverterSpec
    spec = ConverterSpec("s", "VIN", "GND", f_sw=400e3, rise_time=5e-9)
    res = NS(v_in=24.0, duty=0.4)
    out = cm_estimate(spec, res, "cispr32", "B")
    b = out["budget_pf"]
    spec.c_stray = b * 1e-12
    w = cm_estimate(spec, res, "cispr32", "B")["worst"]["margin_db"]
    assert w == pytest.approx(0.0, abs=0.05)
    spec.c_stray = 2 * b * 1e-12
    assert cm_estimate(spec, res, "cispr32", "B")["worst"]["margin_db"] == pytest.approx(-6.02, abs=0.05)


def _loss_reference(topology, rect_diode, d_sw, i_out, v_in, v_out, rds, rds_lo, f, tr, q_oss, vf, dcr):
    """Independent stage losses: the main switch conducts the switch duty D, the rectifier D' = 1 − D; the inductor
    carries I_out (buck) or I_out/D' (boost); the hard-switched edge ½·V·I_L·tr·f per hard edge (one with a
    synchronous rectifier, two with a diode); the output charge Q_oss·V·f (½ with a diode); a diode's D'·I_L·V_f."""
    boost = topology == "boost"
    i_l = i_out / (1 - d_sw) if boost else i_out
    v = v_out if boost else v_in
    main = {"cond": d_sw * i_l ** 2 * (rds_lo if boost else rds)}
    edges = 2 if rect_diode else 1
    main["edge"] = 0.5 * v * i_l * edges * tr * f
    main["qoss"] = (0.5 if rect_diode else 1.0) * q_oss * v * f
    rect = (1 - d_sw) * i_l * vf if rect_diode else (1 - d_sw) * i_l ** 2 * (rds if boost else rds_lo)
    return sum(main.values()), rect, i_l ** 2 * dcr, i_l


def _stage_parts_board():
    """A board with the parts a loss budget names: Q1 (main switch), Q2 (synchronous rectifier), D1 (rectifier
    diode), L1 on SW."""
    from boardgen import BoardGen
    g = BoardGen(layers=2, size=(30.0, 20.0))
    for ref, x, nets in (("Q1", 5.0, ("SW", "GND")), ("Q2", 10.0, ("VOUT", "SW")), ("D1", 15.0, ("VOUT", "SW")),
                         ("L1", 20.0, ("VIN", "SW"))):
        g.footprint(ref, "X", "Package_SO:Fake", (x, 10.0), [
            {"number": 1, "at": (0.0, -1.0), "net": nets[0], "size": (1.0, 1.0)},
            {"number": 2, "at": (0.0, 1.0), "net": nets[1], "size": (1.0, 1.0)}])
    g.plane("GND", "B.Cu")
    return g


@pytest.mark.parametrize("topology, rect_diode, v_in, v_out", [
    ("buck", False, 12.0, 3.3), ("buck", True, 24.0, 5.0),
    ("boost", False, 5.0, 20.0), ("boost", True, 12.0, 36.0), ("boost", False, 12.0, 24.0)])
def test_CV11_part_losses_match_an_independent_loss_budget(tmp_path, topology, rect_diode, v_in, v_out):
    """CV-11: thermal.part_losses for synchronous and diode bucks and boosts at duties other than ½, fed the duty
    exactly as power.prepare_converter gives it (ConverterSpec.pulse: the high side's conduction time)."""
    from dataclasses import replace
    from types import SimpleNamespace as NS
    from conftest import make
    from groundtruth.analysis.thermal import part_losses
    from groundtruth.config.settings import ConverterSpec
    board, sc, _ = make(tmp_path, _stage_parts_board(), "b", nets=())
    f, tr, q, rds, rlo, vf = 400e3, 5e-9, 20e-9, 0.02, 0.03, 0.4
    rect = "D1" if rect_diode else "Q2"
    if topology == "boost":
        spec = ConverterSpec("s", "VOUT", "GND", "SW", [rect], ["Q1"], topology="boost", f_sw=f, i_out=1.0,
                             v_in=v_in, v_out=v_out, rise_time=tr, q_oss=q, rds_on=rds, rds_on_low=rlo, diode_vf=vf,
                             dcr=0.01)
        d_sw = 1 - v_in / v_out
        main_ref, rect_ref = "Q1", rect
    else:
        spec = ConverterSpec("s", "VOUT", "GND", "SW", ["Q1"], [rect], topology="buck", f_sw=f, i_out=1.0,
                             v_in=v_in, v_out=v_out, rise_time=tr, q_oss=q, rds_on=rds, rds_on_low=rlo, diode_vf=vf,
                             dcr=0.01)
        d_sw = v_out / v_in
        main_ref, rect_ref = "Q1", rect
    if board.footprint(rect_ref) is None:
        pytest.skip(f"{rect_ref} not on the test board")
    _, duty = spec.pulse()                              # what Simulate hands part_losses (res.duty)
    rows, _ = part_losses(board, replace(sc.config, converters=[spec]),
                          [NS(net="s", duty=duty, v_in=v_in if topology == "buck" else v_out, dc=[])])
    got = {r["ref"]: r["power_w"] for r in rows}
    main, rect_w, _, _ = _loss_reference(topology, rect_diode, d_sw, 1.0, v_in, v_out, rds, rlo, f, tr, q, vf, 0.01)
    assert got.get(main_ref, 0.0) == pytest.approx(main, rel=1e-6), (topology, got)
    assert got.get(rect_ref, 0.0) == pytest.approx(rect_w, rel=1e-6), (topology, got)


# ================================================================================================ board-level checks
def test_SP9_a_simulated_route_s_emission_is_its_path_current_times_its_moment(tmp_path):
    """SP-9: end to end on a microstrip board: every emission row is the dipole field of its own m·I (one harmonic
    per 120 kHz window at 50 MHz), and the path's mean current at the worst harmonic is what an independent ladder of
    the reported stretches (with their copper and dielectric loss, as reported) gives from the driver's harmonic
    behind 25 Ω, loaded by 5 pF."""
    from boardgen import microstrip_board
    from conftest import SIG, make
    from groundtruth.analysis.simulate import simulate
    from groundtruth.emi.path import PathLine, PathLoad, PathSource
    board, sc, _ = make(tmp_path, microstrip_board(), "ms")
    res = simulate(board, sc.config).routes[0]
    assert not res.error, res.error
    for row in res.emissions[:40]:
        k, r = 2 * math.pi * row["f"] / C0, row["distance_m"]
        e = 2 * ETA0 * k ** 2 * row["m_m2"] / (4 * math.pi * r) * math.sqrt(1 + 1 / (k * r) ** 2)
        assert row["e_dbuv"] == pytest.approx(20 * math.log10(e / math.sqrt(2) / 1e-6), abs=0.01)
    p = res.path
    lines = [PathLine(r["z0"], r["eeff"], r["length_mm"] * 1e-3, width_m=r["width_mm"] * 1e-3,
                      thickness_m=r["thickness_mm"] * 1e-3, er=r["er"], tan_d=r["tan_d"])
             for r in p["elements"] if r["kind"] == "line"]
    assert len(lines) == len(p["elements"])
    amp = _fft_harmonics(SIG.freq, SIG.amplitude_v, 0.5, SIG.rise_time, SIG.rise_time, 200)
    n = round(p["f"] / SIG.freq)
    mean, i_in, _ = _ladder([p["f"]], [amp[n - 1]], lines, PathSource(25.0), PathLoad(5e-12), sections=800)
    assert p["i_mean_ma"] == pytest.approx(abs(mean[0]) * 1e3, rel=0.01)
    assert p["i_in_ma"] == pytest.approx(abs(i_in[0]) * 1e3, rel=0.01)


@pytest.mark.parametrize("distance, flagged", [(1.8, False), (2.3, True), (None, True)])
def test_SP10_the_layer_change_stitching_rule_flags_at_its_threshold(tmp_path, distance, flagged):
    """SP-10: layer_change_stitch: a GND via within stitch_distance_mm (2 mm) of the signal via satisfies it; beyond,
    or none, it is flagged."""
    from boardgen import layer_change_board
    from conftest import make
    from groundtruth.rules.checks import run_checks
    board, sc, _ = make(tmp_path, layer_change_board(distance), f"lc{distance}")
    issues = [i for i in run_checks(board, sc.config).issues if i.check == "layer_change_stitch"]
    assert bool(issues) == flagged


@pytest.mark.parametrize("distance, flagged", [(4.5, False), (6.0, True)])
def test_SP11_the_layer_change_decap_rule_flags_at_its_threshold(tmp_path, distance, flagged):
    """SP-11: layer_change_decap between GND and +3V3 reference planes: a capacitor between them within
    decap_distance_mm (5 mm) of the signal via satisfies it."""
    from boardgen import layer_change_board
    from conftest import make
    from groundtruth.rules.checks import run_checks
    board, sc, _ = make(tmp_path, layer_change_board(None, "+3V3", decap_distance=distance), f"dc{distance}")
    issues = [i for i in run_checks(board, sc.config).issues if i.check == "layer_change_decap"]
    assert bool(issues) == flagged


def test_CV12_capacitor_shares_at_f_sw_follow_their_impedances(tmp_path):
    """CV-12: two capacitors placed symmetrically (10 µF over the strip, 1 µF under it): at f_sw = 200 kHz the copper
    is the same for both and negligible against 1/ωC, so the loop current divides as the capacitors' own impedances
    (ESR + j(ω(ESL + mounting) − 1/ωC)) do."""
    from boardgen import hot_loop_board
    from test_power import SPEC, run
    from groundtruth.analysis.simulate import MOUNT_L
    caps = (("C1", "10u", "top", 2.0), ("C2", "1u", "bottom", 2.0))
    r, _, sc = run(tmp_path, hot_loop_board(20.0, caps=caps), "share")
    w = 2 * math.pi * SPEC.f_sw

    def z(ref):
        """A capacitor's impedance as mounted, from its model."""
        m = sc.config.model_for(ref)
        return complex(m.get("ESR"), w * (m.get("ESL") + MOUNT_L) - 1 / (w * m.capacitance()))
    z1, z2 = z("C1"), z("C2")
    share = {c["name"]: c["share_sw"] for c in r.caps}
    assert share["C1"] == pytest.approx(abs(z2 / (z1 + z2)), rel=0.02)
    assert share["C2"] == pytest.approx(abs(z1 / (z1 + z2)), rel=0.05)


@pytest.mark.parametrize("v_in, v_out", [(5.0, 20.0), (12.0, 36.0)])
def test_CV13_a_boost_s_input_current_is_its_energy_balance(tmp_path, v_in, v_out):
    """CV-13: input_estimate of a synchronous boost at D ≠ ½ with the duty Simulate gives it: V_in·I_in = V_out·I_out +
    the independent loss budget (CV-11's), with the inductor current I_out·V_out/V_in."""
    from types import SimpleNamespace as NS
    from conftest import make
    from groundtruth.analysis.power import input_estimate
    from groundtruth.config.settings import ConverterSpec
    board, sc, _ = make(tmp_path, _stage_parts_board(), "bal", nets=())
    f, tr, q, rds, rlo, dcr = 400e3, 5e-9, 20e-9, 0.02, 0.03, 0.01
    spec = ConverterSpec("s", "VOUT", "GND", "SW", ["Q2"], ["Q1"], topology="boost", f_sw=f, i_out=1.0, v_in=v_in,
                         v_out=v_out, rise_time=tr, q_oss=q, rds_on=rds, rds_on_low=rlo, dcr=dcr)
    _, duty = spec.pulse()
    est = input_estimate(board, sc.config, spec, NS(net="s", duty=duty, v_in=v_out))
    main, rect, ind, _ = _loss_reference("boost", False, 1 - v_in / v_out, 1.0, v_in, v_out, rds, rlo, f, tr, q,
                                         0.0, dcr)
    assert est["p_loss_w"] == pytest.approx(main + rect + ind, rel=1e-6)
    assert est["i_in_a"] == pytest.approx((v_out * 1.0 + main + rect + ind) / v_in, rel=1e-6)


def _mid_rail(tmp_path, name, loads, ret=False):
    """test_dcload's 40 mm, 4 mm pour fed at U1.1, U3 in the middle, U2 at the far end, simulated."""
    from test_dcload import board_with
    from groundtruth.analysis.simulate import simulate
    board, cfg = board_with(tmp_path, 40.0, loads, name, ret=ret, mid=True)
    res = simulate(board, cfg).dc_loads[0]
    assert not res.error, res.error
    return res


def test_DC1_drops_are_reciprocal_and_the_loss_is_tellegen_s(tmp_path):
    """DC-1: two unequal load patterns on the same rail give the transfer-resistance matrix R (drop_k = Σ R_kj·I_j):
    it is symmetric (reciprocity of a passive resistive network), R_far,far − R_far,mid is the pour between them,
    ΔL/(σ·w·t), and in each run the copper loss is Σ I_k·drop_k (Tellegen), r_mohm = loss / I²."""
    from groundtruth.solver.peec import SIGMA_CU
    runs = []
    for k, (i3, i2) in enumerate(((1.0, 3.0), (3.0, 1.0))):
        res = _mid_rail(tmp_path, f"rec{k}", [("U3.1", i3), ("U2.1", i2)])
        d = {r["pin"]: r["supply_mv"] * 1e-3 for r in res.loads}
        runs.append(((i3, i2), (d["U3.1"], d["U2.1"])))
        loss = res.dc[0]["loss_w"]
        assert loss == pytest.approx(i3 * d["U3.1"] + i2 * d["U2.1"], rel=1e-4)
        assert res.dc[0]["r_mohm"] * 1e-3 * (i3 + i2) ** 2 == pytest.approx(loss, rel=1e-9)
    I = np.array([runs[0][0], runs[1][0]]).T             # columns: the runs, rows: U3, U2
    D = np.array([runs[0][1], runs[1][1]]).T
    R = D @ np.linalg.inv(I)
    assert R[0, 1] == pytest.approx(R[1, 0], rel=1e-3)
    per = 0.020 / (SIGMA_CU * 4e-3 * 35e-6)              # 20 mm of the 4 mm × 35 µm pour
    assert R[1, 1] - R[1, 0] == pytest.approx(per, rel=0.07)


def test_DC2_with_returns_the_loss_counts_both_sides(tmp_path):
    """DC-2: with the ground return modelled, the copper loss is Σ I_k·(supply_k + return_k) (Tellegen over both
    nets), and each load's total drop is the sum of its two sides."""
    res = _mid_rail(tmp_path, "both", [("U3.1", 1.0), ("U2.1", 2.0)], ret=True)
    tot = sum(r["current_a"] * (r["supply_mv"] + r["return_mv"]) * 1e-3 for r in res.loads)
    assert res.dc[0]["loss_w"] == pytest.approx(tot, rel=1e-4)
    assert all(r["drop_mv"] == pytest.approx(r["supply_mv"] + r["return_mv"]) for r in res.loads)


def test_DC3_a_series_part_on_the_rail_adds_its_i_r_and_its_loss(tmp_path):
    """DC-3: the rail runs through R1 between two pours (split_strip_board): raising R1 from 1 mΩ to 51 mΩ adds
    I·ΔR to the load's drop, and I²·R1 is R1's loss share."""
    from boardgen import split_strip_board
    from conftest import make
    from groundtruth.analysis.simulate import simulate
    from groundtruth.components.models import ComponentModel
    from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile
    out = {}
    for r1 in (1e-3, 51e-3):
        board, sc, _ = make(tmp_path, split_strip_board(), f"ser{int(r1 * 1e3)}", nets=())
        sc.set_component("R1", ComponentModel("resistor", {"R": r1}))
        sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 2.0)])])
        sc.save()
        res = simulate(board, SettingsFile.load(sc.path).config).dc_loads[0]
        assert not res.error, res.error
        out[r1] = res
    d = {r1: res.loads[0]["supply_mv"] * 1e-3 for r1, res in out.items()}
    assert d[51e-3] - d[1e-3] == pytest.approx(2.0 * 0.050, rel=0.01)
    part = next(p for p in out[51e-3].dc[0]["parts"] if p["name"] == "R1")
    assert part["r_mohm"] * 1e-3 * 4.0 == pytest.approx(4.0 * 0.051, rel=0.01)


def test_DC4_a_pin_named_by_two_loads_carries_both(tmp_path):
    """DC-4: two loads on the same pin are one load with both currents: pin_currents already sums them; the
    injections must too."""
    two = _mid_rail(tmp_path, "dup", [("U2.1", 1.0), ("U2.1", 2.0)])
    one = _mid_rail(tmp_path, "one", [("U2.1", 3.0)])
    assert two.loads[0]["supply_mv"] == pytest.approx(one.loads[0]["supply_mv"], rel=1e-6)
    assert two.dc[0]["loss_w"] == pytest.approx(one.dc[0]["loss_w"], rel=1e-6)


def test_DC5_the_via_capacity_is_ipc_2221_s_internal_conductor_formula():
    """DC-5: I = 0.024·ΔT^0.44·A^0.725 (A in mil², the barrel's wall π·(d + t)·t for a finished hole d)."""
    from groundtruth.analysis.ipc import via_capacity
    d, t = 0.3, 0.025
    a_mil2 = math.pi * (d + t) * t / 0.0254 ** 2
    assert via_capacity(d, 25.0, 10.0) == pytest.approx(0.024 * 10 ** 0.44 * a_mil2 ** 0.725, rel=1e-12)


def test_DC7_pin_joints_and_contacts_add_their_i_r_to_each_load_s_drop(tmp_path):
    """DC-7: a board default of 1 mΩ contact + 0.5 mΩ joint per pin: the source pin carries the rail's total, each
    load pin its own current, so a load's supply drop grows by I_total·1.5 mΩ + I_k·1.5 mΩ; the copper is unchanged."""
    from dataclasses import replace
    from test_dcload import board_with
    from groundtruth.analysis.simulate import simulate
    board, cfg = board_with(tmp_path, 40.0, [("U3.1", 1.0), ("U2.1", 2.0)], "pins", mid=True)
    base = simulate(board, cfg).dc_loads[0]
    pinned = simulate(board, replace(cfg, tht={"contact": 1e-3, "joint": 0.5e-3})).dc_loads[0]
    for b, p in zip(base.loads, pinned.loads):
        assert p["supply_mv"] - b["supply_mv"] == pytest.approx((3.0 * 1.5e-3 + b["current_a"] * 1.5e-3) * 1e3,
                                                                rel=1e-6)
    assert pinned.dc[0]["loss_w"] == pytest.approx(base.dc[0]["loss_w"], rel=1e-9)


def test_CV14_the_loop_s_crossover_and_margins_are_read_where_t_crosses(tmp_path):
    """CV-14: with 8 mΩ of output ESR and 15 mΩ of DCR, the loop gain is Ridley's (written out, with the ESR zero in
    Y_o), and f_c, PM and GM equal a root search on that T (|T| = 1, ∠T = −180°), not only the 2000-point grid's."""
    from scipy.optimize import brentq
    from test_loop import C, GM, I_OUT, K, L, R_SENSE, V_IN, V_OUT, F_SW, _setup
    from groundtruth.analysis.loop import analyse_loop
    b, cfg, spec = _setup(tmp_path, "esr", **{"Slope Compensation": "20mV/us"})
    cfg.components["C2"].model.params.update(ESR=8e-3)
    cfg.components["L1"].model.params.update(R=15e-3)
    res = analyse_loop(b, cfg, spec)
    assert not res.error, res.error

    def T(f):
        """Ridley's buck in peak current mode with an ESR (Y_o exact), the gm compensator, the 25k/125k divider."""
        s = 2j * np.pi * f
        r, ri, ts, d = V_OUT / I_OUT, R_SENSE / K, 1 / F_SW, V_OUT / V_IN
        mc = 1 + 20e3 / (R_SENSE * (V_IN - V_OUT) / L)
        q = mc * (1 - d) - 0.5
        yo = s * C / (1 + s * C * 8e-3) + 1 / r
        wn, qp = math.pi * F_SW, 1 / (math.pi * q)
        gvc = 1 / (yo + ts * q / L) / ri / (1 + s / (wn * qp) + s * s / wn ** 2)
        zc = 1 / (1 / (2.2e3 + 1 / (s * 22e-9)) + s * 22e-12)
        return GM * zc * 0.2 * gvc
    assert np.allclose(res.response.h, T(res.response.f), rtol=1e-9)
    fc = brentq(lambda x: abs(T(x)) - 1, 1e3, 2e5)
    def ph(x):
        """T's phase at x (°), unwrapped from 10 Hz up."""
        return math.degrees(np.unwrap(np.angle(T(np.geomspace(10.0, x, 4000))))[-1])
    assert res.fc == pytest.approx(fc, rel=1e-3)
    assert res.pm == pytest.approx(180 + ph(fc), abs=0.2)
    if res.f180 is not None:
        f180 = brentq(lambda x: ph(x) + 180, fc, 5 * F_SW)
        assert res.f180 == pytest.approx(f180, rel=2e-3)
        assert res.gm_db == pytest.approx(-20 * math.log10(abs(T(f180))), abs=0.05)


def test_CV15_a_boost_known_by_its_duty_alone_draws_its_inductor_current(tmp_path):
    """CV-15: a boost 12 V → ? (V_out unknown) with its duty given, the high side conducting 1/3: the inductor carries
    I_out/(1/3) = 3 A; without an energy balance (no V_out) the ground return still splits by Kirchhoff: the load
    returns 1 A, the low side 2 A, and J1 takes all 3 A (as with V_out = 36 V given)."""
    from boardgen import boost_board
    from conftest import make
    from test_power import run
    from groundtruth.analysis.detect import detect_converters
    board, sc, _ = make(tmp_path, boost_board(out_connector=True), "bd", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out, spec.duty = 300e3, 1.0, 12.0, None, 1 / 3
    spec.input_connector, spec.output_connector = ["J1.1", "J1.2"], ["J2.1", "J2.2"]
    assert spec.pulse() == pytest.approx((3.0, 1 / 3))
    r, _, _ = run(tmp_path, boost_board(out_connector=True), "bd2", spec=spec)
    gnd = next(p for p in r.dc if p["name"].startswith("ground"))

    def via_current(x, y):
        """The current through the vias at (x, y)."""
        return sum(v["current_a"] for v in gnd["vias"] if math.dist((v["x"], v["y"]), (x, y)) < 0.5)
    assert gnd["current_a"] == pytest.approx(3.0, rel=0.01)
    assert via_current(34.5, 9.5) == pytest.approx(1.0, rel=0.01)
    assert via_current(19.5, 17.5) == pytest.approx(2.0, rel=0.01)
    assert via_current(3.0, 9.5) == pytest.approx(3.0, rel=0.01)


def test_SP12_a_given_line_current_is_the_fundamental_s_peak(tmp_path):
    """SP-12 (ISSUE-6): a net's current_a is the line's peak current at the fundamental (README): the emission row at
    f0 carries exactly it, the harmonics keep the trapezoid's shape relative to it."""
    from dataclasses import replace
    from boardgen import microstrip_board
    from conftest import SIG, make
    from groundtruth.analysis.simulate import simulate
    from groundtruth.emi.spectrum import trapezoid_harmonics
    board, sc, _ = make(tmp_path, microstrip_board(), "ia", nets=(replace(SIG, current_a=0.01),))
    res = simulate(board, sc.config).routes[0]
    rows = {round(r["f"]): r["current_a"] for r in res.emissions}
    assert rows[round(SIG.freq)] == pytest.approx(0.01, rel=1e-9)
    f, a = trapezoid_harmonics(SIG.freq, SIG.rise_time, SIG.amplitude_v, SIG.duty)
    k = int(np.argmin(abs(f - 5 * SIG.freq)))
    assert rows[round(5 * SIG.freq)] == pytest.approx(0.01 * a[k] / a[0], rel=1e-9)


def test_CV16_a_boost_s_filter_sits_at_its_input_voltage_and_current(monkeypatch):
    """CV-16 (ISSUE-2): the fix advisor's input side: a buck's V_in and D·I_out; a boost's own V_in and its inductor
    current (the result's v_in is the switched V_out). Middlebrook's |Z_in| = V_in²/P_in."""
    from types import SimpleNamespace as NS
    from groundtruth.analysis import fix_advisor
    from groundtruth.config.settings import ConverterSpec
    buck = ConverterSpec("b", "VIN", "GND", topology="buck", i_out=3.0, v_in=12.0, v_out=3.0)
    boost = ConverterSpec("o", "VOUT", "GND", topology="boost", i_out=1.0, v_in=12.0, v_out=36.0)
    rb = NS(v_in=12.0, pulse_a=3.0, duty=0.25)
    ro = NS(v_in=36.0, pulse_a=3.0, duty=1 / 3)                 # what prepare_converter gives a boost
    assert fix_advisor.input_side(buck, rb) == pytest.approx((12.0, 0.75))
    assert fix_advisor.input_side(boost, ro) == pytest.approx((12.0, 3.0))
    boost.v_in = None                                           # V_in from V_out and the rectifier's share
    assert fix_advisor.input_side(boost, ro) == pytest.approx((12.0, 3.0))
    f = np.array([1e3, 1e4])
    flat = (np.ones(2), np.ones(2) * 0.5, np.zeros(2), np.full(2, 1e9), np.full(2, 1e9))
    monkeypatch.setattr(fix_advisor, "_filter_parts", lambda setup, f, flt: flat)
    mb = fix_advisor.middlebrook(NS(res=ro, spec=boost), {"L": 1e-6, "C": 1e-6})
    assert mb["z_in"] == pytest.approx(12.0 ** 2 / (12.0 * 3.0))
    assert f.size == 2


@pytest.mark.parametrize("layer, width", [("In1.Cu", 0.15), ("In1.Cu", 0.6), ("F.Cu", 0.2), ("F.Cu", 0.12)])
def test_SP14_a_stretch_s_line_is_its_stackup_s_cross_section(tmp_path, layer, width):
    """SP-14 (ISSUE-5): simulate._line_of on an asymmetric 4-layer stackup (0.1 mm, 0.5 mm, 0.2 mm dielectrics,
    35 µm foils, εr 4.5): an In1 strip between F.Cu and In2 (0.1 mm to one, 0.5 mm to the other: an offset stripline
    with the nearer plane at 0.16 of the spacing, where the combination reads up to ~7 % high), an F.Cu strip over
    In1 (a microstrip 0.1 mm up, not the 0.135 mm between the foils' centres), against the 2D field solver on the
    same geometry (planes as foils, no solder mask). Before: ~+40 % (inner, centred IPC-2141 on 0.67 mm) and ~+10 %."""
    from boardgen import BoardGen
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.route import RouteSegment
    from groundtruth.analysis.simulate import _line_of
    from groundtruth.solver.xsection import Rect, Section, Slab, solve
    g = BoardGen(layers=4, size=(20.0, 10.0), dielectrics=[0.1, 0.5, 0.2])
    board = load_board(g.write(tmp_path / "stack.kicad_pcb"))
    st = board.stackup
    seg = RouteSegment((2.0, 5.0), (18.0, 5.0), layer, width, "track", "", 0, "SIG")
    z, e = _line_of(board, [seg], "In1.Cu")
    c = {x.name: x for x in st.copper}
    W, t, er = 8e-3, 35e-6, st.epsilon_between("F.Cu", "In2.Cu")
    w = width * 1e-3

    def foil(name, x0, x1, cid):
        """A copper foil at its stackup height (y up, the top surface at 0), m."""
        zc, th = c[name].z * 1e-3, c[name].thickness * 1e-3
        return Rect(x0, x1, -(zc + th / 2), -(zc - th / 2), cid)
    if layer == "In1.Cu":
        rects = [foil("F.Cu", -W, W, 0), foil("In2.Cu", -W, W, 0), foil("In1.Cu", -w / 2, w / 2, 1)]
        y_lo, y_hi = -(c["In2.Cu"].z + 0.0175) * 1e-3, -(c["F.Cu"].z - 0.0175) * 1e-3
        sec = Section(rects, [Slab(y_lo, y_hi, er)], (-W, W, y_lo, y_hi))
    else:
        rects = [foil("In1.Cu", -W, W, 0), foil("F.Cu", -w / 2, w / 2, 1)]
        top_cu = -(c["F.Cu"].z + 0.0175) * 1e-3                     # the top dielectric's upper face
        y_lo = -(c["In1.Cu"].z + 0.0175) * 1e-3
        sec = Section(rects, [Slab(-(c["In1.Cu"].z - 0.0175) * 1e-3, top_cu, er)], (-W, W, y_lo, top_cu + 3e-3))
    r = solve(sec)
    tol = 0.07 if layer == "In1.Cu" else 0.03
    assert z == pytest.approx(r.z0(), rel=tol), (layer, width, z, r.z0())
    assert e == pytest.approx(r.eeff(), rel=0.03)
    assert t > 0


# ===================================================================================== Phase 7: adversarial, invariants
@pytest.mark.parametrize("key, value", [("freq", 0), ("freq", -50e6), ("rise_time", 0), ("fall_time", 0),
                                        ("duty", 0), ("duty", 1.2), ("amplitude_v", -3.3), ("current_a", 0),
                                        ("load_pf", -1), ("driver_ohms", -5), ("load_ohms", 0), ("rise_time", 15e-9)])
def test_SP13_a_net_s_impossible_signal_is_refused_by_the_settings(tmp_path, key, value):
    """SP-13 (ISSUE-8): a net's signal must be one: f > 0, edges > 0 and within the high and low times (50 MHz, 50 %:
    10 ns each), 0 < duty < 1, amplitude > 0, a given current > 0, no negative source or load. Before, Simulate
    crashed on f = 0, tr = 0 or D = 0 and gave nonsense margins (−1.7 dB at D = 1.2, +301 dB at −3.3 V)."""
    from boardgen import microstrip_board
    from conftest import make
    from groundtruth.config.settings import ConfigError, SettingsFile
    _, sc, _ = make(tmp_path, microstrip_board(), "bad")
    sc.doc["nets"][0][key] = value
    sc.save()
    with pytest.raises(ConfigError) as e:
        SettingsFile.load(sc.path)
    assert any(key.split("_")[0] in m for m in e.value.messages), e.value.messages


@pytest.mark.parametrize("topology, v_in, v_out", [("buck", 5.0, 12.0), ("buck", 12.0, 12.0), ("boost", 12.0, 5.0)])
def test_CV17_a_converter_whose_voltages_contradict_its_topology_is_refused(tmp_path, topology, v_in, v_out):
    """CV-17 (ISSUE-8): a buck steps down, a boost up: given both voltages the wrong way round, the stage would be
    solved at a made-up duty of ½ (and a buck's input current would exceed its output current)."""
    from groundtruth.config.settings import ConfigError, config_from_data
    data = {"converters": [{"name": "s", "rail": "VIN", "ground": "GND", "switch": ["U1"], "topology": topology,
                            "v_in": v_in, "v_out": v_out}]}
    with pytest.raises(ConfigError) as e:
        config_from_data(data)
    assert any("v_out" in m for m in e.value.messages)


def _random_path(rng):
    """A random passive path: lines, series R/L/C/ferrite parts and shunts in any order."""
    from groundtruth.components.models import ComponentModel
    from groundtruth.emi.path import PathLine, PathLoad, PathPart, PathShunt, PathSource
    els = []
    for _ in range(rng.integers(1, 6)):
        k = rng.integers(0, 4)
        if k == 0 or not els:
            els.append(PathLine(rng.uniform(30, 90), rng.uniform(1.5, 4.5), rng.uniform(1e-3, 0.1)))
        elif k == 1:
            m = ComponentModel("resistor", {"R": rng.uniform(0, 500)})
            els.append(PathPart("R", m.impedance, 1e-3))
        elif k == 2:
            m = ComponentModel("cap", {"C": 10 ** rng.uniform(-12, -6), "ESR": rng.uniform(0, 0.1),
                                       "ESL": rng.uniform(0, 1e-9)})
            els.append(PathShunt("C", m.impedance) if rng.random() < 0.5 else PathPart("C", m.impedance, 1e-3))
        else:
            m = ComponentModel("ferrite", {"R": rng.uniform(0.01, 1), "L": 10 ** rng.uniform(-9, -5)})
            els.append(PathPart("L", m.impedance, 1.6e-3))
    src = PathSource(rng.uniform(5, 100), rng.uniform(0, 5e-9), rng.uniform(0, 10e-12))
    load = PathLoad(rng.uniform(1e-12, 20e-12), rng.uniform(20, 1e4) if rng.random() < 0.3 else None)
    return els, src, load


def test_P1_a_passive_path_takes_power_and_matches_the_ladder_on_random_circuits():
    """P-1: on 40 random passive paths the source delivers non-negative power (Re V·I* ≥ 0 at the path's input) and
    the mean current matches the independent ladder (SP-3) everywhere."""
    from groundtruth.emi.path import path_currents
    rng = np.random.default_rng(7)
    f = np.geomspace(1e6, 1e9, 9)
    for _ in range(40):
        els, src, load = _random_path(rng)
        pr = path_currents(f, np.ones(len(f)), els, src, load)
        w = 2 * np.pi * f
        zs = src.r + 1j * w * src.l
        v_node = (1.0 - zs * pr.i_in) / (1 + 1j * w * src.c * zs)      # Vs = v + Zs·(I_in + jωC·v) at the output
        assert np.all((v_node * np.conj(pr.i_in)).real >= -1e-12)
        mean, _, _ = _ladder(f, np.ones(len(f)), els, src, load, sections=300)
        scale = np.maximum(np.abs(mean), 1e-3 * np.max(np.abs(mean)))
        assert np.max(np.abs(pr.i_mean - mean) / scale) < 0.01


def test_P2_halving_the_driver_s_swing_lowers_every_emission_by_6_dB(tmp_path):
    """P-2: the estimate is linear in the line current: half the amplitude, −6.02 dB at every harmonic."""
    from dataclasses import replace
    from boardgen import microstrip_board
    from conftest import SIG, make
    from groundtruth.analysis.simulate import simulate
    out = []
    for k, amp in enumerate((3.3, 1.65)):
        board, sc, _ = make(tmp_path, microstrip_board(), f"lin{k}", nets=(replace(SIG, amplitude_v=amp),))
        out.append({round(r["f"]): r["e_dbuv"] for r in simulate(board, sc.config).routes[0].emissions})
    assert all(out[0][f] - out[1][f] == pytest.approx(20 * math.log10(2), abs=1e-6) for f in out[0])


def test_P3_dc_loads_scale_linearly_and_do_not_depend_on_their_order(tmp_path):
    """P-3: doubling every load doubles every drop and quadruples the loss; listing the loads in another order changes
    nothing; the same settings solved twice give the same numbers."""
    a = _mid_rail(tmp_path, "a", [("U3.1", 1.0), ("U2.1", 2.0)], ret=True)
    b = _mid_rail(tmp_path, "b", [("U3.1", 2.0), ("U2.1", 4.0)], ret=True)
    c = _mid_rail(tmp_path, "c", [("U2.1", 2.0), ("U3.1", 1.0)], ret=True)
    d = _mid_rail(tmp_path, "d", [("U3.1", 1.0), ("U2.1", 2.0)], ret=True)
    da = {r["pin"]: r["drop_mv"] for r in a.loads}
    assert all(r["drop_mv"] == pytest.approx(2 * da[r["pin"]], rel=1e-6) for r in b.loads)
    assert b.dc[0]["loss_w"] == pytest.approx(4 * a.dc[0]["loss_w"], rel=1e-6)
    assert all(r["drop_mv"] == pytest.approx(da[r["pin"]], rel=1e-6) for r in c.loads)
    assert [r["drop_mv"] for r in d.loads] == [r["drop_mv"] for r in a.loads]


def test_P4_losses_are_positive_and_the_energy_balance_holds_at_random_operating_points(tmp_path):
    """P-4: 60 random synchronous and diode bucks and boosts: every loss ≥ 0, P_in = P_out + P_loss, the efficiency in
    (0, 1], the input current above the lossless one, and each loss equal to the independent budget (CV-11's)."""
    from types import SimpleNamespace as NS
    from conftest import make
    from groundtruth.analysis.power import input_estimate
    from groundtruth.config.settings import ConverterSpec
    board, sc, _ = make(tmp_path, _stage_parts_board(), "p4", nets=())
    rng = np.random.default_rng(11)
    for _ in range(60):
        topo = "boost" if rng.random() < 0.5 else "buck"
        diode = rng.random() < 0.4
        v_lo, v_hi = rng.uniform(1, 30), 0.0
        v_hi = v_lo * rng.uniform(1.1, 6)
        v_in, v_out = (v_lo, v_hi) if topo == "boost" else (v_hi, v_lo)
        f, tr, q, i = rng.uniform(50e3, 3e6), rng.uniform(1e-9, 30e-9), rng.uniform(1e-9, 100e-9), rng.uniform(0.1, 20)
        rds, rlo, vf, dcr = rng.uniform(1e-3, 0.1), rng.uniform(1e-3, 0.1), rng.uniform(0.2, 0.8), rng.uniform(0, 0.05)
        rect = "D1" if diode else "Q2"
        hs, ls = ([rect], ["Q1"]) if topo == "boost" else (["Q1"], [rect])
        spec = ConverterSpec("s", "VOUT" if topo == "boost" else "VIN", "GND", "SW", hs, ls, topology=topo, f_sw=f,
                             i_out=i, v_in=v_in, v_out=v_out, rise_time=tr, q_oss=q, rds_on=rds, rds_on_low=rlo,
                             diode_vf=vf, dcr=dcr)
        _, duty = spec.pulse()
        est = input_estimate(board, sc.config, spec, NS(net="s", duty=duty, v_in=v_out if topo == "boost" else v_in))
        assert all(r["power_w"] >= 0 for r in est["losses"])
        d_sw = 1 - v_in / v_out if topo == "boost" else v_out / v_in
        main, rect_w, ind, _ = _loss_reference(topo, diode, d_sw, i, v_in, v_out, rds, rlo, f, tr, q, vf, dcr)
        assert est["p_loss_w"] == pytest.approx(main + rect_w + ind, rel=1e-9)
        assert est["i_in_a"] * v_in == pytest.approx(v_out * i + est["p_loss_w"], rel=1e-9)
        assert 0 < est["efficiency"] <= 1 and est["i_in_a"] >= est["i_in_ideal_a"]


def test_DC6_a_load_off_the_rail_s_copper_is_an_error_not_a_crash(tmp_path):
    """DC-6: a load pin on a net the source's copper does not reach: the rail gets an error saying why."""
    from test_dcload import board_with
    from groundtruth.analysis.simulate import simulate
    board, cfg = board_with(tmp_path, 20.0, [("U2.2", 1.0)], "off")       # U2.2 is a GND pad
    res = simulate(board, cfg).dc_loads[0]
    assert res.error and "rail" in res.error


@pytest.mark.parametrize("layer", ["In1.Cu", "B.Cu"])
def test_SP26_the_path_s_line_is_the_field_solved_cut_of_the_board(tmp_path, layer):
    """SP-26 (ISSUE-5, round 3): with the board's line cache, a stretch's Z0 and εeff are the 2D field solution of
    its own cut: an offset stripline on In1 (0.1 mm to the F.Cu pour, 0.5 mm to In2: the closed form read ~5 % high)
    and a microstrip on B.Cu under a 20 µm solder mask (εr 3.3; the closed form leaves it out). Against a Section
    built here from the stackup's numbers, within 1 %."""
    from boardgen import BoardGen
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.route import RouteSegment
    from groundtruth.analysis.si import SectionCache
    from groundtruth.analysis.simulate import _line_of
    from groundtruth.geometry.copper import CopperMap
    from groundtruth.solver.xsection import Rect, Section, Slab, solve
    g = BoardGen(layers=4, size=(30.0, 20.0), dielectrics=[0.1, 0.5, 0.2], mask=0.02)
    g.plane("GND", "F.Cu")
    g.plane("GND", "In2.Cu")
    g.track((5.0, 10.0), (25.0, 10.0), "SIG", layer, width=0.15)
    board = load_board(g.write(tmp_path / "cut.kicad_pcb"))
    seg = RouteSegment((5.0, 10.0), (25.0, 10.0), layer, 0.15, "track", "", 0, "SIG")
    z, e = _line_of(board, [seg], "In2.Cu", SectionCache(board, CopperMap(board)))
    z_closed, _ = _line_of(board, [seg], "In2.Cu")
    st = board.stackup
    c = {x.name: (x.z * 1e-3, x.thickness * 1e-3) for x in st.copper}
    W, w = 8e-3, 0.15e-3

    def foil(name, x0, x1, cid):
        """A foil at its height (y up, the top surface at 0)."""
        zc, th = c[name]
        return Rect(x0, x1, -(zc + th / 2), -(zc - th / 2), cid)
    er = st.dielectrics[0].epsilon_r
    # the laminate between the outer foils' inner faces (an outer track stands on it); the mask on the laminate
    # beside the bottom track, m_t thick (as analysis/si.SectionCache lays it: not over the track's top)
    bot = -(c["B.Cu"][0] - c["B.Cu"][1] / 2)
    top = -(c["F.Cu"][0] + c["F.Cu"][1] / 2)
    slabs = [Slab(bot, top, er)]
    m_t, m_er = st.mask_bottom.thickness * 1e-3, st.mask_bottom.epsilon_r
    slabs.append(Slab(bot - m_t, bot, m_er))
    if layer == "In1.Cu":
        rects = [foil("F.Cu", -W, W, 0), foil("In2.Cu", -W, W, 0), foil("In1.Cu", -w / 2, w / 2, 1)]
        box = (-W, W, bot - 1e-3, top + 1e-3)
    else:
        rects = [foil("In2.Cu", -W, W, 0), foil("B.Cu", -w / 2, w / 2, 1)]
        box = (-W, W, bot - 4e-3, top + 1e-3)
    ref = solve(Section(rects, slabs, box))
    assert z == pytest.approx(ref.z0(), rel=0.01) and e == pytest.approx(ref.eeff(), rel=0.01), (layer, z, ref.z0())
    assert abs(z_closed / z - 1) > 0.01                # what the cut changes against the closed forms


def test_SP26_a_stretch_without_a_cut_at_its_middle_takes_one_further_along(tmp_path):
    """SP-26 (ISSUE-5's remainder, 2026-10-18): the longest track widens into a pour of its own net at its middle, so
    no cut can be made there; _line_of takes the cut a quarter along instead (what refboard's U301- needed)
    and reads the same line as the board without the pour, within 1 %, not the closed forms."""
    from boardgen import BoardGen
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.route import RouteSegment
    from groundtruth.analysis.si import SectionCache
    from groundtruth.analysis.simulate import _line_of
    from groundtruth.geometry.copper import CopperMap
    zs = {}
    for pour in (False, True):
        g = BoardGen(layers=4, size=(30.0, 20.0), dielectrics=[0.1, 0.5, 0.2], mask=0.02)
        g.plane("GND", "F.Cu")
        g.plane("GND", "In2.Cu")
        g.track((5.0, 10.0), (25.0, 10.0), "SIG", "B.Cu", width=0.15)
        if pour:
            g.zone("SIG", "B.Cu", [(13.0, 8.0), (17.0, 8.0), (17.0, 12.0), (13.0, 12.0)])
        board = load_board(g.write(tmp_path / f"cut_{pour}.kicad_pcb"))
        seg = RouteSegment((5.0, 10.0), (25.0, 10.0), "B.Cu", 0.15, "track", "", 0, "SIG")
        cache = SectionCache(board, CopperMap(board))
        if pour:
            assert cache.solve_at((15.0, 10.0), (1.0, 0.0), "B.Cu", ["SIG"], 0.15) is None   # no cut at the middle
        zs[pour] = _line_of(board, [seg], "In2.Cu", cache)
        z_closed, _ = _line_of(board, [seg], "In2.Cu")
    assert zs[True][0] == pytest.approx(zs[False][0], rel=0.01)
    assert zs[True][1] == pytest.approx(zs[False][1], rel=0.01)
    assert abs(z_closed / zs[True][0] - 1) > 0.01      # not the closed forms


@pytest.mark.parametrize("d, y", [(1.0, 12.0), (1.0, 12.4), (0.5, 12.1), (0.35, 12.0)])
def test_SP27_a_strip_narrower_than_the_pitch_keeps_its_copper_width(d, y):
    """SP-27: a 0.35 mm wide copper strip along x, rasterised on pitches 1-3 x its width: the x strips of each column
    together carry its width (to one sub-grid pitch), centred where the copper is."""
    from shapely.geometry import box
    from groundtruth.solver.peec import build_grid
    w = 0.35
    sub = math.ceil(d / 0.15)
    g = build_grid({"N": box(2.0, y - w / 2, 18.0, y + w / 2)}, (0.0, 0.0, 20.0, 24.0), d, sub=sub)
    cols = range(int(4 / d), int(16 / d))
    for i in cols:
        jj = np.nonzero(g.xb[i])[0]
        assert abs(g.wx[i, jj].sum() * d - w) <= d / sub + 1e-9, (i, g.wx[i, jj])
        centre = g.y0 + np.sum((jj + g.ox[i, jj]) * g.wx[i, jj]) / g.wx[i, jj].sum() * d
        assert centre == pytest.approx(y, abs=d / sub)


def test_SP27_two_touching_partial_strips_are_one_strip_of_their_width():
    """SP-27: two side-by-side strips of width φd touching (centres φd apart) carrying half the current each have the
    inductance of one 2φd strip (kernel.strip_self_numeric, 24 sub-strips, zero thickness), from the full strip's
    self term (the kernels'), what a narrower strip adds and the side-by-side mutual the stack uses
    (PlaneStack._widths), within 1 %."""
    from groundtruth.solver.kernel import strip_kernel, strip_self_numeric
    from groundtruth.solver.peec import _narrow_self, _side_mutual
    d = 1e-3
    full = strip_kernel(3, 3, d)[2, 2]
    assert full == pytest.approx(strip_self_numeric(d, d), rel=1e-6)
    for phi in (0.1, 0.15, 0.3, 0.5):
        self_ = full + _narrow_self(d, phi, 0.0)
        m = float(_side_mutual(d, phi, phi, phi)[0])
        assert (self_ + m) / 2 == pytest.approx(strip_self_numeric(d, 2 * phi * d), rel=0.01), phi


def _neck_island(d, y_neck, w_neck=0.35):
    """A 15 x 15 mm island on the top plate joined to the rest of it by a w x 3 mm neck centred at y_neck, a full
    bottom plate 0.2 mm below (εr 4.5, tan δ 0.02), probed between the plates in the island's far corner; the
    simulation's sub-grid (≤ 0.15 mm)."""
    from shapely.geometry import box
    from shapely.ops import unary_union
    from groundtruth.solver.peec import EPS0, MultiPlaneProblem, PlaneLayer, PlanePair, TraceSegment, build_grid
    T = 0.2e-3
    bb = (0.0, 0.0, 40.0, 24.0)
    top = unary_union([box(0, 0, 18, 24), box(18, y_neck - w_neck / 2, 21, y_neck + w_neck / 2),
                       box(21, 4.5, 36, 19.5)])
    sub = math.ceil(d / 0.15)
    g_top = build_grid({"GND": top}, bb, d, sub=sub)
    g_bot = build_grid({"GND": box(*bb)}, bb, d, sub=sub)
    i, j = round((35.0 - g_top.x0) / d), round((19.0 - g_top.y0) / d)
    x, yy = g_top.x0 + i * d, g_top.y0 + j * d
    probe = [TraceSegment(np.array([x * 1e-3, yy * 1e-3, -T]), np.array([x * 1e-3, yy * 1e-3, 0.0]), 0.3e-3)]
    return MultiPlaneProblem([PlaneLayer("top", 0.0, g_top), PlaneLayer("bottom", -T, g_bot)], probe,
                             (1, i, j), (0, i, j), [], capacitance=[PlanePair(0, 1, EPS0 * 4.5 / T, 0.02)])


def _peak(p, fs):
    """The frequency of Re Z's peak (a parabola through the highest sample and its neighbours, on log f)."""
    r = np.array([complex(p.solve(f).Z_loop).real for f in fs])
    k = int(np.argmax(r))
    assert 0 < k < len(fs) - 1, "peak outside the sweep"
    a, b, _ = np.polyfit(np.log(fs[k - 1:k + 2]), r[k - 1:k + 2], 2)
    return math.exp(-b / (2 * a))


def test_SP27_a_neck_s_resonance_does_not_depend_on_where_it_sits_on_the_grid():
    """SP-27: the neck island's resonance (the island's C against the neck's L, ~750 MHz) with the 0.35 mm neck on a
    0.35 mm grid: aligned with one cell row (one full strip, the neck exactly) and straddling two (two partial strips
    side by side) agree within 2 %. With full-width strips the straddling neck read 817 against 754 MHz; with the
    widths but the strips' mutual a pitch apart, 781."""
    fs = np.geomspace(650e6, 900e6, 16)
    aligned = _peak(_neck_island(0.35, 11.9), fs)
    straddling = _peak(_neck_island(0.35, 12.0), fs)
    assert straddling == pytest.approx(aligned, rel=0.02), (aligned, straddling)


def _tier1(tmp_path, name, zones, holes=()):
    """A 4-layer board (0.2 mm to In1: h = 0.235 mm between the foils' centres): SIG on F.Cu from U1 (x 5) to U2 (x
    35) at y 10, 0.2 mm wide; `zones` [(net, outline)] on In1, the first with `holes`; the Tier 1 issues."""
    from boardgen import BoardGen
    from conftest import make
    from groundtruth.rules.checks import run_checks
    g = BoardGen(layers=4, size=(40.0, 20.0))
    g.ic("U1", (5.0, 10.0), "SIG", gnd_offset=(0.0, 1.0))
    g.ic("U2", (35.0, 10.0), "SIG", gnd_offset=(0.0, 1.0))
    g.track((5.0, 10.0), (35.0, 10.0), "SIG")
    for k, (net, outline) in enumerate(zones):
        g.zone(net, "In1.Cu", outline, holes if k == 0 else ())
    board, sc, _ = make(tmp_path, g, name)
    return [i for i in run_checks(board, sc.config).issues if i.net == "SIG"]


FULL = [(0.3, 0.3), (39.7, 0.3), (39.7, 19.7), (0.3, 19.7)]


@pytest.mark.parametrize("width, flagged", [(1.0, True), (0.3, True), (0.05, False)])
def test_SP21_a_slot_across_the_trace_is_a_gap_of_its_width(tmp_path, width, flagged):
    """SP-21: a slot in the GND plane straight across the trace: one gap_crossing as long as the slot is wide; a
    slot narrower than min_gap_mm (0.1) is not a gap."""
    x0 = 20.0 - width / 2
    hole = [(x0, 4.0), (x0 + width, 4.0), (x0 + width, 16.0), (x0, 16.0)]
    gaps = [i for i in _tier1(tmp_path, f"slot{width}", [("GND", FULL)], [hole]) if i.check == "gap_crossing"]
    assert len(gaps) == (1 if flagged else 0)
    if flagged:
        assert gaps[0].length_mm == pytest.approx(width, abs=0.01) and gaps[0].location[0] == pytest.approx(20.0)


def test_SP22_a_split_between_two_nets_is_named_and_the_other_net_flagged(tmp_path):
    """SP-22: In1 is GND left of x 19.7 and VX right of 20.3: the 0.6 mm split is named with both nets, and the
    15 mm over VX (not a reference net) is a reference_net issue."""
    left = [(0.3, 0.3), (19.7, 0.3), (19.7, 19.7), (0.3, 19.7)]
    right = [(20.3, 0.3), (39.7, 0.3), (39.7, 19.7), (20.3, 19.7)]
    issues = _tier1(tmp_path, "split", [("GND", left), ("VX", right)])
    gap = [i for i in issues if i.check == "gap_crossing"]
    ref = [i for i in issues if i.check == "reference_net"]
    assert len(gap) == 1 and "split between GND and VX" in gap[0].message
    assert gap[0].length_mm == pytest.approx(0.6, abs=0.01)
    assert len(ref) == 1 and ref[0].length_mm == pytest.approx(35.0 - 20.3, abs=0.05)


@pytest.mark.parametrize("edge_y, flagged", [(10.5, True), (11.0, False)])
def test_SP23_a_trace_near_the_plane_edge_is_flagged_within_three_heights(tmp_path, edge_y, flagged):
    """SP-23: a 10 mm long hole in the plane beside the trace (x 15-25, from y = edge_y): edge_proximity when the edge
    is within edge_factor·h = 3 × 0.235 mm of the centreline, with that distance reported; 1 mm away it is not."""
    hole = [(15.0, edge_y), (25.0, edge_y), (25.0, 16.0), (15.0, 16.0)]
    near = [i for i in _tier1(tmp_path, f"edge{edge_y}", [("GND", FULL)], [hole]) if i.check == "edge_proximity"]
    assert bool(near) == flagged
    if flagged:
        assert near[0].details["distance_mm"] == pytest.approx(edge_y - 10.0, abs=0.01)
        assert near[0].details["limit_mm"] == pytest.approx(3 * 0.235, abs=1e-6)
        assert sum(i.length_mm for i in near) > 8.0                  # most of the 10 mm (corners excluded)


def test_SP24_a_trace_running_off_its_plane_is_off_plane_for_the_uncovered_length(tmp_path):
    """SP-24: the GND plane ends at x 25: the last 10 mm of the trace (to U2 at x 35) have no reference: one
    off_plane issue of that length (at a run's end, beyond the 1 mm / 2h pad allowance)."""
    part = [(0.3, 0.3), (25.0, 0.3), (25.0, 19.7), (0.3, 19.7)]
    off = [i for i in _tier1(tmp_path, "off", [("GND", part)]) if i.check == "off_plane"]
    assert len(off) == 1 and off[0].length_mm == pytest.approx(10.0, abs=0.05)


def test_CV29_the_fix_advisor_reproduces_a_boost_and_sizes_its_filter_at_the_input(tmp_path):
    """CV-29: a boost 12 V → 36 V at 1 A with an input connector (its conducted estimate from the inductor ripple):
    evaluate(setup) with no change gives the stage's own figures (the advisor's every option is a difference on
    them), and Middlebrook's |Z_in| is V_in²/P_in at the input, 12²/(12·3) = 4 Ω (ISSUE-2: was 36²/(12·3))."""
    from boardgen import boost_board
    from conftest import make
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.fix_advisor import evaluate, middlebrook
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    board, sc, _ = make(tmp_path, boost_board(), "fab", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out, spec.rise_time = 300e3, 1.0, 12.0, 36.0, 5e-9
    spec.input_connector = ["J1.1", "J1.2"]
    sc.set_converters([spec])
    sc.save()
    res = simulate(board, SettingsFile.load(sc.path).config)
    r, setup = res.converters[0], res.converter_setups[0]
    assert not r.error, r.error
    o = evaluate(setup)
    assert o.overshoot_v == pytest.approx(max(r.overshoot_v or 0.0, (r.ringing or {}).get("overshoot_v", 0.0)))
    if r.worst is not None:
        assert o.radiated_db == pytest.approx(r.worst["margin_db"], abs=1e-9)
    assert r.conducted_worst is not None and o.conducted_db == pytest.approx(r.conducted_worst["margin_db"], abs=1e-9)
    mb = middlebrook(setup, {"L": 2.2e-6, "C": 4.7e-6, "R_damp": 0.7, "C_damp": 22e-6, "damp_at": "connector"})
    assert mb["z_in"] == pytest.approx(12.0 ** 2 / (12.0 * r.pulse_a), rel=1e-9) and r.pulse_a == pytest.approx(3.0)


def _pcm_switching(topology, v_in, v_out, i_out, L, C, ri, se, f_sw, fms, amp=0.003, sub=32):
    """A peak-current-mode buck or boost switched cycle by cycle (ideal synchronous switches, R load, C, L; exact
    linear steps by matrix exponential; turn-off where R_i·i_L + S_e·t = v_c(t), by root search) with its control
    voltage modulated at each f_m: the control-to-output response v̄_out / v_c of the cycle means (what the
    averaged models describe; sampling v_out at the switching instants instead mixes in the boost's pulsed output
    ripple, a few degrees at f_sw/20). se in control-voltage units (V/s). Returns {f_m: complex}."""
    from scipy.linalg import expm
    from scipy.optimize import brentq
    R, T = v_out / i_out, 1 / f_sw
    if topology == "buck":
        D, i_l, up = v_out / v_in, i_out, (v_in - v_out) / L
        modes = ((np.array([[0, -1 / L], [1 / C, -1 / (R * C)]]), [v_in / L, 0.0]),
                 (np.array([[0, -1 / L], [1 / C, -1 / (R * C)]]), [0.0, 0.0]))
    else:
        D = 1 - v_in / v_out
        i_l, up = i_out / (1 - D), v_in / L
        modes = ((np.array([[0, 0], [0, -1 / (R * C)]]), [v_in / L, 0.0]),
                 (np.array([[0, -1 / L], [1 / C, -1 / (R * C)]]), [v_in / L, 0.0]))
    aug = []
    for A, b in modes:
        M = np.zeros((3, 3))
        M[:2, :2], M[:2, 2] = A, b
        aug.append(M)

    def go(x, M, t):
        """The state after t in one mode."""
        return (expm(M * t) @ np.array([x[0], x[1], 1.0]))[:2]

    def run(vc, cycles, x):
        """`cycles` periods from state x: (cycle-middle times, cycle-mean output voltages, end state)."""
        ts, means = [], []
        for n in range(cycles):
            t0 = n * T
            g = lambda tau: ri * go(x, aug[0], tau)[0] + se * tau - vc(t0 + tau)
            d = 0.0 if g(0.0) >= 0 else 1.0 if g(T) < 0 else brentq(g, 0.0, T, xtol=1e-14) / T
            acc = 0.0
            for M, tau in ((aug[0], d * T), (aug[1], (1 - d) * T)):
                if tau <= 0:
                    continue
                E = expm(M * (tau / sub))
                vs = [x[1]]
                for _ in range(sub):
                    x = (E @ np.array([x[0], x[1], 1.0]))[:2]
                    vs.append(x[1])
                vs = np.array(vs)
                acc += tau / sub / 3 * (vs[0] + vs[-1] + 4 * vs[1:-1:2].sum() + 2 * vs[2:-1:2].sum())
            ts.append(t0 + T / 2)
            means.append(acc / T)
        return np.array(ts), np.array(means), x
    vc0 = ri * (i_l + up * D * T / 2) + se * D * T
    _, _, x_ss = run(lambda t: vc0, int(8 * R * C / T) + 400, np.array([i_l, v_out]))
    out = {}
    for fm in fms:
        n_per = round(f_sw / fm)
        w = 2 * math.pi * f_sw / n_per
        dv = amp * vc0
        n_skip = n_per * 4 + int(6 * R * C / T)
        t, v, _ = run(lambda tt: vc0 + dv * math.sin(w * tt), n_skip + 6 * n_per, x_ss)
        t, v = t[n_skip:], v[n_skip:]
        vc = vc0 + dv * np.sin(w * t) * math.sin(w * T / 2) / (w * T / 2)     # the control's own cycle means
        e = np.exp(-1j * w * t)
        out[fm] = (v * e).sum() / (vc * e).sum()
    return out


@pytest.mark.parametrize("topology", ["buck", "boost"])
def test_CV24_the_current_mode_plant_matches_a_cycle_by_cycle_switching_simulation(tmp_path, topology):
    """CV-24: analyse_loop's peak-current-mode plant (its T over the board's own compensator gm·Z(ITH)·H) against
    the converter switched cycle by cycle (_pcm_switching): the buck (Ridley's model) and the boost (the module's
    own derivation, which no published source checked before) agree to 0.3 dB and 1.5° up to f_sw/10 (measured: the
    boost 0.06 dB / 0.3°, the buck 0.21 dB / 0.9° with heavy slope compensation, Ridley's own small error)."""
    from test_loop import B_I, B_IN, B_OUT, C, F_SW, GM, I_OUT, K, L, R_SENSE, V_IN, V_OUT, _boost, _setup
    from groundtruth.analysis.loop import analyse_loop
    if topology == "buck":
        b, cfg, spec = _setup(tmp_path, "sim", **{"Slope Compensation": "20mV/us"})
        v_in, v_out, i_out, rc, cc, chf, h, se = V_IN, V_OUT, I_OUT, 2.2e3, 22e-9, 22e-12, 0.2, 20e3
    else:
        b, cfg, spec = _boost(tmp_path, "sim", **{"Slope Compensation": "10mV/us"})
        v_in, v_out, i_out, rc, cc, chf, h, se = B_IN, B_OUT, B_I, 10e3, 10e-9, 100e-12, 10e3 / 300e3, 10e3
    res = analyse_loop(b, cfg, spec)
    assert not res.error, res.error
    fms = (F_SW / 50, F_SW / 10)
    sim = _pcm_switching(topology, v_in, v_out, i_out, L, C, R_SENSE / K, se / K, F_SW, fms)
    for fm, g in sim.items():
        s = 2j * math.pi * fm
        gc = GM * h / (1 / (rc + 1 / (s * cc)) + s * chf)
        t = np.interp(math.log(fm), np.log(res.response.f), res.response.h.real) + \
            1j * np.interp(math.log(fm), np.log(res.response.f), res.response.h.imag)
        plant = t / gc
        assert 20 * math.log10(abs(g / plant)) == pytest.approx(0.0, abs=0.3), (topology, fm)
        assert math.degrees(np.angle(g / plant)) == pytest.approx(0.0, abs=1.5), (topology, fm)


def _receiver_triangle(n, f0, frac, rate, rbw):
    """An independent EMI receiver on a triangle-swept harmonic: its complex baseband (offset 0 … n·f0·frac), a
    Gaussian filter of 6 dB bandwidth rbw (H(f) = exp(−4 ln2 (f/rbw)²), 0.5 at ±rbw/2) applied by direct convolution
    with its impulse response √(π/a)·exp(−π²t²/a), tunings every rbw/32 across the sweep, the peak envelope."""
    width = n * f0 * frac
    fs = 16.0 * (width + 4 * rbw)
    periods = max(4, math.ceil(8.0 / rbw * rate) + 2)
    t = np.arange(int(periods / rate * fs)) / fs
    dev = width * (1.0 - np.abs(2.0 * ((t * rate) % 1.0) - 1.0))
    s = np.exp(2j * np.pi * np.cumsum(dev) / fs)
    a = 4 * math.log(2) / rbw ** 2
    half = int(4 * math.sqrt(a) / math.pi * fs)
    tau = np.arange(-half, half + 1) / fs
    h = math.sqrt(math.pi / a) * np.exp(-(math.pi ** 2) * tau ** 2 / a) / fs
    return max(float(np.abs(np.convolve(s * np.exp(-2j * np.pi * fc * t), h, mode="valid")[len(tau):]).max())
               for fc in np.arange(-rbw, width + rbw, rbw / 32))


@pytest.mark.parametrize("n, f0, frac, rate", [(1, 500e3, 0.1, 10e3), (1, 500e3, 0.1, 300.0), (5, 500e3, 0.2, 10e3),
                                               (1, 2e6, 0.2, 30e3)])
def test_CV30_the_spread_receiver_reads_what_an_independent_receiver_reads(n, f0, frac, rate):
    """CV-30 (ISSUE-10): emi/spread.receiver_gain, peak detector, triangle modulation, RBW 9 kHz, against the
    independent receiver above within 0.1 dB. The wide sweeps (100 and 500 kHz) have their worst tuning mid-band
    (a comb of lines one modulation rate apart): 25 coarse tunings read them 0.5-0.7 dB low before."""
    from groundtruth.emi.spread import Spread, receiver_gain
    got = receiver_gain(n, f0, Spread(frac, "triangle", rate), 9e3, "peak")
    ref = _receiver_triangle(n, f0, frac, rate, 9e3)
    assert 20 * math.log10(got / ref) == pytest.approx(0.0, abs=0.1)


@pytest.mark.parametrize("v_out", [1.2, 6.0])          # D = 0.1 and 0.5 at 12 V in
def test_CV28_the_switch_node_s_loss_and_densities_are_its_two_states_rms(tmp_path, v_out):
    """CV-28 (ISSUE-7): the switch node carries I from the high side's pads for D and from the low side's for 1 - D.
    Its copper loss is D·P_hs + (1 - D)·P_ls of the two states solved apart (the mean pattern alone read 6-17 % low
    on this buck), its density map the RMS √(D·J_hs² + (1-D)·J_ls²) of those two solves, and the thermal heat
    counts the difference on top of the mean current's."""
    from boardgen import discrete_buck_board
    from conftest import make
    from test_power import run
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.power import (DC_F, _area_shares, _side, dc_board_build, dc_summary, elements_of,
                                            switch_inductor)
    from groundtruth.analysis.simulate import BoardModel
    from groundtruth.analysis.thermal import copper_heat
    from groundtruth.solver.peec import RouteExcitation
    board, sc, _ = make(tmp_path, discrete_buck_board(), "swrms", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, v_out
    r, _, settings = run(tmp_path, discrete_buck_board(), "swrms2", spec=spec)
    D = v_out / 12.0
    sw = next(p for p in r.dc if p["name"].startswith("switch node"))
    # the two conduction states solved apart, on the same whole-board DC model (rebuilt: the result released it)
    st = dc_board_build(BoardModel(board, settings.config))
    els = elements_of(board, spec)
    sw_net, ind = switch_inductor(board, settings.config, spec)
    sink = _area_shares(st, [p for p in ind.pads if p.net == sw_net], _side(ind), -1.0)

    def state(side, pads):
        """The node fed from one side alone: (R in Ω, density maps per A)."""
        inj = [x for el in els if el.ref in side for x in _area_shares(st, pads(el), _side(el.fp), el.weight)]
        sol = RouteExcitation(st.plane, [], injections=inj + sink).solve(DC_F)
        s, m = dc_summary(st, sol, 1.0, "x")
        return s["r_mohm"] * 1e-3, m
    (r_h, m_h), (r_l, m_l) = state(spec.high_side, lambda e: e.b), state(spec.low_side, lambda e: e.a)
    i = spec.i_out
    assert sw["rms"] and sw["loss_w"] == pytest.approx((D * r_h + (1 - D) * r_l) * i * i, rel=2e-3)
    rms = r.dc_maps[sw["name"]]
    for layer in m_h:
        ref = np.sqrt(D * m_h[layer] ** 2 + (1 - D) * m_l[layer] ** 2) * i
        assert np.allclose(rms[layer], ref, rtol=2e-3, atol=1e-3 * float(ref.max() or 1.0)), layer
    # the heat over the node's copper: the mean flow's ρ·J² plus the variance = Σ ρ·J_rms², i.e. the path's loss
    # (the stage's other paths add theirs on top)
    assert r.dc_ripple_sq[sw["name"]]
    total = sum(float(h[3].sum()) for h in copper_heat(board, [r]).values())
    assert total > sw["loss_w"] * 0.99
