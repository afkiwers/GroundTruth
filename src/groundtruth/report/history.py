"""Simulation history: every Simulate kept with the board and settings it ran on, its results and its report, in a
folder next to the board (the project's root), so earlier runs can be listed, compared and opened again.

    <board folder>/groundtruth/history/     (boards sharing a folder share it: each run names its board)
        boards/<YYYY-MM-DD_HH-MM-SS>.kicad_pcb.gz
                                         each distinct board once, named after the run that first kept it (KiCad's
                                         text zips to about 1/5); later runs on the unchanged board name it in their
                                         run.json, so a new file appears exactly when the board changed
        runs/<YYYY-MM-DD_HH-MM-SS>/
            run.json                     when and how it ran (resolution, what-if edits), which board and settings,
                                         the headline figures, the user's note
            results.json                 everything, as `groundtruth simulate --format json` writes it
            settings.groundtruth.yaml    the settings file as the run used it
            report.html                  the report page (the plugin writes it after the run, in the background)

The folder may come with a shared project, so what is read back is data only: a board copy's name from run.json is
used only when it has the form of a run's (no paths), the copy is checked against the run's SHA-256 when unpacked,
and capped in size. Anything in it may also
be deleted or edited by hand at any time (a run folder, a board copy, a report, the whole folder): a missing or
garbled file leaves that run out of the list or that figure blank, or raises HistoryError with what is missing,
never anything else."""
from __future__ import annotations

import difflib
import gzip
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

HISTORY_DIR = "history"         # in the board's groundtruth folder (the user: "It's already under groundtruth")
FORMAT = 1
RUN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d{1,3})?$")
SHA = re.compile(r"^[0-9a-f]{64}$")
BOARD_SUFFIX = ".kicad_pcb.gz"
BOARD_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d{1,3})?\.kicad_pcb\.gz$")   # <run id>.kicad_pcb.gz
MAX_BOARD_BYTES = 1 << 30           # an unpacked board copy (a real one is a few MB): stops a forged .gz filling a disk
MAX_NOTE = 2000
SETTINGS_NAME = "settings.groundtruth.yaml"
REPORT_NAME = "report.html"


class HistoryError(Exception):
    """A run that cannot be read, written or found (a damaged or deleted run, a full disk): the message says
    which run and why."""
    pass


def history_for(board_path: Union[str, Path]) -> "History":
    """The history of the board saved at board_path: <its folder>/groundtruth/history (config.settings.
    project_dir, with its settings file), its runs only (the user: no folder per board in it;
    two boards in one folder keep their runs apart by the board name each run records)."""
    from ..config.settings import project_dir
    p = Path(board_path)
    return History(project_dir(p) / HISTORY_DIR, board=p.name)


def sha256(data: bytes) -> str:
    """The SHA-256 of some bytes, as hex: identifies a board's or settings file's content."""
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes):
    """Write a .part file and rename it over the target: a crash never leaves half a file that a later read
    would take for a run."""
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _items(results: dict) -> list[dict]:
    """The headline figures of every net route, power stage and motor bridge in a results.json."""
    out = []
    for kind in ("routes", "converters", "bridges"):
        for d in results.get(kind) or []:
            if kind == "routes":
                name = f"{d.get('net', '')} {d.get('driver', '')}->{d.get('receiver', '')}"
            else:
                name = str(d.get("net", ""))
            w, c = d.get("worst") or {}, d.get("conducted_worst") or {}
            out.append({"kind": kind[:-1], "name": name,
                        "radiated_db": w.get("margin_db"), "radiated_f": w.get("f"),
                        "conducted_db": c.get("margin_db"), "conducted_f": c.get("f"),
                        "loop_nh": d.get("loop_nh"), "l_ratio": d.get("l_ratio"),
                        "error": str(d.get("error") or "")})
    return out


def headline(results: dict) -> dict:
    """What run.json keeps of a results.json: the issue counts and each item's margins and loop figures."""
    return {"counts": dict(results.get("summary") or {}), "items": _items(results)}


