"""A power stage's control loop (analysis/loop.py) without a Simulate: the parts found, the controller's figures and
where each came from, the crossover, phase and gain margins, the poles and zeros, and a Bode / pole-zero PNG.

    .venv/Scripts/python tools/dev/loop_check.py BOARD SETTINGS [--only conv1 ...] [--set conv1:cs_gain=0.05 ...]
        [--png OUT_DIR]

--set overrides one loop figure of a stage, as the settings' `loop:` would (mode, vref, gm, ro, ea_gain, ea_gbw,
vsense_max, ith_range, cs_gain, slope, ramp, r_sense; values with units: 1.8m, 50mV/us). Reads the board and the
settings only; the PNGs go to OUT_DIR.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.adapters.file_source import load_board  # noqa: E402
from groundtruth.analysis.loop import analyse_loop  # noqa: E402
from groundtruth.analysis.transfer import BodeView, fmt_root, pz_rows  # noqa: E402
from groundtruth.config.settings import SettingsFile  # noqa: E402


def _fmt(v, unit=""):
    """A number for the listing (None as '-')."""
    from groundtruth.components.values import format_si
    return "-" if v is None else format_si(v, unit) if unit else f"{v:.4g}"


def main(argv: list[str]) -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", nargs="*", default=None, help="stage names")
    ap.add_argument("--set", nargs="*", default=[], help="STAGE:key=value loop overrides")
    ap.add_argument("--png", type=Path, default=None, help="folder for <stage>_loop.png")
    args = ap.parse_args(argv)
    board = load_board(args.board)
    config = SettingsFile.load(args.settings).config
    for spec in config.converters:
        if args.only and spec.name not in args.only:
            continue
        for item in args.set:
            stage, kv = item.split(":", 1)
            if stage == spec.name:
                k, v = kv.split("=", 1)
                spec.loop = dict(spec.loop or {}, **{k: v})
        print(f"== {spec.name} ({spec.topology}, {_fmt(spec.v_in, 'V')} -> {_fmt(spec.v_out, 'V')}, "
              f"{_fmt(spec.i_out, 'A')}, {_fmt(spec.f_sw, 'Hz')})")
        lr = analyse_loop(board, config, spec)
        if lr is None:
            print("  no controller found")
            continue
        p = lr.parts
        print(f"  controller {p.controller} channel {p.channel or '-'}, mode {p.mode or '-'}")
        for k, (v, src) in p.params.items():
            print(f"    {k} = {v:.4g}  ({src})")
        print(f"  COMP {p.comp_net}: Rc {_fmt(p.rc, 'Ω')}, Cc {_fmt(p.cc, 'F')}, Chf {_fmt(p.chf, 'F')}, "
              f"R to ground {_fmt(p.r_shunt, 'Ω')}")
        print(f"  FB {p.fb_net}: top {[(r, v or c) for r, v, c in p.top]}, bottom {[(x[0], x[1] or x[2]) for x in p.bottom]}")
        print(f"  sense {_fmt(p.r_sense, 'Ω')} ({', '.join(p.sense_parts) or '-'})")
        for n in lr.notes:
            print(f"  note: {n}")
        if lr.error:
            print(f"  NO LOOP: {lr.error}")
            continue
        print("  figures: " + ", ".join(f"{k} {v:.4g}" if isinstance(v, float) else f"{k} {v}"
                                        for k, v in lr.figures.items()))
        print(f"  f_c {_fmt(lr.fc, 'Hz')}, phase margin {_fmt(lr.pm)}°, gain margin {_fmt(lr.gm_db)} dB "
              f"(at {_fmt(lr.f180, 'Hz')}); f_c / f_sw = {lr.fc / spec.f_sw if lr.fc else float('nan'):.3f}")
        r = lr.response
        for label, roots in (("poles", r.poles), ("zeros", r.zeros)):
            rows = pz_rows(roots)
            print(f"  {label}: " + "; ".join(fmt_root(x) for x in rows))
        if args.png:
            from groundtruth.report.plots import draw_bode, draw_pz, figure_png
            args.png.mkdir(parents=True, exist_ok=True)
            view = BodeView(spec.name, [r])
            (args.png / f"{spec.name}_loop.png").write_bytes(figure_png(draw_bode, view))
            (args.png / f"{spec.name}_loop_pz.png").write_bytes(figure_png(draw_pz, view))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
