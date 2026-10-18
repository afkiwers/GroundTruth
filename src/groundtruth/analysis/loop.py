"""A buck or boost stage's control loop (the plan's M5): the loop gain T(s), its crossover, phase margin and gain margin, as an
exact rational function of s (so its poles and zeros are exact) from the parts on the board and the controller's
figures in its symbol fields (InvenTree parameters: components/fields.py "controller" rows, and "Control Mode").

The parts (`find_loop`): the controller is the IC with a pin on the stage's switch node (SW1 of a dual controller: the
digit picks the channel, ITH1 / V_FB1 / SENSE1±); its compensation pin (ITH / COMP / VC) carries a series R-C to
ground (Rc, Cc) and a small C to ground (Chf); its feedback pin (FB / VFB) the divider: the parts to the output (the
inductor's far side and nets joined to it through shunts) on top, to ground and to any other net (an output-adjust
input, an AC ground) below; its current-sense pins (SENSE± through their filter resistors) reach the sense resistor(s)
(in parallel), or the inductor itself (DCR sensing). The output capacitors are the capacitors from the output nets to
ground (C at bias where given, ESR), the load V_out / I_out.

The model [Ridley 1991] [Erickson 2001]: with Y_o(s) = Σ sC/(1 + sC·ESR) + 1/R_load,
- peak current mode: G_vc(s) = Z'(s)·F_h(s) / R_i with Z' = 1/(Y_o + T_s·(m_c·D' − ½)/L), R_i = R_sense / k (k the
  current-sense gain dV_sense/dV_COMP, from Current Sense Gain or Vsense Max / ITH Range), m_c = 1 + S_e/S_n (S_n the
  sensed up-slope R_sense·(V_in − V_out)/L, S_e the slope compensation, else half the down-slope), and the sampling
  double pole F_h = 1/(1 + s/(ω_n·Q_p) + s²/ω_n²) at ω_n = π·f_sw, Q_p = 1/(π·(m_c·D' − ½)) [Ridley 1991];
- voltage mode: G_vd(s) = (V_in / V_ramp) / (1 + (sL + R_L)·Y_o) [Erickson 2001];
- the compensator: a gm amplifier gm·Z_comp (Z_comp = 1/(1/(Rc + 1/sCc) + sChf + 1/Ro)) times the divider
  H = Y_top/(Y_top + Y_bottom); an op-amp error amplifier (EA Gain, no EA gm): Z_f / Z_in with Z_f the network from
  COMP to FB and Z_in the divider's top (ideal amplifier).
T(s) = G_c·H·G_plant; the crossover f_c where |T| = 1, the phase margin 180° + ∠T(f_c), the gain margin where ∠T
reaches −180°. A constant-on-time or internally compensated controller (Control Mode COT / internal) has no model
here: a note.
A boost (its output is its rail, behind the rectifier; D' = V_in / V_out, I_L = I_out / D', a = I_L / V_out,
Z_L = sL + R_L):
- peak current mode: G_vc = (D' − a·Z_L) / (Y_o + a·D' + G·(D' − a·Z_L)) / R_i · F_h with G = T_s·D'²·(m_c − ½)/L
  (the averaged peak-current law [Erickson 2001, ch. 12], with the duty cycle taken quasi-statically from the
  inductor: the same steps give Ridley's buck model above exactly); S_n = R_sense·V_in/L;
- voltage mode: G_vd = V_out·(D' − a·Z_L) / (Z_L·Y_o + D'²) / V_ramp;
both with the right-half-plane zero at D'²·R_load/L (figures["f_rhp"]) [Erickson 2001], which bounds the crossover.
Both topologies assume continuous conduction [A13] (a note when the load is below the ripple's half).
Validated in tests/test_loop.py against the closed forms (the LC pair and ESR zero, Ridley's pole and sampling pair,
the compensator's zero and pole, the asymptotic crossover gm·Rc·H / (2π·R_i·C); a boost's right-half-plane zero,
its dominant pole and its voltage-mode pair at D'/(2π·sqrt(LC)) with Q = D'·R·sqrt(C/L)).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..components.fields import CONTROL_MODE, field_text, read
from ..components.values import parse_value

SERIES_MAX_OHM = 10.0           # a shunt between output nets (they stay one node)
FILTER_MAX_OHM = 100.0          # a current-sense filter resistor between a SENSE pin and the sense resistor
MODES = ("current", "voltage", "cot", "internal")


# ------------------------------------------------------------------------------------------------ rationals in s
class Rat:
    """A rational function N(s)/D(s), coefficients highest power first."""

    def __init__(self, num, den=(1.0,)):
        """The two polynomials (leading zeros dropped)."""
        self.n = np.trim_zeros(np.atleast_1d(np.asarray(num, float)), "f") if np.any(num) else np.array([0.0])
        self.d = np.trim_zeros(np.atleast_1d(np.asarray(den, float)), "f")

    def __mul__(self, o):
        """The product (a number or a Rat)."""
        o = o if isinstance(o, Rat) else Rat([o])
        return Rat(np.polymul(self.n, o.n), np.polymul(self.d, o.d))

    __rmul__ = __mul__

    def __add__(self, o):
        """The sum (a number or a Rat)."""
        o = o if isinstance(o, Rat) else Rat([o])
        return Rat(np.polyadd(np.polymul(self.n, o.d), np.polymul(o.n, self.d)), np.polymul(self.d, o.d))

    __radd__ = __add__

    def inv(self) -> "Rat":
        """1 / this."""
        return Rat(self.d, self.n)

    def __call__(self, s):
        """The value at s (complex)."""
        return np.polyval(self.n, s) / np.polyval(self.d, s)


S = Rat([1.0, 0.0])             # s


def roots_scaled(poly: np.ndarray, w0: float) -> list:
    """The roots of a polynomial in s found in x = s/w0 (coefficients spanning decades stay well conditioned)."""
    p = np.asarray(poly, float)
    if len(p) < 2:
        return []
    k = np.arange(len(p) - 1, -1, -1)
    q = p * w0 ** k
    q = q / np.max(np.abs(q))
    return [complex(r) * w0 for r in np.roots(q)]


# ------------------------------------------------------------------------------------------------ the parts
@dataclass
class LoopParts:
    """What the loop is made of on the board, with where each value came from."""
    controller: str = ""
    channel: str = ""
    mode: str = ""
    comp_net: str = ""
    fb_net: str = ""
    rc: Optional[float] = None
    cc: Optional[float] = None
    chf: float = 0.0
    r_shunt: Optional[float] = None         # a resistor from COMP straight to ground (a gm amplifier's load)
    top: list = field(default_factory=list)          # [(ref, R or None, C or None)] FB to the output
    bottom: list = field(default_factory=list)       # [(ref, R, C, net)] FB to ground or another net
    zf: list = field(default_factory=list)           # op-amp: [(kind, R, C)] COMP to FB (series RC or single parts)
    r_sense: Optional[float] = None
    sense_parts: list = field(default_factory=list)
    params: dict = field(default_factory=dict)       # controller figures (SI) with their sources
    notes: list = field(default_factory=list)
    missing: list = field(default_factory=list)      # InvenTree fields the model needs and the part lacks


def _norm(fn: str) -> str:
    """A pin name for matching: upper case, only letters, digits, + and - ('V_{FB1}' -> 'VFB1')."""
    return re.sub(r"[^A-Z0-9+\-]", "", (fn or "").upper())


PIN = {"fb": re.compile(r"^(VFB|FB|VOSENSE|VSENSE|FBK)(\d*)$"),
       "comp": re.compile(r"^(ITH|COMP|VC|EAOUT|EAO)(\d*)$"),
       "sense+": re.compile(r"^(SENSE|ISNS|ISENSE|CS|SNS)(\d*)(\+|P)$"),
       "sense-": re.compile(r"^(SENSE|ISNS|ISENSE|CS|SNS)(\d*)(-|N)$"),
       "sw": re.compile(r"^(SW|PHASE|LX|PH)(\d*)$")}


def _pin(fp, kind: str, channel: str):
    """The controller's pad of a kind (fb, comp, sense+, sense-) for the channel ('' or a digit)."""
    for p in fp.pads:
        m = PIN[kind].match(_norm(p.pin_function))
        if m and p.net and m.group(2) in (channel, ""):
            return p
    return None


