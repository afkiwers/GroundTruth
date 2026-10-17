"""The status line of a running job: what it does, how far it is, how long it has run and about how long is left.

The user, about the fix search: "it's not clear from the loading bar how far it has got at any moment and how much
more is to do. This is the case in general when something loads." So every job's line reads
"<message> · 37 % · 1:12 · about 2 min left", and the window refreshes it every second (the elapsed time keeps
counting during a long single step, so a busy job never looks stuck). No wx here: MainFrame owns the timer."""
from __future__ import annotations

import time
from typing import Callable, Optional

MIN_FRAC_FOR_ESTIMATE = 0.03       # an estimate from the first few percent is mostly noise
MIN_SECONDS_FOR_ESTIMATE = 5.0
SMOOTHING = 0.3                    # weight of a new estimate against the running one (it would jump otherwise)


def release_failure(exc: BaseException) -> None:
    """Free what a failed job still holds: the locals of every finished frame in its exception chain (causes,
    contexts, groups). A failed Simulate's traceback runs through the solver's frames, and run_parallel's re-raise
    makes a reference cycle (the exception sits in a future that a frame of its own traceback holds), so its models
    and factorisations (GBs on a big board) stayed until Python's cycle collector happened to run: the user saw
    5.4 GB kept after a failed Simulate. Call after formatting the traceback (the message keeps its text)."""
    import traceback
    seen, todo = set(), [exc]
    while todo:
        e = todo.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        traceback.clear_frames(e.__traceback__)         # skips frames still running (the job's own)
        todo += [e.__cause__, e.__context__, *getattr(e, "exceptions", ())]


def clock(seconds: float) -> str:
    """0:07, 1:12, 1:02:03."""
    s = int(max(seconds, 0) + 0.5)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def about(seconds: float) -> str:
    """'about 40 s', 'about 3 min', 'about 1 h 10 min': rounded, as an estimate should be."""
    if seconds < 60:
        return f"about {max(5, int(round(seconds / 5.0)) * 5)} s"
    if seconds < 3600:
        return f"about {int(round(seconds / 60.0))} min"
    h, m = divmod(int(round(seconds / 60.0)), 60)
    return f"about {h} h {m} min" if m else f"about {h} h"


class ProgressClock:
    """Holds a job's latest message and fraction and its start time. `line()` is the status text."""

    def __init__(self, now: Callable[[], float] = time.monotonic):
        """A stopped clock; `now` is the time source (tests pass a fake one)."""
        self._now = now
        self.t0: Optional[float] = None
        self.msg = ""
        self.frac = 0.0
        self._left: Optional[float] = None        # smoothed seconds left, as of _left_at
        self._left_at = 0.0

    def start(self, msg: str):
        """A job starts: its first message, nothing done, no estimate yet."""
        self.t0, self.msg, self.frac, self._left = self._now(), msg, 0.0, None

    def stop(self):
        """The job ended."""
        self.t0 = None

    @property
    def running(self) -> bool:
        """A job is running."""
        return self.t0 is not None

    def elapsed(self) -> float:
        """Seconds since the job started (0 when none runs)."""
        return self._now() - self.t0 if self.t0 is not None else 0.0

    def update(self, msg: str, frac: float):
        """A progress report. The fraction never goes back (a job that re-plans would otherwise jump around)."""
        self.msg = msg
        frac = min(max(float(frac), 0.0), 1.0)
        if frac <= self.frac:
            return
        self.frac = frac
        t = self.elapsed()
        if frac >= MIN_FRAC_FOR_ESTIMATE and t >= MIN_SECONDS_FOR_ESTIMATE:
            new = t * (1.0 - frac) / frac
            if self._left is None:
                self._left = new
            else:                                 # the running estimate, counted down to now, then blended
                old = max(self._left - (t - self._left_at), 0.0)
                self._left = (1 - SMOOTHING) * old + SMOOTHING * new
            self._left_at = t

    def left(self) -> Optional[float]:
        """Seconds left (counting down between reports), or None while too early to say."""
        if self._left is None or self.t0 is None:
            return None
        return max(self._left - (self.elapsed() - self._left_at), 0.0)

    def line(self) -> str:
        """The status line: the message, % done, the time so far and (once it can be told) the time left."""
        parts = [self.msg] if self.msg else []
        if self.t0 is not None:
            parts.append(f"{self.frac * 100:.0f} %")
            parts.append(clock(self.elapsed()))
            left = self.left()
            if left is not None and self.frac < 1.0:
                parts.append(f"{about(left)} left" if left >= 1.0 else "almost done")
        return "  ·  ".join(parts)
