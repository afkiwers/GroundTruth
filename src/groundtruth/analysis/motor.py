"""Six-step (Hall-commutated) drive of a three-phase BLDC motor by a bridge of three half-bridge legs: the phase,
switch and bus currents in steady state at the user's operating point (bus voltage, PWM frequency, phase current,
speed, motoring or generating).

The motor is given by its terminal data (line-to-line R, L, back-EMF constant). Every phase then has the same
star-equivalent R = R_LL/2 (plus the switches' on-resistance) and L = L_LL/2 = L - M, for a star and for a delta
winding alike; the connection only changes the back-EMF shape the terminals see: a delta's star-equivalent EMF is
(w_ab - w_ca)/3, without the triplen harmonics, which drive a current circulating inside the delta instead.

    L di_k/dt = v_k - v_n - R i_k - e_k,        i_a + i_b + i_c = 0

v_k is the leg voltage: the bus V with the high side on, 0 with the low side on, and with both off the current
flows on through a switch's reverse conduction (GaN: ~2 V, v_sd): -v_sd while it flows out of the leg (low side),
V + v_sd while it flows back into the bus (high side), until it reaches zero; then the phase is open (its current
stays zero, its terminal follows v_n + e_k) until that voltage reaches a clamp again. With equal R and L the
system is three decoupled first-order equations once v_n is known (from the sum of the currents), so each
interval between switching instants is solved in closed form for an EMF that is linear in time over it.

Six-step: sector k (60 electrical degrees) connects the pair PAIRS[k] (p sources, q sinks), centred where their
line-to-line EMF peaks (minus the commutation advance). Commutation happens at PWM period boundaries (as with a
timer's preload), so an electrical period is N = 6·n_s PWM periods and the speed is adjusted to f_pwm/N.
Schemes: hpwm_lon (p's high side chops, q's low side held on; with `synchronous` p's low side conducts in the off
time), hon_lpwm (the mirror), bipolar (p and q both chop, each leg complementary). A dead time precedes every
turn-on. The steady state is the fixed point over 120 degrees, where the pattern repeats with the phases shifted
(a -> b -> c): found by Newton's method on the two independent currents; the duty is found so that the mean
current of the sourcing phase equals the target (negative when generating).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..config.settings import BridgeSpec

HIGH, LOW, OFF = 1, 0, -1                      # leg states: high side on, low side on, both off
PAIRS = ((0, 1), (0, 2), (1, 2), (1, 0), (2, 0), (2, 1))     # (sourcing p, sinking q) per sector, in rotation order
N_MAX = 24_000                                 # PWM periods per electrical period at most (the speed floor)
I_EPS = 1e-9                                   # A: a current below this is zero (rounding residue of the events)
MAX_EVENTS = 10_000                            # events within one switching interval: more is a stuck loop


class DriveError(ValueError):
    """The drive cannot be simulated with these settings (missing motor data, a zero bus voltage, a run that does
    not settle): reported on the bridge's result."""
    pass


@dataclass
class DriveParams:
    """The drive's operating point and the motor's star equivalent, in SI units."""
    v: float                    # bus voltage
    f_pwm: float
    dead: float                 # dead time before every turn-on
    v_sd: float                 # reverse conduction of an off switch
    r: float                    # per phase: R_LL/2 plus the switches' on-resistance
    l: float                    # per phase: L_LL/2
    e_ll: float                 # line-to-line back-EMF peak at the speed
    f_e: float                  # electrical frequency at the speed (0: locked rotor)
    emf: str = "trapezoidal"
    connection: str = "star"
    advance: float = 0.0        # rad
    pwm: str = "hpwm_lon"
    synchronous: bool = True
    d_max: float = 0.98
    rise_time: float = 2e-9
    r_ll: float = 0.0           # the motor alone, for the delta's circulating current
    l_ll: float = 0.0


