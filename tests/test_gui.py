"""The plugin window in file mode, driven through a real event loop: every slow step runs as a job off the GUI
thread (the GUI keeps processing events meanwhile), Cancel stops a running analysis, and the fix search reuses
the last simulation. Skipped without wxPython or a display."""
import gc
import os
import sys
import time

import pytest

wx = pytest.importorskip("wx")

from boardgen import discrete_buck_board, hot_loop_board, layer_change_board, microstrip_board  # noqa: E402
from conftest import make  # noqa: E402


@pytest.fixture(scope="module")
def app():
    try:
        a = wx.App(False)
    except Exception as e:   # no display (CI)
        pytest.skip(f"no display for wx: {e}")
    yield a
    a.Destroy()


def close_window(w):
    """Destroy a top-level window now. wx deletes it only at the next idle time of an event loop; once a test's
    loop has ended there is none, the window stays on screen while the other tests run, and Windows marks it
    "Not Responding". One Yield runs the idle processing that deletes it."""
    w.Destroy()
    wx.Yield()
    assert not w, "the window was not deleted"


@pytest.fixture
def frame(app, tmp_path, monkeypatch):
    from groundtruth.ui import main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    _, _, path = make(tmp_path, layer_change_board(None), "gui", solve_points=2)
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    yield f
    close_window(f)


def run_steps(app, f, steps, timeout=600):
    """Run the event loop. Each step is called once the window is idle (no job for 0.3 s); a step may return
    a number of seconds after which the next step runs even if a job is still busy (to test Cancel).
    Returns [(time, busy)] sampled by a 20 ms GUI timer."""
    ticks = []
    state = {"k": 0, "idle_since": None, "due": None, "t0": time.perf_counter()}
    steps = list(steps)

    def tick(_):
        now = time.perf_counter()
        busy = f.busy() or f.results_panel.rendering()        # heatmaps render in the background too (busy() covers
                                                        # the tabs being built, so f.results_panel exists after it)
        ticks.append((now, busy))
        if now - state["t0"] > timeout:
            app.ExitMainLoop()
            return
        due = state["due"]
        if busy and (due is None or now < due):
            state["idle_since"] = None
            return
        if not busy:
            if state["idle_since"] is None:
                state["idle_since"] = now
                return
            if now - state["idle_since"] < 0.3:
                return
        state["idle_since"] = state["due"] = None
        if state["k"] == len(steps):
            app.ExitMainLoop()
            return
        delay = steps[state["k"]]()
        state["k"] += 1
        if delay:
            state["due"] = now + delay

    timer = wx.Timer(f)
    f.Bind(wx.EVT_TIMER, tick, timer)
    timer.Start(20)
    app.MainLoop()
    timer.Stop()
    assert state["k"] == len(steps), "steps did not finish in time"
    return ticks


def longest_busy_gap(ticks):
    """The longest time between two consecutive GUI timer ticks while a job ran (or the window built its tabs).
    Consecutive ticks only: busy ticks either side of an idle pause are not a stall."""
    return max((b - a for (a, ab), (b, bb) in zip(ticks, ticks[1:]) if ab or bb), default=0.0)


def wait_rendered(f, timeout=60):
    """Let the event loop run until the heatmap view asked for last is on screen."""
    deadline = time.perf_counter() + timeout
    while f.results_panel.rendering() and time.perf_counter() < deadline:
        wx.Yield()
        time.sleep(0.01)
    assert not f.results_panel.rendering(), "the heatmap was not rendered in time"


def test_jobs_run_off_the_gui_thread(app, frame):
    f = frame
    ticks = run_steps(app, f, [lambda: f.run(False) and None, lambda: f.run(True) and None])
    assert f.board is not None and f.settings is not None, "the board was not loaded by the background job"
    assert "layer_change_stitch" in {i.check for i in f.issues}, [i.check for i in f.issues]
    assert f.routes, "the simulation produced no routes"
    assert f.notebook.GetSelection() == f.notebook.FindPage(f.results_panel), f.notebook.GetSelection()
    busy = sum(b for _, b in ticks)
    assert busy > 20, f"only {busy} GUI timer ticks while jobs ran"        # the GUI timer kept firing during jobs
    # Work on the GUI thread would stall it for seconds. In a full test run, a worker inside a C call that holds the
    # GIL occasionally delays the 20 ms timer by 0.3 s (seen 3 times in ~10 runs, never in isolation: 46 ms).
    assert longest_busy_gap(ticks) < 0.5, f"GUI thread stalled {longest_busy_gap(ticks):.2f}s during a job"


def test_simulate_reads_a_board_changed_since_it_was_loaded(app, tmp_path, monkeypatch):
    """ISSUE-19 (VALIDATION_AUDIT.md round 5): the window simulates the board as it is now, not as it was read. A
    1 A DC load through a 40 mm, 0.4 mm track is simulated; the board file is then saved with the track at 0.1 mm
    (as KiCad would, the window not told); the next Simulate finds the board changed (Board.fingerprint), reads it
    again with its settings and simulates the narrow track: the supply drop grows about 4 times (the pads' share
    stays). Before: the old track's drop again, until Reload board."""
    from boardgen import BoardGen
    from groundtruth.config.settings import DcLoad, DcLoadSpec
    from groundtruth.ui import main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)

    def board(width):
        """Two 1 x 1 mm PWR pads joined by a 40 mm PWR track `width` wide, each part with a GND pad (the settings'
        reference net), 2 layers."""
        g = BoardGen(layers=2, size=(56.0, 20.0))
        for ref, x in (("U1", 6.0), ("U2", 46.0)):
            g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, 10.0),
                        [{"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)},
                         {"number": 2, "at": (0, 3.0), "net": "GND", "size": (1.0, 1.0)}])
        g.track((6.0, 10.0), (46.0, 10.0), "PWR", "F.Cu", width=width)
        return g

    _, sc, path = make(tmp_path, board(0.4), "fresh", nets=())
    sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 1.0, "")], "")])
    sc.save()
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    drops, statuses = [], []

    def drop():
        """The rail's supply drop (mV) from the last Simulate's views."""
        rail = next(r for r in f.routes if getattr(r, "kind", "") == "dcload")
        drops.append(rail.loads[0]["supply_mv"])

    try:
        run_steps(app, f, [lambda: f.run(True) and None, drop,
                           lambda: board(0.1).write(path) and None,       # saved in KiCad meanwhile
                           lambda: f.run(True) and None, drop,
                           lambda: statuses.append(f.GetStatusBar().GetStatusText(0))])
        assert {t.width for t in f.board.tracks} == {0.1}, "the window still holds the old board"
        assert drops[1] / drops[0] == pytest.approx(4.0, rel=0.05), drops
    finally:
        close_window(f)


def test_results_of_an_earlier_board_or_settings_are_marked_and_not_heated(app, tmp_path, monkeypatch):
    """ISSUE-29 (VALIDATION_AUDIT.md round 6): the Results views keep the board and settings they were computed on,
    and Thermal and Current capacity heat those. After Reload board read a changed board (a track narrowed in
    KiCad), the old views were shown and Thermal ran on them as if they were the design's; a thermal setting edited
    after the Simulate (the ambient) was ignored by Thermal. Now: the summary line says OUT OF DATE (the board
    changed), Thermal reads the board first and refuses stale views with a message; after a new Simulate the mark is
    gone; an edited load current marks the views (the settings changed); an edited thermal section does not, and
    Thermal takes the new ambient."""
    from boardgen import BoardGen
    from groundtruth.config.settings import DcLoad, DcLoadSpec, config_from_data
    from groundtruth.ui import main_frame
    boxes = []
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: boxes.append(a[0] if a else "") or wx.YES)

    def board(width):
        """Two PWR pads joined by a 40 mm PWR track `width` wide, each part with a GND pad, 2 layers."""
        g = BoardGen(layers=2, size=(56.0, 20.0))
        for ref, x in (("U1", 6.0), ("U2", 46.0)):
            g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, 10.0),
                        [{"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)},
                         {"number": 2, "at": (0, 3.0), "net": "GND", "size": (1.0, 1.0)}])
        g.track((6.0, 10.0), (46.0, 10.0), "PWR", "F.Cu", width=width)
        return g

    _, sc, path = make(tmp_path, board(0.4), "stale", nets=())
    sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 1.0, "")], "")])
    sc.save()
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    seen = {}

    def note(key):
        """Keep the stale reason, the summary line and the views' kinds now."""
        seen[key] = (f._stale_reason(), f.results_panel.metrics.GetLabel(), [r.kind for r in f.routes])

    def edit_current():
        """The DC load's current changed on its tab."""
        f.settings.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 2.0, "")], "")])
        f.on_config_change()

    def edit_ambient():
        """Only the thermal section changed (the settings file edited by hand, read again)."""
        f.settings.doc["thermal"] = {"ambient_c": 41.0}
        f.settings.config = config_from_data(f.settings.doc)

    try:
        run_steps(app, f, [lambda: f.run(True) and None, lambda: note("fresh"),
                           lambda: board(0.1).write(path) and None, lambda: f.on_reload() and None,
                           lambda: note("reloaded"), lambda: f.on_thermal() and None, lambda: note("thermal"),
                           lambda: f.run(True) and None, lambda: note("again"),
                           edit_ambient, lambda: note("ambient"),
                           lambda: seen.__setitem__("inputs", f._thermal_inputs("Thermal")),
                           edit_current, lambda: note("current")])
        assert seen["fresh"][0] == "" and "OUT OF DATE" not in seen["fresh"][1]
        assert seen["reloaded"][0] == "the board changed" and "OUT OF DATE" in seen["reloaded"][1]
        assert any("Run Simulate again" in b for b in boxes)
        assert "thermal" not in seen["thermal"][2]                       # not heated on the old design
        assert seen["again"][0] == "" and "OUT OF DATE" not in seen["again"][1]
        assert seen["ambient"][0] == ""                                   # a thermal setting needs no Simulate ...
        assert seen["inputs"][2].thermal.ambient_c == 41.0                # ... and Thermal takes it
        assert seen["current"][0] == "the settings changed" and "OUT OF DATE" in seen["current"][1]
    finally:
        close_window(f)


def test_cancel_stops_a_running_simulation(app, frame):
    f = frame
    status = []
    run_steps(app, f, [lambda: (f.run(True), 0.5)[1],               # cancel 0.5 s into the simulation
                       lambda: f.on_cancel(),
                       lambda: status.append(f.GetStatusBar().GetStatusText(0)),
                       lambda: f.run(False) and None])
    assert "cancelled" in status[0]
    assert not f.routes                                            # nothing half-finished is shown
    assert f.issues                                                # and the window is usable again


def test_suggest_reuses_the_last_simulation(app, frame):
    f = frame
    status = []
    run_steps(app, f, [lambda: f.run(True) and None,
                       lambda: (f.on_suggest(), status.append(f.GetStatusBar().GetStatusText(0)))[0]])
    assert "from the last simulation" in status[0]
    assert f.fixes.result is not None and f.fixes.result.selected


def test_models_too_big_for_the_free_memory_are_not_kept_and_suggest_still_works(app, frame, monkeypatch):
    """Simulate's models are kept for Suggest fixes only while they take less than a quarter of the free memory
    (kraft_flux_mini_2 kept ~2.3 GB). With almost none free they go: the maps and issues stay, and Suggest fixes
    simulates its own baseline."""
    from groundtruth.core import sysmem
    f = frame
    monkeypatch.setattr(sysmem, "available_bytes", lambda: 1000)
    status = []
    run_steps(app, f, [lambda: f.run(True) and None,
                       lambda: (f.on_suggest(), status.append(f.GetStatusBar().GetStatusText(0)))[0]])
    assert f.routes and f.issues
    assert "from the last simulation" not in status[0]
    assert f.fixes.result is not None and f.fixes.result.selected


@pytest.fixture
def conv_frame(app, tmp_path, monkeypatch):
    from groundtruth.ui import main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    _, _, path = make(tmp_path, hot_loop_board(20.0, connector=True), "conv", nets=())
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    yield f
    close_window(f)


def test_a_running_job_shows_how_far_it_is_and_how_long_it_has_run(app, conv_frame):
    """The user: "it's not clear from the loading bar how far it has got at any moment and how much more is to
    do". A job's status line carries its percent and elapsed time (and, later, about how long is left), refreshed
    every second by a timer that stops with the job."""
    import threading
    f = conv_frame
    gate = threading.Event()

    def work(progress):
        progress("step 1 of 4", 0.25)
        gate.wait(10)
        return 1
    run_steps(app, f, [lambda: None])                  # the tabs are built and the board is read
    assert f._start("testing…", work, lambda r: None, what="test")
    wait_for(app, lambda: "step 1 of 4" in f.GetStatusBar().GetStatusText(0))
    text = f.GetStatusBar().GetStatusText(0)
    assert "25 %" in text and "0:0" in text and f._clock_timer.IsRunning()
    assert f.gauge.GetValue() == 250
    gate.set()
    wait_for(app, lambda: not f.busy())
    assert not f._clock_timer.IsRunning() and not f._clock.running


def test_converters_form_explains_its_fields_scrolls_and_keeps_what_it_does_not_show(app, conv_frame):
    """Every field and its label explain themselves on hover; the form scrolls in a short window while the buttons
    under it stay in view; Q_oss goes through the form; Update keeps what the form does not show; the spread
    spectrum is on the form (since 2026-10-17): filled from the settings, kept by Update, "15%" typed in sets it, a
    text that is no number is refused."""
    from dataclasses import replace
    from types import SimpleNamespace as NS
    from groundtruth.ui import theme
    from groundtruth.ui.converters_panel import FIELD_TIPS
    from groundtruth.ui.widgets import FieldMark
    f = conv_frame
    got = {}

    def check():
        p = f.converters
        labels = {w.GetLabel(): w for w in p.form_win.GetChildren() if isinstance(w, wx.StaticText)
                  and not isinstance(w, FieldMark)}           # the ◆ / ! marks beside the part parameters
        assert set(FIELD_TIPS) <= set(labels)
        for label, w in labels.items():
            assert w.GetToolTipText() == FIELD_TIPS[label]
        assert "Q_oss·V" in p.q_oss.GetToolTipText() and "ringing" in p.c_oss.GetToolTipText()
        for b in (c for c in p.GetChildren() if isinstance(c, theme.Button)):
            assert b.GetParent() is p and b.GetToolTipText()             # outside the scrolled form
        # a short window: the form scrolls, the buttons stay inside the panel
        f.SetSize((1200, 520))
        f.notebook.SetSelection(f.notebook.FindPage(p))
        p.Layout()
        wx.Yield()
        assert p.form_win.GetVirtualSize().height > p.form_win.GetClientSize().height
        panel_h = p.GetClientSize().height
        for b in (c for c in p.GetChildren() if isinstance(c, theme.Button)):
            assert b.GetPosition().y + b.GetSize().height <= panel_h
        # add a stage with Q_oss, give it spread spectrum in the settings, update it from the form
        for ctrl, v in ((p.name, "buck"), (p.rail, "VIN"), (p.ground, "GND"), (p.integrated, "U1"),
                        (p.f_sw, "750kHz"), (p.i_out, "5"), (p.v_in, "52.8"), (p.rise, "2ns"), (p.c_oss, "482p"),
                        (p.q_oss, "38n"), (p.inp, "J1.1, J1.2")):
            ctrl.SetValue(v)
        p.on_add(None)
        p.converters[0] = replace(p.converters[0], spread=0.2, spread_mode="triangle", spread_rate=10e3)
        p._commit()
        p.list.Select(0)
        p.on_select(NS(GetIndex=lambda: 0))
        assert p.q_oss.GetValue() == "38nC"
        assert (p.spread.GetValue(), p.spread_mode.GetStringSelection(), p.spread_rate.GetValue()) == \
            ("20", "triangle", "10kHz")
        p.rise.SetValue("1ns")
        p.on_update(None)
        got["spec"] = f.settings.config.converters[0]
        p.spread.SetValue("15%")
        p.spread_mode.SetStringSelection("random")
        p.spread_rate.SetValue("")
        p.on_update(None)
        got["typed"] = f.settings.config.converters[0]
        p.spread.SetValue("abc")
        p.on_update(None)
        got["refused"] = f.settings.config.converters[0]
    run_steps(app, f, [check])
    c = got["spec"]
    assert c.q_oss == pytest.approx(38e-9) and c.rise_time == pytest.approx(1e-9)
    assert (c.spread, c.spread_mode, c.spread_rate) == (0.2, "triangle", 10e3)
    t = got["typed"]
    assert (t.spread, t.spread_mode, t.spread_rate) == (pytest.approx(0.15), "random", None)
    assert got["refused"].spread == pytest.approx(0.15)          # "abc": refused, the stage as it was


def test_schematic_fields_fill_the_forms_and_a_value_set_here_is_flagged(app, tmp_path, monkeypatch):
    """The parts' schematic fields (InvenTree parameters) fill a stage's R_DS(on) and C_oss: ◆ beside a value from
    the schematic, a warning '!' (with both values in its tooltip) beside one typed in that differs; Use schematic
    values takes the schematic's back. The Parts table names a part's linked parameters and flags a differing one."""
    from types import SimpleNamespace as NS
    from groundtruth.components.models import ComponentModel
    from groundtruth.ui import main_frame
    from groundtruth.ui.parts_panel import SCHEMATIC_COL
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    gen = hot_loop_board(20.0, connector=True)
    gen.fields("U1", **{"Rds(on)": "10milliohm", "Coss": "600 picofarad"})
    gen.fields("C1", ESR="5m")
    _, _, path = make(tmp_path, gen, "fieldsgui", nets=())
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    got = {}

    def check():
        p = f.converters
        for ctrl, v in ((p.name, "buck"), (p.rail, "VIN"), (p.ground, "GND"), (p.integrated, "U1"),
                        (p.f_sw, "750kHz"), (p.i_out, "5"), (p.v_in, "24"), (p.rise, "2ns"), (p.c_oss, "482p")):
            ctrl.SetValue(v)
        p.on_add(None)
        p.list.Select(0)
        p.on_select(NS(GetIndex=lambda: 0))
        got["rds"] = (p.rds_on.GetValue(), p.marks["rds_on"].GetLabel(), p.marks["rds_on"].GetToolTipText())
        got["coss"] = (p.marks["c_oss"].GetLabel(), p.marks["c_oss"].GetToolTipText(),
                       p.marks["c_oss"].GetForegroundColour())
        p.on_schematic(None)
        p.on_select(NS(GetIndex=lambda: 0))
        got["after"] = (p.c_oss.GetValue(), p.marks["c_oss"].GetLabel())
        parts = f.parts
        parts.load(f.board, f.settings)
        got["parts_linked"] = parts.grid.GetCellValue(parts.rows.index("C1"), SCHEMATIC_COL)
        f.settings.set_component("C1", ComponentModel("cap", {**f.settings.config.model_for("C1").params,
                                                               "ESR": 0.02}))
        parts.refresh()
        row = parts.rows.index("C1")
        got["parts_differs"] = parts.grid.GetCellValue(row, SCHEMATIC_COL)
        from groundtruth.ui.parts_panel import PARAM_COL
        got["esr_cell"] = parts.grid.GetCellBackgroundColour(row, PARAM_COL["ESR"])
    try:
        run_steps(app, f, [check])
    finally:
        close_window(f)
    from groundtruth.ui import theme
    assert got["rds"][0] == "10mΩ" and got["rds"][1] == "◆" and "U1 Rds(on) 10milliohm" in got["rds"][2]
    label, tip, colour = got["coss"]
    assert label == "!" and "482p" in tip and "600p" in tip and colour == theme.colour(theme.palette().warn)
    assert got["after"] == ("600pF", "◆")
    assert got["parts_linked"] == "◆ ESR"
    assert got["parts_differs"].startswith("! ESR: schematic 5m")
    assert got["esr_cell"] != theme.colour(theme.palette().panel)


def test_simulate_lists_the_design_changes_and_one_is_checked_in_simulate(app, conv_frame):
    """After Simulate the Fixes tab lists the fix advisor's design changes for the stage (snubber, edge, filter ...);
    selecting a snubber shows its parts, and Check in Simulate re-runs the stage with it in memory (the settings keep
    no snubber) and shows the prediction next to the result."""
    from groundtruth.config.settings import ConverterSpec
    f = conv_frame
    spec = ConverterSpec("buck", "VIN", "GND", switch=["U1"], f_sw=500e3, i_out=5.0, v_in=48.0, v_out=12.0,
                         rise_time=2e-9, c_oss=600e-12, input_connector=["J1.1", "J1.2"])
    got = {}

    def setup():
        f.settings.set_converters([spec])
        f.converters.load(f.board, f.settings)

    def pick():
        p = f.fixes
        got["rows"] = [o["kind"] for _, o in p.design]
        k = next(i for i, (_, o) in enumerate(p.design) if o["kind"] == "snubber")
        p.design_list.Select(k)
        p._design_selected(type("E", (), {"GetIndex": lambda self: k})())
        got["text"] = p.design_text.GetValue()
        got["enabled"] = p.check_design.IsEnabled()
        p._check()
    run_steps(app, f, [setup, lambda: f.run(True) and None, pick])
    assert got["rows"][0] == "now" and {"snubber", "edge", "filter"} <= set(got["rows"])
    assert "part: C " in got["text"] and got["enabled"]
    text = f.fixes.design_text.GetValue()
    assert "checked in Simulate" in text and "predicted" in text and "radiated" in text
    assert not f.settings.config.converters[0].snubber_c                       # the settings are unchanged


