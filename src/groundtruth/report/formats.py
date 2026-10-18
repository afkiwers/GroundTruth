"""Machine-readable report formats: JSON, JUnit XML, GitLab Code Quality, plain text."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import numpy as np

from ..components.values import format_si
from ..core.issues import CHECKS, SEVERITY_RANK, Issue

TIER1_CHECKS = ("route_missing", "no_reference", "gap_crossing", "off_plane", "layer_change_stitch",
                "layer_change_decap", "edge_proximity", "reference_net")
TIER2_CHECKS = ("return_path_broken", "return_detour", "plane_resonance", "emi_margin")
POWER_CHECKS = ("power_loop_broken", "hot_loop", "current_density", "conducted_emission", "emi_margin",
                "plane_resonance")

GITLAB_SEVERITY = {"info": "info", "warning": "minor", "error": "major"}


def ce101_lines(r, who: str = "") -> list:
    """A stage's or bridge's MIL-STD-461 CE101 result in the text report: the worst reading (input lead current,
    30 Hz-10 kHz; 461H ships to 20 kHz) against its limit, or nothing when CE101 is off."""
    from ..components.values import format_si
    w = getattr(r, "ce101_worst", None)
    if not w:
        return []
    return [f"    CE101 ({r.ce101_limit}{', ' + who if who else ''}): input lead {w['i_dbua']:.1f} dBµA @ "
            f"{format_si(w['f'], 'Hz')} (margin {w['margin_db']:+.1f} dB)"]


def _jsonable(o):
    """json.dumps' fallback for what it cannot write itself: numpy numbers and arrays, complex numbers (as
    {"re", "im"}), sets and tuples (as lists)."""
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, complex):
        return {"re": o.real, "im": o.imag}
    if isinstance(o, (set, tuple)):
        return list(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


def share(fraction: float) -> str:
    """A current relative to the signal current: % below 1, ×N above (a resonant current circulating between plane
    capacitance and vias, see simulate.transfers)."""
    return f"{fraction * 100:.0f} %" if fraction < 1 else f"{fraction:.1f}× signal"


def failing(issues: list[Issue], fail_on: str) -> list[Issue]:
    """The issues that fail a CI run: not waived, and at or above the --fail-on severity."""
    rank = SEVERITY_RANK[fail_on]
    return [i for i in issues if not i.waived and SEVERITY_RANK.get(i.severity, 0) >= rank]


def to_json(board_path: str, issues: list[Issue], routes: list = (), meta: Optional[dict] = None,
            converters: list = (), bridges: list = (), dc_loads: list = (), pdn: list = (), si=None) -> str:
    """The JSON report: the board, the run's metadata, issue counts per severity, every issue, and every route's,
    power stage's, bridge's and DC load's summary (no maps); si: the signal-integrity figures (analysis/si.py)."""
    data = {
        "tool": "groundtruth",
        "board": board_path,
        **(meta or {}),
        "summary": {s: sum(1 for i in issues if i.severity == s and not i.waived) for s in ("error", "warning", "info")},
        "issues": [i.to_dict() for i in issues],
        "routes": [r.summary() for r in routes],
        "converters": [c.summary() for c in converters],
        "bridges": [b.summary() for b in bridges],
        "dc_loads": [d.summary() for d in dc_loads],
        "pdn": [d.summary() for d in pdn],
        "si": si.summary() if si is not None else None,
    }
    return json.dumps(data, indent=2, default=_jsonable, ensure_ascii=False)


def dc_load_text(d) -> list[str]:
    """A DC rail with loads (analysis/dcload.py) for the console: its current, loss and density, each load's drop."""
    if d.error:
        return [f"DC load {d.net}: {d.error}"]
    p = d.dc[0]
    out = [f"DC load {d.net}: {p['current_a']:.3g} A from {d.driver}, copper loss {p['loss_w'] * 1e3:.0f} mW, "
           f"{p['sustained_a_mm2']:.0f} A/mm² sustained on {p['sustained_layer']}"
           + (f", busiest via {p['vias'][0]['name']} {p['vias'][0]['current_a']:.2f} A" if p["vias"] else "")]
    for r in d.loads:
        out.append(f"  {r['pin']}: {r['current_a']:.3g} A, drop {r['drop_mv']:.1f} mV ({r['supply_mv']:.1f} supply"
                   f" + {r['return_mv']:.1f} return" + (f"; of which {r['pins_mv']:.1f} at the pins' joints and "
                                                         "contacts" if r.get("pins_mv") else "") + ")")
    return out


