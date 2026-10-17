"""The fix advisor (analysis/fix_advisor.py): design changes for a power stage evaluated on Simulate's own solves.
Every change is checked against a full Simulate with that change in the settings, or against the circuit it
models solved directly."""
import math
from dataclasses import replace

import numpy as np
import pytest

from boardgen import hot_loop_board
from conftest import make
from groundtruth.analysis.fix_advisor import (E6, E24, Change, evaluate, filter_ratio, middlebrook, nearest,
                                              stage_options)
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import ConverterSpec

SPEC = ConverterSpec("hot", "VIN", "GND", switch=["U1"], f_sw=500e3, i_out=5.0, v_in=48.0, v_out=12.0,
                     rise_time=2e-9, c_oss=600e-12, input_connector=["J1.1", "J1.2"])


def solve(tmp, name, spec=SPEC):
    board, sc, _ = make(tmp, hot_loop_board(20.0, connector=True), name, nets=())
    sc.set_converters([spec])
    sc.save()
    res = simulate(board, sc.config)
    return res.converters[0], res.converter_setups[0], board


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return solve(tmp_path_factory.mktemp("fa"), "fa")


def worst(rows):
    return min(r["margin_db"] for r in rows if r["margin_db"] is not None)


def test_no_change_gives_the_stages_own_figures(base):
    r, setup, _ = base
    o = evaluate(setup)
    assert o.f_ring == pytest.approx(r.ringing["f"], rel=1e-12) and o.q == pytest.approx(r.ringing["q"], rel=1e-12)
    assert o.overshoot_v == pytest.approx(max(r.overshoot_v, r.ringing["overshoot_v"]), rel=1e-12)
    # the reports keep the strongest reading per receiver bin and decade: the worst row is the worst harmonic
    assert o.radiated_db == pytest.approx(r.worst["margin_db"], abs=1e-9)
    assert o.conducted_db == pytest.approx(r.conducted_worst["margin_db"], abs=1e-9)
    assert o.cm_budget_pf == pytest.approx(r.cm["budget_pf"], rel=1e-9) and o.loss_w == 0.0


@pytest.mark.parametrize("change, spec_change", [
    (Change(rise_time=4e-9), {"rise_time": 4e-9}),
    (Change(snubber=(2.2e-9, 1.5)), {"snubber_c": 2.2e-9, "snubber_r": 1.5})])
def test_a_change_predicts_what_simulate_gives_with_it(base, tmp_path, change, spec_change):
    """The advisor's prediction against a full Simulate of the stage with the change in its settings (a slower edge
    or a proposed snubber, the YAML `snubber: {C, R}`): the loop solves are the same, so the figures agree up to
    the harmonics the radiated band's refinement picked (a slower edge moves them)."""
    r, setup, _ = base
    o = evaluate(setup, change)
    r2, _, _ = solve(tmp_path, "fa2", replace(SPEC, **spec_change))
    assert o.f_ring == pytest.approx(r2.ringing["f"], rel=1e-9) and o.q == pytest.approx(r2.ringing["q"], rel=1e-9)
    assert o.overshoot_v == pytest.approx(max(r2.overshoot_v, r2.ringing["overshoot_v"]), rel=1e-9)
    assert o.radiated_db == pytest.approx(r2.worst["margin_db"], abs=0.5)
    assert o.conducted_db == pytest.approx(r2.conducted_worst["margin_db"], abs=0.5)


def test_the_costs_are_the_textbook_ones(base):
    r, setup, _ = base
    c, v, f = 2.2e-9, 48.0, 500e3
    assert evaluate(setup, Change(snubber=(c, 1.5))).loss_w == pytest.approx(c * v * v * f, rel=1e-12)
    # a synchronous stage (an integrated one counts as one): one hard edge, ½·V·I·Δt·f_sw
    assert evaluate(setup, Change(rise_time=4e-9)).loss_w == pytest.approx(0.5 * v * 5.0 * 2e-9 * f, rel=1e-12)


def test_the_filter_ratio_is_the_circuits_current_divider(base):
    """filter_ratio against the circuit solved by nodal analysis: the stage's Norton current into node A (its input
    capacitors), the filter L from A to B, the filter capacitor (damped) and the LISN from B to ground."""
    r, setup, _ = base
    from groundtruth.analysis.fix_advisor import _filter_parts
    flt = {"L": 4.7e-6, "C": 2.2e-6, "R_damp": 1.5, "C_damp": 10e-6, "dcr": 5e-3, "damp_at": "connector"}
    f = np.geomspace(150e3, 30e6, 25)
    z_th, _, z_l, z_c, z_lisn = _filter_parts(setup, f, flt)
    want = []
    for k in range(len(f)):
        y = np.array([[1 / z_th[k] + 1 / z_l[k], -1 / z_l[k]], [-1 / z_l[k], 1 / z_l[k] + 1 / z_c[k] + 1 / z_lisn[k]]])
        va, vb = np.linalg.solve(y, [1.0, 0.0])
        with_f = vb / z_lisn[k]
        without = z_th[k] / (z_th[k] + z_lisn[k])
        want.append(abs(with_f / without))
    assert filter_ratio(setup, f, flt) == pytest.approx(np.array(want), rel=1e-9)
    hi = filter_ratio(setup, np.array([3e6, 30e6]), flt)
    assert 20 * math.log10(hi[1] / hi[0]) < -30                # a second-order filter: about -40 dB a decade


