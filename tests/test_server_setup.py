"""tools/server_setup.py: the compute server's service install, checked without changing the machine (--dry-run,
the pieces it writes)."""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import server_setup  # noqa: E402

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_the_server_needs_no_kicad_api_and_no_window_toolkit():
    """The server's libraries: the plugin's requirements without kicad-python and wxPython."""
    req = server_setup.requirements(Path(__file__).resolve().parents[1] / "plugin" / "requirements.txt")
    names = [r.split(">")[0].split("=")[0].lower() for r in req]
    assert "numpy" in names and "scipy" in names and "psutil" in names and "pyamg" in names
    assert "kicad-python" not in names and "wxpython" not in names


def test_the_scheduled_task_runs_at_boot_as_system_without_a_time_limit():
    """The task: a boot trigger, SYSTEM, no execution time limit (Task Scheduler's default stops a task after 72 h),
    restarted on failure, KiCad's Python running the install's run_server.py."""
    xml = server_setup.task_xml(Path(r"C:\Program Files\KiCad\10.0\bin\python.exe"),
                                Path(r"C:\ProgramData\GroundTruth\run_server.py"), Path(r"C:\ProgramData\GroundTruth"))
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert root.find("t:Triggers/t:BootTrigger", NS) is not None
    assert root.find("t:Principals/t:Principal/t:UserId", NS).text == "S-1-5-18"
    assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
    assert root.find("t:Settings/t:RestartOnFailure/t:Count", NS).text == "999"
    assert root.find("t:Settings/t:DisallowStartIfOnBatteries", NS).text == "false"
    exe = root.find("t:Actions/t:Exec", NS)
    assert exe.find("t:Command", NS).text.endswith(r"bin\python.exe")
    assert exe.find("t:Arguments", NS).text.endswith("run_server.py")


def test_the_runner_sets_the_home_and_paths_and_restarts_the_server(tmp_path):
    """run_server.py: valid Python, GROUNDTRUTH_HOME, the app and the venv's libraries on the path (also for the job
    processes), output to service.log, the server started again when it stops."""
    text = server_setup.runner_text(server_setup.layout(tmp_path))
    compile(text, "run_server.py", "exec")
    assert repr(str(tmp_path)) in text and 'os.environ["GROUNDTRUTH_HOME"] = HOME' in text
    assert 'os.environ["PYTHONPATH"]' in text and "service.log" in text and "while True:" in text


def test_a_dry_run_install_prints_the_plan_and_changes_nothing(tmp_path, capsys, monkeypatch):
    """--dry-run: the venv, the libraries, the folder's permissions (SYSTEM and administrators, by SID), the firewall
    rule on private and domain networks, the scheduled task and its start; nothing written."""
    monkeypatch.setattr(server_setup, "kicad_python", lambda: Path(r"C:\KiCad\bin\python.exe"))
    root = tmp_path / "gt"
    assert server_setup.main(["install", "--dir", str(root), "--dry-run", "--workers", "2", "--cuda", "cu126"]) == 0
    out = capsys.readouterr().out
    assert "-m venv" in out and "pip install --upgrade -r" in out and "whl/cu126" in out
    assert "/inheritance:r" in out and "*S-1-5-18:(OI)(CI)F" in out and "*S-1-5-32-544:(OI)(CI)F" in out
    assert "localport=8765" in out and "profile=private,domain" in out
    assert 'schtasks /Create /TN "GroundTruth compute server"' in out and "schtasks /Run" in out
    assert '"workers": 2' in out and "groundtruth_app.pth" in out     # GroundTruth found without PYTHONPATH
    assert not root.exists()


