"""Find the copper path of a signal from its driver pin to a receiver pin.

The path may run through tracks, arcs, vias, pads and series parts whose model passes a
signal (0R, termination resistors, ferrites). The result is an ordered list of horizontal
segments (per layer) and vertical via transitions, which the rule checks and the solver use.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations, pairwise
from typing import Optional

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from shapely import STRtree
from shapely.geometry import Point as SPoint

from ..config.settings import Config
from ..core.board import Board, Pad, Point, Track
from ..geometry.shapes import arc_points, clean_polygon, pad_shape, track_shape


class RouteError(ValueError):
    """No copper path between the pins (the message says what may block it): reported as route_missing."""
    pass


@dataclass
class RouteSegment:
    """A horizontal step of the route on one layer: along a track, across a pad, through a series part (pad to
    pad), or across a pour of the net (kind "zone", its width an effective one)."""
    start: Point
    end: Point
    layer: str
    width: float
    kind: str = "track"          # track | pad | part | zone (through a copper pour of the net)
    uuid: str = ""
    line: int = 0
    net: str = ""

    @property
    def length(self) -> float:
        """The step's straight length (mm)."""
        return math.dist(self.start, self.end)


@dataclass
class RouteVia:
    """A vertical step of the route: through a via or a through-hole pad, from one layer to another."""
    position: Point
    from_layer: str
    to_layer: str
    kind: str = "via"            # via | pad (through-hole pad)
    uuid: str = ""
    line: int = 0
    net: str = ""
    drill: float = 0.3


@dataclass
class Route:
    """A signal's copper path from its driver pad to a receiver pad, as steps in order, with the nets it passes
    and the series parts on it."""
    net: str
    driver: Pad
    receiver: Pad
    steps: list = field(default_factory=list)       # RouteSegment | RouteVia, in order
    nets: set[str] = field(default_factory=set)     # nets the route passes through
    parts: list[str] = field(default_factory=list)  # series parts on the route

    @property
    def segments(self) -> list[RouteSegment]:
        """The horizontal steps."""
        return [s for s in self.steps if isinstance(s, RouteSegment)]

    @property
    def vias(self) -> list[RouteVia]:
        """The vertical steps."""
        return [s for s in self.steps if isinstance(s, RouteVia)]

    @property
    def length(self) -> float:
        """The route's horizontal length (mm)."""
        return sum(s.length for s in self.segments)

    def layer_lengths(self) -> dict[str, float]:
        """mm of the route on each layer."""
        out: dict[str, float] = {}
        for s in self.segments:
            out[s.layer] = out.get(s.layer, 0.0) + s.length
        return out


def _passes(config: Config, fp, through) -> bool:
    """A 2-net part a signal path may run through: its model passes signals (0R, R <= 1 kΩ, ferrite, inductor),
    or the user named it in the net's `through` (an AC-coupling capacitor, a larger resistor)."""
    if fp.reference in through:
        return True
    model = config.model_for(fp.reference)
    return model is not None and model.passes_signal()


def _amp_pads(config: Config, fp):
    """(input pad, output pad) of a part modelled as an amplifier (an op-amp stage the path runs through), or
    None (no amplifier, or its pins can't be told: components.models.amplifier_pins)."""
    from ..components.models import amplifier_pins
    return amplifier_pins(fp, config.model_for(fp.reference))


def _blocking(board: Board, config: Config, nets: set[str], through) -> str:
    """The 2-net parts that lead out of the path's nets but don't pass a signal (a capacitor, a resistor > 1 kΩ, an
    open part), for the message when no path is found: the user may name them in the net's `through`."""
    refs = set(config.reference_nets)
    found = []
    for fp in board.footprints:
        fn = fp.nets()
        model = config.model_for(fp.reference)
        if model is not None and model.kind == "amplifier" and set(fn) & nets and _amp_pads(config, fp) is None:
            found.append(f"{fp.reference} (an amplifier whose input and output pins can't be told: set pins "
                         "[in, out] on the Parts tab)")
            continue
        if len(fn) != 2 or _passes(config, fp, through) or not (set(fn) & nets) or set(fn) <= nets or set(fn) & refs:
            continue
        model = config.model_for(fp.reference)
        found.append(f"{fp.reference} ({model.kind if model is not None else 'no model'})")
    if not found:
        return ""
    more = f" and {len(found) - 6} more" if len(found) > 6 else ""
    return (f"; parts leading on that don't pass a signal: {', '.join(found[:6])}{more}. If the path runs through "
            "one of them, name it in the net's `through` (Signal paths tab: Pass through)")


