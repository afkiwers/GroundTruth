"""Which signal paths get their line from the field-solved cut and which fall back on the closed forms (development
aid, docs/NEXT_STEPS.md; VALIDATION_AUDIT ISSUE-5): per configured net, the main layer, the closed-form Z0, the cut's
Z0 at the longest track (analysis/si.SectionCache, as simulate._line_of takes it) and their ratio; "no cut" where
none could be made there, so the closed forms are used (up to ~10 % high on lopsided inner layers, no solder mask).
Reads the board and the settings only.

    .venv/Scripts/python tools/dev/line_cut_check.py BOARD SETTINGS
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.si import SectionCache
    from groundtruth.analysis.simulate import BoardModel, _line_of
    from groundtruth.components.fields import apply_fields
    from groundtruth.config.settings import SettingsFile
    from groundtruth.rules.checks import run_checks
    board = load_board(args.board)
    cfg = SettingsFile.load(Path(args.settings)).config
    apply_fields(cfg, board)
    cfg = replace(cfg, nets=[replace(n, enabled=True) for n in cfg.nets], converters=[], bridges=[])
    model = BoardModel(board, cfg)
    checks = run_checks(board, cfg, model.copper, None)
    cache = SectionCache(board, model.copper)
    print(f"{'net':30s} {'layer':8s} {'closed':>7s} {'cut':>7s}  closed/cut")
    fallback = 0
    for (net, rx), route in checks.routes.items():
        segs = [s for s in route.segments if s.kind == "track"]
        if not segs:
            continue
        z_closed, _ = _line_of(board, segs, "")
        z_cut, _ = _line_of(board, segs, "", cache)
        layer = max({s.layer for s in segs}, key=lambda l: sum(s.length for s in segs if s.layer == l))
        if abs(z_cut - z_closed) < 1e-9:              # the cache gave nothing: the closed form came back
            fallback += 1
            print(f"{net[-30:]:30s} {layer:8s} {z_closed:7.2f} {'no cut':>7s}")
        else:
            print(f"{net[-30:]:30s} {layer:8s} {z_closed:7.2f} {z_cut:7.2f}  {z_closed / z_cut:9.3f}")
    print(f"{fallback} path(s) on the closed forms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
