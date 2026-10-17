"""Tier 1: geometric return-path checks on the routed copper of each configured net.

Planes are the filled zones on the copper layer(s) adjacent to the signal layer (nearest one,
or both for a symmetric stripline). Tracks on the plane layer are not plane: they cut slots
into it, which is exactly what these checks are looking for.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

from shapely.geometry import LineString, MultiLineString, Point as SPoint, Polygon as SPolygon
from shapely.ops import unary_union

from ..analysis.route import Route, RouteError, RouteSegment, RouteVia, find_route
from ..config.settings import Config
from ..core.board import Board
from ..core.issues import Issue, dedupe, resolve_severity, sort_issues
from ..geometry.copper import CopperMap, reference_layers

END_ALLOWANCE_MM = 1.0       # uncovered trace at a run end shorter than this is a via/pad clearance
SMALL_HOLE_MM2 = 1.0         # plane holes smaller than this (antipads) are not "edges"
CLOSING_MM = 0.25            # morphological closing radius when looking for plane edges


@dataclass
class CheckResult:
    """What the rule checks found: the issues (sorted, waivers applied) and the route of each (net, receiver), which
    Simulate takes over instead of tracing the copper again."""
    issues: list[Issue] = field(default_factory=list)
    routes: dict[tuple[str, str], Route] = field(default_factory=dict)   # (net, receiver) -> route
    si: Optional[object] = None         # analysis.si.SiResult: impedance profiles, pairs, crosstalk, via stubs


def _pieces(geom) -> list[LineString]:
    """The LineStrings in a shapely geometry of any kind (a line, a multi-line, a collection), flattened."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    out = []
    for g in getattr(geom, "geoms", []):
        out.extend(_pieces(g))
    return out


def _merge_lines(geom) -> list[LineString]:
    """The geometry's line pieces with touching ones joined: a gap crossed by two consecutive segments of a trace
    is one crossing, not two."""
    from shapely.ops import linemerge
    pieces = _pieces(geom)
    if len(pieces) <= 1:
        return pieces
    merged = linemerge(MultiLineString(pieces))
    return _pieces(merged)


@dataclass
class _Run:
    """A stretch of the route on one layer: consecutive segments joined end to end, between vias (or pads)."""
    layer: str
    segments: list[RouteSegment]

    @property
    def line(self) -> LineString:
        """The run's centreline (a zero-length run gets a 1 nm stub, so it is still a valid line)."""
        pts = [self.segments[0].start] + [s.end for s in self.segments]
        if len(pts) == 2 and pts[0] == pts[1]:
            pts = [pts[0], (pts[0][0] + 1e-6, pts[0][1])]
        return LineString(pts)

    def length_str(self) -> str:
        """The run's length for messages ("12.34 mm of trace")."""
        return f"{sum(s.length for s in self.segments):.2f} mm of trace"

    def nearest_segment(self, p) -> RouteSegment:
        """The segment closest to point p: an issue found along the run points at that track (its uuid selects it
        in KiCad, its line number goes into CI reports)."""
        sp = SPoint(p)
        return min(self.segments, key=lambda s: LineString([s.start, s.end]).distance(sp)
                   if s.start != s.end else sp.distance(SPoint(s.start)))


def _runs(route: Route) -> list[_Run]:
    """The route cut into runs: a new run at every via and wherever two segments don't join end to end (a branch
    of the copper); empty runs are dropped."""
    runs: list[_Run] = []
    for s in route.steps:
        if not isinstance(s, RouteSegment):
            runs.append(None)  # a via breaks the run
            continue
        last = runs[-1] if runs else None
        if last is not None and last.layer == s.layer and math.dist(last.segments[-1].end, s.start) < 1e-6:
            last.segments.append(s)
        else:
            runs.append(_Run(s.layer, [s]))
    return [r for r in runs if r is not None and sum(s.length for s in r.segments) > 0]


def _transitions(route: Route) -> list[tuple[str, str, list[RouteVia]]]:
    """(layer before, layer after, via steps) for every layer change along the route."""
    out = []
    last_layer: Optional[str] = None
    pending: list[RouteVia] = []
    for s in route.steps:
        if isinstance(s, RouteVia):
            pending.append(s)
        else:
            if pending and last_layer is not None and s.layer != last_layer:
                out.append((last_layer, s.layer, pending))
            pending = []
            last_layer = s.layer
    return out


