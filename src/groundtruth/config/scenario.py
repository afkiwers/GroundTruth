"""Saved configurations: what a simulation analyses and how, kept apart from what is shared by every simulation of a
board, so a set-up can be saved, loaded again later and run the same way (the user: "there is no real way of saving a
configuration and ... loading it at a later point to execute the same simulation with the same nets ... or the same
bridge settings and selection").

The settings file stays one file, the working settings (loaded by default: "the last configuration"). A
configuration is a second file, `<board>/groundtruth/configs/<name>.gtconfig.yaml`, holding only SCENARIO_KEYS:
the nets, converters, bridges, DC loads, PDN rails, signal-integrity pairs and targets (each with its on/off tick),
the what-if edits, and the run settings (analysis, thermal, victims). Loading one replaces exactly those sections of
the working settings; the rest stays shared: the part models (one truth, from the schematic), the through-hole pin
values, reference nets, stack, safety voltages, rules, waivers, drawing and report options (decided with the user,
2026-10-23: parts shared, run settings per configuration).

A configuration remembers its parts by a short hash per part model (`parts`): loading it later names the parts that
changed since it was saved, without putting the old values back (a later correction of a part is not undone). An
exact re-run with the old parts is the History tab's: each kept run holds its whole settings file.

KiCad-independent (the CLI's --config uses it too). Heavy steps (deep copies, parsing) are plain functions a GUI runs
in a worker thread.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from ruamel.yaml.comments import CommentedMap

from .settings import (Config, ConfigError, SettingsFile, _dumps, _write_atomic, _yaml, config_from_data,
                       validate)

FORMAT = 1                          # `groundtruth_configuration:` in the file: its layout's version
SUFFIX = ".gtconfig.yaml"
FOLDER = "configs"                  # in the board's groundtruth folder
BEFORE_LOAD = ".before-load" + SUFFIX   # the working settings' scenario before the last load ("Back to last settings")
# what a configuration holds (the rest of the settings file is shared by every simulation of the board)
SCENARIO_KEYS = ("nets", "converters", "bridges", "dc_loads", "pdn", "si", "modifications", "analysis", "thermal",
                 "victims")
HEADER_KEYS = ("groundtruth_configuration", "name", "note", "saved", "board", "parts")
ACTIVE_KEY = "configuration"        # in the working settings: {name, file} of the configuration it came from


def folder_for(settings_path: Union[str, Path]) -> Path:
    """The board's configurations folder: <board folder>/groundtruth/configs (beside the settings file)."""
    return Path(settings_path).parent / FOLDER


def file_name(name: str) -> str:
    """A configuration's file name from its name: letters, digits, '-' and '_' kept, the rest '_'."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()).strip("_") or "configuration"
    return stem + SUFFIX


def _part_hash(entry) -> str:
    """The first 8 hex digits of the SHA-256 of one part's settings entry (its model, value, footprint, pin values)
    as sorted JSON: equal entries give equal hashes whatever their YAML formatting."""
    text = json.dumps(entry, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def parts_fingerprint(doc) -> dict:
    """{reference: hash} of every part in a settings document's `components:` (parts marked removed left out)."""
    comps = doc.get("components") or {}
    return {str(ref): _part_hash(e) for ref, e in comps.items() if not (isinstance(e, dict) and e.get("removed"))}


def changed_parts(saved: Optional[dict], doc) -> tuple[list[str], list[str]]:
    """(parts whose model changed since the configuration was saved, parts it knew that the settings no longer
    have), each sorted. A configuration saved without `parts` (by hand) changes nothing."""
    if not saved:
        return [], []
    now = parts_fingerprint(doc)
    changed = sorted(r for r, h in saved.items() if r in now and now[r] != h)
    gone = sorted(r for r in saved if r not in now)
    return changed, gone


