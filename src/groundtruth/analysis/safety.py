"""Voltage-aware safety checks (the plan's M2): what each net's voltage asks of the copper and the parts around it.

Net voltages (`net_voltages`): grounds and the reference nets 0 V; a rail's voltage from its name (+12V, VDD_3V3,
analysis.pdn.rail_voltage); a power stage's input rail and switch node its v_in (the node swings to it), a motor
bridge's bus and phase nodes its v_bus; a PDN rail's voltage; and the settings' own (`safety: voltages:`), which
win. A net with no voltage known is taken as a low-voltage one (0 V) next to a high-voltage net.

Run checks (no solve):
- clearance_voltage: copper of two nets closer on a layer than IPC-2221B Table 6-1 asks for their voltage difference
  (B1 internal layers; B2 external uncoated, sea level to 3050 m; B4 external with a permanent coating, `safety:
  coated`); pairs whose difference needs more than the basic spacing (over 30 V), one issue per pair and layer, at the
  closest point. A table of your own (`clearance_mm`: [[V, mm], ...], e.g. B3 above 3050 m, which public sources
  do not reproduce reliably) replaces the external columns.
- cap_voltage: a capacitor's voltage (between its nets) against its rating (its Rated Voltage or Voltage field, or
  "50V" in its value), derated by kind (ceramic 50 %, tantalum 50 %, aluminium 80 %, by default): over the rating an
  error, over the derated value a warning. Capacitors on nets of 10 V or more without a rating: one info issue naming
  them.
- esd_protection: a connector's signal pins (not ground) with no ESD / TVS part (by value, part or library name:
  TVS, ESD, SMBJ, PESD, TPD, USBLC, NUP ...) on the same net within `esd_mm` (10 mm): info for every connector, or
  a warning for the ones named in `esd_connectors` (the board's outside world).

After Simulate (from the solved currents and voltages):
- resistor_power: a resistor's loss in a DC path (I²R, the power stages' paths, all stages together where combined,
  DC loads) or a motor bridge's RMS currents against its rating (its Rated Power or Power field, else by package:
  0402 1/16 W ... 2512 1 W), derated (50 %); resistors dissipating UNRATED_FROM_W or more without a rating: one info
  issue naming them (only R parts: a pass FET's resistor model has no power rating).
- vds_rating: a power stage's switch-node peak (v_in + its overshoot) against its switches' Vds rating (their Vds
  field), above `vds_derating` (80 %) of it a warning, above it an error.

Validated in tests/test_safety.py: spacing on synthetic boards of known gaps against the table, ratings read from
fields and values, derating, a DC path's resistor loss against I²R, ESD found and missed.
"""
from __future__ import annotations

import math
import re
from typing import Callable, Optional

from shapely.ops import nearest_points

from ..components.fields import read as read_field
from ..components.models import model_pads
from ..core.board import Board, Footprint

# IPC-2221B Table 6-1 (electrical conductor spacing) [IPC-2221B], mm, by the voltage between the conductors (DC or AC
# peak): (upper end of the band V, B1 internal layers, B2 external uncoated up to 3050 m, B4 external with permanent
# polymer coating). Above 500 V: the 500 V value plus PER_VOLT_ABOVE_500 per volt over it. Two independent reproductions
# and the table as remembered agree on these columns (the A5-A7 and B3 columns are not built in).
IPC2221B = ((15, 0.05, 0.10, 0.05), (30, 0.05, 0.10, 0.05), (50, 0.10, 0.60, 0.13), (100, 0.10, 0.60, 0.13),
            (150, 0.20, 0.60, 0.40), (170, 0.20, 1.25, 0.40), (250, 0.20, 1.25, 0.40), (300, 0.20, 1.25, 0.40),
            (500, 0.25, 2.50, 0.80))
PER_VOLT_ABOVE_500 = (0.0025, 0.005, 0.00305)
CHECK_FROM_V = 30.0             # pairs up to this difference need the basic spacing only (the fab's own rules)
EXTERNAL = ("F.Cu", "B.Cu")
CONNECTOR_PREFIXES = ("J", "P", "X", "CN", "CON")
ESD_WORDS = ("TVS", "ESD", "SMAJ", "SMBJ", "SMCJ", "SMF", "P6KE", "P4SMA", "1.5KE", "PESD", "TPD", "USBLC", "PRTR",
             "SP05", "RCLAMP", "NUP", "ESDA", "SRV05", "IP4220", "CDSOT", "SM712", "PGB", "TCLAMP")
