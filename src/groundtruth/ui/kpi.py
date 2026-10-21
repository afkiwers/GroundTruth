"""The Results tab's KPI tiles: the key numbers of a view (signal route, power stage, DC path, thermal), each as
(title, value, sub-line, status, tooltip). Status: "ok" | "warn" | "error" | "" (no judgement), from the
settings' rules. No wx here: the tiles themselves are theme.Tile."""
from __future__ import annotations

from typing import Optional

Kpi = tuple  # (title, value, sub, status, tip)


def _ce101_row(r, warn_db: float) -> list:
    """The CE101 row of a stage or bridge (MIL-STD-461 input lead current, 30 Hz-10 kHz; 461H ships to 20 kHz),
    when CE101 is set."""
    from ..components.values import format_si
    w = getattr(r, "ce101_worst", None)
    if not w:
        return []
    m = w["margin_db"]
    return [("CE101 margin", f"{m:+.1f} dB", f"@ {format_si(w['f'], 'Hz')}", _margin_status(m, warn_db),
             f"input lead current (CE101) against {r.ce101_limit} (negative: over)")]


def _margin_status(margin_db: Optional[float], warn_db: float) -> str:
    """A margin's status: over the limit (negative) error, under warn_db above it warn, else ok."""
    if margin_db is None:
        return ""
    return "error" if margin_db < 0 else "warn" if margin_db < warn_db else "ok"


def _level(v: float, warn: float, error: float) -> str:
    """A value's status against its warning and error thresholds (larger is worse)."""
    return "error" if v >= error else "warn" if v >= warn else "ok"


def kpis(rr, rules=None) -> list[Kpi]:
    """The tiles for the result `rr` shown on the Results tab, by what it is: a thermal result, a motor bridge
    (one operating point or its sweep), a power stage (its hot loop, a DC path or its RMS view) or a signal route.
    rules: the settings' rules (their thresholds judge the values; defaults without them)."""
    if getattr(rr, "error", None):
        return [("result", "no result", rr.error, "error", rr.error)]
    if rr.kind == "thermal":
        return _thermal(rr, rules)
    if rr.kind == "bridge":
        return _sweep(rr, rules) if rr.view == "sweep" else _bridge(rr, rules)
    if rr.kind == "dcload":                     # a DC rail with loads (analysis/dcload.py): its DC path's tiles
        return _dc(rr, rules)
    if rr.kind == "pdn":                        # a supply rail's PDN impedance (analysis/pdn.py)
        return _pdn(rr)
    if rr.kind == "bode":                       # responses with their poles and zeros (analysis/transfer.py)
        return _bode(rr)
    if rr.kind == "converter":
        if getattr(rr, "rms_of", None):
            return _rms(rr.rms_of, rules)
        return _dc(rr, rules) if getattr(rr, "view_of", None) else _converter(rr, rules)
    return _route(rr, rules)


def _bode(rr) -> list[Kpi]:
    """A Bode view: per response its dominant pole (the lowest natural frequency, with Q), up to four tiles."""
    from ..analysis.transfer import fmt_hz, pz_rows
    out = []
    for r in rr.responses[:4]:
        rows = pz_rows(r.poles)
        if not rows:
            continue
        p = rows[0]
        how = "exact" if r.exact else (f"fitted, ±{r.fit_error_db:.2f} dB" if r.fit_error_db is not None else "")
        out.append((r.name.split(": ", 1)[-1][:28], fmt_hz(p["f0"]),
                    (f"Q {p['q']:.2g}" if p["q"] else "real pole") + (f" · {how}" if how else ""), "",
                    "the response's dominant pole (its lowest natural frequency); every pole and zero is in the "
                    "map below and the report"))
    return out


