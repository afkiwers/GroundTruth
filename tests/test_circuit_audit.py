"""Audit round 12, the circuit side: a rail's voltage from its name (C-1), a part's value with a rating after a
space (C-2), crosstalk at an unterminated receiver (C-3), an inverting gain block's bandwidth (C-4)."""
import numpy as np
import pytest

from groundtruth.analysis.pdn import rail_voltage
from groundtruth.components.models import ComponentModel
from groundtruth.components.values import parse_value


@pytest.mark.parametrize("name, volts", [("+3.3V", 3.3), ("+1.8V", 1.8), ("+2.5V", 2.5), ("+1.2V", 1.2), ("+0.9V", 0.9),
                                         ("+12.5V", 12.5), ("+5.0V", 5.0), ("+3V3", 3.3), ("+12V", 12.0),
                                         ("VDD_1V8", 1.8), ("/MCU/+5V_ISO", 5.0), ("+5V0", 5.0), ("GND", None),
                                         ("/Power/SW1", None)])
def test_C_1_a_rails_voltage_reads_both_ways_of_writing_it(name, volts):
    """KiCad's power symbols write "+3.3V" as well as "+3V3": the decimal form read its digits after the point
    ("+1.8V" as 8 V, "+5.0V" as 0 V), which moved the PDN target and the capacitors' voltages by that factor."""
    assert rail_voltage(name) == (pytest.approx(volts) if volts is not None else None)


@pytest.mark.parametrize("text, value", [
    ("1u 50V", 1e-6), ("1u 25V", 1e-6), ("1n 2kV", 1e-9), ("10u 6.3V", 10e-6), ("100n 50V", 100e-9),
    ("1k 5%", 1e3), ("1k 1%", 1e3), ("10k 1%", 10e3), ("330R 1/4W", 330.0), ("0R 0603", 0.0), ("0R 1%", 0.0),
    # the unit after a space still belongs to the value
    ("4.7 k", 4700.0), ("10 kOhm", 10e3), ("2.2 uF", 2.2e-6), ("10 Ohms/1%", 10.0),
    # as before
    ("10uF 16V", 10e-6), ("100nF/50V X7R", 100e-9), ("100n X7R 50V", 100e-9), ("4k7", 4700.0), ("1R5", 1.5),
    ("22u0", 22e-6), ("4,7k", 4700.0), ("1e-6", 1e-6), ("100n", 100e-9),
    # shouted prefixes and "meg"
    ("10UF", 10e-6), ("100NF", 100e-9), ("100PF", 100e-12), ("1meg", 1e6), ("2.2MEG", 2.2e6)])
def test_C_2_a_rating_or_tolerance_after_a_space_is_not_part_of_the_value(text, value):
    """"1u 50V" read as the infix form 1u50 = 1.5 µF, "10k 1%" as 10.1 kΩ, "0R 0603" as 0.0603 Ω (every space was
    removed before the value was read)."""
    assert parse_value(text) == pytest.approx(value, rel=1e-9, abs=1e-18)


def test_C_4_an_inverting_gain_blocks_bandwidth_is_gbw_over_one_plus_its_gain():
    """A voltage-feedback stage's bandwidth is GBW over its noise gain: the gain when non-inverting, 1 + |gain|
    when inverting [Franco 2015]: an inverter of -1 has GBW/2 (it read GBW), -10 has GBW/11."""
    def corner(gain):
        """The gain block's -3 dB frequency with GBW 1 MHz."""
        return ComponentModel("amplifier", {"gain": gain, "gbw": 1e6}).amp_corner()
    assert corner(-1.0) == pytest.approx(500e3) and corner(-2.0) == pytest.approx(1e6 / 3)
    assert corner(-10.0) == pytest.approx(1e6 / 11) and corner(10.0) == pytest.approx(100e3)
    assert corner(1.0) == pytest.approx(1e6) and corner(0.5) == pytest.approx(1e6)
    m = ComponentModel("amplifier", {"gain": -1.0, "gbw": 1e6})
    assert abs(m.amp_gain(500e3)) == pytest.approx(1 / np.sqrt(2))
    assert ComponentModel("amplifier", {"gain": -1.0, "bandwidth": 2e5, "gbw": 1e6}).amp_corner() == 2e5


