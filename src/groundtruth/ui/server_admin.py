"""Server administration: one window for everything guarded by this computer's administration password.

The user (2026-11-20): "put everything in one new window maybe combined with the installation so all password
protected stuff is contained in there". Preferences › Compute server › Server administration… asks for the password
once (or, with none set yet, for a new one), then opens this window with:
  * the servers (the configured one and its cluster) with their versions against this plugin's: Check, Update,
    Update all (signed remote updates, remote/release.py) and Options… (optional packages, remote/options.py);
  * how long the selected server keeps a finished result for its owner (POST /v1/settings; the user: "keeping the
    results for a settable amount of hours like 120hrs", after an 8x result expired over a weekend);
  * Clear past jobs (this computer's list of the Server jobs tab);
  * Change password.
The password is jobs_password's (a salted PBKDF2 hash in ui.json; checked and made off the GUI thread). It guards
against changes made by accident or in passing, not against someone using this Windows account; the access key
still guards the servers themselves.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

import wx

from . import theme
from .messages import box as message_box   # laid-out message boxes (ui/messages.py)

TITLE = "GroundTruth: server administration"
GAP = 6                  # spacing unit (DIP), as Preferences
KEEP_MIN_H, KEEP_MAX_H = 1, 720     # what the server takes (remote/server.py KEEP_MIN_H / KEEP_MAX_H)


def off_thread(window, work: Callable, done: Callable):
    """Run work() in a thread (a password hash takes ~0.5 s: never on the GUI thread), then done(its result) on the
    GUI thread, unless `window` is gone by then."""

    def run():
        """(thread) The work, then back to the GUI thread."""
        out = work()
        wx.CallAfter(lambda: done(out) if window else None)
    threading.Thread(target=run, name="gt-password", daemon=True).start()


def ask_password(parent, message: str) -> Optional[str]:
    """A masked password entry, None when cancelled."""
    with wx.PasswordEntryDialog(parent, message, TITLE) as dlg:
        return dlg.GetValue() if dlg.ShowModal() == wx.ID_OK else None


def new_password(parent, done: Callable[[bool], None], first: bool):
    """Ask for a new administration password twice, hash it off the GUI thread and save it; done(True) once saved,
    done(False) when cancelled or refused (with a message). `first`: none is set yet (the intro says what it is
    for)."""
    from . import jobs_password as jp
    intro = ("Server administration needs a password of this computer first. It guards the server updates and "
             "options, how long the servers keep results, and clearing past jobs.\n\n") if first else ""
    pw = ask_password(parent, f"{intro}New administration password (at least {jp.MIN_LENGTH} characters):")
    if pw is None:
        done(False)
        return
    again = ask_password(parent, "The new administration password again:")
    if again is None:
        done(False)
        return
    why = jp.problem(pw, again)
    if why:
        message_box(f"Not set: {why}.", TITLE, wx.ICON_WARNING)
        done(False)
        return

    def saved(made):
        """(GUI thread) The hash is made: store it."""
        theme.save_prefs(**{jp.PREF_KEY: made})
        message_box("The administration password is set.", TITLE, wx.ICON_INFORMATION)
        done(True)
    off_thread(parent, lambda: jp.make_record(pw), saved)


def open_admin(parent, settings: Callable[[], tuple], past_jobs=None, opened: Optional[Callable] = None):
    """Preferences › Server administration…: the password (or a new one when none is set), checked off the GUI
    thread, then the window (modal). `settings()`: the server address and access key as typed in Preferences now;
    `past_jobs`: the main window's (count(), clear()), None without one. `opened(dialog)`: called with the window
    once it shows (tests)."""
    from . import jobs_password as jp
    rec = jp.record(theme.prefs())

    def show(ok: bool):
        """(GUI thread) The password is right (or new): the window."""
        if not ok or not parent:
            return
        with AdminDialog(parent, settings, past_jobs) as dlg:
            if opened is not None:
                wx.CallAfter(opened, dlg)
            dlg.ShowModal()
    if rec is None:
        new_password(parent, show, first=True)
        return
    pw = ask_password(parent, "Administration password of this computer:")
    if pw is None:
        return

    def checked(ok: bool):
        """(GUI thread) Wrong: say so; right: the window."""
        if not ok:
            message_box("Wrong password.", TITLE, wx.ICON_WARNING)
            return
        show(True)
    off_thread(parent, lambda: jp.matches(rec, pw), checked)


class AdminDialog(wx.Dialog):
    """The window (see the module): the servers, how long they keep results, past jobs, the password."""

    def __init__(self, parent, settings: Callable[[], tuple], past_jobs=None):
        """`settings()`: (address, key) from Preferences; `past_jobs`: (count(), clear()) or None."""
        super().__init__(parent, title=TITLE, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        theme.set_icon(self)
        self.past_jobs = past_jobs
        gap = self.FromDIP(GAP)
        s = wx.BoxSizer(wx.VERTICAL)

        def heading(text: str, note: str):
            """A group's bold title and its muted line."""
            t = wx.StaticText(self, label=text)
            t.SetFont(t.GetFont().Bold())
            s.Add(t, 0, wx.LEFT | wx.RIGHT | wx.TOP, 2 * gap)
            n = wx.StaticText(self, label=note)
            n.SetName("muted")
            n.Wrap(self.FromDIP(620))
            s.Add(n, 0, wx.LEFT | wx.RIGHT | wx.TOP, gap)

        heading("Compute servers",
                "The server set in Preferences and the servers of its cluster, each with its version against this "
                "plugin's. Update installs this plugin's version on an out-of-date server that takes remote updates; "
                "Options… installs or removes its optional packages (a GPU build of PyTorch, the MKL sparse solver); "
                "Keep results sets how long it keeps a finished result for its owner.")
        address, key = settings()
        self.servers = ServersPanel(self, settings)
        s.Add(self.servers, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 2 * gap)

        heading("Past jobs",
                "Removes the Server jobs tab's past jobs (shown, failed, cancelled, no longer on the server) from this "
                "computer's list, never a job still queued or running, nor anything on a server.")
        self.clear_past = wx.Button(self, label="Clear past jobs…")
        self.clear_past.Bind(wx.EVT_BUTTON, lambda e: self.on_clear_past())
        self.clear_past.Enable(past_jobs is not None)
        s.Add(self.clear_past, 0, wx.LEFT | wx.TOP, 2 * gap)

        heading("Password",
                "This window's password, kept in this computer's ui.json as a salted hash only. It guards against "
                "changes made by accident or in passing, not against someone using your Windows account. It also "
                "shows the access key in Preferences where Windows Hello is not available.")
        self.change_password = wx.Button(self, label="Change password…")
        self.change_password.Bind(wx.EVT_BUTTON, lambda e: self.on_change_password())
        s.Add(self.change_password, 0, wx.LEFT | wx.TOP, 2 * gap)

        s.Add(self.CreateSeparatedButtonSizer(wx.CLOSE), 0, wx.EXPAND | wx.ALL, 2 * gap)
        self.SetSizer(s)
        self.SetMinSize(self.FromDIP(wx.Size(660, 520)))
        self.Fit()
        self.CentreOnParent()
        self.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
        if address and key:
            wx.CallAfter(lambda: self and self.servers.start_check())      # once the window shows

    def on_change_password(self):
        """Change password…: the new one twice (the window was opened with the current one)."""
        self.change_password.Disable()
        new_password(self, lambda ok: self and self.change_password.Enable(), first=False)

    def on_clear_past(self):
        """Clear past jobs…: a Yes/No naming how many, then the main window's clear()."""
        if self.past_jobs is None:
            return
        count, clear = self.past_jobs
        n = count()
        if not n:
            message_box("There are no past jobs to clear.", TITLE, wx.ICON_INFORMATION)
            return
        if message_box(f"Remove the {n} past job(s) from this computer's list? Jobs still queued or running and "
                       "anything on the servers stay.", TITLE, wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return
        done = clear()
        message_box(f"{done} past job(s) cleared.", TITLE, wx.ICON_INFORMATION)


class ServersPanel(wx.Panel):
    """The servers: the configured server and the servers its cluster names, each with its version against this
    plugin's (ui/server_updates.py), Update / Update all for the out-of-date ones that take signed remote updates
    (remote/release.py), Options… for the selected one's optional packages, and how long the selected one keeps
    results. Every request runs in a background thread; following an update ends when the window closes (the
    server carries on by itself)."""

    COLS = (("Server", 130), ("Version", 140), ("Keeps results", 90), ("State", 300))

    def __init__(self, parent, settings):
        """`settings()`: (the server address, the access key) as typed in Preferences."""
        from ..remote.release import bundled
        super().__init__(parent)
        self.settings = settings
        self.rows: list[dict] = []
        self.busy: set[str] = set()                 # addresses being updated
        self.stop = threading.Event()
        self.bundle = bundled()
        gap = self.FromDIP(GAP)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
                                size=self.FromDIP(wx.Size(sum(w for _, w in self.COLS) + 10, 120)))
        for i, (title, w) in enumerate(self.COLS):
            self.list.InsertColumn(i, title, width=self.FromDIP(w))
        self.check_b = wx.Button(self, label="Check")
        self.check_b.SetToolTip("ask every server for its version")
        self.update_b = wx.Button(self, label="Update")
        self.update_b.SetToolTip("update the selected server to this plugin's version")
        self.all_b = wx.Button(self, label="Update all")
        self.all_b.SetToolTip("update every out-of-date server, one after the other")
        self.options_b = wx.Button(self, label="Options…")
        self.options_b.SetToolTip("install or remove the selected server's optional packages (a GPU build of "
                                  "PyTorch, the MKL sparse solver)")
        # how long the selected server keeps a finished result (POST /v1/settings)
        self.keep = wx.SpinCtrl(self, min=KEEP_MIN_H, max=KEEP_MAX_H, initial=24,
                                size=self.FromDIP(wx.Size(80, -1)))
        self.keep.SetToolTip(f"hours, {KEEP_MIN_H} to {KEEP_MAX_H} (30 days)")
        self.keep_b = wx.Button(self, label="Set")
        self.keep_b.SetToolTip("the selected server keeps a finished result this long for its owner, from now on "
                               "(results waiting included) and after its restarts")
        self.note = wx.StaticText(self, label="")
        self.note.SetName("muted")
        self.check_b.Bind(wx.EVT_BUTTON, lambda e: self.start_check())
        self.update_b.Bind(wx.EVT_BUTTON, lambda e: self.on_update(all_=False))
        self.all_b.Bind(wx.EVT_BUTTON, lambda e: self.on_update(all_=True))
        self.options_b.Bind(wx.EVT_BUTTON, lambda e: self.on_options())
        self.keep_b.Bind(wx.EVT_BUTTON, lambda e: self.on_keep())
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: self._selected())
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for b in (self.check_b, self.update_b, self.all_b, self.options_b):
            buttons.Add(b, 0, wx.RIGHT, gap)
        keep_row = wx.BoxSizer(wx.HORIZONTAL)
        keep_row.Add(wx.StaticText(self, label="Keep results of the selected server for"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, gap)
        keep_row.Add(self.keep, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, gap)
        keep_row.Add(wx.StaticText(self, label="hours"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2 * gap)
        keep_row.Add(self.keep_b, 0, wx.ALIGN_CENTER_VERTICAL)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self.list, 1, wx.EXPAND)
        s.Add(buttons, 0, wx.TOP, gap)
        s.Add(keep_row, 0, wx.TOP, gap)
        s.Add(self.note, 0, wx.TOP | wx.EXPAND, gap // 2)
        self.SetSizer(s)
        self._enable()

    def _destroyed(self, evt):
        """The window closes: stop following updates (EVT_WINDOW_DESTROY bubbles up from children: ours only)."""
        evt.Skip()
        if evt.GetEventObject() is self:
            self.stop.set()

    def _selection(self) -> Optional[dict]:
        """The selected row, or None."""
        sel = self.list.GetFirstSelected()
        return self.rows[sel] if 0 <= sel < len(self.rows) else None

    def _selected(self):
        """A row selected: its keep time in the box, the buttons for it."""
        row = self._selection()
        if row is not None and row.get("keep_hours"):
            self.keep.SetValue(int(round(float(row["keep_hours"]))))
        self._enable()

    def start_check(self):
        """Ask every server for its version (background); the list fills when they have answered."""
        from .server_updates import check
        address, key = self.settings()
        if not address or not key:
            self.note.SetLabel("Enter the server's address and access key in Preferences first.")
            return
        self.note.SetLabel("Asking the servers…")
        self.check_b.Disable()

        def ask():
            """(background) The rows, or why not."""
            try:
                rows, error = check(address, key), ""
            except Exception as e:                  # noqa: BLE001 - shown in the note
                rows, error = [], str(e)
            wx.CallAfter(self._show, rows, error)
        threading.Thread(target=ask, name="gt-server-versions", daemon=True).start()

    def _show(self, rows: list, error: str):
        """(GUI thread) The servers in the list, the out-of-date ones marked; a note on what to do."""
        if not self:
            return
        self.check_b.Enable()
        self.rows = rows
        self.list.DeleteAllItems()
        warn = theme.colour(theme.palette().warn)
        for r in rows:
            i = self.list.InsertItem(self.list.GetItemCount(), r["name"])
            self.list.SetItem(i, 1, r["version"])
            self.list.SetItem(i, 2, f"{float(r['keep_hours']):g} h" if r.get("keep_hours") else "")
            self.list.SetItem(i, 3, r["text"])
            if r["state"] in ("older", "unreachable"):
                self.list.SetItemTextColour(i, warn)
        old = [r for r in rows if r["state"] == "older"]
        if error:
            self.note.SetLabel(error)
        elif not old:
            self.note.SetLabel("Every server runs this plugin's version." if rows else "")
        elif self.bundle is None:
            self.note.SetLabel(f"{len(old)} server(s) out of date. This plugin carries no server package (a "
                               "development copy): update them with the server package, or install a built plugin.")
        else:
            self.note.SetLabel(f"{len(old)} server(s) out of date; Update installs this plugin's version on them "
                               "at once (a busy one cancels its running jobs: their owners run them again).")
        self.note.Wrap(max(200, self.list.GetSize().width))
        if rows and self.list.GetFirstSelected() < 0:
            self.list.Select(0)                     # the configured server: its keep time in the box
        self.GetParent().Layout()
        self._selected()

    def _enable(self):
        """Update: a selected out-of-date server that takes remote updates; Update all: any such server; neither
        without the plugin's server package or while updating. Options… and Set: a reachable selected one."""
        can = [r for r in self.rows if r["can_update"] and r["address"] not in self.busy]
        row = self._selection()
        self.update_b.Enable(bool(self.bundle) and row is not None and row in can)
        self.all_b.Enable(bool(self.bundle) and bool(can))
        ok = row is not None and row["state"] != "unreachable" and row["address"] not in self.busy
        self.options_b.Enable(ok)
        self.keep_b.Enable(ok)
        self.keep.Enable(ok)

    def on_keep(self):
        """Set: the selected server keeps results this long (background request); its row shows the new time."""
        row = self._selection()
        if row is None:
            return
        hours = self.keep.GetValue()
        _, key = self.settings()
        self.keep_b.Disable()

        def ask():
            """(background) The server's answer, or why not."""
            from ..remote.client import Remote
            try:
                return Remote(row["address"], key).set_keep_hours(hours), ""
            except Exception as e:                  # noqa: BLE001 - said in the note
                return None, str(e)

        def done(out):
            """(GUI thread) The row and the note say what happened."""
            answer, error = out
            self._enable()
            if answer is None:
                self.note.SetLabel(f"{row['name']} kept its setting: {error}")
            else:
                row["keep_hours"] = answer["keep_hours"]
                for i, r in enumerate(self.rows):
                    if r is row:
                        self.list.SetItem(i, 2, f"{answer['keep_hours']:g} h")
                self.note.SetLabel(f"{row['name']} keeps finished results {answer['keep_hours']:g} h from now on"
                                   + ("." if answer.get("saved") else
                                      ", until its restart (it could not save its settings file)."))
            self.note.Wrap(max(200, self.list.GetSize().width))
            self.GetParent().Layout()
        off_thread(self, ask, done)

    def on_options(self):
        """Options…: the selected server's options as it lists them, one to install or remove after a
        confirmation; its row says how far it is. The server takes only names of its own fixed list
        (remote/options.py). The window was opened with the administration password."""
        from .server_updates import follow_option, installed_lines, no_options_reason, option_choices
        row = self._selection()
        if row is None:
            return
        title = "GroundTruth: server options"
        _, key = self.settings()
        self.options_b.Disable()

        def look():
            """(background) The server's status, or why not."""
            from ..remote.client import Remote
            try:
                return Remote(row["address"], key).status()
            except Exception as e:                  # noqa: BLE001 - said in the message below
                return str(e)

        def chosen(status):
            """(GUI thread) Offer the server's options; start the chosen one."""
            self._enable()
            if isinstance(status, str):
                message_box(f"{row['name']} can't be asked: {status}", title, wx.ICON_WARNING)
                return
            choices = option_choices(status)
            # what the server has, above the choices (the user: "show what options have been installed already")
            have = installed_lines(status)
            has = ("On " + row["name"] + ":\n" + "\n".join("  " + h for h in have) + "\n\n") if have else ""
            if not choices:
                message_box(f"{has}{row['name']} offers nothing to install or remove from here: "
                            f"{no_options_reason(status)}.", title, wx.ICON_INFORMATION)
                return
            with wx.SingleChoiceDialog(self, f"{has}Remove an installed option, or install one:", title,
                                       [c["text"] for c in choices]) as d:
                pick = choices[d.GetSelection()] if d.ShowModal() == wx.ID_OK else None
            if pick is None or message_box(pick["confirm"], title, wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
                return
            self.busy.add(row["address"])
            self._enable()

            def work():
                """(background) Ask the server, follow it, then check the servers again."""
                _, text = follow_option(row["address"], key, pick["name"], pick["action"],
                                        lambda t: wx.CallAfter(self._row_text, row["address"], t), stop=self.stop)
                wx.CallAfter(self._row_text, row["address"], text)
                if not self.stop.is_set():
                    wx.CallAfter(self._options_done, row["address"], text)
            threading.Thread(target=work, name="gt-server-option", daemon=True).start()
        off_thread(self, look, chosen)

    def _options_done(self, address: str, text: str):
        """(GUI thread) An option's install or removal ended: the row keeps what happened; the buttons are back."""
        if not self:
            return
        self.busy.discard(address)
        self._enable()
        self._row_text(address, text)

    def on_update(self, all_: bool):
        """Update the selected (or every) out-of-date server, after asking, one after the other in the background;
        each row says how far its update is."""
        from .server_updates import update
        row = self._selection()
        targets = [r for r in self.rows if r["can_update"] and r["address"] not in self.busy] if all_ else \
            ([row] if row is not None and row["can_update"] else [])
        if not targets or self.bundle is None:
            return
        from . import server_info
        if not server_info.confirm_update(self, targets, self.bundle["version"]):
            return
        # servers with jobs running: the user decides whether they finish first or are stopped (never by itself)
        busy = server_info.ask_running_jobs(self, [r for r in targets if r.get("running")])
        if busy is None:
            return
        _, key = self.settings()
        self.busy |= {r["address"] for r in targets}
        self._enable()
        bundle = self.bundle

        def work():
            """(background) One server after the other; each row told every step."""
            for r in targets:
                done, text = update(r["address"], key, bundle,
                                    lambda t, a=r["address"]: wx.CallAfter(self._row_text, a, t), stop=self.stop,
                                    busy=busy)
                wx.CallAfter(self._row_text, r["address"], text)
                if self.stop.is_set():
                    return
            wx.CallAfter(self._updated, [r["address"] for r in targets])
        threading.Thread(target=work, name="gt-server-update", daemon=True).start()

    def _row_text(self, address: str, text: str):
        """(GUI thread) A server's State cell."""
        if not self:
            return
        for i, r in enumerate(self.rows):
            if r["address"] == address:
                self.list.SetItem(i, 3, text)

    def _updated(self, addresses: list):
        """(GUI thread) The updates are over: ask every server again."""
        if not self:
            return
        self.busy -= set(addresses)
        self.start_check()
