"""Two Simulates' via currents side by side (development aid): which vias of each power stage's DC paths carry most,
before and after a model change.

    .venv/Scripts/python tools/dev/via_compare.py A.pickle B.pickle [--stage conv2] [--near 138.44,78.6 --radius 3.5]
        [--top 6]

A and B are power-stage results pickled by `thermal_check.py --cache` (run once per tree: main's in a `git
worktree`, or before and after the change). Per DC path: its current, its `--top` busiest vias in A and the same vias
in B, then (with --near) every via within --radius mm of that point in both, as amps and as a share of the path's
current. Vias are matched by name (their net and position)."""
import argparse
import math
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _vias(path: dict) -> dict:
    """A DC path's vias by name: {name: (amps, x, y)}."""
    return {v["name"]: (float(v["current_a"]), float(v["x"]), float(v["y"])) for v in path.get("vias", [])}


def main() -> int:
    """The command line (see the module): load both pickles and print the paths' vias side by side."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--stage", help="part of a stage's name to keep")
    ap.add_argument("--near", help="x,y mm: also list the vias within --radius of this point")
    ap.add_argument("--radius", type=float, default=3.5)
    ap.add_argument("--top", type=int, default=6)
    args = ap.parse_args()
    a = {s.net: s for s in pickle.loads(Path(args.a).read_bytes())}
    b = {s.net: s for s in pickle.loads(Path(args.b).read_bytes())}
    near = tuple(float(v) for v in args.near.split(",")) if args.near else None
    for net, sa in a.items():
        sb = b.get(net)
        if sb is None or (args.stage and args.stage not in net):
            continue
        paths_b = {p["name"]: p for p in sb.dc}
        for pa in sa.dc:
            pb = paths_b.get(pa["name"])
            if pb is None:
                continue
            va, vb = _vias(pa), _vias(pb)
            print(f"\n{net} | {pa['name']} | {pa['current_a']:.3g} A")
            print(f"  {'via':38s} {'A':>8s} {'B':>8s}")
            shown = sorted(va, key=lambda k: -va[k][0])[:args.top]
            shown += [k for k in sorted(vb, key=lambda k: -vb[k][0])[:args.top] if k not in shown]
            for k in shown:
                print(f"  {k[:38]:38s} {va.get(k, (0,))[0]:7.3f}A {vb.get(k, (0,))[0]:7.3f}A")
            if near:
                close = [k for k in set(va) | set(vb)
                         if math.dist((va.get(k) or vb.get(k))[1:], near) <= args.radius]
                if close:
                    print(f"  within {args.radius:g} mm of {near}:")
                    for k in sorted(close, key=lambda k: -(va.get(k, (0,))[0])):
                        ia, ib = va.get(k, (0.0,))[0], vb.get(k, (0.0,))[0]
                        print(f"    {k[:36]:36s} {ia:6.3f} A ({100 * ia / pa['current_a']:4.1f} %)  "
                              f"{ib:6.3f} A ({100 * ib / pb['current_a']:4.1f} %)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
