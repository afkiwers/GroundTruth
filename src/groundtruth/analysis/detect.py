"""Suggest nets and power stages to analyse, with default timing, for the user to review.

Nets are classified by their name and the schematic pin names on them (SPI clock, RMII, USB, ...).
The driver is chosen from the schematic pin types: an output beats a bidirectional pin, which beats a
passive (connector) pin, which beats an input.

Power stages are found from their inductors: a net between an inductor and a switching transistor, a catch diode
or an IC's SW/LX pin is a switch node. The transistor whose other power pin is on ground is the low side, the
other one the high side (its other net is the hot loop's rail). Switching frequency, load current and voltages
cannot be read from the board: they get defaults to review.

Every suggestion carries the reasons it was made, and lands in the settings marked `source: auto,
reviewed: false` until the user confirms it.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from ..components.defaults import ref_prefix, suggest_model
from ..components.models import ComponentModel, amplifier_pins
from ..config.settings import BridgeSpec, Config, ConverterSpec, LegSpec, NetSpec, _natural_key
from ..core.board import Board, Footprint, Pad

DRIVER_RANK = {"output": 4, "tri_state": 4, "open_collector": 3, "open_emitter": 3, "bidirectional": 2,
               "unspecified": 1, "free": 1, "": 1, "passive": 0.5, "input": 0}
NOT_ENDPOINTS = ("TP", "MH", "H", "FID", "LOGO", "REF")
POWER_NAME = re.compile(r"^[+-]|^\d+V\d*|VCC|VDD|VEE|VSS|VBUS|VIN|VOUT|VBAT|VSYS|PWR|AVDD|DVDD|^V\d|RAIL|SUPPLY",
                        re.I)
# a name with one of these measures a supply rather than being one (an ADC input, a current-sense output)
MEASURE_NAME = re.compile(r"ADC|SENSE|SNS|MEAS|MON|ISNS|IMON|VMON|(?:^|[_\W])FB(?:$|[_\W\d])|FEEDBACK", re.I)


@dataclass(frozen=True)
class SignalClass:
    """A kind of signal recognised by name: its name pattern (net or pin names), the typical frequency and rise
    time a suggestion gets, how fast it is (score), and a hint for the user."""
    name: str
    pattern: str
    freq: float
    rise_time: float
    score: int          # 3 = clearly fast, 2 = likely fast, 1 = moderate, 0 = slow
    note: str = ""


CLASSES = [
    SignalClass("USB data", r"USB.*(D[PMN+-]|DP|DM)\b|^D[PM+-]$|^USB_?D", 12e6, 4e-9, 3,
                "USB full speed; use 240 MHz / 0.5 ns for high speed"),
    SignalClass("Ethernet (R)MII", r"RMII|(^|_)MII|REF_?CLK|ETH|(^|_)(TXD|RXD)\d|TX_?EN|CRS_?DV|MDC", 50e6, 1e-9, 3),
    SignalClass("SDIO / QSPI", r"QSPI|SDIO|SD_?(CLK|CMD|D\d)|OSPI", 50e6, 1e-9, 3),
    SignalClass("SPI clock", r"(SCK|SCLK)$|SPI_?CLK|SPI\w*_SCK", 20e6, 2e-9, 3),
    SignalClass("SPI data", r"MOSI|MISO|COPI|CIPO|(^|_)SD[IO]$", 10e6, 2e-9, 2),
    SignalClass("Debug clock", r"SWCLK|SW_?CLK|(^|_)TCK$", 10e6, 3e-9, 2),
    SignalClass("Crystal / clock input", r"XTAL|XIN|XOUT|OSC|CLK_?IN|MCO|CLKOUT|HSE|LSE", 25e6, 3e-9, 2),
    SignalClass("Clock", r"CLK|CLOCK", 10e6, 2e-9, 2),
    SignalClass("Switching node", r"(^|_)SW\d*$|GATE|(^|_)(HG|LG|HO|LO)\d*$|PHASE|(^|_)LX\d*$", 500e3, 10e-9, 2,
                "power stage; check the converter's switching frequency and edge rate"),
    SignalClass("PWM", r"PWM", 100e3, 10e-9, 1),
    SignalClass("CAN (logic side)", r"CAN_?(TX|RX)", 1e6, 20e-9, 1),
    SignalClass("Serial TX/RX", r"(^|_)(TX|RX)D?$|UART|USART", 1e6, 10e-9, 1),
    SignalClass("I2C", r"SCL|SDA|I2C", 400e3, 50e-9, 0),
]


@dataclass
class DetectedNet:
    """A signal net detection suggests: its NetSpec (unreviewed), the class it was taken for, how sure detection
    is, and why (shown with the suggestion)."""
    spec: NetSpec
    signal_class: SignalClass
    confidence: str                      # high | medium | low
    reasons: list[str] = field(default_factory=list)
    covers: frozenset = frozenset()      # every net of its path (series parts, labelled op-amps): Detect replaces
                                         # unreviewed entries for its pieces (refreshes)

    @property
    def score(self) -> int:
        """How fast the class is (3 = clearly fast ... 0 = slow): `detect --min-score` filters on it."""
        return self.signal_class.score


def _leaf(net: str) -> str:
    """A hierarchical net's last part, upper case ("/CPU/spi_sck" -> "SPI_SCK")."""
    return net.split("/")[-1].upper()


def classify(net: str, pin_functions: list[str]) -> Optional[tuple[SignalClass, str]]:
    """The signal class a net is taken for: the first class (in CLASSES order: the fast ones first) whose name
    pattern matches the net's name, or else one of its pins' names; with what matched. None for no match."""
    names = [_leaf(net)] + [f.upper() for f in pin_functions if f]
    for cls in CLASSES:
        rx = re.compile(cls.pattern, re.I)
        for i, name in enumerate(names):
            if rx.search(name):
                return cls, ("net name" if i == 0 else f"pin name {name!r}")
    return None


def is_power_net(board: Board, net: str, config: Config) -> bool:
    """A supply net: a reference net, a net with a power pin, or a supply name (POWER_NAME) unless the name says
    it measures something (MEASURE_NAME: /CPU/ADC_I_RAIL_1, the ADC input measuring rail 1's current, contains
    RAIL but is a sensitive signal: the user's board, 2026-10-11)."""
    if net in config.reference_nets:
        return True
    if net in _marked(board, config):                  # a GT sig label: the user says it is a signal
        return False
    if any(p.net == net and p.pin_type in ("power_in", "power_out") for p in board.pads()):
        return True
    leaf = _leaf(net)
    return bool(POWER_NAME.search(leaf)) and not MEASURE_NAME.search(leaf)


def _marked(board: Board, config: Config) -> dict:
    """The nets GT sig labels mark as signals (analysis/tags.signal_marks): {net: {"drivers", "receivers"}}."""
    from .tags import signal_marks
    return signal_marks(board, config.reference_nets)[0]


def _model(config: Config, fp):
    """A part's model: the settings', else the suggested one."""
    return config.model_for(fp.reference) or suggest_model(fp)


def _group(board: Board, config: Config, net: str, by_net: dict) -> set[str]:
    """The net plus nets reached through series parts (0R, termination R, beads), never power nets."""
    group, frontier = {net}, [net]
    while frontier:
        n = frontier.pop()
        for fp in by_net.get(n, []):
            if not _model(config, fp).passes_signal() or len(fp.nets()) != 2:
                continue
            for other in fp.nets():
                if other and other not in group and not is_power_net(board, other, config):
                    group.add(other)
                    frontier.append(other)
    return group


GENERIC = SignalClass("Signal", "", 10e6, 2e-9, 0, "no class from the net or pin names: set the frequency "
                      "and rise time")


def _index(board: Board) -> tuple[dict[str, list], dict[str, Footprint]]:
    """(net -> parts with a pad on it, reference -> footprint)."""
    return board.parts_by_net(), {fp.reference: fp for fp in board.footprints}


