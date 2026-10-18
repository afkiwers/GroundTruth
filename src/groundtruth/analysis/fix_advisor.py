"""Fix advisor for power stages: what it takes to clear a stage's ringing, radiated and conducted issues, as design
changes with their parts, predicted results and costs (the user: "help with actually properly fixing these issues").

Layout candidates (vias, capacitors) are suggest.py's. The changes here are evaluated on the solves Simulate already
did: the loop inductance, the band's loop moment per ampere (band_moments) and the input transfer (_transfer) stay;
what a change alters is the current the loop carries (the edge, the snubber, the loop's size) or what reaches the
LISN (a filter, spread spectrum). `evaluate(setup, change)` recomputes the figures as finish_converter does
(power.stage_spectrum, emission_arrays, conducted_arrays, cm_estimate), so evaluate(setup) reproduces the stage's
own numbers and every option is a difference on the same model.

- snubber: an RC across the switch node (C_s, R_s) on a grid around Z0 = sqrt(L/C_oss) [McMurray 1972]: the best
  damping, and the
  lowest-loss one that meets the overshoot target. Its R dissipates C_s·V²·f_sw.
- edge: a slower switch-node edge (gate resistor): less spectrum above 1/(π·tr) [Paul 2006] and less overshoot, for
  ½·V·I·Δtr·f_sw [Erickson 2001]
  more switching loss per hard edge.
- loop: the largest hot-loop inductance that meets the targets (the moment scales with the loop, same shape).
- filter: a damped LC at the input connector (input_filter_advice made concrete), its attenuation from a two-port
  between the stage's input capacitors and the LISN, checked against the converter's input impedance (Middlebrook).
- spread: spread spectrum, through the simulated receiver.
- cm: a common-mode choke and Y-capacitors for assumed strays, from the common-mode budget."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Optional

import numpy as np

from ..components.values import format_si

E6 = (1.0, 1.5, 2.2, 3.3, 4.7, 6.8)
E12 = (1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2)
E24 = (1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8,
       7.5, 8.2, 9.1)
SNUBBER_C = (1.0, 1.5, 2.0, 3.0, 4.0)       # × C_oss
SNUBBER_R = (0.5, 0.7, 1.0, 1.5, 2.0)       # × Z0 = sqrt(L / C_oss)
EDGE_SLOWER = (1.5, 2.0, 3.0)               # × the edge time
SPREADS = (("triangle", 0.1, 10e3), ("triangle", 0.2, 10e3), ("triangle", 0.2, 3e3), ("random", 0.2, None))
CM_STRAYS_PF = (1.0, 10.0, 50.0)
CM_Y_CAP = 4.7e-9                           # Y capacitor per line to chassis, F
VDS_DERATING = 0.8                          # the switch node's peak stays below this × the switch's Vds rating
X7R_AT_BIAS = 0.5                           # a filter capacitor's share of its nominal C at the rail voltage
LOOP_MIN_SCALE = 0.4                        # a layout change shortens a hot loop to this share of today's at best ...
LOOP_MIN_NH = 0.4                           # ... and not below this (an optimal GaN layout's loop, EPC's guides)


def nearest(value: float, series=E24, up: bool = False) -> float:
    """The standard value of `series` nearest to value (up: the smallest one not below it)."""
    exp = math.floor(math.log10(value))
    cands = [m * 10 ** e for e in (exp - 1, exp, exp + 1) for m in series]
    if up:
        return min(c for c in cands if c >= value * (1 - 1e-9))
    return min(cands, key=lambda c: abs(math.log(c / value)))


@dataclass
class Change:
    """A design change to evaluate on a power stage (any combination): another edge time, an RC snubber, a
    scaled hot loop, an input filter, spread spectrum. Unset fields keep the stage as it is."""
    rise_time: Optional[float] = None           # s
    snubber: Optional[tuple[float, float]] = None   # (C, R) across the switch node
    loop_scale: float = 1.0                     # the hot loop's L and moment × this
    filter: Optional[dict] = None               # {"L", "C", "R_damp", "C_damp", "dcr"} at the input connector
    spread: Optional[tuple] = None              # (f_sw, emi.spread.Spread) instead of the stage's own
    radiated_spread: bool = True                # False: the radiated estimate without spread (ranking options:
                                                # the receiver simulation of a radiated sweep takes seconds)


@dataclass
class Outcome:
    """What a stage would show with a change: its worst radiated and conducted margins (and where), the ringing,
    the overshoot, the common-mode capacitance budget, and the extra loss the change costs."""
    radiated_db: Optional[float] = None         # worst radiated margin, dB
    radiated_f: Optional[float] = None
    conducted_db: Optional[float] = None
    conducted_f: Optional[float] = None
    f_ring: Optional[float] = None
    q: Optional[float] = None
    overshoot_v: float = 0.0                    # the larger of L·di/dt and the ringing's overshoot
    cm_budget_pf: Optional[float] = None
    loss_w: float = 0.0                         # extra loss the change costs

    def to_dict(self) -> dict:
        """The outcome as plain data (the report and the Fixes tab)."""
        return dict(self.__dict__)


def _worst(f: np.ndarray, margins: np.ndarray) -> tuple[Optional[float], Optional[float]]:
    """(smallest margin, its frequency) over the harmonics with a limit, or (None, None) without one."""
    ok = np.isfinite(margins)
    if not ok.any():
        return None, None
    k = int(np.nanargmin(np.where(ok, margins, np.nan)))
    return float(margins[k]), float(f[k])


def input_impedance(setup, f: np.ndarray) -> np.ndarray:
    """The stage's input capacitors in parallel (their models), as the source the input filter sees."""
    cfg = setup.config
    y = np.zeros(len(f), complex)
    for c in setup.res.caps:
        m = cfg.model_for(c["name"]) if not c["lisn"] else None
        if m is None:
            continue
        z = np.array([m.impedance(x) or np.inf for x in f], complex)
        y += 1.0 / z
    return np.where(np.abs(y) > 0, 1.0 / np.where(np.abs(y) > 0, y, 1.0), 1e3)


