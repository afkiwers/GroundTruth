"""Compare grid-convergence runs (tools/dev/cavity_converge.py --json) between resolutions (development aid,
docs/NEXT_STEPS.md): per pair of neighbouring resolutions, the rms and largest difference of the loop moment |m| in
dB over the harmonics both solved, by band; and the |m| table side by side.

    .venv/Scripts/python tools/dev/converge_compare.py out/converge/spi_sck_widths.json out/converge/spi_sck_widths_4x.json
    .venv/Scripts/python tools/dev/converge_compare.py --bands 0.5e9,1e9,1.5e9 A.json B.json C.json --table

Every file may hold several resolutions; a resolution given twice is taken from the last file.
"""
from __future__ import annotations

import argparse
import json
import math
import sys


def load(paths: list[str]) -> dict[float, dict[int, float]]:
    """{resolution: {f Hz: |m| mm²}} from cavity_converge JSON files."""
    out: dict[float, dict[int, float]] = {}
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for r in json.load(fh):
                out[float(r["resolution"])] = {int(f): s["m_mm2"] for f, s in r["solves"].items() if s.get("m_mm2")}
    return out


def spread(a: dict[int, float], b: dict[int, float], lo: float, hi: float) -> tuple[float, float, int]:
    """(rms dB, largest dB, count) of 20·log10(|m_a| / |m_b|) over the frequencies both have in [lo, hi)."""
    d = [20 * math.log10(a[f] / b[f]) for f in a if f in b and lo <= f < hi]
    if not d:
        return math.nan, math.nan, 0
    return math.sqrt(sum(x * x for x in d) / len(d)), max(abs(x) for x in d), len(d)


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--bands", default="0.5e9,1e9,1.5e9", help="band edges, Hz, comma separated")
    ap.add_argument("--table", action="store_true", help="also print |m| per frequency and resolution")
    args = ap.parse_args()
    runs = load(args.files)
    res = sorted(runs)
    edges = [float(x) for x in args.bands.split(",")]
    for a, b in zip(res, res[1:]):
        for lo, hi in zip(edges, edges[1:]):
            rms, mx, n = spread(runs[a], runs[b], lo, hi)
            print(f"{a:g}x vs {b:g}x, {lo / 1e9:g}-{hi / 1e9:g} GHz: rms {rms:5.2f} dB, max {mx:5.2f} dB ({n})")
    if args.table:
        fs = sorted(set().union(*runs.values()))
        print("   MHz " + "".join(f"{r:>9g}x" for r in res))
        for f in fs:
            print(f"{f / 1e6:6.0f} " + "".join(f"{runs[r].get(f, math.nan):10.1f}" for r in res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
