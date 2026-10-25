"""Install the GroundTruth compute server on a Windows PC as a background service (the user, 2026-10-14: "make a
service and installer"; the server so far ran in a console window that had to stay open and logged in).

    python server_setup.py install [--workers N] [--threads N] [--gpu off|auto] [--cuda cu126] [--port 8765]
                                   [--key] [--lead | --no-lead] [--peers HOST,...] [--beacon-port 8766]
                                         (a cluster: --key alone asks for the key of the cluster's servers)
    python server_setup.py update        the GroundTruth next to this file installed over the old one, restarted
    python server_setup.py status        the service's state and the server's own status
    python server_setup.py key           the access key (to give to the users)
    python server_setup.py start | stop | uninstall [--purge]
    python server_setup.py install --remote-update on|off   signed remote updates from the users' plugins (asked on a
                                         first install; remote/release.py); every manual install resets the trusted
                                         release key to the installed package's
    python server_setup.py install --address [IP|auto]   the address this server announces (alone: this PC's
                                         network cards listed to pick one)
    python server_setup.py apply-update  (run by the service's runner from an unpacked remote update: the package
                                         next to this file over the install, the old one kept as app.prev)

Run it **as administrator** with KiCad's Python (install_server.cmd does both): it installs into
C:\\ProgramData\\GroundTruth (`--dir`), independent of any user profile:

- `app\\groundtruth`: a copy of the GroundTruth package next to this file (the server package's own copy);
- `venv`: a Python environment made from KiCad's Python with the server's libraries (numpy, scipy, shapely, numba,
  pyamg, psutil ...; PyTorch with --gpu), installed from PyPI;
- `run_server.py`: the service's entry: it runs `groundtruth serve` with KiCad's Python and the venv's libraries,
  logs to service.log and server.log, and starts the server again should it ever stop;
- the server's own files: server.key (the access key), server.json (its settings: workers, threads, gpu,
  keep_hours, port; the cluster's beacon_port, lead, peers), server_jobs\\ (the jobs, kept across restarts).

Several servers with the same access key form a **cluster** (remote/cluster.py): they find each other on the local
network (UDP 8766) and elect a lead. The first server makes the key; every further one is installed with `--key`
(asked for) so it joins.

The service is a Windows **scheduled task** "GroundTruth compute server": started at boot as the SYSTEM account
(nobody needs to log in), no time limit, restarted if it fails; no extra software (a real Windows service would
need pywin32's service host, which does not run reliably from a venv). The folder is readable by administrators and
SYSTEM only (the access key is a secret). The firewall gets inbound rules for the TCP port and the UDP beacon port
on the private and domain networks (never public).

Everything Windows-specific (schtasks, netsh, icacls) runs through `run()`, so `--dry-run` prints the plan and the
tests check it without changing the machine.
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
MIN_PYTHON = (3, 11)            # pyproject's requires-python: the analysis uses itertools.pairwise, tomllib, ...
TASK = "GroundTruth compute server"
RULE = "GroundTruth compute server"
DEFAULT_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "GroundTruth"
# macOS (the user: "the server should support macs", an Apple M2): a launchd daemon instead of the scheduled task
MAC_DIR = Path("/Library/Application Support/GroundTruth")
LABEL = "com.groundtruth.server"                         # the launchd daemon's label
PLIST = Path("/Library/LaunchDaemons") / f"{LABEL}.plist"
SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"
PLATFORM = {"name": "mac" if sys.platform == "darwin" else "windows"}   # --platform (hidden) sets it for dry runs


def mac() -> bool:
    """The install is for macOS (else Windows)."""
    return PLATFORM["name"] == "mac"


def default_dir() -> Path:
    """The install folder: C:\\ProgramData\\GroundTruth, or /Library/Application Support/GroundTruth on a Mac."""
    return MAC_DIR if mac() else DEFAULT_DIR


def launcher() -> str:
    """The double-click installer's name on this platform (named in the messages)."""
    return "install_server.command" if mac() else "install_server.cmd"


def at_boot() -> str:
    """How the service starts, in words."""
    return "starts at boot" if mac() else "starts with Windows"
SKIP_REQUIREMENTS = ("kicad-python", "wxpython")         # the server needs no KiCad API and no window
SID_SYSTEM, SID_ADMINS = "*S-1-5-18", "*S-1-5-32-544"     # locale-independent account names for icacls
DRY = {"on": False}
ASK = "<ask>"                                            # --key without a value: asked for (shown as typed or pasted)


def say(text: str = ""):
    """Print at once (the installer's output is read as it goes)."""
    print(text, flush=True)


HEARTBEAT_S = 15.0              # a long step says it is still working when nothing was printed for this long


def run(cmd: list[str], check: bool = True, quiet: bool = False) -> int:
    """Run a command (or only print it with --dry-run); its exit code. Its output is relayed through a pipe, as it
    comes (the venv's python.exe is a launcher: what pip prints straight into the console is lost, and the install
    window sat silent for minutes); without output for HEARTBEAT_S a line says it is still working and for how long.
    `quiet`: the output is kept back and shown only when the command fails."""
    if DRY["on"]:
        say("  would run: " + subprocess.list2cmdline(cmd))
        return 0
    import threading
    t0 = time.monotonic()
    last = [t0]
    kept: list[bytes] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def relay():
        """Pass the output on (raw chunks: pip's lines as they come), or keep it back when quiet."""
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                return
            last[0] = time.monotonic()
            if quiet:
                kept.append(chunk)
            else:
                text = chunk.decode("utf-8", "replace").replace("\r\n", "\n")
                sys.stdout.write("".join("    " + line if line.strip() else line
                                         for line in text.splitlines(keepends=True)))
                sys.stdout.flush()
    reader = threading.Thread(target=relay, daemon=True)
    reader.start()
    while proc.poll() is None:
        time.sleep(0.5)
        if not quiet and time.monotonic() - last[0] > HEARTBEAT_S:
            say(f"    ... still working ({clock(time.monotonic() - t0)} so far)")
            last[0] = time.monotonic()
    reader.join(timeout=5)
    if check and proc.returncode != 0:
        raise SystemExit(f"failed (exit code {proc.returncode}): {subprocess.list2cmdline(cmd)}"
                         + ("\n" + b"".join(kept).decode("utf-8", "replace") if quiet else ""))
    return proc.returncode


def clock(seconds: float) -> str:
    """'2:05' or '1:02:05'."""
    s = int(seconds)
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def span(seconds: float) -> str:
    """A duration in words: '45 s', '12 min', '2 h 13 min', '3 d 4 h' (as remote/cluster.span; this file runs
    before GroundTruth is importable)."""
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


class Steps:
    """Numbered steps: '[3/8] Installing the libraries ...' with what each does, and its time when done."""

    def __init__(self, titles: list[str]):
        """The steps to come."""
        self.titles, self.k, self.t0, self.start = titles, 0, time.monotonic(), None

    def next(self, what: str = ""):
        """Begin the next step (and close the one before)."""
        self.done()
        self.k += 1
        self.start = time.monotonic()
        say(f"\n[{self.k}/{len(self.titles)}] {self.titles[self.k - 1]}" + (f"\n    {what}" if what else ""))

    def done(self):
        """The step before took this long (said only when it took a while)."""
        if self.start is not None and time.monotonic() - self.start >= 5:
            say(f"    done in {clock(time.monotonic() - self.start)}")
        self.start = None


