"""A power stage's operating-point sweep (analysis/converter_sweep.py): the nominal point's row is the stage's own
result, a swept point agrees with a fresh Simulate at that point (re-solved: the independent reference), the load
current's rows are the efficiency sweep's, and the settings, the form, the view, its tiles and the reports carry it."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boardgen import discrete_buck_board  # noqa: E402
from conftest import make  # noqa: E402
from groundtruth.analysis.simulate import simulate  # noqa: E402
from groundtruth.config.settings import ConverterSpec  # noqa: E402

V_IN, I_OUT = 12.0, 2.0


def _stage(tmp_path, name, **spec):
    """The loop board (a peak-current controller, its divider, a sense shunt) with an input connector: (board,
    config with the stage)."""
    g = discrete_buck_board(loop=True)
    g.fields("U8", **{"Control Mode": "peak current", "EA gm": "1.8mmho", "Current Sense Gain": "0.1",
                      "Vref": "0.8V", "FB Ripple Min": "1mV"})
    b, sc, _ = make(tmp_path, g, name, nets=())
    cfg = sc.config
    cfg.components["C2"].model.params.update(C=22e-6, ESR=0.005, ESL=0.5e-9)
    cfg.components["L1"].model.params.update(L=4.7e-6, R=0.02)
    base = dict(switch_node="SW", high_side=["Q1"], low_side=["Q2"], f_sw=500e3, i_out=I_OUT, v_in=V_IN, v_out=3.3,
                rise_time=5e-9, c_oss=1e-9, rds_on=0.02, input_connector=["J1.1", "J1.2"])
    base.update(spec)
    cfg.converters[:] = [ConverterSpec("buck", "VIN", "GND", **base)]
    return b, cfg


@pytest.fixture(scope="module")
def swept(tmp_path_factory):
    """The stage simulated with a sweep of V in (9, 16 V) and I out (0.5, 3 A), emissions, losses and ripple."""
    b, cfg = _stage(tmp_path_factory.mktemp("sweep"), "sw", sweep_v_in=[9.0, 16.0], sweep_i_out=[0.5, 3.0])
    r = simulate(b, cfg)
    return r.converters[0], r


def _ref(tmp_path, v_in, i_out):
    """A fresh Simulate of the stage at that point (re-solved), its switches' C_oss as a datasheet would give it at
    that bus voltage (a junction's C ∝ 1/√V from the stage's 1 nF at 12 V)."""
    b, cfg = _stage(tmp_path, f"ref_{v_in:g}_{i_out:g}", v_in=v_in, i_out=i_out, c_oss=1e-9 * (V_IN / v_in) ** 0.5)
    return simulate(b, cfg).converters[0]


def test_the_switches_charge_follows_the_input_voltage_as_a_junctions():
    """C_oss and Q_oss at another V in: C ∝ 1/√V, and Q the integral of that C from 0 (checked numerically); a boost's
    switches see V out, which the sweep keeps, so theirs stay."""
    import numpy as np
    from groundtruth.analysis.converter_sweep import switch_charge_at
    spec = ConverterSpec("buck", "VIN", "GND", f_sw=500e3, i_out=2.0, v_in=48.0, v_out=12.0, c_oss=600e-12,
                         q_oss=40e-9)
    at = switch_charge_at(spec, 48.0, 24.0)
    assert at["c_oss"] == pytest.approx(600e-12 * 2 ** 0.5)
    v = np.linspace(0.0, 48.0, 400001)[1:]
    c = 600e-12 * (48.0 / v) ** 0.5                                        # C(V) through the nominal point
    q = lambda top: float(np.trapezoid(c[v <= top], v[v <= top]))         # noqa: E731
    assert at["q_oss"] / spec.q_oss == pytest.approx(q(24.0) / q(48.0), rel=2e-3)
    assert switch_charge_at(spec, 48.0, 48.0) == {}
    boost = ConverterSpec("b", "VIN", "GND", topology="boost", c_oss=1e-9, v_in=12.0, v_out=24.0)
    assert switch_charge_at(boost, 12.0, 9.0) == {}


def test_the_nominal_row_is_the_stages_own_result(swept):
    """The nominal point (in both sweeps) gives the stage's own radiated and conducted margins, overshoot, losses,
    FB ripple and phase margin."""
    c, _ = swept
    s = c.sweep
    peak = max(c.overshoot_v, (c.ringing or {}).get("overshoot_v", 0.0), (c.recovery or {}).get("overshoot_v", 0.0))
    assert s is not None and not s.error and s.outputs == ["emissions", "losses", "ripple"]
    nominal = [r for r in s.rows if r["nominal"]]
    assert len(nominal) == 2 and len(s.rows) == 6
    for r in nominal:
        assert r["radiated_db"] == pytest.approx(c.worst["margin_db"], abs=1e-6)
        assert r["conducted_db"] == pytest.approx(c.conducted_worst["margin_db"], abs=1e-6)
        assert r["overshoot_v"] == pytest.approx(peak, rel=1e-6)       # the larger of L·di/dt and the ringing
        assert r["p_loss_w"] == pytest.approx(c.input["p_loss_w"], rel=1e-3)
        assert r["fb_pp"] == pytest.approx(c.fb_ripple.fb_pp, rel=1e-9)
        assert r["pm_deg"] == pytest.approx(c.loop["pm"], abs=1e-6)


@pytest.mark.parametrize("v_in, i_out", [(16.0, I_OUT), (V_IN, 0.5)])
def test_a_swept_point_agrees_with_a_fresh_simulate_there(swept, tmp_path, v_in, i_out):
    """Against the stage simulated afresh at the point (every field solve made again, the refinement at its own
    margins): margins within 0.5 dB, overshoot within 1 %, losses within 2 %, FB ripple within 2 %, phase margin
    within 1°."""
    c, _ = swept
    row = next(r for r in c.sweep.rows if r["v_in"] == v_in and r["i_out"] == i_out)
    ref = _ref(tmp_path, v_in, i_out)
    assert row["radiated_db"] == pytest.approx(ref.worst["margin_db"], abs=0.5)
    assert row["conducted_db"] == pytest.approx(ref.conducted_worst["margin_db"], abs=0.5)
    peak = max(ref.overshoot_v, (ref.ringing or {}).get("overshoot_v", 0.0),
               (ref.recovery or {}).get("overshoot_v", 0.0))
    assert row["overshoot_v"] == pytest.approx(peak, rel=0.01)
    assert row["p_loss_w"] == pytest.approx(ref.input["p_loss_w"], rel=0.02)
    assert row["fb_pp"] == pytest.approx(ref.fb_ripple.fb_pp, rel=0.02)
    assert row["pm_deg"] == pytest.approx(ref.loop["pm"], abs=1.0)


def test_the_load_current_rows_are_the_efficiency_sweeps(swept):
    """At the swept load currents the losses and efficiency are those of the stage's efficiency curve there."""
    from groundtruth.analysis.efficiency import sweep as efficiency_sweep
    c, r = swept
    setup = r.converter_setups[0]
    e = efficiency_sweep(r.model.board, r.model.config, setup.spec, c, [0.5, 3.0])
    rows = {x["i_out"]: x for x in c.sweep.axis("i_out")}
    for k, i in enumerate(e.currents):
        assert rows[i]["p_loss_w"] == pytest.approx(e.p_loss[k], rel=1e-9)
        assert rows[i]["efficiency"] == pytest.approx(e.efficiency[k], rel=1e-9)


