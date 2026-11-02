"""Cross-check GroundTruth's PDN impedance (analysis/pdn.py) against KiPI's AC solve (github.com/drewsum/KiPI, a 2.5D
finite-element PDN solver) on the same boards.

    .venv/Scripts/python tools/dev/kipi_crosscheck.py OUT_DIR --kipi <KiPI repo> --kipi-python <venv python>
        [--cases bare,near4,near_far] [--size 100x80] [--budget 40000] [--edge 1.0]

Each case is a tests/boardgen.pdn_board (a VDD plane over a GND plane 0.2 mm apart, FR4 εr 4.5, tan δ 0.02; an IC U1
with one supply and one ground pin, vias in its pads), its zones refilled by kicad-cli (antipads around the other
net's vias, as KiCad fills them), so both tools read exactly the same copper:
- bare: no capacitor: the planes' capacitance, then the cavity's resonances (f10 = c / (2·a·√εr));
- near4: four 1 µF 0402 around the IC (2 mm);
- near_far: one 1 µF beside the IC and one 10 µF 60 mm away (the planes' spreading inductance between them).
Both are open-loop (GroundTruth `source: none`; KiPI leaves VRMs out of its AC model) with the same capacitor models
(C, ESR, ESL as GroundTruth's settings give them). KiPI runs in its own environment (kipi_side.py; never this venv).
Writes OUT_DIR/<case>.png (both |Z| and their ratio) and prints the ratio by frequency band.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BANDS = [(1e4, 1e5), (1e5, 1e6), (1e6, 1e7), (1e7, 1e8), (1e8, 3e8), (3e8, 1e9)]


def cases(size) -> dict:
    """The boards by name: pdn_board's capacitor lists for a board of `size` mm (the IC at boardgen.PDN_IC)."""
    from boardgen import PDN_IC
    x, y = PDN_IC
    return {"bare": [],
            "near4": [("C1", "1u", (x + 2.0, y)), ("C2", "1u", (x - 2.0, y + 2.0)), ("C3", "1u", (x, y + 2.0)),
                      ("C4", "1u", (x, y - 2.0))],
            "near_far": [("C1", "1u", (x + 2.0, y)), ("C2", "10u", (min(x + 60.0, size[0] - 5.0), y + 20.0))]}


def refill(pcb: Path):
    """Fill the board's zones with kicad-cli (KiPI does not fill zones; GroundTruth reads the fill too)."""
    cli = shutil.which("kicad-cli") or os.path.join(os.environ.get("ProgramFiles", ""), "KiCad", "10.0", "bin",
                                                     "kicad-cli.exe")
    r = subprocess.run([cli, "pcb", "drc", "--refill-zones", "--save-board", "-o", str(pcb.with_suffix(".rpt")),
                        str(pcb)], capture_output=True, text=True)
    if r.returncode not in (0, 5):                      # 5: DRC violations (the generated board has some)
        raise SystemExit(f"kicad-cli refill failed: {r.stderr.strip()[-300:]}")


def ours(pcb: Path, budget: int, resolution: float = 1.0):
    """GroundTruth's PDN of the board's VDD rail at U1: (frequencies, |Z|, the capacitor models, seconds)."""
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import PdnSpec, SettingsFile, settings_path
    board = load_board(pcb)
    sc = SettingsFile.new(settings_path(pcb), board)
    sc.set_nets([])
    sc.doc["analysis"]["cavity_max_nodes"] = budget
    sc.set_pdn([PdnSpec("VDD", source="none", voltage=3.3, parts=["U1"])])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    cfg.analysis.resolution = resolution          # every model's node budget x this (the plugin's Resolution)
    caps = {}
    for ref in sorted(fp.reference for fp in board.footprints):
        m = cfg.model_for(ref)
        if m is not None and m.kind == "cap":
            caps[ref] = [m.get("C"), m.get("ESR"), m.get("ESL")]
    t0 = time.perf_counter()
    r = simulate(board, cfg)
    p = r.pdn[0]
    if p.error:
        raise SystemExit(f"GroundTruth: {p.error}")
    port = p.ports[0]
    import numpy as np
    z = np.asarray(port["z_ohm"]) * np.exp(1j * np.radians(port["phase_deg"]))
    return port["f"], z, caps, time.perf_counter() - t0


