"""A power stage's load step (analysis/load_step.py) against independent results: the converter's closed-loop
admittance against the loop model's Z_ol/(1 + T) (peak current and voltage mode, buck and boost), the step of a
voltage-mode buck against a time integration of its averaged circuit (inductor, capacitor, gm amplifier with its
R-C network), a coil switched on against the same circuit with the coil in it, the ramp's limits, the approximate
loop's crossover and phase margin, ΔV ≈ ΔI/(2π·f_c·C), and the settings' load_step round trip."""
import math

import numpy as np
import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.analysis.load_step import (RECOVER_BAND, analyse_load_step, approximate_loop, converter_model,
                                            resolved, step_line)
from groundtruth.analysis.loop import analyse_loop
from groundtruth.config.settings import ConverterSpec, LoadStep, _load_step, load_step_data

F_SW, V_IN, V_OUT, I_OUT = 500e3, 12.0, 3.3, 2.0
L, C, K, GM = 4.7e-6, 22e-6, 0.05, 1.8e-3
RAMP = 20.0                                                 # V: a voltage-mode loop with 117° (1 V: under 1°)
RC, CC, CHF, H = 2.2e3, 22e-9, 22e-12, 25e3 / 125e3          # the loop board's ITH network and divider


def _buck(tmp_path, name, **fields):
    """The loop board's buck (12 V to 3.3 V, 2 A, 500 kHz) with the controller's fields; C2 22 µF and L1 4.7 µH
    ideal (no ESR, no DCR)."""
    g = discrete_buck_board(loop=True)
    g.fields("U8", **{"Control Mode": "peak current", "EA gm": "1.8mmho", "Current Sense Gain": str(K), **fields})
    b, sc, _ = make(tmp_path, g, name, nets=())
    cfg = sc.config
    cfg.components["C2"].model.params.update(C=C, ESR=0.0)
    cfg.components["L1"].model.params.update(L=L, R=0.0)
    spec = ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"], f_sw=F_SW,
                         i_out=I_OUT, v_in=V_IN, v_out=V_OUT)
    return b, cfg, spec


@pytest.mark.parametrize("fields", [{}, {"Control Mode": "voltage", "Ramp": "1V"}])
def test_the_converters_admittance_is_the_closed_loop(tmp_path, fields):
    """Y_conv + Y_o = (1 + T)/Z_ol at every frequency (10⁻⁹), peak current and voltage mode: the Norton form the
    load step and the PDN build on is the loop model's closed loop."""
    b, cfg, spec = _buck(tmp_path, "adm" + str(len(fields)), **fields)
    m = analyse_loop(b, cfg, spec).model
    s = 2j * np.pi * np.geomspace(10, 250e3, 40)
    assert np.allclose(m.y_conv(s) + m.y_o(s), (1 + m.t(s)) / m.z_ol(s), rtol=1e-9)
    assert np.allclose(m.y_caps(s), s * C, rtol=1e-12)


@pytest.mark.parametrize("fields", [{}, {"Control Mode": "voltage", "Ramp": "1V"}])
def test_a_boosts_admittance_is_the_closed_loop(tmp_path, fields):
    """The same for a boost (its a·D' and right-half-plane terms in both modes)."""
    from test_loop import _boost
    b, cfg, spec = _boost(tmp_path, "badm" + str(len(fields)), **fields)
    m = analyse_loop(b, cfg, spec).model
    s = 2j * np.pi * np.geomspace(10, 250e3, 40)
    assert np.allclose(m.y_conv(s) + m.y_o(s), (1 + m.t(s)) / m.z_ol(s), rtol=1e-8)


