"""Install the groundtruth IPC plugin into every KiCad 10+ found for this user.

    python tools/install_plugin.py            # copy into <KiCad documents>/<ver>/plugins/io.github.iwers.groundtruth
    python tools/install_plugin.py --dest DIR # explicit plugins directory
    python tools/install_plugin.py --dry-run
    python tools/install_plugin.py --uninstall  # remove the copy (to use the Plugin and Content Manager's package)

Or as a package: tools/make_pcm_package.py builds a zip for Plugin and Content Manager > Install from File
(installed to <ver>/3rdparty/plugins/io_github_iwers_groundtruth). Keep only one of the two installed.

KiCad then creates the plugin's virtualenv from plugin/requirements.txt on first start.
The KiCad API must be enabled: Preferences > Plugins > "Enable KiCad API".
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ID = "io.github.iwers.groundtruth"
MIN_MAJOR = 10


def documents_dir() -> Path:
    """The user's Documents folder (handles OneDrive / folder redirection on Windows)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        from uuid import UUID

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", wintypes.BYTE * 8)]

        u = UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
        guid = GUID(u.fields[0], u.fields[1], u.fields[2], (wintypes.BYTE * 8).from_buffer_copy(u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            return Path(path)
    return Path.home() / "Documents"


def kicad_documents_homes() -> list[Path]:
    """KiCad's per-user document folders that exist: KICAD_DOCUMENTS_HOME when set, else Documents/KiCad (where
    Windows really keeps Documents, OneDrive included), and on Linux ~/.local/share/kicad too."""
    env = os.environ.get("KICAD_DOCUMENTS_HOME")
    if env:
        return [Path(env)]
    homes = [documents_dir() / "KiCad"]
    if sys.platform.startswith("linux"):
        homes.append(Path.home() / ".local" / "share" / "kicad")
    return [h for h in homes if h.exists()]


def plugin_dirs() -> list[Path]:
    """The plugins folder of every KiCad version from MIN_MAJOR up found in those homes."""
    out = []
    for home in kicad_documents_homes():
        for ver in sorted(home.iterdir()):
            m = re.fullmatch(r"(\d+)\.\d+", ver.name)
            if m and int(m.group(1)) >= MIN_MAJOR and ver.is_dir():
                out.append(ver / "plugins")
    return out


def install(dest_plugins: Path, dry_run: bool = False) -> Path:
    """Copy the plugin folder and the groundtruth package into dest_plugins/PLUGIN_ID, replacing what was there
    (KiCad's own files from opening the demo and Python caches left out). Returns the target."""
    target = dest_plugins / PLUGIN_ID
    print(f"installing into {target}")
    if dry_run:
        return target
    if target.exists():
        # empty it rather than remove it: a running plugin window (or OneDrive) may hold the folder itself open,
        # and Windows then refuses to delete it after its contents are already gone
        for child in target.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    # KiCad's own files from opening the demo in the repo (lock, per-user view state, backups) stay behind
    shutil.copytree(ROOT / "plugin", target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("~*.lck", "*.kicad_prl", "*-backups", ".history", "fp-info-cache",
                                                  "__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "src" / "groundtruth", target / "groundtruth",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for cmd in target.glob("*.cmd"):        # cmd.exe misreads labels and blocks in LF files: always CRLF
        cmd.write_bytes(cmd.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    (target / "version.txt").write_text(f"GroundTruth development install ({_commit()}, "
                                        f"{datetime.now():%Y-%m-%d %H:%M})\n", encoding="utf-8")
    return target


def _commit() -> str:
    """The repository's commit, "-dirty" with uncommitted changes under plugin/ or src/ (or "unknown")."""
    import subprocess
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                                check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "plugin", "src"], cwd=ROOT, capture_output=True,
                               text=True).stdout.strip()
        return commit + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def pcm_copy(dest_plugins: Path) -> Path:
    """Where the Plugin and Content Manager puts the same plugin (tools/make_pcm_package.py): 3rdparty/plugins,
    the identifier's dots as underscores."""
    return dest_plugins.parent / "3rdparty" / "plugins" / PLUGIN_ID.replace(".", "_")


def uninstall(dest_plugins: Path, dry_run: bool = False):
    """Remove the copied plugin from dest_plugins (its Python environment stays)."""
    target = dest_plugins / PLUGIN_ID
    if not target.exists():
        print(f"not installed in {dest_plugins}")
        return
    print(f"removing {target}")
    if not dry_run:
        shutil.rmtree(target)


def main() -> int:
    """The command line: install into every KiCad 10+ found (or --dest), warning when the Plugin and Content Manager
    installed GroundTruth too; or --uninstall."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, help="KiCad plugins directory to install into")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the copied plugin (before installing the package with the Plugin and Content "
                         "Manager: both installed show every button twice); its Python environment stays")
    args = ap.parse_args()
    dests = [args.dest] if args.dest else plugin_dirs()
    if not dests:
        print(f"no KiCad {MIN_MAJOR}+ documents folder found; pass --dest <plugins dir>", file=sys.stderr)
        return 1
    if args.uninstall:
        for d in dests:
            uninstall(d, args.dry_run)
        print("done. Restart KiCad.")
        return 0
    for d in dests:
        if pcm_copy(d).exists():
            print(f"warning: the Plugin and Content Manager installed GroundTruth too ({pcm_copy(d)}): KiCad will show "
                  "every button twice. Uninstall one of them (the manager's Installed tab, or --uninstall here).",
                  file=sys.stderr)
        install(d, args.dry_run)
    print("done. Close every KiCad window and start KiCad again (KiCad 10 has no plugin reload), then check that "
          "Preferences > Plugins > Enable KiCad API is still ticked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
