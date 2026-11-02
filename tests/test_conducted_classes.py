"""Conducted-emission standards and classes as TI WEBENCH offers them (CISPR 22 class A / B, CISPR 25 class 1-5): the
limit lines per class, which class a stage is checked against (its own, the analysis', the standard's strictest),
the settings, and the window's choices."""
import numpy as np
import pytest

from boardgen import hot_loop_board
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import AnalysisSettings, ConverterSpec, SettingsFile, config_from_data
from groundtruth.emi.conducted import (CISPR25_AV5, CLASSES, band_limits, describe, limit_line, normal_class,
                                       stage_limit)
from groundtruth.ui import limit_choice as lc

from test_power import SPEC, make_conv


def test_cispr_22_has_cispr_32s_conducted_limits():
    """CISPR 32 took CISPR 22's mains-port limits over unchanged: class B 56-46 dBµV falling with log f to 500 kHz,
    46 to 5 MHz, 50 to 30 MHz; class A 66, then 60 (average) [CISPR 22] [CISPR 32, Annex A]."""
    f = np.array([150e3, 500e3 - 1, 1e6, 10e6, 29e6])
    for cls in ("A", "B"):
        assert np.allclose(limit_line(f, "cispr22", cls), limit_line(f, "cispr32", cls))
    assert np.allclose(limit_line(f, "cispr22", "B"), [56.0, 46.0, 46.0, 50.0, 50.0], atol=1e-4)
    assert np.allclose(limit_line(f, "cispr22", "A"), [66.0, 66.0, 60.0, 60.0, 60.0])
    assert np.sqrt(10) * 150e3 == pytest.approx(474.3e3, rel=1e-3)          # half-way in log f: 51 dBµV
    assert float(limit_line([np.sqrt(150e3 * 500e3)], "cispr22", "B")[0]) == pytest.approx(51.0)
    assert describe("cispr22", "A") == "CISPR 22 class A (average)"


def test_cispr_25_classes_step_up_from_class_5_per_band():
    """Class k is class 5 plus (5 - k) of the band's steps (LW 10 dB, MW 8, the higher bands 6) [CISPR 25] [A10]:
    the steps TI WEBENCH's curves show too (its peak lines: LW 70/80/90/100/110 dBµV for classes 5 to 1)."""
    for k in range(1, 6):
        bands = band_limits("cispr25", str(k))
        for (lo, hi, v, _), (lo5, hi5, v5, step) in zip(bands, CISPR25_AV5):
            assert (lo, hi) == (lo5, hi5) and v == v5 + (5 - k) * step
    assert band_limits("cispr25", "1")[0][2] == 90.0 and band_limits("cispr25", "5")[0][2] == 50.0
    assert np.isnan(limit_line([3e6], "cispr25", "5")[0])     # between MW and SW: no limit
    assert describe("cispr25", "3") == "CISPR 25 class 3 (voltage method, average)"


def test_a_stage_reads_the_analysis_class_only_with_the_analysis_standard():
    """The bug this branch fixes: a stage naming CISPR 25 took the analysis' CISPR 32 class "B" (no CISPR 25 class:
    silently class 5); a stage naming CISPR 32 under a CISPR 25 class 3 analysis took "3" (silently B)."""
    a = AnalysisSettings(conducted="cispr32", conducted_class="A")
    assert stage_limit(None, None, a) == ("cispr32", "A")               # inherited standard: its class too
    assert stage_limit("cispr32", None, a) == ("cispr32", "A")          # the same standard named again
    assert stage_limit("cispr25", None, a) == ("cispr25", "5")          # another standard: its strictest
    assert stage_limit("cispr25", "3", a) == ("cispr25", "3")           # its own class
    assert stage_limit("cispr22", None, a) == ("cispr22", "B")
    assert stage_limit(None, "B", a) == ("cispr32", "B")                # a class with the analysis' standard
    assert stage_limit("none", "3", a) == ("none", "")
    a25 = AnalysisSettings(conducted="cispr25", conducted_class="B")    # an old file: the default "B"
    assert stage_limit(None, None, a25) == ("cispr25", "5")
    assert stage_limit("mil461", None, a25) == ("mil461", "basic")
    assert normal_class("mil461", "28V") == "basic" and normal_class("cispr22", "b") == "B"
    assert normal_class("cispr25", "6") is None and normal_class("none", "B") is None


