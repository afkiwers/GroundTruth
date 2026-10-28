"""Matplotlib figures for the HTML report, CI artifacts and the wx heatmap panel.

Only `matplotlib.figure.Figure` is used (no pyplot), so callers pick the canvas/back-end.
"""
from __future__ import annotations

import io
import math
from typing import Optional

import numpy as np
from matplotlib import patheffects
from matplotlib.collections import EllipseCollection, LineCollection
from matplotlib.colors import LogNorm, Normalize
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, NullFormatter
from matplotlib.transforms import ScaledTranslation, blended_transform_factory

from ..analysis.simulate import RouteResult
from ..core.board import Board
from ..core.issues import Issue

LAYER_COLOURS = ["#c83434", "#c8a034", "#34a0c8", "#8434c8", "#34c87a", "#c834a0", "#7a7a7a", "#3448c8"]
SEV_COLOURS = {"error": "#d62728", "warning": "#ff9f1c", "info": "#1f77b4"}

# Text sizes in points. The user, about 6-7 pt legends and titles in the plugin: "the legend and graph descriptions
# are very small and hard to read". The plugin draws at 100 dpi (times the display's scale), where 9 pt is 12.5 px,
# the size of the window's own text; nothing is smaller than the bold 8 pt labels on the maps. Margins are worked
# out in inches from these sizes, so the text fits at any figure size (tests/test_plot_text.py).
FS_MARK = 8             # bold labels on the maps and in the side view (a via's share)
FS_TICK = 8.5           # tick labels, legends
FS_TEXT = 9             # axis labels, the maps' titles, notes
FS_TITLE = 10           # figure titles
LINE = 1.3              # a line of text is this many times its size high (matplotlib: 1.2, and some air)


def _lh(size: float, lines: int = 1) -> float:
    """The height of `lines` lines of text at `size` points, in inches."""
    return lines * size * LINE / 72


_ADVANCE: dict[tuple[str, bool], float] = {}     # a character's advance at 1 pt, in points


def _advance(ch: str, bold: bool) -> float:
    """A character's advance width at 1 pt in matplotlib's font (no hinting), measured once. matplotlib keeps a
    font object per thread, so this is thread-safe; laying out a whole text in FreeType costs ~20 µs a character,
    which on the GUI thread (the Results page's heights) stalled the window."""
    a = _ADVANCE.get((ch, bold))
    if a is None:
        from matplotlib import ft2font
        from matplotlib.font_manager import FontProperties, findfont, get_font
        flags = ft2font.LoadFlags.NO_HINTING if hasattr(ft2font, "LoadFlags") else ft2font.LOAD_NO_HINTING
        font = get_font(findfont(FontProperties(weight="bold" if bold else "normal")))
        font.set_size(100, 72)
        a = font.load_char(ord(ch), flags=flags).linearHoriAdvance / 65536 / 100
        _ADVANCE[(ch, bold)] = a
    return a


WARM_CHARS = "".join(chr(c) for c in range(32, 127)) + "→←↑↓×µΩ≈²³°±≥≤○●▲■…–—·√Δλπ"


def warm_text():
    """Measure the usual characters (_advance) once, in a worker: the first measurement looks the font up
    (findfont, ~0.3 s), and the plugin's page measures its titles on the GUI thread."""
    for bold in (False, True):
        for ch in WARM_CHARS:
            _advance(ch, bold)


def _width_in(text: str, size: float, bold: bool = False) -> float:
    """A text's width in inches at `size` points in matplotlib's font: the sum of its characters' advances (no
    kerning: within a few per cent of the drawn width, enough for margins and line breaks)."""
    return sum(_advance(ch, bold) for ch in text) * size / 72


def _wrap(text: str, width_in: float, size: float) -> list[str]:
    """`text` broken at spaces into lines no wider than width_in (a single word wider than that stays whole)."""
    lines, line = [], ""
    for word in text.split(" "):
        trial = f"{line} {word}" if line else word
        if line and _width_in(trial, size) > width_in:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line] if line else lines


def _frame(fig, left: float, right: float, bottom: float, top: float) -> tuple[float, float, float, float]:
    """Margins in inches as the plot area's (x0, x1, y0, y1) in figure fractions (`fig`: a Figure or its (width,
    height) in inches). On a figure too small for them they shrink in proportion: a cramped plot, never
    matplotlib's ValueError for left >= right."""
    w, h = fig.get_size_inches() if isinstance(fig, Figure) else fig
    w, h = max(w, 1e-3), max(h, 1e-3)
    sx = min(1.0, 0.8 * w / max(left + right, 1e-9))
    sy = min(1.0, 0.8 * h / max(bottom + top, 1e-9))
    return left * sx / w, 1 - right * sx / w, bottom * sy / h, 1 - top * sy / h


def _adjust(fig: Figure, left: float, right: float, bottom: float, top: float, **kw) -> None:
    """subplots_adjust with the margins in inches (see _frame)."""
    x0, x1, y0, y1 = _frame(fig, left, right, bottom, top)
    fig.subplots_adjust(left=x0, right=x1, bottom=y0, top=y1, **kw)


def _title_and_key(ax, title: str, width_in: float, size: float = FS_TITLE) -> float:
    """`title` over the axes and the legend of its labelled artists above the plot, where it hides no data: on the
    title's line, right-aligned, when both fit in width_in (the axes' width), else in a row of its own between the
    title (wrapped, centred) and the plot. Returns the height they take over the axes, in inches."""
    labels = ax.get_legend_handles_labels()[1]
    em = FS_TICK / 72
    key_in = sum(_width_in(l, FS_TICK) for l in labels) + len(labels) * 2.0 * em + \
        max(len(labels) - 1, 0) * 1.2 * em + 0.15
    style = {"fontsize": FS_TICK, "handlelength": 1.5, "handletextpad": 0.5, "frameon": False, "borderpad": 0.2,
                 "columnspacing": 1.2}
    pad = 6.0                                           # matplotlib's title pad, points
    if labels and _width_in(title, size) + 0.3 + key_in <= width_in:
        ax.set_title(title, loc="left", fontsize=size)
        ax.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=len(labels), borderaxespad=0.15, **style)
        return pad / 72 + _lh(size)
    lines = _wrap(title, width_in, size)
    key_pt = 0.0
    if labels:
        rows = 1 if key_in <= width_in else len(labels)
        key_pt = rows * FS_TICK * 1.5 + 4
        ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=len(labels) if rows == 1 else 1,
                  borderaxespad=0.2, **style)
    ax.set_title("\n".join(lines), fontsize=size, pad=pad + key_pt)
    return (pad + key_pt) / 72 + _lh(size, len(lines))


def _hz(v_mhz: float, _pos=None) -> str:
    """A frequency given in MHz as '150 kHz', '100 MHz', '1.5 GHz'."""
    hz = v_mhz * 1e6
    for unit, f in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if hz >= f * 0.999:
            return f"{hz / f:g} {unit}"
    return f"{hz:g} Hz"


def _freq_axis(ax, f_mhz) -> None:
    """A log frequency axis (values in MHz) with the unit on its labels ('100 MHz', '1 GHz'), so it needs no axis
    label. Spanning less than two decades, the 2 and 5 steps between the decades are labelled too."""
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(_hz))
    f = np.asarray(f_mhz, float)
    if len(f) and f.min() > 0 and f.max() / f.min() < 100:
        def minor(v, _pos=None):
            """Minor tick labels on the log axis only at 2 and 5 times a power of ten."""
            return _hz(v) if v > 0 and round(v / 10 ** math.floor(math.log10(v))) in (2, 5) else ""
        ax.xaxis.set_minor_formatter(FuncFormatter(minor))
        ax.tick_params(axis="x", which="minor", pad=5.0)    # level with the decades' labels (longer ticks)
    else:
        ax.xaxis.set_minor_formatter(NullFormatter())


def layer_colour(board: Board, layer: str) -> str:
    """A copper layer's colour in the plots: F.Cu and B.Cu their own, inner layers from the palette in order."""
    names = board.copper_layers
    if layer == "F.Cu":
        return LAYER_COLOURS[0]
    if layer == "B.Cu":
        return LAYER_COLOURS[2]
    i = names.index(layer) if layer in names else 0
    return LAYER_COLOURS[(i + 3) % len(LAYER_COLOURS)]


def _outline(ax, board: Board):
    """Board outline and cut-outs as one collection (one artist instead of one line per ring: much faster)."""
    rings = [list(ring) + [ring[0]] for outline, holes in board.outline for ring in [outline, *holes] if ring]
    if rings:
        ax.add_collection(LineCollection(rings, colors="#888", linewidths=0.8))


def _route(ax, board: Board, rr: RouteResult):
    """The route's runs, one collection per layer (with a legend entry per layer)."""
    by_layer: dict[str, list] = {}
    for rl, pts in rr.route_xy:
        by_layer.setdefault(rl, []).append(list(pts))
    for rl, runs in by_layer.items():
        ax.add_collection(LineCollection(runs, colors=layer_colour(board, rl), linewidths=1.2, label=rl))


CARRIER = "#39e75f"         # vias and parts that carry the current between layers / nets
MIN_LABEL = 0.05            # their share of the current is written next to them from this share on
# where a carrier's label may go, in points from its marker (first free one wins; the busiest is placed first):
# right / left of it, then higher and lower rows. (dx, dy, horizontal alignment)
LABEL_SPOTS = [(7, 5, "left"), (7, -14, "left"), (-7, 5, "right"), (-7, -14, "right"), (7, 16, "left"),
               (7, -25, "left"), (-7, 16, "right"), (-7, -25, "right"), (7, 27, "left"), (7, -36, "left"),
               (-7, 27, "right"), (-7, -36, "right")]


def busiest_via(rr) -> Optional[dict]:
    """The via the view's "busiest via" tile names (ui/kpi.py): a DC path's (or a DC load's) via carrying the most
    current, or a motor bridge's RMS view's; {name, x, y, current_a, ...} or None (other views, no vias)."""
    rms = getattr(rr, "rms_of", None)
    if rms is not None:
        vias = (getattr(rms, "rms", None) or {}).get("vias") or []
    else:
        view = getattr(rr, "view_of", None)
        path = next((p for p in (getattr(rr, "dc", None) or []) if p.get("name") == view), None) if view else None
        vias = (path or {}).get("vias") or []
    return vias[0] if vias else None


def hot_spot(rr) -> Optional[dict]:
    """The view's sustained current-density spot its "current density" / "RMS current density" tile names (ui/kpi.py):
    {x, y, layer, a_mm2} of a DC path's (a DC load's) or a motor bridge's RMS view, where draw_heatmap marks it
    with a cyan ×; None for other views or without maps (a bridge's sweep: plots, not maps)."""
    if getattr(rr, "grid", None) is None or not getattr(rr, "sheet_current", None):
        return None
    rms = getattr(rr, "rms_of", None)
    if rms is not None:
        src = getattr(rms, "rms", None) or {}
    else:
        view = getattr(rr, "view_of", None)
        src = next((p for p in (getattr(rr, "dc", None) or []) if p.get("name") == view), None) if view else None
    at = (src or {}).get("sustained_at")
    if not at:
        return None
    return {"x": float(at[0]), "y": float(at[1]), "layer": src.get("sustained_layer") or "",
            "a_mm2": src.get("sustained_a_mm2")}


def carrier_text(t: dict) -> str:
    """A carrier marker's hover text: its name, its share of the current and, where the view knows it, its
    current in amperes ("via GND @(138.25, 75.70): 12 % of the current, 1.20 A"; a motor bridge's: A RMS)."""
    amps = t.get("current_a")
    extra = f", {amps:.3g} A" + (" RMS" if t.get("rms") else "") if amps is not None else ""
    return f"{t['name']}: {t['fraction'] * 100:.0f} % of the current{extra}"


def _place_labels(ax) -> None:
    """Write the carriers' share labels that _carriers queued on `ax`, once its limits and aspect are final (the
    pixel positions are only known then): each at the first of LABEL_SPOTS whose box overlaps no label placed
    before it and no other marker, the busiest first, with a thin leader line when it had to move from beside its
    marker. The user: on a DC path's map the shares of vias a millimetre apart were written over each other ("58 %"
    "8 %"), so which via was the busiest could not be read."""
    specs = getattr(ax, "_gt_label_specs", None) or []
    if not specs:
        return
    ax.apply_aspect()
    k = ax.figure.dpi / 72.0
    pts = ax.transData.transform([(s["x"], s["y"]) for s in specs])
    taken: list[tuple[float, float, float, float]] = [(px - 5 * k, py - 5 * k, px + 5 * k, py + 5 * k)
                                                      for px, py in pts]      # the markers themselves
    for s, (px, py) in sorted(zip(specs, pts), key=lambda sp: -sp[0]["fraction"]):
        w, h = (len(s["text"]) * 0.62 * FS_MARK + 6) * k, FS_MARK * 1.5 * k
        spot = LABEL_SPOTS[0]
        for dx, dy, ha in LABEL_SPOTS:
            x0 = px + dx * k - (w if ha == "right" else 0.0)
            box = (x0, py + dy * k - 0.2 * h, x0 + w, py + dy * k + 0.8 * h)
            if not any(box[0] < t[2] and t[0] < box[2] and box[1] < t[3] and t[1] < box[3] for t in taken):
                spot = (dx, dy, ha)
                taken.append(box)
                break
        moved = spot[:2] != LABEL_SPOTS[0][:2]
        ax.annotate(s["text"], (s["x"], s["y"]), textcoords="offset points", xytext=spot[:2], ha=spot[2],
                    fontsize=FS_MARK, color="#ffffff", fontweight="bold", zorder=5,
                    arrowprops={"arrowstyle": "-", "color": CARRIER, "lw": 0.6} if moved else None,
                    bbox={"boxstyle": "round,pad=0.15", "fc": "#101018", "lw": 0.6 if not s["busiest"] else 1.4,
                          "ec": "#ffffff" if s["busiest"] else CARRIER, "alpha": 0.85})


