"""`groundtruth serve`: the compute server, on a powerful PC on the local network.

The plugin (remote/client.py) sends a job (Simulate, Suggest fixes, Thermal, Current capacity) over HTTP; the server
queues it, runs it in a process of its own (remote/worker.py, through thermal_proc._run_child: progress lines, Cancel
ends the process, a memory watchdog) and keeps the result until the plugin fetches it. Several people share one
server: jobs run first come, first served, `workers` at a time (default 1: one job gets all the cores and the GPU;
with several, each slot gets its own GPU while there are enough).

The user (2026-10-14): a job has an ID, its result waits **at least 24 hours** for its owner (whose laptop may be
asleep or gone home for the night) and is **removed once fetched**; only the owner cancels a job, never a lost
connection; a finished job whose result nobody fetched yet does not hold a slot. So every job lives on disk in the
data folder (`<id>.json` its state, `<id>.in` its input until it has run, `<id>.out` its result): a restart of the
server (a reboot overnight) queues the unfinished jobs again and keeps the results.

Every request and answer is signed with the access key (protocol.py). Endpoints (JSON unless noted):

    GET    /v1/status                 the server: code version, workers, the queue, GPUs, cores, memory
    GET    /v1/jobs[?client=C]        every job (of client C) with its state
    POST   /v1/jobs?kind=K&client=C&code=ID&numpy=N   body: protocol.dumps(args) -> {"id", "position"}
    GET    /v1/jobs/ID                its state: queued (position) | running (fraction, message) | done | failed
                                      (error) | cancelled
    GET    /v1/jobs/ID/result         the result, protocol.dumps-compatible bytes (zlib of the worker's pickle)
    DELETE /v1/jobs/ID                cancel it (queued or running), or forget it (finished: its result goes)
    POST   /v1/update?sig=HEX         body: a signed server package (remote/release.py): taken when remote updates
                                      are on, the signature is the release key's and the build is newer; the server
                                      then starts no new job, and once none runs it exits for its runner to install it

A job from another code version (protocol.code_id) or another NumPy major version is refused: the result's
classes and pickles must match the plugin's.

The server's settings (port, jobs at a time, solver threads, GPU, how long results are kept) are read at every start
from `server.json` next to the key (made with the defaults on the first start); a command-line option overrides its
entry for that run (settings()).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import protocol, release
from .protocol import ProtocolError

MAX_BODY = 2 * 2 ** 30          # a job's body (a board and its settings: kB to a few MB; large what-if boards more)
KEEP_HOURS = 24.0               # a finished job's result is kept this long when its owner does not fetch it
MEMORY_SHARE = 0.85             # of the server's memory, shared by the jobs running at once (the rest: the system)
DEFAULTS = {"port": protocol.DEFAULT_PORT, "workers": 1, "threads": "-", "gpu": "auto", "keep_hours": KEEP_HOURS,
            # the cluster (remote/cluster.py): its UDP port (0: no discovery), this server as the lead, peers on
            # another subnet (host or host:beacon port)
            "beacon_port": 8766, "lead": False, "peers": [],
            # signed remote updates from the users' plugins (remote/release.py): off unless the install turned them on
            "remote_update": False,
            # the address this server announces to the cluster and the plugins ("": all its real ones); on a PC with
            # several network cards, the one the others reach it at (install --address)
            "address": ""}
log = logging.getLogger("groundtruth.server")
_BUILD: list = []


def server_build() -> str:
    """This server's build text (groundtruth.build_version(): "GroundTruth 0.1.41452 (57602a8, built ...)"), read
    once: every job answer carries it, and a remote update restarts the server with the new one."""
    if not _BUILD:
        from .. import build_version
        _BUILD.append(build_version())
    return _BUILD[0]


class _HTTPServer(ThreadingHTTPServer):
    """The HTTP server, owning its port alone. Python's HTTPServer sets SO_REUSEADDR, which on Windows lets a second
    process bind the same port: a second server started by mistake said READY and took some of the requests, and
    any local program could take the jobs (and their boards). On Windows SO_EXCLUSIVEADDRUSE instead: a port in use
    fails the start ("Only one usage of each socket address")."""
    if sys.platform == "win32":
        allow_reuse_address = False

        def server_bind(self):
            """Bind with SO_EXCLUSIVEADDRUSE."""
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


# The GPU check, in a process of its own (see ComputeServer._probe_gpus): one JSON line, the GPUs or why there are none
GPU_PROBE = """
import json
try:
    import torch
except Exception as e:
    print(json.dumps({"gpus": [], "why": "PyTorch does not load (not installed: install_server.cmd install --cuda "
                      "cu126; or a DLL missing: the Microsoft Visual C++ Redistributable): %s: %s"
                      % (type(e).__name__, str(e)[:300])}))
    raise SystemExit
if not torch.cuda.is_available():
    print(json.dumps({"gpus": [], "why": "PyTorch %s finds no CUDA GPU (%s)" % (torch.__version__,
          "a CPU-only PyTorch: install_server.cmd install --cuda cu126" if "+cpu" in torch.__version__
          or torch.version.cuda is None else "no NVIDIA GPU, or its driver is too old for CUDA %s"
          % torch.version.cuda)}))
