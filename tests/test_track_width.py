"""Track width (VALIDATION_AUDIT.md round 5, "Track width"): a copper track's resistance, drop, loss, current density
and temperature through the whole chain (the board's copper on the PEEC grid, a DC load, the copper heat, the
thermal solve), against results that do not come from GroundTruth:

- a uniform track: R = ρ·L / (w·t), J = I / (w·t) [IEC 60028] (σ = 5.8e7 S/m at 20 °C);
- shapes that have no closed form (a neck, a corner, a via): an independent finite-difference sheet solve
  (tests/refsolvers.sheet_resistance);
- temperature: an independent 2D cross-section (tests/refsolvers.cross_section_rise).

The boundary condition everywhere is GroundTruth's: a DC load draws its set current (a current sink); the source
pin is the reference. So at a fixed current, a track narrowed from 0.4 to 0.1 mm has 4 x the resistance, drop, loss
and current density in that track. A track's temperature rise grows by less than its heat where the board spreads the
heat (cross_section_rise: 5.10 against 4.39 K per W/m, +16 %), more with ρ(T)."""
from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, box

import refsolvers
from boardgen import BoardGen
from conftest import make
from groundtruth.analysis.simulate import simulate
from groundtruth.analysis.thermal import thermal_inputs
from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile
from groundtruth.solver.peec import SIGMA_CU, _exact_copper, _line_cover, _ring_edges, build_grid

T_CU = 35e-6
RHO = 1 / SIGMA_CU
_CACHE: dict = {}


