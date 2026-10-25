"""Each ceramic capacitor at its own DC voltage: the voltage across it, and its capacitance there from its DC-bias
curve (components/bias.py).

A part's Cbias (one value, a point or a curve) used to replace C wherever the part sits; the same 100 nF 100 V X7R
on the 55 V rail and at a 3.3 V ADC input are not the same capacitance (the user: "cbias is saved as a single value,
this is probably not ideal as it will be used with different DC voltages"). For a run, at_dc gives every capacitor
with a curve its capacitance at the DC voltage across it, in a copy of the settings, so every solver that reads
ComponentModel.capacitance() sees each copy's own value.

The voltage across a capacitor (cap_voltages) is the worst (largest) DC voltage between its two nets: between two nets
safety.voltage_model knows (grounds, signed rail names, PDN rails, the power stages' and bridges' voltages, the
settings' `safety: voltages:`, through series parts; a bootstrap capacitor the gate drive), their difference; else
the board's linear DC operating point (dc_operating_point: resistors, links and op-amp channels with the known nets
and low-voltage ICs' signal pins as sources, each over its range; the user, of C304 at the ADC input: "Worst case the
opamp is a rail-to-rail IC so in this case it would be 3.3 V"), the largest end of the range of the difference; else
unknown (no DC path: 0 V, the nominal C, listed in a note). The derated value is the small-signal capacitance at that
operating point, as the makers measure it [Murata 2022] (1 kHz, 0.5-1 Vrms, 0 V to the rated voltage, 25 °C): right
for the AC sweeps and the small-signal step, not a large-signal charge model (a capacitor swinging across its curve,
as a snubber across a switch node) [A56]. Less capacitance is the pessimistic side for a filter's corner (the
bandwidth checks) and for decoupling (the PDN and conducted estimates); for a control loop it is not a worst case
(refboard's conv1 crossed higher, with more phase margin) (audit round 7, AA-12)."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, replace
from typing import Optional

from ..components.bias import c_at, check_points, parse_bias, resolve
from ..components.fields import field_text
from ..core.board import Board
from .safety import Volts, cap_kind, cap_rating, voltage_model

CLASS_ONE = re.compile(r"\b(C0G|COG|NP0|NPO)\b", re.I)    # class I ceramics: no DC-bias loss
DEFAULT_SHARE = 0.5         # a Cbias without its voltage holds at this share of the rated voltage [A57]
# an IC's pins are bounded by its supply only when that supply is a logic or analog rail (op-amps to ±15 V, MCUs,
# ADCs: their pins are clamped to it). A high-voltage controller's pins (soft-start, compensation, its internal
# regulator) sit on its internal rails, far below its input: refboard's LTC7890 on 55 V gave its 100 nF soft-start
# capacitor 6 nF under that bound [A58]
IC_SUPPLY_MAX = 18.0


@dataclass
class CapBias:
    """One capacitor at its DC voltage: the nets across it, the voltage (V) and where it comes from, its nominal C
    and the capacitance used, and how the curve was read ("curve", "default voltage": half the rating, "flat": one
    value used as it is, "class I")."""
    ref: str
    nets: tuple
    volts: float
    why: str
    c0: float
    c: float
    how: str
    curve: str = ""

    def summary(self) -> dict:
        """The row as JSON data (the report, the Parts tab)."""
        return {"ref": self.ref, "nets": list(self.nets), "v": round(self.volts, 4), "why": self.why,
                "c0": self.c0, "c": self.c, "how": self.how, "curve": self.curve}


def is_class_one(fp) -> bool:
    """A class I ceramic (C0G / NP0): its Dielectric field, else its value or description."""
    text = " ".join(t for t in (field_text(fp, "Dielectric"), fp.value, fp.fields.get("Description", "")) if t)
    return bool(CLASS_ONE.search(text))


def _supply_pins(fp) -> set:
    """The nets on a part's supply pins (schematic pin type power_in / power_out, or an op-amp's V+ / VCC / VDD ...
    by name: analog_net.pin_role)."""
    from .analog_net import pin_role
    return {p.net for p in fp.pads if p.net and (p.pin_type in ("power_in", "power_out")
                                                  or (pin_role(p.pin_function or "") or ("",))[0] == "supply")}


LINK_OHMS = 1e-3            # a short, an inductor or a ferrite in the DC network: its DCR, at least this
OUTPUT_PINS = ("output", "tri_state", "bidirectional", "open_collector", "open_emitter")


@dataclass
class DcPoint:
    """The linear DC operating point of a board's unknown nets (dc_operating_point): each net's voltage as a
    combination of its sources, `coef` {net: {source: coefficient}}, the sources' ranges `src` {source: (lo, hi, why)};
    `floating` the nets with no DC path to a source."""
    coef: dict
    src: dict
    floating: set

    def row(self, net: str) -> Optional[dict]:
        """A net's voltage as {source: coefficient} (a source net is itself), None when it is not known."""
        if net in self.src:
            return {net: 1.0}
        return self.coef.get(net)

    def interval(self, row: dict) -> tuple[float, float]:
        """The range a combination of sources spans, each source anywhere in its range (superposition)."""
        lo = hi = 0.0
        for s, k in row.items():
            a, b = k * self.src[s][0], k * self.src[s][1]
            lo, hi = lo + min(a, b), hi + max(a, b)
        return lo, hi


