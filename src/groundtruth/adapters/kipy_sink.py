"""Write analysis results onto the open board (User layers), clear them, show an issue's copper (selection, net
highlight and zoom, which change nothing in the design).

Every item groundtruth creates goes into a group whose name starts with "groundtruth", in a single commit
(one undo step). Clearing removes exactly those groups' items, never anything the user drew.

Every public function retries while KiCad answers "busy" (a modal dialog or an interactive tool in the
editor) and then raises KiCadBusyError with a message for the user; a failed commit is dropped, so a retry
starts from an unchanged board.
"""
from __future__ import annotations

import functools
import math
import time
from typing import Any, Iterable, Optional, Union

from ..core.issues import Issue
from .kipy_source import KiCadBusyError, when_ready


def _when_ready(fn):
    """Every public function here takes wait= (seconds to retry while KiCad is busy); when_ready uses it up, so
    the function never sees it. Never give one a parameter of its own called wait."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        """fn, retried while KiCad answers busy (see kipy_source.when_ready)."""
        return when_ready(fn, *args, **kwargs)
    return wrapper

GROUP_PREFIX = "groundtruth"
MM = 1_000_000  # mm -> nm


def _v(x: float, y: float):
    """A board point in mm as KiCad's vector (integer nanometres)."""
    from kipy.geometry import Vector2
    return Vector2.from_xy(int(round(x * MM)), int(round(y * MM)))


class DrawingLayerError(RuntimeError):
    """The drawing layer could not be enabled (an expected condition: said plainly, no traceback)."""


ENABLED: list[str] = []      # drawing layers enabled on the board by this process (the window reports them)


def _drawing_layer(board: Any, name: str):
    """The layer to draw on, enabled on the board first when it is not (the user: "when not enabled, enable
    automatically"). KiCad 10 would create shapes on a disabled layer but refuse to group them: invisible, outside
    'Clear GT markers', removable only by Undo (seen on refboard, which enables User.1 to User.4 only).

    set_enabled_layers DELETES the content of every layer left out of the new list, and cannot be undone: the call
    keeps the copper count and passes every layer enabled now plus the new one, and the result is checked (a
    layer missing afterwards stops the drawing, with the names)."""
    layer = _layer(name)
    enabled = list(board.get_enabled_layers())
    if layer in enabled:
        return layer
    from kipy.util.board_layer import canonical_name, is_copper_layer
    copper = [l for l in enabled if is_copper_layer(l)]
    other = [l for l in enabled if not is_copper_layer(l)]
    now = list(board.set_enabled_layers(len(copper), other + [layer]))
    lost = [canonical_name(l) for l in enabled if l not in now]
    if lost or layer not in now:
        raise DrawingLayerError(
            f"could not enable {name} on the board for drawing" + (f" (KiCad no longer lists {', '.join(lost)})"
                                                                    if lost else "") +
            f"; nothing was drawn. Enable {name} in KiCad (File > Board Setup > Board Editor Layers) and try again.")
    ENABLED.append(name)
    return layer


def take_enabled() -> list[str]:
    """The drawing layers enabled since the last call (for the window's status line)."""
    out = list(ENABLED)
    ENABLED.clear()
    return out


def _layer(name: str):
    """KiCad's layer id for a canonical layer name ("User.9"); ValueError for an unknown one."""
    from kipy.util.board_layer import layer_from_canonical_name
    layer = layer_from_canonical_name(name)
    from kipy.proto.board.board_types_pb2 import BoardLayer
    if layer == BoardLayer.BL_UNKNOWN:
        raise ValueError(f"unknown layer {name!r}")
    return layer


def _circle(center, radius_mm: float, layer, width_mm: float = 0.15):
    """A circle outline (an issue marker) at center (mm) on `layer`."""
    from kipy.board_types import BoardCircle
    c = BoardCircle()
    c.layer = layer
    c.center = _v(*center)
    c.radius_point = _v(center[0] + radius_mm, center[1])
    c.attributes.stroke.width = int(width_mm * MM)
    return c


