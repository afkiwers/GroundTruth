"""Example schematics of the fix advisor's design changes (analysis/fix_advisor.py), drawn with matplotlib: the input
π-filter between the line (LISN) and the stage, as TI WEBENCH draws its input filter; the RC snubber across the
switch node; the gate resistor of a slower edge; both together; the common-mode path to the chassis. The user, of
the Fixes tab: "It is hard to read and understand. Some example filter schematics when clicking on one".

Each part is labelled with its name and the values the advisor proposes (from the option's `change`), so the drawing
is the proposal, not a generic picture. A change without a circuit (a shorter hot loop, spread spectrum, the stage as
it is) gets None. No wx here: the window renders the figure in a background thread (ui/fixes_panel.py).
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Optional

from ..components.values import format_si


def si(v: float, unit: str) -> str:
    """A value with its SI prefix, at most three significant digits ("5.45mΩ", "26.4kHz")."""
    return format_si(float(f"{v:.3g}"), unit)

ELEMENT = 1.0          # length of a part's symbol along its wire (drawing units)
TEXT_SCALE = 1.25      # the labels' size against the parts (0.5 in per unit): readable in the tab's preview


@dataclass(frozen=True)
class Ink:
    """The drawing's colours: lines and names, values and notes, the proposed parts, the background."""
    line: str = "#1f2328"
    muted: str = "#5f6b7a"
    accent: str = "#0a84c6"
    bg: str = "#ffffff"


