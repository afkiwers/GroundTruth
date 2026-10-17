"""A power stage's switching and its victims (analysis/victims.py for a converter, the plan's M3): the reset line of a
microcontroller near the discrete buck's hot loop picks up noise at its receiver pin (the mutual inductance to the
loop times the switched current's slope), less when it runs farther away; over the limit it is an issue; the
victim model changes none of the stage's own figures."""
import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.analysis.detect import detect_converters
from groundtruth.analysis.simulate import simulate

NEAR, FAR = (6.0, 30.0, 4.0), (26.0, 37.0, 28.0)


def _run(tmp_path, name, victim, **victims):
    """Simulate the buck (12 V in, 3 A, 300 kHz) with a reset line at `victim` (None: none); its result and issues."""
    board, sc, _ = make(tmp_path, discrete_buck_board(victim=victim), name, nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.0
    sc.add_detected_converters([spec], {})
    if victims:
        sc.doc["victims"] = victims
    sc.save()
    from groundtruth.config.settings import SettingsFile
    res = simulate(board, SettingsFile.load(sc.path).config)
    (r,) = res.converters
    assert not r.error, r.error
    return r, res.issues


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """The buck alone, with the reset line near the loop, and far from it."""
    return {k: _run(tmp_path_factory.mktemp(k), k, v) for k, v in (("none", None), ("near", NEAR), ("far", FAR))}


def test_the_reset_line_picks_up_noise_at_its_receiver(runs):
    """U5 (an STM32) and its reset are found; the noise is read at U5.4, driven from the pull-up R5.1; it is the
    coupling M times the switched current's slope I/t_r; U5's ground against J1's minus is reported (information)."""
    r, issues = runs["near"]
    rows = {(v["kind"], v["name"]): v for v in r.victims}
    net = rows[("net", "NRST")]
    assert (net["from"], net["to"], net["why"], net["leg"]) == ("R5.1", "U5.4", "reset", r.net)
    assert net["peak"] == pytest.approx(net["m_nh"] * 1e-9 * r.pulse_a / r.rise_time)
    part = rows[("part", "U5")]
    assert part["why"] == "microcontroller" and part["reference"] == "J1.2" and not part["checked"]
    from groundtruth.report.formats import converter_text
    assert "noise at U5.4 on NRST" in "\n".join(converter_text(r))


def test_farther_from_the_loop_the_noise_is_smaller(runs):
    """The same line routed away from the hot loop (the top right corner): less coupling."""
    near = next(v for v in runs["near"][0].victims if v["kind"] == "net")
    far = next(v for v in runs["far"][0].victims if v["kind"] == "net")
    assert far["m_nh"] < near["m_nh"] / 2


def test_the_victim_model_leaves_the_stages_own_figures_alone(runs):
    """The victims are solved on a model of their own: the hot loop's inductance and the conducted estimate are the
    same as without the reset line (within the grid's change near it); no victims without a victim."""
    alone, _ = runs["none"]
    near, _ = runs["near"]
    assert alone.victims == []
    assert near.loop_nh == pytest.approx(alone.loop_nh, rel=0.02)


def test_over_its_limit_the_noise_is_an_issue(tmp_path):
    """induced_warning_v 0.1 mV (the 10k pull-up leaves U5.4 about 0.15 mV): the reset line's noise is a
    victim_coupling issue of the stage."""
    r, issues = _run(tmp_path, "limit", NEAR, induced_warning_v=1e-4)
    (i,) = [i for i in issues if i.check == "victim_coupling"]
    assert i.net == r.net and "NRST" in i.message and i.details["kind"] == "net"
