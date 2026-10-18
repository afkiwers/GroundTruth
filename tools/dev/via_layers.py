"""Which layers one via joins in a net's board-wide model, per resolution (development aid, docs/NEXT_STEPS.md): its
branches (layer → layer, the node cells and their nets), and on every copper layer the via's own cell there (its net,
copper fraction and strips). A connection that exists on one grid and not the next shows here. Reads the board and
the settings only.

    .venv/Scripts/python tools/dev/via_layers.py BOARD SETTINGS --only SPI_SCK --at 132.95 132.60 --resolutions 1 2 4
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True, help="part of the net name")
    ap.add_argument("--at", type=float, nargs=2, required=True, metavar=("X", "Y"), help="the via's position, mm")
    ap.add_argument("--resolutions", type=float, nargs="+", default=[1.0, 2.0])
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import BoardModel, prepare_route
    from groundtruth.components.fields import apply_fields
    from groundtruth.config.settings import SettingsFile
    from groundtruth.rules.checks import run_checks
    board = load_board(args.board)
    x, y = args.at
    tag = f"@({x:.2f}, {y:.2f})"
    for res in args.resolutions:
        cfg = SettingsFile.load(Path(args.settings)).config
        cfg.analysis.gpu = "off"
        apply_fields(cfg, board)
        cfg = replace(cfg, nets=[n for n in cfg.nets if args.only in n.name], converters=[], bridges=[],
                      analysis=replace(cfg.analysis, resolution=res))
        model = BoardModel(board, cfg)
        checks = run_checks(board, cfg, model.copper, None)
        specs = {s.name: s for s in cfg.nets}
        for (net, _), route in checks.routes.items():
            setup, _, _ = prepare_route(model, specs[net], route)
            if setup is None or setup.cavity is None:
                continue
            b, st = setup.cavity.build, setup.cavity.exc.stack
            print(f"{res:g}x: board-wide cell {b.d:.3f} mm")
            for br in st.branches:
                if tag in br.name:
                    (ka, ia, ja), (kb, ib, jb) = br.a, br.b
                    print(f"   {br.kind} {br.name}: {b.names[ka]} ({ia},{ja} {b.grids[ka].net_at((ia, ja))}) -> "
                          f"{b.names[kb]} ({ib},{jb} {b.grids[kb].net_at((ib, jb))})")
            for k, g in enumerate(b.grids):
                i, j = round((x - g.x0) / g.d), round((y - g.y0) / g.d)
                fill = g.fill[i, j] if g.fill is not None else 1.0
                print(f"   {b.names[k]:7s} cell ({i},{j}): net {g.net_at((i, j)) or '-':8s} fill {fill:.2f} "
                      f"strips x {int(g.xb[i - 1, j])}{int(g.xb[i, j])} y {int(g.yb[i, j - 1])}{int(g.yb[i, j])}")
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
