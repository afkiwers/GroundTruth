"""Python's GIL switch interval for the GUI processes (measured on refboard; ARCHITECTURE "Plugin").

A GUI thread that needs the GIL while a job's Python code runs waits one switch interval, and on Windows a 1-5 ms
wait lasts a timer tick (~15.6 ms). wxPython needs the GIL for every Python handler and every virtual call into a
Python-derived window, so filling the Parts grid took 7.9 s instead of 14 ms beside a busy thread (KiCad's
Python 3.11). Below 1 ms the wait rounds to zero and the GUI thread gets the GIL at once (71 ms); the 603 ms stall
while the board is read disappeared. But then a waiting thread spins instead of sleeping, and during a
simulation the solver's threads wait for the GIL too: their spinning made the window's stalls more frequent
(12 instead of 3 over 150 ms in a 390 s run). So solver jobs run with Python's default interval."""
from __future__ import annotations

import sys

GUI_SWITCH_INTERVAL = 0.0005        # s: the GUI thread gets the GIL at once
SOLVER_SWITCH_INTERVAL = 0.005      # s: Python's default, for jobs with many solver threads


def responsive_gui():
    """Call once in a GUI process before its window opens."""
    sys.setswitchinterval(GUI_SWITCH_INTERVAL)


class SolverInterval:
    """For the duration of a solver job: Python's default interval; afterwards the one that was set before
    (a process that never called responsive_gui keeps its own)."""

    def __init__(self):
        """Not entered."""
        self._saved = None

    def enter(self):
        """A solver job starts: at least Python's default interval (remembering the GUI's)."""
        if self._saved is None:
            self._saved = sys.getswitchinterval()
            sys.setswitchinterval(max(self._saved, SOLVER_SWITCH_INTERVAL))

    def leave(self):
        """The job ended: the interval that was set before."""
        if self._saved is not None:
            sys.setswitchinterval(self._saved)
            self._saved = None


_frozen = False


def freeze_long_lived():
    """Once per process, when the window has its tabs (before the board is read): every object alive now (the
    modules: numpy, scipy, matplotlib, shapely, wx; ~110 000 objects; and the window's) goes into Python's
    permanent generation, which garbage collection no longer walks. A full collection holds the GIL from start to
    end, whichever thread triggers it, and walked all of them every time: 170-410 ms on refboard's results,
    the window frozen meanwhile. They live as long as the process, so nothing that would be collected is kept. Only
    once: a test process opens many windows, and the closed ones' garbage must stay collectable."""
    global _frozen
    if _frozen:
        return
    _frozen = True
    import gc
    gc.freeze()


def settle_garbage():
    """After a Simulate's results are shown, once the user is idle (MainFrame._settle_when_idle): one full
    collection, then everything alive goes into the permanent generation. A results set is hundreds of thousands
    of objects (models, maps, views): every automatic full collection walked them again, 150-360 ms each with the
    window frozen, about eight times while the views were looked through (gui_probe.py on refboard).
    gc.freeze only keeps the collector from walking them: anything that loses its last reference is still freed at
    once. Only reference cycles that die later wait for the next settle, which first moves everything back
    (gc.unfreeze) and frees the replaced results' cycles with this one collection, at a moment the user is not
    using the window. A test process (many windows) collects its closed windows' cycles the same way."""
    global _frozen
    import gc
    _frozen = True                  # freeze_long_lived's freeze is part of this one
    gc.unfreeze()
    gc.collect()
    gc.freeze()
