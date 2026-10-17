"""Find where the plugin window's GUI thread stalls, on a real board, without KiCad and without writing next to it.

    .venv/Scripts/python tools/dev/gui_probe.py BOARD.kicad_pcb [--settings S.groundtruth.yaml] [--simulate]
        [--out DIR] [--threshold-ms 150] [--default-gil]

The GIL switch interval is set as in the plugin (ui/gil.py); --default-gil keeps
Python's 5 ms to compare. For kraft_flux_mini_2 with its real operating points, build the settings with
tools/dev/real_settings.py BOARD OUT.groundtruth.yaml first (a full --simulate run takes ~7 min).

The board (and its .kicad_pro, and the settings file if given) is copied into DIR (default: a new temporary
folder) and the window opens on the copy in file mode, so the real board's folder is never written. A 20 ms
timer records when the GUI thread handles events; a watchdog thread samples the GUI thread's Python stack while
no tick has come for --threshold-ms, so every stall is printed with the code that caused it.

Steps, each started once the window is idle: load (plus the auto-detection when the settings list nothing), Run
checks, Simulate (--simulate), every heatmap view, every layer choice of the first view, the arrows off and on,
the cross-section off and on, the Heatmap page scrolled down and up, the wheel zoom, every tab, Save settings. Message boxes are answered automatically and printed.
Garbage collections over 50 ms are listed too (each holds the GIL), and those within a stall are named with it.
--cache R.pickle: the first run pickles Simulate's results there, later runs show them instead of simulating.
"""
from __future__ import annotations

import argparse
import collections
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import wx  # noqa: E402


class Ticker(wx.Timer):
    def __init__(self, on_tick):
        """A timer calling on_tick."""
        super().__init__()
        self.on_tick = on_tick

    def Notify(self):
        """wx calls this on each tick."""
        self.on_tick()


CPU_EVERY = 0.25            # s between two readings of the threads' CPU times
READ_PAUSE_S = 15.0         # s the probe "reads" Simulate's results before the next step


class Watchdog(threading.Thread):
    """Samples the GUI thread's stack while it has not ticked for `threshold` seconds."""

    def __init__(self, threshold: float, cpu: bool = False):
        """A watchdog for the GUI thread (the main thread), stall threshold in seconds; memory and page faults are
        sampled too when psutil is there."""
        super().__init__(daemon=True)
        self.threshold = threshold
        self.last_tick = time.perf_counter()
        self.gui = threading.main_thread().ident
        self.samples: list[tuple[float, tuple]] = []      # (time, innermost frames)
        self.faults: list[tuple[float, int, float]] = []  # (time, page faults so far, working set MB): paging
        try:
            import psutil
            self._proc = psutil.Process()
        except ImportError:
            self._proc = None
        self.other_samples: list[tuple[float, tuple]] = []  # (time, one line per other thread)
        # (time, {OS thread id: CPU seconds so far}): which thread burned CPU during a stall (the one holding the
        # GIL, or a native pool thread competing for the cores); and the GIL switch interval then
        self.cpu: list[tuple[float, dict]] = []
        self.names: dict = {}                               # OS thread id -> Python thread name (while alive)
        self.with_cpu = cpu
        self.intervals: list[tuple[float, float]] = []
        self.stop = threading.Event()

    def run(self):
        """Every 30 ms: note page faults and working set; when the GUI thread has not ticked for longer than the
        threshold, sample its stack and the other threads' innermost frames. Every CPU_EVERY seconds the threads'
        CPU times (psutil lists every thread of the process: ~10 ms, too costly every 30 ms; it slowed a kraft
        Simulate from 870 to 2200 s)."""
        me = threading.get_ident()
        self.names[threading.get_native_id()] = "probe watchdog"
        next_cpu = 0.0
        while not self.stop.is_set():
            if self._proc is not None:
                m = self._proc.memory_info()
                self.faults.append((time.perf_counter(), getattr(m, "num_page_faults", 0), m.rss / 1e6))
                if self.with_cpu and time.perf_counter() >= next_cpu:
                    next_cpu = time.perf_counter() + CPU_EVERY
                    for th in threading.enumerate():
                        if th.native_id is not None and th.native_id not in self.names:
                            self.names[th.native_id] = th.name
                    try:
                        self.cpu.append((time.perf_counter(), {t.id: t.user_time + t.system_time
                                                               for t in self._proc.threads()}))
                    except Exception:           # a thread ended while listed
                        pass
            self.intervals.append((time.perf_counter(), sys.getswitchinterval()))
            if time.perf_counter() - self.last_tick > self.threshold:
                frames = sys._current_frames()
                now = time.perf_counter()
                if self.gui in frames:
                    self.samples.append((now, _summary(frames[self.gui])))
                # a GUI thread stuck in MainLoop waits for the GIL: show what the other threads are doing
                others = tuple(f"[{t}] " + " < ".join(reversed(_summary(f)[-3:])) for t, f in frames.items()
                               if t not in (self.gui, me))
                self.other_samples.append((now, others))
            time.sleep(0.03)


