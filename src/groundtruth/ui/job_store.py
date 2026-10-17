"""The window's server-job list kept safe when several plugin windows share it, and rebuilt from the servers.

The user, 2026-10-16: "current jobs are lost after a while, make sure the plugin queries the server and rebuilds the
list". Two plugin windows were open (each a process with its own remote/jobs.Tracker): both rewrote every job's
job.json through the same `job.json.tmp`, and when a shorter write landed over a longer one the file kept the longer
one's last byte (`...}}`). LocalJobs.get read that as no job at all: a running Simulate, a queued one and a finished
result left Current jobs, though the server had them (and its queue position, "2 job(s) ahead", counted them).

Kept in ui/ on purpose (like job_stop.py): remote/ counts toward the code fingerprint the servers compare with the
plugin's (protocol.code_id skips ui/), and this is only the window's bookkeeping.

- SafeJobs: each writer writes a temporary file of its own and replaces job.json with it (retried while another
  process reads the file); a job.json with trailing bytes is repaired, not dropped; a job finished here (result
  here, shown, cancelled, failed, lost) is not set back to queued or running by another window's tracker, nor a
  result here to lost; a job removed from the list is remembered (`removed/`), so the rebuild leaves it out.
- SyncTracker: also every RECONCILE_S asks each server it knows (the jobs' servers and the one in Preferences) for
  this computer's jobs, and lists those missing here (a Simulate or Suggest fixes still queued, running or done);
  one whose result another window already fetched (the server no longer knows it) is kept as ready, not lost.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Optional

from ..remote.jobs import FINAL, LocalJobs, Tracker, state_fields

RECONCILE_S = 30.0               # how often the servers are asked for this computer's whole job list
REPLACE_TRIES = 40               # job.json replaced while another process reads it: retried this often, 25 ms apart
RECOVER_KINDS = {"simulate": "Simulate", "suggest": "Suggest fixes"}     # jobs handed over (not waited for)


class SafeJobs(LocalJobs):
    """LocalJobs for several windows at once (see the module)."""

    def _write(self, meta: dict):
        """(under the lock) job.json, atomically, through a temporary file of this process and thread: two
        windows writing one shared temporary file interleaved their bytes."""
        d = self._dir(meta["id"])
        tmp = d / f"job.json.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        for k in range(REPLACE_TRIES):
            try:
                tmp.replace(d / "job.json")
                return
            except PermissionError:             # Windows: another process has job.json open for reading
                if k == REPLACE_TRIES - 1:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    raise
                time.sleep(0.025)

    def get(self, jid: str) -> Optional[dict]:
        """A job's meta, or None. A job.json with bytes after its JSON (left by the old shared temporary file) is
        read up to the end of the JSON and written back repaired."""
        try:
            text = (self._dir(jid) / "job.json").read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return json.loads(text)
        except ValueError:
            pass
        try:
            meta, _ = json.JSONDecoder().raw_decode(text.strip())
        except ValueError:
            return None
        if not isinstance(meta, dict) or meta.get("id") != jid:
            return None
        with self.lock:
            try:
                self._write(meta)
            except OSError:
                pass                                # repaired next time; the meta is good either way
        return meta

    def update(self, jid: str, **fields) -> Optional[dict]:
        """Change a job's meta; None when the job is gone. A job finished here stays finished: another window's
        tracker (its state from before) can't set it back to queued or running, nor a result here to lost."""
        with self.lock:
            meta = self.get(jid)
            if meta is None:
                return None
            new = fields.get("state")
            if new is not None and meta.get("state") in FINAL:
                if new not in FINAL or (new == "lost" and self.has_result(jid)):
                    return meta
            meta.update(fields)
            self._write(meta)
            return meta

    def has_result(self, jid: str) -> bool:
        """The job's result is here (fetched by this window or another)."""
        return (self._dir(jid) / "result.bin").exists()

    def has_inputs(self, jid: str) -> bool:
        """What showing the result needs (the board and settings as handed over) is here: not for a job the
        rebuild listed again from its server without them."""
        return (self._dir(jid) / "input.pickle").exists()

    def remove(self, jid: str):
        """Forget a job here, and remember that it was removed (the rebuild from the servers leaves it out)."""
        super().remove(jid)
        try:
            gone = self.folder / "removed"
            gone.mkdir(parents=True, exist_ok=True)
            (gone / jid).touch()
        except OSError:
            pass

    def removed(self) -> set:
        """The ids of the jobs removed from the list."""
        try:
            return {p.name for p in (self.folder / "removed").iterdir()}
        except OSError:
            return set()

    def recover(self, address: str, info: dict) -> Optional[dict]:
        """List again a job a server has for this computer but the list lost (no inputs: its result can be fetched
        and its details read, but not shown; see SafeJobs.has_inputs)."""
        kind = info.get("kind", "")
        meta = {"id": info["id"], "server": address, "kind": kind, "label": RECOVER_KINDS.get(kind, kind),
                "board_name": "(listed again from the server)", "board_path": "", "session": "",
                "submitted": info.get("queued_at") or time.time(), "recovered": True,
                "state": "queued", "fraction": 0.0, "message": "", "error": "", "unreachable_since": None,
                **state_fields(info)}
        with self.lock:
            if self.get(info["id"]) is not None:
                return None
            self._dir(info["id"]).mkdir(parents=True, exist_ok=True)
            self._write(meta)
        return meta


