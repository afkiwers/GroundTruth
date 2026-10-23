"""Stacked boards (analysis/stack.py) and what a motor bridge's switching does to the parts and nets around it
(analysis/victims.py): the placement from the connector pins, the merged stackup, the capacitance of a track facing
a phase node, and the noise at a victim net's receiver on a logic board stacked over a bridge."""
import math
from dataclasses import replace

import numpy as np
import pytest

from boardgen import BoardGen, bridge_board, logic_board
from conftest import make
from groundtruth.adapters.file_source import load_board, loads_board
from groundtruth.analysis.stack import StackError, fit_placement, stack_board
from groundtruth.analysis.victims import EPS0, facing_capacitance
from groundtruth.components.models import ComponentModel
from groundtruth.config.settings import (ComponentEntry, Config, ConfigError, MotorSpec, SettingsFile, StackBoard,
                                         config_from_data)
from groundtruth.geometry.copper import CopperMap


def test_the_placement_is_recovered_from_the_pins():
    """Matched pins of a board turned by 30°, moved and mirrored: the fit gives that transform back, and maps every
    pin onto its mate. One row of pins cannot tell a mirror: it is taken as not mirrored."""
    rng = np.random.default_rng(1)
    theirs = rng.uniform(0, 20, (6, 2))
    for mirrored in (False, True):
        a = math.radians(30.0)
        R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        q = theirs * ([-1.0, 1.0] if mirrored else [1.0, 1.0])
        ours = q @ R.T + [12.0, -4.0]
        p = fit_placement([tuple(x) for x in ours], [tuple(x) for x in theirs])
        assert p.mirrored == mirrored and p.angle == pytest.approx(30.0) and p.residual_mm < 1e-9
        assert np.allclose([p.xy(x) for x in theirs], ours)
    row = [(0.0, 2.54 * k) for k in range(4)]
    assert not fit_placement([(10.0, 6.0 + y) for _, y in row], row).mirrored
    with pytest.raises(StackError):
        fit_placement([(0.0, 0.0)], [(1.0, 1.0)])


@pytest.fixture
def stacked(tmp_path):
    bridge_board(header_gnd_pins=2).write(tmp_path / "power.kicad_pcb")
    logic_board(2).write(tmp_path / "logic.kicad_pcb")
    board = load_board(str(tmp_path / "power.kicad_pcb"))
    cfg = Config(stack=[StackBoard("logic", "logic.kicad_pcb", 5.0, [["J7", "J1"]])], base_dir=str(tmp_path))
    return board, cfg


