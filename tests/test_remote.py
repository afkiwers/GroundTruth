"""The compute server (remote/): signed requests only, restricted pickles, the queue, cancel, and a Simulate on the
server that equals the local one."""
import io
import json
import os
import pickle
from pathlib import Path
import time
import urllib.request

import numpy as np
import pytest

from conftest import make, microstrip_board
from groundtruth.analysis.simulate import simulate
from groundtruth.remote import protocol
from groundtruth.remote.client import Remote, RemoteError, ServerUnavailable, parse_address
from groundtruth.remote.protocol import ProtocolError
from groundtruth.remote.server import ComputeServer

KEY = "test-key-0123456789abcdef"


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A compute server on this machine (a free port, its jobs in tmp_path), its test job kind on, one job at a
    time."""
    monkeypatch.setenv("GROUNDTRUTH_SERVER_TEST", "1")
    srv = ComputeServer(KEY, "127.0.0.1", 0, workers=1, data_dir=tmp_path / "server_jobs")
    srv.start()
    yield srv
    srv.stop()


def remote(srv, key=KEY):
    """A client of `srv`."""
    return Remote(f"127.0.0.1:{srv.port}", key, client="tester@here")


def test_addresses_are_completed_with_the_default_port():
    """What the user types in Preferences: a bare host gets http:// and the default port."""
    assert parse_address("bigpc") == "http://bigpc:8765"
    assert parse_address(" 192.168.1.20:9000/ ") == "http://192.168.1.20:9000"
    with pytest.raises(ServerUnavailable):
        parse_address("")


def test_the_restricted_unpickler_builds_results_and_refuses_code():
    """GroundTruth's classes, NumPy arrays (complex too) and plain builtins load; os.system does not."""
    from groundtruth.core.issues import Issue
    obj = {"a": np.arange(6, dtype=complex).reshape(2, 3), "b": {1, 2}, "c": 1 + 2j,
           "i": Issue("current_density", "warning", "x", "msg", (1.0, 2.0))}
    back = protocol.loads(protocol.dumps(obj))
    assert np.array_equal(back["a"], obj["a"]) and back["c"] == 1 + 2j and back["i"].message == "msg"

    class Evil:
        def __reduce__(self):
            return os.system, ("echo pwned",)
    import zlib
    with pytest.raises(ProtocolError, match="refused"):
        protocol.loads(zlib.compress(pickle.dumps(Evil())))
    with pytest.raises(ProtocolError):           # a groundtruth *function* is refused too, only classes load
        protocol.SafeUnpickler(io.BytesIO(pickle.dumps(protocol.new_key))).load()


def test_requests_need_the_key_and_cannot_be_replayed(server):
    """A wrong key is refused before anything is read; a captured request can't be sent again."""
    with pytest.raises(ServerUnavailable, match="wrong access key"):
        remote(server, "another-key-0123456789").status()
    st = remote(server).status()
    assert st["code"] == protocol.code_id() and st["workers"] == 1
    # replay: the same signed headers a second time
    headers = protocol.sign_request(KEY, "GET", "/v1/status")
    url = f"http://127.0.0.1:{server.port}/v1/status"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        assert r.status == 200
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers))
    assert e.value.code == 403 and "replayed" in json.loads(e.value.read())["error"]


def test_a_plugin_of_another_version_is_refused(server, monkeypatch):
    """The result's classes must match the window's: a job from other code is refused, with what to do."""
    monkeypatch.setattr(protocol, "code_id", lambda: "000000000000")
    with pytest.raises(ServerUnavailable, match="install the same package"):
        remote(server).run("sleep", (0.1,))


def test_jobs_queue_first_come_first_served_and_cancel(server):
    """One job at a time: the second waits (its position shown), a cancelled running job's process ends."""
    r = remote(server)
    first, _ = r.submit("sleep", (3.0,))
    second, ahead = r.submit("sleep", (0.2,))
    assert ahead == 1
    for _ in range(100):
        if server.jobs[first].state == "running":
            break
        time.sleep(0.05)
    st = json.loads(r.request("GET", f"/v1/jobs/{second}")[1])
    assert st["state"] == "queued" and st["position"] == 1
    r.request("DELETE", f"/v1/jobs/{first}")                    # cancel the running one
    for _ in range(200):
        if server.jobs[second].state == "done":
            break
        time.sleep(0.05)
    assert server.jobs[first].state == "cancelled" and server.jobs[second].state == "done"
    assert server.jobs[first].finished - server.jobs[first].started < 2.5     # stopped, not slept out


