"""Signed remote updates of the compute servers (remote/release.py, the server's POST /v1/update, the service runner's
update and roll-back, server_setup.py apply-update, make_server_package's signing, the Preferences list's logic).
The user: "a feature that can be enabled when the server is installed for the first time that allows it to update
remotely ... show the user which server is out of date and offers to update it through a button press"; asked:
signed builds only, and a busy server waits for its running jobs; "The signing key that is used to update the
server should be reset if the server is re-installed manually"."""
import io
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from groundtruth.remote import release  # noqa: E402
from groundtruth.remote.client import Remote, RemoteError  # noqa: E402
from groundtruth.remote.server import ComputeServer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
KEY = "test-key-0123456789abcdef"
NEW = "GroundTruth 0.1.99999 (abc1234, built 2026-10-16 10:00)"


def package(version: str = NEW, folder: str = "groundtruth-server-0.1.99999-abc1234") -> bytes:
    """A small server package: version.txt in its top folder (what the server reads of it)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{folder}/version.txt", version + "\n")
        z.writestr(f"{folder}/groundtruth/__init__.py", "")
    return buf.getvalue()


@pytest.fixture
def signer(tmp_path):
    """A release key of the test's own (its private half in tmp_path)."""
    key, made = release.load_private(tmp_path / "release" / "key.pem", create=True)
    assert made
    return key


@pytest.fixture
def home(tmp_path, signer, monkeypatch):
    """A server's install folder that trusts `signer`, as the installed service (GROUNDTRUTH_SERVICE)."""
    h = tmp_path / "home"
    h.mkdir()
    (h / release.PUBLIC_FILE).write_text(release.public_hex(signer) + "\n", encoding="utf-8")
    monkeypatch.setenv("GROUNDTRUTH_SERVICE", "1")
    monkeypatch.setenv("GROUNDTRUTH_SERVER_TEST", "1")
    return h


def started(home: Path, remote_update: bool = True) -> ComputeServer:
    """A running server in `home` (a free port, one job at a time)."""
    srv = ComputeServer(KEY, "127.0.0.1", 0, workers=1, data_dir=home / "server_jobs", remote_update=remote_update,
                        home=home)
    srv.start()
    return srv


def test_signatures_versions_and_paths():
    """A signature fits only its data and its key; versions compare as numbers; a package's version is its
    version.txt; zip paths that leave their folder are caught; a server is same / older / newer than the plugin."""
    a, _ = release.load_private(Path(pytest.importorskip("tempfile").mkdtemp()) / "a.pem", create=True)
    b, _ = release.load_private(Path(pytest.importorskip("tempfile").mkdtemp()) / "b.pem", create=True)
    data = package()
    sig = release.sign(a, data)
    assert release.verify(release.public_hex(a), data, sig)
    assert not release.verify(release.public_hex(b), data, sig)            # another key
    assert not release.verify(release.public_hex(a), data + b"x", sig)     # changed data
    assert not release.verify(release.public_hex(a), data, "zz") and not release.verify("12", data, sig)
    assert release.version_number(NEW) == (0, 1, 99999) and release.version_number("development (source tree)") is None
    assert release.version_number("GroundTruth 0.1.9") < release.version_number("GroundTruth 0.1.10")
    assert release.zip_version(data) == NEW and release.zip_version(b"not a zip") == ""
    assert release.safe_names(["p/groundtruth/x.py", "p/version.txt"])
    assert not release.safe_names(["../evil"]) and not release.safe_names(["C:/x"]) and not release.safe_names(["/x"])
    assert release.compare({"code": "c1"}, NEW, "c1")[0] == "same"
    assert release.compare({"code": "c0", "version": "GroundTruth 0.1.5 (x)"}, NEW, "c1")[0] == "older"
    assert release.compare({"code": "c0", "version": "GroundTruth 0.2.0 (x)"}, NEW, "c1")[0] == "newer"


