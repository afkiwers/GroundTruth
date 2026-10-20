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
          "fields", "issues", "results", "fixes", "history", "thermal", "prefs", "export", "drawing", "labels",
          "siglabels", "settings", "speed", "methods", "limits", "references", "server")
# the sidebar's groups, by section; a section not listed goes under "More"
GROUPS = (("Getting started", ("workflow", "together", "checks")),
          ("The tabs", ("nets", "converters", "bridges", "dcloads", "pdn", "si", "parts", "issues", "results", "fixes",
                        "history")),
          ("Analyses and output", ("fields", "safety", "thermal", "prefs", "export", "drawing")),
          ("Labels and settings", ("labels", "siglabels", "settings")),
          ("Under the hood", ("speed", "server", "methods", "limits", "references")))


def page() -> str:
    """The help page in the window's theme colours, with the schematic-field table from components/fields.py and the
    references from docs/REFERENCES.md (wx's simple HTML view)."""
    pal = theme.palette()
    text = PAGE.read_text(encoding="utf-8")
    for key, value in (("@BG@", pal.bg), ("@TEXT@", pal.text), ("@LINK@", pal.accent), ("@WARN@", pal.warn),
                       ("@PART_FIELDS@", field_rows()), ("@REFERENCES@", references_html())):
        text = text.replace(key, value)
    return text


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
    return markdown_html(body) + f'<p><a href="{path.as_uri()}">Open REFERENCES.md</a> ({escape_html(str(path))})</p>'


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


def markdown_html(text: str) -> str:
    """The Markdown the docs use, as plain HTML both help views show: headings (## → h4, ### → h5), paragraphs,
    bullet lists (a line starting "- ", continued by indented lines) and pipe tables (the first row a header, the
    second the |---| rule)."""
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
                html.append("<tr>" + "".join(f"<{tag} valign='top'>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
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
        cells = (f"<b>{escape(p.field)}</b>", p.parts, p.keys, p.unit,
                 f"{format_si(p.lo, sym)} … {format_si(p.hi, sym)}", f"<code>{escape(p.example)}</code>", p.feeds)
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

    def __init__(self, parent, topic: str = "workflow"):
        """The window over `parent`, shown at `topic`."""
        super().__init__(parent, title="GroundTruth help", size=parent.FromDIP(wx.Size(1080, 780)))
        self.SetBackgroundColour(theme.colour(theme.palette().bg))
        self.view = None                            # the browser engine's view, when there is one
        self.html = None                            # wx's simple HTML view otherwise
        self._loaded, self._pending = False, topic
        self._text = ""
        self._watch = None                          # the engine's start watched (LOAD_WAIT_MS), stopped on destroy
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        backend = _webview_backend()
        if backend is not None:
            self._web(backend)
        if self.view is None:
            self._classic()
        self.show(topic)

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
        wx.CallAfter(self._scroll, self._pending if self._pending in TOPICS else "top")

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

    def show(self, topic: str):
        """Scroll to a section (once the page is laid out) and bring the window to the front."""
        topic = topic if topic in TOPICS else "top"
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
