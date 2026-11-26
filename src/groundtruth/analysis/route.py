"""Find the copper path of a signal from its driver pin to a receiver pin.

The path may run through tracks, arcs, vias, pads and series parts whose model passes a
signal (0R, termination resistors, ferrites). The result is an ordered list of horizontal
segments (per layer) and vertical via transitions, which the rule checks and the solver use.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
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


PART_STEP = 1000.0             # mm: a step through a part weighs this much more than its pads' distance (find_route)


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
    # each series part's pads as the route passes it, (in, out), parallel to `parts` (an op-amp: its input and
    # output pad, so the channel is known); empty for a route made elsewhere
    part_pads: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what modelling it had to simplify (simulate._chain), for the
                                                    # route's result

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


def _amp_units(config: Config, fp) -> list:
    """The channels of a part modelled as an amplifier (components.models.amplifier_units), [] for none or when its
    pins can't be told."""
    from ..components.models import amplifier_units
    return amplifier_units(fp, config.model_for(fp.reference))


def _blocking(board: Board, config: Config, nets: set[str], through) -> str:
    """The 2-net parts that lead out of the path's nets but don't pass a signal (a capacitor, a resistor > 1 kΩ, an
    open part), for the message when no path is found: the user may name them in the net's `through`."""
    refs = set(config.reference_nets)
    found = []
    for fp in board.footprints:
        fn = fp.nets()
        model = config.model_for(fp.reference)
        if model is not None and model.kind == "amplifier" and set(fn) & nets and not _amp_units(config, fp):
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


def signal_graph(board: Board, config: Config, through=()) -> tuple[dict, dict]:
    """The ways a signal goes from net to net, as (forward, backward) {net: {next net: [(part, input pad, output
    pad)]}}: through every fitted 2-net part that passes signals (_passes: its model, or named in `through`) both
    ways, and through every op-amp channel (components.models.amplifier_units: from each input, IN+ or IN−, to its
    output) and logic-buffer channel (buffer_pins) from its input to its output only [A81]. Parts not fitted (DNP)
    pass nothing. `backward` is `forward` reversed."""
    through = set(through)
    fwd: dict[str, dict] = {}
    back: dict[str, dict] = {}

    def way(a: Pad, b: Pad, fp):
        """A step from a's net to b's net through part fp."""
        if a.net and b.net and a.net != b.net:
            fwd.setdefault(a.net, {}).setdefault(b.net, []).append((fp, a, b))
            back.setdefault(b.net, {}).setdefault(a.net, []).append((fp, b, a))
    from ..components.models import amplifier_units, buffer_pins
    for fp in board.footprints:
        if fp.dnp:
            continue
        m = config.model_for(fp.reference)
        if m is not None and m.kind == "amplifier":
            for _, ins, out in amplifier_units(fp, m):
                for p in ins:
                    way(p, out, fp)
            continue
        if m is not None and m.kind == "buffer":
            for a, b in buffer_pins(fp, m):
                way(a, b, fp)
            continue
        pads = [p for p in fp.pads if p.net]
        if not _passes(config, fp, through):
            continue
        if len({p.net for p in pads}) != 2:
            if m is not None and m.kind in ("resistor", "cap") and not m.pins:
                from .analog_net import array_pairs
                for a, b in array_pairs(fp):      # a resistor or capacitor array's elements [A83]
                    way(a, b, fp)
                    way(b, a, fp)
            continue
        a = pads[0]
        b = next(p for p in pads if p.net != a.net)
        way(a, b, fp)
        way(b, a, fp)
    return fwd, back


def _reach(graph: dict, start: str, barriers: set) -> set[str]:
    """The nets reachable from `start` along `graph` (signal_graph's), never entering a barrier."""
    seen, todo = {start}, [start]
    while todo:
        for nxt in graph.get(todo.pop(), {}):
            if nxt not in seen and nxt not in barriers:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def path_nets(board: Board, config: Config, driver_net: str, receiver_net: str, through=(), barriers=None,
              graph=None) -> set[str]:
    """The nets a signal from the driver's net can pass on its way to the receiver's net: those reachable forward
    from the driver and backward from the receiver (signal_graph; op-amps and buffers only from input to output), so
    a bias or reference follower beside the path, a monitor buffer listening to it, or an op-amp crossed from its
    output to its input is not on it (audit 2026-11, G-1/G-2/G-3/E-6) [A81]. Never entering a barrier net
    (default: the reference nets), the two end nets excepted. Empty when the receiver is not reached."""
    fwd, back = graph if graph is not None else signal_graph(board, config, through)
    stop = (set(config.reference_nets) if barriers is None else set(barriers)) - {driver_net, receiver_net}
    f = _reach(fwd, driver_net, stop)
    if receiver_net not in f:
        return set()
    return f & _reach(back, receiver_net, stop)


