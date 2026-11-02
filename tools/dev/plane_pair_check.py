"""A plane pair's inductance per square against the parallel-plate line: L = mu0 * h * l / w (the cross-check with
KiPI found GroundTruth's PDN inductance on plane pairs ~1.7x high).

    .venv/Scripts/python tools/dev/plane_pair_check.py OUT_DIR [--h 0.2] [--w 10] [--lengths 20,40] [--resolution 1]
        [--kipi <KiPI repo> --kipi-python <venv python>]

A 2-layer board: VDD on F.Cu and GND on B.Cu, both a strip `w` mm wide and `l` mm long, `h` mm of FR4 apart. U1 feeds
the near end (its VDD pad on the VDD strip, its GND pad in the strip's clearance with a via to B.Cu); a row of links
across the far end joins VDD to GND (each a 0402 with a via in its GND pad, modelled as an ideal capacitor: 1 F,
1 µOhm, 1 fH, a short at the test frequencies, in both tools). Below the line's quarter-wave resonance the port is
the line's inductance plus the feed's and the links'; two lengths differ by mu0 * h * (l2 - l1) / w only (the feed
and the links cancel), with a few % less for the edge fringing (the field also spreads beyond the strips' edges).
Prints L at 1 / 3 / 10 MHz per length, the difference against the formula, and KiPI's when asked.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

MU0 = 4e-7 * math.pi
SHORT = (1.0, 1e-6, 1e-15)          # C, ESR, ESL of the far-end links: a short at MHz frequencies
FREQS = (1e6, 3e6, 1e7, 3e7, 1e8)


def board(length: float, w: float, h: float, axis: str = "x"):
    """The strip pair (see the module): along x (u = x, v = y) or along y (the same board turned: u = y, v = x, the
    parts at 270 degrees, KiCad's footprint rotation turning their local +x onto +y), the strips from u = 5 to
    5 + length, v = 5 to 5 + w. Along y the current runs in the model's y strips: a check of the y kernel."""
    from boardgen import BoardGen
    turned = axis == "y"

    def m(u, v):
        """Board coordinates of line coordinates (u along the line, v across it)."""
        return (v, u) if turned else (u, v)
    size = (length + 10.0, w + 10.0)
    g = BoardGen(layers=2, size=size[::-1] if turned else size, dielectrics=[h])
    u0, u1, v0, v1 = 5.0, 5.0 + length, 5.0, 5.0 + w
    rect = [m(u0, v0), m(u1, v0), m(u1, v1), m(u0, v1)]
    g.zone("VDD", "F.Cu", rect)
    g.zone("GND", "B.Cu", rect)
    vc = (v0 + v1) / 2
    angle = 270.0 if turned else 0.0
    g.footprint("U1", "MCU", "Package_SO:Fake_IC", m(u0 + 1.5, vc), [
        {"number": 1, "at": (-0.6, 0.0), "net": "VDD", "size": (0.6, 0.8), "pinfunction": "VDD", "pintype": "power_in"},
        {"number": 2, "at": (0.6, 0.0), "net": "GND", "size": (0.6, 0.8), "pinfunction": "GND", "pintype": "power_in"}],
        angle=angle)
    g.via(m(u0 + 2.1, vc), "GND")
    n = max(2, int(w // 1.0))                        # links 1 mm apart across the far end
    refs = []
    for k in range(n):
        v = v0 + (k + 0.5) * w / n
        ref = f"C{k + 1}"                          # C: KiPI attaches models to capacitors only
        g.two_pin(ref, "1F", m(u1 - 1.5, v), "VDD", "GND", kind="C", angle=angle)
        g.via(m(u1 - 1.0, v), "GND")
        refs.append(ref)
    return g, refs


def _l_at(fs, z, f: float) -> float:
    """The inductance (H) Im(Z)/w at the solve nearest f, at that solve's own frequency."""
    import numpy as np
    k = int(np.argmin(np.abs(np.log(fs / f))))
    return float(z[k].imag / (2 * math.pi * fs[k]))


PITCH_SENTINEL = 7_777_777     # a cavity budget that marks the main PDN model's pitch choice (fixed_pitch)


def fixed_pitch(pitch_mm: float):
    """Make the PDN's main model use `pitch_mm` (analysis/pdn calls _choose_pitch with the cavity budget, which the
    caller sets to PITCH_SENTINEL; the fine local models per IC keep their own choice): two boards of a pair then
    share one grid, so their difference cancels the feed exactly. Dev experiments only."""
    from groundtruth.analysis import pdn
    orig = pdn._choose_pitch

    def choose(h_mm, bbox, n_layers, max_nodes, grid_mm="auto", resolution=1.0):
        """The fixed pitch for the main model, the usual choice otherwise."""
        if max_nodes == PITCH_SENTINEL:
            return pitch_mm
        return orig(h_mm, bbox, n_layers, max_nodes, grid_mm, resolution)
    pdn._choose_pitch = choose


def ours(pcb: Path, refs: list, resolution: float, budget: int = 40000):
    """GroundTruth's port inductance at U1: {f: L (H)}."""
    import numpy as np
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.components.models import ComponentModel
    from groundtruth.config.settings import PdnSpec, SettingsFile, settings_path
    b = load_board(pcb)
    sc = SettingsFile.new(settings_path(pcb), b)
    sc.set_nets([])
    for r in refs:
        sc.set_component(r, ComponentModel("cap", {"C": SHORT[0], "ESR": SHORT[1], "ESL": SHORT[2]}))
    sc.set_pdn([PdnSpec("VDD", source="none", voltage=3.3, parts=["U1"])])
    sc.doc["analysis"]["cavity_max_nodes"] = budget          # the PDN model's pitch (coarser for fewer nodes)
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    cfg.analysis.resolution = resolution
    r = simulate(b, cfg)
    p = r.pdn[0]
    if p.error:
        raise SystemExit(p.error)
    port = p.ports[0]
    fs = np.asarray(port["f"])
    z = np.asarray(port["z_ohm"]) * np.exp(1j * np.radians(port["phase_deg"]))
    return {f: _l_at(fs, z, f) for f in FREQS}


def theirs(pcb: Path, refs: list, args, out: Path):
    """KiPI's port inductance at U1 (kipi_side.py in KiPI's environment): {f: L (H)}."""
    import numpy as np
    case = {"board": str(pcb), "rail": "VDD", "ground": "GND", "probe": ["U1", "1", "2"],
            "caps": {r: list(SHORT) for r in refs}, "f_start": 1e5, "f_stop": 2e8, "points_per_decade": 50,
            "field_points_per_decade": 20, "max_edge_mm": args.edge}
    cj, rj = out / f"{pcb.stem}_kipi_case.json", out / f"{pcb.stem}_kipi.json"
    cj.write_text(json.dumps(case), encoding="utf-8")
    r = subprocess.run([args.kipi_python, str(ROOT / "tools" / "dev" / "kipi_side.py"), "--kipi", args.kipi,
                        str(cj), str(rj)], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise SystemExit(f"KiPI failed:\n{r.stderr[-2000:]}")
    k = json.loads(rj.read_text(encoding="utf-8"))
    fs = np.asarray(k["f"])
    z = np.asarray(k["z_re"]) + 1j * np.asarray(k["z_im"])
    return {f: _l_at(fs, z, f) for f in FREQS}


def main() -> int:
    """Both lengths, both tools (see the module)."""
    from kipi_crosscheck import refill, solid_zones
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--h", type=float, default=0.2, help="dielectric between the strips, mm")
    ap.add_argument("--w", type=float, default=10.0, help="strip width, mm")
    ap.add_argument("--lengths", default="20,40")
    ap.add_argument("--resolution", type=float, default=1.0)
    ap.add_argument("--budget", type=int, default=40000, help="the cavity node budget (sets the pitch)")
    ap.add_argument("--pitch", type=float, help="the PDN model's pitch in mm for every board (fixed_pitch)")
    ap.add_argument("--axis", default="x", choices=["x", "y"], help="the line along x or y (the model's y strips)")
    ap.add_argument("--kipi")
    ap.add_argument("--kipi-python")
    ap.add_argument("--edge", type=float, default=1.0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.pitch:
        fixed_pitch(args.pitch)
        args.budget = PITCH_SENTINEL
    lengths = [float(v) for v in args.lengths.split(",")]
    res = {}
    for ln in lengths:
        g, refs = board(ln, args.w, args.h, args.axis)
        d = out / f"{args.axis}_l{ln:g}"
        d.mkdir(exist_ok=True)
        pcb = g.write(d / f"pp_l{ln:g}.kicad_pcb")
        solid_zones(pcb)
        refill(pcb)
        res[ln] = {"GroundTruth": ours(pcb, refs, args.resolution, args.budget)}
        if args.kipi:
            res[ln]["KiPI"] = theirs(pcb, refs, args, d)
        for who, zs in res[ln].items():
            print(f"l = {ln:g} mm, {who}: L at 1 / 3 / 10 / 30 / 100 MHz = "
                  + " / ".join(f"{zs[f] * 1e9:.4f}" for f in FREQS) + " nH")
    a, b = lengths[0], lengths[-1]
    want = MU0 * args.h * 1e-3 * (b - a) / args.w
    print(f"difference {b:g} - {a:g} mm, formula mu0*h*dl/w = {want * 1e9:.4f} nH (h {args.h} mm, w {args.w} mm)")
    for who in res[a]:
        f = FREQS[1]
        dl = res[b][who][f] - res[a][who][f]
        print(f"  {who}: {dl * 1e9:.4f} nH = {dl / want:.3f} x the formula at 3 MHz; at each f: " + " / ".join(
            f"{(res[b][who][x] - res[a][who][x]) / want:.3f}" for x in FREQS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
