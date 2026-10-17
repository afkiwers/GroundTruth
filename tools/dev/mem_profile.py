"""Where does a Simulate's memory go? Runs simulate() the way the plugin does (in a worker thread, the factorisations
dropped after it), samples the process memory every 50 ms, and reports the peak per phase, a census of the solver's
live objects near the peak, the memory after the run and after the result is let go, and the run time.

    .venv/Scripts/python tools/dev/mem_profile.py BOARD SETTINGS [--only NET ...] [--repeat 2] [--threads 4]
        [--resolution 1] [--json OUT.json] [--trace] [--label NAME] [--save-result R.json] [--available MB]

Census (taken from the progress callback when private memory reaches a new high, +3 %; arrays shared between
models counted once): the plane models (PlaneStack: kernel spectra, the compiled kernels' copy, sparse incidence
matrices, masks and index arrays), the SuperLU factorisations alive (fill × 20 bytes, an estimate: values plus row
indices) and the stored solutions. --trace also tracks Python/numpy allocations (tracemalloc; slower): their peak.
--save-result writes the simulate JSON (report/formats.to_json) for tools/dev/result_diff.py. --available pretends
the machine has that much memory free (core/sysmem.available_bytes): models solved in groups when their
factorisations would not fit (simulate.solve_groups); pass --threads N too, or "auto" threads shrink with it.
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

MB = 1e6


def memory() -> tuple[float, float]:
    """(private bytes, working set) in MB."""
    import psutil
    m = psutil.Process().memory_info()
    return getattr(m, "private", m.vms) / MB, m.rss / MB


def _nbytes(o) -> int:
    """Bytes an object holds: a numpy array's buffer, or a sparse matrix's index and data arrays."""
    import numpy as np
    import scipy.sparse as sp
    if isinstance(o, np.ndarray):
        return o.nbytes                                     # (add() hands it the buffer that owns the memory)
    if sp.issparse(o):
        return sum(getattr(o, a).nbytes for a in ("data", "indices", "indptr", "row", "col", "offsets")
                   if isinstance(getattr(o, a, None), np.ndarray))
    return 0


def census() -> dict:
    """MB per kind of live solver object (arrays shared between objects counted once)."""
    import numpy as np
    from groundtruth.solver import peec
    seen: set[int] = set()

    def add(o) -> int:
        """Count an object's bytes once: a view counts as the array that owns its memory."""
        while isinstance(o, np.ndarray) and isinstance(o.base, np.ndarray):
            o = o.base                                      # a view: count the array that owns the memory
        if id(o) in seen:
            return 0
        seen.add(id(o))
        return _nbytes(o)
    out = {"models": 0, "kernels": 0, "kernels_compiled_copy": 0, "model_sparse": 0, "model_arrays": 0,
           "factorisations": 0, "lu_fill_mb": 0.0, "solutions": 0, "solution_mb": 0.0}
    objs = gc.get_objects()
    for o in objs:
        if isinstance(o, peec.PlaneStack):
            out["models"] += 1
            d = o.__dict__
            if "_kq" in d:                                  # one stored copy (quadrants, distinct layer pairs)
                for a in d["_kq"].values():
                    out["kernels"] += add(a)
            else:                                           # before: the per-pair dicts and the compiled copy
                for k in ("Kx", "Ky"):
                    for a in d.get(k, {}).values():
                        out["kernels"] += add(a)
                for a in d.get("_kp", {}).values():
                    out["kernels_compiled_copy"] += add(a)
            for name, v in o.__dict__.items():
                if name in ("Kx", "Ky", "_kp", "_kq", "_ops", "layers"):
                    continue
                import scipy.sparse as sp
                if sp.issparse(v):
                    out["model_sparse"] += add(v)
                elif isinstance(v, np.ndarray):
                    out["model_arrays"] += add(v)
                elif isinstance(v, dict):
                    for w in v.values():
                        if isinstance(w, tuple):
                            for x in w:
                                out["model_arrays"] += add(x) if isinstance(x, np.ndarray) else 0
            for layer in o.layers:
                g = layer.grid
                for name, v in vars(g).items():
                    if isinstance(v, np.ndarray):
                        out["model_arrays"] += add(v)
        elif isinstance(o, peec.HostedLU):
            lu = o._lu
            if lu is not None:
                out["factorisations"] += 1
                out["lu_fill_mb"] += lu.nnz * 20 / MB
        elif isinstance(o, peec.Solution):
            out["solutions"] += 1
            out["solution_mb"] += sum(add(v) for v in vars(o).values() if isinstance(v, np.ndarray)) / MB
    for k in ("kernels", "kernels_compiled_copy", "model_sparse", "model_arrays"):
        out[k] = round(out[k] / MB, 1)
    out["lu_fill_mb"] = round(out["lu_fill_mb"], 1)
    out["solution_mb"] = round(out["solution_mb"], 1)
    return out


