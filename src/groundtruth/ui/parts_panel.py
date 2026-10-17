"""Parts tab: every footprint with its value and the electrical model used for it."""
from __future__ import annotations

import wx
import wx.grid as gridlib

from ..components.defaults import ref_prefix, suggest_model
from ..components.fields import MODEL_FIELDS, model_values, same
from ..components.models import MODEL_KINDS, MODEL_PARAMS, ComponentModel
from ..analysis.pins import format_values, gt_values, values_from_data
from ..components.values import format_si, parse_value
from ..config.settings import ConfigError
from . import theme

COLS = ["Ref", "Value", "Footprint", "Model", "R (Ω)", "C (F)", "C at bias (F)", "L (H)", "ESR (Ω)", "ESL (H)",
        "Pins", "Schematic", "Note"]
PINS_COL = COLS.index("Pins")             # a part's own through-hole pin values (analysis/pins.py)
PIN_HELP = ("Through-hole pins, comma separated: contact:0.5m (the mating contact, Ω per pin), joint:50u (the solder "
            "joint, Ω per pin), fill:75 (% of the hole soldered, from the side opposite the part), pin:brass (hole = "
            "copper filling the hole, copper, brass, bronze, becu, tellurium), pin_d:1.0 (mm), pin_r:0.1m (Ω through "
            "the board). Each value: the part's GT field, else its Pins cell, else Pin defaults…, else built in.")
PARAM_COL = {"R": 4, "C": 5, "C_bias": 6, "L": 7, "ESR": 8, "ESL": 9}
SCHEMATIC_COL = COLS.index("Schematic")    # ◆ the parameters taken from the symbol's fields, ! set here and different
NOTE_COL = COLS.index("Note")
# The table fills the tab (the user: "expand the table across the whole plane"): the width the columns do not need
# goes to the text columns, in these shares. A long note (Q601's suggested one, ~180 characters) would make the
# whole table scroll sideways, so the Note column starts at most NOTE_MAX wide; editing the cell shows all of it.
STRETCH = {"Value": 1.0, "Footprint": 2.0, "Schematic": 1.0, "Note": 3.0}
NOTE_MAX = 360
_DIFFERS = {True: "#4a3a1f", False: "#ffdcc8"}   # a parameter set here that differs from the schematic (dark, light)


