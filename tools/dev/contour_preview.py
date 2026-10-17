"""The contour map "Draw contours on board" would put into KiCad (results/contours.py), drawn over the heatmap as a PNG,
to judge the levels, the smoothing and the labels without KiCad.

    .venv/Scripts/python tools/dev/contour_preview.py BOARD SETTINGS --only NET [--layer In1.Cu] [--cache F.pickle]
                                                       [--out out/contour_preview.png] [--zoom X0,Y0,X1,Y1]

Simulates the net (reads the board and settings only; --cache keeps the maps, so a second run skips the solve),
then draws one layer (default: the one carrying most of the return) as KiCad would: the lines in one colour, index
lines thicker, the labels along their lines, the legend above the copper. Prints the numbers of lines, labels and
graphic segments.
"""
import argparse
import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _maps(args):
    """(layer_grids, sheet_current, layer_share, heat attributes) of the net's result, from --cache when it exists."""
    cache = Path(args.cache) if args.cache else None
    if cache is not None and cache.exists():
        return pickle.loads(cache.read_bytes())
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    rr = simulate(load_board(Path(args.board)), SettingsFile.load(Path(args.settings)).config,
                  nets=[args.only]).routes[0]
    if rr.error:
        sys.exit(f"{args.only}: {rr.error}")
    keep = {k: getattr(rr, k, None) for k in ("layer_grids", "sheet_current", "layer_share", "heat_unit",
                                              "heat_scale", "ambient_c", "view_of", "kind")}
    if cache is not None:
        cache.write_bytes(pickle.dumps(keep))
    return keep


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", required=True)
    ap.add_argument("--layer")
    ap.add_argument("--cache")
    ap.add_argument("--out", default=str(ROOT / "out" / "contour_preview.png"))
    ap.add_argument("--zoom", help="x0,y0,x1,y1 in board mm")
    args = ap.parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm
    from groundtruth.results.contours import contour_map

    m = _maps(args)
    grids, K = m["layer_grids"], m["sheet_current"]
    layer = args.layer or max(m["layer_share"], key=m["layer_share"].get)
    g = grids[layer]
    peak = max(float(np.nanmax(np.where(grids[n].node, k, np.nan))) for n, k in K.items())
    linear = (m.get("heat_scale") or "log") == "linear"
    unit = m.get("heat_unit") or ("°C" if linear else "A/mm²" if m.get("view_of") else "A/m per A")
    cm = contour_map(g, K[layer], peak=peak, scale="linear" if linear else "log",
                     low=m.get("ambient_c") if linear else None, unit=unit, what=f"{args.only} on {layer}")
    x0, x1, y0, y1 = g.extent
    if args.zoom:
        x0, y0, x1, y1 = (float(v) for v in args.zoom.split(","))
    fig, ax = plt.subplots(figsize=(12, 12 * (y1 - y0) / (x1 - x0)), dpi=120)
    Kn = np.where(g.node, K[layer], np.nan)
    ax.imshow(Kn.T, origin="lower", extent=g.extent, cmap="inferno", alpha=0.55,
              norm=None if linear else LogNorm(vmin=peak / 1000, vmax=peak), interpolation="nearest")
    for value, index, pts in cm.lines:
        ax.plot(pts[:, 0], pts[:, 1], color="#00b0ff", lw=1.6 if index else 0.7)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y1, y0 - 4 * cm.text_mm)                 # y down, as in KiCad; room for the legend
    # the label height in points at this scale (the axes take about 80 % of the 12 in wide figure; a capital is
    # about 0.72 of the font size)
    pt = cm.text_mm / ((x1 - x0) / (12 * 0.8)) * 72 / 0.72
    for x, y, ang, text in cm.labels:
        # the axis is flipped like KiCad's (y down), so a counter-clockwise angle as seen reads the same here
        ax.text(x, y, text, rotation=ang, rotation_mode="anchor", ha="center", va="center", fontsize=pt,
                color="#00b0ff", clip_on=True)
    ax.text(*cm.legend_at, cm.legend, color="#00b0ff", fontsize=9, va="center", clip_on=True)
    ax.set_aspect("equal")
    ax.set_title(f"{args.only} on {layer}: {len(cm.levels)} levels, {len(cm.lines)} line pieces, "
                 f"{len(cm.labels)} labels, {cm.segment_count()} segments")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, bbox_inches="tight")
    print(f"{len(cm.levels)} levels {[round(v, 4) for v in cm.levels]}; {len(cm.lines)} line pieces, "
          f"{len(cm.labels)} labels, {cm.segment_count()} segments, text {cm.text_mm:.2f} mm -> {args.out}")
    print(cm.legend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
