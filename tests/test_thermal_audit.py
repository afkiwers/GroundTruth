"""Audit round 12, the loss bookkeeping Thermal heats the parts with (analysis/thermal.part_losses): a series part
shared by several supply paths (T-1), an integrated switch with an external catch diode (T-3), a DC rail's
connector pins in the capacity search (T-4). Each against the hand sum."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.adapters.file_source import load_board
from groundtruth.analysis.thermal import part_losses
from groundtruth.config.settings import ConverterSpec


def test_T_1_a_part_in_two_supply_paths_is_heated_by_the_summed_current(tmp_path, monkeypatch):
    """Two rails drawing 5 A each through one 10 mΩ part: it carries 10 A and loses 1 W, not 2 × 0.25 W (the paths'
    own I²R; the cross term 2·R·I1·I2 was left out). Unequal currents: R·(I1 + I2)². One path alone: unchanged."""
    import groundtruth.analysis.pins as pins
    board = load_board(discrete_buck_board(surge_fet=True).write(tmp_path / "b.kicad_pcb"))
    ref = next(f.reference for f in board.footprints if f.reference.startswith("Q"))
    cfg = SimpleNamespace(converters=[], dc_loads=[], models={}, model_for=lambda r: None)
    monkeypatch.setattr(pins, "params_cache", lambda b, c: (lambda r: SimpleNamespace(series=0.0)))

    def rail(net, amps):
        """A DC rail's result with the part in its one supply path."""
        return SimpleNamespace(kind="dcload", net=net, dc=[{"name": "rail", "current_a": amps, "joints": [],
                                                           "parts": [{"name": ref, "r_mohm": 10.0}]}])

    def loss(*rails):
        """The part's loss with these rails."""
        return part_losses(board, cfg, list(rails))[0][0]["power_w"]
    assert loss(rail("A", 5.0)) == pytest.approx(0.25)
    assert loss(rail("A", 5.0), rail("B", 5.0)) == pytest.approx(0.010 * 10.0 ** 2)
    assert loss(rail("A", 5.154), rail("B", 1.005)) == pytest.approx(0.010 * 6.159 ** 2)


def test_T_3_an_integrated_switch_with_a_catch_diode_conducts_its_own_share(tmp_path):
    """48 V to 12 V (D = 1/4), 1 A, 0.5 Ω: the switch conducts a quarter of the period whether it is drawn as a
    high-side FET or as an integrated switch with the external diode, D·(I² + ΔI²/12)·R; it read the whole period
    (4× as much) as an integrated switch, with the diode's share counted as well."""
    board, sc, _ = make(tmp_path, discrete_buck_board(), "x", nets=())
    cfg, d = sc.config, 0.25
    kw = dict(switch_node="SW", f_sw=500e3, i_out=1.0, v_in=48.0, v_out=12.0, rise_time=5e-9, rds_on=0.5,
              diode_vf=0.5)

    def conduction(spec):
        """The switch's conduction loss for this stage."""
        rows, _ = part_losses(board, replace(cfg, converters=[spec]),
                              [SimpleNamespace(net=spec.name, duty=d, v_in=48.0, dc=[])])
        return {r["ref"]: r["by"] for r in rows}["Q1"]["conduction"]
    ripple = 48.0 * d * (1 - d) / (cfg.model_for("L1").get("L") * 500e3)
    want = d * (1.0 + ripple ** 2 / 12) * 0.5
    assert conduction(ConverterSpec("buck", "VIN", "GND", high_side=["Q1"], low_side=["D9"], **kw)) == pytest.approx(want)
    assert conduction(ConverterSpec("buck", "VIN", "GND", switch=["Q1"], low_side=["D9"], **kw)) == pytest.approx(want)
    # both switches inside (no rectifier named): the current always flows through the part
    assert conduction(ConverterSpec("buck", "VIN", "GND", switch=["Q1"], **kw)) == pytest.approx(want / d)


