"""The circuit a signal path runs through, read from the board: every op-amp and every part on the path's nets and
on the nets beyond them, as one linear circuit for emi/mna.py (docs/ANALOG_AC.md). The user: "I want a generic
model that can calculate any circuit made from an OPA".

- **The network** (`analog_network`, [A53]): from the path's nets, every two-pin part with a model (resistor, capacitor,
  inductor, ferrite, 0R) and every op-amp channel with a pin on a net of it, and the nets on their other pins, until
  AC ground: the reference nets and the supply rails (`ac_ground_nets`), never a net of the path itself
  (route.route_nets). At most MAX_NETS nets besides the path's and its op-amps' (a note names what it leaves
  out). An IC that is not an op-amp is open on the network (its pins draw nothing), except the path's driver (the
  source) and receivers (the load); every part it leaves out is named in a note.
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
    """The op-amp channels of an `amplifier` part [A52]: by the GT labels amp_in / amp_inv / amp_out (one group per
    channel: a quad's U3A, U3B ..., tags.amp_label_groups), else by the pins' schematic names grouped by channel; a
    channel without IN− is kept with inn None (the gain block)."""
    from .tags import amp_label_groups, channel_name
    params = dict(model.params)
    groups = [g for g in amp_label_groups(fp) if "in" in g and "out" in g]

    def pad(name):
        """The pad named by number or pin name."""
        want = str(name).upper()
        return next((p for p in fp.pads if str(p.number).upper() == want), None) or \
            next((p for p in fp.pads if (p.pin_function or "").upper() == want), None)
    def lone_inverting():
        """The part's one pin named as an inverting input (a single op-amp), else None."""
        inv = [p for p in fp.pads if (pin_role(p.pin_function or "") or ("",))[0] == "inn" and p.net]
        return inv[0] if len(inv) == 1 else None

    def same_channel(p: Pad, role: str) -> Optional[Pad]:
        """The pin of role `role` in the same channel as pad p by their pin names (+INA beside −INA, IN1+ beside
        IN1−), else None (p's name says no channel, or the channel has not exactly one such pin)."""
        r = pin_role(p.pin_function or "")
        if r is None or r[0] == "supply":
            return None
        found = [q for q in fp.pads if q.net and (pin_role(q.pin_function or "") or ("", None))[:2] == (role, r[1])]
        return found[0] if len(found) == 1 else None
    if groups:
        # the labels name each stage's input and output; IN− by its own label, else by its pin name (refboard's
        # TLV6001 U301: amp_in:1, amp_out:4, pin 3 named "−"; a quad has four, so its channels need amp_inv)
        out = []
        for k, g in enumerate(groups):
            a, b = pad(g["in"]), pad(g["out"])
            c = pad(g["inv"]) if "inv" in g else lone_inverting()
            if a is not None and b is not None:
                out.append(OpAmpChannel(channel_name(fp.reference, k, len(groups)), fp.reference, a, c, b, params))
        return out
    if len(model.pins) >= 2:
        a, b = pad(model.pins[0]), pad(model.pins[1])
        if a is None or b is None:
            return []
        # IN− from the same channel as the input named (a dual's −INA beside +INA): the whole part's search finds
        # none on a dual, and the channel became a unity gain block (audit 2026-11, E-4); `pins` naming the inverting
        # input itself (an inverting stage) takes the channel's + input beside it
        role = (pin_role(a.pin_function or "") or ("",))[0]
        plus = same_channel(a, "inp") if role == "inn" else None
        if plus is not None:
            return [OpAmpChannel(fp.reference, fp.reference, plus, a, b, params)]
        inn = same_channel(a, "inn") or lone_inverting()
        return [OpAmpChannel(fp.reference, fp.reference, a, inn, b, params)]
    chans: dict[str, dict] = {}
    for p in fp.pads:
        r = pin_role(p.pin_function or "")
        if r and r[0] != "supply" and p.net:
            chans.setdefault(r[1], {})[r[0]] = p
    out = []
    for ch, roles in sorted(chans.items()):
        if "inp" in roles and "out" in roles:
            out.append(OpAmpChannel(_unit_name(fp.reference, ch) if len(chans) > 1 else fp.reference,
                                    fp.reference, roles["inp"], roles.get("inn"), roles["out"], params))
    if not out:                         # an unnamed output pin: the single op-amp by its inputs and output type
        pins = opamp_pin_roles(fp)
        if pins is not None and all(p.net for p in pins.values()):
            out.append(OpAmpChannel(fp.reference, fp.reference, pins["inp"], pins["inn"], pins["out"], params))
    return out


def _unit_name(ref: str, ch: str) -> str:
    """A channel's name from its part and its pin names' channel: U3A for a letter; U3.1 for a digit, as U31 is
    another part's name (two channels with the same name: one was dropped, audit 2026-11, E-9)."""
    return f"{ref}.{ch}" if ch.isdigit() else ref + ch


def supply_pin_nets(board: Board, hard: bool = False) -> set[str]:
    """Nets on a part's supply pin: schematic pin type power_out (a regulator's output) or a pin named as a supply
    (V+ / V− / VCC / VDD / VEE / VSS: pin_role), and with hard=False also pin type power_in (an IC's supply input:
    a symbol may give that type to a pin that is not one, a gate driver's GNDB on a phase node)."""
    types = ("power_out",) if hard else ("power_in", "power_out")
    out = set()
    for fp in board.footprints:
        for p in fp.pads:
            if p.net and (p.pin_type in types or (pin_role(p.pin_function or "") or ("",))[0] == "supply"):
                out.add(p.net)
    return out


def resistor_only_nets(board: Board, config: Config) -> set[str]:
    """Nets every fitted part on which is a resistor (its model, or an R part without a model): a divider's tap, not a
    rail, whatever its name (no decoupling, no supply pin, no regulator's inductor or connector)."""
    from ..components.defaults import ref_prefix
    kinds: dict[str, set] = {}
    for fp in board.footprints:
        if fp.dnp:
            continue
        m = config.model_for(fp.reference)
        res = m.kind == "resistor" if m is not None else ref_prefix(fp.reference) == "R"
        for n in fp.nets():
            kinds.setdefault(n, set()).add(res)
    return {n for n, k in kinds.items() if k == {True}}


def rail_named(net: str) -> bool:
    """A net named as a rail (+3V3, VCC, VDD_x, VBAT, VREF ...: _RAIL_NAME); never KiCad's automatic name for a
    pin's net (Net-(U3-VOUT))."""
    return not net.rsplit("/", 1)[-1].startswith("Net-(") and bool(_RAIL_NAME.match(net))


def ac_ground_nets(board: Board, config: Config) -> set[str]:
    """The nets that are AC ground for a small-signal circuit [A53]: the reference nets, every net on a supply pin of
    a part (supply_pin_nets), and nets named as rails (rail_named): stiff sources, decoupled to ground. Not a
    rail-named net with only resistors on it: a divider's tap (audit 2026-11, E-2: one named VREF_MID was grounded,
    its resistors left out, the AC coupling's corner 159 Hz for 106 Hz)."""
    out = set(config.reference_nets) | supply_pin_nets(board)
    taps = resistor_only_nets(board, config)
    out |= {n for n in board.nets if rail_named(n) and n not in taps}
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
    # the path's own nets (the signal's: through its series parts and op-amp channels), the circuit around them
    # beyond: on_path tells the channels the signal passes from those only loading it (a quad's other channels)
    path_nets: list = field(default_factory=list)

    def on_path(self, name: str) -> bool:
        """Whether op-amp channel `name` carries the signal: its output and one of its inputs on the path's own nets
        (U3A from the path's divider to its next stage), not a channel the circuit holds only for its loading (a
        bias buffer, a quad's other channels tied to the path's nets: the user, 2026-11-03, "why is it showing all
        the OPAmps (U3:A,B,C,D) I am only looking at U3A"). True for every channel when the path's nets are not
        known."""
        if not self.path_nets:
            return True
        # the path's nets are directed (route.path_nets: forward from the driver and backward from the receiver), and
        # the input counted is not the output's own net: a follower's IN− is its output (audit 2026-11, G-3)
        path = set(self.path_nets)
        for nm, inp, inn, out, _ in self.schematic.get("opamps", ()):
            if nm == name:
                return out in path and any(n in path and n != out for n in (inp, inn) if n)
        return True


def analog_network(board: Board, config: Config, spec: NetSpec, receiver: str, lumped: bool = False,
                   f_top: float = 1e9, cache=None) -> AnalogNetwork:
    """The circuit of the path `spec` to `receiver` [A53] (see the module docstring). lumped: tracks as π sections
    (exact
    poles and zeros; a track longer than SHORT_TRACK of the wavelength at f_top gets a note), else as lines.
    cache: the cross-section cache (simulate.line_cache) for the tracks' Z0."""
    from ..components.models import model_pads
    from .route import _crossed_note, copper_graph, route_nets
    a = config.analysis
    drv, rcv = board.find_pin(spec.driver), board.find_pin(receiver)
    if drv is None or rcv is None:
        raise ValueError(f"pin {spec.driver if drv is None else receiver!r} not found")
    notes: list[str] = []
    # ---- the path's own nets: forward from the driver, backward to this receiver, op-amps only from input to output
    # (route.route_nets [A81]); a net the signal must pass is never AC ground, whatever its name or pin types (a
    # battery divider's VBAT_DIV, a gate driver's power_in GNDB on a phase node: shorted, every response 0, audit
    # 2026-11 E-1), nor are the path's ends
    path, crossed = route_nets(board, config, drv.net, rcv.net, set(spec.through))
    if crossed:
        notes.append(_crossed_note(crossed))
    if not path:
        notes.append(f"{receiver} is not reached from {spec.driver} through parts that pass a signal (series parts, "
                     "Pass through, op-amps from input to output): the circuit is built around both ends")
    ground = ac_ground_nets(board, config) - path - {drv.net, rcv.net}
    start = (path | {drv.net, rcv.net}) - ground
    nets: list[str] = []
    pads_on: dict[str, list[Pad]] = {}
    for fp in board.footprints:
        for p in fp.pads:
            if p.net:
                pads_on.setdefault(p.net, []).append(p)
    fps = {fp.reference: fp for fp in board.footprints}
    parts: dict[str, tuple] = {}            # ref -> (model, pad a, pad b)
    amps: dict[str, OpAmpChannel] = {}
    unmodelled: list[str] = []          # op-amps by their pins whose model is 'open': left out, said in a note
    buffer_ins: dict[str, tuple] = {}   # a logic buffer's input on the nets: "U5.1" -> (pad, its model)
    left_out: dict[str, str] = {}       # parts with a pin on the circuit's nets it leaves out: ref -> why
    not_fitted: list[str] = []
    ends = {drv.footprint, *(q.footprint for q in (board.find_pin(r) for r in spec.receivers) if q is not None)}
    # ---- the nets, breadth first from the path's: the path's own and every included op-amp channel's (its feedback
    # network) first and never cut; other nets (loads, bias branches) after them, up to MAX_NETS in all. The cut
    # followed the file's order and could drop an op-amp's feedback net or the receiver's (audit 2026-11, E-5: a
    # gain-10 stage read 76 794, open loop)
    must = set(start)
    first, later = sorted(start), []
    dropped: list[str] = []
    while first or later:
        net = first.pop(0) if first else later.pop(0)
        if net in nets or net in ground:
            continue
        if net not in must and len(nets) >= MAX_NETS:
            if net not in dropped:
                dropped.append(net)
            continue
        if len(nets) >= 2 * MAX_NETS:       # op-amp chains pulling in ever more must-have nets: a hard stop
            dropped.append(net)
            continue
        nets.append(net)
        for p in pads_on.get(net, []):
            fp = fps.get(p.footprint)
            if fp is None:
                continue
            if fp.dnp:                      # not fitted: not in the circuit (as bias.at_dc and the shunts have it)
                if fp.reference not in not_fitted:
                    not_fitted.append(fp.reference)
                continue
            m = config.model_for(fp.reference)
            if m is None or m.kind == "open":
                if (m is not None and fp.reference not in unmodelled and opamp_pin_roles(fp) is not None):
                    unmodelled.append(fp.reference)
                elif fp.reference not in ends and fp.reference not in left_out:
                    left_out[fp.reference] = "no settings entry" if m is None else "model open"
                continue
            if m.kind == "buffer":         # a logic buffer is not linear: only its input's C_in loads the circuit
                from ..components.models import buffer_pins
                for inp, _ in buffer_pins(fp, m, net):
                    if inp is p:
                        buffer_ins[p.pin] = (p, m)
                continue
            if m.kind == "amplifier":
                for ch in opamp_channels(fp, m):
                    pins = [ch.inp, ch.out] + ([ch.inn] if ch.inn is not None else [])
                    if any(q is p for q in pins) and ch.name not in amps:
                        amps[ch.name] = ch
                        for q in pins:
                            if q.net and q.net not in ground:
                                must.add(q.net)
                                first.append(q.net)
                continue
            if m.kind in PASSIVE and fp.reference not in parts:
                # the pads its model joins (model_pads: `pins`, a 4-terminal resistor's force pads), else all; a
                # passive on more than two nets (a resistor array) is left out with a note, not silently (E-7)
                two = [q for q in model_pads(fp, m) if q.net]
                if len({q.net for q in two}) == 2:
                    pa = next((q for q in two if q.net == net), None)
                    if pa is None:              # this net's pad is not one the model joins (a Kelvin sense pad)
                        left_out.setdefault(fp.reference, "its model joins other pads")
                        continue
                    pb = next(q for q in two if q.net != net)
                    parts[fp.reference] = (m, pa, pb)
                    if pb.net not in ground:
                        later.append(pb.net)
                else:
                    left_out.setdefault(fp.reference, f"a {m.kind} on {len({q.net for q in two})} nets: give it "
                                                      "pins [a, b]")
    if dropped:
        shown = ", ".join(dropped[:5]) + (f" and {len(dropped) - 5} more" if len(dropped) > 5 else "")
        notes.append(f"the circuit stops at {len(nets)} nets: {shown} left out, the parts to them open there (the "
                     "path's own nets and its op-amps' feedback are all in): the response may be wrong")
    if unmodelled:
        notes.append(unmodelled_note(unmodelled))
    if not_fitted:
        notes.append(f"not fitted (DNP), left out: {', '.join(sorted(not_fitted))}")
    if left_out:
        items = [f"{r} ({w})" for r, w in sorted(left_out.items())]
        notes.append("left out of the circuit, open: " + ", ".join(items[:8])
                     + (f" and {len(items) - 8} more" if len(items) > 8 else "")
                     + " (a part the signal or a feedback loop needs must have a model: Parts tab)")
    # ---- the path's driver pin is an op-amp channel's output: the source stands for that channel (both driving one
    # node fought, the response collapsed by 88 dB, audit 2026-11 E-3)
    for nm, ch in list(amps.items()):
        if ch.out.footprint == drv.footprint and ch.out.number == drv.number:
            del amps[nm]
            notes.append(f"{nm}: its output is the path's driver, so the source stands for it (its own circuit left "
                         "out)")
    open_loop = _open_loops(amps, parts, ground)
    if open_loop:
        notes.append(f"{', '.join(open_loop)}: no DC path from the output back to IN− in the circuit (an integrator, "
                     "or a feedback part missing or 'open'): the DC gain is the open-loop gain")
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
            r_, l_ = via_piece(v.layers, la, lb, zs, v.drill or 0.3)
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
    from ..components.models import BUF_C_IN
    for p, m in buffer_ins.values():       # its c_in to ground, else the CMOS default [A73]
        ckt.c(at(p), ckt.ground, m.params.get("c_in") or BUF_C_IN)
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
        if q is not None and q.net not in ground and spec.load_ohms:
            # the load at every receiver, so each receiver's circuit is the same circuit (only the analysed one's
            # was loaded: two receivers, two circuits, audit 2026-11 E-10)
            ckt.r(at(q), ckt.ground, spec.load_ohms)
    out = at(rcv)
    if spec.load_ohms and not any((q := board.find_pin(rx)) is not None and q is rcv for rx in spec.receivers):
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
        # parts marked DNP: left out of the circuit as built (audit 2026-11, E-11: a DNP part with a model stayed in,
        # unlike in the DC bias and the shunts), named in a note
        "dnp": sorted(not_fitted)}
    return AnalogNetwork(ckt, out, nets, opamps, sorted(parts), tracks, notes, node_of, schematic, amp_out, lines,
                         part_nodes, {nm: dict(ch.params) for nm, ch in amps.items()}, path_nets=sorted(start))


