"""Small widgets and helpers shared by the tabs (and kept cheap for the GUI thread)."""
from __future__ import annotations

from typing import Callable, Optional

import wx

from ..components.values import parse_value


CLIPBOARD_TRIES = 3          # another program holding the clipboard (a clipboard manager, Remote Desktop) lets
CLIPBOARD_WAIT_S = 0.03      # go within milliseconds: a few short tries, at most ~0.1 s on the GUI thread


def _clipboard(use: Callable[[], bool]) -> bool:
    """Run `use` (a SetData or GetData) with the clipboard open, a few times while another program holds it; True
    when it worked. wx's own error box is silenced (wx.LogNull): on Windows Open() succeeds and only SetData /
    GetData find the clipboard busy ("Failed to put data on the clipboard (error -2147221040: OpenClipboard
    Failed)"), a modal box the user saw during a test run, 2026-11-03; the caller says so its own way instead."""
    import time
    for k in range(CLIPBOARD_TRIES):
        if k:
            time.sleep(CLIPBOARD_WAIT_S)
        with wx.LogNull():
            if not wx.TheClipboard.Open():
                continue
            try:
                if use():
                    return True
            finally:
                wx.TheClipboard.Close()
    return False


def copy_to_clipboard(make: Callable[[], wx.DataObject]) -> bool:
    """Put the data make() gives (a TextDataObject, a BitmapDataObject) on the clipboard; False when it stayed busy
    (no error box: the caller tells the user, "Clipboard busy"). A new object for every try: the clipboard takes
    the one it is given, and may delete it when SetData fails."""
    return _clipboard(lambda: bool(wx.TheClipboard.SetData(make())))


def copy_text(text: str) -> bool:
    """Plain text onto the clipboard (copy_to_clipboard)."""
    return copy_to_clipboard(lambda: wx.TextDataObject(text))


def read_clipboard(data: wx.DataObject) -> bool:
    """Fill `data` from the clipboard; False when it is busy or holds no such data (no error box)."""
    return _clipboard(lambda: bool(wx.TheClipboard.GetData(data)))


def set_lazy_items(combo: wx.ComboBox, items: list[str]):
    """Give an editable combo box a long list (a board's nets) that is filled in only when it drops down.
    Filling a native combo box costs ~0.6 ms per item on Windows: 70 ms per box for a board's 114 nets, and
    seconds on big boards, which would stall the GUI thread at every board load. Typing a value works without
    the list."""
    combo._lazy_items = list(items)
    if not getattr(combo, "_lazy_bound", False):
        combo.Bind(wx.EVT_COMBOBOX_DROPDOWN, _fill)
        combo._lazy_bound = True
    if combo.GetCount():                    # drop the previous board's list; keep what is typed
        value = combo.GetValue()
        combo.Clear()
        combo.ChangeValue(value)


MARKS = {"schematic": "◆", "parts": "◇", "differs": "!", "": ""}


class FieldMark(wx.StaticText):
    """The mark next to a part parameter's field: ◆ from the schematic fields, ◇ the same as the Parts tab's value,
    ! set here and different from its source (components/fields.stage_marks), with the explanation as its tooltip.
    With on_reset, a click on a mark whose source has another value calls it (the field takes that value back)."""
    def __init__(self, parent, on_reset: Optional[Callable[[], None]] = None):
        """An empty mark, one character wide (fixed size: a mark appearing does not shift the form)."""
        super().__init__(parent, label="", size=wx.Size(16, -1), style=wx.ALIGN_CENTRE_HORIZONTAL | wx.ST_NO_AUTORESIZE)
        self.on_reset = on_reset
        self.label = ""                 # the reset's text ("Reset to 42.6mΩ (L2's R / DCR on the Parts tab)")
        self.resettable = False
        self.Bind(wx.EVT_LEFT_UP, self._click)

    def show(self, state: str, tip: str, value=None, origin: str = "", label: str = ""):
        """Show the mark for `state` ("schematic", "parts", "differs" or "": none); "differs" in the warning
        colour. value / origin / label: what a reset takes (components/fields.Mark), offered while the field
        shows something else."""
        from . import theme
        self.SetLabel(MARKS.get(state, ""))
        self.SetForegroundColour(theme.colour(theme.palette().warn) if state == "differs" else
                                 theme.colour(theme.palette().text))
        self.SetToolTip(tip or "")
        self.label = label
        self.resettable = self.on_reset is not None and value is not None and state in ("differs", "")
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND if self.resettable and state else wx.CURSOR_DEFAULT))

    def _click(self, evt):
        """A click on the mark: the reset, when there is one to make."""
        evt.Skip()
        if self.resettable:
            self.on_reset()