def test_converters_tab_adds_a_stage_and_simulate_shows_its_hot_loop(app, conv_frame):
    f = conv_frame

    def add():
        p = f.converters
        for ctrl, v in ((p.name, "buck"), (p.rail, "VIN"), (p.ground, "GND"), (p.integrated, "U1"),
                        (p.f_sw, "200kHz"), (p.i_out, "2"), (p.v_in, "12"), (p.rise, "10ns"), (p.inp, "J1.1, J1.2")):
            ctrl.SetValue(v)
        p.on_add(None)
    run_steps(app, f, [add, lambda: f.run(True) and None])
    assert [c.name for c in f.settings.config.converters] == ["buck"]
    c = f.routes[0]
    assert c.kind == "converter" and not c.error and c.conducted_worst is not None
    assert "hot loop" in f.results_panel.tile_text()
    assert f.notebook.GetSelection() == f.notebook.FindPage(f.results_panel)
    assert not f.results_panel.canvas3.IsShown()                      # a power stage has no route: no cross-section
    # current arrows on the map, off with the checkbox
    from matplotlib.quiver import Quiver

    def quivers():
        return [c for ax in f.results_panel.fig.axes for c in ax.collections if isinstance(c, Quiver)]
    assert quivers()
    # the views render off the GUI thread and are swapped in without rendering again; the toolbar keeps its
    # mouse connections across the swap (zoom and pan, the coordinates display)
    f.results_panel.arrows.SetValue(False)
    t = time.perf_counter()
    f.results_panel.redraw()
    assert time.perf_counter() - t < 0.1 and f.results_panel.rendering()
    wait_rendered(f)
    assert not quivers()
    assert f.results_panel.canvas.figure is f.results_panel.fig and f.results_panel.canvas.reused >= 2   # pixels reused
    from matplotlib.colors import LogNorm                       # "Log scale" off: 0 to the peak
    assert f.results_panel.log_scale.IsEnabled() and isinstance(f.results_panel.fig.axes[0].images[0].norm, LogNorm)
    f.results_panel.log_scale.SetValue(False)
    f.results_panel.redraw()
    wait_rendered(f)
    assert not isinstance(f.results_panel.fig.axes[0].images[0].norm, LogNorm)
    f.results_panel.log_scale.SetValue(True)
    moves = f.results_panel.canvas.callbacks.callbacks.get("motion_notify_event", {}).values()
    assert any(getattr(ref(), "__name__", "") == "mouse_move" for ref in moves)
    f.results_panel.arrows.SetValue(True)
    # "Open in window": the same view in a window of its own, larger, following the tab's choices
    h = f.results_panel
    h.open_window()
    win = h.window
    # at least 1000 px wide, unless the screen is narrower (Windows fits a window to it: 640 x 480 when the
    # display is locked or in a small remote session)
    assert win and win.IsShown() and win.GetSize().width >= min(1000, wx.GetClientDisplayRect().width) - 20
    wait_for(app, lambda: not h.rendering() and win.canvas.figure is win.fig and win.fig.axes)
    assert win.canvas.get_width_height()[0] > h.canvas.get_width_height()[0]
    win_quivers = lambda: [c for ax in win.fig.axes for c in ax.collections if isinstance(c, Quiver)]   # noqa: E731
    assert win_quivers()
    h.arrows.SetValue(False)
    h.redraw()
    wait_for(app, lambda: not h.rendering() and not win_quivers())
    h.open_window()                                             # a second click brings the same window up
    assert h.window is win
    win.Close()
    wait_for(app, lambda: h.window is None)
    h.redraw()                                                  # and the tab carries on without it
    wait_rendered(f)
    h.arrows.SetValue(True)
    # the DC input path is a heatmap of its own (current density on the grid it was solved on)
    k = next(i for i, r in enumerate(f.routes) if getattr(r, "view_of", "").startswith("input J1.1"))
    f.results_panel.choice.SetSelection(k)
    f.results_panel._route_changed()
    assert "DC input J1.1" in f.results_panel.tile_text() and "A/mm²" in f.results_panel.tile_text()


def test_setup_tabs_are_pastel_and_the_strip_changes_page(app, frame, monkeypatch):
    """The user: "can we colour those with pastel like colers and make them stand out". The tabs are drawn by
    GroundTruth (ui/tabbook.py; native Windows tabs can't be coloured one by one): the six setup tabs each have a
    hue of their own, the rest none, a gap before Parts. A tint is a light mix of the hue (stronger when selected),
    the label a deep shade of it; a click on a tab, the arrow keys on the strip and Ctrl+PgDn change the page."""
    from groundtruth.ui import theme
    from groundtruth.ui.tabbook import SETUP_HUES, mix, tab_colours
    f = frame
    run_steps(app, f, [lambda: None])                          # the tabs are built
    nb = f.notebook
    labels = [nb.GetPageText(k) for k in range(nb.GetPageCount())]
    coloured = [labels[k] for k, h in enumerate(nb.hues) if h]
    assert coloured == ["Signal paths", "Converters", "Bridges", "DC loads", "Power integrity", "Signal integrity"]
    assert len(set(SETUP_HUES.values())) == 6 and nb.gaps == {labels.index("Parts")}
    assert mix("#ff0000", "#ffffff", 0.2) == "#ffcccc"
    for pal in (theme.LIGHT, theme.DARK):
        quiet, chosen = tab_colours("#3b82f6", False, False, pal), tab_colours("#3b82f6", True, False, pal)
        assert quiet[0] != chosen[0] and quiet[2] is None and chosen[2] == "#3b82f6"
        assert tab_colours(None, False, False, pal)[0] is None
    # the strip: tabs left to right without overlap, the gap before Parts; a click shows that tab's page
    rects = nb.strip.tab_rects()
    assert all(a[0] + a[1] < b[0] for a, b in zip(rects, rects[1:]))
    k = labels.index("Results")
    nb.strip._left_down(type("E", (), {"GetX": lambda self: rects[k][0] + 5})())
    assert nb.GetCurrentPage() is f.results_panel
    nb.strip._key(type("E", (), {"GetKeyCode": lambda self: wx.WXK_LEFT, "Skip": lambda self: None})())
    assert nb.GetSelection() == k - 1
    f._step_tab(1)
    f._step_tab(1)
    assert nb.GetSelection() == k + 1
    assert nb.strip.GetBestSize().height > nb.strip.GetCharHeight()
    # the badges (the user: "add (#) as indicator for how many signals are actively part of the sim"): the board
    # was read, so every setup tab has its count; switching the nets off makes Signal paths' 0, hollow, and says so
    from dataclasses import replace
    from groundtruth.ui.main_frame import count_tip
    nets = f.settings.config.nets
    k_nets = nb.FindPage(f.nets)
    assert set(nb.badges) == {k for k, hue in enumerate(nb.hues) if hue}
    assert nets and nb.badges[k_nets][0] == len(nets) and "take" in nb.badges[k_nets][1]
    assert nb.badges[nb.FindPage(f.bridges)] == (0, count_tip(0, 0, "motor bridge", "motor bridges"))
    f.settings.set_nets([replace(n, enabled=False) for n in nets])
    f.on_config_change()
    assert nb.badges[k_nets][0] == 0 and "switched off" in nb.badges[k_nets][1]
    f.settings.set_nets([replace(n, enabled=True) for n in nets])
    f.on_config_change()
    assert nb.badges[k_nets][0] == len(nets)
    assert count_tip(2, 5, "signal path", "signal paths") == \
        "2 of 5 signal paths take part in Run checks and Simulate (3 switched off)"
    assert count_tip(1, 1, "converter", "converters") == "1 converter takes part in Run checks and Simulate"
    assert count_tip(0, 1, "converter", "converters").startswith("The 1 converter switched off")
    # the widths make room for the badge: a tab with one is wider than its label and padding alone
    lw, bw = nb.strip._measure()[k_nets]
    assert bw > 0 and nb.strip.tab_rects()[k_nets][1] >= lw + bw + nb.strip.BADGE_GAP
    # Results, Issues, Fixes in that order; Server jobs hidden without a compute server (no width, skipped by the
    # keys), shown once Preferences name one
    assert labels.index("Results") + 1 == labels.index("Issues") == labels.index("Fixes") - 1
    k_srv = nb.FindPage(f.server_jobs)
    assert k_srv == nb.GetPageCount() - 1 and k_srv in nb.hidden and nb.strip.tab_rects()[k_srv][1] == 0
    nb.SetSelection(k_srv - 1)
    nb.step(1)
    assert nb.GetSelection() == 0
    nb.step(-1)
    assert nb.GetSelection() == k_srv - 1
    from groundtruth.ui import theme as theme_mod
    monkeypatch.setattr(theme_mod, "prefs", lambda: {"compute": "server", "server": "gpu-box:8765", "server_key": "k"})
    f._show_server_tab()
    assert k_srv not in nb.hidden and nb.strip.tab_rects()[k_srv][1] > 0
    nb.SetSelection(k_srv)
    monkeypatch.setattr(theme_mod, "prefs", lambda: {})
    f._show_server_tab()
    assert k_srv in nb.hidden and nb.GetSelection() == 0         # hidden while shown: the first tab instead
    # the user: "fix the plugin logo, add the groundtruth logo": the window carries the toolbar button's logo
    assert theme.app_icons().GetIconCount() == 5 and f.GetIcon().IsOk() and f.GetIcon().GetWidth() in (16, 24, 32)


def test_all_ticks_every_row_on_or_off_and_shows_a_mixed_table(app, frame):
    """The user: "in each of the selectors, provide a enable/disable all button". Every tick-box table (Signal
    paths, Converters, Bridges, DC loads, Power integrity, Signal integrity's pairs) has "Select All" above its tick column:
    a click unticks every row when all were ticked and ticks them all otherwise, saved once (the settings and the
    tab's count follow); it shows ticked / empty / the third state for all / none / some rows, greyed without rows."""
    f = frame
    run_steps(app, f, [lambda: None])
    for panel in (f.nets, f.converters, f.bridges, f.dc_loads, f.pdn, f.si):
        assert isinstance(panel.all_ticks, wx.CheckBox) and panel.all_ticks.GetContainingSizer() is not None
    p = f.nets
    n = len(p.nets)
    assert n >= 1
    p._sync_all_ticks()
    assert p.all_ticks.Get3StateValue() == wx.CHK_CHECKED and p.all_ticks.IsEnabled()
    p.tick_all()                                                 # all were on: all off
    assert not any(s.enabled for s in f.settings.config.nets) and len(f.settings.config.nets) == n
    assert p.all_ticks.Get3StateValue() == wx.CHK_UNCHECKED
    assert not any(p.list.IsItemChecked(i) for i in range(p.list.GetItemCount()))
    assert f.notebook.badges[f.notebook.FindPage(p)][0] == 0
    p.tick_all()                                                 # none on: all on
    assert all(s.enabled for s in f.settings.config.nets)
    assert all(p.list.IsItemChecked(i) for i in range(p.list.GetItemCount()))
    assert f.notebook.badges[f.notebook.FindPage(p)][0] == n
    if n > 1:                                                    # some on: the third state, a click ticks all
        p.list.CheckItem(0, False)
        p._sync_all_ticks()
        assert p.all_ticks.Get3StateValue() == wx.CHK_UNDETERMINED
        p.tick_all()
        assert all(s.enabled for s in f.settings.config.nets)
    f.bridges._sync_all_ticks()                                  # no bridges on this board: greyed
    assert not f.bridges.bridges and not f.bridges.all_ticks.IsEnabled()


def test_run_buttons_stay_usable_while_the_board_is_read_and_a_click_waits(app, frame):
    """The user saw Refill, Run checks, Simulate and Thermal grey at start-up (a board load is a job, and a job
    disabled every button) and chose: clickable, the click queued. During a load (the tabs being built, the board
    read, detection) Run checks / Simulate / Thermal stay enabled; a click is kept (the latest wins), named on the
    status line, and started once the window is idle. Other jobs still disable them, and Reload / Refill stay grey
    during a load."""
    f = frame
    assert not f.pages_built() and f._loading()                  # the tabs are still being built
    f.run(True)
    f.run(False)                                                 # the latest click wins
    assert f._queued[0] == "Run checks" and "Run checks" in f.status_text()
    # the queued Run checks starts by itself once the board is read and the tabs are filled: the Issues tab shows
    wait_for(app, lambda: f._queued is None and not f.busy() and f.issues is not None
             and f.notebook.GetCurrentPage() is f.issues_panel, timeout=120)
    assert not f._queue_timer.IsRunning()
    assert not f._queue_if_loading("Simulate", lambda: None)    # loaded: a click runs at once
    f._enable(False, keep_runs=True)                             # what a load job does
    assert f.t_check.IsEnabled() and f.t_sim.IsEnabled() and f.t_thermal.IsEnabled() and not f.t_reload.IsEnabled()
    f._enable(False)                                             # any other job
    assert not (f.t_check.IsEnabled() or f.t_sim.IsEnabled() or f.t_thermal.IsEnabled())
    f._enable(True)


def test_help_opens_at_the_tab_shown_and_every_link_lands(app, frame):
    """Help (top bar, F1) opens one window at the section of the tab shown, and raises it again for another tab.
    Every topic and every link inside the page has its anchor; the theme's colours are filled in."""
    import re
    from groundtruth.ui.help import TOPICS, page
    f = frame
    run_steps(app, f, [lambda: None])                          # the tabs are built
    text = page()
    anchors = set(re.findall(r'<a name="([^"]+)"', text))
    assert set(TOPICS) <= anchors and not re.search(r"@[A-Z]+@", text)
    assert {h[1:] for h in re.findall(r'href="(#[^"]+)"', text)} <= anchors
    # every tab has its own section for F1, and every section its place in the sidebar's groups (none under "More")
    from groundtruth.ui.help import GROUPS, topic_of
    labels = [f.notebook.GetPageText(k) for k in range(f.notebook.GetPageCount())]
    assert all(topic_of(t) != "workflow" for t in labels), [(t, topic_of(t)) for t in labels]
    grouped = {a for _, ids in GROUPS for a in ids}
    assert set(re.findall(r'<a name="([^"]+)"></a><h3>', text)) <= grouped
    f.notebook.SetSelection(f.notebook.FindPage(f.results_panel))
    t0 = time.perf_counter()
    f.on_help()
    opened = time.perf_counter() - t0
    h = f._help
    assert h.IsShown() and "What Simulate does with nets, converters and bridges" in h.text()
    # the browser engine where there is one (Edge WebView2 here), the page with its sidebar and every topic; opening
    # it never stalls the window (the engine starts in the background)
    from groundtruth.ui.help import _webview_backend, modern_page
    if _webview_backend() is not None:
        assert h.view is not None and h.html is None and opened < 1.0, opened
        modern = modern_page()
        ids = set(re.findall(r'id="([^"]+)"', modern))
        assert set(TOPICS) <= ids and {x[1:] for x in re.findall(r'href="(#[^"]+)"', modern)} <= ids
        assert not re.search(r"@[A-Z_]+@", modern) and "<font" not in modern and modern.count('<nav id="nav">') == 1
        wait_for(app, lambda: h._loaded, timeout=20)
        # an engine that does not start in time (its data folder in use, a broken runtime): the simple view instead
        from groundtruth.ui import help as help_mod
        slow = help_mod.HelpWindow(f, "parts")
        slow._watch.Stop()
        slow._not_loaded()
        assert slow.view is None and slow.html is not None and "Parts tab" in slow.text()
        close_window(slow)
    f.notebook.SetSelection(f.notebook.FindPage(f.bridges))
    f.on_help()
    assert f._help is h                                           # the same window, raised
    close_window(h)
    f.on_help()                                                   # closed: a new one
    assert f._help is not h and f._help.IsShown()
    close_window(f._help)


def test_the_report_file_name_is_a_valid_windows_name_in_live_mode():
    """Live KiCad boards read "kicad-ipc:<name>": the Export dialog suggested "kicad-ipc:kraft_flux_mini_2_report.html",
    which Windows refuses (':'). The name comes from the settings file next to the board, cleaned of anything else."""
    from pathlib import Path
    from types import SimpleNamespace as NS
    from groundtruth.ui.main_frame import report_stem
    live = NS(source="kicad-ipc:kraft_flux_mini_2.kicad_pcb")
    assert report_stem(live, Path("C:/x/kraft_flux_mini_2.groundtruth.yaml")) == "kraft_flux_mini_2"
    assert report_stem(live, None) == "kraft_flux_mini_2.kicad_pcb"
    assert report_stem(NS(source="C:/b/my board.kicad_pcb"), None) == "my_board"
    assert report_stem(NS(source="kicad-ipc:a<b>|c?"), None) == "a_b_c"
    assert not set(':<>"/\\|?*') & set(report_stem(live, None))


def test_export_report_writes_the_page_in_the_background(app, frame, tmp_path, monkeypatch):
    """Export report…: after Simulate, the results become one self-contained HTML page (written as a job, the
    window keeps responding), which opens in the browser. The dialog offers the board's groundtruth folder."""
    from pathlib import Path
    f = frame
    target = tmp_path / "out" / "report.html"
    target.parent.mkdir()

    offered = []

    class Dialog:
        def __init__(self, *a, **k):
            offered.append(k.get("defaultDir"))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ShowModal(self):
            return wx.ID_OK

        def GetPath(self):
            return str(target)
    monkeypatch.setattr(wx, "FileDialog", Dialog)
    opened = []
    monkeypatch.setattr(wx, "LaunchDefaultBrowser", lambda url: opened.append(url) or True)
    from groundtruth.report import html as report_html
    from groundtruth.ui import theme
    monkeypatch.setattr(theme, "pref_path", lambda: tmp_path / "ui.json")     # never the user's preferences
    (tmp_path / "ui.json").write_text('{"history": false}', encoding="utf-8")  # no history report beside it
    asked = []
    real = report_html.render_html
    monkeypatch.setattr(report_html, "render_html", lambda *a, **k: asked.append(k["max_vias"]) or real(*a, **k))

    def vias_5():
        from groundtruth.ui.prefs_dialog import defaults
        f.apply_preferences(dict(defaults({}), max_vias=5))                  # Preferences, OK
        assert theme.prefs()["max_vias"] == 5 and f.results_panel.max_vias() == 5  # kept for every board
    ticks = run_steps(app, f, [lambda: f.run(True) and None, vias_5, lambda: f.on_export()])
    assert asked == [5]                                                       # the report follows it
    assert offered == [str(Path(f.board_path).parent / "groundtruth")]      # the board's groundtruth folder first
    text = target.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>") and "Signal nets" in text and "Method and limits" in text
    assert "data:image/png;base64," in text and "http://" not in text.split("<main>")[0]
    assert opened == [target.as_uri()] and "report written" in f.GetStatusBar().GetStatusText(0)
    assert longest_busy_gap(ticks) < 0.5


def test_start_window_shows_its_status(app):
    from groundtruth.ui.app import StartWindow
    w = StartWindow()
    try:
        assert w.IsShown() and w.GetTitle().startswith("GroundTruth")
        w.status("Connecting to KiCad…")
        assert w.text.GetLabel() == "Connecting to KiCad…"
    finally:
        w.close()
        wx.Yield()
    assert not w


class Ticker(wx.Timer):
    """Records when the GUI thread handled its timer (gaps between ticks are times it was busy) and calls
    on_tick each time."""

    def __init__(self, on_tick=None):
        super().__init__()
        self.ticks = []
        self.on_tick = on_tick

    def Notify(self):
        self.ticks.append(time.perf_counter())
        if self.on_tick:
            self.on_tick()

    def longest_gap(self) -> float:
        return max((b - a for a, b in zip(self.ticks, self.ticks[1:])), default=0.0)


