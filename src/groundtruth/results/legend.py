"""The text Draw arrows on board writes beside the board on each drawing layer: what that layer's arrows are (the
current on the supply side, or the return current) and a few figures to read them by.

KiCad's API cannot name a User layer (kicad-python 0.8 reads a layer's name, nothing sets one), so the layer says
it itself (the user: "draw a text in the layer beside the PCB in 2x2 mm 0.4 thickness. Choose whatever description
is ideal. Also you may add some more comments below that that provides the user with more useful insight").
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

TITLE_MM, TITLE_STROKE_MM = 2.0, 0.4    # the heading: 2 x 2 mm, 0.4 mm strokes (the user)
LINE_MM, LINE_STROKE_MM = 1.0, 0.15     # the lines below it
GAP_MM = 4.0                            # from the board's right edge, and between the two layers' blocks


@dataclass
class Legend:
    """One layer's text: the heading, the lines below it, and where its top left corner goes (board mm)."""
    title: str
    lines: list[str] = field(default_factory=list)
    at: tuple[float, float] = (0.0, 0.0)

    def height(self) -> float:
        """How tall the block is, mm (heading and lines at 1.6 times their size apart)."""
        return 1.6 * TITLE_MM + 1.6 * LINE_MM * len(self.lines)

    def items(self) -> list[tuple[tuple[float, float], str, float, float]]:
        """[(left middle of the text, text, size mm, stroke mm)]: the heading, then each line under it."""
        x, y = self.at
        y += TITLE_MM / 2
        out = [((x, y), self.title, TITLE_MM, TITLE_STROKE_MM)]
        y += 0.8 * TITLE_MM + 0.8 * LINE_MM + 0.4
        for line in self.lines:
            out.append(((x, y), line, LINE_MM, LINE_STROKE_MM))
            y += 1.6 * LINE_MM
        return out


def board_box(board, grid) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) mm: the board outline's extent (Edge.Cuts), else the map's."""
    pts = [p for poly in (getattr(board, "outline", None) or []) for p in poly[0]] if board is not None else []
    if pts:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)
    x0, x1, y0, y1 = grid.extent
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _nets(arrows, grid) -> list[str]:
    """The nets under the arrows, the most arrows first (results.vectors.split_return reads them the same way)."""
    count = Counter()
    for a in arrows:
        i, j = int(round((a[0] - grid.x0) / grid.d)), int(round((a[1] - grid.y0) / grid.d))
        if 0 <= i < grid.node.shape[0] and 0 <= j < grid.node.shape[1] and grid.node[i, j]:
            net = grid.net_at((i, j))
            if net:
                count[net] += 1
    return [n for n, _ in count.most_common()]


def _value(v: float, unit: str) -> str:
    """A density with its unit: three significant digits, whole numbers from 1000 up (no 1.81e+03 on a board)."""
    return (f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.3g}") + f" {unit}"


def arrow_legends(rr, plane_layer: str, ahead: list, back: list, unit: str, widths: tuple[float, float],
                  board=None, version: str = "", when: str = "") -> tuple[Legend, Legend]:
    """The text for the supply side's layer and for the return current's (Legend each, placed beside the board:
    the first at the board's top right, the second under it), from view `rr`, the copper layer drawn, the arrows of
    each side (results.vectors.split_return), the maps' unit and the arrow line widths (Preferences)."""
    grid = rr.layer_grids[plane_layer]
    K = np.where(grid.node, np.asarray(rr.sheet_current[plane_layer], float), 0.0)
    route = getattr(rr, "kind", "") == "route"
    what = (f"{rr.net}" if getattr(rr, "view_of", None) or rr.kind in ("converter", "dcload") else
            f"{rr.net} {rr.driver}->{rr.receiver}")
    f = getattr(rr, "map_frequency", 0.0) or 0.0
    at_f = "DC" if f <= 0 else (f"{f / 1e6:.4g} MHz" if f >= 1e6 else f"{f / 1e3:.4g} kHz")
    share = (getattr(rr, "layer_share", None) or {}).get(plane_layer)
    head = [f"View: {what} ({at_f})",
            f"Copper layer: {plane_layer}" + (f", {share * 100:.0f} % of the current" if share is not None else "")]

    def side(arrows, title: str, about: str) -> Legend:
        """One side's block: its nets, its peak density and where, and how to read the arrows."""
        nets = _nets(arrows, grid)
        lines = list(head)
        if nets:
            lines.append("Nets: " + ", ".join(nets[:4]) + (f" and {len(nets) - 4} more" if len(nets) > 4 else ""))
        on = np.zeros_like(K, bool)
        names = set(nets)
        if names and grid.label is not None:
            ids = [k for k, n in enumerate(grid.nets) if n in names]
            on = np.isin(grid.label, ids) & grid.node
        if on.any():
            k = int(np.argmax(np.where(on, K, -1.0)))
            i, j = np.unravel_index(k, K.shape)
            x, y = grid.x0 + i * grid.d, grid.y0 + j * grid.d
            lines.append(f"Peak here: {_value(float(K[i, j]), unit)} at ({x:.1f}, {y:.1f}) mm")
        lines.append(about)
        lines.append(f"{len(arrows)} arrows; more arrows and thicker lines ({widths[0]:g}-{widths[1]:g} mm) = "
                     "more current")
        busiest = max((t for t in getattr(rr, "transfers", None) or [] if t.get("kind") == "via"),
                      key=lambda t: t.get("current_a", t.get("fraction", 0.0)), default=None)
        if busiest is not None and busiest.get("current_a") is not None:
            lines.append(f"Busiest via of the view: {busiest['name']}, {busiest['current_a']:.2f} A")
        lines.append("GroundTruth" + (f" {version}" if version else "") + (f", {when}" if when else "") +
                     "; Clear GT markers removes this")
        return Legend(title, lines)

    if route:
        first = side(ahead, "Signal current", "On the signal's own copper; its return is on the return layer")
        second = side(back, "Return current", "Where the signal's return flows: under the route is best, "
                                              "detours and gaps add loop area")
    elif f > 0:                                 # a power stage's hot loop: its switching current
        first = side(ahead, "Current, supply side", "The hot loop's switching current: input capacitors, switches, "
                                                    "switch node")
        second = side(back, "Return current", "Its way back through ground: the closer under the supply side, the "
                                              "smaller the loop and the less it radiates")
    else:
        first = side(ahead, "Current, supply side", "From the source to the load: rail, switch node, output")
        second = side(back, "Return current", "Back through ground: it should stay close under the supply "
                                              "side's current")
    return first, second


def place(board, grid, first: Legend, second: Legend) -> None:
    """Both blocks beside the board: the first at its top right corner, GAP_MM out; the second under it."""
    x0, y0, x1, y1 = board_box(board, grid)
    first.at = (x1 + GAP_MM, y0)
    second.at = (x1 + GAP_MM, y0 + first.height() + GAP_MM)
    if math.isnan(first.at[0]):                 # no extent at all: at the origin
        first.at, second.at = (0.0, 0.0), (0.0, first.height() + GAP_MM)
