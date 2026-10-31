"""Thermal solve of the power stages (docs/THERMAL_PLAN.md, M2, option A): GroundTruth's DC path currents and the
parts' losses heat PCB Thermal's 3-D thermal model (thermal/solver) of the same board (thermal/adapter.py).

Heat in the copper: every power stage's DC paths (switch node → inductor, input, output and ground returns, each
with its prescribed current, the switching split included) are resampled onto one grid per copper layer and
added as vectors, because paths share copper (GND carries both returns: adding their squared densities would be
wrong). Heat per sample: ρ(T)·|J|²·t·A, with copper resistivity following temperature (α = 0.00393 /K [IEC 60028]): the
currents stay as prescribed and the heat is rescaled with the local temperature each coupled iteration (first
order: the current pattern itself is not re-solved with temperature-dependent conductivity [A15]).

Heat in the parts (through their pads, as PCB Thermal does; the loss models [Erickson 2001]): switch conduction
(D·I²·R_DS(on) high side, (1−D)·I²·R_DS(on) low side, a catch diode (1−D)·I·V_f), switching on the hard-switched
transistor (a buck's high side, a boost's low side): the overlap ½·V·I·t_edge·f_sw per hard edge (synchronous: the
turn-on edge only; with a diode both) and the output charge, Q_oss·V·f_sw synchronous (½·Q_oss·V·f_sw with a diode
[A16]; C_oss·V² in place of Q_oss·V when no q_oss is given), a synchronous rectifier's reverse conduction in the two
dead times 2·t_dead·f_sw·I·V_SD (taken from its channel's share [A48]) and its recovered charge Q_rr·V·f_sw in the
hard-switched transistor [A49], inductor I²·DCR, and the series parts on the DC paths
(shunts, a surge stopper's FET) from GroundTruth's own DC solution. Missing data (no R_DS(on), no DCR) leaves that loss
out, with a note.

A motor bridge (analysis/bridge.py) heats its copper with its RMS current density (bridge_rms.py: the six-step
drive's currents from the battery to the motor and the switching currents, as mean squares, so added as heat,
not as vectors) and its parts with the losses from its drive waveform (bridge.bridge_losses).

Then PCB Thermal's adaptive mesh (refined where this heat enters through small features), conduction through
the stack, convection and radiation, part temperatures (θJB junction estimates where given).

Current capacity (M3): per power stage, the load current that brings the hottest copper or part junction to a
limit, with the other stages at their operating points. The model is built once; only the heat is rescaled
(capacity_inputs, solve_capacity)."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, Optional

import numpy as np

from ..config.settings import Config
from ..core.board import Board
from ..core.issues import Issue, resolve_severity
from .simulate import RouteResult

Progress = Callable[[str, float], None]
DIODE_VF = 0.5
# s per edge, a synchronous stage without a dead time: an adaptive controller's (the LTC7890's with its DTCx pins on
# GND [ADI LTC7890]) [A48]
DEFAULT_DEAD_TIME = 20e-9
V_SD_GAN, V_SD_SI = 2.0, 0.8            # V, reverse conduction without v_sd: GaN, a silicon body diode (as the bridges)
CAPACITY_MAX_SCALE = 10.0               # the capacity search stops at 10x a stage's operating current
CAPACITY_TOL_K = 0.25                   # and settles when the hottest point is this close to the limit
CAPACITY_PASS1_TOL_K = 2.0              # (the draft pass only locates it)
CAPACITY_MAX_SOLVES = 10                # per stage
ALL_STAGES = "all power stages"         # the capacity of every stage scaled together


@dataclass
class ThermalResult(RouteResult):
    """The thermal solve as a heatmap view: one temperature map per copper layer (°C, in `sheet_current` so the
    Results tab and the reports show it like the current maps), the hot spot, the parts and the heat balance.
    A capacity view (solve_capacity) has `capacity` set: the stage, its operating and capacity currents."""
    kind: str = "thermal"
    ambient_c: float = 25.0
    warn_c: float = 85.0
    max_c: float = 105.0
    t_max: float = 0.0
    hotspot_layer: str = ""
    hotspot: tuple = (0.0, 0.0)
    power_w: float = 0.0
    copper_w: float = 0.0                 # copper Joule heat at the solved temperatures
    copper_w20: float = 0.0               # the same at 20 °C (= the DC paths' copper I²R where they do not overlap)
    parts_w: float = 0.0
    heat_out: dict = field(default_factory=dict)
    parts: list = field(default_factory=list)            # [{"ref", "t_c", "t_junction", "power_w", "why"}]
    losses: list = field(default_factory=list)           # [{"ref", "power_w", "what"}]
    mesh: dict = field(default_factory=dict)
    iterations: list = field(default_factory=list)
    capacity: dict = field(default_factory=dict)
    heat_unit: str = "°C"
    heat_scale: str = "linear"
    dc_hot: list = field(default_factory=list)            # the DC paths at these temperatures (dc_at_temperature)
    # the electro-thermal coupling's rounds (analysis/electrothermal.couple; {} when it was off): rounds, settled,
    # changes, the busiest vias at temperature
    electrothermal: dict = field(default_factory=dict)

    def summary(self) -> dict:
        """The result as plain data for the JSON report (the 20 hottest parts; no temperature maps)."""
        return {"kind": "thermal", "t_max": self.t_max, "hotspot_layer": self.hotspot_layer,
                "hotspot": list(self.hotspot), "ambient_c": self.ambient_c, "power_w": self.power_w,
                "copper_w": self.copper_w, "parts_w": self.parts_w, "heat_out": self.heat_out,
                "parts": self.parts[:20], "losses": self.losses, "mesh": self.mesh, "notes": self.notes,
                "capacity": self.capacity, "dc_at_temperature": getattr(self, "dc_hot", []),   # (older pickles: none)
                "electrothermal": getattr(self, "electrothermal", {}), "error": self.error}

    def label(self) -> str:
        """The view's name in the Results list and the reports."""
        c = self.capacity
        if not c:
            return "Thermal: copper temperature"
        if c.get("currents") is not None:
            return f"Thermal: all power stages at their capacity ({capacity_text(c)} load)"
        if c.get("blocked_by"):
            return f"Thermal: {c['stage']} blocked by {', '.join(c['blocked_by'])}"
        return f"Thermal: {c['stage']} at its capacity ({capacity_text(c)})"


def capacity_text(c: dict) -> str:
    """"14.2 A" (or "≥ 50 A", "0 A"; "blocked" when other stages alone are over the limit; for all stages
    together "62 %" of their operating points)."""
    together = c.get("currents") is not None
    if c.get("bound") == "zero":
        return "0 %" if together else "blocked" if c.get("blocked_by") else "0 A"
    value = f"{c['scale'] * 100:.0f} %" if together else f"{c['i_capacity']:.3g} A"
    return ("≥ " if c.get("bound") == "above" else "") + value


def zero_text(c: dict) -> str:
    """Why a capacity is zero: the other stages at their operating points already put the hottest spot over the
    limit with this stage at no load ("buck conv1 at its operating point already puts copper at Q301 (F.Cu) at
    143.7 °C, over the 105 °C limit: no headroom left for buck conv2"), or this stage's own no-load heat does."""
    hot = f"{c['limited_by']} at {c['t_hottest']:.1f} °C, over the {c['limit_c']:g} °C limit"
    if c.get("blocked_by"):
        who = ", ".join(c["blocked_by"])
        verb = "puts" if len(c["blocked_by"]) == 1 else "put"
        return (f"{who} at {'its' if len(c['blocked_by']) == 1 else 'their'} operating point already {verb} {hot}: "
                f"no headroom left for {c['stage']}")
    if c.get("currents") is not None:
        return f"over the limit even without load: {hot}"
    return f"{c['stage']}: over the limit even without load ({hot}, the other stages at their operating points)"


def _empty(config: Config, error: str = "") -> ThermalResult:
    """A thermal result with no solve, carrying `error` (why there is nothing to show)."""
    tc = config.thermal
    res = ThermalResult("thermal", "power stages", "", ambient_c=tc.ambient_c, warn_c=tc.warn_c, max_c=tc.max_c)
    res.error = error
    return res


def _stages(converters) -> list:
    """The power-stage results a thermal solve can use (their DC paths solved); a motor bridge's legs heat the
    board through the bridge (_bridges)."""
    return [c for c in converters if (c.kind == "dcload" or c.kind == "converter" and not getattr(c, "view_of", None))
            and not c.error and not getattr(c, "bridge", None)]


def _bridges(results) -> list:
    """The motor bridge results a thermal solve can use (their RMS currents solved)."""
    return [b for b in results if b.kind == "bridge" and not b.error and b.rms.get("maps")]


NO_STAGES = ("no power stage or motor bridge results: run Simulate first (the DC paths and a bridge's RMS currents "
             "drive the thermal model)")