@pytest.mark.parametrize("kicad", ["answers slowly", "is not running"])
def test_plugin_start_never_blocks_its_window(app, tmp_path, monkeypatch, kicad):
    """The toolbar action as KiCad starts it: while KiCad takes 1.5 s to answer and the analysis modules load,
    the start window's event loop keeps running (a GUI thread stalled for 5 s is "Not Responding" on Windows).
    Then the main window opens titled with the board, or the user is told what went wrong."""
    from types import SimpleNamespace
    from groundtruth.adapters import kipy_source
    from groundtruth.ui import app as app_module
    from groundtruth.ui import startup
    monkeypatch.setattr(startup, "log_path", lambda: tmp_path / "plugin.log")
    messages = []
    monkeypatch.setattr(wx, "MessageBox", lambda msg, *a, **k: messages.append(msg) or wx.OK)

    def slow_connect():
        time.sleep(1.5)
        if kicad == "is not running":
            raise kipy_source.KiCadConnectionError("cannot reach KiCad's API server")
        return SimpleNamespace(get_board=lambda: SimpleNamespace(name="fake.kicad_pcb"))
    monkeypatch.setattr(kipy_source, "connect", slow_connect)
    titles = []
    ticker = Ticker()

    def watch():                        # close the main window once it has tried to read the (fake) board
        for w in wx.GetTopLevelWindows():
            if w.GetTitle() not in titles:
                titles.append(w.GetTitle())
            if w.GetTitle() == "fake – GroundTruth" and not w.busy() and messages:
                w.Close()
    closer = Ticker(watch)
    gc.collect()        # the suite's leftovers (GBs by now) would make a full GC pass a stall the plugin never sees,
    gc.freeze()         # and frozen they are not rescanned by a full collection during the timed start
    ticker.Start(20)
    closer.Start(50)
    default_interval = sys.getswitchinterval()
    try:
        code = app_module.plugin_main([], started=time.time())
        assert sys.getswitchinterval() == app_module.GUI_SWITCH_INTERVAL   # the GUI gets the GIL at once
    finally:
        sys.setswitchinterval(default_interval)                         # the other tests keep Python's
        gc.unfreeze()
        ticker.Stop()
        closer.Stop()
        wx.Yield()
    assert ticker.longest_gap() < 0.5, f"GUI thread stalled {ticker.longest_gap():.2f} s"
    assert ticker.ticks[-1] - ticker.ticks[0] > 1.4                    # the loop ran throughout
    assert titles[0] == "GroundTruth – starting"
    log = (tmp_path / "plugin.log").read_text(encoding="utf-8")
    if kicad == "is not running":
        assert code == 2 and "cannot reach KiCad" in messages[0] and "no connection to KiCad" in log
        assert "fake – GroundTruth" not in titles
    else:
        assert code == 0 and "fake – GroundTruth" in titles
        assert "analysis modules imported" in log and "main window shown" in log
    assert not wx.GetTopLevelWindows()


def test_a_new_board_gets_its_power_stages_detected(app, tmp_path, monkeypatch):
    """No settings file yet: the window names the board, detects the buck in the background and saves it as
    unreviewed; Detect… then runs as a job too (the GUI stays live) and opens its picker."""
    from groundtruth.config.settings import SettingsFile
    from groundtruth.ui import converters_panel, main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    picked = []

    class Picker:                       # the Detect… dialog, answered with Cancel
        def __init__(self, parent, found, listed):
            picked.append((len(found), len(listed)))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ShowModal(self):
            return wx.ID_CANCEL
    monkeypatch.setattr(converters_panel, "ConverterDetectDialog", Picker)
    path = discrete_buck_board().write(tmp_path / "buck.kicad_pcb")
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        assert f.GetTitle() == "buck – GroundTruth"
        ticks = run_steps(app, f, [lambda: f.converters.on_detect(None)])
        convs = f.settings.config.converters
        assert len(convs) == 1 and convs[0].source == "auto" and not convs[0].reviewed
        assert f._saver.flush()                                 # settings are written in the background
        assert [c.name for c in SettingsFile.load(tmp_path / "groundtruth" / "buck.groundtruth.yaml").config.converters] == \
            [convs[0].name]
        assert not f.converters.hint.IsShown()
        assert f.nets.hint.IsShown() == (not f.settings.config.nets)
        assert picked == [(1, 1)]
        assert longest_busy_gap(ticks) < 0.5
        assert f.GetStatusBar().GetStatusText(0) == "finished: detect power stages"
    finally:
        close_window(f)


def test_net_lists_fill_when_they_drop_down(app, frame):
    """A board's nets go into the combo boxes only when one drops down (filling a native combo box costs ~0.6 ms
    per item on Windows; at load that stalled the window); what is typed is kept."""
    f = frame
    run_steps(app, f, [])                                       # load the board
    combos = [f.nets.net, f.converters.rail, f.converters.sw, f.converters.ground]
    assert all(c.GetCount() == 0 for c in combos)
    f.nets.net.ChangeValue("typed")
    evt = wx.CommandEvent(wx.wxEVT_COMBOBOX_DROPDOWN, f.nets.net.GetId())
    evt.SetEventObject(f.nets.net)
    f.nets.net.GetEventHandler().ProcessEvent(evt)
    assert f.nets.net.GetCount() == len(f.board.nets) > 0 and f.nets.net.GetValue() == "typed"
    f.nets.load(f.board, f.settings)                            # a reload drops the list again, keeps the text
    assert f.nets.net.GetCount() == 0 and f.nets.net.GetValue() == "typed"



def test_several_nets_are_removed_or_reviewed_at_once(app, frame):
    """Ctrl/Shift-click selects several rows of the Nets table: Remove selected (or the Delete key) removes them
    all, Mark reviewed marks them all; Update selected needs exactly one; Ctrl+A selects every row."""
    from dataclasses import replace
    from types import SimpleNamespace
    f = frame
    run_steps(app, f, [])                                       # load the board
    base = f.settings.config.nets[0]
    f.nets.nets = [replace(base, name=f"N{k}", source="auto", reviewed=False) for k in range(5)]
    f.nets._commit()
    names = lambda: [n.name for n in f.settings.config.nets]   # noqa: E731
    for i in (1, 3):
        f.nets.list.Select(i)
    assert f.nets.selected_rows() == [1, 3]
    f.nets.on_reviewed(None)
    assert [n.reviewed for n in f.settings.config.nets] == [False, True, False, True, False]
    assert f.nets.selected_rows() == [1, 3]                    # still selected after the table refreshed
    for i in (1, 3):
        f.nets.list.Select(i, False)
    for i in (0, 4):
        f.nets.list.Select(i)
    key = lambda code: SimpleNamespace(GetKeyCode=lambda: code, Skip=lambda: None)   # noqa: E731
    f.nets.on_key(key(wx.WXK_DELETE))
    assert names() == ["N1", "N2", "N3"] and f.nets.list.GetItemCount() == 3
    f.nets.list.Select(0)
    f.nets.list.Select(2)
    f.nets.on_update(None)                                      # two rows: refused (a message), nothing changed
    assert names() == ["N1", "N2", "N3"]
    f.nets.select("N2")                                         # selecting by name leaves that row alone
    assert f.nets.selected_rows() == [1]
    for i in range(3):
        f.nets.list.Select(i)
    f.nets.on_remove(None)                                      # the "Remove selected" button
    assert names() == [] and f.nets.list.GetItemCount() == 0

def wait_for(app, condition, timeout=10):
    """Run the event loop until condition() holds (the window's timers fire only inside MainLoop, not in
    wx.Yield)."""
    t0 = time.perf_counter()

    def check():
        if condition() or time.perf_counter() - t0 > timeout:
            app.ExitMainLoop()
    ticker = Ticker(check)
    ticker.Start(20)
    app.MainLoop()
    ticker.Stop()
    assert condition(), "timed out"


def test_nets_sent_from_kicad_land_in_the_table(app, tmp_path, monkeypatch):
    """KiCad's 'Add selected net' button (another process) leaves nets in the board's inbox. The window adds
    them as unreviewed suggestions (driver and receivers from the pin types), shows the last one selected on the
    Nets tab and says what it did; a reference net is refused with the reason."""
    from test_detect import spi_board
    from groundtruth.ui import inbox, main_frame
    messages = []
    monkeypatch.setattr(wx, "MessageBox", lambda msg, *a, **k: messages.append(msg) or wx.OK)
    path = spi_board(tmp_path)
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])                                   # board read, SPI nets auto-detected
        names = lambda: [n.name for n in f.settings.config.nets]            # noqa: E731
        assert "/MCU/SPI_SCK" in names() and "/LED" not in names()
        f.notebook.SetSelection(f.notebook.FindPage(f.results_panel))
        inbox.send(path, ["/LED", "/MCU/SPI_SCK"], wait=0)
        wait_for(app, lambda: "/LED" in names())
        led = next(n for n in f.settings.config.nets if n.name == "/LED")
        assert (led.driver, led.receivers, led.source, led.reviewed) == ("U5.1", ["U6.1"], "auto", False)
        assert f.notebook.GetSelection() == f.notebook.FindPage(f.nets)
        assert f.nets.list.GetFirstSelected() == names().index("/MCU/SPI_SCK")        # the last one sent
        status = f.GetStatusBar().GetStatusText(0)
        assert "added /LED" in status and "set its frequency" in status and "already listed" in status
        inbox.send(path, ["GND"], wait=0)
        wait_for(app, lambda: messages)
        assert "reference net" in messages[-1] and "GND" not in names()
        assert not list(f._inbox.requests.glob("*.json"))
    finally:
        close_window(f)


def test_add_kicad_selection_button(app, tmp_path, monkeypatch):
    """Live mode: the Nets tab's 'Add KiCad selection' reads KiCad's selection in a job and adds its nets; with
    nothing selected it says how to select."""
    from test_detect import spi_board
    from groundtruth.adapters.file_source import load_board
    from groundtruth.ui import main_frame
    messages = []
    monkeypatch.setattr(wx, "MessageBox", lambda msg, *a, **k: messages.append(msg) or wx.NO)
    selection = [["/LED"], []]

    class LiveFromFile(main_frame.Session):                     # KiCad stand-in: the board comes from the file
        live = True

        def load(self):
            return load_board(self.file_path), self.file_path

        def selected_nets(self):
            return selection.pop(0)
    f = main_frame.MainFrame(LiveFromFile(file_path=str(spi_board(tmp_path))))
    f.Show()
    try:
        wait_for(app, f.pages_built)                            # the tabs are built step by step
        from groundtruth.ui.theme import Button
        buttons = [w for w in f.nets.GetChildren() if isinstance(w, Button)]
        assert "Add KiCad selection" in [b.GetLabel() for b in buttons]
        run_steps(app, f, [f.on_add_selection])
        assert "/LED" in [n.name for n in f.settings.config.nets]
        run_steps(app, f, [f.on_add_selection])
        assert "Nothing with a net is selected" in messages[-1]
    finally:
        close_window(f)


@pytest.mark.parametrize("case", ["window open", "nothing selected", "no window"])
def test_add_selected_net_button_process(app, tmp_path, monkeypatch, case):
    """KiCad's 'Add selected net to GroundTruth' runs as a process of its own (a fake KiCad here): it reads the
    selection and hands its nets to the board's open window, then ends without a window of its own; with
    nothing selected it says how to select; with no window open it opens one, for which the request waits."""
    import json
    import subprocess
    import threading
    from types import SimpleNamespace as NS
    from groundtruth.adapters import kipy_source
    from groundtruth.ui import app as app_module
    from groundtruth.ui import inbox
    board = tmp_path / "b.kicad_pcb"
    messages = []
    monkeypatch.setattr(wx, "MessageBox", lambda msg, *a, **k: messages.append(msg) or wx.OK)
    selection = [] if case == "nothing selected" else [NS(net=NS(name="/LED"))]
    kb = NS(name=board.name, get_project=lambda: NS(path=str(tmp_path)), get_selection=lambda: selection)
    monkeypatch.setattr(kipy_source, "connect", lambda: NS(get_board=lambda: kb))
    default_interval = sys.getswitchinterval()
    child = None
    try:
        if case == "window open":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            window = inbox.Inbox(board)
            data = json.loads(window.window_file.read_text(encoding="utf-8"))
            data["pid"] = child.pid                             # the board's window: another, running process
            window.window_file.write_text(json.dumps(data), encoding="utf-8")
            taken = []

            def the_window():
                for _ in range(250):
                    if (got := window.take()):
                        taken.extend(got)
                        return
                    time.sleep(0.02)
            t = threading.Thread(target=the_window)
            t.start()
            code = app_module.plugin_main(["add-net"], started=time.time())
            t.join()
            assert code == 0 and taken == [["/LED"]] and not messages
            assert not [w for w in wx.GetTopLevelWindows() if w.IsShown()]
        elif case == "nothing selected":
            assert app_module.plugin_main(["add-net"], started=time.time()) == 2
            assert "Nothing with a net is selected" in messages[0] and "empty selection" in messages[0]
        else:
            def close_once_tried():                             # the fake board cannot be read: close then
                for w in wx.GetTopLevelWindows():
                    if hasattr(w, "busy") and not w.busy() and messages:
                        w.Close()
            closer = Ticker(close_once_tried)
            closer.Start(50)
            try:
                code = app_module.plugin_main(["add-net"], started=time.time())
            finally:
                closer.Stop()
            assert code == 0                                    # the window opened (and was closed)
            waiting = list((inbox.windows_root() / inbox.board_key(board) / "requests").glob("*.json"))
            assert len(waiting) == 1 and json.loads(waiting[0].read_text(encoding="utf-8")) == {"nets": ["/LED"], "pads": []}
    finally:
        sys.setswitchinterval(default_interval)
        if child is not None:
            child.kill()
            child.wait(10)
        wx.Yield()


def test_a_destroyed_window_stops_its_timers(app, tmp_path, monkeypatch):
    """A wx.Timer outlives its owner: one left running after its window is destroyed fires into freed memory.
    That crashed the next test with heap corruption (0xc0000374) or an access violation every time, when the
    inbox timer was only stopped by the close button. Destroying the window must stop every timer."""
    from groundtruth.ui import main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    _, _, path = make(tmp_path, layer_change_board(None), "gui")
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    run_steps(app, f, [])
    timer = f._inbox_timer
    assert timer.IsRunning()
    child = wx.Frame(f)                 # a child window closing (the heatmap window, the tab holder): the destroy
    child.Destroy()                     # event bubbles up to the main window, whose timers must keep running
    wx.Yield()
    assert timer.IsRunning()
    f.Destroy()
    wx.Yield()
    assert not timer.IsRunning()


class LiveSlotBoard:
    """A window in live mode on the slotted microstrip (one gap-crossing issue), with KiCad faked in kipy_sink, so
    the real Session (its KiCad lock and bookkeeping) and the window's background thread are what is tested.
    Showing an issue takes `delay` seconds (or raises `fail`); every KiCad call is logged with start and end."""

    def __init__(self, app, tmp_path, monkeypatch, delay=1.0, draw_time=0.2, gen=None, converters=False):
        """gen: another board (no checks run); converters: detect its power stages into the settings first."""
        import threading
        from groundtruth.adapters import kipy_sink
        from groundtruth.adapters.file_source import load_board
        from groundtruth.ui import main_frame
        self.boxes = []
        monkeypatch.setattr(wx, "MessageBox", lambda msg, *a, **k: self.boxes.append(msg) or wx.NO)
        self.calls, self.started, self.fail = [], threading.Event(), None

        def show_items(kb, lit, focus, context=(), nets=True, zoom=True, zoom_out=0, layer=None, clear_old=False,
                       wait=None):
            self.started.set()
            if self.fail is not None:
                raise self.fail
            t0 = time.perf_counter()
            time.sleep(delay)
            self.calls.append(("show", t0, time.perf_counter(), list(focus), list(context), layer, nets, zoom,
                               list(lit)))
            return {"selected": len(focus) + len(context), "focus": len(focus), "lit": bool(lit) and nets,
                    "previous_layer": None, "notes": []}

        def draw_issues(kb, issues, layer, wait=None, hidden=frozenset()):
            t0 = time.perf_counter()
            time.sleep(draw_time)
            self.calls.append(("draw", t0, time.perf_counter()))
            return len(issues)
        monkeypatch.setattr(kipy_sink, "show_items", show_items)
        monkeypatch.setattr(kipy_sink, "draw_issues", draw_issues)
        monkeypatch.setattr(kipy_sink, "clear_highlight", lambda kb, layer=None, wait=None:
                            self.calls.append(("clear", time.perf_counter())))

        class LiveFromFile(main_frame.Session):
            live = True

            def load(self):
                return load_board(self.file_path), self.file_path
        board, sc, path = make(tmp_path, gen or microstrip_board(slot=(19.5, 4, 20.5, 16)), "slot",
                               **({"nets": ()} if converters else {}))
        if converters:
            from groundtruth.analysis.detect import detect_converters
            sc.add_detected_converters([d.spec for d in detect_converters(board, sc.config)])
            sc.save()
        self.frame = main_frame.MainFrame(LiveFromFile(file_path=str(path)))
        self.frame.Show()
        if gen is not None:
            run_steps(app, self.frame, [])                                      # read the board
            return
        run_steps(app, self.frame, [lambda: self.frame.run(False) and None])       # Run checks
        assert [i.check for i in self.frame.issues] == ["gap_crossing"]

    def shows(self):
        return [c for c in self.calls if c[0] == "show"]

    def status(self):
        return self.frame.GetStatusBar().GetStatusText(0)


def test_clicking_an_issue_shows_it_in_kicad_without_blocking(app, tmp_path, monkeypatch):
    """A click selects the gap crossing's trace in KiCad, highlights its net, zooms to it and adds the GND plane
    beside it, all from a background thread: the window keeps handling events while KiCad takes a second; the
    status bar says what was shown."""
    live = LiveSlotBoard(app, tmp_path, monkeypatch, delay=1.0)
    f = live.frame
    try:
        issue = f.issues[0]
        ticker = Ticker()
        t = time.perf_counter()
        f.issues_panel.list.Select(0)                           # the user clicks the row
        assert time.perf_counter() - t < 0.1                    # handed to the background thread at once
        ticker.Start(20)
        wait_for(app, lambda: live.shows() and "KiCad:" in live.status())
        ticker.Stop()
        assert ticker.longest_gap() < 0.5, f"GUI stalled {ticker.longest_gap():.2f} s"
        _, _, _, focus, context, layer, nets, zoom, lit = live.shows()[0]
        assert focus[0] == issue.items[0] and lit == focus and layer == "B.Cu" and nets and zoom
        assert context == [z.uuid for z in f.board.zones if z.net == "GND"]
        assert "over the gap" in live.status() and f.session.net_lit
        f.issues_panel.zoom.SetValue(False)                     # the options go with the next show
        f.issues_panel.nets.SetValue(False)
        f.issues_panel.show_selected()                          # "Show in KiCad" / double-click
        wait_for(app, lambda: len(live.shows()) == 2)
        assert live.shows()[1][6:8] == (False, False)
    finally:
        close_window(f)


def test_rapid_clicks_show_the_newest_and_kicad_jobs_never_overlap_a_show(app, tmp_path, monkeypatch):
    """Clicking through the list while KiCad is slow shows the one being shown and then the newest click, not
    every one; a KiCad job started meanwhile (drawing the markers) waits for the show: KiCad's connection is never
    used by two threads."""
    import dataclasses
    live = LiveSlotBoard(app, tmp_path, monkeypatch, delay=0.6)
    f = live.frame
    try:
        base = f.issues[0]
        clicks = [dataclasses.replace(base, items=[f"clicked-{k}"]) for k in range(4)]
        f.on_issue_selected(clicks[0])
        wait_for(app, live.started.is_set)                      # the first show is on its way to KiCad
        for issue in clicks[1:]:
            f.on_issue_selected(issue)                          # e.g. arrow keys through the list
        f.on_draw_issues()                                      # a KiCad job, started while the first show runs
        wait_for(app, lambda: [c for c in live.calls if c[0] == "draw"] and len(live.shows()) == 2
                 and not f.busy() and not f._highlighter.busy())
        assert [s[3][0] for s in live.shows()] == ["clicked-0", "clicked-3"]   # clicks 1 and 2 dropped
        spans = sorted((c[1], c[2]) for c in live.calls if c[0] in ("show", "draw"))
        assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:])), spans
    finally:
        close_window(f)


def test_a_click_during_a_long_kicad_job_is_refused_not_queued(app, tmp_path, monkeypatch):
    """A show waiting behind a long KiCad job would move the user's view seconds after the click: it is refused
    with a status line instead."""
    live = LiveSlotBoard(app, tmp_path, monkeypatch, delay=0.0, draw_time=2.0)
    f = live.frame
    try:
        f.on_draw_issues()
        wait_for(app, lambda: f.busy())
        f.on_issue_selected(f.issues[0])
        wait_for(app, lambda: "busy in KiCad" in live.status())
        assert not live.shows() and not live.boxes
    finally:
        wait_for(app, lambda: not f.busy())
        close_window(f)


