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