def _pdn(rr) -> list[Kpi]:
    """A rail's PDN: the target, the worst margin (and where), the ICs, the highest peak, the source."""
    from ..components.values import format_si
    out = [("target", format_si(rr.target_ohm, "Ω") if rr.target_ohm else "none",
            f"to {format_si(rr.check_to, 'Hz')}" if rr.target_ohm else "set the step current", "",
            "V · ripple / I_step: below it the rail holds its voltage for that current step")]
    if rr.worst:
        w = rr.worst
        st = "error" if w["margin_db"] < 0 else "warn" if w["margin_db"] < 3 else "ok"
        out.append(("worst margin", f"{w['margin_db']:+.1f} dB", f"{w['part']} @ {format_si(w['f'], 'Hz')}", st,
                    f"the IC furthest over (or nearest to) the target, held there by {w['holds']}"))
    out.append(("ICs", str(len(rr.ports)), ", ".join(p["part"] for p in rr.ports[:4])
                + (" …" if len(rr.ports) > 4 else ""), "", "the ICs with supply pins on the rail (ports)"))
    peaks = [(pk, p["part"]) for p in rr.ports for pk in p["peaks"][:1]]
    if peaks:
        pk, part = max(peaks, key=lambda t: t[0]["z_ohm"])
        out.append(("highest peak", format_si(pk["z_ohm"], "Ω"), f"{part} @ {format_si(pk['f'], 'Hz')}", "",
                    "the highest anti-resonance: two decaps, or the decaps and the planes, ringing together"))
    if rr.source:
        out.append(("source", rr.source.split(" ", 1)[0], "R + jωL", "", rr.source))
    return out


def _route(rr, rules) -> list[Kpi]:
    """A signal route: where its return current flows, the loop inductance against the ideal one, the via or
    part carrying most of the return, plane displacement, loop moment and the radiated margin."""
    warn_db = getattr(rules, "emi_margin_db", 6.0)
    out = []
    shares = sorted(rr.layer_share.items(), key=lambda kv: -kv[1])
    if shares:
        main, s = shares[0]
        rest = ", ".join(f"{l} {v * 100:.0f} %" for l, v in shares[1:] if v >= 0.05)
        out.append(("return current", f"{s * 100:.0f} % {main}", rest or "on one layer", "",
                    "share of the return current per plane layer"))
    if rr.solves:
        ratio = f"{rr.l_ratio:.2f}× ideal" if rr.l_ratio else ""
        st = _level(rr.l_ratio, getattr(rules, "detour_warning", 1.25), getattr(rules, "detour_error", 2.0)) \
            if rr.l_ratio else ""
        out.append(("loop inductance", f"{rr.solves[-1]['L_nH']:.2f} nH", ratio, st,
                    "the signal's loop; ideal = a continuous plane under the whole route"))
    from ..analysis.simulate import displacement, point_transfers
    from ..report.formats import share
    t = next(iter(point_transfers(rr.transfers)), None)
    if t is not None:
        out.append(("main transfer", share(t["fraction"]), f"{t['name']}, {t['distance_mm']:.1f} mm away",
                    "", "the via or part carrying most of the return between layers or nets"))
    t = displacement(rr.transfers)
    if t is not None:
        out.append(("plane displacement", share(t["fraction"]),
                    f"{'–'.join(t['layers'])} @ {t.get('f', 0) / 1e6:.4g} MHz", "",
                    "net displacement current through the plane capacitance between these two layers (board-wide "
                    "model, at the frequency shown), spread over the whole pair. Above 1× it is a resonant current "
                    "circulating between the planes and the vias joining them; the model has no radiation loss, so "
                    "a peak is an upper estimate."))
    if rr.m_excess_db is not None:
        out.append(("loop moment", f"{rr.m_excess_db:+.1f} dB", "vs the ideal return", "",
                    "radiating loop moment compared with a perfect return path"))
    if rr.worst:
        m = rr.worst["margin_db"]
        out.append(("radiated margin", f"{m:+.1f} dB", f"@ {rr.worst['f'] / 1e6:.4g} MHz",
                    _margin_status(m, warn_db), "worst harmonic against the radiated limit (negative: over)"))
    return out


