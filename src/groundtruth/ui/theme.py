"""The plugin window's look, after PCB Thermal: a dark and a light palette, following the Windows app setting
unless the user picks one (Theme in the top bar, stored in %LOCALAPPDATA%\\GroundTruth\\ui.json).

wxWidgets 3.3 draws the native controls dark itself (wx.App.SetAppearance); this module adds the palette on top
(panels, cards, lists, inputs), buttons of its own with some volume (KiCad's flat native buttons look dated), KPI
tiles, and the matching matplotlib colours. The appearance can only be set before the first window exists, so a
new choice applies the next time the window opens.

No wx objects at import time: the start-up worker imports this module (colours are hex strings until used)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import wx

THEMES = ("system", "light", "dark")


@dataclass(frozen=True)
class Palette:
    """One theme's colours, as hex strings (turned into wx.Colour only when drawn: colour())."""
    dark: bool
    bg: str             # window background
    panel: str          # top bar, headers
    card: str           # cards, lists, tiles
    border: str
    text: str
    muted: str
    accent: str
    accent_dim: str
    warn: str
    error: str
    ok: str
    input: str          # text fields
    shadow: str         # button shadow (alpha applied when drawn)
    btn_top: str        # button gradient
    btn_bottom: str
    btn_hover: str
    btn_pressed: str


DARK = Palette(True, "#14161b", "#1b1e24", "#21252d", "#2e333d", "#e7e9ee", "#9aa3b2", "#4cc2ff", "#2b6f94",
               "#ffcc66", "#ff6b6b", "#7bd88f", "#171a20", "#000000", "#2c313b", "#22262e", "#323844", "#1c2027")
LIGHT = Palette(False, "#f3f5f8", "#e9edf2", "#ffffff", "#d3d9e2", "#1f2328", "#5f6b7a", "#0a84c6", "#a9d8f2",
                "#b7791f", "#d64545", "#2f9e57", "#ffffff", "#5a6b80", "#ffffff", "#eef1f5", "#f6f9fc", "#e2e7ee")

_current: Palette = LIGHT


def pref_path() -> Path:
    """The per-computer preferences file: %LOCALAPPDATA%\\GroundTruth\\ui.json on Windows, ~/.cache elsewhere."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "GroundTruth" / "ui.json"
    return Path.home() / ".cache" / "groundtruth" / "ui.json"


def prefs() -> dict:
    """The user's window preferences (every board): the theme, how many vias the maps mark."""
    try:
        d = json.loads(pref_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_prefs(**values) -> None:
    """Best effort, merged with the others: a preference that cannot be written only means the default next
    time."""
    try:
        p = pref_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**prefs(), **values}), encoding="utf-8")
    except OSError:
        pass


def load_pref() -> str:
    """The saved theme choice (an unknown value counts as "system")."""
    v = prefs().get("theme", "system")
    return v if v in THEMES else "system"


def save_pref(theme: str) -> None:
    """Store the theme choice (used the next time the window opens)."""
    if theme in THEMES:
        save_prefs(theme=theme)


def init_app(app: wx.App, theme: Optional[str] = None) -> Palette:
    """Call once, after creating the wx.App and before the first window: sets the native appearance and picks
    the palette. `theme`: system | light | dark (default: the saved preference)."""
    global _current
    theme = theme or load_pref()
    appearance = {"system": wx.App.Appearance.System, "light": wx.App.Appearance.Light,
                  "dark": wx.App.Appearance.Dark}[theme]
    try:
        app.SetAppearance(appearance)
    except Exception:                    # wxWidgets before 3.3: native controls stay as they are
        pass
    if theme == "system":
        try:
            dark = wx.SystemSettings.GetAppearance().IsDark()
        except Exception:
            dark = False
    else:
        dark = theme == "dark"
    _current = DARK if dark else LIGHT
    return _current


def palette() -> Palette:
    """The palette in use."""
    return _current