def _carriers(ax, board: Board, rr: RouteResult, lname: str, max_vias: Optional[int] = None,
              labels: bool = True) -> list[dict]:
    """The vias (▲) and parts (■) that carry this layer's current to another layer or net (RouteResult.transfers),
    with their share of it ("63 %"). The user, after all 338 vias of a board were drawn: "Only the ones that carry
    current should be drawn. However, the current symbol is a bit small and hard to see": so a larger symbol with a
    dark outline that reads on any colour, the share as a label, and a carrying via also at its true size (pad ring
    and drill, in board units) for the wheel zoom. A plane pair's displacement current is not marked: it is spread
    over the whole pair and comes from the board-wide model at another frequency (a square labelled "1286 %" at
    its weighted centre said nothing about where it flows); the tiles and the transfer list give it.
    max_vias: mark at most that many vias, the view's busiest (the user's "Vias shown"; 0: "Via markers"
    unticked); None: every one. labels False: the markers without their shares (the report).
    The via with the largest share gets a white ring (the "busiest via" tile's, on a DC path's map); the shares are
    queued on the axes and written by _place_labels once the view is final, apart from each other."""
    shown = rr.transfers
    if max_vias is not None:
        busiest = sorted((t for t in shown if t.get("kind") == "via"), key=lambda t: -t["fraction"])[:max_vias]
        keep = {id(t) for t in busiest}
        shown = [t for t in shown if t.get("kind") != "via" or id(t) in keep]
    ts = [t for t in shown if lname in t["layers"] and t.get("kind") != "capacitance"]
    if not ts:
        return []
    vias = [t for t in ts if t["kind"] == "via"]
    if vias and board.vias:
        at = np.array([v.position for v in board.vias], float)
        found = []
        for t in vias:                          # the board's via under the marker: its pad and drill size
            k = int(np.argmin(np.hypot(at[:, 0] - t["x"], at[:, 1] - t["y"])))
            v = board.vias[k]
            if math.hypot(v.position[0] - t["x"], v.position[1] - t["y"]) < max(v.size, 0.3):
                found.append(v)
        if found:
            xy = np.array([v.position for v in found], float)
            size = np.array([v.size for v in found], float)
            drill = np.array([v.drill for v in found], float)
            common = {"angles": np.zeros(len(found)), "units": "xy", "offsets": xy, "offset_transform": ax.transData}
            ax.add_collection(EllipseCollection(size, size, facecolors="none", edgecolors=CARRIER, linewidths=1.2,
                                                zorder=3.5, **common))
            ax.add_collection(EllipseCollection(drill, drill, facecolors="#101018", edgecolors="none", zorder=3.5,
                                                **common))
    for marker, group in (("^", vias), ("s", [t for t in ts if t["kind"] != "via"])):
        if not group:
            continue
        ax.plot([t["x"] for t in group], [t["y"] for t in group], linestyle="none", marker=marker, ms=9,
                mfc=CARRIER, mec="#000000", mew=1.0, zorder=4)
    top = max(vias, key=lambda t: t["fraction"], default=None)
    if top is not None:                         # the busiest via: a white ring with a dark halo, on any colour
        for colour, mew in (("#000000", 3.6), ("#ffffff", 1.8)):
            ax.plot([top["x"]], [top["y"]], linestyle="none", marker="o", ms=19, mfc="none", mec=colour, mew=mew,
                    zorder=4.5)
    if labels:
        ax._gt_label_specs = [{"x": t["x"], "y": t["y"], "fraction": t["fraction"], "busiest": t is top,
                               "text": f"{t['fraction'] * 100:.0f} %"} for t in ts if t["fraction"] >= MIN_LABEL]
    return ts


# a power stage's switch pins (ConverterResult.switch_pins): outline colour and legend word per role
PIN_ROLES = {"D": ("#00e5ff", "drain"), "K": ("#00e5ff", "cathode"), "VIN": ("#00e5ff", "VIN"),
             "S": ("#7cfc00", "source"), "A": ("#7cfc00", "anode"), "GND": ("#7cfc00", "GND"),
             "SW": ("#ff7cff", "switch node")}


def _pad_outline(p: dict) -> tuple[list, list]:
    """A pad's outline (closed), from its polygon or its rotated rectangle (board mm, y down)."""
    if p.get("polygon"):
        pts = list(p["polygon"]) + [p["polygon"][0]]
    else:
        a = math.radians(p.get("angle") or 0.0)
        c, s = math.cos(a), math.sin(a)
        w, h = p["w"] / 2, p["h"] / 2
        pts = [(p["x"] + x * c + y * s, p["y"] - x * s + y * c) for x, y in ((-w, -h), (w, -h), (w, h), (-w, h),
                                                                            (-w, -h))]
    return [q[0] for q in pts], [q[1] for q in pts]


def _switch_pins(ax, rr, lname: str, labels: bool = True) -> None:
    """The switches' drain and source (a diode's cathode and anode, an IC's VIN / SW / GND) on a power stage's
    map: the pads outlined on their own layer (with a dark halo so they read on any colour), and with `labels` a
    role letter at each group's centre and the part's name above it on every layer (where the switch sits over an
    inner plane). Without them the outlines' colours and the legend tell the roles (the report)."""
    pins = getattr(rr, "switch_pins", None) or []
    if not pins:
        return
    groups: dict[tuple, list] = {}
    for p in pins:
        groups.setdefault((p["ref"], p["role"]), []).append(p)
    seen_roles = set()
    for (_, role), ps in groups.items():
        colour, word = PIN_ROLES.get(role, ("#ffffff", role))
        for p in ps:
            if lname not in p["layers"]:          # other layers: the letters only (outlines there were clutter)
                continue
            xs, ys = _pad_outline(p)
            ax.plot(xs, ys, color="#000000", lw=2.2, alpha=0.6, solid_capstyle="round")
            ax.plot(xs, ys, color=colour, lw=1.1)
        if labels:
            cx = sum(p["x"] for p in ps) / len(ps)
            cy = sum(p["y"] for p in ps) / len(ps)
            ax.text(cx, cy, role, color=colour, fontsize=FS_MARK, fontweight="bold", ha="center", va="center",
                    bbox={"boxstyle": "round,pad=0.15", "fc": "#101018", "ec": colour, "lw": 0.7, "alpha": 0.8},
                    zorder=6)
        if role not in seen_roles:
            # the legend names the colour's role: with labels "S = source", without them the colour is the key
            ax.plot([], [], color=colour, lw=1.2, label=f"{role} = {word}" if labels else word)
            seen_roles.add(role)
    if not labels:
        return
    for ref in dict.fromkeys(p["ref"] for p in pins):    # the part's name just above its pads
        ps = [p for p in pins if p["ref"] == ref]
        top = min(p["y"] - max(p["w"], p["h"]) / 2 for p in ps)
        cx = (min(p["x"] for p in ps) + max(p["x"] for p in ps)) / 2
        ax.text(cx, top - 0.4, ref, color="white", fontsize=FS_MARK, ha="center", va="bottom",
                bbox={"boxstyle": "round,pad=0.12", "fc": "#101018", "ec": "none", "alpha": 0.7}, zorder=6)


ALL_LAYERS = "*all*"     # heatmap_layers' `layer` for every copper layer of the board (the Layer list's "all layers")


def heatmap_layers(rr: RouteResult, layer: Optional[str] = None, max_layers: int = 4) -> list[str]:
    """The layers a heatmap shows: the one asked for, ALL_LAYERS: every layer of the model with copper on it, in
    the board's stack order (the user, of the overview's four of six: "add all layers on a board to the
    overview"), else those carrying a noticeable share of the current (at most max_layers), else the first layer."""
    if layer == ALL_LAYERS:
        grids = getattr(rr, "layer_grids", None) or {}
        return [l for l in rr.sheet_current if l not in grids or np.asarray(grids[l].node).any()]
    if layer:
        return [layer] if layer in rr.sheet_current else []
    return rr.main_layers()[:max_layers] or list(rr.sheet_current)[:1]


def _arrows(ax, rr, lname: str, n_across: int, ref: float, view: Optional[tuple] = None,
            floor: Optional[float] = None) -> int:
    """Current-direction arrows on one heatmap panel (none when the result has no vector field). With a `view`
    (x0, x1, y0, y1, a zoomed-in part of the map) they are laid out over that part only, n_across over its
    width: zooming in shows the flow in more detail, not the same arrows larger."""
    from types import SimpleNamespace
    from ..results.vectors import arrow_field
    vec = (getattr(rr, "sheet_vectors", None) or {}).get(lname)
    if vec is None:
        return 0
    g, K, (Kx, Ky) = rr.layer_grids[lname], rr.sheet_current[lname], vec
    if view is not None:                        # the nodes in view (and one beyond), as a grid of their own
        nx, ny = g.node.shape
        i0 = max(int(math.floor((view[0] - g.x0) / g.d)) - 1, 0)
        i1 = min(int(math.ceil((view[1] - g.x0) / g.d)) + 2, nx)
        j0 = max(int(math.floor((view[2] - g.y0) / g.d)) - 1, 0)
        j1 = min(int(math.ceil((view[3] - g.y0) / g.d)) + 2, ny)
        if i1 - i0 < 2 or j1 - j0 < 2:
            return 0
        crop = (slice(i0, i1), slice(j0, j1))
        g = SimpleNamespace(node=g.node[crop], d=g.d, x0=g.x0 + i0 * g.d, y0=g.y0 + j0 * g.d)
        K, Kx, Ky = K[crop], Kx[crop], Ky[crop]
    arrows = arrow_field(g, K, Kx, Ky, n_across=n_across, ref=ref, floor=floor)
    if not arrows:
        return 0
    x, y, dx, dy = (np.array(c) for c in zip(*arrows))
    # all arrows are equally long: the shaft is a sixth of that (quiver's width is a fraction of the axes width),
    # the head about half of the arrow
    length = float(np.hypot(dx[0], dy[0]))
    x0, x1 = (view[0], view[1]) if view is not None else rr.layer_grids[lname].extent[:2]
    ax.quiver(x, y, dx, dy, angles="xy", scale_units="xy", scale=1, color="white", edgecolor="black",
              linewidth=0.25, width=length / 6 / max(abs(x1 - x0), 1e-9), headwidth=3, headlength=3,
              headaxislength=2.7, zorder=3)
    return len(arrows)


