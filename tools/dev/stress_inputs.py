"""Degenerate and extreme inputs (audit round 6, development aid): each case is simulated and must end in a clear
error or a finite, correct-looking number, never a silent NaN, an infinite value or a crash.

    .venv/Scripts/python tools/dev/stress_inputs.py [--cases zero_width,...]

Cases: zero_width (a 0 mm track), zero_cu (copper 0 µm thick in the stackup), zero_diel (two copper layers 0 mm
apart, plane capacitance on), zero_current, negative_current, nan_current, huge_current (1 MA), other_net_pin (the
load's pin on another net), thermal_runaway (a 0.05 mm track at 10 A, Thermal).
"""
import argparse
import math
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def _rail(tmp: Path, name: str, width=0.3, cu=0.035, diel=None, amps=1.0, load="U2.1", thermal=False,
          capacitance=False):
    """A 30 mm PWR track between two pads (2 layers, a GND plane on B.Cu), a DC load: the dcload result (and the
    thermal result)."""
    from boardgen import BoardGen
    from conftest import make
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile
    g = BoardGen(layers=2, size=(46.0, 20.0), cu_thickness=cu, dielectrics=diel)
    for ref, x in (("U1", 8.0), ("U2", 38.0)):
        g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, 10.0),
                    [{"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)},
                     {"number": 2, "at": (0, 3.0), "net": "GND", "size": (1.0, 1.0)}])
    g.track((8.0, 10.0), (38.0, 10.0), "PWR", "F.Cu", width=width)
    g.plane("GND", "B.Cu")
    b, sc, _ = make(tmp, g, name, nets=(), capacitance=capacitance)
    sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad(load, amps, "")], "")])
    if thermal:
        sc.doc["thermal"] = {"convection": "fixed", "h_top": 10.0, "h_bottom": 10.0, "radiation": False,
                             "quality": "draft"}
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    res = simulate(b, cfg)
    r = res.dc_loads[0] if res.dc_loads else None
    th = None
    if thermal and r is not None and not r.error:
        from groundtruth.analysis.thermal import run_thermal
        th = run_thermal(b, cfg, [r])[0]
    return r, th


CASES = {
    "zero_width": dict(width=0.0),
    "zero_cu": dict(cu=0.0),
    "zero_diel": dict(diel=[0.0], capacitance=True),
    "zero_current": dict(amps=0.0),
    "negative_current": dict(amps=-1.0),
    "nan_current": dict(amps=float("nan")),
    "huge_current": dict(amps=1e6),
    "other_net_pin": dict(load="U2.2"),
    "thermal_runaway": dict(width=0.05, amps=10.0, thermal=True),
}


def main() -> int:
    """The command line (see the module): one line per case, its outcome."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=",".join(CASES))
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        for name in args.cases.split(","):
            try:
                r, th = _rail(Path(tmp), name, **CASES[name])
            except Exception as e:      # noqa: BLE001 - what each case does is the point
                tb = traceback.extract_tb(e.__traceback__)[-1]
                print(f"{name:18s} RAISED {type(e).__name__}: {str(e)[:160]}  ({Path(tb.filename).name}:{tb.lineno})")
                continue
            if r is None:
                print(f"{name:18s} no DC load result")
                continue
            if r.error:
                print(f"{name:18s} error: {r.error[:200]}")
                continue
            ld = r.loads[0]
            vals = [ld["supply_mv"], r.dc[0]["loss_w"], r.dc[0]["sustained_a_mm2"], r.dc[0]["peak_a_mm2"]]
            bad = [v for v in vals if not math.isfinite(v)]
            msg = (f"{name:18s} drop {ld['supply_mv']:.6g} mV, loss {r.dc[0]['loss_w']:.4g} W, J "
                   f"{r.dc[0]['sustained_a_mm2']:.4g} A/mm²" + ("  NON-FINITE!" if bad else ""))
            if th is not None:
                msg += f"; thermal: {th.error or f't_max {th.t_max:.4g} °C'}"
            print(msg)
            notes = [n for n in r.notes if n]
            if notes:
                print(f"{'':18s} notes: {notes[:2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
