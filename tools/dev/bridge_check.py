"""A motor bridge on a real board: its six-step drive, the legs' hot loops, the bus capacitors' ripple and the
conducted estimate of all legs at the input, at an operating point given here (the settings file is only read).

    .venv/Scripts/python tools/dev/bridge_check.py BOARD SETTINGS [--bridge NAME] --v-bus 48 --f-pwm 20k
        --i-phase 100 --speed 3000 [--mode motoring|generating] [--pwm hpwm_lon|hon_lpwm|bipolar]
        --motor R_LL,L_LL,KE_LL,POLE_PAIRS[,star|delta[,trapezoidal|sinusoidal]] [--rds-on 1.4m] [--c-oss 2n] [--q-oss 170n]
        [--dead 47n] [--budgets 100000,200000] [--no-victims] [--sweep-i 50,100,200 --sweep-speed 0,3000]

--budgets: the board's analysis.max_nodes for each run (grid convergence: compare the legs' L, the capacitor
shares and the conducted worst). The board and settings are never written.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """The command line (see the module): the bridge from the settings with the operating point and motor given
    here, simulated (once per --budgets value), its drive and legs printed; optionally a sweep."""
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.components.values import parse_value
    from groundtruth.config.settings import MotorSpec, SettingsFile
    from groundtruth.report.formats import bridge_text, converter_text

    def num(s, unit=""):
        """A number from the command line in SI notation, an optional unit after it; exits when unreadable."""
        s = s.strip()
        if unit and s.lower().endswith(unit.lower()):
            s = s[: -len(unit)]
        v = parse_value(s)
        if v is None:
            raise SystemExit(f"cannot read {s!r}")
        return v

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--bridge")
    ap.add_argument("--v-bus", required=True)
    ap.add_argument("--f-pwm", required=True)
    ap.add_argument("--i-phase", required=True)
    ap.add_argument("--speed", required=True)
    ap.add_argument("--mode", default="motoring")
    ap.add_argument("--pwm", default="hpwm_lon")
    ap.add_argument("--motor", required=True, help="R_LL,L_LL,KE_LL,POLE_PAIRS[,star|delta[,trapezoidal|sinusoidal]]")
    ap.add_argument("--rds-on")
    ap.add_argument("--c-oss")
    ap.add_argument("--q-oss")
    ap.add_argument("--dead")
    ap.add_argument("--budgets", default="")
    ap.add_argument("--no-victims", action="store_true", help="leave out the victim runs (faster)")
    ap.add_argument("--sweep-i", default="", help="phase currents of a sweep, e.g. 50,100,200")
    ap.add_argument("--sweep-speed", default="", help="speeds of a sweep, e.g. 0,1500,3000")
    args = ap.parse_args()
    board = load_board(args.board)
    cfg = SettingsFile.load(args.settings).config
    if not cfg.bridges:
        raise SystemExit("no bridges in the settings: run groundtruth detect --write first")
    b = next((x for x in cfg.bridges if x.name == args.bridge), None) if args.bridge else cfg.bridges[0]
    if b is None:
        raise SystemExit(f"no bridge {args.bridge!r}: {[x.name for x in cfg.bridges]}")
    m = args.motor.split(",")
    motor = MotorSpec(m[4] if len(m) > 4 else "star", num(m[0]), num(m[1], "H"), float(m[2]), int(m[3]),
                      m[5] if len(m) > 5 else b.motor.emf, b.motor.advance_deg)
    b = replace(b, v_bus=num(args.v_bus, "V"), f_pwm=num(args.f_pwm, "Hz"), i_phase=num(args.i_phase, "A"),
                speed_rpm=float(args.speed), mode=args.mode, pwm=args.pwm, motor=motor,
                rds_on=num(args.rds_on) if args.rds_on else b.rds_on, c_oss=num(args.c_oss, "F") if args.c_oss
                else b.c_oss, q_oss=num(args.q_oss, "C") if args.q_oss else b.q_oss,
                dead_time=num(args.dead, "s") if args.dead else b.dead_time,
                sweep_i_phase=[num(x, "A") for x in args.sweep_i.split(",") if x.strip()],
                sweep_speed_rpm=[float(x) for x in args.sweep_speed.split(",") if x.strip()])
    budgets = [int(x) for x in args.budgets.split(",") if x] or [cfg.analysis.max_nodes]
    for budget in budgets:
        c = replace(cfg, bridges=[b], analysis=replace(cfg.analysis, max_nodes=budget))
        if args.no_victims:
            c = replace(c, victims=replace(c.victims, enabled=False))
        t = time.time()
        res = simulate(board, c, nets=[b.name])
        print(f"===== max_nodes {budget}: {time.time() - t:.1f} s")
        for br in res.bridges:
            print("\n".join(bridge_text(br)))
        for leg in res.converters:
            print("\n".join(converter_text(leg)))
            print("    capacitor shares at f_pwm: " + ", ".join(f"{x['name']} {x['share_sw']:.2f}" for x in leg.caps
                                                              if x.get("share_sw") is not None)[:300])
        for i in res.issues:
            if i.net.startswith(b.name):
                print(f"  [{i.severity}] {i.check}: {i.message[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