def _suggest(board: Board, config: Config, net: str, by_net: dict, fp_of: dict, picked: bool,
             min_score: int = 0) -> tuple[Optional[DetectedNet], set[str]]:
    """(suggestion, the net's group). A net found by scanning must look like a signal worth analysing (a known
    class, a driver and a receiver on different parts, not a poured supply); a net the user picked gets a
    suggestion whenever it has pads, with the doubts in its reasons."""
    marks = _marked(board, config)
    mark = marks.get(net)
    picked = picked or mark is not None                 # a labelled net is always suggested
    name, through, joined = net, [], {net}
    if mark is not None:                    # labelled nets joined by series parts and op-amps are one path
        joined, through = _labelled_path(board, config, net, marks, by_net)
        mark = {"drivers": [p for n in sorted(joined) for p in marks.get(n, {}).get("drivers", [])],
                "receivers": [p for n in sorted(joined) for p in marks.get(n, {}).get("receivers", [])]}
        named = sorted(n for n in joined if not n.startswith("Net-("))
        name = net if not net.startswith("Net-(") or not named else named[0]
    group = _group(board, config, net, by_net) | joined     # a labelled path's ends are on all of its nets
    pads = [p for p in board.pads() if p.net in group]
    found = classify(net, [p.pin_function for p in pads])
    if (found is None or found[0].score < min_score) and not picked:
        return None, group
    cls, why = found or (GENERIC, "")
    switching = cls.name == "Switching node"
    doubts = []

    def endpoint(p: Pad) -> bool:
        """Whether a pad can be a driver or receiver: a pin of a part that is no passive in the path (an IC, a
        connector: model "open"); on a switching node an inductor's pin too."""
        fp = fp_of.get(p.footprint)
        if fp is None or ref_prefix(p.footprint) in NOT_ENDPOINTS or fp.dnp:
            return False
        kind = _model(config, fp).kind
        return kind == "open" or (switching and kind in ("inductor", "ferrite"))

    ends = [p for p in pads if endpoint(p) and (not switching or p.net == net)]
    if len({p.footprint for p in ends}) < 2:   # needs a driver and a receiver on different parts
        if not picked:
            return None, group
        ends = [p for p in pads if p.footprint in fp_of and ref_prefix(p.footprint) not in NOT_ENDPOINTS]
        if not ends:
            return None, group
        doubts.append("fewer than two parts with logic pins on this net: check the driver and receivers")
    if why and why != "net name" and not switching and any(z.net in group for z in board.zones):
        if not picked:
            return None, group   # a poured net matched only by a pin name is a supply, not a signal
        doubts.append("a poured net, named like a signal only by a pin: is it a supply?")
    ranked = sorted(ends, key=lambda p: (DRIVER_RANK.get(p.pin_type, 1), len(fp_of[p.footprint].pads)),
                    reverse=True)
    driver = ranked[0]
    top = DRIVER_RANK.get(driver.pin_type, 1)
    ties = [p for p in ranked if DRIVER_RANK.get(p.pin_type, 1) == top]
    reasons = [f"class '{cls.name}' from {why}"] if why else []
    if len(ties) == 1 and top > 0.5:
        confidence = "high" if top >= 2 else "medium"
        reasons.append(f"driver {driver.pin}: only {driver.pin_type or 'untyped'} pin, others "
                       f"{', '.join(sorted({p.pin_type or 'untyped' for p in ends if p is not driver}))}")
    elif len(ties) == 1:
        confidence = "medium"
        reasons.append(f"driver {driver.pin}: connector/passive pin feeding inputs")
    else:
        confidence = "low"
        reasons.append(f"driver {driver.pin}: {len(ties)} pins are {driver.pin_type or 'untyped'}; "
                       f"picked the part with most pins ({driver.footprint})")
    if cls.note:
        reasons.append(cls.note)
    if doubts:
        confidence = "low"
        reasons += doubts
    receivers = list(dict.fromkeys(p.pin for p in ends if p.footprint != driver.footprint))
    driver_pin = driver.pin
    if mark is not None:                                # GT sig labels: the user's driver and receivers win
        reasons.insert(0, "labelled a signal (GT sig)")
        if mark["drivers"]:
            driver_pin = mark["drivers"][0]
            reasons = [r for r in reasons if not r.startswith("driver ")]      # the guess it replaces
            reasons.append(f"driver {driver_pin} from its GT sig_drv label")
            receivers = [r for r in receivers if r.split(".")[0] != driver_pin.split(".")[0]]
            if confidence == "low" and not doubts:
                confidence = "medium"
        if mark["receivers"]:
            receivers = list(dict.fromkeys(mark["receivers"]))
            reasons.append(f"receivers {', '.join(receivers)} from GT sig_rx labels")
            rx_parts = {r.split(".")[0] for r in receivers}
            if not mark["drivers"] and driver_pin.split(".")[0] in rx_parts:
                # the guess landed on the labelled receiver's own part: another end drives, or nothing found does
                other = next((p for p in ranked if p.footprint not in rx_parts), None)
                reasons = [r for r in reasons if not r.startswith("driver ")]
                if other is not None:
                    driver_pin = other.pin
                    reasons.append(f"driver {driver_pin}: the end that is not the labelled receiver")
                else:
                    confidence = "low"
                    reasons.append("no driver found apart from the labelled receiver: label the driving pin "
                                   "(GT sig_drv:<pin>) and the op-amps on the way (amp_in / amp_out)")
        if through:
            reasons.append(f"through {', '.join(through)} (between labelled nets)")
    freq, rise = cls.freq, cls.rise_time
    if cls is GENERIC:                  # no class from the names: an op-amp on the path may say how fast it can be
        drv = next((p for p in pads if p.pin == driver_pin), None)
        amp = _amplifier_signal(config, drv.net if drv else "", pads, fp_of, freq, rise)
        if amp is not None:
            freq, rise, why_amp = amp
            reasons.append(why_amp)
    spec = NetSpec(name, driver_pin, receivers, freq, rise, source="auto", reviewed=False,
                   signal_class=cls.name, through=through)
    return DetectedNet(spec, cls, confidence, reasons), group | joined


SLEW_SWING = 0.8       # an op-amp's edge: 10 to 90 % of the swing at its slew rate
AMP_SWING_V = 3.3      # the swing assumed for that edge (NetSpec.amplitude_v's default)


def _amplifier_signal(config: Config, driver_net: str, pads: list, fp_of: dict, freq: float, rise: float
                      ) -> Optional[tuple[float, float, str]]:
    """(frequency, edge, reason) for a net with no class from its names when the op-amps that drive it (parts
    modelled as amplifiers, components.models.amplifier_pins, whose output is on the driver's net: the driver
    itself, or a series part away) give a slew rate or a bandwidth: the fastest full swing the slowest of them can
    make [A37]. An op-amp further along (a labelled path's later stage) does not limit the driver's signal: the
    path model applies its gain pole and slew cap there (emi/path.amplified_path). Its edge is the longer of the slew
    limit, SLEW_SWING·V / SR, and the bandwidth's, 0.35 / BW (a single pole's 10-90 % rise, ln 9 / 2π
    [Johnson 1993]; BW = the model's `bandwidth`, else GBW / |gain|, as ComponentModel.amp_gain); its frequency
    the lowest of `freq`, BW and the slew's full-power frequency SR / (π·V) (a sine of V peak to peak has the slope
    π·f·V). None when no op-amp there has either value (the generic `freq` / `rise` stay)."""
    found = []                          # (frequency, edge, ref, slew, bandwidth) per op-amp with a value
    for ref in sorted({p.footprint for p in pads}):
        fp = fp_of.get(ref)
        m = _model(config, fp) if fp is not None else None
        pins = amplifier_pins(fp, m) if m is not None else None
        if pins is None or not driver_net or pins[1].net != driver_net:
            continue
        slew = m.params.get("slew")
        bw = m.params.get("bandwidth")
        if not bw and m.params.get("gbw"):
            bw = m.params["gbw"] / max(abs(m.params.get("gain") or 1.0), 1.0)
        if not slew and not bw:
            continue
        t = max(rise, 0.35 / bw if bw else 0.0, SLEW_SWING * AMP_SWING_V / slew if slew else 0.0)
        f = min(freq, bw or freq, slew / (math.pi * AMP_SWING_V) if slew else freq)
        found.append((f, t, ref, slew, bw))
    if not found:
        return None
    _, t, ref, slew, bw = max(found, key=lambda x: x[1])          # the slowest edge on the path ...
    f = min(x[0] for x in found)                                   # ... and the lowest frequency
    from ..components.values import format_si
    got = ", ".join(x for x in (f"slew rate {slew / 1e6:.3g} V/µs" if slew else "",
                                f"bandwidth {format_si(float(f'{bw:.3g}'), 'Hz')}" if bw else "") if x)
    t3, f3 = float(f"{t:.3g}"), float(f"{f:.3g}")
    return f3, t3, (f"{format_si(t3, 's')} edges at {format_si(f3, 'Hz')} from {ref}'s {got}: the fastest full "
                  f"{AMP_SWING_V:g} V swing it can make, not the 10 MHz / 2 ns default; set the real signal if it is "
                  "slower")


PATH_MAX_NETS = 24     # a labelled path's search stops growing here (it follows series parts, not a whole board)


def _labelled_path(board: Board, config: Config, net: str, marks: dict, by_net: dict
                   ) -> tuple[set[str], list[str]]:
    """The nets of the signal path `net` is on, between GT-labelled nets, and the parts on it that don't pass a
    signal by default (they go into the path's `through`).

    The path runs through 2-pin parts and through op-amp stages (a part modelled as an amplifier: from its input's
    net to its output's, components.models.amplifier_pins), never into a supply or reference net. Unlabelled nets
    count only where they lie between labelled ones: branches ending in an unlabelled net (a filter capacitor's
    ground side, a load resistor) are trimmed off, one dead end at a time. kraft_flux_mini_2's current sense: the
    ZXCT1107 U302's output (sig_drv:3) through the buffer U301 (amp_in:1, amp_out:4) and R303, 4.7 kΩ, to the ADC
    input /CPU/ADC_I_RAIL_1 (sig_rx:9 on U1102): one path, R303 in `through`."""
    from ..components.models import amplifier_pins
    refs = set(config.reference_nets)
    edges: dict[str, dict[str, Footprint]] = {net: {}}       # net -> {neighbouring net: the part joining them}
    frontier, seen = [net], {net}                             # seen: every net queued once, never again
    while frontier and len(edges) < PATH_MAX_NETS:
        n = frontier.pop()
        for fp in by_net.get(n, []):
            amp = amplifier_pins(fp, _model(config, fp))
            if amp is not None:
                nets = [p.net for p in amp]                 # an op-amp: only its input and output nets
            else:
                nets = [x for x in fp.nets() if x]
                if len(nets) != 2:
                    continue
            if n not in nets:
                continue
            for other in nets:
                if other == n or other in refs or (other not in marks and is_power_net(board, other, config)):
                    continue
                edges[n][other] = fp
                edges.setdefault(other, {})[n] = fp
                if other not in seen:
                    seen.add(other)
                    frontier.append(other)
    # trim dead ends: an unlabelled net with one neighbour leads nowhere the user marked
    trimmed = True
    while trimmed:
        trimmed = False
        for n in [n for n, nb in edges.items() if n not in marks and n != net and len(nb) <= 1]:
            for other in edges.pop(n):
                edges.get(other, {}).pop(n, None)
            trimmed = True
    joined = set(edges)
    through = sorted({fp.reference for n in joined for other, fp in edges[n].items()
                      if other in joined and amplifier_pins(fp, _model(config, fp)) is None
                      and not _model(config, fp).passes_signal()})
    return joined, through


