"""Read the board open in KiCad (10+) over the IPC API into the core board model.

All kicad-python (kipy) calls are confined to this module and `kipy_sink.py`, so API changes in
future KiCad versions only touch these two files.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Callable, Optional, TypeVar

from ..core.board import (Board, CopperLayer, DielectricLayer, Footprint, Pad, Point, Polygon, Stackup, Track,
                          Via, Zone)
from ..geometry.shapes import arc_points, polygonize_outline

MIN_KICAD_MAJOR = 10
NM = 1e-6  # nm -> mm
BUSY_WAIT_S = 5.0   # how long to keep retrying while KiCad answers "busy"

T = TypeVar("T")


# kiapi.board.types.ElectricalPinType -> the strings used in .kicad_pcb files
_PIN_TYPES = {1: "input", 2: "output", 3: "bidirectional", 4: "tri_state", 5: "passive", 6: "free",
              7: "unspecified", 8: "power_in", 9: "power_out", 10: "open_collector", 11: "open_emitter",
              12: "no_connect"}


class KiCadConnectionError(RuntimeError):
    """KiCad's API cannot be reached (no kipy, the API server off, an old KiCad): the message tells the user what
    to do."""
    pass


class KiCadBusyError(RuntimeError):
    """KiCad refuses API requests while the editor shows a modal dialog or runs an interactive tool."""


def when_ready(fn: Callable[..., T], *args, wait: float = BUSY_WAIT_S, **kwargs) -> T:
    """Call fn, retrying while KiCad answers 'busy' (AS_BUSY). A modal dialog (e.g. Preferences) or an
    interactive tool in the PCB editor makes every board request fail until it is closed. fn must be safe to
    repeat: the sink's commits are dropped when a call inside them fails."""
    from kipy.errors import ApiError
    from kipy.proto.common import ApiStatusCode
    deadline = time.monotonic() + wait
    delay = 0.2
    while True:
        try:
            return fn(*args, **kwargs)
        except ApiError as e:
            if e.code != ApiStatusCode.AS_BUSY:
                raise
            if time.monotonic() >= deadline:
                raise KiCadBusyError("KiCad is busy and does not accept requests: close any open dialog in the "
                                     "PCB editor (e.g. Preferences) and finish the current tool (Esc), then try "
                                     "again.") from e
            time.sleep(delay)
            delay = min(2 * delay, 1.0)


def connect():
    """Connect to the running KiCad (env KICAD_API_SOCKET / KICAD_API_TOKEN set by KiCad)."""
    try:
        from kipy import KiCad
    except ImportError as e:
        raise KiCadConnectionError("kicad-python (kipy) is not installed in this environment") from e
    try:
        kicad = KiCad(client_name="groundtruth")
        version = kicad.get_version()
    except Exception as e:  # kipy raises various connection errors
        raise KiCadConnectionError(
            "cannot reach KiCad's API server. Enable it in KiCad: Preferences > Plugins > "
            f"'Enable KiCad API', then restart KiCad. ({e})") from e
    if version.major < MIN_KICAD_MAJOR:
        raise KiCadConnectionError(f"KiCad {version.major}.{version.minor} found; groundtruth needs KiCad "
                                   f"{MIN_KICAD_MAJOR} or newer")
    return kicad


def pin_name(name: str) -> str:
    """A pad's symbol pin name as the file reader gives it: KiCad appends the pin number ("G_1", "PA5_12"), the
    rules match the name ("G", "PA5"). Found live: Q601's gate came through the API as "G_1", so the surge
    stopper's FET was not recognised and no input connector was found in the plugin (the CLI found it)."""
    return re.sub(r"_\d+$", "", name or "")


def footprint_fields(fpi) -> tuple[dict[str, str], str, str, str]:
    """(the symbol's own fields, its sheet copy's key, that sheet's name and file) of a kipy FootprintInstance, as
    the file parser reads them: the fields are the definition's Field items (Reference / Value / Datasheet /
    Description are separate), the sheet copy is the symbol path without the symbol itself."""
    from kipy.board_types import Field
    fields = {item.name: item.text.value for item in fpi.definition.items if isinstance(item, Field) and item.name}
    path = [k.value for k in fpi.sheet_path.path]
    return (fields, "/" + "/".join(path[:-1]) if len(path) > 1 else "",
            (fpi.proto.symbol_sheet_name or "").strip("/").split("/")[-1], fpi.proto.symbol_sheet_filename or "")


def _mm(nm: int) -> float:
    """KiCad's integer nanometres as mm."""
    return round(nm * NM, 6)   # exact to the nanometre, no float noise


def _pt(v) -> Point:
    """A KiCad vector as a board point (mm)."""
    return (_mm(v.x), _mm(v.y))


