"""Voltage-aware safety checks (analysis/safety.py, the plan's M2): IPC-2221B spacing against copper of known gaps,
capacitor ratings and their derating, ESD protection at connectors, resistor losses against I²R, the switch node's
peak against the Vds rating, and where net voltages come from."""
from types import SimpleNamespace

import pytest

from boardgen import safety_board
from conftest import make
from groundtruth.analysis.safety import after_simulate, cap_rating, net_voltages, part_losses, required_mm
from groundtruth.config.settings import ConfigError, ConverterSpec, SettingsFile, config_from_data
from groundtruth.rules.checks import run_checks

SAFETY = ("clearance_voltage", "cap_voltage", "esd_protection")


def _checks(tmp_path, name, safety=None, **board):
    """Run checks on safety_board(**board) with a `safety:` section: the safety checks' issues."""
    _, sc, path = make(tmp_path, safety_board(**board), name, nets=())
    if safety:
        sc.doc["safety"] = safety
        sc.save()
        sc = SettingsFile.load(sc.path)
    from groundtruth.adapters.file_source import load_board
    return [i for i in run_checks(load_board(path), sc.config).issues if i.check in SAFETY]


def _only(issues, check):
    """The issues of one check."""
    return [i for i in issues if i.check == check]


def test_the_table_is_ipc_2221b_table_6_1():
    """B1 internal, B2 external uncoated, B4 external coated (mm) at the band edges, and above 500 V the 500 V value
    plus 0.0025 / 0.005 / 0.00305 mm per volt (1000 V: 1.5 / 5 / 2.325 mm)."""
    for dv, b1, b2, b4 in ((15, 0.05, 0.1, 0.05), (30, 0.05, 0.1, 0.05), (31, 0.1, 0.6, 0.13), (100, 0.1, 0.6, 0.13),
                           (101, 0.2, 0.6, 0.4), (151, 0.2, 1.25, 0.4), (300, 0.2, 1.25, 0.4), (500, 0.25, 2.5, 0.8)):
        assert required_mm(dv, False)[0] == b1 and required_mm(dv, True)[0] == b2
        assert required_mm(dv, True, coated=True)[0] == b4
    assert required_mm(1000, False)[0] == pytest.approx(1.5)
    assert required_mm(1000, True)[0] == pytest.approx(5.0)
    assert required_mm(1000, True, coated=True)[0] == pytest.approx(2.325)
    assert required_mm(60, True, table=[[50, 1.0], [100, 2.0]])[0] == 2.0      # a table of your own: the next row up


def test_copper_closer_than_the_table_for_its_voltage(tmp_path):
    """+48V beside GND 0.4 mm apart on F.Cu: B2 asks 0.6 mm for 48 V, an error at the measured gap; 0.7 mm apart:
    nothing; coated (B4: 0.13 mm): nothing; on an inner layer (B1: 0.1 mm) 0.4 mm passes and 0.08 mm does not."""
    (i,) = _only(_checks(tmp_path, "near", gap=0.4), "clearance_voltage")
    assert i.severity == "error" and i.layer == "F.Cu" and "GND" in i.message
    assert i.details["gap_mm"] == pytest.approx(0.4, abs=0.01) and i.details["required_mm"] == 0.6
    assert not _only(_checks(tmp_path, "far", gap=0.7), "clearance_voltage")
    assert not _only(_checks(tmp_path, "coated", {"coated": True}, gap=0.4), "clearance_voltage")
    assert not _only(_checks(tmp_path, "inner", gap=0.4, layer="In1.Cu"), "clearance_voltage")
    (i,) = _only(_checks(tmp_path, "tight", gap=0.08, layer="In1.Cu"), "clearance_voltage")
    assert i.details["required_mm"] == 0.1 and "B1" in i.message and i.layer == "In1.Cu"


