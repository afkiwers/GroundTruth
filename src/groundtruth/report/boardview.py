"""The report's interactive board view, in the manner of InteractiveHtmlBom: the board drawn on a canvas (front or
back, mirrored like iBOM; one copper layer's pours and tracks, the vias, the parts' pads and outlines, their
references), a searchable parts table that highlights a part on the board when the mouse is over its row (and a
click on a part selects its row), and GroundTruth's own layers: the analysed routes and power stages, and the issues,
numbered as in the report's Issues table, each a marker to click. Pan by dragging, zoom with the wheel, double-click
to see the whole board.

Everything comes from the board model (no KiCad, no pcbnew: the CLI's reports have it too), as compact JSON in the
page: coordinates in mm rounded to 0.01, pours simplified to 0.02 mm."""
from __future__ import annotations

import html
import json
import re
from typing import Optional

from ..core.board import Board
from ..core.issues import Issue

ZONE_SIMPLIFY_MM = 0.02
PAD_SIMPLIFY_MM = 0.005
SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def _flat(points) -> list[float]:
    """Points as one flat list x0, y0, x1, y1 ... rounded to 10 µm (compact JSON for the viewer)."""
    return [round(float(v), 2) for p in points for v in p[:2]]


def _rings(geom, tol: float) -> list[list[list[float]]]:
    """A shapely (multi)polygon as [[outer, hole, ...], ...] flat rings."""
    if geom is None or geom.is_empty:
        return []
    g = geom.simplify(tol, preserve_topology=True) if tol else geom
    polys = list(getattr(g, "geoms", [g]))
    return [[_flat(p.exterior.coords)] + [_flat(h.coords) for h in p.interiors] for p in polys
            if p.geom_type == "Polygon" and not p.is_empty]


def ordered_issues(issues: list[Issue]) -> list[Issue]:
    """The issues in the report's Issues-table order (its numbers): live first, by severity."""
    return sorted(issues, key=lambda i: (i.waived, SEVERITY_RANK.get(i.severity, 3)))


