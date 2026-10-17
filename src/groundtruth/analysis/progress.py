"""Progress of a job made of phases whose sizes become known one after the other (the fix search: how many
candidates, checks and re-simulated routes there are is only known once the phase before has run).

The user: "it's not clear from the loading bar how far it has got at any moment and how much more is to do". So
each phase gets the share of what is left of the bar that its estimated cost (in solves) has against the estimated
cost of everything still to come, and reports its own fraction inside that share. Later phases re-estimate with
what is known by then, which only re-divides the part of the bar not yet used: the bar never goes back.

A phase's steps (`with phases.step("evaluating …"):`) move the bar when they finish, not when they start: parallel
steps all start at once, and the bar then stood still for the whole batch (37 s on the real board)."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Optional

Progress = Callable[[str, float], None]


class Phases:
    """A progress bar over [lo, hi] split into phases as they come (see the module docstring)."""

    def __init__(self, progress: Optional[Progress], lo: float = 0.0, hi: float = 1.0):
        """Report through `progress` (None: silent), on the part [lo, hi] of the bar."""
        self.progress = progress
        self.at = self.end = lo
        self.hi = hi
        self._lock = threading.RLock()     # reports are made while holding it (in order, never lower)
        self._started = self._done = self._total = 0
        self._last = lo

    def start(self, cost: float, rest: float = 0.0, steps: int = 0) -> "Phases":
        """A phase of estimated `cost` followed by an estimated `rest`; `steps` counts its parts for step()."""
        self.at = self.end
        whole = cost + rest
        # this phase's share of what is left of the bar: its cost against everything still to come
        self.end = self.at + (self.hi - self.at) * (cost / whole if whole > 0 else 1.0)
        self.expect(steps)
        return self

    def expect(self, steps: int):
        """The current phase's step count, once it is known (after start)."""
        with self._lock:
            self._started = self._done = 0
            self._total = steps

    def report(self, msg: str, frac: float = 0.0):
        """Never lower than the last report: parallel steps may report out of order."""
        if self.progress:
            with self._lock:
                self._last = max(self._last, self.at + (self.end - self.at) * min(max(frac, 0.0), 1.0))
                self.progress(msg, self._last)

    @contextmanager
    def step(self, msg: str):
        """One of the phase's steps: "msg (k of n)" while it runs, the bar at the finished steps' fraction.
        Thread-safe (the steps run in parallel)."""
        with self._lock:
            self._started += 1
            n = max(self._total, self._started, 1)
            text = f"{msg} ({self._started} of {n})"
            self.report(text, self._done / n)
        try:
            yield
        finally:
            with self._lock:
                self._done += 1
                self.report(text, self._done / max(self._total, self._done, 1))

    def nested(self, prefix: str = "") -> Progress:
        """A progress callback for a job inside the phase (a simulation): its fraction fills the phase."""
        return lambda msg, frac: self.report(prefix + msg, frac)