def signal_nets(board: Board, config: Config, net: str, through=()) -> set[str]:
    """The net plus every net reachable through series parts that pass signals (and the parts in `through`)."""
    refs = set(config.reference_nets)
    through = set(through)
    group = {net}
    frontier = [net]
    by_net = board.parts_by_net()
    while frontier:
        n = frontier.pop()
        for fp in by_net.get(n, []):
            amp = _amp_pads(config, fp)
            if amp is not None:                       # an op-amp stage: from its input's net to its output's
                if n in (amp[0].net, amp[1].net):
                    for other in (amp[0].net, amp[1].net):
                        if other not in group and other not in refs and other:
                            group.add(other)
                            frontier.append(other)
                continue
            if not _passes(config, fp, through) or len(fp.nets()) != 2:
                continue
            for other in fp.nets():
                if other not in group and other not in refs and other:
                    group.add(other)
                    frontier.append(other)
    return group


class _Graph:
    """The copper of the route's nets as a weighted graph: nodes are (layer, point) places, edges the copper
    joining them, weighted by length (mm). The shortest path from the driver to the receiver is the route
    (Dijkstra's algorithm [Dijkstra 1959])."""

    def __init__(self):
        """An empty graph."""
        self.keys: dict[tuple, int] = {}
        self.pos: list[tuple[Point, str]] = []
        self.edges: list[tuple[int, int, float, object]] = []

    def node(self, layer: str, p: Point) -> int:
        """The node at point p on `layer` (made when new). Points are rounded to 1 µm, so a track end and the
        via or pad it ends on become the same node."""
        k = (layer, round(p[0] * 1000), round(p[1] * 1000))
        i = self.keys.get(k)
        if i is None:
            i = self.keys[k] = len(self.pos)
            self.pos.append(((p[0], p[1]), layer))
        return i

    def edge(self, a: int, b: int, w: float, info):
        """Join nodes a and b with weight w (at least 1 nm: Dijkstra needs positive weights) and what the copper
        is (info, to turn the path back into steps)."""
        if a != b:
            self.edges.append((a, b, max(w, 1e-6), info))


