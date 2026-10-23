"""The plugin's help (help.html next to this file): a window of its own, opened at the topic of the tab shown (the
top bar's Help button, F1). Links within the page jump to their section; others open in the web browser.

Two ways to show it. Where a browser engine is available (wx.html2.WebView: Edge WebView2 on Windows, WebKit on
macOS; KiCad 10's wxPython has both) the page is `modern_page()`: help.html's text as one card per section in
help_shell.html, with a sidebar of the sections that marks the one in view, a search box (Ctrl+F) and the theme's
colours (the user: "can we make this help look more modern and better formatted?"). Otherwise, or with
GROUNDTRUTH_HELP_CLASSIC set, wx's own simple HTML view shows `page()` as before. help.html stays written for both:
plain HTML that wx's renderer understands, which modern_page turns into sections.
"""
from __future__ import annotations

import functools
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import wx
import wx.html

from . import theme

PAGE = Path(__file__).with_name("help.html")
SHELL = Path(__file__).with_name("help_shell.html")
# docs/REFERENCES.md (the sources of the formulas and the model's assumptions): next to the groundtruth package in an
# installed plugin (tools/make_pcm_package.py, tools/install_plugin.py copy it to <plugin>/docs), in the repo's docs
REFERENCES = (Path(__file__).resolve().parents[2] / "docs" / "REFERENCES.md",
              Path(__file__).resolve().parents[3] / "docs" / "REFERENCES.md")
TOPICS = ("workflow", "together", "checks", "nets", "converters", "bridges", "dcloads", "pdn", "si", "safety", "parts",
          "fields", "cbias", "issues", "results", "fixes", "history", "thermal", "analog", "prefs", "export", "drawing", "labels",
          "siglabels", "configs", "settings", "speed", "methods", "limits", "references", "licenses", "server")
# the sidebar's groups, by section; a section not listed goes under "More"
GROUPS = (("Getting started", ("workflow", "together", "checks")),
          ("The tabs", ("nets", "converters", "bridges", "dcloads", "pdn", "si", "parts", "issues", "results", "fixes",
                        "history")),
          ("Analyses and output", ("fields", "cbias", "safety", "thermal", "analog", "prefs", "export", "drawing")),
          ("Labels and settings", ("labels", "siglabels", "configs", "settings")),
          ("Under the hood", ("speed", "server", "methods", "limits", "references", "licenses")))


def page() -> str:
    """The help page in the window's theme colours, with the schematic-field table from components/fields.py and the
    references from docs/REFERENCES.md (wx's simple HTML view)."""
    pal = theme.palette()
    text = PAGE.read_text(encoding="utf-8")
    for key, value in (("@BG@", pal.bg), ("@TEXT@", pal.text), ("@LINK@", pal.accent), ("@WARN@", pal.warn),
                       ("@PART_FIELDS@", field_rows()), ("@REFERENCES@", references_html()),
                       ("@CBIAS_CHART@", cbias_table()), ("@CHECK_BASIS@", check_basis_html())):          # wx's simple view draws no SVG: the numbers
        text = text.replace(key, value)
    return text


# The Cbias section's worked example (the user: "draw a chart with a fake curve and place 3-4 points that are used
# for approximation. create the string that would go into the HTTP lib's parameter and draw some line in between that
# show how the plugin would approximate it. Also mention what the min/max number of samples can be"): a made-up
# datasheet curve of a 100 nF 100 V X7R with the shape of the one the user showed (−20 % at 20 V, −50 % at 40 V,
# −85 % at 100 V, a little rise near 0 V), and the voltages its points are read at
CBIAS_C0 = 100e-9
CBIAS_AT = (10.0, 30.0, 50.0, 80.0)
CBIAS_SPAN = (0.0, 100.0)               # the chart's volts


def cbias_datasheet(v: float) -> float:
    """The made-up datasheet curve: C / C0 at v volts (a small rise to ~+1 % at 3 V, then the fall)."""
    import math
    return (1 + 0.012 * (v / 3) * math.exp(1 - v / 3)) / (1 + (v / 40) ** 2.05)