def _par(a, b):
    """Two impedances in parallel."""
    return a * b / (a + b)


def _filter_parts(setup, f: np.ndarray, flt: dict):
    """(Z_th: the stage's input capacitors, Z_th with the damping if it sits there, Z_L, the connector-side shunt,
    Z_LISN) of an input filter. flt["damp_at"]: "connector" (R_d + C_d across the filter capacitor) or "converter"
    (across the stage's input capacitors, where the L-C_in resonance is)."""
    from ..emi.conducted import lisn_loop_impedance
    w = 2 * np.pi * f
    z_th = input_impedance(setup, f)
    z_l = 1j * w * flt["L"] + flt.get("dcr", 0.0)
    z_c = 1.0 / (1j * w * flt["C"])
    z_d = flt["R_damp"] + 1.0 / (1j * w * flt["C_damp"])
    z_lisn = np.array([lisn_loop_impedance(setup.standard)(x) for x in f], complex)
    if flt.get("damp_at") == "converter":
        return z_th, _par(z_th, z_d), z_l, z_c, z_lisn
    return z_th, z_th, z_l, _par(z_c, z_d), z_lisn


def filter_transfer(setup, f: np.ndarray, flt: dict) -> np.ndarray:
    """I_LISN with the filter / without at f (complex). The stage drives its loop current into its input capacitors
    (Z_th) and the LISN loop: without a filter I_LISN = I·Z_th/(Z_th + Z_LISN); with one, a series L and a shunt C at
    the connector sit between them (current dividers), the damping on either side."""
    z_th, z_th2, z_l, z_c, z_lisn = _filter_parts(setup, f, flt)
    with_filter = z_th2 / (z_th2 + z_l + _par(z_c, z_lisn)) * z_c / (z_c + z_lisn)
    return with_filter * (z_th + z_lisn) / z_th


def filter_ratio(setup, f: np.ndarray, flt: dict) -> np.ndarray:
    """|I_LISN with the filter / without| at f (filter_transfer's magnitude)."""
    return np.abs(filter_transfer(setup, f, flt))


def input_side(spec, res) -> tuple[float, float]:
    """(V_in, I_in) of the stage at its input, where an input filter sits, without losses. A buck: the result's
    v_in and D·I_out (its pulse times its duty). A boost: its result's v_in is the switched V_out, so its own V_in
    (else V_out·d, d = V_in/V_out the high side's conduction), and its input current is the inductor's, the pulse.
    (Before 2026-10-17 a boost's Middlebrook check used V_out²/P_in: (V_out/V_in)² too high a |Z_in|.)"""
    if spec.topology == "boost":
        v_in = spec.v_in or (res.v_in or 0.0) * (res.duty or 0.0)
        return v_in, res.pulse_a
    return res.v_in or 0.0, res.pulse_a * res.duty