class _Checker:
    """The checks of one run_checks call: they share the board, the settings' rules and the copper map, and
    collect their issues here (add applies the severity overrides)."""

    def __init__(self, board: Board, config: Config, copper: CopperMap):
        """A checker for this board and settings, with no issues yet."""
        self.board = board
        self.config = config
        self.copper = copper
        self.rules = config.rules
        self.refs = set(config.reference_nets)
        self.issues: list[Issue] = []

    def add(self, check: str, net: str, message: str, location, **kw) -> None:
        """Record an issue of `check` at `location` with the severity the rules give it (a check may pass its own
        `severity` as the default); a check switched off in the rules records nothing."""
        severity = kw.pop("severity", None)
        sev = resolve_severity(check, self.rules.severity, severity)
        if sev == "off":
            return
        self.issues.append(Issue(check, sev, net, message, (round(location[0], 4), round(location[1], 4)), **kw))

    # ------------------------------------------------------------------ per run
    def check_run(self, net: str, route: Route, run: _Run) -> None:
        """The checks along one run of the route: no adjacent layer at all (no_reference), stretches without plane
        under them (off_plane at a run's ends, gap_crossing in between, a split if the plane nets on both sides
        differ), a plane of another net than the reference nets under the trace (reference_net), and stretches
        close to a plane edge (check_edges)."""
        refs = reference_layers(self.board, run.layer)
        line = run.line
        if not refs:
            self.add("no_reference", net, f"{run.layer} has no adjacent copper layer to act as reference plane",
                     line.interpolate(0.5, normalized=True).coords[0], layer=run.layer,
                     items=[run.segments[0].uuid], line=run.segments[0].line, key=f"{run.layer}")
            return
        ref_names = [r[0] for r in refs]
        h = refs[0][1]
        plane = unary_union([self.copper.plane(r) for r in ref_names])
        if plane.is_empty:
            seg = run.segments[0]
            self.add("off_plane", net,
                     f"{run.length_str()} on {run.layer} has no plane at all on {'/'.join(ref_names)}",
                     line.interpolate(0.5, normalized=True).coords[0], layer=run.layer, length_mm=line.length,
                     items=[seg.uuid], line=seg.line, key=f"noplane:{seg.uuid}:{run.layer}")
            return
        # the other adjacent layer (a microstrip's far side): where the near plane is missing, the return may
        # still run there, only farther from the trace
        others = [n for n in self.board.stackup.neighbours(run.layer) if n not in ref_names]
        fallback = unary_union([self.copper.plane(n) for n in others]) if others else SPolygon()

        start, end = SPoint(line.coords[0]), SPoint(line.coords[-1])
        gap_zones = []
        # the pieces of trace with no plane copper under them
        for piece in _merge_lines(line.difference(plane)):
            if piece.length < self.rules.min_gap_mm:
                continue
            mid = piece.interpolate(0.5, normalized=True)
            seg = run.nearest_segment(mid.coords[0])
            at_end = piece.distance(start) < 1e-6 or piece.distance(end) < 1e-6
            if at_end:
                # at a run's end the trace leaves the plane at a pad or via: its clearance (antipad) is no gap
                if piece.length <= max(END_ALLOWANCE_MM, 2 * h):
                    continue
                msg = f"{piece.length:.2f} mm of trace on {run.layer} without reference plane on {'/'.join(ref_names)}"
                if not fallback.is_empty and piece.difference(fallback).length < self.rules.min_gap_mm:
                    far = min(self.board.stackup.distance(run.layer, o) for o in others)
                    msg += f"; return falls back to {'/'.join(others)} ({far:.2f} mm away instead of {h:.2f} mm)"
                self.add("off_plane", net, msg, mid.coords[0], layer=run.layer, length_mm=piece.length,
                         items=[seg.uuid], line=seg.line, key=f"off:{seg.uuid}:{round(mid.x)},{round(mid.y)}")
                gap_zones.append(piece)
                continue
            covered_by_fallback = not fallback.is_empty and piece.difference(fallback).length < self.rules.min_gap_mm
            side_nets = self.side_nets(ref_names, piece)
            if set(side_nets) & route.nets:
                # the gap separates the route's own copper (e.g. a switch-node pour repeated on the next layer)
                # from the plane: that copper carries the signal itself, it is not a split in the return path
                continue
            if len(side_nets) == 2 and side_nets[0] != side_nets[1]:
                msg = (f"crosses the {piece.length:.2f} mm split between {side_nets[0]} and {side_nets[1]} "
                       f"on {'/'.join(ref_names)} under {run.layer}")
            else:
                msg = f"crosses a {piece.length:.2f} mm gap in the {'/'.join(ref_names)} plane under {run.layer}"
            sev = None
            if covered_by_fallback:
                msg += f"; return current must fall back to {'/'.join(others)} (farther away)"
                sev = "warning"
            self.add("gap_crossing", net, msg, mid.coords[0], layer=run.layer, length_mm=piece.length,
                     items=[seg.uuid], line=seg.line, severity=sev,
                     key=f"gap:{seg.uuid}:{round(mid.x, 1)},{round(mid.y, 1)}",
                     details={"reference_layers": ref_names, "height_mm": h})
            gap_zones.append(piece)

        # Plane under the trace belongs to a non-reference net (e.g. +3V3)
        for r in ref_names:
            for pnet, g in self.copper.planes(r).items():
                if pnet in self.refs or pnet in route.nets:
                    continue
                inter = _merge_lines(line.intersection(g))
                total = sum(p.length for p in inter)
                if total < 1.0:
                    continue
                longest = max(inter, key=lambda p: p.length)
                mid = longest.interpolate(0.5, normalized=True)
                seg = run.nearest_segment(mid.coords[0])
                self.add("reference_net", net,
                         f"{total:.1f} mm on {run.layer} referenced to the {pnet!r} plane on {r}; "
                         f"make sure it is decoupled to {'/'.join(sorted(self.refs))} where the return changes plane",
                         mid.coords[0], layer=run.layer, length_mm=total, items=[seg.uuid], line=seg.line,
                         key=f"refnet:{seg.uuid}:{pnet}:{r}", details={"plane_net": pnet, "plane_layer": r})

        self.check_edges(net, run, line, plane, h, gap_zones)

    def side_nets(self, ref_names: list[str], piece: LineString) -> list[str]:
        """Plane nets just before and just after an uncovered piece of trace."""
        out = []
        for end in (piece.coords[0], piece.coords[-1]):
            probe = SPoint(end).buffer(0.15)
            for r in ref_names:
                n = self.copper.plane_net_at(r, probe)
                if n:
                    out.append(n)
                    break
        return out

    def check_edges(self, net, run: _Run, line: LineString, plane, h: float, gap_zones) -> None:
        """edge_proximity: trace within edge_factor x h of the plane's edge, where the return current crowds and
        the fringing field radiates more. Small features are ignored: the plane is closed morphologically first
        (thin slits between pours bridged) and holes below SMALL_HOLE_MM2 (via antipads) don't count as edges;
        the run's ends and the gaps already reported are left out."""
        limit = self.rules.edge_factor * h
        # closing = grow, then shrink by the same radius: slits narrower than 2 x CLOSING_MM disappear
        closed = plane.buffer(CLOSING_MM).buffer(-CLOSING_MM)
        polys = getattr(closed, "geoms", [closed])
        cleaned = unary_union([SPolygon(p.exterior, [r for r in p.interiors if SPolygon(r).area >= SMALL_HOLE_MM2])
                               for p in polys if isinstance(p, SPolygon) and not p.is_empty])
        if cleaned.is_empty:
            return
        band = cleaned.boundary.buffer(limit)               # everything within the limit of an edge
        near = line.intersection(band).intersection(cleaned)   # trace over the plane but near its edge
        exclude = [SPoint(line.coords[0]).buffer(max(END_ALLOWANCE_MM, limit)),
                   SPoint(line.coords[-1]).buffer(max(END_ALLOWANCE_MM, limit))]
        exclude += [g.buffer(limit * 1.5 + CLOSING_MM) for g in gap_zones]
        near = near.difference(unary_union(exclude))
        for piece in _merge_lines(near):
            if piece.length < 0.5:
                continue
            mid = piece.interpolate(0.5, normalized=True)
            dist = cleaned.boundary.distance(piece)
            seg = run.nearest_segment(mid.coords[0])
            self.add("edge_proximity", net,
                     f"{piece.length:.2f} mm of trace on {run.layer} within {dist:.2f} mm of the plane edge "
                     f"(limit {self.rules.edge_factor:g}×h = {limit:.2f} mm)",
                     mid.coords[0], layer=run.layer, length_mm=piece.length, items=[seg.uuid], line=seg.line,
                     key=f"edge:{seg.uuid}:{round(mid.x, 1)},{round(mid.y, 1)}",
                     details={"distance_mm": dist, "limit_mm": limit})

    # ------------------------------------------------------------------ transitions
    def check_transition(self, net: str, layer_a: str, layer_b: str, vias: list[RouteVia]) -> None:
        """A layer change: when the reference plane changes too, the return current has to cross between the two
        planes near the signal via. Same net on both planes: a via of that net joining them must be within
        stitch_distance (layer_change_stitch). Different nets: a capacitor between them within decap_distance
        (layer_change_decap), the return crossing as displacement current."""
        refs_a = [r[0] for r in reference_layers(self.board, layer_a)]
        refs_b = [r[0] for r in reference_layers(self.board, layer_b)]
        if not refs_a or not refs_b or set(refs_a) & set(refs_b):
            return          # a shared reference layer: the return stays on it (a stripline between both)
        pos = vias[0].position
        # which plane net the return uses on each side: the plane found around the via
        probe = SPoint(pos).buffer(max(1.0, self.rules.stitch_distance_mm / 2))
        net_a = next((n for n in (self.copper.plane_net_at(r, probe) for r in refs_a) if n), None)
        net_b = next((n for n in (self.copper.plane_net_at(r, probe) for r in refs_b) if n), None)
        if net_a is None or net_b is None:
            return
        uuids = [v.uuid for v in vias if v.uuid]
        line = next((v.line for v in vias if v.line), 0)
        layer_ra = next(r for r in refs_a if self.copper.plane_net_at(r, probe) == net_a)
        layer_rb = next(r for r in refs_b if self.copper.plane_net_at(r, probe) == net_b)
        if net_a == net_b:
            d = self.nearest_stitch(net_a, layer_ra, layer_rb, pos)
            if d is None or d > self.rules.stitch_distance_mm:
                found = "none on the board" if d is None else f"nearest is {d:.2f} mm away"
                self.add("layer_change_stitch", net,
                         f"signal via {layer_a}→{layer_b} moves the return from {net_a} on {layer_ra} to {layer_rb}; "
                         f"no {net_a} via joining them within {self.rules.stitch_distance_mm:g} mm ({found})",
                         pos, layer=layer_a, items=uuids, line=line, key=f"stitch:{','.join(uuids) or pos}",
                         details={"nearest_mm": d, "from_plane": layer_ra, "to_plane": layer_rb, "plane_net": net_a})
        else:
            d = self.nearest_decap(net_a, net_b, pos)
            if d is None or d > self.rules.decap_distance_mm:
                found = "none on the board" if d is None else f"nearest is {d:.2f} mm away"
                self.add("layer_change_decap", net,
                         f"signal via {layer_a}→{layer_b} moves the return from {net_a} ({layer_ra}) to {net_b} "
                         f"({layer_rb}); no capacitor between them within {self.rules.decap_distance_mm:g} mm ({found})",
                         pos, layer=layer_a, items=uuids, line=line, key=f"decap:{','.join(uuids) or pos}",
                         details={"nearest_mm": d, "nets": [net_a, net_b]})

    def nearest_stitch(self, plane_net: str, la: str, lb: str, pos) -> Optional[float]:
        """Distance (mm) from pos to the nearest via or through-hole pad of `plane_net` that reaches both layers,
        or None when there is none."""
        best = None
        for v in self.board.vias:
            if v.net == plane_net and la in v.layers and lb in v.layers:
                d = math.dist(v.position, pos)
                best = d if best is None or d < best else best
        for p in self.board.pads():
            if p.net == plane_net and la in p.layers and lb in p.layers:
                d = math.dist(p.position, pos)
                best = d if best is None or d < best else best
        return best

    def nearest_decap(self, net_a: str, net_b: str, pos) -> Optional[float]:
        """Distance (mm) from pos to the nearest pad of a capacitor (or a short) joining the two nets, or None."""
        best = None
        for fp in self.board.footprints:
            model = self.config.model_for(fp.reference)
            if model is None or model.kind not in ("cap", "short"):
                continue
            if {net_a, net_b} <= fp.nets():
                d = min(math.dist(p.position, pos) for p in fp.pads if p.net in (net_a, net_b))
                best = d if best is None or d < best else best
        return best


