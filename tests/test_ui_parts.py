"""M1 (after PCB Thermal): via loading against IPC-2221, the Results tab's KPI tiles, the value under the mouse,
and the theme preference. No window needed (the drawn widgets are tested in test_gui.py)."""
import math
from types import SimpleNamespace as NS

import numpy as np
import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.analysis.ipc import via_capacity


def test_via_capacity_is_the_ipc_2221_internal_conductor_fit():
    """0.3 mm drill, 25 µm plating: barrel area π·(0.3 + 0.025) mm · 0.025 mm = 39.6 mil²; IPC-2221 internal
    conductors: I = 0.024 · ΔT^0.44 · A^0.725 = 0.95 A for 10 K."""
    area = math.pi * (0.3 + 0.025) / 0.0254 * (0.025 / 0.0254)
    assert area == pytest.approx(39.56, abs=0.05)
    assert via_capacity(0.3, 25.0) == pytest.approx(0.024 * 10 ** 0.44 * area ** 0.725, rel=1e-9)
    assert via_capacity(0.3, 25.0) == pytest.approx(0.95, abs=0.01)
    assert via_capacity(0.3, 25.0, rise_c=20.0) / via_capacity(0.3, 25.0) == pytest.approx(2 ** 0.44)
    assert via_capacity(0.6, 25.0) > via_capacity(0.3, 25.0) and via_capacity(0.3, 35.0) > via_capacity(0.3, 25.0)


def test_dc_paths_report_via_load_and_flag_overloaded_vias(tmp_path):
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "buck", nets=(), solve_points=1)
    spec = detect_converters(board, sc.config)[0].spec
    spec.i_out = 20.0                                     # far beyond what the board's vias carry
    sc.add_detected_converters([spec])
    res = simulate(board, sc.config)
    c = res.converters[0]
    vias = [v for p in c.dc for v in p["vias"]]
    assert vias and all(v["capacity_a"] > 0 and v["load_pct"] == pytest.approx(100 * v["current_a"] / v["capacity_a"])
                        for v in vias)
    over = [i for i in res.issues if i.check == "via_overload"]
    assert over and "IPC-2221" in over[0].message and over[0].severity in ("warning", "error")


def test_kpi_tiles_judge_margins_overshoot_and_density():
    from groundtruth.ui.kpi import kpis
    rules = NS(emi_margin_db=6.0, overshoot_warning=0.25, overshoot_error=0.5, current_density_warning=50.0,
               current_density_error=100.0, detour_warning=1.25, detour_error=2.0)
    route = NS(error=None, kind="route", layer_share={"In1.Cu": 0.62, "B.Cu": 0.3}, solves=[{"L_nH": 12.3}],
               l_ratio=1.4, transfers=[], m_excess_db=3.1, worst={"margin_db": -2.5, "f": 120e6}, notes=[])
    t = {k[0]: k for k in kpis(route, rules)}
    assert t["radiated margin"][1] == "-2.5 dB" and t["radiated margin"][3] == "error"
    assert t["loop inductance"][3] == "warn" and t["return current"][1] == "62 % In1.Cu"
    conv = NS(error=None, kind="converter", view_of=None, loop_nh=3.43, loop_mohm=58, loop_f=100e6, overshoot_v=17.2,
              v_in=51.8, ringing=None, caps=[], conducted_worst={"margin_db": 4.0, "f": 0.75e6}, worst=None, cm=None)
    t = {k[0]: k for k in kpis(conv, rules)}
    assert t["L·di/dt"][3] == "warn" and t["conducted margin"][3] == "warn" and t["hot loop"][1] == "3.43 nH"
    path = {"name": "output L1 → J2.1", "current_a": 10, "r_mohm": 7.8, "drop_mv": 78, "loss_w": 0.78,
            "sustained_a_mm2": 131, "sustained_layer": "F.Cu", "peak_a_mm2": 180,
            "vias": [{"name": "via GND @(1, 2)", "current_a": 1.2, "load_pct": 126.0}]}
    dc = NS(error=None, kind="converter", view_of="output L1 → J2.1", dc=[path])
    t = {k[0]: k for k in kpis(dc, rules)}
    assert t["current density"][3] == "error" and t["busiest via"][3] == "error" and "126 %" in t["busiest via"][2]


