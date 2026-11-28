"""Power-distribution network (PDN) impedance of a supply rail: what its ICs see between their supply and ground pins.

An engineer checks a rail by its impedance against frequency at the IC: below a target Z_t = V·ripple / I_step
[Smith 1999] the
rail holds its voltage within the ripple when the IC's current steps by I_step, whatever the frequency of the step.
Above the target somewhere, a load step there rings the rail: the decaps, planes, vias and the regulator decide
where. (settings `pdn:`, config.PdnSpec; the plan's milestone M1.)

The model: the whole board's copper of the rail and its ground on every layer (the planes, pours, tracks and vias),
with the capacitance between their facing copper (plane pairs, from the stackup), the decaps as branches with their
C, ESR, ESL and mounting inductance (simulate.part_impedance: every part with a model joining the two nets), and the
source (a regulator's output, a connector) as a branch of vrm_r + jω·vrm_l between its rail and ground pads. Each
IC is a port: +1 A spread over its supply pads on the rail (by area, power._area_shares), -1 A over its pads on the
ground; the solve's Σ J·V is then V_rail - V_ground per ampere: the impedance Z(f) the IC sees, with every decap's
share of the current (which part holds the rail at which frequency).

All ports of a rail are solved together at each frequency (PortSet: one factorisation, a few right-hand sides), and
only what is needed is kept (PortSolution: Z per port, the parts' currents), not the field maps. The frequencies:
CE-style log spacing from 10 kHz to 1 GHz, then refined where |Z| bends (a decap's series resonance, an
anti-resonance between two decaps or a decap and the plane), see PdnSetup.refine.

Checks (issues):
- pdn_impedance: |Z| above the target below `check_to` (default 100 MHz [A19]: above it the package and the die's own
  capacitance decouple), with the frequency, the margin and the part holding the rail there;
- decap_missing: a supply pin with no decap pad on the rail within `decap_mm` (default 3 mm).

Validated in tests/test_pdn.py against analytic results: the plane pair's capacitance at the port, one and N equal
decaps (1/(ωNC) below resonance, ESR/N at it), the source's resistance at low frequency.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Optional

import numpy as np

from ..config.settings import PdnSpec
from ..core.board import Board, Footprint, Pad
from ..components.values import format_si
from ..core.issues import Issue, resolve_severity
from ..solver.peec import Branch, RouteExcitation, SolverError
from .simulate import BoardModel, ModelRun, _choose_pitch, part_pairs

F_MIN, F_MAX = 10e3, 1e9          # the sweep
PER_DECADE = 4                   # initial solves per decade (21 over the sweep)
MAX_SOLVES = 40                  # with the refinement
REFINE_DB = 1.0                  # split a gap whose log-log interpolation may be off by more than this (dB)
WARN_DB = 3.0                    # a margin to the target under this is a warning (under 0: an error)
RINGING_SUM = 1.5                # the parts' |I| per A at the IC adding to more: current circulating (not judged)
SHARE_KEEP = 0.01                # a part's share curve kept for the plot and the decap table from this fraction up
USEFUL_SHARE = 0.10              # a decap's useful band: where it carries at least this share of an IC's current
LITTLE_SHARE = 0.05              # a decap that never reaches this share of any IC's current below check_to does little
SHARE_MIN = 0.05                 # a part's share of the port current listed from this fraction up
SUPPLY_TYPES = ("power_in",)     # schematic pin types of an IC's supply pins
IC_PREFIXES = ("U", "IC")        # without pin types on the board: parts with these prefixes and rail + ground pads
CONNECTOR_PREFIXES = ("J", "P", "X", "CN", "CON")
VRM_R_DEFAULT, VRM_L_DEFAULT = 5e-3, 20e-9       # PdnSpec's defaults: a regulator's output
HARNESS_R, HARNESS_L = 20e-3, 500e-9             # a connector's source with those defaults: ~0.5 m of lead [A110]
STEP_FRACTION = 0.5             # an IC's load step: this share of its maximum supply current [A124]
# an IC's maximum supply current by its pin count when it has no Supply Current field [A124]: (most pins, A)
PACKAGE_CURRENT = ((8, 0.02), (20, 0.05), (48, 0.1), (100, 0.25), (256, 0.5), (10**9, 1.5))


def ic_current(fp: Footprint) -> tuple[float, str, str]:
    """An IC's maximum current from its supply rail: its Supply Current field, else an estimate by its pin count
    [A124]. (amperes, how: "field" | "estimate", a note: why a field was not used, or ""). A field that does not
    read, is not above zero or is outside the usual 10 µA … 100 A (a bare "150" meant as mA reads 150 A) is not
    used: the pin-count estimate instead, and the note says so."""
    from ..components.fields import BY_FIELD, read
    r = read(fp, "Supply Current")
    note = ""
    if r is not None:
        p = BY_FIELD["supply current"]
        if isinstance(r.value, float) and p.lo <= r.value <= p.hi:
            return r.value, "field", ""
        note = (f"{fp.reference} Supply Current {r.text.strip()!r} not used ("
                + (r.note or "not a current") + "): estimated by its pin count instead")
    n = len({p.number for p in fp.pads if p.number})
    return next(a for most, a in PACKAGE_CURRENT if n <= most), "estimate", note


def estimate_i_step(parts: list) -> tuple[Optional[float], str, list]:
    """A rail's load step when none is set: STEP_FRACTION of the sum of its ICs' maximum currents (all of them may
    step together: the safe side). `parts`: the ICs only (supply_parts), not connector ports, whose load is off the
    board. (amperes or None without an IC, "fields" when every IC has a Supply Current field else "estimate",
    [{part, amps, how}])."""
    detail = []
    for fp, _rail_pads, _gnd in parts:
        # a regulator fed from the rail (a power_out pin) draws its own load's current, which its pin count says
        # nothing about: left out of the sum, named in the notes (set Load step for such a rail)
        if any(p.pin_type == "power_out" for p in fp.pads):
            detail.append({"part": fp.reference, "amps": 0.0, "how": "regulator", "note": ""})
            continue
        amps, how, note = ic_current(fp)
        detail.append({"part": fp.reference, "amps": amps, "how": how, "note": note})
    counted = [d for d in detail if d["how"] != "regulator"]
    if not counted:
        return None, "", detail
    how = ("fields" if all(d["how"] == "field" for d in counted) else
           "estimate" if all(d["how"] == "estimate" for d in counted) else "partly")
    return STEP_FRACTION * sum(d["amps"] for d in counted), how, detail


DC_SUFFIX = " (PDN DC)"          # a PDN rail's DC run is the DC load "<rail label> (PDN DC)" (its own Results view)


def _rail_ics(board: Board, spec: PdnSpec, ground: str) -> list:
    """The rail's ICs as prepare_pdn takes them (supply_parts, narrowed to spec.parts)."""
    parts = supply_parts(board, spec.rail, ground)
    if spec.parts:
        parts = [t for t in parts if t[0].reference in set(spec.parts)]
    return parts


def _dc_source_pins(board: Board, config, spec: PdnSpec, ground: str) -> tuple[str, str, str]:
    """Where the rail's DC current comes from: (its supply pin "REF.PAD", its return pin or "", in words). The
    source as the PDN finds it: a power stage's output inductor (named, or the stage whose output the rail is), a
    named part, else find_source (a regulator's power_out pin, a switcher's output inductor, a connector). The
    return: the source's own pad on the ground, else (an inductor) the ground pad of the IC driving its other pad
    (the controller's GND); "" when none (the supply side only is modelled then)."""
    src = None
    if spec.source.lower() == "none":
        return "", "", ""
    stage = None
    if spec.source.startswith(STAGE):
        stage = find_stage(board, config, spec.rail, spec.source[len(STAGE):])
    elif not spec.source:
        stage = find_stage(board, config, spec.rail)
    if stage is not None:
        from .power import switch_inductor
        _node, src = switch_inductor(board, config, stage)
    if src is None:
        src = board.footprint(spec.source) if spec.source and not spec.source.startswith(STAGE) else \
            find_source(board, spec.rail, ground)
    if src is None:
        return "", "", ""
    if stage is not None:
        outs = stage_outputs(board, config, stage)[0]
        pad = next((p for p in src.pads if p.net in outs), None)
    else:
        pad = (next((p for p in src.pads if p.net == spec.rail and p.pin_type == "power_out"), None)
               or next((p for p in src.pads if p.net == spec.rail), None))
    if pad is None:
        return "", "", ""
    ret = next((p for p in src.pads if p.net == ground), None)
    if ret is None:                 # an inductor: its controller's ground pin (the IC driving its other pad)
        other = {p.net for p in src.pads if p.net and p.net != pad.net}
        for fp in board.footprints:
            if not fp.dnp and any(p.net in other and p.pin_type == "power_out" for p in fp.pads):
                ret = next((p for p in fp.pads if p.net == ground), None)
                if ret is not None:
                    break
    return f"{src.reference}.{pad.number}", (_pin_of(board, ret) if ret is not None else ""), src.reference


def _pin_of(board: Board, pad: Pad) -> str:
    """A pad as "REF.PAD" (its footprint found by the pad object)."""
    for fp in board.footprints:
        if any(p is pad for p in fp.pads):
            return f"{fp.reference}.{pad.number}"
    return ""


def dc_load_spec(board: Board, config, spec: PdnSpec):
    """The rail's DC run as a DC load (analysis/dcload.py), so its result has the DC maps, densities and busiest
    vias of a DC load: from the rail's source pin (and its return) to every IC's supply pins, each IC drawing its
    maximum current (ic_current: its Supply Current field, else by its pin count) shared over its supply pins and
    returning through its ground pins [A125]. Regulators fed from the rail and connector ports are not loads (their
    current is not known). (DcLoadSpec or None, a reason when None)."""
    from ..config.settings import DcLoad, DcLoadSpec
    ground = spec.ground or (config.reference_nets[0] if config.reference_nets else "GND")
    src, ret, _ = _dc_source_pins(board, config, spec, ground)
    if not src:
        return None, "no source found (set `source`): the DC voltage at the ICs needs one"
    loads = []
    src_ref = src.rsplit(".", 1)[0]
    for fp, rail_pads, gnd_pads in _rail_ics(board, spec, ground):
        outs = {p.net for p in fp.pads if p.pin_type == "power_out" and p.net and p.net != spec.rail}
        if fp.reference == src_ref or any(p.pin_type == "power_out" and p.net == spec.rail for p in fp.pads):
            continue                      # the rail's own source
        if outs:
            # a regulator fed from the rail (an LDO: its input current is its output's load; a switcher's is less,
            # by its efficiency and voltage ratio, so this is the safe side): its output rails' ICs' maximum
            # currents, one level down (their own regulators not followed) [A125]
            amps = sum(ic_current(f)[0] for o in outs for f, *_ in supply_parts(board, o, ground)
                       if not any(p.pin_type == "power_out" for p in f.pads))
            if amps <= 0:
                continue
        else:
            amps, _how, _note = ic_current(fp)
        for k, p in enumerate(rail_pads):
            g = gnd_pads[k % len(gnd_pads)] if ret and gnd_pads else None
            loads.append(DcLoad(f"{fp.reference}.{p.number}", amps / len(rail_pads),
                                f"{fp.reference}.{g.number}" if g is not None else ""))
    if not loads:
        return None, "no IC to load the rail with"
    if not ret or any(not ld.ret for ld in loads):
        ret = ""
        loads = [DcLoad(ld.pin, ld.current, "") for ld in loads]
    return DcLoadSpec(spec.label + DC_SUFFIX, src, loads, source_return=ret), ""


def ic_dc_voltages(res, dc, spec: PdnSpec, board: Board) -> None:
    """Each loaded IC's DC voltage from the rail's DC run (a DcLoadResult): its worst supply pin's voltage
    V_rail − (supply drop + return drop), the drops, the current, and its limit: the IC's Supply Voltage Min field,
    else V·(1 − ripple) [A125]. All of them in res.dc_ics (ICs left out of the impedance ports too: they draw and
    are checked), each port's also as its "dc"; the run's name on res.dc_view."""
    from ..components.fields import read
    res.dc_view = dc.net
    if dc.error or not dc.loads or res.voltage is None:
        res.notes.append(f"DC voltage at the ICs: {dc.error or 'no DC result'}")
        return
    by_part: dict[str, list] = {}
    for ld in dc.loads:
        by_part.setdefault(ld["pin"].rsplit(".", 1)[0], []).append(ld)
    ports = {p["part"]: p for p in res.ports}
    res.dc_ics = []
    for part, lds in by_part.items():
        w = max(lds, key=lambda r: r["drop_mv"])
        fp = board.footprint(part)
        r = read(fp, "Supply Voltage Min") if fp is not None else None
        limit, how = ((r.value, "its Supply Voltage Min") if r is not None and isinstance(r.value, float)
                      else (res.voltage * (1 - spec.ripple), f"{res.voltage:g} V − {spec.ripple * 100:g} %"))
        d = {"part": part, "v": res.voltage - w["drop_mv"] / 1e3, "drop_mv": w["drop_mv"],
             "supply_mv": w["supply_mv"], "return_mv": w["return_mv"], "pin": w["pin"],
             "current_a": sum(x["current_a"] for x in lds), "limit": limit, "limit_how": how,
             "returns": bool(w["ret"]), "xy": (w["x"], w["y"]),
             "regulator": fp is not None and any(p.pin_type == "power_out" for p in fp.pads)}
        res.dc_ics.append(d)
        if part in ports:
            ports[part]["dc"] = d


def dc_issues(res, rules) -> list:
    """pdn_dc_voltage: an IC whose supply pins see less than their limit at DC (from ic_dc_voltages)."""
    out = []
    for d in getattr(res, "dc_ics", ()):
        if d["v"] >= d["limit"]:
            continue
        sev = resolve_severity("pdn_dc_voltage", rules.severity, None)
        if sev == "off":
            continue
        out.append(Issue("pdn_dc_voltage", sev, res.name,
                         f"{res.name}: {d['part']} sees {d['v']:.3f} V at DC ({d['pin']}: {d['drop_mv']:.0f} mV "
                         f"drop, {d['supply_mv']:.0f} mV supply + {d['return_mv']:.0f} mV return at "
                         f"{_s3(d['current_a'], 'A')}), below {d['limit']:.3f} V ({d['limit_how']})",
                         (round(d["xy"][0], 4), round(d["xy"][1], 4)), key=f"pdn:{res.name}:{d['part']}:dc",
                         details={"v": d["v"], "limit": d["limit"], "drop_mv": d["drop_mv"]}))
    return out


