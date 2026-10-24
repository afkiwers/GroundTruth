"""Detection labels: a `GT` field on a schematic symbol says what the part is in a power stage, so detection does not
have to guess it from part names, pin names and connectivity.

    GT = conv_hs          a converter's high-side switch      GT = hb_hs     a motor bridge leg's high-side switch
    GT = conv1_L          converter 1's inductor              GT = hbA_ls    phase A's low-side switch
    GT = conv_in          the input connector                 GT = hb_out    the motor connector
    GT = conv1_in, conv2_in   one connector, two converters   GT = ignore    never a power-stage part

Signal labels (the Nets tab): a net detection would take for a supply (an ADC input measuring a rail, named
ADC_I_RAIL_1), or one whose driver it guesses wrong, is marked on a symbol:

    GT = sig              every net of this 2-pin part (a filter resistor, a test point) is a signal
    GT = sig:9            the net on pin 9 (pad number or pin name, sig:PA3) is a signal
    GT = sig_drv:5        ... and pin 5 drives it        GT = sig_rx:9     ... and pin 9 receives it

An op-amp stage in a signal path (part model `amplifier`, its gain from a Gain field or the Parts tab):

    GT = amp_in:3, amp_out:1     the stage's input and output pins (a dual op-amp: amp_in:5, amp_out:7);
                                 amp_inv:2 its inverting input where the pin names don't say (analysis/analog_net)

A converter's dead times (its dead_rise / dead_fall, components/fields.stage_values), on the controller IC (the one
with the stage's SW pin):

    GT = dead:20ns               every converter this IC controls, both edges
    GT = dead1:20ns, dead2:30ns  the converter on its SW1 / SW2 pin (a dual controller's channels)
    GT = conv1_dead:20ns         the converter labelled conv1 (wins over the two above)
    GT = dead_rise:20ns, dead_fall:10ns    one edge of the switch node: rise = low side off -> high side on
                                 (the LTC7890's DTCA), fall = high side off -> low side on (its DTCB); also
                                 dead1_rise:20ns, conv1_dead_fall:10ns. For each edge the most specific word wins.

A tag is <kind><id>_<role>: the kind (conv, hb), an optional id (letters and digits: conv1, conv5V, hbA) and the
role; the id may also follow the role (conv_out_1, hb_hs_A). Several tags are separated by commas. Upper or lower
case does not matter.

Why a symbol field: fields reach the board file as footprint properties (an MPN does the same) and the KiCad API
reads them too; they change no net name and no layout; the Symbol Fields Table edits many at once. A field in a
sheet that is used several times (one sheet per phase) is the same in every copy, so the board's sheet copy of each
part tells the copies apart: `hb_hs` in the three copies of phase.kicad_sch makes three legs, named after their
sheets ("Phase A", "Phase B", "Phase C").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..core.board import Board, Footprint

FIELD_NAMES = ("GT", "GROUNDTRUTH")          # the field's name, upper case (matched without case)
ROLES = {"conv": ("hs", "ls", "sw", "l", "in", "out"), "hb": ("hs", "ls", "in", "out")}
CONNECTOR_ROLES = ("in", "out")                                          # shared by the stages they name
TAG = re.compile(r"^(conv|hb)([a-z0-9.+]*)_([a-z]+)(?:_([a-z0-9.+]+))?$", re.I)   # conv1_out or conv_out_1
SIG = re.compile(r"^sig(?:_(drv|rx))?(?::(.+))?$", re.I)                         # sig, sig:9, sig_drv:PA3
AMP = re.compile(r"^amp_(in|inv|out):(.+)$", re.I)                                # amp_in:3, amp_inv:2, amp_out:OUTA
# a controller's dead time (the converter's dead_rise / dead_fall): dead:20ns (every stage it controls, both edges),
# dead1:20ns (the stage on its SW1 pin), conv1_dead:20ns (the stage labelled conv1); _rise / _fall: one edge only
DEAD = re.compile(r"^(?:conv([a-z0-9.+]*)_)?dead(\d*)(?:_(rise|fall))?\s*[:=]\s*(.+)$", re.I)
EDGES = ("rise", "fall")      # the switch node's edges: rise = low side off -> high side on, fall = the other
PHASE = re.compile(r"^[A-CU-W1-3]$", re.I)

HELP = {
    "converters": (
        "Detection labels for converters (optional)\n"
        "Detection finds a converter from its inductor and the switches on the inductor's switch node. If it misses "
        "one, or takes the wrong parts, label the parts in the schematic: add a field named GT to the symbol "
        "(Symbol Properties, or many at once in the Symbol Fields Table) with one of these values:\n"
        "  conv_hs    high-side switch (every one of parallel FETs)\n"
        "  conv_ls    low-side switch or catch diode\n"
        "  conv_sw    a converter IC with its switches inside\n"
        "  conv_L     the power inductor\n"
        "  conv_in    the input connector (its + pin reaches the rail, the other is ground)\n"
        "  conv_out   the output connector (the load)\n"
        "  ignore     never take this part as a converter part\n"
        "A connector's through-hole pin values may follow its labels (GT = conv_in, contact:0.5m, fill:75): contact "
        "and joint (Ω per pin), fill (% of the hole soldered), pin (hole, copper, brass, bronze, becu, tellurium), "
        "pin_d (mm), pin_r (Ω); Help (F1) › Through-hole pins.\n"
        "The dead time of a synchronous converter goes on its controller IC (the part with the converter's SW pin): "
        "GT = dead:20ns for every converter it controls, dead1:20ns, dead2:30ns per channel (its SW1 / SW2 pin), or "
        "conv1_dead:20ns for the converter labelled conv1. One edge only: dead_rise:20ns (low side off, high side "
        "on; the LTC7890's DTCA), dead_fall:10ns (high side off, low side on; its DTCB), also dead1_rise:20ns or "
        "conv1_dead_fall:10ns. The rectifier FETs' Vsd and Qrr fields give the reverse conduction and the recovery "
        "charge.\n"
        "Several converters in one sheet: number them, conv1_hs, conv1_L, conv2_hs … (any letters or digits: "
        "conv5V_hs; the number may also follow the role: conv_out_1). A connector shared by several converters: conv_in feeds every converter it reaches, or list "
        "them: conv1_in, conv2_in. A sheet used several times (one per rail) needs the labels once: every copy "
        "becomes its own converter, named after its sheet.\n"
        "Labelled converters are listed with source 'auto' and the reason 'labelled'; then set the operating "
        "point as usual. Boards without labels are detected as before."),
    "bridges": (
        "Detection labels for motor bridges (optional)\n"
        "Detection finds a half-bridge leg as a node with a transistor to the bus and one to ground and no inductor. "
        "If it misses one, or takes the wrong parts, label the parts in the schematic: add a field named GT to the "
        "symbol (Symbol Properties, or many at once in the Symbol Fields Table) with one of these values:\n"
        "  hb_hs      a leg's high-side switch (every one of parallel FETs)\n"
        "  hb_ls      a leg's low-side switch\n"
        "  hb_out     the motor connector (or each phase's wire tab)\n"
        "  hb_in      the bus input connector (both battery tabs when + and - are separate)\n"
        "  ignore     never take this part as a bridge part\n"
        "A connector's through-hole pin values may follow its labels (GT = hb_in, contact:0.5m, fill:75): contact "
        "and joint (Ω per pin), fill (% of the hole soldered), pin (hole, copper, brass, bronze, becu, tellurium), "
        "pin_d (mm), pin_r (Ω); Help (F1) › Through-hole pins.\n"
        "The phase: hbA_hs, hbB_hs, hbC_hs (A-C, U-W or 1-3; or hb_hs_A). In a sheet used once per phase, hb_hs and hb_ls are "
        "enough: every copy of the sheet is its own leg, its phase taken from the motor connector pin, else the node "
        "name, else the sheet name (\"Phase B\"). Legs on one bus form a bridge; two bridges on one bus are told "
        "apart by their motor connectors (hb_out).\n"
        "Labelled bridges are listed with source 'auto' and the reason 'labelled'; then set the operating point and "
        "the motor as usual. Boards without labels are detected as before."),
    "nets": (
        "How nets are found\n"
        "Detect looks at the net names and the schematic pin names: a name with a known word gives the class and its "
        "default frequency and rise time, e.g. SCK / SCLK / SPI_CLK (SPI clock), MOSI / MISO / SDO / SDI (SPI data), "
        "CLK / XTAL / OSC (clocks), SWCLK / TCK (debug), TX / RX (UART), CAN, USB D+ / D-, ETH, MDIO, I2C SDA / SCL "
        "(slow). The driver is the pin of output type; the receivers are the other logic pins. Name a net after its "
        "function (SPI_SCK, not Net-(U1-Pad12)) and give the pins their names in the symbol, and Detect gets it right.\n"
        "Supplies, grounds and power-stage nodes are never listed here: converters and motor bridges have their own "
        "tabs (their parts can be labelled with a GT field, see those tabs). Any other net: select it in KiCad and "
        "press Add (the plugin), or Add with the net's name.\n"
        "A signal that looks like a supply (ADC_I_RAIL_1: a name with RAIL, VIN …), or one whose driver Detect "
        "guesses wrong, can be labelled in the schematic: a field named GT on a symbol with\n"
        "  sig          every net of this 2-pin part (a filter resistor) is a signal\n"
        "  sig:9        the net on pin 9 (pad number or pin name, sig:PA3) is a signal\n"
        "  sig_drv:5    ... and pin 5 drives it\n"
        "  sig_rx:9     ... and pin 9 receives it\n"
        "  amp_in:3, amp_out:1   an op-amp stage's input and output pins: the path runs through it (gain from a "
        "Gain field or the Parts tab)\n"
        "  amp_inv:2    its inverting input (where the pin names don't say): the circuit around it is solved\n"
        "Label the driving pin, each op-amp and the receiving pin; leave the resistors and capacitors between them "
        "unlabelled. Several labels are separated by commas. Then Update PCB from Schematic, Reload board and "
        "Detect…. A name with ADC, SENSE, MON or FB counts as a signal anyway. Help (F1) › Labelling a signal path "
        "has an example."),
}


@dataclass(frozen=True)
class Tag:
    """One GT label on one part: what kind of stage (or a signal / amplifier label), its id, the part's role in
    it, and the part's reference."""
    kind: str           # conv | hb
    ident: str          # "" or the id after the kind (1, 5v, a)
    role: str           # hs | ls | sw | l | in | out
    ref: str


