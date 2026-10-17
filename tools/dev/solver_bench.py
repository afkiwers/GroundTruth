"""What one solver iteration costs on a real board's models, piece by piece (development aid).

    .venv/Scripts/python tools/dev/solver_bench.py BOARD SETTINGS --only SPI_SCK [--threads 2] [--solve]

Builds the plane models of the chosen nets / power stages exactly as Simulate does (no solve), then times, per
model: the forward FFTs, the layer-kernel multiply-add and the inverse FFTs of one L_p product (`_conv`), the
sparse products, a preconditioner application and one factorisation. With --solve also one full solve at a
planned frequency (iterations and time). --threads sets the FFT threads (Simulate gives each of its parallel
solves cpu_count / solver threads of them). Prints the model sizes too, so a speed-up can be judged against the
size of the problem.
"""
import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def best(fn, n=5):
    """Shortest of n runs (s): the others include page faults and the like."""
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return min(out)


def bench_stack(name, st, f, threads, solve_exc=None):
    """Time each piece of one GMRES iteration on a plane stack at frequency f: the FFTs, the layer kernels'
    multiply-add, the whole L_p product, the sparse products and the preconditioner; then the factorisation and,
    with solve_exc, one full solve."""
    from scipy.fft import fft2, ifft2
    from groundtruth.solver import peec
    print(f"\n{name}: {st.N} layers, grid {st.nx} x {st.ny}, {st.ns} strips, {st.nb} branches, "
          f"{st.Ar.shape[1]} node unknowns; FFT sizes x {st.Px}, y {st.Py}; multiply-add: "
          f"{'numba' if peec._kernel_mac() is not None else 'numpy'}")
    rng = np.random.default_rng(0)
    I_s = rng.standard_normal(st.ns) + 1j * rng.standard_normal(st.ns)
    with peec.thread_budget(threads):
        parts = {}
        for orient, K, P, shape in (("x", st.Kx, st.Px, (st.N, st.nx - 1, st.ny)),
                                    ("y", st.Ky, st.Py, (st.N, st.nx, st.ny - 1))):
            X = rng.standard_normal(shape) + 1j * rng.standard_normal(shape)
            F = fft2(X, s=P, axes=(1, 2), workers=threads)
            parts[f"fft {orient}"] = best(lambda: fft2(X, s=P, axes=(1, 2), workers=threads))

            def mac(orient=orient, F=F, K=K):
                """The kernels' multiply-add as _conv does it (compiled when numba is there, else numpy)."""
                out = np.empty_like(F)
                compiled = peec._kernel_mac()
                if compiled is not None:                    # what _conv uses
                    return compiled(st._kq[orient], F, out)
                tmp = np.empty_like(F[0])
                for t in range(st.N):
                    np.multiply(K[(t, 0)], F[0], out=out[t])
                    for s in range(1, st.N):
                        np.multiply(K[(t, s)], F[s], out=tmp)
                        out[t] += tmp
                return out
            parts[f"kernel multiply-add {orient}"] = best(mac)
            parts[f"ifft {orient}"] = best(lambda: ifft2(F, axes=(1, 2), workers=threads))
        parts["Lp_matvec (all of the above)"] = best(lambda: st.Lp_matvec(I_s))
        x = rng.standard_normal(st.nb + st.Ar.shape[1]) + 0j
        parts["sparse A products"] = best(lambda: (st.Ar @ x[st.nb:], st.ArT @ x[:st.nb]))
        t = time.perf_counter()
        op = st.operator(f, exact=True)
        t_fact = time.perf_counter() - t
        r = rng.standard_normal(st.Ar.shape[1]) + 0j
        parts["preconditioner solve_S"] = best(lambda: op.solve_S(r))
        parts["preconditioner Dinv products"] = best(lambda: op.Dinv @ x[:st.nb])
    lp = parts["Lp_matvec (all of the above)"]
    per_it = (lp + parts["sparse A products"] * 2 + parts["preconditioner solve_S"]
              + parts["preconditioner Dinv products"] * 2)
    for k, v in parts.items():
        print(f"  {k:34s} {v * 1e3:9.2f} ms")
    print(f"  {'one GMRES iteration (about)':34s} {per_it * 1e3:9.2f} ms   factorisation at {f / 1e6:.4g} MHz: "
          f"{t_fact:.2f} s")
    if solve_exc is not None:
        with peec.thread_budget(threads):
            st._ops.clear()
            t = time.perf_counter()
            sol = solve_exc.solve(f)
            dt = time.perf_counter() - t
        print(f"  full solve at {f / 1e6:.4g} MHz: {dt:.2f} s, {sol.iterations} iterations "
              f"({dt / max(sol.iterations, 1) * 1e3:.0f} ms each incl. factorisation)")


def main() -> int:
    """The command line (see the module): build the chosen nets' and stages' models as Simulate does and bench each
    of their plane stacks."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True, help="comma-separated parts of net / power stage names")
    ap.add_argument("--threads", type=int, default=2, help="FFT threads (Simulate: cpu_count / solver threads)")
    ap.add_argument("--solve", action="store_true", help="also one full solve per model")
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.power import prepare_converters
    from groundtruth.analysis.simulate import BoardModel, prepare_route
    from groundtruth.config.settings import SettingsFile
    from groundtruth.rules.checks import run_checks
    board = load_board(args.board)
    cfg = SettingsFile.load(Path(args.settings)).config
    keys = args.only.split(",")
    cfg = replace(cfg, nets=[n for n in cfg.nets if any(k in n.name for k in keys)],
                  converters=[c for c in cfg.converters if any(k in c.name for k in keys)])
    t = time.perf_counter()
    model = BoardModel(board, cfg)
    checks = run_checks(board, cfg, model.copper, None)
    specs = {s.name: s for s in cfg.nets}
    setups = []
    for (net, _), route in checks.routes.items():
        setup, _, _ = prepare_route(model, specs[net], route)
        if setup is not None:
            setups.append(setup)
    csetups, _, _ = prepare_converters(model, cfg.converters)
    print(f"{len(board.copper_layers)} copper layers; models built in {time.perf_counter() - t:.1f} s")
    for s in setups + list(csetups):
        runs = [("fine", s.fine)] + ([("board-wide", s.cavity)] if s.cavity is not None else [])
        for label, run in runs:
            f = sorted(run.planned)[len(run.planned) // 2] if run.planned else s.spec.freq
            bench_stack(f"{s.spec.name} {label} model", run.exc.stack, f, args.threads,
                        run.exc if args.solve else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