def test_progress_comes_through_and_cancel_on_the_plugin_stops_the_job(server):
    """The plugin's progress callback sees the server's steps; raising in it (the plugin's Cancel) cancels the
    job on the server."""
    seen = []

    class Stop(Exception):
        pass

    def progress(msg, frac):
        seen.append((msg, frac))
        if frac > 0.2:
            raise Stop()
    with pytest.raises(Stop):
        remote(server).run("sleep", (5.0,), progress)
    assert any("sleeping" in m and "on 127.0.0.1" in m for m, _ in seen)
    job = next(iter(server.jobs.values()))
    for _ in range(100):
        if job.state == "cancelled":
            break
        time.sleep(0.05)
    assert job.state == "cancelled"


def test_a_failed_job_reports_its_error(server):
    """A job that raises on the server: RemoteError with the server's message; the error is kept in full with the
    job (its detail: the worker's traceback) and named the server computer."""
    r = remote(server)
    jid, _ = r.submit("sleep", ("not a number",))
    for _ in range(200):
        info = r.state(jid)
        if info["state"] == "failed":
            break
        time.sleep(0.05)
    assert info["state"] == "failed" and "ValueError" in info["detail"] and "Traceback" in info["detail"]
    assert info["host"] and info["error"] and "\n\n" not in info["error"]
    with pytest.raises(RemoteError, match="failed on the compute server"):
        r.run("sleep", ("not a number",))


def test_simulate_on_the_server_equals_the_local_run(server, tmp_path):
    """The same board and settings: the server's result has the same issues and route numbers as a local
    Simulate (results are reproducible), and arrives without its models."""
    board, sc, _ = make(tmp_path, microstrip_board(), "m")
    local = simulate(board, sc.config)
    got = remote(server).run("simulate", (board, sc.config, None))
    assert [i.fingerprint for i in got.issues] == [i.fingerprint for i in local.issues]
    assert [r.worst["margin_db"] for r in got.routes] == pytest.approx([r.worst["margin_db"] for r in local.routes])
    assert got.setups == [] and got.model is None


def test_thermal_and_capacity_on_the_server_from_a_server_simulate(server, tmp_path):
    """The plugin's flow with a server: Simulate there, the thermal inputs prepared here from that result (it
    comes without models), the solve there: the same temperatures as a local solve; Current capacity likewise."""
    pytest.importorskip("pyamg")
    from dataclasses import replace
    from boardgen import discrete_buck_board
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.thermal import run_thermal
    from groundtruth.analysis.thermal_proc import capacity_in_process, run_in_process
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "buck", nets=(), solve_points=1)
    spec = replace(detect_converters(board, sc.config)[0].spec, i_out=5.0, v_in=24.0, v_out=5.0, rds_on=0.01)
    cfg = replace(sc.config, converters=[spec], thermal=replace(sc.config.thermal, quality="draft"))
    r = remote(server)
    stages = r.run("simulate", (board, cfg, None)).converters
    here, _ = run_thermal(board, cfg, stages)
    there, issues = run_in_process(board, cfg, stages, server=r)
    assert not there.error and there.t_max == pytest.approx(here.t_max, abs=1e-6)
    views = capacity_in_process(board, cfg, stages, server=r)
    assert views and not views[0].error and views[0].capacity["stage"] == spec.name


def test_suggest_fixes_on_the_server(server, tmp_path):
    """Suggest fixes runs whole on the server (its own baseline) and comes back with its suggestions and the two
    simulations it keeps, without their models."""
    board, sc, _ = make(tmp_path, microstrip_board(slot=(19.5, 4, 20.5, 16)), "slot")
    res = remote(server).run("suggest", (board, sc.config))
    assert res.suggestions and res.before is not None and res.before.setups == []