CAP_DERATING = {"ceramic": 0.5, "tantalum": 0.5, "electrolytic": 0.8}
# a package's usual power rating (thick film; 0612 is the reverse-geometry 1206 of current shunts: 1 W)
RESISTOR_WATTS = (("0201", 1 / 20), ("0402", 1 / 16), ("0603", 1 / 10), ("0805", 1 / 8), ("1206", 1 / 4),
                  ("1210", 1 / 2), ("1218", 1.0), ("2010", 3 / 4), ("2512", 1.0), ("0612", 1.0))
UNRATED_FROM_W = 0.05           # a resistor dissipating this much without a rating is listed (one info issue)
_RATING = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(k?)V(?:DC|AC)?(?![a-z])", re.IGNORECASE)


def is_ground(net: str) -> bool:
    """A net that looks like a ground or return (GND, AGND, VSS, 0V, ...RTN)."""
    name = net.upper().rsplit("/", 1)[-1]
    return "GND" in name or "VSS" in name or name in ("0V", "RTN", "RETURN") or name.endswith("_RTN")


def net_voltages(board: Board, config) -> dict[str, tuple[float, str]]:
    """{net: (peak voltage, where it comes from)} for the nets whose voltage is known (see the module)."""
    return voltage_model(board, config).peak


GATE_FN_SOURCE = re.compile(r"^(S|SN|SOURCE)\d*$", re.IGNORECASE)    # a FET's source pin (not its Kelvin source)
GATE_R_MAX = 100.0              # a resistor up to this passes a gate's riding on (gate resistors, a driver's outputs)
DEFAULT_DRIVE_V = 10.0          # a gate or bootstrap net sits up to this above what it rides on


class Volts:
    """The nets' voltages as ranges: `peak` {net: (highest V, why)} (net_voltages), `lo` {net: lowest V} (a switch
    node swings from 0 to its rail; absent: a DC net, lo = peak), `rides` {net: (net it rides on, drive V)} (a gate
    or bootstrap net sits 0 to drive V above its FET's source or its switch node, moving with it)."""

    def __init__(self, peak: dict, lo: dict, rides: dict):
        """The three maps (see the class)."""
        self.peak, self.lo_v, self.rides = peak, lo, rides

    def hi(self, net: str) -> float:
        """The net's highest voltage (0 when not known)."""
        return self.peak.get(net, (0.0, ""))[0]

    def lo(self, net: str) -> float:
        """The net's lowest voltage."""
        return self.lo_v.get(net, self.hi(net))

    def base(self, net: str, depth: int = 0) -> tuple[str, float]:
        """(the net it ultimately rides on, the most it sits above it): itself and 0 for a net riding on nothing."""
        if net in self.rides and depth < 8:
            ref, drive = self.rides[net]
            b, off = self.base(ref, depth + 1)
            return b, off + drive
        return net, 0.0

    def dv(self, a: str, b: str) -> float:
        """The largest voltage between two nets at any moment: the drive at most when both ride on one net (a
        bootstrap net and its switch node); else the far ends of their ranges (a switch node at 0 V beside its
        51.8 V rail)."""
        ra, oa = self.base(a)
        rb, ob = self.base(b)
        if ra == rb:
            return max(oa, ob)
        return max(self.hi(a) - self.lo(b), self.hi(b) - self.lo(a), 0.0)

    def text(self, net: str) -> str:
        """The net's voltage in words: '51.8 V', '0-51.8 V', 'SW1 + up to 10 V'."""
        if net in self.rides:
            ref, drive = self.rides[net]
            return f"{ref.rsplit('/', 1)[-1]} + up to {drive:g} V"
        lo, hi = self.lo(net), self.hi(net)
        return f"{hi:g} V" if lo == hi else f"{lo:g}-{hi:g} V"


