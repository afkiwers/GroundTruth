"""Detection labels: a GT field on a symbol (conv_hs, hbA_ls, hb_out, ignore ...) says what a part is in a power
stage. Read from the board file (and the KiCad API), grouped per stage and per copy of a reused sheet, and used by
detection where its guesses fail."""
from types import SimpleNamespace as NS


from boardgen import BoardGen, bridge_board, discrete_buck_board
from conftest import make
from groundtruth.analysis.detect import detect_bridges, detect_converters
from groundtruth.analysis.tags import HELP, board_tags, parse


def _rename(g: BoardGen, old: str, new: str):
    """A part under another reference (a power stage with a U reference, like a DrMOS or a half-bridge module)."""
    for k, it in enumerate(g.items):
        g.items[k] = it.replace(f'(property "Reference" "{old}"', f'(property "Reference" "{new}"')


def test_labels_are_parsed_in_both_orders_and_mistakes_are_explained():
    tags, ignore, problems = parse("conv1_hs, conv_out_2 ; hbA_LS,hb_hs_B", "Q1")
    assert [(t.kind, t.ident, t.role) for t in tags] == [("conv", "1", "hs"), ("conv", "2", "out"),
                                                         ("hb", "a", "ls"), ("hb", "b", "hs")]
    assert not ignore and not problems
    assert parse("Ignore", "Q2")[1] is True
    _, _, problems = parse("conv_hss, hb_L, conv1_out_2, motor", "Q3")
    assert problems == [
        "Q3: GT 'conv_hss': conv has the roles hs, ls, sw, L, in, out",
        "Q3: GT 'hb_L': hb has the roles hs, ls, in, out",
        "Q3: GT 'conv1_out_2': the id goes before or after the role, not both",
        "Q3: GT 'motor' is not a label (e.g. conv_hs, conv1_L, hbA_ls, hb_out, sig:9, ignore)"]
    assert "conv_hs" in HELP["converters"] and "hb_out" in HELP["bridges"] and "SCK" in HELP["nets"]


def test_a_reused_sheet_makes_one_leg_per_copy_and_ids_join_parts_across_sheets(tmp_path):
    """The field is the same in every copy of a sheet used once per phase (KiCad keeps only the reference per
    copy), so the board's sheet copy of each part tells the legs apart, named and phased after the sheets. An id
    in sheets that are not copies of one another joins its parts into one stage."""
    g = bridge_board()
    for k, sheet in enumerate(("Phase A", "Phase B", "Phase C")):
        g.label(f"Q{2 * k + 1}", "hb_hs", sheet)
        g.label(f"Q{2 * k + 2}", "hb_ls", sheet)
    g.label("J1", "hb_out")
    g.label("C11", "conv7_L", "Rails", "rails.kicad_sch")        # nonsense, but shows how ids join across sheets
    g.label("U9", "conv7_sw", "Sensors", "sensors.kicad_sch")
    board, _, _ = make(tmp_path, g, "lab", nets=())
    q1 = board.footprint("Q1")
    assert q1.fields["GT"] == "hb_hs" and q1.sheet_name == "Phase A" and q1.sheet_file == "phase.kicad_sch"
    assert q1.sheet and q1.sheet != board.footprint("Q3").sheet
    tags = board_tags(board)
    legs = tags.of("hb")
    assert [(s.name, s.phase, s.refs("hs"), s.refs("ls")) for s in legs] == [
        ("Phase A", "A", ["Q1"], ["Q2"]), ("Phase B", "B", ["Q3"], ["Q4"]), ("Phase C", "C", ["Q5"], ["Q6"])]
    (conv,) = tags.of("conv")
    assert conv.name == "conv7" and conv.roles == {"l": ["C11"], "sw": ["U9"]}
    assert tags.connector_refs("hb", "out", "a") == ["J1"]       # no id: every leg's
    assert "bridge legs Phase A, Phase B, Phase C" in tags.summary()


