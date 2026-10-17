import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from boardgen import discrete_buck_board, microstrip_board
from conftest import make
from groundtruth.cli import main
from groundtruth.config.settings import SettingsFile
from groundtruth.rules.checks import run_checks

SRC = str(Path(__file__).resolve().parent.parent / "src")


def run(*args):
    return main([str(a) for a in args])


def test_exit_codes(tmp_path, solid, slotted):
    _, _, solid_path = solid
    _, _, slot_path = slotted
    assert run("check", solid_path, "-q") == 0
    assert run("check", slot_path, "-q") == 1
    assert run("check", slot_path, "-q", "--fail-on", "error") == 1
    assert run("check", tmp_path / "missing.kicad_pcb", "-q") == 2
    bad = tmp_path / "groundtruth" / "slot.groundtruth.yaml"
    bad.write_text("nets: [{name: SIG, driver: U1.1, receivers: [U2.1], freq: fast, rise_time: 1ns}]\n")
    assert run("check", slot_path, "-q") == 2


def test_report_formats(tmp_path, slotted):
    _, _, path = slotted
    out = tmp_path / "out"
    rc = run("check", path, "-q", "--format", "gitlab-codequality", "--format", "junit", "--format", "json",
             "--out-dir", out)
    assert rc == 1
    cq = json.loads((out / "gl-code-quality-report.json").read_text(encoding="utf-8"))
    assert len(cq) == 1
    for key in ("description", "check_name", "fingerprint", "severity", "location"):
        assert key in cq[0]
    assert cq[0]["severity"] == "major" and cq[0]["location"]["lines"]["begin"] > 1
    suite = ET.parse(out / "groundtruth-junit.xml").getroot()
    assert suite.find(".//failure") is not None
    data = json.loads((out / "groundtruth-report.json").read_text(encoding="utf-8"))
    assert data["summary"]["error"] == 1


def test_baseline_suppresses_known_issues(tmp_path, slotted):
    _, _, path = slotted
    base = tmp_path / "baseline.json"
    assert run("check", path, "-q", "--write-baseline", base) == 1
    assert run("check", path, "-q", "--baseline", base) == 0


def test_fingerprint_stable_when_unrelated_track_moves(tmp_path):
    g1 = microstrip_board(slot=(19.5, 4, 20.5, 16))
    board, sc, _ = make(tmp_path, g1, "a")
    fp1 = run_checks(board, sc.config).issues[0].fingerprint
    # add an unrelated track and re-generate positions of other GND stubs
    text = (tmp_path / "a.kicad_pcb").read_text(encoding="utf-8").replace("(start 5.0 11.0)", "(start 5.0 11.2)")
    text = text.replace(")\n)\n", ")\n\t(segment (start 1 1) (end 2 1) (width 0.2) (layer \"F.Cu\") (net \"X\") "
                                  "(uuid \"0000\"))\n)\n")
    (tmp_path / "a.kicad_pcb").write_text(text, encoding="utf-8")
    from groundtruth.adapters.file_source import load_board
    board2 = load_board(tmp_path / "a.kicad_pcb")
    assert run_checks(board2, sc.config).issues[0].fingerprint == fp1


def test_simulate_with_margin_gate(tmp_path):
    _, _, path = make(tmp_path, microstrip_board(slot=(19.5, 4, 20.5, 20.5)), "split")
    out = tmp_path / "o"
    assert run("simulate", path, "-q", "--format", "html", "--out-dir", out) == 1     # gap_crossing error
    html = (out / "groundtruth-report.html").read_text(encoding="utf-8")
    assert "data:image/png;base64" in html and "gap_crossing" in html
    assert list(out.glob("heatmap_*.png")) and list(out.glob("section_*.png"))
    (page,) = out.glob("view3d_*.html")                 # the 3D page, linked from the report
    assert f"href='{page.name}'" in html
    assert run("simulate", path, "-q", "--fail-on", "error", "--max-dbuv-margin", "60") == 1


