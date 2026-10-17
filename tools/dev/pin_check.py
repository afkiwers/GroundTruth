"""A part's through-hole pin values (analysis/pins.py) on a real board: Simulate without and with them, and what
changes: the part's heat in Thermal (its pins' joint and contact at the stages' currents), the notes, each power
stage's DC paths (drop, hotspot), and the combined DC map's peak density near the part on each layer (where a partly
soldered pin feeds its current). Reads the board and settings only (the values go into a copy of the settings in
memory).

    .venv/Scripts/python tools/dev/pin_check.py BOARD SETTINGS --part J101 --values "fill:75, contact:0.5m"
        [--only "buck conv1" "buck conv2"] [--resolution 1] [--radius 6]
"""
import argparse
import dataclasses
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def near_peaks(view, centre, radius: float) -> dict:
    """The peak current density (A/mm²) per layer of a DC view within `radius` mm of `centre`."""
    out = {}
    for layer, m in (view.sheet_current or {}).items():
        g = (view.layer_grids or {}).get(layer)
        if g is None or m is None:
            continue
        ii, jj = np.meshgrid(np.arange(m.shape[0]), np.arange(m.shape[1]), indexing="ij")
        x, y = g.xy(ii, jj)
        near = np.hypot(x - centre[0], y - centre[1]) <= radius
        out[layer] = float(np.max(np.where(near, m, 0.0))) if near.any() else 0.0
    return out


def columns(converters, part: str) -> tuple[dict, dict]:
    """({pin: the layer its current enters on, when not its part's side}, {pin: {"F.Cu→In1.Cu": A}}): the current
    up or down each piece of the part's pin columns, summed over the stages' DC paths (their branch currents, read
    while combine_dc still has them: Simulate releases them after it)."""
    feeds, flows = {}, {}
    for c in converters:
        for build, currents in (getattr(c, "dc_branch", None) or {}).values():
            if build is None or currents is None:
                continue
            feeds.update({p: l for p, l in getattr(build, "pin_feed", {}).items() if p.startswith(part + ".")})
            for b, amps in zip(build.plane.branches, currents):
                if not b.name.startswith(f"pad {part}."):
                    continue
                pin = b.name[4:]
                key = f"{build.names[b.a[0]]}→{build.names[b.b[0]]}"
                flows.setdefault(pin, {})
                flows[pin][key] = flows[pin].get(key, 0.0) + float(amps)
    return feeds, flows


def run(board, cfg, only, part, radius):
    """One Simulate: (seconds, the part's thermal loss rows, notes, the stages' DC paths, peaks near the part, the
    part's pin feeds and column currents)."""
    from groundtruth.analysis import power
    from groundtruth.analysis.thermal import part_losses
    original, seen = power.combine_dc, {}

    def combine(converters, *a, **k):
        """power.combine_dc, reading the part's column currents first (Simulate drops the branch currents after)."""
        seen["columns"] = columns(converters, part)
        return original(converters, *a, **k)
    power.combine_dc = combine
    t0 = time.time()
    try:
        res = simulate_(board, cfg, only)
    finally:
        power.combine_dc = original
    seconds = time.time() - t0
    rows, notes = part_losses(board, cfg, res.converters)
    paths = [(c.net, p) for c in res.converters for p in (c.dc or [])]
    fp = board.footprint(part)
    peaks = near_peaks(res.dc_combined, fp.position, radius) if res.dc_combined is not None and fp else {}
    return seconds, [r for r in rows if r["ref"] == part], notes, paths, peaks, seen.get("columns", ({}, {}))


def simulate_(board, cfg, only):
    """simulate() of the stages named (all enabled ones without --only)."""
    from groundtruth.analysis.simulate import simulate
    return simulate(board, cfg, nets=only)


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--part", required=True, help="the part whose pin values are tried, e.g. J101")
    ap.add_argument("--values", required=True, help='its pin values, e.g. "fill:75, contact:0.5m"')
    ap.add_argument("--only", nargs="*", default=None, help="power stages to simulate (default: all enabled)")
    ap.add_argument("--resolution", type=float, default=1.0)
    ap.add_argument("--radius", type=float, default=6.0, help="mm around the part for the peak densities")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.pins import params_for, values_from_data
    from groundtruth.config.settings import SettingsFile
    board = load_board(Path(args.board))
    cfg = SettingsFile.load(Path(args.settings)).config
    cfg = dataclasses.replace(cfg, analysis=dataclasses.replace(cfg.analysis, resolution=args.resolution))
    values, problems = values_from_data(args.values, "--values")
    if problems:
        print("\n".join(problems))
        return 2
    comps = dict(cfg.components)
    comps[args.part] = dataclasses.replace(comps[args.part], tht=values)
    tried = dataclasses.replace(cfg, components=comps)
    print(f"{args.part}: {params_for(board, tried, args.part).describe()}")
    results = {}
    for name, c in (("built in", cfg), ("with values", tried)):
        results[name] = run(board, c, args.only, args.part, args.radius)
        print(f"Simulate ({name}): {results[name][0]:.0f} s")
    for name, (_, rows, notes, paths, peaks, (feeds, flows)) in results.items():
        print(f"\n== {name}")
        for r in rows:
            print(f"  {r['ref']} heat {r['power_w']:.3g} W: {r['what']}")
        if not rows:
            print(f"  {args.part}: no heat")
        for n in notes:
            if args.part in n:
                print(f"  note: {n}")
        for stage, p in paths:
            print(f"  {stage} / {p['name']}: {p['current_a']:.3g} A, {p['r_mohm']:.2f} mΩ, drop {p['drop_mv']:.1f} mV, "
                  f"sustained {p['sustained_a_mm2']:.0f} A/mm² on {p['sustained_layer']}")
        print("  peak density within {:g} mm of {}: ".format(args.radius, args.part)
              + ", ".join(f"{l} {v:.1f}" for l, v in peaks.items()) + " A/mm²")
        for pin in sorted(set(feeds) | set(flows)):
            print(f"  {pin}: fed on {feeds.get(pin, 'its part side')}; column current "
                  + ", ".join(f"{k} {v:+.2f} A" for k, v in flows.get(pin, {}).items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