def map_grid(n: int, fig_in, region_mm, frame=(0.80, 0.76)) -> tuple[int, int]:
    """(rows, cols) for n maps of a region (width, height in mm) on a figure (width, height in inches) that makes
    each map largest: a tall, narrow region goes side by side, a wide one stacks. `frame`: the share of the figure
    left for the maps (margins, colour bar, titles)."""
    if n <= 1:
        return 1, 1
    w_in, h_in = fig_in[0] * frame[0], fig_in[1] * frame[1]
    rw, rh = max(region_mm[0], 1e-6), max(region_mm[1], 1e-6)
    best = None
    for cols in range(1, n + 1):
        rows = -(-n // cols)
        scale = min(w_in / cols / rw, h_in / rows * 0.85 / rh)   # 15 % of each row for its title and ticks
        if best is None or scale > best[0] * 1.001:
            best = (scale, rows, cols)
    return best[1], best[2]


PART_LINE = "#29a9f0"        # part outlines over a map: thin, a blue that reads on the dark map and the grey


def _part_outlines(ax, board: Board, extent: tuple, outlines: bool = True, names: bool = True) -> None:
    """Every part's outline over a map (its courtyard, else its fab outline, else a box around its pads), the
    top side's solid and the bottom side's dashed, and (names) each part's reference: to see which current flows
    under which part (the user: "so the user can get a better feel"). Outlines and names are separate choices."""
    from matplotlib import patheffects
    from matplotlib.collections import LineCollection
    x0, x1, y0, y1 = extent
    lines = {False: [], True: []}
    for fp in board.footprints:
        polys = fp.outline
        if not polys and fp.pads:
            xs = [p.position[0] + s * p.size[0] / 2 for p in fp.pads for s in (-1, 1)]
            ys = [p.position[1] + s * p.size[1] / 2 for p in fp.pads for s in (-1, 1)]
            a, b, c, d = min(xs) - 0.25, max(xs) + 0.25, min(ys) - 0.25, max(ys) + 0.25
            polys = [[(a, c), (b, c), (b, d), (a, d), (a, c)]]
        pts = [p for pl in polys for p in pl]
        if not pts:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if max(xs) < min(x0, x1) or min(xs) > max(x0, x1) or max(ys) < min(y0, y1) or min(ys) > max(y0, y1):
            continue
        bottom = fp.layer.startswith("B.")
        if outlines:
            lines[bottom] += polys
        if names:
            ax.text((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, fp.reference, fontsize=FS_MARK - 1.5,
                    color="#d8f0ff", ha="center", va="center", zorder=3.2, clip_on=True,
                    path_effects=[patheffects.withStroke(linewidth=2, foreground="#0b2236")])
    for bottom, segs in lines.items():
        if segs:
            ax.add_collection(LineCollection(segs, colors=PART_LINE, linewidths=0.9, alpha=0.9, zorder=3.1,
                                             linestyles="--" if bottom else "-"))


def mark_at(fig, ax, x_px: float, y_px: float, tol_px: float = 9.0) -> str:
    """What the marker under the mouse is (display pixels x_px, y_px over the map ax): an issue's message, or a
    via's or part's share of the current; an issue first where both are in reach. "" when none is."""
    marks = (getattr(fig, "_gt_marks", None) or {}).get(ax) or []
    if not marks:
        return ""
    xy = ax.transData.transform([(x, y) for x, y, _, _ in marks])
    d = np.hypot(xy[:, 0] - x_px, xy[:, 1] - y_px)
    near = [(rank, d[k], text) for k, (_, _, rank, text) in enumerate(marks) if d[k] <= tol_px]
    return min(near)[2] if near else ""


ISSUE_CLUSTER = 0.07        # issues closer than this share of the view's span share one ring (about two rings' width)
_SEV_ORDER = {"error": 0, "warning": 1, "info": 2}


def cluster_issues(issues, radius: float) -> list[list]:
    """The issues in groups whose rings would overlap: the most severe issue not yet taken (errors first, then in the
    list's order) gathers every other one within `radius` (mm) of it; each group starts with that issue (its ring's
    place, colour and the issue a click shows). Greedy and order-stable: the same issues and view give the same
    groups."""
    left = sorted(issues, key=lambda i: _SEV_ORDER.get(i.severity, 3))
    groups = []
    while left:
        top, rest = left[0], left[1:]
        x, y = top.location
        near = [i for i in rest if math.hypot(i.location[0] - x, i.location[1] - y) <= radius]
        groups.append([top] + near)
        taken = {id(i) for i in near}
        left = [i for i in rest if id(i) not in taken]
    return groups


def cluster_text(group: list) -> str:
    """A ring's hover text: its issue's, or for several their count by severity, the most severe one's message and
    the others' checks (zoom in to part them)."""
    top = group[0]
    if len(group) == 1:
        return f"{top.severity} · {top.check}: {top.message}"
    counts = ", ".join(f"{n} {s}{'s' if n > 1 and s != 'info' else ''}" for s in ("error", "warning", "info")
                       if (n := sum(1 for i in group if i.severity == s)))
    others = {}
    for i in group[1:]:
        others[i.check] = others.get(i.check, 0) + 1
    rest = ", ".join(f"{c} ×{n}" if n > 1 else c for c, n in others.items())
    return (f"{len(group)} issues here ({counts}); the worst: {top.severity} · {top.check}: {top.message}; also "
            f"{rest} (zoom in to part them)")


def view_issues(rr, issues) -> list:
    """The issues a view's maps mark: its own net's (or power stage's), and of a power stage only those about what
    the view shows. A DC path's view (view_of: "input J101.2 → Q301") marks that path's current-density and via
    issues (keys ending ":dc:<path>" / ":via:<path>", power._converter_issues); the stage's other views (the hot
    loop, a bridge's RMS currents) mark none of its DC paths' issues. Before, every view of a stage showed all of
    them: the output path's overloaded via appeared on the switch-node map, where that current doesn't flow."""
    mine = [i for i in issues if i.net == rr.net]
    view = getattr(rr, "view_of", None)

    def path_of(i):
        """The DC path an issue is about (from its key), or None for a stage-wide one."""
        for tag in (":dc:", ":via:"):
            if tag in (i.key or ""):
                return i.key.split(tag, 1)[1]
        return None
    if view and view != "RMS":
        return [i for i in mine if path_of(i) == view]
    return [i for i in mine if path_of(i) is None]


def mark_ref_at(fig, ax, x_px: float, y_px: float, tol_px: float = 9.0):
    """The marker under the mouse as (kind, what, text): ("issue", Issue, ...), ("via", transfer dict, ...) or
    ("part", transfer dict, ...), an issue first where both are in reach (as mark_at); None when no marker is."""
    marks = (getattr(fig, "_gt_marks", None) or {}).get(ax) or []
    refs = (getattr(fig, "_gt_mark_refs", None) or {}).get(ax) or []
    if not marks or len(refs) != len(marks):
        return None
    xy = ax.transData.transform([(m[0], m[1]) for m in marks])
    d = np.hypot(xy[:, 0] - x_px, xy[:, 1] - y_px)
    near = [(marks[k][2], d[k], k) for k in range(len(marks)) if d[k] <= tol_px]
    if not near:
        return None
    k = min(near)[2]
    return refs[k][0], refs[k][1], marks[k][3]


def map_value(hit, x: float, y: float) -> str:
    """'In1.Cu  x 142.31  y 88.02 mm   |K| 512 A/m per A' for the cell under (x, y) of a map recorded by
    draw_heatmap (fig._gt_maps[ax] = (layer, grid, values, unit)); no copper there: says so."""
    layer, g, K, unit = hit
    i, j = int(round((x - g.x0) / g.d)), int(round((y - g.y0) / g.d))     # nearest node (values sit on nodes)
    where = f"{layer}   x {x:.2f}  y {y:.2f} mm"
    if not (0 <= i < K.shape[0] and 0 <= j < K.shape[1]) or not np.isfinite(K[i, j]):
        return f"{where}   no copper"
    v = float(K[i, j])
    return f"{where}   {v:.3g} {unit}" if unit != "°C" else f"{where}   {v:.1f} °C"


def _heatmap_layout(rr, layers: list[str], w_in: float, h_in: float, vectors: bool, scale: str):
    """draw_heatmap's text and layout for a w_in x h_in figure: the title and the note over the maps (wrapped),
    margins in inches from the text they hold (each map's title, the tick labels, the legend of the route's
    layers in one row under the maps, the colour bar's strip on the right), the gaps between the maps and their
    rows x cols."""
    from types import SimpleNamespace
    linear = getattr(rr, "heat_scale", "log") == "linear"
    what = _what(rr)
    title = getattr(rr, "heat_title", None) or (f"{rr.net}  {rr.driver}→{rr.receiver}  {what} current @ "
                                                  f"{rr.map_frequency / 1e6:.4g} MHz")
    note = "▲ via / ■ part carrying it (% of the current; white ring: the busiest via), red / orange ○ issue"
    if vectors and getattr(rr, "sheet_vectors", None):
        note += "; arrows: current direction, more arrows = more current"
    if scale == "linear":
        note += "; linear colour scale"
    if getattr(rr, "terminals_shown", False):       # the Preferences' Terminal current (results/terminals.py)
        note += "; with the current in the parts' terminals"
    if linear:
        note = "× hot spot, ○ issues"
    head = _wrap(title, w_in - 0.3, FS_TITLE)
    notes = _wrap(f"({note})", w_in - 0.3, FS_TEXT)
    keys = len({l for l, _ in rr.route_xy}) + len({p["role"] for p in getattr(rr, "switch_pins", None) or []})
    key_rows = -(-keys // 8)
    tick_in = 0.10 + _lh(FS_TICK)
    top = 0.08 + _lh(FS_TITLE, len(head)) + _lh(FS_TEXT, len(notes)) + 0.08 + _lh(FS_TEXT) + 0.06
    bottom = 0.06 + tick_in + (_lh(FS_TICK, key_rows) + 0.10 if key_rows else 0.0)
    left, right = 0.62, 1.10
    fx0, fx1, fy0, fy1 = _frame((w_in, h_in), left, right, bottom, top)
    g0 = rr.layer_grids[layers[0]]
    region = (g0.extent[1] - g0.extent[0], g0.extent[3] - g0.extent[2])
    rows, cols = map_grid(len(layers), (w_in, h_in), region, frame=(fx1 - fx0, fy1 - fy0))
    return SimpleNamespace(head=head, notes=notes, tick_in=tick_in, top=top, bottom=bottom, left=left, right=right,
                           gap_w=left + 0.10, gap_h=tick_in + _lh(FS_TEXT) + 0.16, rows=rows, cols=cols,
                           region=region)


def heatmap_height_in(rr, w_in: float, h_in: float, layer: Optional[str] = None, vectors: bool = True,
                      scale: str = "log") -> float:
    """The height, at most h_in, that draw_heatmap's maps need on a w_in wide figure. Maps limited by the width (a
    wide board) leave bands above and below them in a taller figure: the plugin's page cuts its maps canvas to
    this, so the plots under them come up sooner."""
    if rr.kind == "bridge":                         # the drive's waveforms, not maps
        return min(h_in, BRIDGE_WAVE_IN)
    layers = heatmap_layers(rr, layer)
    if rr.grid is None or not layers or w_in < 1.0 or h_in < 0.5:      # not laid out yet: no answer to give
        return h_in
    lay = _heatmap_layout(rr, layers, w_in, h_in, vectors, scale)
    cell_w = (w_in - lay.left - lay.right - (lay.cols - 1) * lay.gap_w) / lay.cols
    map_h = cell_w * lay.region[1] / max(lay.region[0], 1e-9)          # the maps keep their aspect ratio
    need = lay.top + lay.bottom + lay.rows * map_h + (lay.rows - 1) * lay.gap_h + 0.05
    return float(min(h_in, max(need, 0.5)))


PX_PER_CELL = 1.5               # an exported map: at least this many pixels per model cell (every cell visible)


def heatmap_dpi(rr, size: tuple, base: int, cap: int) -> int:
    """The DPI that gives a view's maps, drawn on a `size` (inches) figure, at least PX_PER_CELL pixels per model
    cell, between `base` and `cap` (the user: "when exporting a report or in history, make sure you export the highest
    resolution images available": at 25x a 100 mm board has ~1250 cells across, more than a 130 dpi figure's
    pixels). The maps keep their aspect ratio, so the tighter of the two directions decides."""
    try:
        if rr.kind == "bridge":
            return base
        layers = heatmap_layers(rr)
        if rr.grid is None or not layers:
            return base
        w_in, h_in = size
        lay = _heatmap_layout(rr, layers, w_in, h_in, True, "log")
        cell_w = (w_in - lay.left - lay.right - (lay.cols - 1) * lay.gap_w) / lay.cols
        cell_h = (h_in - lay.top - lay.bottom - (lay.rows - 1) * lay.gap_h) / lay.rows
        nx = max(rr.layer_grids[l].nx for l in layers)
        ny = max(rr.layer_grids[l].ny for l in layers)
        # the panel the map fills: its width, or less when the height limits it (the aspect ratio is kept)
        ratio = lay.region[1] / max(lay.region[0], 1e-9)
        map_w = max(min(cell_w, cell_h / max(ratio, 1e-9)), 0.3)
        need = PX_PER_CELL * max(nx / map_w, ny / max(map_w * ratio, 0.3))
        return int(min(cap, max(base, math.ceil(need))))
    except Exception:                           # noqa: BLE001 - an unusual view: the standard resolution
        return base


def _what(rr) -> str:
    """Which current a view's maps show: a bridge's RMS currents, a DC path, a hot loop or a return."""
    view_of = getattr(rr, "view_of", None)
    if view_of:
        return "RMS" if view_of == "RMS" else "DC"
    return "hot-loop" if rr.kind == "converter" else "return"


def draw_heatmap(fig: Figure, board: Board, rr: RouteResult, issues: Optional[list[Issue]] = None,
                 layer: Optional[str] = None, vmin_frac: float = 1e-3, vectors: bool = True,
                 scale: str = "log", view: Optional[tuple] = None, max_vias: Optional[int] = None,
                 outlines: bool = False, names: bool = False, labels: bool = True) -> None:
    """Return-current density on one plane layer, or side by side on the layers that carry it. With `vectors`,
    arrows show which way the current flows (at its dominant phase) and where it concentrates.

    scale: "log" (three decades below the peak: where the current goes at all) or "linear" (0 to the peak: where
    most of it goes; the faint spread over a whole plane, e.g. through plane capacitance, then stays dark). The
    user, about a log map of plane-capacitance spreading: "it almost looks like magnetic field lines rather than
    current densities". Temperature views are always linear (°C from ambient).

    view: (x0, x1, y0, y1) in board mm, a part of the map to show (the plugin's wheel zoom), with the arrows laid
    out for it; the colour scale stays the whole map's, so colours mean the same at every zoom.

    max_vias: mark at most that many of the vias carrying the current, the busiest (None: all of them).
    outlines, names: the parts' outlines and their references over the maps (_part_outlines).
    labels: the text boxes on the maps, the carrying vias' shares ("63 %") and a power stage's pin letters and
    switch names; the report leaves them out (the user, 2026-10-19: "remove the labels from the report. they are
    actually not helping": on a stage's map they piled up over the switches), the plugin keeps them (False: the
    markers, pad outlines and arrows only).

    A motor bridge's view (kind "bridge") has no map: its drive's currents are drawn instead (draw_bridge)."""
    if rr.kind == "bridge":
        draw_bridge(fig, rr)
        return
    fig.clear()
    layers = heatmap_layers(rr, layer)
    if rr.grid is None or not layers:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, rr.error or "no result", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return
    # the maps' values, a non-finite one (inf, NaN from a failed solve) blanked: one would set the colour scale's
    # peak to inf and fail the whole figure ("Invalid vmin or vmax"); counted on each map's title instead
    values = {l: np.where(np.isfinite(rr.sheet_current[l]), rr.sheet_current[l], np.nan) for l in layers}
    bad = {l: int((rr.layer_grids[l].node & ~np.isfinite(rr.sheet_current[l])).sum()) for l in layers}
    finite = [float(np.nanmax(values[l])) for l in layers if np.isfinite(values[l]).any()]
    peak = (max(finite) if finite else 0.0) or 1.0
    linear = getattr(rr, "heat_scale", "log") == "linear"          # temperatures: °C from ambient up
    if linear:
        lows = [float(np.nanmin(values[l])) for l in layers if np.isfinite(values[l]).any()]
        norm = Normalize(vmin=getattr(rr, "ambient_c", None) or (min(lows) if lows else 0.0), vmax=peak)
    elif scale == "linear":
        norm = Normalize(vmin=0.0, vmax=peak)
    else:
        norm = LogNorm(vmin=peak * vmin_frac, vmax=peak)
    converter = rr.kind in ("converter", "dcload")
    what = _what(rr)
    w_in, h_in = fig.get_size_inches()
    lay = _heatmap_layout(rr, layers, w_in, h_in, vectors, scale)
    head, notes, tick_in, rows, cols = lay.head, lay.notes, lay.tick_in, lay.rows, lay.cols
    gap_w, gap_h = lay.gap_w, lay.gap_h
    fx0, fx1, fy0, fy1 = _frame(fig, lay.left, lay.right, lay.bottom, lay.top)
    im = None
    axes = []
    from ..results.vectors import reference_density
    ref = reference_density([np.where(rr.layer_grids[l].node & np.isfinite(values[l]), values[l], 0.0) for l in layers])
    for n, lname in enumerate(layers):
        # shared axes: zooming or panning one layer shows the same spot on all of them
        ax = fig.add_subplot(rows, cols, n + 1, sharex=axes[0] if axes else None, sharey=axes[0] if axes else None)
        axes.append(ax)
        g = rr.layer_grids[lname]
        K = np.where(g.node, values[lname], np.nan)
        fig._gt_maps = getattr(fig, "_gt_maps", {})          # for the plugin's value-under-the-mouse readout
        fig._gt_maps[ax] = (lname, g, K, getattr(rr, "heat_unit", None) or
                            ("A/mm²" if getattr(rr, "view_of", None) else "A/m per A"))
        # on the log scale copper below its floor, no current at all included, takes the floor's (lowest) colour:
        # LogNorm masks a 0 as invalid and drew it transparent, so a net's idle copper showed the figure's background
        # (white) beside copper at a millionth of the peak in black, like a hole in the map; the readout keeps 0
        shown = np.where(np.isnan(K), np.nan, np.maximum(K, norm.vmin * 0.5)) if isinstance(norm, LogNorm) else K
        im = ax.imshow(shown.T, origin="lower", extent=g.extent, cmap="inferno",
                       norm=norm, interpolation="nearest")
        ax.imshow(np.where(g.node, np.nan, 1.0).T, origin="lower", extent=g.extent, cmap="Greys", vmin=0, vmax=3,
                  alpha=0.35, interpolation="nearest")
        if vectors:
            # arrows wherever the log scale colours the copper: down to its floor, peak / 1000
            _arrows(ax, rr, lname, 45 if len(layers) == 1 else 30, ref, view, floor=peak * vmin_frac)
        _route(ax, board, rr)
        if outlines or names:
            _part_outlines(ax, board, view if view is not None else g.extent, outlines, names)
        _switch_pins(ax, rr, lname, labels)
        carried = _carriers(ax, board, rr, lname, max_vias, labels)
        mine = view_issues(rr, issues or [])
        # issues whose rings would overlap at this view's scale are one ring, in the worst one's colour, with their
        # count beside it (zooming in parts them again). The user, of a thermal map with a ring per hot part (dozens
        # on top of each other, on every layer): "the heatmap is cluttered with circles. we need to find a better way"
        x0, x1, y0, y1 = view if view is not None else g.extent
        groups = cluster_issues(mine, ISSUE_CLUSTER * max(abs(x1 - x0), abs(y1 - y0)))
        for grp in groups:
            top = grp[0]
            colour = SEV_COLOURS.get(top.severity, "k")
            ax.plot(*top.location, linestyle="none", marker="o", ms=8 if len(grp) == 1 else 10, mfc="none",
                    mec=colour, mew=1.8, zorder=4.6)
            if len(grp) > 1:
                ax.annotate(str(len(grp)), top.location, textcoords="offset points", xytext=(6, 4), fontsize=FS_MARK,
                            fontweight="bold", color=colour, zorder=4.7,
                            path_effects=[patheffects.withStroke(linewidth=2.2, foreground="#ffffff")])
        fig._gt_marks = getattr(fig, "_gt_marks", {})        # for the plugin's hover: what a marker is
        fig._gt_marks[ax] = ([(grp[0].location[0], grp[0].location[1], 0, cluster_text(grp)) for grp in groups]
                             + [(t["x"], t["y"], 1, carrier_text(t)) for t in carried])
        # what each marker refers to, in the same order (for the plugin's click: show it in KiCad, mark_ref_at): a
        # group's most severe issue
        fig._gt_mark_refs = getattr(fig, "_gt_mark_refs", {})
        fig._gt_mark_refs[ax] = ([("issue", grp[0]) for grp in groups]
                                 + [("via" if t.get("kind") == "via" else "part", t) for t in carried])
        _outline(ax, board)
        x0, x1, y0, y1 = view if view is not None else g.extent
        ax.set_xlim(x0, x1)
        ax.set_ylim(y1, y0)  # KiCad y axis points down
        ax.set_aspect("equal")
        _place_labels(ax)
        ax.tick_params(labelsize=FS_TICK)
        share = rr.layer_share.get(lname)
        blank = f", {bad[lname]} value(s) not finite: blank" if bad[lname] else ""
        if linear:
            hot = float(np.nanmax(K)) if np.isfinite(K).any() else float("nan")
            ax.set_title(f"{lname}  (max {hot:.1f} °C{blank})", fontsize=FS_TEXT)
        else:
            ax.set_title(f"{lname}" + (f"  ({share * 100:.0f} % of {what} current{blank})" if share is not None
                                       else (f"  ({blank[2:]})" if blank else "")), fontsize=FS_TEXT)
        if rr.kind == "thermal" and rr.hotspot_layer == lname:     # the thermal hot spot
            ax.plot(*rr.hotspot, marker="x", ms=10, mew=2, color="#00e5ff")
        # a DC path's or a bridge's RMS density map: its hottest sustained spot (where its density tile's click zooms;
        # the RMS map had none before 2026-10-21)
        spot = hot_spot(rr) if getattr(rr, "view_of", None) else None
        if spot and spot["layer"] == lname:
            ax.plot(spot["x"], spot["y"], marker="x", ms=9, mew=2, color="#00e5ff")
    fig.suptitle("\n".join(head), fontsize=FS_TITLE, y=1 - 0.08 / h_in, va="top")
    fig._gt_note = fig.text(0.5, 1 - (0.08 + _lh(FS_TITLE, len(head))) / h_in, "\n".join(notes), ha="center",
                            va="top", fontsize=FS_TEXT)
    # Fixed layout with a strip of its own for the colour bar: a colour bar that takes its space from the maps
    # (colorbar(ax=...)) ends up on top of them as soon as the caller adjusts the subplots again.
    cell_w = max(((fx1 - fx0) * w_in - (cols - 1) * gap_w) / cols, 0.2)
    cell_h = max(((fy1 - fy0) * h_in - (rows - 1) * gap_h) / rows, 0.2)
    fig.subplots_adjust(left=fx0, right=fx1, bottom=fy0, top=fy1, wspace=gap_w / cell_w, hspace=gap_h / cell_h)
    for ax in axes:                              # the maps shrink to their aspect ratio: match the bar to them
        ax.apply_aspect()
    x0 = min(ax.get_position().x0 for ax in axes)
    x1 = max(ax.get_position().x1 for ax in axes)
    y0 = min(ax.get_position().y0 for ax in axes)
    y1 = max(ax.get_position().y1 for ax in axes)
    handles, labels = axes[0].get_legend_handles_labels()
    uniq = dict(zip(labels, handles))
    if uniq:          # under the maps' tick labels (under the first map it ran into a second row's titles)
        fig.legend(uniq.values(), uniq.keys(), loc="upper center", bbox_to_anchor=((x0 + x1) / 2,
                   y0 - (tick_in + 0.04) / h_in), fontsize=FS_TICK, ncol=min(len(uniq), 8), frameon=False,
                   borderaxespad=0.0, borderpad=0.2, handlelength=1.5, handletextpad=0.5, columnspacing=1.5)
    cb = fig.colorbar(im, cax=fig.add_axes((x1 + 0.18 / w_in, y0, 0.14 / w_in, max(y1 - y0, 0.2))))
    cb.minorticks_off()
    cb.ax.tick_params(labelsize=FS_TICK)
    cb.set_label(getattr(rr, "heat_label", None) or
                 ("|K| (A/m per A of " + ("loop current)" if converter else "signal)")), fontsize=FS_TEXT)


# the emission panels: (rows attribute, reading key, limit key, unit, colour)
EMISSION_PANELS = {"ce101": ("ce101", "i_dbua", "limit_dbua", "dBµA", "#8c564b"),
                   "conducted": ("conducted", "v_dbuv", "limit_dbuv", "dBµV", "#9467bd"),
                   "radiated": ("emissions", "e_dbuv", "limit_dbuv", "dBµV/m", "#1f77b4")}


def _converter_spectrum(fig: Figure, c) -> None:
    """The input lead's current below 10 kHz (CE101, when set), the conducted readings at the input (vs the chosen
    standard) and the hot loop's radiated estimate."""
    panels = [p for p in EMISSION_PANELS if getattr(c, EMISSION_PANELS[p][0], None)]
    if not panels:
        fig.add_subplot(111).set_axis_off()
        return
    w_in, h_in = fig.get_size_inches()
    left_in, right_in = _lh(FS_TEXT) + _width_in("−100", FS_TICK) + 0.24, 0.25
    gap = left_in + 0.25
    cell = max((w_in - left_in - right_in - (len(panels) - 1) * gap) / len(panels), 0.2)
    header = 0.0
    for k, kind in enumerate(panels):
        ax = fig.add_subplot(1, len(panels), k + 1)
        attr, key, lim_key, unit, colour = EMISSION_PANELS[kind]
        rows = getattr(c, attr)
        f = np.array([r["f"] for r in rows]) / 1e6
        v = np.array([r[key] for r in rows])
        ax.vlines(f, v.min() - 10, v, color=colour, lw=0.8)
        ax.plot(f, v, "o", ms=2, color=colour, label="estimate")
        lim = np.array([np.nan if r[lim_key] is None else r[lim_key] for r in rows])
        if np.isfinite(lim).any():
            ax.plot(f, lim, "-", lw=1.2, color="#d62728", label="limit")   # breaks where no limit applies
        if kind != "ce101":
            _band_marks(ax, c, f, radiated=kind == "radiated")
        _freq_axis(ax, f)
        ax.set_ylabel(unit, fontsize=FS_TEXT)
        ax.tick_params(labelsize=FS_TICK, which="both")
        ax.grid(True, which="both", alpha=0.3)
        title = {"ce101": f"input lead current, {getattr(c, 'ce101_limit', '')}",
                 "conducted": f"conducted, {c.conducted_standard}",
                 "radiated": "radiated by the hot loop"}[kind]
        header = max(header, _title_and_key(ax, title, cell, FS_TEXT))
    fig.suptitle(f"{c.net}: emission estimate", fontsize=FS_TITLE, y=1 - 0.06 / h_in, va="top")
    _adjust(fig, left_in, right_in, 0.10 + _lh(FS_TICK) + 0.08, 0.06 + _lh(FS_TITLE) + 0.04 + header,
            wspace=gap / cell)


def _thermal_parts(fig: Figure, t) -> None:
    """The thermal solve's hottest parts as bars, with the warning and error limits."""
    ax = fig.add_subplot(111)
    parts = t.parts[:12]
    if not parts:
        ax.text(0.5, 0.5, "no heated parts", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return
    names = [p["ref"] for p in parts]
    temps = [p["t_junction"] or p["t_c"] for p in parts]
    colours = ["#d62728" if v >= t.max_c else "#ff9f1c" if v >= t.warn_c else "#2ca02c" for v in temps]
    ax.bar(names, temps, color=colours)
    for x, (v, p) in enumerate(zip(temps, parts)):
        ax.text(x, v, f"{v:.0f} °C" + (f"\n{p['power_w']:.2g} W" if p["power_w"] else ""), ha="center",
                va="bottom", fontsize=FS_TICK)
    ax.axhline(t.warn_c, color="#ff9f1c", lw=1, ls="--", label=f"warning {t.warn_c:g} °C")
    ax.axhline(t.max_c, color="#d62728", lw=1, ls="--", label=f"limit {t.max_c:g} °C")
    ax.set_ylabel("°C", fontsize=FS_TEXT)
    ax.tick_params(labelsize=FS_TICK)
    w_in, h_in = fig.get_size_inches()
    left_in, right_in, bottom_in = _lh(FS_TEXT) + _width_in("100", FS_TICK) + 0.24, 0.2, 0.10 + _lh(FS_TICK) + 0.08
    top_in = 0.06 + _title_and_key(ax, "hottest parts: junction (where θJB is set) or case temperature",
                                   w_in - left_in - right_in)
    x0, x1, y0, y1 = _frame(fig, left_in, right_in, bottom_in, top_in)
    fig.subplots_adjust(left=x0, right=x1, bottom=y0, top=y1)
    # headroom for the labels over the bars (two lines): the highest one ends under the frame
    room = min((_lh(FS_TICK, 2) + 0.04) / max((y1 - y0) * h_in, 0.1), 0.8)
    hi = max(max(temps), t.max_c)
    ax.set_ylim(t.ambient_c, max(hi, (max(temps) - room * t.ambient_c) / (1 - room)) + 0.02 * (hi - t.ambient_c))


def _band_marks(ax, rr, f_mhz, radiated: bool = True) -> None:
    """Mark where a spectrum's numbers are not plain results (RouteResult.band): grey above the board's
    quasi-static limit (radiated, with the board-wide model: indicative upper bounds [A47]) and hatched from
    analysis.max_frequency to the edge's own band top (not computed, so not checked)."""
    band = getattr(rr, "band", None) or {}
    if not len(f_mhz):
        return
    top = float(np.max(f_mhz))
    f_qs = band.get("f_qs")
    if radiated and f_qs and f_qs / 1e6 < top:
        ax.axvspan(f_qs / 1e6, top * 1.05, color="#888888", alpha=0.12, lw=0,
                   label="indicative")
    if band.get("capped") and band["natural"] > band["f_max"]:
        ax.axvspan(band["f_max"] / 1e6, band["natural"] / 1e6, facecolor="none", edgecolor="#888888", hatch="//",
                   lw=0, label="not evaluated")


def draw_spectrum(fig: Figure, rr: RouteResult) -> None:
    """The view's spectrum plot: a route's harmonics against the CISPR 32 limit; a power stage's radiated and
    conducted estimates; a bridge's conducted readings; a thermal view's hottest parts instead."""
    if rr.kind == "bridge":
        _bridge_spectrum(fig, rr)
        return
    fig.clear()
    if rr.kind == "thermal":
        _thermal_parts(fig, rr)
        return
    if rr.kind == "converter":
        _converter_spectrum(fig, rr)
        return
    ax = fig.add_subplot(111)
    rows = rr.emissions
    if not rows:
        ax.set_axis_off()
        return
    f = np.array([r["f"] for r in rows]) / 1e6
    e = np.array([r["e_dbuv"] for r in rows])
    ax.vlines(f, e.min() - 10, e, color="#1f77b4", lw=1.2)
    ax.plot(f, e, "o", ms=3, color="#1f77b4", label="harmonics")
    lim = [(r["f"] / 1e6, r["limit_dbuv"]) for r in rows if r["limit_dbuv"] is not None]
    if lim:
        lf, lv = zip(*lim)
        ax.step(lf, lv, where="mid", color="#d62728", label="CISPR 32 limit")
    _band_marks(ax, rr, f)
    _freq_axis(ax, f)
    ax.set_ylabel("dBµV/m", fontsize=FS_TEXT)
    ax.tick_params(labelsize=FS_TICK, which="both")
    ax.grid(True, which="both", alpha=0.3)
    # fixed margins (tight_layout costs a draw), in inches: in the plugin the spectrum can share its row with the
    # cross-section and be under 3 inches wide
    w_in, h_in = fig.get_size_inches()
    left_in, right_in = _lh(FS_TEXT) + _width_in("−100", FS_TICK) + 0.24, 0.25
    header = _title_and_key(ax, f"{rr.net}: differential-mode emission estimate", w_in - left_in - right_in)
    _adjust(fig, left_in, right_in, 0.10 + _lh(FS_TICK) + 0.08, header + 0.06)


SECTION_NONE = "no cross-section: this view has no signal route"
ROW_IN = 0.17           # the side view's least height per copper layer (its 8.5 pt label and some air)
SHARE_MIN_IN = 0.40     # the share plot's least height: with less room it is left out
GAP_IN = 0.08           # between the share plot and the side view
TICKS_IN = 0.10 + _lh(FS_TICK) + 0.06       # under the side view: the ticks and their labels


def _section_rows(board: Board, sec: dict) -> list[str]:
    """The side view's rows: the copper layers the section touches, in stack order."""
    used = set(sec["layers"]) | {r[2] for r in sec["signal"]} | {l for _, a, b in sec["vias"] for l in (a, b)}
    return [l for l in board.copper_layers if l in used] + sorted(used - set(board.copper_layers))


def _section_caption(half_mm: float, width_in: float) -> list[str]:
    """The cross-section's caption (SECTION_CAPTION) as lines for a figure width_in wide (wrapped 15 % short of the
    width: _width_in estimates this font size a little narrow)."""
    return _wrap(SECTION_CAPTION.format(w=half_mm, w2=2 * half_mm), 0.85 * (width_in - 0.3), FS_TICK)


def section_height_in(board: Board, rr, width_in: Optional[float] = None) -> float:
    """The figure height (inches) draw_section wants: the title (a second line for a plane displacement), a share
    plot of about an inch, 0.3 in per copper layer in the side view, the distance axis with its label, and for a
    figure width_in wide its caption (the plugin's scrolling Results tab, the report, the CLI's PNGs). 0: the view
    has no section."""
    from ..analysis.simulate import displacement
    if rr.kind == "bridge":                         # the capacitors' ripple bars (none on the sweep view)
        return 2.8 if getattr(rr, "caps", None) and getattr(rr, "view", "") != "sweep" else 0.0
    sec = getattr(rr, "section", None) or {}
    if not sec or rr.kind in ("converter", "thermal"):
        return 0.0
    lines = 2 if displacement(rr.transfers) is not None else 1
    caption = (_lh(FS_TICK, len(_section_caption(sec["half_mm"], width_in))) + 0.10) if width_in else 0.0
    return 0.06 + _lh(FS_TITLE, lines) + 0.08 + 1.0 + GAP_IN + len(_section_rows(board, sec)) * 0.30 + TICKS_IN + \
        _lh(FS_TEXT) + 0.02 + caption


def draw_section(fig: Figure, board: Board, rr) -> None:
    """The return current along the route, layer by layer (RouteResult.section, analysis/section.py), driver on
    the left. Top: each plane layer's share of the return passing each point of the route within ±half_mm of the
    trace, and their sum (where it dips the return has left the route). Bottom: a side view, the copper layers top
    to bottom (equal rows: the real thicknesses differ too much to read), each plane row coloured by its share, the
    signal on its layers with its layer changes, and the vias / parts carrying the return between layers (green,
    with their share; dashed and with their distance when they are more than two corridor widths from the route).
    Shares are the part in phase with the signal (the excitation is real)."""
    from ..analysis.section import station_at
    from ..analysis.simulate import displacement, point_transfers
    from .formats import share
    if rr.kind == "bridge":
        draw_bridge_caps(fig, rr)
        return
    fig.clear()
    sec = getattr(rr, "section", None) or {}
    if not sec or rr.kind in ("converter", "thermal"):
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, SECTION_NONE, ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return
    s = np.asarray(sec["s_mm"], float)
    I = np.asarray(sec["I"]).real
    planes, W, L = list(sec["layers"]), sec["half_mm"], sec["length_mm"]
    rows = _section_rows(board, sec)
    row = {l: i for i, l in enumerate(rows)}
    # Margins in inches from the text they hold, so that it fits at any size: in the plugin the section shares a
    # row with the spectrum and can be under 5 x 1.5 in. With little height the axis label moves into the title,
    # then the share plot goes (the side view's colours show the same shares).
    w_in, h_in = fig.get_size_inches()
    head = f"{rr.net}: return along the route @ {sec['f'] / 1e6:.4g} MHz, ±{W:.1f} mm, in phase"
    xlabel = f"mm along the route, {rr.driver} → {rr.receiver}"
    cap = displacement(rr.transfers)
    extra = (f"+ {share(cap['fraction'])} displacement {'–'.join(cap['layers'])} @ {cap.get('f', 0) / 1e6:.4g} "
             "MHz (board-wide model, spread over the pair)") if cap is not None and h_in >= 2.6 else ""
    rows_in = len(rows) * ROW_IN
    # what the plots show, under them (the user: "can you add a description at the bottom that explains the plots
    # especially the green lines"); left out first when the figure is small
    caption = _section_caption(W, w_in)

    def layout(labelled: bool, described: bool):
        """(title lines, top and bottom margins, height left for the share plot), with the x-axis label on its
        own line or (labelled False, to save a line) in the title, and (described) the caption under it all."""
        lines = _wrap(head if labelled else f"{head}; x: {xlabel}", w_in - 0.3, FS_TITLE)
        lines += _wrap(extra, w_in - 0.3, FS_TITLE) if extra else []
        top_in = 0.06 + _lh(FS_TITLE, len(lines)) + 0.08
        bottom_in = TICKS_IN + (_lh(FS_TEXT) + 0.02 if labelled else 0.0) + \
            (_lh(FS_TICK, len(caption)) + 0.10 if described else 0.0)
        return lines, top_in, bottom_in, h_in - top_in - bottom_in - rows_in    # what is left for the share plot
    labelled = described = True
    for labelled, described in ((True, True), (False, True), (True, False), (False, False)):
        lines, top_in, bottom_in, spare = layout(labelled, described)
        if spare >= SHARE_MIN_IN + GAP_IN:
            break
    left_in = max([_width_in(r, FS_TICK) for r in rows] + [_width_in("100 %", FS_TICK)]) + 0.34
    right_in = 0.12 + 0.10 + 0.08 + _width_in("−100", FS_TICK) + 0.08 + _lh(FS_TEXT) + 0.06
    x0, x1, y0, y1 = _frame(fig, left_in, right_in, bottom_in, top_in)
    top = None
    if spare >= SHARE_MIN_IN + GAP_IN:          # 40 % of the height, at least SHARE_MIN_IN, ROW_IN per row left
        share_h = max(SHARE_MIN_IN, min((y1 - y0) * h_in * 0.4, spare - GAP_IN)) / h_in
        side_h = y1 - y0 - share_h - GAP_IN / h_in
        top = fig.add_axes((x0, y0 + side_h + GAP_IN / h_in, x1 - x0, share_h))
        side = fig.add_axes((x0, y0, x1 - x0, side_h), sharex=top)
    else:
        side = fig.add_axes((x0, y0, x1 - x0, y1 - y0))

    if top is not None:
        for k, lname in enumerate(planes):
            if np.abs(I[k]).max() >= 0.02:            # a layer carrying nothing would only crowd the plot
                top.plot(s, I[k] * 100, color=layer_colour(board, lname), lw=1.2)
        total = I.sum(axis=0) * 100
        top.plot(s, total, color="#808080", lw=1.6, ls=(0, (4, 2)))      # dashed: a layer's line may be grey too
        top.annotate("Σ", (1.0, total[-1]), xycoords=("axes fraction", "data"), xytext=(3, 0),
                     textcoords="offset points", va="center", fontsize=FS_TICK)
        top.axhline(100, color="#808080", lw=0.6, ls=":")
        top.set_ylim(min(-10.0, float(I.min()) * 100 - 5), max(110.0, float(total.max()) + 5))
        top.set_yticks([0, 100])                     # the unit on the labels: no axis label
        top.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{v:g} %"))
        top.tick_params(labelsize=FS_TICK, labelbottom=False)
        top.grid(True, alpha=0.3)

    edges = np.concatenate([[0.0], (s[1:] + s[:-1]) / 2, [L]])
    norm = Normalize(vmin=-100, vmax=100)
    mesh = None
    for k, lname in enumerate(planes):
        if np.abs(I[k]).max() < 0.02:              # carries nothing: no band (the scale's white middle glared in
            continue                                # the dark theme)
        y = row[lname]
        mesh = side.pcolormesh(edges, [y - 0.42, y + 0.42], (I[k] * 100)[None, :], cmap="RdBu_r", norm=norm,
                               shading="flat")
    halo = [patheffects.withStroke(linewidth=4.6, foreground="#101018")]   # reads on any band colour (F.Cu's red
    for s0, s1, lname in sec["signal"]:                                     # signal on a red band vanished)
        side.plot([s0, s1], [row[lname]] * 2, color=layer_colour(board, lname), lw=2.6, solid_capstyle="butt",
                  zorder=3, path_effects=halo)
    # what each bar is, for the plugin's hover and click (mark_at / mark_ref_at: show it in KiCad), a mark on every
    # row the bar spans so a click anywhere along it finds it; where a green bar is drawn over a grey one (a stitching
    # via beside the signal's projects onto the same station) the green one, on top, wins
    marks, refs = [], []
    for k, (sv, a, b) in enumerate(sec["vias"]):
        side.plot([sv, sv], [row[a], row[b]], color="#b0b0b0", lw=2.0, zorder=3, path_effects=halo)
        at_xy = (sec.get("via_at") or [])[k] if k < len(sec.get("via_at") or []) else None
        if at_xy is not None:
            x_, y_, kind = at_xy
            what = {"x": x_, "y": y_, "name": f"signal {'pad' if kind == 'pad' else 'via'} {a} → {b} "
                                               f"@({x_:.2f}, {y_:.2f})"}
            for r in np.arange(min(row[a], row[b]), max(row[a], row[b]) + 0.01, 0.5):   # every half row
                marks.append((sv, float(r), 1, what["name"]))
                refs.append(("via", what))
    placed = []
    for t in point_transfers(rr.transfers)[:SECTION_CARRIERS]:      # the biggest: a side view has room for a few
        at = station_at(t["x"], t["y"], rr.route_xy)
        ys = [row[l] for l in t["layers"] if l in row]
        if at is not None and ys:
            placed.append((at[0], at[1], min(ys), max(ys), t))
    span_in = max((x1 - x0) * w_in, 1e-6)
    taken: list[list[tuple[float, float]]] = []        # per label row: the stretches its labels take (mm)
    for sp, dist, r0, r1, t in sorted(placed, key=lambda p: p[0]):
        near = dist <= 2 * W
        side.plot([sp, sp], [r0 - 0.3, r1 + 0.3], color=CARRIER, lw=4.0 if near else 2.0, ls="-" if near else "--",
                  solid_capstyle="round", zorder=4)
        for r in np.arange(r0, r1 + 0.01, 0.5):                                          # every half row
            marks.append((sp, float(r), 0, carrier_text(t) + ("" if near else f", {dist:.0f} mm from the route")))
            refs.append(("via" if t.get("kind") == "via" else "part", t))
        if t["fraction"] < MIN_LABEL:
            continue
        text = share(t["fraction"]) + ("" if near else f", {dist:.0f} mm away")
        width = (_width_in(text, FS_MARK, bold=True) + 0.2) / span_in * L      # with its box and offset, in mm
        leftward = sp + width > L and sp - width >= 0                           # near the receiver: left of the bar
        a, b = (sp - width, sp) if leftward else (sp, sp + width)
        level = next((k for k, spans in enumerate(taken) if all(b <= u or a >= v for u, v in spans)), len(taken))
        if level == len(taken):
            taken.append([])
        taken[level].append((a, b))
        # inside the side view, beside the bar, one row lower for each label that would overlap one before; a clear
        # gap from the bar (half its width + 4 pt), or the green bar and the box's green edge read as a "1" ("36 %"
        # looked like "136 %")
        gap = (4.0 if near else 2.0) / 2 + 4
        side.annotate(text, (sp, r0 - 0.3), textcoords="offset points", xytext=(-gap if leftward else gap,
                      -2 - FS_MARK * 1.6 * level), va="top", ha="right" if leftward else "left", fontsize=FS_MARK,
                      color="#ffffff", fontweight="bold", zorder=5,
                      bbox={"boxstyle": "round,pad=0.15", "fc": "#101018", "ec": CARRIER, "lw": 0.6, "alpha": 0.8})
    side.set_yticks(range(len(rows)))
    side.set_yticklabels(rows, fontsize=FS_TICK)
    side.tick_params(axis="y", pad=10)                  # room for the layer's colour swatch
    swatch = blended_transform_factory(side.transAxes, side.transData) + \
        ScaledTranslation(-5.5 / 72, 0, fig.dpi_scale_trans)
    for lname, y in row.items():                        # the colour of the layer's line in the top plot (a legend
        side.plot([0.0], [y], marker="s", ms=6, linestyle="none", color=layer_colour(board, lname),   # would not
                  transform=swatch, clip_on=False)                                                  # fit narrow)
    side.set_ylim(len(rows) - 0.5, -0.5)               # top layer at the top
    side.set_xlim(0.0, L)
    if labelled:
        side.set_xlabel(xlabel, fontsize=FS_TEXT, labelpad=2)
    side.tick_params(labelsize=FS_TICK)
    fig.suptitle("\n".join(lines), fontsize=FS_TITLE, y=1 - 0.06 / h_in, va="top")
    if described:
        fig.text(0.15 / w_in, 0.06 / h_in, "\n".join(caption), ha="left", va="bottom", fontsize=FS_TICK,
                 linespacing=1.25)
    if mesh is not None:
        pos = side.get_position()
        cb = fig.colorbar(mesh, cax=fig.add_axes((pos.x1 + 0.12 / w_in, pos.y0, 0.10 / w_in, pos.height)),
                          extend="both")
        cb.set_ticks([-100, 0, 100])
        cb.set_label("% of return", fontsize=FS_TEXT)
        cb.ax.tick_params(labelsize=FS_TICK)
    fig._gt_section = {ax: (s, I, planes, W) for ax in (top, side) if ax is not None}   # the value under the mouse
    fig._gt_marks = {side: marks}
    fig._gt_mark_refs = {side: refs}


def section_value(hit, x: float) -> str:
    """'s 12.3 mm   In1.Cu 87 %   In2.Cu 9 %   sum 97 % within ±4.7 mm' at the station nearest x (draw_section
    records (s, I, layers, half width) per axes as fig._gt_section)."""
    s, I, layers, W = hit
    k = int(np.argmin(np.abs(s - x)))
    parts = [f"{l} {I[n, k] * 100:.0f} %" for n, l in enumerate(layers) if abs(I[n, k]) >= 0.005]
    return f"s {s[k]:.1f} mm   " + "   ".join(parts) + f"   sum {I[:, k].sum() * 100:.0f} % within ±{W:.1f} mm"


def draw_board(fig: Figure, board: Board, routes: list[RouteResult], issues: list[Issue]) -> None:
    """An overview of the board: its outline, the analysed routes and numbered markers at the live issues."""
    fig.clear()
    ax = fig.add_subplot(111)
    _outline(ax, board)
    for rr in routes:
        _route(ax, board, rr)
    ax.autoscale_view()
    for k, i in enumerate(issues, 1):
        if i.waived:
            continue
        ax.plot(*i.location, marker="o", ms=10, mfc="none", mec=SEV_COLOURS.get(i.severity, "k"), mew=2)
        ax.annotate(str(k), i.location, textcoords="offset points", xytext=(6, 6), fontsize=FS_TICK)
    ax.set_aspect("equal")
    ax.invert_yaxis()
    ax.tick_params(labelsize=FS_TICK)
    ax.set_title("Analysed routes and issues", fontsize=FS_TITLE)
    fig.tight_layout()


# --------------------------------------------------------------------------- motor bridges
BRIDGE_WAVE_IN = 4.2        # the drive's waveforms on the plugin's page
SECTION_CARRIERS = 8             # vias and parts drawn in the cross-section's side view at most
# the cross-section's caption (draw_section; {w}: the corridor's half width, mm)
SECTION_CAPTION = (
    "Top: each plane layer's share of the return current flowing under the route (within ±{w:.1f} mm of the track), "
    "the part in phase with the signal; dashed Σ: their sum (below 100 %, the return has left the corridor). "
    "Side view: the copper layers top to bottom, each band coloured by that share (red: carrying the return, blue: "
    "flowing the other way); the outlined line is the signal track on its layer, the grey bars its own vias. "
    "Green bars: the vias and parts (decaps, 0 Ω links) that carry the return current from one layer to another, "
    "with the share of the signal current through each: solid within {w2:.1f} mm of the route, dashed with their "
    "distance when farther away. A layer change without a green bar near it means the return found no nearby path "
    "between the layers.")
PHASE_COLOURS = ("#1f77b4", "#ff7f0e", "#2ca02c")


def _decimate(t: np.ndarray, y: np.ndarray, n: int = 6000) -> tuple[np.ndarray, np.ndarray]:
    """At most ~2n points, keeping each bucket's minimum and maximum (a PWM current's envelope stays)."""
    if len(t) <= 2 * n:
        return t, y
    k = int(math.ceil(len(t) / n))
    m = len(t) // k * k
    tb, yb = t[:m].reshape(-1, k), y[:m].reshape(-1, k)
    lo, hi = yb.argmin(axis=1), yb.argmax(axis=1)
    rows = np.arange(len(tb))
    first = np.minimum(lo, hi)
    second = np.maximum(lo, hi)
    tt = np.column_stack([tb[rows, first], tb[rows, second]]).ravel()
    yy = np.column_stack([yb[rows, first], yb[rows, second]]).ravel()
    return tt, yy


def draw_bridge_sweep(fig: Figure, rr) -> None:
    """A motor bridge's sweep: the parts' and the copper's loss, the sustained RMS current density, the conducted
    and the radiated margin against the phase current, one line per speed (against the speed when only speeds
    vary). A hollow marker: the current is not reached."""
    fig.clear()
    rows = [r for r in rr.sweep if not r.get("error")]
    w_in, h_in = fig.get_size_inches()
    if not rows:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, "no operating point of the sweep could be driven", ha="center", va="center",
                transform=ax.transAxes, fontsize=FS_TEXT)
        ax.set_axis_off()
        return
    by_current = len({r["i_phase"] for r in rows}) > 1
    x_key, other, x_label = (("i_phase", "speed_rpm", "phase current (A)") if by_current
                             else ("speed_rpm", "i_phase", "speed (rpm)"))
    groups = sorted({r[other] for r in rows})
    title = f"{rr.net}: sweep over {len(rr.sweep)} operating points (from the operating point's solves)"
    head = _wrap(title, w_in - 0.3, FS_TITLE)
    top_in = 0.08 + _lh(FS_TITLE, len(head)) + 0.15 + _lh(FS_TEXT)
    left_in, right_in = _lh(FS_TEXT) + _width_in("−100", FS_TICK) + 0.3, 0.25
    bottom_in = 0.10 + _lh(FS_TICK) + 0.1 + _lh(FS_TEXT)
    x0, x1, y0, y1 = _frame(fig, left_in, right_in, bottom_in, top_in)
    gap_x, gap_y = (left_in + 0.2) / w_in, (0.3 + _lh(FS_TEXT)) / h_in
    ww, hh = (x1 - x0 - gap_x) / 2, (y1 - y0 - gap_y) / 2
    panels = (("losses (W)", [("loss_w", "-", "parts"), ("copper_w", "--", "copper")], None),
              ("RMS current density (A/mm², sustained)", [("sustained_a_mm2", "-", None)], None),
              ("conducted margin (dB)" + (", dotted: CE101" if any(r.get("ce101_margin_db") is not None
                                                                   for r in rows) else ""),
               [("conducted_margin_db", "-", "conducted"), ("ce101_margin_db", ":", "CE101")], 0.0),
              ("radiated margin (dB)", [("radiated_margin_db", "-", None)], 0.0))
    unit = " A" if other == "i_phase" else " rpm"
    for k, (label, series, ref) in enumerate(panels):
        row, col = divmod(k, 2)
        ax = fig.add_axes((x0 + col * (ww + gap_x), y1 - (row + 1) * hh - row * gap_y, ww, hh))
        for g_i, g in enumerate(groups):
            pts = sorted((r for r in rows if r[other] == g), key=lambda r: r[x_key])
            colour = PHASE_COLOURS[g_i % len(PHASE_COLOURS)] if g_i < 3 else f"C{g_i}"
            for key, ls, what in series:
                xy = [(r[x_key], r[key], r["reached"]) for r in pts if r.get(key) is not None]
                if not xy:
                    continue
                name = f"{g:g}{unit}" + (f" {what}" if what and len(series) > 1 else "")
                ax.plot([p[0] for p in xy], [p[1] for p in xy], ls=ls, color=colour, lw=1.2,
                        label=name if (row, col) == (0, 0) or (len(series) == 1 and (row, col) == (0, 1)) else None)
                ax.scatter([p[0] for p in xy], [p[1] for p in xy], s=18, zorder=3,
                           facecolors=[colour if p[2] else "none" for p in xy], edgecolors=colour)
        if ref is not None:
            ax.axhline(ref, color="#d62728", lw=1.0, ls="--")
        ax.set_title(label, fontsize=FS_TEXT, loc="left")
        ax.grid(True, alpha=0.3)
        ax.tick_params(labelsize=FS_TICK)
        if row == 1:
            ax.set_xlabel(x_label, fontsize=FS_TEXT, labelpad=2)
        if k == 0 and ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=FS_TICK, frameon=False, loc="upper left")
    fig.suptitle("\n".join(head), fontsize=FS_TITLE, y=1 - 0.06 / h_in, va="top")


def draw_bridge(fig: Figure, rr) -> None:
    """A motor bridge's six-step drive over one electrical period: the phase currents (top) and the bus current
    the legs draw from the capacitors and the input (bottom), and beside them a zoom on the PWM periods around
    a commutation (where the outgoing phase's current runs out through reverse conduction). Its sweep view:
    draw_bridge_sweep."""
    if getattr(rr, "view", "") == "sweep":
        draw_bridge_sweep(fig, rr)
        return
    fig.clear()
    w = getattr(rr, "waveform", None)
    if rr.error or w is None:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, rr.error or "no drive result", ha="center", va="center", transform=ax.transAxes,
                wrap=True, fontsize=FS_TEXT)
        ax.set_axis_off()
        return
    w_in, h_in = fig.get_size_inches()
    op = rr.operating
    title = (f"{rr.net}: six-step {op['pwm']}, {op['v_bus']:g} V, {op['f_pwm'] / 1e3:g} kHz PWM, {op['mode']} "
             f"{op['i_reached']:.4g} A at {op['speed_rpm']:g} rpm, duty {op['duty']:.3f}")
    head = _wrap(title, w_in - 0.3, FS_TITLE)
    top_in = 0.08 + _lh(FS_TITLE, len(head)) + 0.1 + _lh(FS_TEXT)
    left_in, right_in, bottom_in = _lh(FS_TEXT) + _width_in("−100", FS_TICK) + 0.3, 0.25, 0.10 + _lh(FS_TICK) + 0.1 + \
        _lh(FS_TEXT)
    x0, x1, y0, y1 = _frame(fig, left_in, right_in, bottom_in, top_in)
    gap_x, gap_y = 0.75 / w_in, (0.25 + _lh(FS_TEXT)) / h_in
    wide = (x1 - x0 - gap_x) * 0.68
    hh = (y1 - y0 - gap_y) / 2
    scale, unit = (1e3, "ms") if w.period >= 1e-3 else (1e6, "µs")
    t = w.t * scale
    bus = w.hs.sum(axis=1)
    # the zoom: three PWM periods around the first commutation
    m = max(w.n // 6, 1)
    z0, z1 = (m - 1) * w.T * scale, (m + 2) * w.T * scale
    axes = {}
    for row, (what, ys, label) in enumerate((("phases", [w.i[:, k] for k in range(3)], "phase current (A)"),
                                             ("bus", [bus], "bus current (A)"))):
        yb = y1 - (row + 1) * hh - row * gap_y
        for col, (xa, xw) in enumerate(((x0, wide), (x0 + wide + gap_x, x1 - x0 - wide - gap_x))):
            ax = fig.add_axes((xa, yb, xw, hh), sharex=axes.get((0, col)))
            axes[(row, col)] = ax
            for k, y in enumerate(ys):
                tt, yy = (t, y) if col == 1 else _decimate(t, y)
                colour = PHASE_COLOURS[k] if what == "phases" else "#555555"
                ax.plot(tt, yy, color=colour, lw=1.0 if col == 1 else 0.8,
                        label=f"phase {'ABC'[k]}" if what == "phases" and col == 0 else None)
            if what == "bus":
                ax.axhline(rr.drive.get("bus_mean", 0.0), color="#d62728", lw=1.0, ls="--",
                           label="mean" if col == 0 else None)
            ax.grid(True, alpha=0.3)
            ax.tick_params(labelsize=FS_TICK)
            if col == 1:
                ax.set_xlim(z0, z1)
            else:
                ax.set_xlim(0, w.period * scale)
                ax.set_ylabel(label, fontsize=FS_TEXT)
            if row == 0:
                ax.tick_params(labelbottom=False)
                ax.set_title("one electrical period" if col == 0 else "zoom: a commutation", fontsize=FS_TEXT,
                             loc="left")
            else:
                ax.set_xlabel(unit, fontsize=FS_TEXT, labelpad=2)
    for ax in (axes[(0, 0)], axes[(1, 0)]):         # above the plot, on the right: over no data
        ax.legend(fontsize=FS_TICK, loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=4, frameon=False,
                  handlelength=1.5, borderaxespad=0.1, borderpad=0.1)
    fig.suptitle("\n".join(head), fontsize=FS_TITLE, y=1 - 0.06 / h_in, va="top")


def draw_bridge_caps(fig: Figure, rr) -> None:
    """The bus capacitors' ripple current (RMS) at the operating point, the input (LISN) with them; with victims
    (analysis/victims.py), beside it each victim's peak against its threshold."""
    fig.clear()
    caps = [c for c in getattr(rr, "caps", []) if c.get("rms_a", 0) > 0]
    victims = [v for v in getattr(rr, "victims", []) if v.get("checked")]
    if not caps and not victims:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, "no capacitor currents", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return
    names = [("input (LISN)" if c["lisn"] else c["name"]) for c in caps]
    vnames = [_victim_label(v) for v in victims]
    lab_in = max(_width_in(n, FS_TICK) for n in names + vnames)
    left_in, right_in = _lh(FS_TEXT) + _width_in("100", FS_TICK) + 0.3, 0.25
    bottom_in, top_in = 0.12 + lab_in + 0.1, 0.1 + _lh(FS_TITLE) + 0.08
    x0, x1, y0, y1 = _frame(fig, left_in, right_in, bottom_in, top_in)
    split = x0 + (x1 - x0) * (0.55 if caps and victims else (1.0 if caps else 0.0))
    gap = (left_in / fig.get_size_inches()[0]) if caps and victims else 0.0
    if caps:
        ax = fig.add_axes((x0, y0, split - x0 - gap / 2, y1 - y0))
        ax.bar(range(len(caps)), [c["rms_a"] for c in caps],
               color=["#9467bd" if c["lisn"] else "#1f77b4" for c in caps])
        ax.set_xticks(range(len(caps)))
        ax.set_xticklabels(names, rotation=90, fontsize=FS_TICK)
        ax.set_xlim(-0.6, len(caps) - 0.4)
        ax.set_ylabel("A RMS", fontsize=FS_TEXT)
        ax.tick_params(axis="y", labelsize=FS_TICK)
        ax.grid(True, axis="y", alpha=0.3)
        ax.set_title(f"{rr.net}: bus capacitors' ripple current at the operating point (all legs)",
                     fontsize=FS_TITLE, loc="left")
    if victims:
        ax = fig.add_axes((split + gap / 2, y0, x1 - split - gap / 2, y1 - y0))
        ratio = [v["peak"] / v["limit"] if v["limit"] else 0.0 for v in victims]
        colours = {"net": "#d62728", "injected": "#ff7f0e", "part": "#7f7f7f"}
        ax.bar(range(len(victims)), ratio, color=[colours[v["kind"]] for v in victims])
        for k, (v, r) in enumerate(zip(victims, ratio)):
            text = f"{v['peak'] * 1e3:.2g} mA" if v["kind"] == "injected" else f"{v['peak']:.2g} V"
            ax.text(k, r, text, ha="center", va="bottom", fontsize=FS_TICK)
        ax.axhline(1.0, color="#444444", lw=1, ls="--")
        ax.set_xticks(range(len(victims)))
        ax.set_xticklabels(vnames, rotation=90, fontsize=FS_TICK)
        ax.set_xlim(-0.6, len(victims) - 0.4)
        ax.set_ylim(0, max(1.2, max(ratio) * 1.25))
        ax.set_ylabel("× threshold", fontsize=FS_TEXT)
        ax.tick_params(axis="y", labelsize=FS_TICK)
        ax.grid(True, axis="y", alpha=0.3)
        ax.set_title("victims at the switching edges: noise, injected current, ground shift", fontsize=FS_TITLE,
                     loc="left")


def _victim_label(v: dict) -> str:
    """A victim row's short label for a plot ("NRST noise", "GND ground")."""
    what = {"net": "noise", "injected": "injected", "part": "ground"}[v["kind"]]
    return f"{v['name'].split('/')[-1]} {what}"


def _bridge_spectrum(fig: Figure, rr) -> None:
    """The conducted readings at the input (all legs together, simulated receiver) with the limit; with CE101 set,
    the input lead's current below 10 kHz (the commutation lines) in a panel beside it."""
    fig.clear()
    ce101 = getattr(rr, "ce101", None) or []
    ax = fig.add_subplot(1, 2, 2) if ce101 else fig.add_subplot(111)
    if ce101:
        _bridge_ce101(fig.add_subplot(1, 2, 1), rr, ce101)
    rows = getattr(rr, "conducted", None) or []
    if not rows:
        ax.text(0.5, 0.5, "no conducted estimate (set the bridge's input connector)" if not rr.error else rr.error,
                ha="center", va="center", transform=ax.transAxes, fontsize=FS_TEXT)
        ax.set_axis_off()
        return
    f = np.array([r["f"] for r in rows]) / 1e6
    v = np.array([r["v_dbuv"] for r in rows])
    ax.plot(f, v, color="#9467bd", lw=1.0, label="estimate (receiver)")
    lim = np.array([np.nan if r["limit_dbuv"] is None else r["limit_dbuv"] for r in rows])
    if np.isfinite(lim).any():
        ax.plot(f, lim, color="#d62728", lw=1.2, label="limit")
    _freq_axis(ax, f)
    ax.set_ylabel("dBµV", fontsize=FS_TEXT)
    ax.tick_params(labelsize=FS_TICK, which="both")
    ax.grid(True, which="both", alpha=0.3)
    w_in, h_in = fig.get_size_inches()
    left_in, right_in = _lh(FS_TEXT) + _width_in("100", FS_TICK) + 0.24, 0.25
    width = (w_in - left_in - right_in) / (2 if ce101 else 1) - (left_in / 2 if ce101 else 0)
    header = _title_and_key(ax, f"{rr.net}: conducted at the input, {rr.conducted_standard}", width)
    if ce101:
        header = max(header, _title_and_key(fig.axes[1], f"input lead current, {rr.ce101_limit}", width))
    _adjust(fig, left_in, right_in, 0.10 + _lh(FS_TICK) + 0.08, header + 0.06,
            **({"wspace": (left_in + 0.25) / max(width, 0.2)} if ce101 else {}))


def _bridge_ce101(ax, rr, rows) -> None:
    """A bridge's CE101 readings (the current probe on the input lead, all legs) with the limit."""
    f = np.array([r["f"] for r in rows]) / 1e6
    v = np.array([r["i_dbua"] for r in rows])
    ax.plot(f, v, color=EMISSION_PANELS["ce101"][4], lw=1.0, label="estimate (receiver)")
    lim = np.array([np.nan if r["limit_dbua"] is None else r["limit_dbua"] for r in rows])
    if np.isfinite(lim).any():
        ax.plot(f, lim, color="#d62728", lw=1.2, label="limit")
    _freq_axis(ax, f)
    ax.set_ylabel("dBµA", fontsize=FS_TEXT)
    ax.tick_params(labelsize=FS_TICK, which="both")
    ax.grid(True, which="both", alpha=0.3)


PDN_COLOURS = ("#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#8c564b", "#e377c2", "#17becf", "#bcbd22")


def _signed_hz(v: float, _pos=None) -> str:
    """A signed frequency (Hz) on a symlog axis: '−1 MHz', '0', '20 kHz'."""
    if v == 0:
        return "0"
    return ("−" if v < 0 else "") + _hz(abs(v) / 1e6)


EQ_SCALE = 2.0                 # the Bode view's H(s) lines: this many times the text size


def transfer_lines(view) -> list[tuple[str, str, str, str]]:
    """The H(s) lines a Bode view shows, from its first response: [(label, latex, plain text, values)], the ideal
    circuit's in part names (analysis/symbolic.py) and the full model's normal form with its poles' and zeros'
    frequencies and Q (analysis/transfer.normal_form); values "" where there are none. draw_bode draws them,
    transfer_clipboard copies them."""
    resp = [r for r in getattr(view, "responses", ()) if len(r.f)]
    if not resp:
        return []
    out = []
    sym = next((r.symbolic for r in resp if getattr(r, "symbolic", None)), None)
    if sym and sym.get("latex"):
        note = sym.get("note")                  # e.g. "C3 not populated (DNP), kept in the circuit"
        out.append((f"ideal circuit ({note})" if note else "ideal circuit", sym["latex"], sym.get("text") or "", ""))
    nf = getattr(resp[0], "normal", None)
    if nf and nf.get("latex") and (nf.get("poles") or nf.get("zeros")):
        plain, _, vals = nf["text"].partition(";  ")
        out.append(("full model", nf["latex"], plain, vals))
    return out


BODE_PLOTS_IN = 3.0            # inches the Bode plot's two axes keep at least under the formulas


def bode_height_in(view, w_in: float, default: float) -> float:
    """The height a Bode figure `w_in` wide needs (at least `default`): the formulas over it (transfer_lines) may
    take two inches and more, and the plots keep BODE_PLOTS_IN. An estimate from the factors' count without
    drawing anything (the plugin calls it on the GUI thread): a first-order factor about 0.85 in wide at the
    formulas' size, a quadratic one 1.55 in [A64]."""
    lines = transfer_lines(view)
    if not lines:
        return default
    room = max(w_in - 1.2, 1.0)
    per_char = 0.55 * FS_TEXT / 72
    need = 0.6 + 0.45 + BODE_PLOTS_IN                     # title and key, the frequency axis, the plots
    for label, tex, _plain, vals in lines:
        parts = _long_form(view, label)
        if parts:
            widths = [1.55 if "^2" in f else 0.85 for f in parts.get("num", []) + parts.get("den", [])]
            if sum(widths) + 1.6 > room / 0.7:         # drawn over lines (draw_bode, _draw_long_form)
                rows = 1
                for group in (parts.get("num", []), parts.get("den", [])):
                    used = 0.0
                    for f in group:
                        w = (1.55 if "^2" in f else 0.85) * 0.8
                        if used and used + w > room - 0.8:
                            rows, used = rows + 1, 0.0
                        used += w
                    rows += 1 if group else 0
                need += rows * 0.6
            else:
                need += 0.95
        else:
            need += 0.85 if r"\frac" in tex else 0.45
        if vals:
            need += _lh(FS_TEXT) * -(-len(vals) // max(20, int(room / per_char))) + 0.08
    return max(default, need)


def _long_form(view, label: str) -> Optional[dict]:
    """The full model's H(s) in pieces (transfer.normal_form's latex_parts) when `label` is that line, else None."""
    if label != "full model":
        return None
    resp = [r for r in getattr(view, "responses", ()) if len(r.f)]
    nf = getattr(resp[0], "normal", None) if resp else None
    return (nf or {}).get("latex_parts")


def _draw_long_form(fig: Figure, renderer, parts: dict, label: str, left_in: float, y: float,
                    room: float) -> float:
    """A long H(s) as  H(s) = K·N(s)/D(s)  and under it N(s) = … and D(s) = …, each a product of its factors
    broken into lines that fit `room` inches, at the formulas' size (draw_bode). Returns the height drawn (in)."""
    w_in, h_in = fig.get_size_inches()
    size = FS_TEXT * EQ_SCALE * 0.8
    num, den = parts.get("num") or [], parts.get("den") or []
    head = r"\mathrm{" + label.replace(" ", r"\ ") + r":}\ H(s) = " + parts.get("k", "") \
        + (r" \dfrac{N(s)}{D(s)}" if num else r" \dfrac{1}{D(s)}")
    rows = [head]
    for name, factors in (("N(s)", num), ("D(s)", den)):
        if not factors:
            continue
        line = rf"\quad {name} = "
        for f in factors:
            f = f.replace(r"\frac", r"\dfrac").replace(r"\left(", "(").replace(r"\right)", ")")
            probe = fig.text(0, 0, f"${line}{f}$", fontsize=size)
            wide = probe.get_window_extent(renderer).width / fig.dpi > room
            probe.remove()
            if wide and not line.rstrip().endswith(("=", r"\qquad")):     # this factor on the next line
                rows.append(line)
                line = r"\qquad "
            line += f
        rows.append(line)
    drawn = 0.0
    for row in rows:
        t = fig.text(left_in / w_in, 1 - (y + drawn) / h_in, f"${row}$", ha="left", va="top", fontsize=size,
                     transform=fig.transFigure)
        drawn += t.get_window_extent(renderer).height / fig.dpi + 0.04
    return drawn


def transfer_clipboard(view) -> str:
    """The Bode view's H(s) as text to paste elsewhere (the user: "it would be good if one could copy them"): each
    line in plain text with its values, then the same in LaTeX (for a document, a wiki or a notebook)."""
    lines = transfer_lines(view)
    if not lines:
        return ""
    owner = getattr(view, "owner", "")
    plain = [f"{label}: {text}" + (f"  ({vals})" if vals else "") for label, _, text, vals in lines]
    latex = [f"% {label}" + (f": {vals}" if vals else "") + "\n" + tex for label, tex, _, vals in lines]
    return "\n".join(([owner] if owner else []) + plain + ["", "LaTeX:"] + latex) + "\n"


def draw_bode(fig: Figure, view) -> None:
    """A Bode plot of a view's responses (analysis/transfer.BodeView): magnitude on top (dB, or Ω on a log axis for
    impedances), the unwrapped phase below, on one log frequency axis; each response in its colour, its poles (×)
    and zeros (○) marked at their natural frequencies on its magnitude curve, the legend naming the curves and the
    two markers."""
    from ..analysis.transfer import response_label
    fig.clear()
    resp = [r for r in view.responses if len(r.f)]
    if not resp:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, "no response", ha="center", va="center", transform=ax.transAxes, fontsize=FS_TEXT)
        ax.set_axis_off()
        return
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 2], hspace=0.08)
    ax_m = fig.add_subplot(gs[0])
    ax_p = fig.add_subplot(gs[1], sharex=ax_m)
    ohms = all(r.quantity == "impedance" for r in resp)
    f_all, marked = [], set()
    marks = fig._gt_marks = {}          # for the plugin's hover (results_panels._hover, mark_at): what each marker is
    for k, r in enumerate(resp):
        c = PDN_COLOURS[k % len(PDN_COLOURS)]
        f = np.asarray(r.f) / 1e6
        f_all += list(f)
        mag = np.abs(r.h) if ohms else 20 * np.log10(np.maximum(np.abs(r.h), 1e-30))
        ax_m.plot(f, mag, color=c, lw=1.3, label=response_label(r, view.owner))
        ax_p.plot(f, r.phase_deg(), color=c, lw=1.1)
        for roots, marker in ((r.poles, "x"), (r.zeros, "o")):
            rows = _pz_in_band(roots, r.f)
            if rows:                    # one artist for all of them (one each made the PDN views slow to draw)
                f0 = np.array([row["f0"] for row in rows])
                y0 = np.interp(np.log(f0), np.log(r.f), mag)
                ax_m.plot(f0 / 1e6, y0, marker, ls="none", color=c, ms=6, mfc="none", mew=1.4)
                marked.add(marker)
                name = response_label(r, view.owner)
                marks.setdefault(ax_m, []).extend(
                    (x, y, 0, pz_tip(name, "pole" if marker == "x" else "zero", row))
                    for x, y, row in zip(f0 / 1e6, y0, rows))
    # the markers' key in the legend, after the curves' (the user: "some form of legend would help"): grey, as each
    # marker takes its curve's colour; only the kinds the plot shows
    for marker, what in (("x", "pole"), ("o", "zero")):
        if marker in marked:
            ax_m.plot([], [], marker, ls="none", color="0.35", ms=6, mfc="none", mew=1.4, label=what)
    if ohms:
        ax_m.set_yscale("log")
    ax_m.set_ylabel("|Z| (Ω)" if ohms else "|H| (dB)", fontsize=FS_TEXT)
    ax_p.set_ylabel("phase (°)", fontsize=FS_TEXT)
    _freq_axis(ax_p, f_all)
    ax_m.tick_params(labelbottom=False, labelsize=FS_TICK, which="both")
    ax_p.tick_params(labelsize=FS_TICK, which="both")
    for ax in (ax_m, ax_p):
        ax.grid(True, which="both", alpha=0.3)
    w_in, h_in = fig.get_size_inches()
    left_in, right_in = _lh(FS_TEXT) + _width_in("-1000", FS_TICK) + 0.24, 0.25
    header = _title_and_key(ax_m, f"{view.owner}: Bode", w_in - left_in - right_in)
    # the first response's transfer function in its part names (analysis/symbolic.py), in matplotlib's math text
    # under the title: the user, "i want the transfer function to be shown in the results as well"
    # Two lines, each labelled (the user: "the transfer function does not mention the two complex poles, why!?"):
    # the ideal circuit's H(s) in part names (ideal op-amps, no source, load or tracks: a lone series resistor is
    # H = 1), and the full model's H(s) in normal form with its poles' and zeros' frequencies and Q (what the curves,
    # the step and the pole plot show)
    # Drawn at twice the text size (the user: "the equations are hard to read; they need to be bigger"): a fraction
    # of fractions with subscripts shrinks its inner levels, so the formula itself is set large; each line's height
    # is measured from the drawn text, not guessed
    extra, y = 0.0, 0.08
    renderer = fig.canvas.get_renderer() if hasattr(fig.canvas, "get_renderer") else None
    room = w_in - left_in - right_in                       # inches the formulas may take
    for label, tex, _plain, vals in transfer_lines(view):
        # every fraction at full size (\dfrac): mathtext sets \frac smaller, and a fraction inside a fraction
        # (s/ωp, s²/ωp²) smaller again, too small to read
        tex = (r"\mathrm{" + label.replace(" ", r"\ ") + r":}\ " + tex.replace(r"\frac", r"\dfrac")) \
            .replace(r"\left(", "(").replace(r"\right)", ")")                 # sizes mathtext does not need
        t = None
        try:
            t = fig.text(left_in / w_in, 1 - y / h_in, f"${tex}$", ha="left", va="top", fontsize=FS_TEXT * EQ_SCALE,
                         transform=fig.transFigure)
            box = t.get_window_extent(renderer) if renderer is not None else None
            if box is not None and box.width / fig.dpi > room:      # too wide (many poles): smaller, to fit
                size = FS_TEXT * EQ_SCALE * room / (box.width / fig.dpi)
                parts = _long_form(view, label) if size < FS_TEXT * 1.4 else None
                if parts:                   # still too small to read: H(s) = K·N(s)/D(s), N and D over lines
                    t.remove()
                    t = None
                    height = _draw_long_form(fig, renderer, parts, label, left_in, y, room)
                else:
                    t.set_fontsize(max(FS_TEXT, size))
                    box = t.get_window_extent(renderer)
            if t is not None:
                height = box.height / fig.dpi if box is not None else 0.75
        except ValueError:                  # math text it can't parse: the summary line shows the plain text
            if t is not None:
                t.remove()
            continue
        step = height + 0.08
        if vals:                                           # the factors' values under the formula, wrapped to fit
            import textwrap
            per_line = max(20, int(room / (0.55 * FS_TEXT / 72)))          # characters of about 0.55 em
            wrapped = textwrap.fill(vals, per_line)
            fig.text(left_in / w_in, 1 - (y + step) / h_in, wrapped, ha="left", va="top", fontsize=FS_TEXT,
                     transform=fig.transFigure, color="0.4")
            step += _lh(FS_TEXT) * (wrapped.count("\n") + 1) + 0.08
        y += step
        extra += step
    _adjust(fig, left_in, right_in, 0.10 + _lh(FS_TICK) + 0.08, header + 0.06 + extra, hspace=0.08)


