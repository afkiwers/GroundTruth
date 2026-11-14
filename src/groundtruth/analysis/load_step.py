"""A power stage's response to a load step: its closed-loop output impedance and the output voltage over time when
the load current steps (an electronic load), or when a load branch R + sL is switched on (a heater coil), through a
harness to a load capacitance. The user: "It would be good to see how they react on step loads with let's say LCR
loads attached to them under load", and of the load: "Cable + load caps or load inductor. There are usecases where
the load is a heating coil that could be a pure inductor".

The model, all exact rationals in s (analysis/loop.Rat), so the step's poles, zeros and time response are exact
(transfer.exact_step, partial fractions [Ogata 2010]):

- **The converter** in closed loop as a Norton admittance Y_conv(s) at its output node, from the stage's averaged
  small-signal model (loop.LoopModel: peak current or voltage mode, buck or boost [Ridley 1991] [Erickson 2001]);
  its output capacitors Y_caps beside it (their C at DC bias and ESR), so the output node's admittance is
  Y_A = Y_conv + Y_caps [A117]. A constant-on-time or internally compensated controller, whose loop is not
  modelled from its parts, gets an approximate loop from its datasheet's bandwidth and phase margin (the
  controller's Loop Bandwidth and Loop Phase Margin fields, or the stage's `loop: {fc, pm}`) [A115].
- **The load** (config.LoadStep): a harness Z_c = R_c + sL_c from the output node to the load node, at the load node
  a capacitance C_load with its ESR and the load before the step as a resistor V/I_from. The step is either a
  current I_to − I_from drawn at the load node (over `rise`: a ramp), or a branch Z_b = R + sL switched on there.
- **The nodes**, by nodal analysis: the load node's impedance Z_22 = 1/(Y_ln + 1/(Z_c + 1/Y_A)), the output's
  transfer Z_21 = 1/(Y_ln·(1 + Z_c·Y_A) + Y_A) (no harness: both 1/(Y_ln + Y_A)). A current step ΔI gives
  Δv_2 = −Z_22·ΔI/s and Δv_1 = −Z_21·ΔI/s. A branch switched on at a node at V_2 draws, by the compensation theorem
  [Desoer 1969], i_b = (V_2/s)/(Z_b + Z_22) with the network around it unchanged, and Δv = −Z·i_b.

The figures: the deepest excursion of each node's voltage and when, the final deviation (the harness's IR drop; 0
at the output with an integrating loop), the recovery time into ±RECOVER_BAND of the voltage for good, and the
converter's current with its fastest slew. The model is linear (small signal): a step the inductor cannot follow
(the current's slew above (V_in − V_out)/L rising, V_out/L falling) or that drives the duty cycle to its limit is
larger in the real stage; a note says so [A116]. The step down is the mirror image in this model.

Validated in tests/test_load_step.py: the closed-loop output impedance against Z_ol/(1 + T) of the loop model,
the step against a state-space solve of the averaged circuit, ΔV ≈ ΔI/(2π·f_c·C) for a loop crossing over on its
capacitors, the coil's compensation theorem against the circuit with the branch in it, the ramp's limits, and the
approximate loop's crossover and phase margin. Source keys in square brackets: docs/REFERENCES.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Optional

import numpy as np

from ..config.settings import ConverterSpec, LoadStep
from .loop import LoopModel, Rat, analyse_loop, controller, find_loop, output_admittance, roots_scaled

RECOVER_BAND = 0.01          # recovered: back within ±1 % of the voltage of its final value, for good
PM_MAX = 89.0                # the approximate loop's phase margin at most (90°: its pole and zero at 0 and ∞)
SWEEP_DECADES = 5            # the impedance curves: from f_sw / 10^5 ...
SWEEP_POINTS = 600           # ... to f_sw, this many points
SLEW_MARGIN = 1.0            # the converter current's slew above this share of what the inductor can do: a note
LARGE_DEV = 0.10             # a node moving by more than this share of its voltage: beyond the small-signal model


# ------------------------------------------------------------------------------------------------ results
@dataclass
class NodeStep:
    """One node's voltage over the step: its name, the times (s), the voltage (V, the operating point plus the
    deviation) and the figures (`metrics`, see step_figures)."""
    node: str
    t: np.ndarray
    v: np.ndarray
    metrics: dict


@dataclass
class LoadStepResult:
    """A stage's load step (see the module): the step as resolved, the closed-loop figures, the node responses,
    the impedance curves (transfer.Response) and notes; error when there is no model."""
    stage: str
    v_out: float = 0.0
    step: Optional[LoadStep] = None
    approximate: bool = False
    nodes: list = field(default_factory=list)            # [NodeStep]: the output, and the load behind a harness
    current: Optional[dict] = None                       # {"t", "i_load", "i_conv", "slew"}
    responses: list = field(default_factory=list)        # transfer.Response: Z_ol, Z_out closed loop, Z at the load
    figures: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    error: str = ""

    @property
    def worst(self) -> Optional[NodeStep]:
        """The node with the largest excursion either way."""
        return max(self.nodes, key=lambda n: n.metrics["peak"], default=None)

    def summary(self) -> dict:
        """Plain data for the stage's summary and the JSON report (curves decimated)."""
        from dataclasses import asdict
        out = {"stage": self.stage, "v_out": self.v_out, "approximate": self.approximate, "error": self.error,
               "notes": list(self.notes), "figures": dict(self.figures),
               "step": asdict(self.step) if self.step else None, "nodes": []}
        for n in self.nodes:
            k = np.unique(np.linspace(0, len(n.t) - 1, min(400, len(n.t))).astype(int))
            out["nodes"].append({"node": n.node, "metrics": n.metrics, "t": [float(n.t[i]) for i in k],
                                 "v": [float(n.v[i]) for i in k]})
        return out


