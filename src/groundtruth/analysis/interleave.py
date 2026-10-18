"""Interleaved power stages: the phases of one controller (the two channels of a dual buck, a multiphase converter)
switch at one frequency, each delayed by its phase. Their input currents meet at the LISN and their hot loops'
fields add at the antenna, so what a receiver reads is the stages' phasors added per harmonic, not each stage on its
own (docs/ARCHITECTURE.md "Interleaved stages").

Per stage (stage_phasors, at the end of power.finish_converter, for a stage with `interleave` set): the loop current's
complex harmonics, which are the trapezoid pulse's Fourier coefficients with their delay [Paul 2006] shaped by the
ringing's complex response and the switch node's charge current (the factors power.stage_spectrum applies as
magnitudes, here with their phase), times the stage's complex transfer to the LISN (conducted) and its complex
loop-moment vector (radiated). A boost's conducted estimate is its inductor ripple: a triangle's coefficients. The
magnitudes are the stage's own, so a group of one stage reads what the stage reads.

Per group (interleave_groups, at the end of a Simulate in simulate.finish_issues, after a split Simulate's parts are
merged too): a stage delayed by φ (degrees of the switching period) turns its harmonic n by e^(-j·n·φ); the stages'
phasors are added per harmonic number (superposition in the linear copper model [Desoer 1969], each stage's transfer
from its own model [A35]) and read with the same receivers as one stage. Two equal stages 180° apart cancel at the
odd harmonics and add (+6 dB) at the even ones; in phase they add everywhere. The group's figures go on every member's
result (ConverterResult.interleave) with the same sum taken in phase, for comparison, and its issues once.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from ..config.settings import Config
from ..core.issues import Issue, resolve_severity

# what a member's phasor record holds (ConverterResult.phasors; arrays: picklable for the compute server and the
# parts of a split Simulate): f_sw, the harmonic numbers and complex line currents of the conducted estimate (A peak
# at the LISN) and of the radiated one (A m² vectors, x/y/z), the conducted standard and class, the spread spectrum


def trapezoid_phase(n: np.ndarray, duty: float, f_sw: float, rise_time: float, start: float = 0.0) -> np.ndarray:
    """The unit phasors of a trapezoid pulse train's harmonics n: a pulse of width duty·T at half height, equal
    edges of rise_time, rising at start·T. Its coefficients are A·D·sinc(nD)·sinc(n·f·tr)·e^(-j2πn(start + D/2))
    [Paul 2006] (a pulse centred at start + D/2, the sincs real and signed: a lobe past a null turns the phase by π);
    their magnitudes are spectrum.trapezoid_harmonics'."""
    n = np.asarray(n, float)
    sign = np.sign(np.sinc(n * duty) * np.sinc(n * f_sw * rise_time))
    sign[sign == 0] = 1.0
    return sign * np.exp(-2j * np.pi * n * (start + duty / 2))


def ripple_phase(n: np.ndarray, duty: float) -> np.ndarray:
    """The unit phasors of an asymmetric triangle's harmonics n (rising for duty·T from t = 0, then falling): its
    derivative is two steps, so c_n = -ΔI·(1 - e^(-j2πnD)) / (4π²n²·D·(1 - D)) [Paul 2006] (2|c_n| is
    power.ripple_harmonics')."""
    n = np.asarray(n, float)
    c = -(1.0 - np.exp(-2j * np.pi * n * duty))
    mag = np.abs(c)
    return np.where(mag > 0, c / np.maximum(mag, 1e-300), 1.0)


def _unit(z: np.ndarray) -> np.ndarray:
    """z / |z| (1 where z is 0)."""
    mag = np.abs(z)
    return np.where(mag > 0, z / np.maximum(mag, 1e-300), 1.0)


