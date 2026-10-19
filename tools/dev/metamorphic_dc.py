"""Metamorphic checks of the DC solve (audit round 6, development aid): the same rail under transformations whose
effect is known exactly, printed as the drop's relative change.

    .venv/Scripts/python tools/dev/metamorphic_dc.py [--res 1]

The rail: a 0.3 mm PWR track with a bend, through a via to B.Cu and on to the load, over a GND plane on In1
(4 layers), 1 A. Transformations: rotated 90 / 180 / 270°, mirrored, shifted by sub-cell offsets, the track drawn
reversed, split into collinear pieces, drawn twice; source and load swapped; the current x 5; the copper twice as
thick (tracks only: the via's plating stays, so the drop is a little more than half); the board file's items in
another order. Exact physics: every one leaves the drop unchanged except the current (x 5) and the thickness.
"""
import argparse
import math
import random
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

CENTRE = (25.0, 20.0)


def _xf(p, rot: int = 0, mirror: bool = False, shift=(0.0, 0.0)):
    """A point about CENTRE: mirrored in x, rotated by rot·90°, then shifted."""
    x, y = p[0] - CENTRE[0], p[1] - CENTRE[1]
    if mirror:
        x = -x
    for _ in range(rot % 4):
        x, y = -y, x
    return (round(CENTRE[0] + x + shift[0], 6), round(CENTRE[1] + y + shift[1], 6))


def rail(rot=0, mirror=False, shift=(0.0, 0.0), reverse=False, split=False, double=False, cu=0.035, shuffle=False):
    """The rail's board generator under the transformation."""
    from boardgen import BoardGen
    f = lambda p: _xf(p, rot, mirror, shift)     # noqa: E731
    g = BoardGen(layers=4, size=(50.0, 40.0), cu_thickness=cu)
    for ref, at in (("U1", (10.0, 10.0)), ("U2", (40.0, 30.0))):
        g.footprint(ref, "IC", "Package_SO:Fake_IC", f(at), [{"number": 1, "at": (0, 0), "net": "PWR",
                                                             "size": (1.0, 1.0)}],
                    layer="F.Cu" if ref == "U1" else "B.Cu")
    top = [(10.0, 10.0), (25.0, 10.0), (25.0, 20.0)]
    bot = [(25.0, 20.0), (40.0, 20.0), (40.0, 30.0)]
    for pts, layer in ((top, "F.Cu"), (bot, "B.Cu")):
        segs = list(zip(pts[:-1], pts[1:]))
        for a, b in segs:
            if reverse:
                a, b = b, a
            if split:
                cuts = [a, (a[0] + 0.37 * (b[0] - a[0]), a[1] + 0.37 * (b[1] - a[1])),
                        (a[0] + 0.81 * (b[0] - a[0]), a[1] + 0.81 * (b[1] - a[1])), b]
                for u, v in zip(cuts[:-1], cuts[1:]):
                    g.track(f(u), f(v), "PWR", layer, width=0.3)
            else:
                g.track(f(a), f(b), "PWR", layer, width=0.3)
            if double:
                g.track(f(a), f(b), "PWR", layer, width=0.3)
    g.via(f((25.0, 20.0)), "PWR")
    g.plane("GND", "In1.Cu")
    if shuffle:
        random.Random(7).shuffle(g.items)
    return g


def drop(tmp: Path, g, name: str, resolution: float, swap: bool = False, amps: float = 1.0):
    """(supply drop mV, path loss W) of 'amps' from U1.1 to U2.1 (or the other way round)."""
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile, settings_path
    path = g.write(tmp / f"{name}.kicad_pcb")
    board = load_board(path)
    sc = SettingsFile.new(settings_path(path), board)
    sc.set_nets([])
    sc.doc["analysis"]["plane_capacitance"] = False
    src, dst = ("U2.1", "U1.1") if swap else ("U1.1", "U2.1")
    sc.set_dc_loads([DcLoadSpec("rail", src, [DcLoad(dst, amps, "")], "")])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
    r = simulate(board, cfg).dc_loads[0]
    assert not r.error, r.error
    return r.loads[0]["supply_mv"], r.dc[0]["loss_w"], board.fingerprint()


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--res", type=float, default=1.0)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        v0, p0, fp0 = drop(tmp, rail(), "base", args.res)
        print(f"base: {v0:.6f} mV, loss {p0 * 1e3:.6f} mW, loss / (I·drop) - 1 = {p0 / (v0 * 1e-3) - 1:+.2e}")
        cases = {f"rot{90 * k}": dict(rot=k) for k in (1, 2, 3)}
        cases.update({"mirror": dict(mirror=True), "mirror+rot90": dict(mirror=True, rot=1),
                      "shift 0.037,0.011": dict(shift=(0.037, 0.011)), "shift 0.11,0.173": dict(shift=(0.11, 0.173)),
                      "shift 1,2 (whole mm)": dict(shift=(1.0, 2.0)), "reversed": dict(reverse=True),
                      "split": dict(split=True), "drawn twice": dict(double=True), "file shuffled": dict(shuffle=True)})
        for name, kw in cases.items():
            v, p, fp = drop(tmp, rail(**kw), name.replace(" ", "_").replace(",", "_").replace("+", "_").replace(
                "(", "").replace(")", ""), args.res)
            same_fp = " (same fingerprint)" if fp == fp0 else ""
            print(f"{name:22s} drop {v / v0 - 1:+.2e}  loss {p / p0 - 1:+.2e}{same_fp}")
        v, p, _ = drop(tmp, rail(), "swap", args.res, swap=True)
        print(f"{'source <-> load':22s} drop {v / v0 - 1:+.2e}  loss {p / p0 - 1:+.2e}")
        v, p, _ = drop(tmp, rail(), "x5", args.res, amps=5.0)
        print(f"{'current x 5':22s} drop/5 {v / 5 / v0 - 1:+.2e}  loss/25 {p / 25 / p0 - 1:+.2e}")
        v, p, _ = drop(tmp, rail(cu=0.070), "cu70", args.res)
        print(f"{'copper 70 um':22s} drop ratio {v / v0:.5f} (tracks alone would be 0.5; the via stays)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
