"""The signal paths' Bode sweeps on their own (analysis/simulate.bode_paths, the Signal paths tab's "Bode",
`groundtruth bode`), with a sweep range per net (sweep_from / sweep_to / sweep_points) and the gain figures (the
gain at the sweep's low end, the −3 dB frequency: transfer.gain_figures). The user: "could we add a frequency sweep
of a sinusoidal signal to retrieve amplitude and phase plots as well as pole zero plots?", then #3: the range, a
sweep-only run, the DC gain and the −3 dB bandwidth as tiles.

Validated against closed forms: an RC low-pass 1 / (1 + s·(R_s + R1)·(C9 + C_L)) [Desoer 1969] and an op-amp
stage's closed-loop pole at GBW / gain [Franco 2015]."""
import json
import math
from dataclasses import replace

import numpy as np
import pytest

from boardgen import opamp_path_board, rc_lowpass_board
from conftest import make
from groundtruth.config.settings import NetSpec

SWEEP = dict(sweep_from=1.0, sweep_to=100e6, sweep_points=300)


def _spec(**kw) -> NetSpec:
    """The test boards' path: U1.1 (SIG_A) → … → U2.1, a 1 MHz / 10 ns signal."""
    return NetSpec("SIG_A", "U1.1", ["U2.1"], 1e6, 10e-9, **kw)


def test_an_rc_lowpass_sweeps_to_its_closed_form_without_a_field_solve(tmp_path, monkeypatch):
    """R1 1 kΩ after the 25 Ω source, C9 10 nF and the receiver's 5 pF: 0 dB at 1 Hz, −3 dB and the one pole at
    1 / (2π·1025 Ω·10.005 nF) = 15.52 kHz (1 %; the 30 mm of track adds a little), from 1 Hz to 100 MHz on 300
    points; no plane model is built (a build fails the test)."""
    from groundtruth.analysis import simulate as sim
    from groundtruth.analysis.transfer import gain_figures, pz_rows
    board, sc, _ = make(tmp_path, rc_lowpass_board(), "rc", nets=(_spec(**SWEEP),))
    monkeypatch.setattr(sim.BoardModel, "build", lambda *a, **k: pytest.fail("a plane model was built"))
    (r,) = sim.bode_paths(board, sc.config)
    assert not r.error, r.error
    resp = r.responses[0]
    assert resp.name == "SIG_A: receiver / source" and len(resp.f) == 300
    assert resp.f[0] == pytest.approx(1.0) and resp.f[-1] == pytest.approx(100e6)
    f_c = 1 / (2 * math.pi * 1025.0 * 10.005e-9)
    g = gain_figures(resp)
    assert abs(g["low_db"]) < 0.05 and g["f_3db"] == pytest.approx(f_c, rel=0.01) and g["peak_db"] is None
    assert pz_rows(resp.poles)[0]["f0"] == pytest.approx(f_c, rel=0.01)
    h = 1 / (1 + 2j * math.pi * resp.f * 1025.0 * 10.005e-9)            # the closed form, below 1 MHz
    low = resp.f < 1e6
    assert np.allclose(20 * np.log10(np.abs(resp.h[low])), 20 * np.log10(np.abs(h[low])), atol=0.1)
    assert np.allclose(np.angle(resp.h[low], deg=True), np.angle(h[low], deg=True), atol=1.0)


def test_an_opamp_stage_gives_its_gain_and_its_pole_at_gbw_over_gain(tmp_path):
    """U5 with Gain 10 and GBW 10 MHz: +20 dB from 1 Hz, −3 dB at 1 MHz (2 %), U5's output beside the path's."""
    from groundtruth.analysis.simulate import bode_paths
    from groundtruth.analysis.transfer import gain_figures
    board, sc, _ = make(tmp_path, opamp_path_board(Gain="10", GBW="10MHz"), "amp", nets=(_spec(**SWEEP),))
    (r,) = bode_paths(board, sc.config)
    names = [x.name for x in r.responses]
    assert names == ["SIG_A: receiver / source", "U5: output"]
    for resp in r.responses:
        g = gain_figures(resp)
        assert g["low_db"] == pytest.approx(20.0, abs=0.05) and g["f_3db"] == pytest.approx(1e6, rel=0.02)


def test_simulate_sweeps_the_same_circuit_over_the_nets_range(tmp_path):
    """Simulate's path responses are bode_paths' (the circuit built by the same code, swept over the net's range):
    the same curve to 1e-9."""
    from groundtruth.analysis.simulate import bode_paths, simulate
    board, sc, _ = make(tmp_path, rc_lowpass_board(), "same", nets=(_spec(sweep_from=10.0, sweep_to=10e6,
                                                                              sweep_points=120),),
                        solve_points=2, max_solves=2)
    rr = simulate(board, sc.config).routes[0]
    assert not rr.error, rr.error
    (b,) = bode_paths(board, sc.config)
    a, c = rr.responses[0], b.responses[0]
    assert len(a.f) == 120 and np.allclose(a.f, c.f) and np.allclose(a.h, c.h, rtol=1e-9, atol=0)