def _pads(g: BoardGen, a, b, layer_b: str = "F.Cu"):
    """U1 (pad 1 on F.Cu at a) and U2 (pad 1 at b on layer_b), 1 x 1 mm, both PWR."""
    g.footprint("U1", "IC", "Package_SO:Fake_IC", a, [{"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)}])
    g.footprint("U2", "IC", "Package_SO:Fake_IC", b, [{"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)}],
                layer=layer_b)


def _rail(tmp_path, g: BoardGen, name: str, amps: float = 1.0, resolution: float = 1.0):
    """Board `g` with one DC load of `amps` from U1.1 to U2.1, simulated (cached per name): (board, config,
    result)."""
    key = (name, amps, resolution)
    if key not in _CACHE:
        b, sc, _ = make(tmp_path, g, name, nets=())
        sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", amps, "")], "")])
        sc.save()
        cfg = SettingsFile.load(sc.path).config
        cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
        _CACHE[key] = (b, cfg, simulate(b, cfg).dc_loads[0])
    return _CACHE[key]


def _straight(length: float, width: float, angle: float = 0.0, dy: float = 0.0):
    """A PWR track `length` mm long at `angle`° from (6, 6 + dy) between two pads, a GND plane on In1 (4 layers).
    Returns (board generator, the track's midpoint)."""
    a = math.radians(angle)
    x0, y0 = 6.0, 6.0 + dy
    x1, y1 = x0 + length * math.cos(a), y0 + length * math.sin(a)
    g = BoardGen(layers=4, size=(x1 + 8, max(y1 + 8, 20.0)))
    _pads(g, (x0, y0), (x1, y1))
    g.track((x0, y0), (x1, y1), "PWR", "F.Cu", width=width)
    g.plane("GND", "In1.Cu")
    return g, ((x0 + x1) / 2, (y0 + y1) / 2)


def _r_per_m(tmp_path, width: float, angle: float = 0.0, dy: float = 0.0, resolution: float = 1.0) -> float:
    """The track's resistance per metre from the supply drops of a 20 and a 40 mm track at 1 A (the pads' share
    cancels in the difference)."""
    drops = []
    for length in (20.0, 40.0):
        g, _ = _straight(length, width, angle, dy)
        res = _rail(tmp_path, g, f"s{width}_{angle}_{dy}_{length}_{resolution}".replace(".", "p"),
                    resolution=resolution)[2]
        assert not res.error, res.error
        drops.append(res.loads[0]["supply_mv"] * 1e-3)
    return (drops[1] - drops[0]) / 0.020


def _density_at(res, x: float, y: float, layer: str = "F.Cu") -> float:
    """The DC map's current density (A/mm²) at the node nearest (x, y)."""
    name = next(iter(res.dc_maps))
    g, m = res.dc_grids[name][layer], res.dc_maps[name][layer]
    return float(m[int(round((x - g.x0) / g.d)), int(round((y - g.y0) / g.d))])


@pytest.mark.parametrize("width", [0.4, 0.1, 0.05])
@pytest.mark.parametrize("dy", [0.0, 0.03, 0.06])
def test_TW1_a_straight_track_has_the_resistance_and_density_of_its_cross_section(tmp_path, width, dy):
    """TW-1: R' = 1 / (σ·w·t) within 1 % and J = I / (w·t) within 2 % at the middle, for 0.4, 0.1 and 0.05 mm
    (narrower than the sub-grid's 0.12 mm samples) wherever the track lies on the grid. Before round 5 the strips
    took the sub-grid's whole samples: 0.4 mm read -12 to +17 %, 0.1 mm -37 to -15 %, by where it lay."""
    r = _r_per_m(tmp_path, width, dy=dy)
    assert r == pytest.approx(1 / (SIGMA_CU * width * 1e-3 * T_CU), rel=0.01)
    g, mid = _straight(40.0, width, dy=dy)
    res = _rail(tmp_path, g, f"s{width}_0.0_{dy}_40.0_1.0".replace(".", "p"))[2]
    assert _density_at(res, *mid) == pytest.approx(1.0 / (width * 0.035), rel=0.02)


def test_TW2_narrowing_a_track_from_0p4_to_0p1_mm_at_a_fixed_current(tmp_path):
    """TW-2: the same 1 A through a 0.4 and a 0.1 mm track (the load is a current sink): resistance, drop and loss
    of the track 4.00 x (within 1.5 %), its current density 4.00 x at the middle (2 %); the load's current is the
    set one in both."""
    r4, r1 = _r_per_m(tmp_path, 0.4), _r_per_m(tmp_path, 0.1)
    assert r1 / r4 == pytest.approx(4.0, rel=0.015)
    out = {}
    for w in (0.4, 0.1):
        g, mid = _straight(40.0, w)
        res = _rail(tmp_path, g, f"s{w}_0.0_0.0_40.0_1.0".replace(".", "p"))[2]
        out[w] = (res, mid)
        assert res.loads[0]["current_a"] == 1.0 and res.dc[0]["current_a"] == pytest.approx(1.0)
    (a, ma), (b, mb) = out[0.4], out[0.1]
    # the whole path also holds the pads (equal in both): its drop and loss grow by a little less than 4 x
    track4, track1 = r4 * 0.0390, r1 * 0.0390               # 40 mm minus the pads' half widths
    assert b.loads[0]["supply_mv"] - a.loads[0]["supply_mv"] == pytest.approx((track1 - track4) * 1e3, rel=0.02)
    assert b.dc[0]["loss_w"] - a.dc[0]["loss_w"] == pytest.approx(track1 - track4, rel=0.02)
    assert _density_at(b, *mb) / _density_at(a, *ma) == pytest.approx(4.0, rel=0.02)


@pytest.mark.parametrize("width", [0.4, 0.15, 0.1])
@pytest.mark.parametrize("angle", [30.0, 45.0])
def test_TW3_a_track_at_an_angle_keeps_its_resistance_and_the_heat_its_loss(tmp_path, width, angle):
    """TW-3: a track at 30° or 45° becomes a staircase of x and y strips; with each strip as wide as the border its
    copper crosses (w / cos θ, w / sin θ) it keeps R' = 1 / (σ·w·t) within 1.5 %, the copper heat the thermal solve
    gets is the path's own I²R within 0.5 %, and the sustained density is I / (w·t) within -3 / +8 %. Before round
    5: R' +20 to +57 % (the strips as wide as the track), the heat up to 2 x the loss (each node's ρJ²tA)."""
    r = _r_per_m(tmp_path, width, angle)
    assert r == pytest.approx(1 / (SIGMA_CU * width * 1e-3 * T_CU), rel=0.015)
    g, _ = _straight(40.0, width, angle)
    b, cfg, res = _rail(tmp_path, g, f"s{width}_{angle}_0.0_40.0_1.0".replace(".", "p"))
    heat, _, _ = thermal_inputs(b, cfg, [res])
    q = sum(float(np.sum(p)) for (_, _, _, p) in heat.values())
    assert q == pytest.approx(res.dc[0]["loss_w"], rel=0.005)
    j = 1.0 / (width * 0.035)
    assert 0.97 * j <= res.dc[0]["sustained_a_mm2"] <= 1.08 * j


def _neck_board(neck_len: float, neck_w: float = 0.1, total: float = 30.0, wide: float = 0.4):
    """A `total` mm PWR track, `wide` mm except a centred `neck_len` mm section `neck_w` wide (two pads, 2 layers,
    no plane): (generator, the copper as shapely geometry, the two pads)."""
    x0, x1 = 6.0, 6.0 + total
    xm0, xm1 = 6.0 + (total - neck_len) / 2, 6.0 + (total + neck_len) / 2
    y = 10.0
    g = BoardGen(layers=2, size=(x1 + 8, 20.0))
    _pads(g, (x0, y), (x1, y))
    pieces = [((x0, y), (xm0, y), wide), ((xm0, y), (xm1, y), neck_w), ((xm1, y), (x1, y), wide)]
    for a, b, w in pieces:
        g.track(a, b, "PWR", "F.Cu", width=w)
    pa, pb = box(x0 - 0.5, y - 0.5, x0 + 0.5, y + 0.5), box(x1 - 0.5, y - 0.5, x1 + 0.5, y + 0.5)
    cu = shapely.union_all([LineString([a, b]).buffer(w / 2, quad_segs=8) for a, b, w in pieces] + [pa, pb])
    return g, cu, pa, pb


@pytest.mark.parametrize("neck_len", [2.0, 20.0])
def test_TW4_a_neck_in_a_track_against_an_independent_sheet_solve(tmp_path, neck_len):
    """TW-4: a 30 mm, 0.4 mm track with a 0.1 mm neck (a short 2 mm one, a long 20 mm one): what the neck adds to the
    track's resistance (the same track without it subtracted, so the pads cancel) against the finite-difference
    sheet solve of the same copper (refsolvers.sheet_resistance, h = 0.01 mm; its own error from h = 0.02 mm): the
    neck's own ρL/(wt) and the current's constriction into and out of it.

    A strip that took the width of the border it crosses over its whole length d (a two-point flux [Patankar 1980])
    put an abrupt change of width anywhere within ±d/2 of where it is: up to (d/2)·(1/w_neck - 1/w_track) squares
    of error per end, -9 % of the 2 mm neck on 1x's 0.25 mm cells. A narrow band's series factor (peec._series_factor:
    the width along the strip at STRIP_SAMPLES lines, in series [A39]) places it to d/12: the difference is now a
    fixed amount per neck, under 0.2 square per end at any resolution (0.18 / 0.17 / 0.05 / 0.09 at 1 / 2 / 4 / 16x),
    -2.9 % of the 2 mm neck and -0.25 % of the 20 mm one at 1x. The density in the neck is I / (0.1 mm · t) within
    2 %, in the wide part I / (0.4 mm · t)."""
    g0, cu0, pa, pb = _neck_board(0.0)
    gn, cun, _, _ = _neck_board(neck_len)
    r0 = _rail(tmp_path, g0, "neck0")[2]
    rn = _rail(tmp_path, gn, f"neck{neck_len}".replace(".", "p"))[2]
    assert not r0.error and not rn.error
    d_gt = (rn.loads[0]["supply_mv"] - r0.loads[0]["supply_mv"]) * 1e-3
    ref = {h: refsolvers.sheet_resistance(cun, pa, pb, h, SIGMA_CU, T_CU)
           - refsolvers.sheet_resistance(cu0, pa, pb, h, SIGMA_CU, T_CU) for h in (0.02, 0.01)}
    assert ref[0.01] == pytest.approx(ref[0.02], rel=0.01)          # the reference itself is converged
    assert abs(d_gt - ref[0.01]) <= 2 * 0.25 * RHO / T_CU            # two ends, a quarter square each (ohms)
    assert d_gt == pytest.approx(ref[0.01], rel=0.04 if neck_len < 20.0 else 0.005)
    assert _density_at(rn, 21.0, 10.0) == pytest.approx(1.0 / (0.1 * 0.035), rel=0.02)
    assert _density_at(rn, 6.0 + 3.0, 10.0) == pytest.approx(1.0 / (0.4 * 0.035), rel=0.02)


def test_TW5_a_corner_where_the_track_narrows_against_the_sheet_solve(tmp_path):
    """TW-5: 15 mm of 0.4 mm track, a right-angle corner, then 15 mm of 0.1 mm (the narrowing at the corner) between
    two pads: the whole path's resistance against the independent sheet solve of the same copper within 2 % (the
    pads' feeds differ a little: GroundTruth spreads the current over a pad, the reference holds it equipotential)."""
    a, c, b = (6.0, 6.0), (21.0, 6.0), (21.0, 21.0)
    g = BoardGen(layers=2, size=(30.0, 30.0))
    _pads(g, a, b)
    g.track(a, c, "PWR", "F.Cu", width=0.4)
    g.track(c, b, "PWR", "F.Cu", width=0.1)
    res = _rail(tmp_path, g, "corner")[2]
    assert not res.error, res.error
    pa, pb = box(a[0] - 0.5, a[1] - 0.5, a[0] + 0.5, a[1] + 0.5), box(b[0] - 0.5, b[1] - 0.5, b[0] + 0.5, b[1] + 0.5)
    cu = shapely.union_all([LineString([a, c]).buffer(0.2, quad_segs=8), LineString([c, b]).buffer(0.05, quad_segs=8),
                            pa, pb])
    ref = refsolvers.sheet_resistance(cu, pa, pb, 0.01, SIGMA_CU, T_CU)
    assert res.loads[0]["supply_mv"] * 1e-3 == pytest.approx(ref, rel=0.02)


def test_TW6_a_track_through_a_via(tmp_path):
    """TW-6: 15 mm of 0.4 mm on F.Cu, a 0.3 mm via (0.6 mm pad), 15 mm of 0.1 mm on B.Cu: the drop is the two
    tracks' (each against the sheet solve, pad and via pad as electrodes) plus the barrel's ρ·L / (π·d·t_plating)
    [A3] within 2 %."""
    a, v, b = (6.0, 10.0), (21.0, 10.0), (36.0, 10.0)
    g = BoardGen(layers=2, size=(44.0, 20.0))
    _pads(g, a, b, layer_b="B.Cu")
    g.track(a, v, "PWR", "F.Cu", width=0.4)
    g.via(v, "PWR", ("F.Cu", "B.Cu"), size=0.6, drill=0.3)
    g.track(v, b, "PWR", "B.Cu", width=0.1)
    res = _rail(tmp_path, g, "via")[2]
    assert not res.error, res.error
    ring = shapely.Point(v).buffer(0.3, quad_segs=16)
    pa, pb = box(a[0] - 0.5, a[1] - 0.5, a[0] + 0.5, a[1] + 0.5), box(b[0] - 0.5, b[1] - 0.5, b[0] + 0.5, b[1] + 0.5)
    top = refsolvers.sheet_resistance(shapely.union_all([LineString([a, v]).buffer(0.2, quad_segs=8), pa, ring]),
                                      pa, ring, 0.01, SIGMA_CU, T_CU)
    bot = refsolvers.sheet_resistance(shapely.union_all([LineString([v, b]).buffer(0.05, quad_segs=8), pb, ring]),
                                      ring, pb, 0.01, SIGMA_CU, T_CU)
    length = 1.51e-3 + 0.035e-3                     # the barrel from foil centre to foil centre (2-layer default)
    barrel = RHO * length / (math.pi * 0.3e-3 * 25e-6)
    assert res.loads[0]["supply_mv"] * 1e-3 == pytest.approx(top + barrel + bot, rel=0.02)


def test_TW13_a_via_s_barrel_heats_the_board_with_its_own_loss(tmp_path):
    """TW-13: the TW-6 board (15 mm of 0.4 mm on F.Cu, a via, 15 mm of 0.1 mm on B.Cu) at 1 A: the heat Thermal gets
    is the path's whole loss within 0.2 %, the barrel's ρ·L/(π·d·t)·I² included (half at each of its layers, at the
    via). Before round 5 the barrels were left out (A43): the heat was the loss less theirs."""
    a, v, b = (6.0, 10.0), (21.0, 10.0), (36.0, 10.0)
    g = BoardGen(layers=2, size=(44.0, 20.0))
    _pads(g, a, b, layer_b="B.Cu")
    g.track(a, v, "PWR", "F.Cu", width=0.4)
    g.via(v, "PWR", ("F.Cu", "B.Cu"), size=0.6, drill=0.3)
    g.track(v, b, "PWR", "B.Cu", width=0.1)
    board, cfg, res = _rail(tmp_path, g, "via")
    heat, _, _ = thermal_inputs(board, cfg, [res])
    total = sum(float(np.sum(p)) for (_, _, _, p) in heat.values())
    assert total == pytest.approx(res.dc[0]["loss_w"], rel=0.002)
    from groundtruth.analysis.thermal import _via_power, _via_segments
    barrel = RHO * (1.51e-3 + 0.035e-3) / (math.pi * 0.3e-3 * 25e-6)       # Ω, so W at 1 A
    segs = _via_segments([res])
    assert len(segs) == 1 and {segs[0]["a"], segs[0]["b"]} == {"F.Cu", "B.Cu"}
    assert _via_power(segs[0]) == pytest.approx(barrel, rel=0.01)


def test_TW7_a_track_with_a_gap_is_not_connected(tmp_path):
    """TW-7: a 0.1 mm track interrupted by a 0.2 mm gap: the rail is an error (its copper does not join the pads),
    not a finite drop."""
    g = BoardGen(layers=2, size=(40.0, 20.0))
    _pads(g, (6.0, 10.0), (36.0, 10.0))
    g.track((6.0, 10.0), (20.9, 10.0), "PWR", "F.Cu", width=0.1)
    g.track((21.15, 10.0), (36.0, 10.0), "PWR", "F.Cu", width=0.1)
    res = _rail(tmp_path, g, "gap")[2]
    assert res.error


@pytest.mark.parametrize("gap, joined", [(1e-7, True), (0.02, False)])
def test_TW12_copper_that_touches_is_one_piece_and_a_real_gap_is_not(tmp_path, gap, joined):
    """TW-12: two 10 x 4 mm PWR fills end to end, `gap` mm apart, a pad on each. 1e-7 mm (fills that touch but a
    union keeps apart: refboard's +VDC on In1.Cu) conducts as the same copper drawn as one 20 mm fill, its
    drop within 0.5 %; 0.02 mm (above the 5 µm tolerance, below any cell) is not connected. With each polygon a
    piece of its own, the touching fills were cut apart (the LM5008's input on refboard, round 5)."""
    def board(split: bool):
        """The two fills (`gap` apart) or one fill over both."""
        g = BoardGen(layers=2, size=(30.0, 12.0))
        _pads(g, (6.0, 6.0), (24.0, 6.0))
        if split:
            g.zone("PWR", "F.Cu", [(5.0, 4.0), (15.0, 4.0), (15.0, 8.0), (5.0, 8.0)])
            g.zone("PWR", "F.Cu", [(15.0 + gap, 4.0), (25.0, 4.0), (25.0, 8.0), (15.0 + gap, 8.0)])
        else:
            g.zone("PWR", "F.Cu", [(5.0, 4.0), (25.0, 4.0), (25.0, 8.0), (5.0, 8.0)])
        return g

    res = _rail(tmp_path, board(True), f"touch{gap}".replace(".", "p").replace("-", "m"))[2]
    if not joined:
        assert res.error
        return
    assert not res.error, res.error
    whole = _rail(tmp_path, board(False), "touch_whole")[2]
    assert res.loads[0]["supply_mv"] == pytest.approx(whole.loads[0]["supply_mv"], rel=0.005)


def test_TW8_another_net_s_pour_beside_a_track_does_not_dilute_its_density(tmp_path):
    """TW-8: a GND pour on the same layer 0.2 mm from a 0.1 mm track: the sustained density (over 1 mm) stays the
    track's own within 1 %. Before round 5 the window counted the pour as copper at 0 A/mm²: a third less."""
    out = {}
    for gap in (None, 0.2):
        y, w = 10.0, 0.1
        g = BoardGen(layers=4, size=(56.0, 20.0))
        _pads(g, (6.0, y), (46.0, y))
        g.track((6.0, y), (46.0, y), "PWR", "F.Cu", width=w)
        if gap is not None:
            g.zone("GND", "F.Cu", [(7.0, 1.0), (45.0, 1.0), (45.0, y - w / 2 - gap), (7.0, y - w / 2 - gap)])
        g.plane("GND", "In1.Cu")
        res = _rail(tmp_path, g, f"pour{gap}".replace(".", "p"))[2]
        assert not res.error, res.error
        out[gap] = res.dc[0]["sustained_a_mm2"]
    assert out[0.2] == pytest.approx(out[None], rel=0.01)


def test_TW9_the_exact_coverage_measures_borders_and_areas_like_shapely():
    """TW-9: _line_cover's covered lengths on lines across a pour with a hole and a diagonal track against
    shapely's own intersections (to 1e-9 mm), and _exact_copper's widths and areas: each net's copper area kept
    (the slivers moved to a node, never lost while one is near) within 0.5 % of the polygons' area."""
    pour = box(1.0, 1.0, 9.0, 6.0).difference(box(3.0, 2.5, 5.0, 4.0))
    track = LineString([(1.0, 7.5), (9.0, 11.0)]).buffer(0.075, quad_segs=8)
    geom = shapely.union_all([pour, track])
    pa, pb = _ring_edges(geom)
    d = 0.237
    xs = 0.5 + np.arange(30) * d / 2
    cover = _line_cover(pa[:, 0], pa[:, 1], pb[:, 0], pb[:, 1], 0.5, d / 2, 30, 0.5, d, 50)
    for m, x in enumerate(xs):
        for c in range(50):
            yc = 0.5 + c * d
            seg = LineString([(x, yc - d / 2), (x, yc + d / 2)])
            assert cover[m, c] == pytest.approx(geom.intersection(seg).length, abs=1e-9), (x, yc)
    # every cell's copper area against shapely's intersection of the cell with the copper
    from groundtruth.solver.peec import _cell_areas
    xl, yl, wi, wj = 0.3, 0.2, 44, 52
    right = _line_cover(pa[:, 0], pa[:, 1], pb[:, 0], pb[:, 1], xl + d, d, wi, yl + d / 2, d, wj)
    areas = _cell_areas(pa, pb, xl, yl, d, wi, wj, right)
    for i in range(wi):
        for j in range(wj):
            cell = box(xl + i * d, yl + j * d, xl + (i + 1) * d, yl + (j + 1) * d)
            assert areas[i, j] == pytest.approx(geom.intersection(cell).area, abs=1e-9), (i, j)
    bb = (0.0, 0.0, 10.0, 12.0)
    g = build_grid({"A": pour, "B": track}, bb, d, lines={"B": [([(1.0, 7.5), (9.0, 11.0)], 0.15)]}, sub=2)
    for k, poly in enumerate((pour, track)):
        on = g.label == k
        assert float(np.sum(np.where(on, g.area, 0.0))) * d * d == pytest.approx(poly.area, rel=0.005)
    ex_x, ex_y, area, _, _ = _exact_copper([pour, track], g.label, g.xb, g.yb, g.x0, g.y0, d)
    assert np.allclose(area, g.area)


@pytest.fixture(scope="module")
def thermal_tracks(tmp_path_factory):
    """A 100 mm track at 1 A across a 60 mm wide 2-layer board, fixed h = 10 W/m²K on both faces, no radiation,
    25 °C, for 0.4 and 0.1 mm: {width: (node temperature rise at the middle K, the map's there, the reference)}."""
    from groundtruth.analysis import thermal as T
    from groundtruth.thermal.materials import COPPER
    tmp = tmp_path_factory.mktemp("thermal_tracks")
    out = {}
    for w in (0.4, 0.1):
        g = BoardGen(layers=2, size=(116.0, 60.0))
        _pads(g, (8.0, 30.0), (108.0, 30.0))
        g.track((8.0, 30.0), (108.0, 30.0), "PWR", "F.Cu", width=w)
        b, sc, _ = make(tmp, g, f"th{w}".replace(".", "p"), nets=())
        sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 1.0, "")], "")])
        sc.doc["thermal"] = {"convection": "fixed", "h_top": 10.0, "h_bottom": 10.0, "radiation": False,
                             "ambient_c": 25.0, "quality": "normal"}
        sc.save()
        cfg = SettingsFile.load(sc.path).config
        res = simulate(b, cfg).dc_loads[0]
        heat, losses, notes = T.thermal_inputs(b, cfg, [res])
        m = T._build_model(b, cfg, heat, losses, list(notes), lambda *a: None, None)
        sol = T._coupled(m, m.p20, m.Qc)
        col = m.mesh.locate(np.array([58.0]), np.array([30.0]))
        node = float(sol.T[m.th.cu_nodes(col, np.zeros(1, np.int64))][0]) - 25.0
        th, _ = T._result(b, cfg, m, sol, losses)
        gg, mm = th.layer_grids["F.Cu"], th.sheet_current["F.Cu"]
        i, j = int(round((58.0 - gg.x0) / gg.d)), int(round((30.0 - gg.y0) / gg.d))
        shown = float(np.nanmax(mm[i - 1:i + 2, j - 1:j + 2])) - 25.0
        q20 = 1.0 / (SIGMA_CU * w * 1e-3 * T_CU)                 # W/m at 20 °C
        a = refsolvers.cross_section_rise(w, 1.0)                # K per W/m
        # ρ(T) [IEC 60028]: θ = a·q20·(1 + α(T_amb - 20 + θ)) solved for θ
        ref = a * q20 * (1 + COPPER.alpha * 5.0) / (1 - a * q20 * COPPER.alpha)
        out[w] = (node, shown, ref)
    return out


