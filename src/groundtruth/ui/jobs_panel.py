"""Server jobs tab: the Simulate and Suggest fixes jobs this computer handed to a compute server (remote/jobs.py).

Two lists, newest first: **Current jobs** (queued with the jobs ahead, running with how far, the result here: Show
results) and **Past jobs** (shown, failed, cancelled, no longer on the server), kept until removed (the user:
"a past jobs view that shows the finished job regardless of success or error and if error a detailed error
message"). Each line names the server computer it ran on ("We have several big machines") and its elapsed time
(waiting or running, counted up every second; the run time once it ended). The box below shows everything about
the selected job, a failure's message in full (the worker's traceback), with Copy details. The lists are the
tracker's job.json files, read on refresh (a few small files)."""
from __future__ import annotations

import time
from typing import Callable, Optional

import wx

from ..remote.client import memory_text
from ..remote.jobs import PAST
from . import theme

COLUMNS = [("Handed over", 125), ("Server", 120), ("Board", 160), ("What", 100), ("Resolution", 70), ("State", 170),
           ("Elapsed", 80), ("Memory", 170), ("Details", 330)]
ELAPSED = 6                     # the column the timer refreshes
KINDS = {"simulate": "Simulate", "suggest": "Suggest fixes"}


def resolution_text(m: dict) -> str:
    """The resolution the job was handed over with ('2×', as on the History tab); '–' for a job kept before the
    resolution was recorded."""
    res = m.get("resolution")
    return f"{res:g}×" if isinstance(res, (int, float)) else "–"


def state_text(m: dict) -> str:
    """A job's state as the lists show it."""
    s = m.get("state")
    if m.get("unreachable_since") and s not in ("ready",) + PAST:
        return "server not reachable: retrying"
    if s == "queued":
        n = m.get("position") or 0
        return f"queued, {n} job(s) ahead" if n else "queued, next"
    if s == "running":
        return f"running, {float(m.get('fraction') or 0) * 100:.0f} %"
    return {"ready": "result here: Show results", "shown": "done, results shown", "failed": "FAILED",
            "cancelled": "cancelled", "lost": "no longer on the server", "done": "done, fetching the result"
            }.get(s, s or "?")


def clock_text(seconds: Optional[float]) -> str:
    """'4:07' or '1:02:33' (h:mm:ss); '–' without a time."""
    if seconds is None:
        return "–"
    s = int(max(0.0, seconds))
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def elapsed(m: dict, now: Optional[float] = None) -> Optional[float]:
    """Seconds to show: waiting in the queue (since handed over), running (the server's run time at the last
    answer, counted on since), or the run time of an ended job."""
    now = time.time() if now is None else now
    s = m.get("state")
    if s == "queued":
        return now - m.get("submitted", now)
    if s == "running":
        run, polled = m.get("run_s"), m.get("polled")
        return (run or 0.0) + (now - polled if polled else 0.0)
    return m.get("run_s")


def details(m: dict) -> str:
    """Everything about a job, for the details box (and Copy details)."""
    def when(t):
        """A time stamp, or '–'."""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "–"
    lines = [f"Job {m.get('id')}: {m.get('label') or KINDS.get(m.get('kind'), m.get('kind'))}",
             f"Server: {m.get('server_name') or '?'} ({m.get('server')})",
             f"Board: {m.get('board_name')} ({m.get('board_path') or '?'})",
             f"Resolution: {resolution_text(m)}",
             f"State: {state_text(m)}" + ("   (the window waits for it: its results go straight there; the "
                                          "progress bar's Cancel stops it)" if m.get("attached") and
                                          m.get("state") not in PAST else ""),
             f"Handed over: {when(m.get('submitted'))}   started: {when(m.get('started_at'))}   ended: "
             f"{when(m.get('ended'))}",
             f"Waited in the queue: {clock_text(m.get('queued_s'))}   ran: {clock_text(elapsed(m))}"]
    if m.get("message"):
        lines.append(f"Last step: {m['message']}")
    if m.get("note"):
        lines.append(f"Cluster: {m['note']}")             # why a Simulate ran whole on one server
    mem = memory_text(m if m.get("state") == "running" else {**m, "mem_mb": None, "commit_mb": None})
    if mem:
        lines.append(f"Memory: {mem}")
    if m.get("unreachable_since") and m.get("state") not in PAST:
        lines.append(f"Server not reachable since {when(m['unreachable_since'])}: {m.get('error', '')}")
    if m.get("state") in ("failed", "lost"):
        lines += ["", "Error:", m.get("detail") or m.get("error") or "(no message)"]
    return "\n".join(lines)


DETAILS_PREF = "jobs_details_dip"    # ui.json: the details box's height (DIP), where the user dragged it
DETAILS_DIP = 190


def details_height() -> int:
    """The details box's height from ui.json (DIP), within reason."""
    try:
        return max(60, min(2000, int(theme.prefs().get(DETAILS_PREF) or DETAILS_DIP)))
    except (TypeError, ValueError):
        return DETAILS_DIP


