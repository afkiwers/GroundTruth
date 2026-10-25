"""The circuit a signal path runs through, read from the board: every op-amp and every part on the path's nets and
on the nets beyond them, as one linear circuit for emi/mna.py (docs/ANALOG_AC.md). The user: "I want a generic
model that can calculate any circuit made from an OPA".

- **The network** (`analog_network`, [A53]): from the path's nets, every two-pin part with a model (resistor, capacitor,
  inductor, ferrite, 0R) and every op-amp channel with a pin on a net of it, and the nets on their other pins, until
  AC ground: the reference nets and the supply rails (`ac_ground_nets`). At most MAX_NETS nets (a note when it
  stops there). An IC that is not an op-amp is open on the network (its pins draw nothing), except the path's
  driver (the source) and receivers (the load).
- **The copper**: the network's nets as route.copper_graph has them; pads, vias' joins, through-hole pads and pours
  join their nodes (ideal: a pour is a plane), each track is its own transmission line (emi/path.PathLine with the
  cross-section's Z0 and εeff and its copper and dielectric loss, as the path's stretches have; `lumped=True`: its
  π section R + jωL with C/2 at each end, for the exact poles and zeros), each via between layers a series R + L
  (peec.via_impedance) [A3].
- **Op-amps** (`opamp_channels`): per channel its IN+, IN− and OUT pads, from the schematic pin names (+IN, IN+,
  INP, +INA, IN1−, OUTA, VOUT …), else the GT labels amp_in / amp_inv / amp_out; the macromodel of mna.Circuit.opamp
  with the part's values (gbw, aol, fp2 or pm, z_out as R_O, r_in, c_in) [Franco 2015]. A part with a `gain`
  value is the gain block it always was (a VCVS of its gain and bandwidth behind its output impedance, the circuit
  around it not consulted: an instrumentation-amplifier IC, a PGA), as is an op-amp whose IN− can't be found
  (a note says which).
- **Source and load**: the path's driver as a 1 V source behind its Thevenin R + jωL (driver_ohms, driver_nh) with
  its output capacitance; each receiver's input capacitance and the load resistance to ground."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from ..config.settings import Config, NetSpec
from ..core.board import Board, Footprint, Pad
from ..emi.mna import Circuit, OpAmpParams, line_y

MAX_NETS = 60                  # nets in one network at most (a 0R into a big net would otherwise take the board)
PASSIVE = ("resistor", "cap", "inductor", "ferrite", "short")
AMP_R_O = 100.0                # Ω, an op-amp's open-loop output resistance when its z_out is not given
AMP_GBW = 1e6                  # Hz, its gain-bandwidth when not given (a note says so)
SHORT_TRACK = 0.05             # a track below this share of a wavelength at the sweep's top: its π section is exact
# a schematic pin name's role and channel: +IN / IN+ / INP / +INA / INA+ / IN1+ / VIN+ ..., OUT / VOUT / OUTA / OUT1,
# V+ / V- / VCC / VDD / VEE / VSS (supplies)
_PIN = [
    ("inp", re.compile(r"^(?:V?\+IN|V?IN\+|INP|IN_P|NONINV|\+)([A-D1-4]?)$|^(?:\+IN|IN)([A-D1-4])\+?$", re.I)),
    ("inn", re.compile(r"^(?:V?-IN|V?IN-|INN|IN_N|INV|-)([A-D1-4]?)$|^(?:-IN|IN)([A-D1-4])-$", re.I)),
    ("out", re.compile(r"^(?:V?OUT|VO|OUTPUT)([A-D1-4]?)$", re.I)),
]
_PIN_EXTRA = [   # the channel letter before the sign: +INA / -INB / INA+ / INB-
    ("inp", re.compile(r"^\+IN([A-D1-4])$|^IN([A-D1-4])\+$", re.I)),
    ("inn", re.compile(r"^-IN([A-D1-4])$|^IN([A-D1-4])-$", re.I)),
]
_SUPPLY = re.compile(r"^(V\+|V-|VCC|VDD|VEE|VSS|V\+[A-D]?|V-[A-D]?|VS\+|VS-|AVDD|AVSS)$", re.I)
_RAIL_NAME = re.compile(r"^(?:.*/)?(?:[+-]\d+V\d*|[+-]?\d+V\d+|VCC\w*|VDD\w*|VEE\w*|VSS\w*|V_?BAT\w*|AVDD\w*|"
                        r"DVDD\w*|VREF\w*|\+\w+)$", re.I)


def pin_role(name: str) -> Optional[tuple[str, str]]:
    """(role, channel) of an op-amp pin from its schematic name: role inp / inn / out / supply, channel "" for a
    single op-amp, else its letter or digit ("A", "1"); None when the name says nothing."""
    n = (name or "").strip().replace("−", "-")
    if not n:
        return None
    if _SUPPLY.match(n):
        return "supply", ""
    for role, rx in _PIN_EXTRA + _PIN:
        m = rx.match(n)
        if m:
            ch = next((g for g in m.groups() if g), "")
            return role, ch.upper()
    return None


@dataclass
class OpAmpChannel:
    """One op-amp channel of a part: its reference (with the channel letter for a dual or quad), the pads of its
    non-inverting input, inverting input and output, and its model's values."""
    name: str
    ref: str
    inp: Pad
    inn: Optional[Pad]
    out: Pad
    params: dict


