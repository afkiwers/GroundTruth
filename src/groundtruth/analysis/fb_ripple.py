"""The switching ripple at a buck controller's FB pin: what its feedback divider passes of the output's ripple, and
what a ripple-injection network adds from the switch node.

A constant-on-time controller needs a minimum ripple at FB to switch cleanly (its datasheet says how much and how to
get it: the output capacitor's ESR, or an RC network from the switch node); too much ripple, or ripple on the wrong
edge, makes any controller jitter. The ripple comes from the inductor's ripple current flowing into the output
capacitors (their C at DC bias, ESR and ESL: a low-ESR ceramic gives almost none of the in-phase part a COT
controller needs) and through the copper between the inductor, the capacitors and the divider's tap.

Model, per harmonic n·f_sw of the switching frequency (superposition, then one period of waveform by Fourier
synthesis, `transfer.periodic_wave`):
- the inductor's ripple current, a triangle of ΔI p-p rising for D·T (continuous conduction), its corners rounded
  over the switching edge, as an ideal current source [A112]: c_n = −ΔI·(1 − e^{−j2πnD}) / (4π²n²·D(1−D)) [Erickson
  2001] times sinc(n·f_sw·t_r);
- the switch node's voltage, a trapezoid from the rectifier's level (−V_f with a catch diode, 0 synchronous) to V_in,
  for a ripple-injection network only;
- the copper: a model of the output net(s) and the ground around the stage (simulate.BoardModel, the output
  capacitors as its branches with their ESR, ESL and mounting inductance), the ripple current fed in at the
  inductor's output pad and taken out at the rectifier's ground pads: every pad of the feedback network on the
  output or the ground gets its potential per ampere against the controller's ground pin. Without the copper
  (no model: the parts alone, with a note) the output pads are the capacitors' parallel impedance and the ground
  pads 0 V;
- the feedback network: every two-pin resistor, capacitor or inductor reached from the FB net (the divider, a
  feed-forward capacitor, an injection RC; through at most INJECTION_DEPTH nets), solved as a circuit with each
  source pad driven in turn (emi/mna.py); its own current is not taken from the output [A113].
The result: V_FB(t) over a period (AC part; its DC level is the controller's Vref when known), the output's ripple
at the divider's tap and the inductor's current, their peak-to-peak values, the share each path brings, and the
issue `fb_ripple` against the controller's FB Ripple Min / Max fields (or the stage's `loop:` overrides
fb_ripple_min / fb_ripple_max). Source keys in square brackets: docs/REFERENCES.md.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np

F_TOP = 200e6                   # highest harmonic frequency taken (and at most HARMONICS_MAX harmonics)
HARMONICS_MAX = 600
EXACT_SOLVES = 10               # the first harmonics get a copper solve each; above them log-spaced solves
LOG_SOLVES = 14
WAVE_POINTS = 2048              # samples per period for the peak-to-peak; the result keeps WAVE_KEPT of them
WAVE_KEPT = 512
INJECTION_DEPTH = 2             # nets walked from FB through two-pin parts
REGION_MARGIN_MM = 4.0          # the copper model's margin around the parts involved
GND_FUNCTIONS = ("AGND", "SGND", "GND", "RTN", "VSS", "PGND")    # a controller's ground pin, preferred first


@dataclass
class FbRippleResult:
    """A stage's ripple at its controller's FB pin (see the module)."""
    net: str                            # the stage's name (views are listed by it)
    kind: str = "fb_ripple"
    controller: str = ""
    fb_pin: str = ""
    mode: str = ""
    f_sw: float = 0.0
    duty: float = 0.0
    delta_i: float = 0.0                # the inductor's ripple, A p-p
    t: list = field(default_factory=list)          # s, one period (WAVE_KEPT points)
    v_fb: list = field(default_factory=list)       # V, the ripple at FB (AC)
    v_tap: list = field(default_factory=list)      # V, the output's ripple at the divider's tap (AC)
    i_l: list = field(default_factory=list)        # A, the inductor's ripple current (AC)
    fb_pp: float = 0.0
    tap_pp: float = 0.0
    fb_dc: Optional[float] = None       # V, the FB pin's DC level (Vref) when known
    paths: dict = field(default_factory=dict)      # {"output": V p-p through the divider, "injection": V p-p}
    network: list = field(default_factory=list)    # [{ref, kind, from, to}] the feedback network as found
    caps: list = field(default_factory=list)       # [{ref, C, ESR, ESL}] the output capacitors
    copper: bool = False                # the tap's potentials from the copper model (else the parts alone)
    limits: dict = field(default_factory=dict)     # {"min": (V, source), "max": (V, source)}
    status: str = ""                    # "ok" | "low" | "high" | "" (no limit)
    notes: list = field(default_factory=list)
    error: str = ""

    def label(self) -> str:
        """Its name in the results' list of views."""
        return f"{self.net}  FB ripple"

    def summary(self) -> dict:
        """As plain data for the JSON report (the waveforms as lists)."""
        return asdict(self)


