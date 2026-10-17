import pytest

from boardgen import BoardGen
from groundtruth.adapters.file_source import loads_board
from groundtruth.components.models import ComponentModel
from groundtruth.config.settings import ConfigError, NetSpec, SettingsFile, config_from_data, validate


def board_with(parts):
    g = BoardGen()
    for ref, value in parts:
        g.two_pin(ref, value, (5.0 + len(g.items), 5.0), "A", "B")
    return loads_board(g.text(), source="t.kicad_pcb")


def test_new_settings_suggests_and_roundtrips(tmp_path):
    b = board_with([("R1", "0R"), ("R2", "33R"), ("C1", "100n")])
    sc = SettingsFile.new(tmp_path / "t.groundtruth.yaml", b)
    sc.save()
    again = SettingsFile.load(tmp_path / "t.groundtruth.yaml")
    comps = again.config.components
    assert comps["R1"].model.kind == "short"
    assert comps["R2"].model.params["R"] == 33
    assert comps["C1"].model.kind == "cap"
    text = (tmp_path / "t.groundtruth.yaml").read_text(encoding="utf-8")
    assert "R2: {value: 33R" in text   # one flow-style line per part


def test_merge_keeps_user_edits_and_refreshes_untouched(tmp_path):
    b1 = board_with([("R1", "33R"), ("R2", "33R"), ("R3", "1k")])
    sc = SettingsFile.new(tmp_path / "t.groundtruth.yaml", b1)
    sc.set_component("R1", ComponentModel("short", note="mine"))   # user edit
    sc.doc.yaml_set_comment_before_after_key("components", before="my comment")
    sc.save()

    b2 = board_with([("R1", "47R"), ("R2", "47R"), ("R4", "0R")])   # values changed, R3 removed, R4 new
    sc2 = SettingsFile.load(tmp_path / "t.groundtruth.yaml")
    changes = sc2.merge_board(b2)
    comps = sc2.config.components
    assert comps["R1"].model.kind == "short"                # user edit kept
    assert "value changed" in comps["R1"].model.note
    assert comps["R2"].model.params["R"] == 47              # untouched suggestion refreshed
    assert comps["R3"].removed
    assert comps["R4"].model.kind == "short"
    assert changes["added"] == ["R4"] and changes["removed"] == ["R3"]
    sc2.save()
    assert "my comment" in (tmp_path / "t.groundtruth.yaml").read_text(encoding="utf-8")


def test_config_errors_are_collected():
    with pytest.raises(ConfigError) as e:
        config_from_data({"nets": [{"name": "X", "driver": "U1.1", "receivers": ["U2.1"], "freq": "fast",
                                    "rise_time": "1ns"}],
                          "components": {"R1": {"model": "banana"}},
                          "rules": {"severity": {"gap_crossing": "loud"}}})
    msgs = " | ".join(e.value.messages)
    assert "freq" in msgs and "banana" in msgs and "loud" in msgs


def test_units_in_net_spec():
    cfg = config_from_data({"nets": [{"name": "X", "driver": "U1.1", "receivers": "U2.1", "freq": "20MHz",
                                      "rise_time": "2ns"}]})
    n = cfg.nets[0]
    assert n.freq == pytest.approx(20e6) and n.rise_time == pytest.approx(2e-9) and n.receivers == ["U2.1"]


def test_validate_against_board(tmp_path):
    b = board_with([("R1", "33R")])
    sc = SettingsFile.new(tmp_path / "t.groundtruth.yaml", b)
    sc.set_nets([NetSpec("NOPE", "R1.1", ["R9.1"], 1e6, 1e-9)])
    errors = validate(sc.config, b)
    assert any("NOPE" in e for e in errors) and any("R9.1" in e for e in errors)


