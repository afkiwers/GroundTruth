"""Compute servers finding each other and electing a lead (remote/cluster.py): the rules on beacons handed over by
hand, then real UDP between servers on this machine."""
import time

import pytest

from groundtruth.remote import cluster as cl
from groundtruth.remote.cluster import Cluster, find_servers, sign, verify

KEY = "cluster-test-key-0123456789"


def node(host, code="c0de", key=KEY, forced=False, **kw):
    """A cluster member without sockets."""
    return Cluster(key, 8765, code, lambda: {"cores": 8}, forced_lead=forced, host=host, **kw)


def hear(a, b, now=None):
    """a receives b's beacon (as if from b's address)."""
    return a.accept(b.beacon(), f"10.0.0.{abs(hash(b.host)) % 200 + 1}", now)


def test_beacons_are_signed_and_only_our_key_and_code_count():
    """A beacon with another key, a tampered one, an old one, our own: dropped; same key, other code: foreign
    (shown, never used); same key and code: a peer. The key itself is not in the beacon."""
    a, b = node("A"), node("B")
    msg = b.beacon()
    assert KEY not in str(msg) and verify(KEY, msg)
    assert a.accept(node("X", key="another-key-0123456789").beacon(), "10.0.0.9") is None
    forged = dict(msg, host="EVIL")
    assert a.accept(forged, "10.0.0.9") is None
    assert a.accept(sign(KEY, dict(msg, time=time.time() - 3600)), "10.0.0.9") is None
    assert a.accept(a.beacon(), "10.0.0.1") is None
    assert a.accept(node("C", code="other").beacon(), "10.0.0.3") == "foreign"
    assert hear(a, b) == "peer" and [p.host for p in a.alive()] == ["B"]
    assert a.status()["other_versions"][0]["host"] == "C"


def test_the_first_to_find_another_becomes_lead_and_all_agree():
    """A alone: standalone. A hears B (no claim around): A claims and leads; B hears A's claim and follows; a
    third server joining follows the same lead."""
    a, b, c = node("A"), node("B"), node("C")
    assert a.elect() is None and a.status()["role"] == "standalone"
    hear(a, b)
    lead = a.elect()
    assert lead["me"] and a.claim is not None
    hear(b, a)
    assert b.elect()["host"] == "A" and b.claim is None
    hear(c, a), hear(c, b)
    assert c.elect()["host"] == "A"


def test_competing_claims_settle_the_same_everywhere_and_a_forced_lead_wins():
    """Two servers claiming at once: the earlier claim wins on both, the other gives its claim up. A server started
    with --lead beats an automatic lead, even a later one."""
    a, b = node("A"), node("B")
    a.claim, b.claim = 100.0, 200.0
    hear(a, b), hear(b, a)
    assert a.elect()["me"] and b.elect()["host"] == "A" and b.claim is None
    f = node("F", forced=True)                  # started later, but forced
    hear(a, f), hear(f, a)
    assert a.elect()["host"] == "F" and f.elect()["me"]
    assert a.claim is None and f.claim is not None


def test_a_gone_lead_is_replaced(monkeypatch):
    """The lead not heard for ALIVE_S: dropped; the others elect again among themselves."""
    a, b, c = node("A"), node("B"), node("C")
    a.claim = 100.0
    for x, y in ((b, a), (b, c), (c, a), (c, b)):
        hear(x, y)
    assert b.elect()["host"] == "A" and c.elect()["host"] == "A"
    later = time.time() + cl.ALIVE_S + 1
    hear(b, c, now=later), hear(c, b, now=later)          # A falls silent; B and C still hear each other
    nb = b.elect(now=later)
    hear(c, b, now=later)                                 # B's new claim reaches C
    assert nb["me"] and c.elect(now=later)["host"] == "B"


def test_real_udp_two_servers_elect_and_find_servers_answers_only_our_key():
    """Two clusters on this machine (their own beacon ports, each other as static peer): they find each other and
    agree on one lead; Find servers sent to one gets its signed answer; with another key, no answer."""
    import socket
    ports = []
    for _ in range(2):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        s.close()
    a = Cluster(KEY, 9001, "c0de", lambda: {"cores": 4}, host="A", beacon_port=ports[0],
                static_peers=(f"127.0.0.1:{ports[1]}",))
    b = Cluster(KEY, 9002, "c0de", lambda: {"cores": 4}, host="B", beacon_port=ports[1],
                static_peers=(f"127.0.0.1:{ports[0]}",))
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cl, "BEACON_S", 0.2)
    try:
        a.start(), b.start()
        deadline = time.time() + 10
        while time.time() < deadline and not (a.lead() and b.lead()):
            time.sleep(0.1)
        time.sleep(0.6)                                     # a few rounds: both settle on the same lead
        assert a.lead() and b.lead() and a.lead()["host"] == b.lead()["host"]
        roles = sorted([a.status()["role"], b.status()["role"]])
        assert roles == ["lead", "member"]
        got = find_servers(KEY, timeout=1.0, beacon_port=ports[0], targets=("127.0.0.1",))
        assert [s["host"] for s in got] == ["A"] and got[0]["port"] == 9001
        assert find_servers("wrong-key-0123456789", timeout=0.5, beacon_port=ports[0], targets=("127.0.0.1",)) == []
    finally:
        a.stop(), b.stop()
        monkey.undo()