def signed_rail_voltage(net: str):
    """A rail's voltage from its name with its sign: "+12V" 12, "-12V" / "−12V" −12 (pdn.rail_voltage reads the
    magnitude, which the PDN's target impedance wants). Audit round 7, AA-2: the sign was dropped, so a capacitor
    between +12V and −12V read 0 V instead of 24 V, for the DC-bias derating and the cap_voltage check alike."""
    from .pdn import rail_voltage
    v = rail_voltage(net)
    if v is None:
        return None
    name = net.rsplit("/", 1)[-1].strip()
    return -v if name[:1] in ("-", "−") else v


def voltage_model(board: Board, config) -> Volts:
    """The nets' voltages (see the module and Volts): names, grounds, reference nets, PDN rails, power stages (the
    switch node 0 to its rail), bridges (the phases 0 to the bus), the settings' own, series parts with no drop,
    then the gates and bootstrap nets riding their source or switch node (`_riding`). A negative rail's name gives
    a negative voltage (signed_rail_voltage), so Volts.dv across +12V and −12V is 24 V."""
    out: dict[str, tuple[float, str]] = {}
    lo: dict[str, float] = {}
    nodes: list[str] = []

    def put(net: str, v: Optional[float], why: str, force: bool = False):
        """Keep the highest voltage known for a net (the settings' own win)."""
        if net and v is not None and (force or net not in out or v > out[net][0]):
            out[net] = (float(v), why)
    for net in board.nets:
        if is_ground(net):
            put(net, 0.0, "ground")
        else:
            put(net, signed_rail_voltage(net), "from its name")
    for net in config.reference_nets:
        put(net, 0.0, "reference net", force=True)
    for d in getattr(config, "pdn", ()):
        put(d.rail, d.voltage, f"PDN {d.label}")
    from .power import switch_inductor
    stage_inductors = set()
    for c in config.converters:
        # the hot loop's rail: a buck's input, a boost's output; the switch node swings between ground and it; the
        # inductor's far side is a buck's output (a boost's input). The inductor itself carries no voltage across
        # to it (the switch node's peak is not the output's): it is kept out of _through_series_parts
        boost = c.topology == "boost"
        v, what = (c.v_out, "output") if boost else (c.v_in, "input")
        put(c.rail, v, f"{c.name} {what}")
        node, inductor = switch_inductor(board, config, c)
        put(node or "", v, f"{c.name} switch node (swings to its rail)")
        if node:
            lo[node] = 0.0
            nodes.append(node)
        if inductor is not None:
            stage_inductors.add(inductor.reference)
            far = next((p.net for p in inductor.pads if p.net and p.net != node), "")
            put(far, c.v_in if boost else c.v_out, f"{c.name} {'input' if boost else 'output'}")
    for b in config.bridges:
        put(b.rail, b.v_bus, f"{b.name} bus")
        for leg in b.legs:
            put(leg.switch_node, b.v_bus, f"{b.name} phase {leg.phase} (swings to the bus)")
            if leg.switch_node:
                lo[leg.switch_node] = 0.0
                nodes.append(leg.switch_node)
    for net, v in getattr(getattr(config, "safety", None), "voltages", {}).items():
        put(net, v, "settings (safety: voltages)", force=True)
        lo.pop(net, None)
    # then the series parts only fill in nets still unknown, so the settings' voltages win and spread too
    _through_series_parts(board, config, out, stage_inductors)
    volts = Volts(out, lo, {})
    _riding(board, config, volts, nodes)
    return volts


