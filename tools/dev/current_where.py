"""Where a power stage's current flows inside a box of the board, per view and layer, and on which nets' copper
(development aid: checks "the current moves where it should not").

    .venv/Scripts/python tools/dev/current_where.py BOARD STAGES.pickle X0 Y0 X1 Y1 [--frac 0.03]

STAGES.pickle: power-stage results as `tools/dev/thermal_check.py --cache` writes them. For every stage, its hot-loop
view and each DC path view, and every copper layer: the peak |J| on the layer, the largest |J| inside the box (as a
fraction of the layer's peak and of the view's peak), how many grid cells in the box are over --frac of the view's
peak (where the plugin draws arrows), and which nets' copper those cells sit on (zone fills, pads, tracks)."""
import argparse
import pickle
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def copper_index(board):
    """Per layer: [(net, shapely geometry)] of zone fills, pads and tracks."""
    from shapely.geometry import LineString, Point, Polygon
    out = {L: [] for L in board.copper_layers}
    for z in board.zones:
        for L, polys in z.filled.items():
            for outer, holes in polys:
                if len(outer) >= 3 and L in out:
                    out[L].append((z.net, Polygon(outer, [h for h in holes if len(h) >= 3]).buffer(0)))
    for t in board.tracks:
        if t.layer in out:
            out[t.layer].append((t.net, LineString([t.start, t.end]).buffer(t.width / 2)))
    for p in board.pads():
        for L in p.layers:
            if L in out:
                out[L].append((p.net, Point(p.position).buffer(max(p.size) / 2)))
    return out


def nets_at(index, layer, xs, ys):
    """Which net's copper each point is on (an STRtree of the layer's copper), counted per net."""
    from shapely.geometry import Point
    from shapely.strtree import STRtree
    items = index[layer]
    tree = STRtree([g for _, g in items])
    got = Counter()
    for x, y in zip(xs, ys):
        pt = Point(x, y)
        hit = [items[i][0] for i in tree.query(pt) if items[i][1].covers(pt)]
        got[hit[0] if hit else "(no copper)"] += 1
    return got


def views(c):
    """A stage's current views: (title, grids per layer, |J| per layer) for the hot loop and each DC path."""
    if getattr(c, "sheet_vectors", None):
        yield "hot loop", c.layer_grids, {L: np.hypot(*v) for L, v in c.sheet_vectors.items()}
    for name, layers in (c.dc_vectors or {}).items():
        yield "DC " + name, c.dc_grids.get(name, {}), {L: np.hypot(jx, jy) for L, (jx, jy) in layers.items()}


def main() -> int:
    """The command line (see the module): for every stage, view and layer, the peak, the largest |J| in the box, the
    cells over --frac of the view's peak and the nets under them."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("stages")
    ap.add_argument("box", nargs=4, type=float)
    ap.add_argument("--frac", type=float, default=0.03)
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    board = load_board(args.board)
    stages = pickle.loads(Path(args.stages).read_bytes())
    x0, y0, x1, y1 = args.box
    index = copper_index(board)
    for c in stages:
        for title, grids, mags in views(c):
            peak = max((float(np.nanmax(m)) for m in mags.values() if m.size), default=0.0) or 1.0
            print(f"\n{c.net} — {title} (view peak {peak:.4g})")
            for L, m in mags.items():
                g = grids.get(L)
                if g is None:
                    continue
                X = g.x0 + np.arange(g.nx)[:, None] * g.d + 0 * np.arange(g.ny)[None, :]
                Y = g.y0 + np.arange(g.ny)[None, :] * g.d + 0 * np.arange(g.nx)[:, None]
                inside = (X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1) & g.node
                if not inside.any():
                    print(f"  {L:7s} box outside the model region")
                    continue
                mm = np.nan_to_num(m)
                lp = float(mm.max()) or 1.0
                box = mm[inside]
                hot = inside & (mm > args.frac * peak)
                nets = nets_at(index, L, X[hot], Y[hot]) if hot.any() else Counter()
                print(f"  {L:7s} layer peak {lp:9.4g}  in box max {box.max():9.4g} ({box.max() / lp:6.1%} of layer, "
                      f"{box.max() / peak:6.1%} of view)  arrow cells {int(hot.sum()):5d}/{int(inside.sum()):5d}  "
                      + ", ".join(f"{n} {k}" for n, k in nets.most_common(4)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
