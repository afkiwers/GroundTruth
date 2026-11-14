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
        text = f"updating to {_short(up.get('version'))}: " + (_waiting_text(up, len(status.get("running") or []))
                                                               if up["state"] == "waiting" else "installing")
        can = False
    return {"name": status.get("host") or name, "address": address, "version": _short(status.get("version")),
            "state": state, "text": text, "can_update": can, "update": up or None,
            "running": len(status.get("running") or [])}


def _waiting_text(up: dict, n: int) -> str:
    """What a server with an update waiting is doing: finishing its jobs (mode "wait") or stopping them."""
    jobs = f"{n} running job(s)" if n else "its running job(s)"
    return f"stopping {jobs}" if up.get("mode") == "cancel" else f"after {jobs} have finished"


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
           clock=time.monotonic, sleep=time.sleep, busy: str = "wait") -> tuple[bool, str]:
    """Update one server with this plugin's server package (`bundle`: release.bundled), then follow it until it runs
    this plugin's code: (done, what happened). `progress(text)` is told each step; `stop` (a threading.Event) ends
    the following early (the dialog closed: the server carries on by itself). Done when the server runs the
    package's build (its code alone is not enough: a newer build may keep the code fingerprint). busy: the
    user's choice for a server with running jobs: "wait" (the update installs once they have finished; it is
    followed for UPDATE_WAIT_S, then goes on at the server) or "cancel" (they are stopped now)."""
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
        answer = srv.update(bundle["zip"].read_bytes(), bundle["sig"], busy)
    except Exception as e:                          # noqa: BLE001 - refused or unreachable: said on its row
        return False, str(e).split(": ", 1)[-1] if ": " in str(e) else str(e)
    progress(_waiting_text(answer, answer.get("running") or 0) if answer.get("state") == "waiting"
             else "installing…")
    t0 = clock()
    waiting = False                 # the last answer: the update waits for jobs the user chose to let finish
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
            progress(_waiting_text(up, len(st.get("running") or [])))
            waiting = up.get("mode") != "cancel"
    if waiting:
        return False, ("the update is waiting at the server for its running job(s): it installs by itself when "
                       "they have finished")
    return False, "no answer from the updated server within 30 minutes: check it there (install_server status)"


# ------------------------------------------------------------- a server's options (remote/options.py), from here
OPTION_WAIT_S = 75 * 60         # how long an install is followed (a 2.5 GB download; the server's own limit is 60)


PACKAGE_NAMES = {"torch": "PyTorch", "mkl": "Intel MKL (PARDISO sparse solver)"}


def package_label(package: str, version: str) -> str:
    """An installed option package for people: "PyTorch 2.6.0+cu126 (NVIDIA GPU, CUDA build cu126)" from its
    distribution name and version (PyTorch's local version label names its build: +cuNNN, +rocm..., +cpu)."""
    name = f"{PACKAGE_NAMES.get(package, package)} {version}"
    if package != "torch":
        return name
    local = version.partition("+")[2].lower()
    if local.startswith("cu"):
        return f"{name} (NVIDIA GPU, CUDA build {local})"
    if local.startswith("rocm"):
        return f"{name} (AMD Radeon GPU, ROCm build)"
    return f"{name} (CPU only: no GPU build)"


def installed_lines(status: dict) -> list[str]:
    """What the server has of the options' packages, one line each ("PyTorch ...: installed" / "...: not
    installed"); [] when its status says nothing about them (an older build)."""
    have = (status.get("options") or {}).get("installed") or {}
    return [f"{package_label(pkg, ver)}: installed" if ver else f"{PACKAGE_NAMES.get(pkg, pkg)}: not installed"
            for pkg, ver in sorted(have.items())]


