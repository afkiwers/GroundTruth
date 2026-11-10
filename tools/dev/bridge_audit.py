"""Audit round 10 (docs/VALIDATION_AUDIT.md): independent benchmarks of the motor bridges' physics. The reference is
a time-stepping simulation of the three-phase R-L-EMF circuit with ideal switches and reverse-conducting clamps,
written here (explicit steps, started from rest and run until it repeats); it shares no code with
analysis/motor.py, whose answer is event-driven and closed-form per interval.

    .venv/Scripts/python tools/dev/bridge_audit.py [--only drive,caps,losses,shunt] [--json OUT.json]

- drive: the phase, switch and bus currents (mean, RMS) of six-step drives (three PWM schemes, synchronous or not,
  trapezoidal and sinusoidal EMF, motoring and generating, a locked rotor) at the duty GroundTruth found, and the
  reference's own energy balance.
- caps: the bus capacitors' ripple current against the reference's bus current (its AC part).
- losses: conduction, reverse conduction, the hard turn-ons' V·I and swings per leg against the reference's.
- shunt: a low-side shunt's RMS current on a simulated board against the drive's low-side current.

Each case prints expected, actual, the deviation and PASS / FAIL against its stated tolerance. A FAIL is a finding to
investigate, not a test to loosen.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))             # boardgen, conftest.make: the generated KiCad boards

ROWS: list[dict] = []
HIGH, LOW, OFF = 1, 0, -1
PAIRS = ((0, 1), (0, 2), (1, 2), (1, 0), (2, 0), (2, 1))     # six-step: (sourcing, sinking) leg per 60° sector


def check(area: str, case: str, expected, actual, tol: float, rel: bool = True, note: str = "") -> bool:
    """Record one comparison: |actual − expected| within tol (relative to |expected| when rel, else absolute)."""
    dev = abs(actual - expected) / (abs(expected) if rel and expected != 0 else 1.0)
    ok = dev <= tol
    ROWS.append({"area": area, "case": case, "expected": expected, "actual": actual, "dev": dev, "tol": tol,
                 "rel": rel, "ok": ok, "note": note})
    print(f"  [{'PASS' if ok else 'FAIL'}] {area:7} {case:68} expected {expected:>11.5g}  actual {actual:>11.5g}"
          f"  dev {dev:.2e}" + (f"  ({note})" if note else ""))
    return ok


# ------------------------------------------------------------------------------------- the reference drive
def _trap(phi: float) -> float:
    """A unit trapezoid of period 2π: flat at +1 for 120° about 0, flat at −1 for 120° about π, linear between."""
    a = abs((phi + math.pi) % (2 * math.pi) - math.pi)
    return 1.0 if a <= math.pi / 3 else (-1.0 if a >= 2 * math.pi / 3 else 1.0 - 6.0 * (a - math.pi / 3) / math.pi)


def ref_emf(e_ll: float, shape: str, theta: float) -> list[float]:
    """A star winding's phase EMFs at the electrical angle theta, for a line-to-line peak e_ll: trapezoids of E/2
    (two flat phases in series give E) or sines of E/√3; phase A's top centred on 90°."""
    if shape == "trapezoidal":
        return [e_ll / 2 * _trap(theta - math.pi / 2 - k * 2 * math.pi / 3) for k in range(3)]
    return [e_ll / math.sqrt(3) * math.sin(theta - k * 2 * math.pi / 3) for k in range(3)]


