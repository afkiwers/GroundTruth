"""MIL-STD-461G CE101 (power-lead current, 30 Hz-10 kHz): the limits as the standard's figures draw them, the lead
the board sees below 10 kHz, and the current reaching it (a capacitor against the lead, analytically)."""
import math

import numpy as np
import pytest

from boardgen import hot_loop_board
from groundtruth.analysis.power import DC_F
from groundtruth.analysis.simulate import MOUNT_L
from groundtruth.config.settings import ConfigError, ConverterSpec, config_from_data
from groundtruth.emi.conducted import (CE101_LEAD, LISNS, Ce101, ce101_arrays, ce101_limit, lisn_loop_impedance)
from test_power import SPEC, run


def test_the_limits_follow_figures_ce101_1_and_ce101_4():
    """CE101-4 (Army aircraft, Navy ASW): curve #2 (28 V or below) 100 dBµA from 30 Hz to 1 kHz, then -20 dB per
    decade to 80 dBµA at 10 kHz; curve #1 (above 28 V) 10 dB higher. CE101-1 (ships, DC): 95 dBµA to 2.6 kHz, 76
    at 10 kHz, relaxed by 20·log(I/3) between 3 and 185 A and by 35 dB above. No limit outside 30 Hz-10 kHz."""
    f = np.array([20.0, 30.0, 1e3, 3e3, 10e3, 20e3])
    low = ce101_limit(f, Ce101("aircraft", 28.0))
    assert np.isnan(low[0]) and np.isnan(low[-1])
    assert low[1:5] == pytest.approx([100.0, 100.0, 100.0 - 20 * math.log10(3), 80.0])
    assert ce101_limit(f, Ce101("aircraft", 48.0))[1:5] == pytest.approx(low[1:5] + 10.0)
    ships = ce101_limit(np.array([30.0, 2.6e3, 10e3]), Ce101("ships", 48.0, 2.0))
    assert ships == pytest.approx([95.0, 95.0, 76.0])
    assert ce101_limit([1e3], Ce101("ships", 48.0, 30.0))[0] == pytest.approx(95.0 + 20.0)
    assert ce101_limit([1e3], Ce101("ships", 48.0, 300.0))[0] == pytest.approx(95.0 + 35.0)
    assert "curve #2" in Ce101("aircraft", 28.0).describe() and "curve #1" in Ce101("aircraft", 28.1).describe()


def test_the_probe_reads_one_harmonic_per_bandwidth_in_rms_dbua():
    """2 kHz harmonics are 20 bandwidths apart (100 Hz above 1 kHz): each reads alone, its peak / sqrt 2 in dBµA;
    10 Hz-apart lines within the 10 Hz band below 1 kHz add up."""
    a = ce101_arrays([2e3, 4e3], [1e-3, 2e-3], Ce101("aircraft", 28.0))
    assert a["i_dbua"] == pytest.approx(20 * np.log10(np.array([1e-3, 2e-3]) / math.sqrt(2) / 1e-6))
    assert a["margin_db"] == pytest.approx(a["limit_dbua"] - a["i_dbua"])
    close = ce101_arrays([500.0, 504.0], [1e-3, 1e-3], Ce101("aircraft", 28.0))
    assert close["i_dbua"][0] == pytest.approx(20 * math.log10(2e-3 / math.sqrt(2) / 1e-6))


def test_below_10_khz_the_lisn_branch_is_the_ce101_lead():
    """The LISN branch is the CE101 lead (50 µH + 50 mΩ to a stiff source, and the 0.25 µF port) from 15 Hz to just
    below 10 kHz, CE102's network from 10 kHz, and stays CE102's at the DC solves' 10 Hz (where the series 8 µF keeps
    the DC paths out of the LISN)."""
    z = lisn_loop_impedance("mil461")
    for f, tol in ((30.0, 0.01), (1e3, 0.01), (9.9e3, 0.05)):
        assert z(f) == pytest.approx(complex(2 * CE101_LEAD.impedance(f)[0]))
        w = 2 * math.pi * f
        assert abs(z(f)) == pytest.approx(2 * abs(complex(0.05, w * 50e-6)), rel=tol)   # the port: 3 % at 9.9 kHz
    assert z(10e3) == pytest.approx(complex(2 * LISNS["mil461"].impedance(10e3)[0]))
    assert z(DC_F) == pytest.approx(complex(2 * LISNS["mil461"].impedance(DC_F)[0]))
    assert abs(z(DC_F)) > 1e3