def copper_graph(board: Board, nets: set) -> "_Graph":
    """The copper of these nets as a _Graph: nodes at (layer, point) places, edges for the tracks (arcs as chords),
    the vias and through-hole pads between layers, a track's end joined to the via, pad or track it touches
    (T-junctions), and every pair of pads, vias and track ends on the same filled pour (a "zone" edge with the
    pour's effective width). Shared by find_route (the signal path) and analysis/analog_net (the circuit around
    op-amps); the parts between nets are not in it."""
    g = _Graph()
    tracks = [t for t in board.tracks if t.net in nets]
    vias = [v for v in board.vias if v.net in nets]
    pads = [p for fp in board.footprints for p in fp.pads if p.net in nets]

    # Tracks: arcs are expanded into chords so the path follows the copper.
    track_ends: list[tuple[int, Point, str, Track]] = []
    for t in tracks:
        pts = arc_points(t.start, t.mid, t.end) if t.is_arc else [t.start, t.end]
        ids = [g.node(t.layer, p) for p in pts]
        for (a, pa), (b, pb) in pairwise(zip(ids, pts)):
            g.edge(a, b, math.dist(pa, pb), ("track", t))
        track_ends.append((ids[0], pts[0], t.layer, t))
        track_ends.append((ids[-1], pts[-1], t.layer, t))

    # Vias: one node per spanned layer, vertical edges between consecutive layers.
    zs = {l.name: l.z for l in board.stackup.copper}
    for v in vias:
        ids = [g.node(layer, v.position) for layer in v.layers]
        for (a, la), (b, lb) in pairwise(zip(ids, v.layers)):
            g.edge(a, b, abs(zs[la] - zs[lb]), ("via", v, la, lb))

    # Pads: node at the pad centre on each copper layer, vertical edges for through-hole pads.
    pad_shapes = {}
    for p in pads:
        ids = [g.node(layer, p.position) for layer in p.layers]
        pad_shapes[id(p)] = pad_shape(p)
        for (a, la), (b, lb) in pairwise(zip(ids, p.layers)):
            g.edge(a, b, abs(zs[la] - zs[lb]), ("tht", p, la, lb))

    # Track endpoints touching vias, pads or the middle of other tracks (T-junctions).
    by_layer: dict[str, list] = {}
    for v in vias:
        for layer in v.layers:
            by_layer.setdefault(layer, []).append(("via", v, SPoint(v.position).buffer(v.size / 2 + 1e-4)))
    for p in pads:
        for layer in p.layers:
            by_layer.setdefault(layer, []).append(("pad", p, pad_shapes[id(p)].buffer(1e-4)))
    for t in tracks:
        by_layer.setdefault(t.layer, []).append(("track", t, track_shape(t).buffer(1e-4)))
    # a spatial index per layer, so each track end finds what it touches without testing every item
    trees = {layer: (STRtree([it[2] for it in items]), items) for layer, items in by_layer.items()}
    for node_id, pt, layer, t in track_ends:
        if layer not in trees:
            continue
        tree, items = trees[layer]
        # the track's round end, not only its centre line's end point: copper that overlaps is connected (KiCad's
        # rule). refboard's R303: the 0.4 mm track ends 0.02 mm outside the rotated pad's edge, its end
        # cap well inside; the point alone left /CPU/ADC_I_RAIL_1 "unrouted". Copper of different nets never
        # overlaps on a board that passes DRC, so the wider test joins nothing that isn't joined.
        sp = SPoint(pt).buffer(max(t.width / 2, 1e-4), quad_segs=4)
        for idx in tree.query(sp, predicate="intersects"):
            kind, obj, _ = items[idx]
            if obj is t:
                continue
            if kind == "via":
                g.edge(node_id, g.node(layer, obj.position), math.dist(pt, obj.position), ("join", None))
            elif kind == "pad":
                g.edge(node_id, g.node(layer, obj.position), math.dist(pt, obj.position), ("pad", obj, layer))
            elif kind == "track" and not obj.is_arc:
                for end in (obj.start, obj.end):
                    g.edge(node_id, g.node(layer, end), math.dist(pt, end), ("track", obj))

    # Copper pours of the route's nets (power stages carry their current in pours, not tracks): every pad, via
    # and track end touching the same filled polygon is connected through it. The step becomes a "zone"
    # segment; the solver then treats the pour as copper with its own current distribution, not as a line.
    for z in board.zones:
        if z.net not in nets or z.is_rule_area:
            continue
        for layer, polys in z.filled.items():
            if layer not in trees:
                continue
            tree, items = trees[layer]
            for poly in (clean_polygon(pts) for pts in polys):
                if poly.is_empty:
                    continue
                ports, area = _pour_ports(g, tree, items, poly, z.net, layer), poly.area
                # every pair of ports on the pour is joined straight across it; the step's effective width is
                # the pour's area over that length (at most 10 mm), what the line model takes for it
                for (a, pa), (b, pb) in combinations(ports.items(), 2):
                    length = math.dist(pa, pb)
                    g.edge(a, b, length, ("zone", z, layer, min(area / max(length, 0.5), 10.0)))

    return g