def scroll_form(parent) -> wx.ScrolledWindow:
    """The window a tab's form is built in: it scrolls up and down when the tab is not tall enough, so the form's
    buttons below it always stay in reach (the user: "all settings panes need to be scrollable so if the screen is
    not big enough the user can still reach the buttons !!!"). fit_scroll_width sets its size once it is built."""
    fw = wx.ScrolledWindow(parent, style=wx.VSCROLL)
    fw.SetScrollRate(0, 12)
    return fw


def fit_scroll_width(fw: wx.ScrolledWindow, min_height: int = 120):
    """A built scroll_form's minimum size: the form's width plus the scroll bar's (a scrolled window asks for no
    width of its own, so a wide list beside it would squeeze it), and a short height, so the column it sits in
    can shrink to the tab and the buttons under it stay visible."""
    sizer = fw.GetSizer()
    width = sizer.GetMinSize().width if sizer is not None else -1
    fw.SetMinSize(wx.Size(width + wx.SystemSettings.GetMetric(wx.SYS_VSCROLL_X) if width > 0 else -1, min_height))


def section_form(vgap: int = 5) -> wx.GridBagSizer:
    """The grid a tab's form is laid out in by section_rows: label | field, the sections' titles across both."""
    return wx.GridBagSizer(vgap=vgap, hgap=8)


def section_rows(parent, form: wx.GridBagSizer, sections: list, tips: dict, cells: Optional[dict] = None,
                 start: int = 0) -> int:
    """A tab's form in sections (the user, of the Converters tab: "this properties pane is very complex and
    overwhelming, can you group it better using headings etc. Make it more glancable/self explanatory", then "do
    the same menu structering for the other tabs"): per section (title, note, [(label, field)]) a line above it
    (not the first), its title in bold, its note muted (a one-liner saying what the section is for), then its
    fields' labels, indented, each label and field with the field's tooltip from `tips` (hovering either explains
    it). `cells`: what goes into the grid in place of a field (its sizer with a ◆ mark). A note may be a window
    of its own (a note with an info button). The titles are named "section" and the notes "muted"
    (theme.style_tree colours those), so a lookup of the form's labels can leave them out. `start`: the grid row to
    begin at (a form built in several steps); returns the row after the last."""
    cells = cells or {}
    r = start
    for title, note, fields in sections:
        if r:                                    # a line between sections
            form.Add(wx.StaticLine(parent), (r, 0), (1, 2), wx.EXPAND | wx.TOP, 8)
            r += 1
        head = wx.StaticText(parent, label=title, name="section")
        head.SetFont(head.GetFont().Bold().Scaled(1.1))
        if tips.get(title):                      # a section whose title has a tooltip of its own (a bridge's Sweep)
            head.SetToolTip(tips[title])
        form.Add(head, (r, 0), (1, 2))
        r += 1
        if note:
            form.Add(note if isinstance(note, wx.Window) else wx.StaticText(parent, label=note, name="muted"),
                     (r, 0), (1, 2), wx.BOTTOM, 2)
            r += 1
        for label, ctrl in fields:
            text = wx.StaticText(parent, label=label)
            tip = tips.get(label, "")
            if tip:
                text.SetToolTip(tip)
                ctrl.SetToolTip(tip)
            # a list or a multi-line text: its label at its top
            tall = isinstance(ctrl, wx.ListBox) or (isinstance(ctrl, wx.TextCtrl) and ctrl.IsMultiLine())
            form.Add(text, (r, 0), flag=(wx.ALIGN_TOP if tall else wx.ALIGN_CENTER_VERTICAL) | wx.LEFT,
                     border=10)
            form.Add(cells.get(ctrl, ctrl), (r, 1), flag=wx.EXPAND)
            r += 1
    if r > start and not form.IsColGrowable(1):
        form.AddGrowableCol(1)                   # after the rows: a GridBagSizer has no columns before them
    return r


