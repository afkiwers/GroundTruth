"""Load a .kicad_pcb file (KiCad 10+ format) into the core board model.

Used by the CLI/CI path and the test-suite, so no KiCad installation is needed.
Older formats are read on a best-effort basis with a warning; unknown tokens are ignored.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Optional

from ..core.board import (Board, CopperLayer, DielectricLayer, Footprint, Pad, Point, Polygon, Stackup,
                          Track, Via, Zone)
from ..geometry.shapes import arc_points, polygonize_outline, rotate
from .sexpr import SExprError, SList, block_end, parse

KICAD10_MIN_FILE_VERSION = 20250000  # KiCad 9 writes 20241229, KiCad 10 writes 2026xxxx
DEFAULT_CU_THICKNESS = 0.035


class BoardLoadError(ValueError):
    """The file cannot be read as a KiCad board (unreadable, not a .kicad_pcb, no copper layers)."""
    pass


def load_board(path: str | Path) -> Board:
    """Read a .kicad_pcb file into the board model (BoardLoadError when it cannot be read)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:       # missing, locked, or not a text file at all
        raise BoardLoadError(f"cannot read {path}: {e}") from e
    return loads_board(text, source=str(path))


def load_stackup(path: str | Path) -> Optional[Stackup]:
    """Only the stackup of a .kicad_pcb: parses the file header up to the end of (setup ...), so it stays cheap
    for big boards. Used by the live adapter, because KiCad's API leaves out the dielectric materials."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    i = text.find("(setup")
    j = block_end(text, i) if i >= 0 else -1
    if j < 0:
        return None
    root = parse(text[:j] + ")")
    if root.tag != "kicad_pcb":
        return None
    loader = _Loader(root, str(path))
    loader.copper_names = loader._layers()
    return loader._stackup() if loader.copper_names else None


def loads_board(text: str, source: str = "<string>") -> Board:
    """A board from the text of a .kicad_pcb file; `source` names it in messages and Board.source."""
    try:
        root = parse(text)
    except SExprError as e:                          # named, so the message says which file is broken
        raise BoardLoadError(f"{source}: not a KiCad PCB file ({e})") from e
    if root.tag != "kicad_pcb":
        raise BoardLoadError(f"{source}: not a KiCad PCB file (root is {root.tag!r})")
    return _Loader(root, source).load()


def _net_name(node: SList) -> str:
    """Handles both `(net "GND")` (KiCad 10) and `(net 3 "GND")` (KiCad <= 9)."""
    net = node.find("net")
    if net is None:
        return node.value("net_name", "") or ""
    atoms = net.atoms()
    if not atoms:
        return ""
    if len(atoms) >= 2:
        return atoms[1]
    a = atoms[0]
    if re.fullmatch(r"\d+", a):  # numeric-only: old format, name in net table / net_name
        return node.value("net_name", "") or ""
    return a


def _pts(node: Optional[SList]) -> list[Point]:
    """A (pts ...) list's points: (xy x y) points, and (arc ...) entries sampled into points (KiCad's zone fills and
    polygons may mix both). A point shared by an arc and its predecessor is kept once."""
    out: list[Point] = []
    if node is None:
        return out
    for item in node:
        if not isinstance(item, SList):
            continue
        if item.tag == "xy":
            out.append((float(item[1]), float(item[2])))
        elif item.tag == "arc":
            s, m, e = item.xy("start"), item.xy("mid"), item.xy("end")
            pts = arc_points(s[:2], m[:2], e[:2])
            if out and math.dist(out[-1], pts[0]) < 1e-9:
                pts = pts[1:]
            out.extend(pts)
    return out


class _Loader:
    """One file being read: the parsed tree, the copper layers (and their user-renamed aliases), the nets seen, and
    warnings for the board."""

    def __init__(self, root: SList, source: str):
        """A loader for the parsed file `root`."""
        self.root = root
        self.source = source
        self.warnings: list[str] = []
        self.copper_names: list[str] = []
        self.layer_alias: dict[str, str] = {}
        self.nets: set[str] = set()

    # ---------------------------------------------------------------- layers
    def _layers(self) -> list[str]:
        """The copper layers in stackup order (F.Cu, In1.Cu ... B.Cu), recording user names as aliases."""
        layers = self.root.find("layers")
        names = []
        if layers is not None:
            for item in layers:
                if isinstance(item, SList) and len(item) >= 3:
                    name = item[1]
                    kind = item[2]
                    if name.endswith(".Cu") and kind in ("signal", "power", "mixed", "jumper"):
                        names.append(name)
                    if len(item) >= 4 and isinstance(item[3], str):
                        self.layer_alias[item[3]] = name

        def order(name: str) -> int:
            """Sort key: F.Cu first, inner layers by number, B.Cu last."""
            if name == "F.Cu":
                return -1
            if name == "B.Cu":
                return 10_000
            m = re.match(r"In(\d+)\.Cu", name)
            return int(m.group(1)) if m else 5_000

        return sorted(names, key=order)

    def _expand_layers(self, atoms: list[str]) -> list[str]:
        """A (layers ...) list as copper layer names: wildcards ("*.Cu", "F&B.Cu") expanded, aliases resolved,
        non-copper layers dropped, each once."""
        out: list[str] = []
        for a in atoms:
            a = self.layer_alias.get(a, a)
            if a in ("*.Cu", "*"):
                out.extend(self.copper_names)
            elif a == "F&B.Cu":
                out.extend([n for n in ("F.Cu", "B.Cu") if n in self.copper_names])
            elif a in self.copper_names:
                out.append(a)
        return list(dict.fromkeys(out))

    def _stackup(self) -> Stackup:
        """The stackup from (setup (stackup ...)): copper thicknesses and the dielectrics (a dielectric made of
        sub-layers gets their total thickness and a thickness-weighted epsilon_r). Without a complete stackup the
        copper is spread evenly over the board's thickness (with a warning). z = each foil's centre."""
        thickness = self.root.find("general")
        board_t = thickness.float("thickness", 1.6) if thickness is not None else 1.6
        setup = self.root.find("setup")
        st = setup.find("stackup") if setup is not None else None
        cu_t: dict[str, float] = {}
        dielectrics: list[DielectricLayer] = []
        masks: dict[str, DielectricLayer] = {}
        if st is not None:
            for layer in st.find_all("layer"):
                name = layer[1] if len(layer) > 1 else ""
                kind = (layer.value("type", "") or "").lower()
                if name in ("F.Mask", "B.Mask"):
                    # KiCad writes the mask's thickness, and its epsilon_r only when it differs from 3.3
                    masks[name] = DielectricLayer(name, layer.float("thickness", 0.01), layer.float("epsilon_r", 3.3),
                                                  layer.float("loss_tangent", 0.0))
                elif name in self.copper_names:
                    cu_t[name] = layer.float("thickness", DEFAULT_CU_THICKNESS)
                elif kind in ("core", "prepreg") or name.startswith("dielectric"):
                    # Sub-layers repeat thickness/epsilon_r inside the same list.
                    ts = [float(c[1]) for c in layer.find_all("thickness")]
                    es = [float(c[1]) for c in layer.find_all("epsilon_r")]
                    lts = [float(c[1]) for c in layer.find_all("loss_tangent")]
                    total = sum(ts) if ts else 0.0
                    eps = (sum(t * e for t, e in zip(ts, es)) / total) if total and es else (es[0] if es else 4.5)
                    dielectrics.append(DielectricLayer(name, total, eps, lts[0] if lts else 0.02))
        n_cu = len(self.copper_names)
        if len(dielectrics) != max(n_cu - 1, 0):
            if st is not None:
                self.warnings.append("stackup incomplete; assuming evenly spaced copper layers")
            gap = max(board_t - n_cu * DEFAULT_CU_THICKNESS, 0.1) / max(n_cu - 1, 1)
            dielectrics = [DielectricLayer(f"dielectric {i + 1}", gap) for i in range(max(n_cu - 1, 0))]
        copper = []
        z = 0.0
        for i, name in enumerate(self.copper_names):
            t = cu_t.get(name, DEFAULT_CU_THICKNESS)
            copper.append(CopperLayer(name, i, z + t / 2, t))
            z += t
            if i < len(dielectrics):
                z += dielectrics[i].thickness
        return Stackup(copper, dielectrics, board_t, masks.get("F.Mask"), masks.get("B.Mask"))

    # ---------------------------------------------------------------- items
    def _track(self, node: SList, arc: bool) -> Optional[Track]:
        """A (segment ...) or (arc ...) on a copper layer as a Track (None on other layers)."""
        layer = self.layer_alias.get(node.value("layer", ""), node.value("layer", ""))
        if layer not in self.copper_names:
            return None
        net = _net_name(node)
        self.nets.add(net)
        start, end = node.xy("start")[:2], node.xy("end")[:2]
        mid = node.xy("mid")[:2] if arc else None
        return Track(start, end, node.float("width", 0.2), layer, net, node.value("uuid", ""), node.line, mid)

    def _via(self, node: SList) -> Via:
        """A (via ...) as a Via spanning every copper layer between its two end layers (a through via: all)."""
        at = node.xy("at")
        layers_node = node.find("layers")
        ends = self._expand_layers(layers_node.atoms()) if layers_node is not None else []
        atoms = node.atoms()
        vtype = node.value("type") or next((a for a in atoms if a in ("blind", "micro", "buried", "through")), "through")
        if len(ends) >= 2:
            i0, i1 = sorted(self.copper_names.index(n) for n in (ends[0], ends[-1]))
            span = self.copper_names[i0:i1 + 1]
        else:
            span = list(self.copper_names)
        net = _net_name(node)
        self.nets.add(net)
        return Via((at[0], at[1]), node.float("size", 0.6), node.float("drill", 0.3), span, net, vtype,
                   node.value("uuid", ""), node.line)

    def _footprint(self, node: SList) -> Footprint:
        """A (footprint ...) as a Footprint: reference, value and the symbol's other fields (properties), DNP and
        exclude-from-sim, its schematic sheet copy, its pads and its outline."""
        lib_id = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
        at = node.xy("at")
        pos = (at[0], at[1])
        angle = at[2] if len(at) > 2 else 0.0
        layer = node.value("layer", "F.Cu")
        ref, value, fields = "", "", {}
        for prop in node.find_all("property"):
            if len(prop) >= 3 and prop[1] == "Reference":
                ref = prop[2]
            elif len(prop) >= 3 and prop[1] == "Value":
                value = prop[2]
            elif len(prop) >= 3 and isinstance(prop[1], str) and isinstance(prop[2], str):
                fields[prop[1]] = prop[2]
        for text in node.find_all("fp_text"):  # pre-KiCad 8
            if len(text) >= 3 and text[1] == "reference" and not ref:
                ref = text[2]
            elif len(text) >= 3 and text[1] == "value" and not value:
                value = text[2]
        attr = node.find("attr")
        attrs = set(attr.atoms()) if attr is not None else set()
        dnp = "dnp" in attrs or node.value("dnp") == "yes"
        exclude_sim = "exclude_from_sim" in attrs or node.value("exclude_from_sim") == "yes"
        # (path "/<sheet uuid>/.../<symbol uuid>"): all but the last part name the sheet copy (KiCad 8+ also writes
        # (sheetname "Phase B") (sheetfile "phase.kicad_sch"); a reused sheet's copies differ only in the path)
        path = node.value("path", "") or ""
        fp = Footprint(ref, value, lib_id, pos, angle, layer, dnp=dnp, exclude_from_sim=exclude_sim,
                       uuid=node.value("uuid", ""), line=node.line, fields=fields,
                       sheet=path.rsplit("/", 1)[0] if "/" in path else "",
                       sheet_name=(node.value("sheetname", "") or "").strip("/").split("/")[-1],
                       sheet_file=node.value("sheetfile", "") or "")
        for pnode in node.find_all("pad"):
            pad = self._pad(pnode, fp)
            if pad is not None:
                fp.pads.append(pad)
        fp.outline = self._part_outline(node, fp)
        return fp

    def _part_outline(self, node: SList, fp: Footprint) -> list[list[Point]]:
        """The footprint's courtyard (F/B.CrtYd), else its fabrication outline (F/B.Fab), as polylines in board
        coordinates (the heatmaps' part outlines)."""
        def board(p) -> Point:
            """A footprint-local point in board coordinates (rotated with the footprint, then moved)."""
            r = rotate((p[0], p[1]), fp.angle)
            return (fp.position[0] + r[0], fp.position[1] + r[1])

        found: dict[str, list] = {"CrtYd": [], "Fab": []}
        for g in node:
            if not isinstance(g, SList) or g.tag not in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
                continue
            kind = (g.value("layer", "") or "").split(".")[-1]
            if kind not in found:
                continue
            tag = g.tag
            if tag == "fp_line":
                pts = [g.xy("start"), g.xy("end")]
            elif tag == "fp_rect":
                (x0, y0), (x1, y1) = g.xy("start"), g.xy("end")
                pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            elif tag == "fp_poly":
                pts = _pts(g.find("pts"))
                pts = pts + pts[:1]
            elif tag == "fp_circle":
                (cx, cy), (ex, ey) = g.xy("center"), g.xy("end")
                r = math.hypot(ex - cx, ey - cy)
                pts = [(cx + r * math.cos(a * math.pi / 16), cy + r * math.sin(a * math.pi / 16)) for a in range(33)]
            else:
                pts = arc_points(g.xy("start"), g.xy("mid"), g.xy("end"))
            if len(pts) >= 2:
                found[kind].append([board(p) for p in pts])
        return found["CrtYd"] or found["Fab"]

    def _pad(self, node: SList, fp: Footprint) -> Optional[Pad]:
        """A (pad ...) as a Pad in board coordinates (its local position turned with the footprint), with its
        schematic pin type and name. None for a pad with no copper layer or a non-plated hole."""
        if len(node) < 4:
            return None
        number, ptype, shape = node[1], node[2], node[3]
        layers_node = node.find("layers")
        layers = self._expand_layers(layers_node.atoms()) if layers_node is not None else []
        if not layers or ptype == "np_thru_hole":
            return None
        at = node.xy("at")
        local = (at[0], at[1])
        angle = at[2] if len(at) > 2 else fp.angle
        r = rotate(local, fp.angle)
        pos = (fp.position[0] + r[0], fp.position[1] + r[1])
        size = node.xy("size", (0.0, 0.0))
        if len(size) == 1:
            size = (size[0], size[0])
        drill_node = node.find("drill")
        drill = 0.0
        if drill_node is not None:
            nums = [a for a in drill_node.atoms() if re.fullmatch(r"-?[\d.]+", a)]
            drill = float(nums[0]) if nums else 0.0
        polygon = None
        if shape == "custom":
            polygon = self._custom_pad(node, pos, angle, size)
        net = _net_name(node)
        if net:
            self.nets.add(net)
        pin_type = (node.value("pintype", "") or "").split("+")[0]   # "input+no_connect" -> "input"
        pin_function = re.sub(r"_\d+$", "", node.value("pinfunction", "") or "")  # "PA5_12" -> "PA5"
        return Pad(fp.reference, str(number), pos, angle, (size[0], size[1]), shape, layers, net, ptype, drill,
                   polygon, node.float("roundrect_rratio", 0.0), node.value("uuid", ""), pin_type, pin_function)

    def _custom_pad(self, node: SList, pos: Point, angle: float, size) -> Optional[Polygon]:
        """A custom pad's outline: its anchor rectangle united with its polygon primitives (widened by their
        stroke), turned and moved into place. More than one piece is approximated by the convex hull."""
        from shapely.geometry import Polygon as SPolygon, box
        from shapely.ops import unary_union
        prims = node.find("primitives")
        shapes = [box(-size[0] / 2, -size[1] / 2, size[0] / 2, size[1] / 2)]
        if prims is not None:
            for prim in prims.find_all("gr_poly"):
                pts = _pts(prim.find("pts"))
                if len(pts) >= 3:
                    g = SPolygon(pts)
                    w = prim.float("width", 0.0)
                    shapes.append(g.buffer(w / 2) if w > 0 else g)
        u = unary_union(shapes)
        if u.geom_type != "Polygon":
            u = u.convex_hull
        ring = [rotate(p, angle) for p in list(u.exterior.coords)[:-1]]
        return ([(pos[0] + x, pos[1] + y) for x, y in ring], [])

    def _zone(self, node: SList) -> Zone:
        """A (zone ...) as a Zone: its net, layers, outline, whether it is a keep-out, and its filled polygons per
        layer (what the analysis uses; an unfilled zone has none)."""
        layers_node = node.find("layers")
        if layers_node is not None:
            layers = self._expand_layers(layers_node.atoms())
        else:
            layers = self._expand_layers([node.value("layer", "")])
        net = _net_name(node)
        if net:
            self.nets.add(net)
        is_rule = node.find("keepout") is not None
        outline_node = node.find("polygon")
        outline = None
        if outline_node is not None:
            pts = _pts(outline_node.find("pts"))
            if len(pts) >= 3:
                outline = (pts, [])
        zone = Zone(net, layers, {}, outline, is_rule, node.value("name", "") or "", node.value("uuid", ""),
                    node.line)
        for fill in node.find_all("filled_polygon"):
            layer = self.layer_alias.get(fill.value("layer", ""), fill.value("layer", ""))
            pts = _pts(fill.find("pts"))
            if layer in self.copper_names and len(pts) >= 3:
                zone.filled.setdefault(layer, []).append((pts, []))
        return zone

    def _outline(self) -> list[Polygon]:
        """The board outline from every Edge.Cuts item, the board's own and those inside footprints (turned and
        moved with them): loose lines and arcs are joined into polygons, rectangles, circles and polygons are
        closed already; inner contours become cut-outs (polygonize_outline)."""
        segments: list[list[Point]] = []
        closed: list[Polygon] = []

        def handle(item: SList, tf=None):
            """Collect one graphic item if it is on Edge.Cuts (tf: the footprint's local-to-board transform)."""
            layer = self.layer_alias.get(item.value("layer", ""), item.value("layer", ""))
            if layer != "Edge.Cuts":
                return
            t = tf or (lambda p: p)
            tag = item.tag.split("_", 1)[-1]
            if tag == "line":
                segments.append([t(item.xy("start")[:2]), t(item.xy("end")[:2])])
            elif tag == "arc":
                pts = arc_points(t(item.xy("start")[:2]), t(item.xy("mid")[:2]), t(item.xy("end")[:2]))
                segments.append(pts)
            elif tag == "rect":
                (x0, y0), (x1, y1) = item.xy("start")[:2], item.xy("end")[:2]
                closed.append(([t((x0, y0)), t((x1, y0)), t((x1, y1)), t((x0, y1))], []))
            elif tag == "circle":
                c, e = item.xy("center")[:2], item.xy("end")[:2]
                r = math.dist(c, e)
                closed.append(([t((c[0] + r * math.cos(a * math.pi / 32), c[1] + r * math.sin(a * math.pi / 32)))
                                for a in range(64)], []))
            elif tag == "poly":
                pts = [t(p) for p in _pts(item.find("pts"))]
                if len(pts) >= 3:
                    closed.append((pts, []))

        for item in self.root:
            if isinstance(item, SList) and item.tag.startswith("gr_"):
                handle(item)
        for fp in self.root.find_all("footprint"):
            at = fp.xy("at")
            pos, ang = (at[0], at[1]), (at[2] if len(at) > 2 else 0.0)

            def tf(p, pos=pos, ang=ang):
                """A footprint-local point in board coordinates."""
                r = rotate(p, ang)
                return (pos[0] + r[0], pos[1] + r[1])

            for item in fp:
                if isinstance(item, SList) and item.tag.startswith("fp_"):
                    handle(item, tf)
        return polygonize_outline(segments, closed)

    # ---------------------------------------------------------------- main
    def load(self) -> Board:
        """The whole board: layers and stackup, then every top-level item (tracks, arcs, vias, footprints, zones),
        the outline, the nets, and warnings (an old file format, no outline, unfilled zones)."""
        version = self.root.value("version", "") or ""
        gen_version = self.root.value("generator_version", "") or ""
        if version.isdigit() and int(version) < KICAD10_MIN_FILE_VERSION:
            self.warnings.append(f"file format {version} predates KiCad 10; reading on a best-effort basis")
        self.copper_names = self._layers()
        if not self.copper_names:
            raise BoardLoadError(f"{self.source}: no copper layers found")
        stackup = self._stackup()
        board = Board(self.source, stackup, file_version=version, generator_version=gen_version)
        for item in self.root:
            if not isinstance(item, SList):
                continue
            tag = item.tag
            if tag == "segment":
                t = self._track(item, arc=False)
                if t:
                    board.tracks.append(t)
            elif tag == "arc":
                t = self._track(item, arc=True)
                if t:
                    board.tracks.append(t)
            elif tag == "via":
                board.vias.append(self._via(item))
            elif tag == "footprint":
                board.footprints.append(self._footprint(item))
            elif tag == "zone":
                board.zones.append(self._zone(item))
            elif tag == "net" and len(item) >= 3:  # old net table
                self.nets.add(item[2])
        board.outline = self._outline()
        if not board.outline:
            self.warnings.append("no closed board outline on Edge.Cuts; using copper extents")
        self.nets.discard("")
        board.nets = sorted(self.nets)
        unfilled = board.unfilled_zones()
        if unfilled:
            self.warnings.append(
                f"{len(unfilled)} copper zone(s) have no fill (e.g. net {unfilled[0].net!r}); "
                "run 'Fill All Zones' in KiCad and save, or use --refill-zones")
        board.warnings = self.warnings
        return board