def test_ce101_needs_a_known_platform_and_a_conducted_model():
    with pytest.raises(ConfigError, match="analysis.ce101 must be one of"):
        config_from_data({"analysis": {"ce101": "boat"}})
    with pytest.raises(ConfigError, match="needs a conducted model"):
        config_from_data({"analysis": {"ce101": "aircraft", "conducted": "none"}})
    a = config_from_data({"analysis": {"ce101": "ships", "conducted": "mil461", "ce101_load_a": "40A"}}).analysis
    assert a.ce101_spec(48.0, 5.0) == Ce101("ships", 48.0, 40.0)
    assert config_from_data({}).analysis.ce101_spec(48.0, 5.0) is None


def test_a_slow_converters_lead_current_is_the_capacitor_against_the_lead_divider(tmp_path):
    """A stage switching at 2 kHz has harmonics in CE101's band. Its loop current divides between C1 (470 µF) and
    the leads (2 × 50 µH to stiff sources): line / loop = |Z_C1 / (Z_C1 + 2 Z_lead)| at the CE101 solves (the copper
    adds milliohms against the ~0.1-1 Ω of both branches). Near the series resonance of the leads' 100 µH with C1
    (~730 Hz, in band: why CE101 is solved 4 times a decade and refined where the transfer bends) the reactances
    cancel and the resistances (the leads' 2 × 50 mΩ, C1's ESR, the copper's milliohms) set the peak: there the
    solved peak is checked to sit at the resonance and above 1.5 (more lead current than loop current)."""
    spec = ConverterSpec(**{**SPEC.__dict__, "input_connector": ["J1.1", "J1.2"], "f_sw": 2e3})
    r, _, sc = run(tmp_path, hot_loop_board(20.0, caps=(("C1", "470u", "end", 0.0),), connector=True), "ce101",
                   spec=spec, conducted="mil461", ce101="aircraft")
    assert r.ce101_limit.startswith("MIL-STD-461G CE101") and "curve #2" in r.ce101_limit     # 12 V input
    m = sc.config.model_for("C1")
    tf = {t["f"]: t["ratio"] for t in r.ce101_transfer}
    assert min(tf) == pytest.approx(30.0) and max(tf) == pytest.approx(9.9e3) and 11 <= len(tf) <= 20
    f_res = 1 / (2 * math.pi * math.sqrt(100e-6 * m.get("C")))
    for f, ratio in tf.items():
        w = 2 * math.pi * f
        z_c = complex(m.get("ESR"), w * (m.get("ESL") + MOUNT_L) - 1 / (w * m.get("C")))
        z_lead = 2 * CE101_LEAD.impedance(f)[0]
        if abs(math.log(f / f_res)) > math.log(1.6):
            assert ratio == pytest.approx(abs(z_c / (z_c + z_lead)), rel=0.05), f
    peak = max(tf, key=tf.get)
    assert abs(math.log(peak / f_res)) < math.log(1.15) and tf[peak] > 1.5, (peak, f_res, tf[peak])
    w = 2 * math.pi * f_res                     # at the resonance itself: |Z_C1| / (R_C1 + R_leads)
    z_c = complex(m.get("ESR"), w * (m.get("ESL") + MOUNT_L) - 1 / (w * m.get("C")))
    assert tf[peak] == pytest.approx(abs(z_c / (z_c + 2 * CE101_LEAD.impedance(f_res)[0])), rel=0.25)
    # CE102's transfer has none of the CE101 solves (it keeps the stage's own f_sw solve, for the capacitor sharing)
    assert not {t["f"] for t in r.input_transfer} & set(tf)
    assert r.ce101 and all(30.0 <= row["f"] <= 10e3 for row in r.ce101)
    assert all(row["margin_db"] == pytest.approx(row["limit_dbua"] - row["i_dbua"]) for row in r.ce101)
    assert r.ce101_worst == min(r.ce101, key=lambda row: row["margin_db"])


