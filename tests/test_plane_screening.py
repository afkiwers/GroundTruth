"""A plane between two others screens one side's field from the other (solver/peec.py, PlaneStack._faces) [A92]
[A93]: audit round 8, P8-3 and P8-4.

Each case is a plane line straight on the solver: full strips w wide on every layer, the VDD layer fed along its
whole first column and shorted to the GND layer(s) along its last, two lengths on one grid, their difference the
line's inductance per length alone (the feed and the far end cancel; a feed spread over the width has no spreading
region). Against the one-dimensional exact values of a stack of conducting slabs (the slab's diffusion across its
thickness with the fields on its faces as boundary values [Dowell 1966] [Ramo 1994], written out here):

  * GND / VDD / GND, gaps d (P8-3): the middle plane returns half its current on each side,
        L' = (mu0 d + Li(t) + Li(t/2)) / 2w
    Li(t) = Im((1+j)/(sigma delta) coth((1+j) t/delta)) / omega the one-face slab's internal inductance: mu0 (d/2 +
    t/4)/w at low frequency, mu0 (d + delta)/2w at high. With one sheet per layer the middle plane had one current
    for both faces: mu0 (d - t/2 + 1.5 delta)/2w at high frequency (-26 % for 0.1 mm gaps and 70 um copper).
  * VDD / floating plane / GND, gaps d (P8-4): the floating plane carries eddy currents on its faces,
        L' = (mu0 2d + 2 Li(t) + Im(2 (k/sigma) tanh(k t/2)) / omega) / w,   k = (1+j)/delta
    mu0 (2d + t + 2t/3)/w at low frequency (the field passes through it), mu0 (2d + 2 delta)/w at high (it screens);
    one sheet per layer left it transparent at every frequency (+5.5 % at 30 MHz).

The ratios L'(f) / L'(1 MHz) against the exact ones cancel the field's fringing past the strips' edges (a few %, the
same at every frequency); the absolute values are checked to the fringing's few %. Pitch 0.4 mm; at 0.2 and 0.1 mm
the same within 0.5 % (VALIDATION_AUDIT.md round 8)."""
import math

import numpy as np
import pytest

MU0 = 4e-7 * math.pi
SIGMA = 5.8e7
W = 10.0                    # mm, the strips' width
PITCH = 0.4                 # mm
FREQS = (1e4, 1e6, 3e7, 1e8)


def _line(stack, gaps, length, screen=True):
    """Port impedances {f: Z} of a line `length` mm long: `stack` [(net, copper mm)] top down, `gaps` mm between;
    and its PlaneStack."""
    st, exc = _model(stack, gaps, length, screen)
    return {f: exc.solve(f).Z_loop for f in FREQS}, st


def _model(stack, gaps, length, screen=True):
    """The line's PlaneStack and its excitation (_line)."""
    from shapely.geometry import box
    from groundtruth.solver import peec
    d = PITCH
    bbox = (0.0, 0.0, length, W)
    layers, z = [], 0.0
    for k, (net, t) in enumerate(stack):
        if k:
            z -= (0.5 * stack[k - 1][1] + gaps[k - 1] + 0.5 * t) * 1e-3
        g = peec.build_grid({net: box(0.0, 0.0, length, W)}, bbox, d, sub=4)
        layers.append(peec.PlaneLayer(net, z, g, t * 1e-3))
    nx, ny = layers[0].grid.nx, layers[0].grid.ny
    vdd = [k for k, (n, _) in enumerate(stack) if n == "VDD"][0]
    gnd = [k for k, (n, _) in enumerate(stack) if n == "GND"]
    short = lambda f: 1e-9 + 0j  # noqa: E731
    br = [peec.Branch((vdd, nx - 1, j), (k, nx - 1, j), short) for j in range(ny) for k in gnd]
    br += [peec.Branch((gnd[0], 0, j), (k, 0, j), short) for j in range(ny) for k in gnd[1:]]
    st = peec.PlaneStack(layers, br, **({} if screen else {"screen": False}))
    inj = [((vdd, 0, j), 1.0 / ny) for j in range(ny)] + [((gnd[0], 0, j), -1.0 / ny) for j in range(ny)]
    return st, peec.RouteExcitation(st, [], injections=inj)


