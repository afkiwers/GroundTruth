"""Parse component value strings as people write them in schematics.

Examples: "4k7" -> 4700, "33R" -> 33, "1R5" -> 1.5, "100n" -> 1e-7, "22u0" -> 22e-6,
"10uF/16V" -> 1e-5, "4.7µF" -> 4.7e-6, "0R" -> 0, "600R@100MHz" -> (600, 1e8) via parse_impedance_at.
"""
from __future__ import annotations

import re
from typing import Optional

_MULT = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
    "R": 1.0, "r": 1.0, "Ω": 1.0, "E": 1.0, "F": 1.0, "H": 1.0, "": 1.0,
}
_NOT_A_VALUE = {"dnp", "nc", "n/c", "nf", "nostuff", "no_stuff", "~", ""}

# 4k7 / 1R5 / 22u0 style (multiplier used as decimal point)
_INFIX = re.compile(r"^(\d+)([pnuµμmkKMGRrΩE])(\d+)")
_FRACTION = re.compile(r"^(\d+)/(\d+)(?![\d.])(.*)$")     # "1/16W": a resistor's power rating as a fraction
# 4.7k / 100n / 33R / 10 / 1e-6 style
_PLAIN = re.compile(r"^(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*([pnuµμmkKMGRrΩE]?)")


def parse_value(text: str) -> Optional[float]:
    """Return the numeric value in base SI units, or None if it cannot be parsed."""
    if text is None:
        return None
    s = text.strip()
    if s.lower() in _NOT_A_VALUE:
        return None
    s = s.replace(",", ".").replace(" ", "")          # "4,7k" (decimal comma) and "4.7 k" read as 4.7k
    s = re.sub(r"(?i)ohms?$|ohms?(?=[/@_\s])", "R", s)   # "10ohm", "10 Ohms/1%": the word as the R suffix
    # The infix form first ("4k7": the multiplier is the decimal point), else a plain number with an optional
    # multiplier; anything after it ("uF/16V", "R 1%") is ignored: the leading value is what counts.
    m = _INFIX.match(s)
    if m:
        whole, mult, frac = m.groups()
        return float(f"{whole}.{frac}") * _MULT[mult]
    m = _PLAIN.match(s)
    if m:
        num, mult = m.groups()
        # "1e-6" is matched as the number; a lone "m"/"M" after a number is milli/mega.
        return float(num) * _MULT.get(mult, 1.0)
    return None


_PREFIX_WORDS = {"pico": 1e-12, "nano": 1e-9, "micro": 1e-6, "milli": 1e-3, "kilo": 1e3, "mega": 1e6, "giga": 1e9}
_PREFIX_SYMBOLS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6,
                   "G": 1e9}
# unit words and symbols (lower case) -> quantity; InvenTree reports parameters with pint's names ("3.8milliohm")
_UNITS = {"ohms": "ohm", "ohm": "ohm", "ω": "ohm", "r": "ohm", "farads": "F", "farad": "F", "f": "F",
          "henries": "H", "henrys": "H", "henry": "H", "h": "H", "coulombs": "C", "coulomb": "C", "c": "C",
          "volts": "V", "volt": "V", "v": "V", "amperes": "A", "ampere": "A", "amps": "A", "amp": "A", "a": "A",
          "hertz": "Hz", "hz": "Hz", "watts": "W", "watt": "W", "w": "W"}
_QUANTITY_NAMES = {"ohm": "a resistance", "F": "a capacitance", "H": "an inductance", "C": "a charge",
                   "V": "a voltage", "A": "a current", "Hz": "a frequency", "W": "a power", "s": "a time"}
# a time's unit ("20ns", "0.02us", "20 nanoseconds"): stripped before the number is read, only when a time is asked
# for (a lone "s" is not in _UNITS: "1.8mS" is a transconductance, siemens)
_SECONDS = re.compile(r"(?:seconds?|secs?|s)$", re.I)
_NUMBER = re.compile(r"^([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(.*)$")


