"""Part parameters from schematic fields (components/fields.py): an InvenTree part's parameters reach the symbol as
fields ("Rds": "3.8milliohm"); GroundTruth reads one concrete field name per parameter, keeps the values linked to
the schematic (from_schematic, refreshed on every open and run) and flags a value set here that differs."""
from dataclasses import replace

import pytest

from boardgen import bridge_board, discrete_buck_board
from conftest import make
from groundtruth.analysis.detect import detect_bridges, detect_converters
from groundtruth.components.defaults import suggest_model
from groundtruth.components.fields import (PARAMS, apply_fields, differences, link_form, read, stage_marks,
                                           stage_values, use_schematic)
from groundtruth.components.models import ComponentModel
from groundtruth.components.values import parse_quantity
from groundtruth.config.settings import SettingsFile


@pytest.mark.parametrize("text, quantity, assumed, value", [
    ("3.8milliohm", "ohm", 1e-3, 3.8e-3), ("3.8 milliohm", "ohm", 1e-3, 3.8e-3), ("6.9 mΩ", "ohm", 1e-3, 6.9e-3),
    ("4.7mOhm", "ohm", 1, 4.7e-3), ("3.8m", "ohm", 1e-3, 3.8e-3), ("3.8", "ohm", 1e-3, 3.8e-3), ("4m7", "ohm", 1, 4.7e-3),
    ("4R7", "ohm", 1, 4.7), ("1.5MOhm", "ohm", 1, 1.5e6), ("616 picofarad", "F", 1e-12, 616e-12), ("616p", "F", 1e-12, 616e-12),
    ("616", "F", 1e-12, 616e-12), ("10uF/16V", "F", 1, 10e-6), ("2.2 µF", "F", 1, 2.2e-6), ("38 nanocoulomb", "C", 1e-9, 38e-9),
    ("38nC", "C", 1e-9, 38e-9), ("2.1V", "V", 1, 2.1), ("0.4 nH", "H", 1e-9, 0.4e-9), ("1e-9", "F", 1e-12, 1e-9)])
def test_values_as_inventree_and_datasheets_write_them(text, quantity, assumed, value):
    v, _ = parse_quantity(text, quantity, assumed)
    assert v == pytest.approx(value, rel=1e-9)


@pytest.mark.parametrize("text, quantity, why", [("100V", "ohm", "not a resistance"), ("21.2 A", "F", "not a capacitance"),
                                                  ("3.8 mOhmz", "ohm", "unit not understood"), ("abc", "V", "not a number")])
def test_a_value_of_the_wrong_kind_is_refused(text, quantity, why):
    v, reason = parse_quantity(text, quantity)
    assert v is None and why in reason


def buck(**fields):
    """The discrete buck (Q1 high side, Q2 low side, L1, C1) with symbol fields on its parts."""
    g = discrete_buck_board()
    for ref, values in fields.items():
        g.fields(ref, **values)
    return g


FIELDS = {"Q1": {"Rds(on)": "3.8milliohm"}, "Q2": {"Rds(on)": "5m", "Coss": "616 picofarad", "Qoss": "38 nanocoulomb"},
          "L1": {"DCR": "6.9"}, "C1": {"ESR": "5 milliohm", "Cbias": "4.2uF"}}


def test_a_part_reads_its_fields_with_the_assumed_unit_even_outside_the_usual_range(tmp_path):
    g = buck(Q1={"Rds(on)": "3.8"}, Q2={"rds(ON)": "100V", "Coss": "482"}, L1={"DCR": "20000"})
    board, _, _ = make(tmp_path, g, "read", nets=())
    r = read(board.footprint("Q1"), "Rds(on)")
    assert r.value == pytest.approx(3.8e-3) and r.note == "mΩ assumed" and r.source == "Q1 Rds(on) 3.8"
    assert read(board.footprint("Q2"), "Rds(on)").value is None                     # matched without case; 100 V refused
    assert read(board.footprint("Q2"), "Coss").value == pytest.approx(482e-12)
    dcr = read(board.footprint("L1"), "DCR")                # 20 Ω: unusual, but the user's value
    assert dcr.value == pytest.approx(20.0) and "used as written" in dcr.note
    assert read(board.footprint("C1"), "ESR") is None                               # no field


