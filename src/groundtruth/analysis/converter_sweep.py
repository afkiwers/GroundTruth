"""A power stage at other operating points: its input voltage and its load current swept, each with the other at its
nominal value (the stage's `sweep: {v_in, i_out, outputs}`), on the stage's own solves.

The user: "add a voltage/current sweep option to the converters"; two separate sweeps, the outputs chosen per stage,
run with every Simulate, as a motor bridge's sweep (analysis/bridge.py `sweep`). Only V_in and I_out change; the
switching frequency, V_out and the board stay, so every transfer the Simulate solved holds and nothing is solved
again, except the temperatures when they are asked for:

- "emissions": the hot loop's pulse at the point (its current, duty, the inductor's ripple on it, the recovery)
  through the stage's solved loop: radiated and conducted margins, the switch node's overshoot, the common-mode
  budget (fix_advisor.evaluate on the stage's setup at the point; a boost's input ripple harmonics recomputed);
- "losses": the parts' and the copper's losses and the efficiency (efficiency.losses_at, the DC paths scaled to the
  point's currents) [A114];
- "ripple": the ripple at the controller's FB pin (fb_ripple.at_point, the network's and the copper's transfers
  kept from the stage's own analysis) and the control loop's phase margin, crossover and gain margin
  (loop.analyse_loop at the point: analytic);
- "thermal": the temperatures, a thermal solve per point (thermal.run_thermal, draft mesh) of the stage with its
  losses at the point and its DC paths scaled [A114]: slow, so off unless asked for.

The rows are plain data (the JSON report keeps them); no issues are raised for the points (the operating point's
own are), as for a bridge's sweep. Source keys in square brackets: docs/REFERENCES.md.
"""
from __future__ import annotations

import copy
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Optional

OUTPUTS = ("emissions", "losses", "ripple", "thermal")
DEFAULT_OUTPUTS = ("emissions", "losses", "ripple")
V_IN_FAR = 0.3                  # a V_in this far (share) from the nominal: the DC-duty assumption's note [A114]


@dataclass
class ConverterSweep:
    """A stage's operating-point sweep (see the module): one row per point."""
    net: str
    kind: str = "converter_sweep"
    topology: str = "buck"
    v_in: float = 0.0               # the nominal point
    i_out: float = 0.0
    v_out: Optional[float] = None
    f_sw: float = 0.0
    outputs: list = field(default_factory=list)
    rows: list = field(default_factory=list)    # [{axis "v_in" | "i_out", v_in, i_out, duty, delta_i, nominal, ...}]
    limits: dict = field(default_factory=dict)  # {"fb_min": V, "fb_max": V, "warn_c", "max_c", "margin_db"}
    notes: list = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0

    def label(self) -> str:
        """Its name in the results' list of views."""
        return f"{self.net}  sweep (V in, I out)"

    def axis(self, name: str) -> list:
        """The rows of one sweep ("v_in" or "i_out"), in order of their swept value."""
        rows = [r for r in self.rows if r["axis"] == name]
        return sorted(rows, key=lambda r: r[name])

    def summary(self) -> dict:
        """As plain data for the JSON report."""
        return asdict(self)


def points(spec) -> list[tuple[str, float, float]]:
    """[(axis, v_in, i_out)]: each swept V_in at the nominal load, each swept I_out at the nominal V_in, both with
    the nominal point itself (so each curve passes through it), each value once."""
    out = []
    if spec.sweep_v_in:
        for v in sorted(set(spec.sweep_v_in) | {spec.v_in}):
            out.append(("v_in", float(v), float(spec.i_out)))
    if spec.sweep_i_out:
        for i in sorted(set(spec.sweep_i_out) | {spec.i_out}):
            out.append(("i_out", float(spec.v_in), float(i)))
    return out


