"""A board's power stages' DC paths and the thermal solve, as numbers to compare between two versions of the code
(VALIDATION_AUDIT.md round 5: the real board before and after the grid's exact widths).

    .venv/Scripts/python tools/dev/dc_thermal_summary.py BOARD SETTINGS [--src DIR] [--json OUT] [--gpu off]
        [--resolution N] [--no-thermal] [--quality draft|normal|fine] [--refine-ring N]

Simulates only the settings' converters (and DC loads), then Thermal, and prints per stage each DC path's resistance,
loss and sustained / peak current density, the stages' copper loss together, and Thermal's hottest copper, its place,
the copper heat (at 20 °C and at temperature), the parts' heat and the hottest parts. --src runs another source tree
(a `git worktree` of main): its `src` goes first on sys.path. --quality overrides the settings' thermal.quality,
--refine-ring the blocks refined around each hot one (thermal/solver/mesh.REFINE_RING). The board is only read;
nothing is written next to it."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    """Simulate the stages, run Thermal, print and optionally write the figures."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--src", default=str(ROOT / "src"))
    ap.add_argument("--json")
    ap.add_argument("--gpu", default="off")
    ap.add_argument("--resolution", type=float, default=1.0)
    ap.add_argument("--no-thermal", action="store_true")
    ap.add_argument("--quality", choices=("draft", "normal", "fine"))
    ap.add_argument("--refine-ring", type=int)
    args = ap.parse_args()
    sys.path.insert(0, str(Path(args.src)))
    from dataclasses import replace
    import groundtruth
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.thermal import run_thermal
    from groundtruth.config.settings import SettingsFile

    print(f"source: {Path(groundtruth.__file__).parent}")
    if args.refine_ring is not None:
        from groundtruth.thermal.solver import mesh
        mesh.REFINE_RING = args.refine_ring
    board = load_board(args.board)
    cfg = SettingsFile.load(args.settings).config
    cfg = replace(cfg, analysis=replace(cfg.analysis, gpu=args.gpu, resolution=args.resolution))
    if args.quality:
        cfg = replace(cfg, thermal=replace(cfg.thermal, quality=args.quality))
    names = [c.name for c in cfg.converters] + [d.name for d in cfg.dc_loads]
    t0 = time.time()
    res = simulate(board, cfg, nets=names)
    out = {"stages": {}, "seconds_simulate": time.time() - t0}
    stages = list(res.converters) + list(getattr(res, "dc_loads", []))
    for c in stages:
        rows = []
        for p in getattr(c, "dc", None) or []:
            rows.append({k: p.get(k) for k in ("name", "current_a", "r_mohm", "loss_w", "sustained_a_mm2",
                                               "peak_a_mm2", "sustained_layer")})
        out["stages"][c.net] = {"error": c.error, "loop_nh": getattr(c, "loop_nh", None), "dc": rows}
        print(f"\n{c.net}: {c.error or ''}")
        for r in rows:
            print(f"   {r['name'][:46]:46s} {r['current_a']:7.3f} A  R {r['r_mohm']:8.3f} mΩ  loss {r['loss_w'] * 1e3:8.2f} mW"
                  f"  sustained {r['sustained_a_mm2']:7.1f}  peak {r['peak_a_mm2']:7.1f} A/mm² ({r['sustained_layer']})")
    combined = getattr(res, "dc_combined", None)
    if combined is not None and getattr(combined, "dc", None):
        p = combined.dc[0]
        out["combined"] = {k: p.get(k) for k in ("loss_w", "sustained_a_mm2", "peak_a_mm2")}
        print(f"\nall stages together: copper loss {p['loss_w'] * 1e3:.2f} mW, sustained {p['sustained_a_mm2']:.1f}, "
              f"peak {p['peak_a_mm2']:.1f} A/mm²")
    t1 = time.time()
    if args.no_thermal:
        if args.json:
            Path(args.json).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        return 0
    th, _ = run_thermal(board, cfg, stages)
    out["thermal"] = {"error": th.error, "t_max": th.t_max, "hotspot": th.hotspot, "layer": th.hotspot_layer,
                      "copper_w": th.copper_w, "copper_w20": th.copper_w20, "parts_w": th.parts_w,
                      "seconds": time.time() - t1, "notes": list(th.notes), "mesh": th.mesh,
                      "parts": [{k: p.get(k) for k in ("ref", "t_c", "power_w")} for p in th.parts[:12]],
                      "dc_at_temperature": list(getattr(th, "dc_hot", []))}
    print(f"\nthermal: {th.error or ''} t_max {th.t_max:.2f} °C on {th.hotspot_layer} at ({th.hotspot[0]:.2f}, "
          f"{th.hotspot[1]:.2f}); copper {th.copper_w * 1e3:.1f} mW (20 °C: {th.copper_w20 * 1e3:.1f}), parts "
          f"{th.parts_w:.3f} W; {time.time() - t1:.0f} s; mesh {th.mesh}")
    print("   hottest parts: " + ", ".join(f"{p['ref']} {p['t_c']:.1f} °C" for p in th.parts[:12]))
    for r in getattr(th, "dc_hot", []):           # the DC paths at the solved temperatures (a tree that has them)
        drop = (f", drop {r['drop20_mv']:.2f} → {r['drop_mv']:.2f} mV" if r["drop_mv"] is not None else "")
        print(f"   at {r['t_mean_c']:5.1f} °C: {r['stage']} {r['path'][:44]:44s} R {r['r20_mohm']:.3f} → "
              f"{r['r_mohm']:.3f} mΩ{drop}, loss {r['loss20_w'] * 1e3:.1f} → {r['loss_w'] * 1e3:.1f} mW")
    for n in th.notes:
        print(f"   note: {n}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