@dataclass
class StageTags:
    """The parts labelled for one converter or one bridge leg."""
    kind: str
    ident: str
    name: str                                    # for messages and the stage's name: the id, or the sheet's name
    sheet_name: str
    roles: dict[str, list[str]] = field(default_factory=dict)      # role -> references

    def refs(self, *roles: str) -> list[str]:
        """The references labelled with any of these roles (hs, ls, sw, l)."""
        return [r for role in roles for r in self.roles.get(role, [])]

    def describe(self) -> str:
        """"Q1 hs, Q2 ls, L1 L": what is labelled."""
        order = ("hs", "ls", "sw", "l")
        return ", ".join(f"{r} {role if role != 'l' else 'L'}"
                         for role in sorted(self.roles, key=lambda x: order.index(x) if x in order else 9)
                         for r in sorted(self.roles[role], key=lambda s: (len(s), s)))

    @property
    def phase(self) -> Optional[str]:
        """A bridge leg's phase from its id (hbA) or its sheet's name ("Phase B", "PH_C", "U")."""
        if self.kind != "hb":
            return None
        return self.ident.upper() if PHASE.match(self.ident) else phase_from_name(self.sheet_name)


def phase_from_name(name: str) -> Optional[str]:
    """The phase a sheet's name ends in: "Phase B", "PH_C", "Leg 2", "U" (None without one)."""
    m = re.search(r"(?:^|[\s_\-])(?:PH(?:ASE)?|LEG|HB)?[\s_\-]?([A-CU-W1-3])$", name, re.I)
    return m.group(1).upper() if m else None


