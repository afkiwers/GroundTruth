"""Fixes tab: suggested stitching vias, decaps, bridge capacitors and fills, with predicted effect and a
verification of the selected set."""
from __future__ import annotations

import wx

from ..report.formats import comparison_text
from . import theme

KIND_COLOUR = {"via": (44, 160, 44), "cap": (31, 119, 180), "fill": (148, 103, 189)}   # RGB


CHECK_NAMES = {"emi_margin": "radiated", "conducted_emission": "conducted", "hot_loop": "ringing"}


def _db(v) -> str:
    """A dB change for the table ("–" when unknown)."""
    return "–" if v is None else f"{v:+.1f} dB"


class FixesPanel(wx.Panel):
    """The Fixes tab: the layout fixes found by Suggest (a ticked list, with the verification of the selected
    set) and, under them, the fix advisor's design changes for the power stages."""

    def __init__(self, parent, on_suggest, on_draw, on_place, on_add_whatif, on_compare, on_check_design=None):
        """The tab; the callbacks are the main window's: run Suggest, draw / place / store the ticked fixes,
        compare the what-if edits, and Simulate a design change in memory (on_check_design(stage, option))."""
        super().__init__(parent)
        self.result = None
        self.design: list[tuple[str, dict]] = []          # (stage, option) rows of the design-change list
        self.on_check_design = on_check_design
        self.buttons = []
        btns = wx.WrapSizer(wx.HORIZONTAL)
        for label, fn, tip in (
                ("Suggest fixes", lambda e: on_suggest(), "Find legal positions for fixes, predict and verify them"),
                ("Draw ticked on board", lambda e: on_draw(self.checked()), "Crosses and labels on the suggestion layer"),
                ("Place ticked vias in KiCad", lambda e: on_place(self.checked()), "Adds real vias (one undo step)"),
                ("Add ticked to what-if", lambda e: on_add_whatif(self.checked()), "Store them in the settings"),
                ("Compare what-if", lambda e: on_compare(), "Baseline vs. the settings's enabled what-if edits")):
            b = theme.Button(self, label)
            b.SetToolTip(tip)
            b.Bind(wx.EVT_BUTTON, fn)
            btns.Add(b, 0, wx.RIGHT | wx.BOTTOM, 6)
            self.buttons.append(b)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.EnableCheckBoxes()
        for i, (c, w) in enumerate([("#", 50), ("Kind", 50), ("Net", 150), ("Location", 110), ("Predicted", 210),
                                    ("Fix", 330), ("For issue", 150), ("Note", 200)]):
            self.list.InsertColumn(i, c, width=w)
        self.summary = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 140))
        hint = wx.StaticText(self, label="Ticked = selected by the search (best per issue, verified together). "
                                         "Predicted: change of the loop moment at the worst harmonic (plane "
                                         "capacitance included) and of the loop inductance at the fundamental. The "
                                         "verification re-runs the full analysis on a copy of the board with the "
                                         "selected fixes.")
        hint.Wrap(900)
        # the fix advisor's design changes for the power stages (analysis/fix_advisor.py), after every Simulate
        title = wx.StaticText(self, label="Design changes (power stages): what clears the ringing, radiated and "
                                          "conducted issues, from the last Simulate")
        title.SetFont(title.GetFont().Bold())
        self.design_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (c, w) in enumerate([("Stage", 120), ("Change", 380), ("Radiated", 80), ("Conducted", 80),
                                    ("Overshoot", 75), ("Extra loss", 75), ("Clears", 200)]):
            self.design_list.InsertColumn(i, c, width=w)
        self.design_list.SetMinSize(wx.Size(-1, 150))
        self.design_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._design_selected)
        self.design_text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 110))
        self.check_design = theme.Button(self, "Check in Simulate")
        self.check_design.SetToolTip("Simulate the selected change's stage again with the change applied (in memory: "
                                     "the settings stay as they are) and compare it with the prediction")
        self.check_design.Bind(wx.EVT_BUTTON, lambda e: self._check())
        self.check_design.Enable(False)
        self._busy = False
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.design_text, 1, wx.EXPAND | wx.RIGHT, 6)
        row.Add(self.check_design, 0)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(btns, 0, wx.ALL, 6)
        s.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        s.Add(self.summary, 0, wx.EXPAND | wx.ALL, 6)
        s.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        s.Add(title, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        s.Add(self.design_list, 1, wx.EXPAND | wx.ALL, 6)
        s.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.SetSizer(s)

    def show_design(self, converters: list):
        """The design changes of the stages Simulate found issues on (ConverterResult.fixes)."""
        self.design = [(c.net, o) for c in converters for o in (getattr(c, "fixes", None) or [])]
        lst = self.design_list
        lst.DeleteAllItems()
        for k, (stage, o) in enumerate(self.design):
            oc = o["outcome"] or {}
            ov = oc.get("overshoot_v")
            vals = [stage, o["title"], _db(oc.get("radiated_db")), _db(oc.get("conducted_db")),
                    "–" if ov is None else f"{ov:.1f} V", f"{(oc.get('loss_w') or 0) * 1e3:.0f} mW",
                    ", ".join(CHECK_NAMES.get(c, c) for c in o["clears"])]
            lst.InsertItem(k, vals[0])
            for col, v in enumerate(vals[1:], 1):
                lst.SetItem(k, col, v)
            if o["kind"] == "now":
                lst.SetItemTextColour(k, theme.colour(theme.palette().muted))
        self.design_text.SetValue("Select a change for its parts and notes." if self.design else
                                  "No power stage with ringing, radiated or conducted issues in the last Simulate.")
        self.check_design.Enable(False)

    def _design_selected(self, evt):
        """A design change was selected: its parts and notes below the list; Check enabled if it can be
        simulated."""
        stage, o = self.design[evt.GetIndex()]
        lines = [f"{stage}: {o['title']}"] + [f"  part: {p}" for p in o["parts"]] + [f"  {n}" for n in o["notes"]]
        self.design_text.SetValue("\n".join(lines))
        self.check_design.Enable(self._checkable(o))

    def _checkable(self, o: dict) -> bool:
        """A change Simulate can apply in memory: a snubber, an edge time, spread spectrum (not a shorter loop, a
        layout change, nor a filter, which is outside the board)."""
        return (bool(self.on_check_design) and not self._busy and o["kind"] in ("snubber", "edge", "combined", "spread")
                and o["change"].get("loop_scale", 1.0) == 1.0)

    def _check(self):
        """Simulate the selected design change (the main window runs it as a job)."""
        k = self.design_list.GetFirstSelected()
        if k >= 0 and self.on_check_design:
            self.on_check_design(*self.design[k])

    def show(self, result):
        """Show a Suggest result: one row per suggestion (ticked: chosen by the search), coloured by kind, and the
        verification of the selected set below."""
        self.result = result
        self.list.DeleteAllItems()
        for k, sug in enumerate(result.suggestions):
            row = self.list.InsertItem(k, sug.id)
            at = f" @ {sug.f_eval / 1e6:.4g} MHz" if sug.f_eval else ""
            pred = (f"{sug.delta_db:+.1f} dB{at}, L {sug.delta_l * 100:+.0f} %" if sug.delta_db is not None else "–")
            for col, v in enumerate([sug.kind, sug.net, f"{sug.location[0]:.2f}, {sug.location[1]:.2f}", pred,
                                     f"{sug.title}: {sug.message}", sug.issue.check if sug.issue else "", sug.note], 1):
                self.list.SetItem(row, col, v)
            self.list.CheckItem(row, sug.selected)
            if sug.kind in KIND_COLOUR:
                self.list.SetItemTextColour(row, KIND_COLOUR[sug.kind])
        if result.after is not None:
            self.summary.SetValue("Verification of the selected fixes:\n" +
                                  comparison_text(result.comparison, result.fixed, result.new, result.kept))
        else:
            self.summary.SetValue(f"{len(result.suggestions)} suggestion(s); nothing selected to verify.")

    def checked(self):
        """The ticked suggestions."""
        if self.result is None:
            return []
        return [s for k, s in enumerate(self.result.suggestions) if self.list.IsItemChecked(k)]

    def show_text(self, text: str):
        """A message in the summary box (a comparison, a failure)."""
        self.summary.SetValue(text)

    def enable(self, on: bool):
        """Grey out the buttons while an analysis runs."""
        for b in self.buttons:
            b.Enable(on)
        self._busy = not on
        k = self.design_list.GetFirstSelected()
        self.check_design.Enable(on and k >= 0 and self._checkable(self.design[k][1]))
