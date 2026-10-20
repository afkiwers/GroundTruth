"""Issues and Results tabs."""
from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional

import wx
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg, NavigationToolbar2WxAgg
from matplotlib.figure import Figure

from ..report.plots import draw_heatmap, draw_section, draw_spectrum
from . import theme
from .background import LatestOnly

# RGB tuples, not wx.Colour: modules are imported in the start-up worker thread (wx objects belong to the GUI thread)
SEV_STATUS = {"error": "error", "warning": "warn", "info": "accent"}     # theme palette entries


class IssuesPanel(wx.Panel):
    """The Issues tab: the last run's issues, numbered as their markers on the board, coloured by severity (waived
    ones muted). Clicking one shows it (in KiCad, live); the buttons draw the markers and waive an issue. "Checks…"
    hides the checks the user does not want to see (the list and the markers drawn), remembered on this computer
    (ui.json `issues_hidden`); every issue keeps its number, so the rows shown and the markers still match."""

    def __init__(self, parent, on_show, on_draw, on_waive, live: bool = False):
        """on_show(issue): show the clicked issue on the board (live: in KiCad); live adds the KiCad options."""
        super().__init__(parent)
        self.issues = []
        self.rows: list[tuple[int, object]] = []      # (number, issue) of each row shown, in list order
        self.hidden: set[str] = set(theme.prefs().get("issues_hidden") or [])
        self.on_show = on_show
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (c, w) in enumerate([("#", 36), ("Severity", 70), ("Check", 140), ("Net", 150), ("Location", 110),
                                    ("Layer", 70), ("Message", 600)]):
            self.list.InsertColumn(i, c, width=w)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self.show_selected())   # double-click, Enter
        btns = wx.BoxSizer(wx.HORIZONTAL)
        b = theme.Button(self, "Draw markers on board")
        b.Bind(wx.EVT_BUTTON, lambda e: on_draw())
        btns.Add(b, 0, wx.RIGHT, 6)
        self.draw_button = b
        w = theme.Button(self, "Waive selected…")
        w.Bind(wx.EVT_BUTTON, lambda e: self._waive(on_waive))
        btns.Add(w, 0, wx.RIGHT, 6)
        f = theme.Button(self, "Checks…")
        f.SetToolTip("Choose which checks the list shows (and Draw markers on board draws); the others are hidden, "
                     "not waived, and the choice is kept on this computer")
        f.Bind(wx.EVT_BUTTON, lambda e: self.choose_checks())
        btns.Add(f, 0, wx.RIGHT, 6)
        self.shown_label = wx.StaticText(self, label="")
        btns.Add(self.shown_label, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 18)
        show = theme.Button(self, "Show in KiCad")
        show.SetToolTip("Show the selected issue in KiCad again (after you moved the view or changed the selection "
                        "there). Clicking an issue shows it too.")
        show.Bind(wx.EVT_BUTTON, lambda e: self.show_selected())
        self.zoom = wx.CheckBox(self, label="Zoom to it")
        self.zoom.SetToolTip("Zoom KiCad's view to the issue's copper when you click it")
        self.nets = wx.CheckBox(self, label="Highlight its net")
        self.nets.SetToolTip("Highlight the issue's net in KiCad (everything else dimmed); ~ in KiCad clears it")
        from .prefs_dialog import remember
        for c, key in ((self.zoom, "issue_zoom"), (self.nets, "issue_highlight")):
            c.SetValue(True)
            remember(c, key)
        for c in (show, self.zoom, self.nets):
            btns.Add(c, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)
            c.Show(live)
        self.detail = wx.StaticText(self, label=(
            "Click an issue to show it in KiCad: its copper selected, its net highlighted, the view zoomed to it."
            if live else "Click an issue to see what it is about. (Open GroundTruth from KiCad's toolbar to show "
                         "issues on the board.)"))
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(btns, 0, wx.ALL, 6)
        s.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        s.Add(self.detail, 0, wx.ALL, 6)
        self.SetSizer(s)

    def show(self, issues):
        """List these issues (in the order the markers are numbered), the hidden checks left out."""
        self.issues = issues
        self._fill()

    def _fill(self):
        """The rows: every issue whose check is not hidden, under its own number (its place in the whole list, as
        its marker on the board is numbered), and the count of what is shown."""
        self.rows = [(k, i) for k, i in enumerate(self.issues, 1) if i.check not in self.hidden]
        self.list.Freeze()
        try:
            self.list.DeleteAllItems()
            pal = theme.palette()
            for n, (k, i) in enumerate(self.rows):
                row = self.list.InsertItem(n, str(k))
                for col, v in enumerate([i.severity + (" (waived)" if i.waived else ""), i.check, i.net,
                                         f"{i.location[0]:.2f}, {i.location[1]:.2f}", i.layer, i.message], 1):
                    self.list.SetItem(row, col, v)
                self.list.SetItemTextColour(row, theme.colour(pal.muted if i.waived else
                                                              getattr(pal, SEV_STATUS.get(i.severity, "text"))))
        finally:
            self.list.Thaw()
        gone = len(self.issues) - len(self.rows)
        self.shown_label.SetLabel(f"{len(self.rows)} of {len(self.issues)} shown ({gone} hidden)" if gone else "")
        self.Layout()

    def checks_present(self) -> list[tuple[str, int]]:
        """(check, how many issues have it) for the checks in the list, in the list's order."""
        counts: dict[str, int] = {}
        for i in self.issues:
            counts[i.check] = counts.get(i.check, 0) + 1
        return list(counts.items())

    def set_hidden(self, hidden):
        """Hide these checks (and show the others), remember the choice on this computer, and list again."""
        self.hidden = set(hidden)
        theme.save_prefs(issues_hidden=sorted(self.hidden))
        self._fill()

    def choose_checks(self):
        """"Checks…": tick the checks to show. Checks not in this list keep their state (hidden stays hidden)."""
        present = self.checks_present()
        if not present:
            return
        from ..core.issues import CHECKS
        names = [f"{c} ({n})  {CHECKS.get(c, ('', ''))[1]}" for c, n in present]
        with wx.MultiChoiceDialog(self, "Show the issues of these checks (the others are hidden, not waived):",
                                  "Issues: checks", names) as dlg:
            dlg.SetSelections([k for k, (c, _) in enumerate(present) if c not in self.hidden])
            if dlg.ShowModal() != wx.ID_OK:
                return
            shown = {present[k][0] for k in dlg.GetSelections()}
        others = {c for c in self.hidden if c not in dict(present)}
        self.set_hidden(others | {c for c, _ in present if c not in shown})

    def enable_drawing(self, on: bool):
        """Drawing markers needs KiCad and no job running."""
        self.draw_button.Enable(on)

    def on_select(self, evt):
        """An issue was clicked: its title and fingerprint below the list, and it is shown on the board."""
        i = self.rows[evt.GetIndex()][1]
        self.detail.SetLabel(f"{i.title} — fingerprint {i.fingerprint}")
        self.on_show(i)

    def show_selected(self):
        """Show the selected issue again (Show in KiCad, a double click or Enter)."""
        k = self.list.GetFirstSelected()
        if 0 <= k < len(self.rows):
            self.on_show(self.rows[k][1])

    def _waive(self, on_waive):
        """Ask for a reason and waive the selected issue with it (on_waive stores it in the settings by the
        issue's fingerprint, so it stays waived in later runs)."""
        k = self.list.GetFirstSelected()
        if not 0 <= k < len(self.rows):
            return
        with wx.TextEntryDialog(self, "Reason for accepting this issue:", "Waive issue") as dlg:
            if dlg.ShowModal() == wx.ID_OK and dlg.GetValue().strip():
                on_waive(self.rows[k][1], dlg.GetValue().strip())