def rail_voltage(name: str) -> Optional[float]:
    """A rail's voltage from its name: "+3V3" 3.3, "+12V" 12, "VDD_1V8" 1.8, "/MCU/+5V_ISO" 5, and with a decimal
    point as KiCad's power symbols also write it, "+3.3V" 3.3, "+1.8V" 1.8, "+5.0V" 5; None without one. (Before
    2026-11-12 the decimal form read its digits after the point: "+1.8V" as 8 V, "+5.0V" as 0 V: audit round 12,
    C-1.)"""
    last = name.upper().rsplit("/", 1)[-1]
    m = re.search(r"(?<![\d.])(\d+\.\d+)V(?!\d)", last)          # "3.3V": the point is the decimal point
    if m:
        return float(m.group(1))
    m = re.search(r"(?<![\d.])(\d+)V(\d*)", last)                 # "3V3", "12V": the V is
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2) or 0}")


def _prefix(ref: str) -> str:
    """A reference's letters ("U53" -> "U")."""
    return re.match(r"[A-Za-z_]*", ref).group(0).upper()


def supply_parts(board: Board, rail: str, ground: str) -> list[tuple[Footprint, list[Pad], list[Pad]]]:
    """The ICs on the rail: (part, its supply pads on the rail, its pads on the ground). A part counts with a pad of
    pin type power_in on the rail; a board without pin types: an IC-like reference (U, IC) with pads on both. Parts
    without a ground pad are left out (their return is another part's)."""
    typed = any(p.pin_type for fp in board.footprints for p in fp.pads)
    out = []
    for fp in board.footprints:
        # connectors are not ICs to decouple, even with a power_in pin (refboard's Tag-Connect J41)
        if fp.dnp or _prefix(fp.reference) in CONNECTOR_PREFIXES:
            continue
        on_rail = [p for p in fp.pads if p.net == rail]
        if typed:
            on_rail = [p for p in on_rail if p.pin_type in SUPPLY_TYPES]
        elif _prefix(fp.reference) not in IC_PREFIXES:
            on_rail = []
        gnd = [p for p in fp.pads if p.net == ground]
        if on_rail and gnd:
            out.append((fp, on_rail, gnd))
    return out


def rails_with_ics(board: Board, ground: str) -> dict[str, list[str]]:
    """Every net that ICs draw supply from (a power_in pad on it and a pad on `ground`; connectors left out), with
    those ICs: {net: [refs]}, in one pass over the pads (fast enough for the window's thread). The Power integrity
    tab's Find rails."""
    out: dict[str, list[str]] = {}
    for fp in board.footprints:
        if fp.dnp or _prefix(fp.reference) in CONNECTOR_PREFIXES or not any(p.net == ground for p in fp.pads):
            continue
        for net in {p.net for p in fp.pads if p.pin_type in SUPPLY_TYPES and p.net and p.net != ground}:
            out.setdefault(net, []).append(fp.reference)
    return dict(sorted(out.items(), key=lambda kv: (-len(kv[1]), kv[0])))


STAGE = "stage:"                 # a PdnSpec source naming a power stage: "stage:conv1"
MAX_KEYS = 8                     # key frequencies (peaks, the worst point) where each decap's own impedance is solved
MIN_GAIN_DB = 1.0                # a fix must lower the highest |Z| near its point by at least this
MAX_CERAMIC = 47e-6              # the largest ceramic a fix suggests (a bigger one is no part of that ESL)
SPOT_SPAN = 10.0                 # a fix's spot is judged over this factor either side of its point
MIN_CARRIED = 0.02               # a decap carrying less of every IC's current at the key frequencies is not driven
MAX_DECAP_SOLVES = 30           # at most this many decaps get their own solve there (the ones carrying most current)
STEP_FIT_ABOVE = 3.0            # the step's fit takes the solved |Z| up to this many times check_to [A127]
LATE_TAUS = 10.0                 # a port step's "late" dip: after this many time constants of the check_to low-pass
STAGE_F_LOW = 30.0               # with a power stage as the source the sweep starts at its crossover / this ...
F_MIN_STAGE = 100.0              # ... but not below this


def stage_outputs(board: Board, config, spec) -> tuple[set, str, str]:
    """A power stage's output nets (loop.output_nets: the inductor's far side and nets joined to it through shunts),
    the net its load hangs on and that connector's pins as a PDN port ("J2.2/J2.1", "" without one). The load's
    net: the output connector's + pin's; else the one the feedback divider senses (what the loop holds); else the
    output net with the most capacitors to ground; else the inductor's far side."""
    from .loop import find_loop, output_nets
    from .power import switch_inductor
    from .safety import is_ground
    outs = output_nets(board, config, spec)
    if not outs:
        return set(), "", ""
    port, rail = "", ""
    oc = list(spec.output_connector)
    if len(oc) == 2 and all("." in p for p in oc):
        ref, pin = oc[0].rsplit(".", 1)
        fp = board.footprint(ref)
        pad = next((p for p in fp.pads if p.number == pin), None) if fp is not None else None
        if pad is not None and pad.net in outs:
            rail, port = pad.net, f"{oc[0]}/{oc[1]}"
    if not rail:
        parts = find_loop(board, config, spec)
        for ref, *_ in (parts.top if parts is not None else []):
            fp = board.footprint(ref)
            rail = next((p.net for p in (fp.pads if fp is not None else ()) if p.net in outs), "")
            if rail:
                break
    if not rail:
        by_net = board.parts_by_net()
        count = {n: sum(1 for fp in by_net.get(n, ()) if (m := config.model_for(fp.reference)) is not None
                        and m.kind == "cap" and any(is_ground(p.net) for p in fp.pads)) for n in outs}
        if max(count.values(), default=0):
            rail = max(sorted(count), key=count.get)
    if not rail:
        node, ind = switch_inductor(board, config, spec)
        far = [p.net for p in ind.pads if p.net and p.net != node] if ind is not None else []
        rail = far[0] if far and far[0] in outs else sorted(outs)[0]
    return outs, rail, port


def converter_rails(board: Board, config) -> dict[str, tuple[str, str]]:
    """The rails the board's power stages feed: {net: (stage name, its output connector as a port or "")} (enabled
    stages, not a motor bridge's legs; a stage whose output can't be found left out). The Power integrity tab's
    Find rails lists them beside the rails ICs draw from (the user: "Why are converters not listed under Power
    integrity"); each stage's own output nets also count as its rail for the source (find_stage)."""
    out: dict[str, tuple[str, str]] = {}
    for spec in config.converters:
        if not spec.enabled or spec.bridge or spec.topology not in ("buck", "boost"):
            continue
        _, rail, port = stage_outputs(board, config, spec)
        if rail and rail not in out:
            out[rail] = (spec.name, port)
    return out


def find_stage(board: Board, config, rail: str, name: str = ""):
    """The power stage feeding a rail: the one named (a "stage:NAME" source), else the first enabled stage whose
    output nets hold the rail; None without one."""
    for spec in config.converters:
        if name and spec.name != name:
            continue
        if not spec.enabled or spec.bridge:
            continue
        if name or rail in stage_outputs(board, config, spec)[0]:
            return spec
    return None


def switcher_inductor(board: Board, rail: str) -> Optional[Footprint]:
    """A switching regulator's output inductor on the rail: a two-pad part with one pad on the rail and the other on
    a net an IC drives through a power_out pin (its SW pin: a buck converter whose controller's output pin is the
    switch node, not the rail). None without one."""
    driven = {p.net for fp in board.footprints if not fp.dnp for p in fp.pads
              if p.pin_type == "power_out" and p.net and p.net != rail}
    for fp in board.footprints:
        nets = [p.net for p in fp.pads if p.net]
        if not fp.dnp and _prefix(fp.reference) == "L" and len(set(nets)) == 2 and rail in nets \
                and any(n in driven for n in nets if n != rail):
            return fp
    return None


def find_source(board: Board, rail: str, ground: str) -> Optional[Footprint]:
    """The part feeding the rail: one with a power_out pin on it (a regulator's output), else a switching
    regulator's output inductor (switcher_inductor: its controller's power_out pin is the switch node), else a
    connector with pads on the rail and the ground; None when none is there."""
    for fp in board.footprints:
        if not fp.dnp and any(p.net == rail and p.pin_type == "power_out" for p in fp.pads):
            return fp
    ind = switcher_inductor(board, rail)
    if ind is not None:
        return ind
    for fp in board.footprints:
        nets = {p.net for p in fp.pads}
        if not fp.dnp and _prefix(fp.reference) in CONNECTOR_PREFIXES and rail in nets and ground in nets:
            return fp
    return None


def decaps(board: Board, config, rail: str, ground: str) -> list[Footprint]:
    """The capacitors between the rail and the ground (parts with a cap model and pads on both nets)."""
    out = []
    for fp in board.footprints:
        model = config.model_for(fp.reference)
        if model is None or model.kind != "cap" or fp.dnp:
            continue
        nets = {p.net for p in fp.pads}
        if rail in nets and ground in nets:
            out.append(fp)
    return out


@dataclass
class PortSolution:
    """What a PDN solve keeps at one frequency: per port the impedance (complex, Ω) and the branch currents (per A of
    the port's current; the stack's branch order)."""
    f: float
    Z: np.ndarray
    I_branch: np.ndarray
    # at the key frequencies only (PdnSetup.refine's second phase): each decap's driving-point impedance across its
    # own two terminals with everything in place, {ref: Z_kk} (the rank-one "without it" / "with one more" figures)
    Zd: Optional[dict] = None


class PortSet:
    """Several ports on one model, solved together at each frequency (the model's factorisation is shared; one
    right-hand side each). Duck-types the excitation a ModelRun holds (`stack`, `solve`). At the frequencies in
    `decap_at` it also drives each decap's own terminals (decap_excs): 1 A across the part, the impedance there."""

    def __init__(self, stack, ports: list[list[tuple]]):
        """`ports`: each port's injections [(node, A)], +1 A in at the supply pads, -1 A out at the ground pads."""
        self.stack = stack
        self.excs = [RouteExcitation(stack, [], injections=inj) for inj in ports]
        self.decap_excs: dict = {}          # {ref: RouteExcitation} across each decap's terminals (set_decaps)
        # {f: the decaps to drive there (None: all)}: the key frequencies (all decaps) and, for the spots where a fix
        # would go, every frequency of the band (PdnSetup.refine's phases)
        self.want: dict = {}
        self.last: dict = {}                # {f: PortSolution}: the ports' solve, reused when f comes again

    @property
    def decap_at(self) -> set:
        """The frequencies with decaps to drive."""
        return set(self.want)

    def set_decaps(self, terminals: dict) -> None:
        """Each decap's two terminals {ref: (node a, node b)} (its part branch's ends: the capacitor's own metal), as
        an excitation of +1 A into a, −1 A out of b."""
        self.decap_excs = {ref: RouteExcitation(self.stack, [], injections=[(a, 1.0), (b, -1.0)])
                           for ref, (a, b) in terminals.items()}

    def solve(self, f: float) -> PortSolution:
        """Every port at f (SolverError when one fails); at a key frequency also every decap's own impedance (a
        decap whose solve fails is left out of Zd)."""
        sol = self.last.get(f)
        if sol is None:
            sols = [e.solve(f) for e in self.excs]
            sol = PortSolution(f, np.array([s.Z_loop for s in sols], complex),
                               np.array([s.I_branch for s in sols], complex))
            self.last[f] = sol
        if f in self.want:
            refs = self.want[f]
            zd = dict(sol.Zd or {})
            for ref, e in self.decap_excs.items():
                if ref in zd or (refs is not None and ref not in refs):
                    continue                    # solved already, or not wanted here
                try:
                    zd[ref] = complex(e.solve(f).Z_loop)
                except SolverError:
                    continue
            sol = replace(sol, Zd=zd)
            self.last[f] = sol
        return sol


@dataclass
class PdnResult:
    """A rail's PDN (see the module): per IC port its Z(f) and what holds it, the decaps, the target and the source."""
    name: str
    rail: str
    ground: str
    kind: str = "pdn"
    voltage: Optional[float] = None
    target_ohm: Optional[float] = None
    i_step: Optional[float] = None      # A: the load step the target and the step response use
    i_step_source: str = ""             # "set" (the spec's), "fields" (the ICs' Supply Current), "estimate",
    #                                     "partly" (some fields, some pin counts)
    i_step_detail: list = field(default_factory=list)   # [{part, amps, how}] behind an estimated step
    check_to: float = 100e6
    ripple: float = 0.05                # the allowed deviation, a fraction of the voltage (the spec's)
    dc_view: str = ""                   # the rail's DC run (a DC load view "<name> (PDN DC)"), dc_ics from it
    dc_ics: list = field(default_factory=list)      # [{part, v, drop_mv, limit, ...}] every IC it loads
    left_out: list = field(default_factory=list)    # [{part, pads: [[x, y]]}] ICs not solved as ports (off the grid)
    source: str = ""                  # the source part and its model, in words
    stage: str = ""                     # a power stage as the source (its closed loop: analysis/load_step.py)
    f_min: float = F_MIN                # the sweep's lowest frequency (lower with a power stage's loop)
    ports: list = field(default_factory=list)       # [{part, pins, f, z_ohm, phase_deg, worst, peaks, l_nh, holds}]
    responses: list = field(default_factory=list)   # each IC's Z(f) with its poles and zeros (analysis/transfer.py)
    decaps: list = field(default_factory=list)      # [{ref, value, C, near: [{part, mm}]}]
    worst: Optional[dict] = None        # the worst port's {part, f, z_ohm, margin_db, holds}
    notes: list = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0
    nodes: int = 0
    pitch_mm: float = 0.0

    @property
    def net(self) -> str:
        """Its name where the results list views by net."""
        return self.name

    def label(self) -> str:
        """Its name in lists."""
        return f"{self.name}  PDN"

    def summary(self) -> dict:
        """As plain data for the JSON report (the responses as their own summaries)."""
        d = asdict(replace(self, responses=[]))
        d["responses"] = [r.summary() for r in self.responses]
        return d


