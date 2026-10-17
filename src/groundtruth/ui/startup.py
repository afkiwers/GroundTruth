"""Start-up timing of the plugin: when KiCad created its process, when Python reached our first line, and each
step until the window shows the board. Appended to a small per-user log, so a slow start can be diagnosed after
the fact: %LOCALAPPDATA%\\GroundTruth\\plugin.log on Windows, ~/.cache/groundtruth/plugin.log elsewhere.
Only times and counts are logged (no board or net names). Logging never fails the plugin."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

MAX_BYTES = 200_000      # beyond this the older half of the log is dropped


def log_path() -> Path:
    """Where the start-up log goes: %LOCALAPPDATA%\\GroundTruth\\plugin.log on Windows, ~/.cache elsewhere."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "GroundTruth" / "plugin.log"
    return Path.home() / ".cache" / "groundtruth" / "plugin.log"


def process_created() -> Optional[float]:
    """This process's creation time (epoch seconds; Windows only). The gap to the script's first line is
    Python's own start-up (interpreter, site-packages)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wt.HANDLE
        times = [wt.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(k32.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
            return None
        ft = times[0]
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7 - 11644473600.0   # 100 ns ticks since 1601
    except Exception:
        return None


class StartupLog:
    """The plugin's start-up steps, each with its time since the process was created, appended to the log file
    (so a slow start under KiCad can be found afterwards)."""

    def __init__(self, action: str, started: Optional[float] = None, path: Optional[Path] = None):
        """Begin a start-up's log: the action, when the process and the script started."""
        self.path = path or log_path()
        script = started or time.time()
        created = process_created()
        self.t0 = created if created and created <= script else script
        self._write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.t0))}  {action}  "
                    f"(PID {os.getpid()}, Python {sys.version.split()[0]})")
        if created and created <= script:
            self._write(f"  {0.0:6.2f} s  process created")
        self._write(f"  {script - self.t0:6.2f} s  script started")

    def mark(self, what: str):
        """Log a step with its time since the start."""
        self._write(f"  {time.time() - self.t0:6.2f} s  {what}")

    def _write(self, line: str):
        """Append a line, halving the file when it is over MAX_BYTES; a log that cannot be written is skipped."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                keep = self.path.read_bytes()[-MAX_BYTES // 2:]
                self.path.write_bytes(keep[keep.find(b"\n") + 1:])
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


_current: Optional[StartupLog] = None


def begin(action: str, started: Optional[float] = None) -> StartupLog:
    """Start the log of this start-up (mark() then logs to it)."""
    global _current
    _current = StartupLog(action, started)
    return _current


def mark(what: str, last: bool = False):
    """Log a start-up step if the plugin is starting (no-op otherwise, e.g. in file mode or tests); `last` ends
    the start-up log, so later reloads are not logged."""
    global _current
    if _current is not None:
        _current.mark(what)
        if last:
            _current = None
