"""Where the return current crosses between plane layers in the board-wide (plane-capacitance) model, frequency by
frequency: through the plane capacitance (displacement current) and through vias / parts, and whether the two add
up to what has to cross (KCL). Explains a "plane capacitance" transfer above 100 %.

    .venv/Scripts/python tools/dev/transfer_check.py BOARD SETTINGS --only NET

Reads the board and settings only (nothing is written). For each solved board-wide frequency of the net and each cut
between neighbouring plane layers it prints: Σ I_cap (displacement current crossing the cut, per A of signal),
Σ I_via (vias and parts crossing it), their sum and the current that has to cross (the return pins' injections
above the cut), plus the KCL residual of the whole solution (Aᵀ·I − J). For the plane pairs it prints the
cavity's lowest resonances, f_mn = c/(2√εr)·√((m/a)² + (n/b)²) with a, b the board outline's bounding box.

A displacement current of several A per A with the vias carrying nearly as much back means a current circulating
between the planes and their vias at a resonance; the model has no radiation loss, so the peak is an upper estimate.
On the demo board (2026-10-06) the peak was 12.9 A per A at 1.45 GHz, below the cavity's f10 (1.69 GHz): the plane
pair's 29 pF resonating with the inductance of the two GND vias joining the planes (a parallel L-C tank: below the
peak I_cap = x/(1 - x), x = (f/f_r)^2, f_r 1.31-1.34 GHz from the 150-950 MHz solves).
"""
import argparse
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

C0 = 299_792_458.0


def branch_vector(plane, sol) -> np.ndarray:
    """All branch currents in the stack's unknown order [x strips | y strips | capacitances | branches]."""
    return np.concatenate([sol.I_x[tuple(plane.xa.T)], sol.I_y[tuple(plane.ya.T)], sol.I_cap, sol.I_branch])


def main() -> int:
    """The command line (see the module): simulate the net and, per board-wide frequency and plane cut, print the
    displacement against the via current, the current balance (KCL) and the cavity modes."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True, help="the net to analyse")
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import layer_crossing, simulate
    from groundtruth.config.settings import SettingsFile
    from groundtruth.solver.peec import EPS0

    board = load_board(Path(args.board))
    cfg = SettingsFile.load(Path(args.settings)).config
    res = simulate(board, cfg, nets=[args.only])
    for setup, rr in zip(res.setups, res.routes):
        print(f"{rr.net} {rr.driver}->{rr.receiver}: map at {rr.map_frequency / 1e6:.4g} MHz, worst "
              + (f"{rr.worst['f'] / 1e6:.4g} MHz ({rr.worst['margin_db']:+.1f} dB)" if rr.worst else "-"))
        for t in rr.transfers:
            print(f"   transfer {t['name']:40s} {t['kind']:12s} {t['fraction']:8.3f} A/A  {t['distance_mm']:.1f} mm")
        run = setup.cavity
        if run is None:
            print("   no board-wide model (analysis.plane_capacitance off, or no outline)")
            continue
        st, plane = run.build, run.exc.stack
        names = st.names
        x0, y0, x1, y1 = st.bbox
        a, b = max(x1 - x0, y1 - y0) * 1e-3, min(x1 - x0, y1 - y0) * 1e-3
        print(f"   board-wide model: {', '.join(names)}, {st.d:.2f} mm grid, {st.nodes} nodes, outline box "
              f"{a * 1e3:.1f} x {b * 1e3:.1f} mm")
        for p in plane.pairs:
            t = abs(plane.zs[p.upper] - plane.zs[p.lower])
            er = p.c_per_area * t / EPS0
            sel = (plane.cap[:, 0] == p.upper) & (plane.cap[:, 1] == p.lower)
            if not sel.any():                       # no facing copper (another plane in between everywhere)
                continue
            modes = sorted((C0 / (2 * math.sqrt(er)) * math.hypot(m / a, n / b), m, n)
                           for m in range(3) for n in range(3) if m or n)[:4]
            print(f"   pair {names[p.upper]}-{names[p.lower]}: {t * 1e3:.3f} mm, er {er:.2f}, tan d {p.tan_delta:g}, "
                  f"C {plane.cap[sel, 4].sum() * 1e12:.1f} pF; f_mn " +
                  ", ".join(f"{f / 1e6:.0f} MHz ({m},{n})" for f, m, n in modes))
        J = np.zeros(plane.n_nodes)
        for n, amps in run.exc.injections:
            J[n] += amps
        layer_of = np.nonzero(plane.node)[0]          # layer index of every node, in node-id order
        print(f"   {'f (MHz)':>9s}  {'cut':14s} {'|I_cap|':>8s} {'|I_via|':>8s} {'|sum|':>7s} {'must':>6s} "
              f"{'phase(cap,via)':>14s}  KCL residual")
        for f in sorted(run.ok()):
            sol = run.sols[f]
            Ib = branch_vector(plane, sol)
            out = plane.A.T @ Ib
            kcl = min(np.linalg.norm(out - J), np.linalg.norm(out + J)) / max(np.linalg.norm(J), 1e-30)
            for k in range(len(names) - 1):
                cap, via = layer_crossing(plane, sol, k)
                must = J[layer_of <= k].sum()
                dphi = math.degrees(np.angle(cap) - np.angle(via)) % 360 if abs(cap) and abs(via) else float("nan")
                print(f"   {f / 1e6:9.4g}  {names[k] + '|' + names[k + 1]:14s} {abs(cap):8.3f} {abs(via):8.3f} "
                      f"{abs(cap + via):7.3f} {abs(must):6.3f} {dphi:14.0f}  {kcl:.1e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
