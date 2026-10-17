"""Grid convergence of a net's board-wide (plane capacitance) model: the same frequencies solved at several
resolutions, so a plane resonance can be followed from grid to grid (development aid). Simulate solves different
harmonics on each grid (its refinement follows the results), which makes two Simulates hard to compare near a
resonance.

    .venv/Scripts/python tools/dev/cavity_converge.py BOARD SETTINGS --only SPI_SCK --resolutions 1,2,4
        [--band 500e6,1300e6] [--every 1] [--threads 4] [--json OUT.json]
        [--freqs 680e6:800e6:10e6,1150e6:1400e6:25e6]   # any frequencies instead (peaks between harmonics)
        [--tol 1e-7]                                    # a tighter GMRES tolerance (is a peak solved fully?)

Per resolution: the board-wide grid (cell, nodes), then per solved harmonic the loop moment |m| (mm², the fine
model at the reference frequency plus the board-wide model's change, as Simulate combines them: band_moments) and
the estimated radiated margin (dB, as Simulate computes it at that harmonic; none between harmonics), and the
worst one. The summary lists |m| and the margin per frequency side by side. Long at 4x: run in the background.
"""
import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def one_resolution(board, cfg, only: str, resolution: float, band, every: int, threads: int,
                   freqs=None) -> dict:
    """Solve the net's board-wide model at one resolution on the given frequencies (or the band's harmonics), in two
    rounds as Simulate does (every third frequency first, the rest borrowing their factorisations); returns the
    grid, |m| and margin per frequency and the worst."""
    import numpy as np
    from groundtruth.analysis.simulate import (BoardModel, _margin_curve, band_moments, prepare_route,
                                               run_parallel)
    from groundtruth.rules.checks import run_checks
    from groundtruth.solver.peec import SolverError
    cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
    t0 = time.perf_counter()
    model = BoardModel(board, cfg)
    checks = run_checks(board, cfg, model.copper, None)
    specs = {s.name: s for s in cfg.nets}
    for (net, _), route in checks.routes.items():
        setup, _, _ = prepare_route(model, specs[net], route)
        if setup is None or setup.cavity is None:
            continue
        f_ref = setup.f_ref or setup.spec.freq
        H = setup.harmonics
        fs = list(freqs) if freqs else [float(f) for f in H[(H >= band[0]) & (H <= band[1])][::every]]
        if f_ref not in fs:
            fs = [float(f_ref)] + fs
        st = setup.cavity.exc.stack
        out = {"resolution": resolution, "net": net, "cell_mm": setup.cavity.build.d, "nodes": st.n_nodes,
               "layers": st.N, "solves": {}}
        print(f"{net} at {resolution:g}x: board-wide cell {setup.cavity.build.d:.3f} mm, {st.n_nodes} nodes, "
              f"{len(fs)} frequencies", flush=True)
        setup.fine.sols[f_ref] = setup.fine.exc.solve(f_ref)

        def solve(f):
            """One frequency's solve, timed; None (with the message) when it fails."""
            t = time.perf_counter()
            try:
                sol = setup.cavity.exc.solve(f)
            except SolverError as e:
                print(f"  {f / 1e6:8.1f} MHz: {e}", flush=True)
                return None
            print(f"  {f / 1e6:8.1f} MHz: {sol.iterations} iterations, {time.perf_counter() - t:.0f} s", flush=True)
            return sol
        # every third frequency first, then the rest borrowing their factorisations (two solve rounds, as
        # Simulate's refinement does: PlaneStack.next_round)
        first = fs[::3]
        rest = [f for f in fs if f not in first]
        st.next_round()
        sols = dict(zip(first, run_parallel(first, solve, threads)))
        st.next_round()
        sols.update(zip(rest, run_parallel(rest, solve, threads)))
        sols = {f: s for f, s in sols.items() if s is not None}
        f_m, ms = band_moments(setup, sols)
        margins = _margin_curve(setup, cfg, f_m, ms)
        at = {float(f): float(m) for f, m in zip(setup.harmonics, margins)}
        for f, m in zip(f_m, ms):
            out["solves"][f"{f:.0f}"] = {"m_mm2": float(m) * 1e6, "margin_db": at.get(float(f)),
                                         "iterations": sols[f].iterations}
        ok = {f: v for f, v in out["solves"].items() if v["margin_db"] is not None and np.isfinite(v["margin_db"])}
        if ok:
            w = min(ok, key=lambda f: ok[f]["margin_db"])
            out["worst"] = {"f": float(w), "margin_db": ok[w]["margin_db"]}
            print(f"  worst: {ok[w]['margin_db']:+.1f} dB at {float(w) / 1e6:.0f} MHz", flush=True)
        out["seconds"] = time.perf_counter() - t0
        return out
    raise SystemExit(f"no net matching {only!r} with a board-wide model")


