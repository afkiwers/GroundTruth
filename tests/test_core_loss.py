"""An inductor's core loss [A98] (thermal.core_loss, a stage's core_loss): the maker's point scaled by the Steinmetz
equation P ∝ f^α·ΔI^β [Steinmetz 1892], the duty's factor against a numerical integral of |dB/dt|^α over the
triangle (the improved generalised Steinmetz equation [Venkatachalam 2002]), the loss on the inductor in the part
losses, the same at every current of the efficiency sweep, in the energy balance; the settings."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.analysis.thermal import CORE_ALPHA, CORE_BETA, core_duty_factor, core_loss, part_losses
from groundtruth.config.settings import ConfigError, ConverterSpec, SettingsFile, config_from_data


def _spec(**kw) -> ConverterSpec:
    """A 24 V → 6 V buck at 400 kHz on discrete_buck_board's parts."""
    return ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"],
                         **{"f_sw": 400e3, "i_out": 3.0, "v_in": 24.0, "v_out": 6.0, "rise_time": 5e-9,
                            "dead_rise": 0.0, "dead_fall": 0.0, **kw})


def _igse(duty: float, alpha: float, beta: float, n: int = 200001) -> float:
    """(1/T)∫|dB/dt|^α dt · ΔB^(β−α) of a unit-swing triangle rising for duty·T (T = 1), integrated numerically:
    what the iGSE's loss is proportional to."""
    t = np.linspace(0.0, 1.0, n)
    b = np.where(t < duty, t / duty, 1.0 - (t - duty) / (1.0 - duty))
    slope = np.abs(np.diff(b) / np.diff(t))
    return float(np.sum(slope ** alpha * np.diff(t))) * 1.0 ** (beta - alpha)


@pytest.mark.parametrize("duty", [0.1, 0.25, 0.5, 0.8])
@pytest.mark.parametrize("alpha", [1.0, 1.3, 1.8])
def test_the_duty_factor_is_the_igse_s_integral_over_the_triangle(duty, alpha):
    """core_duty_factor = the triangle's ∫|dB/dt|^α dt over the symmetric triangle's, by numerical integration."""
    assert core_duty_factor(duty, alpha) == pytest.approx(_igse(duty, alpha, 2.2) / _igse(0.5, alpha, 2.2), rel=1e-3)
    assert core_duty_factor(0.5, alpha) == pytest.approx(1.0)
    assert core_duty_factor(duty, alpha) == pytest.approx(core_duty_factor(1 - duty, alpha))


def test_the_point_is_scaled_by_the_steinmetz_exponents():
    """P = P_ref·(f/f_ref)^α·(ΔI/ΔI_ref)^β·F(D): alone the loss as given; each reference scales its own factor; the
    assumed exponents are said."""
    p, how, notes = core_loss(_spec(core_loss=0.15), 2.0, 0.25)
    assert p == pytest.approx(0.15) and "this operating point" in how and not notes
    p, _, notes = core_loss(_spec(core_loss=0.15, core_loss_f=200e3), 2.0, 0.25)
    assert p == pytest.approx(0.15 * 2 ** CORE_ALPHA) and any("core_alpha 1.3" in n for n in notes)
    p, _, notes = core_loss(_spec(core_loss=0.15, core_loss_f=200e3, core_loss_di=1.0, core_alpha=1.5, core_beta=2.5),
                            2.0, 0.25)
    want = 0.15 * 2 ** 1.5 * 2 ** 2.5 * (0.25 ** -0.5 + 0.75 ** -0.5) / 2 ** 1.5
    assert p == pytest.approx(want) and not notes
    p, _, notes = core_loss(_spec(core_loss=0.15, core_loss_di=1.0), 2.0, 0.5)
    assert p == pytest.approx(0.15 * 2 ** CORE_BETA) and any("core_beta 2.2" in n for n in notes)
    # a point at a ripple, but the stage's ripple unknown: left out, said
    p, _, notes = core_loss(_spec(core_loss=0.15, core_loss_di=1.0), 0.0, 0.5)
    assert p is None and any("ripple is unknown" in n for n in notes)
    assert core_loss(_spec(), 2.0, 0.25) == (None, "", [])


