"""Preferences › Compute server › Test connection laid out (ui/server_info.py; the user, of the one-paragraph answer:
"Format this better!")."""
import time

import pytest

from groundtruth.ui.server_info import server_summary, summary_text

NOW = time.time()


def _status(**kw):
    """A status like the user's Mac leading a two-server cluster."""
    st = {"host": "Andres-MacBook-Air.local", "version": "GroundTruth 0.1.41500 (9724629)", "code": "ed1ff59c30b9",
          "python": "3.13.5", "cores": 8, "memory_gb": 24.0, "free_gb": 11.5, "gpus": [],
          "gpu_note": "a Mac (no CUDA on macOS)", "workers": 1, "running": [{}], "queue": [{}, {}],
          "results_waiting": 1, "keep_hours": 24, "uptime_s": 6300, "addresses": ["192.168.10.145"], "address": "",
          "remote_update": True, "update": {},
          "cluster": {"role": "lead", "size": 2, "together_since": NOW - 6300, "now": NOW, "forced_lead": True,
                      "lead": {"me": True}, "events": [{"event": "lost", "host": "xps13", "at": NOW - 600,
                                                         "text": "no beacon"}],
                      "members": [{"host": "andre", "address": "192.168.10.127:8765", "cores": 16, "memory_gb": 31.7,
                                   "gpus": ["RTX 3050"], "joined_at": NOW - 6300, "lead": False}]}}
    st.update(kw)
    return st


def test_the_answer_is_rows_and_a_cluster_table():
    """One row per fact (version, code, cores, memory, GPU with its note, jobs, updates), the cluster as one line
    per server (this one first, marked), notes for --lead and lost servers; Copy details has it all as text."""
    s = server_summary(_status(), "ed1ff59c30b9", "GroundTruth 0.1.41521")
    rows = dict(s["rows"])
    assert s["ok"] and s["title"] == "Connected to Andres-MacBook-Air.local" and not s["warnings"]
    assert rows["Code"].startswith("ed1ff59c30b9") and "same as this plugin" in rows["Code"]
    assert rows["Memory"] == "24.0 GB, 11.5 GB free" and rows["Processor"] == "8 cores"
    assert rows["GPU"].startswith("none: the solves run on the CPU") and "a Mac" in rows["GPU"]
    assert rows["Jobs"] == "1 at a time · 1 running · 2 queued · 1 result(s) waiting to be fetched"
    assert rows["Running for"] == "1 h 45 min" and rows["Remote updates"].startswith("on")
    assert s["cluster_title"] == "Cluster of 2 servers, together for 1 h 45 min"
    assert [(r["role"], r["host"]) for r in s["cluster"]] == [("lead", "Andres-MacBook-Air.local (this one)"),
                                                             ("member", "andre")]
    assert s["cluster"][1]["gpu"] == "RTX 3050" and s["cluster"][1]["since"] == "1 h 45 min"
    assert any("--lead" in n for n in s["cluster_notes"]) and any("xps13 was lost" in n for n in s["cluster_notes"])
    text = summary_text(s)
    assert "Memory: 24.0 GB, 11.5 GB free" in text and "member  andre" in text


def test_another_version_and_a_standalone_server():
    """A server with other code: the headline says it can't take the jobs and a warning says why; a server that
    hears no other: standalone, no table."""
    s = server_summary(_status(code="0123456789ab", cluster={"role": "standalone"}), "ed1ff59c30b9", "GT x")
    assert not s["ok"] and "can't take this plugin's jobs" in s["title"]
    assert any("another GroundTruth" in w for w in s["warnings"])
    assert s["cluster"] == [] and s["cluster_title"].startswith("Standalone")
    # just restarted (an update): the others' beacons not heard yet, said so (the user: "it says no other server
    # with this key is heard. However, andre has the same key", at "Running for 1 s")
    fresh = server_summary(_status(cluster={"role": "standalone"}, uptime_s=1), "ed1ff59c30b9", "GT x")
    assert fresh["cluster_title"].startswith("No other server heard yet: this one started 1 s ago")


def test_the_dialog_builds(monkeypatch):
    """The dialog lays the summary out: a value per row, the cluster list with a line per server."""
    wx = pytest.importorskip("wx")
    from groundtruth.ui import theme
    from groundtruth.ui.server_info import ServerInfoDialog
    app = wx.App(False)
    theme.init_app(app, "light")
    try:
        dlg = ServerInfoDialog(None, "192.168.10.145:8765", server_summary(_status(), "ed1ff59c30b9", "GT"))
        labels = [c.GetLabel() for c in dlg.GetChildren() if isinstance(c, wx.StaticText)]
        assert "24.0 GB, 11.5 GB free" in labels and any(lbl.endswith("Connected to Andres-MacBook-Air.local")
                                                         for lbl in labels)
        assert dlg.cluster.GetItemCount() == 2 and dlg.cluster.GetItemText(1, 1) == "andre"
        dlg.Destroy()
    finally:
        app.Destroy()


def test_the_update_confirmation_lists_the_servers_now_and_after(monkeypatch):
    """Update / Update all ask with a dialog laid out like Test connection's (the user: "that update server dialog
    needs the same formatting as Test connection now has"): a headline, a line per server (now, after), the notes;
    its OK button is Update."""
    wx = pytest.importorskip("wx")
    from groundtruth.ui import theme
    from groundtruth.ui.server_info import UpdateConfirmDialog, update_summary
    rows = [{"name": "Andres-MacBook-Air.local", "version": "0.1.41524 (29fb7ad)"},
            {"name": "andre", "version": "0.1.41524 (29fb7ad)"}]
    s = update_summary(rows, "GroundTruth 0.1.41525 (64c896a, built 2026-10-16 19:57)")
    assert s["title"] == "Update 2 compute servers to 0.1.41525 (64c896a)?"
    assert s["rows"][1] == {"name": "andre", "now": "0.1.41524 (29fb7ad)", "after": "0.1.41525 (64c896a)"}
    assert s["built"] == "this plugin's version, built 2026-10-16 19:57" and len(s["notes"]) == 3
    assert update_summary(rows[:1], "GroundTruth 0.1.41525 (64c896a)")["title"] == \
        "Update Andres-MacBook-Air.local to 0.1.41525 (64c896a)?"
    app = wx.App(False)
    theme.init_app(app, "light")
    try:
        dlg = UpdateConfirmDialog(None, s)
        assert dlg.list.GetItemCount() == 2 and dlg.list.GetItemText(0, 2) == "0.1.41525 (64c896a)"
        assert dlg.ok.GetLabel() == "Update all" and dlg.ok.GetId() == wx.ID_OK
        dlg.Destroy()
    finally:
        app.Destroy()
