"""The laid-out message boxes (ui/messages.py): a settings problem named as the tab's row, numbers in engineering
form, and the hand-over to a test's stand-in for wx.MessageBox."""
import pytest
import wx

from groundtruth.ui import messages


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
    """split_problem: "nets[0] (/CPU/X): ..." is the Signal path /CPU/X, a field after it is named too; a short
    "head: rest" splits; anything else stays whole. tidy: 6.67e-07 s -> 667ns, 7.5e+05 Hz -> 750kHz."""
    assert messages.split_problem("nets[0] (/CPU/ADC): the edges don't fit") == ("Signal path /CPU/ADC",
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
        assert dlg.on_copy() and dlg.copy.GetLabel() == "Copied"
        data = wx.TextDataObject()
        assert wx.TheClipboard.Open()
        try:
            wx.TheClipboard.GetData(data)
        finally:
            wx.TheClipboard.Close()
        assert data.GetText() == f"groundtruth\n\n{message}"
    finally:
        dlg.Destroy()
