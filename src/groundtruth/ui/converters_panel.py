"""Converters tab: switching power stages to analyse (hot loop, input capacitors, conducted emission).

Stages can be entered by hand or suggested by "Detect…" (from the inductors and the switches on their switch
nodes). Suggestions are added as unreviewed: the switching frequency, load current and voltages cannot be read from
the board and start as defaults.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

import wx

from ..components.values import format_si
from ..config.settings import CONDUCTED_STANDARDS, TOPOLOGIES, ConfigError, ConverterSpec
from . import theme
from .listsel import SelectableRows
from .widgets import InfoBox, PickDialog, button_row, parse_field, set_lazy_items, source_status, with_mark

STANDARD_CHOICES = ["(analysis default)"] + list(CONDUCTED_STANDARDS)
SPREAD_MODES = ("random", "triangle")      # ConverterSpec.spread_mode

# hover explanations of the form's fields (on the label and the field): what it is, where to find it, what it changes
FIELD_TIPS = {
    "Name": "A name for this power stage, used in the lists, the Heatmap tab and the reports.",
    "Topology": "buck (steps down), boost (steps up) or custom. Sets the duty cycle (buck V out / V in) and which "
                "switch is hard-switched (a buck's high side, a boost's low side).",
    "Rail net": "The net of the hot loop's capacitors: V in of a buck, V out of a boost.",
    "Switch node": "The net between the switches and the inductor, e.g. SW1. Empty for an integrated stage (found "
                   "from the IC's SW pin).",
    "Ground net": "The power ground the hot loop returns on, e.g. GND.",
    "High side": "The switch(es) between the rail and the switch node, e.g. Q1, or the IC with the high-side "
                 "switch inside.",
    "Low side": "The switch(es) or catch diode between the switch node and ground, e.g. Q2 or D1. A FET here makes "
                "the stage synchronous: only the turn-on edge costs overlap loss. A diode: both edges do.",
    "Integrated stage": "An IC with both switches inside. Leave high side, low side and switch node empty then.",
    "Switching frequency": "f_sw, e.g. 750kHz (from the controller's R_T / R_FREQ formula). Sets where the "
                           "harmonics of the conducted and radiated estimates fall, the capacitor sharing at f_sw, "
                           "and the switching losses of the thermal solve (both grow with f_sw). It does not change "
                           "the DC currents or the hot loop's inductance.",
    "Load current (A)": "The stage's output current: the pulse the switches commutate, the DC paths' currents and "
                        "the conduction losses. The input current is worked out from it (output power plus losses "
                        "over the input voltage). Current capacity scales it.",
    "V in (V)": "Input voltage: the duty cycle and the voltage the switches swing (overlap and output-charge "
                "losses, common mode).",
    "V out (V)": "Output voltage: the duty cycle.",
    "Edge time": "How long the switch node takes to swing (the fast edge), e.g. 2ns. Used for the L·di/dt spike "
                 "(L·I/t), the spectrum above 1/(π·t), and the turn-on overlap loss ½·V·I·t·f_sw. For a GaN FET "
                 "about (Q_GD + Q_GS − Q_G(th)) / gate current; a scope shot of the switch node is best.",
    "C_oss": "The switch's output capacitance at the bus voltage (datasheet C_OSS at V_DS ≈ V in), e.g. 482p. "
             "With the hot loop's inductance it sets the ringing frequency and the overshoot. Also the switching "
             "loss C_oss·V²·f_sw when Q_oss is empty.",
    "Q_oss": "The switch's output charge (datasheet Q_OSS at V_DS ≈ V in), e.g. 38n. Each turn-on of the main "
             "switch loses Q_oss·V (synchronous: it dumps its own charge and charges the other FET's; with a "
             "diode ½·Q_oss·V). Better than C_oss for GaN, whose capacitance falls steeply with voltage. Empty: "
             "C_oss·V² is used.",
    "C stray (chassis)": "Switch node to chassis or heatsink capacitance, e.g. 20pF: the common-mode estimate. "
                         "Empty: only the budget (the largest capacitance that still passes) is reported.",
    "Input connector": "The input connector's pins, plus then minus, e.g. J1.1, J1.2: where the LISN goes for the "
                       "conducted estimate, and the start of the DC input path.",
    "Output connector": "The load's pins, plus then minus, e.g. J2.1, J2.2: the DC output path and its ground "
                        "return.",
    "Conducted standard": "The conducted-emission limit for this stage (default: analysis.conducted in the "
                          "settings).",
    "Spread spectrum (%)": "The controller's spread spectrum: f_sw dithered from f_sw up to f_sw x (1 + this), e.g. "
                           "20 (the LTC7890's is +20 %). The receivers then see the harmonics spread over the band "
                           "(conducted and radiated estimates). Empty: off.",
    "Spread mode": "random: a new frequency every cycle (or Spread rate hops per second; most controllers' pseudo-"
                   "random dither); triangle: swept up and down at Spread rate.",
    "Spread rate (Hz)": "triangle: the modulation frequency (e.g. 10kHz); random: hops per second. Empty: random "
                        "hops every switching cycle; a triangle needs one.",
    "R_DS(on)": "The high-side (or integrated) switch's on-resistance, e.g. 3m (datasheet, at the expected "
                "temperature): its conduction loss D·I²·R. Empty: left out of the thermal solve.",
    "R_DS(on) low side": "The low-side FET's on-resistance, e.g. 3m: its conduction loss (1−D)·I²·R. Empty: the "
                         "same as the high side.",
    "Inductor DCR": "The inductor's DC resistance (datasheet DCR), e.g. 6.3m: its winding loss I²·DCR. Empty: "
                    "left out of the thermal solve.",
    "Diode V_f (V)": "A catch or boost diode's forward voltage at the load current, e.g. 0.5: its loss "
                     "(1−D)·I·V_f. Empty: 0.5 V.",
}
BUTTON_TIPS = {
    "Add": "Add a new power stage from the form.",
    "Update selected": "Replace the selected power stage with the form's values (settings the form does not show, "
                       "such as spread spectrum, are kept).",
    "Remove selected": "Remove the selected power stages (also the Delete key).",
    "Mark reviewed": "Mark the selected auto-detected stages as checked by you (they stop showing in orange).",
    "Use schematic values": "Put the selected stages' part parameters (R_DS(on), C_oss, Q_oss, DCR, V_f) back to "
                            "what their parts' schematic fields say, where they have them (Help: Part parameters).",
    "Detect…": "Find switching power stages on the board (an inductor next to switching transistors, a catch "
               "diode or an IC's SW pin) and choose which to add.",
}


def _switches(c: ConverterSpec) -> str:
    """The Switches column: an integrated stage's IC, or high side / low side."""
    if c.switch:
        return f"integrated {', '.join(c.switch)}"
    return f"{', '.join(c.high_side)} / {', '.join(c.low_side)}"