# ------------------------------------------------------------------------------------------------ the step
def resolved(ls: Optional[LoadStep], spec: ConverterSpec) -> LoadStep:
    """The stage's load step with its defaults filled in: none given, half the load current to all of it; a switched
    branch from no load; a current step to the stage's load current."""
    ls = ls or LoadStep()
    i_from = ls.i_from if ls.i_from is not None else (0.0 if ls.switched else 0.5 * (spec.i_out or 0.0))
    i_to = ls.i_to if ls.i_to is not None else (spec.i_out or 0.0)
    return replace(ls, i_from=i_from, i_to=i_to)


def approximate_loop(fc: float, pm: float) -> Rat:
    """A loop gain from its crossover f_c and phase margin alone [A115]: T(s) = K·(1 + s/ω_z)/(s²·(1 + s/ω_p)), an
    integrating (type II) loop around a capacitive plant, its zero and pole spread m = tan(45° + PM/2) either side
    of ω_c (Venable's K factor [Venable 1983]): ∠T(jω_c) = −180° + atan(m) − atan(1/m) = PM − 180°, and K = ω_c²/m
    makes |T(jω_c)| = 1."""
    wc = 2 * math.pi * fc
    m = math.tan(math.radians(45.0 + min(max(pm, 1.0), PM_MAX) / 2))
    wz, wp = wc / m, wc * m
    return Rat([wc * wc / m / wz, wc * wc / m], [1.0 / wp, 1.0, 0.0, 0.0])