def test_plane_displacement_has_a_tile_of_its_own():
    """A plane pair's displacement current above the signal current (a resonance of the plane capacitance with the
    vias, from the board-wide model) is not the "main transfer": that stays the via carrying most of the return.
    It gets its own tile, as a multiple of the signal current with the frequency it was taken at."""
    from groundtruth.ui.kpi import kpis
    cap = {"name": "plane capacitance In1.Cu–In2.Cu", "kind": "capacitance", "layers": ["In1.Cu", "In2.Cu"],
           "x": 20.0, "y": 10.0, "fraction": 12.86, "distance_mm": 0.3, "distributed": True, "f": 1.45e9}
    via = {"name": "via GND @(20.00, 11.00)", "kind": "via", "layers": ["In1.Cu", "In2.Cu"], "x": 20.0, "y": 11.0,
           "fraction": 0.3, "distance_mm": 1.0}
    route = NS(error=None, kind="route", layer_share={"In1.Cu": 0.62}, solves=[{"L_nH": 12.3}], l_ratio=1.4,
               transfers=[cap, via], m_excess_db=3.1, worst=None, notes=[])
    t = {k[0]: k for k in kpis(route)}
    assert t["main transfer"][1] == "30 %" and t["main transfer"][2].startswith("via GND")
    assert t["plane displacement"][1] == "12.9× signal"
    assert t["plane displacement"][2] == "In1.Cu–In2.Cu @ 1450 MHz"
    route.transfers = [cap]
    t = {k[0]: k for k in kpis(route)}
    assert "main transfer" not in t and "plane displacement" in t


def test_capacity_tiles_and_labels():
    """A capacity view's tiles: the capacity (red under the operating point, amber within 20 % of it), what limits
    it, the loss; its name in the Results list; the bounds as text."""
    from groundtruth.analysis.thermal import ThermalResult, capacity_text
    from groundtruth.ui.kpi import kpis

    def view(scale, bound=""):
        v = ThermalResult("thermal", "power stages", "", power_w=3.2, copper_w=1.1, parts_w=2.1)
        v.capacity = {"stage": "buck SW1", "i_nominal": 10.0, "i_capacity": 10.0 * scale, "scale": scale,
                      "limit_c": 105.0, "t_hottest": 105.1, "limited_by": "copper at Q301 (F.Cu)", "bound": bound,
                      "solves": 3}
        return v
    t = {k[0]: k for k in kpis(view(1.42))}
    assert t["current capacity"][1] == "14.2 A" and t["current capacity"][3] == "ok"
    assert "operating point 10 A (70 % of it)" in t["current capacity"][2]
    assert t["limited by"][1] == "copper at Q301 (F.Cu)" and "105.1 °C" in t["limited by"][2]
    assert t["total loss"][1] == "3.20 W"
    assert {k[0]: k for k in kpis(view(1.1))}["current capacity"][3] == "warn"
    assert {k[0]: k for k in kpis(view(0.8))}["current capacity"][3] == "error"
    zero = {k[0]: k for k in kpis(view(0.0, "zero"))}["current capacity"]
    assert zero[1] == "0 A" and zero[3] == "error" and "even without load" in zero[2]
    assert capacity_text(view(10.0, "above").capacity) == "≥ 100 A"
    assert view(1.42).label() == "Thermal: buck SW1 at its capacity (14.2 A)"
    assert ThermalResult("thermal", "power stages", "").label() == "Thermal: copper temperature"
    both = view(0.62)
    both.capacity.update(stage="all power stages", i_nominal=None, i_capacity=None,
                         currents={"buck SW1": 6.2, "buck SW2": 3.1})
    t = {k[0]: k for k in kpis(both)}
    assert t["load capacity"][1] == "62 %" and t["load capacity"][3] == "error"
    assert t["load capacity"][2] == "buck SW1 6.2 A, buck SW2 3.1 A"
    assert both.label() == "Thermal: all power stages at their capacity (62 % load)"


def test_the_value_under_the_mouse():
    from groundtruth.report.plots import map_value
    from groundtruth.solver.peec import Grid
    node = np.ones((4, 3), bool)
    g = Grid(10.0, 20.0, 0.5, 4, 3, node, np.ones((3, 3), bool), np.ones((4, 2), bool))
    K = np.arange(12, dtype=float).reshape(4, 3)
    K[3, 2] = np.nan
    assert map_value(("In1.Cu", g, K, "A/mm²"), 11.0, 20.5) == "In1.Cu   x 11.00  y 20.50 mm   7 A/mm²"   # node (2, 1)
    assert map_value(("In1.Cu", g, K, "A/mm²"), 11.4, 21.1).endswith("no copper")                     # node (3, 2)
    assert map_value(("In1.Cu", g, K, "A/mm²"), 30.0, 20.0).endswith("no copper")                     # outside
    assert map_value(("F.Cu", g, K, "°C"), 10.0, 20.0).endswith("0.0 °C")


def test_the_theme_preference_round_trips(tmp_path, monkeypatch):
    pytest.importorskip("wx")                             # CI installs the package without the plugin's wx
    from groundtruth.ui import theme
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(theme.sys, "platform", "win32")
    assert theme.load_pref() == "system"                  # nothing saved yet
    theme.save_pref("dark")
    assert theme.load_pref() == "dark" and (tmp_path / "GroundTruth" / "ui.json").exists()
    theme.save_pref("purple")                             # not a theme: ignored
    assert theme.load_pref() == "dark"
    (tmp_path / "GroundTruth" / "ui.json").write_text("{broken", encoding="utf-8")
    assert theme.load_pref() == "system"