def test_TW10_a_track_s_temperature_against_an_independent_cross_section(thermal_tracks):
    """TW-10: the thermal solve's copper temperature at the middle of a long 0.4 and 0.1 mm track at 1 A against the
    2D cross-section (refsolvers.cross_section_rise: the laminate's k_xy / k_z, the copper, convection on both
    faces, ρ(T)) within 3 %, and so the rise's ratio 0.1 / 0.4 mm (about 5: 4 x the heat, +16 % for the narrower
    spreading, +9 % for ρ(T)) within 3 %."""
    for w, (node, _, ref) in thermal_tracks.items():
        assert node == pytest.approx(ref, rel=0.03), w
    ratio = thermal_tracks[0.1][0] / thermal_tracks[0.4][0]
    assert ratio == pytest.approx(thermal_tracks[0.1][2] / thermal_tracks[0.4][2], rel=0.03)
    assert 4.5 < ratio < 5.5


def test_TW11_the_temperature_map_shows_a_narrow_track_s_own_temperature(thermal_tracks):
    """TW-11: the Heatmap's temperature map (level-0 cells of 0.3 mm here) shows the 0.1 mm track at its own
    temperature within 2 % (each cell its hottest refined cell). Before round 5 the cell's mean: 15 % cool."""
    for w, (node, shown, _) in thermal_tracks.items():
        assert shown == pytest.approx(node, rel=0.02), w