def test_labels_find_a_leg_detection_cannot_and_ignore_drops_one(tmp_path):
    """Leg A's switches are U parts (a half-bridge module): detection only takes Q / T / M parts as transistors,
    so without labels the bridge has two legs; labelled hbA_hs / hbA_ls it has three, its phase from the label.
    'ignore' takes a leg's parts out of detection."""
    g = bridge_board()
    _rename(g, "Q1", "U31")
    _rename(g, "Q2", "U32")
    board, sc, _ = make(tmp_path, g, "mod", nets=())
    assert [leg.phase for leg in detect_bridges(board, sc.config)[0].spec.legs] == ["B", "C"]
    g.label("U31", "hbA_hs")
    g.label("U32", "hbA_ls")
    board, sc, _ = make(tmp_path, g, "mod2", nets=())
    (d,) = detect_bridges(board, sc.config)
    assert d.complete and d.confidence == "high"
    assert [(leg.phase, leg.high_side, leg.low_side, leg.output) for leg in d.spec.legs][0] == ("A", ["U31"], ["U32"],
                                                                                              "J1.A")
    assert any("labelled (GT) U31 hs, U32 ls" in r for r in d.reasons)
    g.label("Q5", "ignore")
    g.label("Q6", "ignore")
    board, sc, _ = make(tmp_path, g, "mod3", nets=())
    assert [leg.phase for leg in detect_bridges(board, sc.config)[0].spec.legs] == ["A", "B"]
    assert "ignored: Q5, Q6" in board_tags(board).summary()


def test_labels_find_a_converter_with_module_switches_and_its_labelled_input(tmp_path):
    """A buck whose switches are U parts without SW pin names is not found unlabelled; labelled conv_hs /
    conv_ls it is, with its rail and ground, named after the label. Of two connectors on its input rail the
    nearest is taken, unless the other is labelled conv_in."""
    g = discrete_buck_board()
    _rename(g, "Q1", "U7")
    _rename(g, "Q2", "U8")
    g.footprint("J3", "Conn", "Connector:Fake_2pin", (38.0, 27.0), [
        {"number": 1, "at": (0.0, -1.0), "net": "VIN", "size": (1.5, 1.5)},
        {"number": 2, "at": (0.0, 1.0), "net": "GND", "size": (1.5, 1.5)}])
    board, sc, _ = make(tmp_path, g, "buck", nets=())
    assert detect_converters(board, sc.config) == []
    g.label("U7", "conv_hs")
    g.label("U8", "conv_ls")
    board, sc, _ = make(tmp_path, g, "buck2", nets=())
    (d,) = detect_converters(board, sc.config)
    s = d.spec
    assert (s.high_side, s.low_side, s.rail, s.ground, s.switch_node) == (["U7"], ["U8"], "VIN", "GND", "SW")
    assert s.name == "buck SW" and s.input_connector == ["J1.1", "J1.2"] and d.confidence == "high"
    assert d.reasons[0] == "labelled (GT): U7 hs, U8 ls"
    g.label("J3", "conv_in")
    board, sc, _ = make(tmp_path, g, "buck3", nets=())
    assert detect_converters(board, sc.config)[0].spec.input_connector == ["J3.1", "J3.2"]


def test_a_half_labelled_converter_is_explained_and_still_found(tmp_path):
    """Only the high side labelled: the problem says what to add, and the stage is still found unlabelled."""
    g = discrete_buck_board()
    g.label("Q1", "conv1_hs")
    board, sc, _ = make(tmp_path, g, "half", nets=())
    (d,) = detect_converters(board, sc.config)
    assert (d.spec.high_side, d.spec.low_side) == (["Q1"], ["Q2"])
    assert ("converter conv1: label conv1_ls (or conv1_sw for a converter IC with its switches inside) too "
            "(Q1 hs labelled)") in board_tags(board).problems


def test_the_kicad_api_footprint_gives_fields_and_its_sheet_copy():
    from kipy.board_types import Field
    from groundtruth.adapters.kipy_source import footprint_fields
    gt = Field()
    gt.name = "GT"
    t = gt.text
    t.value = "hb_hs"
    mpn = Field()
    mpn.name = "MPN"
    mpn.text.value = "EPC2302"
    fpi = NS(definition=NS(items=[gt, mpn, object()]),
             sheet_path=NS(path=[NS(value="root"), NS(value="s1"), NS(value="sym")]),
             proto=NS(symbol_sheet_name="/Generator/Phase B/", symbol_sheet_filename="phase.kicad_sch"))
    assert footprint_fields(fpi) == ({"GT": "hb_hs", "MPN": "EPC2302"}, "/root/s1", "Phase B", "phase.kicad_sch")


