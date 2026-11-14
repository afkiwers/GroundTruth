"""Power integrity tab: supply rails whose PDN impedance Simulate checks (analysis/pdn.py; the plan's M1).

A rail is its net, its ground, the allowed ripple and the load step (they give the target impedance), and what
feeds it: a power stage of the Converters tab in closed loop (found by itself when the rail is its output; the user:
"Why are converters not listed under Power integrity"), else a part (a regulator's power_out pin or a connector) with
its output resistance and inductance. It is read at its ICs' supply pins, or at ports (a connector the load hangs
on). Simulate gives each port's impedance curve against the target and the rail's voltage there over time when the
load steps (Results tab: the curves, what holds the rail at each frequency, the step), and issues where it is over
the target or the ripple, or a supply pin has no decap close by.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

import wx
from .messages import box as message_box   # laid-out message boxes (ui/messages.py)

from ..components.values import format_si
from ..config.settings import ConfigError, LoadStep, PdnSpec
from .listsel import SelectableRows
from .widgets import button_row, fit_scroll_width, scroll_form, section_form, section_rows

AUTO_SOURCE = "found by itself"
NO_SOURCE = "none"
FIELD_TIPS = {
    "Rail": "The supply net to check, e.g. +3V3 (the list: nets that ICs draw supply from, the busiest first).",
    "Ground": "Its ground (default: the first reference net, e.g. GND).",
    "Voltage (V)": "The rail's voltage; empty: from its name (+3V3: 3.3 V, VDD_1V8: 1.8 V).",
    "Ripple (%)": "The voltage ripple allowed, in % of the voltage (default 5 %).",
    "Load step (A)": "The current step the ICs draw (e.g. 0.3). With it the target impedance is voltage x ripple / "
                     "step; empty: the curves without a target.",
    "Source": "What feeds the rail: found by itself (a power stage of the Converters tab whose output it is, in closed "
              "loop; else a regulator's power_out pin on it, else a connector), none, a stage (stage:NAME), or a "
              "part with a pad on the rail.",
    "Source R (Ω)": "The source's output resistance (a regulator's flat output impedance), default 5m.",
    "Source L (H)": "Its output inductance above its loop bandwidth, default 20n (an LDO or a buck: tens of nH).",
    "ICs": "The ICs to check, e.g. U1, U2; empty: every IC with supply pins on the rail.",
    "Ports": "Pin pairs to read the rail at besides the ICs, supply / return, e.g. J2.2/J2.1: a connector the load "
             "hangs on (a converter's output to an off-board load). Comma-separated.",
    "Step rise": "How fast the load current ramps, e.g. 1us. Empty: an ideal step (the copper above Check up to is "
                 "left to the IC's package and die).",
    "Load before (A)": "The current the load draws before the step (a resistor in the model). Empty: none.",
    "Harness R": "Leads from the port to the load (both conductors), e.g. 20mΩ. Empty: the load at the port.",
    "Harness L": "Their inductance, e.g. 500nH (about 0.5-1 µH per metre).",
    "Load C": "Capacitance at the load (a downstream board's input capacitors), e.g. 100uF.",
    "Load C ESR": "Its ESR, e.g. 10mΩ.",
    "Coil R": "A load switched on instead of the step: its resistance (a heater coil). With Coil L: R + sL.",
    "Coil L": "The switched load's inductance, e.g. 1mH.",
    "Check up to (Hz)": "The target applies up to here (default 100M: above, the package and the die decouple).",
    "Note": "Your own note (kept in the settings file).",
}
BUTTON_TIPS = {
    "Power tree…": "A flow chart of the board's power: sources, links, converters and regulators, rails, their "
                       "loads and capacitors (Fit, Refresh, Copy, Save image in its window).",
    "Find rails": "add every rail ICs draw supply from and every power stage's output that is not listed yet (its "
                  "source found by itself: the stage in closed loop); then set each one's load step for a target",
    "Add": "add the form's rail to the list (and the settings file)",
    "Update selected": "replace the selected rail with the form's values",
    "Remove selected": "remove the selected rails (Delete does the same)",
}


def _set_items(combo, items: list):
    """A combo box's list, set only when it differs (a native Set takes 3-10 ms on Windows)."""
    if list(combo.GetStrings()) != list(items):
        combo.Set(items)