def to_codequality(board_path: str, issues: list[Issue]) -> str:
    """GitLab Code Quality report (shown in the merge-request widget)."""
    path = Path(os.path.relpath(board_path)).as_posix() if os.path.isabs(board_path) else Path(board_path).as_posix()
    out = []
    for i in issues:
        if i.waived:
            continue
        out.append({
            "description": f"[{i.net}] {i.title}: {i.message}",
            "check_name": f"groundtruth.{i.check}",
            "fingerprint": i.fingerprint,
            "severity": GITLAB_SEVERITY.get(i.severity, "info"),
            "location": {"path": path, "lines": {"begin": max(int(i.line or 1), 1)}},
            "categories": ["Bug Risk"],
        })
    return json.dumps(out, indent=2, ensure_ascii=False)


def to_junit(board_path: str, issues: list[Issue], nets: list[str], checks: tuple[str, ...], fail_on: str,
             converters: list[str] = ()) -> str:
    """One testcase per (net, check) and per (converter, power check); it fails when that net or converter has
    issues at or above `fail_on`."""
    suites = ET.Element("testsuites", name="groundtruth")
    rank = SEVERITY_RANK[fail_on]
    for net, kinds in [(n, checks) for n in nets] + [(c, POWER_CHECKS) for c in converters]:
        suite = ET.SubElement(suites, "testsuite", name=f"{Path(board_path).name}:{net}")
        n_fail = 0
        for check in kinds:
            case = ET.SubElement(suite, "testcase", classname=net, name=f"{check}: {CHECKS[check][1]}")
            found = [i for i in issues if i.net == net and i.check == check]
            bad = [i for i in found if not i.waived and SEVERITY_RANK.get(i.severity, 0) >= rank]
            if bad:
                n_fail += 1
                f = ET.SubElement(case, "failure", message=f"{len(bad)} issue(s)", type=check)
                f.text = "\n".join(f"[{i.severity}] ({i.location[0]:.2f}, {i.location[1]:.2f}) {i.message}" for i in bad)
            other = [i for i in found if i not in bad]
            if other:
                so = ET.SubElement(case, "system-out")
                so.text = "\n".join(f"[{i.severity}{', waived' if i.waived else ''}] {i.message}" for i in other)
        suite.set("tests", str(len(kinds)))
        suite.set("failures", str(n_fail))
    ET.indent(suites)
    return ET.tostring(suites, encoding="unicode", xml_declaration=True)


def _si2(value: float, unit: str) -> str:
    """Two significant digits with an SI prefix (2.8µH, 100µF)."""
    return format_si(float(f"{value:.2g}"), unit)


def dc_resistance(path: dict) -> str:
    """A DC path's resistance, with the copper's and each series part's share when parts are on the path (a
    current-sense shunt would otherwise read as copper)."""
    text = f"{path['r_mohm']:.2f} mΩ"
    parts = path.get("parts") or []
    if parts:
        text += (f" (copper {path['r_copper_mohm']:.2f}, "
                 + ", ".join(f"{p['name']} {p['r_mohm']:.2f}" for p in parts) + ")")
    return text