def test_init_and_merge(tmp_path):
    """The settings go into the board's groundtruth folder; nothing else lands next to the board. A settings file
    beside the board (the layout before 2026-10-10) is not read."""
    path = microstrip_board().write(tmp_path / "n.kicad_pcb")
    (tmp_path / "n.groundtruth.yaml").write_text("nets: [{name: X, driver: U1.1, receivers: [U2.1], freq: 1MHz, "
                                                 "rise_time: 1ns}]\n", encoding="utf-8")
    assert run("init", path) == 0
    assert (tmp_path / "groundtruth" / "n.groundtruth.yaml").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["groundtruth", "n.groundtruth.yaml", "n.kicad_pcb"]
    assert SettingsFile.load(tmp_path / "groundtruth" / "n.groundtruth.yaml").config.nets == []
    assert run("init", path) == 0       # merge path
    assert run("check", path, "-q") == 0   # no nets yet: the board-wide checks only (voltage-aware safety ...)
    assert run("simulate", path, "-q") == 2     # nothing to simulate -> configuration error


def test_cli_never_imports_gui_or_kipy(tmp_path, slotted):
    _, _, path = slotted
    code = ("import sys; from groundtruth.cli import main; rc = main(['check', sys.argv[1], '-q']); "
            "bad = [m for m in ('wx', 'kipy') if m in sys.modules]; print(rc, bad); sys.exit(3 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True,
                          env={"PYTHONPATH": SRC, "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_detect_and_simulate_a_converter(tmp_path, capsys):
    path = discrete_buck_board().write(tmp_path / "buck.kicad_pcb")
    assert run("init", path) == 0
    assert run("detect", path, "--write") == 0
    assert "converter buck SW" in capsys.readouterr().out
    assert [c.name for c in SettingsFile.load(tmp_path / "groundtruth" / "buck.groundtruth.yaml").config.converters] == ["buck SW"]
    out = tmp_path / "o"
    rc = run("simulate", path, "-q", "--format", "json", "--format", "junit", "--format", "html", "--out-dir", out)
    assert rc == 1                                   # no input filter: far above the conducted limit
    data = json.loads((out / "groundtruth-report.json").read_text(encoding="utf-8"))
    conv = data["converters"][0]
    assert conv["kind"] == "converter" and conv["loop_nh"] > 0 and conv["conducted_worst"]["margin_db"] < 0
    assert any(i["check"] == "conducted_emission" for i in data["issues"])
    cases = [tc.get("name") for tc in ET.parse(out / "groundtruth-junit.xml").getroot().iter("testcase")]
    assert any(c.startswith("conducted_emission") for c in cases)
    assert "Hot loop" in (out / "groundtruth-report.html").read_text(encoding="utf-8")
    assert run("check", path, "-q") == 0             # no nets: the board-wide checks only; converters need simulate


def test_simulate_thermal_reports_temperatures_and_gates_on_them(tmp_path, capsys):
    """groundtruth simulate --thermal: the power stages' temperatures after the EMI analysis; its issues count for
    --fail-on like the others, the text output summarises the hottest parts, the PNGs include the map.
    --capacity adds each stage's current capacity (here to max_c 40 °C: under the 5 A operating point)."""
    pytest.importorskip("pyamg")
    from groundtruth.analysis.detect import detect_converters
    board, sc, path = make(tmp_path, discrete_buck_board(out_connector=True), "buck", nets=(), solve_points=1)
    spec = detect_converters(board, sc.config)[0].spec
    spec.i_out, spec.v_in, spec.v_out, spec.rds_on, spec.dcr = 5.0, 24.0, 5.0, 0.010, 0.020
    sc.add_detected_converters([spec])
    sc.doc["thermal"] = {"quality": "draft", "warn_c": 30.0, "max_c": 40.0}
    sc.save()
    out = tmp_path / "o"
    code = run("simulate", path, "--thermal", "--capacity", "--format", "text", "--format", "html", "--out-dir", out)
    text = capsys.readouterr().out
    assert "thermal: hottest copper" in text and "L1" in text and "thermal_hotspot" in text
    assert code == 1                                          # 40 °C limit: an error
    assert (out / "heatmap_thermal.png").exists() and (out / "spectrum_thermal.png").exists()
    lines = text.splitlines()
    head = next(k for k, l in enumerate(lines) if l.startswith("current capacity ("))
    line = lines[head + 1].strip()
    assert line.startswith(spec.name)
    assert "current capacity (the hottest copper or part junction at 40 °C" in text
    assert "all power stages" not in text                      # one stage: no "together" row
    amps = float(line.split()[len(spec.name.split())])
    assert 0 < amps < 5.0 and "operating point 5 A" in line and "limited by" in line
    assert list(out.glob("heatmap_thermal_capacity_*.png"))
