"""A power stage's hot loop as a drawing for the board (Results tab, "Draw hot loop on board"; the user: "draw a
contour about the actual hot loop on layer User.1"), from a Simulate's result: no solve of its own.

What the solve found (analysis/power.py) is drawn, not a guess from the parts' placement: at the hot-loop frequency
(LOOP_F) the loop current leaves the rail capacitors in the shares `caps[i]["share_hf"]` and changes layer in the
vias of `transfers` (each with the `fraction` of the loop current it carries). Drawn per stage:
- the outline of the loop: every element carrying at least MIN_SHARE of the loop current (the switches' pads on
  the rail, switch-node and ground nets, those vias and capacitors) inside one polygon, their convex hull 0.4 mm
  clear of the pads. The hull is the plan view of the area the loop current spans: an upper bound of its
  magnetic area, not the area itself (the return runs under the forward path on the plane below) [A77];
- one path per such capacitor: its rail pad → the high-side switch's rail pads → its switch-node pads → the
  low-side switch's switch-node pads → its ground pads → the capacitor's ground pad, with its share written below
  it: which capacitors close the loop and how far away they are;
- a title: the stage, its loop inductance and ringing, the share the drawn capacitors carry, the outline's area.
The loop is drawn because it is the layout's main lever on emission: its magnetic dipole field goes with the
current switched times the area the loop covers, so the rules are a loop as small as possible, the shortest and
flattest ceramic capacitor closing it first, larger ones behind it, a solid ground plane on the next layer
[Kueck 2018]. The drawing shows which capacitors actually close it, and how far from the switches they are.
Everything in board millimetres; adapters/kipy_sink.draw_hot_loop puts it on the board.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

MIN_SHARE = 0.05            # the smallest share of the loop current an element needs to be part of the drawing
PATH_MM, OUTLINE_MM = 0.08, 0.25     # line widths: a capacitor's path, the outline
TITLE_MM, SHARE_MM = 0.8, 0.5        # text sizes


@dataclass
class HotLoopDrawing:
    """One stage's hot loop for the board: lines [(points, width mm, closed)], texts [(x, y, text, size mm)]
    (left end at x, y), the console / status lines, the outline's area (mm²) and the drawn capacitors' share."""
    stage: str
    lines: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    log: list = field(default_factory=list)
    area_mm2: float = 0.0
    share: float = 0.0


def _centroid(fp, net: str) -> Optional[tuple[float, float]]:
    """The mean position of a footprint's pads on a net (None: it has none)."""
    ps = [p.position for p in fp.pads if p.net == net]
    if not ps:
        return None
    return sum(x for x, _ in ps) / len(ps), sum(y for _, y in ps) / len(ps)


def _loop_nets(hs, ls, caps: list) -> Optional[tuple[str, str, str]]:
    """(rail, switch node, ground) of a stage: the rail capacitors' two nets are its rail and ground, the ground
    being the one the low-side switch (or catch diode) has; the switch node is the net both switches have besides
    those. From the capacitors because an integrated switch's exposed pad on ground has more pads than its rail
    pins. None when they don't fit together."""
    ls_nets = {p.net for p in ls.pads}
    for fp in caps:
        nets = list(dict.fromkeys(p.net for p in fp.pads if p.net))
        if len(nets) != 2:
            continue
        gnd = next((n for n in nets if n in ls_nets), None)
        if gnd is None:
            continue
        rail = nets[1 - nets.index(gnd)]
        common = ({p.net for p in hs.pads} & ls_nets) - {gnd, rail, None, ""}
        if common:
            sw = max(common, key=lambda n: sum(p.net == n for p in hs.pads) + sum(p.net == n for p in ls.pads))
            return rail, sw, gnd
    return None


