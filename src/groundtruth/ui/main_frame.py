"""Main plugin window: Nets | Parts | Issues | Heatmap | Fixes.

Threading: the GUI thread only handles events and shows results. Everything that takes time is a *job* that
runs in the one worker thread (`MainFrame._start`): reading the board (over IPC or from the file), checks,
the simulation, the fix search, applying what-if edits, contour computation and every call into KiCad. One
job runs at a time, so KiCad's API connection is never used by two threads. Jobs report progress with
wx.CallAfter; the analysis jobs can be cancelled, because their progress callback raises `Cancelled` once
the Cancel button was pressed. (Measured on a 4-layer board: the worker thread never held up the GUI thread
for more than 80 ms during a 90 s simulation; what froze the window was work done on the GUI thread itself.)
"""
from __future__ import annotations

import functools
import math
import os
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

import wx

from ..config.settings import ConfigError, SettingsFile, settings_path, validate
from ..core.issues import sort_issues
from . import startup, theme
from .bridges_panel import BridgesPanel
from .dcloads_panel import DcLoadsPanel
from .pdn_panel import PdnPanel
from .si_panel import SiPanel
from .converters_panel import ConvertersPanel
from .background import InOrder, LatestOnly
from .gil import SolverInterval, freeze_long_lived
from .inbox import NO_SELECTION, Inbox
from .saver import BackgroundSaver
from .fixes_panel import FixesPanel
from .history_panel import HistoryPanel
from .jobs_panel import ServerJobsPanel
from .nets_panel import NetsPanel
from .parts_panel import PartsPanel
from .progress import ProgressClock, clock, release_failure
from .results_panels import HeatmapPanel, IssuesPanel

BOARD_ARROW_SPACING_MM = 0.8   # spot spacing of the current arrows drawn on the board (arrows 0.56 mm long)
MAX_BOARD_ARROWS = 1500        # three graphic segments each; the spacing grows beyond that
FOLLOW_MS = 500                # the Parts tab looks at KiCad's selection this often (only while it shows)
STATUS_LINES = 3               # a long status message wraps onto up to this many lines (the bar grows)

# errors whose message is enough for the user (others also get the traceback: they are bugs)
EXPECTED_ERRORS = {"KiCadBusyError", "KiCadConnectionError", "ApiError", "ConnectionError", "BoardLoadError",
                   "SExprError",
                   "ConfigError", "ModificationError", "SolverError", "OSError", "FileNotFoundError",
                   "PermissionError", "DrawingLayerError", "MemoryError", "ThermalMemoryError", "GpuUnavailable",
                   "ServerUnavailable", "RemoteError"}


def report_stem(board, settings_path: Optional[Path]) -> str:
    """The exported report's file name stem: the board's (from its settings file, which sits next to it, also in
    live mode, where the board's source reads "kicad-ipc:<name>"), with only letters, digits, '_', '.', '-': a ':'
    is not allowed in a Windows file name."""
    if settings_path is not None:
        name = Path(settings_path).name.split(".groundtruth")[0]
    else:
        src = board.source or "board"
        name = src.split(":", 1)[1] if src.startswith("kicad-ipc:") else Path(src).stem
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_.") or "board"


def solver_note(solver: Optional[dict]) -> str:
    """ · solved on GPU <name>" after a run whose solves went to a GPU (or fell back from one); "" on the CPU;
    then " · on the compute server <host>" for a run there (its "server" entry, set by the job)."""
    where = (solver or {}).get("server", "")
    if not solver or (solver.get("device", "CPU") == "CPU" and not solver.get("gpu_fallbacks")):
        return where
    from ..solver.gpu import describe
    return f" · solved on {describe(solver)}" + where


class Cancelled(Exception):
    """Raised inside a job's progress callback after the user pressed Cancel."""


class HandedOver:
    """A job's result when it went to the compute server detached: its meta in the jobs folder and the host."""

    def __init__(self, meta: dict, host: str):
        """The job just handed over."""
        self.meta, self.host = meta, host


def simulate_on(server: Optional[tuple[str, str]], board, cfg, progress, nets=None, watch=None):
    """(worker) A Simulate here (its factorisations dropped at once) or, with `server` = (address, access key)
    from Preferences, on the compute server (remote/: the result comes without models; ServerUnavailable when
    the server can't take the job; `watch`: MainFrame._watch, the job listed on the Server jobs tab)."""
    if server is None:
        from ..analysis.simulate import simulate
        res = simulate(board, cfg, nets=nets, progress=progress)
        res.drop_factorisations()
        return res
    from ..remote.client import Remote
    return Remote(*server).run("simulate", (board, cfg, nets), progress, watch=watch)


def settings_key(cfg) -> Optional[str]:
    """The settings as far as a Simulate's results depend on them, as text to compare: everything but the drawing,
    report and thermal sections (Thermal reads its own section when it runs), the waivers (applied to the issues
    shown), the what-if edits and this computer's choices (resolution, threads, GPU). Two runs' keys differ when the
    settings changed in a way that changes the results (VALIDATION_AUDIT.md round 6, ISSUE-29). None for None."""
    if cfg is None:
        return None
    from dataclasses import replace
    a = replace(cfg.analysis, resolution=1.0, threads="auto", gpu="auto", all_cores=False)
    return repr(replace(cfg, analysis=a, drawing=None, report=None, thermal=None, waivers=[], modifications=[],
                        base_dir=""))


def result_views(res) -> list:
    """The views a Simulate's result adds after its routes: every power stage, each DC path's view (the stages'
    currents together first), each bridge with its legs, RMS currents and sweep, and each DC rail with loads (a
    DcLoadResult is its own view: its DC map)."""
    from ..analysis.bridge import rms_view, sweep_view
    from ..analysis.power import COMBINED_VIEW, dc_view
    views = [dc_view(cr, p["name"]) for cr in res.converters for p in cr.dc]
    if res.dc_combined is not None:             # every stage's DC paths together (power.combine_dc)
        views.insert(0, dc_view(res.dc_combined, COMBINED_VIEW))
    stages = [cr for cr in res.converters if not getattr(cr, "bridge", None)]
    for br in res.bridges:                      # each bridge's drive view, its legs, RMS currents, sweep
        legs = [cr for cr in res.converters if getattr(cr, "bridge", None) == br.net]
        more = [rms_view(br, legs[0]) if legs else None, sweep_view(br)]
        stages += [br] + legs + [v for v in more if v is not None]
    loads = [d for d in getattr(res, "dc_loads", ()) if not d.error]
    rails = [p for p in getattr(res, "pdn", ()) if not p.error and p.ports]     # each PDN rail: its curves
    from ..analysis.transfer import bode_views
    return stages + loads + rails + views + bode_views(res)     # the responses' Bode and pole-zero views last


def portable_keep(keep):
    """The history's inputs of a Simulate (MainFrame._history_inputs) as plain data that can wait on disk with a job
    handed to the compute server: without the History object (made again from the board's path when the result is
    shown) and with the settings' text taken now. A reason (str) or None stays as it is."""
    if not isinstance(keep, dict):
        return keep
    out = {k: v for k, v in keep.items() if k not in ("history", "settings", "t0")}
    out["settings_text"] = keep["settings"]()
    return out


def server_note(server: Optional[tuple[str, str]]) -> str:
    """ · on the compute server <host>" for a job that ran there."""
    if server is None:
        return ""
    from ..remote.client import parse_address
    return f" · on the compute server {parse_address(server[0])[len('http://'):]}"


REPORT_CLOSE_WAIT_S = 60.0     # closing the window: how long a history report still being written may finish
SETTLE_POLL_MS = 500           # new results: how often to look whether the user is idle for the garbage collection
KEEP_MODELS_SHARE = 0.25       # Simulate's models are kept for Suggest fixes while they take less than this share
                               # of the memory still available (keep_models)


def keep_run(keep: dict, res, cfg, progress):
    """In the Simulate job, after the solve: the run in the board's history (report/history.py): the board copy
    taken at load, the settings as Simulate started, the results. Returns the Run, or why it was not kept (that
    never fails the Simulate). keep: MainFrame._history_inputs."""
    from .. import __version__
    from ..report import formats
    from ..report.history import HistoryError, cell_mm_of, run_meta
    progress("keeping the run in the history…", 1.0)
    try:
        pitches = [r.grid.d for r in list(res.routes) + list(res.converters) if getattr(r, "grid", None) is not None]
        meta = run_meta(keep["path"], keep["source"], by="plugin", resolution=cfg.analysis.resolution,
                        what_if=keep["what_if"], edits=keep["edits"], seconds=time.monotonic() - keep["t0"],
                        solver=res.solver, cell_mm=cell_mm_of(pitches, cfg.analysis.resolution))
        text = formats.to_json(keep["path"], sort_issues(res.issues), res.routes,
                               {"mode": keep["mode"], "version": __version__}, res.converters, res.bridges,
                               list(getattr(res, "dc_loads", ())), list(getattr(res, "pdn", ())))
        return keep["history"].record(keep["board"], keep["settings"](), text, meta)
    except (OSError, HistoryError, ValueError) as e:
        return f"{type(e).__name__}: {e}"


def wrap_words(text: str, width: float, extents) -> list[str]:
    """text over lines at most `width` wide, broken between words (a word wider than a line keeps a line of its
    own). extents(s): the cumulative widths of s's characters, one wx call per paragraph: every wx call gives up
    the GIL, and beside a busy job taking it back costs a timer tick, so wx.lib.wordwrap's call per word stalled
    the window for up to 0.7 s."""
    import re
    lines = []
    for para in text.split("\n"):
        ext = list(extents(para)) if para else []
        start = 0
        for m in re.finditer(r"\S+", para):
            left = ext[start - 1] if start > 0 else 0
            if m.start() > start and ext[m.end() - 1] - left > width:
                lines.append(para[start:m.start()].rstrip())
                start = m.start()
        lines.append(para[start:].rstrip())
    return lines


def _kicad_call(fn):
    """KiCad's API connection (one socket) must never be used by two threads at once: every Session method that
    talks to KiCad holds the session's lock (jobs run in the worker thread, issue highlights in their own)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        """fn, holding the session's KiCad lock."""
        with self.kicad_lock:
            return fn(self, *args, **kwargs)
    return wrapper


def keep_models(res) -> bool:
    """Keep a Simulate's models and solutions for Suggest fixes (it starts from them) only while they take less than
    KEEP_MODELS_SHARE of the memory still available: kraft_flux_mini_2 with every net and stage kept ~2.3 GB after
    the run. Otherwise they go, and Suggest fixes simulates its own baseline (as when the settings changed).
    Without psutil (memory unknown) they are kept."""
    from ..core.sysmem import available_bytes
    free = available_bytes()
    return free is None or res.model_bytes() < KEEP_MODELS_SHARE * free


class Session:
    """Where the board comes from and whether results can be drawn back (live KiCad vs file).
    Every method may block (IPC, file I/O): call them from a job or a background thread, never from the GUI
    thread."""

    def __init__(self, kicad_board=None, file_path: Optional[str] = None):
        """Live: kicad_board, kipy's handle of the board open in KiCad. File mode: file_path, a .kicad_pcb."""
        self.kb = kicad_board
        self.file_path = file_path
        self.kicad_lock = threading.RLock()
        self.net_lit = False                # a net highlight set by show_issue is on in KiCad
        self.saved_layer = None             # the user's active layer (KiCad's id) while show_issue changed it

    @property
    def live(self) -> bool:
        """Running inside KiCad (results can be drawn on the board)."""
        return self.kb is not None

    @property
    def name(self) -> str:
        """The board's file name without .kicad_pcb (no IPC: kipy keeps it with the board handle)."""
        try:
            n = self.kb.name if self.live else Path(self.file_path).name
        except Exception:
            return ""
        return n[:-len(".kicad_pcb")] if n.endswith(".kicad_pcb") else n

    @_kicad_call
    def load(self):
        """(the board model, the path it is saved at): read over KiCad's API (with unsaved edits) or from the
        file. The path is None for a board KiCad has never saved."""
        if self.live:
            from ..adapters.kipy_source import board_file_path, read_board
            board = read_board(self.kb)
            path = board_file_path(self.kb)
            return board, path
        from ..adapters.file_source import load_board
        return load_board(self.file_path), self.file_path

    @_kicad_call
    def board_copy(self, path: Optional[str]) -> tuple[Optional[bytes], str]:
        """(the board's file text, where it came from) for the simulation history, read right after load() so it
        is the board analysed: live, KiCad's board with any unsaved edits ('kicad', a read-only request); if KiCad
        cannot hand it over, the saved file ('saved file'); in file mode the file ('file'). (None, why) if
        neither works."""
        if self.live:
            try:
                from ..adapters.kipy_source import board_text
                return board_text(self.kb).encode("utf-8"), "kicad"
            except Exception as e:          # noqa: BLE001 - an older KiCad, a message too big: the saved file
                why = f"{type(e).__name__}: {e}"
            try:
                return Path(path).read_bytes(), "saved file"
            except (OSError, TypeError) as e:
                return None, f"{why}; {e}"
        try:
            return Path(self.file_path).read_bytes(), "file"
        except OSError as e:
            return None, str(e)

    @_kicad_call
    def refill(self):
        """Refill every zone in KiCad (live only)."""
        if self.live:
            from ..adapters.kipy_sink import refill_zones
            refill_zones(self.kb)

    @_kicad_call
    def draw_issues(self, issues, layer, hidden=frozenset()):
        """Issue markers on `layer` (replacing the previous ones), none for the `hidden` checks; the number of items
        drawn."""
        from ..adapters.kipy_sink import draw_issues
        return draw_issues(self.kb, issues, layer, hidden=hidden)

    def enabled_layers(self) -> list[str]:
        """Drawing layers the last draws had to enable on the board (kipy_sink._drawing_layer)."""
        if not self.live:
            return []
        from ..adapters.kipy_sink import take_enabled
        return take_enabled()

    @_kicad_call
    def draw_contours(self, cmap, layer, label, line_mm: float = 0.04, index_mm: float = 0.1):
        """A contour map (results.contours.ContourMap: current density or isotherms) on `layer`, its lines line_mm
        and its index lines index_mm wide; (lines, labels) drawn."""
        from ..adapters.kipy_sink import draw_contours
        return draw_contours(self.kb, cmap, layer, label, line_mm, index_mm)

    @_kicad_call
    def draw_arrows(self, arrows, layer, label):
        """Current arrows on `layer`; the number drawn."""
        from ..adapters.kipy_sink import draw_arrows
        return draw_arrows(self.kb, arrows, layer, label)

    @_kicad_call
    def draw_suggestions(self, suggestions, layer):
        """The suggested fixes (crosses and labels) on `layer`; the number of items drawn."""
        from ..adapters.kipy_sink import draw_suggestions
        return draw_suggestions(self.kb, suggestions, layer)

    @_kicad_call
    def place_vias(self, mods):
        """Real vias on the board for these what-if via edits (one undo step); the number placed."""
        from ..adapters.kipy_sink import place_vias
        return place_vias(self.kb, mods)

    @_kicad_call
    def clear(self):
        """Remove everything GroundTruth drew on the board; the number of items removed."""
        from ..adapters.kipy_sink import clear
        return clear(self.kb)

    def show_issue(self, h, zoom: bool = True, nets: bool = True, wait: float = 2.0, lock_wait: float = 0.5) -> dict:
        """Select, highlight and zoom to an issue's copper in KiCad (h: analysis.highlight.Highlight). Refused
        when a KiCad job holds the connection for longer than `lock_wait`: a show queued behind it would move the
        user's view seconds after the click. Keeps track of the highlight we set and of the user's active layer,
        which comes back with the next issue that has no layer of its own (and at close)."""
        from ..adapters.kipy_source import KiCadBusyError
        from ..adapters.kipy_sink import show_items
        if not self.kicad_lock.acquire(timeout=lock_wait):
            raise KiCadBusyError("GroundTruth is still busy in KiCad (a job is running): click again when it is "
                                 "done")
        try:
            target = h.layer or self.saved_layer
            r = show_items(self.kb, h.lit, h.focus, h.context, nets=nets and h.nets, zoom=zoom,
                           zoom_out=h.zoom_out, layer=target, clear_old=self.net_lit, wait=wait)
            self.net_lit = r["lit"]
            if h.layer:
                if self.saved_layer is None:
                    self.saved_layer = r["previous_layer"]
            elif target is not None and r["previous_layer"] is not None:
                self.saved_layer = None             # given back
            return r
        finally:
            self.kicad_lock.release()

    def clear_highlight(self, wait: float = 0.3, lock_wait: float = 2.5):
        """At close: clear our net highlight and give back the user's active layer (best effort)."""
        from ..adapters.kipy_sink import clear_highlight
        if not (self.net_lit or self.saved_layer is not None) or not self.kicad_lock.acquire(timeout=lock_wait):
            return
        try:
            clear_highlight(self.kb, layer=self.saved_layer, wait=wait)
            self.net_lit, self.saved_layer = False, None
        finally:
            self.kicad_lock.release()

    def selected_parts(self) -> Optional[list[str]]:
        """References of the footprints selected in KiCad, for the Parts tab to follow; None when KiCad or our
        connection is busy right now (a job, a show, a dialog in KiCad): polling skips that look instead of
        waiting."""
        if not self.live or not self.kicad_lock.acquire(blocking=False):
            return None
        try:
            from ..adapters.kipy_source import KiCadBusyError, selected_references
            try:
                return selected_references(self.kb, wait=0.0)
            except KiCadBusyError:
                return None
        finally:
            self.kicad_lock.release()

    @_kicad_call
    def selected_pad_ids(self) -> list[str]:
        """Ids of the pads selected in KiCad (empty in file mode): a path picked there starts or ends at them."""
        if not self.live or self.kb is None:
            return []
        from ..adapters.kipy_source import selected_pad_ids
        return selected_pad_ids(self.kb)

    @_kicad_call
    def write_symbol_labels(self, labels: dict[str, str]) -> tuple[list[str], str]:
        """Write GT labels into the open schematic: (the references written, "") or ([], why KiCad couldn't, e.g.
        KiCad 10 has no schematic API). Never raises for that: the caller shows the labels instead."""
        if not self.live or self.kb is None:
            return [], "GroundTruth runs on a board file, not in KiCad"
        from ..adapters.kipy_sink import SchematicApiUnavailable, write_symbol_labels
        try:
            return write_symbol_labels(self.kb, labels), ""
        except SchematicApiUnavailable as e:
            return [], str(e)

    @_kicad_call
    def selected_nets(self) -> list[str]:
        """Nets of the items selected in KiCad (empty in file mode)."""
        if not self.live:
            return []
        from ..adapters.kipy_source import selected_nets
        return selected_nets(self.kb)


