"""Adversarial copper for the DC solve (audit round 6, development aid): shapes that challenge the PEEC grid, solved
at several resolutions and compared with the independent finite-difference sheet solve (tests/refsolvers.py, at
h = 0.02 and 0.01 mm so its own error shows).

    .venv/Scripts/python tools/dev/adversarial_dc.py [--cases neck_short,acute,...] [--res 1,2,4] [--json OUT]

Cases (1 A from U1.1 to U2.1, both 1 x 1 mm pads on F.Cu, net PWR, 2 layers, no plane):
  neck_short   a 0.1 mm neck 0.1 / 0.25 / 0.5 / 1 mm long in a 30 mm, 0.4 mm track (shorter than a cell): what it adds
  min_width    R' of 0.02 / 0.03 mm tracks (below the sub-grid's sample) from 20 and 40 mm against 1/(σwt)
  acute        a V of 0.25 mm track, 15 mm arms, interior angle 20° / 45° / 90°
  parallel     two pads joined by a 0.4 and a 0.1 mm track 2 mm apart: R and each branch's share of the current
  ring         a 0.3 mm annulus of radius 5 mm, the pads at opposite sides
  tee          a symmetric T (stem 10 mm, arms 10 mm, 0.3 mm), 0.5 A to each arm's pad
  wide_step    a 3 mm wide track (wider than 4 cells) stepping down to 0.1 mm: what the 0.1 mm part adds
  other_net    a 0.2 mm PWR track with a GND track 0.05 / 0.1 mm beside it: the drop against the track alone
  gap          a PWR track broken by a gap of 0.003 / 0.006 / 0.02 mm: joined or not connected
  corner       a 2 mm wide L: the density peak at the inner (re-entrant) corner and the sustained value by resolution
"""
import argparse
import json
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

T_CU = 35e-6


def _board(tmp: Path, name: str, size, tracks, pads, extra=None):
    """A 2-layer board with PWR pads (ref, (x, y)) and tracks ((a, b), width, net): the generator."""
    from boardgen import BoardGen
    g = BoardGen(layers=2, size=size)
    for ref, at in pads:
        g.footprint(ref, "IC", "Package_SO:Fake_IC", at, [{"number": 1, "at": (0, 0), "net": "PWR",
                                                         "size": (1.0, 1.0)}])
    for (a, b), w, net in tracks:
        g.track(a, b, net, "F.Cu", width=w)
    if extra:
        extra(g)
    return g


def _solve(tmp: Path, g, name: str, loads, resolution: float):
    """Simulate the DC load(s) [(pad, amps)] from U1.1 at `resolution`: the dcload result."""
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile, settings_path
    path = g.write(tmp / f"{name}.kicad_pcb")
    board = load_board(path)
    sc = SettingsFile.new(settings_path(path), board)
    sc.set_nets([])
    sc.doc["analysis"]["plane_capacitance"] = False
    sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad(p, a, "") for p, a in loads], "")])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
    return simulate(board, cfg).dc_loads[0], board


def _copper(tracks, pads):
    """The copper of the PWR tracks and pads as one shapely geometry (round-capped tracks)."""
    import shapely
    from shapely.geometry import LineString, box
    parts = [LineString([a, b]).buffer(w / 2, quad_segs=16) for (a, b), w, net in tracks if net == "PWR"]
    parts += [box(x - 0.5, y - 0.5, x + 0.5, y + 0.5) for _, (x, y) in pads]
    return shapely.union_all(parts)


def _pad(at):
    """A 1 x 1 mm pad's square at `at`."""
    from shapely.geometry import box
    return box(at[0] - 0.5, at[1] - 0.5, at[0] + 0.5, at[1] + 0.5)


def _ref(cu, ea, eb, hs=(0.02, 0.01)) -> dict:
    """The sheet solve's resistance (mΩ) at each h."""
    import refsolvers
    from groundtruth.solver.peec import SIGMA_CU
    return {h: refsolvers.sheet_resistance(cu, ea, eb, h, SIGMA_CU, T_CU) * 1e3 for h in hs}


