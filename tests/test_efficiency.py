"""A power stage's efficiency sweep (analysis/efficiency.py) against its loss terms' closed forms [A80]: the fixed
losses (each FET's gate charge driven every cycle, the controller's supply), the switches' conduction and the
winding's DCR with the inductor's ripple, I² + ΔI²/12 [Erickson 2001], the copper scaled as I², the energy balance
at the stage's own current against Simulate's input estimate, the end of continuous conduction at ΔI/2; the
measured CSV; the settings."""
import math

import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.analysis.detect import detect_converters
from groundtruth.analysis.efficiency import EFF_V_GATE, read_measured, sweep
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import ConfigError, SettingsFile, config_from_data

QG, IQ, RDS, DCR = 8e-9, 2e-3, 10e-3, 8e-3


@pytest.fixture(scope="module")
def stage(tmp_path_factory):
    """The test buck (12 V → 3 V, 3 A, 300 kHz) simulated once: (board, settings, its result)."""
    t = tmp_path_factory.mktemp("eff")
    board, sc, _ = make(t, discrete_buck_board(), "eff", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.0
    spec.rds_on, spec.dcr, spec.q_oss, spec.qg, spec.iq = RDS, DCR, 10e-9, QG, IQ
    sc.set_converters([spec])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    r = simulate(board, cfg).converters[0]
    assert not r.error and r.efficiency is not None
    return board, cfg, r


def _ripple(cfg, r):
    """ΔI p-p of a buck: V_in·D(1−D)/(L·f_sw) [Erickson 2001], L from the inductor's model."""
    spec = cfg.converters[0]
    l_h = next(cfg.model_for(ref).get("L") for ref in cfg.components if cfg.model_for(ref) is not None
               and cfg.model_for(ref).kind == "inductor")
    return spec.v_in * r.duty * (1 - r.duty) / (l_h * spec.f_sw)


def test_the_fixed_losses_are_the_gates_charge_and_the_controller_s_supply(stage):
    """Gate drive: two FETs × Q_g × V_gate (5 V assumed) × f_sw; the controller: I_q × V_in; both the same at every
    load current."""
    _, cfg, r = stage
    e = r.efficiency
    gate, ctrl = 2 * QG * EFF_V_GATE * 300e3, IQ * 12.0
    assert e.by["gate drive"] == pytest.approx([gate] * len(e.currents))
    assert e.by["controller"] == pytest.approx([ctrl] * len(e.currents))
    assert any("5 V (assumed)" in n for n in e.notes)


def test_conduction_and_the_winding_carry_the_ripple(stage):
    """Between two load currents the conduction and the DCR loss scale as I² + ΔI²/12 (the triangle ripple's mean
    square) - not as I²: at light load the ripple is most of it."""
    _, cfg, r = stage
    e = r.efficiency
    rip = _ripple(cfg, r) ** 2 / 12
    a, b = 0, len(e.currents) - 1
    i1, i2 = e.currents[a], e.currents[b]
    want = (i2 ** 2 + rip) / (i1 ** 2 + rip)
    assert e.by["inductor"][b] / e.by["inductor"][a] == pytest.approx(want, rel=1e-6)
    assert e.by["inductor"][a] == pytest.approx((i1 ** 2 + rip) * DCR, rel=1e-6)
    assert e.by["conduction"][b] / e.by["conduction"][a] == pytest.approx(want, rel=0.02)   # the dead time's share
    assert e.by["conduction"][a] > i1 ** 2 * RDS                                             # the ripple counts


def test_the_copper_scales_as_the_square_of_its_current_and_the_balance_holds(stage):
    """The output path's copper at each current is r·I² (the DC model is linear); at the stage's own current the
    sweep's loss and efficiency are Simulate's input estimate's (the same losses, the gates and the controller in
    both: one operating point has one efficiency, audit round 9, F9-5), and each point's efficiency is
    P_out/(P_out + P_loss)."""
    _, cfg, r = stage
    e = r.efficiency
    r_out = sum(p["r_copper_mohm"] * 1e-3 for p in r.dc if p["name"].startswith("output"))
    k = e.at(3.0)
    assert e.currents[k] == pytest.approx(3.0, rel=0.05)
    if r_out:                                       # an output path: its copper grows as I²
        assert e.by["copper"][-1] >= r_out * e.currents[-1] ** 2 * 0.999
    board, _, _ = stage
    sweep_at = sweep(board, cfg, cfg.converters[0], r, currents=[3.0])
    fixed = sweep_at.by["gate drive"][0] + sweep_at.by["controller"][0]
    # the same losses; the sweep iterates the input current (its path's copper) to the balance, the estimate takes
    # one pass: 0.01 % apart here
    assert sweep_at.p_loss[0] == pytest.approx(r.input["p_loss_w"], rel=1e-3)
    assert sweep_at.efficiency[0] == pytest.approx(r.input["efficiency"], rel=1e-4)
    assert r.input["fixed_w"] == pytest.approx(fixed) and fixed > 0
    for j in range(len(e.currents)):
        assert e.efficiency[j] == pytest.approx(e.p_out[j] / (e.p_out[j] + e.p_loss[j]))
    assert min(e.efficiency) == e.efficiency[0] and e.efficiency[0] < max(e.efficiency) - 0.03   # light load falls


def test_continuous_conduction_ends_at_half_the_ripple(stage):
    """A buck's inductor current reaches zero each cycle below I = ΔI/2: the band the plot shades."""
    _, cfg, r = stage
    assert r.efficiency.ccm_from == pytest.approx(_ripple(cfg, r) / 2, rel=1e-6)


def test_a_measured_csv_is_read_with_a_header_percent_or_fractions(tmp_path):
    """Current, efficiency (% or 0-1), an optional loss; a header row skipped; comma or semicolon; a missing file
    or one without numbers says so."""
    p = tmp_path / "bench.csv"
    p.write_text("I (A),eff (%),loss (W)\n0.5,85.0,0.3\n1,91.5,0.4\n2,93,0.7\n", encoding="utf-8")
    pts, note = read_measured(p)
    assert pts == [(0.5, pytest.approx(0.85), 0.3), (1.0, pytest.approx(0.915), 0.4), (2.0, pytest.approx(0.93), 0.7)]
    assert "3 point(s)" in note
    q = tmp_path / "frac.csv"
    q.write_text("1;0.9\n0.5;0.8\n", encoding="utf-8")
    assert read_measured(q)[0] == [(0.5, 0.8, None), (1.0, 0.9, None)]
    assert read_measured(tmp_path / "none.csv") == ([], f"measured efficiency: {tmp_path / 'none.csv'} not found")
    (tmp_path / "text.csv").write_text("a,b\nc,d\n", encoding="utf-8")
    assert "no rows of numbers" in read_measured(tmp_path / "text.csv")[1]


def test_the_sweep_settings_round_trip_and_are_checked():
    """qg, v_gate, iq, the sweep's currents and the measured CSV are read from a stage; negative values and
    non-positive currents are refused."""
    base = {"name": "b", "rail": "VIN", "ground": "GND", "switch": ["U1"], "f_sw": "300kHz", "i_out": 3,
            "rise_time": "5ns"}
    c = config_from_data({"converters": [dict(base, qg="12nC", v_gate=10, iq="2mA",
                                               efficiency_currents=[0.5, 1, 2], efficiency_measured="bench.csv")]})
    s = c.converters[0]
    assert (s.qg, s.v_gate, s.iq, s.efficiency_currents, s.efficiency_measured) == (
        pytest.approx(12e-9), 10.0, pytest.approx(2e-3), [0.5, 1.0, 2.0], "bench.csv")
    with pytest.raises(ConfigError, match="qg, v_gate and iq must not be negative"):
        config_from_data({"converters": [dict(base, iq=-1)]})
    with pytest.raises(ConfigError, match="efficiency_currents must be positive"):
        config_from_data({"converters": [dict(base, efficiency_currents=[1, 0])]})
    assert math.isfinite(EFF_V_GATE)