# ------------------------------------------------------------------------------------------- heat sources
def _layer_currents(board: Board, converters, bridges=()) -> dict:
    """Per copper layer: the DC paths' current density (A/mm²) on one grid (the finest of the paths' and bridges'
    grids on that layer), per power stage, and the motor bridges' mean square RMS density (A²/mm⁴), per bridge:
    {layer: (X, Y, d, t, {stage: (JX, JY)}, {bridge: J²})}. The densities are in the copper (a strip narrower than
    the pitch carries its current in its own width, Solution.node_field), so t (mm) is per sample: the copper
    thickness x the share of the sample's cell that carries current (Grid.copper_area), and ρ·J²·t·d² is the
    sample's heat. (X, Y) is where that heat is: the node, or the nearest point of its copper where the node lies
    outside it (Grid.hx / hy [A44])."""
    per_layer: dict[str, list] = {}
    for c in converters:
        for name, layers in (getattr(c, "dc_vectors", None) or {}).items():
            grids = c.dc_grids.get(name, {})
            for layer, (jx, jy) in layers.items():
                g = grids.get(layer)
                if g is not None:
                    per_layer.setdefault(layer, []).append((getattr(c, "net", ""), g, jx, jy))
        # a switch node's time variance D(1-D)·|J_hs - J_ls|² (power.dc_summary): heat on top of its mean current's,
        # added as J² as a bridge's RMS density is (and, keyed by the stage, scaled ∝ s² in the capacity search)
        for name, layers in (getattr(c, "dc_ripple_sq", None) or {}).items():
            grids = c.dc_grids.get(name, {})
            for layer, sq in layers.items():
                g = grids.get(layer)
                if g is not None:
                    per_layer.setdefault(layer, []).append((getattr(c, "net", ""), g, np.sqrt(sq), None))
    for b in bridges:
        for layer, m in b.rms["maps"].items():
            per_layer.setdefault(layer, []).append((b.net, b.rms["grids"][layer], m, None))
    out = {}
    for layer, items in per_layer.items():
        d = min(g.d for _, g, _, _ in items)
        x0 = min(g.x0 for _, g, _, _ in items)
        y0 = min(g.y0 for _, g, _, _ in items)
        x1 = max(g.x0 + (g.nx - 1) * g.d for _, g, _, _ in items)
        y1 = max(g.y0 + (g.ny - 1) * g.d for _, g, _, _ in items)
        nx, ny = int(round((x1 - x0) / d)) + 1, int(round((y1 - y0) / d)) + 1
        X, Y = np.meshgrid(x0 + np.arange(nx) * d, y0 + np.arange(ny) * d, indexing="ij")
        per_stage: dict[str, tuple] = {}
        per_rms: dict[str, np.ndarray] = {}
        area = np.zeros((nx, ny))               # the samples' current-carrying share (the largest of the grids')
        PX, PY = X.copy(), Y.copy()             # where each sample's heat is (its node, or its copper beside it)
        moved = np.zeros((nx, ny), bool)
        for stage, g, jx, jy in items:
            i = np.rint((X - g.x0) / g.d).astype(np.int64)
            j = np.rint((Y - g.y0) / g.d).astype(np.int64)
            inside = (i >= 0) & (i < g.nx) & (j >= 0) & (j < g.ny)
            ii, jj = np.where(inside, i, 0), np.where(inside, j, 0)
            on = inside & g.node[ii, jj]
            area = np.maximum(area, np.where(on, g.copper_area()[ii, jj], 0.0))
            if getattr(g, "hx", None) is not None:
                hx, hy = g.hx[ii, jj], g.hy[ii, jj]
                m = on & ~moved & ((hx != 0) | (hy != 0))
                PX = np.where(m, g.x0 + ii * g.d + hx, PX)
                PY = np.where(m, g.y0 + jj * g.d + hy, PY)
                moved |= m
            if jy is None:                    # a bridge's RMS density
                R = per_rms.setdefault(stage, np.zeros((nx, ny)))
                R += np.where(on, np.nan_to_num(jx[ii, jj]) ** 2, 0.0)
                continue
            JX, JY = per_stage.setdefault(stage, (np.zeros((nx, ny)), np.zeros((nx, ny))))
            JX += np.where(on, np.nan_to_num(jx[ii, jj]), 0.0)
            JY += np.where(on, np.nan_to_num(jy[ii, jj]), 0.0)
        t = board.stackup.layer(layer).thickness if board.stackup.layer(layer) else 0.035
        out[layer] = (PX, PY, d, t * area, per_stage, per_rms)
    return out


def _rho_mm() -> float:
    """Copper's resistivity at 20 °C in ohm mm (the current densities here are in A/mm^2)."""
    from ..thermal.materials import COPPER
    return COPPER.resistivity_20c * 1e3                      # Ω·mm


def _via_segments(stages, bridges=()) -> list[dict]:
    """Every via barrel segment that carries current [A43]: [{"x", "y", "a", "b" (its two layers), "r" (Ω), "i":
    {stage: signed A}, "sq": {stage: A²}}]. The stages' DC paths' currents at the vias (dc_via: the paths of all
    stages on the one whole-board model, signed, so they add or cancel as combine_dc adds them), their switching
    variances (dc_ripple_via: the switch node's and the hot loop's, in quadrature) and the motor bridges' mean
    squares (rms["via_heat"])."""
    segs: dict = {}

    def seg(key, x, y, a, b, r) -> dict:
        """The record of one segment, made at its first use."""
        return segs.setdefault(key, {"x": float(x), "y": float(y), "a": a, "b": b, "r": float(r), "i": {}, "sq": {}})

    for c in stages:
        for attr, signed in (("dc_via", True), ("dc_ripple_via", False)):
            for _, (db, cur) in (getattr(c, attr, None) or {}).items():
                ends = getattr(db, "via_ends", None) or []
                if len(ends) != len(db.vias):          # a model sent by an older compute server: no ends
                    continue
                cur = np.asarray(cur, float)
                for k in np.nonzero(np.abs(cur) > 1e-9)[0]:
                    _, _, _, x, y = db.vias[k]
                    s = seg((db.fingerprint, int(k)), x, y, *ends[k])
                    if signed:
                        s["i"][c.net] = s["i"].get(c.net, 0.0) + float(cur[k])
                    else:
                        s["sq"][c.net] = s["sq"].get(c.net, 0.0) + float(cur[k]) ** 2
    for b in bridges:
        for x, y, la, lb, r, ms in (getattr(b, "rms", None) or {}).get("via_heat", ()):
            s = seg(("bridge", b.net, round(x, 4), round(y, 4), la, lb), x, y, la, lb, r)
            s["sq"][b.net] = s["sq"].get(b.net, 0.0) + float(ms)
    return list(segs.values())


def _via_rows(segments: list[dict], value) -> dict:
    """{layer: array}: value(segment) at each of the segment's two layers, in the segments' order (the same order
    for every value, so positions, heat and its capacity terms line up)."""
    rows: dict = {}
    for s in segments:
        v = value(s)
        for layer in (s["a"], s["b"]):
            rows.setdefault(layer, []).append(v)
    return {layer: np.asarray(v, dtype=float) for layer, v in rows.items()}


def _via_power(s: dict) -> float:
    """A via segment's Joule heat at the stages' operating points (W): R·((Σ signed currents)² + Σ variances)."""
    return s["r"] * (sum(s["i"].values()) ** 2 + sum(s["sq"].values()))


def _with_vias(heat: dict, segments: list[dict]) -> dict:
    """`heat` ({layer: (x, y, side, P)}) with each via segment's heat (_via_power) added half at each of its two
    layers, at the via (a layer with no copper heat gets 0.3 mm samples)."""
    xs, ys = _via_rows(segments, lambda s: s["x"]), _via_rows(segments, lambda s: s["y"])
    ps = _via_rows(segments, lambda s: _via_power(s) / 2.0)
    out = dict(heat)
    for layer, p in ps.items():
        if layer in out:
            X, Y, d, P = out[layer]
            out[layer] = (np.concatenate([X, xs[layer]]), np.concatenate([Y, ys[layer]]), d, np.concatenate([P, p]))
        else:
            out[layer] = (xs[layer], ys[layer], 0.3, p)
    return out


def copper_heat(board: Board, converters, bridges=()) -> dict:
    """Per copper layer: (x, y, side, P at 20 °C) of the DC paths' Joule heat, the paths' current vectors added
    on one grid per layer (the finest of the paths' grids on that layer), and the motor bridges' RMS heat; and the
    via barrels' I²R, half at each end of every segment (_via_segments; before 2026-10-19 left out [A43])."""
    rho = _rho_mm()
    out = {}
    for layer, (X, Y, d, t, per_stage, per_rms) in _layer_currents(board, converters, bridges).items():
        # the stages' DC current densities add as vectors (paths sharing copper, a common ground, add or cancel);
        # Joule heat per sample is rho |J|^2 times its volume (t d^2); the bridges' RMS heat is already |J|^2
        JX = sum((jx for jx, _ in per_stage.values()), np.zeros_like(X))
        JY = sum((jy for _, jy in per_stage.values()), np.zeros_like(X))
        p = rho * (JX ** 2 + JY ** 2 + sum(per_rms.values(), np.zeros_like(X))) * t * d * d     # W per sample
        keep = p > 0
        out[layer] = (X[keep], Y[keep], d, p[keep])
    return _with_vias(out, _via_segments(converters, bridges))


def _is_diode(ref: str) -> bool:
    """A part is a diode by its reference (D...)."""
    return ref.upper().startswith("D")


def _synchronous(spec) -> bool:
    """A FET rectifies (or an IC has both switches inside): not a catch or boost diode."""
    rect = spec.high_side if spec.topology == "boost" else spec.low_side
    return any(not _is_diode(r) for r in rect) or (not rect and bool(spec.switch))


def edge_dead_times(spec) -> tuple[float, float]:
    """(hard, soft) dead times of a synchronous stage in s, DEFAULT_DEAD_TIME where not given [A48]: the hard edge
    is the main switch's turn-on (a buck's switch-node rise, a boost's fall), the soft one its turn-off."""
    rise = spec.dead_rise if spec.dead_rise is not None else DEFAULT_DEAD_TIME
    fall = spec.dead_fall if spec.dead_fall is not None else DEFAULT_DEAD_TIME
    return (fall, rise) if spec.topology == "boost" else (rise, fall)


def reverse_conduction(t_hard: float, t_soft: float, i_l: float, delta_i: float, q_node: float) -> dict:
    """The rectifier's reverse conduction per period in the two dead times (CCM) [A48]: {i_hard, i_soft, slew,
    t_soft_rev, charge}. At the hard edge (the main switch's turn-on, at the ripple's valley I_L − ΔI/2) the
    current flows in reverse for the whole dead time. At the soft edge (its turn-off, at the peak I_L + ΔI/2) the
    current first swings the node across, moving the two FETs' output charge q_node in slew = q_node / I; only the
    rest of the dead time is reverse conduction. charge = i_hard·t_hard + i_soft·t_soft_rev (C per period: the
    loss is charge·V_SD·f_sw)."""
    i_hard, i_soft = max(i_l - delta_i / 2, 0.0), i_l + delta_i / 2
    slew = q_node / i_soft if i_soft > 0 else 0.0
    t_soft_rev = max(t_soft - slew, 0.0)
    return {"i_hard": i_hard, "i_soft": i_soft, "slew": slew, "t_soft_rev": t_soft_rev,
            "charge": i_hard * t_hard + i_soft * t_soft_rev}


