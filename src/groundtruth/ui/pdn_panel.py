"""Power integrity tab: supply rails whose PDN impedance Simulate checks (analysis/pdn.py; the plan's M1).

A rail is its net, its ground, the allowed ripple and the load step (they give the target impedance), and the part
feeding it (found by itself: a regulator's power_out pin or a connector) with its output resistance and inductance.
Simulate gives each IC's impedance curve against the target (Results tab: the curves and what holds the rail at each
frequency), and issues where it is over the target or a supply pin has no decap close by.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

import wx

from ..components.values import format_si
from ..config.settings import ConfigError, PdnSpec
from .listsel import SelectableRows
from .widgets import button_row

AUTO_SOURCE = "found by itself"
NO_SOURCE = "none"
FIELD_TIPS = {
    "Rail": "The supply net to check, e.g. +3V3 (the list: nets that ICs draw supply from, the busiest first).",
    "Ground": "Its ground (default: the first reference net, e.g. GND).",
    "Voltage (V)": "The rail's voltage; empty: from its name (+3V3: 3.3 V, VDD_1V8: 1.8 V).",
    "Ripple (%)": "The voltage ripple allowed, in % of the voltage (default 5 %).",
    "Load step (A)": "The current step the ICs draw (e.g. 0.3). With it the target impedance is voltage x ripple / "
                     "step; empty: the curves without a target.",
    "Source": "The part feeding the rail: found by itself (a regulator's power_out pin on it, else a connector), "
              "none, or a part with a pad on the rail.",
    "Source R (Ω)": "The source's output resistance (a regulator's flat output impedance), default 5m.",
    "Source L (H)": "Its output inductance above its loop bandwidth, default 20n (an LDO or a buck: tens of nH).",
    "ICs": "The ICs to check, e.g. U1, U2; empty: every IC with supply pins on the rail.",
    "Check up to (Hz)": "The target applies up to here (default 100M: above, the package and the die decouple).",
    "Note": "Your own note (kept in the settings file).",
}
BUTTON_TIPS = {
    "Find rails": "add every rail ICs draw supply from that is not listed yet (its source found by itself); then "
                  "set each one's load step for a target",
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
        fw = self._fw = wx.Panel(self)
        form = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        form.AddGrowableCol(1)
        self.rail = wx.ComboBox(fw)
        self.ground = wx.ComboBox(fw)
        self.voltage = wx.TextCtrl(fw)
        self.ripple = wx.TextCtrl(fw)
        self.step = wx.TextCtrl(fw)
        self.source = wx.ComboBox(fw)
        self.vrm_r = wx.TextCtrl(fw)
        self.vrm_l = wx.TextCtrl(fw)
        self.parts = wx.TextCtrl(fw)
        self.check_to = wx.TextCtrl(fw)
        self.note = wx.TextCtrl(fw)
        for ctrl, hint in ((self.voltage, "from the name"), (self.ripple, "5"), (self.step, "e.g. 0.3 (for a target)"),
                           (self.vrm_r, "5m"), (self.vrm_l, "20n"), (self.parts, "every IC on the rail"),
                           (self.check_to, "100M")):
            ctrl.SetHint(hint)
        self.rail.Bind(wx.EVT_TEXT, self._rail_changed)
        for label, ctrl in (("Rail", self.rail), ("Ground", self.ground), ("Voltage (V)", self.voltage),
                            ("Ripple (%)", self.ripple), ("Load step (A)", self.step), ("Source", self.source),
                            ("Source R (Ω)", self.vrm_r), ("Source L (H)", self.vrm_l), ("ICs", self.parts),
                            ("Check up to (Hz)", self.check_to), ("Note", self.note)):
            text = wx.StaticText(fw, label=label)
            text.SetToolTip(FIELD_TIPS[label])
            ctrl.SetToolTip(FIELD_TIPS[label])
            form.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(ctrl, 1, wx.EXPAND)
        fw.SetSizer(form)

    def _build_layout(self):
        """The buttons, the note and the tab's layout (the last build step)."""
        fw = self._fw
        del self._fw
        btns = button_row(self, [("Find rails", self.on_find), ("Add", self.on_add),
                                 ("Update selected", self.on_update), ("Remove selected", self.on_remove)], BUTTON_TIPS)
        about = wx.StaticText(self, label="Simulate solves each rail on the whole board: the impedance every IC sees "
                                          "at its supply pins from 10 kHz to 1 GHz against the target (Results tab: "
                                          "the curves, and what holds the rail at each frequency).")
        about.SetName("muted")
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(fw, 0, wx.EXPAND)
        right.Add(btns, 0, wx.TOP | wx.EXPAND, 8)
        right.Add(about, 0, wx.TOP | wx.EXPAND, 8)
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(self.hint, 0, wx.BOTTOM, 6)
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
        from ..analysis.pdn import rails_with_ics
        self.board, self.settings = board, settings
        self.rails = rails_with_ics(board, self._ground()) if board is not None else {}
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
        keep = self.source.GetValue()
        _set_items(self.source, [AUTO_SOURCE, NO_SOURCE] + parts)
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
            i = self.list.InsertItem(self.list.GetItemCount(), d.label)
            for col, text in enumerate([format_si(v, "V") if v else "?", f"{d.ripple * 100:g} %",
                                        format_si(d.i_step, "A") if d.i_step else "–",
                                        format_si(target, "Ω") if target else "–",
                                        d.source or AUTO_SOURCE,
                                        ", ".join(d.parts) if d.parts else
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
        self.check_to.SetValue(format_si(d.check_to, ""))
        self.note.SetValue(d.note)

    def _form(self) -> Optional[PdnSpec]:
        """The form as a PdnSpec, or None after telling the user what is wrong (the settings' own parsing and the
        board's nets and parts check it)."""
        from ..config.settings import _pdn_from_data, pdn_problems, validate
        rail = self.rail.GetValue().strip()
        if not rail:
            wx.MessageBox("Choose the rail (its net), or press Find rails.", "GroundTruth", wx.ICON_INFORMATION)
            return None
        src = self.source.GetValue().strip()
        data = {"rail": rail, "ground": self.ground.GetValue().strip() if self.ground.GetValue().strip() !=
                self._ground() else "",
                "voltage": self.voltage.GetValue().strip() or None, "ripple": (self.ripple.GetValue().strip() or "5")
                + "%", "i_step": self.step.GetValue().strip() or None,
                "source": "" if src in ("", AUTO_SOURCE) else src,
                "vrm_r": self.vrm_r.GetValue().strip() or "5m", "vrm_l": self.vrm_l.GetValue().strip() or "20n",
                "parts": self.parts.GetValue().strip(), "check_to": self.check_to.GetValue().strip() or "100M",
                "note": self.note.GetValue().strip()}
        try:
            spec = _pdn_from_data(data, "the form")
        except ConfigError as e:
            wx.MessageBox("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
            return None
        problems = pdn_problems(spec)
        if self.board is not None and self.settings is not None:
            from copy import copy
            probe = copy(self.settings.config)
            probe.nets, probe.converters, probe.bridges, probe.dc_loads, probe.pdn = [], [], [], [], [spec]
            problems += validate(probe, self.board)
        if problems:
            wx.MessageBox("\n".join(problems), "GroundTruth", wx.ICON_WARNING)
            return None
        return spec

    def _commit(self) -> bool:
        """Write the list to the settings and show it again; False (the list restored) when the settings refuse it."""
        try:
            self.settings.set_pdn(self.pdn)
        except ConfigError as e:
            wx.MessageBox("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
            self.pdn = list(self.settings.config.pdn)
            return False
        self.refresh()
        self.on_change()
        return True

    def _row_names(self, rows: list[int]) -> list[str]:
        """The rails of these rows."""
        return [self.pdn[i].label for i in rows if i < len(self.pdn)]

    def on_find(self, _=None):
        """Find rails: every rail ICs draw supply from that is not listed yet, added with its defaults."""
        if self.board is None:
            return
        listed = {d.rail for d in self.pdn}
        new = [PdnSpec(rail) for rail in self.rails if rail not in listed]
        if not new:
            wx.MessageBox("Every rail the ICs draw supply from is listed already." if self.rails else
                          "No IC on this board has a supply pin (schematic pin type power_in) on a rail with a pad on "
                          f"{self._ground()}: add a rail by hand (Rail, then Add).", "GroundTruth", wx.ICON_INFORMATION)
            return
        self.pdn += new
        if self._commit():
            wx.MessageBox(f"Added {len(new)} rail(s): {', '.join(d.rail for d in new)}. Set each one's load step (the "
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
            wx.MessageBox("Select a single rail to update it from the form.", "GroundTruth", wx.ICON_INFORMATION)
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