def test_a_slow_or_gone_server_is_waited_for_and_only_the_user_cancels(server, monkeypatch):
    """A job handed over is never given up for a lost connection (the user: "The system client should not cancel the
    job unless the user cancels it"): unanswered requests are asked again; the plugin's Cancel (raising in
    progress) is what ends the wait, and then the job is cancelled on the server."""
    from groundtruth.remote import client
    r = remote(server)
    real, misses = Remote.request, {"n": 0}

    def flaky(self, method, path, body=b"", timeout=client.CONNECT_TIMEOUT_S):
        if method == "GET" and path.startswith("/v1/jobs/") and misses["n"] < 3:
            misses["n"] += 1
            raise ServerUnavailable("timed out")
        return real(self, method, path, body, timeout)
    monkeypatch.setattr(Remote, "request", flaky)
    monkeypatch.setattr(client, "RETRY_S", 0.01)
    assert r.run("sleep", (0.2,)) == "slept" and misses["n"] == 3

    def dead(self, method, path, body=b"", timeout=client.CONNECT_TIMEOUT_S):
        if method == "GET" and path.startswith("/v1/jobs/"):
            raise ServerUnavailable("timed out")
        return real(self, method, path, body, timeout)
    monkeypatch.setattr(Remote, "request", dead)

    class Cancel(Exception):
        pass
    seen = []

    def progress(msg, frac):
        seen.append(msg)
        if len(seen) > 20:                      # the user gives up, after many retries: only then it ends
            raise Cancel()
    with pytest.raises(Cancel):
        r.run("sleep", (30.0,), progress)
    assert "retrying" in seen[-1] and "Cancel only if you want it stopped" in seen[-1]
    job = max(server.jobs.values(), key=lambda j: j.queued)
    for _ in range(100):
        if job.state == "cancelled":
            break
        time.sleep(0.05)
    assert job.state == "cancelled"


def test_jobs_and_results_survive_a_server_restart_and_go_once_fetched(tmp_path, monkeypatch):
    """Jobs live on disk: a result waits for its owner across a restart (a reboot overnight) and is removed when
    fetched; a job queued at the restart runs after it; a result nobody fetches expires after keep_s."""
    monkeypatch.setenv("GROUNDTRUTH_SERVER_TEST", "1")
    data = tmp_path / "jobs"
    srv = ComputeServer(KEY, "127.0.0.1", 0, data_dir=data)
    srv.start()
    r = remote(srv)
    done, _ = r.submit("sleep", (0.1,))
    while srv.jobs[done].state != "done":
        time.sleep(0.05)
    srv.stop()
    # a second job written while no server ran: as if queued before a restart
    srv2 = ComputeServer(KEY, "127.0.0.1", 0, data_dir=data)
    assert srv2.jobs[done].state == "done" and (data / f"{done}.out").exists()
    queued, _ = srv2.submit("sleep", "tester@here", pickle.dumps((0.1,)))
    srv3 = ComputeServer(KEY, "127.0.0.1", 0, data_dir=data)
    assert srv3.jobs[queued].state == "queued" and queued in srv3.order
    srv3.start()
    try:
        r3 = remote(srv3)
        assert r3.fetch(done) == "slept"
        r3.forget(done)                                           # fetched: removed
        assert done not in srv3.jobs and not (data / f"{done}.out").exists()
        for _ in range(100):
            if srv3.jobs[queued].state == "done":
                break
            time.sleep(0.05)
        assert srv3.jobs[queued].state == "done"
        srv3.keep_s = 0.0
        with srv3.lock:
            srv3._expire()
        assert queued not in srv3.jobs and not list(data.glob(f"{queued}.*"))
    finally:
        srv3.stop()


