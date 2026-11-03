"""Every route between two points of a board, and everything that hangs on it (the user, 2026-11-03: "signal path
tracing does not work for more complex paths ... create a script where i can define start and endpoint and it maps
every single path in my pcb, including the ones going to ground and power, there are also OPamps connected to them
for bias etc", then "maybe the user could select the two ports (pads) and then click the add signal button and GT
traces it this way").

Unlike the signal-path detection (analysis/detect, analysis/route: only parts that pass a signal by default, op-amps
it can tell), this is a map: the nets are the nodes, and a route may run through

- any part joining two nets (resistor, capacitor, inductor, ferrite, diode, 0R, a two-pin part of any model), either
  way, unless it is not fitted (DNP);
- an op-amp channel, from its + or − input to its output (analysis/analog_net.opamp_channels: GT amp labels, pin
  names, or the part's model pins), and a logic buffer's or comparator's channel, input to output
  (components/models.buffer_pins).

It never runs through a ground or a supply (analysis/analog_net.ac_ground_nets, detect.is_power_net) unless that is
the end asked for. Every route is a simple path (no net twice) [A81]; the search stops at `max_routes` routes or
`max_nets` nets per route and says so.

Then, for every net on a route, what else is on it (`branches`): parts to ground or a supply (decoupling, pull-ups,
dividers), op-amps driving it (a bias stage, followed to its own inputs), op-amps listening to it, ICs' and
transistors' pins, and side branches into other nets, followed `branch_depth` parts further. No wx, no KiCad.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config.settings import Config
from ..core.board import Board, Footprint

MAX_ROUTES = 200            # routes kept before the search stops (a dense board has many through its capacitors)
MAX_NETS = 30               # nets on one route at most
BRANCH_DEPTH = 3            # parts followed into a side branch


@dataclass
class Hop:
    """One step of a route: through `part` (its value) from `pin_in` to `pin_out` ("R12.1" -> "R12.2"); kind "part"
    (a two-net part), "opamp" (an op-amp channel, `part` its name: U3A) or "buffer" (a buffer's or comparator's
    channel)."""
    part: str
    value: str
    kind: str
    pin_in: str
    pin_out: str


@dataclass
class Route:
    """The nets a route passes, in order (start first, end last), and the hops between them (one fewer)."""
    nets: list[str]
    hops: list[Hop]


@dataclass
class Branch:
    """Something on a route's net that the routes do not pass: `kind` "ground" / "supply" (a part to a ground or a
    supply net), "net" (a part to another net, `deeper` what is on that net in turn), "opamp_out" (an op-amp channel
    driving this net: `deeper` what drives its inputs), "opamp_in" (an op-amp channel listening here), "buffer_in",
    "ic" (a pin of a part with more nets: an IC, a transistor), or "dnp" (a part not fitted)."""
    kind: str
    part: str
    value: str
    pin: str                   # its pin on this net ("C12.1")
    to_net: str = ""           # the net on its other side (ground, supply, side net, the op-amp's output)
    deeper: list = field(default_factory=list)


@dataclass
class PathMap:
    """The routes from `start` to `ends`, the branches on every net of them ({net: [Branch]}), the rails the
    routes do not cross, and notes (a search cut short, an end not found)."""
    start: str
    ends: list[str]
    routes: list[Route] = field(default_factory=list)
    branches: dict = field(default_factory=dict)
    rails: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def _model(config: Config, fp: Footprint):
    """The part's model: the settings' (a GT label or the user's choice), else the one suggested from the board. Where
    the part's labels or pins say op-amp or buffer and the settings say 'open', or the other of the two (labels
    changed from amp_ to buf_), the labels win: the settings are read as they are, and may be older than them."""
    from ..components.defaults import suggest_model
    m = config.model_for(fp.reference)
    if m is None or m.kind in ("open", "amplifier", "buffer"):
        s = suggest_model(fp)
        if m is None or (s.kind in ("amplifier", "buffer") and s.kind != m.kind):
            return s
    return m


def resolve(board: Board, point: str) -> Optional[str]:
    """The net of a route's end: a pin ("U4.11") or a net name (exactly, or its last part: "PH_A_DIV" for
    "/Analog Conditioning/PH_A_DIV"). None when there is no such pin or net."""
    pad = board.find_pin(point)
    if pad is not None:
        return pad.net or None
    if point in board.nets:
        return point
    tail = [n for n in board.nets if n.rsplit("/", 1)[-1] == point]
    return tail[0] if len(tail) == 1 else None


def rail_nets(board: Board, config: Config) -> set[str]:
    """Grounds and supplies: never passed through (AC ground for a signal, analog_net.ac_ground_nets: reference nets,
    nets on a supply pin, rail names; and every net detect.is_power_net calls a supply)."""
    from .analog_net import ac_ground_nets
    from .detect import is_power_net
    rails = set(ac_ground_nets(board, config))
    rails |= {n for n in board.nets if n not in rails and is_power_net(board, n, config)}
    return rails


class _Graph:
    """The board as nets joined by parts (see the module): for a net, every way on to another net."""

    def __init__(self, board: Board, config: Config):
        """The parts on each net, read once; models from the settings or suggested."""
        self.board, self.config = board, config
        self.by_net = board.parts_by_net()
        self.models = {fp.reference: _model(config, fp) for fp in board.footprints}
        self._chans: dict[str, list] = {}

    def opamp_channels(self, fp: Footprint) -> list:
        """The part's op-amp channels (analog_net.opamp_channels), [] for no amplifier; cached."""
        if fp.reference not in self._chans:
            m = self.models.get(fp.reference)
            chans = []
            if m is not None and m.kind == "amplifier":
                from .analog_net import opamp_channels
                chans = opamp_channels(fp, m)
            self._chans[fp.reference] = chans
        return self._chans[fp.reference]

    def steps(self, net: str) -> list[tuple[str, Hop]]:
        """[(next net, hop)] from `net`: through every fitted two-net part (either way), every op-amp channel with an
        input on it (to its output), every buffer channel with its input on it (to its output)."""
        from ..components.models import buffer_pins
        out = []
        for fp in self.by_net.get(net, []):
            if fp.dnp:
                continue
            m = self.models.get(fp.reference)
            chans = self.opamp_channels(fp)
            if chans:
                for ch in chans:
                    for inp in (ch.inp, ch.inn):
                        if inp is not None and inp.net == net and ch.out.net and ch.out.net != net:
                            out.append((ch.out.net, Hop(ch.name, fp.value, "opamp", f"{fp.reference}.{inp.number}",
                                                        f"{fp.reference}.{ch.out.number}")))
                continue
            if m is not None and m.kind == "buffer":
                for a, b in buffer_pins(fp, m, net):
                    if a.net == net:
                        out.append((b.net, Hop(fp.reference, fp.value, "buffer", f"{fp.reference}.{a.number}",
                                               f"{fp.reference}.{b.number}")))
                continue
            nets = fp.nets()
            if len(nets) == 2:
                other = next(n for n in nets if n != net)
                a = next(p for p in fp.pads if p.net == net)
                b = next(p for p in fp.pads if p.net == other)
                out.append((other, Hop(fp.reference, fp.value, "part", f"{fp.reference}.{a.number}",
                                       f"{fp.reference}.{b.number}")))
        return out


