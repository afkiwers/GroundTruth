"""A signal path from what the user selected in KiCad: one bit of copper on each net of the path (a track, a via
or a pad), in any order. The nets are put in order by the parts that join them (2-pin series parts, op-amps), the
direction comes from the op-amps (input pin on one net, output pin on the next) or from the pin types at the ends,
and the result is everything a hand-labelled path has: the path (a NetSpec with its Pass-through parts), the
op-amps' amplifier models with their pins, and the GT labels for the schematic symbols (analysis/tags.py).

refboard's current sense: a track of Net-(U301-+), one of Net-(U301--) and one of /CPU/ADC_I_RAIL_1 give
U302.3 → U301 (amp_in:1, amp_out:4) → R303 4.7 kΩ (Pass through) → U1102.9, named /CPU/ADC_I_RAIL_1, with the labels
U302 sig_drv:3, U301 amp_in:1, amp_out:4, U1102 sig_rx:9.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..components.models import ComponentModel
from ..config.settings import Config, NetSpec
from ..core.board import Board, Footprint, Pad
from .detect import DRIVER_RANK, GENERIC, NOT_ENDPOINTS, _model, ref_prefix

OUT_TYPES = ("output", "tri_state", "open_collector", "open_emitter")
AMP_MAX_PADS = 16     # an op-amp stage is a small part (single to quad op-amps); a microcontroller on two of the
                      # selected nets is not one, only a part the path starts or ends at


@dataclass
class PathProposal:
    """A path built from a selection: the NetSpec, the part models to set (op-amps as amplifiers with their input
    and output pins), the GT label text per symbol (the existing labels kept, the path's added), what was
    inferred (shown to the user), and `error` when no single path could be made (then the rest is empty)."""
    spec: Optional[NetSpec] = None
    models: dict[str, ComponentModel] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class _Link:
    """Two selected nets joined by a part: a 2-pin series part, or an op-amp stage (from its input's net to its
    output's, `pins` = (input pad, output pad))."""
    part: Footprint
    a: str
    b: str
    pins: Optional[tuple[Pad, Pad]] = None


def _links(board: Board, nets: list[str]) -> tuple[list[_Link], list[str], list[str]]:
    """The parts joining the selected nets: (links, problems, hints). A 2-pin part on two selected nets is a series
    link; a small part (AMP_MAX_PADS) with more pins on exactly two selected nets is an op-amp stage when one of
    those nets has only its input pins and the other one of its output pins (that sets the direction). A small
    part whose pins don't tell becomes a hint (used only if the nets don't join otherwise); a part on more than
    two selected nets is a problem."""
    chosen = set(nets)
    links, problems, hints = [], [], []
    for fp in board.footprints:
        if fp.dnp:
            continue
        on = {p.net for p in fp.pads if p.net in chosen}
        if len(on) < 2:
            continue
        if len(on) > 2:
            problems.append(f"{fp.reference} joins {len(on)} of the selected nets ({', '.join(sorted(on))}): "
                            "select one path at a time")
            continue
        a, b = sorted(on)
        if len({p.net for p in fp.pads if p.net}) == 2:
            links.append(_Link(fp, a, b))
            continue
        outs = {n: [p for p in fp.pads if p.net == n and p.pin_type in OUT_TYPES] for n in (a, b)}
        ins = {n: [p for p in fp.pads if p.net == n and p.pin_type == "input"] for n in (a, b)}
        if len(fp.pads) > AMP_MAX_PADS:
            continue                        # a big IC on two of the nets: an end of the path, not a stage on it
        if outs[a] and not outs[b] and ins[b]:
            links.append(_Link(fp, b, a, (ins[b][0], outs[a][0])))
        elif outs[b] and not outs[a] and ins[a]:
            links.append(_Link(fp, a, b, (ins[a][0], outs[b][0])))
        else:
            hints.append(f"{fp.reference} joins {a} and {b}, but its pins don't say which is its input and which "
                         "its output: label it in the schematic (GT amp_in:<pin>, amp_out:<pin>)")
    return links, problems, hints


def _order(nets: list[str], links: list[_Link]) -> tuple[list[str], str]:
    """The selected nets as one chain from end to end (each net joined to at most two others, all connected),
    or ([], why not)."""
    nb: dict[str, set[str]] = {n: set() for n in nets}
    for k in links:
        nb[k.a].add(k.b)
        nb[k.b].add(k.a)
    branch = [n for n, s in nb.items() if len(s) > 2]
    if branch:
        return [], f"{branch[0]} is joined to {len(nb[branch[0]])} of the selected nets: a path has no branches"
    ends = [n for n, s in nb.items() if len(s) <= 1]
    if len(nets) > 1 and not ends:
        return [], "the selected nets form a loop"
    chain, prev = [ends[0]], None
    while True:
        nxt = [n for n in nb[chain[-1]] if n != prev]
        if not nxt:
            break
        prev = chain[-1]
        chain.append(nxt[0])
    if len(chain) != len(nets):
        alone = [n for n in nets if n not in chain]
        return [], f"no part joins {', '.join(alone)} to the others (select one bit of each net along one path)"
    return chain, ""


def _ends(board: Board, config: Config, net: str, skip: set[str]) -> list[Pad]:
    """The pads on `net` that can drive or receive the path: an IC's or connector's (a part modelled 'open'),
    not one of the path's own parts, nor a test point."""
    out = []
    for p in board.pads():
        if p.net != net or p.footprint in skip or ref_prefix(p.footprint) in NOT_ENDPOINTS:
            continue
        fp = board.footprint(p.footprint)
        if fp is None or fp.dnp or _model(config, fp).kind != "open":
            continue
        out.append(p)
    return out


def _rank(p: Pad) -> float:
    """How likely a pin drives (detection's DRIVER_RANK by its schematic pin type)."""
    return DRIVER_RANK.get(p.pin_type, 1)


def _add_labels(old: str, words: list[str], amp: bool = False) -> str:
    """A GT field's text with these labels added (those already there kept, none twice). amp: the part is an
    op-amp stage on the path, so its own signal labels (sig, sig_drv, sig_rx) go: they would cut the path at it
    (refboard's U301 had sig_drv:4 before it was labelled as the op-amp it is)."""
    have = [w.strip() for w in (old or "").replace(";", ",").split(",") if w.strip()]
    if amp:
        have = [w for w in have if not w.lower().replace(" ", "").startswith("sig")]
    low = {w.lower().replace(" ", "") for w in have}
    return ", ".join(have + [w for w in words if w.lower() not in low])


def path_from_selection(board: Board, config: Config, nets: list[str], pins: list[str] = ()) -> PathProposal:
    """One path through the selected nets (two or more), see the module. `pins`: pads selected in KiCad ("U302.3"):
    one on an end net fixes that end's driver or receiver."""
    from .tags import tag_text
    out = PathProposal()
    nets = [n for n in dict.fromkeys(nets) if n in board.nets and n not in config.reference_nets]
    if len(nets) < 2:
        out.error = "select one bit of copper on each of at least two nets of the path"
        return out
    links, problems, hints = _links(board, nets)
    if problems:
        out.error = problems[0]
        return out
    chain, why = _order(nets, links)
    if not chain:
        out.error = hints[0] if hints and why.startswith("no part joins") else why
        return out
    parts = {k.part.reference for k in links}
    # direction: an op-amp's input -> output; else the end with the likelier driver pin; else the selection order
    amps = [k for k in links if k.pins is not None]
    if amps:
        k = amps[0]
        if chain.index(k.a) > chain.index(k.b):
            chain.reverse()
        if any(chain.index(x.a) > chain.index(x.b) for x in amps):
            out.error = "the op-amps on the selected nets point in different directions"
            return out
        out.notes.append(f"direction from {k.part.reference}'s input and output pins")
    start, stop = _ends(board, config, chain[0], parts), _ends(board, config, chain[-1], parts)
    picked = [board.find_pin(p) for p in pins]
    picked = [p for p in picked if p is not None and p.net in (chain[0], chain[-1]) and p.footprint not in parts]
    if not amps:
        best = lambda ps: max((_rank(p) for p in ps), default=-1.0)      # noqa: E731
        if best(stop) > best(start) or (best(stop) == best(start) and picked and picked[0].net == chain[-1]):
            chain.reverse()
            start, stop = stop, start
        out.notes.append("direction from the pin types at the ends" if best(start) != best(stop)
                         else "direction from the selection order: check the driver")
    drivers = [p for p in picked if p.net == chain[0]] or sorted(start, key=_rank, reverse=True)[:1]
    receivers = [p for p in picked if p.net == chain[-1]] or [p for p in stop if _rank(p) < 4] or stop
    if not drivers or not receivers:
        out.error = (f"no IC or connector pin on {chain[0] if not drivers else chain[-1]} to "
                     f"{'drive' if not drivers else 'receive'} the path: select its pad too")
        return out
    driver = drivers[0]
    receivers = [p for p in receivers if p.footprint != driver.footprint]
    through = sorted({k.part.reference for k in links if k.pins is None
                      and not _model(config, k.part).passes_signal()})
    named = [n for n in reversed(chain) if not n.startswith("Net-(")]
    name = named[0] if named else chain[0]
    out.spec = NetSpec(name, driver.pin, [p.pin for p in receivers], GENERIC.freq, GENERIC.rise_time,
                       source="auto", reviewed=False, signal_class=GENERIC.name, through=through)
    for k in amps:
        a_in, a_out = k.pins
        m = config.model_for(k.part.reference)
        if m is None or m.kind != "amplifier" or (m.pins and list(m.pins[:2]) != [a_in.number, a_out.number]):
            out.models[k.part.reference] = ComponentModel(
                "amplifier", dict(m.params) if m is not None and m.kind == "amplifier" else {},
                "an op-amp stage on a path picked in KiCad", [a_in.number, a_out.number])
    words: dict[str, list[str]] = {}
    words.setdefault(driver.footprint, []).append(f"sig_drv:{driver.number}")
    for p in receivers:
        words.setdefault(p.footprint, []).append(f"sig_rx:{p.number}")
    for k in amps:
        words.setdefault(k.part.reference, []).extend([f"amp_in:{k.pins[0].number}", f"amp_out:{k.pins[1].number}"])
    for ref, ws in words.items():
        fp = board.footprint(ref)
        old = tag_text(fp) if fp is not None else ""
        new = _add_labels(old, ws, amp=ref in {k.part.reference for k in amps})
        same = sorted(w.strip().lower().replace(" ", "") for w in old.replace(";", ",").split(",") if w.strip())
        if sorted(w.strip().lower().replace(" ", "") for w in new.split(",") if w.strip()) != same:
            out.labels[ref] = new                       # only where the labels change
    out.notes.insert(0, " → ".join([driver.pin, *[f"{k.part.reference}" for k in _in_order(chain, links)],
                                    ", ".join(p.pin for p in receivers)]))
    return out


def _in_order(chain: list[str], links: list[_Link]) -> list[_Link]:
    """The links along the chain, from its start (one per pair of neighbouring nets)."""
    out = []
    for a, b in zip(chain, chain[1:]):
        out += [k for k in links if {k.a, k.b} == {a, b}][:1]
    return out
