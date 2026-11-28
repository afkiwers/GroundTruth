"""Op-amp outputs within their supply: a signal path's large-signal step and its output_saturation check.

The path's circuit (analysis/analog_net, emi/mna.Circuit) is a small-signal one: its supplies are AC ground and its
step is per volt at the driver, so a gain of 5 drew a 5 V output from an op-amp on 3.3 V (the user, 2026-11-28:
"[the op-amp] cannot provide 5V it's supply is 3.3V and its a rail to rail OPA"; "make sure the sim accounts for
supply and rail limits"). Here each amplifier's output is held within its limits [A123]:

- **Limits** (amp_limits): the voltages on the part's supply pins (bias._supply_pins: power pins, or V+ / V− / VCC
  ... by name), as safety.voltage_model knows them (rail names, PDN rails, the settings' own); one known supply alone
  is taken against 0 V (single supply). Its output gets to each rail less its Output Swing High / Low field
  (models.swing_value: volts, or a share of the supply span; none: rail to rail). A part whose supply is not known is
  not limited (a note says so).
- **Operating point** (operating_point): the board's linear DC network (bias.dc_operating_point [Ho 1975] [A58])
  with the driver at its low level (NetSpec.offset_v, 0 V by default); other sources that only have a range (an IC's
  output pin) at the middle of it, named in a note.
- **Large signal**: the circuit's deviation from that operating point is linear except where an output is held, so
  each amplifier's limits become limits on its deviation (lo − V₀, hi − V₀) and Circuit.clamped_dc /
  clamped_step solve it exactly for the circuit: the DC at the driver's low and high levels, and the step from the
  one to the other, each output in volts (an output already at a rail at rest starts there) [Boyle 1974] [A123].
- **output_saturation**: an amplifier held at a limit at either end of the driver's swing (with the voltage it would
  need and the driver's range that stays linear), or only during the step (its overshoot reaches the rail: info).

Not modelled: slew rate, the output's current limit, the inputs' common-mode range (a rail-to-rail output need not
have rail-to-rail inputs), recovery time from saturation (the macromodel's gain node is held, so it recovers at once:
anti-windup, as [Boyle 1974]'s clamp)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

SAT_TOL = 1e-3          # V: an output this far past its limit (or held) counts as clipping (the solves' rounding)


@dataclass
class AmpLimit:
    """One amplifier channel's output limits: its name and part, the limits (V), its supply's rails and why."""
    name: str
    ref: str
    lo: float
    hi: float
    rails: tuple
    why: str


def amp_limits(board, net, vm) -> tuple[dict, list]:
    """{channel: AmpLimit} for the channels of a path's circuit (net: analog_net.AnalogNetwork) whose supply is known,
    and notes for the others. A part's supply: the voltages on its supply pins (bias._supply_pins); one known pin
    alone is taken against 0 V; the output's limits are the rails less its Output Swing High / Low
    (models.swing_headroom) [A123]."""
    from ..components.models import swing_headroom
    from .bias import _supply_pins
    out, notes = {}, []
    for name, ref in net.amp_ref.items():
        fp = board.footprint(ref)
        if fp is None:
            continue
        known = [n for n in _supply_pins(fp) if n in vm.peak]
        if not known:
            notes.append(f"{name}: the voltage of its supply pins is not known (a rail named like +3V3, a PDN rail or "
                         "safety: voltages: in the settings), so its output is not limited to it")
            continue
        lo_s = min(min(vm.lo(n), vm.hi(n)) for n in known)
        hi_s = max(max(vm.lo(n), vm.hi(n)) for n in known)
        if len(known) == 1:                       # a single supply named: against ground
            lo_s, hi_s = min(lo_s, 0.0), max(hi_s, 0.0)
        span = hi_s - lo_s
        if not span > 0:
            notes.append(f"{name}: its supply pins are all at {hi_s:g} V: its output is not limited")
            continue
        prm = net.amp_params.get(name, {})
        (d_hi, how_hi), (d_lo, how_lo) = (swing_headroom(prm.get("swing_hi"), lo_s, hi_s, "hi"),
                                          swing_headroom(prm.get("swing_lo"), lo_s, hi_s, "lo"))
        read = "; ".join(f"Output Swing {k}: {h}" for k, h in (("High", how_hi), ("Low", how_lo)) if h)
        if d_hi + d_lo >= span:
            mid = 0.5 * (lo_s + d_lo + hi_s - d_hi)
            notes.append(f"{name}: its Output Swing High and Low ({d_hi:.3g} V + {d_lo:.3g} V of headroom) leave no "
                         f"room in its {span:g} V supply: its output is held at {mid:.3g} V, between the two")
        how = "rail to rail" if not (d_hi or d_lo) else (
            f"{lo_s + d_lo:.3g} … {hi_s - d_hi:.3g} V (its Output Swing" + (f"; {read})" if read else ")"))
        out[name] = AmpLimit(name, ref, lo_s + d_lo, hi_s - d_hi, (lo_s, hi_s),
                             f"{ref}'s supply {lo_s:g} … {hi_s:g} V, {how}")
    return out, notes


def operating_point(board, config, spec, vm, nets, circuit_nets=()) -> tuple[dict, list]:
    """{net: DC volts} of `nets` with the driver at its low level (NetSpec.offset_v, else 0 V): the board's linear DC
    network (bias.dc_operating_point [Ho 1975] [A58]), every other source at its value (a range's middle: named in
    the notes). The path circuit's own nets (`circuit_nets`) whose voltage is not known otherwise are never sources:
    the circuit sets them (an MCU's ADC pin, typed bidirectional, on an op-amp's output made that net a source at mid
    supply and moved the output's operating point by 1.65 V: audit 2026-11-28). A net with no DC path to a source is
    left out."""
    from .bias import dc_operating_point
    drv = board.find_pin(spec.driver)
    low = float(spec.offset_v or 0.0)
    fixed = {drv.net: (low, low, "the driver's low level")} if drv is not None and drv.net else {}
    exclude = {n for n in circuit_nets if n not in vm.peak} - set(fixed)
    dcp = dc_operating_point(board, config, vm, fixed=fixed, exclude=exclude)
    out, ranged = {}, set()
    for n in nets:
        row = dcp.row(n) if n and n != "0" else None
        if n == "0":
            out[n] = 0.0
            continue
        if row is None:
            continue
        v = 0.0
        for s, k in row.items():
            lo, hi, _ = dcp.src[s]
            v += k * 0.5 * (lo + hi)
            if hi - lo > 1e-9 and abs(k) > 1e-6:
                ranged.add(s)
        out[n] = v
    notes = []
    if ranged:
        notes.append("output limits: the operating point takes " + ", ".join(sorted(ranged)) + " at the middle of "
                     "its range (an IC's output pin: anywhere within its supply)")
    return out, notes


def _grid(t: np.ndarray) -> np.ndarray:
    """A BDF2 grid (transfer.step_grid [Hairer 1993, §III.5]) over a small-signal step's window t: its first steps
    as fine as t's finest, at most STEP_POINTS-th of the window after."""
    from .transfer import STEP_POINTS, step_grid
    span = float(t[-1])
    d = np.diff(t)
    h0 = float(d[d > 0].min()) if np.any(d > 0) else span / STEP_POINTS
    return step_grid(span, max(h0, span * 1e-9), span / STEP_POINTS)[0]


def apply(board, config, spec, lumped, outs: list) -> tuple[list, list]:
    """The path's output limits (see the module) in its lumped circuit (analog_net.AnalogNetwork): each response of
    `outs` ([(Response, its lumped node, its net)]) with a step gets r.step["large"] (its large-signal step in
    volts: {"t", "y", "lin" (the same without limits), "final", "levels" (the driver's low, high), "rails" (an
    op-amp output's limits, else None), "held" (channels at
    a limit), "metrics"}); output_saturation findings for the channels held at the driver's low or high level, or
    during the step. (findings, notes)."""
    from .safety import voltage_model
    from .transfer import step_metrics
    ckt = lumped.circuit
    vm = voltage_model(board, config)
    limits, notes = amp_limits(board, lumped, vm)
    limits = {k: v for k, v in limits.items() if k in ckt.limiters}
    if not limits:
        return [], notes
    amp_nets = {nm: out for nm, _inp, _inn, out, _g in lumped.schematic.get("opamps", ())}
    want = set(amp_nets.values()) | {n for _, _, n in outs}
    dc0, more = operating_point(board, config, spec, vm, [n for n in want if n], lumped.nets)
    notes += more
    # an op-amp whose output has no operating point (an input with no DC path to a source: a capacitor in and no
    # bias resistor, an integrator, a quad's unused channel left open) is not limited; the DC network leaves out
    # everything joined to it (its output's resistors, an op-amp it feeds: bias.dc_operating_point), so the
    # operating points that are known do not depend on it
    lost = sorted(nm for nm, n in amp_nets.items() if n and n != "0" and n not in dc0)
    if lost:
        notes.append(f"output limits: the operating point of {', '.join(lost)} is not known (an input with no DC "
                     "path to a source: a capacitor in and no bias resistor, an integrator), so it is not limited, "
                     "nor are the voltages that depend on it")
    for nm in list(limits):
        if amp_nets.get(nm) not in dc0:
            del limits[nm]
    if not limits:
        return [], notes
    bounds = {nm: (L.lo - dc0[amp_nets[nm]], L.hi - dc0[amp_nets[nm]]) for nm, L in limits.items()}
    low = float(spec.offset_v or 0.0)
    amp = float(spec.amplitude_v)
    high = low + amp
    findings: list[dict] = []

    def at(nm):
        """Where a channel's finding points: its part."""
        fp = board.footprint(limits[nm].ref)
        return fp.position if fp is not None else (0.0, 0.0)
    # ---- DC at the driver's two levels: the linear demand, the held outputs, the driver's linear range
    x_lin, _ = ckt.clamped_dc(1.0, {})
    x_lo, held_lo = ckt.clamped_dc(0.0, bounds)
    x_hi, held_hi = ckt.clamped_dc(amp, bounds)
    window = [-math.inf, math.inf]           # the driver's range where no output is held (linear superposition)
    gains = {}
    for nm, L in limits.items():
        node = lumped.amp_out.get(nm)
        if node not in ckt.nodes:
            continue
        cv = ckt.output(node)
        g, v0 = float(cv @ x_lin), dc0[amp_nets[nm]]
        gains[nm] = g
        if abs(g) > 1e-12:
            a, b = sorted((low + (L.lo - v0) / g, low + (L.hi - v0) / g))
            window = [max(window[0], a), min(window[1], b)]
        elif not L.lo - SAT_TOL <= v0 <= L.hi + SAT_TOL:
            window = [math.inf, -math.inf]
    for nm, L in limits.items():
        if nm not in gains:
            continue
        v0, g = dc0[amp_nets[nm]], gains[nm]
        ends = [(lvl, held.get(nm), v0 + g * (lvl - low), float(ckt.output(lumped.amp_out[nm]) @ x) + v0)
                for lvl, held, x in ((low, held_lo, x_lo), (high, held_hi, x_hi))]
        clipped = [e for e in ends if e[1] or not L.lo - SAT_TOL <= e[3] <= L.hi + SAT_TOL]
        if not clipped:
            continue
        parts = []
        for lvl, side, need, real in clipped:
            # the side it is held at (clamped_dc's), else the nearer limit (an output past it by rounding only)
            upper = side == "hi" if side else abs(real - L.hi) <= abs(real - L.lo)
            parts.append(f"at the driver's {lvl:g} V it would need {need:.3g} V, held at {round(real, 9) + 0.0:.3g} V "
                         f"({'upper' if upper else 'lower'} limit)")
        if window[0] < window[1] and math.isfinite(window[0] + window[1]):
            lin = f"the path is linear for the driver within {window[0]:.3g} … {window[1]:.3g} V"
        elif window[0] < window[1]:
            lin = "the path is linear for the driver " + (f"above {window[0]:.3g} V" if math.isfinite(window[0])
                                                          else f"below {window[1]:.3g} V")
        else:
            lin = "no driver level keeps every output within its limits"
        on = "" if lumped.on_path(nm) else " (in the circuit around the path)"
        findings.append({"check": "output_saturation", "severity": None, "at": at(nm),
                         "message": f"{nm}{on}: its output clips at its limits {L.lo:.3g} … {L.hi:.3g} V "
                                    f"({L.why}): " + "; ".join(parts) + f" (the signal {low:g} … {high:g} V at the "
                                    f"driver; {lin}; set the path's Low level and Amplitude to the real signal)"})
    # ---- the large-signal step on each response's window, one run per distinct window
    runs: dict = {}
    for r, lnode, net_name in outs:
        st = getattr(r, "step", None)
        if not st or st.get("kind") == "load" or net_name not in dc0 or lnode not in ckt.nodes:
            continue
        t = np.asarray(st["t"], float)
        if len(t) < 3 or not t[-1] > 0:
            continue
        runs.setdefault((len(t), float(t[-1])), (t, []))[1].append((r, lnode, net_name))
    step_held: dict = {}
    for t, group in runs.values():
        grid = _grid(t)
        res = ckt.clamped_step([lnode for _, lnode, _ in group], grid, amp, bounds)
        rows = []
        for (r, _lnode, net_name), y, fin in zip(group, res["y"], res["final"]):
            v0 = dc0[net_name]
            # an op-amp's output, or a response on its output's net (a receiver right at it): its limits drawn
            nm = next((k for k in limits if amp_nets.get(k) == net_name), None)
            rows.append((r, nm, v0 + y, v0 + amp * np.interp(grid, t, np.asarray(r.step["y"], float)), v0 + fin))
        # a hold that matters: at a level's DC, or one that moves a response on its output by SAT_TOL or more, or
        # (no response on its output: a stage within the path) one held for at least 1 % of the window; an output
        # resting on its rail (a follower at 0 V on a single supply) touches it for nanoseconds in a step's first
        # wiggle, which cuts nothing worth a word
        cut: dict = {}
        for _r, nm, y_abs, lin, _f in rows:
            if nm is not None:
                cut[nm] = max(cut.get(nm, 0.0), float(np.max(np.abs(lin - y_abs))))
        held = {nm: span for nm, span in res["held"].items()
                if nm in held_lo or nm in held_hi or (cut[nm] >= SAT_TOL if nm in cut
                                                      else span[1] - span[0] >= 0.01 * float(grid[-1]))}
        for nm, span in held.items():
            step_held.setdefault(nm, span)
        for r, nm, y_abs, lin, final in rows:
            rails = (limits[nm].lo, limits[nm].hi) if nm is not None else None
            dev = final - y_abs[0]
            metrics = step_metrics(grid, y_abs - y_abs[0], dev) if abs(dev) > 1e-9 * max(1.0, abs(amp)) else None
            r.step["large"] = {"t": grid, "y": y_abs, "lin": lin, "final": final, "levels": (low, high),
                               "rails": rails, "held": sorted(held), "metrics": metrics}
            if held:
                r.notes.append("large signal: " + "; ".join(
                    f"{k} held at its limit from {_fmt_t(a)} to {_fmt_t(b)}" for k, (a, b) in sorted(held.items()))
                    + f" (the driver {low:g} → {high:g} V)")
    for nm, (a, b) in step_held.items():
        named = {f["message"].split(":")[0].split(" (")[0] for f in findings}     # the channels with a finding
        if nm in held_lo or nm in held_hi or nm not in limits or nm in named:
            continue
        L = limits[nm]
        findings.append({"check": "output_saturation", "severity": "info", "at": at(nm),
                         "message": f"{nm}: its output reaches its limits {L.lo:.3g} … {L.hi:.3g} V ({L.why}) during "
                                    f"the step (from {_fmt_t(a)} to {_fmt_t(b)}, the driver {low:g} → {high:g} V): "
                                    "its overshoot is cut, though both levels are within them"})
    return findings, notes


def _fmt_t(t: float) -> str:
    """A time for a note: with its SI prefix."""
    from .transfer import fmt_s
    return fmt_s(float(t))