def test_a_net_of_unknown_voltage_next_to_a_high_one_is_a_warning(tmp_path):
    """The other track on a net with no known voltage (SENSE): taken as 0 V, a warning that says what to set; with
    its voltage given (45 V, 3 V from +48V) nothing."""
    (i,) = _only(_checks(tmp_path, "unknown", gap=0.4, other_net="SENSE"), "clearance_voltage")
    assert i.severity == "warning" and "SENSE" in i.message and "safety: voltages" in i.message
    assert not _only(_checks(tmp_path, "given", {"voltages": {"SENSE": "45V"}}, gap=0.4, other_net="SENSE"),
                     "clearance_voltage")


def test_capacitor_ratings_and_derating(tmp_path):
    """C1 from +48V to GND: 50 V ceramic is over its 50 % derating (a warning), 25 V is over its rating (an error),
    100 V passes; a Voltage field wins over the value's text; without a rating: one info issue naming it."""
    def caps(name, value, **kw):
        """The cap_voltage issues for C1 of this value."""
        return _only(_checks(tmp_path, name, gap=1.0, cap_value=value, **kw), "cap_voltage")
    (w,) = caps("c50", "100n 50V")
    assert w.severity == "warning" and "50 % of its 50 V" in w.message and w.details["kind"] == "ceramic"
    (e,) = caps("c25", "100n/25V")
    assert e.severity == "error" and "25 V rating" in e.message
    assert caps("c100", "100n 100V") == []
    assert caps("field", "100n 25V", C1={"Voltage": "200V"}) == []
    b, _, _ = make(tmp_path, safety_board(cap_value="100n 25V", C1={"Voltage": "200V"}), "fieldb", nets=())
    assert cap_rating(b.footprint("C1")) == (200.0, "C1 Voltage 200V")
    (u,) = caps("unrated", "100n")
    assert u.severity == "info" and "C1" in u.message


def test_esd_parts_at_connectors(tmp_path):
    """J1's SIG with the PESD5V0 diode 2 mm away: protected; without it: an info issue naming J1.1 (not its
    unconnected pin 3); with J1 listed
    as a connector to the outside world: a warning."""
    assert not _only(_checks(tmp_path, "tvs", gap=1.0), "esd_protection")
    (i,) = _only(_checks(tmp_path, "bare", gap=1.0, tvs=False), "esd_protection")
    assert i.severity == "info" and "1 (SIG)" in i.message and i.details["pins"] == ["J1.1"]
    (i,) = _only(_checks(tmp_path, "named", {"esd_connectors": ["J1"]}, gap=1.0, tvs=False), "esd_protection")
    assert i.severity == "warning"
    assert _only(_checks(tmp_path, "far", {"esd_mm": 1.5}, gap=1.0), "esd_protection")      # 2 mm > 1.5 mm


def test_net_voltages_come_from_names_settings_and_series_parts(tmp_path):
    """+48V from its name, GND 0 V, AUX takes +48V's through the 0R R1; the settings win."""
    b, sc, _ = make(tmp_path, safety_board(), "volts", nets=())
    v = net_voltages(b, sc.config)
    assert v["+48V"][0] == 48.0 and v["GND"][0] == 0.0 and "SIG" not in v
    assert v["AUX"][0] == 48.0 and "R1" in v["AUX"][1]
    sc.config.safety.voltages = {"+48V": 54.0}
    assert net_voltages(b, sc.config)["+48V"] == (54.0, "settings (safety: voltages)")