def _unit_of(rest: str) -> Optional[tuple[float, str, str]]:
    """(prefix factor, quantity or "", what follows) of the text after a number, or None if it is not a unit."""
    factor = 1.0
    low = rest.lower()
    # a prefix written out ("milli") or as a symbol ("m"); but "m" of "mOhm" is a prefix while "h" of "henry"
    # or "f" of "farad" are units: unit words are excluded from the symbol test
    word = next((w for w in _PREFIX_WORDS if low.startswith(w)), None)
    if word:
        factor, rest = _PREFIX_WORDS[word], rest[len(word):]
    elif rest[:1] in _PREFIX_SYMBOLS and not rest.lower().startswith(("ohm", "hz", "henr", "farad", "coulomb")):
        factor, rest = _PREFIX_SYMBOLS[rest[0]], rest[1:]
    low = rest.lower()
    # longest unit name first, so "ohms" is not read as "ohm" + "s"
    unit = next((u for u in sorted(_UNITS, key=len, reverse=True) if low.startswith(u)), None)
    after = rest[len(unit):] if unit else rest
    if after[:1].isalpha():                     # "3.8 mOhmz", "21.2 Amps x": not a unit we know
        return None
    return factor, (_UNITS[unit] if unit else ""), after


def parse_quantity(text: str, quantity: str, assumed: float = 1.0) -> tuple[Optional[float], str]:
    """A part parameter as a schematic field holds it: (value in SI units, or None; why not / what was assumed).

    Reads InvenTree's (pint) strings ("3.8milliohm", "616 picofarad", "38 nanocoulomb", "6.9 mΩ", "100V") and
    datasheet notation ("616p", "4m7", "2.2 uF", "10uF/16V"). quantity: "ohm", "F", "H", "C", "V" or "s" (a time:
    "20ns", "20 nanoseconds"); a unit of another quantity is refused. A bare number is in `assumed` units (e.g. 1e-3
    for "3.8" meaning mΩ)."""
    if text is None or not str(text).strip():
        return None, "empty"
    s = str(text).strip().replace(",", ".").replace(" ", "")
    timed = False                               # a time written with its unit: "1s" is 1 s, not 1 in `assumed` units
    if quantity == "s":
        s, timed = _SECONDS.subn("", s)         # "20ns" -> "20n": the prefix alone is then the multiplier
    f = _FRACTION.match(s)
    if f and int(f.group(2)):                   # 1/16W -> 0.0625W (read as 1 W before: the "/16W" was dropped)
        s = repr(int(f.group(1)) / int(f.group(2))) + f.group(3)
    m = _INFIX.match(s)
    if m:                                       # 4m7, 1R5: the multiplier is the decimal point
        whole, mult, frac = m.groups()
        value, rest = float(f"{whole}.{frac}") * _MULT[mult], s[m.end():]
        u = _unit_of(rest) if rest else (1.0, "", "")
        if u is None or u[0] != 1.0:
            return None, f"'{text}': unit not understood"
        unit = "ohm" if mult in "RrΩE" else u[1]
    else:
        m = _NUMBER.match(s)
        if not m:
            return None, f"'{text}': not a number"
        u = _unit_of(m.group(2)) if m.group(2) else (1.0, "", "")
        if u is None:
            return None, f"'{text}': unit not understood"
        factor, unit, _ = u
        value = float(m.group(1)) * factor
        if not unit and factor == 1.0 and not m.group(2).strip("/@") and "e" not in m.group(1).lower() \
                and not timed:
            return value * assumed, "unit assumed"          # "1e-9" is written in SI units already
    if unit and unit != quantity:
        return None, f"'{text}': not {_QUANTITY_NAMES[quantity]}"
    return value, ""


def parse_impedance_at(text: str) -> Optional[tuple[float, float]]:
    """Ferrite bead notation: "600R@100MHz", "120Ω @ 100 MHz" -> (ohms, hertz)."""
    if not text or "@" not in text:
        return None
    left, right = text.split("@", 1)
    z = parse_value(left)                       # "600R" -> 600; the frequency without its "Hz": "100M" -> 1e8
    f = parse_value(re.sub(r"(?i)hz$", "", right.strip()))
    if z is None or f is None:
        return None
    return z, f


def format_si(value: Optional[float], unit: str = "") -> str:
    """A value with an SI prefix, as the settings and the tabs write it: 4.7e-6, "F" -> "4.7µF"; None -> "?".
    Six significant digits, so what parse_value reads back is the same number."""
    if value is None:
        return "?"
    if value == 0:
        return f"0{unit}"
    for mult, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"),
                         (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= mult * 0.999:          # 0.999: rounding (999.9999e-9) still gets the larger prefix
            return f"{value / mult:.6g}{prefix}{unit}"
    return f"{value:.3g}{unit}"