def find_route(board: Board, config: Config, net: str, driver_pin: str, receiver_pin: str, through=()) -> Route:
    """The signal path from the driver pin to the receiver pin, through series parts into other nets (the parts
    in `through` too: emi/path.py models each part's impedance)."""
    driver = board.find_pin(driver_pin)
    receiver = board.find_pin(receiver_pin)
    if driver is None or receiver is None:
        raise RouteError(f"pin {driver_pin if driver is None else receiver_pin!r} not found")
    through = set(through)
    reach = signal_nets(board, config, net, through)
    nets = reach | ({driver.net, receiver.net} - set(config.reference_nets))

    g = copper_graph(board, nets)

    # Series parts that pass the signal (0R, termination R, ferrite): pad-to-pad edge.
    for fp in board.footprints:
        amp = _amp_pads(config, fp)
        if amp is not None:                           # an op-amp stage: its input pin to its output pin
            a, b = amp
            if a.net in nets and b.net in nets:
                g.edge(g.node(a.layers[0], a.position), g.node(b.layers[0], b.position),
                       math.dist(a.position, b.position), ("part", fp, a.layers[0]))
            continue
        if not _passes(config, fp, through):
            continue
        fp_pads = [p for p in fp.pads if p.net in nets]
        if len(fp_pads) == 2 and fp_pads[0].net != fp_pads[1].net:
            a, b = fp_pads
            g.edge(g.node(a.layers[0], a.position), g.node(b.layers[0], b.position),
                   math.dist(a.position, b.position), ("part", fp, a.layers[0]))

    src = g.node(driver.layers[0], driver.position)
    dst = g.node(receiver.layers[0], receiver.position)
    n = len(g.pos)
    if not g.edges:
        raise RouteError(f"net {net!r} has no copper connecting {driver_pin} and {receiver_pin}")
    # The graph as a symmetric sparse matrix (each edge both ways) for scipy's Dijkstra: the shortest copper
    # path is what a signal's current follows at low frequency only roughly, but it is the copper the signal
    # really runs on (a route has one path; branches and stubs are not part of the loop). Where two edges join
    # the same nodes the shorter one counts, and `info` keeps what it is.
    rows = [e[0] for e in g.edges] + [e[1] for e in g.edges]
    cols = [e[1] for e in g.edges] + [e[0] for e in g.edges]
    w = [e[2] for e in g.edges] * 2
    info = {}
    for a, b, wt, inf in g.edges:
        for key in ((a, b), (b, a)):
            if key not in info or info[key][0] > wt:
                info[key] = (wt, inf)
    mat = coo_matrix((w, (rows, cols)), shape=(n, n)).tocsr()
    dist, pred = dijkstra(mat, indices=src, return_predecessors=True)
    if not np.isfinite(dist[dst]):
        raise RouteError(f"no copper path from {driver_pin} to {receiver_pin} on net {net!r} "
                         "(unrouted, or a series part is modelled as open)" + _blocking(board, config, reach, through))

    order = [dst]                       # walk the predecessors back from the receiver
    while order[-1] != src:
        order.append(int(pred[order[-1]]))
    order.reverse()

    route = Route(net, driver, receiver)
    for a, b in zip(order, order[1:]):
        (pa, la), (pb, lb) = g.pos[a], g.pos[b]
        _, inf = info[(a, b)]
        kind = inf[0]
        if kind in ("via", "tht"):
            obj = inf[1]
            route.steps.append(RouteVia(pa, la, lb, "via" if kind == "via" else "pad",
                                        getattr(obj, "uuid", ""), getattr(obj, "line", 0), obj.net,
                                        getattr(obj, "drill", 0.3)))
            route.nets.add(obj.net)
        elif kind == "track":
            t = inf[1]
            route.steps.append(RouteSegment(pa, pb, la, t.width, "track", t.uuid, t.line, t.net))
            route.nets.add(t.net)
        elif kind == "zone":
            z, _, width = inf[1], inf[2], inf[3]
            route.steps.append(RouteSegment(pa, pb, la, width, "zone", z.uuid, z.line, z.net))
            route.nets.add(z.net)
        elif kind == "part":
            fp = inf[1]
            route.parts.append(fp.reference)
            route.steps.append(RouteSegment(pa, pb, la, 0.3, "part", fp.uuid, fp.line, ""))
        else:  # pad / join connectors inside copper
            route.steps.append(RouteSegment(pa, pb, la, 0.2, "pad", "", 0, ""))
    route.steps = _merge_collinear(route.steps)
    return route