class GcLog:
    """Python's cyclic garbage collections (gc.callbacks): when each ran, how long, which generation. A full
    collection holds the GIL throughout, whichever thread triggered it, so the window can't respond meanwhile."""

    def __init__(self):
        """No collections recorded yet."""
        self.runs: list[tuple[float, float, int, int]] = []     # (start, seconds, generation, objects tracked)
        self._t0 = {}

    def __call__(self, phase, info):
        """gc's callback: time each collection from its start to its stop, per thread."""
        me = threading.get_ident()
        if phase == "start":
            self._t0[me] = time.perf_counter()
        elif me in self._t0:
            t0 = self._t0.pop(me)
            self.runs.append((t0, time.perf_counter() - t0, info["generation"], info.get("collected", 0)))

    def within(self, t: float, d: float, at_least: float = 0.02) -> list:
        """The collections of at least at_least seconds that overlap [t, t + d]."""
        return [r for r in self.runs if r[1] >= at_least and r[0] < t + d and r[0] + r[1] > t]


def cached_start(f, cache: Path):
    """--cache: the window's Simulate job (MainFrame._start with solver=True) pickles what it hands the window
    (without the models kept for the fix search, and without a history entry), or, once that file exists, hands
    the pickled results over instead of simulating. Everything after the job (MainFrame._done) runs as usual."""
    import pickle
    real = f._start

    def start(status, work, done=None, **kw):
        """MainFrame._start for the probe: a solver job is replaced by the cache (loaded, or made on the first run);
        other jobs run as they are."""
        if not kw.get("solver"):
            return real(status, work, done, **kw)
        if cache.exists():
            def work(progress):                 # noqa: F811 - the cached results instead
                """The job: the pickled results instead of a Simulate."""
                progress("loading the cached results", 0.5)
                with open(cache, "rb") as fh:
                    return pickle.load(fh)
        else:
            simulate = work

            def work(progress):                 # noqa: F811
                """The job: the real Simulate, its results pickled for the next probe run."""
                r = list(simulate(progress))
                r[3], r[6] = None, None         # no models for the fix search, no history entry
                with open(cache, "wb") as fh:
                    pickle.dump(tuple(r), fh)
                return tuple(r)
        return real(status, work, done, **kw)
    f._start = start