def bridge_text(b) -> list[str]:
    """Report lines of one motor bridge (its legs are reported as power stages)."""
    if b.error:
        return [f"{b.net}: {b.error}"]
    op = b.operating
    out = [f"{b.net} ({', '.join(b.legs)}): six-step {op['pwm']}, {op['v_bus']:g} V, {op['f_pwm'] / 1e3:g} kHz, "
           f"{op['mode']} {op['i_reached']:.4g} A at {op['speed_rpm']:g} rpm (f_e {op['f_e']:.4g} Hz), duty "
           f"{op['duty']:.3f}"]
    d = b.drive
    out.append(f"    phase current {max(d['phase_rms']):.4g} A RMS (peak {max(d['phase_peak']):.4g} A, hard edges up to "
               f"{max(d['edges']):.4g} A); bus {d['bus_mean']:.4g} A mean, {d['bus_ac_rms']:.4g} A RMS ripple")
    if b.caps:
        out.append("    bus capacitors' ripple: " + ", ".join(f"{c['name']} {c['rms_a']:.3g} A" for c in b.caps[:6])
                   + (" …" if len(b.caps) > 6 else ""))
    wl = b.worst_leg
    if wl.get("overshoot_v") is not None:
        out.append(f"    worst leg: L·di/dt ≈ {wl['overshoot_v']:.1f} V ({wl['overshoot_leg']}), hot loop up to "
                   f"{wl['loop_nh']:.2f} nH")
    if b.conducted_worst:
        w = b.conducted_worst
        out.append(f"    conducted ({b.conducted_standard}, all legs): {w['v_dbuv']:.1f} dBµV at {w['f'] / 1e6:.4g} MHz, "
                   f"margin {w['margin_db']:+.1f} dB")
    out += ce101_lines(b, "all legs")
    if b.rms.get("sustained_layer"):
        out.append("    RMS currents: " + "; ".join(rms_text(b)))
    if b.losses:
        out.append(f"    losses {sum(x['power_w'] for x in b.losses):.2f} W: "
                   + ", ".join(f"{x['ref']} {x['power_w']:.3g} W" for x in b.losses[:8])
                   + (" …" if len(b.losses) > 8 else ""))
    if b.sweep:
        out += sweep_text(b)[1:]
    if b.victims:
        from ..analysis.bridge import victim_text
        out.append("    victims (at the switching edges, worst leg):")
        out += [f"      {victim_text(r)}" for r in b.victims]
    out += [f"    note: {n}" for n in b.notes]
    return out


def sweep_text(b) -> list[str]:
    """A motor bridge's sweep as a table, one line per operating point."""
    out = [f"{b.net}: sweep, {len(b.sweep)} operating points (from the operating point's solves)",
           "    i_phase  speed  duty   loss W  worst part     copper W  A/mm²  cap A  conducted dB  radiated dB"]
    for r in b.sweep:
        head = f"    {r['i_phase']:7.4g} {r['speed_rpm']:6.4g}"
        if r.get("error"):
            out.append(f"{head}  {r['error']}")
            continue
        cond = f"{r['conducted_margin_db']:+12.1f}" if r.get("conducted_margin_db") is not None else f"{'–':>12}"
        rad = f"{r['radiated_margin_db']:+11.1f}" if r.get("radiated_margin_db") is not None else f"{'–':>11}"
        out.append(f"{head} {r['duty']:5.3f}{'' if r['reached'] else '!'} {r['loss_w']:7.2f}  "
                   f"{(r['worst_part'] + ' ' + format(r['worst_part_w'], '.2f')):<13} {r['copper_w']:8.2f} "
                   f"{r['sustained_a_mm2']:6.0f} {r['cap_rms']:6.2f}  {cond}  {rad}")
    if any(not r.get("reached", True) for r in b.sweep):
        out.append("    !: the current asked for is not reached (duty at its limit)")
    return out


def rms_text(b) -> list[str]:
    """A motor bridge's RMS currents in the copper, in a few phrases."""
    r = b.rms
    out = [f"{r['sustained_a_mm2']:.0f} A/mm² sustained (peak {r['peak_a_mm2']:.0f}) on {r['sustained_layer']}",
           f"copper {r['copper_w']:.2f} W"]
    if r["vias"]:
        v = r["vias"][0]
        out.append(f"{v['name']} {v['current_a']:.2f} A" + (f" ({v['load_pct']:.0f} % of IPC-2221)"
                                                             if v.get("load_pct") is not None else ""))
    out += [f"{p['name']} {p['rms_a']:.3g} A" for p in r["parts"][:3]]
    return out


