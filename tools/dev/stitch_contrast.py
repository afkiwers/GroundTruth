"""Where the return current goes with and without a stitching via at a layer change (development aid / to show).

    .venv/Scripts/python tools/dev/stitch_contrast.py [--out out/stitch_contrast.png] [--capacitance] [--stitch 1.0]

The test suite's layer-change board (tests/boardgen.py layer_change_board: F.Cu trace -> signal via at x = 20 ->
B.Cu trace, In1 and In2 both GND, the two ICs' GND vias at x = 5 and 35) simulated twice: without a GND via at the
layer change, and with one --stitch mm from the signal via. Each is drawn on the log and on the linear colour
scale: a 2 x 2 picture. Without the stitch the return has to cross In1 -> In2 at the ICs' GND vias (and, with
--capacitance, through the plane capacitance), so on each plane it fans out over the whole board between the
layer change and that via; with it, both planes carry it in a band under their trace.
"""
import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def main() -> int:
    """The command line (see the module): simulate the layer-change test board without and with a stitching via and
    save their return-current maps side by side, on a log and a linear scale."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "out" / "stitch_contrast.png"))
    ap.add_argument("--capacitance", action="store_true", help="with the board-wide plane-capacitance model")
    ap.add_argument("--stitch", type=float, default=1.0, help="stitching via distance from the signal via (mm)")
    args = ap.parse_args()
    import matplotlib.image as mpimg
    import numpy as np
    from boardgen import layer_change_board
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import NetSpec, SettingsFile
    from groundtruth.report.plots import draw_heatmap, figure_png

    tiles = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, stitch in (("without", None), ("with", args.stitch)):
            path = layer_change_board(stitch).write(Path(tmp) / f"{name}.kicad_pcb")
            board = load_board(path)
            sc = SettingsFile.new(Path(tmp) / f"{name}.groundtruth.yaml", board)
            sc.set_nets([NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9)])
            sc.doc["analysis"]["plane_capacitance"] = args.capacitance
            sc.doc["analysis"]["solve_points"] = 2
            sc.save()
            cfg = SettingsFile.load(Path(tmp) / f"{name}.groundtruth.yaml").config
            res = simulate(board, cfg)
            rr = res.routes[0]
            print(f"{name} stitching via: L {rr.solves[0]['L_nH']:.2f} nH ({rr.l_ratio:.2f}x the ideal), "
                  f"layer shares {', '.join(f'{k} {v * 100:.0f} %' for k, v in rr.layer_share.items())}")
            # two maps side by side fit a low figure; more (the via's pads put F.Cu and B.Cu in too) stack in rows
            size = (8, 4.2) if len(rr.main_layers()[:4]) <= 2 else (8, 7)
            for scale in ("log", "linear"):
                png = figure_png(draw_heatmap, board, rr, res.issues, size=size, scale=scale)
                f = Path(tmp) / f"{name}_{scale}.png"
                f.write_bytes(png)
                img = mpimg.imread(f)
                tiles.append((name, scale, img))
    rows = []
    for name in ("without", "with"):
        rows.append(np.concatenate([img for n, _, img in tiles if n == name], axis=1))
    picture = np.concatenate(rows, axis=0)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    mpimg.imsave(out, picture)
    print(f"wrote {out}: rows = without / with a stitching via {args.stitch} mm from the signal via; "
          "columns = log / linear colour scale")
    return 0


if __name__ == "__main__":
    sys.exit(main())