def test_the_server_takes_only_signed_newer_packages_when_allowed(home, signer, tmp_path, monkeypatch):
    """Remote updates off: refused. On: a package signed with another key is refused (403), one that is not newer
    than the server's build too; a signed newer one is taken, kept in updates/, and with no job running the server
    hands it to its runner (pending.json) and ends with UPDATE_EXIT."""
    import groundtruth
    data = package()
    sig = release.sign(signer, data)
    off = started(home, remote_update=False)
    try:
        with pytest.raises(RemoteError, match="remote updates are off"):
            Remote(f"127.0.0.1:{off.port}", KEY).update(data, sig)
        assert off.status()["remote_update"] is False
    finally:
        off.stop()
    srv = started(home)
    try:
        r = Remote(f"127.0.0.1:{srv.port}", KEY)
        assert r.status()["remote_update"] is True
        other, _ = release.load_private(tmp_path / "other.pem", create=True)
        with pytest.raises(RemoteError, match="not signed with this server's release key"):
            r.update(data, release.sign(other, data))
        monkeypatch.setattr(groundtruth, "build_version", lambda: "GroundTruth 0.1.99999 (abc1234)")
        with pytest.raises(RemoteError, match="not newer"):
            r.update(data, sig)
        monkeypatch.setattr(groundtruth, "build_version", lambda: "GroundTruth 0.1.5 (old)")
        answer = r.update(data, sig)
        assert answer["state"] == "installing" and answer["version"] == NEW
        assert srv.exit_code == release.UPDATE_EXIT
        pending = json.loads((home / "updates" / "pending.json").read_text(encoding="utf-8"))
        assert Path(pending["zip"]).read_bytes() == data and pending["from"] == "GroundTruth 0.1.5 (old)"
        assert srv.status()["update"]["state"] == "installing"
    finally:
        srv.stop()


def test_a_busy_server_finishes_its_running_job_first(home, signer, monkeypatch):
    """An update taken while a job runs waits: the queued job does not start, the running one finishes, then the
    server ends for the update; the queued job is still queued (on disk: it runs after the update)."""
    import groundtruth
    monkeypatch.setattr(groundtruth, "build_version", lambda: "GroundTruth 0.1.5 (old)")
    srv = started(home)
    try:
        r = Remote(f"127.0.0.1:{srv.port}", KEY, client="tester@here")
        running, _ = r.submit("sleep", (1.5,))
        t0 = time.time()
        while r.state(running)["state"] != "running" and time.time() - t0 < 10:
            time.sleep(0.05)
        queued, _ = r.submit("sleep", (0.1,))
        data = package()
        assert r.update(data, release.sign(release.load_private(home.parent / "release" / "key.pem")[0], data)
                        )["state"] == "waiting"
        assert srv.exit_code is None and r.status()["update"]["state"] == "waiting"
        t0 = time.time()
        while srv.exit_code is None and time.time() - t0 < 20:
            time.sleep(0.1)
        assert srv.exit_code == release.UPDATE_EXIT
        assert r.state(running)["state"] == "done" and r.state(queued)["state"] == "queued"
    finally:
        srv.stop()


def staged_package(tmp_path: Path, version: str = NEW, requirements: str = None) -> Path:
    """An unpacked server package as the runner stages it: the real server_setup.py, a small groundtruth package,
    version.txt and requirements.txt."""
    pkg = tmp_path / "pkg" / "groundtruth-server-0.1.99999-abc1234"
    (pkg / "groundtruth" / "remote").mkdir(parents=True)
    (pkg / "groundtruth" / "__init__.py").write_text("NEW = True\n", encoding="utf-8")
    (pkg / "groundtruth" / "remote" / "server.py").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "tools" / "server_setup.py", pkg / "server_setup.py")
    (pkg / "version.txt").write_text(version + "\n", encoding="utf-8")
    (pkg / "requirements.txt").write_text(requirements or (ROOT / "plugin" / "requirements.txt").read_text(
        encoding="utf-8"), encoding="utf-8")
    return pkg


def installed(tmp_path: Path, same_requirements: bool = True) -> dict:
    """An install folder (server_setup.layout) with an old app, its venv's site-packages and its requirements."""
    import server_setup
    root = tmp_path / "root"
    paths = server_setup.layout(root)
    (paths["app"] / "groundtruth").mkdir(parents=True)
    (paths["app"] / "groundtruth" / "__init__.py").write_text("OLD = True\n", encoding="utf-8")
    (paths["app"] / "version.txt").write_text("GroundTruth 0.1.5 (old)\n", encoding="utf-8")
    paths["site"].mkdir(parents=True)
    lines = server_setup.requirements(ROOT / "plugin" / "requirements.txt")
    (root / "requirements-server.txt").write_text("\n".join(lines if same_requirements else lines[:-1]) + "\n",
                                                   encoding="utf-8")
    return paths


