"""Logic buffers (Schmitt triggers) in signal paths: the part model `buffer`, found by its GT buf labels or its
threshold fields; detection runs one path through it (and through the input's RC filter resistor); Simulate cuts the
path at it, the stretch after it driven by the buffer's own edge; its input is a victim checked against its
hysteresis. The board: tests/boardgen.buffer_path_board (J1 → 10 kΩ → U5 SN74LVC2G17 → 1 kΩ → U2)."""
import numpy as np
import pytest

from boardgen import buffer_path_board
from conftest import make
from groundtruth.config.settings import NetSpec

THRESHOLDS = {"Threshold Rising": "2.5V", "Threshold Falling": "1.6V"}     # an SN74LVC2G17's at 5 V: 0.9 V apart


def _board(tmp_path, g, nets=()):
    """The generated board and its settings."""
    board, sc, _ = make(tmp_path, g, "buf", nets=nets, solve_points=3, max_solves=4)
    return board, sc.config


@pytest.mark.parametrize("how", ["labels", "fields"])
def test_detection_runs_one_path_through_the_buffer_and_its_input_resistor(tmp_path, how):
    """The user's Hall input came out as three fragments: the 10 kΩ filter resistor stopped the path (above the
    1 kΩ series limit) and nothing crossed the buffer. With GT = buf_in:1, buf_out:6, or with the threshold fields
    an HTTP library fills in (the pins paired by their names 1A / 1Y), it is one path J1.1 → U2.1 through R1 [A74],
    asked for from either side of the buffer."""
    from groundtruth.analysis.detect import suggest_net
    g = buffer_path_board(labels=True) if how == "labels" else buffer_path_board(**THRESHOLDS)
    board, cfg = _board(tmp_path, g)
    m = cfg.model_for("U5")
    assert m.kind == "buffer"
    if how == "fields":
        assert m.params == {"vt_pos": 2.5, "vt_neg": 1.6} and m.buf_hysteresis() == pytest.approx(0.9)
    for net in ("HW", "HALL_W"):
        d, _ = suggest_net(board, cfg, net)
        assert (d.spec.driver, d.spec.receivers, d.spec.through) == ("J1.1", ["U2.1"], ["R1"]), net
        assert any("logic buffer(s) U5" in r for r in d.reasons) and any("into a logic buffer" in r for r in d.reasons)


def test_without_labels_or_fields_the_buffer_stays_open(tmp_path):
    """Pin names alone don't make a part a buffer (a microcontroller has A / Y-like names too): U5 stays open and the
    path after it starts at its output, as before."""
    from groundtruth.analysis.detect import suggest_net
    board, cfg = _board(tmp_path, buffer_path_board())
    assert cfg.model_for("U5").kind == "open"
    d, _ = suggest_net(board, cfg, "HALL_W")
    assert d.spec.driver == "U5.6" and d.spec.receivers == ["U2.1"]


def test_a_dual_buffer_is_two_paths(tmp_path):
    """buf_in:1, buf_out:6, buf_in:3, buf_out:4: two channels, each its own path; neither runs into the other."""
    from groundtruth.analysis.detect import suggest_net
    from groundtruth.components.models import buffer_pins
    board, cfg = _board(tmp_path, buffer_path_board(labels=True, dual=True))
    fp = board.footprint("U5")
    assert [(a.number, b.number) for a, b in buffer_pins(fp, cfg.model_for("U5"))] == [("1", "6"), ("3", "4")]
    d1, _ = suggest_net(board, cfg, "HW")
    d2, _ = suggest_net(board, cfg, "IN2")
    assert (d1.spec.driver, d1.spec.receivers) == ("J1.1", ["U2.1"])
    assert (d2.spec.driver, d2.spec.receivers) == ("J3.1", ["U3.1"])


def test_a_buffer_model_keeps_its_channel_pairs_in_the_settings():
    """pins as input, output pairs (a dual: 1, 6, 3, 4) survive the settings file; an odd count is refused."""
    from groundtruth.components.models import ComponentModel
    m = ComponentModel.from_dict({"model": "buffer", "pins": "1, 6, 3, 4", "rise": 4e-9, "hyst": 0.8})
    again = ComponentModel.from_dict(m.to_dict())
    assert again.pins == ["1", "6", "3", "4"] and again.buf_rise() == 4e-9 and again.buf_hysteresis() == 0.8
    with pytest.raises(ValueError, match="pairs"):
        ComponentModel.from_dict({"model": "buffer", "pins": [1, 6, 3]})


def test_buffer_fields_set_its_model_and_an_unpaired_label_is_a_problem(tmp_path):
    """The six fields (an InvenTree part's parameters through the HTTP lib) are the model's values; a buf_in
    without its buf_out shows on the tabs' label line."""
    from groundtruth.analysis.tags import board_tags
    fields = dict(THRESHOLDS, **{"Rise Time": "4ns", "Zout": "30", "Cin": "3pF", "Hysteresis": "0.8V"})
    board, cfg = _board(tmp_path, buffer_path_board(**fields))
    m = cfg.model_for("U5")
    assert m.buf_rise() == pytest.approx(4e-9) and m.buf_z_out() == pytest.approx(30.0)
    assert m.params["c_in"] == pytest.approx(3e-12) and m.buf_hysteresis() == pytest.approx(0.8)    # hyst wins
    g = buffer_path_board()
    g.fields("U5", GT="buf_in:1")
    (tmp_path / "lone").mkdir()
    board2, _ = _board(tmp_path / "lone", g)
    assert any("1 buf_in and 0 buf_out" in p for p in board_tags(board2).problems)


