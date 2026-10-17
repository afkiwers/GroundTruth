"""The simulation history (report/history.py): runs kept next to the board, shared board copies, listing,
comparing, notes, deleting; `simulate --history` and `groundtruth history`."""
import gzip
import json
from datetime import datetime

import pytest

from boardgen import microstrip_board
from conftest import make
from groundtruth.cli import main
from groundtruth.report.history import (History, HistoryError, changes, compare, comparison_text, history_for,
                                        run_meta, runs_text)

BOARD = b"(kicad_pcb (version 20250000) (generator pcbnew))\n" * 50


def results(margin: float, l_nh: float, issues=()) -> str:
    return json.dumps({
        "tool": "groundtruth", "board": "b.kicad_pcb",
        "summary": {"error": sum(1 for i in issues if i["severity"] == "error"), "warning": 0, "info": 0},
        "issues": list(issues),
        "routes": [{"net": "SIG", "driver": "U1.1", "receiver": "U2.1", "worst": {"f": 150e6, "margin_db": margin},
                    "l_ratio": 1.3, "error": ""}],
        "converters": [{"net": "conv1", "worst": {"f": 120e6, "margin_db": margin - 20},
                        "conducted_worst": {"f": 0.5e6, "margin_db": -10.0}, "loop_nh": l_nh, "error": ""}],
        "bridges": []})


def issue(fp: str, check="emi_margin", severity="error"):
    return {"fingerprint": fp, "check": check, "severity": severity, "net": "SIG", "message": f"m {fp}",
            "waived": False}


def keep(h: History, board=BOARD, settings="nets: []\n", margin=5.0, l_nh=3.0, issues=(), when=None, **kw):
    meta = run_meta("C:/p/b.kicad_pcb", "file", by="cli", **kw)
    return h.record(board, settings, results(margin, l_nh, issues), meta, now=when)


def test_runs_share_unchanged_boards_and_list_newest_first(tmp_path):
    h = History(tmp_path / "groundtruth" / "history")
    t = datetime(2026, 10, 9, 15, 0, 0)
    r1 = keep(h, when=t)
    r2 = keep(h, when=t, settings="nets: [SIG]\n")              # same second: its own folder
    r3 = keep(h, board=BOARD + b"(segment)\n", when=datetime(2026, 10, 9, 16, 0, 0), resolution=2.0)
    assert (r1.id, r2.id) == ("2026-10-09_15-00-00", "2026-10-09_15-00-00-2")
    assert sorted(f.name for f in h.boards_dir.iterdir()) == [f"{r1.id}.kicad_pcb.gz", f"{r3.id}.kicad_pcb.gz"]
    assert r2.board_name == r1.board_name == f"{r1.id}.kicad_pcb.gz"   # r2's board did not change: r1's copy
    assert gzip.decompress(h.board_copy_path(r1).read_bytes()) == BOARD
    runs = h.runs()
    assert [r.id for r in runs] == [r3.id, r2.id, r1.id]
    ch = changes(runs)
    assert ch[r1.id] == ["first"] and ch[r2.id] == ["settings"] and ch[r3.id] == ["board", "settings", "resolution"]
    assert runs[0].worst("radiated_db")["name"] == "conv1" and runs[0].worst("conducted_db")["radiated_db"] == -15
    assert (r1.folder / "settings.groundtruth.yaml").read_text(encoding="utf-8") == "nets: []\n"
    assert json.loads((r1.folder / "results.json").read_text(encoding="utf-8"))["routes"][0]["net"] == "SIG"
    text = runs_text(runs)
    assert "1  2026-10-09_16-00-00" in text and "[first]" in text and "radiated -15.0 dB conv1" in text


def test_find_by_number_id_or_prefix(tmp_path):
    h = History(tmp_path / "h")
    a = keep(h, when=datetime(2026, 10, 8, 9, 0, 0))
    b = keep(h, when=datetime(2026, 10, 9, 9, 0, 0))
    assert h.find("1").id == b.id and h.find("2").id == a.id
    assert h.find("2026-10-08").id == a.id and h.find(b.id).id == b.id
    for bad in ("3", "2026-10", "nope"):
        with pytest.raises(HistoryError):
            h.find(bad)


