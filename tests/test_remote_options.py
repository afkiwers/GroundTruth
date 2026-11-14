"""Options a compute server installs when the plugin asks (remote/options.py, the server's POST /v1/options). The
user: "currently the user must install gpu packages etc manually and have access to the server. Make available
options installable remotely". Only names of the server's fixed list are taken, their commands are the install
tools' own, and no job runs while pip changes the environment."""
import sys
import time
from pathlib import Path

import pytest

from groundtruth.remote import options
from groundtruth.remote.client import Remote, RemoteError
from groundtruth.remote.server import ComputeServer

KEY = "test-key-0123456789abcdef"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A server's install folder, as the installed service."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("GROUNDTRUTH_SERVICE", "1")
    monkeypatch.setenv("GROUNDTRUTH_SERVER_TEST", "1")
    return h


def started(home: Path) -> ComputeServer:
    """A running server in `home` (a free port, one job at a time)."""
    srv = ComputeServer(KEY, "127.0.0.1", 0, workers=1, data_dir=home / "server_jobs", home=home)
    srv.start()
    return srv


def test_the_options_commands_are_the_install_tools_own():
    """The table's pip arguments equal what tools/install_gpu.py and tools/install_sparse.py run by hand, from the
    same package indexes; every chip the tool knows has its option."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import install_gpu
    import install_sparse
    py = "PY"
    for build in options.CUDA_BUILDS:
        assert options.command(f"gpu-{build}", "install", py)[4:] == install_gpu.install_command(Path(py), cuda=build)[3:]
    for gfx in sorted({g for _, g in install_gpu.AMD_CHIPS}):
        want = install_gpu.install_command(Path(py), amd=gfx)[3:]
        assert options.command(f"gpu-amd-{gfx}", "install", py)[4:] == want
    assert options.AMD_TORCH == install_gpu.AMD_TORCH and options.AMD_INDEX == install_gpu.AMD_INDEX
    assert options.command("sparse-mkl", "install", py)[4:] == ["install", "--upgrade", install_sparse.PACKAGE]
    assert options.command("sparse-mkl", "remove", py)[4:7] == ["uninstall", "-y", "mkl"]
    assert options.command("gpu-cu126", "install", py)[:4] == ["PY", "-m", "pip", "--disable-pip-version-check"]


@pytest.mark.parametrize("name, action", [("torch", "install"), ("gpu-cu126; rm -rf /", "install"),
                                          ("gpu-cu126", "upgrade"), ("https://example.invalid/x.whl", "install"),
                                          ("", ""), ("gpu-cu999", "install"), ("sparse-mkl", "--index-url")])
def test_only_the_tables_names_and_actions_make_a_command(name, action):
    """Whatever is asked for that is not a key of the table (a package name, an address, shell text) or not
    install / remove makes no command at all."""
    with pytest.raises(ValueError):
        options.command(name, action)


def test_the_server_installs_a_named_option_between_jobs_and_refuses_everything_else(home, monkeypatch):
    """POST /v1/options: an unknown name or action is refused (400) and nothing runs; a known option waits for the
    running job, runs with no job beside it (the queued job starts only after), and the status says how it went;
    a second request meanwhile is refused; a failing pip is reported."""
    ran = []

    def fake_run(name, action, log_file, python=None):
        """In place of pip: note what the server had running, take a moment."""
        ran.append((name, action, [j.state for j in srv.jobs.values()]))
        time.sleep(0.5)
        return 0 if name != "gpu-cu118" else 3
    monkeypatch.setattr(options, "run", fake_run)
    monkeypatch.setattr(options, "unsuitable", lambda name: "")
    monkeypatch.setattr(ComputeServer, "_probe_gpus", lambda self: None)
    srv = started(home)
    try:
        r = Remote(f"127.0.0.1:{srv.port}", KEY, client="tester@here")
        for name, action in (("torch==1.0", "install"), ("sparse-mkl", "run"), ("../x", "remove")):
            with pytest.raises(RemoteError, match="unknown option or action"):
                r.option(name, action)
        assert ran == [] and r.status()["options"]["current"] is None
        assert {o["name"] for o in r.status()["options"]["available"]} == set(options.OPTIONS)
        running, _ = r.submit("sleep", (1.5,))
        t0 = time.time()
        while r.state(running)["state"] != "running" and time.time() - t0 < 10:
            time.sleep(0.05)
        answer = r.option("sparse-mkl", "install")
        assert answer["state"] == "waiting" and answer["name"] == "sparse-mkl"
        queued, _ = r.submit("sleep", (0.1,))
        with pytest.raises(RemoteError, match="under way"):
            r.option("gpu-cu126", "install")
        t0 = time.time()
        while (r.status()["options"]["current"] or {}).get("state") != "done" and time.time() - t0 < 20:
            time.sleep(0.1)
        assert r.status()["options"]["current"]["state"] == "done"
        # pip ran with no job running: the first had finished, the queued one had not started
        assert ran == [("sparse-mkl", "install", ["done", "queued"])]
        t0 = time.time()
        while r.state(queued)["state"] != "done" and time.time() - t0 < 20:
            time.sleep(0.1)
        assert r.state(queued)["state"] == "done"                       # the queue goes on after it
        r.option("gpu-cu118", "install")
        t0 = time.time()
        while (r.status()["options"]["current"] or {}).get("state") not in ("done", "failed") and time.time() - t0 < 20:
            time.sleep(0.1)
        cur = r.status()["options"]["current"]
        assert cur["state"] == "failed" and "code 3" in cur["error"]
        # an option that doesn't suit this machine: not installed, but removed (a GPU build left on a Mac)
        monkeypatch.setattr(options, "unsuitable", lambda name: "no GPU build is used on a Mac")
        with pytest.raises(RemoteError, match="no GPU build"):
            r.option("gpu-cu126", "install")
        assert r.option("gpu-cu126", "remove")["action"] == "remove"
        t0 = time.time()
        while (r.status()["options"]["current"] or {}).get("state") != "done" and time.time() - t0 < 20:
            time.sleep(0.1)
        assert ran[-1][:2] == ("gpu-cu126", "remove")
    finally:
        srv.stop()


def test_a_server_in_a_window_takes_no_option(tmp_path, monkeypatch):
    """Not the installed service: refused, and the status lists nothing to install."""
    monkeypatch.delenv("GROUNDTRUTH_SERVICE", raising=False)
    monkeypatch.setattr(ComputeServer, "_probe_gpus", lambda self: None)
    h = tmp_path / "home"
    h.mkdir()
    srv = ComputeServer(KEY, "127.0.0.1", 0, workers=1, data_dir=h / "server_jobs", home=h)
    srv.start()
    try:
        r = Remote(f"127.0.0.1:{srv.port}", KEY, client="tester@here")
        assert r.status()["options"]["available"] == []
        with pytest.raises(RemoteError, match="not as the installed service"):
            r.option("sparse-mkl", "install")
    finally:
        srv.stop()


def test_the_requests_need_the_servers_key(home, monkeypatch):
    """Signed like every request: another key gets nothing done."""
    from groundtruth.remote.client import ServerUnavailable
    monkeypatch.setattr(options, "run", lambda *a, **k: pytest.fail("ran without the key"))
    monkeypatch.setattr(ComputeServer, "_probe_gpus", lambda self: None)
    srv = started(home)
    try:
        with pytest.raises((RemoteError, ServerUnavailable)):
            Remote(f"127.0.0.1:{srv.port}", "another-key-0123456789abcdef", client="x").option("sparse-mkl", "install")
        assert srv.option is None
    finally:
        srv.stop()


def test_the_plugin_offers_what_the_server_lists_and_follows_it():
    """Options… (ui/server_updates.py, no wx): installs of the options that suit the server, a removal for a package
    that is there (once), nothing for a server that lists none; following an install tells each step and ends with
    what the server says."""
    from groundtruth.ui import server_updates as su
    status = {"host": "server-pc", "options": {
        "available": [{"name": "gpu-cu126", "label": "GPU cu126", "about": "2.5 GB", "group": "gpu", "package": "torch",
                       "unsuitable": ""},
                      {"name": "gpu-cu128", "label": "GPU cu128", "about": "2.5 GB", "group": "gpu", "package": "torch",
                       "unsuitable": ""},
                      {"name": "sparse-mkl", "label": "MKL", "about": "150 MB", "group": "sparse", "package": "mkl",
                       "unsuitable": "x86 only"}],
        "installed": {"torch": "2.6.0+cu126", "mkl": None}, "current": None}}
    got = su.option_choices(status)
    # what is installed comes first, to remove (the user: "show what options have been installed already, and
    # offer to uninstall them"), then the installs that suit the machine
    assert [(c["name"], c["action"]) for c in got] == [("gpu-cu126", "remove"), ("gpu-cu126", "install"),
                                                       ("gpu-cu128", "install")]
    assert "PyTorch 2.6.0+cu126 (NVIDIA GPU, CUDA build cu126)" in got[0]["text"] and "server-pc" in got[0]["confirm"]
    assert "replaces the installed torch 2.6.0+cu126" in got[1]["text"]
    assert su.installed_lines(status) == ["Intel MKL (PARDISO sparse solver): not installed",
                                          "PyTorch 2.6.0+cu126 (NVIDIA GPU, CUDA build cu126): installed"]
    assert su.installed_lines({"host": "old"}) == []
    assert "CPU only" in su.package_label("torch", "2.6.0") and "AMD" in su.package_label("torch", "2.12.0+rocm7.14")
    # MKL there: removed, not offered again; also a package no option suits any more (a GPU build on a Mac)
    status["options"]["installed"]["mkl"] = "2025.1.0"
    got = su.option_choices(status)
    assert ("sparse-mkl", "remove") in [(c["name"], c["action"]) for c in got]
    assert ("sparse-mkl", "install") not in [(c["name"], c["action"]) for c in got]
    assert su.option_choices({"host": "old"}) == [] and su.option_choices({"options": {"available": []}}) == []
    # why nothing is offered: an old build, a server in a window, or a machine none suits (a Mac: up to date, but
    # no GPU build and no MKL there; the user was told "older version" for it)
    assert "older version" in su.no_options_reason({"host": "old"})
    assert "in a window" in su.no_options_reason({"options": {"available": [], "installed": {}}})
    mac = {"options": {"available": [
        {"name": "gpu-cu126", "unsuitable": "no GPU build is used on a Mac"},
        {"name": "gpu-cu128", "unsuitable": "no GPU build is used on a Mac"},
        {"name": "sparse-mkl", "unsuitable": "Intel MKL runs on x86 only, not on this arm64 machine"}],
        "installed": {"torch": None, "mkl": None}}}
    assert su.option_choices(mac) == []
    why = su.no_options_reason(mac)
    assert "none of them suits" in why and why.count("no GPU build") == 1 and "arm64" in why
    assert "older" not in why and "window" not in why
    mac["options"]["installed"]["torch"] = "2.6.0"                 # a PyTorch there after all: it can go
    for o in mac["options"]["available"]:
        o.update(package="mkl" if o["name"] == "sparse-mkl" else "torch", group="gpu", label="x", about="y")
    assert [(c["name"], c["action"]) for c in su.option_choices(mac)] == [("gpu-cu126", "remove")]

    class Fake:
        """A client whose status answers come from `answers`."""
        answers, asked = [], []

        def __init__(self, address, key):
            pass

        def option(self, name, action):
            Fake.asked.append((name, action))
            return {"name": name, "action": action, "state": "waiting", "since": 5.0}

        def status(self):
            a = Fake.answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return {"options": {"current": a}}
    cur = {"name": "sparse-mkl", "since": 5.0}
    Fake.answers = [{**cur, "state": "waiting"}, OSError("busy"), {"name": "gpu-cu126", "since": 1.0, "state": "done"},
                    {**cur, "state": "installing"}, {**cur, "state": "done"}]
    steps = []
    done, text = su.follow_option("server-pc", KEY, "sparse-mkl", "install", steps.append, remote=Fake,
                                  sleep=lambda s: None)
    assert done and text == "sparse-mkl installed" and Fake.asked == [("sparse-mkl", "install")]
    assert any("after the running job" in s for s in steps) and any("installing sparse-mkl" in s for s in steps)
    Fake.answers = [{**cur, "state": "failed", "error": "pip ended with code 1: see options.log at the server"}]
    done, text = su.follow_option("server-pc", KEY, "sparse-mkl", "install", steps.append, remote=Fake,
                                  sleep=lambda s: None)
    assert not done and "pip ended with code 1" in text
