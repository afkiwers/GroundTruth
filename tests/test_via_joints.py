"""A via's joints to its planes (simulate.joint_points, add_pin [A89]): one pattern fixed to the grid, the same points
on every layer, so a plane pair's port inductance does not move with where the via lies in its cell. Validated on a
radial plane pair fed at its rim, a coaxial line from the via's barrel out to the rim [Ramo 1994]:

    L = mu0 * (h + delta) / (2 pi) * ln(R_rim / r_via)

with the copper's own inductance as the plane pair's (h + delta at 100 MHz [A79]). Before 2026-11-05 each layer
snapped each point of a circle centred on the hole to its own nearest node: at 0.4 mm cells the line read 0.996,
0.935 and 0.927 of the closed form at grid offsets 0, 1/4 and 1/2 cell, at 0.2 mm 0.974, 1.014 and 0.944."""
import math

import numpy as np
import pytest

from boardgen import BoardGen, radial_pair
from conftest import make
from groundtruth.analysis import simulate as sim

MU0 = 4e-7 * math.pi
SIGMA = 5.8e7
H, DRILL, RADIUS, F = 0.2, 0.3, 10.0, 1e8


def _line(tmp_path, d, offset):
    """(the port's inductance, the closed form) H of radial_pair on a grid of pitch d whose nodes are shifted by
    `offset` cells against the via in x and y."""
    from groundtruth.solver.peec import RouteExcitation
    g, (cx, cy) = radial_pair(RADIUS, H, DRILL)
    board, sc, _ = make(tmp_path, g, f"radial_{d:g}_{offset:g}", nets=())
    x0, y0 = cx - RADIUS - 1.0 - offset * d, cy - RADIUS - 1.0 - offset * d
    st = sim.BoardModel(board, sc.config).build((x0, y0, x0 + 2 * RADIUS + 2.0, y0 + 2 * RADIUS + 2.0),
                                                ["F.Cu", "B.Cu"], {"P"}, d, False)
    # the port: +1 A spread evenly over the top disc's outermost nodes, -1 A over the bottom's (the same nodes)
    inj, rim = [], []
    for k, amps in ((0, 1.0), (1, -1.0)):
        ii, jj = np.nonzero(st.grids[k].node)
        x, y = st.grids[k].xy(ii, jj)
        r = np.hypot(x - cx, y - cy)
        out = r >= r.max() - 0.5 * d
        inj += [((k, int(i), int(j)), amps / out.sum()) for i, j in zip(ii[out], jj[out])]
        rim.append(float(r[out].mean()))
    sol = RouteExcitation(st.plane, [], injections=inj).solve(F)
    delta = 1 / math.sqrt(math.pi * F * MU0 * SIGMA)
    ref = MU0 * (H * 1e-3 + delta) / (2 * math.pi) * math.log(rim[0] / (DRILL / 2))
    return sol.Z_loop.imag / (2 * math.pi * F), ref


@pytest.mark.parametrize("d, low", [(0.4, 0.90), (0.2, 0.95)])
def test_a_radial_plane_pair_is_the_coaxial_line_wherever_the_via_lies_in_its_cell(tmp_path, d, low):
    """At grid offsets 0, 1/4 and 1/2 cell the line's inductance is the same within 1 % (measured 0.928 / 0.935 /
    0.927 of the closed form at 0.4 mm cells, 0.974 / 0.980 / 0.972 at 0.2 mm; the rest is the rim's nodes) and
    within `low` to 1.03 of the closed form, converging with the pitch (0.98 at 0.1 mm). The old joints spread over
    7 % at both pitches."""
    got = []
    for offset in (0.0, 0.25, 0.5):
        L, ref = _line(tmp_path, d, offset)
        assert low * ref < L < 1.03 * ref, (d, offset, L / ref)
        got.append(L / ref)
    assert max(got) / min(got) < 1.01, (d, got)


def test_the_joint_pattern_is_fixed_to_the_grid_and_shared_by_every_layer(tmp_path):
    """joint_points: the same shape (in cells from the node nearest the hole) wherever the hole lies in its cell; on a
    four-layer stack every barrel segment joins the same (i, j) on both its layers, k segments per layer pair; on a
    layer whose copper there is only the via's pad (smaller than the ring), the joints fall back to that pad's own
    nodes."""
    g = BoardGen(layers=4, size=(20.0, 20.0), dielectrics=[0.2, 0.2, 0.2])
    for layer in ("F.Cu", "In1.Cu", "In2.Cu"):
        g.plane("P", layer)
    g.via((10.0, 10.0), "P", size=1.1, drill=1.0)     # its pad (B.Cu's only copper) inside the joints' ring
    board, sc, _ = make(tmp_path, g, "stack", nets=())
    model = sim.BoardModel(board, sc.config)
    shapes = set()
    for offset in (0.0, 0.2, 0.45):
        st = model.build((1.0 - offset * 0.2, 1.0 - offset * 0.2, 19.0, 19.0), ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                         {"P"}, 0.2, False)
        g0 = st.grids[0]
        pts = sim.joint_points(st, (10.0, 10.0), 1.0)
        ci, cj = int(round((10.0 - g0.x0) / st.d)), int(round((10.0 - g0.y0) / st.d))
        shapes.add(tuple(sorted((i - ci, j - cj) for i, j in pts)))
        assert len(pts) == sim.pin_joints(1.0, 0.2) == 10
        vias = [b for b in st.branches if b.kind == "via"]
        planes = [b for b in vias if b.b[0] <= 2]                 # F.Cu - In1 and In1 - In2: planes on both ends
        assert len(planes) == 2 * len(pts)
        assert all(b.a[1:] == b.b[1:] for b in planes)            # the same xy on both layers
        assert {b.a[1:] for b in planes} == set(pts)
        pad = [b for b in vias if b.b[0] == 3]                    # In2 - B.Cu: B.Cu has only the 1.1 mm pad's copper
        assert len(pad) == len(pts)
        assert {b.a[1:] for b in pad} == set(pts)
        assert all(st.grids[3].net_at(b.b[1:]) == "P" for b in pad)
        assert {b.b[1:] for b in pad} != set(pts)                 # fallen back onto the pad's own nodes ...
        assert all(math.dist(st.xy(b.b), (10.0, 10.0)) < 0.55 + 0.2 for b in pad)     # ... within a cell of it
    assert len(shapes) == 1