def route_nets(board: Board, config: Config, driver_net: str, receiver_net: str, through=()) -> tuple[set, set]:
    """(the path's nets, the supply-like nets it runs through): path_nets never entering AC ground
    (analog_net.ac_ground_nets: a part passing signals into a supply would lead on into the regulator behind it),
    else, when the receiver is reached only through such a net, never entering a hard supply (a reference net, a
    regulator's output, a pin named as a supply: analog_net.supply_pin_nets hard), and those nets named: a path
    through a net named like a rail or with an IC's power_in pin on it (a battery divider VBAT_DIV) is a signal
    there, not AC ground (audit 2026-11, E-1: such a net shorted the signal, −6000 dB)."""
    from .analog_net import ac_ground_nets, supply_pin_nets
    graph = signal_graph(board, config, through)
    soft = ac_ground_nets(board, config)
    nets = path_nets(board, config, driver_net, receiver_net, through, soft, graph)
    if nets:
        return nets, set()
    hard = set(config.reference_nets) | supply_pin_nets(board, hard=True)
    nets = path_nets(board, config, driver_net, receiver_net, through, hard, graph)
    return nets, (nets & soft) - {driver_net, receiver_net}


def signal_nets(board: Board, config: Config, net: str, through=(), stop=None) -> set[str]:
    """The net plus every net reachable through series parts that pass signals (and the parts in `through`) and
    through op-amp and buffer channels (every channel of a part: a quad's cascade, audit 2026-11 G-1), either way
    (the path's nets from any of them: the Signal paths tab, the highlight). `stop`: nets never entered nor crossed;
    default AC ground (analog_net.ac_ground_nets: a resistor into a supply no longer leads on through it, G-5)."""
    if stop is None:
        from .analog_net import ac_ground_nets
        stop = ac_ground_nets(board, config) - {net}
    fwd, back = signal_graph(board, config, through)
    both = {n: set(fwd.get(n, ())) | set(back.get(n, ())) for n in set(fwd) | set(back)}
    return _reach(both, net, set(config.reference_nets) | set(stop))


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
        # rule). refboard's R41: the 0.4 mm track ends 0.02 mm outside the rotated pad's edge, its end
        # cap well inside; the point alone left /MCU/ADC_SENSE_1 "unrouted". Copper of different nets never
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

    # A via or pad whose centre lies on the middle of a track (not at one of its ends) is joined to it too: KiCad
    # connects two items when one's anchor (a via's or pad's centre, a track's ends) lies inside the other's copper.
    # A board's via 34 µm beside a track's centre line, in its middle, cut a path off as "unrouted" (2026-11-26); the
    # via joins the track's two ends, each at its distance along the copper.
    for obj in [*vias, *pads]:
        for layer in obj.layers:
            if layer not in trees:
                continue
            tree, items = trees[layer]
            here = SPoint(obj.position)
            for idx in tree.query(here, predicate="intersects"):
                k2, t, _ = items[idx]
                if k2 != "track" or not track_shape(t).contains(here):
                    continue
                if any(math.dist(obj.position, e) < 1e-3 for e in (t.start, t.end)):
                    continue                # at the track's end: joined above (the same node or the end's test)
                node = g.node(layer, obj.position)
                for end in (t.start, t.end):        # the copper between them is this track's (its width, its uuid)
                    g.edge(node, g.node(layer, end), math.dist(obj.position, end), ("track", t))

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
    ends = {driver.net, receiver.net} - set(config.reference_nets)
    # the path's nets: forward from the driver and backward from the receiver, op-amps and buffers only from input
    # to output (route_nets: the receiver reached only against a channel's direction, or across a supply, is no path)
    on_path, crossed = route_nets(board, config, driver.net, receiver.net, through)
    nets = on_path | ends

    g = copper_graph(board, nets)
    # Parts between the path's nets: a pad-to-pad edge, both ways for a part that passes the signal (0R, termination
    # R, ferrite), one way (input pin to output pin) for an op-amp's or buffer's channel. Each part step weighs
    # PART_STEP more than its pads' distance, so the route never leaves its net through two resistors and comes back
    # where the net's own track is longer (audit 2026-11, G-11).
    fwd, _ = signal_graph(board, config, through)
    oneway = []
    for a_net, outs in fwd.items():
        if a_net not in nets:
            continue
        for b_net, steps in outs.items():
            if b_net not in nets:
                continue
            for fp, a, b in steps:
                edge = (g.node(a.layers[0], a.position), g.node(b.layers[0], b.position),
                        math.dist(a.position, b.position) + PART_STEP, ("part", fp, a.layers[0], a, b))
                oneway.append(edge)       # a passing part is in fwd both ways: both its directions are here

    src = g.node(driver.layers[0], driver.position)
    dst = g.node(receiver.layers[0], receiver.position)
    n = len(g.pos)
    if not g.edges and not oneway:
        raise RouteError(f"net {net!r} has no copper connecting {driver_pin} and {receiver_pin}")
    # The graph as a sparse matrix for scipy's Dijkstra [Dijkstra 1959], copper both ways, part steps as given: the
    # shortest copper path is what a signal's current follows at low frequency only roughly, but it is the copper
    # the signal really runs on (a route has one path; branches and stubs are not part of the loop). Where two edges
    # join the same nodes the shorter one counts, and `info` keeps what it is.
    edges = [(a, b, w, inf) for a, b, w, inf in g.edges] + [(b, a, w, inf) for a, b, w, inf in g.edges] + oneway
    edges = [e for e in edges if e[0] != e[1]]
    info = {}
    for a, b, wt, inf in edges:
        if (a, b) not in info or info[(a, b)][0] > wt:
            info[(a, b)] = (wt, inf)
    rows = [k[0] for k in info]
    cols = [k[1] for k in info]
    w = [v[0] for v in info.values()]
    mat = coo_matrix((w, (rows, cols)), shape=(n, n)).tocsr()
    dist, pred = dijkstra(mat, directed=True, indices=src, return_predecessors=True)
    if not np.isfinite(dist[dst]):
        reach = signal_nets(board, config, net, through)
        why = ""
        if not on_path:
            graph = signal_graph(board, config, through)
            if path_nets(board, config, driver.net, receiver.net, through, graph=graph):
                from .analog_net import supply_pin_nets
                rails = sorted(path_nets(board, config, driver.net, receiver.net, through, graph=graph)
                               & (supply_pin_nets(board, hard=True) - {driver.net, receiver.net}))
                why = (f"; the signal reaches {receiver_pin} only through {', '.join(rails) or 'a supply'} (a "
                       "regulator's output or a supply pin: AC ground for a signal)")
            elif path_nets(board, config, receiver.net, driver.net, through, graph=graph):
                why = (f"; {receiver_pin} reaches {driver_pin} only from an op-amp's or buffer's output to its input "
                       "(the signal goes the other way: swap the driver and the receiver)")
        raise RouteError(f"no copper path from {driver_pin} to {receiver_pin} on net {net!r} "
                         "(unrouted, or a series part is modelled as open)" + why
                         + _blocking(board, config, reach, through))

    order = [dst]                       # walk the predecessors back from the receiver
    while order[-1] != src:
        order.append(int(pred[order[-1]]))
    order.reverse()

    route = Route(net, driver, receiver)
    if crossed:
        route.notes.append(_crossed_note(crossed))
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
            route.part_pads.append((inf[3], inf[4]))
            route.steps.append(RouteSegment(pa, pb, la, 0.3, "part", fp.uuid, fp.line, ""))
        else:  # pad / join connectors inside copper
            route.steps.append(RouteSegment(pa, pb, la, 0.2, "pad", "", 0, ""))
    route.steps = _merge_collinear(route.steps)
    return route


