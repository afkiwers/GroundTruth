"""What a motor bridge's or a power stage's switching does to the rest of the board, or to a stack of boards
(analysis/stack.py): the noise a victim net's receiver sees, the current the switch nodes push into it, and how far
victim parts' grounds move, at the switching edges of the operating point.

Each leg's (or stage's) hot loop is solved once more on a model that reaches the victims (their pads, their tracks, the stack's
connector pins), at two frequencies of the edge's spectrum. From that solution, per ampere of loop current:

- receiver noise: each victim net is in the model as copper, driven at its driver pin through the driver's resistance
  to its local ground (a pull-up's value, else victims.drive_ohms, 50 ohm, a push-pull output) and loaded at each receiver pin by an
  input capacitance (victims.input_pf, 5 pF) to the receiver's local ground. The solver then gives the voltage across that input
  directly: the receiver's ground moving against the driver's, the field of the hot loop along the track and the
  connector pins it crosses, all from its own coupling. As an inductance M = |v|/(ωI) at the hot-loop frequency, the
  peak at an edge is M·I/t_r. A ground that crosses to another board on a few pins, or a reset line that crosses
  near the bridge, shows here.
- injected current: a victim track facing a phase node's copper with no copper layer between them (a logic board's
  bottom layer over the bridge's pours) has the capacitance C = ε0·εr·A/h to it [Pozar 2012] and takes C·V_bus/t_r
  at every edge.
- ground shift (for information): a victim part's ground potential against the reference pin (the bridge's input
  minus unless set), L_g = Im(Δφ)/(ωI). A part's ground alone does not upset it; what crosses between grounds does
  (the receiver noise above).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from shapely.geometry import LineString
from shapely.ops import unary_union

from ..config.settings import Config
from ..core.board import Board, Pad
from ..geometry.copper import reference_layers

MU0 = 4e-7 * math.pi
EPS0 = 8.8541878128e-12
DRIVER_TYPES = ("output", "tri_state", "open_collector", "open_emitter", "power_out")

PART_PATTERNS = [("microcontroller", r"STM32|GD32|AT(SAM|MEGA|TINY)|PIC\d|DSPIC|MSP430|ESP32|RP2040|NRF5|EFM32|"
                                     r"LPC\d|MK[LVE]?\d|S32K|TMS320|F28\d|XMC\d|CH32"),
                 ("reset supervisor", r"TCM8\d\d|MAX8\d\d|MAX6\d\d|APX8\d\d|TPS38\d|STM6\d\d|MCP1[03]\d|ADM8\d\d|"
                                      r"NCP30\d|SUPERVIS|RESET")]
NET_PATTERNS = [("reset", r"N?RST|RESET"), ("crystal", r"OSC|XTAL|XIN|XOUT|HSE|LSE"),
                ("Hall sensor", r"HALL|HAL_?[ABCUVW]\b"), ("debug", r"SWD|SW_?CLK|SW_?DIO|JTAG|TCK|TMS"),
                ("PWM / gate input", r"PWM|^IN[HL]|[HL]IN|_HI$|_LO$")]


@dataclass
class VictimSet:
    """What may be upset by a bridge's switching: parts (whose ground shift counts) and nets (whose induced and
    injected voltage counts), each with the reason it was picked, and the pin the ground shift is measured from."""
    parts: dict[str, str] = field(default_factory=dict)     # reference -> why it is one
    nets: dict[str, str] = field(default_factory=dict)      # net -> why
    reference: Optional[Pad] = None                         # the ground shift is measured from here
    reference_set: bool = False                             # ... a pin the user chose
    notes: list[str] = field(default_factory=list)


def _leaf(net: str) -> str:
    """A hierarchical net's last part ("/CPU/NRST" -> "NRST"): what the name patterns are matched against."""
    return net.split("/")[-1]


