"""The window's message boxes, laid out to be read at a glance (the user, of a settings error: "format this and any
other error window better. I want it to be clear and glancable. Currently it is a mess").

`box` has wx.MessageBox's signature and return values, so every call site uses it unchanged. It shows a headline
(the message's first line, or the caption's word for it), then each problem as a block: where it is in bold
("Signal path /CPU/ADC_I_RAIL_1"), what is wrong below it; long numbers in engineering form (6.67e-07 s -> 667 ns).
Tracebacks keep their own monospaced block. Tests replace wx.MessageBox with a stand-in that answers at once: box
then hands over to it, so they never wait on a dialog.
"""
from __future__ import annotations

import re
from typing import Optional

import wx

from . import theme

_ORIGINAL = wx.MessageBox          # the real one; a test's stand-in differs from it
NUMBER = re.compile(r"(-?\d+(?:\.\d+)?e[-+]?\d+)\s?(s|Hz|V|A|F|H|Ω|W)\b")
WHERE = re.compile(r"^(nets|converters|bridges|dc_loads|pdn|stack|components|si\.\w+|victims|analysis|rules|thermal|"
                   r"safety)(?:\[(\d+)\])?(?: \(([^)]*)\))?(?:\.(\w+))?: ")
KIND = {"nets": "Signal path", "converters": "Converter", "bridges": "Bridge", "dc_loads": "DC load",
        "pdn": "Power integrity rail", "stack": "Stacked board", "components": "Part", "victims": "Victims",
        "analysis": "Settings: analysis", "rules": "Settings: rules", "thermal": "Settings: thermal",
        "safety": "Settings: safety"}


def tidy(text: str) -> str:
    """Numbers in e-notation with a unit in engineering form (6.67e-07 s -> 667ns)."""
    from ..components.values import format_si
    return NUMBER.sub(lambda m: format_si(float(f"{float(m.group(1)):.3g}"), m.group(2)), text)


def split_problem(line: str) -> tuple[str, str]:
    """(where, what) of one problem line: a settings path ("nets[0] (/CPU/X): ...") named as the tab's row, else
    the part before a first ": " when it is short; ("", line) otherwise."""
    m = WHERE.match(line)
    if m:
        kind = KIND.get(m.group(1), m.group(1))
        name = m.group(3) or (f"#{int(m.group(2)) + 1}" if m.group(2) else "")
        field = f" · {m.group(4)}" if m.group(4) else ""
        return f"{kind} {name}".strip() + field, line[m.end():]
    head, sep, rest = line.partition(": ")
    if sep and len(head) <= 60 and "\n" not in head and rest:
        return head, rest
    return "", line


SCREEN_SHARE = 0.75         # the problems scroll when the dialog would be taller than this share of the screen


