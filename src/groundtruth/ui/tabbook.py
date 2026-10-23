"""The main window's tabs: a strip GroundTruth draws itself over a wx.Simplebook, in place of wx.Notebook, whose
native Windows tabs cannot be coloured one by one. The setup tabs (Signal paths to Signal integrity: what the user
enters before a run) each get a pastel colour of their own (the user: "colour those with pastel like colours and make
them stand out"); the tabs after them (parts, results, history) stay neutral, set apart by a gap.

A setup tab also carries a badge: how many of its rows take part in the next run (the user: "add (#) as indicator
for how many signals are actively part of the sim ... to make sure the user doesn't accidentally simulate unintended
signals"). A tab with none (nothing listed, or everything switched off) is drawn hollow, with a dashed outline and a
grey 0, so an empty tab shows at a glance; the tooltip says which it is.

TabBook keeps the wx.Notebook API the window, its tools and the tests use (AddPage, SetSelection, GetSelection,
FindPage, GetPageText, GetPageCount, GetCurrentPage), so nothing else changes. No wx objects at import time."""
from __future__ import annotations

from typing import Optional

import wx

from . import theme

# each setup tab's colour (by the window's attribute name for it): the tints and the underline are mixed from it
SETUP_HUES = {"nets": "#3b82f6",           # blue
              "converters": "#f59e0b",     # amber
              "bridges": "#f43f5e",        # rose
              "dc_loads": "#22c55e",       # green
              "pdn": "#8b5cf6",            # violet
              "si": "#14b8a6"}             # teal


def mix(a: str, b: str, t: float) -> str:
    """The colour a share `t` of the way from `b` to `a` ("#rrggbb"): t = 1 gives a, t = 0 gives b."""
    return "#" + "".join(f"{round(255 * (t * x + (1 - t) * y)):02x}" for x, y in zip(theme.rgb(a), theme.rgb(b)))


def tab_colours(hue: Optional[str], selected: bool, hover: bool, pal: theme.Palette) -> tuple:
    """(fill or None, text, underline or None) of one tab. A coloured tab: a pastel tint of its hue over the strip
    (stronger when hovered, strongest when selected), its label in a deep shade of the hue (light theme) or a pale one
    (dark), the full hue as the selected tab's underline. A neutral tab: no fill until hovered or selected, the muted
    text colour unless selected, the accent as its underline."""
    if hue is None:
        fill = pal.card if selected else pal.btn_hover if hover else None
        return fill, pal.text if selected else pal.muted, pal.accent if selected else None
    share = 0.34 if selected else 0.25 if hover else 0.18
    if pal.dark:
        return mix(hue, pal.panel, share), mix(hue, "#ffffff", 0.45), hue if selected else None
    return mix(hue, "#ffffff", share + 0.04), mix(hue, "#000000", 0.55), hue if selected else None


def badge_colours(hue: Optional[str], count: int, pal: theme.Palette) -> tuple:
    """(pill fill, pill text) of a tab's count badge: a stronger tint of the tab's hue with its deep shade (light) or
    white (dark); grey for 0 (nothing takes part)."""
    if count == 0 or hue is None:
        return pal.border, pal.muted
    if pal.dark:
        return mix(hue, pal.panel, 0.6), "#ffffff"
    return mix(hue, "#ffffff", 0.5), mix(hue, "#000000", 0.6)