def opamp_pin_roles(fp: Footprint) -> Optional[dict]:
    """{"inp", "inn", "out": pad} when a part's pins say it is one op-amp without any model or label: one pin named as
    the non-inverting input and one as the inverting input (+ / − / +IN / IN− ...), its output by name or else its one
    pin of electrical type output (a symbol whose output pin is unnamed: the OPA376's "~"), and supply pins by name
    (V+ / V− / VCC ...: a comparator or op-amp, not a connector with + / − pins). None otherwise (a dual or quad by
    its channel names is left to the user, as is anything without supply pins)."""
    roles: dict[str, list] = {}
    for p in fp.pads:
        r = pin_role(p.pin_function or "")
        if r:
            roles.setdefault(r[0], []).append(p)
    if not roles.get("supply") or len(roles.get("inp", ())) != 1 or len(roles.get("inn", ())) != 1:
        return None
    outs = roles.get("out") or [p for p in fp.pads if p.pin_type == "output" and p not in roles["inp"] + roles["inn"]]
    if len(outs) != 1:
        return None
    return {"inp": roles["inp"][0], "inn": roles["inn"][0], "out": outs[0]}


def unmodelled_note(refs: list[str]) -> str:
    """The note for op-amps (by their pins) whose model is 'open': the circuit leaves them out."""
    it = ", ".join(refs)
    return (f"{it} looks like an op-amp (its pin names) but has no model ('open'): it is left out, so its feedback "
            f"parts don't reach the output; set its model to amplifier (or give it a Gain field or GT amp labels)")


def opamp_channels(fp: Footprint, model) -> list[OpAmpChannel]:
    """The op-amp channels of an `amplifier` part [A52]: by the GT labels amp_in / amp_inv / amp_out (one channel),
    else by the pins' schematic names grouped by channel; a channel without IN− is kept with inn None (the gain
    block)."""
    from .tags import amp_label_roles
    params = dict(model.params)
    labels = amp_label_roles(fp)

    def pad(name):
        """The pad named by number or pin name."""
        want = str(name).upper()
        return next((p for p in fp.pads if str(p.number).upper() == want), None) or \
            next((p for p in fp.pads if (p.pin_function or "").upper() == want), None)
    def lone_inverting():
        """The part's one pin named as an inverting input (a single op-amp), else None."""
        inv = [p for p in fp.pads if (pin_role(p.pin_function or "") or ("",))[0] == "inn" and p.net]
        return inv[0] if len(inv) == 1 else None
    if labels and "in" in labels and "out" in labels:
        # the labels name the stage's input and output; IN− by its own label, else by its pin name (refboard's
        # TLV6001 U301: amp_in:1, amp_out:4, pin 3 named "−")
        a, b = pad(labels["in"]), pad(labels["out"])
        c = pad(labels["inv"]) if "inv" in labels else lone_inverting()
        return [OpAmpChannel(fp.reference, fp.reference, a, c, b, params)] if a is not None and b is not None else []
    if len(model.pins) >= 2:
        a, b = pad(model.pins[0]), pad(model.pins[1])
        return ([OpAmpChannel(fp.reference, fp.reference, a, lone_inverting(), b, params)]
                if a is not None and b is not None else [])
    chans: dict[str, dict] = {}
    for p in fp.pads:
        r = pin_role(p.pin_function or "")
        if r and r[0] != "supply" and p.net:
            chans.setdefault(r[1], {})[r[0]] = p
    out = []
    for ch, roles in sorted(chans.items()):
        if "inp" in roles and "out" in roles:
            name = fp.reference + (ch if len(chans) > 1 and ch else "")
            out.append(OpAmpChannel(name, fp.reference, roles["inp"], roles.get("inn"), roles["out"], params))
    if not out:                         # an unnamed output pin: the single op-amp by its inputs and output type
        pins = opamp_pin_roles(fp)
        if pins is not None and all(p.net for p in pins.values()):
            out.append(OpAmpChannel(fp.reference, fp.reference, pins["inp"], pins["inn"], pins["out"], params))
    return out


