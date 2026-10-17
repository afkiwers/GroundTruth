"""Scratch settings for kraft_flux_mini_2: the three signal nets of HANDOFF, all detected power stages with the real
operating points (REAL, sources in SOURCES), Q601 as 6.5 mOhm so the input path reaches the stages, and the
conducted standard. Reads the board; writes only the settings given (never next to the board).

    .venv/Scripts/python tools/dev/real_settings.py BOARD OUT.groundtruth.yaml [--standard mil461]
        [--spread-mode random|triangle|off] [--spread-rate HZ] [--test]

--test uses the session-3 test values instead: 500 kHz, 24 V in, 5 A on SW1/SW2 (C_oss 600 pF), 0.3 A on the
other stages, Q601 5 mOhm. Input connector J101 (pin 2 V_BATT, pin 1 GND) in both cases.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.adapters.file_source import load_board  # noqa: E402
from groundtruth.analysis.detect import detect_converters  # noqa: E402
from groundtruth.config.settings import NetSpec, SettingsFile  # noqa: E402

NETS = [("/CPU/SPI_SCK", "U1102.12", "U1103.6", 20e6, 2e-9), ("/CPU/SPI_MOSI", "U1102.14", "U1103.5", 20e6, 2e-9),
        ("/CPU/SW_CLK", "J1101.4", "U1102.28", 10e6, 3e-9)]


# Real operating points (2026-10-04): f_sw, v_in, v_out, i_out, c_oss. Sources in SOURCES.
REAL = {
    "SW1": (787e3, 51.8, 28.0, 10.0, 616e-12),
    "SW2": (787e3, 51.8, 5.4, 5.0, 616e-12),
    "D501": (385e3, 51.8, 12.0, 0.22, None),
    "U503": (557e3, 12.0, 5.0, 0.14, None),
}
SOURCES = {
    "SW1": "user: 51.8 V in, Rail 1 28 V @ 10 A sustained; LTC7891/7890 f = 37 MHz / R_FREQ, R313 47k: 787 kHz; spread spectrum "
           "on, Burst Mode at light load; EPC2306 C_OSS 616 pF @ 50 V",
    "SW2": "user: Rail 2 5.4 V @ 5 A; otherwise as SW1",
    "D501": "LM5008 R_ON 249k: Fsw = Vout/(1.25e-10 R_ON) = 385 kHz; load estimated from the schematic: LT3970 input "
            "at its 350 mA design maximum + LTC7890 EXTVCC gate drive (~40 mA) = 0.22 A (typical ~0.11 A)",
    "U503": "LT3970-5 R_T 249k: 557 kHz; load estimated: 3V3 LDO ~50 mA + R1S-0505 (CAN) ~90 mA",
}


def main():
    """The command line (see the module): a settings file for the board at OUT with the signal nets and the detected
    power stages at their real (or --test) operating points."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("out", type=Path)
    ap.add_argument("--standard", default="mil461")
    ap.add_argument("--spread-mode", default="random", choices=("random", "triangle", "off"),
                    help="LTC7890 spread spectrum (0 to +20 %; the datasheet does not give the modulation)")
    ap.add_argument("--spread-rate", type=float, help="triangle modulation frequency, Hz")
    ap.add_argument("--test", action="store_true", help="the session-3 test operating points instead of the real ones")
    a = ap.parse_args()
    board = load_board(a.board)
    sc = SettingsFile.new(a.out, board)
    sc.save()
    sc = SettingsFile.load(a.out)
    sc.doc["analysis"]["conducted"] = a.standard
    comp = sc.doc["components"].get("Q601")
    if comp is not None:
        r = 0.005 if a.test else 0.0065
        comp["model"], comp["R"] = "resistor", r
        comp["note"] = ("test: surge stopper on (5 mOhm)" if a.test
                        else "surge stopper on: 6.5 mOhm (schematic note, VoltageSurgeSuppressor)")
    sc.save()
    sc = SettingsFile.load(a.out)
    sc.set_nets([NetSpec(n, d, [r], f, tr, 3.3) for n, d, r, f, tr in NETS])
    specs = []
    for det in detect_converters(board, sc.config):
        s = det.spec
        s.input_connector = ["J101.2", "J101.1"]
        s.reviewed = True
        if a.test:
            sw = "Rails/SW" in (s.switch_node or "")       # SW1, SW2 (GaN); not U503's "...-SW" net
            s.f_sw, s.v_in, s.i_out = 500e3, 24.0, 5.0 if sw else 0.3
            s.c_oss = 0.6e-9 if sw else None
        else:
            # by name, switch node or part: with GT labels (2026-10-07) the stages are conv1..conv4, but their switch
            # nodes still read .../SW1, .../SW2, Net-(D501-K), and the integrated stage is U503
            key = next(k for k in REAL if k in s.name or k in (s.switch_node or "") or k in s.switch + s.low_side)
            s.f_sw, s.v_in, s.v_out, s.i_out, s.c_oss = REAL[key]
            s.note = f"operating point: {SOURCES[key]}"
            if key in ("SW1", "SW2") and a.spread_mode != "off":
                s.spread, s.spread_mode, s.spread_rate = 0.2, a.spread_mode, a.spread_rate
        specs.append(s)
    sc.set_converters(specs)
    sc.save()
    print("wrote", a.out, [n[0] for n in NETS], [s.name for s in specs])


if __name__ == "__main__":
    main()