def loop_phase(setup, freqs: np.ndarray) -> np.ndarray:
    """The unit phasors of a stage's hot-loop current at its harmonics `freqs` (the magnitudes are setup.currents):
    the load pulse (the high side's conduction; a boost's starts after the low side's D·T, the controller's edge at
    t = 0 being the main switch's), the ringing's response (power.ringing_rational at s = jω: what ringing_response
    and snubbed_response take the magnitude of) and the edge's charge current (1 + Y_node·V/I: what
    edge_charge_factor takes the magnitude of)."""
    from .power import ringing_rational
    spec, res = setup.spec, setup.res
    f = np.asarray(freqs, float)
    n = np.rint(f / spec.f_sw)
    start = (1.0 - res.duty) if spec.topology == "boost" else 0.0
    u = trapezoid_phase(n, res.duty, spec.f_sw, spec.rise_time, start)
    model = getattr(setup, "ringing_model", None)
    if model is not None:
        L, r, c_oss, snub = model
        num, den = ringing_rational(L, r, c_oss, snub)
        s = 2j * np.pi * f
        u = u * _unit(np.polyval(num, s) / np.polyval(den, s))
        c_node = (res.ringing or {}).get("charge_c")
        if c_node and res.v_in and res.pulse_a > 0:
            y = 1j * 2 * np.pi * f * c_node
            if snub is not None:
                c_s, r_s = snub
                y = y + 1.0 / (r_s + 1.0 / (1j * 2 * np.pi * f * c_s))
            u = u * _unit(1.0 + y * res.v_in / res.pulse_a)
    return u


def complex_transfer(setup) -> tuple[np.ndarray, np.ndarray]:
    """(frequencies, LISN current per A of loop current, complex) at the conducted band's solves: power._transfer
    with the phase (the hot loop's own solves count only when the conducted run is the same model)."""
    if setup.lisn is None:
        return np.zeros(0), np.zeros(0, complex)
    same = setup.cond.exc is setup.fine.exc
    sols = {**(setup.fine.ok() if same else {}), **setup.cond.ok()}
    low = set(setup.ce101_freqs)
    fs = np.array(sorted(f for f in sols if f not in low))
    return fs, np.array([complex(sols[f].I_branch[setup.lisn]) for f in fs])


def moment_vectors(setup, freqs: np.ndarray) -> Optional[np.ndarray]:
    """The loop moment per ampere as complex vectors (N x 3) at `freqs`: the magnitude interpolated in log-log as
    the stage's own estimate does (simulate.band_moments, _interp_loglog [A22]), the direction (each component's
    real and imaginary part, linear in log f between the solves) scaled to it. None without solves."""
    from .simulate import _interp_loglog, band_vectors
    fs, vs = band_vectors(setup)
    if not fs:
        return None
    vs = np.array(vs, complex).reshape(len(fs), 3)
    f = np.asarray(freqs, float)
    mag = _interp_loglog(f, fs, np.linalg.norm(vs, axis=1))
    if len(fs) == 1:
        d = np.repeat(vs, len(f), axis=0)
    else:
        lf, lx = np.log(f), np.log(np.asarray(fs, float))
        d = np.stack([np.interp(lf, lx, vs[:, k].real) + 1j * np.interp(lf, lx, vs[:, k].imag) for k in range(3)],
                     axis=1)
    norm = np.linalg.norm(d, axis=1)
    return d * (mag / np.maximum(norm, 1e-300))[:, None] * (norm > 0)[:, None]