def cbias_example() -> tuple[str, str, list]:
    """The worked example: (the Cbias Curve text for the HTTP lib, the same in percent, the curve as [(V, C)]), its
    points the datasheet curve at CBIAS_AT rounded as one would read them (2 digits)."""
    from ..components.bias import bias_text, parse_bias
    pts = [(v, float(f"{CBIAS_C0 * cbias_datasheet(v):.2g}")) for v in CBIAS_AT]
    text = ", ".join(f"{c * 1e9:g}nF@{v:g}V" for v, c in pts)
    pct = ", ".join(f"{100 * (c / CBIAS_C0 - 1):+.0f}%@{v:g}V" for v, c in pts)
    points, _ = parse_bias(text)
    return bias_text(points), pct, pts


def cbias_svg() -> str:
    """The worked example as an inline SVG chart for the browser view: ΔC (%) against DC volts, the datasheet curve
    (grey), its points (dots), GroundTruth's curve through them (components/bias.c_at: solid up to the last point,
    dashed above it), the one-point curve from 50 V alone (dotted); the colours the page's CSS variables (light and
    dark themes). Then the HTTP lib text and the limits."""
    from ..components.bias import BIAS_POINTS_MAX, c_at
    text, pct, pts = cbias_example()
    w, h, left, right, top, bottom = 600, 320, 52, 16, 14, 40
    x = lambda v: left + (v - CBIAS_SPAN[0]) / (CBIAS_SPAN[1] - CBIAS_SPAN[0]) * (w - left - right)   # noqa: E731
    y = lambda d: top + (10 - d) / 110 * (h - top - bottom)                                         # noqa: E731
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px" role="img" '
           'aria-label="A DC-bias curve and how GroundTruth approximates it">']
    for d in range(-100, 11, 20):           # the grid and the axes' labels
        out.append(f'<line x1="{left}" x2="{w - right}" y1="{y(d):.1f}" y2="{y(d):.1f}" '
                   'style="stroke:var(--border);stroke-width:1"/>')
        out.append(f'<text x="{left - 6}" y="{y(d) + 4:.1f}" text-anchor="end" font-size="11" '
                   f'style="fill:var(--muted)">{d:+d}%</text>' if d else
                   f'<text x="{left - 6}" y="{y(d) + 4:.1f}" text-anchor="end" font-size="11" '
                   'style="fill:var(--muted)">0%</text>')
    for v in range(0, 101, 20):
        out.append(f'<line x1="{x(v):.1f}" x2="{x(v):.1f}" y1="{top}" y2="{h - bottom}" '
                   'style="stroke:var(--border);stroke-width:1"/>')
        out.append(f'<text x="{x(v):.1f}" y="{h - bottom + 16}" text-anchor="middle" font-size="11" '
                   f'style="fill:var(--muted)">{v} V</text>')
    out.append(f'<text x="{(left + w - right) / 2:.0f}" y="{h - 4}" text-anchor="middle" font-size="12" '
               'style="fill:var(--text)">DC voltage across the capacitor</text>')

    def line(vs, f, style, ident):
        """A polyline of ΔC % = 100 (f(v) − 1) over vs."""
        p = " ".join(f"{x(v):.2f},{y(100 * (f(v) - 1)):.2f}" for v in vs)
        return f'<polyline id="{ident}" fill="none" points="{p}" style="{style}"/>'
    n = 200
    grid = [CBIAS_SPAN[0] + (CBIAS_SPAN[1] - CBIAS_SPAN[0]) * k / n for k in range(n + 1)]
    v_last = pts[-1][0]
    below = sorted({*(v for v in grid if v <= v_last), *(p[0] for p in pts)})   # the points are vertices
    above = [v_last] + [v for v in grid if v > v_last]
    fit = lambda v: c_at(pts, CBIAS_C0, v) / CBIAS_C0                                               # noqa: E731
    out.append(line(grid, cbias_datasheet, "stroke:var(--muted);stroke-width:5;opacity:0.35", "cbias-datasheet"))
    out.append(line(grid, lambda v: c_at([pts[2]], CBIAS_C0, v) / CBIAS_C0,
                    "stroke:var(--text);stroke-width:1.2;stroke-dasharray:2 3", "cbias-one-point"))
    out.append(line(below, fit, "stroke:var(--accent);stroke-width:2", "cbias-fit"))
    out.append(line(above, fit, "stroke:var(--accent);stroke-width:2;stroke-dasharray:7 4", "cbias-tail"))
    for v, c in pts:
        out.append(f'<circle cx="{x(v):.2f}" cy="{y(100 * (c / CBIAS_C0 - 1)):.2f}" r="4.5" data-v="{v:g}" '
                   f'data-c="{c:.4g}" style="fill:var(--accent);stroke:var(--card);stroke-width:1.5"/>')
    keys = [("stroke:var(--muted);stroke-width:5;opacity:0.35", "datasheet (made up)"),
            ("stroke:var(--accent);stroke-width:2", "GroundTruth through the points"),
            ("stroke:var(--accent);stroke-width:2;stroke-dasharray:7 4", "above the last point"),
            ("stroke:var(--text);stroke-width:1.2;stroke-dasharray:2 3", "one point (50 V) alone")]
    for k, (style, label) in enumerate(keys):
        yy = top + 12 + 16 * k
        out.append(f'<line x1="{w - right - 190}" x2="{w - right - 166}" y1="{yy}" y2="{yy}" style="{style}"/>')
        out.append(f'<text x="{w - right - 160}" y="{yy + 4}" font-size="11" style="fill:var(--text)">{label}</text>')
    out.append("</svg>")
    return ("".join(out) + f"<p>Cbias Curve for this part (HTTP lib text parameter): <code>{text}</code><br>"
            f"or as the change from the nominal: <code>{pct}</code><br>Points: at least 1 (one point: the formula), "
            f"two or more interpolated, at most {BIAS_POINTS_MAX}.</p>")


