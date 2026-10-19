"""The plugin's side of the compute server (remote/server.py): send a job, follow it, fetch its result.

`Remote.run(kind, args, progress)` behaves like the local call it replaces: it returns the result object, reports
the server's progress through `progress(message, fraction)` (which raises the plugin's Cancelled when the user
presses Cancel: the job on the server is then cancelled too), and raises

- ServerUnavailable when the server can't be reached or refuses the job before it starts (wrong address, server
  not running, wrong access key, another version): the plugin offers to run the job on this computer instead;
- RemoteError when the job failed on the server (its error message).

Once the server has the job, a lost connection is waited out, never a reason to give the job up: only the user
cancels it. The plugin follows Simulate and Suggest fixes jobs detached from the window (remote/jobs.py) with the
one-request helpers state / fetch / forget / jobs.

Plain urllib, no extra packages; every request signed, every answer checked before a byte of it is unpickled
(protocol.py).
"""
from __future__ import annotations

import getpass
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Callable, Optional
from urllib.parse import quote

from .. import build_version
from . import protocol
from .protocol import ProtocolError

POLL_S = 0.5                    # how often a running job's state is asked for
CONNECT_TIMEOUT_S = 5.0         # reaching the server (a dead address fails within this)
POLL_TIMEOUT_S = 20.0           # one answer about a job already handed over (a busy server may be slow)
RETRY_S = 2.0                   # how often a server that can't be reached is tried again (never given up)
TRANSFER_TIMEOUT_S = 600.0      # sending a job or fetching a result (hundreds of MB on a slow network)


class ServerUnavailable(Exception):
    """The job could not be handed to the server (see the module)."""


class RemoteError(Exception):
    """The job failed on the server."""


def parse_address(address: str) -> str:
    """The server's base URL from what the user typed: "host", "host:port" or "http://host:port"."""
    a = address.strip().rstrip("/")
    if not a:
        raise ServerUnavailable("no server address (Preferences, Compute)")
    if a.startswith("https://"):
        raise ServerUnavailable("the compute server speaks plain http on the local network: use http://")
    if not a.startswith("http://"):
        a = "http://" + a
    host = a[len("http://"):]
    if ":" not in host.split("/")[0]:
        a += f":{protocol.DEFAULT_PORT}"
    return a