def dead_time_values(board: Board, spec) -> tuple[float, float, float, list[str]]:
    """(rise, fall dead times in s, the rectifier's reverse-conduction voltage, notes on what was assumed) of a
    synchronous stage: its dead_rise / dead_fall and v_sd (set, or from the controller's GT field and the FETs' Vsd
    fields), else DEFAULT_DEAD_TIME [A48] and GaN's or a silicon body diode's V_SD (the motor bridge's defaults)."""
    notes = []
    rise = spec.dead_rise if spec.dead_rise is not None else DEFAULT_DEAD_TIME
    fall = spec.dead_fall if spec.dead_fall is not None else DEFAULT_DEAD_TIME
    unset = [e for e, t in (("rise", spec.dead_rise), ("fall", spec.dead_fall)) if t is None]
    if unset:
        notes.append(f"{spec.name}: no dead time for the switch node's {' and '.join(unset)}, so "
                     f"{DEFAULT_DEAD_TIME * 1e9:g} ns assumed (an adaptive controller's); set it, or give the "
                     "controller IC a GT field 'dead:20ns' (per channel 'dead1:20ns', per edge 'dead_rise:20ns', per "
                     "labelled converter 'conv1_dead:20ns')")
    v = spec.v_sd
    if v is None and rise == 0 and fall == 0:
        v = 0.0                                   # no dead time: no reverse conduction, nothing to assume
    elif v is None:
        from .detect import FAST_SWITCHES
        rect = spec.high_side if spec.topology == "boost" else spec.low_side
        gan = any(FAST_SWITCHES.search(f"{fp.value} {fp.lib_id}") for fp in (board.footprint(r) for r in rect) if fp)
        v = V_SD_GAN if gan else V_SD_SI
        notes.append(f"{spec.name}: no v_sd (the rectifier's Vsd field), so {v:g} V assumed in the dead times "
                     f"({'GaN reverse conduction' if gan else 'a silicon body diode'})")
    return rise, fall, v, notes


def ripple_pp(board: Board, config: Config, spec, duty: float, v_in: Optional[float]) -> Optional[float]:
    """The inductor current's ripple ΔI p-p, or None without the inductor's L: V_in·D(1−D)/(L·f_sw) for a buck (D
    its high side's on-time), V_in·D_sw/(L·f_sw) for a boost (D_sw = 1 − D, its switch's) [Erickson 2001] [A43]."""
    from .power import switch_inductor
    _, inductor = switch_inductor(board, config, spec)
    lm = config.model_for(inductor.reference) if inductor is not None else None
    l_h = lm.get("L") if lm is not None and lm.kind == "inductor" else 0.0
    if not (l_h > 0 and spec.f_sw and v_in and 0.0 < duty < 1.0):
        return None
    d_on = (1.0 - duty) if spec.topology == "boost" else duty * (1.0 - duty)
    return v_in * d_on / (l_h * spec.f_sw)


def part_losses(board: Board, config: Config, converters) -> tuple[list[dict], list[str]]:
    """[{"ref", "power_w", "what"}] of every power stage's parts at its operating point, and notes on what is
    missing."""
    from .power import switch_inductor
    out: dict[str, dict] = {}
    notes = []

    def add(ref: str, w: float, what: str):
        """Add a loss to a part, with its cause."""
        if w <= 0 or board.footprint(ref) is None:
            return
        e = out.setdefault(ref, {"ref": ref, "power_w": 0.0, "what": []})
        e["power_w"] += w
        e["what"].append(what)

    from .pins import params_cache, series_note
    pin_params = params_cache(board, config)
    pin_amps: dict[str, float] = {}          # every fed pin's current over all stages and rails (they add up)

    def fed(pin: str, amps: float):
        """A pin a stage or rail feeds its current through (a connector's): its joint and contact heat below."""
        if pin and board.find_pin(pin) is not None:
            pin_amps[pin] = pin_amps.get(pin, 0.0) + abs(amps)

    specs = {c.name: c for c in config.converters}
    loads = {d.name: d for d in config.dc_loads}
    for res in converters:
        if getattr(res, "kind", None) == "dcload":
            # a DC rail with loads (analysis/dcload.py): its series parts (a fuse, a shunt) at the rail's current
            for path in getattr(res, "dc", []) or []:
                for p in path.get("parts", []):
                    add(p["name"], p["r_mohm"] * 1e-3 * path["current_a"] ** 2, f"in DC load {res.net}")
                for p in path.get("joints", []):        # its solder joints (power.dc_summary) at the part
                    add(p["name"], p["r_mohm"] * 1e-3 * path["current_a"] ** 2, f"solder joints, DC load {res.net}")
            if res.net in loads:
                from .dcload import pin_currents
                for pin, amps in pin_currents(loads[res.net]).items():
                    fed(pin, amps)
            continue
        spec = specs.get(res.net)
        if spec is None:
            continue
        # d is the high side's conduction time (ConverterSpec.pulse, the result's duty): a buck's switch duty
        # D = V_out/V_in, a boost's rectifier share 1 - D = V_in/V_out (its switch, the low side, conducts 1 - d).
        # Before 2026-10-17 a boost's d was taken as its switch duty here: right only at d = ½
        d = res.duty if res.duty is not None else 0.5
        boost = spec.topology == "boost"
        # the stage's connectors carry the output current and the input current (the energy balance's when there
        # is one, else the lossless D·I_out of a buck, the inductor current I_out/d of a boost)
        i_in = (getattr(res, "input", None) or {}).get("i_in_a")
        if i_in is None:
            i_in = spec.i_out / d if boost and d > 0 else spec.i_out * d
        for pin in spec.output_connector:
            fed(pin, spec.i_out)
        for pin in spec.input_connector:
            fed(pin, i_in)
        # inductor current: a boost's output is the rectifier's share of it, I_out = d·I_L
        i_l = spec.i_out / d if boost and d > 0 else spec.i_out
        v = spec.v_out if boost else (spec.v_in or res.v_in)                    # switched voltage
        hs = spec.high_side or spec.switch
        ls = spec.low_side
        main, rect = (ls, hs) if boost else (hs, ls)      # the hard-switched transistor(s), the rectifier
        main = [r for r in main if not _is_diode(r)]
        sync = _synchronous(spec)
        d_hs, d_ls = d, 1 - d                             # conduction share: the high side d in both topologies
        # a synchronous rectifier FET conducts in reverse in the dead times (both switches off: the inductor
        # current, continuous and positive, freewheels through it at v_sd [A48]): the whole hard-edge dead time,
        # and at the soft edge what is left after the current has swung the node across (reverse_conduction); its
        # channel is off for both dead times and carries the rest of its share [Erickson 2001]
        rect_fets = [r for r in rect if not _is_diode(r)]
        hard_rev = False                                  # the rectifier conducts before the hard turn-on (Q_rr)
        if rect_fets and not spec.switch and spec.f_sw:
            rise, fall, v_sd, why = dead_time_values(board, spec)
            notes += why
            t_hard, t_soft = edge_dead_times(spec)
            # the node's charge at the soft edge: both FETs' output charge (the side's q_oss, the other side taken
            # alike), else C_oss·V each; neither: no swing taken off the dead time (the larger loss)
            q_node = 2 * spec.q_oss if spec.q_oss else (2 * spec.c_oss * v if spec.c_oss and v else 0.0)
            di = ripple_pp(board, config, spec, d, spec.v_in or res.v_in)
            rc = reverse_conduction(t_hard, t_soft, i_l, di or 0.0, q_node)
            hard_rev = t_hard > 0 and rc["i_hard"] > 0
            off = min((rise + fall) * spec.f_sw, d_hs if boost else d_ls)    # the channel's share given up
            if boost:
                d_hs -= off
            else:
                d_ls -= off
            for ref in rect_fets:
                add(ref, rc["charge"] * spec.f_sw * v_sd / len(rect_fets),
                    f"reverse conduction {v_sd:g} V: {t_hard * 1e9:g} ns at the hard turn-on, "
                    f"{rc['t_soft_rev'] * 1e9:.3g} of {t_soft * 1e9:g} ns after the {rc['slew'] * 1e9:.3g} ns node "
                    "swing")
        if spec.rds_on is not None:
            r_lo = spec.rds_on_low if spec.rds_on_low is not None else spec.rds_on
            fets_hs, fets_ls = [r for r in hs if not _is_diode(r)], [r for r in ls if not _is_diode(r)]
            for ref in fets_hs:              # parallel switches share the current: (I/n)² each
                if spec.switch:                  # integrated: both switches inside, the current always flows
                    add(ref, i_l ** 2 * spec.rds_on / len(fets_hs), f"conduction {i_l:g} A")
                else:
                    add(ref, d_hs * (i_l / len(fets_hs)) ** 2 * spec.rds_on,
                        f"conduction {d_hs * 100:.0f} % of the time")
            for ref in fets_ls:
                add(ref, d_ls * (i_l / len(fets_ls)) ** 2 * r_lo, f"conduction {d_ls * 100:.0f} % of the time")
        else:
            notes.append(f"{spec.name}: no rds_on, so the switches' conduction loss is left out")
        for ref in rect:                     # a rectifier diode conducts while the main switch is off
            if _is_diode(ref):
                vf = spec.diode_vf if spec.diode_vf is not None else DIODE_VF
                add(ref, (d_hs if boost else d_ls) * i_l * vf, f"{'boost' if boost else 'catch'} diode {vf:g} V")
        if v and spec.f_sw and main:
            # overlap of voltage and current: synchronous, only the main switch's turn-on edge is hard (at its
            # turn-off the inductor current carries the switch node across while the channel is already off);
            # with a diode both edges are
            edges = 1 if sync else 2
            p_edge = 0.5 * v * i_l * edges * spec.rise_time * spec.f_sw
            # output charge per main-switch turn-on: synchronous, Q_oss·V (its own charge dumped, the rectifier
            # FET's charged through it: for equal FETs together Q_oss·V); a diode: its own energy, ≈ ½·Q_oss·V
            if spec.q_oss is not None:
                e_oss, how = spec.q_oss * v, "Q_oss·V"
            else:
                e_oss, how = (spec.c_oss or 0.0) * v * v, "C_oss·V²"
            if not sync:
                e_oss, how = 0.5 * e_oss, "½·" + how
            # the rectifier's recovered charge, swept out through the main switch at its hard turn-on with the full
            # voltage across it: Q_rr·V per period [Erickson 2001, §4.6] [A49]. Only when the rectifier conducted
            # before it: a diode always, a synchronous FET's body diode in the dead time
            e_rr = spec.q_rr * v if spec.q_rr and (not sync or hard_rev) else 0.0
            for ref in main:
                add(ref, p_edge / len(main), "switching, turn-on edge" if sync else "switching, both edges")
                add(ref, e_oss * spec.f_sw, f"output charge {how}·f_sw")
                if e_rr:
                    add(ref, e_rr * spec.f_sw / len(main), f"reverse recovery Q_rr·V·f_sw ({spec.q_rr * 1e9:g} nC)")
        _, inductor = switch_inductor(board, config, spec)
        if inductor is not None:
            if spec.dcr is not None:
                add(inductor.reference, i_l ** 2 * spec.dcr, f"DCR {spec.dcr * 1e3:g} mΩ")
            else:
                notes.append(f"{spec.name}: no dcr, so {inductor.reference}'s winding loss is left out")
        for path in getattr(res, "dc", []) or []:
            for p in path.get("parts", []):
                add(p["name"], p["r_mohm"] * 1e-3 * path["current_a"] ** 2, f"in DC {path['name']}")
            for p in path.get("joints", []):            # its solder joints (power.dc_summary) at the part
                add(p["name"], p["r_mohm"] * 1e-3 * path["current_a"] ** 2, f"solder joints, DC {path['name']}")
    # each fed pin's solder joint and mating contact (analysis/pins.py): I²·R at its part, with the pin's current
    # summed over the stages and rails through it (a connector feeding two converters)
    for pin, amps in sorted(pin_amps.items()):
        ref = pin.rsplit(".", 1)[0]
        pp = pin_params(ref)
        if pp.series > 0:
            parts = [f"{k} {getattr(pp, k) * 1e3:g} mΩ" for k in ("contact", "joint") if getattr(pp, k)]
            add(ref, amps ** 2 * pp.series, f"pin {pin} at {amps:.3g} A: {' + '.join(parts)}")
        note = series_note(ref, pin, amps, pp)
        if note:
            notes.append(note)
    rows = [dict(e, what=", ".join(e["what"])) for e in out.values()]
    return sorted(rows, key=lambda r: -r["power_w"]), notes


