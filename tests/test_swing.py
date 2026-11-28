"""Op-amp outputs held within their supply (analysis/swing.py, emi/mna.Circuit.clamped_dc / clamped_step): the
large-signal step in volts and the output_saturation issue, against the closed forms of a clipped amplifier. The
user, 2026-11-28: "[the op-amp] cannot provide 5V it's supply is 3.3V and its a rail to rail OPA" and "make sure the sim
accounts for supply and rail limits"; then: "in case Vsupply is 5V and the rail to rail is only lets say 4.5V or 90%
of vsupply?" (the Output Swing High / Low fields)."""
import numpy as np
import pytest

from boardgen import opamp_circuit_board
from conftest import make
from groundtruth.components.models import ComponentModel, swing_headroom, swing_value
from groundtruth.config.settings import NetSpec
from groundtruth.emi.mna import Circuit, OpAmpParams


def _gain5() -> Circuit:
    """A non-inverting gain of 5 (4 kΩ / 1 kΩ) with a 1 MHz op-amp, its input the source."""
    c = Circuit()
    c.vsource("in", "0")
    c.opamp("U1", "in", "fb", "out", OpAmpParams(gbw=1e6))
    c.r("out", "fb", 4e3)
    c.r("fb", "0", 1e3)
    return c


def test_a_clamped_step_holds_the_output_at_its_limit_and_lets_go():
    """Gain 5, a 1 V step: linear 5 V, held at an upper limit of 3.3 V (the buffer behind R_O 1 Ω: within 1 mV); a
    0.5 V step stays linear (2.5 V, nothing held); a lower limit 0.5 V below rest holds a −1 V step at −0.5 V; an
    output whose limits exclude its rest (already clipped) starts at the limit and is let go when the drive passes it
    (a 0.4 V step: 2.0 V within 1 … 3.3 V)."""
    c = _gain5()
    t = np.linspace(0.0, 20e-6, 2001)
    assert c.step("out", t)[-1] == pytest.approx(5.0, rel=1e-3)
    r = c.clamped_step(["out"], t, 1.0, {"U1": (-10.0, 3.3)})
    assert r["y"][0][-1] == pytest.approx(3.3, abs=2e-3) and r["final"][0] == pytest.approx(3.3, abs=2e-3)
    assert r["held_end"] == {"U1": "hi"} and "U1" in r["held"]
    first = r["held"]["U1"][0]                     # it reaches the rail at 3.3/5 of the way: t = −τ·ln(1 − 0.66)
    tau = 5.0 / (2 * np.pi * 1e6)
    assert first == pytest.approx(-tau * np.log(1 - 3.3 / 5.0), rel=0.02)
    r = c.clamped_step(["out"], t, 0.5, {"U1": (-10.0, 3.3)})
    assert r["y"][0][-1] == pytest.approx(2.5, rel=1e-3) and not r["held"]
    r = c.clamped_step(["out"], t, -1.0, {"U1": (-0.5, 3.3)})
    assert r["final"][0] == pytest.approx(-0.5, abs=2e-3) and r["held_end"] == {"U1": "lo"}
    r = c.clamped_step(["out"], t, 0.4, {"U1": (1.0, 3.3)})
    assert r["y"][0][0] == pytest.approx(1.0, abs=2e-3) and r["held_start"] == {"U1": "lo"}
    assert r["y"][0][-1] == pytest.approx(2.0, rel=1e-3) and not r["held_end"]


def test_clamped_dc_matches_the_linear_solve_within_the_limits():
    """Without limits (or inside them) clamped_dc is the plain DC solve: gain 5."""
    c = _gain5()
    x, held = c.clamped_dc(0.3, {"U1": (-10.0, 10.0)})
    assert c.output("out") @ x == pytest.approx(1.5, rel=1e-4) and not held
    x, held = c.clamped_dc(0.3, {})
    assert c.output("out") @ x == pytest.approx(1.5, rel=1e-4)