def trace(board: Board, config: Config, start: str, ends: list[str], max_routes: int = MAX_ROUTES,
          max_nets: int = MAX_NETS, branch_depth: int = BRANCH_DEPTH, branches: bool = True) -> PathMap:
    """Every route from `start` to any of `ends` (pins or nets, resolve) and what hangs on them (see the module),
    the shortest routes first. A depth-first search over simple paths [A81]: a net is never visited twice on one
    route, a ground or supply only as an end. branches=False: the routes only (a path picked in KiCad)."""
    pm = PathMap(start, list(ends))
    s = resolve(board, start)
    targets = {}
    for e in ends:
        n = resolve(board, e)
        if n is None:
            pm.notes.append(f"{e}: no such pin or net on the board")
        else:
            targets[n] = e
    if s is None:
        pm.notes.append(f"{start}: no such pin or net on the board")
        return pm
    if not targets:
        return pm
    g = _Graph(board, config)
    rails = rail_nets(board, config)
    pm.rails = sorted(rails - set(targets) - {s})
    cut = {"routes": False, "length": False}

    def walk(net: str, nets: list[str], hops: list[Hop], used: set[str]):
        """Extend the route ending at `net` by every step not taken yet."""
        if len(pm.routes) >= max_routes:
            cut["routes"] = True
            return
        for nxt, hop in g.steps(net):
            if nxt in nets or hop.part in used:
                continue
            if nxt in targets:
                if len(pm.routes) >= max_routes:            # the cap holds for routes ending here too
                    cut["routes"] = True
                    return
                pm.routes.append(Route(nets + [nxt], hops + [hop]))
                continue
            if nxt in rails:
                continue
            if len(nets) + 1 >= max_nets:
                cut["length"] = True
                continue
            walk(nxt, nets + [nxt], hops + [hop], used | {hop.part})

    if s in targets:
        pm.notes.append(f"{start} and {targets[s]} are on the same net, {s}")
        pm.routes.append(Route([s], []))
    else:
        walk(s, [s], [], set())
    if cut["routes"]:
        pm.notes.append(f"stopped after {max_routes} routes: there may be more (max_routes)")
    if cut["length"]:
        pm.notes.append(f"routes longer than {max_nets} nets were not followed (max_nets)")
    if not pm.routes:
        pm.notes.append("no route found: the end may be reached only through a ground, a supply, or a part "
                        "that is neither two-pin nor an op-amp / buffer channel GroundTruth can tell (see the "
                        "branches: an IC's pin)")
    pm.routes.sort(key=lambda r: len(r.hops))           # the shortest first (stable: the search's order after)
    if not branches:
        return pm
    on_routes = list(dict.fromkeys(n for r in pm.routes for n in r.nets)) or [s]
    hops_on = {h.part for r in pm.routes for h in r.hops}
    for net in on_routes:
        pm.branches[net] = _branches(g, net, rails, set(on_routes), hops_on, branch_depth, {net})
    return pm


