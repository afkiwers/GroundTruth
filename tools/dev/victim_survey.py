"""What on a motor-drive board could be upset by its bridge: the ICs (microcontroller, regulators, drivers), the
sensitive nets (reset, crystal, Hall, debug, gate-driver inputs, current sense), the ground nets and how far each
sits from the bridge. Read-only; a first look before a coupling analysis.

    .venv/Scripts/python tools/dev/victim_survey.py BOARD.kicad_pcb
"""
from __future__ import annotations

import math
import re
import sys

SENSITIVE = [("reset", r"N?RST|RESET|NRST"), ("crystal", r"OSC|XTAL|XIN|XOUT|HSE|LSE"), ("hall", r"HALL|HAL_?[ABCUVW]"),
             ("debug", r"SWD|SWCLK|SWDIO|JTAG|TCK|TMS"), ("pwm / gate input", r"PWM|IN[HL]|[HL]IN|GEN_[A-Z]_(HI|LO)"),
             ("current sense", r"ISENSE|I_?SENS|CSA|SO[ABC]|SN[ABC]|SP[ABC]|IS[+-]|SHUNT"),
             ("analog / ADC", r"ADC|VSENSE|VBAT_?SENSE|TEMP|NTC"), ("bus / comms", r"CAN|UART|TX|RX|DSHOT|SIG")]


def main(argv: list[str]) -> int:
    """The command line: the centre of the detected bridges' switches, the ground nets, the ICs nearest to it with
    their grounds, and the sensitive nets (by name) with their pads, nearest pad and track length."""
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.detect import _is_ground, detect_bridges, ref_prefix
    from groundtruth.config.settings import Config
    board = load_board(argv[0])
    cfg = Config()
    bridges = detect_bridges(board, cfg)
    cell = [p.position for d in bridges for leg in d.spec.legs for r in leg.high_side + leg.low_side
            for p in board.footprint(r).pads]
    centre = (sum(x for x, _ in cell) / len(cell), sum(y for _, y in cell) / len(cell)) if cell else (0.0, 0.0)
    print(f"{board.source}: {len(board.copper_layers)} layers, bridge centre at ({centre[0]:.1f}, {centre[1]:.1f}) mm")
    grounds = sorted({n for n in board.nets if _is_ground(cfg, n)})
    print("ground nets:", ", ".join(grounds) or "(none named as ground)")
    print("\nICs (U), nearest first:")
    ics = [fp for fp in board.footprints if ref_prefix(fp.reference) in ("U", "IC") and not fp.dnp]
    for fp in sorted(ics, key=lambda f: math.dist(f.position, centre))[:25]:
        gnds = sorted({p.net for p in fp.pads if p.net in grounds})
        print(f"  {fp.reference:6s} {fp.value[:28]:28s} {math.dist(fp.position, centre):6.1f} mm  grounds {', '.join(gnds)}")
    print("\nsensitive nets:")
    for what, rx in SENSITIVE:
        hits = sorted(n for n in board.nets if re.search(rx, n.split("/")[-1], re.I))
        for n in hits[:8]:
            pads = [p for fp in board.footprints for p in fp.pads if p.net == n]
            near = min((math.dist(p.position, centre) for p in pads), default=float("nan"))
            length = sum(math.dist(t.start, t.end) for t in board.tracks if t.net == n)
            print(f"  {what:16s} {n[:44]:44s} {len(pads):2d} pads, nearest {near:5.1f} mm from the bridge, "
                  f"{length:5.1f} mm of track")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
