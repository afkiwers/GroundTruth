"""Signal paths tab (the settings' `nets:` list): each row is a signal path from its driver pin to its receiver
pins, across nets through series parts, Pass-through parts and op-amp stages, with its timing and source / load.
The Chain column shows the path as the route finder follows it (worked out in a background thread).

Paths can be typed in, or suggested by "Detect…". Suggestions are added as unreviewed and stay
marked until the user edits them or presses "Mark reviewed".
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

import wx
from .messages import box as message_box   # laid-out message boxes (ui/messages.py)

from ..components.values import format_si, parse_value
from ..config.settings import ConfigError, NetSpec
from . import theme
from .background import LatestOnly
from .listsel import SelectableRows
from .widgets import InfoBox, PickDialog, button_row, set_lazy_items, source_status

# hover explanations of the form's fields (on the label and the field)
NET_TIPS = {
    "Start net": "The net the path is named after and starts its search from, e.g. /CPU/SPI_SCK: the path runs "
                 "from the driver pin through series parts, Pass-through parts and op-amps to the receiver pins, "
                 "and its return current is solved in the planes under all of it.",
    "Driver pin": "The pin that drives the net (the source of the signal's current), e.g. U1.12.",
    "Receiver pins": "The pins it drives. The return current flows from each receiver's ground back to the driver's.",
    "Frequency": "The signal's clock or bit rate, e.g. 20MHz: the harmonics of the radiated estimate sit at "
                 "multiples of it.",
    "Rise time": "The signal's edge time, e.g. 2ns: above 1/(π·t) its harmonics fall off faster, so faster edges "
                 "radiate more at high frequencies.",
    "Amplitude (V)": "The signal swing, e.g. 3.3: the emission scales with it.",
    "Duty cycle": "The fraction of the period the signal is high, e.g. 0.5 for a clock: it sets which harmonics "
                  "are strong.",
    "Fall time": "The falling edge's time when it differs from the rising one, e.g. 3ns (empty: the rise time).",
    "Source R (Ω)": "The driver's output resistance, e.g. 25 (empty: analysis.driver_ohms, 25 Ω). With the line "
                    "and the series parts it sets the current.",
    "Source L (nH)": "The driver's series inductance (package and bond wire), e.g. 1.5: it slows the current at high "
                     "frequencies (empty: none).",
    "Source C (pF)": "The driver's output capacitance to its return, e.g. 3 (empty: none).",
    "Load R (Ω)": "A resistance at the receivers, e.g. 50 for a parallel termination (empty: none, the receivers' "
                  "inputs only).",
    "Load C (pF)": "The input capacitance per receiver, e.g. 5 (empty: analysis.load_pf, 5 pF).",
    "Pass through": "Parts the signal path may run through although they don't pass a signal by default, e.g. C12 "
                    "for an AC-coupling capacitor or R7 for a resistor above 1 kΩ (comma-separated). Series "
                    "resistors up to 1 kΩ, ferrites and inductors are passed anyway; every part's impedance counts.",
    "Sweep from": "Where the path's Bode sweep starts, e.g. 1Hz for a sensor or ADC input (down to 10mHz; empty: a "
                  "hundredth of the frequency). The gain at the sweep's start is shown as the path's DC gain.",
    "Sweep to": "Where the Bode sweep ends, e.g. 10MHz (empty: the top of the signal's harmonics' band).",
    "Sweep points": "How many log-spaced frequencies the Bode sweep has, 20 to 5000 (empty: 300).",
    "Max bandwidth": "The path's bandwidth limit, e.g. 1kHz for an ADC input's anti-alias filter: a bandwidth at the "
                     "receiver above it (3 dB below the passband peak) is a bandwidth_limit issue (empty: no limit).",
}
NET_BUTTON_TIPS = {
    "Add": "Add a new net from the form.",
    "Update selected": "Replace the selected net with the form's values.",
    "Remove selected": "Remove the selected nets (also the Delete key).",
    "Mark reviewed": "Mark the selected auto-detected nets as checked by you (they stop showing in orange).",
    "Detect…": "Find fast clock and data nets on the board (SPI, I²C, USB, Ethernet …) and choose which to add.",
    "Bode": "Sweep the selected paths (all ticked ones when none is selected) with a sine from Sweep from to Sweep "
            "to: the receiver's voltage per volt of the driver through the path's lines, parts, filters and op-amp "
            "stages, as magnitude and phase, its poles and zeros, the gain and the −3 dB frequency (Results tab). "
            "Seconds: no field solve (Simulate does the same sweep with its results).",
}


class DetectDialog(PickDialog):
    """Pick which suggested nets to add. High/medium-confidence fast nets are pre-ticked. A suggestion for a net
    listed as detection's own, unreviewed, with a different path (analysis.detect.refreshes) is offered too, ticked:
    it replaces that entry, and the unreviewed pieces of its path go."""

    def __init__(self, parent, found, nets: list):
        """found: detect_nets' candidates; nets: the listed nets (those not offered again, unless replaced)."""
        from ..analysis.detect import refreshes
        listed = {n.name for n in nets}
        updates, fragments = refreshes(found, nets)
        replaces = {d.spec.name for d, _ in updates}
        old = {d.spec.name: o for d, o in updates}

        def row(d):
            """A candidate's cells: its spec, class, confidence and the reasons it was found (a replacement says
            what it replaces)."""
            s = d.spec
            why = "; ".join(d.reasons[1:])
            if s.name in old:
                why = f"REPLACES the listed entry (driver {old[s.name].driver}): " + why
            return [s.name, d.signal_class.name, s.driver, ", ".join(s.receivers), format_si(s.freq, "Hz"),
                    format_si(s.rise_time, "s"), d.confidence, why]
        note = "Ticked nets are added as unreviewed; check driver, receivers and timing before relying on results."
        if fragments:
            note += (" A replacement also removes the unreviewed entries for pieces of its path: "
                     + ", ".join(fragments) + ".")
        super().__init__(parent, "Suggested nets",
                         [("Net", 200), ("Class", 130), ("Driver", 80), ("Receivers", 170), ("Freq", 70),
                          ("Rise", 60), ("Confidence", 80), ("Why", 600)],
                         found, [d for d in found if d.spec.name not in listed or d.spec.name in replaces],
                         row, lambda d: d.spec.name in replaces or (d.score >= 2 and d.confidence != "low"),
                         note, size=(1000, 480))
        self.fragments = fragments          # the pieces a chosen replacement's path removes (_merged)