class MessageDialog(wx.Dialog):
    """The laid-out message: icon and headline, the problems as blocks (scrolling when there are many: 18 legs'
    problems ran off the screen), a monospaced detail (a traceback), and a Copy button next to the answers that puts
    the whole message on the clipboard as plain text (the user: "add a copy button so users can copy to clipboard if
    needed")."""

    def __init__(self, parent, message: str, caption: str, style: int):
        """Lay out `message` (wx.MessageBox's text) under `caption` with the buttons and icon `style` asks for."""
        super().__init__(parent, title=caption or "GroundTruth", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.plain = f"{caption}\n\n{message}" if caption else message      # what Copy puts on the clipboard
        gap = self.FromDIP(10)
        text, _, detail = message.partition("\n\nTraceback")
        detail = ("Traceback" + detail) if detail else ""
        lines = [tidy(x.strip()) for x in text.split("\n") if x.strip()]
        head = lines.pop(0) if lines and (len(lines) > 1 or lines[0].endswith(":")) else ""
        icon = (wx.ART_ERROR if style & wx.ICON_ERROR else wx.ART_WARNING if style & wx.ICON_WARNING
                else wx.ART_QUESTION if style & wx.ICON_QUESTION else wx.ART_INFORMATION)
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(wx.StaticBitmap(self, bitmap=wx.ArtProvider.GetBitmap(icon, wx.ART_MESSAGE_BOX)), 0, wx.RIGHT, gap)
        col = wx.BoxSizer(wx.VERTICAL)
        width = self.FromDIP(460)
        if head:
            h = wx.StaticText(self, label=head.rstrip(":"))
            h.SetFont(h.GetFont().Bold().Scaled(1.1))
            h.Wrap(width)
            col.Add(h, 0, wx.BOTTOM, gap)
        # the problems on a page of their own, which scrolls when they are many (sized below, once laid out)
        self.page = page = wx.ScrolledWindow(self, style=wx.VSCROLL)
        page.SetScrollRate(0, self.FromDIP(12))
        rows = wx.BoxSizer(wx.VERTICAL)
        for line in lines:
            where, what = split_problem(line)
            if where:
                w = wx.StaticText(page, label=where)
                w.SetFont(w.GetFont().Bold())
                rows.Add(w, 0, wx.TOP, gap // 2)
            t = wx.StaticText(page, label=what[:1].upper() + what[1:] if where else what)
            t.Wrap(width)
            rows.Add(t, 0, wx.LEFT if where else 0, gap if where else 0)
        page.SetSizer(rows)
        need = rows.GetMinSize()
        screen = wx.Display(max(wx.Display.GetFromWindow(parent), 0) if parent else 0).GetClientArea().height
        page.SetMinSize(wx.Size(need.width + wx.SystemSettings.GetMetric(wx.SYS_VSCROLL_X),
                                min(need.height, max(int(screen * SCREEN_SHARE) - self.FromDIP(220), self.FromDIP(120)))))
        col.Add(page, 1, wx.EXPAND)
        if detail:
            d = wx.TextCtrl(self, value=detail, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
                            size=self.FromDIP(wx.Size(560, 180)))
            d.SetFont(wx.Font(wx.FontInfo(8).Family(wx.FONTFAMILY_TELETYPE)))
            col.Add(d, 1, wx.EXPAND | wx.TOP, gap)
        top.Add(col, 1, wx.EXPAND)
        buttons = (wx.YES_NO if style & wx.YES_NO else wx.OK) | (wx.CANCEL if style & wx.CANCEL else 0)
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(top, 1, wx.EXPAND | wx.ALL, 2 * gap)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.copy = wx.Button(self, label="Copy")
        self.copy.SetToolTip("the whole message as plain text to the clipboard")
        self.copy.Bind(wx.EVT_BUTTON, self.on_copy)      # handled here: it does not close the dialog
        row.Add(self.copy, 0, wx.ALIGN_CENTER_VERTICAL)
        row.AddStretchSpacer()
        row.Add(self.CreateStdDialogButtonSizer(buttons), 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 2 * gap)
        self.SetSizerAndFit(box)
        if style & wx.NO_DEFAULT:
            no = self.FindWindow(wx.ID_NO)
            if no is not None:
                no.SetDefault()
        self.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(e.GetId()))
        theme.style_tree(self)
        self.CentreOnParent()

    def on_copy(self, _=None) -> bool:
        """Put the message (caption, text and any traceback, as given) on the clipboard; the button says so. False
        when the clipboard is busy."""
        if not wx.TheClipboard.Open():
            self.copy.SetLabel("Clipboard busy")
            return False
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(self.plain))
        finally:
            wx.TheClipboard.Close()
        self.copy.SetLabel("Copied")
        return True


def box(message: str, caption: str = "GroundTruth", style: int = wx.OK | wx.CENTRE, parent=None, *a, **k) -> int:
    """wx.MessageBox's drop-in: the laid-out dialog; wx.YES / wx.NO / wx.OK / wx.CANCEL as it returns them. A test's
    stand-in for wx.MessageBox answers instead."""
    if wx.MessageBox is not _ORIGINAL:
        return wx.MessageBox(message, caption, style, parent, *a, **k)
    if parent is None:
        parent = wx.GetActiveWindow() or next(iter(wx.GetTopLevelWindows()), None)
    with MessageDialog(parent, str(message), caption, style) as dlg:
        answer = dlg.ShowModal()
    return {wx.ID_YES: wx.YES, wx.ID_NO: wx.NO, wx.ID_OK: wx.OK, wx.ID_CANCEL: wx.CANCEL}.get(answer, wx.CANCEL)