def _sources(board: Board, config, vm: Volts, amp_pads: set) -> dict:
    """The DC network's sources {net: (lo, hi, why)}: every net voltage_model knows, at its range (a switch node 0 …
    its rail), and each signal pin that drives or may drive (pin types OUTPUT_PINS, or none given) of an IC supplied
    at IC_SUPPLY_MAX or less, anywhere from its lowest supply (0 V at most) to its highest (rail to rail) [A58];
    the op-amp channels' own pins (amp_pads) are modelled, not sources."""
    src: dict = {}
    for n in board.nets:
        if n in vm.peak:
            src[n] = (min(vm.lo(n), vm.hi(n)), max(vm.lo(n), vm.hi(n)), vm.peak[n][1])
    for fp in board.footprints:
        supplies = _supply_pins(fp)
        volts = [(vm.lo(n), vm.hi(n)) for n in supplies if n in vm.peak]
        if fp.dnp or not volts:
            continue
        lo_s = min(0.0, min(a for a, _ in volts))
        hi_s = max(b for _, b in volts)
        if max(abs(lo_s), abs(hi_s)) > IC_SUPPLY_MAX or hi_s <= 0:
            continue
        for p in fp.pads:
            if (not p.net or p.net in supplies or p.net in vm.peak or (fp.reference, p.number) in amp_pads
                    or (p.pin_type and p.pin_type not in OUTPUT_PINS)):
                continue
            a, b, _ = src.get(p.net, (lo_s, hi_s, ""))
            src[p.net] = (min(a, lo_s), max(b, hi_s),
                          f"{fp.reference} pin {p.number} within its supply ({lo_s:g} … {hi_s:g} V)")
    return src