def test_mil_std_461h_takes_the_ships_curve_to_20_khz_and_caps_its_relaxation_at_35_db():
    """MIL-STD-461H (17 April 2026) against 461G, as both standards print it [MIL-STD-461G] [MIL-STD-461H]: CE101-1
    (ships, DC) is the same line, 95 dBµA to 2.6 kHz, continued from 10 kHz (76 dBµA in 461G) to 66 dBµA at 20 kHz
    (5.4.1: ships and submarines 30 Hz to 20 kHz); its relaxation is "the lesser of 20 log(I/3) or 35 dB" (461G:
    20 log(I/3) up to 185 A, then 35 dB). Aircraft (CE101-4) stay at 30 Hz-10 kHz, unchanged."""
    f = np.array([30.0, 2.6e3, 10e3, 20e3, 25e3])
    h = ce101_limit(f, Ce101("ships", 48.0, 2.0, "H"))
    line_10k = 95.0 + (66.0 - 95.0) * math.log(10 / 2.6) / math.log(20 / 2.6)
    assert h[:4] == pytest.approx([95.0, 95.0, line_10k, 66.0]) and np.isnan(h[4])
    assert abs(line_10k - 76.0) < 0.2                      # the same line as 461G's to 10 kHz, within the drawing
    g = ce101_limit(f, Ce101("ships", 48.0, 2.0, "G"))
    assert g[:3] == pytest.approx([95.0, 95.0, 76.0]) and np.isnan(g[3])
    air = ce101_limit(f, Ce101("aircraft", 28.0, 0.0, "H"))
    assert air[2] == pytest.approx(80.0) and np.isnan(air[3])
    assert Ce101("ships", 48.0, 0.0, "H").range == (30.0, 20e3) and Ce101("aircraft", 48.0, 0.0, "H").range[1] == 10e3
    # between 168.7 A (20 log(I/3) = 35) and 185 A the two revisions differ: 461H caps at 35 dB
    assert Ce101("ships", 48.0, 180.0, "G").relax_db == pytest.approx(20 * math.log10(60.0))
    assert Ce101("ships", 48.0, 180.0, "H").relax_db == pytest.approx(35.0)
    assert Ce101("ships", 48.0, 30.0, "H").relax_db == pytest.approx(20.0)
    assert Ce101("ships", 48.0, 2.0, "H").describe().startswith("MIL-STD-461H CE101 (Figure CE101-1, ships DC to 20")
    assert Ce101("ships", 48.0, 2.0).describe().startswith("MIL-STD-461G CE101 (Figure CE101-1, ships DC to 10")


def test_the_revision_is_the_users_setting():
    """analysis.mil461: G (the default) or H, written as G, H, 461H or MIL-STD-461H; anything else is refused."""
    assert config_from_data({}).analysis.mil461 == "G"
    for text in ("H", "h", "461H", "MIL-STD-461H"):
        assert config_from_data({"analysis": {"mil461": text}}).analysis.mil461 == "H", text
    with pytest.raises(ConfigError, match="analysis.mil461 must be one of G, H"):
        config_from_data({"analysis": {"mil461": "461J"}})
    a = config_from_data({"analysis": {"ce101": "ships", "conducted": "mil461", "mil461": "H"}}).analysis
    assert a.ce101_spec(48.0, 5.0) == Ce101("ships", 48.0, 5.0, "H")


def _relaxed(r) -> float:
    """The relaxation a result's CE101 limit names (0 without one)."""
    import re
    m = re.search(r"relaxed ([0-9.]+) dB", r.ce101_limit)
    return float(m.group(1)) if m else 0.0