def _vm_buck_ode(i_from, coil=None, i_step=0.0):
    """The voltage-mode buck's averaged circuit integrated in time (small signal about the operating point): states
    i_L, v (the output's deviation), v_c (ITH), v_cc (across Cc), and a switched coil's current; L·di_L/dt =
    V_in·v_c/V_ramp − v, C·dv/dt = i_L − v/R − i_o − i_b, Chf·dv_c/dt = −gm·H·v − (v_c − v_cc)/Rc,
    Cc·dv_cc/dt = (v_c − v_cc)/Rc, L_b·di_b/dt = (V_OUT + v) − R_b·i_b. Radau, tight tolerances."""
    from scipy.integrate import solve_ivp
    r = V_OUT / i_from if i_from else math.inf

    def rhs(_t, x):
        """The averaged circuit's derivatives."""
        il, v, vc, vcc, ib = x
        load = i_step + (ib if coil else 0.0)
        out = [(V_IN * vc / RAMP - v) / L, (il - v / r - load) / C, (-GM * H * v - (vc - vcc) / RC) / CHF,
               (vc - vcc) / RC / CC, ((V_OUT + v - coil[0] * ib) / coil[1]) if coil else 0.0]
        return out

    def run(t):
        """v(t) and the coil's current at the times t."""
        sol = solve_ivp(rhs, (0, t[-1]), [0, 0, 0, 0, 0], method="Radau", t_eval=t, rtol=1e-10, atol=1e-13,
                        max_step=t[-1] / 2000)
        return sol.y[1], sol.y[4]
    return run


def test_a_current_step_is_the_averaged_circuits(tmp_path):
    """Voltage mode, 1 → 2 A: the output's deviation equals the averaged circuit integrated in time within 0.1 % of
    the dip at every sample; the figures say where the dip is and when it recovers."""
    b, cfg, spec = _buck(tmp_path, "ode", **{"Control Mode": "voltage", "Ramp": f"{RAMP:g}V"})
    r = analyse_load_step(b, cfg, spec, LoadStep(i_from=1.0, i_to=2.0))
    assert not r.error, r.error
    (node,) = r.nodes
    t = node.t[node.t <= node.t[-1] / 4][::7]
    dv = np.interp(t, node.t, node.v) - V_OUT
    want, _ = _vm_buck_ode(1.0, i_step=1.0)(t)
    dip = abs(node.metrics["dip"])
    assert np.max(np.abs(dv - want)) < 1e-3 * dip
    assert node.metrics["dip"] < 0 and node.metrics["t_dip"] > 0
    assert node.metrics["recovery"] is not None and node.metrics["recovery"] > node.metrics["t_dip"]
    assert "load step 1 → 2 A" in step_line(r)


def test_a_coil_switched_on_is_the_circuit_with_the_coil(tmp_path):
    """A 2 Ω + 100 µH coil switched on at the output from no load: by the compensation theorem; the same as the
    averaged circuit with the coil in it (0.1 % of the dip), its current settling at V/(R + Z_out(0))."""
    b, cfg, spec = _buck(tmp_path, "coil", **{"Control Mode": "voltage", "Ramp": f"{RAMP:g}V"})
    r = analyse_load_step(b, cfg, spec, LoadStep(r_load=2.0, l_load=100e-6))
    assert not r.error, r.error
    (node,) = r.nodes
    t = node.t[node.t <= node.t[-1] / 4][::7]
    dv = np.interp(t, node.t, node.v) - V_OUT
    want, ib = _vm_buck_ode(0.0, coil=(2.0, 100e-6))(t)
    assert np.max(np.abs(dv - want)) < 1e-3 * abs(node.metrics["dip"])
    assert np.max(np.abs(np.interp(t, r.current["t"], r.current["i_load"]) - ib)) < 1e-3 * V_OUT / 2.0
    assert r.figures["i_final"] == pytest.approx(V_OUT / 2.0, rel=1e-3)


