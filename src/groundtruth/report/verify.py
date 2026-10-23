"""`groundtruth verify`: a saved configuration run on the command line and judged against a thresholds file, for a
CI gate before production artefacts are made (the user: "execute sims via console passing in the sim config ... accept
a threshold file that defines pass/fail criteria ... return a response or file that contains the tests with their
pass/fail results ... run it via gitlab ci").

A thresholds file (`*.gtthresholds.yaml`, TEMPLATE below, `groundtruth verify --write-template F`) names criteria per
kind of result: the issues (counts by severity and per check), the signal paths, the power stages and the motor bridges
(each with a `default` and per-name entries), and the temperatures. Every criterion is optional; one that is null or
left out is not judged. Unknown keys are refused, so a typo can't switch a check off unnoticed.

Each criterion becomes one test case: passed, failed (with the value measured), or skipped (the run gave no such
figure, e.g. no conducted standard for a stage). An item that failed to simulate fails; a named item the run does not
have fails too (the configuration and the thresholds have drifted apart). The cases are judged on the JSON report the
run writes (report/formats.to_json), so verify sees exactly what `simulate --format json` reports. Results: JUnit XML
(GitLab's merge-request Tests tab, artifacts:reports:junit) and a summary on stdout.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import escape, quoteattr

FORMAT = 1
SUFFIX = ".gtthresholds.yaml"
JUNIT_NAME = "groundtruth-verify-junit.xml"

TEMPLATE = """\
# GroundTruth thresholds: pass / fail criteria for `groundtruth verify` (a CI gate).
#
#   groundtruth verify BOARD --config groundtruth/configs/<name>.gtconfig.yaml --thresholds <this file> \\
#       --out-dir results
#
# Exit code 0: every criterion passed; 1: one or more failed; 2: the run could not be set up (no common settings
# file next to the board, an invalid configuration or thresholds file). Results: results/groundtruth-verify-junit.xml
# (GitLab: artifacts:reports:junit) and a summary in the job log.
#
# Every criterion is optional: null (or a line left out) is not judged. Unknown keys are an error, so a misspelt one
# can't switch a check off unnoticed. Margins are "at least" (dB above the limit line: positive is under the limit);
# the other figures are "at most" unless the comment says "at least".
groundtruth_thresholds: 1

issues:                         # Run checks' and Simulate's issues; waived ones (and a --baseline's) never count
  max_errors: 0                 # at most this many errors
  max_warnings: null            # e.g. 10
  max_info: null
  checks:                       # per check, at most this many issues (a check's id as in the Issues tab and the report)
    gap_crossing: 0
    layer_change_stitch: 0
    clearance_voltage: 0
    # emi_margin: 0
    # victim_coupling: 0

nets:                           # the signal paths (Signal paths tab)
  default:                      # every path, unless named below
    radiated_margin_db: 0       # the worst radiated estimate's margin to its limit, at least (dB)
    return_path_ratio: null     # the return loop's inductance against an ideal plane under the route, at most (1.0:
                                # the return runs right under the track; e.g. 1.5)
  # "/CPU/SPI_SCK":             # a path of its own (its name as on the Signal paths tab): replaces default's values
  #   radiated_margin_db: 6

converters:                     # the power stages (Converters tab)
  default:
    radiated_margin_db: 0       # at least (dB)
    conducted_margin_db: 0      # the worst conducted estimate's margin (LISN, the stage's standard), at least (dB)
    loop_inductance_nh: null    # the hot loop's inductance, at most (nH)
    overshoot_v: null           # the switch node's overshoot L·di/dt, at most (V)
    efficiency_pct: null        # at least (%)
    current_density_a_mm2: null # the copper's highest DC current density, at most (A/mm²)
  # "buck 5V":
  #   loop_inductance_nh: 3

bridges:                        # the motor bridges (Bridges tab): the same figures as a power stage
  default:
    radiated_margin_db: 0
    conducted_margin_db: 0
    loop_inductance_nh: null
    overshoot_v: null
    efficiency_pct: null
    current_density_a_mm2: null

thermal:                        # set a value here and verify runs Thermal too (needs the [thermal] extra)
  max_c: null                   # the hottest copper or part, at most (°C)
