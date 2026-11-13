"""Converters tab: switching power stages to analyse (hot loop, input capacitors, conducted emission).

Stages can be entered by hand or suggested by "Detect…" (from the inductors and the switches on their switch
nodes). Suggestions are added as unreviewed: the switching frequency, load current and voltages cannot be read from
the board and start as defaults.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

import wx
from .messages import box as message_box   # laid-out message boxes (ui/messages.py)

from ..components.values import format_si
from ..analysis.converter_sweep import DEFAULT_OUTPUTS
from ..config.settings import TOPOLOGIES, ConfigError, ConverterSpec
from . import limit_choice as lc
from . import theme
from .listsel import SelectableRows
from .widgets import (InfoBox, PickDialog, button_row, form_state, parse_field, restore_form, section_rows,
                      set_lazy_items, source_status, with_mark)

STANDARD_CHOICES = lc.standard_items()     # as TI WEBENCH names them; the class beside it (ui/limit_choice.py)
SPREAD_MODES = ("random", "triangle")      # ConverterSpec.spread_mode
# the Spread rate field's grey hint per mode (the user, of a triangle refused for its missing rate while the hint read
# "every cycle": "why do I have to define the spread rate if i define the spread spectrum?"): random hops every cycle
# without one, a triangle's modulation frequency has no default (a slow sweep barely lowers a reading, a fast one by
# several dB: emi/spread.py), so the hint says it is needed
SPREAD_RATE_HINTS = {"random": "every cycle (random)", "triangle": "needed: modulation frequency (datasheet)"}

# hover explanations of the form's fields (on the label and the field): what it is, where to find it, what it changes
FIELD_TIPS = {
    "Name": "A name for this power stage, used in the lists, the Results tab and the reports.",
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
    "Integrated stage": "An IC with both switches inside, e.g. U46. High side, low side and switch node are then "
                        "greyed: not used.",
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
                          "settings). CISPR 22 / CISPR 32: equipment on the mains or a DC network (CISPR 32 replaced "
                          "CISPR 22 with the same limits); CISPR 25: vehicles; MIL-STD-461 CE102: military.",
    "Conducted class": lc.CLASS_TIP,
    "Spread spectrum (%)": "The controller's spread spectrum: f_sw dithered from f_sw up to f_sw x (1 + this), e.g. "
                           "20 (the LTC7890's is +20 %). The receivers then see the harmonics spread over the band "
                           "(conducted and radiated estimates). Empty: off.",
    "Spread mode": "random: a new frequency every cycle (or Spread rate hops per second; most controllers' pseudo-"
                   "random dither, the LTC7890's too): no rate needed. triangle: swept up and down at Spread rate, "
                   "which it then needs (the controller's modulation frequency, from its datasheet): how fast the "
                   "sweep is decides how much a receiver's reading drops.",
    "Interleave group": "Stages with the same group name are phases of one controller (a dual or multiphase buck): "
                        "Simulate adds their input currents and hot-loop fields as phasors, so phases 180° apart "
                        "cancel at the odd harmonics of f_sw. Empty: the stage is analysed alone.",
    "Phase (°)": "This stage's delay in degrees of the switching period within its group (the controller's "
                 "datasheet; a dual buck's channels are usually 180° apart). Empty: the group's stages 360°/n apart "
                 "in list order.",
    "Spread rate (Hz)": "triangle only: its modulation frequency, from the controller's datasheet (e.g. 10kHz). "
                        "Greyed for random, which hops to a new frequency every switching cycle.",
    "R_DS(on)": "The high-side (or integrated) switch's on-resistance, e.g. 3m (datasheet, at the expected "
                "temperature): its conduction loss D·I²·R. Empty: left out of the thermal solve.",
    "R_DS(on) low side": "The low-side FET's on-resistance, e.g. 3m: its conduction loss (1−D)·I²·R. Empty: the "
                         "same as the high side.",
    "Inductor DCR": "The inductor's DC resistance (datasheet DCR), e.g. 6.3m: its winding loss I²·DCR. Empty: "
                    "the inductor's R / DCR on the Parts tab (its DCR field, or typed in there; not the suggested "
                    "50m placeholder), else left out. ◇: the same as that R; !: they differ (the losses use the value "
                    "here; click the ! or right-click the field to take the Parts tab's).",
    "Diode V_f (V)": "A catch or boost diode's forward voltage at the load current, e.g. 0.5: its loss "
                     "(1−D)·I·V_f. Empty: 0.5 V.",
    "Dead time, rise": "A synchronous stage's dead time from the low side turning off to the high side turning on "
                       "(the switch node's rising edge; the LTC7890's DTCA), e.g. 20ns. Both switches are off; the "
                       "inductor current meanwhile flows in reverse through the rectifier FET at V_SD. A buck's "
                       "hard edge: the whole dead time is reverse conduction (often as much loss as the FET's "
                       "conduction with GaN). Not the edge time (how fast the node swings once a switch turns on). "
                       "From the controller's datasheet and its dead-time pins, or a GT field on the controller IC: "
                       "dead_rise:20ns, or dead:20ns for both edges (per channel dead1_rise:20ns, per labelled "
                       "converter conv1_dead_rise:20ns). Empty: 20 ns (an adaptive controller's).",
    "Dead time, fall": "The dead time from the high side turning off to the low side turning on (the switch node's "
                       "falling edge; the LTC7890's DTCB), e.g. 20ns. A buck's soft edge: the inductor current first "
                       "swings the node down (the FETs' output charge, 2·Q_oss / I), and only the rest of the dead "
                       "time is reverse conduction. GT field: dead_fall:20ns (or dead:20ns for both). Empty: 20 ns.",
    "Reverse V_SD (V)": "The rectifier FET's reverse-conduction voltage in the dead time (datasheet V_SD; the FET's "
                        "Vsd field), e.g. 1.6 for GaN at the load current, 0.8 for a silicon body diode. Empty: "
                        "2 V GaN, 0.8 V silicon.",
    "Q_rr": "The rectifier FET's reverse-recovery charge (a silicon body diode's Q_rr at the load current; the FET's "
            "Qrr field), e.g. 60n. GaN has none: leave it empty. Its loss Q_rr·V·f_sw in the main switch, a current "
            "spike I_rr = √(2·Q_rr·di/dt) in the hot loop's spectrum, and the overshoot I_rr·√(L/C_oss) when it "
            "snaps off.",
    "Q_g": "One switching FET's total gate charge (the FET's Qg field), e.g. 12n: the efficiency sweep's gate-drive "
           "loss Q_g·V_gate·f_sw per FET. Empty: left out.",
    "Core loss": "The inductor's core loss from the maker's tool or curves, e.g. 150mW. Alone: the loss at this "
                 "stage's frequency and ripple. With 'at f' / 'at ΔI': a point of the maker's data, scaled to them.",
    "Core loss at f": "The frequency the core loss is given at, e.g. 300kHz (empty: the switching frequency).",
    "Core loss at ΔI (A)": "The ripple current, peak to peak, the core loss is given at (empty: this stage's "
                           "ripple, V·D(1−D)/(L·f_sw)).",
    "Core α (frequency)": "Steinmetz exponent of the frequency, P ∝ f^α (empty: 1.3, typical of a moulded powder "
                          "core; ferrite 1.2-1.8).",
    "Core β (ripple)": "Steinmetz exponent of the flux swing, P ∝ ΔI^β (empty: 2.2; ferrite 2.4-2.8).",
    "Gate drive (V)": "The gate-drive voltage the controller drives the FETs with, e.g. 5 (empty: 5 V assumed).",
    "Controller I_q (A)": "The controller's own supply current from the input, e.g. 2m (its datasheet's operating "
                          "or quiescent current while switching): the sweep's loss I_q·V_in. Empty: left out.",
    "Efficiency currents (A)": "The efficiency sweep's load currents, e.g. 0.5, 1, 2, 4, 6. Empty: 24 currents from "
                               "5 % to 120 % of the load current.",
    "Measured efficiency": "A CSV of bench measurements drawn over the model's efficiency curve: columns load "
                           "current (A), efficiency (% or 0-1) and optionally the loss (W); a header row is "
                           "skipped. A file name relative to the board's groundtruth/ folder, or a full path.",
    "Sweep V in (V)": "Input voltages to sweep at the nominal load current, e.g. 36, 48, 65. Empty: none.",
    "Sweep I out (A)": "Load currents to sweep at the nominal input voltage, e.g. 0.1, 0.5, 2. Empty: none.",
    "Sweep outputs": "What to work out at each point: emissions (radiated, conducted, overshoot), losses (and "
                     "efficiency), ripple (at the FB pin, and the control loop), temperatures (a thermal solve per "
                     "point: slow).",
}
BUTTON_TIPS = {
    "Add": "Add a new power stage from the form.",
    "Update selected": "Replace the selected power stage with the form's values (settings the form does not show "
                       "are kept). With several selected: the fields you changed go into each of them; a field "
                       "showing * (they differ) or left as it was keeps each stage's own value.",
    "Remove selected": "Remove the selected power stages (also the Delete key).",
    "Mark reviewed": "Mark the selected auto-detected stages as checked by you (they stop showing in orange).",
    "Use schematic values": "Put the selected stages' part parameters (R_DS(on), C_oss, Q_oss, DCR, V_f, V_SD, Q_rr, "
                            "and the dead time from the controller's GT field) back to what the schematic says, "
                            "where it says it (Help: Part parameters).",
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


class SweepOutputs(wx.Panel):
    """The sweep's outputs as check boxes, with a text control's interface for the form (GetValue / SetValue:
    "emissions, losses"; the form's MIXED shows every box undetermined, and reads back as MIXED until one is
    clicked)."""
    NAMES = (("emissions", "Emissions"), ("losses", "Losses"), ("ripple", "FB ripple, loop"),
             ("thermal", "Temperatures"))

    def __init__(self, parent):
        """Four three-state check boxes in a row."""
        super().__init__(parent)
        self.boxes = {k: wx.CheckBox(self, label=label, style=wx.CHK_3STATE) for k, label in self.NAMES}
        self.mixed = False
        s = wx.BoxSizer(wx.HORIZONTAL)
        for b in self.boxes.values():
            s.Add(b, 0, wx.RIGHT, 8)
            b.Bind(wx.EVT_CHECKBOX, self._clicked)
        self.SetSizer(s)

    def _clicked(self, event):
        """A box clicked: the value is the boxes' now (no longer the several stages' mixed one)."""
        if self.mixed:
            self.mixed = False
            for b in self.boxes.values():
                if b.Get3StateValue() == wx.CHK_UNDETERMINED:
                    b.Set3StateValue(wx.CHK_UNCHECKED)
        event.Skip()

    def GetValue(self) -> str:
        """The ticked outputs, comma-separated (ConvertersPanel.MIXED while several stages differ)."""
        if self.mixed:
            return ConvertersPanel.MIXED
        return ", ".join(k for k, b in self.boxes.items() if b.Get3StateValue() == wx.CHK_CHECKED)

    def SetValue(self, text: str):
        """Tick the outputs named in text (MIXED: every box undetermined)."""
        self.mixed = text == ConvertersPanel.MIXED
        names = {x.strip() for x in text.split(",")}
        for k, b in self.boxes.items():
            b.Set3StateValue(wx.CHK_UNDETERMINED if self.mixed else wx.CHK_CHECKED if k in names
                             else wx.CHK_UNCHECKED)

    def SetHint(self, _hint: str):
        """No hint for check boxes (the text controls' interface)."""


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
        self._multi: Optional[dict] = None      # several rows selected: what the form showed (_form_for_selection)

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
        self.Bind(wx.EVT_SHOW, self._on_show)
        self.build_steps = [self._form_fields, self._form_fields_more, self._form_rows, self._form_layout]
        while self.build_steps and not in_steps:
            self.build_steps.pop(0)()

    def _form_fields(self):
        """The form's fields (the first of the build steps)."""
        # the form scrolls when the window is short; the buttons under it stay in view (before, the last rows of
        # buttons were cut off on a laptop screen)
        self.form_win = fw = wx.ScrolledWindow(self, style=wx.VSCROLL)
        fw.SetScrollRate(0, 12)
        form = wx.GridBagSizer(vgap=5, hgap=8)         # label | field rows under section titles spanning both
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
        self.dead_rise = wx.TextCtrl(fw)         # a synchronous rectifier: dead times, V_SD, Q_rr
        self.dead_rise.SetHint("20ns (assumed)")
        self.dead_fall = wx.TextCtrl(fw)
        self.dead_fall.SetHint("20ns (assumed)")
        self.v_sd = wx.TextCtrl(fw)
        self.v_sd.SetHint("GaN 2, Si 0.8")
        self.q_rr = wx.TextCtrl(fw)
        self.q_rr.SetHint("none")
        self.qg = wx.TextCtrl(fw)                # the efficiency sweep's fixed losses, its currents, a measurement
        self.qg.SetHint("none")
        self.v_gate = wx.TextCtrl(fw)
        self.v_gate.SetHint("5 (assumed)")
        self.iq = wx.TextCtrl(fw)
        self.iq.SetHint("none")
        self.core_loss = wx.TextCtrl(fw)         # the inductor's core loss: a point of the maker's data [A98]
        self.core_loss.SetHint("none")
        self.core_loss_f = wx.TextCtrl(fw)
        self.core_loss_f.SetHint("at f_sw")
        self.core_loss_di = wx.TextCtrl(fw)
        self.core_loss_di.SetHint("at this ripple")
        self.core_alpha = wx.TextCtrl(fw)
        self.core_alpha.SetHint("1.3 (assumed)")
        self.core_beta = wx.TextCtrl(fw)
        self.core_beta.SetHint("2.2 (assumed)")
        self.eff_currents = wx.TextCtrl(fw)
        self.eff_currents.SetHint("5-120 % of the load")
        self.eff_measured = wx.TextCtrl(fw)
        self.eff_measured.SetHint("none (a CSV in groundtruth/)")
        self.sweep_v_in = wx.TextCtrl(fw)       # the operating-point sweep (analysis/converter_sweep.py)
        self.sweep_v_in.SetHint("none")
        self.sweep_i_out = wx.TextCtrl(fw)
        self.sweep_i_out.SetHint("none")
        self.sweep_outputs = SweepOutputs(fw)
        self.inp = wx.TextCtrl(fw)
        self.out = wx.TextCtrl(fw)
        self.standard = wx.Choice(fw, choices=STANDARD_CHOICES)
        self.standard.SetSelection(0)
        self.std_class = wx.Choice(fw, choices=[lc.CLASS_DEFAULT])     # filled from the standard, as WEBENCH's
        self.std_class.SetSelection(0)
        self.standard.Bind(wx.EVT_CHOICE, lambda e: (e.Skip(), self._refill_classes()))
        self.spread = wx.TextCtrl(fw)
        self.spread.SetHint("off")
        self.spread_mode = wx.Choice(fw, choices=list(SPREAD_MODES))
        self.spread_mode.SetSelection(0)
        self.spread_rate = wx.TextCtrl(fw)
        self.spread_rate.SetHint(SPREAD_RATE_HINTS["random"])
        self.spread_mode.Bind(wx.EVT_CHOICE, lambda e: (e.Skip(), self._spread_hint()))
        self._spread_hint()                                 # the empty form: random, its rate greyed
        self.integrated.Bind(wx.EVT_TEXT, lambda e: (e.Skip(), self._integrated_state()))
        self.interleave = wx.TextCtrl(fw)       # interleaving (analysis/interleave.py): a group name and a phase
        self.interleave.SetHint("none")
        self.phase_deg = wx.TextCtrl(fw)
        self.phase_deg.SetHint("even spread")
        # the part parameters the schematic fields can give: a mark beside each (◆ from the schematic, ! differs)
        self.param_ctrls = {"c_oss": self.c_oss, "q_oss": self.q_oss, "rds_on": self.rds_on,
                            "rds_on_low": self.rds_on_low, "dcr": self.dcr, "diode_vf": self.diode_vf,
                            "dead_rise": self.dead_rise, "dead_fall": self.dead_fall, "v_sd": self.v_sd,
                            "q_rr": self.q_rr, "qg": self.qg}
        self.marks = {}
        self._rows = {}
        for key, ctrl in self.param_ctrls.items():
            self._rows[ctrl], self.marks[key] = with_mark(fw, ctrl, lambda k=key: self._reset_param(k))

    def form_sections(self) -> list:
        """The form's sections in order: (title, what it is for, [(label, field)]). The user: "this properties pane
        is very complex and overwhelming, can you group it better using headings etc. Make it more glancable/self
        explanatory": the fields by what they describe, the essentials first, the optional detail last."""
        return [
            ("Stage", "What it converts, between which nets.",
             [("Name", self.name), ("Topology", self.topology), ("Rail net", self.rail), ("Ground net", self.ground)]),
            ("Switches", "Discrete switches, or one IC with both inside.",
             [("High side", self.high), ("Low side", self.low), ("Switch node", self.sw),
              ("Integrated stage", self.integrated)]),
            ("Operating point", "Needed by every analysis.",
             [("Switching frequency", self.f_sw), ("Load current (A)", self.i_out), ("V in (V)", self.v_in),
              ("V out (V)", self.v_out)]),
            ("Connectors", "Where its DC current enters and leaves the board.",
             [("Input connector", self.inp), ("Output connector", self.out)]),
            ("Switching edges", "Spectrum, ringing, switching loss. ◆ from the schematic",
             [("Edge time", self.rise), ("C_oss", self.c_oss), ("Q_oss", self.q_oss),
              ("C stray (chassis)", self.c_stray)]),
            ("Conducted emission", "The limit it is checked against; spread spectrum.",
             [("Conducted standard", self.standard), ("Conducted class", self.std_class),
              ("Spread spectrum (%)", self.spread), ("Spread mode", self.spread_mode),
              ("Spread rate (Hz)", self.spread_rate)]),
            ("Interleaving", "Optional: stages switching out of phase.",
             [("Interleave group", self.interleave), ("Phase (°)", self.phase_deg)]),
            ("Losses", "Empty: left out (the run says so). ◆ schematic, ◇ Parts tab, ! differs (click: reset)",
             [("R_DS(on)", self.rds_on), ("R_DS(on) low side", self.rds_on_low), ("Inductor DCR", self.dcr),
              ("Diode V_f (V)", self.diode_vf), ("Dead time, rise", self.dead_rise),
              ("Dead time, fall", self.dead_fall), ("Reverse V_SD (V)", self.v_sd), ("Q_rr", self.q_rr),
              ("Core loss", self.core_loss), ("Core loss at f", self.core_loss_f),
              ("Core loss at ΔI (A)", self.core_loss_di), ("Core α (frequency)", self.core_alpha),
              ("Core β (ripple)", self.core_beta)]),
            ("Efficiency", "The curve over load current: fixed losses, bench data.",
             [("Q_g", self.qg), ("Gate drive (V)", self.v_gate), ("Controller I_q (A)", self.iq),
              ("Efficiency currents (A)", self.eff_currents), ("Measured efficiency", self.eff_measured)]),
            ("Sweep", "Optional: other operating points, each list at the other's nominal value.",
             [("Sweep V in (V)", self.sweep_v_in), ("Sweep I out (A)", self.sweep_i_out),
              ("Sweep outputs", self.sweep_outputs)]),
        ]

    def _form_rows(self):
        """Each section's title and note (spanning both columns) and its fields' labels and tooltips, in the form's
        grid (the third build step)."""
        section_rows(self.form_win, self._form, self.form_sections(), FIELD_TIPS, self._rows)

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
        left.Add(self.list_head(self.hint), 0, wx.EXPAND | wx.BOTTOM, 6)
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
        """A row was selected: fill the form with its stage and the marks beside the part parameters; once the
        selection has settled, several rows show what they share (_form_for_selection)."""
        self._selection_changed()
        wx.CallAfter(lambda: self and self._form_for_selection())
        self._fill_one(self.converters[evt.GetIndex()])

    # Several stages selected: the form shows the fields they can share, a value they differ in as MIXED, and
    # Update selected writes only the fields the user changed into each of them (the user: "like under signals, I
    # want to be able to select one and more and bulk change options. If parameters match show it if they mismatch
    # shown an * and ignore unless the user puts in a proper new value"; nets_panel does the same for signal paths).
    MIXED = "*"
    OWN = ("name", "switch_node", "high_side", "low_side", "switch")      # a stage's own: greyed for several
    # how a shared field is read back: required / optional numbers with their unit, nets, pin lists, choices, text
    KINDS = {"f_sw": ("num!", "Hz"), "i_out": ("num!", "A"), "rise_time": ("num!", "s"), "v_in": ("num", "V"),
             "v_out": ("num", "V"), "c_oss": ("num", "F"), "q_oss": ("num", "C"), "c_stray": ("num", "F"),
             "rds_on": ("num", "Ω"), "rds_on_low": ("num", "Ω"), "dcr": ("num", "Ω"), "diode_vf": ("num", "V"),
             "dead_rise": ("num", "s"), "dead_fall": ("num", "s"), "v_sd": ("num", "V"), "q_rr": ("num", "C"),
             "spread_rate": ("num", "Hz"), "phase_deg": ("num", "°"), "rail": ("net", ""), "ground": ("net", ""),
             "input_connector": ("pins", ""), "output_connector": ("pins", ""), "topology": ("choice", ""),
             "conducted": ("choice", ""), "conducted_class": ("choice", ""), "spread_mode": ("choice", ""),
             "spread": ("pct", ""),
             "interleave": ("text", ""), "qg": ("num", "C"), "v_gate": ("num", "V"), "iq": ("num", "A"),
             "core_loss": ("num", "W"), "core_loss_f": ("num", "Hz"), "core_loss_di": ("num", "A"),
             "core_alpha": ("num", ""), "core_beta": ("num", ""),
             "efficiency_currents": ("nums", "A"), "efficiency_measured": ("text", ""),
             "sweep_v_in": ("nums", "V"), "sweep_i_out": ("nums", "A"), "sweep_outputs": ("outputs", "")}

    def _ctrls(self) -> dict:
        """ConverterSpec field -> its control on the form."""
        return {"name": self.name, "topology": self.topology, "rail": self.rail, "switch_node": self.sw,
                "ground": self.ground, "high_side": self.high, "low_side": self.low, "switch": self.integrated,
                "f_sw": self.f_sw, "i_out": self.i_out, "v_in": self.v_in, "v_out": self.v_out,
                "rise_time": self.rise, "c_oss": self.c_oss, "q_oss": self.q_oss, "c_stray": self.c_stray,
                "rds_on": self.rds_on, "rds_on_low": self.rds_on_low, "dcr": self.dcr, "diode_vf": self.diode_vf,
                "dead_rise": self.dead_rise, "dead_fall": self.dead_fall, "v_sd": self.v_sd, "q_rr": self.q_rr,
                "input_connector": self.inp, "output_connector": self.out, "conducted": self.standard,
                "conducted_class": self.std_class, "spread": self.spread, "spread_mode": self.spread_mode, "spread_rate": self.spread_rate,
                "interleave": self.interleave, "phase_deg": self.phase_deg, "qg": self.qg, "v_gate": self.v_gate,
                "core_loss": self.core_loss, "core_loss_f": self.core_loss_f, "core_loss_di": self.core_loss_di,
                "core_alpha": self.core_alpha, "core_beta": self.core_beta,
                "iq": self.iq, "efficiency_currents": self.eff_currents, "efficiency_measured": self.eff_measured,
                "sweep_v_in": self.sweep_v_in, "sweep_i_out": self.sweep_i_out, "sweep_outputs": self.sweep_outputs}

    def _texts(self, c: ConverterSpec) -> dict:
        """Every field of stage c as the form shows it."""
        def si(v, unit):
            """A value with its SI prefix and unit, "" for none."""
            return "" if v is None else format_si(v, unit)

        def g(v):
            """A plain number, "" for none."""
            return "" if v is None else f"{v:g}"
        return {"name": c.name, "topology": c.topology, "rail": c.rail, "switch_node": c.switch_node or "",
                "ground": c.ground, "high_side": ", ".join(c.high_side), "low_side": ", ".join(c.low_side),
                "switch": ", ".join(c.switch), "f_sw": format_si(c.f_sw, "Hz"), "i_out": f"{c.i_out:g}",
                "v_in": g(c.v_in), "v_out": g(c.v_out), "rise_time": format_si(c.rise_time, "s"),
                "c_oss": si(c.c_oss, "F"), "q_oss": si(c.q_oss, "C"), "c_stray": si(c.c_stray, "F"),
                "rds_on": si(c.rds_on, "Ω"), "rds_on_low": si(c.rds_on_low, "Ω"), "dcr": si(c.dcr, "Ω"),
                "diode_vf": g(c.diode_vf), "dead_rise": si(c.dead_rise, "s"), "dead_fall": si(c.dead_fall, "s"),
                "v_sd": g(c.v_sd), "q_rr": si(c.q_rr, "C"), "input_connector": ", ".join(c.input_connector),
                "output_connector": ", ".join(c.output_connector),
                "conducted": lc.standard_text(c.conducted),
                "conducted_class": lc.class_text(c.conducted or lc.analysis_of(self.settings).conducted,
                                                 c.conducted_class),
                "spread": "" if not c.spread else f"{c.spread * 100:g}",
                "spread_mode": c.spread_mode if c.spread_mode in SPREAD_MODES else SPREAD_MODES[0],
                "spread_rate": si(c.spread_rate, "Hz"), "interleave": c.interleave, "phase_deg": g(c.phase_deg),
                "qg": si(c.qg, "C"), "v_gate": g(c.v_gate), "iq": si(c.iq, "A"),
                "core_loss": si(c.core_loss, "W"), "core_loss_f": si(c.core_loss_f, "Hz"),
                "core_loss_di": g(c.core_loss_di), "core_alpha": g(c.core_alpha), "core_beta": g(c.core_beta),
                "efficiency_currents": ", ".join(f"{x:g}" for x in c.efficiency_currents),
                "efficiency_measured": c.efficiency_measured,
                "sweep_v_in": ", ".join(f"{x:g}" for x in c.sweep_v_in),
                "sweep_i_out": ", ".join(f"{x:g}" for x in c.sweep_i_out),
                "sweep_outputs": ", ".join(c.sweep_outputs or DEFAULT_OUTPUTS)}

    def _set(self, ctrl, text: str):
        """Show text in a control; a list (wx.Choice) gets a MIXED entry at the top while it shows one."""
        if ctrl is self.std_class:       # the classes of the standard shown (set before it: _ctrls' order)
            self._refill_classes(keep=False)
        if isinstance(ctrl, wx.Choice):
            has = ctrl.GetCount() and ctrl.GetString(0) == self.MIXED
            if text == self.MIXED:
                if not has:
                    ctrl.Insert(self.MIXED, 0)
                ctrl.SetSelection(0)
                return
            if has:
                ctrl.Delete(0)
            if not ctrl.SetStringSelection(text):
                ctrl.SetSelection(0)
            return
        ctrl.SetValue(text)

    @staticmethod
    def _text(ctrl) -> str:
        """What a control shows now (a list's chosen entry)."""
        return ctrl.GetStringSelection() if isinstance(ctrl, wx.Choice) else ctrl.GetValue().strip()

    def _fill_one(self, c: ConverterSpec):
        """The form shows one stage (its own fields enabled again)."""
        self._multi = None
        ctrls = self._ctrls()
        for key in self.OWN:
            ctrls[key].Enable(True)
        for key, text in self._texts(c).items():
            self._set(ctrls[key], text)
        self._spread_hint()
        self._integrated_state()
        self._show_marks(c)

    def _form_for_selection(self):
        """After the selection settled: several rows show their shared fields (a value they differ in as MIXED) and
        their own fields greyed, without the part parameters' marks; back to one row, it shows itself."""
        rows = self.selected_rows()
        if len(rows) == 1 and getattr(self, "_multi", None) is not None:
            self._fill_one(self.converters[rows[0]])
        if len(rows) < 2:
            return
        texts = [self._texts(self.converters[i]) for i in rows if i < len(self.converters)]
        ctrls, shown = self._ctrls(), {}
        for key in self.KINDS:
            values = {t[key] for t in texts}
            shown[key] = values.pop() if len(values) == 1 else self.MIXED
            self._set(ctrls[key], shown[key])
        for key in self.OWN:
            ctrls[key].Enable(False)
        self._spread_hint()
        self._show_marks(None)
        self._multi = shown

    def _parse(self, key: str, text: str):
        """(a shared field's value read from its text, problem or "")."""
        kind, unit = self.KINDS[key]
        if kind in ("num", "num!"):
            if not text:
                return (None, "") if kind == "num" else (None, "needs a number")
            v = parse_field(text, unit)
            if v is None or (kind == "num!" and not v > 0):
                return None, f"'{text}' is not a {'positive ' if kind == 'num!' else ''}number"
            return v, ""
        if kind == "net":
            return (text, "") if self.board is None or text in self.board.nets else (None, f"'{text}' is no net")
        if kind == "nums":                       # comma-separated numbers (the efficiency sweep's currents)
            vals = [parse_field(x.strip(), unit) for x in text.split(",") if x.strip()]
            if any(v is None or not v > 0 for v in vals):
                return None, f"'{text}' is not a list of positive numbers"
            return vals, ""
        if kind == "pins":
            return [r.strip() for r in text.split(",") if r.strip()], ""
        if kind == "outputs":                    # the sweep's outputs (SweepOutputs)
            return [r.strip() for r in text.split(",") if r.strip()], ""
        if kind == "pct":
            if not text:
                return None, ""
            v = parse_field(text.replace("%", ""), "")
            return (v / 100 or None, "") if v is not None else (None, f"'{text}' is not a percentage")
        if key == "conducted":
            return lc.standard_key(text), ""
        if key == "conducted_class":
            return lc.class_key(self._class_standard(), text), ""
        return text, ""

    def _update_many(self, rows: list):
        """Update selected with several rows: every shared field the user changed (not MIXED and not what was shown)
        goes into each selected stage, linked to the schematic where it equals its field; the rest of each stage
        stays its own."""
        from ..components.fields import link_form
        changes, problems = {}, []
        ctrls = self._ctrls()
        for key in self.KINDS:
            text = self._text(ctrls[key])
            if text == self.MIXED or text == self._multi.get(key):
                continue
            value, problem = self._parse(key, text)
            if problem:
                problems.append(f"{key.replace('_', ' ')}: {problem} (or * to leave each stage's own)")
            else:
                changes[key] = value
        if problems:
            message_box("\n".join(problems), "GroundTruth", wx.ICON_WARNING)
            return
        if not changes:
            top = self.GetTopLevelParent()
            if hasattr(top, "SetStatusText"):
                top.SetStatusText(f"nothing changed in the form for the {len(rows)} selected power stages")
            return
        for i in rows:
            old = self.converters[i]
            new = replace(old, reviewed=True, **changes)
            link_form(self.board, self.settings.config, new, old)
            self.converters[i] = new
        if self._commit():
            self.reselect(rows)          # the list was rebuilt: the stages stay selected for the next edit
            self._form_for_selection()

    def _integrated_state(self):
        """High side, Low side and Switch node greyed while Integrated stage names an IC (the user: "when entering
        Integrated stage = U46, should you grey out the High side, Low side and Switch node then"): both switches
        are inside it, so those three are not used and _form saves none of them; their text stays, for a stage
        turned back into discrete switches. Several stages selected keep all four greyed (OWN)."""
        if getattr(self, "_multi", None) is not None:
            return
        used = not self.integrated.GetValue().strip()
        for ctrl in (self.high, self.low, self.sw):
            ctrl.Enable(used)
        hint = "" if used else "not used: integrated stage"
        self.high.SetHint(hint)
        self.low.SetHint(hint)

    def _spread_hint(self):
        """The Spread rate field for the mode shown: its hint (SPREAD_RATE_HINTS; several stages with mixed modes:
        none) and greyed for random (the user: "if random is set, it should grey out the other field"), which then
        hops every switching cycle; its text stays, for a switch back to triangle (_form saves no rate for random)."""
        mode = self.spread_mode.GetStringSelection()
        self.spread_rate.SetHint(SPREAD_RATE_HINTS.get(mode, ""))
        self.spread_rate.Enable(mode != "random")

    def _class_standard(self) -> str:
        """The standard the class choice belongs to: the one chosen, else the analysis'."""
        return lc.effective_standard(self.standard.GetStringSelection(), lc.analysis_of(self.settings))

    def _refill_classes(self, keep: bool = True):
        """The class choice filled with the chosen standard's classes (keep: the class shown, if it has it), and the
        standard's tooltip saying what the analysis default is."""
        shown = self.std_class.GetStringSelection() if keep else None
        lc.refill_classes(self.std_class, self._class_standard(), shown)
        self.standard.SetToolTip(f"{FIELD_TIPS['Conducted standard']}\n"
                                 f"{lc.default_tip(lc.analysis_of(self.settings))}")

    def _show_marks(self, c: Optional[ConverterSpec]):
        """The part parameters' marks for stage c (◆ from the schematic, ◇ as on the Parts tab, ! set here and
        different; none for None)."""
        from ..components.fields import stage_marks
        marks = stage_marks(self.board, self.settings.config, c) if c is not None and self.board else {}
        for key, mark in self.marks.items():
            mark.show(*marks.get(key, ("", "")))

    def _on_show(self, evt):
        """The tab is shown again: the selected stage's marks again (an inductor's R edited on the Parts tab
        meanwhile changes its DCR's ◇ / !)."""
        evt.Skip()
        rows = self.selected_rows()
        if evt.IsShown() and getattr(self, "marks", None) and len(rows) == 1 and rows[0] < len(self.converters):
            self._show_marks(self.converters[rows[0]])

    def _reset_param(self, key: str):
        """A part parameter's mark was clicked (or its field's menu used): the selected stage takes that one value
        back from its source (the schematic's field, linked again; or a DCR from its inductor's R on the Parts tab)
        and is saved. The form's other fields keep what is typed in them (not yet Updated)."""
        from ..components.fields import stage_marks
        rows = self.selected_rows()
        if len(rows) != 1 or self.board is None or rows[0] >= len(self.converters):
            return
        i = rows[0]
        old = self.converters[i]
        mark = stage_marks(self.board, self.settings.config, old).get(key)
        if mark is None or mark.value is None:
            return
        typed = form_state(self.form_win)
        new = replace(old, from_schematic=[k for k in old.from_schematic if k != key])
        setattr(new, key, mark.value)
        if mark.origin == "schematic":
            new.from_schematic.append(key)
        self.converters[i] = new
        if not self._commit():
            return
        self.reselect([i])

        def back():
            """After the selection's refill: the typed fields back, this one with its new value."""
            if not self:
                return
            ctrl = self.param_ctrls[key]
            restore_form(typed, skip=(ctrl,))
            ctrl.ChangeValue(self._texts(new)[key])
            self._spread_hint()
            self._integrated_state()
            self._show_marks(new)
        wx.CallAfter(back)

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
        cls = lc.class_key(self._class_standard(), self.std_class.GetStringSelection())
        integrated = refs(self.integrated)
        spec = ConverterSpec(
            name=self.name.GetValue().strip() or self.sw.GetValue() or "converter", rail=self.rail.GetValue(),
            ground=self.ground.GetValue(), switch_node=None if integrated else (self.sw.GetValue() or None),
            # an integrated stage: its greyed discrete fields are not used (_integrated_state)
            high_side=[] if integrated else refs(self.high), low_side=[] if integrated else refs(self.low),
            switch=integrated, topology=self.topology.GetStringSelection(),
            f_sw=num(self.f_sw, "Hz") or 0.0, i_out=num(self.i_out, "A") or 0.0, v_in=num(self.v_in, "V"),
            v_out=num(self.v_out, "V"), rise_time=num(self.rise, "s") or 0.0, c_oss=num(self.c_oss, "F"),
            q_oss=num(self.q_oss, "C"), c_stray=num(self.c_stray, "F"), rds_on=num(self.rds_on, "Ω"),
            rds_on_low=num(self.rds_on_low, "Ω"), dcr=num(self.dcr, "Ω"), diode_vf=num(self.diode_vf, "V"),
            dead_rise=num(self.dead_rise, "s"), dead_fall=num(self.dead_fall, "s"), v_sd=num(self.v_sd, "V"),
            q_rr=num(self.q_rr, "C"), qg=num(self.qg, "C"), v_gate=num(self.v_gate, "V"), iq=num(self.iq, "A"),
            core_loss=num(self.core_loss, "W"), core_loss_f=num(self.core_loss_f, "Hz"),
            core_loss_di=num(self.core_loss_di, "A"), core_alpha=num(self.core_alpha, ""),
            core_beta=num(self.core_beta, ""),
            efficiency_currents=[v for v in (parse_field(x.strip(), "A") for x in self.eff_currents.GetValue()
                                             .split(",") if x.strip()) if v],
            efficiency_measured=self.eff_measured.GetValue().strip(),
            sweep_v_in=[v for v in (parse_field(x.strip(), "V") for x in self.sweep_v_in.GetValue().split(",")
                                    if x.strip()) if v],
            sweep_i_out=[v for v in (parse_field(x.strip(), "A") for x in self.sweep_i_out.GetValue().split(",")
                                     if x.strip()) if v],
            sweep_outputs=[x.strip() for x in self.sweep_outputs.GetValue().split(",") if x.strip()
                           and x.strip() != self.MIXED],
            input_connector=refs(self.inp), output_connector=refs(self.out),
            conducted=lc.standard_key(std), conducted_class=cls,
            spread=(parse_field(self.spread.GetValue().replace("%", ""), "") or 0.0) / 100 or None,
            spread_mode=self.spread_mode.GetStringSelection(),
            # random hops every cycle: its greyed rate field is not used (_spread_hint)
            spread_rate=None if self.spread_mode.GetStringSelection() == "random" else num(self.spread_rate, "Hz"),
            interleave=self.interleave.GetValue().strip(), phase_deg=num(self.phase_deg, "°"))
        from ..config.settings import validate
        from copy import copy
        probe = copy(self.settings.config)
        probe.nets, probe.converters = [], [spec]
        problems = validate(probe, self.board)
        if not (spec.f_sw and spec.i_out and spec.rise_time):
            problems.append("switching frequency, load current and edge time must be numbers (e.g. 400kHz, 3, 5ns)")
        for ctrl, what in ((self.spread, "spread spectrum (a percentage, e.g. 20)"),
                           (self.spread_rate, "spread rate (a frequency, e.g. 10kHz)"),
                           (self.phase_deg, "phase (degrees of the switching period, e.g. 180)")):
            text = ctrl.GetValue().replace("%", "").strip()
            if text and parse_field(text, "Hz") is None:
                problems.append(f"{what}: '{ctrl.GetValue()}' is not a number")
        if problems:
            message_box("\n".join(problems), "groundtruth", wx.ICON_WARNING)
            return None
        return spec

    def _commit(self) -> bool:
        """Write the list to the settings and show it again; False (the list restored from the settings) when the
        settings refuse it."""
        try:
            self.settings.set_converters(self.converters)
        except ConfigError as e:
            message_box("\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
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
            if getattr(self, "_multi", None) is not None:
                self._update_many(rows)
            return
        i = rows[0] if rows else -1
        spec = self._form() if rows else None
        if i >= 0 and spec:
            from ..components.fields import link_form
            old = self.converters[i]
            # what the form does not show stays as it was (the spread spectrum is on the form since 2026-10-17)
            new = replace(spec, source=old.source, reviewed=True, enabled=old.enabled, r_damp=old.r_damp,
                          duty=old.duty, note=old.note, rds_on_tc=old.rds_on_tc,
                          snubber=list(old.snubber), snubber_c=old.snubber_c, snubber_r=old.snubber_r,
                          loop=dict(old.loop))
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
            message_box("The selected stages' parts have no schematic fields for R_DS(on), C_oss, Q_oss, DCR or V_f "
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
            message_box("No power stages found (an inductor next to a switching transistor, a catch diode or an "
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
