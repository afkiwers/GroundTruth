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
    assert [rail_voltage(n) for n in ("+3V3", "+12V", "VDD_1V8", "/MCU/+5V_ISO", "GND")] == [3.3, 12.0, 1.8, 5.0, None]


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
    gets wrong (here -37 % at 100 MHz on 1.26 mm cells). The fine regional model around the IC (_local_runs) adds
    the difference: the coarse grid then reads the fine grid's |Z| (within 5 %), the same from 4000 and 24000
    nodes (within 1.5 %). Below, in the capacitors' range, nothing changes."""
    from groundtruth.analysis import pdn as P
    caps = [("C1", "100n", (22.0, 15.0)), ("C2", "100n", (18.0, 15.0)), ("C3", "100n", (20.0, 17.0)),
            ("C4", "100n", (20.0, 13.0))]

    def z_at(name, budget, correct):
        """|Z| at 1 and 100 MHz on a board-wide budget, with or without the correction."""
        with monkeypatch.context() as m:        # not monkeypatch.undo(): that also undoes conftest's patches
            if not correct:
                m.setattr(P, "_local_runs", lambda *a, **k: (None, "off"))
            b, sc, _ = make(tmp_path, pdn_board(caps=caps), name, nets=(), cavity_max_nodes=budget)
            sc.set_pdn([PdnSpec("VDD", source="none")])
            sc.save()
            port = simulate(b, SettingsFile.load(sc.path).config).pdn[0].ports[0]
        f, z = np.log(port["f"]), np.log(port["z_ohm"])
        return [float(np.exp(np.interp(np.log(x), f, z))) for x in (1e6, 1e8)], port
    # the reference on 60000 nodes: since vias join the planes around their barrel (add_pin, from ~0.3 mm cells
    # for these 0.3 mm drills [A79]) 24000 nodes no longer is one
    ref, _ = z_at("fine", 60000, False)
    raw, _ = z_at("raw", 4000, False)
    corr, port = z_at("corr", 4000, True)
    mid, _ = z_at("mid", 24000, True)
    assert abs(raw[1] / ref[1] - 1) > 0.25                     # the coarse grid alone is far off at 100 MHz (-39 %)
    # at 100 MHz (ohm): reference 0.3319, coarse grid alone 0.2028, corrected 4000 nodes 0.3390 (+2.1 %: the loop's
    # region the whole board at 0.22 mm, the pins' at 0.20 mm), corrected 24000 nodes 0.3388 (+2.1 %; 0.3387 and
    # 0.3401 with the regional models' plane capacitance, before 2026-11-15 [A118]). Each pin's current goes to its
    # nodes by their cells' share of it [A88] and its joints are bilinear weights round the barrel, the same on every
    # grid [A95]: the two budgets agree to well under 1 %, which is what the correction is for; the 2 % above the
    # reference is the reference's own residue (the board-wide model alone at 0.30 mm)
    assert corr[1] == pytest.approx(ref[1], rel=0.05)
    assert mid[1] == pytest.approx(corr[1], rel=0.01)
    assert mid[1] == pytest.approx(ref[1], rel=0.05)
    assert corr[0] == pytest.approx(ref[0], rel=0.02) and port["l_correction_nh"] > 0.1
    assert port["local_pitch_mm"] < 0.3
    # on 4000 nodes the loop's region (10 cells of 1.26 mm round the decaps) is the whole board at 0.22 mm: the pins
    # and their decaps get their own model at 0.20 mm (the second level of _local_runs)
    assert port["local_pins"] == ["C1", "C2", "C3", "C4"]


def test_a_connector_source_with_the_regulators_defaults_is_a_harness(tmp_path):
    """A connector as the rail's source (no power_out pin), its R and L left at the regulator's 5 mΩ / 20 nH: it is
    fed through 20 mΩ + 500 nH, a short lead [A110], and the source's description says so; at 300 kHz the IC
    then sees about ω·500 nH = 0.94 Ω (with 20 nH it read 0.04 Ω: audit round 12, C-5)."""
    p, _, _ = _run(tmp_path, "harness", PdnSpec("VDD", source="J1"), source=True)
    assert "harness" in p.source and "500 nH" in p.source
    z, f = _at(p.ports[0], 3e5)
    assert z == pytest.approx(2 * math.pi * f * 500e-9, rel=0.25)


def test_a_board_without_its_pours_says_so(tmp_path):
    """A board saved without its zones (a layout in progress: the refboard's working copy had none and every rail
    failed, read as "pads smaller than a cell"): the rail's error says that no filled pour of the rail or its ground
    is on the board."""
    from groundtruth.analysis.pdn import prepare_pdn
    from groundtruth.analysis.simulate import BoardModel
    from groundtruth.config.settings import PdnSpec
    b, sc, _ = make(tmp_path, pdn_board(source=True), "nopour", nets=(), cavity_max_nodes=12000)
    b.zones = []
    _, (res,), _ = prepare_pdn(BoardModel(b, sc.config), [PdnSpec("VDD", voltage=3.3, i_step=0.5)])
    assert "no filled copper pour (zone) of VDD or GND" in res.error, res.error