def auto_nets(board: Board, config: Config, skip: set) -> dict[str, str]:
    """{net: why} of the nets found as victims when the settings list none: routed nets whose name says reset,
    crystal, Hall sensor, debug or PWM / gate input (NET_PATTERNS), not the reference nets and not those in `skip`
    (the switching stages' own nets)."""
    routed = {t.net for t in board.tracks}
    out = {}
    for net in board.nets:
        if net in skip or net not in routed or net in config.reference_nets:
            continue
        why = next((w for w, rx in NET_PATTERNS if re.search(rx, _leaf(net), re.I)), None)
        if why:
            out[net] = why
    return out


def board_auto_nets(board: Board, config: Config) -> dict[str, str]:
    """The automatic victim nets of the whole board (the Bridges tab's victims picker): auto_nets with every
    switching stage's nets left out (every converter and every bridge leg, their rails and grounds), so it lists
    what any stage's Simulate would look at; one stage's own run leaves out only its own (find_victims)."""
    from .detect import power_nets
    stages = config.stages()
    skip = set(power_nets(board, config, stages)) if stages else set()
    skip |= {s.rail for s in stages} | {s.ground for s in stages}
    skip |= {b.rail for b in config.bridges} | {b.ground for b in config.bridges}
    return auto_nets(board, config, skip)


def find_victims(board: Board, config: Config, stages: list, rail: str, ground: str,
                 input_connector: list) -> VictimSet:
    """The parts and nets to look at for switching stages (a bridge's legs, or one power stage): the settings'
    lists, else the board's microcontrollers and reset supervisors and its reset, crystal, Hall, debug and PWM nets
    (routed, not the stages' own). The ground shift is measured from the settings' reference pin, else the input
    connector's minus, else the first low side's ground pad."""
    v, out = config.victims, VictimSet()
    for ref in v.parts:
        if board.footprint(ref) is None:
            out.notes.append(f"victims: part {ref} not on the board")
        else:
            out.parts[ref] = "listed"
    if not v.parts:
        for fp in board.footprints:
            why = next((w for w, rx in PART_PATTERNS if re.search(rx, f"{fp.value} {fp.lib_id}", re.I)), None)
            if why and not fp.dnp:
                out.parts[fp.reference] = why
    for net in v.nets:
        if net not in board.nets:
            out.notes.append(f"victims: net {net} not on the board")
        else:
            out.nets[net] = "listed"
    if not v.nets:
        # the stages' own nets (rail, switch nodes, gate drive, input path): their drive circuit, not their victims
        from .detect import power_nets
        out.nets.update(auto_nets(board, config, set(power_nets(board, config, stages)) | {rail, ground}))
    ref_pin = v.reference or (input_connector[1] if len(input_connector) == 2 else "")
    out.reference, out.reference_set = (board.find_pin(ref_pin) if ref_pin else None), bool(v.reference)
    if out.reference is None:                      # the first low side's (or integrated stage's) ground pad
        out.reference = next((p for s in stages for r in list(s.low_side) + list(s.switch) if board.footprint(r)
                              for p in board.footprint(r).pads if p.net == ground), None)
    return out