def test_resistor_power_and_the_switch_nodes_peak(tmp_path):
    """An 0805 resistor (1/8 W) in a DC path: its loss is I² times its share of the path's resistance; 2 A through
    20 mΩ is 80 mW: over 50 % of 1/8 W, a warning; 3 A (180 mW) over the rating, an error. A stage's switch node at
    40 V + 30 V overshoot against Vds 80 V: over 80 % of it, a warning; 60 V overshoot: an error."""
    b, sc, _ = make(tmp_path, safety_board(R1={"Vds": "80V"}), "loss", nets=())
    cfg = sc.config
    cfg.components["R1"].model.kind = "resistor"
    cfg.components["R1"].model.params["R"] = 0.02

    def result(amps):
        """A Simulate result with one DC path through R1."""
        path = {"name": "input", "current_a": amps, "parts": [{"name": "R1", "current_a": amps, "r_mohm": 20.0}]}
        return SimpleNamespace(dc_combined=None, converters=[], bridges=[],
                               dc_loads=[SimpleNamespace(net="rail", dc=[path])])
    assert part_losses(result(2.0))["R1"][0] == pytest.approx(0.08)
    (w,) = _only(after_simulate(b, cfg, result(2.0)), "resistor_power")
    assert w.severity == "warning" and "0.08 W" in w.message
    (e,) = _only(after_simulate(b, cfg, result(3.0)), "resistor_power")
    assert e.severity == "error"
    # R1 stands in for the stage's switch here: only its Vds field is read
    cfg.converters = [ConverterSpec("buck", "+48V", "GND", switch=["R1"], v_in=40.0)]

    def stage(overshoot):
        """A Simulate result with the stage's switch-node overshoot."""
        c = SimpleNamespace(net="buck", v_in=40.0, overshoot_v=overshoot, ringing=None, error="")
        return SimpleNamespace(dc_combined=None, converters=[c], bridges=[], dc_loads=[])
    (v,) = _only(after_simulate(b, cfg, stage(30.0)), "vds_rating")
    assert v.severity == "warning" and "70.0 V" in v.message and "80 V" in v.message
    (v,) = _only(after_simulate(b, cfg, stage(60.0)), "vds_rating")
    assert v.severity == "error"
    assert not _only(after_simulate(b, cfg, stage(20.0)), "vds_rating")       # 60 V: under 64 V


def test_the_safety_settings():
    """safety: voltages with units, coated, the derating as percentages; a wrong kind refused."""
    s = config_from_data({"safety": {"voltages": {"VBAT": "52V"}, "coated": True, "cap_derating": {"ceramic": "60%"},
                                     "resistor_derating": "70%", "esd_connectors": ["J1"], "esd_mm": "5mm"}}).safety
    assert s.voltages == {"VBAT": 52.0} and s.coated and s.cap_derating == {"ceramic": pytest.approx(0.6)}
    assert s.resistor_derating == pytest.approx(0.7) and s.esd_connectors == ["J1"] and s.esd_mm == 5.0
    with pytest.raises(ConfigError, match="use ceramic, tantalum, electrolytic"):
        config_from_data({"safety": {"cap_derating": {"mica": 0.5}}})


def test_a_pass_fet_joins_its_drain_and_source_not_its_gate(tmp_path):
    """The surge stopper's FET Q9 (VBATT -> VIN, gate Q9_G) modelled as a short, without pins: VBATT takes VIN's
    voltage, the gate net does not (kraft_flux_mini_2's V_BATT through Q601 was taken as 0 V before)."""
    from boardgen import discrete_buck_board
    from groundtruth.components.models import ComponentModel
    b, sc, _ = make(tmp_path, discrete_buck_board(surge_fet=True), "pass", nets=())
    cfg = sc.config
    cfg.safety.voltages = {"VIN": 24.0}
    cfg.components["Q9"].model = ComponentModel("short")
    v = net_voltages(b, cfg)
    assert v["VBATT"][0] == 24.0 and "Q9" in v["VBATT"][1]
    assert v["Q9_G"] == (34.0, "rides on VIN (+ up to 10 V of gate drive)")     # its gate rides its source