def test_a_harness_and_load_capacitance_add_a_load_node(tmp_path):
    """Behind 20 mΩ + 500 nH to 100 µF (10 mΩ ESR): two nodes; the load's final deviation is the harness's IR drop
    for the step, the output's none (the loop's integrator); the closed-loop impedance at the load among the
    curves."""
    b, cfg, spec = _buck(tmp_path, "cable")
    ls = LoadStep(i_from=1.0, i_to=2.0, cable_r=0.02, cable_l=500e-9, c_load=100e-6, esr_load=0.01)
    r = analyse_load_step(b, cfg, spec, ls)
    assert not r.error, r.error
    out, load = r.nodes
    assert (out.node, load.node) == ("output", "load")
    assert load.metrics["final"] == pytest.approx(-1.0 * 0.02, rel=0.02)
    assert abs(out.metrics["final"]) < 0.05 * abs(load.metrics["final"])
    assert [x.name.split(": ", 1)[1] for x in r.responses] == ["Z_out open loop", "Z_out closed loop", "Z at the load"]


def test_the_ramp_tends_to_the_step(tmp_path):
    """A 1 ns ramp gives the ideal step's curve (10⁻³ of the dip); a slow ramp a smaller dip, the same final value."""
    b, cfg, spec = _buck(tmp_path, "ramp")
    step = analyse_load_step(b, cfg, spec, LoadStep(i_from=1.0, i_to=2.0)).nodes[0]
    fast = analyse_load_step(b, cfg, spec, LoadStep(i_from=1.0, i_to=2.0, rise=1e-9)).nodes[0]
    slow = analyse_load_step(b, cfg, spec, LoadStep(i_from=1.0, i_to=2.0, rise=50e-6)).nodes[0]
    assert fast.metrics["dip"] == pytest.approx(step.metrics["dip"], rel=1e-3)
    assert abs(slow.metrics["dip"]) < 0.8 * abs(step.metrics["dip"])
    assert slow.metrics["final"] == pytest.approx(step.metrics["final"], abs=1e-6)


def test_the_approximate_loop_has_its_crossover_and_margin():
    """T from f_c and PM [A115]: |T(j·2π·f_c)| = 1 and 180° + ∠T = PM, for 30°, 60° and 80°."""
    for pm in (30.0, 60.0, 80.0):
        t = approximate_loop(20e3, pm)
        h = t(2j * math.pi * 20e3)
        assert abs(h) == pytest.approx(1.0, rel=1e-9)
        assert 180 + math.degrees(np.angle(h)) == pytest.approx(pm, abs=1e-6)


def test_a_cot_stage_steps_on_its_loop_bandwidth(tmp_path):
    """A constant-on-time controller with Loop Bandwidth 30 kHz and Loop Phase Margin 60°: the approximate loop; the
    dip of a 1 A step at full load near ΔI/(2π·f_c·C) (the closed loop's 1/|1 + T| at f_c), a note says it is
    approximate; without the fields the error names them."""
    b, cfg, spec = _buck(tmp_path, "cot", **{"Control Mode": "COT"})
    r = analyse_load_step(b, cfg, spec)
    assert r.error and "Loop Bandwidth" in r.error
    b, cfg, spec = _buck(tmp_path, "cot2", **{"Control Mode": "COT", "Loop Bandwidth": "30kHz",
                                              "Loop Phase Margin": "60"})
    r = analyse_load_step(b, cfg, spec, LoadStep(i_from=1.0, i_to=2.0))
    assert not r.error, r.error
    assert r.approximate and any("[A115]" in n for n in r.notes)
    est = 1.0 / (2 * math.pi * 30e3 * C)
    assert 0.5 * est < abs(r.nodes[0].metrics["dip"]) < 1.5 * est
    assert r.figures["fc"] == pytest.approx(30e3, rel=0.3)