SHUNT_KINDS = ("resistor", "cap", "inductor", "ferrite", "short")     # part models that can sit across a path


def shunt_parts(board: Board, config: Config, route: Route, returns: set[str]) -> list[tuple]:
    """Parts from the signal path to a return: a 2-net passive part (resistor, capacitor, inductor, ferrite, 0R)
    with one pin on a net of the path and the other on one of `returns` (reference nets, and for the solver poured
    nets too), not one of the path's series parts. [(footprint, model, path pad, return pad)], in board order."""
    from ..components.models import model_pads
    path_nets = set(route.nets) | {route.driver.net, route.receiver.net}
    series = set(route.parts)
    out = []
    for fp in board.footprints:
        if fp.reference in series or fp.dnp:
            continue
        m = config.model_for(fp.reference)
        if m is None or m.kind not in SHUNT_KINDS:
            continue
        pads = model_pads(fp, m)
        if len({p.net for p in pads if p.net}) != 2:
            continue
        on = [p for p in pads if p.net in path_nets]
        off = [p for p in pads if p.net and p.net not in path_nets]
        if on and off and off[0].net in returns:
            out.append((fp, m, on[0], off[0]))
    return out


def _part_text(board: Board, config: Config, ref: str) -> str:
    """A part on a path as the Signal paths tab shows it: the reference with its value, or an op-amp's gain."""
    m = config.model_for(ref)
    if m is not None and m.kind == "amplifier":
        g = m.params.get("gain")
        return f"{ref} ×{1.0 if g is None else g:g}"
    entry = config.components.get(ref)
    fp = board.footprint(ref)
    value = entry.value if entry is not None else (fp.value if fp is not None else "")
    return f"{ref} {value}".strip()


@dataclass
class PathChain:
    """What the Signal paths tab shows for a path: its driver pin, the series parts and op-amps in route order (as
    text), its receivers, and the parts across it to a reference net (shunts: loads, filter capacitors). `error`:
    why no route was found (then the chain is the driver and receivers only)."""
    driver: str
    parts: list[str]
    receivers: list[str]
    shunts: list[str] = field(default_factory=list)
    # every 2-pin part hanging off the path, by the path's net it sits on (path order): [(net, ["R307 10k → GND",
    # "✗ C309 100n → GND (DNP)"])]: capacitors, pull-ups / -downs, to any other net; DNP ones marked (path_branches)
    branches: list = field(default_factory=list)
    error: str = ""

    def text(self) -> str:
        """`U302.3 → U301 ×1 → R303 4k7 → U1102.9` (several receivers joined by commas)."""
        return " → ".join([self.driver, *self.parts, ", ".join(self.receivers)])


def path_chain(board: Board, config: Config, spec) -> PathChain:
    """The path of a NetSpec as find_route follows it (to its first receiver; the receivers share the path up to
    their net), with the shunt parts to reference nets along it. Board-only (no solver model): a few milliseconds
    per path; the tab computes it off the GUI thread."""
    chain = PathChain(spec.driver, [], list(spec.receivers))
    if not spec.receivers:
        chain.error = "no receiver"
        return chain
    try:
        route = find_route(board, config, spec.name, spec.driver, spec.receivers[0], spec.through)
    except RouteError as e:
        chain.error = str(e).split(" (")[0]                     # the first clause: what is missing
        return chain
    chain.parts = [_part_text(board, config, r) for r in route.parts]
    chain.shunts = [f"{_part_text(board, config, fp.reference)} to {ret.net}"
                    for fp, _, _, ret in shunt_parts(board, config, route, set(config.reference_nets))]
    chain.branches = path_branches(board, config, route)
    return chain