def converter_text(c) -> list[str]:
    """Report lines of one power stage."""
    head = (f"{c.net} ({c.driver} → {c.receiver}, {c.topology}, {c.f_sw / 1e3:g} kHz, {c.pulse_a:g} A pulse, "
            f"duty {c.duty:.2f}, tr {c.rise_time * 1e9:g} ns)")
    if c.error:
        return [f"{head}: {c.error}"]
    out = [f"{head}: hot loop {c.loop_nh:.2f} nH, {c.loop_mohm:.0f} mΩ at {c.loop_f / 1e6:g} MHz; "
           f"L·di/dt ≈ {c.overshoot_v:.1f} V" + (f" ({c.overshoot_v / c.v_in * 100:.0f} % of {c.v_in:g} V)" if c.v_in
                                                 else "") + f"  [{c.seconds:.1f}s]"]
    if c.ringing:
        r = c.ringing
        out.append(f"    ringing with C_oss: {r['f'] / 1e6:.0f} MHz, Q {r['q']:.1f}"
                   + (f", overshoot ≈ {r['overshoot_v']:.1f} V" if r.get("overshoot_v") else ""))
    caps = [x for x in c.caps if not x["lisn"]][:6]
    if caps:
        out.append(f"    capacitors (share of the loop current at {c.loop_f / 1e6:g} MHz / {c.f_sw / 1e3:g} kHz): "
                   + ", ".join(f"{x['name']} {x['share_hf'] * 100:.0f} %/{(x['share_sw'] or 0) * 100:.0f} %"
                               for x in caps))
    for path in c.dc:
        via = path["vias"][0] if path["vias"] else None
        out.append(f"    DC {path['name']}: {path['current_a']:.3g} A, {dc_resistance(path)}, {path['drop_mv']:.1f} mV, "
                   f"{path['loss_w'] * 1e3:.0f} mW; {path['sustained_a_mm2']:.0f} A/mm² sustained "
                   f"(peak {path['peak_a_mm2']:.0f}) on {path['sustained_layer']}"
                   + (f"; {via['name']} {via['current_a']:.2f} A ({via['a_mm2']:.0f} A/mm²"
                      + (f", {via['load_pct']:.0f} % of IPC-2221" if via.get('load_pct') is not None else "")
                      + ")" if via else ""))
    if c.worst:
        w = c.worst
        out.append(f"    radiated: worst {w['e_dbuv']:.1f} dBµV/m @ {w['f'] / 1e6:.4g} MHz (margin {w['margin_db']:+.1f} dB)")
    if c.conducted_worst:
        w = c.conducted_worst
        out.append(f"    conducted ({c.conducted_standard}): worst {w['v_dbuv']:.1f} dBµV @ {w['f'] / 1e6:.4g} MHz "
                   f"(margin {w['margin_db']:+.1f} dB)")
    elif not c.conducted_standard:
        out.append("    conducted: not estimated (set input_connector)")
    out += ce101_lines(c)
    flt = getattr(c, "input_filter", None)
    if flt:
        out.append(f"    input filter (first-order guidance): {flt['need_db']:.0f} dB short at {flt['f_worst'] / 1e6:.4g} MHz; "
                   f"a π-filter with the input capacitors, f_c ≤ {flt['f_c'] / 1e3:.3g} kHz, e.g. "
                   f"L {_si2(flt['L'], 'H')} + C {_si2(flt['C'], 'F')} at the connector, damped by "
                   f"{flt['R_damp']:.2g} Ω + {_si2(flt['C_damp'], 'F')} across C; check it against the converter's "
                   "input impedance (Middlebrook)")
    if c.cm:
        cm = c.cm
        line = (f"    common mode ({cm['standard']}): the switch node ({cm['v_sw']:g} V swing) may have at most "
                f"{cm['budget_pf']:.2g} pF to the chassis/heatsink (binding at {cm['f'] / 1e6:.4g} MHz)")
        if cm.get("worst"):
            w = cm["worst"]
            line += (f"; with {cm['c_stray_pf']:g} pF: {w['v_dbuv']:.1f} dBµV @ {w['f'] / 1e6:.4g} MHz "
                     f"(margin {w['margin_db']:+.1f} dB)")
        out.append(line)
    if c.resonance:
        r = c.resonance
        pair = f" ({r['pair'][0]}–{r['pair'][1]})" if r.get("pair") else ""
        out.append(f"    plane resonance: loop moment {r['rise_db']:+.1f} dB at {r['f'] / 1e6:.4g} MHz{pair}")
    nodes = sum(int(g.node.sum()) for g in c.layer_grids.values()) if c.layer_grids else 0
    out.append(f"    model: {nodes} nodes at {c.grid.d:.2f} mm on {', '.join(c.plane_layers)}"
               + (f"; board-wide {c.cavity['nodes']} nodes at {c.cavity['d_mm']:.2f} mm" if c.cavity else ""))
    lp = getattr(c, "loop", None)
    if lp and not lp.get("error") and lp.get("fc"):
        from ..components.values import format_si
        gm = f", gain margin {lp['gm_db']:.1f} dB" if lp.get("gm_db") is not None else ""
        out.append(f"    control loop ({lp['mode']} mode, {lp['controller']}): crossover "
                   f"{format_si(lp['fc'], 'Hz')}, phase margin {lp['pm']:.0f}°{gm}")
    for resp in getattr(c, "responses", ()):
        out.extend(response_lines(resp))
    if getattr(c, "victims", None):
        from ..analysis.victims import victim_text
        out.append("    victims (at the switching edges):")
        out += [f"      {victim_text(r)}" for r in c.victims]
    for n in c.notes:
        out.append(f"    note: {n}")
    out += fix_lines(getattr(c, "fixes", None) or [])
    return out


