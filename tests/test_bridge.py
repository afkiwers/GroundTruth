"""Motor bridges: the settings entry, detection of the half-bridge legs (no inductor on the board: the motor's
winding is the load), and (below) the six-step drive model and the bridge's solve."""
import pytest

from boardgen import bridge_board, discrete_buck_board
from conftest import make
from groundtruth.analysis.detect import (detect_bridges, detect_converters, detect_nets, pass_switches,
                                         series_sensors, suggest_net)
from groundtruth.config.settings import (BridgeSpec, ConfigError, LegSpec, MotorSpec, SettingsFile, config_from_data,
                                         implausible, simulation_blockers, validate)


def _bridge(**kw) -> BridgeSpec:
    legs = [LegSpec(ph, f"PH{ph}", [f"Q{2 * k + 1}"], [f"Q{2 * k + 2}"], output=f"J1.{ph}")
            for k, ph in enumerate("ABC")]
    return BridgeSpec("bridge J1", "VBUS", "GND", legs=legs, **kw)


def test_a_bridge_round_trips_through_the_settings_and_is_checked(tmp_path):
    """Written block style (legs and motor as one-line maps, units kept), read back the same; an entry that
    cannot be used is refused before the document changes; missing operating and motor data do not make it
    invalid, they block only Simulate; unusual values say what may have been meant; validate checks it against the
    board (each leg's parts on the right nets, pins that exist)."""
    board, sc, _ = make(tmp_path, bridge_board(), "br", nets=())
    b = _bridge(input_connector=["J2.1", "J2.2"], v_bus=48.0, f_pwm=40e3, i_phase=120.0, speed_rpm=3000.0,
                mode="generating", rds_on=1.8e-3, c_oss=1.1e-9, dead_time=47e-9,
                motor=MotorSpec("delta", 0.02, 40e-6, 5.5, 7, "trapezoidal", -5.0), sweep_i_phase=[50.0, 120.0],
                sweep_speed_rpm=[0.0, 3000.0])
    sc.set_bridges([b])
    sc.save()
    text = sc.path.read_text(encoding="utf-8")
    assert "  - name: bridge J1\n" in text and "f_pwm: 40kHz" in text and "dead_time: 47ns" in text
    assert "{phase: A, switch_node: PHA, high_side: Q1, low_side: Q2, output: J1.A}" in text
    assert "motor: {connection: delta, r_ll: 20m, l_ll: 40µH, ke_ll: 5.5, pole_pairs: 7" in text
    assert "sweep: {i_phase: [50.0, 120.0], speed_rpm: [0.0, 3000.0]}" in text
    got = SettingsFile.load(sc.path).config.bridges[0]
    assert got.legs == b.legs and got.mode == "generating" and got.motor.connection == "delta"
    assert got.motor.l_ll == pytest.approx(40e-6) and got.motor.advance_deg == -5.0
    assert got.dead_time == pytest.approx(47e-9) and simulation_blockers(got) == []
    assert got.sweep_i_phase == [50.0, 120.0] and got.sweep_speed_rpm == [0.0, 3000.0]
    assert validate(SettingsFile.load(sc.path).config, board) == []
    # the stages the solver gets: one per leg, named after the bridge, with the leg's phase
    stages = SettingsFile.load(sc.path).config.stages()
    assert [s.name for s in stages] == ["bridge J1 A", "bridge J1 B", "bridge J1 C"]
    assert stages[1].bridge == "bridge J1" and stages[1].phase == "B" and stages[1].switch_node == "PHB"
    assert SettingsFile.load(sc.path).config.stage_specs("bridge J1") == stages
    # refused before the document changes
    with pytest.raises(ConfigError, match="needs rail and ground"):
        sc.set_bridges([BridgeSpec("x", "", "GND", legs=b.legs)])
    assert [x.name for x in sc.config.bridges] == ["bridge J1"] and len(sc.doc["bridges"]) == 1
    with pytest.raises(ConfigError, match="two legs have the same switch_node"):
        config_from_data({"bridges": [{"name": "b", "rail": "V", "ground": "GND", "legs": [
            {"phase": "A", "switch_node": "N", "high_side": "Q1", "low_side": "Q2"},
            {"phase": "B", "switch_node": "N", "high_side": "Q3", "low_side": "Q4"}]}]})
    with pytest.raises(ConfigError, match="used by more than one"):
        config_from_data({"converters": [{"name": "bridge J1 A", "rail": "V", "ground": "GND", "switch": "U1"}],
                          "bridges": [{"name": "bridge J1", "rail": "V", "ground": "GND", "legs": [
                              {"phase": "A", "switch_node": "N", "high_side": "Q1", "low_side": "Q2"}]}]})
    # detected bridges have no operating point or motor yet: valid, but not ready to simulate
    blockers = simulation_blockers(_bridge())
    assert any("v_bus" in m for m in blockers) and any("r_ll" in m for m in blockers)
    assert not any("ke_ll" in m for m in blockers)          # no speed set yet: the EMF is not needed
    msgs = implausible(_bridge(f_pwm=40.0, dead_time=47.0, motor=MotorSpec(l_ll=40.0)))
    assert any("f_pwm" in m and "40k" in m for m in msgs) and any("dead_time" in m and "47n" in m for m in msgs)
    assert any("motor.l_ll" in m and "40µ" in m for m in msgs)
    wrong = _bridge()
    wrong.legs[0].high_side = ["Q2"]                        # a low-side part as high side: pads on PHA and GND
    assert any("Q2 (high side) needs pads on VBUS and PHA" in m for m in validate(
        config_from_data({}).__class__(bridges=[wrong]), board))


