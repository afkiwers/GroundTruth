"""How Simulate would group a board's solves (simulate.solve_groups), without solving: builds the models as
Simulate does, then prints every component (runs sharing a model), what it is planned to keep (cached
factorisations) and to have in flight (solve_bytes), and the groups at a given available memory.

    .venv/Scripts/python tools/dev/group_plan.py BOARD SETTINGS [--resolution 8] [--available-gb 17.9] [--src DIR]
        [--platform darwin]

For the speed work (docs/SPEED.md 5.2: refboard's job B ran as 13 groups one after the other on the M2). --src runs
another tree's code (main's in a `git worktree`) for a before/after. Large resolutions build large models: the
memory of the models themselves is needed, not the solves'.
"""
import argparse
import sys
from pathlib import Path


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--resolution", type=float, default=1.0)
    ap.add_argument("--available-gb", type=float, default=None, help="the available memory to plan with "
                    "(default: this machine's)")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--src", help="the src folder of another tree (default: this one's)")
    ap.add_argument("--platform", help="plan as on this platform (sys.platform: win32, darwin, linux; peec.lu_commit)")
    args = ap.parse_args()
    sys.path.insert(0, args.src or str(Path(__file__).resolve().parents[2] / "src"))
    sys.stdout.reconfigure(encoding="utf-8")
    from dataclasses import replace
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis import simulate as sim
    from groundtruth.config.settings import SettingsFile
    from groundtruth.core.sysmem import available_bytes

    if args.platform:                   # SuperLU's memory as that platform counts it (peec.lu_commit)
        sim.peec.sys = type("PlatformSys", (), {"platform": args.platform})()
    board = load_board(args.board)
    cfg = SettingsFile.load(args.settings).config
    cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=args.resolution))
    avail = int(args.available_gb * 2 ** 30) if args.available_gb else available_bytes()

    class Planned(Exception):
        """Raised once the groups are printed: nothing is solved."""

    def plan(runs, threads, available, admitted=False):
        """Print the components and the groups, then stop the Simulate."""
        gb = lambda b: b / 2 ** 30      # noqa: E731
        try:                            # threads auto: each round admitted by memory (since 2026-10-25)
            groups = original(runs, args.threads, avail, admitted=True)
        except TypeError:               # a tree from before
            groups = original(runs, args.threads, avail)
        print(f"{len(runs)} runs, available {gb(avail):.1f} GB, threads {args.threads}: {len(groups)} group(s)")
        for k, g in enumerate(groups):
            held = sim.kept_bytes(g) if hasattr(sim, "kept_bytes") else sum(
                sim.peec.OPERATOR_CACHE * sim.factor_bytes(x) for x in {id(x): x for _, r in g for x in sim._stacks_of(r)}.values())
            names = sorted({f"{getattr(s.spec, 'name', '?')}/{sim.run_kind(s, r)}" for s, r in g})
            print(f"  group {k}: {len(g)} runs, keeps ~{gb(held):.2f} GB, largest solve "
                  f"{gb(max(sim.run_bytes(r) for _, r in g)):.2f} GB: {', '.join(names)}")
        raise Planned()

    original = sim.solve_groups
    sim.solve_groups = plan
    try:
        sim.simulate(board, cfg)
    except Planned:
        return 0
    print("Simulate finished without grouping (free_done off, or one run)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