class PdnSetup:
    """A rail's run for the solver's scheduler (simulate.solve_setups): the ports' run as an extra run, refined by
    `refine`; `fine` is the same model with nothing planned (the scheduler refines only extra runs: the pattern of
    a power stage's conducted run). Duck-types what solve_setups and the memory bookkeeping read of a setup."""
    model_label = "PDN"

    def __init__(self, spec: PdnSpec, res: PdnResult, run: ModelRun, ports: list[dict], branches: list[Branch],
                 build):
        """`ports`: [{part, pins, xy, pads}] in the run's order; `branches`: the model's branches (names: parts)."""
        self.spec, self.res, self.ports, self.branches, self.build = spec, res, ports, branches, build
        self.fine = ModelRun(run.build, run.exc, planned=[])
        # the ports' run (refined), and each IC's two regional models at LOCAL_F (not refined)
        self.extra_runs = [run] + [r for m in ports if m.get("local") for pair in m["local"] for r in pair]
        self.run = run
        self.cavity, self.spectrum_run = None, None
        self.issues: list[Issue] = []
        self.t_start = time.time()
        self.phase = 0                      # refine's phases: 0 the sweep, 1 the key frequencies, 2 the spots
        self.keys: list = []                # the key frequencies (phase 1: every decap driven there)
        self.spot_of: dict = {}             # {(port, f): ref}: where a fix for that peak / worst point would go

    def refine(self, run, config) -> list:
        """One more frequency where the worst port's |Z| bends most (a resonance between solves), or [] when every
        gap is resolved to REFINE_DB or MAX_SOLVES were made."""
        if run is not self.run:
            return []                       # a regional model: its LOCAL_F only
        sols = run.ok()
        nxt = [] if len(run.sols) >= MAX_SOLVES or len(sols) < 3 else self._next_frequency(sols)
        if nxt or self.phase >= 2 or not getattr(run.exc, "decap_excs", None):
            return nxt
        if self.phase == 1:
            # phase 2: the spots where a fix would go (spot_parts: per port near or over its target, the part
            # holding the rail above each such peak or at its worst point), driven at every frequency of the band,
            # so a fix is judged over the whole curve (a new peak it opens between the key frequencies) [A128]
            self.phase = 2
            self.spot_of = spot_parts(sols, _names(self.branches), self.keys, self.res.target_ohm,
                                      self.spec.check_to, set(run.exc.decap_excs))
            # each spot over a decade either side of its point (within the band): a part added there changes the curve
            # near its own resonance and the new anti-resonance it makes with its neighbour, within about that [A128];
            # the whole band cost 52 more solves on a 2-IC rail against the sweep's 80
            want: dict = {}
            for (_p, f0), ref in self.spot_of.items():
                for f in sols:
                    if f <= self.spec.check_to and f0 / SPOT_SPAN <= f <= f0 * SPOT_SPAN and f not in self.keys:
                        want.setdefault(f, set()).add(ref)
            if not want:
                return []
            run.exc.want.update(want)
            return sorted(want)
        # phase 1: the sweep is resolved: its key frequencies again, now with every decap's own impedance (PortSet)
        self.phase = 1
        keys = key_frequencies(sols, self.spec.check_to)
        self.keys = keys
        names = _names(self.branches)

        def carried(ref):
            """The part's largest |I| per A at a port over the key frequencies."""
            idx = [i for i, n in enumerate(names) if n == ref]
            return max((abs(complex(np.sum(sols[f].I_branch[p][idx]))) for f in keys
                        for p in range(len(sols[f].Z))), default=0.0)
        # a decap carrying under MIN_CARRIED of every port's current there is not driven: taking it out moves |Z| by
        # about that share at most (its "without" is that bound, _cap_effects); the others, the MAX_DECAP_SOLVES
        # carrying most (a board's dozens of 100 nF)
        self.carried = {ref: carried(ref) for ref in run.exc.decap_excs}
        keep = sorted((r for r in run.exc.decap_excs if self.carried[r] >= MIN_CARRIED),
                      key=lambda r: -self.carried[r])[:MAX_DECAP_SOLVES]
        run.exc.decap_excs = {r: run.exc.decap_excs[r] for r in keep}
        for f in keys:
            run.exc.want[f] = None
        return keys

    def _next_frequency(self, sols: dict) -> list:
        """The refinement's next frequency ([] when every gap is resolved)."""
        fs = np.array(sorted(sols))
        z = np.max(np.abs(np.array([sols[f].Z for f in fs])), axis=1)      # the worst port at each frequency
        lx, ly = np.log10(fs), 20 * np.log10(np.maximum(z, 1e-12))
        slope = np.diff(ly) / np.maximum(np.diff(lx), 1e-12)
        best = None
        for g in range(len(fs) - 1):
            if fs[g + 1] / fs[g] < 1.03:
                continue
            bends = [abs(slope[g] - slope[k]) for k in (g - 1, g + 1) if 0 <= k < len(slope)]
            unc = max(bends) * (lx[g + 1] - lx[g]) / 8
            if unc >= REFINE_DB and (best is None or unc > best[0]):
                best = (unc, math.sqrt(fs[g] * fs[g + 1]))
        return [float(best[1])] if best else []

    def solve_limit(self, run) -> int:
        """The most solves `run` takes with refine (the progress count, simulate.solve_limit): the ports' run up to
        MAX_SOLVES, a regional model its LOCAL_F."""
        # + the key frequencies again (phase 1) and up to every solved frequency once more round the spots (phase 2)
        return max(len(run.planned), MAX_SOLVES) * 2 + MAX_KEYS if run is self.run else len(run.planned)

    def issue(self, rules, check, message, loc, severity=None, **kw):
        """Add an issue for this rail (unless its check is switched off)."""
        sev = resolve_severity(check, rules.severity, severity)
        if sev != "off":
            self.issues.append(Issue(check, sev, self.spec.label, message, (round(loc[0], 4), round(loc[1], 4)),
                                     **kw))


def _source_branch(st, src: Footprint, rail: str, ground: str, spec: PdnSpec) -> tuple[list[Branch], str]:
    """The source as branches of vrm_r + jω·vrm_l between its rail and ground pads (spread over them like a part),
    and its description; a source without a ground pad joins the nearest ground copper."""
    rail_pads = [p for p in src.pads if p.net == rail]
    gnd_pads = [p for p in src.pads if p.net == ground]
    r, l_ = spec.vrm_r, spec.vrm_l
    # a connector (no power_out pin on the rail) has a harness and the supply behind it, not a regulator's output:
    # with the regulator's defaults left as they are, a lead's instead [A110] (before 2026-11-12 the regulator's
    # 20 nH: the impedance below the decaps' range up to ~30 dB low: audit round 12, C-5)
    harness = (_prefix(src.reference) in CONNECTOR_PREFIXES
               and not any(p.net == rail and p.pin_type == "power_out" for p in src.pads)
               and r == VRM_R_DEFAULT and l_ == VRM_L_DEFAULT)
    if harness:
        r, l_ = HARNESS_R, HARNESS_L

    def z(f, r=r, l_=l_):
        """The source's output impedance."""
        return complex(r, 2 * math.pi * f * l_)
    pairs = part_pairs(st, rail_pads, gnd_pads) if gnd_pads else []
    if not pairs:
        p = rail_pads[0]
        radius = max(3 * st.d, 2.0)
        na = next((n for n in (st.node(l, *p.position, rail, radius) for l in p.layers) if n), None)
        nb = next((n for n in (st.node(l, *p.position, ground, radius) for l in st.names) if n), None)
        pairs = [(na, nb)] if na and nb and na != nb else []
    if not pairs:
        raise SolverError(f"the source {src.reference}: no {rail} / {ground} copper at its pads")
    n = len(pairs)
    out = [Branch(a, b, z if n == 1 else (lambda f, z=z, n=n: n * z(f)), f"source {src.reference}", "part")
           for a, b in pairs]
    what = " (a switching regulator's output inductor)" if _prefix(src.reference) == "L" else ""
    return out, (f"{src.reference} ({src.value}){what} as {r * 1e3:g} mΩ + {l_ * 1e9:g} nH (vrm_r, vrm_l"
                 + ("; a connector: a harness's defaults, give the supply's own values" if harness else "") + ")")


def _stage_branch(st, board: Board, config, stage, rail: str, ground: str) -> tuple[list[Branch], str, float]:
    """A power stage as the rail's source: its closed loop as the admittance Y_conv(s) (loop.LoopModel, or the
    approximate loop of its bandwidth and phase margin: load_step.converter_model) from where its feedback divider
    senses the output (the divider's top resistor's pad on an output net: the loop holds that node; else the
    inductor's output pad) to the nearest ground copper, the branch's impedance 1/Y_conv(j2πf) [A117]. Its output
    capacitors are the model's own part branches (decaps), so they are not in it. A sense shunt between the
    inductor and the divider is inside the loop: at the inductor's pad its resistance read as a droop the loop
    takes out (10 mΩ: 10 mV per A on the test board). (branches, its description, the loop's crossover in Hz or 0);
    SolverError without a model or without copper there."""
    from .load_step import _crossover, converter_model
    from .loop import find_loop
    from .power import switch_inductor
    model, _, err = converter_model(board, config, stage, stage.i_out)
    if model is None:
        raise SolverError(f"{stage.name}: {err}")
    outs = stage_outputs(board, config, stage)[0]
    # where to attach, best first: the divider's top pad (the sensed node), the output capacitors' pads on the
    # sensed net, the inductor's output pad; the first whose grid node lies on its net's main copper (a divider's
    # 0402 pad on a thin track was its own piece on a 0.37 mm grid: the source read 7 Ω at 1 kHz)
    cands = []
    parts = find_loop(board, config, stage)
    sensed = set()
    for ref, *_ in (parts.top if parts is not None else []):
        fp = board.footprint(ref)
        for p in (fp.pads if fp is not None else ()):
            if p.net in outs:
                cands.append((p, f"{ref} (its feedback divider)"))
                sensed.add(p.net)
    caps = [fp for fp in decaps(board, config, next(iter(sensed)), ground)] if sensed else []
    cands += [(p, f"{fp.reference} (an output capacitor)") for fp in caps for p in fp.pads if p.net in sensed]
    node, ind = switch_inductor(board, config, stage)
    if ind is not None:
        cands += [(p, ind.reference) for p in ind.pads if p.net and p.net != node]
    piece, main = copper_pieces(st, st.plane)
    radius = max(3 * st.d, 2.0)
    na = nb = None
    where = ""
    for pad, where in cands:
        na = next((n for n in (st.node(l, *pad.position, pad.net, radius) for l in pad.layers) if n), None)
        nb = next((n for n in (st.node(l, *pad.position, ground, radius) for l in st.names) if n), None)
        if na and nb and na != nb and piece[st.plane.nid[na]] == main.get(pad.net) \
                and piece[st.plane.nid[nb]] == main.get(ground):
            break
    else:
        raise SolverError(f"{stage.name}: no {'/'.join(sorted(outs))} and {ground} copper at its feedback divider, "
                          "output capacitors or inductor on this model's grid")
    y_conv = model.y_conv

    def z(f, y=y_conv):
        """The stage's closed-loop source impedance 1/Y_conv at f."""
        return complex(1.0 / y(2j * math.pi * f))
    fc = _crossover(model.t, stage.f_sw) or 0.0
    text = (f"{stage.name} in closed loop at {where} ({'approximate loop, ' if model.approximate else ''}"
            f"crossover {fc / 1e3:.3g} kHz)" if fc else f"{stage.name} in closed loop at {where}")
    return [Branch(na, nb, z, f"source {stage.name}", "part")], text, fc


def _port_pads(board: Board, port: str, rail: str, ground: str):
    """A port "J2.2/J2.1" as (footprint, [its pad on the rail], [its pad on the ground]), or a reason it can't be."""
    try:
        a, b = (p.strip() for p in port.split("/"))
        ref, pa = a.rsplit(".", 1)
        ref_b, pb = b.rsplit(".", 1)
    except ValueError:
        return f"port {port!r}: write it as supply / return pins, J2.2/J2.1"
    fp, fb = board.footprint(ref), board.footprint(ref_b)
    if fp is None or fb is None or fp is not fb:
        return f"port {port!r}: both pins on one part on the board"
    rp = [p for p in fp.pads if p.number == pa and p.net == rail]
    gp = [p for p in fp.pads if p.number == pb and p.net == ground]
    if not rp or not gp:
        return f"port {port!r}: {a} must be on {rail} and {b} on {ground}"
    return fp, rp, gp