def test_detects_the_three_legs_their_drivers_snubbers_outputs_and_the_bus_input(tmp_path):
    """bridge_board: three legs on VBUS, each a GaN FET pair (pins GN / DN / SN) with its phase node going to the
    motor connector J1; found as one bridge with phases A/B/C from J1's pins, each leg's driver (the IC with a SW
    pin on the node) and the inputs it is driven from, its RC snubber, and the bus input J2 behind the current
    sensor U9 (closed between IP+ and IP- only). The legs' high-side FETs are no series pass FETs, and the phase
    nodes are no signal nets."""
    board, sc, _ = make(tmp_path, bridge_board(), "br", nets=())
    found = detect_bridges(board, sc.config)
    assert len(found) == 1
    d = found[0]
    b = d.spec
    assert d.complete and d.confidence == "high" and b.name == "bridge J1" and (b.rail, b.ground) == ("VBUS", "GND")
    assert [(leg.phase, leg.switch_node, leg.high_side, leg.low_side, leg.output) for leg in b.legs] == [
        (ph, f"PH{ph}", [f"Q{2 * k + 1}"], [f"Q{2 * k + 2}"], f"J1.{ph}") for k, ph in enumerate("ABC")]
    assert [(leg.driver, leg.inputs, leg.snubber) for leg in b.legs] == [
        (f"U{k + 1}", [f"PWM_H{ph}", f"PWM_L{ph}"], [f"C{41 + k}", f"R{41 + k}"]) for k, ph in enumerate("ABC")]
    assert b.input_connector == ["J2.1", "J2.2"] and set(d.models) == {"U9"}
    assert d.models["U9"].kind == "short" and d.models["U9"].pins == ["IP+", "IP-"]
    assert b.rise_time == 2e-9 and b.v_sd == 2.0 and b.source == "auto" and not b.reviewed
    assert series_sensors(board, sc.config) == {"U9": ("VBUS", "VIN_RAW")}
    assert not set(pass_switches(board, sc.config)) & set(b.parts)
    assert detect_converters(board, sc.config) == []
    # in the settings: the phase nodes are no signals, the sensor joins only its IP+ / IP- nets in the path search
    sc.add_detected_bridges([b], d.models)
    assert sc.config.model_for("U9").pins == ["IP+", "IP-"]
    assert not {n.spec.name for n in detect_nets(board, sc.config, 0)} & {"PHA", "PHB", "PHC", "VBUS"}
    spec, why = suggest_net(board, sc.config, "PHB")
    assert spec is None and "phase node of leg B of the motor bridge bridge J1 (Bridges tab)" in why
    from groundtruth.analysis.power import input_path
    assert input_path(board, sc.config, "VBUS", "VIN_RAW", {"GND"}) == ["VBUS", "VIN_RAW"]
    assert input_path(board, sc.config, "VBUS", "+3V3", {"GND"}) is None      # not through the sensor's supply


@pytest.mark.parametrize("variant, legs, complete", [
    ({"inductor_on_a": True}, ["B", "C"], False),        # leg A reaches an inductor through a 0R: a switch node
    ({"legs": 2}, ["A", "B"], False),                     # an H-bridge: listed, not simulated yet
    ({"pinfunctions": "DSG", "sense_ic": False, "load_switch": True}, ["A", "B", "C"], True)])
def test_no_leg_where_an_inductor_or_no_low_side_is(tmp_path, variant, legs, complete):
    """A node that reaches an inductor through series parts is a converter's switch node, not a motor leg; a FET
    pair without a FET to ground (the back-to-back load switch Q90 / Q91) is no leg; D/S/G pin names work like
    EPC's; a bridge of other than three legs is found but not simulated yet. A plain synchronous buck has none."""
    board, sc, _ = make(tmp_path, bridge_board(**variant), "v", nets=())
    found = detect_bridges(board, sc.config)
    assert len(found) == 1 and [leg.phase for leg in found[0].spec.legs] == legs
    assert found[0].complete == complete
    assert not {"Q90", "Q91"} & set(found[0].spec.parts)
    if not variant.get("sense_ic", True):
        assert found[0].spec.input_connector == ["J2.1", "J2.2"] and not found[0].models
    buck, bsc, _ = make(tmp_path, discrete_buck_board(), "buck", nets=())
    assert detect_bridges(buck, bsc.config) == [] and len(detect_converters(buck, bsc.config)) == 1


def test_an_esc_with_one_three_phase_driver_and_wire_tabs(tmp_path):
    """Laid out like an ESC: a single three-phase driver (SHA / INHA / INLA ...) for all legs, a wire tab per
    phase, and the battery on two tabs, the minus one behind a current-sense shunt: still one bridge, phases from
    the node names, each leg's own driver inputs, the input on the two tabs."""
    board, sc, _ = make(tmp_path, bridge_board(esc=True), "esc", nets=())
    (d,) = detect_bridges(board, sc.config)
    b = d.spec
    assert d.complete and [leg.phase for leg in b.legs] == ["A", "B", "C"]
    assert [(leg.output, leg.driver, leg.inputs) for leg in b.legs] == [
        (f"J{4 + k}.1", "U1", [f"PWM_H{ph}", f"PWM_L{ph}"]) for k, ph in enumerate("ABC")]
    assert b.input_connector == ["J2.1", "J3.1"] and not d.models


def test_cli_detect_writes_the_bridge(tmp_path, capsys):
    from groundtruth.cli import main
    path = bridge_board().write(tmp_path / "br.kicad_pcb")
    assert main(["init", str(path)]) == 0
    assert main(["detect", str(path), "--write"]) == 0
    out = capsys.readouterr().out
    assert "new    [high  ] bridge J1: 3 legs (A: high Q1 / low Q2 -> J1.A;" in out and "1 bridge(s)" in out
    sc = SettingsFile.load(tmp_path / "groundtruth" / "br.groundtruth.yaml")
    assert [b.name for b in sc.config.bridges] == ["bridge J1"] and sc.config.model_for("U9").pins == ["IP+", "IP-"]


