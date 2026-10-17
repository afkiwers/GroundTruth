"""How much memory the machine has free (psutil when installed: the plugin has it, a plain CLI install may not).

On Windows "free" is the smaller of two figures: the physical memory available, and the **commit** still available
(the commit limit, RAM plus page file, minus what every program has committed). Windows refuses an allocation when
the commit is used up, however much RAM is free, and SuperLU commits about 6.5x the fill it touches (ARCHITECTURE
"Memory use of a Simulate"): on the user's compute server a job failed for want of memory while Task Manager showed
the RAM at 50 % and the job using about 20 GB (2026-10-14). Planning the parallel solves by the RAM alone
overcommitted; by the smaller figure, fewer solves run at once instead."""
from __future__ import annotations

import sys
from typing import Optional


def commit_available() -> Optional[int]:
    """Windows: the commit still available (bytes; GlobalMemoryStatusEx's ullAvailPageFile, which despite its name
    is the commit limit minus the commit charge). None elsewhere or when it can't be told."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            """The Win32 structure GlobalMemoryStatusEx fills."""
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return int(st.ullAvailPageFile)
    except Exception:           # noqa: BLE001 - no ctypes/windll (an odd Python): unknown
        return None


def available_bytes() -> Optional[int]:
    """Memory available now (bytes): the physical memory available, on Windows at most the commit still available
    (see the module); None when it can't be told (no psutil)."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        phys = int(psutil.virtual_memory().available)
    except Exception:           # noqa: BLE001 - a sandbox without /proc, an odd platform: unknown
        return None
    commit = commit_available()
    return phys if commit is None else min(phys, commit)