def test_kicad_errors_go_to_the_status_bar_and_bugs_to_a_dialog(app, tmp_path, monkeypatch):
    from groundtruth.adapters.kipy_source import KiCadBusyError
    live = LiveSlotBoard(app, tmp_path, monkeypatch, delay=0.0)
    f = live.frame
    try:
        live.fail = KiCadBusyError("KiCad is busy and does not accept requests: close any open dialog")
        f.on_issue_selected(f.issues[0])
        wait_for(app, lambda: "could not show it in KiCad: KiCad is busy" in live.status())
        assert not live.boxes                                   # an expected condition: no dialog
        live.fail = RuntimeError("a bug")
        f.on_issue_selected(f.issues[0])
        wait_for(app, lambda: live.boxes)
        assert "a bug" in live.boxes[0] and "Traceback" in live.boxes[0]
        live.fail = None
        f.on_issue_selected(f.issues[0])                        # the thread still serves the next click
        wait_for(app, lambda: live.shows())
    finally:
        close_window(f)


def test_closing_clears_the_net_highlight_even_while_a_show_is_on_its_way(app, tmp_path, monkeypatch):
    """KiCad keeps a net highlight after the plugin is gone: closing clears ours, also when the window is closed
    before the click's show reached KiCad (the clear then follows the show)."""
    live = LiveSlotBoard(app, tmp_path, monkeypatch, delay=0.8)
    f = live.frame
    f.on_issue_selected(f.issues[0])
    wait_for(app, live.started.is_set)
    f.Close()                                                   # the close button, while the show runs
    wx.Yield()
    clears = [c for c in live.calls if c[0] == "clear"]
    assert live.shows() and clears and clears[0][1] >= live.shows()[0][2]


def test_in_file_mode_a_click_says_what_the_issue_is_about(app, tmp_path, monkeypatch):
    from groundtruth.ui import main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    _, _, path = make(tmp_path, microstrip_board(slot=(19.5, 4, 20.5, 16)), "slot")
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [lambda: f.run(False) and None])
        assert not f.issues_panel.zoom.IsShown()                # no KiCad options without KiCad
        f.issues_panel.list.Select(0)
        wait_for(app, lambda: "open GroundTruth from KiCad" in f.GetStatusBar().GetStatusText(0))
        assert "over the gap" in f.GetStatusBar().GetStatusText(0)
    finally:
        close_window(f)


def test_clicking_nets_highlights_them_in_kicad_and_deselecting_clears(app, tmp_path, monkeypatch):
    """A row of the Nets tab clicked: its net's copper is selected and lit in KiCad from the background thread
    (zoom as ticked); a Shift-click range is one show of all its nets; no row selected clears our highlight."""
    live = LiveSlotBoard(app, tmp_path, monkeypatch, delay=0.0)
    f = live.frame
    try:
        assert f.nets.zoom.IsShown() and [n.name for n in f.nets.nets] == ["SIG"]
        f.nets.list.Select(0)                                   # the user clicks the row
        wait_for(app, lambda: live.shows() and "KiCad: SIG" in live.status())
        _, _, _, focus, context, layer, nets, zoom, lit = live.shows()[-1]
        sig = {t.uuid for t in f.board.tracks if t.net == "SIG"} | {p.uuid for p in f.board.pads() if p.net == "SIG"}
        assert set(lit) == set(focus) == sig and nets and zoom and not layer
        assert "2 pads" in live.status() and f.session.net_lit
        n = len(live.shows())
        f.nets.zoom.SetValue(False)
        f.nets.list.Select(0, False)                            # Ctrl-click: nothing selected any more
        wait_for(app, lambda: len(live.shows()) == n + 1)
        _, _, _, focus, context, _, _, zoom, lit = live.shows()[-1]
        assert not focus and not context and not lit and not zoom
    finally:
        close_window(f)


def test_clicking_power_stages_shows_them_and_several_are_removed_at_once(app, tmp_path, monkeypatch):
    """The Converters tab like the Nets tab: a click shows the stage in KiCad (its switch node lit), several rows
    are selected with Ctrl/Shift-click and removed (or marked reviewed) at once."""
    from dataclasses import replace
    from types import SimpleNamespace
    live = LiveSlotBoard(app, tmp_path, monkeypatch, delay=0.0, gen=discrete_buck_board(), converters=True)
    f = live.frame
    try:
        c = f.converters
        spec = f.settings.config.converters[0]
        assert c.zoom.IsShown() and len(c.converters) == 1
        c.list.Select(0)
        wait_for(app, lambda: live.shows() and "KiCad: power stage" in live.status())
        _, _, _, focus, context, layer, nets, zoom, lit = live.shows()[-1]
        net_of = {t.uuid: t.net for t in f.board.tracks} | {v.uuid: v.net for v in f.board.vias} | \
            {z.uuid: z.net for z in f.board.zones}
        lit_nets = {net_of[u] for u in lit if u in net_of}
        assert spec.switch_node in lit_nets and spec.rail in lit_nets and spec.ground not in lit_nets
        assert nets and zoom and "power flow" in live.status()
        c.converters = [replace(spec, name=f"S{k}", source="auto", reviewed=False) for k in range(4)]
        c._commit()
        for i in (0, 2):
            c.list.Select(i)
        c.on_reviewed(None)
        assert [s.reviewed for s in f.settings.config.converters] == [True, False, True, False]
        c.on_key(SimpleNamespace(GetKeyCode=lambda: wx.WXK_DELETE, Skip=lambda: None))
        assert [s.name for s in f.settings.config.converters] == ["S1", "S3"] and c.list.GetItemCount() == 2
    finally:
        close_window(f)


def test_a_long_status_message_wraps_instead_of_being_cut_off(app, frame):
    """The status bar grows to show a long message (a refused net's reason) on up to three lines, the whole text
    in its tooltip, and shrinks back for a short one; the gauge keeps its place on the right."""
    f = frame
    run_steps(app, f, [])
    f.SetSize((900, 600))
    one_line = f.status.GetSize().height
    text = ("/DCDC Stage/DC_OUT_RAIL_1 is a supply net, not a signal: the Nets tab is for clock and data nets, whose "
            "return current GroundTruth follows. Its current is analysed with the power stage buck SW1 on the "
            "Converters tab (Simulate: hot loop, conducted emission and the DC current density).")
    f.SetStatusText(text)
    wx.Yield()
    shown = f.status_label.GetLabel()
    assert f.GetStatusBar().GetStatusText(0) == text and shown.count("\n") >= 1
    assert shown.replace("\n", " ").split() == text.split()           # all of it, just wrapped
    assert f.status.GetSize().height > one_line and f.status_label.GetToolTipText() == text
    assert f.gauge.GetPosition().x > f.status_label.GetPosition().x + f.status_label.GetSize().width - 4
    f.SetStatusText(text * 4)                                          # too long even for three lines: cut
    wx.Yield()
    assert f.status_label.GetLabel().count("\n") == 2 and f.status_label.GetLabel().endswith("…")
    f.SetStatusText("ready")
    wx.Yield()
    assert f.status.GetSize().height == one_line and f.status_label.GetLabel() == "ready"


def test_a_clock_tick_sets_only_the_status_text(app, frame, monkeypatch):
    """During a Simulate the clock rewrites the status line every second, and every wx call gives up the GIL,
    which beside the solver's threads costs up to a timer tick to take back: the dozen calls a tick made stalled
    the window ~220 ms on kraft_flux_mini_2. A tick now sets the text (bar and label) and nothing else: the field,
    the tooltip and the label's size only when they change, the characters' widths measured once (they agree with
    wx's own measurement of the whole text). The KiCad inbox waits while a job runs."""
    f = frame
    run_steps(app, f, [])
    f.SetSize((900, 600))
    wx.Yield()
    f.SetStatusText("simulating… 0:01 elapsed, about 7:00 left")
    calls = []
    for name in ("SetToolTip", "SetSize", "SetLabelText"):
        real = getattr(f.status_label, name)
        monkeypatch.setattr(f.status_label, name, lambda *a, _n=name, _r=real: (calls.append(_n), _r(*a))[1])
    real_rect = f.status.GetFieldRect
    monkeypatch.setattr(f.status, "GetFieldRect", lambda *a: (calls.append("GetFieldRect"), real_rect(*a))[1])
    f.SetStatusText("simulating… 0:02 elapsed, about 6:59 left")
    assert calls == ["SetLabelText"] and f.status_label.GetLabel().endswith("6:59 left")
    text = "SPI_SCK: 1.234 GHz, board-wide model (34 000 nodes), solve 12 of ≤ 40 — µΩ²"
    dc = wx.ClientDC(f.status_label)
    dc.SetFont(f.status_label.GetFont())
    exact = dc.GetPartialTextExtents(text)
    assert all(abs(a - b) <= 2 + 0.02 * b for a, b in zip(f._char_extents(text), exact))
    taken = []
    monkeypatch.setattr(f, "_inbox", type("Inbox", (), {"take_requests": lambda self: taken.append(1) or []})())
    monkeypatch.setattr(f, "busy", lambda: True)
    f._check_inbox()
    assert taken == []
    monkeypatch.setattr(f, "busy", lambda: False)
    f._check_inbox()
    assert taken == [1]


def test_the_mouse_wheel_zooms_the_heatmap_and_a_double_click_shows_it_all(app, frame):
    """The user: "it would be good if we could use vector graph that is zoomable". The wheel over a map (Ctrl +
    wheel on the tab's scrolling page, the wheel alone in the enlarged window) zooms all layers about the point
    under the mouse; the view is rendered again in the background (the wheel handler returns at once) with the
    arrows laid out for it; a double-click (or the toolbar's Home) shows the whole map."""
    from matplotlib.backend_bases import MouseEvent
    f = frame
    run_steps(app, f, [lambda: f.run(True) and None])
    h = f.results_panel
    wait_rendered(f)
    ax = h.fig.axes[0]
    full = sorted(ax.get_xlim())
    x, y = ax.bbox.x0 + ax.bbox.width / 2, ax.bbox.y0 + ax.bbox.height / 2      # the middle of the first map
    t = time.perf_counter()
    for _ in range(3):                                          # three notches in
        h.canvas.callbacks.process("scroll_event", MouseEvent("scroll_event", h.canvas, x, y, button="up", step=1))
    assert time.perf_counter() - t < 0.2                        # nothing drawn on the GUI thread
    wait_rendered(f)
    x0, x1 = sorted(h.fig.axes[0].get_xlim())
    assert x1 - x0 == pytest.approx((full[1] - full[0]) / 1.25 ** 3, rel=0.02)
    assert all(sorted(a.get_xlim()) == pytest.approx([x0, x1]) for a in h.fig.axes if a in h.fig._gt_maps)
    ax = h.fig.axes[0]
    h.canvas.callbacks.process("button_press_event", MouseEvent("button_press_event", h.canvas, x, y, button=1,
                                                                dblclick=True))
    wait_rendered(f)
    assert sorted(h.fig.axes[0].get_xlim()) == pytest.approx(full)
    h.canvas.callbacks.process("scroll_event", MouseEvent("scroll_event", h.canvas, x, y, button="up", step=1))
    wait_rendered(f)
    h.toolbar.home()                                            # Home undoes the wheel zoom too
    wait_rendered(f)
    assert sorted(h.fig.axes[0].get_xlim()) == pytest.approx(full)