def filter_responses(setup, flt: dict) -> list:
    """The proposed input filter as Bode responses (analysis/transfer.py) of the stage: its attenuation of the
    conducted current (filter_transfer, from a tenth of its corner to the top of the conducted band) and, for
    Middlebrook's criterion, the filter's output impedance as the converter sees it next to the converter's input
    impedance −V_in²/P_in (a negative resistance: |Z_in| flat, phase 180°) over middlebrook's band. The curves are
    vector-fitted for their poles and zeros (the cap models and the LISN are networks of R, L and C)."""
    from ..emi.conducted import frequency_range
    from .transfer import Response, fitted_response
    spec, res = setup.spec, setup.res
    f_c = 1 / (2 * math.pi * math.sqrt(flt["L"] * flt["C"]))
    rng = frequency_range(setup.standard, setup.standard_class)
    top = rng[1] if rng else 30e6
    out = []
    f = np.geomspace(f_c / 10, max(top, f_c * 30), 300)
    att = fitted_response(f"{spec.name}: proposed input filter (LISN current with / without)", spec.name,
                          "current ratio", f, filter_transfer(setup, f, flt), max_order=12)
    att.notes.append(f"the fix advisor's π-filter: L {si(flt['L'], 'H')}, C {si(flt['C'], 'F')}, damping "
                     f"{si(flt['R_damp'], 'Ω')} + {si(flt['C_damp'], 'F')} at the {flt.get('damp_at', 'connector')}")
    out.append(att)
    f = np.geomspace(f_c / 10, max(spec.f_sw / 3, f_c * 3), 300)
    _, z_th2, z_l, z_c, z_lisn = _filter_parts(setup, f, flt)
    out.append(fitted_response(f"{spec.name}: filter output impedance (Middlebrook)", spec.name, "impedance", f,
                               _par(z_th2, z_l + _par(z_c, z_lisn)), max_order=12))
    v_in, i_in = input_side(spec, res)
    p_in = v_in * i_in
    if p_in > 0:
        z_in = v_in ** 2 / p_in
        r = Response(f"{spec.name}: converter input impedance −V²/P", spec.name, "impedance", f,
                     np.full(len(f), -z_in, complex), exact=True)
        r.notes.append("the filter's output impedance must stay below it (6 dB: half) up to the loop's crossover")
        out.append(r)
    return out


def middlebrook(setup, flt: dict) -> dict:
    """The filter's output impedance as the converter sees it (its own input capacitors in parallel, the LISN behind
    the filter) against the converter's input impedance |Z_in| = V_in²/P_in, up to f_sw/3 (its control loop's reach):
    the peak must stay below it, by 6 dB to be safe. From a tenth of the filter's corner up: below it the LISN's
    own resistors dominate, not the filter."""
    res, spec = setup.res, setup.spec
    f_c = 1 / (2 * math.pi * math.sqrt(flt["L"] * flt["C"]))
    f = np.geomspace(f_c / 10, max(spec.f_sw / 3, f_c * 3), 400)
    _, z_th2, z_l, z_c, z_lisn = _filter_parts(setup, f, flt)
    z_out = np.abs(_par(z_th2, z_l + _par(z_c, z_lisn)))
    k = int(np.argmax(z_out))
    v_in, i_in = input_side(spec, res)
    p_in = v_in * i_in
    z_in = (v_in ** 2 / p_in) if p_in > 0 else math.inf
    return {"z_out_peak": float(z_out[k]), "f_peak": float(f[k]), "z_in": z_in,
            "ok": bool(z_out[k] * 2 <= z_in), "margin_db": 20 * math.log10(z_in / z_out[k]) if z_out[k] > 0 else None}


def si(value: float, unit: str = "", digits: int = 2) -> str:
    """A value with an SI prefix to `digits` significant figures (part values: 4.7µH, 48kHz)."""
    if value is None:
        return "?"
    return format_si(float(f"{value:.{digits}g}"), unit)