def detect_nets(board: Board, config: Config, min_score: int = 2, skip: Optional[set[str]] = None
                ) -> list[DetectedNet]:
    """Signal nets worth analysing. `skip`: nets handled otherwise (the power paths of power stages detected but
    not yet in the settings: power_nets); the power paths of the settings' own stages are always skipped."""
    skip = (set(skip or ()) | set(power_nets(board, config, config.stages()))) - set(_marked(board, config))
    by_net, fp_of = _index(board)
    done: set[str] = set()
    out: list[DetectedNet] = []
    for net in board.nets:
        if net in done or net.lower().startswith("unconnected") or is_power_net(board, net, config):
            continue
        if skip and net in skip:
            continue
        found, group = _suggest(board, config, net, by_net, fp_of, picked=False, min_score=min_score)
        if found is None:
            continue
        found.covers = frozenset(group)
        out.append(found)
        done |= group
    out.sort(key=lambda d: (-d.score, {"high": 0, "medium": 1, "low": 2}[d.confidence], d.spec.name))
    return out


def suggest_net(board: Board, config: Config, net: str, as_signal: bool = False) -> tuple[Optional[DetectedNet], str]:
    """A suggestion for one net the user picked (the net of a track selected in KiCad): driver, receivers and
    timing as detect_nets gives them, with doubts in the reasons instead of a refusal. (None, why) for nets
    that are no signals: reference and supply nets, power-stage switch nodes, nets without pads. as_signal: the
    user said it is a signal (a supply-looking net that measures one: "Add it anyway"): not refused as a supply."""
    if net not in board.nets:
        return None, f"{net} is not on the board as read (save it in KiCad, then Reload board)"
    if net in config.reference_nets:
        return None, f"{net} is a reference net: GroundTruth analyses the return current in it"
    stage = next((c for c in config.stages() if c.switch_node == net), None)
    if stage is not None and stage.bridge:
        return None, f"{net} is the phase node of leg {stage.phase} of the motor bridge {stage.bridge} (Bridges tab)"
    if stage is not None:
        return None, f"{net} is the switch node of the power stage {stage.name} (Converters tab)"
    as_signal = as_signal or net in _marked(board, config)       # a GT sig label says so
    role = None if as_signal else power_nets(board, config, config.stages()).get(net)
    if role and not is_power_net(board, net, config):
        tab = "Bridges" if " of the motor bridge " in role else "Converters"
        return None, (f"{net} is {role}: it carries the stage's current, not a signal (a sense or feedback pin on it "
                      f"does not make it one). It is analysed with that stage on the {tab} tab.")
    if is_power_net(board, net, config) and not as_signal:
        return None, _supply_reason(board, config, net)
    by_net, fp_of = _index(board)
    found, group = _suggest(board, config, net, by_net, fp_of, picked=True)
    if found is not None:
        found.covers = frozenset(group)
    if found is None:
        return None, f"{net} has no pads: nothing drives or receives it"
    return found, ""


def _path_key(spec: NetSpec) -> tuple:
    """What a net's path is: its driver, receivers and pass-through parts (timing and the rest left aside)."""
    return spec.driver, tuple(spec.receivers), tuple(sorted(spec.through))


def refreshes(found: list[DetectedNet], nets: list[NetSpec]) -> tuple[list[tuple[DetectedNet, NetSpec]], list[str]]:
    """Listed nets that detection now sees differently, for Detect to replace: (updates, fragments).

    updates: (suggestion, the listed entry) where an entry of the same name is still detection's own and unreviewed
    (source auto, reviewed false) but its path differs (driver, receivers or pass-through parts). fragments: other
    unreviewed detected entries whose net lies inside a suggestion's path. A reviewed or hand-made entry is never
    touched. kraft_flux_mini_2 after its op-amp labels: /CPU/ADC_I_RAIL_1 was listed from C304.1, and its pieces
    Net-(U301-+) and Net-(U301--) on their own; detection now finds one path from U302.3, which replaces the first
    and covers the other two."""
    by_name = {n.name: n for n in nets}

    def unreviewed(n: NetSpec) -> bool:
        """Detection's own entry, not checked by the user yet."""
        return n.source == "auto" and not n.reviewed
    updates = [(d, by_name[d.spec.name]) for d in found if d.spec.name in by_name
               and unreviewed(by_name[d.spec.name]) and _path_key(by_name[d.spec.name]) != _path_key(d.spec)]
    names = {d.spec.name for d in found}
    fragments = sorted(n.name for n in nets if unreviewed(n) and n.name not in names
                       and any(n.name in d.covers for d in found))
    return updates, fragments


def power_nets(board: Board, config: Config, specs) -> dict[str, str]:
    """The nets carrying the current of these power stages, each with its role (analysis.power.stage_power_nets)."""
    if not specs:
        return {}
    from .power import stage_power_nets
    return stage_power_nets(board, config, specs)


def _supply_reason(board: Board, config: Config, net: str) -> str:
    """Why a supply net is not added to the Nets tab, and where its current is analysed instead."""
    def pin_nets(pins):
        """The nets of these pins ("J1.1", ...)."""
        return {p.net for p in (board.find_pin(x) for x in pins) if p is not None}
    found = [c for c in config.stages()
             if net == c.rail or net in pin_nets(c.input_connector) | pin_nets(c.output_connector)]
    stages = list(dict.fromkeys(c.bridge or c.name for c in found))
    tab = " or ".join(t for t, on in (("Converters", any(not c.bridge for c in found)),
                                      ("Bridges", any(c.bridge for c in found))) if on)
    head = (f"{net} is a supply net, not a signal: the Signal paths tab is for clock and data paths, whose return "
            "current GroundTruth follows.")
    if stages:
        return (f"{head}\nIts current is analysed with the power stage{'s' if len(stages) > 1 else ''} "
                f"{', '.join(stages)} on the {tab} tab "
                "(Simulate: hot loop, conducted emission and the DC current density of its input and output paths).")
    return (f"{head}\nA supply's current is analysed with its power stage on the Converters tab (Detect… finds the "
            "stages; set the input and output connectors there for the DC current density).")


# --------------------------------------------------------------------------- power stages
SW_PIN = re.compile(r"(^|[^A-Z])(SW|LX|PH|PHASE|SWITCH)\d*($|[^A-Z])", re.I)
VIN_PIN = re.compile(r"(^|[^A-Z])P?V_?\{?IN\}?\d*($|[^A-Z])|^\+?P?VIN|VCC_?IN", re.I)
VOUT_PIN = re.compile(r"(^|[^A-Z])V_?\{?OUT\}?\d*($|[^A-Z])", re.I)
GND_PIN = re.compile(r"(^|[^A-Z])(P|A)?GND\d*($|[^A-Z])|^(EP|PAD|VSS|RTN|PGND)$", re.I)
GATE_PIN = re.compile(r"^(G|GN|GATE|HG|LG|TG|BG)\d*($|_)", re.I)    # GN: EPC's GaN FETs (GN / DN / SN)
VOLTS = re.compile(r"(\d+)V(\d*)")
FAST_SWITCHES = re.compile(r"EPC|GaN|GAN", re.I)


@dataclass
class DetectedConverter:
    """A power stage detection suggests: its ConverterSpec (unreviewed), confidence, reasons, and part models to
    set with it."""
    spec: ConverterSpec
    confidence: str                      # high | medium | low
    reasons: list[str] = field(default_factory=list)
    models: dict = field(default_factory=dict)   # part models to suggest with it (a surge stopper's FET, closed)


def _volts(net: Optional[str]) -> Optional[float]:
    """+12V -> 12, +3V3 -> 3.3, 5V0 -> 5 (None when the name holds no voltage)."""
    if not net:
        return None
    m = VOLTS.search(net.split("/")[-1].upper())
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2) or 0}")


def _is_ground(config: Config, net: str) -> bool:
    """A ground net: a reference net, or a ground name (GND, PGND, AGND, VSS ...)."""
    from ..config.settings import GND_PATTERN
    return net in config.reference_nets or bool(GND_PATTERN.search(net.split("/")[-1]))


def _centre(pads) -> tuple[float, float]:
    """The mean position of the pads."""
    pads = list(pads)
    return (sum(p.position[0] for p in pads) / len(pads), sum(p.position[1] for p in pads) / len(pads))


def _power_net(board: Board, config: Config, fp: Footprint, exclude: str, zone_nets: set[str]) -> Optional[str]:
    """A transistor's other power terminal (not `exclude`, not the gate): the net with the most pads, preferring
    ground, poured and power-named nets; gate pins (by name, or the single pad of a three-net part) lose."""
    count = Counter(p.net for p in fp.pads if p.net and p.net != exclude)
    if not count:
        return None
    gates = {p.net for p in fp.pads if p.net and GATE_PIN.search(p.pin_function or "")}

    def score(net):
        """Rank a candidate net (compared as a tuple): not a gate first, then power-like, then most pads."""
        power = _is_ground(config, net) or net in zone_nets or bool(POWER_NAME.search(net.split("/")[-1]))
        return (net not in gates, power, count[net])
    return max(count, key=score)


