"""PDN impedance of a supply rail (analysis/pdn.py) against analytic results on a plane-pair board: the planes'
capacitance at the IC, one and four decaps (1/ωC below their resonance, their ESR at it), the source's resistance,
and the settings entry."""
import math

import numpy as np
import pytest

from boardgen import pdn_board
from conftest import make
from groundtruth.analysis.pdn import rail_voltage
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import ConfigError, PdnSpec, SettingsFile, config_from_data

EPS0 = 8.854187817e-12
C_PP = EPS0 * 4.5 * (40 - 0.6) * (30 - 0.6) * 1e-6 / 0.2e-3      # the VDD / GND planes, 0.2 mm FR4 apart: ~231 pF
FOUR = [("C1", "1u", (22.0, 15.0)), ("C2", "1u", (18.0, 15.0)), ("C3", "1u", (20.0, 17.0)), ("C4", "1u", (20.0, 13.0))]


def _run(tmp_path, name, spec, **board):
    """Simulate the rail of pdn_board(**board) with `spec`; (its PdnResult, the issues, the config)."""
    b, sc, _ = make(tmp_path, pdn_board(**board), name, nets=(), cavity_max_nodes=12000)
    sc.set_pdn([spec])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    r = simulate(b, cfg)
    assert len(r.pdn) == 1 and not r.pdn[0].error, r.pdn[0].error
    _run.boards[name] = b
    return r.pdn[0], r.issues, cfg


_run.boards = {}


def _at(port, f):
    """|Z| at the solve nearest to f."""
    fs = np.array(port["f"])
    return port["z_ohm"][int(np.argmin(np.abs(np.log(fs / f))))], float(fs[int(np.argmin(np.abs(np.log(fs / f))))])


@pytest.fixture(scope="module")
def solved(tmp_path_factory):
    """The rail with no decap, one 1 µF and four 1 µF (no source), and with the source J1 alone (50 mΩ)."""
    t = tmp_path_factory.mktemp("pdn")
    spec = PdnSpec("VDD", source="none", voltage=3.3, i_step=1.0)
    return {"bare": _run(t, "bare", spec),
            "one": _run(t, "one", spec, caps=[("C1", "1u", (22.0, 15.0))]),
            "four": _run(t, "four", spec, caps=FOUR),
            "source": _run(t, "source", PdnSpec("VDD", source="J1", vrm_r=0.05, vrm_l=0.0), source=True)}


def test_without_decaps_the_ic_sees_the_planes_capacitance(solved):
    """No decap, no source: between the IC's supply and ground pins only the planes' capacitance, ε0·εr·A/h; the
    port reads 1/(ωC) within 1 % from 100 kHz to 10 MHz (far below the planes' first resonance, ~1.8 GHz)."""
    p, issues, _ = solved["bare"]
    port = p.ports[0]
    assert port["part"] == "U1" and port["pins"] == ["U1.1"]
    for f in (1e5, 1e6, 1e7):
        z, f_ = _at(port, f)
        assert z == pytest.approx(1 / (2 * math.pi * f_ * C_PP), rel=0.01), f_
    assert any(i.check == "decap_missing" for i in issues)              # no decap within 3 mm of U1.1


def test_one_and_four_decaps_are_their_capacitance_then_their_esr(solved):
    """Below its series resonance a decap is its capacitance: one 1 µF reads 1/(ω(C + C_pp)), four 1/(ω(4C + C_pp))
    (1 %). At the resonance the impedance falls to the ESR (+ the copper's milliohms): four in parallel to about a
    quarter of one's. The decap holds the rail there (its share of the port current)."""
    one, issues_one, cfg = solved["one"]
    four, _, _ = solved["four"]
    m = cfg.model_for("C1")
    C, esr = m.get("C"), m.get("ESR")
    for f in (1e4, 1e5):
        z, f_ = _at(one.ports[0], f)
        assert z == pytest.approx(1 / (2 * math.pi * f_ * (C + C_PP)), rel=0.01)
        z, f_ = _at(four.ports[0], f)
        assert z == pytest.approx(1 / (2 * math.pi * f_ * (4 * C + C_PP)), rel=0.01)
    z1, z4 = min(one.ports[0]["z_ohm"]), min(four.ports[0]["z_ohm"])
    assert esr <= z1 < 2 * esr and esr / 4 <= z4 < esr / 2 and z4 < z1 / 2.5
    holds = {h["f"]: h for h in one.ports[0]["holds"]}
    h = holds[min(holds, key=lambda f: abs(math.log(f / 1e5)))]
    assert h["part"] == "C1" and h["share"] == pytest.approx(1.0, rel=0.01)
    assert not any(i.check == "decap_missing" for i in issues_one)       # C1 2.1 mm from U1.1
    assert 0.3 < one.ports[0]["l_nh"] < 5 and 0.1 < four.ports[0]["l_nh"] < one.ports[0]["l_nh"]