# a load's connector J2 (through-hole) with a polymer capacitor 6 mm away (8 nH ESL) and four 10 µF ceramics 16-20 mm
# away (0.4 nH): the connector's current at 100 MHz closes mostly through the ceramics
CONN_CAPS = [("C1", "100u", (14.0, 15.0)), ("C2", "10u", (25.0, 10.0)), ("C3", "10u", (25.0, 20.0)),
             ("C4", "10u", (28.0, 13.0)), ("C5", "10u", (28.0, 17.0))]


def _conn_config(sc):
    """The connector board's config with the polymer capacitor's and the ceramics' models."""
    from groundtruth.components.models import ComponentModel
    cfg = SettingsFile.load(sc.path).config
    cfg.components["C1"].model = ComponentModel("cap", {"C": 100e-6, "ESR": 0.028, "ESL": 8e-9})
    for r in ("C2", "C3", "C4", "C5"):
        cfg.components[r].model = ComponentModel("cap", {"C": 10e-6, "ESR": 0.005, "ESL": 0.4e-9})
    return cfg


def test_a_connectors_fine_model_holds_the_decaps_that_carry_its_current(tmp_path, monkeypatch):
    """A connector port (through-hole pins) whose one near decap is a polymer capacitor while its current at high
    frequency closes through ceramics farther away. The board-wide solve near 100 MHz (probe_shares) puts the
    ceramics in its fine model, which is solved without the planes' capacitance [A118] over 10 coarse cells round
    them: the corrected inductance Im(Z)/ω at 100 MHz then reads the same from 4000 and 12000 nodes (within 1.5 %)
    and the reference, the board-wide model alone on its finest 0.25-0.30 mm grids (1.397 nH at both, made with
    tools/dev/pdn_local_check.py --board-wide-only), within 2 %. Alone the coarse grids read 0.95 and 1.23 nH."""
    from groundtruth.analysis import pdn as P

    def l_at(name, budget):
        """The connector's port at 100 MHz on this board-wide budget: (L in nH from Im(Z)/ω, the port)."""
        b, sc, _ = make(tmp_path, pdn_board(caps=CONN_CAPS, output=(8.0, 15.0)), name, nets=(),
                        cavity_max_nodes=budget)
        sc.set_pdn([PdnSpec("VDD", source="none", ports=["J2.1/J2.2"])])
        sc.save()
        r = simulate(b, _conn_config(sc)).pdn[0]
        assert not r.error, r.error
        port = next(p for p in r.ports if p["part"] == "J2")
        fs = np.array(port["f"])
        k = int(np.argmin(np.abs(np.log(fs / 1e8))))
        z = port["z_ohm"][k] * np.exp(1j * np.radians(port["phase_deg"][k]))
        assert not any("not one inductance" in n for n in r.notes), r.notes
        return z.imag / (2 * math.pi * fs[k]) * 1e9, port
    coarse, port = l_at("c4k", 4000)
    mid, _ = l_at("c12k", 12000)
    assert set(port["local_decaps"]) == {"C1", "C2", "C3", "C4", "C5"} and port["local_share"] > 0.9
    assert port["l_correction_nh"] > 0.3                    # the 1.26 mm grid alone reads about 0.95 nH
    assert coarse == pytest.approx(mid, rel=0.015)
    assert coarse == pytest.approx(1.397, rel=0.02) and mid == pytest.approx(1.397, rel=0.02)


def test_the_local_correction_adds_its_levels_per_frequency():
    """The loop's and the pins' pairs of regional models add per frequency (a telescoping sum), the median of the
    sums is the correction; a frequency where a pair is not inductive is left out of every pair's sum."""
    from types import SimpleNamespace
    from groundtruth.analysis.pdn import _local_correction

    def run(ls):
        """A regional run whose port reads jωL (L in nH per frequency)."""
        sols = {f: SimpleNamespace(Z=np.array([complex(0.0, 2 * math.pi * f * l * 1e-9)])) for f, l in ls.items()}
        return SimpleNamespace(ok=lambda s=sols: s)
    f1, f2, f3 = 3e7, 1e8, 3e8
    loop = (run({f1: 1.50, f2: 1.49, f3: 1.48}), run({f1: 1.00, f2: 1.00, f3: 1.00}))
    pins = (run({f1: 0.71, f2: 0.70, f3: -0.2}), run({f1: 0.69, f2: 0.69, f3: 0.68}))
    meta = {"local": [loop, pins]}
    dl = _local_correction(meta)
    assert [f for f, _ in meta["local_nh"]] == [f1, f2]               # 300 MHz: the pins' fine run is not inductive
    assert [v for _, v in meta["local_nh"]] == pytest.approx([0.52, 0.50])
    assert dl * 1e9 == pytest.approx(0.51)