def ac_ground_nets(board: Board, config: Config) -> set[str]:
    """The nets that are AC ground for a small-signal circuit [A53]: the reference nets, every net on a supply pin of
    a part (schematic pin type power_in / power_out, or an op-amp's V+ / V− / VCC / VEE pin), and nets named as
    rails (+3V3, VCC, VDD_x, VREF ...): stiff sources, decoupled to ground."""
    out = set(config.reference_nets)
    for fp in board.footprints:
        for p in fp.pads:
            if not p.net:
                continue
            if p.pin_type in ("power_in", "power_out") or (pin_role(p.pin_function or "") or ("",))[0] == "supply":
                out.add(p.net)
    out |= {n for n in board.nets if _RAIL_NAME.match(n)}
    return out


@dataclass
class AnalogNetwork:
    """A path's circuit: the MNA circuit, its input (the source's node) and output (the first receiver's node), the
    nets, op-amp channels and parts in it, how each was modelled, and notes."""
    circuit: Circuit
    out_node: str
    nets: list = field(default_factory=list)
    opamps: list = field(default_factory=list)          # (name, "macromodel" | "gain block")
    parts: list = field(default_factory=list)           # references of the passive parts
    tracks: int = 0
    notes: list = field(default_factory=list)
    node_of: dict = field(default_factory=dict)         # "REF.PAD" -> circuit node
    # the ideal schematic in net terms (analysis/symbolic.py): parts [(ref, kind, net a, net b, values)], op-amps
    # [(name, net in+, net in−, net out, gain or None: a nullor)], the input and output nets; AC ground is "0"
    schematic: dict = field(default_factory=dict)
    amp_out: dict = field(default_factory=dict)         # op-amp channel -> the circuit node of its output pin
    # each track piece as a line element: {"a", "b" (nodes), "line" (PathLine), "uuid", "pa", "pb" (its ends, mm),
    # "layer", "net"}; each passive part's nodes and model: {ref: (node a, node b, model, pad a, pad b)}
    lines: list = field(default_factory=list)
    part_nodes: dict = field(default_factory=dict)
    amp_params: dict = field(default_factory=dict)      # op-amp channel -> its model's values (gain, bandwidth ...)