def board_data(board: Board, views: list, issues: list[Issue], config=None) -> dict:
    """The board, its parts and the analysis results as the viewer's data. views: route, power-stage and bridge
    results (their routes and parts are drawn); config: the settings (each part's model in the table)."""
    from ..geometry.shapes import arc_points, pad_shape, to_shapely
    layers = list(board.copper_layers)
    nets: dict[str, int] = {}

    def net_id(name: str) -> int:
        """A net's number in the viewer's net table (numbered as first seen; "" for no net)."""
        return nets.setdefault(name or "", len(nets))

    xs, ys = [], []
    edges = []
    for poly in board.outline:
        ring = _flat(poly[0])
        edges.append(ring)
        xs += ring[0::2]
        ys += ring[1::2]
    tracks: dict[str, list] = {}
    for t in board.tracks:
        pts = arc_points(t.start, t.mid, t.end) if t.mid is not None else [t.start, t.end]
        out = tracks.setdefault(t.layer, [])
        n = net_id(t.net)
        for a, b in zip(pts, pts[1:]):
            out.append([round(a[0], 2), round(a[1], 2), round(b[0], 2), round(b[1], 2), round(t.width, 3), n])
    vias = [[round(v.position[0], 2), round(v.position[1], 2), round(v.size, 3), net_id(v.net)] for v in board.vias]
    zones: dict[str, list] = {}
    for z in board.zones:
        if z.is_rule_area:
            continue
        for layer, polys in z.filled.items():
            for p in polys:
                try:
                    rings = _rings(to_shapely(p), ZONE_SIMPLIFY_MM)
                except Exception:              # noqa: BLE001 - a degenerate fill: leave it out of the picture
                    continue
                zones.setdefault(layer, []).extend([net_id(z.net), r] for r in rings)
    parts = []
    for fp in sorted(board.footprints, key=lambda f: _natural(f.reference)):
        pads = []
        for p in fp.pads:
            front = any(l == layers[0] or l.startswith("F.") for l in p.layers) if layers else True
            back = any(l == layers[-1] or l.startswith("B.") for l in p.layers) if layers else False
            if p.pad_type == "thru_hole" or "*.Cu" in p.layers:
                front = back = True
            try:
                rings = _rings(pad_shape(p), PAD_SIMPLIFY_MM)
            except Exception:                  # noqa: BLE001
                continue
            if rings:
                pads.append([(1 if front else 0) | (2 if back else 0), rings[0][0], net_id(p.net), str(p.number)])
        outline = [_flat(pl) for pl in fp.outline]
        pts = [(x, y) for pl in fp.outline for x, y in pl] or \
              [(p.position[0] + s * p.size[0] / 2, p.position[1] + t * p.size[1] / 2) for p in fp.pads
               for s in (-1, 1) for t in (-1, 1)] or [fp.position]
        bb = [round(min(x for x, _ in pts), 2), round(min(y for _, y in pts), 2),
              round(max(x for x, _ in pts), 2), round(max(y for _, y in pts), 2)]
        model = config.model_for(fp.reference) if config is not None else None
        parts.append({"ref": fp.reference, "val": fp.value, "fp": fp.footprint_name,
                      "side": "B" if fp.layer.startswith("B.") else "F", "bb": bb, "ol": outline, "pads": pads,
                      "model": _model_text(model), "dnp": fp.dnp})
        xs += [bb[0], bb[2]]
        ys += [bb[1], bb[3]]
    routes = []
    for rr in views:
        if getattr(rr, "kind", "route") == "thermal":
            continue
        segs = [[layer, _flat(pts)] for layer, pts in (getattr(rr, "route_xy", None) or []) if len(pts) >= 2]
        refs = list(getattr(rr, "parts", None) or [])
        if rr.kind == "route":
            refs = [r.split(".")[0] for r in (rr.driver, rr.receiver) if r]
        routes.append({"name": rr.net, "kind": rr.kind, "segs": segs, "parts": refs})
    parts_of = {r["name"]: r["parts"] for r in routes}
    refs = {p["ref"] for p in parts}
    out_issues = []
    for k, i in enumerate(ordered_issues(issues), 1):
        own = [i.net] if i.net in refs else parts_of.get(i.net, [])
        out_issues.append({"n": k, "sev": i.severity, "check": i.check, "net": i.net, "msg": i.message,
                           "x": round(i.location[0], 2), "y": round(i.location[1], 2), "layer": i.layer,
                           "waived": i.waived, "parts": own})
        xs.append(i.location[0])
        ys.append(i.location[1])
    bbox = [min(xs), min(ys), max(xs), max(ys)] if xs else [0, 0, 100, 100]
    return {"bbox": [round(v, 2) for v in bbox], "layers": layers, "nets": list(nets), "edges": edges,
            "tracks": tracks, "vias": vias, "zones": zones, "parts": parts, "routes": routes, "issues": out_issues}


def _natural(ref: str) -> list:
    """C2 before C10, as iBOM sorts."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


def _model_text(model) -> str:
    """A part model for the parts table: its kind and its values with units ("cap (C 100nF, ESR 20mΩ)")."""
    if model is None:
        return ""
    from ..components.values import format_si
    units = {"R": "Ω", "C": "F", "L": "H", "ESR": "Ω", "ESL": "H", "C_bias": "F"}
    vals = ", ".join(f"{k} {format_si(v, units.get(k, ''))}" for k, v in model.params.items() if v is not None)
    return model.kind + (f" ({vals})" if vals else "")


def board_view(board: Board, views: list, issues: list[Issue], config=None) -> str:
    """The board view's HTML (its data, canvas, controls and parts table); its style and script are BOARD_CSS and
    BOARD_JS, which the page includes once."""
    data = json.dumps(board_data(board, views, issues, config), separators=(",", ":"))
    data = data.replace("</", "<\\/")               # never ends the script element early
    options = "".join(f"<option>{html.escape(l)}</option>" for l in board.copper_layers)
    return f"""<div class="bv" id="bv">