def fix_lines(rows: list[dict]) -> list[str]:
    """The fix advisor's design changes for a stage, one line each with its parts (analysis/fix_advisor.py)."""
    if not rows:
        return []
    out = ["    how to fix it (predicted on this stage's own solves):"]
    for o in rows:
        oc = o["outcome"] or {}
        figs = ", ".join(f"{name} {oc[k]:+.1f} dB" for k, name in (("radiated_db", "radiated"),
                                                                   ("conducted_db", "conducted"))
                         if oc.get(k) is not None)
        if oc.get("overshoot_v") is not None:
            figs += f", overshoot {oc['overshoot_v']:.1f} V"
        if oc.get("loss_w"):
            figs += f", +{oc['loss_w']:.2g} W"
        clears = f"; clears {', '.join(o['clears'])}" if o["clears"] else ""
        out.append(f"      - {o['title']}" + (f": {figs}" if figs else "") + clears)
        out += [f"          {p}" for p in o["parts"]]
        out += [f"          ({n})" for n in o["notes"] if o["kind"] in ("cm", "loop", "combined", "now")]
    return out


def pdn_text(d) -> list[str]:
    """A PDN rail in the console report: the target, the source, per IC its worst |Z| (and margin), the highest
    peak and the inductance it sees, the decaps near it."""
    from ..components.values import format_si
    head = f"PDN {d.name} ({d.rail} / {d.ground}" + (f", {d.voltage:g} V" if d.voltage else "") + ")"
    if d.error:
        return [f"{head}: {d.error}"]
    out = [head + (f": target {format_si(d.target_ohm, 'Ω')} to {format_si(d.check_to, 'Hz')}" if d.target_ohm
                   else ": no target (set i_step)")]
    if d.source:
        out.append(f"    source: {d.source}")
    for p in d.ports:
        z = max(p["z_ohm"])
        line = f"    {p['part']} ({len(p['pins'])} supply pin(s)): |Z| up to {format_si(z, 'Ω')}"
        if p.get("worst"):
            w = p["worst"]
            line += (f"; worst below {format_si(d.check_to, 'Hz')}: {format_si(w['z_ohm'], 'Ω')} @ "
                     f"{format_si(w['f'], 'Hz')} (margin {w['margin_db']:+.1f} dB, held by {w['holds']})")
        if p["peaks"]:
            pk = p["peaks"][0]
            line += f"; highest peak {format_si(pk['z_ohm'], 'Ω')} @ {format_si(pk['f'], 'Hz')}"
        if p.get("l_nh"):
            line += f"; {p['l_nh']:.2f} nH above the decaps"
        out.append(line)
    near = []
    for c in d.decaps:
        if c["near"]:
            where = ", ".join(f"{n['part']} {n['mm']} mm" for n in c["near"])
            near.append(f"{c['ref']} {c['value']} ({where})")
    if near:
        out.append("    decaps: " + "; ".join(near))
    for resp in getattr(d, "responses", ()):
        out.extend(response_lines(resp))
    return out


def response_lines(r) -> list[str]:
    """A response (analysis/transfer.Response) in the console report: its poles and zeros as natural frequency and
    Q, exact or fitted (with the fit's largest error)."""
    from ..analysis.transfer import fmt_root, pz_rows

    def roots(rows) -> str:
        """'2.74 MHz, 401 MHz Q 740' (the first six)."""
        text = ", ".join(fmt_root(x) for x in rows[:6])
        return text + (" …" if len(rows) > 6 else "") if rows else "none"
    how = "exact" if r.exact else (f"fitted, max error {r.fit_error_db:.2f} dB" if r.fit_error_db is not None
                                   else "no fit")
    return [f"    {r.name}: poles {roots(pz_rows(r.poles))}; zeros {roots(pz_rows(r.zeros))} ({how})"]