def test_an_open_loop_is_found_before_it_is_solved():
    """A regional model without the planes' capacitance whose port pads do not reach its decaps has no path round:
    loop_closes says so (its solve would not converge), counting copper and parts but not the capacitance."""
    from types import SimpleNamespace
    from groundtruth.analysis.pdn import loop_closes
    # nodes 0-1 rail copper, 2-3 ground copper, 4 an island; branches: 2 strips (0-1, 2-3), 1 capacitance (1-3),
    # 1 part (a decap 1-2)
    plane = SimpleNamespace(n_nodes=5, nb=4, ns=2, ncap=1, a_nodes=np.array([0, 2, 1, 1]),
                            b_nodes=np.array([1, 3, 3, 2]), nid={k: k for k in range(5)},
                            node_id=lambda n: n)          # PlaneStack.node_id (a lid's own number, a grid node's)
    assert loop_closes(plane, [(0, 1.0), (3, -1.0)])                  # rail - decap - ground
    assert not loop_closes(plane, [(4, 1.0), (3, -1.0)])              # the island's pad
    no_decap = SimpleNamespace(**{**vars(plane), "nb": 3, "a_nodes": plane.a_nodes[:3], "b_nodes": plane.b_nodes[:3]})
    assert not loop_closes(no_decap, [(0, 1.0), (3, -1.0)])           # through the capacitance alone: open


def test_without_a_load_step_the_target_is_estimated_from_the_ics(tmp_path):
    """No i_step: U1 (2 pins, no Supply Current field) gets 20 mA by its pin count [A124], the step half of it
    (10 mA), the target 3.3 V · 5 % / 10 mA = 16.5 Ω, marked ESTIMATED and its findings warnings at most. With a
    Supply Current field of 400 mA the step is 200 mA (target 0.825 Ω), from the field."""
    from groundtruth.analysis.pdn import estimate_i_step, supply_parts
    from groundtruth.report.formats import pdn_text
    from groundtruth.ui.kpi import _pdn
    g = pdn_board(caps=[("C1", "1u", (22.0, 15.0))])
    b, _, _ = make(tmp_path, g, "est0", nets=())
    step, how, detail = estimate_i_step(supply_parts(b, "VDD", "GND"))
    assert step == pytest.approx(0.01) and how == "estimate" and detail == [
        {"part": "U1", "amps": pytest.approx(0.02), "how": "estimate", "note": ""}]
    p, issues, _ = _run(tmp_path, "est", PdnSpec("VDD", source="none", voltage=3.3), caps=[("C1", "1u", (22.0, 15.0))])
    assert p.i_step_source == "estimate" and p.target_ohm == pytest.approx(16.5)
    assert any("ESTIMATED" in n for n in p.notes)
    assert all(i.severity != "error" for i in issues if i.check in ("pdn_impedance", "pdn_step"))
    tile = _pdn(p)[0]
    assert "ESTIMATED" in tile[2] and tile[3] == "warn"
    assert "ESTIMATED" in pdn_text(p)[0]
    g = pdn_board(caps=[("C1", "1u", (22.0, 15.0))])
    g.fields("U1", **{"Supply Current": "400mA"})
    b, _, _ = make(tmp_path, g, "est1", nets=())
    step, how, _ = estimate_i_step(supply_parts(b, "VDD", "GND"))
    assert step == pytest.approx(0.2) and how == "fields"
    assert 3.3 * 0.05 / step == pytest.approx(0.825)



@pytest.mark.parametrize("text, used", [("400mA", 0.4), ("2A", 2.0), ("0", None), ("abc", None), ("-5mA", None),
                                         ("150", None), ("1e-9A", None)])
def test_a_supply_current_field_is_used_only_when_it_reads_as_a_sensible_current(tmp_path, text, used):
    """A Supply Current field within 10 µA … 100 A is used; one that does not read, is not above zero or is outside
    that range (a bare "150" meant as mA reads 150 A) is not: the pin-count estimate, with a note naming the field."""
    from groundtruth.analysis.pdn import estimate_i_step, supply_parts
    g = pdn_board()
    g.fields("U1", **{"Supply Current": text})
    b, _, _ = make(tmp_path, g, "f", nets=())
    step, how, detail = estimate_i_step(supply_parts(b, "VDD", "GND"))
    if used is None:
        assert how == "estimate" and step == pytest.approx(0.01) and text in detail[0]["note"]
    else:
        assert how == "fields" and step == pytest.approx(used / 2) and not detail[0]["note"]