def _bridge(b, rules) -> list[Kpi]:
    """A motor bridge: its drive at the operating point and what the legs do together."""
    warn_db = getattr(rules, "emi_margin_db", 6.0)
    op, d = b.operating, b.drive
    out = [("drive", f"duty {op['duty']:.3f}", f"{op['mode']}, {op['speed_rpm']:g} rpm, {op['f_pwm'] / 1e3:g} kHz",
            "" if op["reached"] else "warn",
            "six-step: the PWM duty that drives the phase current at this speed and bus voltage"
            + ("" if op["reached"] else "; the current asked for is not reached"))]
    out.append(("phase current", f"{max(d['phase_rms']):.4g} A RMS",
                f"edges up to {max(d['edges']):.4g} A", "",
                "current in the motor's phases, and the largest current a switch turns on or off"))
    if b.caps:
        top = b.caps[0]
        out.append(("bus ripple", f"{d['bus_ac_rms']:.4g} A RMS",
                    f"{'input' if top['lisn'] else top['name']} {top['rms_a']:.3g} A", "",
                    "AC part of the current the legs draw from the bus; and the bus capacitor carrying most of it"))
    wl = b.worst_leg
    if wl.get("overshoot_v") is not None and op.get("v_bus"):
        frac = wl["overshoot_v"] / op["v_bus"]
        st = _level(frac, getattr(rules, "overshoot_warning", 0.25), getattr(rules, "overshoot_error", 0.5))
        out.append(("L·di/dt", f"{wl['overshoot_v']:.1f} V", f"{frac * 100:.0f} % of {op['v_bus']:g} V, "
                    f"{wl['overshoot_leg'].split()[-1]}", st,
                    "the worst leg's spike from its hot-loop inductance at the largest switched current"))
    if b.conducted_worst:
        m = b.conducted_worst["margin_db"]
        out.append(("conducted margin", f"{m:+.1f} dB", f"@ {b.conducted_worst['f'] / 1e6:.4g} MHz",
                    _margin_status(m, warn_db), "all legs together at the input, read by a simulated receiver"))
    out += _ce101_row(b, warn_db)
    if wl.get("radiated"):
        m = wl["radiated"]["margin_db"]
        out.append(("radiated margin", f"{m:+.1f} dB", f"leg {wl['radiated_leg'].split()[-1]}",
                    _margin_status(m, warn_db), "the worst leg's hot loop against the radiated limit"))
    if b.losses:
        top = b.losses[0]
        out.append(("losses", f"{sum(x['power_w'] for x in b.losses):.2f} W",
                    f"{top['ref']} {top['power_w']:.2f} W", "",
                    "the parts' losses from the drive: switches (conduction, reverse conduction, hard turn-on, output "
                    "charge), snubbers, capacitor ESR, series parts; the copper's on the RMS view"))
    noise = [v for v in getattr(b, "victims", []) if v["kind"] == "net"]
    if noise:
        v = max(noise, key=lambda r: r["peak"])
        out.append(("victim noise", f"{v['peak']:.2f} V", f"{v['name'].split('/')[-1]} at {v['to']}",
                    "warn" if v["peak"] > v["limit"] else "",
                    "the largest noise a victim net's receiver sees at the switching edges (its ground against the "
                    "driver's, and what the hot loop induces along it)"))
    return out


