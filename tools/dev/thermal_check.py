"""Thermal solve of a board's power stages from the command line (development aid; analysis/thermal.py).

    .venv/Scripts/python tools/dev/thermal_check.py BOARD SETTINGS [--only SW1,SW2] [--rds-on 4m] [--dcr 5m]
        [--quality draft|normal|fine] [--ambient 25] [--png DIR] [--capacity [--limit 105]] [--cache FILE]
        [--theta Q301=2,Q303=2] [--linear]

Simulates the chosen power stages (their DC paths drive the thermal model), then solves the temperatures. The
loss parameters given here override the settings file for this run only (nothing is written). --png saves the
hottest layer's temperature map. --capacity also searches each stage's current capacity (to --limit °C, default
the settings' max_c) and checks it with a fresh solve at that current. --cache FILE keeps Simulate's power-stage
results between runs (pickled; delete the file after changing the board, the stages or the loss parameters).
--linear makes the problem linear for checks (copper α = 0, fixed h_top / h_bottom, no radiation)."""
import argparse
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module): simulate the power stages (or load --cache), set the test loss values, run
    the thermal solve (and with --capacity the current capacity) and print the results."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", help="comma-separated parts of power stage names to keep")
    ap.add_argument("--rds-on")
    ap.add_argument("--dcr")
    ap.add_argument("--quality", choices=["draft", "normal", "fine"])
    ap.add_argument("--ambient", type=float)
    ap.add_argument("--png")
    ap.add_argument("--capacity", action="store_true")
    ap.add_argument("--limit", type=float)
    ap.add_argument("--cache")
    ap.add_argument("--theta", help="theta_jb per part, e.g. Q301=2,Q303=2 (K/W)")
    ap.add_argument("--linear", action="store_true")
    args = ap.parse_args()
    from dataclasses import replace
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.thermal import run_thermal
    from groundtruth.components.values import parse_value
    from groundtruth.config.settings import SettingsFile
    board = load_board(args.board)
    cfg = SettingsFile.load(Path(args.settings)).config
    keep = [c for c in cfg.converters if not args.only or any(k in c.name for k in args.only.split(","))]
    over = {}
    if args.rds_on:
        over["rds_on"] = parse_value(args.rds_on)
    if args.dcr:
        over["dcr"] = parse_value(args.dcr)
    cfg = replace(cfg, nets=[], converters=[replace(c, **over) for c in keep])
    th_over = {}
    if args.quality:
        th_over["quality"] = args.quality
    if args.ambient is not None:
        th_over["ambient_c"] = args.ambient
    if args.limit is not None:
        th_over["capacity_c"] = args.limit
    if args.theta:
        th_over["theta_jb"] = {k: float(v) for k, v in (kv.split("=") for kv in args.theta.split(","))}
    if args.linear:
        th_over.update(convection="fixed", radiation=False)
        from groundtruth.thermal import materials
        materials.COPPER = replace(materials.COPPER, alpha=0.0)
    if th_over:
        cfg = replace(cfg, thermal=replace(cfg.thermal, **th_over))
    print(f"{len(keep)} power stage(s): {', '.join(c.name for c in keep)}; " + ", ".join(f"{k} {v:g}" for k, v in over.items()))
    t = time.time()
    cache = Path(args.cache) if args.cache else None
    if cache and cache.exists():
        stages = pickle.loads(cache.read_bytes())
        print(f"power-stage results from {cache}")
    else:
        stages = simulate(board, cfg).converters
        print(f"simulate {time.time() - t:.0f} s")
        if cache:
            cache.write_bytes(pickle.dumps(stages, protocol=pickle.HIGHEST_PROTOCOL))
    if args.capacity:
        return capacity(board, cfg, stages)
    t = time.time()
    th, issues = run_thermal(board, cfg, stages, progress=lambda m, f: None)
    if th.error:
        print("error:", th.error)
        return 1
    print(f"thermal {time.time() - t:.0f} s ({cfg.thermal.quality}; mesh {th.mesh})")
    print(f"T_max {th.t_max:.1f} °C on {th.hotspot_layer} at ({th.hotspot[0]:.2f}, {th.hotspot[1]:.2f}); "
          f"ambient {th.ambient_c:g} °C")
    print(f"heat: copper {th.copper_w:.2f} W (at 20 °C {th.copper_w20:.2f} W), parts {th.parts_w:.2f} W, "
          f"total {th.power_w:.2f} W, out {th.heat_out['total']:.2f} W")
    for l in th.losses[:10]:
        print(f"  loss {l['ref']:8s} {l['power_w']:7.3f} W  {l['what']}")
    for p in th.parts[:10]:
        print(f"  part {p['ref']:8s} {p['t_c']:6.1f} °C  {p['power_w']:.3f} W")
    for n in th.notes:
        print("  note:", n)
    for i in issues:
        print(f"  {i.severity:8s} {i.check:18s} {i.message}")
    if args.png:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from groundtruth.report.plots import draw_heatmap
        out = Path(args.png)
        out.mkdir(parents=True, exist_ok=True)
        fig = Figure(figsize=(12, 8), dpi=100)
        FigureCanvasAgg(fig)
        draw_heatmap(fig, board, th, layer=th.hotspot_layer, vectors=False)
        fig.savefig(out / "thermal_hottest_layer.png")
        print("saved", out / "thermal_hottest_layer.png")
    return 0


def capacity(board, cfg, stages) -> int:
    """The capacity search, then a check: a fresh thermal solve with each stage at its capacity current."""
    from dataclasses import replace
    from groundtruth.analysis.thermal import _scaled, capacity_text, run_capacity, run_thermal
    t = time.time()
    views = run_capacity(board, cfg, stages, progress=lambda m, f: print(f"  {m}") if m.startswith("capacity") else None)
    print(f"capacity {time.time() - t:.0f} s")
    for v in views:
        if v.error:
            print("error:", v.error)
            continue
        c = v.capacity
        of = (", ".join(f"{n} {i:.3g} A" for n, i in c["currents"].items()) if c.get("currents") is not None
              else f"of {c['i_nominal']:g} A")
        print(f"{c['stage']}: {capacity_text(c)} (scale {c['scale']:.3f}; {of}), limited by "
              f"{c['limited_by']} at {c['t_hottest']:.2f} °C; {c['solves']} solves; bound {c['bound'] or '-'}")
        for n in v.notes:
            print("  note:", n)
        if c["bound"]:
            continue
        names = set(c["currents"]) if c.get("currents") is not None else {c["stage"]}
        cfg2, stages2 = _scaled(cfg, stages, names, c["scale"])
        # the check scales the stages' DC current maps too (_scaled keeps only what part_losses reads)
        full = [replace(s, dc_vectors={k: {L: (jx * c["scale"], jy * c["scale"]) for L, (jx, jy) in v_.items()}
                                       for k, v_ in s.dc_vectors.items()},
                        dc=[dict(p, current_a=p["current_a"] * c["scale"]) for p in s.dc])
                if s.net in names else s for s in stages]
        th, _ = run_thermal(board, cfg2, full)
        tj = max([p["t_junction"] or 0 for p in th.parts] + [th.t_max])
        print(f"  check: a fresh solve at that load gives {tj:.2f} °C (limit {c['limit_c']:g} °C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
