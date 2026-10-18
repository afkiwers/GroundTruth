"""Grid convergence of a resonance set by copper narrower than the grid (development aid, docs/NEXT_STEPS.md).

A 15 x 15 mm island on the top plate joined to the rest of it by a neck (w x 3 mm), a full bottom plate 0.2 mm
below (εr 4.5, tan δ 0.02): the island's capacitance against the neck's inductance resonates (~750 MHz for a
0.35 mm neck). Probed between the plates in the island's far corner. Per grid pitch d: the resonance (the Re Z peak,
a parabola through the top three samples on log f) and its height. The sub-grid is Simulate's own rule
(analysis/simulate.py: RASTER_MM or finer, at least 2 x 2, at most 6 x 6 per cell) unless --raster / --sub-max
change it, so a finer copper sampling can be tried without touching the code.

    .venv/Scripts/python tools/dev/neck_converge.py                          # 0.35 mm neck, d = 1.0 ... 0.1 mm
    .venv/Scripts/python tools/dev/neck_converge.py --neck 0.35 3.0 --grids 1 0.5 0.25
    .venv/Scripts/python tools/dev/neck_converge.py --raster 0.075 --sub-max 12   # a finer copper sampling

Results on 2026-10-18 (ARCHITECTURE "A strip is as wide as the copper it crosses"): 0.35 mm neck 797 / 756 / 736 /
764 / 717 / 769 / 742 MHz at d = 1.0 / 0.7 / 0.5 / 0.35 / 0.25 / 0.15 / 0.1 mm; 3 mm neck 1237-1271 MHz. Round 4
(the bigger machine): 763 / 758 / 736 / 762 / 716 / 768 / 743 MHz, and with --raster 0.075 --sub-max 12
763 / 783 / 775 / 763 / 807 / 768 / 743 MHz: a finer sampling does not narrow the spread.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

T, ER, TAN_D = 0.2e-3, 4.5, 0.02


def problem(d: float, neck_w: float, raster: float, sub_max: int):
    """The island model on a grid of pitch d (mm) with the given neck width (mm)."""
    from shapely.geometry import box
    from shapely.ops import unary_union
    from groundtruth.solver.peec import EPS0, MultiPlaneProblem, PlaneLayer, PlanePair, TraceSegment, build_grid
    bb = (0.0, 0.0, 40.0, 24.0)
    y = 12.0
    top = unary_union([box(0, 0, 18, 24),                                  # the plate (left)
                       box(18, y - neck_w / 2, 21, y + neck_w / 2),          # the neck, 3 mm long
                       box(21, y - 7.5, 36, y + 7.5)])                       # the island
    sub = max(2, min(sub_max, math.ceil(d / raster - 1e-9)))
    g_top = build_grid({"GND": top}, bb, d, sub=sub)
    g_bot = build_grid({"GND": box(*bb)}, bb, d, sub=sub)
    i, j = round((35.0 - g_top.x0) / d), round((y + 7.0 - g_top.y0) / d)
    x, yy = g_top.x0 + i * d, g_top.y0 + j * d
    probe = [TraceSegment(np.array([x * 1e-3, yy * 1e-3, -T]), np.array([x * 1e-3, yy * 1e-3, 0.0]), 0.3e-3)]
    return MultiPlaneProblem([PlaneLayer("top", 0.0, g_top), PlaneLayer("bottom", -T, g_bot)], probe,
                             (1, i, j), (0, i, j), [], capacitance=[PlanePair(0, 1, EPS0 * ER / T, TAN_D)]), sub


def resonance(p, fs: np.ndarray) -> tuple[float, float]:
    """(frequency of Re Z's peak, the peak) over the sweep fs."""
    r = np.array([complex(p.solve(f).Z_loop).real for f in fs])
    k = int(np.argmax(r))
    if 0 < k < len(fs) - 1:
        a, b, _ = np.polyfit(np.log(fs[k - 1:k + 2]), r[k - 1:k + 2], 2)
        return math.exp(-b / (2 * a)), float(r[k])
    return float(fs[k]), float(r[k])


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--neck", type=float, nargs="+", default=[0.35], help="neck widths, mm")
    ap.add_argument("--grids", type=float, nargs="+", default=[1.0, 0.7, 0.5, 0.35, 0.25, 0.15, 0.1],
                    help="grid pitches, mm")
    ap.add_argument("--raster", type=float, default=0.15, help="copper sampling pitch, mm (Simulate: RASTER_MM)")
    ap.add_argument("--sub-max", type=int, default=6, help="most samples per cell side (Simulate: 6)")
    ap.add_argument("--points", type=int, default=60, help="frequencies per sweep")
    args = ap.parse_args()
    for w in args.neck:
        lo, hi = (550e6, 1000e6) if w < 1.0 else (900e6, 1.8e9)
        fs = np.geomspace(lo, hi, args.points)
        print(f"neck {w} mm, sweep {lo / 1e6:.0f}-{hi / 1e6:.0f} MHz:", flush=True)
        for d in args.grids:
            p, sub = problem(d, w, args.raster, args.sub_max)
            f, r = resonance(p, fs)
            print(f"   d {d:5.3f} mm (sub {sub}): resonance {f / 1e6:7.1f} MHz, peak Re Z {r:7.2f} ohm", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
