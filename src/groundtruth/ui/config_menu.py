"""The window's configurations and Undo / Redo (config/scenario.py, ui/undo.py): the top bar's **Configuration ▾**
menu (Save configuration as… / Save configuration / Load configuration… / Load from a past run ▸ / Back to last
settings / Save settings now) and its **Undo** and **Redo** buttons (Ctrl+Z, Ctrl+Y), as a mixin of MainFrame.

The user (2026-10-23): "there is no real way of saving a configuration and ... loading it at a later point to execute
the same simulation ... Default behaviour, load last config. the offer a button to load settings from file or return
to last settings. Also, an undo button would be good to undo changes."

- Every change of the settings already ends in MainFrame.on_save, which copies the settings document for the
  background writer (SettingsFile.snapshot_doc); that same copy is the undo step (no second copy). Undo / Redo put
  a kept copy back (SettingsFile.restore, ~45 ms on refboard's 223 parts) and reload the tabs one per event
  (MainFrame._load_tabs), so the window never stalls.
- Loading a configuration (a file, or a past run's settings from the History tab) merges and parses it in a job
  (scenario.merged) and swaps the result in on the GUI thread; before that, the working settings' own scenario is
  written to configs/.before-load.gtconfig.yaml, which "Back to last settings" loads (and which then holds the
  configuration left, so it toggles). A load is one undo step.
- The title shows the configuration in use and * when its sections differ from the saved file (scenario.differs).
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Optional

import wx

from . import theme
from .undo import UndoStack

RUNS_IN_MENU = 10           # "Load from a past run": the newest kept runs offered


def initial_state(settings) -> tuple:
    """(worker, at a board read) (a copy of the settings document: the undo stack's current state, the configuration
    in use {name, file} or {}, that configuration's file as loaded or None when it is gone)."""
    from ..config import scenario
    doc = copy.deepcopy(settings.doc)
    active = scenario.active_of(doc)
    scen = None
    if active.get("file"):
        try:
            scen = scenario.load(active["file"])
        except Exception:               # moved or deleted since: the name is still shown, without a * check
            scen = None
    return doc, active, scen


class NameDialog(wx.Dialog):
    """Save configuration as…: its name (also the file's) and an optional note."""

    def __init__(self, parent, name: str = "", note: str = ""):
        """The two fields, OK and Cancel."""
        super().__init__(parent, title="Save configuration as")
        grid = wx.FlexGridSizer(2, 2, 8, 8)
        grid.AddGrowableCol(1)
        self.name = wx.TextCtrl(self, value=name, size=(320, -1))
        self.name.SetHint("e.g. ADC paths at 1 kHz")
        self.note = wx.TextCtrl(self, value=note)
        self.note.SetHint("optional: what this configuration is for")
        for label, ctrl in (("Name", self.name), ("Note", self.note)):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.EXPAND)
        hint = wx.StaticText(self, label="Saved: what is simulated and how (the tabs' rows with their ticks, what-if "
                                         "edits, run settings). The part models stay shared by every configuration.")
        hint.SetName("muted")
        hint.Wrap(420)
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(grid, 0, wx.EXPAND | wx.ALL, 12)
        box.Add(hint, 0, wx.LEFT | wx.RIGHT, 12)
        box.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(box)
        theme.style_tree(self)

    def values(self) -> tuple[str, str]:
        """(name, note), stripped."""
        return self.name.GetValue().strip(), self.note.GetValue().strip()


