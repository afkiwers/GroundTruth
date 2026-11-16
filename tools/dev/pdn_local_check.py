"""A PDN port's inductance above its decaps, piece by piece, at several node budgets: the board-wide model's, each
pair of regional models' (analysis/pdn.py _local_runs: the loop's and the pins', fine and coarse) and the correction
they add up to [A118]. A grid-convergence check of the per-port correction, with no full sweep (only a few
frequencies are solved).

    .venv/Scripts/python tools/dev/pdn_local_check.py BOARD SETTINGS --rail NET [--ports J2.2/J2.1] [--parts U5]
        [--budgets 20000,40000,80000] [--freqs 30e6,100e6,300e6] [--source none] [--json OUT.json]
        [--board-wide-only] [--fine-nodes N]
        [--no-probe] [--local-cap on|off] [--margin MM] [--near MM]      # experiments: the settings before 2026-11-15

Prints per budget and port: L = Im(Z)/ω of the board-wide model at each frequency, the decaps in its loop model
(their share of its decap current), each regional pair's fine and coarse L, the correction and the corrected
board-wide value. --board-wide-only: the board-wide model alone (a reference on a fine grid). measure() takes a
config factory, for a script's own boards (tests/boardgen with part models). It also runs on a tree from before
2026-11-15 (one pair per port: copy it into a worktree of that tree). The board and its settings are only read.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.adapters.file_source import load_board          # noqa: E402
from groundtruth.analysis import pdn                            # noqa: E402
from groundtruth.analysis.simulate import BoardModel            # noqa: E402
from groundtruth.config.settings import PdnSpec, SettingsFile   # noqa: E402


def _l_nh(z: complex, f: float) -> float:
    """Im(Z)/ω in nH."""
    return z.imag / (2 * math.pi * f) * 1e9


def measure(board, config, spec: PdnSpec, budgets: list, freqs: list, regional: bool = True) -> dict:
    """Per budget: the rail's board-wide model and each port's regional models solved at `freqs` / LOCAL_F, printed
    and returned ({budget: {nodes, pitch, ports: {part: {board_wide, fine, coarse, correction, decaps, share}}}}).
    config(budget) gives the config to use (a script can build its own: tests/boardgen boards with part models);
    regional False: the board-wide model alone (a reference on a fine board-wide grid)."""
    out = {}
    for budget in budgets:
        cfg = config(budget)
        model = BoardModel(board, cfg)
        t0 = time.time()
        setups, results, _ = pdn.prepare_pdn(model, [spec])
        if not setups:
            print(f"=== budget {budget}: {results[0].error}")
            continue
        s = setups[0]
        print(f"\n=== budget {budget}: {s.run.build.nodes} nodes at {s.run.build.d:.3f} mm "
              f"(set up in {time.time() - t0:.0f} s)")
        for n in s.res.notes:
            print("  note:", n)
        t0 = time.time()
        bw = {f: s.run.exc.solve(f) for f in freqs}
        print(f"  board-wide solves: {time.time() - t0:.0f} s")
        row = {"nodes": s.run.build.nodes, "pitch": s.run.build.d, "ports": {}}
        for p, meta in enumerate(s.ports):
            l_bw = {f: _l_nh(bw[f].Z[p], f) for f in freqs}
            rec = {"board_wide": l_bw}
            print(f"  {meta['part']}: board-wide L " + ", ".join(f"{f / 1e6:g} MHz {v:.3f}" for f, v in l_bw.items())
                  + " nH")
            if regional and meta.get("local"):
                print(f"    decaps in its loop model: {', '.join(meta.get('local_refs') or [])}"
                      + (f" ({meta['local_share']:.0%} of its decap current at {pdn.LOCAL_PROBE_F / 1e6:g} MHz)"
                         if meta.get("local_share") is not None else "")
                      + (f"; pins model with {', '.join(meta['local_pins'])}" if meta.get("local_pins") else ""))
                rec.update(decaps=meta.get("local_refs"), share=meta.get("local_share"), pins=meta.get("local_pins"),
                           pairs=[])
                total = {f: 0.0 for f in pdn.LOCAL_F}
                pairs = meta["local"]
                if hasattr(pairs[0], "exc"):        # a tree before 2026-11-15: one (fine, coarse) pair
                    pairs = [pairs]
                for name, (fine, coarse) in zip(("loop", "pins"), pairs):
                    t0 = time.time()
                    lf = {f: _l_nh(fine.exc.solve(f).Z[0], f) for f in pdn.LOCAL_F}
                    lc = {f: _l_nh(coarse.exc.solve(f).Z[0], f) for f in pdn.LOCAL_F}
                    print(f"    {name}: fine {fine.build.nodes} nodes at {fine.build.d:.3f} mm, coarse "
                          f"{coarse.build.nodes} at {coarse.build.d:.3f} mm ({time.time() - t0:.0f} s)")
                    for f in pdn.LOCAL_F:
                        print(f"      {f / 1e6:g} MHz: fine {lf[f]:.3f}, coarse {lc[f]:.3f}, difference "
                              f"{lf[f] - lc[f]:+.3f} nH")
                        total[f] += lf[f] - lc[f]
                    rec["pairs"].append({"name": name, "fine": lf, "coarse": lc, "fine_mm": fine.build.d,
                                         "coarse_mm": coarse.build.d})
                dl = sorted(total.values())[len(total) // 2]
                rec["correction"] = dl
                print(f"    correction {dl:+.3f} nH; corrected board-wide L "
                      + ", ".join(f"{f / 1e6:g} MHz {v + dl:.3f}" for f, v in l_bw.items()) + " nH")
            row["ports"][meta["part"]] = rec
        out[str(budget)] = row
    return out


def main(argv=None) -> int:
    """Solve the rail's board-wide model and its ports' regional models at a few frequencies per budget (see the
    module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--rail", required=True)
    ap.add_argument("--ground", default="")
    ap.add_argument("--ports", default="", help="pin pairs supply/return, comma-separated: J2.2/J2.1")
    ap.add_argument("--parts", default="")
    ap.add_argument("--source", default="none")
    ap.add_argument("--budgets", default="20000,40000,80000")
    ap.add_argument("--freqs", default="", help="board-wide frequencies (Hz), default the regional models' LOCAL_F")
    ap.add_argument("--local-cap", choices=("on", "off", "default"), default="default",
                    help="the regional models with or without the planes' capacitance (pdn.LOCAL_CAPACITANCE)")
    ap.add_argument("--margin", type=float, default=None, help="pdn.LOCAL_MARGIN_MM (mm)")
    ap.add_argument("--near", type=float, default=None, help="pdn.LOCAL_NEAR_MM (mm)")
    ap.add_argument("--no-probe", action="store_true",
                    help="no probe solve: only the decaps within --near mm in each port's region (as before 2026-11-15)")
    ap.add_argument("--board-wide-only", action="store_true", help="no regional solves (a fine reference)")
    ap.add_argument("--fine-nodes", type=int, default=0, help="analysis.max_nodes: the regional fine model's budget")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    if args.no_probe:
        pdn.probe_shares = lambda *a, **k: (None, [])
    if args.local_cap != "default":
        pdn.LOCAL_CAPACITANCE = args.local_cap == "on"
    if args.margin is not None:
        pdn.LOCAL_MARGIN_MM = args.margin
    if args.near is not None:
        pdn.LOCAL_NEAR_MM = args.near
    print(f"regional models: capacitance {getattr(pdn, 'LOCAL_CAPACITANCE', True)}, margin {pdn.LOCAL_MARGIN_MM} mm, decaps within "
          f"{pdn.LOCAL_NEAR_MM} mm")
    board = load_board(args.board)
    spec = PdnSpec(args.rail, ground=args.ground, source=args.source,
                   parts=[p.strip() for p in args.parts.split(",") if p.strip()],
                   ports=[p.strip() for p in args.ports.split(",") if p.strip()])
    freqs = [float(f) for f in args.freqs.split(",") if f.strip()] or list(pdn.LOCAL_F)

    def config(budget):
        """The settings' config with this board-wide budget (and the regional one, --fine-nodes)."""
        cfg = SettingsFile.load(Path(args.settings)).config
        cfg.analysis.cavity_max_nodes = budget
        if args.fine_nodes:
            cfg.analysis.max_nodes = args.fine_nodes
        return cfg
    out = measure(board, config, spec, [int(b) for b in args.budgets.split(",") if b.strip()], freqs,
                  regional=not args.board_wide_only)
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