def test_C_3_crosstalk_at_an_unterminated_receiver_against_a_coupled_line_in_time():
    """Two coupled lossless lines (L 300, Lm 60 nH/m, C 110, Cm 10 pF/m, 100 mm, a 0.3 ns edge), solved in the
    frequency domain by their modes: with every end matched the receiver sees the closed forms' FEXT; with open
    receivers and drivers of half the line's impedance it sees more, and the estimate for an unterminated
    receiver, 2·max(FEXT, DRIVER_REFLECTION·NEXT), covers it."""
    from groundtruth.analysis.si import DRIVER_REFLECTION
    L, Lm, C, Cm, length, tr = 300e-9, 60e-9, 110e-12, 10e-12, 0.1, 0.3e-9
    z0 = np.sqrt(L / C)
    td = length * np.sqrt(L * C)
    kb = (Lm / L + Cm / C) / 4
    kf = (Cm / C - Lm / L) / 2
    nxt, fxt = kb * min(1.0, 2 * td / tr), abs(kf) * td / tr

    def far_end(r_drv, g_load):
        """The victim's far-end peak for a unit ramp on the aggressor, per unit of its settled swing."""
        n, dt = 4096, tr / 40
        f = np.fft.rfftfreq(n, dt)
        t = np.arange(n) * dt
        src = np.fft.rfft(np.clip(t / tr, 0, 1) - np.clip((t - n * dt / 2) / tr, 0, 1))     # up, later down again
        out = np.zeros(len(f), complex)
        for k, fk in enumerate(f[1:], start=1):
            w = 2 * np.pi * fk
            peak = []
            # even and odd modes of the symmetric pair: each a single line
            for sign in (1, -1):
                lm, cm = L + sign * Lm, C - sign * Cm
                zm, beta = np.sqrt(lm / cm), w * np.sqrt(lm * cm)
                yl = g_load + 1j * w * 3e-12 * (g_load == 0)
                zl = 1 / yl if yl != 0 else 1e12
                c, s_ = np.cos(beta * length), 1j * np.sin(beta * length)
                # the mode's far-end voltage for half the source on it: V_far = V_s / (A + B/Z_L + R(C + D/Z_L))
                a, b, cc, d = c, s_ * zm, s_ / zm, c
                peak.append(0.5 * src[k] / (a + b / zl + r_drv * (cc + d / zl)))
            out[k] = peak[0] - peak[1]                 # the victim: even minus odd
        v = np.fft.irfft(out, n)
        return float(np.max(np.abs(v[: n // 2])))
    matched = far_end(z0, 1 / z0)
    assert matched == pytest.approx(fxt / 2, rel=0.15)           # half the source's swing is on a matched line
    open_rx = far_end(z0 / 2, 0.0) / (1.0)                        # the aggressor's settled swing is the source's
    rx = 2.0 * max(fxt, DRIVER_REFLECTION * nxt)
    assert open_rx > 1.3 * fxt and rx >= open_rx * 0.95


def test_a_signal_named_after_its_supply_domain_is_not_a_supply():
    """Crosstalk victims and pair detection leave out supplies and grounds only: a voltage in the name counts as a
    supply when a power pin or a pour is on the net or the name starts as a supply's does; PG_3V3, EN_1V8 and
    ADC_12V_MON are signals (they were all left out as supplies before)."""
    from boardgen import BoardGen
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.si import supply_nets
    import tempfile
    from pathlib import Path
    g = BoardGen(layers=2, size=(40.0, 20.0))
    names = ["+3V3", "VDD_1V8", "5V_ISO", "PG_3V3", "EN_1V8", "ADC_12V_MON", "GND", "SPI_SCK", "PWR_1V2"]
    for k, net in enumerate(names):
        g.footprint(f"R{k + 1}", "R", "Resistor_SMD:R_0603_1608Metric", (5.0 + 3 * k, 10.0),
                    [{"number": 1, "at": (-0.8, 0.0), "net": net, "size": (0.8, 0.9)},
                     {"number": 2, "at": (0.8, 0.0), "net": "GND", "size": (0.8, 0.9)}])
    g.zone("PWR_1V2", "B.Cu", [(2.0, 2.0), (38.0, 2.0), (38.0, 6.0), (2.0, 6.0)])     # a pour: a supply
    board = load_board(g.write(Path(tempfile.mkdtemp()) / "b.kicad_pcb"))
    got = supply_nets(board)
    assert {"+3V3", "VDD_1V8", "5V_ISO", "GND", "PWR_1V2"} <= got
    assert not ({"PG_3V3", "EN_1V8", "ADC_12V_MON", "SPI_SCK"} & got)