STEP_IN = 3.4                  # inches: the step response's figure height (the Results tab, the report)


def has_step(view) -> bool:
    """Whether a Bode view has a step response to draw (draw_step)."""
    return any(getattr(r, "step", None) for r in getattr(view, "responses", ()))


def draw_step(fig: Figure, view) -> None:
    """The step responses of a view's responses (transfer.Response.step: each its circuit's response to a 1 V step at
    the driver, emi/mna.Circuit.step): each in its colour against time, its final value dashed with the ±2 % band,
    the 10-90 % rise and the settling time marked; each response on its own time scale (a filter's milliseconds beside
    an op-amp's nanoseconds) when they differ by more than ten times, the slowest's otherwise."""
    from ..analysis.transfer import SETTLE_BAND, response_label
    fig.clear()
    resp = [r for r in view.responses if getattr(r, "step", None)]
    if not resp:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, "no step response", ha="center", va="center", transform=ax.transAxes, fontsize=FS_TEXT)
        ax.set_axis_off()
        return
    spans = [float(r.step["t"][-1]) for r in resp]
    apart = max(spans) / max(min(spans), 1e-300) > 10
    groups = [[r] for r in resp] if apart else [resp]
    axes = [fig.add_subplot(1, len(groups), k + 1) for k in range(len(groups))]
    for ax, group in zip(axes, groups):
        t_end = max(float(r.step["t"][-1]) for r in group)
        scale, unit = next(((k, u) for k, u in ((1.0, "s"), (1e-3, "ms"), (1e-6, "µs"), (1e-9, "ns"))
                            if t_end >= 2 * k), (1e-12, "ps"))
        for r in group:
            c = PDN_COLOURS[resp.index(r) % len(PDN_COLOURS)]
            t, y, m = np.asarray(r.step["t"]) / scale, np.asarray(r.step["y"]), r.step["metrics"]
            ax.plot(t, y, color=c, lw=1.3, label=response_label(r, view.owner))
            fin = m.get("final")
            if fin and m.get("rise") is not None:
                ax.axhline(fin, color=c, lw=0.7, ls="--")
                ax.axhspan(fin * (1 - SETTLE_BAND), fin * (1 + SETTLE_BAND), color=c, alpha=0.08, lw=0)
                if m.get("settling") is not None:
                    ax.axvline(m["settling"] / scale, color=c, lw=0.7, ls=":")
        ax.set_xlabel(f"time ({unit})", fontsize=FS_TEXT)
        ax.tick_params(labelsize=FS_TICK)
        ax.grid(True, alpha=0.3)
        if len(groups) > 1:
            ax.set_title(response_label(group[0], view.owner), fontsize=FS_TEXT)
    axes[0].set_ylabel("V per V step", fontsize=FS_TEXT)
    w_in, h_in = fig.get_size_inches()
    left_in, right_in = _lh(FS_TEXT) + _width_in("-1000", FS_TICK) + 0.24, 0.25
    title = f"{view.owner}: step response (1 V at the driver; dashed: final value, ±2 %; dotted: settled)"
    if len(groups) == 1:                # one time scale: the title and the legend over it
        header = _title_and_key(axes[0], title, w_in - left_in - right_in)
    else:                               # a panel per response, each named over it: the title on its own line above
        fig.text(left_in / w_in, 1 - 0.06 / h_in, title, ha="left", va="top", fontsize=FS_TITLE)
        header = _lh(FS_TITLE) + _lh(FS_TEXT) + 0.10
    _adjust(fig, left_in, right_in, 0.10 + 2 * _lh(FS_TICK) + 0.08, header + 0.06, wspace=0.25)