class PartsPanel(wx.Panel):
    """The Parts tab: a grid of the settings' parts (one row each) where the model and its parameters are edited;
    a filter, and buttons to set the filtered parts' model at once or back to the suggested one. Every edit goes
    to the settings at once (on_change saves them)."""

    def __init__(self, parent, on_change):
        """The tab; on_change() is called after every edit of the settings."""
        super().__init__(parent)
        self.on_change = on_change
        self.board = None
        self.settings = None
        self.rows: list[str] = []

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(wx.StaticText(self, label="Filter:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.filter = wx.SearchCtrl(self, size=(180, -1))
        self.filter.SetDescriptiveText("ref, value or footprint")
        self.filter.Bind(wx.EVT_TEXT, lambda e: self.refresh())
        top.Add(self.filter, 0, wx.RIGHT, 12)
        top.Add(wx.StaticText(self, label="Set filtered parts to:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.bulk = wx.Choice(self, choices=list(MODEL_KINDS))
        self.bulk.SetSelection(0)
        top.Add(self.bulk, 0, wx.RIGHT, 4)
        b = theme.Button(self, "Apply")
        b.Bind(wx.EVT_BUTTON, self.on_bulk)
        top.Add(b, 0, wx.RIGHT, 12)
        r = theme.Button(self, "Reset filtered to suggested")
        r.Bind(wx.EVT_BUTTON, self.on_reset)
        top.Add(r, 0, wx.RIGHT, 12)
        pd = theme.Button(self, "Pin defaults…")
        pd.SetToolTip("the board's through-hole pin values, for every part without its own (GT field or Pins cell). "
                      + PIN_HELP)
        pd.Bind(wx.EVT_BUTTON, self.on_pin_defaults)
        top.Add(pd, 0)

        self.grid = gridlib.Grid(self)
        self.grid.CreateGrid(0, len(COLS))
        for i, c in enumerate(COLS):
            self.grid.SetColLabelValue(i, c)
        self.grid.SetRowLabelSize(0)
        self.grid.Bind(gridlib.EVT_GRID_CELL_CHANGED, self.on_cell)
        self.grid.Bind(wx.EVT_SIZE, self._grid_resized)
        self._natural: list[int] = []           # the columns' widths for their contents (refresh)

        hint = wx.StaticText(self, label="Values accept SI notation (33R, 4k7, 100n, 0.4n). 'open' parts are only "
                                         "ports; 'short' is a dead short. C at bias: a ceramic's capacitance at its "
                                         "DC voltage (maker's curve), used instead of C. Schematic: ◆ values from "
                                         "the symbol's fields, ! set here and different. Pins: a through-hole "
                                         "part's own pin values (contact:0.5m, joint:50u, fill:75, pin:brass; "
                                         "hover Pin defaults… for all). Edits are saved to the settings.")
        hint.SetName("muted")
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(top, 0, wx.ALL, 6)
        s.Add(self.grid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        s.Add(hint, 0, wx.ALL, 6)
        self.SetSizer(s)

    def load(self, board, settings):
        """Show this board's parts (its footprints give the schematic fields)."""
        self.board, self.settings = board, settings
        self.refresh()

    def _visible(self) -> list[str]:
        """The references the filter lets through: the text found in the reference, value or footprint, or equal
        to the reference's prefix ("R" gives every resistor)."""
        q = self.filter.GetValue().strip().lower()
        comps = self.settings.config.components
        refs = [r for r, e in comps.items() if not e.removed]
        if q:
            refs = [r for r in refs if q in r.lower() or q in comps[r].value.lower() or q in comps[r].footprint.lower()
                    or q == ref_prefix(r).lower()]
        return refs

    def refresh(self):
        """Rebuild the table (in one batch: one redraw), size the columns to their contents (the Note column at
        most NOTE_MAX) and spread the spare width (_fit_columns)."""
        if self.settings is None:
            return
        g = self.grid
        if g.IsCellEditControlEnabled():    # an open cell editor is closed (and its value kept) before its row goes
            g.DisableCellEditControl()
        g.BeginBatch()
        if g.GetNumberRows():
            g.DeleteRows(0, g.GetNumberRows())
        self.rows = self._visible()
        g.AppendRows(len(self.rows))
        for row, ref in enumerate(self.rows):
            self._fill_row(row, ref)
        g.AutoSizeColumns()
        note = COLS.index("Note")
        if g.GetColSize(note) > NOTE_MAX:
            g.SetColSize(note, NOTE_MAX)
        self._natural = [g.GetColSize(i) for i in range(len(COLS))]
        self._fit_columns()
        g.EndBatch()

    def _fill_row(self, row: int, ref: str, new: bool = True):
        """One part's row: values, which cells can be edited (the model's parameters), and the colours. new=False
        (an update in place) keeps the Model cell's editor: it may be the one just used."""
        g = self.grid
        e = self.settings.config.components[ref]
        m = e.model
        linked, differs = self._schematic(ref, m)
        marks = (["◆ " + ", ".join(linked)] if linked else []) + \
            [f"! {p}: schematic {format_si(r.value)}" for p, r in differs.items()]
        fp = self.board.footprint(ref) if self.board is not None else None
        gt_pins = gt_values(fp)[0] if fp is not None else {}
        if gt_pins:
            marks.append("◆ GT " + format_values(gt_pins))
        vals = [ref, e.value, e.footprint, m.kind] + [""] * len(PARAM_COL) + [format_values(e.tht), "; ".join(marks),
                                                                             m.note]
        for p, col in PARAM_COL.items():
            if p in m.params:
                vals[col] = format_si(m.params[p])
        for col, v in enumerate(vals):
            g.SetCellValue(row, col, str(v))
        if new:
            for col in (0, 1, 2, SCHEMATIC_COL):
                g.SetReadOnly(row, col)
            g.SetCellEditor(row, 3, gridlib.GridCellChoiceEditor(list(MODEL_KINDS)))
        normal = g.GetDefaultCellBackgroundColour()
        for p, col in PARAM_COL.items():
            g.SetReadOnly(row, col, p not in MODEL_PARAMS[m.kind])
            g.SetCellBackgroundColour(row, col, theme.colour(theme.palette().panel)
                                      if p not in MODEL_PARAMS[m.kind] else
                                      theme.colour(_DIFFERS[theme.palette().dark]) if p in differs else normal)
        g.SetCellBackgroundColour(row, 3, theme.colour("#4a3a1f" if theme.palette().dark else "#ffdcc8")
                                  if m.missing_params() else normal)
        tht = fp is not None and any(p.pad_type == "thru_hole" for p in fp.pads)
        g.SetReadOnly(row, PINS_COL, not tht and not e.tht)      # only a part with through-hole pins has any
        g.SetCellBackgroundColour(row, PINS_COL, normal if tht or e.tht else theme.colour(theme.palette().panel))

    def _schematic(self, ref: str, m: ComponentModel) -> tuple[list[str], dict]:
        """(the model parameters linked to the part's schematic fields, {parameter: Reading} set here and
        different from them)."""
        fp = self.board.footprint(ref) if self.board is not None else None
        if fp is None or m.kind not in MODEL_FIELDS:
            return [], {}
        values, _ = model_values(fp, m.kind)
        linked = [p for p in m.from_schematic if p in values]
        differs = {p: r for p, r in values.items() if p not in m.from_schematic and m.params.get(p) is not None
                   and not same(m.params[p], r.value)}
        return linked, differs

    def _update_row(self, ref: str):
        """After an edit: that part's row again, in place. Rebuilding the whole table (as before) destroyed the
        cell editor the user had already opened on the next cell, and wx's attempt to give it the focus back
        failed ("'SetFocus' failed with error 0x00000057", shown by KiCad as a plugin message)."""
        if self.settings is None or ref not in self.rows:
            return
        row = self.rows.index(ref)
        g = self.grid
        g.BeginBatch()
        self._fill_row(row, ref, new=False)
        g.EndBatch()
        g.ForceRefresh()

    def _grid_resized(self, evt):
        """The tab changed size: fit the columns again, after wx has resized the grid."""
        evt.Skip()
        wx.CallAfter(lambda: self and self._fit_columns())     # once the grid has its new client size

    def _fit_columns(self):
        """The columns' natural widths, the spare width of the tab spread over the text columns (STRETCH). Narrower
        than the columns need: natural widths, and the table scrolls sideways."""
        g = self.grid
        if len(self._natural) != g.GetNumberCols():
            return
        sizes = list(self._natural)
        spare = g.GetClientSize().width - g.GetRowLabelSize() - sum(sizes)
        if spare > 0:
            total = sum(STRETCH.values())
            for name, share in STRETCH.items():
                sizes[COLS.index(name)] += int(spare * share / total)
            sizes[COLS.index("Note")] += spare - sum(int(spare * s / total) for s in STRETCH.values())
        if sizes != [g.GetColSize(i) for i in range(len(sizes))]:
            g.BeginBatch()
            for i, w in enumerate(sizes):
                g.SetColSize(i, w)
            g.EndBatch()

    def select_refs(self, refs: list[str]) -> list[str]:
        """Select these parts' rows (the footprints selected in KiCad), clearing a filter that hides any of them,
        and put the cursor on the first one's Model cell, scrolled into view, ready to edit. Returns the
        references shown (parts the settings do not list are left out)."""
        if self.settings is None:
            return []
        comps = self.settings.config.components
        refs = [r for r in refs if r in comps and not comps[r].removed]
        if not refs:
            return []
        if any(r not in self.rows for r in refs):
            self.filter.ChangeValue("")
            self.refresh()
        g = self.grid
        g.ClearSelection()
        rows = [self.rows.index(r) for r in refs]
        g.GoToCell(rows[0], COLS.index("Model"))
        for row in rows:
            g.SelectRow(row, addToSelected=True)
        return refs

    def on_cell(self, evt):
        """A cell was edited: a new model kind (keeping the parameters and schematic links it still has), the
        note, or a parameter (SI notation; an unreadable value is refused and the old one shown again). The row
        is updated after the event, in place."""
        row, col = evt.GetRow(), evt.GetCol()
        ref = self.rows[row]
        model = self.settings.config.components[ref].model
        text = self.grid.GetCellValue(row, col).strip()
        if col == 3:                    # the pins a model joins (a sensor's IP+ / IP-) stay with the part
            params = {k: v for k, v in model.params.items() if k in MODEL_PARAMS.get(text, ())}
            new = ComponentModel(text, params, "", list(model.pins),
                                 [k for k in model.from_schematic if k in MODEL_PARAMS.get(text, ())])
        elif col == PINS_COL:
            self._set_pins(ref, text)
            return
        elif col == NOTE_COL:
            new = ComponentModel(model.kind, dict(model.params), text, list(model.pins), list(model.from_schematic))
        else:
            name = next(p for p, c in PARAM_COL.items() if c == col)
            val = parse_value(text)
            if val is None and text:
                wx.MessageBox(f"Cannot read {text!r} as a number", "groundtruth", wx.ICON_WARNING)
                wx.CallAfter(lambda: self and self._update_row(ref))     # the old value back
                return
            params = dict(model.params)
            if val is None:
                params.pop(name, None)
            else:
                params[name] = val
            # an edited parameter is the user's, unless it equals what the schematic field says
            fp = self.board.footprint(ref) if self.board is not None else None
            field = model_values(fp, model.kind)[0].get(name) if fp is not None else None
            linked = [k for k in model.from_schematic if k != name]
            if field is not None and same(val, field.value):
                linked.append(name)
            new = ComponentModel(model.kind, params, model.note, list(model.pins), linked)
        if self._set(ref, new):
            self.on_change()
        wx.CallAfter(lambda: self and self._update_row(ref))     # the stored model (the old one if refused)

    def _set(self, ref: str, model: ComponentModel) -> bool:
        """Store a part's model; False (and the user told why) when the settings refuse it."""
        try:
            self.settings.set_component(ref, model)
        except ConfigError as e:
            wx.MessageBox(f"{ref}: " + "\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
            return False
        return True

    def _set_pins(self, ref: str, text: str):
        """A Pins cell was edited: the part's own through-hole pin values (empty: none, the GT field and the
        board's defaults apply); unreadable text is refused and the old values shown again."""
        values, problems = values_from_data(text, ref)
        if problems:
            wx.MessageBox("\n".join(problems) + "\n\n" + PIN_HELP, "groundtruth", wx.ICON_WARNING)
        else:
            try:
                self.settings.set_component_pins(ref, values)
                self.on_change()
            except ConfigError as e:
                wx.MessageBox(f"{ref}: " + "\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
        wx.CallAfter(lambda: self and self._update_row(ref))

    def on_pin_defaults(self, _=None):
        """Pin defaults…: the board's through-hole pin values (settings `tht:`), for every part without its own."""
        if self.settings is None:
            return
        old = format_values(self.settings.config.tht)
        dlg = wx.TextEntryDialog(self, "The board's through-hole pin values, for every part without its own (empty: "
                                       "built in: copper filling the hole, a perfect joint, no contact "
                                       "resistance).\n\n" + PIN_HELP, "Pin defaults", old)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            text = dlg.GetValue().strip()
        finally:
            dlg.Destroy()
        values, problems = values_from_data(text, "Pin defaults")
        if problems:
            wx.MessageBox("\n".join(problems), "groundtruth", wx.ICON_WARNING)
            return
        try:
            self.settings.set_pin_defaults(values)
        except ConfigError as e:
            wx.MessageBox("\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
            return
        self.on_change()

    def on_bulk(self, _):
        """Set every filtered part to the chosen model kind (after asking), keeping the parameters it has."""
        kind = MODEL_KINDS[self.bulk.GetSelection()]
        refs = self._visible()
        if wx.MessageBox(f"Set {len(refs)} part(s) to '{kind}'?", "groundtruth", wx.YES_NO) != wx.YES:
            return
        for ref in refs:
            old = self.settings.config.components[ref].model
            if not self._set(ref, ComponentModel(kind, {k: v for k, v in old.params.items() if k in MODEL_PARAMS[kind]},
                                                 "", list(old.pins),
                                                 [k for k in old.from_schematic if k in MODEL_PARAMS[kind]])):
                break                   # the parts before it are set; the user was told about this one
        self.on_change()
        self.refresh()

    def on_reset(self, _):
        """Give every filtered part the model suggested from its footprint and value again."""
        refs = self._visible()
        for ref in refs:
            fp = self.board.footprint(ref)
            if fp is not None and not self._set(ref, suggest_model(fp)):
                break
        self.on_change()
        self.refresh()