def test_T_4_scaling_a_rails_load_scales_its_loads_currents():
    """The capacity search's scaled settings (thermal._scaled): a DC rail's loads follow the factor, so its pins'
    joint and contact heat does (it stayed at the operating point's before)."""
    from groundtruth.analysis.thermal import _scaled
    from groundtruth.config.settings import DcLoad, DcLoadSpec
    rail = DcLoadSpec("rail", "J1.1", [DcLoad("J2.1", 10.0), DcLoad("J3.1", 10.0)])
    other = DcLoadSpec("other", "J1.1", [DcLoad("J4.1", 3.0)])
    cfg = SimpleNamespace(converters=[], dc_loads=[rail, other])
    scaled, _ = _scaled(_AsConfig(cfg), [], "rail", 2.0)
    assert [ld.current for ld in scaled.dc_loads[0].loads] == [20.0, 20.0]
    assert [ld.current for ld in scaled.dc_loads[1].loads] == [3.0]


def _AsConfig(ns):
    """A minimal settings object dataclasses.replace accepts, with the two lists _scaled reads."""
    from dataclasses import dataclass, field

    @dataclass
    class Mini:
        """Just the lists."""
        converters: list = field(default_factory=list)
        dc_loads: list = field(default_factory=list)
    return Mini(list(ns.converters), list(ns.dc_loads))


def test_T_6_the_via_plating_setting_reaches_the_barrels_resistance():
    """analysis.via_plating_um: a barrel's resistance is its plating ring's, so 18 µm reads 25 / 18 of the default's
    (before, only the IPC-2221 capacity and the density followed the setting); None or 0 is the default again."""
    from groundtruth.solver.peec import VIA_PLATING, set_via_plating, via_impedance, via_plating
    try:
        r25 = via_impedance(1.6e-3, 0.3e-3)(0.0).real
        set_via_plating(18.0)
        assert via_plating() == pytest.approx(18e-6)
        assert via_impedance(1.6e-3, 0.3e-3)(0.0).real == pytest.approx(r25 * 25.0 / 18.0)
        # a pin soldered in its hole conducts over the whole hole: no plating in it
        assert via_impedance(1.6e-3, 0.3e-3, filled=True)(0.0).real < r25 / 2
    finally:
        set_via_plating(None)
    assert via_plating() == VIA_PLATING and via_impedance(1.6e-3, 0.3e-3)(0.0).real == pytest.approx(r25)


def test_T_5_gate_drive_and_controller_losses_heat_the_controller_in_thermal(tmp_path):
    """Q_g 12 nC, 5 V, 500 kHz for the stage's one switch and I_q 2 mA from 48 V: 0.03 W + 0.096 W on the
    controller (an integrated stage's IC) when Thermal asks (fixed=True); the efficiency sweep and the input
    estimate add them themselves and ask without."""
    board, sc, _ = make(tmp_path, discrete_buck_board(), "x", nets=())
    spec = ConverterSpec("buck", "VIN", "GND", switch=["Q1"], switch_node="SW", f_sw=500e3, i_out=1.0, v_in=48.0,
                         v_out=12.0, rise_time=5e-9, qg=12e-9, iq=2e-3)
    cfg = replace(sc.config, converters=[spec])
    stage = [SimpleNamespace(net=spec.name, duty=0.25, v_in=48.0, dc=[])]
    plain = {r["ref"]: r for r in part_losses(board, cfg, stage)[0]}
    heat = {r["ref"]: r for r in part_losses(board, cfg, stage, fixed=True)[0]}
    assert "gate drive" not in plain.get("Q1", {}).get("by", {})
    assert heat["Q1"]["by"]["gate drive"] == pytest.approx(12e-9 * 5.0 * 500e3)
    assert heat["Q1"]["by"]["controller"] == pytest.approx(2e-3 * 48.0)