def stage_phasors(setup, config: Config) -> Optional[dict]:
    """A finished stage's phasor record for its interleave group (see the module), or None when it has neither a
    conducted nor a radiated estimate."""
    from ..emi.conducted import frequency_range
    from ..emi.radiation import limit_range
    from .bridge import _interp_complex
    spec, a = setup.spec, config.analysis
    out = {"f_sw": float(spec.f_sw), "standard": setup.standard, "cls": setup.standard_class,
           "spread": (spec.spread, spec.spread_mode, spec.spread_rate) if spec.spread else None,
           "n_cond": None, "cond": None, "n_rad": None, "rad": None, "n_ce101": None, "ce101": None,
           "ce101_v": None, "ce101_i": None}
    # conducted: the line current at the LISN per harmonic of the standard's range
    fs, ts = complex_transfer(setup)
    if setup.standard != "none" and len(fs):
        rng = frequency_range(setup.standard, setup.standard_class)
        cf = setup.c_freqs
        sel = (cf >= rng[0]) & (cf <= rng[1])
        f = cf[sel]
        if len(f):
            n = np.rint(f / spec.f_sw)
            if setup.cond_freqs is not None and setup.res.ripple:     # a boost: the inductor's ripple
                u = ripple_phase(n, setup.res.ripple["duty"])
            else:
                u = loop_phase(setup, f)
            line = np.abs(setup.c_currents[sel]) * u
            line = line * (_interp_complex(f, fs, ts) if len(fs) > 1 else ts[0])
            out["n_cond"], out["cond"] = n.astype(np.int64), line
    # CE101 (MIL-STD-461, a stage switching below its range's top): the harmonics within the limit's range times the
    # complex transfer to the input lead (bridge._ce101_complex_transfer: power._ce101_stage's with the phase)
    if setup.res.ce101:
        from .bridge import _ce101_complex_transfer
        v, i_in = spec.v_in or setup.res.v_in, (setup.res.input or {}).get("i_in_a")
        ce = a.ce101_spec(v, i_in)
        cf = setup.c_freqs
        if ce is not None:
            lo, hi = ce.range
            sel = (cf >= lo) & (cf <= hi)
            fs1, ts1 = _ce101_complex_transfer(setup, hi)
            if sel.any() and len(fs1):
                f = cf[sel]
                n = np.rint(f / spec.f_sw)
                u = ripple_phase(n, setup.res.ripple["duty"]) if (setup.cond_freqs is not None and
                                                                   setup.res.ripple) else loop_phase(setup, f)
                line = np.abs(setup.c_currents[sel]) * u * (_interp_complex(f, fs1, ts1) if len(fs1) > 1 else ts1[0])
                out.update(n_ce101=n.astype(np.int64), ce101=line, ce101_v=v, ce101_i=i_in)
    # radiated: loop current times its moment vector, per harmonic from half the limit's lowest frequency (as
    # power.finish_converter takes them)
    freqs = setup.harmonics
    rad = freqs >= limit_range(a.limit_spec())[0] * 0.5
    if np.any(rad):
        m = moment_vectors(setup, freqs[rad])
        if m is not None:
            i = np.abs(setup.currents[rad]) * loop_phase(setup, freqs[rad])
            out["n_rad"], out["rad"] = np.rint(freqs[rad] / spec.f_sw).astype(np.int64), m * i[:, None]
    if out["cond"] is None and out["rad"] is None and out["ce101"] is None:
        return None
    return out


def group_phases(config: Config) -> dict[str, dict[str, float]]:
    """group -> {stage: phase in degrees} over the settings' stages (enabled or not, so a stage keeps its phase when
    another is switched off): a stage's phase_deg, else 360°·k/n for the k-th of the group's n stages."""
    groups: dict[str, list] = {}
    for c in config.converters:
        if c.interleave:
            groups.setdefault(c.interleave, []).append(c)
    return {g: {c.name: (c.phase_deg if c.phase_deg is not None else 360.0 * k / len(cs)) % 360.0
                for k, c in enumerate(cs)} for g, cs in groups.items()}


def _add(records: list[tuple[dict, float]], key: str) -> tuple[np.ndarray, np.ndarray]:
    """(harmonic numbers, the members' phasors added per harmonic) for "cond", "ce101" (line currents) or "rad"
    (moment vectors), each member's harmonic n turned by e^(-j·n·φ) (φ its phase): a delay of φ/360 of the period."""
    ns = np.concatenate([d["n_" + key] for d, _ in records])
    vals = []
    for d, ph in records:
        rot = np.exp(-1j * d["n_" + key] * math.radians(ph))
        vals.append(d[key] * (rot if d[key].ndim == 1 else rot[:, None]))
    vals = np.concatenate(vals)
    uniq, inv = np.unique(ns, return_inverse=True)
    total = np.zeros((len(uniq),) + vals.shape[1:], complex)
    np.add.at(total, inv, vals)
    return uniq, total


def _spread(d: dict):
    """(f_sw, Spread) of a record's spread spectrum, or None."""
    if not d["spread"]:
        return None
    from ..emi.spread import Spread
    return d["f_sw"], Spread(*d["spread"])