@dataclass
class BoardTags:
    """Every GT label on a board, read once: per part its labels, the parts marked ignore, the stages (converters
    and bridge legs) the labels make up, the labelled connectors and signal pins, and problems to report."""
    tagged: dict[str, list[Tag]] = field(default_factory=dict)     # reference -> its tags
    ignored: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    stages: list[StageTags] = field(default_factory=list)
    connectors: list[Tag] = field(default_factory=list)            # in / out tags
    signals: list[Tag] = field(default_factory=list)               # sig labels: kind "sig", ident = the pin, role
                                                                   # "" | drv | rx (signal_marks)
    amps: list[Tag] = field(default_factory=list)                  # amp_in / amp_out labels (op-amp stages)

    def __bool__(self) -> bool:
        """True when the board has any GT label (or a GT field that could not be read)."""
        return bool(self.tagged or self.ignored or self.problems or self.signals or self.amps)

    def of(self, kind: str) -> list[StageTags]:
        """The stages of one kind: "conv" (converters) or "hb" (bridge legs)."""
        return [s for s in self.stages if s.kind == kind]

    def used(self, kind: Optional[str] = None) -> set[str]:
        """References labelled as switches or inductors (of one kind, or any)."""
        return {r for s in self.stages if kind is None or s.kind == kind for r in s.refs(*s.roles)}

    def connector_refs(self, kind: str, role: str, ident: Optional[str]) -> list[str]:
        """Connectors labelled <kind>_<role> for the stage with this id: those with the id, else those without one
        (a shared input)."""
        tags = [t for t in self.connectors if t.kind == kind and t.role == role]
        own = [t.ref for t in tags if ident and t.ident == ident]
        return own or [t.ref for t in tags if not t.ident]

    def summary(self) -> str:
        """One line for the status / the info box: what is labelled on this board."""
        if not self:
            return "This board has no GT labels: everything is detected from the parts and their connections."
        parts = []
        for kind, what in (("conv", "converters"), ("hb", "bridge legs")):
            if self.of(kind):
                parts.append(f"{what} {', '.join(s.name for s in self.of(kind))}")
        if self.connectors:
            parts.append(f"{len({t.ref for t in self.connectors})} connector(s)")
        if self.signals:
            parts.append(f"signal labels on {', '.join(sorted({t.ref for t in self.signals}))}")
        if self.amps:
            parts.append(f"op-amp labels on {', '.join(sorted({t.ref for t in self.amps}))}")
        if self.ignored:
            parts.append(f"ignored: {', '.join(sorted(self.ignored))}")
        line = "GT labels on this board: " + ("; ".join(parts) if parts else "none usable")
        problems = list(dict.fromkeys(self.problems))
        if problems:
            line += f". {len(problems)} problem(s): " + " · ".join(problems)
        return line