def _wheel(canvas, rotation: int, ctrl: bool = False) -> None:
    """A mouse wheel notch over the middle of the left half of a canvas, as wx delivers it."""
    e = wx.MouseEvent(wx.wxEVT_MOUSEWHEEL)
    e.SetEventObject(canvas)
    e.SetWheelRotation(rotation)
    e.SetWheelDelta(120)
    e.SetLinesPerAction(3)
    e.SetPosition(wx.Point(canvas.GetSize().width // 4, canvas.GetSize().height // 2))
    e.SetControlDown(ctrl)
    canvas.GetEventHandler().ProcessEvent(e)


def test_the_kpi_tiles_are_reused_between_views_and_the_modules_are_frozen_once(app, frame, monkeypatch):
    """Showing another view gives the KPI tiles already there its figures (Tile.update) instead of making them all
    again: window creation on the GUI thread blocked the window 210-360 ms after a Simulate on kraft_flux_mini_2.
    And once the window has its tabs, the objects alive then (the modules, the window) are in Python's permanent
    generation, which full garbage collections no longer walk (ui/gil.freeze_long_lived), once per process."""
    import gc
    from groundtruth.ui import gil, theme
    f = frame
    run_steps(app, f, [lambda: f.run(True) and None])
    wait_rendered(f)
    assert gil._frozen                          # done when the tabs were built (the count itself is not checked:
    #                                             test_plugin_start_never_blocks_its_window unfreezes everything)
    h = f.results_panel
    tiles = [t for t in h.tiles.GetChildren() if isinstance(t, theme.Tile)]
    first = h.tile_text()
    h._route_changed()                          # the view shown again: the same tiles, the same figures
    wait_rendered(f)
    again = [t for t in h.tiles.GetChildren() if isinstance(t, theme.Tile)]
    assert tiles and len(again) == len(tiles) and all(a is b for a, b in zip(tiles, again))
    assert h.tile_text() == first
    t = tiles[0]
    assert t.update("New title", "12.3 dB", "sub", "warn", "a tip") and t.value == "12.3 dB"
    assert t.GetToolTipText() == "a tip" and not t.update("New title", "12.3 dB", "sub", "warn", "a tip")
    calls = []
    monkeypatch.setattr(gc, "freeze", lambda: calls.append(1))
    monkeypatch.setattr(gil, "_frozen", False)
    gil.freeze_long_lived()
    gil.freeze_long_lived()                     # a second window (or call) freezes nothing more
    assert calls == [1]


def test_the_heatmap_tab_scrolls_through_full_width_plots(app, frame):
    """The user: "instead of having everything on one screen, make it scrollable and have every graph occupy the
    entire width of the application so the user can scroll down to see the other graphs". The maps, the
    cross-section along the route and the spectrum one under the other on a page that scrolls, each as wide as
    the page, at heights of their own (rendered in the background); the mouse over the section gives each layer's
    share there. The wheel scrolls the page, Ctrl + wheel zooms the map under the mouse. Unticking
    "Cross-section" leaves it out; a lower window keeps all three (the page scrolls further) and renders them
    again for the new size in the background."""
    from matplotlib.backend_bases import MouseEvent
    from groundtruth.report.plots import section_height_in
    from groundtruth.ui.results_panels import SPECTRUM_IN
    f = frame
    run_steps(app, f, [lambda: f.run(True) and None])
    h = f.results_panel
    wait_rendered(f)
    assert h.canvas3.IsShown() and h.canvas3.figure is h.fig3 and len(h.fig3.axes) >= 2
    page_w = h.page.GetClientSize().width
    assert all(abs(c.GetSize().width - page_w) <= 2 for c in (h.canvas, h.canvas3, h.canvas2))
    assert h.canvas.GetPosition().y < h.canvas3.GetPosition().y < h.canvas2.GetPosition().y
    # with its caption, as wrapped at the page's width
    assert h.canvas3.GetSize().height == h._px(section_height_in(h.board, h.current(), page_w / h._px(1.0)))
    assert h.canvas2.GetSize().height == h._px(SPECTRUM_IN)
    assert h.page.GetVirtualSize().height > h.page.GetClientSize().height          # it scrolls
    side = h.fig3.axes[1]
    assert [t.get_text() for t in side.get_yticklabels()] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    x, y = side.transData.transform((8.0, 1.0))
    h.canvas3.callbacks.process("motion_notify_event", MouseEvent("motion_notify_event", h.canvas3, x, y))
    assert h.readout.GetLabel().startswith("s 8.") and "In1.Cu" in h.readout.GetLabel(), h.readout.GetLabel()
    # the wheel over the maps scrolls the page and leaves the maps as they are; Ctrl + wheel zooms them
    _wheel(h.canvas, -120)
    assert h.page.GetViewStart()[1] == 3 and h._view is None and not h.rendering()
    _wheel(h.canvas, 120, ctrl=True)
    assert h._view is not None and h.page.GetViewStart()[1] == 3
    wait_rendered(f)
    h.reset_zoom()
    wait_rendered(f)
    h.page.Scroll(-1, 0)
    spectrum_y = h.canvas2.GetPosition().y
    h.section_box.SetValue(False)
    t = time.perf_counter()
    h.redraw()
    assert time.perf_counter() - t < 0.1                        # drawn in the background
    wait_rendered(f)
    assert not h.canvas3.IsShown() and h.canvas2.GetPosition().y < spectrum_y
    assert abs(h.canvas2.GetSize().width - page_w) <= 2
    h.section_box.SetValue(True)
    h.redraw()
    wait_rendered(f)
    assert h.canvas3.IsShown()
    # a lower window: the plots keep their heights (the maps down to MAPS_MIN_PX), the page scrolls further, and
    # the view is rendered again for the new size in the background
    size = f.GetSize()
    f.SetSize((size.width, 480))
    wait_for(app, lambda: h.page.GetClientSize().height < 300 and not h.rendering())
    assert h.canvas3.IsShown() and h.canvas3.GetSize().height == h._px(section_height_in(
        h.board, h.current(), h.page.GetClientSize().width / h._px(1.0)))
    assert tuple(h.canvas.figure.get_size_inches() * h.canvas.figure.dpi) == pytest.approx(
        tuple(h.canvas.GetSize()), abs=2)                       # rendered at the canvas's new size
    f.SetSize(size)
    wait_for(app, lambda: not h.rendering())


def test_open_3d_view_builds_the_page_in_the_background_and_opens_it(app, frame, tmp_path, monkeypatch):
    """"Open 3D view": the page (report/view3d.py) is built off the GUI thread into %LOCALAPPDATA%\\GroundTruth\\
    views and handed to the browser; the button is off while it builds."""
    import webbrowser
    opened = []
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    f = frame
    run_steps(app, f, [lambda: f.run(True) and None])
    h = f.results_panel
    wait_rendered(f)
    assert h.view3d_button.IsEnabled()
    t = time.perf_counter()
    h.open_3d()
    assert time.perf_counter() - t < 0.1 and not h.view3d_button.IsEnabled()
    wait_for(app, lambda: opened and h.view3d_button.IsEnabled(), timeout=60)
    (page,) = (tmp_path / "local" / "GroundTruth" / "views").glob("view3d_*.html")
    assert opened == [page.as_uri()] and "3D view opened" in f.GetStatusBar().GetStatusText(0)


def test_parts_table_fills_the_tab_and_an_edit_updates_its_row_in_place(app, frame):
    """The columns fill the tab's width (the user: "expand the table across the whole plane"). Changing a part's
    model updates its own row (R becomes editable for a resistor) without rebuilding the table: the rebuild
    destroyed a cell editor the user had already opened elsewhere, and KiCad showed wx's "'SetFocus' failed"."""
    import wx.grid as gridlib
    from groundtruth.ui.parts_panel import NOTE_COL
    f = frame
    run_steps(app, f, [])
    f.notebook.SetSelection(f.notebook.FindPage(f.parts))
    g = f.parts.grid
    wait_for(app, lambda: abs(sum(g.GetColSize(i) for i in range(g.GetNumberCols())) - g.GetClientSize().width) <= 1)
    row = f.parts.rows.index("U1")
    other = f.parts.rows.index("U2")
    assert g.IsReadOnly(row, 4)                                 # R: not a parameter of U1's model yet
    g.SetCellValue(row, 3, "resistor")
    g.GetEventHandler().ProcessEvent(gridlib.GridEvent(g.GetId(), gridlib.wxEVT_GRID_CELL_CHANGED, g, row, 3))
    g.SetGridCursor(other, NOTE_COL)                            # the user goes straight on to another cell
    g.EnableCellEditControl()
    wait_for(app, lambda: not g.IsReadOnly(row, 4))             # the edited row, updated in place
    assert g.IsCellEditControlEnabled() and f.parts.rows.index("U2") == other
    assert f.settings.config.components["U1"].model.kind == "resistor"
    g.DisableCellEditControl()


def test_the_parts_tab_follows_the_footprints_selected_in_kicad(app, tmp_path, monkeypatch):
    """Click a part in KiCad: while the Parts tab shows, its row gets selected (a filter hiding it is cleared) with
    the cursor on its Model cell; KiCad is only asked while that tab shows, never while a KiCad job holds the
    connection, and an unchanged selection does not move the table again."""
    from groundtruth.adapters import kipy_source
    live = LiveSlotBoard(app, tmp_path, monkeypatch, delay=0.0)
    f = live.frame
    asked, kicad = [], {"refs": []}

    def selected_references(kb, wait=0.0):
        asked.append(time.perf_counter())
        return list(kicad["refs"])
    monkeypatch.setattr(kipy_source, "selected_references", selected_references)
    try:
        t = time.perf_counter()
        wait_for(app, lambda: time.perf_counter() - t > 1.2)
        assert not asked                                        # the Nets tab shows: KiCad is not asked
        f.notebook.SetSelection(f.notebook.FindPage(f.parts))
        f.parts.filter.SetValue("U1")                           # a filter that hides U2
        kicad["refs"] = ["U2"]                                  # the user clicks U2 in KiCad
        wait_for(app, lambda: "selected in KiCad: U2" in live.status())
        g = f.parts.grid
        row = f.parts.rows.index("U2")
        assert f.parts.filter.GetValue() == "" and g.GetSelectedRows() == [row]
        assert (g.GetGridCursorRow(), g.GetGridCursorCol()) == (row, 3)          # the Model cell
        g.SelectRow(0)                                          # the user picks another row in the table...
        n = len(asked)
        wait_for(app, lambda: len(asked) >= n + 2)
        assert g.GetSelectedRows() == [0]                       # ...an unchanged KiCad selection leaves it
        with f.session.kicad_lock:                              # a KiCad job holds the connection: no look
            n = len(asked)
            t = time.perf_counter()
            wait_for(app, lambda: time.perf_counter() - t > 1.2)
            assert len(asked) == n
        kicad["refs"] = ["U1", "U2"]
        wait_for(app, lambda: "selected in KiCad: U1, U2" in live.status())
        assert sorted(g.GetSelectedRows()) == sorted(f.parts.rows.index(r) for r in ("U1", "U2"))
    finally:
        close_window(f)


def test_the_window_builds_its_tabs_in_steps(app, tmp_path, monkeypatch):
    """The tabs' ~200 native controls cost the GUI thread ~0.4 s, three times that when created inside the shown
    window: the window appears at once and builds one tab per event-loop step in a hidden holder, then moves it in
    (a step stays far below the 0.5 s that reads as a stall); the board is read after the last tab."""
    from groundtruth.ui import main_frame
    steps = []
    orig = main_frame.MainFrame._build_pages

    def timed(self):
        t = time.perf_counter()
        orig(self)
        steps.append(time.perf_counter() - t)
    monkeypatch.setattr(main_frame.MainFrame, "_build_pages", timed)
    _, _, path = make(tmp_path, layer_change_board(None), "steps")
    t = time.perf_counter()
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    built_in = time.perf_counter() - t
    f.Show()
    try:
        assert built_in < 0.15 and not f.pages_built() and f.busy()
        run_steps(app, f, [])
        assert f.pages_built() and f.board is not None
        assert [f.notebook.GetPageText(i) for i in range(f.notebook.GetPageCount())] == \
            ["Signal paths", "Converters", "Bridges", "DC loads", "Power integrity", "Signal integrity", "Parts",
             "Results", "Issues", "Fixes", "History", "Server jobs"]
        # no compute server in Preferences (the tests' own): the Server jobs tab is hidden, its page still there
        assert f.notebook.hidden == {f.notebook.FindPage(f.server_jobs)}
        # 12 tabs (DC loads since 2026-10-15, Power integrity since 2026-10-16, Signal integrity since 2026-10-17);
        # the forms and plots in steps of their own (`build_steps`): Bridges 5 (0.32-0.43 s in one), Converters 4
        # (155-240 ms in one on a kraft_flux_mini_2 copy; its fields in two since the spread spectrum's, 125-146 ms in
        # one), Results 2 (its three canvases ~110 ms), Nets 2 (130-170 ms in one), DC loads 1, Power integrity 2
        # (128-191 ms in one), Signal integrity 3 (149-160 ms in one): every step under 100 ms (build_profile.py)
        assert len(steps) == 30 and max(steps) < 0.35, [f"{s:.3f}" for s in steps]
        assert [w for w in wx.GetTopLevelWindows() if w is not f and not w.GetTitle()] == []   # holder gone
    finally:
        close_window(f)


def test_sharper_map_replaces_the_shown_view_in_the_background(app, frame):
    """Results tab, "Sharper map": the shown view is solved again on a finer grid as a job (the window keeps
    running), then replaces the view; the summary line says so, and a second click says it is sharp already."""
    f = frame
    run_steps(app, f, [lambda: f.run(True) and None])
    h = f.results_panel
    k = h.choice.GetSelection()
    before = h.routes[k]
    ticks = run_steps(app, f, [lambda: f.on_sharper(k)])
    after = h.routes[k]
    assert after is not before and after.sharper and after.grid.d < 0.75 * before.grid.d
    assert f.routes[k] is after                                 # the window's own list is the same one
    wait_rendered(f)
    assert h.metrics.GetLabel().startswith("sharper map:") and "sharper map" in f.GetStatusBar().GetStatusText(0)
    assert longest_busy_gap(ticks) < 0.5
    f.on_sharper(k)
    assert "already sharper" in f.GetStatusBar().GetStatusText(0) and not f.busy()


def test_drawn_buttons_click_like_buttons_and_the_top_bar_uses_them(app, frame, tmp_path, monkeypatch):
    """theme.Button (the plugin's own, with some volume): a click, Space or Enter sends wx.EVT_BUTTON, a disabled
    one sends nothing. The top bar is made of them (Simulate the primary action, Cancel off while idle), and its
    Theme choice is saved for the next start."""
    from types import SimpleNamespace
    from groundtruth.ui import theme
    f = frame
    run_steps(app, f, [])
    clicks = []
    b = theme.Button(f.topbar, "Try me")
    b.Bind(wx.EVT_BUTTON, lambda e: clicks.append(e.GetEventObject()))
    b.click()
    b._key(SimpleNamespace(GetKeyCode=lambda: wx.WXK_SPACE, Skip=lambda: None))
    assert clicks == [b, b] and b.GetBestSize().height >= 30
    b.Enable(False)
    b.click()
    assert len(clicks) == 2
    b.Destroy()
    assert isinstance(f.t_sim, theme.Button) and f.t_sim.kind == "primary" and f.t_cancel.kind == "danger"
    assert f.t_sim.IsEnabled() and not f.t_cancel.IsEnabled()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(theme.sys, "platform", "win32")
    # Preferences: the dialog shows the saved values; OK saves its values, a new theme applies next time
    from groundtruth.ui.prefs_dialog import PreferencesDialog, defaults
    with PreferencesDialog(f, theme.prefs(), cell_mm=0.4) as dlg:
        assert dlg.values() == defaults({}) and "4×  (0.20 × 0.20 mm)" in dlg.resolution.GetStrings()
        # two columns side by side (Simulation left, Heatmaps right) on a pane that scrolls on a short screen
        left, right = dlg.resolution.GetParent(), dlg.via_markers.GetParent()
        assert left.GetPosition().y == right.GetPosition().y and left.GetPosition().x < right.GetPosition().x
        assert isinstance(left.GetParent(), wx.ScrolledWindow)
        dlg.theme.SetSelection(2)
        dlg.resolution.SetSelection(2)
        dlg.outlines.SetValue(True)
        values = dlg.values()
    f.apply_preferences(values)
    assert theme.load_pref() == "dark" and "next time" in f.GetStatusBar().GetStatusText(0)
    assert f.resolution() == 4 and f.results_panel.outlines and theme.prefs()["outlines"] is True
    assert not f.results_panel.names and theme.prefs()["names"] is False         # a switch of its own
    assert defaults({"outlines": True})["names"] is True       # before it had one, the outlines brought the names


def test_thermal_button_adds_a_temperature_view_and_its_issues(app, conv_frame):
    """Top bar, "Thermal": after Simulate, the power stage's DC paths and losses heat the board in a job; the
    Results tab gets a temperature view (tiles: hottest copper, total loss), its issues join the list, and a
    second run replaces the view instead of adding another."""
    pytest.importorskip("pyamg")
    from dataclasses import replace
    f = conv_frame

    def add():
        p = f.converters
        for ctrl, v in ((p.name, "buck"), (p.rail, "VIN"), (p.ground, "GND"), (p.integrated, "U1"),
                        (p.f_sw, "200kHz"), (p.i_out, "2"), (p.v_in, "12"), (p.rise, "10ns"), (p.inp, "J1.1, J1.2"),
                        (p.rds_on, "50m")):
            ctrl.SetValue(v)
        p.on_add(None)

    def draft():                                  # keep the test quick
        f.settings.config.thermal = replace(f.settings.config.thermal, quality="draft", warn_c=26.0)
    run_steps(app, f, [add, draft, lambda: f.run(True) and None])
    assert f.settings.config.converters[0].rds_on == pytest.approx(0.05)
    ticks = run_steps(app, f, [lambda: f.on_thermal()])
    th = [r for r in f.routes if getattr(r, "kind", "") == "thermal"]
    assert len(th) == 1 and th[0].t_max > th[0].ambient_c and not th[0].error
    assert f.results_panel.current() is th[0] and "hottest copper" in f.results_panel.tile_text()
    assert any(i.check in ("thermal_hotspot", "part_temperature") for i in f.issues)    # warn_c 26 °C: it warns
    assert longest_busy_gap(ticks) < 0.5
    run_steps(app, f, [lambda: f.on_thermal()])
    assert len([r for r in f.routes if getattr(r, "kind", "") == "thermal"]) == 1
    # Results tab, "Current capacity": a view per stage at its capacity, next to the thermal view; tiles say it
    limit = th[0].t_max + 10.0
    f.settings.config.thermal = replace(f.settings.config.thermal, capacity_c=limit)
    f._views_from = (f._views_from[0], f.settings.config)
    ticks = run_steps(app, f, [lambda: f.results_panel.capacity_button.click()])
    cap = [r for r in f.routes if getattr(r, "capacity", None)]
    assert len(cap) == 1 and cap[0].capacity["stage"] == "buck" and cap[0].capacity["scale"] > 1
    assert f.results_panel.current() is cap[0] and "current capacity" in f.results_panel.tile_text()
    choice = f.results_panel.choice
    assert choice.GetString(choice.GetSelection()).startswith("Thermal: buck at its capacity")
    assert "capacity" in f.GetStatusBar().GetStatusText(0)
    assert longest_busy_gap(ticks) < 0.5
    run_steps(app, f, [lambda: f.on_thermal()])          # Thermal again keeps the capacity view
    assert len([r for r in f.routes if getattr(r, "capacity", None)]) == 1
    assert len([r for r in f.routes if getattr(r, "kind", "") == "thermal"]) == 2


def test_the_bridges_tab_detects_a_motor_bridge_simulates_it_and_shows_its_drive(app, tmp_path, monkeypatch):
    """The user: "another tab for half bridges ... where those nets are auto discovered". A board with a motor
    bridge and nothing in its settings: the bridge is detected in the background and listed (orange: its operating
    point and motor are not set); Simulate says what is missing and shows the Bridges tab; set from the form, the
    bridge is simulated: the Results tab shows its drive (currents, tiles, the capacitors' ripple bars), then its
    legs; map actions on the drive view ask for a leg."""
    from types import SimpleNamespace as NS
    from boardgen import bridge_board
    from groundtruth.ui import main_frame
    messages = []
    monkeypatch.setattr(wx, "MessageBox", lambda msg, *a, **k: messages.append(msg) or wx.YES)
    _, _, path = make(tmp_path, bridge_board(), "br", nets=(), max_nodes=20000)
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        p = f.bridges
        assert [b.name for b in p.bridges] == ["bridge J1"] and p.list.GetItemText(0, 1) == "auto, SET UP"
        assert f.settings.config.bridges[0].input_connector == ["J2.1", "J2.2"]
        assert f.settings.config.model_for("U9").pins == ["IP+", "IP-"]
        f.run(True)
        assert "set the bus voltage (v_bus)" in messages[-1]
        assert f.notebook.GetSelection() == f.notebook.FindPage(p)
        p.list.Select(0)
        p.on_select(NS(GetIndex=lambda: 0))
        assert p.leg_list.GetItemCount() == 3 and p.leg_list.GetItemText(0, 1) == "PHA"
        for ctrl, v in ((p.v_bus, "48"), (p.f_pwm, "40kHz"), (p.i_phase, "30"), (p.speed, "3000"),
                        (p.r_ll, "50m"), (p.l_ll, "50uH"), (p.ke_ll, "5"), (p.poles, "7"), (p.c_oss, "1n"),
                        (p.sweep_i, "15, 30A")):
            ctrl.SetValue(v)
        p.on_update(None)
        b = f.settings.config.bridges[0]
        assert b.reviewed and b.v_bus == 48 and b.motor.l_ll == pytest.approx(50e-6) and len(b.legs) == 3
        assert b.sweep_i_phase == [15.0, 30.0] and b.sweep_speed_rpm == []
        ticks = run_steps(app, f, [lambda: f.run(True) and None])
        h = f.results_panel
        wait_rendered(f)
        names = [h.choice.GetString(i) for i in range(h.choice.GetCount())]
        assert names[0] == "bridge J1  (drive and bus)" and [n.split()[2] for n in names[1:4]] == ["A", "B", "C"]
        assert h.current().kind == "bridge" and "drive duty" in h.tile_text() and "bus ripple" in h.tile_text()
        assert h.canvas3.IsShown()                                 # the capacitors' ripple bars
        f.on_draw_arrows(h.current(), None)
        assert "pick one of its legs" in f.GetStatusBar().GetStatusText(0)
        assert names[4] == "bridge J1  RMS currents"              # the bridge's RMS current density map
        h.choice.SetSelection(4)
        h._route_changed()
        wait_rendered(f)
        assert "RMS current density" in h.tile_text() and "copper loss" in h.tile_text()
        assert names[5] == "bridge J1  sweep (phase current × speed)"   # the sweep: its plots and worst points
        h.choice.SetSelection(5)
        h._route_changed()
        wait_rendered(f)
        assert "operating points" in h.tile_text() and not h.canvas3.IsShown()
        assert longest_busy_gap(ticks) < 0.5
    finally:
        close_window(f)


def test_each_detection_tab_explains_its_labels_and_shows_the_boards(app, tmp_path, monkeypatch):
    """The user: "Under each tab, eg converter, add an info field that explains the nomenclature for the specific
    tab". Nets, Converters and Bridges each have a one-line info box (shown on request, quickly) with the tab's own
    help; the Converters and Bridges ones say what this board's GT labels gave, and what is wrong with them."""
    from boardgen import bridge_board
    from groundtruth.ui import main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    g = bridge_board()
    for k in range(3):
        g.label(f"Q{2 * k + 1}", "hb_hs", f"Phase {'ABC'[k]}")
        g.label(f"Q{2 * k + 2}", "hb_ls", f"Phase {'ABC'[k]}")
    g.label("C11", "conv_hss")
    _, _, path = make(tmp_path, g, "lab", nets=(), max_nodes=20000)
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        for panel, words in ((f.nets, "SCK"), (f.converters, "conv_hs"), (f.bridges, "hb_hs")):
            box = panel.info
            assert not box.shown()
            t0 = time.perf_counter()
            box.toggle()
            assert box.shown() and time.perf_counter() - t0 < 0.3 and words in box.body.GetValue()
            box.toggle()
            assert not box.shown()
        status = f.bridges.info.status.GetLabel()
        assert "bridge legs Phase A, Phase B, Phase C" in status and "C11: GT 'conv_hss'" in status
        assert f.converters.info.status.GetLabel() == status
        assert f.bridges.bridges and len(f.bridges.bridges[0].legs) == 3
    finally:
        close_window(f)


def test_idle_time_work_waits_for_the_user(app, tmp_path, monkeypatch):
    """After a Simulate the history's report page (its plots hold the GIL while they are drawn) and the garbage
    collection of the replaced results (gil.settle_garbage) wait until the user has left the mouse and keyboard
    alone for idle.QUIET_S (ui/idle.py): while input keeps coming, neither happens; once it stops, both do."""
    import gc
    from groundtruth.ui import idle, main_frame
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))       # preferences at their defaults: history on
    monkeypatch.setattr(idle, "QUIET_S", 1.0)
    busy_user = {"on": True}
    monkeypatch.setattr(idle, "input_idle_s", lambda: 0.0 if busy_user["on"] else 60.0)
    settled = []
    monkeypatch.setattr("groundtruth.ui.gil.settle_garbage", lambda: settled.append(gc.get_freeze_count()))
    _, _, path = make(tmp_path, layer_change_board(None), "idle", solve_points=2)
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [lambda: f.run(True) and None])
        assert "kept in the History tab" in f.GetStatusBar().GetStatusText(0)
        tab = f.history
        wait_for(app, lambda: tab.list.GetItemCount() == 1, timeout=60)
        end = time.time() + 2.0
        while time.time() < end:                     # the user keeps working: nothing is drawn or collected
            app.Yield()
            time.sleep(0.02)
        run = tab.runs[0]
        assert f.history_busy() and not tab.reports.get(run.id) and not settled
        assert f._settle_timer.IsRunning()
        busy_user["on"] = False                       # the user stops: both go on
        wait_for(app, lambda: not f.history_busy() and tab.reports.get(run.id), timeout=120)
        wait_for(app, lambda: bool(settled), timeout=10)
        assert not f._settle_timer.IsRunning() and len(settled) == 1
    finally:
        close_window(f)


def test_simulate_keeps_each_run_in_the_history_tab(app, tmp_path, monkeypatch):
    """Preferences › History (on by default): each Simulate is kept next to the board: a copy of the board (one
    for runs that did not change it), the settings, the results and, in the background, the report page. The
    History tab lists the runs with what changed, keeps a note, compares two, opens a run's report and board copy,
    and deletes a run."""
    from dataclasses import replace
    from pathlib import Path
    from groundtruth.report.history import history_for
    from groundtruth.ui import main_frame
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))       # preferences at their defaults: history on
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    opened = []
    monkeypatch.setattr(wx, "LaunchDefaultBrowser", lambda url, *a: opened.append(url) or True)
    monkeypatch.setattr(wx, "LaunchDefaultApplication", lambda p, *a: opened.append(p) or True)
    _, _, path = make(tmp_path, layer_change_board(None), "hist", solve_points=2)
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        def slower_edges():
            f.settings.set_nets([replace(n, rise_time=2e-9) for n in f.settings.config.nets])
            f.on_config_change()
        run_steps(app, f, [lambda: f.run(True) and None])
        assert "kept in the History tab" in f.GetStatusBar().GetStatusText(0)
        run_steps(app, f, [slower_edges, lambda: f.run(True) and None])
        tab = f.history
        wait_for(app, lambda: not f.history_busy() and tab.list.GetItemCount() == 2
                 and all(tab.reports.get(r.id) for r in tab.runs), timeout=120)
        history = history_for(path)
        runs = history.runs()
        assert [r.id for r in tab.runs] == [r.id for r in runs]
        assert [f.name for f in history.boards_dir.iterdir()] == [f"{runs[1].id}.kicad_pcb.gz"]   # unchanged board:
        assert runs[0].board_name == runs[1].board_name                       # one copy, named after the first run
        assert history.board_bytes(runs[0]) == path.read_bytes()
        assert runs[0].meta["by"] == "plugin" and runs[0].meta["board"]["source"] == "file"
        assert "rise_time: 2ns" in runs[0].settings_text() and "rise_time: 1ns" in runs[1].settings_text()
        assert "<!doctype html>" in runs[0].report.read_text(encoding="utf-8")[:100]
        assert tab.list.GetItemText(0, 2) == "settings" and tab.list.GetItemText(1, 2) == "first"
        assert tab.list.GetItemText(0, 10) == "yes"

        tab.list.Select(0)                                 # the newest: a note, then against the run before it
        tab._selection_changed()
        tab.note.SetValue("slower edges")
        tab._save_note()
        wait_for(app, lambda: not tab.busy() and tab.list.GetItemText(0, 1) == "slower edges")
        assert history.find("1").note == "slower edges"
        tab._on_button("compare")
        wait_for(app, lambda: tab.comparison is not None)
        assert (tab.comparison["a"], tab.comparison["b"]) == (runs[1].id, runs[0].id)
        assert tab.cmp_list.GetItemCount() >= 1 and "the settings changed" in tab.cmp_text.GetValue()
        tab._on_button("report")                           # looked at in the background, then opened
        wait_for(app, lambda: not tab.busy() and len(opened) == 1)
        assert opened[-1] == runs[0].report.as_uri()
        tab._on_button("board")
        wait_for(app, lambda: not tab.busy() and len(opened) == 2)
        assert Path(opened[-1]).read_bytes() == path.read_bytes()

        tab.list.Select(0, False)
        tab.list.Select(1)                                 # the oldest goes
        tab._selection_changed()
        tab._on_button("delete")
        wait_for(app, lambda: not tab.busy() and tab.list.GetItemCount() == 1)
        assert [r.id for r in history.runs()] == [runs[0].id]
        assert tab.list.GetItemText(0, 2) == "first"

        # files deleted by hand while the window is open: the status line says what is gone, nothing raises
        import shutil
        status = lambda: f.GetStatusBar().GetStatusText(0)            # noqa: E731
        left = history.runs()[0]
        left.report.unlink()                                          # listed with its report a moment ago
        tab.list.Select(0)
        tab._selection_changed()
        tab._on_button("report")
        wait_for(app, lambda: not tab.busy() and "no longer there" in status())
        wait_for(app, lambda: tab.list.GetItemText(0, 10) == "–")     # the list was read again
        history.board_copy_path(left).unlink()
        tab._on_button("board")
        wait_for(app, lambda: not tab.busy() and "missing or damaged" in status())
        shutil.rmtree(history.root)                                   # the whole folder
        tab._on_button("delete")                                      # the selected run, already gone
        wait_for(app, lambda: not tab.busy() and tab.list.GetItemCount() == 0)
        assert "No runs yet" in tab.where.GetLabel()
        run_steps(app, f, [lambda: f.run(True) and None])             # the next Simulate starts it again
        wait_for(app, lambda: not f.history_busy() and tab.list.GetItemCount() == 1
                 and all(tab.reports.get(r.id) for r in tab.runs), timeout=120)
        assert history.board_bytes(history.runs()[0]) == path.read_bytes()
    finally:
        close_window(f)