def _text(pos, text: str, layer, size_mm: float = 0.6):
    """A label whose left end sits at pos (KiCad centres text on its position by default, which put labels
    on top of the markers they belong to)."""
    from kipy.board_types import BoardText
    from kipy.proto.common.types import HorizontalAlignment, VerticalAlignment
    t = BoardText()
    t.layer = layer
    t.position = _v(*pos)
    t.value = text
    t.attributes.size = _v(size_mm, size_mm)
    t.attributes.stroke_width = int(size_mm * 0.15 * MM)
    t.attributes.horizontal_alignment = HorizontalAlignment.HA_LEFT
    t.attributes.vertical_alignment = VerticalAlignment.VA_CENTER
    return t


def _polygon(rings: list[list[tuple[float, float]]], layer, filled: bool = False, width_mm: float = 0.05):
    """A graphic polygon with holes on `layer`: rings[0] the outline, the others holes (a suggested fill)."""
    from kipy.board_types import BoardPolygon
    from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode
    poly = BoardPolygon()
    poly.layer = layer
    pwh = PolygonWithHoles()
    outline = PolyLine()
    for x, y in rings[0]:
        outline.append(PolyLineNode.from_point(_v(x, y)))
    outline.closed = True
    pwh.outline = outline
    for hole in rings[1:]:
        h = PolyLine()
        for x, y in hole:
            h.append(PolyLineNode.from_point(_v(x, y)))
        h.closed = True
        pwh.add_hole(h)
    poly.polygons.append(pwh)
    poly.attributes.stroke.width = int(width_mm * MM)
    poly.attributes.fill.filled = filled
    return poly


def _commit_group(board: Any, items: list, name: str, message: str) -> int:
    """Create the items, then group them under `name` so 'Clear GT markers' finds them.

    KiCad 10 only groups items that are already on the board: a group created in the same commit as its items
    is rejected as empty ("no overlapping layers"), and kipy does not report per-item failures. So the items
    are one commit and the group a second one (undo removes the grouping first, then the items), and the
    group is checked afterwards."""
    if not items:
        return 0
    from kipy.board_types import Group
    commit = board.begin_commit()
    try:
        created = board.create_items(items)
        board.push_commit(commit, message)
    except Exception:
        board.drop_commit(commit)
        raise

    def group_them():
        """Group the created items in a second commit (see above)."""
        group = Group()
        group.proto.name = name
        group.items = created
        c = board.begin_commit()
        try:
            board.create_items(group)
            board.push_commit(c, f"{message} (group)")
        except Exception:
            board.drop_commit(c)
            raise
    when_ready(group_them)
    if not any(g.name == name for g in board.get_groups()):
        raise RuntimeError(f"KiCad drew {len(created)} item(s) but did not group them as {name!r}, so "
                           "'Clear GT markers' cannot remove them; use Undo (Ctrl+Z) instead")
    return len(created)


REFILL_WAIT_S = 600.0       # a refill may keep KiCad busy this long (a complex board's zones: minutes)
REFILL_POLL_S = 0.5


def refill_zones(board: Any, progress=None, wait: float = REFILL_WAIT_S) -> None:
    """Refill all zones and wait until KiCad has finished: until a board request is answered again.

    kipy's own wait (refill_zones(block=True)) pings KiCad and stops at the first ping answered, which can be while
    the fill still runs: the board read after it then met AS_BUSY for longer than when_ready's few seconds and told
    the user to close a dialog (the user, on a complex board: "pressing refill zones causes this error"). Here the
    fill is started without that wait (itself retried while a tool or dialog holds KiCad), then a cheap board
    request is retried while KiCad answers busy or the transport times out (KiCad's event loop fills), for up to
    `wait` seconds, saying how long through progress(text, fraction)."""
    from kipy.errors import ApiError
    from kipy.proto.common import ApiStatusCode
    when_ready(board.refill_zones, block=False)
    start = time.monotonic()
    said = 0.0
    while True:
        time.sleep(REFILL_POLL_S)
        try:
            board.get_copper_layer_count()
            return
        except ApiError as e:
            if e.code != ApiStatusCode.AS_BUSY:
                raise
        except (IOError, TimeoutError):          # the transport timed out: KiCad's event loop is filling
            pass
        spent = time.monotonic() - start
        if spent >= wait:
            raise KiCadBusyError(f"KiCad is still filling the zones after {wait / 60:g} min: wait until it has "
                                 "finished (its status bar), then press Reload board.")
        if progress is not None and spent - said >= 2.0:
            said = spent
            progress(f"KiCad is filling the zones… {spent:.0f} s (a complex board takes minutes)",
                     min(spent / wait, 0.99))