def converter_model(board, config, spec: ConverterSpec, i_op: float) -> tuple[Optional[LoopModel], list, str]:
    """The stage's closed-loop model at the load current i_op: (LoopModel, notes, error). The loop's own model where
    analysis/loop.py has one; else, with a loop bandwidth and phase margin (the controller's fields or the stage's
    loop: override), the approximate loop around the output capacitors: Y_conv = T₀·Y_o₀, the controller's gain
    fixed by T₀ at the stage's own load (the modulator a current source into Y_o, as peak current mode is) [A115]."""
    at = replace(spec, i_out=i_op)
    # the load before the step as a resistor (a current of 0: none), so the operating point is i_op's
    lr = analyse_loop(board, config, at, y_load=None if i_op else Rat([0.0]))
    notes = []
    if lr is not None and not lr.error and lr.model is not None:
        return lr.model, list(lr.notes), ""
    parts = lr.parts if lr is not None else find_loop(board, config, spec)
    over = dict(getattr(spec, "loop", None) or {})
    params = dict(parts.params) if parts is not None else {}
    from ..components.values import parse_value
    for key in ("fc", "pm"):
        if over.get(key) is not None and key not in params:
            v = over[key]
            params[key] = (float(v) if isinstance(v, (int, float)) else parse_value(str(v)), "settings (loop)")
    if "fc" not in params or "pm" not in params:
        why = lr.error if lr is not None and lr.error else "no controller found for the stage"
        ctrl_fp = controller(board, spec)[0]
        ctrl = parts.controller if parts is not None else (ctrl_fp.reference if ctrl_fp is not None else "")
        return None, notes, (f"{why}; for the load step give {ctrl or 'the controller'} Loop Bandwidth and Loop "
                             "Phase Margin fields (its datasheet's), or the stage loop: {fc, pm}")
    fc, pm = params["fc"][0], params["pm"][0]
    if not (spec.v_out and fc and fc > 0):
        return None, notes, "the approximate loop needs the stage's V out and a loop bandwidth above 0"
    y_o0, caps = output_admittance(board, config, spec, spec.v_out / spec.i_out if spec.i_out else math.inf)
    if not caps:
        return None, notes, "no output capacitor found from the output to ground"
    y_caps, _ = output_admittance(board, config, spec, math.inf, Rat([0.0]))
    t0 = approximate_loop(fc, pm)
    y_conv = t0 * y_o0
    where = (params["fc"][1] if params["fc"][1] == params["pm"][1] else f"{params['fc'][1]}; {params['pm'][1]}")
    notes.append(f"an approximate loop from the loop bandwidth {fc / 1e3:.3g} kHz and phase margin {pm:.0f}° "
                 f"({where}) at {spec.i_out:g} A, the controller as a current source into the output capacitors "
                 "[A115]")
    y_o, _ = output_admittance(board, config, at, at.v_out / i_op if i_op else math.inf)
    return LoopModel(t0, y_o0.inv(), y_o, y_conv, y_caps, caps, approximate=True), notes, ""


def _poly_rat(h: Rat) -> tuple[list, list, complex, int]:
    """A rational's poles, zeros (rad/s, roots at the origin left out), constant k and the net power m of s at the
    origin, as transfer.exact_step takes them: H = k·s^m·Π(1 − s/z)/Π(1 − s/p). Pole-zero pairs that cancel are
    dropped (transfer.cancel_pairs: a capacitor's ESR zero against the same pole in a sum)."""
    from .transfer import cancel_pairs
    n, d = np.trim_zeros(h.n, "b"), np.trim_zeros(h.d, "b")
    mz, mp = len(h.n) - len(n), len(h.d) - len(d)
    w0 = _scale(h)
    zeros, poles = roots_scaled(n, w0), roots_scaled(d, w0)
    poles, zeros = cancel_pairs(poles, zeros)
    return poles, zeros, complex(n[-1] / d[-1]), mz - mp


def _scale(h: Rat) -> float:
    """A frequency (rad/s) to find a rational's roots at: the geometric mean of its roots' sizes, from the
    polynomials' end coefficients (|c_0/c_n|^(1/n))."""
    sizes = []
    for p in (np.trim_zeros(h.n, "b"), np.trim_zeros(h.d, "b")):
        if len(p) > 1 and p[0] != 0:
            sizes.append(abs(p[-1] / p[0]) ** (1.0 / (len(p) - 1)))
    return float(np.exp(np.mean(np.log(sizes)))) if sizes else 1.0


