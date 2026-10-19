"""Track width through the whole chain: resistance, drop, loss, current density and temperature of a copper track
against independent results, for a sweep of widths (VALIDATION_AUDIT.md round 5, "Track width").

    .venv/Scripts/python tools/dev/track_width_check.py [--widths 0.4,0.3,0.2,0.15,0.1,0.075,0.05] [--amps 1]
        [--resolution 1] [--offsets 0,0.03,0.06] [--angles 0,30,45] [--thermal] [--src DIR] [--json OUT]

Every case is a synthetic board (tests/boardgen.py) with one DC load (analysis/dcload.py), simulated as the plugin
does:
  * straight and angled tracks (two lengths, the difference cancels the pads): R' against 1/(σ·w·t), the drop and
    the loss at the set current, the current density at the track's middle and its sustained maximum against
    I/(w·t), the copper heat the thermal solve gets against the path's own I²R;
  * with --thermal: a 100 mm track across a 60 mm wide 2-layer board, fixed h = 10 W/m²K on both faces, no
    radiation: the solver's copper temperature at the middle of the track against the independent 2D cross-section
    (tests/refsolvers.cross_section_rise) with the copper's ρ(T) [IEC 60028].

--src runs another source tree (a `git worktree` of main for the code before a change): its `src` goes first on
sys.path. The boards are written to a temporary folder; nothing next to a real board is touched.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    """Run the sweep and print (and optionally write) the table."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--widths", default="0.4,0.3,0.2,0.15,0.1,0.075,0.05")
    ap.add_argument("--amps", type=float, default=1.0)
    ap.add_argument("--resolution", type=float, default=1.0)
    ap.add_argument("--offsets", default="0,0.03,0.06", help="the track's y offsets from the grid (mm)")
    ap.add_argument("--angles", default="0,30,45")
    ap.add_argument("--thermal", action="store_true")
    ap.add_argument("--src", default=str(ROOT / "src"), help="the source tree to test (default: this one)")
    ap.add_argument("--json", help="write the rows here")
    args = ap.parse_args()
    sys.path.insert(0, str(Path(args.src)))
    sys.path.insert(0, str(ROOT / "tests"))
    import numpy as np
    from boardgen import BoardGen
    from conftest import make
    import groundtruth
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.thermal import thermal_inputs
    from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile
    from groundtruth.solver.peec import SIGMA_CU

    print(f"source: {Path(groundtruth.__file__).parent}")
    tmp = Path(tempfile.mkdtemp(prefix="track_width_"))
    T_CU = 35e-6
    amps = args.amps
    rows = []

    def track_board(length, width, angle, dy):
        """One PWR track from U1.1 to U2.1 at `angle`°, its start `dy` mm off the grid's rows, a GND plane on In1."""
        a = math.radians(angle)
        x0, y0 = 6.0, 6.0 + dy
        x1, y1 = x0 + length * math.cos(a), y0 + length * math.sin(a)
        g = BoardGen(layers=4, size=(x1 + 8, max(y1 + 8, 20.0)))
        for ref, (x, y) in (("U1", (x0, y0)), ("U2", (x1, y1))):
            g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, y), [
                {"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)},
                {"number": 2, "at": (0, 3.0), "net": "GND", "size": (1.0, 1.0)}])
        g.track((x0, y0), (x1, y1), "PWR", "F.Cu", width=width)
        g.plane("GND", "In1.Cu")
        return g, ((x0 + x1) / 2, (y0 + y1) / 2)

    def rail(gen, name):
        """The board with one 'rail' DC load of `amps` from U1.1 to U2.1, simulated: (board, config, result)."""
        b, sc, _ = make(tmp, gen, name, nets=())
        sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", amps, "")], "")])
        sc.save()
        cfg = SettingsFile.load(sc.path).config
        cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=args.resolution))
        res = simulate(b, cfg).dc_loads[0]
        if res.error:
            raise RuntimeError(f"{name}: {res.error}")
        return b, cfg, res

    def at(res, layer, x, y):
        """The DC density map's value (A/mm²) at the node nearest (x, y)."""
        name = next(iter(res.dc_maps))
        g, m = res.dc_grids[name][layer], res.dc_maps[name][layer]
        i, j = int(round((x - g.x0) / g.d)), int(round((y - g.y0) / g.d))
        return float(m[i, j])

    widths = [float(w) for w in args.widths.split(",")]
    print(f"{'w mm':>6} {'angle':>5} {'dy':>5} | {'R/exact':>7} {'drop mV':>8} {'loss mW':>8} | "
          f"{'J mid':>7} {'J sus':>7} {'J exact':>7} | {'heat/loss':>9}")
    for w in widths:
        exact = 1 / (SIGMA_CU * w * 1e-3 * T_CU)
        for angle in (float(a) for a in args.angles.split(",")):
            for dy in ((float(o) for o in args.offsets.split(",")) if angle == 0 else (0.0,)):
                out = {}
                for length in (20.0, 40.0):
                    gen, mid = track_board(length, w, angle, dy)
                    b, cfg, res = rail(gen, f"w{w}_a{angle}_o{dy}_l{length}".replace(".", "p"))
                    out[length] = (b, cfg, res, mid)
                b, cfg, res, mid = out[40.0]
                r = (out[40.0][2].loads[0]["supply_mv"] - out[20.0][2].loads[0]["supply_mv"]) * 1e-3 / amps / 0.020
                path = res.dc[0]
                heat, _, _ = thermal_inputs(b, cfg, [res])
                q = sum(float(np.sum(p)) for (_, _, _, p) in heat.values())
                j_exact = amps / (w * 0.035)
                row = {"width_mm": w, "angle": angle, "offset_mm": dy, "r_per_m": r, "r_exact_per_m": exact,
                       "drop_mv": res.loads[0]["supply_mv"], "loss_w": path["loss_w"],
                       "j_mid": at(res, "F.Cu", *mid), "j_sustained": path["sustained_a_mm2"],
                       "j_peak": path["peak_a_mm2"], "j_exact": j_exact, "heat_w": q}
                rows.append(row)
                print(f"{w:6.3f} {angle:5.0f} {dy:5.2f} | {r / exact:7.3f} {row['drop_mv']:8.2f} "
                      f"{row['loss_w'] * 1e3:8.2f} | {row['j_mid']:7.1f} {row['j_sustained']:7.1f} {j_exact:7.1f} | "
                      f"{q / path['loss_w']:9.3f}", flush=True)
    if args.thermal:
        import refsolvers
        from groundtruth.analysis import thermal as T
        from groundtruth.thermal.materials import COPPER
        print("\nthermal: 100 mm track, 60 mm board, 2 layers, h = 10 W/m²K both faces, no radiation, 25 °C")
        print(f"{'w mm':>6} | {'GT node K':>9} {'GT map K':>8} {'ref K':>7} {'node/ref':>8} {'map/ref':>7}")
        for w in widths:
            g = BoardGen(layers=2, size=(116.0, 60.0))
            for ref, x in (("U1", 8.0), ("U2", 108.0)):
                g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, 30.0),
                            [{"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)}])
            g.track((8.0, 30.0), (108.0, 30.0), "PWR", "F.Cu", width=w)
            b, sc, _ = make(tmp, g, f"th{w}".replace(".", "p"), nets=())
            sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", amps, "")], "")])
            sc.doc["thermal"] = {"convection": "fixed", "h_top": 10.0, "h_bottom": 10.0, "radiation": False,
                                 "ambient_c": 25.0, "quality": "normal"}
            sc.save()
            cfg = SettingsFile.load(sc.path).config
            res = simulate(b, cfg).dc_loads[0]
            heat, losses, notes = T.thermal_inputs(b, cfg, [res])
            m = T._build_model(b, cfg, heat, losses, list(notes), lambda *a: None, None)
            sol = T._coupled(m, m.p20, m.Qc)
            col = m.mesh.locate(np.array([58.0]), np.array([30.0]))
            t_node = float(sol.T[m.th.cu_nodes(col, np.zeros(1, np.int64))][0]) - 25.0
            th, _ = T._result(b, cfg, m, sol, losses)
            gg, mm = th.layer_grids["F.Cu"], th.sheet_current["F.Cu"]
            i, j = int(round((58.0 - gg.x0) / gg.d)), int(round((30.0 - gg.y0) / gg.d))
            t_map = float(np.nanmax(mm[i - 1:i + 2, j - 1:j + 2])) - 25.0
            q20 = amps ** 2 / (SIGMA_CU * w * 1e-3 * T_CU)
            a = refsolvers.cross_section_rise(w, 1.0)
            ref = a * q20 * (1 + COPPER.alpha * 5.0) / (1 - a * q20 * COPPER.alpha)
            rows.append({"width_mm": w, "thermal_node_k": t_node, "thermal_map_k": t_map, "thermal_ref_k": ref,
                         "notes": list(m.notes)})
            print(f"{w:6.3f} | {t_node:9.2f} {t_map:8.2f} {ref:7.2f} {t_node / ref:8.3f} {t_map / ref:7.3f}"
                  + (f"   ({'; '.join(m.notes)})" if m.notes else ""), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