def test_the_default_range_is_as_before_and_one_end_set_moves_the_other():
    """Unset: a hundredth of the first harmonic (at least 1 Hz) to the band's top, 300 points; sweep_to below the
    automatic start: the start a decade below it."""
    from groundtruth.analysis.simulate import PATH_SWEEP, sweep_range
    h = np.array([1e6, 3e6, 5e6, 1e9])
    f = sweep_range(_spec(), h)
    assert len(f) == PATH_SWEEP and f[0] == pytest.approx(1e4) and f[-1] == pytest.approx(1e9)
    f = sweep_range(_spec(sweep_to=1e3), h)
    assert f[0] == pytest.approx(100.0) and f[-1] == pytest.approx(1e3)
    f = sweep_range(_spec(sweep_from=0.5, sweep_points=50), h)
    assert len(f) == 50 and f[0] == pytest.approx(0.5)


def test_gain_figures_interpolate_the_3db_point_and_see_peaking():
    """A first-order pole at 1 kHz on 30 points over 6 decades: −3 dB at 1 kHz (0.5 %, interpolated between samples
    20 % apart); a resonance with Q 3: peaking about +9.5 dB near f0; an impedance: no figures."""
    from groundtruth.analysis.transfer import Response, gain_figures
    f = np.geomspace(1.0, 1e6, 30)
    s = 2j * math.pi * f
    g = gain_figures(Response("a: x", "a", "voltage gain", f, 1 / (1 + s / (2 * math.pi * 1e3))))
    assert g["low_db"] == pytest.approx(0.0, abs=1e-3) and g["f_3db"] == pytest.approx(1e3, rel=0.005)
    w0, q = 2 * math.pi * 1e4, 3.0
    f = np.geomspace(1.0, 1e6, 600)
    s = 2j * math.pi * f
    g = gain_figures(Response("b: x", "b", "voltage gain", f, w0 ** 2 / (s ** 2 + s * w0 / q + w0 ** 2)))
    assert g["peak_db"] == pytest.approx(20 * math.log10(q / math.sqrt(1 - 1 / (4 * q * q))), abs=0.05)
    assert g["peak_f"] == pytest.approx(1e4, rel=0.05)
    assert gain_figures(Response("c: z", "c", "impedance", f, np.ones(len(f)))) is None


def test_the_sweep_settings_round_trip_and_are_checked(tmp_path):
    """sweep_from / sweep_to with units ("1Hz", "10MHz") and sweep_points written and read back; a start at or
    above the end, below 10 mHz, or 10 points: refused, the settings left as they were."""
    from groundtruth.config.settings import ConfigError, SettingsFile, net_problems
    board, sc, _ = make(tmp_path, rc_lowpass_board(), "s", nets=(_spec(sweep_from=1.0, sweep_to=10e6,
                                                                          sweep_points=400),))
    assert "sweep_from: 1Hz" in sc.path.read_text(encoding="utf-8")
    n = SettingsFile.load(sc.path).config.nets[0]
    assert (n.sweep_from, n.sweep_to, n.sweep_points) == (1.0, 10e6, 400)
    assert net_problems(_spec(sweep_from=1e3, sweep_to=1e3)) == ["sweep_to must be above sweep_from"]
    assert any("sweep_from" in m for m in net_problems(_spec(sweep_from=0.001)))
    assert any("sweep_points" in m for m in net_problems(_spec(sweep_points=10)))
    with pytest.raises(ConfigError):
        sc.set_nets([_spec(sweep_from=5.0, sweep_to=1.0)])
    assert SettingsFile.load(sc.path).config.nets[0].sweep_points == 400


def test_groundtruth_bode_prints_writes_json_and_plots(tmp_path, capsys):
    """`groundtruth bode BOARD --from 1Hz --to 10MHz --points 100 --json --plots`: the gain line, the JSON with
    every point and the gain figures, a Bode and a pole-zero PNG."""
    from groundtruth.cli import main
    _, _, path = make(tmp_path, rc_lowpass_board(), "cli", nets=(_spec(),))
    out = tmp_path / "out"
    code = main(["bode", str(path), "--from", "1Hz", "--to", "10MHz", "--points", "100", "--json", "--plots",
                 "--out-dir", str(out)])
    text = capsys.readouterr().out.replace("−", "-")
    assert code == 0 and ("gain +0.0" in text or "gain -0.0" in text) and "-3 dB at 15.5 kHz" in text
    data = json.loads((out / "groundtruth-bode.json").read_text(encoding="utf-8"))
    resp = data[0]["responses"][0]
    assert len(resp["f"]) == 100 and resp["gain"]["f_3db"] == pytest.approx(15.52e3, rel=0.02)
    assert (out / "SIG_A_bode.png").stat().st_size > 0 and (out / "SIG_A_pz.png").stat().st_size > 0