def test_the_default_step_and_the_slew_note(tmp_path):
    """No load_step: half to full load. A 1 → 15 A step asks the converter's current to slew faster than the
    inductor can, (V_in − V_out)/L: the note; a near short switched on moves the output by most of its voltage: the
    small-signal note [A116]."""
    spec = ConverterSpec("x", "VIN", "GND", i_out=4.0)
    assert (resolved(None, spec).i_from, resolved(None, spec).i_to) == (2.0, 4.0)
    assert resolved(LoadStep(r_load=1.0), spec).i_from == 0.0
    b, cfg, spec = _buck(tmp_path, "slew")
    r = analyse_load_step(b, cfg, spec, LoadStep(i_from=1.0, i_to=15.0))
    assert r.figures["slew_limit"] == pytest.approx((V_IN - V_OUT) / L)
    assert r.figures["slew"] > r.figures["slew_limit"]
    assert any("can do" in n and "[A116]" in n for n in r.notes), r.notes
    r = analyse_load_step(b, cfg, spec, LoadStep(r_load=0.05, l_load=1e-6))
    assert any("beyond a small-signal model" in n for n in r.notes), r.notes
    small = analyse_load_step(b, cfg, spec, LoadStep(i_from=1.0, i_to=1.1))
    assert not any("[A116]" in n for n in small.notes)
    assert RECOVER_BAND == 0.01


def test_the_settings_round_trip():
    """load_step written and read back is the same LoadStep; unknown keys and negative values are refused."""
    from groundtruth.config.settings import ConfigError
    ls = LoadStep(i_from=1.0, i_to=2.5, rise=1e-6, cable_r=0.02, cable_l=5e-7, c_load=1e-4, esr_load=0.01,
                  r_load=None, l_load=0.0, max_dev=0.03)
    from dataclasses import asdict
    back = asdict(_load_step(dict(load_step_data(ls)), "c"))
    assert back == pytest.approx(asdict(ls), rel=1e-9) and back["r_load"] is None
    coil = LoadStep(r_load=2.0, l_load=1e-3)
    assert asdict(_load_step(dict(load_step_data(coil)), "c")) == pytest.approx(asdict(coil), rel=1e-9)
    with pytest.raises(ConfigError):
        _load_step({"fromm": 1}, "c")
    with pytest.raises(ConfigError):
        _load_step({"cable_r": -1}, "c")


def test_the_model_at_no_load(tmp_path):
    """A step from 0 A: the loop at no load (no resistor in Y_o) still has its model."""
    b, cfg, spec = _buck(tmp_path, "zero")
    m, _, err = converter_model(b, cfg, spec, 0.0)
    assert m is not None and not err
    r = analyse_load_step(b, cfg, spec, LoadStep(i_from=0.0, i_to=1.0))
    assert not r.error and r.nodes[0].metrics["dip"] < 0


def test_a_simulated_stage_carries_its_load_step(tmp_path):
    """Simulate of the loop board's buck: the stage's load step (default half to full load) on its result, its own
    Bode view with the open- and closed-loop output impedances and the output's voltage over time, a tile, a
    console line, the plot drawn; its 5.8 % dip is a load_step issue against 5 % (the default), none against 10 %."""
    from matplotlib.figure import Figure

    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.transfer import bode_views
    from groundtruth.config.settings import SettingsFile
    from groundtruth.report.formats import converter_text
    from groundtruth.report.plots import draw_step, has_step
    from groundtruth.ui.kpi import kpis
    g = discrete_buck_board(loop=True)
    g.fields("U8", **{"Control Mode": "peak current", "EA gm": "1.8mmho", "Current Sense Gain": str(K)})
    b, sc, _ = make(tmp_path, g, "sim", nets=())
    spec = ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"], f_sw=F_SW,
                         i_out=I_OUT, v_in=V_IN, v_out=V_OUT)
    sc.add_detected_converters([spec], {})
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    res = simulate(b, cfg)
    (c,) = res.converters
    assert c.load_step is not None and not c.load_step.error
    assert c.load_step.step.i_from == pytest.approx(I_OUT / 2) and c.load_step.step.i_to == pytest.approx(I_OUT)
    (view,) = [v for v in bode_views(res) if v.owner == "buck load step"]
    assert [r.quantity for r in view.responses] == ["impedance", "impedance"] and has_step(view)
    fig = Figure(figsize=(8, 3.4))
    draw_step(fig, view)
    assert any(t[0] == "load step" for t in kpis(c, cfg.rules))
    assert any(line.strip().startswith("load step 1 → 2 A") for line in converter_text(c))
    assert c.summary()["load_step"]["nodes"][0]["node"] == "output"
    assert c.load_step.figures["peak_pct"] == pytest.approx(5.8, abs=0.3)
    assert any(i.check == "load_step" for i in res.issues)
    cfg.converters[0].load_step = LoadStep(max_dev=0.10)
    assert not any(i.check == "load_step" for i in simulate(b, cfg).issues)


