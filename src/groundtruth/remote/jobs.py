"""The plugin's jobs on the compute server, followed detached from the window (the user, 2026-10-14: "the client
should not need to be connected all the time. if the laptop sleeps and wakes up it should simply reconnect";
"once a sim is done, but results not fetched yet, it should allow a new sim to start").

A Simulate or Suggest fixes sent to the server is handed over and the window is free again at once. Each job is
kept on this computer in `%LOCALAPPDATA%\\GroundTruth\\remote_jobs\\<id>\\`:

- `job.json`: what the Server jobs tab lists (server, kind, board, when, state, progress, error);
- `input.pickle`: what showing the result needs later: the board and settings as simulated and the history's
  inputs (written here, by this plugin: a file of this user's own folder);
- `result.bin`: the result once fetched (as the server sent it: signed-checked, zlib of a pickle), so the server can
  forget it at once (results are removed there once fetched).

`Tracker` asks the server about the unfinished jobs every POLL_S in a background thread; a server it can't reach
is simply asked again later (the laptop asleep, out of the office: nothing is given up, only the user cancels a
job). A finished job's result is fetched and stored, then `on_change` tells the window (which shows it, or offers
it on the Server jobs tab). Nothing here imports wx.

The jobs the window waits for (Thermal, Current capacity, Check in Simulate, Compare what-if on the server:
client.Remote.run) are listed too (the user, 2026-10-14: "thermal sims are not shown"): `watcher` keeps their entry
from Remote.run's events, marked "attached" so the Tracker leaves them alone; they end as past jobs (shown, failed,
cancelled), and `close_stale` ends the entries of a window closed while it waited.
"""
from __future__ import annotations

import json
import os
import pickle
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

POLL_S = 5.0                    # how often unfinished jobs are asked about
FINAL = ("ready", "shown", "failed", "cancelled", "lost")
PAST = ("shown", "failed", "cancelled", "lost")       # the Server jobs tab's Past jobs (kept until removed)
# job.json "state": queued | running (on the server) | ready (result here) | shown (its results were shown: a past job,
# its input and result files deleted) | failed (with `detail`: the error in full) | cancelled | lost (the server no
# longer knows it: expired, or removed)


def jobs_dir() -> Path:
    """This user's jobs folder (next to ui.json)."""
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "GroundTruth" if base else Path.home() / ".groundtruth") / "remote_jobs"


