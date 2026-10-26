"""Preferences › Compute server › Test connection: what the server said, laid out to be read at a glance.

The user, of the old answer (one paragraph from remote.client.describe in a message box): "Format this better!".
Now a dialog: a headline (connected, or why not usable), the server as label / value rows (version, code, cores,
memory, GPU, jobs, updates), its cluster as a table (one line per server: role, address, cores, memory, GPU, time
in the cluster) and notes below (servers lost, other versions). Copy details puts the plain text on the clipboard.

`server_summary` turns the status (GET /v1/status) into that content without wx, so tests read it; describe() in
remote/client.py stays for the CLI. Kept in ui/: the code fingerprint the servers compare leaves ui/ out.
"""
from __future__ import annotations

from typing import Optional

import wx

from . import theme


LISTEN_S = 30.0                # a server up for less than this may not have heard the others' beacons yet


def _gb(v) -> str:
    """'24.0 GB', or '?'."""
    return f"{float(v):.1f} GB" if v is not None else "?"


def server_summary(st: dict, plugin_code: str, plugin_version: str) -> dict:
    """The dialog's content from a server's status: {ok, title, rows: [(label, value)], warnings: [str],
    cluster_title, cluster: [{role, host, address, cores, memory, gpu, since}], cluster_notes: [str]}."""
    from ..remote.cluster import span
    ok = st.get("code") == plugin_code
    host = st.get("host") or "?"
    running, queued = len(st.get("running") or []), len(st.get("queue") or [])
    gpus = ", ".join(st.get("gpus") or [])
    gpu = gpus or "none: the solves run on the CPU" + (f"\n{st['gpu_note']}" if st.get("gpu_note") else "")
    jobs = f"{st.get('workers', '?')} at a time · {running} running · {queued} queued"
    if st.get("results_waiting"):
        jobs += f" · {st['results_waiting']} result(s) waiting to be fetched"
    update = st.get("update") or {}
    updates = "on (signed packages from Preferences › Servers)" if st.get("remote_update") else \
        "off (reinstall by hand to update)"
    if update.get("state"):
        updates += f"\nlast: {update.get('state')}" + (f", {update['version']}" if update.get("version") else "")
    rows = [("Version", str(st.get("version", "?"))),
            ("Code", f"{st.get('code')}  " + ("(the same as this plugin's)" if ok else
                                              f"(this plugin: {plugin_code})")),
            ("Addresses", ", ".join(st.get("addresses") or []) or "?"),
            ("Processor", f"{st.get('cores', '?')} cores"),
            ("Memory", f"{_gb(st.get('memory_gb'))}, {_gb(st.get('free_gb'))} free"),
            ("GPU", gpu),
            ("Jobs", jobs),
            ("Results kept", f"{float(st.get('keep_hours') or 0):g} h"),
            ("Running for", span(float(st.get("uptime_s") or 0))),
            ("Remote updates", updates)]
    if st.get("python"):
        rows.insert(1, ("Python", str(st["python"])))
    warnings = [] if ok else [
        f"This server runs another GroundTruth ({st.get('version', '?')}); this plugin is {plugin_version}. It "
        "refuses this plugin's jobs until both have the same package (Preferences › Servers › Update)."]

    c = st.get("cluster") or {}
    members = c.get("members") or []
    now = c.get("now")
    if now is None:
        import time
        now = time.time()
    lead = c.get("lead") or {}
    cluster, notes = [], []
    if c.get("role") in (None, "standalone") and not members:
        up = float(st.get("uptime_s") or 0)
        if up < LISTEN_S:
            # just (re)started, e.g. by an update: the others' beacons (every 5 s, remote/cluster.py) not heard yet
            title = (f"No other server heard yet: this one started {span(up)} ago, and servers with the same key "
                     "find each other within about 10 s. Test connection again in a moment.")
        else:
            title = "Standalone: no other server with this key is heard"
    else:
        since = c.get("together_since")
        title = f"Cluster of {c.get('size', 1 + len(members))} servers" + \
            (f", together for {span(now - since)}" if since else "")
        me_lead = bool(lead.get("me"))
        cluster.append({"role": "lead" if me_lead else "member", "host": f"{host} (this one)",
                        "address": st.get("address") or ", ".join((st.get("addresses") or [])[:1]) or "",
                        "cores": str(st.get("cores", "")), "memory": _gb(st.get("memory_gb")),
                        "gpu": gpus or "none", "since": ""})
        if me_lead and c.get("forced_lead"):
            notes.append(f"{host} leads because it was started with --lead")
        for m in members:
            joined = m.get("joined_at")
            cluster.append({"role": "lead" if m.get("lead") else "member", "host": str(m.get("host", "?")),
                            "address": str(m.get("address", "")), "cores": str(m.get("cores", "")),
                            "memory": _gb(m.get("memory_gb")), "gpu": ", ".join(m.get("gpus") or []) or "none",
                            "since": span(now - joined) if joined else ""})
    for e in [e for e in c.get("events") or [] if e.get("event") == "lost"][-3:]:
        notes.append(f"{e.get('host')} was lost {span(now - e['at'])} ago" + (f" ({e['text']})" if e.get("text")
                                                                                 else ""))
    for m in c.get("other_versions") or []:
        warnings.append(f"{m.get('host')} has the key but another GroundTruth (code {m.get('code')}): not used "
                        "until it is updated")
    return {"ok": ok, "title": f"Connected to {host}" if ok else f"{host} answers, but can't take this plugin's jobs",
            "rows": rows, "warnings": warnings, "cluster_title": title, "cluster": cluster, "cluster_notes": notes}


