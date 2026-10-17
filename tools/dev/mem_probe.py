"""Is memory given back after a failed Simulate? Runs simulate() in a worker thread the way the plugin's jobs do
(MainFrame._start: the exception and its formatted traceback handed to the "GUI", then dropped), makes it fail on
purpose, and prints the process memory before, at its peak and after: the working set (Task Manager's "Memory")
and the private bytes (what the process has committed).

    .venv/Scripts/python tools/dev/mem_probe.py BOARD SETTINGS [--only NET ...] [--fail memory|error|cancel|none]
        [--after 3] [--warm 1] [--gpu off] [--who] [--trace N]

A process's first Simulate grows it for good (imports, Numba's compiled code, FFT plans, the allocators' pools), so
--warm N successful runs come first and the measurement starts after them. --fail memory: the factorisation after
--after of them raises MemoryError, as SuperLU running out does (the scheduler retries the solve alone, then gives
up); error: a RuntimeError there; cancel: the progress callback raises at the --after'th report; none: a
successful run (to compare). --gpu off (default) as in the KiCad plugin's venv, which has no PyTorch. --who: what
still references a surviving solver model; --trace N: the N biggest Python/numpy allocations still alive (slow).
The board and settings are only read (pass a copy's settings, never write next to a real board)."""
from __future__ import annotations

import argparse
import gc
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def memory_mb() -> tuple[float, float]:
    """(working set, private bytes) in MB."""
    import psutil
    m = psutil.Process().memory_info()
    return m.rss / 1e6, getattr(m, "private", m.vms) / 1e6


def _holders(obj, skip: set, depth: int = 8):
    """Print one chain of referrers from obj back towards a root (a module dict, a frame, a thread's dict)."""
    import types
    seen = set(skip)
    cur = obj
    refs: list = []
    for level in range(depth):
        seen.update((id(cur), id(refs)))        # not the object itself, nor this function's own list
        refs = [r for r in gc.get_referrers(cur) if id(r) not in seen]
        if not refs:
            print(f"   {'  ' * level}(no more referrers)")
            return
        r = refs[0]
        label = type(r).__name__
        if isinstance(r, dict):
            mod = next((m.__name__ for m in list(sys.modules.values())
                        if m is not None and getattr(m, "__dict__", None) is r), None)
            keys = [k for k, v in r.items() if v is cur][:3]
            label = f"dict (module {mod})" if mod else f"dict keys {keys}"
        elif isinstance(r, types.FrameType):
            label = f"frame {r.f_code.co_name} ({Path(r.f_code.co_filename).name}:{r.f_lineno})"
        print(f"   {'  ' * level}<- {label}  [{len(refs)} referrer(s)]")
        if isinstance(r, dict) and label.startswith("dict (module"):
            return
        cur = r


