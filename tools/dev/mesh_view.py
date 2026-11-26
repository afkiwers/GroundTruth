"""A 3D view of the meshes Simulate solves a board's signal paths on: one self-contained HTML page.

For each path named, Simulate builds its route model (the copper around the route at a fine pitch) and, with plane
capacitance on, the board-wide model (every plane of the whole board at a coarser pitch). This tool runs that
Simulate and draws both models' meshes: per copper layer the nodes and the strips joining neighbouring nodes (the
PEEC cells the currents flow in, coloured by net), the vias and parts joining layers (vertical), the signal's route
and the board outline. Layers are stacked with their spacing exaggerated (a slider), as the 3D view of the results.

    .venv/Scripts/python tools/dev/mesh_view.py BOARD SETTINGS OUT.html [--nets N1,N2] [--resolution 2]

Git Bash turns a net name starting with "/" into a Windows path: run it with MSYS_NO_PATHCONV=1. The board is only
read; nothing is written next to it (pass a settings file in a scratch folder).

The page draws with a 2D canvas (its own small projection: no WebGL, no library, nothing from the network) under a
Content-Security-Policy that runs only its own inline code; names from the board reach it as escaped JSON and are
written with textContent. Controls as the 3D model viewer's: a drag turns the model about the screen's
axes (no limits, a flick keeps turning), right / middle / Shift+drag pans, the wheel zooms towards the cursor,
two fingers pinch, a double-click resets the view.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.adapters.file_source import load_board            # noqa: E402
from groundtruth.analysis.simulate import BoardModel, simulate      # noqa: E402
from groundtruth.config.settings import SettingsFile                # noqa: E402

CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; "
       "form-action 'none'")
PALETTE = ["#4e9af1", "#e8833a", "#53b77a", "#c45ab3", "#d9c84a", "#5cc6c9", "#e05d5d", "#9b8cf0"]


def _layer_mesh(grid) -> dict:
    """One layer's mesh as flat integer lists: the nodes (i, j, net index) and the x / y strips' start nodes."""
    ii, jj = np.nonzero(grid.node)
    label = grid.label if grid.label is not None else np.zeros(grid.node.shape, int)
    xi, xj = np.nonzero(grid.xb)
    yi, yj = np.nonzero(grid.yb)
    return {"x0": grid.x0, "y0": grid.y0, "d": grid.d, "nx": grid.nx, "ny": grid.ny,
            "nets": list(grid.nets) or [""],
            "nodes": np.column_stack([ii, jj, label[ii, jj]]).ravel().tolist(),
            "xs": np.column_stack([xi, xj, label[xi, xj]]).ravel().tolist(),
            "ys": np.column_stack([yi, yj, label[yi, yj]]).ravel().tolist()}


def _links(st) -> list:
    """The vias and parts joining two layers (or two nodes): [kind, name, x0, y0, layer0, x1, y1, layer1]; a
    branch to a node off the layer grids (a part's lid) is drawn to the lid's point when the stack can place it."""
    out = []
    for b in st.branches:
        if b.kind not in ("via", "part"):
            continue
        try:
            (xa, ya), (xb, yb) = st.xy(b.a), st.xy(b.b)
        except Exception:       # noqa: BLE001 - a node the stack cannot place: left out of the picture
            continue
        la = b.a[0] if b.a[0] < len(st.names) else -1
        lb = b.b[0] if b.b[0] < len(st.names) else -1
        out.append([b.kind, b.name, float(xa), float(ya), int(la), float(xb), float(yb), int(lb)])
    return out


def _model(title: str, st) -> dict:
    """One model: its layers (top first), their heights (mm), its pitch and its meshes and links."""
    return {"title": title, "pitch": st.d, "layers": list(st.names), "z_mm": [z * 1e3 for z in st.z],
            "meshes": [_layer_mesh(g) for g in st.grids], "links": _links(st), "nodes": st.nodes}


def _outline(model: BoardModel) -> list:
    """The board outline's rings (exteriors and holes) as [[x, y], ...] lists."""
    o = model.copper.outline
    rings = []
    for g in getattr(o, "geoms", [o]):
        if g.is_empty:
            continue
        rings.append([list(p) for p in g.exterior.coords])
        rings += [[list(p) for p in r.coords] for r in g.interiors]
    return rings


def collect(board_path: Path, settings_path: Path, nets: list[str] | None, resolution: float | None) -> dict:
    """Simulate the paths and gather every distinct model's mesh (a board-wide model shared by several paths once)."""
    board = load_board(board_path)
    cfg = SettingsFile.load(settings_path).config
    if resolution:
        cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
    model = BoardModel(board, cfg)
    res = simulate(board, cfg, nets=nets, model=model, keep_factorisations=False)
    models, seen, routes, errors = [], set(), [], []
    for s in res.setups:
        name = s.spec.name
        routes.append({"name": name, "segments": [[seg.start[0], seg.start[1], seg.end[0], seg.end[1], seg.layer]
                                                 for seg in s.route.segments]})
        for title, run in ((f"{name}: route model", s.fine), ("board-wide model", s.cavity)):
            if run is None:
                continue
            if id(run.build) in seen:       # a shared model (the board-wide one): it shows every path using it
                next(m for m in models if m["_id"] == id(run.build))["routes"].append(len(routes) - 1)
                continue
            seen.add(id(run.build))
            models.append({**_model(title, run.build), "_id": id(run.build), "routes": [len(routes) - 1]})
    for m in models:
        m.pop("_id")
    for r in res.routes:
        if r.error:
            errors.append(f"{getattr(r, 'net', '')}: {r.error}")
    return {"board": board_path.stem, "outline": _outline(model), "models": models, "routes": routes,
            "errors": errors}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="__CSP__">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Simulation mesh</title>
<style>
:root { --bg:#f6f7f9; --panel:#ffffff; --ink:#1d2430; --muted:#5d6878; --line:#d5dae2; --accent:#2f6fd6; }
@media (prefers-color-scheme: dark) { :root { --bg:#12161c; --panel:#1b2129; --ink:#e4e8ee; --muted:#9aa5b4;
  --line:#2c3440; --accent:#6ea2ff; } }
* { box-sizing: border-box; }
html, body { margin:0; height:100%; background:var(--bg); color:var(--ink);
  font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
#app { display:flex; height:100%; }
aside { width:300px; flex:none; overflow:auto; padding:16px; background:var(--panel); border-right:1px solid var(--line); }
main { flex:1; position:relative; min-width:0; }
canvas { display:block; width:100%; height:100%; cursor:grab; touch-action:none; }
h1 { font-size:16px; margin:0 0 4px; } h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:18px 0 6px; }
.sub { color:var(--muted); font-size:12px; }
label { display:flex; align-items:center; gap:8px; margin:4px 0; cursor:pointer; }
select, input[type=range] { width:100%; }
select { padding:4px; background:var(--bg); color:var(--ink); border:1px solid var(--line); border-radius:4px; }
.sw { width:12px; height:12px; border-radius:2px; flex:none; }
.err { color:#d0453a; font-size:12px; margin-top:6px; }
#hint { position:absolute; right:12px; bottom:10px; color:var(--muted); font-size:12px; pointer-events:none;
  text-align:right; }
/* the centre of rotation, shown while the model turns (the 3D model viewer's pivot marker) */
#pivot { position:absolute; width:24px; height:24px; margin:-12px 0 0 -12px; border-radius:50%;
  border:2px solid #ef5a24; pointer-events:none; box-shadow:0 0 0 2px rgba(255,255,255,.8), inset 0 0 0 2px rgba(255,255,255,.8);
  opacity:0; transform:scale(.5); transition:opacity .25s, transform .25s; }
#pivot::before, #pivot::after { content:""; position:absolute; left:50%; top:50%; background:#ef5a24;
  box-shadow:0 0 0 1px rgba(255,255,255,.8); }
#pivot::before { width:2px; height:38px; margin:-19px 0 0 -1px; }
#pivot::after { width:38px; height:2px; margin:-1px 0 0 -19px; }
#pivot.on { opacity:1; transform:none; }
#gizmo { position:absolute; left:12px; bottom:12px; width:170px; user-select:none; }
#gizmo canvas { width:170px; height:170px; cursor:pointer; touch-action:none; }
.gbtns { display:flex; gap:4px; padding:0 8px; }
.gbtns span { flex:1; }
.gbtns button { width:28px; height:28px; border:1px solid var(--line); border-radius:6px; background:var(--panel);
  color:var(--ink); font-size:15px; line-height:1; cursor:pointer; }
.gbtns button:hover { border-color:var(--accent); color:var(--accent); }
#stats { font-size:12px; color:var(--muted); }
@media (max-width: 700px) { #app { flex-direction:column; } aside { width:auto; max-height:45%;
  border-right:none; border-bottom:1px solid var(--line); } }
</style></head>
<body><div id="app">
<aside>
  <h1 id="title"></h1><div class="sub">The meshes Simulate solves on (PEEC nodes and strips)</div>
  <h2>Model</h2><select id="model"></select><div id="stats"></div>
  <h2>Layers</h2><div id="layers"></div>
  <h2>Nets</h2><div id="nets"></div>
  <h2>Show</h2>
  <label><input type="checkbox" id="showStrips" checked> strips (cell edges)</label>
  <label><input type="checkbox" id="showNodes"> nodes</label>
  <label><input type="checkbox" id="showLinks" checked> vias and parts</label>
  <label><input type="checkbox" id="showRoute" checked> signal route</label>
  <label><input type="checkbox" id="showOutline" checked> board outline</label>
  <h2>Orbit</h2>
  <label><input type="checkbox" id="orbitOn"> auto-orbit (pauses while you drag)</label>
  <input type="range" id="orbitSpeed" min="2" max="90" value="15" aria-label="orbit speed">
  <div class="sub" id="orbitText">15°/s</div>
  <h2>Drag</h2>
  <select id="dragMode" aria-label="drag mode">
    <option value="free">Free: turn any way, no limits</option>
    <option value="turntable">Turntable: keep the board upright</option>
  </select>
  <h2>Layer spacing</h2><input type="range" id="spread" min="1" max="60" value="12">
  <div class="sub">exaggerated: the real stack is ~1.6 mm thick</div>
  <div id="errors"></div>
</aside>
<main><canvas id="c"></canvas><div id="pivot"></div>
<div id="gizmo">
  <div class="gbtns"><button id="home" title="Reset the view" aria-label="reset the view">⌂</button>
    <span></span><button id="rollL" title="Roll 90° left" aria-label="roll left">↺</button>
    <button id="rollR" title="Roll 90° right" aria-label="roll right">↻</button></div>
  <canvas id="cube" width="170" height="170" title="Click a face to look at it; drag the cube to turn, the ring to spin"></canvas>
</div>
<div id="hint">drag: rotate · right / middle / shift+drag: pan · wheel: zoom at the cursor · double-click: reset</div></main>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const PAL = __PALETTE__;
const $ = id => document.getElementById(id);
const cv = $("c"), ctx = cv.getContext("2d");
// The view: a rotation matrix R (rows: screen x right, y up, z towards the viewer) applied to the board's
// centred coordinates about the pivot P, and an orthographic scale (zoom). P is the centre of rotation: it always
// projects to the canvas centre, and panning or zooming at the cursor moves P (in model space) instead of the
// picture, so the model slides under a pivot that stays put (as the 3D model viewer's target). A drag turns it about the
// screen's own axes (a free trackball, as the 3D model viewer does): the surface under the cursor follows it,
// whatever the current orientation, and there is no angle at which it stops.
const ROTATE_SPEED = 0.006;
// the start: tilted back and turned a little about the board's normal, so the cube shows TOP, FRONT and RIGHT
const INITIAL = () => mul(rotX(-0.85), rotZ(-0.45));
let model = 0, R = INITIAL(), zoom = 1, P = [0, 0, 0], spread = 12;
let layerOn = [], netOn = {}, netColour = {};

$("title").textContent = D.board;
D.models.forEach((m, k) => { const o = document.createElement("option"); o.value = k; o.textContent = m.title;
  $("model").appendChild(o); });
D.errors.forEach(e => { const d = document.createElement("div"); d.className = "err"; d.textContent = e;
  $("errors").appendChild(d); });
let ci = 0;
D.models.forEach(m => m.meshes.forEach(me => me.nets.forEach(n => {
  if (!(n in netColour)) { netColour[n] = PAL[ci++ % PAL.length]; netOn[n] = true; } })));

function checkbox(parent, text, on, colour, change) {
  const l = document.createElement("label"), c = document.createElement("input");
  c.type = "checkbox"; c.checked = on; c.addEventListener("change", () => { change(c.checked); draw(); });
  l.appendChild(c);
  if (colour) { const s = document.createElement("span"); s.className = "sw"; s.style.background = colour; l.appendChild(s); }
  const t = document.createElement("span"); t.textContent = text; l.appendChild(t); parent.appendChild(l);
}
function buildPanels() {
  const m = D.models[model];
  $("layers").textContent = ""; $("nets").textContent = "";
  layerOn = m.layers.map(() => true);
  m.layers.forEach((n, k) => checkbox($("layers"), n, true, null, v => { layerOn[k] = v; }));
  const nets = new Set(); m.meshes.forEach(me => me.nets.forEach(n => nets.add(n)));
  [...nets].forEach(n => checkbox($("nets"), n || "(no net)", netOn[n], netColour[n], v => { netOn[n] = v; }));
  const strips = m.meshes.reduce((a, me) => a + (me.xs.length + me.ys.length) / 3, 0);
  $("stats").textContent = `pitch ${m.pitch.toFixed(3)} mm · ${m.nodes.toLocaleString()} nodes · ` +
    `${Math.round(strips).toLocaleString()} strips · ${m.links.length} vias/parts`;
  // centre and scale on the model's extent
  let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
  m.meshes.forEach(me => { x0 = Math.min(x0, me.x0); y0 = Math.min(y0, me.y0);
    x1 = Math.max(x1, me.x0 + (me.nx - 1) * me.d); y1 = Math.max(y1, me.y0 + (me.ny - 1) * me.d); });
  m.cx = (x0 + x1) / 2; m.cy = (y0 + y1) / 2; m.size = Math.max(x1 - x0, y1 - y0, 1);
  resetView();
}
function resetView() { R = INITIAL(); P = [0, 0, 0]; zoom = 1; vel = [0, 0]; }
// move the picture by (dx, dy) canvas pixels: the pivot moves the other way in model space (Rᵀ: screen to model)
function shift(dx, dy) {
  const s = scale(D.models[model]), u = -dx / s, v = dy / s;        // screen y points down, model-view y up
  P = [P[0] + R[0] * u + R[3] * v, P[1] + R[1] * u + R[4] * v, P[2] + R[2] * u + R[5] * v];
}
// 3x3 rotations about the screen axes, and their product
function rotX(a) { const c = Math.cos(a), s = Math.sin(a); return [1, 0, 0, 0, c, -s, 0, s, c]; }
function rotY(a) { const c = Math.cos(a), s = Math.sin(a); return [c, 0, s, 0, 1, 0, -s, 0, c]; }
function mul(A, B) {
  const C = new Array(9);
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++)
    C[3 * i + j] = A[3 * i] * B[j] + A[3 * i + 1] * B[3 + j] + A[3 * i + 2] * B[6 + j];
  return C;
}
function rotZ(a) { const c = Math.cos(a), s = Math.sin(a); return [c, -s, 0, s, c, 0, 0, 0, 1]; }
function spin(a) { R = mul(R, rotZ(a)); orthonormal(); }    // about the board's own vertical (its normal)
// the pivot marker: shown while the model turns by hand (a drag, a flick's inertia, the cube), faded out 0.9 s
// after the last turn; the auto-orbit's steady spin does not show it. It sits where the model turns about: the
// pivot P, which the projection always puts at the canvas centre
let pivotTimer = 0;
function showPivot() {
  $("pivot").classList.add("on");
  clearTimeout(pivotTimer);
  pivotTimer = setTimeout(() => $("pivot").classList.remove("on"), 900);
}
function placePivot() {
  const dpr = window.devicePixelRatio || 1, el = $("pivot");
  el.style.left = `${cv.width / 2 / dpr}px`; el.style.top = `${cv.height / 2 / dpr}px`;
}
function turn(dx, dy) {
  showPivot();
  if ($("dragMode").value === "turntable") {
    // turntable (the model viewer's orbit: turntable): sideways spins about the board's normal, which stays
    // upright on screen; up / down tilts about the screen's horizontal axis, stopping straight above and below.
    // t: the normal's angle from the viewing direction, kept in (0, π) (R[5] >= 0: the normal never points down)
    spin(dx * ROTATE_SPEED);
    const t = Math.atan2(R[5], R[8]);
    const next = Math.min(Math.max(t + dy * ROTATE_SPEED, 1e-3), Math.PI - 1e-3);
    R = mul(rotX(t - next), R);
  } else {
    // free: right turns the front right (about the screen's vertical axis), down tips it down (horizontal axis)
    R = mul(rotX(dy * ROTATE_SPEED), mul(rotY(dx * ROTATE_SPEED), R));
  }
  orthonormal();
}
function orthonormal() {
  // keep R orthonormal against round-off after many small turns (Gram-Schmidt on its rows)
  const r0 = R.slice(0, 3), r1 = R.slice(3, 6);
  const n0 = Math.hypot(...r0); r0.forEach((v, i) => r0[i] = v / n0);
  const d = r0[0] * r1[0] + r0[1] * r1[1] + r0[2] * r1[2]; r1.forEach((v, i) => r1[i] = v - d * r0[i]);
  const n1 = Math.hypot(...r1); r1.forEach((v, i) => r1[i] = v / n1);
  const r2 = [r0[1] * r1[2] - r0[2] * r1[1], r0[2] * r1[0] - r0[0] * r1[2], r0[0] * r1[1] - r0[1] * r1[0]];
  R = [...r0, ...r1, ...r2];
}
function zOf(m, k) {          // a layer's drawn height: its order from the top times the spacing
  if (k < 0) return 0;
  const zs = m.z_mm, top = Math.max(...zs), bot = Math.min(...zs), span = Math.max(top - bot, 1e-6);
  return (zs[k] - (top + bot) / 2) / span * spread * (m.layers.length - 1 || 1) * 0.5;
}
function scale(m) { return Math.min(cv.width, cv.height) / m.size * 0.85 * zoom; }
function projector(m) {
  const W = cv.width, H = cv.height, s = scale(m);
  return (x, y, z) => {
    // board y points down: flip so the view matches KiCad's; then about the pivot
    const X = x - m.cx - P[0], Y = -(y - m.cy) - P[1], Z = z - P[2];
    return [W / 2 + (R[0] * X + R[1] * Y + R[2] * Z) * s,
            H / 2 - (R[3] * X + R[4] * Y + R[5] * Z) * s];
  };
}
function depth(z) { return R[8] * z; }          // a layer's distance towards the viewer (painter's order)
function draw() {
  const dpr = window.devicePixelRatio || 1, r = cv.getBoundingClientRect();
  if (cv.width !== Math.round(r.width * dpr) || cv.height !== Math.round(r.height * dpr)) {
    cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr); }
  ctx.clearRect(0, 0, cv.width, cv.height);
  drawGizmo();                                  // the view cube turns with the model
  placePivot();                                 // the pivot marker at the canvas centre (a resize moves it)
  const m = D.models[model]; if (!m) return;
  const P = projector(m), dark = matchMedia("(prefers-color-scheme: dark)").matches;
  ctx.lineWidth = dpr;
  // the layer farthest from the viewer first, so the nearer one draws over it (from below: the bottom layer last)
  const order = m.layers.map((_, k) => k).sort((a, b) => depth(zOf(m, a)) - depth(zOf(m, b)));
  for (const k of order) {
    if (!layerOn[k]) continue;
    const me = m.meshes[k], z = zOf(m, k);
    if ($("showOutline").checked) {
      ctx.strokeStyle = dark ? "rgba(220,225,235,.45)" : "rgba(40,50,65,.45)";
      for (const ring of D.outline) { ctx.beginPath();
        ring.forEach((p, i) => { const q = P(p[0], p[1], z); i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]); });
        ctx.stroke(); }
    }
    if ($("showStrips").checked) {
      for (const [list, di, dj] of [[me.xs, 1, 0], [me.ys, 0, 1]]) {
        const byNet = {};
        for (let t = 0; t < list.length; t += 3) (byNet[list[t + 2]] ||= []).push(list[t], list[t + 1]);
        for (const n in byNet) {
          const net = me.nets[n] ?? ""; if (!netOn[net]) continue;
          ctx.strokeStyle = netColour[net]; ctx.globalAlpha = 0.75; ctx.beginPath();
          const a = byNet[n];
          for (let t = 0; t < a.length; t += 2) {
            const x = me.x0 + a[t] * me.d, y = me.y0 + a[t + 1] * me.d;
            const p = P(x, y, z), q = P(x + di * me.d, y + dj * me.d, z);
            ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]);
          }
          ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
    }
    if ($("showNodes").checked) {
      for (let t = 0; t < me.nodes.length; t += 3) {
        const net = me.nets[me.nodes[t + 2]] ?? ""; if (!netOn[net]) continue;
        const p = P(me.x0 + me.nodes[t] * me.d, me.y0 + me.nodes[t + 1] * me.d, z);
        ctx.fillStyle = netColour[net]; ctx.fillRect(p[0] - dpr, p[1] - dpr, 2 * dpr, 2 * dpr);
      }
    }
  }
  if ($("showLinks").checked) {
    ctx.lineWidth = 1.2 * dpr; ctx.globalAlpha = 0.55;
    for (const [kind, , xa, ya, la, xb, yb, lb] of m.links) {
      if ((la >= 0 && !layerOn[la]) || (lb >= 0 && !layerOn[lb])) continue;
      const p = P(xa, ya, zOf(m, la)), q = P(xb, yb, zOf(m, lb));
      ctx.strokeStyle = kind === "via" ? (dark ? "#f2f2f2" : "#222") : "#e05d5d";
      ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
  if ($("showRoute").checked) {
    ctx.lineWidth = 3 * dpr; ctx.strokeStyle = "#ff3b8d";
    for (const ri of m.routes) for (const [x0, y0, x1, y1, layer] of D.routes[ri].segments) {
      const k = m.layers.indexOf(layer);
      const z = k >= 0 ? zOf(m, k) : zOf(m, 0);
      const p = P(x0, y0, z), q = P(x1, y1, z);
      ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
    }
  }
}
// Pointer handling after the 3D model viewer's controls: left drag rotates; right / middle drag or Shift / Ctrl
// drag pans; two fingers pinch-zoom and pan; the wheel zooms towards the cursor; a flick keeps turning and slows
// down (inertia); a double-click resets the view.
const pointers = new Map();
let mode = null, pinch = null, lastMove = null, vel = [0, 0], frame = 0;
function zoomAt(f, clientX, clientY) {
  // keep the point under the cursor where it is: the cursor's offset c from the centre shows the model at c / s;
  // after scaling s by f that point must still be under it, so the pivot moves by c / s - c / (s f) (in screen
  // directions, turned into model space by shift)
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const c = [(clientX - r.left) * dpr - cv.width / 2, (clientY - r.top) * dpr - cv.height / 2];
  shift(-c[0] * (1 - 1 / f), -c[1] * (1 - 1 / f)); zoom *= f;
}
function pinchState() {
  const [a, b] = [...pointers.values()];
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, dist: Math.hypot(a.x - b.x, a.y - b.y) };
}
let lastTime = 0;
function animate(now) {
  // one frame of the motion nobody is dragging: the inertia of the last flick (decaying until negligible) and the
  // orbit (a steady spin about the board's normal, paused while a pointer is down); runs while either moves
  frame = 0;
  const dt = lastTime ? Math.min(now - lastTime, 100) : 16;
  lastTime = now;
  let active = false;
  if (!pointers.size && vel[0] * vel[0] + vel[1] * vel[1] >= 0.01) {
    const k = dt / 16.7;
    turn(vel[0] * k, vel[1] * k); vel = [vel[0] * Math.pow(0.9, k), vel[1] * Math.pow(0.9, k)];
    active = true;
  }
  if ($("orbitOn").checked && !pointers.size) {
    spin(+$("orbitSpeed").value * Math.PI / 180 * dt / 1000);
    active = true;
  }
  if (active) { draw(); frame = requestAnimationFrame(animate); }
  else lastTime = 0;
}
function run() { if (!frame) { lastTime = 0; frame = requestAnimationFrame(animate); } }
cv.addEventListener("contextmenu", e => e.preventDefault());
cv.addEventListener("pointerdown", e => {
  if (e.pointerType === "mouse" && e.button > 2) return;
  cv.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  vel = [0, 0];
  if (pointers.size === 1)
    mode = e.button === 1 || e.button === 2 || e.shiftKey || e.ctrlKey || e.metaKey ? "pan" : "rotate";
  else if (pointers.size === 2) { mode = "pinch"; pinch = pinchState(); }
  lastMove = null; cv.style.cursor = "grabbing";
});
cv.addEventListener("pointermove", e => {
  const p = pointers.get(e.pointerId); if (!p) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y, dpr = window.devicePixelRatio || 1;
  p.x = e.clientX; p.y = e.clientY;
  if (mode === "rotate") { turn(dx, dy); lastMove = { dx, dy, t: performance.now() }; }
  else if (mode === "pan") shift(dx * dpr, dy * dpr);
  else if (mode === "pinch") {
    const s = pinchState();
    if (s.dist > 0 && pinch.dist > 0) zoomAt(s.dist / pinch.dist, s.x, s.y);
    shift((s.x - pinch.x) * dpr, (s.y - pinch.y) * dpr); pinch = s;
  }
  draw();
});
function pointerEnd(e) {
  if (!pointers.has(e.pointerId)) return;
  pointers.delete(e.pointerId);
  if (pointers.size === 1) mode = "rotate";
  else if (!pointers.size) {
    mode = null; cv.style.cursor = "grab";
    if (lastMove && performance.now() - lastMove.t < 50) vel = [lastMove.dx, lastMove.dy];
    run();                  // the flick's inertia, and the orbit carries on
  }
}
cv.addEventListener("pointerup", pointerEnd);
cv.addEventListener("pointercancel", pointerEnd);
cv.addEventListener("wheel", e => {
  e.preventDefault(); vel = [0, 0];
  let dy = e.deltaY;
  if (e.deltaMode === 1) dy *= 16; else if (e.deltaMode === 2) dy *= 100;
  // ctrlKey: a trackpad's pinch, small deltas
  zoomAt(Math.exp(-dy * (e.ctrlKey ? 0.01 : 0.0015)), e.clientX, e.clientY); draw();
}, { passive: false });
cv.addEventListener("dblclick", () => { resetView(); draw(); });
$("model").addEventListener("change", e => { model = +e.target.value; buildPanels(); draw(); });
$("spread").addEventListener("input", e => { spread = +e.target.value; draw(); });
$("orbitOn").addEventListener("change", run);
$("orbitSpeed").addEventListener("input", () => { $("orbitText").textContent = `${$("orbitSpeed").value}°/s`; });
// into turntable: the view levelled (the normal upright on screen), as from the start
$("dragMode").addEventListener("change", e => { if (e.target.value === "turntable") { R = INITIAL(); draw(); } });
["showStrips", "showNodes", "showLinks", "showRoute", "showOutline"].forEach(id => $(id).addEventListener("change", draw));
window.addEventListener("resize", draw);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
// ------------------------------------------------------------------ the view cube (as the 3D model viewer's)
// A cube in the corner turned with the model: a click on a face turns the view to look straight at it, a drag on
// the cube turns the model, a drag on the compass ring (N = the board's +y up the screen in KiCad, E = +x) spins
// it about its normal; home resets, the arrows roll the view 90° about the viewing axis.
const gc = $("cube"), gx = gc.getContext("2d");
// each face: its outward normal and its four corners in model coordinates (x right, y up, z = the board's normal),
// and the view that looks straight at it (R's rows: screen x, screen y, towards the viewer)
const FACES = [
  { name: "TOP", n: [0, 0, 1], R: [1, 0, 0, 0, 1, 0, 0, 0, 1] },
  { name: "BOTTOM", n: [0, 0, -1], R: [1, 0, 0, 0, -1, 0, 0, 0, -1] },
  { name: "FRONT", n: [0, -1, 0], R: [1, 0, 0, 0, 0, 1, 0, -1, 0] },
  { name: "BACK", n: [0, 1, 0], R: [-1, 0, 0, 0, 0, 1, 0, 1, 0] },
  { name: "RIGHT", n: [1, 0, 0], R: [0, 1, 0, 0, 0, 1, 1, 0, 0] },
  { name: "LEFT", n: [-1, 0, 0], R: [0, -1, 0, 0, 0, 1, -1, 0, 0] },
];
FACES.forEach(f => {
  // the face's corners: the normal plus ± the two other axes
  const [a, b] = [[1, 0, 0], [0, 1, 0], [0, 0, 1]].filter(e => Math.abs(e[0] * f.n[0] + e[1] * f.n[1] + e[2] * f.n[2]) < .5);
  f.corners = [[1, 1], [-1, 1], [-1, -1], [1, -1]].map(([s, t]) => f.n.map((v, i) => v + s * a[i] + t * b[i]));
});
let cubeHit = [];
function cubeXY(v, c, s) { return [c[0] + (R[0] * v[0] + R[1] * v[1] + R[2] * v[2]) * s,
                                   c[1] - (R[3] * v[0] + R[4] * v[1] + R[5] * v[2]) * s]; }