def test_the_board_copy_comes_from_kicad_with_unsaved_edits_else_from_the_saved_file(tmp_path):
    """Live mode: the history's board copy is KiCad's board text (a read-only request, unsaved edits included);
    when KiCad cannot hand it over, the saved file, and the record says which."""
    from types import SimpleNamespace
    from groundtruth.ui import main_frame
    saved = tmp_path / "b.kicad_pcb"
    saved.write_bytes(b"(kicad_pcb saved)")

    def fail():
        raise RuntimeError("message too large")
    pytest.importorskip("kipy")                     # the request goes through adapters/kipy_source
    live = main_frame.Session(kicad_board=SimpleNamespace(get_as_string=lambda: "(kicad_pcb edited)"))
    assert live.board_copy(str(saved)) == (b"(kicad_pcb edited)", "kicad")
    broken = main_frame.Session(kicad_board=SimpleNamespace(get_as_string=fail))
    assert broken.board_copy(str(saved)) == (b"(kicad_pcb saved)", "saved file")
    data, why = broken.board_copy(str(tmp_path / "gone.kicad_pcb"))
    assert data is None and "message too large" in why
    assert main_frame.Session(file_path=str(saved)).board_copy(str(saved)) == (b"(kicad_pcb saved)", "file")


def test_a_row_unticked_on_the_nets_tab_is_kept_but_left_out_of_simulate(app, frame, monkeypatch):
    """The tick box at the start of a row: unticked, the net stays with all its values (saved as enabled: false,
    greyed) and Simulate leaves it out; with every row off, Simulate says so; ticked again, it is analysed."""
    from groundtruth.config.settings import SettingsFile
    from groundtruth.ui import theme
    f = frame
    messages = []
    monkeypatch.setattr(wx, "MessageBox", lambda msg, *a, **k: messages.append(msg) or wx.OK)
    run_steps(app, f, [])                                       # the board and settings are read
    lst = f.nets.list
    assert lst.IsItemChecked(0) and f.settings.config.nets[0].enabled
    lst.CheckItem(0, False)                                     # the user unticks the row
    wx.Yield()
    if f.settings.config.nets[0].enabled:                       # a platform that sends no event for CheckItem
        f.nets._ticked(0, False)
    assert not f.settings.config.nets[0].enabled and not lst.IsItemChecked(0)
    assert lst.GetItemTextColour(0) == theme.colour(theme.palette().muted) and lst.GetItemText(0, 1).startswith("off")
    assert f._saver.flush()
    assert not SettingsFile.load(f.settings.path).config.nets[0].enabled     # kept in the file, switched off
    f.run(True)
    assert "switched off" in messages[-1] and not f.busy()      # nothing left to simulate
    f.nets._ticked(0, True)
    run_steps(app, f, [lambda: f.run(True) and None])
    assert f.settings.config.nets[0].enabled and [r.net for r in f.routes if r.kind == "route"]


def test_the_nets_form_keeps_the_source_and_load_on_update(app, frame):
    """The Nets form's source (R, L, C), fall time, load (R, C) and Pass through are shown for the selected net and
    written by Update selected. Before, Update dropped a net's driver_ohms and load_pf from the settings file."""
    from dataclasses import replace
    f = frame
    run_steps(app, f, [])
    base = f.settings.config.nets[0]
    f.nets.nets = [replace(base, driver_ohms=40.0, load_pf=7.0, driver_nh=1.5, current_a=0.01)]
    f.nets._commit()
    f.nets.list.Select(0)
    app.Yield()
    assert (f.nets.src_r.GetValue(), f.nets.load_c.GetValue(), f.nets.src_l.GetValue()) == ("40", "7", "1.5")
    f.nets.load_r.SetValue("50")
    f.nets.fall.SetValue("3ns")
    f.nets.on_update(None)
    n = f.settings.config.nets[0]
    assert (n.driver_ohms, n.load_pf, n.driver_nh, n.load_ohms, n.current_a) == (40.0, 7.0, 1.5, 50.0, 0.01)
    assert abs(n.fall_time - 3e-9) < 1e-15
    f.nets.src_c.SetValue("abc")
    assert f.nets._form() is None                              # refused with a message (patched by the fixture)


def test_a_supply_looking_net_picked_in_kicad_can_be_added_anyway(app, frame, monkeypatch):
    """Add KiCad selection on a net detection takes for a supply asks whether to add it as a signal anyway: Yes adds
    it (unreviewed), No leaves it out with the reason in the status line."""
    f = frame
    run_steps(app, f, [])
    import re
    from groundtruth.analysis import detect
    monkeypatch.setattr(detect, "POWER_NAME", re.compile(r"^SIG$"))     # SIG looks like a supply by its name
    f.settings.set_nets([])
    answers = iter([wx.NO, wx.YES])                              # to the Yes/No question only

    def box(message, caption="", style=0, *a, **k):
        return next(answers) if style & wx.YES_NO else wx.OK
    monkeypatch.setattr(wx, "MessageBox", box)
    f.add_nets(["SIG"])
    assert not any(n.name == "SIG" for n in f.settings.config.nets)
    f.add_nets(["SIG"])
    assert any(n.name == "SIG" and not n.reviewed for n in f.settings.config.nets)


def test_the_draw_buttons_before_any_simulate_say_so(app, frame):
    """The Results tab's Draw contours / Draw arrows buttons are enabled once the board is read, before any
    Simulate: with no view they used to fail on None (rr.kind before the None check)."""
    f = frame
    run_steps(app, f, [])
    assert f.results_panel.current() is None
    for b in (f.results_panel.draw_button, f.results_panel.arrow_button):
        f.SetStatusText("")
        b.click()
        assert "run Simulate first" in f.status_text()
        assert not f.busy()


def test_every_button_works_before_and_after_a_simulate(app, frame, monkeypatch):
    """Every enabled button of every tab clicked with nothing to work on yet (no results, nothing selected, empty
    forms), then again after a Simulate: none may fail (an exception in a wx handler only prints, so the hook
    collects them). Modal dialogs answer Cancel; Suggest fixes and Compare are left out (slow, tested on their
    own). Found: an empty Converters form's Add left an invalid stage in the settings document, and every later
    change (the Parts tab's Apply) then failed on it (settings._replace); wx's complaints about the Preferences
    dialog's rows (their parent) are checked too."""
    from groundtruth.ui import theme
    f = frame
    errors, logged = [], []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: errors.append(f"{t.__name__}: {e}"))
    # every dialog class a button opens answers Cancel: wxPython wraps each one's ShowModal separately, so one left
    # out here waits for a click (the DC loads tab's Fill from net… picks with Single/MultiChoiceDialog: the full
    # suite hung there on 2026-10-16)
    for cls in (wx.Dialog, wx.FileDialog, wx.TextEntryDialog, wx.MessageDialog, wx.SingleChoiceDialog,
                wx.MultiChoiceDialog):
        monkeypatch.setattr(cls, "ShowModal", lambda self: wx.ID_CANCEL)

    class Log(wx.Log):
        def DoLogTextAtLevel(self, level, msg):
            """Collect wx's complaints about how windows were built (logged at debug level)."""
            if "should be created as child" in msg:
                logged.append(msg)
    old_log = wx.Log.SetActiveTarget(Log())
    try:
        run_steps(app, f, [])
        skip = {"Suggest fixes", "Compare what-if", "Simulate", "Reload board", "Cancel"}
        clicked = []

        def buttons(w):
            """Every drawn button in the window, depth first."""
            for c in w.GetChildren():
                if isinstance(c, theme.Button):
                    yield c
                yield from buttons(c)

        def clicks(name):
            """One step per button: click it if it is enabled, noting which raised."""
            def step(b):
                n = len(errors)
                if b and b.IsEnabled():
                    b.click()
                clicked.append((name, b.GetLabel() if b else "?", len(errors) - n))
            return [lambda b=b: step(b) for b in buttons(f) if b.GetLabel() not in skip]

        run_steps(app, f, clicks("before"))
        run_steps(app, f, [lambda: f.run(True)])
        assert f.routes
        run_steps(app, f, clicks("after"))
    finally:
        wx.Log.SetActiveTarget(old_log)
        for w in wx.GetTopLevelWindows():       # windows the buttons opened (help, the heatmap window)
            if w is not f and isinstance(w, wx.Frame) and w.GetParent() is f:
                w.Close()
    assert not errors, ([(n, lab) for n, lab, k in clicked if k], errors)
    assert not logged, logged
    assert len(clicked) > 40
    assert not f.settings.doc.get("converters")  # the empty form's Add left nothing in the document
    from groundtruth.config.settings import SettingsFile
    SettingsFile.load(f.settings.path).config     # what was saved still loads


def test_detect_offers_to_replace_a_stale_unreviewed_net(app, tmp_path):
    """The Nets tab's Detect… dialog: an unreviewed detected entry whose path detection now sees differently is
    offered again, ticked, marked REPLACES; choosing it replaces the entry in place (its tick kept) and removes
    the unreviewed pieces inside its path (kraft_flux_mini_2's /CPU/ADC_I_RAIL_1 after the op-amp labels)."""
    sys.path.insert(0, os.path.dirname(__file__))
    from test_path import _labelled_opamp_board
    from groundtruth.analysis.detect import detect_nets
    from groundtruth.config.settings import NetSpec
    from groundtruth.ui.nets_panel import DetectDialog, _merged
    board = _labelled_opamp_board()
    from groundtruth.config.settings import SettingsFile
    sc = SettingsFile.new(tmp_path / "amp.groundtruth.yaml", board)
    found = detect_nets(board, sc.config, 0)
    name = next(d for d in found if d.spec.driver == "U1.1").spec.name
    piece = next(n for n in ("SIG_B", "SIG_C", "SIG_D") if n != name)
    nets = [NetSpec(name, "R2.2", ["U2.1"], 10e6, 2e-9, source="auto", reviewed=False, enabled=False),
            NetSpec(piece, "R1.2", ["U5.3"], 10e6, 2e-9, source="auto", reviewed=False)]
    dlg = DetectDialog(None, found, nets)
    try:
        rows = [dlg.list.GetItemText(k) for k in range(dlg.list.GetItemCount())]
        k = rows.index(name)
        assert dlg.list.IsItemChecked(k) and "REPLACES" in dlg.list.GetItemText(k, 7)
        chosen, fragments = dlg.selected(), dlg.fragments
    finally:
        dlg.Destroy()
    merged = _merged(nets, chosen, found, fragments)
    assert [n.name for n in merged][0] == name and piece not in [n.name for n in merged]
    assert merged[0].driver == "U1.1" and merged[0].enabled is False


def test_the_signal_paths_tab_shows_each_chain_and_the_selected_path(app, frame):
    """The Signal paths tab: the Chain column filled from the background thread (driver → parts → receivers),
    and the selected row in full under the table."""
    f = frame
    run_steps(app, f, [])
    wait_for(app, lambda: bool(f.nets.chains))
    chain = f.nets.list.GetItemText(0, 2)
    assert "→" in chain and "…" not in chain and chain.startswith(f.nets.nets[0].driver), chain
    f.nets.select_only(0)
    wait_for(app, lambda: f.nets.path_text.GetValue().startswith("Path: "))
    assert f.nets.nets[0].driver in f.nets.path_text.GetValue()


def test_the_bode_button_sweeps_the_selected_path_over_its_own_range(app, frame):
    """Signal paths tab: Sweep from / to / points in the form (Update selected keeps them in the settings), "Bode"
    sweeps the selected path as a job (no Simulate) and the Results tab shows its Bode / poles and zeros view, the
    gain tile first; a second sweep replaces that view."""
    f = frame
    run_steps(app, f, [])
    f.nets.select_only(0)
    run_steps(app, f, [])
    f.nets.sweep_from.SetValue("10Hz")
    f.nets.sweep_to.SetValue("1GHz")
    f.nets.sweep_pts.SetValue("150")
    f.nets.on_update(None)
    n = f.settings.config.nets[0]
    assert (n.sweep_from, n.sweep_to, n.sweep_points) == (10.0, 1e9, 150)
    for _ in range(2):
        f.nets.on_bode(None)
        run_steps(app, f, [])
    views = [r for r in f.routes if r.kind == "bode"]
    assert len(views) == 1 and views[0].owner == n.name and len(views[0].responses[0].f) == 150
    assert f.results_panel.current() is views[0] and f.results_panel.tile_text().startswith("gain ")
    assert "swept" in f.status_text()
    # its step response: on the third canvas, under the Bode plot, and a "step" tile
    assert views[0].responses[0].step is not None and f.results_panel.canvas3.IsShown()
    assert "step" in f.results_panel.tile_text()


def test_several_nets_picked_in_kicad_become_one_path_with_its_models_and_labels(app, tmp_path, monkeypatch):
    """One bit of each net of a path picked in KiCad (here handed to add_nets as the inbox would): one Signal paths
    row from U1 through R1, the op-amp U5 and R2 (Pass through) to U2, U5 modelled as the amplifier with its pins,
    and the GT labels shown to add by hand where KiCad can't write the schematic (here: a board file)."""
    from boardgen import opamp_path_board
    from groundtruth.ui import main_frame
    messages = []
    monkeypatch.setattr(wx, "MessageBox", lambda msg, *a, **k: messages.append(msg) or wx.OK)
    _, _, path = make(tmp_path, opamp_path_board(r_after="4k7"), "sel", nets=())
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        f.add_nets(["SIG_C", "SIG_A", "SIG_D", "SIG_B"])
        run_steps(app, f, [])                                   # the label job
        spec = next(n for n in f.settings.config.nets if n.driver == "U1.1")
        assert spec.receivers == ["U2.1"] and spec.through == ["R2"]
        m = f.settings.config.model_for("U5")
        assert m.kind == "amplifier" and m.pins == ["3", "1"]
        assert "the path" in f.status_text() or messages
        assert any("U5:  GT = amp_in:3, amp_out:1" in msg and "U1:  GT = sig_drv:1" in msg for msg in messages), messages
    finally:
        close_window(f)


def test_preferences_shows_cell_sizes_from_the_history_before_a_simulate(app, frame, monkeypatch):
    """Before this session's first Simulate, Preferences takes the cell size from the newest run kept for the
    board (the History tab reads it with the runs); this session's own Simulate takes over once it has run."""
    from groundtruth.ui import prefs_dialog
    f = frame
    seen = []

    class Dialog:
        """Stands in for the dialog: records the cell size it was given, answers Cancel."""
        def __init__(self, parent, prefs, cell_mm=None):
            seen.append(cell_mm)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ShowModal(self):
            return wx.ID_CANCEL
    monkeypatch.setattr(prefs_dialog, "PreferencesDialog", Dialog)
    run_steps(app, f, [])
    f._cell_mm, f.history.cell_mm = None, 0.217
    f.on_preferences()
    f._cell_mm = 0.15
    f.on_preferences()
    assert seen == [0.217, 0.15]


def test_a_dc_load_is_edited_on_its_tab_and_shown_after_simulate(app, tmp_path, monkeypatch):
    """The DC loads tab (the user: "how can I simulate a load across the highlighted trace?"): the rail from the
    settings is listed, a second one added from the form is written to the settings; after Simulate each rail is a
    view of its own (its DC map) with its KPI tiles (the worst drop first after the current) and each load's drop."""
    from test_pours import pour_board
    from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile
    from groundtruth.ui import kpi, main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    _, sc, path = make(tmp_path, pour_board(40.0), "rail", nets=())
    sc.set_dc_loads([DcLoadSpec("VDD rail", "U1.1", [DcLoad("U2.1", 2.0, "U2.2")], "U1.2", max_drop=0.5)])
    sc.save()
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        tab = f.dc_loads
        assert tab.list.GetItemCount() == 1 and tab.list.GetItemText(0, 0) == "VDD rail"
        tab.name.SetValue("half")
        tab.source.SetValue("U1.1")
        tab.source_return.SetValue("")
        tab.loads.SetValue("U2.1=1A")
        tab.max_drop.SetValue("")
        tab.on_add(None)
        assert [d.name for d in f.settings.config.dc_loads] == ["VDD rail", "half"]      # saved in the background
        run_steps(app, f, [lambda: f.run(True) and None])
        rails = [v for v in f.routes if v.kind == "dcload"]
        assert [v.net for v in rails] == ["VDD rail", "half"] and all(not v.error for v in rails)
        tiles = kpi.kpis(rails[0], f.settings.config.rules)
        assert tiles[1][0] == "worst drop" and "U2.1" in tiles[1][2]
        k = f.routes.index(rails[0])
        run_steps(app, f, [lambda: f.results_panel.choice.SetSelection(k), lambda: f.results_panel._route_changed()])
        shown = f.results_panel.choice.GetStringSelection()
        assert "U2.1" in shown or "VDD rail" in shown
        assert "drop" in f.results_panel._converter_metrics(rails[0])
    finally:
        close_window(f)


def test_fill_from_net_fills_the_dc_load_form(app, tmp_path, monkeypatch):
    """The user pressed Add on an empty form (the loads' example in grey looked like a value): an empty form now says
    what to do, and Fill from net… picks the rail (VDD) and its ground (GND), the source (the 2-pin connector,
    preselected) and the loads (the connectors, preselected), a current for each: the form is filled and Add stores
    the rail with every return pin."""
    from boardgen import distribution_board
    from groundtruth.ui import main_frame
    told = []
    monkeypatch.setattr(wx, "MessageBox", lambda text, *a, **k: told.append(text) or wx.OK)

    class Single:
        """Stands in for wx.SingleChoiceDialog: answers OK with the preselected entry."""
        def __init__(self, parent, message, caption, choices):
            self.choices, self.sel = choices, 0

        def SetSelection(self, k):
            self.sel = k

        def GetSelection(self):
            return self.sel

        def ShowModal(self):
            return wx.ID_OK

        def Destroy(self):
            pass

    class Multi(Single):
        """Stands in for wx.MultiChoiceDialog: answers OK with the preselected entries."""
        def SetSelections(self, ks):
            self.sels = list(ks)

        def GetSelections(self):
            return self.sels

    class Text(Single):
        """Stands in for wx.TextEntryDialog: answers 0.25A."""
        def __init__(self, parent, message, caption, value=""):
            super().__init__(parent, message, caption, [])

        def GetValue(self):
            return "0.25A"
    monkeypatch.setattr(wx, "SingleChoiceDialog", Single)
    monkeypatch.setattr(wx, "MultiChoiceDialog", Multi)
    monkeypatch.setattr(wx, "TextEntryDialog", Text)
    _, _, path = make(tmp_path, distribution_board(), "dist", nets=())
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        tab = f.dc_loads
        tab.on_add(None)
        assert told and "The form is empty" in told[-1] and not f.settings.config.dc_loads
        run_steps(app, f, [lambda: tab.on_fill()])
        assert tab.source.GetValue() == "J1.2" and tab.source_return.GetValue() == "J1.1"
        assert tab.loads.GetValue().splitlines() == [f"J{k}.2/J{k}.1=0.25A" for k in (2, 3, 4, 5)]
        tab.on_add(None)
        d = f.settings.config.dc_loads[0]
        assert d.name == "VDD rail" and d.current == pytest.approx(1.0) and [ld.ret for ld in d.loads][0] == "J2.1"
    finally:
        close_window(f)


def test_use_all_cores_is_an_opt_in_with_its_issues_explained(app, frame, monkeypatch):
    """Preferences > Simulation > "All cores" (the user: "make it an opt-in 'use all cores' setting. and add an info
    icon to explain its potential issues"): off by default; ticked, it goes into the settings Simulate runs with
    (analysis.all_cores); the (i) button explains what it changes and its potential issues."""
    from groundtruth.ui.prefs_dialog import ALL_CORES_INFO, PreferencesDialog, defaults, overrides
    told = []
    monkeypatch.setattr(wx, "MessageBox", lambda text, *a, **k: told.append(text))
    assert defaults({})["all_cores"] is False and "all_cores" not in overrides({})
    assert overrides({"all_cores": True})["all_cores"] is True
    with PreferencesDialog(frame, defaults({})) as dlg:
        assert dlg.all_cores.GetValue() is False
        dlg.all_cores.SetValue(True)
        assert dlg.values()["all_cores"] is True
        info = [c for c in dlg.all_cores.GetParent().GetChildren() if isinstance(c, wx.BitmapButton)][0]
        info.ProcessEvent(wx.CommandEvent(wx.wxEVT_BUTTON, info.GetId()))
    assert told and told[0] == ALL_CORES_INFO
    assert "Potential issues" in ALL_CORES_INFO and "reproducible" in ALL_CORES_INFO and "memory" in ALL_CORES_INFO