def cbias_table() -> str:
    """The worked example for wx's simple HTML view (no SVG): the points with the HTTP lib text, then the datasheet
    and GroundTruth's curve side by side every 10 V (the one-point curve beside them)."""
    from ..components.bias import BIAS_POINTS_MAX, c_at
    text, pct, pts = cbias_example()
    rows = ["<tr><th>DC voltage</th><th>datasheet (made up)</th><th>GroundTruth</th><th>one point (50 V)</th></tr>"]
    for v in range(0, 101, 10):
        rows.append(f"<tr><td>{v} V</td><td>{100 * (cbias_datasheet(v) - 1):+.0f} %</td>"
                    f"<td>{100 * (c_at(pts, CBIAS_C0, v) / CBIAS_C0 - 1):+.0f} %"
                    + (" (a point)" if any(p[0] == v for p in pts) else "") + "</td>"
                    f"<td>{100 * (c_at([pts[2]], CBIAS_C0, v) / CBIAS_C0 - 1):+.0f} %</td></tr>")
    return ("<table border='1' cellpadding='3' cellspacing='0'>" + "".join(rows) + "</table>"
            f"<p>Cbias Curve for this part (HTTP lib text parameter): <code>{text}</code><br>or as the change from "
            f"the nominal: <code>{pct}</code><br>Points: at least 1 (one point: the formula), two or more "
            f"interpolated, at most {BIAS_POINTS_MAX}.</p>")


def references_path() -> Optional[Path]:
    """docs/REFERENCES.md where it is (installed plugin or repo), or None."""
    return next((p for p in REFERENCES if p.is_file()), None)


def references_html() -> str:
    """docs/REFERENCES.md as HTML for the help's References section (its own title left out: the section has one),
    with a link that opens the file itself; a note when the file is not there."""
    path = references_path()
    if path is None:
        return "<p>The list (docs/REFERENCES.md) is not installed with this copy of the plugin.</p>"
    body = re.sub(r"\A#\s[^\n]*\n", "", path.read_text(encoding="utf-8"))
    return markdown_html(body, anchors=True) + \
        f'<p><a href="{path.as_uri()}">Open REFERENCES.md</a> ({escape_html(str(path))})</p>'


@functools.lru_cache(maxsize=1)
def _reference_rows() -> dict:
    """{key: its second column} of every table row in docs/REFERENCES.md (the source, or the assumption's text);
    {} when the file is not installed. Read once."""
    path = references_path()
    if path is None:
        return {}
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
        if len(cells) >= 2 and cells[0] and not re.fullmatch(r":?-+:?", cells[0]):
            rows[re.sub(r"[`*]", "", cells[0])] = cells[1]
    return rows