def test_middlebrook_compares_the_filters_peak_with_the_converters_input_impedance(base):
    r, setup, _ = base
    flt = {"L": 4.7e-6, "C": 2.2e-6, "R_damp": 1.5, "C_damp": 10e-6, "dcr": 5e-3, "damp_at": "converter"}
    mb = middlebrook(setup, flt)
    p_in = 48.0 * 5.0 * r.duty
    assert mb["z_in"] == pytest.approx(48.0 ** 2 / p_in, rel=1e-12)
    undamped = middlebrook(setup, dict(flt, R_damp=1e6))
    assert undamped["z_out_peak"] > mb["z_out_peak"]          # the damping lowers the resonance's peak


def test_standard_values():
    assert nearest(2.4e-9, E24) == pytest.approx(2.4e-9) and nearest(3.1e-6, E6) == pytest.approx(3.3e-6)
    assert nearest(3.4e-6, E6, up=True) == pytest.approx(4.7e-6) and nearest(1.0, E6, up=True) == pytest.approx(1.0)


def test_the_options_name_parts_predictions_and_what_they_clear(base):
    r, setup, board = base
    rows = stage_options(setup, board)
    assert rows and rows[0]["kind"] == "now" and rows[0]["outcome"]["radiated_db"] == pytest.approx(
        evaluate(setup).radiated_db)
    kinds = {o["kind"] for o in rows}
    assert {"snubber", "edge", "filter"} <= kinds
    snub = next(o for o in rows if o["kind"] == "snubber")
    assert snub["parts"][0].startswith("C ") and snub["change"]["snubber_c"] > 0
    assert snub["outcome"]["overshoot_v"] < rows[0]["outcome"]["overshoot_v"]
    flt = next(o for o in rows if o["kind"] == "filter")
    assert "conducted_emission" in flt["clears"] and flt["outcome"]["conducted_db"] >= 6.0
    ranked = [len(o["clears"]) for o in rows[1:] if o["outcome"]]
    assert ranked == sorted(ranked, reverse=True)
    assert r.fixes and r.fixes[0]["kind"] == "now"                # Simulate stores them on the stage


def test_a_switch_rating_limits_the_overshoot(tmp_path):
    """With a Vds field on the switch (InvenTree's rating), the overshoot target is the smaller of the rule's
    share of V_in and 80 % of the rating less V_in."""
    from groundtruth.analysis.fix_advisor import targets
    gen = hot_loop_board(20.0, connector=True)
    gen.fields("U1", Vds="60V")
    board, sc, _ = make(tmp_path, gen, "vds", nets=())
    sc.set_converters([SPEC])
    res = simulate(board, sc.config)
    t = targets(res.converter_setups[0], board)
    assert t.vds == 60.0 and t.overshoot_v == pytest.approx(0.8 * 60 - 48.0)


def test_the_proposed_filter_is_among_the_stages_responses(base):
    """Simulate keeps the proposed π-filter's curves on the stage: its attenuation (filter_transfer, complex, at
    every frequency of the curve; falling about 40 dB a decade above its corner; fitted within 0.5 dB), the filter's
    output impedance peaking at middlebrook's figure, and the converter's input impedance −V²/P (180°); the stage's
    Bode views split the ratios from the impedances (two magnitude axes)."""
    from groundtruth.analysis.fix_advisor import filter_transfer
    from groundtruth.analysis.transfer import bode_views
    r, setup, _ = base
    flt = next(o for o in r.fixes if o["kind"] == "filter")["change"]["filter"]
    by = {x.name.split(": ", 1)[1]: x for x in r.responses}
    att = by["proposed input filter (LISN current with / without)"]
    assert att.h == pytest.approx(filter_transfer(setup, att.f, flt), rel=1e-12)
    assert att.fit_error_db is not None and att.fit_error_db < 0.5
    f_c = 1 / (2 * math.pi * math.sqrt(flt["L"] * flt["C"]))
    hi = np.abs(filter_transfer(setup, np.array([10 * f_c, 100 * f_c]), flt))
    assert 20 * math.log10(hi[1] / hi[0]) < -30
    mb = middlebrook(setup, flt)
    z_out = by["filter output impedance (Middlebrook)"]
    assert np.abs(z_out.h).max() == pytest.approx(mb["z_out_peak"], rel=0.02)
    z_in = by["converter input impedance −V²/P"]
    assert np.abs(z_in.h) == pytest.approx(mb["z_in"]) and z_in.phase_deg()[0] == pytest.approx(180.0)
    names = [v.net for v in bode_views(type("R", (), {"converters": [r]})())]
    assert names == ["hot", "hot impedances"]