def test_compare_lists_margins_issues_and_the_settings_diff(tmp_path):
    h = History(tmp_path / "h")
    a = keep(h, margin=2.0, l_nh=3.5, issues=[issue("aaa"), issue("bbb")], settings="rise_time: 1ns\n",
             when=datetime(2026, 10, 9, 9, 0, 0))
    b = keep(h, margin=6.5, l_nh=2.25, issues=[issue("bbb"), issue("ccc", "hot_loop", "warning")],
             settings="rise_time: 2ns\n", when=datetime(2026, 10, 9, 10, 0, 0))
    cmp = compare(a, b)
    sig = next(r for r in cmp["rows"] if r["name"] == "SIG U1.1->U2.1")
    assert sig["radiated_db"] == (2.0, 6.5, 4.5)
    conv = next(r for r in cmp["rows"] if r["name"] == "conv1")
    assert conv["loop_nh"] == (3.5, 2.25, -1.25) and conv["conducted_db"] == (-10.0, -10.0, 0.0)
    assert [i["fingerprint"] for i in cmp["fixed"]] == ["aaa"] and [i["fingerprint"] for i in cmp["new"]] == ["ccc"]
    assert "-rise_time: 1ns" in cmp["settings_diff"] and "+rise_time: 2ns" in cmp["settings_diff"]
    text = comparison_text(cmp)
    assert "(same board, settings and resolution)" not in text
    assert "SIG U1.1->U2.1: radiated +2.0 dB → +6.5 dB (+4.5 dB)" in text
    assert "L 3.50 nH → 2.25 nH (-1.25 nH)" in text and "fixed: 1 issue(s)" in text and "new: 1 issue(s)" in text


def test_notes_delete_and_board_copies_no_run_needs(tmp_path):
    h = History(tmp_path / "h")
    a = keep(h, when=datetime(2026, 10, 9, 9, 0, 0))
    b = keep(h, board=BOARD + b"x", when=datetime(2026, 10, 9, 10, 0, 0))
    h.set_note(a, "before the\nGND vias  ")
    assert h.find(a.id).note == "before the GND vias"
    assert h.delete([b]) == 1
    assert [r.id for r in h.runs()] == [a.id]
    assert [f.name for f in h.boards_dir.iterdir()] == [f"{a.id}.kicad_pcb.gz"]


def test_a_shared_copy_named_after_a_deleted_run_takes_the_next_runs_name(tmp_path):
    h = History(tmp_path / "h")
    a = keep(h, when=datetime(2026, 10, 9, 9, 0, 0))
    b = keep(h, when=datetime(2026, 10, 9, 10, 0, 0))                  # same board: a's copy
    c = keep(h, when=datetime(2026, 10, 9, 11, 0, 0))
    assert c.board_name == a.board_name
    h.delete([a])
    assert [f.name for f in h.boards_dir.iterdir()] == [f"{b.id}.kicad_pcb.gz"]
    assert {r.board_name for r in h.runs()} == {f"{b.id}.kicad_pcb.gz"}
    assert h.board_bytes(h.find(c.id)) == BOARD


