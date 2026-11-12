"""The laid-out message boxes (ui/messages.py): a settings problem named as the tab's row, numbers in engineering
form, and the hand-over to a test's stand-in for wx.MessageBox."""
import pytest
import wx

from groundtruth.ui import messages
from groundtruth.ui.widgets import read_clipboard


@pytest.fixture(scope="module")
def app():
    """A wx app for the dialog tests (skipped without a display)."""
    try:
        a = wx.App(False)
    except Exception as e:   # no display (CI)
        pytest.skip(f"no display for wx: {e}")
    yield a
    a.Destroy()


def test_problems_are_named_by_their_row_and_numbers_read_as_engineers_write_them():
    """split_problem: "nets[0] (/MCU/X): ..." is the Signal path /MCU/X, a field after it is named too; a short
    "head: rest" splits; anything else stays whole. tidy: 6.67e-07 s -> 667ns, 7.5e+05 Hz -> 750kHz."""
    assert messages.split_problem("nets[0] (/MCU/ADC): the edges don't fit") == ("Signal path /MCU/ADC",
                                                                                  "the edges don't fit")
    assert messages.split_problem("converters[2].f_sw: must be above 0") == ("Converter #3 · f_sw", "must be above 0")
    assert messages.split_problem("Could not read the board: file gone") == ("Could not read the board", "file gone")
    assert messages.split_problem("Nothing to say here") == ("", "Nothing to say here")
    assert messages.tidy("high for 6.67e-07 s at 7.5e+05 Hz") == "high for 667ns at 750kHz"


def test_a_tests_stand_in_answers_instead_of_the_dialog(monkeypatch):
    """With wx.MessageBox replaced (the tests' fixtures do), box hands its arguments over and returns the answer."""
    seen = []
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: seen.append(a) or wx.YES)
    assert messages.box("Delete?", "GroundTruth", wx.YES_NO) == wx.YES and seen[0][:2] == ("Delete?", "GroundTruth")


def test_a_long_message_scrolls_and_copy_puts_it_on_the_clipboard(app):
    """The user, of a settings error with 18 legs' problems that ran off the screen: "add a copy button so users can
    copy to clipboard if needed". The problems scroll within the screen; Copy puts caption and text on the
    clipboard as plain text and leaves the dialog open."""
    lines = [f"bridge 'bridge J2' leg {k}: Q{k} (low side) needs pads on PH and GND" for k in range(60)]
    message = "Could not simulate:\n" + "\n".join(lines)
    dlg = messages.MessageDialog(None, message, "groundtruth", wx.OK | wx.ICON_WARNING)
    try:
        screen = wx.Display(0).GetClientArea().height
        assert dlg.GetSize().height <= screen
        assert dlg.page.GetVirtualSize().height > dlg.page.GetClientSize().height    # the rest is a scroll away
        if not dlg.on_copy():
            # another program held the clipboard all through the tries (a Remote Desktop session's clipboard sync
            # did, twice, on the user's machine): the dialog says so, and there is nothing to read back
            assert dlg.copy.GetLabel() == "Clipboard busy"
            pytest.skip("the clipboard was held by another program")
        assert dlg.copy.GetLabel() == "Copied"
        data = wx.TextDataObject()
        if not read_clipboard(data):
            pytest.skip("the clipboard was held by another program")
        assert data.GetText() == f"groundtruth\n\n{message}"
    finally:
        dlg.Destroy()


def test_a_busy_clipboard_says_so_without_an_error_box(app, monkeypatch):
    """Another program holding the clipboard (a clipboard manager, Remote Desktop): on Windows wx's Open succeeds
    and SetData fails, and wx put up a modal error box ("Failed to put data on the clipboard"; the user met it
    during a test run, 2026-11-03). copy_text tries a few times, logs nothing, closes the clipboard every time and
    returns False; the dialog's Copy button says "Clipboard busy"."""
    from groundtruth.ui import widgets

    class Busy:
        """A clipboard another program holds: it opens, but no data goes in or out."""
        def __init__(self):
            self.opened = self.closed = 0

        def Open(self):
            self.opened += 1
            wx.LogError("Failed to put data on the clipboard (OpenClipboard Failed)")     # what wx logs
            return True

        def SetData(self, data):
            return False

        def GetData(self, data):
            return False

        def Close(self):
            self.closed += 1
    busy = Busy()
    monkeypatch.setattr(wx, "TheClipboard", busy)
    monkeypatch.setattr(widgets, "CLIPBOARD_WAIT_S", 0.0)
    logged = []

    class Catch(wx.Log):
        """Every wx log message the copy lets through."""
        def DoLogTextAtLevel(self, level, msg):
            logged.append(msg)
    old = wx.Log.SetActiveTarget(Catch())
    try:
        assert not widgets.copy_text("x") and not widgets.read_clipboard(wx.TextDataObject())
        dlg = messages.MessageDialog(None, "Could not simulate", "groundtruth", wx.OK)
        try:
            assert not dlg.on_copy() and dlg.copy.GetLabel() == "Clipboard busy"
        finally:
            dlg.Destroy()
    finally:
        wx.Log.SetActiveTarget(old)
    assert busy.opened == busy.closed == 3 * widgets.CLIPBOARD_TRIES and logged == []
