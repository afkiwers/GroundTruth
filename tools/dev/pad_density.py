"""The power stages' DC current density at a part's pads, the DC paths alone (no field solve of the hot loops).

    .venv/Scripts/python tools/dev/pad_density.py BOARD SETTINGS --part Q601 [--resolution 2] [--only "buck conv1"]
        [--src DIR] [--json OUT.json]

Builds the enabled stages as Simulate does, solves only their DC paths on the whole-board DC model, sums them as the
"DC currents together" view does (power.combine_dc) and prints: that view's hotspot (sustained and peak A/mm², where),
the part's current, and the density in the copper under and around each of the part's pads (its largest, and where,
and a row of values across the pad's long side through its centre). For checking how a part joins its copper
(simulate.part_lid, [A65]; refboard's Q601 drain read 54 A/mm² in a corner with the sampled joins before 2026-10-24).
--src runs another tree's code (main's in a `git worktree`) for a before/after.
"""
import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--part", required=True, help="the part whose pads to look at (e.g. Q601)")
    ap.add_argument("--resolution", type=float, default=1.0)
    ap.add_argument("--only", nargs="*", help="power stages (default: every enabled one)")
    ap.add_argument("--src", help="the src folder of another tree (default: this one's)")
    ap.add_argument("--json", help="write the figures to this file")
    args = ap.parse_args()
    sys.path.insert(0, args.src or str(Path(__file__).resolve().parents[2] / "src"))
    sys.stdout.reconfigure(encoding="utf-8")
    import numpy as np
    from shapely.geometry import box
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.power import DC_F, combine_dc, dc_analysis, prepare_converters
    from groundtruth.analysis.simulate import BoardModel, solve_at
    from groundtruth.config.settings import SettingsFile
    from groundtruth.geometry.shapes import pad_shape
    t0 = time.perf_counter()
    board = load_board(Path(args.board))
    cfg = SettingsFile.load(Path(args.settings)).config.active()
    cfg = replace(cfg, nets=[], converters=[c for c in cfg.converters if not args.only or c.name in args.only],
                  analysis=replace(cfg.analysis, resolution=args.resolution))
    model = BoardModel(board, cfg)
    setups, _, _ = prepare_converters(model, cfg.converters)
    for s in setups:
        for r in [x for _, _, x in s.dc_runs] + [x for _, x in s.dc_ripple.values()] + \
                ([s.dc_hot[2]] if s.dc_hot else []):
            solve_at(s, r, DC_F)
        s.res.dc, s.res.dc_maps, s.res.dc_grids, _ = dc_analysis(model, s)
    combo, _, _ = combine_dc([s.res for s in setups], cfg.rules, cfg.analysis.via_plating_um)
    view = combo if combo is not None else setups[0].res
    path = view.dc[0]
    out = {"code": args.src or "this tree", "resolution": args.resolution, "seconds": round(time.perf_counter() - t0, 1),
           "sustained_a_mm2": path.get("sustained_a_mm2"), "peak_a_mm2": path.get("peak_a_mm2"),
           "hotspot": path.get("sustained_at") or path.get("peak_at"), "part": {}}
    print(f"{out['code']}, {args.resolution:g}x, {out['seconds']} s: view '{path['name']}': sustained "
          f"{out['sustained_a_mm2']:.1f} A/mm², peak {out['peak_a_mm2']:.1f} A/mm² at {path.get('peak_at')} on "
          f"{path.get('peak_layer')}")
    parts = {p["name"]: p for p in path.get("parts", [])}
    if args.part in parts:
        print(f"  {args.part}: {parts[args.part]['current_a']:.3f} A")
    maps, grids = view.dc_maps[path["name"]], view.dc_grids[path["name"]]
    fp = board.footprint(args.part)
    for pad in fp.pads if fp is not None else []:
        layer = next((l for l in pad.layers if l in maps), None)
        shape = pad_shape(pad)
        if layer is None or shape is None:
            continue
        g, dens = grids[layer], maps[layer]
        x0, y0, x1, y1 = shape.bounds
        ii, jj = np.meshgrid(np.arange(g.nx), np.arange(g.ny), indexing="ij")
        xs, ys = g.xy(ii, jj)
        near = box(x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5)
        inside = (xs >= near.bounds[0]) & (xs <= near.bounds[2]) & (ys >= near.bounds[1]) & (ys <= near.bounds[3])
        if not inside.any():
            continue
        k = np.argmax(np.where(inside, dens, -1))
        i, j = np.unravel_index(k, dens.shape)
        cx, cy = shape.centroid.x, shape.centroid.y
        long_x = (x1 - x0) >= (y1 - y0)
        if long_x:
            row = [(float(x), float(dens[int(round((x - g.x0) / g.d)), int(round((cy - g.y0) / g.d))]))
                   for x in np.linspace(x0, x1, 9)]
        else:
            row = [(float(y), float(dens[int(round((cx - g.x0) / g.d)), int(round((y - g.y0) / g.d))]))
                   for y in np.linspace(y0, y1, 9)]
        out["part"][f"{pad.pin} {layer} @({cx:.2f}, {cy:.2f})"] = {"max_a_mm2": float(dens[i, j]),
                                                                   "max_at": [float(xs[i, j]), float(ys[i, j])],
                                                                   "row": row}
        print(f"  pad {pad.pin} ({pad.net}) on {layer} at ({cx:.2f}, {cy:.2f}), {x1 - x0:.2f} x {y1 - y0:.2f} mm: "
              f"densest {dens[i, j]:.1f} A/mm² at ({xs[i, j]:.2f}, {ys[i, j]:.2f}) (pad ± 0.5 mm)")
        print("    across it (" + ("x" if long_x else "y") + "): " +
              "  ".join(f"{p:.2f}:{v:.1f}" for p, v in row))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