def test_the_service_runs_on_the_venvs_python(tmp_path, monkeypatch):
    """KiCad's Python ignores PYTHONPATH, so the service runs on the venv's python.exe (its job processes and the GPU
    check then find the venv's libraries and, through groundtruth_app.pth, GroundTruth)."""
    seen = {}
    monkeypatch.setattr(server_setup, "kicad_python", lambda: Path(r"C:\KiCadin\python.exe"))
    monkeypatch.setattr(server_setup, "register_task", lambda python, paths: seen.update(python=python))
    server_setup.main(["install", "--dir", str(tmp_path / "gt"), "--dry-run"])
    assert seen["python"] == tmp_path / "gt" / "venv" / "Scripts" / "python.exe"


def test_the_service_home_moves_the_servers_files(monkeypatch, tmp_path):
    """GROUNDTRUTH_HOME (set by run_server.py): the key, server.json and the jobs live there, not in a user's
    LOCALAPPDATA."""
    from groundtruth.remote import protocol, server
    monkeypatch.setenv("GROUNDTRUTH_HOME", str(tmp_path))
    assert protocol.default_key_file() == tmp_path / "server.key"
    assert server.settings_file() == tmp_path / "server.json" and server.default_data_dir() == tmp_path / "server_jobs"


def test_a_long_step_shows_its_output_and_says_it_is_still_working(capsys, monkeypatch):
    """The user: "nothing seems to happen for a while": a command's output is relayed as it comes (indented), and
    when it is silent for HEARTBEAT_S a line says it is still working; quiet commands show nothing unless they fail."""
    monkeypatch.setattr(server_setup, "HEARTBEAT_S", 0.3)
    monkeypatch.setitem(server_setup.DRY, "on", False)          # a dry-run test before may have left it on
    code = "import time; print('downloading torch (2602.8 MB)', flush=True); time.sleep(1.5); print('installed')"
    assert server_setup.run([sys.executable, "-c", code]) == 0
    out = capsys.readouterr().out
    assert "    downloading torch (2602.8 MB)" in out and "still working" in out and "    installed" in out
    assert server_setup.run([sys.executable, "-c", "print('secret detail')"], quiet=True) == 0
    assert "secret detail" not in capsys.readouterr().out
    with pytest.raises(SystemExit, match="the reason"):
        server_setup.run([sys.executable, "-c", "import sys; print('the reason'); sys.exit(3)"], quiet=True)


ROOT = Path(__file__).resolve().parents[1]