def _fed(cu, feeds, hs=(0.02, 0.01)) -> dict:
    """The sheet solve with GroundTruth's own boundary condition (each pad fed uniformly over its area, the drop
    between feed-weighted potentials, refsolvers.sheet_potentials_fed): {h: [drop from the first pad to each other
    pad, mV per the currents given]}."""
    import refsolvers
    from groundtruth.solver.peec import SIGMA_CU
    out = {}
    for h in hs:
        v = refsolvers.sheet_potentials_fed(cu, feeds, h, SIGMA_CU, T_CU)
        out[h] = [(v[0] - x) * 1e3 for x in v[1:]]
    return out


def _drop(res, k: int = 0) -> float:
    """A load's supply drop (mV): mΩ at 1 A."""
    return float(res.loads[k]["supply_mv"])


def case_neck_short(tmp, resolutions):
    """A short 0.1 mm neck in a 0.4 mm track: what it adds, against the sheet solve."""
    rows = []
    y, x0, x1 = 10.0, 6.0, 36.0
    pads = [("U1", (x0, y)), ("U2", (x1, y))]
    base = [(((x0, y), (x1, y)), 0.4, "PWR")]
    r_base = {r: _drop(_solve(tmp, _board(tmp, "nb", (44, 20), base, pads), f"nb{r}", [("U2.1", 1.0)], r)[0])
              for r in resolutions}
    ref_base = _ref(_copper(base, pads), _pad(pads[0][1]), _pad(pads[1][1]))
    feeds = [(_pad(pads[0][1]), 1.0), (_pad(pads[1][1]), -1.0)]
    fed_base = _fed(_copper(base, pads), feeds)
    for L in (0.1, 0.25, 0.5, 1.0):
        xm0, xm1 = 21.0 - L / 2, 21.0 + L / 2
        tr = [(((x0, y), (xm0, y)), 0.4, "PWR"), (((xm0, y), (xm1, y)), 0.1, "PWR"), (((xm1, y), (x1, y)), 0.4, "PWR")]
        # the 0.4 mm pieces' round ends reach into the neck: cut the copper as KiCad would draw it
        ref = _ref(_copper(tr, pads), _pad(pads[0][1]), _pad(pads[1][1]))
        add_ref = {h: ref[h] - ref_base[h] for h in ref}
        fed = _fed(_copper(tr, pads), feeds)
        add_fed = {h: fed[h][0] - fed_base[h][0] for h in fed}
        for r in resolutions:
            add = _drop(_solve(tmp, _board(tmp, "n", (44, 20), tr, pads), f"n{L}_{r}".replace(".", "p"),
                               [("U2.1", 1.0)], r)[0]) - r_base[r]
            rows.append({"case": "neck_short", "neck_mm": L, "res": r, "added_mohm": add,
                         "ref_mohm": add_ref[0.01], "ref_h002": add_ref[0.02],
                         "err_rel": add / add_ref[0.01] - 1, "err_squares": (add - add_ref[0.01]) / (1e3 / (5.8e7 * T_CU)),
                         "fed_mohm": add_fed[0.01], "fed_h002": add_fed[0.02]})
    return rows


def case_min_width(tmp, resolutions):
    """R' of tracks narrower than the sub-grid's sample, from a 20 and a 40 mm track."""
    from groundtruth.solver.peec import SIGMA_CU
    rows = []
    for w in (0.02, 0.03):
        for r in resolutions:
            d = []
            for L in (20.0, 40.0):
                pads = [("U1", (6.0, 10.0)), ("U2", (6.0 + L, 10.0))]
                tr = [(((6.0, 10.0), (6.0 + L, 10.0)), w, "PWR")]
                d.append(_drop(_solve(tmp, _board(tmp, "w", (L + 14, 20), tr, pads), f"w{w}_{L}_{r}".replace(".", "p"),
                                      [("U2.1", 1.0)], r)[0]))
            rp = (d[1] - d[0]) / 20.0          # mΩ/mm
            exact = 1e3 / (SIGMA_CU * w * 1e-3 * T_CU) * 1e-3
            rows.append({"case": "min_width", "width_mm": w, "res": r, "r_per_mm": rp, "exact": exact,
                         "err_rel": rp / exact - 1})
    return rows


