"""Power stages' hot loops drawn on a user layer of a COPY of the board, from a kept Simulate's results (no solve):
what the plugin's "Draw hot loop on board" puts into KiCad (results/hotloop.py), for a board KiCad does not have open.

    .venv/Scripts/python tools/dev/hotloop_draw.py BOARD RESULTS.json OUT.kicad_pcb [--stages "buck conv1,buck conv2"]
        [--layer User.1] [--min-share 0.05]

RESULTS.json is a history run's results.json (<board folder>/groundtruth/history/runs/<run>/results.json). The board
itself is only read; OUT must not be next to it.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from groundtruth.adapters.file_source import load_board  # noqa: E402
from groundtruth.results.hotloop import MIN_SHARE, can_draw, hot_loop_drawing  # noqa: E402


def sexpr(d, layer: str) -> str:
    """A HotLoopDrawing as KiCad S-expressions: gr_line segments and left-aligned gr_text."""
    out = ""
    for pts, width, closed in d.lines:
        pts = list(pts) + ([pts[0]] if closed else [])
        out += "".join(f'\t(gr_line (start {a[0]:.4f} {a[1]:.4f}) (end {b[0]:.4f} {b[1]:.4f}) (stroke (width '
                       f'{width}) (type solid)) (layer "{layer}") (uuid "{uuid.uuid4()}"))\n'
                       for a, b in zip(pts[:-1], pts[1:]))
    for x, y, text, size in d.texts:
        text = text.replace('"', "'")
        out += (f'\t(gr_text "{text}" (at {x:.4f} {y:.4f} 0) (layer "{layer}") (uuid "{uuid.uuid4()}") '
                f'(effects (font (size {size} {size}) (thickness {size * 0.15:.3f})) (justify left)))\n')
    return out


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("results")
    ap.add_argument("out")
    ap.add_argument("--stages", default="")
    ap.add_argument("--layer", default="User.1")
    ap.add_argument("--min-share", type=float, default=MIN_SHARE)
    a = ap.parse_args()
    src, dst = Path(a.board).resolve(), Path(a.out).resolve()
    if dst.parent == src.parent:
        sys.exit("OUT must not be next to the board (the board is only read)")
    board = load_board(src)
    res = json.loads(Path(a.results).read_text(encoding="utf-8"))
    want = [s.strip() for s in a.stages.split(",") if s.strip()]
    items, k = "", 0
    for c in res["converters"]:
        rr = SimpleNamespace(**c)                 # the result's fields, as the plugin's ConverterResult has them
        if want and rr.net not in want:
            continue
        why = can_draw(rr)
        if why:
            print(f"{rr.net}: {why}")
            continue
        try:
            d = hot_loop_drawing(board, rr, a.min_share, k)
        except ValueError as e:
            print(e)
            continue
        k += 1
        items += sexpr(d, a.layer)
        print("\n  ".join(d.log))
    text = src.read_text(encoding="utf-8")
    cut = text.rstrip().rfind(")")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text[:cut] + items + text[cut:], encoding="utf-8")
    print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