def copper_pieces(st, plane) -> tuple[np.ndarray, dict]:
    """The model's pieces of copper joined by copper (grid strips, vias, tracks bridged, pins; not parts and not
    the planes' capacitance): a piece number per node, and per net the piece holding most of its nodes (its main
    copper). A pad landing elsewhere is cut off from its net in this model: a pad smaller than a cell whose thin
    track the grid did not join (a QFN's 0.25 mm pads on a 0.86 mm whole-board grid: refboard's U55 read
    1.7 MΩ at 1 MHz, the stray capacitance of its pads), or a real break in the copper."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = plane.n_nodes
    keep = np.ones(plane.nb, bool)
    keep[plane.ns: plane.ns + plane.ncap] = False                     # the plane pairs' capacitance
    for k, b in enumerate(plane.branches):
        if b.kind == "part":                                          # decaps, the source
            keep[plane.ns + plane.ncap + k] = False
    g = coo_matrix((np.ones(int(keep.sum())), (plane.a_nodes[keep], plane.b_nodes[keep])), shape=(n, n))
    _, label = connected_components(g, directed=False)
    main: dict[str, int] = {}
    for k, grid in enumerate(st.grids):
        if grid.label is None:
            continue
        for ni, net in enumerate(grid.nets):
            ids = plane.nid[k][grid.label == ni]
            ids = ids[ids >= 0]
            if len(ids):
                counts = main.setdefault(net, {})
                for comp, c in zip(*np.unique(label[ids], return_counts=True)):
                    counts[int(comp)] = counts.get(int(comp), 0) + int(c)
    return label, {net: max(c, key=c.get) for net, c in main.items() if c}


LOCAL_F = (30e6, 100e6, 300e6)  # the per-IC correction's frequencies (inductive: above the decaps' resonances)
LOCAL_PROBE_F = 100e6            # the board-wide solve that tells which decaps carry each port's current there ...
LOCAL_SHARE = 0.9                # ... the decaps carrying this fraction of it are in the port's regional model ...
LOCAL_SHARE_NOTE = 0.6           # a loop model holding less of it (its region kept small enough to be fine): a note
LOCAL_NEAR_MM = 9.0              # ... and every decap within this of its supply pads (alone without the probe) ...
LOCAL_MARGIN_MM = 3.0            # ... with at least this much copper around their pads ...
LOCAL_MARGIN_CELLS = 10          # ... and at least this many cells of the coarser pitch of the pair
LOCAL_COARSEST = 0.75            # a regional "fine" pitch must stay under this fraction of the board-wide one
LOCAL_LEVEL = 1.05               # the loop's fine pitch above this × the pins' one: the pins get their own region
LOCAL_SPREAD = 0.2               # the correction varying over LOCAL_F by more than this fraction (and ...
LOCAL_SPREAD_NH = 0.05           # ... this many nH) is not one inductance: said in a note
# The regional models are solved without the planes' capacitance [A118]: their difference is then the inductance the
# coarse grid gets wrong along the port's loop alone. With it, a decap of high ESL in the region (a polymer
# capacitor's 8 nH by a connector) resonated with the region's plane capacitance between LOCAL_F, and the two
# pitches' small difference in that capacitance read as up to 2.5 nH of "inductance" (refboard's output connector:
# 1.14 nH at 40000 nodes, 1.50 nH at 80000).
LOCAL_CAPACITANCE = False


def probe_shares(exc: "PortSet", branches: list[Branch], refs: set, f: float = LOCAL_PROBE_F) -> tuple:
    """Each port's decap currents on the board-wide model at f: (the solution, [{ref: |I| per ampere of the port}]
    per port), the parts' parallel branches added (as finish_pdn's "holds"); (None, []) when the solve fails."""
    try:
        sol = exc.solve(f)
    except (SolverError, MemoryError):
        return None, []
    names = _names(branches)
    idx = {r: [k for k, n in enumerate(names) if n == r] for r in refs}
    shares = []
    for p in range(len(sol.Z)):
        I = sol.I_branch[p]
        shares.append({r: abs(complex(np.sum(I[ks]))) for r, ks in idx.items() if ks and len(I) > max(ks)})
    return sol, shares


def _pad_extent(pads: list) -> list[tuple]:
    """The corners of the pads' copper (their positions ± half their size, either way round): a region drawn around
    them holds the whole pad, not only its centre (a polymer capacitor's pads are 4.4 mm long)."""
    pts = []
    for p in pads:
        r = max(p.size) / 2 if getattr(p, "size", None) else 0.0
        x, y = p.position
        pts += [(x - r, y - r), (x + r, y + r)]
    return pts


def _local_runs(model: BoardModel, st, spec: PdnSpec, ground: str, fp: Footprint, rp: list, gp: list,
                caps: list, shares: Optional[dict] = None) -> tuple:
    """The regional models of one port (an IC's supply and ground pads, or a connector's), in pairs (fine, coarse)
    solved at LOCAL_F with its current; the sum of each pair's difference is what the board-wide model's coarse grid
    gets wrong along the port's loop at high frequency (small pads, vias, escapes, the copper to the decaps: on
    refboard's +3V3 0.04-0.9 nH; the test board's coarsest grid read -43 % at 100 MHz without it), added to the
    port's board-wide curve in finish_pdn [A118].

    Two scales, two regions (a telescoping sum: Z ≈ Z_board(d) + [Z_loop(d1) - Z_loop(d)] + [Z_pins(d2) - Z_pins(d1)]):
    - the loop: the port's pads and the decaps that close its loop there, those carrying LOCAL_SHARE of its decap
      current on the board-wide model at LOCAL_PROBE_F (`shares`, probe_shares) and every decap within LOCAL_NEAR_MM
      of its supply pads, max(LOCAL_MARGIN_MM, LOCAL_MARGIN_CELLS × d) round their pads (the coarse grid's error in
      the planes' spread reaches several of its cells: with 3 mm, 2.4 cells, a test connector read 4 % high); at its
      fine pitch d1 and at the board-wide pitch d on the board-wide grid's own lattice. It takes the grid's error
      along the copper to wherever the current closes.
    - the pins: the port's pads and the near decaps alone (LOCAL_MARGIN_CELLS × d1 round them), at the finest pitch
      d2 and at d1 on the loop model's lattice, when the loop's region is large enough that d1 is above LOCAL_LEVEL
      × d2. The error between d1 and d2 is at small features (pads, vias), so a small region holds it.
    Until 2026-11-15 only the pins' region, against the board-wide pitch: by refboard's output connector the one
    near decap was a polymer capacitor (8 nH) while the current closed through ceramics 15-20 mm away, and the
    correction, measured on a loop the board-wide model does not have, moved with the grid (0.08 nH at 40000 nodes,
    0.43 nH at 80000). When the far decaps would make the loop's region so large that d1 is no longer fine
    (LOCAL_COARSEST of d), those with the smallest share are left out first.

    ([(fine run, coarse run), ...], {"refs": the loop's decaps, "share": their fraction of the probe's decap current
    or None, "pins": the pins' region's decaps or None, "pitch": the finest pitch}), or (None, why)."""
    from .power import _area_shares, _side
    board, a = model.board, model.config.analysis
    by_ref = {c.reference: c for c in caps}
    near = []
    for c in caps:
        cp = [p for p in c.pads if p.net in (spec.rail, ground)]
        if any(math.dist(q.position, s.position) <= LOCAL_NEAR_MM for q in cp if q.net == spec.rail for s in rp):
            near.append(c.reference)
    # the probe's decaps by their share of the port's current, the largest first, up to LOCAL_SHARE of it
    far, total = [], sum(shares.values()) if shares else 0.0
    if total > 0:
        got = sum(shares.get(r, 0.0) for r in near)
        for ref, i in sorted(shares.items(), key=lambda t: -t[1]):
            if got >= LOCAL_SHARE * total:
                break
            if ref not in near and ref in by_ref:
                far.append(ref)
                got += i
    if not near and not far:
        # without a decap the port's loop does not close in the region (its planes' capacitance is left out of the
        # regional models, LOCAL_CAPACITANCE)
        return None, (f"no decap within {LOCAL_NEAR_MM:g} mm" if total <= 0 else "no decap carries its current")
    d = st.d
    layers = list(st.names)
    h = min((board.stackup.dielectric_between(x, y) for x, y in zip(layers, layers[1:])), default=0.2)

    def region_of(refs, x0, y0, pitch):
        """The region round the port's pads and these decaps' pads on the rail and ground, max(LOCAL_MARGIN_MM,
        LOCAL_MARGIN_CELLS cells of `pitch`) round them, snapped outwards to the lattice x0 + k·pitch, y0 + k·pitch;
        and its fine pitch (analysis.max_nodes, the dielectric height)."""
        margin = max(LOCAL_MARGIN_MM, LOCAL_MARGIN_CELLS * pitch)
        pts = _pad_extent(rp + gp)
        for r in refs:
            pts += _pad_extent([p for p in by_ref[r].pads if p.net in (spec.rail, ground)])
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]

        def snap(v, o, up):
            """v on the lattice o + k·pitch (down, or up)."""
            k = (v - o) / pitch
            return o + (math.ceil(k - 1e-9) if up else math.floor(k + 1e-9)) * pitch
        reg = (snap(min(xs) - margin, x0, False), snap(min(ys) - margin, y0, False),
               snap(max(xs) + margin, x0, True), snap(max(ys) + margin, y0, True))
        return reg, _choose_pitch(h, reg, len(layers), a.max_nodes, "auto", a.resolution)
    g0 = st.grids[0]
    # the loop's region: the far decaps with the smallest share leave first while its pitch is too coarse to be fine
    while True:
        refs = near + far
        region, d1 = region_of(refs, g0.x0, g0.y0, d)
        if d1 < LOCAL_COARSEST * d or not far:
            break
        far.pop()
    if not refs:
        return None, "the decaps carrying its current are too far apart for a fine regional model"
    if d1 >= LOCAL_COARSEST * d:
        return None, "the board-wide grid is already about as fine"
    share = (sum(shares.get(r, 0.0) for r in refs) / total) if total > 0 else None

    def pair(reg, fine, coarse, what):
        """The port on this region at the two pitches: (fine run, coarse run); SolverError when its loop does not
        close on either grid (loop_closes)."""
        runs = []
        for pitch in (fine, coarse):
            rs = model.build(reg, layers, {spec.rail, ground}, pitch, LOCAL_CAPACITANCE)
            inj = _area_shares(rs, rp, _side(fp), 1.0) + _area_shares(rs, gp, _side(fp), -1.0)
            if not loop_closes(rs.plane, inj):
                raise SolverError(f"its pads do not reach its decaps on the {what} model's {pitch:.2f} mm grid")
            runs.append(ModelRun(rs, PortSet(rs.plane, [inj]), planned=list(LOCAL_F)))
        return runs[0], runs[1]
    try:
        pairs = [pair(region, d1, d, "loop")]
    except SolverError as e:
        return None, str(e)
    pins, finest = None, d1
    if near:
        # the pins' region on the loop model's fine lattice (its pitch d1 there is the loop model's own grid)
        g1 = pairs[0][0].build.grids[0]
        reg2, d2 = region_of(near, g1.x0, g1.y0, d1)
        if d1 > LOCAL_LEVEL * d2:
            try:
                pairs.append(pair(reg2, d2, d1, "pins'"))
                pins, finest = sorted(near), d2
            except SolverError:
                pass                    # the loop's level alone (its pitch d1 is still finer than the board's)
    return pairs, {"refs": sorted(refs), "share": share, "pins": pins, "pitch": finest}


def loop_closes(plane, inj: list) -> bool:
    """Whether a port's current can flow round on this model without the planes' capacitance: every node it enters
    by joined to a node it leaves by through copper (strips, vias, tracks) and parts (the decaps). A regional model
    without capacitance whose port pads do not reach its decaps (a small pad on a coarse grid, a pour cropped by the
    region) is open: its solve would not converge. (One of refboard's ICs on a 1.15 mm regional grid read as a
    capacitor, -343 nH at 30 MHz, through the planes' capacitance alone, while those were in the regional models.)"""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = plane.n_nodes
    keep = np.ones(plane.nb, bool)
    keep[plane.ns: plane.ns + plane.ncap] = False                     # the planes' capacitance (none here)
    g = coo_matrix((np.ones(int(keep.sum())), (plane.a_nodes[keep], plane.b_nodes[keep])), shape=(n, n))
    _, label = connected_components(g, directed=False)
    # node_id: an injection may be a lid (a pin's or a part's metal), not the grid node under it
    into = {int(label[plane.node_id(nd)]) for nd, a in inj if a > 0}
    out = {int(label[plane.node_id(nd)]) for nd, a in inj if a < 0}
    return bool(into) and into <= out