def _polyline(pl) -> list[Point]:
    """A KiCad polyline as points (its arc nodes sampled; a point shared with the previous one kept once)."""
    pts: list[Point] = []
    for node in pl.nodes:
        if node.has_arc:
            a = node.arc
            seg = arc_points(_pt(a.start), _pt(a.mid), _pt(a.end))
            if pts and math.dist(pts[-1], seg[0]) < 1e-9:
                seg = seg[1:]
            pts.extend(seg)
        else:
            pts.append(_pt(node.point))
    return pts


def _polygon(pwh) -> Polygon:
    """A KiCad polygon with holes as a board Polygon."""
    return (_polyline(pwh.outline), [_polyline(h) for h in pwh.holes])


class _KipyReader:
    """Reads the open board over KiCad's API into the board model, the same model the file reader makes (so the
    analysis cannot tell where a board came from)."""

    def __init__(self, board: Any):
        """A reader for kipy's Board; layer names are looked up once and cached."""
        from kipy.util.board_layer import canonical_name, is_copper_layer
        self.kb = board
        self._canonical = canonical_name
        self._is_copper = is_copper_layer
        self.names: dict[int, str] = {}
        self.warnings: list[str] = []

    def layer_name(self, layer: int) -> str:
        """A KiCad layer id's canonical name ("F.Cu", "In1.Cu" ...)."""
        if layer not in self.names:
            self.names[layer] = self._canonical(layer)
        return self.names[layer]

    def stackup(self) -> Stackup:
        """Copper and dielectric thicknesses from KiCad.

        KiCad 10.0.5 reports every stackup item as disabled and sends dielectrics without their materials, so
        `enabled` is ignored for copper and dielectric items, and εr / tan δ come from the saved board file when
        KiCad leaves them out (thicknesses always come from KiCad: they include unsaved edits)."""
        from kipy.proto.board import board_pb2
        st = self.kb.get_stackup()
        copper: list[CopperLayer] = []
        diel: list[DielectricLayer] = []
        unknown: list[int] = []                 # dielectrics whose εr / tan δ KiCad did not send
        z = 0.0
        pending: list[tuple] = []               # (name, thickness, εr or None, tan δ or None) between two coppers

        def close_gap():
            """The dielectric between the previous copper layer and this one: its sub-layers' total thickness
            and thickness-weighted epsilon_r (those KiCad sent materials for); missing materials noted."""
            t = sum(x[1] for x in pending)
            known = [x for x in pending if x[2] is not None]
            if len(known) < len(pending):
                unknown.append(len(diel))
            eps = (sum(x[1] * x[2] for x in known) / sum(x[1] for x in known)) if known and t else 4.5
            lt = known[0][3] if known and known[0][3] is not None else 0.02
            diel.append(DielectricLayer(pending[0][0], t, eps or 4.5, lt))

        for layer in st.layers:
            if layer.type == board_pb2.BoardStackupLayerType.BSLT_COPPER:
                if copper and pending:
                    close_gap()
                pending = []
                t_cu = _mm(layer.thickness) or 0.035
                copper.append(CopperLayer(self.layer_name(layer.layer), len(copper), z + t_cu / 2, t_cu))
                z += t_cu
            elif layer.type == board_pb2.BoardStackupLayerType.BSLT_DIELECTRIC and copper:
                d = layer.dielectric
                subs = list(d.layers) if d is not None else []
                eps = subs[0].epsilon_r if subs else None
                lt = subs[0].loss_tangent if subs else None
                pending.append((getattr(layer, "user_name", "") or "dielectric", _mm(layer.thickness), eps, lt))
                z += _mm(layer.thickness)
        if not copper:  # stackup not defined: evenly spaced default
            n = self.kb.get_copper_layer_count()
            names = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n - 1)] + ["B.Cu"]
            gap = (1.6 - 0.035 * n) / max(n - 1, 1)
            copper = [CopperLayer(nm, i, i * (gap + 0.035) + 0.0175) for i, nm in enumerate(names)]
            diel = [DielectricLayer(f"dielectric {i + 1}", gap) for i in range(n - 1)]
            self.warnings.append("KiCad sent no stackup; assuming evenly spaced copper layers")
        elif unknown:
            self._materials_from_file(copper, diel, unknown)
        return Stackup(copper, diel, z or 1.6, *getattr(self, "masks", (None, None)))

    def _materials_from_file(self, copper: list[CopperLayer], diel: list[DielectricLayer], unknown: list[int]):
        """Fill in the dielectric materials KiCad left out from the saved board file, when its stackup matches
        the open board's layer for layer; else assume FR-4 and say so."""
        from .file_source import load_stackup
        path = board_file_path(self.kb)
        saved = load_stackup(path) if path else None
        if (saved is not None and [c.name for c in saved.copper] == [c.name for c in copper]
                and len(saved.dielectrics) == len(diel)):
            for k in unknown:
                diel[k].epsilon_r = saved.dielectrics[k].epsilon_r
                diel[k].loss_tangent = saved.dielectrics[k].loss_tangent
                diel[k].name = saved.dielectrics[k].name
            self.masks = (saved.mask_top, saved.mask_bottom)       # the solder masks, from the same file
            return
        self.warnings.append("KiCad did not send the dielectric materials and the saved board file does not match "
                             "the open board: assuming FR4 (εr 4.5, tan δ 0.02); save the board and reload")

    def read(self) -> Board:
        """The whole board: stackup, tracks, vias, footprints with their pads (exact pad shapes asked per layer),
        zones with their fills, the outline from the Edge.Cuts shapes, and the nets."""
        kb = self.kb
        stackup = self.stackup()
        names = stackup.names()
        order = {n: i for i, n in enumerate(names)}
        board = Board(f"kicad-ipc:{kb.name}", stackup, generator_version="ipc")
        board.warnings.extend(self.warnings)
        nets: set[str] = set()

        for t in kb.get_tracks():
            layer = self.layer_name(t.layer)
            if layer not in order:
                continue
            mid = _pt(t.mid) if hasattr(t, "mid") else None
            board.tracks.append(Track(_pt(t.start), _pt(t.end), _mm(t.width), layer, t.net.name,
                                      t.id.value, 0, mid))
            nets.add(t.net.name)

        from kipy.proto.board.board_types_pb2 import ViaType
        for v in kb.get_vias():
            ps = v.padstack
            ends = [self.layer_name(ps.drill.start_layer), self.layer_name(ps.drill.end_layer)]
            ends = [e for e in ends if e in order]
            if len(ends) == 2:
                i0, i1 = sorted(order[e] for e in ends)
                span = names[i0:i1 + 1]
            else:
                span = list(names)
            vtype = {ViaType.VT_BLIND_BURIED: "blind", ViaType.VT_BLIND: "blind", ViaType.VT_BURIED: "buried",
                     ViaType.VT_MICRO: "micro"}.get(v.type, "through")
            try:
                size = _mm(v.diameter)
            except Exception:
                size = 0.6
            board.vias.append(Via(_pt(v.position), size, _mm(v.drill_diameter), span, v.net.name, vtype, v.id.value))
            nets.add(v.net.name)

        from kipy.proto.board.board_types_pb2 import PadType
        all_pads = []
        for fpi in kb.get_footprints():
            ref = fpi.reference_field.text.value
            value = fpi.value_field.text.value
            lib = fpi.definition.id
            lib_id = f"{lib.library}:{lib.name}" if lib.library else lib.name
            attrs = fpi.attributes
            fields, sheet, sheet_name, sheet_file = footprint_fields(fpi)
            fp = Footprint(ref, value, lib_id, _pt(fpi.position), fpi.orientation.degrees,
                           self.layer_name(fpi.layer), dnp=bool(attrs.do_not_populate),
                           exclude_from_sim=bool(getattr(attrs, "exclude_from_simulation", False)),
                           uuid=fpi.id.value, fields=fields, sheet=sheet, sheet_name=sheet_name,
                           sheet_file=sheet_file)
            for p in fpi.definition.pads:
                if p.pad_type == PadType.PT_NPTH:
                    continue
                layers = [self.layer_name(l) for l in p.padstack.layers if self.layer_name(l) in order]
                if not layers:
                    continue
                cl = p.padstack.copper_layers
                size = _pt(cl[0].size) if cl else (0.5, 0.5)
                sym = p.proto.symbol_pin
                pad = Pad(ref, p.number, _pt(p.position), p.padstack.angle.degrees, size, "rect", layers,
                          p.net.name, "thru_hole" if p.pad_type == PadType.PT_PTH else "smd",
                          p.padstack.drill.diameter.x * NM, uuid=p.id.value,
                          pin_type=_PIN_TYPES.get(sym.type, ""), pin_function=pin_name(sym.name))
                fp.pads.append(pad)
                all_pads.append((pad, p))
                nets.add(p.net.name)
            board.footprints.append(fp)

        # Exact pad copper per layer (one request per layer)
        by_layer: dict[str, list] = {}
        for pad, kp in all_pads:
            by_layer.setdefault(pad.layers[0], []).append((pad, kp))
        from kipy.util.board_layer import layer_from_canonical_name
        for layer, items in by_layer.items():
            try:
                polys = kb.get_pad_shapes_as_polygons([kp for _, kp in items], layer_from_canonical_name(layer))
            except Exception:
                continue
            for (pad, _), poly in zip(items, polys):
                if poly is not None:
                    pad.polygon = _polygon(poly)

        from kipy.proto.board.board_types_pb2 import ZoneType
        for z in kb.get_zones():
            if z.type not in (ZoneType.ZT_COPPER, ZoneType.ZT_RULE_AREA):
                continue
            net = z.net.name if z.net is not None else ""
            zone = Zone(net, [self.layer_name(l) for l in z.layers if self.layer_name(l) in order],
                        is_rule_area=z.type == ZoneType.ZT_RULE_AREA, name=z.name, uuid=z.id.value)
            for layer, polys in z.filled_polygons.items():
                lname = self.layer_name(layer)
                if lname in order:
                    zone.filled[lname] = [_polygon(p) for p in polys]
            board.zones.append(zone)
            nets.add(net)

        segments, closed = [], []
        from kipy.board_types import BoardArc, BoardCircle, BoardPolygon, BoardRectangle, BoardSegment
        from kipy.proto.board.board_types_pb2 import BoardLayer
        for s in kb.get_shapes():
            if s.layer != BoardLayer.BL_Edge_Cuts:
                continue
            if isinstance(s, BoardSegment):
                segments.append([_pt(s.start), _pt(s.end)])
            elif isinstance(s, BoardArc):
                segments.append(arc_points(_pt(s.start), _pt(s.mid), _pt(s.end)))
            elif isinstance(s, BoardRectangle):
                (x0, y0), (x1, y1) = _pt(s.top_left), _pt(s.bottom_right)
                closed.append(([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], []))
            elif isinstance(s, BoardCircle):
                c = _pt(s.center)
                r = math.dist(c, _pt(s.radius_point))
                closed.append(([(c[0] + r * math.cos(a * math.pi / 32), c[1] + r * math.sin(a * math.pi / 32))
                                for a in range(64)], []))
            elif isinstance(s, BoardPolygon):
                closed.extend(_polygon(p) for p in s.polygons)
        board.outline = polygonize_outline(segments, closed)
        nets.discard("")
        board.nets = sorted(nets)
        unfilled = board.unfilled_zones()
        if unfilled:
            board.warnings.append(f"{len(unfilled)} zone(s) are not filled; use 'Refill zones' first")
        return board


