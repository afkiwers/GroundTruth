"""The plane-screening switch (2026-11-05, the user: "make it switchable, default off ... a note in the help about the two
ways ... an info icon that opens the help beside the switch"): `analysis.plane_screening` in the settings file, the
command line's --plane-screening, Preferences' "Plane screening" row, the help section both open, and the models the
setting reaches. The physics of the face sheets is tests/test_plane_screening.py's."""
from argparse import Namespace

import pytest

from boardgen import layer_change_board
from conftest import make
from groundtruth.cli import _solver_options, build_parser
from groundtruth.config.settings import ConfigError, SettingsFile, config_from_data, settings_path
from groundtruth.analysis import simulate

NET = {"name": "X", "driver": "U1.1", "receivers": ["U2.1"], "freq": "20MHz", "rise_time": "2ns"}


def _cfg(**analysis):
    """A configuration whose analysis block is `analysis` (everything else the defaults)."""
    return config_from_data({"nets": [NET], "analysis": analysis})


def test_it_is_off_unless_the_settings_say_on():
    """Off when the file does not say (the face sheets cost about 3.5x the board-wide solve time), and true / false,
    on / off and yes / no in any case, quoted or not, are read as they say: a quoted "off" is a string, and
    bool("off") would be true."""
    assert _cfg().analysis.plane_screening is False
    assert _cfg(plane_screening=None).analysis.plane_screening is False
    for v, want in ((True, True), (False, False), ("on", True), ("off", False), ("Off", False), ("YES", True),
                    ("no", False), ("true", True), ("false", False), (" on ", True)):
        assert _cfg(plane_screening=v).analysis.plane_screening is want, v


def test_a_value_that_is_not_on_or_off_is_reported():
    """Anything else is a configuration error naming the setting, not a quiet guess."""
    with pytest.raises(ConfigError) as e:
        _cfg(plane_screening="sometimes")
    assert "analysis.plane_screening must be on or off, not 'sometimes'" in " | ".join(e.value.messages)


def test_a_new_settings_file_names_it_and_it_survives_a_round_trip(tmp_path):
    """`init` writes it (false) so the file shows it is there; true is read back from the file."""
    board, sc, path = make(tmp_path, layer_change_board(0.8), "file")
    text = settings_path(path).read_text(encoding="utf-8")
    assert "plane_screening: false" in text and sc.config.analysis.plane_screening is False
    sc.doc["analysis"]["plane_screening"] = True
    sc.save()
    assert SettingsFile.load(settings_path(path)).config.analysis.plane_screening is True


def test_the_command_line_replaces_the_file_for_one_run():
    """--plane-screening on|off (every command that runs the solver) replaces analysis.plane_screening; without it
    the file's value stays."""
    p = build_parser()
    for command in ("simulate", "suggest", "compare", "verify"):
        extra = ["--config", "c.yaml", "--thresholds", "t.yaml"] if command == "verify" else []
        assert p.parse_args([command, "board.kicad_pcb", *extra, "--plane-screening", "on"]).plane_screening == "on"
        assert p.parse_args([command, "board.kicad_pcb", *extra]).plane_screening is None
    with pytest.raises(SystemExit):
        p.parse_args(["simulate", "board.kicad_pcb", "--plane-screening", "maybe"])
    on, off = _cfg(plane_screening=False), _cfg(plane_screening=True)
    _solver_options(on, Namespace(plane_screening="on"))
    _solver_options(off, Namespace(plane_screening="off"))
    assert on.analysis.plane_screening is True and off.analysis.plane_screening is False
    kept = _cfg(plane_screening=True)
    _solver_options(kept, Namespace(plane_screening=None))
    assert kept.analysis.plane_screening is True


def test_preferences_replace_the_file_per_computer():
    """Preferences' "Plane screening": From the settings file leaves the file's value (no override), off / on replace
    it; anything else in ui.json is taken as From the settings file."""
    from groundtruth.ui.prefs_dialog import FROM_FILE, PLANE_SCREENING, defaults, overrides
    assert PLANE_SCREENING == (FROM_FILE, "off", "on")
    assert defaults({})["plane_screening"] == FROM_FILE and "plane_screening" not in overrides({})
    assert defaults({"plane_screening": "maybe"})["plane_screening"] == FROM_FILE
    assert overrides({"plane_screening": "on"})["plane_screening"] is True
    assert overrides({"plane_screening": "off"})["plane_screening"] is False


def test_the_setting_reaches_the_models_the_route_and_its_ideal_reference_are_solved_on(tmp_path):
    """BoardModel sets StackBuild.screen from the setting on the route's model and on the ideal reference; a model
    nothing set keeps it off."""
    layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    bbox = (0.0, 0.0, 40.0, 30.0)
    for want in (False, True):
        board, sc, _ = make(tmp_path, layer_change_board(0.8), f"reach_{want}", plane_screening=want)
        model = simulate.BoardModel(board, sc.config)
        st = model.build(bbox, layers, {"GND", "SIG"}, 0.5, False)
        assert st.screen is want
        assert st.plane.screen is want
    board, sc, _ = make(tmp_path, layer_change_board(0.8), "reach_default")
    assert simulate.BoardModel(board, sc.config).build(bbox, layers, {"GND", "SIG"}, 0.5, False).screen is False
    assert simulate.StackBuild(layers[:1], [], {layers[0]: 0.0}, bbox, 0.5, {}).screen is False


def test_the_help_explains_the_two_ways_and_the_switch_opens_it():
    """The help has a Plane screening section (a topic of the sidebar's "Under the hood" group), its two ways to set
    it (settings file, Preferences) and the command line, the cost and when to turn it on; Preferences' row links
    it."""
    from pathlib import Path
    from groundtruth.ui import help as gt_help
    html = (Path(simulate.__file__).resolve().parents[1] / "ui" / "help.html").read_text(encoding="utf-8")
    assert "screening" in gt_help.TOPICS
    assert "screening" in dict(gt_help.GROUPS)["Under the hood"]
    assert '<a name="screening"></a>' in html
    section = html.split('<a name="screening"></a>')[1].split('<a name="server"></a>')[0]
    for needle in ("analysis: plane_screening: true", "--plane-screening on", "Preferences", "(i)", "129 s",
                   "155 s", "When to turn it on", "Leave it", "The two ways to set it"):
        assert needle in section, needle
    row = html.split("<b>Plane screening</b>")[1].split("</tr>")[0]
    assert 'href="#screening"' in row and "analysis.plane_screening" in row and "--plane-screening" in row
