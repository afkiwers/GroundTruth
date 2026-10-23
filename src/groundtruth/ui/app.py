"""Plugin / GUI entry points.

    groundtruth-plugin analyse   (KiCad toolbar action: connect over IPC and open the window)
    groundtruth-plugin clear     (KiCad toolbar action: remove everything groundtruth drew)
    groundtruth-gui BOARD.kicad_pcb   (same window on a file, without KiCad; drawing disabled)
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Optional

import wx

from . import startup
from .gil import GUI_SWITCH_INTERVAL, responsive_gui  # noqa: F401  (GUI_SWITCH_INTERVAL: for tests)


def _error(msg: str):
    """An error message box for the user."""
    wx.MessageBox(msg, "GroundTruth", wx.ICON_ERROR)


def _icon() -> Optional[Path]:
    """The 48 px plugin icon: the package's own (ui/icons), else the installed plugin's folder or the repo's (None
    when none has it)."""
    here = Path(__file__).resolve()
    for d in (here.parent / "icons", here.parents[2] / "icons", here.parents[3] / "plugin" / "icons"):
        if (d / "groundtruth-48.png").exists():
            return d / "groundtruth-48.png"
    return None


class StartWindow(wx.Frame):
    """Shown the moment Python has started, so a click on the toolbar button gets an answer at once. Meanwhile a
    worker thread connects to KiCad and loads the analysis modules (numpy, scipy, matplotlib, shapely); this
    window's event loop keeps running, so it never stops responding, and its bar pulses to show it."""

    def __init__(self):
        """A small borderless window, centred and on top: icon, title, a status line and a pulsing bar (its
        timer stopped when the window is destroyed: a timer must never outlive its window)."""
        super().__init__(None, title="GroundTruth – starting",
                         style=wx.FRAME_NO_TASKBAR | wx.STAY_ON_TOP | wx.BORDER_SIMPLE)
        panel = wx.Panel(self)
        row = wx.BoxSizer(wx.HORIZONTAL)
        icon = _icon()
        if icon:
            row.Add(wx.StaticBitmap(panel, bitmap=wx.Bitmap(str(icon), wx.BITMAP_TYPE_PNG)), 0,
                    wx.ALL | wx.ALIGN_CENTER_VERTICAL, 14)
        col = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(panel, label="GroundTruth")
        title.SetFont(title.GetFont().Scaled(1.6).Bold())
        self.text = wx.StaticText(panel, label="Starting…")
        self.gauge = wx.Gauge(panel, range=100, size=(-1, 8))
        col.Add(title, 0, wx.BOTTOM, 4)
        col.Add(self.text, 0)
        col.Add(self.gauge, 0, wx.EXPAND | wx.TOP, 10)
        col.SetMinSize((250, -1))
        row.Add(col, 1, wx.TOP | wx.BOTTOM | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 14)
        panel.SetSizer(row)
        row.Fit(self)
        self.Centre()
        self._pulse = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _: self.gauge.Pulse(), self._pulse)
        self.Bind(wx.EVT_WINDOW_DESTROY, lambda e: (self._pulse.Stop(), e.Skip()))   # never outlive the window
        self._pulse.Start(80)
        self.Show()

    def status(self, text: str):
        """Show what start-up is doing now."""
        self.text.SetLabel(text)
        self.Layout()

    def close(self):
        """Close the window (the start-up is over, or failed)."""
        self._pulse.Stop()
        self.Destroy()


def _connect(log):
    """(KiCad board handle, None) or (None, (log line, message for the user))."""
    from ..adapters.kipy_source import KiCadConnectionError, connect
    try:
        kb = connect().get_board()
    except KiCadConnectionError as e:
        return None, ("no connection to KiCad", str(e))
    except Exception as e:
        return None, ("no board open", f"No board is open in the PCB editor ({e})")
    log.mark("connected to KiCad")
    return kb, None


def _clear(log) -> int:
    """The "Clear GT drawings" action. No window of ours is open while it talks to KiCad."""
    kb, failure = _connect(log)
    if failure:
        log.mark(failure[0])
        _error(failure[1])
        return 2
    from ..adapters.kipy_sink import clear
    try:
        n = clear(kb)
    except Exception as e:
        _error(f"Could not clear GroundTruth's drawings:\n{e}")
        return 2
    startup.mark(f"cleared {n} item(s)", last=True)
    wx.MessageBox(f"Removed {n} GroundTruth item(s) from the board (one undo step).", "GroundTruth")
    return 0