def drive_params(b: BridgeSpec) -> tuple[DriveParams, list[str]]:
    """The drive parameters of a bridge's settings, with notes on what was assumed."""
    notes = []
    m = b.motor
    if b.v_bus is None or m.r_ll is None or m.l_ll is None:
        raise DriveError("the bridge needs v_bus and the motor's r_ll and l_ll")
    speed = b.speed_rpm or 0.0
    if speed and (m.ke_ll is None or not m.pole_pairs):
        raise DriveError("a speed needs the motor's ke_ll and pole_pairs")
    n_hs = max(min(len(leg.high_side) for leg in b.legs), 1) if b.legs else 1
    n_ls = max(min(len(leg.low_side) for leg in b.legs), 1) if b.legs else 1
    r_on = 0.0
    if b.rds_on is None:
        notes.append("no R_DS(on): the switches' conduction is left out of the phase resistance")
    else:
        r_on = 0.5 * (b.rds_on / n_hs + (b.rds_on_low if b.rds_on_low is not None else b.rds_on) / n_ls)
    v_sd = b.v_sd if b.v_sd is not None else 2.0
    p = DriveParams(v=b.v_bus, f_pwm=b.f_pwm, dead=b.dead_time, v_sd=v_sd, r=m.r_ll / 2 + r_on, l=m.l_ll / 2,
                    e_ll=(m.ke_ll or 0.0) * speed / 1000.0, f_e=speed / 60.0 * (m.pole_pairs or 0),
                    emf=m.emf, connection=m.connection, advance=math.radians(m.advance_deg), pwm=b.pwm,
                    synchronous=b.synchronous, d_max=b.d_max, rise_time=b.rise_time, r_ll=m.r_ll, l_ll=m.l_ll)
    return p, notes


# --------------------------------------------------------------------------- back-EMF
def _trap(phi: float) -> float:
    """A trapezoid of period 2π with a 120° flat top at +1 centred on 0 and a 120° flat bottom at -1."""
    a = abs((phi + math.pi) % (2 * math.pi) - math.pi)
    if a <= math.pi / 3:
        return 1.0
    if a >= 2 * math.pi / 3:
        return -1.0
    return 1.0 - 2.0 * (a - math.pi / 3) / (math.pi / 3)


def _delta_winding(p: DriveParams) -> float:
    """A delta winding's EMF peak for a line-to-line (terminal) peak e_ll: the terminals see w_ab - sum(w)/3 (the
    triplen part drives the circulating current instead), whose peak is 4/3 of a trapezoidal winding EMF's."""
    return p.e_ll * (0.75 if p.emf == "trapezoidal" else 1.0)


def emf(p: DriveParams, theta: float) -> tuple[float, float, float]:
    """The star-equivalent back-EMFs (e_a, e_b, e_c) at the electrical angle theta. Sector 0 (A sources, B sinks)
    is centred on 60°, where e_a - e_b peaks at e_ll."""
    E = p.e_ll
    if E == 0.0:
        return 0.0, 0.0, 0.0
    if p.connection == "delta":                 # winding EMFs, w_ab peaking at 60°
        shape = _trap if p.emf == "trapezoidal" else math.cos
        W = _delta_winding(p)
        w = [W * shape(theta - math.pi / 3 - k * 2 * math.pi / 3) for k in range(3)]   # w_ab, w_bc, w_ca
        return (w[0] - w[2]) / 3, (w[1] - w[0]) / 3, (w[2] - w[1]) / 3
    if p.emf == "trapezoidal":                  # line-to-neutral 120° flat tops of E/2: e_a flat from 30° to 150°
        return tuple(E / 2 * _trap(theta - math.pi / 2 - k * 2 * math.pi / 3) for k in range(3))
    return tuple(E / math.sqrt(3) * math.sin(theta - k * 2 * math.pi / 3) for k in range(3))


def delta_circulation(p: DriveParams, samples: int = 720) -> float:
    """RMS of the current circulating inside a delta winding, driven by the triplen part of the winding EMFs
    (sum/3): L_d di/dt + R_d i = sum(w)/3 with the winding's R_d = 1.5 R_LL, L_d = 1.5 L_LL. 0 for a star or a
    sinusoidal EMF."""
    if p.connection != "delta" or p.emf != "trapezoidal" or p.e_ll == 0.0 or p.f_e <= 0:
        return 0.0
    shape = [sum(_delta_winding(p) * _trap(2 * math.pi * k / samples - math.pi / 3 - j * 2 * math.pi / 3) for j in range(3)) / 3
             for k in range(samples)]
    spec = np.fft.rfft(shape) / samples
    rd, ld = 1.5 * p.r_ll, 1.5 * p.l_ll
    amps = [2 * abs(spec[n]) / abs(complex(rd, 2 * math.pi * n * p.f_e * ld)) for n in range(1, len(spec))]
    return float(math.sqrt(sum(a * a / 2 for a in amps)))