def test_the_source_holds_the_rail_below_the_decaps(solved):
    """The source J1 as 50 mΩ: at low frequency the IC sees it (plus the planes' few milliohms between them)."""
    p, _, _ = solved["source"]
    z, f = _at(p.ports[0], 3e4)
    assert z == pytest.approx(0.05, rel=0.1)
    assert "J1" in p.source and p.ports[0]["holds"][0]["part"] == "source J1"


def test_the_target_impedance_and_its_issue(solved):
    """Target = V · ripple / I_step (3.3 V, 5 %, 1 A: 165 mΩ); one 1 µF decap alone is far over it at low frequency:
    a pdn_impedance error naming the part that holds the rail there."""
    p, issues, _ = solved["one"]
    assert p.target_ohm == pytest.approx(3.3 * 0.05 / 1.0)
    w = p.ports[0]["worst"]
    assert w["margin_db"] < -20 and w["holds"] == "C1" and p.worst["part"] == "U1"
    err = [i for i in issues if i.check == "pdn_impedance"]
    assert err and err[0].severity == "error" and "U1" in err[0].message and "C1" in err[0].message


def test_settings_and_rail_names():
    """A pdn entry with units and a percentage, written back without its defaults; problems refused; a rail's
    voltage from its name."""
    c = config_from_data({"pdn": [{"rail": "+3V3", "ripple": "3%", "i_step": "500mA", "vrm_l": "10nH",
                                   "parts": "U1, U2", "check_to": "50MHz"}]}).pdn[0]
    assert (c.ripple, c.i_step, c.vrm_l, c.parts, c.check_to) == (pytest.approx(0.03), pytest.approx(0.5),
                                                                 pytest.approx(10e-9), ["U1", "U2"], pytest.approx(50e6))
    assert c.label == "+3V3" and c.vrm_r == pytest.approx(5e-3)
    with pytest.raises(ConfigError, match="ripple must be"):
        config_from_data({"pdn": [{"rail": "VDD", "ripple": 5}]})
    with pytest.raises(ConfigError, match="needs its rail"):
        config_from_data({"pdn": [{"ripple": "5%"}]})
    assert [rail_voltage(n) for n in ("+3V3", "+12V", "VDD_1V8", "/CPU/+5V_ISO", "GND")] == [3.3, 12.0, 1.8, 5.0, None]


def test_settings_round_trip(tmp_path):
    """set_pdn writes what was given (defaults left out) and reads back the same."""
    b, sc, _ = make(tmp_path, pdn_board(), "rt", nets=())
    spec = PdnSpec("VDD", name="core", voltage=1.2, ripple=0.03, i_step=2.0, source="none", parts=["U1"])
    sc.set_pdn([spec])
    sc.save()
    text = sc.path.read_text(encoding="utf-8")
    assert "rail: VDD" in text and "ripple: 3%" in text and "vrm_r" not in text
    assert SettingsFile.load(sc.path).config.pdn == [spec]


def test_the_reports_show_the_rail(solved):
    """The console report names the target, each IC's worst margin and peak; the HTML report has a Power integrity
    part with the rail's cards, its IC table and the impedance plot; the JSON report carries it."""
    import json
    from groundtruth.report import formats
    from groundtruth.report.html import render_html
    p, issues, cfg = solved["one"]
    text = " | ".join(formats.pdn_text(p))
    assert "target 165mΩ" in text and "U1 (1 supply pin(s))" in text and "held by C1" in text
    page = render_html(_run.boards["one"], issues, [], "simulate", config=cfg, pdn=[p])
    assert "Power integrity" in page and "Worst margin" in page and "C1 1u" in page and "data:image/png" in page
    data = json.loads(formats.to_json("b", issues, pdn=[p]))
    assert data["pdn"][0]["rail"] == "VDD" and data["pdn"][0]["ports"][0]["part"] == "U1"