def pz_tip(curve: str, kind: str, row: dict) -> str:
    """A pole's or zero's hover text (the user: "when hovering over the poles and zeros with the mouse can you show
    a hover-over note like a tooltip?"): whose it is, its natural frequency and, for a complex pair, its Q and
    damping ratio ζ = 1 / 2Q [Ogata 2010]; one in the right half-plane said so (a pole there: unstable; a zero: the
    response starts the wrong way). `row` as transfer.pz_rows gives it."""
    from ..analysis.transfer import fmt_hz
    f0 = fmt_hz(row["f0"])
    if row["re"] > 0:
        return f"{curve}: {kind} at {f0}, in the right half-plane"
    if row["q"]:
        return f"{curve}: {kind} pair at {f0}, Q {row['q']:.3g} (ζ {1 / (2 * row['q']):.3g})"
    return f"{curve}: real {kind} at {f0}"


def _pz_in_band(roots, f) -> list[dict]:
    """The pole / zero rows (transfer.pz_rows) whose f0 lies within the curve's frequencies."""
    from ..analysis.transfer import pz_rows
    f = np.asarray(f, float)
    return [row for row in pz_rows(roots) if f.min() <= row["f0"] <= f.max()]


def draw_pz(fig: Figure, view) -> None:
    """The pole-zero map of a view's responses: the s-plane in Hz (σ/2π across, ω/2π up) on symmetric log axes
    (poles and zeros span decades), × poles and ○ zeros in each response's colour, the dominant ones labelled with
    their natural frequency and Q."""
    from ..analysis.transfer import response_label
    fig.clear()
    ax = fig.add_subplot(111)
    pts = []
    from ..analysis.transfer import pz_rows
    tips = []                           # the plugin's hover (results_panels._hover): each marker's text, pz_tip
    for k, r in enumerate(view.responses):
        c = PDN_COLOURS[k % len(PDN_COLOURS)]
        for roots, marker in ((r.poles, "x"), (r.zeros, "o")):
            xs = [complex(s).real / (2 * math.pi) for s in roots]
            ys = [complex(s).imag / (2 * math.pi) for s in roots]
            if xs:
                ax.plot(xs, ys, marker, color=c, ms=7, mfc="none", mew=1.5,
                        label=response_label(r, view.owner) if marker == "x" else None)
                pts += [abs(v) for v in xs + ys if v]
                kind = "pole" if marker == "x" else "zero"
                for s, x, y in zip(roots, xs, ys):       # both members of a pair: pz_rows reads the upper one
                    row = pz_rows([complex(complex(s).real, abs(complex(s).imag))])[0]
                    tips.append((x, y, 0, pz_tip(response_label(r, view.owner), kind, row)))
    fig._gt_marks = {ax: tips}
    if not pts:
        ax.text(0.5, 0.5, "no poles or zeros", ha="center", va="center", transform=ax.transAxes, fontsize=FS_TEXT)
        ax.set_axis_off()
        return
    lin = max(min(pts) / 3, 1e-3)
    ax.set_xscale("symlog", linthresh=lin)
    ax.set_yscale("symlog", linthresh=lin)
    ax.axhline(0, color="#888", lw=0.8)
    ax.axvline(0, color="#888", lw=0.8)
    from ..analysis.transfer import fmt_root
    for k, r in enumerate(view.responses):
        for row in pz_rows(r.poles)[:4]:             # the lowest natural frequencies: the dominant poles
            text = fmt_root(row)
            ax.annotate(text, (row["re"] / (2 * math.pi), row["im"] / (2 * math.pi)), fontsize=FS_TICK - 1,
                        xytext=(4, 4), textcoords="offset points", color=PDN_COLOURS[k % len(PDN_COLOURS)])
    # no labels inside the linear band around zero (its ±linthresh ticks would print on top of each other)
    label = FuncFormatter(lambda v, pos=None: "0" if v == 0 else "" if abs(v) < 10 * lin * 0.999 else _signed_hz(v))
    ax.xaxis.set_major_formatter(label)
    ax.yaxis.set_major_formatter(label)
    ax.set_xlabel("σ / 2π", fontsize=FS_TEXT)
    ax.set_ylabel("ω / 2π", fontsize=FS_TEXT)
    ax.tick_params(labelsize=FS_TICK - 1)
    ax.grid(True, which="major", alpha=0.3)
    w_in, _ = fig.get_size_inches()
    left_in, right_in = _lh(FS_TEXT) + _width_in("−100 MHz", FS_TICK) + 0.24, 0.25
    header = _title_and_key(ax, f"{view.owner}: poles (×) and zeros (○)", w_in - left_in - right_in)
    _adjust(fig, left_in, right_in, 0.10 + _lh(FS_TICK) + _lh(FS_TEXT) + 0.1, header + 0.06)


