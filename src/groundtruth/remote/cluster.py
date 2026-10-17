"""Compute servers finding each other on the local network, and electing a lead (the user, 2026-10-14: "the server
should scan the local network for others and whoever is first to find another should become lead. Also, an
optional option on startup should define a server as lead if needed").

**Beacons.** Every server sends a small UDP datagram every BEACON_S to the network's broadcast address (and to the
addresses listed in server.json "peers", for servers on another subnet), port BEACON_PORT: its id (random per
start), host name, HTTP port, code version, cores, memory, GPUs and its lead claim. A beacon is signed with the
access key (HMAC-SHA256 over its JSON, as protocol.py signs requests): only servers sharing the key form a cluster,
a forged or foreign beacon is dropped, and the key itself never travels. A beacon older than MAX_SKEW_S is dropped
too. A server not heard for ALIVE_S is gone.

**The lead.** A server that hears another one and no lead claim among them claims the lead (its claim time goes
into its beacons: "whoever is first to find another"). Competing claims are settled the same way on every server:
a forced claim (`groundtruth serve --lead`, server.json "lead": true) beats an automatic one, then the earliest
claim, then the host name. A server that loses gives its claim up. When the lead is gone the others elect again.
Alone, a server is standalone (no lead).

**Find servers.** The plugin sends a query (signed with its access key) to the broadcast address; every server
with that key answers it with its beacon, unicast (a reply to the plugin's own datagram, which the Windows
firewall lets in without a rule).

**What a beacon does not prove.** A peer's address is the datagram's source, which the signature does not cover: a
signed beacon heard on the network could be sent again from another address within MAX_SKEW_S. The table is only
for showing and electing; before a server sends a peer any job (a board), it must check over HTTP that the peer
holds the key (a signed challenge it answers), never trust the table's address alone.

The network part is a thin layer (`_send`, the receive loop); the rules (`accept`, `elect`) are plain functions of
the table, so the tests drive them without sockets.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

BEACON_PORT = 8766              # UDP; the HTTP port stays 8765
BEACON_S = 5.0                  # how often a server announces itself
ALIVE_S = 20.0                  # a server not heard for this long is gone (the lead too: a new election)
MAX_SKEW_S = 120.0              # a beacon's time may differ from ours by this much (else dropped: replay)
FIND_S = 2.0                    # Find servers: how long the plugin listens for answers
EVENTS_KEPT = 50                # the cluster's history in the status: its last joins, losses, returns, lead changes
log = logging.getLogger("groundtruth.server")


def span(seconds: float) -> str:
    """A duration in words: '45 s', '12 min', '2 h 13 min', '3 d 4 h'."""
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"
VERSION = 1


def sign(key: str, msg: dict) -> dict:
    """`msg` with its "sig": HMAC-SHA256 of its JSON (sorted keys, without "sig") with the access key."""
    body = json.dumps({k: v for k, v in msg.items() if k != "sig"}, sort_keys=True, separators=(",", ":"))
    return {**msg, "sig": hmac.new(key.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()}


def verify(key: str, msg: dict, now: Optional[float] = None) -> bool:
    """`msg` is signed with `key` and recent (within MAX_SKEW_S)."""
    sig = msg.get("sig")
    if not isinstance(sig, str):
        return False
    if not hmac.compare_digest(sign(key, msg)["sig"], sig):
        return False
    t = msg.get("time")
    return isinstance(t, (int, float)) and abs((time.time() if now is None else now) - t) <= MAX_SKEW_S


@dataclass
class Peer:
    """Another server of the cluster, as its last beacon described it."""
    id: str
    host: str
    address: str                # its IP to use: one it announced (address_candidates), else the datagram's source
    port: int
    code: str
    seen: float
    info: dict = field(default_factory=dict)        # cores, memory_gb, free_gb, gpus, version
    claim: Optional[float] = None                   # its lead claim time
    forced: bool = False                            # its claim is forced (--lead)
    source: str = ""                                # the IP its datagram came from (may be a virtual adapter's)

    def as_dict(self) -> dict:
        """For the status."""
        return {"id": self.id, "host": self.host, "address": f"{self.address}:{self.port}", "code": self.code,
                "seen_s": round(time.time() - self.seen, 1), "lead_claim": self.claim, "forced": self.forced,
                **self.info}


class Cluster:
    """This server's view of the cluster: the peer table, its own claim, the elected lead; beacons sent and received
    in background threads (start / stop). `info()` gives this server's figures for its beacons."""

    def __init__(self, key: str, http_port: int, code: str, info: Callable[[], dict], forced_lead: bool = False,
                 static_peers: tuple = (), beacon_port: int = BEACON_PORT, host: Optional[str] = None,
                 address: str = ""):
        """Not started yet. `address`: the one this server announces (server.json "address"), its subnet's broadcast
        sent from it."""
        self.key, self.http_port, self.code, self.info = key, http_port, code, info
        self.address = address
        self.beacon_port = beacon_port
        self.host = host or socket.gethostname()
        self.id = secrets.token_hex(6)
        self.started = time.time()
        self.forced = bool(forced_lead)
        self.claim: Optional[float] = self.started if self.forced else None
        self.static = [p for p in static_peers if p]
        self.peers: dict[str, Peer] = {}
        self.foreign: dict[str, dict] = {}          # servers with our key but another code version (status only)
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []
        # the cluster's history, for the status (the user: "show if an individual machine of the cluster disconnected
        # at any time and how long it is connected"): per server (host@address, which survives its restart, unlike
        # its id) when its current connection began and its id; when this server last found company; the lead
        # and since when; the events, newest last (also logged)
        self.joined: dict[str, float] = {}
        self.ids: dict[str, str] = {}
        self.ever: set[str] = set()
        self.together_since: Optional[float] = None
        self.lead_since: Optional[float] = None
        self.events: list[dict] = []

    def own(self) -> list[str]:
        """This server's own network addresses (cached: they rarely change while it runs)."""
        if getattr(self, "_own", None) is None:
            self._own = own_addresses()
        return self._own

    @staticmethod
    def who(p: "Peer") -> str:
        """A server's lasting name in the history: host@address (its id changes at every start)."""
        return f"{p.host}@{p.address}"

    def _event(self, now: float, kind: str, host: str, address: str = "", text: str = ""):
        """(under the lock) Note an event in the history and the server's log."""
        self.events.append({"at": now, "event": kind, "host": host, "address": address, "text": text})
        del self.events[:-EVENTS_KEPT]
        log.info("cluster: %s %s%s%s", host, kind, f" ({address})" if address else "", f": {text}" if text else "")

    # ------------------------------------------------------------------ rules (no sockets)
    def beacon(self, kind: str = "beacon", reply_to: str = "") -> dict:
        """This server's signed beacon (or its answer to a Find servers query)."""
        msg = {"v": VERSION, "kind": kind, "id": self.id, "host": self.host, "port": self.http_port,
               "code": self.code, "time": time.time(), "claim": self.claim, "forced": self.forced,
               "lead": (self.lead() or {}).get("id"), "info": self.info()}
        if reply_to:
            msg["reply_to"] = reply_to
        return sign(self.key, msg)

    def accept(self, msg: dict, address: str, now: Optional[float] = None) -> Optional[str]:
        """Take a received beacon into the table: "peer" (same key and code), "foreign" (same key, another code
        version: shown, never used), None (not ours: unsigned, wrong key, too old, our own, malformed)."""
        now = time.time() if now is None else now
        if not isinstance(msg, dict) or msg.get("v") != VERSION or msg.get("kind") not in ("beacon", "answer") \
                or not verify(self.key, msg, now) or msg.get("id") == self.id:
            return None
        try:
            info = dict(msg.get("info") or {})
            peer = Peer(str(msg["id"]), str(msg["host"]), address_candidates(address, info.get("addresses"),
                                                                             self.own())[0],
                        int(msg["port"]), str(msg["code"]), now, info, msg.get("claim"), bool(msg.get("forced")),
                        address)
        except (KeyError, TypeError, ValueError):
            return None
        with self.lock:
            if peer.code != self.code:
                self.foreign[peer.id] = peer.as_dict()
                return "foreign"
            w = self.who(peer)
            if w not in self.joined:
                # a server not in the cluster a moment ago: new, or back after being lost
                self.joined[w] = now
                self._event(now, "back" if w in self.ever else "joined", peer.host, peer.address,
                            f"{peer.info.get('cores', '?')} cores, {peer.info.get('memory_gb', '?')} GB")
                self.ever.add(w)
                if self.together_since is None:
                    self.together_since = now
            elif self.ids.get(w) not in (None, peer.id):
                # the same server with a new id: it restarted within ALIVE_S (its old id is dropped below)
                self.joined[w] = now
                self._event(now, "restarted", peer.host, peer.address)
                self.peers.pop(self.ids[w], None)
            self.ids[w] = peer.id
            self.peers[peer.id] = peer
        return "peer"

    def alive(self, now: Optional[float] = None) -> list[Peer]:
        """The peers heard within ALIVE_S (the others are dropped)."""
        now = time.time() if now is None else now
        with self.lock:
            for p in [p for p in self.peers.values() if now - p.seen > ALIVE_S]:
                self.peers.pop(p.id, None)
                w = self.who(p)
                if self.ids.get(w) == p.id:
                    since = self.joined.pop(w, None)
                    self.ids.pop(w, None)
                    self._event(now, "lost", p.host, p.address,
                                f"not heard for {ALIVE_S:.0f} s" + (f", it was in the cluster {span(now - since)}"
                                                                    if since else ""))
            if not self.peers:
                self.together_since = None
            return list(self.peers.values())

    def elect(self, now: Optional[float] = None) -> Optional[dict]:
        """Settle the lead (see the module) and return it ({id, host, address, port, me}), or None standalone. Called
        after every beacon round and every received beacon."""
        peers = self.alive(now)
        if not peers:
            if not self.forced:
                self.claim = None               # alone again: standalone, no lead
            return None
        if self.claim is None and not any(p.claim is not None for p in peers):
            self.claim = time.time() if now is None else now     # the first to find another claims the lead

        def rank(forced: bool, claim: float, host: str, pid: str) -> tuple:
            """Forced first, then the earliest claim, then the host name and id: the same order everywhere."""
            return (not forced, claim, host.lower(), pid)
        candidates = [(rank(p.forced, p.claim, p.host, p.id), p) for p in peers if p.claim is not None]
        if self.claim is not None:
            candidates.append((rank(self.forced, self.claim, self.host, self.id), None))
        best = min(candidates, key=lambda c: c[0])[1]
        if best is not None and not self.forced:
            self.claim = None                   # another server leads: this one gives its claim up
        return self._as_lead(best)

    def _as_lead(self, p: Optional[Peer]) -> dict:
        """The lead as a dict (None: this server)."""
        if p is None:
            return {"id": self.id, "host": self.host, "address": "", "port": self.http_port, "me": True}
        return {"id": p.id, "host": p.host, "address": p.address, "port": p.port, "me": False}

    def lead(self) -> Optional[dict]:
        """The lead as last elected (None: standalone)."""
        return getattr(self, "_lead", None)

    def settle(self, now: Optional[float] = None) -> Optional[dict]:
        """Elect, and note a new lead (or the end of the cluster) in the history."""
        now = time.time() if now is None else now
        new, old = self.elect(now), self.lead()
        if (new or {}).get("id") != (old or {}).get("id"):
            with self.lock:
                self.lead_since = now if new else None
                if new is None:
                    self._event(now, "alone", self.host, "", "no other server heard: standalone")
                else:
                    how = "set with --lead" if new["me"] and self.forced else \
                        "forced (--lead)" if not new["me"] and any(p.forced for p in self.peers.values()
                                                                   if p.id == new["id"]) else "first to find another"
                    self._event(now, "lead", new["host"] + (" (this server)" if new["me"] else ""),
                                new.get("address", ""), how)
        self._lead = new
        return new

    def status(self) -> dict:
        """For /v1/status: the role, the lead and the members."""
        lead = self.lead()
        role = "standalone" if lead is None else "lead" if lead["me"] else "member"
        now = time.time()
        with self.lock:
            members = [{**p.as_dict(), "joined_at": self.joined.get(self.who(p)), "lead": bool(lead) and
                        lead["id"] == p.id} for p in sorted(self.peers.values(), key=lambda p: p.host.lower())]
            foreign = list(self.foreign.values())
            events = [dict(e) for e in self.events]
        me = {"host": self.host, "id": self.id, "started_at": self.started, "lead": bool(lead) and lead["me"],
              **self.info()}
        return {"role": role, "lead": lead, "members": members, "other_versions": foreign, "me": me,
                "size": 1 + len(members), "together_since": self.together_since, "lead_since": self.lead_since,
                "static_peers": list(self.static), "events": events, "now": now,
                "forced_lead": self.forced, "beacon_port": self.beacon_port}

    # ------------------------------------------------------------------ network
    def start(self):
        """Open the UDP socket (broadcast, any address) and start sending and receiving."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: no other process may bind the port too and take our datagrams (SO_REUSEADDR would allow it)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("", self.beacon_port))
        s.settimeout(1.0)
        self._sock = s
        self._threads = [threading.Thread(target=self._send_loop, name="gt-cluster-send", daemon=True),
                         threading.Thread(target=self._recv_loop, name="gt-cluster-recv", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self):
        """Stop announcing (the others drop this server after ALIVE_S)."""
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def targets(self) -> list[tuple[str, int]]:
        """Where beacons go: the broadcast address, the broadcast of each own address's /24 subnet, and the static
        peers (server.json "peers": "host", or "host:port" with the peer's BEACON port when it is not the default).
        Windows sends 255.255.255.255 out of one network card only: on a PC with two (the user's andre, 192.168.0.6
        and another network) the others' subnet may never hear it; a subnet's own broadcast address (192.168.0.255)
        is routed out of the card on that subnet."""
        out = [("255.255.255.255", self.beacon_port)]
        own = [self.address] if self.address else self.own()
        out += [(b, self.beacon_port) for b in subnet_broadcasts(own)]
        for p in self.static:
            host, _, port = p.partition(":")
            out.append((host, int(port) if port.isdigit() else BEACON_PORT))
        return out

    def _send(self, msg: dict, target: tuple[str, int]):
        """One datagram (a failure only means this round's beacon did not go out)."""
        try:
            self._sock.sendto(json.dumps(msg).encode("utf-8"), target)
        except OSError:
            pass

    def _send_loop(self):
        """Announce every BEACON_S; settle the lead after each round."""
        while not self._stop.is_set():
            msg = self.beacon()
            for t in self.targets():
                self._send(msg, t)
            self.settle()
            self._stop.wait(BEACON_S)

    def _recv_loop(self):
        """Take beacons in; answer Find servers queries signed with our key."""
        while not self._stop.is_set():
            try:
                data, (addr, port) = self._sock.recvfrom(65536)
                msg = json.loads(data.decode("utf-8"))
            except socket.timeout:
                continue
            except (OSError, ValueError):
                if self._stop.is_set():
                    return
                continue
            if isinstance(msg, dict) and msg.get("kind") == "find":
                if msg.get("v") == VERSION and verify(self.key, msg):
                    self._send(self.beacon("answer", reply_to=str(msg.get("nonce", ""))), (addr, port))
                continue
            if self.accept(msg, addr) == "peer":
                self.settle()


def own_addresses() -> list[str]:
    """This PC's IPv4 addresses on real networks (server.local_addresses without the name)."""
    from .server import local_addresses
    return [a for a in local_addresses()[1:] if a.count(".") == 3]


def subnet_broadcasts(addresses: list[str]) -> list[str]:
    """The broadcast address of each of these own IPv4 addresses' networks, from its network card's netmask (psutil;
    /24 when it can't be read): 192.168.0.6 on 255.255.255.0 -> 192.168.0.255."""
    import ipaddress
    masks = {}
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and a.netmask:
                    masks[a.address] = a.netmask
    except Exception:                               # noqa: BLE001 - no psutil: /24
        pass
    out = []
    for a in addresses:
        if a.count(".") != 3:
            continue
        try:
            net = ipaddress.IPv4Network(f"{a}/{masks.get(a, '255.255.255.0')}", strict=False)
        except ValueError:
            continue
        if net.prefixlen < 31:                      # a /31 or /32 has no broadcast
            out.append(str(net.broadcast_address))
    return list(dict.fromkeys(out))


def adapters() -> list[tuple[str, str]]:
    """[(network card's name, IPv4 address)] of this PC's real networks (as server.local_addresses picks them), for
    choosing the address a server announces (install --address)."""
    from .server import VIRTUAL_ADAPTERS
    out = []
    try:
        import psutil
        stats = psutil.net_if_stats()
        for nic, addrs in psutil.net_if_addrs().items():
            if not (stats.get(nic) and stats[nic].isup) or any(v in nic.lower() for v in VIRTUAL_ADAPTERS):
                continue
            out += [(nic, a.address) for a in addrs if a.family == socket.AF_INET
                    and not a.address.startswith(("127.", "169.254."))]
    except Exception:                               # noqa: BLE001 - no psutil: the addresses alone
        out = [("", a) for a in own_addresses()]
    return out


def same_subnet(a: str, b: str) -> bool:
    """Whether two IPv4 addresses share their first three numbers (a /24 office network: a good guess, the
    connection check in `reachable` has the last word)."""
    return a.count(".") == 3 and b.count(".") == 3 and a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


def address_candidates(source: str, announced, own=()) -> list[str]:
    """A server's addresses to try, best first: the datagram's source when the server announced it; then the
    addresses it announced that share a subnet with one of ours (`own`); then its other announced ones; the
    source last. The user, of Find servers listing andre at 172.20.192.1: Windows may send a server's datagram out
    of a virtual adapter (Hyper-V/WSL vEthernet) nobody else can reach, so the source alone can't be trusted;
    every server announces its real addresses (server.local_addresses) with its beacon."""
    announced = [a for a in (announced or []) if isinstance(a, str) and a]
    own = list(own or ())
    out = []
    if source in announced or source.startswith("127."):
        out.append(source)                  # announced, or this very machine (servers sharing a PC)
    out += [a for a in announced if any(same_subnet(a, o) for o in own)]
    if any(same_subnet(source, o) for o in own):
        out.append(source)                  # on our own subnet: it arrived from there
    out += announced
    out.append(source)
    return list(dict.fromkeys(a for a in out if a))


def reachable(candidates: list[str], port: int, timeout: float = 0.5) -> Optional[str]:
    """The first address of `candidates` that accepts a TCP connection on `port` (the compute server's), or None."""
    for a in candidates:
        try:
            with socket.create_connection((a, int(port)), timeout=timeout):
                return a
        except OSError:
            continue
    return None


def find_servers(key: str, timeout: float = FIND_S, beacon_port: int = BEACON_PORT,
                 targets: tuple = ("255.255.255.255",)) -> list[dict]:
    """The plugin's Find servers: ask the network, collect the answers signed with `key` for `timeout` seconds:
    [{host, address, port, code, lead (the id it follows), id, info}], the lead first, then by host name."""
    nonce = secrets.token_hex(8)
    query = sign(key, {"v": VERSION, "kind": "find", "time": time.time(), "nonce": nonce})
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.2)
    found: dict[str, dict] = {}
    try:
        for t in targets:
            try:
                s.sendto(json.dumps(query).encode("utf-8"), (t, beacon_port))
            except OSError:
                pass
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, (addr, _) = s.recvfrom(65536)
                msg = json.loads(data.decode("utf-8"))
            except socket.timeout:
                continue
            except (OSError, ValueError):
                continue
            if isinstance(msg, dict) and msg.get("kind") == "answer" and msg.get("reply_to") == nonce \
                    and verify(key, msg):
                found[msg["id"]] = {"id": msg["id"], "host": msg.get("host"), "address": addr,
                                    "port": msg.get("port"), "code": msg.get("code"), "lead": msg.get("lead"),
                                    "info": msg.get("info") or {}}
    finally:
        s.close()
    servers = list(found.values())
    return sorted(servers, key=lambda m: (m["lead"] != m["id"], str(m["host"]).lower()))