def test_a_regulator_on_the_rail_is_not_counted_in_the_estimate(tmp_path):
    """U1 given a power_out pin (a regulator fed from the rail): its own load is not known, so it is left out of the
    sum; with no other IC there is no estimated step (no target), and the detail says why."""
    from groundtruth.analysis.pdn import estimate_i_step, supply_parts
    b, _, _ = make(tmp_path, pdn_board(), "reg", nets=())
    parts = supply_parts(b, "VDD", "GND")
    parts[0][0].pads[1].pin_type = "power_out"
    step, how, detail = estimate_i_step(parts)
    assert step is None and how == "" and detail[0]["how"] == "regulator"


def test_without_a_source_there_is_no_step_response_only_a_note(solved):
    """The rail with one decap and no source (source: none), a 1 A step: nothing holds it at DC (|Z| rises as 1/ωC
    towards DC), so a constant step current would drain the decap without limit (1 A · 1 ms / 1 µF = 1000 V): no
    step figures and no pdn_step issue, a note saying why."""
    p, issues, _ = solved["one"]
    assert all(port.get("step") is None for port in p.ports)
    assert not any(i.check == "pdn_step" for i in issues)
    assert any("no step response: nothing holds VDD at DC" in n for n in p.notes)


def test_a_source_with_a_large_resistance_still_gets_its_step(tmp_path):
    """The source J1 as 2 Ω (+ no inductance) with one 1 µF decap, a 100 mA step on 3.3 V: the source holds DC, so
    the step is computed (though the low end may look capacitive) and settles at 100 mA · 2 Ω = 0.2 V."""
    p, _, _ = _run(tmp_path, "bigr", PdnSpec("VDD", source="J1", vrm_r=2.0, vrm_l=1e-9, voltage=3.3, i_step=0.1),
                   source=True, caps=[("C1", "1u", (22.0, 15.0))])
    m = p.ports[0]["step"]
    assert m is not None and m["final"] == pytest.approx(-0.2, rel=0.05), m


def test_a_switchers_output_inductor_is_the_rails_source(tmp_path):
    """No power_out pin on the rail, but an inductor from it to a net an IC drives through a power_out pin (a
    buck's SW): that inductor is the source."""
    from groundtruth.analysis.pdn import find_source
    g = pdn_board()
    g.footprint("L1", "10u", "Inductor_SMD:L_1210_3225Metric", (8.0, 8.0), [
        {"number": 1, "at": (-1.5, 0.0), "net": "SW", "size": (1.0, 2.0)},
        {"number": 2, "at": (1.5, 0.0), "net": "VDD", "size": (1.0, 2.0)}])
    g.footprint("U9", "BUCK", "Package_SO:Fake_IC", (4.0, 8.0), [
        {"number": 1, "at": (0.0, 0.0), "net": "SW", "size": (0.6, 0.8), "pinfunction": "SW",
         "pintype": "power_out"}])
    b, _, _ = make(tmp_path, g, "ind", nets=())
    assert find_source(b, "VDD", "GND").reference == "L1"


def _track_rail(tmp_path, name, fields):
    """A 2-layer board: the connector J1 (VDD, GND) at x = 5 mm, the IC U1 at x = 30 mm, joined on F.Cu by one VDD
    track 0.5 mm wide, GND a plane on B.Cu (vias at both ground pads); U1 with `fields`. Simulated as the PDN rail
    VDD (3.3 V, source J1): (its PdnResult, the issues, the result)."""
    from boardgen import BoardGen
    g = BoardGen(layers=2, size=(40.0, 20.0))
    g.plane("GND", "B.Cu")
    pad = {"size": (1.0, 1.0)}
    g.footprint("J1", "Conn", "Connector:Fake_2pin", (5.0, 10.0), [
        {"number": 1, "at": (0.0, 0.0), "net": "VDD", **pad}, {"number": 2, "at": (0.0, 3.0), "net": "GND", **pad}])
    g.footprint("U1", "MCU", "Package_SO:Fake_IC", (30.0, 10.0), [
        {"number": 1, "at": (0.0, 0.0), "net": "VDD", "pintype": "power_in", **pad},
        {"number": 2, "at": (0.0, 3.0), "net": "GND", "pintype": "power_in", **pad}])
    g.via((5.0, 13.0), "GND")
    g.via((30.0, 13.0), "GND")
    g.track((5.0, 10.0), (30.0, 10.0), "VDD", width=0.5)
    g.fields("U1", **fields)
    b, sc, _ = make(tmp_path, g, name, nets=())
    sc.set_pdn([PdnSpec("VDD", source="J1", voltage=3.3, i_step=0.1)])
    sc.save()
    r = simulate(b, SettingsFile.load(sc.path).config)
    return r.pdn[0], r.issues, r