def test_the_impedance_plot_has_its_phase(solved):
    """draw_pdn: |Z| on top and its phase below on the same frequency axis; the bare rail is the planes'
    capacitance, so its phase is −90° (within 2°) from 100 kHz to 10 MHz; with one decap it crosses 0° at the
    decap's series resonance and turns inductive above it."""
    from matplotlib.figure import Figure
    from groundtruth.report.plots import draw_pdn
    p, _, _ = solved["bare"]
    fig = Figure(figsize=(8, 5))
    draw_pdn(fig, p)
    ax_z, ax_p = fig.axes
    f, ph = (np.asarray(v) for v in ax_p.lines[0].get_data())
    band = (f >= 0.1) & (f <= 10)                       # MHz
    assert ph[band] == pytest.approx(-90.0, abs=2.0)
    one, _, _ = solved["one"]
    ph1 = np.asarray(one.ports[0]["phase_deg"])
    assert ph1.min() < -80 and ph1.max() > 45            # capacitive, then inductive above the resonance


def test_an_ic_cut_off_from_the_rail_is_left_out_and_said(tmp_path):
    """U2's supply pad has no via to the VDD plane: in the model it is not on the rail's copper (it would read only
    the stray capacitance of its pad, and slow every solve). It is left out before solving, with a note naming it;
    U1 is solved as before."""
    p, _, _ = _run(tmp_path, "cut", PdnSpec("VDD", source="none"), caps=[("C1", "1u", (22.0, 15.0))], cut_ic=True)
    assert [port["part"] for port in p.ports] == ["U1"]
    assert any(n.startswith("U2 (VDD): the pads do not reach the main copper") for n in p.notes), p.notes
    assert "1 IC port(s)" in " ".join(p.notes)


def test_the_pins_fine_model_makes_a_coarse_grid_converge(tmp_path, monkeypatch):
    """Above the decaps' resonance the impedance is the inductance at the IC's pins, which a coarse board-wide grid
    gets wrong (here -43 % at 100 MHz on 1.26 mm cells). The fine regional model around the IC (_local_runs) adds
    the difference: the coarse grid then reads the fine grid's |Z| (within 5 %; the correction itself measured
    within 1 % from 1.26 mm to 0.30 mm cells). Below, in the capacitors' range, nothing changes."""
    from groundtruth.analysis import pdn as P
    caps = [("C1", "100n", (22.0, 15.0)), ("C2", "100n", (18.0, 15.0)), ("C3", "100n", (20.0, 17.0)),
            ("C4", "100n", (20.0, 13.0))]

    def z_at(name, budget, correct):
        """|Z| at 1 and 100 MHz on a board-wide budget, with or without the correction."""
        if not correct:
            monkeypatch.setattr(P, "_local_runs", lambda *a, **k: (None, None, "off"))
        b, sc, _ = make(tmp_path, pdn_board(caps=caps), name, nets=(), cavity_max_nodes=budget)
        sc.set_pdn([PdnSpec("VDD", source="none")])
        sc.save()
        port = simulate(b, SettingsFile.load(sc.path).config).pdn[0].ports[0]
        monkeypatch.undo()
        f, z = np.log(port["f"]), np.log(port["z_ohm"])
        return [float(np.exp(np.interp(np.log(x), f, z))) for x in (1e6, 1e8)], port
    ref, _ = z_at("fine", 24000, False)
    raw, _ = z_at("raw", 4000, False)
    corr, port = z_at("corr", 4000, True)
    assert abs(raw[1] / ref[1] - 1) > 0.25                     # the coarse grid alone is far off at 100 MHz
    assert corr[1] == pytest.approx(ref[1], rel=0.05)
    assert corr[0] == pytest.approx(ref[0], rel=0.02) and port["l_correction_nh"] > 0.1
    assert port["local_pitch_mm"] < 0.3