def _riding(board: Board, config, volts: Volts, nodes: list[str]):
    """Gate and bootstrap nets, unknown so far, as riding on another net: a FET's gate on its source (an N-channel's
    gate sits up to the drive above it: a high side's on the switch node, a low side's on ground, a surge stopper's
    on the rail it feeds); a bootstrap net (a capacitor to a switch node, and a pin of an IC or a diode: a
    controller's BOOST / BST) on its switch node (not a snubber's middle, which only reaches a resistor); then through
    resistors up to GATE_R_MAX and ferrites (a driver's TGUP / TGDN through the gate resistors) to the nets beyond.
    The drive: safety.gate_drive_v (10 V). refboard's 27 clearance warnings were such nets taken as 0 V."""
    from .detect import GATE_FN, KELVIN_FN
    drive = getattr(getattr(config, "safety", None), "gate_drive_v", DEFAULT_DRIVE_V) or DEFAULT_DRIVE_V
    known = lambda n: n in volts.peak or n in volts.rides
    for fp in board.footprints:
        if fp.dnp or not re.match(r"[QMT]\d", fp.reference):
            continue
        gates = {p.net for p in fp.pads if p.net and GATE_FN.match(p.pin_function or "")}
        sources = {p.net for p in fp.pads if p.net and GATE_FN_SOURCE.match(p.pin_function or "")
                   and not KELVIN_FN.match(p.pin_function or "")}
        if len(sources) == 1:
            src = next(iter(sources))
            for g in gates:
                if not known(g) and known(src):
                    volts.rides[g] = (src, drive)
    by_net = board.parts_by_net()
    for sw in nodes:
        for fp in by_net.get(sw, ()):
            if fp.dnp or not fp.reference.upper().startswith("C"):
                continue
            other = {p.net for p in fp.pads if p.net and p.net != sw}
            for x in other:
                reaches = {f.reference[:1].upper() for f in by_net.get(x, ()) if f is not fp}
                if not known(x) and not is_ground(x) and reaches & {"U", "D"}:
                    volts.rides[x] = (sw, drive)
    # on through gate resistors and ferrites, as long as something new is found
    changed = True
    while changed:
        changed = False
        for fp in board.footprints:
            m = config.model_for(fp.reference)
            if fp.dnp or m is None:
                continue
            passes = m.kind in ("short", "ferrite") or (m.kind == "resistor" and m.get("R", 1e9) <= GATE_R_MAX)
            nets = sorted({p.net for p in fp.pads if p.net})
            if not passes or len(nets) != 2:
                continue
            a, b = nets
            for x, y in ((a, b), (b, a)):
                if x in volts.rides and not known(y) and not is_ground(y):
                    volts.rides[y] = volts.rides[x]
                    changed = True
    for net, (ref, d) in volts.rides.items():
        base, off = volts.base(net)
        volts.peak[net] = (volts.hi(base) + off, f"rides on {ref} (+ up to {d:g} V of gate drive)")
        volts.lo_v[net] = volts.lo(base)


SERIES_MAX_OHM = 10.0           # a resistor up to this joins two nets at (nearly) one voltage: a shunt, a 0R, a fuse


def _through_series_parts(board: Board, config, out: dict, skip: set = frozenset()):
    """Nets joined by a series part with (nearly) no drop take the voltage of a known net they reach: shorts, 0R
    and resistors up to SERIES_MAX_OHM (current shunts, a surge stopper's pass FET modelled as one), inductors and
    ferrites. refboard's V_BATT (+VDC through the surge stopper Q601) and its current-sense amplifiers'
    supply nets (+VDC through milliohm shunts) were taken as unknown, 0 V, next to +VDC: false clearance errors.
    `skip`: the power stages' own inductors (a switch node's peak is not its output's voltage)."""
    from .detect import GATE_FN, KELVIN_FN
    parent: dict[str, str] = {}

    def find(n):
        """The group a net is in."""
        while parent.get(n, n) != n:
            n = parent[n]
        return n
    joined: dict[tuple, str] = {}
    for fp in board.footprints:
        m = config.model_for(fp.reference)
        if fp.dnp or m is None or fp.reference in skip:
            continue
        ok = m.kind in ("short", "inductor", "ferrite") or (m.kind == "resistor" and m.get("R", 1e9) <= SERIES_MAX_OHM)
        # the model's own pads; a pass FET modelled as a short or resistor without `pins` joins its drain and
        # source, not its gate (or a Kelvin source pin): those are left out by their pin names
        pads = model_pads(fp, m)
        if not m.pins:
            pads = [p for p in pads if not GATE_FN.match(p.pin_function or "")
                    and not KELVIN_FN.match(p.pin_function or "")]
        nets = sorted({p.net for p in pads if p.net})
        if not ok or len(nets) != 2:
            continue
        a, b = find(nets[0]), find(nets[1])
        if a != b:
            parent[a] = b
            joined[(nets[0], nets[1])] = fp.reference
    groups: dict[str, list] = {}
    for n in set(parent) | set(parent.values()):
        groups.setdefault(find(n), []).append(n)
    for members in groups.values():
        known = [(out[n][0], n) for n in members if n in out]
        if not known:
            continue
        v, src = max(known)
        for n in members:
            if n not in out:
                via = next((ref for (x, y), ref in joined.items() if n in (x, y)), "a series part")
                out[n] = (v, f"{src} through {via}")