def test_preferences_switch_each_check_and_set_the_esd_distance(app, frame):
    """Preferences > Run checks: a tick box per check, all ticked by default, and the ESD search distance (greyed
    while ESD at connectors is unticked); values() hands them to ui.json."""
    from groundtruth.ui.prefs_dialog import CHECK_TOGGLES, PreferencesDialog, checks_off, defaults
    with PreferencesDialog(frame, defaults({})) as dlg:
        assert set(dlg.checks) == {key for key, *_ in CHECK_TOGGLES}
        assert all(box.GetValue() for box in dlg.checks.values())
        assert dlg.esd_mm.GetValue() == 10.0 and dlg.esd_mm.IsEnabled()
        esd = dlg.checks["check_esd_protection"]
        esd.SetValue(False)
        esd.ProcessEvent(wx.CommandEvent(wx.wxEVT_CHECKBOX, esd.GetId()))
        assert not dlg.esd_mm.IsEnabled()
        dlg.checks["check_diff_pairs"].SetValue(False)
        dlg.esd_mm.SetValue(4.5)
        v = dlg.values()
    assert v["check_esd_protection"] is False and v["check_diff_pairs"] is False and v["esd_mm"] == 4.5
    assert checks_off(v) == {"esd_protection", "diff_impedance", "diff_skew"}
    with PreferencesDialog(frame, defaults(v)) as dlg:
        assert not dlg.checks["check_diff_pairs"].GetValue() and dlg.esd_mm.GetValue() == 4.5

def test_find_servers_lists_the_cluster_and_fills_the_address(app, frame, monkeypatch):
    """Preferences › Find servers: without a key it only says to enter one; the servers found are listed (the lead
    first, with its role) and the one picked goes into the address (its port only when not the default)."""
    from groundtruth.ui.prefs_dialog import PreferencesDialog, defaults
    told, listed = [], []
    monkeypatch.setattr(wx, "MessageBox", lambda text, *a, **k: told.append(text))

    class Choice:
        """Stands in for the list: records it, picks the second server."""
        def __init__(self, parent, message, caption, choices):
            listed.extend(choices)

        def ShowModal(self):
            return wx.ID_OK

        def GetSelection(self):
            return 1

        def Destroy(self):
            pass
    monkeypatch.setattr(wx, "SingleChoiceDialog", Choice)
    with PreferencesDialog(frame, dict(defaults({}), compute="server", server_key="")) as dlg:
        dlg.on_find()
        assert told and "access key first" in told[0] and dlg.find.IsEnabled()
        found = [{"id": "a", "lead": "a", "host": "DELL", "name": "DELL", "address": "10.0.0.2", "port": 8765,
                  "info": {"cores": 16, "memory_gb": 62.7, "gpus": ["Quadro P2200"]}},
                 {"id": "b", "lead": "a", "host": "ST650", "name": "10.0.0.3", "address": "10.0.0.3", "port": 9000,
                  "info": {"cores": 32, "memory_gb": 128}}]
        dlg._found(found, "")
        assert "DELL (10.0.0.2) — lead, 16 cores, 62.7 GB, Quadro P2200" == listed[0]
        assert "member" in listed[1] and "no GPU" in listed[1]
        assert dlg.server.GetValue() == "10.0.0.3:9000"
        dlg._found([], "")
        assert "No compute server with this access key" in told[-1]


def test_clicking_a_bar_in_the_cross_section_shows_its_via_in_kicad(app, frame):
    """Results tab, the cross-section's side view: a click on a green (return-carrying) or grey (signal) via bar
    hands that via to the window like a map's marker (on_map_mark), with no layer of its own (the via on any); its
    tooltip says a click shows it. The user: "click on the grey or green vias ... zoom into the pcb and highlight
    that specific via"."""
    from types import SimpleNamespace
    from matplotlib.backend_bases import MouseEvent
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    f = frame
    run_steps(app, f, [])
    h = f.results_panel
    fig = Figure(figsize=(6, 2), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_xlim(0, 30), ax.set_ylim(3.5, -0.5)
    via = {"x": 15.0, "y": 10.0, "name": "signal via F.Cu → B.Cu @(15.00, 10.00)"}
    fig._gt_section = {ax: None}
    fig._gt_marks = {ax: [(15.0, r, 1, via["name"]) for r in (0.0, 0.5, 1.0)]}
    fig._gt_mark_refs = {ax: [("via", via)] * 3}
    px, py = ax.transData.transform((15.0, 0.5))
    seen = []
    h.on_mark = lambda *a: seen.append(a)
    canvas = SimpleNamespace(figure=fig, toolbar=None)
    h._press(SimpleNamespace(inaxes=ax, dblclick=False, button=1, x=px + 2, y=py, canvas=canvas))
    assert seen == [("via", via, via["name"], "")]
    # a real click through the cross-section canvas's own callbacks: it must be connected there (it was for the
    # hover only: the tooltip said "click: show it in KiCad" and a click did nothing)
    old = h.canvas3.figure
    try:
        fig._canvas_callbacks = old._canvas_callbacks     # as show_rendered hands them on with a new figure
        h.canvas3.figure = fig
        fig.set_canvas(h.canvas3)
        h.canvas3.callbacks.process("button_press_event",
                                    MouseEvent("button_press_event", h.canvas3, px + 2, py, button=1))
        assert seen[-1] == ("via", via, via["name"], "") and len(seen) == 2
    finally:
        h.canvas3.figure = old
        old.set_canvas(h.canvas3)
    h.on_mark = f.on_map_mark


def test_clicking_a_map_marker_shows_it_in_kicad(app, frame):
    """Results tab: a single left click on a marker (an issue's circle, a via's ▲) hands it to the window
    (on_map_mark), which shows it in KiCad; a double click still resets the zoom and shows nothing; on a board
    file (no KiCad) the status line says where to open it from."""
    from types import SimpleNamespace
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    f = frame
    run_steps(app, f, [])
    h = f.results_panel
    fig = Figure(figsize=(4, 3), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_xlim(0, 40), ax.set_ylim(20, 0)
    via = {"kind": "via", "name": "via GND @(20.00, 10.00)", "x": 20.0, "y": 10.0, "fraction": 0.4}
    fig._gt_maps = {ax: ("In1.Cu", None, None, "A/m")}
    fig._gt_marks = {ax: [(20.0, 10.0, 1, "via GND @(20.00, 10.00): 40 % of the current")]}
    fig._gt_mark_refs = {ax: [("via", via)]}
    px, py = ax.transData.transform((20.0, 10.0))
    seen = []
    h.on_mark = lambda *a: seen.append(a)
    canvas = SimpleNamespace(figure=fig, toolbar=None)
    h._press(SimpleNamespace(inaxes=ax, dblclick=False, button=1, x=px + 2, y=py, canvas=canvas))
    h._press(SimpleNamespace(inaxes=ax, dblclick=True, button=1, x=px, y=py, canvas=canvas))
    h._press(SimpleNamespace(inaxes=ax, dblclick=False, button=1, x=px + 40, y=py, canvas=canvas))   # off it
    assert seen == [("via", via, "via GND @(20.00, 10.00): 40 % of the current", "In1.Cu")]
    h.on_mark = f.on_map_mark
    f.on_map_mark(*seen[0])
    assert "open GroundTruth from KiCad" in f.GetStatusBar().GetStatusText(0)
    # a density tile's hot spot inside the In1.Cu pour (no item right there): KiCad gets the copper around it to
    # zoom to, zoomed even with the Issues tab's "Zoom to it" off (the user: "I need it to zoom into the PCB as well")
    shown, live = [], f.session
    f.session = SimpleNamespace(live=True, show_issue=lambda hl, zoom, nets: (
        shown.append((hl, zoom)), {"selected": len(hl.focus), "focus": len(hl.focus), "lit": False,
                                   "previous_layer": None, "notes": []})[1])
    try:
        f.issues_panel.zoom.SetValue(False)
        f.on_map_mark("point", {"x": 6.0, "y": 12.5}, "the highest sustained current density", "In1.Cu")
        deadline = time.perf_counter() + 30
        while not shown and time.perf_counter() < deadline:
            wx.Yield()
            time.sleep(0.02)
        assert shown and shown[0][1] is True and shown[0][0].focus and shown[0][0].layer == "In1.Cu"
    finally:
        f.session, f._highlighted = live, False


def test_simulate_on_a_compute_server_and_here_when_it_is_gone(app, frame, tmp_path, monkeypatch):
    """Preferences send Simulate to a compute server (remote/): it is handed over and the window is free at once;
    the tracker brings the result back and, as this window handed it over, shows it (the status line names the
    server), listed on the Server jobs tab until then. With the server gone the window asks, and runs the job here
    on Yes."""
    from groundtruth.remote.server import ComputeServer
    from groundtruth.ui import theme
    f = frame
    monkeypatch.setattr(theme, "pref_path", lambda: tmp_path / "ui.json")     # never the user's preferences
    key = "gui-test-key-0123456789"
    srv = ComputeServer(key, "127.0.0.1", 0, data_dir=tmp_path / "server_jobs")
    srv.start()
    try:
        theme.save_prefs(compute="server", server=f"127.0.0.1:{srv.port}", server_key=key)
        status = []
        run_steps(app, f, [lambda: f.run(True) and None,
                           lambda: status.append(f.GetStatusBar().GetStatusText(0))])
        assert "handed to the compute server" in status[0] and not f.routes, status
        assert [m["state"] for m in f.server_jobs.jobs] in (["queued"], ["running"], ["ready"])
        deadline = time.perf_counter() + 300
        while not f.routes and time.perf_counter() < deadline:     # the tracker fetches it and the window shows it
            wx.Yield()
            time.sleep(0.05)
        run_steps(app, f, [lambda: status.append(f.GetStatusBar().GetStatusText(0))])
        assert f.routes and "on the compute server 127.0.0.1" in status[-1], status
        # shown here: removed from the server; a past job here, named after the server computer
        assert not srv.jobs and [m["state"] for m in f.remote_jobs.all()] == ["shown"]
        assert f.remote_jobs.all()[0]["server_name"] and f.server_jobs.past and not f.server_jobs.current
    finally:
        srv.stop()
    asked = []
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: (asked.append(a[0]), wx.YES)[1])
    f.routes = []
    run_steps(app, f, [lambda: f.run(True) and None,
                       lambda: status.append(f.GetStatusBar().GetStatusText(0))])
    assert any("Run it on this computer instead?" in a for a in asked), asked
    assert f.routes and "compute server" not in status[-1], status


def test_a_thermal_on_the_server_is_listed_on_the_server_jobs_tab(app, frame, tmp_path, monkeypatch):
    """The window's watcher (MainFrame._watch, for Thermal, Current capacity, Check in Simulate, Compare what-if on
    the server) lists the waited-for job under Current jobs as "Thermal" with its server, then under Past jobs (the
    user: "thermal sims are not shown")."""
    from groundtruth.ui import theme
    monkeypatch.setattr(theme, "pref_path", lambda: tmp_path / "ui.json")     # never the user's preferences
    theme.save_prefs(compute="server", server="10.0.0.9:8765", server_key="gui-test-key-0123456789")
    run_steps(app, frame, [])
    watch = frame._watch("Thermal", frame.board)
    run_steps(app, frame, [lambda: watch("submitted", "t1", {"position": 0, "host": "BIG"}),
                           lambda: watch("state", "t1", {"state": "running", "fraction": 0.5, "message": "iteration 3",
                                                         "run_s": 12.0, "host": "BIG"})])
    p = frame.server_jobs
    assert [m["id"] for m in p.current] == ["t1"] and p.current[0]["server"] == "10.0.0.9:8765"
    assert p.list.GetItemText(0, 4) == "Thermal" and p.list.GetItemText(0, 1) == "BIG"
    run_steps(app, frame, [lambda: watch("end", "t1", {"state": "done"})])
    assert not p.current and [m["state"] for m in p.past] == ["shown"]


def test_the_details_box_of_the_server_jobs_tab_can_be_made_taller(app, frame, tmp_path, monkeypatch):
    """The user: "the details job window can't be resized which is a bit awkward". A divider between the job lists
    and the details is dragged; the details' height is kept in ui.json and used by the next window."""
    from groundtruth.ui import jobs_panel, theme
    monkeypatch.setattr(theme, "pref_path", lambda: tmp_path / "ui.json")     # never the user's preferences
    run_steps(app, frame, [])
    p = frame.server_jobs
    assert p.split.IsSplit() and p.detail.GetParent() is p.split.GetWindow2()
    h = p.split.GetClientSize().height
    run_steps(app, frame, [lambda: p.split.SetSashPosition(int(h * 0.4)),
                           lambda: p._sash_moved(wx.SplitterEvent(wx.wxEVT_SPLITTER_SASH_POS_CHANGED, p.split))])
    kept = theme.prefs()[jobs_panel.DETAILS_PREF]
    assert kept == pytest.approx(p.ToDIP(h - p.split.GetSashPosition()), abs=2) and kept > jobs_panel.DETAILS_DIP
    assert jobs_panel.details_height() == kept


def test_the_server_jobs_tab_lists_current_and_past_jobs_with_their_details(app, frame):
    """Current jobs (queued, running, a result here) and Past jobs (shown, failed, cancelled, lost) in two lists, with
    the server computer, the resolution and the elapsed time; a failed job's error in full in the details box; Copy
    details."""
    from groundtruth.ui.jobs_panel import clock_text, elapsed
    run_steps(app, frame, [])
    p = frame.server_jobs
    now = time.time()
    jobs = [{"id": "a1", "server": "10.0.0.5", "server_name": "BIG-1", "kind": "simulate", "label": "Simulate",
             "board_name": "b.kicad_pcb", "state": "running", "fraction": 0.4, "run_s": 125.0, "polled": now,
             "submitted": now - 300, "resolution": 2.0,
             "server_version": "GroundTruth 0.1.41452 (57602a8, built 2026-10-19 12:00)"},
            {"id": "b2", "server": "10.0.0.6", "server_name": "BIG-2", "kind": "suggest", "label": "Suggest fixes",
             "board_name": "b.kicad_pcb", "state": "failed", "error": "the job failed (exit code 1): ValueError",
             "detail": "RuntimeError: the job failed\n\nTraceback (most recent call last):\n  ...\nValueError: x",
             "run_s": 61.0, "submitted": now - 900}]
    p.refresh(jobs)
    assert [m["id"] for m in p.current] == ["a1"] and [m["id"] for m in p.past] == ["b2"]
    assert p.list.GetItemText(0, 1) == "BIG-1" and p.list.GetItemText(0, 7) == "2:05"
    # the server's build in full; b2 was kept before it was recorded
    assert p.list.GetItemText(0, 2) == "GroundTruth 0.1.41452 (57602a8, built 2026-10-19 12:00)"
    assert p.past_list.GetItemText(0, 2) == "–"
    assert p.list.GetItemText(0, 5) == "2×" and p.past_list.GetItemText(0, 5) == "–"   # b2: kept before it was recorded
    assert p.past_list.GetItemText(0, 8) == "" and p.list.GetItemText(0, 8) == ""      # no readings yet
    assert clock_text(3725) == "1:02:05" and elapsed(jobs[0], now + 10) == pytest.approx(135.0)
    p.past_list.Select(0)
    wx.Yield()
    text = p.detail.GetValue()
    assert "BIG-2" in text and "Traceback (most recent call last)" in text and "ValueError: x" in text
    assert p.buttons["copy"].IsEnabled() and not p.buttons["show"].IsEnabled()
    p.list.Select(0)                                    # a selection in one list clears the other's
    wx.Yield()
    assert [m["id"] for m in p.selected()] == ["a1"] and p.buttons["cancel"].IsEnabled()
    assert "Resolution: 2×" in p.detail.GetValue()
    assert "Version on the server: GroundTruth 0.1.41452 (57602a8, built 2026-10-19 12:00)" in p.detail.GetValue()



@pytest.mark.skipif(sys.platform != "win32", reason="the drop-down width is a Win32 setting")
def test_a_view_lists_drop_down_is_as_wide_as_its_longest_name(app, frame):
    """The Route list's long view names were cut off at the box's width ("text is truncated"): the drop-down opens
    as wide as the longest entry; the box keeps its size."""
    import ctypes
    from groundtruth.ui.results_panels import _set_items
    choice = wx.Choice(frame, size=(220, -1))
    name = "buck Payload and Servo/SW1  DC output L5 → J2.2 (a long hierarchical view name)"
    _set_items(choice, ["short", name])
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    dropped = user32.SendMessageW(choice.GetHandle(), 0x015F, 0, 0)        # CB_GETDROPPEDWIDTH
    assert dropped >= choice.GetTextExtent(name)[0] > choice.GetSize().width
    choice.Destroy()


def test_the_dc_load_form_completes_pins(app, tmp_path, monkeypatch):
    """The DC loads form's fields list the board's pins in a popup (the user: "can you auto complete the source pin
    source return as well as the loads?"); Enter takes the first, a load's pin is followed by "/" and the return's
    list, the return by "="."""
    from boardgen import distribution_board
    from groundtruth.ui import main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.OK)
    _, _, path = make(tmp_path, distribution_board(), "dist", nets=())
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        tab = f.dc_loads
        src, ret, loads = tab.popups["source"], tab.popups["source_return"], tab.popups["loads"]
        tab.source.ChangeValue("j")
        tab.source.SetInsertionPointEnd()
        src.update(force=True)
        assert src.shown() and src.box.GetString(0).startswith("J1.2") and "VDD" in src.box.GetString(0)
        src.accept()
        assert tab.source.GetValue() == "J1.2" and not src.shown()
        ret.update(force=True)
        assert ret.box.GetString(0).startswith("J1.1")
        ret.accept()
        loads.update(force=True)
        assert loads.box.GetString(0).startswith("J2.2")
        loads.accept()                                          # the load, its "/", and the return's list at once
        assert tab.loads.GetValue() == "J2.2/" and loads.shown() and loads.box.GetString(0).startswith("J2.1")
        loads.accept()
        assert tab.loads.GetValue() == "J2.2/J2.1="
        tab.source.ChangeValue("J1.2")
        tab.source.SetInsertionPointEnd()
        src.update(force=True)
        assert not src.shown()                                  # the pin is complete: nothing to offer
    finally:
        close_window(f)


def test_the_parts_tab_edits_through_hole_pin_values(app, tmp_path, monkeypatch):
    """The Parts tab's Pins column (a through-hole part's own pin values) and Pin defaults… (the board's), both saved
    to the settings; text that is not a pin value is refused with the syntax shown; a surface-mount part has no Pins
    cell to edit. The user: "introduce a GT field parameter ... to define the solder joint resistance", with defaults
    for each value."""
    import wx.grid as gridlib
    from boardgen import distribution_board
    from groundtruth.ui import main_frame
    from groundtruth.ui.parts_panel import PINS_COL
    told = []
    monkeypatch.setattr(wx, "MessageBox", lambda text, *a, **k: told.append(text) or wx.OK)

    class Text:
        """Stands in for wx.TextEntryDialog: answers OK with the board's pin defaults."""
        def __init__(self, parent, message, caption, value=""):
            self.value = value

        def ShowModal(self):
            return wx.ID_OK

        def GetValue(self):
            return "pin:brass, contact:0.25m"

        def Destroy(self):
            pass
    monkeypatch.setattr(wx, "TextEntryDialog", Text)
    _, _, path = make(tmp_path, distribution_board(), "pins", nets=())
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        f.notebook.SetSelection(f.notebook.FindPage(f.parts))
        g = f.parts.grid
        row, smd = f.parts.rows.index("J1"), f.parts.rows.index("C1")
        assert not g.IsReadOnly(row, PINS_COL) and g.IsReadOnly(smd, PINS_COL)

        def edit(text):
            """Type into J1's Pins cell, as the grid reports it."""
            g.SetCellValue(row, PINS_COL, text)
            g.GetEventHandler().ProcessEvent(gridlib.GridEvent(g.GetId(), gridlib.wxEVT_GRID_CELL_CHANGED, g, row,
                                                               PINS_COL))
        edit("contact:0.5m, fill:75")
        assert f.settings.config.components["J1"].tht == {"contact": pytest.approx(5e-4), "fill": 0.75}
        edit("contact:lots")
        assert told and "not a resistance" in told[-1] and "contact:0.5m" in told[-1]
        assert f.settings.config.components["J1"].tht["contact"] == pytest.approx(5e-4)      # kept
        wait_for(app, lambda: g.GetCellValue(row, PINS_COL).startswith("fill:75") or "contact" in
                 g.GetCellValue(row, PINS_COL))
        f.parts.on_pin_defaults()
        assert f.settings.config.tht == {"pin": "brass", "contact": pytest.approx(2.5e-4)}
        from groundtruth.analysis.pins import params_for
        p = params_for(f.board, f.settings.config, "J1")
        assert (p.contact, p.pin) == (pytest.approx(5e-4), "brass") and p.source["pin"] == "board default"
    finally:
        close_window(f)


