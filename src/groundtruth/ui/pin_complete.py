"""Pin completion for the DC loads form (the user: "can you auto complete the source pin source return as well as the
loads?"): the board's pins as REF.PAD with their net and part, ranked for what is being typed where.

- Source pin: connector pins on a supply-looking net first, then every other pin off ground, ground pins last.
- Source return: the source part's own ground pins first, then every ground pin.
- Loads, one PIN/RETURN=CURRENT per line: before the "/" the pins on the source pin's net (the rail), connectors
  first and pins already listed left out; after it the load part's own pin on the source return's net (else on a
  ground net). After the "=" (the current) nothing is offered.

Typed text matches a pin from its start (case ignored: "j1" finds J1.2) or, from two letters on, its net ("vdd"
finds every pin on /VDD_3V3). No wx here: the tests run it, the panel (dcloads_panel.PinPopup) shows it. In ui/ on
purpose: the servers' code fingerprint (protocol.code_id) leaves ui/ out, so this window-only help needs no server
update.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..analysis.dcload import CONNECTOR_PREFIXES

SHOWN_MAX = 200                 # the popup's longest list (it scrolls; typing narrows it)
SUPPLY_WORDS = ("VDD", "VCC", "VIN", "VBAT", "VBUS", "VSYS", "PWR", "POWER", "SUPPLY", "+")


@dataclass(frozen=True)
class Pin:
    """One pin of the board: REF.PAD, its part's reference and value, its net."""
    pin: str
    ref: str
    value: str
    net: str

    def label(self) -> str:
        """The popup's line: the pin, its net and the part's value."""
        return f"{self.pin}    {self.net.lstrip('/')}" + (f"  ·  {self.value}" if self.value else "")


@dataclass
class Completion:
    """What the popup offers for the caret's place: the token's columns in its line (replaced by the chosen pin),
    the pins to offer, best first, and what follows a chosen pin (a load's "/" or "=", so typing goes on)."""
    start: int
    end: int
    pins: list = field(default_factory=list)
    suffix: str = ""


def _natural(text: str) -> tuple:
    """A sort key with numbers as numbers (J2 before J10, J1.2 before J1.10)."""
    return tuple(int(t) if t.isdigit() else t.upper() for t in re.split(r"(\d+)", text))


def is_ground(net: str) -> bool:
    """A net that looks like a ground or return (GND, AGND, VSS, 0V, ...RTN)."""
    name = net.upper().rsplit("/", 1)[-1]
    return "GND" in name or "VSS" in name or name in ("0V", "0", "RTN", "RETURN") or name.endswith("_RTN")


def is_supply(net: str) -> bool:
    """A net that looks like a supply rail (VDD, +5V, VBAT, ...)."""
    name = net.upper().rsplit("/", 1)[-1]
    return not is_ground(net) and (name.startswith(SUPPLY_WORDS) or bool(re.match(r"^\+?\d+V\d*", name)))


def is_connector(ref: str) -> bool:
    """A reference that names a connector (J1, P3, CN2, X1, ...), as Fill from net… tells them."""
    ref = ref.upper()
    return ref.startswith(CONNECTOR_PREFIXES) and ref.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")[:1].isdigit()


def pin_table(board) -> list:
    """Every connected pin of the board's fitted parts, in reference order (one entry per REF.PAD: a part's
    paralleled pads of one number are one pin to the settings)."""
    seen, out = set(), []
    for fp in board.footprints:
        if fp.dnp:
            continue
        for p in fp.pads:
            if not p.net or not p.number or p.pin in seen:
                continue                    # an unconnected pad carries no current; a mounting hole has no number
            seen.add(p.pin)
            out.append(Pin(p.pin, fp.reference, fp.value, p.net))
    return sorted(out, key=lambda p: _natural(p.pin))


def _ref_of(pin: str) -> str:
    """REF of a REF.PAD."""
    return pin.rsplit(".", 1)[0] if "." in pin else pin


def _rank(role: str, p: Pin, net_of: dict, source: str, source_return: str, load_pin: str) -> int:
    """How well a pin fits the field (lower is better; see the module)."""
    if role == "source":
        if is_ground(p.net):
            return 3
        return (0 if is_connector(p.ref) else 1) if is_supply(p.net) else 2
    if role == "source_return":
        if not is_ground(p.net):
            return 3
        return 0 if p.ref == _ref_of(source) else (1 if is_connector(p.ref) else 2)
    if role == "load":
        rail = net_of.get(source.upper())
        if rail:
            return (0 if is_connector(p.ref) else 1) if p.net == rail else 3
        return (0 if is_connector(p.ref) else 1) if is_supply(p.net) else 3
    # the load's return: its own part's pin on the source return's net (else a ground net) first
    ret = net_of.get(source_return.upper())
    on_return = p.net == ret if ret else is_ground(p.net)
    if not on_return:
        return 3
    return 0 if p.ref == _ref_of(load_pin) else (1 if is_connector(p.ref) else 2)


def offer(table: list, role: str, typed: str, source: str = "", source_return: str = "", load_pin: str = "",
          skip: tuple = ()) -> list:
    """The pins to offer for `typed` in a field of `role` (source, source_return, load, load_return), best first:
    see the module. `skip`: pins already used (the source and the loads listed)."""
    t = typed.strip().upper()
    net_of = {p.pin.upper(): p.net for p in table}
    skip_up = {s.upper() for s in skip}
    hits = []
    for p in table:
        if p.pin.upper() in skip_up:
            continue
        starts = p.pin.upper().startswith(t)
        if not starts and not (len(t) >= 2 and t in p.net.upper()):
            continue
        hits.append(((not starts, _rank(role, p, net_of, source, source_return, load_pin), _natural(p.pin)), p))
    hits.sort(key=lambda h: h[0])
    return [p for _, p in hits[:SHOWN_MAX]]


def complete_field(table: list, role: str, text: str, col: int, source: str = "",
                   source_return: str = "") -> Optional[Completion]:
    """A one-line field (Source pin, Source return): the whole text is the token; offered for what is left of the
    caret. None when nothing fits, or only the pin already typed."""
    pins = offer(table, role, text[:col], source=source, skip=(source,) if role == "source_return" else ())
    if not pins or (len(pins) == 1 and pins[0].pin.upper() == text.strip().upper()):
        return None
    return Completion(0, len(text), pins)


def complete_load_line(table: list, line: str, col: int, source: str, source_return: str,
                       listed: tuple = ()) -> Optional[Completion]:
    """A line of the Loads field, PIN/RETURN=CURRENT, with the caret at `col`: the load's pin before the "/", its
    return after it, nothing in the current. `listed`: the other lines' load pins (not offered again)."""
    eq = line.find("=")
    if 0 <= eq < col:
        return None                                 # typing the current
    head_end = eq if eq >= 0 else len(line)
    slash = line.find("/", 0, head_end)
    if 0 <= slash < col:                            # the return pin
        start, end, role = slash + 1, head_end, "load_return"
        load_pin = line[:slash].strip()
        pins = offer(table, role, line[start:col], source, source_return, load_pin, skip=(source, load_pin))
        suffix = "" if eq >= 0 else "="
    else:                                           # the load's pin
        start, end, role = 0, slash if slash >= 0 else head_end, "load"
        load_pin = ""
        pins = offer(table, role, line[:col], source, source_return, skip=(source,) + tuple(listed))
        suffix = "" if slash >= 0 or eq >= 0 else ("/" if source_return.strip() else "=")
    typed = line[start:end].strip().upper()
    if not pins or (len(pins) == 1 and pins[0].pin.upper() == typed):
        return None
    return Completion(start, end, pins, suffix)