def phase_of(msg: str) -> str:
    """Which phase a progress message belongs to: building models, solving, finishing, other."""
    m = msg.lower()
    if "building" in m or "models" in m and "solve" not in m:
        return "build"
    if " mhz" in m or "solve " in m:
        return "solve"
    if "ideal reference" in m or "emission" in m or "finishing" in m:
        return "finish"
    return "other"


def main() -> int:
    """The command line (see the module): simulate --repeat times in a worker thread, sampling memory and taking a
    census at each new peak, then print (and append to --json) the figures per run."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", nargs="+")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--threads")
    ap.add_argument("--resolution", type=float)
    ap.add_argument("--gpu", default="off")
    ap.add_argument("--json", help="append this run's figures to a JSON list file")
    ap.add_argument("--label", default="")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--save-result")
    ap.add_argument("--operator-cache", type=int, help="peec.OPERATOR_CACHE for the run (factorisations per model)")
    ap.add_argument("--available", type=float, help="pretend this much memory is free (MB): solve groups")
    args = ap.parse_args()
    if args.trace:
        import tracemalloc
        tracemalloc.start(1)

    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.components.fields import apply_fields
    from groundtruth.config.settings import SettingsFile

    from groundtruth.solver import peec
    if args.operator_cache:
        peec.OPERATOR_CACHE = args.operator_cache
    if args.available:
        from groundtruth.core import sysmem
        sysmem.available_bytes = lambda: int(args.available * MB)
    made = {"n": 0, "s": 0.0}
    real_factorise = peec.PlaneStack._factorise

    def counted(self, f, exact):
        """The real factorisation, counted and timed."""
        t = time.perf_counter()
        try:
            return real_factorise(self, f, exact)
        finally:
            made["n"] += 1
            made["s"] += time.perf_counter() - t
    peec.PlaneStack._factorise = counted
    board = load_board(args.board)
    cfg = SettingsFile.load(args.settings).config
    cfg.analysis.gpu = args.gpu
    if args.threads:
        cfg.analysis.threads = int(args.threads) if args.threads.isdigit() else args.threads
    if args.resolution:
        cfg.analysis.resolution = args.resolution
    apply_fields(cfg, board)
    gc.collect()
    start = memory()
    print(f"before the first run: private {start[0]:.0f} MB, working set {start[1]:.0f} MB")
    figures = []
    for k in range(args.repeat):
        state = {"phase": "build", "peak": 0.0, "peak_ws": 0.0, "per_phase": {}, "census": None,
                 "census_at": 0.0, "want": False}
        done = threading.Event()
        lock = threading.Lock()

        def sample():
            """Sample the memory every 50 ms until done: the peak per phase, and ask for a census when the peak grew
            by more than 3 % since the last one."""
            while not done.wait(0.05):
                p, w = memory()
                with lock:
                    ph = state["phase"]
                    state["per_phase"][ph] = max(state["per_phase"].get(ph, 0.0), p)
                    if p > state["peak"]:
                        state["peak"] = p
                        if p > state["census_at"] * 1.03:
                            state["want"] = True
                    state["peak_ws"] = max(state["peak_ws"], w)
        threading.Thread(target=sample, daemon=True).start()

        def progress(msg, frac):
            """The progress callback: note the phase, and take a census when the sampler asked for one (here, in the
            solving thread, where the objects are reachable)."""
            with lock:
                state["phase"] = phase_of(msg)
                want = state["want"]
                state["want"] = False
            if want:
                c = census()
                p = memory()[0]
                with lock:
                    state["census"], state["census_at"] = {**c, "private_mb": round(p)}, p
        before = memory()
        made.update(n=0, s=0.0)
        if args.trace:
            import tracemalloc
            tracemalloc.reset_peak()
        box = {}

        def job():
            """The Simulate, as the plugin's job runs it."""
            res = simulate(board, cfg, args.only, progress=progress)
            res.drop_factorisations()
            box["res"] = res
        t0 = time.perf_counter()
        th = threading.Thread(target=job)
        th.start()
        th.join()
        seconds = time.perf_counter() - t0
        done.set()
        res = box.get("res")
        if res is None:
            print("the run failed")
            return 1
        held = memory()
        held_census = census()
        groups = getattr(res, "groups", 1)
        models_mb = res.model_bytes() / MB if hasattr(res, "model_bytes") else None
        dropped = None
        if hasattr(res, "drop_models"):             # what the plugin keeps when the models don't fit (keep_models)
            res.drop_models()
            gc.collect()
            time.sleep(0.5)
            dropped = memory()
        if args.save_result and k == 0:
            from groundtruth import __version__
            from groundtruth.report import formats
            Path(args.save_result).write_text(formats.to_json(args.board, res.issues, res.routes,
                                                              {"mode": "simulate", "version": __version__},
                                                              res.converters, res.bridges), encoding="utf-8")
        del res
        box.clear()
        gc.collect()
        time.sleep(0.5)                                 # the factorisation hosts free what they were handed
        after = memory()
        after_census = census()
        fig = {"label": args.label, "run": k + 1, "seconds": round(seconds, 1), "groups": groups,
               "factorisations": made["n"], "factorising_s": round(made["s"], 1),
               "private_before": round(before[0]), "private_peak": round(state["peak"]),
               "working_set_peak": round(state["peak_ws"]), "private_with_result": round(held[0]),
               "private_after": round(after[0]),
               "models_estimate_mb": None if models_mb is None else round(models_mb),
               "private_models_dropped": None if dropped is None else round(dropped[0]),
               "working_set_models_dropped": None if dropped is None else round(dropped[1]),
               "working_set_with_result": round(held[1]), "working_set_after": round(after[1]), "per_phase_peak": {p: round(v) for p, v in state["per_phase"].items()},
               "census_near_peak": state["census"], "census_with_result": held_census}
        if args.trace:
            import tracemalloc
            fig["traced_peak_mb"] = round(tracemalloc.get_traced_memory()[1] / MB)
        figures.append(fig)
        print(f"run {k + 1}: {seconds:.0f} s; private before {before[0]:.0f}, peak {state['peak']:.0f} MB "
              f"(working set peak {state['peak_ws']:.0f}), with the result {held[0]:.0f}, after {after[0]:.0f} MB")
        print(f"   peak per phase: {fig['per_phase_peak']}; factorisations {made['n']} ({made['s']:.0f} s); "
              f"solved in {fig['groups']} group(s)")
        if dropped is not None:
            print(f"   models (estimate) {models_mb:.0f} MB; with them dropped: private {dropped[0]:.0f}, working set "
                  f"{dropped[1]:.0f} MB (with them: {held[1]:.0f})")
        print(f"   census near the peak: {state['census']}")
        print(f"   census with the result kept: {held_census}")
        print(f"   census after it was let go: {after_census}")
        if args.trace:
            print(f"   traced (Python + numpy) peak: {fig['traced_peak_mb']} MB")
    if args.json:
        p = Path(args.json)
        old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        p.write_text(json.dumps(old + figures, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