# --------------------------------------------------------------------------- the six-step drive model
import math

import numpy as np

from groundtruth.analysis.motor import SixStep, drive_params, emf, six_step


def _drive(**kw) -> BridgeSpec:
    motor = kw.pop("motor", MotorSpec("star", 0.02, 40e-6, 5.0, 7))
    b = BridgeSpec("b", "V", "GND", legs=[LegSpec(ph, f"PH{ph}", ["Q1"], ["Q2"]) for ph in "ABC"], v_bus=48.0,
                   f_pwm=20e3, i_phase=50.0, speed_rpm=0.0, dead_time=0.0, v_sd=2.0, motor=motor)
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def test_locked_rotor_pwm_steady_state_is_the_rl_formula():
    """M1, M2. Locked rotor (no EMF), A to B through 2R and 2L: with H-PWM / L-ON the loop sees V for D·T and 0
    after, so i_max = (V/2R)(1 - e^(-DT/τ))/(1 - e^(-T/τ)), i_min = i_max·e^(-(1-D)T/τ), mean D·V/2R. A dead time
    before each turn-on shortens the on time and freewheels through reverse conduction (-v_sd) twice per period:
    mean = (V(D - t_dt/T) - 2·v_sd·t_dt/T)/2R. Bipolar: (2D - 1)·V/2R."""
    b = _drive()
    w = six_step(b)
    R, L, V, T = 0.01, 20e-6, 48.0, 50e-6          # star equivalent: R_LL/2, L_LL/2 (no R_DS(on) given)
    tau, D = L / R, w.duty
    assert D == pytest.approx(2 * R * 50 / V, rel=1e-6)
    i_max = V / (2 * R) * (1 - math.exp(-D * T / tau)) / (1 - math.exp(-T / tau))
    assert w.i[:, 0].max() == pytest.approx(i_max, rel=1e-9)
    assert w.i[:, 0].min() == pytest.approx(i_max * math.exp(-(1 - D) * T / tau), rel=1e-9)
    assert np.allclose(w.i[:, 1], -w.i[:, 0]) and np.allclose(w.i[:, 2], 0)
    wd = six_step(_drive(dead_time=1e-6))
    t_dt = 1e-6
    assert wd.mean == pytest.approx(50.0, rel=1e-6)
    assert (V * (wd.duty - t_dt / T) - 2 * 2.0 * t_dt / T) / (2 * R) == pytest.approx(50.0, rel=1e-6)
    wb = six_step(_drive(pwm="bipolar"))
    assert (2 * wb.duty - 1) * V / (2 * R) == pytest.approx(50.0, rel=1e-6)


def test_bus_current_of_a_chopped_constant_current():
    """M4. A large inductance keeps the current constant: the bus carries I during the on time and nothing after,
    so its AC RMS is I·sqrt(D(1-D)) (unipolar); bipolar reverses it in the off time: 2I·sqrt(D(1-D)) around the
    mean (2D-1)·I."""
    for pwm, ac in (("hpwm_lon", lambda I, D: I * math.sqrt(D * (1 - D))),
                    ("bipolar", lambda I, D: 2 * I * math.sqrt(D * (1 - D)))):
        w = six_step(_drive(pwm=pwm, motor=MotorSpec("star", 0.02, 0.1, 5.0, 7)))
        s = w.stats()
        assert s["bus_ac_rms"] == pytest.approx(ac(50.0, w.duty), rel=0.01)


def test_part_losses_of_a_chopped_constant_current():
    """L1. Locked rotor, a large inductance (constant I from A to B), H-PWM / L-ON with a dead time t_dt before
    each turn-on: A's high side conducts I for D·T - t_dt, its low side for (1 - D)·T - t_dt and in reverse
    (v_sd) for 2·t_dt; B's low side always. Per period one hard turn-on (A's high side takes I over V + v_sd:
    ½·(V + v_sd)·I·t_r, output charge Q_oss·V·((V + v_sd)/V)²) and one soft one (A's low side over v_sd). A's
    snubber sees the node swing V + v_sd twice and v_sd twice; the open phase C follows half of A's swings.
    ESR·I_rms² for a capacitor."""
    from types import SimpleNamespace
    from groundtruth.analysis.bridge import bridge_losses
    from groundtruth.components.models import ComponentModel
    b = _drive(motor=MotorSpec("star", 0.02, 0.1, 5.0, 7), dead_time=1e-6, rds_on=2e-3, rise_time=5e-9,
               q_oss=50e-9)
    b.legs = [LegSpec(ph, f"PH{ph}", [f"Q{2 * k + 1}"], [f"Q{2 * k + 2}"], snubber=[f"C{k}", f"R{k}"])
              for k, ph in enumerate("ABC")]
    w = six_step(b)
    models = {"C0": ComponentModel("cap", {"C": 1e-9}), "C1": ComponentModel("cap", {"C": 1e-9}),
              "C2": ComponentModel("cap", {"C": 1e-9}), "C9": ComponentModel("cap", {"C": 10e-6, "ESR": 4e-3})}
    board = SimpleNamespace(footprint=lambda ref: ref)
    config = SimpleNamespace(model_for=models.get)
    rows, notes = bridge_losses(board, config, b, w, [{"name": "C9", "rms_a": 20.0}, {"name": "LISN", "lisn": True,
                                                                                      "rms_a": 1.0}])
    got = {r["ref"]: r["power_w"] for r in rows}
    I, V, vsd, T, t_dt, D, R, tr, Q = 50.0, 48.0, 2.0, 50e-6, 1e-6, w.duty, 2e-3, 5e-9, 50e-9
    hard = 0.5 * (V + vsd) * I * tr / T + Q * V * ((V + vsd) / V) ** 2 / T
    soft = 0.5 * vsd * I * tr / T + Q * V * (vsd / V) ** 2 / T
    assert got["Q1"] == pytest.approx(R * I * I * (D - t_dt / T) + hard, rel=1e-4)
    assert got["Q2"] == pytest.approx(R * I * I * (1 - D - t_dt / T) + vsd * I * 2 * t_dt / T + soft, rel=1e-4)
    assert got["Q4"] == pytest.approx(R * I * I, rel=1e-4) and "Q3" not in got and "Q5" not in got
    swings = 2 * (V + vsd) ** 2 + 2 * vsd ** 2
    assert got["R0"] == pytest.approx(0.5 * 1e-9 * swings / T, rel=1e-6)
    assert got["R2"] == pytest.approx(0.5 * 1e-9 * swings / 4 / T, rel=1e-6) and "R1" not in got
    assert got["C9"] == pytest.approx(4e-3 * 400) and "LISN" not in got