def _diode_ends(fp: Footprint) -> Optional[tuple[str, str]]:
    """(cathode net, anode net) of a two-terminal diode: pin names K/A, else KiCad's pad 1 = cathode."""
    pads = [p for p in fp.pads if p.net]
    if len({p.net for p in pads}) != 2:
        return None
    k = next((p for p in pads if (p.pin_function or "").upper().split("_")[0] in ("K", "CATHODE", "C")), None)
    a = next((p for p in pads if (p.pin_function or "").upper().split("_")[0] in ("A", "ANODE")), None)
    if k is None and a is None:
        k = next((p for p in pads if p.number == "1"), None)
    if k is None and a is not None:
        k = next(p for p in pads if p.net != a.net)
    if k is None:
        return None
    return k.net, next(p.net for p in pads if p.net != k.net)


def _ic_nets(fp: Footprint, config: Config) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(input net, output net, ground net) of a converter IC from its pin names."""
    def first(rx):
        """The net of the first pad whose pin name matches rx (power_in pins first)."""
        pads = [p for p in fp.pads if p.net and rx.search(p.pin_function or "")]
        pads.sort(key=lambda p: p.pin_type != "power_in")
        return pads[0].net if pads else None
    gnd = first(GND_PIN) or next((p.net for p in fp.pads if p.net and _is_ground(config, p.net)), None)
    return first(VIN_PIN), first(VOUT_PIN), gnd


GATE_FN = re.compile(r"^G(ATE|N)?\d*$", re.I)
KELVIN_FN = re.compile(r"^(KS|SK|KELVIN|S_?K(ELVIN)?|SS)\d*$", re.I)     # a FET's Kelvin (driver) source pin
SENSE_PIN = re.compile(r"^I_?P[+-]$", re.I)                            # a current-sense IC's primary: IP+ / IP-
DRIVER_SW_PIN = re.compile(r"(^|[^A-Z])(SW|LX|PH|PHASE|SWITCH|HS|VS|HB|SH)[A-CU-W]?\d*($|[^A-Z])", re.I)
DRIVER_IN_PIN = re.compile(r"^(H|L)?IN[HL]?[A-CU-W]?\d*$|^IN_?[HL][A-CU-W]?\d*$|^PWM[HL]?[A-CU-W]?\d*$", re.I)
CONNECTOR_PREFIXES = ("J", "P", "CN", "X", "CON", "TB")


def fet_terms(fp: Footprint) -> Optional[tuple[str, set[str]]]:
    """(gate net, the two power nets) of a transistor. The gate by its pin name (G, GN, GATE), else the net with
    the fewest pads of a three-net part; a Kelvin-source pin is left out. None when that does not leave exactly
    one gate and two power nets."""
    pads = [p for p in fp.pads if p.net and not p.net.lower().startswith("unconnected")
            and not KELVIN_FN.match(p.pin_function or "")]
    nets = {p.net for p in pads}
    gates = {p.net for p in pads if GATE_FN.match(p.pin_function or "")}
    if not gates and len(nets) == 3:
        count = Counter(p.net for p in pads)
        gates = {min(sorted(nets), key=lambda n: count[n])}
    if len(gates) != 1 or len(nets - gates) != 2:
        return None
    return next(iter(gates)), nets - gates


def _switch_fets(board: Board) -> dict[str, tuple[str, set[str]]]:
    """The board's transistors with their (gate, power nets): Q / T / M parts, and any part labelled as a bridge
    leg's switch (GT hb_hs / hb_ls); never one labelled 'ignore'."""
    from .tags import board_tags
    tags = board_tags(board)
    leg_parts = tags.used("hb")
    return {fp.reference: t for fp in board.footprints
            if (ref_prefix(fp.reference) in ("Q", "T", "M") or fp.reference in leg_parts)
            and not fp.dnp and fp.reference not in tags.ignored and (t := fet_terms(fp)) is not None}


def _leg_nodes(board: Board, config: Config, fets: Optional[dict] = None) -> set[str]:
    """Nets between two transistors of which one goes to ground and one to another (non-ground) net: half-bridge
    switch nodes, a converter's or a motor bridge's."""
    fets = _switch_fets(board) if fets is None else fets
    to_ground, to_other = set(), set()
    for _gate, power in fets.values():
        for n in power:
            other = next(iter(power - {n}))
            (to_ground if _is_ground(config, other) else to_other).add(n)
    return {n for n in to_ground & to_other if not _is_ground(config, n)}


def pass_switches(board: Board, config: Config) -> dict[str, tuple[str, str]]:
    """Series pass FETs: (drain net, source net) of each FET that only connects two supply nets when on, like a
    surge stopper's or a reverse-polarity FET (Q601 between J101's V_BATT and +VDC on kraft_flux_mini_2): a gate
    pin, drain and source on two different nets, neither ground nor carrying an inductor (a switching FET always
    touches its switch node's inductor) nor a half-bridge's switch node (a motor bridge's high side), and no series
    model yet (the default 'open')."""
    from .tags import board_tags
    tags = board_tags(board)
    skip = tags.ignored | tags.used()          # labelled switches are no pass FETs
    inductor_nets = {n for fp in board.footprints if _model(config, fp).kind == "inductor" for n in fp.nets()}
    legs = _leg_nodes(board, config)
    out = {}
    for fp in board.footprints:
        if ref_prefix(fp.reference) not in ("Q", "T", "M") or fp.dnp or fp.reference in skip:
            continue
        if _model(config, fp).kind != "open" or not any(GATE_FN.match(p.pin_function or "") for p in fp.pads):
            continue
        power = {p.net for p in fp.pads if p.net and not GATE_FN.match(p.pin_function or "")
                 and not KELVIN_FN.match(p.pin_function or "")}
        if len(power) == 2 and not any(_is_ground(config, n) or n in inductor_nets or n in legs for n in power):
            d = next((p.net for p in fp.pads if (p.pin_function or "").upper().startswith("D") and p.net), None)
            a, b = sorted(power, key=lambda n: n != d)
            out[fp.reference] = (a, b)
    return out


def series_sensors(board: Board, config: Config) -> dict[str, tuple[str, str]]:
    """Current-sense ICs in series with a supply (an ACS71240's IP+ / IP- between a motor drive's input and its
    bus): (IP+ net, IP- net) of each one whose model is still the default 'open' (the path search takes them as
    closed between those two pins only)."""
    out = {}
    for fp in board.footprints:
        if fp.dnp or ref_prefix(fp.reference) not in ("U", "IC") or _model(config, fp).kind != "open":
            continue
        ends = {}
        for p in fp.pads:
            if p.net and SENSE_PIN.match(p.pin_function or ""):
                ends.setdefault((p.pin_function or "").upper().replace("_", ""), p.net)
        if len(ends) == 2 and len(set(ends.values())) == 2 and not any(_is_ground(config, n) for n in ends.values()):
            out[fp.reference] = (ends.get("IP+", ""), ends.get("IP-", ""))
    return out


def _series_model(ref: str, nets: tuple[str, str], sensor: bool) -> ComponentModel:
    """The model detection suggests for a part in series with a power path (a current sensor's primary, or a pass
    FET): a short, with a note on what the user should give it for an accurate figure."""
    a, b = nets
    if sensor:
        return ComponentModel("short", note=f"current sensor between {a} and {b} (IP+ / IP-), taken as closed: give "
                              "it its primary resistance as a resistor model", pins=["IP+", "IP-"])
    return ComponentModel("short", note=f"series pass FET between {a} and {b} (surge stopper or reverse-polarity "
                          "switch), taken as closed: give it its R_DS(on) as a resistor model")


def _closed(config: Config, refs, models: Optional[dict] = None) -> Config:
    """The config with these parts taken as closed switches (a short model, or the model given for them: a sensor
    closed between IP+ and IP- only), for the path search."""
    from dataclasses import replace
    from ..config.settings import ComponentEntry
    comps = dict(config.components)
    for r in refs:
        e = comps.get(r)
        comps[r] = ComponentEntry(r, e.value if e else "", e.footprint if e else "",
                                  (models or {}).get(r) or ComponentModel("short"))
    return replace(config, components=comps)


def _connector_for(board: Board, config: Config, rail: str, ground: str, stop: set[str], centre,
                   passes: Optional[dict] = None, only: Optional[list[str]] = None) -> Optional[list[str]]:
    """The connector of a rail (the input, or the output to the load): a J part with a pin on a net that reaches
    the rail through series parts (never through `stop`), and a pin on ground. The nearest one to the switching
    cell. With `passes` (pass_switches), a rail cut off by a surge-stopper FET is searched again with those FETs
    closed; the ones crossed are added to `passes['used']`. `only`: the connectors labelled for it (GT conv_in):
    searched first, whatever their reference."""
    for among in ([set(only)] if only else []) + [None]:
        found = _connector_search(board, config, rail, ground, stop, centre, among)
        if found is None and passes and passes.get("fets"):
            found = _connector_search(board, _closed(config, passes["fets"], passes.get("models")), rail, ground,
                                      stop, centre, among)
            if found:
                _note_passes(board, config, passes, rail, ground, stop, found)
        if found:
            return found
    return None