def _value(config, fp, kind: str) -> Optional[float]:
    """A part's R or C from its model (C at bias where given), else its value text."""
    m = config.model_for(fp.reference)
    if m is not None:
        if kind == "R" and m.kind == "resistor":
            return m.get("R", None) or None
        if kind == "C" and m.kind == "cap":
            return (m.params.get("C_bias") or m.get("C", 0.0)) or None
    # a part modelled 'open' (off every signal and power path: a compensation part): its value as written
    # ("2.2k", "22n", "22k0", "10uF"; a rating after it, "100n 50V", left out)
    token = (fp.value or "").split()[0] if fp.value else ""
    v = parse_value(token)
    if v is None and token:
        v = parse_value(re.sub(r"(?i)(f|h|r|ω|ohm)$", "", token))
    return v


def _kind(config, fp) -> str:
    """'R', 'C' or '' for a two-pad part by its model (an 'open' one, or none, by its reference letter)."""
    m = config.model_for(fp.reference)
    kind = {"resistor": "R", "cap": "C"}.get(m.kind, "") if m is not None else ""
    if kind or (m is not None and m.kind not in ("open",)):
        return kind
    lib = f"{fp.lib_id}".lower()
    if "resistor" in lib or "capacitor" in lib:
        return "R" if "resistor" in lib else "C"
    return {"R": "R", "C": "C"}.get(fp.reference[:1].upper(), "")       # RC1, CH1: the first letter