def case_acute(tmp, resolutions):
    """A V-shaped 0.25 mm track with an interior angle: the whole path against the sheet solve."""
    rows = []
    for ang in (20.0, 45.0, 90.0):
        a = math.radians(ang / 2)
        apex = (8.0, 20.0)
        p1 = (apex[0] + 15 * math.cos(a), apex[1] + 15 * math.sin(a))
        p2 = (apex[0] + 15 * math.cos(a), apex[1] - 15 * math.sin(a))
        pads = [("U1", p1), ("U2", p2)]
        tr = [((p1, apex), 0.25, "PWR"), ((apex, p2), 0.25, "PWR")]
        ref = _ref(_copper(tr, pads), _pad(p1), _pad(p2))
        fed = _fed(_copper(tr, pads), [(_pad(p1), 1.0), (_pad(p2), -1.0)])
        for r in resolutions:
            v = _drop(_solve(tmp, _board(tmp, "v", (30, 40), tr, pads), f"v{ang}_{r}".replace(".", "p"),
                             [("U2.1", 1.0)], r)[0])
            rows.append({"case": "acute", "angle": ang, "res": r, "r_mohm": v, "ref_mohm": ref[0.01],
                         "ref_h002": ref[0.02], "err_rel": v / ref[0.01] - 1, "fed_mohm": fed[0.01][0],
                         "fed_h002": fed[0.02][0], "err_fed": v / fed[0.01][0] - 1})
    return rows


def case_parallel(tmp, resolutions):
    """Two pads joined by a 0.4 and a 0.1 mm track: the total R and the share of each branch."""
    import numpy as np
    rows = []
    a, b = (6.0, 10.0), (36.0, 10.0)
    pads = [("U1", a), ("U2", b)]
    up = [((a, (8.0, 12.0)), 0.4, "PWR"), (((8.0, 12.0), (34.0, 12.0)), 0.4, "PWR"), (((34.0, 12.0), b), 0.4, "PWR")]
    dn = [((a, (8.0, 8.0)), 0.1, "PWR"), (((8.0, 8.0), (34.0, 8.0)), 0.1, "PWR"), (((34.0, 8.0), b), 0.1, "PWR")]
    ref = _ref(_copper(up + dn, pads), _pad(a), _pad(b))
    r_up = _ref(_copper(up, pads), _pad(a), _pad(b))[0.01]
    r_dn = _ref(_copper(dn, pads), _pad(a), _pad(b))[0.01]
    fed = _fed(_copper(up + dn, pads), [(_pad(a), 1.0), (_pad(b), -1.0)])
    for r in resolutions:
        res, board = _solve(tmp, _board(tmp, "p", (44, 20), up + dn, pads), f"p{r}", [("U2.1", 1.0)], r)
        name = next(iter(res.dc_maps))
        g, m = res.dc_grids[name]["F.Cu"], res.dc_maps[name]["F.Cu"]
        i = int(round((21.0 - g.x0) / g.d))
        ju = float(m[i, int(round((12.0 - g.y0) / g.d))])
        jd = float(m[i, int(round((8.0 - g.y0) / g.d))])
        iu, idn = ju * 0.4 * 0.035, jd * 0.1 * 0.035
        rows.append({"case": "parallel", "res": r, "r_mohm": _drop(res), "ref_mohm": ref[0.01], "ref_h002": ref[0.02],
                     "err_rel": _drop(res) / ref[0.01] - 1, "i_wide": iu, "i_narrow": idn,
                     "share_narrow": idn / (iu + idn), "share_ref": r_up / (r_up + r_dn),
                     "fed_mohm": fed[0.01][0], "fed_h002": fed[0.02][0], "err_fed": _drop(res) / fed[0.01][0] - 1})
    return rows


