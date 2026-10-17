"""Writes the settings file off the GUI thread. Every edit in the window saves the settings; emitting the YAML
takes ~400 ms on a 200-part board, which would freeze the window after each edit. The GUI thread only copies the
document (SettingsFile.snapshot); a background thread writes the copies, only the newest when several wait."""
from __future__ import annotations

from typing import Callable, Optional

from .background import LatestOnly


class BackgroundSaver:
    """The window's settings writer: a LatestOnly worker that writes the newest snapshot handed to it."""

    def __init__(self, on_error: Optional[Callable[[Exception], None]] = None):
        """on_error: called (in the worker thread) when a save fails."""
        self._worker = LatestOnly("groundtruth-save", on_error)

    def save(self, settings):
        """Hand a copy of the settings over; a save still waiting is replaced (this copy holds its changes too)."""
        self._worker.submit(settings.snapshot())

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until everything handed over is on disk (before the window closes). False on timeout."""
        return self._worker.flush(timeout)