def si_text(si) -> list[str]:
    """The signal-integrity figures in the console report: each net's Z0 (range, mean, target), each pair's Zdiff,
    routed lengths and skew, the strongest crosstalk and the lowest via-stub resonance."""
    if si is None or not (si.nets or si.pairs or si.crosstalk or si.stubs or getattr(si, "delays", None)):
        return []
    out = [f"Signal integrity ({si.sections} cross-section(s) solved)"]
    for n in si.nets:
        s = n.summary()
        if "z_mean" not in s:
            out.append(f"    Z0 {n.net}: no cross-section could be solved (no reference copper near it?)")
            continue
        out.append(f"    Z0 {n.net}: {s['z_mean']:.1f} Ω ({s['z_min']:.1f}-{s['z_max']:.1f}) over "
                   f"{s['length_mm']:.1f} mm"
                   + (f"; target {n.target:g} Ω ± {n.tol * 100:g} %: {s['off_mm']:.1f} mm outside" if n.target
                      else ""))
    for p in si.pairs:
        s = p.summary()
        lengths = " / ".join(f"{v:.1f}" for v in s["length_mm"].values())
        z = (f"Zdiff {s['zdiff_mean']:.1f} Ω ({s['zdiff_min']:.1f}-{s['zdiff_max']:.1f})" if "zdiff_mean" in s
             else "never coupled")
        out.append(f"    pair {s['pair']}: {z}" + (f", target {p.spec.zdiff:g} Ω" if p.spec.zdiff else "")
                   + f"; {lengths} mm, skew {s['skew_ps']:.0f} ps (limit {s['skew_limit_ps']:.0f} ps)"
                   + (f"; {s['uncoupled_mm']:.1f} mm uncoupled" if s["uncoupled_mm"] > 0.5 else ""))
    for x in sorted(si.crosstalk, key=lambda r: -max(r["next"], r["fext"]))[:3]:
        out.append(f"    crosstalk {x['aggressor']} -> {x['victim']}: {x['coupled_mm']:.1f} mm on {x['layer']}, "
                   f"{x['gap_mm']:.2f} mm apart: NEXT {x['next'] * 100:.2f} %, FEXT {x['fext'] * 100:.2f} %")
    for d in getattr(si, "delays", ()):
        out.append(f"    tuning profile {d['profile']}: {d['net']} on {d['layer']}: KiCad {d['kicad_ps_cm']:.1f} "
                   f"ps/cm, cross-sections {d['solved_ps_cm']:.1f} ps/cm (a tuned delay comes out "
                   f"{100 / d['ratio']:.0f} %)")
    if si.stubs:
        low = min(si.stubs, key=lambda r: r["f_res"])
        out.append(f"    via stubs: {len(si.stubs)}, the lowest resonance {low['f_res'] / 1e9:.1f} GHz ({low['net']}, "
                   f"{low['stub_mm']:.2f} mm, knee {low['knee'] / 1e9:.2f} GHz)")
    return out