def dc_operating_point(board: Board, config, vm: Volts) -> DcPoint:
    """The DC operating point of the board's linear network [Ho 1975] [A58]: resistors, shorts / inductors / ferrites
    (their DCR, at least LINK_OHMS) and op-amp channels (an amplifier part's, analog_net.opamp_channels: a VCVS of its
    open-loop gain A0 behind R_O, as Circuit.opamp at DC); capacitors are open at DC. The sources: _sources. Solved by
    superposition: each unknown net's voltage as a sum over the sources (one solve with one right-hand side per
    source), so the voltage across a capacitor is an exact range of its two ends' difference (a divider's midpoint 6 V
    of 12 V; a capacitor across a feedback resistor ~0 V). Nets with no DC path to a source are left out (`floating`).
    Audit round 7, AA-1: the bounds before took each net on its own (a divider's midpoint 12 V, a feedback capacitor
    the op-amp's whole supply)."""
    import numpy as np
    from .analog_net import AMP_R_O, opamp_channels
    channels, amp_pads = [], set()
    for fp in board.footprints:
        m = config.model_for(fp.reference)
        if fp.dnp or m is None or m.kind != "amplifier":
            continue
        for ch in opamp_channels(fp, m):
            channels.append(ch)
            amp_pads |= {(fp.reference, p.number) for p in (ch.inp, ch.inn, ch.out) if p is not None}
    src = _sources(board, config, vm, amp_pads)
    links = []                                            # (net a, net b, conductance)
    for fp in board.footprints:
        m = config.model_for(fp.reference)
        nets = sorted({p.net for p in fp.pads if p.net})
        if fp.dnp or m is None or len(nets) != 2:
            continue
        if m.kind == "resistor" and m.get("R", 0.0) > 0:
            links.append((nets[0], nets[1], 1.0 / max(m.get("R"), LINK_OHMS)))
        elif m.kind in ("short", "inductor", "ferrite"):
            links.append((nets[0], nets[1], 1.0 / max(m.get("R", 0.0), LINK_OHMS)))
    # each op-amp channel: an internal node v_i = A0·(v+ − v−) (a gain block: gain·v+), R_O from it to the output
    amps = []
    for k, ch in enumerate(channels):
        if not ch.out.net or not ch.inp.net:
            continue
        prm = ch.params
        if ch.inn is None:
            a0 = float(prm.get("gain") or 1.0)
        else:
            a0 = 10 ** (prm["aol"] / 20) if prm.get("aol") else 1e5
        amps.append((f"#amp{k}", ch.out.net, ch.inp.net, ch.inn.net if ch.inn is not None else None, a0,
                     1.0 / max(prm.get("z_out") or AMP_R_O, LINK_OHMS)))
    nodes = sorted({n for a, b, _ in links for n in (a, b) if n not in src}
                   | {n for _, out, inp, inn, _, _ in amps for n in (out, inp, inn) if n and n not in src}
                   | {a[0] for a in amps})
    # nets with no DC path to a source are left out: a capacitor on one sees an undefined, leakage-set voltage
    parent = {n: n for n in nodes}

    def find(n):
        """Union-find root."""
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    driven = set()
    for a, b, _ in links:
        for x, y in ((a, b), (b, a)):
            if x in parent and y in src:
                driven.add(x)
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    for node, out, inp, inn, _, _ in amps:
        for x in (out, inp, inn):
            if x in parent:
                parent[find(x)] = find(node)
        if any(x in src for x in (out, inp, inn) if x):
            driven.add(node)
    alive = {find(n) for n in driven}
    keep = [n for n in nodes if find(n) in alive]
    floating = {n for n in nodes if find(n) not in alive and not n.startswith("#amp")}
    if not keep:
        return DcPoint({}, src, floating)
    idx = {n: i for i, n in enumerate(keep)}
    sidx = {s: j for j, s in enumerate(sorted(src))}
    mat = np.zeros((len(keep), len(keep)))
    rhs = np.zeros((len(keep), len(sidx)))
    for a, b, g in links:                                 # KCL at each unknown net [Ho 1975]
        for x, y in ((a, b), (b, a)):
            if x in idx:
                mat[idx[x], idx[x]] += g
                if y in idx:
                    mat[idx[x], idx[y]] -= g
                elif y in sidx:
                    rhs[idx[x], sidx[y]] += g
    for node, out, inp, inn, a0, g_o in amps:
        if node not in idx:
            continue
        i = idx[node]                                     # v(node) − A0·(v+ − v−) = 0
        mat[i, i] = 1.0
        for net, sign in ((inp, -a0), (inn, a0)):
            if net is None:
                continue
            if net in idx:
                mat[i, idx[net]] += sign
            elif net in sidx:
                rhs[i, sidx[net]] -= sign
        if out in idx:                                    # R_O from the internal node into the output net
            mat[idx[out], idx[out]] += g_o
            mat[idx[out], i] -= g_o
    try:
        x = np.linalg.solve(mat, rhs)
    except np.linalg.LinAlgError:                         # a singular block (an op-amp without feedback): least squares
        x = np.linalg.lstsq(mat, rhs, rcond=None)[0]
    coef = {n: {s: float(x[idx[n], j]) for s, j in sidx.items() if abs(x[idx[n], j]) > 1e-12}
            for n in keep if not n.startswith("#amp")}
    return DcPoint(coef, src, floating)


