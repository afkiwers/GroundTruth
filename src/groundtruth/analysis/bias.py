"""Each ceramic capacitor at its own DC voltage: the voltage across it, and its capacitance there from its DC-bias
curve (components/bias.py).

A part's Cbias (one value, a point or a curve) used to replace C wherever the part sits; the same 100 nF 100 V X7R
on the 55 V rail and at a 3.3 V ADC input are not the same capacitance (the user: "cbias is saved as a single value,
this is probably not ideal as it will be used with different DC voltages"). For a run, at_dc gives every capacitor
with a curve its capacitance at the DC voltage across it, in a copy of the settings, so every solver that reads
ComponentModel.capacitance() sees each copy's own value.

The voltage across a capacitor is the worst (highest) DC voltage between its two nets (cap_voltages):
safety.voltage_model's (grounds, rail names, PDN rails, the power stages' and bridges' voltages, the settings'
`safety: voltages:`, through series parts; a bootstrap capacitor the gate drive); a net it does not know is at most
the highest supply of the ICs whose signal pins are on it (the user, of C304 at the ADC input: "Worst case the opamp
is a rail-to-rail IC so in this case it would be 3.3 V"), else at most the highest known net one resistor away (a
divider's midpoint), else unknown (0 V: the nominal C, listed in a note). Worst case on purpose: less capacitance
raises a filter's corner (the bandwidth checks stay on the safe side) and weakens decoupling (the PDN and conducted
estimates stay pessimistic)."""
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
# regulator) sit on its internal rails, far below its input: kraft's LTC7890 on 55 V gave its 100 nF soft-start
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


def bounds(board: Board, config, vm: Volts) -> dict:
    """{net: (V, why)} for the nets voltage_model does not know: at most the highest supply of an IC (a part with
    supply pins, that supply at most IC_SUPPLY_MAX [A58]) whose other pins are on it (an op-amp, an MCU's ADC pin: a
    rail-to-rail worst case), then at most the highest known (or so bounded) net one resistor away (a divider's
    midpoint). Each such net lies anywhere from 0 V to its bound (cap_voltages)."""
    out: dict = {}
    for fp in board.footprints:
        supplies = _supply_pins(fp)
        known = [(vm.hi(n), n) for n in supplies if n in vm.peak and vm.hi(n) > 0]
        if fp.dnp or not known or max(known)[0] > IC_SUPPLY_MAX:
            continue
        v, rail = max(known)
        for p in fp.pads:
            if p.net and p.net not in supplies and p.net not in vm.peak and v > out.get(p.net, (-1.0,))[0]:
                out[p.net] = (v, f"at most {fp.reference}'s supply {rail.rsplit('/', 1)[-1]} ({v:g} V)")
    for fp in board.footprints:
        m = config.model_for(fp.reference)
        nets = sorted({p.net for p in fp.pads if p.net})
        if fp.dnp or m is None or m.kind != "resistor" or len(nets) != 2:
            continue
        for a, b in (nets, nets[::-1]):
            if a in vm.peak or a in out:
                continue
            v = vm.hi(b) if b in vm.peak else out.get(b, (None,))[0]
            if v is not None and v > out.get(a, (-1.0,))[0]:
                out[a] = (v, f"at most {b.rsplit('/', 1)[-1]} through {fp.reference} ({v:g} V)")
    return out


def cap_voltages(board: Board, config, vm: Optional[Volts] = None) -> dict:
    """{ref: (V, why, (net a, net b))}: the worst DC voltage across each two-net capacitor (see the module); V None
    when a side's voltage is unknown."""
    vm = vm or voltage_model(board, config)
    extra = bounds(board, config, vm)
    # a bounded net anywhere from 0 V to its bound: across a capacitor to a rail the worst case is the whole rail
    full = Volts({**extra, **vm.peak}, {**{n: 0.0 for n in extra}, **vm.lo_v}, vm.rides)
    out = {}
    for fp in board.footprints:
        m = config.model_for(fp.reference)
        nets = tuple(sorted({p.net for p in fp.pads if p.net}))
        if fp.dnp or m is None or m.kind != "cap" or len(nets) != 2:
            continue
        if nets[0] not in full.peak or nets[1] not in full.peak:
            out[fp.reference] = (None, "voltage unknown", nets)
            continue
        dv = full.dv(*nets)
        why = "; ".join(dict.fromkeys(w for n in nets for w in [full.peak[n][1]] if w and w != "ground"))
        out[fp.reference] = (dv, why or "ground", nets)
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