def evaluate(setup, change: Optional[Change] = None) -> Outcome:
    """The stage's figures with a design change, on its own solves (see the module's note)."""
    from ..emi.conducted import conducted_arrays, frequency_range
    from ..emi.radiation import emission_arrays, limit_range
    from .power import (_interp_loglog, _snubber_rc, _transfer, cm_estimate, pulse_harmonics,
                        spread_of, stage_spectrum)
    from .simulate import band_moments
    from .thermal import _synchronous
    change = change or Change()
    spec, res, cfg = setup.spec, setup.res, setup.config
    a = cfg.analysis
    tr = change.rise_time or spec.rise_time
    L = (res.loop_nh or 0.0) * 1e-9 * change.loop_scale
    r_loop = (res.loop_mohm or 0.0) * 1e-3
    snub = change.snubber or _snubber_rc(cfg, spec)
    freqs = setup.harmonics
    pulse = pulse_harmonics(freqs, spec.f_sw, res.pulse_a, res.duty, tr)
    currents, ring = stage_spectrum(spec, freqs, pulse, res.v_in, res.pulse_a, L, r_loop, tr, snub)
    out = Outcome(f_ring=ring["f"] if ring else None, q=ring["q"] if ring else None,
                  overshoot_v=max(L * res.pulse_a / tr if tr > 0 else 0.0, (ring or {}).get("overshoot_v", 0.0)))
    fs, ms = band_moments(setup)
    if fs:
        rad = freqs >= limit_range(a.limit_spec())[0] * 0.5
        m_curve = _interp_loglog(freqs[rad], fs, ms) * change.loop_scale
        rad_spread = change.spread if change.spread is not None else spread_of(spec)
        arr = emission_arrays(freqs[rad], currents[rad], m_curve, a.limit_spec(), a.distance_m, a.ground_reflection,
                              rad_spread if change.radiated_spread else None)
        out.radiated_db, out.radiated_f = _worst(freqs[rad], arr["margin_db"])
    spread = change.spread if change.spread is not None else spread_of(spec)
    fs_c, hs_c = _transfer(setup)
    if setup.standard != "none" and len(fs_c) and not spec.bridge and res.conducted:
        rng = frequency_range(setup.standard, setup.standard_class)
        cf = setup.c_freqs
        sel = (cf >= rng[0]) & (cf <= rng[1])
        cur = currents if setup.cond_currents is None else setup.cond_currents
        line = cur[sel] * _interp_loglog(cf[sel], fs_c, hs_c)
        if change.filter:
            line = line * filter_ratio(setup, cf[sel], change.filter)
        arr = conducted_arrays(cf[sel], line, setup.standard, setup.standard_class, spread)
        out.conducted_db, out.conducted_f = _worst(cf[sel], arr["margin_db"])
    standard = (spec.conducted or a.conducted or "none").lower()
    from ..emi.spread import Spread
    sp = spread[1] if spread else None
    cm = cm_estimate(replace(spec, rise_time=tr, spread=sp.fraction if isinstance(sp, Spread) else None,
                             spread_mode=sp.mode if isinstance(sp, Spread) else spec.spread_mode,
                             spread_rate=sp.rate if isinstance(sp, Spread) else None),
                     res, standard, a.conducted_class) if not spec.bridge else None
    out.cm_budget_pf = cm["budget_pf"] if cm else None
    v = res.v_in or 0.0
    edges = 1 if _synchronous(spec) else 2
    loss = 0.5 * v * res.pulse_a * edges * (tr - spec.rise_time) * spec.f_sw
    if change.snubber:
        loss += change.snubber[0] * v * v * spec.f_sw
    if change.filter:
        loss += input_side(spec, res)[1] ** 2 * change.filter.get("dcr", 0.0)     # the input current through it
    out.loss_w = loss
    return out


# ------------------------------------------------------------------------------------------------- options
@dataclass
class Targets:
    """What a fix must reach: the emission margin of the rules, and an overshoot no larger than the rules'
    share of V_in (or what the switches' Vds rating leaves, derated, when that is less)."""
    margin_db: float              # radiated and conducted margin to reach (rules.emi_margin_db)
    overshoot_v: float            # largest overshoot: rules.overshoot_warning × V_in, or the Vds rating's share
    vds: Optional[float] = None   # the switches' voltage rating (their Vds field), V

    def clears(self, o: Outcome) -> list[str]:
        """The issue checks an outcome would clear (emi_margin, conducted_emission, hot_loop)."""
        out = []
        if o.radiated_db is not None and o.radiated_db >= self.margin_db:
            out.append("emi_margin")
        if o.conducted_db is not None and o.conducted_db >= self.margin_db:
            out.append("conducted_emission")
        if o.overshoot_v <= self.overshoot_v:
            out.append("hot_loop")
        return out