class TabBook(wx.Simplebook):
    """The pages (a wx.Simplebook: no tabs of its own) and their strip (`strip`, a sibling the window lays out above
    the book). AddPage takes a page's colour and whether a gap goes before it; set_badge gives a tab its count;
    every change of page redraws the strip."""

    def __init__(self, parent):
        """An empty book in `parent`, with its strip (also a child of `parent`)."""
        super().__init__(parent)
        self.hues: list[Optional[str]] = []
        self.gaps: set[int] = set()            # pages with a gap before them (a new group starts)
        self.badges: dict[int, tuple] = {}     # page -> (count, tooltip)
        self.hidden: set[int] = set()          # pages without a tab (set_hidden): still there, not offered
        self.strip = TabStrip(parent, self)
        self.Bind(wx.EVT_BOOKCTRL_PAGE_CHANGED, lambda e: (e.Skip(), self.strip.Refresh()))

    def AddPage(self, page, text, select=False, hue: Optional[str] = None, gap: bool = False) -> bool:
        """Add `page` with tab label `text`, coloured with `hue` (None: neutral), after a gap when `gap`."""
        if gap:
            self.gaps.add(self.GetPageCount())
        self.hues.append(hue)
        done = super().AddPage(page, text, select)
        self.strip.changed()
        return done

    def set_badge(self, page: int, count: Optional[int], tip: str = "") -> None:
        """Show `count` on tab `page` (None: no badge), `tip` as the tab's tooltip; redrawn only when it changed."""
        new = (count, tip) if count is not None else None
        if self.badges.get(page) != new:
            if new is None:
                self.badges.pop(page, None)
            else:
                self.badges[page] = new
            self.strip.changed()

    def set_hidden(self, page: int, hidden: bool) -> None:
        """Take tab `page` off the strip (the page stays, and SetSelection can still show it) or put it back; a
        hidden page that is showing gives way to the first page."""
        if hidden == (page in self.hidden):
            return
        if hidden:
            self.hidden.add(page)
            if self.GetSelection() == page:
                self.SetSelection(0)
        else:
            self.hidden.discard(page)
        self.strip.changed()

    def step(self, step: int) -> None:
        """Show the shown tab `step` places before (< 0) or after, round the ends, skipping hidden tabs."""
        n = self.GetPageCount()
        if not n or len(self.hidden) >= n:
            return
        k = self.GetSelection()
        for _ in range(n):
            k = (k + (1 if step > 0 else -1)) % n
            if k not in self.hidden:
                break
        self.SetSelection(k)

    def SetSelection(self, page: int) -> int:
        """Show page `page` (sending the page-changed events, as wx.Notebook does) and mark its tab."""
        old = super().SetSelection(page)
        self.strip.Refresh()
        return old

    def ChangeSelection(self, page: int) -> int:
        """Show page `page` without events and mark its tab."""
        old = super().ChangeSelection(page)
        self.strip.Refresh()
        return old