def test_under_461h_a_ships_converter_switching_at_12_khz_is_read_to_20_khz(tmp_path):
    """A stage switching at 12 kHz: under 461G no harmonic is in CE101's 30 Hz-10 kHz (a note says so); under 461H
    ships' CE101 reaches 20 kHz, so its fundamental is read, from the conducted band's solves (the LISN as specified
    at the input [A34]; two extra ones at 14.1 and 20 kHz). There the line current per A of loop current is C1
    against the two LISNs, |Z_C1 / (Z_C1 + 2 Z_LISN)| (the copper adds milliohms)."""
    spec = ConverterSpec(**{**SPEC.__dict__, "input_connector": ["J1.1", "J1.2"], "f_sw": 12e3})
    board = hot_loop_board(20.0, caps=(("C1", "470u", "end", 0.0),), connector=True)
    g, _, _ = run(tmp_path, board, "ce101g", spec=spec, conducted="mil461", ce101="ships", mil461="G")
    assert g.ce101 == [] and any(n.startswith("CE101: no switching harmonic below 10 kHz") for n in g.notes)
    h, _, sc = run(tmp_path, board, "ce101h", spec=spec, conducted="mil461", ce101="ships", mil461="H")
    assert h.ce101_limit.startswith("MIL-STD-461H CE101 (Figure CE101-1, ships DC to 20 kHz")
    assert [round(row["f"]) for row in h.ce101] == [12000]
    row = h.ce101[0]
    expect = 95.0 + (66.0 - 95.0) * math.log(12 / 2.6) / math.log(20 / 2.6)
    assert row["limit_dbua"] == pytest.approx(expect + _relaxed(h)) and row["margin_db"] is not None
    tf = {round(t["f"]): t["ratio"] for t in h.input_transfer}
    assert {14142, 20000} <= set(tf)
    m = sc.config.model_for("C1")
    for f in (14142.0, 20000.0):
        w = 2 * math.pi * f
        z_c = complex(m.get("ESR"), w * (m.get("ESL") + MOUNT_L) - 1 / (w * m.get("C")))
        z_lisn = 2 * LISNS["mil461"].impedance(f)[0]
        assert tf[round(f)] == pytest.approx(abs(z_c / (z_c + z_lisn)), rel=0.05), f


def test_a_fast_converter_has_no_ce101_harmonics_and_says_so(tmp_path):
    """At 200 kHz every harmonic is above 10 kHz: no CE101 solves, no rows, a note naming what CE101 would see."""
    spec = ConverterSpec(**{**SPEC.__dict__, "input_connector": ["J1.1", "J1.2"]})
    r, _, _ = run(tmp_path, hot_loop_board(20.0, caps=(("C1", "1u", "end", 0.0),), connector=True), "fast",
                  spec=spec, conducted="mil461", ce101="aircraft")
    assert r.ce101 == [] and r.ce101_transfer == []
    assert any(n.startswith("CE101: no switching harmonic below 10 kHz") for n in r.notes)


def test_the_built_in_re102_curves_follow_the_figures():
    """MIL-STD-461G RE102 by name (`radiated_limit: re102_ground_army`): the printed breakpoints, straight on a log
    frequency axis between them (the rise above 100 MHz ~20 dB/decade), no limit outside the curve (ground from 2
    MHz, Fig. RE102-4), 1 m and Table II bandwidths; an unknown name lists the built-in ones."""
    from groundtruth.emi.radiation import RE102_LIMITS, limit_range, limits
    a = config_from_data({"analysis": {"radiated_limit": "RE102_Ground_Army"}}).analysis
    lim = a.limit_spec()
    assert lim is RE102_LIMITS["re102_ground_army"] and lim.distance_m == 1.0 and lim.bands == "mil461"
    v = limits(np.array([1e6, 2e6, 30e6, 100e6, 1e9, 18e9, 20e9]), lim)[0]
    assert np.isnan(v[0]) and np.isnan(v[-1])
    assert v[1:4] == pytest.approx([24.0, 24.0, 24.0])
    assert v[4] == pytest.approx(24.0 + 45.0 / math.log10(180.0), abs=0.01)       # 1 GHz: ~44 dBµV/m
    assert v[5] == pytest.approx(69.0)
    assert limit_range(lim) == (2e6, 18e9)
    air = limits(np.array([10e3, 2e6, 1e9]), RE102_LIMITS["re102_helicopter"])[0]
    assert air == pytest.approx([60.0, 24.0, 24.0 + 45.0 / math.log10(180.0)], abs=0.01)
    fixed = limits(np.array([10e3, 2e6]), RE102_LIMITS["re102_aircraft_external"])[0]     # from 2 MHz
    assert np.isnan(fixed[0]) and fixed[1] == pytest.approx(24.0)
    ship = limits(np.array([10e3, 100e6, 18e9]), RE102_LIMITS["re102_ship_below_deck"])[0]
    assert ship == pytest.approx([90.0, 56.0, 102.0])
    assert all(c.points[-1][0] == 18e9 for c in RE102_LIMITS.values())
    with pytest.raises(ConfigError, match="not a built-in limit.*re102_ground_army"):
        config_from_data({"analysis": {"radiated_limit": "re102_ground"}})
