"""A masked text field with an eye button that shows its text only after the user has proved it is them (the user,
2026-11-14: "add an eye icon to display the server key passwords. if possible, guard it behind windows hello ...
only if it's correct it will be shown"). Preferences › Compute server › Access key uses it.

Who may look: Windows Hello (ui/hello.py: PIN, fingerprint, face) where this computer has it set up; elsewhere (no
Windows Hello, or not Windows) the password of this computer set with Set password… (jobs_password), the one that
already guards Clear past jobs and a server's options; with neither, the key stays hidden. Both checks run off the
GUI thread (the window never freezes: the Hello dialog waits for the user, a password hash takes ~0.5 s). Hiding
again needs nothing. A wx TextCtrl can't change its masked style once made, so the field holds two (masked and
plain) and shows one, their text kept the same."""
from __future__ import annotations

import threading
from typing import Callable, Optional

import wx

from . import theme
from .messages import box as message_box

TITLE = "GroundTruth: show the access key"


class EyeButton(wx.Control):
    """A small flat button drawn as an eye: open while the text shows, struck through while it is hidden."""

    def __init__(self, parent, on_click: Callable[[], None]):
        """A square button (about a text field's height) calling on_click() when pressed."""
        super().__init__(parent, style=wx.BORDER_NONE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.shown = False
        self._hover = False
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_LEFT_UP, lambda e: on_click() if self.IsEnabled() else None)
        self.Bind(wx.EVT_ENTER_WINDOW, lambda e: self._set_hover(True))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self._set_hover(False))
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.SetInitialSize(self.FromDIP(wx.Size(24, 22)))

    def _set_hover(self, on: bool):
        """Highlight under the mouse."""
        self._hover = on
        self.Refresh()

    def set_shown(self, shown: bool) -> None:
        """Draw the open (text shown) or struck-through (hidden) eye."""
        self.shown = shown
        self.Refresh()

    def Enable(self, enable: bool = True):
        """Greyed like the field when disabled."""
        out = super().Enable(enable)
        self.Refresh()
        return out

    def _paint(self, evt):
        """The eye: an almond outline of two arcs, the pupil, and a slash while the text is hidden."""
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        p = theme.palette()
        w, h = self.GetClientSize()
        # the labels' own colour (whatever theme styled the box), the accent under the mouse, faded when greyed
        fg, bg = self.GetParent().GetForegroundColour(), self.GetParent().GetBackgroundColour()
        if not self.IsEnabled():
            ink = wx.Colour(*((a + b) // 2 for a, b in zip(fg.Get(False), bg.Get(False))))
        else:
            ink = theme.colour(p.accent) if self._hover else fg
        s = min(w, h) * 0.8 / 2                          # half the eye's width
        cx, cy = w / 2, h / 2
        k = self.GetDPIScaleFactor()

        def pen(c, width):
            """An anti-aliased pen `width` DIPs wide (fractional widths kept)."""
            return gc.CreatePen(wx.GraphicsPenInfo(c, width * k))
        gc.SetPen(pen(ink, 1.4))
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        path = gc.CreatePath()                          # the almond: two quadratic arcs through the corners
        path.MoveToPoint(cx - s, cy)
        path.AddQuadCurveToPoint(cx, cy - s * 1.15, cx + s, cy)
        path.AddQuadCurveToPoint(cx, cy + s * 1.15, cx - s, cy)
        gc.StrokePath(path)
        gc.SetBrush(wx.Brush(ink))
        r = s * 0.32
        gc.DrawEllipse(cx - r, cy - r, 2 * r, 2 * r)
        if not self.shown:                              # the slash, with a background-coloured gap around it
            gc.SetPen(pen(self.GetParent().GetBackgroundColour(), 3.4))
            gc.StrokeLine(cx - s * 0.8, cy + s * 0.8, cx + s * 0.8, cy - s * 0.8)
            gc.SetPen(pen(ink, 1.4))
            gc.StrokeLine(cx - s * 0.8, cy + s * 0.8, cx + s * 0.8, cy - s * 0.8)


class SecretField(wx.Panel):
    """A masked text field and its eye button; GetValue / SetValue / Enable / SetToolTip as a TextCtrl's."""

    def __init__(self, parent, value: str = "", what: str = "the access key"):
        """The field with `value`, hidden. `what` names the secret in the verification prompts."""
        super().__init__(parent)
        self.what = what
        self._busy = False
        self._gone = threading.Event()                  # set when the field is destroyed: a pending Hello cancels
        self.masked = wx.TextCtrl(self, value=value, style=wx.TE_PASSWORD)
        self.plain = wx.TextCtrl(self, value=value)
        self.plain.Hide()
        self.eye = EyeButton(self, self.on_eye)
        self.eye.SetToolTip(f"Show {what}: asks Windows Hello (or this computer's password) first")
        s = wx.BoxSizer(wx.HORIZONTAL)
        s.Add(self.masked, 1, wx.ALIGN_CENTER_VERTICAL)
        s.Add(self.plain, 1, wx.ALIGN_CENTER_VERTICAL)
        s.Add(self.eye, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(4))
        self.SetSizer(s)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)

    def _destroyed(self, evt):
        """Our own destruction (the event bubbles up from the children): stop a pending verification."""
        if evt.GetEventObject() is self:
            self._gone.set()
        evt.Skip()

    # ---------------------------------------------------------------- the TextCtrl interface its users need
    def shown(self) -> bool:
        """Whether the text is shown in clear."""
        return self.plain.IsShown()

    def _active(self) -> wx.TextCtrl:
        """The text control on screen."""
        return self.plain if self.shown() else self.masked

    def GetValue(self) -> str:
        """The text (the same in both controls' turn: the one on screen)."""
        return self._active().GetValue()

    def SetValue(self, value: str) -> None:
        """Set the text of both controls."""
        self.masked.SetValue(value)
        self.plain.SetValue(value)

    def Enable(self, enable: bool = True):
        """Enable or grey the field and its eye."""
        for w in (self.masked, self.plain, self.eye):
            w.Enable(enable)
        return super().Enable(enable)

    def SetToolTip(self, tip):
        """The tooltip on the text controls (the panel itself is hidden under them)."""
        self.masked.SetToolTip(tip)
        self.plain.SetToolTip(tip)

    # ---------------------------------------------------------------- show / hide
    def _show(self, on: bool) -> None:
        """Swap the masked and plain controls, carrying the text (and the caret) across."""
        if on == self.shown():
            return
        src, dst = (self.masked, self.plain) if on else (self.plain, self.masked)
        dst.SetValue(src.GetValue())
        dst.SetInsertionPoint(src.GetInsertionPoint())
        src.Hide()
        dst.Show()
        self.eye.set_shown(on)
        self.eye.SetToolTip(f"Hide {self.what}" if on else
                            f"Show {self.what}: asks Windows Hello (or this computer's password) first")
        self.Layout()

    def on_eye(self) -> None:
        """The eye: hide at once; show only after the user is verified (in the background)."""
        if self.shown():
            self._show(False)
            return
        if self._busy:
            return
        self._busy = True
        self.eye.Disable()
        hwnd = self.GetTopLevelParent().GetHandle()
        message = f"GroundTruth: verify it is you to show {self.what} of the compute server."
        gone = self._gone

        def work():
            """(thread) Windows Hello when it is set up; otherwise say why not (the fallback is then asked)."""
            from . import hello
            if hello.supported() and hello.availability() == hello.AVAILABLE:
                return "hello", hello.verify(hwnd, message, cancelled=gone.is_set)
            return "none", None

        def done(out):
            """(GUI thread) Show on success; otherwise say why, or fall back to this computer's password."""
            if not self:
                return
            kind, result = out
            if kind == "hello":
                from . import hello
                self._ended()
                if result == hello.VERIFIED:
                    self._show(True)
                elif result != hello.CANCELED:
                    message_box(f"Windows Hello did not verify you ({hello.RESULT_TEXT.get(result, result)}): "
                                f"{self.what} stays hidden.", TITLE, wx.ICON_WARNING, self)
                return
            self._fallback()
        _in_thread(work, done)

    def _ended(self) -> None:
        """The check is over: the eye takes clicks again."""
        self._busy = False
        self.eye.Enable(self.masked.IsEnabled())

    def _fallback(self) -> None:
        """No Windows Hello here: this computer's password (Set password…) when one is set, checked off the GUI
        thread; with none, the key stays hidden."""
        from . import jobs_password as jp
        rec = jp.record(theme.prefs())
        if rec is None:
            self._ended()
            message_box(f"Showing {self.what} needs Windows Hello, which this computer does not have set up, or this "
                        "computer's password: set one with Set password… (under Past jobs) first.", TITLE,
                        wx.ICON_INFORMATION, self)
            return
        with wx.PasswordEntryDialog(self, f"This computer's password (Set password…) to show {self.what}:",
                                    TITLE) as dlg:
            pw: Optional[str] = dlg.GetValue() if dlg.ShowModal() == wx.ID_OK else None
        if pw is None:
            self._ended()
            return

        def checked(ok):
            """(GUI thread) Show on the right password."""
            if not self:
                return
            self._ended()
            if ok:
                self._show(True)
            else:
                message_box(f"Wrong password: {self.what} stays hidden.", TITLE, wx.ICON_WARNING, self)
        _in_thread(lambda: jp.matches(rec, pw), checked)


def _in_thread(work, done) -> None:
    """work() in a daemon thread, then done(its result) on the GUI thread (done checks its window still exists)."""
    def run():
        """(thread) The work, then back to the GUI thread."""
        out = work()
        wx.CallAfter(done, out)
    threading.Thread(target=run, name="gt-secret-check", daemon=True).start()
