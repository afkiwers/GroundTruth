"""Refill zones of a board copy with kicad-cli (for CI, where nobody pressed 'B' before saving)."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


class RefillError(RuntimeError):
    """kicad-cli is missing or failed: the message says which (the CLI reports it as a usage error)."""
    pass


def find_kicad_cli() -> Optional[str]:
    """kicad-cli's path: on the PATH, else the usual install places (the newest KiCad on Windows first), else
    None."""
    exe = shutil.which("kicad-cli")
    if exe:
        return exe
    candidates = sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\kicad-cli.exe"), reverse=True)
    candidates += ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli",
                   "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def refill_copy(board_path: str, workdir: Optional[str] = None, timeout: int = 600) -> str:
    """Copy the board (and its project/rules files) to a temp dir, refill all zones there with
    `kicad-cli pcb drc --refill-zones --save-board`, and return the path of the refilled copy.
    The original board is never modified."""
    exe = find_kicad_cli()
    if exe is None:
        raise RefillError("kicad-cli not found (install KiCad 10+ or use the kicad/kicad:10.0 image)")
    src = Path(board_path)
    tmp = Path(workdir or tempfile.mkdtemp(prefix="groundtruth-"))
    # the project and rules files hold the zone clearances and net classes the fill uses
    for sibling in [src, src.with_suffix(".kicad_pro"), src.with_suffix(".kicad_dru"), src.with_suffix(".kicad_prl")]:
        if sibling.exists():
            shutil.copy2(sibling, tmp / sibling.name)
    copy = tmp / src.name
    cmd = [exe, "pcb", "drc", "--refill-zones", "--save-board", "--output", str(tmp / "drc.rpt"), str(copy)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RefillError(f"kicad-cli failed: {e}") from e
    if proc.returncode not in (0, 5):  # 5 = DRC violations with --exit-code-violations; harmless here
        raise RefillError(f"kicad-cli exited with {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
    return str(copy)
