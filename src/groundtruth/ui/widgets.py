"""Small widgets and helpers shared by the tabs (and kept cheap for the GUI thread)."""
from __future__ import annotations

from typing import Callable, Optional

import wx

from ..components.values import parse_value


def set_lazy_items(combo: wx.ComboBox, items: list[str]):
    """Give an editable combo box a long list (a board's nets) that is filled in only when it drops down.
    Filling a native combo box costs ~0.6 ms per item on Windows: 70 ms per box for a board's 114 nets, and
    seconds on big boards, which would stall the GUI thread at every board load. Typing a value works without
    the list."""
    combo._lazy_items = list(items)
    if not getattr(combo, "_lazy_bound", False):
        combo.Bind(wx.EVT_COMBOBOX_DROPDOWN, _fill)
        combo._lazy_bound = True
    if combo.GetCount():                    # drop the previous board's list; keep what is typed
        value = combo.GetValue()
        combo.Clear()
        combo.ChangeValue(value)


MARKS = {"schematic": "◆", "differs": "!", "": ""}


class FieldMark(wx.StaticText):
    """The mark next to a part parameter's field: ◆ from the schematic fields, ! set here and different from them
    (components/fields.stage_marks), with the explanation as its tooltip."""
    def __init__(self, parent):
        """An empty mark, one character wide (fixed size: a mark appearing does not shift the form)."""
        super().__init__(parent, label="", size=wx.Size(16, -1), style=wx.ALIGN_CENTRE_HORIZONTAL | wx.ST_NO_AUTORESIZE)

    def show(self, state: str, tip: str):
        """Show the mark for `state` ("schematic", "differs" or "": none); "differs" in the warning colour."""
        from . import theme
        self.SetLabel(MARKS.get(state, ""))
        self.SetForegroundColour(theme.colour(theme.palette().warn) if state == "differs" else
                                 theme.colour(theme.palette().text))
        self.SetToolTip(tip or "")


def with_mark(parent, ctrl) -> tuple[wx.BoxSizer, FieldMark]:
    """A form field with its FieldMark to the right."""
    mark = FieldMark(parent)
    row = wx.BoxSizer(wx.HORIZONTAL)
    row.Add(ctrl, 1, wx.EXPAND)
    row.Add(mark, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 3)
    return row, mark


def parse_field(text: str, unit: str = "") -> Optional[float]:
    """A form field's value: "500kHz" with unit "Hz" is 500e3; empty is None."""
    t = text.strip()
    if not t:
        return None
    if unit and t.lower().endswith(unit.lower()):
        t = t[: -len(unit)]
    return parse_value(t)


def source_status(source: str, reviewed: bool) -> str:
    """A settings entry's Status column: detected and not yet checked shows in capitals."""
    if source == "auto":
        return "auto, reviewed" if reviewed else "auto, REVIEW"
    return "manual"


def button_row(parent, buttons: list[tuple[str, Callable]], tips: dict[str, str]) -> wx.WrapSizer:
    """A row of buttons (label, handler) that wraps when the window is narrow, each with its tooltip."""
    from . import theme
    row = wx.WrapSizer(wx.HORIZONTAL, wx.REMOVE_LEADING_SPACES)       # no stretched last button
    for label, fn in buttons:
        b = theme.Button(parent, label)
        b.Bind(wx.EVT_BUTTON, fn)
        b.SetToolTip(tips.get(label, ""))
        row.Add(b, 0, wx.RIGHT | wx.BOTTOM, 6)
    return row


class PickDialog(wx.Dialog):
    """Pick which detected entries to add: one row per new entry with a tick box (the likely ones ticked) and the
    reasons in the last column; `selected()` gives the ticked entries' specs."""

    def __init__(self, parent, title: str, columns: list[tuple[str, int]], found: list, new: list,
                 row: Callable[[object], list[str]], ticked: Callable[[object], bool], note: str,
                 size: tuple[int, int] = (1050, 420)):
        """found: everything detection found; new: those not yet in the settings (the rows). row(d) gives a
        row's cells, ticked(d) whether it starts ticked; note: a line under the count of new suggestions."""
        super().__init__(parent, title=title, size=size, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.found = new
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.EnableCheckBoxes()
        for i, (c, w) in enumerate(columns):
            self.list.InsertColumn(i, c, width=w)
        for k, d in enumerate(new):
            first, *rest = row(d)
            item = self.list.InsertItem(k, first)
            for col, v in enumerate(rest, 1):
                self.list.SetItem(item, col, v)
            self.list.CheckItem(item, ticked(d))
        text = wx.StaticText(self, label=f"{len(new)} new suggestion(s) ({len(found) - len(new)} already listed). "
                                         + note)
        text.Wrap(size[0] - 50)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(text, 0, wx.ALL, 8)
        s.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        s.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(s)

    def selected(self) -> list:
        """The ticked rows' specs (what to add to the settings)."""
        return [d.spec for k, d in enumerate(self.found) if self.list.IsItemChecked(k)]


class InfoBox(wx.Panel):
    """A tab's help, out of the way: one line "ⓘ <title>  <this board's status>  [Show]", and under it, while
    shown, a read-only text (selectable, so a label can be copied from it). Cheap to build: two labels, a button
    and a text control that stays hidden until asked for."""

    def __init__(self, parent, title: str, text: str, lines: int = 12):
        """The box with its help `text` (hidden), `lines` lines tall when shown."""
        super().__init__(parent)
        from . import theme
        self.text = text
        self.head = wx.StaticText(self, label="ⓘ " + title)
        self.status = wx.StaticText(self, label="", style=wx.ST_ELLIPSIZE_END, name="muted")
        self.button = theme.Button(self, "Show")
        self.button.SetToolTip("Show or hide how this tab's parts are found and how to label them in the schematic")
        self.button.Bind(wx.EVT_BUTTON, self.toggle)
        self.body = wx.TextCtrl(self, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP)
        self.body.SetMinSize(wx.Size(-1, lines * self.GetCharHeight()))
        self.body.Hide()
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.head, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        row.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        row.Add(self.button, 0, wx.ALIGN_CENTER_VERTICAL)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(row, 0, wx.EXPAND)
        s.Add(self.body, 1, wx.EXPAND | wx.TOP, 4)
        self.SetSizer(s)

    def set_status(self, line: str):
        """This board's part of the help (its labels and their problems): on the line, and on top of the text."""
        self.status.SetLabel(line)
        self.status.SetToolTip(line)
        self.body.ChangeValue((line + "\n\n" if line else "") + self.text)

    def shown(self) -> bool:
        """The help text is shown."""
        return self.body.IsShown()

    def toggle(self, _=None):
        """Show or hide the help text, and lay out the tab again (the text takes or gives back its space)."""
        self.body.Show(not self.body.IsShown())
        self.button.SetLabel("Hide" if self.body.IsShown() else "Show")
        top = self.GetParent()
        top.Layout()
        top.Refresh()


def _fill(evt):
    """A lazy combo box is being opened: put its items in now (see set_lazy_items), keeping the typed value. Filling
    combos only when opened keeps building a tab with many of them fast."""
    combo = evt.GetEventObject()
    items = combo._lazy_items
    if combo.GetCount() != len(items):
        value = combo.GetValue()
        combo.Freeze()                      # one redraw instead of one per item (2.4x faster)
        try:
            combo.Set(items)
        finally:
            combo.Thaw()
        combo.ChangeValue(value)
    evt.Skip()