def colour(hex_: str, alpha: int = 255) -> wx.Colour:
    """A "#rrggbb" string as a wx.Colour, with transparency `alpha` (0..255)."""
    h = hex_.lstrip("#")
    return wx.Colour(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def rgb(hex_: str) -> tuple:
    """(r, g, b) 0..1 for matplotlib."""
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


_icons = None


def app_icons() -> "wx.IconBundle":
    """GroundTruth's logo (the toolbar button's: a ground symbol in a current loop on a navy badge) as the windows'
    icon, at the sizes Windows picks from for the title bar, taskbar and Alt+Tab (ui/icons, drawn by
    tools/make_icons.py). Made once; an empty bundle when the files are missing (the window keeps wx's default)."""
    global _icons
    if _icons is None:
        _icons = wx.IconBundle()
        for png in sorted((Path(__file__).with_name("icons")).glob("groundtruth-*.png")):
            icon = wx.Icon(str(png), wx.BITMAP_TYPE_PNG)
            if icon.IsOk():
                _icons.AddIcon(icon)
    return _icons


def set_icon(win) -> None:
    """Give a top-level window GroundTruth's icon (app_icons)."""
    icons = app_icons()
    if not icons.IsEmpty():
        win.SetIcons(icons)


def status_colour(status: str) -> str:
    """The colour of a status (ok, warn, error); the text colour for anything else."""
    p = _current
    return {"ok": p.ok, "warn": p.warn, "error": p.error}.get(status, p.text)


# --------------------------------------------------------------------------------------------- widgets
class Button(wx.Control):
    """A push button drawn with some volume: rounded body with a soft vertical gradient and a shadow line, a
    border that lights up on hover, pressed and disabled states. kind: default | primary (accent, the main
    action) | danger. Sends wx.EVT_BUTTON like wx.Button; Space or Enter clicks it when it has the focus."""

    RADIUS = 7

    def __init__(self, parent, label: str = "", kind: str = "default", id=wx.ID_ANY, size=wx.DefaultSize):
        """A button with `label` (primary: bold). It paints itself entirely (BG_STYLE_PAINT, no background erase:
        no flicker) and follows the mouse and the keyboard focus to draw its states."""
        super().__init__(parent, id, size=size, style=wx.BORDER_NONE | wx.WANTS_CHARS | wx.FULL_REPAINT_ON_RESIZE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._label, self.kind = label, kind
        self.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), self.Refresh()))       # a resized button draws itself anew
        self._hover = self._down = False
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        f = self.GetFont()
        if kind == "primary":
            f = f.Bold()
        self.SetFont(f)
        for evt, fn in ((wx.EVT_PAINT, self._paint), (wx.EVT_ENTER_WINDOW, lambda e: self._set(hover=True)),
                        (wx.EVT_LEAVE_WINDOW, lambda e: self._set(hover=False, down=False)),
                        (wx.EVT_LEFT_DOWN, self._left_down), (wx.EVT_LEFT_UP, self._left_up),
                        (wx.EVT_LEFT_DCLICK, self._left_down), (wx.EVT_MOUSE_CAPTURE_LOST, lambda e: None),
                        (wx.EVT_KEY_DOWN, self._key), (wx.EVT_SET_FOCUS, lambda e: self.Refresh()),
                        (wx.EVT_KILL_FOCUS, lambda e: self.Refresh())):
            self.Bind(evt, fn)
        self.SetInitialSize(size)

    # wx.Button-like API
    def GetLabel(self) -> str:
        """The button's text."""
        return self._label

    def SetLabel(self, label: str):
        """New text: the button takes the size it needs and draws again."""
        self._label = label
        self.InvalidateBestSize()
        self.SetInitialSize()
        self.Refresh()

    def Enable(self, enable: bool = True) -> bool:
        """Enable or disable (greyed), drawing the new state."""
        changed = super().Enable(enable)
        self.Refresh()
        return changed

    def AcceptsFocusFromKeyboard(self) -> bool:
        """Tab reaches the button only while it is enabled (as with wx.Button)."""
        return self.IsEnabled()

    def DoGetBestSize(self):
        """The label's size plus padding, at least 30 px tall."""
        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        w, h = dc.GetTextExtent(self._label or "M")
        return wx.Size(w + 28, max(h + 14, 30))

    def click(self):
        """Send the button event, as a mouse click does."""
        if not self.IsEnabled():
            return
        evt = wx.CommandEvent(wx.wxEVT_BUTTON, self.GetId())
        evt.SetEventObject(self)
        self.GetEventHandler().ProcessEvent(evt)

    def _set(self, hover=None, down=None):
        """Change the hover / pressed state and draw it."""
        if hover is not None:
            self._hover = hover
        if down is not None:
            self._down = down
        self.Refresh()

    def _left_down(self, evt):
        """Pressed: draw it pressed and capture the mouse, so the release is seen even outside the button."""
        if self.IsEnabled():
            self._set(down=True)
            self.SetFocus()
            if not self.HasCapture():
                self.CaptureMouse()

    def _left_up(self, evt):
        """Released: a click only when it was pressed here and is released over the button (as native buttons:
        dragging off cancels it)."""
        was = self._down
        if self.HasCapture():
            self.ReleaseMouse()
        self._set(down=False)
        if was and self.GetClientRect().Contains(evt.GetPosition()):
            self.click()

    def _key(self, evt):
        """Space or Enter clicks; other keys go on (Tab moves the focus)."""
        if evt.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.click()
        else:
            evt.Skip()

    def _colours(self):
        """(gradient top, gradient bottom, border, text) for the button's kind and state (disabled, pressed,
        hover, normal)."""
        p, on = _current, self.IsEnabled()
        if self.kind == "primary":
            if not on:
                return ("#2a3a45", "#2a3a45", "#2a3a45", "#6b7a86") if p.dark else \
                    ("#c9dbe7", "#c9dbe7", "#c9dbe7", "#f4f8fb")
            top, bottom = ("#7ad3ff", "#4cc2ff") if p.dark else ("#2aa5ea", "#0a84c6")
            if self._down:
                top = bottom
            elif self._hover:
                top, bottom = ("#9be0ff", "#62caff") if p.dark else ("#45b3f0", "#1593d8")
            return top, bottom, bottom, "#0b1a24" if p.dark else "#ffffff"
        if self.kind == "danger":
            base = ("#4a232a", "#3a1f24", "#6b2d36", "#ffb3b3") if p.dark else ("#fff5f5", "#fde8e8", "#e3a4a4",
                                                                                  "#b42318")
            if not on:
                return p.btn_bottom, p.btn_bottom, p.border, p.muted
            return base
        if not on:
            return p.btn_bottom, p.btn_bottom, p.border, p.muted
        if self._down:
            return p.btn_pressed, p.btn_pressed, p.accent_dim, p.text
        if self._hover:
            return p.btn_hover, p.btn_bottom, p.accent_dim if p.dark else p.accent, p.text
        return p.btn_top, p.btn_bottom, p.border, p.text

    def _paint(self, evt):
        """Draw the button with a GraphicsContext (anti-aliased): the parent's background, a shadow, the rounded
        gradient body (moved down 1 px while pressed), a highlight line, a focus ring and the centred label."""
        dc = wx.AutoBufferedPaintDC(self)
        bg = self.GetParent().GetBackgroundColour()
        dc.SetBackground(wx.Brush(bg))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        w, h = self.GetClientSize()
        r = self.RADIUS
        top, bottom, border, text = self._colours()
        y_off = 1 if self._down else 0
        if self.IsEnabled() and not self._down:             # the shadow line under the body gives it volume
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(colour(_current.shadow, 70 if _current.dark else 40)))
            gc.DrawRoundedRectangle(1, 2, w - 2, h - 3, r)
        gc.SetBrush(gc.CreateLinearGradientBrush(0, 0, 0, h, colour(top), colour(bottom)))
        gc.SetPen(wx.Pen(colour(border), 1))
        gc.DrawRoundedRectangle(0.5, 0.5 + y_off, w - 1.5, h - 3, r)
        if self.IsEnabled() and not self._down and self.kind == "default":    # a top highlight
            gc.SetPen(wx.Pen(colour("#ffffff", 22 if _current.dark else 160), 1))
            gc.StrokeLine(r, 1.5 + y_off, w - r, 1.5 + y_off)
        if self.HasFocus() and self.IsEnabled():
            gc.SetBrush(wx.TRANSPARENT_BRUSH)
            gc.SetPen(wx.Pen(colour(_current.accent, 150), 1))
            gc.DrawRoundedRectangle(2.5, 2.5 + y_off, w - 5.5, h - 7, r - 2)
        gc.SetFont(self.GetFont(), colour(text))
        tw, th = gc.GetTextExtent(self._label)[:2]
        gc.DrawText(self._label, (w - tw) / 2, (h - 2 - th) / 2 + y_off)


