"""What the solver did during one run: where a Simulate's time goes (docs/SPEED.md).

The user, 2026-10-24, of an 8x Simulate on the M2 compute server: "do a thorough research on how to optimise the
calculations to cut down on computation time". The kept runs showed the same job 84x slower at 8x than at 1x for
about 20-25x the work, and what the rest went to could only be estimated from the code. A Collector made around one
Simulate (`with collect() as c`, analysis/simulate.simulate) counts:

* per model (a PlaneStack, named by the scheduler with `label`): its size; the factorisations made, their seconds
  and fill, and those made again after being freed; the time a factorisation waited at the memory gate; the
  low-rank updates of a derived stack; the solves, their GMRES iterations, failures, borrowed preconditioners and
  borrows past their budget; for solves on the CPU the seconds in the L_p product (its FFTs and multiply-add
  apart), the preconditioner (its SuperLU triangular solves apart) and the Gram-Schmidt orthogonalisation; the
  seconds a solve waited for another thread's factorisation of its frequency;
* per scheduling round (simulate.solve_setups): the tasks, the threads and the model whose memory limited them, the
  wall time against the solves' summed time (the rest is threads waiting for the round's slowest solve);
* per phase of the run (`mark`): its wall time; and the process's CPU time, memory (sampled, and the system's own
  peak) and the machine's swap traffic during the run.

`Collector.as_dict` gives it as plain data: the result keeps it in SimulationResult.solver["stats"], which the
simulation history writes into every kept run's run.json (tools/dev/run_stats.py prints it). Counting changes
nothing a solve computes (the same arithmetic in the same order: tests/test_stats.py); outside a collect() block
each hook is one list lookup. `ENABLED = False` switches the collection off."""
from __future__ import annotations

import os
import sys
import threading
import time
import weakref
from contextlib import contextmanager
from typing import Optional

ENABLED = True              # False: collect() yields None and every hook does nothing
USES_KEPT = 12              # a model's users listed by name at most (the rest counted)

_active: list = [None]      # the Collector of the run in progress (one Simulate at a time per process)
_tls = threading.local()    # per thread: seconds spent factorising and waiting for others' factorisations

# the sums kept per model (Collector.entry); "fill" is the largest factorisation's, not a sum
MODEL_SUMS = ("factorisations", "factorise_s", "remade", "gate_wait_s", "updates", "update_s", "solves",
              "iterations", "gmres_s", "wait_s", "failed", "borrowed", "refactor", "matvecs", "matvec_s", "lp_s",
              "fft_s", "mac_s", "preconds", "precond_s", "lu_solve_s", "gs_s")


class SolveTimes:
    """One solve's split of its time and what happened in it (peec._HostMath and PlaneStack._solve fill it while a
    collector is active): seconds in the system products (`matvec`, of which `lp` the L_p product, itself `fft` and
    `mac`), the preconditioner (`precond`, of which `lu` its SuperLU triangular solves) and Gram-Schmidt (`gs`);
    the products' counts; whether the preconditioner was borrowed and whether that borrow ran past its budget
    (the frequency then factorised itself, `refactor`)."""
    __slots__ = ("matvec", "lp", "fft", "mac", "precond", "lu", "gs", "matvecs", "preconds", "borrowed", "refactor")

    def __init__(self):
        """Nothing timed yet."""
        self.matvec = self.lp = self.fft = self.mac = self.precond = self.lu = self.gs = 0.0
        self.matvecs = self.preconds = self.borrowed = self.refactor = 0


def active() -> Optional["Collector"]:
    """The collector of the run in progress, None outside one (or with ENABLED off)."""
    return _active[0]


def thread_mark() -> tuple[float, float]:
    """This thread's factorising and waiting seconds so far (a solve subtracts what it adds during itself)."""
    return getattr(_tls, "fact", 0.0), getattr(_tls, "wait", 0.0)