def draw_impedance(fig: Figure, si) -> None:
    """Signal integrity (analysis/si.SiResult): one row per net (Z0) and pair (Zdiff), a bar from the lowest to the
    highest cross-section, a dot at the length-weighted mean, the target ± tolerance shaded."""
    fig.clear()
    ax = fig.add_subplot(111)
    rows = []
    for n in si.nets:
        s = n.summary()
        if "z_mean" in s:
            rows.append((f"Z0 {n.net.rsplit('/', 1)[-1]}", s["z_min"], s["z_max"], s["z_mean"], n.target, n.tol))
    for p in si.pairs:
        s = p.summary()
        if "zdiff_mean" in s:
            rows.append((f"Zdiff {s['pair']}", s["zdiff_min"], s["zdiff_max"], s["zdiff_mean"], p.spec.zdiff,
                         p.spec.tol))
    if not rows:
        ax.text(0.5, 0.5, "no cross-section solved", ha="center", va="center", transform=ax.transAxes,
                fontsize=FS_TEXT)
        ax.set_axis_off()
        return
    for k, (name, lo, hi, mean, target, tol) in enumerate(rows):
        y = len(rows) - 1 - k
        if target:
            ax.fill_betweenx([y - 0.4, y + 0.4], target * (1 - tol), target * (1 + tol), color="#2ca02c", alpha=0.18,
                             lw=0)
            ax.plot([target, target], [y - 0.4, y + 0.4], color="#2ca02c", lw=1)
        off = target and (lo < target * (1 - tol) or hi > target * (1 + tol))
        c = "#d62728" if off else "#1f77b4"
        ax.plot([lo, hi], [y, y], color=c, lw=4, solid_capstyle="butt", alpha=0.6)
        ax.plot([mean], [y], "o", color=c, ms=5)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows][::-1], fontsize=FS_TICK)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_xlabel("impedance (Ω)", fontsize=FS_TEXT)
    ax.tick_params(labelsize=FS_TICK)
    ax.grid(True, axis="x", alpha=0.3)
    w_in, h_in = fig.get_size_inches()
    left_in = max(_width_in(r[0], FS_TICK) for r in rows) + 0.2
    fig.subplots_adjust(left=min(left_in / w_in, 0.5), right=1 - 0.25 / w_in, top=1 - 0.15 / h_in,
                        bottom=min((_lh(FS_TEXT) + _lh(FS_TICK) + 0.15) / h_in, 0.6))