def read_board(kicad_board: Any) -> Board:
    """The open board in the model, retried while KiCad is busy (a dialog or tool open: see when_ready)."""
    return when_ready(_read_board, kicad_board)


def _read_board(kicad_board: Any) -> Board:
    """kipy Board -> core Board."""
    return _KipyReader(kicad_board).read()


def board_text(kicad_board: Any) -> str:
    """The open board in KiCad's file format, edits not saved yet included (kipy get_as_string: a read-only
    SaveDocumentToString request; the simulation history keeps it as the run's board copy)."""
    return when_ready(kicad_board.get_as_string)


def board_file_path(kicad_board: Any) -> Optional[str]:
    """Absolute path of the open .kicad_pcb, used to locate the settings next to it."""
    import os
    try:
        project = when_ready(kicad_board.get_project)
        folder = project.path
    except KiCadBusyError:
        raise
    except Exception:
        folder = None
    name = kicad_board.name
    if folder:
        return os.path.join(folder, name)
    return name if os.path.isabs(name) else None


def selected_nets(kicad_board: Any) -> list[str]:
    """Nets of the items selected in the PCB editor (tracks, arcs, vias, pads, zones, copper shapes), in
    selection order without duplicates. A right-click in KiCad selects the item under the cursor too."""
    nets: list[str] = []
    for item in when_ready(kicad_board.get_selection):
        try:
            net = item.net
        except AttributeError:          # footprints, text, ... carry no single net
            continue
        name = getattr(net, "name", "") if net is not None else ""
        if name and name not in nets:
            nets.append(name)
    return nets