def test_ratings_under_inventrees_names_and_as_fractions(tmp_path):
    """kraft_flux_mini_2's parts carry InvenTree's "Rated Voltage: 50V" and "Rated Power: 1/16W"; a fraction is its
    value (1/16 W was read as 1 W before)."""
    from groundtruth.analysis.safety import resistor_rating
    from groundtruth.components.values import parse_quantity
    assert parse_quantity("1/16W", "W") == (0.0625, "") and parse_quantity("1/8 W", "W") == (0.125, "")
    assert parse_quantity("10uF/16V", "V")[0] is None and parse_quantity("4m7", "ohm")[0] == pytest.approx(4.7e-3)
    b, _, _ = make(tmp_path, safety_board(C1={"Rated Voltage": "63V"}, R1={"Rated Power": "1/16W"}), "inv", nets=())
    assert cap_rating(b.footprint("C1")) == (63.0, "C1 Rated Voltage 63V")
    assert resistor_rating(b.footprint("R1")) == (0.0625, "R1 Rated Power 1/16W")


def test_a_resistor_with_a_real_loss_and_no_rating_is_listed(tmp_path):
    """R1 on a footprint of no known package: 0.1 W in its DC path and no Rated Power field: one info issue naming
    it; 0.01 W: nothing (kraft_flux_mini_2's 8 mΩ shunts carry ~0.2 W each without a rating field)."""
    b, sc, _ = make(tmp_path, safety_board(), "unrated", nets=())
    cfg = sc.config
    cfg.components["R1"].model.kind = "resistor"
    fp = b.footprint("R1")
    fp.lib_id = "Resistor_SMD:R_Shunt_Odd"                # footprint_name follows lib_id

    def result(watts):
        """One DC path dissipating `watts` in R1 (20 mΩ)."""
        amps = (watts / 0.02) ** 0.5
        path = {"name": "in", "current_a": amps, "parts": [{"name": "R1", "current_a": amps, "r_mohm": 20.0}]}
        return SimpleNamespace(dc_combined=None, converters=[], bridges=[],
                               dc_loads=[SimpleNamespace(net="rail", dc=[path])])
    (i,) = _only(after_simulate(b, cfg, result(0.1)), "resistor_power")
    assert i.severity == "info" and "R1 0.1 W" in i.message and i.details["parts"] == ["R1"]
    assert not _only(after_simulate(b, cfg, result(0.01)), "resistor_power")


def test_gate_and_bootstrap_nets_ride_on_their_switch_node(tmp_path):
    """The discrete buck (VIN 24 V) with a gate driver: Q1's gate HG rides on its source SW, Q2's gate LG on
    ground, the bootstrap net BST (a capacitor to SW and a driver pin) on SW, the driver's TGU on HG through the 2R
    gate resistor (so on SW). Between a riding net and SW at most the drive (10 V, or the settings'); the switch
    node swings 0 to 24 V, so beside VIN it is 24 V apart at times (it read 0 V before)."""
    from boardgen import discrete_buck_board
    from groundtruth.analysis.safety import voltage_model
    from groundtruth.config.settings import ConverterSpec
    b, sc, _ = make(tmp_path, discrete_buck_board(bootstrap=True), "boot", nets=())
    cfg = sc.config
    cfg.converters = [ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"],
                                    v_in=24.0, v_out=5.0)]
    vm = voltage_model(b, cfg)
    assert vm.rides["HG"] == ("SW", 10.0) and vm.rides["LG"] == ("GND", 10.0)
    assert vm.rides["BST"] == ("SW", 10.0) and vm.rides["TGU"] == ("SW", 10.0)
    assert vm.base("TGU") == ("SW", 10.0) and vm.text("BST") == "SW + up to 10 V"
    assert vm.dv("BST", "SW") == 10.0 and vm.dv("BST", "HG") == 10.0          # they move together
    assert vm.dv("SW", "VIN") == 24.0 and vm.dv("SW", "GND") == 24.0 and vm.text("SW") == "0-24 V"
    assert vm.dv("BST", "GND") == 34.0 and vm.dv("BST", "VIN") == 24.0       # BST at 10 V while SW is low
    cfg.safety.gate_drive_v = 5.0
    assert voltage_model(b, cfg).dv("BST", "SW") == 5.0


