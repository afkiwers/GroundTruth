"""DC loads tab: DC rails with loads to simulate (analysis/dcload.py; the user, of a passive distribution board:
"how can I simulate a load across the highlighted trace?").

A rail is a source pin (the supply's connector pin), optionally its return pin, and loads: a pin drawing a fixed
current, optionally with its return pin. Simulate solves them on the whole-board copper model: the drop each load
sees, the copper's current density and loss, the busiest vias; Thermal and Current capacity take them too.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

import wx
from .messages import box as message_box   # laid-out message boxes (ui/messages.py)

from ..components.values import format_si
from ..config.settings import ConfigError, DcLoadSpec, load_text, parse_load_text
from .listsel import SelectableRows
from .pin_complete import complete_field, complete_load_line, pin_table
from .widgets import button_row

FIELD_TIPS = {
    "Name": "A name for this rail, used in the lists, the Results tab and the reports (e.g. VDD rail).",
    "Source pin": "The pin the rail is fed from, as REF.PAD: the supply connector's pin, e.g. J1.2. Typing lists the "
                  "board's pins (Ctrl+Space or Down: all of them, supply connectors first); Enter or Tab takes one.",
    "Source return": "The pin the current returns to at the source, e.g. J1.1 (GND). With it (and each load's "
                     "return) the ground side is solved too: its drop adds to what each load sees. Empty: the supply "
                     "side only. Typing lists the ground pins, the source part's own first.",
    "Loads": "One load per line: PIN/RETURN=CURRENT, e.g. J2.2/J2.1=0.5A (the return pin may be left out when the "
             "source has none: J2.2=0.5A). Every load draws its current at once. Typing lists the pins on the source "
             "pin's net (after the \"/\": the load part's own pin on the return's net); Enter or Tab takes one, "
             "Ctrl+Space lists them on an empty line.",
    "Max drop (V)": "Optional: a load seeing a larger total drop (supply plus return) is an issue, e.g. 0.2.",
    "Note": "Your own note (kept in the settings file).",
}
BUTTON_TIPS = {
    "Fill from net…": "fill the form from the board: pick the rail's net and its ground, then which part feeds it and "
                      "which parts draw from it (each part's pin on the rail and its ground pin are found for you)",
    "Add": "add the form's rail to the list (and the settings file)",
    "Update selected": "replace the selected rail with the form's values",
    "Remove selected": "remove the selected rails (Delete does the same)",
}


class PinPopup:
    """The board's pins in a list under a text field while the user types (ui/pin_complete.py says which and in
    what order): Up/Down choose, Enter or Tab (or a click) puts the pin in, Escape closes it; Ctrl+Space (or Down in
    a one-line field) opens it on an empty field. A load's pin is followed by its "/" (or "="), so the return's list
    comes up at once."""

    ROWS = 10                   # the list's height in rows (longer lists scroll)

    def __init__(self, ctrl: wx.TextCtrl, ask):
        """`ask(line_text, col, line_no)` -> a pin_complete.Completion for the caret's place, or None."""
        self.ctrl, self.ask = ctrl, ask
        self.comp, self.line, self.busy = None, 0, False
        self.win: Optional[wx.PopupWindow] = None       # made on first use: _window()
        self.box: Optional[wx.ListBox] = None
        ctrl.Bind(wx.EVT_TEXT, self._on_text)
        # the char hook comes before Tab's move to the next field and before the multi-line field's new line
        ctrl.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        ctrl.Bind(wx.EVT_KILL_FOCUS, self._on_blur)
        ctrl.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

    def _window(self) -> wx.PopupWindow:
        """The popup, made the first time it is needed under the field's top-level window then. Not at construction:
        the tabs are built under a temporary window first, and a popup made there was destroyed with it. A popup
        window, not a child of the form, so it may reach past the panel's edge without being clipped."""
        if self.win is None or not self.win:            # never made, or destroyed with its top-level window
            top = self.ctrl.GetTopLevelParent()
            self.win = wx.PopupWindow(top, wx.BORDER_SIMPLE)
            self.box = wx.ListBox(self.win, style=wx.LB_SINGLE)
            s = wx.BoxSizer(wx.VERTICAL)
            s.Add(self.box, 1, wx.EXPAND)
            self.win.SetSizer(s)
            self.box.Bind(wx.EVT_LISTBOX, lambda e: self.accept())
            top.Bind(wx.EVT_MOVE, lambda e: (e.Skip(), self.hide()))     # the window moved: the list would stay
        return self.win

    def shown(self) -> bool:
        """The list is up."""
        return self.win is not None and bool(self.win) and self.win.IsShown()

    def hide(self):
        """Close the list."""
        if self.shown():
            self.win.Hide()

    def _on_destroy(self, evt):
        """The field goes: its popup with it (a top-level window is not destroyed with the form)."""
        evt.Skip()
        if evt.GetEventObject() is self.ctrl and self.win is not None:
            if self.win:
                self.win.Destroy()
            self.win = self.box = None

    def _on_text(self, evt):
        """Typed (or a pin put in): what fits now."""
        evt.Skip()
        if not self.busy:
            self.update()

    def _on_blur(self, evt):
        """Focus left the field: the list closes, unless it went to the list itself (a click on it)."""
        evt.Skip()
        if evt.GetWindow() is not self.box:
            self.hide()

    def _on_key(self, evt):
        """Up/Down/Enter/Tab/Escape while the list is up; Ctrl+Space and Down (one-line field) open it."""
        key = evt.GetKeyCode()
        if key == wx.WXK_SPACE and evt.ControlDown():
            self.update(force=True)
            return
        if not self.shown():
            if key == wx.WXK_DOWN and not self.ctrl.IsMultiLine():
                self.update(force=True)
                return
            evt.Skip()
            return
        n, k = self.box.GetCount(), self.box.GetSelection()
        if key in (wx.WXK_DOWN, wx.WXK_UP, wx.WXK_PAGEDOWN, wx.WXK_PAGEUP):
            step = {wx.WXK_DOWN: 1, wx.WXK_UP: -1, wx.WXK_PAGEDOWN: self.ROWS, wx.WXK_PAGEUP: -self.ROWS}[key]
            self.box.SetSelection(min(n - 1, max(0, k + step)))
            self.box.EnsureVisible(self.box.GetSelection())
        elif key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_TAB):
            self.accept()
        elif key == wx.WXK_ESCAPE:
            self.hide()
        else:
            evt.Skip()

    def update(self, force: bool = False):
        """Ask what fits the caret's place and show it under the caret, or close the list. Only while the field has
        the focus (filling the form from a selected row must not open it), or when asked (`force`)."""
        if not (force or wx.Window.FindFocus() is self.ctrl):
            self.hide()
            return
        if wx.Window.FindFocus() is not self.ctrl:
            # Windows draws a multi-line field's grey hint as its text while it has no focus: focused, the hint goes
            # and the positions below are the typed text's
            self.ctrl.SetFocus()
        pos = self.ctrl.GetInsertionPoint()
        ok, col, line = self.ctrl.PositionToXY(pos)
        if not ok:
            self.hide()
            return
        comp = self.ask(self.ctrl.GetLineText(line), col, line)
        if comp is None or not comp.pins:
            self.hide()
            return
        self.comp, self.line = comp, line
        self._window()
        self.box.Set([p.label() for p in comp.pins])
        self.box.SetSelection(0)
        # under the caret's line (a one-line field: under the field), at least as wide as the field
        row_h = self.box.GetCharHeight() + 4
        size = wx.Size(max(self.ctrl.GetSize().width, self.box.GetTextExtent("W" * 34).width),
                       row_h * min(self.ROWS, len(comp.pins)) + 6)
        at = wx.Point(0, self.ctrl.GetSize().height)
        if self.ctrl.IsMultiLine():
            p = self.ctrl.PositionToCoords(self.ctrl.XYToPosition(comp.start, line))
            if p.x >= 0 and p.y >= 0:
                at = wx.Point(p.x, p.y + self.ctrl.GetCharHeight() + 2)
        screen = self.ctrl.ClientToScreen(at)
        area = wx.Display(max(0, wx.Display.GetFromWindow(self.ctrl))).GetClientArea()
        if screen.y + size.height > area.GetBottom():                     # no room below: above the line
            screen.y -= size.height + self.ctrl.GetCharHeight() + 4
        screen.x = max(area.GetLeft(), min(screen.x, area.GetRight() - size.width))
        self.win.SetSize(size)
        self.win.SetPosition(screen)
        self.win.Layout()
        if not self.win.IsShown():
            self.win.Show()

    def accept(self):
        """Put the chosen pin in place of the typed part (and a load's "/" or "="), then show what comes next."""
        if not self.shown() or self.comp is None or self.box.GetSelection() < 0:
            return
        if wx.Window.FindFocus() is not self.ctrl:
            self.ctrl.SetFocus()            # back from a click on the list (and no grey hint in the text: update)
        comp, line = self.comp, self.line
        text = comp.pins[self.box.GetSelection()].pin + comp.suffix
        start = self.ctrl.XYToPosition(comp.start, line)
        end = self.ctrl.XYToPosition(comp.end, line)
        self.busy = True
        try:
            self.ctrl.Replace(start, end, text)
            self.ctrl.SetInsertionPoint(start + len(text))
        finally:
            self.busy = False
        if comp.suffix:
            self.update(force=True)             # the load's return, right after its "/" (the user is in the field)
        else:
            self.hide()


