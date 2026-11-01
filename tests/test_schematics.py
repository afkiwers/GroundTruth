"""The design changes' example schematics (report/schematics.py): each change with a circuit is drawn with the
advisor's values on its parts; a change without one gets none."""
from types import SimpleNamespace as NS

import pytest

from groundtruth.report.schematics import Ink, figure_for, png

STAGE = NS(driver="Q1", receiver="Q2", topology="buck", rise_time=2e-9, loop_nh=0.8)
FILTER = {"L": 3.3e-6, "C": 11e-6, "R_damp": 0.56, "C_damp": 47e-6, "dcr": 5.4e-3, "damp_at": "converter",
          "C_nom": 22e-6, "i_sat": 6.6, "v_rating": 82, "v": 55, "f_c": 26e3, "standard": "cispr25", "c_in": 9.4e-6}


def texts(fig) -> str:
    """Every text of a figure, joined."""
    return " | ".join(t.get_text() for t in fig.axes[0].texts)


@pytest.mark.parametrize("damp_at", ["converter", "connector"])
def test_the_input_filter_shows_the_proposed_values_between_the_lisn_and_the_stage(damp_at):
    """The π-filter as WEBENCH draws its input filter: the LISN of the standard, C_f (nominal, rating, at bias), L_f
    (I_sat, DCR), the damping R_d + C_d, the stage's input capacitors, the stage, the corner."""
    fig = figure_for({"kind": "filter", "change": {"filter": {**FILTER, "damp_at": damp_at}}}, "buck", Ink(), STAGE)
    t = texts(fig)
    for want in ("5 µH ∥ 50 Ω", "22µF X7R", "≥ 82 V rating", "≈ 11µF at 55 V", "3.3µH, I_sat ≥ 6.6 A",
                 "DCR ≈ 5.4mΩ", "560mΩ", "47µF", "9.4µF", "power stage\nbuck", "26kHz"):
        assert want in t, want
    assert png(fig)[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_snubber_and_the_edge_are_drawn_on_the_half_bridge():
    """A snubber's R and C across the switch node, a slower edge's gate resistor with the edge (and today's), the
    stage's switches by name; "together" draws all of it and the hot-loop target."""
    t = texts(figure_for({"kind": "snubber", "change": {"snubber_c": 2.7e-9, "snubber_r": 1.3}}, "b", Ink(), STAGE))
    assert "1.3Ω" in t and "2.7nF C0G" in t and "Q1" in t and "Q2" in t
    t = texts(figure_for({"kind": "edge", "change": {"rise_time": 6e-9}}, "b", Ink(), STAGE))
    assert "edge 6ns\n(now 2ns)" in t
    t = texts(figure_for({"kind": "combined", "change": {"rise_time": 4e-9, "snubber_c": 2.7e-9, "snubber_r": 1.3,
                                                         "loop_scale": 0.7}}, "b", Ink(), STAGE))
    assert "edge 4ns" in t and "2.7nF" in t and "hot loop ≤ 0.56 nH" in t


def test_common_mode_shows_the_budget_and_changes_without_a_circuit_draw_nothing():
    """The common-mode drawing carries the switch node's capacitance budget from the title; a shorter loop, spread
    spectrum and the stage as it is have no schematic."""
    t = texts(figure_for({"kind": "cm", "change": {},
                          "title": "common mode: the switch node may have at most 0.015 pF to a chassis"},
                         "b", Ink(), STAGE))
    assert "≤ 0.015 pF" in t and "common-mode choke" in t
    for kind in ("loop", "spread", "now"):
        assert figure_for({"kind": kind, "change": {}}, "b", Ink(), STAGE) is None