<script type="application/json" id="bv-data">{data}</script>
<div class="bv-bar noprint">
  <span class="bv-group"><button class="f on" data-side="F">Front</button><button class="f" data-side="B">Back</button></span>
  <label>Copper <select id="bv-layer">{options}</select></label>
  <span class="bv-group" id="bv-show">
    <label><input type="checkbox" data-k="pours" checked> Pours</label>
    <label><input type="checkbox" data-k="tracks" checked> Tracks</label>
    <label><input type="checkbox" data-k="pads" checked> Pads</label>
    <label><input type="checkbox" data-k="outlines" checked> Outlines</label>
    <label><input type="checkbox" data-k="names" checked> Names</label>
    <label><input type="checkbox" data-k="analysed" checked> Analysed</label>
    <label><input type="checkbox" data-k="issues" checked> Issues</label>
  </span>
  <span class="bv-hint">drag to pan · wheel to zoom · double-click: whole board</span>
</div>
<div class="bv-main">
  <div class="bv-canvas-wrap"><canvas id="bv-canvas"></canvas><div id="bv-tip" class="bv-tip"></div></div>
  <div class="bv-side">
    <div class="bv-tabs noprint"><button class="f on" data-tab="parts">Parts</button><button class="f" data-tab="issues">Issues</button></div>
    <input id="bv-filter" class="noprint" type="search" placeholder="Filter: reference, value, footprint, net">
    <div class="bv-list" id="bv-list"></div>
  </div>
</div>
<div class="bv-info" id="bv-info">Move over a part in the list or on the board; click an issue's marker for its message.</div>
</div>"""


BOARD_CSS = """
.bv { background:var(--paper); border:1px solid var(--line); border-radius:var(--radius); box-shadow:var(--shadow);
      padding:12px 14px; margin:12px 0; }
.bv-bar { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; font-size:12.5px; color:var(--ink2);
          margin-bottom:8px; }
.bv-bar label { white-space:nowrap; } .bv-bar select { font:inherit; }
.bv-group { display:inline-flex; gap:6px; flex-wrap:wrap; }
.bv-hint { color:var(--muted); margin-left:auto; }
.bv-main { display:grid; grid-template-columns:minmax(0,1fr) 400px; gap:12px; }
.bv-canvas-wrap { position:relative; height:70vh; min-height:380px; border:1px solid var(--line); border-radius:8px;
                  overflow:hidden; background:var(--page); }
#bv-canvas { width:100%; height:100%; display:block; cursor:grab; }
.bv-tip { position:absolute; pointer-events:none; display:none; max-width:420px; padding:6px 9px; font-size:12px;
          background:var(--paper); color:var(--ink); border:1px solid var(--line); border-radius:6px;
          box-shadow:var(--shadow); }
.bv-side { display:flex; flex-direction:column; min-height:0; height:70vh; min-height:380px; }
.bv-tabs { display:flex; gap:6px; margin-bottom:6px; }
#bv-filter { font:inherit; font-size:12.5px; padding:5px 8px; border:1px solid var(--line); border-radius:6px;
             background:var(--paper); color:var(--ink); margin-bottom:6px; }
