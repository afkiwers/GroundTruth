"""Net auto-detection: suggestions are typed by pin direction and land in the settings unreviewed."""
import pytest

from boardgen import BoardGen
from groundtruth.adapters.file_source import load_board
from groundtruth.analysis.detect import classify, detect_nets
from groundtruth.cli import main
from groundtruth.config.settings import SettingsFile


def spi_board(tmp_path):
    g = BoardGen(size=(40.0, 20.0))
    g.ic("U1", (5.0, 6.0), "/MCU/SPI_SCK", pintype="bidirectional", pinfunction="PA5")
    g.ic("U2", (35.0, 6.0), "/MCU/SPI_SCK", pintype="input", pinfunction="SCK")
    g.ic("U3", (5.0, 12.0), "/MCU/SPI_MISO", pintype="bidirectional", pinfunction="PA6")
    g.ic("U4", (35.0, 12.0), "Net-(R1-Pad2)", pintype="output", pinfunction="DO")
    g.two_pin("R1", "33R", (20.0, 12.0), "/MCU/SPI_MISO", "Net-(R1-Pad2)")   # series termination
    g.ic("U5", (5.0, 17.0), "/LED", pintype="output", pinfunction="PB0")
    g.ic("U6", (35.0, 17.0), "/LED", pintype="input", pinfunction="EN")
    g.track((5.0, 6.0), (35.0, 6.0), "/MCU/SPI_SCK")
    g.track((5.0, 12.0), (19.5, 12.0), "/MCU/SPI_MISO")
    g.track((20.5, 12.0), (35.0, 12.0), "Net-(R1-Pad2)")
    g.plane("GND", "B.Cu")
    return g.write(tmp_path / "spi.kicad_pcb")


@pytest.mark.parametrize("net,pins,expected", [
    ("/CPU/SPI_SCK", [], "SPI clock"), ("/X/RMII_REF_CLK", [], "Ethernet (R)MII"), ("USB_DP", [], "USB data"),
    ("Net-(U1-Pad3)", ["MOSI"], "SPI data"), ("/SW_CLK", [], "Debug clock"), ("/I2C_SCL", [], "I2C"),
])
def test_classify(net, pins, expected):
    assert classify(net, pins)[0].name == expected


def test_detect_driver_from_pin_types(tmp_path):
    board = load_board(spi_board(tmp_path))
    sc = SettingsFile.new(tmp_path / "groundtruth" / "spi.groundtruth.yaml", board)
    found = {d.spec.name: d for d in detect_nets(board, sc.config)}
    sck = found["/MCU/SPI_SCK"]
    assert (sck.spec.driver, sck.spec.receivers) == ("U1.1", ["U2.1"]) and sck.confidence == "high"
    assert sck.spec.freq == 20e6 and sck.spec.source == "auto" and not sck.spec.reviewed
    miso = found["/MCU/SPI_MISO"]        # through the 33R termination; the flash output drives
    assert (miso.spec.driver, miso.spec.receivers) == ("U4.1", ["U3.1"])
    assert "/LED" not in found           # not a fast net


def test_detect_write_then_review(tmp_path, capsys):
    path = spi_board(tmp_path)
    assert main(["detect", str(path), "--write"]) == 0
    sc = SettingsFile.load(tmp_path / "groundtruth" / "spi.groundtruth.yaml")
    nets = {n.name: n for n in sc.config.nets}
    assert set(nets) == {"/MCU/SPI_SCK", "/MCU/SPI_MISO"}
    assert all(n.source == "auto" and not n.reviewed for n in nets.values())
    text = (tmp_path / "groundtruth" / "spi.groundtruth.yaml").read_text(encoding="utf-8")
    assert "freq: 20MHz" in text and "rise_time: 2ns" in text and "reviewed: false" in text
    # a second run does not duplicate
    assert main(["detect", str(path), "--write"]) == 0
    assert len(SettingsFile.load(tmp_path / "groundtruth" / "spi.groundtruth.yaml").config.nets) == 2
    capsys.readouterr()
    main(["check", str(path)])
    assert "not reviewed yet" in capsys.readouterr().err