def _crossed_note(crossed: set) -> str:
    """The note for supply-like nets a path runs through (route_nets): taken as the path's own nets."""
    return (f"the path runs through {', '.join(sorted(crossed))}, which look{'s' if len(crossed) == 1 else ''} like a "
            "supply (a rail's name, or an IC's power_in pin): taken as a signal net "
            "there, not AC ground")


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
        # a Gain field: a fixed gain block; without one the circuit around it sets its gain (analog_net)
        return f"{ref} (op-amp)" if g is None else f"{ref} ×{g:g}"
    if m is not None and m.kind == "buffer":
        return f"{ref} (buffer)"
    entry = config.components.get(ref)
    fp = board.footprint(ref)
    value = entry.value if entry is not None else (fp.value if fp is not None else "")
    return f"{ref} {value}".strip()


@dataclass
class ChainPart:
    """A part on or beside a path, for the path's schematic (report/schematics.draw_path). `kind`: resistor, cap,
    inductor, ferrite, short, opamp, buffer or part (anything else: a box); `name` its reference, or an op-amp's
    channel (U13B); `value` what is written under it ("4.7k", "op-amp", "×2"). A branch part also has the net it sits
    on (`net`), the net at its other side (`to_net`), whether that net is on the path too (`on_path`: drawn as a
    bridge over the path) and whether it is fitted (`dnp`)."""
    ref: str
    name: str
    value: str
    kind: str
    net: str = ""
    to_net: str = ""
    on_path: bool = False
    dnp: bool = False
    setting: bool = False      # not a board part: the path's settings add it (its source R, its receivers' load)
    # an op-amp channel on the path: its input the route does not enter (`other_in`, its net; `inverting`: the route
    # enters at −), whether that input is its own output (`follower`), the parts from it to the output (`feedback`)
    # and the other parts on it (`in_parts`, each with its to_net): a difference amplifier's R1 and R3
    other_in: str = ""
    inverting: bool = False
    follower: bool = False
    feedback: list = field(default_factory=list)
    in_parts: list = field(default_factory=list)


