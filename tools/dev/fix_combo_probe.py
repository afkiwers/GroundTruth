"""Are the selected fixes' effects on a route a combination effect or a prediction-vs-verification difference?

Runs the baseline simulation, then predicts the route's worst margin over all harmonics near the limit
(`_WorstCheck`, low-rank updates on the baseline models) for each selected fix of a `groundtruth suggest --format json`
report alone and for all of them together. Compare with the report's verification row (rebuilt models, all fixes).
Read-only; nothing is written.

    .venv/Scripts/python tools/dev/fix_combo_probe.py BOARD SETTINGS SUGGESTIONS.json "/CPU/SPI_MOSI"

Session 3 case (kraft_flux_mini_2 as saved 18:35): SPI_MOSI verified +15.0 dB @ 820 MHz -> +11.0 dB @ 740 MHz with
S1+S2+S3, while each fix alone was predicted at -0.3 to +0.5 dB.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.adapters.file_source import load_board  # noqa: E402
from groundtruth.analysis.simulate import run_parallel, simulate, solver_threads  # noqa: E402
from groundtruth.analysis.suggest import _Evaluator, _WorstCheck  # noqa: E402
from groundtruth.config.modifications import Modification  # noqa: E402
from groundtruth.config.settings import SettingsFile  # noqa: E402


class _Multi:
    type = "via"


def main():
    """The command line (see the module): the baseline, then each selected fix alone and all together predicted for
    the route, then the report's verification row."""
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    board_path, settings, report, net = sys.argv[1:5]
    d = json.load(open(report, encoding="utf-8"))
    mods = {s["id"]: Modification.from_dict(s["modification"]) for s in d["suggestions"] if s["selected"]}
    print("selected fixes:", {k: (m.type, m.at, m.net) for k, m in mods.items()}, flush=True)
    board = load_board(board_path)
    cfg = SettingsFile.load(settings).config
    t = time.perf_counter()
    res = simulate(board, cfg)
    print(f"baseline {time.perf_counter() - t:.0f} s", flush=True)
    model = res.model

    def check(st, chosen):
        """Predict the route's worst margin with these fixes (low-rank updates on the baseline models, re-solving
        near the limit): (result, rows of baseline and predicted margin per frequency)."""
        ev = _Evaluator(model, st)
        orig = ev.branches_for
        ev.branches_for = lambda m, build: [b for mm in chosen for b in orig(mm, build)]
        w = _WorstCheck(ev, _Multi(), cfg, {})
        run_parallel(sorted(w.jobs, key=lambda j: j[1]), w.solve, solver_threads(cfg))
        r = w.result()
        if r is None:
            return None, []
        fine0 = w.sols.get(("fine", ev.f0), ev.base_fine[ev.f0])
        cav = dict(w.solved)
        cav.update({f: s for (k, f), s in w.sols.items() if k == "cav"})
        new = w._curve(fine0, cav)
        H = st.harmonics
        rows = []
        for f in w.fs:
            k = int(np.argmin(np.abs(H - f)))
            rows.append((round(f / 1e6), round(float(w.base[k]), 1), round(float(new[k]), 1),
                         "re-solved" if ("cav", f) in w.sols else ""))
        return r, rows

    for st in res.setups:
        if st.spec.name != net:
            continue
        for label, chosen in [(k, [m]) for k, m in mods.items()] + [("all", list(mods.values()))]:
            t = time.perf_counter()
            r, rows = check(st, chosen)
            print(f"{label}: {r} ({time.perf_counter() - t:.0f} s)", flush=True)
            if label == "all":
                print("    (MHz, baseline margin, predicted margin)")
                for row in rows:
                    print("    ", row)
    for row in d.get("verification", {}).get("routes", []):
        if row["net"] == net:
            print("verified (rebuilt models, all fixes):", row["worst_margin_db"], row["worst_f"])


if __name__ == "__main__":
    main()