def required_mm(dv: float, external: bool, coated: bool = False, table: Optional[list] = None) -> tuple[float, str]:
    """IPC-2221B Table 6-1's minimum spacing (mm) for a voltage difference dv on an internal or external layer
    (uncoated B2, or coated B4), and the column's name; `table` [[V, mm], ...] (a user's, for external layers)
    instead: the first row whose voltage is not below dv (its last row's value beyond)."""
    dv = abs(dv)
    if external and table:
        rows = sorted((float(v), float(mm)) for v, mm in table)
        return next((mm for v, mm in rows if dv <= v), rows[-1][1]), "your table (safety: clearance_mm)"
    col = 1 if not external else (3 if coated else 2)
    name = {1: "B1 (internal)", 2: "B2 (external, uncoated)", 3: "B4 (external, coated)"}[col]
    for row in IPC2221B:
        if dv <= row[0]:
            return row[col], name
    return IPC2221B[-1][col] + PER_VOLT_ABOVE_500[col - 1] * (dv - 500.0), name


Add = Callable[..., None]       # checker.add(check, net, message, location, **kw)


def check_clearances(board: Board, copper, model: Volts, safety, add: Add) -> int:
    """clearance_voltage (see the module): per layer, every net more than CHECK_FROM_V away from another net's
    voltage at some moment (Volts.dv: a switch node at 0 V beside its rail; an unknown net taken as 0 V) against the
    copper near it, measured edge to edge (shapely: the closest points). Returns the number of pairs found too
    close."""
    from shapely import STRtree
    coated, table = getattr(safety, "coated", False), getattr(safety, "clearance_mm", None)
    volts = model.peak
    vmax = max((v for v, _ in volts.values()), default=0.0)
    found = 0
    for layer in board.copper_layers:
        external = layer in EXTERNAL
        nets = [n for n in copper.nets_on(layer) if n]
        hot = [n for n in nets if n in volts and volts[n][0] > CHECK_FROM_V]
        if not hot:
            continue
        geoms = {n: copper.get(layer, n) for n in nets}
        names = [n for n in nets if geoms[n] is not None and not geoms[n].is_empty]
        tree = STRtree([geoms[n] for n in names])
        done = set()
        for a in hot:
            ga = geoms.get(a)
            if ga is None or ga.is_empty:
                continue
            va = volts[a][0]
            reach, _ = required_mm(va + vmax, external, coated, table)
            for k in tree.query(ga.buffer(reach)):
                b = names[int(k)]
                if b == a or (b, a) in done:
                    continue
                done.add((a, b))
                dv = model.dv(a, b)
                if dv <= CHECK_FROM_V:
                    continue
                need, col = required_mm(dv, external, coated, table)
                gb = geoms[b]
                d = ga.distance(gb)
                if d >= need - 1e-6:
                    continue
                # a part's own pins are spaced and rated by its maker (an EPC2306's interleaved drain and source,
                # a controller's 0.5 mm pitch): its pads against each other don't count, a track against them does
                ga, gb, d = _without_own_pads(board, layer, a, b, ga, gb, d)
                if d >= need - 1e-6:
                    continue
                pa, pb = nearest_points(ga, gb)
                at = ((pa.x + pb.x) / 2, (pa.y + pb.y) / 2)
                found += 1
                known = b in volts
                # both voltages known: an error; the other one not known (taken as 0 V): a warning saying so (a gate
                # drive or a bootstrap net sits near its rail's voltage: set it under safety: voltages)
                add("clearance_voltage", a,
                    f"{a} ({model.text(a)}) and {b} ({model.text(b) if known else 'voltage not known, taken as 0 V'})"
                    f" are {d:.2f} mm apart on {layer}: IPC-2221B {col} asks {need:.2f} mm for {dv:g} V"
                    + ("" if known else f" (give {b}'s voltage under safety: voltages)"), at, layer=layer,
                    severity=None if known else "warning", key=f"clearance:{layer}:{a}:{b}",
                    details={"other": b, "gap_mm": round(d, 3), "required_mm": need, "dv": dv, "column": col,
                             "other_known": known})
    return found


