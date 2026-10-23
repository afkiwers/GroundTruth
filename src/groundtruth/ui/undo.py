"""Undo and Redo for the settings (the user: "an undo button would be good to undo changes").

The window keeps the settings document as it is after every change (the deep copy each save makes anyway,
SettingsFile.snapshot_doc): `current` is the newest, the undo list holds the ones before it, the redo list those
undone. Undo hands back the state before the current one; Redo the one undone last; a new change after an Undo
drops the redo list (as in any editor). The states are whole documents, so an Undo restores everything a change
touched (a tab's edit, a loaded configuration, a detection, a waiver) in one step. Plain Python: no wx.
"""
from __future__ import annotations

from typing import Any, Optional

LIMIT = 50              # undo steps kept (each is one copy of the settings document)


class UndoStack:
    """The settings states of this session: undo steps, the current state, redo steps, each with a label naming
    the change that led to it ("Signal paths edit", "Load configuration ADC sweep")."""

    def __init__(self, limit: int = LIMIT):
        """Empty: no current state until reset() or the first record()."""
        self.limit = limit
        self._undo: list[tuple[Any, str]] = []      # (state, label of the change that came after it)
        self._redo: list[tuple[Any, str]] = []      # (state, label of the change that led to it)
        self.current: Optional[Any] = None
        self.current_label = ""

    def reset(self, state: Any) -> None:
        """Start again from `state` (a board read): no steps to undo or redo."""
        self._undo.clear()
        self._redo.clear()
        self.current, self.current_label = state, ""

    def rebase(self, state: Any) -> None:
        """The settings were read again (the same file, a board read): `state` is the current one, the steps stay."""
        self.current = state

    def record(self, state: Any, label: str) -> None:
        """A change made `state` the current one: the one before becomes an undo step, the redo steps go."""
        if self.current is not None:
            self._undo.append((self.current, label))
            del self._undo[:-self.limit]
        self._redo.clear()
        self.current, self.current_label = state, label

    def can_undo(self) -> bool:
        """There is a state before the current one."""
        return bool(self._undo)

    def can_redo(self) -> bool:
        """A state was undone and no change came since."""
        return bool(self._redo)

    def undo(self) -> tuple[Any, str]:
        """(the state before the current one, the label of the change undone); that state becomes the current one.
        IndexError when there is none."""
        state, label = self._undo.pop()
        self._redo.append((self.current, label))
        self.current = state
        return state, label

    def redo(self) -> tuple[Any, str]:
        """(the state undone last, the label of its change); it becomes the current one again. IndexError when
        there is none."""
        state, label = self._redo.pop()
        self._undo.append((self.current, label))
        self.current = state
        return state, label

    def undo_label(self) -> str:
        """The change Undo takes back ("" when none)."""
        return self._undo[-1][1] if self._undo else ""

    def redo_label(self) -> str:
        """The change Redo makes again ("" when none)."""
        return self._redo[-1][1] if self._redo else ""