# ------------------------------------------------------------------------------------------------ the PDN source
def test_a_stage_is_its_rails_pdn_source(tmp_path):
    """The buck's output rail VOUT as a PDN rail, read at C2's pins (a port, no IC on it), its source found by
    itself: the buck in closed loop (its admittance where its loop holds the output [A117]), the sweep from
    f_c / 30. The field solve's |Z| at the port equals the analytic closed loop 1/(Y_conv + Y_caps) within 5 % from
    1 to 100 kHz (the board's copper and the 10 mΩ sense shunt inside the loop beside it), and the step of 1 A,
    after the copper's spike at the start, the analytic step within 1 % of its dip."""
    from groundtruth.analysis.pdn import converter_rails
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import PdnSpec, SettingsFile
    g = discrete_buck_board(loop=True)
    g.fields("U8", **{"Control Mode": "peak current", "EA gm": "1.8mmho", "Current Sense Gain": str(K)})
    b, sc, _ = make(tmp_path, g, "pdn", nets=(), capacitance=True)
    spec = ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"], f_sw=F_SW,
                         i_out=I_OUT, v_in=V_IN, v_out=V_OUT)
    sc.add_detected_converters([spec], {})
    sc.set_pdn([PdnSpec("VOUT", voltage=V_OUT, i_step=1.0, ports=["C2.1/C2.2"])])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    assert converter_rails(b, cfg) == {"VOUT": ("buck", "")}
    res = simulate(b, cfg)
    (p,) = res.pdn
    assert not p.error, p.error
    assert p.stage == "buck" and "in closed loop" in p.source and p.f_min < 1.5e3
    ana = analyse_load_step(b, cfg, cfg.converters[0], LoadStep(i_from=0.0, i_to=1.0))
    zr = ana.responses[1]
    f, z = np.array(p.ports[0]["f"]), np.array(p.ports[0]["z_ohm"])
    for fi in (1e3, 5e3, 1e4, 3e4, 1e5):
        assert np.interp(fi, f, z) == pytest.approx(np.interp(fi, zr.f, np.abs(zr.h)), rel=0.05), fi
    st = p.responses[0].step
    node = ana.nodes[0]
    t = np.array([1e-7, 1e-6, 5e-6, 1e-5, 3e-5, 1e-4])
    pdn_dv = np.interp(t, st["t"], st["y"]) - V_OUT
    want = np.interp(t, node.t, node.v) - V_OUT
    assert np.max(np.abs(pdn_dv - want)) < 0.01 * abs(node.metrics["dip"])
    m = p.ports[0]["step"]
    assert m["dip_late"] == pytest.approx(node.metrics["dip"], rel=0.01)
    assert m["t_dip"] < 1e-8                       # the copper's spike first (L·ΔI·2π·check_to)
    assert m["peak_late"] < m["peak"]                  # the spike is judged by pdn_impedance, not by pdn_step
    assert any(i.check == "pdn_step" for i in res.issues)      # the dip after it: 6.2 % of 3.3 V


def test_a_port_needs_its_pins_on_the_rail(tmp_path):
    """A port's pins must be on the rail and its ground: else a note, and without another port or IC the rail's
    error names `ports`."""
    from groundtruth.analysis.pdn import _port_pads
    b, cfg, spec = _buck(tmp_path, "port")
    assert isinstance(_port_pads(b, "C2.2/C2.1", "VOUT", "GND"), str)
    fp, rp, gp = _port_pads(b, "C2.1/C2.2", "VOUT", "GND")
    assert fp.reference == "C2" and rp[0].net == "VOUT" and gp[0].net == "GND"
    assert "write it as" in _port_pads(b, "C2", "VOUT", "GND")