def _without_own_pads(board: Board, layer: str, a: str, b: str, ga, gb, d: float):
    """(copper of a, copper of b, the gap) with the pad-to-pad gaps inside one footprint left out: for each part with
    pads on both nets on this layer, the gap is the smaller of a's copper without that part's a-pads against all of
    b's, and all of a's against b's without its b-pads (a track or another part's pad near them still counts).
    Unchanged when no part has pads on both."""
    from shapely.ops import unary_union
    from ..geometry.shapes import pad_shape
    own_a, own_b = [], []
    for fp in board.parts_by_net().get(a, ()):
        pa = [p for p in fp.pads if p.net == a and layer in p.layers]
        pb = [p for p in fp.pads if p.net == b and layer in p.layers]
        if pa and pb:
            own_a += [pad_shape(p) for p in pa]
            own_b += [pad_shape(p) for p in pb]
    if not own_a:
        return ga, gb, d
    ga2 = ga.difference(unary_union(own_a).buffer(1e-4))
    gb2 = gb.difference(unary_union(own_b).buffer(1e-4))
    d1 = ga2.distance(gb) if not ga2.is_empty else float("inf")
    d2 = ga.distance(gb2) if not gb2.is_empty else float("inf")
    return (ga2, gb, d1) if d1 <= d2 else (ga, gb2, d2)


def cap_rating(fp: Footprint) -> tuple[Optional[float], str]:
    """A capacitor's rated voltage: its Rated Voltage (or Voltage) field, else a voltage in its value ("100n 50V",
    "10uF/16V"); (None, "") without one."""
    r = read_field(fp, "Rated Voltage")
    if r is not None and r.value is not None:
        return r.value, r.source
    m = _RATING.search(fp.value or "")
    if m:
        v = float(m.group(1).replace(",", ".")) * (1e3 if m.group(2).lower() == "k" else 1.0)
        return v, f"{fp.reference} value {fp.value}"
    return None, ""


def cap_kind(fp: Footprint) -> str:
    """ceramic, tantalum or electrolytic (aluminium), by its value, footprint and library names."""
    from ..components.defaults import is_electrolytic
    text = f"{fp.value} {fp.footprint_name} {fp.lib_id}".lower()
    if "tantal" in text:
        return "tantalum"
    return "electrolytic" if is_electrolytic(fp) else "ceramic"


def check_capacitors(board: Board, config, vm: Volts, safety, add: Add):
    """cap_voltage (see the module): the largest voltage across the capacitor (Volts.dv: a bootstrap capacitor sees
    the gate drive, not the rail)."""
    volts = vm.peak
    derating = {**CAP_DERATING, **(getattr(safety, "cap_derating", None) or {})}
    unrated = []
    for fp in board.footprints:
        model = config.model_for(fp.reference)
        if fp.dnp or model is None or model.kind != "cap":
            continue
        nets = sorted({p.net for p in fp.pads if p.net})
        if len(nets) != 2 or nets[0] not in volts or nets[1] not in volts:
            continue
        dv = vm.dv(nets[0], nets[1])
        rating, source = cap_rating(fp)
        if rating is None:
            if dv >= 10.0:
                unrated.append(fp.reference)
            continue
        kind = cap_kind(fp)
        limit = derating.get(kind, 0.8) * rating
        if dv <= limit:
            continue
        over = dv > rating
        add("cap_voltage", fp.reference,
            f"{fp.reference} ({fp.value}, {kind}) sees {dv:g} V between {nets[0]} and {nets[1]}: "
            + (f"over its {rating:g} V rating" if over else
               f"over {derating.get(kind, 0.8) * 100:.0f} % of its {rating:g} V rating (the {kind} derating)"),
            fp.position, layer=fp.layer, severity="error" if over else "warning", key=f"cap_voltage:{fp.reference}",
            details={"dv": dv, "rating_v": rating, "kind": kind, "source": source})
    if unrated:
        fp = board.footprint(unrated[0])
        add("cap_voltage", "capacitors",
            f"{len(unrated)} capacitor(s) at 10 V or more have no voltage rating to check ({', '.join(unrated[:12])}"
            + (" …" if len(unrated) > 12 else "")
            + "): give a Rated Voltage field, or the rating in the value (100n 50V)",
            fp.position, layer=fp.layer, severity="info", key="cap_voltage:unrated", details={"parts": unrated})