def stage_rows(board: Board, copper, victims: VictimSet, run, probes: list, ground: str, stage: str, i_edge: float,
               tr: float, v_swing: float, switch_node: Optional[str]) -> list[dict]:
    """One switching stage's victim rows from its victim run (solved at LOOP_F_LO and LOOP_F): the noise at each
    victim net's receivers (the mutual inductance to the hot loop times the switched current's slope I/t_r), each
    victim part's ground shift against the reference pin, and the current the switch node's copper pushes into
    each victim net it faces (C·V/t_r)."""
    from .power import LOOP_F, LOOP_F_LO
    rows = []
    sols = run.ok() if run is not None else {}
    if LOOP_F in sols and LOOP_F_LO in sols:
        hi, lo = sols[LOOP_F], sols[LOOP_F_LO]
        w = 2 * math.pi * LOOP_F
        for probe in probes:
            # the mutual inductance between the hot loop and the victim net's loop (|V| / w per A); times the
            # switched current's slope I / t_r it is the peak voltage induced at the receiver
            m = abs(probe_voltage(hi, probe)) / w
            rows.append({"kind": "net", "name": probe.net, "why": victims.nets[probe.net], "leg": stage,
                         "from": probe.driver.pin, "to": probe.receiver.pin, "m_nh": m * 1e9, "peak": m * i_edge / tr})
        if victims.reference is not None:
            phi_ref_hi = ground_potential(run.build, hi, board, victims.reference, ground)
            phi_ref_lo = ground_potential(run.build, lo, board, victims.reference, ground)
            for ref in victims.parts:
                pads = [p for p in board.footprint(ref).pads if p.net == ground]
                dv = [(ground_potential(run.build, hi, board, p, ground),
                       ground_potential(run.build, lo, board, p, ground)) for p in pads]
                dv = [(h - phi_ref_hi, lo_ - phi_ref_lo) for h, lo_ in dv
                      if None not in (h, lo_, phi_ref_hi, phi_ref_lo)]
                if dv:
                    # the part's ground against the reference: its inductive part (Im / w) times di/dt is the
                    # shift during an edge, its resistive part times the current the step that remains
                    h, lo_ = max(dv, key=lambda x: abs(x[0]))
                    l_g = abs(h.imag) / w
                    rows.append({"kind": "part", "name": ref, "why": victims.parts[ref], "leg": stage,
                                 "reference": victims.reference.pin, "l_nh": l_g * 1e9, "peak": l_g * i_edge / tr,
                                 "step_v": abs(lo_.real) * i_edge})
    if switch_node:
        for net in victims.nets:
            # the switch node's copper facing the victim net's: C dV/dt is the current pushed into it
            c = facing_capacitance(board, copper, net, switch_node)
            if c > 0:
                rows.append({"kind": "injected", "name": net, "why": victims.nets[net], "leg": stage,
                             "c_pf": c * 1e12, "peak": c * v_swing / tr})
    return rows


def finish_rows(rows: list[dict], settings, reference_set: bool) -> list[dict]:
    """The rows in report order (net noise, injected current, ground shift; the largest first), each with its
    warning limit and whether it is checked (a part's ground against a pin it takes no signal from is information,
    unless that pin was chosen)."""
    order = {"net": 0, "injected": 1, "part": 2}
    rows = sorted(rows, key=lambda r: (order[r["kind"]], -r["peak"]))
    for r in rows:
        r["limit"] = victim_limit(r["kind"], settings)
        r["checked"] = r["kind"] != "part" or reference_set
    return rows


def victim_limit(kind: str, settings) -> float:
    """The warning limit for a victim row: the ground shift (V) of a part, the induced voltage (V) of a net, or the
    injected current (A) into a net."""
    if kind == "part":
        return settings.shift_warning_v
    if kind == "net":
        return settings.induced_warning_v
    return settings.injected_warning_ma * 1e-3


def victim_text(r: dict) -> str:
    """One victim row as a sentence (issues, text report); small voltages in mV, small inductances in pH."""
    def volts(v: float) -> str:
        """0.30 V, 11.0 mV, 1.5 mV."""
        return f"{v:.2f} V" if v >= 0.1 else f"{v * 1e3:.1f} mV"

    def henry(nh: float) -> str:
        """0.360 nH, 2.40 pH."""
        return f"{nh:.3f} nH" if nh >= 0.01 else f"{nh * 1e3:.2f} pH"
    if r["kind"] == "part":
        return (("" if r.get("checked") else "(information) ")
                + f"ground of {r['name']} ({r['why']}) moves {volts(r['peak'])} peak against {r['reference']} with "
                f"{r['leg']}: {henry(r['l_nh'])} (step {r['step_v'] * 1e3:.0f} mV with the switched current)")
    if r["kind"] == "net":
        return (f"{volts(r['peak'])} peak noise at {r['to']} on {r['name']} ({r['why']}, driven from {r['from']}) "
                f"with {r['leg']}: {henry(r['m_nh'])} of coupling to its hot loop")
    return (f"{r['peak'] * 1e3:.1f} mA peak injected into {r['name']} ({r['why']}) by {r['leg']}: "
            f"{r['c_pf']:.2f} pF to its switch node")