def free_udp_ports(n):
    """`n` UDP ports free on this machine now."""
    import socket
    out = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        out.append(s.getsockname()[1])
        s.close()
    return out


def test_two_compute_servers_show_their_cluster_in_the_status(tmp_path, monkeypatch):
    """Two compute servers with the cluster on (each other as peers): their status names one lead and the other as
    member; Test connection's text (describe) and install_server.cmd status (status_block) show it."""
    import sys
    from pathlib import Path
    from groundtruth.remote.client import Remote, describe
    from groundtruth.remote.server import ComputeServer
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import server_setup
    monkeypatch.setattr(cl, "BEACON_S", 0.2)
    p = free_udp_ports(2)
    a = ComputeServer(KEY, "127.0.0.1", 0, data_dir=tmp_path / "a", beacon_port=p[0], peers=(f"127.0.0.1:{p[1]}",),
                      lead=True)
    b = ComputeServer(KEY, "127.0.0.1", 0, data_dir=tmp_path / "b", beacon_port=p[1], peers=(f"127.0.0.1:{p[0]}",))
    try:
        a.start(), b.start()
        deadline = time.time() + 10
        while time.time() < deadline and not (a.cluster.lead() and b.cluster.lead()):
            time.sleep(0.1)
        sa, sb = (Remote(f"127.0.0.1:{s.port}", KEY).status() for s in (a, b))
        assert sa["cluster"]["role"] == "lead" and sa["cluster"]["forced_lead"]
        assert sb["cluster"]["role"] == "member" and sb["cluster"]["lead"]["port"] == a.port
        assert [m["address"] for m in sb["cluster"]["members"]] == [f"127.0.0.1:{a.port}"]
        assert "Cluster: 2 servers" in describe(sa) and "lead this server" in describe(sa) and "set with --lead" in describe(sa)
        assert "  member " in describe(sa) and "in the cluster" in describe(sa) and "  lead " in describe(sb)
        tb, ta = server_setup.status_block(sb, b.port), server_setup.status_block(sa, a.port)
        assert "Cluster:   2 servers, together since" in tb and "Lead:      127.0.0.1" not in tb
        assert "Lead:      this server (set with --lead)" in ta and f"(lead): " in tb
        assert f"127.0.0.1:{a.port} (lead)" in tb and "in the cluster" in tb and "heard" in tb
        assert "* " in ta and "(this server, lead)" in ta and "History:" in ta and "joined" in ta
        assert all(len(ln) <= server_setup.WIDTH for ln in ta.splitlines() + tb.splitlines())
    finally:
        a.stop(), b.stop()


def test_a_server_without_the_cluster_says_so():
    """beacon_port 0 (the tests' default): no discovery, shown as off; describe adds nothing."""
    import sys
    from pathlib import Path
    from groundtruth.remote.client import cluster_text
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import server_setup
    st = {"host": "h", "code": "c", "cores": 4, "memory_gb": 8, "free_gb": 4, "gpus": [], "workers": 1,
          "running": [], "queue": [], "cluster": {"role": "standalone", "discovery": False}}
    assert cluster_text(st["cluster"], "h") == ""
    assert "Cluster:   off (beacon_port 0 in server.json)" in server_setup.status_block(st, 8765)