class SyncTracker(Tracker):
    """Tracker that also rebuilds the list from the servers (see the module). `servers()`: addresses to ask
    besides the jobs' own (the compute server in Preferences)."""

    def __init__(self, jobs: SafeJobs, remote: Callable[[str], object], on_change: Callable[[dict], None],
                 servers: Callable[[], list] = lambda: []):
        """Not started yet: start()."""
        super().__init__(jobs, remote, on_change)
        self.servers = servers
        self._reconciled = 0.0

    def check(self):
        """One round of Tracker.check, then (every RECONCILE_S) the rebuild from the servers. A job the server no
        longer knows but whose result is here (another window fetched it) stays ready."""
        super().check()
        for meta in self.jobs.all():
            if meta.get("state") == "lost" and self.jobs.has_result(meta["id"]):
                new = self.jobs.update(meta["id"], state="ready", error="", detail="", message="result here")
                if new is not None:
                    self.on_change(new)
        if time.monotonic() - self._reconciled >= RECONCILE_S:
            self._reconciled = time.monotonic()
            self.reconcile()

    def reconcile(self) -> list[dict]:
        """Ask every known server for this computer's jobs; list the ones missing here (queued, running or done
        Simulates and Suggest fixes). Returns the jobs added."""
        from ..remote.client import RemoteError, ServerUnavailable
        known = {m["id"] for m in self.jobs.all()} | self.jobs.removed()
        addresses = {m["server"] for m in self.jobs.all() if m.get("server")}
        try:
            addresses |= {a for a in self.servers() if a}
        except Exception:                           # noqa: BLE001 - the Preferences may be mid-edit
            pass
        added = []
        for address in sorted(addresses):
            if self._stop.is_set():
                break
            try:
                listed = self.remote(address).jobs()
            except (ServerUnavailable, RemoteError, OSError, ValueError):
                continue                            # asked again next time
            for info in listed:
                if info.get("id") in known or info.get("kind") not in RECOVER_KINDS or \
                        info.get("state") not in ("queued", "running", "done"):
                    continue
                meta = self.jobs.recover(address, info)
                if meta is not None:
                    known.add(meta["id"])
                    added.append(meta)
                    self.on_change(meta)
        return added


def _alive(pid: int) -> bool:
    """Whether process `pid` still runs (psutil when there, else the operating system asked directly)."""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259                                             # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def close_stale(jobs: LocalJobs) -> int:
    """remote/jobs.close_stale for several windows: a waited-for job still listed as running is ended (cancelled)
    only when the window that waits for it is gone (its process, `pid` in its entry), not when another window
    starts. Entries from before the pid was kept are ended as before. How many."""
    n = 0
    for meta in jobs.all():
        if meta.get("attached") and meta.get("state") not in FINAL:
            pid = meta.get("pid")
            if pid and pid != os.getpid() and _alive(int(pid)):
                continue
            jobs.finish(meta["id"], "cancelled", error="the window waiting for it was closed",
                        detail="the window waiting for it was closed before it ended")
            n += 1
    return n