def _merged(*lists) -> list[dict]:
    """Loss rows of several sources, one row per part."""
    out: dict[str, dict] = {}
    for rows in lists:
        for r in rows:
            e = out.setdefault(r["ref"], {"ref": r["ref"], "power_w": 0.0, "what": []})
            e["power_w"] += r["power_w"]
            e["what"].append(r["what"])
    return sorted((dict(e, what=", ".join(w for w in e["what"] if w)) for e in out.values()),
                  key=lambda r: -r["power_w"])


def _bridge_losses_at(board: Board, config: Config, b, s: float) -> dict[str, float]:
    """{ref: W} of a motor bridge result's parts with its phase current scaled by s: the drive again at that
    current; the capacitors' ripple from their shares of the new switching currents, the series parts' current
    scaled by s."""
    from dataclasses import replace
    from .bridge import bridge_losses, caps_at
    from .motor import DriveError, six_step
    spec = next((x for x in config.bridges if x.name == b.net), None)
    if spec is None or not spec.i_phase:
        return {r["ref"]: r["power_w"] for r in b.losses}
    try:
        w = six_step(replace(spec, i_phase=spec.i_phase * s))
    except DriveError:
        return {r["ref"]: r["power_w"] * s * s for r in b.losses}
    caps = caps_at(spec, b.caps, w)
    parts = [dict(p, rms_a=p["rms_a"] * s) for p in b.rms.get("parts", [])]
    rows, _ = bridge_losses(board, config, spec, w, caps, parts)
    return {r["ref"]: r["power_w"] for r in rows}


def _fit(ss, ps) -> tuple[float, float, float]:
    """(a, b, c) of a·s² + b·s + c through three points."""
    a, b, c = np.linalg.solve(np.array([[s * s, s, 1.0] for s in ss]), np.array(ps, float))
    return float(a), float(b), float(c)


# ------------------------------------------------------------------------------------------- the solve
def run_thermal(board: Board, config: Config, converters, progress: Optional[Progress] = None,
                cancel=None) -> tuple[ThermalResult, list[Issue]]:
    """The thermal solve of these power-stage results (ConverterResults from simulate, their DC paths solved), with
    the DC paths at the solved temperatures (with_dc_at_temperature)."""
    inputs = thermal_inputs(board, config, converters, progress)
    if inputs is None:
        return _empty(config, NO_STAGES), []
    res, issues = solve_thermal(board, config, *inputs, progress, cancel)
    if config.thermal.electrothermal:           # rounds of DC paths at temperature and Thermal (electrothermal.py)
        from .electrothermal import couple

        def again(results):
            """Thermal of the results of a round."""
            return solve_thermal(board, config, *thermal_inputs(board, config, results), progress, cancel)
        res, issues, _ = couple(board, config, converters, res, issues, again, progress, cancel)
        return res, issues
    return with_dc_at_temperature(res, board, converters), issues


def dc_at_temperature(th: ThermalResult, board: Board, converters) -> list[dict]:
    """The power stages' and DC loads' DC paths at the solved temperatures, which the DC tables leave at 20 °C
    [A42]: each path's copper (strips and vias) R₂₀ times the mean of 1 + α(T − 20 °C) [IEC 60028] over the path's
    own Joule heat, sample by sample, its series parts as they were; the drop and the loss with that R. First order:
    the currents keep their 20 °C pattern (a hotter stretch would push a little current elsewhere, so R rises a
    little less). T is the result's map at each sample (each level-0 cell its hottest copper [A41]: a little high
    where the cell also holds hotter copper). [{"stage", "path", "factor", "t_mean_c", "r20_mohm", "r_mohm",
    "drop20_mv", "drop_mv", "loss20_w", "loss_w"}]: "drop" None for the hot loop's switching current (no mean)."""
    from ..thermal.materials import COPPER
    if th.error or not th.sheet_current:
        return []
    rho = _rho_mm()
    rows = []
    for c in _stages(converters):
        vectors = getattr(c, "dc_vectors", None) or {}
        ripple = getattr(c, "dc_ripple_sq", None) or {}
        grids = getattr(c, "dc_grids", None) or {}
        for path in getattr(c, "dc", None) or []:
            name = path.get("name")
            heat = heat_t = 0.0                  # Σ the path's heat, Σ heat × its copper's temperature
            for layer, g in grids.get(name, {}).items():
                if layer not in th.sheet_current:
                    continue
                sl = board.stackup.layer(layer)
                t_cu = sl.thickness if sl else 0.035
                sq = np.zeros(g.node.shape)
                if name in vectors and layer in vectors[name]:
                    jx, jy = vectors[name][layer]
                    sq = sq + np.nan_to_num(np.asarray(jx, float)) ** 2 + np.nan_to_num(np.asarray(jy, float)) ** 2
                if name in ripple and layer in ripple[name]:
                    sq = sq + np.nan_to_num(np.asarray(ripple[name][layer], float))
                ii, jj = np.nonzero(g.node & (sq > 0))
                if len(ii) == 0:
                    continue
                p = sq[ii, jj] * g.copper_area()[ii, jj] * t_cu * g.d * g.d     # the sample's heat / ρ
                x, y = g.x0 + ii * g.d, g.y0 + jj * g.d
                if getattr(g, "hx", None) is not None:
                    x, y = x + g.hx[ii, jj], y + g.hy[ii, jj]
                tg, tm = th.layer_grids[layer], th.sheet_current[layer]
                ti = np.clip(np.rint((x - tg.x0) / tg.d).astype(np.int64), 0, tm.shape[0] - 1)
                tj = np.clip(np.rint((y - tg.y0) / tg.d).astype(np.int64), 0, tm.shape[1] - 1)
                t = tm[ti, tj].astype(float)
                ok = np.isfinite(t)
                heat += float(p[ok].sum()) * rho
                heat_t += float((p[ok] * t[ok]).sum()) * rho
            if heat <= 0 or not path.get("r_mohm"):
                continue
            t_mean = heat_t / heat
            f = 1.0 + COPPER.alpha * (t_mean - 20.0)        # ρ(T) is linear: the heat-weighted mean of 1 + αΔT
            r20 = float(path["r_mohm"])
            r_cu = float(path.get("r_copper_mohm", r20))
            r = r20 + r_cu * (f - 1.0)
            ac = bool(path.get("ac"))
            rows.append({"stage": c.net, "path": name, "factor": f, "t_mean_c": t_mean,
                         "r20_mohm": r20, "r_mohm": r,
                         "drop20_mv": None if ac else path.get("drop_mv"),
                         "drop_mv": None if ac or path.get("drop_mv") is None else path["drop_mv"] * r / r20,
                         "loss20_w": path.get("loss_w"), "loss_w": (path.get("loss_w") or 0.0) * r / r20})
    return rows


def with_dc_at_temperature(th: ThermalResult, board: Board, converters) -> ThermalResult:
    """`th` with its DC paths at temperature (dc_at_temperature) and a note on them, the paths whose loss grows
    most first (the busiest: their drop changes most too)."""
    th.dc_hot = dc_at_temperature(th, board, converters)
    if th.dc_hot:
        top = sorted(th.dc_hot, key=lambda r: -((r["loss_w"] or 0.0) - (r["loss20_w"] or 0.0)))
        lo, hi = min(r["factor"] for r in top), max(r["factor"] for r in top)
        each = "; ".join(f"{r['stage']} {r['path']}: {r['r20_mohm']:.3g} → {r['r_mohm']:.3g} mΩ"
                         + (f", drop {r['drop20_mv']:.3g} → {r['drop_mv']:.3g} mV" if r["drop_mv"] is not None else "")
                         for r in top[:4])
        th.notes.append(f"the DC paths at these temperatures (their tables are at 20 °C): copper "
                        f"{(lo - 1) * 100:+.0f} to {(hi - 1) * 100:+.0f} % resistance; " + each
                        + (f"; {len(top) - 4} more in the report" if len(top) > 4 else ""))
    return th


