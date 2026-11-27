"""Through-hole pins, slots and wide holes in the DC models, against their edge cases (audit of [A121] [A122]).

Builds small boards with tests/boardgen.py and solves DC rails on them; prints one line per case and FAIL where a
case does not hold. Cases:

  converge   a rail between two connector pins in 3 mm holes, both layers poured: R at resolutions 0.25-4 (must
             settle), each layer's share 1/2, no via carrying 1 %
  bottom     the same with the connectors on B.Cu, and a slot turned 45°: the same R as on F.Cu / unturned
  tiny       a USB-C shield-sized slot (0.6 x 1.7 mm in a 1.0 x 2.1 mm pad) fed at resolutions 0.25-1: solves
  thin       a 1.5 mm hole in a 2.0 mm pad (0.25 mm ring) fed at resolution 0.25: solves
  dnp        a DNP connector's pin in a 3 mm hole fed: solves (no lid: fed at its barrel's joints)
  fill       a connector pin soldered half way up (GT pin field fill:50): its lid only on the soldered layer, a larger
             drop than at fill 100
  shunt      a through-hole shunt with slotted pins: its 100 µΩ in the path, the pads' faces carry current
  cutall     whole test boards (a buck with through-hole connectors, the distribution board) with every plated
             hole cut (HOLE_CUT_MM lowered): Simulate without errors, results near the uncut ones

Usage: .venv/Scripts/python tools/dev/hole_audit.py [--only converge,tiny,...]
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "tests"))

from boardgen import BoardGen, discrete_buck_board, distribution_board  # noqa: E402
from conftest import make  # noqa: E402
from groundtruth.analysis.simulate import simulate  # noqa: E402
from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile  # noqa: E402

FAILS: list[str] = []


def check(ok: bool, what: str) -> None:
    """Print a case's line; remember it when it fails."""
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        FAILS.append(what)


def two_pins(tmp: Path, name: str, layer: str = "F.Cu", drill="3.0", size=(5.0, 5.0), shape="circle",
             angle: float = 0.0, dnp: bool = False, field: str = "", load_a: float = 10.0, ret: bool = False):
    """A P pour on both layers from x 4 to 56, connector pins J1 (x 9) and J2 (x 51) in it, three stitching vias
    at x 30; a 10 A rail J1.1 -> J2.1. Returns (board, config)."""
    g = BoardGen(layers=2, size=(60.0, 20.0))
    for lay in ("F.Cu", "B.Cu"):
        g.zone("P", lay, [(4.0, 4.0), (56.0, 4.0), (56.0, 16.0), (4.0, 16.0)])
    pin = {"type": "thru_hole", "shape": shape, "size": size, "drill": drill}
    for ref, x in (("J1", 9.0), ("J2", 51.0)):
        g.footprint(ref, "CONN", "Connector:Fake_tht", (x, 10.0), [{"number": 1, "at": (0, 0), "net": "P", **pin}],
                    attrs="through_hole", layer=layer, angle=angle)
    for y in (6.0, 10.0, 14.0):
        g.via((30.0, y), "P", ("F.Cu", "B.Cu"))
    path = g.write(tmp / f"{name}.kicad_pcb")
    if dnp or field:                    # the footprint's DNP flag or a GT field, written into the text
        text = path.read_text(encoding="utf-8")
        if dnp:
            text = text.replace('(attr through_hole)', '(attr through_hole dnp)', 1)
        if field:
            text = text.replace('(property "Reference" "J1"', f'(property "GT" "{field}"\n\t\t\t(at 0 0 0)\n\t\t\t'
                                '(layer "F.Fab")\n\t\t\t(hide yes)\n\t\t)\n\t\t(property "Reference" "J1"', 1)
        path.write_text(text, encoding="utf-8")
    from groundtruth.adapters.file_source import load_board
    from groundtruth.config.settings import settings_path
    board = load_board(path)
    sc = SettingsFile.new(settings_path(path), board)
    sc.set_nets([])
    sc.doc["analysis"]["plane_capacitance"] = False
    sc.set_dc_loads([DcLoadSpec("rail", "J1.1", [DcLoad("J2.1", load_a, "")], "")])
    sc.save()
    return board, SettingsFile.load(sc.path).config


def rail(board, cfg, resolution: float = 1.0):
    """The rail's DC load result at a resolution."""
    cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
    return simulate(board, cfg).dc_loads[0]


