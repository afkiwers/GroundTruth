"""Power stages of a board, analysed from the command line with test parameters (development aid).

Detects the converters (or takes the settings's), overrides their operating point, simulates them and prints the
hot-loop results, capacitor sharing, conducted/radiated worst cases and timing. Nothing is written next to the
board; the settings is only read.

    .venv/Scripts/python tools/dev/power_check.py BOARD SETTINGS [--only SW1,SW2] [--f-sw 500k] [--i-out 5]
        [--v-in 24] [--c-oss 600p] [--rise 2n] [--cap] [--budget 100000] [--series Q601] [--connector J101.2,J101.1]
        [--output J104.2,J104.1]

--series REF gives a series switch (e.g. a surge-stopper FET between the battery input and the rail) a 5 mOhm
resistor model in memory, so the conducted path from --connector (plus, minus) reaches the converters.
--output sets the load's pins (plus, minus) for the DC output path and ground return (detection finds a connector
on the output net; this overrides it).
--cap turns the board-wide plane-capacitance model on (slower; off by default here).
"""
from __future__ import annotations

import argparse
import copy
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.adapters.file_source import load_board  # noqa: E402
from groundtruth.analysis.detect import detect_converters  # noqa: E402
from groundtruth.analysis.simulate import simulate  # noqa: E402
from groundtruth.components.models import ComponentModel  # noqa: E402
from groundtruth.components.values import parse_value  # noqa: E402
from groundtruth.config.settings import ComponentEntry, SettingsFile  # noqa: E402
from groundtruth.report.formats import converter_text  # noqa: E402


def num(text, unit=""):
    """A number from the command line in SI notation, an optional unit after it (None stays None); exits when
    unreadable."""
    if text is None:
        return None
    t = text.strip()
    if unit and t.lower().endswith(unit.lower()):
        t = t[: -len(unit)]
    v = parse_value(t)
    if v is None:
        raise SystemExit(f"cannot read {text!r}")
    return v


def main():
    """The command line (see the module): detect (or read) the power stages, override their operating point and
    parts with the options, simulate them and print each stage's results."""
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", help="comma-separated parts of converter names to keep")
    ap.add_argument("--from-settings", action="store_true", help="use the settings's converters instead of detection")
    ap.add_argument("--f-sw")
    ap.add_argument("--i-out")
    ap.add_argument("--v-in")
    ap.add_argument("--c-oss", help="applied to discrete stages (with a switch node)")
    ap.add_argument("--rise")
    ap.add_argument("--cap", action="store_true", help="board-wide plane-capacitance model on")
    ap.add_argument("--budget", type=int, help="analysis.max_nodes of the converter models")
    ap.add_argument("--series", help="comma-separated series parts to model as 5 mOhm (input path)")
    ap.add_argument("--connector", help="input connector pins 'PLUS,MINUS' for the conducted estimate")
    ap.add_argument("--output", help="the load's pins 'PLUS,MINUS' (DC output path and ground return)")
    ap.add_argument("--standard", help="cispr32 | cispr25 | mil461")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    board = load_board(a.board)
    cfg = copy.copy(SettingsFile.load(a.settings).config)
    cfg.analysis = copy.copy(cfg.analysis)
    cfg.analysis.plane_capacitance = a.cap
    if a.budget:
        cfg.analysis.max_nodes = a.budget
    if a.standard:
        cfg.analysis.conducted = a.standard
    cfg.components = dict(cfg.components)
    for ref in (a.series.split(",") if a.series else []):
        old = cfg.components.get(ref)
        cfg.components[ref] = ComponentEntry(ref, old.value if old else "", old.footprint if old else "",
                                             ComponentModel("resistor", {"R": 0.005}, "in-memory series model"))
    specs = list(cfg.converters) if a.from_settings else [d.spec for d in detect_converters(board, cfg)]
    keep = []
    for s in specs:
        if a.only and not any(o in s.name for o in a.only.split(",")):
            continue
        s = copy.copy(s)
        s.reviewed = True
        if a.f_sw:
            s.f_sw = num(a.f_sw, "Hz")
        if a.i_out:
            s.i_out = num(a.i_out, "A")
        if a.v_in:
            s.v_in = num(a.v_in, "V")
        if a.rise:
            s.rise_time = num(a.rise, "s")
        if a.c_oss and s.switch_node:
            s.c_oss = num(a.c_oss, "F")
        if a.connector:
            s.input_connector = [p.strip() for p in a.connector.split(",")]
        if a.output:
            s.output_connector = [p.strip() for p in a.output.split(",")]
        keep.append(s)
    cfg.converters, cfg.nets = keep, []
    print(f"{len(keep)} converter(s): {', '.join(s.name for s in keep)}", flush=True)

    def progress(msg, frac):
        """Print a progress line."""
        print(f"  [{frac * 100:5.1f}%] {msg}", flush=True)

    t = time.perf_counter()
    res = simulate(board, cfg, progress=progress if a.verbose else None)
    print(f"total {time.perf_counter() - t:.1f}s")
    for c in res.converters:
        print("\n".join(converter_text(c)))
    for i in res.issues:
        print(f"{i.severity:8s} {i.check:20s} {i.message}")


if __name__ == "__main__":
    main()
