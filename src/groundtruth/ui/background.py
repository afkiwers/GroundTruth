"""Background threads for GUI work. LatestOnly: only the newest request matters (render the view now selected, save
the settings as they are now): a request still waiting when a newer one arrives is dropped. InOrder: every request
runs, in the order given (the simulation history's records, reports and deletions).

The threads are daemons, so a stuck one never keeps the process alive; but one still inside matplotlib when Python
shuts down crashed the process with an access violation (gui_probe, exit code 139, a worker thread in
matplotlib's font_manager). So at exit the workers get up to EXIT_WAIT_S to finish the task they are running;
waiting requests are dropped (a render queued after the window closed only failed against the destroyed app), and
`stopping` is set, which a long task (a report page) checks between its steps to end early.
Settings saves are flushed by the window when it closes (MainFrame.on_close)."""
from __future__ import annotations

import atexit
import collections
import threading
import time
import weakref
from typing import Callable, Optional

EXIT_WAIT_S = 5.0
_WORKERS: "weakref.WeakSet[LatestOnly]" = weakref.WeakSet()


def _finish_at_exit():
    """At exit: give every worker's running task the time left of EXIT_WAIT_S to finish (see the module)."""
    deadline = time.monotonic() + EXIT_WAIT_S
    for w in list(_WORKERS):
        w.finish(max(deadline - time.monotonic(), 0.0))


atexit.register(_finish_at_exit)


class LatestOnly:
    """A background thread running submitted tasks one at a time; a task still waiting when another is submitted
    is dropped (only the newest request matters). The queue and the running flag are guarded by one condition
    variable, which flush() waits on."""

    def __init__(self, name: str, on_error: Optional[Callable[[Exception], None]] = None):
        """Start the thread `name`; on_error(e) is called (in the thread) when a task raises."""
        self.on_error = on_error
        self.stopping = threading.Event()
        self._queue: "collections.deque[Callable[[], None]]" = collections.deque()
        self._running = False
        self._cond = threading.Condition()
        threading.Thread(target=self._run, name=name, daemon=True).start()
        _WORKERS.add(self)

    def submit(self, task: Callable[[], None]):
        """Run `task` next; a task still waiting is dropped."""
        with self._cond:
            self._queue.clear()
            self._queue.append(task)
            self._cond.notify_all()

    def busy(self) -> bool:
        """A task is running or waiting."""
        with self._cond:
            return bool(self._queue) or self._running

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until every submitted task has run. False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._running:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    def finish(self, timeout: float) -> bool:
        """Drop the waiting requests, ask the running one to stop and wait for it (at exit). False on timeout."""
        with self._cond:
            self._queue.clear()
        self.stopping.set()
        return self.flush(timeout)

    def _run(self):
        """The thread: wait for a task, run it (reporting a failure, never ending the thread), repeat."""
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                task = self._queue.popleft()
                self._running = True
            try:
                task()
            except Exception as e:          # report, keep the thread alive for the next request
                if self.on_error:
                    try:
                        self.on_error(e)
                    except Exception:       # noqa: BLE001 - e.g. wx.CallAfter once the app is gone
                        pass
            finally:
                with self._cond:
                    self._running = False
                    self._cond.notify_all()


class InOrder(LatestOnly):
    """Every request runs, one after the other, in the order submitted."""

    def submit(self, task: Callable[[], None]):
        """Queue `task` after the others."""
        with self._cond:
            self._queue.append(task)
            self._cond.notify_all()