def option_choices(status: dict) -> list[dict]:
    """What Options… offers for a server, from its status's "options": [{text, name, action, confirm}]. First a
    removal for every option package the server has (the user: "show what options have been installed already,
    and offer to uninstall them"), also one no option suits any more (a GPU build on a Mac); then the installs
    of the options that suit its machine: another GPU build is offered as the installed one's replacement, MKL
    once it is there is not offered again. A removal needs an option of that package to name in the request (the
    server takes option names only). [] for a server that offers nothing (an older build, one running in a
    window, or a machine none suits with nothing installed: no_options_reason says which)."""
    opt = status.get("options") or {}
    have = opt.get("installed") or {}
    host = status.get("host") or "the server"
    available = opt.get("available") or []
    removals, installs = [], []
    for pkg, version in sorted(have.items()):
        named = next((o for o in available if o.get("package") == pkg), None)
        if version and named is not None:
            removals.append({"text": f"Remove   {package_label(pkg, version)}   (installed)", "name": named["name"],
                             "action": "remove",
                             "confirm": f"Remove {package_label(pkg, version)} from {host}?\n\nIts jobs then run "
                                        "without it (on the CPU, or with the built-in sparse solver)."})
    for o in available:
        if o.get("unsuitable"):
            continue
        version = have.get(o["package"])
        if version and o["group"] != "gpu":                    # MKL there: nothing to install again
            continue
        there = f"; replaces the installed {o['package']} {version}" if version else ""
        installs.append({"text": f"Install  {o['label']}  ({o['about']}{there})", "name": o["name"],
                         "action": "install",
                         "confirm": f"Install on {host}:\n\n{o['label']}\n({o['about']})"
                                    "\n\nThe server downloads it from the package's own index and installs it once "
                                    "no job is running; queued jobs wait until it is done."})
    return removals + installs


def no_options_reason(status: dict) -> str:
    """Why option_choices offers nothing for this server, as the end of "<name> offers nothing to install from
    here: ...". Three cases: the server lists options but none suits its machine (a Mac: no GPU build is used
    there, and Intel MKL is x86 only), with its own reasons; it lists none while it answers with an options entry
    (it runs in a window, not as the installed service, which alone may install packages); or its status has no
    options entry at all (a build from before Options…)."""
    opt = status.get("options")
    if not isinstance(opt, dict):
        return "it runs an older version of GroundTruth, from before server options (update it first)"
    available = opt.get("available") or []
    if not available:
        return ("it runs in a window (groundtruth_server.cmd) rather than as the installed service, and only the "
                "service installs packages")
    reasons = []
    for o in available:
        why = o.get("unsuitable") or ""
        if why and why not in reasons:
            reasons.append(why)
    return "none of them suits its machine (" + "; ".join(reasons) + "). Nothing is missing: it runs as it is"


def follow_option(address: str, key: str, name: str, action: str, progress: Callable[[str], None], stop=None,
                  remote=None, clock=time.monotonic, sleep=time.sleep) -> tuple[bool, str]:
    """Ask a server to install or remove one of its options, then follow it: (done, what happened).
    `progress(text)` is told each step; `stop` ends the following early (the server carries on by itself)."""
    from ..remote.client import Remote
    remote = remote or Remote
    srv = remote(address, key)
    progress("asking the server…")
    try:
        answer = srv.option(name, action)
    except Exception as e:                          # noqa: BLE001 - refused or unreachable: said on its row
        return False, str(e).split(": ", 1)[-1] if ": " in str(e) else str(e)
    since = answer.get("since")
    verb = "installing" if action == "install" else "removing"
    t0 = clock()
    while clock() - t0 < OPTION_WAIT_S:
        if stop is not None and stop.is_set():
            return False, f"{verb} {name} goes on at the server (not followed any more)"
        sleep(POLL_S)
        try:
            cur = (srv.status().get("options") or {}).get("current") or {}
        except Exception:                           # noqa: BLE001 - busy with the install: ask again
            continue
        if cur.get("name") != name or cur.get("since") != since:
            continue
        if cur.get("state") == "waiting":
            progress(f"{name}: after the running job(s)")
        elif cur.get("state") == "installing":
            progress(f"{verb} {name}… ({int((clock() - t0) // 60)} min)")
        elif cur.get("state") == "done":
            return True, f"{name} {'installed' if action == 'install' else 'removed'}"
        elif cur.get("state") == "failed":
            return False, f"{name}: {cur.get('error') or 'failed'}"
    return False, f"{verb} {name}: no end within {OPTION_WAIT_S // 60} minutes; see the server's options.log"
