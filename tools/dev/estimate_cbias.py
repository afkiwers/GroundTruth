"""Estimated DC-bias curves (components/bias.py text) for a board's class II ceramic capacitors, from each part's
Rated Voltage and Dielectric fields: C/C0 = 1 / (1 + (V/V1)^2) [A56] with V1 a share of the rated voltage per
dielectric (X7R-class 0.4: the user's datasheet chart of a 100 nF 100 V X7R 0603 fits V1 = 40 V; X5R 0.3; Y5V / Z5U
0.15), as percent points at 10, 25, 50, 75 and 100 % of the rating. An ESTIMATE until the maker's curve is entered
(the user, 2026-10-22: "for now use an estimated curve"); package and capacitance density are not taken into account.

    .venv/Scripts/python tools/dev/estimate_cbias.py BOARD.kicad_pcb [--args]   # the table; --args: REF=curve words
                                                                                # for label_project.py --field
"""
from __future__ import annotations

import sys

from groundtruth.adapters.file_source import load_board
from groundtruth.analysis.bias import is_class_one
from groundtruth.analysis.safety import cap_kind, cap_rating
from groundtruth.components.defaults import ref_prefix
from groundtruth.components.fields import field_text

SHARE = {"X7R": 0.4, "X7S": 0.4, "X6S": 0.4, "X7T": 0.4, "X8R": 0.4, "X5R": 0.3, "Y5V": 0.15, "Z5U": 0.15}
AT = (0.10, 0.25, 0.50, 0.75, 1.00)


def estimate(rated: float, dielectric: str) -> str:
    """The estimated curve's text for a part of this rating and dielectric (X7R when it is not known)."""
    v1 = SHARE.get(dielectric.upper(), 0.4) * rated
    pts = []
    for a in AT:
        v = a * rated
        pts.append(f"{100 * (1 / (1 + (v / v1) ** 2) - 1):.0f}%@{v:.3g}V")
    return ", ".join(pts)


def main(argv: list[str]) -> int:
    """Print the table (or the label_project.py arguments)."""
    board = load_board(argv[0])
    rows = []
    for fp in sorted(board.footprints, key=lambda f: f.reference):
        if ref_prefix(fp.reference) != "C" or fp.dnp or is_class_one(fp) or cap_kind(fp) != "ceramic":
            continue
        rated, _ = cap_rating(fp)
        diel = (field_text(fp, "Dielectric") or "").strip()
        if not rated:
            rows.append((fp.reference, fp.value, diel, None, "no Rated Voltage: skipped"))
            continue
        rows.append((fp.reference, fp.value, diel, rated, estimate(rated, diel)))
    if "--args" in argv:
        print(" ".join(f'"{r}={c}"' for r, _, _, v, c in rows if v))
        return 0
    for r in rows:
        print(f"{r[0]:6} {r[1]:10} {r[2] or '?':5} {('%gV' % r[3]) if r[3] else '-':6} {r[4]}")
    print(f"{sum(1 for r in rows if r[3])} capacitors, {sum(1 for r in rows if not r[3])} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