def run_meta(board_path: str, source: str, *, by: str, resolution: float = 1.0, what_if: bool = False,
             edits: list[str] = (), only: list[str] = (), seconds: Optional[float] = None,
             solver: Optional[dict] = None, cell_mm: Optional[float] = None) -> dict:
    """How a run ran, for History.record. source: where the board copy came from ('kicad': KiCad's board with any
    unsaved edits; 'file': the board file; 'saved file': the file, as KiCad could not hand its board over). by:
    'plugin' or 'cli'. only: the nets/stages simulated when not all (CLI --nets). cell_mm: the coarsest model's
    cell at 1x (cell_mm_of), so Preferences can show each Resolution level's cell size before the next Simulate."""
    from .. import __version__
    return {"version": __version__, "by": by,
            "board": {"name": Path(board_path).name, "path": str(board_path), "source": source},
            "resolution": resolution, "what_if": bool(what_if), "edits": list(edits), "only": list(only),
            "seconds": None if seconds is None else round(float(seconds), 1), "solver": dict(solver or {}),
            "cell_mm": None if cell_mm is None else round(float(cell_mm), 4)}


def cell_mm_of(pitches_mm, resolution: float = 1.0) -> Optional[float]:
    """The 1x cell (mm) of a run's coarsest route or power-stage model, from its models' grid pitches at the run's
    resolution: a resolution r has cells 1/sqrt(r) the size, so the coarsest pitch x sqrt(r). None without any."""
    found = [float(d) for d in pitches_mm if isinstance(d, (int, float)) and d > 0]
    if not found:
        return None
    r = resolution if isinstance(resolution, (int, float)) and resolution >= 1 else 1.0
    return max(found) * r ** 0.5


def edit_texts(mods) -> list[str]:
    """The enabled what-if edits (config.modifications.Modification) in a line each."""
    return [m.label() + (f": {m.note}" if m.note else "") for m in mods]