def test_output_swing_values():
    """Volts or a share of the supply, each side its own, read as the datasheet may give them (the user: "in case
    Vsupply is 5V and the rail to rail is only lets say 4.5V or 90% of vsupply"): headroom "10%" / "50mV", or the
    level reached "90%" / "4.5V" (from half the rail up; on ±15 V "13V" high and "-13V" low); a level past the rail
    is rail to rail (said so); nonsense or a share above 100 % refused."""
    assert swing_value("10%") == "10%" and swing_headroom("10%", 0.0, 5.0, "hi") == (pytest.approx(0.5), "")
    assert swing_headroom(swing_value("90%"), 0.0, 5.0, "hi")[0] == pytest.approx(0.5)
    assert swing_headroom(swing_value("4.5V"), 0.0, 5.0, "hi") == (pytest.approx(0.5), "4.5 V reached")
    assert swing_headroom(swing_value("50mV"), 0.0, 5.0, "hi")[0] == pytest.approx(0.05)
    assert swing_headroom(swing_value("13V"), -15.0, 15.0, "hi")[0] == pytest.approx(2.0)
    assert swing_headroom(swing_value("-13V"), -15.0, 15.0, "lo")[0] == pytest.approx(2.0)
    assert swing_headroom(swing_value("0.1"), 0.0, 3.3, "lo")[0] == pytest.approx(0.1)
    room, how = swing_headroom(swing_value("5.5V"), 0.0, 5.0, "hi")
    assert room == 0.0 and "past" in how
    assert swing_value(0.2) == pytest.approx(0.2) and swing_headroom(None, 0.0, 5.0, "hi") == (0.0, "")
    for bad in ("fast", "160%", "1A"):
        with pytest.raises(ValueError):
            swing_value(bad)
    m = ComponentModel.from_dict({"model": "amplifier", "gbw": 1e6, "swing_hi": "10%", "swing_lo": "20mV"})
    assert m.params["swing_hi"] == "10%" and m.params["swing_lo"] == pytest.approx(0.02)
    assert ComponentModel.from_dict(m.to_dict()).params == m.params


GAIN5 = [("R1", "4k", "R", "OUT", "N"), ("R2", "1k", "R", "N", "GND")]


def _path(tmp_path, parts, pins, amplitude=1.0, offset=None, name="sat", through=(), **amp):
    """A generated op-amp board (U3 with `pins`), its path SIG_IN (U1.1) → OUT (U2.1) at `amplitude` from `offset`,
    U3 an amplifier (GBW 1 MHz, `amp` more values): (responses, notes, findings) of simulate.network_responses."""
    from groundtruth.analysis.simulate import network_responses
    spec = NetSpec("SIG_IN", "U1.1", ["U2.1"], 1e3, 1e-6, amplitude_v=amplitude, offset_v=offset,
                   through=list(through))
    board, sc, _ = make(tmp_path, opamp_circuit_board(parts, [("U3", pins)]), name, nets=(spec,))
    cfg = sc.config
    cfg.components["U3"].model = ComponentModel("amplifier", {"gbw": 1e6, **amp})
    return network_responses(board, cfg, spec, "U2.1", np.geomspace(10.0, 1e7, 120))


NONINV = {3: ("+IN", "SIG_IN"), 2: ("-IN", "N"), 1: ("OUT", "OUT"), 5: ("V+", "+3V3"), 4: ("V-", "GND")}


def test_a_gain_of_five_on_3v3_clips_at_the_rail(tmp_path):
    """The user's case: a gain of 5 on a 3.3 V rail-to-rail op-amp, a 1 V step at the driver. The large-signal step
    ends at 3.3 V (the small-signal one at 5 V), the rails drawn at 0 and 3.3 V; an output_saturation warning says it
    would need 5 V and that the path is linear for the driver within 0 … 0.66 V (3.3 / 5)."""
    out, notes, findings = _path(tmp_path, GAIN5, NONINV)
    large = out[0].step["large"]                  # the receiver U2.1 on U3's output net OUT
    assert large["final"] == pytest.approx(3.3, abs=5e-3) and large["y"][-1] == pytest.approx(3.3, abs=5e-3)
    assert large["lin"][-1] == pytest.approx(5.0, rel=5e-3) and large["rails"] == pytest.approx((0.0, 3.3))
    assert large["levels"] == (0.0, 1.0) and large["held"] == ["U3"]
    assert max(large["y"]) <= 3.3 + 5e-3
    sat = [f for f in findings if f["check"] == "output_saturation"]
    assert len(sat) == 1 and sat[0]["severity"] is None
    msg = sat[0]["message"]
    assert "would need 5" in msg and "held at 3.3" in msg and "0 … 0.66 V" in msg, msg