def _converter(c, rules) -> list[Kpi]:
    """A power stage: hot-loop inductance, the L·di/dt overshoot against the input voltage, ringing, the input
    capacitors' shares, conducted and radiated margins and the common-mode budget."""
    warn_db = getattr(rules, "emi_margin_db", 6.0)
    out = [("hot loop", f"{c.loop_nh:.2f} nH", f"{c.loop_mohm:.0f} mΩ at {c.loop_f / 1e6:g} MHz", "",
            "inductance of the switching loop (switches and input capacitors)")]
    frac = c.overshoot_v / c.v_in if c.v_in else None
    st = _level(frac, getattr(rules, "overshoot_warning", 0.25), getattr(rules, "overshoot_error", 0.5)) \
        if frac is not None else ""
    out.append(("L·di/dt", f"{c.overshoot_v:.1f} V", f"{frac * 100:.0f} % of {c.v_in:g} V" if frac is not None
                else "set v_in to judge it", st, "voltage spike from the hot loop's inductance at the switching edge"))
    if c.ringing:
        out.append(("ringing", f"{c.ringing['f'] / 1e6:.0f} MHz", f"Q {c.ringing['q']:.1f}", "",
                    "the hot loop's inductance with the switches' C_oss"))
    rec = getattr(c, "recovery", None)
    if rec:
        out.append(("recovery", f"{rec['i_rr']:.1f} A", f"{rec['q_rr'] * 1e9:g} nC in {rec['t_rr'] * 1e9:.1f} ns"
                    + (f", ≈{rec['overshoot_v']:.0f} V" if rec.get("overshoot_v") else ""), "",
                    "the rectifier's reverse-recovery spike at the hard turn-on (q_rr: a silicon body diode)"))
    caps = [x for x in c.caps if not x["lisn"]][:3]
    if caps:
        out.append(("input capacitors", f"{caps[0]['share_hf'] * 100:.0f} % {caps[0]['name']}",
                    ", ".join(f"{x['name']} {x['share_hf'] * 100:.0f} %" for x in caps[1:]) or "one capacitor", "",
                    "share of the loop current at 100 MHz"))
    if c.conducted_worst:
        m = c.conducted_worst["margin_db"]
        out.append(("conducted margin", f"{m:+.1f} dB", f"@ {c.conducted_worst['f'] / 1e6:.4g} MHz",
                    _margin_status(m, warn_db), "worst reading against the conducted limit (negative: over)"))
    out += _ce101_row(c, warn_db)
    if c.worst:
        m = c.worst["margin_db"]
        out.append(("radiated margin", f"{m:+.1f} dB", f"@ {c.worst['f'] / 1e6:.4g} MHz", _margin_status(m, warn_db),
                    "the hot loop's radiated estimate against the limit (negative: over)"))
    if c.cm:
        out.append(("common-mode budget", f"≤ {c.cm['budget_pf']:.2g} pF", "switch node to chassis", "",
                    "largest switch-node capacitance to the chassis that keeps conducted emission under the limit"))
    lp = getattr(c, "loop", None)
    if lp and lp.get("pm") is not None:
        from ..analysis.transfer import fmt_hz
        status = "error" if lp["pm"] < 30 else "warn" if lp["pm"] < 45 else "ok"
        out.append(("phase margin", f"{lp['pm']:.0f}°", f"crossover {fmt_hz(lp['fc'])}"
                    + (f", GM {lp['gm_db']:.0f} dB" if lp.get("gm_db") is not None else ""), status,
                    "the control loop's phase margin at its crossover (analysis/loop.py; aim for 45° or more): its "
                    "Bode view shows the loop gain"))
    return out


def _dc(c, rules) -> list[Kpi]:
    """One DC path of a power stage (the one viewed): current, resistance with drop and loss, current density and
    its busiest via."""
    p = next(x for x in c.dc if x["name"] == c.view_of)
    w, e = getattr(rules, "current_density_warning", 50.0), getattr(rules, "current_density_error", 100.0)
    out = [("DC " + p["name"], f"{p['current_a']:.3g} A", "", "", "the DC path and its current"),
           ("resistance", f"{p['r_mohm']:.2f} mΩ", f"{p['drop_mv']:.1f} mV, {p['loss_w'] * 1e3:.0f} mW", "",
            "path resistance (copper, vias and the series parts on it), drop and loss"),
           ("current density", f"{p['sustained_a_mm2']:.0f} A/mm²",
            f"sustained, on {p['sustained_layer']} (peak {p['peak_a_mm2']:.0f})",
            _level(p["sustained_a_mm2"], w, e),
            "averaged over 1 mm of copper; the peak is one grid cell; click: zoom the maps to the spot (the cyan ×), "
            "and from KiCad show its copper there")]
    if p["vias"]:
        v = p["vias"][0]
        load = v.get("load_pct")
        out.append(("busiest via", f"{v['current_a']:.2f} A",
                    f"{v['name']}" + (f", {load:.0f} % of IPC-2221" if load is not None else ""),
                    _level(load, 80.0, 100.0) if load is not None else "",
                    "IPC-2221 capacity for a 10 K rise from the drill and plating; click: zoom the maps to it (the "
                    "white ring), and from KiCad select it there"))
    if c.kind == "dcload" and getattr(c, "loads", None):     # a DC rail: the drop its worst-off load sees
        worst = max(c.loads, key=lambda r: r["drop_mv"])
        limit = c.max_drop * 1e3 if c.max_drop else None
        out.insert(1, ("worst drop", f"{worst['drop_mv']:.1f} mV",
                       f"at {worst['pin']} ({worst['supply_mv']:.1f} supply + {worst['return_mv']:.1f} return"
                       + (f"; {worst['pins_mv']:.1f} at the pins" if worst.get("pins_mv") else "") + ")",
                       _level(worst["drop_mv"], 0.8 * limit, limit) if limit else "",
                       "the voltage drop from the source to the load that sees the most"
                       + (f"; the rail's limit is {limit:.0f} mV" if limit else "")))
    return out


