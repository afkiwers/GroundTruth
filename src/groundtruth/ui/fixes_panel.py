"""Fixes tab: suggested stitching vias, decaps, bridge capacitors and fills, with predicted effect and a
verification of the selected set."""
from __future__ import annotations

from html import escape
from typing import Optional

import wx
import wx.html

from ..report.formats import comparison_text
from . import theme
from .background import LatestOnly

KIND_COLOUR = {"via": (44, 160, 44), "cap": (31, 119, 180), "fill": (148, 103, 189)}   # RGB


CHECK_NAMES = {"emi_margin": "radiated", "conducted_emission": "conducted", "hot_loop": "ringing"}
DESIGN_COLUMNS = [("Stage", 110), ("Change", 380), ("Radiated margin", 115), ("Conducted margin", 120),
                  ("Overshoot", 75), ("Extra loss", 75), ("Clears", 150)]
DESIGN_HELP = "fixes-design"            # the help's anchor of the design changes (help.html)
SCHEMATIC_H = 250                       # the row of the selected change: its text and schematic (DIP)


def _db(v) -> str:
    """A dB change for the table ("–" when unknown)."""
    return "–" if v is None else f"{v:+.1f} dB"


class DetailView(wx.html.HtmlWindow):
    """The selected design change, formatted: its figures before and after, the parts to fit and the notes. Keeps
    the text box's SetValue / GetValue (the main window writes Check in Simulate's comparison there; GetValue is
    the plain text of what is shown)."""

    def __init__(self, parent):
        """An empty view in the theme's colours."""
        super().__init__(parent, style=wx.html.HW_SCROLLBAR_AUTO, size=parent.FromDIP(wx.Size(-1, SCHEMATIC_H)))
        self.SetStandardFonts(self.GetFont().GetPointSize())
        self._text = ""

    def _page(self, body: str):
        """Show an HTML fragment in the theme's colours."""
        p = theme.palette()
        self.SetPage(f'<html><body bgcolor="{p.card}" text="{p.text}" link="{p.accent}">{body}</body></html>')

    def SetValue(self, text: str):                      # noqa: N802 - wx's TextCtrl name, kept for callers
        """Plain text (a message, Check in Simulate's comparison), as it is (monospaced)."""
        self._text = text
        self._page(f"<pre>{escape(text)}</pre>")

    def GetValue(self) -> str:                          # noqa: N802
        """The plain text of what is shown."""
        return self._text

    def show_option(self, stage: str, o: dict, now: dict, margin_db: float):
        """A design change: a heading, a small table now / with the change, the parts as a list and the notes."""
        oc = o["outcome"] or {}
        muted = theme.palette().muted

        def cell(v, unit="dB", fmt="{:+.1f}"):
            """A table value ("–" when unknown)."""
            return "–" if v is None else fmt.format(v) + " " + unit
        rows = [("Radiated margin", "radiated_db", "dB", "{:+.1f}"), ("Conducted margin", "conducted_db", "dB", "{:+.1f}"),
                ("Overshoot", "overshoot_v", "V", "{:.1f}"), ("Extra loss", "loss_w", "W", "{:.2f}")]
        table = "".join(
            f"<tr><td>{name}</td><td align='right'>{cell(now.get(key), unit, fmt)}</td>"
            f"<td align='right'><b>{cell(oc.get(key), unit, fmt)}</b></td></tr>"
            for name, key, unit, fmt in rows if now.get(key) is not None or oc.get(key) is not None)
        clears = ", ".join(CHECK_NAMES.get(c, c) for c in o["clears"])
        html = [f"<p><b>{escape(stage)}: {escape(o['title'])}</b></p>"]
        if o["kind"] != "now" and table:
            html.append(f"<table cellpadding='2' cellspacing='0'><tr><td></td><td><font color='{muted}'>now</font>"
                        f"</td><td><font color='{muted}'>with the change</font></td></tr>{table}</table>")
            html.append(f"<p><font color='{muted}'>margins: the rules want ≥ {margin_db:g} dB"
                        + (f"; this clears <b>{escape(clears)}</b>" if clears else "") + "</font></p>")
        if o["parts"]:
            html.append("<p><b>Parts</b></p><ul>" + "".join(f"<li>{escape(p)}</li>" for p in o["parts"]) + "</ul>")
        if o["notes"]:
            html.append("<p><b>Notes</b></p><ul>" + "".join(f"<li>{escape(n)}</li>" for n in o["notes"]) + "</ul>")
        self._text = "\n".join([f"{stage}: {o['title']}"] + [f"  part: {p}" for p in o["parts"]]
                               + [f"  {n}" for n in o["notes"]])
        self._page("".join(html))