class PdnPanel(SelectableRows, wx.Panel):
    """The Power integrity tab: the rails (a list on the left, each with its on/off tick) and a form on the right.
    Every change is written to the settings at once."""

    def __init__(self, parent, on_change, in_steps: bool = False):
        """on_change(): the settings changed (the window saves them and marks results as stale). in_steps: the form
        is built later, in two `build_steps` (MainFrame._build_pages runs them one per event: the tab in one step
        blocked the window 128-191 ms, tools/dev/build_profile.py)."""
        super().__init__(parent)
        self.on_change = on_change
        self.board = None
        self.settings = None
        self.pdn: list[PdnSpec] = []
        self.rails: dict[str, list[str]] = {}
        self.hint = wx.StaticText(self, label="No rails yet: press Find rails (on the right) to list the supply rails "
                                              "the ICs draw from, then set each one's load step.")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        for i, (c, w) in enumerate([("Rail", 120), ("Voltage", 65), ("Ripple", 55), ("Load step", 70),
                                    ("Target", 70), ("Source", 110), ("ICs", 120)]):
            self.list.InsertColumn(i, c, width=w)
        self.list.SetMinSize(wx.Size(320, 120))
        self._init_rows(None, "rails")
        self._init_enable("pdn")
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.build_steps = [self._build_form, self._build_layout]
        while self.build_steps and not in_steps:
            self.build_steps.pop(0)()

    def _build_form(self):
        """The rail's form (the first build step)."""
        fw = self._fw = scroll_form(self)       # scrolls when the tab is short: the buttons stay in reach
        form = section_form()
        self.rail = wx.ComboBox(fw)
        self.ground = wx.ComboBox(fw)
        self.voltage = wx.TextCtrl(fw)
        self.ripple = wx.TextCtrl(fw)
        self.step = wx.TextCtrl(fw)
        self.source = wx.ComboBox(fw)
        self.vrm_r = wx.TextCtrl(fw)
        self.vrm_l = wx.TextCtrl(fw)
        self.parts = wx.TextCtrl(fw)
        self.ports = wx.TextCtrl(fw)
        self.check_to = wx.TextCtrl(fw)
        self.note = wx.TextCtrl(fw)
        # the step over time (analysis/load_step.sampled_step): its ramp and the load network at the port
        self.ls = {key: wx.TextCtrl(fw) for key in ("rise", "i_from", "cable_r", "cable_l", "c_load", "esr_load",
                                                     "r_load", "l_load")}
        for key, hint in (("rise", "ideal step"), ("i_from", "none"), ("cable_r", "none"), ("cable_l", "none"),
                          ("c_load", "none"), ("esr_load", "0"), ("r_load", "none (the step)"), ("l_load", "none")):
            self.ls[key].SetHint(hint)
        for ctrl, hint in ((self.voltage, "from the name"), (self.ripple, "5"), (self.step, "e.g. 0.3 (for a target)"),
                           (self.vrm_r, "5m"), (self.vrm_l, "20n"), (self.parts, "every IC on the rail"),
                           (self.ports, "none (e.g. J2.2/J2.1)"), (self.check_to, "100M")):
            ctrl.SetHint(hint)
        self.rail.Bind(wx.EVT_TEXT, self._rail_changed)
        section_rows(fw, form, [            # sections as on the Converters tab (widgets.section_rows)
            ("Supply", "The rail and its return.",
             [("Rail", self.rail), ("Ground", self.ground), ("Voltage (V)", self.voltage), ("Note", self.note)]),
            ("Target", "The impedance the rail must stay below: its allowed ripple at a load step.",
             [("Ripple (%)", self.ripple), ("Load step (A)", self.step), ("Check up to (Hz)", self.check_to)]),
            ("Source", "What feeds the rail: a power stage in closed loop, or a part (R and L: a part's only).",
             [("Source", self.source), ("Source R (Ω)", self.vrm_r), ("Source L (H)", self.vrm_l)]),
            ("Read at", "Where the impedance is read. Empty: every IC on the rail.",
             [("ICs", self.parts), ("Ports", self.ports)]),
            ("Load step", "The rail over time when the load steps by Load step (A). Empty: an ideal step at the port.",
             [("Step rise", self.ls["rise"]), ("Load before (A)", self.ls["i_from"]),
              ("Harness R", self.ls["cable_r"]), ("Harness L", self.ls["cable_l"]), ("Load C", self.ls["c_load"]),
              ("Load C ESR", self.ls["esr_load"]), ("Coil R", self.ls["r_load"]), ("Coil L", self.ls["l_load"])])],
            FIELD_TIPS)
        inner = wx.BoxSizer(wx.VERTICAL)
        inner.Add(form, 0, wx.EXPAND | wx.RIGHT, 12)        # room for the scroll bar
        fw.SetSizer(inner)
        fit_scroll_width(fw)

    def _build_layout(self):
        """The buttons, the note and the tab's layout (the last build step)."""
        fw = self._fw
        del self._fw
        btns = button_row(self, [("Power tree…", self.on_power_tree), ("Find rails", self.on_find),
                                 ("Add", self.on_add),
                                 ("Update selected", self.on_update), ("Remove selected", self.on_remove)], BUTTON_TIPS)
        about = wx.StaticText(self, label="Simulate solves each rail on the whole board: the impedance every IC (and "
                                          "port) sees from 10 kHz to 1 GHz (from below a power stage's loop "
                                          "crossover when one feeds it) against the target, and the rail's voltage "
                                          "there when the load steps (Results tab: the curves, what holds the rail "
                                          "at each frequency, the step).")
        about.SetName("muted")
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(fw, 1, wx.EXPAND)                 # the height left, scrolling within it
        right.Add(btns, 0, wx.TOP | wx.EXPAND, 8)   # always below it, in reach
        right.Add(about, 0, wx.TOP | wx.EXPAND, 8)
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(self.list_head(self.hint), 0, wx.EXPAND | wx.BOTTOM, 6)
        left.Add(self.list, 1, wx.EXPAND)
        s = wx.BoxSizer(wx.HORIZONTAL)
        s.Add(left, 3, wx.EXPAND | wx.ALL, 6)
        s.Add(right, 2, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(s)
        self.hint.Hide()
        self.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), about.Wrap(max(150, self.GetClientSize().width * 2 // 5 - 24))))

    def _ground(self) -> str:
        """The default ground: the first reference net."""
        refs = self.settings.config.reference_nets if self.settings else []
        return refs[0] if refs else "GND"

    def load(self, board, settings):
        """Show this board's rails; the form's lists: the rails ICs draw from, the board's nets as grounds."""
        from ..analysis.pdn import converter_rails, rails_with_ics
        self.board, self.settings = board, settings
        self.rails = rails_with_ics(board, self._ground()) if board is not None else {}
        # the power stages' outputs (Converters tab): rails too, ICs on them or not
        self.conv_rails = converter_rails(board, settings.config) if board is not None else {}
        for net in self.conv_rails:
            self.rails.setdefault(net, [])
        # filling the lists fires the rail's text event: _rail_changed then ran once more (scanning the parts and
        # setting the Source list again): it runs once, at the end (the load stalled the window 211 ms on
        # refboard with the module's import, 45 ms without it)
        self._filling = True
        try:
            _set_items(self.rail, list(self.rails))
            # ground-like nets and the reference nets only: every net of a big board (hundreds) in the list blocked
            # the window 270 ms at load (gui_probe on refboard); another one can still be typed
            from .pin_complete import is_ground
            refs = list(settings.config.reference_nets)
            _set_items(self.ground, refs + sorted(n for n in (board.nets if board is not None else ())
                                                  if is_ground(n) and n not in refs))
            self.ground.SetValue(self._ground())
        finally:
            self._filling = False
        self._rail_changed()
        self.pdn = list(settings.config.pdn)
        self.refresh()

    def _rail_changed(self, _=None):
        """The rail changed: the parts on it as Source choices."""
        if _ is not None:
            _.Skip()
        if getattr(self, "_filling", False):
            return
        rail = self.rail.GetValue().strip()
        by_net = self.board.parts_by_net() if self.board is not None else {}
        parts = sorted({fp.reference for fp in by_net.get(rail, ())
                        if not fp.reference.upper().startswith(("C", "R"))})
        stages = [f"stage:{c.name}" for c in (self.settings.config.converters if self.settings else [])
                  if c.enabled and not c.bridge]
        keep = self.source.GetValue()
        _set_items(self.source, [AUTO_SOURCE, NO_SOURCE] + stages + parts)
        self.source.SetValue(keep or AUTO_SOURCE)

    def refresh(self):
        """Rebuild the list from self.pdn (switched-off rows greyed)."""
        self.hint.Show(self.board is not None and not self.pdn)
        self.Layout()
        self.list.DeleteAllItems()
        for d in self.pdn:
            v = d.voltage
            if v is None:
                from ..analysis.pdn import rail_voltage
                v = rail_voltage(d.rail)
            target = v * d.ripple / d.i_step if v and d.i_step else None
            # a source found by itself: the power stage whose output the rail is, named
            stage = getattr(self, "conv_rails", {}).get(d.rail)
            source = d.source or (f"found: {stage[0]}" if stage else AUTO_SOURCE)
            i = self.list.InsertItem(self.list.GetItemCount(), d.label)
            for col, text in enumerate([format_si(v, "V") if v else "?", f"{d.ripple * 100:g} %",
                                        format_si(d.i_step, "A") if d.i_step else "–",
                                        format_si(target, "Ω") if target else "–", source,
                                        ", ".join(d.parts + d.ports) if d.parts or d.ports else
                                        f"all ({len(self.rails.get(d.rail, []))})"], 1):
                self.list.SetItem(i, col, text)
            self._show_enabled(i, d)

    def on_select(self, evt):
        """A row was selected: fill the form with its rail."""
        self._selection_changed()
        d = self.pdn[evt.GetIndex()]
        self.rail.SetValue(d.rail)
        self.ground.SetValue(d.ground or self._ground())
        self.voltage.SetValue("" if d.voltage is None else f"{d.voltage:g}")
        self.ripple.SetValue(f"{d.ripple * 100:g}")
        self.step.SetValue("" if d.i_step is None else format_si(d.i_step, ""))
        self.source.SetValue(d.source or AUTO_SOURCE)
        self.vrm_r.SetValue(format_si(d.vrm_r, ""))
        self.vrm_l.SetValue(format_si(d.vrm_l, ""))
        self.parts.SetValue(", ".join(d.parts))
        self.ports.SetValue(", ".join(d.ports))
        self.check_to.SetValue(format_si(d.check_to, ""))
        self.note.SetValue(d.note)
        ls = d.load_step
        for key, ctrl in self.ls.items():
            v = getattr(ls, key) if ls is not None else None
            ctrl.SetValue("" if v is None or (v == 0 and key not in ("i_from", "r_load")) else format_si(v, ""))

    def _form(self) -> Optional[PdnSpec]:
        """The form as a PdnSpec, or None after telling the user what is wrong (the settings' own parsing and the
        board's nets and parts check it)."""
        from ..config.settings import _pdn_from_data, pdn_problems, validate
        rail = self.rail.GetValue().strip()
        if not rail:
            message_box("Choose the rail (its net), or press Find rails.", "GroundTruth", wx.ICON_INFORMATION)
            return None
        src = self.source.GetValue().strip()
        data = {"rail": rail, "ground": self.ground.GetValue().strip() if self.ground.GetValue().strip() !=
                self._ground() else "",
                "voltage": self.voltage.GetValue().strip() or None, "ripple": (self.ripple.GetValue().strip() or "5")
                + "%", "i_step": self.step.GetValue().strip() or None,
                "source": "" if src in ("", AUTO_SOURCE) else src,
                "vrm_r": self.vrm_r.GetValue().strip() or "5m", "vrm_l": self.vrm_l.GetValue().strip() or "20n",
                "parts": self.parts.GetValue().strip(), "check_to": self.check_to.GetValue().strip() or "100M",
                "note": self.note.GetValue().strip(),
                "ports": [p.strip() for p in self.ports.GetValue().split(",") if p.strip()]}
        # the load step's keys as the settings write them (from: the load before the step)
        step = {("from" if key == "i_from" else key): ctrl.GetValue().strip() for key, ctrl in self.ls.items()
                if ctrl.GetValue().strip()}
        if step:
            data["load_step"] = step
        try:
            spec = _pdn_from_data(data, "the form")
        except ConfigError as e:
            message_box("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
            return None
        problems = pdn_problems(spec)
        if self.board is not None and self.settings is not None:
            from copy import copy
            probe = copy(self.settings.config)
            probe.nets, probe.converters, probe.bridges, probe.dc_loads, probe.pdn = [], [], [], [], [spec]
            problems += validate(probe, self.board)
        if problems:
            message_box("\n".join(problems), "GroundTruth", wx.ICON_WARNING)
            return None
        return spec

    def _commit(self) -> bool:
        """Write the list to the settings and show it again; False (the list restored) when the settings refuse it."""
        try:
            self.settings.set_pdn(self.pdn)
        except ConfigError as e:
            message_box("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
            self.pdn = list(self.settings.config.pdn)
            return False
        self.refresh()
        self.on_change()
        return True

    def _row_names(self, rows: list[int]) -> list[str]:
        """The rails of these rows."""
        return [self.pdn[i].label for i in rows if i < len(self.pdn)]

    def on_power_tree(self, _=None):
        """Power tree…: the board's power tree in a window of its own (ui/power_tree_window.py)."""
        from .power_tree_window import open_power_tree
        open_power_tree(self)

    def on_find(self, _=None):
        """Find rails: every rail ICs draw supply from that is not listed yet, added with its defaults."""
        if self.board is None:
            return
        listed = {d.rail for d in self.pdn}
        new = []
        for rail in self.rails:
            if rail in listed:
                continue
            stage = self.conv_rails.get(rail)
            if stage is None:
                new.append(PdnSpec(rail))
                continue
            # a power stage's output: read at its output connector when no IC draws from it, the step half its load
            spec = next((c for c in self.settings.config.converters if c.name == stage[0]), None)
            ports = [stage[1]] if stage[1] and not self.rails.get(rail) else []
            new.append(PdnSpec(rail, voltage=spec.v_out if spec else None,
                               i_step=0.5 * spec.i_out if spec and spec.i_out else None, ports=ports))
        if not new:
            message_box("Every rail the ICs draw supply from is listed already." if self.rails else
                          "No IC on this board has a supply pin (schematic pin type power_in) on a rail with a pad on "
                          f"{self._ground()}: add a rail by hand (Rail, then Add).", "GroundTruth", wx.ICON_INFORMATION)
            return
        self.pdn += new
        if self._commit():
            message_box(f"Added {len(new)} rail(s): {', '.join(d.rail for d in new)}. Set each one's load step (the "
                          "current the ICs draw at once) for a target impedance, and switch off the ones not to "
                          "check.", "GroundTruth", wx.ICON_INFORMATION)

    def on_add(self, _):
        """Add the form's rail."""
        spec = self._form()
        if spec:
            self.pdn.append(spec)
            self._commit()

    def on_update(self, _):
        """Replace the selected rail with the form's values (its on/off state kept)."""
        rows = self.selected_rows()
        if len(rows) != 1:
            message_box("Select a single rail to update it from the form.", "GroundTruth", wx.ICON_INFORMATION)
            return
        spec = self._form()
        if spec:
            self.pdn[rows[0]] = replace(spec, enabled=self.pdn[rows[0]].enabled)
            if self._commit():
                self.reselect(rows)

    def on_remove(self, _):
        """Remove the selected rails."""
        rows = set(self.selected_rows())
        if rows:
            self.pdn = [d for i, d in enumerate(self.pdn) if i not in rows]
            self._commit()