def tag_text(fp: Footprint) -> str:
    """The value of a footprint's GT field ("" without one)."""
    for name, value in fp.fields.items():
        if name.strip().upper() in FIELD_NAMES:
            return value or ""
    return ""


def parse(text: str, ref: str = "") -> tuple[list[Tag], bool, list[str]]:
    """(tags, ignore, problems) of a GT field's value."""
    tags, ignore, problems = [], False, []
    for raw in re.split(r"[,;]", text or ""):
        word = raw.strip().replace(" ", "")
        if not word:
            continue
        if word.lower() in ("ignore", "gt_ignore", "none"):
            ignore = True
            continue
        from .pins import parse_words, is_pin_word
        if is_pin_word(word):                   # a through-hole pin value (contact:0.5m, fill:75): analysis/pins.py
            problems += [f"{ref}: GT {p}" for p in parse_words(word)[1]]
            continue
        if DEAD.match(word):                    # a controller's dead time: read per stage (dead_times)
            problems += [f"{ref}: GT {why}" for *_, why in dead_times(word) if why]
            continue
        am = AMP.match(word)
        if am:
            tags.append(Tag("amp", am.group(2), am.group(1).lower(), ref))
            continue
        sm = SIG.match(word)
        if sm:
            role, pin = (sm.group(1) or "").lower(), sm.group(2) or ""
            if role and not pin:
                problems.append(f"{ref}: GT '{word}': name the pin, e.g. sig_{role}:5")
                continue
            tags.append(Tag("sig", pin, role, ref))
            continue
        m = TAG.match(word)
        if not m:
            problems.append(f"{ref}: GT '{word}' is not a label (e.g. conv_hs, conv1_L, hbA_ls, hb_out, sig:9, "
                            "ignore)")
            continue
        kind, role = m.group(1).lower(), m.group(3).lower()
        if m.group(2) and m.group(4):
            problems.append(f"{ref}: GT '{word}': the id goes before or after the role, not both")
            continue
        ident = (m.group(2) or m.group(4) or "").lower()
        if role not in ROLES[kind]:
            problems.append(f"{ref}: GT '{word}': {kind} has the roles "
                            f"{', '.join(r if r != 'l' else 'L' for r in ROLES[kind])}")
            continue
        tags.append(Tag(kind, ident, role, ref))
    return tags, ignore, problems


