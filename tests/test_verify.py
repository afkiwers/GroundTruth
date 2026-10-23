"""`groundtruth verify` (report/verify.py, cli.cmd_verify): a saved configuration simulated and judged against a
thresholds file for a CI gate; JUnit results, exit 0 pass / 1 fail / 2 setup error."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET

import pytest
from ruamel.yaml import YAML

from groundtruth.cli import main
from groundtruth.report import verify


def _doc(text: str) -> dict:
    """A thresholds document from YAML text."""
    return YAML(typ="safe").load(io.StringIO(text))


def test_the_template_is_a_valid_thresholds_file_and_typos_are_refused():
    """The documented template loads as it is (no thermal: its max_c is null). An unknown group, criterion or check,
    and a value that is not a number, are each refused by name: a misspelt key must not switch a criterion off."""
    doc = verify.check(_doc(verify.TEMPLATE))
    assert doc["issues"]["max_errors"] == 0 and not verify.needs_thermal(doc)
    bad = _doc("groundtruth_thresholds: 1\nnet: {}\nnets: {default: {radiated_margn_db: 3}}\n"
               "issues: {max_eror: 1, checks: {gap_crosing: 0, emi_margin: lots}}\nthermal: {max_c: 90, min_c: 1}\n")
    with pytest.raises(verify.ThresholdsError) as e:
        verify.check(bad)
    text = "\n".join(e.value.messages)
    for what in ("'net'", "radiated_margn_db", "max_eror", "gap_crosing", "'lots' is not a number", "min_c"):
        assert what in text, what
    with pytest.raises(verify.ThresholdsError, match="not a GroundTruth thresholds file"):
        verify.check({"nets": {}})


REPORT = {
    "issues": [{"check": "gap_crossing", "severity": "error", "waived": False},
               {"check": "gap_crossing", "severity": "error", "waived": True},       # waived: never counts
               {"check": "edge_proximity", "severity": "warning", "waived": False}],
    "routes": [{"net": "/CLK", "worst": {"margin_db": 3.0}, "l_ratio": 1.2, "error": ""},
               {"net": "/DATA", "worst": {"margin_db": 12.0}, "l_ratio": 2.5, "error": ""},
               {"net": "/BROKEN", "worst": {}, "error": "no route"}],
    "converters": [{"net": "buck", "worst": {"margin_db": 20.0}, "conducted_worst": None, "loop_nh": 4.0,
                    "overshoot_v": 2.0, "input": {"efficiency": 0.91}, "peak_density": {"F.Cu": 30.0, "In1.Cu": 55.0},
                    "error": ""}],
    "thermal": {"t_max": 96.5},
}


def test_each_criterion_passes_fails_or_is_skipped_on_the_report():
    """evaluate: issue counts (waived left out) per severity and per check; per item a 'simulated' case (failed with
    its error) and one case per criterion, a named entry replacing the default's value; a figure the run lacks is
    skipped; a named item the run does not have fails; the temperature against max_c."""
    doc = verify.check(_doc("""groundtruth_thresholds: 1
issues: {max_errors: 0, max_warnings: 5, checks: {gap_crossing: 1, edge_proximity: 0}}
nets:
  default: {radiated_margin_db: 6, return_path_ratio: 2.0}
  /CLK: {radiated_margin_db: 2}
  /GONE: {radiated_margin_db: 0}
converters:
  default: {conducted_margin_db: 0, loop_inductance_nh: 5, efficiency_pct: 95, current_density_a_mm2: 50}
