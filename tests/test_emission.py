"""Receiver bandwidth, report thinning, LISN impedances and conducted-emission limit lines."""
import math

import numpy as np
import pytest

from groundtruth.emi.conducted import BANDS, LISNS, conducted_arrays, conducted_table, limit_line
from groundtruth.emi.radiation import emission_table, receiver_bandwidth, receiver_sum


def test_harmonics_within_the_receiver_bandwidth_add_up():
    f = np.array([1.000e6, 1.004e6, 2.0e6])
    assert receiver_sum(f, np.ones(3), BANDS["cispr32"]).tolist() == [2.0, 2.0, 1.0]   # 9 kHz below 30 MHz
    f = np.array([100e6, 100.05e6, 2.0e9, 2.0004e9])
    assert receiver_sum(f, np.ones(4)).tolist() == [2.0, 2.0, 2.0, 2.0]              # 120 kHz / 1 MHz
    assert receiver_bandwidth([10e3, 1e6, 50e6], BANDS["mil461"]).tolist() == [1e3, 10e3, 100e3]


def test_report_rows_are_thinned_to_one_per_bin_and_per_decade():
    f = np.arange(3000, 30001) * 10e3                 # 10 kHz switching, 30-300 MHz: 27 001 harmonics
    rows = emission_table(f, np.full(len(f), 1e-3), np.full(len(f), 1e-6))
    assert len(rows) == pytest.approx((300e6 - 30e6) / 120e3, rel=0.01)
    rows = emission_table(f, np.full(len(f), 1e-3), np.full(len(f), 1e-6), per_decade=100)
    assert 95 <= len(rows) <= 102


def test_lisn_impedances_follow_the_nominal_networks():
    for f in (150e3, 1e6, 10e6):
        w = 2 * math.pi * f
        z50 = complex(5, w * 50e-6) * 50 / (complex(5, w * 50e-6) + 50)
        assert abs(LISNS["cispr32"].impedance(f)[0]) == pytest.approx(abs(z50), rel=1e-9)
        z5 = complex(0, w * 5e-6) * 50 / (complex(0, w * 5e-6) + 50)
        assert abs(LISNS["cispr25"].impedance(f)[0]) == pytest.approx(abs(z5), rel=1e-9)
    assert abs(LISNS["cispr32"].impedance(150e3)[0]) == pytest.approx(32.7, abs=0.2)
    assert abs(LISNS["cispr32"].impedance(30e6)[0]) == pytest.approx(50.0, abs=0.1)


def test_conducted_limit_lines():
    f = np.array([150e3, 300e3, 500e3, 1e6, 5e6, 10e6, 30e6, 40e6])
    b = limit_line(f, "cispr32", "B")
    assert b[:7] == pytest.approx([56.0, 56 - 10 * math.log(2) / math.log(0.5 / 0.15), 46, 46, 46, 50, 50])
    assert np.isnan(b[7])
    assert limit_line([150e3, 1e6], "cispr32", "A") == pytest.approx([66.0, 60.0])
    c25 = limit_line([0.2e6, 0.4e6, 1e6, 100e6], "cispr25", "5")
    assert c25[0] == 50 and np.isnan(c25[1]) and c25[2] == 34 and c25[3] == 18
    assert limit_line([1e6], "cispr25", "3")[0] == 50          # MW: 8 dB per class
    mil = limit_line([10e3, 100e3, 500e3, 5e6], "mil461", "basic")
    assert mil == pytest.approx([94.0, 94 - 34 / math.log10(50), 60.0, 60.0])
    assert limit_line([5e6], "mil461", "115V")[0] == 66.0


def test_conducted_reading_of_one_harmonic():
    """1 mA (peak) at 1 MHz through a 50 µH LISN: V = I·|Z| (no coupling network), read as RMS."""
    a = conducted_arrays([1e6], [1e-3], "cispr32", "B")
    v = 1e-3 * abs(LISNS["cispr32"].impedance(1e6)[0]) / math.sqrt(2)
    assert a["v_dbuv"][0] == pytest.approx(20 * math.log10(v / 1e-6))
    assert a["margin_db"][0] == pytest.approx(46.0 - a["v_dbuv"][0])
    rows = conducted_table(np.array([50e3, 1e6, 40e6]), np.array([1e-3, 1e-3, 1e-3]), "cispr32", "B")
    assert [r["f"] for r in rows] == [1e6]                     # outside 150 kHz-30 MHz: not reported