def test_a_bootstrap_capacitor_sees_the_drive_not_the_rail(tmp_path):
    """C7 (100n, rated 16 V) from BST to SW: 10 V across it (the drive), over 50 % of 16 V (ceramic): a warning; it
    was skipped before (BST unknown)."""
    from boardgen import discrete_buck_board
    from groundtruth.config.settings import ConverterSpec
    b, sc, _ = make(tmp_path, discrete_buck_board(bootstrap=True), "bootc", nets=())
    b.footprint("C7").fields["Rated Voltage"] = "16V"
    cfg = sc.config
    cfg.converters = [ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"],
                                    v_in=24.0, v_out=5.0)]
    from groundtruth.analysis.safety import check_capacitors, voltage_model
    found = []
    check_capacitors(b, cfg, voltage_model(b, cfg), cfg.safety, lambda *a, **k: found.append((a, k)))
    ((args, kw),) = [x for x in found if x[0][1] == "C7"]
    assert "sees 10 V" in args[2] and kw["severity"] == "warning"


def test_checks_switched_off_in_preferences_are_left_out(tmp_path):
    """Preferences > Run checks (the user: "Make sure the user can enable/disable each of the settings as needed;
    default enabled"): every check on by default; a check id in Config.checks_off gives no issue (the safety checks
    not run at all), the others are unchanged; prefs_dialog.with_checks turns the unticked boxes into it."""
    pytest.importorskip("wx")
    from dataclasses import replace
    from groundtruth.adapters.file_source import load_board
    from groundtruth.ui.prefs_dialog import CHECK_TOGGLES, checks_off, defaults, with_checks
    from groundtruth.core.issues import CHECKS
    assert checks_off({}) == frozenset() and all(defaults({})[key] for key, *_ in CHECK_TOGGLES)
    assert all(c in CHECKS for *_, ids, _tip in CHECK_TOGGLES for c in ids)
    _, sc, path = make(tmp_path, safety_board(gap=0.1, cap_value="100n 25V", tvs=False), "off", nets=())
    board = load_board(path)
    every = {i.check for i in run_checks(board, sc.config).issues}
    assert set(SAFETY) <= every
    for check in SAFETY:
        cfg = replace(sc.config, checks_off=frozenset({check}))
        assert {i.check for i in run_checks(board, cfg).issues} == every - {check}
    cfg = with_checks(sc.config, {"check_esd_protection": False, "check_cap_voltage": False})
    assert cfg.checks_off == {"esd_protection", "cap_voltage"}
    assert {i.check for i in run_checks(board, cfg).issues} == every - {"esd_protection", "cap_voltage"}
    assert with_checks(sc.config, {}) is sc.config          # nothing changed: the same object


def test_esd_search_distance_from_preferences(tmp_path):
    """Preferences > Run checks > ESD search distance replaces safety.esd_mm: D1 sits 2 mm from J1.1, so SIG is
    protected at the 10 mm default and not at 1 mm; a value out of range falls back to 10 mm."""
    pytest.importorskip("wx")
    from groundtruth.adapters.file_source import load_board
    from groundtruth.ui.prefs_dialog import defaults, with_checks
    _, sc, path = make(tmp_path, safety_board(), "esd", nets=())
    board = load_board(path)

    def sig_flagged(prefs):
        """Whether J1's SIG pin is reported without ESD protection."""
        cfg = with_checks(sc.config, prefs)
        return any("SIG" in i.message for i in run_checks(board, cfg).issues if i.check == "esd_protection")
    assert defaults({})["esd_mm"] == 10.0 and defaults({"esd_mm": 1000})["esd_mm"] == 10.0
    assert not sig_flagged({}) and not sig_flagged({"esd_mm": 10.0})
    assert sig_flagged({"esd_mm": 1.0})
    assert with_checks(sc.config, {"esd_mm": 1.0}).safety.esd_mm == 1.0
