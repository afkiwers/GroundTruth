"""The laid-out message boxes (ui/messages.py): a settings problem named as the tab's row, numbers in engineering
form, and the hand-over to a test's stand-in for wx.MessageBox."""
import wx

from groundtruth.ui import messages


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