.bv-list { flex:1; overflow:auto; border:1px solid var(--line); border-radius:8px; }
.bv-list table { font-size:12px; table-layout:fixed; } .bv-list td { padding:4px 7px; cursor:pointer;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bv-list th:first-child { width:64px; } .bv-list th:last-child { width:44px; }
.bv-list tr.on td { background:var(--accent2); font-weight:600; }
.bv-info { font-size:12.5px; color:var(--ink2); margin-top:8px; min-height:1.5em; }
@media (max-width:1100px) { .bv-main { grid-template-columns:1fr; } .bv-side { height:40vh; } }
@media print { .bv-canvas-wrap { height:120mm; } .bv-side { display:none; } .bv-main { display:block; } }
"""

BOARD_JS = r"""
(() => {
  const el = document.getElementById('bv-data');
  if (!el) return;
  const D = JSON.parse(el.textContent);
  const cv = document.getElementById('bv-canvas'), ctx = cv.getContext('2d');
  const tip = document.getElementById('bv-tip'), info = document.getElementById('bv-info');
  const list = document.getElementById('bv-list'), filter = document.getElementById('bv-filter');
  const layerSel = document.getElementById('bv-layer');
  const show = {pours: true, tracks: true, pads: true, outlines: true, names: true, analysed: true, issues: true};
  let side = 'F', tab = 'parts', hot = new Set(), picked = null, pickedIssue = null;
  let s = 1, ox = 0, oy = 0, w = 0, h = 0, dpr = 1;
  const bb = D.bbox, partByRef = Object.fromEntries(D.parts.map(p => [p.ref, p]));
  const css = (n, f) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f;
  const C = () => ({ink: css('--ink', '#141a23'), muted: css('--muted', '#6b7584'), line: css('--line', '#e3e7ec'),
    page: css('--page', '#f4f6f8'), accent: css('--accent', '#0d5ea6'), err: css('--err', '#b42318'),
    warn: css('--warn', '#a86200'), info: css('--info', '#2b5fb4'), dark: matchMedia('(prefers-color-scheme: dark)').matches});
  const mx = x => side === 'B' ? bb[0] + bb[2] - x : x;            // the back is seen mirrored, as in iBOM
  const X = x => mx(x) * s + ox, Y = y => y * s + oy;
  const toBoard = (px, py) => { const x = (px - ox) / s; return [side === 'B' ? bb[0] + bb[2] - x : x, (py - oy) / s]; };

  function resize() {
    const r = cv.getBoundingClientRect(); dpr = window.devicePixelRatio || 1;
    w = r.width; h = r.height; cv.width = Math.max(1, w * dpr); cv.height = Math.max(1, h * dpr);
  }
  function fit() {
    resize();
    const bw = Math.max(bb[2] - bb[0], 1), bh = Math.max(bb[3] - bb[1], 1);
    s = 0.94 * Math.min(w / bw, h / bh);
    ox = (w - bw * s) / 2 - bb[0] * s; oy = (h - bh * s) / 2 - bb[1] * s;
    draw();
  }
  function ring(r) { ctx.moveTo(X(r[0]), Y(r[1])); for (let k = 2; k < r.length; k += 2) ctx.lineTo(X(r[k]), Y(r[k + 1])); ctx.closePath(); }
  function polyline(r) { ctx.moveTo(X(r[0]), Y(r[1])); for (let k = 2; k < r.length; k += 2) ctx.lineTo(X(r[k]), Y(r[k + 1])); }
  const sevColour = (c, sev) => sev === 'error' ? c.err : sev === 'warning' ? c.warn : c.info;

  function draw() {
    const c = C(), layer = layerSel.value, bit = side === 'F' ? 1 : 2;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = c.page; ctx.fillRect(0, 0, w, h);
    // board
    ctx.beginPath(); D.edges.forEach(ring);
    ctx.fillStyle = c.dark ? '#1d2a22' : '#e9efe6'; ctx.fill('evenodd');
    ctx.strokeStyle = c.dark ? '#c9b458' : '#8a7a1c'; ctx.lineWidth = 1.5; ctx.stroke();
    const copper = layer.startsWith('B.') ? (c.dark ? '#4f7fc4' : '#3a6db5') : (c.dark ? '#c4704f' : '#c45a33');
    if (show.pours) {
      ctx.beginPath(); (D.zones[layer] || []).forEach(([net, rings]) => rings.forEach(ring));
      ctx.globalAlpha = 0.28; ctx.fillStyle = copper; ctx.fill('evenodd'); ctx.globalAlpha = 1;
    }
    if (show.tracks) {
      ctx.strokeStyle = copper; ctx.lineCap = 'round'; ctx.globalAlpha = 0.85;
      for (const t of D.tracks[layer] || []) {
        ctx.lineWidth = Math.max(t[4] * s, 0.6); ctx.beginPath(); ctx.moveTo(X(t[0]), Y(t[1])); ctx.lineTo(X(t[2]), Y(t[3])); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    ctx.fillStyle = c.dark ? '#9aa4b1' : '#7d8794';
    for (const v of D.vias) { ctx.beginPath(); ctx.arc(X(v[0]), Y(v[1]), Math.max(v[2] * s / 2, 1), 0, 2 * Math.PI); ctx.fill(); }
    const hl = new Set(hot); if (picked) hl.add(picked);
    if (pickedIssue) pickedIssue.parts.forEach(r => hl.add(r));
    for (const p of D.parts) {
      const mine = p.side === side, lit = hl.has(p.ref);
      if (show.pads) {
        ctx.beginPath();
        for (const pad of p.pads) if (pad[0] & bit) ring(pad[1]);
        ctx.fillStyle = lit ? (c.dark ? '#ff5b5b' : '#e0302a') : (c.dark ? '#d7b65a' : '#b8962e');
        ctx.globalAlpha = mine || lit ? 1 : 0.45; ctx.fill(); ctx.globalAlpha = 1;
      }
      if (show.outlines && (mine || lit)) {
        ctx.beginPath(); p.ol.forEach(polyline);
        if (!p.ol.length) { ctx.rect(X(Math.min(p.bb[0], p.bb[2])), Y(p.bb[1]), (p.bb[2] - p.bb[0]) * s * (side === 'B' ? -1 : 1), (p.bb[3] - p.bb[1]) * s); }
        ctx.strokeStyle = lit ? (c.dark ? '#ff5b5b' : '#e0302a') : (c.dark ? '#cfd6df' : '#4c5664');
        ctx.lineWidth = lit ? 2 : 0.8; ctx.stroke();
      }
    }
    if (show.names) {
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      for (const p of D.parts) {
        if (p.side !== side && !hl.has(p.ref)) continue;
        const pw = Math.abs(p.bb[2] - p.bb[0]) * s, ph = (p.bb[3] - p.bb[1]) * s;
        const size = Math.min(Math.max(Math.min(pw / (p.ref.length * 0.62), ph * 0.7), 0), 18);
        if (size < 5 && !hl.has(p.ref)) continue;
        ctx.font = `600 ${Math.max(size, 9)}px "Segoe UI", system-ui, sans-serif`;
        ctx.fillStyle = hl.has(p.ref) ? (c.dark ? '#ffb4b4' : '#a3150f') : c.ink;
        ctx.fillText(p.ref, X((p.bb[0] + p.bb[2]) / 2), Y((p.bb[1] + p.bb[3]) / 2));
      }
    }
    if (show.analysed) {
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      for (const r of D.routes) for (const [l, pts] of r.segs) {
        ctx.beginPath(); polyline(pts); ctx.strokeStyle = c.accent; ctx.globalAlpha = l === layer ? 0.95 : 0.55;
        ctx.lineWidth = 3; ctx.setLineDash(l === layer ? [] : [6, 4]); ctx.stroke();
      }
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    }
    if (show.issues) {
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.font = '700 10px "Segoe UI", system-ui, sans-serif';
      for (const i of D.issues) {
        if (i.waived) continue;
        const x = X(i.x), y = Y(i.y), col = sevColour(c, i.sev), big = pickedIssue === i;
        ctx.beginPath(); ctx.arc(x, y, big ? 11 : 8, 0, 2 * Math.PI);
        ctx.fillStyle = c.dark ? '#12161c' : '#ffffff'; ctx.globalAlpha = 0.85; ctx.fill(); ctx.globalAlpha = 1;
        ctx.lineWidth = big ? 3 : 2; ctx.strokeStyle = col; ctx.stroke();
        ctx.fillStyle = col; ctx.fillText(String(i.n), x, y + 0.5);
      }
    }
  }

  // ---- the table
  function partRows(q) {
    const rows = D.parts.filter(p => !q || [p.ref, p.val, p.fp, p.model].join(' ').toLowerCase().includes(q));
    return '<table><thead><tr><th>Ref</th><th>Value</th><th>Footprint</th><th>Side</th></tr></thead><tbody>' +
      rows.map(p => `<tr data-ref="${esc(p.ref)}" title="${esc(p.model)}"><td>${esc(p.ref)}</td><td>${esc(p.val)}</td><td>${esc(p.fp)}</td><td>${p.side}</td></tr>`).join('') + '</tbody></table>';
  }
  function issueRows(q) {
    const rows = D.issues.filter(i => !q || [i.check, i.net, i.msg].join(' ').toLowerCase().includes(q));
    return '<table><thead><tr><th>#</th><th>Check</th><th>Net</th></tr></thead><tbody>' +
      rows.map(i => `<tr data-issue="${i.n}" title="${esc(i.msg)}"><td><span class="pill ${i.sev}">${i.n}</span></td><td>${esc(i.check)}</td><td>${esc(i.net)}</td></tr>`).join('') + '</tbody></table>';
  }
  const esc = t => String(t).replace(/[&<>"]/g, m => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[m]));
  function fill() { const q = filter.value.trim().toLowerCase(); list.innerHTML = tab === 'parts' ? partRows(q) : issueRows(q); mark(); }
  function mark() { list.querySelectorAll('tr[data-ref]').forEach(tr => tr.classList.toggle('on', tr.dataset.ref === picked));
                    list.querySelectorAll('tr[data-issue]').forEach(tr => tr.classList.toggle('on', pickedIssue && +tr.dataset.issue === pickedIssue.n)); }
  function zoomTo(x0, y0, x1, y1) {
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, span = Math.max(x1 - x0, y1 - y0, 4);
    s = Math.min(w, h) / (span * 2.2); ox = w / 2 - mx(cx) * s; oy = h / 2 - cy * s; draw();
  }
  function showPart(p) {
    info.textContent = `${p.ref} · ${p.val} · ${p.fp} · ${p.side === 'B' ? 'back' : 'front'}` + (p.model ? ` · model: ${p.model}` : '') + (p.dnp ? ' · DNP' : '');
  }
  function showIssue(i) {
    info.innerHTML = `<span class="pill ${i.sev}">${i.n} ${esc(i.sev)}</span> <b>${esc(i.check)}</b> · ${esc(i.msg)}`;
  }
  list.addEventListener('mouseover', ev => {
    const tr = ev.target.closest('tr[data-ref]');
    if (tr) { hot = new Set([tr.dataset.ref]); showPart(partByRef[tr.dataset.ref]); draw(); }
  });
  list.addEventListener('mouseleave', () => { hot = new Set(); draw(); });
  list.addEventListener('click', ev => {
    const tr = ev.target.closest('tr');
    if (!tr) return;
    if (tr.dataset.ref) {
      const p = partByRef[tr.dataset.ref]; picked = p.ref; pickedIssue = null;
      if (p.side !== side) setSide(p.side);
      zoomTo(...p.bb); showPart(p);
    } else if (tr.dataset.issue) {
      const i = D.issues[+tr.dataset.issue - 1]; pickedIssue = i; picked = null; zoomTo(i.x - 3, i.y - 3, i.x + 3, i.y + 3); showIssue(i);
    }
    mark();
  });
  filter.addEventListener('input', fill);
  document.querySelectorAll('.bv-tabs button').forEach(b => b.addEventListener('click', () => {
    tab = b.dataset.tab; document.querySelectorAll('.bv-tabs button').forEach(x => x.classList.toggle('on', x === b)); fill();
  }));
  function setSide(sd) {
    side = sd; document.querySelectorAll('.bv-bar [data-side]').forEach(x => x.classList.toggle('on', x.dataset.side === sd));
    const want = sd === 'F' ? D.layers[0] : D.layers[D.layers.length - 1];
    if (want) layerSel.value = want;
  }
  document.querySelectorAll('.bv-bar [data-side]').forEach(b => b.addEventListener('click', () => { setSide(b.dataset.side); fit(); }));
  layerSel.addEventListener('change', draw);
  document.querySelectorAll('#bv-show input').forEach(cb => cb.addEventListener('change', () => { show[cb.dataset.k] = cb.checked; draw(); }));

  // ---- the canvas: pan, zoom, hover, click
  let drag = null, moved = false;
  cv.addEventListener('mousedown', ev => { drag = [ev.offsetX, ev.offsetY, ox, oy]; moved = false; cv.style.cursor = 'grabbing'; });
  window.addEventListener('mouseup', () => { drag = null; cv.style.cursor = 'grab'; });
  cv.addEventListener('wheel', ev => {
    ev.preventDefault();
    const f = Math.exp(-ev.deltaY * 0.0015), px = ev.offsetX, py = ev.offsetY;
    ox = px - (px - ox) * f; oy = py - (py - oy) * f; s *= f; draw();
  }, {passive: false});
  cv.addEventListener('dblclick', fit);
  function partAt(x, y) {
    let best = null, area = Infinity;
    for (const p of D.parts) {
      if (p.side !== side) continue;
      const [a, b, c2, d] = p.bb;
      if (x >= a && x <= c2 && y >= b && y <= d && (c2 - a) * (d - b) < area) { best = p; area = (c2 - a) * (d - b); }
    }
    return best;
  }
  function issueAt(px, py) {
    if (!show.issues) return null;
    let best = null, dist = 12;
    for (const i of D.issues) { if (i.waived) continue; const d = Math.hypot(X(i.x) - px, Y(i.y) - py); if (d < dist) { best = i; dist = d; } }
    return best;
  }
  cv.addEventListener('mousemove', ev => {
    if (drag) { ox = drag[2] + ev.offsetX - drag[0]; oy = drag[3] + ev.offsetY - drag[1]; moved = true; draw(); return; }
    const i = issueAt(ev.offsetX, ev.offsetY), [x, y] = toBoard(ev.offsetX, ev.offsetY), p = i ? null : partAt(x, y);
    const text = i ? `${i.n} · ${i.sev} · ${i.check}: ${i.msg}` : p ? `${p.ref} · ${p.val} · ${p.fp}` : '';
    tip.style.display = text ? 'block' : 'none';
    if (text) { tip.textContent = text; tip.style.left = Math.min(ev.offsetX + 14, w - 300) + 'px'; tip.style.top = (ev.offsetY + 14) + 'px'; }
    const want = p ? new Set([p.ref]) : new Set();
    if ([...want].join() !== [...hot].join()) { hot = want; draw(); }
  });
  cv.addEventListener('mouseleave', () => { tip.style.display = 'none'; if (hot.size) { hot = new Set(); draw(); } });
  cv.addEventListener('click', ev => {
    if (moved) return;
    const i = issueAt(ev.offsetX, ev.offsetY);
    if (i) { pickedIssue = i; picked = null; showIssue(i); if (tab !== 'issues') document.querySelector('.bv-tabs [data-tab="issues"]').click(); else mark(); draw(); return; }
    const p = partAt(...toBoard(ev.offsetX, ev.offsetY));
    picked = p ? p.ref : null; pickedIssue = null;
    if (p) { showPart(p); if (tab !== 'parts') document.querySelector('.bv-tabs [data-tab="parts"]').click();
             const tr = list.querySelector(`tr[data-ref="${CSS.escape(p.ref)}"]`); if (tr) tr.scrollIntoView({block: 'nearest'}); }
    mark(); draw();
  });
  window.addEventListener('resize', () => { const k = [s, ox, oy]; resize(); [s, ox, oy] = k; draw(); });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', draw);
  window.addEventListener('beforeprint', fit);
  setSide('F'); fill(); fit();
})();
"""


def views_of(routes: list, converters: list, bridges: list) -> list:
    """The results the board view draws: routes and power stages (a bridge's legs carry its parts)."""
    return list(routes) + list(converters) + [b for b in bridges if getattr(b, "route_xy", None)]


def section(board: Board, routes: list, converters: list, bridges: list, issues: list[Issue],
            config=None) -> Optional[str]:
    """The report's Board section, or None for a board without parts."""
    if not board.footprints:
        return None
    return board_view(board, views_of(routes, converters, bridges), issues, config)
