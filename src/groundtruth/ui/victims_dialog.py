"""The Bridges tab's victims picker: which nets a motor bridge's (or power stage's) switching may upset
(analysis/victims.py, settings `victims: nets:`). The user: "offer a selector where the user can add victims by
selecting nets and also review them ... select one or more and then add/bulk add them ... select one or more added
victims to delete/bulk delete them at once".

Two lists: the board's nets (filtered by what is typed) and the victims, each with why it is one. An empty
`victims.nets` means automatic: the nets are found on the board at each run (reset, crystal, Hall, debug, PWM nets;
analysis.victims.auto_nets), and the picker shows those marked "auto". The first Add or Remove turns the automatic
nets into a list of their own, so nothing found automatically drops out unnoticed (agreed with the user); Reset to
automatic goes back. Removing every net leaves the list empty, which is automatic again (said in the dialog).
"""
from __future__ import annotations

from typing import Optional

import wx

from . import theme


class VictimsDialog(wx.Dialog):
    """The picker. nets: the board's nets; listed: the settings' victims.nets ([]: automatic); auto: {net: why}
    found automatically. values() is the list to write ([]: automatic)."""

    def __init__(self, parent, nets: list[str], listed: list[str], auto: dict[str, str]):
        """The two lists, the buttons between them, Reset to automatic, OK / Cancel."""
        super().__init__(parent, title="Victim nets", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.all_nets = sorted(nets, key=str.lower)
        self.auto = dict(auto)
        self.listed: Optional[list[str]] = list(listed) or None     # None: automatic
        self.added: set[str] = set()                                 # added here (why: "added")
        gap = self.FromDIP(8)

        self.filter = wx.SearchCtrl(self)
        self.filter.ShowCancelButton(True)
        self.filter.SetDescriptiveText("filter, e.g. RST or /CPU/")
        self.filter.Bind(wx.EVT_TEXT, lambda e: self._fill_nets())
        self.filter.Bind(wx.EVT_SEARCHCTRL_CANCEL_BTN, lambda e: (self.filter.SetValue(""), self._fill_nets()))
        self.nets = wx.ListBox(self, style=wx.LB_EXTENDED | wx.LB_NEEDED_SB, size=self.FromDIP(wx.Size(280, 340)))
        self.nets.SetToolTip("The board's nets that are not victims yet. Ctrl- or Shift-click selects several; "
                             "double-click adds one.")
        self.nets.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self.add())
        self.victims = wx.ListCtrl(self, style=wx.LC_REPORT, size=self.FromDIP(wx.Size(380, 340)))
        self.victims.InsertColumn(0, "Victim net", width=self.FromDIP(220))
        self.victims.InsertColumn(1, "Why", width=self.FromDIP(140))
        self.victims.SetToolTip("The nets Simulate checks for noise picked up from the switching. Ctrl- or "
                                "Shift-click selects several; Delete or double-click removes.")
        self.victims.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self.remove())
        self.victims.Bind(wx.EVT_LIST_KEY_DOWN, self._key)

        add = theme.Button(self, "Add →")
        add.SetToolTip("add the nets selected on the left")
        add.Bind(wx.EVT_BUTTON, lambda e: self.add())
        rem = theme.Button(self, "← Remove")
        rem.SetToolTip("remove the victims selected on the right")
        rem.Bind(wx.EVT_BUTTON, lambda e: self.remove())
        self.reset = theme.Button(self, "Reset to automatic")
        self.reset.SetToolTip("forget the list: the victims are found on the board at each run again (reset, "
                              "crystal, Hall, debug and PWM nets by their names)")
        self.reset.Bind(wx.EVT_BUTTON, lambda e: self.reset_auto())
        self.mode = wx.StaticText(self)
        self.mode.SetName("muted")

        mid = wx.BoxSizer(wx.VERTICAL)
        mid.AddStretchSpacer()
        mid.Add(add, 0, wx.EXPAND | wx.BOTTOM, gap)
        mid.Add(rem, 0, wx.EXPAND)
        mid.AddStretchSpacer()
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(self, label="Board nets"), 0, wx.BOTTOM, gap // 2)
        left.Add(self.filter, 0, wx.EXPAND | wx.BOTTOM, gap // 2)
        left.Add(self.nets, 1, wx.EXPAND)
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(wx.StaticText(self, label="Victims"), 0, wx.BOTTOM, gap // 2)
        right.Add(self.victims, 1, wx.EXPAND)
        lists = wx.BoxSizer(wx.HORIZONTAL)
        lists.Add(left, 1, wx.EXPAND)
        lists.Add(mid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, gap)
        lists.Add(right, 1, wx.EXPAND)
        foot = wx.BoxSizer(wx.HORIZONTAL)
        foot.Add(self.reset, 0, wx.ALIGN_CENTER_VERTICAL)
        foot.Add(self.mode, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(lists, 1, wx.EXPAND | wx.ALL, 2 * gap)
        box.Add(foot, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 2 * gap)
        box.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 2 * gap)
        self.SetSizerAndFit(box)
        theme.style_tree(self)
        self._refresh()

    # ------------------------------------------------------------------ state
    def current(self) -> list[str]:
        """The victims shown: the list, or (automatic) the nets found on the board."""
        return list(self.listed) if self.listed is not None else sorted(self.auto, key=str.lower)

    def values(self) -> list[str]:
        """The victims.nets to write: the list, or [] for automatic."""
        return list(self.listed) if self.listed else []

    def _why(self, net: str) -> str:
        """Why a net is a victim: found automatically (and its kind), added here, or listed in the settings."""
        if self.listed is None:
            return f"auto: {self.auto.get(net, '')}"
        if net in self.added:
            return "added"
        return f"auto: {self.auto[net]}" if net in self.auto else "listed"

    def _own_list(self):
        """Automatic so far: the automatic nets become the list (nothing drops out unnoticed)."""
        if self.listed is None:
            self.listed = sorted(self.auto, key=str.lower)

    def add(self, nets: Optional[list[str]] = None):
        """Add the nets selected on the left (or `nets`)."""
        picked = nets if nets is not None else [self.nets.GetString(i) for i in self.nets.GetSelections()]
        if not picked:
            return
        self._own_list()
        for n in picked:
            if n not in self.listed:
                self.listed.append(n)
                self.added.add(n)
        self._refresh()

    def remove(self, nets: Optional[list[str]] = None):
        """Remove the victims selected on the right (or `nets`)."""
        if nets is None:
            rows, i = [], self.victims.GetFirstSelected()
            while i >= 0:
                rows.append(self.victims.GetItemText(i))
                i = self.victims.GetNextSelected(i)
            nets = rows
        if not nets:
            return
        self._own_list()
        gone = set(nets)
        self.listed = [n for n in self.listed if n not in gone]
        self.added -= gone
        self._refresh()

    def reset_auto(self):
        """Back to automatic."""
        self.listed, self.added = None, set()
        self._refresh()

    def _key(self, evt):
        """Delete on the victims list removes the selected ones."""
        if evt.GetKeyCode() in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
            self.remove()
        else:
            evt.Skip()

    # ------------------------------------------------------------------ the lists
    def _fill_nets(self):
        """The board's nets that are not victims, filtered by the text typed (any part of the name, any case)."""
        shown = set(self.current())
        needle = self.filter.GetValue().strip().lower()
        items = [n for n in self.all_nets if n not in shown and needle in n.lower()]
        self.nets.Set(items)

    def _refresh(self):
        """Both lists and the line that says automatic or listed."""
        self._fill_nets()
        self.victims.DeleteAllItems()
        for n in sorted(self.current(), key=str.lower):
            i = self.victims.InsertItem(self.victims.GetItemCount(), n)
            self.victims.SetItem(i, 1, self._why(n))
        if self.listed is None:
            self.mode.SetLabel(f"Automatic: {len(self.auto)} net(s) found by their names at each run.")
        elif not self.listed:
            self.mode.SetLabel("The list is empty: that is automatic again.")
        else:
            self.mode.SetLabel(f"Listed: only these {len(self.listed)} net(s) are victims.")
        self.reset.Enable(self.listed is not None)
