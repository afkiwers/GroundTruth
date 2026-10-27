"""What to show on the board for an issue: the copper it is about, for KiCad to select, highlight and zoom to
when the user clicks the issue in the plugin's Issues tab. KiCad-independent: works on the board model, the
settings and the results the window already holds.

An issue's own `items` name at most one offending segment (Tier 1) or nothing at all (simulation and power-stage
issues), so the copper is looked up here from the board model by check:

  focus    uuids the view zooms to and whose nets KiCad highlights: the track segments over a gap or along an
           edge, the via of a layer change, the pads of a missing connection, the whole route of a simulation
           issue, a power stage's switches, capacitors and switch-node copper;
  context  uuids selected after the zoom, so they show without pulling the view out: the plane zones beside a
           gap, the plane whose edge a trace follows, the plane carrying a foreign return;
  layer    the layer to make active: the reference plane of a gap, edge or foreign plane, so it is drawn on top;
  lit      the focus copper whose nets KiCad highlights: KiCad lights up the nets of everything selected when the
           highlight runs, so only the issue's own signal nets (or a power stage's switch node) are selected then,
           never a stitching via's or return pin's ground and never a reference net.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np

from ..config.settings import Config
from ..core.board import Board, Track, Zone
from ..core.issues import Issue

PIECE_CHECKS = ("gap_crossing", "off_plane", "edge_proximity", "reference_net")
ROUTE_CHECKS = ("return_path_broken", "return_detour", "plane_resonance", "emi_margin")
# issues about parts rather than copper: their footprints are what is shown (_parts_issue)
PART_CHECKS = ("cap_voltage", "resistor_power", "esd_protection", "part_temperature", "decap_missing")
NEAR_MM = 1.5          # around a piece of trace: the plane zones on either side of a gap or along an edge
SMALL_MM = 3.0         # a focus smaller than this is zoomed out twice after the fit, below SMALL_MM*3 once
MIN_VIEW_MM = 1.0      # a focus smaller than this is zoomed out to about this much first (the user: "1mm is pretty
                       # much spot on")
ZOOM_STEP = 1.58       # KiCad's zoom out: its preset zoom levels (… 50, 80, 130, 220 …) are ~1.58x apart
ZOOM_STEPS_MAX = 12


def zoom_steps(span_mm: float, extra: int = 0, reach_mm: float = 0.0) -> int:
    """How many of KiCad's zoom-out steps follow "zoom to the selection" for a focus span_mm across: a focus under
    MIN_VIEW_MM is first stepped out to about that (log(1 mm / span) / log(ZOOM_STEP), rounded up: a 0.6 mm via 2),
    then the Preferences' `extra` steps ("Zoom out in KiCad"). KiCad's API can neither set nor read the view, only
    fit the selection and step out, so the setting is a count of KiCad's own steps, not millimetres: a millimetre
    target computed from assumed fit margins and step sizes showed 2-4x too much board (the user: "15mm is too far
    zoomed out. And 1mm is pretty much spot on"; "Let's not call it mm, maybe call it just steps?").
    reach_mm: the view must show this much (a spot beside the focus, Highlight.reach): the steps from the fit up to
    it too."""
    import math
    base = 0 if span_mm >= MIN_VIEW_MM else int(math.ceil(math.log(MIN_VIEW_MM / max(span_mm, 0.05))
                                                           / math.log(ZOOM_STEP)))
    fit = max(span_mm, MIN_VIEW_MM)
    if reach_mm > fit:
        base += int(math.ceil(math.log(reach_mm / fit) / math.log(ZOOM_STEP)))
    return min(base + max(int(extra), 0), ZOOM_STEPS_MAX)
PLANE_MM = 25.0        # a zone wider than this is a plane: selected after the zoom, never zoomed to
CLEARANCE_ZOOM_MM = 6.0  # a voltage-spacing issue zooms to the two sides' copper up to this size (_clearance)
CLEARANCE_NEAR_MM = 2.0  # ... and as close to the gap as this (else the small copper around it)


@dataclass
class Highlight:
    """What to show in KiCad for one issue (see the module docstring for focus, context, layer and lit), how far
    to zoom out after fitting, the focus's bounding box, and a line for the status bar."""
    focus: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    layer: str = ""
    nets: bool = True                    # False: no net highlight for this issue (set by the check's rule)
    lit: list[str] = field(default_factory=list)   # focus copper on the nets to highlight (computed last)
    zoom_out: int = 0                    # zoom steps out after fitting the focus, so a short piece shows in context
    area: Optional[tuple[float, float, float, float]] = None   # bounding box of the focus, mm
    reach: float = 0.0                   # mm the view must span around the focus (a spot beside it: _clearance)
    text: str = ""

    @property
    def empty(self) -> bool:
        """Nothing found to show."""
        return not self.focus and not self.context


def issue_highlight(issue: Issue, board: Board, config: Config, routes: Sequence = ()) -> Highlight:
    """The copper to show for `issue`. `routes`: the window's simulation results (RouteResult, ConverterResult),
    used for a route's return transfers and a power stage's capacitors; may be empty."""
    if issue.check == "clearance_voltage":
        return _clearance(issue, board, config)
    if issue.check in PART_CHECKS:
        return _parts_issue(issue, board, config)
    if issue.check in PIECE_CHECKS and not issue.key.startswith("noplane:"):
        h = _piece(issue, board, config)
    elif issue.check in ("no_reference", "off_plane"):
        h = _run_on_layer(issue, board, config)
    elif issue.check in ("layer_change_stitch", "layer_change_decap"):
        h = _layer_change(issue, board, config)
    elif issue.check == "route_missing":
        h = _missing_route(issue, board, config)
    elif any(b.name == issue.net for b in config.bridges):      # a motor bridge's issue: all its legs
        return stages_highlight([issue.net], board, config, routes)
    elif any(c.name == issue.net for c in config.stages()):
        h = _power_stage(issue, board, config, routes)
    elif issue.check in ROUTE_CHECKS:
        h = _route_issue(issue, board, config, routes)
    else:
        h = Highlight(focus=_valid(issue.items), text="the issue's items")
    lit_nets = _lit_nets(issue, board, config) if h.nets else set()
    return _finish(h, board, config, lit_nets)


def _path_nets(board: Board, config: Config, names: Sequence[str]) -> set[str]:
    """The nets to light for rows of the Nets tab: each listed net's whole signal path (route.signal_nets: through
    series parts, its Pass through parts and op-amp stages) plus its driver's and receivers' nets, never a
    reference net. A name not in the settings is its own net only. (Lighting the named net alone stopped
    refboard's /CPU/ADC_I_RAIL_1 at R303, short of the ZXCT1107 that drives it.)"""
    from .route import signal_nets
    specs = {n.name: n for n in config.nets}
    want = set(names)
    for name in names:
        spec = specs.get(name)
        if spec is None:
            continue
        want |= signal_nets(board, config, name, spec.through)
        for pin in [spec.driver, *spec.receivers]:
            pad = board.find_pin(pin)
            if pad is not None and pad.net:
                want.add(pad.net)
    return want - set(config.reference_nets)


def nets_highlight(names: Sequence[str], board: Board, config: Config) -> Highlight:
    """The copper of nets picked in the Nets tab (tracks, vias, pads, pours), all of it lit; planes are selected
    after the zoom. No names: an empty Highlight, which clears what an earlier show left in KiCad."""
    names = [n for n in dict.fromkeys(names) if n in board.nets]
    if not names:
        return Highlight(nets=False, text="no net")
    want = _path_nets(board, config, names)
    pads = [p.uuid for p in board.pads() if p.net in want]
    copper = _copper_of(board, want, zones=True)
    tracks = sum(t.net in want for t in board.tracks)
    vias = sum(v.net in want for v in board.vias)
    what = names[0] if len(names) == 1 else f"{len(names)} nets ({', '.join(names)})"
    if len(want) > len(names):
        what += f", its path through {len(want)} nets"
    h = Highlight(focus=pads + copper, text=f"{what}: {_n(len(pads), 'pad')}, {_n(tracks, 'track segment')}, "
                                            f"{_n(vias, 'via')}")
    return _finish(h, board, config, want)


POINT_MM = 0.15        # a heatmap click lands on an item when its copper is within this of the point


def point_highlight(board: Board, config: Config, x: float, y: float, layer: str = "", text: str = "") -> Highlight:
    """The copper at a point of a heatmap, where the user clicked a marker (an overloaded via, a via or part carrying
    current): the via there, else the pad, else the track segment, on `layer` when given (a via passes every layer).
    Its net is lit unless it is a reference net (lighting GND would light the whole board). `text`: the status
    line (by default the item's name). Empty when nothing is there."""
    from shapely.geometry import Point
    from ..geometry.shapes import pad_shape
    at = Point(x, y)
    vias = [v for v in board.vias if math.dist(v.position, (x, y)) <= v.size / 2 + POINT_MM]
    pads = [p for p in board.pads() if p.uuid and (not layer or layer in p.layers)
            and (s := pad_shape(p)) is not None and s.distance(at) <= POINT_MM]
    tracks = [t for t in board.tracks if (not layer or t.layer == layer)
              and _dist_track(t, (x, y)) <= t.width / 2 + POINT_MM]
    if vias:
        v = min(vias, key=lambda v: math.dist(v.position, (x, y)))
        uuid, net, what = v.uuid, v.net, f"via {v.net} at ({v.position[0]:.2f}, {v.position[1]:.2f})"
    elif pads:
        p = min(pads, key=lambda p: math.dist(p.position, (x, y)))
        uuid, net, what = p.uuid, p.net, f"pad {p.pin} ({p.net})"
    elif tracks:
        t = min(tracks, key=lambda t: _dist_track(t, (x, y)))
        uuid, net, what = t.uuid, t.net, f"track {t.net} on {t.layer}"
    else:
        return Highlight(nets=False, text=f"no via, pad or track at ({x:.2f}, {y:.2f})")
    h = Highlight(focus=[uuid], layer=layer, text=text or what)
    return _finish(h, board, config, {net} if net else set())


def area_highlight(board: Board, config: Config, x: float, y: float, layer: str = "", half: float = 3.0,
                   text: str = "") -> Highlight:
    """The copper around a spot of a heatmap (a current-density tile's hot spot), for KiCad to zoom to: the view can
    only be zoomed to selected items (zoomFitSelection; the API has no "show this point"), and a hot spot inside a
    pour has no item of its own (point_highlight finds none on an inner plane: the user, "clicking on RMS current
    density only zooms into the heatmap. I need it to zoom into the PCB as well"). So the focus is the small copper
    wholly inside the 2·half square around the spot: vias, pads on `layer`, track segments on `layer` with both
    ends inside; those of the spot's own net (the pour or item at the spot) when it has any there, else every net.
    The item right at the spot comes first and its net is lit (never a reference net); the pour at the spot is
    selected after the zoom (context), so it shows without pulling the view out to all of it. With nothing small in
    the square, the item at the spot alone (its own extent), else the pour (its extent). `text`: the status line."""
    from shapely.geometry import Point, box
    from ..geometry.shapes import pad_shape
    at = Point(x, y)
    square = box(x - half, y - half, x + half, y + half)
    here = point_highlight(board, config, x, y, layer, text=text)
    pours = [z for z in board.zones if not z.is_rule_area and (not layer or layer in z.filled)
             and any(_shape(p).covers(at) for L, polys in z.filled.items() if not layer or L == layer
                     for p in polys)]
    net_of = {t.uuid: t.net for t in board.tracks} | {v.uuid: v.net for v in board.vias} | \
        {p.uuid: p.net for p in board.pads() if p.uuid}
    net = next((net_of[u] for u in here.focus if u in net_of), None) or (pours[0].net if pours else None)

    def inside_track(t):
        """A track segment on the layer with both ends (and an arc's middle) inside the square."""
        ends = [t.start, t.end] + ([t.mid] if t.mid is not None else [])
        return (not layer or t.layer == layer) and all(square.covers(Point(p)) for p in ends)
    small = [(v.uuid, v.net) for v in board.vias if square.covers(Point(v.position))]
    small += [(p.uuid, p.net) for p in board.pads() if p.uuid and (not layer or layer in p.layers)
              and (s := pad_shape(p)) is not None and square.covers(s)]
    small += [(t.uuid, t.net) for t in board.tracks if inside_track(t)]
    own = [u for u, n in small if net and n == net]
    focus = own or [u for u, _ in small]
    focus = [u for u in here.focus if u in focus] + [u for u in focus if u not in here.focus]   # the spot's first
    if not focus:
        focus = here.focus or [z.uuid for z in pours[:1]]
    h = Highlight(focus=focus, context=[z.uuid for z in pours if z.uuid not in focus], layer=layer,
                  text=text or (here.text if not here.empty else f"the copper around ({x:.2f}, {y:.2f})"))
    return _finish(h, board, config, {net} if net else set())


def stages_highlight(names: Sequence[str], board: Board, config: Config, routes: Sequence = ()) -> Highlight:
    """Power stages picked in the Converters tab: their switches and hot-loop capacitors (as the hot-loop issue
    shows them), and the copper of their whole power flow, all lit: input path, rail, switch node, the inductor's
    output side and the output path (analysis.power.stage_power_nets). Never the ground (or another reference
    net): it would light up the whole board."""
    from .detect import power_nets
    specs = list({s.name: s for n in names for s in config.stage_specs(n)}.values())   # a bridge: its legs
    if not specs:
        return Highlight(nets=False, text="no power stage")
    each = [_power_stage(Issue("hot_loop", "info", c.name, "", (0.0, 0.0), key=f"power:{c.name}:loop"),
                         board, config, routes) for c in specs]
    grounds = {c.ground for c in specs} | set(config.reference_nets)
    flow = [n for n, role in power_nets(board, config, specs).items() if n not in grounds]
    parts = [u for one in each for u in one.focus if u in {fp.uuid for fp in board.footprints}]
    h = Highlight(focus=parts + _copper_of(board, set(flow), zones=True),
                  text="; ".join(one.text.split(" and the switch node")[0] for one in each)
                  + (f"; power flow: {', '.join(flow)}" if flow else ""))
    return _finish(h, board, config, set(flow))


def _finish(h: Highlight, board: Board, config: Config, lit_nets: set[str]) -> Highlight:
    """Planes out of the zoom, the lit copper, the bounding box and the zoom-out steps."""
    h.focus = _valid(h.focus)
    planes = _planes(board)
    if any(u not in planes for u in h.focus):    # a plane in the focus would pull the zoom out to all of it
        h.context = [u for u in h.focus if u in planes] + h.context
        h.focus = [u for u in h.focus if u not in planes]
    h.context = [u for u in _valid(h.context) if u not in h.focus]
    # KiCad highlights the nets of all the copper selected at that moment (and with none selected, the copper under
    # the mouse): only the focus copper on the issue's own nets is selected then
    net_of = {t.uuid: t.net for t in board.tracks} | {v.uuid: v.net for v in board.vias} | \
        {z.uuid: z.net for z in board.zones} | {p.uuid: p.net for p in board.pads()}
    h.lit = [u for u in h.focus if net_of.get(u) in lit_nets - set(config.reference_nets)] if h.nets else []
    h.nets = bool(h.lit)
    h.area = _bbox(board, h.focus)
    if h.area:
        span = max(h.area[2] - h.area[0], h.area[3] - h.area[1])
        h.zoom_out = 2 if span < SMALL_MM else 1 if span < 3 * SMALL_MM else 0
    return h


# ------------------------------------------------------------------------------------------------ per check
def _clearance(issue: Issue, board: Board, config: Config) -> Highlight:
    """A voltage spacing too small (safety.check_clearances): the issue sits in the gap between two nets' copper,
    where no item is (the user: "clicking on a clearance_voltage check, it does not zoom into the location": it had
    no items, so nothing was selected to zoom to). Each of the two nets' copper on the issue's layer closest to the
    gap (a via, pad or track segment, else its pour) is selected and both nets lit (never a reference net). KiCad
    can only zoom to what is selected, so only small items (CLEARANCE_ZOOM_MM) right at the gap (CLEARANCE_NEAR_MM)
    are the zoom's focus; a long track or a pour would pull the view out to all of it (62 mm for a segment on
    refboard) and is selected after the zoom (context). With neither side small and near, the small copper of any
    net nearest the spot anchors the zoom (_small_around)."""
    from shapely.geometry import Point
    from ..geometry.shapes import pad_shape
    at, layer = Point(issue.location), issue.layer
    nets = [n for n in (issue.net, (issue.details or {}).get("other")) if n]
    sides, found = [], []
    for net in nets:
        near = [(math.dist(v.position, issue.location) - v.size / 2, v.uuid) for v in board.vias if v.net == net]
        near += [(s.distance(at), p.uuid) for p in board.pads() if p.net == net and p.uuid
                 and (not layer or layer in p.layers) and (s := pad_shape(p)) is not None]
        near += [(_dist_track(t, issue.location) - t.width / 2, t.uuid) for t in board.tracks
                 if t.net == net and (not layer or t.layer == layer)]
        pours = [(min(_shape(poly).distance(at) for poly in z.filled.get(layer, [])), z.uuid) for z in board.zones
                 if z.net == net and not z.is_rule_area and z.filled.get(layer)]
        small = min(near, default=None)
        pour = min(pours, default=None)
        # the small item when it is (about) as close as the pour: the pour's edge is the gap only when no pad or
        # track of the net lies there
        best = small if small is not None and (pour is None or small[0] <= pour[0] + 0.05) else pour
        if best is not None:
            sides.append(best)
            found.append(net)
    gap = (issue.details or {}).get("gap_mm")
    text = f"{' and '.join(found) or 'no copper'} on {layer}" + (f", {gap:g} mm apart" if gap else "")
    focus = [u for d, u in sides if d <= CLEARANCE_NEAR_MM and _span(board, u) <= CLEARANCE_ZOOM_MM]
    rest = [u for _, u in sides if u not in focus]
    if not focus:
        focus = _small_around(board, issue.location, layer)
    h = _finish(Highlight(focus=focus, context=rest, layer=layer, text=text), board, config, set(found))
    if h.area:
        # the view fits the focus and steps out: far enough that the gap beside it shows too (both ways round the
        # focus's middle, and a millimetre around the gap)
        cx, cy = (h.area[0] + h.area[2]) / 2, (h.area[1] + h.area[3]) / 2
        h.reach = 2 * math.dist((cx, cy), issue.location) + 1.0
    return h


def _parts_issue(issue: Issue, board: Board, config: Config) -> Highlight:
    """An issue about parts, not copper (a capacitor's voltage, a resistor's loss, a part's temperature, a connector
    without ESD protection, an IC without decoupling): their footprints selected and zoomed to, and an ESD issue's
    bare pins (the user: "pressing on a cap_voltage entry does not highlight the part on the PCB": the issue named
    no item, so nothing was selected). The parts come from the issue's details ("parts", "part"), its key's last
    field (cap_voltage:C1, thermal:part:Q1) or its net, which these checks set to the part's reference. Nothing is
    lit: the issue is about the part, not a net."""
    details = issue.details or {}
    refs = list(details.get("parts") or []) + ([details["part"]] if details.get("part") else [])
    for name in (issue.key.rsplit(":", 1)[-1] if issue.key else "", issue.net):
        if not refs and name and board.footprint(name) is not None:
            refs = [name]
    fps = [fp for fp in (board.footprint(r) for r in dict.fromkeys(refs)) if fp is not None]
    pins = {str(p) for p in details.get("pins") or []}
    pads = [p.uuid for fp in fps for p in fp.pads if p.pin in pins and p.uuid]
    focus = pads + [fp.uuid for fp in fps if fp.uuid]
    names = ", ".join(fp.reference for fp in fps[:6]) + (" …" if len(fps) > 6 else "")
    h = Highlight(focus=focus, nets=False, layer="",
                  text=(names + (f" (pins {', '.join(sorted(pins))})" if pads else "")) if fps else "no part found")
    return _finish(h, board, config, set())


def _span(board: Board, uuid: str) -> float:
    """The larger side of an item's bounding box, mm (inf for a zone or an unknown item: _bbox has no zones)."""
    box = _bbox(board, [uuid])
    return max(box[2] - box[0], box[3] - box[1]) if box else math.inf


def _small_around(board: Board, at, layer: str, most: int = 3) -> list[str]:
    """The `most` small items (vias, pads on `layer`, track segments on `layer`, each at most CLEARANCE_ZOOM_MM
    across) of any net nearest the spot, looked for within 3, then 6, then 12 mm: something for KiCad to zoom to
    where the spot's own copper is all large (two pours side by side, a long track)."""
    from shapely.geometry import Point
    from ..geometry.shapes import pad_shape
    point = Point(at)
    found = [(math.dist(v.position, at) - v.size / 2, v.uuid) for v in board.vias]
    found += [(s.distance(point), p.uuid) for p in board.pads() if p.uuid and (not layer or layer in p.layers)
              and (s := pad_shape(p)) is not None]
    found += [(_dist_track(t, at) - t.width / 2, t.uuid) for t in board.tracks if not layer or t.layer == layer]
    for reach in (3.0, 6.0, 12.0):
        close = sorted((d, u) for d, u in found if d <= reach and _span(board, u) <= CLEARANCE_ZOOM_MM)
        if close:
            return [u for _, u in close[:most]]
    return []


def _piece(issue: Issue, board: Board, config: Config) -> Highlight:
    """A stretch of trace (length_mm, centred on location): every segment of the route's nets on the issue's
    layer that reaches into it, the offending segment first; the planes beside it as context."""
    nets = _route_nets(board, config, issue.net)
    half = max(issue.length_mm / 2, 0.5)
    tracks = [t for t in board.tracks if t.net in nets and t.layer == issue.layer
              and _dist_track(t, issue.location) <= half + t.width / 2]
    focus = _first(issue.items, [t.uuid for t in tracks])
    what = {"gap_crossing": "over the gap", "off_plane": "without a plane under it",
            "edge_proximity": "along the plane edge", "reference_net": "over the other net's plane"}[issue.check]
    h = Highlight(focus=focus, text=f"{_n(len(tracks), 'track segment')} {what}")
    refs = _reference_layers(issue, board)
    if issue.check == "gap_crossing":
        refs = issue.details.get("reference_layers") or refs
        zones = _zones_near(board, refs[:1], issue.location, half + NEAR_MM)
        h.text += f"; the {_nets_of(zones)} plane on {refs[0]} beside it" if zones and refs else ""
    elif issue.check == "reference_net":
        plane_net, plane_layer = issue.details.get("plane_net", ""), issue.details.get("plane_layer", "")
        refs = [plane_layer] if plane_layer else refs
        zones = _zones_near(board, refs, issue.location, half + NEAR_MM, nets={plane_net} if plane_net else None)
        if plane_net and plane_layer:    # length_mm sums every piece over that net: take each segment over it
            over = _tracks_over(board, [t for t in board.tracks if t.net in nets and t.layer == issue.layer],
                                [z for z in board.zones if z.net == plane_net and not z.is_rule_area], plane_layer)
            if over:
                h.focus = _first(issue.items, [t.uuid for t in over])
                h.text = f"{_n(len(over), 'track segment')} {what}"
                zones = [z for z in board.zones if z.net == plane_net and not z.is_rule_area
                         and _tracks_over(board, over, [z], plane_layer)]
        h.text += f"; the {plane_net} plane on {plane_layer}" if zones else ""
    else:                                # off_plane at a run end, edge_proximity: the plane that stops near it
        zones = _zones_near(board, refs[:1], issue.location, half + NEAR_MM + issue.details.get("distance_mm", 0))
        h.text += f"; the {_nets_of(zones)} plane on {refs[0]} near it" if zones and refs else ""
    h.context = [z.uuid for z in zones]
    h.layer = refs[0] if refs else ""
    return h


def _run_on_layer(issue: Issue, board: Board, config: Config) -> Highlight:
    """A whole run without a reference: every segment of the route's nets on that layer."""
    nets = _route_nets(board, config, issue.net)
    tracks = [t.uuid for t in board.tracks if t.net in nets and t.layer == issue.layer]
    return Highlight(focus=_first(issue.items, tracks), layer=issue.layer,
                     text=f"{_n(len(tracks), 'track segment')} of {issue.net} on {issue.layer}")


def _layer_change(issue: Issue, board: Board, config: Config) -> Highlight:
    """The signal via (listed once per layer pair it crosses), with the nearest stitching via of the plane net, or
    the nearest bridging capacitor: in the zoom when close, otherwise only selected."""
    h = Highlight(focus=_valid(issue.items), text="the signal via")
    limit = 2 * (config.rules.stitch_distance_mm if issue.check == "layer_change_stitch"
                 else config.rules.decap_distance_mm)
    if issue.check == "layer_change_stitch":
        a, b, net = (issue.details.get(k, "") for k in ("from_plane", "to_plane", "plane_net"))
        near = [(math.dist(v.position, issue.location), v.uuid, "stitching via") for v in board.vias
                if v.net == net and a in v.layers and b in v.layers]
        near += [(math.dist(p.position, issue.location), p.uuid, "stitching pad") for p in board.pads()
                 if p.net == net and p.pad_type == "thru_hole" and a in p.layers and b in p.layers]
    else:
        nets = set(issue.details.get("nets", []))
        near = [(math.dist(fp.position, issue.location), fp.uuid, f"capacitor {fp.reference}")
                for fp in board.footprints if len(nets) == 2 and nets <= fp.nets() and _bridges(config, fp)]
    if near:
        d, uuid, what = min(near)
        (h.focus if d <= limit else h.context).append(uuid)
        h.text += f" and the nearest {what} ({d:.1f} mm away)"
    return h


def _missing_route(issue: Issue, board: Board, config: Config) -> Highlight:
    """No copper path between two pins: both pads and all copper already on the net, so the gap shows."""
    pins = issue.key.split(":", 1)[1].split("->") if issue.key.startswith("route:") else []
    pads = [p for p in (board.find_pin(pin) for pin in pins) if p is not None]
    nets = _route_nets(board, config, issue.net)
    copper = _copper_of(board, nets)
    return Highlight(focus=[p.uuid for p in pads] + copper,
                     text=f"pins {' and '.join(p.pin for p in pads) or '?'} and the copper of {issue.net}")


def _route_issue(issue: Issue, board: Board, config: Config, routes: Sequence) -> Highlight:
    """A simulated route: all its copper and both pins; plus the via or part carrying most of a detour, the pins
    without a return, or the return pads on the two sides of a split plane."""
    kind, _, rest = issue.key.partition(":")
    pins = [p for p in re.split(r"->|:", rest) if p] if kind in ("detour", "emi", "resonance", "split") else []
    rr = _route_result(routes, issue.net, pins)
    if rr is not None and not pins:
        pins = [rr.driver, rr.receiver]
    nets = _route_nets(board, config, issue.net)
    focus = [p.uuid for p in (board.find_pin(pin) for pin in pins) if p is not None] + _copper_of(board, nets)
    text = f"the route of {issue.net}"
    if kind in ("noret", "inject") and rest:
        pad = board.find_pin(rest)
        if pad is not None:
            focus.insert(0, pad.uuid)
            text += f" and pin {pad.pin}" + (" (no return reachable)" if kind == "noret" else " (return pin)")
    if kind == "split" and rr is not None:
        for pin in rr.return_pins:
            pad = board.find_pin(pin) if pin else None
            if pad is not None:
                focus.append(pad.uuid)
        text += " and its return pins on the two sides of the split"
    if issue.check == "return_detour" and rr is not None:
        # the transfer the issue's message names: the largest that is a via, pad or part (as simulate picks it)
        top = next((t for t in rr.transfers if t.get("kind") != "capacitance"), None)
        item = _transfer_item(board, top) if top else ""
        if item:
            focus.append(item)
            text += f" and {top['name']} (carries {top.get('fraction', 0) * 100:.0f} % of the return)"
    pair = issue.details.get("pair")
    layer = pair[0] if pair else (issue.layer if issue.check == "return_detour" else "")
    return Highlight(focus=focus, layer=layer or "", text=text)


def _power_stage(issue: Issue, board: Board, config: Config, routes: Sequence) -> Highlight:
    """A power stage (issue.net is its name): its switches and the capacitors closing the hot loop, the switch
    node's copper (the only net highlighted), the input connector for conducted emission, the copper at a
    current-density hotspot."""
    spec = next(c for c in config.stages() if c.name == issue.net)
    fps = [board.footprint(r) for r in spec.parts]
    focus = [fp.uuid for fp in fps if fp is not None]
    found = [fp.reference for fp in fps if fp is not None]
    text = f"power stage {spec.name}: " + (f"{', '.join(found)}" if found else "its parts are not on the board")
    res = next((r for r in routes if r.kind == "converter" and r.net == spec.name
                and not getattr(r, "view_of", None)), None)
    caps = [c["name"] for c in (res.caps if res is not None else []) if not c.get("lisn")][:2]
    if ":dc:" in issue.key:              # current density: the copper at the hotspot
        hot = _copper_at(board, issue.layer, issue.location)
        return Highlight(focus=hot["small"] or hot["zones"], context=hot["zones"] if hot["small"] else [],
                         text=f"the copper at the current-density hotspot of {spec.name} "
                              f"({issue.key.split(':dc:', 1)[1]}) on {issue.layer}", layer=issue.layer)
    if issue.key.endswith(":conducted"):
        conn = {p.split(".")[0] for p in spec.input_connector}
        focus += [board.footprint(r).uuid for r in sorted(conn) if board.footprint(r) is not None]
        text += f", the input connector {', '.join(sorted(conn))}" if conn else ""
        return Highlight(focus=focus, nets=False, text=text)
    for name in caps:
        fp = board.footprint(name)
        if fp is not None:
            focus.append(fp.uuid)
    if caps:
        text += f", capacitors {', '.join(caps)}"
    if spec.switch_node:
        sw = _copper_of(board, {spec.switch_node}, zones=True)
        focus += sw
        text += " and the switch node" if sw else ""
    return Highlight(focus=focus, nets=bool(spec.switch_node), text=text)


# ------------------------------------------------------------------------------------------------ helpers
def _lit_nets(issue: Issue, board: Board, config: Config) -> set[str]:
    """The nets an issue is about: a signal route's nets, a power stage's switch node, or the nets of the copper at
    a current-density hotspot."""
    spec = next((c for c in config.stages() if c.name == issue.net), None)
    if spec is None:
        return _route_nets(board, config, issue.net)
    if ":dc:" in issue.key:
        return {n for n in (_net_at(board, issue.layer, issue.location),) if n}
    return {spec.switch_node} if spec.switch_node else set()


def _net_at(board: Board, layer: str, at) -> str:
    """The net of the copper at a point on a layer (its small copper first, else the pour), "" for none."""
    hot = _copper_at(board, layer, at)
    owner = {v.uuid: v.net for v in board.vias} | {t.uuid: t.net for t in board.tracks} | \
        {p.uuid: p.net for p in board.pads()} | {z.uuid: z.net for z in board.zones}
    return next((owner[u] for u in hot["small"] + hot["zones"] if u in owner), "")


def _valid(uuids: Iterable[str]) -> list[str]:
    """Once each, without empty ids (pad joins) and the what-if copies' made-up ids."""
    return [u for u in dict.fromkeys(uuids) if u and not u.startswith("whatif-")]


def _first(items: list[str], found: list[str]) -> list[str]:
    """The issue's own item first (it is the offending segment), then what was found around it."""
    own = [u for u in items[:1] if u]
    return own + [u for u in found if u not in own]


def _n(k: int, what: str) -> str:
    """A count with its noun, plural when not 1 ("3 vias", "1 via")."""
    return f"{k} {what}" + ("" if k == 1 else "s")


def _route_nets(board: Board, config: Config, net: str) -> set[str]:
    """The nets the net's routes pass through (a series resistor's far side), as the checks found them; not every
    net reachable through a part (signal_nets also reaches a pull-up's or termination's rail)."""
    from .route import RouteError, find_route
    spec = next((n for n in config.nets if n.name == net), None)
    out = {net}
    for rx in (spec.receivers if spec is not None else []):
        try:
            out |= find_route(board, config, net, spec.driver, rx, spec.through).nets
        except (RouteError, KeyError, ValueError):
            pass                         # no path (route_missing): the net's own copper
    return out - set(config.reference_nets) | {net}


def _reference_layers(issue: Issue, board: Board) -> list[str]:
    """The reference plane layer(s) of the issue's layer ([] without a layer, or for an unknown one)."""
    from ..geometry.copper import reference_layers
    try:
        return [name for name, _ in reference_layers(board, issue.layer)] if issue.layer else []
    except Exception:
        return []


def _copper_of(board: Board, nets: set[str], zones: bool = False) -> list[str]:
    """The uuids of the nets' tracks and vias (and zones, with zones=True)."""
    out = [t.uuid for t in board.tracks if t.net in nets] + [v.uuid for v in board.vias if v.net in nets]
    if zones:
        out += [z.uuid for z in board.zones if z.net in nets and not z.is_rule_area]
    return out


def _route_result(routes: Sequence, net: str, pins: list[str]):
    """The window's route result for this net (and these pins, when the issue names them), or None."""
    for r in routes:
        if r.kind != "converter" and not getattr(r, "view_of", None) and r.net == net \
                and (not pins or {r.driver, r.receiver} >= set(pins[:2]) or r.receiver in pins):
            return r
    return None


def _bridges(config: Config, fp) -> bool:
    """Whether a part can carry a return between two planes: a capacitor or a short (unknown parts by their
    reference: C...)."""
    model = config.model_for(fp.reference)
    if model is not None:
        return model.kind in ("cap", "short")
    return fp.reference.upper().startswith("C")


def _seg_dist(p, a, b) -> float:
    """Distance from point p to the segment a-b (to the projection, clamped to the ends)."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def _dist_track(t: Track, p) -> float:
    """Distance from point p to a track's centreline (an arc approximated by its two chords through the middle)."""
    if t.mid is not None:                # arc: through its middle point
        return min(_seg_dist(p, t.start, t.mid), _seg_dist(p, t.mid, t.end))
    return _seg_dist(p, t.start, t.end)


def _shape(polygon):
    """A board polygon (outline, holes) as a shapely Polygon."""
    from shapely.geometry import Polygon as SPolygon
    outline, holes = polygon
    return SPolygon(outline, holes)


def _zones_near(board: Board, layers: list[str], at, radius: float, nets: Optional[set[str]] = None) -> list[Zone]:
    """Zones with fill on one of `layers` within `radius` of `at` (the planes on either side of a gap)."""
    from shapely.geometry import Point
    disk = Point(at).buffer(radius)
    out = []
    for z in board.zones:
        if z.is_rule_area or (nets is not None and z.net not in nets):
            continue
        for layer in layers:
            if any(_shape(poly).intersects(disk) for poly in z.filled.get(layer, [])):
                out.append(z)
                break
    return out


def _tracks_over(board: Board, tracks: list[Track], zones: list[Zone], layer: str) -> list[Track]:
    """The tracks whose centreline crosses the fill of `zones` on `layer`."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    fill = unary_union([_shape(poly) for z in zones for poly in z.filled.get(layer, [])])
    if fill.is_empty:
        return []
    return [t for t in tracks
            if LineString([t.start, t.mid, t.end] if t.mid is not None else [t.start, t.end]).intersects(fill)]


def _nets_of(zones: list[Zone]) -> str:
    """The zones' nets, each once, joined by "/" (for messages)."""
    return "/".join(dict.fromkeys(z.net for z in zones))


VIA_NAME = re.compile(r"^via (.+) @\((-?[\d.]+), (-?[\d.]+)\)$")


def _transfer_item(board: Board, t: dict) -> str:
    """The board item of a return transfer from the results (simulate's branch names): 'via NET @(x, y)', 'pad PIN',
    or a part's reference. Model-only branches (ideal stitches, what-if parts, the LISN) have none."""
    name = t.get("name", "")
    m = VIA_NAME.match(name)
    if m:
        at = (float(m.group(2)), float(m.group(3)))
        near = [v for v in board.vias if v.net == m.group(1) and math.dist(v.position, at) <= 0.05]
        return near[0].uuid if near else ""
    if name.startswith("pad "):
        pad = board.find_pin(name[4:])
        return pad.uuid if pad is not None else ""
    if t.get("kind") == "part":
        fp = board.footprint(name)
        return fp.uuid if fp is not None else ""
    return ""


def _copper_at(board: Board, layer: str, at, radius: float = 0.5) -> dict:
    """Small copper (vias, tracks, pads) at `at` to zoom to, and the zones containing it. Inside a pour only the
    pour's own nets count (a hotspot between another net's vias is about the pour, not those vias); with none of
    its small copper at the spot, the nearest one anchors the zoom, so a plane is never what the view fits."""
    from shapely.geometry import Point
    point = Point(at)
    inside = [z for z in board.zones if not z.is_rule_area
              and any(_shape(poly).buffer(radius).contains(point) for poly in z.filled.get(layer, []))]
    nets = {z.net for z in inside} or None
    small = [v.uuid for v in board.vias if math.dist(v.position, at) <= max(radius, v.size / 2)
             and (nets is None or v.net in nets)]
    small += [t.uuid for t in board.tracks if t.layer == layer and _dist_track(t, at) <= radius + t.width / 2
              and (nets is None or t.net in nets)]
    small += [p.uuid for p in board.pads() if layer in p.layers and math.dist(p.position, at) <= radius
              and (nets is None or p.net in nets)]
    if not small and nets:
        near = [(math.dist(v.position, at), v.uuid) for v in board.vias if v.net in nets]
        near += [(math.dist(p.position, at), p.uuid) for p in board.pads() if p.net in nets and layer in p.layers]
        near += [(_dist_track(t, at), t.uuid) for t in board.tracks if t.net in nets and t.layer == layer]
        small = [u for d, u in sorted(near)[:1]]
    return {"small": small, "zones": [z.uuid for z in inside]}


def _extent(z: Zone) -> float:
    """A zone's size: the larger side of its bounding box (mm), from its fill or else its outline. A zone wider
    than PLANE_MM is a plane (selected for context, never zoomed to). Each fill polygon's points as one array
    (numpy's min / max): refboard's fills are ~140 000 points, which Python's min and max over
    generators took 40-200 ms for on every highlight (a Converters-tab click), holding the GIL."""
    polys = [outline for polys in z.filled.values() for outline, _ in polys if len(outline)] or \
        ([z.outline[0]] if z.outline and len(z.outline[0]) else [])
    if not polys:
        return 0.0
    lo = np.min([np.min(np.asarray(o, float), axis=0) for o in polys], axis=0)
    hi = np.max([np.max(np.asarray(o, float), axis=0) for o in polys], axis=0)
    return float(max(hi - lo))


def _planes(board: Board) -> set[str]:
    """The uuids of the board's zones wider than PLANE_MM, worked out once per board as read: kept in the board's
    __dict__ (as Board's own part index), keyed by the zone list's identity and length, which a board edit
    changes."""
    key = (id(board.zones), len(board.zones))
    cached = board.__dict__.get("_plane_cache")
    if cached is None or cached[0] != key:
        cached = board.__dict__["_plane_cache"] = (key, {z.uuid for z in board.zones if _extent(z) > PLANE_MM})
    return cached[1]


def _bbox(board: Board, uuids: list[str]) -> Optional[tuple[float, float, float, float]]:
    """Bounding box (mm) of the items among the board model's tracks, vias, pads and footprints."""
    want = set(uuids)
    pts: list = []
    for t in board.tracks:
        if t.uuid in want:
            pts += [t.start, t.end]
    pts += [v.position for v in board.vias if v.uuid in want]
    for fp in board.footprints:
        if fp.uuid in want:
            pts += [p.position for p in fp.pads] or [fp.position]
        pts += [p.position for p in fp.pads if p.uuid in want]
    for z in board.zones:
        if z.uuid in want:
            for polys in z.filled.values():
                for outline, _ in polys:
                    pts += outline
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)