@_when_ready
def clear(board: Any, prefix: str = GROUP_PREFIX) -> int:
    """Remove every item in groundtruth-created groups (one undo step). Returns number of items removed."""
    groups = [g for g in board.get_groups() if g.name.startswith(prefix)]
    if not groups:
        return 0
    ids = [i for g in groups for i in g.proto.items]
    commit = board.begin_commit()
    try:
        if ids:
            board.remove_items_by_id(ids)
        board.remove_items(groups)
        board.push_commit(commit, "groundtruth: clear drawings")
    except Exception:
        board.drop_commit(commit)
        raise
    return len(ids)


@_when_ready
def draw_issues(board: Any, issues: Iterable[Issue], layer_name: str = "User.9", hidden=frozenset()) -> int:
    """Mark every live issue on the drawing layer: a circle sized by severity and a label "<number> <E|W|I>
    <check>", replacing the previous markers in one undo step. Waived issues and those of the `hidden` checks (the
    Issues tab's Checks…) get none; every marker keeps its issue's number in the whole list, as the Issues tab
    shows it (it counted only the issues drawn before 2026-10-20, so a waived issue earlier in the list shifted
    every later marker's number off the tab's)."""
    layer = _drawing_layer(board, layer_name)
    clear(board, f"{GROUP_PREFIX}-issues")
    items = []
    labels = []
    for k, i in enumerate(issues, 1):
        if i.waived or i.check in hidden:
            continue
        r = {"error": 0.8, "warning": 0.6}.get(i.severity, 0.45)
        items.append(_circle(i.location, r, layer, 0.15 if i.severity == "error" else 0.1))
        items.append(_text(_label_spot(labels, (i.location[0] + r + 0.3, i.location[1] - r - 0.3), -0.8),
                           f"{k} {i.severity[0].upper()} {i.check}", layer, 0.5))
    return _commit_group(board, items, f"{GROUP_PREFIX}-issues", "groundtruth: draw issue markers")


def _label_spot(taken: list, at, step: float = 0.8, width: float = 8.0):
    """Shift a label by `step` (mm, negative = up) until it no longer overlaps an earlier one: issues often sit
    at the same via. Issue labels stack upwards and suggestion labels downwards, so the two layers' labels
    stay apart too."""
    x, y = at
    while any(abs(y - ty) < abs(step) and abs(x - tx) < width for tx, ty in taken):
        y += step
    taken.append((x, y))
    return x, y


INDEX_LINE_MM, CONTOUR_LINE_MM = 0.1, 0.04      # stroke widths: index lines (1, 10, 100 ...) and the others


def _label(pos, text: str, layer, size_mm: float, angle_deg: float):
    """A contour's value centred on its line, turned along it (angle counter-clockwise as seen on the board)."""
    from kipy.board_types import BoardText
    from kipy.proto.common.types import HorizontalAlignment, VerticalAlignment
    t = BoardText()
    t.layer = layer
    t.position = _v(*pos)
    t.value = text
    t.attributes.size = _v(size_mm, size_mm)
    t.attributes.stroke_width = int(size_mm * 0.15 * MM)
    t.attributes.angle = angle_deg
    t.attributes.horizontal_alignment = HorizontalAlignment.HA_CENTER
    t.attributes.vertical_alignment = VerticalAlignment.VA_CENTER
    return t


@_when_ready
def draw_contours(board: Any, cmap, layer_name: str = "User.7", label: str = "", line_mm: float = CONTOUR_LINE_MM,
                  index_mm: float = INDEX_LINE_MM) -> tuple[int, int]:
    """A contour map (results.contours.ContourMap) on the drawing layer, like a topographic map's height lines:
    each line as graphic segments (index lines thicker: index_mm, the others line_mm; Preferences), its values as
    text along it, and one legend line above the copper; in one group and one undo step, replacing the previous
    contours. Returns (lines, labels)."""
    layer = _drawing_layer(board, layer_name)
    clear(board, f"{GROUP_PREFIX}-current")
    items = []
    for _, index, pts in cmap.lines:
        w = index_mm if index else line_mm
        items += [_segment(tuple(a), tuple(b), layer, w) for a, b in zip(pts[:-1], pts[1:])]
    items += [_label((x, y), text, layer, cmap.text_mm, ang) for x, y, ang, text in cmap.labels]
    if cmap.legend and items:
        items.append(_text(cmap.legend_at, cmap.legend, layer, cmap.text_mm * 1.2))
    _commit_group(board, items, f"{GROUP_PREFIX}-current", f"groundtruth: contours {label}")
    return len(cmap.lines), len(cmap.labels)