def _merged(nets: list, chosen: list, found: list, fragments: list[str]) -> list:
    """The net list with the chosen suggestions in: a suggestion for a listed name replaces that entry in its place
    (keeping its on/off tick), a new one is appended, and an unreviewed fragment inside a chosen suggestion's path
    (DetectDialog) is removed."""
    by_name = {s.name: s for s in chosen}
    covers = set().union(*(d.covers for d in found if d.spec.name in by_name)) if by_name else set()
    drop = {f for f in fragments if f in covers}
    out = [replace(by_name[n.name], enabled=n.enabled) if n.name in by_name else n
           for n in nets if n.name not in drop]
    have = {n.name for n in out}
    return out + [s for s in chosen if s.name not in have]


class NetsPanel(SelectableRows, wx.Panel):
    """The Signal paths tab: the paths to analyse (a list on the left, one row each, with its chain of parts) and a
    form on the right that fills from the selected row; Add, Update, Remove, Mark reviewed and Detect act on the
    list, and every change is written to the settings at once. Under the list, the selected path in full: its chain
    and the parts across it to ground (loads, filter capacitors)."""

    def __init__(self, parent, on_change, run_job=None, on_add_selection=None, on_show=None,
                 in_steps: bool = False, on_bode=None):
        """run_job(status, work, done, what): runs work(progress) off the GUI thread, then done(result).
        on_bode(names): sweep these paths (the window runs analysis.simulate.bode_paths; no button without it).
        on_add_selection(): adds the nets selected in KiCad (live mode only; no button without it).
        on_show(names, zoom): the selected rows changed: show those nets in KiCad (live mode only).
        in_steps: the form's rows and the layout are built later, in two `build_steps` (MainFrame._build_pages runs
        them one per event-loop step: in one, the tab blocked the window 130-170 ms); otherwise here."""
        super().__init__(parent)
        self.on_change = on_change
        self.run_job = run_job or (lambda status, work, done, what: done(work(lambda *a: None)))
        self.board = None
        self.settings = None
        self.nets: list[NetSpec] = []

        self.hint = wx.StaticText(self, label="No signal paths yet: press Detect… to find clock and data paths, "
                                              "or pick a start net on the right and press Add.")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)       # several rows: Remove / Mark reviewed act on all
        for i, (c, w) in enumerate([("Path", 180), ("Status", 95), ("Chain", 330), ("Freq", 75), ("Rise", 60),
                                    ("Ampl.", 50), ("Class", 110)]):
            self.list.InsertColumn(i, c, width=w)
        # each row's chain (analysis.route.path_chain: a route search per path, ~5-60 ms on refboard) is
        # worked out in this thread, the newest request only, and shown when it comes back (_chains_done)
        self._chain_worker = LatestOnly("groundtruth-paths")
        self._chain_gen = 0
        self.chains: dict[str, object] = {}                     # path name -> PathChain of the last computation
        self.path_text = None                                   # the preview under the list (_form_layout)
        self._init_rows(on_show, "nets")
        self._init_enable("nets")
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        # several rows selected: the shared fields as shown then ({key: text}, * where they differ), else None
        self._multi: Optional[dict] = None
        self.list.Bind(wx.EVT_LIST_ITEM_DESELECTED,
                       lambda e: (e.Skip(), wx.CallAfter(lambda: self and self._form_for_selection())))

        form = wx.FlexGridSizer(cols=2, vgap=6, hgap=8)
        form.AddGrowableCol(1)
        self.net = wx.ComboBox(self, style=wx.CB_DROPDOWN)
        self.net.Bind(wx.EVT_COMBOBOX, self.on_net)
        self.net.Bind(wx.EVT_TEXT_ENTER, self.on_net)
        self.driver = wx.Choice(self)
        self.receivers = wx.CheckListBox(self, size=(-1, 110))
        self.freq = wx.TextCtrl(self, value="20MHz")
        self.rise = wx.TextCtrl(self, value="2ns")
        self.ampl = wx.TextCtrl(self, value="3.3")
        self.duty = wx.TextCtrl(self, value="0.5")
        self.fall, self.src_r, self.src_l, self.src_c, self.load_r, self.load_c, self.through = (
            wx.TextCtrl(self) for _ in range(7))
        self.sweep_from, self.sweep_to, self.sweep_pts, self.max_bw = (wx.TextCtrl(self) for _ in range(4))
        for ctrl, hint in ((self.fall, "= rise time"), (self.src_r, "25 (default)"), (self.src_l, "none"),
                           (self.src_c, "none"), (self.load_r, "none"), (self.load_c, "5 (default)"),
                           (self.through, "e.g. C12, R7"), (self.sweep_from, "auto: frequency / 100"),
                           (self.sweep_to, "auto: the band's top"), (self.sweep_pts, "300"),
                           (self.max_bw, "none")):
            ctrl.SetHint(hint)
        self._on_bode = on_bode
        self.through.Bind(wx.EVT_KILL_FOCUS, lambda e: (e.Skip(), self.on_net(keep=True)))
        self._form_sizer, self._on_add_selection = form, on_add_selection
        self.build_steps = [self._form_rows, self._form_layout]
        while self.build_steps and not in_steps:
            self.build_steps.pop(0)()

    def _form_rows(self):
        """Each field's label and tooltip, in the form's grid (the first build step)."""
        form = self._form_sizer
        for label, ctrl in (("Start net", self.net), ("Driver pin", self.driver), ("Receiver pins", self.receivers),
                            ("Frequency", self.freq), ("Rise time", self.rise), ("Amplitude (V)", self.ampl),
                            ("Duty cycle", self.duty), ("Fall time", self.fall), ("Source R (Ω)", self.src_r),
                            ("Source L (nH)", self.src_l), ("Source C (pF)", self.src_c), ("Load R (Ω)", self.load_r),
                            ("Load C (pF)", self.load_c), ("Pass through", self.through),
                            ("Sweep from", self.sweep_from), ("Sweep to", self.sweep_to),
                            ("Sweep points", self.sweep_pts), ("Max bandwidth", self.max_bw)):
            text = wx.StaticText(self, label=label)
            text.SetToolTip(NET_TIPS[label])         # on the label and the field: hovering either explains it
            ctrl.SetToolTip(NET_TIPS[label])
            form.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(ctrl, 1, wx.EXPAND)

    def _path_grip(self):
        """A thin strip under the path box (the user: "should be able to change the height of this window to view
        more complex structures"): dragging it sets the box's height, between three lines and 70 % of the tab; the
        height is kept in ui.json ("path_height") when the drag ends."""
        grip = wx.Window(self, size=wx.Size(-1, 7))
        grip.SetCursor(wx.Cursor(wx.CURSOR_SIZENS))
        grip.SetToolTip("Drag to make the path box taller or shorter")
        drag = {}

        def paint(evt):
            """Three dots in the middle: the usual sign of a grip."""
            dc = wx.PaintDC(grip)
            dc.SetBrush(wx.Brush(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)))
            dc.SetPen(wx.TRANSPARENT_PEN)
            w, h = grip.GetClientSize()
            for dx in (-6, 0, 6):
                dc.DrawCircle(w // 2 + dx, h // 2, 1)

        def down(evt):
            """Start of a drag: the mouse is captured, the start height and position noted."""
            drag["y"] = grip.ClientToScreen(evt.GetPosition()).y
            drag["h"] = self.path_text.GetSize().height
            grip.CaptureMouse()

        def move(evt):
            """While dragging: the box's new height, then the tab laid out again."""
            if "y" not in drag or not evt.Dragging():
                return
            h = drag["h"] + grip.ClientToScreen(evt.GetPosition()).y - drag["y"]
            h = max(self._path_min_h, min(h, int(self.GetClientSize().height * 0.7)))
            self.path_text.SetMinSize(wx.Size(-1, h))
            self.Layout()

        def up(evt=None):
            """End of a drag: release the mouse and remember the height."""
            if "y" not in drag:
                return
            drag.clear()
            if grip.HasCapture():
                grip.ReleaseMouse()
            theme.save_prefs(path_height=int(self.path_text.GetMinSize().height))

        grip.Bind(wx.EVT_PAINT, paint)
        grip.Bind(wx.EVT_LEFT_DOWN, down)
        grip.Bind(wx.EVT_MOTION, move)
        grip.Bind(wx.EVT_LEFT_UP, up)
        grip.Bind(wx.EVT_MOUSE_CAPTURE_LOST, lambda e: drag.clear())
        return grip

    def _form_layout(self):
        """The buttons, the help box and the tab's layout (the last build step)."""
        form, on_add_selection = self._form_sizer, self._on_add_selection
        del self._form_sizer, self._on_add_selection
        btns = button_row(self, [("Add", self.on_add), ("Update selected", self.on_update),
                                 ("Remove selected", self.on_remove), ("Mark reviewed", self.on_reviewed),
                                 ("Detect…", self.on_detect)]
                          + ([("Bode", self.on_bode)] if self._on_bode is not None else []), NET_BUTTON_TIPS)
        if on_add_selection is not None:
            b = theme.Button(self, "Add KiCad selection")
            b.SetToolTip("Adds the net of the track, via or pad selected in KiCad's PCB editor (click or right-click "
                         "it there first). KiCad's toolbar button 'Add selected net to GroundTruth' does the same.")
            b.Bind(wx.EVT_BUTTON, lambda e: on_add_selection())
            btns.Add(b, 0, wx.RIGHT | wx.BOTTOM, 6)

        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(form, 0, wx.EXPAND)
        right.Add(btns, 0, wx.TOP, 8)
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(self.list_head(self.hint), 0, wx.EXPAND | wx.BOTTOM, 6)
        left.Add(self.list, 1, wx.EXPAND)
        # the selected path in full (selectable text: a part reference can be copied from it)
        self.path_text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self._path_min_h = 3 * self.GetCharHeight() + 8
        self.path_text.SetMinSize(wx.Size(-1, max(self._path_min_h, int(theme.prefs().get("path_height") or 0))))
        self.path_text.SetToolTip("The selected path as the route follows it: driver → series parts and op-amps "
                                  "(×gain) → receivers, and its branches (✗: DNP). Drag the grip below to make it "
                                  "taller or shorter")
        left.Add(self.path_text, 0, wx.EXPAND | wx.TOP, 6)
        self.path_grip = self._path_grip()
        left.Add(self.path_grip, 0, wx.EXPAND)
        left.Add(self.zoom, 0, wx.TOP, 6)
        from ..analysis.tags import HELP
        self.info = InfoBox(self, "How paths are found (net and pin names, GT labels)", HELP["nets"])
        left.Add(self.info, 0, wx.EXPAND | wx.TOP, 6)
        s = wx.BoxSizer(wx.HORIZONTAL)
        s.Add(left, 3, wx.EXPAND | wx.ALL, 6)
        s.Add(right, 2, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(s)
        self.hint.Hide()
        # a resize re-wraps the buttons: repaint everything, or a moved drawn button leaves its old picture behind
        self.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), self.Refresh()))

    def load(self, board, settings):
        """Show this board's nets from its settings (the net combo is filled only when opened: set_lazy_items)."""
        self.board, self.settings = board, settings
        set_lazy_items(self.net, board.nets)
        self.nets = list(settings.config.nets)
        self.refresh()

    def refresh(self):
        """Rebuild the list from self.nets: unreviewed detected paths in the warning colour, disabled ones greyed.
        The Chain column shows driver → … → receivers until the background thread has the real chains."""
        self.hint.Show(self.board is not None and not self.nets)
        self.Layout()
        self.list.DeleteAllItems()
        for n in self.nets:
            i = self.list.InsertItem(self.list.GetItemCount(), n.name)
            for col, v in enumerate([source_status(n.source, n.reviewed),
                                     f"{n.driver} → … → {', '.join(n.receivers)}",
                                     format_si(n.freq, "Hz"), format_si(n.rise_time, "s"), f"{n.amplitude_v:g}",
                                     n.signal_class], 1):
                self.list.SetItem(i, col, v)
            if n.source == "auto" and not n.reviewed:
                self.list.SetItemTextColour(i, theme.colour(theme.palette().warn))   # unreviewed
            self._show_enabled(i, n)
        self.refresh_chains()

    def refresh_chains(self):
        """Work out every row's chain again in the background (the list or a part model changed); a request still
        waiting is dropped for this one, and an answer to an older one is ignored (_chain_gen)."""
        if self.board is None or self.settings is None:
            return
        from ..analysis.route import PathChain, path_chain
        self._chain_gen += 1
        gen, board, config, specs = self._chain_gen, self.board, self.settings.config, list(self.nets)

        def work():
            """(background) The chains, one per path; a failure becomes that row's error, never the thread's."""
            out = []
            for s in specs:
                try:
                    out.append(path_chain(board, config, s))
                except Exception as e:                  # noqa: BLE001 - shown in the row
                    out.append(PathChain(s.driver, [], list(s.receivers), error=f"{type(e).__name__}: {e}"))
            wx.CallAfter(self._chains_done, gen, [s.name for s in specs], out)
        self._chain_worker.submit(work)

    def _chains_done(self, gen: int, names: list[str], chains: list):
        """(GUI thread) The chains arrived: fill the Chain column (rows still in the same order) and the preview."""
        if not self or gen != self._chain_gen:
            return
        self.chains = dict(zip(names, chains))
        self.list.Freeze()
        try:
            for i, n in enumerate(self.nets):
                c = self.chains.get(n.name)
                if c is not None and i < self.list.GetItemCount():
                    self.list.SetItem(i, 2, f"no route: {c.error}" if c.error else c.text())
        finally:
            self.list.Thaw()
        self._show_path()

    def _show_path(self):
        """The preview under the list: the selected path's chain and the parts across it, or how to get one."""
        if self.path_text is None:
            return
        rows = self.selected_rows()
        if len(rows) != 1 or rows[0] >= len(self.nets):
            self.path_text.ChangeValue("Select a path to see it in full." if self.nets else "")
            return
        n = self.nets[rows[0]]
        c = self.chains.get(n.name)
        if c is None:
            text = f"{n.name}: working out its path…"
        elif c.error:
            text = (f"{n.name}: no route ({c.error}). A part in the way that doesn't pass a signal (a capacitor, "
                    f"a resistor over 1 kΩ, an op-amp without its model) can be named in Pass through.")
        else:
            text = f"Path: {c.text()}"
            if c.branches:                  # what hangs off each net of the path (✗: not placed, DNP)
                text += "\nBranches:" + "".join(f"\n  at {net}: " + ", ".join(parts) for net, parts in c.branches)
            for note in c.notes:                # what the path's circuit leaves out
                text += f"\n⚠ {note}"
        self.path_text.ChangeValue(text)

    def select(self, name: str):
        """Select the net's row alone (which fills the form) and scroll to it."""
        for i, n in enumerate(self.nets):
            if n.name == name:
                self.select_only(i)
                return

    def _row_names(self, rows: list[int]) -> list[str]:
        """The nets of these rows (what is shown in KiCad for a selection)."""
        return [self.nets[i].name for i in rows if i < len(self.nets)]

    def _through(self) -> list[str]:
        """The Pass through field's references (commas, semicolons or spaces between them)."""
        return [t for t in self.through.GetValue().replace(";", ",").replace(" ", ",").split(",") if t]

    def _pins(self, net: str) -> list[str]:
        """Every pin the signal path can start or end on: the pads of `net` and of the nets its series parts (and
        the Pass through parts) connect it to."""
        from ..analysis.route import signal_nets
        group = (signal_nets(self.board, self.settings.config, net, self._through()) if net in self.board.nets
                 else {net})
        return sorted(p.pin for p in self.board.pads() if p.net in group)

    def on_net(self, _=None, keep: bool = False):
        """The pins of the net and of the nets its series parts (and Pass through) lead to. keep: the driver and
        receivers chosen stay chosen where they still exist (Pass through edited)."""
        if self.board is None:
            return
        driver, receivers = self.driver.GetStringSelection(), list(self.receivers.GetCheckedStrings())
        pins = self._pins(self.net.GetValue())
        self.driver.Set(pins)
        self.receivers.Set(pins)
        if keep and driver in pins:
            self.driver.SetStringSelection(driver)
            self.receivers.SetCheckedStrings([r for r in receivers if r in pins])
        elif pins:
            self.driver.SetSelection(0)

    def on_select(self, evt):
        """A row was selected: fill the form with its path (the pins first, so the driver and receivers can be
        chosen among them) and show it in full under the list."""
        self._selection_changed()
        wx.CallAfter(lambda: self and self._show_path())         # after the list has its new selection
        wx.CallAfter(lambda: self and self._form_for_selection())
        self._fill_one(self.nets[evt.GetIndex()])

    # the form's signal fields, shared by several paths when several are selected (the user: "more than one could be
    # selected and any edited at once. Any paras that are not the same are presented as * and ignored on update
    # unless the user changed it"): (NetSpec field, control, how it is shown and read)
    MIXED = "*"

    def _shared_fields(self) -> list:
        """(key, control, unit kind) of every field several paths can share; the kinds: hz, s, v, num, ohm, nh, pf,
        int (see _show / _read)."""
        return [("freq", self.freq, "hz"), ("rise_time", self.rise, "s"), ("amplitude_v", self.ampl, "v"),
                ("duty", self.duty, "num"), ("fall_time", self.fall, "s?"), ("driver_ohms", self.src_r, "ohm?"),
                ("driver_nh", self.src_l, "nh?"), ("driver_pf", self.src_c, "pf?"), ("load_ohms", self.load_r, "ohm?"),
                ("load_pf", self.load_c, "pf?"), ("sweep_from", self.sweep_from, "hz?"),
                ("sweep_to", self.sweep_to, "hz?"), ("max_bandwidth", self.max_bw, "hz?"),
                ("sweep_points", self.sweep_pts, "int?")]

    @staticmethod
    def _show(value, kind: str) -> str:
        """A field's value as the form shows it ("" for none)."""
        if value is None:
            return ""
        base = kind.rstrip("?")
        if base == "hz":
            return format_si(value, "Hz")
        if base == "s":
            return format_si(value, "s")
        if base == "int":
            return str(value)
        return f"{value:g}"

    def _read(self, ctrl, kind: str):
        """(the field's value, problem or ""): parsed by kind; empty is None where the field is optional (kind ends
        in ?), a problem where it is required."""
        text = ctrl.GetValue().strip()
        base, optional = kind.rstrip("?"), kind.endswith("?")
        strip = {"hz": "Hz", "s": "s", "v": "V", "nh": "nH", "pf": "pF", "ohm": "Ω"}.get(base, "")
        if strip and text.lower().endswith(strip.lower()):
            text = text[: -len(strip)].strip()
        if base == "ohm" and text.lower().endswith("ohm"):
            text = text[:-3].strip()
        if not text:
            return None, "" if optional else "must be a number"
        v = parse_value(text)
        if v is None or (base == "int" and v != int(v)):
            return None, "must be a whole number" if base == "int" else "must be a number"
        return (int(v) if base == "int" else v), ""

    def _fill_one(self, n: NetSpec):
        """The form shows one path (its own fields enabled again)."""
        self._multi = None
        for ctrl in (self.net, self.driver, self.receivers, self.through):
            ctrl.Enable(True)
        self.net.SetValue(n.name)
        self.through.SetValue(", ".join(n.through))
        self.on_net()
        if n.driver in self.driver.GetItems():
            self.driver.SetStringSelection(n.driver)
        self.receivers.SetCheckedStrings([r for r in n.receivers if r in self.receivers.GetItems()])
        self.freq.SetValue(format_si(n.freq, "Hz"))
        self.rise.SetValue(format_si(n.rise_time, "s"))
        self.ampl.SetValue(f"{n.amplitude_v:g}")
        self.duty.SetValue(f"{n.duty:g}")
        self.fall.SetValue(format_si(n.fall_time, "s") if n.fall_time is not None else "")
        for ctrl, v in ((self.src_r, n.driver_ohms), (self.src_l, n.driver_nh), (self.src_c, n.driver_pf),
                        (self.load_r, n.load_ohms), (self.load_c, n.load_pf)):
            ctrl.SetValue("" if v is None else f"{v:g}")
        for ctrl, v in ((self.sweep_from, n.sweep_from), (self.sweep_to, n.sweep_to),
                        (self.max_bw, n.max_bandwidth)):
            ctrl.SetValue("" if v is None else format_si(v, "Hz"))
        self.sweep_pts.SetValue("" if n.sweep_points is None else str(n.sweep_points))

    def _form_for_selection(self):
        """After the selection settled: several rows show their shared fields (a value they differ in as *), and
        their own fields (net, driver, receivers, pass-through) are greyed; one row shows itself."""
        rows = self.selected_rows()
        if len(rows) == 1 and self._multi is not None:
            self._fill_one(self.nets[rows[0]])
        if len(rows) < 2:
            return
        specs = [self.nets[i] for i in rows]
        shown = {}
        for key, ctrl, kind in self._shared_fields():
            texts = {self._show(getattr(s, key), kind) for s in specs}
            shown[key] = texts.pop() if len(texts) == 1 else self.MIXED
            ctrl.SetValue(shown[key])
        for ctrl in (self.net, self.driver, self.receivers, self.through):
            ctrl.Enable(False)
        self._multi = shown

    def _update_many(self, rows: list):
        """Update selected with several rows: every shared field the user changed (not * and not what was shown)
        goes into each selected path; the rest of each path stays its own."""
        changes, problems = {}, []
        for key, ctrl, kind in self._shared_fields():
            text = ctrl.GetValue().strip()
            if text == self.MIXED or text == self._multi.get(key):
                continue
            value, problem = self._read(ctrl, kind)
            if problem:
                problems.append(f"{key.replace('_', ' ')}: {problem} (or * to leave each path's own)")
            else:
                changes[key] = value
        if problems:
            message_box("\n".join(problems), "GroundTruth", wx.ICON_WARNING)
            return
        if not changes:
            self.parent_status(f"nothing changed in the form for the {len(rows)} selected paths")
            return
        for i in rows:
            self.nets[i] = replace(self.nets[i], reviewed=True, **changes)
        if self._commit():
            self.reselect(rows)          # the list was rebuilt: the paths stay selected for the next edit
            self._form_for_selection()

    def parent_status(self, text: str):
        """A line in the window's status bar, when there is one."""
        top = self.GetTopLevelParent()
        if hasattr(top, "SetStatusText"):
            top.SetStatusText(text)

    def _optional(self, ctrl, strip: str = "") -> tuple[Optional[float], bool]:
        """(the field's number or None when empty, whether it was readable)."""
        text = ctrl.GetValue().strip()
        for unit in (strip, "Ω", "ohm"):
            if unit and text.lower().endswith(unit.lower()):
                text = text[: -len(unit)].strip()
        if not text:
            return None, True
        v = parse_value(text)
        return v, v is not None

    def _form(self):
        """The form as a NetSpec, or None (after telling the user what is wrong): a net of the board, a driver and
        at least one other receiver pin, numbers for frequency, rise time and amplitude; the optional source and
        load fields empty or numbers."""
        name = self.net.GetValue()
        driver = self.driver.GetStringSelection()
        receivers = [r for r in self.receivers.GetCheckedStrings() if r != driver]
        f = parse_value(self.freq.GetValue().replace("Hz", "").replace("hz", ""))
        tr = parse_value(self.rise.GetValue().rstrip("s"))
        a = parse_value(self.ampl.GetValue().rstrip("V"))
        d = parse_value(self.duty.GetValue())
        problems = []
        if name not in self.board.nets:
            problems.append("pick a net from the board")
        if not driver or not receivers:
            problems.append("choose a driver pin and at least one receiver pin")
        if not f or not tr or not a:
            problems.append("frequency, rise time and amplitude must be numbers (e.g. 20MHz, 2ns, 3.3)")
        extra = {}
        for key, ctrl, strip, label in (("fall_time", self.fall, "s", "fall time"),
                                        ("driver_ohms", self.src_r, "", "source R"),
                                        ("driver_nh", self.src_l, "nH", "source L"),
                                        ("driver_pf", self.src_c, "pF", "source C"),
                                        ("load_ohms", self.load_r, "", "load R"),
                                        ("load_pf", self.load_c, "pF", "load C")):
            v, ok = self._optional(ctrl, strip)
            if not ok:
                problems.append(f"{label} must be a number or empty")
            extra[key] = v
        for key, ctrl, label in (("sweep_from", self.sweep_from, "sweep from"),
                                 ("sweep_to", self.sweep_to, "sweep to"),
                                 ("max_bandwidth", self.max_bw, "max bandwidth")):
            v, ok = self._optional(ctrl, "Hz")
            if not ok:
                problems.append(f"{label} must be a frequency (e.g. 1Hz, 10MHz) or empty")
            extra[key] = v
        pts, ok = self._optional(self.sweep_pts)
        if not ok or (pts is not None and pts != int(pts)):
            problems.append("sweep points must be a whole number or empty")
        extra["sweep_points"] = int(pts) if ok and pts is not None and pts == int(pts) else None
        if problems:
            message_box("\n".join(problems), "groundtruth", wx.ICON_WARNING)
            return None
        return NetSpec(name, driver, receivers, f, tr, a, d if d else 0.5, through=self._through(), **extra)

    def _commit(self) -> bool:
        """Write the list to the settings, show it again and tell the window (it saves in the background). False
        when the settings refuse it: the user is told why and the list goes back to what the settings hold."""
        try:
            self.settings.set_nets(self.nets)
        except ConfigError as e:
            message_box("\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
            self.nets = list(self.settings.config.nets)
            self.refresh()
            return False
        self.refresh()
        self.on_change()
        return True

    def on_add(self, _):
        """Add the form's net."""
        spec = self._form()
        if spec:
            self.nets.append(spec)
            self._commit()

    def on_update(self, _):
        """Replace the selected net with the form's values, keeping what the form does not show (its source,
        class, enabled state, return pins and current override)."""
        rows = self.selected_rows()
        if len(rows) > 1 and self._multi is not None:
            self._update_many(rows)
            return
        i = rows[0] if rows else -1
        spec = self._form() if rows else None
        if i >= 0 and spec:
            old = self.nets[i]
            # editing a suggestion counts as reviewing it
            self.nets[i] = replace(spec, source=old.source, reviewed=True, signal_class=old.signal_class,
                                   enabled=old.enabled,
                                   driver_return=old.driver_return, receiver_return=old.receiver_return,
                                   current_a=old.current_a)
            if self._commit():
                self.select_only(i)          # the list was rebuilt: the path stays selected for the next edit

    def on_bode(self, _):
        """"Bode": sweep the selected paths, or every ticked one when none is selected (the window runs it as a job
        and shows the responses in the Results tab). A form edit counts once it is in the list (Update selected):
        the sweep runs on the settings."""
        names = self._row_names(self.selected_rows()) or [n.name for n in self.nets if n.enabled]
        if not names:
            message_box("Add a signal path first (or tick one).", "GroundTruth", wx.ICON_INFORMATION)
            return
        self._on_bode(list(dict.fromkeys(names)))

    def on_remove(self, _):
        """Remove the selected nets."""
        rows = set(self.selected_rows())
        if rows:
            self.nets = [n for i, n in enumerate(self.nets) if i not in rows]
            self._commit()

    def on_reviewed(self, _):
        """Mark the selected nets as reviewed, keeping them selected."""
        rows = self.selected_rows()
        if rows:
            for i in rows:
                self.nets[i] = replace(self.nets[i], reviewed=True)
            self._commit()
            self.reselect(rows)

    def on_detect(self, _):
        """Find candidate nets as a background job (detection reads the whole board)."""
        from ..analysis.detect import detect_nets
        if self.board is None:
            return
        board, config = self.board, self.settings.config
        self.run_job("detecting nets…", lambda progress: detect_nets(board, config, min_score=1), self._detected,
                     "detect nets")

    def _detected(self, found):
        """Detection finished: let the user pick which of the new candidates to add."""
        if not found:
            message_box("No candidate nets found.", "groundtruth")
            return
        with DetectDialog(self, found, self.nets) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                chosen = dlg.selected()
                if chosen:
                    self.nets = _merged(self.nets, chosen, found, dlg.fragments)
                    self._commit()