class DcLoadsPanel(SelectableRows, wx.Panel):
    """The DC loads tab: the rails (a list on the left, each with its on/off tick) and a form on the right. Every
    change is written to the settings at once."""

    def __init__(self, parent, on_change):
        """on_change(): the settings changed (the window saves them and marks results as stale)."""
        super().__init__(parent)
        self.on_change = on_change
        self.board = None
        self.settings = None
        self.dc_loads: list[DcLoadSpec] = []
        self.hint = wx.StaticText(self, label="No DC loads yet: press Fill from net… (on the right) to pick the rail's "
                                              "source and loads from the board, then Add.")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        for i, (c, w) in enumerate([("Name", 150), ("Status", 70), ("Source", 90), ("Return", 80), ("Loads", 60),
                                    ("Total", 70), ("Max drop", 80)]):
            self.list.InsertColumn(i, c, width=w)
        self.list.SetMinSize(wx.Size(320, 120))
        self._init_rows(None, "DC loads")
        self._init_enable("dc_loads")
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)

        fw = wx.Panel(self)
        form = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        form.AddGrowableCol(1)
        form.AddGrowableRow(3)
        self.name = wx.TextCtrl(fw)
        self.source = wx.TextCtrl(fw)
        self.source_return = wx.TextCtrl(fw)
        self.loads = wx.TextCtrl(fw, style=wx.TE_MULTILINE, size=(-1, 120))
        self.max_drop = wx.TextCtrl(fw)
        self.note = wx.TextCtrl(fw)
        # examples in grey: an empty field shows what goes in it (a hint is not a value: the user took the loads'
        # example for one and pressed Add on an empty form)
        for ctrl, hint in ((self.name, "e.g. VDD rail"), (self.source, "e.g. J1.2: type for the board's pins"),
                           (self.source_return, "e.g. J1.1 (optional)"),
                           (self.loads, "one per line, e.g.\nJ2.2/J2.1=0.5A\nJ3.2/J3.1=0.5A\n(type for the pins on the "
                                        "rail)"),
                           (self.max_drop, "e.g. 0.2 (optional)")):
            ctrl.SetHint(hint)
        # the board's pins offered while typing (ui/pin_complete.py; the user: "can you auto complete the source pin
        # source return as well as the loads?")
        self.pins: list = []
        self.popups = {
            "source": PinPopup(self.source, lambda text, col, _line: complete_field(self.pins, "source", text, col)),
            "source_return": PinPopup(self.source_return, lambda text, col, _line: complete_field(
                self.pins, "source_return", text, col, source=self.source.GetValue().strip())),
            "loads": PinPopup(self.loads, self._ask_load)}
        for label, ctrl in (("Name", self.name), ("Source pin", self.source), ("Source return", self.source_return),
                            ("Loads", self.loads), ("Max drop (V)", self.max_drop), ("Note", self.note)):
            text = wx.StaticText(fw, label=label)
            text.SetToolTip(FIELD_TIPS[label])
            ctrl.SetToolTip(FIELD_TIPS[label])
            form.Add(text, 0, wx.ALIGN_TOP | wx.TOP, 3)
            form.Add(ctrl, 1, wx.EXPAND)
        fw.SetSizer(form)
        btns = button_row(self, [("Fill from net…", self.on_fill), ("Add", self.on_add),
                                 ("Update selected", self.on_update), ("Remove selected", self.on_remove)], BUTTON_TIPS)
        about = wx.StaticText(self, label="Simulate solves each rail on the whole board's copper: the drop each load "
                                          "sees, the current density and the vias (Results tab), and Thermal and "
                                          "Current capacity heat the board with it.")
        about.SetName("muted")
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(fw, 1, wx.EXPAND)
        right.Add(btns, 0, wx.TOP | wx.EXPAND, 8)
        right.Add(about, 0, wx.TOP | wx.EXPAND, 8)
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(self.list_head(self.hint), 0, wx.EXPAND | wx.BOTTOM, 6)
        left.Add(self.list, 1, wx.EXPAND)
        s = wx.BoxSizer(wx.HORIZONTAL)
        s.Add(left, 3, wx.EXPAND | wx.ALL, 6)
        s.Add(right, 2, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(s)
        self.hint.Hide()
        # the note under the buttons wraps to the form's width (two fifths of the tab, as the sizer gives it)
        self.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), about.Wrap(max(150, self.GetClientSize().width * 2 // 5 - 24))))

    def load(self, board, settings):
        """Show this board's DC loads."""
        self.board, self.settings = board, settings
        self.pins = pin_table(board) if board is not None else []
        self.dc_loads = list(settings.config.dc_loads)
        self.refresh()

    def _ask_load(self, text: str, col: int, line: int):
        """The Loads field's completion for a line (the other lines' load pins are not offered again)."""
        listed = tuple(ln.split("=")[0].split("/")[0].strip()
                       for k, ln in enumerate(self.loads.GetValue().splitlines()) if k != line)
        return complete_load_line(self.pins, text, col, self.source.GetValue().strip(),
                                  self.source_return.GetValue().strip(), tuple(p for p in listed if p))

    def refresh(self):
        """Rebuild the list from self.dc_loads (switched-off rows greyed)."""
        self.hint.Show(self.board is not None and not self.dc_loads)
        self.Layout()
        self.list.DeleteAllItems()
        for d in self.dc_loads:
            i = self.list.InsertItem(self.list.GetItemCount(), d.name)
            for col, v in enumerate(["", d.source, d.source_return or "–", str(len(d.loads)), format_si(d.current, "A"),
                                     format_si(d.max_drop, "V") if d.max_drop else "–"], 1):
                self.list.SetItem(i, col, v)
            self._show_enabled(i, d)

    def on_select(self, evt):
        """A row was selected: fill the form with its rail."""
        self._selection_changed()
        d = self.dc_loads[evt.GetIndex()]
        self.name.SetValue(d.name)
        self.source.SetValue(d.source)
        self.source_return.SetValue(d.source_return)
        self.loads.SetValue("\n".join(load_text(ld) for ld in d.loads))
        self.max_drop.SetValue("" if d.max_drop is None else f"{d.max_drop:g}")
        self.note.SetValue(d.note)

    def _form(self) -> Optional[DcLoadSpec]:
        """The form as a DcLoadSpec, or None after telling the user what is wrong (the settings' own parsing and the
        board's pins check it)."""
        from ..config.settings import _dc_load_from_data, dc_load_problems, validate
        lines = [ln.strip() for ln in self.loads.GetValue().splitlines() if ln.strip()]
        if not self.source.GetValue().strip() and not lines:
            message_box("The form is empty. Press Fill from net… to pick the rail's parts from the board, or type the "
                          "source pin (e.g. J1.2) and one load per line (e.g. J2.2/J2.1=0.5A), then Add.",
                          "GroundTruth", wx.ICON_INFORMATION)
            return None
        try:
            spec = _dc_load_from_data({"name": self.name.GetValue().strip() or "DC load",
                                       "source": self.source.GetValue().strip(),
                                       "source_return": self.source_return.GetValue().strip(),
                                       "loads": [parse_load_text(ln, f"load {k + 1}") for k, ln in enumerate(lines)],
                                       "max_drop": self.max_drop.GetValue().strip() or None,
                                       "note": self.note.GetValue().strip()}, "the form", 0)
        except ConfigError as e:
            message_box("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
            return None
        problems = dc_load_problems(spec)
        if self.board is not None and self.settings is not None:
            from copy import copy
            probe = copy(self.settings.config)
            probe.nets, probe.converters, probe.bridges, probe.dc_loads = [], [], [], [spec]
            problems += validate(probe, self.board)
        if problems:
            message_box("\n".join(problems), "GroundTruth", wx.ICON_WARNING)
            return None
        return spec

    def _commit(self) -> bool:
        """Write the list to the settings and show it again; False (the list restored) when the settings refuse it."""
        try:
            self.settings.set_dc_loads(self.dc_loads)
        except ConfigError as e:
            message_box("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
            self.dc_loads = list(self.settings.config.dc_loads)
            return False
        self.refresh()
        self.on_change()
        return True

    def _row_names(self, rows: list[int]) -> list[str]:
        """The rails of these rows."""
        return [self.dc_loads[i].name for i in rows if i < len(self.dc_loads)]

    def on_fill(self, _=None):
        """Fill from net…: the rail's net and its ground, then the part feeding it and the parts drawing from it (from
        analysis.dcload.rail_candidates: each part's rail pin and its own ground pin), and a current per load."""
        from ..analysis.dcload import guess_source, rail_candidates
        if self.board is None:
            return
        nets = sorted(n for n in self.board.nets if n)
        rail_guess = next((n for n in nets if n.upper().lstrip("/+").startswith(("VDD", "VCC", "VIN", "VBAT", "VBUS",
                                                                                  "+"))), nets[0] if nets else "")
        rail = self._choose("The rail's net (the supply the loads draw from):", nets, rail_guess)
        if rail is None:
            return
        ground = self._choose("Its ground net (the return; Cancel: the supply side only):", nets,
                              "GND" if "GND" in nets else "")
        cands = rail_candidates(self.board, rail, ground or "")
        if len(cands) < 2:
            message_box(f"{rail} reaches {len(cands)} part(s): a rail needs a source and at least one load.",
                          "GroundTruth", wx.ICON_INFORMATION)
            return

        def label(c) -> str:
            """A part in the lists: its pins and value."""
            return f"{c['ref']} ({c['value']}): {c['pin']}" + (f", return {c['ret']}" if c["ret"] else ", no ground pin")
        dlg = wx.SingleChoiceDialog(self, f"Which part feeds {rail}?", "Fill from net: the source",
                                    [label(c) for c in cands])
        dlg.SetSelection(max(0, guess_source(cands)))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            src = cands[dlg.GetSelection()]
        finally:
            dlg.Destroy()
        rest = [c for c in cands if c is not src]
        dlg = wx.MultiChoiceDialog(self, f"Which parts draw current from {rail}?", "Fill from net: the loads",
                                   [label(c) for c in rest])
        dlg.SetSelections([k for k, c in enumerate(rest) if c["connector"]] or list(range(len(rest))))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            loads = [rest[k] for k in dlg.GetSelections()]
        finally:
            dlg.Destroy()
        if not loads:
            return
        dlg = wx.TextEntryDialog(self, "Current per load (each line can be changed afterwards), e.g. 0.5A:",
                                 "Fill from net: the current", "0.5A")
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            amps = dlg.GetValue().strip() or "0.5A"
        finally:
            dlg.Destroy()
        # the returns only when the source and every load have a ground pin (the ground side must balance)
        with_ret = bool(ground) and bool(src["ret"]) and all(c["ret"] for c in loads)
        self.name.SetValue(f"{rail.lstrip('/')} rail")
        self.source.SetValue(src["pin"])
        self.source_return.SetValue(src["ret"] if with_ret else "")
        self.loads.SetValue("\n".join(f"{c['pin']}{'/' + c['ret'] if with_ret else ''}={amps}" for c in loads))
        self.list.SetFocus()

    def _choose(self, message: str, choices: list, default: str):
        """One net from a list (default preselected), or None (Cancel)."""
        dlg = wx.SingleChoiceDialog(self, message, "Fill from net", choices)
        if default in choices:
            dlg.SetSelection(choices.index(default))
        try:
            return choices[dlg.GetSelection()] if dlg.ShowModal() == wx.ID_OK else None
        finally:
            dlg.Destroy()

    def on_add(self, _):
        """Add the form's rail."""
        spec = self._form()
        if spec:
            self.dc_loads.append(spec)
            self._commit()

    def on_update(self, _):
        """Replace the selected rail with the form's values (its on/off state kept)."""
        rows = self.selected_rows()
        if len(rows) != 1:
            message_box("Select a single DC load to update it from the form.", "GroundTruth", wx.ICON_INFORMATION)
            return
        spec = self._form()
        if spec:
            self.dc_loads[rows[0]] = replace(spec, enabled=self.dc_loads[rows[0]].enabled)
            if self._commit():
                self.reselect(rows)

    def on_remove(self, _):
        """Remove the selected rails."""
        rows = set(self.selected_rows())
        if rows:
            self.dc_loads = [d for i, d in enumerate(self.dc_loads) if i not in rows]
            self._commit()