def cap_voltages(board: Board, config, vm: Optional[Volts] = None) -> dict:
    """{ref: (V, why, (net a, net b))}: the worst DC voltage across each two-net capacitor (see the module): between
    two nets voltage_model knows, its Volts.dv (a bootstrap capacitor the gate drive, a switch node's swing); else the
    DC operating point's range of the difference (dc_operating_point), its larger end, at most the board's span of
    voltages (an op-amp's linear output can run past its rails). V None when a side has no DC path to a source."""
    vm = vm or voltage_model(board, config)
    dc = dc_operating_point(board, config, vm)
    span = [v for s in dc.src.values() for v in s[:2]] or [0.0]
    v_max = max(span) - min(span)
    out = {}
    bound = None                    # the IC-driven nets' supply ranges, for a side the operating point can't place
    for fp in board.footprints:
        m = config.model_for(fp.reference)
        nets = tuple(sorted({p.net for p in fp.pads if p.net}))
        if fp.dnp or m is None or m.kind != "cap" or len(nets) != 2:
            continue
        if nets[0] in vm.peak and nets[1] in vm.peak:
            why = "; ".join(dict.fromkeys(w for n in nets for w in [vm.peak[n][1]] if w and w != "ground"))
            out[fp.reference] = (vm.dv(*nets), why or "ground", nets)
            continue
        ra, rb = dc.row(nets[0]), dc.row(nets[1])
        if ra is None or rb is None:
            # a side with no DC path to a source: bounded by an IC that drives it, within its supply (rail to rail
            # [A58]) when there is one: an op-amp whose input the operating point cannot place (its + input on a net
            # with no DC path) still drives its output within its supply; since op-amps known by their pin names
            # became amplifiers (channels, not sources, in the operating point) that bound was lost (test_bias)
            if bound is None:
                bound = _sources(board, config, vm, set())
            ends = [dc.interval(r) if r is not None else bound.get(n) for n, r in ((nets[0], ra), (nets[1], rb))]
            if any(e is None for e in ends):
                out[fp.reference] = (None, "voltage unknown", nets)
                continue
            (alo, ahi), (blo, bhi) = ends[0][:2], ends[1][:2]
            dv = min(max(abs(ahi - blo), abs(alo - bhi)), v_max)
            why = "; ".join(bound[n][2] for n, r in ((nets[0], ra), (nets[1], rb)) if r is None)
            out[fp.reference] = (dv, why, nets)
            continue
        diff = {s: ra.get(s, 0.0) - rb.get(s, 0.0) for s in set(ra) | set(rb)}
        lo, hi = dc.interval(diff)
        dv = min(max(abs(lo), abs(hi)), v_max)
        top = sorted(((abs(k) * max(abs(dc.src[s][0]), abs(dc.src[s][1])), s) for s, k in diff.items()),
                     reverse=True)[:2]
        # the sources that set it most: their nets, an IC pin's with the pin it comes from
        names = ", ".join(s.rsplit("/", 1)[-1] + (f": {dc.src[s][2]}" if " pin " in dc.src[s][2] else "")
                          for w, s in top if w > 1e-6)
        out[fp.reference] = (dv, f"DC operating point, {lo:.3g} … {hi:.3g} V" + (f" (from {names})" if names else ""),
                             nets)
    return out