def reference_entry(key: str, limit: int = 220) -> str:
    """A REFERENCES.md entry's text as plain text (Markdown marks taken out), cut to `limit` characters; "" when the
    key is not there."""
    text = re.sub(r"[`*]|\[([^\]]+)\]\([^)]*\)", lambda m: m.group(1) or "", _reference_rows().get(key, ""))
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def check_basis_html() -> str:
    """The Run checks' bases as Preferences shows them (prefs_dialog.CHECK_BASIS), for the help's Preferences
    section: "Gaps and splits: Johnson 1993, A26; ..." with each key a link to its References row."""
    from .prefs_dialog import CHECK_BASIS, CHECK_TOGGLES
    items = []
    for key, label, _ids, _tip in CHECK_TOGGLES:
        refs = CHECK_BASIS.get(key, ())
        if refs:
            items.append(f"<b>{escape_html(label)}</b>: " +
                         ", ".join(f'<a href="#{ref_anchor(r)}">{escape_html(r)}</a>' for r in refs))
    return "; ".join(items)


def escape_html(text: str) -> str:
    """Text for HTML: &, < and > escaped."""
    from html import escape
    return escape(text, quote=False)


def _inline(text: str) -> str:
    """One line's Markdown inline marks as HTML: `code` (taken out first, so nothing inside it is read as a mark),
    **bold**, *italic*, [text](url)."""
    codes: list[str] = []

    def keep(m) -> str:
        """A code span, set aside behind a placeholder."""
        codes.append(f"<code>{escape_html(m.group(1))}</code>")
        return f"\0{len(codes) - 1}\0"
    text = escape_html(re.sub(r"`([^`]+)`", keep, text))
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return re.sub(r"\0(\d+)\0", lambda m: codes[int(m.group(1))], text)


def ref_anchor(key: str) -> str:
    """The help's anchor of a REFERENCES.md entry ("IPC-2221B" -> "ref-IPC-2221B", "Johnson 1993" ->
    "ref-Johnson-1993", "A26" -> "ref-A26"): links to it open the References section at that row."""
    return "ref-" + re.sub(r"[^A-Za-z0-9]+", "-", key.strip()).strip("-")


ANCHOR = re.compile(r"ref-[A-Za-z0-9-]+")       # the anchors HelpWindow.show takes besides the TOPICS