def _schematic_differs(board: Board, config: Config, checker: "_Checker", selected: Optional[set]):
    """schematic_differs: a part parameter set in GroundTruth that differs from the symbol's field
    (components/fields.py), at the part (a stage: at its first part)."""
    from ..components.fields import differences
    for d in differences(config, board):
        if selected and d.name not in selected:
            continue
        if d.kind == "part":
            ref = d.name
        else:
            specs = config.converters if d.kind == "converter" else config.bridges
            ref = next((s.parts[0] for s in specs if s.name == d.name and s.parts), "")
        fp = board.footprint(ref)
        checker.add("schematic_differs", d.name, d.message(), fp.position if fp else (0.0, 0.0),
                    layer=fp.layer if fp else "", key=f"{d.kind}:{d.name}:{d.key}",
                    details={"key": d.key, "ours": d.ours, "schematic": d.schematic.value,
                             "source": d.schematic.source})


def _unusual_values(board: Board, config: Config, checker: "_Checker", selected: Optional[set]):
    """unusual_value: a converter's or bridge's value outside the usual range for its kind (settings.implausible),
    often a missing unit prefix. Used as written, never blocking; at the stage's first part."""
    from ..config.settings import implausible
    for spec in list(config.converters) + list(config.bridges):
        if selected and spec.name not in selected:
            continue
        fp = board.footprint(spec.parts[0]) if spec.parts else None
        for m in implausible(spec):
            checker.add("unusual_value", spec.name, m, fp.position if fp else (0.0, 0.0),
                        layer=fp.layer if fp else "", key=f"{spec.name}:{m.split(' ', 1)[0]}")


