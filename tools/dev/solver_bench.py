"""What one solver iteration costs on a real board's models, piece by piece (development aid).

    .venv/Scripts/python tools/dev/solver_bench.py BOARD SETTINGS --only SPI_SCK [--threads 2] [--solve]
        [--resolution 4] [--models fine,board-wide,conducted,victims,DC] [--k 3] [--gs-rows 50] [--repeat 5]

Builds the plane models of the chosen nets / power stages exactly as Simulate does (no solve), then times, per
model: the forward FFTs, the layer-kernel multiply-add and the inverse FFTs of one L_p product (`_conv`), the
sparse products, a preconditioner application and one factorisation (its fill), one Gram-Schmidt step with
--gs-rows basis vectors, and what k right-hand sides at one frequency would cost together (one FFT call on k
stacked inputs, one SuperLU solve of k columns) against k times one: the measure of lock-step solves
(docs/SPEED.md). With --solve also one full solve at a planned frequency (iterations and time). --threads sets
the FFT threads (Simulate gives each of its parallel solves cpu_count / solver threads of them). Prints the model
sizes too, so a speed-up can be judged against the size of the problem. --models picks the models by kind
(simulate.run_kind; a converter's input path is "conducted" when it has a model of its own, its DC paths "DC").
"""
import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

REPEAT = [5]                # timing repeats (--repeat): the shortest counts


def best(fn, n=None):
    """Shortest of n runs (s): the others include page faults and the like."""
    out = []
    for _ in range(n or REPEAT[0]):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return min(out)


