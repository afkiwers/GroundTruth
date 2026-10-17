"""Where a net's board-wide model resonates at one frequency (development aid): the plane pairs' displacement
current per cell (largest cells, and per pair in total), the strip current per layer, and the vias / tracks / parts
carrying the most current. Compare two resolutions to see what changes with the grid.

    .venv/Scripts/python tools/dev/cavity_hotspots.py BOARD SETTINGS --only SPI_SCK --f 1.22e9 --resolution 2
        [--top 12]

Reads the board and settings only (nothing is written). Currents are per ampere of signal current.
"""
import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module): solve the net's board-wide model at one frequency and print the
    displacement current per plane pair and its largest cells, the strip current per layer and the branches
    carrying the most current."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True, help="part of the net name")
    ap.add_argument("--f", type=float, required=True, help="frequency, Hz")
    ap.add_argument("--resolution", type=float, default=1.0)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import BoardModel, _m, prepare_route
    from groundtruth.components.fields import apply_fields
    from groundtruth.config.settings import SettingsFile
    from groundtruth.rules.checks import run_checks
    board = load_board(args.board)
    cfg = SettingsFile.load(Path(args.settings)).config
    cfg.analysis.gpu = "off"
    apply_fields(cfg, board)
    cfg = replace(cfg, nets=[n for n in cfg.nets if args.only in n.name], converters=[], bridges=[],
                  analysis=replace(cfg.analysis, resolution=args.resolution))
    model = BoardModel(board, cfg)
    checks = run_checks(board, cfg, model.copper, None)
    specs = {s.name: s for s in cfg.nets}
    for (net, _), route in checks.routes.items():
        setup, _, _ = prepare_route(model, specs[net], route)
        if setup is None or setup.cavity is None:
            continue
        b = setup.cavity.build
        st = setup.cavity.exc.stack
        sol = setup.cavity.exc.solve(args.f)
        print(f"{net} at {args.resolution:g}x, {args.f / 1e6:.0f} MHz: cell {b.d:.3f} mm, {sol.iterations} iterations, "
              f"|m| {_m(sol) * 1e6:.2f} mm²")
        g0 = b.grids[0]
        if st.ncap:
            cu = st.cap[:, :4].astype(int)
            I = np.abs(sol.I_cap)
            print("  displacement current per plane pair (sum of |I| over cells, A per A):")
            for k, l in sorted({(int(a), int(c)) for a, c in cu[:, :2]}):
                sel = (cu[:, 0] == k) & (cu[:, 1] == l)
                print(f"    {b.names[k]}-{b.names[l]}: {I[sel].sum():9.3f}  (net {abs(sol.I_cap[sel].sum()):.3f})")
            print("  largest cells:")
            for n in np.argsort(-I)[: args.top]:
                k, l, i, j = cu[n]
                x, y = g0.xy(i, j)
                nets = [b.grids[q].nets[b.grids[q].label[i, j]] for q in (k, l)]
                print(f"    ({x:7.2f}, {y:7.2f}) {b.names[k]}-{b.names[l]} {nets[0]} / {nets[1]}: {I[n]:.4f}")
        print("  strip current per layer (sum of |I|):")
        for k, name in enumerate(b.names):
            print(f"    {name}: {np.abs(sol.I_x[k]).sum() + np.abs(sol.I_y[k]).sum():9.2f}")
        print("  branches carrying the most current:")
        for n in np.argsort(-np.abs(sol.I_branch))[: args.top]:
            br = st.branches[n]
            print(f"    {br.kind:5s} {br.name}: {abs(sol.I_branch[n]):.3f}")
        return 0
    raise SystemExit(f"no net matching {args.only!r} with a board-wide model")


if __name__ == "__main__":
    sys.exit(main())
