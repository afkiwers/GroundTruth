"""The PEEC solver against closed forms as the grid is refined (audit round 6, development aid):

1. A plate pair a x b mm, T apart (εr, tan δ), probed near a corner: its first cavity mode against the
   magnetic-wall cavity's f10 = c / (2 a √εr) [Pozar 2012] (the PEEC model has no fringing field, so this is the
   limit it should approach), and its capacitance at 1 MHz against ε0 εr a b / T, per pitch.
2. A strip 0.2 mm wide 0.5 mm over a plane, 30 mm long, closed by vias: its loop inductance against Wheeler's
   L = (μ0 / 2π) ln(8h/w + w/4h) per length [Wheeler 1978] (the form tests/test_solver.py checks at one pitch).

    .venv/Scripts/python tools/dev/cavity_analytic_converge.py [--pitches 1,0.5,0.25] [--sub 4] [--skip-cavity]

Every grid samples the copper exactly (build_grid's sub-grid, as Simulate does).
"""
import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

C0 = 299_792_458.0
T_DIEL, ER = 0.2e-3, 4.5


def plate_pair(a_mm: float, b_mm: float, d_mm: float, sub: int, tan_delta: float):
    """The plate pair on a d_mm grid with exact copper (sub-grid `sub`), a probe between the plates at the node
    nearest (1, 1) mm."""
    from groundtruth.solver.peec import EPS0, MultiPlaneProblem, PlaneLayer, PlanePair, TraceSegment, build_grid
    bb = (-d_mm, -d_mm, a_mm + d_mm, b_mm + d_mm)
    grids = [build_grid(box(0, 0, a_mm, b_mm), bb, d_mm, sub=sub) for _ in range(2)]
    g = grids[0]
    i, j = g.nearest_node(1.0, 1.0)
    x, y = (g.x0 + i * d_mm - g.x0) * 1e-3, (g.y0 + j * d_mm - g.y0) * 1e-3
    probe = [TraceSegment(np.array([x, y, -T_DIEL]), np.array([x, y, 0.0]), 0.3e-3)]
    return MultiPlaneProblem([PlaneLayer("top", 0.0, grids[0]), PlaneLayer("bottom", -T_DIEL, grids[1])], probe,
                             (1, i, j), (0, i, j), [], capacitance=[PlanePair(0, 1, EPS0 * ER / T_DIEL, tan_delta)])


def resonance(p, lo: float, hi: float, n: int = 15) -> tuple[float, int]:
    """The |Z| peak between lo and hi: a scan of n points, then twice a finer scan around the best, then a parabola
    through the three highest points in log |Z|. (f, total GMRES iterations)."""
    its = 0
    for _ in range(3):
        fs = np.linspace(lo, hi, n)
        z = []
        for f in fs:
            s = p.solve(f)
            its += s.iterations
            z.append(abs(s.Z_loop))
        k = int(np.clip(np.argmax(z), 1, n - 2))
        step = fs[1] - fs[0]
        lo, hi = fs[k] - step, fs[k] + step
    y0, y1, y2 = (math.log(v) for v in z[k - 1:k + 2])
    off = 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2)
    return float(fs[k] + off * step), its


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pitches", default="1,0.5,0.25")
    ap.add_argument("--sub", type=int, default=4)
    ap.add_argument("--skip-cavity", action="store_true")
    args = ap.parse_args()
    from groundtruth.solver.peec import EPS0
    pitches = [float(x) for x in args.pitches.split(",")]
    if not args.skip_cavity:
        a, b = 40.0, 20.0
        f10 = C0 / (2 * a * 1e-3 * math.sqrt(ER))
        c_exact = EPS0 * ER * a * b * 1e-6 / T_DIEL
        print(f"cavity {a:g} x {b:g} mm, T {T_DIEL * 1e3:g} mm, er {ER}: f10 = {f10 / 1e9:.5f} GHz, "
              f"C = {c_exact * 1e12:.2f} pF")
        for d in pitches:
            t0 = time.time()
            p = plate_pair(a, b, d, args.sub, 0.02)
            s = p.solve(1e6)
            c = -1 / (2 * math.pi * 1e6 * s.Z_loop.imag)
            f, its = resonance(p, 0.9 * f10, 1.1 * f10)
            print(f"  d {d:6.3f} mm: f10 {f / 1e9:.5f} GHz ({f / f10 - 1:+.3%}), C {c * 1e12:.2f} pF "
                  f"({c / c_exact - 1:+.3%}), {its} iterations, {time.time() - t0:.0f} s")
    # the loop inductance of a microstrip closed by two vias, against Wheeler
    from groundtruth.solver.peec import PlaneProblem, TraceSegment, build_grid
    H, W, x0, x1, y = 0.5, 0.2, 5.0, 35.0, 10.0
    wheeler = 2e-7 * math.log(8 * H / W + W / (4 * H)) * (x1 - x0) * 1e-3
    print(f"microstrip loop, w {W} mm, h {H} mm, {x1 - x0:g} mm: Wheeler {wheeler * 1e9:.4f} nH")
    for d in pitches:
        g = build_grid(box(0, 0, 40, 20), (0, 0, 40, 20), d, sub=args.sub)

        def P(x, yy, z):
            """A board point (mm) in the solver's frame (m)."""
            return np.array([(x - g.x0) * 1e-3, (yy - g.y0) * 1e-3, z * 1e-3])

        trace = [TraceSegment(P(x0, y, 0), P(x0, y, H), W * 1e-3), TraceSegment(P(x0, y, H), P(x1, y, H), W * 1e-3),
                 TraceSegment(P(x1, y, H), P(x1, y, 0), W * 1e-3)]
        sol = PlaneProblem(g, trace, g.nearest_node(x0, y), g.nearest_node(x1, y)).solve(1e8)
        print(f"  d {d:6.3f} mm: L {sol.L_loop * 1e9:.4f} nH ({sol.L_loop / wheeler - 1:+.2%}), "
              f"{sol.iterations} iterations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