def _open_settings(board, path) -> tuple[SettingsFile, bool, bool]:
    """(settings, new, dirty) for the board saved at `path`: the settings file next to it, updated for board
    changes, or a new one (not written yet). Runs in a job."""
    sp = settings_path(path)
    if not sp.exists():
        return SettingsFile.new(sp, board), True, True
    settings = SettingsFile.load(sp)
    changes = settings.merge_board(board)
    return settings, False, any(changes.values())


class StatusBar(wx.StatusBar):
    """The window's status bar. Its fields' texts are kept here: GetStatusText reads them as wx's does, but the
    native field 0 stays empty, because MainFrame's word-wrapped label draws that text. The native single line
    showed through beside the label when the bar was two lines tall (its first letters peeked out at the left
    edge between the label's lines)."""

    def __init__(self, parent):
        """A status bar for parent, its texts empty."""
        super().__init__(parent)
        self._texts: dict[int, str] = {}

    def SetStatusText(self, text, number=0):
        """Keep the text; the native field gets it except field 0 (the label's)."""
        self._texts[number] = text
        super().SetStatusText("" if number == 0 else text, number)

    def GetStatusText(self, number=0):
        """The text last set for the field."""
        return self._texts.get(number, "")


class MainFrame(wx.Frame):
    """The plugin window: a top bar of actions (Reload, Refill, Run checks, Simulate, Thermal, Export, Save, Clear,
    Preferences, Help), the tabs, and a status bar with a progress gauge.

    The GUI thread only handles the window. Everything slow runs elsewhere: analyses and KiCad calls as jobs in
    one worker thread (_start; one job at a time, the buttons disabled meanwhile, Cancel stops it at its next
    progress step), settings saves, heatmap renders, issue highlights and history requests in background threads
    of their own (ui/background.py). Results come back to the GUI thread with wx.CallAfter."""

    def __init__(self, session: Session):
        """The window with its top bar and status bar; the tabs are built afterwards, one per event-loop step
        (_build_pages), and then the board is read as a job."""
        name = session.name
        super().__init__(None, title=f"{name} – GroundTruth" if name else "GroundTruth", size=(1200, 820))
        self._window_id = self.GetId()
        self.session = session
        self.board = None
        self.settings: Optional[SettingsFile] = None
        self.issues = []
        self.routes = []
        self.dirty = False
        self.worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._last_sim = None       # (board, config, SimulationResult) of the last plain Simulate run
        self._views_from = None     # (board, config) the heatmap views were computed with (Sharper map)
        self._views_src = None      # (board fingerprint, settings_key) the views came from: _stale_reason
        self.board_path: Optional[str] = None
        self._board_copy: tuple[Optional[bytes], str] = (None, "")   # the board's text as loaded, for the history
        self._board_fp: Optional[str] = None    # Board.fingerprint() of self.board when it was read (run's check)
        self._after_load: Optional[Callable] = None     # what a reload started by run() continues with

        # top bar of drawn buttons (theme.Button: KiCad's native ones look dated), Preferences and Help on the right
        self.topbar = top = wx.Panel(self)
        bar = wx.BoxSizer(wx.HORIZONTAL)

        def button(label, handler, kind="default", tip=""):
            """A top-bar button bound to handler."""
            b = theme.Button(top, label, kind)
            b.Bind(wx.EVT_BUTTON, handler)
            if tip:
                b.SetToolTip(tip)
            bar.Add(b, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
            return b
        self.t_reload = button("Reload board", self.on_reload)
        self.t_refill = button("Refill zones", self.on_refill) if session.live else None
        bar.AddSpacer(12)
        self.t_check = button("Run checks", lambda e: self.run(False))
        self.t_sim = button("Simulate", lambda e: self.run(True), "primary")
        self.t_thermal = button("Thermal", lambda e: self.on_thermal(),
                                tip="copper and part temperatures of the power stages from the last Simulate run "
                                    "(their DC paths and losses heat the board; settings: thermal)")
        self.t_export = button("Export report…", lambda e: self.on_export(),
                               tip="the results shown as one HTML page to share or print: summary, issues, every "
                                   "net, power stage and bridge with its figures, temperatures, fixes")
        self.whatif = wx.CheckBox(top, label="with what-if edits")
        self.whatif.SetToolTip("Run checks / Simulate on a copy of the board with the settings's enabled what-if "
                               "edits (virtual stitching vias, capacitors, fills)")
        bar.Add(self.whatif, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 8)
        self.t_cancel = button("Cancel", self.on_cancel, "danger")
        bar.AddSpacer(12)
        button("Save settings", self.on_save)
        self.t_clear = button("Clear EMI layers", self.on_clear) if session.live else None
        bar.AddStretchSpacer()
        button("Preferences…", lambda e: self.on_preferences(),
               tip="your settings on this computer, for every board: Simulate's resolution, the heatmaps' via "
                   "markers and part outlines, the theme")
        button("Help", lambda e: self.on_help(), tip="what each tab does and how the analyses fit together (F1)")
        frame_top = wx.BoxSizer(wx.VERTICAL)
        frame_top.Add(bar, 1, wx.EXPAND | wx.ALL, 8)
        top.SetSizer(frame_top)
        self.t_cancel.Enable(False)
        self._help = None
        self._solver = None
        self._cell_mm = None        # the last Simulate's coarsest cell at 1× (Preferences shows each level's)
        help_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, lambda e: self.on_help(), id=help_id)
        self.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_NORMAL, wx.WXK_F1, help_id)]))
        line = wx.Panel(self, size=(-1, 1))
        line.SetBackgroundColour(theme.colour(theme.palette().border))

        # the tabs are built one per event-loop step (_build_pages): their ~40 native controls each cost the GUI
        # thread ~2 ms apiece, ~0.25 s in all, which in one block is a visible stall when the window opens
        self.notebook = wx.Notebook(self)
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(top, 0, wx.EXPAND)
        root.Add(line, 0, wx.EXPAND)
        root.Add(self.notebook, 1, wx.EXPAND)
        self.SetSizer(root)
        self._pages = [
            ("nets", "Signal paths", lambda nb: NetsPanel(
                nb, self.on_config_change, self._panel_job,
                on_add_selection=self.on_add_selection if session.live else None,
                on_show=self.on_nets_selected if session.live else None, in_steps=True)),
            ("converters", "Converters", lambda nb: ConvertersPanel(
                nb, self.on_config_change, self._panel_job,
                on_show=self.on_stages_selected if session.live else None, in_steps=True)),
            ("bridges", "Bridges", lambda nb: BridgesPanel(
                nb, self.on_config_change, self._panel_job,
                on_show=self.on_stages_selected if session.live else None, in_steps=True)),
            ("dc_loads", "DC loads", lambda nb: DcLoadsPanel(nb, self.on_config_change)),
            ("pdn", "Power integrity", lambda nb: PdnPanel(nb, self.on_config_change, in_steps=True)),
            ("si", "Signal integrity", lambda nb: SiPanel(nb, self.on_config_change, in_steps=True)),
            ("parts", "Parts", lambda nb: PartsPanel(nb, self.on_config_change)),
            ("issues_panel", "Issues", lambda nb: IssuesPanel(nb, self.on_issue_selected, self.on_draw_issues,
                                                             self.on_waive, live=session.live)),
            ("heatmap", "Heatmap", lambda nb: HeatmapPanel(nb, self.on_draw_contours, self.on_draw_arrows,
                                                           self.on_sharper, self.on_capacity, in_steps=True,
                                                           on_mark=self.on_map_mark)),
            ("fixes", "Fixes", lambda nb: FixesPanel(nb, self.on_suggest, self.on_draw_suggestions,
                                                     self.on_place_vias, self.on_add_whatif, self.on_compare,
                                                     self.on_check_design)),
            ("history", "History", lambda nb: HistoryPanel(nb)),
            ("server_jobs", "Server jobs", lambda nb: ServerJobsPanel(nb, self.on_show_job, self.on_cancel_job,
                                                                      self.on_remove_job))]

        self.status = StatusBar(self)
        self.status.SetFieldsCount(2)
        self.SetStatusBar(self.status)
        self.status.SetStatusWidths([-3, -1])
        # field 1 holds the gauge, which covers it: nothing is written there (text under it peeked out below the
        # gauge when the bar was two lines tall)
        self.gauge = wx.Gauge(self.status, range=1000, size=(200, -1))
        # the native field shows one line: a long message (a refused net's reason) is drawn wrapped by this label
        # over field 0 instead, the bar growing to STATUS_LINES lines; the text is kept by StatusBar (GetStatusText)
        self.status_label = wx.StaticText(self.status, style=wx.ST_NO_AUTORESIZE)
        self._status_lines = 1
        # what _wrap_status last set, and the label font's character widths (_char_extents)
        self._status_shown, self._status_tip, self._status_size, self._status_rect = None, None, None, None
        self._status_widths: dict[str, int] = {}
        self.Bind(wx.EVT_SIZE, self._place_gauge)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._stop_timers)      # also when destroyed without closing
        self._saver = BackgroundSaver(on_error=lambda e: wx.CallAfter(self._save_failed, e))
        # each Simulate's report page for the history, made after its results show (report/history.py)
        self._history_reports = InOrder("groundtruth-history-report",
                                        on_error=lambda e: wx.CallAfter(self._history_report_failed, e))
        self._closing = threading.Event()   # set in on_close: a report page waiting for an idle user goes on at once
        self._highlighter = LatestOnly("groundtruth-highlight",           # clicked issues shown in KiCad
                                       on_error=lambda e: wx.CallAfter(self._highlight_failed, e))
        self._highlighted = False
        self._solver_interval = SolverInterval()
        self._inbox = None                    # nets sent by KiCad's "Add selected net" button (ui/inbox.py)
        self._inbox_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _: self._check_inbox(), self._inbox_timer)
        # the Parts tab follows the footprints selected in KiCad (KiCad has no selection event for plugins: a
        # look every FOLLOW_MS while that tab shows, in its own thread, skipped while KiCad or a job is busy)
        self._follower = LatestOnly("groundtruth-follow", on_error=lambda e: None)
        self._kicad_parts: Optional[list[str]] = None
        self._follow_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _: self._follow_kicad_selection(), self._follow_timer)
        if session.live:
            self._follow_timer.Start(FOLLOW_MS)
        # a running job's status line: message · percent · elapsed · about how long is left, redrawn every second
        # so the elapsed time keeps counting through a long single step (ui/progress.py)
        self._clock = ProgressClock()
        self._clock_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _: self._tick_clock(), self._clock_timer)
        # after new results show: one garbage collection once the user is idle, then they are frozen (gil.py)
        self._settle_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _: self._settle_when_idle(), self._settle_timer)
        self._kicad_watch = None
        if session.live:                      # close with KiCad instead of outliving it
            from .kicad_watch import watch_kicad
            gone = watch_kicad()
            if gone is not None:
                self._kicad_watch = wx.Timer(self)
                self.Bind(wx.EVT_TIMER, lambda _: self._close_if_kicad_gone(gone), self._kicad_watch)
                self._kicad_watch.Start(1000)
        theme.style_tree(self)
        self.status_label.SetForegroundColour(theme.colour(theme.palette().text))
        # jobs handed to the compute server, followed in the background whatever the window does (remote/jobs.py,
        # the Server jobs tab); a job's results show by themselves only in the window that handed it over
        import secrets
        from .job_store import SafeJobs, SyncTracker, close_stale
        self._session_token = secrets.token_hex(8)
        self._shown_at = 0.0                    # when results were last shown here
        # SafeJobs / SyncTracker (ui/job_store.py): several windows share the list safely, and it is rebuilt from
        # the servers (the user: "current jobs are lost after a while")
        self.remote_jobs = SafeJobs()
        close_stale(self.remote_jobs)           # waited-for jobs of a window closed meanwhile: past jobs
        self._tracker = SyncTracker(self.remote_jobs, self._remote_for, lambda m: wx.CallAfter(self._job_changed, m),
                                    servers=self._job_servers,
                                    on_round=lambda t: wx.CallAfter(self._jobs_synced, t))
        self._tracker.start()
        # the window appears at once; its tabs follow step by step, then the board is read in the background
        wx.CallAfter(self._build_pages)

    def _build_pages(self):
        """One tab per call, in tab order, each in its own event-loop step (a CallLater timer, so the GUI handles
        paint and input in between; it belongs to no window, and a window closed meanwhile only ends the chain).
        The board is read once the last tab exists: nothing touches a tab before that."""
        if not self:
            return
        building = getattr(self, "_building", None)
        if building is not None:
            # a panel with more to build (its `build_steps`, e.g. the Bridges tab's form): one step per call, still
            # in the holder (the Bridges tab in one step blocked the GUI thread 0.32-0.43 s, at the stall limit)
            attr, label, panel = building
            panel.build_steps.pop(0)()
        else:
            attr, label, build = self._pages.pop(0)
            # built in a hidden holder and then moved: controls created inside a shown window cost 3x as much
            # (Converters: 316 ms there, ~110 ms hidden + 20 ms to move; Freeze does not help)
            if getattr(self, "_holder", None) is None:
                self._holder = wx.Frame(self)       # never shown
            panel = build(self._holder)
        if getattr(panel, "build_steps", None):
            self._building = (attr, label, panel)
            wx.CallLater(1, self._build_pages)
            return
        self._building = None
        theme.style_tree(panel)                 # the palette (theme.py), while it is still hidden
        panel.Reparent(self.notebook)
        setattr(self, attr, panel)
        self.notebook.AddPage(panel, label)
        if self._pages:
            wx.CallLater(1, self._build_pages)
        else:
            self._holder.Destroy()
            self._holder = None
            self.notebook.SetSelection(0)
            freeze_long_lived()             # garbage collection walks only what comes after (ui/gil.py)
            self.on_reload()

    def pages_built(self) -> bool:
        """Every tab has been built (and the board read was started)."""
        return not self._pages and getattr(self, "_building", None) is None

    def _check_inbox(self):
        """Timer: nets sent from KiCad's "Add selected net" button for this board."""
        # not while a job runs: nets added then wait in the inbox; its file reads give up the GIL, which beside
        # the solver's threads blocked the window ~200 ms (gui_probe)
        if self.settings is None or self._inbox is None or self.busy() or self._modal_open():
            return
        for req in self._inbox.take_requests():
            self.add_nets(req["nets"], req["pads"])

    @staticmethod
    def _modal_open() -> bool:
        """One of our modal dialogs is open (timers then wait: no new dialogs or closing under it)."""
        return any(isinstance(w, wx.Dialog) and w.IsModal() for w in wx.GetTopLevelWindows())

    def on_add_selection(self):
        """Nets tab button (live): the nets of what is selected in KiCad."""
        if self.settings is None:
            self.SetStatusText("the board is not loaded yet")
            return
        session = self.session
        self._start("reading KiCad's selection…",
                    lambda progress: (session.selected_nets(), session.selected_pad_ids()), self._selection_read,
                    what="read KiCad's selection", cancellable=False, kicad=True)

    def _selection_read(self, result: tuple[list[str], list[str]]):
        """KiCad's selection was read (nets, selected pad ids): add it, or explain that nothing with a net is
        selected."""
        nets, pads = result
        if not nets:
            wx.MessageBox(NO_SELECTION, "GroundTruth", wx.ICON_INFORMATION)
            return
        self.add_nets(nets, pads)

    def add_nets(self, nets: list[str], pads: list[str] = ()):
        """Add what was picked in KiCad. Several nets joined by parts are one signal path (one bit of each net
        selected, any order: analysis/selection.py), added with its op-amps' models and GT labels (_add_path).
        Otherwise each net is added on its own as an unreviewed suggestion like Detect's (driver and receivers
        from the pin types). Shows the last one on the Signal paths tab and brings the window to the front."""
        from ..analysis.detect import GENERIC, suggest_net
        not_path = ""
        if len(nets) > 1:
            from ..analysis.selection import path_from_selection
            pins = [p.pin for p in self.board.pads() if p.uuid and p.uuid in set(pads)]
            proposal = path_from_selection(self.board, self.settings.config, list(nets), pins)
            if not proposal.error:
                self._add_path(proposal)
                return
            not_path = f"not one path ({proposal.error}): each net added on its own"
        added, present, refused = [], [], []
        for net in nets:
            if any(n.name == net for n in self.settings.config.nets):
                present.append(net)
                continue
            found, why = suggest_net(self.board, self.settings.config, net)
            if found is None:
                # refused as a supply: the user may know better (the user: "/CPU/ADC_I_RAIL_1 ... is actually a
                # sensitive signal to an ADC ... it should be allowed to go into the nets tab")
                forced, _ = suggest_net(self.board, self.settings.config, net, as_signal=True)
                if forced is not None and wx.MessageBox(
                        f"{why}\n\nIf {net} is a signal (an ADC input, a sense line), add it to the Signal paths "
                        "tab anyway?",
                        "GroundTruth", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) == wx.YES:
                    found = forced
            if found is None:
                refused.append(why)
            else:
                self.settings.add_detected([found.spec])
                added.append(found)
        if added:
            self.nets.load(self.board, self.settings)
            self.on_config_change()
        self.notebook.SetSelection(self.notebook.FindPage(self.nets))
        shown = [d.spec.name for d in added] + present
        if shown:
            self.nets.select(shown[-1])
        text = []
        for d in added:
            text.append(f"added {d.spec.name}: {d.signal_class.name}, driver {d.spec.driver} "
                        f"({d.confidence} confidence)" + ("; set its frequency and rise time"
                                                           if d.signal_class is GENERIC else ""))
        if added:
            text.append("orange until reviewed")
        if present:
            text.append(f"{', '.join(present)} already listed")
        self.SetStatusText(" · ".join(([not_path] if not_path else []) + text + refused))
        self._to_front()
        if refused and not shown:
            wx.MessageBox("\n".join(refused), "GroundTruth", wx.ICON_INFORMATION)

    def _add_path(self, proposal):
        """Put a path picked in KiCad into the settings: the path (an unreviewed detected entry of the same name is
        replaced; a reviewed or hand-made one only if the user says so) and its op-amps' models; then its GT labels
        into the schematic (a KiCad job), or, where KiCad can't (KiCad 10), in a message to add them by hand."""
        from dataclasses import replace
        spec = proposal.spec
        old = next((n for n in self.settings.config.nets if n.name == spec.name), None)
        if old is not None and not (old.source == "auto" and not old.reviewed) and wx.MessageBox(
                f"{spec.name} is listed already (driver {old.driver}). Replace it with the path picked in KiCad?\n\n"
                f"{proposal.notes[0]}", "GroundTruth", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) != wx.YES:
            return
        nets = list(self.settings.config.nets)
        nets = [replace(spec, enabled=n.enabled) if n.name == spec.name else n for n in nets] if old else nets + [spec]
        try:
            self.settings.set_nets(nets)
            for ref, model in proposal.models.items():
                self.settings.set_component(ref, model)
        except ConfigError as e:
            wx.MessageBox("\n".join(e.messages), "GroundTruth", wx.ICON_WARNING)
            return
        self.nets.load(self.board, self.settings)
        if proposal.models:
            self.parts.load(self.board, self.settings)
        self.on_config_change()
        self.notebook.SetSelection(self.notebook.FindPage(self.nets))
        self.nets.select(spec.name)
        amps = ", ".join(f"{r} (pins {' → '.join(m.pins)})" for r, m in proposal.models.items())
        self.SetStatusText(f"{'replaced' if old else 'added'} the path {spec.name}: {proposal.notes[0]}"
                           + (f" · op-amp model for {amps}" if amps else "")
                           + " · orange until reviewed: set its frequency and rise time")
        self._to_front()
        if not proposal.labels:
            return
        labels, session = dict(proposal.labels), self.session
        self._start("writing the GT labels into the schematic…", lambda progress: session.write_symbol_labels(labels),
                    lambda r: self._labels_written(labels, *r), what="write the GT labels",
                    cancellable=False, kicad=session.live)

    def _labels_written(self, labels: dict[str, str], written: list[str], why: str):
        """After the label job: say where they went, or show them to add by hand (Windows copies a message box's
        text with Ctrl+C). The path is in the settings either way, so Simulate uses it now."""
        if written:
            self.SetStatusText(f"GT labels written on {', '.join(written)} in the schematic (one undo step there); "
                               "Update PCB from Schematic (F8) copies them to the board")
            return
        rows = "\n".join(f"    {ref}:  GT = {text}" for ref, text in sorted(labels.items()))
        wx.MessageBox(f"The path is in the settings and Simulate uses it now. KiCad couldn't take the GT labels "
                      f"({why}), so to keep them in the design add them by hand (Schematic editor › Symbol Fields "
                      f"Table, a column named GT; Ctrl+C copies this message), then Update PCB from Schematic:\n\n"
                      f"{rows}", "GroundTruth: GT labels", wx.ICON_INFORMATION)

    def _to_front(self):
        """Bring the window to the front (Windows may only flash it in the taskbar if another program is active)."""
        if self.IsIconized():
            self.Iconize(False)
        self.Raise()
        if not self.IsActive():
            self.RequestUserAttention()

    def _close_if_kicad_gone(self, gone: Callable[[], bool]):
        """Timer: KiCad has exited, so the board buttons can no longer work; close (saving the settings like the
        close button). Waits while one of our modal dialogs is open, so it never closes under it."""
        if self._modal_open():
            return
        if gone():
            self.SetStatusText("KiCad was closed")
            self.Close()

    def on_preferences(self):
        """Top bar "Preferences…": the dialog of the user's settings on this computer (ui/prefs_dialog.py);
        OK keeps them and redraws the heatmaps with them."""
        from .prefs_dialog import PreferencesDialog
        # the cell sizes: this session's last Simulate, else the newest run kept for this board (History tab)
        cell = self._cell_mm if self._cell_mm is not None else getattr(self.history, "cell_mm", None)
        with PreferencesDialog(self, theme.prefs(), cell) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.apply_preferences(dlg.values())

    def apply_preferences(self, values: dict):
        """Store the Preferences dialog's values and apply what can change now (the heatmap's display); the theme
        applies when the window opens next. Resolution, threads and GPU are read by the next Simulate."""
        old = theme.load_pref()
        theme.save_prefs(**values)
        self.heatmap.set_display(values["via_markers"], values["max_vias"], values["outlines"], values["names"])
        note = (f"; the {values['theme']} theme applies the next time GroundTruth opens"
                if values["theme"] != old else "")
        self.SetStatusText(f"preferences saved: resolution {values['resolution']}×, threads {values['threads']}, "
                           f"GPU {values['gpu']}" + note)

    def _place_gauge(self, evt):
        """The window changed size: put the gauge over the status bar's second field and wrap the status line
        again for the new width."""
        rect = self.status.GetFieldRect(1)
        self.gauge.SetPosition((rect.x + 2, rect.y + 2))
        self.gauge.SetSize((rect.width - 4, min(rect.height - 4, self.status.GetCharHeight() + 6)))
        self._status_rect = None                # the field may have moved (and the DPI changed): measured again
        self._status_widths.clear()
        self._wrap_status()
        evt.Skip()

    def SetStatusText(self, text, number=0):
        """As wx's, and the main field's text is also kept and drawn wrapped over several lines when long. Set on
        the StatusBar itself (wx.Frame's C++ call need not reach its Python override)."""
        bar = getattr(self, "status", None)
        if isinstance(bar, StatusBar):
            bar.SetStatusText(text, number)
        else:
            super().SetStatusText(text, number)
        if number == 0 and getattr(self, "status_label", None) is not None:
            self._status_text = text
            self._wrap_status()

    def status_text(self) -> str:
        """The status line's text (kept here: reading it back from the bar is a wx call)."""
        return getattr(self, "_status_text", None) or self.GetStatusBar().GetStatusText(0)

    def _char_extents(self, text: str) -> list[int]:
        """The cumulative widths of text's characters in the status label's font, from widths measured once per
        character (one wx call for the ones not seen yet). During a Simulate the clock rewrites the line every
        second, and every wx call gives up the GIL, which costs up to a timer tick to take back beside the solver's
        threads: the dozen calls a rewrite made blocked the window ~220 ms (gui_probe on kraft_flux_mini_2)."""
        widths = self._status_widths
        new = "".join(sorted({c for c in text if c not in widths}))
        if new:
            dc = wx.ClientDC(self.status_label)
            dc.SetFont(self.status_label.GetFont())
            ext = dc.GetPartialTextExtents(new)
            for k, c in enumerate(new):
                widths[c] = ext[k] - (ext[k - 1] if k else 0)
        out, x = [], 0
        for c in text:
            x += widths[c]
            out.append(x)
        return out

    def _wrap_status(self):
        """Lay the status text out over field 0, wrapped to its width; the bar grows (or shrinks back) to fit up to
        STATUS_LINES lines, the rest cut with an ellipsis; the whole text is the tooltip. Only what changed is set
        (see _char_extents): a clock tick is two wx calls, the text in the bar and in the label."""
        text = self.status_text()
        if getattr(self, "_status_rect", None) is None:
            self._status_rect = self.status.GetFieldRect(0)
        rect = self._status_rect
        label = self.status_label
        lines = wrap_words(text.rstrip("\n"), max(rect.width - 8, 50), self._char_extents)
        if len(lines) > STATUS_LINES:
            lines = lines[:STATUS_LINES - 1] + [lines[STATUS_LINES - 1].rstrip() + " …"]
        shown = "\n".join(lines)
        if shown != self._status_shown:
            self._status_shown = shown
            label.SetLabelText(shown)
        tip = text if len(lines) > 1 else ""
        if tip != self._status_tip:
            self._status_tip = tip
            label.SetToolTip(tip)
        want = len(lines)
        if want < self._status_lines and self.busy():
            # a running job's line changes every second and can wrap onto one line more or less each time: the
            # bar only grows while it runs, since every height change lays the whole window out again
            want = self._status_lines
        if want != self._status_lines:
            self._status_lines = want
            self.status.SetMinHeight(want * self.status.GetCharHeight() + 4 if want > 1 else 0)
            self.SendSizeEvent()            # the frame gives the bar its new height, then this runs again
            return
        size = (rect.x + 4, rect.y + 1, rect.width - 6, rect.height - 2)
        if size != self._status_size:
            self._status_size = size
            label.SetSize(*size)

    # ------------------------------------------------------------------ jobs (worker thread)
    def busy(self) -> bool:
        """A job is running, or the window is still building or filling its tabs."""
        return (not self.pages_built() or getattr(self, "_tabs_loading", False)
                or (self.worker is not None and self.worker.is_alive()))

    def _refuse_if_busy(self) -> bool:
        """True (and the status line says so) while a job runs: one job at a time."""
        if self.busy():
            self.SetStatusText("still working: wait for the current job to finish, or press Cancel")
            return True
        return False

    def _start(self, status: str, work: Callable, done: Optional[Callable] = None, *, what: str,
               cancellable: bool = True, kicad: bool = False, solver: bool = False,
               local: Optional[Callable] = None) -> bool:
        """Run work(progress) -> result in the worker thread, then done(result) in the GUI thread.
        Everything work needs must be captured before the call; work must not touch wx. kicad: the job talks
        to KiCad (its Session calls hold the KiCad lock, so an issue shown meanwhile waits for them). solver:
        the job runs the field solver's threads (Python's default GIL interval meanwhile, see ui/gil.py).
        local: for a job sent to the compute server, how to start it again on this computer, offered when the
        server can't take it (ServerUnavailable)."""
        if self._refuse_if_busy():
            return False
        self._local_retry = local
        if solver:
            self._solver_interval.enter()
        self._cancel.clear()
        self.gauge.SetValue(0)
        self._enable(False, cancellable)
        self._clock.start(status)
        self.SetStatusText(self._clock.line())
        self._clock_timer.Start(1000)

        def progress(msg, frac):
            """(worker thread) The job reports its step and fraction done; this is where Cancel stops it."""
            if self._cancel.is_set():
                raise Cancelled()
            wx.CallAfter(self._progress, msg, frac)

        def target():
            """(worker thread) Run the job; its result, failure or cancellation goes to _finished on the GUI
            thread. A failed or stopped job's memory is released at once (release_failure)."""
            try:
                result = work(progress)
            except Cancelled as e:
                release_failure(e)          # the stopped job's models go now, not at some later gc
                wx.CallAfter(self._finished, None, None, None, what, True)
            except Exception as e:
                tb = traceback.format_exc()
                release_failure(e)          # a failed Simulate kept GBs in its traceback's frames (ui/progress.py)
                wx.CallAfter(self._finished, None, None, (e, tb), what, False)
            else:
                wx.CallAfter(self._finished, done, result, None, what, False)

        self.worker = threading.Thread(target=target, name="groundtruth-job", daemon=True)
        self.worker.start()
        return True

    def _panel_job(self, status: str, work: Callable, done: Callable, what: str) -> bool:
        """For the tabs' buttons (Detect…): their work runs off the GUI thread like the toolbar's."""
        def finished(result):
            """The tab's done(result), after saying the job finished."""
            self.SetStatusText(f"finished: {what}")
            done(result)
        return self._start(status, work, finished, what=what, cancellable=False)

    def _finished(self, done, result, error, what: str, cancelled: bool):
        """(GUI thread) A job ended: stop the clock, enable the buttons, then show its result with done(result),
        or say it was cancelled, or show the error (in full with its traceback unless it is an expected kind)."""
        self.worker = None
        self._solver_interval.leave()
        self._clock_timer.Stop()
        took = self._clock.elapsed()
        self._clock.stop()
        self._enable(True)
        if cancelled or error is not None:
            self._after_load = None         # a run waiting for a reload (_check_board) does not follow a failed one
        if cancelled:
            self.gauge.SetValue(0)
            self.SetStatusText(f"cancelled: {what}")
            return
        if error is not None:
            e, tb = error
            startup.mark(f"could not {what} ({type(e).__name__})", last=True)
            self.gauge.SetValue(0)
            self.SetStatusText(f"could not {what}")
            retry, self._local_retry = getattr(self, "_local_retry", None), None
            if type(e).__name__ == "ServerUnavailable" and retry is not None:
                if wx.MessageBox(f"Could not {what} on the compute server:\n{e}\n\nRun it on this computer instead?",
                                 "groundtruth: compute server", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
                    retry()
                return
            detail = f"{e}" if type(e).__name__ in EXPECTED_ERRORS else f"{e}\n\n{tb}"
            wx.MessageBox(f"Could not {what}:\n{detail}", "groundtruth", wx.ICON_WARNING)
            return
        self.gauge.SetValue(1000)
        if done is not None:
            done(result)
        if took >= 10:                                   # how long it took, for the next time
            self.SetStatusText(self.GetStatusBar().GetStatusText(0) + f" · took {clock(took)}")
        enabled = self.session.enabled_layers()           # a list pop, no KiCad call
        if enabled:                      # a board-setup change outside the drawings: say so
            self.SetStatusText(self.GetStatusBar().GetStatusText(0) + f" · enabled {', '.join(enabled)} on the board "
                               "for the drawings (Board Setup › Board Editor Layers)")

    def _enable(self, on: bool, cancellable: bool = False):
        """Enable the actions (no job running) or disable them (a job runs; Cancel only if it can be cancelled)."""
        for b in (self.t_reload, self.t_refill, self.t_check, self.t_sim, self.t_thermal, self.t_clear):
            if b is not None:
                b.Enable(on)
        self.t_cancel.Enable(not on and cancellable)
        self.whatif.Enable(on)
        self.fixes.enable(on)
        self.issues_panel.enable_drawing(on)
        self.heatmap.enable_drawing(on)

    def _progress(self, msg, frac):
        """(GUI thread) A job's progress report: the status line and the gauge."""
        if self.busy() and not self._cancel.is_set():
            self._clock.update(msg, frac)
            self.SetStatusText(self._clock.line())
            self.gauge.SetValue(int(self._clock.frac * 1000))

    def _tick_clock(self):
        """Timer, every second while a job runs: the elapsed time and the estimate count on between reports."""
        if self._clock.running and self.busy() and not self._cancel.is_set():
            line = self._clock.line()
            if line != self.status_text():
                self.SetStatusText(line)

    def on_cancel(self, _=None):
        """Cancel: ask the running job to stop (it does at its next progress report)."""
        if self.busy():
            self._cancel.set()
            self.t_cancel.Enable(False)
            self.SetStatusText("cancelling… (stops at the next progress step)")

    # ------------------------------------------------------------------ board / settings
    def on_reload(self, _=None):
        """Reload board: read the board (from KiCad or the file), a copy of its text for the history, and its
        settings, as a job."""
        startup.mark("reading the board")
        session = self.session

        keep = self._history_on()

        def work(progress):
            """(worker) The board, its path, (settings, new, dirty) or the settings' errors, the board copy, the
            board's fingerprint."""
            board, path = session.load()
            fp = board.fingerprint()        # ~0.1 s on kraft_flux_mini_2: what run() compares before analysing
            # modules the tabs' load() uses, imported here and not on the GUI thread (analysis.pdn took 59 ms there)
            from ..analysis import pdn, si, transfer  # noqa: F401
            from . import pin_complete  # noqa: F401           (the Power integrity tab's load: 50 ms more)
            if session.live:                # a click on a table row shows it in KiCad: the board's planes ready
                from ..analysis.highlight import _planes
                _planes(board)              # (~160 ms on kraft_flux_mini_2 the first time, then cached)
            copy = session.board_copy(path) if keep and path else (None, "")
            if not path:
                return board, path, None, None, copy, fp
            try:                            # here, not in the GUI thread: parsing the YAML takes ~0.5 s
                return board, path, _open_settings(board, path), None, copy, fp
            except ConfigError as e:
                return board, path, None, e.messages, copy, fp
        if not self._start("reading the board…", work, self._loaded, what="read the board", cancellable=False,
                           kicad=self.session.live):
            self._after_load = None

    def _loaded(self, result):
        """The board was read: open its history and the KiCad-button inbox, fill the tabs, offer to refill
        unfilled zones, and detect nets and stages when the settings list nothing yet."""
        board, path, opened, errors, self._board_copy, fp = result
        startup.mark(f"board read ({len(board.footprints)} parts)", last=True)
        if not path:
            self._after_load = None
            self.history.set_history(None, "The board is not saved yet: its history goes next to it.")
            wx.MessageBox("Save the board first, so the settings can be stored next to it.", "groundtruth", wx.ICON_WARNING)
            return
        if self.board_path != path:
            from ..report.history import history_for
            self.board_path = path
            self.history.set_history(history_for(path))
        if errors:
            self._after_load = None
            wx.MessageBox("Invalid settings file:\n" + "\n".join(errors), "groundtruth", wx.ICON_ERROR)
            return
        self._board_fp = fp
        settings, new, self.dirty = opened
        self.board, self.settings = board, settings
        if self._inbox is None:
            try:
                self._inbox = Inbox(path)
                self._inbox_timer.Start(500)
            except OSError as e:                # no per-user folder: the KiCad button cannot reach us
                self.SetStatusText(f"KiCad's Add selected net button is unavailable: {e}")
        # each tab's load as an event of its own: all of them in one blocked the window 137-211 ms on
        # kraft_flux_mini_2 (parts 31, converters 32, bridges 38, PDN 20 ms); the rest of the load follows the last
        tabs = [self.parts, self.nets, self.converters, self.bridges, self.dc_loads, self.pdn, self.si]
        self._load_tabs(tabs, board, settings, lambda: self._loaded_rest(path, settings, new))

    def _load_tabs(self, tabs: list, board, settings, then: Callable):
        """Load the tabs one per event (wx.CallLater: the window repaints and answers between them), then run
        `then`. busy() is true meanwhile; a newer board (a reload) stops the chain."""
        self._tabs_loading = True

        def step(k: int):
            """Load tab k, schedule the next."""
            if not self or self.board is not board:          # closed, or another board read meanwhile
                self._tabs_loading = False
                return
            if k < len(tabs):
                tabs[k].load(board, settings)
                wx.CallLater(1, step, k + 1)
                return
            self._tabs_loading = False
            then()
        step(0)

    def _loaded_rest(self, path, settings, new: bool):
        """The board's load after its tabs are filled: the status line, the zones' refill offer, and detection when
        the settings list nothing yet."""
        sp = settings.path
        note = f" — {self.board.warnings[0]}" if self.board.warnings else ""
        self.SetStatusText(f"{Path(path).name}: {len(self.board.copper_layers)} copper layers, "
                           f"{len(self.board.footprints)} parts · settings {sp.name}{note}")
        if (self.board.unfilled_zones() and self.session.live and
                wx.MessageBox("Some zones are not filled. Refill all zones now?", "groundtruth", wx.YES_NO) == wx.YES):
            self.on_refill()                # reloads the board, and detects then if the settings are still new
            return                          # (a run waiting for this reload continues after that one)
        self._show_stale()                  # results of the board as it was before this read are marked
        then, self._after_load = self._after_load, None
        if new or not (settings.config.nets or settings.config.converters or settings.config.bridges
                       or settings.config.dc_loads or settings.config.pdn):
            self._auto_detect()
        elif "bridges" not in settings.doc:     # a settings file from before the Bridges tab: look for bridges once
            self._auto_detect(bridges_only=True)
        elif then is not None:              # run() found the board changed and read it again: now the run itself
            then()

    def _auto_detect(self, bridges_only: bool = False):
        """Nothing to analyse yet (a new settings file, or one listing neither nets nor power stages): suggest the
        board's nets, power stages and motor bridges at once (as `groundtruth detect --write` does), marked
        unreviewed (orange) until the user checks them. bridges_only: a settings file written before bridges
        existed: only its bridges are looked for (once: an empty `bridges:` is written when there are none)."""
        from ..analysis.detect import detect_bridges, detect_converters, detect_nets, power_nets
        board, config = self.board, self.settings.config

        def work(progress):
            """(worker) Bridges first: their legs are stages too, and the nets of every stage are left out of
            the signal-net detection (they are power, not signals)."""
            bridges = detect_bridges(board, config)
            if bridges_only:
                return [], [], bridges
            stages = detect_converters(board, config)
            specs = [d.spec for d in stages] + [s for d in bridges for s in d.spec.stages()]
            return stages, detect_nets(board, config, skip=power_nets(board, config, specs)), bridges
        self._start("detecting motor bridges…" if bridges_only else
                    "nothing to analyse yet: detecting nets, power stages and motor bridges…", work,
                    lambda result: self._auto_detected(result, bridges_only),
                    what="detect nets, power stages and bridges", cancellable=False)

    def _auto_detected(self, result, bridges_only: bool = False):
        """Add what detection found to the settings (unreviewed; bridges only when complete), show it on the tabs
        and say what to check."""
        stages, nets, bridges = result
        conv = self.settings.add_detected_converters([d.spec for d in stages],
                                                     {r: m for d in stages for r, m in d.models.items()})
        added = self.settings.add_detected([d.spec for d in nets])
        complete = [d for d in bridges if d.complete]
        brid = self.settings.add_detected_bridges([d.spec for d in complete],
                                                  {r: m for d in complete for r, m in d.models.items()})
        if "bridges" not in self.settings.doc:      # looked for: not again on the next open
            self.settings.set_bridges([])
        self.nets.load(self.board, self.settings)
        self.converters.load(self.board, self.settings)
        self.bridges.load(self.board, self.settings)
        self.on_config_change()
        if bridges_only:            # the settings already list nets or converters: only bridges were looked for
            self.SetStatusText(f"detected {len(brid)} motor bridge(s), orange until reviewed: set the operating point "
                               "and motor on the Bridges tab" if brid else "no motor bridges on this board")
            return
        if not (added or conv or brid):
            self.SetStatusText("no signal paths, power stages or motor bridges detected: add them on the Signal "
                               "paths, Converters and Bridges tabs")
            return
        self.SetStatusText(f"detected {len(added)} net(s), {len(conv)} power stage(s) and {len(brid)} motor "
                           "bridge(s), orange until reviewed: check each (Converters: operating point and "
                           "connectors; Bridges: operating point and motor), then Mark reviewed")

    def on_refill(self, _=None):
        """Refill zones in KiCad, then read the board again."""
        self._start("refilling zones…", lambda progress: self.session.refill(), lambda _: self.on_reload(),
                    what="refill the zones", cancellable=False, kicad=True)

    def on_config_change(self):
        """A tab changed the settings: save them (in the background), and work out the Signal paths tab's chains
        again (a part model the Parts tab changed may open or close a path, or change a value shown)."""
        if getattr(self, "nets", None) is not None and getattr(self.nets, "path_text", None) is not None:
            self.nets.refresh_chains()
        self.dirty = True
        self.on_save()
        self._show_stale()

    def on_export(self):
        """Top bar "Export report…": the results shown (the last Run checks or Simulate, the thermal and capacity
        views, the last fix search) as one self-contained HTML page (report/html.py), rendered as a job, then
        opened in the browser."""
        args = self._report_args(self.fixes.result)
        if args is None:
            self.SetStatusText("Export report needs results: run Run checks or Simulate first")
            return
        board = args[0]
        stem = report_stem(board, self.settings.path if self.settings else None)
        folder = ""
        if self.board_path:                 # the board's groundtruth folder first (made when missing)
            from ..config.settings import project_dir
            try:
                target_dir = project_dir(self.board_path)
                target_dir.mkdir(exist_ok=True)
                folder = str(target_dir)
            except OSError:
                folder = str(Path(self.board_path).parent)
        with wx.FileDialog(self, "Export the report", defaultDir=folder, defaultFile=f"{stem}_report.html",
                           wildcard="HTML page (*.html)|*.html", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            target = Path(dlg.GetPath())

        def work(progress):
            """(worker) Render the page and write it."""
            from ..report.html import render_html
            target.write_text(render_html(*args[:5], progress=progress, **args[5]), encoding="utf-8")
            return target

        def done(path):
            """Say where it went and open it in the browser."""
            self.SetStatusText(f"report written: {path} ({path.stat().st_size / 1e6:.1f} MB)")
            wx.LaunchDefaultBrowser(path.as_uri())
        self._start("exporting the report…", work, done, what="export the report")

    def _report_args(self, suggestions=None) -> Optional[tuple]:
        """render_html's arguments for the results shown (board, issues, routes, mode, settings path, then the
        keyword arguments), taken now on the GUI thread; None without results. Export report and the history's
        report page (without a fix search: it belongs to an earlier run) both use it."""
        board, cfg = self._views_from or (self.board, self.settings.config if self.settings else None)
        if board is None or cfg is None or not (self.routes or self.issues):
            return None
        views, issues = list(self.routes), list(self.issues)
        routes = [r for r in views if r.kind == "route"]
        converters = [r for r in views if r.kind == "converter" and not getattr(r, "view_of", None)]
        bridges = [r for r in views if r.kind == "bridge" and not getattr(r, "view", "")]
        thermal = next((r for r in views if r.kind == "thermal" and not getattr(r, "capacity", None)), None)
        capacity = [r for r in views if r.kind == "thermal" and getattr(r, "capacity", None)]
        from ..analysis.power import COMBINED_NET
        combined = next((r for r in views if r.kind == "converter" and r.net == COMBINED_NET), None)
        mode = "simulation" if routes or converters or bridges else "rule checks"
        from .prefs_dialog import defaults
        mine = defaults(theme.prefs())
        if (mine["author"] and not cfg.report.author) or (mine["company"] and not cfg.report.company):
            from dataclasses import replace       # the user's own name and company, where the board names none
            cfg = replace(cfg, report=replace(cfg.report, author=cfg.report.author or mine["author"],
                                              company=cfg.report.company or mine["company"]))
        settings_path = str(self.settings.path) if self.settings else ""
        return board, issues, routes, mode, settings_path, dict(
            suggestions=suggestions, converters=converters, bridges=bridges, thermal=thermal, capacity=capacity,
            solver=self._solver, config=cfg, max_vias=self.heatmap.max_vias(), outlines=self.heatmap.outlines,
            names=self.heatmap.names, dc_combined=combined,
            # views sharpened with "Sharper map": the report draws their finer maps (the user: "export the highest
            # resolution images available")
            sharper=[v for v in views if getattr(v, "sharper", None)],
            dc_loads=[r for r in views if r.kind == "dcload"], pdn=[r for r in views if r.kind == "pdn"])

    # ------------------------------------------------------------------ simulation history
    def _history_on(self) -> bool:
        """Keep runs is on (Preferences)."""
        from .prefs_dialog import defaults
        return defaults(theme.prefs())["history"]

    def _history_inputs(self, edits):
        """What the Simulate job needs to keep the run in the history (a dict), taken now: the board copy from the
        load, the settings frozen as they are. A string says why the run will not be kept; None: Keep runs is
        off (Preferences)."""
        if not self._history_on():
            return None
        history = self.history.history
        data, source = self._board_copy
        if history is None:
            return "the board has no history folder (not saved yet)"
        if data is None:
            return (f"no copy of the board ({source})" if source else
                    "Keep runs was off when the board was read: Reload board to start keeping runs")
        from ..report.history import edit_texts
        what_if = edits is not None
        return {"history": history, "path": self.board_path, "board": data, "source": source,
                "settings": self.settings.frozen_text(), "what_if": what_if,
                "edits": edit_texts(edits) if what_if else [], "t0": time.monotonic(),
                "mode": "simulate (Tier 1 + Tier 2)" + (" with what-if edits" if what_if else "")}

    @staticmethod
    def _kept(kept) -> str:
        """The status line's note on the history after a Simulate."""
        if kept is None:
            return ""
        if isinstance(kept, str):
            return f" · not kept in the history: {kept}"
        return " · kept in the History tab"

    def _keep_report(self, run):
        """The report page of the run just kept, written in the background (Preferences: History, Report) while
        the results can be looked at; the History tab says "writing…" until it is there."""
        from .prefs_dialog import defaults
        args = self._report_args()
        if args is None or not defaults(theme.prefs())["history_report"]:
            return
        history, worker, closing = self.history.history, self._history_reports, self._closing
        self.history.writing_report(run.id, True)

        def task():
            """(history thread) Render and store the run's report page, a plot at a time while the user leaves the
            mouse and keyboard alone (ui/idle.py: a plot being drawn holds the GIL, the window would stall); the tab
            is told when it is done. Once the window closes nobody is using it: the rest is drawn at once."""
            from .idle import wait_until_quiet

            def progress(msg, frac):
                """Before each plot: wait until the user is idle; stop when the window closes and cannot wait."""
                if worker.stopping.is_set():            # the window is closing and could not wait
                    raise Cancelled()
                wait_until_quiet(lambda: worker.stopping.is_set() or closing.is_set())
                if worker.stopping.is_set():
                    raise Cancelled()
            try:
                progress("report: start", 0.0)
                from ..report.html import render_html
                history.write_report_checked(run, render_html(*args[:5], progress=progress, **args[5]))
            except Cancelled:
                pass
            finally:
                wx.CallAfter(self._history_report_done, run.id)
        worker.submit(task)

    def _settle_when_idle(self):
        """(timer) New results are showing: once no job runs, no map is being drawn and the user has left the mouse
        and keyboard alone for idle.QUIET_S, one full garbage collection frees the replaced results' reference
        cycles and what is alive is frozen (gil.settle_garbage): its ~200-350 ms fall where nobody is waiting,
        instead of the collector walking the results again during every later view switch."""
        from .idle import QUIET_S, input_idle_s
        if self.busy() or self.heatmap.rendering() or input_idle_s() < QUIET_S:
            return
        self._settle_timer.Stop()
        from .gil import settle_garbage
        settle_garbage()

    def _history_report_done(self, run_id: str):
        """The run's report page is written (or given up): the History tab stops saying "writing…"."""
        if self:
            self.history.writing_report(run_id, False)

    def _history_report_failed(self, e: Exception):
        """A history report page failed: said in the status line (the run itself is kept)."""
        if self:
            self.SetStatusText("the run's report page for the history could not be written: "
                               f"{type(e).__name__}: {e}")

    def history_busy(self) -> bool:
        """A history report or a History tab request is still running in the background."""
        return self._history_reports.busy() or (getattr(self, "history", None) is not None and self.history.busy())

    def _sim_config(self):
        """The settings Simulate and Suggest fixes run with: the board's, with this computer's resolution. The same
        object while neither changes, so the fix search can tell that the last Simulate still applies."""
        from dataclasses import replace
        from .prefs_dialog import overrides
        base, r, over = self.settings.config, self.resolution(), overrides(theme.prefs())
        key = (r, tuple(sorted(over.items())))
        cached = getattr(self, "_sim_cfg", None)
        if cached is None or cached[0] is not base or cached[1] != key:
            cfg = base if r == 1 and not over else replace(base, analysis=replace(base.analysis,
                                                                                  resolution=float(r), **over))
            self._sim_cfg = cached = (base, key, cfg)
        return cached[2]

    def resolution(self) -> int:
        """Simulate's node factor on this computer (Preferences)."""
        from .prefs_dialog import defaults
        return defaults(theme.prefs())["resolution"]

    def _note_cells(self, views):
        """The 1× cell of the coarsest model just computed (at the run's resolution), for Preferences to show each
        Resolution level's cell size."""
        cells = [r.grid.d for r in views if getattr(r, "grid", None) is not None and r.kind in ("route", "converter")]
        if cells:
            self._cell_mm = max(cells) * math.sqrt(getattr(self, "_sim_resolution", 1))

    def on_help(self):
        """Top bar "Help" (F1): the help window at the topic of the tab shown; one window, raised again."""
        from .help import HelpWindow, topic_of
        k = self.notebook.GetSelection()
        topic = topic_of(self.notebook.GetPageText(k) if k >= 0 else "")
        if self._help:
            self._help.show(topic)
        else:
            self._help = HelpWindow(self, topic)

    def on_save(self, _=None):
        """Save settings: write them in the background (the newest snapshot wins)."""
        if self.settings is not None:
            self._saver.save(self.settings)     # copies the settings here, writes them in the background
            self.dirty = False

    def _save_failed(self, e: Exception):
        """A background save failed: say so, and keep the settings marked unsaved."""
        if self:                                 # the window may be gone (a failed save while closing)
            self.SetStatusText(f"could not save the settings: {e} (NOT saved)")
            self.dirty = True

    # ------------------------------------------------------------------ analysis
    def _ready(self, simulate: bool = True) -> bool:
        """Nothing else running, board loaded, nets (or, to simulate, converters) configured, settings consistent
        with the board."""
        if self._refuse_if_busy():
            return False
        if self.settings is None or self.board is None:
            self.SetStatusText("the board is not loaded yet")
            return False
        listed = self.settings.config
        cfg = listed.active()                  # the rows ticked on their tabs: the others are not analysed
        if simulate and (listed.nets or listed.converters or listed.bridges or listed.dc_loads or listed.pdn) and not (
                cfg.nets or cfg.converters or cfg.bridges or cfg.dc_loads or cfg.pdn):
            wx.MessageBox("Every net, power stage, motor bridge and DC load is switched off: tick at least one on its "
                          "tab (the tick box at the start of its row).", "groundtruth")
            return False
        if simulate and cfg.bridges:
            from ..config.settings import simulation_blockers
            blocked = [f"{b.name}: {'; '.join(simulation_blockers(b))}" for b in cfg.bridges if simulation_blockers(b)]
            if blocked:
                wx.MessageBox("Set these on the Bridges tab first (or remove the bridge):\n" + "\n".join(blocked),
                              "groundtruth")
                self.notebook.SetSelection(self.notebook.FindPage(self.bridges))
                return False
        # Run checks without paths still runs the board-wide checks (voltage-aware safety, schematic, values)
        if simulate and not (cfg.nets or cfg.converters or cfg.bridges or cfg.dc_loads or cfg.pdn):
            wx.MessageBox("Add at least one path on the Signal paths tab first (or a converter on the Converters "
                          "tab, a motor bridge on the Bridges tab, a DC load on the DC loads tab, or a rail on the "
                          "Power integrity tab).", "groundtruth")
            self.notebook.SetSelection(self.notebook.FindPage(self.converters))
            return False
        errors = validate(self.settings.config, self.board)
        if errors:
            wx.MessageBox("\n".join(errors), "groundtruth: settings does not match the board", wx.ICON_WARNING)
            return False
        return True

    def _what_if_edits(self, board, cfg):
        """The enabled what-if edits if they are valid for this board, else None after telling the user why.
        (Only checks: applying them copies the board, so that happens in the job.)"""
        from ..analysis.whatif import validate_modifications
        active = [m for m in cfg.modifications if m.enabled]
        if not active:
            wx.MessageBox("The settings has no enabled what-if edits. Add some on the Fixes tab (\"Add ticked to "
                          "what-if\") or under 'modifications:' in the settings.", "groundtruth")
            return None
        errors = validate_modifications(board, active)
        if errors:
            wx.MessageBox("\n".join(errors), "groundtruth: invalid what-if edits", wx.ICON_WARNING)
            return None
        return active

    def _server(self, local: bool = False) -> Optional[tuple[str, str]]:
        """(address, access key) when Preferences send the analyses to a compute server and `local` doesn't
        override it, else None."""
        from .prefs_dialog import compute_server
        return None if local else compute_server(theme.prefs())

    def run(self, simulate: bool, local: bool = False, fresh: bool = False):
        """Run checks (simulate=False: the rule checks only, fast) or Simulate (the field solves of every enabled
        net, power stage and bridge), optionally on a copy of the board with the what-if edits, as a job; on the
        compute server when Preferences say so (the rule checks always here), unless `local`.

        First (unless `fresh`) a short job reads the board again and compares its fingerprint with the one it had
        when the window read it (_check_board): a track narrowed in KiCad since would otherwise be analysed at its
        old width (VALIDATION_AUDIT.md round 5, ISSUE-19). Changed: the board is read again as Reload board does
        (settings, tabs), and the run follows. Unchanged, or KiCad too busy to answer: the run goes on at once."""
        if not self._ready(simulate):
            return
        if not fresh and self._board_fp is not None:
            self._check_board(lambda: self.run(simulate, local, fresh=True))
            return
        board, cfg = self.board, self.settings.config
        if simulate:
            cfg = self._sim_config()
            self._sim_resolution = self.resolution()
        what_if = self.whatif.GetValue()
        edits = self._what_if_edits(board, cfg) if what_if else None
        if what_if and edits is None:
            return
        keep = self._history_inputs(edits) if simulate else None
        server = self._server(local) if simulate else None
        src = (self._board_fp, settings_key(self.settings.config))     # what the views will be of (_stale_reason)

        def work(progress):
            """(worker) Apply the what-if edits, then simulate (here or on the compute server): drop the
            factorisations at once (the largest part of the memory), keep the run in the history, add each stage's
            DC and each bridge's RMS and sweep views, and keep the models for Suggest fixes only while memory
            allows (keep_models; a server's result has none)."""
            b, c = board, cfg
            if what_if:
                from ..analysis.whatif import apply_modifications
                progress("applying the what-if edits…", 0.0)
                b, c = apply_modifications(board, cfg, edits)
            if simulate and server is not None:
                # handed over and the window free again: the job runs on the server whatever happens here (the
                # laptop asleep, KiCad closed); the tracker brings its result back (remote/jobs.py)
                return self._hand_over(server, "simulate", (b, c, None),
                                       {"board": b, "cfg": c, "keep": portable_keep(keep), "what_if": what_if},
                                       "Simulate" + (" with what-if edits" if what_if else ""), progress)
            if simulate:
                res = simulate_on(None, b, c, progress)
                kept = keep_run(keep, res, c, progress) if isinstance(keep, dict) else keep
                sim = None
                if not what_if and keep_models(res):
                    sim = (board, cfg, res)
                else:
                    res.drop_models()
                return (b, res.issues, res.routes + result_views(res), sim, c, res.solver, kept,
                        getattr(res.checks, "si", None))
            from ..rules.checks import run_checks
            res = run_checks(b, c)
            return b, res.issues, [], None, c, None, None, res.si

        def done(r):
            """Show the run, or say that the server has it."""
            if isinstance(r, HandedOver):
                self._handed_over(r)
            else:
                self._done(*r, what_if=what_if, src=src)
        self._start(("handing the Simulate to the compute server" if server is not None else
                     "simulating" if simulate else "checking") + (" with what-if edits…" if what_if else "…"), work,
                    done, what="run the analysis", solver=simulate and server is None,
                    local=(lambda: self.run(simulate, local=True)) if server is not None else None)

    def _check_board(self, then: Callable):
        """Before a run: read the board again (a short job: KiCad's board with its unsaved edits, or the file) and
        compare its fingerprint with the window's. The same: then(). Changed: read it again as Reload board does,
        and then() once its tabs are filled (_after_load). Not readable now (KiCad busy, the file gone): then() on the
        board as read, saying so."""
        session, fp = self.session, self._board_fp

        def work(progress):
            """(worker) ("same" | "changed" | "unknown", why)."""
            try:
                board, _ = session.load()
            except Exception as e:      # noqa: BLE001 - KiCad busy or closed, the file unreadable: analyse what we have
                return "unknown", f"{type(e).__name__}: {e}"
            return ("same" if board.fingerprint() == fp else "changed"), ""

        def done(result):
            """Go on with the run, or read the board again first."""
            state, why = result
            if state == "changed":
                self._after_load = then
                self.on_reload()
                if self._after_load is then:        # the reload started: say why the run waits
                    self.SetStatusText("the board changed since it was read: reading it again, then the run")
                return
            then()
            if state == "unknown":
                self.SetStatusText(self.GetStatusBar().GetStatusText(0) + " · could not check the board for changes "
                                   f"({why}): analysing it as read")
        self._start("checking the board for changes…", work, done, what="check the board for changes",
                    cancellable=False, kicad=session.live)

    def _stale_reason(self) -> str:
        """Why the results shown are not the current design's ("the board changed", "the settings changed", both),
        or "" when they are (or nothing is shown, or where they came from is unknown). The views keep the board and
        settings they were computed on, which Thermal, Current capacity and the report work from: after Reload board
        or a settings edit they were shown, and Thermal ran on them, as if current (ISSUE-29)."""
        if not self.routes or self._views_src is None:
            return ""
        fp, key = self._views_src
        why = []
        if fp is not None and self._board_fp is not None and fp != self._board_fp:
            why.append("the board changed")
        if key is not None and self.settings is not None and key != settings_key(self.settings.config):
            why.append("the settings changed")
        return " and ".join(why)

    def _show_stale(self):
        """Mark the Heatmap's summary line (and say in the status bar) when the results shown are out of date."""
        why = self._stale_reason()
        self.heatmap.set_stale(why)
        if why:
            self.SetStatusText(f"the results shown are from an earlier Simulate: {why} since; Simulate again for "
                               "the current design")

    def _done(self, board, issues, routes, sim, cfg=None, solver=None, kept=None, si=None, what_if: bool = False,
              src: Optional[tuple] = None):
        """Show a run: the issues, the signal-integrity figures on their tab (si), and for a Simulate the Heatmap tab
        with its views, the fix advisor's design changes, and the run selected on the History tab (its report page
        written in the background). src: (board fingerprint, settings_key) the run was of (_stale_reason)."""
        if sim is not None:
            self._last_sim = sim
        if routes:
            self._settle_timer.Start(SETTLE_POLL_MS)    # the replaced results' garbage, once the user is idle
            self._shown_at = time.time()
            self._views_from = (board, cfg)     # what the heatmap views were computed with (what-if: the edits)
            self._views_src = src
            self.heatmap.set_stale("")
            self._solver = solver
        self.issues = issues
        if routes:
            self.routes = routes
        self.issues_panel.show(issues)
        if si is not None:
            self.si.show_results(si)
        n = {s: sum(1 for i in issues if i.severity == s and not i.waived) for s in ("error", "warning", "info")}
        self.SetStatusText(f"{n['error']} error(s), {n['warning']} warning(s), {n['info']} info"
                           + (" — with what-if edits" if what_if else "") + solver_note(solver) + self._kept(kept))
        if routes:
            self.notebook.SetSelection(self.notebook.FindPage(self.heatmap))
            self.heatmap.set_rules(self.settings.config.rules if self.settings else None)
            self.heatmap.show(board, routes, issues)
            self._note_cells(routes)
            self.fixes.show_design([r for r in routes if r.kind == "converter" and not getattr(r, "bridge", None)
                                    and not getattr(r, "view_of", None)])
            if kept is not None and not isinstance(kept, str):
                self.history.refresh(select=kept.id)
                self._keep_report(kept)
        else:
            self.notebook.SetSelection(self.notebook.FindPage(self.issues_panel))

    def on_check_design(self, stage: str, option: dict, local: bool = False):
        """Fixes tab, Check in Simulate: the stage simulated again with the advisor's change applied in memory (the
        settings stay as they are), its figures against the prediction; on the compute server when Preferences
        say so, unless `local`."""
        from dataclasses import replace
        board, cfg = self._views_from or (self.board, self.settings.config)
        spec = next((c for c in cfg.converters if c.name == stage), None)
        if board is None or spec is None:
            return
        ch = option["change"]
        kw = {}
        if "snubber_c" in ch:
            kw.update(snubber=[], snubber_c=ch["snubber_c"], snubber_r=ch["snubber_r"])
        if "rise_time" in ch:
            kw["rise_time"] = ch["rise_time"]
        if "spread" in ch:
            kw.update(spread=ch["spread"], spread_mode=ch["spread_mode"], spread_rate=ch["spread_rate"])
        cfg2 = replace(cfg, converters=[replace(spec, **kw) if c is spec else c for c in cfg.converters])
        server = self._server(local)

        def work(progress):
            """(worker) Simulate this stage alone with the change."""
            res = simulate_on(server, board, cfg2, progress, nets=[stage],
                              watch=self._watch(f"Check {stage}", board, cfg2) if server else None)
            return next((c for c in res.converters if c.net == stage), None)

        def done(r):
            """Predicted against simulated, side by side."""
            if r is None or r.error:
                self.fixes.design_text.SetValue(f"{stage}: the check failed{': ' + r.error if r else ''}")
                return
            p = option["outcome"] or {}
            got = {"radiated_db": r.worst["margin_db"] if r.worst else None,
                   "conducted_db": r.conducted_worst["margin_db"] if r.conducted_worst else None,
                   "overshoot_v": max(r.overshoot_v or 0.0, (r.ringing or {}).get("overshoot_v", 0.0))}
            lines = [f"{stage}: {option['title']}, checked in Simulate", "                predicted   simulated"]
            for key, name, unit in (("radiated_db", "radiated", "dB"), ("conducted_db", "conducted", "dB"),
                                    ("overshoot_v", "overshoot", "V")):
                a, b = p.get(key), got[key]
                if a is not None or b is not None:
                    lines.append(f"  {name:12s}  {'–' if a is None else f'{a:+7.1f}':>8s}    "
                                 f"{'–' if b is None else f'{b:+7.1f}':>8s} {unit}")
            self.fixes.design_text.SetValue("\n".join(lines))
        self._start(f"checking '{option['title']}' on {stage}…", work, done, what="check a design change", solver=True,
                    local=(lambda: self.on_check_design(stage, option, local=True)) if server is not None else None)

    # ------------------------------------------------------------------ board drawing (jobs, they call KiCad)
    def _need_live(self) -> bool:
        """True in KiCad; in file mode, say that drawing needs KiCad."""
        if not self.session.live:
            wx.MessageBox("Drawing on the board needs the plugin to run inside KiCad.", "groundtruth")
            return False
        return True

    def on_issue_selected(self, issue):
        """Show the clicked issue on the board: its copper selected in KiCad, its net highlighted, the view zoomed
        to it (options on the Issues tab). What to show is worked out (analysis/highlight.py) and sent to KiCad in
        a background thread, under the session's KiCad lock, the newest click winning: the window never waits for
        KiCad. The outcome lands in the status bar."""
        if self.board is None or self.settings is None:
            return
        board, config, routes, session = self.board, self.settings.config, list(self.routes), self.session
        zoom, nets = self.issues_panel.zoom.GetValue(), self.issues_panel.nets.GetValue()
        if session.live:
            self.SetStatusText(f"showing {issue.title} in KiCad…")
            self._highlighted = True        # from now on, closing the window cleans up in KiCad

        def work():
            """(highlight thread) Work out the issue's items and show them in KiCad."""
            from ..analysis.highlight import issue_highlight
            h = issue_highlight(issue, board, config, routes)
            # also with nothing to show: KiCad must not keep showing the previous issue's copper
            result = session.show_issue(h, zoom=zoom, nets=nets) if session.live else None
            wx.CallAfter(self._issue_shown, issue, h, result)
        self._highlighter.submit(work)

    def on_map_mark(self, kind: str, what, text: str, layer: str):
        """Heatmap tab: a marker on a map was clicked (plots.mark_ref_at). Show what it marks in KiCad, as the
        Issues tab shows an issue: the via, pad or track at the marker (an overloaded via, a via carrying current),
        a part marker's footprint, or, for an issue at no particular copper (a hot loop, a route's margin), the
        issue's own copper (issue_highlight). In the highlight thread, the newest click winning."""
        if self.board is None or self.settings is None:
            return
        board, config, routes, session = self.board, self.settings.config, list(self.routes), self.session
        zoom, nets = self.issues_panel.zoom.GetValue(), self.issues_panel.nets.GetValue()
        x, y = what.location if kind == "issue" else (what["x"], what["y"])
        short = text if len(text) <= 90 else text[:89] + "…"
        if not session.live:
            self.SetStatusText(f"{short} (open GroundTruth from KiCad to see it on the board)")
            return
        self.SetStatusText(f"showing {short} in KiCad…")
        self._highlighted = True            # from now on, closing the window cleans up in KiCad

        def work():
            """(highlight thread) Work out the marker's copper and show it in KiCad."""
            from ..analysis.highlight import Highlight, _finish, issue_highlight, point_highlight
            fp = board.footprint(what.get("name", "")) if kind == "part" else None
            if fp is not None:              # a part carrying current: its footprint
                h = _finish(Highlight(focus=[fp.uuid], text=f"{fp.reference} ({fp.value})"), board, config, set())
            else:                           # the copper right at the marker: on the map's layer (a via: any)
                h = point_highlight(board, config, x, y, layer if kind != "via" else "", text=short)
                if h.empty and kind == "issue":
                    h = issue_highlight(what, board, config, routes)
            result = session.show_issue(h, zoom=zoom, nets=nets)
            wx.CallAfter(self._mark_shown, short, h, result)
        self._highlighter.submit(work)

    def _mark_shown(self, text: str, h, result: dict):
        """(GUI thread) Say what was shown for a clicked map marker."""
        if not self:
            return
        if h.empty:
            self.SetStatusText(f"{text}: nothing on the board to select for it")
        elif not result["selected"]:
            self.SetStatusText(f"{text}: its copper is no longer on the board (changed since the analysis?): "
                               "Reload board, then Simulate")
        else:
            self.SetStatusText(f"KiCad: {h.text}" + ("" if h.text == text else f" · {text}"))

    def _issue_shown(self, issue, h, result):
        """(GUI thread) Say what was shown for the issue, or why nothing could be (items gone since the run)."""
        if not self:
            return
        where = f"at {issue.location[0]:.2f}, {issue.location[1]:.2f} mm" + (f" on {issue.layer}" if issue.layer
                                                                              else "")
        if not self.session.live:
            self.SetStatusText(f"{issue.title} {where}: {h.text or 'no board items'} (open GroundTruth from KiCad "
                               "to see it on the board)")
            return
        if h.empty:
            self.SetStatusText(f"{issue.title} {where}: nothing on the board to select for it")
            return
        if not result["selected"]:
            self.SetStatusText(f"{issue.title}: its copper is no longer on the board (changed since the analysis?): "
                               "Reload board, then Run checks")
            return
        missing = len(h.focus) + len(h.context) - result["selected"]
        text = [f"KiCad: {h.text}"] + ([f"{missing} item(s) no longer on the board"] if missing > 0 else [])
        self.SetStatusText(" · ".join(text + result["notes"]))

    def _follow_kicad_selection(self):
        """Timer tick: while the Parts tab shows, ask KiCad (in the follower thread) which footprints are
        selected; a new selection selects their rows."""
        if (self.board is None or self.settings is None or not self.IsShown() or self.IsIconized()
                or self.notebook.GetCurrentPage() is not self.parts or self._follower.busy()):
            return
        session = self.session

        def work():
            """(follower thread) KiCad's selected footprints, unless KiCad is busy."""
            refs = session.selected_parts()
            if refs is not None:
                wx.CallAfter(self._kicad_parts_selected, refs)
        self._follower.submit(work)

    def _kicad_parts_selected(self, refs: list[str]):
        """(GUI thread) A new selection in KiCad: select those parts' rows on the Parts tab."""
        if not self or refs == self._kicad_parts:
            return
        self._kicad_parts = refs            # only a change moves the table: the user may have clicked a row since
        if refs and self.notebook.GetCurrentPage() is self.parts:
            shown = self.parts.select_refs(refs)
            if shown:
                self.SetStatusText(f"selected in KiCad: {', '.join(shown)}")

    def on_nets_selected(self, names: list[str], zoom: bool):
        """The rows selected in the Nets tab changed: select and highlight those nets' copper in KiCad."""
        self._show_rows("nets", names, zoom)

    def on_stages_selected(self, names: list[str], zoom: bool):
        """The rows selected in the Converters tab changed: show those power stages in KiCad (switches, hot-loop
        capacitors, the switch node highlighted)."""
        self._show_rows("stages", names, zoom)

    def _show_rows(self, kind: str, names: list[str], zoom: bool):
        """Show the nets or power stages selected in a table in KiCad (and zoom to them), in the background thread
        that shows issues, the newest selection winning; no rows clears our highlight. Live mode only."""
        if self.board is None or self.settings is None or not self.session.live:
            return
        board, config, session, routes = self.board, self.settings.config, self.session, list(self.routes)
        if names:
            self.SetStatusText(f"highlighting {', '.join(names)} in KiCad…")
        self._highlighted = True            # from now on, closing the window cleans up in KiCad

        def work():
            """(highlight thread) Work out the rows' items and show them in KiCad."""
            from ..analysis.highlight import nets_highlight, stages_highlight
            h = (nets_highlight(names, board, config) if kind == "nets"
                 else stages_highlight(names, board, config, routes))
            result = session.show_issue(h, zoom=zoom, nets=True)
            wx.CallAfter(self._nets_shown, names, h, result)
        self._highlighter.submit(work)

    def _nets_shown(self, names, h, result):
        """(GUI thread) Say what was highlighted for the rows."""
        if not self or not names:
            return
        if h.empty:
            self.SetStatusText(f"{', '.join(names)}: no copper on the board as read")
        elif not result["selected"]:
            self.SetStatusText(f"{', '.join(names)}: its copper is no longer on the board (changed since it was "
                               "read?): Reload board")
        else:
            self.SetStatusText(" · ".join([f"KiCad: {h.text}"] + result["notes"]))

    def _highlight_failed(self, e: Exception):
        """A KiCad that is busy, gone or slow is reported in the status bar; anything else is a bug: shown in
        full."""
        if not self:
            return
        if type(e).__name__ in EXPECTED_ERRORS or type(e).__module__.startswith("kipy"):
            self.SetStatusText(f"could not show it in KiCad: {e}")
        else:
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            wx.MessageBox(f"Could not show it in KiCad:\n{e}\n\n{tb}", "GroundTruth", wx.ICON_WARNING)

    def on_draw_issues(self):
        """Issues tab, "Draw markers on board": every issue's marker on the issues layer, as a job."""
        if self._refuse_if_busy() or not self._need_live() or self.settings is None:
            return
        layer = self.settings.config.drawing.issues_layer
        issues, hidden = list(self.issues), frozenset(self.issues_panel.hidden)
        self._start("drawing the issue markers…", lambda progress: self.session.draw_issues(issues, layer, hidden),
                    lambda n: self.SetStatusText(f"drew {n} marker item(s) on {layer}"),
                    what="draw the issue markers", cancellable=False, kicad=True)

    def _thermal_inputs(self, what: str):
        """(power-stage results of the last Simulate, board, settings) for a thermal job, or None (said why)."""
        stages = [r for r in self.routes if r.kind in ("bridge", "dcload")
                  or r.kind == "converter" and not getattr(r, "view_of", None)]
        board, cfg = self._views_from or (None, None)
        if not stages or board is None or cfg is None:
            self.SetStatusText(f"{what} needs the power stages' DC paths, a DC load or a motor bridge's RMS currents: "
                               "run Simulate with at least one power stage, DC load or bridge first")
            return None
        why = self._stale_reason()
        if why:
            # its heat would be the DC paths of the board and settings as simulated, not as they are now (ISSUE-29)
            self.heatmap.set_stale(why)
            wx.MessageBox(f"{what} works from the last Simulate's DC paths and losses, but {why} since that "
                          f"Simulate: its temperatures would be those of the design as it was. Run Simulate again, "
                          f"then {what}.", "GroundTruth", wx.ICON_INFORMATION)
            return None
        from dataclasses import replace
        from .prefs_dialog import FROM_FILE, defaults
        # the thermal section as the settings have it now (ambient, convection, limits: edited after the Simulate,
        # they were ignored), the DC paths and losses as simulated (unchanged: _stale_reason above)
        if self.settings is not None:
            cfg = replace(cfg, thermal=self.settings.config.thermal)
        quality = defaults(theme.prefs())["thermal_quality"]
        if quality != FROM_FILE:
            cfg = replace(cfg, thermal=replace(cfg.thermal, quality=quality))
        return stages, board, cfg

    def _show_thermal_views(self, keep, new: list, cfg, board):
        """Put thermal views in the Heatmap list (the views `keep` says to keep, then the new ones) and show the
        first new one."""
        self.routes = [r for r in self.routes if keep(r)] + new
        self.heatmap.set_rules(cfg.rules)
        self.heatmap.show(board, self.routes, self.issues)
        self.heatmap.choice.SetSelection(len(self.routes) - len(new))
        self.heatmap._route_changed()
        self.notebook.SetSelection(self.notebook.FindPage(self.heatmap))

    def on_thermal(self, local: bool = False, fresh: bool = False):
        """Top bar, "Thermal": the power stages of the views shown (the last Simulate) heat the board through
        their DC paths and losses (analysis/thermal.py), as a cancellable job (solved on the compute server when
        Preferences say so, unless `local`); the result is a heatmap view of its own (replacing an earlier one)
        and its issues join the list. First (unless `fresh`) the board is read again (_check_board): a design
        changed since the Simulate is refused with a word (_thermal_inputs), not heated as it was."""
        if self._refuse_if_busy():
            return
        if not fresh and self._board_fp is not None and self._views_src is not None:
            self._check_board(lambda: self.on_thermal(local, fresh=True))
            return
        got = self._thermal_inputs("Thermal")
        if got is None:
            return
        stages, board, cfg = got
        cancel = self._cancel
        server = self._server(local)

        def work(progress):
            """(worker) The thermal solve, in a child process (or on the compute server)."""
            # in a process of its own: pyamg holds the GIL for half a second at a time (a 528 ms window stall in
            # a thread); Cancel ends the process
            from ..analysis.thermal_proc import ThermalCancelled, run_in_process
            remote = None
            if server is not None:
                from ..remote.client import Remote
                remote = Remote(*server)
                remote.watch = self._watch("Thermal", board)
            try:
                return run_in_process(board, cfg, stages, progress=progress, cancel=cancel, server=remote)
            except ThermalCancelled:
                raise Cancelled()

        def done(result):
            """Show the temperature view and add its issues."""
            th, issues = result
            if th.error:
                wx.MessageBox(th.error, "GroundTruth: thermal", wx.ICON_WARNING)
                return
            self.issues = sort_issues([i for i in self.issues if i.net != "thermal"] + issues)
            self.issues_panel.show(self.issues)
            # an earlier thermal view is replaced; capacity views stay (they belong to the same Simulate)
            self._show_thermal_views(lambda r: r.kind != "thermal" or getattr(r, "capacity", None),
                                     [th], cfg, board)
            self.SetStatusText(f"thermal: hottest copper {th.t_max:.1f} °C on {th.hotspot_layer}, "
                               f"{th.power_w:.2f} W ({th.copper_w:.2f} W copper, {th.parts_w:.2f} W parts), "
                               f"{len(issues)} issue(s), {th.mesh.get('seconds', 0):.0f} s")
        self._start("thermal: building the model…", work, done, what="solve the temperatures", solver=True,
                    local=(lambda: self.on_thermal(local=True)) if server is not None else None)

    def on_capacity(self, local: bool = False, fresh: bool = False):
        """Heatmap tab, "Current capacity": per power stage of the last Simulate, the load current that brings
        the hottest copper or part junction to thermal.capacity_c (default max_c), the other stages at their
        operating points (analysis/thermal.solve_capacity), in the thermal process (or on the compute server when
        Preferences say so, unless `local`); one view per stage at its capacity (replacing earlier capacity
        views). The board is read again first, as for Thermal."""
        if self._refuse_if_busy():
            return
        if not fresh and self._board_fp is not None and self._views_src is not None:
            self._check_board(lambda: self.on_capacity(local, fresh=True))
            return
        got = self._thermal_inputs("Current capacity")
        if got is None:
            return
        stages, board, cfg = got
        cancel = self._cancel
        server = self._server(local)

        def work(progress):
            """(worker) The capacity search, in the thermal child process (or on the compute server)."""
            from ..analysis.thermal_proc import ThermalCancelled, capacity_in_process
            remote = None
            if server is not None:
                from ..remote.client import Remote
                remote = Remote(*server)
                remote.watch = self._watch("Current capacity", board)
            try:
                return capacity_in_process(board, cfg, stages, progress=progress, cancel=cancel, server=remote)
            except ThermalCancelled:
                raise Cancelled()

        def done(views):
            """Show one view per stage at its capacity (and the first failure, if any)."""
            from ..analysis.thermal import capacity_text
            failed = [v for v in views if v.error]
            if failed:
                wx.MessageBox(failed[0].error, "GroundTruth: current capacity", wx.ICON_WARNING)
            views = [v for v in views if not v.error]
            if not views:
                return
            self._show_thermal_views(lambda r: not getattr(r, "capacity", None), views, cfg, board)
            self.SetStatusText("current capacity to {:g} °C: ".format(views[0].capacity["limit_c"]) + "; ".join(
                f"{v.capacity['stage']} {capacity_text(v.capacity)} ({v.capacity['limited_by']})" for v in views))
        self._start("current capacity: building the thermal model…", work, done, what="find the current capacity",
                    solver=True, local=(lambda: self.on_capacity(local=True)) if server is not None else None)

    def on_sharper(self, k: int):
        """Heatmap tab, "Sharper map": the shown view solved again on a grid with 4x the nodes, as a job (a
        power stage takes up to a minute); the view is replaced by the sharper one."""
        from ..analysis.sharper import describe
        views = self.heatmap.routes
        if not 0 <= k < len(views) or self._refuse_if_busy():
            return
        view = views[k]
        if view.kind == "bridge":
            self.SetStatusText("a bridge's view shows its drive and bus: pick one of its legs for the maps")
            return
        if view.kind == "thermal":
            self.SetStatusText("for a finer thermal map choose Thermal quality: fine in Preferences (or "
                               "thermal.quality in the settings file) and run Thermal again")
            return
        if getattr(view, "sharper", None):
            self.SetStatusText(f"this map is already sharper ({view.sharper['d_mm']:.2f} mm grid)")
            return
        if view.grid is None or view.error:
            self.SetStatusText("this view has no map to sharpen")
            return
        board, cfg = self._views_from or (None, None)
        if board is None or cfg is None:
            self.SetStatusText("run Simulate first")
            return

        def work(progress):
            """(worker) The view solved again on the finer grid."""
            from ..analysis.sharper import sharper_view
            progress(f"solving {view.net} on a finer grid…", 0.1)
            return sharper_view(board, cfg, view)

        def done(new):
            """Replace the view with the sharper one."""
            self.heatmap.replace_view(k, new)
            self.SetStatusText(describe(new))
        self._start(f"sharper map of {view.net}: solving on a finer grid…", work, done, what="sharpen the map",
                    cancellable=False, solver=True)

    def on_draw_contours(self, rr, plane_layer):
        """Heatmap tab, "Draw contours on board": a contour map of the shown layer's current density (or its
        temperature: isotherms) on the current-drawing layer, like a map's height lines with their values written
        on them (results/contours.py), as a job."""
        if rr is None:                          # no view yet (the button is live before any Simulate)
            self.SetStatusText("run Simulate first: there is no map to draw")
            return
        if rr.kind == "bridge":
            self.SetStatusText("a bridge's view shows its drive and bus: pick one of its legs for the maps")
            return
        if plane_layer is None or self._refuse_if_busy() or not self._need_live():
            return
        layer = self.settings.config.drawing.current_layer
        thermal = rr.kind == "thermal"
        label = f"thermal isotherms on {plane_layer}" if thermal else f"{rr.net} {rr.driver}->{rr.receiver} on {plane_layer}"
        what = "temperature isotherm" if thermal else "return current"
        from .prefs_dialog import defaults
        p = defaults(theme.prefs())
        line_mm, index_mm = p["contour_line_mm"], p["contour_index_mm"]      # Preferences: Contour lines

        def work(progress):
            """(worker) The contour map at the heatmap's levels (the peak over all of the result's layers, as the
            heatmap's colour scale; isotherms in °C from the ambient up) and draw it."""
            import numpy as np
            from ..results.contours import contour_map
            peak = max(float(np.nanmax(np.where(rr.layer_grids[n].node, k, np.nan)))
                       for n, k in rr.sheet_current.items())
            unit = "°C" if thermal else (getattr(rr, "heat_unit", None) or
                                         ("A/mm²" if getattr(rr, "view_of", None) else "A/m per A"))
            cmap = contour_map(rr.layer_grids[plane_layer], rr.sheet_current[plane_layer], peak=peak,
                               scale="linear" if thermal else "log", low=rr.ambient_c if thermal else None,
                               unit=unit, what=f"GroundTruth {what} contours, {label}")
            return self.session.draw_contours(cmap, layer, label, line_mm, index_mm)

        def done(counts):
            """Say what was drawn (or that the layer has nothing to draw at the map's levels)."""
            lines, labels = counts
            self.SetStatusText(f"drew {lines} {what} contour line(s) with {labels} value label(s) of {plane_layer} "
                               f"on {layer}" if lines else
                               f"{plane_layer} has no {what} contours to draw (its values are below the map's levels)")

        self._start(f"drawing the {what} contours…", work, done, what="draw the contours", cancellable=False,
                    kicad=True)

    def on_draw_arrows(self, rr, plane_layer):
        """Heatmap tab, "Draw arrows on board": the shown layer's current arrows on the current-drawing layer, as
        a job (their number capped: each arrow is three KiCad items)."""
        if rr is None:                          # no view yet (the button is live before any Simulate)
            self.SetStatusText("run Simulate first: there is no map to draw")
            return
        if rr.kind == "bridge":
            self.SetStatusText("a bridge's view shows its drive and bus: pick one of its legs for the maps")
            return
        if rr.kind == "thermal":
            self.SetStatusText("a temperature map has no current arrows: pick a current view (or Draw contours for "
                               "isotherms)")
            return
        if plane_layer is None or self._refuse_if_busy() or not self._need_live():
            return
        vec = (getattr(rr, "sheet_vectors", None) or {}).get(plane_layer)
        if vec is None:
            self.SetStatusText(f"no current vectors for {plane_layer}: run the simulation again")
            return
        layer = self.settings.config.drawing.current_layer
        label = f"{rr.net} {rr.driver}->{rr.receiver} on {plane_layer}"
        from .prefs_dialog import defaults
        p = defaults(theme.prefs())
        w_min, w_max = p["arrow_min_mm"], p["arrow_max_mm"]

        def work(progress):
            """(worker) Lay out the arrows (as on the heatmap), set each one's width by its density, draw them."""
            import numpy as np
            from ..results.vectors import arrow_field, arrow_widths, reference_density
            # the same full-fill density for every layer of the result, as on the heatmap: a layer carrying little
            # current gets few arrows
            maps = [np.where(rr.layer_grids[n].node, K, 0.0) for n, K in rr.sheet_current.items()]
            ref = reference_density(maps)
            arrows = arrow_field(rr.layer_grids[plane_layer], rr.sheet_current[plane_layer], *vec,
                                 spacing_mm=BOARD_ARROW_SPACING_MM, max_arrows=MAX_BOARD_ARROWS, ref=ref,
                                 with_density=True)
            # line width by the density at each arrow (Preferences: Arrow lines), against the peak of all layers
            peak = max((float(m.max()) for m in maps if m.size), default=0.0)
            widths = arrow_widths([a[4] for a in arrows], peak, w_min, w_max)
            return self.session.draw_arrows([a[:4] + (w,) for a, w in zip(arrows, widths)], layer, label)

        self._start("drawing the current arrows…", work,
                    lambda n: self.SetStatusText(f"drew {n} current arrow(s) of {plane_layer} on {layer}, "
                                                 f"{w_min:g} to {w_max:g} mm by current density"),
                    what="draw the current arrows", cancellable=False, kicad=True)

    # ------------------------------------------------------------------ fixes
    def on_suggest(self, local: bool = False):
        """Fixes tab, "Suggest fixes": the fix search as a job, starting from the last Simulate's models when they
        still apply (same board object, same settings object: see _sim_config), else from a fresh simulation; on
        the compute server when Preferences say so (it simulates its own baseline there), unless `local`."""
        if not self._ready():
            return
        board, cfg = self.board, self._sim_config()
        last = self._last_sim
        server = self._server(local)
        src = (self._board_fp, settings_key(self.settings.config))
        # the last Simulate run is the baseline if neither the board, the settings nor the resolution changed since
        baseline = last[2] if last is not None and last[0] is board and last[1] is cfg and server is None else None

        def work(progress):
            """(worker) Search, verify, and drop the factorisations of the two simulations it keeps; or hand the
            search to the compute server (detached, as Simulate)."""
            if server is not None:
                return self._hand_over(server, "suggest", (board, cfg), {"board": board, "cfg": cfg},
                                       "Suggest fixes", progress)
            from ..analysis.suggest import suggest_fixes
            res = suggest_fixes(board, cfg, progress=progress, baseline=baseline)
            for r in (res.before, res.after):
                if r is not None:
                    r.drop_factorisations()
            return board, res

        self._start("handing the fix search to the compute server…" if server is not None else
                    "searching for fixes" + (" (from the last simulation)…" if baseline else "…"), work,
                    lambda r: self._handed_over(r) if isinstance(r, HandedOver) else self._suggest_done(*r, src=src),
                    what="search for fixes", solver=server is None,
                    local=(lambda: self.on_suggest(local=True)) if server is not None else None)

    def _suggest_done(self, board, res, src: Optional[tuple] = None):
        """Show the suggestions, and the search's baseline simulation as the current results (src: the board
        fingerprint and settings_key it was of)."""
        self._shown_at = time.time()
        self.fixes.show(res)
        # the search starts with a full simulation of the real board: show it like a Simulate run
        self.issues, self.routes = res.before.issues, res.before.routes
        self.issues_panel.show(self.issues)
        self.notebook.SetSelection(self.notebook.FindPage(self.fixes))
        if self.routes:
            self._views_from = (board, self._sim_config())
            self._views_src = src
            self.heatmap.set_stale("")
            self.heatmap.set_rules(self.settings.config.rules if self.settings else None)
            self.heatmap.show(board, self.routes, self.issues)
        self.SetStatusText(f"{len(res.suggestions)} suggestion(s), {len(res.selected)} selected"
                           + solver_note(res.solver))

    # ------------------------------------------------------------------ jobs on the compute server
    def _remote_for(self, address: str):
        """(any thread) A client of the server at `address` with the access key in Preferences now."""
        from ..remote.client import Remote
        from .prefs_dialog import defaults
        return Remote(address, defaults(theme.prefs())["server_key"])

    def _job_servers(self) -> list:
        """(tracker thread) The compute server in Preferences, asked for this computer's jobs besides the listed
        jobs' own servers (SyncTracker.reconcile)."""
        from .prefs_dialog import compute_server
        server = compute_server(theme.prefs())
        return [server[0]] if server else []

    def _hand_over(self, server, kind: str, args: tuple, inputs: dict, label: str, progress) -> "HandedOver":
        """(worker) Send a job to the compute server and keep it in the jobs folder with what showing its result
        will need (`inputs`); ServerUnavailable when the server can't take it (the window then offers a local run)."""
        from ..remote.client import Remote
        remote = Remote(*server)
        progress(f"sending the job to the compute server {remote.host}…", 0.0)
        jid, ahead = remote.submit(kind, args)
        board = inputs["board"]
        name = Path(board.source).name if board.source and not board.source.startswith("kicad-ipc:") else \
            (board.source or "board").split(":", 1)[-1]
        # the server that has the job: the address in Preferences, or the cluster's lead it was sent on to; the
        # resolution it runs at (this computer's Preferences, in the settings handed over) for the Server jobs tab
        meta = self.remote_jobs.add({"id": jid, "server": remote.host, "kind": kind, "label": label,
                                     "resolution": float(inputs["cfg"].analysis.resolution),
                                     "board_path": str(self.board_path or ""), "board_name": name,
                                     "session": self._session_token, "position": ahead,
                                     "server_name": getattr(remote, "server_name", remote.host),
                                     "server_version": getattr(remote, "server_version", "")}, inputs)
        return HandedOver(meta, remote.host)

    def _board_name(self, board) -> str:
        """The board's file name for the Server jobs tab (a KiCad board: its name from the source)."""
        return Path(board.source).name if board.source and not board.source.startswith("kicad-ipc:") else             (board.source or "board").split(":", 1)[-1]

    def _watch(self, label: str, board, cfg=None):
        """(worker) The `watch` that lists a job the window waits for (Thermal, Current capacity, Check in Simulate,
        Compare what-if on the compute server) on the Server jobs tab while it runs, then under Past jobs
        (remote/jobs.watcher; the user, of a Thermal on the server: it was not shown there). `cfg`: the settings a
        simulating job runs with, for its resolution (Thermal and Current capacity have none: their setting is the
        thermal quality)."""
        from ..remote.jobs import watcher
        server = self._server() or ("", "")
        base = {"server": server[0], "kind": label.split(":")[0].split()[0].lower(), "label": label,
                "board_path": str(self.board_path or ""), "board_name": self._board_name(board),
                "session": self._session_token, "pid": os.getpid()}    # job_store.close_stale: whose window
        if cfg is not None:
            base["resolution"] = float(cfg.analysis.resolution)
        return watcher(self.remote_jobs, base, lambda m: wx.CallAfter(self._refresh_jobs))

    def _handed_over(self, h: "HandedOver"):
        """The job is on the server: say so, list it, have the tracker ask about it at once."""
        m = h.meta
        ahead = m.get("position") or 0
        self.SetStatusText(f"{m['label']} handed to the compute server {h.host} as job {m['id']}"
                           + (f" ({ahead} job(s) ahead)" if ahead else "") + ": keep working, or close KiCad; the "
                           "results show when they are ready (Server jobs tab)")
        self._refresh_jobs()
        self._tracker.poke()

    def _refresh_jobs(self):
        """The Server jobs tab from the jobs folder (a few small files), and whether the servers were asked yet."""
        panel = getattr(self, "server_jobs", None)
        if panel is not None:
            panel.refresh(self.remote_jobs.all())
            panel.set_sync(self._tracker.last_round)

    def _jobs_synced(self, when: float):
        """(GUI thread, from the tracker) A round of asking the compute servers is done: the tab's list is theirs."""
        panel = getattr(self, "server_jobs", None) if self else None
        if panel is not None:
            panel.refresh(self.remote_jobs.all())
            panel.set_sync(when)

    def _job_changed(self, meta: dict):
        """(GUI thread, from the tracker) A job's state changed: the tab follows; a finished one is shown at once
        when this window handed it over for the board open here and no results were shown since, else offered."""
        if not self:
            return
        self._refresh_jobs()
        state, what = meta.get("state"), f"{meta.get('label', 'job')} on {meta.get('board_name', '')}"
        if state == "ready":
            mine = (meta.get("session") == self._session_token and self._same_board(meta)
                    and self._shown_at < meta.get("submitted", 0) and self.worker is None)
            if mine:
                self.on_show_job(meta["id"])
            else:
                self.SetStatusText(f"{what}: the result from the compute server is here: Server jobs tab, Show "
                                   "results")
        elif state == "failed":
            self.SetStatusText(f"{what} failed on the compute server {meta.get('server_name', '')}: "
                               f"{meta.get('error', '')} (Server jobs tab, Past jobs: the details)")

    def _same_board(self, meta: dict) -> bool:
        """The job's board is the one open here (by its file)."""
        return bool(self.board_path) and Path(str(self.board_path)).resolve() == Path(meta.get("board_path") or
                                                                                       ".").resolve()

    def on_show_job(self, jid: str):
        """Server jobs tab, Show results (or by itself when ready): the result kept here, shown as if it had run
        here (a Simulate also kept in the History tab); then the job is forgotten here."""
        meta = self.remote_jobs.get(jid)
        if meta is None or meta.get("state") != "ready" or self._refuse_if_busy():
            return
        if not self.remote_jobs.has_inputs(jid):
            wx.MessageBox("This job was listed again from its server after this computer's list had lost it: its "
                          "result is here, but not the board and settings it was run with, so it can't be shown. "
                          "Run Simulate again; Remove from list takes it away.", "groundtruth")
            return
        if not self._same_board(meta):
            wx.MessageBox(f"This result is for {meta.get('board_name')} ({meta.get('board_path')}). Open that board "
                          "(in KiCad, then GroundTruth) to see it; it stays in the Server jobs tab.", "groundtruth")
            return
        jobs, kind = self.remote_jobs, meta["kind"]

        def work(progress):
            """(worker) Read the result and its inputs; a Simulate's views and its history entry."""
            progress("reading the result from the compute server…", 0.1)
            inputs, res = jobs.load(jid)
            res.solver = {**(getattr(res, "solver", None) or {}), "server": f" · on the compute server "
                                                                          f"{meta['server']}"}
            src = (inputs["board"].fingerprint(), settings_key(inputs["cfg"]))    # what it is of (_stale_reason)
            if kind == "suggest":
                return "suggest", inputs["board"], res, src
            keep = inputs.get("keep")
            if isinstance(keep, dict):
                from ..report.history import history_for
                text = keep["settings_text"]
                keep = dict(keep, history=history_for(keep["path"]), settings=lambda t=text: t,
                            t0=time.monotonic() - float(meta.get("run_s") or 0.0))
            kept = keep_run(keep, res, inputs["cfg"], progress) if isinstance(keep, dict) else keep
            return ("simulate", inputs["board"], res.issues, res.routes + result_views(res), None, inputs["cfg"],
                    res.solver, kept, inputs.get("what_if", False), getattr(getattr(res, "checks", None), "si", None),
                    src)

        def done(r):
            """Show it; the job becomes a past job (its files deleted, its line kept in Past jobs)."""
            jobs.finish(jid, "shown", message="results shown")
            self._refresh_jobs()
            if r[0] == "suggest":
                self._suggest_done(r[1], r[2], src=r[3])
            else:
                self._done(*r[1:8], si=r[9], what_if=r[8], src=r[10])
        self._start(f"showing the result of job {jid}…", work, done, what="show the compute server's result")

    def on_cancel_job(self, jid: str):
        """Server jobs tab, Cancel job: stop it on the server (in a background thread: the server may be slow or
        away); only the user does this, a lost connection never does."""
        meta = self.remote_jobs.get(jid)
        if meta is None or meta.get("state") not in ("queued", "running"):
            return
        if wx.MessageBox(f"Stop {meta.get('label')} on {meta.get('board_name')} on the compute server?",
                         "groundtruth: cancel job", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return
        self._stop_job(meta, remove=False)

    def _stop_job(self, meta: dict, remove: bool):
        """Cancel a queued or running job on its server, in a background thread (the server may be slow or away),
        then make it a past job (`remove`: take it off the list). A server that can't be reached is not given up on
        by itself; the user is asked whether to drop the job from the list anyway."""
        from .job_stop import remove_anyway, stop_on_server
        jobs, jid = self.remote_jobs, meta["id"]

        def cancel():
            """(background) Ask the server to stop it."""
            try:
                why = stop_on_server(jobs, self._remote_for(meta["server"]), jid, remove=remove)
            except Exception as e:              # noqa: BLE001 - no client for that server: as unreachable
                why = str(e) or type(e).__name__
            wx.CallAfter(after, why)

        def after(why):
            """(GUI thread) Say what happened; offer to drop a job whose server can't be reached."""
            if not self:
                return
            if why is None:
                self.SetStatusText(f"job {jid} cancelled on the compute server" + (" and removed" if remove else ""))
            elif wx.MessageBox(f"The compute server {meta.get('server_name') or meta.get('server')} can't be "
                               f"reached, so {meta.get('label')} on {meta.get('board_name')} could not be stopped "
                               f"there:\n\n{why}\n\nRemove it from this list anyway? If the server comes back and "
                               "still has the job, it finishes it there; its result is not fetched.",
                               "groundtruth: server not reachable", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
                remove_anyway(jobs, jid)
                self.SetStatusText(f"job {jid} removed from the list (its server was not reachable)")
            else:
                self.SetStatusText(f"job {jid} kept: its server is asked again until it answers")
            self._refresh_jobs()
        threading.Thread(target=cancel, name="gt-cancel-job", daemon=True).start()

    def on_remove_job(self, jid: str):
        """Server jobs tab, Remove from list: forget a job here (one still on the server is cancelled there first,
        after asking; one whose server can't be reached is removed after a second question)."""
        meta = self.remote_jobs.get(jid)
        if meta is None:
            return
        if meta.get("state") in ("queued", "running"):
            if wx.MessageBox(f"Stop {meta.get('label')} on {meta.get('board_name')} on the compute server and remove "
                             "it from the list?", "groundtruth: remove job", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
                self._stop_job(meta, remove=True)
            return
        if meta.get("state") == "ready" and wx.MessageBox(
                f"The result of {meta.get('label')} on {meta.get('board_name')} has not been shown yet. Remove it?",
                "groundtruth", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return
        # a past job (shown, failed, cancelled, lost) just leaves the list
        self.remote_jobs.remove(jid)
        self._refresh_jobs()

    def on_draw_suggestions(self, sugs):
        """Fixes tab, "Draw ticked on board": the ticked suggestions on the suggestion layer, as a job."""
        if not sugs:
            self.SetStatusText("tick at least one suggestion")
            return
        if self._refuse_if_busy() or not self._need_live():
            return
        layer = self.settings.config.drawing.suggest_layer
        self._start("drawing the suggestions…", lambda progress: self.session.draw_suggestions(sugs, layer),
                    lambda n: self.SetStatusText(f"drew {n} item(s) for {len(sugs)} suggestion(s) on {layer}"),
                    what="draw the suggestions", cancellable=False, kicad=True)

    def on_place_vias(self, sugs):
        """Fixes tab, "Place ticked vias in KiCad": after confirming, add the ticked via suggestions to the real
        board (one undo step); they are then switched off as what-if edits and the board is read again."""
        vias = [s.modification for s in sugs if s.modification is not None and s.modification.type == "via"]
        if not vias:
            wx.MessageBox("Tick at least one via suggestion. Capacitors and fills have to be placed by hand.",
                          "groundtruth")
            return
        if self._refuse_if_busy() or not self._need_live():
            return
        text = "\n".join(f"  {m.label()}, {m.size:g} mm pad / {m.drill:g} mm drill" for m in vias)
        if wx.MessageBox(f"Add these {len(vias)} through via(s) to the board?\n\n{text}\n\nThis changes the "
                         "design (one undo step in KiCad). Refill zones and run DRC afterwards.",
                         "groundtruth: place vias", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return

        def placed(n):
            """The vias are on the board: update the settings and reload."""
            # the vias are real now: a what-if run must not count them a second time
            if self.settings.enable_modifications({m.key() for m in vias}, False, "placed in KiCad"):
                self.on_save()
            self.SetStatusText(f"added {n} via(s) to the board; refill zones and run DRC")
            self.on_reload()

        self._start("placing the vias…", lambda progress: self.session.place_vias(vias), placed,
                    what="add the vias", cancellable=False, kicad=True)

    def on_add_whatif(self, sugs):
        """Fixes tab, "Add ticked to what-if": store the ticked suggestions as enabled what-if edits in the
        settings and tick "with what-if edits"."""
        from ..analysis.suggest import as_modifications
        mods = as_modifications(sugs, enabled=True)
        if not mods:
            wx.MessageBox("Tick at least one via, capacitor or fill suggestion.", "groundtruth")
            return
        n = self.settings.add_modifications(mods)
        again = self.settings.enable_modifications({m.key() for m in mods}, True)
        self.on_save()
        self.whatif.SetValue(True)
        self.SetStatusText(f"{n} what-if edit(s) added, {again} re-enabled in {self.settings.path.name}; "
                           "\"with what-if edits\" is ticked")

    def on_compare(self, local: bool = False):
        """Fixes tab, "Compare what-if": the board simulated without and with the enabled what-if edits, as a
        job (both on the compute server when Preferences say so, unless `local`); the comparison is shown on the
        Fixes tab."""
        if not self._ready():
            return
        board, cfg = self.board, self.settings.config
        edits = self._what_if_edits(board, cfg)
        if edits is None:
            return
        server = self._server(local)

        def work(progress):
            """(worker) Both simulations (each half the progress bar) and their comparison as text."""
            from ..analysis.suggest import compare_results, issue_changes
            from ..analysis.whatif import apply_modifications
            from ..report.formats import comparison_text
            board2, cfg2 = apply_modifications(board, cfg, edits)
            before = simulate_on(server, board, cfg, lambda m, f: progress(f"baseline: {m}", 0.5 * f),
                                 watch=self._watch("What-if: baseline", board, cfg) if server else None)
            after = simulate_on(server, board2, cfg2, lambda m, f: progress(f"what-if: {m}", 0.5 + 0.5 * f),
                                watch=self._watch("What-if: with the edits", board, cfg2) if server else None)
            fixed, new = issue_changes(before.issues, after.issues)
            return (f"Baseline vs. {len(edits)} enabled what-if edit(s):\n"
                    + comparison_text(compare_results(before, after), fixed, new))

        self._start("comparing the board with and without the what-if edits…", work, self._compare_done,
                    what="compare the what-if edits", solver=True,
                    local=(lambda: self.on_compare(local=True)) if server is not None else None)

    def _compare_done(self, text: str):
        """Show the comparison on the Fixes tab."""
        self.fixes.show_text(text)
        self.notebook.SetSelection(self.notebook.FindPage(self.fixes))
        self.SetStatusText("comparison done")

    def on_clear(self, _=None):
        """Clear EMI layers: remove everything GroundTruth drew on the board, as a job."""
        self._start("clearing the EMI layers…", lambda progress: self.session.clear(),
                    lambda n: self.SetStatusText(f"removed {n} groundtruth item(s) from the board"),
                    what="clear the EMI layers", cancellable=False, kicad=True)

    def on_waive(self, issue, reason):
        """Waive an issue: its fingerprint and the reason go into the settings, so later runs keep it waived."""
        self.settings.add_waiver(issue.fingerprint, reason)
        issue.waived, issue.waiver_reason = True, reason
        self.on_save()
        self.issues_panel.show(self.issues)

    def _stop_timers(self, evt=None):
        """A wx.Timer outlives its owner window: one still running after the window is destroyed fires into
        freed memory (heap corruption). So every way the window ends stops them, not only the close button.
        EVT_WINDOW_DESTROY is a command event: it also bubbles up from child windows (the hidden tab holder, the
        heatmap window), which must not stop this window's timers."""
        # compared by id: during destruction wxPython may hand back a new wrapper object for this very window
        if evt is not None and evt.GetId() != self._window_id:
            evt.Skip()
            return
        tracker = getattr(self, "_tracker", None)
        if tracker is not None:                 # the jobs stay on the server and on disk for the next start
            tracker.stop()
        for timer in (getattr(self, "_kicad_watch", None), getattr(self, "_inbox_timer", None),
                      getattr(self, "_follow_timer", None), getattr(self, "_clock_timer", None),
                      getattr(self, "_settle_timer", None)):
            if timer is not None:
                timer.Stop()
        if evt is not None:
            evt.Skip()

    def on_close(self, evt):
        """Closing: stop the timers, clear our highlight in KiCad, unregister the inbox, stop a running job, save
        unsaved settings and wait for the save, let a history report finish out of sight, stop the History tab's
        thread."""
        self._stop_timers()
        if self._highlighted and self.session.live:     # KiCad keeps a net highlight after we are gone
            self.Hide()                     # waiting for a show still on its way must not look frozen
            session = self.session
            self._highlighter.submit(lambda: session.clear_highlight())   # replaces a show not yet started
            self._highlighter.flush(3.0)
        if self._inbox is not None:
            self._inbox.close()         # KiCad's button opens a new window from now on
        if self.busy():
            self._cancel.set()          # a running analysis stops at its next progress step; the thread is a daemon
        if self.dirty and self.settings is not None:
            self._saver.save(self.settings)
        self._saver.flush()             # the save thread is a daemon: finish writing before the process ends
        if self._history_reports.busy():   # a history report still being written: let it finish, out of sight
            self._closing.set()             # no longer waiting for the user to be idle (ui/idle.py)
            self.Hide()
            if not self._history_reports.flush(REPORT_CLOSE_WAIT_S):
                self._history_reports.finish(2.0)
        if getattr(self, "history", None) is not None:
            self.history.stop()
        evt.Skip()
