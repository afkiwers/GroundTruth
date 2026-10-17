"""Does the baseline peak inside a gap between its solved harmonics? (follow-up of fix_combo_probe.py)

Runs the baseline simulation, solves one route's board-wide model at extra harmonics (default 700, 740, 780 MHz),
and prints its margin per solved frequency before and after adding them; then, with a suggestions report, the
margin there with all selected fixes (low-rank prediction). Read-only.

    .venv/Scripts/python tools/dev/gap_probe.py BOARD SETTINGS SUGGESTIONS.json "/CPU/SPI_MOSI" [700e6,740e6,780e6]

If the baseline already reads about the verification's worst margin in the gap, a before/after comparison of
differently sampled spectra was the cause, not the fixes.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.adapters.file_source import load_board  # noqa: E402
from groundtruth.analysis.simulate import _margin_curve, band_moments, run_parallel, simulate, solver_threads  # noqa: E402
from groundtruth.analysis.suggest import _Evaluator, _WorstCheck  # noqa: E402
from groundtruth.config.modifications import Modification  # noqa: E402
from groundtruth.config.settings import SettingsFile  # noqa: E402


class _Multi:
    type = "via"


def margins(st, cfg):
    """{MHz: margin dB} at each frequency the route's board-wide model has solved."""
    fs, ms = band_moments(st)
    curve = _margin_curve(st, cfg, fs, ms)
    H = st.harmonics
    return {round(f / 1e6): round(float(curve[int(np.argmin(np.abs(H - f)))]), 1) for f in fs}


def main():
    """The command line (see the module): the baseline margins as solved, again with the extra frequencies solved,
    then with all selected fixes predicted, and the report's verification row."""
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    board_path, settings, report, net = sys.argv[1:5]
    extra = [float(x) for x in (sys.argv[5] if len(sys.argv) > 5 else "700e6,740e6,780e6").split(",")]
    d = json.load(open(report, encoding="utf-8"))
    mods = [Modification.from_dict(s["modification"]) for s in d["suggestions"] if s["selected"]]
    board = load_board(board_path)
    cfg = SettingsFile.load(settings).config
    t = time.perf_counter()
    res = simulate(board, cfg)
    print(f"baseline {time.perf_counter() - t:.0f} s", flush=True)
    st = next(s for s in res.setups if s.spec.name == net)
    print("baseline, as solved:", margins(st, cfg), flush=True)
    run = st.spectrum_run
    for f in extra:
        if f not in run.sols:
            run.sols[f] = run.exc.solve(f)
    print("baseline with the gap solved:", margins(st, cfg), flush=True)
    ev = _Evaluator(res.model, st)
    orig = ev.branches_for
    ev.branches_for = lambda m, build: [b for mm in mods for b in orig(mm, build)]
    w = _WorstCheck(ev, _Multi(), cfg, {})
    run_parallel(sorted(w.jobs, key=lambda j: j[1]), w.solve, solver_threads(cfg))
    print("all selected fixes (predicted worst):", w.result(), flush=True)
    fine0 = w.sols.get(("fine", ev.f0), ev.base_fine[ev.f0])
    cav = dict(w.solved)
    cav.update({f: s for (k, f), s in w.sols.items() if k == "cav"})
    new = w._curve(fine0, cav)
    H = st.harmonics
    print("with all fixes:", {round(f / 1e6): round(float(new[int(np.argmin(np.abs(H - f)))]), 1) for f in w.fs})
    for row in d.get("verification", {}).get("routes", []):
        if row["net"] == net:
            print("verified (rebuilt models, all fixes):", row["worst_margin_db"], row["worst_f"])


if __name__ == "__main__":
    main()
