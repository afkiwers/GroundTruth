"""Bridges tab: motor drives (half-bridge legs on one bus, six-step commutation) to analyse.

Bridges are entered by hand or found by "Detect…" (half-bridge legs whose switch node has no inductor: the motor's
winding is the load). Suggestions are added as unreviewed: the operating point (bus voltage, PWM frequency, phase
current, speed) and the motor (line-to-line R and L, back-EMF constant, pole pairs, star or delta) cannot be read
from the board, and Simulate needs them.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

import wx
from .messages import box as message_box   # laid-out message boxes (ui/messages.py)

from ..components.values import format_si
from ..config.settings import (CONDUCTED_STANDARDS, CONNECTIONS, DRIVE_MODES, EMF_SHAPES, PWM_SCHEMES, BridgeSpec,
                               ConfigError, LegSpec, MotorSpec, simulation_blockers)
from . import theme
from .listsel import SelectableRows
from .widgets import InfoBox, PickDialog, button_row, parse_field, set_lazy_items, source_status, with_mark

STANDARD_CHOICES = ["(analysis default)"] + list(CONDUCTED_STANDARDS)

FIELD_TIPS = {
    "Name": "A name for this bridge, used in the lists, the Results tab and the reports.",
    "Bus net": "The net of the legs' high sides and the bus capacitors, e.g. V_BLDC or +BATT.",
    "Ground net": "The power ground the low sides return to, e.g. GND.",
    "Input connector": "Where the bus comes in, + and - pins, e.g. J5.1, J5.2 (two wire tabs: J2.1, J3.1). The LISN "
                       "of the conducted estimate goes there.",
    "Conducted standard": "The conducted limit for this bridge; the analysis default otherwise.",
    "Bus voltage (V)": "The bus voltage at the operating point (20-75 V for a 48 V system).",
    "PWM frequency": "The timer's switching frequency, e.g. 20kHz or 100kHz (set in the firmware).",
    "Phase current (A)": "The mean current in the conducting phases (the motor's DC-equivalent current): 0-500 A. "
                         "Its direction is the mode.",
    "Mode": "motoring: current from the bus into the motor; generating: back into the bus (synchronous rectification).",
    "Speed (rpm)": "Mechanical speed; 0 for a locked rotor (cranking). Sets the back-EMF and the commutation rate.",
    "PWM scheme": "hpwm_lon: the high side of the sourcing phase chops, the sinking phase's low side stays on; "
                  "hon_lpwm: the mirror; bipolar: both conducting legs chop, complementary.",
    "Synchronous": "The chopping leg's other switch conducts in the off time (complementary PWM with dead time); "
                   "unticked: the current freewheels through reverse conduction.",
    "Dead time": "Before every turn-on, e.g. 47ns (the driver's DT resistor, plus the timer's own dead time).",
    "Edge time": "The switch-node edge, e.g. 2ns for GaN: sets the high-frequency spectrum and L·di/dt.",
    "R_DS(on)": "One high-side switch (two in parallel share the current), e.g. 1.4m for an EPC2302.",
    "R_DS(on) low side": "One low-side switch; empty: the same as the high side.",
    "Reverse conduction (V)": "An off switch conducting backwards in the dead time: ~1.5-2 V for GaN, ~0.8 V for a "
                              "silicon FET's body diode.",
    "C_oss": "The switch node's output capacitance, all parallel switches together, e.g. 2n for two EPC2302: sets "
             "the hot loop's ringing.",
    "Q_oss": "Output charge, all parallel switches together, e.g. 170n for two EPC2302: Q_oss·V is lost at every "
             "hard turn-on.",
    "Sweep currents (A)": "Phase currents Simulate also evaluates, e.g. 50, 100, 200 (every one at every sweep "
                          "speed), from the operating point's solves: no extra solves, a few seconds per point.",
    "Sweep speeds (rpm)": "Speeds for the sweep, e.g. 0, 1000, 3000; empty: the operating point's speed.",
    "C stray (chassis)": "Phase node, motor cable and frame to chassis (common mode); optional.",
    "Connection": "The motor's winding: star or delta. Given its terminal data it only changes the back-EMF shape "
                  "the terminals see (a delta has a circulating current inside).",
    "R line-to-line": "Resistance between two motor terminals, e.g. 20m (as datasheets give it, or as measured at "
                      "the connector).",
    "L line-to-line": "Inductance between two motor terminals, e.g. 40uH.",
    "Back-EMF (V/krpm)": "Line-to-line back-EMF peak per 1000 rpm (ke). From Kv: ke = 1000·√2 / Kv for a sinusoidal "
                         "motor (Kv in rpm/V RMS), about 1000 / Kv otherwise.",
    "Pole pairs": "The motor's pole pairs (poles / 2): sets the electrical frequency.",
    "EMF shape": "trapezoidal (a BLDC motor) or sinusoidal (a PMSM).",
    "Advance (°)": "Commutation advance in electrical degrees (negative: retarded).",
    "Phase": "The leg's phase name (A, B, C).",
    "Switch node": "The leg's phase node, e.g. PH_A.",
    "High side": "The leg's high-side switches, bus to phase node, e.g. Q1, Q13.",
    "Low side": "The leg's low-side switches, phase node to ground, e.g. Q2, Q14.",
    "Output pin": "The motor connector pin of the phase, e.g. J1.A (optional).",
}
BUTTON_TIPS = {
    "Add": "Add the bridge in the form as a new entry.",
    "Update selected": "Replace the selected bridge with the form (counts as reviewed).",
    "Remove selected": "Remove the selected bridges.",
    "Mark reviewed": "Mark the selected detected bridges as checked.",
    "Use schematic values": "Put the selected bridges' FET parameters (R_DS(on), V_SD, C_oss, Q_oss) back to what the "
                            "FETs' schematic fields say, where they have them (Help: Part parameters).",
    "Detect…": "Find motor bridges on the board: half-bridge legs whose switch node goes to the motor (no inductor).",
    "Set leg": "Replace the selected leg with the leg fields.",
    "Add leg": "Add a leg from the leg fields.",
    "Remove leg": "Remove the selected leg.",
}


def _status(b: BridgeSpec) -> str:
    """The Status column: a bridge Simulate cannot run yet (no operating point or motor) says so first."""
    if simulation_blockers(b):
        return "set operating point" if b.source != "auto" or b.reviewed else "auto, SET UP"
    return source_status(b.source, b.reviewed)


def _refs(text: str) -> list[str]:
    """Comma-separated entries of a field."""
    return [r.strip() for r in text.split(",") if r.strip()]


def _fmt(v, unit: str = "") -> str:
    """A value for a field in SI notation, empty when not set."""
    return "" if v is None else format_si(v, unit)


class BridgeDetectDialog(PickDialog):
    """Pick which detected motor bridges to add. Complete bridges of medium or high confidence are pre-ticked."""

    def __init__(self, parent, found, listed: set):
        """found: detect_bridges' candidates; listed: the part sets of the bridges already in the settings."""
        def row(d):
            """A candidate's cells: name, its legs (phase: high side / low side), bus, input, confidence, reasons."""
            b = d.spec
            legs = "; ".join(f"{leg.phase}: {', '.join(leg.high_side)} / {', '.join(leg.low_side)}" for leg in b.legs)
            return [b.name, legs, b.rail, " / ".join(b.input_connector), d.confidence, "; ".join(d.reasons)]
        super().__init__(parent, "Suggested motor bridges",
                         [("Name", 150), ("Legs", 320), ("Bus", 150), ("Input", 100), ("Confidence", 80), ("Why", 600)],
                         found, [d for d in found if frozenset(d.spec.parts) not in listed], row,
                         lambda d: d.complete and d.confidence != "low",
                         "Ticked bridges are added as unreviewed: set the operating point and the motor before "
                         "Simulate.")