def test_the_slow_and_switching_parts_of_a_chopped_constant_current():
    """R1. Locked rotor, a large inductance, no dead time: A's high side carries I for D·T, a pulse train whose
    mean is I·D (the slow part: no lines below f_pwm/2) and whose n-th harmonic has the peak 2·I·sin(nπD)/(nπ);
    B's low side carries I into ground all the time (-I from ground into the leg, as the unit solves take it).
    What lies above the third band is put in it, so the parts add up to the mean square I²·D."""
    from groundtruth.analysis.bridge_rms import drive_spectra
    w = six_step(_drive(motor=MotorSpec("star", 0.02, 0.1, 5.0, 7), rise_time=1e-12))
    I, D = 50.0, w.duty
    G, C = drive_spectra(w, 20e3)
    assert G[0, 0] == pytest.approx((I * D) ** 2, rel=1e-4)
    assert G[4, 4] == pytest.approx(I * I, rel=1e-4) and G[0, 4] == pytest.approx(-I * I * D, rel=1e-4)
    for n in (1, 2):
        assert abs(C[n][0, 0]) == pytest.approx((2 * I * math.sin(n * math.pi * D) / (n * math.pi)) ** 2, rel=1e-3)
    assert G[0, 0] + sum(C[n][0, 0].real for n in C) / 2 == pytest.approx(I * I * D, rel=1e-4)


def test_energy_balance_and_periodic_steady_state_at_speed():
    """M3, M6. At 3000 rpm, trapezoidal EMF, H-PWM / L-ON with dead time and v_sd = 0 (no reverse-conduction
    loss): the power from the bus equals the back-EMF power plus the copper loss; the steady state found from
    the 120° symmetry repeats over a whole electrical period; generating reaches -I."""
    b = _drive(speed_rpm=3000.0, dead_time=200e-9, v_sd=0.0)
    w = six_step(b)
    p, _ = drive_params(b)
    drive = SixStep(p)
    theta = drive.theta0 + drive.w * w.t
    e = np.array([emf(p, th) for th in theta])
    bus = w.hs.sum(axis=1)
    dt = np.diff(w.t)
    avg = lambda y: float(((y[:-1] + y[1:]) / 2 * dt).sum() / w.period)
    p_bus = 48.0 * avg(bus)
    p_emf = avg((e * w.i).sum(axis=1))
    p_cu = p.r * avg((w.i ** 2).sum(axis=1))
    assert p_bus == pytest.approx(p_emf + p_cu, rel=0.01)
    assert w.i[-1] == pytest.approx(w.i[0], abs=1e-6 * 50)
    assert w.mean == pytest.approx(50.0, rel=1e-6) and w.f_e == pytest.approx(20e3 / w.n)
    g = six_step(_drive(speed_rpm=3000.0, mode="generating"))
    assert g.mean == pytest.approx(-50.0, rel=1e-6) and g.stats()["bus_mean"] < 0


def test_generating_near_the_bus_voltage_settles_and_balances():
    """A starter/generator motor (delta, 30 mΩ / 46 µH, 7.35 V/krpm, 10 pole pairs) generating 55 A at 7000 rpm into
    55 V with a 200 ns dead time: in a dead time with no current an open phase's clamp started it and the drive
    took the seed straight back, over and over at the same instant (Simulate hung for hours). It settles now,
    reaches the current, and the energy balances: the bus's power = the back-EMF's + the copper's + the reverse
    conduction's (v_sd·|i| in the dead times); generating, the bus's and the back-EMF's are negative. (The
    trapezoid average of e·i over the breakpoints is good to about 1 % at 18 PWM periods per electrical period.)"""
    import time
    b = _drive(v_bus=55.0, f_pwm=22e3, i_phase=55.0, speed_rpm=7000.0, mode="generating", dead_time=200e-9,
               rds_on=1.8e-3, motor=MotorSpec("delta", 30e-3, 46e-6, 7.35, 10, "sinusoidal"))
    t0 = time.perf_counter()
    w = six_step(b)
    assert time.perf_counter() - t0 < 10 and w.reached and w.mean == pytest.approx(-55.0, rel=1e-6)
    p, _ = drive_params(b)
    drive = SixStep(p)
    e = np.array([emf(p, th) for th in drive.theta0 + drive.w * w.t])
    dt = np.diff(w.t)
    avg = lambda y: float(((y[:-1] + y[1:]) / 2 * dt).sum() / w.period)
    p_bus = 55.0 * avg(w.hs.sum(axis=1))
    p_emf = avg((e * w.i).sum(axis=1))
    p_cu = p.r * avg((w.i ** 2).sum(axis=1))
    c = w.conduction()
    p_rev = p.v_sd * float(c["hs_rev"].sum() + c["ls_rev"].sum())
    assert p_bus < 0 and p_bus == pytest.approx(p_emf + p_cu + p_rev, rel=0.02)