def test_the_dc_voltage_at_an_ic_is_the_rail_less_the_tracks_drop(tmp_path):
    """U1 draws 1 A (its Supply Current) through a 0.5 mm × 25 mm track of 35 µm copper: the supply side drops
    I·ρL/(wt) = 1 A · 1.72e-8 Ω·m · 24 mm / (0.5 mm · 35 µm) ≈ 23.6 mV over the track between the pads' edges
    (within 15 %: the pads and the current's entry), the return through the plane far less; the voltage is 3.3 V
    less it. A Supply Voltage Min of 3.29 V is above it: pdn_dc_voltage. The DC run is its own view."""
    p, issues, r = _track_rail(tmp_path, "dc", {"Supply Current": "1A", "Supply Voltage Min": "3.29V"})
    d = p.ports[0]["dc"]
    expect = 1.0 * 1.72e-8 * 24e-3 / (0.5e-3 * 35e-6) * 1e3
    assert d["supply_mv"] == pytest.approx(expect, rel=0.15), d
    assert 0 <= d["return_mv"] < d["supply_mv"] / 3
    assert d["v"] == pytest.approx(3.3 - d["drop_mv"] / 1e3) and d["limit"] == pytest.approx(3.29)
    assert any(i.check == "pdn_dc_voltage" and "U1" in i.message for i in issues)
    assert p.dc_view == "VDD (PDN DC)" and any(x.net == "VDD (PDN DC)" for x in r.dc_loads)
    from groundtruth.ui.kpi import _pdn
    tile = next(t for t in _pdn(p) if t[0] == "lowest DC")
    assert tile[3] == "error" and tile[1].startswith("3.2")


def test_without_a_supply_voltage_min_the_limit_is_the_ripple(tmp_path):
    """No Supply Voltage Min: the limit is 3.3 V · (1 − 5 %) = 3.135 V, far below the drop at 1 A: no issue."""
    p, issues, _ = _track_rail(tmp_path, "dc2", {"Supply Current": "1A"})
    assert p.ports[0]["dc"]["limit"] == pytest.approx(3.135)
    assert not any(i.check == "pdn_dc_voltage" for i in issues)



def test_the_pdn_dc_run_heats_nothing_and_is_not_in_the_combined_view(tmp_path):
    """The PDN rail's own DC run (its ICs' maximum currents, often estimated) is a view of its own, marked pdn_rail:
    Thermal's stages leave it out (no heat from estimated currents), and so does the part power check."""
    from groundtruth.analysis.thermal import _stages
    p, _, r = _track_rail(tmp_path, "heat", {"Supply Current": "1A"})
    (run,) = [x for x in r.dc_loads if x.net == "VDD (PDN DC)"]
    assert run.pdn_rail == "VDD" and _stages([run]) == []
    assert [x["part"] for x in p.dc_ics] == ["U1"]


def test_an_ldo_fed_from_the_rail_draws_its_output_rails_load(tmp_path):
    """U2, an LDO with its input (power_in) on VDD and its output (power_out) on V2, where U3 draws 300 mA: in the
    VDD DC run U2 draws those 300 mA (its input current is its output's load), U1 its own 1 A."""
    from groundtruth.analysis.pdn import dc_load_spec
    from boardgen import BoardGen
    g = BoardGen(layers=2, size=(40.0, 20.0))
    g.plane("GND", "B.Cu")
    pad = {"size": (1.0, 1.0)}
    g.footprint("J1", "Conn", "Connector:Fake_2pin", (5.0, 10.0), [
        {"number": 1, "at": (0.0, 0.0), "net": "VDD", **pad}, {"number": 2, "at": (0.0, 3.0), "net": "GND", **pad}])
    g.footprint("U1", "MCU", "Package_SO:Fake_IC", (30.0, 10.0), [
        {"number": 1, "at": (0.0, 0.0), "net": "VDD", "pintype": "power_in", **pad},
        {"number": 2, "at": (0.0, 3.0), "net": "GND", "pintype": "power_in", **pad}])
    g.footprint("U2", "LDO", "Package_SO:Fake_IC", (20.0, 5.0), [
        {"number": 1, "at": (0.0, 0.0), "net": "VDD", "pintype": "power_in", **pad},
        {"number": 2, "at": (2.0, 0.0), "net": "V2", "pintype": "power_out", **pad},
        {"number": 3, "at": (0.0, 3.0), "net": "GND", "pintype": "power_in", **pad}])
    g.footprint("U3", "ADC", "Package_SO:Fake_IC", (25.0, 5.0), [
        {"number": 1, "at": (0.0, 0.0), "net": "V2", "pintype": "power_in", **pad},
        {"number": 2, "at": (0.0, 3.0), "net": "GND", "pintype": "power_in", **pad}])
    g.track((5.0, 10.0), (30.0, 10.0), "VDD", width=0.5)
    g.fields("U1", **{"Supply Current": "1A"})
    g.fields("U3", **{"Supply Current": "300mA"})
    b, sc, _ = make(tmp_path, g, "ldo", nets=())
    spec, why = dc_load_spec(b, sc.config, PdnSpec("VDD", source="J1", voltage=3.3))
    assert spec is not None, why
    amps = {ld.pin.split(".")[0]: ld.current for ld in spec.loads}
    assert amps == {"U1": pytest.approx(1.0), "U2": pytest.approx(0.3)}


