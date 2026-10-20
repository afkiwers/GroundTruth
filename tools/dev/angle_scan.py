"""A straight track's resistance per length by angle (audit round 6, development aid): R' from a 20 and a 40 mm track
(the pads cancel) against 1/(σ·w·t), for one width over a range of angles and grid positions.

    .venv/Scripts/python tools/dev/angle_scan.py [--width 0.1] [--angles 0:90:5] [--offsets 0,0.1] [--res 1]
"""
import argparse
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def r_per_m(tmp: Path, w: float, angle: float, off: float, resolution: float) -> float:
    """R' (Ω/m) of a w mm track at `angle`° whose start is `off` mm into a cell, from 20 and 40 mm."""
    from boardgen import BoardGen
    from conftest import make
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile
    a = math.radians(angle)
    drops = []
    for L in (20.0, 40.0):
        x0, y0 = 6.0 + off, 6.0 + 0.7 * off
        x1, y1 = x0 + L * math.cos(a), y0 + L * math.sin(a)
        g = BoardGen(layers=2, size=(x1 + 8.0, y1 + 8.0))
        for ref, at in (("U1", (x0, y0)), ("U2", (x1, y1))):
            g.footprint(ref, "IC", "Package_SO:Fake_IC", at, [{"number": 1, "at": (0, 0), "net": "PWR",
                                                               "size": (1.0, 1.0)}])
        g.track((x0, y0), (x1, y1), "PWR", "F.Cu", width=w)
        b, sc, _ = make(tmp, g, f"a{angle:.1f}_{off}_{L}".replace(".", "p"), nets=())
        sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 1.0, "")], "")])
        sc.save()
        cfg = SettingsFile.load(sc.path).config
        cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
        r = simulate(b, cfg).dc_loads[0]
        assert not r.error, r.error
        drops.append(r.loads[0]["supply_mv"] * 1e-3)
    return (drops[1] - drops[0]) / 0.020


def main() -> int:
    """The command line (see the module): one line per angle, R'/exact per offset."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width", type=float, default=0.1)
    ap.add_argument("--angles", default="0:90:5")
    ap.add_argument("--offsets", default="0,0.1")
    ap.add_argument("--res", type=float, default=1.0)
    args = ap.parse_args()
    from groundtruth.solver.peec import SIGMA_CU
    lo, hi, step = (float(x) for x in args.angles.split(":"))
    angles = [lo + k * step for k in range(int(round((hi - lo) / step)) + 1)]
    offsets = [float(x) for x in args.offsets.split(",")]
    exact = 1 / (SIGMA_CU * args.width * 1e-3 * 35e-6)
    with tempfile.TemporaryDirectory() as tmp:
        for ang in angles:
            vals = [r_per_m(Path(tmp), args.width, ang, off, args.res) / exact for off in offsets]
            print(f"{ang:5.1f}°  " + "  ".join(f"{v - 1:+.2%}" for v in vals), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