def test_the_other_board_sits_above_with_its_nets_joined_by_the_pins(stacked):
    """The logic board lands where its header meets J7 ((x, y) + (10, 6)); its layers sit above ours, its bottom
    copper the gap above our top surface, air between; GND and +3V3 are joined (one net each) by a vertical pin per
    joined pin, from its bottom layer to our top layer; its other parts and nets carry its name."""
    board, cfg = stacked
    m, notes = stack_board(board, cfg)
    assert "logic placed above at a 5 mm gap, turned 0.0°" in notes[0] and "within 0.00 mm" in notes[0]
    logic = loads_board(logic_board(2).text())
    names = [l.name for l in m.stackup.copper]
    assert names == ["logic:F.Cu", "logic:In1.Cu", "logic:In2.Cu", "logic:B.Cu", "F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    t = logic.stackup.board_thickness
    for l in logic.stackup.copper:          # not mirrored: its bottom faces us
        assert m.stackup.layer(f"logic:{l.name}").z == pytest.approx(-5.0 - t + l.z)
    gap = m.stackup.dielectrics[3]
    assert gap.name == "air gap to logic" and gap.epsilon_r == 1.0
    assert m.stackup.dielectric_between("logic:B.Cu", "F.Cu") == pytest.approx(5.0, abs=0.05)
    assert m.footprint("logic:U1").position == pytest.approx((25.0, 21.0))
    assert m.footprint("logic:R1").pads[0].net == "logic:NRST" and "logic:NRST" in m.nets
    pins = [v for v in m.vias if v.uuid.startswith("stack pin ")]
    assert sorted(v.net for v in pins) == ["+3V3", "GND", "GND"]
    assert all(v.layers == ["logic:B.Cu", "F.Cu"] for v in pins)
    assert {p.net for p in m.footprint("logic:J1").pads} == {"GND", "+3V3"}
    with pytest.raises(StackError):
        stack_board(board, replace(cfg, stack=[replace(cfg.stack[0], connectors=[["J7", "U1"]])]))


def test_stack_and_victims_settings():
    cfg = config_from_data({"stack": [{"name": "logic", "file": "logic.kicad_pcb", "gap": "3.2mm",
                                       "connectors": [["J7", "J1"]], "side": "above"}],
                            "victims": {"nets": ["logic:/CPU/NRST"], "drive_ohms": "40k", "input_pf": "10pF"}})
    assert cfg.stack[0].gap == pytest.approx(3.2) and cfg.stack[0].flip == "auto"
    assert cfg.victims.nets == ["logic:/CPU/NRST"] and cfg.victims.drive_ohms == pytest.approx(40e3)
    assert cfg.victims.input_pf == pytest.approx(10.0)
    with pytest.raises(ConfigError) as e:
        config_from_data({"stack": [{"name": "x y", "gap": -1, "connectors": [["J7"]], "side": "left"}]})
    msg = " ".join(e.value.messages)
    for words in ("file is missing", "gap must be positive", "connectors:", "side must be", "name:"):
        assert words in msg


def test_a_track_facing_a_pour_has_the_plate_capacitance_unless_a_plane_is_between():
    """A 0.5 mm track 8 mm along over a pour on the next layer: C = ε0·εr·w·l/h. A plane on a layer between hides
    it."""
    for layers, shield in ((2, False), (4, True)):
        g = BoardGen(layers=layers, size=(20.0, 20.0))
        g.zone("SW", "B.Cu", [(4.0, 4.0), (16.0, 4.0), (16.0, 16.0), (4.0, 16.0)])
        if shield:
            g.plane("GND", "In1.Cu")
        g.track((6.0, 10.0), (14.0, 10.0), "VICTIM", layer="F.Cu", width=0.5)
        b = loads_board(g.text())
        c = facing_capacitance(b, CopperMap(b), "VICTIM", "SW")
        if shield:
            assert c == 0.0
        else:
            stk = b.stackup
            want = EPS0 * stk.epsilon_between("F.Cu", "B.Cu") * 0.5e-3 * 8e-3 / (stk.dielectric_between("F.Cu", "B.Cu")
                                                                                   * 1e-3)
            assert c == pytest.approx(want, rel=1e-6)


# --------------------------------------------------------------------------- the bridge under a logic board
def _stack_settings(tmp_path, gap=5.0, **victims):
    board, sc, _ = make(tmp_path, bridge_board(header_gnd_pins=2), "power", nets=(), max_nodes=20000)
    logic_board(2).write(tmp_path / "logic.kicad_pcb")
    from groundtruth.analysis.detect import detect_bridges
    (d,) = detect_bridges(board, sc.config)
    b = d.spec
    b.v_bus, b.f_pwm, b.i_phase, b.speed_rpm, b.c_oss = 48.0, 40e3, 30.0, 3000.0, 1e-9
    b.motor = MotorSpec("star", 0.05, 50e-6, 5.0, 7)
    sc.add_detected_bridges([b], d.models)
    sc.doc["stack"] = [{"name": "logic", "file": "logic.kicad_pcb", "gap": gap, "connectors": [["J7", "J1"]]}]
    if victims:
        sc.doc["victims"] = victims
    sc.save()
    return board, SettingsFile.load(sc.path).config


@pytest.fixture(scope="module")
def victims_run(tmp_path_factory):
    from groundtruth.analysis.simulate import simulate
    out = {}
    for key, gap, victims in (("5", 5.0, {}), ("50ohm", 5.0, {"parts": ["logic:U1"], "nets": ["logic:NRST"],
                                                               "drive_ohms": 50.0}), ("8", 8.0, {})):
        board, cfg = _stack_settings(tmp_path_factory.mktemp(f"stack{key}"), gap, **victims)
        if key == "50ohm":                   # NRST driven by a 50 ohm output instead of its 10k pull-up
            cfg.components["logic:R1"] = ComponentEntry("logic:R1", "10k", "", ComponentModel("open"))
        r = simulate(board, cfg)
        out[key] = (r, {(v["kind"], v["name"]): v for v in r.bridges[0].victims})
    return out


def test_victims_are_found_and_reported_per_receiver(victims_run):
    """On the stacked logic board: the STM32 (a microcontroller), its reset and its Hall line are found; each net's
    noise is read at the STM32's pin, driven from the pull-up / the sensor connector; the reset track on the bottom
    layer faces leg A's phase pour across the gap and takes a current at every edge, the Hall track behind the
    logic board's GND plane takes none; U1's ground against the bridge's input minus is reported, but not raised as
    an issue (no reference pin was chosen)."""
    r, rows = victims_run["5"]
    (br,) = r.bridges
    assert not br.error and br.notes[0].startswith("stack: logic placed above")
    nrst, hall = rows[("net", "logic:NRST")], rows[("net", "logic:HALL_U")]
    assert (nrst["from"], nrst["to"], nrst["why"]) == ("logic:R1.1", "logic:U1.7", "reset")
    assert (hall["from"], hall["to"], hall["why"]) == ("logic:J2.1", "logic:U1.10", "Hall sensor")
    inj = rows[("injected", "logic:NRST")]
    assert inj["leg"] == "bridge J1 A" and inj["c_pf"] > 0 and ("injected", "logic:HALL_U") not in rows
    assert inj["peak"] == pytest.approx(inj["c_pf"] * 1e-12 * 48.0 / 2e-9)
    part = rows[("part", "logic:U1")]
    assert part["why"] == "microcontroller" and part["reference"] == "J2.2" and part["l_nh"] > 0
    assert not any(i.check == "victim_coupling" and "logic:U1 (" in i.message for i in r.issues)
    from groundtruth.report.formats import bridge_text
    text = "\n".join(bridge_text(br))
    assert "victims (at the switching edges, worst leg):" in text and "noise at logic:U1.7 on logic:NRST" in text


def test_the_receiver_sees_the_induced_voltage_divided_by_its_terminations(victims_run):
    """For one induced voltage around the reset line's loop, the receiver's share is |Z_in| / |Z_drive + Z_in + jωL|:
    driven by its 10 kΩ pull-up the STM32's pin sees about 1/32 of what it sees when a 50 Ω output drives the line
    (Z_in: 5 pF at 100 MHz; the loop's own jωL is small against both)."""
    _, rows = victims_run["5"]
    _, rows50 = victims_run["50ohm"]
    z_in = 1 / (2j * math.pi * 100e6 * 5e-12)
    ratio = abs(10e3 + z_in) / abs(50 + z_in)
    got = rows50[("net", "logic:NRST")]["peak"] / rows[("net", "logic:NRST")]["peak"]
    assert got == pytest.approx(ratio, rel=0.1)


def test_a_wider_gap_couples_less(victims_run):
    """Moving the logic board from 5 to 8 mm above the bridge: less noise on both nets and less injected current
    (the plate capacitance goes as 1/h: 5/8 of it)."""
    _, near = victims_run["5"]
    _, far = victims_run["8"]
    for key in (("net", "logic:NRST"), ("net", "logic:HALL_U")):
        assert far[key]["peak"] < near[key]["peak"]
    c5, c8 = near[("injected", "logic:NRST")]["c_pf"], far[("injected", "logic:NRST")]["c_pf"]
    assert c8 / c5 == pytest.approx(5.0 / 8.0, rel=0.02)


def test_the_pickers_automatic_victims_are_what_a_bridges_run_finds_and_the_list_is_written(tmp_path):
    """The Bridges tab's victims picker: board_auto_nets lists, for a board with one bridge, exactly the victim nets
    that bridge's Simulate finds when the settings list none (find_victims); set_victim_nets writes victims.nets
    (the other victims settings kept) and an empty list removes it (automatic again)."""
    from groundtruth.analysis.detect import detect_bridges
    from groundtruth.analysis.victims import board_auto_nets, find_victims
    g = bridge_board(header_gnd_pins=2)
    g.track((12.0, 31.0), (40.0, 31.0), "/MCU/NRST", "B.Cu")         # a reset and a crystal net, routed
    g.track((12.0, 32.5), (40.0, 32.5), "OSC_IN", "B.Cu")
    board, sc, _ = make(tmp_path, g, "power", nets=(), max_nodes=20000)
    (d,) = detect_bridges(board, sc.config)
    sc.add_detected_bridges([d.spec], d.models)
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    (b,) = cfg.bridges
    auto = board_auto_nets(board, cfg)
    assert auto == {"/MCU/NRST": "reset", "OSC_IN": "crystal"}
    assert auto == find_victims(board, cfg, b.stages(), b.rail, b.ground, b.input_connector).nets
    sc = SettingsFile.load(tmp_path / "groundtruth" / "power.groundtruth.yaml")
    sc.doc["victims"] = {"drive_ohms": 33.0}
    sc.set_victim_nets(["NRST", "OSC_IN"])
    assert sc.config.victims.nets == ["NRST", "OSC_IN"] and sc.config.victims.drive_ohms == 33.0
    sc.set_victim_nets([])
    assert sc.config.victims.nets == [] and "nets" not in sc.doc["victims"] and sc.config.victims.drive_ohms == 33.0
    del sc.doc["victims"]["drive_ohms"]
    sc.set_victim_nets([])
    assert "victims" not in sc.doc