def selected_pad_ids(kicad_board: Any) -> list[str]:
    """The ids (UUIDs) of the pads selected in the PCB editor, in selection order: a selected pad names the pin a
    path picked in KiCad starts or ends at (analysis/selection.py; the board model's pads carry the same uuid)."""
    out: list[str] = []
    for item in when_ready(kicad_board.get_selection):
        if type(item).__name__ == "Pad":
            uid = str(item.id.value)
            if uid and uid not in out:
                out.append(uid)
    return out


def selected_references(kicad_board: Any, wait: float = 0.0) -> list[str]:
    """References of the footprints selected in the PCB editor, in selection order. `wait`: how long to retry
    while KiCad is busy (a dialog or an interactive tool), then KiCadBusyError; the Parts tab's polling passes 0,
    so a busy KiCad only skips one look."""
    refs: list[str] = []
    for item in when_ready(kicad_board.get_selection, wait=wait):
        field = getattr(item, "reference_field", None)
        ref = field.text.value if field is not None else ""
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def selection_kinds(kicad_board: Any) -> list[str]:
    """The types of the items selected in the PCB editor (e.g. ['Zone', 'FootprintInstance']), as the API sees
    them: says why selected_nets found none."""
    return [type(item).__name__ for item in when_ready(kicad_board.get_selection)]