def draw_pdn(fig: Figure, p) -> None:
    """A rail's PDN (analysis/pdn.PdnResult): |Z| at each IC against frequency (log-log), the target to check_to,
    each IC's highest peak marked; the impedance's phase below on the same frequency axis (+90°: inductive, −90°:
    capacitive, crossing 0° at a resonance)."""
    fig.clear()
    if p.error or not p.ports:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, p.error or "no IC on the rail", ha="center", va="center", transform=ax.transAxes,
                fontsize=FS_TEXT)
        ax.set_axis_off()
        return
    phases = all(port.get("phase_deg") for port in p.ports)
    if phases:
        gs = fig.add_gridspec(2, 1, height_ratios=[3, 2], hspace=0.08)
        ax = fig.add_subplot(gs[0])
        ax_p = fig.add_subplot(gs[1], sharex=ax)
    else:                                       # a result kept before the phase was stored
        ax, ax_p = fig.add_subplot(111), None
    f_all = []
    for k, port in enumerate(p.ports):
        f = np.array(port["f"]) / 1e6
        f_all += list(f)
        c = PDN_COLOURS[k % len(PDN_COLOURS)]
        ax.plot(f, port["z_ohm"], color=c, lw=1.2, label=port["part"])
        if ax_p is not None:
            ax_p.plot(f, port["phase_deg"], color=c, lw=1.1)
        if port["peaks"]:
            pk = port["peaks"][0]
            ax.plot([pk["f"] / 1e6], [pk["z_ohm"]], "v", color=c, ms=5)
    if p.target_ohm:
        ax.plot([min(f_all), p.check_to / 1e6], [p.target_ohm] * 2, "--", color="#d62728", lw=1.2, label="target")
        for a_ in (ax, ax_p):
            if a_ is not None:
                a_.axvline(p.check_to / 1e6, color="#d62728", lw=0.8, ls=":")
    ax.set_yscale("log")
    ax.set_ylabel("|Z| (Ω)", fontsize=FS_TEXT)
    if ax_p is not None:
        _freq_axis(ax_p, f_all)
        ax_p.set_ylim(-100, 100)
        ax_p.set_yticks([-90, -45, 0, 45, 90])
        ax_p.set_ylabel("phase (°)", fontsize=FS_TEXT)
        ax.tick_params(labelbottom=False)
    else:
        _freq_axis(ax, f_all)
    for a_ in (ax, ax_p):
        if a_ is not None:
            a_.tick_params(labelsize=FS_TICK, which="both")
            a_.grid(True, which="both", alpha=0.3)
    w_in, _ = fig.get_size_inches()
    left_in, right_in = _lh(FS_TEXT) + _width_in("1000", FS_TICK) + 0.24, 0.25
    header = _title_and_key(ax, f"{p.name}: impedance at the ICs' supply pins ({p.rail} to {p.ground})",
                            w_in - left_in - right_in)
    _adjust(fig, left_in, right_in, 0.10 + _lh(FS_TICK) + 0.08, header + 0.06,
            **({"hspace": 0.08} if ax_p is not None else {}))


