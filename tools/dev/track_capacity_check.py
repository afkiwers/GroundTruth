"""A long track's current capacity (Thermal's Current capacity on a DC load) against the independent 2D cross-section
(tests/refsolvers.cross_section_rise) with the copper's ρ(T): the current at which the track's middle reaches the
limit (VALIDATION_AUDIT.md round 5, PHYSICS_AUDIT.md section 4.5).

    .venv/Scripts/python tools/dev/track_capacity_check.py [--widths 0.4,0.1] [--limit 105] [--h 10]

A 100 mm track across a 60 mm wide 2-layer board (1.51 mm FR-4), fixed h on both faces, no radiation, 25 °C. The
reference: θ = a·q20·I²·(1 + α(T_amb − 20 + θ)) with a the cross-section's rise per W/m and q20 = ρ20/(w·t) per A²,
solved for the I that gives θ = limit − ambient [IEC 60028]."""
from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    """Run Current capacity for each width and print it beside the reference."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--widths", default="0.4,0.1")
    ap.add_argument("--limit", type=float, default=105.0)
    ap.add_argument("--h", type=float, default=10.0)
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "tests"))
    from boardgen import BoardGen
    from conftest import make
    import refsolvers
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.thermal import run_capacity
    from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile
    from groundtruth.solver.peec import SIGMA_CU
    from groundtruth.thermal.materials import COPPER

    tmp = Path(tempfile.mkdtemp(prefix="track_capacity_"))
    amb = 25.0
    print(f"{'w mm':>6} | {'GroundTruth A':>13} {'reference A':>11} {'ratio':>6}")
    for w in (float(x) for x in args.widths.split(",")):
        g = BoardGen(layers=2, size=(116.0, 60.0))
        for ref, x in (("U1", 8.0), ("U2", 108.0)):
            g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, 30.0),
                        [{"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)}])
        g.track((8.0, 30.0), (108.0, 30.0), "PWR", "F.Cu", width=w)
        b, sc, _ = make(tmp, g, f"cap{w}".replace(".", "p"), nets=())
        sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 1.0, "")], "")])
        sc.doc["thermal"] = {"convection": "fixed", "h_top": args.h, "h_bottom": args.h, "radiation": False,
                             "ambient_c": amb, "quality": "normal", "capacity_c": args.limit}
        sc.save()
        cfg = SettingsFile.load(sc.path).config
        res = simulate(b, cfg).dc_loads
        caps = run_capacity(b, cfg, res)
        cap = next((c.capacity for c in caps if c.capacity), {})
        got = cap.get("i_capacity")
        a = refsolvers.cross_section_rise(w, 1.0, h_top=args.h, h_bottom=args.h)
        q20 = 1.0 / (SIGMA_CU * w * 1e-3 * 35e-6)
        theta = args.limit - amb
        # theta = a q20 I² (1 + α (amb − 20 + theta))  →  I² = theta / (a q20 (1 + α (amb − 20 + theta)))
        i_ref = math.sqrt(theta / (a * q20 * (1 + COPPER.alpha * (amb - 20.0 + theta))))
        print(f"{w:6.3f} | {got if got is None else round(got, 3):>13} {i_ref:11.3f} "
              f"{(got / i_ref) if got else float('nan'):6.3f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
