"""The demo project shipped in the plugin folder (plugin/demo, made by tools/make_demo.py): it loads, its settings
are valid and list the net, and the rule checks find the missing stitching via it is there to show."""
from pathlib import Path

from groundtruth.adapters.file_source import load_board
from groundtruth.config.settings import SettingsFile
from groundtruth.rules.checks import run_checks

DEMO = Path(__file__).resolve().parents[1] / "plugin" / "demo"


def test_the_demo_board_shows_a_missing_stitching_via():
    board = load_board(DEMO / "groundtruth_demo.kicad_pcb")
    assert board.copper_layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert (DEMO / "groundtruth_demo.kicad_pro").exists() and (DEMO / "README.txt").exists()
    cfg = SettingsFile.load(DEMO / "groundtruth" / "groundtruth_demo.groundtruth.yaml").config
    assert [n.name for n in cfg.nets] == ["SIG"] and cfg.nets[0].reviewed
    assert cfg.analysis.plane_capacitance and cfg.analysis.gpu == "auto"
    issues = run_checks(board, cfg).issues
    assert "layer_change_stitch" in {i.check for i in issues}