# --------------------------------------------------------------------------- the sources' harmonics
def ripple_current(n: np.ndarray, delta_i: float, duty: float, f_sw: float, edge: float) -> np.ndarray:
    """Complex Fourier coefficients c_n (x(t) = Σ c_n e^{jnωt}, n ≠ 0) of the inductor's ripple current: a triangle
    of delta_i p-p rising from −ΔI/2 at t = 0 to +ΔI/2 at D·T (a buck's high side on) [Erickson 2001]: the
    derivative is a square wave of ΔI/(D·T) and −ΔI/((1−D)·T), so c_n = −ΔI·(1 − e^{−j2πnD}) / (4π²n²·D(1−D)); its
    corners rounded over the switching edge `edge` (a box of that width: sinc(n·f_sw·edge)) [A112]."""
    n = np.asarray(n, float)
    d = min(max(duty, 1e-4), 1.0 - 1e-4)
    c = -delta_i * (1.0 - np.exp(-2j * math.pi * n * d)) / (4.0 * math.pi ** 2 * n * n * d * (1.0 - d))
    return c * np.sinc(n * f_sw * edge)


def switch_voltage(n: np.ndarray, v_high: float, v_low: float, duty: float, f_sw: float, edge: float) -> np.ndarray:
    """Complex Fourier coefficients of the switch node's voltage: v_low, rising over `edge` from t = 0 to v_high,
    falling at D·T over `edge` back to v_low (a trapezoid on the same time origin as ripple_current). From its
    derivative, two boxes of ±A/edge: d_n = (A/T)·sinc(n·f·edge)·(e^{−jnω·edge/2} − e^{−jnω(D·T + edge/2)}),
    c_n = d_n / (jnω)."""
    n = np.asarray(n, float)
    w = 2 * math.pi * n * f_sw
    t_sw = 1.0 / f_sw
    a = v_high - v_low
    d_n = (a / t_sw) * np.sinc(n * f_sw * edge) * (np.exp(-1j * w * edge / 2)
                                                  - np.exp(-1j * w * (duty * t_sw + edge / 2)))
    return d_n / (1j * w)


# --------------------------------------------------------------------------- the feedback network
@dataclass
class _Element:
    """One part of the feedback network: its model and the circuit nodes of its two pads."""
    ref: str
    kind: str
    values: dict
    a: str
    b: str


def _model_of(board, config, fp):
    """A part's model: the settings', else (none, or "open": not modelled for the copper) the one its footprint's
    value suggests, as the control loop reads its divider (loop._kind)."""
    from ..components.defaults import suggest_model
    from ..components.models import ComponentModel
    from ..components.values import parse_value
    from .loop import _kind
    m = config.model_for(fp.reference)
    if m is not None and m.kind != "open":
        return m
    m = suggest_model(fp)
    if m is not None and m.kind != "open":
        return m
    # a high-value resistor is suggested "open" for the copper models (it carries nothing there): here its value
    # counts, read as the control loop reads a divider's
    v = parse_value(fp.value)
    kind = {"R": "resistor", "C": "cap"}.get(_kind(config, fp), "")
    if not v or not kind:
        return None
    return ComponentModel(kind, {"R": v} if kind == "resistor" else {"C": v})


