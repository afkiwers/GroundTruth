"""What-if edits and fix suggestions."""
import math

import pytest

from boardgen import layer_change_board, microstrip_board
from conftest import make
from groundtruth.analysis.suggest import (PlacementChecker, gap_midpoints, harmonic_verdict, suggest_fixes,
                                     via_candidates)
from groundtruth.analysis.whatif import apply_modifications
from groundtruth.cli import main
from groundtruth.config.modifications import Modification
from groundtruth.config.settings import SettingsFile
from groundtruth.geometry.copper import CopperMap
from groundtruth.rules.checks import run_checks


def checks_of(board, cfg):
    return {i.check for i in run_checks(board, cfg).issues}


def test_modifications_roundtrip(tmp_path):
    board, sc, _ = make(tmp_path, layer_change_board(None), "rt")
    mods = [Modification("via", (20.0, 11.0), net="GND"),
            Modification("cap", (25.0, 13.0), nets=("GND", "GND"), layer="F.Cu", C=10e-9),
            Modification("fill", layer="In1.Cu", net="GND", polygon=[(1, 1), (2, 1), (2, 2)], enabled=False)]
    assert sc.add_modifications(mods) == 3
    assert sc.add_modifications(mods[:1]) == 0          # no duplicates
    sc.save()
    again = SettingsFile.load(tmp_path / "groundtruth" / "rt.groundtruth.yaml").config.modifications
    assert [m.type for m in again] == ["via", "cap", "fill"]
    assert again[1].C == pytest.approx(10e-9) and not again[2].enabled
    assert "freq" not in (tmp_path / "groundtruth" / "rt.groundtruth.yaml").read_text(encoding="utf-8").split("modifications")[1]


def test_whatif_via_fixes_missing_stitch(tmp_path):
    board, sc, _ = make(tmp_path, layer_change_board(None), "via")
    assert "layer_change_stitch" in checks_of(board, sc.config)
    b2, c2 = apply_modifications(board, sc.config, [Modification("via", (20.0, 11.2), net="GND")])
    assert len(b2.vias) == len(board.vias) + 1 and len(board.vias) == len(board.vias)   # original untouched
    assert "layer_change_stitch" not in checks_of(b2, c2)


def test_whatif_cap_fixes_missing_decap(tmp_path):
    board, sc, _ = make(tmp_path, layer_change_board(None, "+3V3"), "cap")
    assert "layer_change_decap" in checks_of(board, sc.config)
    b2, c2 = apply_modifications(board, sc.config, [Modification("cap", (21.5, 12.5), nets=("GND", "+3V3"),
                                                                 layer="F.Cu")])
    assert c2.model_for("WHATIF1").kind == "cap" and sc.config.model_for("WHATIF1") is None
    assert "layer_change_decap" not in checks_of(b2, c2)


def test_placement_rules(tmp_path):
    board, sc, _ = make(tmp_path, layer_change_board(None), "pl")
    checker = PlacementChecker(board, CopperMap(board))
    assert checker.blocked((20.0, 10.0), "GND", 0.3, board.copper_layers)          # on the signal via
    assert checker.blocked((0.2, 10.0), "GND", 0.3, board.copper_layers)           # board edge
    assert checker.blocked((20.0, 11.2), "GND", 0.3, board.copper_layers) is None
    vias = via_candidates(checker, board, (20.0, 10.0), "GND", ["In1.Cu", "In2.Cu"])
    assert vias and all(0.75 <= math.dist(v.at, (20.0, 10.0)) <= 1.35 for v in vias)   # pads 0.2 mm apart at 0.8
    assert all(math.dist(a.at, b.at) >= 0.8 for a in vias for b in vias if a is not b)


def test_suggest_stitching_via_and_verify(tmp_path):
    board, sc, _ = make(tmp_path, layer_change_board(None), "sug", solve_points=1)
    res = suggest_fixes(board, sc.config)
    sel = [s for s in res.selected if s.issue and s.issue.check == "layer_change_stitch"]
    assert sel, [(s.id, s.kind, s.issue.check if s.issue else None, s.note) for s in res.suggestions]
    s = sel[0]
    assert s.kind == "via" and s.modification.net == "GND"
    assert math.dist(s.location, (20.0, 10.0)) <= 1.35
    assert s.delta_l is not None and s.delta_l < 0                 # smaller loop
    assert "layer_change_stitch" in {i.check for i in res.fixed}
    assert not [i for i in res.new if i.severity == "error"]


def test_suggest_with_plane_capacitance_predicts_at_the_worst_harmonic(tmp_path):
    """With the board-wide model on, a candidate is judged at the worst harmonic through both models, and the
    prediction matches the verified change of that harmonic's margin."""
    board, sc, _ = make(tmp_path, layer_change_board(None), "capsug", capacitance=True, solve_points=3,
                        max_solves=6, cavity_max_nodes=12000)
    res = suggest_fixes(board, sc.config)
    via = next(s for s in res.selected if s.kind == "via")
    before = res.before.routes[0]
    assert before.cavity_solves and via.f_eval == before.worst["f"] and via.f_eval > before.solves[0]["f"]
    assert via.delta_db < 0

    def margin_at(rr, f):
        return next(e["margin_db"] for e in rr.emissions if e["f"] == f)

    after = res.after.routes[0]
    gain = margin_at(after, via.f_eval) - margin_at(before, via.f_eval)   # margin gain there = moment drop
    # the verification rebuilds both models with the via as board copper (measured: -21.6 dB predicted, +19.0 dB)
    assert gain > 0 and abs(gain + via.delta_db) < max(2.0, 0.2 * abs(via.delta_db)), (gain, via.delta_db)
    # The check at every harmonic near the limit (and at the midpoints of the gaps next to them, solved on the
    # baseline first) predicts the worst margin over the whole spectrum with the via, wherever it moves (measured:
    # +17.1 dB at 1450 MHz → +28.2 dB predicted at 350 MHz, +27.1 dB verified at 550)
    w = via.worst["SIG"]
    assert w["before_db"] <= before.worst["margin_db"] + 1e-6        # more samples can only find a lower minimum
    assert abs(w["after_db"] - after.worst["margin_db"]) < 2.0, (w, after.worst)
    assert via.note.startswith(f"worst margin {w['before_db']:+.1f} → {w['after_db']:+.1f} dB")
    solved = sorted(res.before.setups[0].spectrum_run.ok())
    mids = gap_midpoints(res.before.setups[0], sc.config)
    assert mids and all(any(a < f < b for a, b in zip(solved, solved[1:])) for f in mids), (mids, solved)