def extract(doc, name: str, note: str = "", board: str = "") -> CommentedMap:
    """A configuration from a settings document: the header (format, name, note, when, which board, the parts'
    hashes) and a deep copy of each SCENARIO_KEYS section the document has (comments and order kept)."""
    out = CommentedMap()
    out["groundtruth_configuration"] = FORMAT
    out["name"] = name
    out["note"] = note
    out["saved"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    out["board"] = board
    parts = CommentedMap(sorted(parts_fingerprint(doc).items()))
    parts.fa.set_flow_style()
    out["parts"] = parts
    for key in SCENARIO_KEYS:
        if key in doc:
            out[key] = copy.deepcopy(doc[key])
    return out


def save(path: Union[str, Path], scen: CommentedMap) -> Path:
    """Write a configuration (atomically; its folder made when missing)."""
    path = Path(path)
    _write_atomic(path, _dumps(scen))
    return path


def parse(text: str, source: str = "configuration") -> CommentedMap:
    """A configuration from its YAML text. ConfigError for invalid YAML or a file that is not a configuration."""
    try:
        doc = _yaml().load(text)
    except Exception as e:  # ruamel parser errors
        raise ConfigError([f"{source}: invalid YAML: {e}"]) from e
    if not isinstance(doc, dict) or "groundtruth_configuration" not in doc:
        raise ConfigError([f"{source}: not a GroundTruth configuration (no groundtruth_configuration: line)"])
    return doc


def load(path: Union[str, Path]) -> CommentedMap:
    """Read a configuration file. ConfigError when it is unreadable or not a configuration."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError([f"cannot read {path}: {e}"]) from e
    return parse(text, str(path))


def from_settings_text(text: str, name: str, board: str = "") -> CommentedMap:
    """The configuration a whole settings file holds (a history run's copy): its scenario sections and parts' hashes,
    named `name`. ConfigError for invalid YAML."""
    try:
        doc = _yaml().load(text) or CommentedMap()
    except Exception as e:  # ruamel parser errors
        raise ConfigError([f"the run's settings: invalid YAML: {e}"]) from e
    return extract(doc, name, board=board)


def merged(doc, scen) -> tuple[CommentedMap, Config]:
    """(the document with the configuration's sections in place of its own, that document parsed): each
    SCENARIO_KEYS section replaced in place (a new one appended), one the configuration lacks removed (its
    defaults or none: a configuration defines what is simulated). `doc` itself is modified: pass a copy. A
    ConfigError (an invalid configuration) is raised before anything is handed back."""
    for key in SCENARIO_KEYS:
        if key in scen:
            doc[key] = copy.deepcopy(scen[key])
        elif key in doc:
            del doc[key]
    return doc, config_from_data(doc)


def notes(scen, doc, config: Config, board=None) -> list[str]:
    """What the user should know after loading `scen` into a document: the parts changed or gone since it was saved,
    and what it names that the board lacks (config.settings.validate)."""
    out = []
    changed, gone = changed_parts(scen.get("parts"), doc)
    if changed:
        out.append(f"{len(changed)} part(s) changed since this configuration was saved (their current values are "
                   f"used): {', '.join(changed[:12])}{' …' if len(changed) > 12 else ''}")
    if gone:
        out.append(f"{len(gone)} part(s) it was saved with are no longer in the settings: {', '.join(gone[:12])}"
                   f"{' …' if len(gone) > 12 else ''}")
    if board is not None:
        out += validate(config, board)
    return out


def apply(settings: SettingsFile, scen, board=None, active: Optional[dict] = None) -> list[str]:
    """Load a configuration into the settings in one step (the CLI's --config; the plugin does the same in a worker,
    see ui/main_frame): the merged document parsed first, so an invalid configuration raises ConfigError and leaves
    the settings as they were; `active` ({name, file}) recorded as the configuration in use. The notes."""
    doc, config = merged(copy.deepcopy(settings.doc), scen)
    if active is not None:
        set_active(doc, active)
    settings.doc, settings.config = doc, config
    return notes(scen, doc, config, board)


def set_active(doc, active: Optional[dict]) -> None:
    """Record in a settings document which configuration it came from ({name, file}), or none."""
    if active:
        entry = CommentedMap((k, str(v)) for k, v in active.items() if v)
        entry.fa.set_flow_style()
        doc[ACTIVE_KEY] = entry
    elif ACTIVE_KEY in doc:
        del doc[ACTIVE_KEY]


def active_of(doc) -> dict:
    """The configuration a settings document came from ({name, file}), or {}."""
    a = doc.get(ACTIVE_KEY)
    return {str(k): str(v) for k, v in a.items()} if isinstance(a, dict) else {}


def differs(doc, scen) -> bool:
    """The document's scenario sections are not the configuration's (the window's title shows a *)."""
    return any(doc.get(k) != scen.get(k) for k in SCENARIO_KEYS)