def test_no_cmd_file_echoes_a_bracket_inside_a_block():
    """cmd.exe ends a bracketed block at the first ) it meets, also inside an echo: install_server.cmd failed with
    ". was unexpected at this time." before doing anything (an echo "(it shows each step ...)" in an if-block)."""
    import re
    bad = []
    for f in list((ROOT / "tools").glob("*.cmd")) + list((ROOT / "plugin").glob("*.cmd")):
        depth = 0
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.strip()
            if code.lower().startswith("rem") or code.startswith("::"):
                continue
            if depth and code.lower().startswith("echo") and re.search(r"(?<!\^)[()]", code[4:]):
                bad.append(f"{f.name}:{n}: {code}")
            depth += len(re.findall(r"(?<!\^)\($", code)) - (1 if code.startswith(")") else 0)
    assert not bad, bad


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_install_server_cmd_runs_through_cmd_exe(tmp_path):
    """The wrapper itself through cmd.exe, without a UAC prompt: as a plain user it only says it elevates (PowerShell
    echoed), as administrator (net session replaced) it runs server_setup.py to the end (--dry-run)."""
    import glob
    import shutil
    import subprocess
    if not glob.glob(r"C:\Program Files\KiCad\*\bin\python.exe"):
        pytest.skip("KiCad is not installed")
    shutil.copy2(ROOT / "tools" / "server_setup.py", tmp_path / "server_setup.py")
    shutil.copy2(ROOT / "plugin" / "requirements.txt", tmp_path / "requirements.txt")
    shutil.copytree(ROOT / "src" / "groundtruth", tmp_path / "groundtruth", ignore=shutil.ignore_patterns("__pycache__"))
    src = (ROOT / "tools" / "install_server.cmd").read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    src = src.replace("\npause\n", "\n")
    for name, text in (("plain.cmd", src.replace("powershell -NoProfile", "echo ELEVATE powershell -NoProfile")),
                       ("admin.cmd", src.replace("net session >nul 2>&1", "cmd /c exit 0"))):
        (tmp_path / name).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    out = subprocess.run(["cmd", "/c", str(tmp_path / "plain.cmd"), "install", "--cuda", "cu126"], cwd=tmp_path,
                         capture_output=True, text=True, timeout=120)
    assert "unexpected" not in out.stdout + out.stderr and "ELEVATE" in out.stdout and "-Verb RunAs" in out.stdout
    out = subprocess.run(["cmd", "/c", str(tmp_path / "admin.cmd"), "install", "--dry-run"], cwd=tmp_path,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "unexpected" not in out.stdout + out.stderr
    assert "Registering and starting the service" in out.stdout and "Done in" in out.stdout


def test_the_status_is_a_tidy_block_with_the_gpus_cause_and_fix():
    """install_server.cmd status (and the end of install): one fact per line within the console's width, the GPU's
    cause in plain words and its one fix (the user, of the one long wrapped line: "this needs better formatting")."""
    st = {"host": "andre", "version": "GroundTruth 0.1.41230 (108793a, built 2026-10-14 18:41)", "code": "ff760539f86f",
          "cores": 16, "memory_gb": 31.7, "free_gb": 21.3, "gpus": [], "workers": 1, "running": [], "queue": [],
          "threads": "-", "gpu": "auto", "keep_hours": 24.0,
          "gpu_note": "PyTorch does not load (not installed: install_server.cmd install --cuda cu126; or a DLL "
                      "missing: the Microsoft Visual C++ Redistributable): ModuleNotFoundError: No module named 'torch'"}
    text = server_setup.status_block(st, 8765, "running; starts with Windows")
    lines = text.splitlines()
    assert all(len(ln) <= server_setup.WIDTH for ln in lines)
    assert "  Version:   GroundTruth 0.1.41230 (108793a), code ff760539f86f" in lines
    assert "  Why:       PyTorch is not installed in the service's environment" in lines
    assert any(ln.startswith("  Fix:       install_server.cmd install --cuda cu126") for ln in lines)
    assert "threads: as each user chooses" in text and "none running or queued" in text
    dll = server_setup.gpu_reason("PyTorch does not load (...): OSError: [WinError 126] ... shm.dll")
    assert "lacks a DLL" in dll[0] and "Visual C++" in dll[1]
    st.update(gpus=["NVIDIA RTX A2000"], gpu_note="")
    assert "  GPU:       NVIDIA RTX A2000" in server_setup.status_block(st, 8765).splitlines()
    # where the install is (the user: "add a path entry that shows where the binaries are stored on the PC"),
    # each path whole on its line
    root = Path(r"C:\ProgramData\GroundTruth")
    lines = server_setup.status_block(st, 8765, paths=server_setup.layout(root)).splitlines()
    assert f"  Path:      {root}" in lines
    assert any("program:" in ln and "app\\" in ln for ln in lines)
    assert any("Python:" in ln and "venv\\Scripts\\python.exe" in ln for ln in lines)
    assert any("logs:" in ln and "server.log" in ln for ln in lines)
    assert "Path:" not in server_setup.status_block(st, 8765)


def test_the_address_list_leaves_out_virtual_adapters(monkeypatch):
    """The users get the PC's real addresses: an adapter that is down, a Hyper-V/WSL vEthernet (the user's laptop
    showed 172.20.192.1), loopback and link-local are left out."""
    import socket
    from types import SimpleNamespace as NS
    psutil = pytest.importorskip("psutil")
    from groundtruth.remote.server import local_addresses

    def addr(ip):
        return NS(family=socket.AF_INET, address=ip)
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {
        "Ethernet": [addr("192.168.10.127")], "vEthernet (WSL (Hyper-V firewall))": [addr("172.20.192.1")],
        "Wi-Fi": [addr("169.254.3.4")], "Ethernet 2": [addr("10.1.1.5")], "Loopback Pseudo-Interface 1": [addr("127.0.0.1")]})
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {
        "Ethernet": NS(isup=True), "vEthernet (WSL (Hyper-V firewall))": NS(isup=True), "Wi-Fi": NS(isup=True),
        "Ethernet 2": NS(isup=False), "Loopback Pseudo-Interface 1": NS(isup=True)})
    assert local_addresses()[1:] == ["192.168.10.127"]


