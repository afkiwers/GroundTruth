"""Is the KiCad that started the plugin still running? The plugin's window is a process of its own and would
outlive KiCad, with every board button failing; the main window closes itself once KiCad has exited.
Watches the process, never the API: no requests to KiCad.

On Windows KiCad starts the venv's Scripts\\pythonw.exe, a launcher that runs KiCad's own pythonw.exe as its child,
so KiCad is the grandparent; elsewhere it is the parent."""
from __future__ import annotations

import os
import sys
from typing import Callable, Optional

KICAD_EXES = ("kicad", "pcbnew")
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


def find_kicad(procs: dict[int, tuple[int, str]], pid: int, levels: int = 4) -> Optional[int]:
    """The nearest ancestor of `pid` that is a KiCad executable. procs: pid -> (parent pid, exe name)."""
    for _ in range(levels):
        parent = procs.get(pid, (0, ""))[0]
        if not parent or parent not in procs:
            return None
        if procs[parent][1].lower().startswith(KICAD_EXES):
            return parent
        pid = parent
    return None


def _process_table() -> dict[int, tuple[int, str]]:
    """{pid: (parent pid, exe name)} of every process (Windows: a Toolhelp32 snapshot through ctypes); {} when the
    snapshot fails."""
    import ctypes
    import ctypes.wintypes as wt

    class PROCESSENTRY32W(ctypes.Structure):
        """The Win32 PROCESSENTRY32W record (one process of the snapshot)."""
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)            # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    procs: dict[int, tuple[int, str]] = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(wt.HANDLE(snap), ctypes.byref(e))
        while ok:
            procs[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            ok = k32.Process32NextW(wt.HANDLE(snap), ctypes.byref(e))
    finally:
        k32.CloseHandle(wt.HANDLE(snap))
    return procs


def exited_check(pid: int) -> Optional[Callable[[], bool]]:
    """A non-blocking `exited()` for process `pid` (Windows: a SYNCHRONIZE handle, so a reused pid can't fool
    it), or None if the process can't be opened."""
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wt.HANDLE
    handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return None
    return lambda: k32.WaitForSingleObject(wt.HANDLE(handle), 0) == WAIT_OBJECT_0


def watch_kicad() -> Optional[Callable[[], bool]]:
    """`gone()` for the KiCad that started this process, or None when it can't be found (then nothing is
    watched). Never raises."""
    try:
        if sys.platform == "win32":
            pid = find_kicad(_process_table(), os.getpid())
            return exited_check(pid) if pid else None
        parent = os.getppid()
        return None if parent <= 1 else (lambda: os.getppid() != parent)   # orphans are re-parented
    except Exception:
        return None
