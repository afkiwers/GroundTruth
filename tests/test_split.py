"""One Simulate split into parts (analysis/split.py): the items, their spread over servers, and the merged parts
equal to the whole run, through the compute server's own pickling."""
from dataclasses import replace

import numpy as np
import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.analysis.detect import detect_converters
from groundtruth.analysis.simulate import simulate
from groundtruth.analysis.split import merge_parts, plan_split, split_items
from groundtruth.config.settings import SettingsFile
from groundtruth.remote import protocol


def twins(tmp_path):
    """Two identical buck stages on one board (they share the input, the pours and the ground): the combined DC
    view and its issues exist only with both."""
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "twins", nets=())
    a = detect_converters(board, sc.config)[0].spec
    a.f_sw, a.i_out, a.v_in, a.v_out = 300e3, 3.0, 12.0, 3.0
    b = replace(a, name="twin")
    sc.set_converters([a, b])
    sc.save()
    return board, SettingsFile.load(sc.path).config


def test_items_and_their_spread_over_the_servers(tmp_path):
    """split_items lists what simulate would take (switched-off rows only when named); plan_split puts the costliest
    first, each where it finishes soonest, a faster server getting more."""
    board, cfg = twins(tmp_path)
    names = [c.name for c in cfg.converters]
    assert [i["name"] for i in split_items(cfg)] == names and all(i["kind"] == "stage" for i in split_items(cfg))
    off = replace(cfg, converters=[replace(cfg.converters[0], enabled=False), cfg.converters[1]])
    assert [i["name"] for i in split_items(off)] == names[1:]
    assert [i["name"] for i in split_items(off, [names[0]])] == names[:1]
    items = [{"name": n, "cost": c} for n, c in (("a", 6), ("b", 6), ("c", 1), ("d", 1), ("e", 1), ("f", 6))]
    plan = plan_split(items, [{"speed": 16}, {"speed": 8}])
    assert sorted(i["name"] for p in plan for i in p) == list("abcdef")
    load = [sum(i["cost"] for i in p) for p in plan]
    assert load[0] > load[1] and load == [15, 6] or load == [14, 7]
    assert plan_split(items[:1], [{"speed": 1}, {"speed": 1}])[1] == []


def test_the_merged_parts_equal_the_whole_run(tmp_path):
    """Each stage simulated as a part of its own (the first also running the job's rule checks), the parts pickled
    and loaded as the compute servers send them, then merged: the same issues, stages in the same order with the same
    numbers, and the stages' DC currents together (combine_dc from the parts' DcBoards) equal to the whole run's."""
    board, cfg = twins(tmp_path)
    whole = simulate(board, cfg)
    names = [c.name for c in cfg.converters]
    parts = []
    for k, name in enumerate(reversed(names)):          # the parts in another order than the settings'
        p = simulate(board, cfg, nets=[name], part={"checks": k == 1, "job_nets": None})
        p.drop_factorisations()
        p.drop_models()
        parts.append(protocol.loads(protocol.dumps(p)))
    merged = merge_parts(cfg, None, parts, notes=["split over 2 servers"])
    assert [r.net for r in merged.converters] == [r.net for r in whole.converters] == names
    assert [i.fingerprint for i in merged.issues] == [i.fingerprint for i in whole.issues]
    assert [i.message for i in merged.issues] == [i.message for i in whole.issues]
    for m, w in zip(merged.converters, whole.converters):
        assert m.loop_nh == pytest.approx(w.loop_nh) and m.worst["margin_db"] == pytest.approx(w.worst["margin_db"])
        assert [p["r_mohm"] for p in m.dc] == pytest.approx([p["r_mohm"] for p in w.dc])
        assert not m.dc_branch
    mc, wc = merged.dc_combined, whole.dc_combined
    assert mc is not None and wc is not None and mc.dc[0]["current_a"] == pytest.approx(wc.dc[0]["current_a"])
    assert [v["name"] for v in mc.dc[0]["vias"]] == [v["name"] for v in wc.dc[0]["vias"]]
    assert [v["current_a"] for v in mc.dc[0]["vias"]] == pytest.approx([v["current_a"] for v in wc.dc[0]["vias"]])
    for layer, m in mc.dc_maps[list(mc.dc_maps)[0]].items():
        assert np.allclose(m, wc.dc_maps[list(wc.dc_maps)[0]][layer], rtol=1e-5, atol=1e-7)
    assert "split over 2 servers" in merged.notes and merged.part is None
    assert merged.checks.routes.keys() == whole.checks.routes.keys()