class Tile(wx.Control):
    """A KPI tile: muted title, big value (coloured by status: ok | warn | error | ''), muted sub-line."""

    def __init__(self, parent, title: str, value: str, sub: str = "", status: str = "", tip: str = ""):
        """A tile showing these figures (see kpi.py for what they are)."""
        super().__init__(parent, style=wx.BORDER_NONE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.title, self.value, self.sub, self.status = title, value, sub, status
        self._big = self.GetFont().Bold().Scaled(1.6)
        self._small = self.GetFont().Scaled(0.92)
        self.Bind(wx.EVT_PAINT, self._paint)
        self._tip = tip
        if tip:
            self.SetToolTip(tip)
        self._click = None
        self.Bind(wx.EVT_LEFT_UP, lambda e: self._click() if self._click is not None else e.Skip())
        self.SetInitialSize()

    def set_click(self, callback) -> None:
        """Make the tile a button (callback() on a left click, a hand cursor), or plain again with None."""
        self._click = callback
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND if callback is not None else wx.CURSOR_DEFAULT))

    def update(self, title: str, value: str, sub: str = "", status: str = "", tip: str = "") -> bool:
        """Show other figures in this tile (the Results tab reuses its tiles: creating windows on the GUI thread
        is what took the time). True when its size may have changed."""
        changed = (title, value, sub, status) != (self.title, self.value, self.sub, self.status)
        if changed:
            self.title, self.value, self.sub, self.status = title, value, sub, status
            self.InvalidateBestSize()
            self.SetMinSize(self.GetBestSize())
            self.Refresh()
        if tip != self._tip:
            self._tip = tip
            if tip:
                self.SetToolTip(tip)
            else:
                self.UnsetToolTip()
        return changed

    def DoGetBestSize(self):
        """Wide enough for the widest of its three lines (at least 90 px plus padding), tall enough for them."""
        dc = wx.ClientDC(self)
        dc.SetFont(self._small)
        w1, h1 = dc.GetTextExtent(self.title)
        w3, _ = dc.GetTextExtent(self.sub) if self.sub else (0, 0)
        dc.SetFont(self._big)
        w2, h2 = dc.GetTextExtent(self.value)
        return wx.Size(max(w1, w2, w3, 90) + 28, h1 * (2 if self.sub else 1) + h2 + 26)

    def _paint(self, evt):
        """Draw the card (rounded, bordered), the status stripe and the three lines."""
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        p = _current
        w, h = self.GetClientSize()
        gc.SetBrush(wx.Brush(colour(p.card)))
        gc.SetPen(wx.Pen(colour(p.border), 1))
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, 10)
        if self.status:                                       # a status stripe on the left edge
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(colour(status_colour(self.status))))
            gc.DrawRoundedRectangle(3, 8, 3, h - 16, 1.5)
        gc.SetFont(self._small, colour(p.muted))
        y = 9
        gc.DrawText(self.title, 14, y)
        y += gc.GetTextExtent(self.title)[1] + 1
        gc.SetFont(self._big, colour(status_colour(self.status) if self.status else p.text))
        gc.DrawText(self.value, 14, y)
        y += gc.GetTextExtent(self.value)[1] + 1
        if self.sub:
            gc.SetFont(self._small, colour(p.muted))
            gc.DrawText(self.sub, 14, y)