def test_an_unusual_value_never_blocks_simulate_and_is_listed_as_info(tmp_path):
    """refboard's L503 (33 µH) has DCR 1330 mΩ = 1.33 Ω, above the usual range of a power inductor's DCR:
    it is the user's value, so it is used (no settings error that blocks Simulate) and listed as an info issue
    that says what to write if a unit is missing."""
    from groundtruth.config.settings import validate
    from groundtruth.rules.checks import run_checks
    board, sc, _ = make(tmp_path, buck(L1={"DCR": "1330"}), "dcr", nets=())
    d, = detect_converters(board, sc.config)
    assert d.spec.dcr == pytest.approx(1.33)
    sc.add_detected_converters([d.spec], d.models)
    assert validate(sc.config, board) == []
    issues = [i for i in run_checks(board, sc.config).issues if i.check == "unusual_value"]
    assert len(issues) == 1 and issues[0].severity == "info"
    assert "dcr 1.33Ω is unusual" in issues[0].message and "used as written" in issues[0].message


def test_detection_takes_a_stages_parameters_from_its_parts(tmp_path):
    board, sc, _ = make(tmp_path, buck(**FIELDS), "stage", nets=())
    d, = detect_converters(board, sc.config)
    s = d.spec
    assert (s.rds_on, s.rds_on_low, s.c_oss, s.q_oss, s.dcr) == pytest.approx((3.8e-3, 5e-3, 616e-12, 38e-9, 6.9e-3))
    assert set(s.from_schematic) == {"rds_on", "rds_on_low", "c_oss", "q_oss", "dcr"}
    assert any("from the schematic fields" in r for r in d.reasons)


def test_a_bridge_counts_a_sides_parallel_fets_and_skips_parts_that_disagree(tmp_path):
    g = bridge_board()
    for ref in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6"):
        g.fields(ref, **{"Rds(on)": "1.2m", "Coss": "1nF", "Vsd": "1.9V"})
    board, sc, _ = make(tmp_path, g, "br", nets=())
    d, = detect_bridges(board, sc.config)
    assert (d.spec.rds_on, d.spec.c_oss, d.spec.v_sd) == pytest.approx((1.2e-3, 1e-9 * len(d.spec.legs[0].low_side), 1.9))
    assert set(d.spec.from_schematic) == {"rds_on", "rds_on_low", "c_oss", "v_sd"}
    two = bridge_board()
    for ref, rds in (("Q1", "1.2m"), ("Q3", "2m"), ("Q5", "1.2m")):
        two.fields(ref, **{"Rds(on)": rds})
    board2, sc2, _ = make(tmp_path, two, "br3", nets=())
    values, notes = stage_values(board2, sc2.config, detect_bridges(board2, sc2.config)[0].spec)
    assert "rds_on" not in values and any("differs between" in n for n in notes)