def test_the_parts_tab_shows_each_capacitor_at_its_dc_voltage(app, tmp_path, monkeypatch):
    """The Parts tab's C at bias (a DC-bias curve's text) and C in circuit (the capacitance at the voltage across the
    part, worked out in the background): C1 on +12V with 40nF@40V shows ~88 nF at 12 V; a curve typed in is
    saved, text that is no curve refused with the formats shown. The user: "rather than having a real value we
    could use an array like this (90nF@10V 75nF@50V, 20nF@70V)"."""
    import wx.grid as gridlib
    from test_bias import _voltage_board
    from groundtruth.ui import main_frame
    from groundtruth.ui.parts_panel import CIRCUIT_COL, PARAM_COL
    told = []
    monkeypatch.setattr(wx, "MessageBox", lambda text, *a, **k: told.append(text) or wx.OK)
    _, _, path = make(tmp_path, _voltage_board(), "cbias", nets=())
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        g, row = f.parts.grid, f.parts.rows.index("C1")
        wait_for(app, lambda: "at ≤ 12 V" in g.GetCellValue(row, CIRCUIT_COL))
        assert g.GetCellValue(row, CIRCUIT_COL).startswith("88") and g.IsReadOnly(row, CIRCUIT_COL)
        assert g.GetCellValue(row, PARAM_COL["C_bias"]) == "40nF@40V"
        col = PARAM_COL["C_bias"]

        def edit(text):
            """Type into C1's C at bias cell, as the grid reports it."""
            g.SetCellValue(row, col, text)
            g.GetEventHandler().ProcessEvent(gridlib.GridEvent(g.GetId(), gridlib.wxEVT_GRID_CELL_CHANGED, g, row,
                                                               col))
        edit("95nF@10V, 66nF@30V, 39nF@50V")
        assert f.settings.config.components["C1"].model.bias == "95nF@10V, 66nF@30V, 39nF@50V"
        assert "C_bias" not in f.settings.config.components["C1"].model.from_schematic     # set here now
        edit("75nF, 50nF")
        assert told and "each needs its voltage" in told[-1] and "75nF@50V" in told[-1]
        assert f.settings.config.components["C1"].model.bias == "95nF@10V, 66nF@30V, 39nF@50V"    # kept
        wait_for(app, lambda: g.GetCellValue(row, col) == "95nF@10V, 66nF@30V, 39nF@50V"
                 and "at ≤ 12 V" in g.GetCellValue(row, CIRCUIT_COL))
    finally:
        close_window(f)


def test_preferences_list_the_servers_and_update_the_old_ones(app, frame, monkeypatch):
    """Preferences › Compute server › Servers (the user: "it will show the user which server is out of date and
    offers to update it through a button press"): the servers asked in the background, the out-of-date one marked;
    Update all updates the ones that take remote updates (after asking), then asks them again; without the plugin's
    server package (a development copy) the buttons stay off."""
    from groundtruth.ui import prefs_dialog, server_updates
    from groundtruth.ui.prefs_dialog import PreferencesDialog, defaults
    rows = [{"name": "andre", "address": "andre", "version": "0.1.5 (x)", "state": "older", "text": "out of date",
             "can_update": True, "update": None},
            {"name": "mac", "address": "10.0.0.3:8765", "version": "0.1.9 (y)", "state": "same", "text": "up to date",
             "can_update": False, "update": None}]
    asked, updated = [], []
    monkeypatch.setattr(server_updates, "check", lambda a, k: asked.append((a, k)) or [dict(r) for r in rows])
    monkeypatch.setattr(server_updates, "update", lambda a, k, b, progress, stop=None:
                        updated.append(a) or progress("installing…") or (True, "updated to 0.1.9 (y)"))
    from groundtruth.ui import server_info
    confirmed = []
    monkeypatch.setattr(server_info, "confirm_update",
                        lambda parent, t, v: confirmed.append([r["name"] for r in t]) or True)
    import groundtruth.remote.release as release
    monkeypatch.setattr(release, "bundled", lambda folder=None: {"zip": None, "sig": "ab", "version": "0.1.9 (y)"})
    prefs = dict(defaults({}), compute="server", server="andre", server_key="k" * 20)
    with PreferencesDialog(frame, prefs) as dlg:
        p = dlg.servers
        assert isinstance(p, prefs_dialog.ServersPanel)
        wait_for(app, lambda: p.list.GetItemCount() == 2)            # asked by itself once the dialog shows
        assert asked == [("andre", "k" * 20)] and "1 server(s) out of date" in p.note.GetLabel()
        assert p.all_b.IsEnabled() and not p.update_b.IsEnabled()
        p.list.Select(0)
        wait_for(app, lambda: p.update_b.IsEnabled())
        p.on_update(all_=True)
        wait_for(app, lambda: len(asked) == 2)                        # asked again after the update
        assert updated == ["andre"] and not p.busy and confirmed == [["andre"]]
        p.bundle = None
        p._enable()
        assert not p.all_b.IsEnabled() and not p.update_b.IsEnabled()


def test_a_pdn_rail_is_found_set_and_shown_after_simulate(app, tmp_path, monkeypatch):
    """The Power integrity tab (the plan's M1): Find rails lists the rail the IC draws from (VDD on pdn_board), the
    form sets its load step and writes it; after Simulate the rail is a view of its own on the Results tab (its
    curves, and what holds the rail under them) with its tiles (the target, the worst margin); the draw-on-board
    buttons are off for it."""
    from boardgen import pdn_board
    from groundtruth.ui import kpi, main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    _, sc, path = make(tmp_path, pdn_board(caps=[("C1", "1u", (22.0, 15.0))], source=True), "pdn", nets=(),
                       cavity_max_nodes=8000)
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        tab = f.pdn
        assert list(tab.rails) == ["VDD"] and tab.list.GetItemCount() == 0
        tab.on_find()
        assert [d.rail for d in f.settings.config.pdn] == ["VDD"]
        tab.list.Select(0)
        wx.Yield()
        tab.rail.SetValue("VDD")
        tab.voltage.SetValue("3.3")                         # "VDD" names no voltage
        tab.step.SetValue("0.5")
        tab.on_update(None)
        spec = f.settings.config.pdn[0]
        assert spec.i_step == pytest.approx(0.5) and tab.list.GetItemText(0, 4) == "330mΩ"      # 3.3 V · 5 % / 0.5 A
        run_steps(app, f, [lambda: f.run(True) and None])
        rails = [v for v in f.routes if v.kind == "pdn"]
        assert len(rails) == 1 and rails[0].ports[0]["part"] == "U1" and "J1" in rails[0].source
        tiles = kpi.kpis(rails[0], f.settings.config.rules)
        assert tiles[0][0] == "target" and tiles[1][0] == "worst margin"
        k = f.routes.index(rails[0])
        run_steps(app, f, [lambda: f.results_panel.choice.SetSelection(k), lambda: f.results_panel._route_changed()])
        wait_for(app, lambda: not f.results_panel.rendering())
        assert "PDN" in f.results_panel.choice.GetStringSelection() and not f.results_panel.draw_button.IsEnabled()
        assert "target" in f.results_panel.tile_text() and "U1" in f.results_panel._metrics(rails[0])
        titles = [t.get_text() for ax in f.results_panel.fig2.axes for t in [ax.title] + ax.texts]
        assert any("holds the rail" in t for t in titles) or f.results_panel.fig2.texts
    finally:
        close_window(f)


def test_the_signal_integrity_tab_finds_a_pair_sets_a_target_and_shows_the_results(app, tmp_path, monkeypatch):
    """The Signal integrity tab (the plan's M3): Find pairs lists SIG_P / SIG_N (line_board's pair), the form sets
    its Zdiff and writes it; an impedance target by pattern and the limits are written; after Run checks each row
    shows what was found (Zdiff and skew, Z0's range) and the issues are on the Issues tab."""
    from boardgen import line_board
    from groundtruth.config.settings import SettingsFile
    from groundtruth.ui import main_frame
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    _, sc, path = make(tmp_path, line_board(width=0.2, pair_gap=0.2, n_extra=2.0), "si", nets=())
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [])
        tab = f.si
        assert tab.list.GetItemCount() == 0 and tab.p.GetCount() == 1
        tab.on_find()
        (pair,) = f.settings.config.si.pairs
        assert (pair.p, pair.n, pair.reviewed) == ("SIG_P", "SIG_N", False)
        tab.list.Select(0)
        wx.Yield()
        tab.on_select(type("E", (), {"GetIndex": lambda self: 0})())
        tab.zdiff.SetValue("150")
        tab.skew.SetValue("5p")                             # SIG_N is 2 mm (~13 ps) longer
        tab.on_update(None)
        assert f.settings.config.si.pairs[0].zdiff == 150.0 and tab.list.GetItemText(0, 1) == "150Ω"
        tab.t_nets.SetValue("SIG_*")
        tab.t_z0.SetValue("40")
        tab.on_add_target(None)
        assert f.settings.config.si.impedance[0].nets == ["SIG_*"] and tab.tlist.GetItemCount() == 1
        tab.xt.SetValue("2")
        tab.on_limits(None)
        assert f.settings.config.si.crosstalk_max == pytest.approx(0.02)
        assert f._saver.flush()
        assert SettingsFile.load(f.settings.path).config.si.pairs[0].zdiff == 150.0
        run_steps(app, f, [lambda: f.run(False) and None])
        assert "Zdiff" in tab.list.GetItemText(0, 4) and "skew" in tab.list.GetItemText(0, 4)
        assert "Z0" in tab.tlist.GetItemText(0, 4)
        assert {"diff_impedance", "diff_skew", "impedance"} <= {i.check for i in f.issues}
    finally:
        close_window(f)


def test_a_routes_bode_view_draws_its_curves_and_poles(app, frame):
    """After Simulate the signal path has a "Bode / poles and zeros" view (analysis/transfer.BodeView, the plan's
    M4): the Results tab draws its magnitude and phase on the main canvas and the pole-zero map below, its tiles
    name the dominant pole, and the board-drawing buttons are off (curves, not maps)."""
    f = frame
    run_steps(app, f, [lambda: f.run(True) and None])
    k = next(i for i, v in enumerate(f.routes) if v.kind == "bode")
    view = f.routes[k]
    assert view.responses and view.responses[0].quantity == "voltage gain"
    run_steps(app, f, [lambda: f.results_panel.choice.SetSelection(k), lambda: f.results_panel._route_changed()])
    wait_for(app, lambda: not f.results_panel.rendering())
    assert "Bode" in f.results_panel.choice.GetStringSelection() and not f.results_panel.draw_button.IsEnabled()
    assert "Hz" in f.results_panel.tile_text()
    titles = [ax.get_title(loc="left") for ax in f.results_panel.canvas.figure.axes]
    assert any("Bode" in t for t in titles), titles
    titles2 = [ax.get_title(loc="left") for ax in f.results_panel.canvas2.figure.axes]
    assert any("poles" in t for t in titles2), titles2
    # the mouse over a pole's or zero's marker, on the Bode plot and the pole-zero map: its tooltip and the readout
    # name it, without the map markers' "click: show it in KiCad" (the user: "... show a hover-over note like a
    # tooltip?")
    from matplotlib.backend_bases import MouseEvent
    for canvas in (f.results_panel.canvas, f.results_panel.canvas2):
        ax, marks = next((a, m) for a, m in canvas.figure._gt_marks.items() if m)
        x, y = ax.transData.transform((marks[0][0], marks[0][1]))
        f.results_panel._hover(MouseEvent("motion_notify_event", canvas, x, y))
        assert canvas.GetToolTipText() == marks[0][3] and " at " in marks[0][3] and "KiCad" not in marks[0][3]
        assert f.results_panel.readout.GetLabel() == marks[0][3]


def test_a_selection_is_shown_in_kicad_once_it_settles(app):
    """Clicking or arrowing down a table shows only the row the user stops on in KiCad (listsel.SHOW_SETTLE_MS):
    every row passed over used to be selected, highlighted and zoomed to in KiCad, and the window lagged."""
    from groundtruth.ui import listsel

    class Rows(wx.Panel, listsel.SelectableRows):
        """A table of four rows with the mixin."""

        def __init__(self, parent, on_show):
            super().__init__(parent)
            self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
            self.list.InsertColumn(0, "Name")
            for k in range(4):
                self.list.InsertItem(k, f"row{k}")
            self._init_rows(on_show, "rows")

        def _row_names(self, rows):
            return [f"row{k}" for k in rows]

        def on_remove(self, _):
            pass
    shown = []
    top = wx.Frame(None)
    p = Rows(top, lambda names, zoom: shown.append(names))
    top.Show()
    try:
        for k in range(4):                     # quickly from row to row, as arrowing down does
            for j in range(4):
                p.list.Select(j, on=(j == k))
            app.Yield()
            time.sleep(0.05)
        assert shown == []                     # still moving: nothing sent to KiCad yet
        wait_for(app, lambda: bool(shown), timeout=5)
        assert shown == [["row3"]]             # the row stopped on, once
        time.sleep(listsel.SHOW_SETTLE_MS / 1000 + 0.2)
        app.Yield()
        assert shown == [["row3"]]
    finally:
        top.Destroy()


def _issue(check, k, severity="error", waived=False):
    """A made-up issue of `check` at (k, k)."""
    from groundtruth.core.issues import Issue
    return Issue(check, severity, f"N{k}", f"{check} {k}", (float(k), float(k)), waived=waived)


def test_the_issues_tab_hides_the_checks_not_chosen_and_keeps_every_number(app, tmp_path):
    """The user: "add a filter to enable/hide entries based on what Checks are selected". Hidden checks leave the
    list, the rows keep their issue's number in the whole list (as its marker on the board is numbered), a click
    on a row is that row's issue, the count of what is hidden shows, and the choice is kept on this computer."""
    from groundtruth.ui import theme
    from groundtruth.ui.results_panels import IssuesPanel
    f = wx.Frame(None)
    try:
        p = IssuesPanel(f, lambda i: None, lambda: None, lambda i, r: None)
        issues = [_issue("clearance_voltage", 1), _issue("emi_margin", 2), _issue("clearance_voltage", 3),
                  _issue("via_overload", 4, "warning")]
        p.show(issues)
        assert [k for k, _ in p.rows] == [1, 2, 3, 4] and p.shown_label.GetLabel() == ""
        assert dict(p.checks_present()) == {"clearance_voltage": 2, "emi_margin": 1, "via_overload": 1}
        p.set_hidden({"clearance_voltage"})
        assert p.list.GetItemCount() == 2 and [k for k, _ in p.rows] == [2, 4]
        assert p.list.GetItemText(1, 0) == "4" and p.rows[1][1] is issues[3]
        assert "2 of 4 shown" in p.shown_label.GetLabel()
        assert theme.prefs()["issues_hidden"] == ["clearance_voltage"]
        again = IssuesPanel(f, lambda i: None, lambda: None, lambda i, r: None)     # a new window: remembered
        assert again.hidden == {"clearance_voltage"}
        p.set_hidden(set())
        assert p.list.GetItemCount() == 4
    finally:
        f.Destroy()
        wx.Yield()


def test_board_markers_keep_the_issue_numbers_and_skip_waived_and_hidden_checks(monkeypatch):
    """Draw markers on board: no marker for a waived issue or a hidden check, and each marker's label carries its
    issue's number in the Issues tab. Before, the markers counted only those drawn, so a waived issue earlier in
    the list shifted every later number off the tab's."""
    from groundtruth.adapters import kipy_sink
    texts = []
    monkeypatch.setattr(kipy_sink, "_drawing_layer", lambda board, name: "L")
    monkeypatch.setattr(kipy_sink, "clear", lambda board, prefix=None: 0)
    monkeypatch.setattr(kipy_sink, "_circle", lambda *a, **k: "circle")
    monkeypatch.setattr(kipy_sink, "_text", lambda pos, text, layer, size=0.6: texts.append(text) or "text")
    monkeypatch.setattr(kipy_sink, "_commit_group", lambda board, items, name, msg: len(items))
    issues = [_issue("emi_margin", 1, waived=True), _issue("clearance_voltage", 2), _issue("emi_margin", 3),
              _issue("via_overload", 4, "warning")]
    kipy_sink.draw_issues(None, issues, "User.9", hidden=frozenset({"clearance_voltage"}))
    assert texts == ["3 E emi_margin", "4 W via_overload"]


def test_the_busiest_via_tile_zooms_to_it_and_shows_it_in_kicad(app, tmp_path, monkeypatch):
    """The user: "how can I see which via is the busiest" (the tile named it only by its coordinates). On a DC
    path's view the "busiest via" tile is a button: it zooms the maps to a FOCUS_MM square around the via (its white
    ring marks it) and asks to show it in KiCad as a click on its marker does; the other tiles stay plain."""
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.report.plots import busiest_via
    from groundtruth.ui import main_frame
    from groundtruth.ui.results_panels import FOCUS_MM
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)
    board, sc, path = make(tmp_path, discrete_buck_board(out_connector=True), "busy", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.0
    sc.set_converters([spec])
    sc.save()
    f = main_frame.MainFrame(main_frame.Session(file_path=str(path)))
    f.Show()
    try:
        run_steps(app, f, [lambda: f.run(True) and None])
        h = f.results_panel
        k = next(i for i, r in enumerate(f.routes) if getattr(r, "view_of", None) and busiest_via(r) is not None)
        h.choice.SetSelection(k)
        h._route_changed()
        v = busiest_via(h.current())
        tiles = [t for t in h.tiles.GetChildren() if getattr(t, "title", None) is not None]
        tile = next(t for t in tiles if t.title == "busiest via")
        dens = next(t for t in tiles if t.title == "current density")
        assert tile._click is not None and dens._click is not None
        assert all(t._click is None for t in tiles if t not in (tile, dens))
        marks = []
        h.on_mark = lambda *a: marks.append(a)
        tile._click()
        x0, x1, y0, y1 = h._view
        assert x0 <= v["x"] <= x1 and y0 <= v["y"] <= y1 and x1 - x0 == pytest.approx(FOCUS_MM)
        assert marks and marks[0][0] == "via" and (marks[0][1]["x"], marks[0][1]["y"]) == (v["x"], v["y"])
        assert v["name"] in marks[0][2] and " A" in marks[0][2]
        run_steps(app, f, [lambda: None])                     # the zoomed maps render
        # the user: "the same for when clicking on RMS current density": the density tile zooms to its hot spot
        # (the path's sustained spot, the cyan ×) and shows the copper there, on its layer, in KiCad
        from groundtruth.report.plots import hot_spot
        spot = hot_spot(h.current())
        dens._click()
        x0, x1, y0, y1 = h._view
        assert x0 <= spot["x"] <= x1 and y0 <= spot["y"] <= y1 and x1 - x0 == pytest.approx(FOCUS_MM)
        assert marks[-1][0] == "point" and (marks[-1][1]["x"], marks[-1][1]["y"]) == (spot["x"], spot["y"])
        assert marks[-1][3] == spot["layer"] and "current density" in marks[-1][2]
        run_steps(app, f, [lambda: None])
    finally:
        close_window(f)


def test_clicking_a_bridge_leg_shows_that_leg_alone_in_kicad(app):
    """The user: "clicking on a leg does not highlight the leg on the PCB". Picking a leg in the Bridges tab's leg
    table shows that leg's stage ("bridge J1 B": its switches, hot loop and switch node) in KiCad, once the
    selection settles, instead of the whole bridge the row click showed."""
    from groundtruth.config.settings import BridgeSpec, LegSpec
    from groundtruth.ui.bridges_panel import BridgesPanel
    shown = []
    top = wx.Frame(None)
    p = BridgesPanel(top, lambda *a: None, on_show=lambda names, zoom: shown.append(names))
    top.Show()
    try:
        legs = [LegSpec(ph, f"PH_{ph}", [f"Q{k}"], [f"Q{k + 1}"]) for ph, k in (("A", 1), ("B", 3), ("C", 5))]
        p.bridges = [BridgeSpec("bridge J1", "VBUS", "GND", legs)]
        p.refresh()
        p.list.Select(0)
        wait_for(app, lambda: shown == [["bridge J1"]], timeout=5)     # the row: the whole bridge
        assert p.leg_list.GetItemCount() == 3
        p.leg_list.Select(1)
        wait_for(app, lambda: len(shown) == 2, timeout=5)
        assert shown[-1] == ["bridge J1 B"] and p.leg_high.GetValue() == "Q3"
    finally:
        top.Destroy()