else:
    names = [torch.cuda.get_device_name(k) for k in range(torch.cuda.device_count())]
    # CUDA can load and still show no device (CUDA_VISIBLE_DEVICES set empty: PyTorch 2.7 on Windows): say so
    print(json.dumps({"gpus": names, "why": "" if names else "PyTorch %s loads CUDA but sees no device "
                      "(CUDA_VISIBLE_DEVICES=%r)" % (torch.__version__, __import__("os").environ.get(
                          "CUDA_VISIBLE_DEVICES"))}))
"""


# a Mac's server: no CUDA on macOS, and an Apple GPU (Metal) has no float64, which the solver needs
MAC_GPU_NOTE = "a Mac (no CUDA on macOS, and an Apple GPU has no double precision for the solver)"


def default_data_dir() -> Path:
    """Where the server keeps its jobs: server_jobs next to the access key."""
    return protocol.default_key_file().parent / "server_jobs"


def settings_file() -> Path:
    """The server's settings file, next to the access key."""
    return protocol.default_key_file().parent / "server.json"


def settings(path: Optional[Path] = None, overrides: Optional[dict] = None) -> dict:
    """The server's settings: DEFAULTS, then the settings file's entries (the file is made with the defaults when
    it is missing, so the admin finds it), then `overrides` (command-line options given, None ones ignored)."""
    path = path or settings_file()
    out = dict(DEFAULTS)
    try:
        out.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k in DEFAULTS})
    except FileNotFoundError:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    except (OSError, ValueError) as e:
        log.warning("%s: %s; using the defaults", path, e)
    out.update({k: v for k, v in (overrides or {}).items() if v is not None})
    out["port"], out["workers"] = int(out["port"]), max(1, int(out["workers"]))
    out["keep_hours"], out["threads"], out["gpu"] = float(out["keep_hours"]), str(out["threads"]), str(out["gpu"])
    lead = out["lead"]
    out["beacon_port"] = int(out["beacon_port"])
    out["lead"] = lead.strip().lower() in ("1", "true", "yes") if isinstance(lead, str) else bool(lead)
    peers = out["peers"]
    out["peers"] = [x.strip() for x in (peers.split(",") if isinstance(peers, str) else peers or []) if str(x).strip()]
    return out


@dataclass
class Job:
    """One job: its state in memory and in `<id>.json`; its input and result are files (Store)."""
    id: str
    kind: str
    client: str
    queued: float = field(default_factory=time.time)
    state: str = "queued"               # queued | running | done | failed | cancelled
    fraction: float = 0.0
    message: str = ""
    started: Optional[float] = None
    finished: Optional[float] = None
    error: str = ""
    detail: str = ""                    # a failure in full: the message, the worker's own output (its traceback)
    peak_mb: float = 0.0
    result_bytes: int = 0
    parent: str = ""                    # a part of this server's split job `parent` (remote/split.py)
    whole: bool = False                 # a Simulate that runs whole here (not split: one item, no other server)
    split: bool = False                 # running as a split job's coordinator: it holds no worker slot (its parts
                                        # do), so it is not counted as a job ahead in the queue (_busy)
    note: str = ""                      # why a Simulate on a cluster was not split (shown in the plugin's details)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    live: dict = field(default_factory=dict, repr=False, compare=False)   # memory now (thermal_proc._Watchdog)

    def meta(self) -> dict:
        """What `<id>.json` keeps (every field but the cancel event; asdict would deep-copy its lock and fail)."""
        return {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in ("cancel", "live")}

    def info(self, position: Optional[int]) -> dict:
        """Its state for GET /v1/jobs/ID."""
        now = time.time()
        return {"id": self.id, "kind": self.kind, "client": self.client, "state": self.state,
                "position": position, "fraction": self.fraction, "message": self.message, "error": self.error,
                "detail": self.detail, "note": self.note, "started_at": self.started, "host": socket.gethostname(),
                # the build it runs on, for the plugin's Server jobs tab (which version made a result)
                "version": server_build(),
                "queued_at": self.queued, "finished_at": self.finished,
                "queued_s": round((self.started or now) - self.queued, 1),
                "run_s": round((self.finished or now) - self.started, 1) if self.started else 0.0,
                "peak_mb": round(max(self.peak_mb, self.live.get("peak_mb", 0.0))), "result_bytes": self.result_bytes,
                # the job's memory now (running): in use, and reserved (Windows commit), MB
                "mem_mb": round(self.live["rss_mb"]) if self.state == "running" and "rss_mb" in self.live else None,
                "commit_mb": (round(self.live["commit_mb"]) if self.state == "running"
                              and self.live.get("commit_mb") is not None else None),
                **server_memory()}


