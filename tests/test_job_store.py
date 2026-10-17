"""The window's server-job list (ui/job_store.py) with several plugin windows at once, rebuilt from the servers, and
the server's queue position (the user, 2026-10-16: "current jobs are lost after a while, make sure the plugin
querries the sever and rebuilds the list. Also when starting a new job it queues it and currently is in position 2
with nothing on")."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from groundtruth.remote.client import RemoteError, ServerUnavailable
from groundtruth.ui.job_store import SafeJobs, SyncTracker, close_stale

JID = "7205dd4f49c1f8e4"


def _job(jobs, jid=JID, **meta):
    """A handed-over Simulate in the folder."""
    return jobs.add({"id": jid, "server": "mac:8765", "kind": "simulate", "label": "Simulate", **meta},
                    {"board": "inputs"})


def test_a_job_json_with_a_byte_left_over_is_repaired_not_dropped(tmp_path):
    """What two windows writing one shared job.json.tmp left on the user's laptop: the JSON and one '}' more. The
    old reader returned no job (the job left Current jobs); it is read and written back repaired."""
    jobs = SafeJobs(tmp_path)
    _job(jobs, state="running")
    path = tmp_path / JID / "job.json"
    path.write_text(path.read_text(encoding="utf-8") + "}", encoding="utf-8")
    with pytest.raises(ValueError):
        json.loads(path.read_text(encoding="utf-8"))
    assert jobs.get(JID)["state"] == "running"
    assert [m["id"] for m in jobs.all()] == [JID]
    assert json.loads(path.read_text(encoding="utf-8"))["id"] == JID         # repaired on disk


def test_two_windows_writing_at_once_never_break_the_file(tmp_path):
    """Two stores on one folder (two windows: their own locks) update one job as fast as they can while a third
    reads it: every read is a whole job (each writer has its own temporary file; the replace is retried while the
    reader has the file open)."""
    a, b, reader = SafeJobs(tmp_path), SafeJobs(tmp_path), SafeJobs(tmp_path)
    _job(a)
    stop, bad = threading.Event(), []

    def write(store, n):
        """Rewrite the job with messages of different lengths."""
        k = 0
        while not stop.is_set():
            store.update(JID, message="x" * (n + k % 7), fraction=k / 1000)
            k += 1

    def read():
        """Read it raw, as json.loads does."""
        while not stop.is_set():
            try:
                json.loads((tmp_path / JID / "job.json").read_text(encoding="utf-8"))
            except ValueError as e:
                bad.append(e)
            except OSError:
                pass
    threads = [threading.Thread(target=write, args=(a, 10)), threading.Thread(target=write, args=(b, 30)),
               threading.Thread(target=read)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join()
    assert not bad, bad[:3]
    assert reader.get(JID)["id"] == JID
    assert not list((tmp_path / JID).glob("*.tmp"))


def test_a_finished_job_is_not_set_back_by_another_windows_tracker(tmp_path):
    """Window A fetched the result (ready) and the server forgot the job; window B's tracker, from its older round,
    writes running, then lost (the server no longer knows it): the job stays ready. A shown job stays shown."""
    jobs = SafeJobs(tmp_path)
    _job(jobs, state="running")
    jobs.store_result(JID, b"result")
    jobs.update(JID, state="ready")
    assert jobs.update(JID, state="running", fraction=0.5)["state"] == "ready"
    assert jobs.update(JID, state="lost", error="unknown job")["state"] == "ready"
    jobs.finish(JID, "shown")
    assert jobs.update(JID, state="queued")["state"] == "shown"


class FakeServer:
    """A server's answers: its job list and each job's state."""

    def __init__(self, listed, states=None, down=False):
        """`listed`: GET /v1/jobs?client=...; `states`: id -> GET /v1/jobs/ID."""
        self.listed, self.states, self.down = listed, states or {}, down

    def jobs(self):
        """This computer's jobs."""
        if self.down:
            raise ServerUnavailable("mac: not reachable")
        return self.listed

    def state(self, jid):
        """A job's state; unknown: RemoteError, as the real client."""
        if self.down:
            raise ServerUnavailable("mac: not reachable")
        if jid not in self.states:
            raise RemoteError(f"no job {jid}")
        return self.states[jid]


def _info(jid, state, kind="simulate"):
    """A job as the server lists it."""
    return {"id": jid, "kind": kind, "state": state, "queued_at": 1000.0, "position": 1 if state == "queued" else None,
            "fraction": 0.2, "message": "solving", "host": "Andres-MacBook-Air.local"}