def bench_stack(name, st, f, threads, solve_exc=None, k=3, gs_rows=50):
    """Time each piece of one GMRES iteration on a plane stack at frequency f: the FFTs, the layer kernels'
    multiply-add, the whole L_p product, the sparse products and the preconditioner; then the factorisation, a
    Gram-Schmidt step, k right-hand sides together against one at a time and, with solve_exc, one full solve."""
    from scipy.fft import fft2, ifft2
    from groundtruth.solver import peec
    n_unknowns = st.nb + st.Ar.shape[1]
    print(f"\n{name}: {st.N} layers, grid {st.nx} x {st.ny} at {st.d * 1e3:.4f} mm, {st.n_nodes} nodes, {st.ns} "
          f"strips, {st.nb} branches, {st.Ar.shape[1]} node unknowns ({n_unknowns} in all); FFT sizes x {st.Px}, "
          f"y {st.Py}; preconditioner {st.precond_mode}; multiply-add: "
          f"{'numba' if peec._kernel_mac() is not None else 'numpy'}")
    rng = np.random.default_rng(0)
    I_s = rng.standard_normal(st.ns) + 1j * rng.standard_normal(st.ns)
    with peec.thread_budget(threads):
        parts = {}
        for orient, P, shape in (("x", st.Px, (st.N, st.nx - 1, st.ny)), ("y", st.Py, (st.N, st.nx, st.ny - 1))):
            X = rng.standard_normal(shape) + 1j * rng.standard_normal(shape)
            F = fft2(X, s=P, axes=(1, 2), workers=threads)
            parts[f"fft {orient}"] = best(lambda: fft2(X, s=P, axes=(1, 2), workers=threads))

            def mac(orient=orient, F=F):
                """The kernels' multiply-add as _conv does it (compiled when numba is there, else numpy)."""
                out = np.empty_like(F)
                compiled = peec._kernel_mac()
                if compiled is not None:                    # what _conv uses
                    return compiled(st._kq[orient], F, out)
                tmp = np.empty_like(F[0])
                for t in range(st.N):
                    np.multiply(st.kernel(orient, t, 0), F[0], out=out[t])
                    for s in range(1, st.N):
                        np.multiply(st.kernel(orient, t, s), F[s], out=tmp)
                        out[t] += tmp
                return out
            parts[f"kernel multiply-add {orient}"] = best(mac)
            parts[f"ifft {orient}"] = best(lambda: ifft2(F, axes=(1, 2), workers=threads))
            if orient == "x" and k > 1:
                # k right-hand sides at one frequency: one FFT call on k stacked inputs against k calls
                Xk = np.stack([X] * k)
                parts[f"fft x, {k} inputs stacked"] = best(lambda: fft2(Xk, s=P, axes=(2, 3), workers=threads))
        parts["Lp_matvec (all of the above)"] = best(lambda: st.Lp_matvec(I_s))
        x = rng.standard_normal(n_unknowns) + 0j
        parts["sparse A products"] = best(lambda: (st.Ar @ x[st.nb:], st.ArT @ x[:st.nb]))
        t = time.perf_counter()
        op = st.operator(f, exact=True)
        t_fact = time.perf_counter() - t
        r = rng.standard_normal(st.Ar.shape[1]) + 0j
        parts["preconditioner solve_S"] = best(lambda: op.solve_S(r))
        if k > 1:
            R = np.stack([r] * k, axis=1)
            parts[f"preconditioner solve_S, {k} columns at once"] = best(lambda: op.solve_S(R))
        parts["preconditioner Dinv products"] = best(lambda: op.Dinv @ x[:st.nb])
        # one Gram-Schmidt step (two passes over gs_rows basis vectors, as _gmres does near the end of a cycle)
        rows = max(2, min(gs_rows, 400_000_000 // (16 * n_unknowns)))
        V = rng.standard_normal((rows, n_unknowns)) + 0j
        w = rng.standard_normal(n_unknowns) + 0j
        parts[f"Gram-Schmidt step, {rows} basis vectors"] = best(
            lambda: peec._HostVectors.project(V, rows - 1, w.copy()), max(2, REPEAT[0] // 2))
        del V
    lp = parts["Lp_matvec (all of the above)"]
    per_it = (lp + parts["sparse A products"] * 2 + parts["preconditioner solve_S"]
              + parts["preconditioner Dinv products"] * 2)
    for key, v in parts.items():
        print(f"  {key:44s} {v * 1e3:9.2f} ms")
    fill = getattr(st, "lu_nnz", 0)
    print(f"  {'one GMRES iteration without Gram-Schmidt':44s} {per_it * 1e3:9.2f} ms   factorisation at "
          f"{f / 1e6:.4g} MHz: {t_fact:.2f} s, fill {fill / 1e6:.1f} M entries ({fill * 16 / 2 ** 20:.0f} MB)")
    if solve_exc is not None:
        with peec.thread_budget(threads):
            st._ops.clear()
            t = time.perf_counter()
            sol = solve_exc.solve(f)
            dt = time.perf_counter() - t
        print(f"  full solve at {f / 1e6:.4g} MHz: {dt:.2f} s, {sol.iterations} iterations "
              f"({dt / max(sol.iterations, 1) * 1e3:.0f} ms each incl. factorisation)")


def board_models(board_path, settings, only: str, resolution=None, kinds=None) -> list[tuple]:
    """The plane models of the nets / power stages whose names contain one of `only`'s comma-separated parts,
    built exactly as Simulate builds them (no solve): [(setup name, kind, stack, frequency, excitation)], each model
    once (whichever run uses it first), `kinds` (a set of simulate.run_kind names) only when given; the frequency is
    the middle one of the run's planned solves."""
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.power import prepare_converters
    from groundtruth.analysis.simulate import BoardModel, prepare_route, run_kind
    from groundtruth.config.settings import SettingsFile
    from groundtruth.rules.checks import run_checks
    board = load_board(board_path)
    cfg = SettingsFile.load(Path(settings)).config
    keys = only.split(",")
    cfg = replace(cfg, nets=[n for n in cfg.nets if any(k in n.name for k in keys)],
                  converters=[c for c in cfg.converters if any(k in c.name for k in keys)])
    if resolution:
        cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
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
    out, seen = [], set()
    for s in setups + list(csetups):
        runs = [s.fine] + ([s.cavity] if s.cavity is not None else []) + list(getattr(s, "extra_runs", ()))
        for run in runs:
            kind, st = run_kind(s, run), run.exc.stack
            if id(st) in seen or (kinds is not None and kind not in kinds):
                continue
            seen.add(id(st))
            f = sorted(run.planned)[len(run.planned) // 2] if run.planned else s.spec.freq
            out.append((s.spec.name, kind, st, f, run.exc))
    return out


def main() -> int:
    """The command line (see the module): build the chosen nets' and stages' models as Simulate does and bench each
    of their plane stacks (each model once, whichever run uses it first)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True, help="comma-separated parts of net / power stage names")
    ap.add_argument("--threads", type=int, default=2, help="FFT threads (Simulate: cpu_count / solver threads)")
    ap.add_argument("--resolution", type=float, help="analysis.resolution (the plugin's Resolution)")
    ap.add_argument("--models", help="comma-separated model kinds to bench (default: all)")
    ap.add_argument("--k", type=int, default=3, help="right-hand sides for the lock-step measurements")
    ap.add_argument("--gs-rows", type=int, default=50, help="basis vectors of the Gram-Schmidt step")
    ap.add_argument("--repeat", type=int, default=5, help="timing repeats (the shortest counts)")
    ap.add_argument("--solve", action="store_true", help="also one full solve per model")
    args = ap.parse_args()
    REPEAT[0] = max(1, args.repeat)
    kinds = set(args.models.split(",")) if args.models else None
    for name, kind, st, f, exc in board_models(args.board, args.settings, args.only, args.resolution, kinds):
        bench_stack(f"{name} {kind} model", st, f, args.threads, exc if args.solve else None, args.k, args.gs_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