def feedback_network(board, config, spec, fb_net: str, controller: str, out_nets: set, sw_net: str, ground: str
                     ) -> tuple[list, dict, list]:
    """(elements, source pads {node: (pad, kind "tap" | "gnd" | "sw")}, notes): every two-pin resistor, capacitor,
    inductor, ferrite or short reached from the FB net through at most INJECTION_DEPTH nets, not through the
    controller. A pad on an output net is a source node of its own ("tap" + the pad: its potential comes from the
    copper), one on the ground too ("gnd"), one on the switch node "SW"; other nets met are the network's own
    nodes, beyond the depth they are taken as AC ground (a note)."""
    from .safety import is_ground
    by_net = board.parts_by_net()
    elements, sources, notes = [], {}, []
    seen_parts: set = set()
    depth = {fb_net: 0}
    todo = [fb_net]

    def node_of(pad, net: str) -> str:
        """The circuit node of a part's pad on `net`."""
        if net == fb_net:
            return "FB"
        if net in out_nets:
            name = f"tap:{pad.footprint}.{pad.number}"
            sources[name] = (pad, "tap")
            return name
        if net == ground or is_ground(net):
            name = f"gnd:{pad.footprint}.{pad.number}"
            sources[name] = (pad, "gnd")
            return name
        if net == sw_net:
            sources["SW"] = (pad, "sw")
            return "SW"
        if depth.get(net, INJECTION_DEPTH + 1) <= INJECTION_DEPTH:
            return f"n:{net}"
        return "0"
    while todo:
        net = todo.pop(0)
        for fp in by_net.get(net, ()):
            if fp.reference in seen_parts or fp.reference == controller or fp.dnp:
                continue
            nets = [p.net for p in fp.pads if p.net]
            if len(set(nets)) != 2:
                continue
            m = _model_of(board, config, fp)
            if m is None or m.kind not in ("resistor", "cap", "inductor", "ferrite", "short"):
                continue
            if m.kind == "inductor" and sw_net in nets:
                continue                                # the power inductor itself
            seen_parts.add(fp.reference)
            pa = next(p for p in fp.pads if p.net == net)
            pb = next(p for p in fp.pads if p.net and p.net != net)
            other = pb.net
            if (other not in out_nets and other != sw_net and other != ground and not is_ground(other)
                    and other not in depth):
                depth[other] = depth[net] + 1
                if depth[other] <= INJECTION_DEPTH:
                    todo.append(other)
                else:
                    notes.append(f"{other} (beyond {fp.reference}) taken as AC ground")
            values = dict(m.params)
            if m.kind == "cap":
                values["C"] = m.capacitance()           # at its DC voltage in a run (analysis/bias.py)
                values.pop("C_bias", None)
            elements.append(_Element(fp.reference, m.kind, values, node_of(pa, net), node_of(pb, other)))
    return elements, sources, notes


def network_transfers(elements: list, sources: dict, f: np.ndarray) -> dict:
    """{source node: H(f)}: V_FB per volt at that source node with every other source node at 0 V (superposition:
    the circuit takes one source at a time, emi/mna.py)."""
    from ..emi.mna import Circuit
    out = {}
    for s in sources:
        ckt = Circuit()
        for e in elements:
            a = "S" if e.a == s else ("0" if e.a in sources else e.a)
            b = "S" if e.b == s else ("0" if e.b in sources else e.b)
            if a != b:
                ckt.part(a, b, e.kind, e.values, e.ref)
        if "S" not in ckt.nodes or "FB" not in ckt.nodes:
            out[s] = np.zeros(len(f), complex)
            continue
        for node in list(ckt.nodes):
            if node not in ("0", "S"):
                ckt.r(node, "0", 1e12)                  # no floating node (a capacitor-only branch)
        ckt.vsource("S", "0")
        out[s] = ckt.transfer(f, "FB")
    return out


# --------------------------------------------------------------------------- the copper
def _controller_ground(fp, ground: str):
    """The controller's ground pad, its signal ground first (AGND / SGND / GND / RTN, then PGND)."""
    pads = [p for p in fp.pads if p.net == ground]
    for name in GND_FUNCTIONS:
        hit = [p for p in pads if (p.pin_function or "").upper().startswith(name)]
        if hit:
            return hit[0]
    return pads[0] if pads else None


