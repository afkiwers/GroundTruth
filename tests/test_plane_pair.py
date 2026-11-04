"""A plane pair's inductance per square against the parallel-plate line (analysis/pdn.py on solver/peec.py) [A79]:
a strip pair w wide and h apart, shorted at its far end, two lengths on one grid; their difference is the line's
inductance per length alone (the feed and the far end cancel):

    L' = mu0 * (h + 2t/3) / w     at low frequency (the current fills both foils' thickness t)
    L' = mu0 * (h + delta) / w    once the skin depth delta << t (the current on the faces turned to each other)

less a few % for the field fringing past the strips' edges (w/h = 50). Before 2026-11-02 the strips stood at the
foils' centres without their internal inductance: mu0 * (h + t) / w at every frequency, 1.13-1.20 x mu0*h/w here
(the cross-check with KiPI, a 2.5D FEM solver, found it)."""
import math

import pytest

from boardgen import plane_pair_line
from conftest import make

MU0 = 4e-7 * math.pi
SIGMA = 5.8e7
H, W, T = 0.2e-3, 10e-3, 35e-6
PITCH = 0.4                 # mm, both lengths on one grid (fixed: the node budget would give each its own)


def _line_l(tmp_path, length, monkeypatch):
    """The port inductance Im(Z)/w at U1 (complex Z from |Z| and phase) per frequency, for a line `length` mm long."""
    import numpy as np
    from groundtruth.analysis import pdn
    from groundtruth.analysis.simulate import simulate
    from groundtruth.components.models import ComponentModel
    from groundtruth.config.settings import PdnSpec, SettingsFile
    orig = pdn._choose_pitch
    monkeypatch.setattr(pdn, "_choose_pitch", lambda h, bbox, n, budget, *a: PITCH if budget == 999_999
                        else orig(h, bbox, n, budget, *a))
    g, refs = plane_pair_line(length)
    b, sc, _ = make(tmp_path, g, f"line{length:g}", nets=(), cavity_max_nodes=999_999)
    for r in refs:
        sc.set_component(r, ComponentModel("cap", {"C": 1.0, "ESR": 1e-6, "ESL": 1e-15}))
    sc.set_pdn([PdnSpec("VDD", source="none", voltage=3.3, parts=["U1"])])
    sc.save()
    r = simulate(b, SettingsFile.load(sc.path).config)
    p = r.pdn[0]
    assert not p.error, p.error
    port = p.ports[0]
    fs = np.asarray(port["f"])
    z = np.asarray(port["z_ohm"]) * np.exp(1j * np.radians(port["phase_deg"]))
    return fs, z.imag / (2 * np.pi * fs)


@pytest.fixture(scope="module")
def lines(tmp_path_factory):
    """The two lines' L(f)."""
    mp = pytest.MonkeyPatch()
    try:
        t = tmp_path_factory.mktemp("planepair")
        return {ln: _line_l(t, ln, mp) for ln in (20.0, 40.0)}
    finally:
        mp.undo()


def _per_length(lines, f):
    """The line's inductance per metre at the solves nearest f (their difference over the 20 mm between them)."""
    import numpy as np
    out = []
    for ln in (20.0, 40.0):
        fs, ls = lines[ln]
        out.append(ls[int(np.argmin(np.abs(np.log(fs / f))))])
    return (out[1] - out[0]) / 20e-3


def test_a_plane_pair_is_mu0_times_its_gap_plus_the_copper_s_own_inductance(lines):
    """At 1 MHz (delta = 66 um > t) mu0 * (h + 2t/3) / w, at 100 MHz (delta = 6.6 um) mu0 * (h + delta) / w: within
    -6 % (fringing lowers it; measured -2.5 %) / +3 %; the old centre-to-centre model read mu0 * (h + t) / w, 5 % and
    16 % above these."""
    low = MU0 * (H + 2 * T / 3) / W
    delta = 1 / math.sqrt(math.pi * 1e8 * MU0 * SIGMA)
    high = MU0 * (H + delta) / W
    lo, hi = _per_length(lines, 1e6), _per_length(lines, 1e8)
    assert 0.94 * low < lo < 1.03 * low, (lo, low)
    assert 0.94 * high < hi < 1.03 * high, (hi, high)
    assert hi < lo                                   # the copper's own inductance falls with the skin depth


@pytest.mark.parametrize("t_um", [18.0, 35.0, 70.0, 105.0, 210.0])
def test_the_slab_impedance_is_the_coth_form_at_every_frequency(t_um):
    """kernel.slab_impedance against the one-face slab written out independently, (1 + j)/(sigma delta) coth((1 + j)
    t/delta) with coth u = (1 + e^-2u)/(1 - e^-2u) [Ramo 1994], from 1 kHz to 20 GHz: within 1e-5 in both parts (its
    thin-foil series stops at x^2 in the reactance: 8x^4/315 = 2.5e-6 at t/delta = 0.1), and its high-frequency limit
    (1 + j)/(sigma delta) (the surface impedance of a half space) at t/delta > 30 within 1e-9. Its
    argument is capped where coth is 1; before round 8 the factor (1 + j) t/delta in front was capped too, which froze
    the impedance at its t/delta = 30 value: 70 um copper read 10 % low at 1 GHz, 35 um 43 % low at 10 GHz."""
    import numpy as np
    from groundtruth.solver.kernel import slab_impedance
    t = t_um * 1e-6
    for f in np.logspace(3, np.log10(2e10), 60):
        delta = 1 / math.sqrt(math.pi * f * MU0 * SIGMA)
        u = (1 + 1j) * t / delta
        if abs(u) < 1e-3:       # the exact form cancels there: its series u coth u = 1 + u^2/3 - u^4/45 + 2u^6/945
            ref = (1 + u ** 2 / 3 - u ** 4 / 45 + 2 * u ** 6 / 945) / (SIGMA * t)
        else:
            ref = u * (1 + np.exp(-2 * u)) / (1 - np.exp(-2 * u)) / (SIGMA * t)
        z = slab_impedance(t, f, SIGMA)
        assert abs(z.real / ref.real - 1) < 1e-5, (t_um, f, z, ref)
        assert abs(z.imag / ref.imag - 1) < 1e-5, (t_um, f, z, ref)
        if t / delta > 30:
            surface = (1 + 1j) / (SIGMA * delta)
            assert abs(z / surface - 1) < 1e-9, (t_um, f, z, surface)