def test_output_swing_fields_set_the_limits(tmp_path):
    """Output Swing High 10 % of 3.3 V: the output stops at 2.97 V; Output Swing Low 50 mV: the lower limit 0.05 V.
    The same upper limit given as the level reached, 2.97 V or 90 %: the same, and the finding says how it was read."""
    out, _, findings = _path(tmp_path, GAIN5, NONINV, name="swing", swing_hi="10%", swing_lo=0.05)
    large = out[0].step["large"]
    assert large["rails"] == pytest.approx((0.05, 2.97))
    assert large["final"] == pytest.approx(2.97, abs=5e-3)
    assert any("0.05 … 2.97 V" in f["message"] for f in findings if f["check"] == "output_saturation")
    for k, hi in enumerate((2.97, "90%")):
        out, _, findings = _path(tmp_path, GAIN5, NONINV, name=f"level{k}", swing_hi=hi)
        assert out[0].step["large"]["rails"] == pytest.approx((0.0, 2.97))
        assert any("reached" in f["message"] for f in findings if f["check"] == "output_saturation")


def test_within_the_limits_the_large_signal_step_is_the_small_signal_one(tmp_path):
    """0.5 V at the driver: 2.5 V out, within 0 … 3.3 V: no finding, the large-signal curve the small-signal one
    times the amplitude (BDF2 on its own grid: within 0.5 %)."""
    out, _, findings = _path(tmp_path, GAIN5, NONINV, amplitude=0.5, name="inside")
    large = out[0].step["large"]
    assert not large["held"] and large["final"] == pytest.approx(2.5, rel=2e-3)
    assert np.max(np.abs(large["y"] - large["lin"])) < 0.005 * 2.5
    assert not [f for f in findings if f["check"] == "output_saturation"]


