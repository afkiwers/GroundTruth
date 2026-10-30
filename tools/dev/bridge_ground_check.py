"""Why a motor bridge's legs are (not) grouped on one ground: for each leg detection finds, its low side's net and
every part on that net, with its model, its resistance and whether the shunt rule passes it (detect._LegFinder.grounds,
[A71]: at most two parts that pass current, resistors up to power.INPUT_PATH_MAX_OHMS), and the ground nets reached.

    .venv/Scripts/python tools/dev/bridge_ground_check.py BOARD.kicad_pcb [--settings S.groundtruth.yaml]

Read-only: nothing is written. Run it on a board you can't share and report the lines of one leg."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def main() -> int:
    """Print each leg's low-side net, the parts on it and the ground nets it reaches."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("--settings", help="the board's settings file (its part models); default: none")
    args = ap.parse_args()
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis import detect
    from groundtruth.analysis.power import INPUT_PATH_MAX_OHMS
    from groundtruth.analysis.tags import board_tags
    from groundtruth.config.settings import Config, SettingsFile
    board = load_board(args.board)
    config = SettingsFile.load(args.settings).config if args.settings else Config()
    finder = detect._LegFinder(board, config, board_tags(board), detect.series_sensors(board, config))
    legs = finder.legs()
    print(f"{len(legs)} leg(s); the threshold: resistors up to {INPUT_PATH_MAX_OHMS:g} ohm, at most two parts")
    for leg in legs:
        own = leg.ret or leg.ground
        print(f"\nleg {leg.node}: low side {', '.join(leg.ls)} on {own}"
              + (f" -> grouped on {leg.ground} through {', '.join(leg.shunt)}" if leg.ret else ""))
        print(f"  {own} counts as ground by its name: {detect._is_ground(config, own)}")
        for fp in finder.by_net.get(own, []):
            if fp.reference in leg.ls:
                continue
            m = detect._model(config, fp)
            r = m.get("R") if m.kind == "resistor" else None
            nets = sorted(n for n in fp.nets() if n != own)
            ok = fp.reference in finder.sensors or m.kind in ("short", "ferrite") or (
                m.kind == "resistor" and r is not None and r <= INPUT_PATH_MAX_OHMS)
            why = ("passes" if ok and len(fp.nets()) == 2 else
                   f"{len(fp.nets())} nets, not 2" if ok else
                   f"model {m.kind!r}" + (f", R {r:g} ohm > {INPUT_PATH_MAX_OHMS:g}" if r is not None else ""))
            print(f"  {fp.reference:8s} {fp.value[:16]:16s} to {', '.join(nets)[:60]:60s} {why}"
                  + ("" if not nets else f"  (ground by name: {[detect._is_ground(config, n) for n in nets]})"))
        reached = finder.grounds(own)
        print("  ground nets reached:", ", ".join(f"{n} via {', '.join(p) or '-'}" for n, p in reached.items()) or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