def thread_add(kind: str, seconds: float):
    """Add `seconds` of factorising ("fact") or of waiting for another thread's factorisation ("wait") to this
    thread's count (thread_mark)."""
    setattr(_tls, kind, getattr(_tls, kind, 0.0) + seconds)


@contextmanager
def collect():
    """A Collector for the solves inside (None with ENABLED off). Nested: the outer run's collector."""
    if not ENABLED:
        yield None
        return
    if _active[0] is not None:
        yield _active[0]
        return
    c = Collector()
    _active[0] = c
    try:
        yield c
    finally:
        _active[0] = None


def label(stack, kind: str, name: str = ""):
    """Name a model in the active collector (no-op without one): see Collector.label."""
    c = _active[0]
    if c is not None and stack is not None:
        c.label(stack, kind, name)


def mark(name: str):
    """End a phase of the run in the active collector (no-op without one): see Collector.mark."""
    c = _active[0]
    if c is not None:
        c.mark(name)


def _psutil():
    """psutil, or None (a plain CLI install may not have it)."""
    try:
        import psutil
        return psutil
    except Exception:           # noqa: BLE001 - not installed, or broken: no memory figures
        return None


def _rss() -> Optional[int]:
    """This process's resident memory now (bytes), None without psutil."""
    ps = _psutil()
    try:
        return int(ps.Process().memory_info().rss) if ps else None
    except Exception:           # noqa: BLE001
        return None


def _peak_rss() -> Optional[int]:
    """The most resident memory this process has had since it started (bytes): Windows' peak working set, else the
    rusage maximum (bytes on macOS, kilobytes on Linux). None when neither can be told. A compute server's job is a
    process of its own, so there this is the job's peak; in the plugin it covers the window's whole session."""
    ps = _psutil()
    try:
        peak = getattr(ps.Process().memory_info(), "peak_wset", None) if ps else None
        if peak:
            return int(peak)
    except Exception:           # noqa: BLE001
        pass
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(r if sys.platform == "darwin" else r * 1024)
    except Exception:           # noqa: BLE001 - no resource module (Windows without psutil)
        return None


def _swap() -> Optional[tuple[int, int]]:
    """The machine's swap traffic so far, (bytes in, bytes out); None where psutil can't tell (Windows reports 0)."""
    ps = _psutil()
    try:
        s = ps.swap_memory() if ps else None
        return (int(s.sin), int(s.sout)) if s is not None else None
    except Exception:           # noqa: BLE001
        return None


def _available() -> Optional[int]:
    """The memory available to this process now (core.sysmem.available_bytes), None when unknown."""
    try:
        from ..core.sysmem import available_bytes
        return available_bytes()
    except Exception:           # noqa: BLE001
        return None


def machine() -> dict:
    """The machine the run is on: platform, processor, logical and physical cores, memory, the Python and numerical
    library versions (a run's speed depends on all of them)."""
    import platform
    info = {"platform": sys.platform, "machine": platform.machine(), "cpus": os.cpu_count(),
            "python": platform.python_version()}
    if sys.platform == "darwin":
        info["macos"] = platform.mac_ver()[0]
    elif sys.platform == "win32":
        info["windows"] = platform.version()
    ps = _psutil()
    if ps is not None:
        try:
            info["cores"] = ps.cpu_count(logical=False)
            info["ram_gb"] = round(ps.virtual_memory().total / 2 ** 30, 1)
        except Exception:       # noqa: BLE001
            pass
    for mod in ("numpy", "scipy", "numba"):
        m = sys.modules.get(mod)
        if m is not None:
            info[mod] = getattr(m, "__version__", "")
    return info