def test_the_server_settings_file_and_its_overrides(tmp_path):
    """server.json is made with the defaults on the first start; its entries count, an option given overrides."""
    from groundtruth.remote.server import DEFAULTS, settings
    f = tmp_path / "server.json"
    assert settings(f) == {**DEFAULTS, "keep_hours": 24.0}
    assert json.loads(f.read_text(encoding="utf-8"))["workers"] == 1
    f.write_text(json.dumps({"workers": 2, "threads": 8, "keep_hours": 48}), encoding="utf-8")
    got = settings(f, {"workers": None, "gpu": "off"})
    assert got["workers"] == 2 and got["threads"] == "8" and got["keep_hours"] == 48.0 and got["gpu"] == "off"


def test_two_jobs_at_a_time_get_a_gpu_each(tmp_path):
    """With several GPUs, slot k uses GPU k mod their number; one GPU, none or --gpu off: PyTorch's own choice."""
    srv = ComputeServer(KEY, "127.0.0.1", 0, workers=2, gpu="auto", data_dir=tmp_path)
    srv.gpu_count = 2
    assert [srv._gpu_for(k) for k in range(2)] == [0, 1]
    srv.gpu_count = 1
    assert srv._gpu_for(1) is None
    srv.gpu, srv.gpu_count = "off", 2
    assert srv._gpu_for(1) is None


def test_the_plugins_tracker_brings_a_detached_job_back(server, tmp_path):
    """A job handed over and kept in the jobs folder: the tracker follows it, fetches the result into the folder,
    the server forgets it; the result and the inputs load back for showing. A job the server no longer knows is
    'lost'."""
    from groundtruth.remote.jobs import LocalJobs, Tracker
    r = remote(server)
    jobs, changes = LocalJobs(tmp_path / "remote_jobs"), []
    jid, _ = r.submit("sleep", (0.2,))
    jobs.add({"id": jid, "server": f"127.0.0.1:{server.port}", "kind": "sleep", "label": "test"}, {"what": "inputs"})
    t = Tracker(jobs, lambda address: remote(server), changes.append)
    for _ in range(100):
        t.check()
        if jobs.get(jid)["state"] == "ready":
            break
        time.sleep(0.05)
    assert jobs.get(jid)["state"] == "ready" and changes
    assert jobs.load(jid) == ({"what": "inputs"}, "slept")
    assert jid not in server.jobs                                  # removed from the server once fetched
    jobs.add({"id": "0123456789abcdef", "server": f"127.0.0.1:{server.port}", "kind": "sleep", "label": "x"}, {})
    t.check()
    assert jobs.get("0123456789abcdef")["state"] == "lost"


def test_with_gpu_off_the_server_does_not_load_pytorch():
    """--gpu off: no GPU probe (loading PyTorch froze the server); the status says the GPUs are not used."""
    srv = ComputeServer(KEY, "127.0.0.1", 0, gpu="off")
    srv._probe_gpus()
    assert srv.gpus == ["not used (--gpu off)"]


def test_a_running_job_reports_its_memory_and_the_servers_free_memory(server):
    """The user: "report how much memory is in use by the simulation and how much is still available": a running
    job's info has its memory in use (the watchdog's reading of its process tree) and the server's free RAM (and
    commit, on Windows); memory_text says it."""
    pytest.importorskip("psutil")
    import sys
    from groundtruth.remote.client import memory_text
    r = remote(server)
    jid, _ = r.submit("sleep", (4.0,))
    info = None
    for _ in range(200):
        info = r.state(jid)
        if info.get("mem_mb"):
            break
        time.sleep(0.05)
    assert info["state"] == "running" and info["mem_mb"] > 1 and info["server_free_mb"] > 0
    assert (info["commit_mb"] is not None) == (sys.platform == "win32")
    assert "uses" in memory_text(info) and "server free: RAM" in memory_text(info)
    assert memory_text(info, short=True).startswith("uses ") and "free" in memory_text(info, short=True)
    assert memory_text({"peak_mb": 2048}) == "peak 2.0 GB" and memory_text({}) == ""
    r.forget(jid)