def _per_length(stack, gaps, screen=True):
    """{f: L' (H/m)} from the lines 20 and 40 mm long, and the 40 mm line's stack."""
    a, _ = _line(stack, gaps, 20.0, screen)
    b, st = _line(stack, gaps, 40.0, screen)
    return {f: ((b[f] - a[f]).imag / (2 * math.pi * f)) / 20e-3 for f in FREQS}, st


def _li(t, f):
    """The one-face slab's internal inductance per square (H): Im((1+j)/(sigma delta) coth((1+j) t/delta)) / omega,
    coth u = (1 + e^-2u)/(1 - e^-2u) [Ramo 1994]."""
    delta = 1 / math.sqrt(math.pi * f * MU0 * SIGMA)
    u = (1 + 1j) * t / delta
    z = u * (1 + np.exp(-2 * u)) / (1 - np.exp(-2 * u)) / (SIGMA * t)
    return z.imag / (2 * math.pi * f)


def _eddy(t, f):
    """The floating slab's mode, Im(2 (k/sigma) tanh(k t/2)) / omega per square (H)."""
    k = (1 + 1j) * math.sqrt(math.pi * f * MU0 * SIGMA)
    return (2 * k / SIGMA * np.tanh(k * t / 2)).imag / (2 * math.pi * f)


@pytest.fixture(scope="module")
def middle():
    """GND / VDD / GND, 0.1 mm gaps, 70 um copper (P8-3's worst case)."""
    return _per_length([("GND", 0.07), ("VDD", 0.07), ("GND", 0.07)], [0.1, 0.1])


@pytest.fixture(scope="module")
def floating():
    """VDD / AUX (floating) / GND, 0.2 mm gaps, 35 um copper (P8-4), and the same stack without the AUX plane."""
    return (_per_length([("VDD", 0.035), ("AUX", 0.035), ("GND", 0.035)], [0.2, 0.2]),
            _per_length([("VDD", 0.035), ("GND", 0.035)], [0.435]))


def _middle_exact(f, d=0.1e-3, t=70e-6):
    """GND / VDD / GND, per metre."""
    return (MU0 * d + _li(t, f) + _li(t / 2, f)) / (2 * W * 1e-3)


def _floating_exact(f, d=0.2e-3, t=35e-6):
    """VDD / floating / GND, per metre."""
    return (MU0 * 2 * d + 2 * _li(t, f) + _eddy(t, f)) / (W * 1e-3)


def test_the_slab_two_port_is_the_coth_csch_form_and_the_face_sheets_reproduce_it():
    """kernel.slab_two_port against (k/sigma) coth(kt), (k/sigma)/sinh(kt) written out, 1 Hz to 30 GHz, 18-105 um
    (relative to z11, within 1e-9); z11 is the one-face slab (slab_impedance). kernel.face_sheet_impedance: its sum mode
    is the slab's (z_self + z_mutual = z11 + z12), and its difference mode 2 (z_self - z_mutual) in parallel with the
    free space between the two sheets, j omega mu0 t, is the slab's difference mode 2 (z11 - z12) [A92]. At DC the two
    face sheets are 4 and -2 squares' resistance (the slab's 1 for both together, its eddy loop 12)."""
    from groundtruth.solver.kernel import face_sheet_impedance, slab_impedance, slab_two_port
    for t in (18e-6, 35e-6, 70e-6, 105e-6):
        for f in np.logspace(0, math.log10(3e10), 80):
            k = (1 + 1j) * math.sqrt(math.pi * f * MU0 * SIGMA)
            u = k * t
            e = np.exp(-2 * u)
            if abs(u) < 1e-3:   # series: u coth u = 1 + u^2/3 - u^4/45, u / sinh u = 1 - u^2/6 + 7u^4/360
                r11, r12 = (1 + u ** 2 / 3 - u ** 4 / 45) / (SIGMA * t), (1 - u ** 2 / 6 + 7 * u ** 4 / 360) / (SIGMA * t)
            else:
                r11 = k / SIGMA * (1 + e) / (1 - e)
                r12 = k / SIGMA * 2 * np.exp(-u) / (1 - e)
            z11, z12 = slab_two_port(t, f, SIGMA)
            assert abs(z11 - r11) < 1e-9 * abs(r11) and abs(z12 - r12) < 1e-9 * abs(r11), (t, f, z11, z12, r11, r12)
            assert abs(z11 - slab_impedance(t, f, SIGMA)) < 2e-6 * abs(z11)
            zs, zm = face_sheet_impedance(t, f, SIGMA)
            assert abs((zs + zm) - (r11 + r12)) < 1e-9 * abs(r11)
            zv = 1j * 2 * math.pi * f * MU0 * t
            dm = 2 * (zs - zm)
            assert abs(zv * dm / (zv + dm) - 2 * (r11 - r12)) < 1e-8 * abs(r11), (t, f)
    zs, zm = face_sheet_impedance(35e-6, 0.0, SIGMA)
    assert zs == pytest.approx(4 / (SIGMA * 35e-6)) and zm == pytest.approx(-2 / (SIGMA * 35e-6))


