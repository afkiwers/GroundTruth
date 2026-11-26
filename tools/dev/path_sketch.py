"""A signal path's schematic as the Signal paths tab draws it (report/schematics.draw_path), to a PNG, without the
window: check its layout on a real board's paths.

    .venv/Scripts/python tools/dev/path_sketch.py BOARD SETTINGS OUT_DIR [--nets NAME ...] [--dark]

One PNG per path (all of the settings' paths without --nets), and the path's text beside it on the console. Only
reads the board and the settings.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def main() -> int:
    """Draw each path of the settings (or those named) to OUT_DIR/<path>.png."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("out_dir")
    ap.add_argument("--nets", nargs="*", help="the paths to draw (default: all)")
    ap.add_argument("--dark", action="store_true", help="the dark theme's colours")
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.route import path_chain
    from groundtruth.config.settings import SettingsFile
    from groundtruth.report.schematics import Ink, draw_path, png
    board = load_board(Path(args.board))
    cfg = SettingsFile.load(Path(args.settings)).config
    ink = Ink("#e7e9ee", "#9aa3b2", "#4cc2ff", "#21252d") if args.dark else Ink()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for spec in cfg.nets:
        if args.nets and spec.name not in args.nets:
            continue
        chain = path_chain(board, cfg, spec)
        print(f"== {spec.name}\n{chain.full_text()}")
        fig = draw_path(chain, ink)
        if fig is None:
            continue
        f = out / (re.sub(r"[^\w.-]+", "_", spec.name).strip("_") + ".png")
        f.write_bytes(png(fig))
        print(f"   -> {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
