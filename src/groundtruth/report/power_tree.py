"""The power tree drawn as a flow chart (analysis/power_tree.py's graph): sources on the left, then rails, links,
switching and linear regulators column by column as power flows, each rail's loads and capacitor groups beside it;
rounded boxes coloured by kind (a source with a double border, a regulator's dashed), curved arrows, a legend on
top. matplotlib only (Agg: drawn off the GUI thread), as PNG or SVG.

The layout is a layered drawing [Sugiyama 1981]: each box's column is the longest path to it from a box nothing
feeds (sources, and rails without one), its loads and capacitors one column right of their rail; within a column
the boxes are put at the mean height of what feeds them (the barycentre), then pushed apart where they would
overlap, column by column from the left.
"""
from __future__ import annotations

import io
from typing import Optional

CHAR_IN = 0.072              # inches per character of the boxes' text (8 pt)
LINE_IN = 0.17               # inches per text line
PAD_IN = 0.14                # inside a box, around its text
GAP_X_IN = 0.75              # between columns
GAP_Y_IN = 0.16              # between boxes in a column
MARGIN_IN = 0.3
LEGEND_IN = 0.55             # the legend row's height
INDENT_IN = 0.35             # a rail's loads and capacitors under it, indented by this
FONT = 8.0
LEAVES = ("load", "caps")
NAMES = {"source": "Source", "link": "Series link", "smps": "SMPS", "ldo": "LDO", "rail": "Rail", "load": "Load",
         "caps": "Capacitors"}
TAGS = {"source": "SOURCE", "smps": "SMPS", "ldo": "LDO"}        # the small capital line over a box's title

# per kind: fill, edge (light theme), and the dark theme's
LIGHT = {"source": ("#c3e6c9", "#3d8b56"), "link": ("#fbe7b0", "#b8941e"), "smps": ("#c5dbf4", "#3f6fa8"),
         "ldo": ("#fbd6a6", "#c27c2c"), "rail": ("#dcdfe5", "#6b7280"), "load": ("#f5c5c2", "#b9473f"),
         "caps": ("#e4d4f3", "#7e57a8")}
DARK = {"source": ("#1d3b28", "#62c084"), "link": ("#463c1c", "#d8b440"), "smps": ("#1c3350", "#73a6e3"),
        "ldo": ("#4a3418", "#e3a25c"), "rail": ("#2c3038", "#9aa3b2"), "load": ("#4a2422", "#e57d75"),
        "caps": ("#382848", "#b68cdb")}


def _size(node) -> tuple[float, float]:
    """A box's width and height (inches) from its text."""
    lines = [node.title] + [x for x in node.lines if x] + ([TAGS[node.kind]] if node.kind in TAGS else [])
    w = max(len(x) for x in lines) * CHAR_IN + 2 * PAD_IN
    return max(w, 0.9), len(lines) * LINE_IN + 2 * PAD_IN * 0.8


def layout(tree) -> dict:
    """Each node's box {id: (x, y, w, h)} in inches (x the left edge, y the top, growing down), see the module."""
    ids = [n.id for n in tree.nodes]
    kind = {n.id: n.kind for n in tree.nodes}
    main = [(a, b) for a, b, _ in tree.edges if kind.get(a) not in LEAVES and kind.get(b) not in LEAVES]
    preds: dict = {i: [] for i in ids}
    for a, b, _ in tree.edges:
        if b in preds:
            preds[b].append(a)
    # columns: the longest path from a root over the main edges (a cycle stops at the node count)
    col = {i: 0 for i in ids if kind[i] not in LEAVES}
    for _ in range(len(col)):
        changed = False
        for a, b in main:
            if col[b] < col[a] + 1 and col[a] + 1 < len(col) + 1:
                col[b] = col[a] + 1
                changed = True
        if not changed:
            break
    # a rail's loads and capacitors stack under it in its own column, indented (to the right of it they shared the
    # column with the stages it feeds and were pushed far below, their lines crossing: refboard's first drawing)
    under: dict = {}
    for i in ids:
        if kind[i] in LEAVES and preds[i]:
            under.setdefault(preds[i][0], []).append(i)
            col[i] = col.get(preds[i][0], 0)
        elif kind[i] in LEAVES:
            col[i] = 0
    size = {n.id: _size(n) for n in tree.nodes}
    ncol = max(col.values(), default=0) + 1
    widths = [max([size[i][0] + (INDENT_IN if kind[i] in LEAVES else 0.0) for i in ids if col[i] == c] or [1.0])
              for c in range(ncol)]
    xs = [MARGIN_IN + sum(widths[:c]) + c * GAP_X_IN for c in range(ncol)]
    pos: dict = {}
    order = {i: k for k, i in enumerate(ids)}
    for c in range(ncol):
        heads = [i for i in ids if col[i] == c and kind[i] not in LEAVES]

        def want(i):
            """A box's wanted centre height: the mean of what feeds it, else its place in the list."""
            ys = [pos[p][1] + pos[p][3] / 2 for p in preds[i] if p in pos]
            return sum(ys) / len(ys) if ys else order[i] * 0.01
        heads.sort(key=lambda i: (want(i), order[i]))
        y = MARGIN_IN + LEGEND_IN
        for i in heads:
            w, h = size[i]
            top = max(want(i) - h / 2, y) if any(p in pos for p in preds[i]) else y
            pos[i] = (xs[c] if kind[i] == "rail" else xs[c] + (widths[c] - w) / 2, top, w, h)
            y = top + h + GAP_Y_IN
            # its loads first, then its capacitors, under it
            for leaf in sorted(under.get(i, []), key=lambda j: (kind[j] == "caps", order[j])):
                lw, lh = size[leaf]
                pos[leaf] = (xs[c] + INDENT_IN, y, lw, lh)
                y += lh + GAP_Y_IN * 0.6
            if under.get(i):
                y += GAP_Y_IN
    for i in ids:                       # a leaf without a rail (none expected): at the top left
        pos.setdefault(i, (MARGIN_IN, MARGIN_IN + LEGEND_IN, *size[i]))
    return pos