def test_only_a_layer_with_copper_on_both_sides_is_split():
    """Two layers: no face sheets (an ordinary plane pair costs nothing more); three: the middle one only, at its
    faces; screen=False (a DC model) none."""
    st2, _ = _model([("VDD", 0.035), ("GND", 0.035)], [0.2], 4.0)
    assert st2.n_sheets == 2 and len(st2.tw_b) == 0
    st3, _ = _model([("GND", 0.035), ("VDD", 0.035), ("GND", 0.035)], [0.2, 0.2], 4.0)
    assert st3.n_sheets == 4 and list(st3.twin_parent) == [1]
    assert st3.sheet_z[1] - st3.sheet_z[3] == pytest.approx(35e-6)          # the two faces, t apart
    assert st3.face_gap(0, 1) == pytest.approx(0.2e-3) and st3.face_gap(3, 2) == pytest.approx(0.2e-3)
    st0, _ = _model([("GND", 0.035), ("VDD", 0.035), ("GND", 0.035)], [0.2, 0.2], 4.0, screen=False)
    assert st0.n_sheets == 3


def test_a_plane_between_two_others_returns_on_both_faces(middle):
    """GND / VDD / GND (P8-3): L'(f) / L'(1 MHz) within 1 % of the exact ratio from 10 kHz to 100 MHz (fringing
    cancels; measured 0.2-0.6 %), and L' within 5 % of the exact value (the 1D one, fringing left out: +3 %). With one
    sheet per layer the ratio fell to 0.83 at 30 MHz (P8-3: the middle plane at -26 %)."""
    per, st = middle
    for f in FREQS:
        trend = (per[f] / per[1e6]) / (_middle_exact(f) / _middle_exact(1e6))
        assert abs(trend - 1) < 0.01, (f, trend)
        assert 0.97 < per[f] / _middle_exact(f) < 1.05, (f, per[f] / _middle_exact(f))


def test_a_floating_plane_screens_at_high_frequency_and_lets_the_field_through_at_low(floating):
    """VDD / floating / GND (P8-4): against the same stack without the floating plane (the fringing cancels) the
    exact ratio within 1 % at every frequency: 1 at 10 kHz (transparent), 0.95 at 30 MHz, 0.94 at 100 MHz (its faces'
    eddy currents keep the field out of its copper). With one sheet per layer the ratio stayed ~1 (+5.5 %)."""
    (per, st), (none, _) = floating
    for f in FREQS:
        exact = _floating_exact(f) / ((MU0 * 0.435e-3 + 2 * _li(35e-6, f)) / (W * 1e-3))
        assert abs((per[f] / none[f]) / exact - 1) < 0.01, (f, per[f] / none[f], exact)
    assert abs(per[1e4] / none[1e4] - 1) < 1e-3           # low frequency: as if it were not there
    assert per[3e7] / none[3e7] < 0.965