def summary_text(s: dict) -> str:
    """The summary as plain text (Copy details)."""
    out = [s["title"], ""] + [f"{k}: {v}".replace("\n", "\n    ") for k, v in s["rows"]]
    out += [""] + [f"! {w}" for w in s["warnings"]] if s["warnings"] else []
    out += ["", s["cluster_title"]]
    out += [f"  {r['role']:<7} {r['host']}  {r['address']}  {r['cores']} cores, {r['memory']}, {r['gpu']}"
            + (f", in the cluster {r['since']}" if r["since"] else "") for r in s["cluster"]]
    out += [f"  {n}" for n in s["cluster_notes"]]
    return "\n".join(out)


CLUSTER_COLUMNS = (("Role", "role", 70), ("Server", "host", 210), ("Address", "address", 150),
                   ("Cores", "cores", 55), ("Memory", "memory", 75), ("GPU", "gpu", 230), ("In cluster", "since", 95))
WRAP_PX = 520                   # values wrap here (the GPU note on a Mac is a long sentence)


class ServerInfoDialog(wx.Dialog):
    """The Test connection answer (see the module)."""

    def __init__(self, parent: Optional[wx.Window], address: str, s: dict):
        """Lay out summary `s` (server_summary) of the server at `address`."""
        super().__init__(parent, title=f"Compute server {address}", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.summary = s
        pal = theme.palette()
        outer = wx.BoxSizer(wx.VERTICAL)

        head = wx.StaticText(self, label=("✔  " if s["ok"] else "⚠  ") + s["title"])
        font = head.GetFont()
        head.SetFont(font.Scaled(1.35).Bold())
        head.SetForegroundColour(theme.colour(pal.ok if s["ok"] else pal.warn))
        outer.Add(head, 0, wx.ALL, 14)

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=18)
        for label, value in s["rows"]:
            k = wx.StaticText(self, label=label, name="muted")
            v = wx.StaticText(self, label=value)
            v.Wrap(self.FromDIP(WRAP_PX))
            grid.Add(k, 0, wx.ALIGN_TOP)
            grid.Add(v, 0, wx.ALIGN_TOP)
        outer.Add(grid, 0, wx.LEFT | wx.RIGHT, 18)

        for w in s["warnings"]:
            t = wx.StaticText(self, label="⚠  " + w)
            t.SetForegroundColour(theme.colour(pal.warn))
            t.Wrap(self.FromDIP(WRAP_PX + 120))
            outer.Add(t, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)

        sub = wx.StaticText(self, label=s["cluster_title"])
        sub.SetFont(sub.GetFont().Bold())
        outer.Add(sub, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
        self.cluster = None
        if s["cluster"]:
            lst = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_THEME)
            for k, (name, _, width) in enumerate(CLUSTER_COLUMNS):
                lst.InsertColumn(k, name, width=self.FromDIP(width))
            for r in s["cluster"]:
                i = lst.InsertItem(lst.GetItemCount(), r["role"])
                for k, (_, key, _) in enumerate(CLUSTER_COLUMNS[1:], 1):
                    lst.SetItem(i, k, r[key])
            rows = len(s["cluster"])
            lst.SetMinSize(self.FromDIP(wx.Size(sum(w for *_, w in CLUSTER_COLUMNS) + 8, 32 + 22 * rows)))
            outer.Add(lst, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
            self.cluster = lst
        for n in s["cluster_notes"]:
            t = wx.StaticText(self, label=n, name="muted")
            t.Wrap(self.FromDIP(WRAP_PX + 120))
            outer.Add(t, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        copy = wx.Button(self, label="Copy details")
        copy.Bind(wx.EVT_BUTTON, self._copy)
        ok = wx.Button(self, wx.ID_OK, "OK")
        ok.SetDefault()
        buttons.Add(copy, 0, wx.RIGHT, 8)
        buttons.AddStretchSpacer()
        buttons.Add(ok)
        outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(outer)
        theme.style_tree(self)
        self.CentreOnParent()

    def _copy(self, _=None):
        """Copy details: the plain text to the clipboard."""
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(summary_text(self.summary)))
            finally:
                wx.TheClipboard.Close()


# --------------------------------------------------------------------------- Update / Update all, confirmed
UPDATE_COLUMNS = (("Server", "name", 220), ("Now", "now", 170), ("After the update", "after", 170))
UPDATE_NOTES = ("A busy server cancels its running jobs and updates at once: their owners see why and run them again "
                "on the new version. Queued jobs wait and run after the update.",
                "It restarts by itself, and goes back to its old version if the new one does not start.",
                "The servers are updated one after the other; the list shows how far each one is.")


def update_summary(targets: list, version: str) -> dict:
    """The confirmation's content: {title, rows: [{name, now, after}], built, notes}. `targets`: the Servers
    list's rows to update; `version`: the server package's build text (release.bundled)."""
    from .server_updates import _short
    after = _short(version)
    built = version.split(", built", 1)[1].rstrip(")").strip() if ", built" in version else ""
    n = len(targets)
    return {"title": (f"Update {targets[0]['name']} to {after}?" if n == 1 else
                      f"Update {n} compute servers to {after}?"),
            "rows": [{"name": r["name"], "now": r["version"], "after": after} for r in targets],
            "built": f"this plugin's version, built {built}" if built else "this plugin's version",
            "notes": list(UPDATE_NOTES)}


class UpdateConfirmDialog(wx.Dialog):
    """Preferences › Servers › Update / Update all: what will be updated, laid out as Test connection's answer (the
    user: "that update server dialog needs the same formatting as Test connection now has"). OK = Update."""

    def __init__(self, parent: Optional[wx.Window], s: dict):
        """Lay out summary `s` (update_summary)."""
        super().__init__(parent, title="Update compute servers", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        pal = theme.palette()
        outer = wx.BoxSizer(wx.VERTICAL)
        head = wx.StaticText(self, label=s["title"])
        head.SetFont(head.GetFont().Scaled(1.35).Bold())
        head.SetForegroundColour(theme.colour(pal.accent))
        head.SetMinSize(head.GetBestSize())          # the scaled font's width (Fit alone cut the end off)
        outer.Add(head, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
        sub = wx.StaticText(self, label=s["built"], name="muted")
        outer.Add(sub, 0, wx.LEFT | wx.RIGHT, 14)
        outer.AddSpacer(6)
        lst = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_THEME)
        for k, (name, _, width) in enumerate(UPDATE_COLUMNS):
            lst.InsertColumn(k, name, width=self.FromDIP(width))
        for r in s["rows"]:
            i = lst.InsertItem(lst.GetItemCount(), r["name"])
            lst.SetItem(i, 1, r["now"])
            lst.SetItem(i, 2, r["after"])
        lst.SetMinSize(self.FromDIP(wx.Size(sum(w for *_, w in UPDATE_COLUMNS) + 8, 32 + 22 * len(s["rows"]))))
        outer.Add(lst, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        self.list = lst
        for n in s["notes"]:
            t = wx.StaticText(self, label="•  " + n, name="muted")
            t.Wrap(self.FromDIP(sum(w for *_, w in UPDATE_COLUMNS)))
            outer.Add(t, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        buttons = wx.StdDialogButtonSizer()
        ok = self.ok = wx.Button(self, wx.ID_OK, "Update" if len(s["rows"]) == 1 else "Update all")
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, "Cancel"))
        buttons.Realize()
        outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(outer)
        theme.style_tree(self)
        self.CentreOnParent()


def confirm_update(parent: Optional[wx.Window], targets: list, version: str) -> bool:
    """Ask before updating `targets` (UpdateConfirmDialog): True for Update."""
    dlg = UpdateConfirmDialog(parent, update_summary(targets, version))
    try:
        return dlg.ShowModal() == wx.ID_OK
    finally:
        dlg.Destroy()