def main() -> int:
    """The command line (see the module): every resolution in turn (the JSON rewritten after each, so a long run
    keeps what it has), then a table of |m| and margin per frequency side by side."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True, help="part of the net name")
    ap.add_argument("--resolutions", default="1,2,4")
    ap.add_argument("--band", default="500e6,1300e6", help="lowest,highest harmonic to solve (Hz)")
    ap.add_argument("--every", type=int, default=1, help="every n-th harmonic in the band")
    ap.add_argument("--freqs", help="start:stop:step[,start:stop:step...] (Hz) instead of the band's harmonics")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--tol", type=float, help="GMRES tolerance (relative residual; Simulate's is 1e-5), with up to "
                                              "30 restarts")
    ap.add_argument("--json", help="write the results here")
    args = ap.parse_args()
    if args.tol:
        from groundtruth.solver.peec import PlaneStack
        tol, cycles, guess = PlaneStack.solve.__defaults__
        PlaneStack.solve.__defaults__ = (args.tol, max(cycles, 30), guess)
    from groundtruth.adapters.file_source import load_board
    from groundtruth.components.fields import apply_fields
    from groundtruth.config.settings import SettingsFile
    board = load_board(args.board)
    cfg = SettingsFile.load(Path(args.settings)).config
    cfg.analysis.gpu = "off"
    apply_fields(cfg, board)
    cfg = replace(cfg, nets=[n for n in cfg.nets if args.only in n.name], converters=[], bridges=[])
    band = [float(x) for x in args.band.split(",")]
    freqs = []
    for part in (args.freqs.split(",") if args.freqs else []):
        a, b, step = (float(x) for x in part.split(":"))
        n = int(round((b - a) / step))
        freqs += [a + k * step for k in range(n + 1)]
    runs = []
    for r in (float(x) for x in args.resolutions.split(",")):
        runs.append(one_resolution(board, cfg, args.only, r, band, args.every, args.threads, sorted(set(freqs))))
        if args.json:      # after every resolution: a long run keeps what it has
            Path(args.json).write_text(json.dumps(runs, indent=1), encoding="utf-8")
    print("\n|m| (mm²) and margin (dB) per frequency:")
    print("     MHz " + "".join(f"  {r['resolution']:g}x {r['cell_mm']:.2f} mm".ljust(24) for r in runs))
    for f in sorted({f for r in runs for f in r["solves"]}, key=float):
        cells = []
        for r in runs:
            v = r["solves"].get(f)
            m = v and v["margin_db"]
            margin = f"{m:+6.1f} dB" if m is not None and m == m else ""
            cells.append(f"{v['m_mm2']:10.2f} {margin:>11}  " if v else " " * 24)
        print(f"{float(f) / 1e6:8.0f} " + "".join(cells))
    for r in runs:
        w = r.get("worst")
        if w:
            print(f"{r['resolution']:g}x: worst {w['margin_db']:+.1f} dB at {w['f'] / 1e6:.0f} MHz "
                  f"({r['nodes']} nodes, {r['seconds']:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
