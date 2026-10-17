"""Rasterisation keeps the copper topology: thin webs between antipads conduct, real slots still cut."""
import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from shapely.geometry import Point, box
from shapely.ops import unary_union

from groundtruth.solver.peec import EPS0, PlaneLayer, PlanePair, PlaneStack, build_grid

PLANE = box(0, 0, 20, 10)
BBOX = (0, 0, 20, 10)


def pieces(g) -> int:
    """Connected copper pieces of a grid (nodes joined by strips)."""
    n = g.nx * g.ny
    idx = np.arange(n).reshape(g.nx, g.ny)
    rows = np.concatenate([idx[:-1, :][g.xb], idx[:, :-1][g.yb]])
    cols = np.concatenate([idx[1:, :][g.xb], idx[:, 1:][g.yb]])
    _, lab = connected_components(sp.coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n)), directed=False)
    return len(set(lab[g.node.ravel()].tolist()))


def test_antipad_row_stays_connected():
    """0.36 mm antipads every 0.5 mm along x = 10 leave 0.14 mm copper webs: point sampling on a 0.5 mm grid
    loses every node on that line and cuts the plane in two; the sub-grid keeps it in one piece."""
    holes = unary_union([Point(10, 0.5 * k).buffer(0.18, 32) for k in range(21)])
    webbed = PLANE.difference(holes)
    assert pieces(build_grid(webbed, BBOX, 0.5)) == 2
    g = build_grid(webbed, BBOX, 0.5, sub=5)
    assert pieces(g) == 1
    assert g.fill is not None and 0.2 < g.fill[20, 4] < 1.0 and g.fill[5, 5] == 1.0


def test_slot_still_cuts_with_subgrid():
    slotted = PLANE.difference(box(9.85, -1, 10.15, 11))      # 0.3 mm slot, narrower than the 0.5 mm grid
    assert pieces(build_grid(slotted, BBOX, 0.5, sub=5)) == 2


def test_capacitance_follows_copper_fill():
    """Plane-pair capacitance counts the copper actually facing in each cell, not whole cells."""
    holes = unary_union([Point(2 + 1.0 * i, 2 + 1.0 * j).buffer(0.3, 32) for i in range(16) for j in range(6)])
    perforated = PLANE.difference(holes)
    t, er = 0.2e-3, 4.5

    def capacitance(geom):
        layers = [PlaneLayer(n, z, build_grid(geom, BBOX, 0.25, sub=4)) for n, z in (("top", t), ("bot", 0.0))]
        st = PlaneStack(layers, capacitance=[PlanePair(0, 1, EPS0 * er / t, 0.0)])
        return st.cap[:, 4].sum()

    c_solid, c_holes = capacitance(PLANE), capacitance(perforated)
    expected = perforated.area / PLANE.area
    assert abs(c_holes / c_solid - expected) < 0.03


def _joined(g, a, b) -> bool:
    """Are grid cells a and b one piece (strips plus the grid's track bridges)?"""
    n = g.nx * g.ny
    idx = np.arange(n).reshape(g.nx, g.ny)
    rows = [idx[:-1, :][g.xb], idx[:, :-1][g.yb], np.array([idx[i0, j0] for i0, j0, *_ in g.bridges], int)]
    cols = [idx[1:, :][g.xb], idx[:, 1:][g.yb], np.array([idx[i1, j1] for _, _, i1, j1, *_ in g.bridges], int)]
    rows, cols = np.concatenate(rows), np.concatenate(cols)
    _, lab = connected_components(sp.coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n)), directed=False)
    return lab[idx[a]] == lab[idx[b]]


def test_a_track_through_another_nets_pour_stays_joined_on_every_grid():
    """A 0.4 mm track of net A runs through net B's pour (0.25 mm clearance), with 45° bends. On a coarse grid
    the corner cells of a diagonal step can be B's (its piece of the cell is larger): the track walk stopped there
    and the track fell apart. refboard's 78 mm +3V3 track on In3 floated free on the 0.86 and 0.61 mm
    grids and resonated at ~1.24 GHz (its worst SPI_SCK margin); joined on the 0.43 mm grid. Now its two ends are
    one piece on every grid (bridges where B owns a cell on the path), and B's pour is not cut by the track."""
    pts = [(1.0, 2.0), (6.0, 2.0), (11.0, 7.0), (19.0, 7.0), (19.0, 9.0)]
    from shapely.geometry import LineString
    pads = unary_union([Point(p).buffer(0.5) for p in (pts[0], pts[-1])])      # where the track starts and ends
    track = LineString(pts).buffer(0.2).union(pads)
    pour = PLANE.difference(LineString(pts).buffer(0.45).union(pads.buffer(0.25)))
    for d in (0.86, 0.61, 0.5, 0.43, 0.3):
        sub = max(1, min(6, int(np.ceil(d / 0.15 - 1e-9))))
        g = build_grid({"B": pour, "A": track}, BBOX, d, seeds={"A": [pts[0], pts[-1]]}, lines={"A": [(pts, 0.4)]},
                       sub=sub)
        ends = [g.nearest_node(x, y, 1.5 * d, net="A") for x, y in (pts[0], pts[-1])]
        assert None not in ends, d
        assert _joined(g, *ends), f"track cut at d = {d} mm"
        assert all(w == 0.4 and length >= d for *_, length, w in g.bridges)


def test_a_track_bridge_has_the_tracks_resistance_and_partial_inductance():
    """The lumped branch for a bridged piece of track: DC resistance l/(σ·w·t) and Ruehli's closed form for the
    partial self inductance of a rectangular bar, μ0·l/(2π)·[ln(2l/(w+t)) + 0.5 + 0.2235·(w+t)/l]."""
    import math
    from groundtruth.solver.peec import SIGMA_CU, track_impedance
    l, w, t = 1.2e-3, 0.4e-3, 35e-6
    z = track_impedance(l, w, t)
    assert abs(z(0.0).real / (l / (SIGMA_CU * w * t)) - 1) < 1e-12
    ruehli = 4e-7 * math.pi * l / (2 * math.pi) * (math.log(2 * l / (w + t)) + 0.5 + 0.2235 * (w + t) / l)
    f = 1e9
    assert abs(z(f).imag / (2 * math.pi * f) / ruehli - 1) < 0.05
    assert z(f).real > z(0.0).real                          # skin effect at 1 GHz (δ ≈ 2 µm < t)