def test_snapshot_writes_the_settings_as_they_were(tmp_path):
    """The plugin saves in the background: the snapshot is a copy, so edits made after it (while the copy is
    being written) neither change nor break that write; the write is atomic (no temporary file left)."""
    b = board_with([("R1", "33R")])
    sc = SettingsFile.new(tmp_path / "t.groundtruth.yaml", b)
    sc.set_nets([NetSpec(name="A", driver="R1.1", receivers=["R1.2"], freq=10e6, rise_time=2e-9)])
    write = sc.snapshot()
    sc.set_nets([])                                     # edited after the snapshot
    write()
    assert [n.name for n in SettingsFile.load(tmp_path / "t.groundtruth.yaml").config.nets] == ["A"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t.groundtruth.yaml"]
    sc.save()
    assert SettingsFile.load(tmp_path / "t.groundtruth.yaml").config.nets == []


def test_a_missing_unit_prefix_is_caught_before_it_ruins_the_estimate():
    """refboard's settings had 'c_oss: 482F' (482 typed for 482 pF): the hot loop rang at 0 Hz and every
    margin was meaningless (+134 dB conducted, +238 dB radiated). Values outside the usual range are named, each
    with what to write if a unit is missing (an info issue: they are used as written); real values pass."""
    from groundtruth.config.settings import ConverterSpec, implausible
    bad = ConverterSpec("SW1", "VIN", "GND", "SW", ["Q1"], ["Q2"], c_oss=482.0, rise_time=2.0, f_sw=750.0,
                        c_stray=20.0)
    msgs = implausible(bad)
    assert len(msgs) == 4 and any("c_oss" in m and "482p" in m for m in msgs)
    assert any("rise_time" in m and "2n" in m for m in msgs) and any("f_sw" in m and "750k" in m for m in msgs)
    good = ConverterSpec("SW1", "VIN", "GND", "SW", ["Q1"], ["Q2"], c_oss=616e-12, c_stray=20e-12, rise_time=2e-9,
                         f_sw=787e3)
    assert implausible(good) == []
    from dataclasses import replace
    assert any("q_oss" in m and "38n" in m for m in implausible(replace(good, q_oss=38.0)))
    assert implausible(replace(good, q_oss=38e-9)) == []


def test_q_oss_is_read_and_written_with_its_unit(tmp_path):
    """The EPC2306's Q_OSS (38 nC at 50 V) as 'q_oss: 38nC' or '38n'; it is written back with its unit."""
    from groundtruth.config.settings import SettingsFile
    p = tmp_path / "b.groundtruth.yaml"
    p.write_text("version: 1\nconverters:\n  - {name: SW1, rail: VIN, ground: GND, switch_node: SW, high_side: Q1, "
                 "low_side: Q2, q_oss: 38nC, c_oss: 482p}\n", encoding="utf-8")
    sf = SettingsFile.load(p)
    c = sf.config.converters[0]
    assert c.q_oss == pytest.approx(38e-9) and c.c_oss == pytest.approx(482e-12)
    sf.set_converters([c])
    sf.save()
    assert "q_oss: 38nC" in p.read_text(encoding="utf-8")
    assert SettingsFile.load(p).config.converters[0].q_oss == pytest.approx(38e-9)


def test_an_invalid_list_is_refused_and_leaves_the_settings_as_they_were(tmp_path):
    """set_nets / set_converters / set_bridges / set_modifications / set_component parse the new document before
    changing it: an invalid entry raises ConfigError and nothing changes. (Writing first left the entry in the
    document, so every later change failed on it: an empty Converters form's Add, then the Parts tab's Apply.)"""
    from groundtruth.config.settings import ConverterSpec
    b = board_with([("R1", "33R")])
    sc = SettingsFile.new(tmp_path / "t.groundtruth.yaml", b)
    net = NetSpec("A", "R1.1", ["R1.2"], 20e6, 1e-9)
    sc.set_nets([net])
    before = sc.dumps()
    with pytest.raises(ConfigError, match="rail and ground"):
        sc.set_converters([ConverterSpec(name="x", rail="", ground="")])
    with pytest.raises(ConfigError):
        sc.set_nets([net, NetSpec("B", "R1.1", [], 20e6, 1e-9)])               # no receiver
    assert sc.dumps() == before
    assert [n.name for n in sc.config.nets] == ["A"] and not sc.config.converters
    sc.set_component("R1", ComponentModel("resistor", {"R": 47.0}))        # a valid change still goes in
    assert sc.config.components["R1"].model.params["R"] == 47.0