def _rows(records: list[tuple[dict, float]], config: Config) -> dict:
    """The group's conducted and radiated rows and worst readings with the members at their phases (records:
    [(phasor record, phase)]); keys absent where a part could not be combined."""
    from ..emi.conducted import conducted_table
    from ..emi.radiation import emission_table
    from .power import REPORT_PER_DECADE
    a = config.analysis
    d0 = records[0][0]
    f_sw, sp = d0["f_sw"], _spread(d0)
    out: dict = {}
    if all(d["cond"] is not None for d, _ in records):
        n, line = _add(records, "cond")
        rows = conducted_table(n * f_sw, np.abs(line), d0["standard"], d0["cls"], per_decade=REPORT_PER_DECADE,
                               spread=sp)
        out["conducted"] = rows
        compared = [r for r in rows if r["margin_db"] is not None]
        out["conducted_worst"] = min(compared, key=lambda r: r["margin_db"]) if compared else None
    if all(d["rad"] is not None for d, _ in records):
        n, m = _add(records, "rad")
        # the receiver reads |Σ I·m| (A m²): the table multiplies a current by a moment, so the current is 1
        rows = emission_table(n * f_sw, np.ones(len(n)), np.linalg.norm(m, axis=1), a.limit_spec(), a.distance_m,
                              a.ground_reflection, per_decade=REPORT_PER_DECADE, spread=sp)
        for r in rows:
            r.pop("current_a", None)
            r["moment_am2"] = r.pop("m_m2")
        out["radiated"] = rows
        compared = [r for r in rows if r["margin_db"] is not None]
        out["worst"] = min(compared, key=lambda r: r["margin_db"]) if compared else None
    if all(d["ce101"] is not None for d, _ in records):
        from ..emi.conducted import ce101_table
        # the limit at the stages' input current together (CE101-1 relaxes with the load current)
        i_in = sum(d["ce101_i"] or 0.0 for d, _ in records) or None
        ce = a.ce101_spec(d0["ce101_v"], i_in)
        if ce is not None:
            n, line = _add(records, "ce101")
            rows = ce101_table(n * f_sw, np.abs(line), ce, per_decade=REPORT_PER_DECADE)
            out["ce101"], out["ce101_limit"] = rows, ce.describe()
            compared = [r for r in rows if r["margin_db"] is not None]
            out["ce101_worst"] = min(compared, key=lambda r: r["margin_db"]) if compared else None
    return out


