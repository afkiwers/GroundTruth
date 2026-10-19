"""A neck in a track at several resolutions against an independent sheet solve: what a 0.1 mm neck adds to a 30 mm,
0.4 mm track's resistance (the same track without it subtracted, so the pads cancel), GroundTruth's DC load against
the finite-difference sheet solve of the same copper (tests/refsolvers.sheet_resistance, h = 0.01 mm). The width
step's placement (PHYSICS_AUDIT.md section 4.2, REFERENCES A39, ISSUE-21).

    .venv/Scripts/python tools/dev/neck_check.py [--necks 2,5,20] [--resolutions 1,2,4,16] [--src DIR]

The boards are tests/test_track_width._neck_board's (synthetic, in a temporary folder). --src runs another source
tree (a `git worktree` of main): its `src` goes first on sys.path. 16x takes a few minutes."""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    """Print each neck's added resistance per resolution against the reference, in % and in squares."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--necks", default="2,5,20", help="neck lengths (mm)")
    ap.add_argument("--resolutions", default="1,2,4,16")
    ap.add_argument("--src", default=str(ROOT / "src"), help="the source tree to test (default: this one)")
    args = ap.parse_args()
    sys.path.insert(0, str(Path(args.src)))
    sys.path.insert(0, str(ROOT / "tests"))
    import groundtruth
    import refsolvers
    import test_track_width as tw
    from groundtruth.solver.peec import SIGMA_CU

    print(f"source: {Path(groundtruth.__file__).parent}")
    tmp = Path(tempfile.mkdtemp(prefix="neck_check_"))
    square = 1.0 / (SIGMA_CU * tw.T_CU)                         # ohms per square of the copper
    g0, cu0, pa, pb = tw._neck_board(0.0)
    ref0 = refsolvers.sheet_resistance(cu0, pa, pb, 0.01, SIGMA_CU, tw.T_CU)
    for neck in (float(v) for v in args.necks.split(",")):
        gn, cun, _, _ = tw._neck_board(neck)
        ref = refsolvers.sheet_resistance(cun, pa, pb, 0.01, SIGMA_CU, tw.T_CU) - ref0
        for res in (float(v) for v in args.resolutions.split(",")):
            r0 = tw._rail(tmp, g0, f"n0_{res}".replace(".", "p"), resolution=res)[2]
            rn = tw._rail(tmp, gn, f"n{neck}_{res}".replace(".", "p"), resolution=res)[2]
            d_gt = (rn.loads[0]["supply_mv"] - r0.loads[0]["supply_mv"]) * 1e-3
            print(f"neck {neck:g} mm, {res:g}x (d {rn.grid.d:.4f} mm): adds {d_gt * 1e3:.3f} mΩ, reference "
                  f"{ref * 1e3:.3f}: {d_gt / ref - 1:+.2%}, {(d_gt - ref) / square:+.3f} square", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
