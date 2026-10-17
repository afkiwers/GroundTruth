"""The background worker behind the plugin's settings saves and heatmap renders: only the newest request runs
when several wait, flush waits for the work handed over, and a failing task is reported without stopping it."""
import threading
import time

from groundtruth.ui.background import LatestOnly


def test_newest_request_wins_and_flush_waits():
    ran, gate = [], threading.Event()
    w = LatestOnly("test")
    w.submit(lambda: (gate.wait(5), ran.append(1)))     # runs, and blocks until the gate opens
    time.sleep(0.05)
    w.submit(lambda: ran.append(2))                     # waits ...
    w.submit(lambda: ran.append(3))                     # ... and is replaced by this one
    assert w.busy()
    assert not w.flush(timeout=0.1)                     # still blocked
    gate.set()
    assert w.flush(timeout=5)
    assert ran == [1, 3] and not w.busy()


def test_a_failing_task_is_reported_and_the_worker_goes_on():
    errors, ran = [], []
    w = LatestOnly("test", on_error=errors.append)
    w.submit(lambda: 1 / 0)
    assert w.flush(5)
    w.submit(lambda: ran.append("next"))
    assert w.flush(5)
    assert [type(e) for e in errors] == [ZeroDivisionError] and ran == ["next"]


def test_running_work_finishes_before_python_exits(monkeypatch):
    """A render still running in a daemon worker when Python shut down crashed the process (access violation in
    matplotlib): the exit hook waits for it, but no longer than EXIT_WAIT_S; a request still waiting is dropped
    (it would only fail against the destroyed app)."""
    from groundtruth.ui import background
    done = []
    w = LatestOnly("test")
    w.submit(lambda: (time.sleep(0.3), done.append(1)))
    time.sleep(0.05)
    w.submit(lambda: done.append(2))                   # waiting behind the running one
    background._finish_at_exit()
    assert done == [1] and not w.busy()
    gate = threading.Event()
    w.submit(lambda: gate.wait(10))                    # stuck: the hook gives up after its wait
    monkeypatch.setattr(background, "EXIT_WAIT_S", 0.2)
    t = time.monotonic()
    background._finish_at_exit()
    assert time.monotonic() - t < 2.0
    gate.set()