# ------------------------------------------------------------------------------- a path picked in KiCad: two pads
def routes_between(board: Board, config: Config, a: str, b: str, max_routes: int = 50) -> tuple[str, str, list]:
    """(start pin, end pin, routes) between two pads selected in KiCad (the user: "maybe the user could select the
    two ports (pads) and then click the add signal button and GT traces it this way"), in the direction a signal
    can take: through an op-amp or buffer only from its input; where both ways work (passive parts only), from the
    pad more likely to drive (selection._rank: an output pin before an input, an IC before a passive)."""
    from .selection import _rank
    ab = trace(board, config, a, [b], max_routes=max_routes, branches=False).routes
    ba = trace(board, config, b, [a], max_routes=max_routes, branches=False).routes
    if ab and ba:
        pa, pb = board.find_pin(a), board.find_pin(b)
        if pa is not None and pb is not None and _rank(pb) > _rank(pa):
            return b, a, ba
        return a, b, ab
    return (a, b, ab) if ab or not ba else (b, a, ba)


def route_text(route: Route) -> str:
    """A route as one line: "U4.11 → R12 10k → U3A (op-amp) → PH_A_DIV" (the nets by their last part)."""
    out = [_short(route.nets[0])]
    for hop, net in zip(route.hops, route.nets[1:]):
        out += [f"{hop.part} {hop.value}".strip() + (" (op-amp)" if hop.kind == "opamp" else
                                                      " (buffer)" if hop.kind == "buffer" else ""), _short(net)]
    return " → ".join(out)


def proposal_for_route(board: Board, config: Config, route: Route, start: str, end: str):
    """The route as a signal path to add (selection.PathProposal, as Add KiCad selection makes from selected nets):
    driver `start`, receiver `end`, named after a net with a real name (the end's side first), the route's parts
    that don't pass a signal by default in its `through` (a capacitor, a large resistor); its op-amps and buffers
    kept as their labels make them (a part the settings still have 'open' set to amplifier / buffer, with no pins
    of its own: a quad's channels come from its labels); the GT labels sig_drv / sig_rx added at the ends, so
    Detect finds the path again."""
    from ..components.models import ComponentModel
    from ..config.settings import NetSpec
    from .detect import GENERIC
    from .selection import PathProposal, _add_labels
    from .tags import tag_text
    out = PathProposal()
    driver, receiver = board.find_pin(start), board.find_pin(end)
    if driver is None or receiver is None:
        out.error = f"no pin {start if driver is None else end} on the board"
        return out
    named = [n for n in reversed(route.nets) if not n.startswith("Net-(")]
    name = named[0] if named else route.nets[0]
    through, amps = [], []
    for hop in route.hops:
        ref = hop.pin_in.rsplit(".", 1)[0]
        if hop.kind in ("opamp", "buffer"):
            amps.append((ref, hop.kind))
            continue
        fp = board.footprint(ref)
        if fp is not None and not _model(config, fp).passes_signal():
            through.append(ref)
    out.spec = NetSpec(name, driver.pin, [receiver.pin], GENERIC.freq, GENERIC.rise_time, source="auto",
                       reviewed=False, signal_class=GENERIC.name, through=sorted(set(through)))
    want = {"opamp": "amplifier", "buffer": "buffer"}
    for ref, kind in dict.fromkeys(amps):
        m = config.model_for(ref)
        if m is None or m.kind != want[kind] or m.pins:
            out.models[ref] = ComponentModel(want[kind], dict(m.params) if m is not None and m.kind == want[kind]
                                             else {}, f"an {'op-amp' if kind == 'opamp' else 'buffer'} on a path "
                                             "traced in KiCad (its channels from its GT labels)")
    for ref, word in ((driver.footprint, f"sig_drv:{driver.number}"), (receiver.footprint, f"sig_rx:{receiver.number}")):
        fp = board.footprint(ref)
        old = tag_text(fp) if fp is not None else ""
        if word.replace(" ", "") not in old.replace(" ", ""):
            out.labels[ref] = _add_labels(out.labels.get(ref, old), [word], amp=False)
    out.notes.append(f"{start} → " + route_text(route) + f" → {end}")
    return out