def prepare_pdn(model: BoardModel, specs: list) -> tuple[list, list, list]:
    """Each rail's model and ports: (setups to solve, results, issues). A rail that can't be set up (no outline, no
    IC on it, no copper at a pad) gets a result with its error."""
    from .power import _area_shares, _side
    board, config = model.board, model.config
    setups, results, issues = [], [], []
    bbox = model.board_region()
    a = config.analysis
    for spec in specs:
        ground = spec.ground or (config.reference_nets[0] if config.reference_nets else "GND")
        v = spec.voltage if spec.voltage is not None else rail_voltage(spec.rail)
        res = PdnResult(spec.label, spec.rail, ground, voltage=v, check_to=spec.check_to, ripple=spec.ripple)
        results.append(res)
        if bbox is None:
            res.error = "the board has no outline (Edge.Cuts): the PDN model is the whole board"
            continue
        parts = supply_parts(board, spec.rail, ground)
        if spec.parts:
            wanted = set(spec.parts)
            parts = [t for t in parts if t[0].reference in wanted]
            missing = wanted - {t[0].reference for t in parts}
            if missing:
                res.notes.append(f"{', '.join(sorted(missing))}: no supply pin on {spec.rail} and pad on {ground}: "
                                 "left out")
        # the load step: the one set, else estimated from the ICs' currents (their fields, else their pin counts)
        if spec.i_step is not None:
            res.i_step, res.i_step_source = spec.i_step, "set"
        else:
            res.i_step, res.i_step_source, res.i_step_detail = estimate_i_step(parts)
        if res.i_step and v is not None:
            res.target_ohm = v * spec.ripple / res.i_step
        elif res.i_step:
            res.notes.append(f"no voltage for {spec.rail} (set voltage): no target impedance")
        if res.i_step_source not in ("", "set"):
            counted = sorted((d for d in res.i_step_detail if d["how"] != "regulator"), key=lambda d: -d["amps"])
            what = {"fields": "from the ICs' Supply Current fields", "estimate": "ESTIMATED by the ICs' pin counts",
                    "partly": "PARTLY ESTIMATED (Supply Current fields, else pin counts)"}[res.i_step_source]
            res.notes.append(f"load step {format_si(res.i_step, 'A')}: half the {len(counted)} ICs' maximum current "
                             f"{what}: " + ", ".join(f"{d['part']} {format_si(d['amps'], 'A')}"
                                                     + ("" if d["how"] == "field" else " est.") for d in counted)
                             + " (set Load step, or the ICs' Supply Current field, to replace it)")
        regs = [d["part"] for d in res.i_step_detail if d["how"] == "regulator"]
        if regs and spec.i_step is None:
            res.notes.append(f"{', '.join(regs)}: regulator(s) on {spec.rail} (a power_out pin) not counted in the load step "
                             "(their load's current is not known): set Load step for this rail")
        res.notes += [d["note"] for d in res.i_step_detail if d.get("note")]
        for port in spec.ports:             # pin pairs: a connector the load hangs on (a converter's output)
            got = _port_pads(board, port, spec.rail, ground)
            if isinstance(got, str):
                res.notes.append(got + ": left out")
            else:
                parts.append(got)
        if not parts:
            res.error = (f"no IC with a supply pin on {spec.rail} and a pad on {ground} (pin type power_in; set "
                         "`parts` to name them, or `ports` for a connector the load hangs on)")
            continue
        # the source: a power stage (named, or the one whose output this is), else a part (named, or found)
        stage = None
        if spec.source.startswith(STAGE):
            stage = find_stage(board, config, spec.rail, spec.source[len(STAGE):])
            if stage is None:
                res.notes.append(f"no power stage {spec.source[len(STAGE):]!r}: the source is found by itself")
        elif not spec.source:
            stage = find_stage(board, config, spec.rail)
        nets = {spec.rail, ground} | (stage_outputs(board, config, stage)[0] if stage is not None else set())
        try:
            # the rail and its ground on every copper layer; the capacitance between their facing copper
            layers = list(board.copper_layers)
            h = min((board.stackup.dielectric_between(x, y) for x, y in zip(layers, layers[1:])), default=0.2)
            d = _choose_pitch(max(h, 0.25), bbox, len(layers), a.cavity_max_nodes, "auto", a.resolution)
            st = model.build(bbox, layers, nets, d, True)
            branches, src_text = [], ""
            src = None
            if stage is not None:
                try:
                    branches, src_text, fc = _stage_branch(st, board, config, stage, spec.rail, ground)
                    res.source, res.stage = src_text, stage.name
                    if fc:
                        res.f_min = max(min(F_MIN, fc / STAGE_F_LOW), F_MIN_STAGE)
                except SolverError as e:
                    res.notes.append(f"the power stage as the source: {e}; a regulator's vrm_r / vrm_l instead")
                    stage = None
            if stage is None and spec.source.lower() != "none" and not spec.source.startswith(STAGE):
                src = board.footprint(spec.source) if spec.source else find_source(board, spec.rail, ground)
            if src is not None:
                branches, src_text = _source_branch(st, src, spec.rail, ground, spec)
                res.source = src_text
            else:
                res.notes.append(f"no source on {spec.rail} (a regulator's power_out pin, or a connector; `source` "
                                 "names it): below the decaps' range the impedance is theirs alone")
            plane = st.plane.with_branches(branches) if branches else st.plane
            piece, main = copper_pieces(st, plane)
            # the rail's main copper is the piece its source feeds (a coarse grid can split a pour in two, and the
            # piece with most nodes need not be the fed one), else the piece holding most of the ICs' supply pads
            # (a power stage's branch sits on its inductor's net, which may be behind a shunt: not the rail's piece)
            fed = [int(piece[plane.node_id(b.a)]) for b in branches if stage is None]     # b.a may be a lid
            if fed:
                main[spec.rail] = max(set(fed), key=fed.count)
            else:
                pads_at = []
                for fp, rp, _ in parts:
                    for p in rp:
                        nd = next((x for x in (st.node(l, *p.position, spec.rail, max(st.d, 1.0)) for l in p.layers)
                                   if x), None)
                        if nd is not None:
                            pads_at.append(int(piece[plane.nid[nd]]))
                if pads_at:
                    main[spec.rail] = max(set(pads_at), key=pads_at.count)
            ports, meta, cut = [], [], []
            for fp, rp, gp in parts:
                side = _side(fp)
                try:
                    inj = _area_shares(st, rp, side, 1.0) + _area_shares(st, gp, side, -1.0)
                except SolverError as e:
                    # one IC's pad without copper on this grid (a small pad on a coarse model): that IC is left out,
                    # not the rail
                    res.notes.append(f"{fp.reference} left out: {e} on this model's {st.d:.2f} mm grid")
                    res.left_out.append({"part": fp.reference, "pads": [list(map(float, p.position)) for p in rp]})
                    continue
                # its pads on the rail's and the ground's main copper, or it would read only stray capacitance (and
                # slow every solve: a nearly floating port)
                on_rail = all(piece[plane.node_id(nd)] == main.get(spec.rail) for nd, a_ in inj if a_ > 0)
                on_gnd = all(piece[plane.node_id(nd)] == main.get(ground) for nd, a_ in inj if a_ < 0)
                if not (on_rail and on_gnd):
                    cut.append(fp.reference + ("" if on_rail else f" ({spec.rail})") + ("" if on_gnd else f" ({ground})"))
                    res.left_out.append({"part": fp.reference, "pads": [list(map(float, p.position)) for p in rp]})
                    continue
                ports.append(inj)
                meta.append({"part": fp.reference, "value": fp.value, "pins": [p.pin for p in rp],
                             "xy": (float(fp.position[0]), float(fp.position[1])), "pads": rp, "ground_pads": gp,
                             "fp": fp})
            if cut:
                res.notes.append(f"{', '.join(cut)}: the pads do not reach the main copper of that net on this model's "
                                 f"{st.d:.2f} mm grid (pads smaller than a cell on thin tracks, or a break in the "
                                 "copper): left out; a higher resolution (Preferences) resolves small pads")
            if not ports:
                # a board without the pours (zones unfilled, or removed while it is laid out again) has its rail and
                # ground in pieces of tracks: say so rather than blame the pads (refboard saved mid-relayout: no
                # zone at all, every rail failed with "a higher resolution resolves small pads")
                poured = {z.net for z in board.zones if not z.is_rule_area and any(z.filled.values())}
                missing = [n for n in (spec.rail, ground) if n not in poured]
                why = (f"; the board has no filled copper pour (zone) of {' or '.join(missing)}: fill its zones "
                       "(Refill zones), or check that the pours are on the board" if missing else "")
                raise SolverError("no IC's pads reach the rail's copper on this model" + why)
            exc = PortSet(plane, ports)
        except SolverError as e:
            res.error = f"{spec.label}: {e}"
            continue
        n0 = int(math.ceil(math.log10(F_MAX / res.f_min) * PER_DECADE))
        run = ModelRun(st, exc, planned=[float(f) for f in np.geomspace(res.f_min, F_MAX, n0 + 1)])
        # each port's loop on a fine regional model (the inductance there; _local_runs), its decaps those that carry
        # its current on this model: one of the sweep's own solves, the one nearest LOCAL_PROBE_F, made now and kept
        # (the scheduler solves only what is not solved yet)
        caps = decaps(board, config, spec.rail, ground)
        # each decap's two terminals (its one part branch's ends): driven at the key frequencies for its own
        # impedance (the "without it" and "one more beside it" figures, rank-one [A128]); a part modelled as several
        # branches (no terminal metal) has no one pair of terminals and is left out
        terms = {}
        for c in caps:
            own = [b for b in plane.branches if b.kind == "part" and b.name == c.reference]
            if len(own) == 1:
                terms[c.reference] = (own[0].a, own[0].b)
        exc.set_decaps(terms)
        f_probe = min(run.planned, key=lambda f: abs(math.log(f / LOCAL_PROBE_F)))
        sol, shares = probe_shares(exc, list(plane.branches), {c.reference for c in caps}, f_probe) if caps \
            else (None, [])
        if sol is not None:
            run.sols[f_probe] = sol
        elif caps:
            res.notes.append(f"the solve at {f_probe / 1e6:.3g} MHz that finds each port's decaps failed: the decaps "
                             f"within {LOCAL_NEAR_MM:g} mm of its pins are in its fine model")
        why = []
        for k, m in enumerate(meta):
            pairs, info = _local_runs(model, st, spec, ground, m["fp"], m["pads"], m["ground_pads"], caps,
                                      shares[k] if k < len(shares) else None)
            m["local"] = pairs
            if pairs is None:
                why.append(f"{m['part']} ({info})")
            else:
                m["local_pitch"] = info["pitch"]
                m["local_refs"], m["local_share"], m["local_pins"] = info["refs"], info["share"], info["pins"]
        if why:
            res.notes.append("without the fine model at the pins: " + "; ".join(why))
        res.nodes, res.pitch_mm = st.nodes, st.d
        res.notes.append(f"PDN modelled on the whole board: {spec.rail} and {ground} on {len(layers)} layers, "
                         f"{st.nodes} nodes at {st.d:.2f} mm, {len(meta)} IC port(s)")
        # the scheduler's progress names a setup by spec.name: the label (the rail when no name is set)
        setups.append(PdnSetup(replace(spec, name=spec.label), res, run, meta, list(plane.branches), st))
    return setups, results, issues


def _names(branches: list[Branch]) -> list[str]:
    """Each branch's part name ("" for vias and tracks); the source's as "source REF"."""
    return [b.name if b.kind == "part" else "" for b in branches]


def _inductance(fs: np.ndarray, Zc: np.ndarray, z: np.ndarray, peaks: list) -> Optional[float]:
    """The inductance the IC sees above its decaps (nH): Im(Z)/ω in the inductive stretch between the |Z| minimum
    (the decaps' series resonance) and the first anti-resonance above it (with the planes' capacitance), the
    median of the solves there. Near the minimum the decaps' capacitance still lowers Im(Z) and near the peak the
    planes' raises it (one 1 µF decap on the test board read 12.8 nH at the solve below its 305 MHz peak against
    ~2 nH in between); None without an inductive stretch."""
    k0 = int(np.argmin(z))
    above = [pk["f"] for pk in peaks if pk["f"] > fs[k0]]
    f_top = min(above) if above else 300e6
    ks = [k for k in range(k0 + 1, len(fs)) if fs[k] < f_top and Zc[k].imag > 0]
    if not ks:
        return None
    return float(np.median([Zc[k].imag / (2 * math.pi * fs[k]) for k in ks]) * 1e9)


def _local_correction(meta: dict) -> Optional[float]:
    """The inductance (H) the board-wide grid gets wrong along a port's loop: per pair of its regional models
    (_local_runs: the loop's, and the pins' when they have their own) Im(Z_fine - Z_coarse)/ω, summed, the median
    over LOCAL_F; None without them. Added in series to the port's curve: it is the port's own current path that the
    grid resolves differently, and at low frequency (the capacitors' range) jω·ΔL is negligible next to them. Each
    frequency's sum is kept in meta["local_nh"]: without the planes' capacitance in the regional models
    (LOCAL_CAPACITANCE) they agree to a few pH, and finish_pdn says so when they do not (LOCAL_SPREAD)."""
    if not meta.get("local"):
        return None
    sums: dict[float, float] = {}
    for k, (fine, coarse) in enumerate(meta["local"]):
        sf, sc = fine.ok(), coarse.ok()
        # only where both are inductive (the loop closing through its decaps): below a decap's resonance the
        # difference would be a capacitance's
        common = {f for f in set(sf) & set(sc) if sf[f].Z[0].imag > 0 and sc[f].Z[0].imag > 0}
        if not common:
            return None
        if k:
            common &= set(sums)                     # a frequency every pair has
            sums = {f: sums[f] for f in common}
        for f in common:
            sums[f] = sums.get(f, 0.0) + (sf[f].Z[0] - sc[f].Z[0]).imag / (2 * math.pi * f)
    if not sums:
        return None
    meta["local_nh"] = [(float(f), float(v * 1e9)) for f, v in sorted(sums.items())]
    return float(np.median(list(sums.values())))