def test_the_list_is_rebuilt_from_the_servers(tmp_path):
    """The server has this computer's running and queued Simulates the list lost (and a part, a cancelled one, a
    Thermal the window waited for, one removed from the list on purpose): the rebuild lists the two, marked as
    listed again (without the inputs, so not to be shown); a server that can't be reached is asked again later."""
    jobs = SafeJobs(tmp_path)
    _job(jobs, "56c32b44d626fe27", state="cancelled")           # known: tells the tracker the server's address
    jobs.add({"id": "aaaaaaaaaaaaaaaa", "server": "mac:8765", "kind": "simulate"}, {})
    jobs.remove("aaaaaaaaaaaaaaaa")                             # removed by the user: stays away
    listed = [_info(JID, "running"), _info("08f9490c72bbb932", "queued"), _info("bbbbbbbbbbbbbbbb", "running", "part"),
              _info("cccccccccccccccc", "cancelled"), _info("dddddddddddddddd", "running", "thermal"),
              _info("aaaaaaaaaaaaaaaa", "running")]
    changes = []
    t = SyncTracker(jobs, lambda address: FakeServer(listed), changes.append, servers=lambda: ["mac:8765"])
    added = t.reconcile()
    assert sorted(m["id"] for m in added) == ["08f9490c72bbb932", JID]
    m = jobs.get(JID)
    assert m["state"] == "running" and m["recovered"] and m["server"] == "mac:8765" and m["label"] == "Simulate"
    assert m["server_name"] == "Andres-MacBook-Air.local" and not jobs.has_inputs(JID)
    assert jobs.get("08f9490c72bbb932")["position"] == 1 and len(changes) == 2
    assert t.reconcile() == []                                  # nothing twice
    down = SyncTracker(SafeJobs(tmp_path / "other"), lambda a: FakeServer([], down=True), changes.append,
                       servers=lambda: ["mac:8765"])
    assert down.reconcile() == []


def test_a_result_another_window_fetched_stays_ready(tmp_path):
    """The other window fetched the result and the server forgot the job: this window's tracker hears "unknown
    job". The job is ready (its result is here), not lost."""
    jobs = SafeJobs(tmp_path)
    _job(jobs, state="running")
    jobs.store_result(JID, b"result")                           # the other window, before it wrote "ready"
    t = SyncTracker(jobs, lambda address: FakeServer([]), lambda m: None)
    t.check()
    assert jobs.get(JID)["state"] == "ready"


def test_a_second_window_does_not_end_the_first_windows_waited_for_jobs(tmp_path):
    """close_stale at a window's start ends the waited-for jobs of windows that are gone, not those of a window
    still open (its process alive); entries without a process id end as before."""
    jobs = SafeJobs(tmp_path)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        jobs.add({"id": "1111111111111111", "kind": "thermal", "attached": True, "state": "running",
                  "pid": other.pid}, None)
        jobs.add({"id": "2222222222222222", "kind": "thermal", "attached": True, "state": "running"}, None)
        assert close_stale(jobs) == 1
        assert jobs.get("1111111111111111")["state"] == "running"
        assert jobs.get("2222222222222222")["state"] == "cancelled"
    finally:
        other.kill()
        other.wait()
    assert close_stale(jobs) == 1                               # its window is gone now
    assert jobs.get("1111111111111111")["state"] == "cancelled"


def test_a_split_jobs_coordinator_is_not_a_job_ahead(monkeypatch, tmp_path):
    """A Simulate split over the cluster runs as a coordinator that gave its worker slot back at once: a job
    queued behind it waits only for the jobs in slots (the user: "position 2 with nothing on")."""
    from groundtruth.remote.server import ComputeServer, Job
    srv = ComputeServer("k" * 24, "127.0.0.1", 0, data_dir=tmp_path / "server_jobs")
    coord = Job("c" * 16, "simulate", "me@here", state="running", split=True)
    slot = Job("s" * 16, "simulate", "me@here", state="running")
    srv.jobs.update({coord.id: coord, slot.id: slot})
    jid, ahead = srv.submit("simulate", "me@here", b"payload")
    assert ahead == 1 and srv.position(srv.jobs[jid]) == 1
    slot.split = True                                           # (a test: two coordinators)
    assert srv.position(srv.jobs[jid]) == 0