def _branches(g: _Graph, net: str, rails: set, on_routes: set, hops_on: set, depth: int, seen: set,
              via: str = "") -> list:
    """What is on `net` besides the routes (see Branch), followed `depth` parts into side nets and bias stages;
    `seen` keeps a side branch from coming back round, `via` is the part it came through (not listed again)."""
    from ..components.models import buffer_pins
    out = []
    for fp in g.by_net.get(net, []):
        ref = fp.reference
        if ref == via:
            continue
        pin = next((f"{ref}.{p.number}" for p in fp.pads if p.net == net), ref)
        if fp.dnp:
            out.append(Branch("dnp", ref, fp.value, pin))
            continue
        chans = g.opamp_channels(fp)
        if chans:
            for ch in chans:
                if ch.out.net == net and ch.name not in hops_on:
                    # an op-amp driving this net (a bias or reference stage): what drives its inputs
                    deeper = []
                    if depth > 0:
                        for inp in (ch.inp, ch.inn):
                            if inp is not None and inp.net and inp.net not in seen and inp.net != net:
                                deeper.append(Branch("net", ch.name, fp.value, f"{ref}.{inp.number}", inp.net,
                                                     _side(g, inp.net, rails, on_routes, hops_on, depth - 1,
                                                           seen | {inp.net}, ref)))
                    out.append(Branch("opamp_out", ch.name, fp.value, f"{ref}.{ch.out.number}", net, deeper))
                for inp in (ch.inp, ch.inn):
                    if inp is not None and inp.net == net and ch.name not in hops_on:
                        out.append(Branch("opamp_in", ch.name, fp.value, f"{ref}.{inp.number}", ch.out.net))
            continue
        m = g.models.get(ref)
        if m is not None and m.kind == "buffer":
            for a, b in buffer_pins(fp, m, net):
                if a.net == net and ref not in hops_on:
                    out.append(Branch("buffer_in", ref, fp.value, f"{ref}.{a.number}", b.net))
            continue
        nets = fp.nets()
        if len(nets) != 2:
            out.append(Branch("ic", ref, fp.value, pin + _pin_name(fp, net)))
            continue
        if ref in hops_on:
            continue
        other = next(n for n in nets if n != net)
        if other in rails:
            out.append(Branch("ground" if _is_ground(g, other) else "supply", ref, fp.value, pin, other))
        elif other in on_routes:
            out.append(Branch("net", ref, fp.value, pin, other))        # across two nets of the routes
        elif other not in seen:
            out.append(Branch("net", ref, fp.value, pin, other,
                              _side(g, other, rails, on_routes, hops_on, depth - 1, seen | {other}, ref)))
    return out


def _side(g: _Graph, net: str, rails: set, on_routes: set, hops_on: set, depth: int, seen: set,
          via: str) -> list:
    """A side net's own branches, `depth` more parts deep (none at 0), reached through the part `via`."""
    return _branches(g, net, rails, on_routes, hops_on, depth, seen, via) if depth > 0 else []


def _pin_name(fp: Footprint, net: str) -> str:
    """" (OUT)": the schematic pin name of the part's pad on `net`, when it has one."""
    names = [p.pin_function for p in fp.pads if p.net == net and p.pin_function]
    return f" ({names[0]})" if names else ""


def _is_ground(g: _Graph, net: str) -> bool:
    """A ground rather than a supply: a reference net, or a ground name (models.GROUND_NAME)."""
    from ..components.models import GROUND_NAME
    return net in g.config.reference_nets or bool(GROUND_NAME.match(net.rsplit("/", 1)[-1]))


