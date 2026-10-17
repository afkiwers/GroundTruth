"""A 3D view of a result's current densities: one self-contained HTML page, opened in the browser.

The copper layers are stacked (their spacing exaggerated: a board is a millimetre thick), each plane layer showing
its current-density map with arrows (the heatmap's colours, more transparent where less current flows), the board
outline, the signal on its layers and its layer changes, and the vias and parts that carry the current between
layers as bars with their share. With the board-wide plane-capacitance model there is a second view: that model's
plane currents at the worst harmonic, the displacement current between each pair of planes (red: downwards, blue:
upwards, at the phase of the largest plane current) and the vias' currents. That is where a "plane displacement
12.9× signal" circulates.

Drawn with CSS 3D transforms: no WebGL, no library, nothing from the network. A Content-Security-Policy lets the page
run only its own inline code and show only its embedded (data:) images; names from the board reach the page as
escaped JSON and are written with textContent. No wx or kipy here: the plugin's "Open 3D view" button and the CLI
(`--format html`) both use it.
"""
from __future__ import annotations

import base64
import html
import io
import json
import math
import re
from pathlib import Path
from typing import Optional

import numpy as np
from matplotlib.colors import LogNorm, Normalize, to_hex
from matplotlib.figure import Figure

from ..analysis.simulate import displacement, point_transfers
from .formats import share
from .plots import CARRIER, layer_colour

SHEET_PX = 900              # a layer image's longer side
ARROWS_ACROSS = 36
DISPLACEMENT_MIN = 0.10     # a plane pair's displacement sheet shows from this share of the busiest pair's (Σ|J|)
CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "base-uri 'none'; form-action 'none'")