"""

# criterion -> (direction ">=" at least | "<=" at most, unit, how to read it from an item of the JSON report)
_STAGE = {
    "radiated_margin_db": (">=", "dB", lambda d: (d.get("worst") or {}).get("margin_db")),
    "conducted_margin_db": (">=", "dB", lambda d: (d.get("conducted_worst") or {}).get("margin_db")),
    "loop_inductance_nh": ("<=", "nH", lambda d: d.get("loop_nh")),
    "overshoot_v": ("<=", "V", lambda d: d.get("overshoot_v")),
    "efficiency_pct": (">=", "%", lambda d: None if (d.get("input") or {}).get("efficiency") is None
                       else 100.0 * d["input"]["efficiency"]),
    "current_density_a_mm2": ("<=", "A/mm²", lambda d: max((d.get("peak_density") or {}).values(), default=None)),
}
CRITERIA: dict[str, dict[str, tuple[str, str, Callable]]] = {
    "nets": {"radiated_margin_db": _STAGE["radiated_margin_db"],
             "return_path_ratio": ("<=", "", lambda d: d.get("l_ratio"))},
    "converters": _STAGE,
    "bridges": _STAGE,
}
REPORT_KEY = {"nets": "routes", "converters": "converters", "bridges": "bridges"}
ISSUE_KEYS = {"max_errors": "error", "max_warnings": "warning", "max_info": "info"}


class ThresholdsError(Exception):
    """A thresholds file that can't be used: every problem in `messages`."""

    def __init__(self, messages: list[str]):
        """The problems, one per line."""
        super().__init__("; ".join(messages))
        self.messages = messages


@dataclass
class Case:
    """One criterion judged: its group (issues, nets, converters, bridges, thermal), the item it is about, the
    criterion with its direction, limit and unit, the value measured, and the outcome (passed, failed, skipped)."""
    group: str
    item: str
    criterion: str
    op: str = ""
    limit: Optional[float] = None
    unit: str = ""
    value: Optional[float] = None
    status: str = "passed"
    message: str = ""

    def name(self) -> str:
        """The test case's name: "<item>: <criterion> >= <limit> <unit>"."""
        lim = "" if self.limit is None else f" {self.op} {_num(self.limit)}{(' ' + self.unit) if self.unit else ''}"
        return f"{self.item}: {self.criterion}{lim}"


def _num(v: float) -> str:
    """A figure for a message: up to 4 significant digits."""
    return f"{v:.4g}"


def _is_number(v) -> bool:
    """A finite int or float (not a bool)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# --------------------------------------------------------------------------- the file
def load(path) -> dict:
    """Read and check a thresholds file. ThresholdsError lists every problem (unreadable, not YAML, not a thresholds
    file, unknown keys, values that are not numbers)."""
    from ruamel.yaml import YAML
    path = Path(path)
    try:
        doc = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ThresholdsError([f"cannot read {path}: {e}"]) from e
    except Exception as e:  # ruamel parser errors
        raise ThresholdsError([f"{path}: invalid YAML: {e}"]) from e
    return check(doc, str(path))


def check(doc, source: str = "thresholds") -> dict:
    """The thresholds as plain data, checked: the known groups and criteria only, numbers or null."""
    from ..core.issues import CHECKS
    if not isinstance(doc, dict) or "groundtruth_thresholds" not in doc:
        raise ThresholdsError([f"{source}: not a GroundTruth thresholds file (no groundtruth_thresholds: line)"])
    errors = []

    def number(where: str, v):
        """None, or a finite number (else an error)."""
        if v is not None and not _is_number(v):
            errors.append(f"{where}: {v!r} is not a number")

    known = {"groundtruth_thresholds", "issues", "thermal", *CRITERIA}
    errors += [f"{source}: unknown key {k!r} (known: {', '.join(sorted(known))})" for k in doc if k not in known]
    issues = doc.get("issues") or {}
    if not isinstance(issues, dict):
        errors.append("issues: must be a mapping")
        issues = {}
    for k, v in issues.items():
        if k in ISSUE_KEYS:
            number(f"issues.{k}", v)
        elif k == "checks":
            for c, n in (v or {}).items():
                if c not in CHECKS:
                    errors.append(f"issues.checks: unknown check {c!r}")
                number(f"issues.checks.{c}", n)
        else:
            errors.append(f"issues: unknown key {k!r} (known: {', '.join([*ISSUE_KEYS, 'checks'])})")
    for group, crits in CRITERIA.items():
        entries = doc.get(group) or {}
        if not isinstance(entries, dict):
            errors.append(f"{group}: must be a mapping of 'default' and item names")
            continue
        for name, values in entries.items():
            if not isinstance(values, dict):
                errors.append(f"{group}.{name}: must be a mapping of criteria")
                continue
            for k, v in values.items():
                if k not in crits:
                    errors.append(f"{group}.{name}: unknown criterion {k!r} (known: {', '.join(crits)})")
                number(f"{group}.{name}.{k}", v)
    thermal = doc.get("thermal") or {}
    for k, v in (thermal.items() if isinstance(thermal, dict) else []):
        if k != "max_c":
            errors.append(f"thermal: unknown key {k!r} (known: max_c)")
        number(f"thermal.{k}", v)
    if errors:
        raise ThresholdsError(errors)
    return doc


def needs_thermal(doc: dict) -> bool:
    """A temperature criterion is set: the run includes Thermal."""
    return (doc.get("thermal") or {}).get("max_c") is not None


# --------------------------------------------------------------------------- judging
def _judge(case: Case) -> Case:
    """Set a case's outcome from its value and limit."""
    if case.value is None:
        case.status, case.message = "skipped", "not measured in this run"
    elif (case.value >= case.limit) if case.op == ">=" else (case.value <= case.limit):
        case.status = "passed"
        case.message = f"{_num(case.value)}{(' ' + case.unit) if case.unit else ''}"
    else:
        case.status = "failed"
        case.message = (f"measured {_num(case.value)}{(' ' + case.unit) if case.unit else ''}, "
                        f"{'below' if case.op == '>=' else 'above'} the limit {_num(case.limit)}")
    return case