def dead_times(text: str) -> list[tuple[str, str, str, Optional[float], str, str]]:
    """The dead-time words of a controller's GT field: [(converter id or "", channel or "", edge "rise" / "fall" or
    "" for both, seconds or None, the word as written, why it is unusable or "")]. "dead:20ns" applies to every
    stage the IC controls, "dead2:20ns" to the one on its SW2 pin, "conv1_dead:20ns" to the stage labelled conv1;
    "_rise" / "_fall" after "dead<n>" to that switch-node edge only; a bare number is in ns."""
    from ..components.values import parse_quantity
    out = []
    for raw in re.split(r"[,;]", text or ""):
        word = raw.strip()
        m = DEAD.match(word.replace(" ", ""))
        if not m:
            continue
        value, why = parse_quantity(m.group(4), "s", 1e-9)
        if value is not None and not 0.0 <= value <= 2e-6:
            value, why = None, f"'{m.group(4)}' is not a dead time (0 to 2 µs)"
        out.append(((m.group(1) or "").lower(), m.group(2) or "", (m.group(3) or "").lower(), value, word,
                    f"'{word}': {why}" if value is None else ""))
    return out


def dead_time_of(fp: Footprint, ident: str, channel: str, edge: str) -> Optional[tuple[float, str]]:
    """(seconds, the word) the controller `fp`'s GT field gives one switch-node `edge` ("rise" / "fall") of the
    stage with GT id `ident` on its channel `channel` (its SW pin's number); None without a word that applies. The
    most specific word wins: the stage's own (conv<id>_dead), then its channel's (dead<n>), then the IC's (dead);
    at each level a word for this edge before one for both."""
    words = [w for w in dead_times(tag_text(fp)) if w[3] is not None]
    levels = ((lambda w: bool(ident) and w[0] == ident), (lambda w: not w[0] and bool(channel) and w[1] == channel),
              (lambda w: not w[0] and not w[1]))
    for level in levels:
        for which in (edge, ""):
            hit = next((w for w in words if level(w) and w[2] == which), None)
            if hit is not None:
                return hit[3], hit[4]
    return None