def targets(setup, board=None) -> Targets:
    """The margins and the overshoot to reach; board: for the switches' Vds fields."""
    from ..components.fields import read
    spec, res, rules = setup.spec, setup.res, setup.config.rules
    v = res.v_in or 0.0
    over = rules.overshoot_warning * v
    vds = None
    if board is not None:
        ratings = [r.value for r in (read(fp, "Vds") for fp in (board.footprint(x) for x in spec.parts) if fp)
                   if r is not None and r.value]
        vds = min(ratings) if ratings else None
    if vds:
        over = min(over, max(VDS_DERATING * vds - v, 0.0))
    return Targets(rules.emi_margin_db, over, vds)


@dataclass
class FixOption:
    """One design change the advisor proposes: what kind, a title, the parts to fit, the change in settings
    terms (for Check in Simulate), its outcome, the issues it clears, and how to do it."""
    kind: str                     # snubber | edge | loop | filter | spread | cm
    title: str
    parts: list[str] = field(default_factory=list)
    change: dict = field(default_factory=dict)
    outcome: Optional[Outcome] = None
    clears: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """The option as plain data (the result keeps these, the Fixes tab and the report read them)."""
        d = dict(self.__dict__)
        d["outcome"] = self.outcome.to_dict() if self.outcome else None
        return d


ISSUE_CHECKS = ("emi_margin", "conducted_emission", "hot_loop")


def stage_options(setup, board=None) -> list[dict]:
    """The design changes that help this stage's failing checks, best first: each with its parts, the predicted
    figures, the extra loss and the checks it clears; the first row is the stage as it is. [] when the stage has
    nothing to fix. board: for the switches' Vds fields (the overshoot's limit)."""
    spec, res = setup.spec, setup.res
    if res.error or not res.loop_nh:
        return []
    t = targets(setup, board)
    base = evaluate(setup)
    failing = [c for c in ISSUE_CHECKS if c not in t.clears(base) and _applies(c, base)]
    if not failing:
        return []
    v = res.v_in or 0.0
    opts: list[FixOption] = []
    ringing = "hot_loop" in failing or "emi_margin" in failing
    snubbers = []
    if spec.c_oss and ringing:
        snubbers = _snubbers(setup, t, v)
        opts += snubbers
        opts += _edges(setup)
    if ringing:
        o = _loop(setup, t)
        if o:
            opts.append(o)
    if "conducted_emission" in failing and res.conducted:
        o = _filter(setup, t)
        if o:
            opts.append(o)
        o = _spread(setup, base)
        if o:
            opts.append(o)
    for o in opts:
        if o.outcome is not None and not o.change.get("infeasible"):
            o.clears = [c for c in t.clears(o.outcome) if c in failing]
    wanted = {c for c in failing if c in ("hot_loop", "emi_margin")}
    if wanted and not any(wanted <= set(o.clears) for o in opts):
        o = _combined(setup, t, snubbers, wanted)
        if o:
            o.clears = [c for c in t.clears(o.outcome) if c in failing]
            opts.append(o)
    if res.cm:
        o = _cm(res.cm)
        if o:
            opts.append(o)
    opts.sort(key=lambda o: (-len(o.clears), o.outcome.loss_w if o.outcome else math.inf))
    head = {"kind": "now", "title": "now (no change)", "parts": [], "change": {}, "outcome": base.to_dict(),
            "clears": [], "notes": [f"to fix: {', '.join(failing)}; targets: margin ≥ {t.margin_db:g} dB, overshoot "
                                    f"≤ {t.overshoot_v:.1f} V" + (f" (Vds {t.vds:g} V × {VDS_DERATING:g} − V_in)"
                                                                  if t.vds else "")]}
    return [head] + [o.to_dict() for o in opts]


def _applies(check: str, o: Outcome) -> bool:
    """Whether an outcome has a figure for this check at all (a stage without a conducted estimate cannot clear
    conducted_emission)."""
    return {"emi_margin": o.radiated_db is not None, "conducted_emission": o.conducted_db is not None,
            "hot_loop": True}[check]


def _snubber_parts(setup, c: float, r: float, loss: float, v: float) -> list[str]:
    """The snubber's parts as a shopping list: a C0G capacitor rated 1.5 x the voltage (at least 50 V), a pulse-rated
    resistor for twice its loss."""
    return [f"C {si(c, 'F')} C0G/NP0, ≥ {max(1.5 * v, 50):.0f} V",
            f"R {si(r, 'Ω')}, ≥ {si(max(2 * loss, 0.1), 'W')} (pulse rated)"]