def test_T_2_conduction_losses_are_marked_to_follow_the_temperature(tmp_path):
    """The rows say which watts are an I²R whose resistance follows the temperature: a FET's conduction at
    0.5 %/K (the stage's rds_on_tc when set, none at 0), the winding's DCR at copper's 0.393 %/K; switching
    losses do not."""
    from groundtruth.analysis.thermal import COPPER_TC, RDS_ON_TC
    board, sc, _ = make(tmp_path, discrete_buck_board(), "x", nets=())
    kw = dict(switch_node="SW", f_sw=500e3, i_out=2.0, v_in=24.0, v_out=12.0, rise_time=5e-9, rds_on=0.05,
              dcr=0.02, high_side=["Q1"], low_side=["Q2"])

    def rows(**more):
        """The stage's loss rows."""
        spec = ConverterSpec("buck", "VIN", "GND", **kw, **more)
        return {r["ref"]: r for r in part_losses(board, replace(sc.config, converters=[spec]),
                                                 [SimpleNamespace(net=spec.name, duty=0.5, v_in=24.0, dc=[])])[0]}
    got = rows()
    assert got["Q1"]["tdep"] == {RDS_ON_TC: pytest.approx(got["Q1"]["by"]["conduction"])}
    assert got["Q1"]["power_w"] > got["Q1"]["by"]["conduction"]                 # its switching loss is not in it
    assert got["L1"]["tdep"] == {COPPER_TC: pytest.approx(got["L1"]["by"]["inductor"])}
    assert rows(rds_on_tc=0.008)["Q1"]["tdep"] == {0.008: pytest.approx(got["Q1"]["by"]["conduction"])}
    assert "tdep" not in rows(rds_on_tc=0.0)["Q1"]


def test_T_2_a_hot_parts_conduction_loss_rises_with_its_temperature():
    """The coupled iteration on a one-node model: 1 W of conduction at 0.5 %/K into 50 K/W from 25 °C settles where
    P = 1·(1 + 0.005·(T − 25)) and T = 25 + 50·P: P = 1/(1 − 0.25) = 1.333 W, T = 91.7 °C (75 °C without the
    temperature dependence)."""
    import numpy as np
    from groundtruth.analysis.thermal import _coupled

    class OneNode:
        """A thermal model of one node: 50 K/W to ambient."""
        n = 1

        def solve(self, q, t0, tol=1e-6):
            """Steady state of the node."""
            return np.array([25.0 + 50.0 * q[0]])
    m = SimpleNamespace(th=OneNode(), env=SimpleNamespace(ambient_c=25.0), nodes=np.zeros(0, int))
    one = (np.array([0]), np.array([1.0]), 1.0, 0.005, 0.0)
    plain = _coupled(m, np.zeros(0), np.array([1.0]))
    hot = _coupled(m, np.zeros(0), np.array([1.0]), tdep=[one])
    assert plain.T[0] == pytest.approx(75.0, abs=0.05)
    assert hot.T[0] == pytest.approx(25.0 + 50.0 / 0.75, abs=0.1) and hot.parts_hot == pytest.approx(1 / 0.75 - 1, abs=0.01)


@pytest.mark.parametrize("i_l, delta_i", [(5.0, 4.0), (1.5, 4.0), (1.0, 4.0), (0.2, 4.0), (0.0, 4.0)])
def test_P_4_a_reversed_valley_swings_the_node_before_the_main_switch_turns_on(i_l, delta_i):
    """The node at the hard edge's dead time (20 ns), charged by the reversed valley current |I_v| = ΔI/2 − I_L
    through a linear capacitance holding q_node = 80 nC at 48 V, stepped in time and clamped at the rail by the
    main switch's body diode: the voltage left for the turn-on and the charge the body diode carries equal
    reverse_conduction's residual and main_charge within 0.5 %. Continuous conduction (I_v ≤ 0): nothing swings."""
    from groundtruth.analysis.thermal import reverse_conduction
    v, q_node, t_dead = 48.0, 80e-9, 20e-9
    c = q_node / v
    i_v = max(delta_i / 2 - i_l, 0.0)
    node, diode, dt = 0.0, 0.0, t_dead / 20000
    for _ in range(20000):
        if node < v:
            node = min(node + i_v * dt / c, v)
        else:
            diode += i_v * dt
    rc = reverse_conduction(t_dead, t_dead, i_l, delta_i, q_node)
    assert rc["residual"] == pytest.approx(1.0 - node / v, abs=0.005)
    assert rc["main_charge"] == pytest.approx(diode, rel=0.005, abs=1e-12)
    if i_v == 0:
        assert rc["residual"] == 1.0 and rc["main_charge"] == 0.0
