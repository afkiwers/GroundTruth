"""Saved configurations (config/scenario.py) and the settings' undo steps (ui/undo.py): what a simulation runs is saved
apart from what the board's simulations share, loaded back exactly, and every change can be taken back."""
from __future__ import annotations

import copy

import pytest

from boardgen import layer_change_board
from conftest import SIG, make
from groundtruth.config import scenario
from groundtruth.config.settings import ConfigError, NetSpec, SettingsFile


@pytest.fixture
def board_settings(tmp_path):
    """A generated board with its settings (one net SIG)."""
    return make(tmp_path, layer_change_board(None), "cfg")


def test_a_configuration_round_trip_restores_what_is_simulated_and_leaves_the_board_shared(board_settings, tmp_path):
    """extract -> save -> load -> apply: the nets (with their ticks), the run settings and the what-if edits come
    back exactly; the part models, waivers and rules are the settings' own (shared), not the configuration's."""
    board, sc, _ = board_settings
    sc.doc["analysis"]["solve_points"] = 7
    saved = scenario.extract(sc.doc, "two nets", note="SIG only", board="cfg.kicad_pcb")
    path = scenario.save(scenario.folder_for(sc.path) / scenario.file_name("two nets"), saved)
    assert path.name == "two_nets.gtconfig.yaml" and path.parent.name == "configs"
    assert set(saved) >= {"groundtruth_configuration", "name", "parts", "nets", "analysis"}
    assert "components" not in saved and "waivers" not in saved and "rules" not in saved
    # later: the nets change, a waiver is added, a rule changes, solve points too
    sc.set_nets([NetSpec("OTHER", "U1.1", ["U2.1"], 1e6, 1e-8, enabled=False), SIG])
    sc.add_waiver("abc", "accepted")
    sc.doc["rules"]["stitch_distance_mm"] = 3.5
    sc.doc["analysis"]["solve_points"] = 2
    sc.config = SettingsFile(sc.path, sc.doc).config
    notes = scenario.apply(sc, scenario.load(path), board, active={"name": "two nets", "file": str(path)})
    assert [n.name for n in sc.config.nets] == ["SIG"] and sc.config.nets[0].enabled
    assert sc.config.analysis.solve_points == 7
    assert [w.fingerprint for w in sc.config.waivers] == ["abc"]                # shared: kept
    assert sc.doc["rules"]["stitch_distance_mm"] == 3.5
    assert notes == [] and scenario.active_of(sc.doc) == {"name": "two nets", "file": str(path)}
    assert not scenario.differs(sc.doc, scenario.load(path))


def test_a_section_the_configuration_lacks_is_cleared_and_an_invalid_one_changes_nothing(board_settings):
    """A configuration defines what is simulated: a section it does not have is removed (none / defaults). One that
    does not parse raises ConfigError before the settings change."""
    board, sc, _ = board_settings
    scen = scenario.extract(sc.doc, "x")
    del scen["nets"]
    scenario.apply(sc, scen, board)
    assert sc.config.nets == [] and "nets" not in sc.doc
    before = copy.deepcopy(sc.doc)
    bad = scenario.extract(sc.doc, "bad")
    bad["analysis"]["margin_mm"] = "lots"
    with pytest.raises(ConfigError):
        scenario.apply(sc, bad, board)
    assert sc.doc == before


def test_parts_changed_or_gone_since_saving_are_named(board_settings):
    """The parts stay shared: a configuration loaded later uses today's part models and names those that changed
    since it was saved, and those the settings no longer have."""
    board, sc, _ = board_settings
    scen = scenario.extract(sc.doc, "x")
    refs = sorted(sc.doc["components"])
    assert len(refs) >= 2 and set(scen["parts"]) == set(refs)
    sc.doc["components"][refs[0]]["value"] = "47nF"
    del sc.doc["components"][refs[1]]
    changed, gone = scenario.changed_parts(scen["parts"], sc.doc)
    assert changed == [refs[0]] and gone == [refs[1]]
    notes = scenario.notes(scen, sc.doc, sc.config)
    assert any(refs[0] in n and "changed" in n for n in notes) and any(refs[1] in n for n in notes)


def test_a_configuration_naming_what_the_board_lacks_says_so(board_settings):
    """Loading a configuration with a net the board does not have notes it (the board check of Run checks)."""
    board, sc, _ = board_settings
    sc.set_nets([NetSpec("NOT_ON_BOARD", "U9.1", ["U8.1"], 1e6, 1e-8)])
    scen = scenario.extract(sc.doc, "elsewhere")
    sc.set_nets([SIG])
    notes = scenario.apply(sc, scen, board)
    assert any("NOT_ON_BOARD" in n or "U9" in n for n in notes), notes


def test_a_past_runs_settings_and_foreign_files(board_settings, tmp_path):
    """from_settings_text: a history run's whole settings file gives its configuration. A file that is not a
    configuration, or not YAML, is refused with the reason."""
    board, sc, _ = board_settings
    scen = scenario.from_settings_text(sc.dumps(), "run 2026-10-23 #1", board="cfg.kicad_pcb")
    assert scen["name"] == "run 2026-10-23 #1" and [n["name"] for n in scen["nets"]] == ["SIG"]
    other = tmp_path / "x.yaml"
    other.write_text("nets: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not a GroundTruth configuration"):
        scenario.load(other)
    other.write_text("a: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        scenario.load(other)


def test_restore_takes_a_kept_state_back_without_touching_it(board_settings):
    """SettingsFile.snapshot_doc hands out the copy a save writes (the undo step); restore takes a kept copy back as
    the settings (parsed), leaving the kept one as it was."""
    _, sc, _ = board_settings
    kept, write = sc.snapshot_doc()
    sc.set_nets([])
    assert sc.config.nets == []
    sc.restore(kept)
    assert [n.name for n in sc.config.nets] == ["SIG"]
    sc.set_nets([])
    assert [n["name"] for n in kept["nets"]] == ["SIG"]       # the kept state is untouched
    write()
    assert [n.name for n in SettingsFile.load(sc.path).config.nets] == ["SIG"]


def test_undo_and_redo_walk_the_states_in_order():
    """record / undo / redo: undo hands back the state before with the label of the change undone, redo the one
    undone; a new change drops the redo steps; at most `limit` steps are kept."""
    from groundtruth.ui.undo import UndoStack
    u = UndoStack(limit=3)
    u.reset("s0")
    assert not u.can_undo() and not u.can_redo()
    for i in (1, 2, 3, 4):
        u.record(f"s{i}", f"edit {i}")
    assert u.undo_label() == "edit 4"
    assert u.undo() == ("s3", "edit 4") and u.undo() == ("s2", "edit 3") and u.undo() == ("s1", "edit 2")
    assert not u.can_undo()                                       # s0 fell off the limit
    assert u.redo() == ("s2", "edit 2") and u.current == "s2" and u.redo_label() == "edit 3"
    u.record("s5", "edit 5")
    assert not u.can_redo() and u.undo() == ("s2", "edit 5")
    u.rebase("s2 read again")
    assert u.current == "s2 read again" and u.can_undo()