def region_points(board: Board, victims: VictimSet, ground: str) -> tuple[list, set]:
    """(points, layers) the victims' model must also hold: the victim parts' ground pads, the victim nets' copper
    and pads, the reference pin and a stack's connector pins."""
    pts, layers = [], set()
    for ref in victims.parts:
        for p in board.footprint(ref).pads:
            if p.net == ground:
                pts.append(p.position)
                layers |= set(p.layers[:1])
    for t in board.tracks:
        if t.net in victims.nets:
            pts += [t.start, t.end]
            layers.add(t.layer)
            layers |= {r for r, _ in reference_layers(board, t.layer)[:1]}
    for net in victims.nets:
        for p in (p for fp in board.parts_by_net().get(net, []) for p in fp.pads if p.net == net):
            pts.append(p.position)
            layers |= set(p.layers[:1])
    if victims.reference is not None:
        pts.append(victims.reference.position)
        layers |= set(victims.reference.layers[:1])
    for v in board.vias:
        if v.uuid.startswith("stack pin "):
            pts.append(v.position)
            layers |= set(v.layers)
    return pts, layers


# ------------------------------------------------------------------ the victims in the model
@dataclass
class Probe:
    """A victim net's receiver input in the model: its voltage is V(rx_node) - V(gnd_node)."""
    net: str
    driver: Pad
    receiver: Pad
    rx_node: tuple
    gnd_node: tuple


def pad_node(st, pad: Pad, net: str) -> Optional[tuple]:
    """The model's node of the net at a pin; further out along its copper when a fine-pitch neighbour took the pin's
    own grid node (0.5 mm pins on a 0.8 mm grid share nodes, and a node holds one net)."""
    for snap in (max(1.5 * st.d, 0.4), 3.0, 6.0):
        n = next((n for n in (st.node(l, pad.position[0], pad.position[1], net, snap) for l in pad.layers) if n),
                 None)
        if n is not None:
            return n
    return None


def ground_node(st, board: Board, pad: Pad, ground: str) -> Optional[tuple]:
    """The ground copper a pin is referred to: on its own layers when it is a ground pin, else the nearest layer
    (in the stackup) with ground copper at that spot."""
    stk = board.stackup
    z0 = stk.layer(pad.layers[0]).z if pad.layers and stk.layer(pad.layers[0]) else 0.0
    layers = sorted((l for l in st.names), key=lambda l: abs(stk.layer(l).z - z0))
    if pad.net == ground:
        layers = [l for l in pad.layers if l in st.names] + layers
    for snap in (max(1.5 * st.d, 0.4), 3.0):
        for layer in layers:
            n = st.node(layer, pad.position[0], pad.position[1], ground, snap)
            if n is not None:
                return n
    return None


def ground_potential(st, sol, board: Board, pad: Pad, ground: str) -> Optional[complex]:
    """The solved potential of the ground copper under `pad` (V per A of the switching current), None when the pad
    reaches no ground copper in the model."""
    n = ground_node(st, board, pad, ground)
    return complex(sol.V[n]) if n is not None else None


def _drive_ohms(board: Board, config: Config, pad: Pad) -> float:
    """A resistor drives the net with its own value (a pull-up: from the settings, else its footprint's value, as for
    a stacked board's parts), anything else as a push-pull output."""
    from ..components.defaults import suggest_model
    fp = board.footprint(pad.footprint)
    model = config.model_for(pad.footprint) or (suggest_model(fp) if fp is not None else None)
    if model is not None and model.kind == "resistor":
        return max(model.get("R"), 1e-3)
    return config.victims.drive_ohms


