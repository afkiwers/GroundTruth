"""The band an emission estimate covers (2026-10-20, branch `qs-band`): where the board-wide model stops being
quasi-static (its emissions marked indicative upper bounds [A47]) and analysis.max_frequency, the user's cut of the
band evaluated (what lies above it said to be not computed, never passed)."""
from __future__ import annotations

import math

import pytest
from matplotlib.figure import Figure

from boardgen import BoardGen, hot_loop_board, layer_change_board
from conftest import make
from groundtruth.adapters.file_source import load_board
from groundtruth.analysis.simulate import QS_FRACTION, quasi_static_limit, simulate
from groundtruth.config.settings import ConfigError, ConverterSpec, NetSpec, SettingsFile, config_from_data

C0 = 299_792_458.0
SIG = NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9)


def test_the_quasi_static_limit_is_a_quarter_wavelength_across_the_board(tmp_path):
    """f_qs = c / (4·size·√εr) on the board's longer outline side and its densest dielectric [A47]: 100 x 60 mm of
    εr 4.5 at 353.3 MHz; a board twice as long, half that."""
    for (w, h), want in (((100.0, 60.0), 0.25 * C0 / (0.100 * math.sqrt(4.5))),
                         ((60.0, 200.0), 0.25 * C0 / (0.200 * math.sqrt(4.5)))):
        b = load_board(BoardGen(layers=2, size=(w, h)).write(tmp_path / f"b{int(w)}.kicad_pcb"))
        qs = quasi_static_limit(b)
        assert qs["f"] == pytest.approx(want, rel=1e-9) and qs["size_mm"] == pytest.approx(max(w, h))
    assert QS_FRACTION == 0.25 and 353e6 < 0.25 * C0 / (0.1 * math.sqrt(4.5)) < 354e6


def test_max_frequency_reads_auto_a_frequency_or_refuses_nonsense():
    """analysis.max_frequency: auto (None) by default, an SI frequency ("1GHz", 3e8), never 0 or below."""
    assert config_from_data({}).analysis.max_frequency is None
    assert config_from_data({"analysis": {"max_frequency": "auto"}}).analysis.max_frequency is None
    assert config_from_data({"analysis": {"max_frequency": "1GHz"}}).analysis.max_frequency == pytest.approx(1e9)
    assert config_from_data({"analysis": {"max_frequency": 3e8}}).analysis.max_frequency == pytest.approx(3e8)
    for bad in (0, -5, "fast"):
        with pytest.raises(ConfigError):
            config_from_data({"analysis": {"max_frequency": bad}})


@pytest.fixture(scope="module")
def cavity_route(tmp_path_factory):
    """The layer-change board with the board-wide model, a 50 MHz, 1 ns signal (band to 3.2 GHz): its route."""
    tmp = tmp_path_factory.mktemp("qs")
    board, sc, _ = make(tmp, layer_change_board(None), "qs", nets=(SIG,), capacitance=True, solve_points=3,
                        max_solves=6, cavity_max_nodes=12000)
    res = simulate(board, sc.config)
    return board, res.routes[0], res.issues


def test_emissions_above_the_quasi_static_limit_are_marked_indicative(cavity_route):
    """With the board-wide model, every emission row above f_qs is "indicative" and none below; the band records
    f_qs, and a note says what the rows there are (upper bounds); a margin or plane-resonance issue says so when its
    frequency lies there, and only then."""
    board, rr, issues = cavity_route
    f_qs = quasi_static_limit(board)["f"]
    assert rr.band["f_qs"] == pytest.approx(f_qs) and not rr.band["capped"]
    above = [r for r in rr.emissions if r["f"] > f_qs]
    assert above and all(r.get("indicative") for r in above)
    assert not any(r.get("indicative") for r in rr.emissions if r["f"] <= f_qs)
    assert any("quarter wavelength" in n and "upper bounds" in n for n in rr.notes)
    assert bool(rr.worst.get("indicative")) == (rr.worst["f"] > f_qs)
    for i in issues:                    # a margin or a resonance above f_qs says it is an upper bound
        f = i.details.get("f")
        if i.check in ("emi_margin", "plane_resonance") and f is not None:
            assert ("indicative upper bound" in i.message) == (f > f_qs), i.message


def test_without_the_board_wide_model_nothing_is_marked(tmp_path):
    """A route alone (no plane capacitance) is a loop of a few millimetres: quasi-static far higher, no row marked
    and no f_qs recorded."""
    board, sc, _ = make(tmp_path, layer_change_board(None), "noqs", nets=(SIG,), capacitance=False)
    rr = simulate(board, sc.config).routes[0]
    assert not any(r.get("indicative") for r in rr.emissions) and "f_qs" not in rr.band


def test_a_capped_band_stops_there_and_says_it_was_not_checked(tmp_path):
    """analysis.max_frequency = 300 MHz on a 1 ns signal (its own band to 3.2 GHz): no harmonic above 300 MHz is
    evaluated, the band record says capped with both tops, and a note says the rest was not computed (not passed).
    The spectrum hatches what was cut and shades the indicative range."""
    from groundtruth.report.plots import draw_spectrum
    board, sc, _ = make(tmp_path, layer_change_board(None), "cap", nets=(SIG,), capacitance=True, solve_points=3,
                        max_solves=6, cavity_max_nodes=12000, max_frequency="300MHz")
    rr = simulate(board, sc.config).routes[0]
    assert max(r["f"] for r in rr.emissions) <= 300e6
    assert rr.band["capped"] and rr.band["f_max"] == pytest.approx(300e6)
    assert rr.band["natural"] == pytest.approx(10 / (math.pi * 1e-9))
    assert any("max_frequency" in n and "not checked" in n for n in rr.notes)
    fig = Figure(figsize=(6, 3))
    draw_spectrum(fig, rr)
    labels = [p.get_label() for p in fig.axes[0].patches]
    assert "not evaluated" in labels


def test_a_power_stage_takes_the_cap_too(tmp_path):
    """A power stage's band (its edge's, at least to its ringing) is cut the same way: its conducted and radiated
    rows end at the cap, with the note."""
    spec = ConverterSpec("hot", "VIN", "GND", switch=["U1"], f_sw=200e3, i_out=2.0, v_in=12.0, v_out=5.0,
                         rise_time=10e-9)
    board, sc, _ = make(tmp_path, hot_loop_board(20.0, connector=True), "pcap", nets=(), max_frequency="20MHz")
    sc.set_converters([spec])
    sc.save()
    r = simulate(board, SettingsFile.load(sc.path).config).converters[0]
    assert not r.error, r.error
    assert r.band["capped"] and r.band["f_max"] == pytest.approx(20e6)
    assert all(row["f"] <= 20e6 for row in r.conducted + r.emissions)
    assert any("max_frequency" in n for n in r.notes)
