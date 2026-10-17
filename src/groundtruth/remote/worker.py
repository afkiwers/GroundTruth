"""One compute-server job in a process of its own: `python -m groundtruth.remote.worker IN OUT KIND THREADS GPU`.

The server (remote/server.py) starts it through thermal_proc._run_child, as the plugin starts its thermal solve:
the job's arguments come pickled in IN (read with the restricted unpickler: the server checked the request's
signature, this is the second line of defence), progress goes to stdout as "P <fraction> <message>" lines, and the
result is pickled to OUT (a .tmp renamed, never half a file). A process per job: Cancel ends it, a crash or a
MemoryError takes only this job, and its memory goes back to the server when it exits.

The kinds:
- "simulate" (board, config, nets): simulate(); the result without its factorisations and models (the plugin's
  Suggest fixes then simulates its own baseline, on the server too);
- "suggest" (board, config): suggest_fixes(); the two simulations it keeps, without their models;
- "thermal" (board, config, (heat, losses, notes)) and "capacity" (board, config, inputs): the thermal solve and the
  current-capacity search, with the inputs the plugin prepared (thermal_inputs / capacity_inputs, fast);
- "sleep" (seconds,): only with GROUNDTRUTH_SERVER_TEST set, for the queue's tests.

A Simulate split over the cluster (remote/split.py) adds three kinds; JOB is the split job's own input as the
server keeps it (the pickled (board, config, nets)), passed on unopened:
- "items" (the split job's (board, config, nets)): {items: analysis.split.split_items (names, costs, memory
  estimates), board, resolution} (the keys of what earlier splits measured);
- "part" (JOB, names, checks): simulate(nets=names, part=...) of some items, without models;
- "merge" (JOB, [part results as fetched], notes): analysis.split.merge_parts, the whole Simulate's result.

The server's own choices replace the plugin's for its hardware: the solver threads and the GPU (THREADS, GPU:
"auto", a number, "on", "off", or "-" to keep the job's). The resolution stays the user's.
"""
from __future__ import annotations

import os
import pickle
import sys
import time
from dataclasses import replace
from pathlib import Path


def _on_server(config, threads: str, gpu: str):
    """The job's config with the server's solver threads and GPU choice ("-": the job's own)."""
    a = config.analysis
    if threads != "-":
        a = replace(a, threads=int(threads) if threads.isdigit() else threads)
    if gpu != "-":
        a = replace(a, gpu=gpu)
    return replace(config, analysis=a)


def run(kind: str, args: tuple, threads: str = "-", gpu: str = "-", progress=None):
    """The result of one job (see the module)."""
    if kind == "simulate":
        from ..analysis.simulate import simulate
        board, config, nets = args
        res = simulate(board, _on_server(config, threads, gpu), nets=nets, progress=progress)
        res.drop_factorisations()
        res.drop_models()
        return res
    if kind == "items":
        from ..analysis.split import split_items
        board, config, nets = args
        # the board and the resolution: the keys of what the splitting server measured in earlier runs
        return {"items": split_items(config, nets), "board": str(board.source or ""),
                "resolution": float(config.analysis.resolution or 1.0)}
    if kind == "part":
        from ..analysis.simulate import simulate
        from .protocol import load_raw
        job, names, checks = args
        board, config, nets = load_raw(job)
        res = simulate(board, _on_server(config, threads, gpu), nets=list(names), progress=progress,
                       part={"checks": bool(checks), "job_nets": nets})
        res.drop_factorisations()
        res.drop_models()
        return res
    if kind == "merge":
        from ..analysis.split import merge_parts
        from .protocol import load_raw, loads
        job, fetched, notes = args
        _board, config, nets = load_raw(job)
        if progress:
            progress("joining the parts", 0.5)
        return merge_parts(config, nets, [loads(b) for b in fetched], notes, _board)
    if kind == "suggest":
        from ..analysis.suggest import suggest_fixes
        board, config = args
        res = suggest_fixes(board, _on_server(config, threads, gpu), progress=progress)
        for r in (res.before, res.after):
            if r is not None:
                r.drop_factorisations()
                r.drop_models()
        return res
    if kind == "thermal":
        from ..analysis.thermal import solve_thermal
        board, config, (heat, losses, notes) = args
        return solve_thermal(board, config, heat, losses, notes, progress)
    if kind == "capacity":
        from ..analysis.thermal import solve_capacity
        board, config, inputs = args
        return solve_capacity(board, config, inputs, progress)
    if kind == "sleep" and os.environ.get("GROUNDTRUTH_SERVER_TEST"):
        seconds = float(args[0])
        steps = max(1, int(seconds / 0.05))
        for k in range(steps):
            if progress:
                progress(f"sleeping {k + 1}/{steps}", (k + 1) / steps)
            time.sleep(seconds / steps)
        return "slept"
    raise ValueError(f"unknown job kind {kind!r}")


KINDS = ("simulate", "suggest", "thermal", "capacity", "sleep", "items", "part", "merge")
CLIENT_KINDS = ("simulate", "suggest", "thermal", "capacity", "sleep", "part")   # what POST /v1/jobs takes
PARENT_CHECK_S = 5.0


def _end_with_parent():
    """Stop this job when the server that started it is gone (the service stopped, its process killed): its
    result would go to nobody, and an orphaned job would keep every core busy. A daemon thread checks the parent
    every PARENT_CHECK_S (psutil; without it nothing is checked). The venv's python.exe is a launcher, so the
    parent may be the launcher whose own parent is the server: both are watched."""
    import threading
    try:
        import psutil
        me = psutil.Process()
        watched = [p for p in (me.parent(), me.parent().parent() if me.parent() else None) if p is not None]
    except Exception:                       # noqa: BLE001 - no psutil, no access: nothing to watch
        return
    if not watched:
        return

    def watch():
        """Exit at once when a watched ancestor has ended."""
        while True:
            time.sleep(PARENT_CHECK_S)
            if not all(p.is_running() for p in watched):
                os._exit(3)
    threading.Thread(target=watch, name="gt-parent-watch", daemon=True).start()


def main(argv=None) -> int:
    """The child's entry (see the module)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    inp, out, kind = Path(argv[0]), Path(argv[1]), argv[2]
    threads, gpu = (argv[3], argv[4]) if len(argv) > 4 else ("-", "-")
    _end_with_parent()
    from .protocol import load_file
    args = load_file(inp)

    def progress(msg: str, frac: float):
        """Report progress to the server: one "P <fraction> <message>" line, flushed at once."""
        print(f"P {frac:.4f} {' '.join(str(msg).split())}", flush=True)
    result = run(kind, args, threads, gpu, progress)
    tmp = out.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
