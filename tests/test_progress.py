"""The status line of a running job (ui/progress.py): percent, elapsed time, a smoothed estimate of what is left."""
from groundtruth.ui.progress import ProgressClock, about, clock


class FakeTime:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_the_line_shows_percent_elapsed_and_an_estimate_once_it_can():
    now = FakeTime()
    p = ProgressClock(now)
    p.start("searching for fixes…")
    assert p.line() == "searching for fixes…  ·  0 %  ·  0:00"
    now.t += 2
    p.update("SPI_SCK: candidates", 0.10)
    assert "left" not in p.line()                          # 2 s in: too early to say
    now.t += 8                                             # 10 s for 25 %: about 30 s left
    p.update("SPI_SCK: evaluating S3", 0.25)
    assert p.line() == "SPI_SCK: evaluating S3  ·  25 %  ·  0:10  ·  about 30 s left"
    now.t += 10                                            # no report for 10 s: it counts down, the clock runs on
    assert p.left() == 20.0 and "0:20" in p.line()
    p.update("going back", 0.2)                            # the bar never moves backwards
    assert p.frac == 0.25 and p.msg == "going back"
    now.t += 10                                            # 30 s for 50 %: the estimate blends towards 30 s
    p.update("halfway", 0.5)
    assert 10.0 < p.left() < 30.0
    p.stop()
    assert p.line() == "halfway" and p.left() is None


def test_clock_and_about_round_like_people_do():
    assert clock(7) == "0:07" and clock(72) == "1:12" and clock(3723) == "1:02:03"
    assert about(3) == "about 5 s" and about(42) == "about 40 s" and about(150) == "about 2 min"
    assert about(4200) == "about 1 h 10 min" and about(7200) == "about 2 h"


def test_release_failure_frees_what_a_failed_jobs_frames_hold_without_a_gc():
    """A worker's exception re-raised through a future (as simulate.run_parallel does) is a reference cycle: the
    frames, and the models they hold, stayed until Python's cycle collector ran. release_failure clears the frames,
    so they go at once."""
    import gc
    import weakref
    from concurrent.futures import ThreadPoolExecutor
    from groundtruth.ui.progress import release_failure

    class Model:
        pass
    refs = []

    def work():
        model = Model()                 # what a failed solve's frame holds
        refs.append(weakref.ref(model))
        raise RuntimeError("the solve failed")

    def run():
        with ThreadPoolExecutor(1) as pool:
            futures = [pool.submit(work)]
            return [f.result() for f in futures]
    gc.disable()
    try:
        for release in (False, True):
            refs.clear()
            try:
                run()
            except RuntimeError as e:
                err = e
            if release:
                release_failure(err)
            del err
            assert (refs[0]() is None) == release
            gc.collect()
    finally:
        gc.enable()