def _cpu_during(dog, t: float, d: float) -> list[str]:
    """Lines naming the threads that used the most CPU during the stall [t, t + d] (OS thread ids matched to Python
    threads by native_id; others are native pools: BLAS, SuperLU, wx), and the GIL switch interval then."""
    before = [x for x in dog.cpu if x[0] <= t]
    after = [x for x in dog.cpu if x[0] >= t + d]
    out = []
    iv = [v for ts, v in dog.intervals if t <= ts <= t + d]
    if iv:
        out.append(f"GIL switch interval {max(iv) * 1e3:g} ms")
    if not before or not after:
        return out
    a, b = before[-1], after[0]
    names = dict(dog.names)
    names[threading.main_thread().native_id] = "GUI"
    used = sorted(((b[1][k] - a[1].get(k, 0.0), k) for k in b[1]), reverse=True)
    span = b[0] - a[0]
    top = [f"{names.get(k, f'native {k}')} {u * 1e3:.0f} ms" for u, k in used[:4] if u > 0.01]
    if top:
        out.append(f"CPU in {span * 1e3:.0f} ms: " + ", ".join(top))
    return out


def _summary(frame) -> tuple:
    """A thread's stack as its innermost frames (only the project's and the main libraries' when there are any),
    file:line function."""
    stack = traceback.extract_stack(frame)
    ours = [f for f in stack if any(k in f.filename for k in ("groundtruth", "matplotlib", "ruamel", "shapely",
                                                               "numpy", "scipy", "kipy"))]
    return tuple(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in (ours or stack)[-7:])