def test_runs_of_the_first_history_version_find_their_hash_named_copy(tmp_path):
    h = History(tmp_path / "h")
    a = keep(h)
    old = h.boards_dir / f"{a.board_sha}.kicad_pcb.gz"
    h.board_copy_path(a).rename(old)
    meta = json.loads((a.folder / "run.json").read_text(encoding="utf-8"))
    del meta["board"]["file"]
    (a.folder / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    assert h.board_bytes(h.find(a.id)) == BOARD
    b = keep(h, when=datetime(2030, 1, 1, 0, 0, 0))                    # same board: a copy named after this run
    assert b.board_name == f"{b.id}.kicad_pcb.gz" and h.board_bytes(b) == BOARD


def test_a_board_copy_is_checked_against_its_hash(tmp_path):
    h = History(tmp_path / "h")
    a = keep(h)
    out = h.extract_board(a, tmp_path / "copy" / "b.kicad_pcb")
    assert out.read_bytes() == BOARD
    h.board_copy_path(a).write_bytes(gzip.compress(b"(kicad_pcb tampered)"))
    with pytest.raises(HistoryError, match="does not match its hash"):
        h.board_bytes(a)
    forged = json.loads((a.folder / "run.json").read_text(encoding="utf-8"))
    for name in ("../../evil.kicad_pcb.gz", "C:/x.kicad_pcb.gz", f"{a.id}.kicad_pcb.gz\n"):
        forged["board"]["file"] = name                                # a path in run.json is never followed
        (a.folder / "run.json").write_text(json.dumps(forged), encoding="utf-8")
        assert h.find(a.id).board_name == "" and h.board_copy_path(h.find(a.id)) is None
    forged["board"]["sha256"] = "../../../evil"
    (a.folder / "run.json").write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(HistoryError, match="names no board copy"):
        h.board_bytes(h.find(a.id))


def test_damaged_runs_are_skipped(tmp_path):
    h = History(tmp_path / "h")
    a = keep(h)
    (h.runs_dir / "2026-01-01_00-00-00").mkdir()                    # half written: no run.json
    (h.runs_dir / "not a run").mkdir()
    assert [r.id for r in h.runs()] == [a.id]


def test_files_deleted_or_edited_by_hand_never_crash_it(tmp_path):
    """Anything in the folder may be deleted or edited by hand: a board copy, results.json, the settings copy, a
    report, a run folder, the whole folder; run.json garbled. Listing, comparing and the texts still work; what
    needs a missing file raises HistoryError saying what is missing."""
    import shutil
    h = History(tmp_path / "h")
    a = keep(h, issues=[issue("aaa")], when=datetime(2026, 10, 9, 9, 0, 0))
    b = keep(h, board=BOARD + b"x", issues=[issue("bbb")], when=datetime(2026, 10, 9, 10, 0, 0))
    c = keep(h, board=BOARD + b"y", when=datetime(2026, 10, 9, 11, 0, 0))

    h.board_copy_path(a).unlink()                                      # a board copy deleted
    with pytest.raises(HistoryError, match="missing or damaged"):
        h.extract_board(a, tmp_path / "out" / "a.kicad_pcb")
    h.board_copy_path(b).write_bytes(b"not gzip at all")               # ... or replaced by something else
    with pytest.raises(HistoryError, match="missing or damaged"):
        h.board_bytes(b)

    (b.folder / "results.json").unlink()                               # results and settings deleted
    (b.folder / "settings.groundtruth.yaml").unlink()
    cmp = compare(a, b)
    assert cmp["fixed"] is None and cmp["rows"] and b.settings_text() is None and cmp["settings_diff"] == []
    text = comparison_text(cmp)
    assert "results.json is missing" in text and "settings.groundtruth.yaml is missing" in text

    (c.folder / "run.json").write_text(json.dumps({"counts": [1], "items": [1, {"radiated_db": "x"},
                                                                              {"radiated_db": -3.0}],
                                                   "resolution": "x", "what_if": "y", "board": "z"}),
                                       encoding="utf-8")                # run.json edited into nonsense
    runs = h.runs()
    garbled = h.find(c.id, runs)
    assert garbled.counts == {} and garbled.board_sha == "" and garbled.worst("radiated_db")["name"] == ""
    assert "radiated -3.0 dB" in runs_text(runs) and changes(runs)[c.id]
    assert comparison_text(compare(b, garbled))
    (a.folder / "run.json").write_text("[1, 2", encoding="utf-8")       # unreadable: left out of the list
    assert a.id not in [r.id for r in h.runs()]

    shutil.rmtree(c.folder)                                            # a run folder deleted by hand
    with pytest.raises(HistoryError):
        h.set_note(c, "too late")
    with pytest.raises(HistoryError, match="deleted before its report"):
        h.write_report_checked(c, "<html></html>")
    assert h.delete([c]) == 1                                          # already gone: no error

    shutil.rmtree(h.root)                                              # the whole folder
    assert h.runs() == [] and h.delete([a, b]) == 2
    d = keep(h, when=datetime(2026, 10, 9, 12, 0, 0))                  # the next run starts it again
    assert [r.id for r in h.runs()] == [d.id] and h.board_bytes(d) == BOARD


def test_a_deleted_board_copy_is_made_again_by_the_next_run_on_that_board(tmp_path):
    h = History(tmp_path / "h")
    a = keep(h, when=datetime(2026, 10, 9, 9, 0, 0))
    h.board_copy_path(a).unlink()
    b = keep(h, when=datetime(2026, 10, 9, 10, 0, 0))                  # same board: a new copy, named after b
    assert b.board_name == f"{b.id}.kicad_pcb.gz"
    assert h.board_bytes(b) == BOARD and h.board_bytes(a) == BOARD     # the earlier run has it back too


def test_history_folder_is_next_to_the_board():
    assert history_for("C:/proj/kraft.kicad_pcb").root.as_posix() == "C:/proj/groundtruth/history"


def test_two_boards_in_one_folder_share_the_history_folder_and_keep_their_runs_apart(tmp_path):
    """No folder per board: runs/ and boards/ directly in groundtruth/history; each board lists only its own
    runs (a run names its board), and deleting one board's runs keeps the other's copies."""
    a, b = history_for(tmp_path / "a.kicad_pcb"), history_for(tmp_path / "b.kicad_pcb")
    assert a.root == b.root == tmp_path / "groundtruth" / "history"
    ra = a.record(BOARD, "", results(1.0, 2.0), run_meta(str(tmp_path / "a.kicad_pcb"), "file", by="cli"),
                  now=datetime(2026, 10, 9, 9, 0, 0))
    rb = b.record(BOARD + b"b", "", results(1.0, 2.0), run_meta(str(tmp_path / "b.kicad_pcb"), "file", by="cli"),
                  now=datetime(2026, 10, 9, 10, 0, 0))
    assert [r.id for r in a.runs()] == [ra.id] and [r.id for r in b.runs()] == [rb.id]
    assert ra.folder.parent == a.root / "runs" and len(a.all_runs()) == 2
    a.delete([ra])
    assert [r.id for r in b.runs()] == [rb.id] and b.board_bytes(rb) == BOARD + b"b"
    assert [f.name for f in b.boards_dir.iterdir()] == [f"{rb.id}.kicad_pcb.gz"]


def test_simulate_history_and_the_history_command(tmp_path, capsys):
    """groundtruth simulate --history keeps the run (board copy, settings, results, report); a second run with
    other settings shares the board copy; `groundtruth history` lists both and compares them."""
    _, sc, path = make(tmp_path, microstrip_board(), "h", solve_points=2)
    assert main(["simulate", str(path), "-q", "--history"]) in (0, 1)      # 1: the board has issues
    sc.doc["nets"][0]["rise_time"] = "2ns"
    sc.save()
    assert main(["simulate", str(path), "-q", "--history"]) in (0, 1)      # 1: the board has issues
    h = history_for(path)
    runs = h.runs()
    assert len(runs) == 2 and len(list(h.boards_dir.iterdir())) == 1
    assert h.board_bytes(runs[0]) == path.read_bytes()
    assert runs[0].meta["by"] == "cli" and runs[0].meta["board"]["source"] == "file"
    assert runs[0].report is not None and "<html" in runs[0].report.read_text(encoding="utf-8")[:500].lower()
    assert "2ns" in runs[0].settings_text() and runs[0].items[0]["radiated_db"] is not None
    capsys.readouterr()
    assert main(["history", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[settings]" in out and "[first]" in out
    assert main(["history", str(path), "--compare", "1"]) == 0          # against the run before it
    out = capsys.readouterr().out
    assert f"{runs[1].id} → {runs[0].id}" in out and "+  rise_time: 2ns" not in out and "rise_time" in out
    assert main(["history", str(path), "--compare", "1", "2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["a"] == runs[1].id
    assert main(["history", str(path), "--compare", "9"]) == 2


def test_check_has_no_history_and_a_board_without_runs_says_so(tmp_path, capsys):
    _, _, path = make(tmp_path, microstrip_board(), "c")
    assert main(["check", str(path), "-q"]) == 0
    assert not (tmp_path / "groundtruth" / "history").exists()
    assert main(["history", str(path)]) == 0
    assert "no runs recorded yet" in capsys.readouterr().out


def test_a_run_keeps_its_cell_size_for_the_resolution_choice(tmp_path):
    """Preferences shows each Resolution level's cell size before this session's first Simulate, from the newest
    kept run: its run.json records the coarsest model's 1× cell; a run kept before that is read from the grid
    pitches in its results.json (at 4× the cells are half the size: the 1× cell is twice the pitch)."""
    from groundtruth.report.history import cell_mm_of
    h = History(tmp_path / "groundtruth" / "history")
    new = keep(h, when=datetime(2026, 10, 12, 9, 0, 0), resolution=4.0, cell_mm=cell_mm_of([0.1, 0.12], 4.0))
    assert new.meta["cell_mm"] == pytest.approx(0.24) and h.runs()[0].cell_mm() == pytest.approx(0.24)
    old_results = json.dumps({"routes": [{"net": "A", "grid": {"d_mm": 0.105}}, {"net": "B", "grid": {"d_mm": 0.0}}],
                              "converters": [{"net": "conv1", "grid": {"d_mm": 0.08}}], "bridges": []})
    meta = run_meta("C:/p/b.kicad_pcb", "file", by="cli", resolution=4.0)
    meta.pop("cell_mm")                                          # as written before the field existed
    old = h.record(BOARD, "", old_results, meta, now=datetime(2026, 10, 1, 9, 0, 0))
    assert old.cell_mm() == pytest.approx(0.21)
    assert cell_mm_of([]) is None and cell_mm_of([0.2], "x") == pytest.approx(0.2)    # a hand-edited resolution