def test_cli_detect_lists_the_labels_and_their_problems(tmp_path, capsys):
    from groundtruth.cli import main
    g = discrete_buck_board()
    g.label("Q1", "conv_hs")
    g.label("Q2", "conv_ls, conv_hss")
    _, _, path = make(tmp_path, g, "cli", nets=())
    assert main(["detect", str(path), "--settings", str(tmp_path / "cli.groundtruth.yaml")]) == 0
    out = capsys.readouterr().out
    assert "GT labels on this board: converters root sheet" in out
    assert "Q2: GT 'conv_hss': conv has the roles" in out
    assert "converter buck SW:" in out


def test_a_labelled_converter_ic_with_a_pin_on_both_ends_of_its_inductor(tmp_path):
    """Like the LT3970 (U503 on refboard): the IC's SW pin on one end of the inductor and its OUT pin on
    the other. The switch node is the end on the SW pin."""
    g = BoardGen(layers=2, size=(30.0, 20.0))
    g.plane("GND", "B.Cu")
    g.footprint("U5", "LT3970", "Package_SO:Fake_MSOP", (10.0, 10.0), [
        {"number": 1, "at": (-1.0, -1.0), "net": "VIN", "pinfunction": "VIN", "pintype": "power_in"},
        {"number": 2, "at": (1.0, -1.0), "net": "SW", "pinfunction": "SW", "pintype": "power_out"},
        {"number": 3, "at": (1.0, 1.0), "net": "VOUT", "pinfunction": "OUT", "pintype": "input"},
        {"number": 4, "at": (-1.0, 1.0), "net": "GND", "pinfunction": "GND", "pintype": "power_in"}])
    g.footprint("L1", "33u", "Inductor_SMD:L_1210_3225Metric", (16.0, 10.0), [
        {"number": 1, "at": (-1.4, 0.0), "net": "SW"}, {"number": 2, "at": (1.4, 0.0), "net": "VOUT"}])
    g.two_pin("C1", "10u", (6.0, 10.0), "VIN", "GND", kind="C")
    g.label("U5", "conv4_sw")
    g.label("L1", "conv4_L")
    board, sc, _ = make(tmp_path, g, "ic", nets=())
    (d,) = detect_converters(board, sc.config)
    assert (d.spec.name, d.spec.switch, d.spec.rail, d.spec.ground) == ("buck conv4", ["U5"], "VIN", "GND")
    assert d.reasons[0] == "labelled (GT): U5 sw, L1 L" and not board_tags(board).problems


def test_signal_labels_mark_a_supply_named_net_with_its_driver_and_receivers(tmp_path):
    """GT = sig_drv:1 on U2 and sig_rx:1 on U1 mark RAIL_X (a supply name) as a signal driven by U2.1: the user's
    ADC input measuring a rail, whose driver detection guessed the wrong way round. Detect lists it, Add KiCad
    selection takes it, with the labelled driver and receiver; a pin the part doesn't have is a problem."""
    from groundtruth.analysis.detect import detect_nets, is_power_net, suggest_net
    from groundtruth.analysis.tags import signal_marks
    tags, _, problems = parse("sig, sig:9, sig_drv:PA3, SIG_RX:5", "U7")
    assert [(t.kind, t.ident, t.role) for t in tags] == [("sig", "", ""), ("sig", "9", ""), ("sig", "PA3", "drv"),
                                                         ("sig", "5", "rx")] and not problems
    assert parse("sig_drv", "U7")[2] == ["U7: GT 'sig_drv': name the pin, e.g. sig_drv:5"]
    g = BoardGen(layers=2, size=(40.0, 20.0))
    g.ic("U1", (5.0, 10.0), "RAIL_X")
    g.ic("U2", (35.0, 10.0), "RAIL_X")
    g.track((5.0, 10.0), (35.0, 10.0), "RAIL_X")
    g.plane("GND", g.cu[-1])
    g.label("U2", "sig_drv:1")
    g.label("U1", "sig_rx:1, sig_rx:7")
    board, sc, _ = make(tmp_path, g, "sig", nets=())
    cfg = sc.config
    marks, problems = signal_marks(board, cfg.reference_nets)
    assert marks == {"RAIL_X": {"drivers": ["U2.1"], "receivers": ["U1.1"]}}
    assert problems == ["U1: GT 'sig_rx:7': no pin '7' on U1 (pad number or pin name)"]
    assert not is_power_net(board, "RAIL_X", cfg)
    found, why = suggest_net(board, cfg, "RAIL_X")
    assert found is not None, why
    assert (found.spec.driver, found.spec.receivers) == ("U2.1", ["U1.1"])
    assert "labelled a signal (GT sig)" in found.reasons
    assert any(d.spec.name == "RAIL_X" for d in detect_nets(board, cfg))
    line = board_tags(board).summary()
    assert "signal labels on U1, U2" in line
    assert "1 problem(s): U1: GT 'sig_rx:7': no pin '7' on U1" in line     # shown on the tabs, not only skipped