# --------------------------------------------------------------------------- closed-form interval solution
def _f1(x: float) -> float:          # 1 - e^-x
    """1 - e^-x, accurate for small x too (expm1 avoids the cancellation)."""
    return -math.expm1(-x)


def _f2(x: float) -> float:          # x - (1 - e^-x)
    """x - (1 - e^-x): its Taylor series for small x, where the difference of near-equal terms would lose
    every digit."""
    if x < 1e-3:
        return x * x / 2 - x ** 3 / 6 + x ** 4 / 24
    return x - _f1(x)


def _f3(x: float) -> float:          # x^2/2 - x + (1 - e^-x)
    """x^2/2 - x + (1 - e^-x), with its Taylor series for small x (as _f2)."""
    if x < 1e-3:
        return x ** 3 / 6 - x ** 4 / 24 + x ** 5 / 120
    return x * x / 2 - x + _f1(x)


@dataclass
class _Lin:
    """di/dt·L = a + b·t - R·i over an interval: the current after t, and its integral."""
    tau: float
    L: float

    def at(self, i0: float, a: float, b: float, t: float) -> float:
        """The current after t from i0, with drive voltage a + b t across the R-L: the exact solution of the
        first-order ODE (tau = L/R; without resistance a plain integral)."""
        if not math.isfinite(self.tau):
            return i0 + (a * t + b * t * t / 2) / self.L
        x = t / self.tau
        return i0 * math.exp(-x) + a / self.L * self.tau * _f1(x) + b / self.L * self.tau ** 2 * _f2(x)

    def integral(self, i0: float, a: float, b: float, t: float) -> float:
        """The integral of that current over [0, t] (the charge: for mean currents), in closed form."""
        if not math.isfinite(self.tau):
            return i0 * t + (a * t * t / 2 + b * t ** 3 / 6) / self.L
        x = t / self.tau
        return (i0 * self.tau * _f1(x) + a / self.L * self.tau ** 2 * _f2(x) + b / self.L * self.tau ** 3 * _f3(x))


def _root(f, lo: float, hi: float, flo: float) -> float:
    """A zero of f in (lo, hi] where f changes sign (bisection with secant steps)."""
    fhi = f(hi)
    for _ in range(60):
        mid = hi - fhi * (hi - lo) / (fhi - flo) if fhi != flo else (lo + hi) / 2
        if not lo < mid < hi:
            mid = (lo + hi) / 2
        fm = f(mid)
        if fm == 0.0 or hi - lo < 1e-15:
            return mid
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi, fhi = mid, fm
        if hi - lo < 1e-13 * max(hi, 1e-12):
            break
    return (lo + hi) / 2


