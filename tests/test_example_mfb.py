"""examples/opamp_mfb_lowpass: a multiple-feedback 2nd-order low-pass drawn in KiCad (schematic and board), the
design of TI's OPA376 datasheet, §8.2 [TI SBOS406]: R1 590 Ω, R3 499 Ω, R4 2.94 kΩ, C2 39 nF, C5 1 nF; an inverting
gain of 5, a 25 kHz cutoff, a Chebyshev response with 3 dB of peaking. GroundTruth reads the circuit from the board
and its transfer function must be TI's Eq. 1 (an independent closed form)."""
import math
from pathlib import Path

import numpy as np
import pytest
import sympy as sp

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "opamp_mfb_lowpass"
R1, R3, R4, C2, C5 = sp.symbols("R1 R3 R4 C2 C5")
s = sp.Symbol("s")
VALUES = {R1: 590.0, R3: 499.0, R4: 2940.0, C2: 39e-9, C5: 1e-9}
# TI SBOS406G, Eq. 1: Output/Input(s) = −1/(R1·R3·C2·C5) / (s² + (s/C2)·(1/R1 + 1/R3 + 1/R4) + 1/(R3·R4·C2·C5))
TI_EQ1 = (-1 / (R1 * R3 * C2 * C5)) / (s ** 2 + (s / C2) * (1 / R1 + 1 / R3 + 1 / R4) + 1 / (R3 * R4 * C2 * C5))


@pytest.fixture(scope="module")
def path():
    """The example's input path (J1.2 through R1, R3 and U1 to J2.2) as detection finds it, and its circuit."""
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.analog_net import analog_network
    from groundtruth.analysis.detect import suggest_net
    from groundtruth.config.settings import SettingsFile
    board = load_board(EXAMPLE / "opamp_path_lp.kicad_pcb")
    cfg = SettingsFile.load(EXAMPLE / "groundtruth" / "opamp_path_lp.groundtruth.yaml").config
    found, why = suggest_net(board, cfg, "Net-(J1-Pin_2)")
    assert found is not None, why
    return found.spec, analog_network(board, cfg, found.spec, found.spec.receivers[0])


def test_detection_finds_the_path_through_the_op_amp(path):
    """From the input connector J1 to the output connector J2, through the op-amp U1 (recognised by its pin names)."""
    spec, net = path
    assert (spec.driver, spec.receivers) == ("J1.2", ["J2.2"])
    assert [name for name, _ in net.opamps] == ["U1"]
    assert {"R1", "R3", "R4", "C2", "C5"} <= set(net.parts)


def test_its_transfer_function_is_tis_equation_1(path):
    """The ideal circuit read from the board (each op-amp a nullor) gives exactly TI's Eq. 1 in the parts' names."""
    from groundtruth.analysis.symbolic import symbolic_transfer
    _, net = path
    tf = symbolic_transfer(net.schematic)
    assert tf is not None
    assert sp.simplify(tf.expr - TI_EQ1) == 0


def test_the_designs_gain_cutoff_and_peaking():
    """TI's Eq. 2 with the board's values: DC gain −R4/R1 = −4.98 (the design's 5 V/V), f0 = 1/(2π√(R3·R4·C2·C5)) =
    21.0 kHz; the response peaks ~3 dB above DC (the design's Chebyshev) and is back to the DC gain, then −3 dB below
    it, near the design's 25 kHz cutoff."""
    h = sp.lambdify(s, TI_EQ1.subs(VALUES), "numpy")
    assert complex(h(0)).real == pytest.approx(-2940 / 590)
    f0 = 1 / (2 * math.pi * math.sqrt(2940 * 499 * 39e-9 * 1e-9))
    assert f0 == pytest.approx(21.04e3, rel=1e-3)
    f = np.logspace(2, 6, 4001)
    db = 20 * np.log10(np.abs(h(2j * np.pi * f)) / abs(h(0)))
    assert 2.5 < db.max() < 3.5
    f_3db = f[np.argmax((f > f[np.argmax(db)]) & (db < -3.0))]
    assert 25e3 < f_3db < 35e3