def test_linked_values_follow_the_schematic_and_overrides_are_kept_and_flagged(tmp_path):
    board, sc, path = make(tmp_path, buck(**FIELDS), "sync", nets=())
    d, = detect_converters(board, sc.config)
    sc.add_detected_converters([d.spec])
    sc.save()
    sc = SettingsFile.load(sc.path)
    c = sc.config.converters[0]
    assert set(c.from_schematic) == {"rds_on", "rds_on_low", "c_oss", "q_oss", "dcr"}     # round trip
    # the user sets C_oss here: kept, and flagged against the schematic
    c2 = replace(c, c_oss=482e-12, q_oss=None, from_schematic=[])
    link_form(board, sc.config, c2, c)
    assert "c_oss" not in c2.from_schematic and "dcr" in c2.from_schematic
    assert c2.q_oss == pytest.approx(38e-9) and "q_oss" in c2.from_schematic      # emptied: the schematic's again
    sc.set_converters([c2])
    # InvenTree changes L1's DCR and Q2's Coss: the linked DCR follows, the user's C_oss stays
    board2, _, _ = make(tmp_path, buck(**{**FIELDS, "L1": {"DCR": "8.1m"}, "Q2": {**FIELDS["Q2"], "Coss": "700p"}}),
                        "sync2", nets=())
    changes = sc.merge_board(board2)
    c3 = sc.config.converters[0]
    assert c3.dcr == pytest.approx(8.1e-3) and c3.c_oss == pytest.approx(482e-12)
    assert c3.name in changes["schematic"]
    diff, = differences(sc.config, board2)
    assert (diff.key, diff.ours, diff.schematic.value) == ("c_oss", pytest.approx(482e-12), pytest.approx(700e-12))
    assert stage_marks(board2, sc.config, c3)["c_oss"][0] == "differs"
    assert stage_marks(board2, sc.config, c3)["dcr"][0] == "schematic"
    from groundtruth.rules.checks import run_checks
    issues = [i for i in run_checks(board2, sc.config).issues if i.check == "schematic_differs"]
    assert len(issues) == 1 and issues[0].severity == "info" and "700p" in issues[0].message
    # Use schematic values takes it back
    c4 = replace(c3, from_schematic=list(c3.from_schematic))
    assert "c_oss" in use_schematic(board2, sc.config, c4)
    assert c4.c_oss == pytest.approx(700e-12) and "c_oss" in c4.from_schematic
    # a field that disappears leaves the value to the user (empty, so it is noticed)
    board3, _, _ = make(tmp_path, buck(**{**FIELDS, "L1": {}}), "sync3", nets=())
    sc.set_converters([c4])
    apply_fields(sc.config, board3)
    assert sc.config.converters[0].dcr is None and "dcr" not in sc.config.converters[0].from_schematic


def test_part_models_take_their_fields_and_keep_the_users_values(tmp_path):
    board, sc, _ = make(tmp_path, buck(**FIELDS), "parts", nets=())
    m = suggest_model(board.footprint("C1"))
    assert m.params["ESR"] == pytest.approx(5e-3) and m.params["C_bias"] == pytest.approx(4.2e-6)
    assert m.from_schematic == ["ESR", "C_bias"]
    assert ComponentModel.from_dict(m.to_dict()).from_schematic == ["ESR", "C_bias"]
    assert sc.config.model_for("C1").params["ESR"] == pytest.approx(5e-3)      # a new settings file has it
    assert suggest_model(board.footprint("L1")).params["R"] == pytest.approx(6.9e-3)   # the inductor's DCR
    # settings made before the fields: the default ESR (20 mΩ) gives way to the field; the user's ESL stays
    plain, sc0, _ = make(tmp_path, discrete_buck_board(), "plain", nets=())
    model = sc0.config.model_for("C1")
    sc0.set_component("C1", ComponentModel("cap", {**model.params, "ESL": 2e-9}))
    board2, _, _ = make(tmp_path, buck(C1={"ESR": "5m", "ESL": "0.9nH"}), "parts2", nets=())
    sc0.merge_board(board2)
    m2 = sc0.config.model_for("C1")
    assert m2.params["ESR"] == pytest.approx(5e-3) and m2.params["ESL"] == pytest.approx(2e-9)
    assert [(d.name, d.key) for d in differences(sc0.config, board2)] == [("C1", "ESL")]


def test_a_stage_simulates_the_same_from_fields_as_typed_in(tmp_path):
    board, sc, _ = make(tmp_path, buck(**FIELDS), "simf", nets=())
    d, = detect_converters(board, sc.config)
    spec = replace(d.spec, f_sw=300e3, i_out=3.0, v_in=12.0, v_out=3.0)
    typed = replace(spec, from_schematic=[])
    from groundtruth.analysis.simulate import simulate
    out = []
    for s in (spec, typed):
        sc.set_converters([s])
        out.append(simulate(board, sc.config).converters[0])
    assert out[0].ringing["f"] == pytest.approx(out[1].ringing["f"], rel=1e-9)
    assert out[0].worst["e_dbuv"] == pytest.approx(out[1].worst["e_dbuv"], abs=1e-9)
    assert any(n.startswith("from the schematic fields: ") and "c_oss 616p" in n for n in out[0].notes)


def test_the_help_lists_every_field_the_plugin_reads():
    from groundtruth.ui.help import page
    text = page()
    assert "@PART_FIELDS@" not in text
    for p in PARAMS:
        assert f"<b>{p.field}</b>" in text