def _step(h: Rat):
    """The exact step response of h (transfer.ExactStep), or None (an improper or ill-conditioned one)."""
    from .transfer import exact_step
    poles, zeros, k, m = _poly_rat(h)
    return exact_step(poles, zeros, k, m)


def _ramp(ex, t: np.ndarray, tr: float) -> np.ndarray:
    """The response to a unit ramp from 0 to 1 over tr (then held) from an exact step's closed form:
    (Y₁(t) − Y₁(t − tr))/tr with Y₁ the step response's integral, final·t + Σ a/p·(e^{pt} − 1) for its simple poles
    [Ogata 2010]; a repeated pole's term integrated numerically on the times (trapezoids)."""
    def integral(x):
        """∫₀ˣ y(τ) dτ at the times x (0 for x ≤ 0)."""
        x = np.maximum(np.asarray(x, float), 0.0)
        out = ex.final * x
        acc = np.zeros(x.shape, complex)
        rest = []
        for p, a in zip(ex.poles, ex.coefs):
            if len(a) == 1 and p != 0:
                acc += a[0] / p * np.expm1(p * x)
            else:
                rest.append((p, a))
        out = out + acc.real
        if rest:
            grid = np.unique(np.concatenate([np.linspace(0, max(float(np.max(x)), 1e-30), 20001), x]))
            yv = sum((np.exp(p * grid) * np.polyval(a[::-1], grid)).real for p, a in rest)
            from scipy.integrate import cumulative_trapezoid
            cum = cumulative_trapezoid(yv, grid, initial=0.0)
            out = out + np.interp(x, grid, cum)
        return out
    if tr <= 0:
        return ex(t)
    return (integral(t) - integral(t - tr)) / tr


def step_figures(t: np.ndarray, dv: np.ndarray, final: float, v_nom: float, band: float, down: bool = True) -> dict:
    """A node's figures from its deviation dv(t) (V) after the step: the dip, its deepest excursion the way the step
    pushes it (down for more load: `down`), and when, in volts and per cent of v_nom; the final deviation; the
    largest swing past the final value the other way (`back`: a lightly damped loop rings back above the start,
    sometimes further than it dipped); `peak`: the largest |dv| either way; the recovery time: the last time
    |dv − final| is above band·v_nom (0 when it never is, None when still out at the end)."""
    from .transfer import _last_exit
    sign = -1.0 if down else 1.0
    k = int(np.argmax(sign * dv))
    dip = float(dv[k])
    back = float(np.max(-sign * (dv - final))) if len(dv) else 0.0
    rec = _last_exit(t, np.abs(dv - final), band * v_nom)
    return {"kind": "load", "dip": dip, "t_dip": float(t[k]), "dip_pct": 100 * dip / v_nom if v_nom else None,
            "final": float(final), "back": max(back, 0.0), "peak": float(np.max(np.abs(dv))), "recovery": rec,
            "band": band}


def _times(exs: list, f_low: float) -> np.ndarray:
    """The times to show: exact_step_response's grid of the slowest of the responses (its significant poles'
    window and ringing)."""
    from .transfer import exact_step_response
    grids = [exact_step_response(ex, f_low)["t"] for ex in exs if ex is not None]
    if not grids:
        return np.linspace(0.0, 1.0 / f_low, 2000)
    t_end = max(float(g[-1]) for g in grids)
    return np.unique(np.concatenate([g[g <= t_end] for g in grids] + [np.linspace(0.0, t_end, 3000)]))