def stage_ident(board: Board, refs: list[str]) -> str:
    """The GT id of the labelled converter whose parts include any of `refs` ("" when none is labelled with
    one)."""
    want = set(refs)
    return next((st.ident for st in board_tags(board).of("conv") if want & set(st.refs(*st.roles))), "")


def amp_label_roles(fp: Footprint) -> dict[str, str]:
    """A part's GT amp labels by role: {"in": pin, "inv": pin, "out": pin} (those it has)."""
    tags, _, _ = parse(tag_text(fp), fp.reference)
    return {t.role: t.ident for t in tags if t.kind == "amp"}


def amp_label_pins(fp: Footprint) -> Optional[tuple[str, str]]:
    """(input pin, output pin) a part's GT amp_in / amp_out labels name, or None without both."""
    tags, _, _ = parse(tag_text(fp), fp.reference)
    pins = {t.role: t.ident for t in tags if t.kind == "amp"}
    return (pins["in"], pins["out"]) if "in" in pins and "out" in pins else None


def signal_marks(board: Board, reference_nets=()) -> tuple[dict[str, dict], list[str]]:
    """The nets the sig labels mark as signals: {net: {"drivers": [pins], "receivers": [pins]}}, and problems (a
    pin the part doesn't have, `sig` on a part with more than two nets). Reference nets are never signals."""
    refs = set(reference_nets)
    tags = board_tags(board)
    key = (id(tags), tuple(sorted(refs)))
    cached = board.__dict__.get("_gt_signals")
    if cached is not None and cached[0] == key:
        return cached[1]
    fps = {fp.reference: fp for fp in board.footprints}
    out: dict[str, dict] = {}
    problems: list[str] = []
    for t in tags.signals:
        fp = fps.get(t.ref)
        if fp is None:
            continue
        if t.ident:
            pad = _label_pad(fp, t.ident)
            if pad is None:
                problems.append(_no_pin(t))
                continue
            pads = [pad]
        else:
            pads = [p for p in fp.pads if p.net and p.net not in refs]
            if len({p.net for p in pads}) > 2:
                problems.append(f"{t.ref}: GT 'sig' marks the nets of a 2-pin part; name the pin on a larger one "
                                "(sig:9)")
                continue
        for pad in pads:
            if not pad.net or pad.net in refs:
                continue
            e = out.setdefault(pad.net, {"drivers": [], "receivers": []})
            if t.role == "drv":
                e["drivers"].append(pad.pin)
            elif t.role == "rx":
                e["receivers"].append(pad.pin)
    board.__dict__["_gt_signals"] = (key, (out, problems))
    return out, problems


def _label_pad(fp: Footprint, ident: str):
    """The pad a label's pin names: by pad number first, else by schematic pin name (matched without case)."""
    want = ident.lower()
    return next((p for p in fp.pads if p.number.lower() == want), None) or \
        next((p for p in fp.pads if (p.pin_function or "").lower() == want), None)


def _no_pin(t: Tag) -> str:
    """The problem text for a signal or op-amp label naming a pin its part doesn't have."""
    label = f"sig{'_' + t.role if t.role else ''}:{t.ident}" if t.kind == "sig" else f"amp_{t.role}:{t.ident}"
    return f"{t.ref}: GT '{label}': no pin {t.ident!r} on {t.ref} (pad number or pin name)"


def _pin_problems(fp: Footprint, tags: list[Tag]) -> list[str]:
    """Signal and op-amp labels that can't work on this part, found when the labels are read so the tabs' label
    line shows them (otherwise the label is just skipped and its path silently missing): a pin the part doesn't
    have, or an op-amp with amp_in but no amp_out (or the other way round)."""
    out = [_no_pin(t) for t in tags if t.kind in ("sig", "amp") and t.ident and _label_pad(fp, t.ident) is None]
    roles = {t.role for t in tags if t.kind == "amp"} - {"inv"}
    if len(roles) == 1:
        have = roles.pop()
        out.append(f"{fp.reference}: GT amp_{have} needs amp_{'out' if have == 'in' else 'in'} too: "
                   "the op-amp stage's input and output pin")
    return out