def thermal_inputs(board: Board, config: Config, converters, progress: Optional[Progress] = None
                   ) -> Optional[tuple[dict, list[dict], list]]:
    """(copper heat, part losses, notes) of these power-stage and motor bridge results; None when none of them
    can be solved."""
    stages, bridges = _stages(converters), _bridges(converters)
    if not stages and not bridges:
        return None
    if progress:
        progress("thermal: the heat of the DC paths and the parts", 0.01)
    losses, notes = part_losses(board, config, stages)
    # a DC path whose solve failed has no current map: its copper heat is missing, so say so rather than present
    # the temperatures as the whole stage's (VALIDATION_AUDIT.md round 6, ISSUE-25)
    notes = list(notes) + [f"{s.net}: DC {name} was not solved: its copper heat is not in these temperatures"
                           for s in stages for name in (getattr(s, "dc_unsolved", None) or [])]
    notes += [f"{b.net}: {name} was not solved: its heat is not in these temperatures"
              for b in bridges for name in (getattr(b, "rms_missing", None) or [])]
    return copper_heat(board, stages, bridges), _merged(losses, *(b.losses for b in bridges)), notes


def solve_thermal(board: Board, config: Config, heat: dict, losses: list[dict], notes: Optional[list] = None,
                  progress: Optional[Progress] = None, cancel=None) -> tuple[ThermalResult, list[Issue]]:
    """The temperatures for this heat: `heat` per copper layer (x, y, side, W at 20 °C; copper_heat) and `losses`
    [{"ref", "power_w", "what"}] through the parts' pads."""
    say = _sayer(progress)
    m = _build_model(board, config, heat, losses, notes, say, cancel)
    sol = _coupled(m, m.p20, m.Qc, say)
    if sol.error:
        res = _empty(config, sol.error)
        res.losses, res.notes = losses, list(m.notes)
        return res, []
    res, issues = _result(board, config, m, sol, losses)
    say("thermal: done", 1.0)
    return res, issues


def _sayer(progress: Optional[Progress]):
    """A progress function that may be called whether or not a callback was given."""
    def say(msg: str, frac: float):
        """Report through `progress`, if any."""
        if progress:
            progress(msg, frac)
    return say


@dataclass
class _Model:
    """A board's thermal model, built once (mesh, matrices, the AMG hierarchy after the first solve), with the
    copper heat samples mapped to its nodes."""
    tc: object
    env: object
    tb: object
    setup: object
    th: object
    mesh: object
    grid: object
    layers: list                          # copper layers of `heat` in the model, in the order of the samples
    nodes: np.ndarray                     # thermal node of every copper heat sample
    p20: np.ndarray                       # the samples' heat at 20 °C (W)
    Qc: np.ndarray                        # the parts' heat per node (W)
    notes: list
    t0: float


@dataclass
class _Solution:
    """One coupled thermal solve: the heat it had, the node temperatures, the iterations' history, the final copper
    and total heat per node, an error (thermal runaway) and whether the iteration settled."""
    p20: np.ndarray                       # the copper heat at 20 °C and the parts' heat this solve had
    Qc: np.ndarray
    T: Optional[np.ndarray] = None
    hist: list = field(default_factory=list)
    Pcu: Optional[np.ndarray] = None
    Q: Optional[np.ndarray] = None
    linear: str = ""                      # the last linear solve's failure (CG unconverged / broken down), if any
    error: str = ""
    settled: bool = True


def _build_model(board: Board, config: Config, heat: dict, losses: list[dict], notes, say, cancel) -> _Model:
    """The board's thermal model (PCB Thermal's solver): the stack, the mesh (refined adaptively where the copper
    heat and the parts' heat enter, within the quality's budget), the conduction / convection / radiation model,
    the parts' heat per node, and every copper heat sample mapped to its thermal node."""
    from ..thermal.adapter import to_thermal_board
    from ..thermal.project import BoardSetup, ComponentPower, Environment, MeshSettings
    from ..thermal.solver.common import Progress as TProgress
    from ..thermal.solver.coupled import plan_mesh
    from ..thermal.solver.mesh import HeatSamples, Mesh, plan_refinement, shape_samples, K_REF
    from ..thermal.solver.stack import build_stack
    from ..thermal.solver.thermal import ThermalModel, component_heat_areas

    t0 = time.time()
    tc = config.thermal

    def tprogress(msg):                             # PCB Thermal's progress messages (model building)
        """Pass PCB Thermal's progress messages on, mapped into 20-80 % of this phase."""
        if isinstance(msg, dict) and msg.get("type") == "progress":
            say(f"thermal: {msg.get('text', '')}", 0.2 + 0.6 * float(msg.get("overall", 0.0)))

    say("thermal: preparing the board model", 0.02)
    tb = to_thermal_board(board)
    model_notes = list(notes or [])
    setup = BoardSetup(board_file=str(tb.path),
                       components=[ComponentPower(l["ref"], l["power_w"], tc.theta_jb.get(l["ref"], 0.0))
                                   for l in losses],
                       mesh=MeshSettings(quality=tc.quality))
    env = Environment(ambient_c=tc.ambient_c, orientation=tc.orientation, convection=tc.convection,
                      airspeed=tc.airspeed, h_top=tc.h_top, h_bottom=tc.h_bottom, radiation=tc.radiation)
    stack = build_stack(tb, setup.materials)
    plan = plan_mesh(tb, setup)
    grid, q = plan["grid"], plan["quality"]
    layer_index = {L: k for k, L in enumerate(tb.copper_layers)}

    # refinement follows where the heat enters (copper samples and the parts' pads), as PCB Thermal plans it
    samples = HeatSamples.empty()
    for x, y, d, p in heat.values():
        samples = samples + HeatSamples(x - d / 2, y - d / 2, np.full(len(x), d), p)
    for c in setup.components:
        fp = tb.footprint(c.ref)
        if fp is None or c.power <= 0:
            continue
        for geom, share, kind in component_heat_areas(tb, fp):
            shape = geom.shape if kind == "pad" else geom
            ds = max(plan["dx_fine"] / 2, math.sqrt(shape.area / 1e5))
            samples = samples + shape_samples(shape, c.power * share, ds)
    leaves = None
    if plan["adaptive"] and plan["dx_fine"] < grid.dx and len(samples.p):
        say("thermal: planning the adaptive mesh", 0.12)
        keep = samples.p > 0.01 * q[3] * K_REF * plan["dx_fine"] * 1e-3
        samples = HeatSamples(samples.x[keep], samples.y[keep], samples.s[keep], samples.p[keep])
        leaves, info = plan_refinement(grid, samples, q[3], plan["dx_fine"], q[5], stack)
        if info["limited"]:
            model_notes.append(f"the adaptive mesh reached its budget: local temperatures may be off by up to about "
                               f"{info['remaining_err']:.2g} K (thermal.quality: fine for more detail)")
    if leaves is None:
        from ..thermal.solver.mesh import B
        nbx, nby = math.ceil(grid.nx / B), math.ceil(grid.ny / B)
        leaves = {0: {(i, j) for i in range(nby) for j in range(nbx)}}
    say("thermal: building the thermal model", 0.2)
    tp = TProgress(tprogress, cancel)
    mesh = Mesh(tb, stack, grid, leaves, tp)
    th = ThermalModel(tb, setup, env, stack, mesh, tp)
    Qc, _ = th.component_heat()
    model_notes += th.warnings

    nodes, p20, layers = [], [], []
    for layer, (x, y, _, p) in heat.items():
        if layer not in layer_index:
            continue
        cols = mesh.locate(x, y)
        nodes.append(th.cu_nodes(cols, np.full(len(cols), layer_index[layer])))
        p20.append(p)
        layers.append(layer)
    nodes = np.concatenate(nodes) if nodes else np.zeros(0, np.int64)
    p20 = np.concatenate(p20) if p20 else np.zeros(0)
    return _Model(tc, env, tb, setup, th, mesh, grid, layers, nodes, p20, Qc, model_notes, t0)


def _coupled(m: _Model, p20: np.ndarray, Qc: np.ndarray, say=None, T: Optional[np.ndarray] = None) -> _Solution:
    """The coupled iteration (copper resistivity follows the temperature), from T (default: ambient)."""
    from ..thermal.materials import COPPER
    th, env, nodes = m.th, m.env, m.nodes
    T = np.full(th.n, env.ambient_c) if T is None else T.copy()
    omega, hist = 1.0, []
    max_it, tol = 30, 0.02
    sol = _Solution(p20, Qc)
    for it in range(max_it):
        if say:
            say(f"thermal: coupled iteration {it + 1}", 0.8 + 0.15 * (1 - 0.7 ** it))
        rise = max(float(T.max()) - env.ambient_c, 1.0)
        lin_tol = 1e-5 if not hist else float(np.clip(1e-3 * hist[-1]["dT"] / rise, 1e-8, 1e-5))
        Pcu = p20 * (1 + COPPER.alpha * (T[nodes] - 20.0)) if len(nodes) else p20
        Q = Qc + np.bincount(nodes, weights=Pcu, minlength=th.n)
        T_new = th.solve(Q, T, tol=lin_tol)
        # an unconverged CG returns its best x; two such in a row can differ by less than the tolerance, which would
        # read as settled (ISSUE-26): the last solve must have converged for the answer to count
        solver = getattr(th, "solver", None)
        sol.linear = "" if solver is None or getattr(solver, "last_converged", True) else (
            f"the thermal solver's CG stopped short (relative residual {solver.last_residual:.1e} after "
            f"{solver.last_iters} iterations, asked {lin_tol:.0e})")
        dT = float(np.max(np.abs(T_new - T)))
        if hist and dT > hist[-1]["dT"] * 1.05:
            omega = max(0.25, omega * 0.6)
        T = T + omega * (T_new - T)
        hist.append({"iter": it + 1, "dT": dT, "Tmax": float(T.max()), "power": float(Q.sum())})
        if not np.isfinite(T.max()) or T.max() > 1000:
            sol.error = "thermal runaway: the copper passes 1000 °C (the current would fuse it)"
            return sol
        if it >= 1 and dT < tol and not sol.linear:
            break
    else:
        sol.settled = False
    sol.T, sol.hist, sol.Pcu, sol.Q = T, hist, Pcu, Q
    return sol