@dataclass
class ChainLeg:
    """The route to one more receiver of a path (the first is the chain itself): its nets in order (one more than
    its series parts), its series parts, its branch parts by net (path_branch_parts) and the settings' load at its
    receiver."""
    receiver: str
    nets: list
    series: list
    branch_parts: list
    load: list = field(default_factory=list)


@dataclass
class PathChain:
    """What the Signal paths tab shows for a path: its driver pin, the series parts and op-amps in route order (as
    text), its receivers, and the parts across it to a reference net (shunts: loads, filter capacitors). `error`:
    why no route was found (then the chain is the driver and receivers only)."""
    driver: str
    parts: list[str]
    receivers: list[str]
    shunts: list[str] = field(default_factory=list)
    # every 2-pin part hanging off the path, by the path's net it sits on (path order): [(net, ["R45 10k → GND",
    # "✗ C42 100n (DNP) → GND"])]: capacitors, pull-ups / -downs, to any other net; DNP ones marked (path_branches)
    branches: list = field(default_factory=list)
    notes: list = field(default_factory=list)       # what the path's circuit leaves out (an op-amp without a model)
    error: str = ""
    # the same as a circuit, for the path's schematic (the user, 2026-11-26: "instead of writing it, can you provide a
    # graph 'schematic' of the path?"): the path's nets in order (one more than its series parts: nets[i] before
    # series[i], nets[i + 1] after it), the series parts, and the branch parts by net (path_branch_parts)
    nets: list = field(default_factory=list)
    series: list = field(default_factory=list)
    branch_parts: list = field(default_factory=list)
    ground_nets: set = field(default_factory=set)   # the nets drawn as a ground symbol (reference and ground nets)
    # what the path's circuit adds beside the board's parts (analog_net: the driver's source resistance in series,
    # the load at each receiver), as ChainParts with setting=True; the schematic draws them in the accent colour
    source: list = field(default_factory=list)
    load: list = field(default_factory=list)
    legs: list = field(default_factory=list)        # the routes to the other receivers (ChainLeg), drawn below
    leg_errors: list = field(default_factory=list)  # [(receiver, why)] for those without a route

    def text(self) -> str:
        """`U42.3 → U41 ×1 → R41 4k7 → U53.9` (several receivers joined by commas)."""
        return " → ".join([self.driver, *self.parts, ", ".join(self.receivers)])

    def full_text(self) -> str:
        """The path as the Signal paths tab used to write it (and copies it): its chain, the branches per net, the
        notes."""
        if self.error:
            return f"no route ({self.error})"
        text = f"Path: {self.text()}"
        if self.branches:
            text += "\nBranches:" + "".join(f"\n  at {net}: " + ", ".join(parts) for net, parts in self.branches)
        for leg in self.legs:
            parts = [p.name + (f" {p.value}" if p.value and p.kind not in ("opamp", "buffer") else "")
                     for p in leg.series]
            text += f"\nTo {leg.receiver}: " + " → ".join([self.driver, *parts, leg.receiver])
        for rx, why in self.leg_errors:
            text += f"\nTo {rx}: no route ({why})"
        return text + "".join(f"\n⚠ {note}" for note in self.notes)


