"""One solve of one net's plane model at one frequency, as Simulate builds it (development aid): does it converge,
how many iterations, how long, the loop moment. For a solve Simulate reported as "GMRES did not converge".

    .venv/Scripts/python tools/dev/solve_probe.py BOARD SETTINGS --only SPI_SCK --f 1.58e9 [--resolution 4]
        [--model board-wide|route]
"""
import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module): build the net's board-wide (or route) model and solve it at each
    frequency, printing whether it converged, its iterations and time."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True, help="part of the net name")
    ap.add_argument("--f", required=True, help="frequency, Hz (several: comma-separated)")
    ap.add_argument("--resolution", type=float, default=1.0)
    ap.add_argument("--model", default="board-wide", choices=("board-wide", "route"))
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import BoardModel, _m, prepare_route
    from groundtruth.config.settings import SettingsFile
    from groundtruth.rules.checks import run_checks
    from groundtruth.solver.peec import SolverError
    board = load_board(args.board)
    cfg = SettingsFile.load(Path(args.settings)).config
    cfg = replace(cfg, nets=[n for n in cfg.nets if args.only in n.name], converters=[],
                  analysis=replace(cfg.analysis, resolution=args.resolution))
    t = time.perf_counter()
    model = BoardModel(board, cfg)
    checks = run_checks(board, cfg, model.copper, None)
    specs = {s.name: s for s in cfg.nets}
    for (net, _), route in checks.routes.items():
        setup, _, _ = prepare_route(model, specs[net], route)
        if setup is None:
            continue
        run = setup.cavity if args.model == "board-wide" else setup.fine
        if run is None:
            print(f"{net}: no {args.model} model")
            continue
        st = run.exc.stack
        print(f"{net} {args.model}: {st.N} layers, grid {st.nx} x {st.ny}, d {run.build.d:.3f} mm "
              f"(built in {time.perf_counter() - t:.1f} s)")
        for f in (float(x) for x in str(args.f).split(",")):
            t = time.perf_counter()
            try:
                sol = run.exc.solve(f)
                print(f"  {f / 1e6:.1f} MHz: {sol.iterations} iterations, {time.perf_counter() - t:.1f} s, "
                      f"L {sol.L_loop * 1e9:.2f} nH, m {_m(sol) * 1e6:.2f} mm²")
            except SolverError as e:
                print(f"  {f / 1e6:.1f} MHz: {e} after {time.perf_counter() - t:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