def test_parts_from_different_models_are_not_combined(tmp_path):
    """Two parts whose whole-board DC models differ (another board): no combined view, a note says why, and each
    stage keeps its own DC-path issues."""
    board, cfg = twins(tmp_path)
    names = [c.name for c in cfg.converters]
    a = simulate(board, cfg, nets=[names[0]], part={"checks": True, "job_nets": None})
    b = simulate(board, cfg, nets=[names[1]], part={})
    for _, (db, _cur) in b.converters[0].dc_branch.items():
        db.fingerprint = "another model"
    merged = merge_parts(cfg, None, [a, b])
    assert merged.dc_combined is None and any("not combined" in n for n in merged.notes)


KEY = "split-test-key-0123456789"


def two_servers(tmp_path, monkeypatch):
    """Two compute servers on this machine forming a cluster (each other as peer on loopback): A the lead (--lead),
    B a member; once both see their lead."""
    import socket
    import time
    from groundtruth.remote import cluster as cl
    from groundtruth.remote.server import ComputeServer
    monkeypatch.setattr(cl, "BEACON_S", 0.2)
    ports = []
    for _ in range(2):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        s.close()
    a = ComputeServer(KEY, "127.0.0.1", 0, data_dir=tmp_path / "a", beacon_port=ports[0],
                      peers=(f"127.0.0.1:{ports[1]}",), lead=True)
    b = ComputeServer(KEY, "127.0.0.1", 0, data_dir=tmp_path / "b", beacon_port=ports[1],
                      peers=(f"127.0.0.1:{ports[0]}",))
    a.start(), b.start()
    deadline = time.time() + 15
    while time.time() < deadline and not (a.cluster.lead() and b.cluster.lead() and a.cluster.alive()):
        time.sleep(0.1)
    assert b.cluster.lead() and not b.cluster.lead()["me"]
    return a, b


def test_a_simulate_sent_to_a_member_is_split_by_the_lead_and_equals_one_run(tmp_path, monkeypatch):
    """The plugin sends a Simulate to the member B: B names its lead A, the job goes there (A proves it holds the
    key first) and A splits it: one stage on A, one on B, as part jobs; the merged result equals a whole run here,
    with a note naming both servers; afterwards no job is left on either server."""
    from groundtruth.remote.client import Remote
    board, cfg = twins(tmp_path)
    whole = simulate(board, cfg)
    a, b = two_servers(tmp_path, monkeypatch)
    try:
        r = Remote(f"127.0.0.1:{b.port}", KEY, client="tester@here")
        seen = []
        got = r.run("simulate", (board, cfg, None), lambda m, f: seen.append(m))
        assert r.host == f"127.0.0.1:{a.port}"                      # the job went to the lead
        assert any("split over 2 servers" in m for m in seen), seen[-5:]
        assert [i.fingerprint for i in got.issues] == [i.fingerprint for i in whole.issues]
        assert [c.net for c in got.converters] == [c.net for c in whole.converters]
        assert [c.loop_nh for c in got.converters] == pytest.approx([c.loop_nh for c in whole.converters])
        assert got.dc_combined is not None
        assert got.dc_combined.dc[0]["current_a"] == pytest.approx(whole.dc_combined.dc[0]["current_a"])
        assert any(n.startswith("split over 2 compute servers") for n in got.notes), got.notes
        assert got.setups == [] and got.model is None
        assert not a.jobs and not b.jobs
    finally:
        a.stop(), b.stop()


