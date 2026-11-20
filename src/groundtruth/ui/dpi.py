"""Sharp windows on a scaled Windows display: the process declares itself DPI-aware before its first window.

A process that does not declare it is drawn by Windows at 100 % and its bitmap stretched to the display's scaling,
so at 125-150 % every text and line looks blurry (the user, 2026-11-20, on a laptop at 150 % with two external
monitors: "the gui now looks slightly blurry"). KiCad starts the plugin as its own pythonw.exe, whose manifest
declares nothing, so the plugin must do it itself. The windows already size themselves in device-independent pixels
(wx's FromDIP), which is what wxWidgets 3.2 (wxPython 4.2) needs to scale them per monitor.

Per-monitor v2 is asked for first (each window scaled to the monitor it is on, rescaled when it moves: wxWidgets 3.2
handles WM_DPICHANGED), then the older per-monitor and system-wide forms for older Windows. A process whose DPI
awareness is already set (by a manifest or an earlier call) keeps it: the calls then fail, harmlessly. Not Windows:
nothing to do (macOS and GTK scale by themselves). No wx here: it must run before wx makes anything."""
from __future__ import annotations

import sys

PER_MONITOR_AWARE_V2 = -4       # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703+)
PROCESS_PER_MONITOR_DPI_AWARE = 2   # PROCESS_DPI_AWARENESS for shcore.SetProcessDpiAwareness (Windows 8.1+)


def dpi_aware() -> str:
    """Declare this process DPI-aware (Windows; call before the first window). What was set: "per-monitor v2",
    "per-monitor", "system", or "" (not Windows, or the process's awareness was already set and is kept)."""
    if sys.platform != "win32":
        return ""
    import ctypes
    try:
        user32 = ctypes.windll.user32
        fn = user32.SetProcessDpiAwarenessContext
        fn.restype = ctypes.c_bool
        fn.argtypes = [ctypes.c_void_p]
        if fn(ctypes.c_void_p(PER_MONITOR_AWARE_V2)):
            return "per-monitor v2"
    except (AttributeError, OSError):    # Windows before 10 1703: no such function
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0:     # S_OK
            return "per-monitor"
    except (AttributeError, OSError):    # Windows before 8.1
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    return ""