def is_admin() -> bool:
    """Running elevated (as administrator; on a Mac as root, through sudo)."""
    if mac():
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                    # noqa: BLE001 - not Windows
        return False


def python_version(python: Path) -> tuple | None:
    """`python`'s (major, minor), asked by running it; None when it does not run."""
    try:
        out = subprocess.run([str(python), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                             capture_output=True, text=True, timeout=60).stdout.split()
        return int(out[0]), int(out[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def too_old_text(found: str) -> str:
    """Why a Python can't run the server and what to do (KiCad for macOS bundles Python 3.9: the user's M2,
    2026-10-15, whose first job failed on `itertools.pairwise`)."""
    need = ".".join(map(str, MIN_PYTHON))
    if mac():
        return (f"GroundTruth needs Python {need} or newer; found: {found}. KiCad for macOS brings an older one.\n"
                "Install Python 3.13 from https://www.python.org/downloads/macos/ (the macOS 64-bit universal2 "
                "installer), then run install_server.command again: it finds it and makes the server's environment "
                "anew.")
    return f"GroundTruth needs Python {need} or newer; found: {found}. Give --python with a newer one."


def mac_pythons() -> list:
    """The Pythons a Mac may have, in the order they are preferred: KiCad.app's, python.org's, Homebrew's, the
    PATH's."""
    found = [Path(p) for pattern in (
        "/Applications/KiCad*/KiCad.app/Contents/Frameworks/Python.framework/Versions/*/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.*/bin/python3",       # python.org's installer
        "/opt/homebrew/bin/python3", "/usr/local/bin/python3") for p in sorted(glob.glob(pattern), reverse=True)]
    which = shutil.which("python3")
    return found + ([Path(which)] if which else [])


def kicad_python() -> Path:
    """KiCad's own Python (the newest KiCad 10+ installed), which the server runs on; on a Mac the first Python of
    MIN_PYTHON or newer among KiCad.app's (a framework build; KiCad 10's is 3.9, too old), python.org's, Homebrew's
    and the PATH's."""
    if mac():
        tried = []
        for python in mac_pythons():
            version = python_version(python)
            if version and version >= MIN_PYTHON:
                return python
            tried.append(f"{python} ({'.'.join(map(str, version)) if version else 'does not run'})")
        raise SystemExit(too_old_text(", ".join(tried) or "no python3"))
    found = []
    for exe in glob.glob(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "KiCad", "*", "bin",
                                      "python.exe")):
        try:
            major = int(Path(exe).parents[1].name.split(".")[0])
        except ValueError:
            continue
        if major >= 10:
            found.append((major, Path(exe)))
    if not found:
        raise SystemExit("KiCad 10 or later was not found under Program Files: install KiCad, or give --python")
    return max(found)[1]


def layout(root: Path) -> dict:
    """The install's folders and files (on a Mac the venv's site-packages is found once the venv exists: venv_site)."""
    site = root / "venv" / "Lib" / "site-packages"
    if mac():
        found = sorted(glob.glob(str(root / "venv" / "lib" / "python3*" / "site-packages")))
        site = Path(found[-1]) if found else root / "venv" / "lib" / "python3" / "site-packages"
    return {"root": root, "app": root / "app", "venv": root / "venv", "site": site,
            "runner": root / "run_server.py", "log": root / "server.log", "service_log": root / "service.log",
            "settings": root / "server.json", "key": root / "server.key"}


def venv_python(paths: dict) -> Path:
    """The venv's Python, which the service runs on (venv\\Scripts\\python.exe; venv/bin/python3 on a Mac)."""
    return paths["venv"] / ("bin/python3" if mac() else "Scripts/python.exe")


def requirements(source: Path) -> list[str]:
    """The server's libraries: the plugin's requirements.txt without the KiCad API and the window toolkit."""
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line and not line.lower().startswith(SKIP_REQUIREMENTS):
            lines.append(line)
    return lines + [x for x in SERVER_EXTRA if x not in lines]


RUNNER = '''"""The GroundTruth compute server's service entry (written by server_setup.py; run by the service at boot:
the scheduled task "{task}" as SYSTEM on Windows, the launchd daemon on a Mac). It runs `groundtruth serve` in a
process of its own and starts it again whenever it stops. When the server ends for a remote update (exit code 75,
remote/release.py) it installs the update with the new package's own installer (server_setup.py apply-update) and
starts the new server; should that not stay up for HEALTH_S, the previous version comes back."""
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile

HOME = {home!r}
APP = {app!r}
SITE = {site!r}
UPDATE_EXIT = 75                  # the server's exit code when an update waits (remote/release.py)
HEALTH_S = 120                    # an updated server that ends sooner than this is rolled back
os.environ["GROUNDTRUTH_HOME"] = HOME                     # key, settings, jobs and logs in the install folder
os.environ["GROUNDTRUTH_SERVICE"] = "1"                   # the server takes remote updates only as the service
os.environ["GROUNDTRUTH_PARENT_PID"] = str(os.getpid())   # and ends should this runner be killed
os.environ["PYTHONPATH"] = os.pathsep.join([APP, SITE])  # the server and its job processes find the same code
log = open(os.path.join(HOME, "service.log"), "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log                              # a service has no console
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def say(*text):
    """A line in service.log, with the time."""
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *text, flush=True)


def serve_once():
    """One run of the server, in a process of its own (so the next one loads an update's new code): its exit code."""
    code = "import sys; from groundtruth.cli import main; sys.exit(main(['serve', '--log', %r]))" % \
        os.path.join(HOME, "server.log")
    return subprocess.call([sys.executable, "-c", code], stdout=log, stderr=log, creationflags=NO_WINDOW)


def last(state, **info):
    """updates/last.json: how the last remote update went (the server reports it in its status)."""
    os.makedirs(os.path.join(HOME, "updates"), exist_ok=True)
    with open(os.path.join(HOME, "updates", "last.json"), "w", encoding="utf-8") as f:
        json.dump(dict(state=state, at=time.time(), **info), f)


def apply_update():
    """The update the server left in updates/pending.json (already checked: signed, newer): unpacked, every path
    inside the staging folder, then installed by the new package's own server_setup.py apply-update. True when it
    is installed; otherwise the old version stays and last.json says why."""
    pending = os.path.join(HOME, "updates", "pending.json")
    try:
        with open(pending, encoding="utf-8") as f:
            info = json.load(f)
        os.remove(pending)
    except (OSError, ValueError):
        return False
    staging = os.path.join(HOME, "updates", "staging")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with zipfile.ZipFile(info["zip"]) as z:
            for name in z.namelist():
                parts = name.replace(chr(92), "/").split("/")
                if name.startswith(("/", chr(92))) or ":" in name or ".." in parts:
                    raise ValueError("a path outside the package: " + name)
            z.extractall(staging)
        setup = glob.glob(os.path.join(staging, "*", "server_setup.py"))[0]
    except Exception as e:                                  # noqa: BLE001 - the old version stays
        last("failed", version=info.get("version"), error="the package could not be unpacked: " + str(e))
        say("update failed:", e)
        return False
    say("installing the update to", info.get("version"))
    rc = subprocess.call([sys.executable, setup, "apply-update", "--dir", HOME, "--from-version",
                          info.get("from", "")], stdout=log, stderr=log, creationflags=NO_WINDOW)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        os.remove(info["zip"])
    except OSError:
        pass
    if rc != 0:
        say("update failed (exit code %d): the previous version stays" % rc)
        return False
    return True


def roll_back(why):
    """The previous version (app.prev) back in place of an update that did not stay up."""
    prev, failed = APP + ".prev", APP + ".failed"
    if not os.path.isdir(prev):
        return
    shutil.rmtree(failed, ignore_errors=True)
    os.replace(APP, failed)
    os.replace(prev, APP)
    say("the updated server did not stay up (" + why + "): back to the previous version")
    last("rolled back", error=why)


def main():
    """Run the server, again and again (10 s apart; a minute when the port is taken); updates in between."""
    updated = False
    while True:
        say("starting the GroundTruth compute server")
        t0, rc = time.time(), 1
        try:
            rc = serve_once()
        except BaseException:
            traceback.print_exc()
        if rc == UPDATE_EXIT:
            updated = apply_update()
            continue
        if updated and time.time() - t0 < HEALTH_S:
            roll_back("it ended after %.0f s with exit code %s" % (time.time() - t0, rc))
            updated = False
            continue
        updated = False
        say("the server stopped (exit code %s); starting it again" % rc)
        time.sleep(60 if rc == 2 else 10)


main()
'''


def runner_text(paths: dict) -> str:
    """run_server.py for this install."""
    return RUNNER.format(task=TASK, home=str(paths["root"]), app=str(paths["app"]), site=str(paths["site"]))


SERVER_EXTRA = ("cryptography>=42",)   # the server's own libraries: checking signed updates (remote/release.py)
RELEASE_PUB = "release_key.pub"        # the trusted release key, from the package (remote/release.PUBLIC_FILE)


def trust_release_key(paths: dict):
    """The release key this server trusts for remote updates: the one in the package installed now, every manual
    install or update resetting it (the user: "The signing key that is used to update the server should be reset if
    the server is re-installed manually"); a remote update never changes it. A package without one leaves none:
    remote updates are refused until a signed package is installed here."""
    src, dst = HERE / RELEASE_PUB, paths["root"] / RELEASE_PUB
    if src.exists():
        key = src.read_text(encoding="utf-8").strip()
        say(f"    the release key for remote updates: {key[:8]}…{key[-8:]} (from this package; it replaces any "
            "earlier one)")
        if not DRY["on"]:
            dst.write_text(key + "\n", encoding="utf-8")
    else:
        say("    no release key in this package (not a signed build): remote updates are refused until a signed "
            "package is installed here")
        if not DRY["on"] and readable(dst):
            dst.unlink()


def local_adapters(paths: dict) -> list[tuple[str, str]]:
    """[(network card, IPv4 address)] of this PC (remote/cluster.adapters, run with the install's own Python: it
    has psutil); [] in a dry run or when it can't be asked."""
    if DRY["on"]:
        return []
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from groundtruth.remote.cluster import adapters; "
            "print(json.dumps(adapters()))")
    try:
        out = subprocess.run([str(venv_python(paths)), "-c", code, str(paths["app"])], capture_output=True,
                             text=True, timeout=60).stdout
        return [tuple(x) for x in json.loads(out.strip().splitlines()[-1])]
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return []


def ask_address(args, paths: dict) -> None:
    """--address: the address this server announces to the cluster and the users (server.json "address"; the user:
    "can we start a server with a known address ... and then get presented the available options?"). Alone: this
    PC's network cards and addresses listed, one picked (0: automatic, every real address); an address: checked
    against this PC's; `auto`: back to automatic. Not given: the setting stays as it is."""
    if args.address is None:
        return
    found = local_adapters(paths)
    ips = [ip for _, ip in found]
    if args.address == ASK:
        if DRY["on"]:
            say("    (--address: this PC's addresses are listed here to pick from)")
            args.address = None
            return
        say("")
        say("The address this server announces: the one the other servers and the users' PCs reach it at.")
        say("      0  automatic: every address of this PC's real networks")
        for k, (nic, ip) in enumerate(found, 1):
            say(f"      {k}  {ip}" + (f"  ({nic})" if nic else ""))
        while True:
            answer = input("    which one? [0] ").strip()
            if answer in ("", "0"):
                args.address = ""
                break
            if answer.isdigit() and 1 <= int(answer) <= len(found):
                args.address = found[int(answer) - 1][1]
                break
            if answer in ips:
                args.address = answer
                break
            say("    a number from the list, please")
        return
    if args.address.strip().lower() in ("auto", "automatic", "-"):
        args.address = ""
    elif ips and args.address.strip() not in ips:
        raise SystemExit(f"--address {args.address}: not an address of this PC (it has {', '.join(ips)}); --address "
                         "alone lists them")
    else:
        args.address = args.address.strip()


def ask_remote_update(args, paths: dict, update: bool) -> None:
    """--remote-update on|off, or on a first install (no setting yet, a console to ask in) the question; the answer
    goes to server.json (write_settings). Off unless said yes."""
    if args.remote_update is not None or update or DRY["on"]:
        return
    try:
        current = json.loads(paths["settings"].read_text(encoding="utf-8")) if readable(paths["settings"]) else {}
    except (OSError, ValueError):
        current = {}
    if "remote_update" in current or not sys.stdin or not sys.stdin.isatty():
        return
    say("")
    say("Remote updates: the users' GroundTruth plugins may update this server (Preferences, Compute server), with")
    say("packages signed by the release key of the package installed now. Without them, update it here by hand.")
    answer = input("Allow remote updates? [y/N] ").strip().lower()
    args.remote_update = "on" if answer in ("y", "yes") else "off"


def cmd_apply_update(args, paths: dict) -> int:
    """apply-update (run by the service's runner, from the unpacked update; the server has ended): the package next
    to this file into app.new, the libraries only when its requirements changed, then app -> app.prev and
    app.new -> app, a new run_server.py; the access key, settings, jobs and trusted release key stay. last.json
    says how it went; on a failure the old version stays."""
    root, frm = paths["root"], args.from_version or ""
    version = (HERE / "version.txt").read_text(encoding="utf-8").strip() if (HERE / "version.txt").exists() else "?"
    new_app, prev = root / "app.new", root / "app.prev"
    say(f"GroundTruth compute server: remote update {frm or '?'} -> {version}")
    try:
        shutil.rmtree(new_app, ignore_errors=True)
        copy_package({**paths, "app": new_app})
        req = root / "requirements-server.txt"
        lines = requirements(source()[1])
        old = req.read_text(encoding="utf-8").split() if req.exists() else []
        if sorted(old) != sorted(lines):
            say("    the libraries changed: pip installs them")
            req.write_text("\n".join(lines) + "\n", encoding="utf-8")
            run([str(venv_python(paths)), "-m", "pip", "install", "--upgrade", "-r", str(req)])
        shutil.rmtree(prev, ignore_errors=True)
        if paths["app"].exists():
            os.replace(paths["app"], prev)
        os.replace(new_app, paths["app"])
        link_app(paths)
        paths["runner"].write_text(runner_text(paths), encoding="utf-8")
        if mac():
            lock_down(paths)
        record(paths, "installed", version=version, previous=frm)
        say(f"    installed {version}; the previous version is kept in {prev.name}")
        return 0
    except BaseException as e:                              # noqa: BLE001 - SystemExit from run() too
        shutil.rmtree(new_app, ignore_errors=True)
        record(paths, "failed", version=version, error=f"{type(e).__name__}: {e}")
        say(f"    the update failed ({e}): the previous version stays")
        return 1


def record(paths: dict, state: str, **info):
    """updates/last.json: how the last remote update went (the server's status reports it)."""
    folder = paths["root"] / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "last.json").write_text(json.dumps({"state": state, "at": time.time(), **info}), encoding="utf-8")


