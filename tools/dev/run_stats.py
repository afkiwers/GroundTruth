"""Where a Simulate's time went, from the solver statistics it kept (solver/stats.py, docs/SPEED.md).

    .venv/Scripts/python tools/dev/run_stats.py RUN [RUN ...] [--models 12]

RUN is a kept run's folder or its run.json (<board folder>/groundtruth/history/runs/<time>/, where every plugin
Simulate is kept, a compute server's included), or a JSON file written by `tools/dev/profile_sim.py --json`.
Prints the machine, the phases, the scheduling rounds (threads, wall against busy time: what the threads waited for
a round's slowest solve), the costliest models (factorisations, solves, GMRES iterations and where an iteration's
time went: the L_p product's FFTs and multiply-add, the preconditioner's SuperLU solves, Gram-Schmidt), the totals
and the memory. A split job's parts are printed one after the other.
"""
import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> tuple[str, dict]:
    """(a title, the stats dict) from a run folder, a run.json or a profile_sim JSON; exits when there are none."""
    p = path / "run.json" if path.is_dir() else path
    d = json.loads(p.read_text(encoding="utf-8"))
    st = (d.get("solver") or {}).get("stats") or d.get("stats")
    if not st:
        sys.exit(f"{p}: no solver statistics (a run from before they were kept, or a split without them)")
    title = f"{p}: resolution {d.get('resolution', '?')}, {d.get('seconds', '?')} s"
    if d.get("solver", {}).get("build"):
        title += f", {d['solver']['build']}{d['solver'].get('server', '')}"
    return title, st


def pct(part: float, whole: float) -> str:
    """part as a percentage of whole ("–" without a whole)."""
    return f"{100 * part / whole:5.1f} %" if whole else "    –  "


def show(st: dict, n_models: int):
    """Print one run's statistics (see the module)."""
    m = st.get("machine", {})
    print(f"  machine: {m.get('platform')} {m.get('machine')} {m.get('macos') or m.get('windows') or ''}, "
          f"{m.get('cpus')} logical / {m.get('cores')} physical cores, {m.get('ram_gb')} GB; Python {m.get('python')}, "
          f"numpy {m.get('numpy')}, scipy {m.get('scipy')}, numba {m.get('numba', '–')}")
    wall = st.get("wall_s") or 0.0
    print(f"  wall {wall:.0f} s, CPU {st.get('cpu_s', 0):.0f} s ({100 * (st.get('cpu_share') or 0):.0f} % of all cores)")
    info = st.get("info", {})
    lim = info.get("memory_limit") or {}
    print(f"  threads: setting {info.get('threads_setting')}, {info.get('threads_cpu')} by cores, "
          f"{info.get('threads_memory')} by memory"
          + (" for the largest, each round admitted by memory" if info.get("admission") else "")
          + f" (available {info.get('available_gb')} GB, largest solve: "
          f"{lim.get('model')} ~{lim.get('gb')} GB); groups {info.get('groups')}; memory gate: "
          f"{info.get('gate_freed', 0)} cached factorisations freed, {info.get('gate_waits', 0)} waits; "
          f"retried alone {info.get('retried_alone', 0)}, unsolved for memory {info.get('unsolved_for_memory', 0)}")
    sp_ = info.get("sparse")
    if sp_:             # the factorisation backend (solver/sparse.py, since 2026-10-25)
        print(f"  sparse solver: {sp_.get('active')} (chosen {sp_.get('chosen')}, {sp_.get('fallbacks', 0)} fell back to "
              f"SuperLU)" + "".join(f"; {k} not loaded: {v}" for k, v in (sp_.get("not_loaded") or {}).items()))
    print("  phases: " + ", ".join(f"{p['phase']} {p['s']:.0f} s ({pct(p['s'], wall).strip()})"
                                   for p in st.get("phases", [])))
    rounds = st.get("rounds", [])
    if rounds:
        # at once: the most solves that ran side by side (memory admission, since 2026-10-25; the threads before)
        print("  rounds:  #  step     tasks threads at once   wall s   busy s  use   tasks by model")
        for r in rounds:
            use = r["busy_s"] / (r["threads"] * r["wall_s"]) if r.get("wall_s") and r.get("threads") else 0.0
            kinds = ", ".join(f"{k} {v}" for k, v in sorted(r.get("by_kind", {}).items()))
            print(f"          {r['round']:2d}  {r['step']:8s} {r['tasks']:5d} {r['threads']:7d} "
                  f"{r.get('at_once', r['threads']):7d} {r['wall_s']:8.1f} {r['busy_s']:8.1f} {100 * use:3.0f} %  {kinds}")
    models = st.get("models", [])
    print(f"  models ({len(models)}, the costliest {min(n_models, len(models))}; factorise s includes the memory "
          f"gate's wait; an iteration's time in ms and where it went):")
    print("    label            nodes   unknowns  fact   s     fill M  remade upd  solves  its  gmres s  ms/it  "
          "L_p (fft/mac)       precond (LU)   GS     borrowed refactor failed  uses")
    for e in models[:n_models]:
        its = e.get("iterations") or 0
        g = e.get("gmres_s") or 0.0
        uses = ", ".join(e.get("uses", [])) + (f" +{e['more_uses']}" if e.get("more_uses") else "")
        label = e.get("label") or "unlabelled"
        if e.get("derived"):
            label += f" +{e.get('extra_branches', 0)}"
        print(f"    {label[:16]:16s} {e['nodes']:7d} {e['unknowns']:9d} {e['factorisations']:4d} "
              f"{e['factorise_s']:7.1f} {e['fill'] / 1e6:7.1f} {e['remade']:5d} {e['updates']:4d} {e['solves']:6d} "
              f"{its:5d} {g:8.1f} {1e3 * g / its if its else 0:6.0f}  "
              f"{pct(e['lp_s'], g)} ({pct(e['fft_s'], g).strip()}/{pct(e['mac_s'], g).strip()})  "
              f"{pct(e['precond_s'], g)} ({pct(e['lu_solve_s'], g).strip()})  {pct(e['gs_s'], g)}  "
              f"{e['borrowed']:5d} {e['refactor']:5d} {e['failed']:5d}   {uses}")
    t = st.get("totals", {})
    print(f"  totals: {t.get('factorisations')} factorisations {t.get('factorise_s', 0):.0f} s (largest fill "
          f"{(t.get('fill') or 0) / 1e6:.1f} M), {t.get('updates')} updates {t.get('update_s', 0):.0f} s, "
          f"{t.get('solves')} solves, {t.get('iterations')} GMRES iterations {t.get('gmres_s', 0):.0f} s, waited for "
          f"others' factorisations {t.get('wait_s', 0):.0f} s, {t.get('failed')} failed")
    mem = st.get("memory", {})
    print(f"  memory: process up to {mem.get('rss_max_mb')} MB sampled ({mem.get('process_peak_mb')} MB peak since "
          f"the process started), available {mem.get('available_start_mb')} MB at the start, "
          f"{mem.get('available_min_mb')} MB at the least; swap in {mem.get('swap_in_mb')} MB, out "
          f"{mem.get('swap_out_mb')} MB during the run")


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--models", type=int, default=12, help="how many models to list (the costliest first)")
    args = ap.parse_args()
    for path in args.runs:
        title, st = load(path)
        print(title)
        for k, part in enumerate(st.get("parts") or [st]):
            if "parts" in st:
                print(f" part {k + 1} of {len(st['parts'])}:")
            show(part, args.models)
        print()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
