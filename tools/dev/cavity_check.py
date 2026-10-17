"""Reproduce the plane-capacitance validation and the GMRES stall near cavity resonance.

    .venv/Scripts/python tools/dev/cavity_check.py

1. Parallel plate 20x20 mm, t = 0.2 mm, er 4.5: C must equal eps0*er*A/t for the rasterised area.
2. Plate pair 40x20 mm probed at a corner: |Z| should peak near f10 = c/(2a*sqrt(er)) ~ 1.77-1.81 GHz.
   This stalled GMRES (10 000+ iterations) while the FFT product ran in single precision; now ~20.
"""
import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from groundtruth.solver.peec import (EPS0, MultiPlaneProblem, PlaneLayer, PlanePair, SolverError,  # noqa: E402
                                TraceSegment, build_grid)

T, ER = 0.2e-3, 4.5


def plate_pair(a_mm: float, b_mm: float, d_mm: float, tan_delta: float = 0.0) -> MultiPlaneProblem:
    """Two square plates a x b mm, d mm cells, T apart, with a probe between them near a corner and their
    capacitance per area (and loss tangent) as a plane pair."""
    grids = [build_grid(box(0, 0, a_mm, b_mm), (0, 0, a_mm, b_mm), d_mm) for _ in range(2)]
    i = j = 1
    x, y = i * d_mm * 1e-3, j * d_mm * 1e-3
    probe = [TraceSegment(np.array([x, y, -T]), np.array([x, y, 0.0]), 0.3e-3)]   # source between the plates
    return MultiPlaneProblem([PlaneLayer("top", 0.0, grids[0]), PlaneLayer("bottom", -T, grids[1])], probe,
                             (1, i, j), (0, i, j), [], capacitance=[PlanePair(0, 1, EPS0 * ER / T, tan_delta)])


def main():
    """The parallel-plate capacitance against eps0 er A / t at 1 MHz, then a sweep across the first cavity mode (f10
    = c / (2 a sqrt(er))) printing the GMRES iterations near resonance."""
    argparse.ArgumentParser(description=__doc__).parse_args()
    p = plate_pair(20, 20, 0.5)
    s = p.solve(1e6)
    c = -1 / (2 * math.pi * 1e6 * s.Z_loop.imag)
    side = 39 * 0.5e-3   # interior nodes 39 x 39 at 0.5 mm
    print(f"parallel plate: C = {c * 1e12:.2f} pF, eps0*er*A/t = {EPS0 * ER * side ** 2 / T * 1e12:.2f} pF, "
          f"{s.iterations} iterations")

    p = plate_pair(40, 20, 0.5, tan_delta=0.02)
    print(f"cavity f10 analytic: {3e8 / (2 * 0.039 * math.sqrt(ER)) / 1e9:.3f} GHz (rasterised 39 mm)")
    for f in np.linspace(1.4e9, 2.2e9, 17):
        t0 = time.time()
        try:
            s = p.solve(f)
            print(f"{f / 1e9:.2f} GHz  |Z| = {abs(s.Z_loop):8.2f} ohm  {s.iterations:6d} it  {time.time() - t0:6.1f} s")
        except SolverError as e:
            print(f"{f / 1e9:.2f} GHz  no convergence: {e}  {time.time() - t0:6.1f} s")


if __name__ == "__main__":
    main()