def _other(fp, net: str) -> Optional[str]:
    """A two-net part's net on the other side of `net`."""
    nets = {p.net for p in fp.pads if p.net}
    return next(iter(nets - {net}), None) if len(nets) == 2 and net in nets else None


def output_nets(board, config, spec) -> set:
    """The stage's output: a buck's inductor's far side (a boost's rail, behind its rectifier) and the nets joined
    to it through shunts (≤ SERIES_MAX_OHM), shorts and ferrites."""
    from .power import switch_inductor
    node, ind = switch_inductor(board, config, spec)
    if ind is None:
        return set()
    if spec.topology == "boost":                        # the inductor's far side is the boost's input
        out = {spec.rail} - {""}
    else:
        out = {next((p.net for p in ind.pads if p.net and p.net != node), "")} - {""}
    by_net = board.parts_by_net()
    todo = list(out)
    while todo:
        n = todo.pop()
        for fp in by_net.get(n, ()):
            if fp is ind or fp.dnp:
                continue
            m = config.model_for(fp.reference)
            joins = m is not None and (m.kind in ("short", "ferrite")
                                       or (m.kind == "resistor" and m.get("R", 1e9) <= SERIES_MAX_OHM))
            o = _other(fp, n)
            if joins and o and o not in out:
                out.add(o)
                todo.append(o)
    return out


