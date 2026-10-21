"""The analysis report: one self-contained HTML page (images embedded, style and script inline, nothing loaded
from elsewhere) for sharing and for CI artifacts, from the CLI (`--format html`) and the plugin's Export report.

Cover and summary first (the board, the key figures, the findings per check, everything analysed at a glance), then the issues, the signal nets, the
power stages, the motor bridges, the thermal results and the suggested fixes, each with its figures, and an
appendix on method and limits. On screen a contents column follows the page; printed (or saved as PDF from the
browser) it is an A4 document with page breaks before the main sections."""
from __future__ import annotations

import base64
import html
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .formats import _si2, dc_resistance, share
from ..core.board import Board
from ..core.issues import Issue
from .boardview import BOARD_CSS, BOARD_JS, section as board_section
from .plots import draw_bridge_caps, draw_heatmap, draw_section, draw_spectrum, figure_png, heatmap_dpi

e = html.escape
DPI = 200               # the plots (spectra, sections, bars): sharp on a high-resolution screen and in print
MAP_DPI_MAX = 400       # the maps: as many pixels as their model has cells (plots.heatmap_dpi), at most this
WAIVED = ' <span class="pill muted">waived</span>'
SELECTED = ' <span class="pill ok">selected</span>'
NOT_REACHED = ' <span class="meh">not reached</span>'

_CSS = """
:root {
  --ink:#141a23; --ink2:#3d4756; --muted:#6b7584; --line:#e3e7ec; --line2:#eef1f4; --page:#f4f6f8; --paper:#fff;
  --accent:#0d5ea6; --accent2:#e8f1fa; --ok:#1a7f43; --ok-bg:#e7f5ec; --warn:#a86200; --warn-bg:#fdf3e1;
  --err:#b42318; --err-bg:#fdecea; --info:#2b5fb4; --info-bg:#eaf0fb;
  --radius:10px; --shadow:0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.07);
}
@media screen and (prefers-color-scheme: dark) {
  :root { --ink:#e7ebf0; --ink2:#c2c9d3; --muted:#8f99a8; --line:#2b323c; --line2:#232a33; --page:#12161c;
          --paper:#1a2028; --accent:#5aa9ee; --accent2:#18293b; --ok:#4cc27e; --ok-bg:#16291f; --warn:#f0a640;
          --warn-bg:#2d2416; --err:#f0716a; --err-bg:#311b1b; --info:#7aa7f0; --info-bg:#1a2333;
          --shadow:0 1px 2px rgba(0,0,0,.4); }
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; background:var(--page); color:var(--ink);
       font:14.5px/1.55 "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
       -webkit-font-smoothing:antialiased; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
code, .mono { font-family:"Cascadia Mono", Consolas, "SF Mono", Menlo, monospace; font-size:12.5px; }
.layout { display:grid; grid-template-columns:250px minmax(0,1fr); max-width:1400px; margin:0 auto; }
nav.toc { position:sticky; top:0; align-self:start; height:100vh; overflow:auto; padding:28px 16px 28px 24px;
          border-right:1px solid var(--line); font-size:13px; }
nav.toc .brand { font-weight:700; letter-spacing:.02em; color:var(--ink); margin-bottom:2px; }
nav.toc .sub { color:var(--muted); font-size:12px; margin-bottom:18px; word-break:break-word; }
nav.toc ol { list-style:none; margin:0; padding:0; } nav.toc li { margin:1px 0; }
nav.toc a { display:block; padding:4px 8px; border-radius:6px; color:var(--ink2); }
nav.toc a:hover { background:var(--accent2); text-decoration:none; }
nav.toc a.active { background:var(--accent2); color:var(--accent); font-weight:600; }
nav.toc ol ol a { padding-left:20px; font-size:12.5px; color:var(--muted); }
nav.toc .tools { margin-top:20px; display:flex; gap:6px; flex-wrap:wrap; }
main { padding:28px 40px 60px; min-width:0; }
.cover { background:var(--paper); border:1px solid var(--line); border-radius:var(--radius); box-shadow:var(--shadow);
         padding:30px 34px 26px; border-top:5px solid var(--accent); }
.cover .kicker { text-transform:uppercase; letter-spacing:.12em; font-size:11.5px; color:var(--accent);
                 font-weight:700; }
.cover h1 { font-size:28px; line-height:1.2; margin:6px 0 4px; font-weight:700; }
.cover .board { font-size:16px; color:var(--ink2); margin-bottom:18px; }
.meta { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:10px 24px; margin:0; }
.meta div { border-top:1px solid var(--line2); padding-top:6px; }
.meta dt { font-size:11.5px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
.meta dd { margin:1px 0 0; font-weight:550; word-break:break-word; }
section.part { margin-top:34px; }
h2 { font-size:21px; margin:0 0 4px; font-weight:700; letter-spacing:-.01em; }
h2 .num { color:var(--accent); margin-right:8px; }
.lead { color:var(--ink2); margin:0 0 16px; max-width:900px; }
h3 { font-size:17px; margin:0; font-weight:650; }
h4 { font-size:13px; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); margin:20px 0 8px; }
.item { background:var(--paper); border:1px solid var(--line); border-radius:var(--radius); box-shadow:var(--shadow);
        padding:20px 24px; margin:16px 0; }
.item > header { display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; border-bottom:1px solid var(--line2);
                 padding-bottom:10px; margin-bottom:14px; }
.item > header .what { color:var(--muted); font-size:13.5px; }
.kpis { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:10px; margin:6px 0 14px; }
.kpi { background:var(--paper); border:1px solid var(--line); border-radius:8px; padding:10px 12px 9px;
       border-left:4px solid var(--line); }
.kpi .k { font-size:11.5px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }
.kpi .v { font-size:20px; font-weight:700; line-height:1.25; margin-top:2px; font-variant-numeric:tabular-nums; }
.kpi .s { font-size:12px; color:var(--muted); }
.kpi.ok { border-left-color:var(--ok); } .kpi.warn { border-left-color:var(--warn); }
.kpi.error { border-left-color:var(--err); } .kpi.ok .v { color:var(--ok); } .kpi.warn .v { color:var(--warn); }
.kpi.error .v { color:var(--err); }
.summary-grid { display:grid; grid-template-columns:minmax(0,1.1fr) minmax(0,1fr); gap:16px; align-items:start; }
.summary-grid.one { grid-template-columns:1fr; }
.findings { list-style:none; margin:0; padding:0; }
.findings li { display:flex; gap:10px; align-items:flex-start; padding:8px 0; border-bottom:1px solid var(--line2);
               font-size:13.5px; }
.findings .pill { flex:none; margin-top:1px; }
.findings li:last-child { border-bottom:0; }
.findings.bycheck .pill { min-width:62px; text-align:center; }
.findings .fcheck code { font-size:11.5px; color:var(--muted); }
.findings .fworst { color:var(--ink2); font-size:12.5px; margin-top:2px; }
.tablewrap.narrow { max-width:620px; }
#issue-table td.finding { min-width:380px; }
#issue-table td.netc { min-width:120px; max-width:240px; }
#issue-table td.where { white-space:nowrap; }
#issue-table code.fp { font-size:11px; color:var(--muted); }
.pill { display:inline-block; padding:1px 9px; border-radius:999px; font-size:11.5px; font-weight:650;
        letter-spacing:.02em; white-space:nowrap; text-transform:capitalize; }
.pill.error { color:var(--err); background:var(--err-bg); } .pill.warning { color:var(--warn); background:var(--warn-bg); }
.pill.info { color:var(--info); background:var(--info-bg); } .pill.ok { color:var(--ok); background:var(--ok-bg); }
.pill.muted { color:var(--muted); background:var(--line2); }
.tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; margin:8px 0 14px; }
table { border-collapse:collapse; width:100%; font-size:13px; background:var(--paper); }
thead th { position:sticky; top:0; background:var(--line2); color:var(--ink2); text-align:left; font-weight:650;
           font-size:11.5px; text-transform:uppercase; letter-spacing:.05em; padding:8px 10px;
           border-bottom:1px solid var(--line); white-space:nowrap; }
th.sortable { cursor:pointer; user-select:none; } th.sortable:after { content:" ↕"; color:var(--muted); }
th[data-dir="asc"]:after { content:" ↑"; } th[data-dir="desc"]:after { content:" ↓"; }
td { padding:7px 10px; border-bottom:1px solid var(--line2); vertical-align:top; }
tbody tr:nth-child(even) td { background:color-mix(in srgb, var(--line2) 45%, transparent); }
tbody tr:hover td { background:var(--accent2); }
td.nw { white-space:nowrap; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
tr.waived td { opacity:.5; } tr.hidden, tr.pgout { display:none; }
.pager { display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin:-6px 0 14px; font-size:12.5px; }
.pager .pginfo { color:var(--muted); margin-right:8px; } .pager .pgnav { display:flex; gap:4px; margin-left:auto;
                                                                           align-items:center; }
.pager button.f.pg { min-width:30px; padding:3px 8px; } .pager button.f:disabled { opacity:.4; cursor:default; }
.bad { color:var(--err); font-weight:650; } .meh { color:var(--warn); font-weight:650; } .good { color:var(--ok); }
figure { margin:12px 0 18px; background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px; }
figure img { display:block; max-width:100%; height:auto; margin:0 auto; cursor:zoom-in; }
figure.full { overflow:auto; max-height:90vh; }
figure.full img { max-width:none; cursor:zoom-out; }
figcaption { font-size:12.5px; color:#5a6472; margin-top:6px; text-align:center; }
details.notes { margin:10px 0 0; font-size:13px; color:var(--ink2); }
details.notes summary { cursor:pointer; color:var(--muted); font-weight:600; }
details.notes ul { margin:6px 0 0; padding-left:20px; } details.notes li { margin:2px 0; }
.filters { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0 4px; }
button.f { font:inherit; font-size:12.5px; padding:4px 11px; border-radius:999px; border:1px solid var(--line);
           background:var(--paper); color:var(--ink2); cursor:pointer; }
button.f.on { background:var(--accent); border-color:var(--accent); color:#fff; }
button.f.dim { opacity:.45; }
.filters.checks { align-items:center; }
.filters .cn { opacity:.75; margin-left:2px; }
.empty { color:var(--muted); font-style:italic; }
.note { color:var(--muted); font-size:13px; }
.appendix p, .appendix li { max-width:900px; color:var(--ink2); }
footer { margin-top:40px; padding-top:14px; border-top:1px solid var(--line); color:var(--muted); font-size:12px;
         display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; }
@media (max-width:980px) { .layout { grid-template-columns:1fr; } nav.toc { display:none; } main { padding:18px; }
                           .summary-grid { grid-template-columns:1fr; } }
@page { size:A4; margin:15mm 14mm 16mm; }
@media print {
  body { background:#fff; font-size:10.5pt; } .layout { display:block; max-width:none; }
  nav.toc, .filters, .noprint { display:none !important; } main { padding:0; }
  .cover, .item, .kpi, .tablewrap, figure { box-shadow:none; break-inside:avoid; }
  .item { break-inside:auto; border-color:#ccd2da; } section.part { break-before:page; margin-top:0; }
  section.part.first { break-before:auto; } thead th { position:static; }
  tr, figure, .kpis { break-inside:avoid; } h2, h3, h4 { break-after:avoid; }
  * { -webkit-print-color-adjust:exact; print-color-adjust:exact; } a { color:inherit; }
  tr.hidden, tr.pgout { display:table-row; }
}
"""