def test_the_code_check_ignores_the_window_code(monkeypatch):
    """The window's code (ui/) does not change code_id: a window-only update of the plugin keeps working with the
    server; any other module does."""
    import hashlib
    from pathlib import Path as P
    real = P.read_bytes
    base = protocol.code_id()

    def changed(self, part):
        def read(p):
            data = real(p)
            return data + b"\n# changed" if p.as_posix().endswith(part) else data
        return read
    monkeypatch.setattr(P, "read_bytes", changed(None, "ui/jobs_panel.py"))
    assert protocol.code_id() == base
    monkeypatch.setattr(P, "read_bytes", changed(None, "remote/server.py"))      # a server-side fix alone
    assert protocol.code_id() == base
    monkeypatch.setattr(P, "read_bytes", changed(None, "analysis/simulate.py"))
    assert protocol.code_id() != base


def test_a_job_process_ends_when_its_server_is_gone(tmp_path, monkeypatch):
    """Stopping the service kills the server's process; its job process must not compute on for nobody: the worker
    watches its parent (and grandparent: the venv's python.exe is a launcher) and exits within seconds."""
    psutil = pytest.importorskip("psutil")
    import subprocess
    import sys
    from groundtruth.remote import worker
    monkeypatch.setenv("GROUNDTRUTH_SERVER_TEST", "1")
    inp = tmp_path / "in.pickle"
    inp.write_bytes(pickle.dumps((60.0,)))
    src = str(Path(worker.__file__).resolve().parents[2])
    parent = (f"import subprocess, sys, os\n"
              f"env = dict(os.environ, PYTHONPATH={src!r})\n"
              f"p = subprocess.Popen([sys.executable, '-m', 'groundtruth.remote.worker', {str(inp)!r}, "
              f"{str(tmp_path / 'out.pickle')!r}, 'sleep', '-', '-'], env=env, stdout=subprocess.DEVNULL, "
              f"stderr=subprocess.DEVNULL)\n"
              f"print(p.pid, flush=True)\n"
              f"import time; time.sleep(8)\n")      # the 'server' lives while its job starts, then is gone
    out = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=60)
    launcher = psutil.Process(int(out.stdout.split()[0]))
    tree = [launcher] + launcher.children(recursive=True)
    deadline = time.time() + 30
    while any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in tree) and time.time() < deadline:
        time.sleep(0.5)
    assert not any(p.is_running() for p in tree), "the job process outlived its server"
    assert not (tmp_path / "out.pickle").exists()


def test_no_gpu_comes_with_its_reason(tmp_path, monkeypatch):
    """The user's laptop service said "no GPU found" with nothing to go on: the probe says why (here: the probe's
    Python has no PyTorch, or a CPU-only one), the status carries it, describe shows it."""
    from groundtruth.remote.client import describe
    srv = ComputeServer(KEY, "127.0.0.1", 0, gpu="auto", data_dir=tmp_path)
    srv._probe_gpus()
    if srv.gpus:
        pytest.skip("this machine has a usable GPU")
    assert srv.gpu_note and ("PyTorch" in srv.gpu_note)
    st = srv.status()
    assert st["gpu_note"] == srv.gpu_note and srv.gpu_note in describe(st)


def test_a_mac_server_says_its_cpu_solves(tmp_path, monkeypatch):
    """On a Mac nothing is probed and the reason is the Mac's, not Windows advice about --cuda and a missing DLL (the
    user's M2: "dlls missing on mac?")."""
    from groundtruth.remote import server as server_mod
    from groundtruth.remote.client import describe
    srv = ComputeServer(KEY, "127.0.0.1", 0, gpu="auto", data_dir=tmp_path)
    monkeypatch.setattr(server_mod.sys, "platform", "darwin")
    srv._probe_gpus()
    monkeypatch.undo()
    assert srv.gpus == [] and srv.gpu_count == 0 and srv.gpu_note == server_mod.MAC_GPU_NOTE
    text = describe(srv.status())
    assert "a Mac" in text and "DLL" not in text and "--cuda" not in text


def test_a_second_server_cannot_take_the_same_port(tmp_path):
    """The port is the server's alone: a second server on it fails to start (on Windows, Python's HTTPServer would
    otherwise share it through SO_REUSEADDR and take some of the requests)."""
    a = ComputeServer(KEY, "127.0.0.1", 0, data_dir=tmp_path / "a")
    a.start()
    try:
        b = ComputeServer(KEY, "127.0.0.1", a.port, data_dir=tmp_path / "b")
        with pytest.raises(OSError):
            b.start()
    finally:
        a.stop()