def theirs(pcb: Path, caps: dict, args, out: Path):
    """KiPI's curve of the same board (kipi_side.py in KiPI's environment): (frequencies, |Z|, its JSON)."""
    case = {"board": str(pcb), "rail": "VDD", "ground": "GND", "probe": ["U1", "1", "2"], "caps": caps,
            "f_start": 1e4, "f_stop": 1e9, "points_per_decade": 50, "field_points_per_decade": args.field_ppd,
            "max_edge_mm": args.edge}
    cj = out / f"{pcb.stem}_kipi_case.json"
    rj = out / f"{pcb.stem}_kipi.json"
    cj.write_text(json.dumps(case), encoding="utf-8")
    r = subprocess.run([args.kipi_python, str(ROOT / "tools" / "dev" / "kipi_side.py"), "--kipi", args.kipi,
                        str(cj), str(rj)], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise SystemExit(f"KiPI failed:\n{r.stderr[-2000:]}")
    print("  " + r.stdout.strip().splitlines()[-1])
    k = json.loads(rj.read_text(encoding="utf-8"))
    return k["f"], [complex(a, b) for a, b in zip(k["z_re"], k["z_im"])], k


def compare(name: str, f_g, z_g, f_k, z_k, out: Path):
    """The ratio GroundTruth / KiPI at GroundTruth's frequencies (KiPI's dense curve interpolated on log-log), by
    band, and a plot of both."""
    import numpy as np
    fg, zgc = np.asarray(f_g), np.asarray(z_g)
    fk, zkc = np.asarray(f_k), np.asarray(z_k)
    zg, z_k = np.abs(zgc), np.abs(zkc)
    zk_at = np.exp(np.interp(np.log(fg), np.log(fk), np.log(z_k)))
    # each tool's capacitance at 100 kHz and the inductance in series with it, L(f) = (Im Z + 1/(wC))/w: below the
    # cavity's resonance a plane pair fed at a point is C in series with its spreading (and the feed's) inductance
    lrow = {}
    for who, f, zc in (("GroundTruth", fg, zgc), ("KiPI", fk, zkc)):
        def at(x, f=f, zc=zc):
            """The complex Z at the solve nearest x."""
            return zc[int(np.argmin(np.abs(np.log(f / x))))], float(f[int(np.argmin(np.abs(np.log(f / x))))])
        z0, f0 = at(1e5)
        c = -1 / (2 * np.pi * f0 * z0.imag)
        ls = []
        for x in (3e7, 1e8, 2e8):
            z1, f1 = at(x)
            w = 2 * np.pi * f1
            ls.append((z1.imag + 1 / (w * c)) / w)
        lrow[who] = (c, ls)
        print(f"  {who}: C {c * 1e12:.1f} pF, series L at 30 / 100 / 200 MHz: "
              + " / ".join(f"{v * 1e9:.3f}" for v in ls) + " nH")
    db = 20 * np.log10(zg / zk_at)
    rows = []
    for lo, hi in BANDS:
        sel = (fg >= lo) & (fg <= hi)
        if sel.any():
            k = int(np.argmax(np.abs(db[sel])))
            rows.append((lo, hi, float(np.median(db[sel])), float(db[sel][k]), float(fg[sel][k])))
    # the deepest dip above 10 MHz (a series resonance: a capacitance with the inductance in series with it)
    for who, f, z in (("GroundTruth", fg, zg), ("KiPI", np.asarray(f_k), np.asarray(z_k))):
        hi = f > 1e7
        if hi.any() and np.argmin(z[hi]) not in (0, hi.sum() - 1):
            k = int(np.argmin(z[hi]))
            print(f"  {who}: deepest dip above 10 MHz {f[hi][k] / 1e6:.4g} MHz, {z[hi][k] * 1e3:.3g} mΩ")
    print(f"  {name}: GroundTruth / KiPI, dB per band (median, largest at f)")
    for lo, hi, med, worst, fw in rows:
        print(f"    {lo / 1e6:>8.3g}-{hi / 1e6:<6.3g} MHz  {med:+6.2f}  {worst:+6.2f} at {fw / 1e6:.4g} MHz")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, (a, b) = plt.subplots(2, 1, figsize=(9, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    a.loglog(fk, z_k, color="#d95f02", lw=1.5, label="KiPI (2.5D FEM, dense sweep)")
    a.loglog(fg, zg, "o-", color="#1b9e77", ms=4, lw=1.2, label="GroundTruth (PEEC, its solves)")
    a.set_ylabel("|Z| at U1 (Ω)")
    a.set_title(f"PDN impedance, case {name}")
    a.grid(True, which="both", alpha=0.3)
    a.legend()
    b.semilogx(fg, db, "o-", color="#555555", ms=3)
    b.axhline(0, color="#999999", lw=0.8)
    b.set_ylabel("GT / KiPI (dB)")
    b.set_xlabel("frequency (Hz)")
    b.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / f"{name}.png", dpi=110)
    plt.close(fig)
    return {"bands": rows, "l": {k: [v[0], v[1]] for k, v in lrow.items()}}


def radial(size: float, drill: float = 0.3) -> "object":
    """A square plane pair `size` mm wide on a 2-layer board 0.2 mm thick: VDD on F.Cu, GND on B.Cu, fed at its
    centre by U1 (its VDD pad on the VDD plane, its GND pad in the plane's clearance with one via to B.Cu). Pads and
    vias join the zones solidly (no thermal relief). Below the cavity's resonance: C in series with the spreading
    inductance mu0*h/(2*pi)*(ln(a/r) - 3/4) (a: the plate's equivalent radius, r: the feed's), so two sizes differ by
    mu0*h/(2*pi)*ln(a2/a1) whatever the feed's details."""
    from boardgen import BoardGen
    c = size / 2
    g = BoardGen(layers=2, size=(size, size), dielectrics=[0.2])
    g.plane("VDD", "F.Cu")
    g.plane("GND", "B.Cu")
    g.footprint("U1", "MCU", "Package_SO:Fake_IC", (c, c), [
        {"number": 1, "at": (-0.6, 0.0), "net": "VDD", "size": (0.6, 0.8), "pinfunction": "VDD", "pintype": "power_in"},
        {"number": 2, "at": (0.6, 0.0), "net": "GND", "size": (0.6, 0.8), "pinfunction": "GND", "pintype": "power_in"}])
    g.via((c + 0.6, c), "GND", size=drill + 0.3, drill=drill)    # the feed via (radialS_dD: drill D mm)
    return g


def solid_zones(pcb: Path):
    """Pads and vias join the zones solidly (KiCad's default is a thermal relief for pads), 0.2 mm clearance."""
    t = pcb.read_text(encoding="utf-8")
    t = t.replace("\t\t(min_thickness 0.2)", "\t\t(connect_pads yes\n\t\t\t(clearance 0.2)\n\t\t)\n"
                  "\t\t(min_thickness 0.2)")
    pcb.write_text(t, encoding="utf-8")


def main() -> int:
    """Run the cases (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--kipi", required=True, help="KiPI's repository")
    ap.add_argument("--kipi-python", required=True, help="the Python of an environment with KiPI's dependencies")
    ap.add_argument("--cases", default="bare,near4,near_far")
    ap.add_argument("--size", default="100x80", help="board size in mm")
    ap.add_argument("--budget", type=int, default=40000, help="GroundTruth's cavity node budget")
    ap.add_argument("--resolution", type=float, default=1.0, help="GroundTruth's Resolution (every node budget x)")
    ap.add_argument("--pitch", type=float, help="GroundTruth's PDN pitch in mm for every board (plane_pair_check.fixed_pitch)")
    ap.add_argument("--edge", type=float, default=1.0, help="KiPI's largest mesh edge (mm)")
    ap.add_argument("--field-ppd", type=int, default=20, help="KiPI's field solves per decade")
    args = ap.parse_args()
    from boardgen import pdn_board
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.pitch:                                  # one grid for every case: their differences cancel the feed
        from plane_pair_check import PITCH_SENTINEL, fixed_pitch
        fixed_pitch(args.pitch)
        args.budget = PITCH_SENTINEL
    size = tuple(float(v) for v in args.size.split("x"))
    summary = {}
    for name in args.cases.split(","):
        d = out / name
        d.mkdir(exist_ok=True)
        if name.startswith("radial"):                   # radial20, radial40: the centre-fed square plane pair
            caps = []
            sz, _, dr = name[6:].partition("_d")
            pcb = radial(float(sz), float(dr) if dr else 0.3).write(d / f"{name}.kicad_pcb")
            solid_zones(pcb)
        else:
            caps = cases(size)[name]
            pcb = pdn_board(caps=caps, size=size).write(d / f"{name}.kicad_pcb")
        refill(pcb)
        print(f"{name}: {len(caps)} capacitor(s), board {args.size} mm")
        f_g, z_g, models, t_g = ours(pcb, args.budget, args.resolution)
        print(f"  GroundTruth: {len(f_g)} solves, {t_g:.0f} s, models {models}")
        f_k, z_k, k = theirs(pcb, models, args, d)
        summary[name] = {**compare(name, f_g, z_g, f_k, z_k, out), "gt_s": t_g, "kipi_s": k["seconds"],
                         "kipi_plane_c_f": k["plane_capacitance_f"]}
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
