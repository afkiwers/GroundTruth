"""One PDN rail's Simulate on a board copy: its source, |Z| at the low end, each port's step figures and notes.

    .venv/Scripts/python tools/dev/pdn_step_check.py BOARD SETTINGS --rail +5V [--json OUT]
"""
import argparse
import json
import sys

from groundtruth.adapters.file_source import load_board
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import SettingsFile


def main():
    """Run the rail and print what its step response rests on."""
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--rail", required=True)
    ap.add_argument("--json")
    ap.add_argument("--png", help="folder for each rail's decap-share plot (report/plots.draw_pdn_decaps)")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = SettingsFile.load(a.settings).config
    r = simulate(load_board(a.board), cfg, nets=[n for n in (p.label for p in cfg.pdn) if a.rail in n])
    for d in r.pdn:
        print(d.name, "source:", d.source, "| stage:", d.stage, "| f_min", d.f_min, "| step", d.i_step, d.i_step_source)
        for p in d.ports:
            print(f"  {p['part']}: f {p['f'][0]:.3g}..  |Z| {p['z_ohm'][0]:.4g} {p['z_ohm'][1]:.4g} {p['z_ohm'][2]:.4g} "
                  f"Ω, phase {p['phase_deg'][0]:.1f}°; dc {p.get('dc')}; step {p.get('step')}")
        for n in d.notes:
            print("  note:", n)
    for i in r.issues:
        if i.check.startswith("pdn"):
            print(" ", i.severity, i.message)
    if a.png:
        import pathlib
        from groundtruth.report.plots import draw_pdn_decaps, figure_png
        pathlib.Path(a.png).mkdir(parents=True, exist_ok=True)
        for d in r.pdn:
            if d.ports:
                name = "".join(c if c.isalnum() else "_" for c in d.name)
                (pathlib.Path(a.png) / f"decaps_{name}.png").write_bytes(figure_png(draw_pdn_decaps, d, size=(9.5, 3.6)))
                for row in d.decaps:
                    print("  decap", {k: row.get(k) for k in ("ref", "value", "srf", "peak_share", "peak_at",
                                                              "peak_f", "band", "little", "for_left_out")})
    if a.json:
        json.dump([d.summary() for d in r.pdn], open(a.json, "w"), default=str)


if __name__ == "__main__":
    main()