def test_star_and_delta_with_a_sinusoidal_emf_are_the_same_at_the_terminals():
    """M5. Given by their terminal data, a star and a delta motor with a sinusoidal EMF are the same machine at
    the terminals; with a trapezoidal EMF the delta's star equivalent has no triplen harmonics, and its line-to-line
    EMF peaks at the terminal constant ke_ll (the winding's EMF is 3/4 of it: the triplen
    part drives the circulating current)."""
    kw = dict(speed_rpm=2000.0, dead_time=100e-9)
    ws = six_step(_drive(motor=MotorSpec("star", 0.02, 40e-6, 5.0, 7, "sinusoidal"), **kw))
    wd = six_step(_drive(motor=MotorSpec("delta", 0.02, 40e-6, 5.0, 7, "sinusoidal"), **kw))
    assert wd.duty == pytest.approx(ws.duty, rel=1e-9) and np.allclose(wd.i, ws.i, atol=1e-9 * 50)
    p, _ = drive_params(_drive(motor=MotorSpec("delta", 0.02, 40e-6, 5.0, 7), **kw))
    th = np.linspace(0, 2 * np.pi, 720, endpoint=False)
    ea = np.array([emf(p, x)[0] for x in th])
    spec = np.abs(np.fft.rfft(ea)) / len(th)
    assert spec[3] < 1e-9 * spec[1] and spec[9] < 1e-9 * spec[1]
    eab = np.array([emf(p, x)[0] - emf(p, x)[1] for x in th])
    assert eab.max() == pytest.approx(p.e_ll, rel=1e-9)            # ke_ll is what the terminals show


# --------------------------------------------------------------------------- the bridge on the board
from groundtruth.analysis.power import ringing_response, snubbed_response
from groundtruth.analysis.simulate import MOUNT_L, simulate


@pytest.fixture(scope="module")
def solved(tmp_path_factory):
    """bridge_board with its detected bridge, a 48 V / 40 kHz / 30 A / 3000 rpm star motor and a sweep over 15
    and 30 A, simulated."""
    tmp = tmp_path_factory.mktemp("brsim")
    board, sc, _ = make(tmp, bridge_board(), "br", nets=(), max_nodes=20000)
    (d,) = detect_bridges(board, sc.config)
    b = d.spec
    b.v_bus, b.f_pwm, b.i_phase, b.speed_rpm, b.c_oss = 48.0, 40e3, 30.0, 3000.0, 1e-9
    b.motor = MotorSpec("star", 0.05, 50e-6, 5.0, 7)
    b.sweep_i_phase, b.sweep_speed_rpm = [15.0, 30.0], [3000.0]
    sc.add_detected_bridges([b], d.models)
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    return board, cfg, simulate(board, cfg)


def test_the_legs_share_one_model_and_mirror_legs_match(solved):
    """B3. The three legs are solved on one model (one factorisation per frequency); A and C mirror each other on
    the board, so their hot loops match."""
    board, cfg, r = solved
    legs = [s for s in r.converter_setups if s.spec.bridge == "bridge J1"]
    assert len(legs) == 3 and legs[0].fine.build is legs[1].fine.build is legs[2].fine.build
    la, lc = (s.res.loop_nh for s in (legs[0], legs[2]))
    assert la == pytest.approx(lc, rel=0.03) and 1 < la < 10
    assert all(s.res.kind == "converter" and s.res.bridge == "bridge J1" for s in legs)
    assert [s.res.phase for s in legs] == ["A", "B", "C"]


def test_the_lisn_divider_and_the_capacitor_shares(solved):
    """B4, B5. Below the capacitors' self-resonance the line current per A of a leg's loop current is the divider
    of the three bus capacitors in parallel against both LISNs: |Z_C/(Z_C + 2 Z_LISN)|; at f_pwm every leg's
    current divides among the capacitors (shares adding to 1, the LISN's under 1 %)."""
    board, cfg, r = solved
    from groundtruth.analysis.bridge import _complex_transfer
    from groundtruth.emi.conducted import LISNS
    legs = [s for s in r.converter_setups if s.spec.bridge]
    f = 150e3
    w = 2 * math.pi * f
    zc = []
    for ref in ("C11", "C21", "C31"):
        m = cfg.model_for(ref)
        zc.append(m.get("ESR") + 1j * w * (m.get("ESL") + MOUNT_L) - 1j / (w * m.get("C")))
    z_par = 1 / sum(1 / z for z in zc)
    z_lisn = 2 * LISNS["cispr32"].impedance([f])[0][0]
    for s in legs:
        fs, ts = _complex_transfer(s)
        assert abs(ts[list(fs).index(f)]) == pytest.approx(abs(z_par / (z_par + z_lisn)), rel=0.05)
        assert sum(c["share_sw"] for c in s.res.caps) == pytest.approx(1.0, rel=0.03)


def test_snubbed_ringing_reduces_to_the_series_lc_and_to_the_added_capacitance():
    """B6. Without the snubber (R_s large) the response is the series LC's; with R_s = 0 the snubber capacitor adds
    to C_oss: the resonance moves to 1/(2 pi sqrt(L (C_oss + C_s)))."""
    L, r, c_oss, c_s = 3e-9, 0.1, 1e-9, 1e-9
    f = np.geomspace(1e6, 1e9, 400)
    f_r = 1 / (2 * math.pi * math.sqrt(L * c_oss))
    assert snubbed_response(f, L, r, c_oss, c_s, 1e12) == pytest.approx(
        ringing_response(f, f_r, math.sqrt(L / c_oss) / r), rel=1e-6)
    fg = np.geomspace(1e7, 1e9, 20001)
    peak = fg[np.argmax(snubbed_response(fg, L, 0.01, c_oss, c_s, 0.0))]      # light damping: a sharp peak
    assert peak == pytest.approx(1 / (2 * math.pi * math.sqrt(L * (c_oss + c_s))), rel=1e-3)


