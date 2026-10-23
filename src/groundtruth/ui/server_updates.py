"""The compute servers' versions against this plugin, and their remote updates, for Preferences (the user: "when the
groundtruth plugin is open and the user goes to preferences, it will show the user which server is out of date and
offers to update it through a button press"). No wx here: the tests run it; prefs_dialog.ServersPanel shows it.

The servers are the one in Preferences and those its status names: its cluster's members and the servers it hears
with another code version (a cluster leaves those out: they are the out-of-date ones). Each is asked for its own
status (its version, code, whether it takes remote updates, an update under way). An update hands it the server
package this plugin carries (remote/release.bundled: the same build as the plugin) and follows its status until it
runs this plugin's code, or says why not.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

POLL_S = 3.0                    # how often a server being updated is asked
UPDATE_WAIT_S = 30 * 60         # how long an update is followed (a busy server stops its running jobs first)


def _port(address: str, default: int = 8765) -> int:
    """The port of "host:port" (default without one)."""
    host, _, port = address.rpartition(":")
    return int(port) if host and port.isdigit() else default


def server_addresses(status: dict, address: str, found: list = ()) -> list[tuple[str, list[str]]]:
    """[(name, [addresses to try, best first])] of the servers to show: the configured one (as typed), its cluster's
    members and the servers of other versions it hears (the address the lead uses, then every address the server
    announces: a PC with two network cards may answer this PC on the other one), then servers a Find servers query
    heard (`found`, remote/cluster.find_servers) that its status does not name; each once (by name and port)."""
    out = [(status.get("host") or address, [address])]
    seen = {((status.get("host") or "").lower(), _port(address))}
    c = status.get("cluster") or {}
    entries = list(c.get("members") or []) + list(c.get("other_versions") or [])
    entries += [{**f, **(f.get("info") or {}), "address": f"{f.get('address')}:{f.get('port', 8765)}"}
                for f in found or () if f.get("address")]
    for m in entries:
        a = m.get("address")
        if not a:
            continue
        port = _port(a)
        key = ((m.get("host") or "").lower(), port)
        if key in seen:
            continue
        seen.add(key)
        tries = [a] + [f"{ip}:{port}" for ip in m.get("addresses") or [] if ip]
        out.append((m.get("host") or a, list(dict.fromkeys(tries))))
    return out


def compare_build(status: dict, mine_version: str, mine_code: str, target_version: str = "") -> tuple[str, str]:
    """release.compare, and an earlier build with the same code as "older" too: such a server takes this plugin's
    jobs, but the package this plugin carries (`target_version`, else the plugin's own build) has changes outside
    the code fingerprint (the server's own program, e.g. its queue positions). The user, with both servers listed
    "up to date" on 0.1.41500 next to a 0.1.41521 plugin: "should the server say outdated given that the plugin is
    newer?". (Here, not in remote/release.py: remote/ counts toward the code fingerprint.)"""
    from ..remote.release import compare, version_number
    state, text = compare(status, mine_version, mine_code)
    if state == "same":
        theirs = version_number(status.get("version"))
        target = version_number(target_version or mine_version)
        if theirs is not None and target is not None and theirs < target:
            return "older", "older build (takes this plugin's jobs; Update brings it up to date)"
    return state, text


def row_of(name: str, address: str, status: Optional[dict], error: str, mine_version: str, mine_code: str,
           target_version: str = "") -> dict:
    """One server's row: {name, address, version, state, text, can_update, update}; `state` one of same, older,
    newer, unknown (compare_build) or unreachable. `target_version`: the build of the server package this plugin
    carries (release.bundled), what Update installs."""
    if status is None:
        return {"name": name, "address": address, "version": "", "state": "unreachable",
                "text": f"can't be reached ({error})" if error else "can't be reached", "can_update": False,
                "update": None}
    state, text = compare_build(status, mine_version, mine_code, target_version)
    can = state == "older" and bool(status.get("remote_update"))
    if state == "older" and not status.get("remote_update"):
        text += " (remote updates off: update it at the server)"
    up = status.get("update") or {}
    if up.get("state") in ("waiting", "installing"):
        text = f"updating to {_short(up.get('version'))}: " + ("stopping its running job(s)"
                                                               if up["state"] == "waiting" else "installing")
        can = False
    return {"name": status.get("host") or name, "address": address, "version": _short(status.get("version")),
            "state": state, "text": text, "can_update": can, "update": up or None}


def _short(version: Optional[str]) -> str:
    """'GroundTruth 0.1.41452 (57602a8, built ...)' -> '0.1.41452 (57602a8)'."""
    text = str(version or "?")
    if text.startswith("GroundTruth "):
        text = text[len("GroundTruth "):]
    return text.split(", built")[0] + (")" if ", built" in text else "")


def check(address: str, key: str, remote=None, find=None, reach=None) -> list[dict]:
    """Every server's row (see the module); raises the configured server's error when it can't be asked (nothing
    else is known then). Each other server is asked at the first of its addresses that answers from this PC.
    `remote(address, key)`, `find(key)` (the network query) and `reach(hosts, port)` (the first host that accepts a
    connection): for tests."""
    from .. import build_version
    from ..remote import cluster
    from ..remote.client import Remote
    from ..remote.protocol import code_id
    from ..remote.release import bundled
    remote = remote or Remote
    find = find or cluster.find_servers
    reach = reach or cluster.reachable
    mine_version, mine_code = build_version(), code_id()
    target = (bundled() or {}).get("version") or ""          # what Update would install
    first = remote(address, key).status()
    try:
        heard = find(key)
    except Exception:                               # noqa: BLE001 - the query is a help, never a stop
        heard = []
    rows = []
    for name, tries in server_addresses(first, address, heard):
        if tries == [address]:
            rows.append(row_of(name, address, first, "", mine_version, mine_code, target))
            continue
        port = _port(tries[0])
        host = reach([t.rpartition(":")[0] for t in tries], port)
        addr = f"{host}:{port}" if host else tries[0]
        if host is None:
            st, err = None, "not at " + ", ".join(t.rpartition(":")[0] for t in tries)
        else:
            try:
                st, err = remote(addr, key).status(), ""
            except Exception as e:                  # noqa: BLE001 - one server down is shown on its row
                st, err = None, str(e)
        rows.append(row_of(name, addr, st, err, mine_version, mine_code, target))
    return rows


def update(address: str, key: str, bundle: dict, progress: Callable[[str], None], stop=None, remote=None,
           clock=time.monotonic, sleep=time.sleep) -> tuple[bool, str]:
    """Update one server with this plugin's server package (`bundle`: release.bundled), then follow it until it runs
    this plugin's code: (done, what happened). `progress(text)` is told each step; `stop` (a threading.Event) ends
    the following early (the dialog closed: the server carries on by itself). Done when the server runs the
    package's build (its code alone is not enough: a newer build may keep the code fingerprint)."""
    from ..remote.client import Remote
    from ..remote.protocol import code_id
    from ..remote.release import version_number
    remote = remote or Remote
    mine = code_id()
    target = version_number(bundle.get("version"))
    srv = remote(address, key)
    progress("sending the update…")
    try:
        # the server's record of its last update before this one: only a newer record is this update's outcome
        before = (srv.status().get("update") or {}).get("at")
        answer = srv.update(bundle["zip"].read_bytes(), bundle["sig"])
    except Exception as e:                          # noqa: BLE001 - refused or unreachable: said on its row
        return False, str(e).split(": ", 1)[-1] if ": " in str(e) else str(e)
    progress("stopping its running job(s)" if answer.get("state") == "waiting" else "installing…")
    t0 = clock()
    while clock() - t0 < UPDATE_WAIT_S:
        if stop is not None and stop.is_set():
            return False, "the update goes on at the server (not followed any more)"
        sleep(POLL_S)
        try:
            st = srv.status()
        except Exception:                           # noqa: BLE001 - restarting: not answering for a while
            progress("installing… (the server restarts)")
            continue
        theirs = version_number(st.get("version"))
        if (theirs >= target if target is not None and theirs is not None else
                target is None and st.get("code") == mine):
            return True, f"updated to {_short(st.get('version'))}"
        up = st.get("update") or {}
        if up.get("state") in ("failed", "rolled back") and up.get("at") != before:
            return False, f"the update {up['state']}: {up.get('error') or '?'}"
        if up.get("state") == "waiting":
            n = len(st.get("running") or [])
            progress(f"stopping {n} running job(s)" if n else "stopping its running job(s)")
    return False, "no answer from the updated server within 30 minutes: check it there (install_server status)"