def tap_impedances(model, spec, inductor, sw_net: str, ctrl_gnd, sources: dict, out_nets: set, ground: str,
                   freqs: np.ndarray, caps: list = ()) -> tuple[dict, list]:
    """({source node: Z(f), or None for a pad the copper does not join}, notes): each tap / ground source pad's
    potential against the controller's ground pad on the copper model of the output net(s) and the ground around
    the stage. The current enters at the inductor's output pad and leaves at the rectifier's ground pads (or,
    without them, the controller's ground pins). A pad on copper the model does not join to the current's
    (pdn.copper_pieces: an unrouted pad, a break) reads nothing there: None, the caller takes the parts' value;
    a controller ground cut off so is replaced by the rectifier's ground. Solved at the first EXACT_SOLVES
    harmonics and LOG_SOLVES log-spaced frequencies to the top, interpolated on log axes (magnitude) and linearly
    (unwrapped phase) between. Raises SolverError."""
    from ..solver.peec import RouteExcitation, SolverError
    from .power import _area_shares, _region_build, _side
    from .victims import ground_node, pad_node
    board = model.board
    out_pads = [p for p in inductor.pads if p.net in out_nets]
    rect_refs = list(spec.low_side or [])
    rect_gnd = [p for r in rect_refs if board.footprint(r) is not None
                for p in board.footprint(r).pads if p.net == ground]
    if not rect_gnd:
        ctrl = board.footprint(ctrl_gnd.footprint)
        rect_gnd = [p for p in ctrl.pads if p.net == ground]
    if not out_pads or not rect_gnd:
        raise SolverError("the inductor's output pad or the rectifier's ground pads not found")
    taps = {s: v[0] for s, v in sources.items() if v[1] in ("tap", "gnd")}
    # the region holds every pad involved: the inductor's, the rectifier's, the network's, the controller's ground
    # and the output capacitors' (the current's way to ground: without them it has none in the model)
    cap_pads = [p for c in caps if board.footprint(c["ref"]) is not None for p in board.footprint(c["ref"]).pads]
    involved = out_pads + rect_gnd + list(taps.values()) + [ctrl_gnd] + cap_pads
    pts = [p.position for p in involved]
    layers = {l for p in involved for l in p.layers}
    side = _side(inductor)
    st, _ = _region_build(model, pts, layers, set(out_nets) | {ground}, side, REGION_MARGIN_MM)
    inj = _area_shares(st, out_pads, side, 1.0) + _area_shares(st, rect_gnd, side, -1.0)
    from .pdn import copper_pieces
    plane = st.plane
    exc = RouteExcitation(plane, [], injections=inj)
    piece, _ = copper_pieces(st, plane)

    def joined(nd, sign: int) -> bool:
        """The node is on a piece of copper the current's injections of this sign are on."""
        return nd is not None and int(piece[plane.nid[nd]]) in {int(piece[plane.nid[n]]) for n, a in inj
                                                                 if (a > 0) == (sign > 0)}
    notes = []
    ref = ground_node(st, board, ctrl_gnd, ground)
    if not joined(ref, -1):
        ref = next(n for n, a in inj if a < 0)
        notes.append(f"the controller's ground pin {ctrl_gnd.footprint}.{ctrl_gnd.number} is not joined to the "
                     "rectifier's ground by copper in the model: the ripple is read against the rectifier's ground")
    nodes = {}
    for s, pad in taps.items():
        tap = sources[s][1] == "tap"
        nd = pad_node(st, pad, pad.net) if tap else ground_node(st, board, pad, ground)
        if not joined(nd, 1 if tap else -1):
            notes.append(f"{pad.footprint}.{pad.number} ({pad.net}) is not joined to the "
                         f"{'output' if tap else 'ground'} copper in the model (unrouted, or a break): "
                         f"{'the capacitors without copper' if tap else '0 V'} taken there")
            nd = None
        nodes[s] = nd
    f_sw = float(freqs[0])
    exact = freqs[:EXACT_SOLVES]
    tail = np.geomspace(exact[-1] * 1.5, freqs[-1], LOG_SOLVES) if freqs[-1] > exact[-1] * 1.5 else np.zeros(0)
    solve_f = np.unique(np.concatenate([exact, tail]))
    z = {s: [] for s in taps}
    for f in solve_f:
        sol = exc.solve(float(f))
        for s, nd in nodes.items():
            z[s].append(complex(sol.V[nd] - sol.V[ref]) if nd is not None else 0j)
    out = {s: None for s, nd in nodes.items() if nd is None}
    for s, vals in z.items():
        if nodes[s] is None:
            continue
        v = np.array(vals, complex)
        mag = np.interp(np.log(freqs), np.log(solve_f), np.log(np.maximum(np.abs(v), 1e-30)))
        ph = np.interp(np.log(freqs), np.log(solve_f), np.unwrap(np.angle(v)))
        out[s] = np.exp(mag) * np.exp(1j * ph)
    notes.append(f"the output's copper modelled around the stage: {st.nodes} nodes at {st.d:.2f} mm, "
                 f"{len(solve_f)} solves from {f_sw / 1e3:.0f} kHz to {solve_f[-1] / 1e6:.3g} MHz")
    return out, notes