def case_converge(tmp: Path) -> None:
    """R by resolution between two pins in 3 mm holes; shares and vias."""
    board, cfg = two_pins(tmp, "conv")
    rs = {}
    for res_ in (0.25, 1.0, 4.0):
        r = rail(board, cfg, res_)
        check(not r.error, f"converge: solves at resolution {res_} ({r.error or 'no error'})")
        if r.error:
            return
        rs[res_] = r.dc[0]["r_mohm"]
        check(abs(r.layer_share.get("F.Cu", 0) - 0.5) < 0.03, f"converge: F.Cu share {r.layer_share.get('F.Cu', 0):.3f}"
              f" at {res_} (1/2: the layers alike)")
        check(not r.dc[0]["vias"], f"converge: no via carries 1 % at {res_} ({len(r.dc[0]['vias'])})")
        check(abs(r.loads[0]["drop_mv"] - r.dc[0]["drop_mv"]) < 1e-3 * r.dc[0]["drop_mv"] + 1e-9,
              f"converge: the load's drop is the path's at {res_}")
    print("      R mΩ by resolution:", {k: round(v, 5) for k, v in rs.items()})
    check(abs(rs[1.0] - rs[4.0]) / rs[4.0] < 0.03, f"converge: R at 1x within 3 % of 4x ({rs[1.0]:.5f} / {rs[4.0]:.5f})")
    # the strip's own resistance as a sanity bound: 42 mm between the pins, 12 mm wide, two 35 µm layers
    from groundtruth.solver.peec import SIGMA_CU
    strip = 42e-3 / (SIGMA_CU * 12e-3 * 70e-6) * 1e3
    check(0.5 * strip < rs[4.0] < 1.5 * strip, f"converge: R {rs[4.0]:.4f} mΩ near the strip's {strip:.4f} mΩ "
          "(less the pins' width, plus their spreading)")


def case_bottom(tmp: Path) -> None:
    """Connectors on B.Cu and a turned slot give the same R as their plain twins."""
    base = rail(*two_pins(tmp, "top"))
    bot = rail(*two_pins(tmp, "bot", layer="B.Cu"))
    check(not bot.error, f"bottom: solves ({bot.error or 'no error'})")
    if not bot.error:
        check(abs(bot.dc[0]["r_mohm"] - base.dc[0]["r_mohm"]) / base.dc[0]["r_mohm"] < 0.02,
              f"bottom: R {bot.dc[0]['r_mohm']:.5f} as on top {base.dc[0]['r_mohm']:.5f}")
    slot0 = rail(*two_pins(tmp, "s0", drill="oval 4 2", size=(6.0, 4.0), shape="oval"))
    slot45 = rail(*two_pins(tmp, "s45", drill="oval 4 2", size=(6.0, 4.0), shape="oval", angle=45.0))
    slot90 = rail(*two_pins(tmp, "s90", drill="oval 4 2", size=(6.0, 4.0), shape="oval", angle=90.0))
    for name, r in (("0°", slot0), ("45°", slot45), ("90°", slot90)):
        check(not r.error, f"bottom: slot at {name} solves ({r.error or 'no error'})")
    if not (slot0.error or slot90.error or slot45.error):
        r0, r45, r90 = (x.dc[0]["r_mohm"] for x in (slot0, slot45, slot90))
        print(f"      slot R mΩ: 0° {r0:.5f}, 45° {r45:.5f}, 90° {r90:.5f}")
        # a slot across the current (90°) leaves the pin less width to hand over than one along it: R(90) >= R(0)
        check(min(r0, r90) * 0.98 <= r45 <= max(r0, r90) * 1.02, "bottom: 45° between 0° and 90°")


def case_tiny(tmp: Path) -> None:
    """A USB-C shield-sized slot fed, coarse to fine."""
    board, cfg = two_pins(tmp, "tiny", drill="oval 0.6 1.7", size=(1.0, 2.1), shape="oval", load_a=1.0)
    for res_ in (0.25, 0.5, 1.0):
        r = rail(board, cfg, res_)
        check(not r.error, f"tiny: a 0.6 x 1.7 mm slot fed at resolution {res_} ({r.error or 'no error'})")