def finish_pdn(model: BoardModel, setup: PdnSetup):
    """The curves, what holds each port, the peaks, the worst margin against the target, the decaps near each IC
    and the issues."""
    res, spec, config = setup.res, setup.spec, model.config
    sols = setup.run.ok()
    if len(sols) < 2:
        res.error = res.error or "the PDN solves failed (see the notes)"
        return res
    failed = sorted(f for f, s in setup.run.sols.items() if s is None)
    if failed:
        # (a rail without decaps at 10 kHz: the planes alone are tens of kΩ against milliohms of copper, a system too
        # ill-conditioned for the iterative solve)
        res.notes = [n for n in res.notes if not n.startswith("GMRES did not converge")]
        res.notes.append(f"{len(failed)} frequenc{'y' if len(failed) == 1 else 'ies'} could not be solved "
                         f"({', '.join(f'{f / 1e3:.4g} kHz' for f in failed[:4])}): left out of the curves")
    fs = np.array(sorted(sols))
    Z = np.array([sols[f].Z for f in fs])                       # (frequencies, ports)
    names = _names(setup.branches)
    part_names = sorted({n for n in names if n})
    idx = {n: [k for k, m in enumerate(names) if m == n] for n in part_names}
    w = 2 * math.pi * fs
    for p, meta in enumerate(setup.ports):
        dl = _local_correction(meta)
        if dl is not None:
            Z[:, p] = Z[:, p] + 1j * w * dl          # the pins' inductance from the fine regional model
        z = np.abs(Z[:, p])
        port = {"part": meta["part"], "value": meta["value"], "pins": meta["pins"], "xy": meta["xy"],
                "f": [float(f) for f in fs], "z_ohm": [float(x) for x in z],
                "phase_deg": [float(np.degrees(np.angle(c))) for c in Z[:, p]]}
        # its Bode response: poles at the anti-resonances, zeros at the series resonances (a decap's SRF); the
        # impedance turns inductive at the top (the s·e term)
        from .transfer import fitted_response
        res.responses.append(fitted_response(f"{meta['part']}: Z at its supply pins", res.name, "impedance", fs,
                                             Z[:, p], use_e=True))
        # a curve no passive network has (|Z| rising while capacitive: the fine model's correction, a constant
        # inductance, larger than the board-wide model's own inductance there): said, and nothing judged from it
        bad = nonphysical_from(fs, Z[:, p], spec.check_to)
        if bad is not None:
            port["nonphysical_from"] = bad
            res.notes.append(f"{meta['part']}: its curve is not physical from {_s3(bad, 'Hz')} (|Z| rising while its "
                             "phase is capacitive: the fine model's correction"
                             + (f" of {dl * 1e9:+.2g} nH" if dl is not None else "") + " outweighs the board-wide "
                             "model's own inductance at its pins there): its peaks, fixes and load step are not "
                             "judged; a higher resolution (Preferences) brings the two models together")
        else:
            _port_step(setup, res.responses[-1], meta, fs, Z[:, p])
        # what holds the rail at each frequency: each part's share of the port's current (its branches added)
        holds = []
        every = {n: [] for n in part_names}         # each part's share at every frequency (decaps)
        ringing = []                                # the frequencies where the parts' currents circulate
        for k, f in enumerate(fs):
            I = sols[f].I_branch[p]
            cur = {n: complex(np.sum(I[idx[n]])) for n in part_names if idx[n] and len(I) > max(idx[n])}
            shares = {n: abs(c) for n, c in cur.items()}
            for n in part_names:
                every[n].append(float(shares.get(n, 0.0)))
            # near an anti-resonance the parts carry current circulating between them, many times the IC's own (their
            # magnitudes add to well over 1 A per A): no part's share means "supplies the IC" there, so those
            # frequencies are marked and not judged (the decap table, the plot's shading) [A126]
            ringing.append(sum(shares.values()) > RINGING_SUM)
            top = max(shares.items(), key=lambda t: t[1], default=(None, 0.0))
            holds.append({"f": float(f), "part": top[0] if top[1] >= SHARE_MIN else "planes",
                          "share": float(top[1])})
        port["holds"] = holds
        # kept: the parts that carry a share worth showing somewhere in the sweep (|I| of the part per A at the
        # port: the shares of parts in parallel can add to more than 1 where their currents are out of phase)
        port["shares"] = {n: v for n, v in every.items() if max(v, default=0.0) >= SHARE_KEEP}
        port["ringing"] = ringing
        # peaks: local maxima of |Z| (anti-resonances), the highest first
        peaks = [k for k in range(1, len(fs) - 1) if z[k] > z[k - 1] and z[k] >= z[k + 1]]
        port["peaks"] = [{"f": float(fs[k]), "z_ohm": float(z[k])} for k in sorted(peaks, key=lambda k: -z[k])[:4]]
        port["l_nh"] = _inductance(fs, Z[:, p], z, port["peaks"])
        port["l_correction_nh"] = None if dl is None else float(dl * 1e9)
        port["local_pitch_mm"] = meta.get("local_pitch")
        port["local_decaps"] = meta.get("local_refs")          # the decaps in its fine model ...
        port["local_share"] = meta.get("local_share")          # ... and their share of its decap current
        port["local_pins"] = meta.get("local_pins")            # the pins' own fine model's decaps (or None)
        if meta.get("local_share") is not None and meta["local_share"] < LOCAL_SHARE_NOTE:
            res.notes.append(f"{meta['part']}: its fine model holds the decaps that carry {meta['local_share']:.0%} "
                             "of its current at high frequency (the others are too far away for a region that fine): "
                             "the rest of its loop is the board-wide grid's")
        got = meta.get("local_nh") or []
        vals = [v for _, v in got]
        if len(vals) > 1 and max(vals) - min(vals) > max(LOCAL_SPREAD * abs(dl * 1e9), LOCAL_SPREAD_NH):
            res.notes.append(f"{meta['part']}: the fine model's correction is not one inductance ("
                             + ", ".join(f"{v:+.2f} nH at {f / 1e6:g} MHz" for f, v in got)
                             + "): its curve above the decaps is less certain")
        if res.target_ohm:
            band = [k for k in range(len(fs)) if fs[k] <= spec.check_to]
            k = max(band, key=lambda k: z[k])
            port["worst"] = {"f": float(fs[k]), "z_ohm": float(z[k]),
                             "margin_db": float(20 * math.log10(res.target_ohm / max(z[k], 1e-12))),
                             "holds": holds[k]["part"]}
        res.ports.append(port)
        _port_step_figures(res, port)
    if res.target_ohm:
        w = min(res.ports, key=lambda p: p["worst"]["margin_db"])
        res.worst = {"part": w["part"], **w["worst"]}
    _decaps_near(model, setup)
    setup.model_board = model.board
    _decap_use(setup, config)
    _cap_effects(setup)
    _explain_peaks(setup, config)
    _issues(setup, config.rules)
    res.seconds = time.time() - setup.t_start
    return res


def _port_step(setup: PdnSetup, resp, meta: dict, fs: np.ndarray, z: np.ndarray):
    """The rail's voltage at a port over time when the IC draws i_step (over the load_step's rise; none: an ideal
    step), or when the load_step's coil is switched on there, through its harness to its load capacitance
    (load_step.sampled_step on the solved impedance up to check_to: the band the target applies to, the package and
    die decoupling above it [A19]): its response's step curve (plots.draw_step) and figures, notes."""
    from ..config.settings import LoadStep
    from .load_step import sampled_step
    res, spec = setup.res, setup.spec
    ls = spec.load_step or LoadStep()
    if res.voltage is None or not (res.i_step or ls.switched) or np.sum(fs <= spec.check_to) < 6:
        return
    # nothing holds the rail at DC (no source: |Z| rising as 1/ωC towards DC): a constant step current would drain
    # the decaps without limit (ΔV = I·t / C, tens of volts after milliseconds on a 5 V rail), not a dip the board
    # can show. No step response then, said why. (A source with a large R or L leaves the low end capacitive too,
    # but holds DC: its step is computed.)
    if not res.source:
        res.notes.append(f"{meta['part']}: no step response: nothing holds {spec.rail} at DC in the model (no "
                         "source found: set `source` to the part that feeds it); a step would drain the decaps "
                         "without limit")
        return
    # the fit takes the impedance up to STEP_FIT_ABOVE × check_to: far above it a resonance of the planes (hundreds
    # of MHz) is still there after the low-pass (7× down at 7 × check_to) and became the fit's constant term: the
    # rail rising first when the load draws more (+310 mV at t = 0 on a 5 V rail), a ring that hid the real dip [A127]
    keep = fs <= STEP_FIT_ABOVE * spec.check_to
    try:
        st = sampled_step(fs[keep], z[keep], ls, res.i_step or 0.0, res.voltage, ls.rise, f_max=spec.check_to)
    except (ValueError, ZeroDivisionError, np.linalg.LinAlgError) as e:
        res.notes.append(f"{meta['part']}: no step response ({e})")
        return
    # a passive rail cannot move against the step first (more current drawn: it falls; −L·ΔI·2π·check_to at t = 0+):
    # a start the other way is the fit's, not the board's: no figures then, said so
    t0, v0 = st["nodes"][0][1], st["nodes"][0][2]
    first = float(v0[np.argmax(t0 > 0)] - res.voltage) if np.any(t0 > 0) else 0.0
    # (a start against the step that is a sizeable part of the whole response: a few mV of the fit's noise at the
    # first picosecond beside a 200 mV dip is not it)
    biggest = float(np.max(np.abs(v0 - res.voltage))) if len(v0) else 0.0
    if not ls.switched and (res.i_step or 0.0) > 0 and first > 0.5 * biggest and             first > 0.02 * spec.ripple * res.voltage:
        res.notes.append(f"{meta['part']}: no step response: its fit starts {first * 1e3:+.3g} mV, against the step "
                         "(a passive rail falls first when the load draws more): the impedance above check_to is not "
                         "fitted well enough")
        return
    for name, t, v, m in st["nodes"]:
        # the dip after the first LATE_TAUS time constants of the low-pass at check_to: the decaps' and the
        # regulator's, apart from the spike of the copper's inductance at the start (L·ΔI·2π·check_to)
        late = t > LATE_TAUS / (2 * math.pi * spec.check_to)
        if np.any(late):
            dv = v[late] - res.voltage
            k = int(np.argmin(dv) if (res.i_step or ls.switched) else np.argmax(dv))
            m["dip_late"], m["t_dip_late"] = float(dv[k]), float(t[late][k])
            m["peak_late"] = float(np.max(np.abs(dv)))
        if name == "port":                     # (a load node behind a harness: its figures in the notes)
            resp.step = {"t": t, "y": v, "metrics": m, "kind": "load", "v_nom": res.voltage, "max_dev": spec.ripple,
                         "label": meta["part"]}
        else:
            res.notes.append(f"{meta['part']}: at the load behind the harness the rail dips {m['dip'] * 1e3:+.3g} mV "
                             f"({m['dip_pct']:+.2f} %)")
    res.notes += [f"{meta['part']}: {n}" for n in st["notes"]]


def _decaps_near(model: BoardModel, setup: PdnSetup):
    """Every decap with the ICs it is near (its rail pad's distance to their supply pins), and decap_missing issues
    for supply pins with none within decap_mm."""
    board, config, res, spec = model.board, model.config, setup.res, setup.spec
    caps = decaps(board, config, spec.rail, res.ground)
    rows = []
    for fp in caps:
        m = config.model_for(fp.reference)
        cap_pads = [p for p in fp.pads if p.net == spec.rail]
        near = []
        for port in setup.ports:
            mm = min(math.dist(cp.position, pp.position) for cp in cap_pads for pp in port["pads"])
            if mm <= 3 * spec.decap_mm:
                near.append({"part": port["part"], "mm": round(mm, 2)})
        # C: nominal; C_dc: what the solve used, at the capacitor's DC voltage (analysis/bias.py)
        rows.append({"ref": fp.reference, "value": fp.value, "C": m.get("C"), "C_dc": m.capacitance(),
                     "near": sorted(near, key=lambda n: n["mm"])})
    res.decaps = sorted(rows, key=lambda r: r["ref"])
    pads = [(fp, [p for p in fp.pads if p.net == spec.rail]) for fp in caps]
    for port in setup.ports:
        far = []
        for pin in port["pads"]:
            mm = min((math.dist(pin.position, cp.position) for _, cps in pads for cp in cps), default=math.inf)
            if mm > spec.decap_mm:
                far.append((pin, mm))
        if far:
            pin, mm = max(far, key=lambda t: t[1])
            where = "no decap on the rail" if mm == math.inf else f"the nearest decap {mm:.1f} mm away"
            setup.issue(config.rules, "decap_missing",
                        f"{spec.label}: {len(far)} of {port['part']}'s {len(port['pads'])} supply pin(s) on {spec.rail} "
                        f"have no decap within {spec.decap_mm:g} mm ({pin.pin}: {where})",
                        tuple(pin.position), severity="warning", key=f"pdn:{spec.label}:{port['part']}:decap",
                        details={"part": port["part"], "pins": [p.pin for p, _ in far]})


def _decap_use(setup: PdnSetup, config) -> None:
    """Each decap's part in holding the rail, from the solved branch currents (no extra solve): over every IC port,
    its largest share of an IC's current below check_to (and at which IC and frequency), the band where it carries
    at least USEFUL_SHARE, its own series resonance 1/(2π·√(ESL·C)) (C at its DC bias), and "little" when it never
    reaches LITTLE_SHARE: added to each res.decaps row; a decap_ineffective info lists those. The share is a part's
    current per A drawn at the IC: a decap far from every IC, or one whose resonance sits where others already hold
    the rail, carries little [A126]."""
    res, spec = setup.res, setup.spec
    for row in res.decaps:
        best = (0.0, None, None)
        band = None
        for port in res.ports:
            v = port.get("shares", {}).get(row["ref"])
            if not v:
                continue
            fs = port["f"]
            ring = port.get("ringing") or [False] * len(fs)
            for f, s, r in zip(fs, v, ring):
                if f <= spec.check_to and not r and s > best[0]:
                    best = (s, port["part"], f)
            run = None                      # the widest stretch of consecutive frequencies at USEFUL_SHARE or more
            for f, s, r in zip(fs, v, ring):
                if s >= USEFUL_SHARE and f <= spec.check_to and not r:
                    run = (run[0], f) if run else (f, f)
                    if band is None or run[1] / run[0] > band[1] / band[0]:
                        band = run
                else:
                    run = None
        m = config.model_for(row["ref"])
        c, esl = (row.get("C_dc") or row.get("C")), (m.get("ESL") if m is not None else None)
        row["srf"] = float(1 / (2 * math.pi * math.sqrt(esl * c))) if c and esl else None
        row["peak_share"], row["peak_at"], row["peak_f"] = float(best[0]), best[1], best[2]
        row["band"] = [float(band[0]), float(band[1])] if band else None
        row["little"] = best[0] < LITTLE_SHARE
        # near an IC that was left out of the ports (its pads off the grid): it decouples that IC, which the model
        # does not draw from, so it is not called useless
        fp = setup.model_board.footprint(row["ref"]) if getattr(setup, "model_board", None) else None
        pads = [p.position for p in fp.pads if p.net == spec.rail] if fp is not None else []
        near = next((lo["part"] for lo in res.left_out for xy in lo["pads"] for cp in pads
                     if math.dist(cp, xy) <= 3 * spec.decap_mm), None)
        if row["little"] and near:
            row["little"], row["for_left_out"] = False, near
    little = [r["ref"] for r in res.decaps if r.get("little")]
    if little and res.ports:
        setup.issue(config.rules, "decap_ineffective",
                    f"{spec.label}: {len(little)} of {len(res.decaps)} decap(s) never carry {LITTLE_SHARE:.0%} of an "
                    f"IC's current below {_s3(spec.check_to, 'Hz')}: {', '.join(little[:12])}"
                    + (" …" if len(little) > 12 else "") + " (far from every IC, or their resonance where others hold "
                    "the rail: move them nearer an IC, or change their value)", _part_xy(setup, little[0]),
                    severity="info", key=f"pdn:{spec.label}:little",
                    details={"decaps": little})


def _part_xy(setup: PdnSetup, ref: str) -> tuple:
    """A part's position (an issue's location), else the first port's."""
    fp = setup.model_board.footprint(ref) if getattr(setup, "model_board", None) else None
    return tuple(map(float, fp.position)) if fp is not None else setup.res.ports[0]["xy"]


def nonphysical_from(fs, zc, check_to: float) -> Optional[float]:
    """The first frequency below check_to from which a port's curve is not that of a passive network: |Z| rising
    faster than half a decade per decade while its phase is below −45° (capacitive), at two solves in a row (a
    passive driving-point impedance rising that steeply is inductive [A129]); None when it is physical."""
    z = np.abs(np.asarray(zc))
    ph = np.degrees(np.angle(np.asarray(zc)))
    run = 0
    for k in range(1, len(fs)):
        if fs[k] > check_to:
            break
        slope = math.log10(max(z[k], 1e-15) / max(z[k - 1], 1e-15)) / max(math.log10(fs[k] / fs[k - 1]), 1e-9)
        run = run + 1 if (slope > 0.5 and ph[k] < -45 and ph[k - 1] < -45) else 0
        if run >= 2:
            return float(fs[k - 1])
    return None


