"""Signal integrity (the plan's M3): impedance along the tracks, differential pairs, crosstalk and via stubs.

Every figure comes from cross-sections of the board solved by solver/xsection.py: a cut perpendicular to a track,
across every copper layer, with the copper of all nets in it (CopperMap: tracks, pads, zone fills). The signal
net(s) are the conductors driven, every other net is the reference at 0 V (planes, pours, quiet neighbours: the
usual assumption for Z0); the dielectrics are the stackup's, with the solder mask over the outer layers (KiCad's
default 0.01 mm of εr 3.3 when the board file has no stackup). Equal cuts (the same copper at the same offsets, as
along a straight track) are solved once (`SectionCache`).

Run checks:
- impedance: a net's Z0 along its tracks against a target (settings `si: impedance:` by name, pattern or KiCad net
  class): the length outside the tolerance, the worst Z0 and where. Listed signal nets without a target get their
  Z0 profile shown (no issue).
- diff_impedance / diff_skew (settings `si: pairs:`, detection suggests them by name): the pair's differential
  impedance along P's tracks (where N runs beside it; elsewhere the pair is uncoupled, about 2·Z0), and the delay
  mismatch of the two nets' routed lengths (each layer's delay from its cross-sections) against max_skew (default a
  tenth of the edge).
- crosstalk: a listed net (the aggressor, its edge and swing from `nets:`) running parallel to another net's track
  on one layer: near-end NEXT = K_b·min(1, 2·T_d/t_r) and far-end FEXT = |K_f|·T_d/t_r of the swing, K_b and K_f
  from the pair's cross-section at its closest stretch and T_d the delay of the coupled length (all parallel runs of
  the two nets together, at the closest spacing: an upper estimate). Above `crosstalk_max` (5 %) an issue on the
  victim.
- via_stub: a via of a listed net or pair whose barrel runs past the layers the net uses: the stub resonates at
  f = c/(4·l·sqrt(εr)) (a quarter wave, a notch in the transmission); below `stub_factor` (3) times the signal's
  knee 0.35/t_r it eats into the edge's spectrum.

Validated in tests/test_si.py on synthetic boards against closed forms (a microstrip's Z0, a pair's Zdiff, the
crosstalk of two parallel tracks, a stub's quarter wave) and the solver's own tests (tests/test_xsection.py).
"""
from __future__ import annotations

import fnmatch
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..core.board import Board, Track
from ..solver.xsection import C_LIGHT, Rect, Section, SectionResult, Slab, solve

KNEE = 0.35                     # a signal's knee frequency 0.35/t_r: most of an edge's spectrum lies below it
SEGMENT_MIN_MM = 0.2            # shorter track pieces (pad escapes, corners) are not cut
PARALLEL_DEG = 15.0             # tracks within this angle of each other run parallel (crosstalk)
COUPLED_MIN_MM = 0.5            # a parallel stretch shorter than this is left out
DEFAULT_MASK = (0.01, 3.3)      # KiCad's default solder mask: mm, εr