def case_thin(tmp: Path) -> None:
    """A 1.5 mm hole in a 2.0 mm pad fed on a coarse grid."""
    board, cfg = two_pins(tmp, "thin", drill="1.5", size=(2.0, 2.0), load_a=1.0)
    for res_ in (0.1, 0.25, 1.0):
        r = rail(board, cfg, res_)
        check(not r.error, f"thin: a 0.25 mm ring round a 1.5 mm hole fed at resolution {res_} "
                           f"({r.error or 'no error'})")


def case_dnp(tmp: Path) -> None:
    """A DNP connector's pin fed."""
    board, cfg = two_pins(tmp, "dnp", dnp=True)
    check(board.footprint("J1").dnp, "dnp: the footprint reads as DNP")
    r = rail(board, cfg)
    check(not r.error, f"dnp: a DNP pin in a 3 mm hole fed ({r.error or 'no error'})")


def case_fill(tmp: Path) -> None:
    """A pin soldered half way: joined on the soldered layer only, a larger drop."""
    from groundtruth.analysis.simulate import BoardModel
    full = two_pins(tmp, "full")
    half = two_pins(tmp, "half", field="fill:50")
    from groundtruth.analysis.pins import params_cache
    pp = params_cache(half[0], half[1])("J1")
    check(abs(pp.fill - 0.5) < 1e-9, f"fill: the GT field reads as fill {pp.fill}")
    st = BoardModel(*half).build((2.0, 2.0, 58.0, 18.0), ["F.Cu", "B.Cu"], {"P"}, 0.25, False)
    lid = st.pin_lids.get("J1.1")
    layers = {st.names[b.a[0]] for b in st.branches if b.kind == "bond" and b.b == lid}
    check(layers == {"B.Cu"}, f"fill: J1's lid bonded on {sorted(layers)} (the soldered side, B.Cu)")
    rf, rh = rail(*full), rail(*half)
    check(not rh.error, f"fill: solves ({rh.error or 'no error'})")
    if not (rf.error or rh.error):
        check(rh.loads[0]["drop_mv"] > rf.loads[0]["drop_mv"], f"fill: drop {rh.loads[0]['drop_mv']:.4f} mV half "
              f"soldered > {rf.loads[0]['drop_mv']:.4f} mV full")


def case_shunt(tmp: Path) -> None:
    """A through-hole shunt with slotted pins in the path."""
    sys.path.insert(0, str(HERE / "tests"))
    from test_dcload import _slotted_shunt_board
    board, cfg = _slotted_shunt_board(tmp)
    for res_ in (0.5, 1.0, 4.0):
        r = rail(board, cfg, res_)
        check(not r.error, f"shunt: solves at {res_} ({r.error or 'no error'})")
        if not r.error:
            part = next((p for p in r.dc[0]["parts"] if p["name"] == "R1"), None)
            check(part is not None and abs(part["r_mohm"] - 0.1) < 0.002,
                  f"shunt: R1 at {part and round(part['r_mohm'], 4)} mΩ at {res_}")


def case_cutall(tmp: Path) -> None:
    """Whole boards with every plated hole cut (HOLE_CUT_MM lowered under their 1 mm pins)."""
    from groundtruth.geometry import shapes
    for name, gen, spec in (("buck", discrete_buck_board(out_connector="tht"), None),
                            ("dist", distribution_board(), DcLoadSpec(
                                "VDD rail", "J1.2", [DcLoad(f"J{k}.2", 0.5, f"J{k}.1") for k in (2, 3, 4, 5)], "J1.1"))):
        out = {}
        for cut in (99.0, 0.5):
            shapes.HOLE_CUT_MM = cut
            board, sc, _ = make(tmp, gen, f"{name}{int(cut * 10)}", nets=())
            if spec is not None:
                sc.set_dc_loads([spec])
                sc.save()
            if name == "buck":
                from groundtruth.analysis.detect import detect_converters
                found = detect_converters(board, sc.config)
                if found:
                    s = found[0].spec
                    s.f_sw, s.i_out, s.v_in, s.v_out = 300e3, 3.0, 12.0, 3.0
                    sc.set_converters([s])
                    sc.save()
            cfg = SettingsFile.load(sc.path).config
            res = simulate(board, cfg)
            errs = [r.error for r in list(res.converters) + list(res.dc_loads) if r.error]
            check(not errs, f"cutall: {name} with holes {'cut' if cut < 1 else 'uncut'} solves ({errs or 'no error'})")
            rows = [(r.net, p["name"], p["r_mohm"]) for r in list(res.converters) + list(res.dc_loads)
                    for p in r.dc if not p.get("ac")]
            out[cut] = rows
        shapes.HOLE_CUT_MM = 1.5
        for (net, pname, a), (_, _, b) in zip(out[99.0], out[0.5]):
            check(abs(a - b) <= 0.25 * a + 1e-6, f"cutall: {name} {net} {pname}: R {a:.4f} uncut, {b:.4f} cut")