def analog_network(board: Board, config: Config, spec: NetSpec, receiver: str, lumped: bool = False,
                   f_top: float = 1e9, cache=None) -> AnalogNetwork:
    """The circuit of the path `spec` to `receiver` [A53] (see the module docstring). lumped: tracks as π sections
    (exact
    poles and zeros; a track longer than SHORT_TRACK of the wavelength at f_top gets a note), else as lines.
    cache: the cross-section cache (simulate.line_cache) for the tracks' Z0."""
    from .route import copper_graph, signal_nets
    a = config.analysis
    ground = ac_ground_nets(board, config)
    drv, rcv = board.find_pin(spec.driver), board.find_pin(receiver)
    if drv is None or rcv is None:
        raise ValueError(f"pin {spec.driver if drv is None else receiver!r} not found")
    notes: list[str] = []
    # ---- the nets: from the path's own (signal_nets: through series parts and op-amps) outwards
    start = (signal_nets(board, config, spec.name, set(spec.through)) | {drv.net, rcv.net}) - ground
    nets: list[str] = []
    todo = sorted(start)
    pads_on: dict[str, list[Pad]] = {}
    for fp in board.footprints:
        for p in fp.pads:
            if p.net:
                pads_on.setdefault(p.net, []).append(p)
    fps = {fp.reference: fp for fp in board.footprints}
    parts: dict[str, tuple] = {}            # ref -> (model, pad a, pad b)
    amps: dict[str, OpAmpChannel] = {}
    unmodelled: list[str] = []          # op-amps by their pins whose model is 'open': left out, said in a note
    while todo:
        net = todo.pop(0)
        if net in nets or net in ground:
            continue
        if len(nets) >= MAX_NETS:
            notes.append(f"the circuit stops at {MAX_NETS} nets ({net} and beyond left out)")
            break
        nets.append(net)
        for p in pads_on.get(net, []):
            fp = fps.get(p.footprint)
            if fp is None:
                continue
            m = config.model_for(fp.reference)
            if m is None or m.kind == "open":
                if (m is not None and not fp.dnp and fp.reference not in unmodelled
                        and opamp_pin_roles(fp) is not None):
                    unmodelled.append(fp.reference)
                continue
            if m.kind == "amplifier":
                for ch in opamp_channels(fp, m):
                    pins = [ch.inp, ch.out] + ([ch.inn] if ch.inn is not None else [])
                    if any(q is p for q in pins) and ch.name not in amps:
                        amps[ch.name] = ch
                        todo += [q.net for q in pins if q.net and q.net not in ground]
                continue
            if m.kind in PASSIVE and fp.reference not in parts:
                two = [q for q in fp.pads if q.net]
                if len({q.net for q in two}) == 2 and len(two) >= 2:
                    pa = next(q for q in two if q.net == net)
                    pb = next(q for q in two if q.net != net)
                    parts[fp.reference] = (m, pa, pb)
                    if pb.net not in ground:
                        todo.append(pb.net)
    if unmodelled:
        notes.append(unmodelled_note(unmodelled))
    # ---- the copper of those nets: nodes joined by pads, vias' layer joins and pours; tracks as lines; vias R + L
    g = copper_graph(board, set(nets))
    parent = list(range(len(g.pos)))

    def find(i):
        """The union-find root of graph node i."""
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, j, _, info in g.edges:
        if info[0] in ("pad", "join", "tht", "zone"):
            parent[find(i)] = find(j)
    # a net whose pads no copper joins (unrouted, or a gap): joined ideally, as the netlist says (a note). Joined
    # means by any copper, tracks and vias too (a second union-find over every edge): pads at the two ends of a
    # track stay apart in `parent`, the track's line between them
    whole = list(range(len(g.pos)))

    def find_all(i):
        """The root of graph node i over every copper edge."""
        while whole[i] != i:
            whole[i] = whole[whole[i]]
            i = whole[i]
        return i
    for i, j, _, _ in g.edges:
        whole[find_all(i)] = find_all(j)
    unrouted = set()
    for net in nets:
        islands = {}
        for p in pads_on.get(net, []):
            k = (p.layers[0], round(p.position[0] * 1000), round(p.position[1] * 1000)) if p.layers else None
            if k in g.keys:
                islands.setdefault(find_all(g.keys[k]), g.keys[k])
        ids = list(islands.values())
        for i in ids[1:]:
            parent[find(i)] = find(ids[0])
            whole[find_all(i)] = find_all(ids[0])
        if len(ids) > 1:
            unrouted.add(net)
    if unrouted:
        notes.append("not joined by copper, joined as the netlist says (no track there): "
                     + ", ".join(sorted(unrouted)))
    ckt = Circuit()
    name = lambda i: f"n{find(i)}"                                    # noqa: E731
    from ..emi.path import PathLine
    from ..solver.peec import via_impedance
    from .simulate import _line_loss, _line_of
    from .route import RouteSegment
    zs = {l.name: l.z for l in board.stackup.copper}
    tracks = 0
    # tracks: each track's own copper as lines between its points (its ends, an arc's chord points) and the
    # T-junctions on it (another track ending on its middle). copper_graph joins such an end to both ends of the
    # track it meets (for the route search); as circuit elements those joins would lie beside the track's own line
    # and double its copper, so the track is split at the junction instead
    from ..geometry.shapes import arc_points
    track_of: dict[int, object] = {}
    juncs: dict[int, set] = {}
    own: dict[int, list] = {}
    for i, j, _, info in g.edges:
        if info[0] != "track":
            continue
        t = info[1]
        if id(t) not in own:
            pts = arc_points(t.start, t.mid, t.end) if t.is_arc else [t.start, t.end]
            own[id(t)] = [g.keys[(t.layer, round(p[0] * 1000), round(p[1] * 1000))] for p in pts]
            track_of[id(t)] = t
            juncs[id(t)] = set()
        ids = set(own[id(t)])
        if i not in ids or j not in ids:                  # a junction: the end that is not one of the track's own
            juncs[id(t)].add(j if i in ids else i)
    lines: list[dict] = []
    for key, t in track_of.items():
        ids = own[key]
        pts = [g.pos[n][0] for n in ids]
        cum = [0.0]
        for p, q in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.dist(p, q))
        stops = [(c, n) for c, n in zip(cum, ids)]
        for n in juncs[key]:                              # each junction at its place along the track's chords
            x = g.pos[n][0]
            best = min(range(len(pts) - 1), key=lambda m: _seg_dist(x, pts[m], pts[m + 1])) if len(pts) > 1 else 0
            u = _seg_t(x, pts[best], pts[best + 1]) if len(pts) > 1 else 0.0
            stops.append((cum[best] + u * (cum[best + 1] - cum[best]) if len(pts) > 1 else 0.0, n))
        stops.sort(key=lambda s_: s_[0])
        la = t.layer
        neigh = board.stackup.neighbours(la)
        ref = neigh[0] if neigh else la
        for (s0, i), (s1, j) in zip(stops, stops[1:]):
            if find(i) == find(j) and s1 - s0 < 1e-6:
                continue
            pa, pb = g.pos[i][0], g.pos[j][0]
            seg = [RouteSegment(pa, pb, la, t.width, "track", t.uuid, t.line, t.net)]
            z0, eeff = _line_of(board, seg, ref, cache)
            line = PathLine(z0, eeff, max(s1 - s0, 1e-4) * 1e-3, **_line_loss(board, seg, ref))
            if lumped:
                _pi_section(ckt, name(i), name(j), line, f_top, notes)
            else:
                ckt.y2(name(i), ckt.ground, name(j), ckt.ground, line_y(line.zc, line.gamma_l))
            lines.append({"a": name(i), "b": name(j), "line": line, "uuid": t.uuid, "pa": pa, "pb": pb,
                          "layer": la, "net": t.net})
            tracks += 1
    for i, j, length, info in g.edges:
        kind = info[0]
        if kind == "via":
            v, la, lb = info[1], info[2], info[3]
            zf = via_impedance(abs(zs[la] - zs[lb]) * 1e-3, (v.drill or 0.3) * 1e-3)
            r_, l_ = zf(0.0).real, zf(1e6).imag / (2 * math.pi * 1e6)
            mid = ckt.internal("via")
            ckt.r(name(i), mid, max(r_, 1e-6))
            ckt.l(mid, name(j), l_, f"via {v.net}")
    # a part's or pin's node: its pad on its first layer; AC ground nets are the circuit's ground
    node_of: dict[str, str] = {}

    def at(p: Pad) -> str:
        """The circuit node of a pad: ground on an AC-ground net, else its copper node (a pad with no copper: its own
        node, so the circuit stays whole)."""
        if p.net in ground or not p.net:
            return ckt.ground
        k = (p.layers[0], round(p.position[0] * 1000), round(p.position[1] * 1000))
        n = name(g.keys[k]) if k in g.keys else f"pad {p.footprint}.{p.number}"
        node_of[f"{p.footprint}.{p.number}"] = n
        return n
    part_nodes = {}
    for ref, (m, pa, pb) in parts.items():
        part_nodes[ref] = (at(pa), at(pb), m, pa, pb)
        vals = dict(m.params)
        if m.kind == "cap":
            vals["C"] = m.capacitance()
            vals.pop("C_bias", None)
        ckt.part(at(pa), at(pb), m.kind, vals, ref)
    opamps = []
    amp_out = {}
    for nm, ch in amps.items():
        prm = ch.params
        amp_out[nm] = at(ch.out)
        if prm.get("gain") is not None or ch.inn is None:
            # the gain block: out = gain·v(in+) with one pole (bandwidth, else gbw/|gain|), behind z_out
            _gain_block(ckt, nm, at(ch.inp), at(ch.out), prm)
            why = "its Gain field" if prm.get("gain") is not None else "its IN− pin could not be told"
            opamps.append((nm, "gain block"))
            notes.append(f"{nm}: the gain block from its fields ({why}), the circuit around it not used")
            continue
        gbw = prm.get("gbw") or AMP_GBW
        if not prm.get("gbw"):
            notes.append(f"{nm}: no GBW, {AMP_GBW / 1e6:g} MHz assumed")
        # PM → the second pole [Franco 2015]: a follower's loop A(s) = GBW/s · 1/(1 + s/ω2) crosses over at x·GBW
        # with x·√(1 + (x·GBW/fp2)²) = 1 and has PM = 90° − atan(x·GBW/fp2), so x = sin(PM) and
        # fp2 = GBW·sin(PM)·tan(PM)
        # (exact for A0 ≫ 1; GBW·tan(PM), which takes the crossover at GBW, gave 29° for 15°)
        pm = math.radians(prm["pm"]) if prm.get("pm") else None
        fp2 = prm.get("fp2") or (gbw * math.sin(pm) * math.tan(pm) if pm else None)
        # c_in is each input's capacitance to ground (what it meant for the gain block: the load on the path), no
        # capacitance between the inputs: with it, a follower's input fed its output past the amplifier and
        # cancelled the output through R_O at √(2π·GBW/(C·R_O)), an undamped notch (5.88 MHz, −175 dB on refboard's
        # TLV6001) a real output stage does not have
        ckt.opamp(nm, at(ch.inp), at(ch.inn), at(ch.out),
                  OpAmpParams(gbw=gbw, a0=10 ** (prm["aol"] / 20) if prm.get("aol") else 1e5, fp2=fp2,
                              r_out=prm.get("z_out") or AMP_R_O, r_in=prm.get("r_in") or 1e12,
                              c_in=0.0, c_cm=prm.get("c_in") or 2e-12))
        opamps.append((nm, "macromodel"))
    # the source: 1 V behind the driver's R + jωL, its output C; the receivers' load
    d = at(drv)
    src = ckt.internal("source")
    ckt.vsource(src, ckt.ground)
    r_s = spec.driver_ohms if spec.driver_ohms is not None else a.driver_ohms
    if spec.driver_nh:
        mid = ckt.internal("source L")
        ckt.r(src, mid, max(r_s, 1e-6))
        ckt.l(mid, d, spec.driver_nh * 1e-9, "source L")
    else:
        ckt.r(src, d, max(r_s, 1e-6))
    if spec.driver_pf:
        ckt.c(d, ckt.ground, spec.driver_pf * 1e-12)
    load_pf = spec.load_pf if spec.load_pf is not None else a.load_pf
    for rx in spec.receivers:
        q = board.find_pin(rx)
        if q is not None and q.net not in ground:
            ckt.c(at(q), ckt.ground, load_pf * 1e-12)
    out = at(rcv)
    if spec.load_ohms:
        ckt.r(out, ckt.ground, spec.load_ohms)
    # nodes no element reaches would make the matrix singular: tie each to ground through 1 TΩ (an open pin's leak)
    for n in list(ckt.nodes):
        ckt.r(n, ckt.ground, 1e12)
    gnd = lambda net: "0" if net in ground or not net else net             # noqa: E731
    schematic = {
        "parts": [(ref, m.kind, gnd(pa.net), gnd(pb.net), dict(m.params, C=m.capacitance()) if m.kind == "cap"
                   else dict(m.params)) for ref, (m, pa, pb) in sorted(parts.items())],
        "opamps": [(nm, gnd(ch.inp.net), gnd(ch.inn.net) if ch.inn is not None else None, gnd(ch.out.net),
                    None if (kind == "macromodel") else float(ch.params.get("gain") if ch.params.get("gain")
                                                              is not None else 1.0))
                   for (nm, kind), ch in zip(opamps, amps.values())],
        "in": gnd(drv.net), "out": gnd(rcv.net),
        # parts marked DNP: kept in the circuit (the design as drawn), named in a note so H(s) can be redone without
        "dnp": sorted(ref for ref in parts if (fp := board.footprint(ref)) is not None and fp.dnp)}
    return AnalogNetwork(ckt, out, nets, opamps, sorted(parts), tracks, notes, node_of, schematic, amp_out, lines,
                         part_nodes, {nm: dict(ch.params) for nm, ch in amps.items()})