def apply(pkg: Path, root: Path) -> int:
    """server_setup.py apply-update from the staged package, as the runner runs it (no admin needed without the task,
    firewall and folder rights)."""
    return subprocess.call([sys.executable, str(pkg / "server_setup.py"), "apply-update", "--dir", str(root),
                            "--from-version", "GroundTruth 0.1.5 (old)", "--no-task", "--no-firewall", "--no-acl"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_apply_update_swaps_the_program_and_keeps_the_old_one(tmp_path):
    """The new package's installer puts its GroundTruth in app (the old one in app.prev), writes a new runner and
    last.json; the libraries are left alone when the requirements did not change."""
    pkg, paths = staged_package(tmp_path), installed(tmp_path)
    assert apply(pkg, paths["root"]) == 0
    assert "NEW" in (paths["app"] / "groundtruth" / "__init__.py").read_text(encoding="utf-8")
    assert "OLD" in (paths["root"] / "app.prev" / "groundtruth" / "__init__.py").read_text(encoding="utf-8")
    assert (paths["app"] / "version.txt").read_text(encoding="utf-8").strip() == NEW
    assert "UPDATE_EXIT = 75" in paths["runner"].read_text(encoding="utf-8")
    last = json.loads((paths["root"] / "updates" / "last.json").read_text(encoding="utf-8"))
    assert last["state"] == "installed" and last["version"] == NEW and last["previous"] == "GroundTruth 0.1.5 (old)"


def test_a_failed_update_leaves_the_old_version(tmp_path):
    """New requirements need pip; with no Python environment there it fails: the old app stays, last.json says
    why."""
    pkg, paths = staged_package(tmp_path), installed(tmp_path, same_requirements=False)
    assert apply(pkg, paths["root"]) != 0
    assert "OLD" in (paths["app"] / "groundtruth" / "__init__.py").read_text(encoding="utf-8")
    assert not (paths["root"] / "app.new").exists()
    last = json.loads((paths["root"] / "updates" / "last.json").read_text(encoding="utf-8"))
    assert last["state"] == "failed" and last["error"]


def runner_calls(paths: dict, call: str) -> str:
    """The service's runner (run_server.py for `paths`) with its last line, main(), replaced by `call`, run in a
    process of its own: what `call` printed to result.txt."""
    import server_setup
    text = server_setup.runner_text(paths)
    assert text.rstrip().endswith("main()")
    out = paths["root"] / "result.txt"
    script = paths["root"] / "runner_test.py"
    script.write_text(text.rstrip()[:-len("main()")] + f"\nopen({str(out)!r}, 'w').write(repr({call}))\n",
                      encoding="utf-8")
    subprocess.run([sys.executable, str(script)], check=True, timeout=120)
    return out.read_text(encoding="utf-8")


def test_the_runner_installs_a_pending_update_and_rolls_back(tmp_path):
    """The runner unpacks the update the server left (pending.json), runs the new package's apply-update; roll_back
    puts the previous version back and records it; a package with a path outside its folder is refused."""
    import server_setup
    paths = installed(tmp_path)
    pkg = staged_package(tmp_path)
    zpath = paths["root"] / "updates" / "update.zip"
    zpath.parent.mkdir(parents=True)
    with zipfile.ZipFile(zpath, "w") as z:
        for f in pkg.rglob("*"):
            if f.is_file():
                z.write(f, f"{pkg.name}/{f.relative_to(pkg).as_posix()}")
    (zpath.parent / "pending.json").write_text(json.dumps({"zip": str(zpath), "version": NEW,
                                                           "from": "GroundTruth 0.1.5 (old)"}), encoding="utf-8")
    assert runner_calls(paths, "apply_update()") == "True"
    assert "NEW" in (paths["app"] / "groundtruth" / "__init__.py").read_text(encoding="utf-8")
    assert not (zpath.parent / "pending.json").exists() and not zpath.exists()
    assert runner_calls(paths, "roll_back('it ended after 3 s')") == "None"
    assert "OLD" in (paths["app"] / "groundtruth" / "__init__.py").read_text(encoding="utf-8")
    last = json.loads((paths["root"] / "updates" / "last.json").read_text(encoding="utf-8"))
    assert last["state"] == "rolled back" and "3 s" in last["error"]
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("../outside.txt", "x")
    (zpath.parent / "pending.json").write_text(json.dumps({"zip": str(zpath), "version": NEW}), encoding="utf-8")
    assert runner_calls(paths, "apply_update()") == "False"
    assert not (paths["root"] / "updates" / "outside.txt").exists() and not (paths["root"] / "outside.txt").exists()
    assert "outside" in json.loads((paths["root"] / "updates" / "last.json").read_text(encoding="utf-8"))["error"]
    assert "UPDATE_EXIT = 75" in server_setup.runner_text(paths)


def test_a_manual_install_resets_the_trusted_release_key(tmp_path, capsys, monkeypatch):
    """Each manual install or update takes the release key of the package installed (the user: "The signing key ...
    should be reset if the server is re-installed manually"); a package without one leaves none; --remote-update
    goes to server.json."""
    import server_setup
    monkeypatch.setitem(server_setup.DRY, "on", False)     # the installer's dry-run tests leave it on
    paths = server_setup.layout(tmp_path / "root")
    paths["root"].mkdir()
    (paths["root"] / server_setup.RELEASE_PUB).write_text("a" * 64 + "\n", encoding="utf-8")
    here = tmp_path / "unpacked"
    here.mkdir()
    monkeypatch.setattr(server_setup, "HERE", here)
    (here / server_setup.RELEASE_PUB).write_text("b" * 64 + "\n", encoding="utf-8")
    server_setup.trust_release_key(paths)
    assert (paths["root"] / server_setup.RELEASE_PUB).read_text(encoding="utf-8").strip() == "b" * 64
    (here / server_setup.RELEASE_PUB).unlink()
    server_setup.trust_release_key(paths)
    assert not (paths["root"] / server_setup.RELEASE_PUB).exists()
    assert "remote updates are refused" in capsys.readouterr().out

    class Args:
        """write_settings' options: only remote updates set."""
        port = workers = threads = gpu = keep_hours = lead = peers = beacon_port = None
        remote_update = "on"
    cfg = server_setup.write_settings(paths, Args)
    assert cfg["remote_update"] is True
    Args.remote_update = None
    assert server_setup.write_settings(paths, Args)["remote_update"] is True        # kept on an update
    block = server_setup.status_block({"remote_update": True, "update": {"state": "installed", "version": NEW,
                                                                        "at": time.time()}}, 8765)
    assert "remote updates on" in block and "updated to" in block


def test_the_server_package_is_signed_with_the_release_key(tmp_path, monkeypatch):
    """make_server_package.build: the zip holds release_key.pub (the build PC's key) and version.txt, and its
    signature checks out with that key."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    import make_server_package
    folder, data, sig = make_server_package.build("0.1.99999", "abc1234", NEW + "\n")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        public = z.read(f"{folder}/{release.PUBLIC_FILE}").decode().strip()
        assert z.read(f"{folder}/version.txt").decode().strip() == NEW and f"{folder}/server_setup.py" in z.namelist()
    assert release.verify(public, data, sig) and release.zip_version(data) == NEW
    assert public == release.public_hex(release.load_private()[0])         # this PC's key, kept for the next build


def test_the_preferences_list_and_an_update_followed(tmp_path):
    """The servers to list (the configured one, its members, the other-version ones), each one's row; an update
    sent and followed until the server runs this plugin's code; a refusal and a roll-back said on its row."""
    from groundtruth.remote.protocol import code_id
    from groundtruth.ui import server_updates as su
    mine = code_id()
    st = {"host": "build_pc", "code": "old", "version": "GroundTruth 0.1.5 (x, built y)", "remote_update": True,
          "cluster": {"members": [{"host": "mac", "address": "10.0.0.3:8765"}],
                      "other_versions": [{"host": "laptop", "address": "10.0.0.2:8765"}]}}
    assert su.server_addresses(st, "build_pc") == [("build_pc", ["build_pc"]), ("mac", ["10.0.0.3:8765"]),
                                                ("laptop", ["10.0.0.2:8765"])]
    row = su.row_of("build_pc", "build_pc", st, "", NEW, mine)
    assert row["state"] == "older" and row["can_update"] and row["version"] == "0.1.5 (x)"
    off = su.row_of("x", "x", {**st, "remote_update": False}, "", NEW, mine)
    assert not off["can_update"] and "remote updates off" in off["text"]
    assert su.row_of("y", "y", None, "timed out", NEW, mine)["state"] == "unreachable"
    bundle = {"zip": tmp_path / "s.zip", "sig": "ab", "version": NEW}
    bundle["zip"].write_bytes(b"zip")

    class Fake:
        """A client whose status answers come from `answers`, one per call."""
        answers, sent = [], []

        def __init__(self, address, key):
            pass

        def status(self):
            a = Fake.answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        def update(self, data, sig):
            Fake.sent.append((data, sig))
            return {"state": "waiting"}
    Fake.answers = [{"update": None}, {"update": {"state": "waiting"}, "running": [1]}, OSError("restarting"),
                    {"code": mine, "version": NEW}]
    steps = []
    done, text = su.update("build_pc", KEY, bundle, steps.append, remote=Fake, sleep=lambda s: None)
    assert done and "updated to 0.1.99999 (abc1234)" in text and Fake.sent == [(b"zip", "ab")]
    assert "waiting for 1 running job(s)" in steps and any("restarts" in s for s in steps)
    Fake.answers = [{"update": {"state": "failed", "at": 1.0}}, {"update": {"state": "failed", "at": 1.0}},
                    {"update": {"state": "rolled back", "at": 2.0, "error": "it ended after 3 s"}}]
    done, text = su.update("build_pc", KEY, bundle, steps.append, remote=Fake, sleep=lambda s: None)
    assert not done and "rolled back" in text and "3 s" in text        # the older failure (at 1.0) was not this one


def test_end_to_end_the_service_updates_itself(tmp_path, monkeypatch):
    """The service's runner (run_server.py) with a real server, on this machine's libraries: an older install (another
    code: one file more) takes a signed newer package built from this tree; the server ends, the runner installs it
    with the package's own installer and starts the new server, which answers with this plugin's code; the old
    program is kept as app.prev."""
    import socket
    import sysconfig
    import psutil
    import make_server_package
    import server_setup
    from groundtruth.remote.protocol import code_id
    from groundtruth.ui import server_updates as su
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "builder"))           # the release key of this test
    root = tmp_path / "srv"
    paths = server_setup.layout(root)
    shutil.copytree(ROOT / "src" / "groundtruth", paths["app"] / "groundtruth",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (paths["app"] / "groundtruth" / "_older.py").write_text('"""An older build."""\n', encoding="utf-8")
    (paths["app"] / "version.txt").write_text("GroundTruth 0.1.5 (old)\n", encoding="utf-8")
    paths["site"].mkdir(parents=True)                       # where apply-update links the program (.pth)
    folder, data, sig = make_server_package.build("0.1.99999", "e2e", NEW + "\n", working_tree=True)
    (root / release.PUBLIC_FILE).write_text(release.public_hex(release.load_private()[0]) + "\n", encoding="utf-8")
    (root / "server.key").write_text(KEY + "\n", encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (root / "server.json").write_text(json.dumps({"port": port, "beacon_port": 0, "remote_update": True,
                                                  "workers": 1}), encoding="utf-8")
    (root / "requirements-server.txt").write_text(
        "\n".join(server_setup.requirements(ROOT / "plugin" / "requirements.txt")) + "\n", encoding="utf-8")
    # the runner on this machine's libraries (the installed service has its own venv)
    runner = server_setup.runner_text({**paths, "site": Path(sysconfig.get_paths()["purelib"])})
    paths["runner"].write_text(runner, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(paths["runner"])], cwd=str(root))
    address = f"127.0.0.1:{port}"
    try:
        t0, st = time.time(), None
        while time.time() - t0 < 90 and st is None:
            try:
                st = Remote(address, KEY).status()
            except Exception:                               # noqa: BLE001 - still starting
                time.sleep(0.5)
        assert st is not None, (root / "service.log").read_text(encoding="utf-8")
        assert st["version"] == "GroundTruth 0.1.5 (old)" and st["code"] != code_id() and st["remote_update"]
        zpath = tmp_path / "server_update.zip"
        zpath.write_bytes(data)
        done, text = su.update(address, KEY, {"zip": zpath, "sig": sig, "version": NEW}, lambda t: None)
        log = (root / "service.log").read_text(encoding="utf-8")
        assert done, (text, log)
        st = Remote(address, KEY).status()
        assert st["version"] == NEW and st["code"] == code_id() and st["update"]["state"] == "installed"
        assert (root / "app.prev" / "groundtruth" / "_older.py").exists()
        assert not (paths["app"] / "groundtruth" / "_older.py").exists()
        assert "installing the update to" in log
        # a newer, signed package whose server fails at its start: the runner goes back to the version before it
        broken_version = "GroundTruth 0.1.100000 (broken, built 2026-10-16 11:00)"
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as dst:
            for item in src.infolist():
                body = src.read(item)
                if item.filename == f"{folder}/version.txt":
                    body = (broken_version + "\n").encode()
                if item.filename == f"{folder}/groundtruth/remote/server.py":
                    body = b"raise RuntimeError('this build does not start')\n" + body
                dst.writestr(item, body)
        broken = buf.getvalue()
        assert Remote(address, KEY).update(broken, release.sign(release.load_private()[0], broken))["version"] == \
            broken_version
        t0, up = time.time(), {}
        while time.time() - t0 < 120 and up.get("state") != "rolled back":
            time.sleep(1.0)
            try:
                st = Remote(address, KEY).status()
                up = st.get("update") or {}
            except Exception:                               # noqa: BLE001 - restarting
                pass
        log = (root / "service.log").read_text(encoding="utf-8")
        assert up.get("state") == "rolled back", log
        assert st["version"] == NEW and st["code"] == code_id() and "back to the previous version" in log
    finally:
        for child in psutil.Process(proc.pid).children(recursive=True):
            child.kill()
        proc.kill()
        proc.wait(timeout=30)


def test_a_server_with_two_networks_is_found_at_the_address_that_answers(monkeypatch, tmp_path, capsys):
    """The user: build_pc (198.51.100.6) in the cluster of server-pc (203.0.113.128) was not listed in Preferences,
    and "can we start a server with a known address ... and then get presented the available options?". A server
    can announce one chosen address (server.json "address", install --address: picked from this PC's network cards
    or checked against them); the beacons also go to each own subnet's broadcast (Windows sends 255.255.255.255 out
    of one card only); the status shows the addresses; Preferences tries every address a server announces, and adds
    the servers a network query heard that the lead does not name."""
    import server_setup
    from groundtruth.remote.cluster import Cluster, subnet_broadcasts
    from groundtruth.ui import server_updates as su
    # beacons: the chosen address's subnet broadcast (or each own address's)
    c = Cluster(KEY, 8765, "x", lambda: {}, address="198.51.100.6")
    assert ("198.51.100.255", c.beacon_port) in c.targets() and ("255.255.255.255", c.beacon_port) in c.targets()
    assert subnet_broadcasts(["10.1.2.3", "bad"]) and all(b.endswith(".255") for b in subnet_broadcasts(["10.1.2.3"]))
    # the server announces the chosen address alone, its status lists it first
    srv = ComputeServer(KEY, "127.0.0.1", 0, data_dir=tmp_path / "jobs", home=tmp_path, address="198.51.100.6")
    assert srv.addresses(announced=True) == ["198.51.100.6"] and srv.addresses()[0] == "198.51.100.6"
    st = srv.status()
    assert st["address"] == "198.51.100.6" and st["addresses"][0] == "198.51.100.6"
    block = server_setup.status_block({**st, "host": "build_pc"}, 8765)
    assert "build_pc (198.51.100.6" in block and "announces 198.51.100.6" in block
    # install --address: listed and picked, given and checked, or automatic again
    paths = server_setup.layout(tmp_path / "inst")
    monkeypatch.setitem(server_setup.DRY, "on", False)
    monkeypatch.setattr(server_setup, "local_adapters", lambda p: [("Ethernet", "198.51.100.6"),
                                                                   ("Wi-Fi", "203.0.113.7")])

    class Args:
        """--address as given."""
        address = server_setup.ASK
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    server_setup.ask_address(Args, paths)
    assert Args.address == "203.0.113.7" and "(Wi-Fi)" in capsys.readouterr().out
    Args.address = "10.9.9.9"
    with pytest.raises(SystemExit, match="not an address of this PC"):
        server_setup.ask_address(Args, paths)
    Args.address = "auto"
    server_setup.ask_address(Args, paths)
    assert Args.address == ""
    # Preferences: a member reached at the second address it announces; a server only the query heard is added
    lead = {"host": "server-pc", "code": "c", "version": NEW, "remote_update": True,
            "cluster": {"members": [{"host": "build_pc", "address": "203.0.113.7:8765",
                                     "addresses": ["203.0.113.7", "198.51.100.6"]}]}}
    heard = [{"host": "mac", "address": "10.0.0.3", "port": 8765, "info": {"addresses": ["10.0.0.3"]}},
             {"host": "build_pc", "address": "198.51.100.6", "port": 8765}]

    class Fake:
        """A client answering the status of whatever address it was made for."""
        def __init__(self, address, key):
            self.address = address

        def status(self):
            return lead if self.address == "203.0.113.128" else {"host": self.address, "code": "c",
                                                                   "version": NEW}
    rows = su.check("203.0.113.128", KEY, remote=Fake, find=lambda key: heard,
                    reach=lambda hosts, port: "198.51.100.6" if "198.51.100.6" in hosts else None)
    assert [(r["name"], r["address"]) for r in rows] == [("server-pc", "203.0.113.128"),
                                                         ("198.51.100.6:8765", "198.51.100.6:8765"),
                                                         ("mac", "10.0.0.3:8765")]
    assert rows[2]["state"] == "unreachable" and "not at 10.0.0.3" in rows[2]["text"]


def test_an_older_build_with_the_same_code_is_out_of_date_and_updated_to_the_build(tmp_path):
    """The user: both servers "up to date" on 0.1.41500 next to a 0.1.41521 plugin ("should the server say outdated
    given that the plugin is newer?"). Same code, earlier build than the package the plugin carries: older (it takes
    the jobs, the row says so) and offered for Update; the same build: up to date. The update is done when the server
    runs the package's build, not as soon as it answers with the same code (it did before the update too)."""
    from groundtruth.remote.protocol import code_id
    from groundtruth.ui import server_updates as su
    mine = code_id()
    st = {"host": "mac", "code": mine, "version": "GroundTruth 0.1.41500 (9724629, built x)", "remote_update": True}
    row = su.row_of("mac", "mac", st, "", "GroundTruth 0.1.41521 (ad230a9, built y)", mine,
                    "GroundTruth 0.1.41521 (ad230a9, built y)")
    assert row["state"] == "older" and row["can_update"] and "takes this plugin's jobs" in row["text"]
    same = su.row_of("mac", "mac", {**st, "version": "GroundTruth 0.1.41521 (ad230a9, built y)"}, "",
                     "GroundTruth 0.1.41521 (ad230a9, built y)", mine, "GroundTruth 0.1.41521 (ad230a9, built y)")
    assert same["state"] == "same" and same["text"] == "up to date"
    bundle = {"zip": tmp_path / "s.zip", "sig": "ab", "version": "GroundTruth 0.1.41521 (ad230a9, built y)"}
    bundle["zip"].write_bytes(b"zip")

    class Fake:
        """A client whose status answers come from `answers`, one per call."""
        answers = [{"update": None}, {**st, "update": {"state": "installing"}}, {**st},
                   {**st, "version": "GroundTruth 0.1.41521 (ad230a9, built y)"}]

        def __init__(self, address, key):
            pass

        def status(self):
            return Fake.answers.pop(0)

        def update(self, data, sig):
            return {"state": "installing"}
    done, text = su.update("mac", KEY, bundle, lambda s: None, remote=Fake, sleep=lambda s: None)
    assert done and "0.1.41521" in text and Fake.answers == []