# --------------------------------------------------------------------------- the drive
@dataclass
class DriveWaveform:
    """The steady-state drive over one electrical period (or one PWM period with a locked rotor)."""
    params: DriveParams
    duty: float
    target: float               # the mean sourcing-phase current asked for (negative: generating)
    mean: float                 # reached
    n: int                      # PWM periods in the period
    T: float                    # PWM period
    f_e: float                  # the electrical frequency simulated (f_pwm / n; 0: locked rotor)
    t: np.ndarray               # breakpoint times (repeated where a switch changes state)
    i: np.ndarray               # phase currents at the breakpoints, shape (len(t), 3)
    hs: np.ndarray              # high-side switch currents (from the bus into each leg), shape (len(t), 3)
    ls: np.ndarray              # low-side switch currents (from each leg into ground: i = hs - ls), (len(t), 3)
    edges: list = field(default_factory=list)   # per leg: largest |current| switched at a hard edge
    notes: list[str] = field(default_factory=list)
    reached: bool = True
    states: Optional[np.ndarray] = None         # leg states (HIGH, LOW, OFF) at the breakpoints, shape (len(t), 3)

    @property
    def period(self) -> float:
        """The simulated period (s): n PWM periods."""
        return self.n * self.T

    def conduction(self) -> dict:
        """Per leg: the mean square current through each side's channel (hs_sq, ls_sq) and the mean current an
        off switch conducts in reverse (hs_rev, ls_rev: dead times and freewheeling), over the period."""
        dt = np.diff(self.t)[:, None]
        a, b, s = self.i[:-1], self.i[1:], self.states[1:]
        # between breakpoints the current is taken as linear: the mean of its square over an interval from a to b
        # is (a^2 + ab + b^2) / 3
        sq = (a * a + a * b + b * b) / 3 * dt
        mean = (a + b) / 2 * dt               # one sign per interval: a current reaching zero is a breakpoint
        off = s == OFF
        T = self.period
        return {"hs_sq": np.where(s == HIGH, sq, 0.0).sum(axis=0) / T,
                "ls_sq": np.where(s == LOW, sq, 0.0).sum(axis=0) / T,
                "hs_rev": np.where(off & (mean < 0), -mean, 0.0).sum(axis=0) / T,
                "ls_rev": np.where(off & (mean > 0), mean, 0.0).sum(axis=0) / T}

    def node_voltages(self) -> np.ndarray:
        """The phase nodes' voltages at the breakpoints: V or 0 with a side on, the reverse-conduction clamp while
        an off leg carries current, and an open phase's v_n + e_k (from the conducting legs' terminal equations)."""
        p = self.params
        V, vsd = p.v, p.v_sd
        S, cur = self.states, self.i
        v = np.where(S == HIGH, V, np.where(S == LOW, 0.0, np.where(cur > 0, -vsd, np.where(cur < 0, V + vsd,
                                                                                               np.nan))))
        theta0, w = math.pi / 6 - p.advance, 2 * math.pi * self.f_e
        for j in np.flatnonzero(np.isnan(v).any(axis=1)):
            e = emf(p, theta0 + w * self.t[j])
            on = [k for k in range(3) if not np.isnan(v[j, k])]
            vn = sum(v[j, k] - e[k] for k in on) / len(on) if len(on) >= 2 else 0.0
            for k in range(3):
                if np.isnan(v[j, k]):
                    v[j, k] = min(max(vn + e[k], -vsd), V + vsd)
        return v

    def switching(self) -> dict:
        """Per leg, summed over the switching instants and divided by the period: at a side's turn-on, the voltage
        it takes over times the current (hs_vi, ls_vi: the overlap loss is ½·t_r of it) and the swing squared
        (hs_dv2, ls_dv2: the output charge's loss is E_oss·(Δv/V)²); every swing squared of the node (node_dv2:
        a snubber's loss is ½·C of it). Turn-off is soft (the load current carries the node across)."""
        v = self.node_voltages()
        t, S, cur = self.t, self.states, self.i
        # the instants: a breakpoint repeated, and the period's end joining its start
        pairs = [(j - 1, j) for j in range(1, len(t)) if t[j] == t[j - 1]] + [(len(t) - 1, 0)]
        out = {k: np.zeros(3) for k in ("hs_vi", "ls_vi", "hs_dv2", "ls_dv2", "node_dv2")}
        for a, b in pairs:
            dv = np.abs(v[b] - v[a])
            out["node_dv2"] += dv * dv
            for k in range(3):
                if S[b, k] != S[a, k] and S[b, k] != OFF:
                    side = "hs" if S[b, k] == HIGH else "ls"
                    out[side + "_vi"][k] += dv[k] * abs(cur[b, k])
                    out[side + "_dv2"][k] += dv[k] ** 2
        return {k: x / self.period for k, x in out.items()}

    def _avg(self, y: np.ndarray, squared: bool = False) -> np.ndarray:
        """Time average (or mean square) of piecewise-linear breakpoint data over the period."""
        t = self.t
        dt = np.diff(t)[:, None]
        a, b = y[:-1], y[1:]
        if squared:
            s = (a * a + a * b + b * b) / 3
        else:
            s = (a + b) / 2
        return (s * dt).sum(axis=0) / self.period

    def hs_ac_rms(self) -> list[float]:
        """Per leg: the AC part (RMS) of the high side's current, the leg's switching current."""
        return [math.sqrt(max(sq - m * m, 0.0)) for m, sq in zip(self._avg(self.hs), self._avg(self.hs, True))]

    def stats(self) -> dict:
        """RMS and peak phase currents, the switches' RMS currents, and the bus current (the high sides' sum):
        mean, RMS, its AC part and its range."""
        bus = self.hs.sum(axis=1)
        mean_bus = float(self._avg(bus[:, None])[0])
        rms_bus = float(math.sqrt(max(self._avg(bus[:, None], True)[0], 0.0)))
        return {"phase_rms": [float(math.sqrt(max(v, 0.0))) for v in self._avg(self.i, True)],
                "phase_peak": [float(np.abs(self.i[:, k]).max()) for k in range(3)],
                "hs_rms": [float(math.sqrt(max(v, 0.0))) for v in self._avg(self.hs, True)],
                "ls_rms": [float(math.sqrt(max(v, 0.0))) for v in self._avg(self.ls, True)],
                "bus_mean": mean_bus, "bus_rms": rms_bus,
                "bus_ac_rms": float(math.sqrt(max(rms_bus ** 2 - mean_bus ** 2, 0.0))),
                "bus_min": float(bus.min()), "bus_max": float(bus.max())}