def _result(board: Board, config: Config, m: _Model, sol: _Solution, losses: list[dict],
            capacity: Optional[dict] = None) -> tuple[ThermalResult, list[Issue]]:
    """The heatmap view, parts and issues of a solved temperature field."""
    from ..solver.peec import Grid as GtGrid
    from ..thermal.solver.coupled import _component_temps
    tc, env, th, mesh, T, tb = m.tc, m.env, m.th, m.mesh, sol.T, m.tb
    res = _empty(config)
    issues: list[Issue] = []
    res.losses = losses
    res.notes = list(m.notes)
    if not sol.settled:
        res.notes.append(f"the coupled iteration did not settle to 0.02 °C in {len(sol.hist)} iterations")
    if sol.linear:
        res.notes.append(f"{sol.linear}: the temperatures are not converged and may be wrong; try thermal.quality "
                         "draft or fine (another mesh)")
    res.iterations = sol.hist
    res.power_w = float(sol.Q.sum())
    res.copper_w20 = float(sol.p20.sum())
    res.copper_w = float(sol.Pcu.sum()) if len(m.nodes) else 0.0
    res.parts_w = float(sol.Qc.sum())
    res.heat_out = {"total": float(th.heat_out(T))}            # convection + radiation (+ mounts), W

    # maps: the level-0 grid, one temperature map per copper layer (rows of PCB Thermal's maps are y)
    Tcu = th.copper_values(T)
    g0 = mesh.grid0
    res.layer_grids, res.sheet_current = {}, {}
    for k, layer in enumerate(tb.copper_layers):
        # each level-0 cell shows its hottest refined cell [A41]: the copper's own temperature where a track is narrower
        # than the cell (its mean read a 0.1 mm track on 0.3 mm cells 15 % cool, while t_max and the issues, from
        # the refined cells, were right; VALIDATION_AUDIT.md round 5, ISSUE-14)
        mm = mesh.to_level0(Tcu[k], how="max").T.astype(np.float32)      # (nx, ny)
        node = np.isfinite(mm)
        gg = GtGrid(g0.x0 + g0.dx / 2, g0.y0 + g0.dx / 2, g0.dx, g0.nx, g0.ny, node,
                    node[:-1, :] & node[1:, :], node[:, :-1] & node[:, 1:])
        res.layer_grids[layer], res.sheet_current[layer] = gg, mm
    res.grid = next(iter(res.layer_grids.values()))
    res.plane_layers = list(tb.copper_layers)
    rise = {L: float(np.nanmax(res.sheet_current[L]) - env.ambient_c) for L in res.sheet_current}
    tot = sum(max(v, 0.0) for v in rise.values()) or 1.0
    res.layer_share = {L: max(v, 0.0) / tot for L, v in rise.items()}   # the layer list's order: hottest first
    col, slab = th.node_column_slab(int(np.argmax(T)))
    res.t_max = float(T.max())
    res.hotspot = (float(mesh.col_x[col]), float(mesh.col_y[col]))
    res.hotspot_layer = slab.name if slab.kind == "copper" else max(rise, key=rise.get)
    res.location, res.layer = res.hotspot, res.hotspot_layer
    res.map_frequency = 0.0
    lead = (f"{capacity['stage']} at {capacity_text(capacity)}, its capacity to {capacity['limit_c']:g} °C (other "
            f"stages at their operating points); " if capacity else "")
    if capacity and capacity.get("blocked_by"):
        lead = f"{capacity['stage']} at no load, {zero_text(capacity)}; "
    res.heat_title = (f"{lead}copper temperature ({tc.ambient_c:g} °C ambient, {tc.convection} convection): "
                      f"{res.copper_w:.2f} W in the copper, {res.parts_w:.2f} W in the parts")
    res.heat_label = "temperature (°C)"
    res.transfers = []
    by_loss = {l["ref"]: l for l in losses}
    parts = []
    for r in _component_temps(tb, m.setup, th, T):
        if r["ref"] not in by_loss and r["t_max"] < env.ambient_c + 5:
            continue
        parts.append({"ref": r["ref"], "value": r.get("value", ""), "t_c": r["t_max"], "t_mean": r["t_mean"],
                      "t_junction": r.get("t_junction"), "power_w": by_loss.get(r["ref"], {}).get("power_w", 0.0),
                      "why": by_loss.get(r["ref"], {}).get("what", "heated by the board")})
    res.parts = sorted(parts, key=lambda p: -(p["t_junction"] or p["t_c"]))
    res.mesh = {"thermal_cell_mm": m.grid.dx, "finest_cell_mm": float(mesh.col_dx.min()), "unknowns": int(th.n),
                "seconds": time.time() - m.t0}
    if capacity:
        res.capacity = capacity
        res.receiver = f"capacity {capacity['stage']}"                 # report file names: one per stage
        if capacity.get("currents") is not None:
            lead = (f"all power stages at {capacity_text(capacity)} of their loads, their capacity to "
                    f"{capacity['limit_c']:g} °C; ")
            res.heat_title = (f"{lead}copper temperature ({tc.ambient_c:g} °C ambient, {tc.convection} convection): "
                              f"{res.copper_w:.2f} W in the copper, {res.parts_w:.2f} W in the parts")
        return res, []                    # at its capacity a stage is at the limit by construction: no issues

    # issues
    def issue(check, msg, loc, layer, sev, key, details):
        """Add a thermal issue (unless its check is switched off)."""
        s = resolve_severity(check, config.rules.severity, sev)
        if s != "off":
            issues.append(Issue(check, s, "thermal", msg, (round(loc[0], 4), round(loc[1], 4)), layer=layer,
                                key=key, details=details))
    if res.t_max >= tc.warn_c:
        issue("thermal_hotspot", f"copper reaches {res.t_max:.1f} °C on {res.hotspot_layer} at "
              f"({res.hotspot[0]:.2f}, {res.hotspot[1]:.2f}) ({res.t_max - tc.ambient_c:+.1f} K over "
              f"{tc.ambient_c:g} °C ambient; limit {tc.max_c:g} °C)", res.hotspot, res.hotspot_layer,
              "error" if res.t_max >= tc.max_c else "warning", "thermal:hotspot", {"t_max": res.t_max})
    # parts that dissipate: one issue each, from warn_c; parts only heated by the board (a gate resistor or a
    # capacitor next to a hot switch): one issue for all of them, past max_c
    passive = []
    for p in res.parts:
        t = p["t_junction"] or p["t_c"]
        if p["power_w"] <= 0:
            if t >= tc.max_c:
                passive.append(p)
            continue
        if t >= tc.warn_c:
            fp = board.footprint(p["ref"])
            loc = fp.position if fp else res.hotspot
            what = f"junction ≈{p['t_junction']:.1f} °C" if p["t_junction"] else f"{p['t_c']:.1f} °C at its pads"
            issue("part_temperature", f"{p['ref']}: {what} ({p['power_w']:.2f} W: {p['why']}; limit {tc.max_c:g} °C)",
                  loc, fp.layer if fp else "", "error" if t >= tc.max_c else "warning", f"thermal:part:{p['ref']}",
                  {"t_c": p["t_c"], "power_w": p["power_w"]})
    if passive:
        hot = passive[0]
        fp = board.footprint(hot["ref"])
        names = ", ".join(f"{p['ref']} {p['t_c']:.0f} °C" for p in passive[:8]) + (" …" if len(passive) > 8 else "")
        issue("part_temperature", f"{len(passive)} part(s) without losses of their own are heated past "
              f"{tc.max_c:g} °C by the copper around them: {names} (check their temperature ratings)",
              fp.position if fp else res.hotspot, fp.layer if fp else "", "error", "thermal:parts:heated",
              {"parts": [p["ref"] for p in passive]})
    return res, issues


# ------------------------------------------------------------------------------------------- current capacity
def _quadratic(p0: float, p1: float, p2: float) -> tuple[float, float, float]:
    """(a, b, c) of a·s² + b·s + c through the values at s = 0, 1 and 2."""
    a = (p2 - 2 * p1 + p0) / 2
    return a, p1 - p0 - a, p0


def _scaled(config: Config, stages, name: str, s: float):
    """The settings and stage results with stage `name`'s load current scaled by s (what part_losses reads)."""
    from dataclasses import replace
    names = {name} if isinstance(name, str) else set(name)
    cfg = replace(config, converters=[replace(c, i_out=c.i_out * s) if c.name in names else c
                                      for c in config.converters])
    out = [SimpleNamespace(kind=getattr(c, "kind", "converter"), net=c.net, duty=c.duty, v_in=c.v_in,
                           dc=[dict(p, current_a=p["current_a"] * s) for p in (getattr(c, "dc", None) or [])])
           if c.net in names else c for c in stages]
    return cfg, out


