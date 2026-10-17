"""Preferences (top bar, next to Help): the settings that are the user's on this computer rather than a board's
(those live in the board's settings file): Simulate's resolution, solver threads and GPU, the thermal quality, how
the heatmaps mark vias and parts, the report's author and company, the simulation history, the compute server, the theme. Kept in
ui.json (theme.prefs); the working toggles (current arrows, log scale, cross-section, zoom and highlight in KiCad)
remember their state there too (remember)."""
from __future__ import annotations

import math
from typing import Optional

import wx

from . import theme

VIAS_SHOWN = ("All", "5", "10", "20", "50")      # the via markers per map at most
FROM_FILE = "From the settings file"
THREADS = (FROM_FILE, "auto", "1", "2", "4", "8", "16", "32")
GPU = (FROM_FILE, "auto", "on", "off")
QUALITY = (FROM_FILE, "draft", "normal", "fine")
GAP = 6                  # the dialog's spacing unit (DIP): rows and labels one apart, groups and margins two
MIN_PANE_DIP = 200       # the scrolled pane of groups is never shorter (the dialog's minimum height)


def remember(ctrl: wx.CheckBox, key: str) -> None:
    """A checkbox that keeps its state for next time (ui.json): its saved value now, every change saved (bound
    after the control's own handler, so it runs first and lets that one run too)."""
    saved = theme.prefs().get(key)
    if isinstance(saved, bool):
        ctrl.SetValue(saved)

    def save(evt):
        """Store the new state and let the control's own handler run too."""
        theme.save_prefs(**{key: ctrl.GetValue()})
        evt.Skip()
    ctrl.Bind(wx.EVT_CHECKBOX, save)


def overrides(prefs: dict) -> dict:
    """The analysis and thermal settings this computer's preferences replace (an empty dict: the file's)."""
    p = defaults(prefs)
    out = {}
    if p["threads"] != FROM_FILE:
        out["threads"] = "auto" if p["threads"] == "auto" else int(p["threads"])
    if p["gpu"] != FROM_FILE:
        out["gpu"] = p["gpu"]
    if p["all_cores"]:
        out["all_cores"] = True
    return out


def resolution_label(factor: int, cell_mm: Optional[float] = None) -> str:
    """'4×  (0.21 × 0.21 mm)': a Resolution entry, with its cell size once a Simulate has shown it."""
    text = f"{factor}×" + (" standard" if factor == 1 else "")
    return text + (f"  ({cell_mm:.2f} × {cell_mm:.2f} mm)" if cell_mm is not None else "")


ARROW_MIN_MM, ARROW_MAX_MM = 0.01, 0.3      # line widths of the arrows drawn on the board (the user's defaults)


def _arrow_range(prefs: dict) -> dict:
    """The board arrows' thinnest and thickest line (mm), each from 0.001 to 2 mm (else its default), the thickest
    at least the thinnest."""
    def mm(key, default):
        """A width from the preferences, or the default when missing or out of range."""
        v = prefs.get(key)
        return float(v) if isinstance(v, (int, float)) and 0.001 <= v <= 2.0 else default
    lo, hi = mm("arrow_min_mm", ARROW_MIN_MM), mm("arrow_max_mm", ARROW_MAX_MM)
    return {"arrow_min_mm": min(lo, hi), "arrow_max_mm": max(lo, hi)}


CONTOUR_LINE_MM, CONTOUR_INDEX_MM = 0.04, 0.1   # Draw contours on board: the lines and the index lines (1, 10, 100)


def _contour_widths(prefs: dict) -> dict:
    """The board contours' line widths (mm): the ordinary lines and the index lines, each from 0.001 to 2 mm (else
    its default)."""
    def mm(key, default):
        """A width from the preferences, or the default when missing or out of range."""
        v = prefs.get(key)
        return float(v) if isinstance(v, (int, float)) and 0.001 <= v <= 2.0 else default
    return {"contour_line_mm": mm("contour_line_mm", CONTOUR_LINE_MM),
            "contour_index_mm": mm("contour_index_mm", CONTOUR_INDEX_MM)}