_JS = """
(() => {
  // long tables (class 'paged'): a page of 25 rows at a time, or 50, 100 or all, with a pager under the table; the
  // rows the issues' filters hide are left out of the count; a sort or a filter goes back to the first page;
  // printing shows every row
  const pagers = new Map();
  const paginate = (t, page) => {
    const p = pagers.get(t);
    if (!p) return;
    if (page !== undefined) p.page = page;
    const all = [...t.tBodies[0].rows];
    const rows = all.filter(r => !r.classList.contains('hidden'));
    const size = p.size || Math.max(rows.length, 1);
    const pages = Math.max(1, Math.ceil(rows.length / size));
    p.page = Math.min(Math.max(p.page, 0), pages - 1);
    const from = p.page * size, to = Math.min(from + size, rows.length);
    all.forEach(r => r.classList.remove('pgout'));
    rows.forEach((r, k) => r.classList.toggle('pgout', k < from || k >= to));
    p.info.textContent = rows.length ? `Rows ${from + 1}–${to} of ${rows.length}` : 'No rows';
    p.sizes.forEach(b => b.classList.toggle('on', Number(b.dataset.size) === p.size));
    // the pages: the first, the last and two either side of this one, gaps as "…"
    const want = [...new Set([0, pages - 1, ...[-2, -1, 0, 1, 2].map(d => p.page + d)])]
      .filter(k => k >= 0 && k < pages).sort((a, b) => a - b);
    p.nav.replaceChildren();
    const btn = (label, k, on, off) => {
      const b = document.createElement('button');
      b.className = 'f pg' + (on ? ' on' : '');
      b.textContent = label;
      b.disabled = !!off;
      b.addEventListener('click', () => paginate(t, k));
      p.nav.appendChild(b);
    };
    if (pages > 1) {
      btn('‹', p.page - 1, false, p.page === 0);
      want.forEach((k, n) => {
        if (n && k - want[n - 1] > 1) p.nav.appendChild(Object.assign(document.createElement('span'), {textContent: '…'}));
        btn(String(k + 1), k, k === p.page);
      });
      btn('›', p.page + 1, false, p.page === pages - 1);
    }
  };
  document.querySelectorAll('table.paged').forEach(t => {
    const box = document.createElement('div');
    box.className = 'pager noprint';
    const info = document.createElement('span');
    info.className = 'pginfo';
    const show = Object.assign(document.createElement('span'), {className: 'note', textContent: 'Show'});
    const sizes = [25, 50, 100, 0].map(n => {
      const b = document.createElement('button');
      b.className = 'f';
      b.dataset.size = n;
      b.textContent = n ? String(n) : 'all';
      b.addEventListener('click', () => { pagers.get(t).size = n; paginate(t, 0); });
      return b;
    });
    const nav = Object.assign(document.createElement('span'), {className: 'pgnav'});
    box.append(info, show, ...sizes, nav);
    t.closest('.tablewrap').after(box);
    pagers.set(t, {size: 25, page: 0, info, sizes, nav});
    paginate(t, 0);
  });
  // sortable tables: a click on a column header sorts by it (numbers by their data-v value)
  document.querySelectorAll('table.sort').forEach(t => t.querySelectorAll('th').forEach((th, col) => {
    th.classList.add('sortable');
    th.addEventListener('click', () => {
      const tb = t.tBodies[0], rows = [...tb.rows];
      const dir = th.dataset.dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
      t.querySelectorAll('th').forEach(o => { if (o !== th) delete o.dataset.dir; });
      const val = r => r.cells[col] ? (r.cells[col].dataset.v ?? r.cells[col].innerText) : '';
      rows.sort((a, b) => { const x = val(a), y = val(b), n = parseFloat(x) - parseFloat(y);
        const c = isNaN(n) ? String(x).localeCompare(String(y)) : n; return dir === 'asc' ? c : -c; });
      rows.forEach(r => tb.appendChild(r));
      paginate(t, 0);
    });
  }));
  // the issues' filters: one severity (or all), and any number of checks (none picked: every check); a row shows
  // when it passes both. Each check chip counts its rows of the chosen severity (a chip with none is dimmed)
  const btns = [...document.querySelectorAll('button.f[data-sev]')];
  const chks = [...document.querySelectorAll('button.f[data-check]')];
  const allChk = document.querySelector('button.f[data-check-all]');
  const rows = [...document.querySelectorAll('#issue-table tbody tr')];
  let sev = 'all';
  const apply = () => {
    const picked = new Set(chks.filter(b => b.classList.contains('on')).map(b => b.dataset.check));
    let shown = 0;
    rows.forEach(r => {
      const hide = (sev !== 'all' && r.dataset.sev !== sev) || (picked.size > 0 && !picked.has(r.dataset.check));
      r.classList.toggle('hidden', hide);
      if (!hide) shown++;
    });
    chks.forEach(b => {
      const n = rows.filter(r => r.dataset.check === b.dataset.check && (sev === 'all' || r.dataset.sev === sev)).length;
      b.querySelector('.cn').textContent = n;
      b.classList.toggle('dim', n === 0);
    });
    if (allChk) allChk.classList.toggle('on', picked.size === 0);
    const out = document.getElementById('issues-shown');
    if (out) out.textContent = shown === rows.length ? '' : `${shown} of ${rows.length} shown`;
    const table = document.getElementById('issue-table');   // (the section around it is #issues)
    if (table) paginate(table, 0);
  };
  btns.forEach(b => b.addEventListener('click', () => {
    btns.forEach(o => o.classList.toggle('on', o === b));
    sev = b.dataset.sev;
    apply();
  }));
  chks.forEach(b => b.addEventListener('click', () => { b.classList.toggle('on'); apply(); }));
  if (allChk) allChk.addEventListener('click', () => { chks.forEach(b => b.classList.remove('on')); apply(); });
  // a check's count in the summary's main findings: the Issues table with that check only, every severity
  document.querySelectorAll('a[data-show-check]').forEach(a => a.addEventListener('click', () => {
    chks.forEach(b => b.classList.toggle('on', b.dataset.check === a.dataset.showCheck));
    btns.forEach(o => o.classList.toggle('on', o.dataset.sev === 'all'));
    sev = 'all';
    apply();
  }));
  // a click on a figure shows it at its full resolution (scroll inside it), another click fits it again
  document.querySelectorAll('figure img').forEach(img => img.addEventListener('click', () =>
    img.parentElement.classList.toggle('full')));
  // the contents column follows the page
  const links = [...document.querySelectorAll('nav.toc a[href^="#"]')];
  const byId = new Map(links.map(a => [a.getAttribute('href').slice(1), a]));
  const obs = new IntersectionObserver(es => es.forEach(en => {
    if (en.isIntersecting) { links.forEach(a => a.classList.remove('active'));
      const a = byId.get(en.target.id); if (a) a.classList.add('active'); }
  }), {rootMargin: '-10% 0px -80% 0px'});
  byId.forEach((_, id) => { const el = document.getElementById(id); if (el) obs.observe(el); });
})();
"""


# --------------------------------------------------------------------------------------------- small pieces
def _img(png: bytes, caption: str) -> str:
    """A figure: the PNG embedded as a data URL (the page is one self-contained file), with its caption."""
    return (f'<figure><img alt="{e(caption)}" src="data:image/png;base64,{base64.b64encode(png).decode()}">'
            f'<figcaption>{e(caption)}</figcaption></figure>')


def _kpi(label: str, value: str, sub: str = "", status: str = "") -> str:
    """One KPI card: a label, a big value and a sub-line; status (ok | warn | error) colours it."""
    return (f"<div class='kpi {status}'><div class='k'>{e(label)}</div><div class='v'>{e(value)}</div>"
            f"<div class='s'>{e(sub)}</div></div>")


def _kpis(cards: list[str]) -> str:
    """A row of KPI cards (nothing without cards)."""
    return "<div class='kpis'>" + "".join(cards) + "</div>" if cards else ""


PAGE_ROWS = 25             # a paged table shows this many rows at first (then 50, 100, all: the page's script)