thermal: {max_c: 100}
"""))
    cases = {(c.group, c.item, c.criterion): c for c in verify.evaluate(REPORT, doc)}
    status = {k: c.status for k, c in cases.items()}
    assert status[("issues", "issues", "errors")] == "failed" and cases[("issues", "issues", "errors")].value == 1
    assert status[("issues", "issues", "warnings")] == "passed"
    assert status[("issues", "issues", "gap_crossing")] == "passed"                      # 1 counted, the waived not
    assert status[("issues", "issues", "edge_proximity")] == "failed"
    assert status[("nets", "/CLK", "radiated_margin_db")] == "passed"                    # 3 dB >= its own 2 dB
    assert cases[("nets", "/CLK", "radiated_margin_db")].limit == 2.0
    assert status[("nets", "/DATA", "radiated_margin_db")] == "passed"
    assert status[("nets", "/DATA", "return_path_ratio")] == "failed"
    assert "above the limit 2" in cases[("nets", "/DATA", "return_path_ratio")].message
    assert status[("nets", "/BROKEN", "simulated")] == "failed" and ("nets", "/BROKEN", "radiated_margin_db") not in cases
    assert status[("nets", "/GONE", "in this run")] == "failed"
    assert status[("converters", "buck", "conducted_margin_db")] == "skipped"            # no conducted standard
    assert status[("converters", "buck", "loop_inductance_nh")] == "passed"
    assert status[("converters", "buck", "efficiency_pct")] == "failed"                  # 91 % < 95 %
    assert status[("converters", "buck", "current_density_a_mm2")] == "failed"           # 55 > 50 A/mm²
    assert status[("thermal", "board", "max_c")] == "passed"
    doc["thermal"]["max_c"] = 90
    assert {c.criterion: c.status for c in verify.evaluate(REPORT, doc) if c.group == "thermal"} == {"max_c": "failed"}
    xml = verify.junit(list(cases.values()), {"board": "b.kicad_pcb"})
    root = ET.fromstring(xml)
    assert int(root.get("failures")) == sum(c.status == "failed" for c in cases.values())
    assert int(root.get("skipped")) == 1 and root.find(".//testsuite/properties/property").get("value") == "b.kicad_pcb"
    names = [tc.get("name") for tc in root.iter("testcase")]
    assert "/CLK: radiated_margin_db >= 2 dB" in names
    summary = verify.summary(list(cases.values()))
    assert summary.startswith("verify: ") and "FAIL  [converters] buck: efficiency_pct >= 95 %" in summary


def test_verify_runs_a_configuration_and_gates_on_the_thresholds(tmp_path, slotted, capsys):
    """End to end on the slotted microstrip (SIG crosses a slot: a gap_crossing error and its emission): a saved
    configuration, a strict thresholds file -> exit 1 with the failures in the JUnit file; a lenient one -> exit 0;
    no common settings file, or an invalid thresholds file -> exit 2; --write-template writes the template (not over
    an existing file without --force). The common settings file is not changed."""
    from groundtruth.config import scenario
    _, sc, board = slotted
    before = sc.path.read_text(encoding="utf-8")
    cfg = scenario.save(tmp_path / "ci.gtconfig.yaml", scenario.extract(sc.doc, "ci"))
    strict = tmp_path / "strict.gtthresholds.yaml"
    strict.write_text("groundtruth_thresholds: 1\nissues: {max_errors: 0, checks: {gap_crossing: 0}}\n"
                      "nets: {default: {radiated_margin_db: 100}}\n", encoding="utf-8")
    out = tmp_path / "results"
    assert main(["verify", str(board), "--config", str(cfg), "--thresholds", str(strict), "--out-dir", str(out)]) == 1
    root = ET.parse(out / verify.JUNIT_NAME).getroot()
    failed = {tc.get("name") for tc in root.iter("testcase") if tc.find("failure") is not None}
    assert "issues: gap_crossing <= 0" in failed and "SIG: radiated_margin_db >= 100 dB" in failed
    assert "verify:" in capsys.readouterr().out
    lenient = tmp_path / "lenient.gtthresholds.yaml"
    lenient.write_text("groundtruth_thresholds: 1\nissues: {checks: {gap_crossing: 5}}\n"
                       "nets: {default: {radiated_margin_db: -100}}\n", encoding="utf-8")
    assert main(["verify", str(board), "--config", str(cfg), "--thresholds", str(lenient), "--out-dir", str(out)]) == 0
    assert sc.path.read_text(encoding="utf-8") == before
    bad = tmp_path / "bad.gtthresholds.yaml"
    bad.write_text("groundtruth_thresholds: 1\nnets: {default: {radiated: 1}}\n", encoding="utf-8")
    assert main(["verify", str(board), "--config", str(cfg), "--thresholds", str(bad), "--out-dir", str(out)]) == 2
    assert main(["verify", str(board), "--config", str(cfg), "--thresholds", str(strict), "--settings",
                 str(tmp_path / "missing.yaml"), "--out-dir", str(out)]) == 2
    assert "no common settings file" in capsys.readouterr().err
    tpl = tmp_path / "t.gtthresholds.yaml"
    assert main(["verify", "--write-template", str(tpl)]) == 0 and tpl.read_text(encoding="utf-8") == verify.TEMPLATE
    assert main(["verify", "--write-template", str(tpl)]) == 2
    assert main(["verify", "--write-template", str(tpl), "--force"]) == 0


def test_the_examples_template_is_the_one_verify_writes():
    """examples/release.gtthresholds.yaml (beside the CI example) is verify.TEMPLATE, so the two never drift."""
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "examples" / "release.gtthresholds.yaml"
    assert path.read_text(encoding="utf-8") == verify.TEMPLATE