# a receiver pin's input capacitance is picofarads: a few pF for a logic or ADC input, tens with a connector and its
# cable (assumption [A119]: bounds for a typed value to be taken as meant)
LOAD_PF_PLAUSIBLE = (0.1, 1000.0)


def load_pf_note(load_pf) -> str:
    """A note when a path's Load C (pF) is far outside a pin's input capacitance: a value typed in farads (8e-12 →
    8e-12 pF, nothing), or a board capacitor's value (1 µF), which the path's circuit then adds at every receiver on
    top of the board's parts (the user, 2026-11-26: "There is only one 1uF ... This really shouldn't be right?!?").
    "" when it is plausible or not set."""
    if load_pf is None or LOAD_PF_PLAUSIBLE[0] <= load_pf <= LOAD_PF_PLAUSIBLE[1]:
        return ""
    from ..components.values import format_si
    if load_pf < LOAD_PF_PLAUSIBLE[0]:
        hint = (f"typed in farads? the field is in pF: {load_pf * 1e12:g} for {format_si(load_pf, 'F')}"
                if 1e-15 <= load_pf <= 1e-9 else "the field is in pF")
    else:
        hint = ("a capacitor on the board is in the circuit already as itself; this one is added at every receiver "
                "pin on top of it")
    return (f"Load C {format_si(load_pf * 1e-12, 'F')} at each receiver: a pin's input is a few pF ({hint}). "
            "Empty: the default")


def _kind(config: Config, ref: str) -> str:
    """A part's symbol kind for the path schematic: by its model, else by its reference's letters."""
    m = config.model_for(ref)
    kind = m.kind if m is not None else ""
    if kind == "amplifier":
        return "opamp"
    if kind in ("resistor", "cap", "inductor", "ferrite", "short", "buffer"):
        return kind
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    return {"R": "resistor", "C": "cap", "L": "inductor", "FB": "ferrite"}.get(letters, "part")


def _series_part(board: Board, config: Config, ref: str, pads) -> ChainPart:
    """A series part of a route as the schematic draws it: an op-amp by its channel (the unit whose input and output
    the route passes: U13B) with "op-amp" or its gain under it, any other part with its value."""
    kind = _kind(config, ref)
    fp = board.footprint(ref)
    if kind == "opamp":
        name = ref
        if pads is not None and fp is not None:
            pin_in, pin_out = pads
            name = next((unit for unit, ins, out in _amp_units(config, fp)
                         if out.pin == pin_out.pin and any(p.pin == pin_in.pin for p in ins)), ref)
        g = config.model_for(ref).params.get("gain")
        part = ChainPart(ref, name, "op-amp" if g is None else f"×{g:g}", kind)
        if pads is not None and fp is not None:
            _opamp_inputs(board, config, fp, pads, part)
        return part
    if kind == "buffer":
        return ChainPart(ref, ref, "buffer", kind)
    entry = config.components.get(ref)
    value = entry.value if entry is not None else (fp.value if fp is not None else "")
    return ChainPart(ref, ref, value or "", kind)


def _part_value(board: Board, config: Config, fp) -> str:
    """A part's value as the settings or the board give it."""
    entry = config.components.get(fp.reference)
    return (entry.value if entry is not None else fp.value) or ""