class ComputeServer:
    """The queue, its worker slots and the HTTP endpoints (see the module). `start()` serves in background
    threads (tests, the CLI's serve waits)."""

    def __init__(self, key: str, host: str = "0.0.0.0", port: int = protocol.DEFAULT_PORT, workers: int = 1,
                 threads: str = "-", gpu: str = "-", keep_s: float = KEEP_HOURS * 3600.0,
                 data_dir: Optional[Path] = None, beacon_port: int = 0, lead: bool = False, peers: tuple = (),
                 remote_update: bool = False, home: Optional[Path] = None, address: str = ""):
        """`threads` / `gpu`: the server's solver threads and GPU choice for every job ("-": each job's own, i.e.
        the plugin's Preferences); `data_dir`: the jobs' folder (default_data_dir); `remote_update`: take signed
        updates (remote/release.py) into `home` (the install folder: its release_key.pub, updates/)."""
        self.key, self.host, self.port = key, host, port
        self.workers, self.threads, self.gpu, self.keep_s = max(1, int(workers)), str(threads), str(gpu), keep_s
        self.data = Path(data_dir) if data_dir else default_data_dir()
        self.data.mkdir(parents=True, exist_ok=True)
        self.code = protocol.code_id()
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []                  # queued job ids, first come first
        self.lock = threading.Condition()
        self.nonces = protocol.NonceCache()
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.gpus: list[str] = []
        self.gpu_count = 0                          # usable GPUs, once the probe found them (slots share them out)
        self.gpu_note = ""                          # why none were found (the probe's reason)
        self.started = time.time()
        # the cluster: off (0) unless `groundtruth serve` turns it on (tests run many servers on one machine)
        self.beacon_port, self.forced_lead, self.static_peers = int(beacon_port), bool(lead), tuple(peers)
        self.cluster = None
        self.remote_update = bool(remote_update)
        self.address = str(address or "").strip()  # the address announced ("": every real one, local_addresses)
        self.home = Path(home) if home else protocol.default_key_file().parent
        self.update: Optional[dict] = None          # an update taken, waiting for the running jobs (then installing)
        self.exit_code: Optional[int] = None        # set when the server is to end (serve() returns it)
        self._restore()

    # ------------------------------------------------------------------ the jobs on disk
    def _path(self, jid: str, ext: str) -> Path:
        """A job's file: .json (state), .in (input), .out (result)."""
        return self.data / f"{jid}{ext}"

    def _save(self, job: Job):
        """Write a job's state (atomically: a .tmp renamed)."""
        tmp = self._path(job.id, ".json.tmp")
        tmp.write_text(json.dumps(job.meta()), encoding="utf-8")
        tmp.replace(self._path(job.id, ".json"))

    def _drop(self, jid: str):
        """Delete a job's files."""
        for ext in (".json", ".in", ".out", ".json.tmp"):
            try:
                self._path(jid, ext).unlink()
            except OSError:
                pass

    def _restore(self):
        """The jobs of the last run, after a restart: unfinished ones queued again (a running one starts over),
        finished ones kept until they expire; anything unreadable dropped."""
        for f in sorted(self.data.glob("*.json")):
            jid = f.stem
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                job = Job(**{k: v for k, v in d.items() if k in Job.__dataclass_fields__ and k not in ("cancel",
                                                                                                        "live")})
            except (OSError, ValueError, TypeError):
                self._drop(jid)
                continue
            if job.parent and job.state in ("queued", "running"):
                # a part of a split job: the split starts over after a restart (its parts are made anew)
                self._drop(jid)
                continue
            if job.state in ("queued", "running"):
                if not self._path(jid, ".in").exists():
                    self._drop(jid)
                    continue
                job.state, job.started, job.fraction, job.message = "queued", None, 0.0, "queued again after a restart"
                self._save(job)
            elif job.state == "done" and not self._path(jid, ".out").exists():
                self._drop(jid)
                continue
            self.jobs[jid] = job
        self.order = sorted((j.id for j in self.jobs.values() if j.state == "queued"),
                            key=lambda jid: self.jobs[jid].queued)
        with self.lock:
            self._expire()
        if self.jobs:
            log.info("restored %d job(s): %d queued, %d finished", len(self.jobs), len(self.order),
                     len(self.jobs) - len(self.order))

    # ------------------------------------------------------------------ queue
    def submit(self, kind: str, client: str, payload: bytes, front: bool = False, parent: str = "",
               whole: bool = False) -> tuple[str, int]:
        """Queue a job (its input to disk first): (its id, how many jobs are ahead of it, running ones included).
        `front`: first in the queue (a split job's own part: the job already had its turn); `parent`: the split
        job it is a part of; `whole`: a Simulate not to be split."""
        job = Job(secrets.token_hex(8), kind, client, parent=parent, whole=whole)
        self._path(job.id, ".in").write_bytes(payload)
        self._save(job)
        with self.lock:
            self.jobs[job.id] = job
            if front:
                self.order.insert(0, job.id)
            else:
                self.order.append(job.id)
            ahead = self._busy() + self.order.index(job.id)
            self.lock.notify_all()
        log.info("%s queued %s from %s (%d kB, %d ahead)", job.id, kind, client, len(payload) // 1024, ahead)
        return job.id, ahead

    def _busy(self) -> int:
        """(under the lock) Jobs running in a worker slot: a split job's coordinator is not one (its slot was free
        again at once; its parts are jobs of their own). Counting it showed "2 job(s) ahead" with one running."""
        return sum(1 for j in self.jobs.values() if j.state == "running" and not j.split)

    def position(self, job: Job) -> Optional[int]:
        """Jobs ahead of a queued one (those running in a slot and those queued before it); None once it runs."""
        if job.state != "queued":
            return None
        running = self._busy()
        return running + self.order.index(job.id) if job.id in self.order else running

    def cancel(self, job_id: str) -> Optional[Job]:
        """Cancel a queued or running job, or forget a finished one (its files go). None if unknown."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job.state == "queued":
                job.state, job.finished = "cancelled", time.time()
                self.order.remove(job.id)
                self._save(job)
                try:
                    self._path(job.id, ".in").unlink()
                except OSError:
                    pass
                log.info("%s cancelled while queued", job.id)
            elif job.state == "running":
                job.cancel.set()                    # the slot kills the process and marks it cancelled
                log.info("%s cancelled by its owner", job.id)
            else:
                self.jobs.pop(job_id, None)
                self._drop(job_id)
                log.info("%s fetched or forgotten: removed", job.id)
        return job

    def _slot(self, k: int):
        """A worker slot: run the next queued job, one at a time, until the server stops."""
        while not self._stop.is_set():
            with self.lock:
                # an update waiting (`self.update`): the queued jobs stay queued (on disk: they run after it)
                while (not self.order or self.update is not None) and not self._stop.is_set():
                    self.lock.wait(timeout=1.0)
                    self._expire()
                if self._stop.is_set():
                    return
                job = self.jobs[self.order.pop(0)]
                job.state, job.started, job.message = "running", time.time(), ""
                job.split = False                   # (a split that put it back to run whole set it before)
                self._save(job)
            # asked outside the lock (the cluster's own lock); a coordinator holds no slot: not a job ahead
            job.split = self._may_split(job)
            if job.split:
                # split over the cluster by a coordinator thread of its own: this slot is free again at once (the
                # parts are ordinary jobs in the servers' queues, this server's own part first in its queue)
                from .split import SplitRun
                threading.Thread(target=SplitRun(self, job).run, name=f"gt-split-{job.id}", daemon=True).start()
                continue
            gpu = self._gpu_for(k)
            log.info("%s started %s from %s (slot %d%s)", job.id, job.kind, job.client, k,
                     f", GPU {gpu}" if gpu is not None else "")
            self._run(job, gpu)
            self._update_when_idle()
            if job.state == "queued":
                log.info("%s interrupted by the server stopping: queued again for the next start", job.id)
            else:
                log.info("%s %s after %.0f s%s", job.id, job.state, job.finished - job.started,
                         f": {job.error}" if job.error else "")

    def _may_split(self, job: Job) -> bool:
        """Whether a job is to be split over the cluster: a Simulate (not a part, not marked whole) on a server
        that hears other servers of its cluster (SplitRun decides on: two items or more, a peer answering)."""
        return (job.kind == "simulate" and not job.whole and self.cluster is not None
                and bool(self.cluster.alive()))

    def _gpu_for(self, k: int) -> Optional[int]:
        """The GPU a slot's jobs use: with several GPUs, slot k gets GPU k mod their number (two slots on two
        GPUs: one each); None: whatever PyTorch picks (one GPU or none, or --gpu off)."""
        if self.gpu == "off" or self.gpu_count < 2:
            return None
        return k % self.gpu_count

    def _run(self, job: Job, gpu: Optional[int] = None):
        """Run a job's worker process; its result to `<id>.out`, or its error. A server stopping mid-job (Ctrl+C,
        the window closed) leaves it queued, to start over at the next start."""
        from ..analysis.thermal_proc import ThermalCancelled, ThermalMemoryError, _run_child

        def progress(msg: str, frac: float):
            """The worker's progress line."""
            job.message, job.fraction = msg, frac
        env = {"CUDA_VISIBLE_DEVICES": str(gpu)} if gpu is not None else None
        try:
            payload = self._path(job.id, ".in").read_bytes()
            raw, job.peak_mb = _run_child(payload, progress, job.cancel, 0.2, self._limits(),
                                          module="groundtruth.remote.worker",
                                          args=(job.kind, self.threads, self.gpu), env_extra=env, raw=True,
                                          stats=job.live, what="the job")
            data = zlib.compress(raw, 1)
            self._path(job.id, ".out").write_bytes(data)
            job.result_bytes = len(data)
            job.state = "done"
        except ThermalCancelled:
            job.state = "queued" if self._stop.is_set() else "cancelled"
        except ThermalMemoryError as e:
            # stopped by the watchdog, or the worker's own MemoryError (a Simulate works around a shortage itself:
            # peec.GATE, simulate.MemoryRelief; what still runs out is elsewhere): its traceback is kept with the job
            # (it used to be dropped, and the message named "the thermal solve" whatever the job was)
            job.state = "failed"
            job.error = (f"{str(e).rstrip('.')}: the server had too little memory free for it (fewer jobs at once, "
                         "or a lower resolution)")
            job.detail = job.error + (f"\n\n{e.detail}" if e.detail else "")
            log.warning("%s failed:\n%s", job.id, job.detail)
        except Exception as e:                      # noqa: BLE001 - any failure is the job's, reported to its owner
            # the first line for the lists; everything (the worker's stderr: its traceback) kept with the job (the
            # user: "if there is an error. it should be saved somewhere")
            text = str(e).replace("the thermal solve", "the job")
            job.state, job.error, job.detail = "failed", text.split("\n\n")[0], f"{type(e).__name__}: {text}"
            log.warning("%s failed:\n%s", job.id, job.detail)
        finally:
            job.finished = time.time() if job.state != "queued" else None
            if job.state != "queued":
                try:
                    self._path(job.id, ".in").unlink()      # the board is no longer needed
                except OSError:
                    pass
            self._save(job)

    def _limits(self) -> Optional[tuple[float, float]]:
        """(a job's memory limit, memory to keep free) in MB: MEMORY_SHARE of the server's memory split over the
        slots; None without psutil."""
        try:
            import psutil
            total = psutil.virtual_memory().total / 2 ** 20
        except Exception:                           # noqa: BLE001
            return None
        return total * MEMORY_SHARE / self.workers, max(512.0, total * 0.05)

    def _expire(self):
        """(under the lock) Remove finished jobs whose result waited longer than keep_s."""
        now = time.time()
        for j in [j for j in self.jobs.values() if j.finished and now - j.finished > self.keep_s]:
            self.jobs.pop(j.id, None)
            self._drop(j.id)
            log.info("%s expired: its result was not fetched within %.0f h", j.id, self.keep_s / 3600)

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        """What GET /v1/status answers."""
        with self.lock:
            queue = [self.jobs[j].info(self.position(self.jobs[j])) for j in self.order]
            running = [j.info(None) for j in self.jobs.values() if j.state == "running"]
            waiting = sum(1 for j in self.jobs.values() if j.state == "done")
        try:
            import psutil
            mem = round(psutil.virtual_memory().total / 2 ** 30, 1)
            free = round(psutil.virtual_memory().available / 2 ** 30, 1)
        except Exception:                           # noqa: BLE001
            mem = free = None
        import numpy
        from .. import build_version
        return {"server": "groundtruth", "protocol": protocol.PROTOCOL, "code": self.code,
                "version": build_version(),
                "numpy": numpy.__version__, "python": sys.version.split()[0], "host": socket.gethostname(),
                # its addresses (the user: "it would be good if status shows the servers IP"), the announced first
                "addresses": self.addresses(), "address": self.address,
                "workers": self.workers, "threads": self.threads, "gpu": self.gpu, "gpus": self.gpus,
                "gpu_note": self.gpu_note,
                "cores": os.cpu_count(), "memory_gb": mem, "free_gb": free, "running": running, "queue": queue,
                "results_waiting": waiting, "keep_hours": self.keep_s / 3600,
                "uptime_s": round(time.time() - self.started),
                # signed remote updates: whether this server takes them, one waiting, how the last one went
                "remote_update": self.remote_update and self._release_key() is not None,
                "update": ({k: v for k, v in self.update.items() if k != "zip"} if self.update
                           else self._last_update()),
                "cluster": self.cluster.status() if self.cluster is not None else {"role": "standalone",
                                                                                    "discovery": False}}

    def jobs_of(self, client: Optional[str]) -> list[dict]:
        """GET /v1/jobs: every job (of `client`), oldest first."""
        with self.lock:
            return [j.info(self.position(j)) for j in sorted(self.jobs.values(), key=lambda j: j.queued)
                    if client is None or j.client == client]

    def _probe_gpus(self):
        """The GPUs PyTorch sees on this machine, for the status and the slots (best effort, in the background).
        In a process of its own: importing torch loads gigabytes of CUDA libraries while holding Python's GIL (over
        a minute on the user's server the first time, Defender scanning them), which froze the whole server: a job
        was queued but the plugin's progress requests timed out. With --gpu off nothing is probed."""
        if self.gpu == "off":
            self.gpus = ["not used (--gpu off)"]
            return
        if sys.platform == "darwin":
            # nothing to probe on a Mac, and the probe's reasons (install --cuda, a missing DLL) are Windows advice
            # that read like a broken install (the user's M2: "dlls missing on mac?")
            self.gpus, self.gpu_count, self.gpu_note = [], 0, MAC_GPU_NOTE
            log.info("GPUs: none used on a Mac, the solves run on the CPU")
            return
        import subprocess
        from ..analysis.thermal_proc import CREATE_NO_WINDOW, _python
        self.gpus = ["(still being detected)"]
        try:
            out = subprocess.run([_python(), "-c", GPU_PROBE], capture_output=True, text=True, timeout=600,
                                 creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            got = json.loads(out.stdout.strip().splitlines()[-1])
            found, why = list(got.get("gpus") or []), str(got.get("why") or "")
        except Exception as e:                      # noqa: BLE001 - the probe itself failed: the CPU solves
            found, why = [], f"the GPU check failed ({type(e).__name__}: {e})"
        # the user's laptop service said "no GPU found" with nothing to go on: the reason goes with it
        self.gpus, self.gpu_count, self.gpu_note = found, len(found), why
        log.info("GPUs: %s", ", ".join(found) or f"none usable, the solves run on the CPU: {why}")
        if self.gpu_count > 1 and self.workers > 1:
            log.info("%d jobs at a time on %d GPUs: each slot uses its own GPU", self.workers, self.gpu_count)

    # ------------------------------------------------------------------ HTTP
    def start(self):
        """Listen and start the worker slots (background threads)."""
        server = self

        class Handler(BaseHTTPRequestHandler):
            """One request (ThreadingHTTPServer: a thread each)."""
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                """Requests are not logged one by one (the jobs are, by the queue)."""

            def _send(self, status: int, body: bytes, nonce: Optional[str], ctype: str = "application/json"):
                """Answer, signed for the request's nonce (an unsigned answer only to a refused request)."""
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if nonce is not None:
                    self.send_header("X-GT-Sig", protocol.sign_response(server.key, nonce, status, body))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, obj, nonce: Optional[str]):
                """A JSON answer."""
                self._send(status, json.dumps(obj).encode("utf-8"), nonce)

            def _handle(self, method: str):
                """Check the signature, then route."""
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    self._json(413, {"error": f"the job is larger than {MAX_BODY // 2 ** 20} MB"}, None)
                    return
                body = self.rfile.read(n) if n else b""
                try:
                    with server.lock:
                        nonce = protocol.check_request(server.key, method, self.path, self.headers, body,
                                                       server.nonces)
                except ProtocolError as e:
                    log.warning("refused %s %s from %s: %s", method, urlparse(self.path).path,
                                self.client_address[0], e)
                    self._json(403, {"error": str(e)}, None)
                    return
                try:
                    server._route(self, method, body, nonce)
                except Exception as e:              # noqa: BLE001 - a bad request must not stop the server
                    log.exception("request failed")
                    self._json(500, {"error": f"{type(e).__name__}: {e}"}, nonce)

            def do_GET(self):
                """GET."""
                self._handle("GET")

            def do_POST(self):
                """POST."""
                self._handle("POST")

            def do_DELETE(self):
                """DELETE."""
                self._handle("DELETE")

        self.httpd = _HTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._threads = [threading.Thread(target=self.httpd.serve_forever, name="gt-http", daemon=True)]
        self._threads += [threading.Thread(target=self._slot, args=(k,), name=f"gt-slot{k}", daemon=True)
                          for k in range(self.workers)]
        self._threads.append(threading.Thread(target=self._probe_gpus, name="gt-gpus", daemon=True))
        for t in self._threads:
            t.start()
        if self.beacon_port:
            self._start_cluster()
        log.info("GroundTruth compute server %s on %s:%d, %d job(s) at a time, results kept %.0f h, jobs in %s",
                 self.code, self.host, self.port, self.workers, self.keep_s / 3600, self.data)

    def _start_cluster(self):
        """Discovery and the lead election (remote/cluster.py); a UDP port that can't be opened leaves this server
        standalone, with a note in the log (the HTTP side works the same)."""
        from .cluster import Cluster

        def info() -> dict:
            """This server's figures for its beacons."""
            from .. import build_version
            mem = server_memory()
            return {"cores": os.cpu_count(), "memory_gb": round((mem.get("server_total_mb") or 0) / 1024, 1),
                    "free_gb": round((mem.get("server_free_mb") or 0) / 1024, 1),
                    "gpus": [g for g in self.gpus if not g.startswith("(")], "workers": self.workers,
                    "version": build_version(),
                    # its real network addresses: a datagram may leave from a virtual adapter (cluster.address_candidates)
                    "addresses": self.addresses(announced=True),
                    # its load, for the cluster's status (a glance without the lock: a count, not a decision)
                    "running": sum(1 for j in list(self.jobs.values()) if j.state == "running"),
                    "queued": len(self.order)}
        try:
            self.cluster = Cluster(self.key, self.port, self.code, info, forced_lead=self.forced_lead,
                                   static_peers=self.static_peers, beacon_port=self.beacon_port,
                                   address=self.address)
            self.cluster.start()
            log.info("cluster: announcing on UDP %d%s%s", self.beacon_port,
                     ", this server is the lead (--lead)" if self.forced_lead else "",
                     f", peers {', '.join(self.static_peers)}" if self.static_peers else "")
        except OSError as e:
            self.cluster = None
            log.warning("cluster: UDP %d can't be opened (%s): this server stays standalone", self.beacon_port, e)

    def stop(self):
        """Stop listening and end the slots; a running job is stopped and stays queued for the next start."""
        self._stop.set()
        with self.lock:
            for j in self.jobs.values():
                if j.state == "running":
                    j.cancel.set()
            self.lock.notify_all()
        if self.cluster is not None:
            self.cluster.stop()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        for t in self._threads:
            if t.name.startswith("gt-slot"):
                t.join(timeout=10)

    def _route(self, h, method: str, body: bytes, nonce: str):
        """Answer one checked request (see the module's endpoints)."""
        url = urlparse(h.path)
        parts = [p for p in url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if parts[:1] != ["v1"]:
            h._json(404, {"error": "unknown path"}, nonce)
            return
        parts = parts[1:]
        if method == "GET" and parts == ["status"]:
            h._json(200, self.status(), nonce)
            return
        if method == "GET" and parts == ["jobs"]:
            h._json(200, self.jobs_of(q.get("client")), nonce)
            return
        if method == "POST" and parts == ["jobs"]:
            from .worker import CLIENT_KINDS
            kind = q.get("kind", "")
            if kind not in CLIENT_KINDS or (kind == "sleep" and not os.environ.get("GROUNDTRUTH_SERVER_TEST")):
                h._json(400, {"error": f"unknown job kind {kind!r}"}, nonce)
                return
            if q.get("code") != self.code:
                from .. import build_version
                h._json(409, {"error": f"the server runs {build_version()} (code {self.code}), this plugin "
                                       f"{q.get('version', '?')} (code {q.get('code')}): install the same package on "
                                       "both"}, nonce)
                return
            import numpy
            if q.get("numpy", "").split(".")[0] != numpy.__version__.split(".")[0]:
                h._json(409, {"error": f"the server has NumPy {numpy.__version__}, this plugin {q.get('numpy')}: "
                                       "run the server with the plugin's Python environment"}, nonce)
                return
            lead = self.cluster.lead() if self.cluster is not None else None
            if kind == "simulate" and q.get("direct") != "1" and lead and not lead["me"]:
                # a member: the cluster's lead splits the Simulates; the plugin sends the job there (after checking
                # that the lead holds the key) or, should the lead not answer it, back here with direct=1
                h._json(307, {"redirect": f"{lead['address']}:{lead['port']}", "lead": lead["host"],
                              "host": socket.gethostname()}, nonce)
                return
            try:
                payload = zlib.decompress(body)
            except zlib.error:
                h._json(400, {"error": "the job's body is not a compressed pickle"}, nonce)
                return
            jid, ahead = self.submit(kind, q.get("client", "?")[:80], payload)
            h._json(201, {"id": jid, "position": ahead, "host": socket.gethostname(), "version": server_build()},
                    nonce)
            return
        if method == "POST" and parts == ["update"]:
            status, answer = self._take_update(body, q.get("sig", ""))
            h._json(status, answer, nonce)
            return
        if len(parts) >= 2 and parts[0] == "jobs":
            with self.lock:
                job = self.jobs.get(parts[1])
                info = job.info(self.position(job)) if job is not None else None
            if job is None:
                h._json(404, {"error": f"no such job: its result was fetched already, or it expired (results are kept "
                                       f"{self.keep_s / 3600:.0f} h)"}, nonce)
                return
            if method == "GET" and len(parts) == 2:
                h._json(200, info, nonce)
                return
            if method == "GET" and parts[2:] == ["result"]:
                out = self._path(job.id, ".out")
                if job.state == "done" and out.exists():
                    h._send(200, out.read_bytes(), nonce, "application/octet-stream")
                else:
                    h._json(409, {"error": job.error or f"the job is {job.state}", "state": job.state}, nonce)
                return
            if method == "DELETE" and len(parts) == 2:
                self.cancel(job.id)
                h._json(200, {"id": job.id, "state": job.state}, nonce)
                return
        h._json(404, {"error": "unknown request"}, nonce)

    # ------------------------------------------------------------------ signed remote updates (remote/release.py)
    def addresses(self, announced: bool = False) -> list[str]:
        """This server's IPv4 addresses on real networks, the chosen one (server.json "address") first; `announced`:
        what its beacons tell the others: the chosen one alone when there is one (another network card's address
        would be taken by peers on that card's subnet, and the others could not reach it there)."""
        own = [a for a in local_addresses()[1:] if a.count(".") == 3]
        if self.address:
            return [self.address] if announced else [self.address] + [a for a in own if a != self.address]
        return own

    def _release_key(self) -> Optional[str]:
        """The release key's public half this server trusts (release_key.pub in its install folder, set by its last
        manual install), None without one."""
        try:
            text = (self.home / release.PUBLIC_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text if len(text) == 64 else None

    def _last_update(self) -> Optional[dict]:
        """How the last remote update went (updates/last.json, written by the installer's apply-update or the
        runner's roll-back), None before any."""
        try:
            return json.loads((self.home / "updates" / "last.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _take_update(self, data: bytes, signature: str) -> tuple[int, dict]:
        """POST /v1/update: (HTTP status, answer). Taken only when remote updates are on, this server runs as the
        installed service (its runner installs the package), the package is signed with this server's release key
        and is a newer build than its own (remote/release.py); then kept in updates/ and installed once no job
        runs."""
        from .. import build_version
        if not self.remote_update:
            return 409, {"error": "remote updates are off on this server: turn them on once at the server "
                                  "(install_server install --remote-update on)"}
        if not os.environ.get("GROUNDTRUTH_SERVICE"):
            return 409, {"error": "this server runs in a window, not as the installed service: update it with the "
                                  "server package's installer"}
        public = self._release_key()
        if public is None:
            return 409, {"error": "this server has no release key (it was installed from a package without one): "
                                  "install a signed server package at the server once"}
        if len(data) > release.MAX_UPDATE:
            return 413, {"error": f"the package is larger than {release.MAX_UPDATE // 2 ** 20} MB"}
        if not release.verify(public, data, signature):
            log.warning("refused an update: not signed with this server's release key")
            return 403, {"error": "the package is not signed with this server's release key (set by its last manual "
                                  "install): install a plugin built with that key, or update the server by hand"}
        version = release.zip_version(data)
        new, old = release.version_number(version), release.version_number(build_version())
        if new is None:
            return 400, {"error": "the package has no version.txt"}
        if old is not None and new <= old:
            return 409, {"error": f"the server runs {build_version()}, the package is {version}: not newer"}
        with self.lock:
            if self.update is not None:
                return 409, {"error": f"an update to {self.update.get('version')} is already waiting"}
            folder = self.home / "updates"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "update-{}.{}.{}.zip".format(*new)
            path.write_bytes(data)
            # running jobs are cancelled, not waited for: an outdated server should not finish a long Simulate on old
            # code (the user: "when the server is outdated and a sim is running, cancel that sim and update
            # immediately"); their owners see why and run them again on the new version
            busy = [j for j in self.jobs.values() if j.state == "running"]
            for j in busy:
                j.message = f"cancelled: the server is updating to {version}; run it again"
            self.update = {"state": "waiting" if busy else "installing", "version": version,
                           "from": build_version(), "zip": str(path), "since": time.time(), "running": len(busy),
                           "cancelled": [j.id for j in busy]}
            self.lock.notify_all()
            answer = {k: v for k, v in self.update.items() if k != "zip"}
        log.info("update to %s taken; %s", version, f"cancelling {len(busy)} running job(s) first" if busy else
                 "installing it now")
        for j in busy:
            self.cancel(j.id)                   # the slot stops it, then installs the update (_update_when_idle)
        self._update_when_idle()
        return 202, answer

    def _update_when_idle(self):
        """Once an update is waiting and no job runs: hand it to the service's runner (updates/pending.json) and end
        the server with release.UPDATE_EXIT (serve() returns it; the runner installs the package and starts the new
        server; the queued jobs stay on disk and run after it)."""
        with self.lock:
            if self.update is None or self.exit_code is not None:
                return
            if any(j.state == "running" for j in self.jobs.values()):
                return
            self.update["state"] = "installing"
            pending = self.home / "updates" / "pending.json"
            pending.write_text(json.dumps({"zip": self.update["zip"], "version": self.update["version"],
                                           "from": self.update["from"]}), encoding="utf-8")
            self.exit_code = release.UPDATE_EXIT
        log.info("no job running: ending for the update to %s", self.update["version"])


def server_memory() -> dict:
    """The server's memory now, MB: physical free and total, and the Windows commit still free (the figure that runs
    out first with a small page file; None elsewhere). Empty without psutil."""
    try:
        import psutil
        vm = psutil.virtual_memory()
    except Exception:                               # noqa: BLE001
        return {}
    from ..core.sysmem import commit_available
    commit = commit_available()
    return {"server_free_mb": round(vm.available / 2 ** 20), "server_total_mb": round(vm.total / 2 ** 20),
            "server_commit_free_mb": round(commit / 2 ** 20) if commit is not None else None}


VIRTUAL_ADAPTERS = ("vethernet", "hyper-v", "wsl", "virtualbox", "vmware", "loopback", "bluetooth", "npcap",
                    "docker", "tailscale", "zerotier",
                    # macOS: VPN tunnels, AirDrop / Wi-Fi Direct, low-latency WLAN, Thunderbolt bridge, Apple's
                    # internal interfaces, IPv6 tunnels
                    "utun", "awdl", "llw", "bridge", "anpi", "gif", "stf", "ap1")


def local_addresses() -> list[str]:
    """This PC's name and its IPv4 addresses on real networks, for the users' Preferences: the adapters that are up,
    without virtual ones (Hyper-V's and WSL's vEthernet showed the user a 172.20.x address nobody can reach),
    loopback and link-local; without psutil, every IPv4 the name resolves to."""
    name = socket.gethostname()
    ips = []
    try:
        import psutil
        stats = psutil.net_if_stats()
        for nic, addrs in psutil.net_if_addrs().items():
            if not (stats.get(nic) and stats[nic].isup) or any(v in nic.lower() for v in VIRTUAL_ADAPTERS):
                continue
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")) and \
                        a.address not in ips:
                    ips.append(a.address)
        return [name] + ips
    except Exception:                               # noqa: BLE001 - no psutil: the plain list below
        ips = []
    try:
        for info in socket.getaddrinfo(name, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [name] + ips


def serve(host: str, port: int, key: str, workers: int = 1, threads: str = "-", gpu: str = "-",
          log_file=None, keep_hours: float = KEEP_HOURS, beacon_port: int = 8766, lead: bool = False,
          peers: tuple = (), remote_update: bool = False, address: str = "") -> int:
    """Run the server until Ctrl+C (the CLI's `groundtruth serve`); says plainly when it is ready."""
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)
    srv = ComputeServer(key, host, port, workers, threads, gpu, keep_s=keep_hours * 3600.0, beacon_port=beacon_port,
                        lead=lead, peers=tuple(peers), remote_update=remote_update, address=address)
    try:
        srv.start()
    except OSError as e:                            # the port is taken (a second server?) or not allowed
        print(f"Could not listen on {host}:{port}: {e}\nIs another GroundTruth server already running? Use "
              "--port to choose another port.", file=sys.stderr, flush=True)
        return 2
    print(f"READY: listening on port {srv.port}, {srv.workers} job(s) at a time, results kept {keep_hours:g} h. "
          "Leave this window open; Ctrl+C stops the server (a running job starts over at the next start). Jobs "
          "are listed below as they come.\n", flush=True)
    # run by the service's runner in a process of its own (since remote updates): it ends with the runner
    parent = int(os.environ.get("GROUNDTRUTH_PARENT_PID") or 0)
    try:
        while srv.exit_code is None:
            time.sleep(1.0)
            if parent and not _alive(parent):
                log.info("the service's runner has ended: stopping")
                break
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        srv.stop()
    return srv.exit_code or 0


def _alive(pid: int) -> bool:
    """Whether a process still runs (psutil; True when it can't be told)."""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:                               # noqa: BLE001
        return True