def key_frequencies(sols: dict, check_to: float) -> list:
    """The frequencies where each decap's own impedance is worth solving: per port its worst |Z| below check_to and its
    three highest peaks there, the MAX_KEYS with the highest |Z| of any port (ascending)."""
    fs = sorted(sols)
    Z = np.abs(np.array([sols[f].Z for f in fs]))               # (frequencies, ports)
    keys = set()
    band = [k for k, f in enumerate(fs) if f <= check_to]
    for p in range(Z.shape[1]):
        z = Z[:, p]
        if not band:
            continue
        keys.add(max(band, key=lambda k: z[k]))
        peaks = [k for k in band if 0 < k < len(fs) - 1 and z[k] > z[k - 1] and z[k] >= z[k + 1]]
        keys.update(sorted(peaks, key=lambda k: -z[k])[:3])
    top = sorted(keys, key=lambda k: -float(np.max(Z[k])))[:MAX_KEYS]
    return sorted(float(fs[k]) for k in top)


def spot_parts(sols: dict, names: list, keys: list, target: Optional[float], check_to: float,
               solvable: set) -> dict:
    """Where a fix for each key point near or over the target would go (within WARN_DB of it): {(port, f): ref}, the
    decap carrying most of that port's current at the nearest solve above a peak that does not ring (the part on
    the peak's capacitive side: a part beside it lowers the peak), or at the worst point itself. Only decaps that
    can be driven (`solvable`); {} without a target."""
    if not target:
        return {}
    fs = sorted(sols)
    parts = sorted({n for n in names if n})
    idx = {n: [i for i, m in enumerate(names) if m == n] for n in parts}

    def shares(f, p):
        """Each part's |I| per A at port p and f."""
        sf = sols[f]
        return {n: abs(complex(np.sum(sf.I_branch[p][idx[n]]))) for n in parts if idx[n]}
    out = {}
    for f in keys:
        if f not in sols or f > check_to:
            continue
        for p in range(len(sols[f].Z)):
            if abs(sols[f].Z[p]) < target * 10 ** (-WARN_DB / 20):
                continue
            z = [abs(sols[x].Z[p]) for x in fs]
            k = fs.index(f)
            j = k
            if 0 < k < len(fs) - 1 and z[k] > z[k - 1] and z[k] >= z[k + 1]:
                j = next((i for i in range(k + 1, len(fs)) if sum(shares(fs[i], p).values()) <= RINGING_SUM), k)
            sh = {n: v for n, v in shares(fs[j], p).items() if n in solvable}
            if sh:
                out[(p, f)] = max(sh, key=sh.get)
    return out


def without_part(zpp: complex, i_k: complex, z_k: complex, zkk: complex) -> complex:
    """The port's impedance with one part taken out, from the solve with it in place (rank one: compensation and
    reciprocity [A128]): the part (impedance z_k) carries i_k per A at the port and the network seen across its two
    terminals is Z_kk (the part in parallel with the rest, Z_kk0): Z_pp0 = Z_pp + i_k² · (Z_kk0 + z_k), with
    1/Z_kk0 = 1/Z_kk − 1/z_k."""
    rest = 1.0 / (1.0 / zkk - 1.0 / z_k)
    return zpp + i_k ** 2 * (rest + z_k)


def with_added(zpp: complex, i_k: complex, z_k: complex, zkk: complex, z_new: complex) -> complex:
    """The port's impedance with a part z_new added across an existing part's terminals (in parallel with it, beside
    it): Z_pp − Z_pk² / (Z_kk + z_new), Z_pk = z_k · i_k the voltage across the part per A at the port [A128]."""
    zpk = z_k * i_k
    return zpp - zpk ** 2 / (zkk + z_new)


def _branch_z(setup: PdnSetup, ref: str, f: float) -> Optional[complex]:
    """A part's own impedance at f (its one part branch), None when it has several."""
    own = [b for b in setup.branches if b.kind == "part" and b.name == ref]
    return complex(own[0].z(f)) if len(own) == 1 else None


def _key_data(setup: PdnSetup) -> tuple[list, dict]:
    """The key frequencies' solutions with their decaps' Z_kk ([f], {f: PortSolution}) and each part's branch
    indices; ([], {}) when none was solved."""
    sols = setup.run.ok()
    keys = [f for f in setup.keys if getattr(sols.get(f), "Zd", None)]
    return keys, sols


def _port_z(port: dict, f: float) -> Optional[complex]:
    """A port's impedance at a solved frequency as its curve has it (the fine model's correction included)."""
    try:
        k = port["f"].index(f)
    except ValueError:
        return None
    return port["z_ohm"][k] * np.exp(1j * math.radians(port["phase_deg"][k]))


def _cap_effects(setup: PdnSetup) -> None:
    """Each decap's "without it" figure, exact at the key frequencies (the ports' peaks and worst points, where each
    decap's own impedance was solved): the largest rise of |Z| at one of them when it is taken out (without_part),
    compared at the SAME frequency: {part, f, z_with, z_without, db, at_least: True} (db ≤ 0: no rise at any of
    them, "no_rise"). It is a lower bound on the harm, never a claim that removing it helps: taking a decap out
    moves the anti-resonances it takes part in (a 10 µF out, the source's inductance with the 100 nF rose 18 dB at
    a new frequency on the test board), which the key frequencies do not see [A128]."""
    res, spec = setup.res, setup.spec
    keys, sols = _key_data(setup)
    if not keys:
        return
    names = _names(setup.branches)
    band = [f for f in keys if f <= spec.check_to]
    for row in res.decaps:
        ref = row["ref"]
        idx = [k for k, n in enumerate(names) if n == ref]
        best = None
        for p, port in enumerate(res.ports):
            if port.get("nonphysical_from") is not None:
                continue
            for f in band:
                s, zc = sols[f], _port_z(port, f)
                zkk, zk = (s.Zd or {}).get(ref), _branch_z(setup, ref, f)
                if zkk is None or zk is None or zc is None or not idx:
                    continue
                ik = complex(np.sum(s.I_branch[p][idx]))
                z0 = abs(zc + (without_part(complex(s.Z[p]), ik, zk, zkk) - complex(s.Z[p])))
                db = 20 * math.log10(max(z0, 1e-12) / max(abs(zc), 1e-12))     # at the same frequency
                if best is None or db > best["db"]:
                    best = {"part": port["part"], "f": f, "z_with": abs(zc), "z_without": z0, "db": db,
                            "at_least": True}
                    if res.target_ohm:
                        best["margin_with"] = 20 * math.log10(res.target_ohm / max(abs(zc), 1e-12))
                        best["margin_without"] = 20 * math.log10(res.target_ohm / max(z0, 1e-12))
        if best is not None and best["db"] <= 0.05:
            best["no_rise"] = True
        if best is not None:
            row["without"] = best
        elif ref in getattr(setup, "carried", {}):
            # not driven: it carries under MIN_CARRIED of every IC's current at the key frequencies; taking out a
            # part carrying a share s raises |Z| there by about 1/(1 − s) at most: that bound, marked as one
            s_ = setup.carried[ref]
            row["without"] = {"bound": True, "carried": s_, "db": 20 * math.log10(1 / max(1 - s_, 1e-6))}


def without_text(w: Optional[dict], short: bool = False) -> str:
    """A decap's "without it" figure in words (_cap_effects): the proven rise, a lower bound; or that it carries too
    little to be solved; or no rise at its key points (a moved peak not judged). short: for a plot's legend."""
    if not w:
        return ""
    if w.get("bound"):
        return (f"<{max(w['db'], 0.1):.1f} dB" if short else
                f"under {max(w['db'], 0.01):.2f} dB (it carries {w['carried'] * 100:.1f} % at most: not solved)")
    if w.get("no_rise"):
        return ("no rise at its key points" if short else
                "no rise at its peaks and worst points; taking it out moves the peaks it takes part in: not judged")
    at = f"{_s3(w['f'], 'Hz')} ({w['part']})"
    if short:
        return f"≥{w['db']:+.1f} dB @ {_s3(w['f'], 'Hz')}"
    return (f"at least {w['db']:+.1f} dB: |Z| {_s3(w['z_with'], 'Ω')} → {_s3(w['z_without'], 'Ω')} at {at}"
            + (f", margin there {w['margin_with']:+.1f} → {w['margin_without']:+.1f} dB" if "margin_with" in w else "")
            + "; it can also move a peak elsewhere")


E6 = (1.0, 1.5, 2.2, 3.3, 4.7, 6.8)


def _e6(c: float, up: bool = False) -> float:
    """The E6 value nearest to c (log scale); up: the smallest E6 value at least c."""
    dec = 10 ** math.floor(math.log10(c))
    vals = [v * dec for v in E6 + (10.0,)]
    if up:
        return min(v for v in vals if v >= c * (1 - 1e-9))
    return min(vals, key=lambda v: abs(math.log(v / c)))


def _fitted_q(resp, f: float) -> Optional[float]:
    """The Q of a fitted response's pole nearest f (within 20 %): |p| / (2·|Re p|) of a complex pole in the left
    half plane (rad/s, transfer.Response.poles); None without one."""
    best = None
    for pl in getattr(resp, "poles", None) or ():
        pl = complex(pl)
        if pl.real >= 0 or pl.imag <= 0:
            continue
        fp = abs(pl) / (2 * math.pi)
        d = abs(fp - f) / f
        if d <= 0.2 and (best is None or d < best[0]):
            best = (d, abs(pl) / (2 * abs(pl.real)))
    return best[1] if best else None