def test_an_open_lossy_track_is_its_capacitance_at_low_frequency_and_low_loss_above():
    """PathLine with the full telegrapher line (zc, gamma_l [Pozar 2012]): an open 86 mm, 0.2 mm track over a plane
    0.1 mm below at 1 Hz takes the current of its capacitance C'·l (1 %), no conductance beside the dielectric's
    G'·l (the low-loss form made it ~10 kΩ to ground); at 100 MHz γ is the low-loss α + jβ (1 %)."""
    from groundtruth.emi.path import PathLine, PathLoad, PathSource, path_currents
    from groundtruth.emi.spectrum import C0
    line = PathLine(36.0, 4.5, 0.086, width_m=0.2e-3, er=4.5, tan_d=0.02, planes_h_m=(0.1e-3,))
    f = np.array([1.0, 10.0])
    q = path_currents(f, np.ones(2), [line], PathSource(25.0), PathLoad(1e-18))
    cl = math.sqrt(4.5) / (C0 * 36.0) * 0.086                         # the line's whole capacitance
    assert np.allclose(np.abs(q.i_in), 2 * math.pi * f * cl, rtol=0.01)
    r, lp, g, cp = line.rlgc(f)
    assert np.all(q.i_in.real <= 1.01 * g * 0.086 + 1e-15)            # only the dielectric's loss is a conductance
    f = np.array([1e8])
    gam = line.gamma_l(f) / line.length_m
    assert gam.real[0] == pytest.approx(line.alpha(f)[0], rel=0.01)
    assert gam.imag[0] == pytest.approx(2 * math.pi * 1e8 * math.sqrt(4.5) / C0, rel=0.01)
    zc = line.zc(f)[0]                         # Z0·(1 + j(G'/2ωC' − R'/2ωL')) for a low-loss line
    r, lp, g, cp = line.rlgc(f)
    w = 2 * math.pi * 1e8
    assert zc.real == pytest.approx(36.0, rel=0.01)
    assert zc.imag == pytest.approx(36.0 * (g[0] / (2 * w * cp) - r[0] / (2 * w * lp)), rel=0.05)


def test_a_followers_output_impedance_is_divided_by_its_loop():
    """ComponentModel.amp_z_out_at: z_out 2.3 kΩ (open loop), GBW 1 MHz, gain 1: z_out·jf/(jf + 1 MHz) [Franco
    2015], 2.3 Ω at 1 kHz, z_out far above; without GBW or bandwidth z_out as given, without z_out 1 Ω. Into
    4.7 kΩ and 75 nF the stage's corner is then 1/(2π·4.7 kΩ·75 nF) = 451 Hz (1 %), not the 303 Hz of a flat
    2.3 kΩ (refboard's TLV6001 → R303 → C304)."""
    from groundtruth.components.models import ComponentModel
    from groundtruth.emi.path import PathAmp, PathLoad, PathPart, PathShunt, PathSource, amplified_path
    from groundtruth.analysis.transfer import Response, gain_figures
    m = ComponentModel("amplifier", {"gain": 1.0, "gbw": 1e6, "z_out": 2300.0})
    assert abs(m.amp_z_out_at(1e3)) == pytest.approx(2.3, rel=0.01)
    assert abs(m.amp_z_out_at(1e9)) == pytest.approx(2300.0, rel=0.01)
    assert m.amp_z_out_at(1e3).imag > 0                                  # inductive below the corner
    assert ComponentModel("amplifier", {"gain": 1.0, "z_out": 50.0}).amp_z_out_at(1e3) == 50.0
    assert ComponentModel("amplifier", {"gain": 1.0}).amp_z_out_at(1e3) == 1.0
    f = np.geomspace(1.0, 1e5, 400)
    amp = PathAmp("U1", m.amp_gain, m.amp_z_in, m.amp_z_out_at)
    stages = [[], [PathPart("R1", lambda x: 4700.0), PathShunt("C1", lambda x: 1 / (2j * math.pi * x * 75e-9))]]
    q = amplified_path(f, np.ones(len(f)), stages, [amp], PathSource(25.0), PathLoad(5e-12))
    g = gain_figures(Response("p: x", "p", "voltage gain", f, q[-1].v_end))
    assert g["f_3db"] == pytest.approx(1 / (2 * math.pi * 4700 * 75e-9), rel=0.01)
