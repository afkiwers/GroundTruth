"""The fix advisor's options for a board's power stages (development aid): Simulate, then every stage's design
changes with their predicted figures, parts and notes.

    .venv/Scripts/python tools/dev/fix_check.py BOARD SETTINGS [--only SW1] [--test-buck]

--test-buck: the synthetic discrete buck of tests/boardgen.py at 48 V, 5 A, 750 kHz, 2 ns, C_oss 600 pF instead.
"""
import argparse
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]


def show(res):
    """Print every stage's design-change rows: kind, radiated and conducted margins, overshoot, extra loss, what it
    clears, its title, parts and notes."""
    for c in res.converters:
        print(f"\n== {c.net}: {len(c.fixes)} rows")
        for o in c.fixes:
            oc = o["outcome"] or {}

            def db(k):
                """An outcome figure in dB for the row, a dash when missing."""
                v = oc.get(k)
                return "  –  " if v is None else f"{v:+5.1f}"
            ov = oc.get("overshoot_v")
            print(f"{o['kind']:9s} rad {db('radiated_db')} cond {db('conducted_db')} "
                  f"ov {'–' if ov is None else f'{ov:5.1f}'} loss {oc.get('loss_w') or 0:5.2f} W "
                  f"clears {','.join(o['clears']) or '-':30s} {o['title']}")
            for p in o["parts"]:
                print(f"{'':12s}part: {p}")
            for n in o["notes"]:
                print(f"{'':12s}note: {n}")


def main() -> int:
    """The command line: Simulate the board's power stages (or --test-buck: a generated discrete buck at 48 V to 12
    V, 5 A, 750 kHz) and show the fix advisor's rows."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", nargs="?")
    ap.add_argument("settings", nargs="?")
    ap.add_argument("--only", help="part of a stage name")
    ap.add_argument("--test-buck", action="store_true")
    args = ap.parse_args()
    from groundtruth.analysis.simulate import simulate
    if args.test_buck:
        from boardgen import discrete_buck_board
        from conftest import make
        from groundtruth.analysis.detect import detect_converters
        board, sc, _ = make(Path(tempfile.mkdtemp()), discrete_buck_board(), "fix", nets=())
        spec = detect_converters(board, sc.config)[0].spec
        sc.set_converters([replace(spec, f_sw=750e3, i_out=5.0, v_in=48.0, v_out=12.0, rise_time=2e-9,
                                   c_oss=600e-12)])
        cfg = sc.config
    else:
        from groundtruth.adapters.file_source import load_board
        from groundtruth.components.fields import apply_fields
        from groundtruth.config.settings import SettingsFile
        board = load_board(args.board)
        cfg = SettingsFile.load(Path(args.settings)).config
        apply_fields(cfg, board)
        cfg = replace(cfg, nets=[], bridges=[],
                      converters=[c for c in cfg.converters if not args.only or args.only in c.name])
    t = time.time()
    res = simulate(board, cfg)
    print(f"simulated in {time.time() - t:.0f} s")
    show(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