def analyse_load_step(board, config, spec: ConverterSpec, ls: Optional[LoadStep] = None) -> LoadStepResult:
    """The stage's load step (see the module): `ls` (default: the stage's own load_step, else half to full load)."""
    from .transfer import Response
    ls = resolved(ls if ls is not None else spec.load_step, spec)
    res = LoadStepResult(spec.name, v_out=float(spec.v_out or 0.0), step=ls)
    if not spec.v_out or not spec.f_sw:
        res.error = "the load step needs the stage's V out and f_sw"
        return res
    model, notes, err = converter_model(board, config, spec, ls.i_from)
    res.notes += notes
    if model is None:
        res.error = err
        return res
    res.approximate = model.approximate
    v = float(spec.v_out)
    one = Rat([1.0])
    y_a = model.y_conv + model.y_caps                                  # the output node: converter and its caps
    # the load node: C_load with its ESR, and the load before the step as a resistor V/I_from
    y_ln = Rat([0.0])
    if ls.c_load:
        y_ln = y_ln + Rat([ls.c_load, 0.0], [ls.c_load * ls.esr_load, 1.0])
    v_load = v - ls.i_from * ls.cable_r
    if ls.i_from:
        y_ln = y_ln + Rat([ls.i_from / v_load])
    cable = ls.cable_r > 0 or ls.cable_l > 0
    if cable:
        zc = Rat([ls.cable_l, ls.cable_r]) if ls.cable_l else Rat([ls.cable_r])
        den = y_ln * (one + zc * y_a) + y_a
        z22 = (one + zc * y_a) * den.inv()
        z21 = den.inv()
    else:
        z22 = z21 = (y_ln + y_a).inv()
    # the step at the load node: a current, or a switched branch's own current (compensation theorem)
    if ls.switched:
        zb = Rat([ls.l_load, ls.r_load or 0.0]) if ls.l_load else Rat([ls.r_load or 0.0])
        if not any(zb.n) and not any(z22.n):
            res.error = "the switched load branch has neither R nor L"
            return res
        i_rat = (zb + z22).inv() * v_load                              # s·i_b(s): the branch's current
        amp, rise = 1.0, 0.0
    else:
        i_rat = one
        amp, rise = ls.i_to - ls.i_from, ls.rise
    if amp == 0:
        res.error = "the load step is 0 A (from = to)"
        return res
    # the step responses (per unit step of i_rat's input): each node's −Z·i, the load current, the converter's
    nodes = [("output", (-1.0) * z21 * i_rat)] + ([("load", (-1.0) * z22 * i_rat)] if cable else [])
    i_conv_rat = model.y_conv * z21 * i_rat                            # the converter's current: −Y_conv·Δv₁
    exs = [_step(h) for _, h in nodes]
    ex_i, ex_c = _step(i_rat), _step(i_conv_rat)
    if any(e is None for e in exs):
        res.error = "the load step's response could not be expanded (an ill-conditioned model)"
        return res
    f_low = spec.f_sw / 10 ** SWEEP_DECADES
    t = _times(exs, f_low)
    for (name, _), ex in zip(nodes, exs):
        dv = amp * _ramp(ex, t, rise)
        fig = step_figures(t, dv, amp * ex.final, v, RECOVER_BAND, down=amp > 0)
        if not rise:
            from .transfer import exact_settling
            # exact: the last exit from the band on the closed form, however long after the window
            rec, bound = exact_settling(ex, RECOVER_BAND * v / abs(amp), float(t[-1]))
            fig["recovery"] = rec
            if bound:
                fig["recovery_bound"] = True
        res.nodes.append(NodeStep(name, t, v + dv if name == "output" else v_load + dv, fig))
    i_load = amp * _ramp(ex_i, t, rise) if ex_i is not None else None
    i_conv = amp * _ramp(ex_c, t, rise) if ex_c is not None else None
    slew = None
    if ex_c is not None:
        # the fastest slew on a uniform grid over the dip and its recovery (the shown grid joins several, and two
        # nearly equal times turned rounding into a slew of 10⁶ A/s)
        w = max(_slew_window(t, ex_c), 1e-15)
        tu = np.linspace(0.0, w, 20001)
        slew = float(np.max(np.abs(np.diff(amp * _ramp(ex_c, tu, rise))))) / (tu[1] - tu[0])
    res.current = {"t": t, "i_load": i_load, "i_conv": i_conv, "slew": slew}
    res.figures.update(i_from=ls.i_from, i_to=ls.i_to if not ls.switched else None, switched=ls.switched,
                       slew=slew, peak_conv=float(np.max(np.abs(i_conv))) if i_conv is not None else None,
                       i_final=amp * ex_i.final if ex_i is not None else None)
    _large_signal(board, config, spec, res, slew, amp)
    # the impedance curves: open loop (control held), closed loop at the output, and at the load behind a harness
    f = np.geomspace(spec.f_sw / 10 ** SWEEP_DECADES, spec.f_sw, SWEEP_POINTS)
    z_out = (y_a + _y_net(ls, y_ln)).inv() if cable else z21          # the output node's own, the load hung on it
    curves = [("Z_out open loop", model.z_ol), ("Z_out closed loop", z_out)]
    if cable:
        curves.append(("Z at the load", z22))
    for name, h in curves:
        poles, zeros, _, _ = _poly_rat(h)
        owner = f"{spec.name} load step"
        r = Response(f"{owner}: {name}", owner, "impedance", f, h(2j * np.pi * f), poles, zeros, exact=True)
        res.responses.append(r)
    # the time responses beside the closed loop's curves (plots.draw_step: kind "load")
    for node in res.nodes:
        target = res.responses[1] if node.node == "output" else res.responses[-1]
        target.step = {"t": node.t, "y": node.v, "metrics": node.metrics, "kind": "load", "v_nom": v
                       if node.node == "output" else v_load, "max_dev": ls.max_dev,
                       "i": i_load if node.node == ("load" if cable else "output") else None,
                       "i_conv": i_conv if node.node == "output" else None}
    w = res.worst
    res.figures.update(dip=w.metrics["dip"], dip_pct=w.metrics["dip_pct"], node=w.node, back=w.metrics["back"],
                       peak=w.metrics["peak"], peak_pct=100 * w.metrics["peak"] / v,
                       recovery=w.metrics["recovery"], fc=_crossover(model.t, spec.f_sw))
    _too_large(res)
    res.notes.append("the step down is the mirror image of the step up in this linear model")
    return res