def main() -> int:
    """The command line (see the module): warm-up runs, then the measured runs with the injected failure, printing
    the memory before, at the peak and after each."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", nargs="+", help="nets / stages to simulate (default: all in the settings)")
    ap.add_argument("--fail", choices=("memory", "error", "cancel", "none"), default="memory")
    ap.add_argument("--after", type=int, default=3)
    ap.add_argument("--warm", type=int, default=1, help="successful runs before the measured one (default 1)")
    ap.add_argument("--repeat", type=int, default=1, help="measured runs in a row (default 1): growth per run")
    ap.add_argument("--no-release", action="store_true",
                    help="skip ui.progress.release_failure (the plugin frees a failed job's frames with it)")
    ap.add_argument("--gpu", choices=("off", "auto"), default="off")
    ap.add_argument("--threads", help="analysis.threads for the runs (default: the settings')")
    ap.add_argument("--who", action="store_true", help="print what still references a surviving solver model")
    ap.add_argument("--trace", type=int, default=0, metavar="N",
                    help="trace allocations (tracemalloc, which numpy reports its buffers to) and print the N "
                         "biggest still alive after the job, by where they were made (slow)")
    args = ap.parse_args()

    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.components.fields import apply_fields
    from groundtruth.config.settings import SettingsFile
    from groundtruth.solver import peec
    from groundtruth.ui.progress import release_failure

    board = load_board(args.board)
    cfg = SettingsFile.load(args.settings).config
    cfg.analysis.gpu = args.gpu
    if args.threads:
        cfg.analysis.threads = int(args.threads) if args.threads.isdigit() else args.threads
    apply_fields(cfg, board)
    state = {"fail": False, "factorisations": 0, "reports": 0}

    class Cancelled(Exception):
        pass

    real = peec.PlaneStack._factorise

    def factorise(self, f, exact):
        """The real factorisation, failing on purpose (MemoryError or RuntimeError) after --after factorisations
        while a failure is armed."""
        state["factorisations"] += 1
        if state["fail"] and args.fail in ("memory", "error") and state["factorisations"] > args.after:
            if args.fail == "memory":
                raise MemoryError("injected: the sparse factorisation ran out of memory")
            raise RuntimeError("injected failure")
        return real(self, f, exact)
    peec.PlaneStack._factorise = factorise

    def progress(msg, frac):
        """The progress callback; raises Cancelled after --after reports when cancelling is armed."""
        state["reports"] += 1
        if state["fail"] and args.fail == "cancel" and state["reports"] > args.after:
            raise Cancelled("cancelled")

    def job() -> str:                               # as MainFrame._start's job thread
        """One Simulate in a worker thread, as MainFrame._start runs a job: its outcome as text."""
        out = []

        def target():
            """The worker: simulate and drop the factorisations; on failure format the traceback and release the
            failure's frames as the plugin does (unless --no-release)."""
            try:
                res = simulate(board, cfg, args.only, progress=progress)
                res.drop_factorisations()           # as the plugin does after a successful Simulate
                out.append("ok")
                del res
            except Exception as e:                  # noqa: BLE001
                text = traceback.format_exc()
                if not args.no_release:
                    release_failure(e)              # as MainFrame._start does
                out.append(f"{type(e).__name__}: {(str(e) or '-').splitlines()[0][:90]} ({len(text)} chars)")
        th = threading.Thread(target=target)
        th.start()
        th.join()
        return out[0]

    for k in range(args.warm):
        t0 = time.time()
        print(f"warm-up run {k + 1}: {job()} in {time.time() - t0:.0f} s, then {memory_mb()[0]:.0f} MB")
    if args.trace:
        import tracemalloc
        tracemalloc.start(12)
    gc.collect()
    base = memory_mb()
    peak = {"mb": base[0]}
    done = threading.Event()

    def sample():
        """Sample the private memory every 0.2 s until done, keeping the peak."""
        while not done.wait(0.2):
            peak["mb"] = max(peak["mb"], memory_mb()[0])
    threading.Thread(target=sample, daemon=True).start()
    t0 = time.time()
    for k in range(args.repeat):
        state.update(fail=args.fail != "none", factorisations=0, reports=0)
        outcome = job()
        if args.repeat > 1:
            gc.collect()
            print(f"   measured run {k + 1}: private {memory_mb()[1]:.0f} MB")
    done.set()
    after = memory_mb()
    gc.collect()
    collected = memory_mb()
    print(f"measured run: {outcome} in {time.time() - t0:.0f} s")
    print("                 working set   private")
    print(f"   before         {base[0]:8.0f} MB  {base[1]:8.0f} MB")
    print(f"   peak           {peak['mb']:8.0f} MB")
    print(f"   after          {after[0]:8.0f} MB  {after[1]:8.0f} MB   (the job ended, no gc.collect)")
    print(f"   after gc       {collected[0]:8.0f} MB  {collected[1]:8.0f} MB")
    alive = [o for o in gc.get_objects() if isinstance(o, peec.PlaneStack)]
    print(f"solver models (PlaneStack) still alive: {len(alive)}")
    if alive and args.who:
        print("what holds the first one, up to the root (module or thread):")
        _holders(alive[0], skip={id(alive)})
    if args.trace:
        import tracemalloc
        snap = tracemalloc.take_snapshot()
        total = sum(s.size for s in snap.statistics("filename"))
        print(f"traced and still alive since the measured run began: {total / 1e6:.0f} MB")
        for stat in snap.statistics("traceback")[:args.trace]:
            print(f"\n{stat.size / 1e6:8.1f} MB in {stat.count} block(s)")
            for line in stat.traceback.format()[-12:]:
                if "groundtruth" in line or "scipy" in line or "numpy" in line:
                    print("   ", line.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