INV = {3: ("+IN", "GND"), 2: ("-IN", "N"), 1: ("OUT", "OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}
GAIN_M2 = [("R1", "1k", "R", "SIG_IN", "N"), ("R2", "2k", "R", "N", "OUT")]


def test_an_inverting_stage_on_a_single_supply_and_the_low_level(tmp_path):
    """An inverting gain of −2 on one 5 V supply: from 0 V the output would go negative and sits at 0 V (the lower
    limit, held at rest and at the top); with the driver's Low level at −2 V (the driver pin at rest) it starts at
    4 V and steps by the circuit's gain with the driver's 25 Ω, −2000/1025, to 2.05 V: linear."""
    out, _, findings = _path(tmp_path, GAIN_M2, INV, name="inv0")
    large = out[0].step["large"]
    assert large["final"] == pytest.approx(0.0, abs=5e-3) and large["y"][0] == pytest.approx(0.0, abs=5e-3)
    assert any(f["check"] == "output_saturation" for f in findings)
    out, _, findings = _path(tmp_path, GAIN_M2, INV, offset=-2.0, name="inv2")
    large = out[0].step["large"]
    g = 2000.0 / 1025.0
    assert large["y"][0] == pytest.approx(4.0, rel=2e-3) and large["final"] == pytest.approx(4.0 - g, rel=2e-3)
    assert not large["held"] and not [f for f in findings if f["check"] == "output_saturation"]


def test_an_op_amp_with_an_unknown_supply_is_not_limited(tmp_path):
    """V+ on a net whose voltage nothing names: no limits, a note says why."""
    pins = {**NONINV, 5: ("V+", "VA")}
    out, notes, findings = _path(tmp_path, GAIN5, pins, name="unknown")
    assert not any("large" in (x.step or {}) for x in out)
    assert any("not limited" in n for n in notes)


def test_an_overshoot_cut_at_the_rail_is_an_info(tmp_path):
    """A unity-gain Sallen-Key with Q 5 (C1 100 nF, C2 1 nF, 10 kΩ each) on 5 V, the driver 0 → 4.5 V: both levels
    within 0 … 5 V, but the ringing would carry the output well above 5 V: held there, an output_saturation info
    (not a warning), the final value 4.5 V."""
    parts = [("R1", "10k", "R", "SIG_IN", "A"), ("R2", "10k", "R", "A", "B"), ("C1", "100n", "C", "A", "OUT"),
             ("C2", "1n", "C", "B", "GND")]
    pins = {3: ("+IN", "B"), 2: ("-IN", "OUT"), 1: ("OUT", "OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}
    out, _, findings = _path(tmp_path, parts, pins, amplitude=4.5, name="ring")
    large = out[0].step["large"]
    assert max(large["lin"]) > 5.5 and max(large["y"]) <= 5.0 + 5e-3
    assert large["final"] == pytest.approx(4.5, rel=2e-3)
    sat = [f for f in findings if f["check"] == "output_saturation"]
    assert len(sat) == 1 and sat[0]["severity"] == "info" and "during the step" in sat[0]["message"]


def test_a_follower_resting_on_its_rail_is_not_flagged(tmp_path):
    """A unity Sallen-Key (Q 0.707) on one 5 V supply from 0 V: its output rests on the 0 V rail and touches it for
    nanoseconds in the step's first wiggle; that cuts nothing (under 1 mV): no finding, nothing held."""
    parts = [("R1", "10k", "R", "SIG_IN", "A"), ("R2", "10k", "R", "A", "B"), ("C1", "20n", "C", "A", "OUT"),
             ("C2", "10n", "C", "B", "GND")]
    pins = {3: ("+IN", "B"), 2: ("-IN", "OUT"), 1: ("OUT", "OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}
    out, _, findings = _path(tmp_path, parts, pins, amplitude=3.3, name="rest")
    assert not out[0].step["large"]["held"]
    assert not [f for f in findings if f["check"] == "output_saturation"]


def test_a_bipolar_supply_holds_the_output_at_its_negative_rail(tmp_path):
    """An inverting gain of −10 on ±5 V (V− on −5V: a negative rail by its name), the driver 0 → 1 V: the output would
    need −9.76 V (the driver's 25 Ω) and is held at −5 V; the path is linear for the driver within ±5/9.76 V."""
    parts = [("R1", "1k", "R", "SIG_IN", "N"), ("R2", "10k", "R", "N", "OUT")]
    pins = {3: ("+IN", "GND"), 2: ("-IN", "N"), 1: ("OUT", "OUT"), 5: ("V+", "+5V"), 4: ("V-", "-5V")}
    out, _, findings = _path(tmp_path, parts, pins, name="bipolar")
    large = out[0].step["large"]
    assert large["rails"] == pytest.approx((-5.0, 5.0)) and large["final"] == pytest.approx(-5.0, abs=5e-3)
    (sat,) = [f for f in findings if f["check"] == "output_saturation"]
    assert "lower limit" in sat["message"] and "-0.513 … 0.513 V" in sat["message"], sat["message"]


DUAL = {1: ("+INA", "SIG_IN"), 2: ("-INA", "NA"), 3: ("OUTA", "MID"), 4: ("V-", "GND"), 5: ("+INB", "MID"),
        6: ("-INB", "NB"), 7: ("OUTB", "OUT"), 8: ("V+", "+5V")}
DUAL_PARTS = [("R1", "1k", "R", "MID", "NA"), ("R2", "1k", "R", "NA", "GND"),        # U3A: gain 2
              ("R3", "2k", "R", "OUT", "NB"), ("R4", "1k", "R", "NB", "GND")]        # U3B: gain 3


def test_a_dual_op_amps_second_stage_clips_and_its_first_does_not(tmp_path):
    """Two channels of one part on 5 V: U3A gain 2 (2 V at the top: within), U3B gain 3 after it (6 V: held at 5 V).
    One finding, for U3B; the first stage's output response ends at 2 V, the receiver at 5 V; the linear range is
    U3B's: 0 … 0.833 V."""
    out, _, findings = _path(tmp_path, DUAL_PARTS, DUAL, name="dual")
    sat = [f for f in findings if f["check"] == "output_saturation"]
    assert [f["message"].split(":")[0] for f in sat] == ["U3B"], [f["message"] for f in sat]
    assert "0 … 0.833 V" in sat[0]["message"]
    a = next(r for r in out if r.name.endswith("U3A: output")).step["large"]
    assert a["final"] == pytest.approx(2.0, rel=2e-3) and a["rails"] == pytest.approx((0.0, 5.0))
    assert out[0].step["large"]["final"] == pytest.approx(5.0, abs=5e-3)


def test_a_gain_block_is_held_too_and_its_operating_point_is_the_gain_blocks(tmp_path):
    """U3 with a Gain field of 10 (a gain block: the circuit around it not used, in AC and in the DC operating point
    alike) on 3.3 V, the driver 0 → 1 V: held at 3.3 V, linear for the driver within 0 … 0.33 V."""
    pins = {3: ("+IN", "SIG_IN"), 2: ("-IN", "N"), 1: ("OUT", "OUT"), 5: ("V+", "+3V3"), 4: ("V-", "GND")}
    out, _, findings = _path(tmp_path, GAIN5, pins, name="block", gain=10.0)
    assert out[0].step["large"]["final"] == pytest.approx(3.3, abs=5e-3)
    (sat,) = [f for f in findings if f["check"] == "output_saturation"]
    assert "0 … 0.33 V" in sat["message"], sat["message"]


def test_an_op_amp_without_feedback_saturates_without_failing(tmp_path):
    """An open-loop op-amp (+IN the signal, −IN on GND, a comparator by mistake): its gain is A0, so any driver level
    above 0 holds it at the top; a finding, a large-signal step that ends at 3.3 V, nothing raised."""
    pins = {3: ("+IN", "SIG_IN"), 2: ("-IN", "GND"), 1: ("OUT", "OUT"), 5: ("V+", "+3V3"), 4: ("V-", "GND")}
    out, _, findings = _path(tmp_path, [("R1", "10k", "R", "OUT", "GND")], pins, name="open")
    assert any(f["check"] == "output_saturation" for f in findings)
    lg = out[0].step and out[0].step.get("large")
    assert not lg or lg["final"] == pytest.approx(3.3, abs=5e-3)


def test_an_ac_coupled_follower_returns_to_its_bias(tmp_path):
    """C1 1 µF into a follower biased by R1 100 kΩ to GND on 5 V, the driver 0 → 3.3 V: the output jumps to 3.3 V and
    decays back to 0 V (its final value; no rise or overshoot figures), within its limits: no warning."""
    parts = [("C1", "1u", "C", "SIG_IN", "B"), ("R1", "100k", "R", "B", "GND")]
    pins = {3: ("+IN", "B"), 2: ("-IN", "OUT"), 1: ("OUT", "OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}
    out, _, findings = _path(tmp_path, parts, pins, amplitude=3.3, name="ac", through=["C1"])
    large = out[0].step["large"]
    assert max(large["y"]) == pytest.approx(3.3, rel=0.02) and large["final"] == pytest.approx(0.0, abs=1e-3)
    assert not [f for f in findings if f["check"] == "output_saturation" and f["severity"] is None]


def test_an_output_whose_operating_point_is_not_known_is_not_limited(tmp_path):
    """C1 into a follower with no bias resistor: its input has no DC path to anything, so neither has its output (a
    note), and it is not limited (no large-signal step)."""
    parts = [("C1", "1u", "C", "SIG_IN", "B"), ("R2", "10k", "R", "OUT", "GND")]
    pins = {3: ("+IN", "B"), 2: ("-IN", "OUT"), 1: ("OUT", "OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}
    out, notes, findings = _path(tmp_path, parts, pins, name="floating", through=["C1"])
    assert any("operating point of U3 is not known" in n for n in notes), notes
    assert not any("large" in (r.step or {}) for r in out)


def test_output_swing_that_leaves_no_room(tmp_path):
    """Output Swing High 60 % reached (1.98 V on 3.3 V) and Low 2 V of headroom: no room; a note says so, the output
    held at the middle of the two limits (2 V and 1.98 V: 1.99 V)."""
    out, notes, _ = _path(tmp_path, GAIN5, NONINV, name="noroom", swing_hi="60%", swing_lo=2.0)
    assert any("leave no room" in n for n in notes)
    assert out[0].step["large"]["final"] == pytest.approx(1.99, abs=5e-3)


def test_the_low_level_and_the_swing_fields_survive_the_settings_file(tmp_path):
    """offset_v written and read back (and left out when not set); Output Swing fields from a schematic as text
    ("10%", "35mV") become the model's values; a model with them goes through the settings file."""
    from groundtruth.components.fields import read
    from groundtruth.config.settings import SettingsFile
    spec = NetSpec("SIG_IN", "U1.1", ["U2.1"], 1e3, 1e-6, amplitude_v=2.0, offset_v=-1.5)
    g = opamp_circuit_board(GAIN5, [("U3", NONINV)])
    g.fields("U3", **{"Output Swing High": "10%", "Output Swing Low": "35mV"})
    board, sc, _ = make(tmp_path, g, "round", nets=(spec,))
    fp = board.footprint("U3")
    assert read(fp, "Output Swing High").value == "10%"
    assert read(fp, "Output Swing Low").value == pytest.approx(0.035)
    sc.set_component("U3", ComponentModel("amplifier", {"gbw": 1e6, "swing_hi": "10%", "swing_lo": 0.035}))
    sc.set_nets([spec, NetSpec("SIG_B", "U1.1", ["U2.1"], 1e3, 1e-6)])
    sc.save()
    again = SettingsFile.load(sc.path).config
    assert again.nets[0].offset_v == -1.5 and again.nets[1].offset_v is None
    assert again.components["U3"].model.params["swing_hi"] == "10%"
    assert again.components["U3"].model.params["swing_lo"] == pytest.approx(0.035)


def test_the_plot_the_tile_and_the_json_of_a_clipped_path(tmp_path):
    """The step plot draws a clipped path in volts (axis "V", the rails as lines) and the step tile says it clips;
    the JSON carries "large"."""
    from types import SimpleNamespace
    from matplotlib.figure import Figure
    from groundtruth.report.plots import draw_step
    from groundtruth.ui.kpi import _bode
    out, _, _ = _path(tmp_path, GAIN5, NONINV, name="plot")
    view = SimpleNamespace(owner="SIG_IN", responses=out, kind="bode")
    fig = Figure(figsize=(8, 3))
    draw_step(fig, view)
    ax = fig.axes[0]
    assert ax.get_ylabel() == "V"
    flat = [line.get_ydata()[0] for line in ax.get_lines() if len(line.get_ydata()) == 2]
    assert any(abs(v - 3.3) < 1e-9 for v in flat) and any(abs(v) < 1e-9 for v in flat)
    tiles = [t for t in _bode(view) if t[0] == "step"]
    assert tiles and tiles[0][1].startswith("clips at 3.3") and tiles[0][3] == "warn"
    js = out[0].summary()["step"]["large"]
    assert js["held"] == ["U3"] and js["final"] == pytest.approx(3.3, abs=5e-3) and len(js["t"]) == len(js["y"])


def test_a_two_pole_op_amp_does_not_wind_up_at_its_rail():
    """A Q 5 Sallen-Key (10 kΩ, 10 kΩ, 100 nF, 1 nF) whose overshoot (1.73 V for 1 V) is cut at 1.2 V: with a second
    open-loop pole (Fp2 20 MHz) its first stage is bounded too, so it leaves the rail as the one-pole model does
    (within 10 %), not 1.5 ms later after its first stage had integrated the held loop's error (audit 2026-11-28)."""
    def held(fp2):
        """The hold's span and the output at 3 ms."""
        c = Circuit()
        c.vsource("src", "0")
        c.r("src", "a", 10e3)
        c.r("a", "b", 10e3)
        c.c("a", "out", 100e-9)
        c.c("b", "0", 1e-9)
        c.opamp("U", "b", "out", "out", OpAmpParams(gbw=10e6, fp2=fp2))
        t = np.linspace(0, 20e-3, 20001)
        r = c.clamped_step(["out"], t, 1.0, {"U": (-10.0, 1.2)})
        assert max(r["y"][0]) <= 1.2 + 1e-6
        return r["held"]["U"], float(np.interp(3e-3, t, r["y"][0]))
    (a1, b1), y1 = held(None)
    (a2, b2), y2 = held(20e6)
    assert a2 == pytest.approx(a1, rel=0.02) and b2 == pytest.approx(b1, rel=0.1)
    assert y2 == pytest.approx(y1, abs=0.01)


QUAD_UNUSED = {**DUAL, 9: ("+INC", "FLOAT"), 10: ("-INC", "NB"), 11: ("OUTC", "OUTC")}


def test_an_unused_channel_left_open_does_not_switch_the_others_off(tmp_path):
    """The dual's two stages plus a third channel whose + input is on a net nothing else touches and whose − input sits
    on the path's NB (in the circuit, an input drawing no current): its operating point is not known (a note), it is
    not limited, but U3B still clips at 5 V with its finding."""
    out, notes, findings = _path(tmp_path, DUAL_PARTS, QUAD_UNUSED, name="unused")
    assert any("operating point of U3C is not known" in n for n in notes), notes
    assert out[0].step["large"]["final"] == pytest.approx(5.0, abs=5e-3)
    assert [f["message"].split(":")[0] for f in findings if f["check"] == "output_saturation"] == ["U3B"]