class ConverterDetectDialog(PickDialog):
    """Pick which detected power stages to add. All but the low-confidence ones are pre-ticked."""

    def __init__(self, parent, found, listed: set):
        """found: detect_converters' candidates; listed: the part sets of the stages already in the settings (a
        stage is the same one when it has the same parts, whatever its name)."""
        def row(d):
            """A candidate's cells: its spec, confidence and the reasons it was found."""
            s = d.spec
            return [s.name, s.topology, _switches(s), s.rail, s.ground, " / ".join(s.input_connector),
                    " / ".join(s.output_connector), d.confidence, "; ".join(d.reasons)]
        super().__init__(parent, "Suggested power stages",
                         [("Name", 180), ("Topology", 70), ("Switches", 150), ("Rail", 120), ("Ground", 70),
                          ("Input", 110), ("Output", 110), ("Confidence", 80), ("Why", 600)],
                         found, [d for d in found if frozenset(d.spec.parts) not in listed], row,
                         lambda d: d.confidence != "low",
                         "Ticked stages are added as unreviewed: set the switching frequency, load current, "
                         "voltages and edge time before relying on results.")


class ConvertersPanel(SelectableRows, wx.Panel):
    """The Power stages tab: the switching converters to analyse (a list on the left) and a form on the right with
    their nets, switches, operating point and part parameters (the parameters marked when they come from the
    schematic fields). Every change is written to the settings at once."""

    def __init__(self, parent, on_change, run_job=None, on_show=None, in_steps: bool = False):
        """run_job(status, work, done, what): runs work(progress) off the GUI thread, then done(result).
        on_show(names, zoom): the selected rows changed: show those power stages in KiCad (live mode only).
        in_steps: the form is built later, in four `build_steps` (MainFrame._build_pages runs them one per
        event-loop step: in one, the tab blocked the window 155 ms on refboard's copy, 240 ms on a slow
        moment); otherwise here."""
        super().__init__(parent)
        self.on_change = on_change
        self.run_job = run_job or (lambda status, work, done, what: done(work(lambda *a: None)))
        self.board = None
        self.settings = None
        self.converters: list[ConverterSpec] = []

        self.hint = wx.StaticText(self, label="No power stages yet: press Detect… to find switching converters, "
                                              "or fill in the form on the right and press Add.")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)       # several rows: Remove / Mark reviewed act on all
        for i, (c, w) in enumerate([("Name", 150), ("Status", 95), ("Topology", 65), ("Switches", 130),
                                    ("Rail", 110), ("f_sw", 70), ("I out", 50), ("V in→out", 75), ("Edge", 55),
                                    ("Input", 90), ("Output", 90)]):
            self.list.InsertColumn(i, c, width=w)
        self.list.SetMinSize(wx.Size(320, 120))   # it scrolls sideways; else its ~1000 px of columns squeeze the form
        self._init_rows(on_show, "power stages")
        self._init_enable("converters")
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.build_steps = [self._form_fields, self._form_fields_more, self._form_rows, self._form_layout]
        while self.build_steps and not in_steps:
            self.build_steps.pop(0)()

    def _form_fields(self):
        """The form's fields (the first of the build steps)."""
        # the form scrolls when the window is short; the buttons under it stay in view (before, the last rows of
        # buttons were cut off on a laptop screen)
        self.form_win = fw = wx.ScrolledWindow(self, style=wx.VSCROLL)
        fw.SetScrollRate(0, 12)
        form = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        form.AddGrowableCol(1)
        self.name = wx.TextCtrl(fw)
        self.topology = wx.Choice(fw, choices=list(TOPOLOGIES))
        self.topology.SetSelection(0)
        self.rail = wx.ComboBox(fw, style=wx.CB_DROPDOWN)
        self.sw = wx.ComboBox(fw, style=wx.CB_DROPDOWN)
        self.ground = wx.ComboBox(fw, style=wx.CB_DROPDOWN)
        self.high = wx.TextCtrl(fw)
        self.low = wx.TextCtrl(fw)
        self.integrated = wx.TextCtrl(fw)
        self.f_sw = wx.TextCtrl(fw, value="500kHz")
        self.i_out = wx.TextCtrl(fw, value="1")
        self.v_in = wx.TextCtrl(fw)
        self.v_out = wx.TextCtrl(fw)
        self.rise = wx.TextCtrl(fw, value="10ns")
        self.c_oss = wx.TextCtrl(fw)
        self.q_oss = wx.TextCtrl(fw)
        self.c_stray = wx.TextCtrl(fw)
        self._form = form

    def _form_fields_more(self):
        """The rest of the form's fields and the part parameters' marks (the second build step: all of them in one
        blocked the window 125-146 ms, tools/dev/build_profile.py)."""
        fw = self.form_win
        self.rds_on = wx.TextCtrl(fw)            # thermal solve: the parts' losses
        self.rds_on_low = wx.TextCtrl(fw)
        self.dcr = wx.TextCtrl(fw)
        self.diode_vf = wx.TextCtrl(fw)
        self.inp = wx.TextCtrl(fw)
        self.out = wx.TextCtrl(fw)
        self.standard = wx.Choice(fw, choices=STANDARD_CHOICES)
        self.standard.SetSelection(0)
        self.spread = wx.TextCtrl(fw)
        self.spread.SetHint("off")
        self.spread_mode = wx.Choice(fw, choices=list(SPREAD_MODES))
        self.spread_mode.SetSelection(0)
        self.spread_rate = wx.TextCtrl(fw)
        self.spread_rate.SetHint("every cycle")
        # the part parameters the schematic fields can give: a mark beside each (◆ from the schematic, ! differs)
        self.param_ctrls = {"c_oss": self.c_oss, "q_oss": self.q_oss, "rds_on": self.rds_on,
                            "rds_on_low": self.rds_on_low, "dcr": self.dcr, "diode_vf": self.diode_vf}
        self.marks = {}
        self._rows = {}
        for key, ctrl in self.param_ctrls.items():
            self._rows[ctrl], self.marks[key] = with_mark(fw, ctrl)

    def _form_rows(self):
        """Each field's label and tooltip, in the form's grid (the third build step)."""
        fw, form, rows = self.form_win, self._form, self._rows
        for label, ctrl in (("Name", self.name), ("Topology", self.topology), ("Rail net", self.rail),
                            ("Switch node", self.sw), ("Ground net", self.ground), ("High side", self.high),
                            ("Low side", self.low), ("Integrated stage", self.integrated),
                            ("Switching frequency", self.f_sw), ("Load current (A)", self.i_out),
                            ("V in (V)", self.v_in), ("V out (V)", self.v_out), ("Edge time", self.rise),
                            ("C_oss", self.c_oss), ("Q_oss", self.q_oss), ("C stray (chassis)", self.c_stray),
                            ("Input connector", self.inp), ("Output connector", self.out),
                            ("Conducted standard", self.standard), ("Spread spectrum (%)", self.spread),
                            ("Spread mode", self.spread_mode), ("Spread rate (Hz)", self.spread_rate),
                            ("R_DS(on)", self.rds_on),
                            ("R_DS(on) low side", self.rds_on_low), ("Inductor DCR", self.dcr),
                            ("Diode V_f (V)", self.diode_vf)):
            text = wx.StaticText(fw, label=label)
            tip = FIELD_TIPS.get(label, "")
            if tip:                          # on the label and the field: hovering either explains it
                text.SetToolTip(tip)
                ctrl.SetToolTip(tip)
            form.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(rows.get(ctrl, ctrl), 1, wx.EXPAND)

    def _form_layout(self):
        """The buttons, the labels' help and the tab's layout (the last build step)."""
        fw, form = self.form_win, self._form
        del self._rows, self._form
        inner = wx.BoxSizer(wx.VERTICAL)
        inner.Add(form, 0, wx.EXPAND | wx.RIGHT, 12)        # room for the scroll bar
        fw.SetSizer(inner)
        # a scrolled window asks for no width of its own: give it the form's, or the wide list squeezes it
        fw.SetMinSize(wx.Size(inner.GetMinSize().width + wx.SystemSettings.GetMetric(wx.SYS_VSCROLL_X), 120))
        btns = button_row(self, [("Add", self.on_add), ("Update selected", self.on_update),
                                 ("Remove selected", self.on_remove), ("Mark reviewed", self.on_reviewed),
                                 ("Use schematic values", self.on_schematic), ("Detect…", self.on_detect)],
                          BUTTON_TIPS)
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(fw, 1, wx.EXPAND)
        right.Add(btns, 0, wx.TOP | wx.EXPAND, 8)
        from ..analysis.tags import HELP
        self.info = InfoBox(self, "Labels (GT field)", HELP["converters"])
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(self.hint, 0, wx.BOTTOM, 6)
        left.Add(self.list, 1, wx.EXPAND)
        left.Add(self.zoom, 0, wx.TOP, 6)
        left.Add(self.info, 0, wx.EXPAND | wx.TOP, 6)
        s = wx.BoxSizer(wx.HORIZONTAL)
        s.Add(left, 3, wx.EXPAND | wx.ALL, 6)
        s.Add(right, 2, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(s)
        self.hint.Hide()
        # a resize re-wraps the buttons: repaint everything, or a moved drawn button leaves its old picture behind
        self.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), self.Refresh()))

    def load(self, board, settings):
        """Show this board's power stages, and the GT labels found on it in the help box's status line."""
        self.board, self.settings = board, settings
        for combo in (self.rail, self.sw, self.ground):
            set_lazy_items(combo, board.nets)
        self.converters = list(settings.config.converters)
        from ..analysis.tags import board_tags
        self.info.set_status(board_tags(board).summary())
        self.refresh()

    def refresh(self):
        """Rebuild the list from self.converters: unreviewed detected stages in the warning colour, disabled ones
        greyed."""
        self.hint.Show(self.board is not None and not self.converters)
        self.Layout()
        self.list.DeleteAllItems()
        for c in self.converters:
            i = self.list.InsertItem(self.list.GetItemCount(), c.name)
            volts = f"{c.v_in or '?'}→{c.v_out or '?'}"
            for col, v in enumerate([source_status(c.source, c.reviewed), c.topology, _switches(c), c.rail,
                                     format_si(c.f_sw, "Hz"), f"{c.i_out:g}", volts, format_si(c.rise_time, "s"),
                                     " / ".join(c.input_connector), " / ".join(c.output_connector)], 1):
                self.list.SetItem(i, col, v)
            if c.source == "auto" and not c.reviewed:
                self.list.SetItemTextColour(i, theme.colour(theme.palette().warn))   # unreviewed
            self._show_enabled(i, c)

    def on_select(self, evt):
        """A row was selected: fill the form with its stage, and the marks beside the part parameters."""
        self._selection_changed()
        c = self.converters[evt.GetIndex()]
        self.name.SetValue(c.name)
        self.topology.SetStringSelection(c.topology)
        self.rail.SetValue(c.rail)
        self.sw.SetValue(c.switch_node or "")
        self.ground.SetValue(c.ground)
        self.high.SetValue(", ".join(c.high_side))
        self.low.SetValue(", ".join(c.low_side))
        self.integrated.SetValue(", ".join(c.switch))
        self.f_sw.SetValue(format_si(c.f_sw, "Hz"))
        self.i_out.SetValue(f"{c.i_out:g}")
        self.v_in.SetValue("" if c.v_in is None else f"{c.v_in:g}")
        self.v_out.SetValue("" if c.v_out is None else f"{c.v_out:g}")
        self.rise.SetValue(format_si(c.rise_time, "s"))
        self.c_oss.SetValue("" if c.c_oss is None else format_si(c.c_oss, "F"))
        self.q_oss.SetValue("" if c.q_oss is None else format_si(c.q_oss, "C"))
        self.c_stray.SetValue("" if c.c_stray is None else format_si(c.c_stray, "F"))
        for ctrl, v in ((self.rds_on, c.rds_on), (self.rds_on_low, c.rds_on_low), (self.dcr, c.dcr)):
            ctrl.SetValue("" if v is None else format_si(v, "Ω"))
        self.diode_vf.SetValue("" if c.diode_vf is None else f"{c.diode_vf:g}")
        self.inp.SetValue(", ".join(c.input_connector))
        self.out.SetValue(", ".join(c.output_connector))
        self.standard.SetStringSelection(c.conducted or STANDARD_CHOICES[0])
        self.spread.SetValue("" if not c.spread else f"{c.spread * 100:g}")
        self.spread_mode.SetStringSelection(c.spread_mode if c.spread_mode in SPREAD_MODES else SPREAD_MODES[0])
        self.spread_rate.SetValue("" if c.spread_rate is None else format_si(c.spread_rate, "Hz"))
        self._show_marks(c)

    def _show_marks(self, c: Optional[ConverterSpec]):
        """The part parameters' marks for stage c (◆ from the schematic, ! set here and different; none for None)."""
        from ..components.fields import stage_marks
        marks = stage_marks(self.board, self.settings.config, c) if c is not None and self.board else {}
        for key, mark in self.marks.items():
            mark.show(*marks.get(key, ("", "")))

    def _form(self) -> Optional[ConverterSpec]:
        """The form as a ConverterSpec, or None after telling the user what is wrong. It is checked by the
        settings' own validation, run on a copy of the settings holding only this stage."""
        def refs(ctrl):
            """A field's comma-separated references."""
            return [r.strip() for r in ctrl.GetValue().split(",") if r.strip()]

        def num(ctrl, unit):
            """A field's number (its unit may be typed after it), None when empty."""
            return parse_field(ctrl.GetValue(), unit)
        std = self.standard.GetStringSelection()
        spec = ConverterSpec(
            name=self.name.GetValue().strip() or self.sw.GetValue() or "converter", rail=self.rail.GetValue(),
            ground=self.ground.GetValue(), switch_node=self.sw.GetValue() or None, high_side=refs(self.high),
            low_side=refs(self.low), switch=refs(self.integrated), topology=self.topology.GetStringSelection(),
            f_sw=num(self.f_sw, "Hz") or 0.0, i_out=num(self.i_out, "A") or 0.0, v_in=num(self.v_in, "V"),
            v_out=num(self.v_out, "V"), rise_time=num(self.rise, "s") or 0.0, c_oss=num(self.c_oss, "F"),
            q_oss=num(self.q_oss, "C"), c_stray=num(self.c_stray, "F"), rds_on=num(self.rds_on, "Ω"),
            rds_on_low=num(self.rds_on_low, "Ω"), dcr=num(self.dcr, "Ω"), diode_vf=num(self.diode_vf, "V"),
            input_connector=refs(self.inp), output_connector=refs(self.out),
            conducted=None if std == STANDARD_CHOICES[0] else std,
            spread=(parse_field(self.spread.GetValue().replace("%", ""), "") or 0.0) / 100 or None,
            spread_mode=self.spread_mode.GetStringSelection(),
            spread_rate=num(self.spread_rate, "Hz"))
        from ..config.settings import validate
        from copy import copy
        probe = copy(self.settings.config)
        probe.nets, probe.converters = [], [spec]
        problems = validate(probe, self.board)
        if not (spec.f_sw and spec.i_out and spec.rise_time):
            problems.append("switching frequency, load current and edge time must be numbers (e.g. 400kHz, 3, 5ns)")
        for ctrl, what in ((self.spread, "spread spectrum (a percentage, e.g. 20)"),
                           (self.spread_rate, "spread rate (a frequency, e.g. 10kHz)")):
            text = ctrl.GetValue().replace("%", "").strip()
            if text and parse_field(text, "Hz") is None:
                problems.append(f"{what}: '{ctrl.GetValue()}' is not a number")
        if problems:
            wx.MessageBox("\n".join(problems), "groundtruth", wx.ICON_WARNING)
            return None
        return spec

    def _commit(self) -> bool:
        """Write the list to the settings and show it again; False (the list restored from the settings) when the
        settings refuse it."""
        try:
            self.settings.set_converters(self.converters)
        except ConfigError as e:
            wx.MessageBox("\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
            self.converters = list(self.settings.config.converters)
            return False
        self.refresh()
        self.on_change()
        return True

    def on_add(self, _):
        """Add the form's stage; its parameters equal to the schematic fields are linked to them (link_form)."""
        from ..components.fields import link_form
        spec = self._form()
        if spec:
            link_form(self.board, self.settings.config, spec)
            self.converters.append(spec)
            self._commit()
            self._show_marks(spec)

    def _row_names(self, rows: list[int]) -> list[str]:
        """The stages of these rows (what is shown in KiCad for a selection)."""
        return [self.converters[i].name for i in rows if i < len(self.converters)]

    def on_update(self, _):
        """Replace the selected stage with the form's values, keeping what the form does not show."""
        rows = self.selected_rows()
        if len(rows) > 1:
            wx.MessageBox("Select a single power stage to update it from the form.", "GroundTruth",
                          wx.ICON_INFORMATION)
            return
        i = rows[0] if rows else -1
        spec = self._form() if rows else None
        if i >= 0 and spec:
            from ..components.fields import link_form
            old = self.converters[i]
            # what the form does not show stays as it was (the spread spectrum is on the form since 2026-10-17)
            new = replace(spec, source=old.source, reviewed=True, enabled=old.enabled, r_damp=old.r_damp,
                          duty=old.duty, note=old.note,
                          snubber=list(old.snubber), snubber_c=old.snubber_c, snubber_r=old.snubber_r)
            link_form(self.board, self.settings.config, new, old)
            self.converters[i] = new
            self._commit()
            self.reselect([i])               # the list was rebuilt: the stage stays selected for the next Update
            self._show_marks(new)

    def on_schematic(self, _):
        """Take the selected stages' part parameters from their parts' schematic fields."""
        from ..components.fields import use_schematic
        rows = self.selected_rows()
        if not rows or self.board is None:
            return
        taken = {}
        for i in rows:
            c = replace(self.converters[i], from_schematic=list(self.converters[i].from_schematic))
            taken[c.name] = use_schematic(self.board, self.settings.config, c)
            self.converters[i] = c
        if not any(taken.values()):
            wx.MessageBox("The selected stages' parts have no schematic fields for R_DS(on), C_oss, Q_oss, DCR or V_f "
                          "(Help: Part parameters lists the field names).", "GroundTruth", wx.ICON_INFORMATION)
            return
        if self._commit():
            self.reselect(rows)

    def on_remove(self, _):
        """Remove the selected stages."""
        rows = set(self.selected_rows())
        if rows:
            self.converters = [c for i, c in enumerate(self.converters) if i not in rows]
            self._commit()

    def on_reviewed(self, _):
        """Mark the selected stages as reviewed, keeping them selected."""
        rows = self.selected_rows()
        if rows:
            for i in rows:
                self.converters[i] = replace(self.converters[i], reviewed=True)
            if self._commit():
                self.reselect(rows)

    def on_detect(self, _):
        """Find power stages as a background job."""
        from ..analysis.detect import detect_converters
        if self.board is None:
            return
        board, config = self.board, self.settings.config
        self.run_job("detecting power stages…", lambda progress: detect_converters(board, config), self._detected,
                     "detect power stages")

    def _detected(self, found):
        """Detection finished: let the user pick which new stages to add (with the part models detection
        suggests for them)."""
        if not found:
            wx.MessageBox("No power stages found (an inductor next to a switching transistor, a catch diode or an "
                          "IC's SW pin).", "groundtruth")
            return
        with ConverterDetectDialog(self, found, {frozenset(c.parts) for c in self.converters}) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                chosen = dlg.selected()
                if chosen:
                    # a surge stopper's FET on the input path, as a closed switch (only where its model is 'open')
                    self.settings.add_suggested_models({r: m for d in found if d.spec in chosen
                                                        for r, m in d.models.items()})
                    self.converters.extend(chosen)
                    self._commit()