HEAD_FRAC = 0.3          # arrow head length, fraction of the arrow
HEAD_ANGLE = math.radians(25)


@_when_ready
def draw_arrows(board: Any, arrows: list[tuple[float, float, float, float]], layer_name: str = "User.7",
                label: str = "", width_mm: float = 0.05, group: str = "arrows") -> int:
    """Current-direction arrows [(x, y, dx, dy) mm] from results.vectors.arrow_field, each as a shaft and two head
    strokes, in one group and one undo step (replacing the previous arrows). Returns the number of arrows. An arrow
    with a fifth value draws at that line width (mm; results.vectors.arrow_widths: thicker where the current
    density is higher), else at width_mm. group: "arrows", or "arrows-return" for the return current's on a layer
    of their own; drawing "arrows" clears both (their names share the prefix), "arrows-return" only its own."""
    if not arrows:                              # nothing to draw: the previous ones go, no layer is enabled for it
        clear(board, f"{GROUP_PREFIX}-{group}")
        return 0
    layer = _drawing_layer(board, layer_name)
    clear(board, f"{GROUP_PREFIX}-{group}")
    items = []
    for arrow in arrows:
        x, y, dx, dy = arrow[:4]
        w = arrow[4] if len(arrow) > 4 else width_mm
        tip = (x + dx, y + dy)
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        back = math.atan2(-dy, -dx)
        # a thick line needs a longer head to stay an arrow (a 0.17 mm head under a 0.3 mm line is a blob): at
        # least 1.5 line widths, at most 60 % of the arrow
        h = min(max(HEAD_FRAC * length, 1.5 * w), 0.6 * length)
        items.append(_segment((x, y), tip, layer, w))
        for s in (1, -1):
            a = back + s * HEAD_ANGLE
            items.append(_segment(tip, (tip[0] + h * math.cos(a), tip[1] + h * math.sin(a)), layer, w))
    _commit_group(board, items, f"{GROUP_PREFIX}-{group}", f"groundtruth: current arrows {label}")
    return len(items) // 3


def _segment(a, b, layer, width_mm: float = 0.12):
    """A graphic line from a to b (mm) on `layer`."""
    from kipy.board_types import BoardSegment
    s = BoardSegment()
    s.layer = layer
    s.start, s.end = _v(*a), _v(*b)
    s.attributes.stroke.width = int(width_mm * MM)
    return s


@_when_ready
def draw_suggestions(board: Any, suggestions, layer_name: str = "User.8") -> int:
    """A cross and a label ("S1 via GND -4.2 dB") for every suggested via/capacitor/fill, in one undo step."""
    layer = _drawing_layer(board, layer_name)
    clear(board, f"{GROUP_PREFIX}-suggest")
    items = []
    labels = []
    for s in suggestions:
        if s.modification is None:
            continue
        x, y = s.location
        r = 0.6 if s.selected else 0.4
        w = 0.15 if s.selected else 0.08
        items.append(_segment((x - r, y - r), (x + r, y + r), layer, w))
        items.append(_segment((x - r, y + r), (x + r, y - r), layer, w))
        pred = f" {s.delta_db:+.1f} dB" if s.delta_db is not None else ""
        items.append(_text(_label_spot(labels, (x + r + 0.3, y + r + 0.3), 0.8), f"{s.id} {s.kind}{pred}", layer,
                           0.5))
        if s.modification.type == "fill":
            items.append(_polygon([list(s.modification.polygon)], layer, width_mm=0.08))
    return _commit_group(board, items, f"{GROUP_PREFIX}-suggest", "groundtruth: draw suggested fixes")