class LocalJobs:
    """The jobs folder: one sub-folder per job (see the module). Thread-safe (a lock around every file change)."""

    def __init__(self, folder: Optional[Path] = None):
        """The jobs in `folder` (default jobs_dir())."""
        self.folder = Path(folder) if folder else jobs_dir()
        self.lock = threading.RLock()

    def _dir(self, jid: str) -> Path:
        """A job's folder."""
        return self.folder / jid

    def add(self, meta: dict, inputs) -> dict:
        """Keep a job just handed over: its meta (id, server, kind, board_path, board_name, label, session) and its
        inputs (pickled for showing it later). Returns the meta as stored."""
        meta = {"state": "queued", "fraction": 0.0, "message": "", "error": "", "submitted": time.time(),
                "unreachable_since": None, **meta}
        d = self._dir(meta["id"])
        with self.lock:
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "input.pickle", "wb") as f:
                pickle.dump(inputs, f, protocol=pickle.HIGHEST_PROTOCOL)
            self._write(meta)
        return meta

    def _write(self, meta: dict):
        """(under the lock) job.json, atomically."""
        d = self._dir(meta["id"])
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        tmp.replace(d / "job.json")

    def update(self, jid: str, **fields) -> Optional[dict]:
        """Change a job's meta; None when the job is gone."""
        with self.lock:
            meta = self.get(jid)
            if meta is None:
                return None
            meta.update(fields)
            self._write(meta)
            return meta

    def get(self, jid: str) -> Optional[dict]:
        """A job's meta, or None."""
        try:
            return json.loads((self._dir(jid) / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def all(self) -> list[dict]:
        """Every job, newest first."""
        if not self.folder.exists():
            return []
        out = [m for m in (self.get(d.name) for d in self.folder.iterdir() if d.is_dir()) if m]
        return sorted(out, key=lambda m: -m.get("submitted", 0))

    def store_result(self, jid: str, data: bytes):
        """Keep a fetched result (its signed-checked bytes)."""
        with self.lock:
            tmp = self._dir(jid) / "result.bin.tmp"
            tmp.write_bytes(data)
            tmp.replace(self._dir(jid) / "result.bin")

    def load(self, jid: str) -> tuple:
        """(inputs, result) of a job whose result is here: the inputs are this plugin's own file, the result is read
        with the restricted unpickler (protocol.loads), as when it came from the server."""
        from .protocol import SafeUnpickler, loads
        d = self._dir(jid)
        with open(d / "input.pickle", "rb") as f:
            inputs = SafeUnpickler(f).load()
        return inputs, loads((d / "result.bin").read_bytes())

    def finish(self, jid: str, state: str = "shown", **fields) -> Optional[dict]:
        """Make a job a past job: its state, and its input and result files deleted (the large part); its job.json
        stays for the Past jobs list until removed."""
        with self.lock:
            for name in ("input.pickle", "result.bin"):
                try:
                    (self._dir(jid) / name).unlink()
                except OSError:
                    pass
            return self.update(jid, state=state, ended=time.time(), **fields)

    def remove(self, jid: str):
        """Forget a job here (its folder goes)."""
        with self.lock:
            shutil.rmtree(self._dir(jid), ignore_errors=True)


def state_fields(info: dict) -> dict:
    """A job's entry fields from the server's job info (GET /v1/jobs/ID): state, progress, times, error, memory,
    the computer it runs on."""
    fields = {"unreachable_since": None, "state": info["state"], "fraction": info.get("fraction", 0.0),
              "message": info.get("message", ""), "position": info.get("position"),
              "error": info.get("error", ""), "detail": info.get("detail", ""), "note": info.get("note", ""),
              "queued_s": info.get("queued_s"), "run_s": info.get("run_s"),
              "started_at": info.get("started_at"), "polled": time.time(),
              # the job's memory on the server and what the server has free (client.memory_text)
              **{k: info.get(k) for k in ("mem_mb", "commit_mb", "peak_mb", "server_free_mb",
                                          "server_total_mb", "server_commit_free_mb")}}
    if info.get("host"):
        fields["server_name"] = info["host"]        # the computer it runs on (several big machines)
    return fields


WATCH_S = 2.0                   # a waited-for job's progress reaches the list at most this often


def watcher(jobs: LocalJobs, base: dict, on_change: Callable[[dict], None]) -> Callable[[str, str, dict], None]:
    """The `watch` for client.Remote.run: a job the window waits for (Thermal, Current capacity, Check in Simulate,
    Compare what-if on the server) listed on the Server jobs tab like the handed-over ones (the user: a Thermal on
    the server was not shown there). `base`: its entry (server, kind, label, board_name, board_path, session); it is
    marked "attached", so the Tracker leaves it to this watcher, and it ends as a past job: shown (its results went
    straight to the window), failed or cancelled. `on_change(meta)` is told at most every WATCH_S, and at once when
    the state changes."""
    last = {"t": 0.0, "state": None}

    def watch(event: str, jid: str, info: dict):
        """Keep the job's entry (see watcher)."""
        if event == "submitted":
            meta = jobs.add({**base, "id": jid, "attached": True, "state": "queued", "position": info.get("position"),
                             "server_name": info.get("host") or base.get("server", "")}, None)
        elif event == "state":
            meta = jobs.update(jid, **state_fields(info))
        else:
            state = info.get("state", "cancelled")
            fields = {"fraction": 1.0, "message": "results shown"} if state == "done" else \
                {"error": info.get("error") or state, "detail": info.get("detail") or info.get("error") or state}
            meta = jobs.finish(jid, {"done": "shown"}.get(state, state), **fields)
        if meta is None:
            return
        now = time.monotonic()
        if event != "state" or meta.get("state") != last["state"] or now - last["t"] >= WATCH_S:
            last["t"], last["state"] = now, meta.get("state")
            on_change(meta)
    return watch


def close_stale(jobs: LocalJobs) -> int:
    """Waited-for jobs still listed as running from a window that has closed since (its Cancel stopped them on the
    server, or the plugin ended): made past jobs, cancelled. How many."""
    n = 0
    for meta in jobs.all():
        if meta.get("attached") and meta.get("state") not in FINAL:
            jobs.finish(meta["id"], "cancelled", error="the window waiting for it was closed",
                        detail="the window waiting for it was closed before it ended")
            n += 1
    return n


class Tracker:
    """Follows the unfinished jobs of LocalJobs in a background thread (see the module). `remote(address)` gives a
    remote.client.Remote for a job's server (with the current access key), `on_change(meta)` is called (from the
    thread) whenever a job's state changes."""

    def __init__(self, jobs: LocalJobs, remote: Callable[[str], object], on_change: Callable[[dict], None]):
        """Not started yet: start()."""
        self.jobs, self.remote, self.on_change = jobs, remote, on_change
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start following (a daemon thread)."""
        self._thread = threading.Thread(target=self._loop, name="gt-remote-jobs", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop following (the window closes; the jobs stay on the server and here for the next start)."""
        self._stop.set()
        self._wake.set()

    def poke(self):
        """Ask the server now (a job was just handed over)."""
        self._wake.set()

    def _loop(self):
        """Every POLL_S (or when poked): check every unfinished job once."""
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:                       # noqa: BLE001 - the tracker must never die; next round tries again
                pass
            self._wake.wait(POLL_S)
            self._wake.clear()

    def check(self):
        """One round: each unfinished job's state from its server; a finished one's result fetched, stored here and
        removed from the server."""
        from .client import RemoteError, ServerUnavailable
        for meta in self.jobs.all():
            if self._stop.is_set():
                return
            if meta["state"] in FINAL:
                continue
            if meta.get("attached"):
                continue                            # a window waits for it and keeps its entry (watcher)
            jid = meta["id"]
            try:
                server = self.remote(meta["server"])
                info = server.state(jid)
                fields = state_fields(info)
                if info["state"] in ("failed", "cancelled"):
                    fields["ended"] = info.get("finished_at") or time.time()
                if info["state"] == "done":
                    self.jobs.store_result(jid, server.fetch(jid, raw=True))
                    fields.update(state="ready", fraction=1.0, message="result here",
                                  ended=info.get("finished_at") or time.time())
                    try:
                        server.forget(jid)          # removed from the server once fetched
                    except (ServerUnavailable, OSError):
                        pass
            except ServerUnavailable as e:
                fields = {"unreachable_since": meta.get("unreachable_since") or time.time(), "error": str(e)}
            except RemoteError as e:                # the server no longer knows it (expired, removed)
                fields = {"state": "lost", "error": str(e), "detail": str(e), "ended": time.time()}
            new = self.jobs.update(jid, **fields)
            if new is not None and any(meta.get(k) != new.get(k) for k in ("state", "fraction", "message",
                                                                            "unreachable_since", "position",
                                                                            "mem_mb", "server_free_mb")):
                self.on_change(new)