def test_the_sweep_settings_round_trip_and_are_checked(tmp_path):
    """`sweep: {v_in, i_out, outputs}` is read and written; unknown outputs, non-positive values and more than
    SWEEP_MAX points are refused."""
    from groundtruth.config.settings import SWEEP_MAX, config_from_data
    data = {"converters": [{"name": "s", "topology": "buck", "rail": "VIN", "ground": "GND", "switch_node": "SW",
                            "high_side": ["Q1"], "low_side": ["Q2"], "f_sw": "500kHz", "i_out": 2, "v_in": 12,
                            "v_out": 3.3, "rise_time": "5ns",
                            "sweep": {"v_in": [9, "16V"], "i_out": "0.5, 3", "outputs": ["losses", "thermal"]}}]}
    c = config_from_data(data).converters[0]
    assert (c.sweep_v_in, c.sweep_i_out, c.sweep_outputs) == ([9.0, 16.0], [0.5, 3.0], ["losses", "thermal"])
    b, cfg = _stage(tmp_path, "rt", sweep_v_in=[9.0, 16.0], sweep_i_out=[0.5], sweep_outputs=["emissions"])
    from groundtruth.config.settings import SettingsFile, settings_path
    sc = SettingsFile.load(settings_path(tmp_path / "rt.kicad_pcb"))
    sc.set_converters(cfg.converters)
    sc.save()
    back = SettingsFile.load(sc.path).config.converters[0]
    assert (back.sweep_v_in, back.sweep_i_out, back.sweep_outputs) == ([9.0, 16.0], [0.5], ["emissions"])
    from groundtruth.config.settings import ConfigError
    for bad in ({"outputs": ["noise"]}, {"v_in": [-1]}, {"i_out": [0.1 + k for k in range(SWEEP_MAX + 1)]}):
        d = {"converters": [dict(data["converters"][0], sweep=bad)]}
        with pytest.raises(ConfigError, match="sweep"):
            config_from_data(d)


def test_the_sweep_is_shown_view_tiles_text_plot_and_report(swept):
    """Its own view after the stages, its tiles (the worst margins, the lowest efficiency, the FB ripple range, the
    lowest phase margin), the console table, both plots and the HTML report's part; the JSON keeps the rows."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from groundtruth.report.formats import converter_text
    from groundtruth.report.html import render_html
    from groundtruth.report.plots import draw_converter_sweep, draw_sweep_table
    from groundtruth.ui.kpi import kpis
    from groundtruth.ui.main_frame import result_views
    c, r = swept
    s = c.sweep
    assert s in result_views(r) and s.label().endswith("sweep (V in, I out)")
    titles = [t[0] for t in kpis(s)]
    assert titles[:3] == ["points", "worst radiated", "worst conducted"]
    assert {"lowest efficiency", "FB ripple", "lowest phase margin"} <= set(titles)
    assert any("sweep of" in line for line in converter_text(c))
    for draw in (draw_converter_sweep, draw_sweep_table):
        fig = Figure(figsize=(9, 8))
        draw(fig, s)
        assert fig.axes
    page = render_html(r.model.board, r.issues, r.routes, "simulation", converters=r.converters)
    assert "id='sweep" in page and "Operating-point sweeps" in page
    js = c.summary()["sweep"]
    assert js["kind"] == "converter_sweep" and len(js["rows"]) == 6


def test_temperatures_are_swept_when_asked_for(tmp_path):
    """With "thermal" among the outputs each point gets a thermal solve: the hottest copper rises with the load."""
    pytest.importorskip("numba")
    b, cfg = _stage(tmp_path, "th", sweep_i_out=[1.0, 4.0], sweep_outputs=["losses", "thermal"])
    c = simulate(b, cfg).converters[0]
    rows = c.sweep.axis("i_out")
    assert all(r.get("t_max_c") is not None for r in rows), [r.get("thermal_error") or r.get("error") for r in rows]
    assert rows[0]["t_max_c"] < rows[-1]["t_max_c"]