def test_op_amp_labels_with_a_missing_pin_or_half_a_pair_are_problems(tmp_path):
    """An op-amp label naming a pin the part doesn't have, or amp_in without amp_out, is a problem on the tabs'
    label line: the path would otherwise just be missing from Detect without a word."""
    g = BoardGen(layers=2, size=(40.0, 20.0))
    g.ic("U1", (5.0, 10.0), "A")
    g.ic("U2", (35.0, 10.0), "A")
    g.track((5.0, 10.0), (35.0, 10.0), "A")
    g.label("U1", "amp_in:1, amp_out:9")
    g.label("U2", "amp_in:1")
    board, _, _ = make(tmp_path, g, "amp", nets=())
    assert board_tags(board).problems == [
        "U1: GT 'amp_out:9': no pin '9' on U1 (pad number or pin name)",
        "U2: GT amp_in needs amp_out too: the op-amp stage's input and output pin"]


def test_labelled_nets_joined_by_a_large_resistor_are_one_path(tmp_path):
    """refboard's ADC input: an op-amp buffer's output (GT sig_drv:1 on U2) through a 4.7 kΩ resistor (no
    signal part by default) to the ADC pin (GT sig_rx:1 on U1) on ADC_I_RAIL_1. One suggestion, named after the
    named net, driver U2.1, receiver U1.1, R1 in `through`; the route runs through it."""
    from groundtruth.analysis.detect import detect_nets, suggest_net
    from groundtruth.rules.checks import run_checks
    g = BoardGen(layers=2, size=(40.0, 20.0))
    g.ic("U1", (5.0, 10.0), "ADC_I_RAIL_1")
    g.ic("U2", (35.0, 10.0), "Net-(U2-OUT)")
    g.two_pin("R1", "4k7", (20.0, 10.0), "ADC_I_RAIL_1", "Net-(U2-OUT)", kind="R")
    g.track((5.0, 10.0), (19.5, 10.0), "ADC_I_RAIL_1")
    g.track((20.5, 10.0), (35.0, 10.0), "Net-(U2-OUT)")
    g.plane("GND", g.cu[-1])
    g.label("U2", "sig_drv:1")
    g.label("U1", "sig_rx:1")
    board, sc, _ = make(tmp_path, g, "adc", nets=())
    cfg = sc.config
    found, why = suggest_net(board, cfg, "Net-(U2-OUT)")
    assert found is not None, why
    s = found.spec
    assert (s.name, s.driver, s.receivers, s.through) == ("ADC_I_RAIL_1", "U2.1", ["U1.1"], ["R1"])
    assert [d.spec.name for d in detect_nets(board, cfg) if d.spec.name in ("ADC_I_RAIL_1", "Net-(U2-OUT)")] == \
        ["ADC_I_RAIL_1"]
    cfg.nets = [s]
    res = run_checks(board, cfg)
    assert ("ADC_I_RAIL_1", "U1.1") in res.routes and res.routes[("ADC_I_RAIL_1", "U1.1")].parts == ["R1"]