def _thick_pair_lp(w, h, t):
    """L' (H/m) of two plates w wide and t thick, h apart, each carrying a uniform current (+I and -I): the DC
    distribution of a long pair, from the exact energy of uniform rectangles, (mu0 / 2 pi) (2 ln g12 - ln g11 -
    ln g22) with ln g the mean of ln r over two rectangles [Grover 1946] (Gauss-Legendre in y, closed form across x)."""
    xg, wg = np.polynomial.legendre.leggauss(24)

    def mean_log(y0a, y1a, y0b, y1b):
        """Mean ln r between [0, w] x [y0a, y1a] and [0, w] x [y0b, y1b] (r = 0 on the diagonal: integrable)."""
        def across(dy):
            """2 * int_0^w (w - u) * 0.5 ln(u^2 + dy^2) du, in closed form (u ln u - u at dy = 0)."""
            a = abs(dy)

            def anti(u):
                """An antiderivative of 0.5 (w - u) ln(u^2 + a^2)."""
                if a == 0.0:
                    lu = math.log(u) if u > 0 else 0.0
                    return w * (u * lu - u) - 0.5 * (u * u * lu - u * u / 2)
                big = math.log(u * u + a * a)
                return w * (0.5 * u * big - u + a * math.atan(u / a)) - 0.25 * ((u * u + a * a) * big - u * u)
            return 2 * (anti(w) - anti(0.0))
        ya = 0.5 * (y1a - y0a) * (xg + 1) + y0a
        yb = 0.5 * (y1b - y0b) * (xg + 1) + y0b
        tot = sum(wi * wj * across(a - b) for a, wi in zip(ya, 0.5 * (y1a - y0a) * wg)
                  for b, wj in zip(yb, 0.5 * (y1b - y0b) * wg))
        return tot / (w * w * (y1a - y0a) * (y1b - y0b))
    l11 = mean_log(0, t, 0, t)
    l12 = mean_log(0, t, t + h, 2 * t + h)
    return MU0 / (2 * math.pi) * (2 * l12 - 2 * l11)


def test_a_thin_dielectric_pair_converges_to_the_dc_distribution_with_the_cell_size():
    """P8-5: 25 um of dielectric between 35 um copper, 10 mm wide, at 10 kHz (the DC current distribution): the line's
    inductance per length against the two uniform plates' exact value converges with the cell size, +9 % at 0.4 mm
    cells, +3 % at 0.2 (first order: the cells' edge strips). The audit's 'unexplained +7 % that does not shrink' came
    from its 20 / 40 mm point-fed lines, whose ends are not far enough from each other for a 10 mm width (the same
    model on 40 / 80 mm lines reads +1 %), and from a reference with the plates as zero-thickness strips; not a model
    defect (it does not need the face sheets: a two-layer stack has none)."""
    exact = _thick_pair_lp(W * 1e-3, 25e-6, 35e-6)
    excess = []
    for pitch in (0.4, 0.2):
        global PITCH
        old, PITCH = PITCH, pitch
        try:
            per, _ = _per_length([("VDD", 0.035), ("GND", 0.035)], [0.025])
        finally:
            PITCH = old
        excess.append(per[1e4] / exact - 1)
    assert 0.0 < excess[0] < 0.11 and 0.0 < excess[1] < 0.045, excess
    assert excess[1] < 0.5 * excess[0] + 0.01, excess