def is_esd(fp: Footprint) -> bool:
    """An ESD / TVS protection part, by its value, library or footprint name (ESD_WORDS)."""
    text = f"{fp.value} {fp.lib_id} {fp.footprint_name}".upper()
    return any(w in text for w in ESD_WORDS)


def check_esd(board: Board, safety, add: Add):
    """esd_protection (see the module): per connector, its signal pins with no ESD / TVS part on the same net
    within esd_mm."""
    reach = getattr(safety, "esd_mm", 10.0)
    named = set(getattr(safety, "esd_connectors", None) or ())
    guards: dict[str, list] = {}
    pads_on: dict[str, int] = {}
    for fp in board.footprints:
        for p in fp.pads:
            if p.net:
                pads_on[p.net] = pads_on.get(p.net, 0) + 1
                if not fp.dnp and is_esd(fp):
                    guards.setdefault(p.net, []).append(p.position)
    for fp in board.footprints:
        ref = fp.reference.upper()
        if fp.dnp or not ref.startswith(CONNECTOR_PREFIXES) \
                or not ref.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")[:1].isdigit():
            continue
        if ref.startswith("JP"):        # a (solder) jumper, not a connector
            continue
        if named and fp.reference not in named:
            continue
        # an unconnected pin (KiCad's "unconnected-(J1-Pad2)", or a net with no other pad) leads nowhere to protect
        bare = [p for p in fp.pads if p.net and not is_ground(p.net) and pads_on.get(p.net, 0) > 1
                and not p.net.rsplit("/", 1)[-1].lower().startswith("unconnected-")
                and not any(math.dist(p.position, g) <= reach for g in guards.get(p.net, ()))]
        if not bare:
            continue
        pins = ", ".join(f"{p.pin} ({p.net.rsplit('/', 1)[-1]})" for p in bare[:8]) + (" …" if len(bare) > 8 else "")
        add("esd_protection", fp.reference,
            f"{fp.reference}: {len(bare)} pin(s) with no ESD / TVS part on their net within {reach:g} mm: {pins}",
            fp.position, layer=fp.layer, severity="warning" if named else "info", key=f"esd:{fp.reference}",
            details={"pins": [p.pin for p in bare]})


def run_safety_checks(board: Board, config, copper, add: Add, off: frozenset = frozenset()):
    """The Run checks part (clearance_voltage, cap_voltage, esd_protection), each unless `off` names it (switched
    off in Preferences: not run at all, the clearance check being the slow one)."""
    safety = getattr(config, "safety", None)
    volts = voltage_model(board, config) if {"clearance_voltage", "cap_voltage"} - set(off) else None
    if "clearance_voltage" not in off:
        check_clearances(board, copper, volts, safety, add)
    if "cap_voltage" not in off:
        check_capacitors(board, config, volts, safety, add)
    if "esd_protection" not in off:
        check_esd(board, safety, add)


def resistor_rating(fp: Footprint) -> tuple[Optional[float], str]:
    """A resistor's rated power (W): its Rated Power (or Power) field, else by its package size in the footprint
    name."""
    r = read_field(fp, "Rated Power")
    if r is not None and r.value is not None:
        return r.value, r.source
    name = f"{fp.footprint_name} {fp.lib_id}"
    for size, watts in RESISTOR_WATTS:
        if size in name:
            return watts, f"{fp.reference} {size} package"
    return None, ""


def part_losses(result) -> dict[str, tuple[float, str]]:
    """{part: (loss W, where)} of the series parts in the solved DC paths (every stage together where combined,
    each stage's own paths, DC loads) and the motor bridges' RMS currents; the largest loss of a part kept."""
    out: dict[str, tuple[float, str]] = {}

    def put(name, w, where):
        """Keep the larger loss."""
        if name not in out or w > out[name][0]:
            out[name] = (w, where)
    paths = []
    combined = getattr(result, "dc_combined", None)
    if combined is not None:
        paths += [(p, "all stages together") for p in getattr(combined, "dc", [])]
    for r in list(getattr(result, "converters", ())) + list(getattr(result, "dc_loads", ())):
        paths += [(p, f"{r.net}: {p['name']}") for p in getattr(r, "dc", [])]
    for p, where in paths:
        for part in p.get("parts", ()):
            put(part["name"], p["current_a"] ** 2 * part["r_mohm"] * 1e-3, where)
    for b in getattr(result, "bridges", ()):
        for part in (getattr(b, "rms", None) or {}).get("parts", ()):
            put(part["name"], part["rms_a"] ** 2 * part["r_ohm"], f"{b.net} (RMS)")
    return out


