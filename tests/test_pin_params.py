"""Through-hole pin values (analysis/pins.py), against analytic results: each value falls back on its own (GT field,
the part's settings, the board's defaults, built in); the pin column is its conductors' σ·A summed, soldered and
unsoldered parts in series; a partly soldered pin feeds the board where the solder ends, and the drop then grows by
exactly the current times the column's extra resistance; a joint and a contact add I·R per fed pin to the drop
and I²·R of heat at the part; a connector with a lot of current and no contact value gets a note. The user: "do we
assume a solid connection between the connector and the via ... introduce a GT field parameter ... to define the
solder joint resistance", then "implement all of them but allow default values for each"."""
import math
from dataclasses import replace

import pytest

from boardgen import distribution_board
from conftest import make
from groundtruth.analysis import pins
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import ComponentEntry, DcLoad, DcLoadSpec, SettingsFile
from groundtruth.solver.peec import SIGMA_CU, VIA_PLATING


def rail(tmp_path, name, amps=5.0):
    """The distribution board with one DC rail: J1.2 feeds `amps` to J2.2, returning through J2.1 to J1.1."""
    board, sc, _ = make(tmp_path, distribution_board(), name, nets=())
    sc.set_dc_loads([DcLoadSpec("VDD rail", "J1.2", [DcLoad("J2.2", amps, "J2.1")], "J1.1")])
    sc.save()
    return board, SettingsFile.load(sc.path).config


def with_part(cfg, ref, values):
    """The settings with a part's own pin values (its Pins cell)."""
    comps = dict(cfg.components)
    comps[ref] = replace(comps[ref], tht=values)
    return replace(cfg, components=comps)


def load_of(res) -> dict:
    """The rail's one load row."""
    return res.dc_loads[0].loads[0]


def test_each_value_falls_back_on_its_own(tmp_path):
    """GT field first, then the part's settings, then the board's defaults, then built in: per value, and each
    value's source is kept (the Parts tab and the notes show it)."""
    board, cfg = rail(tmp_path, "fallback")
    board.footprint("J1").fields["GT"] = "contact:0.5m"
    cfg = with_part(cfg, "J1", {"fill": 0.75, "contact": 1e-3})
    cfg = replace(cfg, tht={"pin": "brass", "contact": 2e-3, "joint": 1e-4})
    p = pins.params_for(board, cfg, "J1")
    assert (p.contact, p.fill, p.pin, p.joint, p.pin_d) == (pytest.approx(5e-4), 0.75, "brass", 1e-4, None)
    assert p.source == {"contact": "GT field", "fill": "part setting", "pin": "board default", "joint": "board default"}
    assert p.series == pytest.approx(6e-4) and not p.plain and not p.given("pin_d")
    q = pins.params_for(board, replace(cfg, tht={}), "J3")                 # nothing anywhere: built in
    assert q.plain and q.series == 0 and q.source == {}


def test_values_read_and_written_as_text_and_settings(tmp_path):
    """The GT field's words beside its labels; units and percentages; bad values are problems; the settings keep a
    part's values when its model is set, and the board's defaults round-trip."""
    vals, problems = pins.parse_words("conv_in, contact: 0.5mΩ; fill:75%, pin:Phosphor-Bronze, pin_d:1.1mm, joint:50u")
    assert vals == {"contact": pytest.approx(5e-4), "fill": 0.75, "pin": "bronze", "pin_d": 1.1,
                    "joint": pytest.approx(5e-5)} and not problems
    assert pins.parse_words("fill:120, pin:gold, contact:x")[1] and len(pins.parse_words("fill:120, pin:gold")[1]) == 2
    from groundtruth.analysis.tags import parse
    tags, _, problems = parse("conv1_in, contact:0.5m, fill:75", "J1")
    assert [t.role for t in tags] == ["in"] and not problems
    assert parse("contact:lots", "J1")[2]                       # a bad value is reported with the labels' problems
    assert pins.parse_words(pins.format_values(vals))[0] == vals    # what the tab writes reads back the same
    board, sc, _ = make(tmp_path, distribution_board(), "rw", nets=())
    sc.set_component_pins("J1", {"contact": 5e-4, "fill": 0.75})
    sc.set_component("J1", sc.config.components["J1"].model)          # a model edit keeps the pin values
    sc.set_pin_defaults({"pin": "brass"})
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    assert cfg.components["J1"].tht == {"contact": pytest.approx(5e-4), "fill": 0.75} and cfg.tht == {"pin": "brass"}
    sc.set_component_pins("J1", {})
    sc.set_pin_defaults({})
    assert not sc.config.components["J1"].tht and not sc.config.tht


