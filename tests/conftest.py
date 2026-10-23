import pytest

from boardgen import layer_change_board, microstrip_board
from groundtruth.adapters.file_source import load_board
from groundtruth.config.settings import NetSpec, SettingsFile, settings_path

SIG = NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9)


@pytest.fixture(autouse=True)
def _private_user_folders(tmp_path_factory, monkeypatch):
    """Windows register a board inbox and the plugin logs its start-up in the user's folder: in tests both go
    to a temporary folder, never into %LOCALAPPDATA%\\GroundTruth. The preferences (ui.json) are a private file
    too: a test never sees the user's resolution or theme, and keeps no simulation history unless it turns it on
    (a test that sets LOCALAPPDATA itself starts from the defaults, history on). Background drawing does not wait
    for the user to be idle (ui/idle.py): the person at the machine would hold the tests up."""
    from groundtruth.ui import idle, inbox, startup
    monkeypatch.setattr(idle, "QUIET_S", 0.0)
    root = tmp_path_factory.mktemp("user")
    monkeypatch.setattr(inbox, "windows_root", lambda: root / "windows")
    monkeypatch.setattr(startup, "log_path", lambda: root / "plugin.log")
    (root / "local" / "GroundTruth").mkdir(parents=True)
    (root / "local" / "GroundTruth" / "ui.json").write_text('{"history": false}', encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(root / "local"))
    # a package built in a test signs with a throwaway key made in that private folder: no trusted public half to
    # match (the repository's tools/release_key.pub is the user's; tools/make_server_package.PUB_ENV)
    monkeypatch.setenv("GROUNDTRUTH_RELEASE_PUB", str(root / "no-trusted-key.pub"))


def make(tmp_path, gen, name="b", nets=(SIG,), capacitance=False, **analysis):
    """Write a generated board + settings into tmp_path; return (board, settings, board path).

    The board-wide plane-capacitance model is off unless a test asks for it: it is covered by its own
    tests, and keeping it out of the others keeps the suite fast."""
    path = gen.write(tmp_path / f"{name}.kicad_pcb")
    board = load_board(path)
    sc = SettingsFile.new(settings_path(path), board)     # <tmp>/groundtruth/<name>.groundtruth.yaml
    sc.set_nets(list(nets))
    sc.doc["analysis"]["plane_capacitance"] = capacitance
    for k, v in analysis.items():
        sc.doc["analysis"][k] = v
    sc.save()
    sc = SettingsFile.load(settings_path(path))
    return board, sc, path


@pytest.fixture
def solid(tmp_path):
    return make(tmp_path, microstrip_board(), "solid")


@pytest.fixture
def slotted(tmp_path):
    return make(tmp_path, microstrip_board(slot=(19.5, 4, 20.5, 16)), "slot")


__all__ = ["make", "SIG", "layer_change_board", "microstrip_board"]