def with_mark(parent, ctrl, on_reset: Optional[Callable[[], None]] = None) -> tuple[wx.BoxSizer, FieldMark]:
    """A form field with its FieldMark to the right. With on_reset, the field's right-click menu offers the reset
    while the mark has one (the mark's label); otherwise the field keeps its own menu (cut, copy, paste)."""
    mark = FieldMark(parent, on_reset)
    row = wx.BoxSizer(wx.HORIZONTAL)
    row.Add(ctrl, 1, wx.EXPAND)
    row.Add(mark, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 3)
    if on_reset is not None:
        def menu(evt):
            """The field's right-click menu: the reset, when its mark offers one."""
            if not mark.resettable:
                evt.Skip()
                return
            m = wx.Menu()
            item = m.Append(wx.ID_ANY, mark.label)
            ctrl.Bind(wx.EVT_MENU, lambda e: on_reset(), item)
            ctrl.PopupMenu(m)
            m.Destroy()
        ctrl.Bind(wx.EVT_CONTEXT_MENU, menu)
    return row, mark


def form_state(win) -> dict:
    """What every text field and list in a form window shows now ({control: text or selection}): kept over a
    rebuild of the form from the settings (a reset of one field keeps what is typed in the others)."""
    out = {}
    for w in win.GetChildren():
        if isinstance(w, (wx.TextCtrl, wx.ComboBox)):
            out[w] = w.GetValue()
        elif isinstance(w, wx.Choice):
            out[w] = w.GetSelection()
        out.update(form_state(w))
    return out


def restore_form(state: dict, skip=()):
    """Put back what form_state saw, except in the controls `skip` (without text events: the caller updates
    what depends on them)."""
    for w, v in state.items():
        if w in skip or not w:
            continue
        if isinstance(w, wx.Choice):
            if 0 <= v < w.GetCount():
                w.SetSelection(v)
        elif w.GetValue() != v:
            w.ChangeValue(v)


def parse_field(text: str, unit: str = "") -> Optional[float]:
    """A form field's value: "500kHz" with unit "Hz" is 500e3; empty is None."""
    t = text.strip()
    if not t:
        return None
    if unit and t.lower().endswith(unit.lower()):
        t = t[: -len(unit)]
    return parse_value(t)


def source_status(source: str, reviewed: bool) -> str:
    """A settings entry's Status column: detected and not yet checked shows in capitals."""
    if source == "auto":
        return "auto, reviewed" if reviewed else "auto, REVIEW"
    return "manual"


def button_row(parent, buttons: list[tuple[str, Callable]], tips: dict[str, str]) -> wx.WrapSizer:
    """A row of buttons (label, handler) that wraps when the window is narrow, each with its tooltip."""
    from . import theme
    row = wx.WrapSizer(wx.HORIZONTAL, wx.REMOVE_LEADING_SPACES)       # no stretched last button
    for label, fn in buttons:
        b = theme.Button(parent, label)
        b.Bind(wx.EVT_BUTTON, fn)
        b.SetToolTip(tips.get(label, ""))
        row.Add(b, 0, wx.RIGHT | wx.BOTTOM, 6)
    return row