def figure(tree, dark: bool = False, title: str = ""):
    """The power tree as a matplotlib Figure (see the module)."""
    from matplotlib.figure import Figure
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
    from matplotlib.path import Path
    colours = DARK if dark else LIGHT
    ink, muted, bg = ("#e6e8eb", "#9aa3b2", "#1e1f22") if dark else ("#1f2328", "#5f6b7a", "#f8f9fb")
    pos = layout(tree)
    if not pos:
        fig = Figure(figsize=(5, 2), dpi=110)
        fig.patch.set_facecolor(bg)
        fig.text(0.5, 0.5, "no supply rails found on this board", ha="center", va="center", color=muted)
        return fig
    width = max(x + w for x, _, w, _ in pos.values()) + MARGIN_IN
    height = max(y + h for _, y, _, h in pos.values()) + MARGIN_IN
    width = max(width, 7.2)
    fig = Figure(figsize=(width, height), dpi=110)
    fig.patch.set_facecolor(bg)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)                     # y grows down, as the layout's
    ax.axis("off")
    kind = {n.id: n.kind for n in tree.nodes}
    # the arrows first (under the boxes): a cubic curve from the right side of one to the left side of the other
    for a, b, ek in tree.edges:
        if a not in pos or b not in pos:
            continue
        xa, ya, wa, ha = pos[a]
        xb, yb, wb, hb = pos[b]
        if kind[b] in LEAVES:
            # under its rail: an elbow from the rail's lower left down, then right into the box
            xe = xa + INDENT_IN / 2
            path = Path([(xe, ya + ha), (xe, yb + hb / 2), (xb, yb + hb / 2)], [Path.MOVETO, Path.LINETO,
                                                                                 Path.LINETO])
        else:
            p0, p3 = (xa + wa, ya + ha / 2), (xb, yb + hb / 2)
            dx = max((p3[0] - p0[0]) * 0.5, 0.2)
            path = Path([p0, (p0[0] + dx, p0[1]), (p3[0] - dx, p3[1]), p3],
                        [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4])
        c = colours[kind[b]][1] if kind[b] in ("load", "caps") else colours["smps"][1]
        ax.add_patch(FancyArrowPatch(path=path, arrowstyle="-" if ek == "caps" else "-|>", mutation_scale=10,
                                     color=c, lw=1.2, ls=(0, (4, 3)) if ek == "caps" else "-"))
    for n in tree.nodes:
        x, y, w, h = pos[n.id]
        fill, edge = colours[n.kind]
        dashed = n.kind in ("smps", "ldo")
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.08", fc=fill, ec=edge,
                                    lw=1.5, ls=(0, (4, 2)) if dashed else "-"))
        if n.kind == "source":                 # a double border
            ax.add_patch(FancyBboxPatch((x + 0.04, y + 0.04), w - 0.08, h - 0.08, fc="none", ec=edge, lw=1.0,
                                        boxstyle="round,pad=0,rounding_size=0.06"))
        ty = y + PAD_IN * 0.8 + LINE_IN / 2
        if n.kind in TAGS:
            ax.text(x + w / 2, ty, TAGS[n.kind], ha="center", va="center", fontsize=FONT - 1.5, color=edge,
                    fontweight="bold")
            ty += LINE_IN
        ax.text(x + w / 2, ty, n.title, ha="center", va="center", fontsize=FONT + 0.5, color=ink, fontweight="bold")
        for line in [x_ for x_ in n.lines if x_]:
            ty += LINE_IN
            ax.text(x + w / 2, ty, line, ha="center", va="center", fontsize=FONT, color=ink)
    # the legend: one small box per kind present, left to right
    lx = MARGIN_IN
    for k in [k for k in NAMES if any(n.kind == k for n in tree.nodes)]:
        fill, edge = colours[k]
        w = len(NAMES[k]) * CHAR_IN + 0.3
        ax.add_patch(FancyBboxPatch((lx, MARGIN_IN), w, 0.26, boxstyle="round,pad=0,rounding_size=0.06", fc=fill,
                                    ec=edge, lw=1.2, ls=(0, (4, 2)) if k in ("smps", "ldo") else "-"))
        ax.text(lx + w / 2, MARGIN_IN + 0.13, NAMES[k], ha="center", va="center", fontsize=FONT, color=ink)
        lx += w + 0.12
    if title:
        ax.text(width - MARGIN_IN, MARGIN_IN + 0.13, title, ha="right", va="center", fontsize=FONT, color=muted)
    return fig


def image(tree, fmt: str = "png", dark: bool = False, title: str = "") -> bytes:
    """The power tree as PNG or SVG bytes (rendered with Agg: safe off the GUI thread)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = figure(tree, dark, title)
    FigureCanvasAgg(fig)
    out = io.BytesIO()
    fig.savefig(out, format=fmt, facecolor=fig.get_facecolor())
    return out.getvalue()


def notes_text(tree) -> Optional[str]:
    """The tree's notes as one line (None without any)."""
    return "; ".join(tree.notes) if tree.notes else None