def find_loop(board, config, spec) -> Optional[LoopParts]:
    """The loop's parts and the controller's figures (see the module); None without a controller found."""
    from .safety import is_ground
    by_net = board.parts_by_net()
    lp = LoopParts()
    ctrl, channel = None, ""
    if spec.switch:                                     # an integrated stage: the IC is the controller
        ctrl = board.footprint(spec.switch[0])
        sw = _pin(ctrl, "sw", "") if ctrl else None
        channel = re.sub(r"\D", "", _norm(sw.pin_function)) if sw else ""
    if ctrl is None and spec.switch_node:
        for fp in by_net.get(spec.switch_node, ()):
            if fp.dnp or not re.match(r"(U|IC)\d", fp.reference.upper()):
                continue
            sw = next((p for p in fp.pads if p.net == spec.switch_node and PIN["sw"].match(_norm(p.pin_function))),
                      None)
            if sw is not None:
                ctrl, channel = fp, PIN["sw"].match(_norm(sw.pin_function)).group(2)
                break
    if ctrl is None:
        return None
    lp.controller, lp.channel = ctrl.reference, channel
    over = dict(getattr(spec, "loop", None) or {})
    mode = str(over.get("mode") or field_text(ctrl, CONTROL_MODE) or "").strip().lower()
    lp.mode = ("current" if "current" in mode else "voltage" if "voltage" in mode else "cot" if "cot" in mode
               or "on-time" in mode or "on time" in mode else "internal" if "internal" in mode else "")
    # the controller's figures: settings overrides, else its symbol fields
    names = {"vref": "Vref", "gm": "EA gm", "ro": "EA Ro", "ea_gain": "EA Gain", "ea_gbw": "EA GBW",
             "vsense_max": "Vsense Max", "ith_range": "ITH Range", "cs_gain": "Current Sense Gain",
             "slope": "Slope Compensation", "ramp": "Ramp"}
    for key, fname in names.items():
        if over.get(key) is not None:
            lp.params[key] = (float(parse_value(str(over[key])) if not isinstance(over[key], (int, float))
                                    else over[key]), "settings (loop)")
            continue
        r = read(ctrl, fname)
        if r is not None and r.value is not None:
            lp.params[key] = (r.value, r.source)
    comp = _pin(ctrl, "comp", channel)
    fb = _pin(ctrl, "fb", channel)
    lp.comp_net, lp.fb_net = (comp.net if comp else ""), (fb.net if fb else "")
    if comp is not None:
        for fp in by_net.get(comp.net, ()):
            if fp is ctrl or fp.dnp:
                continue
            o, k = _other(fp, comp.net), _kind(config, fp)
            if o is None:
                continue
            if is_ground(o) and k == "C":
                lp.chf += _value(config, fp, "C") or 0.0
            elif is_ground(o) and k == "R":
                lp.r_shunt = _value(config, fp, "R")
            elif k == "R":                              # a series R-C: R to a node, C from it to ground (or to FB)
                cap = next((f for f in by_net.get(o, ()) if f is not fp and _kind(config, f) == "C"), None)
                if cap is not None and is_ground(_other(cap, o) or ""):
                    lp.rc, lp.cc = _value(config, fp, "R"), _value(config, cap, "C")
                elif cap is not None and _other(cap, o) == lp.fb_net:
                    lp.zf.append(("RC", _value(config, fp, "R"), _value(config, cap, "C")))
            elif k == "C" and o == lp.fb_net:
                lp.zf.append(("C", None, _value(config, fp, "C")))
    outs = output_nets(board, config, spec)
    if fb is not None:
        for fp in by_net.get(fb.net, ()):
            if fp is ctrl or fp.dnp:
                continue
            o, k = _other(fp, fb.net), _kind(config, fp)
            if o is None or k not in ("R", "C") or o == lp.comp_net:
                continue
            r = _value(config, fp, "R") if k == "R" else None
            c = _value(config, fp, "C") if k == "C" else None
            if o in outs:
                lp.top.append((fp.reference, r, c))
            else:
                lp.bottom.append((fp.reference, r, c, o))
                if not is_ground(o):
                    lp.notes.append(f"{fp.reference} from {o.rsplit('/', 1)[-1]} also feeds the feedback node: taken "
                                    "as an AC ground (its source's impedance left out)")
    # the sense resistor between the SENSE pins (through their filter resistors)
    sp, sn = _pin(ctrl, "sense+", channel), _pin(ctrl, "sense-", channel)
    if sp is not None and sn is not None:
        def reach(net):
            """The nets a SENSE pin reaches directly or through a filter resistor."""
            out = {net}
            for fp in by_net.get(net, ()):
                o = _other(fp, net)
                if o and _kind(config, fp) == "R" and (_value(config, fp, "R") or 1e9) <= FILTER_MAX_OHM:
                    out.add(o)
            return out
        a_nets, b_nets = reach(sp.net), reach(sn.net)
        g = 0.0
        for fp in board.footprints:
            nets = {p.net for p in fp.pads if p.net}
            if fp.dnp or len(nets) != 2 or not (nets & a_nets) or not (nets & b_nets) or _kind(config, fp) != "R":
                continue
            r = _value(config, fp, "R")
            if r and r < 1.0:                           # a shunt, not a filter resistor
                g += 1 / r
                lp.sense_parts.append(fp.reference)
        if g:
            lp.r_sense = 1 / g
        else:
            from .power import switch_inductor
            _, ind = switch_inductor(board, config, spec)
            if ind is not None and {p.net for p in ind.pads} & a_nets and {p.net for p in ind.pads} & b_nets:
                m = config.model_for(ind.reference)
                lp.r_sense = m.get("R", 0.0) if m else None
                lp.sense_parts = [ind.reference]
                lp.notes.append(f"current sensed across {ind.reference}'s DCR")
    if over.get("r_sense") is not None:
        lp.r_sense = float(parse_value(str(over["r_sense"])))
    return lp