class SixStep:
    """The six-step drive for one set of parameters: run PWM periods from a state, find the steady state and the
    duty for a current."""

    def __init__(self, p: DriveParams):
        """The drive for `p`. The electrical period is made a whole number of PWM periods (6 sectors of n_s each),
        so the steady state repeats exactly; the simulated electrical frequency is then f_pwm / n."""
        if p.l <= 0 or p.v <= 0 or p.f_pwm <= 0:
            raise DriveError("the drive needs a positive bus voltage, PWM frequency and motor inductance")
        self.p = p
        self.T = 1.0 / p.f_pwm
        self.lin = _Lin(p.l / p.r if p.r > 0 else math.inf, p.l)
        self.notes: list[str] = []
        if p.f_e > 0:
            n_s = max(1, round(p.f_pwm / (6 * p.f_e)))
            if 6 * n_s > N_MAX:
                n_s = N_MAX // 6
                self.notes.append(f"{p.f_pwm / (6 * p.f_e):.0f} PWM periods per sector at this speed: simulated with "
                                  f"{n_s} (commutation every {n_s} periods, the back-EMF kept at the true speed)")
            elif n_s < 4:
                self.notes.append(f"only {p.f_pwm / (6 * p.f_e):.1f} PWM periods per sector: the PWM barely "
                                  "resolves the commutation")
            self.n_s, self.n = n_s, 6 * n_s
            self.f_e = p.f_pwm / self.n
        else:
            self.n_s, self.n, self.f_e = 1, 1, 0.0
        self.theta0 = math.pi / 6 - p.advance       # sector 0 starts here
        self.w = 2 * math.pi * self.f_e

    # ---- one PWM period's segments
    def _segments(self, duty: float, pq: tuple[int, int]) -> list[tuple[float, float, tuple]]:
        """[(start, end, leg states)] within a period for the pair (p, q); the third leg is off."""
        p_, q_ = pq
        T, dt = self.T, min(self.p.dead, self.T)
        on = min(max(duty, 0.0), 1.0) * T
        # four intervals: dead time before turn-on, on, dead time after turn-off, off
        b = [0.0, min(dt, on), on, min(on + dt, T), T]
        sync = self.p.synchronous
        scheme = self.p.pwm
        out = []
        for k in range(4):
            s = [OFF, OFF, OFF]
            if scheme == "hpwm_lon":
                s[p_] = (OFF, HIGH, OFF, LOW if sync else OFF)[k]
                s[q_] = LOW
            elif scheme == "hon_lpwm":
                s[p_] = HIGH
                s[q_] = (OFF, LOW, OFF, HIGH if sync else OFF)[k]
            else:                                   # bipolar: both legs complementary
                s[p_] = (OFF, HIGH, OFF, LOW if sync else OFF)[k]
                s[q_] = (OFF, LOW, OFF, HIGH if sync else OFF)[k]
            if b[k + 1] > b[k]:
                out.append((b[k], b[k + 1], tuple(s)))
        return out

    def _pair(self, m: int) -> tuple[int, int]:
        """The (sourcing, sinking) legs in PWM period m: the sector's pair (with a locked rotor, the first)."""
        return PAIRS[(m // self.n_s) % 6] if self.f_e > 0 else PAIRS[0]

    def _emf_at(self, t: float) -> tuple[float, float, float]:
        """The three phases' back-EMF at time t."""
        return emf(self.p, self.theta0 + self.w * t)

    # ---- the integration
    def run(self, x0, duty: float, m0: int, periods: int, record: Optional[list] = None,
            mean_of: Optional[list] = None) -> list[float]:
        """Run `periods` PWM periods from period index m0 (within the electrical period) with phase currents x0.
        record: breakpoints (t, i_a, i_b, i_c, states) are appended, two per switching instant (before and after).
        mean_of: [integral] accumulates the integral of the sourcing phase's current. Returns the end currents."""
        p = self.p
        i = [float(x0[0]), float(x0[1]), float(x0[2])]
        V, vsd = p.v, p.v_sd
        lin = self.lin
        for m in range(m0, m0 + periods):
            pq = self._pair(m)
            base = m * self.T
            for (s0, s1, S) in self._segments(duty, pq):
                if record is not None:
                    record.append((base + s0, i[0], i[1], i[2], S))
                t, seg_end = s0, s1
                # the back-EMF is taken as linear within the interval (a few % of a sector long), so each phase
                # sees a voltage a + b t and the closed-form _Lin solution applies between events
                e0 = self._emf_at(base + s0)
                e1 = self._emf_at(base + s1)
                span = s1 - s0
                be = [(e1[k] - e0[k]) / span for k in range(3)]
                blocked: set = set()              # phases whose clamp start died at once (below)
                started, events = None, 0
                while seg_end - t > 1e-15 * self.T:
                    events += 1
                    if events > MAX_EVENTS:
                        raise DriveError(f"the drive's switching events do not settle at t = {base + t:.6g} s "
                                         f"(currents {i[0]:.3g}, {i[1]:.3g}, {i[2]:.3g} A)")
                    ec = [e0[k] + be[k] * (t - s0) for k in range(3)]
                    v = [0.0, 0.0, 0.0]
                    cond = []
                    for k in range(3):
                        if S[k] == HIGH:
                            v[k] = V
                        elif S[k] == LOW:
                            v[k] = 0.0
                        elif i[k] > 0:
                            v[k] = -vsd
                        elif i[k] < 0:
                            v[k] = V + vsd
                        else:
                            continue
                        cond.append(k)
                    rem = seg_end - t
                    if len(cond) < 2:             # no path for current (all legs off and idle)
                        i = [0.0, 0.0, 0.0]
                        t = seg_end
                        break
                    # the star point's voltage from the conducting phases (their currents sum to zero): the mean of
                    # their terminal voltages minus EMFs
                    if len(cond) == 3:
                        vn0 = (sum(v) - sum(ec)) / 3
                        vns = -sum(be) / 3
                        f = None
                    else:
                        x, y = cond
                        f = 3 - x - y
                        vn0 = (v[x] + v[y] - ec[x] - ec[y]) / 2
                        vns = -(be[x] + be[y]) / 2
                    a = {k: v[k] - vn0 - ec[k] for k in cond}
                    bb = {k: -vns - be[k] for k in cond}
                    t_ev, ev = rem, None
                    for k in cond:                # a freewheeling current running out
                        if S[k] == OFF:
                            i_end = lin.at(i[k], a[k], bb[k], rem)
                            if (i_end > 0) != (i[k] > 0) or i_end == 0.0:
                                tc = _root(lambda s, k=k: lin.at(i[k], a[k], bb[k], s), 0.0, rem, i[k])
                                if tc < t_ev:
                                    t_ev, ev = tc, ("zero", k)
                    if f is not None and f not in blocked:    # an open phase's terminal reaching a clamp
                        v0, sl = vn0 + ec[f], vns + be[f]
                        hi, lo = V + vsd, -vsd
                        if v0 > hi or v0 < lo:
                            t_ev, ev = 0.0, ("start", f, -1.0 if v0 > hi else 1.0)
                        elif sl > 0 and (hi - v0) / sl < t_ev:
                            t_ev, ev = (hi - v0) / sl, ("start", f, -1.0)
                        elif sl < 0 and (lo - v0) / sl < t_ev:
                            t_ev, ev = (lo - v0) / sl, ("start", f, 1.0)
                    if mean_of is not None:
                        k_src = pq[0]
                        if k_src in a:
                            mean_of[0] += lin.integral(i[k_src], a[k_src], bb[k_src], t_ev)
                    for k in cond:
                        i[k] = lin.at(i[k], a[k], bb[k], t_ev)
                        if abs(i[k]) < I_EPS and k != started:     # (a started phase's seed is smaller)
                            i[k] = 0.0
                    t += t_ev
                    if ev is not None and ev[0] == "zero":
                        k = ev[1]
                        if started == k and t_ev < 1e-9 * self.T:
                            # its clamp started it, the drive took the seed current straight back: the
                            # terminal sits at the clamp with no current; it stays open in this interval
                            blocked.add(k)
                        started = None
                        i[k] = 0.0
                        others = [j for j in range(3) if j != k]
                        d = (i[others[0]] - i[others[1]]) / 2
                        i[others[0]], i[others[1]] = d, -d
                        if record is not None:
                            record.append((base + t, i[0], i[1], i[2], S))
                    elif ev is not None:          # a diode starts conducting
                        i[ev[1]] = ev[2] * 1e-12
                        started = ev[1]
                        if record is not None:
                            record.append((base + t, i[0], i[1], i[2], S))
                        if t_ev == 0.0 and ev[0] == "start":
                            # make progress: the clamp holds now; step on with the new topology
                            continue
                if record is not None:
                    record.append((base + s1, i[0], i[1], i[2], S))
        return i

    # ---- steady state for a duty
    def steady(self, duty: float, guess: float) -> tuple[list[float], float]:
        """The steady state's phase currents at the start of sector 0 and the mean sourcing-phase current."""
        span = 2 * self.n_s if self.f_e > 0 else 1

        def end(x):
            """Run from start currents x (two of them: the third makes the sum zero) over `span` periods: the end
            currents and the mean sourcing-phase current."""
            acc = [0.0]
            e = self.run([x[0], x[1], -x[0] - x[1]], duty, 0, span, mean_of=acc)
            return e, acc[0] / (span * self.T)

        def residual(x):
            """How far the run's end is from the periodic steady state (the start, phases rotated by 120 degrees
            after two sectors; with a locked rotor the same phases after one period)."""
            e, _ = end(x)
            if self.f_e > 0:                     # after 120°: (a, b, c) -> (c, a, b) of the start
                return np.array([e[0] - (-x[0] - x[1]), e[1] - x[0]])
            return np.array([e[0] - x[0], e[1] - x[1]])
        x = np.array([0.0, -guess] if self.f_e > 0 else [guess, -guess], float)
        scale = max(abs(guess), 1e-3)
        r = residual(x)
        # damped Newton with a finite-difference Jacobian (2 x 2): the map from start to end currents is almost
        # linear, so it converges in a few steps
        for _ in range(30):
            if np.abs(r).max() < 1e-10 * scale:
                break
            h = 1e-6 * scale
            J = np.empty((2, 2))
            for j in range(2):
                dx = np.zeros(2)
                dx[j] = h
                J[:, j] = (residual(x + dx) - r) / h
            try:
                step = np.linalg.solve(J, -r)
            except np.linalg.LinAlgError:
                step = -r
            lam = 1.0
            for _ in range(20):                  # damped: accept a step that reduces the residual
                xn = x + lam * step
                rn = residual(xn)
                if np.abs(rn).max() < np.abs(r).max() or lam < 1e-3:
                    break
                lam /= 2
            x, r = xn, rn
        _, mean = end(x)
        return [x[0], x[1], -x[0] - x[1]], mean

    def solve(self, target: float) -> tuple[float, list[float], float, bool]:
        """(duty, start currents, mean reached, reached?) for a mean sourcing-phase current `target` (negative:
        generating)."""
        p = self.p
        # a first duty from the averaged circuit: the applied line voltage d V balances the back-EMF and the two
        # phases' resistive drop (bipolar PWM applies (2d - 1) V)
        e_avg = p.e_ll * (1.0 if p.emf == "trapezoidal" else 3 / math.pi)
        if p.pwm == "bipolar":
            d0 = 0.5 * (1 + (e_avg + 2 * p.r * target) / p.v)
        else:
            d0 = (e_avg + 2 * p.r * target) / p.v + p.dead / self.T
        lo_d, hi_d = 0.0, p.d_max
        guess = abs(target) or 1.0

        def g(d):
            """Mean current reached at duty d minus the target (with the steady state's start currents)."""
            x, mean = self.steady(d, guess)
            return mean - target, x, mean
        d = min(max(d0, lo_d + 1e-4), hi_d)
        gd, x, mean = g(d)
        if abs(gd) <= 1e-6 * max(abs(target), 1e-3):
            return d, x, mean, True
        # bracket: the mean grows with the duty
        if gd < 0:
            a_, ga = d, gd
            b_ = hi_d
            gb, xb, mb = g(b_)
            if gb < 0:
                return b_, xb, mb, False          # cannot reach the target even at d_max
        else:
            b_, gb = d, gd
            a_ = lo_d
            ga, xa, ma = g(a_)
            if ga > 0:
                return a_, xa, ma, False
        side = 0
        for _ in range(60):                      # Illinois (regula falsi)
            c = (a_ * gb - b_ * ga) / (gb - ga) if gb != ga else (a_ + b_) / 2
            gc, x, mean = g(c)
            if abs(gc) <= 1e-6 * max(abs(target), 1e-3) or b_ - a_ < 1e-12:
                return c, x, mean, True
            if (gc < 0) == (ga < 0):
                a_, ga = c, gc
                if side == -1:
                    gb /= 2
                side = -1
            else:
                b_, gb = c, gc
                if side == 1:
                    ga /= 2
                side = 1
        return c, x, mean, abs(gc) <= 1e-3 * max(abs(target), 1e-3)

    # ---- the waveform
    def waveform(self, duty: float, x0: list[float], target: float, mean: float, reached: bool) -> DriveWaveform:
        """The steady state over one electrical period, recorded at every breakpoint: phase currents, leg states,
        the switches' currents (a side carries the leg current when it is on, or in reverse when it is off and
        the current freewheels through it) and the hard-switched current per leg."""
        rec: list = []
        periods = self.n
        self.run(x0, duty, 0, periods, record=rec)
        t = np.array([r[0] for r in rec])
        cur = np.array([[r[1], r[2], r[3]] for r in rec])
        states = np.array([r[4] for r in rec])
        hs = np.where(states == HIGH, cur, np.where((states == OFF) & (cur < 0), cur, 0.0))
        ls = np.where(states == LOW, -cur, np.where((states == OFF) & (cur > 0), -cur, 0.0))
        # hard edges: a leg's high side taking or giving up current while the current flows out of the leg (or
        # its low side while it flows in): the largest |current| at such an instant, per leg
        edges = [0.0, 0.0, 0.0]
        for j in range(1, len(t)):
            if t[j] != t[j - 1]:
                continue
            for k in range(3):
                if states[j, k] != states[j - 1, k] and hs[j, k] != hs[j - 1, k]:
                    edges[k] = max(edges[k], abs(cur[j, k]))
        notes = list(self.notes)
        if not reached:
            notes.append(f"the target current {abs(target):.4g} A is not reached at this speed and bus voltage: "
                         f"{abs(mean):.4g} A at duty {duty:.3f}")
        return DriveWaveform(self.p, duty, target, mean, self.n, self.T, self.f_e, t, cur, hs, ls, edges, notes,
                             reached, states)


def six_step(b: BridgeSpec) -> DriveWaveform:
    """The steady-state six-step drive of a bridge at its operating point."""
    p, notes = drive_params(b)
    if b.i_phase is None:
        raise DriveError("the bridge needs i_phase")
    drive = SixStep(p)
    target = b.i_phase * (-1.0 if b.mode == "generating" else 1.0)
    duty, x0, mean, reached = drive.solve(target)
    w = drive.waveform(duty, x0, target, mean, reached)
    w.notes = notes + w.notes
    circ = delta_circulation(p)
    if circ > 0:
        w.notes.append(f"delta winding: {circ:.3g} A RMS circulates inside it (the trapezoidal EMF's triplen part)")
    return w