def test_trapezoid_at_is_the_trapezoid_harmonics_at_whole_harmonics():
    """trapezoid_at (any frequency) gives trapezoid_harmonics' c_n exactly at n·f0 [Paul 2006], also with a
    fall time of its own."""
    from groundtruth.emi.spectrum import trapezoid_at, trapezoid_harmonics
    for fall in (None, 30e-9):
        f, a = trapezoid_harmonics(1e6, 10e-9, 3.3, 0.3, 500e6, fall)
        assert np.allclose(trapezoid_at(f, 1e6, 10e-9, 3.3, 0.3, fall), a, rtol=1e-9, atol=1e-15)


def test_a_buffer_drives_the_stretch_after_it_with_its_own_source():
    """amplified_path with a PathBuffer, against the circuit by hand [Desoer 1969]: the stretch before it is a
    divider into the buffer's input impedance; the stretch after it is the buffer's own source v_b(f) behind its
    z_out into the series part and the load, whatever the input did."""
    from groundtruth.emi.path import PathBuffer, PathLoad, PathPart, PathSource, amplified_path
    f = np.array([1e5, 1e6, 1e7, 1e8])
    c_in, z_out, r1, r3, c_load = 5e-12, 25.0, 10e3, 1e3, 10e-12
    z_in = lambda fk: 1.0 / (2j * np.pi * fk * c_in)                      # noqa: E731
    v_b = lambda fs: 5.0 / (1.0 + np.asarray(fs) / 1e7)                    # noqa: E731
    buf = PathBuffer("U5", z_in, z_out, v_b)
    out = amplified_path(f, np.ones(len(f)), [[PathPart("R1", lambda fk: r1)], [PathPart("R3", lambda fk: r3)]],
                         [buf], PathSource(50.0), PathLoad(c_load))
    zi = np.array([z_in(x) for x in f])
    assert np.allclose(out[0].v_end, zi / (50.0 + r1 + zi), rtol=1e-6)
    zl = 1.0 / (2j * np.pi * f * c_load)
    assert np.allclose(out[1].i_in, v_b(f) / (z_out + r3 + zl), rtol=1e-6)


def test_simulate_drives_the_stretch_after_the_buffer_with_its_edge(tmp_path):
    """A 1 kHz, 2 µs Hall signal through U5 (a 4 ns edge, on +5V): one route, two stages; each harmonic's current
    is the larger of the two stages' spectra (the slow input's trapezoid at 3.3 V, the buffer's at 5 V), so the
    fast edge after the buffer counts where the input has nearly nothing [A76]. The Bode ends at U5's input, and the
    report names the buffer."""
    from groundtruth.analysis.simulate import simulate
    from groundtruth.emi.spectrum import trapezoid_at
    from groundtruth.report.html import render_html
    spec = NetSpec("HW", "J1.1", ["U2.1"], 1e3, 2e-6, through=["R1"])
    board, cfg = _board(tmp_path, buffer_path_board(**dict(THRESHOLDS, **{"Rise Time": "4ns"})), nets=(spec,))
    res = simulate(board, cfg)
    rr = res.routes[0]
    assert not rr.error, rr.error
    kinds = [(e["kind"], e.get("ref")) for e in rr.path["elements"] if e["kind"] != "line"]
    assert kinds == [("part", "R1"), ("shunt", "C1"), ("amp", "U5"), ("part", "R3"), ("shunt", "C2")], kinds
    assert any("logic buffer(s) U5: 2 stages" in n for n in rr.notes), rr.notes
    assert any("Bode: to the logic buffer's input U5.1" in n for n in rr.notes), rr.notes
    s = res.setups[0]
    f = np.asarray(s.harmonics, float)
    want = np.maximum(trapezoid_at(f, 1e3, 2e-6, 3.3, 0.5), trapezoid_at(f, 1e3, 4e-9, 5.0, 0.5))
    assert np.allclose(s.currents, want, rtol=1e-9)
    assert f.max() > 100e6 and len(f) <= 4000            # the buffer's edge sets the band, thinned
    hi = f > 10e6               # far above the input's corner the buffer's edge carries it (but at its own nulls)
    assert s.currents[hi].sum() > 100 * trapezoid_at(f[hi], 1e3, 2e-6, 3.3, 0.5).sum()
    assert "logic buffer (drives the stretch after it" in render_html(board, res.issues, res.routes, "simulate")


def test_a_schmitt_input_is_a_victim_checked_against_its_hysteresis(tmp_path):
    """Its input net is a victim by itself (HW_F matches no name pattern); noise at U5.1 is checked against the
    0.9 V hysteresis instead of the flat limit, and the issue says so [A75]; its Cin is the receiver's load."""
    from groundtruth.analysis.victims import auto_nets, buffer_input, finish_rows, victim_text
    board, cfg = _board(tmp_path, buffer_path_board(**dict(THRESHOLDS, Cin="3pF")))
    assert auto_nets(board, cfg, set()).get("HW_F") == "Schmitt input of U5"
    assert buffer_input(board, cfg, board.find_pin("U5.1")) == ("U5", pytest.approx(0.9), pytest.approx(3e-12))
    assert buffer_input(board, cfg, board.find_pin("U5.6")) is None             # its output is no input
    row = {"kind": "net", "name": "HW_F", "to": "U5.1", "from": "R1.2", "why": "Schmitt input of U5", "peak": 1.2,
           "leg": "bridge A", "m_nh": 0.5}
    out = finish_rows([dict(row)], cfg.victims, False, board, cfg)[0]
    assert out["limit"] == pytest.approx(0.9) and out["limit_why"] == "U5's hysteresis"
    assert "against U5's hysteresis" in victim_text(out)
    plain = finish_rows([dict(row)], cfg.victims, False)[0]                     # without the board: the flat limit
    assert "limit_why" not in plain and plain["limit"] != pytest.approx(0.9)