# --------------------------------------------------------------------------------------------- styling
def style_tree(win: wx.Window, pal: Optional[Palette] = None, bg: Optional[str] = None) -> None:
    """Colour what native drawing cannot know about: labels named "muted" (hints, sub-lines). Everything else
    stays native on purpose: wx 3.3 already draws panels, lists, grids and inputs light or dark, and explicit
    colours cost a lot. A list with non-default colours (set on it, or inherited from a panel's foreground)
    switches Windows to custom drawing per cell: 150-290 ms for the Issues tab's first paint on refboard
    (gui_probe), against 0 ms native. Cheap (no layout); call after a tab is built."""
    p = pal or _current
    if isinstance(win, wx.StaticText) and win.GetName() == "muted":
        win.SetForegroundColour(colour(p.muted))
    for child in win.GetChildren():
        if not child.IsTopLevel():
            style_tree(child, p, bg)


def style_figure(fig, pal: Optional[Palette] = None) -> None:
    """matplotlib figure in the palette (text, axes, spines, grid, legends, colour bars). Thread-safe: touches
    only this figure, so the background renderer can call it."""
    p = pal or _current
    if not p.dark:
        return                       # light: matplotlib's own look already fits
    fg, muted, face, card, border = rgb(p.text), rgb(p.muted), rgb(p.bg), rgb(p.card), rgb(p.border)
    fig.set_facecolor(face)
    if fig._suptitle is not None:
        fig._suptitle.set_color(fg)
    for t in fig.texts:                  # the maps' note under the title
        t.set_color(fg)
    for leg in fig.legends:              # the maps' legend of the route's layers
        for t in leg.get_texts():
            t.set_color(fg)
    for ax in fig.axes:
        ax.set_facecolor(card)
        for title in (ax.title, ax._left_title, ax._right_title):     # the spectrum's title is on the left
            title.set_color(fg)
        ax.xaxis.label.set_color(muted)
        ax.yaxis.label.set_color(muted)
        ax.tick_params(colors=muted, which="both")
        for s in ax.spines.values():
            s.set_color(border)
        for line in ax.get_xgridlines() + ax.get_ygridlines():
            line.set_color(border)
        leg = ax.get_legend()
        if leg is not None:
            leg.get_frame().set_facecolor(card)
            leg.get_frame().set_edgecolor(border)
            for t in leg.get_texts():
                t.set_color(fg)
        for t in ax.texts:
            t.set_color(fg)