def test_a_lost_member_s_part_runs_on_the_lead_and_cancel_stops_every_part(tmp_path, monkeypatch):
    """B stops right after it got its part: after LOST_S its stage runs on A instead (said in a note) and the job
    still ends with both stages. A second job cancelled while split: its parts are cancelled too."""
    import time
    from groundtruth.remote import split
    from groundtruth.remote.client import Remote
    monkeypatch.setattr(split, "LOST_S", 1.0)
    board, cfg = twins(tmp_path)
    a, b = two_servers(tmp_path, monkeypatch)
    try:
        r = Remote(f"127.0.0.1:{a.port}", KEY, client="tester@here")
        jid, _ = r.submit("simulate", (board, cfg, None))
        deadline = time.time() + 60
        while time.time() < deadline and not any(j.kind == "part" for j in b.jobs.values()):
            time.sleep(0.1)
        b.stop()
        for _ in range(3000):
            info = r.state(jid)
            if info["state"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.2)
        assert info["state"] == "done", info
        got = r.fetch(jid)
        r.forget(jid)
        assert sorted(c.net for c in got.converters) == sorted(c.name for c in cfg.converters)
        assert any("so it ran on" in n for n in got.notes), got.notes
        # cancel while it runs: split (B is gone: both parts on A, or A's own part only) or, once the lead no longer
        # hears B, whole. Cancelled as soon as it started: waiting for parts that a whole run never makes let the
        # job finish, and forget then removed it (it passed on main only because the slower first job had used up
        # the shared deadline)
        jid, _ = r.submit("simulate", (board, cfg, None))
        deadline = time.time() + 120
        while time.time() < deadline and (a.jobs.get(jid) is None or a.jobs[jid].state == "queued") and \
                not any(j.kind == "part" for j in a.jobs.values()):
            time.sleep(0.02)
        r.forget(jid)
        for _ in range(300):
            if all(j.state in ("cancelled",) for j in a.jobs.values() if j.kind == "part") and \
                    a.jobs.get(jid) is not None and a.jobs[jid].state == "cancelled":
                break
            time.sleep(0.1)
        assert a.jobs[jid].state == "cancelled"
        assert all(j.state == "cancelled" for j in a.jobs.values() if j.kind == "part")
    finally:
        a.stop(), b.stop()


def test_two_stages_go_to_two_servers_whatever_their_cores():
    """The user's refboard (two power stages switched on) on build_pc (16 cores, the lead) and laptop (8 cores):
    one stage each ("it's not sharing the load": counting all cores put both on build_pc and ran the job whole). Cores
    count up to SPEED_CORES; a busy server's speed drops; an idle server without an item gets one, a busy one not."""
    from groundtruth.analysis.split import SPEED_CORES, server_speed
    stages = [{"name": "buck conv1", "cost": 6.0}, {"name": "buck conv2", "cost": 6.0}]
    assert server_speed(16) == server_speed(SPEED_CORES) == server_speed(8) and server_speed(8, busy=1) == 4.0
    plan = plan_split(stages, [{"speed": server_speed(16)}, {"speed": server_speed(8)}])
    assert [len(p) for p in plan] == [1, 1]
    # even with very different speeds an idle server is given one
    plan = plan_split(stages, [{"speed": 100.0}, {"speed": 1.0, "busy": 0}])
    assert [len(p) for p in plan] == [1, 1]
    # a busy member is not forced to take one
    plan = plan_split(stages, [{"speed": 8.0}, {"speed": server_speed(8, 3), "busy": 3}])
    assert [len(p) for p in plan] == [2, 0]


def test_the_reason_a_simulate_ran_whole_reaches_the_plugin():
    """A Simulate that is not split carries the reason in its job info (`note`), the plugin's job entry keeps it
    (jobs.state_fields) and its details box shows it."""
    from groundtruth.remote.jobs import state_fields
    from groundtruth.remote.server import Job
    from groundtruth.ui.jobs_panel import details
    job = Job("j1", "simulate", "me", note="not split over the cluster: no other server of the cluster answers")
    fields = state_fields(job.info(None))
    assert fields["note"].startswith("not split")
    assert "Cluster: not split over the cluster: no other server" in details({"id": "j1", **fields})


def test_the_plan_follows_each_server_s_memory_and_speed():
    """The user: "scale the work load depending on the machines capabilities". refboard at 16x: two power
    stages of ~9.6 GB each and four small nets; build_pc (18 GB free) and laptop (the user's laptop, 2.2 GB free): the
    stages only where they fit (build_pc), the nets spread by speed, laptop gets what fits it; an item that fits no server
    goes to the one with the most free memory; a faster server (measured) gets more."""
    from groundtruth.analysis.split import FIT_SHARE, fits
    stages = [{"name": f"buck conv{k}", "cost": 6.0, "mem_gb": 9.6} for k in (1, 2)]
    nets = [{"name": f"net{k}", "cost": 1.0, "mem_gb": 0.8} for k in range(4)]
    build_pc, xps = {"speed": 8.0, "free_gb": 18.0}, {"speed": 8.0, "free_gb": 2.2}
    assert fits(stages[0], build_pc) and not fits(stages[0], xps) and fits(nets[0], xps)
    plan = plan_split(stages + nets, [build_pc, xps])
    assert sorted(i["name"] for i in plan[0] if i["name"].startswith("buck")) == ["buck conv1", "buck conv2"]
    assert plan[1] and all(i["name"].startswith("net") for i in plan[1])
    # nothing fits the laptop: it gets nothing, and the stages still run (on build_pc)
    plan = plan_split(stages, [build_pc, xps])
    assert [len(p) for p in plan] == [2, 0]
    # too big for every server: to the one with the most free memory
    huge = [{"name": "huge", "cost": 6.0, "mem_gb": 100.0}, {"name": "net", "cost": 1.0, "mem_gb": 0.1}]
    assert [i["name"] for i in plan_split(huge, [xps, build_pc])[1]] == ["huge"]
    # measured speeds: a server three times as fast takes about three quarters of equal items
    many = [{"name": f"n{k}", "cost": 1.0, "mem_gb": 0.1} for k in range(8)]
    plan = plan_split(many, [{"speed": 3.0, "free_gb": 50}, {"speed": 1.0, "free_gb": 50}])
    assert [len(p) for p in plan] == [6, 2]
    assert FIT_SHARE < 1.0


