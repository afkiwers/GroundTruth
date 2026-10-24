"""Does the GMRES tolerance limit the results? (audit round 6, development aid)

Runs `groundtruth simulate --format json` in this process with the PEEC solver's GMRES tolerance (PlaneStack.solve's
`tol`, 1e-5 by default) and its restart count set from the command line, so that two runs, the default and a much
tighter tolerance, can be compared number by number with tools/dev/result_diff.py. A result that moves by more than
its stated accuracy when only the tolerance is tightened was stopped too early (false convergence).

    .venv/Scripts/python tools/dev/tol_check.py BOARD --settings S --out-dir OUT [--tol 1e-10] [--cycles 60]
        [--nets NET ...] [--resolution N] [--thermal]

Also prints, per solve, the residual of the KVL rows (volts) and the KCL rows (amperes) apart, relative to their
own right-hand side's size (or the system's when that is zero): GMRES stops on the norm of both together.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module): patch the solver's tolerance, run the CLI's simulate, print the solve
    statistics."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("--settings", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--tol", type=float, default=1e-5, help="GMRES relative residual (default 1e-5, the solver's)")
    ap.add_argument("--cycles", type=int, default=8, help="GMRES restarts before the extra cycles (default 8)")
    ap.add_argument("--nets", nargs="*")
    ap.add_argument("--resolution", default="1")
    ap.add_argument("--thermal", action="store_true")
    args = ap.parse_args()
    import numpy as np
    from groundtruth import cli
    from groundtruth.solver import peec

    stats = []
    orig_solve = peec.PlaneStack._solve

    def _solve(self, f, op, rhs_top, J, tol, max_cycles, guess, m, times=None):
        """PlaneStack._solve at the requested tolerance; records the block residuals of the answer."""
        out = orig_solve(self, f, op, rhs_top, J, args.tol, args.cycles, guess, m, times)
        x = out[3]
        b = np.concatenate([rhs_top.astype(complex), J[self.keep].astype(complex)])
        r = b - _HostMath(self).matvec(op, x)
        nb = self.nb
        scale = float(np.linalg.norm(b))
        top = float(np.linalg.norm(b[:nb])) or scale
        bot = float(np.linalg.norm(b[nb:])) or scale
        stats.append((f, out[2], float(np.linalg.norm(r[:nb])) / top, float(np.linalg.norm(r[nb:])) / bot,
                      float(np.linalg.norm(r)) / scale))
        return out

    _HostMath = peec._HostMath
    peec.PlaneStack._solve = _solve
    argv = ["simulate", args.board, "--settings", args.settings, "--format", "json", "--out-dir", args.out_dir,
            "--resolution", str(args.resolution), "--gpu", "off", "-q"]
    if args.nets:
        argv += ["--nets", *args.nets]
    if args.thermal:
        argv.append("--thermal")
    try:
        code = cli.main(argv)
    except SystemExit as e:
        code = e.code
    if stats:
        a = np.array(stats)
        print(f"{len(a)} solves at tol {args.tol:g}: iterations {int(a[:, 1].sum())} "
              f"(max {int(a[:, 1].max())}); residual, KVL rows / own rhs: max {a[:, 2].max():.2e}, "
              f"KCL rows / own rhs: max {a[:, 3].max():.2e}, whole: max {a[:, 4].max():.2e}")
        worst = a[np.argsort(-a[:, 2])[:5]]
        for f, its, kvl, kcl, whole in worst:
            print(f"  {f / 1e6:10.3f} MHz  {int(its):4d} its  KVL {kvl:.2e}  KCL {kcl:.2e}  whole {whole:.2e}")
    return int(code or 0)


if __name__ == "__main__":
    sys.exit(main())
