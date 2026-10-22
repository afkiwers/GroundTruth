"""Every capacitor's DC voltage and capacitance in circuit for a board (analysis/bias.at_dc), as JSON: run it with
two trees' sources to compare them (audit round 7: main's per-net bounds against the DC operating point).

    .venv/Scripts/python tools/dev/cap_bias_dump.py BOARD SETTINGS OUT.json
    PYTHONPATH=<worktree>/src .venv/Scripts/python tools/dev/cap_bias_dump.py BOARD SETTINGS OLD.json

Read only: the settings are merged with the board in memory, never saved."""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    """Write {ref: {v, why, c0, c, how}} and print the capacitors whose voltage moved by more than 0.5 V when a
    previous dump is given as a fourth argument."""
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.bias import at_dc
    from groundtruth.config.settings import SettingsFile
    board = load_board(argv[0])
    sc = SettingsFile.load(argv[1])
    sc.merge_board(board)
    _, rows, notes = at_dc(board, sc.config)
    out = {r.ref: {"v": r.volts, "why": r.why, "c0": r.c0, "c": r.c, "how": r.how, "nets": list(r.nets)}
           for r in rows}
    with open(argv[2], "w", encoding="utf-8") as fh:
        json.dump({"caps": out, "notes": notes}, fh, indent=1)
    if len(argv) > 3:
        old = json.load(open(argv[3], encoding="utf-8"))["caps"]
        for ref in sorted(out, key=lambda r: (r[0], len(r), r)):
            a, b = old.get(ref), out[ref]
            if a and abs(a["v"] - b["v"]) > 0.5:
                print(f"{ref:6} {' – '.join(n.rsplit('/', 1)[-1] for n in b['nets']):40} {a['v']:6.3g} V → "
                      f"{b['v']:6.3g} V   {a['c'] * 1e9:9.4g} → {b['c'] * 1e9:9.4g} nF   {b['why'][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