CB_SETDROPPEDWIDTH = 0x0160            # Win32: the drop-down list's width, independent of the box's


def _set_items(choice: wx.Choice, items: list[str]):
    """Fill a native list without a redraw per item (inserting costs ~2 ms per item on Windows otherwise); its
    drop-down opens as wide as its longest entry (the user: the Route list's long view names were cut off at the
    box's width, "text is truncated")."""
    choice.Freeze()
    try:
        choice.Set(items)
    finally:
        choice.Thaw()
    _drop_width(choice, items)


def _drop_width(choice: wx.Choice, items: list[str]):
    """Windows: open the drop-down list wide enough for its longest entry (at most the screen's width); the box
    itself keeps its size. Elsewhere nothing (GTK and macOS size their pop-ups to the entries already)."""
    if not items or not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        widest = max(choice.GetTextExtent(s)[0] for s in items) + choice.FromDIP(28)   # + scroll bar, margins
        screen = wx.Display(max(wx.Display.GetFromWindow(choice), 0)).GetClientArea().width
        user32 = ctypes.windll.user32
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW(choice.GetHandle(), CB_SETDROPPEDWIDTH, max(min(widest, screen - 40),
                                                                         choice.GetSize().width), 0)
    except Exception:                   # noqa: BLE001 - only the list's width: never worth failing the tab over
        pass


class PrerenderedCanvas(FigureCanvasWxAgg):
    """A matplotlib wx canvas that can show a figure rendered in another thread without rendering it again on
    the GUI thread. matplotlib's own redraws (toolbar zoom and pan, resizing) still render here."""

    defer = None                # callable: True while a figure rendered in the background is on its way
    page = None                 # the scrolled window it sits on (the Results tab): the wheel scrolls that

    def _on_mouse_wheel(self, event):
        """On the Results tab's scrolling page the wheel scrolls the page: the maps fill its first screen, so a
        wheel that zoomed them left no way down to the plots under them. Ctrl + wheel zooms the map under the mouse
        (matplotlib's scroll_event, as the wheel alone does in the enlarged window) and leaves the page where it
        is. matplotlib binds this method by name (backend_wx, 3.11)."""
        if self.page is None:
            super()._on_mouse_wheel(event)
        elif event.ControlDown():
            super()._on_mouse_wheel(event)
            event.Skip(False)               # handled: Windows would pass it on to the page, which would scroll
        else:
            scroll_page(self.page, event)

    def _on_paint(self, event):
        """A paint after a resize renders the figure here, on the GUI thread, and every figure's draw holds
        matplotlib's one class-wide lock (Figure._render_lock): while the background thread rendered a view, the
        window waited for it (0.68 s in test_jobs_run_off_the_gui_thread once the cross-section's canvas came and
        went). With a rendered figure on its way, only the background is painted; it arrives with its pixels."""
        if not self._isDrawn and self.defer is not None and self.defer():
            dc = wx.PaintDC(self)
            dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
            dc.Clear()
            return
        super()._on_paint(event)

    def show_rendered(self, fig: Figure, agg: FigureCanvasAgg) -> bool:
        """Swap in `fig`, rendered by the offscreen `agg` at this canvas's pixel size, reusing its pixels. Uses
        matplotlib internals (checked with 3.11: _canvas_callbacks, _original_dpi, _lastKey, _isDrawn,
        _create_bitmap); if they change or the size no longer matches, the figure is rendered here instead
        (correct, only slower). True when the pixels were reused."""
        old = self.figure
        fig._original_dpi = old._original_dpi
        fig._canvas_callbacks = old._canvas_callbacks       # keeps the toolbar's mouse connections
        fig.set_canvas(self)
        self.figure = fig
        try:
            if agg.get_width_height(physical=True) != self.get_width_height(physical=True):
                raise ValueError("the canvas was resized meanwhile")
            self.renderer, self._lastKey = agg.renderer, agg._lastKey
            self.bitmap = self._create_bitmap()
            self._isDrawn = True
            self.Refresh(eraseBackground=False)
            self.reused = getattr(self, "reused", 0) + 1
            return True
        except Exception:
            self.draw_idle()
            return False


def views_dir() -> Path:
    """Where "Open 3D view" writes its pages: %LOCALAPPDATA%\\GroundTruth\\views (~/.cache/groundtruth/views
    elsewhere), the user's own folder."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "GroundTruth" / "views"
    return Path.home() / ".cache" / "groundtruth" / "views"


ZOOM_STEP = 1.25            # one wheel notch zooms in or out this much, about the point under the mouse
FOCUS_MM = 6.0              # the busiest via's tile zooms the maps to a square this wide around it
SPECTRUM_IN = 2.8           # the spectrum's height on the Results tab's page (full width), inches of its figure
MAPS_MIN_PX = 300           # the maps fill the page's visible height, but never less than this
SCROLL_PX = 40              # the page's scroll unit: one wheel notch (3 lines) scrolls 120 px
ZOOM_HINT = "Ctrl + wheel over a map zooms it, double-click goes back; the wheel scrolls the page"
READOUT_CHARS = 140         # a marker's text in the readout next to the toolbar (the tooltip has all of it)


def scroll_page(page: wx.ScrolledWindow, event: wx.MouseEvent) -> None:
    """Scroll `page` as its own wheel handling would, for a wheel event over one of its children (a canvas, which
    otherwise takes the wheel for itself)."""
    if event.GetWheelAxis() != wx.MOUSE_WHEEL_VERTICAL or not event.GetWheelRotation():
        return
    lines = -event.GetWheelRotation() / (event.GetWheelDelta() or 120) * (event.GetLinesPerAction() or 3)
    page.Scroll(-1, max(page.GetViewStart()[1] + int(round(lines)), 0))


class ZoomToolbar(NavigationToolbar2WxAgg):
    """matplotlib's toolbar whose Home button also undoes the wheel zoom (the zoomed view is a figure rendered
    for it, so matplotlib's own home would only go back to that)."""

    def __init__(self, canvas, on_home=None):
        """The toolbar of `canvas`; on_home() undoes a wheel zoom (True when there was one)."""
        super().__init__(canvas)
        self.on_home = on_home

    def home(self, *args):
        """Home: undo the wheel zoom if there is one, else matplotlib's own home (undo zoom and pan)."""
        if self.on_home is not None and self.on_home():
            return
        super().home(*args)