# ------------------------------------------------------------------------------------------------ net classes
def kicad_project(board: Board) -> dict:
    """The project file beside a board file (.kicad_pro, JSON), {} for a live board or without one (read only)."""
    src = board.source or ""
    if not src.endswith(".kicad_pcb"):
        return {}
    try:
        return json.loads(Path(src).with_suffix(".kicad_pro").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def kicad_netclasses(board: Board, project: Optional[dict] = None) -> dict[str, str]:
    """{net: KiCad net class} from the project file beside a board file (netclass_assignments, then the
    netclass_patterns with KiCad's wildcards * and ?); nets in neither are KiCad's Default class (left out)."""
    ns = (kicad_project(board) if project is None else project).get("net_settings") or {}
    out: dict[str, str] = {}
    patterns = [(str(p.get("pattern", "")), str(p.get("netclass", ""))) for p in ns.get("netclass_patterns") or []
                if isinstance(p, dict)]
    for net in board.nets:
        for pat, cls in patterns:
            if pat and cls and fnmatch.fnmatchcase(net, pat):
                out[net] = cls
                break
    for net, cls in (ns.get("netclass_assignments") or {}).items():
        cls = cls[0] if isinstance(cls, list) and cls else cls
        if isinstance(cls, str) and cls:
            out[net] = cls
    return out


# KiCad 10's tuning profiles (Board Setup > Tuning Profiles), in the project file's tuning_profiles >
# tuning_profiles_impedance_geometric: profile_name, type (0 single, 1 differential), target_impedance (Ω, 0 = none),
# enable_time_domain_tuning, layer_entries [{signal_layer, top_reference_layer, bottom_reference_layer ("UNDEFINED"
# = none), width and diff_pair_gap (nm), delay (attoseconds per mm: 100 ps/cm = 10 000 000)}], via_overrides
# [{signal_layer_from/to, via_layer_from/to, delay (as)}], via_prop_delay. Net classes name theirs in
# net_settings > classes > tuning_profile. (Learned from the user's "Test" profile on refboard, 2026-10-17.)
AS_PER_MM = 1e-18 / 1e-3        # a profile's unit delay (as/mm) in s/m


def kicad_profiles(board: Board, project: Optional[dict] = None) -> dict[str, dict]:
    """{profile name: {z0 (None for 0), diff (bool), time_domain (bool), layers: {layer: {delay (s/m or None),
    width_mm, gap_mm, top, bottom}}, classes: [net classes using it]}} from the project file."""
    pro = kicad_project(board) if project is None else project
    raw = ((pro.get("tuning_profiles") or {}).get("tuning_profiles_impedance_geometric") or [])
    classes = [c for c in (pro.get("net_settings") or {}).get("classes") or [] if isinstance(c, dict)]
    out = {}
    for p in raw:
        if not isinstance(p, dict) or not p.get("profile_name"):
            continue
        layers = {}
        for e in p.get("layer_entries") or []:
            if isinstance(e, dict) and e.get("signal_layer"):
                ref = lambda k: None if e.get(k) in (None, "", "UNDEFINED") else e.get(k)
                layers[e["signal_layer"]] = {"delay": (float(e.get("delay") or 0) * AS_PER_MM) or None,
                                             "width_mm": float(e.get("width") or 0) * 1e-6,
                                             "gap_mm": float(e.get("diff_pair_gap") or 0) * 1e-6,
                                             "top": ref("top_reference_layer"), "bottom": ref("bottom_reference_layer")}
        name = str(p["profile_name"])
        out[name] = {"z0": float(p.get("target_impedance") or 0) or None, "diff": int(p.get("type") or 0) != 0,
                     "time_domain": bool(p.get("enable_time_domain_tuning")), "layers": layers,
                     "classes": [str(c.get("name")) for c in classes if c.get("tuning_profile") == name]}
    return out


def profile_nets(board: Board, project: Optional[dict] = None) -> dict[str, str]:
    """{net: tuning profile} through the nets' KiCad net classes (a net in no class is in Default)."""
    pro = kicad_project(board) if project is None else project
    profiles = kicad_profiles(board, pro)
    by_class = {c: name for name, p in profiles.items() for c in p["classes"]}
    if not by_class:
        return {}
    classes = kicad_netclasses(board, pro)
    return {net: by_class[classes.get(net, "Default")] for net in board.nets if classes.get(net, "Default") in by_class}


def impedance_targets(board: Board, config, project: Optional[dict] = None) -> dict[str, tuple]:
    """{net: (z0, tol, layers, why)} from `si: impedance:` (the first target that names a net wins), then from the
    KiCad tuning profiles of single-ended nets' classes with a target impedance (10 % tolerance, on the profile's
    layers). A profile on KiCad's Default class (every net in no other class) applies to the listed nets only."""
    si = getattr(config, "si", None)
    targets = list(getattr(si, "impedance", None) or ())
    out = {}
    pro = kicad_project(board) if project is None else project
    profiles = kicad_profiles(board, pro)
    by_net = profile_nets(board, pro)
    if by_net:
        classes, listed = kicad_netclasses(board, pro), {n.name for n in config.nets}
        for net, name in by_net.items():
            p = profiles[name]
            if p["z0"] and not p["diff"] and (classes.get(net, "Default") != "Default" or net in listed):
                out[net] = (p["z0"], 0.10, list(p["layers"]), f"KiCad tuning profile {name}")
    if not targets:
        return out
    classes = kicad_netclasses(board, pro) if any(t.netclass for t in targets) else {}
    for net in board.nets:
        for t in targets:
            hit = any(net == p or fnmatch.fnmatchcase(net, p) for p in t.nets) or (
                t.netclass and classes.get(net) == t.netclass)
            if hit:
                why = f"net class {t.netclass}" if t.netclass and classes.get(net) == t.netclass else "si: impedance"
                out[net] = (t.z0, t.tol, list(t.layers), why)
                break
    return out


# ------------------------------------------------------------------------------------------------ differential pairs
_PAIR_SUFFIXES = (("_P", "_N"), ("P", "N"), ("+", "-"), ("_H", "_L"), ("H", "L"), ("_DP", "_DM"), ("DP", "DM"),
                  ("", "*"), ("", "#"))


_BARE_STEM = re.compile(r"(\d|CLK|CK|TX|RX|DAT|DATA|LANE|D)$", re.IGNORECASE)


def find_pairs(board: Board) -> list[tuple[str, str]]:
    """Nets that look like a differential pair by their names: _P/_N, P/N, +/-, _H/_L (CAN_H / CAN_L), DP/DM,
    X / X* and X / X# (an inverted twin, RS-485's /RX and /RX*). A pair of single letters (P and N) is not one."""
    names = set(board.nets)
    out, used = [], set()
    for net in sorted(board.nets):
        if net in used:
            continue
        for sp, sn in _PAIR_SUFFIXES:
            if sp and not net.endswith(sp):
                continue
            stem = net[:len(net) - len(sp)] if sp else net
            leaf = stem.rsplit("/", 1)[-1]
            # a bare letter (TX0P / TX0N, CLKP / CLKN) only after a digit or a clock / data stem: VCAP and VCAN are
            # no pair
            if not leaf or (len(sp) == 1 and sp.isalpha() and not _BARE_STEM.search(leaf)):
                continue
            twin = stem + sn
            if twin in names and twin != net and twin not in used:
                out.append((net, twin))
                used.update((net, twin))
                break
    return out


def pair_defaults(p: str, n: str) -> tuple[Optional[float], float, str]:
    """(zdiff or None, rise time, interface) suggested for a pair by its name: USB 90 Ω / 500 ps (high speed),
    Ethernet, LVDS, HDMI, MIPI, PCIe, SATA 100 Ω (85 Ω PCIe), CAN and RS-485 no impedance target on the board (their
    slow, slope-controlled edges: 50 ns / 20 ns)."""
    text = f"{p} {n}".upper()
    for words, z, tr, what in ((("USB",), 90.0, 500e-12, "USB"), (("PCIE", "PCI_E"), 85.0, 100e-12, "PCIe"),
                               (("ETH", "MDI", "RGMII", "SGMII"), 100.0, 1e-9, "Ethernet"),
                               (("LVDS",), 100.0, 300e-12, "LVDS"), (("HDMI", "TMDS"), 100.0, 100e-12, "HDMI"),
                               (("MIPI", "CSI", "DSI"), 100.0, 150e-12, "MIPI"), (("SATA",), 100.0, 100e-12, "SATA"),
                               (("CAN",), None, 50e-9, "CAN"), (("RS485", "RS-485", "RS422"), None, 20e-9, "RS-485")):
        if any(w in text for w in words):
            return z, tr, what
    return None, 1e-9, ""


def detect_pairs(board: Board, config) -> list:
    """The differential pairs found by name (find_pairs) that are not listed yet, as unreviewed DiffPairSpecs with
    their interface's defaults (pair_defaults). Supply-like nets (a voltage in the name, grounds: BAT+ / BAT-) and
    nets without tracks are left out."""
    from ..config.settings import DiffPairSpec
    from .pdn import rail_voltage
    from .safety import is_ground
    listed = {frozenset((p.p, p.n)) for p in getattr(getattr(config, "si", None), "pairs", ())}
    routed = {t.net for t in board.tracks}
    out = []
    for pn, nn in find_pairs(board):
        if frozenset((pn, nn)) in listed or not ({pn, nn} <= routed):
            continue
        if any(is_ground(x) or rail_voltage(x) is not None for x in (pn, nn)):
            continue
        z, tr, what = pair_defaults(pn, nn)
        out.append(DiffPairSpec(pn, nn, zdiff=z, rise_time=tr, source="auto", reviewed=False,
                                note=(f"{what} by its name" if what else "a pair by its name")
                                + ("" if z else ": no impedance target suggested")))
    return out


# ------------------------------------------------------------------------------------------------ cross-sections
class SectionCache:
    """Cross-sections of one board, cut and solved on demand: an STRtree of each layer's net geometries for the cut,
    the stackup's heights, and the solved sections by their copper (equal cuts solved once)."""

    def __init__(self, board: Board, copper, threads: int = 1):
        """Index nothing yet: each layer's tree is built when a cut first needs it. threads: for prefetch."""
        self.board, self.copper, self.threads = board, copper, max(1, threads)
        self.trees: dict[str, tuple] = {}
        self.cuts: dict[tuple, Optional[tuple]] = {}
        self.solved: dict[tuple, Optional[SectionResult]] = {}
        st = board.stackup
        self.z = {c.name: (c.z, c.thickness) for c in st.copper}
        self.order = [c.name for c in st.copper]

    def _tree(self, layer: str):
        """(net names, geometries, STRtree) of one layer's copper."""
        if layer not in self.trees:
            from shapely import STRtree
            names, geoms = [], []
            for net in self.copper.nets_on(layer):
                g = self.copper.get(layer, net)
                if g is not None and not g.is_empty:
                    names.append(net)
                    geoms.append(g)
            self.trees[layer] = (names, geoms, STRtree(geoms) if geoms else None)
        return self.trees[layer]

    def h_mm(self, layer: str) -> float:
        """The distance from a layer to its nearest other copper layer (mm): the scale of its field."""
        st = self.board.stackup
        return min((st.distance(layer, o) for o in st.neighbours(layer)), default=1.0) or 1.0

    def cut(self, at, along, layer: str, nets: list[str], width: Optional[float] = None) -> Optional[tuple]:
        """The copper crossing a cut through `at` (mm) perpendicular to the unit vector `along`, half a width that
        reaches 20 field heights (1 to 15 mm): {(layer, conductor, u0, u1)} with the offsets u (mm) along the cut;
        conductors: nets[k] -> k + 1, all other copper 0. None when nets[0] has no copper on `layer` at the cut's
        centre, or (given the track's width) that copper is much wider than the track: a pad, a pour or a corner,
        no line."""
        from shapely.geometry import LineString
        h = self.h_mm(layer)
        half = min(max(20 * h, 1.0), 15.0)
        nx, ny = -along[1], along[0]
        line = LineString([(at[0] - half * nx, at[1] - half * ny), (at[0] + half * nx, at[1] + half * ny)])
        ids = {net: k + 1 for k, net in enumerate(nets)}
        pieces = []
        centre_found = False
        for lay in self.order:
            names, geoms, tree = self._tree(lay)
            if tree is None:
                continue
            for k in tree.query(line):
                g = geoms[int(k)]
                inter = g.intersection(line)
                for seg in _lines(inter):
                    us = [(x - at[0]) * nx + (y - at[1]) * ny for x, y in seg.coords]
                    u0, u1 = min(us), max(us)
                    if u1 - u0 < 1e-6:
                        continue
                    cid = ids.get(names[int(k)], 0)
                    if lay == layer and cid == 1 and u0 <= 1e-6 and u1 >= -1e-6:
                        centre_found = True
                    pieces.append((lay, cid, round(u0, 4), round(u1, 4)))
        if not centre_found:
            return None
        centre = next(p for p in pieces if p[0] == layer and p[1] == 1 and p[2] <= 1e-6 and p[3] >= -1e-6)
        if width and centre[3] - centre[2] > 1.5 * width + 0.05:
            return None
        # the line is the centre piece (and a pair's N: its piece on the same layer nearest the centre); the nets'
        # other copper in the cut is the same net elsewhere along its route (its own track passing under it, a
        # via): a neighbour at 0 V here, not part of the line (refboard's SPI_MISO read 26 Ω with its B.Cu track
        # driven as part of it)
        keep = {centre}
        for cid in range(2, len(nets) + 1):
            own = [p for p in pieces if p[0] == layer and p[1] == cid
                   and (not width or p[3] - p[2] <= 1.5 * width + 0.05)]
            if own:
                keep.add(min(own, key=lambda p: min(abs(p[2]), abs(p[3]))))
        pieces = [p if p in keep or p[1] == 0 else (p[0], 0, p[2], p[3]) for p in pieces]
        return tuple(sorted(pieces)), half

    def section(self, pieces: tuple, half: float, layer: str) -> Section:
        """A cut's Section in metres: copper foils as rectangles (y up, the board's top surface at 0), the
        dielectrics as slabs from the substrate's surfaces to the inner foils' centres (the prepreg fills the gaps
        between the traces), the solder masks on the outer surfaces, air around (20 field heights, 0.5 to 10 mm)."""
        st = self.board.stackup
        rects = []
        for lay, cid, u0, u1 in pieces:
            z, t = self.z[lay]
            rects.append(Rect(u0 * 1e-3, u1 * 1e-3, -(z + t / 2) * 1e-3, -(z - t / 2) * 1e-3, cid))
        cu = st.copper
        bounds = [cu[0].z + cu[0].thickness / 2] + [c.z for c in cu[1:-1]] + [cu[-1].z - cu[-1].thickness / 2]
        slabs = [Slab(-bounds[i + 1] * 1e-3, -bounds[i] * 1e-3, d.epsilon_r)
                 for i, d in enumerate(st.dielectrics) if i + 1 < len(bounds)]
        top, bottom = bounds[0], bounds[-1]
        for mask, surface, sign in ((st.mask_top, top, -1), (st.mask_bottom, bottom, 1)):
            t_m, er_m = (mask.thickness, mask.epsilon_r) if mask is not None else DEFAULT_MASK
            if t_m > 0:
                y_s = -surface * 1e-3
                y_m = y_s - sign * t_m * 1e-3
                slabs.append(Slab(min(y_s, y_m), max(y_s, y_m), er_m))
        air = min(max(20 * self.h_mm(layer), 0.5), 10.0)
        box = (-half * 1e-3, half * 1e-3, -(st.copper[-1].z + st.copper[-1].thickness / 2 + air) * 1e-3,
               (air - (st.copper[0].z - st.copper[0].thickness / 2)) * 1e-3)
        return Section(rects, slabs, box)

    def _cut(self, at, along, layer: str, nets: list[str], width: Optional[float]):
        """cut(), remembered (a prefetch cuts first, the checks then ask again)."""
        k = (round(at[0], 4), round(at[1], 4), round(along[0], 6), round(along[1], 6), layer, tuple(nets), width)
        if k not in self.cuts:
            self.cuts[k] = self.cut(at, along, layer, nets, width)
        return self.cuts[k]

    @staticmethod
    def _key(c, layer: str, nets: list[str], precise: bool) -> Optional[tuple]:
        """A cut's cache key, or None when it can't be solved (no reference copper: nothing to return over, no Z0;
        a conductor missing). Equal cuts are solved once; far copper matters little, so its offsets are compared
        coarser (5 % of their distance from the centre, 1 µm near it): the cuts along a straight run share one."""
        if c is None:
            return None
        pieces = c[0]
        if not any(cid == 0 for _, cid, _, _ in pieces):
            return None
        if any(not any(cid == k for _, cid, _, _ in pieces) for k in range(1, len(nets) + 1)):
            return None
        return layer, precise, tuple((lay, cid, _q(u0), _q(u1)) for lay, cid, u0, u1 in pieces)

    def _solve(self, c, layer: str, precise: bool) -> Optional[SectionResult]:
        """One cut solved (None: a conductor without a grid node)."""
        pieces, half = c
        try:
            return solve(self.section(pieces, half, layer), extrapolate=precise)
        except ValueError:
            return None

    def prefetch(self, jobs: list[tuple]):
        """Solve the distinct cuts of [(at, along, layer, nets, width, precise)] at once, in `threads` threads
        (SuperLU releases the GIL: 2.4 times faster on 4 threads); the checks then find them solved."""
        todo = {}
        for at, along, layer, nets, width, precise in jobs:
            c = self._cut(at, along, layer, nets, width)
            key = self._key(c, layer, nets, precise)
            if key is not None and key not in self.solved and key not in todo:
                todo[key] = (c, layer, precise)
        if not todo:
            return
        if self.threads > 1 and len(todo) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(min(self.threads, len(todo))) as ex:
                results = list(ex.map(lambda job: self._solve(*job), todo.values()))
        else:
            results = [self._solve(*job) for job in todo.values()]
        self.solved.update(zip(todo, results))

    def solve_at(self, at, along, layer: str, nets: list[str], width: Optional[float] = None,
                 precise: bool = True) -> Optional[tuple[SectionResult, tuple]]:
        """(the solved section, its pieces) of a cut, or None (nets[0] not at the centre or not a line there, a
        conductor without a grid node, no reference copper in the cut). precise: extrapolated over two grids (0.2 %
        of exact); else one grid (a few % low in Z0: enough for crosstalk coefficients, at a third of the time)."""
        c = self._cut(at, along, layer, nets, width)
        key = self._key(c, layer, nets, precise)
        if key is None:
            return None
        if key not in self.solved:
            self.solved[key] = self._solve(c, layer, precise)
        r = self.solved[key]
        return (r, c[0]) if r is not None else None


def _q(u: float) -> float:
    """An offset (mm) rounded for the cache key: to 1 µm near the centre, to 5 % of its distance farther out."""
    step = max(1e-3, 0.05 * abs(u))
    return round(round(u / step) * step, 4)


def _lines(geom) -> list:
    """The LineStrings of a shapely intersection (points left out)."""
    if geom is None or geom.is_empty:
        return []
    kind = geom.geom_type
    if kind == "LineString":
        return [geom]
    if kind in ("MultiLineString", "GeometryCollection"):
        return [g for part in geom.geoms for g in _lines(part)]
    return []


def _width_at(pieces: tuple, layer: str, conductor: int = 1) -> float:
    """The width (mm) of the conductor's copper on `layer` through the cut's centre."""
    return next((u1 - u0 for lay, cid, u0, u1 in pieces if lay == layer and cid == conductor and u0 <= 1e-4
                 and u1 >= -1e-4), 0.0)


def _gap_at(pieces: tuple, layer: str) -> Optional[float]:
    """The edge-to-edge gap (mm) between conductor 1 at the centre and the nearest conductor 2 copper on `layer`."""
    c1 = next(((u0, u1) for lay, cid, u0, u1 in pieces if lay == layer and cid == 1 and u0 <= 1e-4 and u1 >= -1e-4),
              None)
    if c1 is None:
        return None
    gaps = [max(u0 - c1[1], c1[0] - u1) for lay, cid, u0, u1 in pieces if lay == layer and cid == 2]
    return min(gaps) if gaps else None


# ------------------------------------------------------------------------------------------------ sampling
def track_points(tracks: list[Track], step_mm: float, board: Optional[Board] = None) -> list[tuple]:
    """Points to cut along tracks: [(x, y, layer, along (unit vector), length each point stands for (mm), track)],
    every step_mm (at least one per track at its middle; arcs by their chord through the mid point). Pieces shorter
    than SEGMENT_MIN_MM are skipped, and with the board, points on a pad or via of the track's net (no line there:
    a cut through a via meets its copper on every layer)."""
    keep_out = _pads_and_vias(board, {t.net for t in tracks}) if board is not None else {}
    out = []
    for t in tracks:
        (x0, y0), (x1, y1) = t.start, t.end
        length = math.hypot(x1 - x0, y1 - y0)
        if length < SEGMENT_MIN_MM:
            continue
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        if t.is_arc:
            out.append((t.mid[0], t.mid[1], t.layer, (ux, uy), length, t))
            continue
        n = max(1, int(length // step_mm))
        for k in range(n):
            f = (k + 0.5) / n
            x, y = x0 + f * (x1 - x0), y0 + f * (y1 - y0)
            if any(math.dist((x, y), c) <= r + t.width / 2 for c, r in keep_out.get(t.net, ())):
                continue
            out.append((x, y, t.layer, (ux, uy), length / n, t))
    return out


def _pads_and_vias(board: Board, nets: set) -> dict[str, list]:
    """{net: [(centre, radius mm)]} of the nets' pads (their half diagonal) and vias."""
    out: dict[str, list] = {}
    for v in board.vias:
        if v.net in nets:
            out.setdefault(v.net, []).append((v.position, v.size / 2))
    for net in nets:
        for fp in board.parts_by_net().get(net, ()):
            for pad in fp.pads:
                if pad.net == net:
                    out.setdefault(net, []).append((pad.position, math.hypot(*pad.size) / 2))
    return out


def net_length(board: Board, net: str) -> dict[str, float]:
    """{layer: routed track length (mm)} of a net, plus "via": the via barrel between the outermost layers its tracks
    use at each of its vias."""
    out: dict[str, float] = {}
    tracks = [t for t in board.tracks if t.net == net]
    for t in tracks:
        out[t.layer] = out.get(t.layer, 0.0) + _track_len(t)
    st = board.stackup
    for v in board.vias:
        if v.net != net:
            continue
        used = _via_layers(board, v, tracks)
        if len(used) >= 2:
            zs = [st.layer(x).z for x in used]
            out["via"] = out.get("via", 0.0) + max(zs) - min(zs)
    return out


def _track_len(t: Track) -> float:
    """A track's length (mm); an arc's from its three points."""
    (x0, y0), (x1, y1) = t.start, t.end
    if not t.is_arc:
        return math.hypot(x1 - x0, y1 - y0)
    mx, my = t.mid
    a, b, c = math.hypot(mx - x0, my - y0), math.hypot(x1 - mx, y1 - my), math.hypot(x1 - x0, y1 - y0)
    s = (a + b + c) / 2
    area = math.sqrt(max(s * (s - a) * (s - b) * (s - c), 0.0))
    if area < 1e-12:
        return c
    r = a * b * c / (4 * area)
    return 2 * r * math.asin(min(1.0, c / (2 * r))) if a * a + b * b > c * c else 2 * r * (
        math.pi - math.asin(min(1.0, c / (2 * r))))


def _via_layers(board: Board, via, tracks: list[Track]) -> list[str]:
    """The copper layers a via's net uses at the via: its tracks ending there and its pads under it."""
    r = via.size / 2 + 0.05
    used = set()
    for t in tracks:
        if t.layer in via.layers and any(math.dist(p, via.position) <= r + t.width / 2 for p in (t.start, t.end)):
            used.add(t.layer)
    for fp in board.parts_by_net().get(via.net, ()):
        for p in fp.pads:
            if p.net == via.net and math.dist(p.position, via.position) <= max(p.size) / 2:
                used.update(x for x in p.layers if x in via.layers)
    order = board.copper_layers
    return sorted(used, key=order.index)


# ------------------------------------------------------------------------------------------------ results
@dataclass
class NetProfile:
    """A net's Z0 along its tracks: per cut (x, y, layer, length mm, Z0 Ω, εeff, width mm), the target if any."""
    net: str
    points: list = field(default_factory=list)
    target: Optional[float] = None
    tol: float = 0.1
    why: str = ""
    unsolved_mm: float = 0.0            # track length whose cut could not be solved (no reference copper)

    def summary(self) -> dict:
        """The profile's figures for reports: length, Z0 range and length-weighted mean, length off target."""
        pts = self.points
        total = sum(p[3] for p in pts)
        out = {"net": self.net, "length_mm": round(total, 2), "target": self.target, "tol": self.tol,
               "unsolved_mm": round(self.unsolved_mm, 2)}
        if pts:
            out.update(z_min=min(p[4] for p in pts), z_max=max(p[4] for p in pts),
                       z_mean=sum(p[4] * p[3] for p in pts) / total if total else pts[0][4])
            if self.target:
                out["off_mm"] = round(sum(p[3] for p in pts if abs(p[4] / self.target - 1) > self.tol), 2)
        return out


@dataclass
class PairResult:
    """A differential pair: Zdiff along P's tracks (x, y, layer, length mm, Zdiff Ω, coupled), each net's routed
    length per layer and delay (s), and the skew."""
    spec: object
    points: list = field(default_factory=list)
    lengths: dict = field(default_factory=dict)          # net -> {layer: mm}
    delays: dict = field(default_factory=dict)           # net -> s
    skew_s: float = 0.0
    uncoupled_mm: float = 0.0

    def summary(self) -> dict:
        """The pair's figures for reports."""
        pts = self.points
        total = sum(p[3] for p in pts)
        out = {"pair": self.spec.label, "p": self.spec.p, "n": self.spec.n, "zdiff_target": self.spec.zdiff,
               "length_mm": {k: round(sum(v.values()), 2) for k, v in self.lengths.items()},
               "skew_ps": round(self.skew_s * 1e12, 1), "skew_limit_ps": round(self.spec.skew_limit * 1e12, 1),
               "uncoupled_mm": round(self.uncoupled_mm, 2)}
        coupled = [p for p in pts if p[5]]
        if coupled:
            out.update(zdiff_min=min(p[4] for p in coupled), zdiff_max=max(p[4] for p in coupled),
                       zdiff_mean=sum(p[4] * p[3] for p in coupled) / sum(p[3] for p in coupled))
        return out


@dataclass
class SiResult:
    """What the signal-integrity checks found besides their issues: the profiles and pairs (reports, the plugin)."""
    nets: list = field(default_factory=list)             # NetProfile
    pairs: list = field(default_factory=list)            # PairResult
    crosstalk: list = field(default_factory=list)        # dicts: aggressor, victim, layer, mm, gap, next, fext
    stubs: list = field(default_factory=list)            # dicts: net, via, stub mm, f_res, knee
    delays: list = field(default_factory=list)           # dicts: a KiCad tuning profile's unit delay vs the solved
    sections: int = 0                                    # cross-sections solved

    def summary(self) -> dict:
        """Everything as plain data (JSON report)."""
        return {"nets": [n.summary() for n in self.nets], "pairs": [p.summary() for p in self.pairs],
                "crosstalk": self.crosstalk, "stubs": self.stubs, "delays": self.delays, "sections": self.sections}


Add = Callable[..., None]


# ------------------------------------------------------------------------------------------------ checks
def check_impedance(board: Board, cache: SectionCache, nets: list[str], targets: dict, step: float,
                    add: Add) -> list[NetProfile]:
    """impedance (see the module): each net's Z0 profile; an issue where a target's net is outside it."""
    out = []
    jobs = []
    for net in nets:
        layers = (targets.get(net) or (None, None, []))[2]
        jobs += [((x, y), along, layer, [net], t.width, True) for x, y, layer, along, share, t in
                 track_points([t for t in board.tracks if t.net == net], step, board) if not layers or layer in layers]
    cache.prefetch(jobs)
    for net in nets:
        target = targets.get(net)
        prof = NetProfile(net, target=target[0] if target else None, tol=target[1] if target else 0.1,
                          why=target[3] if target else "")
        layers = target[2] if target else []
        for x, y, layer, along, share, t in track_points([t for t in board.tracks if t.net == net], step, board):
            if layers and layer not in layers:
                continue
            got = cache.solve_at((x, y), along, layer, [net], t.width)
            if got is None:
                prof.unsolved_mm += share
                continue
            r, pieces = got
            prof.points.append((round(x, 3), round(y, 3), layer, share, r.z0(), r.eeff(), _width_at(pieces, layer)))
        out.append(prof)
        if not prof.target or not prof.points:
            continue
        off = [p for p in prof.points if abs(p[4] / prof.target - 1) > prof.tol]
        if not off:
            continue
        worst = max(off, key=lambda p: abs(p[4] / prof.target - 1))
        s = prof.summary()
        add("impedance", net,
            f"{net}: Z0 {s['z_min']:.0f}-{s['z_max']:.0f} Ω, outside {prof.target:g} Ω ± {prof.tol * 100:g} % along "
            f"{s['off_mm']:.1f} of {s['length_mm']:.1f} mm; worst {worst[4]:.1f} Ω on {worst[2]} ({worst[6]:.3f} mm "
            f"wide)", (worst[0], worst[1]), layer=worst[2], key=f"impedance:{net}",
            details={**s, "worst": {"x": worst[0], "y": worst[1], "layer": worst[2], "z0": worst[4],
                                    "width_mm": worst[6]}, "source": prof.why})
    return out


def _layer_delays(board: Board, cache: SectionCache, net: str, step: float) -> dict[str, float]:
    """{layer: delay per mm (s)} of a net from cross-sections of its own tracks (the mean over its cuts); the
    layer's dielectric (stripline sqrt(εr)/c) where none could be solved."""
    acc: dict[str, list] = {}
    for x, y, layer, along, share, t in track_points([t for t in board.tracks if t.net == net], max(step, 2.0), board):
        got = cache.solve_at((x, y), along, layer, [net], t.width)
        if got is not None:
            acc.setdefault(layer, []).append(got[0].delay() * 1e-3)
    st = board.stackup
    out = {}
    for layer in board.copper_layers:
        if layer in acc:
            out[layer] = sum(acc[layer]) / len(acc[layer])
        else:
            nb = st.neighbours(layer)
            er = st.epsilon_between(layer, nb[0]) if nb else 4.5
            out[layer] = math.sqrt(er) / C_LIGHT * 1e-3
    return out


def check_pairs(board: Board, cache: SectionCache, pairs: list, step: float, add: Add) -> list[PairResult]:
    """diff_impedance and diff_skew (see the module)."""
    out = []
    st = board.stackup
    jobs = []
    for spec in pairs:
        jobs += [((x, y), along, layer, [spec.p, spec.n], t.width, True) for x, y, layer, along, share, t in
                 track_points([t for t in board.tracks if t.net == spec.p], step, board)]
        for net in (spec.p, spec.n):
            jobs += [((x, y), along, layer, [net], t.width, True) for x, y, layer, along, share, t in
                     track_points([t for t in board.tracks if t.net == net], max(step, 2.0), board)]
    cache.prefetch(jobs)
    for spec in pairs:
        res = PairResult(spec)
        for x, y, layer, along, share, t in track_points([t for t in board.tracks if t.net == spec.p], step, board):
            got = cache.solve_at((x, y), along, layer, [spec.p, spec.n], t.width)
            gap = _gap_at(got[1], layer) if got is not None else None
            if got is None or gap is None or gap > 4 * max(_width_at(got[1], layer), 0.1):
                # N not beside P here: the pair is uncoupled; Zdiff about the two lines' Z0 in series
                one = cache.solve_at((x, y), along, layer, [spec.p], t.width)
                res.uncoupled_mm += share
                if one is not None:
                    res.points.append((round(x, 3), round(y, 3), layer, share, 2 * one[0].z0(), False,
                                       one[0].delay()))
                continue
            r, pieces = got
            res.points.append((round(x, 3), round(y, 3), layer, share, r.zdiff(0, 1), True, r.diff_delay(0, 1)))
        delays = {net: _layer_delays(board, cache, net, step) for net in (spec.p, spec.n)}
        for net in (spec.p, spec.n):
            lengths = net_length(board, net)
            res.lengths[net] = lengths
            d = delays[net]
            via_er = st.epsilon_between(st.copper[0].name, st.copper[-1].name) if len(st.copper) > 1 else 4.5
            res.delays[net] = sum(mm * (math.sqrt(via_er) / C_LIGHT * 1e-3 if layer == "via" else d.get(layer, 0.0))
                                  for layer, mm in lengths.items())
        res.skew_s = abs(res.delays[spec.p] - res.delays[spec.n])
        out.append(res)
        s = res.summary()
        coupled = [p for p in res.points if p[5]]
        if spec.zdiff and coupled:
            off = [p for p in coupled if abs(p[4] / spec.zdiff - 1) > spec.tol]
            if off:
                worst = max(off, key=lambda p: abs(p[4] / spec.zdiff - 1))
                add("diff_impedance", spec.p,
                    f"{spec.label}: Zdiff {s['zdiff_min']:.0f}-{s['zdiff_max']:.0f} Ω, outside {spec.zdiff:g} Ω ± "
                    f"{spec.tol * 100:g} % along {sum(p[3] for p in off):.1f} of {sum(p[3] for p in coupled):.1f} mm "
                    f"where the two run together; worst {worst[4]:.1f} Ω on {worst[2]}"
                    + (f" ({res.uncoupled_mm:.1f} mm uncoupled besides)" if res.uncoupled_mm > 0.5 else ""),
                    (worst[0], worst[1]), layer=worst[2], key=f"diff_impedance:{spec.p}", details=s)
        if res.skew_s > spec.skew_limit and res.lengths[spec.p] and res.lengths[spec.n]:
            lp, ln = (sum(res.lengths[n].values()) for n in (spec.p, spec.n))
            pad = board.tracks and next((t for t in board.tracks if t.net == spec.p), None)
            at = pad.start if pad else (0.0, 0.0)
            add("diff_skew", spec.p,
                f"{spec.label}: the nets differ by {res.skew_s * 1e12:.0f} ps ({lp:.1f} mm and {ln:.1f} mm routed), "
                f"more than {spec.skew_limit * 1e12:.0f} ps"
                + ("" if spec.max_skew is not None else f" (a tenth of its {spec.rise_time * 1e9:g} ns edge)"),
                at, layer=pad.layer if pad else "", key=f"diff_skew:{spec.p}", details=s)
    return out


def check_crosstalk(board: Board, cache: SectionCache, aggressors: list, limit: float, add: Add) -> list[dict]:
    """crosstalk (see the module): aggressors [(net, rise time s, swing V)]."""
    from shapely import STRtree
    from shapely.geometry import LineString
    from .safety import is_ground
    from .pdn import rail_voltage
    by_layer: dict[str, list[Track]] = {}
    for t in board.tracks:
        if t.net and not t.is_arc:
            by_layer.setdefault(t.layer, []).append(t)
    trees = {lay: STRtree([LineString([t.start, t.end]) for t in ts]) for lay, ts in by_layer.items()}
    out = []
    for net, tr, swing in aggressors:
        groups: dict[tuple, dict] = {}
        for a in (t for t in board.tracks if t.net == net and not t.is_arc):
            la = math.dist(a.start, a.end)
            if la < COUPLED_MIN_MM or a.layer not in trees:
                continue
            ux, uy = (a.end[0] - a.start[0]) / la, (a.end[1] - a.start[1]) / la
            reach = min(max(6 * cache.h_mm(a.layer), 0.5), 3.0)
            ts = by_layer[a.layer]
            for k in trees[a.layer].query(LineString([a.start, a.end]).buffer(reach + a.width)):
                b = ts[int(k)]
                if b.net == net or not b.net or is_ground(b.net) or rail_voltage(b.net) is not None:
                    continue
                lb = math.dist(b.start, b.end)
                if lb < COUPLED_MIN_MM:
                    continue
                vx, vy = (b.end[0] - b.start[0]) / lb, (b.end[1] - b.start[1]) / lb
                if abs(ux * vx + uy * vy) < math.cos(math.radians(PARALLEL_DEG)):
                    continue
                t0 = (b.start[0] - a.start[0]) * ux + (b.start[1] - a.start[1]) * uy
                t1 = (b.end[0] - a.start[0]) * ux + (b.end[1] - a.start[1]) * uy
                lo, hi = max(0.0, min(t0, t1)), min(la, max(t0, t1))
                if hi - lo < COUPLED_MIN_MM:
                    continue
                tm = (lo + hi) / 2
                px, py = a.start[0] + tm * ux, a.start[1] + tm * uy
                # the victim's centreline distance at the overlap's middle (its point nearest the aggressor's there)
                s = (px - b.start[0]) * vx + (py - b.start[1]) * vy
                bx, by = b.start[0] + s * vx, b.start[1] + s * vy
                gap = math.dist((px, py), (bx, by)) - (a.width + b.width) / 2
                if gap > reach:
                    continue
                g = groups.setdefault((b.net, a.layer), {"mm": 0.0, "gap": 1e9, "at": None})
                g["mm"] += hi - lo
                if gap < g["gap"]:
                    g.update(gap=gap, at=(px, py), along=(ux, uy))
        cache.prefetch([(g["at"], g["along"], layer, [net, victim], None, False)
                        for (victim, layer), g in groups.items()])
        for (victim, layer), g in groups.items():
            got = cache.solve_at(g["at"], g["along"], layer, [net, victim], precise=False)
            if got is None:
                continue
            r = got[0]
            td = g["mm"] * 1e-3 * r.delay(0)
            nxt = r.kb(1, 0) * min(1.0, 2 * td / tr)
            fxt = abs(r.kf(1, 0)) * td / tr
            row = {"aggressor": net, "victim": victim, "layer": layer, "coupled_mm": round(g["mm"], 2),
                   "gap_mm": round(max(g["gap"], 0.0), 3), "next": nxt, "fext": fxt, "kb": r.kb(1, 0),
                   "kf": r.kf(1, 0), "rise_time": tr, "swing_v": swing}
            out.append(row)
            worst = max(nxt, fxt)
            if worst <= limit:
                continue
            add("crosstalk", victim,
                f"{victim}: {net} runs beside it on {layer} for {g['mm']:.1f} mm, {max(g['gap'], 0):.2f} mm apart: "
                f"near-end crosstalk {nxt * 100:.1f} % ({nxt * swing * 1e3:.0f} mV of {swing:g} V), far-end "
                f"{fxt * 100:.1f} % ({fxt * swing * 1e3:.0f} mV) with its {tr * 1e9:g} ns edges; over "
                f"{limit * 100:g} %", g["at"], layer=layer, key=f"crosstalk:{net}:{victim}:{layer}", details=row)
    return out


def check_stubs(board: Board, nets: list, factor: float, add: Add) -> list[dict]:
    """via_stub (see the module): nets [(net, rise time s)]."""
    st = board.stackup
    out = []
    for net, tr in nets:
        tracks = [t for t in board.tracks if t.net == net]
        knee = KNEE / tr
        for v in board.vias:
            if v.net != net or len(v.layers) < 2:
                continue
            used = _via_layers(board, v, tracks)
            if len(used) < 2:
                continue
            ends = sorted(v.layers, key=board.copper_layers.index)
            top, bottom = ends[0], ends[-1]
            stub_top = st.layer(used[0]).z - st.layer(top).z
            stub_bottom = st.layer(bottom).z - st.layer(used[-1]).z
            stub = max(stub_top, stub_bottom)
            if stub < 0.05:
                continue
            a, b = (top, used[0]) if stub_top >= stub_bottom else (used[-1], bottom)
            er = st.epsilon_between(a, b)
            f_res = C_LIGHT / (4 * stub * 1e-3 * math.sqrt(er))
            row = {"net": net, "at": [round(v.position[0], 3), round(v.position[1], 3)], "stub_mm": round(stub, 3),
                   "f_res": f_res, "knee": knee, "layers": [a, b]}
            out.append(row)
            if f_res >= factor * knee:
                continue
            add("via_stub", net,
                f"{net}: the via at ({v.position[0]:.2f}, {v.position[1]:.2f}) leaves a {stub:.2f} mm stub "
                f"({a} to {b}) "
                f"resonating at {f_res / 1e9:.2f} GHz, under {factor:g} × the signal's {knee / 1e9:.2f} GHz knee: "
                "back-drill it, use a blind via or change layers nearer the via's end",
                v.position, layer=a, key=f"via_stub:{net}:{v.position[0]:.2f},{v.position[1]:.2f}", details=row)
    return out


DELAY_TOL = 0.10                # a profile's unit delay this far from the cross-sections' is an issue


def check_profile_delays(board: Board, nets: list, pairs: list, profiles: dict, by_net: dict, add: Add) -> list[dict]:
    """tuning_delay: for the nets (NetProfile) and pairs (PairResult) whose KiCad tuning profile tunes in time, each
    layer's unit delay there against the cross-sections' (the length-weighted mean of sqrt(εeff)/c, the pair's
    differential mode): KiCad turns lengths into delays with the profile's figure, so a wrong one tunes by the wrong
    amount. Rows {net, profile, layer, kicad, solved (s/m), ratio}."""
    rows = []
    items = [(n.net, [(p[2], p[3], math.sqrt(p[5]) / C_LIGHT) for p in n.points]) for n in nets]
    items += [(f"{r.spec.p} / {r.spec.n}", [(p[2], p[3], p[6]) for p in r.points if len(p) > 6 and p[5]],
               r.spec.p) for r in pairs]
    for item in items:
        label, pts = item[0], item[1]
        name = by_net.get(item[2] if len(item) > 2 else label)
        prof = profiles.get(name) if name else None
        if not prof or not prof["time_domain"]:
            continue
        for layer, entry in prof["layers"].items():
            on = [(share, d) for lay, share, d in pts if lay == layer]
            if not entry["delay"] or not on:
                continue
            solved = sum(s * d for s, d in on) / sum(s for s, _ in on)
            ratio = entry["delay"] / solved
            row = {"net": label, "profile": name, "layer": layer, "kicad_ps_cm": entry["delay"] * 1e12 / 100,
                   "solved_ps_cm": solved * 1e12 / 100, "ratio": ratio}
            rows.append(row)
            if abs(ratio - 1) > DELAY_TOL:
                t = next((t for t in board.tracks if t.net == (item[2] if len(item) > 2 else label)
                          and t.layer == layer), None)
                add("tuning_delay", item[2] if len(item) > 2 else label,
                    f"{label}: KiCad's tuning profile {name} gives {layer} {row['kicad_ps_cm']:.1f} ps/cm, the board's "
                    f"cross-sections {row['solved_ps_cm']:.1f} ps/cm (εeff {(solved * C_LIGHT) ** 2:.2f}): a length "
                    f"KiCad tunes to a delay there gives {100 / ratio:.0f} % of it; set the profile's unit "
                    f"delay to {row['solved_ps_cm']:.1f} ps/cm", t.start if t else (0.0, 0.0), layer=layer,
                    key=f"tuning_delay:{label}:{layer}", details=row)
    return rows


def run_si_checks(board: Board, config, copper, add: Add, selected: Optional[set] = None) -> SiResult:
    """The Run checks part (impedance, diff_impedance, diff_skew, crosstalk, via_stub): the listed nets, the pairs
    and the impedance targets; `selected`: only these nets (and the pairs with one of them)."""
    si = getattr(config, "si", None)
    step = getattr(si, "step_mm", 2.0) or 2.0
    project = kicad_project(board)
    profiles, by_net = kicad_profiles(board, project), profile_nets(board, project)
    targets = impedance_targets(board, config, project)
    listed = [n for n in config.nets]
    pairs = [p for p in getattr(si, "pairs", ()) if p.enabled]
    # a pair without its own Zdiff takes its nets' differential tuning profile's target
    from dataclasses import replace as _replace
    for k, p in enumerate(pairs):
        prof = profiles.get(by_net.get(p.p, "")) if by_net.get(p.p) == by_net.get(p.n) else None
        if p.zdiff is None and prof and prof["diff"] and prof["z0"]:
            pairs[k] = _replace(p, zdiff=prof["z0"], note=f"Zdiff from KiCad tuning profile {by_net[p.p]}")
    # nets of a profile's class (not KiCad's Default: that would be every net) are profiled like listed nets
    classes = kicad_netclasses(board, project) if by_net else {}
    for net, name in by_net.items():
        if net not in targets and (not selected or net in selected) and classes.get(net, "Default") != "Default":
            targets[net] = (None, 0.10, list(profiles[name]["layers"]), f"KiCad tuning profile {name}")
    if selected:
        listed = [n for n in listed if n.name in selected]
        pairs = [p for p in pairs if p.p in selected or p.n in selected]
        targets = {k: v for k, v in targets.items() if k in selected}
    from .simulate import solver_threads
    cache = SectionCache(board, copper, solver_threads(config))
    res = SiResult()
    on_board = set(board.nets)
    nets = list(dict.fromkeys([n.name for n in listed] + list(targets)))
    res.nets = check_impedance(board, cache, [n for n in nets if n in on_board], targets, step, add)
    res.pairs = check_pairs(board, cache, [p for p in pairs if p.p in on_board and p.n in on_board], step, add)
    res.crosstalk = check_crosstalk(board, cache, [(n.name, n.rise_time, n.amplitude_v) for n in listed
                                                   if n.name in on_board], getattr(si, "crosstalk_max", 0.05), add)
    stub_nets = [(n.name, n.rise_time) for n in listed] + [(net, p.rise_time) for p in pairs for net in (p.p, p.n)]
    res.stubs = check_stubs(board, [s for s in stub_nets if s[0] in on_board], getattr(si, "stub_factor", 3.0), add)
    res.delays = check_profile_delays(board, res.nets, res.pairs, profiles, by_net, add)
    res.sections = len(cache.solved)
    return res