@pytest.fixture(scope="module")
def buck(tmp_path_factory):
    """discrete_buck_board and its settings."""
    b, sc, _ = make(tmp_path_factory.mktemp("core"), discrete_buck_board(), "core", nets=())
    return b, sc.config


def test_the_core_loss_heats_the_inductor_whatever_the_load(buck):
    """In the part losses it is on the inductor, in its own category, beside the winding's; at a tenth of the load
    it is the same (the ripple does not follow the load)."""
    board, cfg = buck
    di = 24.0 * 0.25 * 0.75 / (cfg.model_for("L1").get("L") * 400e3)
    spec = _spec(dcr=10e-3, core_loss=0.2, core_loss_f=300e3, core_loss_di=1.0, core_alpha=1.4, core_beta=2.4)
    want = 0.2 * (400 / 300) ** 1.4 * di ** 2.4 * core_duty_factor(0.25, 1.4)
    for i in (3.0, 0.3):
        rows, _ = part_losses(board, replace(cfg, converters=[replace(spec, i_out=i)]),
                              [SimpleNamespace(net="buck", duty=0.25, v_in=24.0, dc=[])])
        by = {r["ref"]: r["by"] for r in rows}["L1"]
        assert by["inductor core"] == pytest.approx(want, rel=1e-9)
        assert by["inductor"] == pytest.approx((i * i + di * di / 12) * 10e-3, rel=1e-9)


def test_the_sweep_and_the_energy_balance_hold_it(tmp_path):
    """Simulated: the efficiency sweep's "inductor core" is the given loss at every current, the stage's input
    estimate has it (the loss with it minus the loss without: the core loss, to the input path's copper), and the
    sweep says when it is left out."""
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    board, sc, _ = make(tmp_path, discrete_buck_board(), "cs", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out, spec.rds_on, spec.dcr = 300e3, 3.0, 12.0, 3.0, 10e-3, 8e-3
    out = {}
    for core in (None, 0.12):
        spec.core_loss = core
        sc.set_converters([spec])
        sc.save()
        r = simulate(board, SettingsFile.load(sc.path).config).converters[0]
        assert not r.error
        out[core] = r
    e = out[0.12].efficiency
    assert e.by["inductor core"] == pytest.approx([0.12] * len(e.currents))
    assert "inductor core" not in out[None].efficiency.by
    assert any("no core_loss" in n for n in out[None].efficiency.notes)
    assert not any("no core_loss" in n for n in e.notes)
    assert out[0.12].input["p_loss_w"] - out[None].input["p_loss_w"] == pytest.approx(0.12, rel=0.01)
    assert out[0.12].input["efficiency"] < out[None].input["efficiency"]


def test_the_settings_round_trip_and_are_checked(tmp_path):
    """core_loss and its point are read with their units, written back, and refused when impossible."""
    base = {"name": "b", "rail": "VIN", "ground": "GND", "switch": ["U1"]}
    c = config_from_data({"converters": [dict(base, core_loss="150mW", core_loss_f="300kHz", core_loss_di=2,
                                              core_alpha=1.4, core_beta=2.5)]})
    s = c.converters[0]
    assert (s.core_loss, s.core_loss_f, s.core_loss_di, s.core_alpha, s.core_beta) == pytest.approx(
        (0.15, 300e3, 2.0, 1.4, 2.5))
    for bad, msg in ((dict(core_loss=-1), "core_loss must not be negative"),
                     (dict(core_loss=0.1, core_loss_di=0), "must be positive"),
                     (dict(core_loss=0.1, core_beta=9), "between 0.5 and 4")):
        with pytest.raises(ConfigError, match=msg):
            config_from_data({"converters": [dict(base, **bad)]})
    board, sc, _ = make(tmp_path, discrete_buck_board(), "rt", nets=())
    sc.set_converters([_spec(core_loss=0.15, core_loss_f=300e3, core_loss_di=2.0, core_alpha=1.4)])
    sc.save()
    back = SettingsFile.load(sc.path).config.converters[0]
    assert (back.core_loss, back.core_loss_f, back.core_loss_di, back.core_alpha, back.core_beta) == (
        pytest.approx(0.15), pytest.approx(300e3), 2.0, 1.4, None)
