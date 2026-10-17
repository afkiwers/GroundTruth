"""Memory across Simulates of different boards in one process (as the plugin, which stays open between boards):
every board in turn, for some rounds, the memory after each run with its result let go. Growth from round to
round would be memory a run keeps.

    .venv/Scripts/python tools/dev/mem_boards.py --rounds 3 [--threads 4] [--only NET] [--only NET2]
        BOARD1 SETTINGS1 BOARD2 SETTINGS2 [...]

--only applies to boards whose settings name it. The boards are only read."""
from __future__ import annotations

import argparse
import gc
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def main() -> int:
    """The command line (see the module): for each round and each board, simulate, let the result go and print the
    process memory."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pairs", nargs="+", help="BOARD SETTINGS BOARD SETTINGS ...")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--threads")
    ap.add_argument("--only", action="append", help="a net or stage to simulate (repeatable)")
    args = ap.parse_args()
    if len(args.pairs) % 2:
        ap.error("give BOARD SETTINGS pairs")
    import psutil
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.components.fields import apply_fields
    from groundtruth.config.settings import SettingsFile
    proc = psutil.Process()
    jobs = []
    for b, s in zip(args.pairs[::2], args.pairs[1::2]):
        board = load_board(b)
        cfg = SettingsFile.load(s).config
        cfg.analysis.gpu = "off"
        if args.threads:
            cfg.analysis.threads = int(args.threads) if args.threads.isdigit() else args.threads
        apply_fields(cfg, board)
        names = {n.name for n in cfg.nets} | {c.name for c in cfg.converters} | {x.name for x in cfg.bridges}
        only = [n for n in (args.only or []) if n in names] or None
        jobs.append((Path(b).stem, board, cfg, only))
    m = proc.memory_info()
    print(f"start: private {m.private / 1e6:.0f} MB, working set {m.rss / 1e6:.0f} MB")
    for r in range(args.rounds):
        for name, board, cfg, only in jobs:
            box = {}
            t0 = time.perf_counter()
            th = threading.Thread(target=lambda: box.update(res=simulate(board, cfg, only)))
            th.start()
            th.join()
            box.clear()
            gc.collect()
            time.sleep(0.5)
            m = proc.memory_info()
            print(f"round {r + 1} {name:24s} {time.perf_counter() - t0:6.0f} s   after: private "
                  f"{m.private / 1e6:6.0f} MB, working set {m.rss / 1e6:5.0f} MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