def _note_passes(board: Board, config: Config, passes: dict, rail: str, ground: str, stop: set[str], found):
    """The pass FETs the path from the connector `found` to the rail crosses, into passes['used']."""
    from .power import input_path, path_parts
    closed = _closed(config, passes["fets"], passes.get("models"))
    plus = board.find_pin(found[0])
    path = input_path(board, closed, rail, plus.net, {ground} | stop) or []
    passes.setdefault("used", []).extend(fp.reference for fp in path_parts(board, closed, path)
                                         if fp.reference in passes["fets"])


def _connector_search(board: Board, config: Config, rail: str, ground: str, stop: set[str], centre,
                      among: Optional[set[str]] = None) -> Optional[list[str]]:
    """[+ pin, ground pin] of the nearest connector reaching the rail (`among`: only these parts, any prefix)."""
    from .power import input_path
    best = None
    for fp in board.footprints:
        if (fp.reference not in among) if among is not None else ref_prefix(fp.reference) not in CONNECTOR_PREFIXES:
            continue
        gnd = [p for p in fp.pads if p.net == ground]
        if not gnd:
            continue
        for p in fp.pads:
            if not p.net or p.net == ground or p.net in stop:
                continue
            if input_path(board, config, rail, p.net, {ground} | stop) is None:
                continue
            d = math.dist(p.position, centre)
            if best is None or d < best[0]:
                best = (d, [p.pin, min(gnd, key=lambda q: math.dist(q.position, p.position)).pin])
    return best[1] if best else None


def _is_inductor(config: Config, fp: Footprint) -> bool:
    """A two-net part modelled as an inductor."""
    return _model(config, fp).kind == "inductor" and len(fp.nets()) == 2


def detect_converters(board: Board, config: Config) -> list[DetectedConverter]:
    """Switching converters: the labelled ones first (GT conv_hs / conv_ls / conv_sw / conv_L, analysis/tags.py),
    then every inductor with a switch on one end, among the parts that carry no label."""
    from .tags import board_tags
    tags = board_tags(board)
    zone_nets = {z.net for z in board.zones if z.net and z.is_filled}
    by_net = board.parts_by_net()
    out: list[DetectedConverter] = []
    seen: set[str] = set()
    pass_fets = pass_switches(board, config)
    # never picked up again by the search: ignored parts, bridge-leg parts and the parts of the converters built
    # from labels (a labelled stage that cannot be built leaves its parts to the search)
    labelled = tags.used("hb") | tags.ignored
    for st in tags.of("conv"):
        d = _tagged_converter(board, config, st, tags, by_net, zone_nets, pass_fets)
        if d is not None:
            out.append(d)
            if d.spec.switch_node:
                seen.add(d.spec.switch_node)
            labelled |= set(d.spec.parts) | set(st.refs("l"))
    for ind in board.footprints:
        if ind.dnp or ind.reference in labelled or not _is_inductor(config, ind):
            continue
        for sw in sorted(ind.nets()):
            if sw in seen or _is_ground(config, sw):
                continue
            parts = [fp for fp in by_net.get(sw, []) if fp is not ind and not fp.dnp and fp.reference not in labelled]
            fets = [fp for fp in parts if ref_prefix(fp.reference) in ("Q", "T", "M") and len(fp.nets()) >= 3]
            diodes = [fp for fp in parts if ref_prefix(fp.reference) == "D" and _diode_ends(fp)]
            ics = [fp for fp in parts if ref_prefix(fp.reference) in ("U", "IC")
                   and any(p.net == sw and SW_PIN.search(p.pin_function or "") for p in fp.pads)]
            if not fets and not diodes and not ics:
                continue
            d = _converter_at(board, config, ind, sw, fets, diodes, ics, zone_nets, pass_fets, tags)
            if d is not None:
                out.append(d)
                seen.add(sw)
    _unique_names(out)
    _interleave_proposals(board, out, by_net)
    out.sort(key=lambda d: d.spec.name)
    return out


def _stage_controller(board: Board, spec: ConverterSpec, by_net: dict) -> Optional[str]:
    """The IC that switches a stage: an integrated stage's own switch IC; else an IC (U / IC) with a pad on the
    switch node (a synchronous controller's SW pin) or on the high-side switch's gate net, directly or through a
    gate resistor (one R between the gate and the IC's pin). None when no IC (or more than one) qualifies."""
    if spec.switch:
        return spec.switch[0]
    nets = {spec.switch_node} if spec.switch_node else set()
    for ref in spec.high_side:
        fp = board.footprint(ref)
        t = fet_terms(fp) if fp is not None else None
        if t is None:
            continue
        nets.add(t[0])
        for r in by_net.get(t[0], []):           # a gate resistor: its other net
            if ref_prefix(r.reference) == "R" and len(r.nets()) == 2:
                nets |= r.nets() - {t[0]}
    ics = {fp.reference for n in nets if n for fp in by_net.get(n, [])
           if ref_prefix(fp.reference) in ("U", "IC") and not fp.dnp}
    return next(iter(ics)) if len(ics) == 1 else None


def _interleave_proposals(board: Board, found: list[DetectedConverter], by_net: dict):
    """Stages switched by one IC (the channels of a dual or multiphase controller, like kraft_flux_mini_2's LTC7890
    U303 for SW1 and SW2) become an interleave group named after it, 360°/n apart in name order: what most such
    controllers do, but some run their channels in phase or set the phase by a pin, so a reason asks to check the
    datasheet (analysis/interleave.py)."""
    by_ic: dict[str, list[DetectedConverter]] = {}
    for d in found:
        ic = _stage_controller(board, d.spec, by_net)
        if ic is not None:
            by_ic.setdefault(ic, []).append(d)
    for ic, ds in by_ic.items():
        if len(ds) < 2 or len({d.spec.f_sw for d in ds}) > 1:
            continue
        ds.sort(key=lambda d: d.spec.name)
        others = ", ".join(d.spec.name for d in ds)
        for k, d in enumerate(ds):
            d.spec.interleave = f"{ic} phases"
            d.spec.phase_deg = 360.0 * k / len(ds)
            d.reasons.append(f"interleaved with the other stages {ic} switches ({others}), {d.spec.phase_deg:g}° of "
                             f"{len(ds)}: check the controller's datasheet for its phases")


def _tagged_converter(board: Board, config: Config, st, tags, by_net: dict, zone_nets: set[str],
                      pass_fets: dict) -> Optional[DetectedConverter]:
    """A converter from its labelled parts: the switch node is the net its inductor shares with its switches (the
    inductor found on the switches' nets when none is labelled); the labelled roles are taken as given."""
    if not st.refs("sw") and not (st.refs("hs") and st.refs("ls")):
        return None                                 # board_tags reported what is missing
    fps = {r: board.footprint(r) for role in st.roles for r in st.roles[role]}
    switches = [fps[r] for r in st.refs("hs", "ls", "sw") if fps.get(r)]
    sw_nets = {n for fp in switches for n in fp.nets() if not _is_ground(config, n)}
    inds = [fps[r] for r in st.refs("l") if fps.get(r)]
    if not inds:
        inds = [fp for n in sw_nets for fp in by_net.get(n, []) if fp not in switches and not fp.dnp
                and _is_inductor(config, fp)]
    if not inds:
        tags.problems.append(f"converter {st.name}: no inductor (label it conv{st.ident}_L) on the switches' nets")
        return None
    ind = inds[0]
    shared = sorted(n for n in ind.nets() if n in sw_nets)
    if len(shared) > 1:          # an IC with a pin on both ends (U503: SW, and OUT sensing the output): its SW pin's
        on_sw_pin = [n for n in shared if any(p.net == n and SW_PIN.search(p.pin_function or "")
                                              for fp in switches for p in fp.pads)]
        shared = on_sw_pin if len(on_sw_pin) == 1 else shared
    if len(shared) != 1:
        tags.problems.append(f"converter {st.name}: inductor {ind.reference} shares "
                             f"{'no net' if not shared else 'two nets'} with its labelled switches")
        return None
    sw = shared[0]
    fets = [fp for fp in switches if fp.reference in st.refs("hs", "ls") and not _diode_ends(fp)]
    diodes = [fp for fp in switches if fp.reference in st.refs("hs", "ls") and _diode_ends(fp)]
    ics = [fp for fp in switches if fp.reference in st.refs("sw")]
    d = _converter_at(board, config, ind, sw, fets, diodes, ics, zone_nets, pass_fets, tags,
                      forced={"hs": set(st.refs("hs")), "ls": set(st.refs("ls"))}, st=st)
    if d is None:
        tags.problems.append(f"converter {st.name}: the labelled parts give no rail and ground (check the labels "
                             "hs / ls / sw and the parts' pin names)")
    return d