def test_the_cluster_key_is_shown_as_typed_cleaned_and_checked_on_the_network(tmp_path, monkeypatch, capsys):
    """install --key asks for the key with plain input (shown as typed or pasted: the user, "it's really hard to enter
    a key"), strips quotes, spaces and a copied "Access key:" label, writes it, and says which servers on the network
    use it, or that none answered (a typo makes a separate cluster)."""
    key = "_U47G28LvP2ZUjhYpVyI8T8wW6LzYO2pcQSZ5e52jjc"
    assert server_setup.clean_key(f'  "{key}"\r\n') == key
    assert server_setup.clean_key(f"Access key:      {key}") == key
    monkeypatch.setattr(server_setup, "DRY", {"on": False})
    monkeypatch.setattr("builtins.input", lambda prompt="": f"  {key} ")
    real_check = server_setup.key_check
    monkeypatch.setattr(server_setup, "key_check",
                        lambda paths, k: real_check(paths, k, find=lambda kk: [
                            {"host": "andre", "address": "192.168.10.127"}] if kk == key else []))
    paths = server_setup.layout(tmp_path)
    assert server_setup.make_key(paths, given=server_setup.ASK) == key
    assert paths["key"].read_text(encoding="utf-8").strip() == key
    out = capsys.readouterr().out
    assert "type or paste it here" not in out          # input()'s own prompt, not printed by say()
    assert "used by: andre (192.168.10.127)" in out and f"{len(key)} characters" in out
    assert "no server on this network answered" in real_check(paths, "x" * 20, find=lambda k: [])
    assert "could not ask" in real_check(paths, key, find=lambda k: 1 / 0)


def test_the_log_command_shows_the_end_of_the_logs_and_finds_lines(tmp_path, capsys):
    """install_server.cmd log: the last lines of server.log and service.log (the folder is admin-only, so Explorer
    can't open them: the user, "I can't access the log file"), --find keeps only the matching lines."""
    paths = server_setup.layout(tmp_path)
    paths["log"].write_text("\n".join(f"line {k}" for k in range(100)) + "\nabc123 runs whole: no other server\n",
                            encoding="utf-8")
    assert server_setup.cmd_log(paths, lines=5) == 0
    out = capsys.readouterr().out
    assert "line 98" in out and "line 94" not in out and "runs whole" in out and "service.log: not there yet" in out
    server_setup.cmd_log(paths, find="WHOLE")
    out = capsys.readouterr().out
    assert "abc123 runs whole" in out and "line 1" not in out and "the last 1 of 1" in out


