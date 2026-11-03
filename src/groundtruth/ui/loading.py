"""The card shown in the tabs' place while a board is read (the user, 2026-11-03: "rather than having the loading bar
at the bottom for loading the PCB, have it as an overlay, it sometime takes a while to load the PCB and if the user
is not aware of that it looks like the config is lost").

MainFrame swaps it with the tab pages while it loads (reading the board, filling the tabs, detecting on a new
settings file: MainFrame._loading) and back once the window is idle. It stands in the tab pages' place in the
window's sizer rather than floating over them: a child window over a sibling is painted over by it on Windows. No wx
objects at import time."""
from __future__ import annotations

from typing import Callable

import wx

from . import theme

NOTE = ("Your settings (signal paths, power stages, parts) appear here once the board is read: nothing is lost "
        "meanwhile.")


class LoadingCard(wx.Panel):
    """A centred card: what is being loaded, the step it is at (its percent, elapsed time and what is left, the
    status line's text: ui/progress.ProgressClock), a gauge, why the tabs are empty for now, and Cancel."""

    def __init__(self, parent, on_cancel: Callable[[], None]):
        """The card, hidden; on_cancel: what its Cancel button does (the top bar's Cancel)."""
        super().__init__(parent)                # its own background native, as the tab pages' (theme.style_tree)
        card = wx.Panel(self)
        # the card stands out from the window as the theme's cards do (the theme changes only when GroundTruth
        # opens again: a fixed colour is enough)
        card.SetBackgroundColour(theme.colour(theme.palette().card))
        card.SetName("loading-card")
        self.title = wx.StaticText(card, label="Loading the board…")
        self.title.SetFont(self.title.GetFont().Bold().Scaled(1.35))
        self.step = wx.StaticText(card, label="", style=wx.ST_ELLIPSIZE_END | wx.ST_NO_AUTORESIZE)
        self.gauge = wx.Gauge(card, range=1000, size=(-1, 10))
        note = wx.StaticText(card, label=NOTE, name="muted")
        note.Wrap(400)
        self.cancel = theme.Button(card, "Cancel", "danger")
        self.cancel.Bind(wx.EVT_BUTTON, lambda e: on_cancel())
        inner = wx.BoxSizer(wx.VERTICAL)
        inner.Add(self.title, 0, wx.BOTTOM, 10)
        inner.Add(self.step, 0, wx.EXPAND | wx.BOTTOM, 6)
        inner.Add(self.gauge, 0, wx.EXPAND | wx.BOTTOM, 12)
        inner.Add(note, 0, wx.EXPAND | wx.BOTTOM, 12)
        inner.Add(self.cancel, 0, wx.ALIGN_RIGHT)
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(inner, 1, wx.EXPAND | wx.ALL, 22)
        card.SetSizer(box)
        card.SetMinSize(wx.Size(460, -1))
        outer = wx.BoxSizer(wx.VERTICAL)        # the card in the middle of the tab area
        outer.AddStretchSpacer(2)
        outer.Add(card, 0, wx.ALIGN_CENTER_HORIZONTAL)
        outer.AddStretchSpacer(3)
        self.SetSizer(outer)
        self.card = card
        card.Bind(wx.EVT_PAINT, self._paint_border)
        self.Hide()

    def _paint_border(self, evt):
        """The card's 1-pixel border in the theme's border colour."""
        dc = wx.PaintDC(self.card)
        dc.SetPen(wx.Pen(theme.colour(theme.palette().border)))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        w, h = self.card.GetClientSize()
        dc.DrawRectangle(0, 0, w, h)

    def show_step(self, title: str, step: str, frac: float, cancellable: bool):
        """The card's text and gauge: `title` (what is loaded), `step` (the job's line), frac 0-1 (a negative
        value: no fraction known, the gauge pulses), and whether Cancel works now."""
        if self.title.GetLabel() != title:
            self.title.SetLabel(title)
            self.card.Layout()
        if self.step.GetLabel() != step:
            self.step.SetLabel(step)
            self.step.SetToolTip(step)
        if frac < 0:
            self.gauge.Pulse()
        else:
            value = int(max(0.0, min(1.0, frac)) * 1000)
            if self.gauge.GetValue() != value:
                self.gauge.SetValue(value)
        if self.cancel.IsEnabled() != cancellable:
            self.cancel.Enable(cancellable)