class PickDialog(wx.Dialog):
    """Pick which detected entries to add: one row per new entry with a tick box (the likely ones ticked) and the
    reasons in the last column; `selected()` gives the ticked entries' specs."""

    def __init__(self, parent, title: str, columns: list[tuple[str, int]], found: list, new: list,
                 row: Callable[[object], list[str]], ticked: Callable[[object], bool], note: str,
                 size: tuple[int, int] = (1050, 420)):
        """found: everything detection found; new: those not yet in the settings (the rows). row(d) gives a
        row's cells, ticked(d) whether it starts ticked; note: a line under the count of new suggestions."""
        super().__init__(parent, title=title, size=size, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.found = new
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.EnableCheckBoxes()
        for i, (c, w) in enumerate(columns):
            self.list.InsertColumn(i, c, width=w)
        for k, d in enumerate(new):
            first, *rest = row(d)
            item = self.list.InsertItem(k, first)
            for col, v in enumerate(rest, 1):
                self.list.SetItem(item, col, v)
            self.list.CheckItem(item, ticked(d))
        text = wx.StaticText(self, label=f"{len(new)} new suggestion(s) ({len(found) - len(new)} already listed). "
                                         + note)
        text.Wrap(size[0] - 50)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(text, 0, wx.ALL, 8)
        s.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        s.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(s)

    def selected(self) -> list:
        """The ticked rows' specs (what to add to the settings)."""
        return [d.spec for k, d in enumerate(self.found) if self.list.IsItemChecked(k)]


class InfoBox(wx.Panel):
    """A tab's help, out of the way: one line "ⓘ <title>  <this board's status>  [Show]", and under it, while
    shown, a read-only text (selectable, so a label can be copied from it). Cheap to build: two labels, a button
    and a text control that stays hidden until asked for."""

    def __init__(self, parent, title: str, text: str, lines: int = 12):
        """The box with its help `text` (hidden), `lines` lines tall when shown."""
        super().__init__(parent)
        from . import theme
        self.text = text
        self.head = wx.StaticText(self, label="ⓘ " + title)
        self.status = wx.StaticText(self, label="", style=wx.ST_ELLIPSIZE_END, name="muted")
        self.button = theme.Button(self, "Show")
        self.button.SetToolTip("Show or hide how this tab's parts are found and how to label them in the schematic")
        self.button.Bind(wx.EVT_BUTTON, self.toggle)
        self.body = wx.TextCtrl(self, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP)
        self.body.SetMinSize(wx.Size(-1, lines * self.GetCharHeight()))
        self.body.Hide()
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.head, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        row.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        row.Add(self.button, 0, wx.ALIGN_CENTER_VERTICAL)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(row, 0, wx.EXPAND)
        s.Add(self.body, 1, wx.EXPAND | wx.TOP, 4)
        self.SetSizer(s)

    def set_status(self, line: str):
        """This board's part of the help (its labels and their problems): on the line, and on top of the text."""
        self.status.SetLabel(line)
        self.status.SetToolTip(line)
        self.body.ChangeValue((line + "\n\n" if line else "") + self.text)

    def shown(self) -> bool:
        """The help text is shown."""
        return self.body.IsShown()

    def toggle(self, _=None):
        """Show or hide the help text, and lay out the tab again (the text takes or gives back its space)."""
        self.body.Show(not self.body.IsShown())
        self.button.SetLabel("Hide" if self.body.IsShown() else "Show")
        top = self.GetParent()
        top.Layout()
        top.Refresh()


def _fill(evt):
    """A lazy combo box is being opened: put its items in now (see set_lazy_items), keeping the typed value. Filling
    combos only when opened keeps building a tab with many of them fast."""
    combo = evt.GetEventObject()
    items = combo._lazy_items
    if combo.GetCount() != len(items):
        value = combo.GetValue()
        combo.Freeze()                      # one redraw instead of one per item (2.4x faster)
        try:
            combo.Set(items)
        finally:
            combo.Thaw()
        combo.ChangeValue(value)
    evt.Skip()
