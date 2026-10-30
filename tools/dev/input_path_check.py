"""Why a power path (a bridge's or converter's input: the connector's + pin to the bus) is or isn't found: from one
net, every part leading on to another non-ground net, its model, and whether the input-path search passes it
(power._passes: a short, an inductor or ferrite, a resistor up to INPUT_PATH_MAX_OHMS; joining the model's nets:
a 4-terminal shunt's force pads only), breadth first, up to the goal net.

    .venv/Scripts/python tools/dev/input_path_check.py BOARD.kicad_pcb --settings S.groundtruth.yaml \\
        --from VBUS --to BATTERY+

Net names may be given by their last part (VBUS for /DC Power Inputs/VBUS). Read-only: nothing is written. Run it on
a board you can't share and report the lines that end in "stops here"."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def main() -> int:
    """Walk from --from towards --to and print each part on the way: passes, or why it stops the path."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("--settings", help="the board's settings file (its part models); without it no part has a model")
    ap.add_argument("--from", dest="start", required=True, help="the bus net (the bridge's or stage's rail)")
    ap.add_argument("--to", dest="goal", required=True, help="the input connector's + net")
    ap.add_argument("--depth", type=int, default=6)
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.power import INPUT_PATH_MAX_OHMS, _passes
    from groundtruth.analysis.safety import is_ground
    from groundtruth.components.models import model_nets
    from groundtruth.config.settings import Config, SettingsFile
    board = load_board(args.board)
    config = SettingsFile.load(args.settings).config if args.settings else Config()

    def find(name: str) -> str:
        """A net by its full name, else the one whose last part is `name`."""
        if name in board.nets:
            return name
        hits = [n for n in board.nets if n.split("/")[-1] == name]
        if len(hits) != 1:
            sys.exit(f"net {name!r}: {'not found' if not hits else 'several: ' + ', '.join(hits)}")
        return hits[0]
    start, goal = find(args.start), find(args.goal)
    by_net = board.parts_by_net()
    print(f"from {start} to {goal}; parts pass when modelled short / inductor / ferrite / resistor <= "
          f"{INPUT_PATH_MAX_OHMS:g} ohm (no model in the settings: they stop the path)")
    seen, frontier = {start}, [start]
    for depth in range(args.depth):
        nxt = []
        for net in frontier:
            for fp in by_net.get(net, []):
                m = config.model_for(fp.reference)
                joined = model_nets(fp, m) if m is not None else set(fp.nets())
                others = sorted(n for n in joined if n != net and not is_ground(n))
                if net not in joined and m is not None and m.kind in ("short", "resistor", "inductor", "ferrite"):
                    # a part whose model joins other pads than this net's (a 4-terminal shunt's force pads, a FET's
                    # drain and source): which it joins, and its pads (number, net, size) to see why
                    pads = ", ".join(f"{p.number}:{(p.net or '-').split('/')[-1]} {p.size[0]:.2f}x{p.size[1]:.2f}"
                                     for p in fp.pads)
                    print(f"  {'  ' * depth}{net.split('/')[-1]} -> {fp.reference:6s} {fp.value[:18]:18s} model "
                          f"{m.kind} joins only {', '.join(sorted(n.split('/')[-1] for n in joined))}, not this net: "
                          f"pads {pads[:200]}")
                    continue
                if net not in joined or not others or len(fp.nets()) > 6:
                    continue                    # not a series part from this net (a cap to ground, an IC)
                ok = _passes(config, fp)          # exactly power.input_path's rule: the model's nets, any number
                what = "no model in the settings" if m is None else (
                    f"model {m.kind}" + (f" R {m.get('R'):g} ohm" if m.kind == "resistor" else ""))
                kelvin = " (joins only " + ", ".join(sorted(n.split("/")[-1] for n in joined)) + ")"                     if len(joined) < len(fp.nets()) else ""
                verdict = "passes" if ok else "stops here"
                print(f"  {'  ' * depth}{net.split('/')[-1]} -> {fp.reference:6s} {fp.value[:18]:18s} "
                      f"{what}{kelvin} -> {', '.join(o.split('/')[-1] for o in others)[:50]}: {verdict}")
                if ok:
                    for o in others:
                        if o not in seen:
                            seen.add(o)
                            nxt.append(o)
        if goal in seen:
            print(f"reached {goal}")
            return 0
        frontier = nxt
        if not frontier:
            break
    print(f"NOT reached {goal}: the lines ending in 'stops here' are the parts to give a series model "
          "(Parts tab: a FET that is on as a short or its R_DS(on) as a resistor; a shunt its resistance)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