def test_TW14_an_edge_node_s_heat_goes_into_its_copper_not_the_resin_beside_it(tmp_path):
    """TW-14: a 0.4 mm track 0.01 mm off the grid at 3 A (2 layers, 0.25 mm DC cells): its edge rows' nodes lie up to
    0.06 mm outside the copper (the sub-grid gave them a node). Every copper heat sample is on the copper (Grid.hx /
    hy [A44]), and at thermal.quality fine (0.025 mm cells here) the hottest copper is the track's own temperature
    within 2 %: with the heat at the nodes, the edge nodes' share went into resin-only cells (0.3 W/mK), a hot spot
    beside the track 36 % (20 K) above it."""
    from groundtruth.analysis import thermal as T
    y = 15.01
    g = BoardGen(layers=2, size=(46.0, 30.0))
    _pads(g, (8.0, y), (38.0, y))
    g.track((8.0, y), (38.0, y), "PWR", "F.Cu", width=0.4)
    b, sc, _ = make(tmp_path, g, "edge", nets=())
    sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 3.0, "")], "")])
    sc.doc["thermal"] = {"convection": "fixed", "h_top": 10.0, "h_bottom": 10.0, "radiation": False,
                         "ambient_c": 25.0, "quality": "fine"}
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    res = simulate(b, cfg).dc_loads[0]
    heat, losses, notes = T.thermal_inputs(b, cfg, [res])
    x, yy, _, p = heat["F.Cu"]
    copper = shapely.union_all([LineString([(8.0, y), (38.0, y)]).buffer(0.2), box(7.5, y - 0.5, 8.5, y + 0.5),
                                box(37.5, y - 0.5, 38.5, y + 0.5)])
    assert shapely.intersects_xy(copper.buffer(1e-3), x, yy).all()
    assert np.any(np.abs(yy - y) > 0.15)                    # the edge rows are there, moved onto the copper
    m = T._build_model(b, cfg, heat, losses, list(notes), lambda *a: None, None)
    sol = T._coupled(m, m.p20, m.Qc)
    th, _ = T._result(b, cfg, m, sol, losses)
    col = m.mesh.locate(np.array([23.0]), np.array([y]))
    node = float(sol.T[m.th.cu_nodes(col, np.zeros(1, np.int64))][0])
    assert float(m.mesh.col_dx.min()) < 0.05
    assert th.t_max - 25.0 == pytest.approx(node - 25.0, rel=0.02)