def capacity_inputs(board: Board, config: Config, converters) -> dict:
    """What solve_capacity needs, from the power-stage results (fast; the plugin prepares it in the window's
    process and searches in the thermal process): the heat at the operating points, and per stage its copper heat
    and part losses as quadratics a·s² + b·s + c in the scale s of its load current. Copper: |s·J_stage +
    J_others|² (a cross term where the stage shares copper with the others); conduction and DCR ∝ s², switching
    and a catch diode ∝ s, C_oss·V²·f_sw constant. The DC solve is linear: the paths' currents scale with s."""
    stages, bridges = _stages(converters), _bridges(converters)
    specs = {c.name: c for c in config.converters}
    names = [c.net for c in stages if specs.get(c.net) is not None and (specs[c.net].i_out or 0) > 0]
    # DC rails with loads (analysis/dcload.py): the load is the rail's total current, all its loads scaled together
    rails = {c.net: c.dc[0]["current_a"] for c in stages if c.kind == "dcload" and c.dc}
    names += [n for n in rails if n not in names]
    notes = [f"{c.net}: no load current (i_out), so no capacity" for c in stages if c.net not in names]
    nominal = {n: (rails[n] if n in rails else specs[n].i_out) for n in names}
    for b in bridges:              # a bridge's load is its phase current
        spec = next((x for x in config.bridges if x.name == b.net), None)
        if spec is not None and spec.i_phase:
            nominal[b.net] = spec.i_phase
            names.append(b.net)
    if bridges:
        notes.append("motor bridges: the copper's RMS heat taken ∝ i_phase², the parts' losses from the drive at "
                     "0.5, 1 and 1.5 × i_phase")
    # with several stages also all of them together, scaled by one factor ("the board at x % of every load")
    groups = {n: {n} for n in names}
    if len(names) > 1:
        groups[ALL_STAGES] = set(names)
    rho = _rho_mm()
    heat, terms = {}, {g: {} for g in groups}
    for layer, (X, Y, d, t, per_stage, per_rms) in _layer_currents(board, stages, bridges).items():
        k = rho * t * d * d
        zero = np.zeros_like(X)
        JX = sum((jx for jx, _ in per_stage.values()), zero)
        JY = sum((jy for _, jy in per_stage.values()), zero)
        R = sum(per_rms.values(), zero)
        keep = (sum((jx ** 2 + jy ** 2 for jx, jy in per_stage.values()), zero) + R) > 0
        heat[layer] = (X[keep], Y[keep], d, (k * (JX ** 2 + JY ** 2 + R))[keep])
        for g, members in groups.items():
            jx = sum((per_stage.get(n, (zero, zero))[0] for n in members), zero)
            jy = sum((per_stage.get(n, (zero, zero))[1] for n in members), zero)
            own = sum((per_rms.get(n, zero) for n in members), zero)
            rx, ry = JX - jx, JY - jy
            terms[g][layer] = ((k * (jx ** 2 + jy ** 2 + own))[keep], (2 * k * (jx * rx + jy * ry))[keep],
                               (k * (rx ** 2 + ry ** 2 + R - own))[keep])
    # the via barrels' heat [A43], appended to every layer's samples in _with_vias' order: per group R·(s·I_g +
    # I_rest)² + R·(s²·var_g + var_rest), the group's own currents and variances scaling with s
    segs = _via_segments(stages, bridges)
    heat = _with_vias(heat, segs)
    for g, members in groups.items():
        def split(s, members=members) -> tuple:
            """(a, b, c) of the segment's heat as a quadratic in the group's scale."""
            ig = sum(v for n, v in s["i"].items() if n in members)
            ir = sum(v for n, v in s["i"].items() if n not in members)
            vg = sum(v for n, v in s["sq"].items() if n in members)
            vr = sum(v for n, v in s["sq"].items() if n not in members)
            return s["r"] * (ig * ig + vg), 2 * s["r"] * ig * ir, s["r"] * (ir * ir + vr)
        cols = [_via_rows(segs, lambda s, i=i: split(s)[i] / 2.0) for i in range(3)]
        for layer in cols[0]:
            old = terms[g].get(layer, (np.zeros(0), np.zeros(0), np.zeros(0)))
            terms[g][layer] = tuple(np.concatenate([old[i], cols[i][layer]]) for i in range(3))
    losses, loss_notes = part_losses(board, config, stages)
    losses = _merged(losses, *(b.losses for b in bridges))
    fixed = {b.net: {r["ref"]: (0.0, 0.0, r["power_w"]) for r in b.losses} for b in bridges}
    scaled = {b.net: _bridge_terms(board, config, b) for b in bridges if b.net in names}
    loss_terms = {}
    for g, members in groups.items():
        conv = members & (set(specs) | set(rails))
        at = [{r["ref"]: r["power_w"] for r in part_losses(board, *_scaled(config, stages, conv, s))[0]}
              for s in (0.0, 1.0, 2.0)]
        lt = {ref: _quadratic(*(p.get(ref, 0.0) for p in at)) for ref in sorted(set().union(*at))}
        for b in bridges:
            for ref, abc in (scaled[b.net] if b.net in members else fixed[b.net]).items():
                lt[ref] = tuple(x + y for x, y in zip(lt.get(ref, (0.0, 0.0, 0.0)), abc))
        loss_terms[g] = lt
    info = {n: {"i_out": nominal[n]} for n in names}
    if ALL_STAGES in groups:
        info[ALL_STAGES] = {"i_out": None, "members": dict(nominal)}
    return {"heat": heat, "losses": losses, "notes": loss_notes + notes, "terms": terms, "loss_terms": loss_terms,
            "stages": info}


def _bridge_terms(board: Board, config: Config, b) -> dict[str, tuple]:
    """{ref: (a, b, c)}: a motor bridge's part losses as quadratics in the scale of its phase current, fitted to
    the drive at 0.5, 1 and 1.5 × i_phase."""
    at = [_bridge_losses_at(board, config, b, s) for s in (0.5, 1.0, 1.5)]
    return {ref: _fit((0.5, 1.0, 1.5), [p.get(ref, 0.0) for p in at]) for ref in sorted(set().union(*at))}


def run_capacity(board: Board, config: Config, converters, progress: Optional[Progress] = None,
                 cancel=None) -> list[ThermalResult]:
    """The current capacity of these power-stage and motor bridge results, in this process (the CLI; the plugin
    uses thermal_proc.capacity_in_process)."""
    if not _stages(converters) and not _bridges(converters):
        return [_empty(config, NO_STAGES)]
    return solve_capacity(board, config, capacity_inputs(board, config, converters), progress, cancel)


def solve_capacity(board: Board, config: Config, inputs: dict, progress: Optional[Progress] = None,
                   cancel=None) -> list[ThermalResult]:
    """Per power stage: the load current at which the hottest copper or part junction (θJB given) reaches
    thermal.capacity_c (default max_c), the other stages at their operating points, as a heatmap view at that
    current. Each trial current is a coupled solve on a model built once, started from the previous temperatures;
    the search is a bracketed secant on s² (the heat is mostly ∝ s²) to within CAPACITY_TOL_K.

    Two passes, because PCB Thermal's adaptive mesh refines where the heat is: a draft model planned for the
    operating points finds where each capacity lies, then a model at thermal.quality planned for the heat at
    those currents (each stage at its capacity, an envelope over the stages) settles it. Without the second pass
    a fresh solve at the capacity current came out 1.2 K off on the test buck (its mesh refined for more heat)."""
    from dataclasses import replace
    tc = config.thermal
    limit = tc.capacity_c if tc.capacity_c is not None else tc.max_c
    names = list(inputs["stages"])
    if not names:
        return [_empty(config, "no power stage has a load current (i_out) to scale")]
    if limit <= tc.ambient_c:
        return [_empty(config, f"the capacity limit {limit:g} °C is not above the ambient {tc.ambient_c:g} °C")]
    say = _sayer(progress)

    def part(lo, hi):                     # progress of a pass, mapped into [lo, hi]
        """A progress function for one pass, mapped into [lo, hi] of the bar."""
        return lambda msg, frac: say(msg, lo + (hi - lo) * frac)

    # pass 1: draft, planned for the operating points
    draft = replace(config, thermal=replace(tc, quality="draft"))
    m1 = _build_model(board, draft, inputs["heat"], inputs["losses"], inputs["notes"], part(0.0, 0.25), cancel)
    k1 = _Capacity(board, m1, inputs, limit, part(0.25, 0.4))
    base = k1.run(names[0], 1.0, 0, 1)    # the operating point: the same solve for every stage
    first = {}
    for k, name in enumerate(names):
        tried = [base]
        k1.search(name, tried, _first_guess(inputs, name, base[1], limit, tc.ambient_c), k, len(names),
                  CAPACITY_PASS1_TOL_K)
        first[name] = min(tried, key=lambda t: abs(t[1]))[0]

    # pass 2: thermal.quality, the mesh planned for the heat with each stage at its capacity
    s_plan = {n: min(first[n], CAPACITY_MAX_SCALE) for n in names}
    heat = {}
    for L, (x, y, d, _) in inputs["heat"].items():
        heat[L] = (x, y, d, np.max([a * s_plan[n] ** 2 + b * s_plan[n] + c
                                    for n in names for a, b, c in [inputs["terms"][n][L]]], axis=0))
    powers: dict[str, float] = {}
    for n in names:
        for r, (a, b, c) in inputs["loss_terms"][n].items():
            powers[r] = max(powers.get(r, 0.0), a * s_plan[n] ** 2 + b * s_plan[n] + c)
    losses = [{"ref": r, "power_w": p, "what": ""} for r, p in powers.items() if p > 0]
    del k1, m1, base
    m2 = _build_model(board, config, heat, losses, inputs["notes"], part(0.4, 0.7), cancel)
    k2 = _Capacity(board, m2, inputs, limit, part(0.7, 1.0))
    results = []
    for k, name in enumerate(names):
        s0 = first[name]
        tried = [k2.run(name, s0, k, len(names))]
        f0 = tried[0][1]
        # Joule heat goes with I^2, so locally the temperature rise goes with s^2: the guess scales s0 by
        # sqrt(wanted rise / rise at s0)
        rise = f0 + limit - tc.ambient_c          # locally the rise goes with s²
        guess = s0 * math.sqrt((limit - tc.ambient_c) / rise) if math.isfinite(rise) and rise > 0 else 0.5 * s0
        bound = k2.search(name, tried, min(max(guess, 0.0), CAPACITY_MAX_SCALE), k, len(names))
        s, f, sol, P, where = min(tried, key=lambda t: abs(t[1]))
        results.append(k2.result(name, s, f, sol, P, where, bound, len(tried), config))
        del tried, sol
    say("thermal: done", 1.0)
    return results