class Collector:
    """The counts of one run (see the module). Thread-safe: solves in parallel threads add to it."""

    def __init__(self):
        """Start the run's clock, CPU time, memory and swap figures; no models, rounds or phases yet."""
        self.t0 = time.perf_counter()
        self.cpu0 = time.process_time()
        self._lock = threading.Lock()
        self.models: list[dict] = []
        self.rounds: list[dict] = []
        self.marks: list[tuple[str, float]] = [("start", self.t0)]
        self.info: dict = {}                    # set by the scheduler: threads, their limit, memory gate counts
        self.swap0 = _swap()
        self.available0 = _available()
        self.rss_max = _rss() or 0
        self.available_min = self.available0

    # ------------------------------------------------------------------ models
    def entry(self, stack) -> dict:
        """The model's counts (made on first use with its size). Kept on the stack itself, checked to be this
        collector's and this stack's: PlaneStack.with_branches makes shallow copies, which share the base's
        __dict__ contents at first."""
        e = stack.__dict__.get("_stats_entry")
        if e is not None and e["_owner"] is self and e["_ref"]() is stack:
            return e
        with self._lock:
            e = stack.__dict__.get("_stats_entry")
            if e is not None and e["_owner"] is self and e["_ref"]() is stack:
                return e
            base = getattr(stack, "_base", None)
            e = {"label": "", "uses": [], "more_uses": 0, "layers": int(stack.N),
                 "grid": [int(stack.nx), int(stack.ny)], "pitch_mm": round(float(stack.d) * 1e3, 4),
                 "nodes": int(stack.n_nodes), "unknowns": int(stack.nb + stack.Ar.shape[1]),
                 "precond": stack.precond_mode, "derived": base is not None,
                 "extra_branches": int(stack.nb - base.nb) if base is not None else 0, "fill": 0}
            e.update({k: 0 for k in MODEL_SUMS})
            e["_owner"], e["_ref"] = self, weakref.ref(stack)
            stack.__dict__["_stats_entry"] = e
            self.models.append(e)
            return e

    def add(self, stack, **amounts):
        """Add numbers to a model's counts (keys of MODEL_SUMS)."""
        e = self.entry(stack)
        with self._lock:
            for k, v in amounts.items():
                e[k] += v

    def label(self, stack, kind: str, name: str = ""):
        """Name a model by what it is ("board-wide", "route", "DC", ...: the first name given stays) and note who uses
        it (`name`: a net, a power stage; the first USES_KEPT listed, the rest counted)."""
        e = self.entry(stack)
        with self._lock:
            if not e["label"]:
                e["label"] = kind
            if name and name not in e["uses"]:
                if len(e["uses"]) < USES_KEPT:
                    e["uses"].append(name)
                else:
                    e["more_uses"] += 1

    def factorised(self, stack, seconds: float, fill: int):
        """A factorisation of the model was made in `seconds` with `fill` stored entries."""
        e = self.entry(stack)
        with self._lock:
            e["factorisations"] += 1
            e["factorise_s"] += seconds
            e["fill"] = max(e["fill"], int(fill))

    def solved(self, stack, iterations: Optional[int], wall: float, mark: tuple[float, float],
               times: Optional[SolveTimes], failed: bool = False):
        """A solve on the model ended after `wall` seconds (`mark`: thread_mark at its start: what this thread spent
        factorising and waiting meanwhile is the factorisations' time, not GMRES's), with its iterations (None when
        it failed) and its time split (`times`, CPU solves)."""
        fact, wait = thread_mark()
        fact, wait = fact - mark[0], wait - mark[1]
        e = self.entry(stack)
        with self._lock:
            e["solves"] += 1
            e["failed"] += int(failed)
            e["iterations"] += int(iterations or 0)
            e["gmres_s"] += max(wall - fact - wait, 0.0)
            e["wait_s"] += wait
            if times is not None:
                for k, attr in (("matvecs", "matvecs"), ("matvec_s", "matvec"), ("lp_s", "lp"), ("fft_s", "fft"),
                                ("mac_s", "mac"), ("preconds", "preconds"), ("precond_s", "precond"),
                                ("lu_solve_s", "lu"), ("gs_s", "gs"), ("borrowed", "borrowed"),
                                ("refactor", "refactor")):
                    e[k] += getattr(times, attr)

    # ------------------------------------------------------------------ the run
    def sample(self):
        """Note the process's memory and the memory available now (their extremes are kept)."""
        rss, avail = _rss(), _available()
        with self._lock:
            if rss:
                self.rss_max = max(self.rss_max, rss)
            if avail is not None:
                self.available_min = avail if self.available_min is None else min(self.available_min, avail)

    def mark(self, name: str):
        """The run's phase called `name` ends now (its wall time is from the previous mark); memory sampled."""
        self.sample()
        with self._lock:
            self.marks.append((name, time.perf_counter()))

    def round(self, **info):
        """A scheduling round's figures (simulate.solve_setups: its tasks, threads, limit, wall and busy seconds)."""
        self.sample()
        with self._lock:
            self.rounds.append(dict(info))

    def summary_line(self) -> str:
        """One line for the run's last progress message (the Server jobs tab shows it as the job's last step): the
        solves, iterations and factorisations with their seconds, the solve threads and what limited them."""
        tot = self.totals()
        threads = sorted({r.get("threads") for r in self.rounds if r.get("threads")})
        text = (f"{tot['solves']} solves, {tot['iterations']} GMRES iterations ({tot['gmres_s']:.0f} s), "
                f"{tot['factorisations']} factorisations ({tot['factorise_s']:.0f} s)")
        if threads:
            text += f", {threads[0]}" + (f"-{threads[-1]}" if threads[-1] != threads[0] else "") + " solve thread(s)"
        limit = self.info.get("memory_limit")
        if limit and self.info.get("threads_memory", 0) < self.info.get("threads_cpu", 0):
            text += f" (memory: {limit['model']} ~{limit['gb']:.1f} GB per solve)"
        return text

    def totals(self) -> dict:
        """The models' counts summed (the largest fill)."""
        with self._lock:
            out = {k: sum(e[k] for e in self.models) for k in MODEL_SUMS}
            out["fill"] = max((e["fill"] for e in self.models), default=0)
        return out

    def as_dict(self) -> dict:
        """Everything as plain data (JSON-able): wall and CPU time, phases, rounds, models (the costliest first, with
        the solver's own size-limit and least-fill counts from the stacks still alive), totals, memory, machine."""
        self.sample()
        now = time.perf_counter()
        wall = now - self.t0
        cpu = time.process_time() - self.cpu0
        phases = [{"phase": name, "s": round(t - prev, 2)}
                  for (_, prev), (name, t) in zip(self.marks, self.marks[1:])]
        if self.marks[-1][1] < now:
            phases.append({"phase": "rest", "s": round(now - self.marks[-1][1], 2)})
        models = []
        with self._lock:
            entries = list(self.models)
        for e in sorted(entries, key=lambda e: -(e["factorise_s"] + e["gmres_s"] + e["update_s"])):
            d = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in e.items() if not k.startswith("_")}
            st = e["_ref"]()
            if st is not None:
                d["lu_fallbacks"] = int(getattr(st, "lu_fallbacks", 0) or 0)
                d["least_fill"] = int(getattr(st, "lu_memory_fallbacks", 0) or 0)
            models.append(d)
        swap1 = _swap()
        peak = _peak_rss()
        mb = lambda b: None if b is None else round(b / 2 ** 20)        # noqa: E731
        return {
            "wall_s": round(wall, 2), "cpu_s": round(cpu, 2),
            "cpu_share": round(cpu / (wall * (os.cpu_count() or 1)), 3) if wall > 0 else None,
            "phases": phases, "rounds": list(self.rounds), "models": models,
            "totals": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.totals().items()},
            "info": dict(self.info),
            "memory": {"rss_max_mb": mb(self.rss_max), "process_peak_mb": mb(peak),
                       "available_start_mb": mb(self.available0), "available_min_mb": mb(self.available_min),
                       "swap_in_mb": mb(swap1[0] - self.swap0[0]) if swap1 and self.swap0 else None,
                       "swap_out_mb": mb(swap1[1] - self.swap0[1]) if swap1 and self.swap0 else None},
            "machine": machine(),
        }