def evaluate(report: dict, doc: dict) -> list[Case]:
    """Every criterion of the thresholds `doc` judged on a JSON report (formats.to_json's data)."""
    cases: list[Case] = []
    issues = [i for i in report.get("issues") or [] if not i.get("waived")]
    th = doc.get("issues") or {}
    for key, severity in ISSUE_KEYS.items():
        if th.get(key) is not None:
            n = sum(1 for i in issues if i.get("severity") == severity)
            cases.append(_judge(Case("issues", "issues", f"{severity}s", "<=", th[key], "", n)))
    for check_id, limit in (th.get("checks") or {}).items():
        if limit is not None:
            n = sum(1 for i in issues if i.get("check") == check_id)
            cases.append(_judge(Case("issues", "issues", check_id, "<=", limit, "", n)))
    for group, crits in CRITERIA.items():
        entries = doc.get(group) or {}
        if not entries:
            continue
        items = {str(d.get("net", "")): d for d in report.get(REPORT_KEY[group]) or []}
        default = entries.get("default") or {}
        for name in [n for n in entries if n != "default" and n not in items]:
            cases.append(Case(group, name, "in this run", status="failed",
                              message="named in the thresholds but not simulated (not in the configuration, or "
                                      "switched off)"))
        for name, d in items.items():
            values = {**default, **(entries.get(name) or {})}
            if not any(v is not None for v in values.values()):
                continue
            ok = not d.get("error")
            cases.append(Case(group, name, "simulated", status="passed" if ok else "failed",
                              message="" if ok else str(d.get("error"))))
            for crit, limit in values.items():
                if limit is None or not ok:
                    continue
                op, unit, read = crits[crit]
                value = read(d)
                cases.append(_judge(Case(group, name, crit, op, float(limit), unit,
                                         None if value is None else float(value))))
    max_c = (doc.get("thermal") or {}).get("max_c")
    if max_c is not None:
        t = report.get("thermal") or {}
        value = t.get("t_max")
        case = _judge(Case("thermal", "board", "max_c", "<=", float(max_c), "°C",
                           None if value is None else float(value)))
        if not t:
            case.status, case.message = "failed", "Thermal did not run (no power stage or bridge, or it failed)"
        cases.append(case)
    return cases


# --------------------------------------------------------------------------- results
def junit(cases: list[Case], meta: dict) -> str:
    """JUnit XML: one test suite per group, one test case per criterion (failure: the value against the limit;
    skipped: not measured). meta (board, configuration, thresholds, version) goes into each suite's properties."""
    groups: dict[str, list[Case]] = {}
    for c in cases:
        groups.setdefault(c.group, []).append(c)
    total_f = sum(c.status == "failed" for c in cases)
    total_s = sum(c.status == "skipped" for c in cases)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuites name="groundtruth verify" tests="{len(cases)}" failures="{total_f}" skipped="{total_s}">']
    props = "".join(f'<property name={quoteattr(str(k))} value={quoteattr(str(v))}/>' for k, v in meta.items())
    for group, cs in groups.items():
        out.append(f'  <testsuite name={quoteattr("groundtruth." + group)} tests="{len(cs)}" '
                   f'failures="{sum(c.status == "failed" for c in cs)}" '
                   f'skipped="{sum(c.status == "skipped" for c in cs)}">')
        out.append(f"    <properties>{props}</properties>")
        for c in cs:
            head = f'    <testcase classname={quoteattr("groundtruth." + group)} name={quoteattr(c.name())}'
            if c.status == "failed":
                out.append(f"{head}><failure message={quoteattr(c.message)}>{escape(c.message)}</failure></testcase>")
            elif c.status == "skipped":
                out.append(f"{head}><skipped message={quoteattr(c.message)}/></testcase>")
            else:
                out.append(f"{head}><system-out>{escape(c.message)}</system-out></testcase>")
        out.append("  </testsuite>")
    out.append("</testsuites>")
    return "\n".join(out) + "\n"


def summary(cases: list[Case]) -> str:
    """The job log's summary: the counts, then every failed and skipped case with its reason."""
    failed = [c for c in cases if c.status == "failed"]
    skipped = [c for c in cases if c.status == "skipped"]
    lines = [f"verify: {len(cases) - len(failed) - len(skipped)} passed, {len(failed)} failed, "
             f"{len(skipped)} skipped"]
    lines += [f"  FAIL  [{c.group}] {c.name()}: {c.message}" for c in failed]
    lines += [f"  skip  [{c.group}] {c.name()}: {c.message}" for c in skipped]
    return "\n".join(lines)
