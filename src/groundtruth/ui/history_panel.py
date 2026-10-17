"""History tab: the Simulate runs kept for this board (report/history.py), newest first, with what changed since the
run before; a note per run; two runs compared; a run's report page and board copy opened. Every file access runs
in the tab's own background thread (InOrder: a deletion is never dropped for a newer request), never on the GUI
thread: the folder may be on OneDrive, where a file not kept on this computer is downloaded when it is read."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

import wx

from ..report.history import History, Run, changes, compare, comparison_text
from . import theme
from .background import InOrder
from .progress import clock

COLUMNS = [("Run", 140), ("Note", 200), ("Changed", 140), ("Resolution", 70), ("What-if", 60), ("Errors", 50),
           ("Warnings", 60), ("Worst radiated", 190), ("Worst conducted", 190), ("Took", 50), ("Report", 70)]
CMP_COLUMNS = [("Item", 220), ("Radiated margin", 140), ("Δ", 60), ("Conducted margin", 140), ("Δ", 60),
               ("Loop L", 130), ("Δ", 70), ("L / ideal", 100)]
WORSE_DB = 0.5          # a margin change counted as better / worse in the comparison's colours
WORSE_NH = 0.05


def run_label(run_id: str) -> str:
    """'2026-10-09 19:07:18' (and '-2' for a second run in the same second)."""
    day, _, rest = run_id.partition("_")
    return f"{day} {rest[:8].replace('-', ':')}{rest[8:]}"


def _margin(item: Optional[dict], key: str) -> str:
    """A run's worst margin with the net or stage it belongs to ("–" when the run has none)."""
    return "–" if item is None else f"{item[key]:+.1f} dB  {item.get('name', '')}"


def _pair(x, y, fmt: str) -> str:
    """'+2.0 dB → +6.5 dB'; blank when neither run has the figure (a net has no loop L, a stage no L/ideal)."""
    if not isinstance(x, (int, float)) and not isinstance(y, (int, float)):
        return ""
    one = (lambda v: "–" if not isinstance(v, (int, float)) else fmt.format(v))
    return f"{one(x)} → {one(y)}"


def _d(v, fmt: str) -> str:
    """A change for a Δ column, blank when there is none."""
    return "" if not isinstance(v, (int, float)) else fmt.format(v)


def verdict(row: dict) -> str:
    """'better', 'worse' or '' for a comparison row: a margin up (or the loop inductance down) is better; any
    figure the other way makes the row worse."""
    signs = []
    for key, limit, up_is_good in (("radiated_db", WORSE_DB, True), ("conducted_db", WORSE_DB, True),
                                   ("loop_nh", WORSE_NH, False)):
        d = row[key][2]
        if isinstance(d, (int, float)) and abs(d) >= limit:
            signs.append((d > 0) == up_is_good)
    if not signs:
        return ""
    return "better" if all(signs) else "worse"