DNP_MARK = "✗"


def path_branches(board: Board, config: Config, route: Route) -> list:
    """What hangs off a path, for the Signal paths tab (the user: "add branches to let's say GND or other nets to also
    show capacitors, pullups/downs. mark the ones ... that are DNP"): per net of the path, in path order, every 2-pin
    part with one pin on it and the other on another net, not one of the path's series parts, DNP ones included and
    marked. Display only: the solver's shunts (shunt_parts) leave DNP parts out and take reference nets only."""
    order = list(dict.fromkeys(n for n in [route.driver.net, *route.nets, route.receiver.net] if n))
    series = set(route.parts)
    found: dict[str, list[str]] = {n: [] for n in order}
    for fp in board.footprints:
        if fp.reference in series:
            continue
        nets = [p.net for p in fp.pads if p.net]
        if len(fp.pads) != 2 or len(set(nets)) != 2:
            continue
        on = [n for n in nets if n in found]
        if len(on) != 1:                    # both pins on the path: a series part of another branch, not a shunt
            continue
        other = next(n for n in nets if n != on[0])
        text = f"{_part_text(board, config, fp.reference)} → {other}"
        found[on[0]].append(f"{DNP_MARK} {text} (DNP)" if fp.dnp else text)
    return [(n, sorted(found[n], key=lambda s: s.lstrip(DNP_MARK + " "))) for n in order if found[n]]


def _pour_ports(g: _Graph, tree, items, poly, net: str, layer: str) -> dict[int, Point]:
    """The graph nodes a filled polygon joins: the vias and pads on it, and the ends of its net's tracks inside it."""
    ports: dict[int, Point] = {}
    for kind, obj, _ in (items[idx] for idx in tree.query(poly, predicate="intersects")):
        if kind in ("via", "pad"):
            ports[g.node(layer, obj.position)] = obj.position
        elif obj.net == net:
            reach = poly.buffer(obj.width / 2 + 1e-3)
            for end in (obj.start, obj.end):
                if reach.contains(SPoint(end)):
                    ports[g.node(layer, end)] = end
    return ports


def _merge_collinear(steps: list) -> list:
    """Join consecutive chords of the same track so arcs are not hundreds of steps."""
    out: list = []
    for s in steps:
        prev = out[-1] if out else None
        if (isinstance(s, RouteSegment) and isinstance(prev, RouteSegment) and prev.uuid and prev.uuid == s.uuid
                and prev.layer == s.layer and prev.end == s.start):
            # keep chords of arcs separate only if they bend noticeably
            v1 = (prev.end[0] - prev.start[0], prev.end[1] - prev.start[1])
            v2 = (s.end[0] - s.start[0], s.end[1] - s.start[1])
            cross = abs(v1[0] * v2[1] - v1[1] * v2[0])
            if cross < 1e-9 * max(1e-12, math.hypot(*v1) * math.hypot(*v2)) + 1e-12:
                out[-1] = RouteSegment(prev.start, s.end, prev.layer, prev.width, prev.kind, prev.uuid, prev.line,
                                       prev.net)
                continue
        out.append(s)
    return out


def return_pad(board: Board, config: Config, pin: Pad, explicit: Optional[str] = None) -> Optional[Pad]:
    """The reference-net pad that carries the return current for a signal pin.

    Explicit settings choice wins; otherwise the nearest reference-net pad of the same part,
    otherwise the nearest reference-net pad of any part within 10 mm.
    """
    if explicit:
        return board.find_pin(explicit)
    refs = set(config.reference_nets)
    fp = board.footprint(pin.footprint)
    own = [p for p in (fp.pads if fp else []) if p.net in refs]
    if own:
        return min(own, key=lambda p: math.dist(p.position, pin.position))
    others = [p for p in board.pads() if p.net in refs and math.dist(p.position, pin.position) < 10.0]
    if others:
        return min(others, key=lambda p: math.dist(p.position, pin.position))
    return None