def _snubbers(setup, t: Targets, v: float) -> list[FixOption]:
    """The snubber grid around Z0: the best damping, and the lowest loss that meets the overshoot target."""
    spec, res = setup.spec, setup.res
    # the ringing's characteristic impedance: an R near Z0 damps it best; C a few times C_oss, so the
    # resistor is in the circuit at the ringing frequency (a smaller C barely loads it, a larger one costs loss)
    z0 = math.sqrt(res.loop_nh * 1e-9 / spec.c_oss)
    tried = []
    for kc in SNUBBER_C:
        c = nearest(kc * spec.c_oss, E12)
        for kr in SNUBBER_R:
            r = nearest(kr * z0, E24)
            tried.append(((c, r), evaluate(setup, Change(snubber=(c, r)))))
    best = max(tried, key=lambda x: (len(t.clears(x[1])), x[1].radiated_db or -1e9, -x[1].overshoot_v))
    meets = [x for x in tried if x[1].overshoot_v <= t.overshoot_v]
    cheap = min(meets, key=lambda x: x[1].loss_w) if meets else None
    where = "ground" if spec.topology != "boost" else "the rail"
    side = "low" if spec.topology != "boost" else "high"
    out = []
    for (c, r), o in [best] + ([cheap] if cheap and cheap[0] != best[0] else []):
        label = "best damping" if (c, r) == best[0] else "lowest loss that meets the overshoot target"
        out.append(FixOption(
            "snubber", f"RC snubber across the switch node, {si(c, 'F')} + {si(r, 'Ω')} ({label})",
            _snubber_parts(setup, c, r, o.loss_w, v), {"snubber_c": c, "snubber_r": r}, o,
            notes=[f"from the switch node to {where} right at the {side}-side switch, its own loop as small as it can "
                   f"be; Z0 = √(L/C_oss) = {si(z0, 'Ω')}; the resistor dissipates C·V²·f_sw = {si(o.loss_w, 'W')}; "
                   "its charge current also flows in the hot loop, which the conducted figure includes"]))
    return out


def _edges(setup) -> list[FixOption]:
    """Slower switch-node edges (EDGE_SLOWER times today's), each evaluated: less high-frequency content and
    overshoot, more overlap loss."""
    spec = setup.spec
    out = []
    for k in EDGE_SLOWER:
        tr = spec.rise_time * k
        o = evaluate(setup, Change(rise_time=tr))
        out.append(FixOption("edge", f"slower switch-node edge: {si(tr, 's')} (now {si(spec.rise_time, 's')})",
                             ["a turn-on gate resistor (or a weaker driver) on the hard-switched switch"],
                             {"rise_time": tr}, o,
                             notes=["set the gate resistor so the switch node's edge measures this on a scope; the "
                                    f"extra overlap loss is ½·V·I·Δt·f_sw = {si(o.loss_w, 'W')}"]))
    return out


def _loop(setup, t: Targets) -> Optional[FixOption]:
    """The largest loop (scale of today's) that meets the radiated and overshoot targets, by bisection."""
    res = setup.res

    def ok(k):
        """Whether the loop at k x today's inductance meets the overshoot and radiated targets (and its outcome)."""
        o = evaluate(setup, Change(loop_scale=k))
        c = t.clears(o)
        return ("hot_loop" in c) and ("emi_margin" in c or o.radiated_db is None), o
    if ok(1.0)[0]:
        return None
    reach = max(LOOP_MIN_SCALE, LOOP_MIN_NH / res.loop_nh)        # what a layout can do
    good, o_good = ok(min(reach, 1.0))
    if not good:
        return FixOption("loop", f"a shorter hot loop alone is not enough (even at {res.loop_nh * reach:.2g} nH)", [],
                         {"loop_scale": reach, "infeasible": True}, o_good,
                         notes=[f"a layout shortens a hot loop to about {LOOP_MIN_SCALE:g}× at best (an optimal GaN "
                                f"layout: about {LOOP_MIN_NH:g} nH); combine it with a snubber or a slower edge"])
    lo, hi = reach, 1.0
    for _ in range(20):
        mid = (lo + hi) / 2
        g, o = ok(mid)
        if g:
            lo, o_good = mid, o
        else:
            hi = mid
    near = [c["name"] for c in res.caps if not c["lisn"]][:2]
    return FixOption("loop", f"a shorter hot loop: ≤ {res.loop_nh * lo:.2g} nH (now {res.loop_nh:.3g} nH)", [],
                     {"loop_scale": lo}, o_good,
                     notes=[f"input capacitors right at the switches (now {', '.join(near)} carry most of it), the "
                            "return on the layer directly below; Suggest fixes searches capacitor and via positions"])