def case_ring(tmp, resolutions):
    """A 0.3 mm annulus of radius 5 mm (as 48 segments), the pads at opposite sides."""
    rows = []
    c, R, n = (15.0, 15.0), 5.0, 48
    pts = [(c[0] + R * math.cos(2 * math.pi * k / n), c[1] + R * math.sin(2 * math.pi * k / n)) for k in range(n + 1)]
    tr = [((pts[k], pts[k + 1]), 0.3, "PWR") for k in range(n)]
    a, b = (c[0] + R, c[1]), (c[0] - R, c[1])
    pads = [("U1", a), ("U2", b)]
    ref = _ref(_copper(tr, pads), _pad(a), _pad(b))
    fed = _fed(_copper(tr, pads), [(_pad(a), 1.0), (_pad(b), -1.0)])
    for r in resolutions:
        v = _drop(_solve(tmp, _board(tmp, "r", (30, 30), tr, pads), f"r{r}", [("U2.1", 1.0)], r)[0])
        rows.append({"case": "ring", "res": r, "r_mohm": v, "ref_mohm": ref[0.01], "ref_h002": ref[0.02],
                     "err_rel": v / ref[0.01] - 1, "fed_mohm": fed[0.01][0], "fed_h002": fed[0.02][0],
                     "err_fed": v / fed[0.01][0] - 1})
    return rows


def case_tee(tmp, resolutions):
    """A symmetric T, 0.5 A to each arm: by symmetry the arms' pads are equipotential, so the sheet solve with both as
    one electrode is the exact counterpart of the two current sinks."""
    import shapely
    rows = []
    s, j, l, rr = (20.0, 6.0), (20.0, 16.0), (10.0, 16.0), (30.0, 16.0)
    tr = [((s, j), 0.3, "PWR"), ((j, l), 0.3, "PWR"), ((j, rr), 0.3, "PWR")]
    pads = [("U1", s), ("U2", l), ("U3", rr)]
    ref = _ref(_copper(tr, pads), _pad(s), shapely.union_all([_pad(l), _pad(rr)]))
    fed = _fed(_copper(tr, pads), [(_pad(s), 1.0), (_pad(l), -0.5), (_pad(rr), -0.5)])
    for r in resolutions:
        res, _ = _solve(tmp, _board(tmp, "t", (40, 24), tr, pads), f"t{r}", [("U2.1", 0.5), ("U3.1", 0.5)], r)
        v = [_drop(res, k) for k in range(2)]
        rows.append({"case": "tee", "res": r, "drop_mv": v, "ref_mv": ref[0.01], "ref_h002": ref[0.02],
                     "err_rel": max(abs(x / ref[0.01] - 1) for x in v), "fed_mv": fed[0.01], "fed_h002": fed[0.02],
                     "err_fed": max(abs(x / f - 1) for x, f in zip(v, fed[0.01]))})
    return rows


def case_wide_step(tmp, resolutions):
    """A 3 mm wide track stepping down to a 0.1 mm one: what the narrow part adds (wider bands keep the border's
    width: the step within ±d/2, A39)."""
    rows = []
    y = 10.0
    pads = [("U1", (6.0, y)), ("U2", (36.0, y))]
    base = [(((6.0, y), (36.0, y)), 3.0, "PWR")]
    step = [(((6.0, y), (20.0, y)), 3.0, "PWR"), (((20.0, y), (36.0, y)), 0.1, "PWR")]
    ref_b, ref_s = (_ref(_copper(t, pads), _pad(pads[0][1]), _pad(pads[1][1])) for t in (base, step))
    feeds = [(_pad(pads[0][1]), 1.0), (_pad(pads[1][1]), -1.0)]
    fed_b, fed_s = (_fed(_copper(t, pads), feeds) for t in (base, step))
    for r in resolutions:
        vb = _drop(_solve(tmp, _board(tmp, "wb", (44, 20), base, pads), f"wb{r}", [("U2.1", 1.0)], r)[0])
        vs = _drop(_solve(tmp, _board(tmp, "ws", (44, 20), step, pads), f"ws{r}", [("U2.1", 1.0)], r)[0])
        add, add_ref = vs - vb, ref_s[0.01] - ref_b[0.01]
        rows.append({"case": "wide_step", "res": r, "added_mohm": add, "ref_mohm": add_ref,
                     "ref_h002": ref_s[0.02] - ref_b[0.02], "err_rel": add / add_ref - 1,
                     "err_squares": (add - add_ref) / (1e3 / (5.8e7 * T_CU)),
                     "fed_mohm": fed_s[0.01][0] - fed_b[0.01][0], "fed_h002": fed_s[0.02][0] - fed_b[0.02][0]})
    return rows