def defaults(prefs: dict) -> dict:
    """The preferences with their defaults filled in."""
    from ..analysis.simulate import RESOLUTIONS
    vias = prefs.get("max_vias")
    return {"resolution": prefs.get("resolution", 1) if prefs.get("resolution", 1) in RESOLUTIONS else 1,
            "threads": str(prefs.get("threads")) if str(prefs.get("threads")) in THREADS else FROM_FILE,
            "gpu": prefs.get("gpu") if prefs.get("gpu") in GPU else FROM_FILE,
            "all_cores": bool(prefs.get("all_cores", False)),
            "thermal_quality": prefs.get("thermal_quality") if prefs.get("thermal_quality") in QUALITY else FROM_FILE,
            "via_markers": bool(prefs.get("via_markers", True)),
            "max_vias": vias if isinstance(vias, int) and vias > 0 else None,
            "outlines": bool(prefs.get("outlines", False)),
            # the references came with the outlines before they had a switch of their own
            "names": bool(prefs.get("names", prefs.get("outlines", False))),
            "author": str(prefs.get("author", "") or ""),
            "company": str(prefs.get("company", "") or ""),
            "history": bool(prefs.get("history", True)),
            "history_report": bool(prefs.get("history_report", True)),
            **_arrow_range(prefs),
            **_contour_widths(prefs),
            "compute": "server" if prefs.get("compute") == "server" else "local",
            "server": str(prefs.get("server", "") or ""),
            "server_key": str(prefs.get("server_key", "") or ""),
            "theme": prefs.get("theme", "system") if prefs.get("theme") in theme.THEMES else "system"}


COMPUTE = ("This computer", "A GroundTruth compute server")
ALL_CORES_INFO = (
    "Use all cores\n\n"
    "What it does:\n"
    "- Solver threads 'auto' uses every physical core of the computer that solves (normally at most 4).\n"
    "- After the first frequencies, Simulate refines the spectrum one frequency per net or power stage and round. "
    "With this on it takes up to 4 at once while cores would otherwise wait, and its solve budget grows as much, "
    "so the extra frequencies come on top of the usual ones.\n\n"
    "When it helps: big machines (8 or more cores) with few nets or power stages, at high resolution. With many "
    "nets and stages the cores are busy anyway and it changes little.\n\n"
    "Potential issues:\n"
    "- Results are no longer exactly reproducible across computers: which frequencies get solved depends on how "
    "many cores there are. Figures normally agree within a fraction of a dB; near a sharp resonance a margin can "
    "differ more. Compare runs (History tab) only when both used the same setting on the same machine.\n"
    "- More solves in total (up to 4x the refinement): more CPU time, heat and power. On a laptop it competes with "
    "KiCad and everything else, and the fans run.\n"
    "- More memory at once (a factorisation per solve running). GroundTruth still lowers the threads when memory is "
    "short, but there is less room for other programs.\n"
    "- It can be slower when the cores are busy with other work: the extra solves then queue instead of filling "
    "idle cores.\n"
    "- On a compute server the server's own 'threads' setting still applies; in a cluster each server uses its "
    "own core count, so the parts of one run may refine differently.\n\n"
    "Off (the default): the same frequencies and the same figures on every computer.")


def compute_server(prefs: dict) -> Optional[tuple[str, str]]:
    """(address, access key) when the preferences send the analyses to a compute server, else None."""
    p = defaults(prefs)
    return (p["server"], p["server_key"]) if p["compute"] == "server" else None