class SchematicPreview(wx.Panel):
    """The selected change's schematic, scaled to the panel's size (aspect kept) whenever it is resized; a short
    text when there is none. Double-click: the window with the full-size drawing (the Fixes tab's handler)."""

    def __init__(self, parent):
        """An empty preview in the theme's card colour."""
        super().__init__(parent, size=parent.FromDIP(wx.Size(-1, SCHEMATIC_H)))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._image: Optional[wx.Image] = None
        self._cache: Optional[wx.Bitmap] = None
        self._text = ""
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), self._rescale()))

    def set_png(self, png: Optional[bytes], text: str = ""):
        """Show a PNG (None: the text instead)."""
        import io
        self._image = wx.Image(io.BytesIO(png), wx.BITMAP_TYPE_PNG) if png is not None else None
        self._text = text
        self._rescale()

    def _rescale(self):
        """The image scaled to fit (high quality, once per size: painting only blits it)."""
        w, h = self.GetClientSize()
        self._cache = None
        if self._image is not None and w > 4 and h > 4:
            scale = min(w / self._image.GetWidth(), h / self._image.GetHeight(), 1.0)
            img = self._image.Scale(max(1, int(self._image.GetWidth() * scale)),
                                    max(1, int(self._image.GetHeight() * scale)), wx.IMAGE_QUALITY_HIGH)
            self._cache = wx.Bitmap(img)
        self.Refresh()

    def _paint(self, _evt):
        """The card colour, then the scaled image (centred) or the text."""
        dc = wx.AutoBufferedPaintDC(self)
        p = theme.palette()
        dc.SetBackground(wx.Brush(theme.colour(p.card)))
        dc.Clear()
        w, h = self.GetClientSize()
        if self._cache is not None:
            dc.DrawBitmap(self._cache, (w - self._cache.GetWidth()) // 2, (h - self._cache.GetHeight()) // 2)
        elif self._text:
            dc.SetTextForeground(theme.colour(p.muted))
            dc.DrawLabel(self._text, wx.Rect(0, 0, w, h), wx.ALIGN_CENTER)


def copy_png(png: bytes) -> bool:
    """A PNG onto the clipboard as an image (to paste into a document, an e-mail, a chat); False when the clipboard
    could not be opened."""
    import io
    if not wx.TheClipboard.Open():
        return False
    try:
        wx.TheClipboard.SetData(wx.BitmapDataObject(wx.Bitmap(wx.Image(io.BytesIO(png), wx.BITMAP_TYPE_PNG))))
    finally:
        wx.TheClipboard.Close()
    return True


class SchematicWindow(wx.Frame):
    """A design change's schematic full size, with Save (PNG)."""

    def __init__(self, parent, title: str, png: bytes):
        """The window over `parent` showing the PNG."""
        import io
        super().__init__(parent, title=title)
        theme.set_icon(self)
        self.png = png
        self.SetBackgroundColour(theme.colour(theme.palette().card))
        img = wx.Image(io.BytesIO(png), wx.BITMAP_TYPE_PNG)
        view = wx.ScrolledWindow(self)
        view.SetScrollRate(10, 10)
        bmp = wx.StaticBitmap(view, bitmap=wx.Bitmap(img))
        vs = wx.BoxSizer(wx.VERTICAL)
        vs.Add(bmp, 0, wx.ALL, 8)
        view.SetSizer(vs)
        save = theme.Button(self, "Save as PNG…")
        save.Bind(wx.EVT_BUTTON, lambda e: self._save())
        copy = theme.Button(self, "Copy")
        copy.SetToolTip("The schematic to the clipboard, as an image")
        copy.Bind(wx.EVT_BUTTON, lambda e: copy_png(self.png))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(copy, 0, wx.RIGHT, 6)
        row.Add(save, 0)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(view, 1, wx.EXPAND)
        s.Add(row, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(s)
        avail = wx.Display(wx.Display.GetFromWindow(parent) if parent else 0).GetClientArea()
        self.SetSize(min(img.GetWidth() + 40, avail.width - 40), min(img.GetHeight() + 100, avail.height - 40))
        self.CentreOnParent()

    def _save(self):
        """Write the PNG where the user chooses."""
        with wx.FileDialog(self, "Save the schematic", wildcard="PNG image (*.png)|*.png",
                           defaultFile="schematic.png", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                with open(dlg.GetPath(), "wb") as f:
                    f.write(self.png)


class FixesPanel(wx.Panel):
    """The Fixes tab: the layout fixes found by Suggest (a ticked list, with the verification of the selected
    set) and, under them, the fix advisor's design changes for the power stages."""

    def __init__(self, parent, on_suggest, on_draw, on_place, on_add_whatif, on_compare, on_check_design=None):
        """The tab; the callbacks are the main window's: run Suggest, draw / place / store the ticked fixes,
        compare the what-if edits, and Simulate a design change in memory (on_check_design(stage, option))."""
        super().__init__(parent)
        self.result = None
        self.design: list[tuple[str, dict]] = []          # (stage, option) rows of the design-change list
        self.on_check_design = on_check_design
        self.buttons = []
        btns = wx.WrapSizer(wx.HORIZONTAL)
        for label, fn, tip in (
                ("Suggest fixes", lambda e: on_suggest(), "Find legal positions for fixes, predict and verify them"),
                ("Draw ticked on board", lambda e: on_draw(self.checked()), "Crosses and labels on the suggestion layer"),
                ("Place ticked vias in KiCad", lambda e: on_place(self.checked()), "Adds real vias (one undo step)"),
                ("Add ticked to what-if", lambda e: on_add_whatif(self.checked()), "Store them in the settings"),
                ("Compare what-if", lambda e: on_compare(), "Baseline vs. the settings's enabled what-if edits")):
            b = theme.Button(self, label)
            b.SetToolTip(tip)
            b.Bind(wx.EVT_BUTTON, fn)
            btns.Add(b, 0, wx.RIGHT | wx.BOTTOM, 6)
            self.buttons.append(b)
        for b in self.buttons[1:4]:             # Draw / Place / Add ticked: nothing ticked before a fix search
            b.block("Please run Suggest fixes first")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.EnableCheckBoxes()
        for i, (c, w) in enumerate([("#", 50), ("Kind", 50), ("Net", 150), ("Location", 110), ("Predicted", 210),
                                    ("Fix", 330), ("For issue", 150), ("Note", 200)]):
            self.list.InsertColumn(i, c, width=w)
        self.summary = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
        hint = wx.StaticText(self, label="Ticked = selected by the search (best per issue, verified together). "
                                         "Predicted: change of the loop moment at the worst harmonic (plane "
                                         "capacitance included) and of the loop inductance at the fundamental. The "
                                         "verification re-runs the full analysis on a copy of the board with the "
                                         "selected fixes.")
        hint.Wrap(900)
        # the fix advisor's design changes for the power stages (analysis/fix_advisor.py), after every Simulate.
        # The user, 2026-11-02: "Can we brush this area up a bit. It is hard to read and understand. Some example
        # filter schematics when clicking on one, maybe right click opens a dropdown": the margins marked against
        # the rules' margin, the full title on hover, the selected change formatted beside its schematic
        # (report/schematics.py, rendered in a worker), and a right-click menu.
        title = wx.StaticText(self, label="Design changes (power stages): what clears the ringing, radiated and "
                                          "conducted issues, from the last Simulate")
        title.SetFont(title.GetFont().Bold())
        legend = wx.StaticText(self, label="Margins to the limit: ✓ meets the rules' margin, ! under the limit but "
                                           "inside the margin, ✗ over the limit. Click a change for its parts and "
                                           "schematic; right-click for more.")
        legend.SetForegroundColour(theme.colour(theme.palette().muted))
        self.design_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (c, w) in enumerate(DESIGN_COLUMNS):
            self.design_list.InsertColumn(i, c, width=w)
        self.design_list.SetMinSize(wx.Size(-1, 150))
        self.design_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._design_selected)
        self.design_list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self._design_menu)
        self.design_list.Bind(wx.EVT_MOTION, self._design_hover)
        self.design_text = DetailView(self)
        self.schematic = SchematicPreview(self)
        self.schematic.SetToolTip("The selected change's example schematic; double-click (or Schematic…) for the full "
                                  "size, with Save")
        self.schematic.Bind(wx.EVT_LEFT_DCLICK, lambda e: self._open_schematic())
        self._png: Optional[bytes] = None           # the selected change's schematic, as rendered
        self._asked = 0                             # the newest schematic asked for (a later one wins)
        self._drawer = LatestOnly("groundtruth-schematic", on_error=lambda e: wx.CallAfter(self._drawn, -1, None))
        self.check_design = theme.Button(self, "Check in Simulate")
        self.check_design.SetToolTip("Simulate the selected change's stage again with the change applied (in memory: "
                                     "the settings stay as they are) and compare it with the prediction")
        self.check_design.Bind(wx.EVT_BUTTON, lambda e: self._check())
        self.check_design.Enable(False)
        self.open_schematic = theme.Button(self, "Schematic…")
        self.open_schematic.SetToolTip("The selected change's example schematic in a window of its own (with Save)")
        self.open_schematic.Bind(wx.EVT_BUTTON, lambda e: self._open_schematic())
        self.open_schematic.Enable(False)
        self.copy_parts = theme.Button(self, "Copy parts")
        self.copy_parts.SetToolTip("The selected change's parts and notes to the clipboard (for the BOM or a ticket)")
        self.copy_parts.Bind(wx.EVT_BUTTON, lambda e: self._copy())
        self.copy_parts.Enable(False)
        self._busy = False
        side = wx.BoxSizer(wx.VERTICAL)
        for b in (self.check_design, self.open_schematic, self.copy_parts):
            side.Add(b, 0, wx.EXPAND | wx.BOTTOM, 6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.design_text, 2, wx.EXPAND | wx.RIGHT, 6)        # the schematic gets the larger share
        row.Add(self.schematic, 3, wx.EXPAND | wx.RIGHT, 6)
        row.Add(side, 0)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(btns, 0, wx.ALL, 6)
        s.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        s.Add(self.summary, 0, wx.EXPAND | wx.ALL, 6)
        s.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        s.Add(title, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        s.Add(legend, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        s.Add(self.design_list, 1, wx.EXPAND | wx.ALL, 6)
        s.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.SetSizer(s)
        self.results: dict = {}                    # stage -> its ConverterResult (the schematic's part names)
        self.margin_db = 6.0                       # the rules' emission margin (the ✓ / ! marks)

    def show_design(self, converters: list, margin_db: Optional[float] = None):
        """The design changes of the stages Simulate found issues on (ConverterResult.fixes): per stage its "now"
        row first, then the changes that clear an issue, then the rest, each in the advisor's order. margin_db:
        the rules' emission margin, for the marks."""
        if margin_db is not None:
            self.margin_db = margin_db
        self.results = {c.net: c for c in converters}
        rows = []
        for c in converters:
            opts = list(getattr(c, "fixes", None) or [])
            opts.sort(key=lambda o: (o["kind"] != "now", not o["clears"]))       # stable: the advisor's order within
            rows += [(c.net, o) for o in opts]
        self.design = rows
        lst = self.design_list
        lst.DeleteAllItems()
        for k, (stage, o) in enumerate(self.design):
            oc = o["outcome"] or {}
            ov = oc.get("overshoot_v")
            vals = [stage, o["title"], self._margin(oc.get("radiated_db")), self._margin(oc.get("conducted_db")),
                    "–" if ov is None else f"{ov:.1f} V",
                    "–" if o["kind"] == "now" else f"{(oc.get('loss_w') or 0) * 1e3:.0f} mW",
                    ", ".join(CHECK_NAMES.get(c, c) for c in o["clears"]) or ("" if o["kind"] == "now" else "–")]
            lst.InsertItem(k, vals[0])
            for col, v in enumerate(vals[1:], 1):
                lst.SetItem(k, col, v)
            if o["kind"] == "now":
                lst.SetItemTextColour(k, theme.colour(theme.palette().muted))
        self.design_text.SetValue("Select a change for its parts, notes and schematic." if self.design else
                                  "No power stage with ringing, radiated or conducted issues in the last Simulate.")
        self._show_png(None)
        for b in (self.check_design, self.open_schematic, self.copy_parts):
            b.Enable(False)

    def _margin(self, v) -> str:
        """A margin to the limit with its mark: ✓ at least the rules' margin, ! under the limit by less, ✗ over."""
        if v is None:
            return "–"
        return f"{v:+.1f} dB " + ("✓" if v >= self.margin_db else "!" if v >= 0 else "✗")

    def _now_of(self, stage: str) -> dict:
        """The stage's outcome as it is (its "now" row), for the before / after table."""
        return next(((o["outcome"] or {}) for s, o in self.design if s == stage and o["kind"] == "now"), {})

    def _design_selected(self, evt):
        """A design change was selected: its figures before and after, parts and notes beside its schematic (drawn
        in the background); Check enabled if it can be simulated."""
        k = evt.GetIndex()
        stage, o = self.design[k]
        self.design_text.show_option(stage, o, self._now_of(stage), self.margin_db)
        self.check_design.Enable(self._checkable(o))
        self.copy_parts.Enable(o["kind"] != "now")
        self._draw(stage, o)

    def _draw(self, stage: str, o: dict):
        """The option's schematic, rendered in the worker (matplotlib off the GUI thread); none for a change
        without a circuit."""
        from ..report import schematics
        self._asked += 1
        asked, result, ink = self._asked, self.results.get(stage), self._ink()

        def work():
            """(worker) The PNG of the schematic, or None."""
            fig = schematics.figure_for(o, stage, ink, result)
            png = schematics.png(fig) if fig is not None else None
            wx.CallAfter(self._drawn, asked, png)
        self._drawer.submit(work)

    def _drawn(self, asked: int, png: Optional[bytes]):
        """(GUI thread) A schematic arrived: shown unless a newer one was asked for meanwhile."""
        if not self or (asked != -1 and asked != self._asked):
            return
        self._show_png(png)

    def _show_png(self, png: Optional[bytes]):
        """The schematic beside the text, or an empty box (with a word why, for a selected change without one);
        Schematic… enabled with one."""
        self._png = png
        self.schematic.set_png(png, "" if png is not None or self.design_list.GetFirstSelected() < 0 else
                               "no circuit to draw: a layout change or a controller setting")
        self.open_schematic.Enable(png is not None)

    @staticmethod
    def _ink():
        """The schematic's colours from the window's theme."""
        from ..report.schematics import Ink
        p = theme.palette()
        return Ink(line=p.text, muted=p.muted, accent=p.accent, bg=p.card)

    def _open_schematic(self):
        """The schematic in a window of its own, full size, with Save."""
        k = self.design_list.GetFirstSelected()
        if self._png is None or k < 0:
            return
        stage, o = self.design[k]
        SchematicWindow(self, f"{stage}: {o['title']}", self._png).Show()

    def _copy_schematic(self):
        """The selected change's schematic to the clipboard, as an image."""
        if self._png is not None and copy_png(self._png):
            top = self.GetTopLevelParent()
            if hasattr(top, "SetStatusText"):
                top.SetStatusText("schematic copied to the clipboard")

    def _copy(self):
        """The selected change's parts and notes to the clipboard."""
        k = self.design_list.GetFirstSelected()
        if k < 0:
            return
        stage, o = self.design[k]
        text = "\n".join([f"{stage}: {o['title']}"] + [f"- {p}" for p in o["parts"]] + [f"  {n}" for n in o["notes"]])
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
            top = self.GetTopLevelParent()
            if hasattr(top, "SetStatusText"):
                top.SetStatusText("parts copied to the clipboard")

    def _design_menu(self, evt):
        """Right-click on a change: its schematic in a window, the parts copied, Check in Simulate, the help."""
        k = evt.GetIndex()
        if k < 0:
            return
        if self.design_list.GetFirstSelected() != k:            # the row under the mouse becomes the selected one
            self.design_list.Select(k)
            self._design_selected(evt)
        menu = wx.Menu()
        for label, fn, on in self.menu_items(self.design[k][1]):
            if label is None:
                menu.AppendSeparator()
                continue
            item = menu.Append(wx.ID_ANY, label)
            item.Enable(on)
            self.Bind(wx.EVT_MENU, lambda e, fn=fn: fn(), item)
        self.design_list.PopupMenu(menu)
        menu.Destroy()

    def menu_items(self, o: dict) -> list:
        """The right-click menu of a change: [(label, function, enabled)], None as the label for a separator (a
        method of its own: the popup is modal, tests read the entries here)."""
        return [("Show the schematic in a window", self._open_schematic, self._png is not None),
                ("Copy the schematic", self._copy_schematic, self._png is not None),
                ("Copy the parts list", self._copy, o["kind"] != "now"),
                ("Check in Simulate", self._check, self._checkable(o)),
                (None, None, False),
                ("Help: design changes", self._help, True)]

    def _help(self):
        """The help at the Fixes tab's design changes."""
        top = self.GetTopLevelParent()
        if hasattr(top, "show_help"):
            top.show_help("fixes", DESIGN_HELP)

    def _design_hover(self, evt):
        """The full title of the row under the mouse as the list's tooltip (the column cuts long ones)."""
        evt.Skip()
        k, _ = self.design_list.HitTest(evt.GetPosition())
        tip = ""
        if 0 <= k < len(self.design):
            stage, o = self.design[k]
            tip = f"{stage}: {o['title']}\n(click: parts and schematic; right-click: more)"
        if self.design_list.GetToolTipText() != tip:
            self.design_list.SetToolTip(tip)

    def _checkable(self, o: dict) -> bool:
        """A change Simulate can apply in memory: a snubber, an edge time, spread spectrum (not a shorter loop, a
        layout change, nor a filter, which is outside the board)."""
        return (bool(self.on_check_design) and not self._busy and o["kind"] in ("snubber", "edge", "combined", "spread")
                and o["change"].get("loop_scale", 1.0) == 1.0)

    def _check(self):
        """Simulate the selected design change (the main window runs it as a job)."""
        k = self.design_list.GetFirstSelected()
        if k >= 0 and self.on_check_design and self._checkable(self.design[k][1]):
            self.on_check_design(*self.design[k])

    def show(self, result):
        """Show a Suggest result: one row per suggestion (ticked: chosen by the search), coloured by kind, and the
        verification of the selected set below."""
        self.result = result
        for b in self.buttons[1:4]:
            b.block("" if result.suggestions else "Suggest fixes found nothing to draw, place or add")
        self.list.DeleteAllItems()
        for k, sug in enumerate(result.suggestions):
            row = self.list.InsertItem(k, sug.id)
            at = f" @ {sug.f_eval / 1e6:.4g} MHz" if sug.f_eval else ""
            pred = (f"{sug.delta_db:+.1f} dB{at}, L {sug.delta_l * 100:+.0f} %" if sug.delta_db is not None else "–")
            for col, v in enumerate([sug.kind, sug.net, f"{sug.location[0]:.2f}, {sug.location[1]:.2f}", pred,
                                     f"{sug.title}: {sug.message}", sug.issue.check if sug.issue else "", sug.note], 1):
                self.list.SetItem(row, col, v)
            self.list.CheckItem(row, sug.selected)
            if sug.kind in KIND_COLOUR:
                self.list.SetItemTextColour(row, KIND_COLOUR[sug.kind])
        if result.after is not None:
            self.summary.SetValue("Verification of the selected fixes:\n" +
                                  comparison_text(result.comparison, result.fixed, result.new, result.kept))
        else:
            self.summary.SetValue(f"{len(result.suggestions)} suggestion(s); nothing selected to verify.")

    def checked(self):
        """The ticked suggestions."""
        if self.result is None:
            return []
        return [s for k, s in enumerate(self.result.suggestions) if self.list.IsItemChecked(k)]

    def show_text(self, text: str):
        """A message in the summary box (a comparison, a failure)."""
        self.summary.SetValue(text)

    def enable(self, on: bool):
        """Grey out the buttons while an analysis runs."""
        for b in self.buttons:
            b.Enable(on)
        self._busy = not on
        k = self.design_list.GetFirstSelected()
        self.check_design.Enable(on and k >= 0 and self._checkable(self.design[k][1]))
