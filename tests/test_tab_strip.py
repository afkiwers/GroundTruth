"""The tab strip (ui/tabbook.py) keeps its drawing as a bitmap and draws it anew only when something it shows changed:
every repaint was some 100 wx calls, each one giving the GIL up beside the solver's threads (220 ms with the window
frozen in gui_probe). Skipped without wxPython or a display."""
import dataclasses

import pytest

wx = pytest.importorskip("wx")


@pytest.fixture(scope="module")
def app():
    try:
        a = wx.App(False)
    except Exception as e:   # no display (CI)
        pytest.skip(f"no display for wx: {e}")
    yield a
    a.Destroy()


@pytest.fixture
def strip(app, monkeypatch):
    """A window with a book of three tabs; the strip's `_draw` calls are counted in `strip.draws`."""
    from groundtruth.ui import tabbook
    draws = []
    real = tabbook.TabStrip._draw
    monkeypatch.setattr(tabbook.TabStrip, "_draw", lambda self, dc, pal: (draws.append(1), real(self, dc, pal))[1])
    frame = wx.Frame(None, size=(500, 200))
    book = tabbook.TabBook(frame)
    for name, hue in (("Nets", "#3b82f6"), ("Results", None), ("Issues", None)):
        book.AddPage(wx.Panel(book), name, hue=hue)
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(book.strip, 0, wx.EXPAND)
    sizer.Add(book, 1, wx.EXPAND)
    frame.SetSizer(sizer)
    frame.Show()
    wx.Yield()
    book.strip.draws = draws
    yield book
    frame.Destroy()
    wx.Yield()


def repaint(strip):
    """Paint the strip now."""
    strip.Refresh()
    strip.Update()
    wx.Yield()


def test_a_repaint_blits_the_bitmap_and_a_change_draws_it_anew(strip, monkeypatch):
    """Painting again and again draws once; a badge, the selected tab, the hovered tab, a tab's label and the
    theme each make the next paint draw anew; the painted pixels are the drawing's."""
    from groundtruth.ui import theme
    s = strip.strip
    repaint(s)
    first = len(s.draws)
    assert first >= 1
    for _ in range(5):
        repaint(s)
    assert len(s.draws) == first                           # five repaints, no drawing

    strip.set_badge(0, 3, "three")
    repaint(s)
    assert len(s.draws) == first + 1
    strip.SetSelection(2)
    repaint(s)
    assert len(s.draws) == first + 2
    s._set_hover(1)
    repaint(s)
    assert len(s.draws) == first + 3
    strip.SetPageText(1, "Maps")
    s.changed()                                            # what AddPage / set_badge do for a new label
    repaint(s)
    assert len(s.draws) == first + 4
    dark = dataclasses.replace(theme.palette(), dark=not theme.palette().dark, bg="#101010")
    monkeypatch.setattr(theme, "_current", dark)
    repaint(s)
    assert len(s.draws) == first + 5
    repaint(s)
    assert len(s.draws) == first + 5

    w, h = s.GetClientSize()
    shot = wx.Bitmap(w, h)
    mem = wx.MemoryDC(shot)
    mem.Blit(0, 0, w, h, wx.ClientDC(s), 0, 0)
    mem.SelectObject(wx.NullBitmap)
    assert shot.ConvertToImage().GetRed(2, 2) == int(dark.bg[1:3], 16)    # the strip's own background colour


def test_a_resize_draws_the_strip_at_its_new_size(strip):
    """The bitmap belongs to a size."""
    s = strip.strip
    repaint(s)
    n = len(s.draws)
    s.GetParent().SetSize((420, 200))
    wx.Yield()
    repaint(s)
    assert len(s.draws) > n
