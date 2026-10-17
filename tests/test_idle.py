"""ui/idle.py (waiting for the user to leave the mouse and keyboard alone) and gil.settle_garbage (one collection,
then the survivors frozen): no window needed."""
import gc
import math
import sys
import time
import weakref

import pytest

from groundtruth.ui import gil, idle


@pytest.mark.skipif(sys.platform not in ("win32", "darwin"), reason="the system says only on Windows and macOS")
def test_the_input_age_comes_from_the_system():
    """A finite, non-negative age that grows while nobody touches the machine (or restarts at 0 if they do)."""
    a = idle.input_idle_s()
    assert math.isfinite(a) and a >= 0.0


def test_waiting_ends_when_quiet_or_stopped(monkeypatch):
    """wait_until_quiet: at once when the input is old enough; after the input gets old enough (polled, sleeping
    meanwhile); False as soon as stop() says so."""
    assert idle.wait_until_quiet(lambda: True, 0.0) is False          # stopped first
    ages = iter([0.2, 0.6, 1.5])
    monkeypatch.setattr(idle, "input_idle_s", lambda: next(ages))
    monkeypatch.setattr(idle, "POLL_S", 0.01)
    t = time.perf_counter()
    assert idle.wait_until_quiet(lambda: False, 1.0) is True
    assert time.perf_counter() - t < 1.0
    monkeypatch.setattr(idle, "input_idle_s", lambda: 0.0)
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 3
    assert idle.wait_until_quiet(stop, 10.0) is False and n[0] == 4


def test_settling_frees_the_frozen_cycles_and_freezes_the_living():
    """A reference cycle that was alive at a freeze and dies afterwards is not collected while frozen; the next
    settle (unfreeze, collect, freeze) frees it, and what is still alive is frozen."""
    class Node:
        pass
    gc.collect()
    try:
        a, b = Node(), Node()
        a.other, b.other = b, a
        alive = Node()
        gone = weakref.ref(a)
        gc.freeze()
        del a, b
        gc.collect()
        assert gone() is not None                     # frozen: the collector does not walk it
        gil.settle_garbage()
        assert gone() is None                         # the settle's collection freed it
        assert gc.get_freeze_count() > 0 and alive is not None
    finally:
        gc.unfreeze()                                 # the rest of the test process collects as usual
