"""Suggest a model for each footprint from its reference prefix, value and footprint name. The suggested values
(ESR and ESL by package, a ferrite's L from its impedance rating) are typical figures, not a part's own [A25]; the
user's settings and the part's fields replace them. Source keys: docs/REFERENCES.md."""
from __future__ import annotations

import math
import re

from ..core.board import Footprint
from .models import ComponentModel
from .values import parse_impedance_at, parse_value

# Equivalent series inductance by package (typical MLCC values, H)
_ESL_BY_PACKAGE = [
    ("01005", 0.2e-9), ("0201", 0.3e-9), ("0402", 0.4e-9), ("0603", 0.6e-9), ("0805", 0.8e-9),
    ("1206", 1.0e-9), ("1210", 1.2e-9), ("1812", 1.5e-9), ("2220", 1.8e-9),
]
DEFAULT_ESL = 1.0e-9
DEFAULT_MLCC_ESR = 0.02
ELECTROLYTIC_ESR = 0.1
ELECTROLYTIC_ESL = 3e-9
DEFAULT_INDUCTOR_DCR = 0.05
DEFAULT_FERRITE_DCR = 0.1


def ref_prefix(reference: str) -> str:
    """The letters of a reference ("C" of C12, also of logic:C12, a part of a stacked board)."""
    m = re.match(r"[A-Za-z_#]+", reference.rsplit(":", 1)[-1])
    return m.group(0).upper() if m else ""


def package_esl(footprint_name: str) -> float:
    """A capacitor's typical equivalent series inductance from the package size in its footprint's name
    ("C_0402_1005Metric" -> 0.4 nH): ESL grows with the body's length, the current loop through the part."""
    for code, esl in _ESL_BY_PACKAGE:
        if code in footprint_name:
            return esl
    return DEFAULT_ESL


def is_electrolytic(fp: Footprint) -> bool:
    """An aluminium or tantalum capacitor (no DC-bias loss), by reference prefix or footprint."""
    name = fp.footprint_name
    return ref_prefix(fp.reference) in ("CP", "CE") or "Elec" in name or "CP_" in name or "Tantalum" in name


def suggest_model(fp: Footprint, fields: bool = True) -> ComponentModel:
    """The model a part gets until the user sets one: from its reference prefix, value and footprint, and (fields)
    the parameters its symbol's fields give (components/fields.py), listed in its from_schematic."""
    model = _suggest(fp)
    if fields:
        from .fields import model_values
        values, _ = model_values(fp, model.kind)
        for param, reading in values.items():       # a field overrides the guess from value and footprint
            model.params[param] = reading.value
        model.from_schematic = list(values)
        # the main value came from a field: a "could not parse value" note about the Value text no longer applies
        if "C" in values or "L" in values or "R" in values:
            model.note = "" if model.note.startswith("could not parse") else model.note
    return model


def _suggest(fp: Footprint) -> ComponentModel:
    """The guess itself, in order: an op-amp (its Gain field or GT amp labels), DNP (open), a net tie (short),
    then by reference prefix: resistors, capacitors (ESR/ESL by kind and package), ferrites, inductors, fuses.
    Anything else (ICs, connectors, transistors ...) is "open": its pins are only ports of the analysis."""
    prefix = ref_prefix(fp.reference)
    if not fp.dnp and any(k.strip().lower() == "gain" and (v or "").strip() for k, v in fp.fields.items()):
        return ComponentModel("amplifier", note="an op-amp stage (its Gain field)")
    if not fp.dnp:
        from ..analysis.tags import amp_label_pins
        if amp_label_pins(fp):
            return ComponentModel("amplifier", note="an op-amp stage (its GT amp_in / amp_out labels)")
    value = fp.value or ""
    name = fp.footprint_name
    lib = fp.lib_id

    if fp.dnp:
        return ComponentModel("open", note="DNP")
    if "NetTie" in lib or "NetTie" in name:
        return ComponentModel("short", note="net tie")

    if prefix in ("R", "RN", "RA", "RS"):
        v = parse_value(value)
        if v is None:
            return ComponentModel("resistor", {}, note=f"could not parse value {value!r}")
        if v == 0:
            return ComponentModel("short", note="0R")
        return ComponentModel("resistor", {"R": v})

    if prefix in ("C", "CP", "CE"):
        v = parse_value(value)
        electrolytic = is_electrolytic(fp)
        esr = ELECTROLYTIC_ESR if electrolytic else DEFAULT_MLCC_ESR
        esl = ELECTROLYTIC_ESL if electrolytic else package_esl(name)
        params = {"ESR": esr, "ESL": esl}
        if v is not None and v > 0:
            params["C"] = v
            return ComponentModel("cap", params)
        return ComponentModel("cap", params, note=f"could not parse value {value!r}")

    if prefix in ("FB", "FL", "BEAD") or "Ferrite" in name or "Bead" in name:
        zf = parse_impedance_at(value)
        if zf:
            # "600R@100MHz": modelled as an inductor whose reactance is that impedance at that frequency,
            # L = Z / (2 pi f) (a bead is inductive below its peak; its resistive peak is not modelled)
            z, f = zf
            return ComponentModel("ferrite", {"R": DEFAULT_FERRITE_DCR, "L": z / (2 * math.pi * f)},
                                  note=f"{value}")
        return ComponentModel("ferrite", {"R": DEFAULT_FERRITE_DCR, "L": 1e-6}, note="assumed ~600R@100MHz")

    if prefix == "L":
        v = parse_value(value)
        if v is not None and v > 0:
            return ComponentModel("inductor", {"L": v, "R": DEFAULT_INDUCTOR_DCR})
        return ComponentModel("inductor", {"R": DEFAULT_INDUCTOR_DCR}, note=f"could not parse value {value!r}")

    if prefix == "F":  # fuse / polyfuse
        return ComponentModel("short", note="fuse")

    if fp.exclude_from_sim:
        return ComponentModel("open", note="excluded from simulation")

    # ICs, connectors, transistors, diodes, crystals, jumpers, test points: pins are ports only.
    return ComponentModel("open")