def test_a_job_the_window_waits_for_is_listed_too(server, tmp_path):
    """Thermal, Current capacity, Check in Simulate and Compare what-if keep the window waiting (Remote.run): with
    `watch` (jobs.watcher) they are listed on the Server jobs tab like the handed-over jobs while they run, and end
    as past jobs: shown, failed or cancelled (the user: a Thermal on the server was not shown there). The tracker
    leaves them to the window; one left running by a window closed meanwhile is closed at the next start."""
    from groundtruth.remote.jobs import LocalJobs, Tracker, close_stale, watcher
    jobs = LocalJobs(tmp_path / "jobs")
    seen = []
    base = {"server": f"127.0.0.1:{server.port}", "kind": "thermal", "label": "Thermal", "board_name": "b.kicad_pcb"}
    assert remote(server).run("sleep", (0.6,), watch=watcher(jobs, base, seen.append)) == "slept"
    states = [m["state"] for m in seen]
    assert states[0] == "queued" and "running" in states and states[-1] == "shown"
    m = jobs.all()[0]
    assert m["attached"] and m["label"] == "Thermal" and m["server_name"] and m["run_s"] > 0
    assert not (tmp_path / "jobs" / m["id"] / "input.pickle").exists()        # a past job keeps only its entry
    with pytest.raises(RemoteError):
        remote(server).run("sleep", ("not a number",), watch=watcher(jobs, base, seen.append))
    failed = jobs.get(seen[-1]["id"])
    assert failed["state"] == "failed" and "ValueError" in failed["detail"]

    class Stop(Exception):
        pass

    def progress(msg, frac):
        if frac > 0.2:
            raise Stop()
    with pytest.raises(Stop):
        remote(server).run("sleep", (5.0,), progress, watch=watcher(jobs, base, seen.append))
    assert jobs.get(seen[-1]["id"])["state"] == "cancelled"
    # a running entry the tracker must not touch, until close_stale (a window closed meanwhile)
    jobs.add({**base, "id": "gone", "attached": True, "state": "running"}, None)
    calls = []
    Tracker(jobs, lambda a: calls.append(a), lambda m: None).check()
    assert calls == []
    assert close_stale(jobs) == 1 and jobs.get("gone")["state"] == "cancelled"


def test_a_job_on_a_server_that_is_gone_can_be_removed(tmp_path):
    """The user's job on a server gone for 12 hours stayed in Current jobs: Remove from list only tried to cancel it
    there. stop_on_server says why it could not, the job stays until the user says remove_anyway; a reachable server
    is told, and the job is cancelled or removed."""
    from groundtruth.remote.client import ServerUnavailable
    from groundtruth.remote.jobs import LocalJobs
    from groundtruth.ui.job_stop import remove_anyway, stop_on_server

    class Server:
        """A client.Remote stand-in: reachable or not, remembers what it was told to forget."""
        def __init__(self, up):
            """Reachable when `up`."""
            self.up, self.forgot = up, []

        def forget(self, jid):
            """DELETE the job, or fail as an unreachable server does."""
            if not self.up:
                raise ServerUnavailable("192.0.2.127: can't be reached")
            self.forgot.append(jid)

    jobs = LocalJobs(tmp_path)
    for jid in ("a", "b", "c"):
        jobs.add({"id": jid, "server": "build_pc", "kind": "simulate", "state": "running"}, None)
    why = stop_on_server(jobs, Server(False), "a", remove=True)
    assert why and "can't be reached" in why and jobs.get("a")["state"] == "running"
    remove_anyway(jobs, "a")
    assert jobs.get("a") is None
    up = Server(True)
    assert stop_on_server(jobs, up, "b", remove=True) is None and jobs.get("b") is None
    assert stop_on_server(jobs, up, "c") is None and jobs.get("c")["state"] == "cancelled" and up.forgot == ["b", "c"]
