"""Every route between two points of a board, and everything on it (analysis/path_map.py): the user, 2026-11-03,
"create a script where i can define start and endpoint and it maps every single path in my pcb, including the ones
going to ground and power, there are also OPamps connected to them for bias etc".

    .venv/Scripts/python tools/dev/path_map.py BOARD --from U4.11 --to U1.9
    .venv/Scripts/python tools/dev/path_map.py BOARD --from "Net-(U3A-+)" --to PH_A_DIV --to U53.9 \\
        [--settings S.groundtruth.yaml] [--depth 3] [--max-routes 200] [--max-nets 30] [--json OUT.json] [--dot OUT.dot]

--from / --to: a pin (REF.PAD) or a net (its full name, or the last part of it when that is unique); several --to are
allowed. The board's own settings (<board folder>/groundtruth/<board>.groundtruth.yaml) are read when --settings is
not given and they exist: their part models (an op-amp with GT labels, a part set to 'buffer') decide which op-amp
and buffer channels a route can pass. Read only: nothing is written next to the board. --dot writes a Graphviz graph
(`dot -Tsvg OUT.dot -o OUT.svg` draws it), --json the map as data.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """Read the board (and its settings), map the routes, print them; --json / --dot write the map to files."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", type=Path)
    ap.add_argument("--from", dest="start", required=True, help="the start: a pin (U4.11) or a net")
    ap.add_argument("--to", dest="ends", action="append", required=True, help="an end: a pin or a net (repeatable)")
    ap.add_argument("--settings", type=Path, help="a settings file (default: the board's own, when it exists)")
    ap.add_argument("--depth", type=int, default=3, help="parts followed into a side branch (default 3)")
    ap.add_argument("--max-routes", type=int, default=200)
    ap.add_argument("--max-nets", type=int, default=30)
    ap.add_argument("--json", type=Path, help="the map as JSON")
    ap.add_argument("--dot", type=Path, help="the map as a Graphviz graph")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")       # arrows and Ω on a cp1252 console
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.path_map import format_dot, format_text, trace
    from groundtruth.config.settings import Config, SettingsFile
    board = load_board(args.board)
    settings = args.settings
    if settings is None:
        own = args.board.parent / "groundtruth" / f"{args.board.stem}.groundtruth.yaml"
        settings = own if own.exists() else None
    config = SettingsFile.load(settings).config if settings else Config()
    print(f"{args.board.name}: {len(board.footprints)} parts, {len(board.nets)} nets; settings: "
          f"{settings.name if settings else 'none (models suggested from the board)'}")
    pm = trace(board, config, args.start, args.ends, args.max_routes, args.max_nets, args.depth)
    print(format_text(pm), end="")
    if args.json:
        args.json.write_text(json.dumps(dataclasses.asdict(pm), indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"JSON: {args.json}")
    if args.dot:
        args.dot.write_text(format_dot(pm), encoding="utf-8")
        print(f"Graphviz: {args.dot}  (dot -Tsvg {args.dot.name} -o {args.dot.stem}.svg)")
    return 0 if pm.routes else 1


if __name__ == "__main__":
    sys.exit(main())