class Sketch:
    """A schematic on one matplotlib axis: wires, two-terminal parts between two points (horizontal or vertical),
    grounds, boxes and labels. Parts are drawn in their own frame (s along the part, t across it) and turned onto
    the line from a to b."""

    def __init__(self, ink: Ink, width: float, height: float, scale: float = 0.5):
        """An empty drawing of width x height units (scale inches per unit)."""
        from matplotlib.figure import Figure
        self.ink = ink
        self.fig = Figure(figsize=(width * scale, height * scale), dpi=110)
        self.fig.patch.set_facecolor(ink.bg)
        self.ax = self.fig.add_axes((0, 0, 1, 1))
        self.ax.set_xlim(-1.6, width - 1.6)
        self.ax.set_ylim(-0.9, height - 0.9)
        self.ax.set_aspect("equal")
        self.ax.axis("off")

    # ---------------------------------------------------------------- primitives
    def wire(self, *pts, dashed: bool = False, colour: Optional[str] = None):
        """A wire through the points."""
        xs, ys = zip(*pts)
        self.ax.plot(xs, ys, color=colour or self.ink.line, lw=1.4, ls="--" if dashed else "-",
                     solid_capstyle="round")

    def dot(self, p):
        """A junction."""
        self.ax.plot([p[0]], [p[1]], "o", color=self.ink.line, ms=4)

    def text(self, p, s: str, colour: Optional[str] = None, size: float = 8.5, **kw):
        """Text at p (matplotlib's alignment keywords); sizes are scaled by TEXT_SCALE (the drawing is shown small
        beside the Fixes tab's text, where the labels must stay readable)."""
        self.ax.text(p[0], p[1], s, color=colour or self.ink.line, fontsize=size * TEXT_SCALE, **kw)

    def _part(self, a, b, shape, name: str, value: str, side: int, proposed: bool, dashed: bool = False):
        """A part between a and b: wires to the symbol's ends, the symbol (shape: [(s, t), ...] polylines in the
        part's frame, s from 0 to ELEMENT), its name and value beside it (side +1 / -1: left or above / the
        other side)."""
        (ax_, ay), (bx, by) = a, b
        length = math.hypot(bx - ax_, by - ay)
        ux, uy = (bx - ax_) / length, (by - ay) / length
        nx, ny = -uy, ux
        s0 = (length - ELEMENT) / 2
        start = (ax_ + ux * s0, ay + uy * s0)
        end = (ax_ + ux * (s0 + ELEMENT), ay + uy * (s0 + ELEMENT))
        self.wire(a, start)
        self.wire(end, b)
        colour = self.ink.accent if proposed else self.ink.line
        for poly in shape:
            pts = [(start[0] + ux * s + nx * t, start[1] + uy * s + ny * t) for s, t in poly]
            self.wire(*pts, colour=colour, dashed=dashed)
        mid = (start[0] + ux * ELEMENT / 2 + nx * 0.42 * side, start[1] + uy * ELEMENT / 2 + ny * 0.42 * side)
        vertical = abs(uy) > abs(ux)
        # the text grows away from the part: right of it (left-aligned) when the offset points right, and so on
        ha = ("left" if nx * side > 0 else "right") if vertical else "center"
        va = "center" if vertical else ("bottom" if ny * side > 0 else "top")
        label = _subscript(name) + ("\n" + value if value else "")
        self.text(mid, label, colour=colour, ha=ha, va=va, size=8.5, linespacing=1.15)

    def resistor(self, a, b, name: str, value: str = "", side: int = 1, proposed: bool = True):
        """A resistor (zig-zag) from a to b."""
        n = 6
        pts = [(0.0, 0.0), (0.1, 0.0)]
        for k in range(n):
            pts.append((0.1 + (k + 0.5) * 0.8 / n, 0.16 * (1 if k % 2 == 0 else -1)))
        pts += [(0.9, 0.0), (ELEMENT, 0.0)]
        self._part(a, b, [pts], name, value, side, proposed)

    def inductor(self, a, b, name: str, value: str = "", side: int = 1, proposed: bool = True):
        """An inductor (four loops) from a to b."""
        pts = [(0.0, 0.0), (0.1, 0.0)]
        for k in range(4):
            for j in range(13):
                ang = math.pi * j / 12
                pts.append((0.1 + 0.2 * k + 0.1 - 0.1 * math.cos(ang), 0.14 * math.sin(ang)))
        pts += [(0.9, 0.0), (ELEMENT, 0.0)]
        self._part(a, b, [pts], name, value, side, proposed)

    def capacitor(self, a, b, name: str, value: str = "", side: int = 1, proposed: bool = True,
                  polar: bool = False, dashed: bool = False):
        """A capacitor (two plates; polar: the lower plate curved, an electrolytic or polymer) from a to b."""
        g = 0.09
        shape = [[(0.0, 0.0), (0.5 - g, 0.0)], [(0.5 - g, -0.24), (0.5 - g, 0.24)], [(0.5 + g, 0.0), (ELEMENT, 0.0)]]
        if polar:
            shape.append([(0.5 + g + 0.06 * (1 - math.cos(math.pi * (k / 8 - 0.5))), 0.24 * (k / 4 - 1))
                          for k in range(9)])
            shape.append([(0.5 - g - 0.16, 0.18), (0.5 - g - 0.16, 0.06)])        # the + sign
            shape.append([(0.5 - g - 0.22, 0.12), (0.5 - g - 0.10, 0.12)])
        else:
            shape.append([(0.5 + g, -0.24), (0.5 + g, 0.24)])
        self._part(a, b, shape, name, value, side, proposed, dashed=dashed)

    def ground(self, p):
        """A ground symbol below p."""
        x, y = p
        self.wire((x, y), (x, y - 0.18))
        for k, w in enumerate((0.22, 0.14, 0.06)):
            self.wire((x - w, y - 0.18 - 0.08 * k), (x + w, y - 0.18 - 0.08 * k))

    def chassis(self, p):
        """A chassis (frame) ground below p."""
        x, y = p
        self.wire((x, y), (x, y - 0.2))
        self.wire((x - 0.24, y - 0.2), (x + 0.24, y - 0.2))
        for k in range(3):
            self.wire((x - 0.2 + 0.2 * k, y - 0.2), (x - 0.3 + 0.2 * k, y - 0.34))

    def box(self, x0, y0, x1, y1, label: str, dashed: bool = False, colour: Optional[str] = None):
        """A labelled box (a block: the power stage, the LISN, a driver)."""
        c = colour or self.ink.line
        self.wire((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0), dashed=dashed, colour=c)
        self.text(((x0 + x1) / 2, (y0 + y1) / 2), label, colour=c, ha="center", va="center", size=8.5,
                  linespacing=1.2)

    def source(self, top, bottom, label: str):
        """A DC source (circle, + at the top) from bottom to top."""
        from matplotlib.patches import Circle
        x = top[0]
        my = (top[1] + bottom[1]) / 2
        self.wire(top, (x, my + 0.3))
        self.wire((x, my - 0.3), bottom)
        self.ax.add_patch(Circle((x, my), 0.3, fill=False, ec=self.ink.line, lw=1.4))
        self.text((x, my + 0.1), "+", ha="center", va="center", size=9)
        self.text((x, my - 0.14), "−", ha="center", va="center", size=9)
        self.text((x - 0.42, my), label, ha="right", va="center")

    def switch(self, top, bottom, label: str, side: int = -1):
        """A transistor as a switch (a MOSFET's channel, drain at the top) from bottom to top, its name beside it."""
        x = top[0]
        my = (top[1] + bottom[1]) / 2
        self.wire(top, (x, my + 0.3), (x - 0.14, my + 0.3))
        self.wire((x, my - 0.3), bottom)
        self.wire((x, my - 0.3), (x - 0.14, my - 0.3))
        self.wire((x - 0.14, my - 0.36), (x - 0.14, my + 0.36))
        self.wire((x - 0.24, my - 0.3), (x - 0.24, my + 0.3))          # the gate
        self.wire((x - 0.24, my - 0.3), (x - 0.5, my - 0.3))
        self.text((x + 0.2 * -side if side < 0 else x - 0.6, my), label, ha="left" if side < 0 else "right",
                  va="center")

    def note(self, s: str):
        """A note under the drawing."""
        self.text((self.ax.get_xlim()[0] + 0.1, self.ax.get_ylim()[0] + 0.12), s, colour=self.ink.muted, size=8,
                  ha="left", va="bottom")