def _combined(setup, t: Targets, snubbers: list[FixOption], wanted: set) -> Optional[FixOption]:
    """When no single change clears the ringing and the radiated issues: the cheapest mix of a snubber, a slower
    edge and a shorter loop that does; when none does, the mix that comes closest, with how far it falls short."""
    spec, res = setup.spec, setup.res
    snubs = [None] + [(o.change["snubber_c"], o.change["snubber_r"]) for o in snubbers]
    scales = sorted({1.0, 0.75, 0.5, max(LOOP_MIN_SCALE, LOOP_MIN_NH / res.loop_nh)}, reverse=True)
    tried = []
    for snub in snubs:
        for k in (1.0,) + EDGE_SLOWER:
            for scale in scales:
                if snub is None and k == 1.0 or scale > 1.0:
                    continue
                o = evaluate(setup, Change(snubber=snub, rise_time=spec.rise_time * k, loop_scale=scale))
                tried.append((o.loss_w + (1.0 - scale) * 0.5, snub, k, scale, o))   # a shorter loop costs work
    if not tried:
        return None
    found = [x for x in tried if wanted <= set(t.clears(x[4]))]
    if found:
        _, snub, k, scale, o = min(found, key=lambda x: x[0])
    else:
        _, snub, k, scale, o = max(tried, key=lambda x: (len(t.clears(x[4])), x[4].radiated_db or -1e9, -x[0]))
    what, parts, change = [], [], {}
    if snub:
        what.append(f"snubber {si(snub[0], 'F')} + {si(snub[1], 'Ω')}")
        parts += _snubber_parts(setup, snub[0], snub[1], snub[0] * (res.v_in or 0) ** 2 * spec.f_sw, res.v_in or 0)
        change.update(snubber_c=snub[0], snubber_r=snub[1])
    if k != 1.0:
        what.append(f"edge {si(spec.rise_time * k, 's')}")
        parts.append("a turn-on gate resistor for that edge")
        change["rise_time"] = spec.rise_time * k
    if scale != 1.0:
        what.append(f"hot loop ≤ {res.loop_nh * scale:.2g} nH")
        change["loop_scale"] = scale
    short = [] if found else [f"radiated still {t.margin_db - o.radiated_db:.1f} dB short"
                              if o.radiated_db is not None and o.radiated_db < t.margin_db else "",
                              f"overshoot still {o.overshoot_v - t.overshoot_v:.1f} V over"
                              if o.overshoot_v > t.overshoot_v else ""]
    short = [x for x in short if x]
    return FixOption("combined", ("together: " if found else "closest mix: ") + " + ".join(what)
                     + (f" ({'; '.join(short)})" if short else ""), parts, change, o,
                     notes=["no single change clears both the ringing and the radiated issue; this is "
                            + ("the cheapest mix found (extra loss, a shorter loop counted as work)" if found else
                               "the mix of a snubber, a slower edge and a shorter loop that comes closest: the rest "
                               "needs the loop itself smaller, a different switch or shielding")])