def test_compare_aligns_differently_sampled_runs(tmp_path):
    """The same board simulated with 4 and with 10 solves per route: each run's own worst margin differs (measured
    +21.0 vs +17.1 dB, the small budget never solved 1450 MHz), but compared over the same harmonics the two
    agree, as they must for the same physics. (On the real board, sampling turned SPI_MOSI's −1.9 dB into −4.0.)"""
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.suggest import compare_results
    runs = []
    for k in (4, 10):
        board, sc, _ = make(tmp_path, layer_change_board(None), f"s{k}", capacitance=True, solve_points=3,
                            max_solves=k, cavity_max_nodes=12000)
        runs.append(simulate(board, sc.config))
    row = next(r for r in compare_results(*runs) if r.get("kind") != "converter")
    sampled, aligned = row["worst_margin_sampled_db"], row["worst_margin_db"]
    assert row["aligned"] and abs(sampled[0] - sampled[1]) > 2.0, sampled
    assert abs(aligned[0] - aligned[1]) < 0.1, (sampled, aligned)
    assert row["worst_f"][0] == row["worst_f"][1]
    # the stored results are unchanged
    assert runs[0].routes[0].worst["margin_db"] == pytest.approx(sampled[0])


def test_fix_dropped_when_it_moves_a_resonance_onto_a_harmonic():
    """The verdict from the predicted worst margins (dB) of the fix's own route and the other routes. Numbers from
    the real board in session 2: the SPI_MOSI via raised MOSI's worst margin +3.6 → +11.4 dB and moved a plane
    resonance onto SPI_SCK's 660 MHz (+10.8 → +9.2 dB): a trade-off worth keeping, noted."""
    def r(before, after, f=660e6):
        return {"before_db": before, "after_db": after, "f": f}
    keep, note = harmonic_verdict(r(3.6, 11.4, 740e6), {"SCK": r(10.8, 9.2)}, -2.3, 6.0)
    assert keep and note == "worst margin +3.6 → +11.4 dB; also lowers SCK's worst margin +10.8 → +9.2 dB (at 660 MHz)"
    # ... but not when SCK ends below the warning level, or loses more than MOSI gains
    assert not harmonic_verdict(r(3.6, 11.4), {"SCK": r(7.0, 5.5)}, -2.3, 6.0)[0]
    keep, note = harmonic_verdict(r(3.6, 5.0), {"SCK": r(20.0, 17.0)}, -2.3, 6.0)
    assert not keep and note.startswith("not selected: lowers SCK's worst margin +20.0 → +17.0 dB")
    # a fix that moves its own route's resonance onto a worse harmonic is dropped, small changes are not noted
    keep, note = harmonic_verdict(r(8.0, 6.5, 420e6), {}, -5.0, 6.0)
    assert not keep and note == "not selected: moves the worst margin +8.0 → +6.5 dB (at 420 MHz)"
    assert harmonic_verdict(r(8.0, 7.5), {"SCK": r(10.0, 9.5)}, -5.0, 6.0) == (True, "worst margin +8.0 → +7.5 dB")
    # own route not checked (far below the limit): its predicted gain at the old worst harmonic counts
    assert harmonic_verdict(None, {"SCK": r(10.0, 8.0)}, -5.0, 6.0)[0]
    assert not harmonic_verdict(None, {"SCK": r(10.0, 8.0)}, -1.5, 6.0)[0]


def test_suggest_fills_slot(tmp_path):
    board, sc, _ = make(tmp_path, microstrip_board(slot=(19.5, 4, 20.5, 16)), "slot", solve_points=1)
    res = suggest_fixes(board, sc.config)
    fills = [s for s in res.selected if s.kind == "fill"]
    assert fills and fills[0].modification.layer == "B.Cu"
    assert "gap_crossing" in {i.check for i in res.fixed}


def test_cli_suggest_write_then_compare(tmp_path, capsys):
    _, _, path = make(tmp_path, layer_change_board(None), "cli", solve_points=1)
    assert main(["suggest", str(path), "--write", "-q"]) == 0
    mods = SettingsFile.load(tmp_path / "groundtruth" / "cli.groundtruth.yaml").config.modifications
    assert any(m.enabled and m.type == "via" for m in mods)
    assert main(["check", str(path), "-q"]) == 1                   # the real board still fails
    assert main(["check", str(path), "-q", "--what-if"]) == 0     # with the suggested fixes it passes
    capsys.readouterr()
    assert main(["compare", str(path)]) == 0
    out = capsys.readouterr().out
    assert "fixed: " in out and "layer_change_stitch" in out