def _add_selected_net(log):
    """The "Add selected net" action: hand the nets selected in KiCad to the open GroundTruth window of this
    board (ui/inbox.py). Returns (exit code, None) when done, or (None, KiCad board) when no window took them:
    this process then opens the window, which takes the request once it has read the board."""
    kb, failure = _connect(log)
    if failure:
        log.mark(failure[0])
        _error(failure[1])
        return 2, None
    from ..adapters.kipy_source import board_file_path, selected_nets, selected_pad_ids, selection_kinds
    from .inbox import NO_SELECTION, send
    try:
        nets = selected_nets(kb)
        pads = selected_pad_ids(kb) if len(nets) > 1 else []     # several nets: maybe one path, its ends' pins
        kinds = [] if nets else selection_kinds(kb)
        path = board_file_path(kb)
    except Exception as e:                      # KiCad busy (a dialog open), connection lost
        _error(f"Could not read KiCad's selection:\n{e}")
        return 2, None
    if not nets:
        what = ", ".join(sorted(set(kinds)))
        startup.mark(f"nothing selected with a net ({len(kinds)} item(s){': ' + what if kinds else ''})", last=True)
        seen = (f"\n\nKiCad reports {len(kinds)} selected item(s) without a net ({what})." if kinds else
                "\n\nKiCad reports an empty selection.")
        wx.MessageBox(NO_SELECTION + seen, "GroundTruth", wx.ICON_INFORMATION)
        return 2, None
    if not path:
        _error("Save the board first, so its settings can be stored next to it.")
        return 2, None
    if send(path, nets, pads=pads):
        startup.mark(f"{len(nets)} net(s) handed to the open window", last=True)
        return 0, None
    log.mark(f"{len(nets)} net(s) waiting; no window open: opening one")
    return None, kb


def plugin_main(argv: Optional[list[str]] = None, started: Optional[float] = None) -> int:
    """started: time.time() at the entry script's first line (for the start-up log).

    The GUI thread only runs windows: connecting to KiCad and loading the analysis modules happen in a worker
    thread, so no window of the plugin ever stops responding (Windows marks a window "Not Responding" after 5 s
    without handling messages)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "analyse"
    log = startup.begin(action, started)
    responsive_gui()
    app = wx.GetApp() or wx.App(False)
    from . import theme
    theme.init_app(app)                  # before the first window: the appearance cannot change afterwards
    if action == "clear":
        return _clear(log)
    connected = None
    if action == "add-net":
        code, connected = _add_selected_net(log)
        if code is not None:
            return code
    win = StartWindow()
    log.mark("start window shown")
    exit_code = [0]

    def failed(what: str, msg: str):
        """(GUI thread) Start-up failed: log it, tell the user, end with exit code 2."""
        startup.mark(what, last=True)
        _error(msg)
        exit_code[0] = 2
        win.close()                         # the last window: the event loop ends

    def opened(kb):
        """(GUI thread) The modules are loaded: open the main window on KiCad's board, then close this one."""
        try:
            from .main_frame import MainFrame, Session      # imported by the worker already
            win.status("Opening the window…")
            frame = MainFrame(Session(kicad_board=kb))
            frame.Show()
        except Exception as e:
            failed(f"window failed ({type(e).__name__})", f"GroundTruth could not open its window:\n{e}")
            return
        win.close()
        log.mark("main window shown")

    def work():
        """(worker thread) Connect to KiCad unless done already, import the analysis modules, then hand over to
        the GUI thread (wx.CallAfter: wx may only be used from the GUI thread)."""
        try:
            kb = connected
            if kb is None:
                wx.CallAfter(win.status, "Connecting to KiCad…")
                kb, failure = _connect(log)
                if failure:
                    wx.CallAfter(failed, *failure)
                    return
            wx.CallAfter(win.status, "Loading the analysis modules…")
            from . import main_frame  # noqa: F401  (numpy, scipy, matplotlib, shapely: the slow part)
            log.mark("analysis modules imported")
            wx.CallAfter(opened, kb)
        except Exception as e:
            wx.CallAfter(failed, f"start-up failed ({type(e).__name__})", f"GroundTruth could not start:\n{e}")

    threading.Thread(target=work, name="groundtruth-start", daemon=True).start()
    old_log = wx.Log.SetActiveTarget(_LogToFile(startup.log_path()))
    try:
        app.MainLoop()
    finally:
        wx.Log.SetActiveTarget(old_log)
    return exit_code[0]


class _LogToFile(wx.Log):
    """wxWidgets' own messages into plugin.log. On stderr, KiCad showed them in a Messages dialog over the board
    (the user saw "Plugin action 'Analyse return paths': ...window.cpp(539): 'SetFocus' failed with error
    0x00000057"): they are notes for whoever reads the log, not something the user can act on."""

    def __init__(self, path: Path):
        """Log into the file `path`."""
        super().__init__()
        self.path = path

    def DoLogTextAtLevel(self, level, msg):
        """wx calls this for each message: append it with the time (a log that cannot be written is skipped)."""
        import time
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  wx: {msg}\n")
        except OSError:
            pass


def gui_main(argv: Optional[list[str]] = None) -> int:
    """The groundtruth-gui command: the plugin window on a board file, without KiCad (for development and
    for looking at a board's results outside KiCad)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: groundtruth-gui BOARD.kicad_pcb", file=sys.stderr)
        return 2
    from .main_frame import MainFrame, Session
    responsive_gui()
    app = wx.App(False)
    from . import theme
    theme.init_app(app)
    frame = MainFrame(Session(file_path=argv[0]))
    frame.Show()
    app.MainLoop()
    return 0