def _slew_window(t: np.ndarray, ex) -> float:
    """The window the converter current's fastest slew is searched in (it comes soon after the step): 200 time
    scales 1/|p| of its fastest pole plus 5 % of the shown window, at most the shown window."""
    if not len(ex.poles):
        return float(t[-1])
    fast = max(abs(p) for p in ex.poles)
    return float(min(t[-1], 200.0 / fast + float(t[-1]) * 0.05))


def _y_net(ls: LoadStep, y_ln: Rat) -> Rat:
    """What hangs on the output node behind the harness: Y_ln/(1 + Z_c·Y_ln)."""
    zc = Rat([ls.cable_l, ls.cable_r]) if ls.cable_l else Rat([ls.cable_r])
    return y_ln * (Rat([1.0]) + zc * y_ln).inv()


def _crossover(t: Rat, f_sw: float) -> Optional[float]:
    """Where |T| falls through 1 (Hz), on a dense sweep up to f_sw; None when it doesn't."""
    f = np.geomspace(f_sw / 1e6, f_sw, 3000)
    mag = np.abs(t(2j * np.pi * f))
    k = np.flatnonzero((mag[:-1] >= 1) & (mag[1:] < 1))
    if not len(k):
        return None
    i = int(k[0])
    return float(math.exp(np.interp(0.0, [-math.log(mag[i]), -math.log(mag[i + 1])],
                                    [math.log(f[i]), math.log(f[i + 1])])))