# ------------------------------------------------------------------------------------------------ the loop
@dataclass
class LoopResult:
    """The loop gain and its figures: crossover (Hz), phase margin (°), gain margin (dB) and where the phase
    reaches −180° (Hz); the response for the Bode views; notes; error (no model)."""
    fc: Optional[float] = None
    pm: Optional[float] = None
    gm_db: Optional[float] = None
    f180: Optional[float] = None
    mode: str = ""
    response: object = None
    parts: Optional[LoopParts] = None
    figures: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    error: str = ""

    def summary(self) -> dict:
        """Plain data for the stage's summary."""
        p = self.parts
        return {"fc": self.fc, "pm": self.pm, "gm_db": self.gm_db, "f180": self.f180, "mode": self.mode,
                "error": self.error, "notes": list(self.notes), "figures": self.figures,
                "controller": p.controller if p else "", "comp": {"rc": p.rc, "cc": p.cc, "chf": p.chf} if p else {},
                "divider": {"top": [x[0] for x in p.top], "bottom": [x[0] for x in p.bottom]} if p else {},
                "r_sense": p.r_sense if p else None}


def _admittance(parts) -> Optional[Rat]:
    """The admittance of resistors and capacitors in parallel ([(ref, R, C, ...)]: R or C each)."""
    y = None
    for item in parts:
        r, c = item[1], item[2]
        term = Rat([1.0 / r]) if r else (Rat([c, 0.0]) if c else None)
        if term is not None:
            y = term if y is None else y + term
    return y


def output_admittance(board, config, spec, load_ohm: float) -> tuple[Rat, list]:
    """Y_o(s) = Σ sC/(1 + sC·ESR) over the output capacitors + 1/R_load, and the capacitors [(ref, C, ESR)]."""
    from .safety import is_ground
    outs = output_nets(board, config, spec)
    y = Rat([1.0 / load_ohm])
    caps = []
    for fp in board.footprints:
        nets = {p.net for p in fp.pads if p.net}
        m = config.model_for(fp.reference)
        if fp.dnp or m is None or m.kind != "cap" or len(nets) != 2 or not (nets & outs):
            continue
        if not any(is_ground(n) for n in nets - outs):
            continue
        c = m.params.get("C_bias") or m.get("C", 0.0)
        esr = m.get("ESR", 0.0)
        if c:
            y = y + Rat([c, 0.0], [c * esr, 1.0])
            caps.append((fp.reference, c, esr))
    return y, caps


