"""Parts tab: every footprint with its value and the electrical model used for it."""
from __future__ import annotations

import wx
from .messages import box as message_box   # laid-out message boxes (ui/messages.py)
import wx.grid as gridlib

from ..components.defaults import ref_prefix, suggest_model
from typing import Optional

from ..components.fields import MODEL_FIELDS, model_values, same, show_value
from ..components.models import MODEL_KINDS, MODEL_PARAMS, ComponentModel
from ..analysis.pins import format_values, gt_values, values_from_data
from ..components.values import format_si, parse_value
from ..config.settings import ConfigError
from . import theme
from .background import LatestOnly

COLS = ["Ref", "Value", "Footprint", "Model", "R (Ω)", "C (F)", "C at bias", "C in circuit", "L (H)", "ESR (Ω)",
        "ESL (H)", "Pins", "Schematic", "Note"]
PINS_COL = COLS.index("Pins")             # a part's own through-hole pin values (analysis/pins.py)
# a ceramic's capacitance at the DC voltage across it (analysis/bias.part_bias, its voltage from cap_voltages, worked
# out in the background when the tab loads): what a run uses; read only
CIRCUIT_COL = COLS.index("C in circuit")
BIAS_HELP = ("C at bias: the capacitor's DC-bias curve, 1 to 12 points: 75nF (one value: taken at half its Rated "
             "Voltage), 75nF@50V (one point), 95nF@10V, 66nF@30V, 39nF@50V (a curve), or -5%@10V, -34%@30V (the "
             "change from C). Each capacitor then gets its capacitance at its own DC voltage (C in circuit). See "
             "Cbias in the help.")
PIN_HELP = ("Through-hole pins, comma separated: contact:0.5m (the mating contact, Ω per pin), joint:50u (the solder "
            "joint, Ω per pin), fill:75 (% of the hole soldered, from the side opposite the part), pin:brass (hole = "
            "copper filling the hole, copper, brass, bronze, becu, tellurium), pin_d:1.0 (mm), pin_r:0.1m (Ω through "
            "the board); for any part with a model, SMD too: bond:50 (µm of solder between its pads and its metal; "
            "built in 75, 0 = a perfect bond); a power stage's switch: entry:tab (each side's metal soldered over "
            "its pads) or entry:even (spread over its pins, a GaN die; built in: tab for a pad 1 mm wide or more). "
            "Each value: the part's GT field, else its Pins cell, else Pin defaults…, else built in.")
