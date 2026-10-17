"""Runs a simulation in a separate low-priority process.

The GUI stays responsive, the solver can be cancelled cooperatively (checked at
every CG iteration) and, if it is stuck inside native code, terminated outright
without harming the application.
"""

from __future__ import annotations

import os
import sys
import traceback


def limit_threads(n: int) -> None:
    """Cap native thread pools. Must run before numpy/scipy are imported in the child."""
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS"):
        os.environ[var] = str(n)


def lower_priority() -> None:
    """Run this process below normal priority, so the user's other programs stay responsive (best effort)."""
    try:
        import psutil

        p = psutil.Process()
        if sys.platform == "win32":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(10)
    except Exception:
        pass


def solve_process(board_path: str, project_json: str, board_index: int, out_path: str, queue, cancel_event):
    """The child process's entry (PCB Thermal's app): read the board, run the coupled solve, save the results
    to out_path. Progress and the outcome go to the parent through `queue` as dicts; cancel_event stops it."""
    lower_priority()
    from pathlib import Path

    from ..kicad.board import load_board
    from ..project import Project
    from .common import Cancelled, Progress, SolverError
    from .coupled import run

    progress = Progress(queue.put, cancel_event)
    try:
        progress.stage("Reading board", 0.0, 0.02)
        board = load_board(board_path)
        project = Project.from_json(project_json)
        res = run(board, project, board_index, progress)
        progress.stage("Saving results", 0.99, 1.0)
        res.save(Path(out_path))
        queue.put({"type": "done", "path": out_path, "summary": res.summary})
    except Cancelled:
        queue.put({"type": "cancelled"})
    except SolverError as e:
        queue.put({"type": "error", "text": str(e)})
    except MemoryError:
        queue.put({"type": "error", "text": "Out of memory. Use Draft quality or a larger cell size."})
    except Exception as e:  # pragma: no cover - reported to the UI
        queue.put({"type": "error", "text": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()})
