"""How the GPU path does against the CPU on this machine, alone and with other processes on the same GPU (development
aid, docs/NEXT_STEPS.md item 7): a two-stage buck board from tests/boardgen.py (test_split's "twins") simulated
in this process on the CPU and on the GPU, then N processes at once on the GPU, each with its wall time and
GMRES iterations. A process that does not finish in --timeout seconds is reported as such (and stopped).

    .venv/Scripts/python tools/dev/gpu_contention.py [--procs 2] [--timeout 300]
    .venv/Scripts/python tools/dev/gpu_contention.py --one gpu     # one run (what each child does)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def twins(folder: Path):
    """The board and settings of test_split.twins: two identical discrete bucks on one board."""
    from boardgen import discrete_buck_board
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.config.settings import SettingsFile, settings_path
    path = discrete_buck_board(out_connector=True).write(folder / "twins.kicad_pcb")
    board = load_board(path)
    sc = SettingsFile.new(settings_path(path), board)
    sc.set_nets([])
    sc.doc["analysis"]["plane_capacitance"] = False
    a = detect_converters(board, sc.config)[0].spec
    a.f_sw, a.i_out, a.v_in, a.v_out = 300e3, 3.0, 12.0, 3.0
    sc.set_converters([a, replace(a, name="twin")])
    sc.save()
    return board, SettingsFile.load(sc.path).config


def one(mode: str) -> dict:
    """One Simulate of the twins on `mode` (cpu | gpu): its wall time, device and solver summary."""
    from groundtruth.analysis.simulate import simulate
    with tempfile.TemporaryDirectory() as tmp:
        board, cfg = twins(Path(tmp))
        cfg.analysis.gpu = "off" if mode == "cpu" else "on"
        t0 = time.time()
        res = simulate(board, cfg)
        return {"mode": mode, "seconds": round(time.time() - t0, 1), "solver": res.solver,
                "loop_nh": [round(c.loop_nh or 0, 4) for c in res.converters]}


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--one", choices=("cpu", "gpu"))
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()
    if args.one:
        print(json.dumps(one(args.one)))
        return 0
    print("in process:", json.dumps(one("cpu")))
    print("in process:", json.dumps(one("gpu")))
    t0 = time.time()
    kids = [subprocess.Popen([sys.executable, __file__, "--one", "gpu"], stdout=subprocess.PIPE, text=True)
            for _ in range(args.procs)]
    for k, p in enumerate(kids):
        try:
            out, _ = p.communicate(timeout=max(1.0, args.timeout - (time.time() - t0)))
            print(f"process {k} of {args.procs} on the GPU:", out.strip().splitlines()[-1] if out.strip() else "?")
        except subprocess.TimeoutExpired:
            p.kill()
            print(f"process {k} of {args.procs} on the GPU: not finished after {args.timeout:g} s (stopped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
