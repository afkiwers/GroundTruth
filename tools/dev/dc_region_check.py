"""A power stage's DC paths solved on the normal region (power.POWER_MARGINS_MM around the parts) and on regions
widened by larger margins, up to the whole board: how much the region's edge changes their resistance and current
density (the user, on conv1's ground map: "are you saying that the right side is cut off?"). Reads the board and
settings only.

    .venv/Scripts/python tools/dev/dc_region_check.py BOARD SETTINGS --only "buck conv1" [--margins 5,100]
        [--resolution 2]
"""
import argparse
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module): one Simulate of the stage per margin, its DC paths side by side."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True, help="the power stage's name")
    ap.add_argument("--margins", default="5,100", help="comma-separated region margins (mm); 100: the whole board")
    ap.add_argument("--resolution", type=float, default=1.0)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis import power
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    board = load_board(Path(args.board))
    cfg = SettingsFile.load(Path(args.settings)).config
    cfg = dataclasses.replace(cfg, analysis=dataclasses.replace(cfg.analysis, resolution=args.resolution))
    for m in (float(v) for v in args.margins.split(",")):
        power.POWER_MARGINS_MM = (m,)
        res = simulate(board, cfg, nets=[args.only])
        c = next(x for x in res.converters if x.net == args.only)
        notes = [n for n in c.notes if "modelled" in n]
        print(f"\nmargin {m:g} mm: {'; '.join(notes)}")
        for p in c.dc:
            at = p["sustained_at"]
            print(f"  {p['name']:32s} R {p['r_mohm']:6.3f} mΩ (copper {p['r_copper_mohm']:.3f})  sustained "
                  f"{p['sustained_a_mm2']:5.1f} A/mm² at ({at[0]:.1f}, {at[1]:.1f}) on {p['sustained_layer']}, "
                  f"peak {p['peak_a_mm2']:.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