def test_classes_are_read_written_and_checked_in_the_settings(tmp_path):
    """Per stage and per bridge `conducted_class` round-trips ("CLASS 3" read as "3"); a class the standard does
    not have is an error naming the valid ones, for the analysis and for a stage."""
    data = {"board": "x.kicad_pcb", "analysis": {"conducted": "cispr22", "conducted_class": "A"},
            "converters": [{"name": "a", "rail": "VIN", "ground": "GND", "switch": ["U1"], "f_sw": "200kHz",
                            "i_out": 2, "rise_time": "10ns", "conducted": "cispr25", "conducted_class": "CLASS 3"}]}
    cfg = config_from_data(data)
    assert cfg.converters[0].conducted_class == "3" and cfg.analysis.conducted == "cispr22"
    assert stage_limit(cfg.converters[0].conducted, cfg.converters[0].conducted_class, cfg.analysis) == \
        ("cispr25", "3")
    with pytest.raises(Exception) as e:
        config_from_data({**data, "analysis": {"conducted": "cispr25", "conducted_class": "B"}})
    assert "conducted_class 'B' is not a class of cispr25: one of 1, 2, 3, 4, 5" in str(e.value)
    bad = {**data["converters"][0], "conducted": "cispr22", "conducted_class": "4"}
    with pytest.raises(Exception) as e:
        config_from_data({**data, "converters": [bad]})
    assert "not a class of cispr22: one of A, B" in str(e.value)
    # written back with the stage
    from boardgen import hot_loop_board as gen
    _, sc, _ = make_conv(tmp_path, gen(20.0), "rt", [ConverterSpec(**{**SPEC.__dict__, "conducted": "cispr25",
                                                                        "conducted_class": "2"})])
    again = SettingsFile.load(sc.path).config.converters[0]
    assert (again.conducted, again.conducted_class) == ("cispr25", "2")


def test_the_window_names_standards_and_classes_as_webench():
    """The choices' entries: CISPR 22 and CISPR 25 first, CLASS A / B and CLASS 1-5, mapped to the settings' keys
    and back."""
    assert lc.standard_items()[:3] == ["(analysis default)", "CISPR 22", "CISPR 25"]
    assert lc.class_items("cispr22") == ["(default)", "CLASS A", "CLASS B"]
    assert lc.class_items("cispr25") == ["(default)"] + [f"CLASS {k}" for k in range(1, 6)]
    assert lc.class_items("none") == ["(default)"]
    for std in CLASSES:
        assert lc.standard_key(lc.standard_text(std)) == std
        for c in CLASSES[std]:
            assert lc.class_key(std, lc.class_text(std, c)) == c
    assert lc.standard_key("(analysis default)") is None and lc.class_key("cispr25", "(default)") is None
    assert lc.effective_standard("(analysis default)", AnalysisSettings(conducted="cispr25")) == "cispr25"
    assert lc.default_tip(AnalysisSettings(conducted="cispr25")) == \
        "(analysis default): CISPR 25 class 5 (voltage method, average)"