def test_simulate_runs_the_bridge_end_to_end(solved, tmp_path):
    """B7. The bridge's drive reaches its current; the legs' hot loops, overshoot and ringing (the snubbers
    damp it); the conducted estimate of all legs together at the input, with an issue when it is over the limit;
    a bridge without its motor data is reported, not simulated."""
    board, cfg, r = solved
    (br,) = r.bridges
    assert br.kind == "bridge" and not br.error and br.operating["reached"]
    assert br.operating["i_reached"] == pytest.approx(30.0, rel=1e-6)
    assert br.conducted and br.conducted_worst is not None and br.conducted_standard.startswith("CISPR 32")
    assert {c["name"] for c in br.caps} == {"C11", "C21", "C31"}
    legs = [c for c in r.converters if c.bridge]
    assert all(c.ringing and "snubber" in c.ringing for c in legs)
    assert not any(c.conducted for c in legs)                 # only the bridge reports the input
    assert any(i.check == "conducted_emission" and i.net == "bridge J1" for i in r.issues)
    assert "waveform" not in br.summary()
    from dataclasses import replace
    cfg2 = replace(cfg, bridges=[replace(cfg.bridges[0], v_bus=None)])
    r2 = simulate(board, cfg2)
    assert r2.bridges[0].error.startswith("not simulated: set the bus voltage") and not r2.converters


def test_rms_currents_from_the_battery_to_the_motor(solved):
    """R2. Each leg's only via between its phase node and the motor pin carries the whole phase current: its RMS
    is the phase current's (the PWM ripple that goes to the motor is left out, < 1 %). The battery's current sensor
    carries the bus current's slow part: at least its mean, at most its RMS (the capacitors take the switching
    part). Every bus capacitor's ESR loss and the snubbers' loss are in the bridge's losses; a part modelled as a
    short has none; the maps stay out of the summary."""
    board, cfg, r = solved
    (br,) = r.bridges
    rms, stats = br.rms, br.drive
    vias = {v["name"].split()[1]: v["current_a"] for v in rms["vias"] if v["name"].split()[1].startswith("PH")}
    assert sorted(vias) == ["PHA", "PHB", "PHC"]
    for k, ph in enumerate("ABC"):
        assert vias[f"PH{ph}"] == pytest.approx(stats["phase_rms"][k], rel=0.01)
    (sensor,) = rms["parts"]
    assert stats["bus_mean"] < sensor["rms_a"] < stats["bus_rms"]
    assert rms["sustained_a_mm2"] > 0 and set(rms["maps"]) >= {"F.Cu", "B.Cu"}
    refs = {x["ref"] for x in br.losses}
    assert {"C11", "C21", "C31"} <= refs and any(x.startswith("R") for x in refs)
    # the sensor is modelled as a short (its 1 mΩ is a numerical stand-in): no loss, a note asking for its R
    assert sensor["name"] not in refs and any(f"{sensor['name']} carries" in n and "short" in n for n in br.notes)
    assert "maps" not in br.summary()["rms"] and "sustained_a_mm2" in br.summary()["rms"]
    assert any(i.check == "current_density" and i.net == "bridge J1" for i in r.issues)
    # the plugin's view of it: a heatmap of the RMS density with its tiles; the drive view lists the losses
    from groundtruth.analysis.bridge import rms_view
    from groundtruth.ui.kpi import kpis
    v = rms_view(br, next(c for c in r.converters if c.bridge))
    assert v.net == "bridge J1" and v.sheet_current is rms["maps"] and v.view_of == "RMS"
    tiles = {k[0]: k for k in kpis(v, cfg.rules)}
    assert {"RMS current density", "copper loss", "busiest via"} <= set(tiles)
    assert tiles["copper loss"][1] == f"{rms['copper_w']:.2f} W" and rms["copper_w"] > 0
    assert "losses" in {k[0] for k in kpis(br, cfg.rules)}


def test_a_factorisation_out_of_memory_is_retried_alone(solved, monkeypatch):
    """SuperLU reports a failed allocation as RuntimeError("SUPERLU_MALLOC fails ..."), which stopped Simulate on
    a laptop with 4 GB free (a large motor-drive board). It is a MemoryError now: the solves that hit it run again one at a time
    after the others, and the results are the same."""
    from dataclasses import replace
    import groundtruth.solver.peec as peec
    board, cfg, r = solved
    real, calls = peec.splu, [0]

    def flaky(*a, **kw):
        calls[0] += 1
        if calls[0] <= 2:
            raise RuntimeError("SUPERLU_MALLOC fails for buf in intCalloc() at line 173 in file memory.c")
        return real(*a, **kw)
    monkeypatch.setattr(peec, "splu", flaky)
    cfg2 = replace(cfg, analysis=replace(cfg.analysis, threads=4),
                   bridges=[replace(cfg.bridges[0], sweep_i_phase=[], sweep_speed_rpm=[])])
    r2 = simulate(board, cfg2)
    assert calls[0] > 2 and not r2.bridges[0].error
    assert r2.bridges[0].conducted_worst["margin_db"] == pytest.approx(r.bridges[0].conducted_worst["margin_db"],
                                                                        abs=0.01)


def test_a_locked_rotor_checks_only_the_switching_leg(solved):
    """At 0 rpm one commutation step is driven: A's high side chops, B's low side is held on, C is off. Only A has
    switching edges, so only A gets hot-loop checks; the bridge says why B and C have none."""
    from dataclasses import replace
    board, cfg, _ = solved
    r = simulate(board, replace(cfg, bridges=[replace(cfg.bridges[0], speed_rpm=0.0, sweep_i_phase=[],
                                                      sweep_speed_rpm=[])]))
    legs = {i.net for i in r.issues if i.check in ("hot_loop", "emi_margin")}
    assert legs <= {"bridge J1 A"} and "bridge J1 A" in legs
    assert any("leg(s) B, C do not switch" in n for n in r.bridges[0].notes)