def _opamp_inputs(board: Board, config: Config, fp, pads, part: ChainPart):
    """Fill an op-amp channel's ChainPart with its other input (the one the route does not enter): its net, whether
    it is the output itself (a follower), the 2-pin parts from it to the output (feedback) and the other 2-pin parts
    on it (to a reference, another stage's output). The schematic draws them at the triangle's second input."""
    from .analog_net import opamp_channels
    pin_in, pin_out = pads
    ch = next((c for c in opamp_channels(fp, config.model_for(fp.reference))
               if c.out is not None and c.out.pin == pin_out.pin
               and pin_in.pin in {p.pin for p in (c.inp, c.inn) if p is not None}), None)
    if ch is None:
        return
    out = ch.out
    part.inverting = ch.inn is not None and ch.inn.pin == pin_in.pin
    other = ch.inp if part.inverting else ch.inn
    if other is None or not other.net:
        return
    part.other_in = other.net
    part.follower = other.net == out.net
    if part.follower:
        return
    for q in board.footprints:
        nets = [x.net for x in q.pads if x.net]
        if q.reference == fp.reference or len(q.pads) != 2 or len(set(nets)) != 2 or other.net not in nets:
            continue
        to = next(n for n in nets if n != other.net)
        cp = ChainPart(q.reference, q.reference, _part_value(board, config, q), _kind(config, q.reference),
                       other.net, to, False, q.dnp)
        (part.feedback if to == out.net else part.in_parts).append(cp)
    part.feedback.sort(key=lambda c: c.ref)
    part.in_parts.sort(key=lambda c: c.ref)


def _leg_of(board: Board, config: Config, route: Route, receiver: str) -> ChainLeg:
    """A route as a ChainLeg: each series part's pads give the nets before and after it."""
    pads = list(getattr(route, "part_pads", []) or [])
    pads += [None] * (len(route.parts) - len(pads))
    series = [_series_part(board, config, r, p) for r, p in zip(route.parts, pads)]
    nets = [route.driver.net] + [p[1].net if p is not None else "" for p in pads]
    nets[-1] = route.receiver.net
    return ChainLeg(receiver, nets, series, path_branch_parts(board, config, route))


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
    # the circuit for the schematic, to the first receiver and then to each other one (the user, 2026-11-26: "it
    # only shows one input, u13C is connected to U7.12 and uses another OPA in subtraction/amplification mode")
    main = _leg_of(board, config, route, spec.receivers[0])
    chain.nets, chain.series, chain.branch_parts = main.nets, main.series, main.branch_parts
    chain.branches = _branch_text(chain.branch_parts)
    for rx in spec.receivers[1:]:
        try:
            other = find_route(board, config, spec.name, spec.driver, rx, spec.through)
        except RouteError as e:
            chain.leg_errors.append((rx, str(e).split(" (")[0]))
            continue
        chain.legs.append(_leg_of(board, config, other, rx))
    from ..components.models import GROUND_NAME
    chain.ground_nets = set(config.reference_nets) | {n for n in board.nets if GROUND_NAME.match(n.split("/")[-1])}
    # the settings' parts of the path's circuit (analog_net builds the same: the path's value, else the default)
    from ..components.values import format_si
    a = config.analysis
    gnd = next(iter(sorted(config.reference_nets)), "GND")
    r_src = spec.driver_ohms if spec.driver_ohms is not None else a.driver_ohms
    src_how = "path setting" if spec.driver_ohms is not None else "default"
    chain.source = [ChainPart("", "R source", f"{format_si(r_src, 'Ω')} ({src_how})", "resistor", setting=True)]
    c_load = spec.load_pf if spec.load_pf is not None else a.load_pf
    load_how = "path setting" if spec.load_pf is not None else "default"
    chain.load = [ChainPart("", "C load", f"{format_si(c_load * 1e-12, 'F')} ({load_how})", "cap",
                            route.receiver.net, gnd, setting=True)]
    if spec.load_ohms is not None:
        chain.load.append(ChainPart("", "R load", f"{format_si(spec.load_ohms, 'Ω')} (path setting)", "resistor",
                                    route.receiver.net, gnd, setting=True))
    for leg in chain.legs:                  # the same load at every receiver
        leg.load = [replace(c, net=leg.nets[-1]) for c in chain.load]
    note = load_pf_note(spec.load_pf)
    if note:
        chain.notes.append(note)
    # an op-amp by its pins on the path's nets whose model is 'open' (not one the route passes): the circuit leaves
    # it out, and with it what its feedback parts do
    from .analog_net import opamp_pin_roles, unmodelled_note
    nets = {route.driver.net, *route.nets, route.receiver.net}
    passed = set(route.parts)
    loose = [fp.reference for fp in board.footprints
             if fp.reference not in passed and not fp.dnp and any(p.net in nets for p in fp.pads if p.net)
             and (m := config.model_for(fp.reference)) is not None and m.kind == "open"
             and opamp_pin_roles(fp) is not None]
    if loose:
        chain.notes.append(unmodelled_note(sorted(loose)))
    return chain


