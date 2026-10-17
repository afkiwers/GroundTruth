"""Rows switched off (enabled: false; the tick box on the Nets, Converters and Bridges tabs) keep all their values but
are left out of Run checks and Simulate, unless named (CLI --nets). The user: "if one wants to not simulate a
particular net, converter 'row' one has to delete it which then looses all the data entered"."""
from dataclasses import replace

from boardgen import microstrip_board
from conftest import SIG, make
from groundtruth.analysis.simulate import simulate
from groundtruth.cli import main
from groundtruth.config.settings import NetSpec, SettingsFile, validate
from groundtruth.rules.checks import run_checks


def slotted(tmp_path, *nets):
    return make(tmp_path, microstrip_board(slot=(19.5, 4, 20.5, 16)), "slot", nets=nets or (SIG,))


def test_a_switched_off_row_keeps_its_values_and_is_written_only_when_off(tmp_path):
    _, sc, path = slotted(tmp_path, replace(SIG, enabled=False, signal_class="clock", amplitude_v=1.8))
    text = sc.path.read_text(encoding="utf-8")
    assert "enabled: false" in text and "amplitude_v: 1.8" in text
    again = SettingsFile.load(sc.path).config
    n, = again.nets
    assert not n.enabled and n.amplitude_v == 1.8 and n.signal_class == "clock"
    sc.set_nets([replace(n, enabled=True)])
    line, = [ln for ln in sc.dumps().splitlines() if "name: SIG" in ln]
    assert "enabled" not in line                              # on is the default: nothing written


def test_active_leaves_out_what_is_off_unless_named():
    from groundtruth.config.settings import Config
    a, b = NetSpec("A", "U1.1", ["U2.1"], 1e6, 1e-9, enabled=False), NetSpec("B", "U1.1", ["U2.1"], 1e6, 1e-9)
    cfg = Config(nets=[a, b])
    assert [n.name for n in cfg.active().nets] == ["B"]
    assert [n.name for n in cfg.active(["A"]).nets] == ["A", "B"]
    on = Config(nets=[b])
    assert on.active() is on                                 # nothing off: the same settings


def test_checks_and_simulate_skip_a_switched_off_net_and_validate_ignores_it(tmp_path):
    board, sc, _ = slotted(tmp_path)
    assert "gap_crossing" in {i.check for i in run_checks(board, sc.config).issues}
    cfg = replace(sc.config, nets=[replace(SIG, enabled=False)])
    assert run_checks(board, cfg).issues == []
    assert "gap_crossing" in {i.check for i in run_checks(board, cfg, nets=["SIG"]).issues}   # named: checked
    assert simulate(board, cfg).routes == []
    broken = replace(cfg, nets=[replace(SIG, driver="U9.1", enabled=False)])  # a part no longer on the board
    assert validate(broken, board) == []
    assert validate(replace(broken, nets=[replace(SIG, driver="U9.1")]), board)


def test_the_cli_says_when_everything_is_off_and_runs_a_named_one(tmp_path, capsys):
    _, sc, path = slotted(tmp_path, replace(SIG, enabled=False))
    assert main(["check", str(path), "-q"]) == 0    # the board-wide checks only, with a note
    err = capsys.readouterr().err
    assert "switched off" in err and "only the board-wide checks ran" in err
    assert main(["check", str(path), "--nets", "SIG"]) == 1   # named: checked (the gap crossing fails it)
    assert "run because named: SIG" in capsys.readouterr().err
