"""Hand KiCad's selection to the GroundTruth window of the same board.

KiCad starts every plugin action as a process of its own. The "Add selected net" button's process writes a
request file into the board's inbox and, when a GroundTruth window is open on that board, waits until the
window has taken it (the window checks twice a second). With no window open it opens one itself, which takes
the request once it has read the board. So the window stays the only writer of the settings file.

Per user and board: %LOCALAPPDATA%\\GroundTruth\\windows\\<board key>\\ (~/.cache/groundtruth/windows/...
elsewhere), holding window.json (the open window's PID) and requests/*.json. Requests carry net names only,
checked before use; the folder is the user's own. No wx here."""
from __future__ import annotations

import hashlib
import json
import re
import os
import sys
import time
from pathlib import Path
from typing import Optional

MAX_NETS = 50
MAX_NAME = 300
NO_SELECTION = ("Nothing with a net is selected in KiCad's PCB editor.\n\nClick (or right-click) a track, via or "
                "pad there, then try again.")


def windows_root() -> Path:
    """The folder where open windows register and requests wait (per user)."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "GroundTruth" / "windows"
    return Path.home() / ".cache" / "groundtruth" / "windows"


def board_key(board_path) -> str:
    """The same key for every spelling of the board's path (case and separators on Windows)."""
    p = os.path.normcase(os.path.abspath(str(board_path)))
    return hashlib.sha256(p.encode("utf-8")).hexdigest()[:16]


def pid_alive(pid: int) -> bool:
    """Whether process `pid` is still running (Windows: open it and test whether waiting on it times out, i.e. it
    has not exited; elsewhere: signal 0)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wt
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wt.HANDLE
        handle = k32.OpenProcess(0x00100000 | 0x1000, False, pid)   # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(wt.HANDLE(handle), 0) == 0x102   # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(wt.HANDLE(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_json(path: Path, data: dict):
    """Atomic: a reader never sees half a file (the temporary name does not end in .json)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _valid_nets(data) -> list[str]:
    """A request's net names, checked: a list of non-empty strings, at most MAX_NETS of at most MAX_NAME
    characters (a request file comes from another process: nothing in it is trusted)."""
    nets = data.get("nets") if isinstance(data, dict) else None
    if not isinstance(nets, list):
        return []
    return [n for n in nets[:MAX_NETS] if isinstance(n, str) and 0 < len(n) <= MAX_NAME]


PAD_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")    # a KiCad item id (a UUID); anything else in a request is dropped


def _valid_pads(data) -> list[str]:
    """A request's selected pad ids, checked like the net names (a list of UUID-shaped strings, at most MAX_NETS):
    a path picked in KiCad starts or ends at a selected pad. Missing (an older request): none."""
    pads = data.get("pads") if isinstance(data, dict) else None
    if not isinstance(pads, list):
        return []
    return [u for u in pads[:MAX_NETS] if isinstance(u, str) and PAD_ID.match(u)]


class Inbox:
    """The window's side: registers this process as the board's window and hands out each request once."""

    def __init__(self, board_path, root: Optional[Path] = None):
        """Register this process as the board's window (window.json with its PID) and make the requests folder."""
        self.folder = (root or windows_root()) / board_key(board_path)
        self.requests = self.folder / "requests"
        self.requests.mkdir(parents=True, exist_ok=True)
        self.window_file = self.folder / "window.json"
        _write_json(self.window_file, {"pid": os.getpid(), "board": str(board_path)})

    def take(self) -> list[list[str]]:
        """The waiting requests' net names, oldest first (take_requests without the pads)."""
        return [r["nets"] for r in self.take_requests()]

    def take_requests(self) -> list[dict]:
        """The waiting requests, oldest first, each {"nets": [names], "pads": [selected pad ids]}. Each is removed;
        when two windows of the board are open, only one of them gets it."""
        out = []
        try:
            files = sorted(self.requests.glob("*.json"))
        except OSError:
            return out
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            try:
                f.unlink()
            except OSError:
                continue                    # taken by another window meanwhile
            nets = _valid_nets(data)
            if nets:
                out.append({"nets": nets, "pads": _valid_pads(data)})
        return out

    def close(self):
        """Unregister (only if the registration is still this process's: another window may have taken over)."""
        try:
            if json.loads(self.window_file.read_text(encoding="utf-8")).get("pid") == os.getpid():
                self.window_file.unlink()
        except (OSError, ValueError, AttributeError):
            pass


def open_window_pid(board_path, root: Optional[Path] = None) -> Optional[int]:
    """PID of the GroundTruth window registered for the board, if that process still runs."""
    try:
        data = json.loads(((root or windows_root()) / board_key(board_path) / "window.json")
                          .read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return pid if pid != os.getpid() and pid_alive(pid) else None


def send(board_path, nets: list[str], wait: float = 3.0, root: Optional[Path] = None, pads: list[str] = ()) -> bool:
    """The button's side: leave a request for the board's window. True when an open window took it within
    `wait` seconds. False when no window is open or none took it: the request then waits in the inbox for the
    window this process opens."""
    requests = (root or windows_root()) / board_key(board_path) / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    target = requests / f"{time.time_ns()}-{os.getpid()}.json"
    _write_json(target, {"nets": list(nets), "pads": list(pads)})
    pid = open_window_pid(board_path, root)
    if pid is None:
        return False
    if sys.platform == "win32":                 # this process was started by a click: let the window come forward
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(pid)
        except (AttributeError, OSError):       # best effort: the window still opens, perhaps behind KiCad
            pass
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not target.exists():
            return True
        time.sleep(0.05)
    return not target.exists()