class BridgesPanel(SelectableRows, wx.Panel):
    """The Motor bridges tab: the bridges to analyse (a list), the legs of the bridge in the form (a second list),
    and the form: nets, drive (operating point, PWM), switch parameters, the motor, a sweep of operating points and
    the selected leg. Every change of the bridge list is written to the settings at once; the legs are edited in
    the form and saved with the bridge (Add / Update)."""

    def __init__(self, parent, on_change, run_job=None, on_show=None, in_steps: bool = False):
        """run_job(status, work, done, what): runs work(progress) off the GUI thread, then done(result).
        on_show(names, zoom): the selected rows changed: show those bridges in KiCad (live mode only).
        in_steps: the form is built later, in five `build_steps` (MainFrame._build_pages runs them one
        event-loop step each: the whole tab in one step blocked the GUI thread 0.32-0.43 s); else at once."""
        super().__init__(parent)
        self.on_change = on_change
        self.run_job = run_job or (lambda status, work, done, what: done(work(lambda *a: None)))
        self.board = None
        self.settings = None
        self.bridges: list[BridgeSpec] = []
        self.legs: list[LegSpec] = []          # the legs of the bridge in the form

        self.hint = wx.StaticText(self, label="No motor bridges yet: press Detect… to find half-bridge legs driving a "
                                              "motor, or fill in the form and press Add.")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        for i, (c, w) in enumerate([("Name", 130), ("Status", 120), ("Legs", 50), ("Bus", 120), ("V", 45),
                                    ("f_pwm", 65), ("I phase", 60), ("rpm", 55), ("Motor", 150), ("Input", 90)]):
            self.list.InsertColumn(i, c, width=w)
        self.list.SetMinSize(wx.Size(320, 100))
        self._init_rows(on_show, "bridges")
        self._init_enable("bridges")
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.leg_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (c, w) in enumerate([("Phase", 50), ("Switch node", 150), ("High side", 90), ("Low side", 90),
                                    ("Output", 60), ("Driver", 55), ("Driven from", 220)]):
            self.leg_list.InsertColumn(i, c, width=w)
        self.leg_list.SetMinSize(wx.Size(320, 90))
        self.leg_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_leg_select)

        self.form_win = fw = wx.ScrolledWindow(self, style=wx.VSCROLL)
        fw.SetScrollRate(0, 12)
        btns = button_row(self, [("Add", self.on_add), ("Update selected", self.on_update),
                                 ("Remove selected", self.on_remove), ("Mark reviewed", self.on_reviewed),
                                 ("Use schematic values", self.on_schematic),
                                 ("Detect…", self.on_detect), ("Set leg", self.on_set_leg),
                                 ("Add leg", self.on_add_leg), ("Remove leg", self.on_remove_leg)], BUTTON_TIPS)
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(fw, 1, wx.EXPAND)
        right.Add(btns, 0, wx.TOP | wx.EXPAND, 8)
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(self.list_head(self.hint), 0, wx.EXPAND | wx.BOTTOM, 6)
        left.Add(self.list, 2, wx.EXPAND)
        left.Add(wx.StaticText(self, label="Legs of the bridge in the form:"), 0, wx.TOP, 6)
        left.Add(self.leg_list, 1, wx.EXPAND | wx.TOP, 2)
        # the victim nets (every bridge's and power stage's Simulate looks at them): what is set, and the picker
        self.victims_line = wx.StaticText(self, label="Victim nets: the board is not loaded yet")
        self.victims_btn = theme.Button(self, "Victims…")
        self.victims_btn.SetToolTip("pick the nets the switching may upset (Simulate checks the noise they pick up): "
                                    "add or remove one or several at once, or go back to finding them automatically")
        self.victims_btn.Bind(wx.EVT_BUTTON, lambda e: self.on_victims())
        vrow = wx.BoxSizer(wx.HORIZONTAL)
        vrow.Add(self.victims_line, 1, wx.ALIGN_CENTER_VERTICAL)
        vrow.Add(self.victims_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        left.Add(vrow, 0, wx.EXPAND | wx.TOP, 6)
        left.Add(self.zoom, 0, wx.TOP, 6)
        from ..analysis.tags import HELP
        self.info = InfoBox(self, "Labels (GT field)", HELP["bridges"])
        left.Add(self.info, 0, wx.EXPAND | wx.TOP, 6)
        s = wx.BoxSizer(wx.HORIZONTAL)
        s.Add(left, 3, wx.EXPAND | wx.ALL, 6)
        s.Add(right, 2, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(s)
        self.hint.Hide()
        self.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), self.Refresh()))
        self.build_steps = [self._form_bridge, self._form_drive, self._form_parts, self._form_legs, self._form_layout]
        while self.build_steps and not in_steps:
            self.build_steps.pop(0)()

    # ------------------------------------------------------------------ the form, in five build steps
    def _text(self, value: str = "") -> wx.TextCtrl:
        """A text field in the form."""
        return wx.TextCtrl(self.form_win, value=value)

    def _choice(self, items, first: int = 0) -> wx.Choice:
        """A choice in the form with entry `first` selected."""
        c = wx.Choice(self.form_win, choices=list(items))
        c.SetSelection(first)
        return c

    def _add_rows(self, rows, marked: Optional[dict] = None):
        """Rows of the form: (label, control), None for a gap, a string for a heading."""
        fw, form = self.form_win, self._form_sizer
        for row in rows:
            if row is None or isinstance(row, str):        # a gap, or a heading
                form.Add(wx.StaticText(fw, label=row or ""), 0, wx.TOP, 4)
                form.Add(wx.StaticText(fw, label=""), 0)
                continue
            label, ctrl = row
            text = wx.StaticText(fw, label=label)
            tip = FIELD_TIPS.get(label, "")
            if tip:
                text.SetToolTip(tip)
                ctrl.SetToolTip(tip)
            form.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add((marked or {}).get(ctrl, ctrl), 1, wx.EXPAND)

    def _form_bridge(self):
        """The bridge's nets (the form's ~45 controls are built in five steps, one event-loop step each,
        `build_steps`: three blocked the window 97-131 ms each on refboard's copy)."""
        T, fw = self._text, self.form_win
        self._form_sizer = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        self._form_sizer.AddGrowableCol(1)
        self.name = T()
        self.rail = wx.ComboBox(fw, style=wx.CB_DROPDOWN)
        self.ground = wx.ComboBox(fw, style=wx.CB_DROPDOWN)
        self.inp = T()
        self.standard = self._choice(STANDARD_CHOICES)
        self._add_rows((("Name", self.name), ("Bus net", self.rail), ("Ground net", self.ground),
                        ("Input connector", self.inp), ("Conducted standard", self.standard), None))

    def _form_drive(self):
        """The drive."""
        T, fw = self._text, self.form_win
        self.v_bus, self.f_pwm, self.i_phase = T(), T("20kHz"), T()
        self.mode = self._choice(DRIVE_MODES)
        self.speed = T()
        self.pwm = self._choice(PWM_SCHEMES)
        self.sync = wx.CheckBox(fw)
        self.sync.SetValue(True)
        self.dead, self.rise = T("50ns"), T("2ns")
        self._add_rows((("Bus voltage (V)", self.v_bus), ("PWM frequency", self.f_pwm),
                        ("Phase current (A)", self.i_phase), ("Mode", self.mode), ("Speed (rpm)", self.speed),
                        ("PWM scheme", self.pwm), ("Synchronous", self.sync), ("Dead time", self.dead),
                        ("Edge time", self.rise), None))

    def _form_parts(self):
        """The switches' parameters and the motor."""
        T, fw = self._text, self.form_win
        self.rds_on, self.rds_on_low, self.v_sd = T(), T(), T()
        self.c_oss, self.q_oss, self.c_stray = T(), T(), T()
        self.connection = self._choice(CONNECTIONS)
        self.r_ll, self.l_ll, self.ke_ll, self.poles = T(), T(), T(), T()
        self.emf = self._choice(EMF_SHAPES)
        self.advance = T("0")
        # the part parameters the schematic fields can give: a mark beside each (◆ from the schematic, ! differs)
        self.marks, marked = {}, {}
        for key, ctrl in (("rds_on", self.rds_on), ("rds_on_low", self.rds_on_low), ("v_sd", self.v_sd),
                          ("c_oss", self.c_oss), ("q_oss", self.q_oss)):
            marked[ctrl], self.marks[key] = with_mark(fw, ctrl)
        self._add_rows((("R_DS(on)", self.rds_on), ("R_DS(on) low side", self.rds_on_low),
                        ("Reverse conduction (V)", self.v_sd), ("C_oss", self.c_oss), ("Q_oss", self.q_oss),
                        ("C stray (chassis)", self.c_stray), "Motor",
                        ("Connection", self.connection), ("R line-to-line", self.r_ll),
                        ("L line-to-line", self.l_ll), ("Back-EMF (V/krpm)", self.ke_ll), ("Pole pairs", self.poles),
                        ("EMF shape", self.emf), ("Advance (°)", self.advance)), marked)

    def _form_legs(self):
        """The sweep and the selected leg."""
        T, fw = self._text, self.form_win
        self.sweep_i, self.sweep_rpm = T(), T()
        self.leg_phase, self.leg_node, self.leg_high, self.leg_low, self.leg_out = (
            T(), wx.ComboBox(fw, style=wx.CB_DROPDOWN), T(), T(), T())
        self._add_rows(("Sweep", ("Sweep currents (A)", self.sweep_i), ("Sweep speeds (rpm)", self.sweep_rpm),
                        "Selected leg", ("Phase", self.leg_phase), ("Switch node", self.leg_node),
                        ("High side", self.leg_high), ("Low side", self.leg_low), ("Output pin", self.leg_out)))

    def _form_layout(self):
        """The form laid out, and the tab with it."""
        fw = self.form_win
        inner = wx.BoxSizer(wx.VERTICAL)
        inner.Add(self._form_sizer, 0, wx.EXPAND | wx.RIGHT, 12)
        fw.SetSizer(inner)
        fw.SetMinSize(wx.Size(inner.GetMinSize().width + wx.SystemSettings.GetMetric(wx.SYS_VSCROLL_X), 120))
        self.Layout()

    # ------------------------------------------------------------------ lists
    def load(self, board, settings):
        """Show this board's bridges, and the GT labels found on it in the help box's status line."""
        self.board, self.settings = board, settings
        for combo in (self.rail, self.ground, self.leg_node):
            set_lazy_items(combo, board.nets)
        self.bridges = list(settings.config.bridges)
        from ..analysis.tags import board_tags
        self.info.set_status(board_tags(board).summary())
        self.refresh()
        self._show_victims()

    # ------------------------------------------------------------------ victims
    def _show_victims(self):
        """The line above Victims…: automatic, or the nets listed (the first few by name)."""
        v = self.settings.config.victims if self.settings is not None else None
        if v is None:
            return
        if not v.nets:
            text = "Victim nets: automatic (reset, crystal, Hall, debug and PWM nets, found by name at each run)"
        else:
            names = ", ".join(n.split("/")[-1] for n in v.nets[:4]) + (" …" if len(v.nets) > 4 else "")
            text = f"Victim nets: {len(v.nets)} listed ({names})"
        if not v.enabled:
            text += " · switched off (victims.enabled: false)"
        self.victims_line.SetLabel(text)
        self.victims_line.SetToolTip("\n".join(v.nets) if v.nets else text)

    def on_victims(self):
        """Victims…: the automatic victim nets found in a job (they leave out every stage's own nets), then the
        picker; OK writes the list (an undo step like any edit)."""
        if self.board is None or self.settings is None:
            return
        board, config = self.board, self.settings.config

        def work(progress):
            """(worker) The board's automatic victim nets."""
            from ..analysis.victims import board_auto_nets
            return board_auto_nets(board, config)

        def done(auto):
            """The picker; its list written on OK."""
            from .victims_dialog import VictimsDialog
            with VictimsDialog(self, list(board.nets), list(config.victims.nets), auto) as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                nets = dlg.values()
            if nets == list(self.settings.config.victims.nets):
                return
            try:
                self.settings.set_victim_nets(nets)
            except ConfigError as e:
                message_box("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
                return
            self._show_victims()
            self.on_change()
        self.run_job("finding the victim nets…", work, done, "find the victim nets")

    def refresh(self):
        """Rebuild the bridge list: unreviewed or not yet set up bridges in the warning colour."""
        self.hint.Show(self.board is not None and not self.bridges)
        self.Layout()
        self.list.DeleteAllItems()
        for b in self.bridges:
            i = self.list.InsertItem(self.list.GetItemCount(), b.name)
            m = b.motor
            motor = (f"{m.connection}, {_fmt(m.r_ll, 'Ω')}, {_fmt(m.l_ll, 'H')}" if m.r_ll is not None else "not set")
            for col, v in enumerate([_status(b), str(len(b.legs)), b.rail.split("/")[-1],
                                     "" if b.v_bus is None else f"{b.v_bus:g}", format_si(b.f_pwm, "Hz"),
                                     "" if b.i_phase is None else f"{b.i_phase:g}",
                                     "" if b.speed_rpm is None else f"{b.speed_rpm:g}", motor,
                                     " / ".join(b.input_connector)], 1):
                self.list.SetItem(i, col, v)
            if (b.source == "auto" and not b.reviewed) or simulation_blockers(b):
                self.list.SetItemTextColour(i, theme.colour(theme.palette().warn))
            self._show_enabled(i, b)

    def _show_legs(self):
        """Rebuild the leg list from self.legs (the form's bridge)."""
        self.leg_list.DeleteAllItems()
        for leg in self.legs:
            i = self.leg_list.InsertItem(self.leg_list.GetItemCount(), leg.phase)
            for col, v in enumerate([leg.switch_node, ", ".join(leg.high_side), ", ".join(leg.low_side), leg.output,
                                     leg.driver, ", ".join(n.split("/")[-1] for n in leg.inputs)], 1):
                self.leg_list.SetItem(i, col, v)

    def on_select(self, evt):
        """A bridge was selected: fill the form with it, and copy its legs for editing (the stored bridge changes
        only on Update)."""
        self._selection_changed()
        b = self.bridges[evt.GetIndex()]
        self.name.SetValue(b.name)
        self.rail.SetValue(b.rail)
        self.ground.SetValue(b.ground)
        self.inp.SetValue(", ".join(b.input_connector))
        self.standard.SetStringSelection(b.conducted or STANDARD_CHOICES[0])
        self.v_bus.SetValue("" if b.v_bus is None else f"{b.v_bus:g}")
        self.f_pwm.SetValue(format_si(b.f_pwm, "Hz"))
        self.i_phase.SetValue("" if b.i_phase is None else f"{b.i_phase:g}")
        self.mode.SetStringSelection(b.mode)
        self.speed.SetValue("" if b.speed_rpm is None else f"{b.speed_rpm:g}")
        self.pwm.SetStringSelection(b.pwm)
        self.sync.SetValue(b.synchronous)
        self.dead.SetValue(format_si(b.dead_time, "s"))
        self.rise.SetValue(format_si(b.rise_time, "s"))
        self.rds_on.SetValue(_fmt(b.rds_on, "Ω"))
        self.rds_on_low.SetValue(_fmt(b.rds_on_low, "Ω"))
        self.v_sd.SetValue("" if b.v_sd is None else f"{b.v_sd:g}")
        self.c_oss.SetValue(_fmt(b.c_oss, "F"))
        self.q_oss.SetValue(_fmt(b.q_oss, "C"))
        self.c_stray.SetValue(_fmt(b.c_stray, "F"))
        m = b.motor
        self.connection.SetStringSelection(m.connection)
        self.r_ll.SetValue(_fmt(m.r_ll, "Ω"))
        self.l_ll.SetValue(_fmt(m.l_ll, "H"))
        self.ke_ll.SetValue("" if m.ke_ll is None else f"{m.ke_ll:g}")
        self.poles.SetValue("" if m.pole_pairs is None else str(m.pole_pairs))
        self.emf.SetStringSelection(m.emf)
        self.advance.SetValue(f"{m.advance_deg:g}")
        self.sweep_i.SetValue(", ".join(f"{x:g}" for x in b.sweep_i_phase))
        self.sweep_rpm.SetValue(", ".join(f"{x:g}" for x in b.sweep_speed_rpm))
        self.legs = [replace(leg) for leg in b.legs]
        self._show_legs()
        self._show_marks(b)

    def _show_marks(self, b: Optional[BridgeSpec]):
        """The switch parameters' marks for bridge b (◆ from the schematic, ! set here and different)."""
        from ..components.fields import stage_marks
        marks = stage_marks(self.board, self.settings.config, b) if b is not None and self.board else {}
        for key, mark in self.marks.items():
            mark.show(*marks.get(key, ("", "")))

    def on_leg_select(self, evt):
        """A leg was selected: fill the Selected leg fields with it, and show that leg alone in KiCad (live mode)."""
        k = evt.GetIndex()
        if 0 <= k < len(self.legs):
            leg = self.legs[k]
            self.leg_phase.SetValue(leg.phase)
            self.leg_node.SetValue(leg.switch_node)
            self.leg_high.SetValue(", ".join(leg.high_side))
            self.leg_low.SetValue(", ".join(leg.low_side))
            self.leg_out.SetValue(leg.output)
            self._selection_changed(lambda: self._leg_names(leg.phase))

    def _leg_names(self, phase: str) -> list[str]:
        """The stage name ("bridge J2 A") of the selected bridge's leg of this phase, as KiCad shows it; the stored
        leg, since the form's legs count only after Update. Nothing when no single bridge with that leg is selected
        (a new bridge, or a leg added in the form): the bridges selected are shown then."""
        rows = self.selected_rows()
        if len(rows) == 1 and rows[0] < len(self.bridges):
            b = self.bridges[rows[0]]
            for leg in b.legs:
                if leg.phase == phase:
                    return [b.leg_name(leg)]
        return self._row_names(rows)

    def _leg_form(self) -> Optional[LegSpec]:
        """The Selected leg fields as a LegSpec (phase numbered when empty), or None when the switch node or a side
        is missing."""
        leg = LegSpec(self.leg_phase.GetValue().strip() or str(len(self.legs) + 1), self.leg_node.GetValue().strip(),
                      _refs(self.leg_high.GetValue()), _refs(self.leg_low.GetValue()), self.leg_out.GetValue().strip())
        if not leg.switch_node or not leg.high_side or not leg.low_side:
            message_box("A leg needs its switch node, high side and low side.", "groundtruth", wx.ICON_WARNING)
            return None
        return leg

    def on_set_leg(self, _):
        """Replace the selected leg with the fields (its gate driver, inputs and snubber, found by detection, kept)."""
        k = self.leg_list.GetFirstSelected()
        leg = self._leg_form() if k >= 0 else None
        if leg is not None:
            old = self.legs[k]
            self.legs[k] = replace(leg, driver=old.driver, inputs=old.inputs, snubber=old.snubber)
            self._show_legs()

    def on_add_leg(self, _):
        """Add the fields as a new leg."""
        leg = self._leg_form()
        if leg is not None:
            self.legs.append(leg)
            self._show_legs()

    def on_remove_leg(self, _):
        """Remove the selected leg."""
        k = self.leg_list.GetFirstSelected()
        if k >= 0:
            del self.legs[k]
            self._show_legs()

    # ------------------------------------------------------------------ the form
    def _form(self) -> Optional[BridgeSpec]:
        """The form as a BridgeSpec, or None after listing every problem at once (unreadable fields, missing
        required ones, and the settings' own checks run on a copy holding only this bridge)."""
        problems = []

        def need(ctrl, unit, what, optional=True):
            """A field's number; an unreadable or (not optional) missing one is noted as a problem."""
            v = parse_field(ctrl.GetValue(), unit)
            if v is None and ctrl.GetValue().strip():
                problems.append(f"{what}: cannot read {ctrl.GetValue()!r}")
            elif v is None and not optional:
                problems.append(f"{what} is needed")
            return v
        try:
            advance = float(self.advance.GetValue() or 0)
        except ValueError:
            advance, _ = 0.0, problems.append("advance must be a number of degrees")
        poles = need(self.poles, "", "pole pairs")
        std = self.standard.GetStringSelection()

        def values(ctrl, unit, what):
            """A field's comma-separated numbers (a sweep); unreadable entries are noted."""
            out = []
            for x in _refs(ctrl.GetValue()):
                v = parse_field(x, unit)
                if v is None:
                    problems.append(f"{what}: cannot read {x!r}")
                else:
                    out.append(v)
            return out
        b = BridgeSpec(
            name=self.name.GetValue().strip() or "bridge", rail=self.rail.GetValue(), ground=self.ground.GetValue(),
            legs=list(self.legs), input_connector=_refs(self.inp.GetValue()),
            conducted=None if std == STANDARD_CHOICES[0] else std,
            v_bus=need(self.v_bus, "V", "bus voltage"), f_pwm=need(self.f_pwm, "Hz", "PWM frequency", False) or 0.0,
            i_phase=need(self.i_phase, "A", "phase current"), mode=self.mode.GetStringSelection(),
            speed_rpm=need(self.speed, "rpm", "speed"), pwm=self.pwm.GetStringSelection(),
            synchronous=self.sync.GetValue(), dead_time=need(self.dead, "s", "dead time", False) or 0.0,
            rise_time=need(self.rise, "s", "edge time", False) or 0.0,
            rds_on=need(self.rds_on, "Ω", "R_DS(on)"), rds_on_low=need(self.rds_on_low, "Ω", "R_DS(on) low side"),
            v_sd=need(self.v_sd, "V", "reverse conduction"), c_oss=need(self.c_oss, "F", "C_oss"),
            q_oss=need(self.q_oss, "C", "Q_oss"), c_stray=need(self.c_stray, "F", "C stray"),
            motor=MotorSpec(self.connection.GetStringSelection(), need(self.r_ll, "Ω", "R line-to-line"),
                            need(self.l_ll, "H", "L line-to-line"), need(self.ke_ll, "", "back-EMF"),
                            None if poles is None else int(round(poles)), self.emf.GetStringSelection(), advance),
            sweep_i_phase=values(self.sweep_i, "A", "sweep currents"),
            sweep_speed_rpm=values(self.sweep_rpm, "rpm", "sweep speeds"))
        from copy import copy
        from ..config.settings import _bridge_problems, validate
        probe = copy(self.settings.config)
        probe.nets, probe.converters, probe.bridges = [], [], [b]
        problems += _bridge_problems(b) + validate(probe, self.board)
        if problems:
            message_box("\n".join(problems), "groundtruth", wx.ICON_WARNING)
            return None
        return b

    def _commit(self) -> bool:
        """Write the list to the settings and show it again; False (the list restored) when the settings refuse
        it."""
        try:
            self.settings.set_bridges(self.bridges)
        except ConfigError as e:
            message_box("\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
            self.bridges = list(self.settings.config.bridges)
            return False
        self.refresh()
        self.on_change()
        return True

    def on_add(self, _):
        """Add the form's bridge; its parameters equal to the schematic fields are linked to them."""
        from ..components.fields import link_form
        b = self._form()
        if b:
            link_form(self.board, self.settings.config, b)
            self.bridges.append(b)
            self._commit()
            self._show_marks(b)

    def _row_names(self, rows: list[int]) -> list[str]:
        """The bridges of these rows (what is shown in KiCad for a selection)."""
        return [self.bridges[i].name for i in rows if i < len(self.bridges)]

    def on_update(self, _):
        """Replace the selected bridge with the form's values, keeping what the form does not show."""
        rows = self.selected_rows()
        if len(rows) > 1:
            message_box("Select a single bridge to update it from the form.", "GroundTruth", wx.ICON_INFORMATION)
            return
        i = rows[0] if rows else -1
        b = self._form() if rows else None
        if i >= 0 and b:
            from ..components.fields import link_form
            old = self.bridges[i]
            new = replace(b, source=old.source, reviewed=True, enabled=old.enabled, note=old.note, r_damp=old.r_damp,
                          d_max=old.d_max)
            link_form(self.board, self.settings.config, new, old)
            self.bridges[i] = new
            self._commit()
            self._show_marks(new)

    def on_schematic(self, _):
        """Take the selected bridges' switch parameters from their FETs' schematic fields."""
        from ..components.fields import use_schematic
        rows = self.selected_rows()
        if not rows or self.board is None:
            return
        taken = {}
        for i in rows:
            b = replace(self.bridges[i], from_schematic=list(self.bridges[i].from_schematic))
            taken[b.name] = use_schematic(self.board, self.settings.config, b)
            self.bridges[i] = b
        if not any(taken.values()):
            message_box("The selected bridges' FETs have no schematic fields for R_DS(on), C_oss, Q_oss or V_SD "
                          "(Help: Part parameters lists the field names).", "GroundTruth", wx.ICON_INFORMATION)
            return
        if self._commit():
            self.reselect(rows)

    def on_remove(self, _):
        """Remove the selected bridges."""
        rows = set(self.selected_rows())
        if rows:
            self.bridges = [b for i, b in enumerate(self.bridges) if i not in rows]
            self._commit()

    def on_reviewed(self, _):
        """Mark the selected bridges as reviewed, keeping them selected."""
        rows = self.selected_rows()
        if rows:
            for i in rows:
                self.bridges[i] = replace(self.bridges[i], reviewed=True)
            if self._commit():
                self.reselect(rows)

    def on_detect(self, _):
        """Find motor bridges as a background job."""
        from ..analysis.detect import detect_bridges
        if self.board is None:
            return
        board, config = self.board, self.settings.config
        self.run_job("detecting motor bridges…", lambda progress: detect_bridges(board, config), self._detected,
                     "detect motor bridges")

    def _detected(self, found):
        """Detection finished: let the user pick which new bridges to add (with the part models detection
        suggests for them)."""
        if not found:
            message_box("No motor bridges found (half-bridge legs: a switch from the bus to a node and one from the "
                          "node to ground, the node going to a connector without an inductor).", "groundtruth")
            return
        with BridgeDetectDialog(self, found, {frozenset(b.parts) for b in self.bridges}) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                chosen = dlg.selected()
                if chosen:
                    self.settings.add_suggested_models({r: m for d in found if d.spec in chosen
                                                        for r, m in d.models.items()})
                    self.bridges.extend(chosen)
                    self._commit()