def file_stem(rr) -> str:
    """A file-name-safe stem for a view (net and receiver): the CLI's heatmap_<stem>.png and view3d_<stem>.html.
    Only letters, digits, '_', '.', '-' remain, so it cannot leave the folder it is written to."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{rr.net}_{rr.receiver}").strip("_.") or "view"


def has_view(rr) -> bool:
    """A view the 3D page can show: current-density maps (routes, power stages, DC paths; not temperatures)."""
    return (rr.kind != "thermal" and getattr(rr, "grid", None) is not None
            and bool(getattr(rr, "sheet_current", None)) and not getattr(rr, "error", ""))


def _png(fig) -> str:
    """A figure as a PNG data URL (transparent background), to embed in the page."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True, dpi=fig.dpi)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _canvas(grid, px: int):
    """An axes-free, transparent figure covering exactly the grid's extent, px pixels on its longer side (one
    layer's image in the 3D stack)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    x0, x1, y0, y1 = grid.extent
    w, h = x1 - x0, y1 - y0
    W, H = (px, max(int(round(px * h / w)), 2)) if w >= h else (max(int(round(px * w / h)), 2), px)
    fig = Figure(figsize=(W / 100, H / 100), dpi=100)
    FigureCanvasAgg(fig)
    fig.patch.set_alpha(0.0)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    ax.patch.set_alpha(0.0)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y1, y0)                     # KiCad's y points down
    ax.set_aspect("auto")
    return fig, ax


def _map_png(grid, K, vec, norm, ref: float, px: int = SHEET_PX) -> str:
    """|K| in the heatmap's colours; transparent off the copper, faint on idle copper, more opaque where more
    current flows; arrows as on the heatmap."""
    from matplotlib import colormaps
    from ..results.vectors import arrow_field
    fig, ax = _canvas(grid, px)
    node = np.asarray(grid.node, bool)
    K = np.asarray(K, float)
    n = np.ma.filled(norm(np.where(node & (K > 0), K, np.nan)), 0.0)
    n = np.clip(np.nan_to_num(n), 0.0, 1.0)
    rgba = colormaps["inferno"](n)
    rgba[..., 3] = np.where(node, 0.10 + 0.85 * n ** 0.8, 0.0)
    ax.imshow(np.transpose(rgba, (1, 0, 2)), origin="lower", extent=grid.extent, interpolation="nearest",
              aspect="auto")
    if vec is not None:
        arrows = arrow_field(grid, K, *vec, n_across=ARROWS_ACROSS, ref=ref)
        if arrows:
            x, y, dx, dy = (np.array(c) for c in zip(*arrows))
            length = float(np.hypot(dx[0], dy[0]))
            ax.quiver(x, y, dx, dy, angles="xy", scale_units="xy", scale=1, color="white", edgecolor="black",
                      linewidth=0.3, width=length / 6 / max(grid.extent[1] - grid.extent[0], 1e-9), headwidth=3,
                      headlength=3, headaxislength=2.7)
    return _png(fig)


def _displacement_png(grid, D, px: int = SHEET_PX) -> tuple[str, float]:
    """Displacement current density between two planes: red flowing down, blue up, opacity with its size.
    Returns (image, the density shown at full colour)."""
    from matplotlib import colormaps
    fig, ax = _canvas(grid, px)
    D = np.asarray(D, float)
    on = D != 0
    top = float(np.percentile(np.abs(D[on]), 99)) if on.any() else 1.0
    top = top or 1.0
    n = np.clip(D / top, -1.0, 1.0)
    rgba = colormaps["RdBu_r"]((n + 1) / 2)
    rgba[..., 3] = np.where(on, 0.08 + 0.72 * np.abs(n) ** 0.7, 0.0)
    ax.imshow(np.transpose(rgba, (1, 0, 2)), origin="lower", extent=grid.extent, interpolation="nearest",
              aspect="auto")
    return _png(fig), top


def _gradient(cmap: str, n: int = 12) -> str:
    """A colour map as CSS gradient stops (the page's colour bar)."""
    from matplotlib import colormaps
    c = colormaps[cmap]
    return ", ".join(f"{to_hex(c(k / (n - 1)))} {100 * k / (n - 1):.0f}%" for k in range(n))


def _rings(board) -> list:
    """The board outline's rings, closed, as coordinate lists for the page."""
    return [[[round(float(x), 4), round(float(y), 4)] for x, y in list(ring) + [ring[0]]]
            for outline, holes in board.outline for ring in [outline, *holes] if ring]


def _order(board, layers) -> list[str]:
    """The layers in stackup order (unknown names, a stack's other board's, after them)."""
    known = [l for l in board.copper_layers if l in layers]
    return known + sorted(set(layers) - set(known))


def _signal_vias(route_xy: list) -> list[tuple]:
    """(x, y, layer a, layer b) where the signal changes layer: consecutive runs on two layers that meet."""
    out = []
    for (la, pa), (lb, pb) in zip(route_xy, route_xy[1:]):
        if la != lb and pa and pb and math.dist(pa[-1], pb[0]) < 1e-6:
            out.append((float(pb[0][0]), float(pb[0][1]), la, lb))
    return out


def _bars(transfers: list, level: dict, width: float) -> list[dict]:
    """The vias and parts carrying the return between layers as vertical bars, from their top to their bottom
    layer, labelled with their share where it is at least 5 %."""
    bars = []
    for t in point_transfers(transfers):
        ls = [level[l] for l in t["layers"] if l in level]
        if not ls:
            continue
        bars.append({"x": float(t["x"]), "y": float(t["y"]), "top": min(ls), "bottom": max(ls), "colour": CARRIER,
                     "width": width, "label": share(t["fraction"]) if t["fraction"] >= 0.05 else "",
                     "title": f"{t['name']}: {share(t['fraction'])}"})
    return bars


def _mode(board, title: str, grids: dict, K: dict, vectors: dict, transfers: list, route_xy: list,
          scale: str, shares: Optional[dict] = None, unit: str = "", disp: Optional[dict] = None) -> dict:
    """One scene: sheets (layer images in stack order), lines (the signal), bars (vias, parts), outline."""
    layers = _order(board, list(K) + [l for l, _ in route_xy]
                    + [l for t in point_transfers(transfers) for l in t["layers"]])
    level = {l: i for i, l in enumerate(layers)}
    g0 = next(iter(grids.values()))
    x0, x1, y0, y1 = g0.extent
    shown = [l for l in K if np.any(np.asarray(K[l]) > 0)]
    peak = max((float(np.nanmax(K[l])) for l in shown), default=1.0) or 1.0
    norm = Normalize(0.0, peak) if scale == "linear" else LogNorm(peak * 1e-3, peak)
    from ..results.vectors import reference_density
    ref = reference_density([np.where(grids[l].node, K[l], 0.0) for l in shown]) if shown else 0.0
    sheets = []
    for l in layers:
        s = {"name": l, "level": level[l], "img": None, "note": ""}
        if l in K:
            s["img"] = _map_png(grids[l], K[l], vectors.get(l), norm, ref)
            if shares and l in shares:
                s["note"] = f"{shares[l] * 100:.0f} %"
        sheets.append(s)
    # Displacement between plane pairs: only the pairs that carry a noticeable part of it (on refboard the
    # pairs across a plane with holes, e.g. F.Cu-In2.Cu at 8 pF beside F.Cu-In1.Cu at 1161 pF, only cluttered the
    # stack), half-way between the two planes, or a quarter step below the upper one when a layer sits half-way.
    weight = {p: float(np.abs(D).sum()) for p, D in (disp or {}).items() if p[0] in level and p[1] in level}
    heaviest = max(weight.values(), default=0.0)
    for (a, b), w in weight.items():
        if w <= 0 or w < DISPLACEMENT_MIN * heaviest:
            continue
        img, top = _displacement_png(grids[a], disp[(a, b)])
        mid = (level[a] + level[b]) / 2
        sheets.append({"name": f"{a}–{b} displacement", "level": mid if mid % 1 else level[a] + 0.25, "img": img,
                       "note": f"±{top * 1e-6:.3g} A/mm² per A", "displacement": True})
    lines = [{"level": level[l], "pts": [[round(float(x), 4), round(float(y), 4)] for x, y in pts],
              "colour": layer_colour(board, l)} for l, pts in route_xy if l in level and len(pts) >= 2]
    width = max(min(x1 - x0, y1 - y0) / 150, 0.15)
    bars = [{"x": x, "y": y, "top": min(level[a], level[b]), "bottom": max(level[a], level[b]), "colour": "#b0b0b0",
             "width": width * 0.7, "label": "", "title": f"signal via {a} → {b}"}
            for x, y, a, b in _signal_vias(route_xy) if a in level and b in level]
    bars += _bars(transfers, level, width)
    return {"title": title, "x0": x0, "y0": y0, "w": x1 - x0, "h": y1 - y0, "sheets": sheets, "lines": lines,
            "bars": bars, "levels": len(layers),
            "scale": f"|K| {'linear, 0' if scale == 'linear' else 'log, 3 decades below the peak'} to "
                     f"{peak:.3g} {unit}".strip()}


def view3d_data(board, rr, scale: str = "log") -> dict:
    """The scenes of the page: the view's own maps and, with the board-wide model, its picture at the worst
    harmonic."""
    converter = rr.kind in ("converter", "dcload")
    unit = getattr(rr, "heat_unit", None) or ("A/mm²" if getattr(rr, "view_of", None) else
                                              "A/m per A of " + ("loop current" if converter else "signal"))
    what = "DC" if getattr(rr, "view_of", None) else ("hot-loop" if converter else "return")
    title = getattr(rr, "heat_title", None) or (f"{rr.net}  {rr.driver}→{rr.receiver}: {what} current @ "
                                                f"{rr.map_frequency / 1e6:.4g} MHz")
    modes = [_mode(board, title, rr.layer_grids, rr.sheet_current, getattr(rr, "sheet_vectors", None) or {},
                   rr.transfers, rr.route_xy, scale, rr.layer_share, unit)]
    bm = getattr(rr, "board_maps", None) or {}
    if bm:
        cap = displacement(rr.transfers)
        sub = f"; plane displacement {share(cap['fraction'])}" if cap else ""
        modes.append(_mode(board, f"{rr.net}: board-wide model @ {bm['f'] / 1e6:.4g} MHz (worst harmonic){sub}",
                           bm["grids"], bm["sheet_current"], bm["sheet_vectors"], bm["transfers"], rr.route_xy,
                           scale, unit="A/m per A of signal", disp=bm["displacement"]))
        modes[-1]["note"] = ("Plane currents of the board-wide model at the phase of the largest plane current; "
                             "between the planes the displacement current through their capacitance (red: "
                             "downwards, blue: upwards). Above 1× the signal current it circulates with the vias "
                             "joining the planes (a resonance; the model has no radiation loss, so its height is an "
                             "upper estimate).")
    return {"modes": modes, "outline": _rings(board), "inferno": _gradient("inferno"),
            "rdbu": _gradient("RdBu_r")}


def _json(data) -> str:
    """JSON that cannot end its <script> element or open a comment."""
    return json.dumps(data, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e") \
        .replace("&", "\\u0026")


def view3d_html(board, rr, scale: str = "log") -> str:
    """The self-contained 3D page of one view: the data as JSON (escaped so it cannot end its script element),
    the viewer's script and a Content-Security-Policy that allows nothing from outside the page."""
    data = view3d_data(board, rr, scale)
    title = html.escape(f"{rr.net} – 3D current view")
    return (_PAGE.replace("{{CSP}}", CSP).replace("{{TITLE}}", title)
            .replace("{{DATA}}", _json(data)).replace("{{SCRIPT}}", _SCRIPT))


def write_view3d(board, rr, folder: Path, scale: str = "log") -> Path:
    """Write the view's 3D page into `folder` (view3d_<stem>.html, overwritten for the same view)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"view3d_{file_stem(rr)}.html"
    path.write_text(view3d_html(board, rr, scale), encoding="utf-8")
    return path


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{{CSP}}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root { --bg:#f6f6f7; --fg:#1d1d1f; --muted:#666; --line:#d9d9de; --card:#fff; --scene:#121317; }
@media (prefers-color-scheme: dark) { :root { --bg:#16171a; --fg:#e8e8e8; --muted:#9a9a9a; --line:#33353a;
  --card:#1e2024; } }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif }
header { padding:10px 16px 6px } h1 { font-size:16px; margin:0 0 2px } .muted { color:var(--muted) }
.bar { display:flex; flex-wrap:wrap; gap:6px 14px; align-items:center; padding:6px 16px; border-bottom:1px solid var(--line) }
.bar label { display:inline-flex; gap:5px; align-items:center; white-space:nowrap }
button { font:inherit; padding:3px 10px; border:1px solid var(--line); background:var(--card); color:var(--fg);
  border-radius:6px; cursor:pointer } button.on { outline:2px solid #4c8bf5 }
#view { position:relative; height:calc(100vh - 190px); min-height:320px; overflow:hidden; background:var(--scene);
  perspective:2400px; perspective-origin:50% 38%; cursor:grab; touch-action:none; user-select:none }
#view.drag { cursor:grabbing }
#cam { position:absolute; left:50%; top:50%; width:0; height:0; transform-style:preserve-3d }
.sheet { position:absolute; transform-style:preserve-3d; outline:1px solid rgba(160,160,170,.35) }
.sheet.plain { background:rgba(150,150,160,.06) }
.sheet img, .sheet svg { position:absolute; left:0; top:0; width:100%; height:100%; pointer-events:none }
.rib { position:absolute; transform-origin:50% 0 }
.anchor { position:absolute; width:1px; height:1px }
#labels { position:absolute; inset:0; pointer-events:none; overflow:hidden }
#labels span { position:absolute; left:0; top:0; font:600 11px/1.2 system-ui,sans-serif; color:#fff;
  background:rgba(16,16,24,.78); border:1px solid rgba(255,255,255,.18); padding:1px 4px; border-radius:4px;
  white-space:nowrap; transform-origin:0 0 }
#labels span.carrier { border-color:#39e75f } #labels span.layer { background:rgba(16,16,24,.55) }
.legend { display:flex; flex-wrap:wrap; gap:8px 22px; align-items:center; padding:8px 16px }
.grad { display:inline-block; width:160px; height:10px; border-radius:2px; vertical-align:middle; margin:0 6px }
</style></head>
<body>
<header><h1 id="title"></h1><div class="muted" id="note"></div></header>
<div class="bar" id="controls">
  <span id="modes"></span>
  <label>Layer spacing <input type="range" id="gap" min="10" max="400" value="120"></label>
  <label>Opacity <input type="range" id="opacity" min="20" max="100" value="92"></label>
  <button id="topview" type="button">Top view</button><button id="reset" type="button">Reset</button>
  <span id="layers"></span>
</div>
<div id="view"><div id="cam"></div><div id="labels"></div></div>
<div class="legend"><span><span class="muted">current density</span><span class="grad" id="g1"></span>
  <span id="scale"></span></span><span id="dlegend"><span class="muted">displacement</span><span class="grad" id="g2"></span>
  <span class="muted">blue up · red down</span></span>
  <span class="muted">Drag to turn (the edge you grab follows the mouse) and tilt, wheel to zoom, double-click to reset. Layer spacing is exaggerated (equal
  steps in stack order); green bars: vias and parts carrying the current between layers, grey: the signal's own
  layer changes.</span></div>
<script type="application/json" id="gt-data">{{DATA}}</script>
<script>{{SCRIPT}}</script>
</body></html>
"""

_SCRIPT = r"""
(function () {
  'use strict';
  const D = JSON.parse(document.getElementById('gt-data').textContent);
  const view = document.getElementById('view'), cam = document.getElementById('cam');
  const overlay = document.getElementById('labels');
  const SVG = 'http://www.w3.org/2000/svg';
  const st = {mode: 0, tilt: 58, turn: -28, zoom: 1, gap: 120, opacity: 0.92, hidden: new Set()};
  let anchors = [];
  document.getElementById('g1').style.background = 'linear-gradient(90deg,' + D.inferno + ')';
  document.getElementById('g2').style.background = 'linear-gradient(90deg,' + D.rdbu + ')';

  function el(tag, cls, parent) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }
  function label(text, cls, x, y, z) {
    const a = el('i', 'anchor', cam);
    a.style.transform = 'translate3d(' + x + 'px,' + y + 'px,' + z + 'px)';
    const s = el('span', cls, overlay);
    s.textContent = text;
    anchors.push([a, s]);
    return s;
  }
  function build() {
    const m = D.modes[st.mode];
    cam.textContent = '';
    overlay.textContent = '';
    anchors = [];
    document.getElementById('title').textContent = m.title;
    document.getElementById('note').textContent = m.note || '';
    document.getElementById('scale').textContent = m.scale;
    document.getElementById('dlegend').style.display = m.sheets.some(s => s.displacement) ? '' : 'none';
    const px = Math.min(view.clientWidth * 0.6 / m.w, view.clientHeight * 0.55 / m.h);
    const W = m.w * px, H = m.h * px, mid = (m.levels - 1) / 2;
    const z = lv => -(lv - mid) * st.gap;
    const X = x => (x - m.x0 - m.w / 2) * px, Y = y => (y - m.y0 - m.h / 2) * px;
    for (const s of m.sheets) {
      if (st.hidden.has(s.name)) continue;
      const d = el('div', 'sheet' + (s.img ? '' : ' plain'), cam);
      d.style.width = W + 'px'; d.style.height = H + 'px';
      d.style.left = -W / 2 + 'px'; d.style.top = -H / 2 + 'px';
      d.style.transform = 'translateZ(' + z(s.level) + 'px)';
      if (s.img) {
        const im = el('img', '', d);
        im.src = s.img; im.alt = s.name; im.style.opacity = st.opacity;
      }
      const svg = document.createElementNS(SVG, 'svg');
      svg.setAttribute('viewBox', m.x0 + ' ' + m.y0 + ' ' + m.w + ' ' + m.h);
      svg.setAttribute('preserveAspectRatio', 'none');
      const lines = s.displacement ? [] : D.outline.map(r => [r, '#8a8a92', 1]).concat(
        m.lines.filter(l => l.level === s.level).map(l => [l.pts, l.colour, 3]));
      for (const [pts, colour, w] of lines) {
        const p = document.createElementNS(SVG, 'polyline');
        p.setAttribute('points', pts.map(q => q[0] + ',' + q[1]).join(' '));
        p.setAttribute('fill', 'none'); p.setAttribute('stroke', colour);
        p.setAttribute('stroke-width', w); p.setAttribute('vector-effect', 'non-scaling-stroke');
        svg.appendChild(p);
      }
      d.appendChild(svg);
      label(s.name + (s.note ? '  ' + s.note : ''), 'layer', -W / 2 - 4, -H / 2, z(s.level));
    }
    for (const b of m.bars) {
      const h = (b.bottom - b.top) * st.gap + 8, w = Math.max(b.width * px, b.label ? 6 : 3);
      for (const turn of [0, 90]) {
        const r = el('div', 'rib', cam);
        r.style.width = w + 'px'; r.style.height = h + 'px'; r.style.left = -w / 2 + 'px'; r.style.top = '0px';
        r.style.background = b.colour; r.title = b.title;
        r.style.transform = 'translate3d(' + X(b.x) + 'px,' + Y(b.y) + 'px,' + (z(b.top) + 4) +
          'px) rotateZ(' + turn + 'deg) rotateX(-90deg)';
      }
      if (b.label) label(b.label, 'carrier', X(b.x), Y(b.y), z(b.top) + 10);
    }
    apply();
  }
  function place() {                    // labels at their anchors' projected points, moved down where they overlap
    const v = view.getBoundingClientRect();
    const boxes = anchors.map(([a, s]) => {
      const r = a.getBoundingClientRect();
      return {s: s, x: r.left - v.left + 3, y: r.top - v.top - 16, w: s.offsetWidth, h: s.offsetHeight};
    }).sort((p, q) => p.y - q.y);
    const done = [];
    for (const b of boxes) {
      for (let moved = true; moved;) {
        moved = false;
        for (const o of done) {
          if (b.x < o.x + o.w && o.x < b.x + b.w && b.y < o.y + o.h && o.y < b.y + b.h) {
            b.y = o.y + o.h + 1;
            moved = true;
          }
        }
      }
      done.push(b);
      b.s.style.transform = 'translate(' + b.x + 'px,' + b.y + 'px)';
    }
  }
  function apply() {
    cam.style.transform = 'scale(' + st.zoom + ') rotateX(' + st.tilt + 'deg) rotateZ(' + st.turn + 'deg)';
    requestAnimationFrame(place);
  }
  function controls() {
    const modes = document.getElementById('modes');
    modes.textContent = '';
    if (D.modes.length > 1) {
      D.modes.forEach((m, i) => {
        const b = el('button', i === st.mode ? 'on' : '', modes);
        b.type = 'button';
        b.textContent = i === 0 ? 'Route model' : 'Board-wide model';
        b.onclick = () => { st.mode = i; st.hidden.clear(); controls(); build(); };
      });
    }
    const box = document.getElementById('layers');
    box.textContent = '';
    for (const s of D.modes[st.mode].sheets) {
      const l = el('label', '', box), c = el('input', '', l);
      c.type = 'checkbox'; c.checked = !st.hidden.has(s.name);
      c.onchange = () => { if (c.checked) st.hidden.delete(s.name); else st.hidden.add(s.name); build(); };
      l.appendChild(document.createTextNode(s.name));
    }
  }
  const reset = () => { st.tilt = 58; st.turn = -28; st.zoom = 1; apply(); };
  document.getElementById('reset').onclick = reset;
  document.getElementById('topview').onclick = () => { st.tilt = 0; st.turn = 0; apply(); };
  document.getElementById('gap').oninput = e => { st.gap = +e.target.value; build(); };
  document.getElementById('opacity').oninput = e => {
    st.opacity = e.target.value / 100;
    cam.querySelectorAll('img').forEach(i => { i.style.opacity = st.opacity; });
  };
  // Turning follows the part of the model that was grabbed, like a turntable under the hand: rotateZ turns the
  // stack clockwise on screen, which moves its far (upper) half the way the mouse goes but its near (lower) half
  // the other way. So a drag that starts below the model's centre (the middle of the view, where #cam sits)
  // turns the other way round, and the near edge moves with the mouse too. (The user: grabbing the front, "it
  // moves as if the user holds the back of it".) Up and down tilt the stack, the near edge following the mouse.
  let drag = null, side = 1;
  view.addEventListener('pointerdown', e => { drag = [e.clientX, e.clientY]; view.classList.add('drag');
    const v = view.getBoundingClientRect();
    side = e.clientY > v.top + v.height / 2 ? -1 : 1;
    view.setPointerCapture(e.pointerId); });
  view.addEventListener('pointermove', e => {
    if (!drag) return;
    st.turn += side * (e.clientX - drag[0]) * 0.35;
    st.tilt = Math.max(0, Math.min(89, st.tilt - (e.clientY - drag[1]) * 0.35));
    drag = [e.clientX, e.clientY];
    apply();
  });
  const end = () => { drag = null; view.classList.remove('drag'); };
  view.addEventListener('pointerup', end);
  view.addEventListener('pointercancel', end);
  view.addEventListener('wheel', e => { e.preventDefault();
    st.zoom = Math.max(0.3, Math.min(8, st.zoom * Math.exp(-e.deltaY * 0.0015))); apply(); }, {passive: false});
  view.addEventListener('dblclick', reset);
  window.addEventListener('resize', build);
  if (location.hash === '#board' && D.modes.length > 1) st.mode = 1;     // a link straight to the board-wide view
  // layer spacing to fit the stack in the window: 2 layers get the full 140 px, 6 layers less
  st.gap = Math.round(Math.max(30, Math.min(140, 0.5 * view.clientHeight / Math.max(D.modes[0].levels - 1, 1))));
  document.getElementById('gap').value = st.gap;
  controls();
  build();
})();
"""