def _subscript(name: str) -> str:
    """A part's name with its subscript set as one ("C_f": C with f below, "R_g,on": R with g,on below)."""
    if "_" not in name:
        return name
    base, sub = name.split("_", 1)
    return f"${base}_{{{sub}}}$"


# ---------------------------------------------------------------- the design changes
def _lisn_name(standard: str) -> str:
    """The test network the stage sees at its connector, in words."""
    return {"cispr25": "LISN\n5 µH ∥ 50 Ω", "mil461": "LISN\n50 µH"}.get(standard or "", "LISN\n50 µH ∥ 50 Ω")


def draw_filter(flt: dict, stage: str, ink: Ink):
    """The input π-filter as the advisor proposes it: the line (source and LISN), the connector, C_f, the damping
    R_d + C_d where it was chosen (at the connector or across the stage's input capacitors), L_f, the stage's own
    input capacitors and the stage. Each shunt's text stands right of it, so the next part is GAP further on."""
    gap = 3.1
    conv = flt.get("damp_at") == "converter"
    x_c = 4.0                                            # C_f
    x_dc = x_c + gap if not conv else None               # damping at the connector
    x_l0 = (x_dc or x_c) + 0.6                           # L_f
    x_l1 = x_l0 + 2.4
    x_in = x_l1 + 1.0                                    # the stage's input capacitors
    x_dv = x_in + gap if conv else None                  # damping at the stage
    x_box = (x_dv or x_in) + gap
    sk = Sketch(ink, x_box + 4.2, 6.8)
    top, bot = 3.6, 0.6
    sk.source((0, top), (0, bot), "V in")
    sk.wire((0, top), (0.4, top))
    sk.box(0.4, top - 0.6, 2.8, top + 0.6, _lisn_name(flt.get("standard", "")))
    sk.wire((0, bot), (x_box, bot))
    sk.wire((2.8, top), (x_l0, top))
    sk.wire((3.3, -0.3), (3.3, 5.0), dashed=True, colour=ink.muted)
    sk.text((1.6, 5.0), "line, LISN", colour=ink.muted, ha="center", va="bottom")
    sk.text((3.4, 5.0), "connector", colour=ink.muted, ha="left", va="bottom")
    v = flt.get("v")
    c_value = f"{si(flt.get('C_nom', flt['C']), 'F')} X7R"
    if flt.get("v_rating"):
        c_value += f"\n≥ {flt['v_rating']:.0f} V rating"
    if v:
        c_value += f"\n≈ {si(flt['C'], 'F')} at {v:g} V"
    sk.capacitor((x_c, top), (x_c, bot), "C_f", c_value)
    mid = (top + bot) / 2

    def shunt_dots(x):
        """The junctions of a shunt branch on both rails."""
        sk.dot((x, top))
        sk.dot((x, bot))

    def damping(xd):
        """The damping branch R_d + C_d to ground at xd."""
        sk.resistor((xd, top), (xd, mid), "R_d", si(flt["R_damp"], "Ω"))
        sk.capacitor((xd, mid), (xd, bot), "C_d", f"{si(flt['C_damp'], 'F')}\nelectrolytic\nor polymer", polar=True)
        shunt_dots(xd)
    shunt_dots(x_c)
    if x_dc:
        damping(x_dc)
    l_value = si(flt["L"], "H") + (f", I_sat ≥ {flt['i_sat']:.2g} A" if flt.get("i_sat") else "")
    if flt.get("dcr"):
        l_value += f"\nDCR ≈ {si(flt['dcr'], 'Ω')}"
    sk.inductor((x_l0, top), (x_l1, top), "L_f", l_value)
    sk.wire((x_l1, top), (x_box, top))
    c_in = flt.get("c_in")
    sk.capacitor((x_in, top), (x_in, bot), "C_in", (f"{si(c_in, 'F')}\n" if c_in else "") + "the stage's input\n"
                 "capacitors", proposed=False)
    shunt_dots(x_in)
    if x_dv:
        damping(x_dv)
    sk.box(x_box, bot - 0.2, x_box + 2.4, top + 0.2, f"power stage\n{stage}")
    sk.text((x_box + 2.4, 5.0), "blue: proposed\nblack: on the board or the test bench", colour=ink.muted,
            ha="right", va="bottom", size=8)
    if flt.get("f_c"):
        sk.note(f"corner f_c = 1/(2π√(L_f·C_f)) ≈ {si(flt['f_c'], 'Hz')}: the filter falls 40 dB/decade above it")
    return sk.fig