def lumped_impedance(caps: list, freqs: np.ndarray, r_load: Optional[float]) -> np.ndarray:
    """The output's impedance without copper: the capacitors (ESR + jω(ESL + MOUNT_L) + 1/jωC each) and the load
    in parallel."""
    from .simulate import MOUNT_L
    w = 2j * math.pi * freqs
    y = np.zeros(len(freqs), complex) + (1.0 / r_load if r_load else 0.0)
    for c in caps:
        y = y + 1.0 / (c["ESR"] + w * (c["ESL"] + MOUNT_L) + 1.0 / (w * c["C"]))
    return 1.0 / np.where(np.abs(y) > 0, y, 1e-30)


# --------------------------------------------------------------------------- the stage
def analyse(model, spec, res, use_copper: bool = True) -> Optional[FbRippleResult]:
    """The stage's ripple at its controller's FB pin (see the module), None for a stage without a controller with an
    FB pin found (or not a buck). Raises nothing: what goes wrong is the result's error."""
    from ..solver.peec import SolverError
    from .loop import find_loop, output_nets
    from .power import switch_inductor
    from .thermal import DIODE_VF, _is_diode, ripple_pp
    from .transfer import periodic_wave
    board, config = model.board, model.config
    if spec.topology != "buck" or spec.bridge:
        return None
    lp = find_loop(board, config, spec)
    if lp is None or not lp.fb_net:
        return None
    out = FbRippleResult(spec.name, controller=lp.controller, mode=lp.mode)
    ctrl = board.footprint(lp.controller)
    fb_pad = next((p for p in ctrl.pads if p.net == lp.fb_net), None)
    out.fb_pin = f"{lp.controller}.{fb_pad.number}" if fb_pad else lp.controller
    params = {k: v for k, v in lp.params.items()}
    if "vref" in params:
        out.fb_dc = params["vref"][0]
    for key, name in (("fb_ripple_min", "min"), ("fb_ripple_max", "max")):
        if key in params:
            out.limits[name] = params[key]
    v_in = spec.v_in or res.v_in
    duty = res.duty
    if not v_in or not spec.f_sw or duty is None:
        out.error = "no input voltage, switching frequency or duty: no ripple"
        return out
    out.f_sw, out.duty = spec.f_sw, duty
    sw_net, ind = switch_inductor(board, config, spec)
    if ind is None:
        out.error = "no inductor found on the switch node"
        return out
    di = ripple_pp(board, config, spec, duty, v_in)
    if not di:
        out.error = "the inductor's L is not known (Parts tab): no ripple current"
        return out
    out.delta_i = di
    if spec.i_out and di > 2 * spec.i_out:
        out.notes.append(f"at {spec.i_out:g} A the ripple's {di:.3g} A p-p reaches below zero: the stage runs in "
                         "discontinuous conduction (or pulse skipping) unless it is forced continuous: the "
                         "waveform is the continuous one [A112]")
    ground = spec.ground or "GND"
    outs = output_nets(board, config, spec)
    elements, sources, notes = feedback_network(board, config, spec, lp.fb_net, lp.controller, outs, sw_net, ground)
    out.notes += notes
    out.network = [{"ref": e.ref, "kind": e.kind, "from": e.a, "to": e.b} for e in elements]
    if not any(k == "tap" for _, k in sources.values()) and "SW" not in sources:
        out.error = f"no part from FB ({lp.fb_net}) to the output or the switch node found: no feedback network"
        return out
    # the output capacitors (as the control loop finds them), with their ESL
    from .loop import output_admittance
    _, caps = output_admittance(board, config, spec, 1e9)
    out.caps = [{"ref": r, "C": c, "ESR": esr, "ESL": (config.model_for(r).get("ESL", 0.0)
                                                       if config.model_for(r) else 0.0)} for r, c, esr in caps]
    # the harmonics: up to F_TOP, at most HARMONICS_MAX
    n_max = int(max(1, min(HARMONICS_MAX, F_TOP // spec.f_sw)))
    n = np.arange(1, n_max + 1, dtype=float)
    freqs = n * spec.f_sw
    edge = spec.rise_time or 0.0
    i_l = ripple_current(n, di, duty, spec.f_sw, edge)
    rect = list(spec.low_side or [])
    v_low = -(spec.diode_vf if spec.diode_vf is not None else DIODE_VF) if rect and all(
        _is_diode(r) for r in rect) else 0.0
    v_sw = switch_voltage(n, v_in, v_low, duty, spec.f_sw, edge)
    # each source pad's voltage per harmonic
    z_src: dict = {}
    ctrl_gnd = _controller_ground(ctrl, ground)
    tap_like = {s for s, v in sources.items() if v[1] in ("tap", "gnd")}
    if use_copper and tap_like and ctrl_gnd is not None:
        try:
            z_src, cn = tap_impedances(model, spec, ind, sw_net, ctrl_gnd, sources, outs, ground, freqs, out.caps)
            out.notes += cn
            out.copper = any(v is not None for v in z_src.values())
        except SolverError as e:
            out.notes.append(f"the copper model could not be solved ({e}): the parts alone")
    if tap_like and any(z_src.get(s) is None for s in tap_like):
        r_load = (spec.v_out / spec.i_out) if spec.v_out and spec.i_out else None
        z_out = lumped_impedance(out.caps, freqs, r_load)
        for s in tap_like:
            if z_src.get(s) is None:
                z_src[s] = z_out if sources[s][1] == "tap" else np.zeros(len(freqs), complex)
        if not out.caps:
            out.notes.append("no output capacitor with a model found")
    h = network_transfers(elements, sources, freqs)
    v_fb_out = sum((h[s] * z_src[s] * i_l for s in tap_like), np.zeros(len(freqs), complex))
    v_fb_inj = h["SW"] * v_sw if "SW" in sources else np.zeros(len(freqs), complex)
    # the output's ripple where the divider taps it (the first tap pad of the network: its top resistor's)
    tap0 = next((s for s in sources if sources[s][1] == "tap"), None)
    v_tap = z_src[tap0] * i_l if tap0 is not None else np.zeros(len(freqs), complex)
    waves = {k: periodic_wave(c, WAVE_POINTS) for k, c in
             (("fb", v_fb_out + v_fb_inj), ("tap", v_tap), ("il", i_l), ("out", v_fb_out), ("inj", v_fb_inj))}
    pp = {k: float(np.ptp(w)) for k, w in waves.items()}
    out.fb_pp, out.tap_pp = pp["fb"], pp["tap"]
    out.paths = {"output": pp["out"], "injection": pp["inj"]}
    step = WAVE_POINTS // WAVE_KEPT
    out.t = [float(x) for x in (np.arange(WAVE_POINTS) / WAVE_POINTS / spec.f_sw)[::step]]
    out.v_fb, out.v_tap, out.i_l = ([float(x) for x in waves[k][::step]] for k in ("fb", "tap", "il"))
    # the check against the controller's limits
    lo, hi = out.limits.get("min"), out.limits.get("max")
    if lo is not None and out.fb_pp < lo[0]:
        out.status = "low"
    elif hi is not None and out.fb_pp > hi[0]:
        out.status = "high"
    elif lo is not None or hi is not None:
        out.status = "ok"
    elif lp.mode == "cot":
        out.notes.append("a constant-on-time controller needs a minimum ripple at FB: give the controller its "
                         "datasheet's figure as an FB Ripple Min field to have it checked")
    return out


def ripple_text(r: FbRippleResult) -> str:
    """One line for the stage's notes and the text report."""
    if r.error:
        return f"FB ripple: {r.error}"
    lim = ""
    if r.limits:
        lim = "; limits " + ", ".join(f"{k} {v[0] * 1e3:.3g} mV" for k, v in sorted(r.limits.items()))
    inj = f", {r.paths['injection'] * 1e3:.3g} mV of it from the injection" if r.paths.get("injection") else ""
    return (f"FB ripple at {r.fb_pin}: {r.fb_pp * 1e3:.3g} mV p-p ({r.tap_pp * 1e3:.3g} mV at the divider's tap, "
            f"ΔI {r.delta_i:.3g} A p-p{inj}; {'copper and parts' if r.copper else 'parts alone'}){lim}")