def test_the_installer_joins_a_cluster_and_opens_the_beacon_port(tmp_path, capsys, monkeypatch):
    """install --key K --lead --peers --beacon-port: the key written as given (a short one refused), server.json with
    the cluster's entries, the firewall's UDP rule next to the TCP one; --no-lead undoes the lead."""
    import json
    import sys
    from pathlib import Path
    from types import SimpleNamespace as NS
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import server_setup
    monkeypatch.setattr(server_setup, "kicad_python", lambda: Path(r"C:\KiCad\bin\python.exe"))
    assert server_setup.main(["install", "--dir", str(tmp_path / "gt"), "--dry-run", "--lead", "--peers",
                              "other-pc, bigpc:9000", "--beacon-port", "8800"]) == 0
    out = capsys.readouterr().out
    assert "protocol=UDP" in out and "localport=8800" in out and "localport=8765" in out
    assert '"lead": true' in out and '"peers": ["other-pc", "bigpc:9000"]' in out and '"beacon_port": 8800' in out
    monkeypatch.setattr(server_setup, "DRY", {"on": False})
    paths = server_setup.layout(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    assert server_setup.make_key(paths, given="  the-clusters-key-0123456789 ") == "the-clusters-key-0123456789"
    assert paths["key"].read_text(encoding="utf-8").strip() == "the-clusters-key-0123456789"
    with pytest.raises(SystemExit):
        server_setup.make_key(paths, given="short")
    args = NS(port=None, workers=None, threads=None, gpu=None, keep_hours=None, lead=False, peers=None,
              beacon_port=None)
    paths["settings"].write_text(json.dumps({"lead": True, "peers": ["other-pc"]}), encoding="utf-8")
    new = server_setup.write_settings(paths, args)
    assert new["lead"] is False and new["peers"] == ["other-pc"]


def test_the_server_settings_take_peers_as_a_list_or_text(tmp_path):
    """server.json "peers" as a list or as comma-separated text (and --peers), "lead" and "beacon_port" typed."""
    import json
    from groundtruth.remote.server import settings
    f = tmp_path / "server.json"
    f.write_text(json.dumps({"peers": "other-pc, bigpc:9000", "lead": 1, "beacon_port": "8800"}), encoding="utf-8")
    got = settings(f)
    assert got["peers"] == ["other-pc", "bigpc:9000"] and got["lead"] is True and got["beacon_port"] == 8800
    assert settings(f, {"peers": "server-pc", "lead": False})["peers"] == ["server-pc"]


def test_the_history_tells_who_joined_left_came_back_or_restarted_and_who_leads(monkeypatch):
    """The cluster's history (the user: "show if an individual machine of the cluster disconnected at any time and
    how long it is connected"): B joins A, A takes the lead; B not heard for ALIVE_S: lost, with how long it was in
    the cluster, and A alone again; B back; B restarted (a new id from the same host and address) is one event, not
    a loss; the status has the size, each member's time in the cluster, and the events."""
    a, b = node("A"), node("B")
    t0 = 1_000_000.0
    monkeypatch.setattr(cl.time, "time", lambda: t0)
    assert hear(a, b, t0) == "peer"
    a.settle(t0)
    assert [e["event"] for e in a.events] == ["joined", "lead"] and a.status()["size"] == 2
    assert a.status()["members"][0]["joined_at"] == t0 and a.status()["together_since"] == t0
    later = t0 + 600 + cl.ALIVE_S + 1
    monkeypatch.setattr(cl.time, "time", lambda: later)
    a.settle(later)
    assert [e["event"] for e in a.events][-2:] == ["lost", "alone"] and "10 min" in a.events[-2]["text"]
    assert a.status()["size"] == 1 and a.status()["role"] == "standalone"
    assert hear(a, b, later) == "peer"
    assert a.events[-1]["event"] == "back"
    b2 = node("B")                                  # B restarted: a new id, the same host and address
    assert hear(a, b2, later + 1) == "peer"
    assert a.events[-1]["event"] == "restarted" and len(a.alive(later + 1)) == 1


def test_a_server_is_reached_on_an_address_it_announced_not_a_virtual_adapter_s():
    """The user's Find servers listed build-pc at 172.20.0.1, its Hyper-V/WSL vEthernet address (Windows sent the
    answer out of that adapter): every server announces its real addresses, and the receiver uses the one it shares
    a subnet with (the source only when it was announced, or nothing was), checked with a connection."""
    import socket
    from groundtruth.remote.cluster import address_candidates, reachable
    real = ["192.0.2.127"]
    assert address_candidates("172.20.0.1", real, ["192.0.2.140"])[0] == "192.0.2.127"
    assert address_candidates("172.20.0.1", real, ["10.1.1.5"]) == ["192.0.2.127", "172.20.0.1"]
    assert address_candidates("192.0.2.127", ["10.0.0.2", "192.0.2.127"], [])[0] == "192.0.2.127"
    assert address_candidates("10.0.0.7", None, ["10.0.0.5"]) == ["10.0.0.7"]           # an older server: its source
    # the beacon of a server sending from its virtual adapter: the peer table keeps the announced address
    a = node("A")
    b = node("B")
    b.info = lambda: {"cores": 16, "addresses": ["192.0.2.127"]}
    a._own = ["192.0.2.140"]
    assert a.accept(b.beacon(), "172.20.0.1") == "peer"
    p = a.alive()[0]
    assert p.address == "192.0.2.127" and p.source == "172.20.0.1"
    # reachable: the first candidate that accepts a connection
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    port = s.getsockname()[1]
    try:
        assert reachable(["127.0.0.2", "127.0.0.1"], port, timeout=0.3) in ("127.0.0.2", "127.0.0.1")
        assert reachable(["127.0.0.1"], port) == "127.0.0.1"
    finally:
        s.close()
    assert reachable(["127.0.0.1"], port, timeout=0.3) is None


def test_a_server_announces_its_real_addresses(tmp_path):
    """The beacon's info has the server's addresses on real networks (server.local_addresses), IPv4 only."""
    import socket as _s
    from groundtruth.remote.server import ComputeServer
    p = []
    for _ in range(1):
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        p.append(s.getsockname()[1])
        s.close()
    srv = ComputeServer(KEY, "127.0.0.1", 0, data_dir=tmp_path, beacon_port=p[0])
    srv.start()
    try:
        info = srv.cluster.info()
        assert isinstance(info["addresses"], list) and all(a.count(".") == 3 for a in info["addresses"])
        assert not any(a.startswith(("127.", "169.254.")) for a in info["addresses"])
    finally:
        srv.stop()
