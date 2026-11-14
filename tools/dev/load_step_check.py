"""A power stage's load step (analysis/load_step.py) without a Simulate: the model used (the loop's own or the
approximate one), the step, each node's dip, ring-back, final deviation and recovery, the converter current's slew
against the inductor's, the notes, and the impedance / step PNGs.

    .venv/Scripts/python tools/dev/load_step_check.py BOARD SETTINGS [--only conv1 ...] [--from 5 --to 10]
        [--rise 1u] [--cable 10m,500n] [--c-load 100u,5m] [--coil 2,1m] [--max-dev 5%] [--loop conv3:fc=40k,pm=60]
        [--png OUT_DIR]

Without --from/--to/--coil the stage's own load_step (else half to full load). --loop sets a stage's approximate
loop as the settings' `loop: {fc, pm}` would (a constant-on-time or internally compensated controller). The
capacitors are taken at their DC bias, as Simulate takes them (analysis/bias.at_dc). Reads the board and the
settings only; the PNGs go to OUT_DIR.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.adapters.file_source import load_board  # noqa: E402
from groundtruth.analysis.bias import at_dc  # noqa: E402
from groundtruth.analysis.load_step import analyse_load_step, resolved, step_line  # noqa: E402
from groundtruth.analysis.transfer import BodeView, fmt_s  # noqa: E402
from groundtruth.components.values import format_si, parse_value  # noqa: E402
from groundtruth.config.settings import LoadStep, SettingsFile  # noqa: E402


def _pair(text: str) -> tuple[float, float]:
    """'10m,500n' as two numbers."""
    a, b = text.split(",")
    return parse_value(a), parse_value(b)


def main(argv: list[str]) -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", nargs="*", default=None, help="stage names")
    ap.add_argument("--from", dest="i_from", default=None, help="A before the step")
    ap.add_argument("--to", dest="i_to", default=None, help="A after it")
    ap.add_argument("--rise", default=None, help="the current's ramp time")
    ap.add_argument("--cable", default=None, help="R,L of the harness to the load")
    ap.add_argument("--c-load", default=None, help="C,ESR at the load")
    ap.add_argument("--coil", default=None, help="R,L of a load branch switched on")
    ap.add_argument("--max-dev", default=None, help="the deviation allowed (5%%)")
    ap.add_argument("--loop", nargs="*", default=[], help="STAGE:fc=40k,pm=60 approximate loops")
    ap.add_argument("--png", type=Path, default=None, help="folder for <stage>_load_step*.png")
    args = ap.parse_args(argv)
    board = load_board(args.board)
    config, _, bias_notes = at_dc(board, SettingsFile.load(args.settings).config)
    for spec in config.converters:
        if args.only and spec.name not in args.only:
            continue
        for item in args.loop:
            stage, kv = item.split(":", 1)
            if stage == spec.name:
                spec.loop = dict(spec.loop or {}, **dict(x.split("=", 1) for x in kv.split(",")))
        ls = spec.load_step or LoadStep()
        if args.i_from is not None:
            ls = replace(ls, i_from=parse_value(args.i_from))
        if args.i_to is not None:
            ls = replace(ls, i_to=parse_value(args.i_to))
        if args.rise:
            ls = replace(ls, rise=parse_value(args.rise))
        if args.cable:
            ls = replace(ls, cable_r=_pair(args.cable)[0], cable_l=_pair(args.cable)[1])
        if args.c_load:
            ls = replace(ls, c_load=_pair(args.c_load)[0], esr_load=_pair(args.c_load)[1])
        if args.coil:
            ls = replace(ls, r_load=_pair(args.coil)[0], l_load=_pair(args.coil)[1])
        if args.max_dev:
            ls = replace(ls, max_dev=parse_value(args.max_dev.rstrip("%")) / 100)
        print(f"== {spec.name} ({spec.topology}, {format_si(spec.v_in, 'V')} -> {format_si(spec.v_out, 'V')}, "
              f"{format_si(spec.i_out, 'A')}, {format_si(spec.f_sw, 'Hz')}): {resolved(ls, spec)}")
        r = analyse_load_step(board, config, spec, ls)
        print("  " + step_line(r))
        if r.error:
            continue
        f = r.figures
        print(f"  model: {'approximate (bandwidth, phase margin)' if r.approximate else 'the loop model'}, "
              f"crossover {format_si(f.get('fc'), 'Hz') if f.get('fc') else '-'}")
        for n in r.nodes:
            m = n.metrics
            print(f"  {n.node}: dip {m['dip'] * 1e3:+.4g} mV ({m['dip_pct']:+.2f} %) at {fmt_s(m['t_dip'])}, "
                  f"back {m['back'] * 1e3:.3g} mV, final {m['final'] * 1e3:+.3g} mV, recovery {fmt_s(m['recovery'])}")
        if f.get("slew") is not None:
            print(f"  converter current: slew {f['slew'] * 1e-6:.3g} A/us (inductor: "
                  f"{(f.get('slew_limit') or 0) * 1e-6:.3g} A/us), peak {f.get('peak_conv') or 0:.3g} A"
                  + (f", load final {f['i_final']:.3g} A" if f.get("i_final") is not None else ""))
        for n in r.notes:
            print(f"  note: {n}")
        if args.png:
            from groundtruth.report.plots import draw_bode, draw_step, figure_png
            args.png.mkdir(parents=True, exist_ok=True)
            view = BodeView(r.responses[0].owner, r.responses)
            (args.png / f"{spec.name}_load_step_z.png").write_bytes(figure_png(draw_bode, view))
            (args.png / f"{spec.name}_load_step.png").write_bytes(figure_png(draw_step, view))
    for n in bias_notes:
        print(f"bias: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