class _Capacity:
    """The capacity search on one thermal model: the heat at a stage's load-current scale, the hottest point of a
    solution, the trial solves."""

    def __init__(self, board: Board, m: _Model, inputs: dict, limit: float, say):
        """The search on model `m`: each part's heat distribution per watt (computed once, so a trial only scales
        it), and the pads its junction estimate averages over (parts with a theta_jb)."""
        from ..thermal.project import ComponentPower
        self.board, self.m, self.inputs, self.limit, self.say = board, m, inputs, limit, say
        th = m.th
        # each part's heat per watt (its pads), and the pads a junction estimate averages over
        self.refs = sorted(set().union(*(lt.keys() for lt in inputs["loss_terms"].values())))
        self.theta = {r: m.tc.theta_jb.get(r, 0.0) for r in self.refs}
        self.unit, self.pads = {}, {}
        saved = m.setup.components
        try:
            for r in self.refs:
                m.setup.components = [ComponentPower(r, 1.0, 0.0)]
                q, _ = th.component_heat()
                i = np.nonzero(q)[0]
                self.unit[r] = (i, q[i])
        finally:
            m.setup.components = saved
        for r in self.refs:
            fp = m.tb.footprint(r)
            got = [th.pad_nodes(p) for p in fp.pads if p.layers] if fp is not None and self.theta[r] > 0 else []
            if got:
                w = np.concatenate([g[1] for g in got])
                self.pads[r] = (np.concatenate([g[0] for g in got]), w / w.sum())
        self.near = {r: board.footprint(r).position for r in self.refs if board.footprint(r) is not None}
        self.what = {l["ref"]: l["what"] for l in inputs["losses"]}
        self.T_warm = None

    def heat_at(self, name: str, s: float):
        """The heat with stage `name` at s x its load current: every heat term is a quadratic in s (a s^2 for the
        I^2 R losses, b s for switching losses, c for what does not scale), for the copper samples and the parts."""
        m, t = self.m, self.inputs["terms"][name]
        p20 = (np.concatenate([t[L][0] * s * s + t[L][1] * s + t[L][2] for L in m.layers]) if m.layers
               else np.zeros(0))
        P = {r: max(a * s * s + b * s + c, 0.0) for r, (a, b, c) in self.inputs["loss_terms"][name].items()}
        Qc = np.zeros(m.th.n)
        for r, p in P.items():
            if p > 0 and r in self.unit:
                Qc[self.unit[r][0]] += p * self.unit[r][1]
        return p20, Qc, P

    def hottest(self, sol: _Solution, P: dict) -> tuple[float, str]:
        """(hottest temperature, where): a part's junction (its pads' mean temperature + P theta_jb) when one is
        hotter than any copper, else the hottest copper node and the part next to it."""
        T, th = sol.T, self.m.th
        hot, where = float(T.max()), ""
        for r, (n_, w) in self.pads.items():
            tj = float(T[n_] @ w) + P.get(r, 0.0) * self.theta[r]
            if tj > hot:
                hot, where = tj, f"{r}'s junction"
        if not where:
            col, slab = th.node_column_slab(int(np.argmax(T)))
            x, y = float(self.m.mesh.col_x[col]), float(self.m.mesh.col_y[col])
            near = self.near
            by = min(near, key=lambda r: math.hypot(near[r][0] - x, near[r][1] - y), default=None)
            at = f" at {by}" if by and math.hypot(near[by][0] - x, near[by][1] - y) < 3.0 else ""
            where = f"copper{at} ({slab.name if slab.kind == 'copper' else 'inside the board'})"
        return hot, where

    def run(self, name: str, s: float, k: int, n: int, count: int = 0) -> tuple:
        """One trial: (s, f = hottest − limit, solution, part powers, where); f is inf at a thermal runaway."""
        p20, Qc, P = self.heat_at(name, s)
        sol = _coupled(self.m, p20, Qc, None, self.T_warm)
        if sol.error:
            f, where = math.inf, "thermal runaway"
        else:
            hot, where = self.hottest(sol, P)
            f, self.T_warm = hot - self.limit, sol.T
        i_out = self.inputs["stages"][name]["i_out"]
        load = f"{s * i_out:.3g} A" if i_out else f"{s * 100:.0f} % load"
        self.say(f"capacity of {name}: {load} → " + ("thermal runaway" if math.isinf(f)
                 else f"{f + self.limit:.1f} °C"), (k + min(count / 5, 0.95)) / n)
        return s, f, sol, P, where

    def search(self, name: str, tried: list, guess: float, k: int, n: int, tol: float = CAPACITY_TOL_K) -> str:
        """Trials appended to `tried` until the root is found or bounded (_search's answer)."""
        pairs = [(t[0], t[1]) for t in tried]

        def run(s):
            """One trial at scale s, recorded."""
            tried.append(self.run(name, s, k, n, len(tried)))
            pairs.append((s, tried[-1][1]))
        return _search(run, pairs, guess, tol)

    def result(self, name, s, f, sol, P, where, bound, solves, config) -> ThermalResult:
        """The thermal view of a stage at its capacity: the solution's maps with the capacity found (current,
        scale, what limits it, whether it is only a bound)."""
        from ..thermal.project import ComponentPower
        m = self.m
        m.setup.components = [ComponentPower(r, P.get(r, 0.0), self.theta.get(r, 0.0)) for r in self.refs]
        losses = sorted(({"ref": r, "power_w": p, "what": self.what.get(r, "")} for r, p in P.items() if p > 0),
                        key=lambda l: -l["power_w"])
        info = self.inputs["stages"][name]
        i_out = info["i_out"]
        cap = {"stage": name, "i_nominal": i_out, "i_capacity": s * i_out if i_out else None, "scale": s,
               "limit_c": self.limit, "t_hottest": f + self.limit, "limited_by": where, "bound": bound,
               "solves": solves}
        if info.get("members"):           # all stages together: each one's current at that load
            cap["currents"] = {n: s * i for n, i in info["members"].items()}
        elif bound == "zero":
            cap["blocked_by"] = self.blocked_by(name, where)
        res, _ = _result(self.board, config, m, sol, losses, cap)
        if not bound and abs(f) > CAPACITY_TOL_K:
            res.notes.append(f"the search stopped {f:+.1f} K from the limit after {solves} solves")
        return res

    def blocked_by(self, name: str, where: str) -> list[str]:
        """Over the limit with stage `name` at no load: the other stages whose own loss heats the part at the
        hottest spot ("copper at Q301 (F.Cu)", "Q301's junction"), i.e. the part's loss grows with that stage's
        load (a quadratic or linear term in its scale), so it is their operating point that is already too hot.
        Empty when the spot is at no part, or the part's loss is a constant of every stage."""
        m = re.match(r"(\S+)'s junction", where) or re.search(r" at (\S+) \(", where)
        ref = m.group(1) if m else None
        if ref is None:
            return []
        out = []
        for n, terms in self.inputs["loss_terms"].items():
            a, b, _ = terms.get(ref, (0.0, 0.0, 0.0))
            # its loss at its operating point that grows with its load (over 0.1 mW: not the fit's rounding)
            if n != name and not self.inputs["stages"].get(n, {}).get("members") and a + b > 1e-4:
                out.append(n)
        return out


def _first_guess(inputs: dict, name: str, f1: float, limit: float, ambient: float) -> float:
    """The scale at which the stage's share of the heat would bring the operating point's rise to the limit, if
    the rise followed the total heat: A·s² + B·s + C = H(1)·(limit − ambient) / rise(1)."""
    A = B = C = 0.0
    for a, b, c in inputs["terms"][name].values():
        A, B, C = A + float(a.sum()), B + float(b.sum()), C + float(c.sum())
    for a, b, c in inputs["loss_terms"][name].values():
        A, B, C = A + a, B + b, C + c
    rise1 = f1 + limit - ambient
    if not math.isfinite(rise1) or rise1 <= 0 or A + B + C <= 0:
        return 0.5
    target = (A + B + C) * (limit - ambient) / rise1
    if A > 1e-15:
        disc = B * B - 4 * A * (C - target)
        s = (-B + math.sqrt(disc)) / (2 * A) if disc >= 0 else 0.0
    else:
        s = (target - C) / B if B > 0 else CAPACITY_MAX_SCALE
    return float(min(max(s, 0.0), CAPACITY_MAX_SCALE))


def _search(run, tried: list, guess: float, tol: float = CAPACITY_TOL_K) -> str:
    """Bracketed secant on u = s² for the root of f(s) = hottest − limit, from the trials so far [(s, f)] and a
    guess for the next one; run(s) adds a trial to `tried`.
    Returns "" (found), "above" (still under the limit at CAPACITY_MAX_SCALE) or "zero" (over it at no load)."""
    top = CAPACITY_MAX_SCALE ** 2
    s0, f0 = tried[0]
    s_next = guess if abs(guess - s0) > 1e-3 * max(s0, 1.0) else (s0 * 1.05 if f0 < 0 else s0 * 0.95)
    while True:
        under = sorted((s * s, f) for s, f in tried if f < 0)
        over = sorted((s * s, f) for s, f in tried if f >= 0)
        lo, hi = (under[-1] if under else None), (over[0] if over else None)   # (u, f) next to the root
        if min(abs(f) for _, f in tried) <= tol:
            return ""
        if lo and lo[0] >= top - 1e-9:
            return "above"
        if hi and hi[0] <= 0.0:
            return "zero"
        if lo and hi and math.sqrt(hi[0]) - math.sqrt(lo[0]) < 2e-3 * math.sqrt(hi[0]):
            return ""
        if len(tried) >= CAPACITY_MAX_SOLVES:
            return ""
        if len(tried) > 1:                # the first step takes the guess
            if lo and hi:                 # regula falsi in u, kept off the bracket's ends (bisection at a runaway)
                span = hi[0] - lo[0]
                u = 0.5 * (lo[0] + hi[0]) if math.isinf(hi[1]) else lo[0] - lo[1] * span / (hi[1] - lo[1])
                u = min(max(u, lo[0] + 0.05 * span), hi[0] - 0.05 * span)
            elif lo:                      # all under the limit: extrapolate up from the two highest
                (ua, fa), (ub, fb) = under[-2], under[-1]
                slope = (fb - fa) / (ub - ua) if ub > ua else 0.0
                u = ub - fb / slope if slope > 0 else 4 * ub
                u = min(max(u, 1.005 * ub), 4 * max(ub, 0.25), top)
            else:                         # all over it: extrapolate down from the two lowest
                finite = [(u_, f) for u_, f in over if math.isfinite(f)]
                if len(finite) >= 2:
                    (ua, fa), (ub, fb) = finite[0], finite[1]
                    slope = (fb - fa) / (ub - ua) if ub > ua else 0.0
                    u = ua - fa / slope if slope > 0 else 0.25 * ua
                    u = 0.0 if u <= 0 else min(max(u, 0.25 * ua), 0.995 * ua)   # over even at no load? try it
                else:
                    u = 0.25 * hi[0]
                if u < 1e-3:
                    u = 0.0
            s_next = math.sqrt(max(u, 0.0))
        run(s_next)