def markdown_html(text: str, anchors: bool = False) -> str:
    """The Markdown the docs use, as plain HTML both help views show: headings (## → h4, ### → h5), paragraphs,
    bullet lists (a line starting "- ", continued by indented lines) and pipe tables (the first row a header, the
    second the |---| rule). anchors: each table row's first cell (a REFERENCES.md key) gets the anchor ref_anchor
    gives it (`name` for wx's simple view, `id` for the browser engine)."""
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"(#{2,6})\s+(.*)", line)
        if m:
            level = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):          # the header's rule row
                    rows.append(cells)
                i += 1
            html = ["<table border='1' cellpadding='3' cellspacing='0'>"]
            for k, cells in enumerate(rows):
                tag = "th" if k == 0 else "td"
                mark = ""
                if anchors and k and cells and cells[0]:
                    a = ref_anchor(re.sub(r"[`*]", "", cells[0]))
                    mark = f'<a name="{a}" id="{a}"></a>'
                html.append("<tr>" + "".join(f"<{tag} valign='top'>{mark if j == 0 else ''}{_inline(c)}</{tag}>"
                                             for j, c in enumerate(cells)) + "</tr>")
            out.append("\n".join(html) + "</table>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and (lines[i].startswith("- ") or (items and lines[i].startswith("  "))):
                if lines[i].startswith("- "):
                    items.append(lines[i][2:].strip())
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + "</ul>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"#{2,6}\s|\||- ", lines[i]):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


def field_rows() -> str:
    """The table rows of the schematic fields the plugin reads (one per components/fields.PARAMS entry)."""
    from html import escape
    from ..components.fields import PARAMS
    from ..components.values import format_si
    rows = []
    for p in PARAMS:
        sym = {"ohm": "Ω"}.get(p.quantity, p.quantity)
        span = "text" if p.quantity == "text" else f"{format_si(p.lo, sym)} … {format_si(p.hi, sym)}"
        cells = (f"<b>{escape(p.field)}</b>", p.parts, p.keys, p.unit, span, f"<code>{escape(p.example)}</code>",
                 p.feeds)
        rows.append("<tr>" + "".join(f"<td valign='top'>{c}</td>" for c in cells) + "</tr>")
    return "\n".join(rows)


def _rgba(hex_: str, alpha: float) -> str:
    """A palette colour with transparency, for CSS."""
    r, g, b = (round(c * 255) for c in theme.rgb(hex_)[:3])          # theme.rgb is 0..1 (matplotlib's)
    return f"rgba({r}, {g}, {b}, {alpha})"


def css_vars(pal=None) -> str:
    """The shell's colours (CSS custom properties) from the plugin's light or dark palette."""
    p = pal or theme.palette()
    extra = {"accent-soft": _rgba(p.accent, 0.16), "hover": _rgba(p.text, 0.06), "zebra": _rgba(p.text, 0.025),
             "code": _rgba(p.text, 0.06), "mark": _rgba(p.warn, 0.45 if p.dark else 0.55),
             "shadow": "rgba(0, 0, 0, 0.35)" if p.dark else "rgba(15, 23, 42, 0.07)"}
    base = {"bg": p.bg, "panel": p.panel, "card": p.card, "border": p.border, "text": p.text, "muted": p.muted,
            "accent": p.accent, "warn": p.warn}
    return "\n".join(f"  --{k}: {v};" for k, v in {**base, **extra}.items())


def sections() -> tuple[str, list[tuple[str, str, str]]]:
    """help.html as (its introduction, [(anchor, title, body)] per section): the page's parts between its <hr>
    lines, each starting with `<a name="X"></a><h3>Title</h3>`; the contents list is left out (the sidebar takes
    its place)."""
    text = PAGE.read_text(encoding="utf-8").replace("@PART_FIELDS@", field_rows()).replace("@REFERENCES@",
                                                                                         references_html())
    text = text.replace("@CBIAS_CHART@", cbias_svg())          # the browser view draws the chart
    text = text.replace("@CHECK_BASIS@", check_basis_html())
    body = re.search(r"<body[^>]*>(.*)</body>", text, re.S).group(1)
    head, _, rest = body.partition("<hr>")
    intro = re.sub(r"<p><b>Contents</b></p>.*$", "", head, flags=re.S)          # the old contents list
    intro = re.sub(r"<a name=\"top\"></a>|<h2>.*?</h2>", "", intro, flags=re.S).strip()
    out: list[tuple[str, str, str]] = []
    for chunk in rest.split("<hr>"):
        m = re.match(r'\s*<a name="([^"]+)"></a>\s*<h3>(.*?)</h3>(.*)$', chunk, re.S)
        if m:
            out.append((m.group(1), m.group(2).strip(), m.group(3)))
        elif out:                                   # an <hr> inside a section: it stays with that section
            a, t, b = out[-1]
            out[-1] = (a, t, b + chunk)
    return intro, out


def _modern_html(fragment: str) -> str:
    """help.html's markup for the browser engine: anchors as ids, the orange marks as styled spans, wx's table
    attributes left to the style sheet."""
    fragment = re.sub(r'<a name="([^"]+)"></a>', r'<a id="\1"></a>', fragment)
    fragment = re.sub(r'<font color="@WARN@">(.*?)</font>', r'<span class="warn">\1</span>', fragment, flags=re.S)
    fragment = re.sub(r"<table[^>]*>", "<table>", fragment)
    return re.sub(r"\s(?:valign|align)='?\"?[a-z]+'?\"?", "", fragment)


def nav_html(found: list[tuple[str, str, str]]) -> str:
    """The sidebar: the sections under their GROUPS, in the page's order within a group."""
    titles = {a: re.sub(r"<[^>]+>", "", t) for a, t, _ in found}
    seen, lines = set(), []
    for group, ids in GROUPS + (("More", tuple(a for a, _, _ in found)),):
        items = [a for a in ids if a in titles and a not in seen]
        if not items:
            continue
        lines.append(f'<div class="group">{group}</div>')
        for a in items:
            seen.add(a)
            lines.append(f'<a href="#{a}">{titles[a]}</a>')
    return "\n".join(lines)


def modern_page(pal=None) -> str:
    """The help for the browser engine: help_shell.html with help.html's sections as cards, the sidebar and the
    theme's colours (see the module)."""
    from .. import build_version
    intro, found = sections()
    content = [f'<header class="intro" id="top"><h1>GroundTruth help</h1>{_modern_html(intro)}</header>']
    for anchor, title, body in found:
        content.append(f'<section id="{anchor}"><h3>{title}</h3>{_modern_html(body)}</section>')
    shell = SHELL.read_text(encoding="utf-8")
    for key, value in (("@VARS@", css_vars(pal)), ("@VERSION@", build_version()), ("@NAV@", nav_html(found)),
                       ("@CONTENT@", "\n".join(content))):
        shell = shell.replace(key, value)
    return shell


def topic_of(tab: str) -> str:
    """The section for a tab's label ("Results" -> "results"); the workflow for any other."""
    key = {"signal paths": "nets", "server jobs": "server", "dc loads": "dcloads", "power integrity": "pdn",
           "signal integrity": "si"}.get(tab.strip().lower(), tab.strip().lower())
    return key if key in TOPICS else "workflow"


def _webview_backend():
    """The browser engine to show the help with (Edge WebView2 on Windows, the platform's own elsewhere), or None:
    no wx.html2, the engine missing (an old Windows without the WebView2 runtime), or GROUNDTRUTH_HELP_CLASSIC set."""
    if os.environ.get("GROUNDTRUTH_HELP_CLASSIC"):
        return None
    try:
        import wx.html2 as webview
    except ImportError:
        return None
    backend = webview.WebViewBackendEdge if sys.platform == "win32" else webview.WebViewBackendDefault
    try:
        return backend if webview.WebView.IsBackendAvailable(backend) else None
    except Exception:                               # noqa: BLE001 - an engine that can't even be asked
        return None


class HelpWindow(wx.Frame):
    """The help window: modern_page in a browser engine where there is one, else page() in wx's simple HTML view
    (`self.html`); themed like the plugin either way."""

    def __init__(self, parent, topic: str = "workflow", anchor: str = ""):
        """The window over `parent`, shown at `topic` (or at `anchor` in it: a References entry, ref_anchor)."""
        super().__init__(parent, title="GroundTruth help", size=parent.FromDIP(wx.Size(1080, 780)))
        theme.set_icon(self)
        self.SetBackgroundColour(theme.colour(theme.palette().bg))
        self.view = None                            # the browser engine's view, when there is one
        self.html = None                            # wx's simple HTML view otherwise
        self._loaded, self._pending = False, self._target(topic, anchor)
        self._text = ""
        self._watch = None                          # the engine's start watched (LOAD_WAIT_MS), stopped on destroy
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        backend = _webview_backend()
        if backend is not None:
            self._web(backend)
        if self.view is None:
            self._classic()
        self.show(topic, anchor)

    LOAD_WAIT_MS = 8000         # the browser engine's page not there by then: the simple view instead

    def _classic(self):
        """wx's simple HTML view with page() (no browser engine, or it did not start)."""
        self.html = wx.html.HtmlWindow(self, style=wx.html.HW_SCROLLBAR_AUTO)
        size = self.GetFont().GetPointSize()
        self.html.SetStandardFonts(size + 1)
        self.html.Bind(wx.html.EVT_HTML_LINK_CLICKED, self._on_link)
        self.html.SetPage(page())
        self.SendSizeEvent()

    def _on_destroy(self, evt):
        """The window goes: the watch on the engine's start stops (a timer must not outlive its window; the event
        bubbles up from the views: ours only)."""
        evt.Skip()
        if evt.GetEventObject() is self and self._watch is not None:
            self._watch.Stop()
            self._watch = None

    def _not_loaded(self):
        """(LOAD_WAIT_MS after the start, or an engine error) The page never came (WebView2 failed to start: its data
        folder in use, a broken runtime): the simple view takes the window, at the section asked for."""
        self._watch = None
        if not self or self._loaded or self.view is None:
            return
        self.view.Destroy()
        self.view = None
        self._classic()
        wx.CallAfter(self._scroll, self._pending or "top")

    def _web(self, backend):
        """The browser engine's view with modern_page; links to other pages open in the web browser (none is ever
        followed in the help window). Left as None when the engine fails to start."""
        import wx.html2 as webview
        try:
            html = modern_page()
            view = webview.WebView.New(self, backend=backend)
        except Exception:                           # noqa: BLE001 - the simple view instead
            return
        self.view = view
        self._text = re.sub(r"\s+", " ", re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html,
                                                flags=re.S))
        try:
            view.EnableAccessToDevTools(False)
        except Exception:                           # noqa: BLE001 - not every engine has it
            pass
        view.Bind(webview.EVT_WEBVIEW_LOADED, self._on_loaded)
        view.Bind(webview.EVT_WEBVIEW_NAVIGATING, self._on_navigating)
        view.Bind(webview.EVT_WEBVIEW_NEWWINDOW, self._on_new_window)
        view.Bind(webview.EVT_WEBVIEW_ERROR, lambda e: (e.Skip(), wx.CallAfter(self._not_loaded)))
        view.SetPage(html, "")
        self._watch = wx.CallLater(self.LOAD_WAIT_MS, self._not_loaded)

    def text(self) -> str:
        """The page's text (for tests and checks): what the window shows, without its markup."""
        return self.html.ToText() if self.html is not None else self._text

    @staticmethod
    def _target(topic: str, anchor: str = "") -> str:
        """Where to go: a References entry's anchor (ref_anchor) when given, else the section (unknown: the top)."""
        if anchor and ANCHOR.fullmatch(anchor):
            return anchor
        return topic if topic in TOPICS else "top"

    def show(self, topic: str, anchor: str = ""):
        """Scroll to a section, or to an anchor in one (a References entry), once the page is laid out, and bring the
        window to the front."""
        topic = self._target(topic, anchor)
        if self.view is not None:
            if self._loaded:
                self._run(f"gtShow({json.dumps(topic)})")
            else:
                self._pending = topic
        else:
            wx.CallAfter(self._scroll, topic)
        self.Show()
        self.Raise()

    def _scroll(self, topic: str):
        """(simple view) Jump to the section's anchor."""
        if self:                            # closed before the call came
            self.html.ScrollToAnchor(topic)

    def _on_loaded(self, evt):
        """(browser engine) The page is there: go to the section asked for meanwhile."""
        evt.Skip()
        if not self._loaded:
            self._loaded = True
            if self._watch is not None:             # it started: no need to fall back any more
                self._watch.Stop()
                self._watch = None
            if self._pending and self._pending != "top":
                self._run(f"gtShow({json.dumps(self._pending)})")

    def _run(self, script: str):
        """(browser engine) Run a script in the page without waiting for it: RunScript waits for the result in a loop
        of its own, which hung when called outside the main loop (a test) and would run the window's events nested
        inside the caller's; RunScriptAsync (wx 3.1.6+, KiCad 10's wxPython has it) returns at once."""
        if hasattr(self.view, "RunScriptAsync"):
            self.view.RunScriptAsync(script)
        else:
            wx.CallAfter(lambda: self and self.view.RunScript(script))

    def _on_navigating(self, evt):
        """(browser engine) A link to another page: opened in the web browser, never in the help window (links
        within the page are scrolled to by the page's own script)."""
        url = evt.GetURL()
        if url.startswith(("http:", "https:", "mailto:", "file:")):
            evt.Veto()
            wx.LaunchDefaultBrowser(url)

    def _on_new_window(self, evt):
        """(browser engine) A link asking for a new window: the web browser."""
        wx.LaunchDefaultBrowser(evt.GetURL())

    def _on_link(self, evt):
        """(simple view) A link was clicked: a section within the page, or an outside page in the web browser."""
        href = evt.GetLinkInfo().GetHref()
        if href.startswith("#"):
            self.html.ScrollToAnchor(href[1:])
        else:
            wx.LaunchDefaultBrowser(href)