class ServerJobsPanel(wx.Panel):
    """The tab (see the module). on_show(job id), on_cancel(job id), on_remove(job id): the window's actions."""

    def __init__(self, parent, on_show: Callable[[str], None], on_cancel: Callable[[str], None],
                 on_remove: Callable[[str], None]):
        """The two lists, the details box and the buttons, empty until refresh(jobs); a one-second timer counts the
        elapsed times up (stopped when the tab is destroyed)."""
        super().__init__(parent)
        self.jobs: list[dict] = []
        self.current: list[dict] = []
        self.past: list[dict] = []
        self.where = wx.StaticText(self, label="Jobs handed to a compute server from this computer (Preferences, "
                                               "Compute server). They run there while you work on, or with the "
                                               "laptop asleep; a result waits there 24 h and comes here when the "
                                               "server can be reached.")
        self.where.SetName("muted")

        def button(label, tip, action, kind="default"):
            """A button acting on the selected jobs of both lists."""
            b = theme.Button(self, label, kind=kind)
            b.SetToolTip(tip)
            b.Bind(wx.EVT_BUTTON, lambda e: [action(m["id"]) for m in self.selected()])
            return b
        self.buttons = {
            "show": button("Show results", "show the selected job's results (its board must be the one open)",
                           on_show, "primary"),
            "cancel": button("Cancel job", "stop the selected job on the server (only you can: a lost connection "
                                           "never does)", on_cancel),
            "remove": button("Remove from list", "forget the selected jobs here (a result not shown yet is lost; a "
                                                 "job still on the server is cancelled there first; one on a server "
                                                 "that can't be reached after asking)", on_remove)}
        self.buttons["copy"] = theme.Button(self, "Copy details")
        self.buttons["copy"].SetToolTip("the selected job's details (and error) to the clipboard")
        self.buttons["copy"].Bind(wx.EVT_BUTTON, lambda e: self._copy())

        # the lists above, the details below, a divider between them to drag (the user: "the details job window
        # can't be resized which is a bit awkward"); where it was left is kept in ui.json (DETAILS_PREF)
        self.split = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_3DSASH)
        self.split.SetMinimumPaneSize(self.FromDIP(60))
        upper, lower = wx.Panel(self.split), wx.Panel(self.split)

        def job_list(tip: str) -> wx.ListCtrl:
            """A report list with the columns; selecting in it clears the other list's selection."""
            lst = wx.ListCtrl(upper, style=wx.LC_REPORT)
            for i, (c, w) in enumerate(COLUMNS):
                lst.InsertColumn(i, c, width=w)
            lst.SetToolTip(tip)
            lst.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e, l=lst: (self._selected_in(l), e.Skip()))
            lst.Bind(wx.EVT_LIST_ITEM_DESELECTED, lambda e: (self._show_details(), e.Skip()))
            return lst
        self.list = job_list("Queued, running, or done with the result here (double-click: Show results)")
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: [on_show(m["id"]) for m in self.selected()
                                                              if m.get("state") == "ready"])
        self.past_list = job_list("Ended jobs, kept until you remove them: select one for its details (a failure's "
                                  "error in full)")
        self.detail = wx.TextCtrl(lower, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.detail.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))

        top = wx.BoxSizer(wx.HORIZONTAL)
        for key in ("show", "cancel", "remove", "copy"):
            top.Add(self.buttons[key], 0, wx.RIGHT, 6)

        def heading(parent, text: str) -> wx.StaticText:
            """A bold section title."""
            t = wx.StaticText(parent, label=text)
            t.SetFont(t.GetFont().Bold())
            return t
        us = wx.BoxSizer(wx.VERTICAL)
        us.Add(heading(upper, "Current jobs"), 0, wx.LEFT | wx.RIGHT, 6)
        us.Add(self.list, 1, wx.EXPAND | wx.ALL, 6)
        us.Add(heading(upper, "Past jobs"), 0, wx.LEFT | wx.RIGHT, 6)
        us.Add(self.past_list, 1, wx.EXPAND | wx.ALL, 6)
        upper.SetSizer(us)
        ls = wx.BoxSizer(wx.VERTICAL)
        ls.Add(heading(lower, "Details of the selected job (drag the line above to make this taller)"), 0,
               wx.LEFT | wx.RIGHT | wx.TOP, 6)
        ls.Add(self.detail, 1, wx.EXPAND | wx.ALL, 6)
        lower.SetSizer(ls)
        # the details' height in DIP from ui.json (default DETAILS_DIP); the lists take the rest when resized
        self.split.SetSashGravity(1.0)
        self.split.SplitHorizontally(upper, lower, -self.FromDIP(details_height()))
        self.split.Bind(wx.EVT_SPLITTER_SASH_POS_CHANGED, self._sash_moved)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self.where, 0, wx.EXPAND | wx.ALL, 6)
        s.Add(top, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        s.Add(self.split, 1, wx.EXPAND)
        self.SetSizer(s)
        self.Bind(wx.EVT_SIZE, lambda e: (self.where.Wrap(max(200, self.GetClientSize().width - 12)), e.Skip()))
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self._tick(), self._timer)
        self._timer.Start(1000)
        self._id = self.GetId()
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self._enable()

    def _destroyed(self, evt):
        """Stop the timer with the tab (a timer outliving its window fires into freed memory); only for this panel's
        own destroy event (child windows' bubble up)."""
        if evt.GetId() == self._id:
            self._timer.Stop()
        evt.Skip()

    def _sash_moved(self, event):
        """The user dragged the divider: keep the details' height for the next start (ui.json, a small file)."""
        event.Skip()
        height = self.split.GetClientSize().height - self.split.GetSashPosition()
        if height > 0:
            theme.save_prefs(**{DETAILS_PREF: int(self.ToDIP(height))})

    def refresh(self, jobs: list[dict]):
        """Show `jobs` (LocalJobs.all()) in the two lists, keeping the selection."""
        keep = {m["id"] for m in self.selected()}
        self.jobs = list(jobs)
        self.current = [m for m in self.jobs if m.get("state") not in PAST]
        self.past = [m for m in self.jobs if m.get("state") in PAST]
        for lst, rows in ((self.list, self.current), (self.past_list, self.past)):
            lst.Freeze()
            try:
                lst.DeleteAllItems()
                for k, m in enumerate(rows):
                    lst.InsertItem(k, time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("submitted", 0))))
                    for col, text in enumerate((m.get("server_name") or m.get("server", ""), m.get("board_name", ""),
                                                (m.get("label") if m.get("attached") else None)
                                                or KINDS.get(m.get("kind"), m.get("kind", "")), resolution_text(m),
                                                state_text(m),
                                                clock_text(elapsed(m)), self._memory(m), self._summary(m)),
                                               start=1):
                        lst.SetItem(k, col, text)
                    if m.get("state") in ("failed", "lost"):
                        lst.SetItemTextColour(k, theme.colour(theme.palette().warn))
                    if m["id"] in keep:
                        lst.Select(k)
            finally:
                lst.Thaw()
        self._show_details()

    @staticmethod
    def _memory(m: dict) -> str:
        """The Memory column: what a running job uses and what the server has free; an ended job's peak."""
        if m.get("state") == "running":
            return memory_text(m, short=True)
        return f"peak {m['peak_mb'] / 1024:.1f} GB" if m.get("peak_mb") else ""

    @staticmethod
    def _summary(m: dict) -> str:
        """The Details column: the error's first line, the server's last step, or the label."""
        if m.get("state") in ("failed", "lost") or (m.get("unreachable_since") and m.get("state") not in PAST):
            return (m.get("error") or "").split("\n")[0]
        return m.get("message") or m.get("label", "")

    def _tick(self):
        """Every second: the elapsed times of the jobs still waiting or running."""
        if not self or not self.IsShownOnScreen():
            return
        for k, m in enumerate(self.current):
            if m.get("state") in ("queued", "running"):
                self.list.SetItem(k, ELAPSED, clock_text(elapsed(m)))

    def _selected_in(self, lst):
        """A selection in one list clears the other's (the buttons and the details are for one list at a time)."""
        other = self.past_list if lst is self.list else self.list
        k = other.GetFirstSelected()
        while k != -1:
            other.Select(k, False)
            k = other.GetNextSelected(k)
        self._show_details()

    def selected(self) -> list[dict]:
        """The selected jobs' metas (of either list)."""
        out = []
        for lst, rows in ((self.list, self.current), (self.past_list, self.past)):
            k = lst.GetFirstSelected()
            while k != -1:
                if k < len(rows):
                    out.append(rows[k])
                k = lst.GetNextSelected(k)
        return out

    def _show_details(self):
        """The details box for the selection, and the buttons."""
        sel = self.selected()
        self.detail.SetValue("\n\n".join(details(m) for m in sel) if sel else
                             "Select a job for its details: where and how long it ran, and a failure's error in "
                             "full.")
        self._enable()

    def _copy(self):
        """The selected jobs' details to the clipboard."""
        sel = self.selected()
        if sel and wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject("\n\n".join(details(m) for m in sel)))
            finally:
                wx.TheClipboard.Close()

    def _enable(self):
        """The buttons for what is selected."""
        sel = self.selected()
        self.buttons["show"].Enable(any(m.get("state") == "ready" for m in sel))
        self.buttons["cancel"].Enable(any(m.get("state") in ("queued", "running") for m in sel))
        self.buttons["remove"].Enable(bool(sel))
        self.buttons["copy"].Enable(bool(sel))

    def count_active(self) -> tuple[int, int]:
        """(jobs on the server, results here not shown yet)."""
        return (sum(1 for m in self.jobs if m.get("state") in ("queued", "running")),
                sum(1 for m in self.jobs if m.get("state") == "ready"))


def find(jobs: list[dict], jid: str) -> Optional[dict]:
    """The job with this id, or None."""
    return next((m for m in jobs if m["id"] == jid), None)