def test_a_stages_class_moves_its_limit_by_the_class_steps_and_nothing_else(tmp_path):
    """Two copies of one stage on one board, both on CISPR 25 under a CISPR 32 class B analysis: one class 3, one
    without a class (class 5, the strictest). Their readings are the same; every limit and margin differs by
    exactly two of the band's steps."""
    base = {**SPEC.__dict__, "input_connector": ["J1.1", "J1.2"], "conducted": "cispr25"}
    c3 = ConverterSpec(**{**base, "name": "c3", "conducted_class": "3"})
    c5 = ConverterSpec(**{**base, "name": "c5"})
    board, sc, _ = make_conv(tmp_path, hot_loop_board(20.0, caps=(("C1", "1u", "end", 0.0),), connector=True),
                             "cls", [c3, c5])
    res = simulate(board, sc.config)
    r3, r5 = (next(r for r in res.converters if r.net == n) for n in ("c3", "c5"))
    assert not r3.error and not r5.error
    assert r3.conducted_standard == "CISPR 25 class 3 (voltage method, average)"
    assert r5.conducted_standard == "CISPR 25 class 5 (voltage method, average)"
    rows5 = {round(x["f"]): x for x in r5.conducted}
    assert r3.conducted and len(rows5) == len(r3.conducted)
    assert any(row["limit_dbuv"] is not None for row in r3.conducted)
    steps = {(lo, hi): step for lo, hi, _, step in CISPR25_AV5}
    for row in r3.conducted:
        other = rows5[round(row["f"])]
        assert row["v_dbuv"] == pytest.approx(other["v_dbuv"], abs=1e-6)
        if row["limit_dbuv"] is None:          # between CISPR 25's bands: no limit in either class
            assert other["limit_dbuv"] is None
            continue
        step = min(s for (lo, hi), s in steps.items() if lo <= row["f"] <= hi)   # the lowest limit applies
        assert row["limit_dbuv"] - other["limit_dbuv"] == pytest.approx(2 * step, abs=1e-6)
        assert row["margin_db"] - other["margin_db"] == pytest.approx(2 * step, abs=1e-6)


def test_the_help_explains_the_classes_with_charts_from_the_limit_tables():
    """The help's section draws each standard's classes from emi/conducted.py (an SVG per chart in the browser view,
    tables in wx's simple view), and the Converters / Bridges sections link to it."""
    from groundtruth.ui import help as h
    assert "conducted" in h.TOPICS
    _, found = h.sections()
    body = next(b for a, _, b in found if a == "conducted")
    assert body.count("<svg") == 3 and "@LIMITS_" not in body
    assert "CISPR 25 CLASS 3, average, 0.53-1.8 MHz: 50 dBµV" in body       # a band's tooltip, from the table
    assert "CISPR 22 CLASS A, quasi-peak, 0.15-0.5 MHz: 79 dBµV" in body
    classic = h.page()
    sec = classic.split('name="conducted"')[1].split('name="issues"')[0]
    assert "<svg" not in sec and "50 (70)" in sec and "56-46 (66-56)" in sec
    assert classic.count('href="#conducted"') >= 3
    assert "--cls5:" in h.css_vars()


def test_the_plotted_limit_spans_each_band_not_only_its_harmonics():
    """The user, of a 750 kHz stage's CISPR 25 class 5 plot: "why is the red cispr line not complete?". Drawn
    through the harmonics' limits, MW (0.53-1.8 MHz) showed only from 0.75 to 1.5 MHz and SW (5.9-6.2 MHz, one
    harmonic at 6.0 MHz) not at all. Drawn from the bands: MW from 0.53 MHz, SW's 33 dBµV line, no line between
    the bands (the standard limits the broadcast bands only); a result without the keys keeps the old drawing."""
    from types import SimpleNamespace as NS
    from matplotlib.figure import Figure
    from groundtruth.emi.conducted import limit_line
    from groundtruth.report.plots import _conducted_limit
    f = np.arange(1, 145) * 0.75                               # MHz: 750 kHz harmonics to 108 MHz
    lim = limit_line(f * 1e6, "cispr25", "5")
    ax = Figure().add_subplot(111)
    _conducted_limit(ax, NS(conducted_limit=("cispr25", "5")), f, lim)
    x, y = (np.asarray(v, float) for v in ax.lines[0].get_data())
    mw = np.isfinite(y) & (x >= 0.5) & (x <= 1.9)
    assert x[mw].min() == pytest.approx(0.53, rel=1e-3) and x[mw].max() == pytest.approx(1.8, rel=1e-3)
    sw = np.isfinite(y) & (x > 5.8) & (x < 6.3)
    assert sw.sum() > 5 and np.allclose(y[sw], 33.0)
    assert not np.isfinite(y[(x > 2.0) & (x < 5.8)]).any()     # no limit between MW and SW
    ax2 = Figure().add_subplot(111)
    _conducted_limit(ax2, NS(), f, lim)                        # an old result: through the harmonics
    assert np.allclose(np.asarray(ax2.lines[0].get_xdata(), float), f)