def _converter_at(board: Board, config: Config, ind: Footprint, sw: str, fets: list, diodes: list, ics: list,
                  zone_nets: set[str], pass_fets: dict, tags, forced: Optional[dict] = None, st=None
                  ) -> Optional[DetectedConverter]:
    """The converter with inductor `ind` on switch node `sw` and these candidate switches. `forced`: the labelled
    high- and low-side parts (their role is not guessed from which side goes to ground); `st`: their labels."""
    forced = forced or {"hs": set(), "ls": set()}
    other = next(n for n in ind.nets() if n != sw)
    reasons = ([f"labelled (GT): {st.describe()}"] if st is not None else []) + [
        f"switch node {sw}: inductor {ind.reference} ({ind.value}) to {other}"]
    hs, ls, rail, ground, switch = [], [], None, None, []
    topology, confidence = "buck", "high"
    for q in fets:
        net = _power_net(board, config, q, sw, zone_nets)
        if net is None:
            continue
        low = q.reference in forced["ls"] or (q.reference not in forced["hs"] and _is_ground(config, net))
        if low:
            ls.append(q.reference)
            ground = ground or net
        else:
            hs.append(q.reference)
            rail = rail or net
    for d in diodes:
        k, a_ = _diode_ends(d)
        if k == sw and (_is_ground(config, a_) or d.reference in forced["ls"]):
            ls.append(d.reference)
            ground = ground or a_
            reasons.append(f"{d.reference}: catch diode (cathode on the switch node, anode on {a_})")
        elif a_ == sw and (not _is_ground(config, k) or d.reference in forced["hs"]) and not hs:
            hs.append(d.reference)
            rail = rail or k
            topology = "boost"
            reasons.append(f"{d.reference}: boost diode (anode on the switch node, cathode on {k})")
    ic = ics[0] if ics else None
    if ic is not None and not hs:
        vin, vout, gnd = _ic_nets(ic, config)
        if ls:
            hs, rail = [ic.reference], vin
            reasons.append(f"{ic.reference} ({ic.value}): switches {vin} to the switch node "
                           f"(SW pin), {', '.join(ls)} is the low side")
        elif vin == other:
            topology, rail, ground, switch = "boost", vout, gnd, [ic.reference]
            reasons.append(f"{ic.reference} ({ic.value}): integrated boost (inductor from its input)")
        else:
            rail, ground, switch = vin, gnd, [ic.reference]
            reasons.append(f"{ic.reference} ({ic.value}): integrated power stage, hot loop from its "
                           f"{vin} pins to its {gnd} pins")
            confidence = "medium" if st is None else "high"
    if not rail or not ground or (not switch and not (hs and ls)):
        return None
    if hs and ls:
        reasons.append(f"high side {', '.join(hs)} ({rail} to {sw}), low side {', '.join(ls)} ({sw} to {ground})")
    if topology == "buck" and not switch:
        v_rail, v_other = _volts(rail), _volts(other)
        if v_rail and v_other and v_other > v_rail:
            topology = "boost"
            reasons.append(f"boost: {other} ({v_other:g} V) above {rail} ({v_rail:g} V)")
        elif not (v_rail and v_other):
            reasons.append("assumed a buck (check the topology)")
    v_in, v_out = (_volts(rail), _volts(other)) if topology == "buck" else (_volts(other), _volts(rail))
    centre = _centre(p for fp in (board.footprint(r) for r in hs + ls + switch) if fp for p in fp.pads)
    stop = {sw, other} if topology == "buck" else {sw}
    ident = st.ident if st is not None else None
    passes = {"fets": {r: n for r, n in pass_fets.items() if r not in hs + ls + switch}}
    only_in = tags.connector_refs("conv", "in", ident)
    conn = _connector_for(board, config, rail if topology == "buck" else other, ground, stop, centre, passes,
                          only=only_in)
    models = {ref: _series_model(ref, passes["fets"][ref], False) for ref in dict.fromkeys(passes.get("used", []))}
    if only_in and not (conn and board.find_pin(conn[0]).footprint in only_in):
        reasons.append(f"labelled input {', '.join(only_in)} does not reach {rail}: searched the board instead")
    if conn and models:
        reasons.append(f"input connector {conn[0]} / {conn[1]} through the series FET "
                       f"{', '.join(models)}, taken as closed (a short model; give it its R_DS(on))")
    elif conn:
        reasons.append(f"input connector {conn[0]} / {conn[1]}")
    else:
        reasons.append("no input connector found through series parts: set input_connector for the "
                       "conducted estimate (series switches need a short or resistor model)")
    only_out = tags.connector_refs("conv", "out", ident)
    out_conn = _connector_for(board, config, other if topology == "buck" else rail, ground,
                              {sw, rail} if topology == "buck" else {sw, other}, centre, only=only_out)
    if only_out and not (out_conn and board.find_pin(out_conn[0]).footprint in only_out):
        reasons.append(f"labelled output {', '.join(only_out)} does not reach {other if topology == 'buck' else rail}"
                       ": searched the board instead")
    if out_conn:
        reasons.append(f"output connector {out_conn[0]} / {out_conn[1]}")
    else:
        reasons.append("no output connector found: set output_connector (the load's + and - pins) for "
                       "the DC output path and ground return")
    fast = any(FAST_SWITCHES.search(f"{fp.value} {fp.lib_id}") for fp in
               (board.footprint(r) for r in hs + ls + switch) if fp)
    rise = 2e-9 if fast else (5e-9 if fets else 10e-9)
    reasons.append(f"defaults to review: f_sw 500 kHz, i_out 1 A, rise time {rise * 1e9:g} ns"
                   + (" (GaN)" if fast else ""))
    leaf = sw.split("/")[-1]
    if st is not None and (st.ident or st.sheet_name):       # labelled with an id, or in a named sheet
        name = f"{topology} {st.name}"
    else:
        name = f"{topology} {leaf}" if not switch else f"{topology} {switch[0]} {leaf}"
    spec = ConverterSpec(name, rail, ground, None if switch else sw, hs if not switch else [],
                         ls if not switch else [], switch, topology, 500e3, 1.0, v_in, v_out, None, rise,
                         input_connector=conn or [], output_connector=out_conn or [], source="auto",
                         reviewed=False, note="; ".join(reasons[:2 if st is not None else 1]))
    if not switch:
        spec.snubber = _snubber(board, config, sw, rail, ground, board.parts_by_net())
        if spec.snubber:
            reasons.append(f"snubber {spec.snubber[0]} + {spec.snubber[1]}")
    _from_fields(board, config, spec, reasons)
    return DetectedConverter(spec, confidence, reasons, models)


def _from_fields(board: Board, config: Config, spec, reasons: list[str]):
    """A detected stage's part parameters from its parts' schematic fields (components/fields.py), named in the
    reasons."""
    from ..components.fields import fill_stage
    _, notes = fill_stage(board, config, spec)
    if spec.from_schematic:
        reasons.append("from the schematic fields: " + ", ".join(spec.from_schematic))
    reasons.extend(notes)


# --------------------------------------------------------------------------- motor bridges
@dataclass
class DetectedBridge:
    """A motor bridge detection suggests: its BridgeSpec (legs found; the operating point and motor are the
    user's), confidence, reasons, and part models to set with it."""
    spec: BridgeSpec
    confidence: str                      # high | medium | low
    reasons: list[str] = field(default_factory=list)
    models: dict = field(default_factory=dict)   # part models to suggest with it (input-path sensors and FETs, closed)

    @property
    def complete(self) -> bool:
        """Three legs: a three-phase bridge, which Simulate handles (six-step)."""
        return len(self.spec.legs) == 3


def _series_reach(board: Board, config: Config, start: str, stop: set[str], by_net: dict, sensors: dict,
                  depth: int = 3) -> dict[str, list[str]]:
    """Nets reached from `start` through series parts that pass current (0R, shunts, ferrites, low-ohm resistors,
    current-sense ICs between IP+ and IP-), never through `stop`, each with the parts crossed."""
    from .power import INPUT_PATH_MAX_OHMS
    reach = {start: []}
    frontier = [start]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for fp in by_net.get(n, []):
                if fp.reference in sensors:
                    joined = set(sensors[fp.reference])
                else:
                    m = _model(config, fp)
                    ok = m.kind in ("short", "ferrite") or (m.kind == "resistor" and m.get("R") <= INPUT_PATH_MAX_OHMS)
                    if not ok or len(fp.nets()) != 2:
                        continue
                    joined = fp.nets()
                if n not in joined:
                    continue
                for other in joined:
                    if other and other not in reach and other not in stop:
                        reach[other] = reach[n] + [fp.reference]
                        nxt.append(other)
        frontier = nxt
    return reach


def _split_input(board: Board, config: Config, rail: str, ground: str, stop: set[str], centre,
                 passes: dict, among: Optional[set[str]] = None) -> Optional[list[str]]:
    """The bus input as two separate connectors (an ESC's battery wire tabs: J2 on +BATT, J3 on IS-, the
    current-sense shunt's far side): the + pin reaching the rail and the - pin reaching ground through series parts
    (with `config`'s pass parts closed), the + one nearest the bridge, the - one nearest it. The parts crossed go
    to passes['used']. `among`: only these parts (labelled hb_in), any prefix."""
    from .power import input_path, path_parts
    plus, minus = [], []
    for fp in board.footprints:
        if (fp.reference not in among) if among is not None else ref_prefix(fp.reference) not in CONNECTOR_PREFIXES:
            continue
        for p in fp.pads:
            if not p.net or p.net in stop:
                continue
            many = len(fp.nets()) > 2           # a signal header's supply pin is no bus input
            to_rail = input_path(board, config, rail, p.net, {ground} | stop)
            if to_rail is not None:
                plus.append(((many, math.dist(p.position, centre)), p, to_rail))
                continue
            to_gnd = input_path(board, config, ground, p.net, {rail} | stop)
            if to_gnd is not None:
                minus.append((many, p, to_gnd))
    if not plus or not minus:
        return None
    _, p_pad, path = min(plus, key=lambda c: c[0])
    _, m_pad, m_path = min(minus, key=lambda c: (c[0], math.dist(c[1].position, p_pad.position)))
    for pth in (path, m_path):
        passes.setdefault("used", []).extend(fp.reference for fp in path_parts(board, config, pth)
                                             if fp.reference in passes.get("fets", {}))
    return [p_pad.pin, m_pad.pin]