def _large_signal(board, config, spec: ConverterSpec, res: LoadStepResult, slew: Optional[float], amp: float):
    """A note when the step asks more of the inductor than it can do: the converter current's fastest slew above
    (V_in − V_out)/L rising or V_out/L falling for a buck [A116] (a boost's: V_in/L and (V_out − V_in)/L)."""
    from .power import switch_inductor
    if slew is None or not (spec.v_in and spec.v_out):
        return
    _, ind = switch_inductor(board, config, spec)
    m = config.model_for(ind.reference) if ind is not None else None
    L = m.get("L", 0.0) if m is not None else 0.0
    if not L:
        return
    if spec.topology == "boost":
        up, down = spec.v_in / L, (spec.v_out - spec.v_in) / L
    else:
        up, down = (spec.v_in - spec.v_out) / L, spec.v_out / L
    lim = up if amp > 0 else down
    res.figures["slew_limit"] = lim
    if lim > 0 and slew > SLEW_MARGIN * lim:
        res.notes.append(f"the step asks the converter's current to slew at {slew * 1e-6:.3g} A/µs, the inductor "
                         f"({ind.reference}, {L * 1e6:.3g} µH) can do {lim * 1e-6:.3g} A/µs: the real dip is deeper "
                         "and longer than this linear model's (the duty cycle saturates) [A116]")


def _too_large(res: LoadStepResult):
    """A note when a node moves by more than LARGE_DEV of its voltage: the averaged model is linearised about the
    operating point, and a step that size drives the duty cycle (or the controller's current limit) to its stop
    [A116]."""
    w = res.worst
    if w is not None and res.v_out and w.metrics["peak"] > LARGE_DEV * res.v_out:
        res.notes.append(f"the {w.node} moves by {100 * w.metrics['peak'] / res.v_out:.0f} % of its voltage: beyond a small-"
                         "signal model (the duty cycle or the current limit stops the real stage) [A116]")


FIT_NOTE_DB = 1.0            # a sampled impedance's rational fit worse than this (dB): its step is approximate


def _pole_residues(fit) -> np.ndarray:
    """A vector fit's residue at each of its poles (complex), from its real basis (transfer._basis): a real pole's
    coefficient as it is; a complex pair's (c₁, c₂) give c₁ + j·c₂ at the upper pole and c₁ − j·c₂ at the lower,
    since c₁·(1/(s − a) + 1/(s − a*)) + c₂·(j/(s − a) − j/(s − a*)) = (c₁ + jc₂)/(s − a) + (c₁ − jc₂)/(s − a*)."""
    out, k = [], 0
    poles, c = fit.poles, np.asarray(fit.c, float)
    while k < len(poles):
        if abs(poles[k].imag) > 0:
            out += [complex(c[k], c[k + 1]), complex(c[k], -c[k + 1])]
            k += 2
        else:
            out.append(complex(c[k]))
            k += 1
    return np.array(out, complex)


