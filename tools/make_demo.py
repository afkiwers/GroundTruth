"""Build the demo project shipped with the plugin (plugin/demo/, copied into KiCad's plugin folder by
tools/install_plugin.py).

    .venv/Scripts/python tools/make_demo.py            # writes plugin/demo/
    .venv/Scripts/python tools/make_demo.py --out DIR  # somewhere else

The board is the test suite's layer-change board (tests/boardgen.py layer_change_board): a 3.3 V, 50 MHz clock SIG
from U1 on F.Cu to a via at x = 20 mm, on to U2 on B.Cu, over two GND planes (In1, In2) joined only by the ICs' GND
vias at the ends: no stitching via at the layer change. KiCad's own CLI fills the zones and saves the board, so it
is in KiCad 10's own format. The settings file lists the net (reviewed) and the parts, with the default analysis.
README.txt says what to try. Run it again after changing the generator; the files are checked in.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

NAME = "groundtruth_demo"
README = """GroundTruth demo board
======================

A 40 x 20 mm, 4-layer board: the clock SIG runs from U1 on F.Cu to a via at x = 20 mm and on to U2 on B.Cu. F.Cu is
referenced to the GND plane on In1, B.Cu to the GND plane on In2. The two planes are joined only by the ICs' GND vias
at the ends: there is no stitching via where the signal changes layers.

Try it:
1. In KiCad: File > Open Project > {name}.kicad_pro (in this folder), then open its PCB editor.
2. Start GroundTruth (the toolbar button). The settings file groundtruth/{name}.groundtruth.yaml already lists the net.
3. Run checks: a missing stitching via at the layer change (layer_change_stitch).
4. Simulate, then the Results tab: on each GND plane the return current runs under its trace up to the layer change,
   then fans out over the whole plane to the far IC's GND via. Untick "Log scale" to see where most of it flows.
   Scroll down (the mouse wheel; Ctrl + wheel over a map zooms it): the cross-section under the maps follows the
   return along the route, layer by layer: In1 and In2 share it half
   and half at the layer change, and far from it about 30 % still flows on the plane the signal has left. The tile
   "plane displacement" (12.9x the signal current at 1450 MHz) is a resonance of the two planes' capacitance with
   the ICs' GND vias: a current circulating between them.
5. Fixes tab > Suggest fixes: a GND stitching via next to the signal via, predicted and verified. "Draw suggestions"
   puts it on the board; "Add ticked to what-if" and Simulate "with what-if edits" show the return staying in
   narrow bands under the traces, and the cross-section shows it swapping planes right at the vias.
6. Clear GT markers removes everything GroundTruth drew.

Reinstalling the plugin puts the demo back as it was (copy this folder elsewhere to keep your changes).
"""


def main() -> int:
    """Write the demo project (board from tests/boardgen.py, a minimal .kicad_pro, its settings with the example
    net) into --out; kicad-cli fills the zones and saves the board in KiCad's own format when it is installed."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "plugin" / "demo")
    args = ap.parse_args()
    from boardgen import layer_change_board
    from groundtruth.adapters.file_source import load_board
    from groundtruth.config.settings import NetSpec, SettingsFile, settings_path
    d = args.out
    d.mkdir(parents=True, exist_ok=True)
    pcb = layer_change_board(None).write(d / f"{NAME}.kicad_pcb")
    (d / f"{NAME}.kicad_pro").write_text(f'{{\n  "meta": {{\n    "filename": "{NAME}.kicad_pro",\n    "version": 3\n'
                                         '  }\n}\n', encoding="utf-8")
    cli = shutil.which("kicad-cli") or os.path.join(os.environ.get("ProgramFiles", ""), "KiCad", "10.0", "bin",
                                                     "kicad-cli.exe")
    if not os.path.exists(cli):
        print("kicad-cli not found: the zones stay unfilled (press B in KiCad)", file=sys.stderr)
    else:       # KiCad fills the zones and writes the board in its own format
        rpt = d / "drc.rpt"
        r = subprocess.run([cli, "pcb", "drc", "--refill-zones", "--save-board", "-o", str(rpt), str(pcb)],
                           capture_output=True, text=True)
        rpt.unlink(missing_ok=True)
        (d / f"{NAME}.kicad_prl").unlink(missing_ok=True)      # per-user view state: KiCad makes its own
        print("kicad-cli refill:", "ok" if r.returncode in (0, 5) else r.stderr.strip()[-300:])
    board = load_board(pcb)
    settings = settings_path(pcb)                  # demo/groundtruth/<name>.groundtruth.yaml
    settings.unlink(missing_ok=True)
    sc = SettingsFile.new(settings, board)
    sc.set_nets([NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9, signal_class="demo clock")])
    sc.save()
    SettingsFile.load(settings).config            # loads cleanly
    (d / "README.txt").write_text(README.format(name=NAME), encoding="utf-8")
    for p in sorted(d.iterdir()):
        print(f"  {p.name:36s} {p.stat().st_size:8d} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