def test_the_column_is_its_conductors_in_parallel_and_its_parts_in_series():
    """Built in: copper filling the hole (as before). A brass pin: rod + solder gap + plating, each σ·A; pin_r
    replaces the rod. A segment: soldered and open lengths in series."""
    drill, t = 1.0, 1.6e-3
    r = drill / 2 * 1e-3
    plating = SIGMA_CU * 2 * math.pi * r * VIA_PLATING
    g_s, g_o = pins.column_conductance(pins.PinParams(), drill, t)
    assert g_s == pytest.approx(SIGMA_CU * math.pi * r * r) and g_o == pytest.approx(plating)
    brass = pins.PinParams(pin="brass", pin_d=0.8)
    a_pin, a_gap = math.pi * 0.4e-3 ** 2, math.pi * r * r - math.pi * 0.4e-3 ** 2
    g_s, _ = pins.column_conductance(brass, drill, t)
    assert g_s == pytest.approx(a_pin / pins.MATERIALS["brass"] + a_gap / pins.SOLDER_RHO + plating)
    by_r = pins.PinParams(pin="brass", pin_d=0.8, pin_r=2e-4)
    assert pins.column_conductance(by_r, drill, t)[0] == pytest.approx(t / 2e-4 + a_gap / pins.SOLDER_RHO + plating)
    z = pins.segment_impedance(1.0e-3, 0.25e-3, drill, 40.0, 4.0)(0.0)
    assert z.real == pytest.approx(0.25e-3 / 40.0 + 0.75e-3 / 4.0)
    assert pins.feed_layer(pins.PinParams(fill=0.5), "F.Cu", ["F.Cu", "B.Cu"], {"F.Cu": 0.0, "B.Cu": -1.6e-3}) == "B.Cu"
    assert pins.feed_layer(pins.PinParams(), "F.Cu", ["F.Cu", "B.Cu"], {"F.Cu": 0.0, "B.Cu": -1.6e-3}) is None


def column_board(tmp_path, name):
    """A through-hole power pin J1.1 (pads on F.Cu and B.Cu) feeding a 3 mm PWR pour on F.Cu to a load pad U2.1; B.Cu
    has nothing else, so a pin soldered only from the bottom part of the way up must carry its current up its column
    to the pour. One rail: 5 A from J1.1 to U2.1."""
    from boardgen import BoardGen
    g = BoardGen(layers=2, size=(30.0, 10.0))
    tht = {"type": "thru_hole", "drill": 1.0, "size": (1.7, 1.7), "shape": "circle"}
    g.footprint("J1", "Conn_01x01", "Connector:Fake_1pin", (5.0, 5.0), [{"number": 1, "at": (0.0, 0.0), "net": "PWR",
                                                                         **tht}], attrs="through_hole")
    g.footprint("U2", "Load", "Package_SO:Fake_Pad", (24.0, 5.0), [{"number": 1, "at": (0.0, 0.0), "net": "PWR",
                                                                     "size": (1.5, 1.5)}])
    g.zone("PWR", "F.Cu", [(4.0, 3.5), (25.0, 3.5), (25.0, 6.5), (4.0, 6.5)])
    board, sc, _ = make(tmp_path, g, name, nets=())
    sc.set_dc_loads([DcLoadSpec("PWR rail", "J1.1", [DcLoad("U2.1", 5.0, "")], "")])
    sc.save()
    return board, SettingsFile.load(sc.path).config