@_when_ready
def place_vias(board: Any, mods) -> int:
    """Create real through vias for via modifications (one undo step). These are design changes, not
    annotations: they are not grouped, so 'Clear GT markers' leaves them alone."""
    from kipy.board_types import Via
    from kipy.proto.board import board_types_pb2 as bt
    from kipy.proto.board.board_types_pb2 import BoardLayer, PadStackShape, ViaType
    nets = {n.name: n for n in board.get_nets()}
    items = []
    for m in mods:
        if m.type != "via":
            continue
        if m.net not in nets:
            raise ValueError(f"net {m.net!r} is not on the board")
        raw = bt.Via()
        raw.position.x_nm, raw.position.y_nm = int(round(m.at[0] * MM)), int(round(m.at[1] * MM))
        raw.net.CopyFrom(nets[m.net].proto)
        raw.type = ViaType.VT_THROUGH
        ps = raw.pad_stack
        ps.type = bt.PadStackType.PST_NORMAL
        cl = ps.copper_layers.add()
        cl.layer, cl.shape = BoardLayer.BL_F_Cu, PadStackShape.PSS_CIRCLE
        cl.size.x_nm = cl.size.y_nm = int(round(m.size * MM))
        ps.drill.start_layer, ps.drill.end_layer = BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu
        ps.drill.diameter.x_nm = ps.drill.diameter.y_nm = int(round(m.drill * MM))
        items.append(Via(proto=raw))
    if not items:
        return 0
    commit = board.begin_commit()
    try:
        created = board.create_items(items)
        board.push_commit(commit, f"groundtruth: add {len(items)} stitching via(s)")
    except Exception:
        board.drop_commit(commit)
        raise
    return len(created)


# KiCad's own editor actions, run through the API's RunAction (verified in the KiCad 10.0.5 source). KiCad calls
# RunAction an unstable API: an action it does not know answers RAS_INVALID, which is skipped and reported.
NET_HIGHLIGHT = "pcbnew.EditorControl.highlightNetSelection"   # the nets of the selected copper, others dimmed
CLEAR_HIGHLIGHT = "pcbnew.EditorControl.clearHighlight"
ZOOM_FIT = "common.Control.zoomFitSelection"
ZOOM_OUT = "common.Control.zoomOutCenter"


@_when_ready
def show_items(board: Any, lit: Iterable[str], focus: Iterable[str], context: Iterable[str] = (), nets: bool = True,
               zoom: bool = True, zoom_out: int = 0, layer: Union[str, int, None] = None,
               clear_old: bool = False) -> dict:
    """Show an issue's copper in the PCB editor without touching the design (no commit, no undo step, the board
    stays unmodified), in this order:
      1. the selection becomes `lit` (the copper on the issue's own nets) and KiCad highlights those nets. KiCad
         lights up the nets of everything selected at that moment, and with no copper selected the net under the
         mouse, so the highlight runs only when KiCad found some of `lit`;
      2. the rest of `focus` joins the selection and the view zooms to it, `zoom_out` steps further out;
      3. `context` joins the selection (planes: selected while the view stays on the focus);
      4. `layer` (a name, or KiCad's layer id to restore one) becomes the active layer.
    A highlight left from an earlier call is cleared when this one sets none (clear_old: one of ours is on), so a
    net the user highlighted in KiCad is left alone. Ids KiCad no longer knows are skipped.

    Returns {"selected", "focus" (items selected), "lit" (a highlight of ours is on now), "previous_layer" (the
    active layer before step 4, or None), "notes" (what did not work)}. Takes wait= like every function here."""
    notes: list[str] = []
    lit, focus, context = list(lit), list(focus), list(context)
    board.clear_selection()
    n = _add_ids(board, lit) if nets and lit else 0
    if n:
        lit_on = _action(board, NET_HIGHLIGHT, notes) or clear_old
    elif clear_old:
        lit_on = not _action(board, CLEAR_HIGHLIGHT, notes)
    else:
        lit_on = False
    rest = [u for u in focus if not n or u not in set(lit)]
    n_focus = _add_ids(board, rest) if rest else n
    if zoom and n_focus and _action(board, ZOOM_FIT, notes):
        for _ in range(zoom_out):
            _action(board, ZOOM_OUT, notes)
    selected = _add_ids(board, context) if context else n_focus
    previous = None
    if layer not in (None, "") and (n_focus or isinstance(layer, int)):
        try:
            from kipy.util.board_layer import layer_from_canonical_name
            previous = board.get_active_layer()
            board.set_active_layer(layer if isinstance(layer, int) else layer_from_canonical_name(layer))
        except Exception as e:          # not a layer of this board: keep the user's active layer
            previous = None
            notes.append(f"could not make {layer} the active layer ({e})")
    return {"selected": selected, "focus": n_focus, "lit": lit_on, "previous_layer": previous, "notes": notes}