def analyse_loop(board, config, spec) -> Optional[LoopResult]:
    """The stage's loop gain (see the module); None for a stage without a controller found (a bridge leg, a
    stage of discrete parts driven from elsewhere)."""
    from .power import switch_inductor
    parts = find_loop(board, config, spec)
    if parts is None:
        return None
    res = LoopResult(mode=parts.mode, parts=parts, notes=list(parts.notes))
    p = {k: v[0] for k, v in parts.params.items()}

    def fail(msg):
        """No model: the reason."""
        res.error = msg
        return res
    if parts.mode in ("cot", "internal"):
        return fail(f"{parts.controller}: {'constant on-time' if parts.mode == 'cot' else 'internally compensated'}"
                    " control: no loop model (its datasheet's stability conditions apply)")
    if not parts.mode:
        return fail(f"{parts.controller}: give its symbol a Control Mode field (peak current, voltage, COT or "
                    "internal) for the loop gain")
    boost = spec.topology == "boost"
    if spec.topology not in ("buck", "boost"):
        return fail(f"the loop gain is modelled for buck and boost stages (this one: {spec.topology})")
    if not (spec.v_in and spec.v_out and spec.i_out and spec.f_sw):
        return fail("the loop gain needs the stage's V in, V out, load current and f_sw")
    if (spec.v_out >= spec.v_in) != boost:
        return fail(f"a {spec.topology} from {spec.v_in:g} V to {spec.v_out:g} V: check the stage's voltages")
    _, ind = switch_inductor(board, config, spec)
    m = config.model_for(ind.reference) if ind is not None else None
    if m is None or not m.get("L", 0.0):
        return fail("the loop gain needs the stage's inductor and its inductance")
    L, r_l = m.get("L"), m.get("R", 0.0)
    load = spec.v_out / spec.i_out
    y_o, caps = output_admittance(board, config, spec, load)
    if not caps:
        return fail("no output capacitor found from the output to ground")
    # continuous conduction (ideal): a buck's D = V_out / V_in, a boost's D' = 1 - D = V_in / V_out
    D = 1 - spec.v_in / spec.v_out if boost else spec.v_out / spec.v_in
    dp = 1 - D
    ts = 1.0 / spec.f_sw
    i_l = spec.i_out / dp if boost else spec.i_out                  # the inductor's average current
    # the inductor's slopes (A/s): up while the switch is on, down while off
    up = (spec.v_in if boost else spec.v_in - spec.v_out) / L
    down = (spec.v_out - spec.v_in if boost else spec.v_out) / L
    figs = {"L": L, "R_L": r_l, "C_out": sum(c for _, c, _ in caps), "caps": [x[0] for x in caps], "R_load": load,
            "D": D}
    if i_l < up * D * ts / 2:                                       # the ripple's valley below zero
        res.notes.append(f"discontinuous conduction at {spec.i_out:g} A (the inductor's ripple "
                         f"{up * D * ts:.3g} A peak to peak): the model, for continuous conduction, is pessimistic "
                         "at this load")
    if boost:
        # the right-half-plane zero (the same in voltage and current mode): D'²·R_load/L with an ideal inductor
        figs["f_rhp"] = dp * dp * load / L / (2 * math.pi)
    if parts.mode == "current":
        k = p.get("cs_gain") or (p["vsense_max"] / p["ith_range"] if p.get("vsense_max") and p.get("ith_range")
                                 else None)
        missing = [] if k else [f"{parts.controller}: Current Sense Gain (or Vsense Max and ITH Range)"]
        if parts.r_sense is None:
            missing.append("the sense resistor between the SENSE pins (or the settings' loop: r_sense)")
        if missing:
            return fail("the loop gain needs " + "; ".join(missing))
        ri = parts.r_sense / k
        sn = parts.r_sense * up                                     # the sensed up-slope, V/s
        sf = parts.r_sense * down                                   # the sensed down-slope
        if p.get("slope") is not None:
            se = p["slope"]
        else:
            se = 0.5 * sf
            res.notes.append(f"{parts.controller}: no Slope Compensation field: half the inductor's down-slope "
                             f"({se * 1e-6:.3g} V/µs at the sense input) assumed")
        mc = 1 + se / sn
        q = mc * dp - 0.5
        figs.update(R_i=ri, mc=mc, R_sense=parts.r_sense, k=k)
        if q <= 0:
            return fail(f"subharmonic oscillation: m_c·D' = {mc * dp:.2f} ≤ 0.5 (more slope compensation needed)")
        wn = math.pi * spec.f_sw
        qp = 1 / (math.pi * q)
        figs.update(Q_p=qp)
        fh = Rat([1.0], [1 / wn ** 2, 1 / (wn * qp), 1.0])         # the sampling double pole at f_sw / 2
        if boost:
            # the averaged peak-current law (Erickson ch. 12) with the duty cycle taken quasi-statically from the
            # inductor (d = D'·v / V_out), as Ridley's buck model follows from it: i_L = v_c / R_i − G·v with
            # G = T_s·D'²·(m_c − ½)/L; the output node Y_o·v = D'·i_L − I_L·d with the inductor's
            # d = (D'·v + Z_L·i_L)/V_out gives G_vc = (D' − a·Z_L) / (Y_o + a·D' + G·(D' − a·Z_L)) / R_i,
            # a = I_L / V_out: the right-half-plane zero at D'/(a·L) = D'²·R/L, the pole near (2/R + G·D')/C
            a = i_l / spec.v_out
            g = ts * dp * dp * (mc - 0.5) / L
            fwd = Rat([dp]) + (-a) * Rat([L, r_l])
            plant = Rat([1.0 / ri]) * fwd * (y_o + a * dp + g * fwd).inv() * fh
        else:
            plant = (Rat([1.0 / ri]) * (y_o + ts * q / L).inv()) * fh
    else:
        ramp = p.get("ramp")
        if not ramp:
            return fail(f"{parts.controller}: give its symbol a Ramp field (the PWM ramp, V) for a voltage-mode loop")
        if boost:
            # the averaged boost (Erickson ch. 7): Z_L·i_L = −D'·v + V_out·d, Y_o·v = D'·i_L − I_L·d, so
            # G_vd = V_out·(D' − a·Z_L) / (Z_L·Y_o + D'²): the LC pair at D'/sqrt(LC), the zero at D'²·R/L
            a = i_l / spec.v_out
            zl = Rat([L, r_l])
            plant = Rat([spec.v_out / ramp]) * (Rat([dp]) + (-a) * zl) * (zl * y_o + dp * dp).inv()
        else:
            plant = Rat([spec.v_in / ramp]) * (Rat([1.0]) + Rat([L, r_l]) * y_o).inv()
    y_top = _admittance(parts.top)
    if y_top is None:
        return fail(f"no feedback divider found from the output to {parts.controller}'s FB pin")
    if p.get("gm") is not None:                         # a transconductance error amplifier
        if parts.rc is None or parts.cc is None:
            res.notes.append("no series R-C found on the compensation pin: Z(COMP) is its capacitance alone")
        y_c = Rat([parts.chf, 0.0]) if parts.chf else None
        if parts.rc and parts.cc:
            term = Rat([parts.cc, 0.0], [parts.rc * parts.cc, 1.0])
            y_c = term if y_c is None else y_c + term
        ro = p.get("ro") or parts.r_shunt
        if ro:
            y_c = Rat([1.0 / ro]) if y_c is None else y_c + Rat([1.0 / ro])
        if y_c is None:
            return fail(f"no compensation network found on {parts.controller}'s COMP pin")
        y_bot = _admittance(parts.bottom)
        h = y_top * (y_top + (y_bot if y_bot is not None else Rat([0.0]))).inv()
        gc = Rat([p["gm"]]) * y_c.inv() * h
        figs.update(gm=p["gm"], Rc=parts.rc, Cc=parts.cc, Chf=parts.chf)
    elif p.get("ea_gain") is not None or parts.zf:      # an op-amp error amplifier: Z_f / Z_in (ideal)
        y_f = None
        for kind, r, c in parts.zf:
            term = Rat([c, 0.0], [r * c, 1.0]) if kind == "RC" else Rat([c, 0.0])
            y_f = term if y_f is None else y_f + term
        if y_f is None:
            return fail(f"no network found from {parts.controller}'s COMP to its FB pin")
        gc = y_top * y_f.inv()
        res.notes.append("an ideal op-amp error amplifier (its finite gain and bandwidth left out)")
    else:
        return fail(f"{parts.controller}: give its symbol an EA gm field (a gm error amplifier) or EA Gain (an "
                    "op-amp)")
    t = gc * plant
    res.figures = figs
    # the figures from a dense sweep up to five times f_sw
    f = np.geomspace(max(spec.f_sw / 1e5, 1.0), 5 * spec.f_sw, 2000)
    h = t(2j * np.pi * f)
    mag = np.abs(h)
    ph = np.degrees(np.unwrap(np.angle(h)))
    down = np.flatnonzero((mag[:-1] >= 1) & (mag[1:] < 1))
    if len(down):
        k = down[0]
        x = np.interp(0.0, [-math.log(mag[k]), -math.log(mag[k + 1])], [math.log(f[k]), math.log(f[k + 1])])
        res.fc = math.exp(x)
        res.pm = 180.0 + float(np.interp(x, np.log(f), ph))
        after = np.flatnonzero((np.log(f) > x)[:-1] & (ph[:-1] > -180) & (ph[1:] <= -180))
        if len(after):
            j = after[0]
            xf = np.interp(-180.0, [ph[j + 1], ph[j]], [math.log(f[j + 1]), math.log(f[j])])
            res.f180 = math.exp(xf)
            res.gm_db = -20 * math.log10(float(np.exp(np.interp(xf, np.log(f), np.log(mag)))))
    else:
        res.notes.append("the loop gain never crosses 1 up to 5 × f_sw")
    from .transfer import Response, cancel_pairs
    w0 = 2 * math.pi * spec.f_sw
    # pole-zero pairs that cancel (several output capacitors, each with its ESR) left out of the lists
    poles, zeros = cancel_pairs(roots_scaled(t.d, w0), roots_scaled(t.n, w0))
    res.response = Response(f"{spec.name}: loop gain T", spec.name, "loop gain", f, h, poles, zeros, exact=True,
                            notes=list(res.notes))
    return res