def test_the_bridge_heats_the_board_and_has_a_phase_current_capacity(solved):
    """T1. The thermal model gets the bridge's RMS heat (its copper loss, as the RMS view reports it) and its parts'
    losses (their sum is the parts' heat); the capacity's loss quadratics pass through the operating point; the
    capacity search finds the phase current at which the board reaches the limit."""
    pytest.importorskip("pyamg")
    pytest.importorskip("numba")
    from dataclasses import replace
    from groundtruth.analysis.thermal import capacity_inputs, run_capacity, run_thermal, thermal_inputs
    board, cfg, _ = solved
    cfg = replace(cfg, thermal=replace(cfg.thermal, quality="draft"),            # 30 A would fuse its thin copper
                  bridges=[replace(cfg.bridges[0], i_phase=6.0)])
    r = simulate(board, cfg)
    (br,) = r.bridges
    results = list(r.converters) + list(r.bridges)
    heat, losses, _ = thermal_inputs(board, cfg, results)
    assert sum(float(p.sum()) for *_, p in heat.values()) == pytest.approx(br.rms["copper_w"], rel=1e-6)
    assert {x["ref"]: x["power_w"] for x in losses} == pytest.approx({x["ref"]: x["power_w"] for x in br.losses})
    inp = capacity_inputs(board, cfg, results)
    assert list(inp["stages"]) == ["bridge J1"] and inp["stages"]["bridge J1"]["i_out"] == 6.0
    for x in br.losses:
        assert sum(inp["loss_terms"]["bridge J1"][x["ref"]]) == pytest.approx(x["power_w"], rel=1e-6)
    th, _ = run_thermal(board, cfg, results)
    assert not th.error and th.t_max > cfg.thermal.ambient_c
    assert th.parts_w == pytest.approx(sum(x["power_w"] for x in br.losses), rel=1e-6)
    (cap,) = run_capacity(board, cfg, results)
    c = cap.capacity
    assert not cap.error and c["stage"] == "bridge J1" and c["i_nominal"] == 6.0
    assert c["bound"] or abs(c["t_hottest"] - c["limit_c"]) < 0.5


def test_the_sweep_reproduces_the_operating_point_and_scales(solved):
    """W1. The sweep re-weights the operating point's solves: its point at the operating point (30 A, 3000 rpm) is
    the full result (losses, RMS density and copper loss, the busiest capacitor, the conducted reading, L·di/dt
    and the radiated margin); at half the current the switched current halves about, so L·di/dt halves and the
    radiated margin gains about 6 dB, and the losses and the copper's heat drop."""
    board, cfg, r = solved
    (br,) = r.bridges
    lo, op = br.sweep
    assert (op["i_phase"], op["speed_rpm"], lo["i_phase"]) == (30.0, 3000.0, 15.0)
    assert op["loss_w"] == pytest.approx(sum(x["power_w"] for x in br.losses), rel=1e-9)
    assert op["sustained_a_mm2"] == pytest.approx(br.rms["sustained_a_mm2"], rel=1e-9)
    assert op["copper_w"] == pytest.approx(br.rms["copper_w"], rel=1e-9)
    assert op["cap_rms"] == pytest.approx(max(c["rms_a"] for c in br.caps if not c["lisn"]), rel=1e-9)
    assert op["conducted_margin_db"] == pytest.approx(br.conducted_worst["margin_db"], abs=1e-9)
    assert op["overshoot_v"] == pytest.approx(br.worst_leg["overshoot_v"], rel=1e-9)
    assert op["radiated_margin_db"] == pytest.approx(br.worst_leg["radiated"]["margin_db"], abs=1e-9)
    ratio = lo["edge_a"] / op["edge_a"]
    assert 0.4 < ratio < 0.6 and lo["overshoot_v"] == pytest.approx(op["overshoot_v"] * ratio, rel=1e-9)
    assert lo["radiated_margin_db"] == pytest.approx(op["radiated_margin_db"] - 20 * math.log10(ratio), abs=1e-9)
    assert lo["loss_w"] < op["loss_w"] and lo["copper_w"] < 0.4 * op["copper_w"]
    assert br.summary()["sweep"] == br.sweep


def test_the_fix_search_takes_the_bridges_issues_to_its_legs(tmp_path):
    """F1. Leg C's low-side GND strip has no vias of its own: the fix search proposes a GND via there, predicts a
    shorter hot loop, and verifies it on the bridge. The bridge's own issues (conducted emission, RMS current
    density) are power issues, not signal routes: no route advice for them."""
    from groundtruth.analysis.suggest import suggest_fixes
    board, sc, _ = make(tmp_path, bridge_board(unstitched="C"), "fix", nets=(), max_nodes=20000)
    (d,) = detect_bridges(board, sc.config)
    b = d.spec
    b.v_bus, b.f_pwm, b.i_phase, b.speed_rpm = 48.0, 40e3, 30.0, 3000.0
    b.motor = MotorSpec("star", 0.05, 50e-6, 5.0, 7)
    sc.add_detected_bridges([b], d.models)
    sc.save()
    res = suggest_fixes(board, SettingsFile.load(sc.path).config, max_fixes=2)
    assert not [s for s in res.suggestions if s.net == "bridge J1"]
    vias = [s for s in res.suggestions if s.net == "bridge J1 C" and s.kind == "via"]
    assert vias, [(s.net, s.kind, s.title) for s in res.suggestions]
    best = min(vias, key=lambda s: s.delta_l)
    assert best.modification.net == "GND" and best.delta_l < -0.02 and best.selected
    assert math.dist(best.modification.at, board.find_pin("Q6.2").position) < 3.0
    row = next(r for r in res.comparison if r.get("kind") == "converter" and r.get("net") == "bridge J1 C")
    assert row["loop_nh"][1] < row["loop_nh"][0]