def case_other_net(tmp, resolutions):
    """A 0.2 mm PWR track with a GND track 0.05 / 0.1 mm beside it (closer than a cell): the PWR drop must be the
    track's alone (another net's copper never conducts its current)."""
    rows = []
    y = 10.0
    pads = [("U1", (6.0, y)), ("U2", (36.0, y))]
    alone = [(((6.0, y), (36.0, y)), 0.2, "PWR")]
    for r in resolutions:
        v0 = _drop(_solve(tmp, _board(tmp, "o0", (44, 20), alone, pads), f"o0_{r}", [("U2.1", 1.0)], r)[0])
        for gap in (0.05, 0.1):
            yo = y + 0.2 + gap
            tr = alone + [(((8.0, yo), (34.0, yo)), 0.2, "GND")]
            v = _drop(_solve(tmp, _board(tmp, "o", (44, 20), tr, pads), f"o{gap}_{r}".replace(".", "p"),
                             [("U2.1", 1.0)], r)[0])
            rows.append({"case": "other_net", "gap_mm": gap, "res": r, "drop_mv": v, "alone_mv": v0,
                         "err_rel": v / v0 - 1})
    return rows


def case_gap(tmp, resolutions):
    """A track broken by a small gap: joined at most PIECE_TOL_MM (5 µm, touching fills), else not connected."""
    rows = []
    y = 10.0
    pads = [("U1", (6.0, y)), ("U2", (36.0, y))]
    for gap in (0.003, 0.006, 0.02):
        # the two pieces' round ends: the gap is between the caps' tips
        tr = [(((6.0, y), (20.9 - gap / 2, y)), 0.2, "PWR"), (((21.1 + gap / 2, y), (36.0, y)), 0.2, "PWR")]
        for r in resolutions:
            res, _ = _solve(tmp, _board(tmp, "g", (44, 20), tr, pads), f"g{gap}_{r}".replace(".", "p"),
                            [("U2.1", 1.0)], r)
            rows.append({"case": "gap", "gap_mm": gap, "res": r, "error": res.error or "",
                         "drop_mv": None if res.error else _drop(res)})
    return rows


def case_corner(tmp, resolutions):
    """A 2 mm wide L-shaped track: its peak density (at the inner, re-entrant corner: singular in 2D, J ~ r^-1/3)
    and the sustained value the checks use, by resolution."""
    rows = []
    a, c, b = (6.0, 6.0), (26.0, 6.0), (26.0, 26.0)
    pads = [("U1", a), ("U2", b)]
    tr = [((a, c), 2.0, "PWR"), ((c, b), 2.0, "PWR")]
    for r in resolutions:
        res, _ = _solve(tmp, _board(tmp, "c", (34, 34), tr, pads), f"c{r}", [("U2.1", 10.0)], r)
        path = res.dc[0]
        rows.append({"case": "corner", "res": r, "peak": path["peak_a_mm2"], "peak_at": path["peak_at"],
                     "sustained": path["sustained_a_mm2"], "uniform": 10.0 / (2.0 * 0.035)})
    return rows


CASES = {k[5:]: v for k, v in dict(globals()).items() if k.startswith("case_")}


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=",".join(CASES))
    ap.add_argument("--res", default="1,2,4")
    ap.add_argument("--json")
    args = ap.parse_args()
    resolutions = [float(x) for x in args.res.split(",")]
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in args.cases.split(","):
            rows = CASES[name](Path(tmp), resolutions)
            for row in rows:
                print(json.dumps({k: (round(v, 6) if isinstance(v, float) else v) for k, v in row.items()}))
            sys.stdout.flush()
            out += rows
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