def _phase_label(output_pad: Optional[Pad], node: str) -> Optional[str]:
    """A leg's phase from its connector pin (J1.A, U/V/W, 1/2/3) or its node's name (PH_A, PHASE_U, SW_B)."""
    if output_pad is not None and re.fullmatch(r"[A-CU-W1-3]", str(output_pad.number), re.I):
        return str(output_pad.number).upper()
    if not node:
        return None
    m = re.search(r"(?:^|[_\-/ ])(?:PH(?:ASE)?|SW|OUT|MOT(?:OR)?)?[_\-]?([A-CU-W1-3])$", node.split("/")[-1], re.I)
    return m.group(1).upper() if m else None


def _leg_driver(board: Board, config: Config, node: str, gates: list[str], by_net: dict, fets: set[str]
                ) -> tuple[str, list[str]]:
    """(driver IC, its input nets) of a leg: the IC with a switch-node pin (SW, HS, VS) on the node, else the one
    IC the gate nets reach through up to two resistors, diodes or beads. The inputs are its HIN / LIN / PWM pins'
    nets: where the switching signals come from (information for the user)."""
    def inputs(fp, sw_pin: str = ""):
        """The driver IC's input nets (HIN / LIN / PWM pins, not supplies)."""
        pads = [p for p in fp.pads if p.net and DRIVER_IN_PIN.match(p.pin_function or "")
                and not is_power_net(board, p.net, config)]
        # a three-phase driver (DRV8300: SHA / INHA / INLA ...): the inputs with this leg's phase letter
        letter = re.fullmatch(r"(?:SH|SW|PH|HS|VS|HB)([A-CU-W])", sw_pin, re.I)
        own = [p for p in pads if letter and (p.pin_function or "").upper().endswith(letter.group(1).upper())]
        return sorted({p.net for p in (own or pads)})
    for fp in by_net.get(node, []):
        if ref_prefix(fp.reference) in ("U", "IC"):
            sw = next((p.pin_function or "" for p in fp.pads
                       if p.net == node and DRIVER_SW_PIN.search(p.pin_function or "")), None)
            if sw is not None:
                return fp.reference, inputs(fp, sw)
    found, seen, frontier = set(), set(gates), list(gates)
    for _ in range(2):
        nxt = []
        for n in frontier:
            for fp in by_net.get(n, []):
                if fp.reference in fets:
                    continue
                if ref_prefix(fp.reference) in ("U", "IC"):
                    found.add(fp.reference)
                elif ref_prefix(fp.reference) in ("R", "D", "FB", "L") and len(fp.nets()) == 2:
                    for o in fp.nets():
                        if o not in seen and not is_power_net(board, o, config):
                            seen.add(o)
                            nxt.append(o)
        frontier = nxt
    if len(found) == 1:
        ref = found.pop()
        return ref, inputs(board.footprint(ref))
    return "", []


def _snubber(board: Board, config: Config, node: str, rail: str, ground: str, by_net: dict) -> list[str]:
    """[C, R] of an RC snubber on the node: a capacitor from the node to a net that holds only a resistor on to the
    rail or ground (e.g. a C + R from a motor bridge's phase node PH_A to its bus)."""
    for c in by_net.get(node, []):
        if _model(config, c).kind != "cap" or len(c.nets()) != 2:
            continue
        mid = next(n for n in c.nets() if n != node)
        others = [fp for fp in by_net.get(mid, []) if fp is not c]
        if len(others) == 1 and _model(config, others[0]).kind == "resistor" and len(others[0].nets()) == 2:
            far = next(n for n in others[0].nets() if n != mid)
            if far in (rail, ground):
                return [c.reference, others[0].reference]
    return []


@dataclass
class _Leg:
    """A half-bridge leg found on the board, before the legs are grouped into bridges."""
    node: str
    hs: list[str]
    ls: list[str]
    rail: str
    ground: str
    reasons: list[str]
    out: Optional[Pad] = None            # the motor connector pin
    via: list[str] = field(default_factory=list)       # the series parts between the node and that pin
    driver: str = ""
    inputs: list[str] = field(default_factory=list)
    snubber: list[str] = field(default_factory=list)
    centre: tuple = (0.0, 0.0)
    phase: Optional[str] = None          # from a label (hbA_hs)
    sheet_phase: Optional[str] = None    # from the sheet copy's name ("Phase B")
    labelled: bool = False


class _LegFinder:
    """The legs of a board: the labelled ones (GT hb_hs / hb_ls), then every other node with a transistor to a rail
    and one to ground and no inductor on it."""

    def __init__(self, board: Board, config: Config, tags, sensors: dict):
        """The finder for this board: its labels, the current sensors found, and every transistor's power nets."""
        self.board, self.config, self.tags, self.sensors = board, config, tags, sensors
        self.by_net, self.fp_of = _index(board)
        self.fets = _switch_fets(board)

    def far_end(self, ref: str, node: str) -> str:
        """A transistor's power net other than the node."""
        return next(iter(self.fets[ref][1] - {node}))

    def legs(self) -> list[_Leg]:
        """Every leg: the labelled ones, then the unlabelled nodes (not a converter's switch node, nor a node a
        labelled leg already took), with the transistors no label claimed."""
        out = [leg for st in self.tags.of("hb") if (leg := self.labelled(st)) is not None]
        taken_nodes = {leg.node for leg in out}
        taken = self.tags.used("conv") | {r for leg in out for r in leg.hs + leg.ls}
        free = {r: t for r, t in self.fets.items() if r not in taken}
        converter_nodes = {c.switch_node for c in self.config.converters if c.switch_node}
        for node in sorted(_leg_nodes(self.board, self.config, self.fets)):
            if node not in converter_nodes and node not in taken_nodes:
                leg = self.unlabelled(node, free)
                if leg is not None:
                    out.append(leg)
        return out

    def labelled(self, st) -> Optional[_Leg]:
        """The leg a GT label set makes: its high and low sides must be transistors that share exactly one net
        (the node). Problems go to the labels' problem list; None when it can't be made."""
        hs = sorted((r for r in st.refs("hs") if r in self.fets), key=_natural_key)
        ls = sorted((r for r in st.refs("ls") if r in self.fets), key=_natural_key)
        bad = [r for r in st.refs("hs", "ls") if r not in self.fets]
        if bad:
            self.tags.problems.append(f"leg {st.name}: {', '.join(bad)} not read as a transistor (one gate pin, G / "
                                      "GN / GATE, and two power pins)")
        if not hs or not ls:
            return None
        nodes = set.intersection(*(self.fets[r][1] for r in hs + ls))
        if len(nodes) != 1:
            self.tags.problems.append(f"leg {st.name}: the labelled high and low sides share "
                                      f"{'no net' if not nodes else 'more than one net'}")
            return None
        node = nodes.pop()
        rail, ground, _ = self.rail_ground(node, hs, ls)
        leaf = node.split("/")[-1]
        return self.leg(node, hs, ls, rail, ground, [
            f"leg {leaf}: labelled (GT) {st.describe()}: high side {', '.join(hs)} ({rail.split('/')[-1]} to "
            f"{leaf}), low side {', '.join(ls)} ({leaf} to {ground.split('/')[-1]})"], st)

    def unlabelled(self, node: str, free: dict) -> Optional[_Leg]:
        """A leg at `node` from the unlabelled transistors: those to a non-ground net are the high side, those to
        ground the low side. None without both, or when an inductor hangs on the node (a converter's)."""
        on_node = [r for r, (_, power) in free.items() if node in power]
        hs = sorted((r for r in on_node if not _is_ground(self.config, self.far_end(r, node))), key=_natural_key)
        ls = sorted((r for r in on_node if _is_ground(self.config, self.far_end(r, node))), key=_natural_key)
        if not hs or not ls:
            return None
        rail, ground, n_rails = self.rail_ground(node, hs, ls)
        reach = _series_reach(self.board, self.config, node, {rail, ground}, self.by_net, self.sensors)
        if any(len(fp.nets()) == 2 and _model(self.config, fp).kind == "inductor"
               for n in reach for fp in self.by_net.get(n, [])):
            return None                       # a converter's switch node (perhaps through a 0R or shunt)
        leaf = node.split("/")[-1]
        reasons = [f"leg {leaf}: high side {', '.join(hs)} ({rail.split('/')[-1]} to {leaf}), low side "
                   f"{', '.join(ls)} ({leaf} to {ground.split('/')[-1]}), no inductor on the node"]
        if n_rails > 1:
            reasons.append(f"high-side parts go to {n_rails} rails; took {rail}")
        return self.leg(node, hs, ls, rail, ground, reasons, reach=reach)

    def rail_ground(self, node: str, hs: list[str], ls: list[str]) -> tuple[str, str, int]:
        """(the rail most high sides go to, the ground most low sides go to, how many rails the high sides reach)."""
        rails = Counter(self.far_end(r, node) for r in hs)
        grounds = Counter(self.far_end(r, node) for r in ls)
        return rails.most_common(1)[0][0], grounds.most_common(1)[0][0], len(rails)

    def leg(self, node, hs, ls, rail, ground, reasons, st=None, reach=None) -> _Leg:
        """The leg with its motor connector pin (a labelled hb_out first), driver, snubber and phase hints."""
        board, config, by_net = self.board, self.config, self.by_net
        if reach is None:
            reach = _series_reach(board, config, node, {rail, ground}, by_net, self.sensors)
        own_out = set(self.tags.connector_refs("hb", "out", st.ident if st is not None else None))

        def outputs(keep):
            """(pad, the parts in series to it) of every pad of the parts `keep` accepts, on a net the node reaches."""
            return [(p, reach[n]) for n in reach for fp in by_net.get(n, []) if keep(fp)
                    for p in fp.pads if p.net == n]
        found = outputs(lambda fp: fp.reference in own_out) if own_out else []
        if own_out and not found:
            reasons.append(f"labelled motor connector {', '.join(sorted(own_out))} not reached from {node}")
        if not found:
            found = outputs(lambda fp: ref_prefix(fp.reference) in CONNECTOR_PREFIXES)
        centre = _centre(p for r in hs + ls for p in self.fp_of[r].pads)
        out_pad, via = min(found, key=lambda o: (len(o[1]), math.dist(o[0].position, centre))) if found else (None, [])
        driver, inputs = _leg_driver(board, config, node, sorted({self.fets[r][0] for r in hs + ls}), by_net,
                                     set(self.fets))
        from .tags import phase_from_name
        return _Leg(node, hs, ls, rail, ground, reasons, out_pad, via, driver, inputs,
                    _snubber(board, config, node, rail, ground, by_net), centre,
                    phase=st.phase if st is not None else None,
                    sheet_phase=phase_from_name(self.fp_of[hs[0]].sheet_name), labelled=st is not None)


