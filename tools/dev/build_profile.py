"""How long the plugin's main window blocks the GUI thread while it opens: the constructor, then each tab's build
step (one per event-loop step, built in a hidden holder; see MainFrame._build_pages). File mode on a synthetic
board, no KiCad. A step near 0.5 s reads as a stall; the test suite asserts < 0.35 s.

    .venv/Scripts/python tools/dev/build_profile.py [--repeat 3]
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]


def main() -> int:
    """Open the main window on a small generated board --repeat times, timing every _build_pages call (one tab, or
    one build step of a tab) by wrapping it, and print each step's time."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    import wx
    from boardgen import microstrip_board
    from groundtruth.ui import main_frame
    app = wx.App(False)
    path = microstrip_board().write(Path(tempfile.mkdtemp()) / "probe.kicad_pcb")
    steps: list = []
    orig = main_frame.MainFrame._build_pages

    def timed(self):
        """_build_pages with a stopwatch: the step's name (the tab, and its build step), then its time."""
        building = getattr(self, "_building", None)     # a panel's later build steps (BridgesPanel's form)
        if building is not None:
            name = f"{building[1]} (step {4 - len(building[2].build_steps)})"
        else:
            name = self._pages[0][1] if self._pages else "?"
        t = time.perf_counter()
        orig(self)
        steps.append((name, time.perf_counter() - t))
    main_frame.MainFrame._build_pages = timed
    for k in range(args.repeat):
        steps.clear()
        t = time.perf_counter()
        f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
        ctor = time.perf_counter() - t
        f.Show()
        deadline = time.perf_counter() + 30
        check = wx.Timer()                     # the build steps are timers: they fire in MainLoop, not wx.Yield

        def done(_, f=f, deadline=deadline):
            """Timer: end the event loop once the tabs are built and the board is read (or after 30 s)."""
            if not (f.busy() or f.board is None) or time.perf_counter() > deadline:   # tabs built, board read
                app.ExitMainLoop()
        check.Bind(wx.EVT_TIMER, done)
        check.Start(20)
        app.MainLoop()
        check.Stop()
        print(f"run {k + 1}: constructor {ctor * 1000:.0f} ms; " +
              ", ".join(f"{n} {s * 1000:.0f}" for n, s in steps) + " ms")
        f.Destroy()
        wx.Yield()
    app.Destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