def test_cli_simulate_reports_the_bridge(tmp_path, capsys):
    """B9. groundtruth simulate: the bridge in the text output (drive, capacitors, conducted) and in the JSON
    report next to its legs."""
    import json
    from groundtruth.cli import main
    board, sc, path = make(tmp_path, bridge_board(), "br", nets=(), max_nodes=20000)
    (d,) = detect_bridges(board, sc.config)
    b = d.spec
    b.v_bus, b.f_pwm, b.i_phase, b.speed_rpm = 48.0, 40e3, 30.0, 3000.0
    b.motor = MotorSpec("star", 0.05, 50e-6, 5.0, 7)
    sc.add_detected_bridges([b], d.models)
    sc.save()
    out = tmp_path / "o"
    main(["simulate", str(path), "--format", "text", "--format", "json", "--out-dir", str(out)])
    text = capsys.readouterr().out
    assert "bridge J1 (bridge J1 A, bridge J1 B, bridge J1 C): six-step hpwm_lon, 48 V, 40 kHz" in text
    assert "bus capacitors' ripple: C" in text and "conducted (CISPR 32" in text
    data = json.loads((out / "groundtruth-report.json").read_text(encoding="utf-8"))
    (bj,) = data["bridges"]
    assert bj["kind"] == "bridge" and bj["operating"]["duty"] > 0 and "waveform" not in bj
    assert sorted(c["net"] for c in data["converters"] if c.get("bridge")) == ["bridge J1 A", "bridge J1 B",
                                                                              "bridge J1 C"]


@pytest.fixture(scope="module")
def solved_ce101(tmp_path_factory):
    """The bridge of `solved` (48 V, 30 A, star motor, 7 pole pairs) against MIL-STD-461 CE101 for aircraft, at
    3000 rpm with a sweep over 1500 and 3000 rpm."""
    tmp = tmp_path_factory.mktemp("brce101")
    board, sc, _ = make(tmp, bridge_board(), "br", nets=(), max_nodes=20000, conducted="mil461", ce101="aircraft")
    (d,) = detect_bridges(board, sc.config)
    b = d.spec
    b.v_bus, b.f_pwm, b.i_phase, b.speed_rpm, b.c_oss = 48.0, 40e3, 30.0, 3000.0, 1e-9
    b.motor = MotorSpec("star", 0.05, 50e-6, 5.0, 7)
    b.sweep_i_phase, b.sweep_speed_rpm = [30.0], [1500.0, 3000.0]
    sc.add_detected_bridges([b], d.models)
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    return board, cfg, simulate(board, cfg)


def test_ce101_of_a_bridge_is_its_commutation_through_the_bus_capacitors_and_leads(solved_ce101):
    """CE101: below 10 kHz the bus current of the six-step drive has lines every electrical frequency, strongest
    at the commutation, 6·f_e (2105 Hz at 3000 rpm, 7 pole pairs). Each leg's transfer to the input lead is the
    divider of the three 15 µF bus capacitors against the leads (2 × (50 µH + 50 mΩ)), refined around their series
    resonance (~2.37 kHz, Q ~14); the probe reads the legs' line phasors added at the lead. Curve #1 (48 V)."""
    from groundtruth.analysis.bridge import _complex_transfer, _column, _interp_complex
    from groundtruth.emi.conducted import CE101_LEAD
    from groundtruth.emi.pwm_lines import line_spectrum
    board, cfg, r = solved_ce101
    br = r.bridges[0]
    w = br.waveform
    assert "curve #1" in br.ce101_limit and br.ce101 and br.ce101_worst
    f6 = 6 * w.f_e
    assert w.f_e == pytest.approx(3000 / 60 * 7, rel=0.01)
    assert abs(br.ce101_worst["f"] - f6) < 50
    # the divider at every CE101 solve away from the resonance, and the resonance found
    zc = [(m.get("ESR"), m.get("ESL") + MOUNT_L, m.get("C")) for m in (cfg.model_for(x) for x in ("C11", "C21", "C31"))]
    f_res = 1 / (2 * math.pi * math.sqrt(100e-6 * sum(c for _, _, c in zc)))
    legs = [s for s in r.converter_setups if s.spec.bridge]
    for s in legs:
        fs, ts = _complex_transfer(s, ce101=True)
        assert 11 < len(fs) <= 20
        for f, t in zip(fs, ts):
            if abs(math.log(f / f_res)) > math.log(1.3):
                wf = 2 * math.pi * f
                z_par = 1 / sum(1 / complex(esr, wf * esl - 1 / (wf * c)) for esr, esl, c in zc)
                z_lead = 2 * CE101_LEAD.impedance(f)[0]
                assert abs(t) == pytest.approx(abs(z_par / (z_par + z_lead)), rel=0.05), f
        assert abs(math.log(fs[int(np.argmax(np.abs(ts)))] / f_res)) < math.log(1.1)
    # the reading at 6·f_e: the legs' high-side lines there times their transfers, added (the receiver's filter
    # passes it at -0.07 dB from the 2100 Hz tuning; the neighbouring lines are 350 Hz away)
    line = 0
    for s in legs:
        f, ph, _ = line_spectrum(w.t, w.hs[:, _column(s, legs)], w.period, w.T, w.params.rise_time, 3e3)
        k = int(np.argmin(np.abs(f - f6)))
        fs, ts = _complex_transfer(s, ce101=True)
        line = line + ph[k] * _interp_complex(np.array([f[k]]), fs, ts)[0]
    gauss = math.exp(-math.log(2) * (2 * (br.ce101_worst["f"] - f6) / 100.0) ** 2)
    assert br.ce101_worst["current_a"] == pytest.approx(abs(line) * gauss, rel=0.02)
    # the sweep: a CE101 margin at each speed
    assert all(row.get("ce101_margin_db") is not None for row in br.sweep)