class TabStrip(wx.Control):
    """The row of tabs over a TabBook: rounded-top tabs drawn with a GraphicsContext (anti-aliased), a gap and a
    thin divider between groups, a baseline under the row, count badges. A click or the arrow keys (with the focus)
    change the page; the window adds Ctrl+PgUp / Ctrl+PgDn. Widths come from the bold font for every tab, so the
    selected tab turning bold moves nothing."""

    PAD_X, PAD_Y, TOP, SPACE, GAP, RADIUS, BAR = 14, 7, 6, 3, 18, 7, 3
    BADGE_GAP, BADGE_PAD = 6, 5                 # between label and badge; the pill's padding beside its number

    def __init__(self, parent, book: TabBook):
        """The strip of `book`, in `parent`; it paints itself entirely (no background erase: no flicker)."""
        super().__init__(parent, style=wx.BORDER_NONE | wx.WANTS_CHARS | wx.FULL_REPAINT_ON_RESIZE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.book = book
        self._bold = self.GetFont().Bold()
        self._small = self.GetFont().Bold().Scaled(0.85)
        self._hover: Optional[int] = None
        self._widths: Optional[list] = None      # each tab's (label width, badge width) (changed() forgets them)
        for evt, fn in ((wx.EVT_PAINT, self._paint), (wx.EVT_LEFT_DOWN, self._left_down),
                        (wx.EVT_MOTION, self._motion), (wx.EVT_LEAVE_WINDOW, lambda e: self._set_hover(None)),
                        (wx.EVT_KEY_DOWN, self._key), (wx.EVT_SET_FOCUS, lambda e: self.Refresh()),
                        (wx.EVT_KILL_FOCUS, lambda e: self.Refresh()), (wx.EVT_MOUSE_CAPTURE_LOST, lambda e: None)):
            self.Bind(evt, fn)
        self.SetInitialSize()

    def changed(self) -> None:
        """A page or a badge was added or changed: measure the tabs again and redraw."""
        self._widths = None
        self.Refresh()

    def DoGetBestSize(self):
        """As tall as a tab with its padding, the top margin and the underline; as wide as the tabs."""
        h = self.GetCharHeight() + 2 * self.PAD_Y + self.TOP + self.BAR
        return wx.Size(sum(w for _, w in self.tab_rects()) or 200, h)

    def _measure(self) -> list:
        """Each tab's (label width in the bold font, badge pill width or 0), cached until something changes."""
        if self._widths is None:
            dc = wx.ClientDC(self)
            out = []
            for k in range(self.book.GetPageCount()):
                dc.SetFont(self._bold)
                lw = dc.GetTextExtent(self.book.GetPageText(k))[0]
                badge = self.book.badges.get(k)
                dc.SetFont(self._small)
                bw = dc.GetTextExtent(str(badge[0]))[0] + 2 * self.BADGE_PAD if badge else 0
                out.append((lw, max(bw, dc.GetCharHeight() + 2) if badge else 0))
            self._widths = out
        return self._widths

    def tab_rects(self) -> list:
        """(x, width) of every tab, left to right (a hidden one: width 0, nothing hits or draws it). When the window
        is too narrow for the natural widths, the padding shrinks first (to 5 px a side); a tab's label is then
        clipped to its tab."""
        widths = [lw + (bw + self.BADGE_GAP if bw else 0) for lw, bw in self._measure()]
        shown = [w for k, w in enumerate(widths) if k not in self.book.hidden]
        gaps = len(self.book.gaps) * self.GAP + self.SPACE * max(len(shown) - 1, 0) + 8
        room = self.GetClientSize().width
        pad = self.PAD_X
        if room > 0 and shown and sum(shown) + 2 * pad * len(shown) + gaps > room:
            pad = max(5, (room - gaps - sum(shown)) // (2 * len(shown)))
        x, out = 8, []
        for k, w in enumerate(widths):
            if k in self.book.hidden:
                out.append((x, 0))
                continue
            if k in self.book.gaps:
                x += self.GAP - self.SPACE
            out.append((x, w + 2 * pad))
            x += w + 2 * pad + self.SPACE
        return out

    def hit(self, x: int) -> Optional[int]:
        """The tab at x (pixels from the left), or None."""
        for k, (x0, w) in enumerate(self.tab_rects()):
            if x0 <= x < x0 + w:
                return k
        return None

    def _set_hover(self, k: Optional[int]) -> None:
        """Mark tab k as under the mouse (None: none), with its badge's tooltip, and redraw when that changed."""
        if k != self._hover:
            self._hover = k
            self.SetCursor(wx.Cursor(wx.CURSOR_HAND if k is not None else wx.CURSOR_DEFAULT))
            tip = self.book.badges.get(k, (0, ""))[1] if k is not None else ""
            if tip:
                self.SetToolTip(tip)
            else:
                self.UnsetToolTip()
            self.Refresh()

    def _motion(self, evt):
        """The mouse moved: the tab under it lights up."""
        self._set_hover(self.hit(evt.GetX()))

    def _left_down(self, evt):
        """A click on a tab shows its page."""
        k = self.hit(evt.GetX())
        if k is not None and k != self.book.GetSelection():
            self.book.SetSelection(k)

    def _key(self, evt):
        """Left / Right (with the focus on the strip): the page before / after; other keys go on."""
        step = {wx.WXK_LEFT: -1, wx.WXK_RIGHT: 1}.get(evt.GetKeyCode())
        if step is None:
            evt.Skip()
            return
        self.book.step(step)

    def _paint(self, evt):
        """Draw the strip: the window background, a baseline, every tab (fill with rounded top corners, label,
        badge, the selected tab's underline), the divider in each group gap and a focus ring on the selected tab.
        A tab whose badge is 0 has no fill unless selected, only a dashed outline in its colour."""
        dc = wx.AutoBufferedPaintDC(self)
        pal = theme.palette()
        dc.SetBackground(wx.Brush(theme.colour(pal.bg)))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        w, h = self.GetClientSize()
        top, bottom, r = self.TOP, h - self.BAR + 1, self.RADIUS
        gc.SetPen(wx.Pen(theme.colour(pal.border), 1))
        gc.StrokeLine(0, h - 0.5, w, h - 0.5)
        sel = self.book.GetSelection()
        sizes = self._measure()
        for k, (x, tw) in enumerate(self.tab_rects()):
            if tw == 0:                                  # hidden
                continue
            if k in self.book.gaps:                      # a short divider in the middle of the gap
                gc.SetPen(wx.Pen(theme.colour(pal.border), 1))
                xd = x - self.GAP / 2 + 0.5
                gc.StrokeLine(xd, top + 5, xd, bottom - 4)
            hue = self.book.hues[k]
            badge = self.book.badges.get(k)
            empty = badge is not None and badge[0] == 0
            fill, text, bar = tab_colours(hue, k == sel, k == self._hover, pal)
            if empty and k != sel:
                fill, text = (pal.btn_hover if k == self._hover else None), pal.muted
            gc.PushState()
            gc.Clip(x, 0, tw, h)
            if fill is not None:
                # a rounded rectangle running below the strip's bottom: only its top corners are round
                gc.SetBrush(wx.Brush(theme.colour(fill)))
                gc.SetPen(wx.Pen(theme.colour(pal.border), 1) if fill == pal.card else wx.TRANSPARENT_PEN)
                gc.DrawRoundedRectangle(x + 0.5, top + 0.5, tw - 1, h - top + r, r)
            if empty and k != sel and hue is not None:  # hollow: the tab's outline dashed in a soft shade of its hue
                gc.SetBrush(wx.TRANSPARENT_BRUSH)
                gc.SetPen(wx.Pen(theme.colour(mix(hue, pal.bg, 0.55)), 1, wx.PENSTYLE_SHORT_DASH))
                gc.DrawRoundedRectangle(x + 0.5, top + 0.5, tw - 1, h - top + r, r)
            if bar is not None:
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.SetBrush(wx.Brush(theme.colour(bar)))
                gc.DrawRectangle(x, h - self.BAR, tw, self.BAR)
            font = self._bold if k == sel else self.GetFont()
            gc.SetFont(font, theme.colour(text))
            label = self.book.GetPageText(k)
            lw, lh = gc.GetTextExtent(label)[:2]
            bw = sizes[k][1]
            content = lw + (self.BADGE_GAP + bw if bw else 0)
            x0 = x + max((tw - content) / 2, 4)
            gc.DrawText(label, x0, top + (bottom - top - lh) / 2)
            if badge is not None:                        # the count in a pill right of the label
                pill, ink = badge_colours(hue, badge[0], pal)
                gc.SetFont(self._small, theme.colour(ink))
                nw, nh = gc.GetTextExtent(str(badge[0]))[:2]
                ph = nh + 2
                px, py = x0 + lw + self.BADGE_GAP, top + (bottom - top - ph) / 2
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.SetBrush(wx.Brush(theme.colour(pill)))
                gc.DrawRoundedRectangle(px, py, bw, ph, ph / 2)
                gc.DrawText(str(badge[0]), px + (bw - nw) / 2, py + 1)
            if k == sel and self.HasFocus():
                gc.SetBrush(wx.TRANSPARENT_BRUSH)
                gc.SetPen(wx.Pen(theme.colour(pal.accent, 150), 1))
                gc.DrawRoundedRectangle(x + 3.5, top + 3.5, tw - 7, bottom - top - 6, r - 3)
            gc.PopState()