def to_text(issues: list[Issue], routes: list = (), warnings: list[str] = (), converters: list = (),
            dc_loads: list = (), pdn: list = (), si=None) -> str:
    """The console report: the board's notes, each power stage, DC load and route with its key figures, then the
    issues (waived ones marked) and the totals."""
    lines = []
    for w in warnings:
        lines.append(f"note: {w}")
    for c in converters:
        lines.extend(converter_text(c))
    for d in dc_loads:
        lines.extend(dc_load_text(d))
    for d in pdn:
        lines.extend(pdn_text(d))
    lines.extend(si_text(si))
    for rr in routes:
        head = f"{rr.net} {rr.driver}->{rr.receiver}"
        if rr.error:
            lines.append(f"{head}: {rr.error}")
            continue
        shares = "/".join(f"{l} {s * 100:.0f}%" for l, s in sorted(rr.layer_share.items(), key=lambda kv: -kv[1])
                          if s >= 0.05)
        parts = [f"return on {shares or rr.reference_layer} (h={rr.height_mm:.3f} mm)"]
        if rr.solves:
            parts.append(f"L={rr.solves[-1]['L_nH']:.2f} nH")
        if rr.l_ratio:
            parts.append(f"{rr.l_ratio:.2f}x ideal")
        if rr.m_excess_db is not None:
            parts.append(f"moment {rr.m_excess_db:+.1f} dB")
        if rr.worst:
            parts.append(f"worst {rr.worst['e_dbuv']:.1f} dBuV/m @ {rr.worst['f'] / 1e6:.4g} MHz "
                         f"(margin {rr.worst['margin_db']:+.1f} dB)")
        lines.append(f"{head}: " + ", ".join(parts) + f"  [{rr.seconds:.1f}s]")
        for resp in getattr(rr, "responses", ()):
            lines.extend(response_lines(resp))
        if rr.resonance:
            r = rr.resonance
            pair = f" ({r['pair'][0]}–{r['pair'][1]})" if r.get("pair") else ""
            lines.append(f"    plane resonance: loop moment {r['rise_db']:+.1f} dB at {r['f'] / 1e6:.4g} MHz{pair}")
        if rr.cavity:
            lines.append(f"    board-wide model: {rr.cavity['nodes']} nodes at {rr.cavity['d_mm']:.2f} mm, "
                         f"{len(rr.cavity_solves)} harmonics solved")
        for t in rr.transfers[:3]:
            amount = f"{t['fraction'] * 100:.0f}% of" if t["fraction"] < 1 else f"{t['fraction']:.1f}x"
            if t.get("kind") == "capacitance":
                lines.append(f"    plane displacement: {'–'.join(t['layers'])} carries {amount} the signal current "
                             f"through the plane capacitance at {t.get('f', 0) / 1e6:.4g} MHz (board-wide model"
                             + ("; above 1x: circulating with the vias at a resonance)" if t["fraction"] >= 1
                                else ")"))
                continue
            lines.append(f"    transfer: {t['name']} ({'→'.join(t['layers'])}) carries {amount} "
                         f"the signal current, {t['distance_mm']:.1f} mm from the route")
        for n in rr.notes:
            lines.append(f"    note: {n}")
    for k, i in enumerate(issues, 1):
        flag = f" (waived: {i.waiver_reason})" if i.waived else ""
        lines.append(f"{k:3d}. {i.severity.upper():7s} {i.check:20s} {i.net}  @({i.location[0]:.2f}, "
                     f"{i.location[1]:.2f}) {i.layer}{flag}\n       {i.message}  [{i.fingerprint}]")
    counts = {s: sum(1 for i in issues if i.severity == s and not i.waived) for s in ("error", "warning", "info")}
    lines.append(f"{counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} info")
    return "\n".join(lines)


# --------------------------------------------------------------------------- baseline
def load_baseline(path: str) -> dict[str, dict]:
    """A baseline file's accepted issues {fingerprint: {check, net, message}}; an absent file is an empty
    baseline."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data.get("fingerprints", {})


def apply_baseline(issues: list[Issue], baseline: dict[str, dict]) -> int:
    """Mark the issues the baseline accepts as waived (reason "baseline"); returns how many."""
    n = 0
    for i in issues:
        if not i.waived and i.fingerprint in baseline:
            i.waived, i.waiver_reason = True, "baseline"
            n += 1
    return n


def write_baseline(path: str, issues: list[Issue]):
    """Write the current (not waived) issues as a baseline file, so CI only fails on new ones from now on."""
    data = {"version": 1, "fingerprints": {i.fingerprint: {"check": i.check, "net": i.net, "message": i.message}
                                           for i in issues if not i.waived}}
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- fix suggestions / comparison
def _pair(v, fmt: str) -> str:
    """A before → after pair of values, each formatted with fmt ("–" for a missing one)."""
    a, b = v
    sa = fmt.format(a) if a is not None else "–"
    sb = fmt.format(b) if b is not None else "–"
    return f"{sa} → {sb}"


def _worst_pair(r: dict) -> str:
    """'+13.9 dB @ 740 MHz → +11.0 dB @ 740 MHz', and each run's own value when comparing both runs over the
    same harmonics changed it."""
    def one(m, f):
        """One run's margin with its frequency."""
        return "–" if m is None else f"{m:+.1f} dB" + (f" @ {f / 1e6:.4g} MHz" if f else "")
    (mb, ma), (fb, fa) = r["worst_margin_db"], r.get("worst_f") or (None, None)
    text = f"{one(mb, fb)} → {one(ma, fa)}"
    sampled = r.get("worst_margin_sampled_db")
    if r.get("aligned") and sampled and any(s is not None and m is not None and abs(s - m) > 0.05
                                            for s, m in zip(sampled, (mb, ma))):
        text += f" (each run's own refinement: {_pair(sampled, '{:+.1f} dB')})"
    return text