def ref_drive(v: float, f_pwm: float, duty: float, dead: float, v_sd: float, r: float, l_h: float, e_ll: float,
              n_s: int, shape: str = "trapezoidal", pwm: str = "hpwm_lon", sync: bool = True,
              periods: int = 14, sub: int = 120) -> dict:
    """The six-step drive stepped through time from rest: `periods` electrical periods (6·n_s PWM periods each; a
    locked rotor n_s = 0: one sector held, `periods`·40 PWM periods), the last one measured. Per PWM period: the
    dead time, the on time (to duty·T), the dead time, the off time; `sub` explicit midpoint steps in the on and
    off times, 6 in a dead time. Each step: every leg's terminal voltage (V, 0, or a clamp at −v_sd / V + v_sd
    while its off leg carries current; an off leg without current is open until its terminal would pass a clamp),
    the star point from the conducting legs, di/dt = (v − v_n − e − R·i)/L. Returns means and mean squares."""
    T = 1.0 / f_pwm
    locked = n_s == 0
    n = 6 * n_s if not locked else 1
    w = 2 * math.pi * f_pwm / n if not locked else 0.0
    theta0 = math.pi / 6
    total = periods * n if not locked else periods * 40
    measure_from = total - n if not locked else total - 1
    on = min(max(duty, 0.0), 1.0) * T
    dt_d = min(dead, T)
    bounds = [0.0, min(dt_d, on), on, min(on + dt_d, T), T]
    i = [0.0, 0.0, 0.0]
    acc = {k: np.zeros(3) for k in ("i", "i2", "hs", "hs2", "ls2", "hs_ch2", "ls_ch2", "hs_rev", "ls_rev", "hs_vi",
                                    "ls_vi", "hs_dv2", "ls_dv2", "node_dv2", "ei")}
    bus, bus2, t_meas = 0.0, 0.0, 0.0
    hs_cross = np.zeros((3, 3))
    env_cross = np.zeros((3, 3))        # the high sides' PWM-period means (their slow part), products summed
    prev_state, prev_v = None, None

    def terminal(S, cur, e):
        """(leg voltages, conducting legs) for leg states S, currents cur, EMFs e: clamps for off legs with
        current; an open leg starts conducting when its floating terminal passes a clamp."""
        vk, cond = [0.0] * 3, []
        for k in range(3):
            if S[k] == HIGH:
                vk[k] = v
            elif S[k] == LOW:
                vk[k] = 0.0
            elif cur[k] > 1e-9:
                vk[k] = -v_sd
            elif cur[k] < -1e-9:
                vk[k] = v + v_sd
            else:
                continue
            cond.append(k)
        if len(cond) >= 2:
            vn = sum(vk[k] - e[k] for k in cond) / len(cond)
            for k in range(3):
                if k not in cond:                  # floating: does it pass a clamp?
                    vf = vn + e[k]
                    if vf > v + v_sd:
                        vk[k] = v + v_sd
                        cond.append(k)
                    elif vf < -v_sd:
                        vk[k] = -v_sd
                        cond.append(k)
                    else:
                        vk[k] = vf
            vn = sum(vk[k] - e[k] for k in cond) / len(cond)
        else:                                  # no current anywhere: the star point follows the one driven leg
            driven = [k for k in range(3) if S[k] != OFF]
            vn = (vk[driven[0]] - e[driven[0]]) if len(driven) == 1 else 0.0
            for k in range(3):
                if S[k] == OFF:
                    vk[k] = min(max(vn + e[k], -v_sd), v + v_sd)
            cond = []
        return vk, cond, vn

    for m in range(total):
        p_, q_ = PAIRS[(m // n_s) % 6] if not locked else PAIRS[0]
        meas = m >= measure_from
        hs_period = np.zeros(3)
        for seg in range(4):
            a, b = bounds[seg], bounds[seg + 1]
            if b <= a:
                continue
            S = [OFF, OFF, OFF]
            chop = (OFF, HIGH, OFF, LOW if sync else OFF)[seg]
            mirror = (OFF, LOW, OFF, HIGH if sync else OFF)[seg]
            if pwm == "hpwm_lon":
                S[p_], S[q_] = chop, LOW
            elif pwm == "hon_lpwm":
                S[p_], S[q_] = HIGH, mirror
            else:
                S[p_], S[q_] = chop, mirror
            steps = sub if seg in (1, 3) else 6
            h = (b - a) / steps
            for s in range(steps):
                t0 = m * T + a + s * h
                e = ref_emf(e_ll, shape, theta0 + w * t0) if e_ll else [0.0] * 3
                vk, cond, vn = terminal(S, i, e)
                if meas and prev_state is not None and s == 0:      # a switching instant: the turn-ons' V·I, swings
                    for k in range(3):
                        dv = abs(vk[k] - prev_v[k])
                        acc["node_dv2"][k] += dv * dv
                        if S[k] != prev_state[k] and S[k] != OFF:
                            side = "hs" if S[k] == HIGH else "ls"
                            acc[side + "_vi"][k] += dv * abs(i[k])
                            acc[side + "_dv2"][k] += dv * dv
                # midpoint step for the conducting legs
                d1 = [(vk[k] - vn - e[k] - r * i[k]) / l_h if k in cond else 0.0 for k in range(3)]
                im = [i[k] + 0.5 * h * d1[k] for k in range(3)]
                e2 = ref_emf(e_ll, shape, theta0 + w * (t0 + 0.5 * h)) if e_ll else [0.0] * 3
                vk2, cond2, vn2 = terminal(S, im, e2)
                new = [i[k] + h * (vk2[k] - vn2 - e2[k] - r * im[k]) / l_h if k in cond2 else (0.0 if k not in cond
                                                                                                 else i[k])
                       for k in range(3)]
                for k in range(3):
                    if S[k] != OFF:
                        continue
                    if abs(i[k]) > 1e-9:           # a freewheeling current that ran out stays at zero
                        if k in cond and (new[k] > 0) != (i[k] > 0):
                            new[k] = 0.0
                    elif k in cond2:               # an open leg started by a clamp: only in the clamp's direction
                        if (new[k] > 0) != (vk2[k] < 0):
                            new[k] = 0.0
                if any(S[k] == OFF and new[k] == 0.0 and k in cond for k in range(3)):
                    live = [k for k in range(3) if not (S[k] == OFF and new[k] == 0.0)]
                    if len(live) == 2:             # the other two carry equal and opposite currents
                        d = (new[live[0]] - new[live[1]]) / 2
                        new[live[0]], new[live[1]] = d, -d
                    elif len(live) < 2:
                        new = [0.0, 0.0, 0.0]
                if meas:
                    mid = [(i[k] + new[k]) / 2 for k in range(3)]
                    sq = [(i[k] ** 2 + i[k] * new[k] + new[k] ** 2) / 3 for k in range(3)]
                    hs = [mid[k] if S[k] == HIGH or (S[k] == OFF and mid[k] < 0) else 0.0 for k in range(3)]
                    for k in range(3):
                        acc["i"][k] += mid[k] * h
                        acc["i2"][k] += sq[k] * h
                        acc["ei"][k] += e2[k] * mid[k] * h
                        if S[k] == HIGH:
                            acc["hs_ch2"][k] += sq[k] * h
                            acc["hs2"][k] += sq[k] * h
                        elif S[k] == LOW:
                            acc["ls_ch2"][k] += sq[k] * h
                            acc["ls2"][k] += sq[k] * h
                        elif mid[k] < 0:
                            acc["hs_rev"][k] += -mid[k] * h
                            acc["hs2"][k] += sq[k] * h
                        elif mid[k] > 0:
                            acc["ls_rev"][k] += mid[k] * h
                            acc["ls2"][k] += sq[k] * h
                        acc["hs"][k] += hs[k] * h
                    hs_cross += np.outer(hs, hs) * h
                    hs_period += np.array(hs) * h
                    b_now = sum(hs)
                    bus += b_now * h
                    bus2 += b_now * b_now * h
                    t_meas += h
                i = new
                prev_v = vk
            prev_state = list(S)
        if meas:
            env_cross += np.outer(hs_period / T, hs_period / T) * T
    out = {k: x / t_meas for k, x in acc.items()}
    out.update(bus_mean=bus / t_meas, bus_ms=bus2 / t_meas, hs_cross=hs_cross / t_meas, period=t_meas)
    # the high sides' covariance split at the PWM period: the slow part (the period means' covariance) and the
    # rest, the PWM part (the variance within the periods)
    out["hs_slow"] = env_cross / t_meas - np.outer(out["hs"], out["hs"])
    out["hs_fast"] = out["hs_cross"] - env_cross / t_meas
    out["bus_ac_rms"] = math.sqrt(max(out["bus_ms"] - out["bus_mean"] ** 2, 0.0))
    # the reference's own energy balance: bus power − EMF power − copper − reverse conduction
    out["balance"] = v * out["bus_mean"] - out["ei"].sum() - r * out["i2"].sum() \
        - v_sd * (out["hs_rev"].sum() + out["ls_rev"].sum())
    return out


def _spec(**kw):
    """A bridge's settings for the drive alone: 48 V, 20 kHz, 50 A, a 20 mΩ / 40 µH / 5 V/krpm / 7 pole-pair motor."""
    from groundtruth.config.settings import BridgeSpec, LegSpec, MotorSpec
    motor = kw.pop("motor", MotorSpec("star", 0.02, 40e-6, 5.0, 7))
    b = BridgeSpec("b", "V", "GND", legs=[LegSpec(ph, f"PH{ph}", ["Q1"], ["Q2"]) for ph in "ABC"], v_bus=48.0,
                   f_pwm=20e3, i_phase=50.0, speed_rpm=3000.0, dead_time=500e-9, v_sd=2.0, rds_on=2e-3, motor=motor)
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _both(b, **ref_kw):
    """(GroundTruth's drive waveform, the reference at the same duty) for a bridge's settings."""
    from groundtruth.analysis.motor import drive_params, six_step
    w = six_step(b)
    p, _ = drive_params(b)
    n_s = w.n // 6 if w.f_e > 0 else 0
    ref = ref_drive(p.v, p.f_pwm, w.duty, p.dead, p.v_sd, p.r, p.l, p.e_ll, n_s, p.emf, p.pwm, p.synchronous,
                    **ref_kw)
    return w, ref


CASES = [
    ("H-PWM/L-ON sync, trapezoid, 3000 rpm", {}),
    ("H-PWM/L-ON not sync", {"synchronous": False}),
    ("H-ON/L-PWM sync", {"pwm": "hon_lpwm"}),
    ("bipolar sync", {"pwm": "bipolar"}),
    ("sinusoidal EMF", {"sine": True}),
    ("generating", {"mode": "generating"}),
    ("light load 5 A, not sync (discontinuous)", {"i_phase": 5.0, "synchronous": False}),
    ("locked rotor", {"speed_rpm": 0.0}),
]


def _case(kw):
    """A case's bridge settings (sine: the motor's EMF sinusoidal)."""
    from groundtruth.config.settings import MotorSpec
    kw = dict(kw)
    if kw.pop("sine", False):
        kw["motor"] = MotorSpec("star", 0.02, 40e-6, 5.0, 7, "sinusoidal")
    return _spec(**kw)


def drive():
    """The drive's currents against the time-stepped reference at the same duty."""
    for name, kw in CASES:
        print(f"Drive: {name}")
        w, ref = _both(_case(kw))
        s = w.stats()
        tag = name[:22]
        check("drive", f"{tag}: the reference's energy balance / bus power", 0.0,
              ref["balance"] / max(abs(48.0 * ref["bus_mean"]), 1.0), 5e-3, rel=False)
        check("drive", f"{tag}: bus mean current", ref["bus_mean"], s["bus_mean"], 0.01)
        check("drive", f"{tag}: bus AC RMS", ref["bus_ac_rms"], s["bus_ac_rms"], 0.01)
        check("drive", f"{tag}: phase RMS (A)", math.sqrt(ref["i2"][0]), s["phase_rms"][0], 0.01)
        check("drive", f"{tag}: high-side RMS (A)", math.sqrt(ref["hs2"][0]), s["hs_rms"][0], 0.01)
        check("drive", f"{tag}: low-side RMS (A)", math.sqrt(ref["ls2"][0]), s["ls_rms"][0], 0.01)


def caps():
    """The bus capacitors' ripple current: with every leg's switching current shared alike (one bus, one bank), a
    capacitor taking the share s carries s times the bus current's AC part."""
    from groundtruth.analysis.bridge import caps_at
    for name, kw in CASES[:4] + CASES[5:6]:
        print(f"Capacitors: {name}")
        b = _case(kw)
        w, ref = _both(b)
        got = caps_at(b, [{"name": "C", "legs": {"A": 1.0, "B": 1.0, "C": 1.0}}], w)[0]["rms_a"]
        check("caps", f"{name[:22]}: a capacitor taking all of it = the bus AC RMS", ref["bus_ac_rms"], got, 0.02)
    print("Capacitors: one leg's own capacitor (its share 1 of that leg, 0 of the others)")
    b = _case({})
    w, ref = _both(b)
    got = caps_at(b, [{"name": "C", "legs": {"A": 1.0, "B": 0.0, "C": 0.0}}], w)[0]["rms_a"]
    # its leg's PWM part (the variance within the PWM periods), and a third of a share of the bus's slow AC part
    own = math.sqrt(max(ref["hs_fast"][0, 0] + ref["hs_slow"].sum() / 9.0, 0.0))
    check("caps", "leg A's capacitor alone: leg A's PWM part + 1/3 of the bus's slow part", own, got, 0.03)
    env = math.sqrt(max(ref["hs_cross"][0, 0] - ref["hs"][0] ** 2, 0.0))
    check("caps", "  (leg A's whole AC RMS, its 120° envelope with it, is larger)", 1.0, float(env > 1.1 * got), 0.0,
          note=f"{env:.1f} A against {got:.1f} A")


def losses():
    """The loss inputs per leg (conduction's mean squares, reverse conduction's mean currents, the turn-ons' V·I
    and swings) against the reference's."""
    for name, kw in (CASES[0], CASES[1], CASES[2], CASES[3], CASES[5]):
        print(f"Losses: {name}")
        w, ref = _both(_case(kw))
        c, sw = w.conduction(), w.switching()
        tag = name[:22]
        for key, ref_key, what in (("hs_sq", "hs_ch2", "high-side channel mean square"),
                                   ("ls_sq", "ls_ch2", "low-side channel mean square"),
                                   ("hs_rev", "hs_rev", "high-side reverse mean current"),
                                   ("ls_rev", "ls_rev", "low-side reverse mean current")):
            tot_ref, tot = float(ref[ref_key].sum()), float(c[key].sum())
            if max(tot_ref, tot) > 1e-3:
                check("losses", f"{tag}: {what} (all legs)", tot_ref, tot, 0.02)
        for key, what in (("hs_vi", "high-side turn-ons' V·I"), ("ls_vi", "low-side turn-ons' V·I"),
                          ("node_dv2", "node swings squared")):
            tot_ref, tot = float(ref[key].sum()), float(sw[key].sum())
            if max(tot_ref, tot) > 1e-3 * 48.0 * 50.0 * 20e3 * (48.0 if key == "node_dv2" else 1.0) * 1e-3:
                check("losses", f"{tag}: {what} per second (all legs)", tot_ref, tot, 0.03)


def shunt():
    """A low-side shunt carries its leg's whole low-side current, chopped or not: its RMS on a simulated board
    against the drive's low-side RMS current."""
    from boardgen import bridge_board
    from conftest import make
    from groundtruth.analysis.detect import detect_bridges
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import MotorSpec, SettingsFile
    print("Shunt: a 2 mΩ shunt per low side, 48 V, 40 kHz, 30 A, 3000 rpm (simulated)")
    tmp = Path(tempfile.mkdtemp(prefix="bridge_audit_"))
    board, sc, _ = make(tmp, bridge_board(phase_shunts="ABC"), "br", nets=(), max_nodes=20000)
    (d,) = detect_bridges(board, sc.config)
    b = d.spec
    b.v_bus, b.f_pwm, b.i_phase, b.speed_rpm, b.c_oss = 48.0, 40e3, 30.0, 3000.0, 1e-9
    b.motor = MotorSpec("star", 0.05, 50e-6, 5.0, 7)
    sc.add_detected_bridges([b], d.models)
    sc.save()
    r = simulate(board, SettingsFile.load(sc.path).config)
    (br,) = r.bridges
    ls_rms = br.waveform.stats()["ls_rms"]
    got = {p["name"]: p["rms_a"] for p in br.rms["parts"]}
    for k, ref in enumerate(("R51", "R52", "R53")):
        check("shunt", f"{ref}: the shunt's RMS = leg {'ABC'[k]}'s low-side RMS", ls_rms[k], got.get(ref, 0.0), 0.03)
    loss = {x["ref"]: x["power_w"] for x in br.losses}
    check("shunt", "R51's loss = R·(low-side RMS)²", 2e-3 * ls_rms[0] ** 2, loss.get("R51", 0.0), 0.06)
    # the capacitors on the board: every one's ripple against its share of the bus AC current
    caps_rms = math.sqrt(sum(c["rms_a"] ** 2 for c in br.caps if not c["lisn"]))
    check("shunt", "the capacitors' ripple together (root sum square) <= the bus AC RMS", 1.0,
          float(caps_rms <= 1.02 * br.drive["bus_ac_rms"]), 0.0,
          note=f"{caps_rms:.2f} A against {br.drive['bus_ac_rms']:.2f} A")


def main():
    """Run the chosen areas; exit 1 when a case fails."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", default="drive,caps,losses,shunt")
    ap.add_argument("--json")
    args = ap.parse_args()
    areas = {"drive": drive, "caps": caps, "losses": losses, "shunt": shunt}
    for name in args.only.split(","):
        areas[name.strip()]()
    bad = [r for r in ROWS if not r["ok"]]
    print(f"\n{len(ROWS) - len(bad)} of {len(ROWS)} cases pass")
    for r in bad:
        print(f"  FAIL {r['area']}: {r['case']}")
    if args.json:
        Path(args.json).write_text(json.dumps(ROWS, indent=1, default=float), encoding="utf-8")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