def test_suggest_a_net_picked_in_kicad(tmp_path):
    """A net the user picks (a track selected in KiCad): the same suggestion as Detect for a known class; a net
    no class knows still gets its driver and receivers from the pin types (generic timing, to set); reference,
    supply, switch-node and unknown nets are refused with the reason."""
    from boardgen import discrete_buck_board
    from conftest import make
    from groundtruth.analysis.detect import GENERIC, detect_converters, suggest_net
    board = load_board(spi_board(tmp_path))
    sc = SettingsFile.new(tmp_path / "groundtruth" / "spi.groundtruth.yaml", board)
    detected = {d.spec.name: d.spec for d in detect_nets(board, sc.config)}
    sck, why = suggest_net(board, sc.config, "/MCU/SPI_SCK")
    assert why == "" and sck.spec == detected["/MCU/SPI_SCK"]
    led, _ = suggest_net(board, sc.config, "/LED")
    assert led.signal_class is GENERIC and (led.spec.driver, led.spec.receivers) == ("U5.1", ["U6.1"])
    assert led.spec.source == "auto" and not led.spec.reviewed and "/LED" not in detected
    for net, words in (("GND", "reference net"), ("/NOPE", "not on the board")):
        found, why = suggest_net(board, sc.config, net)
        assert found is None and words in why, why
    buck, bsc, _ = make(tmp_path, discrete_buck_board(), "buck", nets=())
    bsc.add_detected_converters([d.spec for d in detect_converters(buck, bsc.config)])
    stage = bsc.config.converters[0].name
    for net, words in (("SW", "switch node of the power stage"), ("VIN", "supply net, not a signal"),
                       ("VIN", f"analysed with the power stage {stage} on the Converters tab")):
        found, why = suggest_net(buck, bsc.config, net)
        assert found is None and words in why, why
    bsc.config.converters.clear()                       # no stage known: where to look for one
    assert "Detect… finds the stages" in suggest_net(buck, bsc.config, "VIN")[1]


def _named_net_board(tmp_path, net):
    from boardgen import BoardGen
    from conftest import make
    g = BoardGen(layers=2, size=(40.0, 20.0))
    g.ic("U1", (5.0, 10.0), net)
    g.ic("U2", (35.0, 10.0), net)
    g.track((5.0, 10.0), (35.0, 10.0), net)
    g.plane("GND", g.cu[-1])
    board, sc, _ = make(tmp_path, g, "named", nets=())
    return board, sc.config


def test_a_net_that_measures_a_supply_is_a_signal(tmp_path):
    """/CPU/ADC_I_RAIL_1 (the user's board: the ADC input measuring rail 1's current) contains RAIL, yet it is a
    sensitive signal: a measuring word in the name (ADC, SENSE, MON, FB …) makes a supply-looking name a signal;
    a plain rail name stays a supply, unless the user says it is a signal (as_signal: "Add it anyway")."""
    from groundtruth.analysis.detect import suggest_net
    board, cfg = _named_net_board(tmp_path, "/CPU/ADC_I_RAIL_1")
    found, why = suggest_net(board, cfg, "/CPU/ADC_I_RAIL_1")
    assert found is not None, why
    (tmp_path / "x").mkdir()
    board, cfg = _named_net_board(tmp_path / "x", "RAIL_3V3_LOAD")
    found, why = suggest_net(board, cfg, "RAIL_3V3_LOAD")
    assert found is None and "supply net" in why
    found, _ = suggest_net(board, cfg, "RAIL_3V3_LOAD", as_signal=True)
    assert found is not None and found.spec.name == "RAIL_3V3_LOAD"


@pytest.mark.parametrize("fields, freq, rise", [
    ({"Gain": "1", "GBW": "1MHz", "Slew Rate": "0.5V/us"}, 48.2e3, 5.28e-6),   # slew-limited: 0.8·3.3 V / SR
    ({"Gain": "10", "GBW": "10MHz"}, 1e6, 0.35e-6),                            # bandwidth-limited: GBW / gain
    ({}, 10e6, 2e-9),                                                          # no op-amp data: the default
])
def test_an_op_amp_driven_net_gets_the_fastest_signal_its_op_amp_can_make(tmp_path, fields, freq, rise):
    """A net with no class from its names, driven by an op-amp's output (U5 in opamp_path_board): its suggested edge
    and frequency come from U5's slew rate or bandwidth [A37] (a TLV6001's 0.5 V/µs: 5.28 µs at 48.2 kHz), not
    10 MHz / 2 ns, with a reason saying so; the net before U5 (driven by U1) keeps the default, and so does every net
    when U5 has no such data."""
    from boardgen import opamp_path_board
    from conftest import make
    from groundtruth.analysis.detect import suggest_net
    from groundtruth.components.fields import apply_fields
    board, sc, _ = make(tmp_path, opamp_path_board(**fields), "amp", nets=())
    cfg = sc.config
    apply_fields(cfg, board)
    after, _ = suggest_net(board, cfg, "SIG_C")
    assert after.spec.driver == "U5.1"
    assert after.spec.freq == pytest.approx(freq, rel=0.01) and after.spec.rise_time == pytest.approx(rise, rel=0.01)
    assert ("U5's" in after.reasons[-1]) == bool(fields)
    before, _ = suggest_net(board, cfg, "SIG_A")
    assert (before.spec.freq, before.spec.rise_time) == (10e6, 2e-9)