class ServersPanel(wx.Panel):
    """Preferences › Compute server › Servers: the configured server and the servers its cluster names, each with
    its version against this plugin's (ui/server_updates.py), and Update / Update all for the out-of-date ones that
    take signed remote updates (remote/release.py). Every request runs in a background thread; following an update
    ends when the dialog closes (the server carries on by itself)."""

    COLS = (("Server", 130), ("Version", 140), ("State", 300))

    def __init__(self, parent, settings):
        """`settings()`: (the server address, the access key) as typed in the dialog now."""
        import threading
        from ..remote.release import bundled
        super().__init__(parent)
        self.settings = settings
        self.rows: list[dict] = []
        self.busy: set[str] = set()                 # addresses being updated
        self.stop = threading.Event()
        self.bundle = bundled()
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
                                size=self.FromDIP(wx.Size(sum(w for _, w in self.COLS) + 10, 104)))
        for i, (title, w) in enumerate(self.COLS):
            self.list.InsertColumn(i, title, width=self.FromDIP(w))
        self.check_b = wx.Button(self, label="Check")
        self.check_b.SetToolTip("ask every server for its version")
        self.update_b = wx.Button(self, label="Update")
        self.update_b.SetToolTip("update the selected server to this plugin's version")
        self.all_b = wx.Button(self, label="Update all")
        self.all_b.SetToolTip("update every out-of-date server, one after the other")
        self.note = wx.StaticText(self, label="")
        self.note.SetName("muted")
        self.check_b.Bind(wx.EVT_BUTTON, lambda e: self.start_check())
        self.update_b.Bind(wx.EVT_BUTTON, lambda e: self.on_update(all_=False))
        self.all_b.Bind(wx.EVT_BUTTON, lambda e: self.on_update(all_=True))
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: self._enable())
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for b in (self.check_b, self.update_b, self.all_b):
            buttons.Add(b, 0, wx.RIGHT, self.FromDIP(GAP))
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self.list, 1, wx.EXPAND)
        s.Add(buttons, 0, wx.TOP, self.FromDIP(GAP))
        s.Add(self.note, 0, wx.TOP | wx.EXPAND, self.FromDIP(GAP // 2))
        self.SetSizer(s)
        self._enable()

    def _destroyed(self, evt):
        """The dialog closes: stop following updates (EVT_WINDOW_DESTROY bubbles up from children: ours only)."""
        evt.Skip()
        if evt.GetEventObject() is self:
            self.stop.set()

    def start_check(self):
        """Ask every server for its version (background); the list fills when they have answered."""
        import threading
        from .server_updates import check
        address, key = self.settings()
        if not address or not key:
            self.note.SetLabel("Enter the server's address and access key first.")
            return
        self.note.SetLabel("Asking the servers…")
        self.check_b.Disable()

        def ask():
            """(background) The rows, or why not."""
            try:
                rows, error = check(address, key), ""
            except Exception as e:                  # noqa: BLE001 - shown in the note
                rows, error = [], str(e)
            wx.CallAfter(self._show, rows, error)
        threading.Thread(target=ask, name="gt-server-versions", daemon=True).start()

    def _show(self, rows: list, error: str):
        """(GUI thread) The servers in the list, the out-of-date ones marked; a note on what to do."""
        if not self:
            return
        self.check_b.Enable()
        self.rows = rows
        self.list.DeleteAllItems()
        warn = theme.colour(theme.palette().warn)
        for r in rows:
            i = self.list.InsertItem(self.list.GetItemCount(), r["name"])
            self.list.SetItem(i, 1, r["version"])
            self.list.SetItem(i, 2, r["text"])
            if r["state"] in ("older", "unreachable"):
                self.list.SetItemTextColour(i, warn)
        old = [r for r in rows if r["state"] == "older"]
        if error:
            self.note.SetLabel(error)
        elif not old:
            self.note.SetLabel("Every server runs this plugin's version." if rows else "")
        elif self.bundle is None:
            self.note.SetLabel(f"{len(old)} server(s) out of date. This plugin carries no server package (a "
                               "development copy): update them with the server package, or install a built plugin.")
        else:
            self.note.SetLabel(f"{len(old)} server(s) out of date; Update installs this plugin's version on them "
                               "(their running jobs finish first).")
        self.note.Wrap(max(200, self.list.GetSize().width))
        self.GetParent().Layout()
        self._enable()

    def _enable(self):
        """Update: a selected out-of-date server that takes remote updates; Update all: any such server; neither
        without the plugin's server package or while updating."""
        can = [r for r in self.rows if r["can_update"] and r["address"] not in self.busy]
        sel = self.list.GetFirstSelected()
        self.update_b.Enable(bool(self.bundle) and 0 <= sel < len(self.rows) and self.rows[sel] in can)
        self.all_b.Enable(bool(self.bundle) and bool(can))

    def on_update(self, all_: bool):
        """Update the selected (or every) out-of-date server, after asking, one after the other in the background;
        each row says how far its update is."""
        import threading
        from .server_updates import update
        sel = self.list.GetFirstSelected()
        targets = [r for r in self.rows if r["can_update"] and r["address"] not in self.busy] if all_ else \
            ([self.rows[sel]] if 0 <= sel < len(self.rows) and self.rows[sel]["can_update"] else [])
        if not targets or self.bundle is None:
            return
        from . import server_info
        if not server_info.confirm_update(self, targets, self.bundle["version"]):
            return
        _, key = self.settings()
        self.busy |= {r["address"] for r in targets}
        self._enable()
        bundle = self.bundle

        def work():
            """(background) One server after the other; each row told every step."""
            for r in targets:
                done, text = update(r["address"], key, bundle,
                                    lambda t, a=r["address"]: wx.CallAfter(self._row_text, a, t), stop=self.stop)
                wx.CallAfter(self._row_text, r["address"], text)
                if self.stop.is_set():
                    return
            wx.CallAfter(self._updated, [r["address"] for r in targets])
        threading.Thread(target=work, name="gt-server-update", daemon=True).start()

    def _row_text(self, address: str, text: str):
        """(GUI thread) A server's State cell."""
        if not self:
            return
        for i, r in enumerate(self.rows):
            if r["address"] == address:
                self.list.SetItem(i, 2, text)

    def _updated(self, addresses: list):
        """(GUI thread) The updates are over: ask every server again."""
        if not self:
            return
        self.busy -= set(addresses)
        self.start_check()


class PreferencesDialog(wx.Dialog):
    """cell_mm: the 1× cell of the last Simulate's coarsest model (None before one): each Resolution entry shows
    its own."""

    def __init__(self, parent, prefs: dict, cell_mm: Optional[float] = None):
        """The dialog with `prefs` (ui.json; missing entries at their defaults) in its controls, in groups of
        label/control rows laid out in two columns on a pane that scrolls when the screen is shorter than the
        dialog (the user, on a laptop: "settings space is tight. make it a double row and add some vertical scroll
        bars for smaller screens"). Nothing is saved here: the caller reads values() after OK."""
        super().__init__(parent, title="Preferences", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        from ..analysis.simulate import RESOLUTION_MIN_MM, RESOLUTIONS
        p = defaults(prefs)
        gap = self.FromDIP(GAP)
        root = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(self, label="Your settings on this computer, for every board. A board's own settings "
                                          "(nets, converters, rules) are in its settings file. Hover over a setting "
                                          "for what it does.")
        intro.SetName("muted")
        root.Add(intro, 0, wx.LEFT | wx.RIGHT | wx.TOP, 2 * gap)
        root.AddSpacer(gap)

        # the groups sit in two columns on a scrolled pane; the columns share the width equally
        pane = wx.ScrolledWindow(self, style=wx.VSCROLL)
        pane.SetScrollRate(0, self.FromDIP(20))
        columns = wx.BoxSizer(wx.HORIZONTAL)
        cols = {"left": wx.BoxSizer(wx.VERTICAL), "right": wx.BoxSizer(wx.VERTICAL)}
        columns.Add(cols["left"], 1, wx.EXPAND | wx.LEFT, 2 * gap)
        columns.Add(cols["right"], 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 2 * gap)
        boxes = {}                          # grid -> the static box it sits in (its rows' parent)
        labels: dict[str, list] = {"left": [], "right": []}     # per column, to line their controls up

        def group(title: str, side: str) -> wx.FlexGridSizer:
            """A titled box with a two-column grid (labels, controls), added to the `side` column."""
            box = wx.StaticBoxSizer(wx.VERTICAL, pane, title)
            grid = wx.FlexGridSizer(2, gap, 2 * gap)
            grid.AddGrowableCol(1)
            box.Add(grid, 1, wx.EXPAND | wx.ALL, gap)
            cols[side].Add(box, 0, wx.EXPAND | wx.BOTTOM, 2 * gap)
            boxes[id(grid)] = (box.GetStaticBox(), side)
            return grid

        def row(grid, label: str, ctrl, tip: str):
            """A label and its control in `grid`, both with the explanation as tooltip. Both belong to the group's
            static box: wx 3.3 wants a StaticBoxSizer's items to be children of its box (it logs every other one
            as an error)."""
            sb, side = boxes[id(grid)]
            ctrl.Reparent(sb)
            text = wx.StaticText(sb, label=label)
            text.SetToolTip(tip)
            ctrl.SetToolTip(tip)
            labels[side].append(text)
            grid.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.EXPAND)

        sim = group("Simulation", "left")
        self.resolution = wx.Choice(self, choices=[
            resolution_label(r, None if cell_mm is None else max(cell_mm / math.sqrt(r), RESOLUTION_MIN_MM))
            for r in RESOLUTIONS])
        self.resolution.SetSelection(RESOLUTIONS.index(p["resolution"]))
        row(sim, "Resolution", self.resolution,
            "every model gets this many times the nodes, its cells 1/√ of that in size (the sizes in brackets are "
            "the coarsest model's of the last Simulate of this board, shown once one has run; smaller model regions "
            "get finer cells). Memory and time grow about as "
            "much as the nodes: 2× to 4× on a laptop, 8× and up on a machine with lots of memory. A solve that runs "
            "out of memory is retried alone, then reported. Sharper map goes one step finer than what was "
            "simulated. Applies from the next Simulate.")
        self.threads = wx.Choice(self, choices=list(THREADS))
        self.threads.SetStringSelection(p["threads"])
        row(sim, "Solver threads", self.threads,
            "how many models are solved at once: auto = the physical cores (at most 4). More is faster on a big "
            "machine but needs a model's memory each; fewer keeps a busy laptop responsive and within its memory. "
            "From the settings file: its analysis.threads.")
        self.gpu = wx.Choice(self, choices=list(GPU))
        self.gpu.SetStringSelection(p["gpu"])
        row(sim, "GPU", self.gpu,
            "the solver's iterations on a graphics card (PyTorch with CUDA or AMD ROCm, see Help): auto uses one if this "
            "computer has one, on refuses to run without it, off never uses it. From the settings file: its "
            "analysis.gpu.")
        # "Use all cores": opt-in, with an info button on what it costs (the user: "make it an opt-in 'use all
        # cores' setting. and add an info icon to explain its potential issues")
        holder = wx.Panel(self)
        hs = wx.BoxSizer(wx.HORIZONTAL)
        self.all_cores = wx.CheckBox(holder, label="Use every core (faster on big machines)")
        self.all_cores.SetValue(p["all_cores"])
        info = wx.BitmapButton(holder, bitmap=wx.ArtProvider.GetBitmap(wx.ART_INFORMATION, wx.ART_BUTTON,
                                                                        self.FromDIP(wx.Size(16, 16))),
                               style=wx.BORDER_NONE)
        info.SetToolTip("What 'Use all cores' changes, and its potential issues")
        info.Bind(wx.EVT_BUTTON, lambda e: wx.MessageBox(ALL_CORES_INFO, "Use all cores", wx.ICON_INFORMATION,
                                                         self))
        hs.Add(self.all_cores, 0, wx.ALIGN_CENTER_VERTICAL)
        hs.Add(info, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(6))
        holder.SetSizer(hs)
        row(sim, "All cores", holder,
            "Solver threads 'auto' uses every physical core (not at most 4) and the refinement solves several "
            "frequencies at once while cores would idle; off: the same result on every computer. Press (i) for its "
            "potential issues.")

        comp = group("Compute server", "left")
        self.compute = wx.Choice(self, choices=list(COMPUTE))
        self.compute.SetSelection(1 if p["compute"] == "server" else 0)
        row(comp, "Run analyses on", self.compute,
            "Simulate, Suggest fixes, Check in Simulate, Compare what-if, Thermal and Current capacity run on this "
            "computer, or on a powerful PC on the local network running 'groundtruth serve' (see Help). The board is "
            "still read here, and the results show here as usual. If the server can't be reached you are asked "
            "whether to run the job here instead.")
        self.server = wx.TextCtrl(self, value=p["server"])
        self.server.SetHint("e.g. 192.168.1.20 or bigpc:8765")
        row(comp, "Server address", self.server, "the compute server's name or IP address on the local network, "
                                                 "optionally :port (default 8765)")
        self.server_key = wx.TextCtrl(self, value=p["server_key"], style=wx.TE_PASSWORD)
        row(comp, "Access key", self.server_key,
            "the key the server's window shows when it starts (groundtruth_server.cmd). Every job and result is "
            "signed with it; kept in this computer's ui.json")
        self.test = wx.Button(self, label="Test connection")
        row(comp, "Connection", self.test, "ask the server for its version, GPUs, memory and queue")
        self.test.Bind(wx.EVT_BUTTON, self.on_test)
        self.find = wx.Button(self, label="Find servers")
        row(comp, "On this network", self.find,
            "list the compute servers on the local network that use this access key (the cluster's lead first) and "
            "pick one for the address. Servers on another subnet aren't found: type their address")
        self.find.Bind(wx.EVT_BUTTON, self.on_find)
        # every server's version against this plugin's, with remote updates (the user: "it will show the user which
        # server is out of date and offers to update it through a button press")
        self.servers = ServersPanel(self, lambda: (self.server.GetValue().strip(), self.server_key.GetValue().strip()))
        row(comp, "Servers", self.servers,
            "the server above and the servers of its cluster, each with its version against this plugin's. Update "
            "installs this plugin's version on an out-of-date server that takes remote updates (turned on at its "
            "install; only packages signed with the release key of its last manual install are taken); its running "
            "jobs finish first. Check asks them again.")

        def server_fields(_=None):
            """The address and key only matter for the server."""
            on = self.compute.GetSelection() == 1
            for ctrl in (self.server, self.server_key, self.test, self.find, self.servers):
                ctrl.Enable(on)
        self.compute.Bind(wx.EVT_CHOICE, server_fields)
        server_fields()
        if self.compute.GetSelection() == 1 and p["server"] and p["server_key"]:
            wx.CallAfter(lambda: self and self.servers.start_check())      # once the dialog shows

        therm = group("Thermal", "left")
        self.thermal_quality = wx.Choice(self, choices=list(QUALITY))
        self.thermal_quality.SetStringSelection(p["thermal_quality"])
        row(therm, "Quality", self.thermal_quality,
            "the thermal mesh: draft is quick, fine is detailed and needs more memory (the solve stops with a "
            "message above 60 % of this computer's memory). From the settings file: its thermal.quality.")

        maps = group("Heatmaps", "right")
        self.via_markers = wx.CheckBox(self, label="Mark the vias carrying the current (▲)")
        self.via_markers.SetValue(p["via_markers"])
        row(maps, "Via markers", self.via_markers,
            "the ▲ on every via with at least 1 % of the current, with its share from 5 % up; unticked: none "
            "(parts' ■ and issue circles stay). The checks look at every via either way.")
        self.vias_shown = wx.Choice(self, choices=list(VIAS_SHOWN))
        self.vias_shown.SetStringSelection(str(p["max_vias"]) if p["max_vias"] else "All")
        row(maps, "Vias shown", self.vias_shown, "at most this many via markers per map, the busiest first")
        self.via_markers.Bind(wx.EVT_CHECKBOX, lambda e: self.vias_shown.Enable(self.via_markers.GetValue()))
        self.vias_shown.Enable(p["via_markers"])
        self.outlines = wx.CheckBox(self, label="Draw every part's outline over the maps")
        self.outlines.SetValue(p["outlines"])
        row(maps, "Part outlines", self.outlines,
            "its courtyard, else its fabrication outline, else a box around its pads; top-side parts solid, "
            "bottom-side dashed")
        self.names = wx.CheckBox(self, label="Write every part's reference over the maps")
        self.names.SetValue(p["names"])
        row(maps, "Part names", self.names, "at the middle of its outline (also without the outlines)")

        board = group("Drawing on the board", "right")
        why = ("Draw arrows on board: each arrow's line width follows the current density there, on the heatmap's "
               "log scale: the thickest at the result's peak, the thinnest three decades below it and lower")
        self.arrow_min = wx.SpinCtrlDouble(self, min=0.001, max=2.0, inc=0.01, initial=p["arrow_min_mm"])
        self.arrow_max = wx.SpinCtrlDouble(self, min=0.001, max=2.0, inc=0.01, initial=p["arrow_max_mm"])
        for ctrl in (self.arrow_min, self.arrow_max):
            ctrl.SetDigits(3)
        row(board, "Thinnest arrow line (mm)", self.arrow_min, why)
        row(board, "Thickest arrow line (mm)", self.arrow_max, why)
        self.contour_line = wx.SpinCtrlDouble(self, min=0.001, max=2.0, inc=0.01, initial=p["contour_line_mm"])
        self.contour_index = wx.SpinCtrlDouble(self, min=0.001, max=2.0, inc=0.01, initial=p["contour_index_mm"])
        for ctrl in (self.contour_line, self.contour_index):
            ctrl.SetDigits(3)
        row(board, "Contour line (mm)", self.contour_line,
            "Draw contours on board: the width of the contour lines (2, 5, 20, 50 ...)")
        row(board, "Index contour line (mm)", self.contour_index,
            "Draw contours on board: the width of the index lines, every decade (1, 10, 100 ...) or every fifth "
            "isotherm, drawn thicker like a map's index contours")

        report = group("Report", "left")
        self.author = wx.TextCtrl(self, value=p["author"])
        row(report, "Author", self.author, "the exported report's 'Prepared by' (a board's own report.author wins)")
        self.company = wx.TextCtrl(self, value=p["company"])
        row(report, "Company", self.company, "the exported report's company (a board's own report.company wins)")

        hist = group("History", "right")
        self.history = wx.CheckBox(self, label="Keep every Simulate in the board's groundtruth\\history folder")
        self.history.SetValue(p["history"])
        row(hist, "Keep runs", self.history,
            "after each Simulate: a copy of the board as simulated (with unsaved edits; boards that did not change "
            "share one zipped copy), the settings, the results; listed and compared on the History tab. The folder "
            "is next to the board: groundtruth/history/")
        self.history_report = wx.CheckBox(self, label="With the report page")
        self.history_report.SetValue(p["history_report"])
        row(hist, "Report", self.history_report,
            "also the run's HTML report (as Export report writes it), made in the background after the results "
            "show: a few MB per run")
        self.history.Bind(wx.EVT_CHECKBOX, lambda e: self.history_report.Enable(self.history.GetValue()))
        self.history_report.Enable(p["history"])

        look = group("Appearance", "right")
        self.theme = wx.Choice(self, choices=["System", "Light", "Dark"])
        self.theme.SetSelection(theme.THEMES.index(p["theme"]))
        row(look, "Theme", self.theme, "System follows the Windows app setting (light or dark); a new choice "
                                       "applies the next time GroundTruth opens")

        # every label of a column as wide as its widest: the controls of all its groups start on one line
        for texts in labels.values():
            w = max((t.GetBestSize().width for t in texts), default=0)
            for t in texts:
                t.SetMinSize((w, -1))
        pane.SetSizer(columns)
        root.Add(pane, 1, wx.EXPAND)
        root.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 2 * gap)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        root.Add(buttons, 0, wx.EXPAND | wx.ALL, 2 * gap)
        self.SetSizer(root)
        self._fit_to_screen(pane, columns, intro)
        theme.style_tree(self)

    def _fit_to_screen(self, pane: wx.ScrolledWindow, columns: wx.Sizer, intro: wx.StaticText):
        """Size the dialog to show every group when the screen is tall enough, else as tall as the screen allows
        with the pane scrolling; never wider than the screen, never smaller than MIN_PANE_DIP tall."""
        area = wx.Display(max(wx.Display.GetFromWindow(self.GetParent()) if self.GetParent() else 0, 0)
                          ).GetClientArea()
        content = columns.GetMinSize()
        bar = wx.SystemSettings.GetMetric(wx.SYS_VSCROLL_X)
        width = min(content.width + bar, int(area.width * 0.95))
        intro.Wrap(width - self.FromDIP(4 * GAP))
        pane.SetVirtualSize(content)
        pane.SetMinSize((width, self.FromDIP(MIN_PANE_DIP)))
        self.Layout()
        chrome = self.GetSizer().GetMinSize().height - self.FromDIP(MIN_PANE_DIP)   # intro, line, buttons
        frame = self.GetSize().height - self.GetClientSize().height                 # title bar and border
        height = min(content.height + chrome + frame, int(area.height * 0.92))
        self.SetMinSize((min(width, area.width), self.FromDIP(MIN_PANE_DIP) + chrome + frame))
        self.SetSize((self.GetSizer().GetMinSize().width + self.GetSize().width - self.GetClientSize().width,
                      height))
        self.CentreOnParent()

    def values(self) -> dict:
        """The dialog's choices, as ui.json entries (vias shown "all" is None: no limit)."""
        from ..analysis.simulate import RESOLUTIONS
        v = self.vias_shown.GetStringSelection()
        return {"resolution": RESOLUTIONS[self.resolution.GetSelection()],
                "threads": self.threads.GetStringSelection(),
                "gpu": self.gpu.GetStringSelection(),
                "all_cores": self.all_cores.GetValue(),
                "thermal_quality": self.thermal_quality.GetStringSelection(),
                "author": self.author.GetValue().strip(),
                "company": self.company.GetValue().strip(),
                "history": self.history.GetValue(),
                "history_report": self.history_report.GetValue(),
                **_arrow_range({"arrow_min_mm": self.arrow_min.GetValue(),
                                "arrow_max_mm": self.arrow_max.GetValue()}),
                **_contour_widths({"contour_line_mm": self.contour_line.GetValue(),
                                   "contour_index_mm": self.contour_index.GetValue()}),
                "via_markers": self.via_markers.GetValue(),
                "max_vias": int(v) if v.isdigit() else None,
                "outlines": self.outlines.GetValue(),
                "names": self.names.GetValue(),
                "compute": "server" if self.compute.GetSelection() == 1 else "local",
                "server": self.server.GetValue().strip(),
                "server_key": self.server_key.GetValue().strip(),
                "theme": theme.THEMES[self.theme.GetSelection()]}

    def on_test(self, _=None):
        """Test connection: the server's status, asked in a background thread (a dead address takes seconds),
        then shown in a message box."""
        address, key = self.server.GetValue(), self.server_key.GetValue()
        self.test.Disable()
        self.test.SetLabel("Testing…")
        import threading

        def ask():
            """(background) Ask the server; show the answer on the GUI thread."""
            from .. import build_version
            from ..remote.client import Remote
            from ..remote.protocol import code_id
            from .server_info import server_summary
            try:
                summary, text = server_summary(Remote(address, key).status(), code_id(), build_version()), ""
            except Exception as e:              # noqa: BLE001 - every failure is the answer here
                summary, text = None, str(e)
            wx.CallAfter(self._tested, address, summary, text)
        threading.Thread(target=ask, name="gt-test-server", daemon=True).start()

    def _tested(self, address: str, summary: Optional[dict], error: str):
        """(GUI thread) Show the test's answer (the dialog may be closed by now): the server laid out
        (ui/server_info.py), or why it could not be asked."""
        if self:
            self.test.Enable()
            self.test.SetLabel("Test connection")
        if summary is None:
            wx.MessageBox(error, "Compute server: not reachable", wx.ICON_WARNING)
            return
        from .server_info import ServerInfoDialog
        dlg = ServerInfoDialog(self if self else None, address, summary)
        dlg.ShowModal()
        dlg.Destroy()

    def on_find(self, _=None):
        """Find servers: a query signed with the access key, broadcast on the local network (remote/cluster.py);
        only servers with this key answer. Asked in a background thread (it listens for two seconds)."""
        key = self.server_key.GetValue().strip()
        if not key:
            wx.MessageBox("Enter the access key first: only the servers that use it answer (the key shows in the "
                          "server's window, or with install_server.cmd key on the server).", "Find servers",
                          wx.ICON_INFORMATION)
            return
        self.find.Disable()
        self.find.SetLabel("Searching…")
        import threading

        def ask():
            """(background) Ask the network; show the answers on the GUI thread."""
            import socket
            from ..remote.cluster import find_servers
            try:
                found, error = find_servers(key), ""
            except OSError as e:
                found, error = [], str(e)
            from ..remote.cluster import address_candidates, own_addresses, reachable
            try:
                own = own_addresses()
            except Exception:                           # noqa: BLE001 - only the order of the tries depends on it
                own = []
            for m in found:
                # its address: the one of those it announced that this computer reaches (the datagram's source may be
                # a virtual adapter's, the user saw build_pc at 172.20.0.1); then the host name when it leads there
                # (it survives a new DHCP address), else that address
                tries = address_candidates(m["address"], (m.get("info") or {}).get("addresses"), own)
                m["address"] = reachable(tries, m.get("port") or 8765) or tries[0]
                try:
                    m["name"] = m["host"] if socket.gethostbyname(str(m["host"])) == m["address"] else m["address"]
                except (OSError, UnicodeError):
                    m["name"] = m["address"]
            wx.CallAfter(self._found, found, error)
        threading.Thread(target=ask, name="gt-find-servers", daemon=True).start()

    def _found(self, found: list, error: str):
        """(GUI thread) List the servers found and put the one picked into the address field (the dialog may be
        closed by now)."""
        if not self:
            return
        self.find.Enable()
        self.find.SetLabel("Find servers")
        if not found:
            wx.MessageBox((f"The search failed: {error}" if error else
                           "No compute server with this access key answered on this network.") +
                          "\n\nCheck the key, that the server runs (install_server.cmd status on it) and that this "
                          "computer is on the same subnet; a server elsewhere can still be used by its address.",
                          "Find servers", wx.ICON_WARNING)
            return
        from ..remote.protocol import DEFAULT_PORT

        def text(m: dict) -> str:
            """One server, for the list."""
            info = m.get("info") or {}
            gpus = ", ".join(info.get("gpus") or []) or "no GPU"
            role = "lead" if m.get("lead") == m.get("id") else ("member" if m.get("lead") else "standalone")
            return (f"{m.get('host')} ({m.get('address')}) — {role}, {info.get('cores', '?')} cores, "
                    f"{info.get('memory_gb', '?')} GB, {gpus}")
        dlg = wx.SingleChoiceDialog(self, "Compute servers with this access key (a cluster's jobs go to its lead):",
                                    "Find servers", [text(m) for m in found])
        try:
            if dlg.ShowModal() == wx.ID_OK:
                m = found[dlg.GetSelection()]
                port = int(m.get("port") or DEFAULT_PORT)
                self.server.SetValue(str(m.get("name") or m.get("address")) +
                                     ("" if port == DEFAULT_PORT else f":{port}"))
        finally:
            dlg.Destroy()