def board_tags(board: Board) -> BoardTags:
    """Every GT label on the board, grouped into stages (a converter, a bridge leg): parts with the same kind and id
    belong together; without an id, or with an id that recurs in copies of one reused sheet, each sheet copy is a
    stage of its own. Read once per board: detection adds the problems it meets (a labelled converter without an
    inductor) to the same object, which the tabs and `groundtruth detect` show."""
    key = (id(board.footprints), len(board.footprints))        # a what-if copy with its own parts reads its own
    cached = board.__dict__.get("_gt_tags")
    if cached is None or cached[0] != key:
        cached = board.__dict__["_gt_tags"] = (key, _read_tags(board))
    return cached[1]


def _read_tags(board: Board) -> BoardTags:
    """Read every footprint's GT field into BoardTags: parse the labels, set aside ignore and DNP parts, group the
    switch and inductor labels into stages (one per id; per sheet copy when the id comes from a reused sheet, or
    there is none), and note stages that miss a role (a converter needs hs and ls, or sw; a leg hs and ls)."""
    out = BoardTags()
    switch_tags: list[tuple[Tag, Footprint]] = []
    for fp in board.footprints:
        text = tag_text(fp)
        if not text:
            continue
        tags, ignore, problems = parse(text, fp.reference)
        out.problems += problems
        if fp.dnp and (tags or ignore):
            continue
        if ignore:
            out.ignored.add(fp.reference)
            if tags:
                out.problems.append(f"{fp.reference}: GT has 'ignore' and labels: ignored")
            continue
        out.signals += [t for t in tags if t.kind == "sig"]
        out.amps += [t for t in tags if t.kind == "amp"]
        out.problems += _pin_problems(fp, tags)
        tags = [t for t in tags if t.kind not in ("sig", "amp")]       # amp: read per part (amp_label_pins)
        if tags:
            out.tagged[fp.reference] = tags
        for t in tags:
            if t.role in CONNECTOR_ROLES:
                out.connectors.append(t)
            else:
                switch_tags.append((t, fp))
    # an id seen in several copies of the same sheet file is one stage per copy
    copies: dict[tuple, set] = {}
    for t, fp in switch_tags:
        copies.setdefault((t.kind, t.ident, fp.sheet_file), set()).add(fp.sheet)
    groups: dict[tuple, StageTags] = {}
    for t, fp in switch_tags:
        per_copy = not t.ident or len(copies[(t.kind, t.ident, fp.sheet_file)]) > 1
        key = (t.kind, t.ident, fp.sheet if per_copy else "")
        st = groups.get(key)
        if st is None:
            if t.ident and not per_copy:
                name = f"{t.kind}{t.ident}"
            else:
                name = " ".join(x for x in (fp.sheet_name or ("root sheet" if not fp.sheet else fp.sheet[-8:]),
                                            f"{t.kind}{t.ident}" if t.ident else "") if x)
            st = groups[key] = StageTags(t.kind, t.ident, name, fp.sheet_name)
        st.roles.setdefault(t.role, []).append(fp.reference)
    out.stages = sorted(groups.values(), key=lambda s: (s.kind, s.name))
    for st in out.stages:
        p = f"{st.kind}{st.ident}"
        if st.kind == "hb":
            missing = [f"{p}_{r}" for r in ("hs", "ls") if r not in st.roles]
        elif "sw" in st.roles:
            missing = []
        else:
            missing = [f"{p}_{r}" for r in ("hs", "ls") if r not in st.roles]
            if missing:
                missing[-1] += f" (or {p}_sw for a converter IC with its switches inside)"
        if missing:
            what = "leg" if st.kind == "hb" else "converter"
            out.problems.append(f"{what} {st.name}: label {' and '.join(missing)} too ({st.describe()} labelled)")
    return out