@_when_ready
def clear_highlight(board: Any, layer: Optional[int] = None) -> None:
    """Undo show_items: clear our net highlight (KiCad keeps it after the plugin has closed) and give back the
    user's active layer (`layer`, KiCad's id), if one was changed."""
    _action(board, CLEAR_HIGHLIGHT, [])
    if layer is not None:
        board.set_active_layer(layer)


def _add_ids(board: Any, uuids: Iterable[str]) -> int:
    """AddToSelection with raw ids: KiCad skips ids it does not know, where GetItemsById fails when none of them
    exists any more. Returns the size of the selection afterwards."""
    from kipy.proto.common.commands import editor_commands_pb2
    cmd = editor_commands_pb2.AddToSelection()
    cmd.header.document.CopyFrom(board.document)
    for u in uuids:
        cmd.items.add().value = u
    return len(board.client.send(cmd, editor_commands_pb2.SelectionResponse).items)


def _action(board: Any, name: str, notes: list[str]) -> bool:
    """Run a KiCad action by name (zoom, highlight ...); False, with a note, when this KiCad has no such action."""
    from kipy.proto.common import commands
    cmd = commands.RunAction()
    cmd.action = name
    if board.client.send(cmd, commands.RunActionResponse).status != commands.RAS_OK:
        notes.append(f"this KiCad has no action {name}")
        return False
    return True


class SchematicApiUnavailable(RuntimeError):
    """KiCad can't change the schematic through its API: KiCad 10 has no schematic API (kicad-python marks it
    KiCad 11), or the schematic editor isn't open. The caller keeps what it can (the settings) and tells the user
    which GT labels to add by hand."""


def write_symbol_labels(kicad_board: Any, labels: dict[str, str]) -> list[str]:
    """Set the GT field (detection labels, analysis/tags.py) of these symbols in the open schematic, one undo step
    in the schematic editor: {reference: the field's whole text}. A symbol without a GT field gets a hidden one.
    Returns the references written; SchematicApiUnavailable when KiCad can't do it (see the class)."""
    from kipy.errors import ApiError
    try:
        from kipy.proto.common import commands
        from kipy.proto.common.types import DocumentType
        from kipy.schematic import Schematic
        from kipy.schematic_types import SchematicField
    except ImportError as e:                    # an older kicad-python
        raise SchematicApiUnavailable(f"this kicad-python has no schematic API ({e})") from e
    client = kicad_board.client
    try:
        cmd = commands.GetOpenDocuments()
        cmd.type = DocumentType.DOCTYPE_SCHEMATIC
        docs = client.send(cmd, commands.GetOpenDocumentsResponse).documents
        if not docs:
            raise SchematicApiUnavailable("the schematic editor is not open")
        sch = Schematic(client, docs[0])
        changed, done = [], set()
        for sym in sch.get_symbols():
            ref = sym.reference_field.text.value
            if ref not in labels:
                continue
            # copies of the fields (the wrappers point into the symbol's own list, which the setter replaces)
            fields = [SchematicField(proto=f.proto) for f in sym.user_fields]
            gt = next((f for f in fields if f.name.strip().upper() in ("GT", "GROUNDTRUTH")), None)
            if gt is None:
                gt = SchematicField()
                gt.name, gt.visible = "GT", False
                fields.append(gt)
            text = gt.text
            text.value = labels[ref]
            gt.text = text
            sym.user_fields = fields
            changed.append(sym)
            done.add(ref)
        if changed:
            commit = sch.begin_commit()
            sch.update_items(changed)
            sch.push_commit(commit, "GroundTruth: GT labels for a signal path")
    except ApiError as e:                       # "no handler": KiCad 10 serves no schematic requests
        raise SchematicApiUnavailable(f"KiCad did not accept the schematic request ({e})") from e
    return sorted(done)