def _sweep(b, rules) -> list[Kpi]:
    """A motor bridge's sweep: the worst of its operating points."""
    warn_db = getattr(rules, "emi_margin_db", 6.0)
    rows = [r for r in b.sweep if not r.get("error")]
    out = [("operating points", f"{len(b.sweep)}", f"{sum(1 for r in rows if not r['reached'])} not reached",
            "warn" if any(not r["reached"] for r in rows) else "",
            "every phase current at every speed of the bridge's sweep, from the operating point's solves")]
    if not rows:
        return out

    def at(r):
        """An operating point as text."""
        return f"{r['i_phase']:g} A, {r['speed_rpm']:g} rpm"
    top = max(rows, key=lambda r: r["loss_w"])
    out.append(("losses", f"{top['loss_w']:.1f} W", f"parts, at {at(top)}", "", "the largest parts' loss of the sweep"))
    hot = max(rows, key=lambda r: r["sustained_a_mm2"])
    w, e = getattr(rules, "current_density_warning", 50.0), getattr(rules, "current_density_error", 100.0)
    out.append(("RMS current density", f"{hot['sustained_a_mm2']:.0f} A/mm²", f"at {at(hot)}",
                _level(hot["sustained_a_mm2"], w, e), "the largest sustained RMS current density of the sweep"))
    for key, title, tip in (("conducted_margin_db", "conducted margin", "all legs at the input, simulated receiver"),
                            ("radiated_margin_db", "radiated margin", "the worst leg, scaled with its edge current")):
        got = [r for r in rows if r.get(key) is not None]
        if got:
            r = min(got, key=lambda x: x[key])
            out.append((title, f"{r[key]:+.1f} dB", f"at {at(r)}", _margin_status(r[key], warn_db),
                        f"the smallest of the sweep: {tip}"))
    return out


def _rms(b, rules) -> list[Kpi]:
    """A motor bridge's RMS currents from the battery to the motor (its RMS view)."""
    r = b.rms
    w, e = getattr(rules, "current_density_warning", 50.0), getattr(rules, "current_density_error", 100.0)
    out = [("RMS current density", f"{r['sustained_a_mm2']:.0f} A/mm²",
            f"sustained, on {r['sustained_layer']} (peak {r['peak_a_mm2']:.0f})", _level(r["sustained_a_mm2"], w, e),
            "the six-step drive's currents, battery to motor and the switching currents, as RMS: what heats the "
            "copper; averaged over 1 mm, the peak is one grid cell; click: zoom the maps to the spot (the cyan "
            "×), and from KiCad show its copper there"),
           ("copper loss", f"{r['copper_w']:.2f} W", f"phase {max(b.drive['phase_rms']):.3g} A RMS", "",
            "Joule heat in the copper of the modelled region (vias and parts not included)")]
    if r["vias"]:
        v = r["vias"][0]
        load = v.get("load_pct")
        out.append(("busiest via", f"{v['current_a']:.2f} A RMS",
                    f"{v['name']}" + (f", {load:.0f} % of IPC-2221" if load is not None else ""),
                    _level(load, 80.0, 100.0) if load is not None else "",
                    "IPC-2221 capacity for a 10 K rise from the drill and plating; click: zoom the maps to it (the "
                    "white ring), and from KiCad select it there"))
    return out