def test_a_mac_install_is_a_launchd_daemon_with_root_only_files(tmp_path, capsys, monkeypatch):
    """macOS (the user: "the server should support macs", an Apple M2), planned with --dry-run --platform mac: the
    venv's bin/python3, the folder for root only (chown/chmod), the Application Firewall allowing the server's Python,
    a launchd daemon loaded and started; the messages name install_server.command; --cuda is ignored (no CUDA, no
    double precision on an Apple GPU); uninstall unloads the daemon and removes its plist."""
    import plistlib
    monkeypatch.setitem(server_setup.PLATFORM, "name", "windows")       # restored after the test
    monkeypatch.setattr(server_setup, "kicad_python", lambda: Path("/Applications/KiCad/python3"))
    root = tmp_path / "gt"
    assert server_setup.main(["install", "--platform", "mac", "--dir", str(root), "--dry-run", "--cuda", "cu126"]) == 0
    out = capsys.readouterr().out
    assert "--cuda ignored: a Mac has no CUDA" in out and "whl/cu126" not in out
    assert "chown -R root:wheel" in out and "chmod -R go-rwx" in out
    assert "socketfilterfw --add" in out and "socketfilterfw --unblockapp" in out
    assert "launchctl bootstrap system" in out and "launchctl kickstart -k system/com.groundtruth.server" in out
    assert str(Path("venv") / "bin" / "python3") in out and "install_server.command key" in out
    assert "schtasks" not in out and "netsh" not in out and "icacls" not in out and not root.exists()
    paths = server_setup.layout(root)
    plist = plistlib.loads(server_setup.launchd_plist(server_setup.venv_python(paths), paths))
    assert plist["Label"] == "com.groundtruth.server" and plist["RunAtLoad"] and plist["KeepAlive"]
    assert plist["ProgramArguments"] == [str(server_setup.venv_python(paths)), str(paths["runner"])]
    assert server_setup.main(["uninstall", "--platform", "mac", "--dir", str(root), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "launchctl bootout system/com.groundtruth.server" in out and "socketfilterfw --remove" in out
    assert "com.groundtruth.server.plist" in out
    why, fix = server_setup.gpu_reason("PyTorch does not load: No module named 'torch'")
    assert "double-precision" in why and "CPU solves" in fix


def test_a_mac_install_takes_a_python_new_enough(monkeypatch):
    """KiCad 10 for macOS brings Python 3.9 (the user's M2: the first job failed on itertools.pairwise): the install
    takes the first Python of 3.11 or newer, else stops and names python.org's installer."""
    monkeypatch.setitem(server_setup.PLATFORM, "name", "mac")
    kicad, org = Path("/Applications/KiCad/python3"), Path("/Library/Frameworks/3.13/python3")
    versions = {kicad: (3, 9), org: (3, 13)}
    monkeypatch.setattr(server_setup, "python_version", lambda p: versions.get(p))
    monkeypatch.setattr(server_setup, "mac_pythons", lambda: [kicad, org])
    assert server_setup.kicad_python() == org
    monkeypatch.setattr(server_setup, "mac_pythons", lambda: [kicad])
    with pytest.raises(SystemExit) as stop:
        server_setup.kicad_python()
    assert "3.11 or newer" in str(stop.value) and "python.org" in str(stop.value) and "(3.9)" in str(stop.value)


def test_python_version_asks_the_interpreter():
    """python_version runs the interpreter; one that does not run gives None."""
    assert server_setup.python_version(Path(sys.executable)) == tuple(sys.version_info[:2])
    assert server_setup.python_version(Path("no/such/python")) is None


def test_the_mac_launcher_is_lf_and_executable():
    """install_server.command: LF line endings (bash would read a CR as part of a word) and committed executable."""
    import subprocess
    data = (ROOT / "tools" / "install_server.command").read_bytes()
    assert b"\r" not in data and data.startswith(b"#!/bin/bash")
    mode = subprocess.run(["git", "ls-files", "-s", "tools/install_server.command"], cwd=ROOT, capture_output=True,
                          text=True).stdout.split()[:1]
    assert mode in (["100755"], [])                     # [] before it is committed


def test_the_package_keeps_the_mac_launcher_executable(tmp_path):
    """The zip says its entries come from Unix (else macOS ignores the mode and the launcher can't be run)."""
    import importlib.util
    import zipfile
    spec = importlib.util.spec_from_file_location("make_server_package", ROOT / "tools" / "make_server_package.py")
    pkg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pkg)
    out = tmp_path / "p.zip"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(pkg.zip_entry("p/install_server.command", executable=True), b"#!/bin/bash\n")
        z.writestr(pkg.zip_entry("p/server_setup.py"), b"")
    with zipfile.ZipFile(out) as z:
        launcher, script = z.infolist()
    assert launcher.create_system == script.create_system == 3
    assert launcher.external_attr >> 16 == 0o100755 and script.external_attr >> 16 == 0o100644