DNP_MARK = "✗"


def path_branches(board: Board, config: Config, route: Route) -> list:
    """What hangs off a path, for the Signal paths tab (the user: "add branches to let's say GND or other nets to also
    show capacitors, pullups/downs. mark the ones ... that are DNP"): per net of the path, in path order, every 2-pin
    part with one pin on it and the other on another net, not one of the path's series parts, DNP ones included and
    marked; a part across a series part's two nets is listed as parallel to it ("C2 100n ∥ R2"), across an op-amp
    stage's input and output as its feedback ("R4 2k94 ∥ U1"). Display only: the solver's shunts (shunt_parts) leave
    DNP parts out and take reference nets only. As text: [(net, ["R45 10k → GND", ...])]."""
    return _branch_text(path_branch_parts(board, config, route))


def _branch_text(items: list) -> list:
    """path_branch_parts' items as the tab's text, sorted by reference within each net."""
    return [(net, sorted((text for _, text in parts), key=lambda s: s.lstrip(DNP_MARK + " "))) for net, parts in items]


def path_branch_parts(board: Board, config: Config, route: Route) -> list:
    """path_branches with each part as a ChainPart too: [(net, [(ChainPart, text)])], in path order (the schematic
    draws the ChainPart, the tab writes the text)."""
    # the path's nets in route order (its steps' nets as they come; route.nets is a set), any the steps miss after
    order = list(dict.fromkeys(n for n in [route.driver.net, *(getattr(s, "net", "") for s in route.steps),
                                           *sorted(route.nets), route.receiver.net] if n))
    series = set(route.parts)
    # the net pair each series part spans: a part across the same two nets is in parallel with it (a feedback
    # capacitor across its resistor, a speed-up capacitor across a series resistor)
    spans: dict[frozenset, list[str]] = {}
    on_route = set(order)
    for ref in route.parts:
        sfp = board.footprint(ref)
        snets = {p.net for p in sfp.pads if p.net} if sfp is not None else set()
        amp = next(((p, out) for _, ins, out in (_amp_units(config, sfp) if sfp is not None else [])
                    for p in ins if p.net in on_route and out.net in on_route and p.net != out.net), None)
        if amp is not None:                 # an op-amp stage (its channel on the route) spans its input's and
            snets = {amp[0].net, amp[1].net}    # output's nets: its feedback
        if len(snets) == 2:
            spans.setdefault(frozenset(snets), []).append(ref)
    found: dict[str, list[tuple]] = {n: [] for n in order}
    for fp in board.footprints:
        if fp.reference in series:
            continue
        nets = [p.net for p in fp.pads if p.net]
        if len(fp.pads) != 2 or len(set(nets)) != 2:
            continue
        # "(DNP)" right after the part it is about, not at the end of the line: "✗ R23 0R ∥ U13 (DNP)" read as if
        # the op-amp were not fitted (the user, 2026-11-26: "it says U13 DNP? schematic shows R23 DNP")
        part = _part_text(board, config, fp.reference)
        part = f"{DNP_MARK} {part} (DNP)" if fp.dnp else part
        entry = config.components.get(fp.reference)
        value = (entry.value if entry is not None else fp.value) or ""
        on = [n for n in nets if n in found]
        if len(on) == 2:                    # both pins on the path: across a series part, or another branch's part
            # each series part once: an op-amp with two channels on the path spans their nets twice ("U13, U13")
            across = list(dict.fromkeys(spans.get(frozenset(nets), [])))
            first, last = sorted(on, key=order.index)
            # parallel to a series part or an op-amp stage; else a bridge between two of the path's nets (a
            # multiple-feedback filter's R from the input node to the output), named by the net it reaches
            cp = ChainPart(fp.reference, fp.reference, value, _kind(config, fp.reference), first, last, True, fp.dnp)
            found[first].append((cp, f"{part} ∥ {', '.join(across)}" if across else f"{part} → {last}"))
            continue
        if len(on) != 1:
            continue
        other = next(n for n in nets if n != on[0])
        cp = ChainPart(fp.reference, fp.reference, value, _kind(config, fp.reference), on[0], other, False, fp.dnp)
        found[on[0]].append((cp, f"{part} → {other}"))
    return [(n, sorted(found[n], key=lambda t: t[0].ref)) for n in order if found[n]]


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