def test_TW15_after_thermal_a_track_s_resistance_and_drop_at_its_temperature(tmp_path):
    """TW-15: the DC tables give a track's resistance and drop at 20 °C [A42]; after Thermal the result lists each
    DC path at the solved temperatures (thermal.dc_at_temperature): a 60 mm, 0.2 mm track at 2 A (fixed h, normal
    quality), its copper R, drop and loss × (1 + α(T − 20 °C)) [IEC 60028], T the mean over its heat: within 1.5 K of
    the map's mean along the track (a uniform track heats evenly along its length), 5 K below its middle (cooler
    towards the ends). The JSON report holds them (thermal.dc_at_temperature)."""
    from groundtruth.analysis.thermal import run_thermal
    from groundtruth.thermal.materials import COPPER
    y = 20.0
    g = BoardGen(layers=2, size=(76.0, 40.0))
    _pads(g, (8.0, y), (68.0, y))
    g.track((8.0, y), (68.0, y), "PWR", "F.Cu", width=0.2)
    b, sc, _ = make(tmp_path, g, "hot", nets=())
    sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 2.0, "")], "")])
    sc.doc["thermal"] = {"convection": "fixed", "h_top": 10.0, "h_bottom": 10.0, "radiation": False,
                         "ambient_c": 25.0, "quality": "normal"}
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    res = simulate(b, cfg).dc_loads[0]
    th, _ = run_thermal(b, cfg, [res])
    assert not th.error and len(th.dc_hot) == 1
    row, path = th.dc_hot[0], res.dc[0]
    gg, mm = th.layer_grids["F.Cu"], th.sheet_current["F.Cu"]
    j = int(round((y - gg.y0) / gg.d))
    along = mm[int(round((8.5 - gg.x0) / gg.d)):int(round((67.5 - gg.x0) / gg.d)) + 1, j].astype(float)
    assert float(along.max()) - 25.0 > 10.0                     # hot enough to matter
    assert row["t_mean_c"] == pytest.approx(float(along.mean()), abs=1.5)
    f = 1 + COPPER.alpha * (row["t_mean_c"] - 20.0)
    assert row["factor"] == pytest.approx(f, rel=1e-9)
    assert row["r20_mohm"] == pytest.approx(path["r_mohm"]) and row["r_mohm"] == pytest.approx(path["r_mohm"] * f)
    assert row["drop_mv"] == pytest.approx(path["drop_mv"] * f) and row["loss_w"] == pytest.approx(path["loss_w"] * f)
    assert any("DC paths at these temperatures" in n for n in th.notes)
    # and the JSON report carries them (the CLI's simulate --thermal --format json)
    import json
    from groundtruth.report import formats
    data = json.loads(formats.to_json("b", [], [], None, dc_loads=[res], thermal=th))
    (got,) = data["thermal"]["dc_at_temperature"]
    assert got["path"] == row["path"] and got["r_mohm"] == pytest.approx(row["r_mohm"])