def _half_bridge(sk: Sketch, hs: str, ls: str, boost: bool):
    """A half bridge at x = 2: the rail at the top, the switch node in the middle, ground at the bottom, the
    inductor to the output on the right. Returns (rail y, switch node point, ground y)."""
    rail, sw, gnd = 5.0, (2.0, 2.8), 0.6
    sk.wire((0.6, rail), (2.0, rail))
    sk.text((0.5, rail), "V in" if not boost else "V out", ha="right", va="center")
    sk.switch((2.0, rail), (2.0, sw[1]), hs or "high side")
    sk.switch((2.0, sw[1]), (2.0, gnd), ls or "low side")
    sk.wire((0.6, gnd), (7.2, gnd))
    sk.ground((0.9, gnd))
    sk.dot(sw)
    sk.inductor((5.0, sw[1]), (7.2, sw[1]), "L", "", side=1, proposed=False)
    sk.wire(sw, (5.0, sw[1]))
    sk.text((7.3, sw[1]), "V out" if not boost else "V in", ha="left", va="center")
    sk.text((sw[0] + 0.15, sw[1] - 0.12), "SW", colour=sk.ink.muted, ha="left", va="top", size=8)
    return rail, sw, gnd


def draw_switching(change: dict, stage: str, ink: Ink, hs: str = "", ls: str = "", boost: bool = False,
                   rise_now: Optional[float] = None, loop_nh: Optional[float] = None):
    """The snubber (R_s + C_s from the switch node to ground; to the rail for a boost), the gate resistor of a
    slower edge, or both (the "together" change), on a half bridge."""
    snub = change.get("snubber_c") is not None
    edge = change.get("rise_time") is not None
    sk = Sketch(ink, 12.2, 6.6)
    rail, sw, gnd = _half_bridge(sk, hs, ls, boost)
    if snub:
        x = 3.7                         # clear of the switches' names
        sk.dot((x, sw[1]))
        end = (x, gnd) if not boost else (x, rail)
        mid = (x, (sw[1] + end[1]) / 2)
        sk.resistor((x, sw[1]), mid, "R_s", si(change["snubber_r"], "Ω"), side=1 if not boost else -1)
        sk.capacitor(mid, end, "C_s", si(change["snubber_c"], "F") + " C0G", side=1 if not boost else -1)
        if boost:
            sk.wire((2.0, rail), (x, rail))
        sk.dot(end)
        sk.note("snubber across the switch node:\nclose to the switches, its own short loop")
    if edge:
        # the gate driver and the turn-on resistor of the edge-controlled switch (the high side of a buck)
        # (a boost's low side); the gate lead of Sketch.switch ends 0.5 left of the switch, 0.3 under its middle
        gy = ((rail + sw[1]) / 2 if not boost else (sw[1] + gnd) / 2) - 0.3
        sk.box(-2.0, gy - 0.35, -1.0, gy + 0.35, "driver")
        sk.resistor((-1.0, gy), (1.5, gy), "R_g,on", f"edge {si(change['rise_time'], 's')}"
                    + (f"\n(now {si(rise_now, 's')})" if rise_now else ""), side=-1)
        if not snub:
            sk.note("turn-on gate resistor: slows the switch node's\nedge (more switching loss)")
    if change.get("loop_scale") not in (None, 1.0) and loop_nh:
        sk.text((5.0, rail + 0.25), f"hot loop ≤ {loop_nh * change['loop_scale']:.2g} nH (layout)",
                colour=ink.accent, ha="center", va="bottom")
    sk.ax.set_xlim(-2.2, sk.ax.get_xlim()[1])
    return sk.fig