def _emissions(setup, spec_at, i_pulse: float, duty: float, v_in: float, ripple: float) -> dict:
    """The emission figures at the point: fix_advisor.evaluate on a copy of the stage's setup whose result carries
    the point's pulse (the solved transfers and moments are the setup's own) [A114]."""
    from ..emi.conducted import frequency_range
    from .fix_advisor import evaluate
    from .power import ripple_harmonics
    res_at = replace(setup.res, pulse_a=i_pulse, duty=duty, v_in=v_in, pulse_ripple_a=ripple)
    s = copy.copy(setup)
    s.spec, s.res = spec_at, res_at
    if spec_at.topology == "boost" and setup.cond_currents is not None and setup.standard != "none":
        # a boost's conducted source is its input ripple: its harmonics at this point, on the same frequencies
        d_sw = 1 - v_in / spec_at.v_out
        di = (res_at.ripple or {}).get("L") and v_in * d_sw / (res_at.ripple["L"] * spec_at.f_sw)
        if di:
            rng = frequency_range(setup.standard, setup.standard_class)
            s.cond_freqs, s.cond_currents = ripple_harmonics(spec_at.f_sw, di, d_sw, rng[1] * 1.01)
    o = evaluate(s)
    return {"radiated_db": o.radiated_db, "radiated_f": o.radiated_f, "conducted_db": o.conducted_db,
            "conducted_f": o.conducted_f, "overshoot_v": o.overshoot_v, "cm_budget_pf": o.cm_budget_pf}


def _thermal(board, config, res, spec_at, losses: dict) -> dict:
    """The temperatures at the point: one thermal solve (draft mesh) of the stage with its losses at the point and
    its DC paths, maps and vectors scaled to the point's currents [A114]."""
    from .power import _rescale_path
    from .thermal import run_thermal
    probe = copy.copy(res)
    for name in ("dc_maps", "dc_vectors", "dc_terminal", "dc_branch", "dc_ripple_sq", "dc_ripple_branch"):
        if isinstance(getattr(res, name, None), dict):
            setattr(probe, name, dict(getattr(res, name)))
    probe.dc = []
    for p, new in zip(res.dc, losses["paths"]):
        q = dict(p, vias=[dict(v) for v in p.get("vias", [])], parts=[dict(x) for x in p.get("parts", [])])
        _rescale_path(probe, q, (new["current_a"] / p["current_a"]) if p.get("current_a") else 1.0)
        probe.dc.append(q)
    probe.input = dict(res.input or {}, i_in_a=losses["i_in"])
    cfg = replace(losses["config"], thermal=replace(config.thermal, quality="draft"))
    th, _ = run_thermal(board, cfg, [probe])
    if th.error:
        return {"thermal_error": th.error}
    hot = max(th.parts, key=lambda p: p.get("t_c", 0.0)) if th.parts else None
    return {"t_max_c": th.t_max, "t_part_c": hot["t_c"] if hot else None, "hot_part": hot["ref"] if hot else ""}


