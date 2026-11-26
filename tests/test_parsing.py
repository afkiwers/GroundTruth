import math

import pytest

from boardgen import BoardGen, microstrip_board
from groundtruth.adapters.file_source import loads_board
from groundtruth.adapters.sexpr import SExprError, parse
from groundtruth.geometry.shapes import clean_polygon


def test_sexpr_basics_and_lines():
    root = parse('(a\n  (b 1 "two words")\n  (c "esc \\"q\\"" (d))\n)')
    assert root.tag == "a"
    b = root.find("b")
    assert b.atoms() == ["1", "two words"] and b.line == 2
    assert root.find("c")[1] == 'esc "q"'
    assert root.find("c").line == 3


def test_sexpr_unbalanced():
    with pytest.raises(SExprError):
        parse("(a (b)")


@pytest.mark.parametrize("layers", [2, 4, 6, 8])
def test_any_layer_count_and_stackup(layers):
    g = BoardGen(layers=layers)
    g.plane("GND", g.cu[1])
    b = loads_board(g.text())
    assert b.copper_layers[0] == "F.Cu" and b.copper_layers[-1] == "B.Cu"
    assert len(b.copper_layers) == layers
    zs = [l.z for l in b.stackup.copper]
    assert zs == sorted(zs)
    # z distance = dielectric + copper between the two foil centres
    assert b.stackup.distance("F.Cu", g.cu[1]) == pytest.approx(g.dielectrics[0] + 0.035)
    assert b.stackup.neighbours(g.cu[1]) == [g.cu[0], g.cu[2]] if layers > 2 else ["F.Cu"]


def test_pad_rotation_matches_kicad():
    g = BoardGen()
    g.two_pin("R1", "33R", (10.0, 10.0), "A", "B", angle=90.0)
    b = loads_board(g.text())
    p1, p2 = b.footprint("R1").pads
    # KiCad: 90 deg is counter-clockwise on screen (y down): local -x goes to +y
    assert p1.position == pytest.approx((10.0, 10.5))
    assert p2.position == pytest.approx((10.0, 9.5))


def test_fractured_zone_fill_becomes_polygon_with_hole():
    g = microstrip_board(slot=(19.5, 4, 20.5, 16))
    b = loads_board(g.text())
    zone = next(z for z in b.zones if z.net == "GND")
    poly = clean_polygon(zone.filled["B.Cu"][0])
    assert len(poly.interiors) == 1
    assert poly.area == pytest.approx(39.4 * 19.4 - 12.0, rel=1e-6)


def test_old_net_format_and_outline():
    text = microstrip_board().text().replace('(net "SIG")', '(net 1 "SIG")', 1)
    b = loads_board(text)
    assert "SIG" in b.nets
    assert len(b.outline) == 1
    assert math.isclose(len(b.outline[0][0]), 4)


def test_an_outline_whose_ends_differ_by_a_fraction_of_a_nanometre_still_closes():
    """A board edge whose pieces meet in KiCad but whose end points differ here in the last bits (an arc's end
    computed from start/mid, a turned footprint), with a closed slot inside: the board is the whole outer contour
    with the slot as a hole. Without snapping to 1 nm the outer ring never closed and the board was read as the
    slot alone (a real 735 mm² board as three 7.7 mm² slots; every model clipped to them)."""
    from groundtruth.geometry.shapes import polygonize_outline, to_shapely
    e = 3e-10                                           # mm: well under KiCad's 1 nm
    outer = [[(0, 0), (40, 0)], [(40, 0 + e), (40, 20)], [(40, 20), (0, 20 - e)], [(0, 20), (0, 0)]]
    slot = [[(10, 8), (14, 8)], [(14, 8), (14, 12)], [(14, 12), (10, 12)], [(10, 12), (10, 8)]]
    polys = polygonize_outline(outer + slot)
    assert len(polys) == 1
    board = to_shapely(polys[0])
    assert board.area == pytest.approx(40 * 20 - 4 * 4, abs=1e-6) and len(polys[0][1]) == 1


def test_a_broken_or_binary_board_file_is_named_in_the_error(tmp_path):
    """A file that is not S-expression text, or not text at all, raises BoardLoadError naming the file (the CLI
    printed the bare parser message before: "atom outside of list at offset 0")."""
    from groundtruth.adapters.file_source import BoardLoadError, load_board
    bad, binary = tmp_path / "bad.kicad_pcb", tmp_path / "binary.kicad_pcb"
    bad.write_text("garbage (", encoding="utf-8")
    binary.write_bytes(b"\xff\xfe\x00")
    for p in (bad, binary):
        with pytest.raises(BoardLoadError, match=p.name):
            load_board(p)