def _six_planes(screen, d=2.0):
    """A six-plane stack (GND / VDD alternating, 35 um copper, 0.1 / 0.535 / 0.1 / 0.535 / 0.1 mm of dielectric: the
    usual six-layer build-up) 24 x 16 mm at `d` mm cells, its plane pairs' capacitance, VDD shorted to GND at one end
    and fed at the other: (PlaneStack, RouteExcitation). The cells' edge strips are narrower than the pitch (the grid
    is cut at the board's edge), the case that makes the face pairs' corrections matter (_twin_corrections)."""
    from shapely.geometry import box
    from groundtruth.solver import peec
    names = ["GND", "VDD", "GND", "VDD", "GND", "VDD"]
    gaps = [0.1, 0.535, 0.1, 0.535, 0.1]
    t, length, width = 0.035, 24.0, 16.0
    bbox = (0.0, 0.0, length, width)
    layers, z = [], 0.0
    for k, net in enumerate(names):
        if k:
            z -= (t + gaps[k - 1]) * 1e-3
        layers.append(peec.PlaneLayer(net, z, peec.build_grid({net: box(0.0, 0.0, length, width)}, bbox, d, sub=4), t * 1e-3))
    nx, ny = layers[0].grid.nx, layers[0].grid.ny
    vdd = [k for k, n in enumerate(names) if n == "VDD"]
    gnd = [k for k, n in enumerate(names) if n == "GND"]
    short = lambda f: 1e-9 + 0j  # noqa: E731
    br = [peec.Branch((vdd[0], nx - 1, j), (k, nx - 1, j), short) for j in range(ny) for k in gnd]
    br += [peec.Branch((vdd[0], 0, j), (k, 0, j), short) for j in range(ny) for k in vdd[1:]]
    br += [peec.Branch((gnd[0], 0, j), (k, 0, j), short) for j in range(ny) for k in gnd[1:]]
    pairs = [peec.PlanePair(k, k + 1, 8.854e-12 * 4.3 / (gaps[k] * 1e-3), 0.02) for k in range(5) if names[k] != names[k + 1]]
    st = peec.PlaneStack(layers, br, capacitance=pairs, precond="sheet", **({} if screen else {"screen": False}))
    inj = [((vdd[0], 0, j), 1.0 / ny) for j in range(ny)] + [((gnd[0], 0, j), -1.0 / ny) for j in range(ny)]
    return st, peec.RouteExcitation(st, [], injections=inj)


def test_the_face_pairs_difference_inductance_stays_positive_beside_narrow_strips():
    """A split layer's two faces share the copper, so the narrow-strip corrections (a strip's own dL, its neighbours'
    M_near [A4]) apply between the faces as within them [A94]. Left on each face alone, the difference mode
    L_aa - L_ab (the free space between the faces, ~ mu0 t = 44 pH per square) took the corrections whole at every
    edge strip: it went negative (-26 pH of 21 pH on this stack), and the partial inductance matrix with it
    (not positive definite; GMRES's iterations x 2-4 with the 'sheet' preconditioner). Now the difference block is
    positive definite at its free-space value (19-22 pH), the whole matrix positive definite."""
    st, _ = _six_planes(True)
    assert len(st.tw_b) and st.dL is not None and np.any(st.dL != 0)
    ns = st.ns
    L = np.zeros((ns, ns))
    for i in range(ns):
        e = np.zeros(ns, complex)
        e[i] = 1.0
        L[:, i] = st.Lp_matvec(e).real
    a, b = st.tw_a, st.tw_b
    assert abs(L - L.T).max() < 1e-12 * abs(L).max()
    diff = 0.5 * (L[np.ix_(a, a)] + L[np.ix_(b, b)] - L[np.ix_(a, b)] - L[np.ix_(b, a)])
    w = np.linalg.eigvalsh(diff)
    assert 15e-12 < w.min() and w.max() < 25e-12, (w.min(), w.max())      # mu0 t / 2 per face: 22 pH
    assert np.linalg.eigvalsh(L).min() > 0.0


def test_a_split_stack_needs_about_the_iterations_of_the_unsplit_one():
    """The 'sheet' preconditioner on the six-plane stack with and without face sheets: 15 / 19 / 23 iterations
    at 20 / 100 / 400 MHz unsplit, 16 / 21 / 25 split (20 / 44 / 87 before the faces shared the narrow-strip
    corrections, 1.3 / 2.3 / 3.8 x): within 1.3 x, as the target of the preconditioner work was."""
    split, exc_s = _six_planes(True)
    plain, exc_p = _six_planes(False)
    assert split.n_sheets == 10 and plain.n_sheets == 6
    for f in (2e7, 1e8, 4e8):
        s, p = exc_s.solve(f), exc_p.solve(f)
        assert s.iterations <= 1.3 * p.iterations + 1, (f, s.iterations, p.iterations)
