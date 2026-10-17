"""On the machine with the GPU: same results as the CPU, and how much faster? (run after tools/install_gpu.py)

    python tools/dev/gpu_check.py BOARD SETTINGS [--only SPI_SCK,SW1] [--threads N]

Simulates the chosen nets / power stages twice in one process, analysis.gpu off and then on, and prints the two
times, where the second run's solves went (and any that fell back to the CPU), and the largest differences between
the two results (routes, power stages and issues, number by number as tools/dev/result_diff.py does). Expect
differences at the GMRES tolerance (~1e-5 relative, a few 1e-5 dB) and the same issues. Never writes next to the
board. On a machine without a GPU, GROUNDTRUTH_GPU_DEVICE=cpu runs the GPU code with PyTorch on the CPU (slower than
numpy: a check of the code, not of the speed).
"""
import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))


def summary(res) -> dict:
    """A result's routes, power stages and issues as plain data, for the number-by-number comparison."""
    return {"routes": [r.summary() for r in res.routes], "converters": [c.summary() for c in res.converters],
            "issues": [{"fingerprint": i.fingerprint, "check": i.check, "net": i.net, "message": i.message}
                       for i in res.issues]}


def main() -> int:
    """The command line (see the module): simulate with the GPU off, then on, and print the times, the device used,
    CPU fallbacks and the largest differences between the two results."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", help="comma-separated parts of net / power stage names to keep")
    ap.add_argument("--threads", type=int)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    from groundtruth.solver import gpu
    from result_diff import walk
    try:
        dev = gpu.probe("on")
    except gpu.GpuUnavailable as e:
        print(e, file=sys.stderr)
        return 1
    print(f"GPU: {dev.name}")
    board = load_board(args.board)
    cfg = SettingsFile.load(Path(args.settings)).config
    if args.only:
        keys = args.only.split(",")
        cfg = replace(cfg, nets=[n for n in cfg.nets if any(k in n.name for k in keys)],
                      converters=[c for c in cfg.converters if any(k in c.name for k in keys)])
    runs = {}
    for mode in ("off", "on"):
        c = replace(cfg, analysis=replace(cfg.analysis, gpu=mode,
                                          **({"threads": args.threads} if args.threads else {})))
        t = time.perf_counter()
        res = simulate(board, c)
        runs[mode] = (time.perf_counter() - t, res)
        print(f"gpu {mode:3s}: {runs[mode][0]:7.1f} s   solver: {gpu.describe(res.solver)}")
    (t_cpu, cpu), (t_gpu, on) = runs["off"], runs["on"]
    print(f"speed-up {t_cpu / t_gpu:.2f}x")
    diffs, missing = [], []
    walk(summary(cpu), summary(on), "", diffs, missing)
    db = sorted((d for d in diffs if d[1] == "dB"), reverse=True)
    rel = sorted((d for d in diffs if d[1] == "rel" and "iterations" not in d[2]), reverse=True)
    print(f"\n{len(diffs)} numbers compared: largest {db[0][0] if db else 0:.2g} dB, {rel[0][0] if rel else 0:.2g} "
          f"relative (iteration counts left out)")
    for d, kind, path, a, b in (db[: args.top] + rel[: args.top]):
        print(f"  {d:10.3g} {kind:3s}  {path}: {a!r} vs {b!r}")
    for m in missing[: args.top]:
        print("  only in one / different text: " + m)
    ok = (not db or db[0][0] < 0.01) and not missing
    print("\nthe GPU gives the CPU's results" if ok else "\nDIFFERENT RESULTS: see above, and keep analysis.gpu off")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