def can_draw(rr) -> str:
    """"" when view `rr` is a power stage's hot loop this module can draw, else why not (for the button's tip)."""
    if rr is None:
        return "Please run the simulation first"
    if getattr(rr, "kind", "") != "converter" or getattr(rr, "view_of", None) or getattr(rr, "rms_of", None):
        return "pick a power stage's view (a converter, not its DC paths) to draw its hot loop"
    if getattr(rr, "error", ""):
        return f"{rr.net} was not solved: {rr.error}"
    if not getattr(rr, "caps", None) or rr.loop_nh is None:
        return f"{rr.net} has no hot-loop result to draw (no rail capacitors or loop inductance)"
    return ""


def hot_loop_drawing(board, rr, min_share: float = MIN_SHARE, k: int = 0) -> HotLoopDrawing:
    """Stage `rr`'s (a ConverterResult, or its results.json dict through a namespace) hot loop on `board`, see the
    module. k: the stage's number among several drawn together: its labels move down by it, so stages that share
    capacitors don't write over each other. Raises ValueError when the stage's switches are not on the board."""
    from shapely.geometry import MultiPoint
    out = HotLoopDrawing(rr.net)
    hs, ls = board.footprint(rr.driver), board.footprint(rr.receiver)
    by_share = sorted(rr.caps, key=lambda c: -c.get("share_hf", 0))
    cap_fps = [fp for fp in (board.footprint(c["name"]) for c in by_share) if fp is not None]
    nets = _loop_nets(hs, ls, cap_fps) if hs is not None and ls is not None else None
    if nets is None:
        raise ValueError(f"{rr.net}: no hot loop to draw from its parts' pads: {rr.driver} / {rr.receiver} share no "
                         "switch node besides its rail capacitors' nets (an integrated stage has its loop inside)")
    rail, sw, gnd = nets
    spine = [_centroid(hs, rail), _centroid(hs, sw), _centroid(ls, sw), _centroid(ls, gnd)]
    pts = [p.position for fp in (hs, ls) for p in fp.pads if p.net in nets]
    pts += [(v["x"], v["y"]) for v in rr.transfers if v.get("kind") == "via" and v.get("fraction", 0) >= min_share]
    tag = rr.net.split()[-1]                             # "conv1" of "buck conv1"
    caps = [c for c in rr.caps if c.get("share_hf", 0) >= min_share and board.footprint(c["name"]) is not None]
    for c in caps:
        fp = board.footprint(c["name"])
        plus, minus = _centroid(fp, rail), _centroid(fp, gnd)
        if plus is None or minus is None:               # not across rail and ground (an input filter's part)
            continue
        pts += [p.position for p in fp.pads]
        out.lines.append(([plus] + spine + [minus], PATH_MM, False))
        out.texts.append((minus[0] - 0.6, minus[1] + 1.8 + 0.8 * k, f"{tag} {c['share_hf'] * 100:.0f}%", SHARE_MM))
        out.share += c["share_hf"]
        out.log.append(f"{c['name']} {c['share_hf'] * 100:.0f} %, {math.dist(plus, spine[0]):.1f} mm from "
                       f"{rr.driver}")
    hull = MultiPoint(pts).convex_hull.buffer(0.4, join_style=1).simplify(0.05)
    ring = list(hull.exterior.coords)[:-1]
    out.lines.append((ring, OUTLINE_MM, True))
    out.area_mm2 = float(hull.area)
    ringing = (getattr(rr, "ringing", None) or {}).get("f")
    title = (f"GroundTruth: {rr.net} hot loop, {rr.loop_nh:.2f} nH at {rr.loop_f / 1e6:.0f} MHz"
             + (f", rings at {ringing / 1e6:.0f} MHz" if ringing else "")
             + f"; capacitors >= {min_share * 100:.0f} %: {out.share * 100:.0f} % of the loop current; "
             f"outline {out.area_mm2:.0f} mm2")
    x0, y0 = min(x for x, _ in ring), min(y for _, y in ring)
    out.texts.append((x0, y0 - 1.0 - 1.2 * k, title, TITLE_MM))
    out.log.insert(0, title)
    return out
