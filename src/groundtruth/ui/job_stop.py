"""Stopping a server job from the Server jobs tab (Cancel job, Remove from list), without wx so tests run it.

Kept in ui/ on purpose: remote/ counts toward the code fingerprint the servers compare with the plugin's
(protocol.code_id skips ui/), and this is only the window's bookkeeping, so changing it must not make every server
need reinstalling.
"""
from __future__ import annotations

from typing import Optional

from ..remote.jobs import LocalJobs


def stop_on_server(jobs: LocalJobs, server, jid: str, remove: bool = False) -> Optional[str]:
    """Cancel job `jid` on its server (`server`: a client.Remote) and make it a past job, cancelled; with `remove`
    take it off the list instead. None when done; else why the server could not be asked (unreachable: the job is
    left as it was, and the caller may offer remove_anyway)."""
    try:
        server.forget(jid)                  # a server that no longer knows the job answers too: nothing to stop
    except Exception as e:                  # noqa: BLE001 - unreachable, or refused: nothing changed there
        return str(e) or type(e).__name__
    if remove:
        jobs.remove(jid)
    else:
        jobs.finish(jid, "cancelled", message="cancelled by you")
    return None


def remove_anyway(jobs: LocalJobs, jid: str):
    """Take a job off the list although its server could not be told (the user's choice: a job on a server gone
    for 12 hours stayed in Current jobs, Remove from list only tried to cancel it there). Should the server come
    back, it finishes the job and keeps its result for keep_hours; nobody fetches it."""
    jobs.remove(jid)
