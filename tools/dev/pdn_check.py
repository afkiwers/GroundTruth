"""A supply rail's PDN impedance on a real board (analysis/pdn.py), optionally at several node budgets (grid
convergence), without touching the board or its settings: the settings are copied to a scratch file with only this
rail's pdn entry.

    .venv/Scripts/python tools/dev/pdn_check.py BOARD SETTINGS --rail +3V3 [--i-step 1] [--ripple 5%]
        [--source U504 | none] [--vrm-r 5m] [--vrm-l 20n] [--parts U1102,U1103] [--budgets 40000,80000]
        [--json OUT.json]

Prints per budget and IC: |Z| at a few frequencies, the peaks, the inductance above the decaps, the worst margin
to the target and what holds the rail there; then the decaps near each IC and the issues.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.adapters.file_source import load_board          # noqa: E402
from groundtruth.analysis.simulate import simulate              # noqa: E402
from groundtruth.components.values import format_si, parse_value  # noqa: E402
from groundtruth.config.settings import PdnSpec, SettingsFile   # noqa: E402

SHOW_F = (1e5, 1e6, 1e7, 5e7, 1e8, 3e8)


def main(argv=None) -> int:
    """Run the rail at each budget and print it (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--rail", required=True)
    ap.add_argument("--ground", default="")
    ap.add_argument("--i-step", default=None)
    ap.add_argument("--ripple", default="5%")
    ap.add_argument("--voltage", default=None)
    ap.add_argument("--source", default="")
    ap.add_argument("--vrm-r", default="5m")
    ap.add_argument("--vrm-l", default="20n")
    ap.add_argument("--parts", default="")
    ap.add_argument("--budgets", default="", help="cavity_max_nodes values, e.g. 40000,80000 (default: the file's)")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    board = load_board(args.board)
    scratch = Path(tempfile.mkdtemp(prefix="pdn_check_"))
    path = scratch / Path(args.settings).name
    shutil.copy(args.settings, path)
    sc = SettingsFile.load(path)
    ripple = float(args.ripple.rstrip("%")) / 100 if args.ripple.endswith("%") else float(args.ripple)
    spec = PdnSpec(args.rail, ground=args.ground, ripple=ripple,
                   i_step=parse_value(args.i_step) if args.i_step else None,
                   voltage=parse_value(args.voltage) if args.voltage else None, source=args.source,
                   vrm_r=parse_value(args.vrm_r), vrm_l=parse_value(args.vrm_l),
                   parts=[p.strip() for p in args.parts.split(",") if p.strip()])
    sc.set_pdn([spec])
    sc.save()
    out = {}
    budgets = [int(b) for b in args.budgets.split(",") if b.strip()] or [None]
    for budget in budgets:
        cfg = SettingsFile.load(path).config
        if budget:
            cfg.analysis.cavity_max_nodes = budget
        t0 = time.time()
        r = simulate(board, cfg, nets=[spec.label])
        p = r.pdn[0]
        print(f"\n=== budget {budget or cfg.analysis.cavity_max_nodes}: {time.time() - t0:.0f} s")
        if p.error:
            print("error:", p.error)
            continue
        for n in p.notes:
            print("note:", n)
        print(f"{p.rail} / {p.ground}, {p.voltage} V, target "
              + (format_si(p.target_ohm, "Ω") if p.target_ohm else "none") + (f"; source {p.source}" if p.source else ""))
        for port in p.ports:
            zs = []
            for f in SHOW_F:
                k = min(range(len(port["f"])), key=lambda i: abs(port["f"][i] / f - 1))
                zs.append(f"{format_si(port['f'][k], 'Hz')} {format_si(port['z_ohm'][k], 'Ω')}")
            print(f"  {port['part']} ({port['value']}, {len(port['pins'])} pins): " + ", ".join(zs))
            print(f"     peaks {[(format_si(x['f'], 'Hz'), format_si(x['z_ohm'], 'Ω')) for x in port['peaks']]}, "
                  f"L above the decaps {port['l_nh'] and round(port['l_nh'], 2)} nH"
                  + (f", worst {port['worst']}" if port.get("worst") else ""))
        for c in p.decaps:
            if c["near"]:
                print(f"  decap {c['ref']} {c['value']}: " + ", ".join(f"{n['part']} {n['mm']} mm" for n in c["near"]))
        for i in r.issues:
            if i.check in ("pdn_impedance", "decap_missing"):
                print(f"  [{i.severity}] {i.message}")
        out[str(budget)] = p.summary()
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
