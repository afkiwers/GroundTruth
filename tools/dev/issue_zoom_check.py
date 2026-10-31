"""What a click on each issue of a kept run would show in KiCad (analysis/highlight.issue_highlight), without KiCad:
per issue its check, how many items are selected (focus) and around them (context), the zoom box's size, and a flag
where the click could not zoom (nothing in the focus) or would zoom far out (the box over --wide mm). The Issues tab's
"some don't zoom in" is found here, check by check.

    .venv/Scripts/python tools/dev/issue_zoom_check.py RUN_DIR [--wide 40] [--all]

RUN_DIR is a history run (<board folder>/groundtruth/history/runs/<run>): its results.json, its settings and the
board copy kept with it (extracted to a temporary folder; the board's own folder is only read). --all lists every
issue, else only the flagged ones and a count per check.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from groundtruth.adapters.file_source import load_board  # noqa: E402
from groundtruth.analysis.highlight import issue_highlight  # noqa: E402
from groundtruth.config.settings import SettingsFile  # noqa: E402
from groundtruth.core.issues import Issue  # noqa: E402
from groundtruth.report.history import History, Run  # noqa: E402


def issue_of(d: dict) -> Issue:
    """An Issue from its results.json dict (fields it does not know are left out)."""
    names = {f.name for f in fields(Issue)}
    kw = {k: v for k, v in d.items() if k in names}
    kw["location"] = tuple(kw.get("location") or (0.0, 0.0))
    return Issue(**kw)


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--wide", type=float, default=40.0)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    run_dir = Path(a.run).resolve()
    hist = History(run_dir.parents[1])
    run = hist.find(run_dir.name)
    tmp = Path(tempfile.mkdtemp(prefix="gt_zoom_"))
    board = load_board(hist.extract_board(run, tmp / "board.kicad_pcb"))
    config = SettingsFile.load(run_dir / "settings.groundtruth.yaml").config
    res = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    # the power stages' results as the window has them (kind, net, caps): enough for their issues' capacitors
    routes = [SimpleNamespace(kind="converter", view_of=None, **{k: v for k, v in c.items() if k != "kind"})
              for c in res.get("converters", [])]
    flagged, per = [], Counter()
    for k, d in enumerate(res.get("issues", []), 1):
        i = issue_of(d)
        try:
            h = issue_highlight(i, board, config, routes)
            span = max(h.area[2] - h.area[0], h.area[3] - h.area[1]) if h.area else None
            why = "NO ZOOM (empty focus)" if not h.focus else f"WIDE ({span:.0f} mm)" if span and span > a.wide else ""
        except Exception as e:                     # a handler that fails: no zoom either
            h, span, why = None, None, f"ERROR {type(e).__name__}: {e}"
        per[(i.check, bool(why))] += 1
        line = (f"#{k:<4} {i.check:<24} {i.net[:28]:<28} focus {len(h.focus) if h else '-':>3} "
                f"ctx {len(h.context) if h else '-':>3} box {'-' if span is None else f'{span:6.1f}'} mm  "
                f"{why}  {h.text[:70] if h else ''}")
        if why or a.all:
            flagged.append(line)
    print("\n".join(flagged))
    print("\nper check (ok / flagged):")
    for c in sorted({c for c, _ in per}):
        print(f"  {c:<26} {per[(c, False)]:>4} / {per[(c, True)]:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
