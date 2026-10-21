"""The thermal solve in a process of its own, for the plugin (as PCB Thermal runs its solver).

pyamg's C++ routines hold Python's GIL for the whole call (multigrid setup on 1.1 M unknowns: about half a second
at a time), which froze the plugin window for 528 ms during a thread-based solve (gui_probe on kraft_flux_mini_2).
In a child process nothing the solver does can stall the window, Cancel simply ends the process, and its memory
is returned when it exits.

The parent prepares the inputs (fast: copper_heat and part_losses, or capacity_inputs) and pickles them to a
temporary file; the child (`python -m groundtruth.analysis.thermal_proc IN OUT`, no console window, below-normal
priority) solves, reports progress as lines "P <fraction> <message>" on stdout, and pickles the result to OUT.

Memory watchdog (as PCB Thermal's runner): the parent checks the child twice a second and stops it when it uses
more than 60 % of the computer's memory, or when the computer is about to run out (less than 5 % free, at least
0.5 GB) while the child holds more than that: KiCad and the rest of the desktop must keep working."""
from __future__ import annotations

import os
import pickle
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
MEMORY_SHARE = 0.6                       # of the RAM, for the child alone (PCB Thermal's limit)
FREE_SHARE, FREE_MIN_MB = 0.05, 512.0    # memory to leave free for everything else
MEMORY_CHECK_S = 0.5

Progress = Callable[[str, float], None]


class ThermalCancelled(Exception):
    """The user pressed Cancel while the child solved: the child was killed."""
    pass


class ThermalMemoryError(MemoryError):
    """The watchdog stopped the solve, or the child ran out of memory itself: it needed more memory than this
    computer can spare. `detail`: the child's error output (its traceback) when it ended itself."""
    detail = ""


THERMAL = "the thermal solve"           # what _run_child runs unless told otherwise (its messages name it)
THERMAL_ADVICE = "use thermal.quality: draft (or normal instead of fine), or close other programs"


def memory_limits() -> Optional[tuple[float, float]]:
    """(the child's memory limit, the free memory to keep) in MB, or None without psutil (no watchdog)."""
    try:
        import psutil
        total = psutil.virtual_memory().total / 2 ** 20
    except Exception:
        return None
    return total * MEMORY_SHARE, max(FREE_MIN_MB, total * FREE_SHARE)