def zoomed(view: tuple, full: tuple, x: float, y: float, step: float) -> Optional[tuple]:
    """The view (x0, x1, y0, y1) after zooming by `step` (< 1: in) about (x, y), kept inside `full` (shifted,
    never squeezed, so the maps keep their shape); None once it covers all of `full` again."""
    x0, x1, y0, y1 = view
    nx0, nx1 = x - (x - x0) * step, x + (x1 - x) * step
    ny0, ny1 = y - (y - y0) * step, y + (y1 - y) * step
    fx0, fx1, fy0, fy1 = full
    if nx1 - nx0 >= (fx1 - fx0) * 0.999 or ny1 - ny0 >= (fy1 - fy0) * 0.999:
        return None
    dx = max(fx0 - nx0, 0.0) - max(nx1 - fx1, 0.0)
    dy = max(fy0 - ny0, 0.0) - max(ny1 - fy1, 0.0)
    return (nx0 + dx, nx1 + dx, ny0 + dy, ny1 + dy)


class HeatmapWindow(wx.Frame):
    """The heatmap on its own, in a window that can be enlarged or maximised (the tab's maps are small): the
    tab's view (route, layer, arrows) with matplotlib's toolbar (zoom, pan, save). Rendered in the tab's
    background thread like the tab itself; matplotlib's own redraws (zoom, pan, resize) render here."""

    def __init__(self, parent, on_closed):
        """A window at least as large as the main one, with the toolbar and a readout above the canvas;
        on_closed() is called when it closes."""
        top = wx.GetTopLevelParent(parent)
        size = top.GetSize() if top else wx.Size(1200, 800)
        super().__init__(top, title=f"{top.GetTitle() if top else 'GroundTruth'}: heatmap",
                         size=(max(size.width, 1000), max(size.height, 750)))
        self.fig = Figure(figsize=(12, 8))
        self.canvas = PrerenderedCanvas(self, -1, self.fig)
        self.toolbar = ZoomToolbar(self.canvas)
        self.readout = wx.StaticText(self, label="")
        s = wx.BoxSizer(wx.VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.toolbar, 0, wx.EXPAND)
        row.Add(self.readout, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        s.Add(row, 0, wx.EXPAND)
        s.Add(self.canvas, 1, wx.EXPAND)
        self.SetSizer(s)
        self.Bind(wx.EVT_CLOSE, lambda e: (on_closed(), e.Skip()))
        self._resized = None
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.on_resized = None

    def _on_size(self, evt):
        """After a resize settles, the tab renders the view again at the new size (in the background)."""
        evt.Skip()
        if self.on_resized is not None:
            if self._resized is not None:
                self._resized.Stop()
            self._resized = wx.CallLater(300, lambda: self and self.on_resized and self.on_resized())


CURVE_KINDS = ("pdn", "bode")       # views drawn as curves, not maps (no layers, arrows or board drawing)


class ResultsPanel(wx.Panel):
    """The Results tab: the views of the last run (a signal route, a power stage, its DC paths, a bridge, a thermal
    solve) chosen in the Route list, with the view's KPI tiles and a summary line, and below them on a scrolling
    page the current-density maps, a route's cross-section and the spectrum. Figures are built and rendered in a
    background thread (redraw) and swapped into the canvases when ready, so the window never waits for them."""

    def __init__(self, parent, on_draw_contours, on_draw_arrows=None, on_sharper=None, on_capacity=None,
                 in_steps: bool = False, on_mark=None):
        """in_steps: the plots' page is built later, in two `build_steps` (MainFrame._build_pages runs them one per
        event-loop step: the three matplotlib canvases in one step blocked the window ~110 ms); otherwise here.
        on_mark(kind, what, text, layer): a marker on a map was clicked (plots.mark_ref_at; layer: the map's)."""
        super().__init__(parent)
        self.on_mark = on_mark
        self.board = None
        self.routes = []
        self.issues = []
        self._asked = self._shown = 0          # generations of the requested and the displayed view
        self._renderer = LatestOnly("groundtruth-render",
                                    on_error=lambda e: wx.CallAfter(self._render_failed, e))
        # the page's heights measure text on the GUI thread (plots._width_in): matplotlib's first font lookup there
        # stalled the first view after a Simulate by ~300 ms (gui_probe on refboard); done here instead
        from ..report.plots import warm_text
        self._renderer.submit(warm_text)
        # The row wraps onto a second line in a narrow window (a laptop screen; the Route list grows with the
        # longest view name after a run): a plain row pushed the last buttons out of sight. Each label stays with
        # its list.
        top = wx.WrapSizer(wx.HORIZONTAL, wx.REMOVE_LEADING_SPACES)     # no stretched last item
        route = wx.BoxSizer(wx.HORIZONTAL)
        route.Add(wx.StaticText(self, label="Route:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.choice = wx.Choice(self)
        self.choice.SetMinSize((220, -1))
        self.choice.Bind(wx.EVT_CHOICE, lambda e: self._route_changed())
        route.Add(self.choice, 0, wx.ALIGN_CENTER_VERTICAL)
        top.Add(route, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT | wx.BOTTOM, 8)
        layer = wx.BoxSizer(wx.HORIZONTAL)
        layer.Add(wx.StaticText(self, label="Layer:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.layer = wx.Choice(self)
        self.layer.Bind(wx.EVT_CHOICE, lambda e: self.redraw())
        layer.Add(self.layer, 0, wx.ALIGN_CENTER_VERTICAL)
        top.Add(layer, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT | wx.BOTTOM, 8)
        self.arrows = wx.CheckBox(self, label="Current arrows")
        self.arrows.SetValue(True)
        self.arrows.SetToolTip("arrows on the map, all the same size: each points the way the current flows, and "
                               "how many there are shows how much flows (they crowd where it concentrates; idle "
                               "copper has none). The colour is the current density.")
        self.arrows.Bind(wx.EVT_CHECKBOX, lambda e: self.redraw())
        top.Add(self.arrows, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT | wx.BOTTOM, 8)
        self.log_scale = wx.CheckBox(self, label="Log scale")
        self.log_scale.SetValue(True)
        self.log_scale.SetToolTip("colours over three decades below the peak, so that even weak current spread over "
                                  "a whole plane shows; untick for a linear scale from 0 to the peak, which shows "
                                  "where most of the current flows (temperature views are always linear)")
        self.log_scale.Bind(wx.EVT_CHECKBOX, lambda e: self.redraw())
        top.Add(self.log_scale, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT | wx.BOTTOM, 8)
        self.section_box = wx.CheckBox(self, label="Cross-section")
        self.section_box.SetValue(True)
        self.section_box.SetToolTip("below the maps (scroll down), for a signal route: how much of the return each "
                                    "layer carries along the route (driver on the left) and where it changes layer; "
                                    "untick to leave it out")
        self.section_box.Bind(wx.EVT_CHECKBOX, lambda e: self.redraw())
        from .prefs_dialog import remember
        for c, key in ((self.arrows, "arrows"), (self.log_scale, "log_scale"), (self.section_box, "cross_section")):
            remember(c, key)
        top.Add(self.section_box, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT | wx.BOTTOM, 8)
        from .prefs_dialog import defaults
        p = defaults(theme.prefs())         # the Preferences dialog sets them (set_display)
        self.via_markers, self.vias_limit, self.outlines, self.names = (p["via_markers"], p["max_vias"], p["outlines"],
                                                                        p["names"])
        b = theme.Button(self, "Draw contours on board")
        b.Bind(wx.EVT_BUTTON, lambda e: on_draw_contours(self.current(), self.selected_layer()))
        top.Add(b, 0, wx.RIGHT | wx.BOTTOM, 4)
        self.draw_button = b
        b2 = theme.Button(self, "Draw arrows on board")
        b2.SetToolTip("the current arrows of the selected layer, on the current-drawing User layer")
        b2.Bind(wx.EVT_BUTTON, lambda e: on_draw_arrows and on_draw_arrows(self.current(), self.selected_layer()))
        top.Add(b2, 0, wx.RIGHT | wx.BOTTOM, 4)
        self.arrow_button = b2
        b3 = theme.Button(self, "Open in window")
        b3.SetToolTip("the heatmap in a window of its own that you can enlarge or maximise; it follows the route, "
                      "layer and arrows chosen here. The toolbar zooms and pans (all layers together).")
        b3.Bind(wx.EVT_BUTTON, lambda e: self.open_window())
        top.Add(b3, 0, wx.RIGHT | wx.BOTTOM, 4)
        b6 = theme.Button(self, "Open 3D view")
        b6.SetToolTip("the layers stacked in 3D in your web browser: the current maps, the signal, the vias and parts "
                      "carrying the current between layers, and with plane capacitance the board-wide model's "
                      "displacement current between the planes. Drag to turn it. The page is written to "
                      f"{views_dir()} (it holds this board's maps and names) and overwritten for the same view.")
        b6.Bind(wx.EVT_BUTTON, lambda e: self.open_3d())
        top.Add(b6, 0, wx.RIGHT | wx.BOTTOM, 4)
        self.view3d_button = b6
        self._view3d = LatestOnly("groundtruth-3d")
        b4 = theme.Button(self, "Sharper map")
        b4.SetToolTip("solve the shown view again on a grid with 4x the nodes (about half the cell size), in the "
                      "background: about 20 s for a route, up to a minute for a power stage. The numbers above stay "
                      "as analysed (the default grid is converged for them); only the map gets sharper.")
        b4.Bind(wx.EVT_BUTTON, lambda e: on_sharper and on_sharper(self.choice.GetSelection()))
        top.Add(b4, 0, wx.RIGHT | wx.BOTTOM, 4)
        self.sharper_button = b4
        b5 = theme.Button(self, "Current capacity")
        b5.SetToolTip("per power stage of the last Simulate: the load current that brings the hottest copper or "
                      "part junction to thermal.capacity_c (default max_c), the other stages at their operating "
                      "points. A few thermal solves in the background; one view per stage at its capacity.")
        b5.Bind(wx.EVT_BUTTON, lambda e: on_capacity and on_capacity())
        top.Add(b5, 0, wx.BOTTOM, 4)
        self.capacity_button = b5
        self.window: Optional[HeatmapWindow] = None
        self.rules = None                       # the settings' rules: the tiles' colours (set_rules)
        self.tiles = wx.Panel(self)             # KPI tiles (ui/kpi.py), rebuilt per view
        self.tiles.SetSizer(wx.WrapSizer(wx.HORIZONTAL))
        self.metrics = wx.StaticText(self, label="Run the simulation to see the return current.")
        self.stale = ""             # why the views shown are out of date (MainFrame._stale_reason), or ""
        self.metrics.SetName("muted")
        self._top = top
        self.build_steps = [self._build_maps, self._build_plots]
        while self.build_steps and not in_steps:
            self.build_steps.pop(0)()

    def _build_maps(self):
        """The scrolling page and the maps' canvas with its toolbar (the first build step)."""
        # The plots on a page that scrolls, each as wide as the tab: the maps fill the first screen, the
        # cross-section and the spectrum follow below at heights of their own. The user: "instead of having
        # everything on one screen, make it scrollable and have every graph occupy the entire width of the
        # application". (On one screen the lower plots were too low for readable text on a laptop.) The wheel
        # scrolls the page; Ctrl + wheel zooms a map (PrerenderedCanvas._on_mouse_wheel).
        self.page = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.ALWAYS_SHOW_SB)
        self.page.SetScrollRate(0, SCROLL_PX)
        self.fig = Figure(figsize=(8, 5))
        self.canvas = PrerenderedCanvas(self.page, -1, self.fig)
        self.toolbar = ZoomToolbar(self.canvas, on_home=self.reset_zoom)
        self.toolbar.Reparent(self)                     # made on the canvas's parent: it stays above the page
        self.readout = wx.StaticText(self, label=ZOOM_HINT)    # the value under the mouse
        self.readout.SetName("muted")
        self.canvas.mpl_connect("motion_notify_event", self._hover)
        # Ctrl + wheel zoom (all layers), double-click back to the whole map: rendered again in the background
        self._view: Optional[tuple] = None
        self._connect_zoom(self.canvas)

    def _build_plots(self):
        """The cross-section's and the spectrum's canvases and the tab's layout (the last build step)."""
        top = self._top
        del self._top
        self.fig3 = Figure(figsize=(8, 2.6))           # the cross-section along the route (routes only)
        self.canvas3 = PrerenderedCanvas(self.page, -1, self.fig3)
        self.canvas3.mpl_connect("motion_notify_event", self._hover)
        self.fig2 = Figure(figsize=(8, 2.4))
        self.canvas2 = PrerenderedCanvas(self.page, -1, self.fig2)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(top, 0, wx.EXPAND | wx.ALL, 6)
        s.Add(self.tiles, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        s.Add(self.metrics, 0, wx.LEFT | wx.BOTTOM | wx.TOP, 6)
        tool_row = wx.BoxSizer(wx.HORIZONTAL)
        tool_row.Add(self.toolbar, 0, wx.EXPAND)
        tool_row.Add(self.readout, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        s.Add(tool_row, 0, wx.EXPAND)
        s.Add(self.page, 1, wx.EXPAND)
        plots = wx.BoxSizer(wx.VERTICAL)
        for c, h in ((self.canvas, MAPS_MIN_PX), (self.canvas3, 120), (self.canvas2, 120)):
            plots.Add(c, 0, wx.EXPAND | wx.BOTTOM, 6)
            c.SetMinSize((150, h))                      # heights set per view (_fit_page); widths follow the page
            c.defer = self.rendering
            c.page = self.page
        self.page.SetSizer(plots)
        self.canvas3.Hide()                             # until a route's view is shown
        self.SetSizer(s)
        self._page_size = (0, 0)
        self._resize_pending = False
        self.page.Bind(wx.EVT_SIZE, self._page_resized)
        self.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), self.Refresh()))   # drawn buttons repaint after a re-wrap

    def set_rules(self, rules):
        """The settings' rules, which judge the tiles' figures (ok / warn / error)."""
        self.rules = rules

    def _show_tiles(self, rr):
        """The view's KPI tiles. The tiles already there show the new figures (Tile.update); only missing ones are
        made and extra ones destroyed: making them all again for every view blocked the window 210-360 ms after
        a Simulate on refboard (window creation on the GUI thread, beside the render worker)."""
        from .kpi import kpis
        want = list(kpis(rr, self.rules))
        have = [t for t in self.tiles.GetChildren() if isinstance(t, theme.Tile)]
        self.tiles.Freeze()
        try:
            sizer = self.tiles.GetSizer()
            resized = len(want) != len(have)
            for tile, figures in zip(have, want):
                resized = tile.update(*figures) or resized
            for tile in have[len(want):]:
                sizer.Detach(tile)
                tile.Destroy()
            for figures in want[len(have):]:
                sizer.Add(theme.Tile(self.tiles, *figures), 0, wx.ALL, 3)
            for tile in self.tiles.GetChildren():           # the busiest via's tile zooms to it (focus_busiest)
                if isinstance(tile, theme.Tile):
                    tile.set_click(self.focus_busiest if tile.title == "busiest via" else None)
            self.tiles.SetToolTip(self._metrics(rr))
            if resized:
                self.Layout()
        finally:
            self.tiles.Thaw()

    def focus_busiest(self):
        """The "busiest via" tile was clicked: zoom the maps to that via (FOCUS_MM around it; its white ring marks
        it, plots._carriers) and, opened from KiCad, select it there as a click on its marker does (on_mark). The
        user: "how can I see which via is the busiest" (the tile named it only by its coordinates)."""
        from ..report.plots import busiest_via
        rr = self.current()
        v = busiest_via(rr) if rr is not None else None
        if v is None or rr.grid is None:
            return
        x, y = v["x"], v["y"]
        x0, x1, y0, y1 = rr.grid.extent
        half = FOCUS_MM / 2
        cx, cy = min(max(x, x0 + half), x1 - half), min(max(y, y0 + half), y1 - half)
        self._view = (cx - half, cx + half, cy - half, cy + half)
        self.redraw(maps_only=True)
        if self.on_mark is not None:
            rms = " RMS" if getattr(rr, "rms_of", None) is not None else ""
            self.on_mark("via", {**v, "kind": "via"}, f"{v['name']}: {v['current_a']:.3g} A{rms} (the busiest)",
                         self.selected_layer() or "")

    def tile_text(self) -> str:
        """The tiles' text (title value sub), for tests and the status line."""
        return " · ".join(f"{t.title} {t.value} {t.sub}".strip() for t in self.tiles.GetChildren()
                          if isinstance(t, theme.Tile))

    def _hover(self, event):
        """The value under the mouse (draw_heatmap records each map's grid and values on its figure); over an
        issue's circle or a via's or part's marker, what it is (also as the map's tooltip, in full)."""
        text, mark = "", ""
        maps = getattr(event.canvas.figure, "_gt_maps", None) or {}
        hit = maps.get(event.inaxes) if event.inaxes is not None else None
        if hit is not None and event.xdata is not None:
            from ..report.plots import map_value, mark_at
            mark = mark_at(event.canvas.figure, event.inaxes, event.x, event.y)
            text = mark if len(mark) <= READOUT_CHARS else mark[:READOUT_CHARS - 1] + "…"
            text = text or map_value(hit, event.xdata, event.ydata)
        tip = mark + ("\n(click: show it in KiCad)" if mark and self.on_mark is not None else "")
        if event.canvas.GetToolTipText() != tip:
            event.canvas.SetToolTip(tip)
        sec = (getattr(event.canvas.figure, "_gt_section", None) or {}).get(event.inaxes) \
            if event.inaxes is not None else None
        if sec is not None and event.xdata is not None:
            from ..report.plots import section_value
            text = section_value(sec, event.xdata)
        target = self.readout
        if self.window and event.canvas is self.window.canvas:
            target = self.window.readout
        elif not text:
            text = ZOOM_HINT                    # nothing under the mouse: how the wheel works on this page
        if target and target.GetLabel() != text:
            target.SetLabel(text)

    def _connect_zoom(self, canvas):
        """Wheel zoom and double-click reset on a canvas showing the maps."""
        canvas.mpl_connect("scroll_event", self._scroll)
        canvas.mpl_connect("button_press_event", self._press)

    def _scroll(self, event):
        """Wheel over a map: zoom about the point under the mouse; the maps are rendered again for that view in
        the background (with arrows laid out for it), so the window never waits for the drawing."""
        hit = (getattr(event.canvas.figure, "_gt_maps", None) or {}).get(event.inaxes)
        if hit is None or event.xdata is None or event.ydata is None:
            return
        full = hit[1].extent
        if self.rendering():                    # notches come faster than renders: build on the view asked for
            start = self._view or full
        else:                                   # what is shown (the toolbar may have panned it)
            ax = event.inaxes
            (x0, x1), (y0, y1) = sorted(ax.get_xlim()), sorted(ax.get_ylim())
            start = (x0, x1, y0, y1)
        step = 1 / ZOOM_STEP if event.button == "up" else ZOOM_STEP
        view = zoomed(start, full, event.xdata, event.ydata, step ** abs(getattr(event, "step", 1) or 1))
        if view != self._view:
            self._view = view
            self.redraw(maps_only=True)

    def _press(self, event):
        """A double click on a map (with no toolbar tool active) shows the whole map again; a single left click on
        a marker (an issue's circle, a via's or part's marker) shows what it marks in KiCad (on_mark)."""
        toolbar = getattr(event.canvas, "toolbar", None)
        if event.inaxes is None or (toolbar is not None and toolbar.mode):
            return
        if event.dblclick:
            if toolbar is not None:
                self.reset_zoom()
            return
        if event.button != 1 or self.on_mark is None:
            return
        from ..report.plots import mark_ref_at
        hit = mark_ref_at(event.canvas.figure, event.inaxes, event.x, event.y)
        maps = getattr(event.canvas.figure, "_gt_maps", None) or {}
        if hit is not None and event.inaxes in maps:
            kind, what, text = hit
            self.on_mark(kind, what, text, maps[event.inaxes][0])     # [0]: the map's layer

    def reset_zoom(self) -> bool:
        """Back to the whole map; True when there was a zoom to undo."""
        if self._view is None:
            return False
        self._view = None
        self.redraw(maps_only=True)
        return True

    def open_window(self):
        """The heatmap in a window of its own (raised if already open); it follows this tab's choices and is
        rendered by the same background thread."""
        if self.window:
            self.window.Raise()
            return
        self.window = HeatmapWindow(self, self._window_closed)
        self.window.canvas.defer = self.rendering
        self.window.toolbar.on_home = self.reset_zoom
        self.window.on_resized = self.redraw
        self.window.canvas.mpl_connect("motion_notify_event", self._hover)
        self._connect_zoom(self.window.canvas)
        self.window.Show()
        wx.CallAfter(self.redraw)               # once the window has its size

    def _window_closed(self):
        """The enlarged window closed: stop rendering for it."""
        self.window = None

    def open_3d(self):
        """Write the selected view's 3D page (report/view3d.py) and open it in the browser, in the background: the
        layer images take a second or two on a big board."""
        from ..report.view3d import has_view
        rr, board = self.current(), self.board
        if rr is None or board is None or not has_view(rr):
            self._status("this view has no current maps for a 3D view")
            return
        scale = "log" if self.log_scale.GetValue() else "linear"
        self.view3d_button.Disable()
        self._status(f"building the 3D view of {rr.net}…")

        def build():                            # worker thread: no wx except CallAfter
            """Write the page and open it in the default browser, then report back on the GUI thread."""
            try:
                from ..report.view3d import write_view3d
                path = write_view3d(board, rr, views_dir(), scale)
                opened = webbrowser.open(path.as_uri())
                wx.CallAfter(self._3d_done, None, path, opened)
            except Exception as e:              # noqa: BLE001 - shown to the user
                wx.CallAfter(self._3d_done, e, None, False)
        self._view3d.submit(build)

    def _3d_done(self, error, path, opened: bool):
        """(GUI thread) The 3D page is written (or failed): say so and enable the button again."""
        if not self:
            return
        from ..report.view3d import has_view
        rr = self.current()
        self.view3d_button.Enable(rr is not None and has_view(rr))
        if error is not None:
            self._status(f"could not build the 3D view: {error}")
        elif opened:
            self._status(f"3D view opened in the browser ({path})")
        else:
            self._status(f"3D view written to {path}: open it in a browser")

    def _status(self, text: str):
        """A message in the main window's status bar."""
        top = wx.GetTopLevelParent(self)
        if hasattr(top, "SetStatusText"):
            top.SetStatusText(text)

    def enable_drawing(self, on: bool):
        """Drawing on the board needs KiCad and no job running."""
        self.draw_button.Enable(on)
        self.arrow_button.Enable(on)

    def show(self, board, routes, issues):
        """A run's views (routes: every view the run made) and its issues (circled on the maps); the first view is
        shown."""
        self.board, self.routes, self.issues = board, routes, issues
        _set_items(self.choice, [r.label() if r.kind in ("thermal", "bridge", "pdn", "bode")
                                 else f"{r.net}  RMS currents" if getattr(r, "rms_of", None)
                                 else f"{r.net}  DC {r.view_of}" if getattr(r, "view_of", None)
                                 else f"{r.net}  {r.driver}→{r.receiver}" for r in routes])
        if routes:
            self.choice.SetSelection(0)
        self._route_changed()

    def _summary(self, rr) -> str:
        """The summary line of view rr, led by a warning while the views are out of date (set_stale)."""
        from ..analysis.sharper import describe
        from .kpi import notes
        text = "   ·   ".join(t for t in (describe(rr), notes(rr)) if t)
        if self.stale:
            text = f"OUT OF DATE ({self.stale} since this Simulate: Simulate again)   ·   " + text
        return text

    def set_stale(self, why: str):
        """Mark the views as out of date (why: "the board changed", ...) or current (""): the summary line says
        so at once, without drawing the maps again."""
        if why == self.stale:
            return
        self.stale = why
        rr = self.current()
        if rr is not None:
            self.metrics.SetLabel(self._summary(rr))

    def replace_view(self, k: int, view):
        """Put a recomputed view (a sharper map) in place of view k, and show it if it is the one selected."""
        if 0 <= k < len(self.routes):
            self.routes[k] = view
            if k == self.choice.GetSelection():
                self.redraw()

    def _route_changed(self):
        """Another view was chosen: its layers in the Layer list (by their share of the current), the options it
        allows, the whole map, and draw it."""
        rr = self.current()
        self.choice.SetToolTip(self.choice.GetStringSelection() or "the views of the last run")   # the full name
        share = getattr(rr, "layer_share", None) or {}      # a PDN rail has no maps (its curves instead)
        layers = sorted(share, key=lambda l: -share[l]) if rr else []
        _set_items(self.layer, ["main layers"] + [f"{l} ({share[l] * 100:.0f} %)" for l in layers])
        self._layers = layers
        self.layer.SetSelection(0)
        curves = rr is not None and rr.kind in CURVE_KINDS
        self.log_scale.Enable(rr is not None and not curves and getattr(rr, "heat_scale", "log") != "linear")
        # nothing to draw on the board, sharpen or load with current from curves
        for b in (self.draw_button, self.arrow_button, self.layer, self.sharper_button, self.capacity_button,
                  self.arrows, self.section_box):
            b.Enable(not curves)
        from ..report.view3d import has_view
        self.view3d_button.Enable(rr is not None and has_view(rr))
        self._view = None                           # another view: its whole map
        self.redraw()

    def current(self):
        """The view chosen in the Route list (None before a run)."""
        k = self.choice.GetSelection()
        return self.routes[k] if 0 <= k < len(self.routes) else None

    def selected_layer(self):
        """The chosen plane layer, or the one carrying most return current for 'main layers'."""
        k = self.layer.GetSelection()
        layers = getattr(self, "_layers", [])
        if k > 0:
            return layers[k - 1]
        return layers[0] if layers else None

    def redraw(self, maps_only: bool = False):
        """Show the selected view. The summary line changes at once; both figures are built and rendered in a
        background thread (~0.4 s for a 4-layer board, which would freeze the window) and swapped in when ready.
        Only the newest request is rendered when several come quickly. maps_only (a wheel zoom): only the maps,
        the tiles and the spectrum stay."""
        rr = self.current()
        if rr is None:
            return
        k = self.layer.GetSelection()
        layer, vectors, board, issues = self._layers[k - 1] if k > 0 else None, self.arrows.GetValue(), \
            self.board, self.issues
        scale = "log" if self.log_scale.GetValue() else "linear"
        view, max_vias, outlines, names = self._view, self.max_vias(), self.outlines, self.names
        if not maps_only:
            self._show_tiles(rr)
            text = self._summary(rr)
            if text != self.metrics.GetLabel():
                self.metrics.SetLabel(text)
            want = self.section_shown(rr)
            if want != self.canvas3.IsShown():
                self._blank_canvases()
                self.canvas3.Show(want)
            self._fit_page(rr, layer, vectors, scale)       # the canvases get their final size before it is read

        def heat(fig):
            """The maps of this view, with every choice copied above (the render thread must not read the GUI)."""
            if rr.kind == "pdn":                # a rail's PDN: its impedance curves instead of maps
                from ..report.plots import draw_pdn
                draw_pdn(fig, rr)
                return
            if rr.kind == "bode":               # responses (analysis/transfer.py): the Bode plot
                from ..report.plots import draw_bode
                draw_bode(fig, rr)
                return
            draw_heatmap(fig, board, rr, issues, layer=layer, vectors=vectors, scale=scale, view=view,
                         max_vias=max_vias, outlines=outlines, names=names)
        views = [(self.canvas, heat)]
        if not maps_only:
            if self.canvas3.IsShown():
                views.append((self.canvas3, lambda fig: draw_section(fig, board, rr)))
            if rr.kind == "pdn":
                from ..report.plots import draw_pdn_holds
                views.append((self.canvas2, lambda fig: draw_pdn_holds(fig, rr)))
            elif rr.kind == "bode":             # and their poles and zeros in the s-plane
                from ..report.plots import draw_pz
                views.append((self.canvas2, lambda fig: draw_pz(fig, rr)))
            else:
                views.append((self.canvas2, lambda fig: draw_spectrum(fig, rr)))
        if self.window:                         # the enlarged window shows the same view
            views.append((self.window.canvas, heat))
        sizes = [(c.figure.dpi, c.get_width_height(physical=True)) for c, _ in views]
        self._asked += 1
        asked = self._asked

        pal = theme.palette()                   # plain data: safe to use in the render thread

        def render():                           # worker thread: new figures only, nothing the GUI uses
            """Build each figure at its canvas's pixel size, render it offscreen (Agg), and hand them over."""
            done = []
            for (canvas, build), (dpi, (w, h)) in zip(views, sizes):
                fig = Figure(figsize=(max(w, 1) / dpi, max(h, 1) / dpi), dpi=dpi)
                agg = FigureCanvasAgg(fig)
                build(fig)
                theme.style_figure(fig, pal)
                agg.draw()
                done.append((canvas, fig, agg))
            wx.CallAfter(self._rendered, asked, done)
        self._renderer.submit(render)

    def _rendered(self, asked: int, done):
        """(GUI thread) Rendered figures arrived: swap them into their canvases, unless a newer request was made
        meanwhile (the generation counter `asked`), whose figures will follow."""
        if not self or asked < self._asked:     # the panel is gone, or a newer view is on its way
            return
        for canvas, fig, agg in done:
            if canvas:                          # the enlarged window may have been closed meanwhile
                canvas.show_rendered(fig, agg)
        self.fig = done[0][1]
        self.fig2 = next((fig for canvas, fig, _ in done if canvas is self.canvas2), self.fig2)
        self.fig3 = next((fig for canvas, fig, _ in done if canvas is self.canvas3), self.fig3)
        self.toolbar.update()                   # the zoom history belonged to the previous figure
        win = next((fig for canvas, fig, _ in done if self.window and canvas is self.window.canvas), None)
        if win is not None:
            self.window.fig = win
            self.window.toolbar.update()
        self._shown = asked

    def _render_failed(self, e: Exception):
        """A view could not be drawn: say so in the summary line (and stop waiting for it)."""
        if self:
            self._shown = self._asked
            self.metrics.SetLabel(f"could not draw this view: {e}")

    def _blank_canvases(self):
        """Before the cross-section appears or goes: the canvases may change size, and matplotlib's resize handler
        renders whatever figure a canvas holds on the GUI thread (the old maps with arrows: 0.75 s in
        test_jobs_run_off_the_gui_thread). They are all rendered again in the background right after, so they
        show nothing until then."""
        for c in (self.canvas, self.canvas3, self.canvas2):
            c.figure.clear()

    def _px(self, inches: float) -> int:
        """Inches of a figure on these canvases in window pixels (matplotlib's _on_size, the other way round)."""
        dpi = self.canvas.figure.dpi
        if wx.Platform != "__WXMSW__":
            dpi /= self.GetDPIScaleFactor()
        return max(int(round(inches * dpi)), 1)

    def _fit_page(self, rr, layer, vectors: bool, scale: str):
        """The plots' heights on the page: the maps fill what the page shows, or less when they are limited by its
        width (plots.heatmap_height_in: no empty bands above and below them); the cross-section (a route's, when
        ticked) and the spectrum have heights of their own (plots.section_height_in, SPECTRUM_IN); all as wide as
        the page. The page then scrolls over all of them."""
        from ..report.plots import heatmap_height_in, section_height_in
        w, h = self.page.GetClientSize()
        inch = self._px(1.0)
        if rr.kind in CURVE_KINDS:              # the curves: the page's height, as a map would get at most
            maps = max(h - 6, MAPS_MIN_PX)
        else:
            maps = self._px(heatmap_height_in(rr, w / inch, max(h - 6, MAPS_MIN_PX) / inch, layer, vectors, scale))
        heights = ((self.canvas, max(maps, MAPS_MIN_PX)),
                   (self.canvas3, self._px(section_height_in(self.board, rr)) if self.canvas3.IsShown() else 120),
                   (self.canvas2, self._px(SPECTRUM_IN)))
        for c, h in heights:
            if c.GetMinSize().height != h:
                c.SetMinSize((150, h))
        # one layout, without the canvases repainting at each step on the way (162 ms after a Simulate)
        self.page.Freeze()
        try:
            self.page.FitInside()
            self.page.Layout()
        finally:
            self.page.Thaw()

    def _page_resized(self, event):
        """The page changed size (the window was resized, or the tiles above it wrapped): the maps follow its
        height and every plot its width, so the view is rendered again in the background, once per burst of size
        events (the renderer keeps only the newest request). Until it arrives the canvases paint only their
        background (rendering() is True: see PrerenderedCanvas.defer), instead of matplotlib rendering the old
        figures at every new size on the GUI thread. No timer: wx's timers do not fire while the GUI tests wait
        with wx.Yield."""
        event.Skip()
        size = tuple(self.page.GetClientSize())
        if size == self._page_size:
            return
        self._page_size = size
        if self.current() is None or self._resize_pending:
            return
        self._resize_pending = True
        wx.CallAfter(self._after_resize)

    def _after_resize(self):
        """The burst of size events is over: render the view for the new size."""
        if not self:
            return
        self._resize_pending = False
        self.redraw()

    def max_vias(self) -> Optional[int]:
        """How many via markers a map gets at most: 0 without via markers, else Vias shown (None: all)."""
        return self.vias_limit if self.via_markers else 0

    def set_display(self, via_markers: bool, vias_limit: Optional[int], outlines: bool, names: bool = False):
        """The Preferences' heatmap settings; the view shown is drawn again with them."""
        self.via_markers, self.vias_limit, self.outlines, self.names = via_markers, vias_limit, outlines, names
        if self.current() is not None:
            self.redraw()

    def section_shown(self, rr=None) -> bool:
        """The cross-section is wanted (its box ticked) and the view has one (a signal route)."""
        rr = rr if rr is not None else self.current()
        if rr is not None and rr.kind == "bridge":      # its capacitors' ripple bars (not on the sweep)
            return bool(self.section_box.GetValue() and getattr(rr, "caps", None) and rr.view != "sweep")
        return bool(self.section_box.GetValue() and rr is not None and getattr(rr, "section", None)
                    and rr.kind not in ("converter", "thermal"))

    def rendering(self) -> bool:
        """A requested view is not on screen yet (or the page was resized and the view is about to be rendered
        again for its new size)."""
        return self._shown < self._asked or self._resize_pending

    @staticmethod
    def _metrics(rr) -> str:
        """The view's figures as text (the tiles' tooltip): return layers, main transfer, plane displacement, loop
        inductance, moment and margin for a route; a power stage's or a bridge's own summary."""
        if rr.error:
            return rr.error
        if rr.kind in ("converter", "dcload"):
            return ResultsPanel._converter_metrics(rr)
        if rr.kind == "pdn":
            from ..report.formats import pdn_text
            return "\n".join(pdn_text(rr))
        if rr.kind == "bode":
            from ..report.formats import response_lines
            return "\n".join(line for r in rr.responses for line in response_lines(r))
        if rr.kind == "bridge":
            from ..report.formats import bridge_text, sweep_text
            return "\n".join(sweep_text(rr) if rr.view == "sweep" else bridge_text(rr))
        parts = ["return on " + ", ".join(f"{l} {s * 100:.0f} %" for l, s in
                                          sorted(rr.layer_share.items(), key=lambda kv: -kv[1]) if s >= 0.05)]
        from ..analysis.simulate import displacement, point_transfers
        from ..report.formats import share
        t = next(iter(point_transfers(rr.transfers)), None)
        if t is not None:
            parts.append(f"main transfer {t['name']} ({share(t['fraction'])}, {t['distance_mm']:.1f} mm away)")
        t = displacement(rr.transfers)
        if t is not None:
            parts.append(f"plane displacement {'–'.join(t['layers'])} {share(t['fraction'])} @ "
                         f"{t.get('f', 0) / 1e6:.4g} MHz")
        if rr.solves:
            parts.append(f"loop L {rr.solves[-1]['L_nH']:.2f} nH")
        if rr.l_ratio:
            parts.append(f"{rr.l_ratio:.2f}× ideal")
        if rr.m_excess_db is not None:
            parts.append(f"moment {rr.m_excess_db:+.1f} dB vs ideal")
        if rr.worst:
            parts.append(f"worst margin {rr.worst['margin_db']:+.1f} dB @ {rr.worst['f'] / 1e6:.4g} MHz")
        return "   ·   ".join(parts) + ("\n" + "; ".join(rr.notes) if rr.notes else "")

    @staticmethod
    def _converter_metrics(c) -> str:
        """A power stage's figures as text: its RMS view, a DC path, or the hot loop with ringing, input
        capacitors and margins."""
        if getattr(c, "rms_of", None):
            from ..report.formats import rms_text
            return "   ·   ".join(rms_text(c.rms_of))
        if getattr(c, "view_of", None):
            p = next(x for x in c.dc if x["name"] == c.view_of)
            via = p["vias"][0] if p["vias"] else None
            if p.get("combined"):            # every stage's DC paths at once (power.combine_dc)
                return "   ·   ".join([f"all power stages together: {p['loads']}", f"{p['current_a']:.3g} A in",
                                       f"copper loss {p['loss_w']:.2f} W",
                                       f"{p['sustained_a_mm2']:.0f} A/mm² sustained (peak {p['peak_a_mm2']:.0f}) on "
                                       f"{p['sustained_layer']}"] +
                                      ([f"{via['name']}: {via['current_a']:.2f} A, {via['a_mm2']:.0f} A/mm²"]
                                       if via else []))
            from ..report.formats import dc_resistance
            if c.kind == "dcload":           # a DC rail with loads: the drop each load sees
                return "   ·   ".join([f"DC load {p['current_a']:.3g} A from {c.driver}",
                                       f"copper loss {p['loss_w'] * 1e3:.0f} mW",
                                       f"{p['sustained_a_mm2']:.0f} A/mm² sustained (peak {p['peak_a_mm2']:.0f}) on "
                                       f"{p['sustained_layer']}"] +
                                      [f"{r['pin']}: {r['current_a']:.3g} A, drop {r['drop_mv']:.1f} mV" for r in
                                       c.loads])
            return "   ·   ".join([f"DC {p['name']}: {p['current_a']:.3g} A", f"{dc_resistance(p)}, {p['drop_mv']:.1f} mV, "
                                   f"{p['loss_w'] * 1e3:.0f} mW",
                                   f"{p['sustained_a_mm2']:.0f} A/mm² sustained (peak {p['peak_a_mm2']:.0f}) on "
                                   f"{p['sustained_layer']}"] +
                                  ([f"{via['name']}: {via['current_a']:.2f} A, {via['a_mm2']:.0f} A/mm²"] if via else []))
        parts = [f"hot loop {c.loop_nh:.2f} nH ({c.loop_mohm:.0f} mΩ) at {c.loop_f / 1e6:g} MHz",
                 f"L·di/dt {c.overshoot_v:.1f} V" + (f" ({c.overshoot_v / c.v_in * 100:.0f} % of {c.v_in:g} V)"
                                                     if c.v_in else "")]
        if c.ringing:
            parts.append(f"rings at {c.ringing['f'] / 1e6:.0f} MHz, Q {c.ringing['q']:.1f}")
        if getattr(c, "input", None):          # output power plus losses (power.input_estimate)
            parts.append(f"input {c.input['i_in_a']:.3g} A, efficiency {c.input['efficiency'] * 100:.1f} %")
        caps = [x for x in c.caps if not x["lisn"]][:3]
        if caps:
            parts.append("caps " + ", ".join(f"{x['name']} {x['share_hf'] * 100:.0f} %" for x in caps))
        if c.conducted_worst:
            parts.append(f"conducted margin {c.conducted_worst['margin_db']:+.1f} dB @ "
                         f"{c.conducted_worst['f'] / 1e6:.4g} MHz")
        if getattr(c, "ce101_worst", None):
            parts.append(f"CE101 margin {c.ce101_worst['margin_db']:+.1f} dB @ {c.ce101_worst['f']:.4g} Hz")
        if c.worst:
            parts.append(f"radiated margin {c.worst['margin_db']:+.1f} dB @ {c.worst['f'] / 1e6:.4g} MHz")
        if getattr(c, "interleave", None):   # its interleave group, the stages together (analysis/interleave.py)
            from ..analysis.interleave import interleave_text
            parts += interleave_text(c.interleave)
        if c.cm:
            parts.append(f"common-mode budget ≤ {c.cm['budget_pf']:.2g} pF switch node to chassis")
        return "   ·   ".join(parts) + ("\n" + "; ".join(c.notes) if c.notes else "")