def _group_legs(legs: list[_Leg]) -> dict[tuple, list[_Leg]]:
    """Legs on one bus and ground form a bridge; more than three are split by their driver, else by their motor
    connector (two motor bridges on one battery)."""
    groups: dict[tuple, list] = {}
    for leg in legs:
        groups.setdefault((leg.rail, leg.ground), []).append(leg)
    for key in [k for k, v in groups.items() if len(v) > 3]:
        members = groups.pop(key)
        for split_by in (lambda m: m.driver, lambda m: m.out.footprint if m.out is not None else None):
            parts: dict = {}
            for m in members:
                parts.setdefault(split_by(m), []).append(m)
            if len(parts) > 1:
                break
        for tag, ms in parts.items():
            groups[(*key, str(tag))] = ms
    return groups


def _phase_labels(members: list[_Leg]) -> list[str]:
    """Each leg's phase: from labels, else the motor connector's pins, else the node names, else the sheets' names
    (the first that gives every leg a different one), else numbers."""
    for candidate in ([m.phase for m in members],
                      [_phase_label(m.out, None) for m in members],
                      [_phase_label(None, m.node) for m in members],
                      [m.sheet_phase for m in members]):
        if None not in candidate and len(set(candidate)) == len(candidate):
            return candidate
    return [str(k + 1) for k in range(len(members))]


def detect_bridges(board: Board, config: Config) -> list[DetectedBridge]:
    """Motor bridges: half-bridge legs whose switch node has no inductor (the motor's winding is the load, off the
    board), grouped by bus rail, ground and output connector. A leg is a node with a transistor to ground (low side)
    and one to a single non-ground rail (high side); a node that reaches an inductor through series parts is a
    converter's switch node, and a pair of FETs of which none goes to ground (a load switch, ORing) is no leg."""
    from .tags import board_tags
    tags = board_tags(board)
    sensors = series_sensors(board, config)
    legs = _LegFinder(board, config, tags, sensors).legs()
    pass_fets = pass_switches(board, config)
    out = [_bridge(board, config, tags, sensors, pass_fets, gkey[0], gkey[1], members)
           for gkey, members in sorted(_group_legs(legs).items(), key=lambda kv: tuple(map(str, kv[0])))]
    names = Counter(d.spec.name for d in out)
    for d in out:
        if names[d.spec.name] > 1:
            d.spec.name = f"{d.spec.name} {d.spec.legs[0].switch_node.split('/')[-1]}"
    return out


def _bridge(board: Board, config: Config, tags, sensors: dict, pass_fets: dict, rail: str, ground: str,
            members: list[_Leg]) -> DetectedBridge:
    """One bridge from its legs: phases, the bus input connector, the reasons and the defaults to review."""
    labels = _phase_labels(members)
    order = sorted(range(len(members)), key=lambda k: labels[k])
    members, labels = [members[k] for k in order], [labels[k] for k in order]
    reasons = [r for m in members for r in m.reasons]
    n = len(members)
    confidence = "high" if n == 3 else ("low" if n > 3 else "medium")
    if n != 3:
        reasons.append(f"{n} leg{'s' if n != 1 else ''}: only three-phase bridges are simulated yet (six-step)"
                       + ("; split it into bridges of three" if n > 3 else ""))
    for m, lab in zip(members, labels):
        if m.out is not None:
            reasons.append(f"leg {lab} to the motor at {m.out.pin}" + (f" through {', '.join(m.via)}" if m.via else ""))
        if m.driver:
            reasons.append(f"leg {lab} driven by {m.driver}" + (f" from {', '.join(m.inputs)}" if m.inputs else ""))
        if m.snubber:
            reasons.append(f"leg {lab} snubber {m.snubber[0]} + {m.snubber[1]}")
    if any(m.out is None for m in members):
        reasons.append("no motor connector found on some legs: set their output pin")
    centre = tuple(sum(m.centre[k] for m in members) / n for k in (0, 1))
    nodes = {m.node for m in members}
    passes = {"fets": {**pass_fets, **sensors}, "models": {r: _series_model(r, nn, True) for r, nn in sensors.items()}}
    closed = _closed(config, passes["fets"], passes["models"])
    only_in = [t.ref for t in tags.connectors if t.kind == "hb" and t.role == "in"]
    conn = None
    if only_in:                       # labelled bus input (hb_in): one connector, or the two battery tabs
        conn = ((_connector_for(board, config, rail, ground, nodes, centre, passes, only=only_in)
                 if len(only_in) == 1 else None)
                or _split_input(board, closed, rail, ground, nodes, centre, passes, among=set(only_in)))
        if conn is None:
            reasons.append(f"labelled bus input {', '.join(only_in)} does not reach {rail.split('/')[-1]} and "
                           "ground: searched the board instead")
    conn = (conn or _connector_for(board, config, rail, ground, nodes, centre, passes)
            or _split_input(board, closed, rail, ground, nodes, centre, passes))
    if any(m.labelled for m in members) and n == 3:
        confidence = "high"
    models = {r: _series_model(r, sensors[r], True) if r in sensors else _series_model(r, pass_fets[r], False)
              for r in dict.fromkeys(passes.get("used", []))}
    if conn:
        reasons.append(f"bus input connector {conn[0]} / {conn[1]}" +
                       (f" through {', '.join(models)}, taken as closed (short models; give them their "
                        "resistance)" if models else ""))
    else:
        reasons.append("no bus input connector found: set input_connector for the conducted estimate")
    switches = [board.footprint(r) for m in members for r in m.hs + m.ls]
    fast = any(FAST_SWITCHES.search(f"{fp.value} {fp.lib_id}") for fp in switches)
    reasons.append("to set: the bus voltage, PWM frequency, phase current, speed and the motor (line-to-line R, L, "
                   "back-EMF constant, pole pairs); defaults to review: H-PWM / L-ON synchronous, dead time 50 ns, "
                   f"edge {2 if fast else 10} ns" + (" (GaN)" if fast else ""))
    values = {fp.value for fp in switches}
    if len(values) == 1:
        reasons.append(f"switches: {values.pop()} (check the part fitted for R_DS(on), C_oss and Q_oss)")
    conns = {m.out.footprint for m in members if m.out is not None}
    name = f"bridge {conns.pop()}" if len(conns) == 1 else f"bridge {rail.split('/')[-1]}"
    spec = BridgeSpec(name, rail, ground, legs=[
        LegSpec(lab, m.node, m.hs, m.ls, output=m.out.pin if m.out is not None else "", driver=m.driver,
                inputs=m.inputs, snubber=m.snubber) for m, lab in zip(members, labels)],
        input_connector=conn or [], rise_time=2e-9 if fast else 10e-9, source="auto", reviewed=False,
        note=reasons[0])
    _from_fields(board, config, spec, reasons)
    if spec.v_sd is None:                       # no Vsd field: GaN's reverse conduction, or a silicon body diode
        spec.v_sd = 2.0 if fast else 0.8
    return DetectedBridge(spec, confidence, reasons, models)


def _unique_names(found: list[DetectedConverter]):
    """Several stages can share a switch-node leaf name (copies of a stage under different sheets: RAIL A/SW1 and
    RAIL A1/SW1): name them by more of the net path, else by their first switch."""
    from collections import Counter
    count = Counter(d.spec.name for d in found)
    for d in found:
        s = d.spec
        if count[s.name] < 2:
            continue
        net = s.switch_node or ""
        tail = "/".join(p for p in net.split("/")[-2:] if p)
        s.name = f"{s.topology} {tail}" if tail and tail != net.split("/")[-1] else s.name
    count = Counter(d.spec.name for d in found)
    for d in found:
        if count[d.spec.name] > 1:
            d.spec.name = f"{d.spec.name} ({(d.spec.high_side or d.spec.switch)[0]})"