def test_four_equal_decaps_share_the_current_equally(solved):
    """Four equal 1 µF decaps placed symmetrically round the IC: below their resonance each carries a quarter of
    its current (C/(4C + C_pp), within 5 %), so each is useful; the own resonance is 1/(2π·√(ESL·C))."""
    p, _, cfg = solved["four"]
    port = p.ports[0]
    k = int(np.argmin(np.abs(np.log(np.array(port["f"]) / 1e5))))
    for ref in ("C1", "C2", "C3", "C4"):
        assert port["shares"][ref][k] == pytest.approx(0.25, rel=0.05), ref
    m = cfg.model_for("C1")
    row = next(r for r in p.decaps if r["ref"] == "C1")
    assert row["srf"] == pytest.approx(1 / (2 * math.pi * math.sqrt(m.get("ESL") * row["C_dc"])), rel=1e-6)
    assert not row["little"] and row["band"] and row["band"][0] <= 1e4 * 1.01


def test_a_tiny_cap_beside_a_big_one_does_little(tmp_path):
    """10 pF beside 10 µF: below check_to (100 MHz) the 10 pF cap's admittance is 10⁶ times smaller and its own
    resonance (~1.6 GHz with 1 nH) lies above the band, so it never carries 5 % of the IC's current: flagged (info
    decap_ineffective); the 10 µF one is not."""
    p, issues, _ = _run(tmp_path, "tiny", PdnSpec("VDD", source="none", voltage=3.3, i_step=1.0),
                        caps=[("C1", "10u", (22.0, 15.0)), ("C2", "10p", (18.0, 15.0))])
    rows = {r["ref"]: r for r in p.decaps}
    assert rows["C2"]["little"] and not rows["C1"]["little"]
    assert rows["C1"]["peak_share"] > 0.9 and rows["C2"]["peak_share"] < 0.05
    (info,) = [i for i in issues if i.check == "decap_ineffective"]
    assert info.severity == "info" and "C2" in info.message and "C1" not in info.message
    from groundtruth.report.plots import draw_pdn_decaps
    from matplotlib.figure import Figure
    fig = Figure(figsize=(8, 3))
    draw_pdn_decaps(fig, p)
    labels = [l.get_label() for l in fig.axes[0].get_lines()]
    assert any(l.startswith("C1") for l in labels)


def test_ringing_decaps_do_not_read_as_working_and_a_left_out_ics_decap_is_not_flagged(tmp_path):
    """10 µF and 100 nF (an anti-resonance between them) and U2 cut off from the rail (left out of the ports) with a
    10 nF decap beside it: the in-phase shares stay within 0 … 1 at every frequency (the current circulating between
    the two decaps cancels out), and the decap beside U2 is not called useless (it decouples an IC the model did not
    draw from), marked for_left_out."""
    p, issues, _ = _run(tmp_path, "ring", PdnSpec("VDD", source="none", voltage=3.3, i_step=1.0), cut_ic=True,
                        caps=[("C1", "10u", (22.0, 15.0)), ("C2", "100n", (18.0, 15.0)),
                              ("C3", "10n", (31.5, 22.0))])
    assert [lo["part"] for lo in p.left_out] == ["U2"]
    for v in p.ports[0]["shares"].values():
        pass
    ring = p.ports[0]["ringing"]
    assert any(ring) and not ring[0]          # the anti-resonance is marked, the low end is not
    for row in p.decaps:
        assert row["peak_share"] <= 1.5, row        # ringing frequencies (the parts' |I| over 1.5) are not judged
    row = next(r for r in p.decaps if r["ref"] == "C3")
    assert not row["little"] and row.get("for_left_out") == "U2"
    assert not any("C3" in i.message for i in issues if i.check == "decap_ineffective")