def _seg_t(p, a, b) -> float:
    """Where point p projects onto segment a-b, as its fraction (0 at a, 1 at b)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    ll = dx * dx + dy * dy
    return 0.0 if ll == 0 else min(max(((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / ll, 0.0), 1.0)


def _seg_dist(p, a, b) -> float:
    """Distance from point p to segment a-b (mm)."""
    t = _seg_t(p, a, b)
    return math.dist(p, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))


def _pi_section(ckt: Circuit, a: str, b: str, line, f_top: float, notes: list):
    """A track as its lumped π section: R (DC) + jωL in series, C/2 to ground at each end (L', C' from its Z0 and
    εeff, R' at DC) [Pozar 2012]; a note when the track is not short against the wavelength at f_top."""
    import numpy as np
    r, lp, g, cp = line.rlgc(np.array([1.0]))
    length = line.length_m
    mid = ckt.internal("track")
    ckt.r(a, mid, max(float(r[0]) * length, 1e-6))
    ckt.l(mid, b, lp * length, "track")
    ckt.c(a, ckt.ground, cp * length / 2)
    ckt.c(b, ckt.ground, cp * length / 2)
    from ..emi.spectrum import C0
    if length > SHORT_TRACK * C0 / (math.sqrt(line.eeff) * f_top) and "long tracks" not in " ".join(notes):
        exact_to = SHORT_TRACK * C0 / (math.sqrt(line.eeff) * length)
        notes.append(f"long tracks as one π section each: exact below {exact_to / 1e6:.3g} MHz")


def _gain_block(ckt: Circuit, name: str, inp: str, out: str, prm: dict):
    """An amplifier as its gain block [A52]: v = gain·v(in) through one pole (bandwidth, else gbw/|gain|: a
    voltage-feedback op-amp's closed-loop bandwidth [Franco 2015]; none: flat) and a buffer behind its output
    impedance (z_out), its input C to ground."""
    gain = float(prm.get("gain") if prm.get("gain") is not None else 1.0)
    bw = prm.get("bandwidth") or (prm["gbw"] / max(abs(gain), 1.0) if prm.get("gbw") else None)
    if prm.get("c_in"):
        ckt.c(inp, ckt.ground, prm["c_in"])
    node = ckt.internal(f"{name}.g")
    ckt.vccs(ckt.ground, node, inp, ckt.ground, gain)       # gain·v(in) into 1 Ω ∥ C: the pole
    ckt.r(node, ckt.ground, 1.0)
    if bw:
        ckt.c(node, ckt.ground, 1.0 / (2 * math.pi * bw))
    oi = ckt.internal(f"{name}.o")
    ckt.vcvs(oi, ckt.ground, node, ckt.ground, 1.0, f"{name}.buf")
    ckt.r(oi, out, max(prm.get("z_out") or 1.0, 1e-6))
