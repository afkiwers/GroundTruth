"""The conducted standard and class choices of the Converters and Bridges tabs, as TI WEBENCH offers them: the
standard ("CISPR 22", "CISPR 25", ...) and, filled from it, its class ("CLASS A" / "CLASS B", "CLASS 1" ... "CLASS 5").

The texts the window shows and the settings' keys (emi/conducted.CLASSES) are mapped here. No wx import: the
functions only call the methods of the wx.Choice they are given.
"""
from __future__ import annotations

from typing import Optional

from ..config.settings import CONDUCTED_STANDARDS
from ..emi.conducted import CLASSES, DEFAULT_CLASS, STANDARD_NAMES, class_label, describe, normal_class

STANDARD_DEFAULT = "(analysis default)"
CLASS_DEFAULT = "(default)"
CLASS_TIP = ("The class of the conducted standard: the same measurement, a stricter or looser limit. CISPR 22 / 32: "
             "CLASS A commercial and industrial, CLASS B residential (about 10 dB stricter). CISPR 25: CLASS 1 "
             "(loosest) to CLASS 5 (strictest), as the vehicle maker's specification names it. (default): the "
             "analysis' class with the analysis' standard, else the standard's strictest (CLASS B, CLASS 5). Help: "
             "Conducted-emission limits.")


def analysis_of(settings):
    """The settings' analysis section, or the defaults before a board's settings are loaded."""
    if settings is None:
        from ..config.settings import AnalysisSettings
        return AnalysisSettings()
    return settings.config.analysis


def standard_items() -> list[str]:
    """The standard choice's entries: the analysis default, then the standards by name (CISPR 22, CISPR 25 first)."""
    return [STANDARD_DEFAULT] + [STANDARD_NAMES[k] for k in CONDUCTED_STANDARDS]


def standard_text(key: Optional[str]) -> str:
    """A stage's standard (settings key, None for the analysis') as the choice shows it."""
    return STANDARD_NAMES.get(key, key) if key else STANDARD_DEFAULT


def standard_key(text: str) -> Optional[str]:
    """The settings key of a standard choice's entry (None for the analysis default; an unknown text unchanged)."""
    if not text or text == STANDARD_DEFAULT:
        return None
    return next((k for k, v in STANDARD_NAMES.items() if v == text), text)


def effective_standard(text: str, analysis) -> str:
    """The standard a stage's class belongs to: the chosen one, else the analysis'."""
    return standard_key(text) or (analysis.conducted or "none")


def class_items(standard: str) -> list[str]:
    """The class choice's entries for a standard key: (default), then its classes ("CLASS A", ...); none: (default)."""
    return [CLASS_DEFAULT] + [class_label(standard, c) for c in CLASSES.get(standard, ())]


def class_text(standard: str, cls: Optional[str]) -> str:
    """A stage's class (settings value, None for the default) as the choice shows it for that standard."""
    c = normal_class(standard, cls) if cls else None
    return class_label(standard, c) if c else CLASS_DEFAULT


def class_key(standard: str, text: str) -> Optional[str]:
    """The settings value of a class choice's entry for that standard (None for the default)."""
    return next((c for c in CLASSES.get(standard, ()) if class_label(standard, c) == text), None)


def default_tip(analysis) -> str:
    """What "(analysis default)" stands for now, for the standard's tooltip."""
    std = analysis.conducted or "none"
    if std not in CLASSES:
        return f"{STANDARD_DEFAULT}: none (analysis.conducted)"
    return f"{STANDARD_DEFAULT}: {describe(std, normal_class(std, analysis.conducted_class) or DEFAULT_CLASS[std])}"


def refill_classes(ctrl, standard: str, keep: Optional[str] = None):
    """Fill a class wx.Choice for a standard key, choosing `keep` (a shown text) if the standard has it, else
    (default); disabled while the standard has no classes ("none")."""
    items = class_items(standard)
    if list(ctrl.GetStrings()) != items:
        ctrl.Set(items)
    if keep is None or not ctrl.SetStringSelection(keep):
        ctrl.SetSelection(0)
    ctrl.Enable(len(items) > 1)
