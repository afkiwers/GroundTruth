"""The KiCad plugin's packaging and life cycle, without KiCad: every action has an entry script of its own (KiCad 10
starts it without plugin.json's "args"), the start-up log records each step without ever failing, and the window
finds the KiCad that started it, to close when it exits."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from groundtruth.ui import startup

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"


def test_each_action_has_its_own_entry_script():
    spec = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    scripts = {}
    for action in spec["actions"]:
        assert "args" not in action, f"{action['identifier']}: KiCad 10 does not pass args to the entry script"
        script = PLUGIN / action["entrypoint"]
        assert script.exists(), script
        scripts[action["identifier"]] = script.read_text(encoding="utf-8")
    assert len({a["entrypoint"] for a in spec["actions"]}) == len(spec["actions"])
    assert re.search(r"plugin_main\(sys\.argv\[1:\]", scripts["analyse"])
    assert re.search(r'plugin_main\(\["clear"\]', scripts["clear"])
    assert re.search(r'plugin_main\(\["add-net"\]', scripts["add-net"])
    for text in scripts.values():           # the start-up log's clock starts before any other import
        assert "import" not in text[:text.index("import time")]
        assert text.index("import time") < text.index("STARTED = time.time()") < text.index("import os")


def test_startup_log_records_steps_and_stays_small(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "plugin.log"
    monkeypatch.setattr(startup, "MAX_BYTES", 2000)
    for _ in range(40):
        log = startup.StartupLog("analyse", path=path)
        startup._current = log
        startup.mark("connected to KiCad")
        startup.mark("board read (12 parts)", last=True)
        startup.mark("not logged: start-up is over")
    text = path.read_text(encoding="utf-8")
    assert path.stat().st_size < 2000 + 200
    assert "not logged" not in text and text.splitlines()[-1].endswith("board read (12 parts)")
    head = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  analyse  \(PID \d+, Python [\d.]+\)$")
    step = re.compile(r"^ +\d+\.\d\d s  (process created|script started|connected to KiCad|board read \(12 parts\))$")
    for line in text.splitlines():                          # trimming keeps whole lines only
        assert head.match(line) or step.match(line), line


def test_startup_log_never_fails(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    log = startup.StartupLog("analyse", path=blocker / "plugin.log")   # the parent is a file: no log, no error
    log.mark("still fine")
    startup.mark("no log active")


def test_finds_kicad_above_the_venv_launcher():
    """The chain seen live on Windows: KiCad's pythonw.exe <- the venv's launcher pythonw.exe <- kicad.exe."""
    from groundtruth.ui.kicad_watch import find_kicad
    procs = {23124: (25440, "pythonw.exe"), 25440: (27156, "pythonw.exe"), 27156: (4100, "kicad.exe"),
             4100: (1, "explorer.exe")}
    assert find_kicad(procs, 23124) == 27156
    assert find_kicad({**procs, 27156: (4100, "pcbnew.exe")}, 23124) == 27156     # the standalone PCB editor
    assert find_kicad({**procs, 27156: (4100, "cmd.exe")}, 23124) is None          # started by hand
    assert find_kicad({1: (1, "a.exe"), 2: (1, "b.exe")}, 2) is None               # a cycle ends


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_notices_when_a_process_exits():
    from groundtruth.ui.kicad_watch import _process_table, exited_check, watch_kicad
    table = _process_table()
    assert table[os.getpid()][0] == os.getppid()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        exited = exited_check(child.pid)
        assert exited is not None and not exited()
        child.kill()
        child.wait(10)
        assert exited()
    finally:
        child.kill()
    assert watch_kicad() is None                      # pytest was not started by KiCad: nothing to watch


def test_inbox_hands_kicad_selections_to_the_open_window(tmp_path):
    """KiCad's 'Add selected net' button runs as a process of its own: it leaves the nets in the board's inbox,
    and the board's window (another process) takes each request once. With no window open, or once its process
    has ended, the request waits for the window the button opens."""
    from groundtruth.ui import inbox
    board = tmp_path / "b.kicad_pcb"
    assert not inbox.send(board, ["/A"], wait=0.2)                  # no window: the request waits ...
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        window = inbox.Inbox(board)
        assert window.take() == [["/A"]] and window.take() == []      # ... and is taken once
        assert inbox.open_window_pid(board) is None                  # a sender never waits for its own process
        data = json.loads(window.window_file.read_text(encoding="utf-8"))
        data["pid"] = child.pid                                       # the window as another, running process
        window.window_file.write_text(json.dumps(data), encoding="utf-8")
        taken = []

        def the_window():
            for _ in range(150):
                got = window.take()
                if got:
                    taken.extend(got)
                    return
                time.sleep(0.02)
        t = threading.Thread(target=the_window)
        t.start()
        same_board = str(board).upper() if sys.platform == "win32" else str(board)   # any spelling of the path
        assert inbox.send(same_board, ["/B", "/C"], wait=5)
        t.join()
        assert taken == [["/B", "/C"]]
        (window.requests / "1-bad.json").write_text("{not json", encoding="utf-8")
        (window.requests / "2-bad.json").write_text(json.dumps({"nets": [1, "", "x" * 400]}), encoding="utf-8")
        assert window.take() == [] and not list(window.requests.glob("*.json"))   # malformed: dropped
    finally:
        child.kill()
        child.wait(10)
    assert not inbox.send(board, ["/D"], wait=0.2)                  # that window's process has ended
    own = inbox.Inbox(board)
    own.close()
    assert not own.window_file.exists()


def test_inbox_requests_carry_the_selected_pads_checked(tmp_path):
    """A selection of several nets also sends its selected pads' ids (a path's end pins); the window gets them
    with the nets. Anything that is not an id-shaped string is dropped (the file comes from another process),
    and an older request without pads still works."""
    from groundtruth.ui import inbox
    board = tmp_path / "b.kicad_pcb"
    pad = "a78a4ec5-76b7-4820-a139-13ac82db1875"
    inbox.send(board, ["/A", "/B"], wait=0, pads=[pad, "../../etc", 7])
    window = inbox.Inbox(board)
    (window.requests / "9-old.json").write_text(json.dumps({"nets": ["/C"]}), encoding="utf-8")
    assert window.take_requests() == [{"nets": ["/A", "/B"], "pads": [pad]}, {"nets": ["/C"], "pads": []}]
    window.close()