def _table(head: list[str], rows: list[str], sort: bool = True, tid: str = "", narrow: bool = False,
           paged: bool = False) -> str:
    """A table: head = column titles (a leading '#' makes a column right-aligned numbers), rows = '<tr>…</tr>';
    narrow: a few short columns, not stretched across the page (their numbers far from their names); paged: with
    more than PAGE_ROWS rows, a page at a time with a pager under it (the user: "these tables are too long in the
    report. can we pageinate, default is 25, then 50, 100, all")."""
    ths = "".join(f"<th class='num'>{e(h[1:])}</th>" if h.startswith("#") else f"<th>{e(h)}</th>" for h in head)
    classes = " ".join(c for c, on in (("sort", sort), ("paged", paged and len(rows) > PAGE_ROWS)) if on)
    attrs = (f' id="{tid}"' if tid else "") + (f' class="{classes}"' if classes else "")
    return (f"<div class='tablewrap{' narrow' if narrow else ''}'><table{attrs}><thead><tr>{ths}</tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def _num(v, fmt: str, sort_value=None) -> str:
    """A number cell, formatted, with its raw value for the table's sorting script (data-v; "–" sorts last)."""
    if v is None:
        return "<td class='num' data-v='-1e99'>–</td>"
    sv = v if sort_value is None else sort_value
    return f"<td class='num' data-v='{sv}'>{fmt.format(v)}</td>"


def _ce101_card(r, warn: float) -> list:
    """The MIL-STD-461 CE101 card of a stage or bridge (input lead current, 30 Hz-10 kHz; 461H ships to
    20 kHz), when CE101 is set."""
    from ..components.values import format_si
    w = getattr(r, "ce101_worst", None)
    if not w:
        return []
    return [_kpi("CE101", f"{w['margin_db']:+.1f} dB", f"{w['i_dbua']:.1f} dBµA @ {format_si(w['f'], 'Hz')}",
                 _margin_cls(w["margin_db"], warn))]


def _margin_cls(m: Optional[float], warn: float) -> str:
    """A margin's status: error below 0 dB, warn below the rules' warning margin, else ok ("" for none)."""
    if m is None:
        return ""
    return "error" if m < 0 else "warn" if m < warn else "ok"


def _margin_td(m: Optional[float], warn: float) -> str:
    """A margin cell, coloured by its status, sortable."""
    if m is None:
        return "<td class='num' data-v='1e99'>–</td>"
    cls = {"error": "bad", "warn": "meh", "ok": "good"}[_margin_cls(m, warn)]
    return f"<td class='num {cls}' data-v='{m}'>{m:+.1f} dB</td>"


def _notes(notes: list[str]) -> str:
    """Notes as a collapsed list (nothing without notes)."""
    if not notes:
        return ""
    return (f"<details class='notes'><summary>Notes ({len(notes)})</summary><ul>"
            + "".join(f"<li>{e(n)}</li>" for n in notes) + "</ul></details>")


def _anchor(kind: str, name: str) -> str:
    """An element id for a section (kind + the name with every other character a dash): links and the contents."""
    return kind + "-" + "".join(c if c.isalnum() else "-" for c in name).strip("-").lower()


def _where(t: dict) -> str:
    """A transfer's place: its distance from the route, or for a plane pair's displacement current (spread over
    the pair, from the board-wide model) the frequency it was taken at."""
    if t.get("kind") == "capacitance":
        return f"spread over the pair, @ {t.get('f', 0) / 1e6:.4g} MHz"
    return f"{t['distance_mm']:.1f} mm"


def _view3d_link(rr) -> str:
    """Links to the view's 3D page (and straight to its board-wide model), or nothing without maps."""
    from .view3d import file_stem, has_view
    if not has_view(rr):
        return ""
    href = e(f"view3d_{file_stem(rr)}.html", quote=True)
    wide = f" · <a href='{href}#board'>board-wide model</a>" if getattr(rr, "board_maps", None) else ""
    return (f"<p class='note noprint'><a href='{href}'>Open the 3D view</a>{wide} (a page next to this report: drag "
            "to turn the layer stack)</p>")


def _view_key(rr) -> tuple:
    """Which view a result is, for matching a sharpened one ("Sharper map") with the one the report draws: its kind,
    net, receiver and DC path (view_of)."""
    return (rr.kind, rr.net, getattr(rr, "receiver", ""), getattr(rr, "view_of", None) or "")


def _maps_size(rr) -> tuple[float, float]:
    """A view's maps figure size (inches): taller when several layers are shown side by side."""
    return (9.5, 6.0 if len(rr.main_layers()[:4]) <= 1 else 7.8)


class _Figures:
    """Figure rendering with progress (the plugin's export runs as a background job and says how far it is)."""

    def __init__(self, total: int, progress: Optional[Callable[[str, float], None]], max_vias: Optional[int] = None,
                 outlines: bool = False, names: bool = False):
        """total: the figures expected (for the progress fraction); the display options the maps follow."""
        self.total, self.done, self.progress = max(total, 1), 0, progress
        self.max_vias, self.outlines, self.names = max_vias, outlines, names
        self.sharper: dict = {}             # _view_key -> the view sharpened in the plugin ("Sharper map")

    def png(self, what: str, draw, *args, size=(9.5, 5.5), dpi: int = DPI, **kw) -> bytes:
        """One figure rendered to PNG, with a progress report first."""
        if self.progress:
            self.progress(f"report: {what}", min(self.done / self.total, 0.99))
        self.done += 1
        return figure_png(draw, *args, size=size, dpi=dpi, **kw)

    def heat(self, what: str, board, rr, *args, size=(9.5, 5.5)) -> bytes:
        """A view's maps (draw_heatmap), with at most max_vias via markers and, if asked, the parts' outlines and
        names; at the resolution its model has (heatmap_dpi: every cell at least 1.5 pixels, up to MAP_DPI_MAX).
        Without the maps' text boxes (shares, pin letters, switch names: the user found they did not help; the
        tables under the maps list the vias' shares)."""
        rr = self.sharper.get(_view_key(rr), rr)            # a view sharpened in the plugin: its finer maps
        return self.png(what, draw_heatmap, board, rr, *args, size=size, dpi=heatmap_dpi(rr, size, DPI, MAP_DPI_MAX),
                        max_vias=self.max_vias, outlines=self.outlines, names=self.names, labels=False)


# --------------------------------------------------------------------------------------------- the page
def render_html(board: Board, issues: list[Issue], routes: list, mode: str, settings: str = "",
                suggestions=None, converters: list = (), view3d: bool = False, bridges: list = (),
                thermal=None, capacity: list = (), solver: Optional[dict] = None, config=None,
                progress: Optional[Callable[[str, float], None]] = None, max_vias: Optional[int] = None,
                outlines: bool = False, names: bool = False, dc_combined=None, sharper: list = (),
                dc_loads: list = (), pdn: list = (), si=None) -> str:
    """The report page. si: the signal-integrity figures (analysis/si.SiResult), their own part. converters: power-stage results (a motor bridge's legs are shown with their bridge);
    dc_combined: every stage's DC paths together (SimulationResult.dc_combined, or its view), first under Power
    stages;
    bridges: BridgeResults; thermal: a ThermalResult; capacity: ThermalResults with `capacity`; solver: where the
    solves ran (gpu.Run.summary()); config: the settings (rules for the margins' colours, the `report:` cover
    lines); view3d: link each view to its 3D page (view3d_<stem>.html next to the report, written by the CLI);
    max_vias: via markers per map at most, the busiest (the plugin's "Vias shown"; None: every carrying via);
    outlines, names: the parts' outlines and references over the maps (the plugin's "Part outlines", "Part
    names"); dc_loads: DcLoadResults (analysis/dcload.py), their own part; sharper: views sharpened in the plugin ("Sharper map"), drawn instead of the ones they replace. Every
    map is drawn with as many pixels as its model has cells (up to MAP_DPI_MAX dpi); a click on a figure shows it at
    full resolution."""
    from .. import build_version
    # the build that wrote this page, and the one that solved it (a compute server's own; the same code, as the server
    # refuses another, but its build text names that machine's install)
    here = _named(build_version())
    solved_by = _named((solver or {}).get("build") or build_version())
    rules = getattr(config, "rules", None)
    warn = getattr(rules, "emi_margin_db", 6.0)
    meta = getattr(config, "report", None)
    stages = [c for c in converters if not getattr(c, "bridge", None)]
    legs = {b.net: [c for c in converters if getattr(c, "bridge", None) == b.net] for b in bridges}
    combined = dc_combined if dc_combined is not None and getattr(dc_combined, "dc", None) else None
    n_figs = (sum(3 for _ in routes) + sum(2 + len(c.dc) for c in stages) + (combined is not None)
              + sum(4 + len(legs[b.net]) + bool(b.sweep) for b in bridges) + (2 if thermal else 0) + len(capacity)
              + len(dc_loads or ()) + len(pdn or ()) + (1 if si is not None and (si.nets or si.pairs) else 0))
    figs = _Figures(n_figs, progress, max_vias, outlines, names)
    figs.sharper = {_view_key(v): v for v in sharper or ()}
    # live KiCad boards read "kicad-ipc:<name>": the cover shows the name
    board_name = (board.source.split(":", 1)[1] if board.source.startswith("kicad-ipc:")
                  else Path(board.source).name) if board.source else "board"
    parts, toc = [], []

    def part(pid: str, title: str, lead: str, body: str, sub: list[tuple[str, str]] = ()):
        """Add a numbered part of the report (and its entry, with sub-entries, in the contents)."""
        k = len(parts) + 1
        toc.append((pid, f"{k}. {title}", list(sub)))
        parts.append(f"<section class='part{' first' if k == 1 else ''}' id='{pid}'><h2><span class='num'>{k}</span>"
                     f"{e(title)}</h2>" + (f"<p class='lead'>{lead}</p>" if lead else "") + body + "</section>")

    part("summary", "Summary", "", _summary(issues, routes, stages, bridges, thermal, capacity, warn, figs, board,
                                            list(routes) + list(converters), rules))
    view = board_section(board, routes, converters, bridges, issues, config)
    if view:
        part("board", "Board", "The board as in an interactive BOM: front or back, one copper layer's pours and "
             "tracks, the parts with their pads, outlines and references, the analysed routes and power stages, "
             "and the issues numbered as in the Issues table. Drag to pan, wheel to zoom; hover over a part in "
             "the list to find it on the board, click an issue's marker for its message.", view)
    part("issues", "Issues", "Every finding of the run, most severe first. The first filter shows one severity; the "
         "checks below it show only the ones clicked (one or more; all: every check); a click on "
         "a column header sorts by it. A waived issue was accepted in the settings (with its reason).",
         _issues(issues))
    if routes:
        part("nets", "Signal nets", "Where each signal's return current flows in the planes, the loop it makes "
             "and the emission that loop radiates, against an ideal reference (the route's own planes, solid and "
             "stitched every 2 mm).", _nets(board, routes, issues, warn, figs, view3d),
             [(_anchor("net", f"{r.net} {r.receiver}"), r.net) for r in routes])
    if stages:
        from ..analysis.power import COMBINED_NET
        together = _combined_dc(board, combined, issues, rules, figs) if combined is not None else ""
        part("power", "Power stages", "Each switching stage's hot loop (the switches, the input capacitors and the "
             "copper between them), its DC power paths and its conducted emission at the input"
             + ("; first, all stages' DC currents together." if together else "."),
             together + "".join(_stage(board, c, issues, warn, figs, view3d, rules) for c in stages),
             ([(_anchor("stage", COMBINED_NET), "All stages: DC together")] if together else [])
             + [(_anchor("stage", c.net), c.net) for c in stages])
    rails = [d for d in dc_loads or () if not d.error and d.dc]
    if rails:
        from ..analysis.power import COMBINED_NET
        together = _combined_dc(board, combined, issues, rules, figs) if combined is not None and not stages else ""
        part("dcloads", "DC loads", "Each DC rail with its loads: the supply's current from its source to every "
             "load (and back through the ground when its returns are given), solved on one copper model of the whole "
             "board: the drop each load sees, the copper's loss, the current density and the vias.",
             together + "".join(_dc_load(board, d, issues, rules, figs) for d in rails),
             ([(_anchor("stage", COMBINED_NET), "All DC currents together")] if together else [])
             + [(_anchor("dcload", d.net), d.net) for d in rails])
    if pdn:
        part("pdn", "Power integrity", "Each checked supply rail's impedance as its ICs see it between their supply "
             "and ground pins, from 10 kHz to 1 GHz: the decaps, the planes' capacitance, the vias and the regulator "
             "together, solved on one model of the whole board. Below the target impedance (the allowed ripple over "
             "the load step) the rail holds its voltage whatever the frequency of the step.",
             "".join(_pdn(d, issues, figs) for d in pdn), [(_anchor("pdn", d.name), d.name) for d in pdn])
    if si is not None and (si.nets or si.pairs or si.crosstalk or si.stubs):
        part("si", "Signal integrity", "The listed nets' characteristic impedance along their tracks and the "
             "differential pairs' impedance and skew, from cross-sections of the board solved in two dimensions "
             "(the stackup's dielectrics and solder mask, every net's copper in the cut); the strongest crosstalk "
             "into tracks running beside them, and the via stubs.", _si(si, figs))
    if bridges:
        part("bridges", "Motor bridges", "Each bridge's six-step drive at its operating point, its legs' hot loops, "
             "the conducted emission of all legs at the input, the RMS currents in the copper, the parts' losses, "
             "the victims at the switching edges and the operating-point sweep.",
             "".join(_bridge(board, b, legs[b.net], issues, warn, figs) for b in bridges),
             [(_anchor("bridge", b.net), b.net) for b in bridges])
    if thermal is not None or capacity:
        part("thermal", "Temperatures", "The copper and part temperatures with the heat of every power stage and "
             "bridge (steady state), and the current at which the hottest point reaches the limit.",
             _thermal(board, thermal, capacity, issues, figs))
    if suggestions is not None:
        part("fixes", "Suggested fixes", "", _suggestions_section(suggestions))
    part("method", "Method and limits", "", _appendix(config, board))

    nav = "".join(f"<li><a href='#{pid}'>{e(title)}</a>" + (
        "<ol>" + "".join(f"<li><a href='#{a}'>{e(t)}</a></li>" for a, t in sub) + "</ol>" if sub else "") + "</li>"
                  for pid, title, sub in toc)
    title = (meta.title if meta and meta.title else "EMI and power analysis")
    now = datetime.now()
    cover_meta = [("Board", board_name), ("Date", f"{now:%d %B %Y, %H:%M}"), ("Analysis", mode),
                  ("Settings", Path(settings).name if settings else "–"),
                  ("Solved on", _solver_text(solver)), ("Version", solved_by)]
    res_factor = getattr(getattr(config, "analysis", None), "resolution", 1.0)
    if res_factor != 1:
        cover_meta.append(("Resolution", f"{res_factor:g}× the nodes"))
    if meta:
        cover_meta += [(k, v) for k, v in (("Company", meta.company), ("Prepared by", meta.author),
                                           ("Document", meta.document)) if v]
    cover = ("<header class='cover'><div class='kicker'>Analysis report</div>"
             f"<h1>{e(title)}</h1><div class='board'>{e(board_name)}</div><dl class='meta'>"
             + "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in cover_meta) + "</dl></header>")
    warnings = "".join(f"<p class='note'>Board note: {e(w)}</p>" for w in board.warnings)
    if progress:
        progress("report: done", 1.0)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="{e(here)}">
<title>{e(title)} – {e(board_name)}</title><style>{_CSS}{BOARD_CSS}</style></head>
<body><div class="layout">
<nav class="toc"><div class="brand">{e(title)}</div><div class="sub">{e(board_name)}</div><ol>{nav}</ol>
<div class="tools"><button class="f" onclick="window.print()">Print / PDF</button></div></nav>
<main>{cover}{warnings}{''.join(parts)}
<footer><span>{e(board_name)} · {e(title)}</span><span>Generated by {e(here)} on {now:%Y-%m-%d %H:%M}</span></footer>
</main></div><script>{_JS}</script><script>{BOARD_JS}</script></body></html>"""


def _named(build: str) -> str:
    """A build text as the cover shows it, in full ("GroundTruth 0.1.41910 (9094c40, built 2026-10-19 12:05)", as the
    help's title); a source-tree run's "development (source tree)" gets the name in front."""
    return build if build.startswith("GroundTruth") else f"GroundTruth {build}"


def _solver_text(solver: Optional[dict]) -> str:
    """Where the solves ran, for the cover ("CPU", "GPU <name>", with any fallbacks)."""
    if not solver:
        return "CPU"
    from ..solver.gpu import describe
    return describe(solver)


# --------------------------------------------------------------------------------------------- summary
def _summary(issues, routes, stages, bridges, thermal, capacity, warn, figs, board, views, rules=None) -> str:
    """The summary part: KPI cards (issue counts, the worst margins, hottest copper), an overview table of every
    net, stage and bridge, and the board overview figure."""
    live = [i for i in issues if not i.waived]
    count = {s: sum(1 for i in live if i.severity == s) for s in ("error", "warning", "info")}
    cards = [_kpi("Errors", str(count["error"]), "to fix", "error" if count["error"] else "ok"),
             _kpi("Warnings", str(count["warning"]), "to review", "warn" if count["warning"] else "ok"),
             _kpi("Information", str(count["info"]), f"{sum(1 for i in issues if i.waived)} waived")]
    analysed = ", ".join(x for x in (f"{len(routes)} net route(s)" if routes else "",
                                     f"{len(stages)} power stage(s)" if stages else "",
                                     f"{len(bridges)} motor bridge(s)" if bridges else "") if x) or "rule checks"
    cards.append(_kpi("Analysed", str(len(routes) + len(stages) + len(bridges)), analysed))
    rad = [(r.worst["margin_db"], r.net) for r in list(routes) + list(stages) if getattr(r, "worst", None)]
    rad += [(b.worst_leg["radiated"]["margin_db"], b.net) for b in bridges if b.worst_leg.get("radiated")]
    if rad:
        m, who = min(rad)
        cards.append(_kpi("Worst radiated margin", f"{m:+.1f} dB", who, _margin_cls(m, warn)))
    cond = [(c.conducted_worst["margin_db"], c.net) for c in list(stages) + list(bridges)
            if getattr(c, "conducted_worst", None)]
    if cond:
        m, who = min(cond)
        cards.append(_kpi("Worst conducted margin", f"{m:+.1f} dB", who, _margin_cls(m, warn)))
    if thermal is not None and not thermal.error:
        st = "error" if thermal.t_max >= thermal.max_c else "warn" if thermal.t_max >= thermal.warn_c else "ok"
        cards.append(_kpi("Hottest point", f"{thermal.t_max:.0f} °C", f"{thermal.power_w:.1f} W in all", st))
    findings = _findings_by_check(live)
    where = ("<p class='note'>Where they are: the <a href='#board'>Board</a> section shows the board with the "
             "analysed routes and stages and every issue numbered; a check's count opens the Issues table showing "
             "only that check.</p>") if board.footprints else ""
    return (_kpis(cards) + "<div class='summary-grid one'><div class='item'>"
            "<h4 style='margin-top:0'>Main findings</h4>" + findings + where + "</div></div>"
            + _at_a_glance(routes, stages, bridges, issues, warn, rules))


def _overshoot(c, rules=None) -> tuple[str, float, list, str]:
    """A power stage's overshoot as the hot_loop check judges it: (the estimate that is largest, its volts, every
    estimate [(name, V)], its level "error" | "warn" | "ok" against the rules' shares of V_in, "" without v_in)."""
    rec = getattr(c, "recovery", None)
    parts = [("L·di/dt", c.overshoot_v or 0.0), ("ringing", (c.ringing or {}).get("overshoot_v") or 0.0),
             ("recovery", (rec or {}).get("overshoot_v") or 0.0)]
    name, over = max(parts, key=lambda p: p[1])
    if not c.v_in:
        return name, over, parts, ""
    frac = over / c.v_in
    level = ("error" if frac >= getattr(rules, "overshoot_error", 0.5) else
             "warn" if frac >= getattr(rules, "overshoot_warning", 0.25) else "ok")
    return name, over, parts, level


FINDING_CHECKS = 10            # checks listed in the summary's main findings (the rest: the Issues table)
_SEV_RANK = {"error": 0, "warning": 1, "info": 2}


def _findings_by_check(live: list[Issue]) -> str:
    """The summary's main findings: one line per check, the most severe checks first and the most frequent first
    within a severity, each with its counts per severity, its worst case (the lowest margin where the issues carry
    one, else its first most severe issue) and a link opening the Issues table showing that check only. Before
    2026-10-20 the eight most severe issues were listed one by one: on kraft_flux_mini_2 eight clearance errors of one
    rail, and none of the converters' failing emission margins or their overshoot."""
    from ..core.issues import CHECKS
    if not live:
        return "<p class='empty'>No findings.</p>"
    groups: dict[str, list[Issue]] = {}
    for i in live:
        groups.setdefault(i.check, []).append(i)

    def rank(c: str) -> tuple:
        """Sort key of a check: its most severe issue, then the number of its issues (more first)."""
        return min(_SEV_RANK.get(i.severity, 3) for i in groups[c]), -len(groups[c]), c

    def worst(items: list[Issue]) -> Issue:
        """The issue a check is shown by: the most severe, and among those the lowest margin where they have one."""
        top = min(_SEV_RANK.get(i.severity, 3) for i in items)
        same = [i for i in items if _SEV_RANK.get(i.severity, 3) == top]
        with_margin = [i for i in same if isinstance((i.details or {}).get("margin_db"), (int, float))]
        return min(with_margin, key=lambda i: i.details["margin_db"]) if with_margin else same[0]
    lines = []
    checks = sorted(groups, key=rank)
    for c in checks[:FINDING_CHECKS]:
        items = groups[c]
        w = worst(items)
        counts = ", ".join(f"{n} {s}{'s' if n > 1 and s != 'info' else ''}" for s in ("error", "warning", "info")
                           if (n := sum(1 for i in items if i.severity == s)))
        title = CHECKS.get(c, ("", c))[1]
        lines.append(f"<li><span class='pill {w.severity}'>{e(w.severity)}</span><div><div class='fcheck'>"
                     f"<b>{e(title)}</b> <code>{e(c)}</code> · <a href='#issues' data-show-check='{e(c)}'>{counts}"
                     f"</a></div><div class='fworst'>{'worst: ' if len(items) > 1 else ''}{_finding(w)}</div></div>"
                     "</li>")
    more = len(checks) - FINDING_CHECKS
    tail = f"<p class='note'>{more} more check(s) in the <a href='#issues'>Issues</a> table.</p>" if more > 0 else ""
    return "<ul class='findings bycheck'>" + "".join(lines) + "</ul>" + tail


def _at_a_glance(routes, stages, bridges, issues, warn, rules=None) -> str:
    """The summary's table of everything analysed, one row each, linked to its part: a net's route or a power
    stage's or motor bridge's operating point, its worst radiated and conducted margins, a stage's overshoot (the
    largest estimate: L·di/dt, the ringing, a recovery) against its input voltage and its efficiency."""
    from ..components.values import format_si
    if not (routes or stages or bridges):
        return ""
    rows = []
    for r in routes:
        if getattr(r, "error", None):
            continue
        w = getattr(r, "worst", None)
        rows.append(f"<tr><td><a href='#{_anchor('net', f'{r.net} {r.receiver}')}'>{e(r.net)}</a></td>"
                    f"<td>net {e(r.driver)} → {e(r.receiver)}</td>"
                    + _margin_td(w["margin_db"] if w else None, warn) + "<td class='num'>–</td>"
                    "<td class='num'>–</td><td class='num'>–</td></tr>")
    for c in stages:
        if c.error:
            continue
        w, cw = c.worst, c.conducted_worst
        _, over, _, level = _overshoot(c, rules)
        cls = {"error": "bad", "warn": "meh", "ok": "good"}.get(level, "")
        over_td = (f"<td class='num {cls}' data-v='{over}'>{over:.1f} V ({over / c.v_in * 100:.0f} %)</td>"
                   if c.v_in else "<td class='num'>–</td>")
        eff = (c.input or {}).get("efficiency")
        op = (f"{c.topology}, {c.v_in:g} V, {c.pulse_a:g} A, {format_si(c.f_sw, 'Hz')}, edge "
              f"{c.rise_time * 1e9:g} ns" if c.v_in else f"{c.topology}, {c.pulse_a:g} A, {format_si(c.f_sw, 'Hz')}")
        rows.append(f"<tr><td><a href='#{_anchor('stage', c.net)}'>{e(c.net)}</a></td><td>{e(op)}</td>"
                    + _margin_td(w["margin_db"] if w else None, warn)
                    + _margin_td(cw["margin_db"] if cw else None, warn) + over_td
                    + _num(eff * 100 if eff else None, "{:.1f} %") + "</tr>")
    for b in bridges:
        rad = (getattr(b, "worst_leg", None) or {}).get("radiated")
        cw = getattr(b, "conducted_worst", None)
        rows.append(f"<tr><td><a href='#{_anchor('bridge', b.net)}'>{e(b.net)}</a></td><td>motor bridge</td>"
                    + _margin_td(rad["margin_db"] if rad else None, warn)
                    + _margin_td(cw["margin_db"] if cw else None, warn) + "<td class='num'>–</td>"
                    "<td class='num'>–</td></tr>")
    if not rows:
        return ""
    return ("<h4>At a glance</h4>" + _table(["Analysed", "Operating point", "#Radiated margin", "#Conducted margin",
                                             "#Overshoot (of V in)", "#Efficiency"], rows))


def _finding(i: Issue) -> str:
    """An issue as a line: its net in bold, then the message (most messages start with the net already)."""
    msg = i.message
    if msg.startswith(i.net + ":"):
        msg = msg[len(i.net) + 1:].lstrip()
    return f"<b>{e(i.net)}</b> · {e(msg)}"


def _issues(issues: list[Issue]) -> str:
    """The issues table (numbered as on the board view, filterable by severity, sortable)."""
    if not issues:
        return "<p class='empty'>No issues.</p>"
    rank = {"error": 0, "warning": 1, "info": 2}
    rows = []
    for k, i in enumerate(sorted(issues, key=lambda i: (i.waived, rank.get(i.severity, 3))), 1):
        rows.append(f"<tr data-sev='{i.severity}' data-check='{e(i.check)}' class='{'waived' if i.waived else ''}'>"
                    f"<td class='num' data-v='{k}'>{k}"
                    f"</td><td data-v='{rank.get(i.severity, 3)}'><span class='pill {i.severity}'>{e(i.severity)}</span>"
                    f"{WAIVED if i.waived else ''}</td>"
                    f"<td class='nw'>{e(i.check)}</td><td class='netc'>{e(i.net).replace('/', '/<wbr>')}</td>"
                    f"<td class='finding'>{e(i.message)}"
                    + (f"<br><span class='note'>Waived: {e(i.waiver_reason)}</span>" if i.waived and i.waiver_reason
                       else "") +
                    f"</td><td class='where' data-v='{i.location[0]}'>{i.location[0]:.2f}, {i.location[1]:.2f}"
                    + (f"<br><span class='note'>{e(i.layer)}</span>" if i.layer else "")
                    + f"</td><td><code class='fp' title='fingerprint (waivers name it)'>{i.fingerprint}</code></td></tr>")
    n = {s: sum(1 for i in issues if i.severity == s) for s in rank}
    filters = ("<div class='filters noprint'><button class='f on' data-sev='all'>All " + str(len(issues))
               + "</button>" + "".join(f"<button class='f' data-sev='{s}'>{s.capitalize()} {n[s]}</button>"
                                       for s in rank if n[s]) + "</div>")
    # the checks: one chip each (its count follows the severity chosen), any number of them picked; none picked shows
    # every check. The user: "I only want to see clearance_voltage or cap_voltage ... or vds_rating and
    # plane_resonance. So be able to filter out one or more"
    from ..core.issues import CHECKS
    counts: dict[str, int] = {}
    for i in issues:
        counts[i.check] = counts.get(i.check, 0) + 1
    if len(counts) > 1:
        filters += ("<div class='filters noprint checks'><span class='note'>Checks:</span>"
                    "<button class='f on' data-check-all='1' title='Show every check'>all</button>"
                    + "".join(f"<button class='f' data-check='{e(c)}' title='{e(CHECKS.get(c, ('', ''))[1])}'>"
                              f"{e(c)} <span class='cn'>{counts[c]}</span></button>" for c in sorted(counts))
                    + "<span class='note' id='issues-shown'></span></div>")
    return filters + _table(["#Nr", "Severity", "Check", "Net", "Finding", "Where (mm, layer)", "Fingerprint"],
                            rows, tid="issue-table", paged=True)


# --------------------------------------------------------------------------------------------- signal nets
def _nets(board, routes, issues, warn, figs, view3d) -> str:
    """The signal nets part: a table of every route, then each route's article."""
    rows = []
    for r in routes:
        a = _anchor("net", f"{r.net} {r.receiver}")
        res = r.resonance or {}
        rows.append(f"<tr><td><a href='#{a}'>{e(r.net)}</a></td><td>{e(r.driver)} → {e(r.receiver)}</td>"
                    f"<td>{e(r.reference_layer or '–')}</td>{_num(r.l_ratio, '{:.2f}×')}"
                    f"{_margin_td(r.worst['margin_db'] if r.worst else None, warn)}"
                    f"{_num(res.get('rise_db'), '{:+.1f} dB')}<td>{e(r.error) if r.error else ''}</td></tr>")
    out = [_table(["Net", "Driver → receiver", "Return on", "#Loop L / ideal", "#Worst margin", "#Plane resonance",
                   "Error"], rows)]
    for r in routes:
        out.append(_net(board, r, issues, warn, figs, view3d))
    return "".join(out)


def _net(board, rr, issues, warn, figs, view3d) -> str:
    """One route's article: its figures, signal path, maps, cross-section, spectrum, the vias and parts carrying
    its return, its issues and notes."""
    head = (f"<article class='item' id='{_anchor('net', f'{rr.net} {rr.receiver}')}'><header><h3>{e(rr.net)}</h3>"
            f"<span class='what'>{e(rr.driver)} → {e(rr.receiver)}</span></header>")
    if rr.error:
        return head + f"<p class='bad'>{e(rr.error)}</p></article>"
    shares = ", ".join(f"{l} {s * 100:.0f} %" for l, s in sorted(rr.layer_share.items(), key=lambda kv: -kv[1])
                       if s >= 0.02)
    cards = [_kpi("Return current", rr.reference_layer or "–", shares or f"h = {rr.height_mm:.3f} mm"),
             _kpi("Loop inductance", f"{rr.solves[-1]['L_nH']:.2f} nH" if rr.solves else "–",
                  f"ideal {rr.ideal.get('L_nH', float('nan')):.2f} nH" if rr.ideal else ""),
             _kpi("Versus ideal", f"{rr.l_ratio:.2f}×" if rr.l_ratio else "–",
                  f"loop moment {rr.m_excess_db:+.1f} dB" if rr.m_excess_db is not None else "")]
    if rr.worst:
        m = rr.worst["margin_db"]
        cards.append(_kpi("Worst margin", f"{m:+.1f} dB",
                          f"{rr.worst['e_dbuv']:.1f} dBµV/m @ {rr.worst['f'] / 1e6:.4g} MHz", _margin_cls(m, warn)))
    cards.append(_kpi("Line", f"{rr.z0:.0f} Ω", f"series +{rr.series_ohms:g} Ω" if rr.series_ohms else "Z0"))
    if rr.resonance:
        r = rr.resonance
        cards.append(_kpi("Plane resonance", f"{r['rise_db']:+.1f} dB",
                          f"{r['f'] / 1e6:.4g} MHz" + (f", {r['pair'][0]}–{r['pair'][1]}" if r.get("pair") else "")))
    body = [head, _kpis(cards)]
    body.append(_img(figs.heat(f"{rr.net} maps", board, rr, issues, size=_maps_size(rr)),
                     f"{rr.net}: return current density on each plane layer (arrows: the direction of flow)"))
    if view3d:
        body.append(_view3d_link(rr))
    if getattr(rr, "section", None):
        body.append(_img(figs.png(f"{rr.net} cross-section", draw_section, board, rr, size=(9.5, 3.4)),
                         f"{rr.net}: the return current layer by layer along the route, driver on the left"))
    else:
        figs.done += 1
    if rr.transfers:
        body.append("<h4>Where the return current changes layer or net</h4>" + _table(
            ["Via / part / plane pair", "Layers", "#Share of the return", "#Where"],
            [f"<tr><td>{e(t['name'])}</td><td>{e(' → '.join(t['layers']))}</td>"
             f"<td class='num' data-v='{t['fraction']}'>{share(t['fraction'])}</td>"
             f"<td class='num' data-v='{t['distance_mm']}'>{e(_where(t))}</td></tr>" for t in rr.transfers]))
    path = getattr(rr, "path", None) or {}
    if any(x["kind"] in ("part", "shunt", "amp") for x in path.get("elements", ())):
        rows = []
        for x in path["elements"]:
            if x["kind"] == "part":
                z = x.get("z_ohm")
                rows.append(f"<tr><td>{e(x['ref'])}</td><td>{e(x['model'])}</td><td class='num'>"
                            f"{'–' if z is None else _si2(z, 'Ω')}</td><td class='num'>{x['length_mm']:.1f}</td></tr>")
            elif x["kind"] == "amp":
                g = x.get("gain")
                rows.append(f"<tr><td>{e(x['ref'])}</td><td>op-amp stage{'' if g is None else f', gain {g:.3g}'}</td>"
                            "<td class='num'>–</td><td class='num'>–</td></tr>")
            elif x["kind"] == "shunt":
                z = x.get("z_ohm")
                cur = f", {x['i_ma']:.3g} mA" if x.get("i_ma") is not None else ""
                rows.append(f"<tr><td>{e(x['ref'])}</td><td>{e(x['model'])} to {e(x['to'])}{cur}</td><td class='num'>"
                            f"{'–' if z is None else _si2(z, 'Ω')}</td><td class='num'>–</td></tr>")
            else:
                rows.append(f"<tr><td>{e(', '.join(x['nets']) or 'track')}</td><td>line, Z0 {x['z0']:.0f} Ω</td>"
                            f"<td class='num'>–</td><td class='num'>{x['length_mm']:.1f}</td></tr>")
        body.append(f"<h4>Signal path at {path['f'] / 1e6:.4g} MHz (driver to receiver)</h4>"
                    f"<p class='note'>current into the path {path['i_in_ma']:.3g} mA, mean along it "
                    f"{path['i_mean_ma']:.3g} mA, at the receivers {path['v_end_v']:.3g} V (per harmonic)</p>"
                    + _table(["Stretch / part", "What", "#|Z|", "#Length (mm)"], rows, sort=False))
    body.append(_img(figs.png(f"{rr.net} spectrum", draw_spectrum, rr, size=(9.5, 3.4)),
                     f"{rr.net}: radiated estimate per harmonic against the limit"))
    body.append(_responses(rr.net, getattr(rr, "responses", ()), figs))
    body.append(_notes(rr.notes))
    return "".join(body) + "</article>"


# --------------------------------------------------------------------------------------------- power stages
def _stage(board, c, issues, warn, figs, view3d, rules=None) -> str:
    """One power stage's article: hot loop and ringing, capacitor sharing, radiated and conducted estimates, DC
    paths, the fix advisor's options, issues and notes."""
    head = (f"<article class='item' id='{_anchor('stage', c.net)}'><header><h3>{e(c.net)}</h3>"
            f"<span class='what'>{e(c.topology)}: {e(c.driver)} → {e(c.receiver)}, {c.f_sw / 1e3:g} kHz, "
            f"{c.pulse_a:g} A, edge {c.rise_time * 1e9:g} ns</span></header>")
    if c.error:
        return head + f"<p class='bad'>{e(c.error)}</p></article>"
    # the overshoot judged is the largest estimate (L·di/dt over the edge, the ringing with C_oss, a recovery's
    # snap-off), as the hot_loop check takes it; its colour is that check's. Before 2026-10-20 the card showed L·di/dt
    # alone: green "7.5 V" on kraft_flux_mini_2 beside a 40 V ringing the check called an error
    rec = getattr(c, "recovery", None)
    name, over, parts, status = _overshoot(c, rules)
    cards = [_kpi("Hot loop", f"{c.loop_nh:.2f} nH", f"{c.loop_mohm:.0f} mΩ at {c.loop_f / 1e6:g} MHz"),
             _kpi("Overshoot", f"{over:.1f} V",
                  (f"{over / c.v_in * 100:.0f} % of {c.v_in:g} V, from the {name}" if c.v_in else "set v_in")
                  + "".join(f"; {n} {v:.1f} V" for n, v in parts if v and n != name), status)]
    if c.ringing:
        r = c.ringing
        cards.append(_kpi("Ringing", f"{r['f'] / 1e6:.0f} MHz", f"Q {r['q']:.1f} with C_oss and the loop"))
    if rec:
        cards.append(_kpi("Recovery", f"{rec['i_rr']:.1f} A", f"{rec['q_rr'] * 1e9:g} nC in {rec['t_rr'] * 1e9:.1f} ns" +
                          (f", overshoot {rec['overshoot_v']:.1f} V" if rec.get("overshoot_v") else "")))
    if c.conducted_worst:
        w = c.conducted_worst
        cards.append(_kpi("Conducted", f"{w['margin_db']:+.1f} dB",
                          f"{w['v_dbuv']:.1f} dBµV @ {w['f'] / 1e6:.4g} MHz", _margin_cls(w["margin_db"], warn)))
    cards += _ce101_card(c, warn)
    if c.worst:
        w = c.worst
        cards.append(_kpi("Radiated", f"{w['margin_db']:+.1f} dB", f"{w['e_dbuv']:.1f} dBµV/m @ {w['f'] / 1e6:.4g} MHz",
                          _margin_cls(w["margin_db"], warn)))
    g = getattr(c, "interleave", None)
    if g:                                    # its interleave group: the stages together (analysis/interleave.py)
        who = ", ".join(f"{n} {p:g}°" for n, p in g["phases_deg"].items())
        for label, key, inp in (("Conducted, group", "conducted_worst", "conducted_in_phase_worst"),
                                ("Radiated, group", "worst", "radiated_in_phase_worst")):
            w, wi = g.get(key), g.get(inp)
            if w:
                cards.append(_kpi(label, f"{w['margin_db']:+.1f} dB",
                                  f"{who} @ {w['f'] / 1e6:.4g} MHz" + (f"; in phase {wi['margin_db']:+.1f} dB"
                                                                      if wi else ""),
                                  _margin_cls(w["margin_db"], warn)))
    flt = getattr(c, "input_filter", None)
    if flt:
        cards.append(_kpi("Input filter (guidance)", f"f_c ≤ {flt['f_c'] / 1e3:.3g} kHz",
                          f"e.g. {_si2(flt['L'], 'H')} + {_si2(flt['C'], 'F')}"))
    if c.cm:
        cards.append(_kpi("Common-mode budget", f"≤ {c.cm['budget_pf']:.2g} pF",
                          f"switch node to chassis, @ {c.cm['f'] / 1e6:.4g} MHz"))
    body = [head, _kpis(cards)]
    if getattr(c, "fixes", None):
        body.append(_fixes(c.fixes, warn))
    body.append(_img(figs.heat(f"{c.net} hot loop", board, c, issues, size=_maps_size(c)),
                     f"{c.net}: hot-loop current density at {c.loop_f / 1e6:g} MHz"))
    if view3d:
        body.append(_view3d_link(c))
    caps = c.caps
    if caps:
        body.append("<h4>Who carries the loop current</h4>" + _table(
            ["Part", f"#At {c.loop_f / 1e6:g} MHz (edges)", f"#At {c.f_sw / 1e3:g} kHz (ripple)"],
            [f"<tr><td>{e(x['name'])}{' (input lines)' if x['lisn'] else ''}</td>"
             f"<td class='num' data-v='{x['share_hf']}'>{share(x['share_hf'])}</td>"
             f"<td class='num' data-v='{x['share_sw'] or 0}'>"
             f"{share(x['share_sw']) if x['share_sw'] is not None else '–'}</td></tr>" for x in caps], narrow=True))
    if c.dc:
        from ..analysis.power import dc_view
        rows = []
        for p in c.dc:
            via = p["vias"][0] if p["vias"] else None
            via_txt = (f"{e(via['name'])}: {via['current_a']:.2f} A"
                       + (f", {via['load_pct']:.0f} % of IPC-2221" if via.get('load_pct') is not None else "")
                       ) if via else "–"
            rows.append(f"<tr><td>{e(p['name'])}</td>{_num(p['current_a'], '{:.3g} A')}<td>{e(dc_resistance(p))}</td>"
                        f"{_num(p['drop_mv'], '{:.1f} mV')}{_num(p['loss_w'] * 1e3, '{:.0f} mW')}"
                        f"{_num(p['sustained_a_mm2'], '{:.0f} A/mm²')}<td>{e(p['sustained_layer'])}</td>"
                        f"<td>{via_txt}</td></tr>")
        body.append("<h4>DC power paths</h4>" + _table(
            ["Path", "#Current", "Resistance", "#Drop", "#Loss", "#Sustained density", "Layer", "Busiest via"], rows))
        for p in c.dc:
            v = dc_view(c, p["name"])
            body.append(_img(figs.heat(f"{c.net} DC {p['name']}", board, v, issues, size=_maps_size(v)),
                             f"{c.net}: DC {p['name']}, current density at {p['current_a']:.3g} A"))
    body.append(_img(figs.png(f"{c.net} spectrum", draw_spectrum, c, size=(9.5, 3.4)),
                     f"{c.net}: radiated and conducted estimates against the limits"))
    if getattr(c, "victims", None):
        body.append("<h4>Victims at the switching edges</h4>" + _table(
            ["Finding", "#Peak", "#Threshold"], [_victim_row(v) for v in c.victims], paged=True))
    body.append(_responses(c.net, getattr(c, "responses", ()), figs))
    body.append(_notes(c.notes))
    return "".join(body) + "</article>"


def _victim_row(v: dict, text=None) -> str:
    """A victim's table row: its sentence (`text`, default analysis/victims.victim_text; a bridge's its own), the
    peak and the threshold with their unit (V for a net's noise or a part's ground bounce, A for a current injected
    through a capacitance), the peak red at or above the threshold."""
    from ..components.values import format_si
    from ..analysis.victims import victim_text
    text = text or victim_text
    unit = "V" if v.get("kind") in ("net", "part") else "A"
    peak, limit = v["peak"], v.get("limit")
    cls = " bad" if limit is not None and peak >= limit else ""
    return (f"<tr><td>{e(text(v))}</td><td class='num{cls}' data-v='{peak}'>{format_si(float(f'{peak:.3g}'), unit)}"
            f"</td>" + (f"<td class='num' data-v='{limit}'>{format_si(float(f'{limit:.3g}'), unit)}</td>"
                        if limit is not None else "<td class='num'>–</td>") + "</tr>")


COMBINED_VIAS = 12              # vias listed in the stages-together table (the busiest; the map marks them all)


def _si(si, figs) -> str:
    """The signal-integrity part (analysis/si.py): each net's Z0 and each pair's Zdiff (tables and a range chart
    against their targets), the strongest crosstalk, the via stubs."""
    from .plots import draw_impedance
    body = []
    if si.nets:
        rows = []
        for n in si.nets:
            s = n.summary()
            ok = "z_mean" in s
            cells = [e(n.net), f"{s['length_mm']:.1f}", f"{s['z_mean']:.1f}" if ok else "–",
                     f"{s['z_min']:.1f}-{s['z_max']:.1f}" if ok else "–",
                     e(f"{n.target:g} Ω ± {n.tol * 100:g} %") if n.target else "none",
                     f"{s.get('off_mm', 0):.1f}" if n.target else "–"]
            rows.append("<tr>" + "".join(f"<td class='num'>{c}</td>" if k in (1, 2, 5) else f"<td>{c}</td>"
                                         for k, c in enumerate(cells)) + "</tr>")
        body.append("<h4>Impedance</h4>" + _table(["Net", "#Length (mm)", "#Z0 mean (Ω)", "Range (Ω)", "Target",
                                                   "#Outside (mm)"], rows))
    if si.pairs:
        rows = []
        for p in si.pairs:
            s = p.summary()
            ok = "zdiff_mean" in s
            cells = [e(s["pair"]), f"{s['zdiff_mean']:.1f}" if ok else "–",
                     f"{s['zdiff_min']:.1f}-{s['zdiff_max']:.1f}" if ok else "–",
                     e(f"{p.spec.zdiff:g} Ω") if p.spec.zdiff else "none",
                     " / ".join(f"{v:.1f}" for v in s["length_mm"].values()), f"{s['skew_ps']:.0f}",
                     f"{s['skew_limit_ps']:.0f}", f"{s['uncoupled_mm']:.1f}"]
            rows.append("<tr>" + "".join(f"<td class='num'>{c}</td>" if k in (1, 5, 6, 7) else f"<td>{c}</td>"
                                         for k, c in enumerate(cells)) + "</tr>")
        body.append("<h4>Differential pairs</h4>" + _table(["Pair", "#Zdiff mean (Ω)", "Range (Ω)", "Target",
                                                            "Lengths P / N (mm)", "#Skew (ps)", "#Limit (ps)",
                                                            "#Uncoupled (mm)"], rows))
    if si.nets or si.pairs:
        body.append(_img(figs.png("signal integrity impedance", draw_impedance, si, size=(9.5, 0.9 + 0.32 * (
            len(si.nets) + len(si.pairs)))), "Each net's Z0 and each pair's Zdiff along its tracks: the bar spans "
            "the lowest to the highest cross-section, the dot is the length-weighted mean, the shaded band the "
            "target with its tolerance"))
    if si.crosstalk:
        rows = []
        for x in sorted(si.crosstalk, key=lambda r: -max(r["next"], r["fext"]))[:12]:
            cells = [e(x["aggressor"]), e(x["victim"]), e(x["layer"]), f"{x['coupled_mm']:.1f}", f"{x['gap_mm']:.2f}",
                     f"{x['next'] * 100:.2f}", f"{x['fext'] * 100:.2f}",
                     f"{max(x['next'], x['fext']) * x['swing_v'] * 1e3:.0f}"]
            rows.append("<tr>" + "".join(f"<td class='num'>{c}</td>" if k >= 3 else f"<td>{c}</td>"
                                         for k, c in enumerate(cells)) + "</tr>")
        body.append("<h4>Crosstalk (the strongest)</h4>" + _table(
            ["Aggressor", "Victim", "Layer", "#Coupled (mm)", "#Gap (mm)", "#NEXT (%)", "#FEXT (%)", "#Peak (mV)"],
            rows))
    if getattr(si, "delays", None):
        rows = []
        for d in si.delays:
            cells = [e(d["profile"]), e(d["net"]), e(d["layer"]), f"{d['kicad_ps_cm']:.1f}",
                     f"{d['solved_ps_cm']:.1f}", f"{100 / d['ratio']:.0f}"]
            rows.append("<tr>" + "".join(f"<td class='num'>{c}</td>" if k >= 3 else f"<td>{c}</td>"
                                         for k, c in enumerate(cells)) + "</tr>")
        body.append("<h4>KiCad tuning profiles: unit delay</h4>" + _table(
            ["Profile", "Net", "Layer", "#KiCad (ps/cm)", "#Cross-sections (ps/cm)", "#A tuned delay comes out (%)"],
            rows))
    if si.stubs:
        rows = []
        for x in sorted(si.stubs, key=lambda r: r["f_res"])[:12]:
            cells = [e(x["net"]), e(f"({x['at'][0]:.2f}, {x['at'][1]:.2f})"), e(" to ".join(x["layers"])),
                     f"{x['stub_mm']:.2f}", f"{x['f_res'] / 1e9:.1f}", f"{x['knee'] / 1e9:.2f}"]
            rows.append("<tr>" + "".join(f"<td class='num'>{c}</td>" if k >= 3 else f"<td>{c}</td>"
                                         for k, c in enumerate(cells)) + "</tr>")
        body.append("<h4>Via stubs (the lowest resonances)</h4>" + _table(
            ["Net", "Via at (mm)", "Stub", "#Length (mm)", "#Resonance (GHz)", "#Signal knee (GHz)"], rows))
    return "".join(body)


def _responses(owner: str, responses, figs) -> str:
    """An owner's frequency responses (analysis/transfer.py): the Bode plot, the pole-zero map and each response's
    poles and zeros (natural frequency, Q, exact or fitted with its error)."""
    responses = [r for r in responses if len(r.f)]
    if not responses:
        return ""
    from ..analysis.transfer import BodeView, fmt_root, pz_rows, unit_groups
    from .plots import draw_bode, draw_pz
    rows = []
    for r in responses:
        how = "exact" if r.exact else (f"fitted (max {r.fit_error_db:.2f} dB)" if r.fit_error_db is not None
                                       else "no fit")
        rows.append(f"<tr><td>{e(r.name.split(': ', 1)[-1])}</td><td>{e(how)}</td>"
                    f"<td>{e(', '.join(fmt_root(x) for x in pz_rows(r.poles)) or 'none')}</td>"
                    f"<td>{e(', '.join(fmt_root(x) for x in pz_rows(r.zeros)) or 'none')}</td></tr>")
    out = ["<h4>Frequency response, poles and zeros</h4>"]
    # one Bode plot per magnitude unit (ratios in dB, impedances in Ω: unit_groups), one pole-zero map for all
    for name, group in unit_groups(owner, responses):
        out.append(_img(figs.png(f"{name} Bode", draw_bode, BodeView(name, group), size=(9.5, 5.0)),
                        f"{name}: magnitude and phase; × poles and ○ zeros at their natural frequencies"))
    out.append(_img(figs.png(f"{owner} poles and zeros", draw_pz, BodeView(owner, responses), size=(9.5, 4.0)),
                    f"{owner}: poles and zeros in the s-plane (Hz; symmetric log axes)"))
    out.append(_table(["Response", "Model", "Poles (f0, Q)", "Zeros (f0, Q)"], rows, sort=False))
    return "".join(out)


def _pdn(d, issues, figs) -> str:
    """The article of one PDN rail (analysis/pdn.py): its target and source, per IC its worst margin, highest peak,
    the inductance above the decaps and the decaps near it, and the impedance plot."""
    from ..components.values import format_si
    from .plots import draw_pdn
    body = [f"<article class='item' id='{_anchor('pdn', d.name)}'><header><h3>{e(d.name)}</h3>"
            f"<span class='what'>PDN, {e(d.rail)} to {e(d.ground)}"
            + (f", {d.voltage:g} V" if d.voltage else "") + "</span></header>"]
    if d.error:
        return "".join(body) + f"<p class='note'>{e(d.error)}</p></article>"
    cards = [_kpi("Target", format_si(d.target_ohm, "Ω") if d.target_ohm else "none",
                  f"up to {format_si(d.check_to, 'Hz')}" if d.target_ohm else "set i_step for one")]
    if d.worst:
        w = d.worst
        cards.append(_kpi("Worst margin", f"{w['margin_db']:+.1f} dB",
                          f"{w['part']}: {format_si(w['z_ohm'], 'Ω')} @ {format_si(w['f'], 'Hz')}",
                          "error" if w["margin_db"] < 0 else "warn" if w["margin_db"] < 3 else "ok"))
    cards.append(_kpi("ICs", str(len(d.ports)), "with supply pins on the rail"))
    if d.source:
        cards.append(_kpi("Source", d.source.split(" ", 1)[0], d.source.split(" ", 1)[1] if " " in d.source else ""))
    body.append(_kpis(cards))
    rows = []
    for p in d.ports:
        w = p.get("worst") or {}
        pk = p["peaks"][0] if p["peaks"] else None
        near = [f"{c['ref']} {c['value']} ({n['mm']} mm)" for c in d.decaps for n in c["near"]
                if n["part"] == p["part"]]
        cells = [e(p["part"]), str(len(p["pins"])),
                 e(f"{w['margin_db']:+.1f} dB @ {format_si(w['f'], 'Hz')}") if w else "–",
                 e(w.get("holds", "")),
                 e(f"{format_si(pk['z_ohm'], 'Ω')} @ {format_si(pk['f'], 'Hz')}") if pk else "–",
                 f"{p['l_nh']:.2f} nH" if p.get("l_nh") else "–", e(", ".join(near[:4]) or "none")]
        rows.append("<tr>" + "".join(f"<td class='num'>{c}</td>" if k in (1, 5) else f"<td>{c}</td>"
                                     for k, c in enumerate(cells)) + "</tr>")
    body.append("<h4>ICs</h4>" + _table(["IC", "#Supply pins", "Worst margin", "Held there by", "Highest peak",
                                         "#L above decaps", "Decaps within 9 mm"], rows))
    body.append(_img(figs.png(f"{d.name} PDN", draw_pdn, d, size=(9.5, 4.2)),
                     f"{d.name}: |Z| at each IC's supply pins; the dashed red line is the target"))
    body.append(_responses(f"{d.name} PDN", getattr(d, "responses", ()), figs))
    for n in d.notes:
        body.append(f"<p class='note'>{e(n)}</p>")
    body.append("</article>")
    return "".join(body)


def _dc_load(board, d, issues, rules, figs) -> str:
    """The article of one DC rail with loads (analysis/dcload.py, `d` its DcLoadResult): its current, the worst drop,
    the copper's loss and density, each load's drop (supply and return), the busiest vias, and its map."""
    p = d.dc[0]
    dens_w = getattr(rules, "current_density_warning", 50.0)
    dens_e = getattr(rules, "current_density_error", 100.0)
    limit = d.max_drop * 1e3 if d.max_drop else None

    def level(x, w, err) -> str:
        """A figure's status against its warning and error thresholds."""
        return "error" if x >= err else "warn" if x >= w else "ok"
    worst = max(d.loads, key=lambda r: r["drop_mv"], default=None)
    cards = [_kpi("Current", f"{p['current_a']:.3g} A", f"from {d.driver} to {len(d.loads)} load(s)")]
    if worst is not None:
        cards.append(_kpi("Worst drop", f"{worst['drop_mv']:.1f} mV", f"at {worst['pin']}"
                          + (f" (limit {limit:.0f} mV)" if limit else ""),
                          level(worst["drop_mv"], 0.8 * limit, limit) if limit else ""))
    cards += [_kpi("Copper loss", f"{p['loss_w'] * 1e3:.0f} mW", "in the rail's copper (and its return)"),
              _kpi("Sustained density", f"{p['sustained_a_mm2']:.0f} A/mm²",
                   f"{p['sustained_layer']} @ ({p['sustained_at'][0]:.1f}, {p['sustained_at'][1]:.1f})",
                   level(p["sustained_a_mm2"], dens_w, dens_e))]
    body = [f"<article class='item' id='{_anchor('dcload', d.net)}'><header><h3>{e(d.net)}</h3>"
            f"<span class='what'>DC load, {p['current_a']:.3g} A from {e(d.driver)}</span></header>", _kpis(cards)]
    # the pins' joints and contacts (analysis/pins.py): a column only when some pin has a value
    pins_col = any(r.get("pins_mv") for r in d.loads)
    body.append("<h4>Loads</h4>" + _table(
        ["Load", "Return", "#Current", "#Supply drop", "#Return drop"] + (["#Of which at pins"] if pins_col else [])
        + ["#Total drop"],
        [f"<tr><td>{e(r['pin'])}</td><td>{e(r['ret'] or '–')}</td>{_num(r['current_a'], '{:.3g} A')}"
         f"{_num(r['supply_mv'], '{:.1f} mV')}{_num(r['return_mv'], '{:.1f} mV')}"
         + (_num(r.get('pins_mv', 0.0), '{:.1f} mV') if pins_col else "")
         + f"{_num(r['drop_mv'], '{:.1f} mV')}</tr>" for r in d.loads]))
    vias = sorted(p["vias"], key=lambda x: -x["current_a"])[:8]
    if vias:
        body.append("<h4>Busiest vias</h4>" + _table(
            ["Via", "#Current", "#Plating density", "#Load (IPC-2221)", "Where"],
            [f"<tr><td>{e(x['name'])}</td>{_num(x['current_a'], '{:.2f} A')}{_num(x['a_mm2'], '{:.0f} A/mm²')}"
             + (_num(x['load_pct'], '{:.0f} %') if x.get('load_pct') is not None else "<td class='num'>–</td>")
             + f"<td>({x['x']:.2f}, {x['y']:.2f})</td></tr>" for x in vias]))
    body.append(_img(figs.heat(f"{d.net} DC load", board, d, issues, size=_maps_size(d)),
                     f"{d.net}: DC current density with every load drawing its current"))
    body.append(_notes(d.notes))
    return "".join(body) + "</article>"


def _combined_dc(board, c, issues, rules, figs) -> str:
    """The article of every power stage's DC paths together (power.combine_dc, `c` its result): every stage at its
    own load at once, their current densities added on the one whole-board model; the total input current, the
    copper's loss, the hotspots, the vias against IPC-2221, and the map with its checks' markers."""
    from ..analysis.power import COMBINED_NET, COMBINED_VIEW, dc_view
    v = dc_view(c, COMBINED_VIEW)
    p = c.dc[0]
    dens_w = getattr(rules, "current_density_warning", 50.0)
    dens_e = getattr(rules, "current_density_error", 100.0)
    via_w = getattr(rules, "via_load_warning", 100.0)
    via_e = getattr(rules, "via_load_error", 150.0)

    def level(x, w, err) -> str:
        """A figure's status against its warning and error thresholds."""
        return "error" if x >= err else "warn" if x >= w else "ok"
    vias = p["vias"]
    busiest = max((x for x in vias if x.get("load_pct") is not None), key=lambda x: x["load_pct"], default=None)
    cards = [_kpi("Input current", f"{p['current_a']:.3g} A", "every stage's expected input current, added"),
             _kpi("Copper loss", f"{p['loss_w']:.2f} W", "of the total current in the DC paths' copper"),
             _kpi("Sustained density", f"{p['sustained_a_mm2']:.0f} A/mm²",
                  f"{p['sustained_layer']} @ ({p['sustained_at'][0]:.1f}, {p['sustained_at'][1]:.1f})",
                  level(p["sustained_a_mm2"], dens_w, dens_e)),
             _kpi("Peak density", f"{p['peak_a_mm2']:.0f} A/mm²",
                  f"{p['peak_layer']} @ ({p['peak_at'][0]:.1f}, {p['peak_at'][1]:.1f})")]
    if busiest is not None:
        cards.append(_kpi("Busiest via", f"{busiest['load_pct']:.0f} %",
                          f"of IPC-2221: {busiest['current_a']:.2f} A @ ({busiest['x']:.2f}, {busiest['y']:.2f})",
                          level(busiest["load_pct"], via_w, via_e)))
    body = [f"<article class='item' id='{_anchor('stage', COMBINED_NET)}'><header><h3>All power stages: DC currents "
            f"together</h3><span class='what'>{e(p['loads'])}</span></header>",
            "<p>Every power stage at its own load at once, their DC paths' currents added on one copper model of the "
            "whole board: what the shared copper (the input connector, a surge stopper, the rail, the ground) really "
            "carries. The current-density and via checks run on this total, in place of each stage's own.</p>",
            _kpis(cards)]
    rows = sorted(vias, key=lambda x: -(x.get("load_pct") or 0.0))[:COMBINED_VIAS]
    if rows:
        body.append(f"<h4>Vias on the total current (the {len(rows)} most loaded of {len(vias)})</h4>" + _table(
            ["Via (net @ place, mm)", "#Current", "#Plating density", "#Load (IPC-2221)"],
            [f"<tr><td>{e(x['name'])}</td>{_num(x['current_a'], '{:.2f} A')}{_num(x['a_mm2'], '{:.0f} A/mm²')}"
             + (_num(x['load_pct'], '{:.0f} %') if x.get('load_pct') is not None else "<td class='num'>–</td>")
             + "</tr>" for x in rows], narrow=False))
    body.append(_img(figs.heat(f"{COMBINED_NET} DC together", board, v, issues, size=_maps_size(v)),
                     f"All power stages: DC current density with every stage at its load ({p['loads']})"))
    body.append(_notes(c.notes))
    return "".join(body) + "</article>"


FIX_CHECKS = {"emi_margin": "radiated", "conducted_emission": "conducted", "hot_loop": "ringing"}
_TEXT_CLS = {"error": "bad", "warn": "meh", "ok": "good", "": ""}


def _fixes(rows: list[dict], warn: float) -> str:
    """The fix advisor's design changes for a stage (analysis/fix_advisor.py): the stage now, then each option
    with its parts, the predicted figures, its extra loss and what it clears."""
    def margin(v):
        """A predicted margin for the options table ("–" for none)."""
        return "<td class='num'>–</td>" if v is None else \
            f"<td class='num {_TEXT_CLS[_margin_cls(v, warn)]}'>{v:+.1f} dB</td>"
    out = []
    for o in rows:
        oc = o["outcome"] or {}
        parts = "<br>".join(e(p) for p in o["parts"])
        notes = "".join(f"<div class='note'>{e(n)}</div>" for n in o["notes"])
        clears = ", ".join(FIX_CHECKS.get(c, c) for c in o["clears"]) or ("–" if o["kind"] != "now" else "")
        ov = oc.get("overshoot_v")
        out.append(f"<tr><td><b>{e(o['title'])}</b>{notes}</td>"
                   f"<td>{parts}</td>" + margin(oc.get("radiated_db")) + margin(oc.get("conducted_db"))
                   + (f"<td class='num'>{ov:.1f} V</td>" if ov is not None else "<td class='num'>–</td>")
                   + f"<td class='num'>{(oc.get('loss_w') or 0) * 1e3:.0f} mW</td><td>{e(clears)}</td></tr>")
    return ("<h4>How to fix it</h4><p class='note'>Design changes evaluated on this stage's own solves (the loop, "
            "its moment and the input path stay; the filter is a two-port estimate at the connector). Check a "
            "snubber or an edge change with Compare what-if before relying on it.</p>"
            + _table(["Change", "Parts", "#Radiated", "#Conducted", "#Overshoot", "#Extra loss", "Clears"], out,
                     sort=False))


# --------------------------------------------------------------------------------------------- motor bridges
def _bridge(board, b, legs, issues, warn, figs) -> str:
    """One motor bridge's article: the drive and its currents, the legs' hot loops, the capacitors' ripple, the
    conducted estimate, losses, RMS current density, the sweep and the victims."""
    from ..analysis.bridge import rms_view, sweep_view, victim_text
    head = f"<article class='item' id='{_anchor('bridge', b.net)}'><header><h3>{e(b.net)}</h3>"
    if b.error:
        return head + f"</header><p class='bad'>{e(b.error)}</p></article>"
    op, d, wl = b.operating, b.drive, b.worst_leg
    head += (f"<span class='what'>six-step {e(op['pwm'])}, {op['v_bus']:g} V, {op['f_pwm'] / 1e3:g} kHz, "
             f"{e(op['mode'])} {op['i_reached']:.4g} A at {op['speed_rpm']:g} rpm</span></header>")
    cards = [_kpi("Drive", f"duty {op['duty']:.3f}", f"f_e {op['f_e']:.4g} Hz" + ("" if op["reached"]
                                                                                 else ", current not reached"),
                  "" if op["reached"] else "warn"),
             _kpi("Phase current", f"{max(d['phase_rms']):.4g} A RMS", f"peak {max(d['phase_peak']):.4g} A"),
             _kpi("Bus", f"{d['bus_mean']:.4g} A mean", f"{d['bus_ac_rms']:.4g} A RMS ripple")]
    if wl.get("overshoot_v") is not None:
        frac = wl["overshoot_v"] / op["v_bus"]
        cards.append(_kpi("L·di/dt", f"{wl['overshoot_v']:.1f} V", f"{frac * 100:.0f} % of the bus, "
                          f"{wl['overshoot_leg']}", "error" if frac >= 0.5 else "warn" if frac >= 0.25 else "ok"))
    if b.conducted_worst:
        w = b.conducted_worst
        cards.append(_kpi("Conducted", f"{w['margin_db']:+.1f} dB", f"{w['v_dbuv']:.1f} dBµV @ {w['f'] / 1e6:.4g} MHz",
                          _margin_cls(w["margin_db"], warn)))
    cards += _ce101_card(b, warn)
    if wl.get("radiated"):
        m = wl["radiated"]["margin_db"]
        cards.append(_kpi("Radiated", f"{m:+.1f} dB", wl["radiated_leg"], _margin_cls(m, warn)))
    if b.losses:
        cards.append(_kpi("Part losses", f"{sum(x['power_w'] for x in b.losses):.1f} W",
                          f"most in {b.losses[0]['ref']} ({b.losses[0]['power_w']:.2f} W)"))
    r = b.rms
    if r.get("sustained_layer"):
        cards.append(_kpi("RMS density", f"{r['sustained_a_mm2']:.0f} A/mm²",
                          f"sustained on {r['sustained_layer']}; copper {r['copper_w']:.1f} W"))
    body = [head, _kpis(cards)]
    body.append(_img(figs.heat(f"{b.net} drive", board, b, issues, size=(10.5, 6.4)),
                     f"{b.net}: phase and bus currents over one electrical period, and a commutation"))
    if b.caps:
        body.append(_img(figs.png(f"{b.net} capacitors", draw_bridge_caps, b, size=(9.5, 3.8)),
                         "Bus capacitors' ripple current, and the victims against their thresholds"))
    else:
        figs.done += 1
    body.append(_img(figs.png(f"{b.net} conducted", draw_spectrum, b, size=(9.5, 3.8)),
                     "Conducted emission at the input, all legs, read by a simulated receiver"))
    if legs:
        body.append("<h4>Legs</h4>" + _table(
            ["Leg", "Switches", "#Hot loop", "#L·di/dt", "#Ringing", "#Radiated margin"],
            [f"<tr><td>{e(c.net)}</td><td>{e(c.driver)} → {e(c.receiver)}</td>{_num(c.loop_nh, '{:.2f} nH')}"
             f"{_num(c.overshoot_v, '{:.1f} V')}{_num((c.ringing or {}).get('f', 0) / 1e6 or None, '{:.0f} MHz')}"
             f"{_margin_td(c.worst['margin_db'] if c.worst else None, warn)}</tr>" for c in legs if not c.error]))
        for c in legs:
            if not c.error:
                body.append(_img(figs.heat(f"{c.net} hot loop", board, c, issues, size=_maps_size(c)),
                                 f"{c.net}: hot-loop current density at {c.loop_f / 1e6:g} MHz"))
    if b.losses:
        body.append("<h4>Parts' losses at the operating point</h4>" + _table(
            ["Part", "#Loss", "What"],
            [f"<tr><td>{e(x['ref'])}</td>{_num(x['power_w'], '{:.3g} W')}<td>{e(x['what'])}</td></tr>"
             for x in b.losses], paged=True))
    rv = rms_view(b, legs[0]) if legs else None
    if rv is not None:
        body.append(_img(figs.heat(f"{b.net} RMS currents", board, rv, issues, size=_maps_size(rv)),
                         f"{b.net}: RMS current density from the battery to the motor"))
        rows = [f"<tr><td>{e(v['name'])}</td>{_num(v['current_a'], '{:.2f} A')}"
                f"{_num(v.get('load_pct'), '{:.0f} %')}</tr>" for v in r.get("vias", [])]
        rows += [f"<tr><td>{e(p['name'])} (series part)</td>{_num(p['rms_a'], '{:.3g} A')}<td class='num'>–</td></tr>"
                 for p in r.get("parts", [])]
        if rows:
            body.append("<h4>Busiest vias and series parts (RMS)</h4>"
                        + _table(["Item", "#RMS current", "#Of its IPC-2221 rating"], rows, paged=True))
    else:
        figs.done += 1
    if b.sweep:
        sv = sweep_view(b)
        body.append("<h4>Operating-point sweep</h4>")
        body.append(_img(figs.heat(f"{b.net} sweep", board, sv, issues, size=(9.5, 6.2)),
                         "Losses, RMS density and margins against the phase current, one line per speed "
                         "(hollow: the current is not reached)"))
        rows = []
        for s in b.sweep:
            if s.get("error"):
                rows.append(f"<tr><td class='num'>{s['i_phase']:g}</td><td class='num'>{s['speed_rpm']:g}</td>"
                            f"<td colspan='8'>{e(s['error'])}</td></tr>")
                continue
            rows.append(f"<tr>{_num(s['i_phase'], '{:g} A')}{_num(s['speed_rpm'], '{:g}')}"
                        f"<td class='num'>{s['duty']:.3f}{'' if s['reached'] else NOT_REACHED}</td>"
                        f"{_num(s['loss_w'], '{:.1f} W')}<td>{e(s['worst_part'])} ({s['worst_part_w']:.2f} W)</td>"
                        f"{_num(s['copper_w'], '{:.1f} W')}{_num(s['sustained_a_mm2'], '{:.0f}')}"
                        f"{_num(s['cap_rms'], '{:.2f} A')}{_margin_td(s.get('conducted_margin_db'), warn)}"
                        f"{_margin_td(s.get('radiated_margin_db'), warn)}</tr>")
        body.append(_table(["#Phase current", "#Speed (rpm)", "#Duty", "#Part losses", "Most loss", "#Copper",
                            "#A/mm² RMS", "#Busiest cap", "#Conducted", "#Radiated"], rows))
    if b.victims:
        body.append("<h4>Victims at the switching edges</h4>" + _table(
            ["Finding", "#Peak", "#Threshold"], [_victim_row(v, victim_text) for v in b.victims], paged=True))
    body.append(_notes(b.notes))
    return "".join(body) + "</article>"


# --------------------------------------------------------------------------------------------- thermal
def _thermal(board, thermal, capacity, issues, figs) -> str:
    """The thermal part: the copper temperature maps, the hottest parts, and each stage's current capacity."""
    from ..analysis.thermal import capacity_text
    body = []
    if thermal is not None:
        t = thermal
        if t.error:
            body.append(f"<p class='bad'>{e(t.error)}</p>")
        else:
            st = "error" if t.t_max >= t.max_c else "warn" if t.t_max >= t.warn_c else "ok"
            body.append(_kpis([_kpi("Hottest copper", f"{t.t_max:.1f} °C", f"{t.hotspot_layer}, ambient "
                                    f"{t.ambient_c:g} °C", st),
                               _kpi("Total heat", f"{t.power_w:.2f} W", f"copper {t.copper_w:.2f} W, parts "
                                    f"{t.parts_w:.2f} W"),
                               _kpi("Limits", f"{t.warn_c:g} / {t.max_c:g} °C", "warning / maximum")]))
            body.append(_img(figs.heat("temperatures", board, t, issues, size=_maps_size(t)),
                             "Copper temperature per layer (× the hot spot)"))
            body.append(_img(figs.png("part temperatures", draw_spectrum, t, size=(9.5, 3.6)),
                             "The hottest parts against the limits"))
            if t.parts:
                # the junction column only where a θJB gives one (without, every row read "–"); the hottest 25
                shown = t.parts[:25]
                junction = any(p.get("t_junction") is not None for p in shown)
                body.append(f"<h4>Parts (the {len(shown)} hottest of {len(t.parts)})</h4>" + _table(
                    ["Part", "#Board under it"] + (["#Junction"] if junction else []) + ["#Loss", "Why"],
                    [f"<tr><td>{e(p['ref'])}</td>{_num(p['t_c'], '{:.1f} °C')}"
                     + (_num(p.get('t_junction'), '{:.1f} °C') if junction else "")
                     + f"{_num(p.get('power_w'), '{:.2f} W')}<td>{e(p.get('why', ''))}</td></tr>" for p in shown]))
            body.append(_notes(t.notes))
    if capacity:
        rows = []
        for c in capacity:
            cap = c.capacity
            rows.append(f"<tr><td>{e(cap.get('stage', ''))}</td><td class='num'>{e(capacity_text(cap))}</td>"
                        f"{_num(cap.get('i_nominal'), '{:g} A')}<td>{e(cap.get('limited_by', ''))}</td>"
                        f"{_num(cap.get('t_hottest'), '{:.1f} °C')}</tr>")
        body.append("<h4>Current capacity</h4>" + _table(
            ["Stage", "#Capacity", "#Operating point", "Limited by", "#Hottest"], rows))
        for c in capacity:
            body.append(_img(figs.heat(f"capacity {c.capacity.get('stage', '')}", board, c, issues,
                                      size=_maps_size(c)),
                             f"{c.capacity.get('stage', '')} at its capacity ({capacity_text(c.capacity)})"))
    return "".join(body)


# --------------------------------------------------------------------------------------------- fixes, appendix
def _suggestions_section(res) -> str:
    """The fix search part: every suggestion with its predicted effect, and the verification's comparison."""
    rows = []
    for sug in res.suggestions:
        at = f" @ {sug.f_eval / 1e6:.4g} MHz" if sug.f_eval else ""
        pred = (f"{sug.delta_db:+.1f} dB{at} / L {sug.delta_l * 100:+.0f} %" if sug.delta_db is not None
                else e(sug.note or "–"))
        if sug.delta_db is not None and sug.note:
            pred += f"<br><span class='note'>{e(sug.note)}</span>"
        rows.append(f"<tr class='{'' if sug.selected else 'waived'}'><td>{e(sug.id)}"
                    f"{SELECTED if sug.selected else ''}</td>"
                    f"<td>{e(sug.kind)}</td><td>{e(sug.net)}</td><td><b>{e(sug.title)}</b><br><span class='note'>"
                    f"{e(sug.message)}</span></td><td class='num'>{sug.location[0]:.2f}, {sug.location[1]:.2f}</td>"
                    f"<td data-v='{sug.delta_db if sug.delta_db is not None else 0}'>{pred}</td>"
                    f"<td>{e(sug.issue.check) if sug.issue else ''}</td></tr>")
    out = ["<p class='lead'>Selected fixes were verified together by simulating a copy of the board with them. "
           "Predicted: the change of the loop moment at the worst harmonic (plane capacitance included) and of the "
           "loop inductance at the fundamental.</p>",
           _table(["Id", "Kind", "Net", "Fix", "#Location (mm)", "Predicted", "For issue"], rows)
           if rows else "<p class='empty'>No suggestions.</p>"]
    if res.after is not None:
        def pair(v, fmt):
            """A (before, after) pair as 'a → b', '–' for a missing side."""
            a, b = v
            return f"{fmt.format(a) if a is not None else '–'} → {fmt.format(b) if b is not None else '–'}"
        out.append("<h4>Before → after</h4>" + _table(
            ["Net", "L / ideal or hot loop", "Worst margin", "Plane resonance"],
            [f"<tr><td>{e(r['net'])} <span class='note'>{e(r['route'])}</span></td>"
             f"<td>{pair(r['loop_nh'], '{:.2f} nH') if r.get('kind') == 'converter' else pair(r['l_ratio'], '{:.2f}')}</td>"
             f"<td>{pair(r['worst_margin_db'], '{:+.1f} dB')}</td><td>{pair(r['resonance_db'], '{:+.1f} dB')}</td></tr>"
             for r in res.comparison]))
        out.append(f"<p>Fixed: {len(res.fixed)} issue(s). New: {len(res.new)} issue(s).</p>")
        if res.kept:
            from .formats import KEPT_TEXT
            out.append(f"<p class='note'>{e(KEPT_TEXT + ', '.join(res.kept))}</p>")
    return "".join(out)


def _appendix(config, board=None) -> str:
    """The appendix: how the run was set up (limits, distances, standards, the band, solver settings), the board's
    quasi-static limit [A47] and the method's limits, so a reader knows what the figures are."""
    from ..components.values import format_si
    from ..emi.conducted import describe
    a = getattr(config, "analysis", None)
    rows = []
    qs = None
    if board is not None:
        from ..analysis.simulate import quasi_static_limit
        qs = quasi_static_limit(board)
    if a is not None:
        for k, v in (("Grid", f"{a.grid_mm} mm" if a.grid_mm != "auto" else "automatic"),
                     ("Resolution", f"{a.resolution:g}× the nodes (cells 1/√ of it)"),
                     ("Node budget per model", f"{int(a.max_nodes * a.resolution):,}"),
                     ("Plane capacitance", "on" if a.plane_capacitance
                                                                     else "off"),
                     ("Radiated limit", a.limit_text()),
                     ("Conducted standard", describe(a.conducted, a.conducted_class) if a.conducted != "none"
                      else "none"),
                     ("Radiated: measuring distance", f"{a.distance_m:g} m" if a.distance_m else "the limit's own "
                      "(CISPR 32: 10 m below 1 GHz, 3 m above)"),
                     ("Radiated: ground reflection", "on (a test site's ground plane)" if a.ground_reflection
                      else "off (free space)"),
                     ("Highest frequency evaluated", format_si(a.max_frequency, "Hz") if a.max_frequency
                      else "automatic: a decade above each edge's corner, 10/(π·t_edge)"),
                     ("Harmonics solved per route", str(a.max_solves))):
            rows.append(f"<tr><td>{e(k)}</td><td>{e(str(v))}</td></tr>")
    if qs:
        f_qs = format_si(float(f"{qs['f']:.3g}"), "Hz")
        rows.append(f"<tr><td>Quasi-static up to</td><td>{e(f_qs)} (the board's "
                    f"{qs['size_mm']:.0f} mm in εr {qs['epsilon_r']:.2g} is a quarter wavelength there; above it the "
                    "board-wide model's emissions are indicative upper bounds)</td></tr>")
    return ("<div class='appendix'><h4>How the results are computed</h4><ul>"
            "<li><b>Rule checks</b> follow each net from its driver to its receivers and check every segment against "
            "the planes next to it (gaps, splits, plane edges, layer changes without a stitching via or a "
            "capacitor).</li>"
            "<li><b>The field solver</b> rasterises the copper of every layer and solves the currents with a "
            "quasi-static PEEC model: resistive and inductive plane strips, mutual inductance between all layers, "
            "vias and parts as branches, and the plane pairs' capacitance from the stackup. Its validation against "
            "analytic results (image theory, Wheeler's formula, plate capacitance and cavity resonance) is part of "
            "the tool's tests.</li>"
            "<li><b>Power stages and bridges</b> are solved as known currents through the switches, the capacitors "
            "as branches with their C, ESR and ESL, and a LISN at the input for the conducted emission. A bridge's "
            "drive is a six-step model of the motor (closed-form intervals, steady state), its spectrum exact for "
            "the piecewise-linear currents, read by a simulated EMI receiver.</li></ul>"
            "<h4>Limits</h4><ul>"
            "<li>Emission figures are for ranking and comparing layouts, not a compliance prediction: the model is "
            "quasi-static, it estimates the differential-mode loop of each net or hot loop, and common-mode currents "
            "on cables, which often dominate, are not modelled.</li>"
            "<li>Plane resonance peaks are upper estimates (no radiation loss); a margin set by a resonance is good "
            "to a few dB.</li>"
            "<li>The edge time sets the high-frequency content and L·di/dt; currents, frequencies and part models "
            "are as entered in the settings.</li>"
            "<li>Temperatures are steady state; heat leaves through the board's faces only.</li></ul>"
            + ("<h4>Analysis settings</h4>" + _table(["Setting", "Value"], rows, sort=False) if rows else "")
            + "</div>")