def _python() -> str:
    """The interpreter for the child: python.exe next to pythonw.exe (the plugin runs under pythonw, whose
    standard streams may be missing), else this one."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def run_in_process(board, config, converters, progress: Optional[Progress] = None, cancel=None,
                   poll_s: float = 0.1, limits: Optional[tuple[float, float]] = None, server=None):
    """run_thermal's result (ThermalResult, issues), solved in a child process, or with `server` (a
    remote.client.Remote) on the compute server; the inputs are prepared here either way (fast), and so are the DC
    paths at the solved temperatures (with_dc_at_temperature: the stages' maps stay here)."""
    from .thermal import NO_STAGES, _empty, thermal_inputs, with_dc_at_temperature
    inputs = thermal_inputs(board, config, converters, progress)
    if inputs is None:
        return _empty(config, NO_STAGES), []

    def solve(inputs):
        """One thermal solve of these inputs, on the compute server or in the child."""
        if server is not None:
            return server.run("thermal", (board, config, inputs), progress)
        (res, issues), peak = _run_child(("solve", board, config, inputs), progress, cancel, poll_s, limits)
        if peak:
            res.mesh["memory_mb"] = round(peak)
        return res, issues
    res, issues = solve(inputs)
    if config.thermal.electrothermal:
        # rounds of DC paths at temperature (solved here, where the stages' excitations are) and Thermal (there)
        from ..thermal.solver.common import Cancelled
        from .electrothermal import couple
        try:
            res, issues, _ = couple(board, config, converters, res, issues,
                                    lambda results: solve(thermal_inputs(board, config, results)), progress, cancel)
        except Cancelled as e:          # Cancel between the rounds: as a cancelled thermal solve
            raise ThermalCancelled("thermal solve cancelled") from e
        return res, issues
    return with_dc_at_temperature(res, board, converters), issues


def capacity_in_process(board, config, converters, progress: Optional[Progress] = None, cancel=None,
                        poll_s: float = 0.1, limits: Optional[tuple[float, float]] = None, server=None):
    """run_capacity's result (a ThermalResult per power stage, at its capacity), solved in a child process, or
    with `server` (a remote.client.Remote) on the compute server."""
    from .thermal import NO_STAGES, _bridges, _empty, _stages, capacity_inputs
    if not _stages(converters) and not _bridges(converters):
        return [_empty(config, NO_STAGES)]
    if progress:
        progress("capacity: the heat of the DC paths and the parts per power stage", 0.01)
    inputs = capacity_inputs(board, config, converters)
    if server is not None:
        return server.run("capacity", (board, config, inputs), progress)
    results, peak = _run_child(("capacity", board, config, inputs), progress, cancel, poll_s, limits)
    for r in results:
        if peak:
            r.mesh["memory_mb"] = round(peak)
    return results


def _run_child(job, progress, cancel, poll_s, limits, module: str = "groundtruth.analysis.thermal_proc",
               args: tuple = (), env_extra: Optional[dict] = None, raw: bool = False,
               stats: Optional[dict] = None, what: str = THERMAL):
    """Run `job` in the child; (its result, the child's peak memory in MB or 0). `stats`, when given, is kept up to
    date with the child's memory at every watchdog check (rss_mb, commit_mb, peak_mb: the compute server reports
    them to the plugin).

    The compute server (remote/server.py) runs its jobs the same way: `module` is the child's entry (called as
    `python -m module IN OUT *args`), `job` the already pickled input (bytes are written as they are),
    `env_extra` more environment variables (a GPU's number), and `raw` returns the result file's bytes unread
    (the server only passes them on). `what` names the work in the errors ("the job": a Simulate on the server
    that ran out of memory was reported as "the thermal solve ran out of memory", 2026-10-19).

    The child is told its memory limit (core.sysmem.LIMIT_ENV): the solver's memory gate plans within it, so the
    watchdog never has to stop a job that could have made do with less."""
    # The inputs and the result go through pickle files in a new private temporary folder (mkdtemp: readable
    # by this user only). Unpickling runs code from the file, so these must never be files someone else could
    # have written: they are only ever this folder's, written by this process and its child.
    work = Path(tempfile.mkdtemp(prefix="groundtruth-thermal-"))
    inp, out = work / "in.pickle", work / "out.pickle"
    limits = memory_limits() if limits is None else limits
    try:
        with open(inp, "wb") as f:
            if isinstance(job, bytes):
                f.write(job)
            else:
                pickle.dump(job, f, protocol=pickle.HIGHEST_PROTOCOL)
        import groundtruth
        from ..core.sysmem import LIMIT_ENV
        # the child imports groundtruth from the same place as this process (the plugin's folder is not on the
        # child's default path)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            [str(Path(groundtruth.__file__).resolve().parents[1])] + [p for p in [os.environ.get("PYTHONPATH")] if p]),
            PYTHONIOENCODING="utf-8", **(env_extra or {}))
        if limits is not None:
            env[LIMIT_ENV] = f"{limits[0]:.0f}"         # the watchdog's limit, MB (sysmem.available_bytes)
        flags = (CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS) if sys.platform == "win32" else 0
        proc = subprocess.Popen([_python(), "-m", module, str(inp), str(out), *map(str, args)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=flags,
                                text=True, encoding="utf-8", errors="replace")
        import threading
        lines: list[str] = []
        errs: list[str] = []

        def read(stream, into):                      # both pipes drained, so a chatty child never blocks
            """Collect a pipe's lines (in a thread of its own) until the child closes it."""
            for line in stream:
                into.append(line.rstrip("\n"))
        readers = [threading.Thread(target=read, args=(proc.stdout, lines), daemon=True),
                   threading.Thread(target=read, args=(proc.stderr, errs), daemon=True)]
        for r in readers:
            r.start()
        watch = _Watchdog(proc.pid, limits, stats, what)
        try:
            while proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise ThermalCancelled("thermal solve cancelled")
                while lines:
                    line = lines.pop(0)
                    if line.startswith("P ") and progress:
                        _, frac, msg = line.split(" ", 2)
                        progress(msg, float(frac))      # may raise the caller's own cancel exception
                watch.check()                           # raises ThermalMemoryError
                time.sleep(poll_s)
        except BaseException:
            proc.kill()                                 # cancelled (here or by the progress callback): end it
            proc.wait()
            raise
        for r in readers:
            r.join(timeout=2)
        err = "\n".join(errs)
        if proc.returncode != 0 or not out.exists():
            tail = err.strip().splitlines()[-1:] or ["no message"]
            if "MemoryError" in tail[0]:
                e = ThermalMemoryError(f"{what} ran out of memory" + (f": {THERMAL_ADVICE}" if what == THERMAL else
                                                                     f" ({tail[0]})"))
                e.detail = err[-3000:]          # the traceback: where it ran out (the server keeps it with the job)
                raise e
            raise RuntimeError(f"{what} failed (exit code {proc.returncode}): {tail[0]}\n\n{err[-3000:]}")
        if raw:
            return out.read_bytes(), watch.peak_mb
        with open(out, "rb") as f:
            return pickle.load(f), watch.peak_mb
    finally:
        for p in (inp, out):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            work.rmdir()
        except OSError:
            pass


class _Watchdog:
    """The child's memory, checked every MEMORY_CHECK_S: over its limit, or the computer almost out while the
    child holds a large share, stops the solve (ThermalMemoryError; the caller kills the process)."""

    def __init__(self, pid: int, limits: Optional[tuple[float, float]], stats: Optional[dict] = None,
                 what: str = THERMAL):
        """Watch process `pid` with `limits` (memory_limits; None: no watchdog, e.g. without psutil); `stats` gets
        each measurement (see _run_child); `what` names the work in its messages."""
        self.limits, self.stats, self.what = limits, stats, what
        self.peak_mb = 0.0
        self._last = 0.0
        self._proc = None
        if limits is not None:
            try:
                import psutil
                self._psutil, self._proc = psutil, psutil.Process(pid)
            except Exception:
                self.limits = None

    def check(self):
        """Called from the parent's poll loop: at most every MEMORY_CHECK_S, measure the child (and its children)
        and raise ThermalMemoryError when it is over its limit or the computer is running out."""
        if self.limits is None or time.monotonic() - self._last < MEMORY_CHECK_S:
            return
        self._last = time.monotonic()
        try:                                 # a venv's python.exe is a launcher: the interpreter is its child
            tree = [self._proc] + self._proc.children(recursive=True)
            infos = [p.memory_info() for p in tree]
            rss = sum(i.rss for i in infos) / 2 ** 20
            # what the processes have reserved (Windows: private bytes, counted against the commit limit; SuperLU
            # reserves ~6.5x what it touches): the figure that runs out first on a server with a small page file
            commit = (sum(i.private for i in infos) / 2 ** 20) if all(hasattr(i, "private") for i in infos) else None
            free = self._psutil.virtual_memory().available / 2 ** 20
        except Exception:                    # the child just ended, or no access: nothing to judge
            return
        self.peak_mb = max(self.peak_mb, rss)
        if self.stats is not None:
            self.stats.update(rss_mb=rss, commit_mb=commit, peak_mb=self.peak_mb)
        limit, keep_free = self.limits
        advice = f" ({THERMAL_ADVICE})" if self.what == THERMAL else ""
        if rss > limit:
            raise ThermalMemoryError(
                f"{self.what} was stopped: it used {rss / 1024:.1f} GB, over its limit of {limit / 1024:.1f} GB"
                f"{advice}")
        if free < keep_free and rss > keep_free:
            raise ThermalMemoryError(
                f"{self.what} was stopped: only {free / 1024:.1f} GB of memory were left free (it held "
                f"{rss / 1024:.1f} GB){advice}")


def main(argv=None) -> int:
    """The child's entry: read the job from IN, solve it (reporting progress on stdout), write the result to OUT
    (atomically: a .tmp renamed, so the parent never reads half a file)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    inp, out = Path(argv[0]), Path(argv[1])
    with open(inp, "rb") as f:
        mode, board, config, data = pickle.load(f)
    from .thermal import solve_capacity, solve_thermal

    def progress(msg: str, frac: float):
        """Report progress to the parent: one "P <fraction> <message>" line, flushed at once."""
        print(f"P {frac:.4f} {msg}", flush=True)
    if mode == "capacity":
        result = solve_capacity(board, config, data, progress)
    else:
        heat, losses, notes = data
        result = solve_thermal(board, config, heat, losses, notes, progress)
    tmp = out.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
