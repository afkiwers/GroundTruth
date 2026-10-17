"""A copy of a board with one stage's vias mirrored onto its mirrored twin where the twin has none: for a pair of
power stages laid out as mirror images (kraft_flux_mini_2's conv1 / conv2 about the controller U303, x = 141.84 mm),
so the twin gets the same ground (or other) vias. Writes only OUT, never next to the board.

    .venv/Scripts/python tools/dev/mirror_vias.py BOARD OUT.kicad_pcb --axis-x 141.84 --region 142,86,168,95
        [--nets GND] [--map SW1=SW2 ...] [--min-gap 0.5] [--clearance 0.2] [--dry-run]

--region x0,y0,x1,y1 (mm): where the source vias are taken from (the source stage's side). A source via of a net in
--nets (after --map, which renames the source's nets to the twin's) is mirrored about x = --axis-x, and added when
the twin has no via of that net within --min-gap of the mirrored spot and no copper of another net (pads, tracks,
vias, on any layer: the vias go through) comes within --clearance of it. Zones are not checked: refill them after
(kicad-cli pcb drc --refill-zones --save-board OUT), which clears other nets' pours around the new vias, and run DRC.
Prints every source via with what was done. Same size, drill and start / end layers as the source via; a new uuid.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module): find the vias to add, check each one's clearance, write the copy."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("out")
    ap.add_argument("--axis-x", type=float, required=True)
    ap.add_argument("--region", required=True, help="x0,y0,x1,y1 of the source stage's side (mm)")
    ap.add_argument("--nets", default="GND", help="comma-separated source nets to mirror (default GND)")
    ap.add_argument("--map", nargs="*", default=[], help="SOURCE=TWIN net renames (substrings of the net names)")
    ap.add_argument("--min-gap", type=float, default=0.5, help="a twin via of the net this close counts as there")
    ap.add_argument("--clearance", type=float, default=0.2, help="to other nets' copper (mm)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    src = Path(args.board).resolve()
    if out == src or out.parent == src.parent:
        print("OUT must be in another folder than the board (real boards are never written next to)", file=sys.stderr)
        return 2
    from shapely.geometry import Point, box
    from groundtruth.adapters.file_source import load_board
    from groundtruth.geometry.shapes import pad_shape, track_shape
    board = load_board(src)
    x0, y0, x1, y1 = (float(v) for v in args.region.split(","))
    region = box(x0, y0, x1, y1)
    nets = {n.strip() for n in args.nets.split(",") if n.strip()}
    renames = [tuple(m.split("=", 1)) for m in args.map]

    def twin_net(net: str) -> str:
        """The twin's name for a source net (--map applied)."""
        for a, b in renames:
            net = net.replace(a, b)
        return net

    def mirror(p):
        """A point mirrored about the vertical axis."""
        return (2 * args.axis_x - p[0], p[1])

    # other-net copper near the twin's side, as shapes per net (pads on any layer, tracks, vias: a through via
    # meets all of them)
    copper = []
    for fp in board.footprints:
        for p in fp.pads:
            copper.append((p.net, pad_shape(p)))
    copper += [(t.net, track_shape(t)) for t in board.tracks]
    copper += [(v.net, Point(v.position).buffer(v.size / 2)) for v in board.vias]
    twin_vias = [v for v in board.vias]
    added, text = [], src.read_bytes().decode("utf-8")      # as stored: its own line endings kept
    nl = "\r\n" if "\r\n" in text[:4096] else "\n"
    for v in board.vias:
        if v.net not in nets or not region.contains(Point(v.position)):
            continue
        net, at = twin_net(v.net), mirror(v.position)
        near = min((math.dist(w.position, at) for w in twin_vias if w.net == net), default=math.inf)
        if near < args.min_gap:
            print(f"  {v.net} ({v.position[0]:.2f},{v.position[1]:.2f}): twin has one {near:.2f} mm away")
            continue
        disc = Point(at).buffer(v.size / 2 + args.clearance)
        clash = sorted({n or "(no net)" for n, s in copper if n != net and s is not None and s.intersects(disc)})
        if clash:
            print(f"  {v.net} ({v.position[0]:.2f},{v.position[1]:.2f}) -> ({at[0]:.2f},{at[1]:.2f}): skipped, "
                  f"too close to {', '.join(clash)}")
            continue
        added.append((net, at, v))
        twin_vias.append(type("V", (), {"net": net, "position": at})())       # later ones keep their distance
        print(f"+ {v.net} ({v.position[0]:.2f},{v.position[1]:.2f}) -> {net} ({at[0]:.2f},{at[1]:.2f})")
    if args.dry_run or not added:
        print(f"{len(added)} via(s) to add" + (" (dry run: nothing written)" if args.dry_run else ""))
        return 0
    block = "".join(
        f'\t(via\n\t\t(at {at[0]:.4f} {at[1]:.4f})\n\t\t(size {v.size:g})\n\t\t(drill {v.drill:g})\n'
        # KiCad writes a via's start and end layer only (the board model lists every layer it passes)
        f'\t\t(layers "{v.layers[0]}" "{v.layers[-1]}")\n\t\t(net "{net}")\n'
        f'\t\t(uuid "{uuid.uuid4()}")\n\t)\n'
        for net, at, v in added).replace("\n", nl)
    # into the board's top level, before its first via (KiCad keeps the items in any order and re-sorts on save)
    m = re.search(r"^\t\(via\b", text, re.M)
    if m is None:
        print("no via in the board to insert next to", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes((text[:m.start()] + block + text[m.start():]).encode("utf-8"))
    print(f"{len(added)} via(s) added -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
