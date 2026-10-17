import pytest

from groundtruth.components.defaults import suggest_model
from groundtruth.components.models import ComponentModel
from groundtruth.components.values import parse_impedance_at, parse_value
from groundtruth.core.board import Footprint


@pytest.mark.parametrize("text,value", [
    ("4k7", 4700), ("33R", 33), ("1R5", 1.5), ("0R", 0), ("0", 0), ("10k0", 10_000), ("2M2", 2.2e6),
    ("100n", 1e-7), ("100nF", 1e-7), ("22u0", 22e-6), ("10uF/16V", 1e-5), ("4.7µF", 4.7e-6), ("1.5pF", 1.5e-12),
    ("100mR", 0.1), ("47 Ohm", 47), ("1e-6", 1e-6), ("4,7k", 4700),
])
def test_parse_value(text, value):
    assert parse_value(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["DNP", "NC", "", "~", "abc"])
def test_parse_value_none(text):
    assert parse_value(text) is None


def test_ferrite_notation():
    assert parse_impedance_at("600R@100MHz") == pytest.approx((600, 1e8))
    assert parse_impedance_at("120Ω @ 100 MHz") == pytest.approx((120, 1e8))


def fp(ref, value, lib="Resistor_SMD:R_0402_1005Metric", dnp=False):
    return Footprint(ref, value, lib, (0, 0), 0, "F.Cu", dnp=dnp)


@pytest.mark.parametrize("ref,value,lib,kind,params", [
    ("R1", "0R", "Resistor_SMD:R_0402", "short", {}),
    ("R2", "33R", "Resistor_SMD:R_0402", "resistor", {"R": 33}),
    ("C1", "100n", "Capacitor_SMD:C_0603_1608Metric", "cap", {"C": 1e-7, "ESL": 0.6e-9}),
    ("C2", "47u", "Capacitor_THT:CP_Elec_10x10.5", "cap", {"ESR": 0.1, "ESL": 3e-9}),
    ("FB1", "600R@100MHz", "Inductor_SMD:L_0603", "ferrite", {"L": 600 / (2 * 3.141592653589793 * 1e8)}),
    ("L1", "10u", "Inductor_SMD:L_1210", "inductor", {"L": 1e-5}),
    ("U1", "STM32", "Package_QFP:LQFP-48", "open", {}),
    ("J1", "USB_C", "Connector:USB_C", "open", {}),
    ("NT1", "", "NetTie:NetTie-2_SMD_Pad0.5mm", "short", {}),
])
def test_suggestions(ref, value, lib, kind, params):
    m = suggest_model(fp(ref, value, lib))
    assert m.kind == kind
    for k, v in params.items():
        assert m.params[k] == pytest.approx(v)


def test_dnp_is_open():
    assert suggest_model(fp("R5", "0R", dnp=True)).kind == "open"


def test_model_impedance_and_paths():
    cap = ComponentModel("cap", {"C": 100e-9, "ESR": 0.02, "ESL": 0.5e-9})
    srf = 1 / (2 * 3.141592653589793 * (100e-9 * 0.5e-9) ** 0.5)
    assert abs(cap.impedance(srf).imag) < 1e-6 and cap.impedance(srf).real == pytest.approx(0.02)
    assert ComponentModel("open").impedance(1e6) is None
    assert ComponentModel("resistor", {"R": 33}).passes_signal()
    assert not ComponentModel("resistor", {"R": 10e3}).passes_signal()   # pull-up, not a series part
    assert not cap.passes_signal()
    with pytest.raises(ValueError):
        ComponentModel("banana")