@dataclass
class Run:
    """One kept run: its id (the folder's name: when it ran), its folder and its run.json. The properties read
    run.json defensively: the file may have been edited by hand, so a missing or malformed entry reads as empty."""
    id: str
    folder: Path
    meta: dict

    @property
    def note(self) -> str:
        """The user's note on the run."""
        return str(self.meta.get("note") or "")

    @property
    def time(self) -> str:
        """When it ran (ISO 8601 with the time zone)."""
        return str(self.meta.get("time") or "")

    def _part(self, key: str) -> dict:
        """A sub-record of run.json ({} when it is missing or not a record)."""
        part = self.meta.get(key)
        return part if isinstance(part, dict) else {}

    @property
    def board_file_name(self) -> str:
        """The name of the board file the run simulated ("kraft_flux_mini_2.kicad_pcb"; "" when run.json says
        none)."""
        return str(self._part("board").get("name") or "")

    @property
    def board_sha(self) -> str:
        """The simulated board's content hash, "" when run.json has none (or not a valid one)."""
        sha = str(self._part("board").get("sha256") or "")
        return sha if SHA.fullmatch(sha) else ""

    @property
    def board_name(self) -> str:
        """The file name of the run's board copy in boards/ (named after the run that first kept that board), ""
        when run.json names none or something that is not such a name."""
        name = str(self._part("board").get("file") or "")
        return name if BOARD_FILE.fullmatch(name) else ""

    @property
    def settings_sha(self) -> str:
        """The settings file's content hash."""
        return str(self._part("settings").get("sha256") or "")

    @property
    def counts(self) -> dict:
        """Issue counts per severity."""
        c = self.meta.get("counts")
        return {k: v for k, v in c.items() if isinstance(v, int)} if isinstance(c, dict) else {}

    @property
    def items(self) -> list[dict]:
        """The headline figures (run.json may have been edited by hand: only well-formed entries, the name a
        string)."""
        items = self.meta.get("items")
        if not isinstance(items, list):
            return []
        return [{**i, "name": str(i.get("name") or ""), "kind": str(i.get("kind") or "")}
                for i in items if isinstance(i, dict)]

    @property
    def report(self) -> Optional[Path]:
        """The run's HTML report page, None when it was not written (yet)."""
        p = self.folder / REPORT_NAME
        return p if p.is_file() else None

    @property
    def settings_file(self) -> Path:
        """Where the run's copy of the settings is kept."""
        return self.folder / SETTINGS_NAME

    def cell_mm(self) -> Optional[float]:
        """The 1x cell of the run's coarsest model (mm): from run.json, or for a run kept before it was recorded
        there, from the grid pitches in results.json (read from disk: call it off the GUI thread). None when neither
        says."""
        c = self.meta.get("cell_mm")
        if isinstance(c, (int, float)) and c > 0:
            return float(c)
        try:
            res = self.results()
        except HistoryError:
            return None
        pitches = [(d.get("grid") or {}).get("d_mm") for kind in ("routes", "converters")
                   for d in res.get(kind) or [] if isinstance(d, dict) and isinstance(d.get("grid"), dict)]
        return cell_mm_of(pitches, self.meta.get("resolution", 1.0))

    def worst(self, key: str) -> Optional[dict]:
        """The item with the lowest margin ('radiated_db' or 'conducted_db'), or None."""
        found = [i for i in self.items if isinstance(i.get(key), (int, float))]
        return min(found, key=lambda i: i[key]) if found else None

    def results(self) -> dict:
        """The run's results (the JSON report's data); HistoryError when the file is missing or not results."""
        try:
            data = json.loads((self.folder / "results.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise HistoryError(f"run {self.id}: results.json is missing or unreadable ({e})") from e
        if not isinstance(data, dict):
            raise HistoryError(f"run {self.id}: results.json is not a results file")
        return data

    def issues(self) -> list[dict]:
        """The run's issues (results.json), well-formed ones only."""
        issues = self.results().get("issues")
        return [i for i in issues if isinstance(i, dict)] if isinstance(issues, list) else []

    def settings_text(self) -> Optional[str]:
        """The settings file the run used; None when it is missing or unreadable."""
        try:
            return self.settings_file.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None


class History:
    """A history folder. Nothing is created until the first run is recorded. board: the board file name whose runs
    runs() lists (history_for sets it; "" lists every run in the folder)."""

    def __init__(self, root: Union[str, Path], board: str = ""):
        """The history at `root`, listing the runs of the board file `board` ("" for all)."""
        self.root = Path(root)
        self.board = board

    @property
    def boards_dir(self) -> Path:
        """Where the board copies are kept, gzipped, one per distinct board content."""
        return self.root / "boards"

    @property
    def runs_dir(self) -> Path:
        """Where each run's folder is."""
        return self.root / "runs"

    def board_copy_path(self, run: Run) -> Optional[Path]:
        """Where the run's board copy is named to be (it may have been deleted by hand), or None."""
        return self.boards_dir / run.board_name if run.board_name else None

    def _candidates(self, run: Run, runs: Optional[list[Run]] = None) -> list[Path]:
        """Files that may hold the run's board: its own copy, then any other run's copy of the same board (same
        hash), then the hash-named copy of the first history version. All are checked against the hash."""
        names = [run.board_name] if run.board_name else []
        if run.board_sha:
            names += [r.board_name for r in (self.all_runs() if runs is None else runs)
                      if r.board_sha == run.board_sha and r.board_name and r.board_name not in names]
            names.append(run.board_sha + BOARD_SUFFIX)
        return [self.boards_dir / n for n in names]

    # ------------------------------------------------------------------ writing
    def record(self, board: bytes, settings_text: str, results_json: str, meta: dict,
               now: Optional[datetime] = None) -> Run:
        """Keep one run: the board (stored once per distinct content), the settings, the results (the text of
        formats.to_json) and run.json with `meta` (how it ran: board name, source, resolution, what-if ...) and the
        headline figures. Returns the new Run."""
        results = json.loads(results_json)
        now = (now or datetime.now()).astimezone()
        sha = sha256(board)
        earlier = self.all_runs()
        folder = self._new_run_folder(now)
        self.boards_dir.mkdir(parents=True, exist_ok=True)
        name = next((r.board_name for r in earlier if r.board_sha == sha and r.board_name
                     and (self.boards_dir / r.board_name).is_file()), None)
        if name is None:                        # a board not kept yet (or its copy deleted): named after this run
            name = folder.name + BOARD_SUFFIX
            _write_atomic(self.boards_dir / name, gzip.compress(board, compresslevel=6, mtime=0))
        settings = settings_text.encode("utf-8")
        run_meta = {"format": FORMAT, "time": now.isoformat(timespec="seconds"), **meta,
                    "board": {**(meta.get("board") or {}), "sha256": sha, "bytes": len(board), "file": name},
                    "settings": {"sha256": sha256(settings)}, "note": "", **headline(results)}
        _write_atomic(folder / SETTINGS_NAME, settings)
        _write_atomic(folder / "results.json", results_json.encode("utf-8"))
        _write_atomic(folder / "run.json", json.dumps(run_meta, indent=2, ensure_ascii=False).encode("utf-8"))
        return Run(folder.name, folder, run_meta)

    def _new_run_folder(self, now: datetime) -> Path:
        """A new, empty run folder named after the time (made exclusively, so two processes never share one)."""
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        base = now.strftime("%Y-%m-%d_%H-%M-%S")
        for k in range(1, 1000):
            folder = self.runs_dir / (base if k == 1 else f"{base}-{k}")
            try:
                folder.mkdir()                  # exclusive: two runs in one second get -2, -3 ...
                return folder
            except FileExistsError:
                continue
        raise HistoryError(f"too many runs at {base}")

    def write_report(self, run: Run, text: str):
        """Keep the run's HTML report page."""
        _write_atomic(run.folder / REPORT_NAME, text.encode("utf-8"))

    def set_note(self, run: Run, note: str) -> Run:
        """Set the user's note on a run (one line, at most MAX_NOTE characters); returns the run as updated."""
        note = " ".join(str(note).split())[:MAX_NOTE]
        meta = {**self._read_meta(run.folder), "note": note}
        try:
            _write_atomic(run.folder / "run.json", json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
        except OSError as e:
            raise HistoryError(f"run {run.id}: the note could not be saved ({e})") from e
        return Run(run.id, run.folder, meta)

    def write_report_checked(self, run: Run, text: str):
        """write_report for a run that may have been deleted meanwhile: HistoryError instead of an OSError."""
        if not (run.folder / "run.json").is_file():
            raise HistoryError(f"run {run.id} was deleted before its report was written")
        try:
            self.write_report(run, text)
        except OSError as e:
            raise HistoryError(f"run {run.id}: the report could not be written ({e})") from e

    def delete(self, runs: list[Run]) -> int:
        """Remove these runs, then every board copy no run uses any more; a copy still used but named after a run
        that is gone is renamed after the earliest run using it. Returns the number of runs removed."""
        n = 0
        for r in runs:
            folder = self.runs_dir / r.id
            if not (RUN_ID.match(r.id) and folder.parent == self.runs_dir):
                continue
            try:
                shutil.rmtree(folder)
            except FileNotFoundError:           # deleted by hand meanwhile: gone either way
                pass
            except OSError as e:
                raise HistoryError(f"run {r.id} could not be deleted: {e}") from e
            n += 1
        left = self.all_runs()                  # every board's runs: copies are shared by hash
        self._rename_orphaned_copies(left)
        left = self.all_runs()
        used = {r.board_name for r in left if r.board_name} | {r.board_sha + BOARD_SUFFIX for r in left if r.board_sha}
        try:
            copies = list(self.boards_dir.glob("*" + BOARD_SUFFIX)) if self.boards_dir.is_dir() else []
        except OSError:
            copies = []
        for f in copies:
            if f.name not in used:
                try:
                    f.unlink(missing_ok=True)
                except OSError:                 # open elsewhere: the next delete tries again
                    pass
        return n

    def _rename_orphaned_copies(self, left: list[Run]):
        """A copy named after a deleted run but still used by others gets the earliest of their names, and their
        run.json follow (best effort: a run naming a file that is not there finds its board by hash)."""
        ids = {r.id for r in left}
        users: dict[str, list[Run]] = {}
        for r in left:
            if r.board_name:
                users.setdefault(r.board_name, []).append(r)
        for name, runs in users.items():
            if name[:-len(BOARD_SUFFIX)] in ids:
                continue
            first = min(runs, key=lambda r: _id_key(r.id))
            new = first.id + BOARD_SUFFIX
            try:
                if not (self.boards_dir / new).exists():
                    os.replace(self.boards_dir / name, self.boards_dir / new)
            except OSError:
                continue
            for r in runs:
                try:
                    meta = self._read_meta(r.folder)
                    if isinstance(meta.get("board"), dict):
                        meta["board"]["file"] = new
                        _write_atomic(r.folder / "run.json",
                                      json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
                except (OSError, HistoryError):
                    continue

    # ------------------------------------------------------------------ reading
    def _read_meta(self, folder: Path) -> dict:
        """A run folder's run.json as a record; HistoryError when it is unreadable."""
        try:
            meta = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise HistoryError(f"run {folder.name}: run.json unreadable ({e})") from e
        if not isinstance(meta, dict):
            raise HistoryError(f"run {folder.name}: run.json is not a record")
        return meta

    def runs(self) -> list[Run]:
        """The board's readable runs, newest first (a damaged or half-written one is left out; a run that names no
        board is shown too)."""
        if not self.board:
            return self.all_runs()
        mine = self.board.casefold()
        return [r for r in self.all_runs() if r.board_file_name.casefold() in ("", mine)]

    def all_runs(self) -> list[Run]:
        """Every readable run in the folder, whichever board, newest first."""
        try:
            folders = list(self.runs_dir.iterdir()) if self.runs_dir.is_dir() else []
        except OSError:                         # the folder went away meanwhile
            return []
        out = []
        for folder in folders:
            if RUN_ID.match(folder.name) and folder.is_dir():
                try:
                    out.append(Run(folder.name, folder, self._read_meta(folder)))
                except HistoryError:
                    continue
        return sorted(out, key=lambda r: _id_key(r.id), reverse=True)

    def find(self, ref: str, runs: Optional[list[Run]] = None) -> Run:
        """A run by its id, a unique start of it, or a number: 1 = the newest, 2 = the one before ..."""
        runs = self.runs() if runs is None else runs
        ref = str(ref).strip()
        if ref.isdigit() and len(ref) < 4:
            k = int(ref)
            if 1 <= k <= len(runs):
                return runs[k - 1]
            raise HistoryError(f"there are {len(runs)} run(s), not {k}")
        hits = [r for r in runs if r.id == ref] or [r for r in runs if r.id.startswith(ref)]
        if len(hits) != 1:
            raise HistoryError(f"no run {ref!r}" if not hits else f"{ref!r} matches {len(hits)} runs")
        return hits[0]

    def board_bytes(self, run: Run) -> bytes:
        """The run's board as it was simulated: its copy, else another copy of the same board (each checked
        against the run's hash)."""
        sha = run.board_sha
        if not sha:
            raise HistoryError(f"run {run.id} names no board copy")
        first_error = None
        for path in self._candidates(run):
            if not path.is_file():
                continue
            try:
                with gzip.open(path, "rb") as f:
                    data = f.read(MAX_BOARD_BYTES + 1)
            except (OSError, EOFError, ValueError) as e:     # gone, truncated, not gzip (zlib.error is an OSError)
                first_error = first_error or f"the board copy {path.name} is missing or damaged ({e})"
                continue
            if len(data) > MAX_BOARD_BYTES:
                first_error = first_error or f"the board copy {path.name} is larger than {MAX_BOARD_BYTES >> 20} MB"
            elif sha256(data) != sha:
                first_error = first_error or (f"the board copy {path.name} does not match its hash "
                                              "(changed or damaged)")
            else:
                return data
        raise HistoryError(f"run {run.id}: " + (first_error or "the board copy is missing or damaged (deleted?)"))

    def extract_board(self, run: Run, target: Union[str, Path]) -> Path:
        """Unpack the run's board copy (checked against its hash) to `target`; returns the path."""
        data = self.board_bytes(run)
        target = Path(target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(target, data)
        except OSError as e:
            raise HistoryError(f"the board copy could not be unpacked to {target} ({e})") from e
        return target


def _id_key(run_id: str) -> tuple[str, int]:
    """Sort key: the time, then the -2, -3 ... of runs in the same second."""
    k = run_id[20:]                     # after "YYYY-MM-DD_HH-MM-SS-"
    return run_id[:19], int(k) if k.isdigit() else 1


# --------------------------------------------------------------------------- comparing runs
def changes(runs: list[Run]) -> dict[str, list[str]]:
    """Per run id, what differs from the run before it (runs newest first, as History.runs gives them): 'board',
    'settings', 'resolution', 'what-if'; ['first'] for the oldest."""
    out = {}
    for k, r in enumerate(runs):
        if k + 1 >= len(runs):
            out[r.id] = ["first"]
            continue
        p = runs[k + 1]
        c = []
        if r.board_sha != p.board_sha:
            c.append("board")
        if r.settings_sha != p.settings_sha:
            c.append("settings")
        if r.meta.get("resolution") != p.meta.get("resolution"):
            c.append("resolution")
        if bool(r.meta.get("what_if")) != bool(p.meta.get("what_if")):
            c.append("what-if")
        out[r.id] = c
    return out


def _delta(a, b):
    """b - a when both are numbers, else None."""
    return b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None


def compare(a: Run, b: Run, settings_lines: int = 200) -> dict:
    """Run a (older) against run b (newer): per item its margins and loop figures in both and the change; the
    issues fixed and new (by fingerprint, waived ones left out); what changed in how they ran; the settings diff."""
    ia = {(i.get("kind"), i.get("name")): i for i in a.items}
    ib = {(i.get("kind"), i.get("name")): i for i in b.items}
    rows = []
    for key in list(ia) + [k for k in ib if k not in ia]:
        x, y = ia.get(key, {}), ib.get(key, {})
        row = {"kind": key[0], "name": key[1], "only": "a" if not y else "b" if not x else ""}
        for f in ("radiated_db", "conducted_db", "loop_nh", "l_ratio"):
            row[f] = (x.get(f), y.get(f), _delta(x.get(f), y.get(f)))
        row["radiated_f"] = (x.get("radiated_f"), y.get("radiated_f"))
        row["error"] = (x.get("error", ""), y.get("error", ""))
        rows.append(row)
    missing = []
    try:
        issues_a = [i for i in a.issues() if not i.get("waived")]
        issues_b = [i for i in b.issues() if not i.get("waived")]
        fa, fb = {i.get("fingerprint") for i in issues_a}, {i.get("fingerprint") for i in issues_b}
        fixed = [i for i in issues_a if i.get("fingerprint") not in fb]
        new = [i for i in issues_b if i.get("fingerprint") not in fa]
    except HistoryError as e:
        fixed = new = None
        missing.append(str(e))
    sa, sb = a.settings_text(), b.settings_text()
    if sa is None or sb is None:
        diff = []
        missing += [f"run {r.id}: {SETTINGS_NAME} is missing" for r, s in ((a, sa), (b, sb)) if s is None]
    else:
        diff = list(difflib.unified_diff(sa.splitlines(), sb.splitlines(), f"{a.id}/{SETTINGS_NAME}",
                                         f"{b.id}/{SETTINGS_NAME}", lineterm="", n=1))
    if len(diff) > settings_lines:
        diff = diff[:settings_lines] + [f"… {len(diff) - settings_lines} more line(s)"]
    how = []
    if a.board_sha != b.board_sha:
        how.append("the board changed")
    if a.settings_sha != b.settings_sha:
        how.append("the settings changed")
    for key, what in (("resolution", "resolution"), ("what_if", "what-if edits")):
        if a.meta.get(key) != b.meta.get(key):
            how.append(f"{what}: {_plain(a.meta.get(key))} → {_plain(b.meta.get(key))}")
    return {"a": a.id, "b": b.id, "rows": rows, "fixed": fixed, "new": new, "how": how, "settings_diff": diff,
            "missing": missing}


def _plain(v) -> str:
    """A run setting for the comparison text: on/off, a number as a factor ("2×"), "–" for none."""
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, (int, float)):
        return f"{v:g}×"
    return "–" if v is None else str(v)


def _num(v, fmt: str) -> str:
    """A number formatted, or "–" when it is not one."""
    return "–" if not isinstance(v, (int, float)) else fmt.format(v)


def comparison_text(cmp: dict, rows: bool = True) -> str:
    """The comparison for a console (CLI `groundtruth history --compare`); rows=False: without the per-item lines
    (the History tab shows those in a table)."""
    lines = [f"{cmp['a']} → {cmp['b']}" + (f" ({'; '.join(cmp['how'])})" if cmp["how"] else
                                           " (same board, settings and resolution)")]
    for r in cmp["rows"] if rows else ():
        if r["only"]:
            lines.append(f"{r['name']}: only in {cmp['a'] if r['only'] == 'a' else cmp['b']}")
            continue
        parts = []
        for f, label, fmt, dfmt in (("radiated_db", "radiated", "{:+.1f} dB", "{:+.1f} dB"),
                                    ("conducted_db", "conducted", "{:+.1f} dB", "{:+.1f} dB"),
                                    ("loop_nh", "L", "{:.2f} nH", "{:+.2f} nH"),
                                    ("l_ratio", "L/ideal", "{:.2f}", "{:+.2f}")):
            x, y, d = r[f]
            if x is None and y is None:
                continue
            parts.append(f"{label} {_num(x, fmt)} → {_num(y, fmt)}" + (f" ({dfmt.format(d)})" if d else ""))
        err = r["error"][1]
        lines.append(f"{r['name']}: " + (", ".join(parts) if parts else "no figures") + (f" — {err}" if err else ""))
    for m in cmp.get("missing") or ():
        lines.append(f"not compared: {m}")
    if cmp["fixed"] is None:
        lines.append("issues: not compared")
    else:
        for label, issues in (("fixed", cmp["fixed"]), ("new", cmp["new"])):
            lines.append(f"{label}: {len(issues)} issue(s)" + "".join(
                f"\n   - [{i.get('severity', '')} {i.get('check', '')}] {i.get('net', '')}: {i.get('message', '')}"
                for i in issues))
    if cmp["settings_diff"]:
        lines += ["settings:"] + ["   " + s for s in cmp["settings_diff"]]
    return "\n".join(lines)


def worst_text(item: Optional[dict], key: str) -> str:
    """'−22.9 dB conv1' for the History list."""
    return "–" if item is None else f"{item[key]:+.1f} dB {item.get('name', '')}"


def runs_text(runs: list[Run]) -> str:
    """The CLI's list of runs, newest first."""
    if not runs:
        return "no runs recorded yet"
    ch = changes(runs)
    lines = []
    for k, r in enumerate(runs, 1):
        c = r.counts
        lines.append(f"{k:3d}  {r.id}  {c.get('error', 0)} error(s) {c.get('warning', 0)} warning(s)  "
                     f"radiated {worst_text(r.worst('radiated_db'), 'radiated_db')}  "
                     f"conducted {worst_text(r.worst('conducted_db'), 'conducted_db')}  "
                     f"[{', '.join(ch[r.id]) or 'no change'}]" + (f"  “{r.note}”" if r.note else ""))
    return "\n".join(lines)