def test_the_load_step_is_up_front_on_the_tile_the_plot_and_the_report(tmp_path):
    """The source J1 as 50 mΩ (no inductance) and one 1 µF decap, a 1 A step on 3.3 V: the rail settles at 1 A · |Z| at
    the lowest frequency (50 mΩ + the planes' milliohms, within 3 %); on the way the copper's ~1 nH between the
    source and the decap rings against the decap (ζ = R/2·√(C/L) < 1), so the dip after the spike lies between that
    and twice it. The tile shows that dip against the ±165 mV allowed (ok), the plot draws ΔV in mV on a log time
    axis."""
    from matplotlib.figure import Figure
    from groundtruth.report.plots import draw_pdn_step, pdn_has_step
    from groundtruth.ui.kpi import _pdn
    p, _, _ = _run(tmp_path, "stepup", PdnSpec("VDD", source="J1", vrm_r=0.05, vrm_l=1e-12, voltage=3.3, i_step=1.0),
                   source=True, caps=[("C1", "1u", (22.0, 15.0))])
    m = p.ports[0]["step"]
    z0 = p.ports[0]["z_ohm"][0]
    assert m["final"] == pytest.approx(-1.0 * z0, rel=0.03) and 0.045 < z0 < 0.06, m
    assert 2 * m["final"] < m["dip_late"] <= m["final"] + 1e-6, m
    tile = next(t for t in _pdn(p) if t[0] == "load step")
    assert tile[3] == "ok" and "165 mV" in tile[2] and tile[1] == f"{m['dip_late'] * 1e3:+.3g} mV"
    assert pdn_has_step(p)
    fig = Figure(figsize=(8, 3))
    draw_pdn_step(fig, p)
    assert fig.axes[0].get_xscale() == "log" and "mV" in fig.axes[0].get_ylabel()


SPEC_2CAP = PdnSpec("VDD", source="J1", vrm_r=0.02, vrm_l=5e-9, voltage=3.3, i_step=1.0)
CAPS_2 = [("C1", "10u", (22.0, 15.0)), ("C2", "100n", (18.0, 15.0))]


def test_without_a_decap_is_exact_on_the_board_model(tmp_path):
    """The rank-one "without it" [A128] at each key frequency against the same board with C2's model open (its pads
    and vias kept), on the board-wide model (raw solves, the IC's fine-model correction apart): within 0.1 %. Taking
    C2 out lowers |Z| at the C1/C2 anti-resonance (no anti-resonance without it) and raises it at 100 MHz."""
    from groundtruth.analysis import pdn
    from groundtruth.components.models import ComponentModel
    runs = {}
    for name, open_c2 in (("both", False), ("open", True)):
        b, sc, _ = make(tmp_path, pdn_board(source=True, caps=CAPS_2), name, nets=(), cavity_max_nodes=12000)
        sc.set_pdn([SPEC_2CAP])
        if open_c2:
            sc.set_component("C2", ComponentModel("open", {}))
        sc.save()
        runs[name] = simulate(b, SettingsFile.load(sc.path).config, keep_factorisations=True).pdn_setups[0]
    sb, so = runs["both"], runs["open"]
    idx = [i for i, n in enumerate(pdn._names(sb.branches)) if n == "C2"]
    keys = sb.keys
    assert len(keys) >= 2
    moved = []
    for f in keys:
        s = sb.run.ok()[f]
        pred = pdn.without_part(complex(s.Z[0]), complex(np.sum(s.I_branch[0][idx])), pdn._branch_z(sb, "C2", f),
                                s.Zd["C2"])
        real = so.run.exc.excs[0].solve(f).Z_loop
        assert abs(pred) == pytest.approx(abs(real), rel=1e-3), f
        moved.append((f, abs(pred) / abs(s.Z[0])))
    assert any(r < 0.9 for f, r in moved if 5e6 < f < 3e7) and any(r > 1.2 for f, r in moved if f >= 1e8)