KEPT_TEXT = "not simulated again (no fix on or near them, and far below the limit; their baseline results stand): "


def comparison_text(rows: list[dict], fixed: list[Issue], new: list[Issue], kept=()) -> str:
    """A before/after comparison for the console: per power stage and route its figures before → after, then
    the issues fixed and new, and what was not simulated again."""
    lines = []
    for r in rows:
        if r.get("kind") == "converter":
            lines.append(f"{r['net']} hot loop: L {_pair(r['loop_nh'], '{:.2f} nH')}, radiated margin "
                         f"{_pair(r['worst_margin_db'], '{:+.1f} dB')}, conducted margin "
                         f"{_pair(r['conducted_margin_db'], '{:+.1f} dB')}")
            continue
        lines.append(f"{r['net']} {r['route']}: L/ideal {_pair(r['l_ratio'], '{:.2f}')}, moment "
                     f"{_pair(r['m_excess_db'], '{:+.1f} dB')}, worst margin {_worst_pair(r)}, "
                     f"resonance {_pair(r['resonance_db'], '{:+.1f} dB')}")
    lines.append(f"fixed: {len(fixed)} issue(s)" + "".join(f"\n   - [{i.check}] {i.net}: {i.message}" for i in fixed))
    lines.append(f"new:   {len(new)} issue(s)" + "".join(f"\n   - [{i.check}] {i.net}: {i.message}" for i in new))
    if kept:
        lines.append(KEPT_TEXT + ", ".join(kept))
    return "\n".join(lines)


def comparison_json(board_path: str, rows: list[dict], fixed: list[Issue], new: list[Issue]) -> str:
    """The comparison as JSON (compare --format json)."""
    return json.dumps({"tool": "groundtruth", "board": board_path, "routes": rows,
                       "fixed": [i.to_dict() for i in fixed], "new": [i.to_dict() for i in new]},
                      indent=2, default=_jsonable, ensure_ascii=False)


def suggestions_text(res) -> str:
    """The fix search for the console: every suggestion with its predicted effect (selected ones starred), then
    the verification's comparison."""
    lines = []
    if not res.suggestions:
        lines.append("no suggestions: nothing to fix, or no legal spot for a fix")
    for s in res.suggestions:
        mark = "*" if s.selected else " "
        pred = ""
        if s.delta_db is not None:
            at = f" at {s.f_eval / 1e6:.4g} MHz" if s.f_eval else ""
            pred = f"   predicted: loop moment {s.delta_db:+.1f} dB{at}, L {s.delta_l * 100:+.0f} %"
            if s.note:
                pred += f" ({s.note})"
        elif s.note:
            pred = f"   ({s.note})"
        issue = f" [{s.issue.check} {s.issue.fingerprint}]" if s.issue else ""
        lines.append(f"{s.id:>4s}{mark} {s.kind:6s} {s.net}  @({s.location[0]:.2f}, {s.location[1]:.2f}) {s.layer}"
                     f"\n       {s.title}: {s.message}{pred}{issue}")
    if res.selected:
        lines.append("* = selected; verified together:" if res.after is not None else "* = selected")
    if res.after is not None:
        lines.append(comparison_text(res.comparison, res.fixed, res.new, res.kept))
    return "\n".join(lines)


def suggestions_json(board_path: str, res) -> str:
    """The fix search as JSON: the suggestions and, when verified, the comparison."""
    return json.dumps({"tool": "groundtruth", "board": board_path,
                       "suggestions": [s.to_dict() for s in res.suggestions],
                       "verification": {"routes": res.comparison, "fixed": [i.to_dict() for i in res.fixed],
                                        "new": [i.to_dict() for i in res.new], "kept": list(res.kept)}
                       if res.after is not None else None},
                      indent=2, default=_jsonable, ensure_ascii=False)