def interleave_groups(converters: list, config: Config) -> tuple[list[Issue], list[str]]:
    """Every interleave group with at least two solved members: their phasors added (see the module), the result on
    each member (ConverterResult.interleave), issues below the EMI margin and notes for groups that cannot be
    combined. The members' phasor records are released afterwards (they are only needed here)."""
    from ..emi.conducted import describe
    rules, a = config.rules, config.analysis
    phases = group_phases(config)
    issues: list[Issue] = []
    notes: list[str] = []
    by_group: dict[str, list] = {}
    for r in converters:
        if getattr(r, "kind", "") != "converter" or r.bridge:
            continue
        spec = next((c for c in config.converters if c.name == r.net), None)
        if spec is not None and spec.interleave:
            by_group.setdefault(spec.interleave, []).append(r)
    for group, members in by_group.items():
        solved = [r for r in members if not r.error and r.phasors is not None]
        if len(solved) < 2:
            if len(phases.get(group, {})) >= 2 and len(members) >= 2:
                notes.append(f"interleave group {group}: fewer than two of its stages were solved; not combined")
            continue
        recs = [(r.phasors, phases[group].get(r.net, 0.0)) for r in solved]
        d0 = recs[0][0]
        why = []
        if any(abs(d["f_sw"] - d0["f_sw"]) > 1e-6 * d0["f_sw"] for d, _ in recs):
            why.append("its stages switch at different frequencies")
        if any(d["spread"] != d0["spread"] for d, _ in recs):
            why.append("its stages' spread spectrum differs (only a common dither keeps them in step)")
        if why:
            notes.append(f"interleave group {group}: not combined: {'; '.join(why)}")
            continue
        g = {"group": group, "stages": [r.net for r in solved],
             "phases_deg": {r.net: phases[group].get(r.net, 0.0) for r in solved}, "f_sw": d0["f_sw"],
             "conducted_standard": "", "notes": []}
        if any(d["cond"] is not None for d, _ in recs) and not all(d["cond"] is not None for d, _ in recs):
            g["notes"].append("conducted not combined: not every stage has a conducted estimate (input_connector)")
        elif any(d["standard"] != d0["standard"] or d["cls"] != d0["cls"] for d, _ in recs):
            g["notes"].append("conducted not combined: the stages are checked against different standards")
            for d, _ in recs:
                d["cond"] = None
        if d0["cond"] is not None:
            g["conducted_standard"] = describe(d0["standard"], d0["cls"])
        if any(d["ce101"] is not None for d, _ in recs) and not all(d["ce101"] is not None for d, _ in recs):
            g["notes"].append("CE101 not combined: not every stage has harmonics in its range (each stage's own)")
        g.update(_rows(recs, config))
        in_phase = _rows([(d, 0.0) for d, _ in recs], config)
        g["conducted_in_phase_worst"] = in_phase.get("conducted_worst")
        g["radiated_in_phase_worst"] = in_phase.get("worst")
        g["ce101_in_phase_worst"] = in_phase.get("ce101_worst")
        g["stages_worst"] = {r.net: {"conducted": (r.conducted_worst or {}).get("margin_db"),
                                     "radiated": (r.worst or {}).get("margin_db"),
                                     "ce101": (r.ce101_worst or {}).get("margin_db")} for r in solved}
        for r in solved:
            r.interleave = g
        # issues: the group's own, at its first stage
        lead = solved[0]
        loc = (round(lead.location[0], 4), round(lead.location[1], 4))
        who = ", ".join(f"{n} {p:g}°" for n, p in g["phases_deg"].items())
        w = g.get("conducted_worst")
        if w is not None and w["margin_db"] < rules.emi_margin_db:
            sev = resolve_severity("conducted_emission", rules.severity, "error" if w["margin_db"] < 0 else "warning")
            if sev != "off":
                issues.append(Issue("conducted_emission", sev, group,
                                    f"interleave group {group} ({who}): together an estimated {w['v_dbuv']:.1f} dBµV "
                                    f"at {w['f'] / 1e6:.4g} MHz on the input vs {g['conducted_standard']} "
                                    f"{w['limit_dbuv']:.1f} dBµV (margin {w['margin_db']:+.1f} dB)", loc,
                                    layer=lead.layer, key=f"power:{group}:interleave:conducted",
                                    details={k: w[k] for k in ("f", "v_dbuv", "limit_dbuv", "margin_db")}))
        w = g.get("worst")
        if w is not None and w["margin_db"] < rules.emi_margin_db:
            sev = resolve_severity("emi_margin", rules.severity, "error" if w["margin_db"] < 0 else "warning")
            if sev != "off":
                issues.append(Issue("emi_margin", sev, group,
                                    f"interleave group {group} ({who}): the hot loops together radiate an estimated "
                                    f"{w['e_dbuv']:.1f} dBµV/m at {w['f'] / 1e6:.4g} MHz ({w['distance_m']:g} m) vs "
                                    f"{a.limit_text()} limit {w['limit_dbuv']:.1f} dBµV/m (margin "
                                    f"{w['margin_db']:+.1f} dB)", loc, layer=lead.layer,
                                    key=f"power:{group}:interleave:emi",
                                    details={k: w[k] for k in ("f", "e_dbuv", "limit_dbuv", "margin_db")}))
        w = g.get("ce101_worst")
        if w is not None and w["margin_db"] < rules.emi_margin_db:
            sev = resolve_severity("conducted_emission", rules.severity, "error" if w["margin_db"] < 0 else "warning")
            if sev != "off":
                from ..components.values import format_si
                issues.append(Issue("conducted_emission", sev, group,
                                    f"interleave group {group} ({who}): input lead current together "
                                    f"{w['i_dbua']:.1f} dBµA at {format_si(w['f'], 'Hz')} vs {g['ce101_limit']} "
                                    f"{w['limit_dbua']:.1f} dBµA (margin {w['margin_db']:+.1f} dB)", loc,
                                    layer=lead.layer, key=f"power:{group}:interleave:ce101",
                                    details={k: w[k] for k in ("f", "i_dbua", "limit_dbua", "margin_db")}))
    for r in converters:
        if hasattr(r, "phasors"):
            r.phasors = None
    return issues, notes


def interleave_text(g: dict) -> list[str]:
    """A group's figures in a few phrases (the reports and the window): the margins together at the phases, in
    phase, and each stage's own."""
    from ..components.values import format_si
    who = ", ".join(f"{n} {p:g}°" for n, p in g["phases_deg"].items())
    out = [f"interleaved ({g['group']}: {who})"]
    for what, key, inp in (("conducted", "conducted_worst", "conducted_in_phase_worst"),
                           ("CE101", "ce101_worst", "ce101_in_phase_worst"),
                           ("radiated", "worst", "radiated_in_phase_worst")):
        w, wi = g.get(key), g.get(inp)
        if w is None:
            continue
        own = [s[what.lower()] for s in g["stages_worst"].values() if s.get(what.lower()) is not None]
        txt = f"{what} margin together {w['margin_db']:+.1f} dB @ {format_si(w['f'], 'Hz')}"
        if wi is not None:
            txt += f" (in phase {wi['margin_db']:+.1f} dB"
            txt += f"; each alone from {min(own):+.1f} dB)" if own else ")"
        out.append(txt)
    return out + list(g.get("notes", ()))
