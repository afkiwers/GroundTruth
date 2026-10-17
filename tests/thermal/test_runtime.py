"""Cancellation, persistence and the solver subprocess."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from thermal_boardgen import make_board  # noqa: E402
from groundtruth.thermal.kicad.board import load_board  # noqa: E402
from groundtruth.thermal.project import BoardSetup, ComponentPower, Project, Terminal  # noqa: E402
from groundtruth.thermal.solver.common import Cancelled, Progress  # noqa: E402
from groundtruth.thermal.solver.coupled import run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _board(tmp_path):
    zones = [(l, [(0, 0), (80, 0), (80, 60), (0, 60)]) for l in ("F.Cu", "B.Cu")]
    return make_board(tmp_path / "c.kicad_pcb", outline=(0, 0, 80, 60), zones=zones,
                      pads=[dict(ref="A", x=5, y=30, w=2, h=2), dict(ref="B", x=75, y=30, w=2, h=2)])


def _project(path):
    bs = BoardSetup(path.name, terminals=[Terminal("in", ["A.1"], "source", 5.0), Terminal("out", ["B.1"], "sink")],
                    components=[ComponentPower("A", 0.2)])
    bs.mesh.quality = "fine"
    return Project(boards=[bs])


def test_cooperative_cancel(tmp_path):
    f = _board(tmp_path)
    ev = threading.Event()
    seen = []

    def sink(msg):
        seen.append(msg)
        if len(seen) == 5:
            ev.set()

    t0 = time.perf_counter()
    with pytest.raises(Cancelled):
        run(load_board(f), _project(f), 0, Progress(sink, ev))
    assert time.perf_counter() - t0 < 10


def test_project_round_trip(tmp_path):
    p = _project(tmp_path / "x.kicad_pcb")
    p.environment.enclosure.enabled = True
    p.boards[0].materials.copper_thickness_override["F.Cu"] = 0.07
    path = tmp_path / "x.pcbsim.json"
    p.save(path)
    q = Project.load(path)
    assert q == p