def sweep(model, setup, fb=None) -> Optional[ConverterSweep]:
    """The stage's operating-point sweep (see the module), None without sweep values. `fb`: the stage's own
    FbRippleResult (its transfers serve every point)."""
    from .efficiency import losses_at
    from .loop import analyse_loop
    from .thermal import ripple_pp
    spec, res, config = setup.spec, setup.res, model.config
    pts = points(spec)
    if not pts:
        return None
    t0 = time.time()
    outputs = [o for o in (spec.sweep_outputs or DEFAULT_OUTPUTS) if o in OUTPUTS]
    out = ConverterSweep(spec.name, topology=spec.topology, v_in=spec.v_in or res.v_in or 0.0, i_out=spec.i_out,
                         v_out=spec.v_out, f_sw=spec.f_sw, outputs=outputs)
    out.limits["margin_db"] = config.rules.emi_margin_db
    out.limits["warn_c"], out.limits["max_c"] = config.thermal.warn_c, config.thermal.max_c
    if fb is not None and not fb.error:
        out.limits.update({"fb_" + k: v[0] for k, v in fb.limits.items()})
    if not out.v_in or not spec.f_sw:
        out.error = "the sweep needs the stage's V in and f_sw"
        return out
    if any(abs(v - out.v_in) > V_IN_FAR * out.v_in for a, v, _ in pts if a == "v_in"):
        out.notes.append(f"V in more than {V_IN_FAR * 100:.0f} % from the nominal: the switch node's DC path keeps the "
                         "nominal duty's share between the switches [A114]")
    if "thermal" in outputs:
        try:
            import numba  # noqa: F401  (the thermal solver's)
        except ImportError:
            outputs.remove("thermal")
            out.notes.append("temperatures not swept: the thermal solver is not installed (the thermal extra)")
    t_thermal = 0.0
    for axis, v_in, i_out in pts:
        row = {"axis": axis, "v_in": v_in, "i_out": i_out,
               "nominal": abs(v_in - out.v_in) < 1e-12 and abs(i_out - spec.i_out) < 1e-12}
        out.rows.append(row)
        spec_at = replace(spec, v_in=v_in, i_out=i_out, duty=None if spec.v_out else spec.duty)
        if spec.topology == "buck" and spec.v_out and v_in <= spec.v_out:
            row["error"] = f"V in {v_in:g} V is not above V out {spec.v_out:g} V"
            continue
        if spec.topology == "boost" and spec.v_out and v_in >= spec.v_out:
            row["error"] = f"V in {v_in:g} V is not below V out {spec.v_out:g} V"
            continue
        i_pulse, duty = spec_at.pulse()
        ripple = ripple_pp(model.board, config, spec_at, duty, v_in) or 0.0
        row.update(duty=duty, delta_i=ripple)
        try:
            if "emissions" in outputs:
                row.update(_emissions(setup, spec_at, i_pulse, duty, v_in, ripple))
            losses = None
            if "losses" in outputs or "thermal" in outputs:
                losses = losses_at(model.board, config, spec_at, res, i_out, v_in, duty, i_ref=spec.i_out)
                top = max(losses["rows"], key=lambda r: r["power_w"]) if losses["rows"] else None
                row.update(p_loss_w=losses["p_loss"], efficiency=losses["efficiency"], i_in_a=losses["i_in"],
                           worst_part=top["ref"] if top else "", worst_part_w=top["power_w"] if top else 0.0)
            if "ripple" in outputs:
                from .fb_ripple import at_point
                got = at_point(fb, v_in, duty, ripple) if fb is not None else None
                if got is not None:
                    row.update(fb_pp=got["fb_pp"], fb_tap_pp=got["tap_pp"], fb_status=got["status"])
                lr = analyse_loop(model.board, config, spec_at)
                if lr is not None and not lr.error and lr.pm is not None:
                    row.update(pm_deg=lr.pm, fc_hz=lr.fc, gm_db=lr.gm_db)
            if "thermal" in outputs and losses is not None:
                t1 = time.time()
                row.update(_thermal(model.board, config, res, spec_at, losses))
                t_thermal += time.time() - t1
        except Exception as e:                      # noqa: BLE001 - one point's failure is its row's, not the run's
            row["error"] = str(e)
    if t_thermal:
        out.notes.append(f"temperatures: a thermal solve per point (draft mesh), {t_thermal:.0f} s in all")
    out.seconds = time.time() - t0
    return out


def sweep_text(s: ConverterSweep) -> list[str]:
    """The sweep as a table for the console report and the view's tooltip."""
    if s.error:
        return [f"sweep: {s.error}"]
    head = ["V in", "I out", "duty"]
    cols = []
    if "emissions" in s.outputs:
        cols += [("radiated_db", "rad dB", "{:+.1f}"), ("conducted_db", "cond dB", "{:+.1f}"),
                 ("overshoot_v", "overshoot V", "{:.3g}")]
    if "losses" in s.outputs:
        cols += [("p_loss_w", "loss W", "{:.3g}"), ("efficiency", "eff %", "{:.1%}")]
    if "ripple" in s.outputs:
        cols += [("fb_pp", "FB mV", "{:.3g}"), ("pm_deg", "PM °", "{:.0f}")]
    if "thermal" in s.outputs:
        cols += [("t_max_c", "T max °C", "{:.0f}"), ("t_part_c", "T part °C", "{:.0f}")]
    out = [f"sweep of {s.net} ({len(s.rows)} points, {s.seconds:.0f} s): " + "  ".join(head + [c[1] for c in cols])]
    for r in sorted(s.rows, key=lambda r: (r["axis"], r[r["axis"]])):
        if r.get("error"):
            out.append(f"  {r['v_in']:g} V  {r['i_out']:g} A: {r['error']}")
            continue
        vals = [f"{r['v_in']:g} V", f"{r['i_out']:g} A", f"{r.get('duty', 0):.3f}"]
        for key, _, fmt in cols:
            x = r.get(key)
            vals.append("–" if x is None else (fmt.format(x * 1e3) if key == "fb_pp" else fmt.format(x)))
        out.append("  " + "  ".join(vals) + ("  (nominal)" if r.get("nominal") else ""))
    return out + [f"  {n}" for n in s.notes]
