"""Where Simulate (or the fix search) spends its time, on a real board (development aid).

    .venv/Scripts/python tools/dev/profile_sim.py BOARD SETTINGS [--only SW1,SPI_SCK] [--suggest] [--top 40]
        [--threads N] [--resolution 4] [--out FILE.prof] [--json FILE.json] [--no-profile]

Runs simulate (and with --suggest the fix search from that result) under cProfile and prints: the time of each
phase from the progress messages (model building, each solve, finishing), the slowest progress steps, and the
functions with the most cumulative and own time. cProfile only sees the calling thread's Python code; the solver
threads show up as waiting (run_parallel). Use --threads 1 to see the solves themselves. The run's solver
statistics (solver/stats.py) are printed too (tools/dev/run_stats.py) and, with --json, written with the counts
and the progress steps; --no-profile leaves cProfile out (its overhead on a long run).
"""
import argparse
import cProfile
import pstats
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _count_solver_work() -> dict:
    """Wrap PlaneStack._factorise and PlaneStack.solve: {"factorise": [calls, s], "solve": [calls, s,
    iterations]}, summed over all threads (so the seconds can exceed the wall-clock time)."""
    import threading
    from groundtruth.solver.peec import PlaneStack
    counts = {"factorise": [0, 0.0], "solve": [0, 0.0, 0]}
    lock = threading.Lock()
    fact, solve = PlaneStack._factorise, PlaneStack.solve

    def timed_factorise(self, f, exact):
        """The real factorisation, counted and timed (a low-rank update of a base factorisation is not counted)."""
        t = time.perf_counter()
        try:
            return fact(self, f, exact)
        finally:
            with lock:
                if self._base is None:              # a low-rank update of a base factorisation is not one
                    counts["factorise"][0] += 1
                    counts["factorise"][1] += time.perf_counter() - t

    def timed_solve(self, *a, **k):
        """The real solve, counted and timed, with its GMRES iterations added up."""
        t = time.perf_counter()
        out = solve(self, *a, **k)
        with lock:
            counts["solve"][0] += 1
            counts["solve"][1] += time.perf_counter() - t
            counts["solve"][2] += out[2]
        return out
    PlaneStack._factorise, PlaneStack.solve = timed_factorise, timed_solve
    return counts


def main() -> int:
    """The command line (see the module): simulate (and with --suggest the fix search) under cProfile, then print
    the phases' times, the slowest steps, the solver's work and the top functions."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", help="comma-separated parts of net / power stage names to keep")
    ap.add_argument("--suggest", action="store_true", help="also run the fix search from the simulation")
    ap.add_argument("--top", type=int, default=35)
    ap.add_argument("--threads", type=int)
    ap.add_argument("--resolution", type=float, help="analysis.resolution for this run (the plugin's Resolution)")
    ap.add_argument("--out", help="save the profile (for snakeviz and the like)")
    ap.add_argument("--json", help="write the solver statistics, the counts and the progress steps to this file")
    ap.add_argument("--no-profile", action="store_true", help="run without cProfile (a long run's overhead)")
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    t = time.perf_counter()
    board = load_board(args.board)
    cfg = SettingsFile.load(Path(args.settings)).config
    print(f"board read in {time.perf_counter() - t:.1f} s: {len(board.copper_layers)} copper layers")
    if args.only:
        keys = args.only.split(",")
        cfg = replace(cfg, nets=[n for n in cfg.nets if any(k in n.name for k in keys)],
                      converters=[c for c in cfg.converters if any(k in c.name for k in keys)])
    if args.threads:
        cfg = replace(cfg, analysis=replace(cfg.analysis, threads=args.threads))
    if args.resolution:
        cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=args.resolution))
    print(f"nets: {[n.name for n in cfg.nets]}; power stages: {[c.name for c in cfg.converters]}")
    steps: list[tuple[float, str, float]] = []
    t0 = time.perf_counter()

    def progress(msg, frac):
        """Record each progress message with its time since the start."""
        steps.append((time.perf_counter() - t0, msg, frac))

    counts = _count_solver_work()
    prof = cProfile.Profile()
    if not args.no_profile:
        prof.enable()
    res = simulate(board, cfg, progress=progress)
    t_sim = time.perf_counter() - t0
    sim_counts = {k: list(v) for k, v in counts.items()}
    if args.suggest:
        from groundtruth.analysis.suggest import suggest_fixes
        steps.append((time.perf_counter() - t0, "--- suggest_fixes", 0.0))
        suggest_fixes(board, cfg, progress=progress, baseline=res)
    prof.disable()
    total = time.perf_counter() - t0
    print(f"\nsimulate {t_sim:.1f} s" + (f", suggest {total - t_sim:.1f} s" if args.suggest else ""))
    for label, c in (("simulate", sim_counts), ("suggest", {k: [b - a for a, b in zip(sim_counts[k], v)]
                                                              for k, v in counts.items()}))[: 2 if args.suggest else 1]:
        print(f"  {label}: {c['factorise'][0]} factorisations ({c['factorise'][1]:.1f} s in all, summed over "
              f"threads), {c['solve'][0]} solves, {c['solve'][2]} GMRES iterations ({c['solve'][1]:.1f} s)")
    stats = (res.solver or {}).get("stats")
    if stats:                       # the run's own solver statistics (solver/stats.py), as run_stats.py prints them
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from run_stats import show
        print("\nthe run's solver statistics:")
        show(stats, 15)
    if args.json:
        import json
        Path(args.json).write_text(json.dumps({
            "board": str(args.board), "settings": str(args.settings), "only": args.only,
            "resolution": cfg.analysis.resolution, "seconds": round(t_sim, 2), "solver": res.solver,
            "counts": sim_counts, "steps": [{"t": round(tt, 2), "msg": m, "frac": round(fr, 4)}
                                             for tt, m, fr in steps]}, indent=1, default=str), encoding="utf-8")
        print(f"\nwritten: {args.json}")
    if args.no_profile:
        return 0
    # each progress step lasts until the next one (the steps run in parallel: these are wall-clock gaps)
    gaps = [(steps[k + 1][0] - steps[k][0], steps[k][1], steps[k][2]) for k in range(len(steps) - 1)]
    print("\nthe longest gaps between progress messages (wall clock; the bar does not move meanwhile):")
    for d, msg, frac in sorted(gaps, key=lambda g: -g[0])[:15]:
        print(f"  {d:7.1f} s  at {frac * 100:5.1f} %  after '{msg[:90]}'")
    print("\nthe bar against the time (fraction shown vs fraction of the run's time):")
    for k in range(0, len(steps), max(1, len(steps) // 20)):
        tt, msg, frac = steps[k]
        print(f"  {tt / total * 100:5.1f} % of the time   bar {frac * 100:5.1f} %   {msg[:80]}")
    st = pstats.Stats(prof)
    if args.out:
        st.dump_stats(args.out)
    print(f"\ntop {args.top} by cumulative time:")
    st.sort_stats("cumulative").print_stats(args.top)
    print(f"top {args.top} by own time:")
    st.sort_stats("tottime").print_stats(args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
