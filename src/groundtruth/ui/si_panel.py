"""Signal integrity tab: differential pairs and impedance targets that Run checks checks (analysis/si.py; the
plan's M3).

A pair is its two nets, the differential impedance to hold (none: shown, not checked), its edge (the allowed skew is a
tenth of it unless given) and a note. An impedance target is a set of nets (names or patterns with * and ?) or a KiCad
net class with the Z0 and tolerance their tracks must hold. The limits: crosstalk as a share of the aggressor's swing,
the via stubs' resonance against the signal's knee, the cross-sections' spacing. After Run checks (or Simulate) each
pair and target shows what was found: Zdiff and skew, Z0's range; the issues are on the Issues tab.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

import wx
from .messages import box as message_box   # laid-out message boxes (ui/messages.py)

from ..components.values import format_si, parse_value
from ..config.settings import ConfigError, DiffPairSpec, ImpedanceTarget
from .listsel import SelectableRows
from .widgets import button_row

PAIR_TIPS = {
    "P net": "The pair's positive net (e.g. /USB/D_P); Find pairs fills the pairs it finds by name.",
    "N net": "Its negative net (e.g. /USB/D_N).",
    "Name": "A short name for lists and issues; empty: the nets' names.",
    "Zdiff (Ω)": "The differential impedance to hold, e.g. 90 (USB) or 100 (Ethernet, LVDS); empty: shown, not "
                 "checked.",
    "Tolerance (%)": "How far Zdiff may be off, in % (default 10).",
    "Edge (s)": "The pair's rise time, e.g. 500p: it sets the allowed skew (a tenth of it) and the via-stub check.",
    "Max skew (s)": "The delay mismatch allowed between the two nets, e.g. 10p; empty: a tenth of the edge.",
    "Note": "Your own note (kept in the settings file).",
}
TARGET_TIPS = {
    "Nets": "The nets to hold to Z0: names or patterns with * and ?, comma separated (e.g. /CPU/SPI_*).",
    "Net class": "Or a KiCad net class (from the project file beside the board), e.g. HighSpeed.",
    "Z0 (Ω)": "The single-ended impedance to hold, e.g. 50.",
    "Tol. (%)": "How far Z0 may be off, in % (default 10).",
    "Layers": "Only on these copper layers, comma separated; empty: every layer.",
}
LIMIT_TIPS = {
    "Crosstalk (%)": "Near- or far-end crosstalk above this share of the aggressor's swing is an issue (default 5).",
    "Stub factor": "A via stub resonating below this times the signal's knee (0.35 / rise time) is an issue "
                   "(default 3).",
    "Cut every (mm)": "The cross-sections along a track this far apart (default 2): smaller is finer and slower.",
}
BUTTON_TIPS = {
    "Find pairs": "add every pair found by its nets' names (_P/_N, +/-, CAN_H/CAN_L, DP/DM, X and X*) that is not "
                  "listed yet, with its interface's impedance and edge when the name tells (USB 90 Ω, Ethernet "
                  "100 Ω ...)",
    "Add": "add the form's pair",
    "Update selected": "replace the selected pair with the form's values",
    "Remove selected": "remove the selected pairs (Delete does the same)",
    "Add target": "add the form's impedance target",
    "Remove target": "remove the selected impedance target",
    "Apply limits": "write the crosstalk, stub and cut limits to the settings",
}


class SiPanel(SelectableRows, wx.Panel):
    """The Signal integrity tab: the pairs (a list with on/off ticks) and the impedance targets on the left, their
    forms and the limits on the right. Every change is written to the settings at once."""

    def __init__(self, parent, on_change, in_steps: bool = False):
        """on_change(): the settings changed (the window saves them and marks results as stale). in_steps: the forms
        are built later, in three `build_steps` (MainFrame._build_pages runs them one per event: the tab in one
        step blocked the window 149-160 ms, tools/dev/build_profile.py)."""
        super().__init__(parent)
        self.on_change = on_change
        self.board = None
        self.settings = None
        self.pairs: list[DiffPairSpec] = []
        self.targets: list[ImpedanceTarget] = []
        self.results = None                 # analysis.si.SiResult of the last run
        self.hint = wx.StaticText(self, label="No pairs yet: press Find pairs (on the right) to list the pairs found "
                                              "by their nets' names, or add one by hand.")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        for i, (c, w) in enumerate([("Pair", 130), ("Zdiff", 70), ("Edge", 60), ("Max skew", 70), ("Result", 230),
                                    ("Source", 120)]):
            self.list.InsertColumn(i, c, width=w)
        self.list.SetMinSize(wx.Size(320, 110))
        self._init_rows(None, "pairs")
        self._init_enable("pairs")
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.tlist = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (c, w) in enumerate([("Nets / class", 160), ("Z0", 60), ("Tolerance", 70), ("Layers", 90),
                                    ("Result", 220)]):
            self.tlist.InsertColumn(i, c, width=w)
        self.tlist.SetMinSize(wx.Size(320, 80))
        self.tlist.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_target_select)

        self.build_steps = [self._form_pair, self._form_target, self._form_layout]
        while self.build_steps and not in_steps:
            self.build_steps.pop(0)()

    def _form_pair(self):
        """The pair's form and its buttons (the first build step)."""
        fw = wx.Panel(self)
        form = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        form.AddGrowableCol(1)
        self.p = wx.ComboBox(fw)
        self.n = wx.ComboBox(fw)
        self.name = wx.TextCtrl(fw)
        self.zdiff = wx.TextCtrl(fw)
        self.tol = wx.TextCtrl(fw)
        self.edge = wx.TextCtrl(fw)
        self.skew = wx.TextCtrl(fw)
        self.note = wx.TextCtrl(fw)
        for ctrl, hint in ((self.name, "from the nets"), (self.zdiff, "none: not checked"), (self.tol, "10"),
                           (self.edge, "1n"), (self.skew, "a tenth of the edge")):
            ctrl.SetHint(hint)
        for label, ctrl in (("P net", self.p), ("N net", self.n), ("Name", self.name), ("Zdiff (Ω)", self.zdiff),
                            ("Tolerance (%)", self.tol), ("Edge (s)", self.edge), ("Max skew (s)", self.skew),
                            ("Note", self.note)):
            self._row(fw, form, label, ctrl, PAIR_TIPS[label])
        fw.SetSizer(form)
        btns = button_row(self, [("Find pairs", self.on_find), ("Add", self.on_add),
                                 ("Update selected", self.on_update), ("Remove selected", self.on_remove)], BUTTON_TIPS)
        self._built = {"fw": fw, "btns": btns}

    def _form_target(self):
        """The impedance target's and the limits' forms with their buttons (the second build step)."""
        tw = wx.Panel(self)
        tform = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        tform.AddGrowableCol(1)
        self.t_nets = wx.TextCtrl(tw)
        self.t_class = wx.TextCtrl(tw)
        self.t_z0 = wx.TextCtrl(tw)
        self.t_tol = wx.TextCtrl(tw)
        self.t_layers = wx.TextCtrl(tw)
        for ctrl, hint in ((self.t_nets, "e.g. /CPU/SPI_*"), (self.t_class, "or a KiCad net class"),
                           (self.t_z0, "e.g. 50"), (self.t_tol, "10"), (self.t_layers, "every layer")):
            ctrl.SetHint(hint)
        for label, ctrl in (("Nets", self.t_nets), ("Net class", self.t_class), ("Z0 (Ω)", self.t_z0),
                            ("Tol. (%)", self.t_tol), ("Layers", self.t_layers)):
            self._row(tw, tform, label, ctrl, TARGET_TIPS[label])
        tw.SetSizer(tform)
        tbtns = button_row(self, [("Add target", self.on_add_target), ("Remove target", self.on_remove_target)],
                           BUTTON_TIPS)

        lw = wx.Panel(self)
        lform = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        lform.AddGrowableCol(1)
        self.xt = wx.TextCtrl(lw)
        self.stub = wx.TextCtrl(lw)
        self.step = wx.TextCtrl(lw)
        for label, ctrl in (("Crosstalk (%)", self.xt), ("Stub factor", self.stub), ("Cut every (mm)", self.step)):
            self._row(lw, lform, label, ctrl, LIMIT_TIPS[label])
        lw.SetSizer(lform)
        lbtns = button_row(self, [("Apply limits", self.on_limits)], BUTTON_TIPS)
        self._built.update(tw=tw, tbtns=tbtns, lw=lw, lbtns=lbtns)

    def _form_layout(self):
        """The tab's layout (the last build step)."""
        fw, btns, tw, tbtns, lw, lbtns = (self._built[k] for k in ("fw", "btns", "tw", "tbtns", "lw", "lbtns"))
        del self._built
        about = wx.StaticText(self, label="Run checks solves cross-sections of the board along the listed nets and the "
                                          "pairs: Z0 and Zdiff against their targets, the pairs' skew, crosstalk into "
                                          "tracks beside the listed nets, and via stubs (the issues on the Issues tab, "
                                          "the figures here and in the report).")
        about.SetName("muted")

        def heading(text):
            """A bold section title."""
            t = wx.StaticText(self, label=text)
            t.SetFont(t.GetFont().Bold())
            return t
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(heading("Differential pair"), 0, wx.BOTTOM, 4)
        right.Add(fw, 0, wx.EXPAND)
        right.Add(btns, 0, wx.TOP | wx.EXPAND, 6)
        right.Add(heading("Impedance target"), 0, wx.TOP | wx.BOTTOM, 4)
        right.Add(tw, 0, wx.EXPAND)
        right.Add(tbtns, 0, wx.TOP | wx.EXPAND, 6)
        right.Add(heading("Limits"), 0, wx.TOP | wx.BOTTOM, 4)
        right.Add(lw, 0, wx.EXPAND)
        right.Add(lbtns, 0, wx.TOP | wx.EXPAND, 6)
        right.Add(about, 0, wx.TOP | wx.EXPAND, 8)
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(self.list_head(self.hint), 0, wx.EXPAND | wx.BOTTOM, 6)
        left.Add(heading("Differential pairs"), 0, wx.BOTTOM, 4)
        left.Add(self.list, 3, wx.EXPAND)
        left.Add(heading("Impedance targets"), 0, wx.TOP | wx.BOTTOM, 4)
        left.Add(self.tlist, 2, wx.EXPAND)
        s = wx.BoxSizer(wx.HORIZONTAL)
        s.Add(left, 3, wx.EXPAND | wx.ALL, 6)
        s.Add(right, 2, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(s)
        self.hint.Hide()
        self.Bind(wx.EVT_SIZE, lambda e: (e.Skip(), about.Wrap(max(150, self.GetClientSize().width * 2 // 5 - 24))))

    @staticmethod
    def _row(parent, form, label, ctrl, tip):
        """A labelled form row with its tooltip."""
        text = wx.StaticText(parent, label=label)
        text.SetToolTip(tip)
        ctrl.SetToolTip(tip)
        form.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(ctrl, 1, wx.EXPAND)

    def load(self, board, settings):
        """Show this board's pairs, targets and limits; the P / N lists: the nets of the pairs found by name."""
        from ..analysis.si import find_pairs
        self.board, self.settings = board, settings
        self.results = None
        found = find_pairs(board) if board is not None else []
        self.p.Set([p for p, _ in found])
        self.n.Set([n for _, n in found])
        si = settings.config.si
        self.pairs = list(si.pairs)
        self.targets = list(si.impedance)
        self.xt.SetValue(f"{si.crosstalk_max * 100:g}")
        self.stub.SetValue(f"{si.stub_factor:g}")
        self.step.SetValue(f"{si.step_mm:g}")
        self.refresh()

    def show_results(self, si):
        """The figures of a run (analysis.si.SiResult) in the lists' Result columns."""
        self.results = si
        self.refresh()

    def _pair_result(self, spec) -> str:
        """A pair's figures from the last run: Zdiff's mean and range, the skew."""
        res = next((p for p in getattr(self.results, "pairs", ()) if p.spec.p == spec.p and p.spec.n == spec.n), None)
        if res is None:
            return ""
        s = res.summary()
        z = (f"Zdiff {s['zdiff_mean']:.0f} Ω ({s['zdiff_min']:.0f}-{s['zdiff_max']:.0f})" if "zdiff_mean" in s
             else "never side by side")
        return f"{z}, skew {s['skew_ps']:.0f} ps"

    def _target_result(self, t) -> str:
        """A target's figures from the last run: the range of Z0 over its nets, the length outside."""
        from ..analysis.si import impedance_targets
        if self.results is None or self.board is None:
            return ""
        probe = replace(self.settings.config.si, impedance=[t])
        nets = set(impedance_targets(self.board, replace(self.settings.config, si=probe)))
        profs = [n.summary() for n in self.results.nets if n.net in nets and n.points]
        if not profs:
            return "no net of it checked" if nets else "no net matches"
        lo, hi = min(p["z_min"] for p in profs), max(p["z_max"] for p in profs)
        off = sum(p.get("off_mm", 0.0) for p in profs)
        return f"{len(profs)} net(s), Z0 {lo:.0f}-{hi:.0f} Ω" + (f", {off:.1f} mm outside" if off else "")

    def refresh(self):
        """Rebuild both lists (switched-off pairs greyed)."""
        self.hint.Show(self.board is not None and not self.pairs)
        self.Layout()
        self.list.DeleteAllItems()
        for d in self.pairs:
            i = self.list.InsertItem(self.list.GetItemCount(), d.label)
            for col, text in enumerate([format_si(d.zdiff, "Ω") if d.zdiff else "–", format_si(d.rise_time, "s"),
                                        format_si(d.skew_limit, "s"), self._pair_result(d),
                                        "detected, review" if not d.reviewed else d.source], 1):
                self.list.SetItem(i, col, text)
            self._show_enabled(i, d)
        self.tlist.DeleteAllItems()
        for t in self.targets:
            i = self.tlist.InsertItem(self.tlist.GetItemCount(), ", ".join(t.nets) or f"class {t.netclass}")
            for col, text in enumerate([format_si(t.z0, "Ω"), f"{t.tol * 100:g} %", ", ".join(t.layers) or "all",
                                        self._target_result(t)], 1):
                self.tlist.SetItem(i, col, text)

    def on_select(self, evt):
        """A pair was selected: fill the form with it."""
        self._selection_changed()
        d = self.pairs[evt.GetIndex()]
        self.p.SetValue(d.p)
        self.n.SetValue(d.n)
        self.name.SetValue(d.name)
        self.zdiff.SetValue(f"{d.zdiff:g}" if d.zdiff else "")
        self.tol.SetValue(f"{d.tol * 100:g}")
        self.edge.SetValue(format_si(d.rise_time, ""))
        self.skew.SetValue(format_si(d.max_skew, "") if d.max_skew is not None else "")
        self.note.SetValue(d.note)

    def on_target_select(self, evt):
        """A target was selected: fill its form."""
        t = self.targets[evt.GetIndex()]
        self.t_nets.SetValue(", ".join(t.nets))
        self.t_class.SetValue(t.netclass)
        self.t_z0.SetValue(f"{t.z0:g}")
        self.t_tol.SetValue(f"{t.tol * 100:g}")
        self.t_layers.SetValue(", ".join(t.layers))

    def _form(self) -> Optional[DiffPairSpec]:
        """The pair form as a DiffPairSpec, or None after telling the user what is wrong (the settings' own parsing
        and the board's nets check it)."""
        from ..config.settings import _si_from_data
        p, n = self.p.GetValue().strip(), self.n.GetValue().strip()
        if not p or not n or p == n:
            message_box("Give the pair's two nets (P and N), or press Find pairs.", "GroundTruth",
                          wx.ICON_INFORMATION)
            return None
        if self.board is not None:
            missing = [x for x in (p, n) if x not in set(self.board.nets)]
            if missing:
                message_box(f"No net {', '.join(missing)} on this board.", "GroundTruth", wx.ICON_WARNING)
                return None
        data = {"p": p, "n": n, "name": self.name.GetValue().strip(), "zdiff": self.zdiff.GetValue().strip() or None,
                "tol": (self.tol.GetValue().strip() or "10") + "%", "rise_time": self.edge.GetValue().strip() or "1n",
                "max_skew": self.skew.GetValue().strip() or None, "note": self.note.GetValue().strip()}
        errors: list[str] = []
        si = _si_from_data({"pairs": [data]}, errors)
        if errors or not si.pairs:
            message_box("\n".join(errors) or "The pair could not be read.", "GroundTruth", wx.ICON_WARNING)
            return None
        return si.pairs[0]

    def _commit(self) -> bool:
        """Write the pairs to the settings and show them again; False (the list restored) when the settings refuse
        them."""
        try:
            self.settings.set_si_pairs(self.pairs)
        except ConfigError as e:
            message_box("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
            self.pairs = list(self.settings.config.si.pairs)
            return False
        self.refresh()
        self.on_change()
        return True

    def _row_names(self, rows: list[int]) -> list[str]:
        """The pairs of these rows."""
        return [self.pairs[i].label for i in rows if i < len(self.pairs)]

    def on_find(self, _=None):
        """Find pairs: every pair found by name that is not listed yet, with its interface's defaults."""
        if self.board is None:
            return
        from ..analysis.si import detect_pairs
        new = detect_pairs(self.board, self.settings.config)
        if not new:
            message_box("Every pair found by its nets' names is listed already." if self.pairs else
                          "No two routed nets are named like a pair (_P/_N, +/-, CAN_H/CAN_L, DP/DM, X and X*): add "
                          "one by hand (P net, N net, then Add).", "GroundTruth", wx.ICON_INFORMATION)
            return
        self.pairs += new
        if self._commit():
            message_box(f"Added {len(new)} pair(s): {', '.join(d.label for d in new)}. Review each one's impedance "
                          "and edge (from its interface when its name tells), then Run checks.", "GroundTruth",
                          wx.ICON_INFORMATION)

    def on_add(self, _):
        """Add the form's pair."""
        spec = self._form()
        if spec:
            self.pairs.append(spec)
            self._commit()

    def on_update(self, _):
        """Replace the selected pair with the form's values (its on/off state kept; reviewed from now on)."""
        rows = self.selected_rows()
        if len(rows) != 1:
            message_box("Select a single pair to update it from the form.", "GroundTruth", wx.ICON_INFORMATION)
            return
        spec = self._form()
        if spec:
            old = self.pairs[rows[0]]
            self.pairs[rows[0]] = replace(spec, enabled=old.enabled, source=old.source)
            if self._commit():
                self.reselect(rows)

    def on_remove(self, _):
        """Remove the selected pairs."""
        rows = set(self.selected_rows())
        if rows:
            self.pairs = [d for i, d in enumerate(self.pairs) if i not in rows]
            self._commit()

    def _commit_targets(self) -> bool:
        """Write the impedance targets; False (the list restored) when the settings refuse them."""
        try:
            self.settings.set_si_targets(self.targets)
        except ConfigError as e:
            message_box("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
            self.targets = list(self.settings.config.si.impedance)
            return False
        self.refresh()
        self.on_change()
        return True

    def on_add_target(self, _):
        """Add the target form's nets or class with its Z0."""
        from ..config.settings import _si_from_data
        data = {"nets": self.t_nets.GetValue().strip(), "class": self.t_class.GetValue().strip(),
                "z0": self.t_z0.GetValue().strip(), "tol": (self.t_tol.GetValue().strip() or "10") + "%",
                "layers": [x.strip() for x in self.t_layers.GetValue().split(",") if x.strip()]}
        if not data["z0"]:
            message_box("Give the target's Z0 (e.g. 50).", "GroundTruth", wx.ICON_INFORMATION)
            return
        errors: list[str] = []
        si = _si_from_data({"impedance": [data]}, errors)
        if errors or not si.impedance:
            message_box("\n".join(errors) or "The target could not be read.", "GroundTruth", wx.ICON_WARNING)
            return
        self.targets.append(si.impedance[0])
        self._commit_targets()

    def on_remove_target(self, _):
        """Remove the selected impedance target."""
        k = self.tlist.GetFirstSelected()
        if 0 <= k < len(self.targets):
            del self.targets[k]
            self._commit_targets()

    def on_limits(self, _):
        """Write the limits (crosstalk %, stub factor, cut spacing)."""
        def number(ctrl, default: str) -> float:
            """A field's number (its default when empty); ValueError naming the text when it isn't one."""
            text = ctrl.GetValue().strip() or default
            v = parse_value(text)
            if v is None:
                raise ValueError(f"'{text}' is not a number")
            return v
        try:
            xt = number(self.xt, "5") / 100
            stub = number(self.stub, "3")
            step = number(self.step, "2")
            if not (0 < xt < 1 and stub > 0 and step > 0):
                raise ValueError("crosstalk between 0 and 100 %, stub factor and cut spacing above 0")
            self.settings.set_si_limits(xt, stub, step)
        except (ValueError, ConfigError) as e:
            message_box(str(e) if isinstance(e, ValueError) else "\n".join(e.messages), "GroundTruth",
                          wx.ICON_WARNING)
            return
        self.on_change()
