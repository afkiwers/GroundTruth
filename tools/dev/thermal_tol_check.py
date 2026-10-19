"""Is the thermal solve's answer limited by its iteration tolerances? (audit round 6, development aid)

Builds a synthetic board (a long track at a DC current, tests/boardgen.py) or takes a board and settings, solves its
Thermal the way GroundTruth does (analysis.thermal._coupled: CG to a relative residual 1e-5 ... 1e-8, the coupled
iteration until a step changes no temperature by more than 0.02 K), then continues from that answer with every
linear solve at 1e-12 and prints how far the temperatures moved (the error the shipped tolerances leave), plus each
CG solve's iterations and whether it converged.

    .venv/Scripts/python tools/dev/thermal_tol_check.py [--width 0.1] [--amps 1] [--length 100] [--quality normal]
    .venv/Scripts/python tools/dev/thermal_tol_check.py --board BOARD --settings S [--only conv1]
"""
import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def _synthetic(tmp: Path, width: float, amps: float, length: float, quality: str):
    """A `length` mm track `width` mm wide at `amps` across a 2-layer board, fixed h = 10 W/m²K, no radiation:
    (board, config, the DC result)."""
    from boardgen import BoardGen
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile, settings_path
    g = BoardGen(layers=2, size=(length + 16.0, 60.0))
    for ref, x in (("U1", 8.0), ("U2", 8.0 + length)):
        g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, 30.0),
                    [{"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)}])
    g.track((8.0, 30.0), (8.0 + length, 30.0), "PWR", "F.Cu", width=width)
    path = g.write(tmp / "t.kicad_pcb")
    board = load_board(path)
    sc = SettingsFile.new(settings_path(path), board)
    sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", amps, "")], "")])
    sc.doc["thermal"] = {"convection": "fixed", "h_top": 10.0, "h_bottom": 10.0, "radiation": False,
                         "ambient_c": 25.0, "quality": quality}
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    return board, cfg, simulate(board, cfg).dc_loads


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width", type=float, default=0.1)
    ap.add_argument("--amps", type=float, default=1.0)
    ap.add_argument("--length", type=float, default=100.0)
    ap.add_argument("--quality", default="normal")
    ap.add_argument("--board")
    ap.add_argument("--settings")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    import numpy as np
    from groundtruth.analysis import thermal as T
    from groundtruth.thermal.solver import common

    log = []
    orig = common.SPDSolver.solve

    def solve(self, A, b, x0, progress, tol=1e-9, maxiter=1000):
        """SPDSolver.solve, recording the true relative residual and iterations of every solve."""
        x = orig(self, A, b, x0, progress, tol=tol, maxiter=maxiter)
        r = float(np.linalg.norm(b - A @ x) / max(np.linalg.norm(b), 1e-300))
        log.append((tol, self.last_iters, r))
        return x

    common.SPDSolver.solve = solve
    with tempfile.TemporaryDirectory() as tmp:
        if args.board:
            from dataclasses import replace
            from groundtruth.adapters.file_source import load_board
            from groundtruth.analysis.simulate import simulate
            from groundtruth.config.settings import SettingsFile
            board = load_board(args.board)
            cfg = SettingsFile.load(Path(args.settings)).config
            if args.only:
                cfg = replace(cfg, nets=[], converters=[c for c in cfg.converters if c.name in args.only])
            cfg = replace(cfg, thermal=replace(cfg.thermal, quality=args.quality))
            res = simulate(board, cfg)
            stages = list(res.converters) + list(res.dc_loads)
        else:
            board, cfg, stages = _synthetic(Path(tmp), args.width, args.amps, args.length, args.quality)
        heat, losses, notes = T.thermal_inputs(board, cfg, stages)
        m = T._build_model(board, cfg, heat, losses, list(notes), lambda *a: None, None)
        print(f"thermal unknowns {m.th.n}, finest cell {float(m.mesh.col_dx.min()):.4f} mm")
        sol = T._coupled(m, m.p20, m.Qc)
        print(f"shipped tolerances: {len(sol.hist)} coupled iterations, settled {sol.settled}, "
              f"t_max {float(sol.T.max()):.4f} °C")
        for tol, its, r in log:
            print(f"  CG tol {tol:.1e}: {its} iterations, true relative residual {r:.2e}"
                  f"{'  NOT CONVERGED' if r > 1.01 * tol else ''}")
        n0 = len(log)
        tight = m.th.solve
        m.th.solve = lambda Q, Tp, tol=1e-8: tight(Q, Tp, tol=1e-12)
        ref = T._coupled(m, m.p20, m.Qc, T=sol.T)
        for _ in range(3):
            ref = T._coupled(m, m.p20, m.Qc, T=ref.T)
        rise = float(ref.T.max()) - float(m.env.ambient_c)
        d = np.abs(sol.T - ref.T)
        print(f"tight (CG 1e-12, 4 more passes): t_max {float(ref.T.max()):.4f} °C; the shipped answer's error: "
              f"max {float(d.max()):.2e} K ({float(d.max()) / max(rise, 1e-9):.2e} of the rise), "
              f"t_max {float(sol.T.max()) - float(ref.T.max()):+.2e} K")
        bad = [x for x in log[n0:] if x[2] > 1.01 * x[0]]
        print(f"  tight CG solves: {len(log) - n0}, not converged {len(bad)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