def _filter(setup, t: Targets) -> Optional[FixOption]:
    """An input pi-filter at the connector that meets the conducted margin: L and C from the required attenuation
    (input_filter_advice), rounded up to E6 values (the capacitor's nominal value derated for its DC bias), an
    R-C damping branch at the characteristic impedance; damped at the converter or at the connector, whichever
    keeps Middlebrook's stability criterion better, then evaluated."""
    from .power import input_filter_advice
    res = setup.res
    adv = input_filter_advice(res.conducted, t.margin_db)
    if adv is None:
        return None
    L = nearest(adv["L"], E6, up=True)
    c_nom = nearest(adv["C"] / X7R_AT_BIAS, E6, up=True)
    c_eff = c_nom * X7R_AT_BIAS
    r_d = nearest(math.sqrt(L / c_eff), E24)
    c_d = nearest(4 * c_eff, E6, up=True)
    v, i_in = input_side(setup.spec, res)    # the filter sits at the stage's input: its voltage and current
    dcr = 3e-3 * (L / 1e-6) ** 0.5           # a shielded power inductor of that size, for the loss estimate
    trials = []
    for at in ("converter", "connector"):
        flt = {"L": L, "C": c_eff, "R_damp": r_d, "C_damp": c_d, "dcr": dcr, "damp_at": at}
        trials.append((middlebrook(setup, flt), flt))
    mb, flt = max(trials, key=lambda x: (x[0]["ok"], x[0]["margin_db"] or -1e9))
    o = evaluate(setup, Change(filter=flt))
    # its curves among the stage's Bode responses (the stage's view; the impedances in a view of their own)
    res.responses += filter_responses(setup, flt)
    where = ("across the stage's own input capacitors" if flt["damp_at"] == "converter"
             else "across the filter capacitor at the connector")
    return FixOption(
        "filter", f"input π-filter at the connector, corner ≈ {si(1 / (2 * math.pi * math.sqrt(L * c_eff)), 'Hz')}",
        [f"L {si(L, 'H')}, I_sat ≥ {1.3 * i_in:.2g} A (DCR ≈ {si(dcr, 'Ω')})",
         f"C {si(c_nom, 'F')} X7R ≥ {max(1.5 * v, 25):.0f} V at the connector (≈ {si(c_eff, 'F')} at {v:g} V)",
         f"damping {where}: R {si(r_d, 'Ω')} in series with C {si(c_d, 'F')} (electrolytic or polymer)"],
        {"filter": flt}, o,
        notes=["the stage's own input capacitors form the π's other side; Middlebrook: the filter's output impedance "
               f"peaks at {si(mb['z_out_peak'], 'Ω')} ({si(mb['f_peak'], 'Hz')}) against the converter's |Z_in| = "
               f"{si(mb['z_in'], 'Ω')}: " + ("stable (≥ 6 dB apart)" if mb["ok"] else
                                              "too close: more damping, a larger damping capacitor or a smaller L")])


def _spread(setup, base: Outcome) -> Optional[FixOption]:
    """Spread spectrum, when the stage has none: the best of SPREADS for the conducted margin, offered only if it
    gains more than 0.5 dB."""
    from ..emi.spread import Spread
    spec = setup.spec
    if spec.spread:
        return None
    best = None
    # ranked by the conducted margin; the radiated reading with spread only for the chosen one (below)
    for mode, amount, rate in SPREADS:
        o = evaluate(setup, Change(spread=(spec.f_sw, Spread(amount, mode, rate)), radiated_spread=False))
        if o.conducted_db is not None and (best is None or o.conducted_db > best[1].conducted_db):
            best = ((mode, amount, rate), o)
    if best is None or base.conducted_db is None or best[1].conducted_db <= base.conducted_db + 0.5:
        return None
    (mode, amount, rate), _ = best
    o = evaluate(setup, Change(spread=(spec.f_sw, Spread(amount, mode, rate))))
    how = f"{mode}, +{amount * 100:g} %" + (f" at {si(rate, 'Hz')}" if rate else ", every cycle")
    return FixOption("spread", f"spread spectrum ({how}): {o.conducted_db - base.conducted_db:+.1f} dB conducted",
                     ["the controller's spread-spectrum pin or setting, if it has one"],
                     {"spread": amount, "spread_mode": mode, "spread_rate": rate}, o,
                     notes=["simulated EMI receiver (bandwidth, detector, dwell), for the conducted and the radiated "
                            "estimate"])


def _cm(cm: dict) -> Optional[FixOption]:
    """Common mode: the switch node's capacitance budget to chassis, and for typical stray capacitances the
    common-mode choke (with Y capacitors) that would make up the difference (information: not simulated)."""
    budget, f = cm.get("budget_pf"), cm.get("f")
    if not budget or not f:
        return None
    w = 2 * math.pi * f
    rows = []
    for stray in CM_STRAYS_PF:
        need = 20 * math.log10(stray / budget) + 6 if stray > budget else 0.0
        if need <= 0:
            rows.append(f"{stray:g} pF to chassis: no common-mode parts needed")
            continue
        # an L-C divider per line: a common-mode choke L_cm into Y capacitors to chassis (both lines: 2·C_y)
        l_cm = (10 ** (need / 20) + 1) / (w * w * 2 * CM_Y_CAP)
        rows.append(f"{stray:g} pF to chassis: {need:.0f} dB at {si(f, 'Hz')}, e.g. a common-mode choke "
                    f"≥ {si(l_cm, 'H')} with {si(CM_Y_CAP, 'F')} Y capacitors per line")
    return FixOption("cm", f"common mode: the switch node may have at most {budget:.2g} pF to a chassis or heatsink",
                     [], {}, None, notes=rows)
