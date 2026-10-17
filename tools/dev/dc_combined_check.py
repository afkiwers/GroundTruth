"""Every power stage's DC paths on the shared whole-board model, each stage alone and all together (power.combine_dc):
the paths' resistance, density and busiest via per stage, the combined view's hotspot and vias, the DC issues, and
how long the Simulate took. Reads the board and settings only.

    .venv/Scripts/python tools/dev/dc_combined_check.py BOARD SETTINGS [--only "buck conv1" "buck conv2"]
        [--resolution 2]
"""
import argparse
import dataclasses
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", nargs="*", default=None, help="power stages to simulate (default: all enabled)")
    ap.add_argument("--resolution", type=float, default=1.0)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    board = load_board(Path(args.board))
    cfg = SettingsFile.load(Path(args.settings)).config
    cfg = dataclasses.replace(cfg, analysis=dataclasses.replace(cfg.analysis, resolution=args.resolution))
    t0 = time.time()
    res = simulate(board, cfg, nets=args.only)
    print(f"Simulate {time.time() - t0:.0f} s")
    for c in list(res.converters) + ([res.dc_combined] if res.dc_combined is not None else []):
        print(f"\n{c.net}: " + "; ".join(n for n in c.notes if "modelled" in n or "together" in n))
        for p in c.dc:
            via = p["vias"][0] if p["vias"] else None
            at = p["sustained_at"]
            print(f"  {p['name']:32s} {p['current_a']:6.3g} A  R {p['r_mohm']:6.3f} mΩ  loss {p['loss_w']:.3f} W  "
                  f"sustained {p['sustained_a_mm2']:5.1f} A/mm² at ({at[0]:.1f}, {at[1]:.1f}) {p['sustained_layer']}, "
                  f"peak {p['peak_a_mm2']:.0f}" + (f"; {via['name']} {via['current_a']:.2f} A "
                                                   f"({via['load_pct']:.0f} %)" if via else ""))
    print("\nDC issues:")
    for i in res.issues:
        if i.check in ("current_density", "via_overload"):
            print(f"  {i.severity:8s} {i.check:15s} {i.net}: {i.message[:150]}")
    print("notes:", res.notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
