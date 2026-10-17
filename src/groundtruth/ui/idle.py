"""Whether the user has left the mouse and keyboard alone: background work that holds Python's GIL in long C calls
(matplotlib drawing a report's plots) waits for it, so the window answers at once while it is used.

The window's own thread needs the GIL for every event it handles; a plot being drawn holds it for up to about a
second (Agg rendering and PNG encoding are single C calls), whatever the switch interval (ui/gil.py). A history
report page draws 25-50 of them after each Simulate: gui_probe.py on refboard showed view and tab switches
stalling up to 1.3 s while it ran. So that page is drawn only while no input has come for QUIET_S seconds, one plot
at a time (MainFrame._keep_report).

The input's age is the operating system's (any window, so typing in KiCad counts too): GetLastInputInfo on Windows,
CGEventSourceSecondsSinceLastEventType on macOS. Elsewhere it is unknown and taken as idle (no waiting, as before).
No wx here: the CLI-only install imports nothing of wx, and this is called from worker threads."""
from __future__ import annotations

import math
import sys
import time
from typing import Callable

QUIET_S = 3.0                   # s without mouse or keyboard input before background drawing goes on
POLL_S = 0.25                   # s between two looks while waiting


def _windows_idle() -> float:
    """Seconds since the last input on Windows (GetLastInputInfo against GetTickCount, both in ms; the 32-bit tick
    count wraps every 49.7 days, so the difference is taken modulo 2³²)."""
    import ctypes
    from ctypes import wintypes

    class LastInputInfo(ctypes.Structure):
        """LASTINPUTINFO."""
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return math.inf
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


_mac_fn = None


def _mac_idle() -> float:
    """Seconds since the last input on macOS (Quartz: any event type, the HID system's state)."""
    global _mac_fn
    if _mac_fn is None:
        import ctypes
        lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        _mac_fn = lib.CGEventSourceSecondsSinceLastEventType
        _mac_fn.restype = ctypes.c_double
        _mac_fn.argtypes = [ctypes.c_int32, ctypes.c_uint32]
    return float(_mac_fn(1, 0xFFFFFFFF))        # kCGEventSourceStateHIDSystemState, kCGAnyInputEventType


def input_idle_s() -> float:
    """Seconds since the user last moved the mouse, clicked or typed (in any window); inf when the system can't
    say."""
    try:
        if sys.platform == "win32":
            return _windows_idle()
        if sys.platform == "darwin":
            return _mac_idle()
    except (OSError, AttributeError, ValueError):
        pass
    return math.inf


def wait_until_quiet(stop: Callable[[], bool], quiet_s: float = None) -> bool:
    """Block (sleeping, without the GIL) until no input has come for quiet_s seconds (QUIET_S), or stop() is true.
    True when it is quiet, False when stopped."""
    quiet = QUIET_S if quiet_s is None else quiet_s
    while not stop():
        idle = input_idle_s()
        if idle >= quiet:
            return True
        time.sleep(min(max(quiet - idle, 0.05), POLL_S))
    return False