def task_xml(python: Path, runner: Path, workdir: Path) -> str:
    """The scheduled task: at boot, as SYSTEM, highest privileges, no time limit, one instance, restarted every
    minute should it fail, normal priority (a compute server should get the CPU), not stopped on battery."""
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>GroundTruth compute server: runs the GroundTruth plugin's Simulate jobs for the users on the network (server_setup.py).</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python))}</Command>
      <Arguments>{escape(subprocess.list2cmdline([str(runner)]))}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'''


def source() -> tuple[Path, Path]:
    """(the groundtruth package, requirements.txt) to install: next to this file in the unpacked server package, or
    in the repository (src/groundtruth, plugin/requirements.txt) when run from tools/."""
    if (HERE / "groundtruth").is_dir():
        return HERE / "groundtruth", HERE / "requirements.txt"
    return HERE.parent / "src" / "groundtruth", HERE.parent / "plugin" / "requirements.txt"


def link_app(paths: dict):
    """groundtruth_app.pth in the venv's site-packages: every Python of the venv (the server, its job processes, the
    GPU check) finds GroundTruth in app\\ without PYTHONPATH, which KiCad's Python ignores."""
    pth = paths["site"] / "groundtruth_app.pth"
    say(f"    {pth}: GroundTruth for every process of the service")
    if not DRY["on"]:
        pth.write_text(str(paths["app"]) + "\n", encoding="utf-8")


def clean_key(text: str) -> str:
    """A typed or pasted key without what came along with it: spaces, line ends, surrounding quotes, and the
    "Access key:" label when the whole line was copied."""
    text = text.strip().strip("\"'").strip()
    if ":" in text and text.lower().split(":", 1)[0].strip() in ("access key", "key"):
        text = text.split(":", 1)[1].strip().strip("\"'").strip()
    return text


def key_check(paths: dict, key: str, find=None) -> str:
    """Which servers on this network use `key` (a Find servers query, remote/cluster.find_servers; this server's own
    service is stopped during the install, so it does not answer): a line saying so, or that none did, with why that
    may be. `find`: the query (tests)."""
    try:
        if find is None:
            sys.path.insert(0, str(paths["app"]))
            from groundtruth.remote.cluster import find_servers as find
        found = find(key)
    except Exception as e:                              # noqa: BLE001 - the check is a help, never a stop
        return f"(could not ask the network for servers with this key: {e})"
    if found:
        return "this key is used by: " + ", ".join(f"{m.get('host')} ({m.get('address')})" for m in found) + \
            ": this server joins them"
    return ("no server on this network answered to this key: check it against `install_server key` there (.cmd or "
            ".command; one "
            "wrong character makes a separate cluster); a server on another subnet answers only when listed with "
            "--peers")


def readable(path: Path) -> bool:
    """`path` exists and this process may look at it. An installed server's folder is for administrators and SYSTEM
    only: a --dry-run as a normal user can't even ask whether its files exist (PermissionError), and sees none."""
    try:
        return path.exists()
    except PermissionError:
        return False


def copy_package(paths: dict):
    """The GroundTruth to install (source()) and its version.txt into app\\, replacing the old copy."""
    src = source()[0]
    if not (src / "remote" / "server.py").exists():
        raise SystemExit(f"no GroundTruth package next to this file ({src}): run it from the unpacked server "
                         "package")
    say(f"    into {paths['app']}")
    if DRY["on"]:
        return
    if paths["app"].exists():
        shutil.rmtree(paths["app"])
    shutil.copytree(src, paths["app"] / "groundtruth", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("version.txt",):
        if (HERE / name).exists():
            shutil.copy2(HERE / name, paths["app"] / name)


def make_venv(python: Path, paths: dict, gpu: str, cuda: str):
    """The libraries: a venv from KiCad's Python, the server requirements from PyPI, PyTorch for --gpu (not on a Mac:
    no CUDA there, and an Apple GPU has no double precision for the solver)."""
    venv_py = venv_python(paths)
    if not DRY["on"] and readable(venv_py) and (python_version(venv_py) or (0, 0)) < MIN_PYTHON:
        # an environment made from a too old Python (a Mac's first install on KiCad's 3.9): made anew
        say(f"    the Python environment is older than {'.'.join(map(str, MIN_PYTHON))}: made anew")
        shutil.rmtree(paths["venv"])
    if DRY["on"] or not readable(venv_py):
        say(f"    making the Python environment {paths['venv']}")
        run([str(python), "-m", "venv", str(paths["venv"])])
    req = paths["root"] / "requirements-server.txt"
    lines = requirements(source()[1])
    if not DRY["on"]:
        req.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say("    the libraries from PyPI (about 300 MB; a few minutes on a first install, seconds on an update)")
    run([str(venv_py), "-m", "pip", "install", "--upgrade", "pip"], check=False)
    run([str(venv_py), "-m", "pip", "install", "--upgrade", "-r", str(req)])
    if not DRY["on"]:
        paths["site"] = layout(paths["root"])["site"]        # a Mac's venv: lib/python3.X/site-packages, now known
    if cuda and mac():
        say("    --cuda ignored: a Mac has no CUDA, and its GPU has no double precision for the solver: the CPU "
            "solves (Apple Silicon is fast at it)")
    elif cuda and gpu != "off":
        say(f"    PyTorch with CUDA ({cuda}): a 2.6 GB download, 5-20 minutes; pip prints nothing while it "
            "downloads, the lines below say it is still working")
        run([str(venv_py), "-m", "pip", "install", "--upgrade", "torch", "--index-url",
             f"https://download.pytorch.org/whl/{cuda}"])


def write_settings(paths: dict, args):
    """server.json: the defaults, the options given on top (the server reads it at every start)."""
    current = {}
    if readable(paths["settings"]):
        try:
            current = json.loads(paths["settings"].read_text(encoding="utf-8"))
        except ValueError:
            current = {}
    new = {"port": 8765, "workers": 1, "threads": "-", "gpu": "auto", "keep_hours": 24.0, "beacon_port": 8766,
           "lead": False, "peers": [], "remote_update": False, "address": "", **current}
    peers = [x.strip() for x in args.peers.split(",") if x.strip()] if args.peers is not None else None
    for key, value in (("port", args.port), ("workers", args.workers), ("threads", args.threads), ("gpu", args.gpu),
                       ("keep_hours", args.keep_hours), ("lead", args.lead), ("peers", peers),
                       ("beacon_port", args.beacon_port),
                       ("remote_update", None if getattr(args, "remote_update", None) is None
                        else args.remote_update == "on"),
                       ("address", getattr(args, "address", None))):
        if value is not None:
            new[key] = value
    say(f"    {paths['settings']}: {json.dumps(new)}")
    if not DRY["on"]:
        paths["settings"].write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    return new


def make_key(paths: dict, new: bool = False, given: str = "") -> str:
    """The access key (made once and kept on update; `new`: replaced by a new one, every user then needs it;
    `given`: this one, to join a cluster: every server of a cluster has the same key)."""
    if DRY["on"]:
        return "<the access key>"
    if given == ASK:
        # shown as typed (the user: "show that you typed, it's really hard to enter a key"): a 43-character key typed
        # blind went wrong, and a wrong key quietly makes a cluster of its own; right-click pastes in the console
        say("    the cluster's access key: `install_server key` on one of its servers shows it (.cmd on Windows, "
            ".command on a Mac)")
        given = input("    type or paste it here: ")
    if given:
        given = clean_key(given)
        if len(given) < 16:
            raise SystemExit("--key: an access key has at least 16 characters (install_server key on a server "
                             "of the cluster shows its key)")
        paths["key"].write_text(given + "\n", encoding="utf-8")
        say(f"    the access key given (--key, {len(given)} characters): this server joins the cluster that uses it")
        say("    " + key_check(paths, given))
        return given
    if new and readable(paths["key"]):
        paths["key"].unlink()
    sys.path.insert(0, str(paths["app"]))
    os.environ["GROUNDTRUTH_HOME"] = str(paths["root"])
    from groundtruth.remote import protocol
    key, made = protocol.read_key(paths["key"], create=True)
    if made:
        say("    a new access key was made")
    return key


def lock_down(paths: dict):
    """The folder for SYSTEM and administrators only (the key is a secret), inherited by everything in it; on a Mac
    owned by root, nobody else may read it."""
    if mac():
        say("    the folder for root only (the access key is a secret)")
        run(["chown", "-R", "root:wheel", str(paths["root"])], quiet=True)
        run(["chmod", "-R", "go-rwx", str(paths["root"])], quiet=True)
        return
    say("    the folder for SYSTEM and administrators only (the access key is a secret)")
    run(["icacls", str(paths["root"]), "/inheritance:r", "/grant:r", f"{SID_SYSTEM}:(OI)(CI)F",
         f"{SID_ADMINS}:(OI)(CI)F"], quiet=True)


def firewall(port: int, beacon_port: int = 8766, python: Path = None):
    """The inbound rules, private and domain networks (an office PC joined to a domain is on the domain profile),
    replacing old ones: TCP `port` (the jobs) and UDP `beacon_port` (the servers finding each other; 0: none). On a Mac
    the Application Firewall allows programs, not ports: the server's Python (the real file behind the venv's link) is
    allowed incoming connections (it matters only when the firewall is on: System Settings, Network, Firewall)."""
    if mac():
        real = os.path.realpath(str(python)) if python and not DRY["on"] else str(python)
        say(f"    the macOS firewall: the server's Python may accept connections (TCP {port}, UDP {beacon_port})")
        run([SOCKETFILTERFW, "--add", real], check=False, quiet=True)
        run([SOCKETFILTERFW, "--unblockapp", real], check=False, quiet=True)
        return
    say(f"    allowing TCP {port} (jobs)" + (f" and UDP {beacon_port} (the cluster)" if beacon_port else "")
        + " in on the private and domain networks (never public)")
    run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={RULE}"], check=False, quiet=True)
    run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={RULE}", "dir=in", "action=allow",
         "protocol=TCP", f"localport={port}", "profile=private,domain"], quiet=True)
    if beacon_port:
        run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={RULE}", "dir=in", "action=allow",
             "protocol=UDP", f"localport={beacon_port}", "profile=private,domain"], quiet=True)


def launchd_plist(python: Path, paths: dict) -> bytes:
    """The launchd daemon (a Mac's service): at boot (RunAtLoad), as root, restarted whenever it stops (KeepAlive,
    10 s apart), running run_server.py with the venv's Python, its output to service.log."""
    import plistlib
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": [str(python), str(paths["runner"])],
        "WorkingDirectory": str(paths["root"]), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "StandardOutPath": str(paths["service_log"]), "StandardErrorPath": str(paths["service_log"]),
        "ProcessType": "Standard"})


def register_task(python: Path, paths: dict):
    """The scheduled task (replacing an old one), running run_server.py with `python`: the venv's python.exe. KiCad's
    own Python ignores PYTHONPATH (built so), so a server on it started job processes and a GPU check that found
    neither GroundTruth nor the venv's libraries ("PyTorch is not installed" with PyTorch installed). The venv's
    python.exe is a launcher, but it ties the real interpreter to itself (a job object): ending the task still ends
    the server."""
    if mac():
        say(f"    the launchd daemon {LABEL}: at boot, as root, nobody needs to log in ({PLIST})")
        if not DRY["on"]:
            PLIST.write_bytes(launchd_plist(python, paths))
            os.chmod(PLIST, 0o644)
        run(["launchctl", "bootout", f"system/{LABEL}"], check=False, quiet=True)
        run(["launchctl", "bootstrap", "system", str(PLIST)], quiet=True)
        return
    xml = paths["root"] / "task.xml"
    say(f"    the scheduled task \"{TASK}\": at boot, as SYSTEM, nobody needs to log in")
    if not DRY["on"]:
        xml.write_text(task_xml(python, paths["runner"], paths["root"]), encoding="utf-16")
    run(["schtasks", "/Create", "/TN", TASK, "/XML", str(xml), "/F"], quiet=True)


def task_exists() -> bool:
    """The scheduled task is registered."""
    if DRY["on"]:
        return False
    if mac():
        return subprocess.run(["launchctl", "print", f"system/{LABEL}"], capture_output=True).returncode == 0
    return subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True).returncode == 0


def stop(wait_s: float = 20.0):
    """End the task (and with it the server; its job processes end within seconds, see remote/worker.py), and wait
    until it no longer runs: a start while the old server still runs is ignored (one instance), and the old server
    went on with its old code and its old GPU check (the user's install showed "PyTorch is not installed" although
    pip had just installed it)."""
    if mac():
        run(["launchctl", "bootout", f"system/{LABEL}"], check=False, quiet=True)     # KeepAlive no longer restarts it
    else:
        run(["schtasks", "/End", "/TN", TASK], check=False, quiet=True)
    if DRY["on"]:
        return
    end = time.monotonic() + wait_s
    while time.monotonic() < end and task_state().startswith("running"):
        time.sleep(1)


def start():
    """Start the task now (it also starts at every boot); on a Mac load the daemon again and start it."""
    if mac():
        if not task_exists():
            run(["launchctl", "bootstrap", "system", str(PLIST)], check=False, quiet=True)
        run(["launchctl", "kickstart", "-k", f"system/{LABEL}"], check=False, quiet=True)
        return
    run(["schtasks", "/Run", "/TN", TASK], quiet=True)


def addresses(paths: dict = None) -> list[str]:
    """This PC's name and its IPv4 addresses on real networks (the server's own local_addresses, which leaves out
    virtual adapters such as Hyper-V's or WSL's; with the install's libraries on the path), else every IPv4. The
    address chosen with --address (server.json "address") comes right after the name."""
    if paths is not None and not DRY["on"]:
        sys.path[:0] = [str(paths["app"]), str(paths["site"])]
        try:
            from groundtruth.remote.server import local_addresses
            found = local_addresses()
            chosen = ""
            if readable(paths["settings"]):
                chosen = str(json.loads(paths["settings"].read_text(encoding="utf-8")).get("address") or "")
            return found[:1] + ([chosen] if chosen else []) + [a for a in found[1:] if a != chosen]
        except Exception:                               # noqa: BLE001 - fall back to the plain list
            pass
    import socket
    name = socket.gethostname()
    ips = []
    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo(name, None, socket.AF_INET)
                      if not i[4][0].startswith("127.")})
    except OSError:
        pass
    return [name] + ips


def tell_users(key: str, port: int, paths: dict = None):
    """What each user enters in the plugin, set apart."""
    names = " or ".join(n if port == 8765 else f"{n}:{port}" for n in addresses(paths))
    bar = "=" * 78
    say(f"\n{bar}\n Give each user these (Preferences, Compute server: run analyses on a GroundTruth compute server):"
        f"\n\n   Server address:  {names}\n   Access key:      {key}\n\n Keep the key like a password. "
        f"{launcher()} key shows it again.\n More servers for a cluster: {launcher()} install --key "
        f"(it asks for this key).\n{bar}")


WIDTH = 78                      # the console window's usual width: the status block's lines fit it


def status_block(st: dict, port: int, task: str = "", paths: dict | None = None) -> str:
    """The server's status (GET /v1/status) as a block, one fact per line, long reasons wrapped (the user, of the
    one long line it was: "this needs better formatting"); with `paths` (layout()) also where the install is on this
    PC (the user: "add a path entry that shows where the binaries are stored on the PC")."""
    import textwrap

    def line(label: str, text: str) -> list[str]:
        """'  Label:    text', wrapped under itself (no label: a continuation under the one before); an entry of a
        list ('- ', '* ', or a time 'HH:MM ' / 'MM-DD HH:MM ') wraps under its own text, not under its bullet."""
        import re
        head = f"  {(label + ':') if label else '':<10} "
        bullet = re.match(r"([-*!] |(?:\d\d-\d\d )?\d\d:\d\d )", text)
        hang = " " * (len(head) + (len(bullet.group(1)) if bullet else 0))
        return textwrap.wrap(text, WIDTH, initial_indent=head, subsequent_indent=hang) or [head]
    out = []
    if task:
        out += line("Service", task)
    ips = [a for a in st.get("addresses") or [] if a]
    out += line("Server", f"{st.get('host')}" + (f" ({', '.join(ips)})" if ips else "") + f" answers on port {port}")
    if st.get("address"):
        out += line("Address", f"announces {st['address']} to the cluster and the users (install --address changes "
                               "it; --address auto: every address)")
    version = str(st.get("version", "?")).split(", built")[0].rstrip(")") + ")" if ", built" in str(
        st.get("version", "")) else str(st.get("version", "?"))
    out += line("Version", f"{version}, code {st.get('code')}")
    out += line("Machine", f"{st.get('cores')} cores, {st.get('memory_gb')} GB RAM ({st.get('free_gb')} GB free)")
    gpus = [g for g in st.get("gpus") or [] if not g.startswith("(")]
    if gpus:
        out += line("GPU", ", ".join(gpus))
    elif (st.get("gpus") or [""])[0].startswith("(still"):
        out += line("GPU", "still being detected (loading PyTorch can take a minute): ask again shortly")
    else:
        out += line("GPU", "none: the solves run on the CPU")
        why, fix = gpu_reason(st.get("gpu_note") or "")
        if why:
            out += line("Why", why)
        if fix:
            out += line("Fix", fix)
    running, queued = st.get("running") or [], st.get("queue") or []
    jobs = f"{st.get('workers')} at a time; " + (f"{len(running)} running, {len(queued)} queued" if running or queued
                                                 else "none running or queued")
    if st.get("results_waiting"):
        jobs += f"; {st['results_waiting']} result(s) waiting to be fetched"
    out += line("Jobs", jobs)
    out += cluster_lines(st.get("cluster") or {}, line)
    def own(v):
        """A setting, '-' as words."""
        return "as each user chooses" if str(v) == "-" else str(v)
    out += line("Settings", f"threads: {own(st.get('threads'))}; GPU: {own(st.get('gpu'))}; results kept "
                            f"{st.get('keep_hours', 24):g} h")
    out += line("Updates", "remote updates on (signed packages from the users' plugins)" if st.get("remote_update")
                else "remote updates off (install --remote-update on turns them on)")
    up = st.get("update") or {}
    if up:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(up.get("at") or up.get("since") or 0))
        out += line("", {"installed": f"- {when} updated to {up.get('version')}",
                         "failed": f"- {when} an update to {up.get('version')} failed: {up.get('error')}",
                         "rolled back": f"- {when} an update was rolled back: {up.get('error')}",
                         "waiting": f"- an update to {up.get('version')} waits for the running jobs",
                         "installing": f"- installing an update to {up.get('version')}"}.get(up.get("state"), ""))
    if paths:
        # paths are never wrapped mid-name: each part on a line of its own
        out.append(f"  {'Path:':<10} {paths['root']}")
        for what, where in (("program", f"{paths['app'].name}\\ (GroundTruth)"),
                            ("Python", f"{paths['venv'].name}\\ (its libraries; {paths['venv'].name}\\Scripts\\"
                                       f"python.exe runs the server)"),
                            ("settings", f"{paths['settings'].name}, key in {paths['key'].name}"),
                            ("logs", f"{paths['log'].name}, {paths['service_log'].name}; jobs in server_jobs\\")):
            out.append(f"  {'':<10} {what + ':':<9} {where}")
    return "\n".join(out)


HISTORY_SHOWN = 10              # the cluster's last events in the status (all of them are in server.log)


def cluster_lines(c: dict, line) -> list[str]:
    """The cluster in the status block (the user: "list the size of the cluster and each PC's name/IP inside the
    cluster. also it should show if an individual machine of the cluster disconnected at any time and how long it
    is connected to that cluster"): its size and since when, the lead and how it was chosen, every server with its
    address, hardware, load and time in the cluster, servers with the key but another version, and the history
    (joined, lost, back, restarted, lead changes). `line(label, text)` formats one entry."""
    if c.get("discovery") is False:
        return line("Cluster", "off (beacon_port 0 in server.json): this server does not look for others")
    now = c.get("now") or time.time()

    def when(t) -> str:
        """A time stamp: 'HH:MM' today, else 'MM-DD HH:MM'."""
        lt = time.localtime(t)
        return time.strftime("%H:%M" if lt[:3] == time.localtime(now)[:3] else "%m-%d %H:%M", lt)

    def hardware(m: dict) -> str:
        """'16 cores, 31.7 GB, 2x Quadro P2200'; its load: 'idle' or '1 running, 2 queued'."""
        gpus = m.get("gpus") or []
        names = sorted(set(gpus), key=gpus.index)
        g = ", ".join(f"{gpus.count(n)}x {n}" if gpus.count(n) > 1 else n for n in names) or "no GPU"
        busy = [f"{m[k]} {k}" for k in ("running", "queued") if m.get(k)]
        load = ", ".join(busy) if busy else "idle" if "running" in m else ""
        return f"{m.get('cores', '?')} cores, {m.get('memory_gb', '?')} GB, {g}" + (f"; {load}" if load else "")

    out = []
    members, lead, me = c.get("members") or [], c.get("lead") or {}, c.get("me") or {}
    port = c.get("beacon_port") or 8766
    if members:
        since = c.get("together_since")
        out += line("Cluster", f"{c.get('size', 1 + len(members))} servers" +
                    (f", together since {when(since)} ({span(now - since)})" if since else ""))
        how = "set with --lead" if c.get("forced_lead") and lead.get("me") else ""
        led = (f"this server{' (' + how + ')' if how else ''}" if lead.get("me") else
               f"{lead.get('host')} {lead.get('address')}") + \
            (f", since {when(c['lead_since'])} ({span(now - c['lead_since'])})" if c.get("lead_since") else "")
        out += line("Lead", led + "; Simulates sent to any server are split by it")
        out += line("Servers", f"* {me.get('host', '?')} (this server{', lead' if lead.get('me') else ''}): "
                               f"{hardware(me)}; up {span(now - me['started_at'])}" if me.get("started_at") else
                    f"* {me.get('host', '?')} (this server)")
        for m in members:
            joined = m.get("joined_at")
            out += line("", f"- {m.get('host')} {m.get('address')}{' (lead)' if m.get('lead') else ''}: "
                            f"{hardware(m)}; in the cluster {span(now - joined) if joined else '?'}, "
                            f"heard {m.get('seen_s', 0):.0f} s ago")
    else:
        out += line("Cluster", "standalone: no other server with this key heard on the network"
                    + (" since this server started" if not c.get("events") else ""))
    for m in c.get("other_versions") or []:
        out += line("", f"! {m.get('host')} {m.get('address')} has this key but GroundTruth "
                        f"{m.get('version', 'of another version')}: not used until it is updated")
    peers = c.get("static_peers") or []
    out += line("Network", f"announcements on UDP {port}" +
                (f"; listed in server.json (other subnets): {', '.join(peers)}" if peers else
                 "; same subnet only (server.json \"peers\" adds others)"))
    events = c.get("events") or []
    if events:
        shown = events[-HISTORY_SHOWN:]
        out += line("History", f"{when(shown[0]['at'])} {shown[0]['host']} {shown[0]['event']}"
                               + (f": {shown[0]['text']}" if shown[0].get("text") else ""))
        for e in shown[1:]:
            out += line("", f"{when(e['at'])} {e['host']} {e['event']}" + (f": {e['text']}" if e.get("text") else ""))
        if len(events) > len(shown):
            out += line("", f"({len(events) - len(shown)} earlier; all of them in server.log)")
    return out


def gpu_reason(note: str) -> tuple[str, str]:
    """(why, fix) from the server's GPU note: the cause in plain words and the one fix for it."""
    if mac() and note:                  # no CUDA on a Mac, and Apple's GPUs have no double precision (float64)
        return ("a Mac's GPU (Apple M-series) cannot do the solver's double-precision arithmetic",
                "none needed: the CPU solves (Apple Silicon is quick at it)")
    if "No module named 'torch'" in note:
        return ("PyTorch is not installed in the service's environment",
                "install_server.cmd install --cuda cu126   (2.6 GB download)")
    if "WinError 126" in note or "DLL" in note.split("):")[-1]:
        return ("PyTorch is installed, but Windows lacks a DLL it needs",
                "install the Microsoft Visual C++ Redistributable (x64: aka.ms/vs/17/release/vc_redist.x64.exe), then "
                "install_server.cmd stop and install_server.cmd start")
    if "CPU-only" in note:
        return "the installed PyTorch is a CPU-only build", "install_server.cmd install --cuda cu126"
    if "no NVIDIA GPU" in note:
        return ("PyTorch finds no NVIDIA GPU (this PC has none, or its driver is too old)",
                "update the NVIDIA driver, or leave it: the CPU does the work")
    return note, ""


def task_state() -> str:
    """The scheduled task's state in a few words ('running, starts with Windows'), or why it can't be told; on a
    Mac the launchd daemon's."""
    if mac():
        out = subprocess.run(["launchctl", "print", f"system/{LABEL}"], capture_output=True, text=True)
        if out.returncode != 0:
            return "not running (install_server.command start; or install it)"
        state = next((ln.split("=", 1)[1].strip() for ln in out.stdout.splitlines() if ln.strip().startswith("state =")),
                     "unknown")
        return f"{state}; starts at boot"
    out = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"], capture_output=True, text=True)
    if out.returncode != 0:
        return f"not installed ({launcher()} install)"
    status = next((ln.split(":", 1)[1].strip() for ln in out.stdout.splitlines() if ln.strip().startswith("Status:")),
                  "unknown")
    return f"{status.lower()}; starts with Windows"


def wait_ready(paths: dict, port: int, key: str, timeout: float = 120.0, task: str = "") -> bool:
    """Ask the new server for its status until it answers (it starts within seconds; the first start compiles), then
    show it as a block."""
    if DRY["on"]:
        return True
    sys.path.insert(0, str(paths["app"]))
    from groundtruth.remote.client import Remote
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            st = Remote(f"127.0.0.1:{port}", key, client="server_setup").status()
            say("")
            say(status_block(st, port, task, paths))
            return True
        except Exception:                                # noqa: BLE001 - not up yet
            say(f"    waiting for the server to answer ({clock(timeout - (end - time.monotonic()))}) ...")
            time.sleep(3)
    say(f"    the server does not answer yet: see {paths['service_log']} and {paths['log']}")
    return False


def install_shim(paths: dict):
    """On a Mac: the Accelerate shim install_server.command built as the installing user (gt_accelerate.dylib beside
    this file) into the install's folder, where solver/sparse.py looks first (PREBUILT). The service runs as root and
    cannot compile it itself (Xcode's licence prompt; the M2, 2026-10-25). Without it the solver uses SuperLU."""
    if not mac():
        return
    built = Path(__file__).resolve().with_name("gt_accelerate.dylib")
    if not built.exists():
        say("Apple Accelerate shim: not built (no Xcode command line tools?): the solver uses SuperLU")
        return
    if not DRY["on"]:
        shutil.copy2(built, paths["root"] / "gt_accelerate.dylib")
    say("Apple Accelerate shim: installed (the faster sparse factorisation)")


def cmd_install(args, paths: dict, update: bool = False) -> int:
    """install / update (see the module), in numbered steps."""
    python = Path(args.python) if args.python else kicad_python()
    if not DRY["on"]:
        version = python_version(python)
        if not version or version < MIN_PYTHON:
            raise SystemExit(too_old_text(f"{python} ({'.'.join(map(str, version)) if version else 'does not run'})"))
    t0 = time.monotonic()
    say(f"GroundTruth compute server: {'updating' if update else 'installing'} {paths['root']}")
    say(f"Python: {python}")
    running = task_exists()
    titles = (["Stopping the running server"] if running else []) + ["Copying GroundTruth"]
    if not (update and args.skip_pip):
        titles.append("Installing the libraries")
    titles += ["Writing the settings and the service's start script"]
    titles += [] if args.no_acl else ["Protecting the folder"]
    titles += [] if args.no_firewall else ["Opening the firewall port"]
    titles += [] if args.no_task else ["Registering and starting the service"]
    steps = Steps(titles)
    if not DRY["on"]:
        paths["root"].mkdir(parents=True, exist_ok=True)
    if running:                     # install or update: the old server must not go on running its old code
        steps.next("a job running now starts over afterwards")
        stop()
    steps.next()
    copy_package(paths)
    if not (update and args.skip_pip):
        steps.next()
        make_venv(python, paths, args.gpu or "auto", args.cuda)
    steps.next()
    link_app(paths)
    if not DRY["on"]:
        paths["runner"].write_text(runner_text(paths), encoding="utf-8")
    trust_release_key(paths)
    install_shim(paths)
    ask_remote_update(args, paths, update)
    ask_address(args, paths)
    cfg = write_settings(paths, args)
    key = make_key(paths, args.new_key, args.key or "")
    if not args.no_acl:
        steps.next()
        lock_down(paths)
    if not args.no_firewall:
        steps.next()
        firewall(int(cfg["port"]), int(cfg.get("beacon_port", 8766)), venv_python(paths))
    if not args.no_task:
        steps.next("the first start takes a little longer (Python compiles GroundTruth)")
        register_task(venv_python(paths), paths)
        start()
        wait_ready(paths, int(cfg["port"]), key)
    steps.done()
    tell_users(key, int(cfg["port"]), paths)
    say(f"\nDone in {clock(time.monotonic() - t0)}." + (
        "" if args.no_task else f" The server runs in the background and {at_boot()}. "
        f"{launcher()} status shows it; the logs are in " + str(paths["root"]) + "."))
    return 0


def cmd_status(paths: dict) -> int:
    """The task's state and the server's own status, as a block."""
    say("GroundTruth compute server")
    task = task_state()
    try:
        cfg = json.loads(paths["settings"].read_text(encoding="utf-8"))
        key = paths["key"].read_text(encoding="utf-8").strip()
        if not wait_ready(paths, int(cfg.get("port", 8765)), key, timeout=10, task=task):
            say(f"  Service:   {task}")
    except OSError as e:
        say(f"cannot read the server's files ({e}): run as administrator")
    return 0


LOG_LINES = 60                  # install_server.cmd log: the last lines shown


def cmd_log(paths: dict, lines: int = LOG_LINES, find: str = "") -> int:
    """The end of server.log (every job, the cluster's events, why a Simulate was not split) and of service.log (the
    service's own start-ups and crashes), with `find` only the lines containing it. The folder is for administrators
    and SYSTEM only (the access key is in it): Explorer and Notepad, which run without administrator rights, can't open
    them (the user: "I can't access the log file"); install_server.cmd log shows them in an administrator window."""
    for name in ("log", "service_log"):
        f = paths[name]
        try:
            text = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            say(f"{f}: not there yet")
            continue
        if find:
            text = [ln for ln in text if find.lower() in ln.lower()]
        shown = text[-max(1, lines):] if name == "log" else text[-min(lines, 15):]
        say("")
        say(f"--- {f}" + (f", lines with '{find}'" if find else "") + f": the last {len(shown)} of {len(text)} ---")
        for ln in shown:
            say(ln)
    return 0


def cmd_uninstall(paths: dict, purge: bool) -> int:
    """Stop and remove the task and the firewall rule; with --purge also the folder (key, settings, jobs, logs)."""
    stop()
    if mac():
        run([SOCKETFILTERFW, "--remove", os.path.realpath(str(venv_python(paths)))], check=False, quiet=True)
        say(f"removing {PLIST}")
        if not DRY["on"]:
            PLIST.unlink(missing_ok=True)
    else:
        run(["schtasks", "/Delete", "/TN", TASK, "/F"], check=False, quiet=True)
        run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={RULE}"], check=False, quiet=True)
    if purge:
        say(f"removing {paths['root']}")
        if not DRY["on"]:
            shutil.rmtree(paths["root"], ignore_errors=True)
    else:
        say(f"kept {paths['root']} (the key, settings, jobs and logs): --purge removes it")
    return 0


def main(argv=None) -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("install", "update", "status", "log", "key", "start", "stop", "uninstall",
                                        "apply-update"))
    ap.add_argument("--lines", type=int, default=LOG_LINES, help=f"log: how many of the last lines (default {LOG_LINES})")
    ap.add_argument("--find", help="log: only the lines containing this text (e.g. a job id, split, cluster)")
    ap.add_argument("--dir", help=f"the install folder (default {DEFAULT_DIR}; on a Mac {MAC_DIR})")
    ap.add_argument("--platform", choices=("windows", "mac"), help=argparse.SUPPRESS)   # dry runs and tests
    ap.add_argument("--python", help="the Python to run on (default: KiCad 10+'s own)")
    ap.add_argument("--workers", type=int, help="jobs at a time (server.json)")
    ap.add_argument("--threads", help="solver threads per job (server.json; '-': each user's choice)")
    ap.add_argument("--gpu", choices=("auto", "on", "off", "-"), help="use the GPUs (server.json)")
    ap.add_argument("--cuda", default="", help="also install PyTorch with this CUDA build (cu126, cu128) for an "
                                               "NVIDIA GPU (a few GB)")
    ap.add_argument("--port", type=int, help="the port (server.json; default 8765)")
    ap.add_argument("--keep-hours", type=float, help="how long a result waits for its owner (server.json)")
    ap.add_argument("--key", nargs="?", const=ASK, help="join a cluster: use the access key of its servers "
                    "(install_server.cmd key on one of them shows it); --key alone asks for it (shown as typed; right-click pastes) and "
                    "says which servers on the network use it")
    ap.add_argument("--lead", action=argparse.BooleanOptionalAction, default=None,
                    help="this server is (--no-lead: is no longer) the cluster's lead (server.json)")
    ap.add_argument("--peers", help="cluster servers on another subnet, comma-separated (server.json)")
    ap.add_argument("--beacon-port", type=int, help="the UDP port the servers find each other on (server.json; "
                                                    "default 8766; 0: no cluster)")
    ap.add_argument("--skip-pip", action="store_true", help="update: keep the installed libraries")
    ap.add_argument("--remote-update", choices=("on", "off"),
                    help="let the users' plugins update this server with signed packages (server.json; asked on a "
                         "first install)")
    ap.add_argument("--from-version", help=argparse.SUPPRESS)      # apply-update: the version it replaces
    ap.add_argument("--address", nargs="?", const=ASK,
                    help="the address this server announces to the cluster and the users (server.json): --address "
                         "alone lists this PC's network cards to pick one; --address auto: every address")
    ap.add_argument("--new-key", action="store_true", help="install/update: replace the access key with a new one "
                                                           "(every user then needs the new key)")
    ap.add_argument("--no-task", action="store_true", help="no scheduled task (run run_server.py yourself)")
    ap.add_argument("--no-firewall", action="store_true", help="no firewall rule")
    ap.add_argument("--no-acl", action="store_true", help="leave the folder's permissions as they are")
    ap.add_argument("--purge", action="store_true", help="uninstall: also the folder (key, settings, jobs, logs)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be done, change nothing")
    args = ap.parse_args(argv)
    DRY["on"] = args.dry_run
    if args.platform:
        PLATFORM["name"] = args.platform
    args.dir = args.dir or str(default_dir())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    paths = layout(Path(args.dir))
    # apply-update is run by the service itself (SYSTEM / root): no check here; without those rights it fails on the
    # locked folder anyway
    needs_admin = args.command in ("install", "update", "start", "stop", "uninstall", "key", "status", "log") and not (
        args.no_task and args.no_firewall and args.no_acl)
    if needs_admin and not args.dry_run and not is_admin():
        raise SystemExit("run this with sudo (install_server.command asks for your password)" if mac() else
                         "run this as administrator (install_server.cmd asks for it)")
    if args.command in ("install", "update"):
        return cmd_install(args, paths, update=args.command == "update")
    if args.command == "apply-update":
        return cmd_apply_update(args, paths)
    if args.command == "status":
        return cmd_status(paths)
    if args.command == "key":
        say(paths["key"].read_text(encoding="utf-8").strip() if not DRY["on"] else "<the access key>")
        return 0
    if args.command == "log":
        return cmd_log(paths, args.lines, args.find)
    if args.command == "start":
        start()
        return 0
    if args.command == "stop":
        stop()
        return 0
    return cmd_uninstall(paths, args.purge)


if __name__ == "__main__":
    sys.exit(main())
