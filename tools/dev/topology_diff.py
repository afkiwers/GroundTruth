"""Do two versions of the code connect a board's copper alike? For the whole-board DC model (power.dc_board_build),
groups every pad and via by the copper piece it sits in (strips and vias only, no parts) and prints the pads one
version joins and the other does not (VALIDATION_AUDIT.md round 5: the piece-aware grid, ISSUE-15).

    .venv/Scripts/python tools/dev/topology_diff.py BOARD SETTINGS --src DIR [--json OUT]

`--src` is the other source tree (a `git worktree` of main); this tree's `src` is the first. Each version runs in a
process of its own. A pair joined by one only is either a real gap the other bridged (the copper does not touch:
the script measures the gap on the copper itself) or a connection the other lost."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np, scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from groundtruth.adapters.file_source import load_board
from groundtruth.config.settings import SettingsFile
from groundtruth.analysis.simulate import BoardModel
from groundtruth.analysis.power import dc_board_build
board = load_board(sys.argv[2])
cfg = SettingsFile.load(sys.argv[3]).config
st = dc_board_build(BoardModel(board, cfg))
s = st.plane
keep = np.zeros(s.nb, bool)
keep[: s.ns] = True
off = s.ns + s.ncap
for k, b in enumerate(s.branches):
    if b.kind in ("via", "track"):
        keep[off + k] = True
g = sp.coo_matrix((np.ones(keep.sum()), (s.a_nodes[keep], s.b_nodes[keep])), shape=(s.n_nodes, s.n_nodes))
_, comp = connected_components(g, directed=False)
out = {}
for fp in board.footprints:
    for p in fp.pads:
        if not p.net:
            continue
        for l in p.layers:
            if l in st.names:
                n = st.node(l, p.position[0], p.position[1], p.net, 0.5)
                if n and s.nid[n] >= 0:
                    out[f"{p.pin}@{l}"] = [p.net, int(comp[s.nid[n]])]
print(json.dumps(out))
'''


def groups(src: str, board: str, settings: str) -> dict:
    """{pad@layer: (net, copper component)} from the source tree `src`, in a process of its own."""
    r = subprocess.run([sys.executable, "-c", PROBE, src, board, settings], capture_output=True, text=True,
                       encoding="utf-8", env={**__import__("os").environ, "PYTHONPATH": src})
    if r.returncode != 0:
        raise SystemExit(r.stderr[-2000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def main() -> int:
    """Compare the two versions' pad connectivity and print the differences."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--src", required=True)
    ap.add_argument("--json")
    args = ap.parse_args()
    a = groups(str(ROOT / "src"), args.board, args.settings)
    b = groups(args.src, args.board, args.settings)
    common = sorted(set(a) & set(b))
    by_net: dict[str, list] = {}
    for key in common:
        by_net.setdefault(a[key][0], []).append(key)
    diffs = []
    for net, keys in sorted(by_net.items()):
        for i, k1 in enumerate(keys):
            for k2 in keys[i + 1:]:
                ja, jb = a[k1][1] == a[k2][1], b[k1][1] == b[k2][1]
                if ja != jb:
                    diffs.append({"net": net, "a": k1, "b": k2, "joined_here": ja, "joined_there": jb})
    print(f"{len(common)} pads compared, {len(diffs)} pairs joined differently")
    for d in diffs[:200]:
        print(f"  {d['net']}: {d['a']} – {d['b']}: this tree {'joined' if d['joined_here'] else 'apart'}, "
              f"the other {'joined' if d['joined_there'] else 'apart'}")
    if args.json:
        Path(args.json).write_text(json.dumps(diffs, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