def after_simulate(board: Board, config, result) -> list:
    """The Simulate part (resistor_power, vds_rating) as issues."""
    from ..core.issues import Issue, resolve_severity
    safety = getattr(config, "safety", None)
    rules = config.rules
    issues = []

    def add(check, net, message, loc, severity=None, **kw):
        """An issue unless its check is off."""
        sev = resolve_severity(check, rules.severity, severity)
        if sev != "off":
            issues.append(Issue(check, sev, net, message, (round(loc[0], 4), round(loc[1], 4)), **kw))
    derate = getattr(safety, "resistor_derating", 0.5)
    unrated = []
    for name, (w, where) in part_losses(result).items():
        fp = board.footprint(name)
        model = config.model_for(name)
        if fp is None or model is None or model.kind != "resistor":
            continue
        # a resistor model on another part (a pass FET's R_DS(on), Q601 on refboard) has no power rating
        if not (re.match(r"R\d", fp.reference) or "resistor" in f"{fp.lib_id}".lower()):
            continue
        rating, source = resistor_rating(fp)
        if rating is None and w >= UNRATED_FROM_W:
            unrated.append((name, w))
        if rating is None or w <= derate * rating:
            continue
        over = w > rating
        add("resistor_power", name,
            f"{name} ({fp.value}) dissipates {w:.3g} W ({where}): " + (f"over its {rating:.3g} W rating" if over else
                                                                     f"over {derate * 100:.0f} % of its {rating:.3g} W "
                                                                     "rating"),
            fp.position, layer=fp.layer, severity="error" if over else "warning", key=f"resistor_power:{name}",
            details={"loss_w": w, "rating_w": rating, "source": source})
    if unrated:
        unrated.sort(key=lambda x: -x[1])
        fp = board.footprint(unrated[0][0])
        add("resistor_power", "resistors",
            f"{len(unrated)} resistor(s) dissipating {UNRATED_FROM_W * 1e3:g} mW or more have no power rating to check "
            "(" + ", ".join(f"{n} {w:.2g} W" for n, w in unrated[:8]) + (" …" if len(unrated) > 8 else "")
            + "): give a Rated Power field", fp.position, layer=fp.layer, severity="info",
            key="resistor_power:unrated", details={"parts": [n for n, _ in unrated]})
    vds_derate = getattr(safety, "vds_derating", 0.8)
    for c in getattr(result, "converters", ()):
        spec = next((s for s in config.stages() if s.name == c.net), None)
        if spec is None or c.v_in is None or c.error:
            continue
        overshoot = max(c.overshoot_v or 0.0, (c.ringing or {}).get("overshoot_v", 0.0) or 0.0,
                        (getattr(c, "recovery", None) or {}).get("overshoot_v", 0.0) or 0.0)
        rail_v = spec.v_out if spec.topology == "boost" and spec.v_out else c.v_in     # what the node swings to
        peak = rail_v + overshoot
        refs = list(spec.high_side or spec.switch) + list(spec.low_side)
        ratings = [r.value for r in (read_field(board.footprint(x), "Vds") for x in refs if board.footprint(x))
                   if r is not None and r.value]
        if not ratings:
            continue
        vds = min(ratings)
        if peak <= vds_derate * vds:
            continue
        fp = board.footprint(refs[0])
        over = peak > vds
        add("vds_rating", spec.name,
            f"{spec.name}: the switch node peaks at {peak:.1f} V ({rail_v:g} V + {overshoot:.1f} V overshoot): "
            + (f"over the switches' {vds:g} V rating" if over else
               f"over {vds_derate * 100:.0f} % of the switches' {vds:g} V rating"),
            fp.position if fp else (0.0, 0.0), layer=fp.layer if fp else "", severity="error" if over else "warning",
            key=f"vds:{spec.name}", details={"peak_v": peak, "vds": vds})
    return issues