# ------------------------------------------------------------------------------------------------- output
def _short(net: str) -> str:
    """A net's last part for the text ("/Analog Conditioning/PH_A_DIV" -> "PH_A_DIV")."""
    return net.rsplit("/", 1)[-1] or net


def format_text(pm: PathMap) -> str:
    """The map as text: each route as its chain, then every net on them with its branches, indented."""
    lines = [f"Routes from {pm.start} to {', '.join(pm.ends)}: {len(pm.routes)}"]
    for k, r in enumerate(pm.routes, 1):
        chain = [f"[{_short(r.nets[0])}]"]
        for hop, net in zip(r.hops, r.nets[1:]):
            what = f"{hop.part} {hop.value}".strip()
            if hop.kind == "opamp":
                what += f" (op-amp {hop.pin_in.rsplit('.', 1)[1]}→{hop.pin_out.rsplit('.', 1)[1]})"
            elif hop.kind == "buffer":
                what += " (buffer)"
            chain += [what, f"[{_short(net)}]"]
        lines.append(f"  {k:3d}. " + " → ".join(chain))
    lines += [f"  note: {n}" for n in pm.notes]
    lines.append("")
    lines.append("On each net of the routes:")
    for net, branches in pm.branches.items():
        lines.append(f"  [{net}]")
        if not branches:
            lines.append("      (nothing else)")
        lines += _branch_lines(branches, 3)
    return "\n".join(lines) + "\n"


_KIND = {"ground": "to ground", "supply": "to supply", "net": "to", "opamp_out": "driven by op-amp",
         "opamp_in": "into op-amp", "buffer_in": "into buffer", "ic": "pin of", "dnp": "not fitted:"}


def _branch_lines(branches: list, indent: int) -> list[str]:
    """Branch lines, nested branches indented further."""
    out = []
    pad = "  " * indent
    for b in branches:
        what = f"{b.part} {b.value}".strip()
        if b.kind in ("ground", "supply", "net"):
            text = f"{what} {_KIND[b.kind]} [{_short(b.to_net)}]"
        elif b.kind in ("opamp_in", "buffer_in"):
            text = f"{_KIND[b.kind]} {what} ({b.pin}), its output on [{_short(b.to_net)}]"
        elif b.kind == "opamp_out":
            text = f"{_KIND[b.kind]} {what} ({b.pin}); its inputs:"
        elif b.kind == "ic":
            text = f"{_KIND[b.kind]} {what}: {b.pin}"
        else:
            text = f"{_KIND[b.kind]} {what}"
        out.append(f"{pad}{text}")
        out += _branch_lines(b.deeper, indent + 2)
    return out


def format_dot(pm: PathMap) -> str:
    """The routes and their branches as a Graphviz graph (nets as ellipses, parts as labelled edges; rails grey,
    the start and the ends bold): `dot -Tsvg map.dot -o map.svg` draws it."""
    nodes, edges = {}, set()

    def node(net: str, style: str = ""):
        """A net's node (once)."""
        nodes.setdefault(net, style)

    def walk(net: str, branches: list):
        """The branches of `net` as edges."""
        for b in branches:
            if b.to_net:
                node(b.to_net, "rail" if b.kind in ("ground", "supply") else "")
                edges.add((net, b.to_net, f"{b.part} {b.value}".strip()))
            walk(b.to_net or net, b.deeper)
    for r in pm.routes:
        for a, hop, b in zip(r.nets, r.hops, r.nets[1:]):
            node(a)
            node(b)
            edges.add((a, b, f"{hop.part} {hop.value}".strip()))
    for net, branches in pm.branches.items():
        node(net)
        walk(net, branches)
    ends = {r.nets[0] for r in pm.routes} | {r.nets[-1] for r in pm.routes}
    out = ["graph path_map {", "  rankdir=LR;", "  node [shape=ellipse, fontsize=10];", "  edge [fontsize=9];"]
    for net, style in nodes.items():
        attrs = "style=filled, fillcolor=lightgrey" if style == "rail" else ("penwidth=2" if net in ends else "")
        out.append(f'  "{net}" [label="{_short(net)}"{", " + attrs if attrs else ""}];')
    for a, b, label in sorted(edges):
        out.append(f'  "{a}" -- "{b}" [label="{label}"];')
    out.append("}")
    return "\n".join(out) + "\n"