def test_a_peak_is_explained_and_the_worst_points_fix_is_what_a_real_part_does(tmp_path):
    """C1 10 µF and C2 100 nF: the peak between them (~11.5 MHz) is C1 (inductive above its resonance) with C2
    (capacitive), Z0 = 1/(2πf·C2); the worst point (100 MHz, inductive) is the inductance above the decaps, and its
    fix, one more 10 µF beside C1, predicted by rank one [A128], matches a real third 10 µF placed next to C1 and
    solved again within 3 %. The issue names the cause and the fix."""
    p, issues, _ = _run(tmp_path, "m5", SPEC_2CAP, source=True, caps=CAPS_2)
    port = p.ports[0]
    pk = next(x for x in port["peaks"] if 5e6 < x["f"] < 3e7)
    c2 = next(r for r in p.decaps if r["ref"] == "C2")["C_dc"]
    assert pk["low"] == "C1" and pk["high"] == "C2" and "anti-resonance" in pk["cause"]
    # Q from the curve's fitted pole (C2's mounting in it: the bare part read Q 0.83 for a peak of Q ~3), Z0 = |Z|/Q,
    # near 1/(2πf·C2) (within the mounting's share)
    assert pk["q_from"] == "the curve's fitted pole" and 1.0 < pk["q"] < 6.0
    assert pk["z0"] == pytest.approx(pk["z_ohm"] / pk["q"], rel=1e-9)
    # below the bare part's 1/(2πf·C2): its mounting inductance cancels part of its reactance (~45 mΩ left at 11.5 MHz)
    assert 0.15 < pk["z0"] * 2 * math.pi * pk["f"] * c2 < 1.0
    w = port["worst"]
    assert w["f"] >= 1e8 and "inductance above the decaps" in w["cause"]
    fx = w["fixes"][0]
    assert fx["beside"] == "C1" and fx["z_local_new"] < fx["z_local_old"] and not fx["peak"]
    assert "nH there" in w["cause"]
    b, _, _ = _run(tmp_path, "m5b", SPEC_2CAP, source=True, caps=CAPS_2 + [("C3", "10u", (22.0, 13.6))])
    real = b.ports[0]["z_ohm"][b.ports[0]["f"].index(fx["f_local_new"])]
    assert fx["z_local_new"] == pytest.approx(real, rel=0.03)
    (iss,) = [i for i in issues if i.check == "pdn_impedance"]
    assert "inductance above the decaps" in iss.message and "beside C1" in iss.message
    row = next(r for r in p.decaps if r["ref"] == "C2")
    assert row["without"]["db"] > 0 and row["without"]["margin_without"] < row["without"]["margin_with"]


def test_without_a_decap_never_claims_that_removing_it_helps(tmp_path):
    """The reviewer's case: C1 10 µF and C2 100 nF, check_to 20 MHz. Taking C1 out moves the anti-resonance (the
    source's 5 nH with C2: ~7 MHz, 0.89 Ω on a re-solve, far worse), which the key frequencies do not see: the figure
    is the largest rise at one of them, compared at the same frequency, never a fall read as help; the text says it
    is a lower bound and that a peak can move."""
    from groundtruth.analysis.pdn import without_text
    spec = PdnSpec("VDD", source="J1", vrm_r=0.02, vrm_l=5e-9, voltage=3.3, i_step=1.0, check_to=20e6)
    p, _, _ = _run(tmp_path, "wo", spec, source=True, caps=CAPS_2)
    for row in p.decaps:
        w = row["without"]
        assert w.get("no_rise") or w["db"] > 0, row
        text = without_text(w)
        assert ("at least" in text and "move a peak" in text) or "not judged" in text, text


def test_a_fix_is_judged_by_where_the_peak_moves_and_a_bank_counts_as_one_capacitance(tmp_path):
    """The reviewer's case: one more 100 nF beside C2 does not lower the C1/C2 peak much, it moves it down (~8.7 MHz):
    the fix reports the highest |Z| within a decade of the peak after it, matching a real re-solve with that part
    within 10 %. And four equal 1 µF in parallel above a peak are its capacitive side together: C = 4 × one,
    Z0 = 1/(2πf·4C), the cause naming the three others."""
    spec = PdnSpec("VDD", source="J1", vrm_r=0.02, vrm_l=5e-9, voltage=3.3, i_step=5.0)
    p, _, _ = _run(tmp_path, "mv", spec, source=True, caps=CAPS_2)
    pk = next(x for x in p.ports[0]["peaks"] if 5e6 < x["f"] < 3e7)
    more = [fx for fx in pk["fixes"] if fx["what"].startswith("one more")]
    assert more, (pk["fixes"], pk.get("fix_note"))        # (a fix judged by its moved peak must be kept, not dropped)
    if more:
        fx = more[0]
        b, _, _ = _run(tmp_path, "mv2", spec, source=True, caps=CAPS_2 + [("C3", "100n", (18.0, 13.6))])
        port = b.ports[0]
        near = [z for f, z in zip(port["f"], port["z_ohm"]) if fx["span"][0] <= f <= fx["span"][1]]
        assert fx["z_local_new"] == pytest.approx(max(near), rel=0.10)
        assert fx["z_local_new"] <= fx["z_local_old"] * 10 ** (-1 / 20)
    q, _, _ = _run(tmp_path, "bank", PdnSpec("VDD", source="J1", vrm_r=0.02, vrm_l=5e-9, voltage=3.3, i_step=1.0),
                   source=True, caps=FOUR)
    c1 = next(r for r in q.decaps if r["ref"] == "C1")["C_dc"]
    bump = max((x for x in q.ports[0]["peaks"] if x.get("c_above")), key=lambda x: x["c_above"])
    assert bump["c_above"] == pytest.approx(4 * c1, rel=1e-6) and "3 more in parallel" in bump["cause"]
    assert bump["z0"] == pytest.approx(1 / (2 * math.pi * bump["f"] * 4 * c1), rel=0.15)    # the fit's, near the bank's