def sampled_step(f, z, ls: LoadStep, amp: float, v_nom: float, rise: float, f_max: Optional[float] = None) -> dict:
    """The step at a port whose impedance is known only at frequencies (a PDN's field solves, analysis/pdn.py), with
    the load network of `ls` hung on it (a harness to a load capacitance, or a coil switched on): the node responses
    computed per frequency as analyse_load_step's (Y_A = 1/Z), each fitted by vector fitting [Gustavsen 1999] as
    Σ c/(s − a) + d (+ s·e without f_max), and stepped in closed form: y(t) = H(0) + Σ (c/a)·e^{at} for t > 0
    [Ogata 2010]; a ramp over `rise` as analyse_load_step's. f_max: the current the board's copper sees is the
    step through a first-order low-pass 1/(1 + s/2πf_max): above it the IC's package and die supply the current
    [A19] (a fit of the samples below f_max alone left the response above it to the fit's extrapolation: −6 V at
    t = 0 on a 3.3 V rail); the board's inductance then shows as L·ΔI·2πf_max at the start. Without f_max the
    s·e term adds e·amp/rise while the current ramps (rise > 0). {"nodes": [(name, t, v, metrics)], "fit_db":
    worst fit, "notes"}."""
    from .transfer import ExactStep, exact_step_response, vector_fit
    f = np.asarray(f, float)
    z = np.asarray(z, complex)
    s = 2j * np.pi * f
    lowpass = 1.0 / (1.0 + s / (2 * np.pi * f_max)) if f_max else np.ones_like(s)
    y_a = 1.0 / z
    y_ln = np.zeros_like(s)
    if ls.c_load:
        y_ln = y_ln + s * ls.c_load / (1 + s * ls.c_load * ls.esr_load)
    v_load = v_nom - (ls.i_from or 0.0) * ls.cable_r
    if ls.i_from:
        y_ln = y_ln + (ls.i_from or 0.0) / v_load
    cable = ls.cable_r > 0 or ls.cable_l > 0
    if cable:
        zc = ls.cable_r + s * ls.cable_l
        den = y_ln * (1 + zc * y_a) + y_a
        z22, z21 = (1 + zc * y_a) / den, 1 / den
    else:
        z22 = z21 = 1 / (y_ln + y_a)
    if ls.switched:
        zb = (ls.r_load or 0.0) + s * ls.l_load
        i_s = v_load / (zb + z22)                    # s·i_b(s): the branch's current (compensation theorem)
        amp, rise = 1.0, 0.0
    else:
        i_s = lowpass
    out = {"nodes": [], "fit_db": 0.0, "notes": []}
    nodes = [("port", -z21 * i_s, v_nom)] + ([("load", -z22 * i_s, v_load)] if cable else [])
    for name, h, v0 in nodes:
        fit = vector_fit(f, h, use_e=not f_max)
        out["fit_db"] = max(out["fit_db"], float(fit.max_db))
        a, c = np.asarray(fit.poles, complex), _pole_residues(fit)
        h0 = float(np.real(fit.d - np.sum(c / a)))
        ex = ExactStep(h0, a, [np.array([ci / ai]) for ai, ci in zip(a, c)])
        ex.scale = max(abs(h0), float(np.sum(np.abs(c / a))), 1e-300)
        t = exact_step_response(ex, float(f[0]))["t"]
        if rise > 0:
            t = np.unique(np.concatenate([t, np.linspace(0.0, 3 * rise, 400)]))
        dv = amp * _ramp(ex, t, rise)
        if rise > 0 and fit.e:
            dv = dv + amp * float(np.real(fit.e)) / rise * ((t > 0) & (t <= rise))
        out["nodes"].append((name, t, v0 + dv, step_figures(t, dv, amp * h0, v0, RECOVER_BAND, down=amp > 0)))
    if out["fit_db"] > FIT_NOTE_DB:
        out["notes"].append(f"the step is from a rational fit of the solved impedance that is off by up to "
                            f"{out['fit_db']:.1f} dB: approximate")
    return out


def step_line(r: LoadStepResult) -> str:
    """One line for the console, the tiles and the report: the step and its worst node's figures."""
    from .transfer import fmt_s
    if r.error:
        return f"{r.stage}: load step: {r.error}"
    ls, w = r.step, r.worst
    what = (f"switching on {ls.r_load or 0:g} Ω + {ls.l_load * 1e6:g} µH" if ls.switched
            else f"{ls.i_from:g} → {ls.i_to:g} A" + (f" in {fmt_s(ls.rise)}" if ls.rise else ""))
    m = w.metrics
    rec = ("not recovered in the window" if m["recovery"] is None
           else f"stays within ±{RECOVER_BAND:.0%}" if m["recovery"] == 0
           else f"recovers (±{RECOVER_BAND:.0%}) in {fmt_s(m['recovery'])}")
    back = (f", rings back {m['back'] * 1e3:.3g} mV past its final value" if m["back"] > 0.5 * abs(m["dip"]) else "")
    return (f"{r.stage}: load step {what}: {m['dip'] * 1e3:+.4g} mV ({m['dip_pct']:+.2f} %) at the {w.node} after "
            f"{fmt_s(m['t_dip'])}{back}, {rec}" + (" (approximate loop)" if r.approximate else ""))