def test_what_a_split_measured_is_kept_and_used(tmp_path):
    """Stats (the splitting server's data folder): a part's run gives its server's speed (from SPEED_MIN_S on) and
    its item's peak memory; a part that ran out of memory raises its items' need above that server's free memory;
    everything survives a restart (a new Stats from the file)."""
    from groundtruth.remote.split import SPEED_MIN_S, Stats
    st = Stats(tmp_path)
    assert st.mem("b.kicad_pcb", "buck conv1", 16) is None and st.speed("build_pc") is None
    st.record_part("b.kicad_pcb", 16, "build_pc", [{"name": "buck conv1", "cost": 6.0}], 600.0, 9200.0)
    st.record_part("b.kicad_pcb", 16, "build_pc", [{"name": "net", "cost": 1.0}], SPEED_MIN_S / 2, 300.0)
    st.record_too_big("b.kicad_pcb", 16, [{"name": "buck conv2", "cost": 6.0}], 2.2)
    again = Stats(tmp_path)
    assert again.speed("build_pc") == pytest.approx(6.0 / 600.0)          # the short part did not count for speed
    assert again.mem("b.kicad_pcb", "buck conv1", 16) == pytest.approx(9200 / 1024, abs=0.01)
    assert again.mem("b.kicad_pcb", "net", 16) == pytest.approx(300 / 1024, abs=0.01)
    assert again.mem("b.kicad_pcb", "buck conv2", 16) > 2.2 and again.mem("b.kicad_pcb", "buck conv1", 8) is None


def test_a_part_that_ran_out_of_memory_moves_to_a_server_it_fits(tmp_path, monkeypatch):
    """The user's case: laptop (2.2 GB free) ran out of memory on a power stage. The part moves to the server it
    fits that would finish it soonest, never back to a server it was tried on; its items' need is learned; the
    progress names the move; when no server is left that fits, the job fails with why."""
    from types import SimpleNamespace as NS
    from groundtruth.remote import split as remote_split
    from groundtruth.remote.split import Part, SplitRun
    monkeypatch.setattr(remote_split.socket, "gethostname", lambda: "build_pc")    # this server (the lead)
    srv = NS(data=tmp_path)
    job = NS(id="j1", client="me", message="", fraction=0.0)
    run = SplitRun(srv, job)
    run.board, run.resolution = "b.kicad_pcb", 16.0
    me = "build_pc"
    run.servers = [{"host": me, "remote": None, "speed": 8.0, "free_gb": 18.0},
                   {"host": "laptop", "remote": None, "speed": 8.0, "free_gb": 2.2},
                   {"host": "other-pc", "remote": None, "speed": 8.0, "free_gb": 100.0}]
    sent = []
    run._submit = lambda part: sent.append(part.host)
    stage = {"name": "buck conv2", "cost": 6.0, "mem_gb": 1.5}
    p = Part("laptop", None, [stage], False, run.servers[1])
    run.parts = [Part(me, None, [{"name": "buck conv1", "cost": 6.0, "mem_gb": 9.6}], True, run.servers[0]), p]
    run._move(p, "ran out of memory", memory=True)
    assert sent == ["other-pc"] and p.host == "other-pc" and p.tried == [id(run.servers[1]), id(run.servers[2])]
    assert run.stats.mem("b.kicad_pcb", "buck conv2", 16) > 2.2 and stage["mem_gb"] > 2.2
    run._progress()
    assert "laptop ran out of memory: moved to other-pc" in job.message
    run._move(p, "ran out of memory", memory=True)                    # other-pc too: back to this server
    assert sent[-1] == me
    with pytest.raises(RuntimeError, match="no other server"):
        run._move(p, "ran out of memory", memory=True)
