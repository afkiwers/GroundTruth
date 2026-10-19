"""A Simulate split over the cluster's servers (the user, 2026-10-14: "can we have multiple servers (cluster) to do
the sim faster?"): the coordinator, on the server the job was sent to (normally the cluster's lead: a member sends
the plugin on to it, server.py).

The job's items (nets, power stages, bridges; analysis.split.split_items, in a short child process: the server never
unpickles a board itself) are spread over this server and the other servers of the cluster that answer
by what each can do (analysis.split.plan_split: an item only to a server with the memory free for it, more to the
faster ones, every idle server one it fits; the user: "scale the work load depending on the machines
capabilities"). What a split measures (each item's peak memory, each server's speed) is kept (Stats) and plans the
next one. Each share is an ordinary "part" job in that
server's queue: the job's own input passed on unopened, the item names, and whether it runs the rule checks. This
server's part goes to the front of its own queue (the job already had its turn). The coordinator itself runs in a
thread of its own, not in a job slot: a split waiting for its parts holds no slot, so two servers splitting jobs over
each other can't deadlock.

Before a peer gets a board, it proves it holds the access key: its status answer is signed for a fresh nonce
(protocol.sign_response). The cluster's table alone is not trusted with that (a beacon's address is not signed).

The parts are followed every POLL_S: their progress makes the job's ("split over 3 servers: andre 40 %, ..."). A
finished part's result is fetched (and removed there) into this server's data folder. A part whose server can't be
reached for LOST_S, or that fails, runs out of memory or is cancelled there, moves to the server it fits that
would finish it soonest and where it was not tried (`_move`; noted in the progress and the result; an out-of-memory
part teaches Stats); a failure of this server's own part that is not for memory fails the job. Cancel cancels every part. When all are in, a short "merge" child joins them
(analysis.split.merge_parts) into the job's result, which the plugin fetches as from any Simulate.

The job runs whole, as before, when it has fewer than two items or no other server answers.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import socket
import threading
import time
import zlib
from pathlib import Path
from typing import Optional

from . import protocol
from .client import Remote, RemoteError, ServerUnavailable

POLL_S = 1.0                    # how often the parts' states are asked for
LOST_S = 120.0                  # a part's server not reachable for this long: its items run here instead
log = logging.getLogger("groundtruth.server")


def clock(seconds: float) -> str:
    """m:ss or h:mm:ss."""
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


STATS_FILE = "split_stats.json"  # in the server's data folder: what earlier parts measured
SPEED_MIN_S = 60.0              # a part shorter than this says little about its server's speed: not recorded
_stats_lock = threading.Lock()


class Stats:
    """What earlier split parts measured, kept by the server that splits (its data folder, STATS_FILE), so the next
    split is planned by what each server really did (the user: "scale the work load depending on the machines
    capabilities"):
    - per item (board, item name, resolution): its peak memory, GB (`mem`); a part that failed for want of memory
      raises it to at least that server's free memory, so the item is not sent there again;
    - per server (host): the work it got through per second (item cost units / s, `speed`), smoothed over runs.
    Plain JSON; unreadable or missing: empty (the estimates are used)."""

    def __init__(self, folder: Path):
        """The stats in `folder` (loaded now)."""
        self.path = Path(folder) / STATS_FILE
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.items: dict = dict(data.get("items") or {})
        self.speeds: dict = dict(data.get("speed") or {})

    @staticmethod
    def key(board: str, item: str, resolution) -> str:
        """An item's key: the board, the item's name and the resolution (its models' size)."""
        return f"{board}|{item}|{float(resolution or 1):g}"

    def mem(self, board: str, item: str, resolution) -> Optional[float]:
        """The item's measured peak memory (GB), or None."""
        got = self.items.get(self.key(board, item, resolution))
        return float(got["mem_gb"]) if got and got.get("mem_gb") else None

    def speed(self, host: str) -> Optional[float]:
        """The server's measured speed (cost units per second), or None."""
        got = self.speeds.get(host)
        return float(got) if got else None

    def record_part(self, board: str, resolution, host: str, items: list[dict], run_s: float, peak_mb: float):
        """A finished part: its server's speed (when it ran SPEED_MIN_S or longer) and its items' peak memory (a
        part of one item: its peak; of several: an upper bound, kept only where nothing better is known)."""
        if run_s >= SPEED_MIN_S:
            units = sum(float(i["cost"]) for i in items) / run_s
            old = self.speed(host)
            self.speeds[host] = units if old is None else 0.5 * old + 0.5 * units      # smoothed over runs
        if peak_mb:
            for i in items:
                k = self.key(board, i["name"], resolution)
                if len(items) == 1 or k not in self.items:
                    self.items[k] = {"mem_gb": round(peak_mb / 1024.0, 2), "at": time.time()}
        self.save()

    def record_too_big(self, board: str, resolution, items: list[dict], free_gb: Optional[float]):
        """A part that failed for want of memory: its items need more than that server had free."""
        if not free_gb:
            return
        for i in items:
            k = self.key(board, i["name"], resolution)
            need = max(float((self.items.get(k) or {}).get("mem_gb") or 0.0), float(free_gb) * 1.05)
            self.items[k] = {"mem_gb": round(need, 2), "at": time.time()}
        self.save()

    def save(self):
        """Write the file (atomically; a failure only loses what was learned)."""
        with _stats_lock:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"items": self.items, "speed": self.speeds}, indent=1), encoding="utf-8")
                tmp.replace(self.path)
            except OSError as e:
                log.warning("split stats not saved: %s", e)