def _explain_peaks(setup: PdnSetup, config) -> None:
    """Each port's peaks below check_to explained [A128]: the parts on either side (what holds the rail at the
    nearest solves below and above that do not ring: the one below is inductive there, above its own resonance, the
    one above capacitive), the pair's characteristic impedance Z0 = 1/(2πf·C_above) and Q = |Z_peak| / Z0, the margin
    to the target; and where the peak is over the target or within WARN_DB of it, fixes tried at the key frequencies
    with with_added at the best decap's terminals: a capacitor resonating at the peak (C = 1/((2πf)²·ESL)), a damping
    capacitor (ESR ≈ Z0, C ≥ 4 × C_above), one more of the part above; kept only when the peak falls and the port's
    worst |Z| at the key frequencies does not rise. Each peak dict gains low, high, z0, q, margin_db, cause, fixes."""
    res, spec = setup.res, setup.spec
    keys, sols = _key_data(setup)
    names = _names(setup.branches)
    rows = {r["ref"]: r for r in res.decaps}
    for p, port in enumerate(res.ports):
        if port.get("nonphysical_from") is not None:
            continue
        fs, ring, holds = port["f"], port.get("ringing") or [], port["holds"]
        for pk in port["peaks"]:
            if pk["f"] > spec.check_to:
                continue
            k = fs.index(pk["f"])
            below = next((j for j in range(k - 1, -1, -1) if not (ring and ring[j])), None)
            above = next((j for j in range(k + 1, len(fs)) if not (ring and ring[j])), None)
            low = holds[below]["part"] if below is not None else None
            high = holds[above]["part"] if above is not None else None
            # the capacitive side's capacitance: every decap carrying at least USEFUL_SHARE of the IC's current at
            # the solve above (a bank of equal parts together: four 1 µF are 4 µF, Z0 a quarter of one's), else the
            # curve's own capacitance there (the planes, the source)
            group = []
            if above is not None:
                # and still capacitive there: below their own resonance (a bigger decap still carrying current above
                # the peak is the inductive side's, not this one's)
                group = [r for r in rows if (port.get("shares", {}).get(r) or [0] * len(fs))[above] >= USEFUL_SHARE
                         and (rows[r].get("srf") or 0.0) > fs[above]]
            c_high = sum((rows[r].get("C_dc") or rows[r].get("C") or 0.0) for r in group) if group else None
            if not c_high and above is not None:
                c_high = 1 / (2 * math.pi * fs[above] * max(port["z_ohm"][above], 1e-12))
            pk["low"], pk["high"] = low, high
            resp = res.responses[p] if p < len(res.responses) else None
            q_fit = _fitted_q(resp, pk["f"])
            trusted = q_fit is not None and not any("coarse" in n for n in getattr(resp, "notes", ()))
            if q_fit:
                # the peak's own Q from the curve's fitted pole (the mounting's inductance in it; the bare part's C
                # read a Q 3.7 peak as Q 0.83 [A128]), Z0 = |Z_peak| / Q as in a parallel resonance
                pk["q"], pk["z0"] = q_fit, pk["z_ohm"] / q_fit
                pk["q_from"] = "the curve's fitted pole" + ("" if trusted else " (a coarse fit: approximate)")
            elif c_high:
                pk["z0"] = 1 / (2 * math.pi * pk["f"] * c_high)
                pk["q"], pk["q_from"] = pk["z_ohm"] / pk["z0"], "the capacitive side's C"
            if c_high:
                pk["c_above"] = c_high
            if res.target_ohm:
                pk["margin_db"] = 20 * math.log10(res.target_ohm / pk["z_ohm"])
            more = [r for r in group if r != high]
            if low and high and low != high:
                lw = "the source's inductance" if low.startswith("source") else (
                    "the planes" if low == "planes" else f"{low} (inductive above its own resonance)")
                hw = "the planes' capacitance" if high == "planes" else (
                    "the source" if high.startswith("source") else
                    f"{high}" + (f" and {len(more)} more in parallel ({format_si(float(format(c_high, '.3g')), 'F')} "
                                 "together)" if more else "") + " (capacitive)")
                pk["cause"] = f"anti-resonance of {lw} with {hw}"
            else:                       # one part holds both sides: its inductance against the capacitance round it
                who = low or high or "the board's copper"
                who = "the source" if who.startswith("source") else who
                pk["cause"] = f"{who}'s inductance against the rest of the rail's capacitance"
            pk["fixes"] = []
            # fixes for a point over the target or within WARN_DB of it; not for a soft bump (Q < 1: nothing rings,
            # the level itself is what is short) and not without the capacitive side's value; why not, said
            if not (res.target_ohm and pk.get("margin_db", 99) < WARN_DB):
                continue
            if config.analysis.resolution < 2:
                # a peak's height and frequency depend on the grid (refboard: 2.54 Ω @ 4.2 MHz at 1x, 0.79 Ω @
                # 5.6 MHz at 4x): its fix is only as good as the curve
                pk["grid_note"] = ("its height and frequency depend on the grid: check at resolution 2 or more "
                                   "(Preferences) before adding parts")
            soft = trusted and pk.get("q", 9) < 1
            why = (f"a soft bump (Q {pk['q']:.2g}: it rings little; the level itself is short there)" if soft else
                   "no decap on the rail to try a fix beside" if not keys else
                   "its frequency was not among the key frequencies the fixes are solved at" if pk["f"] not in keys
                   else "no capacitive side found above it" if not c_high and not pk.get("z0") else "")
            if why:
                pk["fix_note"] = why
                continue
            s = sols[pk["f"]]
            zd = s.Zd or {}
            # where to add: the spot refine drove a decade round the point (spot_parts), else the part above if it
            # was solved, else the decap carrying most of this IC's current here (judged at the key frequencies)
            spots = [setup.spot_of[(p, pk["f"])]] if (p, pk["f"]) in setup.spot_of else [high] if high in zd else sorted(
                (r for r in zd if [i for i, n in enumerate(names) if n == r]),
                key=lambda r: -abs(complex(np.sum(s.I_branch[p][[i for i, n in enumerate(names) if n == r]]))))[:1]
            if not spots:
                pk["fix_note"] = ("no decap could be driven here (its solve failed, or the parts have no single pair "
                                  "of terminals)")
                continue
            at = spots[0]
            m = config.model_for(high) if high in rows else None
            esl = (m.get("ESL") if m is not None else None) or 0.5e-9
            esr = (m.get("ESR") if m is not None else None) or 0.01
            w = 2 * math.pi * pk["f"]
            cands = []
            c_res = _e6(1 / (w ** 2 * esl))
            if c_res <= MAX_CERAMIC:            # a ceramic resonating there (a bigger one is no ceramic of that ESL)
                cands.append(("a capacitor resonating at the peak", c_res, esr, esl))
            # a damping part: ESR ≈ Z0 (a polymer or tantalum part of that ESR, or a ceramic with a series resistor),
            # C well above the capacitive side's so its own resonance is below the peak
            cands.append(("a damping capacitor with ESR about Z0 (polymer / tantalum, or a ceramic and a series "
                          "resistor)", _e6(4 * c_high, up=True), max(pk["z0"], 1e-3), 1e-9))
            if high in rows:
                cands.append((f"one more {rows[high]['value']}", rows[high].get("C_dc") or rows[high].get("C"), esr,
                              esl))
            pk["fixes"], why = _try_fixes(setup, p, port, pk["f"], pk["z_ohm"], at, cands, keys, sols, names)
            if not pk["fixes"]:
                pk["fix_note"] = why
        # the worst point when it is no peak (the inductive rise towards check_to, or too little capacitance low
        # down): its cause by the impedance's phase there and the same kind of fix, one more of what holds it there
        w = port.get("worst")
        if not w or not res.target_ohm or any(pk["f"] == w["f"] for pk in port["peaks"]):
            continue
        k = fs.index(w["f"])
        ph = port["phase_deg"][k]
        hold = holds[k]["part"]
        if ph > 45:
            zc = _port_z(port, w["f"])
            l_here = zc.imag / (2 * math.pi * w["f"]) if zc is not None else None   # the inductance at that point
            whose = ("the source's lead and the copper" if hold.startswith("source") else
                     "the planes' and the copper's" if hold == "planes" else
                     f"{hold}'s mounting, the vias and the planes between it and the pins")
            w["cause"] = ("the inductance above the decaps" + (f" ({l_here * 1e9:.2g} nH there)" if l_here else "")
                          + f": {whose}")
            # no part fixes an inductance: the layout does (what helps, in the order it usually pays)
            w["advice"] = ("lower that inductance: the decap closer to the pins (same layer, short wide traces), more "
                           "and shorter vias to the planes, the planes closer together, a smaller decap package")
        elif ph < -45:
            w["cause"] = f"too little capacitance at {_s3(w['f'], 'Hz')} (the rail held by {hold} there)"
        else:
            w["cause"] = f"held by {hold} at its own resonance (its ESR and the copper's resistance)"
        w["fixes"] = []
        if config.analysis.resolution < 2 and ph <= 45:     # (an inductive rise converges; a resonance less so)
            w["grid_note"] = ("its height and frequency depend on the grid: check at resolution 2 or more "
                              "(Preferences) before adding parts")
        hold = setup.spot_of.get((p, w["f"]), hold)          # the spot refine drove round the point
        if w["margin_db"] >= WARN_DB:
            continue
        if w["f"] not in keys or hold not in rows or hold not in (sols[w["f"]].Zd or {}):
            w["fix_note"] = ("no decap on the rail to try a part beside" if not keys else
                             "no decap could be driven here (its solve failed, or no single pair of terminals)")
            continue
        m = config.model_for(hold)
        c_h = rows[hold].get("C_dc") or rows[hold].get("C")
        esl = (m.get("ESL") if m is not None else None) or 0.5e-9
        esr = (m.get("ESR") if m is not None else None) or 0.01
        cands = [(f"one more {rows[hold]['value']}", c_h, esr, esl)]
        c_more = _e6(1 / (2 * math.pi * w["f"] * res.target_ohm))
        if ph < -45 and c_more <= MAX_CERAMIC:      # capacitance short: the bulk the target asks for there
            cands.append(("more capacitance", c_more, esr, esl))
        w["fixes"], why = _try_fixes(setup, p, port, w["f"], w["z_ohm"], hold, cands, keys, sols, names, peak=False)
        if not w["fixes"]:
            w["fix_note"] = why


def _try_fixes(setup: PdnSetup, p: int, port: dict, f0: float, z0_abs: float, at: str, cands: list, keys: list,
               sols: dict, names: list, peak: bool = True) -> list:
    """Candidate parts (what, C, ESR, ESL) added beside the decap `at`, each tried with with_added wherever `at` was
    driven (a decade either side of the point and the key frequencies) [A128]. A fix moves a peak as often as it
    lowers it, so it is judged by the highest |Z| within that decade, not by |Z| at the old frequency: kept when that
    falls by at least MIN_GAIN_DB and the highest |Z| of everything judged does not rise. The best two (lowest new
    local maximum), as {what, c, esr, esl, beside, z_local_old, z_local_new, f_local_new, margin_local_new,
    reaches_target, span, peak}. (The IC's fine-model correction is taken as it was.)"""
    res, spec = setup.res, setup.spec
    idx = [i for i, n in enumerate(names) if n == at]
    where = [f for f in sorted(sols) if f <= spec.check_to and at in (sols[f].Zd or {})]
    near = [f for f in where if f0 / SPOT_SPAN <= f <= f0 * SPOT_SPAN]
    if f0 not in where or not near:
        return [], "the decap beside which a part would go was not solved round this point"
    old = {f: abs(_port_z(port, f) or 0.0) for f in where}
    old_local = max(old[f] for f in near)
    old_worst = max(old.values())
    out, rejected = [], []
    for what, c, r, l_ in cands:
        if not c:
            continue
        new = {}
        for f in where:
            sf, zc = sols[f], _port_z(port, f)
            zkk, zk = (sf.Zd or {}).get(at), _branch_z(setup, at, f)
            if zkk is None or zk is None or zc is None:
                continue
            wf = 2 * math.pi * f
            zn = complex(r, wf * l_ - 1 / (wf * c))
            ik = complex(np.sum(sf.I_branch[p][idx]))
            new[f] = abs(zc + (with_added(complex(sf.Z[p]), ik, zk, zkk, zn) - complex(sf.Z[p])))
        if not all(f in new for f in near):
            continue
        f_loc = max(near, key=lambda f: new[f])
        local = new[f_loc]
        if 20 * math.log10(old_local / max(local, 1e-12)) < MIN_GAIN_DB:
            rejected.append("lowers the highest |Z| near it by less than 1 dB")
            continue
        if max(new.values()) > old_worst * 1.001:
            rejected.append("raises the rail's worst point")
            continue
        # outside the span (the span's own new maximum is what is reported): no judged point newly over the target
        if any(new[f] > res.target_ohm >= old[f] for f in new if f not in near):
            rejected.append("pushes a point further away over the target")
            continue
        # a peak's new maximum at the top of a span that check_to cut short: the peak moved towards or past the
        # band's edge, where the shared-pads model is least sure (−2.4 dB there on a test board): flagged
        edge = peak and f0 * SPOT_SPAN > spec.check_to and f_loc == max(near)
        out.append({"what": what, "c": c, "esr": r, "esl": l_, "beside": at, "z_local_old": old_local,
                    "z_local_new": local, "f_local_new": f_loc,
                    "margin_local_new": 20 * math.log10(res.target_ohm / max(local, 1e-12)),
                    "reaches_target": bool(local <= res.target_ohm), "span": [min(near), max(near)], "peak": peak,
                    "edge": edge})
    out.sort(key=lambda x: x["z_local_new"])
    why = ("no part tried helps: " + "; ".join(sorted(set(rejected))) if rejected else
           "no part tried could be judged here")
    return out[:2], ("" if out else why)


def fix_text(fx: dict) -> str:
    """One suggested fix in words: the part, where, and what the highest |Z| within a decade of the point becomes."""
    c = float(f"{fx['c']:.2g}")
    return (f"add {fx['what']}: {format_si(c, 'F')} (ESR {_s3(fx['esr'], 'Ω')}, ESL {_s3(fx['esl'], 'H')}) beside "
            f"{fx['beside']}: the highest |Z| from {_s3(fx['span'][0], 'Hz')} to {_s3(fx['span'][1], 'Hz')} falls "
            f"from {_s3(fx['z_local_old'], 'Ω')} to ≈{_s3(fx['z_local_new'], 'Ω')} (at {_s3(fx['f_local_new'], 'Hz')}"
            f"; margin there {fx['margin_local_new']:+.1f} dB, "
            + ("meets the target" if fx["reaches_target"] else "still short of the target")
            + ("; the peak moves to the band's edge: approximate)" if fx.get("edge") else ")"))


def point_advice(pk: dict) -> str:
    """A peak's or worst point's fixes and notes as one text: the best fix, the layout advice (an inductive rise),
    why no fix, and the grid note."""
    parts = [fix_text(pk["fixes"][0])] if pk.get("fixes") else []
    parts += [pk[k] for k in ("advice", "fix_note", "grid_note") if pk.get(k)]
    return "; ".join(parts)


def _port_step_figures(res: PdnResult, port: dict):
    """The port's step figures, taken from its response's step (set by _port_step before the port was listed)."""
    r = res.responses[-1] if res.responses else None
    st = getattr(r, "step", None)
    if st and st.get("label") == port["part"]:
        port["step"] = {"label": port["part"], **st["metrics"]}


def _s3(value, unit: str = "") -> str:
    """A value in an issue's text: three significant digits (format_si keeps six, for values read back)."""
    return format_si(float(f"{value:.3g}"), unit)


def _issues(setup: PdnSetup, rules):
    """pdn_impedance: a port above the target below check_to (an error over it, a warning within WARN_DB);
    pdn_step: the voltage at a port moving by more than the ripple when its IC steps (or the coil is switched on),
    after the copper's spike at the start (peak_late: the decaps' and the source's response). The spike, L·ΔI over
    the edge the check_to low-pass leaves, is the impedance above the decaps that pdn_impedance already judges in
    frequency; judged again in time it flagged an off-board load at a connector port (5 A, 1.3 nH: −3.5 V in 0.5 ns
    on refboard's converter output) whose harness could never draw that edge."""
    res, spec = setup.res, setup.spec
    # an estimated load step (none set): its findings are warnings at most and say so
    est = res.i_step_source in ("estimate", "partly")
    tag = (f" [load step {_s3(res.i_step, 'A')} ESTIMATED from the ICs' pin counts: set Load step]" if est else
           f" [load step {_s3(res.i_step, 'A')} from the ICs' Supply Current]" if res.i_step_source == "fields"
           else "")
    for port in res.ports:
        m = port.get("step")
        peak = m.get("peak_late", m["peak"]) if m else None
        if m and res.voltage and peak > spec.ripple * res.voltage:
            spike = (f"; before it the copper's spike {m['dip'] * 1e3:+.3g} mV at {_s3(m['t_dip'], 's')}"
                     if m.get("dip_late") is not None and abs(m["dip"]) > abs(m["dip_late"]) else "")
            late = m.get("dip_late", m["dip"]), m.get("t_dip_late", m["t_dip"])
            setup.issue(rules, "pdn_step",
                        f"{spec.label}: at {port['part']} the rail moves by {peak * 1e3:.3g} mV "
                        f"({100 * peak / res.voltage:.2f} %) when the load steps, more than the "
                        f"{spec.ripple * 100:g} % allowed (dips {late[0] * 1e3:+.3g} mV after {_s3(late[1], 's')}"
                        f"{spike}){tag}", port["xy"], severity="warning" if est else None,
                        key=f"pdn:{spec.label}:{port['part']}:step",
                        details={"dip": late[0], "peak": peak, "t_dip": late[1], "spike": m["dip"]})
    if not res.target_ohm:
        return
    for port in res.ports:
        w = port["worst"]
        if w["margin_db"] >= WARN_DB:
            continue
        sev = "error" if w["margin_db"] < 0 and not est else "warning"
        # why, and the best fix tried there (_explain_peaks: the peak at the worst frequency, else the worst point)
        at = next((pk for pk in port["peaks"] if pk["f"] == w["f"]), w)
        why = f": {at['cause']}" if at.get("cause") else ""
        if port.get("nonphysical_from") is not None:
            why = (f" [its curve is not physical from {_s3(port['nonphysical_from'], 'Hz')}: not trusted, see the "
                   "notes; a higher resolution resolves it]")
        fixes = at.get("fixes") or []
        fix = f"; {point_advice(at)}" if point_advice(at) else ""
        setup.issue(rules, "pdn_impedance",
                    f"{spec.label}: {port['part']} sees {_s3(w['z_ohm'], 'Ω')} at {_s3(w['f'], 'Hz')} "
                    f"against the target {_s3(res.target_ohm, 'Ω')} (margin {w['margin_db']:+.1f} dB; held there "
                    f"by {w['holds']}){why}{fix}{tag}", port["xy"], severity=sev,
                    key=f"pdn:{spec.label}:{port['part']}:z",
                    details={k: w[k] for k in ("f", "z_ohm", "margin_db")})