def main(argv=None) -> int:
    """The command line (see the module): open the window on a copy of the board, walk the steps (tabs, Simulate,
    every heatmap view, zoom, Thermal ...) one at a time once the window is idle, while a 20 ms ticker and the
    watchdog record every stall; then print each stall with its step, stacks, page faults and garbage
    collections."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("board", type=Path)
    p.add_argument("--settings", type=Path, help="settings file to use (copied next to the board copy)")
    p.add_argument("--simulate", action="store_true", help="also simulate and walk every heatmap view")
    p.add_argument("--thermal", action="store_true", help="also run Thermal after Simulate (needs --simulate)")
    p.add_argument("--capacity", action="store_true", help="also run Current capacity after Simulate (needs --simulate)")
    p.add_argument("--out", type=Path, help="scratch folder for the copies (default: a new temporary folder)")
    p.add_argument("--threshold-ms", type=float, default=150.0)
    p.add_argument("--default-gil", action="store_true", help="keep Python's 5 ms GIL switch interval (to compare)")
    p.add_argument("--cache", type=Path, help="Simulate's results, pickled: written by the first run, then the "
                   "Simulate step shows them instead of simulating (minutes saved per probe; no history kept)")
    p.add_argument("--available", type=float, help="pretend this much memory is free (MB): 1000000 solves every "
                   "model together (no groups), as before simulate.solve_groups")
    p.add_argument("--cpu", action="store_true", help="also name the threads that used the CPU during each stall "
                   "(psutil every 0.25 s: its thread listing holds the GIL ~150 ms, so it adds stalls of its own)")
    p.add_argument("--theme", choices=["system", "light", "dark"], default="system",
                   help="the plugin's theme (ui/theme.py), as the plugin sets it at start-up")
    p.add_argument("--real-prefs", action="store_true",
                   help="use this computer's Preferences and server job list (default: a private folder, so the "
                        "window computes here and never touches the user's ui.json or remote_jobs)")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):     # the report has arrows and Ω: cp1252 consoles and pipes can't
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    out = args.out or Path(tempfile.mkdtemp(prefix="gui_probe_"))
    if not args.real_prefs:
        # a private %LOCALAPPDATA%\GroundTruth: the user's Preferences (a compute server) sent the probe's Simulate to
        # the server, and its job tracker worked on the user's own job list (2026-10-16)
        import os
        (out / "localappdata").mkdir(parents=True, exist_ok=True)
        os.environ["LOCALAPPDATA"] = str(out / "localappdata")
    out.mkdir(parents=True, exist_ok=True)
    if out.resolve() == args.board.resolve().parent:
        print("refusing: --out is the board's own folder")
        return 2
    board = Path(shutil.copy2(args.board, out / args.board.name))
    pro = args.board.with_suffix(".kicad_pro")
    if pro.exists():
        shutil.copy2(pro, out / pro.name)
    from groundtruth.config.settings import settings_path
    settings = settings_path(board)                # <out>/groundtruth/<board>.groundtruth.yaml
    if args.settings:
        settings.parent.mkdir(exist_ok=True)
        shutil.copy2(args.settings, settings)
    elif settings.exists():
        settings.unlink()
    print(f"scratch copy: {board}")
    if args.available:
        from groundtruth.core import sysmem
        sysmem.available_bytes = lambda: int(args.available * 1e6)

    from groundtruth.ui.gil import responsive_gui
    if not args.default_gil:
        responsive_gui()                    # as the plugin and groundtruth-gui do
    app = wx.App(False)
    from groundtruth.ui import theme
    theme.init_app(app, args.theme)
    # the whole message: a first line like "Could not run the analysis:" alone hides the reason
    wx.MessageBox = lambda msg, *a, **k: (print("  [message box] " + msg.replace("\n", "\n      ")[:4000]),
                                          wx.NO)[1]
    from groundtruth.ui import main_frame
    t_open = time.perf_counter()
    f = main_frame.MainFrame(main_frame.Session(file_path=str(board)))
    f.Show()
    print(f"window constructed in {time.perf_counter() - t_open:.2f} s (GUI thread; the tabs follow one per "
          "event-loop step, timed with the load)")
    tab_labels = [label for _, label, _ in f._pages]     # the tabs do not exist yet: they are built step by step
    if args.cache:
        cached_start(f, args.cache)

    steps: list[tuple[str, callable]] = [("Run checks", lambda: f.run(False))]
    if args.simulate:
        steps.append(("Simulate", lambda: f.run(True)))
        # someone reads the results before clicking on: the window's idle-time work (ui/idle.py: the garbage
        # collection after new results, the history report's plots) runs then, as it would for a person
        steps.append((f"pause {READ_PAUSE_S:g} s (reading the results)", lambda: state.update(
            hold=time.perf_counter() + READ_PAUSE_S)))
        if args.thermal:
            steps.append(("Thermal", lambda: f.on_thermal()))
        if args.capacity:
            steps.append(("Current capacity", lambda: f.on_capacity()))

        def views():
            """One step per heatmap view in the Route list, then the first again with its layers, arrows and
            cross-section off and on, page scrolling, wheel zoom and the double-click back."""
            n = f.heatmap.choice.GetCount()
            out_steps = []
            for i in range(n):
                label = f.heatmap.choice.GetString(i)
                out_steps.append((f"heatmap view {label[:60]}",
                                  lambda i=i: (f.heatmap.choice.SetSelection(i), f.heatmap._route_changed())))
            if n:
                out_steps.append(("heatmap view 1 again", lambda: (f.heatmap.choice.SetSelection(0),
                                                                   f.heatmap._route_changed())))
                for j in range(f.heatmap.layer.GetCount()):
                    out_steps.append((f"layer choice {f.heatmap.layer.GetString(j)}",
                                      lambda j=j: (f.heatmap.layer.SetSelection(j), f.heatmap.redraw())))
                out_steps.append(("arrows off", lambda: (f.heatmap.arrows.SetValue(False), f.heatmap.redraw())))
                out_steps.append(("arrows on", lambda: (f.heatmap.arrows.SetValue(True), f.heatmap.redraw())))
                out_steps.append(("cross-section off", lambda: (f.heatmap.section_box.SetValue(False),
                                                                f.heatmap.redraw())))
                out_steps.append(("cross-section on", lambda: (f.heatmap.section_box.SetValue(True),
                                                               f.heatmap.redraw())))
                out_steps.append(("page scrolled to the end", lambda: f.heatmap.page.Scroll(
                    -1, f.heatmap.page.GetVirtualSize().height)))
                out_steps.append(("page scrolled to the top", lambda: f.heatmap.page.Scroll(-1, 0)))
                out_steps.append(("wheel zoom in (3 notches)", lambda: wheel(3, "up")))
                out_steps.append(("wheel zoom in (3 more)", lambda: wheel(3, "up")))
                out_steps.append(("double-click: whole map", lambda: dblclick()))
            return out_steps

        def wheel(n, button):           # as the mouse wheel does, over the middle of the first map
            """n wheel notches over the middle of the first map, as the mouse would send them."""
            from matplotlib.backend_bases import MouseEvent
            h = f.heatmap
            ax = h.fig.axes[0]
            x, y = ax.bbox.x0 + ax.bbox.width * 0.45, ax.bbox.y0 + ax.bbox.height * 0.5
            for _ in range(n):
                h.canvas.callbacks.process("scroll_event", MouseEvent("scroll_event", h.canvas, x, y, button=button,
                                                                      step=1))

        def dblclick():
            """A double click in the middle of the first map (back to the whole map)."""
            from matplotlib.backend_bases import MouseEvent
            h = f.heatmap
            ax = h.fig.axes[0]
            x, y = ax.bbox.x0 + ax.bbox.width / 2, ax.bbox.y0 + ax.bbox.height / 2
            h.canvas.callbacks.process("button_press_event", MouseEvent("button_press_event", h.canvas, x, y,
                                                                        button=1, dblclick=True))
        steps.append(("(heatmap views)", views))
    long_status = ("a status line long enough to wrap onto a second line, as a job's progress messages do: "
                   "/CPU/SPI_SCK: 1.234 GHz, board-wide model (34 000 nodes), solve 12 of at most 40, about 6:59 "
                   "left of the whole Simulate, the window laid out again when the bar grows and when it shrinks")
    # every tab before and after the status bar changes height: a tab shown the first time after the window was
    # laid out again pays for its own layout then (wx lays a hidden notebook page out when it is shown)
    for k, label in enumerate(tab_labels):
        steps.append((f"tab {label}", lambda k=k: f.notebook.SetSelection(k)))
    steps.append(("status bar grows (two lines)", lambda: f.SetStatusText(long_status)))
    steps.append(("status bar shrinks (one line)", lambda: f.SetStatusText("ready")))
    for k, label in enumerate(tab_labels):
        steps.append((f"tab {label} (after the status bar)", lambda k=k: f.notebook.SetSelection(k)))
    steps.append(("Save settings", lambda: f.on_save()))

    dog = Watchdog(args.threshold_ms / 1000, cpu=args.cpu)
    # when the window's garbage settle (gil.settle_garbage, after new results, once the user is idle) ran, how long
    from groundtruth.ui import gil
    settles: list[tuple[float, float]] = []
    real_settle = gil.settle_garbage

    def timed_settle():
        """gil.settle_garbage, timed for the report."""
        t0 = time.perf_counter()
        real_settle()
        settles.append((t0, time.perf_counter() - t0))
    gil.settle_garbage = timed_settle
    ticks: list[float] = []
    marks: list[tuple[float, str]] = [(time.perf_counter(), "load + auto-detect")]
    state = {"idle": None, "k": 0}
    # the probe is the user: its last step is the last input (ui/idle.py reads the system's input otherwise, which
    # the probe's synthetic events never touch, so the window's idle-time work would run during every step)
    from groundtruth.ui import idle
    idle.input_idle_s = lambda: time.perf_counter() - marks[-1][0]

    def tick():
        """Every 20 ms: record the tick (a late one is a stall); once the window has been idle for 0.4 s, run the
        next step, and close the window after the last."""
        now = time.perf_counter()
        ticks.append(now)
        dog.last_tick = now
        if f.busy() or f.heatmap.rendering() or now < state.get("hold", 0.0):
            state["idle"] = None
            return
        if state["idle"] is None:
            state["idle"] = now
            return
        if now - state["idle"] < 0.4:
            return
        state["idle"] = None
        if state["k"] == len(steps):
            # closed as the user closes it (MainFrame.on_close, inside the loop), so the process ends the way the
            # plugin's does; the loop ends with the last window. (Destroying it after the loop crashed Python's
            # shutdown now and then: "Garbage-collecting", exit code 139.)
            state["status"] = f.status_text()
            timer.Stop()
            f.Close()
            return
        name, fn = steps[state["k"]]
        state["k"] += 1
        if name == "(heatmap views)":
            steps[state["k"]:state["k"]] = fn()
            return
        marks.append((time.perf_counter(), name))
        fn()

    timer = Ticker(tick)
    import gc
    gcs = GcLog()
    gc.callbacks.append(gcs)
    dog.start()
    timer.Start(20)
    app.MainLoop()
    timer.Stop()
    dog.stop.set()
    dog.join(2.0)           # not left running into Python's shutdown (a daemon thread there crashed it, exit 139)
    marks.append((time.perf_counter(), "end"))

    def step_at(t):
        """The step that was running at time t."""
        name = marks[0][1]
        for tm, nm in marks:
            if tm <= t:
                name = nm
        return name
    gaps = [(a, b - a) for a, b in zip(ticks, ticks[1:]) if b - a > args.threshold_ms / 1000]
    print(f"\nstatus: {state.get('status', '(the window closed before the last step)')}")
    print(f"{len(ticks)} ticks; {len(gaps)} stalls over {args.threshold_ms:.0f} ms\n")
    per_step = collections.defaultdict(list)
    for t, d in gaps:
        per_step[step_at(t)].append((t, d))
    for (t0, name), (t1, _) in zip(marks, marks[1:]):
        worst = max((d for _, d in per_step.get(name, [])), default=0.0)
        print(f"{name:55s} {t1 - t0:7.2f} s   worst stall {worst * 1e3:6.0f} ms")
    print("\nstalls with the GUI thread's stack:")
    for t, d in sorted(gaps, key=lambda g: -g[1])[:15]:
        inside = [s for ts, s in dog.samples if t <= ts <= t + d]
        common = collections.Counter(inside).most_common(1)
        print(f"\n  {d * 1e3:6.0f} ms during '{step_at(t)}'")
        for line in (common[0][0] if common else ("(no sample)",)):
            print(f"      {line}")
        others = collections.Counter(s for ts, s in dog.other_samples if t <= ts <= t + d).most_common(1)
        for line in (others[0][0] if others else ()):
            print(f"      other thread: {line}")
        for t0, dur, gen, _ in gcs.within(t, d):
            print(f"      garbage collection: generation {gen}, {dur * 1e3:.0f} ms")
        for line in _cpu_during(dog, t, d):
            print(f"      {line}")
        span = [x for x in dog.faults if t - 0.05 <= x[0] <= t + d + 0.05]
        if len(span) > 1:
            print(f"      page faults meanwhile: {span[-1][1] - span[0][1]}; working set {span[0][2]:.0f} -> "
                  f"{span[-1][2]:.0f} MB")
    print("\ngarbage settles (gil.settle_garbage): " + (", ".join(
        f"{d * 1e3:.0f} ms during '{step_at(t0)}'" for t0, d in settles) or "none"))
    big = sorted((r for r in gcs.runs if r[1] >= 0.05), key=lambda r: -r[1])
    print(f"\ngarbage collections over 50 ms: {len(big)}"
          + "".join(f"\n  {d * 1e3:6.0f} ms, generation {g}, during '{step_at(t0)}'" for t0, d, g, _ in big[:15]))
    gc.callbacks.remove(gcs)
    # the steps' closures hold wx windows in reference cycles: collected now, while the app still exists, not by
    # Python's last collection at exit after it (an access violation there, "Garbage-collecting", exit code 139,
    # in most probe runs; the plugin's own exit, checked with gui_main 6 of 6 times, was clean)
    steps.clear()
    gc.collect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