def case_signal(tmp: Path) -> None:
    """A signal path between through-hole connector pins (SIG from J1.1 to J2.1 on F.Cu, each connector's pin 2 on
    the GND plane on B.Cu, 1 mm holes) with its holes cut and uncut: it solves, its return keeps its DC connection,
    the figures stay close."""
    from groundtruth.geometry import shapes
    out = {}
    g = BoardGen(layers=2, size=(50.0, 20.0))
    tht = {"type": "thru_hole", "drill": 1.0, "size": (1.7, 1.7), "shape": "circle"}
    for ref, x in (("J1", 6.0), ("J2", 44.0)):
        g.footprint(ref, "Conn_01x02", "Connector:Fake_2pin", (x, 10.0), [
            {"number": 1, "at": (0.0, 0.0), "net": "SIG", **tht}, {"number": 2, "at": (0.0, 2.54), "net": "GND", **tht}],
            attrs="through_hole")
    g.track((6.0, 10.0), (44.0, 10.0), "SIG", width=0.3)
    g.plane("GND", "B.Cu")
    for cut in (99.0, 0.5):
        shapes.HOLE_CUT_MM = cut
        board, sc, _ = make(tmp, g, f"sig{int(cut * 10)}", nets=())
        sc.doc["nets"] = [{"name": "SIG", "driver": "J1.1", "receivers": ["J2.1"], "freq": "10MHz", "rise_time": "5ns"}]
        sc.save()
        cfg = SettingsFile.load(sc.path).config
        res = simulate(board, cfg)
        r = res.routes[0] if res.routes else None
        check(r is not None and not r.error, f"signal: SIG with holes {'cut' if cut < 1 else 'uncut'} solves "
                                             f"({(r.error if r else 'no route') or 'no error'})")
        if r is None or r.error:
            continue
        check(r.dc_connected, f"signal: its return is DC connected ({'cut' if cut < 1 else 'uncut'})")
        worst = (r.worst or {}).get("margin_db")
        out[cut] = (r.l_ratio, r.layer_share.get("B.Cu", 0.0), worst)
        print(f"      {'cut' if cut < 1 else 'uncut'}: l_ratio {r.l_ratio}, B.Cu share "
              f"{r.layer_share.get('B.Cu', 0.0):.3f}, worst margin {worst}")
    shapes.HOLE_CUT_MM = 1.5
    if len(out) == 2:
        (la, sa, wa), (lb, sb, wb) = out[99.0], out[0.5]
        check(la is None or lb is None or abs(la - lb) <= 0.15 * la, f"signal: L ratio {la} uncut, {lb} cut")
        check(abs(sa - sb) <= 0.05, f"signal: B.Cu share {sa:.3f} uncut, {sb:.3f} cut")
        check(wa is None or wb is None or abs(wa - wb) <= 2.0, f"signal: worst margin {wa} uncut, {wb} cut (dB)")


CASES = {"signal": case_signal, "converge": case_converge, "bottom": case_bottom, "tiny": case_tiny, "thin": case_thin, "dnp": case_dnp,
         "fill": case_fill, "shunt": case_shunt, "cutall": case_cutall}


def main() -> int:
    """Run the cases asked for; exit 1 when any failed."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="", help="comma-separated cases (default: all)")
    args = ap.parse_args()
    names = [n for n in args.only.split(",") if n] or list(CASES)
    tmp = Path(tempfile.mkdtemp(prefix="hole_audit_"))
    for n in names:
        print(f"== {n}")
        try:
            CASES[n](tmp / n if (tmp / n).mkdir() is None else tmp)
        except Exception as e:                      # a crash is a failed case, with its message
            import traceback
            traceback.print_exc()
            check(False, f"{n}: raised {type(e).__name__}: {e}")
    print(f"\n{len(FAILS)} failed" + (": " + "; ".join(FAILS) if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