class HistoryPanel(wx.Panel):
    """The History tab (see the module): the list of runs, a note field, the buttons, and the comparison of two
    runs (a table and the text of what changed)."""

    def __init__(self, parent):
        """The tab, empty until set_history; its background thread is started here."""
        super().__init__(parent)
        self.history: Optional[History] = None
        self.runs: list[Run] = []
        self.cell_mm: Optional[float] = None     # the newest kept run's 1x cell (mm): Preferences' Resolution sizes
        self.reports: dict[str, Optional[Path]] = {}    # run id -> its report page (looked up with the list)
        self.writing: set[str] = set()          # runs whose report page is being written
        self.comparison: Optional[dict] = None
        self._worker = InOrder("groundtruth-history", on_error=lambda e: wx.CallAfter(self._failed, e))
        self.where = wx.StaticText(self, label="The board is not loaded yet.")
        self.where.SetName("muted")
        self.buttons = {}
        btns = wx.WrapSizer(wx.HORIZONTAL)
        for key, label, tip in (
                ("compare", "Compare", "the two runs selected, or the one selected against the run before it: "
                                       "margins, loop inductances, issues fixed and new, the settings changed"),
                ("report", "Open report", "the run's report page in the web browser"),
                ("board", "Open board copy", "the board as it was simulated, unpacked to a temporary file and opened "
                                             "with the program Windows uses for .kicad_pcb files"),
                ("folder", "Open folder", "the history folder in Explorer"),
                ("delete", "Delete…", "remove the selected runs (and board copies no other run uses)")):
            b = theme.Button(self, label)
            b.SetToolTip(tip)
            b.Bind(wx.EVT_BUTTON, lambda e, k=key: self._on_button(k))
            btns.Add(b, 0, wx.RIGHT | wx.BOTTOM, 6)
            self.buttons[key] = b
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        for i, (c, w) in enumerate(COLUMNS):
            self.list.InsertColumn(i, c, width=w)
        self.list.SetToolTip("Each Simulate, newest first. Changed: what differs from the run below it (board, "
                             "settings, resolution, what-if). Ctrl-click two runs to compare them.")
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: (self._selection_changed(), e.Skip()))
        self.list.Bind(wx.EVT_LIST_ITEM_DESELECTED, lambda e: (self._selection_changed(), e.Skip()))
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._on_button("report"))
        self.note = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.note.SetHint("a note for the selected run, e.g. 'GND vias at C324/C326'")
        self.note.Bind(wx.EVT_TEXT_ENTER, lambda e: self._save_note())
        self.save_note = theme.Button(self, "Save note")
        self.save_note.Bind(wx.EVT_BUTTON, lambda e: self._save_note())
        note_row = wx.BoxSizer(wx.HORIZONTAL)
        note_row.Add(wx.StaticText(self, label="Note"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        note_row.Add(self.note, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        note_row.Add(self.save_note, 0)
        self.cmp_title = wx.StaticText(self, label="Comparison: select two runs (or one, for the run before it) and "
                                                   "press Compare")
        self.cmp_title.SetFont(self.cmp_title.GetFont().Bold())
        self.cmp_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (c, w) in enumerate(CMP_COLUMNS):
            self.cmp_list.InsertColumn(i, c, width=w)
        self.cmp_list.SetToolTip("Margin: below the limit is positive. Green: better (margins up, loop inductance "
                                 "down by more than 0.5 dB / 0.05 nH), red: something worse. Each run's worst "
                                 "harmonic is its own (no common frequency grid).")
        self.cmp_text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(-1, 130))
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self.where, 0, wx.ALL, 6)
        s.Add(btns, 0, wx.LEFT | wx.RIGHT, 6)
        s.Add(self.list, 3, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        s.Add(note_row, 0, wx.EXPAND | wx.ALL, 6)
        s.Add(self.cmp_title, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        s.Add(self.cmp_list, 2, wx.EXPAND | wx.ALL, 6)
        s.Add(self.cmp_text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.SetSizer(s)
        self._selection_changed()

    # ------------------------------------------------------------------ for the window
    def set_history(self, history: Optional[History], why: str = ""):
        """The board's history (None: none, `why` says so)."""
        self.history = history
        self.cell_mm = None                     # until the runs of this history are read
        if history is None:
            self.runs = []
            self.list.DeleteAllItems()
            self.where.SetLabel(why or "No history for this board.")
            self._selection_changed()
            return
        self.where.SetLabel(f"Runs kept in {history.root}")
        self.refresh()

    def refresh(self, select: Optional[str] = None):
        """Read the runs again (in the background), then select `select` (a run id) or keep the selection."""
        history = self.history
        if history is None:
            return
        keep = [r.id for r in self.selected()] if select is None else [select]

        def work():
            """(background) Read the runs and what changed between them, then show them on the GUI thread."""
            try:
                runs = history.runs()
                # the newest run's cell size, for Preferences' Resolution choice (before this session simulates)
                cell = next((c for c in (r.cell_mm() for r in runs[:5]) if c is not None), None)
                found = (history, runs, changes(runs), {r.id: r.report for r in runs}, keep, cell)
            except Exception as e:          # noqa: BLE001 - said in the status line, not via _failed (which
                wx.CallAfter(self._status, f"history: the runs could not be read ({e})")   # reads again)
                return
            wx.CallAfter(self._show, *found)
        self._worker.submit(work)

    def writing_report(self, run_id: str, on: bool):
        """The run's report page is being written (on) or done: its Report column says so."""
        (self.writing.add if on else self.writing.discard)(run_id)
        self.refresh()

    def busy(self) -> bool:
        """A file request is running or waiting."""
        return self._worker.busy()

    def stop(self, timeout: float = 2.0):
        """At close: drop what waits, let the running request end."""
        self._worker.finish(timeout)

    def selected(self) -> list[Run]:
        """The selected runs, newest first."""
        out, i = [], self.list.GetFirstSelected()
        while i >= 0:
            if i < len(self.runs):
                out.append(self.runs[i])
            i = self.list.GetNextSelected(i)
        return out

    # ------------------------------------------------------------------ list
    def _show(self, history: History, runs: list[Run], ch: dict, reports: dict, keep: list[str],
              cell: Optional[float] = None):
        """(GUI thread) Show the runs read in the background, selecting those in `keep`; dropped when the window
        closed or another board was loaded meanwhile. cell: the newest run's 1x cell (mm), kept for Preferences."""
        if not self or history is not self.history:      # closed, or another board since
            return
        self.runs, self.reports, self.cell_mm = runs, reports, cell
        lst = self.list
        lst.Freeze()
        try:
            lst.DeleteAllItems()
            for k, r in enumerate(runs):
                c, m = r.counts, r.meta
                report = "writing…" if r.id in self.writing else ("yes" if reports.get(r.id) else "–")
                res = m.get("resolution")
                vals = [run_label(r.id), r.note, ", ".join(ch.get(r.id, [])) or "nothing",
                        f"{res:g}×" if isinstance(res, (int, float)) else "–", "yes" if m.get("what_if") else "",
                        str(c.get("error", 0)), str(c.get("warning", 0)),
                        _margin(r.worst("radiated_db"), "radiated_db"),
                        _margin(r.worst("conducted_db"), "conducted_db"),
                        clock(m["seconds"]) if isinstance(m.get("seconds"), (int, float)) else "–", report]
                lst.InsertItem(k, vals[0])
                for col, v in enumerate(vals[1:], 1):
                    lst.SetItem(k, col, v)
                if r.id in keep:
                    lst.Select(k)
        finally:
            lst.Thaw()
        n = len(runs)
        self.where.SetLabel(f"{n} run(s) kept in {history.root}" if n else
                            f"No runs yet: each Simulate is kept in {history.root}")
        self._selection_changed()

    def _selection_changed(self):
        """Enable what the selection allows (Compare: two runs, or one with a run before it; Report: one run that
        has a page) and show the selected run's note."""
        sel = self.selected()
        one = len(sel) == 1
        self.buttons["compare"].Enable(len(sel) == 2 or (one and self.runs.index(sel[0]) + 1 < len(self.runs)))
        self.buttons["report"].Enable(one and self.reports.get(sel[0].id) is not None)
        self.buttons["board"].Enable(one)
        self.buttons["folder"].Enable(bool(self.runs))
        self.buttons["delete"].Enable(bool(sel))
        self.note.Enable(one)
        self.save_note.Enable(one)
        self.note.ChangeValue(sel[0].note if one else "")

    # ------------------------------------------------------------------ actions
    def _on_button(self, key: str):
        """A button (or a double click: report) on the selection; delete asks first."""
        sel, history = self.selected(), self.history
        if history is None:
            return
        if key == "folder":
            self._open_if_there(history.root, "the history folder", folder=True)
        elif key == "report" and len(sel) == 1 and self.reports.get(sel[0].id) is not None:
            self._open_if_there(self.reports[sel[0].id], f"the report of {run_label(sel[0].id)}")
        elif key == "board" and len(sel) == 1:
            self._open_board(history, sel[0])
        elif key == "compare":
            self._compare(history, sel)
        elif key == "delete" and sel:
            if wx.MessageBox(f"Delete {len(sel)} run(s) from the history? This cannot be undone.", "GroundTruth",
                             wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) != wx.YES:
                return

            def work():
                """(background) Delete the runs."""
                n = history.delete(sel)
                wx.CallAfter(self._status, f"deleted {n} run(s) from the history")
            self._worker.submit(work)
            self.refresh()

    def _open_if_there(self, path: Path, what: str, folder: bool = False):
        """Open a file (browser) or folder (Explorer) that may have been deleted by hand since the list was read:
        looked at in the background; gone, the status line says so and the list is read again."""
        def work():
            """(background) Is it still there? (On OneDrive this may be slow.)"""
            there = path.is_dir() if folder else path.is_file()
            wx.CallAfter(self._open_checked, path, what, folder, there)
        self._worker.submit(work)

    def _open_checked(self, path: Path, what: str, folder: bool, there: bool):
        """(GUI thread) Open it if it is there, else say it is gone and read the list again."""
        if not self:
            return
        if not there:
            self._status(f"{what} is no longer there (deleted?): {path}")
            self.refresh()
        elif folder:
            wx.LaunchDefaultApplication(str(path))
        else:
            wx.LaunchDefaultBrowser(path.as_uri())

    def _open_board(self, history: History, run: Run):
        """Unpack the run's board copy to a temporary file (the history keeps it compressed) and open it."""
        target = Path(tempfile.gettempdir()) / "GroundTruth" / "history" / f"{history.root.name}_{run.id}.kicad_pcb"

        def work():
            """(background) Unpack the board copy."""
            path = history.extract_board(run, target)
            wx.CallAfter(self._opened, path)
        self._worker.submit(work)

    def _opened(self, path: Path):
        """(GUI thread) Open the unpacked board with the program Windows uses for .kicad_pcb files."""
        if self:
            self._status(f"board copy: {path}")
            wx.LaunchDefaultApplication(str(path))

    def _compare(self, history: History, sel: list[Run]):
        """Compare two runs (one selected: with the run before it), the older first; read in the background."""
        if len(sel) == 1:
            k = self.runs.index(sel[0])
            if k + 1 >= len(self.runs):
                return
            sel = [sel[0], self.runs[k + 1]]
        if len(sel) != 2:
            return
        b, a = sorted(sel, key=self.runs.index)         # a: the older
        self.cmp_title.SetLabel(f"Comparison: {run_label(a.id)} → {run_label(b.id)}  (reading…)")

        def work():
            """(background) Read both runs' results and compare them."""
            cmp = compare(a, b)
            wx.CallAfter(self._show_compare, cmp)
        self._worker.submit(work)

    def _show_compare(self, cmp: dict):
        """(GUI thread) The comparison: one row per net or stage (green better, red worse: verdict) and the text
        of the issues fixed and new and the settings changed."""
        if not self:
            return
        self.comparison = cmp
        self.cmp_title.SetLabel(f"Comparison: {run_label(cmp['a'])} → {run_label(cmp['b'])}")
        lst = self.cmp_list
        lst.DeleteAllItems()
        pal = theme.palette()
        for k, r in enumerate(cmp["rows"]):
            if r["only"]:
                vals = [r["name"], f"only in {run_label(cmp[r['only']])}"] + [""] * 6
            else:
                rad, con, l_nh, lr = r["radiated_db"], r["conducted_db"], r["loop_nh"], r["l_ratio"]
                vals = [r["name"], _pair(rad[0], rad[1], "{:+.1f} dB"), _d(rad[2], "{:+.1f}"),
                        _pair(con[0], con[1], "{:+.1f} dB"), _d(con[2], "{:+.1f}"),
                        _pair(l_nh[0], l_nh[1], "{:.2f} nH"), _d(l_nh[2], "{:+.2f} nH"),
                        _pair(lr[0], lr[1], "{:.2f}")]
            lst.InsertItem(k, vals[0])
            for col, v in enumerate(vals[1:], 1):
                lst.SetItem(k, col, v)
            v = "" if r["only"] else verdict(r)
            if v:
                lst.SetItemTextColour(k, theme.colour(pal.ok if v == "better" else pal.error))
        self.cmp_text.SetValue(comparison_text(cmp, rows=False))

    def _save_note(self):
        """Store the note field for the selected run."""
        sel, history = self.selected(), self.history
        if history is None or len(sel) != 1:
            return
        run, text = sel[0], self.note.GetValue()

        def work():
            """(background) Write the note."""
            history.set_note(run, text)
            wx.CallAfter(self._status, "note saved")
        self._worker.submit(work)
        self.refresh(select=run.id)

    def _status(self, text: str):
        """A message in the main window's status bar."""
        if not self:
            return
        frame = wx.GetTopLevelParent(self)
        if isinstance(frame, wx.Frame) and frame.GetStatusBar() is not None:
            frame.SetStatusText(text)

    def _failed(self, e: Exception):
        """A background request failed (a file deleted by hand meanwhile, a disk error): say so, read the list
        again."""
        if self:
            self.cmp_title.SetLabel("Comparison")
            self._status(f"history: {e}")
            self.refresh()