class ConfigActions:
    """Mixin for MainFrame: the configuration menu and Undo / Redo (see the module). Expects the window's settings,
    board, notebook and tabs, _start, _load_tabs, _saver, _update_tab_counts, _show_stale and busy()."""

    def _config_init(self):
        """The state: the undo steps, the configuration in use and its file's sections, the label of the next undo
        step (set by the action that changes the settings; else the tab shown names it)."""
        self._undo = UndoStack()
        self._undo_label: Optional[str] = None
        self._active: dict = {}
        self._active_scen = None
        self._config_modified = False
        self._base_title = self.GetTitle()

    def _config_buttons(self, button):
        """The top bar's Configuration ▾, Undo and Redo (in place of Save settings: every edit is saved anyway)."""
        # the menu pops up after the click's own handling has ended (a popup menu runs a loop of its own)
        self.t_config = button("Configuration ▾", lambda e: wx.CallAfter(self.on_config_menu),
                               tip="save what is set up on the tabs as a configuration, load one (or a past run's), "
                                   "go back to the settings before the last load")
        self.t_undo = button("Undo", lambda e: self.on_undo(), tip="take back the last change of the settings (Ctrl+Z)")
        self.t_redo = button("Redo", lambda e: self.on_redo(), tip="make the change undone last again (Ctrl+Y)")
        self.t_undo.Enable(False)
        self.t_redo.Enable(False)

    def _config_accelerators(self) -> list:
        """Ctrl+Z, Ctrl+Y and Ctrl+Shift+Z for the window's accelerator table."""
        undo_id, redo_id = wx.NewIdRef(), wx.NewIdRef()
        self.Bind(wx.EVT_MENU, lambda e: self._undo_key(False), id=undo_id)
        self.Bind(wx.EVT_MENU, lambda e: self._undo_key(True), id=redo_id)
        return [(wx.ACCEL_CTRL, ord("Z"), undo_id), (wx.ACCEL_CTRL, ord("Y"), redo_id),
                (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("Z"), redo_id)]

    def _undo_key(self, redo: bool):
        """Ctrl+Z / Ctrl+Y: in a text field its own undo (the window's accelerators come before the field on
        Windows: settings Undo there would surprise), elsewhere the settings' Undo / Redo."""
        focus = wx.Window.FindFocus()
        if isinstance(focus, wx.TextEntry):
            if redo and focus.CanRedo():
                focus.Redo()
            elif not redo and focus.CanUndo():
                focus.Undo()
            return
        self.on_redo() if redo else self.on_undo()

    # ------------------------------------------------------------------ state
    def _config_loaded(self, state):
        """A board read: the settings' copy becomes the undo stack's current state (its steps kept across reads of
        the same board), and the configuration in use is shown. None (no settings read): nothing changes."""
        if state is None:
            return
        doc, self._active, self._active_scen = state
        if self._undo.current is None:
            self._undo.reset(doc)
        else:
            self._undo.rebase(doc)
        self._config_modified = self._differs(doc)
        self._config_refresh()

    def _differs(self, doc) -> bool:
        """The settings' scenario sections differ from the configuration file in use (False with none)."""
        from ..config import scenario
        return self._active_scen is not None and scenario.differs(doc, self._active_scen)

    def _record_undo(self, doc):
        """(on_save) The settings changed: `doc` (the copy just handed to the writer) becomes an undo step, named by
        the action that changed them or else by the tab shown."""
        label = self._undo_label
        if label is None:
            k = self.notebook.GetSelection()
            label = f"{self.notebook.GetPageText(k)} edit" if k >= 0 else "edit"
        self._undo_label = None
        if label == "":                     # saved without a step of its own (the parts merged at a board read)
            self._undo.rebase(doc)
        else:
            self._undo.record(doc, label)
        self._config_modified = self._differs(doc)
        self._config_refresh()

    def _config_refresh(self):
        """The title (configuration in use, * when changed) and the Undo / Redo buttons' state and tooltips."""
        name = self._active.get("name", "")
        title = self._base_title + (f" · {name}{'*' if self._config_modified else ''}" if name else "")
        if self.GetTitle() != title:
            self.SetTitle(title)
        idle = not self.busy()
        self.t_undo.Enable(idle and self._undo.can_undo())
        self.t_redo.Enable(idle and self._undo.can_redo())
        self.t_undo.SetToolTip(f"Undo: {self._undo.undo_label()} (Ctrl+Z)" if self._undo.can_undo()
                               else "nothing to undo")
        self.t_redo.SetToolTip(f"Redo: {self._undo.redo_label()} (Ctrl+Y)" if self._undo.can_redo()
                               else "nothing to redo")

    def _config_enable(self, on: bool):
        """(MainFrame._enable) A job starts or ends: the menu and Undo / Redo only when idle."""
        self.t_config.Enable(on)
        self.t_undo.Enable(on and self._undo.can_undo())
        self.t_redo.Enable(on and self._undo.can_redo())

    # ------------------------------------------------------------------ undo / redo
    def on_undo(self):
        """Undo: the settings as they were before their last change (tabs reloaded, saved)."""
        if self.settings is None or self.busy() or not self._undo.can_undo():
            return
        state, label = self._undo.undo()
        self._put_back(state, f"undone: {label}")

    def on_redo(self):
        """Redo: the change undone last made again."""
        if self.settings is None or self.busy() or not self._undo.can_redo():
            return
        state, label = self._undo.redo()
        self._put_back(state, f"redone: {label}")

    def _put_back(self, state, message: str):
        """Take a kept state as the settings (restore: a copy, parsed), write it, reload the tabs, say so."""
        from ..config import scenario
        from ..config.settings import ConfigError
        try:
            self.settings.restore(state)
        except ConfigError as e:            # kept states were valid settings; a guard all the same
            self.SetStatusText(f"could not restore the settings: {'; '.join(e.messages)}")
            return
        self._active = scenario.active_of(self.settings.doc)
        if self._active.get("file") and (self._active_scen is None or
                                         self._active_scen.get("name") != self._active.get("name")):
            try:
                self._active_scen = scenario.load(self._active["file"])
            except Exception:
                self._active_scen = None
        elif not self._active:
            self._active_scen = None
        self._saver.save(self.settings)     # written; not a new undo step (the stack moved already)
        self.dirty = False
        self._config_modified = self._differs(state)
        self._reload_tabs(message)

    def _reload_tabs(self, message: str):
        """The setting tabs show the settings again (one tab per event), then the counts, the stale marks, the
        title and `message` on the status line."""
        tabs = [self.parts, self.nets, self.converters, self.bridges, self.dc_loads, self.pdn, self.si]

        def then():
            """After the last tab: what depends on all of them."""
            self._update_tab_counts()
            self._show_stale()
            self._config_refresh()
            self.SetStatusText(message)
        self._load_tabs(tabs, self.board, self.settings, then)

    # ------------------------------------------------------------------ the menu
    def on_config_menu(self):
        """Configuration ▾: the menu under the button."""
        if self.settings is None:
            self.SetStatusText("the board is not loaded yet")
            return
        from ..config import scenario
        menu = wx.Menu()
        items = []

        def add(label, handler, enabled=True, sub=None):
            """One entry (or a submenu)."""
            if sub is not None:
                item = menu.AppendSubMenu(sub, label)
            else:
                item = menu.Append(wx.ID_ANY, label)
                self.Bind(wx.EVT_MENU, lambda e, h=handler: h(), item)
            item.Enable(enabled)
            items.append(item)
        name = self._active.get("name", "")
        add("Save configuration as…", self.save_config_as)
        add(f"Save configuration “{name}”" if name else "Save configuration", self.save_config,
            enabled=bool(self._active.get("file")))
        menu.AppendSeparator()
        add("Load configuration…", self.load_config_file)
        runs = list(getattr(self.history, "runs", []) or [])[:RUNS_IN_MENU]
        sub = wx.Menu()
        from .history_panel import run_label
        for run in runs:
            note = run.note
            item = sub.Append(wx.ID_ANY, run_label(run.id) + (f" — {note}" if note else ""))
            self.Bind(wx.EVT_MENU, lambda e, r=run: self.load_config_from_run(r), item)
        add("Load from a past run", None, enabled=bool(runs), sub=sub)
        before = scenario.folder_for(self.settings.path) / scenario.BEFORE_LOAD
        add("Back to last settings", self.back_to_last, enabled=before.exists())
        menu.AppendSeparator()
        add("Save settings now", self.on_save)
        b = self.t_config
        self.topbar.PopupMenu(menu, b.GetPosition() + wx.Point(0, b.GetSize().height))
        menu.Destroy()

    def save_config_as(self):
        """Save configuration as…: name and note, then configs/<name>.gtconfig.yaml (asked before overwriting); it
        becomes the configuration in use."""
        from ..config import scenario
        with NameDialog(self, self._active.get("name", "")) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name, note = dlg.values()
        if not name:
            return
        path = scenario.folder_for(self.settings.path) / scenario.file_name(name)
        if path.exists() and path != Path(self._active.get("file", "")) and wx.MessageBox(
                f"A configuration {path.name} exists already. Replace it?", "GroundTruth",
                wx.YES_NO | wx.NO_DEFAULT) != wx.YES:
            return
        self._write_config(path, name, note)

    def save_config(self):
        """Save configuration: the configuration in use, written again with the tabs' current state."""
        if self._active.get("file"):
            note = str(self._active_scen.get("note", "")) if self._active_scen is not None else ""
            self._write_config(Path(self._active["file"]), self._active.get("name", ""), note)

    def _write_config(self, path: Path, name: str, note: str):
        """Write the configuration from the settings as they are (the undo stack's current copy, read only) in the
        background; then it is the one in use (recorded in the settings: an undo step)."""
        from ..config import scenario
        doc = self._undo.current if self._undo.current is not None else copy.deepcopy(self.settings.doc)
        board = Path(self.board.source).name if self.board is not None and self.board.source else ""

        def work(progress):
            """(worker) The configuration's sections copied and written."""
            scen = scenario.extract(doc, name, note, board)
            scenario.save(path, scen)
            return scen

        def done(scen):
            """It is the configuration in use now."""
            self._active, self._active_scen = {"name": name, "file": str(path)}, scen
            scenario.set_active(self.settings.doc, self._active)
            self._undo_label = f"Save configuration {name}"
            self.dirty = True
            self.on_save()
            self.SetStatusText(f"configuration saved: {path}")
        self._start("saving the configuration…", work, done, what="save the configuration", cancellable=False)

    def load_config_file(self):
        """Load configuration…: a file (the board's configs folder first)."""
        from ..config import scenario
        folder = scenario.folder_for(self.settings.path)
        with wx.FileDialog(self, "Load a configuration", defaultDir=str(folder if folder.exists() else
                                                                         self.settings.path.parent),
                           wildcard=f"GroundTruth configurations (*{scenario.SUFFIX})|*{scenario.SUFFIX}|"
                                    "All files (*.*)|*.*", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = Path(dlg.GetPath())
        self._load_config(lambda: scenario.load(path), {"file": str(path)}, f"Load configuration {path.stem}")

    def load_config_from_run(self, run):
        """Load from a past run (the menu, the History tab): the configuration that run simulated."""
        from ..config import scenario
        from .history_panel import run_label
        name = f"run {run_label(run.id)}"

        def read():
            """(worker) The run's settings file, as a configuration."""
            text = run.settings_text()
            if text is None:
                from ..config.settings import ConfigError
                raise ConfigError([f"the settings of {name} are missing or unreadable"])
            return scenario.from_settings_text(text, name)
        self._load_config(read, {}, f"Load {name}")

    def back_to_last(self):
        """Back to last settings: the settings' scenario as it was before the last load (which then holds the one
        left: choosing it again goes back)."""
        from ..config import scenario
        path = scenario.folder_for(self.settings.path) / scenario.BEFORE_LOAD
        self._load_config(lambda: scenario.load(path), None, "Back to last settings")

    def _load_config(self, read, active: Optional[dict], label: str):
        """Load a configuration (read() gives it, in the worker): the working settings' own scenario kept as the
        "last settings" first, the configuration merged and parsed, then swapped in, the tabs reloaded, one undo
        step. active: what to record as in use ({file}: its name from the file; {}: none; None: the one the
        before-load file recorded). Notes (parts changed, names the board lacks) shown after."""
        from ..config import scenario
        if self.dirty:
            self.on_save()
        base = self._undo.current if self._undo.current is not None else copy.deepcopy(self.settings.doc)
        board, before = self.board, scenario.folder_for(self.settings.path) / scenario.BEFORE_LOAD
        board_name = Path(board.source).name if board is not None and board.source else ""

        def work(progress):
            """(worker) Read, keep the current scenario as the last settings, merge, parse, the notes."""
            scen = read()
            now = scenario.extract(base, "last settings", board=board_name)
            now["was"] = dict(scenario.active_of(base)) or {}
            doc, config = scenario.merged(copy.deepcopy(base), scen)
            if active is None:
                use = dict(scen.get("was") or {})
            elif active.get("file"):
                use = {"name": str(scen.get("name") or Path(active["file"]).stem), "file": active["file"]}
            else:
                use = {}
            scenario.set_active(doc, use)
            notes = scenario.notes(scen, doc, config, board)
            scenario.save(before, now)          # only once the configuration has proved valid
            target = None
            if use.get("file"):
                try:
                    target = scenario.load(use["file"])
                except Exception:
                    target = None
            return doc, config, use, target, notes

        def done(result):
            """Swap the settings in, reload, record, tell."""
            doc, config, use, target, notes = result
            self.settings.doc, self.settings.config = doc, config
            self._active, self._active_scen = use, target
            self._undo_label = label
            self.dirty = True
            self.on_save()
            self._reload_tabs(f"{label.lower()}: done" + (f" · {len(notes)} note(s)" if notes else ""))
            if notes:
                wx.MessageBox(f"{label}:\n\n" + "\n\n".join(notes), "GroundTruth", wx.ICON_INFORMATION)
        self._start("loading the configuration…", work, done, what=label.lower(), cancellable=False)