class Remote:
    """A compute server at `address` with the access key `key`."""

    def __init__(self, address: str, key: str, client: Optional[str] = None):
        """`client`: the name the server's log and queue show (default user@computer)."""
        self.url = parse_address(address)
        self.key = (key or "").strip()
        if not self.key:
            raise ServerUnavailable("no access key for the compute server (Preferences, Compute)")
        try:
            who = getpass.getuser()
        except Exception:                          # noqa: BLE001
            who = "?"
        self.client = client or f"{who}@{socket.gethostname()}"
        self.watch: Optional[Callable[[str, str, dict], None]] = None     # run()'s default `watch`

    @property
    def host(self) -> str:
        """The server's host:port, for the status line."""
        return self.url[len("http://"):]

    def request(self, method: str, path: str, body: bytes = b"", timeout: float = CONNECT_TIMEOUT_S
                ) -> tuple[int, bytes]:
        """(status, body) of a signed request whose answer is checked; ServerUnavailable when the server can't be
        reached or refuses the key."""
        headers = protocol.sign_request(self.key, method, path, body)
        req = urllib.request.Request(self.url + path, data=body if method == "POST" else None, method=method,
                                     headers={**headers, "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, data, sig = r.status, r.read(), r.headers.get("X-GT-Sig")
        except urllib.error.HTTPError as e:
            status, data, sig = e.code, e.read(), e.headers.get("X-GT-Sig")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ServerUnavailable(f"the compute server at {self.host} can't be reached ({reason})")
        if status == 403 and sig is None:           # refused before it was trusted: its reason, unsigned
            raise ServerUnavailable(f"the compute server at {self.host} refused the request: {_error(data)}")
        try:
            protocol.check_response(self.key, headers["X-GT-Nonce"], status, sig, data)
        except ProtocolError as e:
            raise ServerUnavailable(f"{self.host}: {e}")
        return status, data

    def status(self) -> dict:
        """The server's status (GET /v1/status): its version, workers, queue, GPUs, memory."""
        st, data = self.request("GET", "/v1/status")
        if st != 200:
            raise ServerUnavailable(f"{self.host}: {_error(data)}")
        return json.loads(data)

    def submit(self, kind: str, args: tuple) -> tuple[str, int]:
        """Queue a job: (its id, jobs ahead of it).

        A member of a cluster answers a Simulate with its lead's address (307; the lead splits it over the
        cluster): the job goes to the lead, which first proves it holds the key (its status answer is signed for
        our fresh nonce: the address came from the cluster's announcements, which don't vouch for it), and from
        then on this Remote is the lead's (the job's state, result and Cancel are there). When the lead does not
        answer, the job goes back to the member with direct=1, and the member runs it."""
        import numpy
        body = protocol.dumps(args)
        path = (f"/v1/jobs?kind={quote(kind)}&client={quote(self.client)}&code={protocol.code_id()}"
                f"&numpy={quote(numpy.__version__)}&version={quote(build_version())}")
        st, data = self.request("POST", path, body, timeout=TRANSFER_TIMEOUT_S)
        if st == 307:
            to = json.loads(data)
            lead = Remote(str(to.get("redirect")), self.key, self.client)
            try:
                lead.status()
                st, data = lead.request("POST", path + "&direct=1", body, timeout=TRANSFER_TIMEOUT_S)
                if st == 201:
                    self.url = lead.url
                    self.redirected_from = to.get("host") or ""
            except ServerUnavailable:
                st = 0
            if st != 201:
                st, data = self.request("POST", path + "&direct=1", body, timeout=TRANSFER_TIMEOUT_S)
        if st != 201:
            raise ServerUnavailable(f"the compute server at {self.host} refused the job: {_error(data)}")
        got = json.loads(data)
        self.server_name = got.get("host") or self.host     # the server computer's name, for the Server jobs tab
        self.server_version = got.get("version") or ""      # its build ("" from a server before 2026-10-19)
        return got["id"], int(got.get("position") or 0)

    def run(self, kind: str, args: tuple, progress: Optional[Callable[[str, float], None]] = None,
            watch: Optional[Callable[[str, str, dict], None]] = None):
        """Run a job on the server, waiting for it, and return its result (see the module). Once the server has the
        job, nothing but the user ends the wait: a lost connection is waited out (the user, 2026-10-14: "The system
        client should not cancel the job unless the user cancels it"); the plugin's Cancel raises in `progress` and
        the job is cancelled on the server (when it can be reached).

        `watch(event, job id, info)`: told "submitted" ({position, host, version}), every "state" (the server's job info) and
        the "end" ({state: done | failed | cancelled, error}): the plugin lists the waited-for jobs on its Server jobs
        tab with it (the user: a Thermal on the server was not listed there). A failing watch never stops the job."""
        watch = watch or self.watch

        def tell(event: str, jid: str, info: dict):
            """watch(), its errors ignored."""
            if watch is not None:
                try:
                    watch(event, jid, info)
                except Exception:                   # noqa: BLE001 - the list is a convenience, the job matters
                    pass
        jid, ahead = self.submit(kind, args)
        tell("submitted", jid, {"position": ahead, "host": getattr(self, "server_name", self.host),
                                "version": getattr(self, "server_version", "")})
        end = {"state": "cancelled", "error": "cancelled here"}
        try:
            while True:
                info = self.wait_state(jid, progress)
                tell("state", jid, info)
                state = info["state"]
                # progress() is also where the plugin's Cancel stops this loop (it raises)
                if state == "queued" and progress:
                    n = info.get("position") or 0
                    progress(f"waiting for the compute server {self.host}: {n} job(s) ahead" if n else
                             f"starting on the compute server {self.host}…", 0.0)
                elif state == "running" and progress:
                    mem = memory_text(info, short=True)
                    progress(f"{info['message']} (on {self.host}" + (f", {mem})" if mem else ")"),
                             float(info["fraction"]))
                elif state == "done":
                    break
                elif state == "cancelled":
                    end = {"state": "cancelled", "error": "cancelled on the compute server"}
                    raise RemoteError("the job was cancelled on the compute server")
                elif state == "failed":
                    end = {"state": "failed", "error": info.get("error") or "failed", "detail": info.get("detail")}
                    raise RemoteError(f"the job failed on the compute server {self.host}: {info.get('error')}")
                time.sleep(POLL_S)
            if progress:
                progress(f"fetching the result from {self.host}…", 1.0)
            result = self._retry(lambda: self.fetch(jid), progress, "fetching the result")
            end = {"state": "done", "error": ""}
            return result
        except RemoteError as e:
            if end["state"] == "cancelled" and end["error"] == "cancelled here":
                end = {"state": "failed", "error": str(e)}
            raise
        finally:
            # one DELETE either way: a fetched result is removed from the server, a job stopped here (the user's
            # Cancel, or it failed) is cancelled or forgotten there
            try:
                self.forget(jid)
            except (ServerUnavailable, OSError):
                pass
            tell("end", jid, end)

    # ---- one request each (the plugin's job tracker, remote/jobs.py, follows detached jobs with these)
    def state(self, jid: str) -> dict:
        """A job's state (GET /v1/jobs/ID): ServerUnavailable when the server can't be reached, RemoteError when
        it doesn't know the job (fetched already, or expired)."""
        st, data = self.request("GET", f"/v1/jobs/{jid}", timeout=POLL_TIMEOUT_S)
        if st == 404:
            raise RemoteError(_error(data))
        if st != 200:
            raise ServerUnavailable(f"{self.host}: {_error(data)}")
        return json.loads(data)

    def fetch(self, jid: str, raw: bool = False):
        """A finished job's result (the object, or with `raw` its signed-checked bytes, protocol.loads-ready)."""
        st, data = self.request("GET", f"/v1/jobs/{jid}/result", timeout=TRANSFER_TIMEOUT_S)
        if st == 200:
            return data if raw else protocol.loads(data)
        if st in (404, 409):
            raise RemoteError(f"{self.host}: {_error(data)}")
        raise ServerUnavailable(f"{self.host}: {_error(data)}")

    def forget(self, jid: str):
        """DELETE /v1/jobs/ID: cancel a queued or running job; remove a finished one (its result) from the server."""
        self.request("DELETE", f"/v1/jobs/{jid}")

    def update(self, data: bytes, signature: str) -> dict:
        """POST /v1/update: hand the server a signed server package (remote/release.py). Its answer ({state:
        waiting | installing, version, running}); RemoteError with the server's reason when it refuses it (remote
        updates off, another release key, not newer)."""
        st, body = self.request("POST", f"/v1/update?sig={quote(signature)}", data, timeout=TRANSFER_TIMEOUT_S)
        if st == 202:
            return json.loads(body)
        raise RemoteError(f"{self.host}: {_error(body)}")

    def jobs(self) -> list[dict]:
        """This client's jobs on the server (GET /v1/jobs?client=...)."""
        st, data = self.request("GET", f"/v1/jobs?client={quote(self.client)}", timeout=POLL_TIMEOUT_S)
        if st != 200:
            raise ServerUnavailable(f"{self.host}: {_error(data)}")
        return json.loads(data)

    def wait_state(self, jid: str, progress=None) -> dict:
        """A job's state, asked again until the server answers: once the job is on the server a lost connection is
        no reason to give it up (the user's server froze loading PyTorch, a laptop sleeps or leaves the network)."""
        return self._retry(lambda: self.state(jid), progress, "waiting for its answer")

    def _retry(self, call, progress, doing: str):
        """call() until it gets through (ServerUnavailable retried every RETRY_S; the plugin's Cancel raises in
        `progress`, which is what ends it)."""
        lost = None
        while True:
            try:
                return call()
            except ServerUnavailable as e:
                lost = lost or time.monotonic()
                waited = time.monotonic() - lost
                if progress:
                    progress(f"the compute server {self.host} can't be reached for {waited:.0f} s ({doing}); "
                             f"retrying. The job stays there: Cancel only if you want it stopped. ({e})", 0.0)
                time.sleep(RETRY_S)


def memory_text(info: dict, short: bool = False) -> str:
    """A job's memory on the server, from its info (server.Job.info): 'uses 12.4 GB (reserved 31.0 GB), peak 15.1 GB;
    server free: RAM 30.2 of 62.7 GB, commit 18.0 GB'; short: 'uses 12.4 GB · 30.2 GB free'. '' without figures."""
    def gb(mb):
        """MB as GB."""
        return f"{mb / 1024:.1f} GB"
    use, commit, peak = info.get("mem_mb"), info.get("commit_mb"), info.get("peak_mb")
    free, total, cfree = info.get("server_free_mb"), info.get("server_total_mb"), info.get("server_commit_free_mb")
    if short:
        parts = ([f"uses {gb(use)}"] if use is not None else [f"peak {gb(peak)}"] if peak else [])
        parts += [f"{gb(free)} free"] if free is not None and use is not None else []
        return " · ".join(parts)
    job = (f"uses {gb(use)}" + (f" (reserved {gb(commit)})" if commit is not None else "") if use is not None
           else "")
    if peak:
        job += (", " if job else "") + f"peak {gb(peak)}"
    server = ""
    if free is not None:
        server = f"server free: RAM {gb(free)}" + (f" of {gb(total)}" if total else "") +             (f", commit {gb(cfree)}" if cfree is not None else "")
    return "; ".join(x for x in (job, server) if x)


def _error(data: bytes) -> str:
    """The "error" of a JSON answer, or its text."""
    try:
        return str(json.loads(data).get("error") or data[:200])
    except (ValueError, AttributeError):
        return data[:200].decode("utf-8", "replace")


def describe(st: dict) -> str:
    """One line about a server's status, for Test connection."""
    gpus = ", ".join(st.get("gpus") or []) or ("no GPU found (CPU solves)"
                                               + (f": {st['gpu_note']}" if st.get("gpu_note") else ""))
    busy = len(st.get("running") or []) + len(st.get("queue") or [])
    text = (f"{st.get('host')}: {st.get('version', 'GroundTruth')} (code {st.get('code')}), {st.get('cores')} cores, "
            f"{st.get('memory_gb')} GB ({st.get('free_gb')} GB free), {gpus}; {st.get('workers')} job(s) at a time, "
            f"{busy} job(s) there now")
    cl = cluster_text(st.get("cluster") or {}, st.get("host", ""))
    return text + (f"\n\n{cl}" if cl else "")


def cluster_text(c: dict, host: str = "") -> str:
    """The cluster a server sees (remote/cluster.py status), in words: its size and for how long, the lead, every
    server with its address, hardware and time in the cluster, the last losses; '' when standalone without any."""
    from .cluster import span
    members = c.get("members") or []
    now = c.get("now") or time.time()
    lost = [e for e in c.get("events") or [] if e.get("event") == "lost"][-3:]
    if c.get("role") in (None, "standalone") and not members:
        return "" if not lost else "Cluster: standalone now; " + "; ".join(
            f"{e['host']} lost {span(now - e['at'])} ago" for e in lost)
    lead = c.get("lead") or {}
    since = c.get("together_since")
    who = "this server (" + host + ")" if lead.get("me") else f"{lead.get('host')} ({lead.get('address')})"
    lines = [f"Cluster: {c.get('size', 1 + len(members))} servers" + (f" for {span(now - since)}" if since else "")
             + f"; lead {who}" + (" — set with --lead" if c.get("forced_lead") and lead.get("me") else "")]
    for m in members:
        gpus = ", ".join(m.get("gpus") or []) or "no GPU"
        joined = m.get("joined_at")
        lines.append(f"  {'lead' if m.get('lead') else 'member'} {m.get('host')} ({m.get('address')}): "
                     f"{m.get('cores')} cores, {m.get('memory_gb')} GB, {gpus}"
                     + (f"; in the cluster {span(now - joined)}" if joined else ""))
    for e in lost:
        lines.append(f"  {e['host']} was lost {span(now - e['at'])} ago ({e.get('text', '')})")
    for m in c.get("other_versions") or []:
        lines.append(f"  {m.get('host')} has the key but another GroundTruth version (code {m.get('code')}): "
                     "not used until updated")
    return "\n".join(lines)
