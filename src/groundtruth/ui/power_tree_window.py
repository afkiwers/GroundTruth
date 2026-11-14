"""The power tree in a window of its own (Power tree… on the Power integrity and Converters tabs): the flow chart of
analysis/power_tree.py drawn by report/power_tree.py, with Fit (the whole tree in the window, or full size), Refresh
(read the board and the settings again), Copy (an image on the clipboard) and Save image… (PNG or SVG). The user:
"it would be nice if the user could create a flow chart similar to this per button press to better understand the
topology".

The graph is read and drawn in a background worker (ui/background.LatestOnly: matplotlib off the GUI thread; the
window never waits on it), the PNG shown when it arrives. No timers."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Callable, Optional

import wx

from . import theme
from .messages import box as message_box


def _render(board, config, results, dark: bool, title: str) -> tuple[bytes, object]:
    """(worker) The tree and its PNG."""
    from ..analysis.power_tree import power_tree
    from ..report.power_tree import image
    tree = power_tree(board, config, results)
    return image(tree, "png", dark, title), tree


class PowerTreeWindow(wx.Frame):
    """The power tree's window (see the module). `source()` gives (board, config, results or None, title) as they
    are now: called on the GUI thread at each Refresh, the reading and drawing then in the worker."""

    def __init__(self, parent, source: Callable[[], tuple]):
        """The window over `parent`, its first drawing started."""
        super().__init__(parent, title="Power tree", size=(1200, 700))
        theme.set_icon(self)
        self.source = source
        self.png: Optional[bytes] = None
        self.tree = None
        self.fit = True
        self._asked = 0
        from .background import LatestOnly
        self._worker = LatestOnly("power-tree", on_error=lambda e: wx.CallAfter(self._failed, e))
        p = theme.palette()
        self.SetBackgroundColour(theme.colour(p.card))
        self.view = wx.ScrolledWindow(self)
        self.view.SetScrollRate(10, 10)
        self.view.SetBackgroundColour(theme.colour(p.card))
        self.bitmap = wx.StaticBitmap(self.view)
        vs = wx.BoxSizer(wx.VERTICAL)
        vs.Add(self.bitmap, 0, wx.ALL, 8)
        self.view.SetSizer(vs)
        self.status = wx.StaticText(self, label="Reading the board …", name="muted")
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        for label, tip, fn in (("Fit", "The whole tree in the window, or full size (click again)", self._toggle_fit),
                               ("Refresh", "Read the board and the settings again", self.refresh),
                               ("Copy", "The tree to the clipboard, as an image", self._copy),
                               ("Save image…", "Save the tree as PNG or SVG", self._save)):
            b = theme.Button(self, label)
            b.SetToolTip(tip)
            b.Bind(wx.EVT_BUTTON, lambda e, fn=fn: fn())
            row.Add(b, 0, wx.LEFT, 6)
            if label == "Copy":
                self.copy_button = b
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self.view, 1, wx.EXPAND)
        s.Add(row, 0, wx.ALL | wx.EXPAND, 8)
        self.SetSizer(s)
        self.view.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), self.fit and wx.CallAfter(self._show)))
        self.CentreOnParent()
        self.refresh()

    def refresh(self):
        """Read the board and the settings now and draw the tree in the worker."""
        try:
            board, config, results, title = self.source()
        except Exception as e:              # no board open, settings not loaded: said in the window
            self.status.SetLabel(f"Nothing to draw: {e}")
            return
        if board is None or config is None:
            self.status.SetLabel("Open a board first.")
            return
        self._asked += 1
        asked, dark = self._asked, theme.palette().dark
        self.status.SetLabel("Drawing the power tree …")

        def work():
            """(worker) Read and draw; the PNG to the window."""
            png, tree = _render(board, config, results, dark, title)
            wx.CallAfter(self._drawn, asked, png, tree)
        self._worker.submit(work)

    def _drawn(self, asked: int, png: bytes, tree):
        """(GUI thread) A drawing arrived: shown unless a newer one was asked for or the window is gone."""
        if not self or asked != self._asked:
            return
        self.png, self.tree = png, tree
        n = sum(1 for x in tree.nodes if x.kind == "rail")
        self.status.SetLabel(f"{n} rails, {sum(1 for x in tree.nodes if x.kind in ('smps', 'ldo'))} regulators"
                             + (f". {'; '.join(tree.notes)}" if tree.notes else ""))
        self._show()

    def _failed(self, e: Exception):
        """(GUI thread) The drawing failed: said in the status line."""
        if self:
            self.status.SetLabel(f"The power tree could not be drawn: {e}")

    def _show(self):
        """The PNG in the view: scaled down to the view's size with Fit, else full size (scrolling)."""
        if not self or self.png is None:
            return
        img = wx.Image(io.BytesIO(self.png), wx.BITMAP_TYPE_PNG)
        if self.fit:
            w, h = self.view.GetClientSize()
            k = min((w - 16) / max(img.GetWidth(), 1), (h - 16) / max(img.GetHeight(), 1), 1.0)
            if k > 0 and k < 1.0:
                img = img.Scale(max(int(img.GetWidth() * k), 1), max(int(img.GetHeight() * k), 1),
                                wx.IMAGE_QUALITY_HIGH)
        self.bitmap.SetBitmap(wx.Bitmap(img))
        self.view.FitInside()
        self.view.Layout()

    def _toggle_fit(self):
        """Fit: the whole tree in the window, or full size."""
        self.fit = not self.fit
        self._show()

    def _copy(self):
        """The drawing on the clipboard as an image (the button says whether it worked)."""
        if self.png is None:
            return
        from .fixes_panel import copy_png
        self.copy_button.SetLabel("Copied" if copy_png(self.png) else "Clipboard busy")

    def _save(self):
        """Save the tree as PNG (as drawn) or SVG (drawn again as vectors)."""
        if self.tree is None:
            return
        with wx.FileDialog(self, "Save the power tree", wildcard="PNG image (*.png)|*.png|SVG image (*.svg)|*.svg",
                           defaultFile="power_tree.png", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = Path(dlg.GetPath())
        try:
            if path.suffix.lower() == ".svg":
                from ..report.power_tree import image
                path.write_bytes(image(self.tree, "svg"))
            else:
                path.write_bytes(self.png)
        except OSError as e:
            message_box(f"Could not save {path}: {e}", "GroundTruth", wx.ICON_WARNING)


def open_power_tree(panel):
    """Power tree… on a tab: the window over the main window, reading the tab's board and settings (and the last
    Simulate's results for the stages' efficiency)."""
    def source():
        """(board, config, results, title) as they are now."""
        top = panel.GetTopLevelParent()
        sim = getattr(top, "_last_sim", None)
        results = sim[2] if sim and sim[0] is panel.board else None
        name = Path(getattr(top, "board_path", None) or "").stem
        return panel.board, panel.settings.config if panel.settings else None, results, name
    PowerTreeWindow(panel.GetTopLevelParent(), source).Show()