class Part:
    """One share of the split: where it runs (`remote` None: this server), its items, its job id there, its state."""

    def __init__(self, host: str, remote: Optional[Remote], items: list[dict], checks: bool,
                 server: Optional[dict] = None):
        """Not submitted yet. `server`: its entry in the plan (free memory, speed)."""
        self.host, self.remote, self.items, self.checks = host, remote, items, checks
        self.server = server or {}
        # the servers it was sent to, as their plan entries (id): not by host name, two servers may share one
        self.tried: list[int] = [id(self.server)] if server else []
        self.names = [i["name"] for i in items]
        self.cost = sum(float(i["cost"]) for i in items)
        self.id = ""
        self.state, self.fraction, self.message = "queued", 0.0, ""
        self.lost_since: Optional[float] = None
        self.run_s = 0.0
        self.result: Optional[bytes] = None     # protocol.loads-ready bytes, once fetched
        self.again = ""                         # why it runs again here (its server was lost or failed it)


class SplitRun:
    """Coordinates one split Simulate (see the module). `run()` in a thread of its own: it ends the job (done, failed,
    cancelled), or puts it back in the queue to run whole."""

    def __init__(self, server, job):
        """`server`: the ComputeServer; `job`: its Job, state running, taken from the queue."""
        self.server, self.job = server, job
        self.payload = b""
        self.parts: list[Part] = []
        self.notes: list[str] = []
        self.left_out: list[str] = []           # the cluster's servers not used, and why (for the job's note)
        self.servers: list[dict] = []           # the plan's servers (this one first): host, remote, speed, free_gb
        self.stats = Stats(server.data)
        self.board, self.resolution = "", 1.0   # the job's board and resolution: the keys of what is measured

    # ------------------------------------------------------------------ helpers
    def _child(self, kind: str, args_bytes: bytes):
        """A short worker process of this server (items, merge): its raw result bytes."""
        from ..analysis.thermal_proc import _run_child

        def progress(msg: str, frac: float):
            """The child's progress, as the job's message."""
            self.job.message = msg
        raw, _peak = _run_child(args_bytes, progress, self.job.cancel, 0.2, self.server._limits(),
                                module="groundtruth.remote.worker", args=(kind, self.server.threads, self.server.gpu),
                                raw=True, what="the job")
        return raw

    def _peers(self) -> list[dict]:
        """The cluster's other servers that answer now and run this code, each proven to hold the key (its status
        is signed for our fresh nonce): {host, remote, busy, cores, free_gb, measured}."""
        out = []
        cluster = self.server.cluster
        from .cluster import address_candidates
        for p in (cluster.alive() if cluster is not None else []):
            # its best address first, then the others it announced (cluster.address_candidates)
            st, r, error = None, None, None
            for addr in address_candidates(p.address, p.info.get("addresses"), cluster.own()):
                r = Remote(f"{addr}:{p.port}", self.server.key,
                           client=f"{self.job.client} via {socket.gethostname()}"[:80])
                try:
                    st = r.status()
                    break
                except (ServerUnavailable, OSError, ValueError) as e:
                    error = e
            if st is None:
                log.info("%s split: %s does not answer (%s): left out", self.job.id, p.host, error)
                self.left_out.append(f"{p.host} did not answer ({error})")
                continue
            if st.get("code") != self.server.code:
                self.left_out.append(f"{p.host} runs another GroundTruth version")
                continue
            busy = len(st.get("running") or []) + len(st.get("queue") or [])
            host = st.get("host") or p.host
            out.append({"host": host, "remote": r, "busy": busy, "cores": st.get("cores"),
                        "free_gb": st.get("free_gb"), "measured": self.stats.speed(host)})
        return out

    def _speeds(self, servers: list[dict]):
        """Each server's speed for the plan: the measured ones when every server has been measured (one scale for
        all), else the cores-based estimate for all (analysis.split.server_speed)."""
        from ..analysis.split import server_speed
        measured = all(s.get("measured") for s in servers)
        for s in servers:
            s["speed"] = server_speed(s.get("cores"), s.get("busy", 0), s.get("measured") if measured else None)

    def _describe(self, s: dict, items: list[dict]) -> str:
        """A server's share in words, for the log and the notes: its items, its free memory and speed."""
        free = s.get("free_gb")
        return (f"{s['host']} ({', '.join(i['name'] for i in items)}; "
                + (f"{free:.1f} GB free" if free is not None else "free memory unknown")
                + (f", measured {s['measured']:.3g} units/s" if s.get("measured") else f", {s.get('cores')} cores")
                + ")")

    def _whole(self, why: str):
        """Run the job whole after all: back to the front of the queue, marked not to split."""
        log.info("%s runs whole: %s", self.job.id, why)
        srv = self.server
        with srv.lock:
            # the reason goes with the job to the plugin (its details box), not only to the log
            self.job.note = f"not split over the cluster: {why}"
            self.job.whole, self.job.state, self.job.message = True, "queued", ""
            self.job.started = None
            srv.order.insert(0, self.job.id)
            srv._save(self.job)
            srv.lock.notify_all()

    def _submit(self, part: Part):
        """Queue a part on its server (this one: at the front of the queue)."""
        args = (self.payload, part.names, part.checks)
        if part.remote is None:
            part.id, _ = self.server.submit("part", f"{self.job.client} (part of {self.job.id})",
                                            pickle.dumps(args, protocol=pickle.HIGHEST_PROTOCOL), front=True,
                                            parent=self.job.id)
        else:
            part.id, _ = part.remote.submit("part", args)
        part.state, part.lost_since = "queued", None
        log.info("%s split: part %s on %s: %s", self.job.id, part.id, part.host, ", ".join(part.names))

    def _target(self, part: Part) -> Optional[dict]:
        """Where a part goes when its server lost it: the plan's servers it was not tried on yet and whose free
        memory fits all its items, the one that would finish it soonest (its open work / speed); this server when
        it fits or no server fits (it was the plan's last resort anyway); None when this server was tried and
        nothing else fits."""
        from ..analysis.split import fits
        able = [s for s in self.servers if id(s) not in part.tried and all(fits(i, s) for i in part.items)]

        def open_work(s: dict) -> float:
            """The cost of the parts still running or waiting on a server, over its speed."""
            return sum(p.cost for p in self.parts if p.server is s and p.result is None) / (s.get("speed") or 1)
        if able:
            return min(able, key=open_work)
        mine = self.servers[0] if self.servers else None        # this server: the plan's first entry
        return mine if mine is not None and id(mine) not in part.tried else None

    def _move(self, part: Part, why: str, memory: bool = False):
        """Send a part's items to another server (its server was lost, or failed or cancelled it), named in the
        progress and the result's notes. `memory`: it failed for want of memory: its items' need is raised to more
        than that server had free (Stats), so neither this run nor later ones send them there again."""
        if part.remote is not None and part.id:
            try:
                part.remote.forget(part.id)
            except (ServerUnavailable, RemoteError, OSError):
                pass
        if memory:
            self.stats.record_too_big(self.board, self.resolution, part.items, part.server.get("free_gb"))
            for i in part.items:
                i["mem_gb"] = self.stats.mem(self.board, i["name"], self.resolution) or i.get("mem_gb")
        target = self._target(part)
        if target is None:
            raise RuntimeError(f"the part with {', '.join(part.names)} {why} on {part.host}, and no other server of "
                               "the cluster has the memory for it")
        old = part.host
        part.again = f"{old} {why}: moved to {target['host']}"
        self.notes.append(f"{', '.join(part.names)}: {why} on {old}, so it ran on {target['host']}")
        log.info("%s split: %s %s: moved to %s", self.job.id, old, why, target["host"])
        part.host, part.remote, part.server = target["host"], target.get("remote"), target
        part.tried.append(id(target))
        part.fraction = 0.0
        self._submit(part)

    def _cancel_parts(self):
        """Cancel every part not fetched yet (best effort: a lost server drops its part when it expires)."""
        for part in self.parts:
            if not part.id or part.result is not None:
                continue
            try:
                if part.remote is None:
                    self.server.cancel(part.id)
                else:
                    part.remote.forget(part.id)
            except (ServerUnavailable, RemoteError, OSError):
                pass

    def _poll(self, part: Part):
        """One look at a part: its progress; fetched when done; run again here when lost or failed elsewhere."""
        srv = self.server
        if part.remote is None:
            with srv.lock:
                j = srv.jobs.get(part.id)
                info = j.info(srv.position(j)) if j is not None else None
            if info is None:
                raise RuntimeError(f"this server's part {part.id} is gone")
        else:
            try:
                info = part.remote.state(part.id)
                part.lost_since = None
            except ServerUnavailable as e:
                part.lost_since = part.lost_since or time.monotonic()
                part.message = f"{part.host} can't be reached ({e})"
                if time.monotonic() - part.lost_since > LOST_S:
                    self._move(part, f"could not be reached for {LOST_S / 60:.0f} min")
                return
            except RemoteError:
                self._move(part, "no longer had the part")
                return
        part.state, part.fraction, part.message = info["state"], float(info.get("fraction") or 0.0), \
            info.get("message") or ""
        part.run_s = float(info.get("run_s") or 0.0)
        if part.state == "done":
            if part.remote is None:
                part.result = srv._path(part.id, ".out").read_bytes()
                srv.cancel(part.id)                     # fetched: its files go
            else:
                try:
                    part.result = part.remote.fetch(part.id, raw=True)
                except ServerUnavailable:
                    part.lost_since = part.lost_since or time.monotonic()
                    return
                try:
                    part.remote.forget(part.id)
                except (ServerUnavailable, RemoteError, OSError):
                    pass
            part.fraction = 1.0
            log.info("%s split: part on %s done after %s (peak %.1f GB)", self.job.id, part.host, clock(part.run_s),
                     float(info.get("peak_mb") or 0) / 1024)
            # what it measured: the server's speed and the items' memory, for the next split's plan
            self.stats.record_part(self.board, self.resolution, part.host, part.items, part.run_s,
                                   float(info.get("peak_mb") or 0))
        elif part.state in ("failed", "cancelled"):
            why = info.get("error") or part.state
            memory = "memory" in why.lower()
            if part.remote is None and not memory:
                raise RuntimeError(f"the part on {part.host} ({', '.join(part.names)}) {part.state}: {why}")
            self._move(part, "ran out of memory" if memory else f"{part.state} it ({why})", memory=memory)

    def _progress(self):
        """The job's progress from its parts' (weighted by their cost)."""
        total = sum(p.cost for p in self.parts) or 1.0
        self.job.fraction = 0.95 * sum(p.cost * (1.0 if p.result is not None else p.fraction)
                                       for p in self.parts) / total
        each = ", ".join(f"{p.host} " + ("done" if p.result is not None else
                                         "waiting" if p.state == "queued" else f"{p.fraction * 100:.0f} %")
                         for p in self.parts)
        moved = "; ".join(p.again for p in self.parts if p.again)
        self.job.message = f"split over {len(self.parts)} servers: {each}" + (f" ({moved})" if moved else "")

    # ------------------------------------------------------------------ the run
    def run(self):
        """Plan, send, follow, merge (see the module); the job ends here, or goes back to run whole."""
        from ..analysis.split import plan_split
        srv, job = self.server, self.job
        try:
            self.payload = srv._path(job.id, ".in").read_bytes()
            job.message = "splitting over the cluster: finding the items"
            got = protocol.load_raw(self._child("items", self.payload))
            items, self.board, self.resolution = got["items"], got.get("board", ""), got.get("resolution", 1.0)
            for i in items:             # what earlier runs measured replaces the estimate
                i["mem_gb"] = self.stats.mem(self.board, i["name"], self.resolution) or i.get("mem_gb")
            if len(items) < 2:
                self._whole("the board has one item to simulate (a net, power stage or bridge): nothing to split")
                return
            peers = self._peers()
            if not peers:
                self._whole("no other server of the cluster answers" + (
                    f" ({'; '.join(self.left_out)})" if self.left_out else ""))
                return
            from .server import server_memory
            with srv.lock:          # this server's other jobs (this one is running, but holds no slot)
                busy = sum(1 for j in srv.jobs.values() if j.state == "running" and j.id != job.id) + len(srv.order)
            free_mb = server_memory().get("server_free_mb")
            me = socket.gethostname()
            servers = [{"host": me, "remote": None, "busy": busy, "cores": os.cpu_count(),
                        "free_gb": round(free_mb / 1024, 1) if free_mb else None,
                        "measured": self.stats.speed(me)}] + peers
            self._speeds(servers)
            self.servers = servers
            plan = plan_split(items, servers)
            shares = [(s, its) for s, its in zip(servers, plan) if its]
            log.info("%s split plan: %s", job.id, "; ".join(self._describe(s, its) for s, its in shares))
            if len(shares) < 2 and shares[0][0] is servers[0]:
                # all on this server: run it whole here (a single part on another server is sent there, below:
                # a member with the memory this server lacks)
                others = [s["host"] for s in servers if not any(sh[0] is s for sh in shares)]
                self._whole(f"its {len(items)} item(s) go best to this server alone ({', '.join(others)}: busy, "
                            "or too little memory free for them)")
                return
            # this server's own share (when it has one) runs the job's rule checks
            self.parts = [Part(s["host"], s["remote"], its, k == 0, s) for k, (s, its) in enumerate(shares)]
            for part in self.parts:
                try:
                    self._submit(part)
                except (ServerUnavailable, RemoteError, OSError) as e:
                    self._move(part, f"did not take it ({e})")
            started = time.monotonic()
            while True:
                if job.cancel.is_set():
                    self._cancel_parts()
                    job.state = "queued" if srv._stop.is_set() else "cancelled"
                    return
                for part in self.parts:
                    if part.result is None:
                        self._poll(part)
                self._progress()
                if all(p.result is not None for p in self.parts):
                    break
                job.cancel.wait(POLL_S)
            each = "; ".join(f"{p.host}: {', '.join(p.names)} ({clock(p.run_s)})" for p in self.parts)
            took = clock(time.monotonic() - started)
            self.notes.insert(0, f"split over {len(self.parts)} compute servers in {took}: {each}" if len(self.parts) > 1
                              else f"run on {each} in {took}: the server it was sent to has too little memory free "
                                   "for it")
            job.message = "joining the parts"
            raw = self._child("merge", pickle.dumps((self.payload, [p.result for p in self.parts], self.notes),
                                                    protocol=pickle.HIGHEST_PROTOCOL))
            data = zlib.compress(raw, 1)
            srv._path(job.id, ".out").write_bytes(data)
            job.result_bytes, job.state, job.fraction = len(data), "done", 1.0
        except Exception as e:                      # noqa: BLE001 - the job's failure, reported to its owner
            if job.cancel.is_set():
                job.state = "queued" if srv._stop.is_set() else "cancelled"
            else:
                text = str(e)
                job.state, job.error, job.detail = "failed", text.split("\n\n")[0], f"{type(e).__name__}: {text}"
                log.warning("%s split failed:\n%s", job.id, job.detail)
            self._cancel_parts()
        finally:
            if job.state != "queued" or srv._stop.is_set():
                with srv.lock:
                    job.finished = time.time() if job.state != "queued" else None
                    if job.state not in ("queued",):
                        try:
                            srv._path(job.id, ".in").unlink()
                        except OSError:
                            pass
                    srv._save(job)
            log.info("%s %s%s", job.id, job.state, f": {job.error}" if job.error else "")