def _capacity(t) -> list[Kpi]:
    """A thermal capacity search: the current (or load factor) at which the hottest copper or part reaches the
    limit, what limits it, and the losses there. Below 1× (the operating point already too hot) is an error,
    under 1.2× a warning."""
    from ..analysis.thermal import capacity_text
    c = t.capacity
    s = c["scale"] if c.get("bound") != "zero" else 0.0
    status = "ok" if c.get("bound") == "above" else "error" if s < 1 else "warn" if s < 1.2 else "ok"
    if c.get("currents") is not None:
        head = ("load capacity", capacity_text(c), ", ".join(f"{n} {i:.3g} A" for n, i in c["currents"].items())
                if s > 0 else "over the limit even without load", status,
                f"every power stage's load scaled by one factor: where the hottest copper or part junction reaches "
                f"{c['limit_c']:g} °C (thermal.capacity_c)")
    else:
        head = ("current capacity", capacity_text(c), f"{c['stage']}; operating point {c['i_nominal']:g} A "
                f"({100 / s:.0f} % of it)" if s > 0 else
                f"{c['stage']}: over the limit even without load", status,
                f"the load current at which the hottest copper or part junction reaches {c['limit_c']:g} °C, the "
                f"other power stages at their operating points (thermal.capacity_c)")
    return [head,
            ("limited by", c["limited_by"], f"{c['t_hottest']:.1f} °C, limit {c['limit_c']:g} °C", "",
             "where the limit is reached first"),
            ("total loss", f"{t.power_w:.2f} W", f"copper {t.copper_w:.2f} W, parts {t.parts_w:.2f} W", "",
             "Joule heat of the DC paths plus the parts' losses at this current")]


def _thermal(t, rules) -> list[Kpi]:
    """A thermal result: the hottest copper, the losses and the hottest part (or the capacity tiles)."""
    if getattr(t, "capacity", None):
        return _capacity(t)
    out = [("hottest copper", f"{t.t_max:.1f} °C", f"{t.t_max - t.ambient_c:+.1f} K over {t.ambient_c:g} °C, "
            f"{t.hotspot_layer}", _level(t.t_max, t.warn_c, t.max_c), "the hot spot of the copper"),
           ("total loss", f"{t.power_w:.2f} W", f"copper {t.copper_w:.2f} W, parts {t.parts_w:.2f} W", "",
            "Joule heat of the DC paths plus the parts' losses")]
    hottest = sorted(t.parts, key=lambda p: -p["t_c"])[:1]
    for p in hottest:
        out.append(("hottest part", f"{p['t_c']:.1f} °C", f"{p['ref']}, {p['power_w']:.2f} W",
                    _level(p["t_c"], t.warn_c, t.max_c), "board temperature under the part's pads"))
    et = getattr(t, "electrothermal", None) or {}
    if et:              # the electro-thermal coupling (analysis/electrothermal.py)
        top = (et.get("vias") or [None])[0]
        sub = (f"busiest via {top['current20_a']:.3g} → {top['current_a']:.3g} A" if top and
               top.get("current20_a") is not None else (et.get("stopped") or ""))
        out.append(("electro-thermal", "settled" if et.get("settled") else "not settled",
                    f"{et.get('rounds', 0)} of at most {et.get('max_rounds', 0)} rounds; {sub}",
                    "" if et.get("settled") else "warn",
                    "the DC paths solved again with the copper at its temperature, then Thermal again, until no "
                    f"path's loss or via's current moved more than {et.get('tolerance', 0) * 100:g} %; the busiest via's "
                    "current at 20 °C and at temperature (the notes and the report list the vias)"))
    return out


def notes(rr) -> str:
    """The result's notes on one line (shown under the tiles)."""
    return "; ".join(getattr(rr, "notes", []) or [])