def run_checks(board: Board, config: Config, copper: Optional[CopperMap] = None,
               nets: Optional[Iterable[str]] = None) -> CheckResult:
    """Tier 1: trace each configured net from its driver to every receiver (find_route) and run the geometric
    checks along it; plus the settings checks (schematic_differs, unusual_value). nets: only these (switched-off
    rows are checked when named). Issues deduplicated, waivers applied, sorted."""
    nets = list(nets) if nets else None
    config = config.active(nets)            # rows switched off are not checked, unless named
    copper = copper or CopperMap(board)
    checker = _Checker(board, config, copper)
    result = CheckResult()
    selected = set(nets) if nets else None
    for spec in config.nets:
        if selected and spec.name not in selected:
            continue
        for rx in spec.receivers:
            try:
                route = find_route(board, config, spec.name, spec.driver, rx, spec.through)
            except RouteError as e:
                pad = board.find_pin(rx) or board.find_pin(spec.driver)
                checker.add("route_missing", spec.name, str(e), pad.position if pad else (0.0, 0.0),
                            key=f"route:{spec.driver}->{rx}")
                continue
            result.routes[(spec.name, rx)] = route
            for run in _runs(route):
                checker.check_run(spec.name, route, run)
            for la, lb, vias in _transitions(route):
                checker.check_transition(spec.name, la, lb, vias)
    _schematic_differs(board, config, checker, selected)
    _unusual_values(board, config, checker, selected)
    if not selected:            # the whole board's voltage-aware checks (analysis/safety.py), not for a net's check
        from ..analysis.safety import run_safety_checks
        run_safety_checks(board, config, copper, checker.add)
    # signal integrity (analysis/si.py): impedance, pairs, crosstalk, via stubs of the listed nets (or the selected)
    from ..analysis.si import run_si_checks
    result.si = run_si_checks(board, config, copper, checker.add, selected)
    issues = dedupe(checker.issues)
    for w in config.waivers:
        for i in issues:
            if i.fingerprint == w.fingerprint:
                i.waived, i.waiver_reason = True, w.reason
    result.issues = sort_issues(issues)
    return result