def via_piece(layers: list, la: str, lb: str, zs: dict, drill: float) -> tuple[float, float]:
    """(R Ω, L H) of a via's piece between layers la and lb: its share (by length) of the whole barrel's
    (peec.via_impedance over the via's full span [A3]); each piece's own partial inductance, summed, left out the
    pieces' mutual inductance (audit 2026-11, E-12: 0.43 for 0.63 nH through four layers)."""
    from ..solver.peec import via_impedance
    seg = abs(zs[la] - zs[lb])
    span = abs(zs[layers[0]] - zs[layers[-1]]) or seg or 1e-3
    zf = via_impedance(span * 1e-3, drill * 1e-3)
    share = seg / span
    return zf(0.0).real * share, zf(1e6).imag / (2 * math.pi * 1e6) * share


def _open_loops(amps: dict, parts: dict, ground: set) -> list[str]:
    """The macromodel op-amp channels whose output has no DC path back to their IN−: through resistors, inductors,
    ferrites and 0R of the circuit (both ways) and other channels (input to output), never through ground. Such a
    channel runs at its open-loop gain at DC: a feedback resistor left out (no model, 'open'), or an integrator
    (audit 2026-11, E-8: a gain-10 stage read 99 504 without a word)."""
    dc: dict[str, set] = {}
    for m, pa, pb in parts.values():
        if m.kind in ("resistor", "inductor", "ferrite", "short") and pa.net and pb.net:
            dc.setdefault(pa.net, set()).add(pb.net)
            dc.setdefault(pb.net, set()).add(pa.net)
    for ch in amps.values():
        for q in (ch.inp, ch.inn):
            if q is not None and q.net and ch.out.net:
                dc.setdefault(q.net, set()).add(ch.out.net)
    out = []
    for nm, ch in amps.items():
        if ch.inn is None or ch.params.get("gain") is not None or not ch.inn.net or ch.inn.net in ground:
            continue
        seen, todo = {ch.out.net}, [ch.out.net]
        while todo:
            for n in dc.get(todo.pop(), ()):
                if n not in seen and n not in ground:
                    seen.add(n)
                    todo.append(n)
        if ch.inn.net not in seen:
            out.append(nm)
    return out


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
