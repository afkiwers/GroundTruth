"""The cross-section along a route (analysis/section.py) on two grid budgets: is it converged?

    .venv/Scripts/python tools/dev/section_check.py BOARD SETTINGS --only NET [--budgets 100000,200000] [--out DIR]

Simulates the net's route model (plane capacitance off: the section comes from the route model alone) once per
analysis.max_nodes budget, writes each section as DIR/section_<budget>.png, and prints per plane layer the share of
the return (in phase, inside the corridor) at a few points along the route and the largest difference between the
budgets over the whole route (the stations interpolated onto the finer grid's). Reads the board and settings only.
"""
import argparse
import dataclasses
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module): simulate the net on each grid budget (analysis.max_nodes) and save each
    grid's cross-section as a PNG in --out, to compare by eye."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True)
    ap.add_argument("--budgets", default="100000,200000")
    ap.add_argument("--out", default=str(ROOT / "out" / "section_check"))
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    from groundtruth.report.plots import draw_section, figure_png

    board = load_board(Path(args.board))
    cfg = SettingsFile.load(Path(args.settings)).config
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.only).strip("_")
    runs = []
    for budget in (int(b) for b in args.budgets.split(",")):
        c = dataclasses.replace(cfg, analysis=dataclasses.replace(cfg.analysis, max_nodes=budget,
                                                                  plane_capacitance=False))
        rr = simulate(board, c, nets=[args.only]).routes[0]
        if rr.error or not rr.section:
            print(f"{budget}: {rr.error or 'no section'}")
            return 1
        sec = rr.section
        png = out / f"section_{stem}_{budget}.png"
        png.write_bytes(figure_png(draw_section, board, rr, size=(10, 3.6)))
        print(f"max_nodes {budget}: grid {rr.grid.d:.3f} mm, {len(sec['s_mm'])} stations, corridor "
              f"±{sec['half_mm']:.2f} mm, {sec['length_mm']:.1f} mm long, {sec['f'] / 1e6:.4g} MHz -> {png}")
        runs.append((budget, sec))
    (_, a), (_, b) = runs[0], runs[-1]
    s = np.asarray(b["s_mm"], float)
    L = b["length_mm"]
    print(f"{'layer':10s} " + " ".join(f"{x:>11.0%}" for x in (0.1, 0.3, 0.5, 0.7, 0.9)) + "   max |diff|  mean |diff|")
    for name in b["layers"]:
        if name not in a["layers"]:
            continue
        ib = np.asarray(b["I"][b["layers"].index(name)]).real
        ia = np.interp(s, np.asarray(a["s_mm"], float), np.asarray(a["I"][a["layers"].index(name)]).real)
        if max(np.abs(ia).max(), np.abs(ib).max()) < 0.02:
            continue
        cells = []
        for x in (0.1, 0.3, 0.5, 0.7, 0.9):
            k = int(np.argmin(np.abs(s - x * L)))
            cells.append(f"{ia[k] * 100:4.0f}/{ib[k] * 100:<4.0f}%")
        d = np.abs(ia - ib)
        print(f"{name:10s} " + " ".join(f"{c:>11s}" for c in cells) + f"   {d.max() * 100:8.1f} %  {d.mean() * 100:8.1f} %")
    ta = np.interp(s, np.asarray(a["s_mm"], float), np.asarray(a["I"]).real.sum(axis=0))
    tb = np.asarray(b["I"]).real.sum(axis=0)
    print(f"{'sum':10s} mean {ta.mean() * 100:.0f} / {tb.mean() * 100:.0f} %, max |diff| {np.abs(ta - tb).max() * 100:.1f} %")
    return 0


if __name__ == "__main__":
    sys.exit(main())