def part_bias(fp, m, entry) -> tuple:
    """One capacitor at its DC voltage: (CapBias or None, problems). `entry` is its cap_voltages row (V or None,
    why, nets). The CapBias's c is the capacitance a run uses (c0 for class I, tantalum and electrolytic parts; the
    flat value of a single Cbias without a voltage or rating); None (no row) for a part without a curve."""
    if m is None or m.kind != "cap" or not m.bias:
        return None, []
    c0 = m.get("C")
    points, bad = parse_bias(m.bias)
    bad += check_points(points, c0)
    if bad:
        return None, bad
    v, why, nets = entry or (None, "not a two-net capacitor", ())
    if is_class_one(fp) or cap_kind(fp) != "ceramic":
        return CapBias(fp.reference, nets, v or 0.0, why, c0, c0,
                       "class I" if is_class_one(fp) else cap_kind(fp), m.bias), []
    rating, _ = cap_rating(fp)
    curve, how = resolve(points, c0, DEFAULT_SHARE * rating if rating else None)
    # one value, no voltage, no rating: used as it is (as before); else the curve at the voltage (0 V: unknown)
    c = points[0].capacitance(c0) if how == "flat" else c_at(curve, c0, v or 0.0)
    return CapBias(fp.reference, nets, v or 0.0, why if v is not None else "voltage unknown", c0, c, how, m.bias), []


def at_dc(board: Board, config) -> tuple:
    """(a copy of the settings with every capacitor that has a DC-bias curve at its own DC voltage, [CapBias], notes)
    for a run. The copy's models carry params C_bias = the curve at the voltage across the part (components/bias.
    c_at); a single value without a voltage is taken at half the Rated Voltage [A57], or, without a rating, as it is;
    class I ceramics, tantalum and electrolytic capacitors keep their nominal C. The settings themselves are not
    changed."""
    rows, notes, unknown, problems = [], [], [], []
    caps = {ref: e for ref, e in config.components.items()
            if not e.removed and e.model.kind == "cap" and e.model.bias}
    if not caps:
        return config, rows, notes
    volts = cap_voltages(board, config)
    comps = dict(config.components)
    for ref, e in caps.items():
        fp = board.footprint(ref)
        if fp is None:
            continue
        row, bad = part_bias(fp, e.model, volts.get(ref))
        if bad:
            problems.append(f"{ref}: {'; '.join(bad)}")
            continue
        rows.append(row)
        if row.how not in ("flat", "class I", "tantalum", "electrolytic") and volts.get(ref, (None,))[0] is None:
            unknown.append(ref)
        if row.how in ("class I", "tantalum", "electrolytic"):
            model = replace(e.model, params={k: v for k, v in e.model.params.items() if k != "C_bias"}, bias="")
        else:
            model = replace(e.model, params={**e.model.params, "C_bias": row.c})
        comps[ref] = replace(e, model=model)
    out = copy.copy(config)
    out.components = comps
    if unknown:
        notes.append(f"{len(unknown)} capacitor(s) with a DC-bias curve on a net of unknown voltage, taken at 0 V "
                     f"(their nominal C): {', '.join(sorted(unknown)[:12])}{' …' if len(unknown) > 12 else ''}; "
                     "give the net its voltage under the settings' safety: voltages")
    defaulted = sorted(r.ref for r in rows if r.how == "default voltage")
    if defaulted:
        notes.append(f"{len(defaulted)} capacitor(s) have a Cbias without its voltage, taken at half their Rated "
                     f"Voltage [A57]: {', '.join(defaulted[:12])}{' …' if len(defaulted) > 12 else ''}; give the "
                     "voltage (Cbias Voltage, or 75nF@50V) or the curve (Cbias Curve)")
    notes += [f"DC-bias curve not used: {p}" for p in problems]
    return out, rows, notes