def terminations(st, board: Board, config: Config, victims: VictimSet, ground: str
                 ) -> tuple[list, list[Probe], list[str]]:
    """(branches, probes, notes): each victim net's driver resistance and receiver input capacitances to their
    local grounds, as branches of the model, and where to read each receiver's voltage."""
    from ..solver.peec import Branch
    branches, probes, notes = [], [], []
    for net in victims.nets:
        pairs = ends(board, net, victims)
        if not pairs:
            continue
        drv = pairs[0][0]
        nd, gd = pad_node(st, drv, net), ground_node(st, board, drv, ground)
        if nd is None or gd is None:
            notes.append(f"victims: {net}: its driver {drv.pin} or the ground there is not in the model")
            continue
        r = _drive_ohms(board, config, drv)
        branches.append(Branch(nd, gd, lambda f, r=r: complex(r), f"{net} driver {drv.pin}", "part"))
        for _, rx in pairs:
            nr, gr = pad_node(st, rx, net), ground_node(st, board, rx, ground)
            if nr is None or gr is None:
                notes.append(f"victims: {net}: its receiver {rx.pin} or the ground there is not in the model")
                continue
            c_in = config.victims.input_pf * 1e-12
            branches.append(Branch(nr, gr, lambda f, c=c_in: 1.0 / (2j * math.pi * f * c),
                                   f"{net} receiver {rx.pin}", "part"))
            probes.append(Probe(net, drv, rx, nr, gr))
    return branches, probes, notes


def probe_voltage(sol, probe: Probe) -> complex:
    """The voltage across a receiver's input, per A of loop current."""
    return complex(sol.V[probe.rx_node] - sol.V[probe.gnd_node])


def ends(board: Board, net: str, victims: VictimSet) -> list[tuple[Pad, Pad]]:
    """(driver pad, receiver pad) pairs of a victim net. With the schematic's pin types: when the net has input pins
    and a pin that drives (output, or a bidirectional GPIO), that one drives and the inputs receive (a PWM line from
    the microcontroller to a gate driver is read at the driver's input). Otherwise the victim parts' pins receive (a
    reset, a Hall line, a crystal into the microcontroller) and another part's pin drives."""
    pads = [p for fp in board.footprints if not fp.dnp for p in fp.pads if p.net == net]
    if len(pads) < 2:
        return []
    inputs = [p for p in pads if p.pin_type == "input"]
    outputs = [p for p in pads if p.pin_type in DRIVER_TYPES]
    drivers = outputs or [p for p in pads if p.pin_type == "bidirectional"]
    if inputs and drivers:
        return [(drivers[0], r) for r in inputs]
    # a GPIO alone says nothing of the direction: a pin of another part drives (a sensor, a pull-up, a filter)
    drv = outputs[0] if outputs else next((p for p in pads if p.footprint not in victims.parts), pads[0])
    rx = [p for p in pads if p is not drv and p.footprint in victims.parts] or [p for p in pads if p is not drv]
    return [(drv, r) for r in rx]


def facing_capacitance(board: Board, copper, net: str, aggressor: str) -> float:
    """Capacitance (F) between a victim net's tracks and the aggressor net's copper on other layers, where no
    copper layer lies between them over the overlap (parallel plates, no fringe)."""
    stk = board.stackup
    total = 0.0
    for layer in board.copper_layers:
        tracks = [t for t in board.tracks if t.net == net and t.layer == layer]
        if not tracks:
            continue
        victim = unary_union([LineString([t.start, t.end]).buffer(t.width / 2, cap_style=2) for t in tracks])
        lv = stk.layer(layer)
        for other in board.copper_layers:
            agg = copper.get(other, aggressor) if other != layer else None
            if agg is None or agg.is_empty:
                continue
            overlap = victim.intersection(agg)
            lo, hi = sorted((lv.index, stk.layer(other).index))
            for between in stk.copper[lo + 1:hi]:          # shielded where a layer between has copper
                if overlap.is_empty:
                    break
                shield = unary_union(list(copper.planes(between.name).values()))
                if not shield.is_empty:
                    overlap = overlap.difference(shield)
            if not overlap.is_empty:
                h = stk.dielectric_between(layer, other) * 1e-3
                total += EPS0 * stk.epsilon_series(layer, other) * overlap.area * 1e-6 / h   # plates: in series
    return total