function drawGizmo() {
  const dpr = window.devicePixelRatio || 1;
  if (gc.width !== 170 * dpr) { gc.width = gc.height = 170 * dpr; }
  gx.setTransform(dpr, 0, 0, dpr, 0, 0); gx.clearRect(0, 0, 170, 170);
  const dark = matchMedia("(prefers-color-scheme: dark)").matches, c = [85, 80], s = 30;
  // the compass ring around the cube's foot: drawn behind, its letters at N/E/S/W
  gx.lineWidth = 9; gx.strokeStyle = dark ? "rgba(150,165,185,.35)" : "rgba(120,135,155,.3)"; gx.beginPath();
  for (let k = 0; k <= 64; k++) { const a = k / 64 * 2 * Math.PI, p = cubeXY([2 * Math.cos(a), 2 * Math.sin(a), -1], c, s);
    k ? gx.lineTo(p[0], p[1]) : gx.moveTo(p[0], p[1]); }
  gx.stroke();
  gx.font = "bold 10px system-ui, sans-serif"; gx.textAlign = "center"; gx.textBaseline = "middle";
  gx.fillStyle = dark ? "#9aa5b4" : "#5d6878";
  for (const [t, v] of [["N", [0, 2, -1]], ["E", [2, 0, -1]], ["S", [0, -2, -1]], ["W", [-2, 0, -1]]]) {
    const p = cubeXY(v, c, s); gx.fillText(t, p[0], p[1]); }
  // the faces turned towards the viewer, farthest first
  const vis = FACES.map(f => ({ f, z: R[6] * f.n[0] + R[7] * f.n[1] + R[8] * f.n[2] })).filter(o => o.z > 1e-3)
    .sort((a, b) => a.z - b.z);
  cubeHit = [];
  for (const { f, z } of vis) {
    const pts = f.corners.map(v => cubeXY(v, c, s));
    gx.beginPath(); pts.forEach((p, i) => i ? gx.lineTo(p[0], p[1]) : gx.moveTo(p[0], p[1])); gx.closePath();
    const shade = Math.round((dark ? 60 : 200) + z * (dark ? 40 : 45));
    gx.fillStyle = f === hoverFace ? (dark ? "#3f5f8f" : "#cfe0fb") : `rgb(${shade},${shade},${shade + 6})`;
    gx.fill(); gx.lineWidth = 1; gx.strokeStyle = dark ? "#8090a4" : "#7d8794"; gx.stroke();
    if (z > 0.25) {     // the label, faded as the face turns away
      const m = cubeXY(f.n, c, s); gx.globalAlpha = Math.min(1, (z - 0.25) * 2);
      gx.fillStyle = dark ? "#e4e8ee" : "#2a3340"; gx.font = "600 9px system-ui, sans-serif";
      gx.fillText(f.name, m[0], m[1]); gx.globalAlpha = 1;
    }
    cubeHit.push({ f, pts });
  }
}
function inPoly(p, pts) {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > p[1]) !== (yj > p[1]) && p[0] < (xj - xi) * (p[1] - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
function faceAt(p) { for (let k = cubeHit.length - 1; k >= 0; k--) if (inPoly(p, cubeHit[k].pts)) return cubeHit[k].f; return null; }
// a smooth turn to a target orientation: the rotation between them as a quaternion, slerped over ~0.45 s
function toQuat(M) {
  const [m00, m01, m02, m10, m11, m12, m20, m21, m22] = M, tr = m00 + m11 + m22;
  let q;
  if (tr > 0) { const s = Math.sqrt(tr + 1) * 2; q = [0.25 * s, (m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s]; }
  else if (m00 > m11 && m00 > m22) { const s = Math.sqrt(1 + m00 - m11 - m22) * 2;
    q = [(m21 - m12) / s, 0.25 * s, (m01 + m10) / s, (m02 + m20) / s]; }
  else if (m11 > m22) { const s = Math.sqrt(1 + m11 - m00 - m22) * 2;
    q = [(m02 - m20) / s, (m01 + m10) / s, 0.25 * s, (m12 + m21) / s]; }
  else { const s = Math.sqrt(1 + m22 - m00 - m11) * 2; q = [(m10 - m01) / s, (m02 + m20) / s, (m12 + m21) / s, 0.25 * s]; }
  return q;
}
function fromQuat([w, x, y, z]) {
  return [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w),
          2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
          2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)];
}
function slerp(a, b, t) {
  let d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
  if (d < 0) { b = b.map(v => -v); d = -d; }        // the short way round
  if (d > 0.9995) { const q = a.map((v, i) => v + t * (b[i] - v)), n = Math.hypot(...q); return q.map(v => v / n); }
  const th = Math.acos(d), s = Math.sin(th);
  return a.map((v, i) => (Math.sin((1 - t) * th) * v + Math.sin(t * th) * b[i]) / s);
}
let tween = null;
function turnTo(target) {
  vel = [0, 0]; $("orbitOn").checked = false;       // looking at a face: nothing else turns it away
  tween = { q0: toQuat(R), q1: toQuat(target), t0: performance.now(), ms: 450 };
  stepTween();
}
function stepTween() {
  if (!tween) return;
  const t = Math.min((performance.now() - tween.t0) / tween.ms, 1), e = t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  R = fromQuat(slerp(tween.q0, tween.q1, e)); orthonormal(); showPivot(); draw();
  if (t < 1) requestAnimationFrame(stepTween); else tween = null;
}
let gdrag = null, hoverFace = null;
function gpos(e) { const r = gc.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
gc.addEventListener("pointerdown", e => {
  gc.setPointerCapture(e.pointerId); tween = null; vel = [0, 0];
  const p = gpos(e);
  gdrag = { x: e.clientX, y: e.clientY, moved: 0, face: faceAt(p), ring: !faceAt(p) };
});
gc.addEventListener("pointermove", e => {
  if (!gdrag) { const f = faceAt(gpos(e)); if (f !== hoverFace) { hoverFace = f; drawGizmo(); } return; }
  const dx = e.clientX - gdrag.x, dy = e.clientY - gdrag.y;
  gdrag.x = e.clientX; gdrag.y = e.clientY; gdrag.moved += Math.abs(dx) + Math.abs(dy);
  if (gdrag.moved < 4) return;
  if (gdrag.ring) { spin(dx * ROTATE_SPEED); showPivot(); } else turn(dx, dy);
  draw();
});
gc.addEventListener("pointerup", () => {
  if (gdrag && gdrag.moved < 4 && gdrag.face) turnTo(gdrag.face.R);
  gdrag = null;
});
gc.addEventListener("pointerleave", () => { if (hoverFace) { hoverFace = null; drawGizmo(); } });
gc.addEventListener("dblclick", e => e.stopPropagation());
$("home").addEventListener("click", () => { P = [0, 0, 0]; zoom = 1; turnTo(INITIAL()); });
// roll: about the viewing axis (screen z), 90° each way
$("rollL").addEventListener("click", () => turnTo(mul(rotZ(Math.PI / 2), R)));
$("rollR").addEventListener("click", () => turnTo(mul(rotZ(-Math.PI / 2), R)));

buildPanels(); draw(); run();
</script></body></html>
"""


def page(data: dict) -> str:
    """The HTML page with the data embedded (JSON with "<" escaped, so no board name can close the script)."""
    blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    return (PAGE.replace("__CSP__", html.escape(CSP)).replace("__PALETTE__", json.dumps(PALETTE))
            .replace("__DATA__", blob))


def main(argv=None) -> int:
    """Parse the arguments, simulate, write the page."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("board", type=Path)
    ap.add_argument("settings", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--nets", help="comma-separated signal paths (default: every enabled one)")
    ap.add_argument("--resolution", type=float, help="the analysis resolution (default: the settings')")
    a = ap.parse_args(argv)
    data = collect(a.board, a.settings, a.nets.split(",") if a.nets else None, a.resolution)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(page(data), encoding="utf-8")
    for m in data["models"]:
        print(f"{m['title']}: pitch {m['pitch']:.3f} mm, {m['nodes']} nodes, layers {', '.join(m['layers'])}")
    for e in data["errors"]:
        print("error:", e)
    print(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