_STAGE_IN_NAME = re.compile(r"conv([a-z0-9.+]*)$", re.I)            # "buck conv3" -> "3" (detection's names)


def follow_labels(board: Board, converters: list) -> tuple[list, list[str]]:
    """Converters whose switches no longer sit where their roles need them (the board was re-annotated: U502
    became U501) take the parts their GT labels now name (the user's flux-2 after 2026-10-23: "I can't simulate the
    converters anymore"). A converter is matched to the labelled stage by the id in its name ("buck conv3" ->
    conv3_*); one whose parts still fit, or without a labelled stage, is left as it is; the new parts must fit too.
    Returns (the converters, one note per converter changed: "buck conv3: U502 → U501")."""
    from dataclasses import replace
    stages = {st.ident.lower(): st for st in board_tags(board).of("conv")}
    nets = set(board.nets)
    out, notes = [], []
    for c in converters:
        m = _STAGE_IN_NAME.search(c.name.strip())
        st = stages.get(m.group(1).lower()) if m else None
        if st is None or _roles_fit(board, c):
            out.append(c)
            continue
        # a high side / a converter IC's switch: either label (a controller IC with its switch inside is "sw")
        new = replace(c, high_side=(st.refs("hs") or st.refs("sw")) if c.high_side else [],
                      low_side=st.refs("ls") if c.low_side else [],
                      switch=(st.refs("sw") or st.refs("hs")) if c.switch else [])
        if new.switch_node and new.switch_node not in nets:                 # an auto-named net renamed with its part
            new = replace(new, switch_node=_switch_net(board, st) or new.switch_node)
        if not _roles_fit(board, new):
            out.append(c)
            continue
        moved = {o: n for role in ("high_side", "low_side", "switch")
                 for o, n in zip(getattr(c, role), getattr(new, role)) if o != n}
        old_l = re.search(r"\b(\S+) L;", c.note or "")                      # detection's note names the inductor
        if old_l and st.refs("l") and old_l.group(1) != st.refs("l")[0]:
            moved[old_l.group(1)] = st.refs("l")[0]
        note = c.note or ""
        if moved:                                       # every old reference at once, so U502 -> U501 and
            note = re.sub(r"\b(" + "|".join(map(re.escape, moved)) + r")\b",   # U503 -> U502 don't chain
                          lambda k: moved[k.group(1)], note)
        if new.switch_node != c.switch_node:
            note = note.replace(c.switch_node or "", new.switch_node)
        out.append(replace(new, note=note))
        notes.append(f"{c.name}: " + ", ".join(f"{o} → {n}" for o, n in moved.items())
                     + (f", switch node {new.switch_node}" if new.switch_node != c.switch_node else "")
                     + " (its GT labels)")
    return out, notes


def _roles_fit(board: Board, c) -> bool:
    """Every switch of the converter is on the board with pads on the nets its role joins (as settings.validate
    checks: high side rail-switch node, low side switch node-ground, an IC's switch rail-ground)."""
    ends = {"high_side": (c.rail, c.switch_node), "low_side": (c.switch_node, c.ground), "switch": (c.rail, c.ground)}
    for role, want in ends.items():
        for ref in getattr(c, role):
            fp = board.footprint(ref)
            if fp is None or not {n for n in want if n} <= fp.nets():
                return False
    return True


def _switch_net(board: Board, st: StageTags) -> Optional[str]:
    """The stage's switch node: the net its labelled inductor shares with its labelled switches."""
    sw = set().union(*[board.footprint(r).nets() for r in st.refs("hs", "ls", "sw") if board.footprint(r)])
    for r in st.refs("l"):
        fp = board.footprint(r)
        hit = sorted(fp.nets() & sw) if fp else []
        if len(hit) == 1:
            return hit[0]
    return None