def draw_common_mode(stage: str, budget_pf: Optional[float], ink: Ink):
    """The common-mode path: the switch node's stray capacitance to the chassis (the budget), and the usual
    remedy at the input, a common-mode choke with Y capacitors from each line to the chassis. The chassis is one
    rail at the bottom, so the loop (stage, C_stray, chassis, both LISNs) can be followed; the + line's Y capacitor
    crosses the − line without a junction (no dot)."""
    sk = Sketch(ink, 15.8, 8.0)
    top, bot, ch = 5.2, 3.6, 0.9
    sk.box(0.0, top - 0.45, 1.6, top + 0.45, "LISN (+)")
    sk.box(0.0, bot - 0.45, 1.6, bot + 0.45, "LISN (−)")
    sk.wire((1.6, top), (3.0, top))
    sk.wire((1.6, bot), (3.0, bot))
    sk.inductor((3.0, top), (5.0, top), "L_cm", "common-mode choke")
    sk.inductor((3.0, bot), (5.0, bot), "", "", side=-1)
    for y in ((top + bot) / 2 + 0.1, (top + bot) / 2 - 0.1):          # the choke's shared core
        sk.wire((3.5, y), (4.5, y), colour=ink.accent)
    sk.wire((5.0, top), (8.9, top))
    sk.wire((5.0, bot), (8.9, bot))
    sk.capacitor((6.9, top), (6.9, ch), "C_Y", "+ line")
    sk.capacitor((5.7, bot), (5.7, ch), "C_Y", "− line", side=-1)
    sk.dot((6.9, top))
    sk.dot((5.7, bot))
    sk.box(8.9, bot - 0.5, 11.1, top + 0.5, f"power stage\n{stage}")
    sk.dot((10.0, bot - 0.5))
    sk.capacitor((10.0, bot - 0.5), (10.0, ch), "C_stray", (f"≤ {budget_pf:.2g} pF" if budget_pf else "")
                 + "\nswitch node to\nchassis / heatsink", proposed=False, dashed=True)
    sk.wire((0.8, ch), (10.0, ch))
    sk.dot((5.7, ch))
    sk.dot((6.9, ch))
    sk.chassis((0.8, ch))
    sk.text((1.1, ch - 0.12), "chassis", colour=ink.muted, ha="left", va="top")
    sk.note("the switch node's dV/dt drives current through C_stray; it returns through both LISNs.\n"
            "Blue: the usual remedy, a common-mode choke with Y capacitors to the chassis")
    return sk.fig


def figure_for(option: dict, stage: str, ink: Ink, result=None):
    """The schematic of one design change (fix_advisor's option dict), or None for a change without a circuit.
    result: the stage's ConverterResult (its switches, topology, edge and hot loop for the labels)."""
    kind, change = option.get("kind"), option.get("change") or {}
    hs = getattr(result, "driver", "") or ""
    ls = getattr(result, "receiver", "") or ""
    boost = getattr(result, "topology", "") == "boost"
    if kind == "filter" and change.get("filter"):
        return draw_filter(change["filter"], stage, ink)
    if kind in ("snubber", "edge", "combined") and (change.get("snubber_c") is not None
                                                     or change.get("rise_time") is not None):
        return draw_switching(change, stage, ink, hs, ls, boost, rise_now=getattr(result, "rise_time", None) or None,
                              loop_nh=getattr(result, "loop_nh", None))
    if kind == "cm":
        import re
        m = re.search(r"at most ([0-9.eE+-]+) pF", option.get("title", ""))
        return draw_common_mode(stage, float(m.group(1)) if m else None, ink)
    return None


def png(fig) -> bytes:
    """A figure as PNG bytes (rendered with Agg: safe off the GUI thread)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    FigureCanvasAgg(fig)
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=fig.get_facecolor())
    return out.getvalue()