def test_a_partly_soldered_pin_adds_its_column_to_the_drop(tmp_path):
    """J1 soldered from the bottom (B.Cu) to 10 % and to 50 % of the board: its current enters on B.Cu either way
    (the solder does not reach F.Cu, where the pour is) and climbs the column. Between the two only the column
    differs: the drop grows by I × 0.4·T × (1/G_plating - 1/G_copper-filled hole), exactly. Soldered all the way
    (built in) the current enters on F.Cu and the column carries nothing: less drop than either."""
    board, cfg = column_board(tmp_path, "fill")
    z = {l.name: l.z for l in board.stackup.copper}
    t = abs(z["F.Cu"] - z["B.Cu"]) * 1e-3                                  # outer copper to outer copper, m
    g_s, g_o = pins.column_conductance(pins.PinParams(), 1.0, t)
    rows = {}
    for fill in (0.1, 0.5, 1.0):
        res = simulate(board, with_part(cfg, "J1", {"fill": fill}))
        assert not res.dc_loads[0].error, res.dc_loads[0].error
        rows[fill] = load_of(res)["supply_mv"]
    extra = 5.0 * 0.4 * t * (1 / g_o - 1 / g_s) * 1e3                     # mV
    assert rows[0.1] - rows[0.5] == pytest.approx(extra, rel=0.02)
    assert rows[1.0] < rows[0.5] < rows[0.1]


def test_joint_and_contact_add_their_drop_and_heat(tmp_path):
    """J1 with contact 1 mΩ (GT field), J2 with joint 0.25 mΩ + contact 0.25 mΩ (its settings): each load's drop
    grows by I·R at every pin it passes (J1.2, J2.2 out; J2.1, J1.1 back), the copper's currents do not change, and
    Thermal heats J1 and J2 with I²·R per pin."""
    from groundtruth.analysis.thermal import part_losses
    board, cfg = rail(tmp_path, "series")
    plain = simulate(board, cfg)
    board.footprint("J1").fields["GT"] = "contact:1m"
    cfg2 = with_part(cfg, "J2", {"joint": 2.5e-4, "contact": 2.5e-4})
    res = simulate(board, cfg2)
    a, b = load_of(plain), load_of(res)
    per_side = 5.0 * (1e-3 + 5e-4) * 1e3                                   # mV, supply side and return side each
    assert b["supply_mv"] - a["supply_mv"] == pytest.approx(per_side, rel=1e-6)
    assert b["return_mv"] - a["return_mv"] == pytest.approx(per_side, rel=1e-6)
    assert b["pins_mv"] == pytest.approx(2 * per_side) and a["pins_mv"] == 0
    assert res.dc_loads[0].dc[0]["loss_w"] == pytest.approx(plain.dc_loads[0].dc[0]["loss_w"], rel=1e-9)
    losses = {r["ref"]: r for r in part_losses(board, cfg2, res.dc_loads)[0]}
    assert losses["J1"]["power_w"] == pytest.approx(2 * 5.0 ** 2 * 1e-3)      # two pins, 25 W·mΩ each
    assert losses["J2"]["power_w"] == pytest.approx(2 * 5.0 ** 2 * 5e-4)
    assert "contact 1 mΩ" in losses["J1"]["what"]


def test_a_connector_without_its_contact_gets_a_note(tmp_path):
    """5 A through J1 and J2 with no contact value: a note names each pin (the model guesses nothing); with a
    board-wide contact the notes go."""
    board, cfg = rail(tmp_path, "note")
    res = simulate(board, cfg).dc_loads[0]
    notes = [n for n in res.notes if "without a contact resistance" in n]
    assert {n.split()[0] for n in notes} == {"J1.2", "J1.1", "J2.2", "J2.1"}
    res = simulate(board, replace(cfg, tht={"contact": 5e-4})).dc_loads[0]
    assert not [n for n in res.notes if "without a contact resistance" in n]
    assert pins.series_note("U1", "U1.1", 50.0, pins.PinParams()) is None            # not a connector
    assert pins.series_note("J9", "J9.1", 1.0, pins.PinParams()) is None             # little current