PARAM_COL = {"R": 4, "C": 5, "C_bias": 6, "L": 8, "ESR": 9, "ESL": 10}
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
        # each capacitor's DC voltage (analysis/bias.cap_voltages), worked out in the background (board-wide: not on
        # the GUI thread); _volts_asked numbers the requests, so a late answer for an older board is dropped
        self._volts: Optional[dict] = None
        self._volts_asked = 0
        self._volter = LatestOnly("part voltages")

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
                                         "ports; 'short' is a dead short. C at bias: a ceramic's DC-bias curve "
                                         "(75nF@50V, or 95nF@10V, 39nF@50V); C in circuit: its capacitance at the "
                                         "DC voltage across it, what a run uses (Cbias in the help). A FET's or "
                                         "controller's fields (Rds(on), Coss …) go to its power stage: Converters "
                                         "tab. Schematic: ◆ values from the symbol's fields, ! set here and "
                                         "different. Pins: a part's own pin values (through-hole: contact:0.5m, "
                                         "joint:50u, fill:75, pin:brass; any part: bond:50, µm of solder; a "
                                         "stage's switch: entry:tab|even; hover "
                                         "Pin defaults… for all). Edits are "
                                         "saved to the settings.")
        hint.SetName("muted")
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(top, 0, wx.ALL, 6)
        s.Add(self.grid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        s.Add(hint, 0, wx.ALL, 6)
        self.SetSizer(s)

    def load(self, board, settings):
        """Show this board's parts (its footprints give the schematic fields), and work out each capacitor's DC
        voltage for the C in circuit column in the background."""
        self.board, self.settings = board, settings
        self.refresh()
        self.ask_voltages()

    def ask_voltages(self):
        """Work out each capacitor's DC voltage (analysis/bias.cap_voltages) in the background; the C in circuit
        cells show it when it arrives (_got_voltages). The board and settings are handed over as they are now (an
        edit makes a new settings object: the worker's stays as it was)."""
        if self.board is None or self.settings is None:
            return
        self._volts_asked += 1
        asked, board, config = self._volts_asked, self.board, self.settings.config

        def work():
            """(worker) The voltages, then back to the GUI thread."""
            from ..analysis.bias import cap_voltages
            volts = cap_voltages(board, config)
            wx.CallAfter(lambda: self and self._got_voltages(asked, volts))
        self._volter.submit(work)

    def _got_voltages(self, asked: int, volts: dict):
        """(GUI thread) The capacitors' voltages arrived: fill the C in circuit cells (a newer request's answer
        follows when this one is outdated)."""
        if asked < self._volts_asked or self.settings is None:
            return
        self._volts = volts
        g = self.grid
        g.BeginBatch()
        for row, ref in enumerate(self.rows):
            e = self.settings.config.components.get(ref)
            if e is not None and e.model.kind == "cap":
                g.SetCellValue(row, CIRCUIT_COL, self._circuit_text(ref, e.model))
        g.EndBatch()

    def _circuit_text(self, ref: str, m: ComponentModel) -> str:
        """A capacitor's C in circuit cell: its capacitance at the DC voltage across it ("99.3nF at 3.3 V"), the
        nominal without a curve, "…" while the voltages are worked out, the problem when the curve is unreadable."""
        if m.kind != "cap":
            return ""
        if not m.bias:
            return format_si(m.get("C"), "F")
        if self._volts is None:
            return "…"
        from ..analysis.bias import part_bias
        fp = self.board.footprint(ref) if self.board is not None else None
        if fp is None:
            return ""
        row, bad = part_bias(fp, m, self._volts.get(ref))
        if bad:
            return "! " + bad[0]
        c = format_si(float(f"{row.c:.3g}"), "F")
        if row.how in ("class I", "tantalum", "electrolytic"):
            return f"{c} ({row.how}: no DC-bias loss)"
        if row.how == "flat":
            return f"{c} (no voltage or rating: as given)"
        if row.why == "voltage unknown":
            return f"{c} (voltage unknown: 0 V)"
        return f"{c} at ≤ {row.volts:.3g} V"     # the worst-case DC voltage across it (analysis/bias.cap_voltages)

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
            [f"! {p}: schematic {show_value(r.value)}" for p, r in differs.items()]
        fp = self.board.footprint(ref) if self.board is not None else None
        gt_pins = gt_values(fp)[0] if fp is not None else {}
        if gt_pins:
            marks.append("◆ GT " + format_values(gt_pins))
        vals = [""] * len(COLS)                 # by column: the parameter columns are not all next to each other
        vals[:4] = [ref, e.value, e.footprint, m.kind]
        vals[PINS_COL], vals[SCHEMATIC_COL], vals[NOTE_COL] = format_values(e.tht), "; ".join(marks), m.note
        for p, col in PARAM_COL.items():
            v = m.value_of(p)
            if v is not None:
                vals[col] = show_value(v)       # a capacitor's C at bias: its DC-bias curve's text
        vals[CIRCUIT_COL] = self._circuit_text(ref, m)
        for col, v in enumerate(vals):
            g.SetCellValue(row, col, str(v))
        if new:
            for col in (0, 1, 2, SCHEMATIC_COL, CIRCUIT_COL):
                g.SetReadOnly(row, col)
            g.SetCellEditor(row, 3, gridlib.GridCellChoiceEditor(list(MODEL_KINDS)))
        g.SetCellBackgroundColour(row, CIRCUIT_COL, g.GetDefaultCellBackgroundColour() if m.kind == "cap" else
                                  theme.colour(theme.palette().panel))
        normal = g.GetDefaultCellBackgroundColour()
        for p, col in PARAM_COL.items():
            g.SetReadOnly(row, col, p not in MODEL_PARAMS[m.kind])
            g.SetCellBackgroundColour(row, col, theme.colour(theme.palette().panel)
                                      if p not in MODEL_PARAMS[m.kind] else
                                      theme.colour(_DIFFERS[theme.palette().dark]) if p in differs else normal)
        g.SetCellBackgroundColour(row, 3, theme.colour("#4a3a1f" if theme.palette().dark else "#ffdcc8")
                                  if m.missing_params() else normal)
        # through-hole pins have every pin value; any part with a model that conducts (a lumped part) its solder
        # `bond`, a power stage's switch its `entry` (simulate.switch_lids)
        pins = (fp is not None and any(p.pad_type == "thru_hole" for p in fp.pads)) or bool(e.tht) or \
            (m is not None and m.kind != "open") or \
            any(ref in s.parts for s in self.settings.config.stages())
        g.SetReadOnly(row, PINS_COL, not pins)
        g.SetCellBackgroundColour(row, PINS_COL, normal if pins else theme.colour(theme.palette().panel))

    def _schematic(self, ref: str, m: ComponentModel) -> tuple[list[str], dict]:
        """(the model parameters linked to the part's schematic fields, {parameter: Reading} set here and
        different from them)."""
        fp = self.board.footprint(ref) if self.board is not None else None
        if fp is None or m.kind not in MODEL_FIELDS:
            return [], {}
        values, _ = model_values(fp, m.kind)
        linked = [p for p in m.from_schematic if p in values]
        differs = {p: r for p, r in values.items() if p not in m.from_schematic and m.value_of(p) is not None
                   and not same(m.value_of(p), r.value)}
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
                                 [k for k in model.from_schematic if k in MODEL_PARAMS.get(text, ())],
                                 model.bias if text == "cap" else "")
        elif col == PINS_COL:
            self._set_pins(ref, text)
            return
        elif col == NOTE_COL:
            new = ComponentModel(model.kind, dict(model.params), text, list(model.pins), list(model.from_schematic),
                                 model.bias)
        elif col == PARAM_COL["C_bias"]:
            new = self._bias_edit(ref, model, text)
            if new is None:
                wx.CallAfter(lambda: self and self._update_row(ref))     # the old value back
                return
        else:
            name = next(p for p, c in PARAM_COL.items() if c == col)
            val = parse_value(text)
            if val is None and text:
                message_box(f"Cannot read {text!r} as a number", "groundtruth", wx.ICON_WARNING)
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
            new = ComponentModel(model.kind, params, model.note, list(model.pins), linked, model.bias)
        if self._set(ref, new):
            self.on_change()
            if col in (3, PARAM_COL["C"], PARAM_COL["C_bias"]):
                self.ask_voltages()             # a capacitor's model changed: its C in circuit again
        wx.CallAfter(lambda: self and self._update_row(ref))     # the stored model (the old one if refused)

    def _bias_edit(self, ref: str, model: ComponentModel, text: str) -> Optional[ComponentModel]:
        """The C at bias cell was edited: the part's DC-bias curve from the text (components/bias.py; empty: none),
        linked to the schematic again when it equals the fields'. None (the user told why) when it can't be read."""
        from ..components.bias import check_points, parse_bias
        points, problems = parse_bias(text)
        problems += check_points(points, model.get("C"))
        if problems:
            message_box(f"{ref}: " + "\n".join(problems) + "\n\n" + BIAS_HELP, "groundtruth", wx.ICON_WARNING)
            return None
        new = ComponentModel(model.kind, dict(model.params), model.note, list(model.pins),
                             [k for k in model.from_schematic if k != "C_bias"], model.bias)
        new.set_value("C_bias", text or None)
        fp = self.board.footprint(ref) if self.board is not None else None
        field = model_values(fp, model.kind)[0].get("C_bias") if fp is not None else None
        if field is not None and same(new.value_of("C_bias"), field.value):
            new.from_schematic.append("C_bias")
        return new

    def _set(self, ref: str, model: ComponentModel) -> bool:
        """Store a part's model; False (and the user told why) when the settings refuse it."""
        try:
            self.settings.set_component(ref, model)
        except ConfigError as e:
            message_box(f"{ref}: " + "\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
            return False
        return True

    def _set_pins(self, ref: str, text: str):
        """A Pins cell was edited: the part's own through-hole pin values (empty: none, the GT field and the
        board's defaults apply); unreadable text is refused and the old values shown again."""
        values, problems = values_from_data(text, ref)
        if problems:
            message_box("\n".join(problems) + "\n\n" + PIN_HELP, "groundtruth", wx.ICON_WARNING)
        else:
            try:
                self.settings.set_component_pins(ref, values)
                self.on_change()
            except ConfigError as e:
                message_box(f"{ref}: " + "\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
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
            message_box("\n".join(problems), "groundtruth", wx.ICON_WARNING)
            return
        try:
            self.settings.set_pin_defaults(values)
        except ConfigError as e:
            message_box("\n".join(e.messages), "groundtruth", wx.ICON_WARNING)
            return
        self.on_change()

    def on_bulk(self, _):
        """Set every filtered part to the chosen model kind (after asking), keeping the parameters it has."""
        kind = MODEL_KINDS[self.bulk.GetSelection()]
        refs = self._visible()
        if message_box(f"Set {len(refs)} part(s) to '{kind}'?", "groundtruth", wx.YES_NO) != wx.YES:
            return
        for ref in refs:
            old = self.settings.config.components[ref].model
            if not self._set(ref, ComponentModel(kind, {k: v for k, v in old.params.items() if k in MODEL_PARAMS[kind]},
                                                 "", list(old.pins),
                                                 [k for k in old.from_schematic if k in MODEL_PARAMS[kind]],
                                                 old.bias if kind == "cap" else "")):
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
