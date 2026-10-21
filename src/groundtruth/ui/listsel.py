"""Multi-row selection for the Nets and Converters tables: Ctrl/Shift-click selects several rows, Delete removes
them (the panel's on_remove), Ctrl+A selects all; and the rows selected go to `on_show` (live mode: shown in
KiCad) once the selection has settled."""
from __future__ import annotations

import wx

SHOW_SETTLE_MS = 300        # a selection is shown in KiCad when it has not changed for this long


class SelectableRows:
    """Mixin for a panel with `self.list` (a wx.ListCtrl in report mode, not LC_SINGLE_SEL), `on_remove(evt)` and
    `_row_names(rows)` (the names to show for those row indices)."""

    def _init_rows(self, on_show, what: str):
        """Call after creating self.list. Creates self.zoom (shown only with on_show), to add to the layout."""
        self.on_show = on_show
        self._show_later = None             # the pending show (wx.CallLater), restarted by every change
        self._show_names = None             # what that show shows instead of the selected rows (_selection_changed)
        self.list.Bind(wx.EVT_LIST_KEY_DOWN, self.on_key)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: (self._selection_changed(), e.Skip()))
        self.list.Bind(wx.EVT_LIST_ITEM_DESELECTED, lambda e: (self._selection_changed(), e.Skip()))
        self.list.SetToolTip(f"Ctrl- or Shift-click selects several {what}; Delete removes the selected ones, Ctrl+A "
                             "selects all.")
        self.zoom = wx.CheckBox(self, label=f"Zoom KiCad to the selected {what}")
        self.zoom.SetToolTip(f"Clicking {what} highlights them in KiCad (Ctrl-click: several); this also zooms the "
                             "view to their copper. ~ in KiCad clears the highlight.")
        self.zoom.SetValue(True)
        self.zoom.Show(on_show is not None)

    def _init_enable(self, attr: str):
        """A tick box at the start of every row: ticked, the row is analysed; unticked, it keeps all its values
        but Run checks and Simulate leave it out (Config.active). The user: "if one wants to not simulate a
        particular net, converter 'row' one has to delete it which then looses all the data entered". attr: the
        panel's list of specs (self.nets ...); refresh() calls _show_enabled for each row; a click saves through
        the panel's _commit()."""
        self._enable_attr = attr
        self._filling_ticks = False
        self.list.EnableCheckBoxes()
        self.list.Bind(wx.EVT_LIST_ITEM_CHECKED, lambda e: self._ticked(e.GetIndex(), True))
        self.list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, lambda e: self._ticked(e.GetIndex(), False))

    def _show_enabled(self, i: int, spec):
        """Row i's tick (without it counting as a click), greyed when switched off."""
        self._filling_ticks = True
        try:
            self.list.CheckItem(i, spec.enabled)
        finally:
            self._filling_ticks = False
        if not spec.enabled:
            from . import theme
            self.list.SetItemTextColour(i, theme.colour(theme.palette().muted))
            self.list.SetItem(i, 1, "off · " + self.list.GetItemText(i, 1))     # the Status column

    def _ticked(self, i: int, on: bool):
        """The user ticked or unticked row i: switch that row's entry on or off and save (ticks set while the
        table is filled are not clicks: _filling_ticks)."""
        if self._filling_ticks:
            return
        specs = getattr(self, self._enable_attr)
        if not 0 <= i < len(specs) or specs[i].enabled == on:
            return
        from dataclasses import replace
        rows = self.selected_rows()
        specs[i] = replace(specs[i], enabled=on)
        if self._commit() is False:          # refused (an invalid entry): show what is stored
            self.refresh()
        self.reselect(rows)

    def selected_rows(self) -> list[int]:
        """The selected rows' indices, in order."""
        rows, i = [], self.list.GetFirstSelected()
        while i >= 0:
            rows.append(i)
            i = self.list.GetNextSelected(i)
        return rows

    def reselect(self, rows: list[int]):
        """Select these rows again after the table was refreshed (which clears the selection)."""
        for i in rows:
            if i < self.list.GetItemCount():
                self.list.Select(i)

    def select_only(self, i: int):
        """Select row i alone and scroll to it."""
        for k in self.selected_rows():
            if k != i:
                self.list.Select(k, False)
        self.list.Select(i)
        self.list.Focus(i)
        self.list.EnsureVisible(i)

    def on_key(self, evt):
        """Delete removes the selected rows, Ctrl+A selects them all; other keys go on to the list."""
        key = evt.GetKeyCode()
        if key in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
            self.on_remove(None)
        elif key == ord("A") and wx.GetKeyState(wx.WXK_CONTROL):
            for i in range(self.list.GetItemCount()):
                self.list.Select(i)
        else:
            evt.Skip()

    def _selection_changed(self, names=None):
        """The selection changed: the selected rows go to on_show once it has not changed for SHOW_SETTLE_MS (a
        Shift-click sends one event per row; clicking or arrowing down a table sends one burst per row). Showing a
        stage in KiCad selects, highlights and zooms to its copper there: done for every row passed over, it kept
        KiCad and the highlight thread busy and the window lagged (the user, clicking through the Converters tab).
        Each change restarts the wait, so only the row the user stops on is shown. names: a function giving what
        to show instead of the selected rows (a bridge's leg picked in its second table); the latest call wins."""
        if self.on_show is None:
            return
        self._show_names = names
        if self._show_later is not None and self._show_later.IsRunning():
            self._show_later.Restart(SHOW_SETTLE_MS)
            return

        def show():
            """The selection has settled: hand it to on_show (the window may have closed meanwhile)."""
            if self:
                pick = self._show_names
                self.on_show(pick() if pick else self._row_names(self.selected_rows()), self.zoom.GetValue())
        self._show_later = wx.CallLater(SHOW_SETTLE_MS, show)