def draw_pdn_holds(fig: Figure, p) -> None:
    """What holds a rail at each IC against frequency: a band per IC, coloured by the part carrying the largest
    share of its current there (the source, a decap, or the planes), each stretch named when it is wide enough."""
    fig.clear()
    ax = fig.add_subplot(111)
    if p.error or not p.ports:
        ax.set_axis_off()
        return
    colours, stretches = {}, []
    for row, port in enumerate(p.ports):
        hs = port["holds"]
        start = 0
        for k in range(1, len(hs) + 1):
            if k == len(hs) or hs[k]["part"] != hs[start]["part"]:
                lo = hs[start]["f"] if start == 0 else math.sqrt(hs[start - 1]["f"] * hs[start]["f"])
                hi = hs[k - 1]["f"] if k == len(hs) else math.sqrt(hs[k - 1]["f"] * hs[k]["f"])
                stretches.append((row, hs[start]["part"], lo / 1e6, hi / 1e6))
                start = k
    palette = list(PDN_COLOURS) + ["#7f7f7f", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5"]
    for row, part, lo, hi in stretches:
        c = colours.setdefault(part, "#cccccc" if part == "planes" else palette[len(colours) % len(palette)])
        ax.barh(row, hi - lo, left=lo, height=0.7, color=c, edgecolor="white", linewidth=0.5)
        if hi / lo > 2.2:
            ax.text(math.sqrt(lo * hi), row, part, ha="center", va="center", fontsize=FS_TICK)
    ax.set_yticks(range(len(p.ports)))
    ax.set_yticklabels([port["part"] for port in p.ports], fontsize=FS_TICK)
    ax.invert_yaxis()
    _freq_axis(ax, [s[2] for s in stretches] + [s[3] for s in stretches])
    ax.tick_params(labelsize=FS_TICK, which="both")
    w_in, _ = fig.get_size_inches()
    left_in, right_in = _width_in("U1102W", FS_TICK) + 0.3, 0.25
    header = _title_and_key(ax, "what holds the rail at each IC (the part carrying most of its current)",
                            w_in - left_in - right_in)
    _adjust(fig, left_in, right_in, 0.10 + _lh(FS_TICK) + 0.08, header + 0.06)


def figure_png(draw, *args, size=(8, 5), dpi=110, **kw) -> bytes:
    """A plot as PNG bytes: draw(fig, *args, **kw) on a new figure of `size` inches, rendered off-screen (Agg),
    cropped to what is drawn (a board's maps keep their aspect inside the figure: a tall board in a wide figure left
    a band of empty paper above and below, up to half the image in the report)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=size, dpi=dpi)
    FigureCanvasAgg(fig)
    draw(fig, *args, **kw)
    _close_title_gap(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.08)
    return buf.getvalue()


def _close_title_gap(fig, margin_in: float = 0.12, min_gap_in: float = 0.5) -> None:
    """Move a figure's title (suptitle, and the maps' note under it) down to just above the rest of the figure when
    that ended more than min_gap_in lower: maps keep their aspect ratio, so three tall boards side by side sit in
    the middle of the figure with the title at its top, an empty band between them (cropping the image cannot remove
    a band inside it). Measured on the rendered figure: the axes with their titles and tick labels, the figure's
    legends and its other texts; an ordinary figure's gap is left as it is."""
    texts = [t for t in (getattr(fig, "_suptitle", None), getattr(fig, "_gt_note", None)) if t is not None]
    if not texts or not fig.axes:
        return
    try:
        r = fig.canvas.get_renderer()
        inv = fig.transFigure.inverted()
        boxes = [ax.get_tightbbox(r) for ax in fig.axes if ax.get_visible()]
        boxes += [lg.get_window_extent(r) for lg in fig.legends]
        boxes += [t.get_window_extent(r) for t in fig.texts if t not in texts and t.get_text()]
        top = max(inv.transform(b)[1][1] for b in boxes if b is not None)
        low = min(inv.transform(t.get_window_extent(r))[0][1] for t in texts if t.get_text())
    except (AttributeError, ValueError):
        return
    h = fig.get_figheight()
    if (low - top) * h > min_gap_in:
        gap = low - top - margin_in / h
        for t in texts:
            t.set_y(t.get_position()[1] - gap)
