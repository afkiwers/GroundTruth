"""The KiPI half of tools/dev/kipi_crosscheck.py: KiPI's AC (PDN impedance) solve of one board, run with KiPI's own
Python environment (never GroundTruth's venv) and KiPI's source folder on the path.

    <kipi venv>/Scripts/python tools/dev/kipi_side.py --kipi <KiPI repo> CASE.json OUT.json

CASE.json (written by kipi_crosscheck.py): the board file, the rail and ground nets, the probe (an IC's supply and
ground pins), the capacitor models {ref: [C, ESR, ESL]}, the sweep and KiPI's mesh edge. The rail's regulator is
KiPI's "source" VRM on the probe's own pins: KiPI leaves VRMs out of the AC model (the open-loop PDN), as
GroundTruth's `source: none` does, so both see the copper, the planes and the capacitors only. OUT.json: the
frequencies, the probe's complex impedance, KiPI's plane capacitance, its field-solve frequencies and the time taken.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    """Solve the case with KiPI and write its curve."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kipi", required=True, help="KiPI's repository (its kipi package is imported from there)")
    ap.add_argument("case")
    ap.add_argument("out")
    args = ap.parse_args()
    sys.path.insert(0, str(Path(args.kipi).resolve()))
    from kipi.ac.model import CapModel, Probe
    from kipi.ac.solver import solve_ac
    from kipi.dc.pi.model import PinRef, PISetup, Vrm
    from kipi.sources.kicad_pcb.reader import read_board

    case = json.loads(Path(args.case).read_text(encoding="utf-8"))
    snap = read_board(case["board"])
    rail, gnd = case["rail"], case["ground"]
    ref, sup, gp = case["probe"]                        # the IC, its supply pin, its ground pin
    setup = PISetup()
    setup.vrms.append(Vrm("VRM1", PinRef(ref, rail, [sup]), case.get("voltage", 3.3),
                          ground=PinRef(ref, gnd, [gp]), bandwidth_khz=0.01))
    setup.probes.append(Probe(ref, PinRef(ref, rail, [sup]), PinRef(ref, gnd, [gp])))
    for name, (c, esr, esl) in case["caps"].items():
        setup.caps.append(CapModel(name, capacitance_f=c, esr_ohm=esr, esl_h=esl, refs=[name]))
    s = setup.ac
    s.rail, s.ground = "VRM1", gnd
    s.f_start_hz, s.f_stop_hz = case["f_start"], case["f_stop"]
    s.points_per_decade = case.get("points_per_decade", 50)
    s.field_points_per_decade = case.get("field_points_per_decade", 20)
    s.max_edge_mm = case.get("max_edge_mm", 1.0)
    s.dc_bias = False                                   # GroundTruth's models here are at their nominal C too
    t0 = time.perf_counter()
    res = solve_ac(snap, setup, "VRM1", gnd)
    z = res.probes[0].z
    out = {"f": [float(x) for x in res.freqs], "z_re": [float(v.real) for v in z], "z_im": [float(v.imag) for v in z],
           "plane_capacitance_f": float(res.plane_capacitance_f), "field_f": [float(x) for x in res.field_freqs],
           "seconds": time.perf_counter() - t0, "warnings": list(res.warnings), "caps": list(res.caps),
           "missing_caps": list(res.missing_caps)}
    Path(args.out).write_text(json.dumps(out), encoding="utf-8")
    print(f"KiPI: {len(out['f'])} points, plane C {out['plane_capacitance_f'] * 1e12:.1f} pF, "
          f"{out['seconds']:.0f} s, caps {out['caps']}, missing {out['missing_caps']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
